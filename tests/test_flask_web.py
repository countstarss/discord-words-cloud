from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch


class FlaskWebTests(unittest.TestCase):
    def _create_app(self):
        try:
            from src.web.flask_app import create_app
        except ModuleNotFoundError as exc:
            if exc.name in {"flask", "mistune"}:
                self.skipTest("Flask is not installed in the current environment.")
            raise
        return create_app()

    def test_template_structure_exists(self):
        template_root = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
        static_root = Path(__file__).resolve().parent.parent / "src" / "web" / "static"

        self.assertTrue((template_root / "base.html").exists())
        self.assertTrue((template_root / "dashboard" / "index.html").exists())
        self.assertTrue((template_root / "messages" / "index.html").exists())
        self.assertTrue((template_root / "reports" / "index.html").exists())
        self.assertTrue((template_root / "components" / "sidebar.html").exists())
        self.assertTrue((static_root / "css" / "app.css").exists())
        self.assertTrue((static_root / "js" / "common.js").exists())

    def test_dashboard_route_renders_sidebar_layout(self):
        app = self._create_app()
        with app.test_client() as client:
            response = client.get("/")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("sidebar", html)
        self.assertIn('id="reportDate"', html)
        self.assertIn("Dashboard", html)
        self.assertNotIn("FLASK WORKSPACE", html)

    def test_reports_route_renders_report_browser_layout(self):
        app = self._create_app()
        with app.test_client() as client:
            response = client.get("/reports")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="scopeSelect"', html)
        self.assertIn('id="reportList"', html)
        self.assertIn('id="reportBody"', html)
        self.assertIn("Reports", html)
        self.assertNotIn("Configured Coverage", html)

    def test_messages_route_renders_message_browser_layout(self):
        app = self._create_app()
        with app.test_client() as client:
            response = client.get("/messages")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="scopeSelect"', html)
        self.assertIn('id="reportDate"', html)
        self.assertIn('id="messageList"', html)
        self.assertIn("Messages", html)

    def test_messages_endpoint_delegates_to_existing_logic(self):
        app = self._create_app()
        expected = {"messages": [], "limit": 5, "offset": 10}

        with patch("src.web.blueprints.api.api_get_messages", return_value=expected) as mock_get_messages:
            with app.test_client() as client:
                response = client.get("/api/messages?limit=5&offset=10&scope_key=demo")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        mock_get_messages.assert_called_once_with(limit=5, offset=10, scope_key="demo")

    def test_message_browser_endpoint_delegates_to_existing_logic(self):
        app = self._create_app()
        expected = {"messages": [], "available_scopes": [], "available_dates": [], "pagination": {"page": 2}}

        with patch("src.web.blueprints.api.api_get_messages_browser", return_value=expected) as mock_get_messages:
            with app.test_client() as client:
                response = client.get("/api/messages/browser?scope_key=demo&report_date=2026-04-03&page=2&page_size=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        mock_get_messages.assert_called_once_with(
            scope_key="demo",
            report_date="2026-04-03",
            page=2,
            page_size=20,
        )

    def test_message_browser_endpoint_clamps_page_bounds(self):
        app = self._create_app()

        with patch("src.web.blueprints.api.api_get_messages_browser", return_value={"messages": []}) as mock_get_messages:
            with app.test_client() as client:
                response = client.get("/api/messages/browser?page=-3&page_size=999")

        self.assertEqual(response.status_code, 200)
        mock_get_messages.assert_called_once_with(
            scope_key=None,
            report_date=None,
            page=1,
            page_size=100,
        )

    def test_daily_report_endpoint_delegates_to_existing_logic(self):
        app = self._create_app()
        expected = {"reports": [{"report_date": "2026-03-26", "content_cn": "# Title"}]}

        with patch("src.web.blueprints.api.api_get_daily_reports", return_value=expected) as mock_get_reports:
            with patch("src.web.blueprints.api.enrich_daily_report_payload", return_value={"reports": [{"report_date": "2026-03-26", "content_html": "<h1>Title</h1>"}]}) as mock_enrich:
                with app.test_client() as client:
                    response = client.get("/daily-report?scope_key=global")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"reports": [{"report_date": "2026-03-26", "content_html": "<h1>Title</h1>"}]})
        mock_get_reports.assert_called_once_with(scope_key="global")
        mock_enrich.assert_called_once_with(expected)

    def test_daily_report_payload_is_enriched_with_content_html(self):
        from src.web.markdown import enrich_daily_report_payload

        payload = {"reports": [{"report_date": "2026-03-26", "content_cn": "# Title"}]}

        with patch("src.web.markdown.render_markdown_html", return_value="<h1>Title</h1>\n") as mock_render:
            enriched = enrich_daily_report_payload(payload)

        self.assertEqual(enriched["reports"][0]["content_html"], "<h1>Title</h1>\n")
        mock_render.assert_called_once_with("# Title")
