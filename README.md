# Airport Investment Agent

A minimal terminal chat using Python, uv, one LangChain agent, and OpenRouter.

See [DESIGN.md](DESIGN.md) for the scoring rules, source tradeoffs, and four
assignment demo questions.

## Run

```sh
uv sync --locked
cp .env.example .env
uv run --env-file .env python -m airport_agent.cli
```

Set `OPENROUTER_API_KEY` in `.env`. The default model is
`openai/gpt-5.6-terra`.

The agent exposes these tools:

- `get_airport_details`: identifiers, coordinates, and runways from Aviation Weather Center.
- `get_aviation_weather`: latest METAR from Aviation Weather Center.
- `get_airport_status`: current events from the FAA NAS Status feed.
- `get_outbound_flights` and `get_inbound_flights`: observed OpenSky flights for an explicit UTC date range.
- `get_airport_traffic`: cached monthly outbound BTS T-100 commercial passenger, seat, departure, and load-factor totals.
- `get_airport_performance`: cached historical BTS domestic arrival delays, cancellations, and delay causes.
- `compare_airport_performance`: ranks domestic arrival disruption by delay rate, with cancellations as the tiebreaker.
- `compare_airport_opportunity`: compares two airports through visible growth, supply-pressure, and operational-pressure votes.
- `get_airport_demand_pressure`: passenger growth relative to seat growth, with occupancy and domestic delays as supporting evidence.
- `compare_airport_growth`: comparable outbound passenger growth and occupancy rankings, with underlying totals and coverage exclusions.
- `calculate_percentage`: deterministic percentage calculation.

FAA, Aviation Weather Center, and BTS calls use `httpx`; OpenSky calls use its
official Python SDK. OpenSky flight history requires an OAuth2 client. Add both
values to `.env`:

```text
OPENSKY_CLIENT_ID=
OPENSKY_CLIENT_SECRET=
```

OpenSky records are ADS-B observations, so counts can be incomplete and are not
scheduled-flight or passenger totals. Long haul is fixed at a route distance
over 3,000 statute miles. Flights with an unresolved airport remain visible as
unknown and are excluded from the long-haul subset.

T-100 traffic comes from the public [BTS airport summary API](https://data.bts.gov/resource/r495-tyji.json),
filtered to one airport and the requested months. No bulk downloads or API key
are needed. JSON responses are cached for 24 hours in `.cache/t100.sqlite3`
(excluded from Git); old ZIP cache entries are no longer used.

The summary covers domestic and outbound international traffic across all reported
commercial service classes, including nonscheduled services and cargo departures.
It does not support a scheduled-passenger-only filter, routes, cancellation
counts, or complete inbound totals. Long-haul analysis continues to use OpenSky.

## Historical arrival performance

Try: "What were SFO's arrival delay rate, cancellation rate, and main delay
causes in January 2025?"

`get_airport_performance` accepts `airport`, `start_month`, and `end_month`
(`YYYY-MM`, inclusive, up to 12 months). It fetches the filtered public
[BTS arrival report](https://www.transtats.bts.gov/OT_Delay/OT_DelayCause1.asp)
with `httpx`, parses the HTML in Python, and returns structured JSON to the
agent. This source is an HTML report, not a JSON API. No credentials or bulk
downloads are needed; validated results share the 24-hour SQLite cache.

The response includes counts, delay and cancellation rates, average delay among
arrivals delayed 15+ minutes, and delay minutes by reported cause. Operation
percentages use all reported operations, including cancellations and diversions.
Cause counts are rounded and prorated, so delayed arrivals are calculated as
total minus on-time, cancelled, and diverted operations instead of summing causes.

Coverage is domestic arrivals from reporting carriers, separate from T-100's
commercial traffic population. Period totals do not establish complete reporting
for every month. This report does not supply taxi times or physical capacity;
NAS delay alone is not evidence of a runway bottleneck. Unavailable periods or
unrecognized reports return an error rather than zero delays.

## Tracing

Set `LANGSMITH_API_KEY` in `.env`. With `LANGSMITH_TRACING=true`, LangChain
automatically sends agent runs, model calls, tool inputs/outputs, errors, and
timings to the `airport-investment-agent` project in LangSmith. The development
terminal also prints tool inputs and results; the final answer presents business
findings without tool names or internal workflow. Restart after changing `.env`;
the VS Code launch configuration also loads this file.

The default endpoint is `https://api.smith.langchain.com`. For an EU workspace,
use `https://eu.api.smith.langchain.com`. Set `LANGSMITH_TRACING=false` to disable
tracing. Traces include conversation and tool data.

## Growth opportunity screen

Try: "Compare SFO, LAX, and SNA for January–December 2025 against the same
months in 2024 using the growth opportunity screen. Explain the ranking and
limitations."

The tool accepts `airports`, `start_month`, and `end_month` (up to 12 months).
It compares outbound commercial traffic with the same months one year
earlier, ranking passenger growth first and current seat occupancy second.
Exact ties share a rank. There is no weighted investment score.

- Passenger growth: `(current passengers / prior passengers - 1) * 100`.
- Seat growth: `(current available seats / prior available seats - 1) * 100`.
- Growth gap: passenger growth minus seat growth, in percentage points.
- Seat occupancy: `current passengers / current available seats * 100`.

Results include absolute passenger change, both periods' totals, service coverage,
sources, and excluded airports with reasons. Missing months and zero
baselines are excluded from ranking. Presence of monthly rows does not establish
complete reporting. This is an initial traffic growth screen: it does not measure
latent demand, establish infrastructure constraints, or prove profitability.

Offline checks: `uv run python -m unittest discover`.

## Evaluation suite

The [evaluation suite](evals/README.md) contains 24 tool-grounded cases, including
the four assignment questions, clarified variants, KPI screens, source-boundary
tests, and a conversational follow-up. It generates fresh reference evidence from
the deterministic tools and uses a separate OpenRouter call to judge the agent's
answer and routing.

## Two-airport opportunity screen

Try: "Compare SFO and SNA for modernization and growth opportunity from January
through December 2025 versus the same months in 2024. Which ranks higher?"

The screen gives one vote each for outbound passenger growth, constrained airline
seat supply, and domestic arrival disruption. An airport needs two votes to lead;
tied dimensions cast no vote. Supply pressure requires both positive passenger
growth and passenger growth above seat growth, so a merely less-negative gap does
not win that dimension. The output keeps every component visible and does not
claim to forecast returns or measure physical airport capacity.
