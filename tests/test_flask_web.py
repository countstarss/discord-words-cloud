from __future__ import annotations

import unittest
from unittest.mock import patch


class FlaskWebTests(unittest.TestCase):
    def test_dashboard_html_contains_configured_channel_summary_hook(self):
        from src.api.app import _dashboard_html

        html = _dashboard_html()
        self.assertIn('id="configuredChannelCount"', html)
        self.assertIn('id="targetTree"', html)

    def _create_app(self):
        try:
            from src.web.flask_app import create_app
        except ModuleNotFoundError as exc:
            if exc.name == "flask":
                self.skipTest("Flask is not installed in the current environment.")
            raise
        return create_app()

    def test_dashboard_matches_fastapi_markup(self):
        from src.api.app import _dashboard_html

        app = self._create_app()
        with app.test_client() as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), _dashboard_html())

    def test_messages_endpoint_delegates_to_existing_logic(self):
        app = self._create_app()
        expected = {"messages": [], "limit": 5, "offset": 10}

        with patch("src.web.flask_app.api_get_messages", return_value=expected) as mock_get_messages:
            with app.test_client() as client:
                response = client.get("/api/messages?limit=5&offset=10&scope_key=demo")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        mock_get_messages.assert_called_once_with(limit=5, offset=10, scope_key="demo")

    def test_daily_report_endpoint_delegates_to_existing_logic(self):
        app = self._create_app()
        expected = {"reports": [{"report_date": "2026-03-26"}]}

        with patch("src.web.flask_app.api_get_daily_reports", return_value=expected) as mock_get_reports:
            with app.test_client() as client:
                response = client.get("/daily-report?scope_key=global")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        mock_get_reports.assert_called_once_with(scope_key="global")
