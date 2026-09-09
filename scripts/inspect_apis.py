"""Fetch small API responses with httpx; no model calls or file downloads."""

import argparse
from datetime import UTC, date, datetime, time, timedelta
import json
import os
from pathlib import Path

import httpx

FAA_STATUS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"


def fetch(client, url, headers=None, params=None):
    result = {"url": url, "params": params or {}, "fetched_at": datetime.now(UTC).isoformat()}
    try:
        response = client.get(url, headers=headers, params=params)
        result["http_status"] = response.status_code
        result["content_type"] = response.headers.get("content-type")
        try:
            result["body"] = response.json()
        except ValueError:
            result["body"] = response.text
    except httpx.RequestError as error:
        result.update(http_status=None, error=str(error))
    return result


def opensky_headers(client):
    client_id = os.getenv("OPENSKY_CLIENT_ID", "").strip()
    client_secret = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()
    if not client_id and not client_secret:
        return {}
    if not client_id or not client_secret:
        raise SystemExit("Set both OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET, or neither.")
    try:
        response = client.post(OPENSKY_TOKEN_URL, data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
        response.raise_for_status()
        return {"Authorization": f"Bearer {response.json()['access_token']}"}
    except (httpx.HTTPError, KeyError, ValueError):
        raise SystemExit("OpenSky token request failed; check credentials and connectivity.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(UTC).date() - timedelta(days=2))
    args = parser.parse_args()
    begin = int(datetime.combine(args.date, time.min, tzinfo=UTC).timestamp())
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "AirportInvestmentAssessment/0.1"}) as client:
        auth = opensky_headers(client)
        probes = {
            "airport": fetch(client, "https://aviationweather.gov/api/data/airport", params={"ids": "PANC", "format": "json"}),
            "weather": fetch(client, "https://aviationweather.gov/api/data/metar", params={"ids": "PANC", "format": "json"}),
            "faa_status": fetch(client, FAA_STATUS_URL),
            "departures": fetch(client, "https://opensky-network.org/api/flights/departure", auth, {"airport": "PANC", "begin": begin, "end": begin + 86_400}),
        }
    output = Path(__file__).resolve().parents[1] / "samples" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True)
    for name, result in probes.items():
        (output / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        body = result.get("body")
        count = len(body) if isinstance(body, list) else None
        print(f"{name}: HTTP {result['http_status']}, records={count}")
    print(f"Saved responses: {output}")


if __name__ == "__main__":
    main()
