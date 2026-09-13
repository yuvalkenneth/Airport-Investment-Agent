"""Small, deterministic tools exposed to the agent."""

from math import isfinite

from langchain.tools import ToolException, tool

from airport_agent.aviation import AirportQuery, AviationDataError, FlightQuery, airport_details, airport_status, get_flights, latest_weather
from airport_agent.bts import TrafficQuery, airport_traffic
from airport_agent.kpis import ComparisonQuery, OpportunityQuery, compare_growth, compare_opportunity, compare_performance, demand_pressure
from airport_agent.performance import PerformanceQuery, airport_performance


@tool
def calculate_percentage(part: float, total: float) -> dict:
    """Calculate part / total * 100 from finite, non-negative values."""
    if not (isfinite(part) and isfinite(total) and 0 <= part <= total and total > 0):
        raise ToolException("Use finite numbers with total > 0 and 0 <= part <= total.")
    return {"part": part, "total": total, "percentage": round(part / total * 100, 4)}


@tool(args_schema=AirportQuery)
def get_airport_details(airport: str) -> dict:
    """Get airport identifiers, coordinates, runways, and basic facilities from AWC. Report returned fields only; runway identifiers, headings, and dimensions do not establish spacing, operating roles, aircraft compatibility, or capacity."""
    try:
        return airport_details(airport)
    except AviationDataError as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=AirportQuery)
def get_aviation_weather(airport: str) -> dict:
    """Get the latest METAR observation for an airport from AWC."""
    try:
        return latest_weather(airport)
    except AviationDataError as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=AirportQuery)
def get_airport_status(airport: str) -> dict:
    """Get current FAA NAS delay, ground-stop, and closure events for a US airport."""
    try:
        return airport_status(airport)
    except AviationDataError as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=FlightQuery)
def get_outbound_flights(**kwargs) -> dict:
    """Get observed OpenSky departures, optionally filtered by the fixed long-haul definition."""
    try:
        return get_flights(FlightQuery(**kwargs), "departure")
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=FlightQuery)
def get_inbound_flights(**kwargs) -> dict:
    """Get observed OpenSky arrivals, optionally filtered by the fixed long-haul definition."""
    try:
        return get_flights(FlightQuery(**kwargs), "arrival")
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=TrafficQuery)
def get_airport_traffic(**kwargs) -> dict:
    """Get monthly outbound BTS commercial passenger, seat, departure, and load-factor totals. Includes all service classes; no routes or cancellation counts."""
    try:
        return airport_traffic(TrafficQuery(**kwargs))
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=PerformanceQuery)
def get_airport_performance(**kwargs) -> dict:
    """Get historical BTS domestic arrival delay rates, cancellations, and delay causes for an airport and inclusive month range (up to 12 months). Rates use all reported operations. Average delay covers only arrivals delayed 15+ minutes. No taxi times or physical capacity measures."""
    try:
        return airport_performance(PerformanceQuery(**kwargs))
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=ComparisonQuery)
def compare_airport_growth(**kwargs) -> dict:
    """Return the complete named growth screen for 2-10 US airports: outbound passenger growth versus the same months last year, then occupancy, with seats, totals, and exclusions. Do not add performance or demand-pressure data unless the user explicitly asks for those dimensions."""
    try:
        return compare_growth(ComparisonQuery(**kwargs))
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=ComparisonQuery)
def compare_airport_performance(**kwargs) -> dict:
    """Compare US airport congestion using domestic arrival delay rate, then cancellations, for the same explicit months. More disrupted ranks first. Includes counts, reported causes and coverage; not a measure of physical capacity."""
    try:
        return compare_performance(ComparisonQuery(**kwargs))
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=OpportunityQuery)
def compare_airport_opportunity(**kwargs) -> dict:
    """Return the complete named opportunity screen for exactly two US airports: growth momentum, airline supply pressure, and domestic arrival disruption. Each visible dimension gets one vote; two wins produce the leader. Add infrastructure or other context only when explicitly requested. This is not an investment return or physical-capacity model."""
    try:
        return compare_opportunity(OpportunityQuery(**kwargs))
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


@tool(args_schema=PerformanceQuery)
def get_airport_demand_pressure(**kwargs) -> dict:
    """Return the complete demand-pressure proxy for one airport: passenger growth versus seat growth against the same months last year, occupancy, and the full domestic arrival performance report when available. Do not request that performance separately for the same airport and period. This is not an unserved-flight count or proof of cause."""
    try:
        return demand_pressure(PerformanceQuery(**kwargs))
    except (AviationDataError, ValueError) as exc:
        raise ToolException(str(exc)) from exc


for api_tool in (get_airport_details, get_aviation_weather, get_airport_status, get_outbound_flights, get_inbound_flights, get_airport_traffic, get_airport_performance, compare_airport_performance, compare_airport_opportunity, get_airport_demand_pressure, compare_airport_growth):
    api_tool.handle_tool_error = True
calculate_percentage.handle_tool_error = True

TOOLS = [get_airport_details, get_aviation_weather, get_airport_status, get_outbound_flights, get_inbound_flights, get_airport_traffic, get_airport_performance, compare_airport_performance, compare_airport_opportunity, get_airport_demand_pressure, compare_airport_growth, calculate_percentage]
