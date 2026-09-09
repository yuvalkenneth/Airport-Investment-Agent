import unittest
from unittest.mock import patch

import httpx

from scripts.inspect_apis import fetch, opensky_headers


class ApiInspectionTest(unittest.TestCase):
    def test_httpx_preserves_data_and_errors_without_recording_credentials(self):
        def respond(request):
            if request.method == "POST":
                self.assertEqual(request.headers["content-type"], "application/x-www-form-urlencoded")
                return httpx.Response(200, json={"access_token": "test-token"})
            if request.url.path == "/denied":
                return httpx.Response(403, text="You cannot access historical flights")
            if request.url.path == "/timeout":
                raise httpx.ReadTimeout("Timed out", request=request)
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            return httpx.Response(200, json=[{"total_departures": "5412"}])

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with patch.dict("os.environ", {"OPENSKY_CLIENT_ID": "test-id", "OPENSKY_CLIENT_SECRET": "test-secret"}):
                headers = opensky_headers(client)
            sample = fetch(client, "https://example.test/data", headers)
            self.assertEqual(sample["body"], [{"total_departures": "5412"}])
            self.assertNotIn("test-token", str(sample))
            denied = fetch(client, "https://example.test/denied")
            self.assertEqual(denied["http_status"], 403)
            self.assertEqual(denied["body"], "You cannot access historical flights")
            self.assertIsNone(fetch(client, "https://example.test/timeout")["http_status"])

if __name__ == "__main__":
    unittest.main()
