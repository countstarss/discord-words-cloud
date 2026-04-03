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
    from src.reports import DailyReportService, DailyReportTranslator, run_daily_report_for_date, run_daily_report_worker

    assert DailyReportService is not None
    assert DailyReportTranslator is not None
    assert run_daily_report_for_date is not None
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

    monkeypatch.setattr(config_module, "_load_project_env", lambda *args, **kwargs: None)
    monkeypatch.delenv("DISCORD_GUILD_IDS", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_IDS", raising=False)
    monkeypatch.delenv("DISCORD_REGION_CHANNELS", raising=False)
    monkeypatch.setenv("TARGET_GUILD_IDS", "[1283101973045841952]")
    monkeypatch.setenv("TARGET_CHANNEL_IDS", "[1400146275512352799]")

    config = load_config(str(config_path))
    assert config["targets"]["guild_ids"] == [1283101973045841952]
    assert config["targets"]["channel_ids"] == [1400146275512352799]
    assert config["targets"]["regions"][0]["name"] == "Default"
    assert config["targets"]["regions"][0]["channels"][0]["name"] == "channel 1400146275512352799"


def test_load_config_supports_region_channel_groups(monkeypatch, tmp_path):
    from src.common.config import load_config
    import src.common.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "targets:",
                "  regions: ${DISCORD_REGION_CHANNELS:[]}",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_module, "_load_project_env", lambda *args, **kwargs: None)
    monkeypatch.delenv("DISCORD_GUILD_IDS", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_IDS", raising=False)
    monkeypatch.setenv(
        "DISCORD_REGION_CHANNELS",
        '[{"key":"cn","name":"中国","guild_id":1283101973045841952,"channels":[{"id":1400146275512352799,"name":"频道标题1"},{"id":1400146275512352800,"name":"频道标题2"}]},{"key":"th","name":"泰国","guild_id":1483900000000000000,"channels":[{"id":1483900000000000001,"name":"频道标题A"}]}]',
    )

    config = load_config(str(config_path))
    assert config["targets"]["guild_ids"] == [1283101973045841952, 1483900000000000000]
    assert config["targets"]["channel_ids"] == [
        1400146275512352799,
        1400146275512352800,
        1483900000000000001,
    ]
    assert [region["name"] for region in config["targets"]["regions"]] == ["中国", "泰国"]
    assert config["targets"]["regions"][0]["channels"][0]["name"] == "频道标题1"
    assert config["targets"]["channel_registry"][1400146275512352799]["region_name"] == "中国"


def test_multiline_region_channels_env_is_supported(monkeypatch, tmp_path):
    from src.common.config import load_config
    import src.common.config as config_module

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "DISCORD_REGION_CHANNELS=[",
                '  {"key":"cn",',
                '    "name":"中文",',
                '    "guild_id":1283101973045841952,',
                '    "channels":[',
                '      {"id":1296952760314495066,"name":"聊天室"},',
                '      {"id":1339199601781243954,"name":"Rubii反馈"}',
                "    ]",
                "  },",
                '  {"key":"th",',
                '    "name":"泰国",',
                '    "guild_id":1283101973045841952,',
                '    "channels":[',
                '      {"id":1400146275512352799,"name":"聊天室"}',
                "    ]",
                "  }",
                "]",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "targets:",
                "  regions: ${DISCORD_REGION_CHANNELS:[]}",
            ]
        ),
        encoding="utf-8",
    )

    for key in ["DISCORD_GUILD_IDS", "DISCORD_CHANNEL_IDS", "DISCORD_REGION_CHANNELS"]:
        monkeypatch.delenv(key, raising=False)

    original_loader = config_module._load_project_env

    def custom_loader(*args, **kwargs):
        return original_loader(env_path, override=True)

    monkeypatch.setattr(config_module, "_load_project_env", custom_loader)

    config = load_config(str(config_path))
    assert [region["name"] for region in config["targets"]["regions"]] == ["中文", "泰国"]
    assert config["targets"]["regions"][0]["channels"][0]["name"] == "聊天室"
    assert config["targets"]["channel_registry"][1296952760314495066]["region_name"] == "中文"
