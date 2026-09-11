"""Deterministic airport screens with explicit periods and separate evidence."""

from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field, model_validator

from airport_agent.aviation import AirportQuery, AviationDataError
from airport_agent.bts import SERVICE_SCOPE, TrafficQuery, airport_traffic
from airport_agent.performance import PerformanceQuery, airport_performance


def _months(start: str, end: str) -> list[str]:
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(sy * 12 + sm - 1, ey * 12 + em)]


def _prior(month: str) -> str:
    year, number = map(int, month.split("-"))
    return f"{year - 1:04d}-{number:02d}"


class ComparisonQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    airports: list[str] = Field(min_length=2, max_length=10)
    start_month: str = Field(description="Inclusive YYYY-MM; growth comparisons use the same months one year earlier")
    end_month: str = Field(description="Inclusive YYYY-MM; maximum 12 months")

    @model_validator(mode="after")
    def validate_query(self):
        self.airports = list(dict.fromkeys(AirportQuery(airport=a).airport for a in self.airports))
        if len(self.airports) < 2:
            raise ValueError("Provide at least two distinct airports")
        TrafficQuery(airport=self.airports[0], start_month=self.start_month, end_month=self.end_month)
        if len(_months(self.start_month, self.end_month)) > 12:
            raise ValueError("Compare at most 12 months at a time")
        return self


class OpportunityQuery(ComparisonQuery):
    airports: list[str] = Field(min_length=2, max_length=2)


def _growth(airport: str, start_month: str, end_month: str) -> tuple[dict, dict]:
    periods = []
    for start, end in [(start_month, end_month), (_prior(start_month), _prior(end_month))]:
        data = airport_traffic(TrafficQuery(airport=airport, start_month=start, end_month=end, direction="outbound"))
        missing = sorted(set(_months(start, end)) - set(data["months_with_data"]))
        if missing:
            raise AviationDataError(f"No monthly traffic summaries for {', '.join(missing)}; cannot establish comparable coverage")
        periods.append(data)
    current, previous = periods
    c, p = current["totals"], previous["totals"]
    if p["passengers"] <= 0 or p["available_seats"] <= 0 or c["available_seats"] <= 0:
        raise AviationDataError("Positive baseline passengers and seats, and current seats, are required")
    passenger_growth = (c["passengers"] / p["passengers"] - 1) * 100
    seat_growth = (c["available_seats"] / p["available_seats"] - 1) * 100
    return {
        "airport": current["airport"],
        "passenger_growth_pct": passenger_growth,
        "seat_growth_pct": seat_growth,
        "growth_gap_percentage_points": passenger_growth - seat_growth,
        "growth_gap_interpretation": (
            "Passenger growth exceeded seat growth" if passenger_growth > seat_growth else
            "Seat growth exceeded passenger growth" if seat_growth > passenger_growth else
            "Passenger and seat growth were equal"
        ),
        "seat_occupancy_pct": c["passengers"] / c["available_seats"] * 100,
        "passenger_change": c["passengers"] - p["passengers"],
        "current_totals": c,
        "previous_totals": p,
    }, current["source"]


def compare_growth(query: ComparisonQuery) -> dict:
    ranked, excluded, sources = [], [], []
    for airport in query.airports:
        try:
            row, source = _growth(airport, query.start_month, query.end_month)
            ranked.append(row)
            if source not in sources:
                sources.append(source)
        except AviationDataError as exc:
            excluded.append({"airport": airport, "reason": str(exc)})

    ranked.sort(key=lambda row: (-row["passenger_growth_pct"], -row["seat_occupancy_pct"], row["airport"]))
    # Use unrounded metrics; exact ties share a rank.
    last_key, rank = None, 0
    for index, row in enumerate(ranked, 1):
        key = (row["passenger_growth_pct"], row["seat_occupancy_pct"])
        if key != last_key:
            rank = index
        row["rank"], last_key = rank, key
        for field in ("passenger_growth_pct", "seat_growth_pct", "growth_gap_percentage_points", "seat_occupancy_pct"):
            row[field] = round(row[field], 2)
    return {
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "baseline": {"start_month": _prior(query.start_month), "end_month": _prior(query.end_month)},
        "direction": "outbound",
        "service": SERVICE_SCOPE,
        "ranking_rule": "Passenger growth descending, then current seat occupancy descending. Exact ties share a rank. No weighted score.",
        "ranking": ranked,
        "excluded": excluded,
        "sources": sources,
        "limitations": "Growth-opportunity screening heuristic, not measured unmet demand or investment feasibility. High occupancy does not establish an airport infrastructure bottleneck. Percentage growth can favor small airports; inspect passenger change and totals. Months with rows do not guarantee full reporting. Departures include cargo operations; service classes cannot be filtered.",
    }


def compare_performance(query: ComparisonQuery) -> dict:
    ranked, excluded = [], []
    for airport in query.airports:
        try:
            data = airport_performance(PerformanceQuery(
                airport=airport, start_month=query.start_month, end_month=query.end_month,
            ))
            if data["totals"]["reported_operations"] <= 0:
                raise AviationDataError("No reported operations for a comparison")
            ranked.append(data)
        except AviationDataError as exc:
            excluded.append({"airport": airport, "reason": str(exc)})

    def key(row):
        totals = row["totals"]
        return (Fraction(totals["delayed_arrivals"], totals["reported_operations"]),
                Fraction(totals["cancellations"], totals["reported_operations"]))

    ranked.sort(key=lambda row: (*(-value for value in key(row)), row["airport"]))
    last_key, rank = None, 0
    for index, row in enumerate(ranked, 1):
        if key(row) != last_key:
            rank = index
        row["rank"], last_key = rank, key(row)
    return {
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "ranking_rule": "More disrupted first: domestic arrival 15+ minute delay rate descending, then cancellation rate descending. Exact ties share a rank; calculated from counts before rounding.",
        "ranking": ranked,
        "excluded": excluded,
        "limitations": "Domestic arrival reliability is a congestion proxy, not physical airport utilization. Same reporting scope and period; aggregate reports do not verify every month's completeness. Delay attribution cannot establish an infrastructure bottleneck.",
    }


def compare_opportunity(query: OpportunityQuery) -> dict:
    """Compare two airports using three visible, equally weighted dimensions."""
    rows, excluded, sources = {}, [], []
    for airport in query.airports:
        try:
            growth, traffic_source = _growth(airport, query.start_month, query.end_month)
            performance = airport_performance(PerformanceQuery(
                airport=airport, start_month=query.start_month, end_month=query.end_month,
            ))
            if performance["totals"]["reported_operations"] <= 0:
                raise AviationDataError("No reported domestic arrival operations")
            rows[airport] = {"growth": growth, "performance": performance}
            for source in (traffic_source, performance["source"]):
                if source not in sources:
                    sources.append(source)
        except AviationDataError as exc:
            excluded.append({"airport": airport, "reason": str(exc)})

    limitations = (
        "This is an equal-weight modernization opportunity screen, not a validated "
        "investment model or forecast. Seat pressure measures airline supply, not "
        "physical airport capacity. Delay evidence covers reported domestic arrivals "
        "and does not prove an infrastructure constraint. Profitability, project cost, "
        "feasibility, and latent demand are not measured."
    )
    if len(rows) != 2:
        return {
            "period": {"start_month": query.start_month, "end_month": query.end_month},
            "status": "insufficient_evidence", "winner": None,
            "excluded": excluded, "sources": sources, "limitations": limitations,
        }

    a, b = query.airports

    def choose(a_value, b_value):
        return None if a_value == b_value else (a if a_value > b_value else b)

    growth_winner = choose(rows[a]["growth"]["passenger_growth_pct"],
                           rows[b]["growth"]["passenger_growth_pct"])
    def supply_pressure(airport):
        growth = rows[airport]["growth"]
        if growth["passenger_growth_pct"] <= 0 or growth["growth_gap_percentage_points"] <= 0:
            return None
        return growth["growth_gap_percentage_points"], growth["seat_occupancy_pct"]

    a_pressure, b_pressure = supply_pressure(a), supply_pressure(b)
    supply_winner = (None if a_pressure is None and b_pressure is None else
                     a if b_pressure is None else b if a_pressure is None else
                     choose(a_pressure, b_pressure))

    def disruption(airport):
        totals = rows[airport]["performance"]["totals"]
        return (Fraction(totals["delayed_arrivals"], totals["reported_operations"]),
                Fraction(totals["cancellations"], totals["reported_operations"]))

    operations_winner = choose(disruption(a), disruption(b))
    dimensions = [
        {"dimension": "growth_momentum", "winner": growth_winner,
         "rule": "Higher outbound passenger growth wins."},
        {"dimension": "supply_pressure", "winner": supply_winner,
         "rule": "Requires positive passenger growth and a positive passenger-minus-seat growth gap. Higher gap wins; occupancy breaks a tie."},
        {"dimension": "operational_pressure", "winner": operations_winner,
         "rule": "Higher domestic arrival delay rate wins; cancellation rate breaks a tie."},
    ]
    wins = {airport: sum(d["winner"] == airport for d in dimensions) for airport in query.airports}
    winner = next((airport for airport, count in wins.items() if count >= 2), None)
    evidence = {}
    for airport, row in rows.items():
        growth, performance = row["growth"], row["performance"]
        evidence[airport] = {
            "passenger_growth_pct": round(growth["passenger_growth_pct"], 2),
            "passenger_change": growth["passenger_change"],
            "seat_growth_pct": round(growth["seat_growth_pct"], 2),
            "growth_gap_percentage_points": round(growth["growth_gap_percentage_points"], 2),
            "seat_occupancy_pct": round(growth["seat_occupancy_pct"], 2),
            "reported_domestic_arrival_operations": performance["totals"]["reported_operations"],
            "delayed_15min_pct": performance["metrics"]["delayed_15min_pct"],
            "cancellation_pct": performance["metrics"]["cancellation_pct"],
        }
    return {
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "baseline": {"start_month": _prior(query.start_month), "end_month": _prior(query.end_month)},
        "status": "ranked" if winner else "mixed_evidence", "winner": winner,
        "dimension_wins": wins,
        "decision_rule": "Each dimension has one vote; an airport needs at least two wins. Ties cast no vote.",
        "dimensions": dimensions, "evidence": evidence, "excluded": excluded,
        "sources": sources, "limitations": limitations,
    }


def demand_pressure(query: PerformanceQuery) -> dict:
    growth, source = _growth(query.airport, query.start_month, query.end_month)
    growing_faster_than_seats = (growth["passenger_growth_pct"] > 0
                                 and growth["growth_gap_percentage_points"] > 0)
    for field in ("passenger_growth_pct", "seat_growth_pct", "growth_gap_percentage_points", "seat_occupancy_pct"):
        growth[field] = round(growth[field], 2)
    performance, missing_evidence = None, []
    try:
        performance = airport_performance(query)
    except AviationDataError as exc:
        missing_evidence.append({"evidence": "domestic arrival performance", "reason": str(exc)})
    return {
        "airport": growth["airport"],
        "period": {"start_month": query.start_month, "end_month": query.end_month},
        "baseline": {"start_month": _prior(query.start_month), "end_month": _prior(query.end_month)},
        "proxy": "Observed passenger growth relative to seat growth; occupancy and domestic delays are separate supporting evidence.",
        "signal_rule": "Growing traffic outpaces seat supply when passenger growth > 0 and passenger growth minus seat growth > 0 percentage points, compared with the same months one year earlier.",
        "growing_traffic_outpaces_seats": growing_faster_than_seats,
        "traffic": growth,
        "traffic_service": SERVICE_SCOPE,
        "traffic_source": source,
        "domestic_arrival_performance": performance,
        "missing_evidence": missing_evidence,
        "limitations": "This proxy describes observed traffic pressure, not measured unmet demand or a count of unserved passengers/flights. A false signal does not prove absence of unmet demand. Seat growth is airline supply, not runway or terminal capacity. Separate domestic delay evidence can describe reported disruption, but cannot establish why latent demand exists or whether expansion would relieve it. The populations have different denominators and are not multiplied into a score.",
    }
