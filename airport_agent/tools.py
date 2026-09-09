"""Small, deterministic tools exposed to the agent."""

from math import isfinite

from langchain.tools import ToolException, tool

from airport_agent.aviation import AirportQuery, AviationDataError, FlightQuery, airport_details, airport_status, get_flights, latest_weather, route_distance_miles


@tool
def calculate_percentage(part: float, total: float) -> dict:
    """Calculate part / total * 100 from finite, non-negative values."""
    if not (isfinite(part) and isfinite(total) and 0 <= part <= total and total > 0):
        raise ToolException("Use finite numbers with total > 0 and 0 <= part <= total.")
    return {"part": part, "total": total, "percentage": round(part / total * 100, 4)}


@tool(args_schema=AirportQuery)
def get_airport_details(airport: str) -> dict:
    """Get airport identifiers, coordinates, runways, and basic facilities from AWC."""
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


@tool
def calculate_route_distance(origin_lat: float, origin_lon: float, destination_lat: float, destination_lon: float) -> dict:
    """Calculate great-circle distance in statute miles between two coordinates."""
    distance = route_distance_miles({"lat": origin_lat, "lon": origin_lon}, {"lat": destination_lat, "lon": destination_lon})
    return {"distance_miles": distance, "long_haul": distance > 3_000}


for api_tool in (get_airport_details, get_aviation_weather, get_airport_status, get_outbound_flights, get_inbound_flights):
    api_tool.handle_tool_error = True
calculate_percentage.handle_tool_error = True

TOOLS = [get_airport_details, get_aviation_weather, get_airport_status, get_outbound_flights, get_inbound_flights, calculate_route_distance, calculate_percentage]
