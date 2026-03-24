# Test configuration
import pytest
import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_config_import():
    """Test that config can be imported."""
    from src.common import config
    assert config is not None


def test_storage_import():
    """Test that storage modules can be imported."""
    from src.storage import Database, get_db, init_db
    from src.storage.models import Base, Message
    assert Database is not None
    assert Message is not None


def test_collector_import():
    """Test that collector modules can be imported."""
    from src.collector.client import DiscordCollector, SimpleLangDetector
    assert DiscordCollector is not None
    assert SimpleLangDetector is not None


def test_api_import():
    """Test that API can be imported."""
    from src.api.app import app
    assert app is not None
