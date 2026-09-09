"""Thin httpx clients for the public aviation APIs used by the agent."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from math import asin, cos, radians, sin, sqrt
import os
import re
import xml.etree.ElementTree as ET
from typing import Literal

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

AWC_URL = "https://aviationweather.gov/api/data"
FAA_STATUS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
OPENSKY_URL = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
LONG_HAUL_MILES = 3_000
USER_AGENT = "AirportInvestmentAssessment/0.1"


class AviationDataError(RuntimeError):
    pass


class AirportQuery(BaseModel):
    airport: str = Field(description="Three-letter IATA or four-letter ICAO airport code")

    @field_validator("airport")
    @classmethod
    def normalize_airport(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,4}", value):
            raise ValueError("airport must be a three-letter IATA or four-letter ICAO code")
        return value


class FlightQuery(AirportQuery):
    start_date: date = Field(description="Inclusive UTC date in YYYY-MM-DD format")
    end_date: date = Field(description="Inclusive UTC date in YYYY-MM-DD format")
    haul: Literal["all", "long_haul", "not_long_haul"] = "all"
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_period(self) -> FlightQuery:
        days = (self.end_date - self.start_date).days + 1
        if days < 1:
            raise ValueError("end_date must be on or after start_date")
        if days > 7:
            raise ValueError("OpenSky queries are limited to seven days per tool call")
        return self


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _json(response: httpx.Response, source: str):
    try:
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        detail = response.text.strip()[:200] if response.is_error else "invalid JSON"
        raise AviationDataError(f"{source} request failed ({response.status_code}): {detail}") from exc


@lru_cache(maxsize=256)
def airport_details(code: str) -> dict:
    code = code.upper()
    ids = code if len(code) == 4 else ",".join((code, f"K{code}", f"P{code}"))
    with _client() as client:
        rows = _json(client.get(f"{AWC_URL}/airport", params={"ids": ids, "format": "json"}), "AWC")
    matches = [row for row in rows if code in {row.get("icaoId"), row.get("iataId"), row.get("faaId")}]
    if not matches:
        raise AviationDataError(f"No airport found for {code}")
    return matches[0]


def latest_weather(code: str) -> dict:
    airport = airport_details(code)
    with _client() as client:
        rows = _json(client.get(f"{AWC_URL}/metar", params={"ids": airport["icaoId"], "format": "json"}), "AWC")
    return {"airport": airport["icaoId"], "observation": rows[0] if rows else None, "source": f"{AWC_URL}/metar"}


def _xml_value(element: ET.Element):
    children = list(element)
    return {child.tag: _xml_value(child) for child in children} if children else (element.text or "").strip()


def airport_status(code: str) -> dict:
    iata = airport_details(code).get("iataId") or code[-3:]
    try:
        with _client() as client:
            response = client.get(FAA_STATUS_URL)
            response.raise_for_status()
        root = ET.fromstring(response.content)
    except (httpx.HTTPError, ET.ParseError) as exc:
        raise AviationDataError(f"FAA NAS status request failed: {exc}") from exc
    events = []
    for group in root.findall("Delay_type"):
        for event in group.findall("./*/*"):
            if event.findtext("ARPT") == iata:
                events.append({"type": group.findtext("Name", default=""), "details": _xml_value(event)})
    return {"airport": iata, "updated_at": root.findtext("Update_Time"), "active_events": events, "active_event_count": len(events), "source": FAA_STATUS_URL}


def _auth_headers(client: httpx.Client) -> dict[str, str]:
    client_id = os.getenv("OPENSKY_CLIENT_ID", "").strip()
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
    if not client_id and not client_secret:
        return {}
    if not client_id or not client_secret:
        raise AviationDataError("Set both OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET")
    data = _json(client.post(OPENSKY_TOKEN_URL, data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}), "OpenSky OAuth")
    return {"Authorization": f"Bearer {data['access_token']}"}


def route_distance_miles(origin: dict, destination: dict) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (origin["lat"], origin["lon"], destination["lat"], destination["lon"]))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return round(2 * 3958.7613 * asin(sqrt(a)), 1)


def get_flights(query: FlightQuery, direction: Literal["departure", "arrival"]) -> dict:
    airport = airport_details(query.airport)
    other_field = "estArrivalAirport" if direction == "departure" else "estDepartureAirport"
    endpoint = f"{OPENSKY_URL}/flights/{direction}"
    rows = []
    with _client() as client:
        headers = _auth_headers(client)
        day = query.start_date
        while day <= query.end_date:
            begin = int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())
            response = client.get(endpoint, params={"airport": airport["icaoId"], "begin": begin, "end": begin + 86_400}, headers=headers)
            rows.extend([] if response.status_code == 404 else _json(response, "OpenSky"))
            day += timedelta(days=1)

    flights = []
    unknown_other_airport = 0
    for row in rows:
        other_code = row.get(other_field)
        distance = None
        if other_code:
            try:
                distance = route_distance_miles(airport, airport_details(other_code))
            except AviationDataError:
                pass
        if distance is None:
            unknown_other_airport += 1
        haul = "long_haul" if distance is not None and distance > LONG_HAUL_MILES else "not_long_haul" if distance is not None else "unknown"
        if query.haul != "all" and haul != query.haul:
            continue
        flights.append({"icao24": row.get("icao24"), "callsign": (row.get("callsign") or "").strip() or None, "first_seen": row.get("firstSeen"), "last_seen": row.get("lastSeen"), "other_airport": other_code, "distance_miles": distance, "haul": haul})
    return {"airport": airport["icaoId"], "direction": direction, "period": {"start_date": str(query.start_date), "end_date": str(query.end_date)}, "haul_filter": query.haul, "long_haul_definition": f"distance > {LONG_HAUL_MILES:,} statute miles", "observed_flights": len(rows), "matching_flights": len(flights), "unknown_other_airport": unknown_other_airport, "flights": flights[:query.limit], "truncated": len(flights) > query.limit, "source": endpoint, "limitations": "ADS-B observations; not schedules, passenger totals, or complete traffic coverage"}
