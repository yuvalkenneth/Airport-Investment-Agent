"""Offline traffic and growth checks with synthetic BTS JSON responses."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from airport_agent.bts import BTS_T100_URL, CACHE_TTL_SECONDS, TrafficQuery, airport_traffic
from airport_agent.aviation import AviationDataError
from airport_agent.tools import compare_airport_growth, compare_airport_opportunity, compare_airport_performance, get_airport_demand_pressure


def monthly(airport="SFO", month="2025-01", passengers=80, seats=100, departures=1):
    return dict(origin_airport_code=airport, reporting_month=f"{month}-01T00:00:00.000",
                total_departures=str(departures), total_passengers=str(passengers), total_seats=str(seats))


class BTSChecks(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("airport_agent.bts.CACHE_PATH", Path(directory) / "t100.sqlite3"))
        self.enterContext(patch("airport_agent.bts.airport_details", side_effect=lambda code: {
            "iataId": code[-3:], "country": "US",
        }))
        self.requests = []
        self.rows = [monthly()]
        self.response = lambda request: httpx.Response(200, json=self.rows)

        def handle(request):
            self.requests.append(request)
            return self.response(request)

        self.enterContext(patch("airport_agent.bts._client", side_effect=lambda: httpx.Client(
            transport=httpx.MockTransport(handle),
        )))

    def traffic(self, **kwargs):
        return airport_traffic(TrafficQuery(**(dict(
            airport="SFO", start_month="2025-01", end_month="2025-01",
        ) | kwargs)))

    def test_weighted_occupancy_and_narrow_request(self):
        self.rows = [monthly(passengers=10, seats=20, departures=3),
                     monthly(month="2025-02", passengers=90, seats=100, departures=7)]
        result = self.traffic(airport="KSFO", end_month="2025-02")
        self.assertEqual(result["airport"], "SFO")
        self.assertEqual(result["totals"], dict(performed_departures=10, available_seats=120,
                                             passengers=100, passenger_load_factor_pct=83.33))
        self.assertEqual(result["months_with_data"], ["2025-01", "2025-02"])
        self.assertIn("cargo", result["service"])
        self.assertNotIn("top_routes_by_passengers", result)
        request = self.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url).split("?")[0], BTS_T100_URL)
        self.assertEqual(request.url.params["$where"], "origin_airport_code = 'SFO' AND "
                         "reporting_month >= '2025-01-01T00:00:00' AND "
                         "reporting_month <= '2025-02-01T00:00:00'")

    def test_cache_reuse_and_expiry(self):
        with patch("airport_agent.bts.time.time", return_value=1_000_000):
            first = self.traffic()
            second = self.traffic()
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(len(self.requests), 1)
        self.rows = [monthly(passengers=90)]
        with patch("airport_agent.bts.time.time", return_value=1_000_000 + CACHE_TTL_SECONDS):
            refreshed = self.traffic()
        self.assertFalse(refreshed["cache"]["hit"])
        self.assertEqual(refreshed["totals"]["passengers"], 90)
        self.assertEqual(len(self.requests), 2)

    def test_bad_responses_are_not_cached(self):
        missing_seats = monthly()
        del missing_seats["total_seats"]
        for payload in ({"error": "unexpected"}, [missing_seats], [monthly(passengers=-1)],
                        [monthly(), monthly()], [monthly(airport="LAX")], [monthly(month="2024-01")]):
            with self.subTest(payload=payload):
                self.rows = payload
                with self.assertRaises(AviationDataError):
                    self.traffic()
        for response in (httpx.Response(429, text="Rate limit"), httpx.Response(200, text="invalid JSON")):
            self.response = lambda request, response=response: response
            with self.assertRaises(AviationDataError):
                self.traffic()
        self.response = lambda request: httpx.Response(200, json=[monthly()])
        result = self.traffic()
        self.assertFalse(result["cache"]["hit"])
        self.assertEqual(result["totals"]["passengers"], 80)

    def test_unsupported_queries_are_rejected(self):
        for kwargs in (dict(direction="inbound"), dict(direction="both"), dict(route_limit=5),
                       dict(start_month="2025-1"), dict(start_month="2025-13"),
                       dict(start_month="2025-02"), dict(end_month="2030-01")):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.traffic(**kwargs)
        self.assertEqual(self.requests, [])

    def test_ranking_calculations_and_coverage(self):
        def response(request):
            where = request.url.params["$where"]
            airport = where.split("'")[1]
            year = 2024 if "2024-01" in where else 2025
            # SFO and LAX share growth; LAX wins on occupancy. SNA declines.
            values = ({"SFO": (80, 100), "LAX": (80, 100), "SNA": (80, 100), "OAK": (0, 100)}
                      if year == 2024 else
                      {"SFO": (88, 110), "LAX": (88, 100), "SNA": (72, 100), "OAK": (10, 100)})
            rows = [monthly(airport, f"{year}-01", *values[airport])] if airport in values else []
            return httpx.Response(200, json=rows)

        self.response = response
        result = compare_airport_growth.invoke(dict(airports=["SFO", "LAX", "SNA", "OAK", "BUR"], start_month="2025-01", end_month="2025-01"))
        rows = result["ranking"]
        self.assertEqual([r["airport"] for r in rows], ["LAX", "SFO", "SNA"])
        self.assertEqual(rows[0]["passenger_growth_pct"], 10)
        self.assertEqual(rows[0]["seat_occupancy_pct"], 88)
        self.assertEqual(rows[0]["growth_gap_percentage_points"], 10)
        self.assertEqual(rows[0]["growth_gap_interpretation"], "Passenger growth exceeded seat growth")
        self.assertEqual(rows[1]["growth_gap_percentage_points"], 0)
        self.assertEqual(rows[1]["growth_gap_interpretation"], "Passenger and seat growth were equal")
        self.assertEqual(rows[2]["passenger_growth_pct"], -10)
        self.assertEqual(rows[2]["growth_gap_interpretation"], "Seat growth exceeded passenger growth")
        self.assertEqual({r["airport"] for r in result["excluded"]}, {"OAK", "BUR"})
        self.assertIn("cargo", result["service"])
        partial = compare_airport_growth.invoke(dict(airports=["SFO", "LAX"], start_month="2025-01", end_month="2025-02"))
        self.assertEqual(partial["ranking"], [])
        self.assertIn("2025-02", partial["excluded"][0]["reason"])
        empty = self.traffic(airport="BUR")
        self.assertIsNone(empty["totals"]["passenger_load_factor_pct"])


class ScreenChecks(unittest.TestCase):
    @patch("airport_agent.kpis.airport_performance")
    @patch("airport_agent.kpis.airport_traffic")
    def test_opportunity_screen_uses_three_visible_votes(self, traffic, performance):
        def traffic_report(query):
            current = query.start_month == "2025-01"
            values = {"SFO": (96, 125), "SNA": (88, 115)} if current else {
                "SFO": (80, 100), "SNA": (80, 100),
            }
            passengers, seats = values[query.airport]
            return {"airport": query.airport, "months_with_data": [query.start_month],
                    "source": {"name": "Synthetic traffic"}, "totals": {
                        "passengers": passengers, "available_seats": seats}}

        def performance_report(query):
            total, delayed, cancelled = {"SFO": (100, 25, 1), "SNA": (100, 20, 2)}[query.airport]
            return {"airport": query.airport,
                    "totals": {"reported_operations": total,
                               "delayed_arrivals": delayed, "cancellations": cancelled},
                    "metrics": {"delayed_15min_pct": delayed,
                                "cancellation_pct": cancelled},
                    "source": {"name": "Synthetic performance"}}

        traffic.side_effect = traffic_report
        performance.side_effect = performance_report
        result = compare_airport_opportunity.invoke({
            "airports": ["SFO", "SNA"], "start_month": "2025-01", "end_month": "2025-01",
        })
        self.assertEqual(result["winner"], "SFO")
        self.assertEqual(result["dimension_wins"], {"SFO": 2, "SNA": 0})
        self.assertEqual([row["winner"] for row in result["dimensions"]], ["SFO", None, "SFO"])

    @patch("airport_agent.kpis.airport_performance")
    def test_congestion_uses_unrounded_rates_then_cancellations_and_excludes_missing(self, performance):
        def report(query):
            values = {"LAX": (10000, 2001, 10), "SNA": (10000, 2000, 20),
                      "SFO": (20000, 4000, 40), "BOS": (10000, 2000, 10), "BGR": (0, 0, 0)}
            if query.airport not in values:
                raise AviationDataError("Report unavailable")
            total, delayed, cancelled = values[query.airport]
            return {"airport": query.airport, "totals": {"reported_operations": total,
                    "delayed_arrivals": delayed, "cancellations": cancelled}}
        performance.side_effect = report
        result = compare_airport_performance.invoke(dict(airports=["SNA", "LAX", "SFO", "BOS", "MHT", "BGR"],
                                                         start_month="2025-01", end_month="2025-01"))
        self.assertEqual([(r["airport"], r["rank"]) for r in result["ranking"]],
                         [("LAX", 1), ("SFO", 2), ("SNA", 2), ("BOS", 4)])
        self.assertEqual({r["airport"] for r in result["excluded"]}, {"MHT", "BGR"})
        self.assertTrue(all(call.args[0].start_month == "2025-01" for call in performance.call_args_list))

    @patch("airport_agent.kpis.airport_performance", side_effect=AviationDataError("No delay report"))
    @patch("airport_agent.kpis.airport_traffic")
    def test_pressure_requires_growing_passengers_outpacing_seats_and_preserves_missing_evidence(self, traffic, performance):
        for passengers, seats, expected in [(88, 105, True), (88, 110, False), (72, 80, False), (80, 90, False)]:
            with self.subTest(passengers=passengers, seats=seats):
                def report(query):
                    current = query.start_month == "2025-01"
                    return {"airport": "SFO", "months_with_data": [query.start_month],
                            "source": {"name": "Synthetic test"}, "totals": {
                                "passengers": passengers if current else 80,
                                "available_seats": seats if current else 100}}
                traffic.side_effect = report
                result = get_airport_demand_pressure.invoke(dict(airport="SFO", start_month="2025-01", end_month="2025-01"))
                self.assertIs(result["growing_traffic_outpaces_seats"], expected)
                self.assertAlmostEqual(result["traffic"]["growth_gap_percentage_points"], (passengers / 80 - seats / 100) * 100, places=2)
                self.assertIsNone(result["domestic_arrival_performance"])
                self.assertEqual(result["missing_evidence"][0]["reason"], "No delay report")
                self.assertEqual(result["baseline"]["start_month"], "2024-01")
        traffic.side_effect = lambda query: {"months_with_data": []}
        self.assertIn("cannot establish comparable coverage", get_airport_demand_pressure.invoke(
            dict(airport="SFO", start_month="2025-01", end_month="2025-01")))


if __name__ == "__main__":
    unittest.main()
