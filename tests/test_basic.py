# Test configuration
import pytest
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_config_import():
    """Test that config can be imported."""
    from src.common import config
    assert config is not None


def test_storage_import():
    """Test that storage modules can be imported."""
    from src.storage import Database, get_db, init_db
    from src.storage.models import Base, DailyReport, HourlyReport, Message
    assert Database is not None
    assert Message is not None
    assert DailyReport is not None
    assert HourlyReport is not None


def test_collector_import():
    """Test that collector modules can be imported."""
    from src.collector.client import DiscordCollector, SimpleLangDetector
    assert DiscordCollector is not None
    assert SimpleLangDetector is not None


def test_api_import():
    """Test that API can be imported."""
    from src.api.app import app
    assert app is not None


def test_reports_import():
    """Test that daily report modules can be imported."""
    from src.reports import DailyReportService, DailyReportTranslator, run_daily_report_once_now, run_daily_report_worker

    assert DailyReportService is not None
    assert DailyReportTranslator is not None
    assert run_daily_report_once_now is not None
    assert run_daily_report_worker is not None


def test_load_config_supports_target_env_aliases(monkeypatch, tmp_path):
    from src.common.config import load_config
    import src.common.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "targets:",
                "  guild_ids: ${DISCORD_GUILD_IDS:}",
                "  channel_ids: ${DISCORD_CHANNEL_IDS:}",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("DISCORD_GUILD_IDS", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_IDS", raising=False)
    monkeypatch.setenv("TARGET_GUILD_IDS", "[1283101973045841952]")
    monkeypatch.setenv("TARGET_CHANNEL_IDS", "[1400146275512352799]")

    config = load_config(str(config_path))
    assert config["targets"]["guild_ids"] == "[1283101973045841952]"
    assert config["targets"]["channel_ids"] == "[1400146275512352799]"
