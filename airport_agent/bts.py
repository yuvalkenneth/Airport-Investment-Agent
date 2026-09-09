"""Cached, narrow access to BTS T-100 traffic data."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO, TextIOWrapper
from pathlib import Path
import sqlite3
from typing import Literal
from zipfile import BadZipFile, ZipFile

import httpx
from pydantic import Field, field_validator, model_validator

from airport_agent.aviation import AirportQuery, AviationDataError, airport_details

BTS_T100_URL = "https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=Nv4Pn&gnoyr_VQ=FMG"
CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "t100.sqlite3"
FIELDS = (
    "DEPARTURES_SCHEDULED",
    "DEPARTURES_PERFORMED",
    "SEATS",
    "PASSENGERS",
    "DISTANCE",
    "ORIGIN",
    "DEST",
    "YEAR",
    "MONTH",
    "CLASS",
)
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


class TrafficQuery(AirportQuery):
    start_month: str = Field(description="Inclusive month in YYYY-MM format")
    end_month: str = Field(description="Inclusive month in YYYY-MM format")
    direction: Literal["outbound", "inbound", "both"] = "both"
    route_limit: int = Field(default=10, ge=1, le=25)

    @field_validator("start_month", "end_month")
    @classmethod
    def valid_month(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m")
        except ValueError as exc:
            raise ValueError("month must use YYYY-MM format") from exc
        return value

    @model_validator(mode="after")
    def valid_period(self) -> TrafficQuery:
        start, end = _month_key(self.start_month), _month_key(self.end_month)
        if end < start:
            raise ValueError("end_month must be on or after start_month")
        if end[0] - start[0] > 4:
            raise ValueError("T-100 queries are limited to five calendar years")
        return self


class _HiddenFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("type") == "hidden" and attributes.get("name"):
            self.values[attributes["name"]] = attributes.get("value") or ""


def _month_key(value: str) -> tuple[int, int]:
    year, month = value.split("-")
    return int(year), int(month)


def _cache() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    connection = sqlite3.connect(CACHE_PATH)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS responses "
        "(geography TEXT, year INTEGER, payload BLOB, fetched_at TEXT, "
        "PRIMARY KEY (geography, year))"
    )
    return connection


def _fetch_year(geography: str, year: int) -> bytes:
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            page = client.get(BTS_T100_URL)
            page.raise_for_status()
            parser = _HiddenFields()
            parser.feed(page.text)
            form = parser.values | {
                "cboGeography": geography,
                "cboYear": str(year),
                "cboPeriod": "All",
                "btnDownload": "Download",
            }
            form.update({field: "on" for field in FIELDS})
            response = client.post(BTS_T100_URL, data=form)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AviationDataError(f"BTS T-100 request failed: {exc}") from exc
    if not response.content.startswith(b"PK"):
        raise AviationDataError("BTS T-100 returned an unexpected response")
    return response.content


def _load_year(geography: str, year: int) -> tuple[bytes, bool]:
    with _cache() as connection:
        row = connection.execute(
            "SELECT payload FROM responses WHERE geography = ? AND year = ?",
            (geography, year),
        ).fetchone()
        if row:
            return row[0], True
        payload = _fetch_year(geography, year)
        connection.execute(
            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?)",
            (geography, year, payload, datetime.now(UTC).isoformat()),
        )
        return payload, False


def _rows(payload: bytes):
    try:
        archive = ZipFile(BytesIO(payload))
        csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
    except (BadZipFile, StopIteration) as exc:
        raise AviationDataError("BTS T-100 archive did not contain a CSV file") from exc
    with archive, archive.open(csv_name) as raw, TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        for row in csv.DictReader(text):
            yield {key.strip(): (value or "").strip() for key, value in row.items() if key}


def _number(row: dict[str, str], field: str) -> float:
    try:
        return float(row.get(field) or 0)
    except ValueError:
        return 0


def _clean(value: float) -> int | float:
    return int(value) if value.is_integer() else round(value, 2)


def airport_traffic(query: TrafficQuery) -> dict:
    airport = airport_details(query.airport)
    airport_code = airport.get("iataId") or query.airport
    geography = STATE_NAMES.get(airport.get("state", ""))
    if airport.get("country") != "US" or not geography:
        raise AviationDataError("T-100 geography filtering currently supports US airports")

    start, end = _month_key(query.start_month), _month_key(query.end_month)
    cache_hits, fetched, selected = [], [], []
    for year in range(start[0], end[0] + 1):
        payload, hit = _load_year(geography, year)
        (cache_hits if hit else fetched).append(year)
        for row in _rows(payload):
            key = (int(_number(row, "YEAR")), int(_number(row, "MONTH")))
            if not start <= key <= end or row.get("CLASS") != "F" or _number(row, "SEATS") <= 0:
                continue
            origin, destination = row.get("ORIGIN"), row.get("DEST")
            matches = (
                query.direction == "both" and airport_code in {origin, destination}
                or query.direction == "outbound" and origin == airport_code
                or query.direction == "inbound" and destination == airport_code
            )
            if matches:
                selected.append(row)

    totals = {field: sum(_number(row, field) for row in selected) for field in FIELDS[:4]}
    routes: dict[str, dict[str, float]] = {}
    for row in selected:
        other = row["DEST"] if row["ORIGIN"] == airport_code else row["ORIGIN"]
        route = routes.setdefault(other, {field: 0 for field in FIELDS[:4]})
        for field in route:
            route[field] += _number(row, field)

    def summary(values: dict[str, float]) -> dict:
        seats, passengers = values["SEATS"], values["PASSENGERS"]
        return {
            "scheduled_departures": _clean(values["DEPARTURES_SCHEDULED"]),
            "performed_departures": _clean(values["DEPARTURES_PERFORMED"]),
            "scheduled_minus_performed": _clean(values["DEPARTURES_SCHEDULED"] - values["DEPARTURES_PERFORMED"]),
            "available_seats": _clean(seats),
            "passengers": _clean(passengers),
            "passenger_load_factor_pct": round(passengers / seats * 100, 2) if seats else None,
        }

    top_routes = [
        {"other_airport": code, **summary(values)}
        for code, values in sorted(routes.items(), key=lambda item: item[1]["PASSENGERS"], reverse=True)[: query.route_limit]
    ]
    return {
        "airport": airport_code,
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "direction": query.direction,
        "service": "scheduled passenger service",
        "totals": summary(totals),
        "top_routes_by_passengers": top_routes,
        "cache": {"years_reused": cache_hits, "years_fetched": fetched},
        "source": {"name": "BTS T-100 Segment (All Carriers)", "url": BTS_T100_URL},
        "limitations": "Monthly reported traffic and capacity; it measures served passengers, not latent demand. Scheduled minus performed is not a cancellation count and can be negative.",
    }
