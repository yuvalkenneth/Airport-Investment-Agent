"""Small offline checks for aviation response normalization."""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from airport_agent.aviation import FlightQuery, get_flights, route_distance_miles


class AviationChecks(unittest.TestCase):
    def test_distance_and_fixed_long_haul_threshold(self):
        anc = {"lat": 61.1741, "lon": -149.9981}
        sea = {"lat": 47.4502, "lon": -122.3088}
        nrt = {"lat": 35.772, "lon": 140.3929}
        self.assertLess(route_distance_miles(anc, sea), 3_000)
        self.assertGreater(route_distance_miles(anc, nrt), 3_000)

    @patch("airport_agent.aviation._airport_details_many")
    @patch("airport_agent.aviation._opensky_client")
    @patch("airport_agent.aviation.airport_details")
    def test_open_sky_flights_keep_unknown_destinations_visible(self, details, client_factory, details_many):
        details.return_value = {"icaoId": "PANC", "lat": 61.1741, "lon": -149.9981}
        details_many.return_value = {"RJAA": {"icaoId": "RJAA", "lat": 35.772, "lon": 140.3929}}
        client_factory.return_value.get_departures_by_airport.return_value = [
            SimpleNamespace(icao24="one", callsign="TEST1 ", firstSeen=1, lastSeen=2, estArrivalAirport="RJAA"),
            SimpleNamespace(icao24="two", callsign=None, firstSeen=3, lastSeen=4, estArrivalAirport=None),
        ]
        query = FlightQuery(airport="ANC", start_date=date(2026, 9, 7), end_date=date(2026, 9, 7))

        result = get_flights(query, "departure")

        self.assertEqual(result["observed_flights"], 2)
        self.assertEqual(result["unknown_other_airport"], 1)
        self.assertEqual(result["flights"][0]["haul"], "long_haul")


if __name__ == "__main__":
    unittest.main()
