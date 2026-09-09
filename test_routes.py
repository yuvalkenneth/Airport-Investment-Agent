"""Small offline checks for aviation response normalization."""

import unittest
from datetime import date
from unittest.mock import patch

import httpx

from airport_agent.aviation import FlightQuery, get_flights, route_distance_miles


class AviationChecks(unittest.TestCase):
    def test_distance_and_fixed_long_haul_threshold(self):
        anc = {"lat": 61.1741, "lon": -149.9981}
        sea = {"lat": 47.4502, "lon": -122.3088}
        nrt = {"lat": 35.772, "lon": 140.3929}
        self.assertLess(route_distance_miles(anc, sea), 3_000)
        self.assertGreater(route_distance_miles(anc, nrt), 3_000)

    @patch("airport_agent.aviation.airport_details")
    @patch("airport_agent.aviation._client")
    def test_open_sky_flights_keep_unknown_destinations_visible(self, client_factory, details):
        details.side_effect = lambda code: {
            "ANC": {"icaoId": "PANC", "lat": 61.1741, "lon": -149.9981},
            "RJAA": {"icaoId": "RJAA", "lat": 35.772, "lon": 140.3929},
        }[code]
        response = httpx.Response(200, json=[
            {"icao24": "one", "callsign": "TEST1 ", "firstSeen": 1, "lastSeen": 2, "estArrivalAirport": "RJAA"},
            {"icao24": "two", "callsign": None, "firstSeen": 3, "lastSeen": 4, "estArrivalAirport": None},
        ], request=httpx.Request("GET", "https://opensky-network.org"))
        client = client_factory.return_value.__enter__.return_value
        client.get.return_value = response
        query = FlightQuery(airport="ANC", start_date=date(2026, 9, 7), end_date=date(2026, 9, 7))

        result = get_flights(query, "departure")

        self.assertEqual(result["observed_flights"], 2)
        self.assertEqual(result["unknown_other_airport"], 1)
        self.assertEqual(result["flights"][0]["haul"], "long_haul")


if __name__ == "__main__":
    unittest.main()
