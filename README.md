# Airport Investment Agent

A minimal terminal chat using Python, uv, one LangChain agent, and OpenRouter.

## Run

```sh
uv sync --locked
cp .env.example .env
uv run --env-file .env python -m airport_agent.cli
```

Set `OPENROUTER_API_KEY` in `.env`. The default model is
`deepseek/deepseek-v4-flash-0731`.

The agent exposes these tools:

- `get_airport_details`: identifiers, coordinates, and runways from Aviation Weather Center.
- `get_aviation_weather`: latest METAR from Aviation Weather Center.
- `get_airport_status`: current events from the FAA NAS Status feed.
- `get_outbound_flights` and `get_inbound_flights`: observed OpenSky flights for an explicit UTC date range.
- `calculate_route_distance` and `calculate_percentage`: deterministic calculations.

All remote calls use `httpx`. Responses are handled in memory; the agent does
not download or maintain aviation datasets. OpenSky flight history requires an
OAuth2 client. Add both values to `.env`:

```text
OPENSKY_CLIENT_ID=
OPENSKY_CLIENT_SECRET=
```

OpenSky records are ADS-B observations, so counts can be incomplete and are not
scheduled-flight or passenger totals. Long haul is fixed at a route distance
over 3,000 statute miles. Flights with an unresolved airport remain visible as
unknown and are excluded from the long-haul subset.

Inspect small live responses without invoking the model:

```sh
uv run --env-file .env python scripts/inspect_apis.py
```

The inspector writes JSON evidence under `samples/`. It never records OpenSky
credentials or access tokens.

Run the offline checks:

```sh
uv run python -m unittest -v
```
