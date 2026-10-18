"""POST /v1/fx/calendar/parse (#401 part 2b): auth, a tiny valid feed, strict 422s with no payload echo.

Every event here is SYNTHETIC and hand-written in the feed's shape; no third-party data is copied.
"""

import asyncio
from datetime import datetime, timezone
import unittest

import httpx

from market_analysis.api import create_app

TOKEN = "test-service-token-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PATH = "/v1/fx/calendar/parse"


def ev(title, country, date, impact, forecast="", previous=""):
    return {"title": title, "country": country, "date": date, "impact": impact, "forecast": forecast,
            "previous": previous}


WEEK = [
    ev("CPI y/y", "GBP", "2026-10-21T02:00:00-04:00", "High", "3.1%", "3.0%"),
    ev("Bank Holiday", "JPY", "2026-10-19T00:00:00-04:00", "Holiday"),
    ev("G20 Meetings", "All", "2026-10-20T00:00:00-04:00", "Low"),
]


class FxCalendarParseApiTests(unittest.TestCase):
    def post(self, body, headers=HEADERS):
        app = create_app(TOKEN)

        async def send():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.post(PATH, json=body, headers=headers)
        return asyncio.run(send())

    def test_auth_is_required(self):
        self.assertEqual(self.post({"payload": WEEK}, headers={}).status_code, 401)
        self.assertEqual(self.post({"payload": WEEK}, headers={"Authorization": "Bearer " + "y" * 40}).status_code, 401)

    def test_a_valid_week(self):
        response = self.post({"payload": WEEK})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {
            "schema_version": 1,
            "rows": [{"source": "forexfactory", "source_event_key": "GBP|CPI y/y|2026-10-18|0",
                      "event_ts": "2026-10-21T06:00:00Z", "currency": "GBP", "impact": "high", "title": "CPI y/y",
                      "detail": {"forecast": "3.1%", "previous": "3.0%"}}],
            # Sunday 2026-10-25 00:00 New York (EDT) = 04:00 UTC.
            "covered_to_ms": int(datetime(2026, 10, 25, 4, tzinfo=timezone.utc).timestamp()) * 1000,
            "skipped_holidays": 1,
            "skipped_all_low": 1,
        })

    def test_parser_rejections_are_422_without_the_payload(self):
        secret_title = "Do-Not-Echo-This-Title"
        cases = [
            [ev(secret_title, "GBP", "2026-10-21T02:00:00", "High")],  # naive date
            [ev(secret_title, "GBP", "2026-10-21T02:00:00-04:00", "Critical")],
            [],
            [WEEK[1]],  # only a holiday: an empty result is a failed fetch
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.post({"payload": payload})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret_title, response.text)
                self.assertLessEqual(len(response.json()["detail"]), 300)

    def test_all_with_high_impact_needs_an_owner_decision(self):
        response = self.post({"payload": WEEK + [ev("G7 Statement", "All", "2026-10-21T09:00:00-04:00", "High")]})
        self.assertEqual(response.status_code, 422)
        self.assertIn("needs an owner decision", response.json()["detail"])

    def test_a_body_that_is_not_a_list_is_rejected_by_the_model(self):
        for body in ({"payload": {"events": WEEK}}, {"payload": WEEK, "extra": 1}, {}):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "Invalid analysis request"})


if __name__ == "__main__":
    unittest.main()
