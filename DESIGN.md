# Airport Investment Agent — design

## Purpose and scope

A conversational prototype for screening US airport modernization opportunities.
It ranks observed growth and disruption, then explains the evidence. It does not
measure latent demand, prove an infrastructure bottleneck, or calculate investment
returns. The assignment's four example questions are supported within the source
coverage below; missing dates or ambiguous airports trigger clarification.

All aviation sources are free. OpenRouter model usage is separate and needs an
API key; LangSmith tracing is optional. No bulk aviation dataset is downloaded.

## Architecture and use of AI

Terminal input → one LangChain agent using OpenRouter → Pydantic-validated tools
→ source adapters → deterministic Python calculations → conversational answer.

The LLM chooses relevant tools, resolves follow-ups, asks clarifying questions,
and explains results. Python owns counts, percentages, proxy signals, and ranks.
Final answers cite the data source and period without exposing internal tool
workflow. The development terminal displays tool inputs/results for inspection;
LangSmith can trace the same run. Conversation history stays in the session.

`aviation.py`, `bts.py`, and `performance.py` adapt sources; `kpis.py` contains
scoring; `tools.py` exposes functions. Existing modules are reused, with no
research subagents, vector database, or additional orchestration layer.

## Source coverage

| Source | Used for | Important boundary |
| --- | --- | --- |
| [BTS T-100 airport summary API](https://data.bts.gov/resource/r495-tyji.json) | Monthly outbound passengers, seats, departures; growth and occupancy | Domestic and outbound international commercial traffic, all reported service classes. Cargo departures included. No route detail or scheduled-service filter. |
| [BTS On-Time public report](https://www.transtats.bts.gov/OT_Delay/OT_DelayCause1.asp) | Historical arrival reliability and reported delay causes | Domestic arrivals from reporting carriers. Filtered HTML report parsed into JSON, not a JSON API. No international delay coverage or taxi times. |
| [OpenSky](https://openskynetwork.github.io/opensky-api/) | Observed arrivals/departures and inferred route distances | OAuth2 through the official Python SDK. ADS-B coverage is incomplete; unknown destinations remain visible. No scheduled-flight or passenger totals. |
| [Aviation Weather Center](https://aviationweather.gov/data/api/) | Airport identification, coordinates, runways, current METAR | Airport facts do not establish usable runway/gate throughput. Current weather cannot explain a historical trend. |
| [FAA NAS status](https://nasstatus.faa.gov/) | Current disruption context | Current reported events, not historical airport-wide delay rates. |

`httpx` handles BTS, AWC, and FAA requests. BTS requests select an airport and
period; validated results are cached in SQLite for 24 hours. Errors are not cached
as zero. Airport metadata is cached in memory. OpenSky samples are requested per
UTC day, at most seven days per call; their counts are computed before display
limits. Credentials and cache files are excluded from Git.

## Deterministic rules

**Growth screen:** compare a requested period of up to 12 months with the same
months one year earlier. Rank passenger growth descending, then current seat
occupancy descending. Exact ties share a rank. Also show absolute passenger change
and the underlying volumes so a small base is visible.

- Passenger growth = `(current passengers / prior passengers − 1) × 100`.
- Seat growth = `(current seats / prior seats − 1) × 100`.
- Growth gap = passenger growth minus seat growth, in percentage points.
- Seat occupancy = `current passengers / current seats × 100`.

Missing monthly rows or nonpositive required baselines exclude an airport from
ranking. Monthly rows do not guarantee complete carrier reporting. A default
New England demo shortlist is BOS, BDL, PVD, MHT, and BGR; it is not an exhaustive
regional airport inventory or a peer-normalized investment score.

**Congestion comparison:** rank domestic arrival delay rate descending, then
cancellation rate descending. First means more disrupted. Both rates divide by
all reported operations, including cancellations and diversions. Delay means
arrival at least 15 minutes late. Rank from exact count ratios before rounding;
exact ties share a rank. Average delay covers delayed arrivals only. This is a
reliability proxy for congestion, not physical utilization.

**Demand-pressure proxy:** a positive signal requires both passenger growth > 0
and growth gap > 0. This asks whether growing served traffic outpaces airline seat
supply. Occupancy and domestic delays remain separate supporting evidence; their
different populations are never multiplied into a composite. A negative signal
does not establish absence of latent demand. No unserved-flight count is produced.
Reported delay categories explain observed disruption, not the causes of unmet
demand. Late-aircraft delay can propagate several causes, and weather can also
appear within NAS delays. Neither category alone proves a runway bottleneck.

**Long-haul share:** confirmed observed outbound flights with inferred route
distance > 3,000 statute miles, divided by all observed outbound flights for the
same airport and UTC dates. Unknown destinations remain in the denominator;
this is a lower bound within the observed sample. Counts must agree across the
filtered and unfiltered requests. It is not an estimate of every scheduled flight.

These rules are intentionally transparent. No arbitrary weighted investment
score or hub-class z-score is presented without a defensible peer dataset.
Expansion feasibility would additionally need peak-hour throughput, gate/runway
constraints, local planning context, costs, and revenue evidence.

## Four explicit demo questions

1. Among BOS, BDL, PVD, MHT, and BGR, which should be investigated first for
   terminal expansion using outbound passenger growth in January 2025 versus
   January 2024, with occupancy as the tiebreaker? Show absolute growth too.
2. Compare LAX and SNA congestion in January 2025 using BTS domestic arrival
   delay rate, with cancellation rate as the tiebreaker. Explain the main
   reported delay causes and what this says about expansion.
3. What percentage of observed outbound flights from ANC on September 7, 2026
   UTC were confirmed long-haul, defined as route distance over 3,000 statute
   miles? Include unknown destinations in the denominator and state coverage.
4. Assess unmet demand at SFO using a demand-pressure proxy: passenger growth
   versus seat growth in January 2025 compared with January 2024. Show occupancy
   and January 2025 domestic arrival delays as separate evidence. What can we
   conclude, and what explains the reported disruption?

These are explicit demonstration periods, not defaults for undated questions.
For live OpenSky testing, choose a completed recent UTC day within the account's
available history. Also try an original undated question to check clarification,
then a follow-up such as “What does that imply for terminal expansion?”

## Validation

Run `uv run python -m unittest discover` for offline checks of source parsing,
coverage rejection, caching, rank ties, pressure rules, OpenSky failure handling,
and the conversational tool loop. Live examples need separate verification:
successful offline tests do not establish current source access or LLM behavior.


### Prototype verification, September 10, 2026

All 19 offline checks passed. The [saved live examples](samples/assessment_smoke.json)
include tool arguments, source results, and the raw final answers for the four
questions, plus SFO clarification and its follow-up. All four data flows returned
evidence using the configured `deepseek/deepseek-v4-flash-0731` model.

The narrative check is **not fully passing**. Despite prompt corrections, DeepSeek
still added unsupported cargo claims for ANC and incorrectly called Air Carrier
Delay the largest SNA cause (the returned data show Aircraft Arriving Late is
larger). Some prose also includes unrequested arithmetic or overinterprets causes.
These failures are recorded in the sample's manual review; correct deterministic
metrics should not be mistaken for fully reliable generated investment explanations.


The default was subsequently changed to `openai/gpt-5.6-terra` through OpenRouter.
In [two targeted rechecks](samples/terra_recheck.json), Terra correctly identified
SNA's leading cause and omitted unsupported cargo claims for ANC. The prompt and
tools were unchanged. These two errors did not recur; the broader narrative
behavior has not been exhaustively evaluated. All 19 offline checks still pass.
