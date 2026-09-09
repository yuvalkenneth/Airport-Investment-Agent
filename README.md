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
- `get_airport_traffic`: cached monthly BTS T-100 passenger, capacity, load-factor, and route totals.
- `calculate_route_distance` and `calculate_percentage`: deterministic calculations.

FAA and Aviation Weather Center calls use `httpx`; OpenSky calls use its official
Python SDK. Responses are handled in memory, so the agent does not maintain an
aviation dataset. OpenSky flight history requires an OAuth2 client. Add both
values to `.env`:

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

T-100 requests fetch one state-year at a time. Compressed responses are cached
in `.cache/t100.sqlite3`, which is excluded from Git, and reused by later queries.
