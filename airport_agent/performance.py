"""Historical arrival performance from BTS's filtered public HTML report."""

from contextlib import closing
from datetime import UTC, datetime
from html.parser import HTMLParser
import json
import sqlite3
import time

import httpx
from pydantic import ConfigDict, Field, model_validator

from airport_agent.aviation import AirportQuery, AviationDataError, _client, airport_details
from airport_agent.bts import CACHE_PATH, CACHE_TTL_SECONDS, TrafficQuery

FORM_URL = "https://www.transtats.bts.gov/OT_Delay/OT_DelayCause1.asp"
REPORT_URL = "https://www.transtats.bts.gov/OT_Delay/ot_delaycause1.asp?qv52ynB=qn6n&20=E"
CAUSES = ("Air Carrier Delay", "Weather Delay", "National Aviation System Delay",
          "Security Delay", "Aircraft Arriving Late")


def _period_value(month: str) -> int:
    year, number = map(int, month.split("-"))
    return year * 12 + number


class PerformanceQuery(AirportQuery):
    model_config = ConfigDict(extra="forbid")

    start_month: str = Field(description="Inclusive month in YYYY-MM format")
    end_month: str = Field(description="Inclusive month in YYYY-MM format; at most 12 months per request")

    @model_validator(mode="after")
    def valid_period(self) -> "PerformanceQuery":
        TrafficQuery(airport=self.airport, start_month=self.start_month, end_month=self.end_month)
        if _period_value(self.end_month) - _period_value(self.start_month) >= 12:
            raise ValueError("Performance queries are limited to 12 months")
        return self


class _ReportHTML(HTMLParser):
    """Read select values and table cells, including BTS's unclosed option tags."""

    def __init__(self, html: str):
        super().__init__()
        self.options: dict[str, list[str]] = {}
        self.selected: dict[str, str] = {}
        self.rows: list[list[str]] = []
        self._select = None
        self._rows: list[list[str]] = []
        self._cells: list[list[str]] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self._select = attrs.get("name")
        elif tag == "option" and self._select:
            value = attrs.get("value", "")
            self.options.setdefault(self._select, []).append(value)
            if self._select not in self.selected or "selected" in attrs:
                self.selected[self._select] = value
        elif tag == "tr":
            self._rows.append([])
        elif tag in ("td", "th"):
            self._cells.append([])
        elif tag == "br" and self._cells:
            self._cells[-1].append(" ")

    def handle_data(self, data):
        if self._cells:
            self._cells[-1].append(data)

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag in ("td", "th") and self._cells:
            cell = " ".join("".join(self._cells.pop()).split())
            if self._rows:
                self._rows[-1].append(cell)
        elif tag == "tr" and self._rows:
            self.rows.append(self._rows.pop())


def _parse_report(report: _ReportHTML, selection: dict) -> dict:
    if any(report.selected.get(key) != value for key, value in selection.items()):
        raise AviationDataError("BTS returned a different airport, carrier, or period than requested")
    header = ["", "Number of Operations", "% of Total Operations", "Delayed Minutes",
              "% of Total Delayed Minutes"]
    labels = ("On Time", *CAUSES, "Cancelled", "Diverted", "Total Operations")
    rows = [row for row in report.rows if row and row[0] in labels]
    if header not in report.rows or len(rows) != len(labels) or {row[0] for row in rows} != set(labels):
        raise AviationDataError("BTS returned no complete arrival summary or its table format changed")
    try:
        data = {row[0]: row for row in rows}

        def count(label, column):
            text = data[label][column].replace(",", "")
            if not text.isascii() or not text.isdecimal():
                raise ValueError("invalid count")
            return int(text)

        total = count("Total Operations", 1)
        on_time = count("On Time", 1)
        cancelled = count("Cancelled", 1)
        diverted = count("Diverted", 1)
        # Cause operation counts are prorated and rounded; their sum is not a flight count.
        delayed = total - on_time - cancelled - diverted
        minutes = count("Total Operations", 3)
        cause_minutes = {cause: count(cause, 3) for cause in CAUSES}
        if (total <= 0 or delayed < 0 or bool(delayed) != bool(minutes)
                or any(value > minutes for value in cause_minutes.values())
                or abs(sum(cause_minutes.values()) - minutes) > len(CAUSES)):
            raise ValueError("empty or inconsistent totals")
    except (ValueError, IndexError) as exc:
        raise AviationDataError("BTS returned missing, empty, or inconsistent arrival totals") from exc

    return {
        "totals": {
            "reported_operations": total, "on_time_arrivals": on_time,
            "delayed_arrivals": delayed, "cancellations": cancelled,
            "diversions": diverted, "delayed_minutes": minutes,
        },
        "metrics": {
            "on_time_pct": round(on_time / total * 100, 2),
            "delayed_15min_pct": round(delayed / total * 100, 2),
            "cancellation_pct": round(cancelled / total * 100, 2),
            "diversion_pct": round(diverted / total * 100, 2),
            "avg_delay_minutes_delayed_arrivals": round(minutes / delayed, 2) if delayed else None,
        },
        "delay_causes": [
            {"cause": cause, "delayed_minutes": value,
             "share_of_delay_minutes_pct": round(value / minutes * 100, 2) if minutes else None}
            for cause, value in cause_minutes.items()
        ],
    }


def _fetch_report(airport: str, start: str, end: str) -> dict:
    try:
        with _client() as client:
            response = client.get(FORM_URL)
            response.raise_for_status()
            form = _ReportHTML(response.text)
            airport_value = next((value for value in form.options.get("Airport", [])
                                  if value.split("::")[0] == airport), None)
            if not airport_value:
                raise AviationDataError(f"BTS arrival performance does not list airport {airport}")
            selection = {"Carrier": "All", "Airport": airport_value,
                         "PeriodFrom": str(_period_value(start)), "PeriodTo": str(_period_value(end))}
            if any(value not in form.options.get(key, []) for key, value in selection.items()):
                raise AviationDataError("The requested period is not available in the BTS arrival report")
            response = client.post(REPORT_URL, data=selection)
            response.raise_for_status()
            return _parse_report(_ReportHTML(response.text), selection)
    except httpx.HTTPError as exc:
        raise AviationDataError(f"BTS arrival performance request failed: {exc}") from exc


def airport_performance(query: PerformanceQuery) -> dict:
    airport = query.airport
    if len(airport) == 4:
        details = airport_details(airport)
        if details.get("country") != "US" or not details.get("iataId"):
            raise AviationDataError("BTS arrival performance requires a US airport with an IATA code")
        airport = AirportQuery(airport=details["iataId"]).airport
    CACHE_PATH.parent.mkdir(exist_ok=True)
    key = (airport, query.start_month, query.end_month)
    with closing(sqlite3.connect(CACHE_PATH)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS arrival_performance "
            "(airport TEXT, start_month TEXT, end_month TEXT, payload TEXT, fetched_at REAL, "
            "PRIMARY KEY (airport, start_month, end_month))"
        )
        cached = connection.execute(
            "SELECT payload, fetched_at FROM arrival_performance "
            "WHERE airport = ? AND start_month = ? AND end_month = ?", key,
        ).fetchone()
        hit = bool(cached and 0 <= time.time() - cached[1] < CACHE_TTL_SECONDS)
        result = json.loads(cached[0]) if hit else _fetch_report(*key)
        fetched_at = cached[1] if hit else time.time()
        if not hit:
            connection.execute("INSERT OR REPLACE INTO arrival_performance VALUES (?, ?, ?, ?, ?)",
                               (*key, json.dumps(result), fetched_at))
    return {
        "airport": airport,
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "direction": "inbound",
        "coverage": "Domestic arrivals reported to BTS; all reporting carriers in the selected period.",
        **result,
        "definitions": {
            "operation_rates": "All four operation percentages use reported_operations, including cancellations and diversions, as their denominator.",
            "delayed_arrivals": "Gate arrivals 15 or more minutes after schedule; total minus on-time, cancelled, and diverted operations.",
            "average_delay": "Delayed minutes divided by delayed arrivals; excludes on-time, cancelled, and diverted operations.",
        },
        "cache": {"hit": hit, "fetched_at": datetime.fromtimestamp(fetched_at, UTC).isoformat()},
        "source": {"name": "BTS Airline On-Time Statistics and Delay Causes", "url": FORM_URL},
        "limitations": (
            "Aggregate period totals do not verify completeness of each month's reporting. "
            "Coverage differs from T-100 commercial traffic; do not combine their flight denominators. "
            "NAS delay includes weather, traffic, ATC, and airport operations; it does not isolate infrastructure constraints. "
            "This report provides no taxi times, physical capacity utilization, or measured unmet demand."
        ),
    }
