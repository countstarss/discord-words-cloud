from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
        self.assertTrue((template_root / "export" / "index.html").exists())
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
        self.assertIn('id="showAllMessages"', html)
        self.assertIn('id="messageList"', html)
        self.assertIn('id="exportMessagesCsv"', html)
        self.assertIn("Messages", html)

    def test_export_route_renders_export_layout(self):
        app = self._create_app()
        with app.test_client() as client:
            response = client.get("/export")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="reportType"', html)
        self.assertIn('id="scopeSelect"', html)
        self.assertIn('id="exportList"', html)
        self.assertIn("Export", html)

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
            all_messages=False,
        )

    def test_message_browser_endpoint_delegates_all_messages_flag(self):
        app = self._create_app()
        expected = {"messages": [], "all_messages": True, "pagination": {"page": 1}}

        with patch("src.web.blueprints.api.api_get_messages_browser", return_value=expected) as mock_get_messages:
            with app.test_client() as client:
                response = client.get("/api/messages/browser?scope_key=demo&all_messages=true&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        mock_get_messages.assert_called_once_with(
            scope_key="demo",
            report_date=None,
            page=1,
            page_size=20,
            all_messages=True,
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
            all_messages=False,
        )

    def test_message_browser_payload_all_messages_uses_scope_without_date_filter(self):
        from src.web.api_payloads import get_messages_browser

        mock_db = Mock()
        mock_db.get_message_dates_for_scope.return_value = [date(2026, 4, 3)]
        mock_db.count_messages.return_value = 25
        mock_db.get_all_messages.return_value = []

        scopes = [{"scope_key": "cn:100", "label": "China / general"}]
        with patch("src.web.api_payloads.get_db", return_value=mock_db):
            with patch("src.web.api_payloads._message_browser_scopes", return_value=scopes):
                payload = get_messages_browser(scope_key="cn:100", report_date="2026-04-03", page=2, page_size=20, all_messages=True)

        self.assertTrue(payload["all_messages"])
        self.assertIsNone(payload["selected_date"])
        self.assertEqual(payload["pagination"]["total_items"], 25)
        mock_db.count_messages.assert_called_once_with(scope_key="cn:100")
        mock_db.get_all_messages.assert_called_once_with(limit=20, offset=20, scope_key="cn:100")
        mock_db.count_messages_for_window.assert_not_called()
        mock_db.get_messages_page_for_window.assert_not_called()

    def test_messages_csv_export_returns_csv_download(self):
        app = self._create_app()
        mock_db = Mock()
        mock_db.get_messages_for_window.return_value = [
            SimpleNamespace(
                created_at=datetime(2026, 4, 3, 2, 30, tzinfo=timezone.utc),
                region_name="China",
                channel_name="general",
                message_id=123,
                author_id=456,
                detected_language="en",
                content="hello, csv",
            )
        ]
        scopes = [{"scope_key": "cn:100", "label": "China / general", "region_key": "cn", "channel_name": "general"}]

        with patch("src.web.blueprints.messages._message_browser_scopes", return_value=scopes):
            with patch("src.web.blueprints.messages.get_db", return_value=mock_db):
                with app.test_client() as client:
                    response = client.get("/messages/export.csv?scope_key=cn:100&report_date=2026-04-03")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('attachment; filename="messages_cn_general_20260403.csv"', response.headers["Content-Disposition"])
        csv_text = response.get_data(as_text=True)
        self.assertTrue(csv_text.startswith("\ufeffcreated_at,region_name,channel_name,message_id,author_id,detected_language,content"))
        self.assertIn('"hello, csv"', csv_text)
        mock_db.get_messages_for_window.assert_called_once()
        self.assertEqual(mock_db.get_messages_for_window.call_args.kwargs["scope_key"], "cn:100")

    def test_messages_csv_export_all_messages_returns_scope_download(self):
        app = self._create_app()
        mock_db = Mock()
        mock_db.get_messages_for_scope.return_value = [
            SimpleNamespace(
                created_at=datetime(2026, 4, 3, 2, 30, tzinfo=timezone.utc),
                region_name="China",
                channel_name="general",
                message_id=123,
                author_id=456,
                detected_language="en",
                content="all scope content",
            )
        ]
        scopes = [{"scope_key": "cn:100", "label": "China / general", "region_key": "cn", "channel_name": "general"}]

        with patch("src.web.blueprints.messages._message_browser_scopes", return_value=scopes):
            with patch("src.web.blueprints.messages.get_db", return_value=mock_db):
                with app.test_client() as client:
                    response = client.get("/messages/export.csv?scope_key=cn:100&all_messages=true")

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="messages_cn_general_all.csv"', response.headers["Content-Disposition"])
        csv_text = response.get_data(as_text=True)
        self.assertIn("all scope content", csv_text)
        mock_db.get_messages_for_scope.assert_called_once_with(scope_key="cn:100")
        mock_db.get_messages_for_window.assert_not_called()

    def test_messages_csv_export_rejects_unknown_scope(self):
        app = self._create_app()
        scopes = [{"scope_key": "cn:100", "label": "China / general"}]

        with patch("src.web.blueprints.messages._message_browser_scopes", return_value=scopes):
            with app.test_client() as client:
                response = client.get("/messages/export.csv?scope_key=cn:404&report_date=2026-04-03")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "Invalid scope_key"})

    def test_messages_csv_export_rejects_invalid_date(self):
        app = self._create_app()
        scopes = [{"scope_key": "cn:100", "label": "China / general"}]

        with patch("src.web.blueprints.messages._message_browser_scopes", return_value=scopes):
            with app.test_client() as client:
                response = client.get("/messages/export.csv?scope_key=cn:100&report_date=not-a-date")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "Invalid report_date"})

    def test_messages_csv_export_rejects_missing_configured_scopes(self):
        app = self._create_app()

        with patch("src.web.blueprints.messages._message_browser_scopes", return_value=[]):
            with app.test_client() as client:
                response = client.get("/messages/export.csv?scope_key=cn:100&report_date=2026-04-03")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "No message scopes configured"})

    def test_messages_csv_export_returns_header_for_empty_result(self):
        app = self._create_app()
        mock_db = Mock()
        mock_db.get_messages_for_window.return_value = []
        scopes = [{"scope_key": "cn:100", "label": "China / general"}]

        with patch("src.web.blueprints.messages._message_browser_scopes", return_value=scopes):
            with patch("src.web.blueprints.messages.get_db", return_value=mock_db):
                with app.test_client() as client:
                    response = client.get("/messages/export.csv?scope_key=cn:100&report_date=2026-04-03")

        self.assertEqual(response.status_code, 200)
        csv_text = response.get_data(as_text=True)
        self.assertEqual(
            csv_text,
            "\ufeffcreated_at,region_name,channel_name,message_id,author_id,detected_language,content\r\n",
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

    def test_export_catalog_endpoint_uses_catalog_helper(self):
        app = self._create_app()
        expected = {"items": [], "available_scopes": [], "available_dates": [], "selected_scope": "__all__"}

        with patch("src.web.blueprints.export._catalog_payload", return_value=expected) as mock_catalog:
            with app.test_client() as client:
                response = client.get("/api/export/catalog?report_type=hourly&scope_key=global&report_date=2026-04-03")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        mock_catalog.assert_called_once_with("hourly", "global", "2026-04-03")

    def test_export_download_endpoint_uses_download_helper(self):
        app = self._create_app()

        with patch("src.web.blueprints.export._download_response", return_value=app.response_class("ok", status=200)) as mock_download:
            with app.test_client() as client:
                response = client.post("/export/download", json={"items": [{"report_type": "daily", "id": 1}]})

        self.assertEqual(response.status_code, 200)
        mock_download.assert_called_once_with([{"report_type": "daily", "id": 1}])

    def test_export_catalog_uses_non_empty_filter_for_daily_reports(self):
        from src.web.blueprints.export import _catalog_payload

        mock_db = Mock()
        mock_db.get_daily_report_dates.return_value = [date(2026, 4, 3)]
        mock_db.list_daily_reports.return_value = []

        with patch("src.web.blueprints.export.get_db", return_value=mock_db):
            with patch("src.web.blueprints.export._configured_export_scopes", return_value=[]):
                payload = _catalog_payload("daily", "global", None)

        self.assertEqual(payload["report_type"], "daily")
        mock_db.get_daily_report_dates.assert_called_once_with(scope_key="global", non_empty_only=True)
        mock_db.list_daily_reports.assert_called_once_with(
            report_date=date(2026, 4, 3),
            scope_key="global",
            non_empty_only=True,
        )

    def test_export_catalog_uses_non_empty_filter_for_hourly_reports(self):
        from src.web.blueprints.export import _catalog_payload

        mock_db = Mock()
        mock_db.get_hourly_report_dates.return_value = [date(2026, 4, 3)]
        mock_db.list_hourly_reports.return_value = []

        with patch("src.web.blueprints.export.get_db", return_value=mock_db):
            with patch("src.web.blueprints.export._configured_export_scopes", return_value=[]):
                payload = _catalog_payload("hourly", "global", None)

        self.assertEqual(payload["report_type"], "hourly")
        mock_db.get_hourly_report_dates.assert_called_once_with(scope_key="global", non_empty_only=True)
        mock_db.list_hourly_reports.assert_called_once_with(
            report_date=date(2026, 4, 3),
            scope_key="global",
            non_empty_only=True,
        )

    def test_daily_report_payload_is_enriched_with_content_html(self):
        from src.web.markdown import enrich_daily_report_payload

        payload = {"reports": [{"report_date": "2026-03-26", "content_cn": "# Title"}]}

        with patch("src.web.markdown.render_markdown_html", return_value="<h1>Title</h1>\n") as mock_render:
            enriched = enrich_daily_report_payload(payload)

        self.assertEqual(enriched["reports"][0]["content_html"], "<h1>Title</h1>\n")
        mock_render.assert_called_once_with("# Title")
