"""Small offline check of the BTS HTML adapter, calculations, and cache."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from pydantic import ValidationError

from airport_agent.aviation import AviationDataError
from airport_agent.performance import CACHE_TTL_SECONDS, PerformanceQuery, airport_performance
from airport_agent.tools import TOOLS, get_airport_performance

# The intentionally unclosed option tags match BTS's public report form.
REPORT = """
<select name=Carrier><option value=All>All<option value=UA>United</select>
<select name=Airport><option value=All>All<option selected value=SFO::San%20Francisco>SFO</select>
<select name=PeriodFrom><option selected value=24301>January 2025<option value=24302>February 2025</select>
<select name=PeriodTo><option selected value=24301>January 2025<option value=24302>February 2025</select>
<table><tr><td><table>
<tr><td></td><td>Number of Operations</td><td>% of Total Operations</td><td>Delayed Minutes</td><td>% of Total<br>Delayed Minutes</td></tr>
<tr><td>On Time</td><td>9,331</td><td>83.60%</td><td>N/A</td><td>N/A</td></tr>
<tr><td>Air Carrier Delay</td><td>642</td><td>5.75%</td><td>42,639</td><td>37.01%</td></tr>
<tr><td>Weather Delay</td><td>75</td><td>0.67%</td><td>7,520</td><td>6.53%</td></tr>
<tr><td>National Aviation System Delay</td><td>498</td><td>4.46%</td><td>23,154</td><td>20.10%</td></tr>
<tr><td>Security Delay</td><td>8</td><td>0.07%</td><td>236</td><td>0.20%</td></tr>
<tr><td>Aircraft Arriving Late</td><td>501</td><td>4.49%</td><td>41,664</td><td>36.16%</td></tr>
<tr><td>Cancelled</td><td>101</td><td>0.90%</td><td>N/A</td><td>N/A</td></tr>
<tr><td>Diverted</td><td>6</td><td>0.05%</td><td>N/A</td><td>N/A</td></tr>
<tr><td>Total Operations</td><td>11,161</td><td>100.00%</td><td>115,213</td><td>100.00%</td></tr>
</table></td></tr></table>
"""
QUERY = dict(airport="SFO", start_month="2025-01", end_month="2025-01")


class PerformanceChecks(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("airport_agent.performance.CACHE_PATH", Path(directory) / "cache.sqlite3"))
        self.requests = []
        self.html = REPORT
        self.status = 200

        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, text=REPORT if request.method == "GET" else self.html)

        self.enterContext(patch("airport_agent.performance._client", side_effect=lambda: httpx.Client(
            transport=httpx.MockTransport(handle),
        )))

    def test_registered_tool_parses_report_and_reuses_cache(self):
        self.assertIn(get_airport_performance, TOOLS)
        with patch("airport_agent.performance.time.time", return_value=1_000_000):
            result = get_airport_performance.invoke(QUERY)
            cached = get_airport_performance.invoke(QUERY)
        self.assertEqual(result["totals"], dict(reported_operations=11161, on_time_arrivals=9331,
                                             delayed_arrivals=1723, cancellations=101,
                                             diversions=6, delayed_minutes=115213))
        self.assertEqual(result["metrics"], dict(on_time_pct=83.6, delayed_15min_pct=15.44,
                                              cancellation_pct=0.9, diversion_pct=0.05,
                                              avg_delay_minutes_delayed_arrivals=66.87))
        self.assertEqual(result["delay_causes"][2]["share_of_delay_minutes_pct"], 20.1)
        self.assertFalse(result["cache"]["hit"])
        self.assertTrue(cached["cache"]["hit"])
        self.assertEqual([r.method for r in self.requests], ["GET", "POST"])
        self.assertEqual(parse_qs(self.requests[1].content.decode()), {
            "Carrier": ["All"], "Airport": ["SFO::San%20Francisco"],
            "PeriodFrom": ["24301"], "PeriodTo": ["24301"],
        })
        with patch("airport_agent.performance.time.time", return_value=1_000_000 + CACHE_TTL_SECONDS):
            refreshed = get_airport_performance.invoke(QUERY)
        self.assertFalse(refreshed["cache"]["hit"])
        self.assertEqual(len(self.requests), 4)

    def test_wrong_scope_and_bad_reports_are_not_cached(self):
        for html in (REPORT.replace("selected value=SFO", "value=SFO"),
                     REPORT.replace("selected value=24301", "selected value=24302"),
                     REPORT.replace("value=UA", "selected value=UA"),
                     REPORT.replace("9,331", "12,000"),
                     REPORT.replace("42,639", "N/A"),
                     REPORT.replace("Delayed Minutes</td>", "Different Column</td>"),
                     "<html>No records</html>"):
            self.html = html
            with self.subTest(html=html[:100]), self.assertRaises(AviationDataError):
                airport_performance(PerformanceQuery(**QUERY))
        self.status = 503
        with self.assertRaises(AviationDataError):
            airport_performance(PerformanceQuery(**QUERY))
        self.status, self.html = 200, REPORT
        self.assertFalse(airport_performance(PerformanceQuery(**QUERY))["cache"]["hit"])

    def test_period_range_and_unavailable_months(self):
        self.html = REPORT.replace(
            "<select name=PeriodTo><option selected value=24301>",
            "<select name=PeriodTo><option value=24301>",
        ).replace("value=24302>February", "selected value=24302>February")
        # Keep PeriodFrom at January while selecting February as the inclusive end.
        self.html = self.html.replace(
            "<select name=PeriodFrom><option selected value=24301>January 2025<option selected value=24302>",
            "<select name=PeriodFrom><option selected value=24301>January 2025<option value=24302>",
        )
        result = airport_performance(PerformanceQuery(**(QUERY | {"end_month": "2025-02"})))
        self.assertEqual(result["period"]["end_month"], "2025-02")
        self.assertEqual(parse_qs(self.requests[-1].content.decode())["PeriodTo"], ["24302"])
        self.assertNotIn("months_with_data", result)
        for kwargs in ({"start_month": "2025-13"}, {"start_month": "2025-02"},
                       {"end_month": "2026-01"}, {"direction": "outbound"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                PerformanceQuery(**(QUERY | kwargs))
        for kwargs in ({"end_month": "2025-03"}, {"airport": "ZZZ"}):
            before = len(self.requests)
            with self.subTest(kwargs=kwargs), self.assertRaises(AviationDataError):
                airport_performance(PerformanceQuery(**(QUERY | kwargs)))
            self.assertEqual(len(self.requests), before + 1)  # Only the availability form was fetched.


if __name__ == "__main__":
    unittest.main()
