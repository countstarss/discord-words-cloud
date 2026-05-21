from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import discord
import pytest

from src.collector.client import DiscordCollector


def _collector(*, channel_id: int = 456) -> DiscordCollector:
    config = {
        "targets": {
            "guild_ids": [1],
            "channel_ids": [channel_id],
            "channel_registry": {
                channel_id: {
                    "region_key": "cn",
                    "region_name": "China",
                    "channel_id": channel_id,
                    "channel_name": "Forum Feedback",
                    "channel_group": "post",
                }
            },
        },
        "processor": {"min_text_length": 1},
    }
    return DiscordCollector(config=config, db=Mock())


def _message(
    *,
    message_id: int = 1001,
    channel: object,
    content: str = "hello",
    attachments: list[object] | None = None,
    embeds: list[object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=1),
        channel=channel,
        author=SimpleNamespace(id=42, bot=False),
        content=content,
        attachments=attachments or [],
        embeds=embeds or [],
        created_at=datetime(2026, 4, 3, 2, 30, tzinfo=timezone.utc),
    )


async def _async_items(items: list[object]):
    for item in items:
        yield item


def test_collector_keeps_plain_text_channel_scope_when_channel_has_category_parent():
    collector = _collector(channel_id=100)
    channel = SimpleNamespace(
        id=100,
        name="general",
        parent_id=999,
        type=discord.ChannelType.text,
    )

    payload = collector._build_payload(_message(channel=channel, content="plain channel message"))

    assert payload is not None
    assert payload["channel_id"] == 100
    assert payload["scope_key"] == "cn:100"
    assert payload["channel_name"] == "Forum Feedback"


def test_collector_uses_forum_parent_channel_for_thread_messages():
    collector = _collector(channel_id=456)
    parent = SimpleNamespace(id=456, name="Forum Feedback")
    thread = SimpleNamespace(
        id=900,
        name="Crash after latest update",
        parent_id=456,
        parent=parent,
        type=discord.ChannelType.public_thread,
    )

    payload = collector._build_payload(_message(channel=thread, content="The app crashes on launch."))

    assert payload is not None
    assert payload["channel_id"] == 456
    assert payload["scope_key"] == "cn:456"
    assert payload["region_name"] == "China"
    assert payload["channel_name"] == "Forum Feedback"
    assert "Post title: Crash after latest update" in payload["content"]
    assert "The app crashes on launch." in payload["content"]


def test_collector_keeps_forum_message_with_attachment_and_embed_without_text_body():
    collector = _collector(channel_id=456)
    parent = SimpleNamespace(id=456, name="Forum Feedback")
    thread = SimpleNamespace(
        id=901,
        name="Screenshot report",
        parent_id=456,
        parent=parent,
        type=discord.ChannelType.public_thread,
    )
    message = _message(
        channel=thread,
        content="",
        attachments=[SimpleNamespace(url="https://cdn.example.com/screenshot.png")],
        embeds=[SimpleNamespace(title="Bug details", url="https://example.com/bug")],
    )

    payload = collector._build_payload(message)

    assert payload is not None
    assert payload["channel_id"] == 456
    assert "Post title: Screenshot report" in payload["content"]
    assert "Attachment: https://cdn.example.com/screenshot.png" in payload["content"]
    assert "Embed: Bug details | https://example.com/bug" in payload["content"]


@pytest.mark.asyncio
async def test_backfill_resolver_expands_forum_channel_threads(monkeypatch):
    collector = _collector(channel_id=456)
    active_thread = SimpleNamespace(id=901, type=discord.ChannelType.public_thread)
    archived_thread = SimpleNamespace(id=902, type=discord.ChannelType.public_thread)

    class FakeForum:
        id = 456
        name = "Forum Feedback"
        type = discord.ChannelType.forum
        threads = [active_thread]

        def archived_threads(self, *, limit=None):
            assert limit is None
            return _async_items([archived_thread, active_thread])

    async def resolve_channel(channel_id: int):
        assert channel_id == 456
        return FakeForum()

    monkeypatch.setattr(collector, "_resolve_channel", resolve_channel)

    channels = await collector._resolve_backfill_channels()

    assert [channel.id for channel in channels] == [901, 902]


@pytest.mark.asyncio
async def test_thread_create_fetches_and_stores_starter_message():
    collector = _collector(channel_id=456)
    collector.db.upsert_message.return_value = True
    parent = SimpleNamespace(id=456, name="Forum Feedback")

    class FakeThread:
        def __init__(self):
            self.id = 903
            self.name = "New bug post"
            self.parent_id = 456
            self.parent = parent
            self.type = discord.ChannelType.public_thread

        async def fetch_message(self, message_id: int):
            assert message_id == 903
            return _message(message_id=903, channel=self, content="Starter message body")

    await collector.on_thread_create(FakeThread())

    collector.db.upsert_message.assert_called_once()
    payload = collector.db.upsert_message.call_args.args[0]
    assert payload["message_id"] == 903
    assert payload["channel_id"] == 456
    assert payload["scope_key"] == "cn:456"
    assert "Post title: New bug post" in payload["content"]
    assert "Starter message body" in payload["content"]
