"""Cached, airport-and-period queries to the public BTS T-100 summary API."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from airport_agent.aviation import AirportQuery, AviationDataError, _client, _json, airport_details

BTS_T100_URL = "https://data.bts.gov/resource/r495-tyji.json"
CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "t100.sqlite3"
CACHE_TTL_SECONDS = 24 * 60 * 60
SERVICE_SCOPE = "all reported commercial services; passenger and cargo, scheduled and nonscheduled"


class TrafficQuery(AirportQuery):
    model_config = ConfigDict(extra="forbid")

    start_month: str = Field(pattern=r"^\d{4}-\d{2}$", description="Inclusive month in YYYY-MM format")
    end_month: str = Field(pattern=r"^\d{4}-\d{2}$", description="Inclusive month in YYYY-MM format")
    direction: Literal["outbound"] = "outbound"

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
        if self.end_month < self.start_month:
            raise ValueError("end_month must be on or after start_month")
        if int(self.end_month[:4]) - int(self.start_month[:4]) > 4:
            raise ValueError("T-100 queries are limited to five calendar years")
        return self


class _TrafficMonth(BaseModel):
    origin_airport_code: str
    reporting_month: datetime
    total_departures: int = Field(ge=0)
    total_passengers: int = Field(ge=0)
    total_seats: int = Field(ge=0)


def _fetch_months(airport: str, start: str, end: str) -> list[dict]:
    params = {
        "$select": ",".join(_TrafficMonth.model_fields),
        "$where": (
            f"origin_airport_code = '{airport}' AND "
            f"reporting_month >= '{start}-01T00:00:00' AND "
            f"reporting_month <= '{end}-01T00:00:00'"
        ),
        "$order": "reporting_month",
        "$limit": 1000,
    }
    try:
        with _client() as client:
            rows = _json(client.get(BTS_T100_URL, params=params), "BTS T-100")
    except httpx.HTTPError as exc:
        raise AviationDataError(f"BTS T-100 request failed: {exc}") from exc
    # A five-year query needs at most 60 monthly rows.
    if not isinstance(rows, list) or len(rows) >= 1000:
        raise AviationDataError("BTS T-100 returned an unexpected or truncated response")
    return rows


def _load_months(airport: str, start: str, end: str) -> tuple[list[_TrafficMonth], dict]:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    key = (airport, start, end)
    with closing(sqlite3.connect(CACHE_PATH)) as connection, connection:
        # Old ZIP cache entries are left untouched and are never read.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS airport_summaries "
            "(airport TEXT, start_month TEXT, end_month TEXT, payload TEXT, fetched_at REAL, "
            "PRIMARY KEY (airport, start_month, end_month))"
        )
        cached = connection.execute(
            "SELECT payload, fetched_at FROM airport_summaries "
            "WHERE airport = ? AND start_month = ? AND end_month = ?", key,
        ).fetchone()
        hit = bool(cached and 0 <= time.time() - cached[1] < CACHE_TTL_SECONDS)
        raw = json.loads(cached[0]) if hit else _fetch_months(*key)
        try:
            rows = [_TrafficMonth.model_validate(row) for row in raw]
        except ValueError as exc:
            raise AviationDataError("BTS T-100 returned missing or invalid monthly totals") from exc
        months = [row.reporting_month.strftime("%Y-%m") for row in rows]
        if (len(set(months)) != len(months)
                or any(row.origin_airport_code != airport for row in rows)
                or any(not start <= month <= end for month in months)):
            raise AviationDataError("BTS T-100 returned duplicate months or rows outside the requested airport/period")
        fetched_at = cached[1] if hit else time.time()
        if not hit:
            connection.execute(
                "INSERT OR REPLACE INTO airport_summaries VALUES (?, ?, ?, ?, ?)",
                (*key, json.dumps(raw), fetched_at),
            )
    return rows, {"hit": hit, "fetched_at": datetime.fromtimestamp(fetched_at, UTC).isoformat()}


def airport_traffic(query: TrafficQuery) -> dict:
    airport = airport_details(query.airport)
    if airport.get("country") != "US":
        raise AviationDataError("This BTS T-100 summary supports US origin airports")
    airport_code = AirportQuery(airport=airport.get("iataId") or query.airport).airport
    rows, cache = _load_months(airport_code, query.start_month, query.end_month)
    seats = sum(row.total_seats for row in rows)
    passengers = sum(row.total_passengers for row in rows)
    return {
        "airport": airport_code,
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "direction": query.direction,
        "service": SERVICE_SCOPE,
        "totals": {
            "performed_departures": sum(row.total_departures for row in rows),
            "available_seats": seats,
            "passengers": passengers,
            "passenger_load_factor_pct": round(passengers / seats * 100, 2) if seats else None,
        },
        "months_with_data": sorted(row.reporting_month.strftime("%Y-%m") for row in rows),
        "cache": cache,
        "source": {"name": "BTS AFF - T100 Segment Summary By Origin Airport", "url": BTS_T100_URL},
        "limitations": (
            "Domestic and outbound international commercial traffic, with no service-class filter. "
            "Departure counts include cargo operations. This summary provides no route breakdown, "
            "scheduled-flight counts, cancellations, or complete inbound totals. It measures served "
            "passengers and seats, not latent demand or physical airport capacity."
        ),
    }
