# Collector client for Discord
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import discord

from ..storage import Database


# MARK: - Simple Language Detector
class SimpleLangDetector:
    """Best-effort script-based language detector for metadata only."""

    THAI_START = 0x0E00
    THAI_END = 0x0E7F
    HIRAGANA_START = 0x3040
    HIRAGANA_END = 0x309F
    KATAKANA_START = 0x30A0
    KATAKANA_END = 0x30FF
    HANGUL_START = 0xAC00
    HANGUL_END = 0xD7AF
    CJK_START = 0x4E00
    CJK_END = 0x9FFF
    SIMPLIFIED_HINTS = {"这", "个", "们", "为", "发", "现", "没", "后", "里", "应", "开", "关"}
    TRADITIONAL_HINTS = {"這", "個", "們", "為", "發", "現", "沒", "後", "裡", "應", "開", "關"}

    def __init__(self, min_confidence: float = 0.2):
        self.min_confidence = min_confidence

    def detect(self, text: str) -> tuple[str, float]:
        if not text:
            return "unknown", 0.0

        counts = {
            "th": 0,
            "ja": 0,
            "ko": 0,
            "han": 0,
            "latin": 0,
        }
        total_chars = 0
        for char in text:
            code = ord(char)
            if self.THAI_START <= code <= self.THAI_END:
                counts["th"] += 1
                total_chars += 1
            elif self.HIRAGANA_START <= code <= self.HIRAGANA_END or self.KATAKANA_START <= code <= self.KATAKANA_END:
                counts["ja"] += 1
                total_chars += 1
            elif self.HANGUL_START <= code <= self.HANGUL_END:
                counts["ko"] += 1
                total_chars += 1
            elif self.CJK_START <= code <= self.CJK_END:
                counts["han"] += 1
                total_chars += 1
            elif char.isascii() and char.isalpha():
                counts["latin"] += 1
                total_chars += 1

        if total_chars == 0:
            return "unknown", 0.0

        for key in ("th", "ja", "ko"):
            ratio = counts[key] / total_chars
            if ratio >= self.min_confidence:
                return key, min(ratio, 1.0)

        han_ratio = counts["han"] / total_chars
        if han_ratio >= self.min_confidence:
            simplified_hits = sum(1 for char in text if char in self.SIMPLIFIED_HINTS)
            traditional_hits = sum(1 for char in text if char in self.TRADITIONAL_HINTS)
            if traditional_hits > simplified_hits:
                return "zh-Hant", min(1.0, han_ratio + 0.1)
            if simplified_hits > traditional_hits:
                return "zh-Hans", min(1.0, han_ratio + 0.1)
            return "zh", han_ratio

        latin_ratio = counts["latin"] / total_chars
        if latin_ratio >= self.min_confidence:
            return "en", min(latin_ratio, 1.0)

        mixed_ratio = max(counts.values()) / total_chars
        return "other", min(mixed_ratio, 1.0)


# MARK: - Simple Text Cleaner
class SimpleTextCleaner:
    def __init__(
        self,
        remove_urls: bool = True,
        remove_mentions: bool = True,
        collapse_repeats: bool = True,
    ):
        self.remove_urls = remove_urls
        self.remove_mentions = remove_mentions
        self.collapse_repeats = collapse_repeats
    
    def clean(self, text: str) -> str:
        if not text:
            return ""
        
        result = text
        
        if self.remove_urls:
            import re
            result = re.sub(r'https?://\S+', '', result)
            result = re.sub(r'discord\.gg/\S+', '', result)
        
        if self.remove_mentions:
            import re
            result = re.sub(r'<@\d+>', '', result)
            result = re.sub(r'<@!\d+>', '', result)
            result = re.sub(r'<#\d+>', '', result)
            result = re.sub(r'@everyone', '', result)
            result = re.sub(r'@here', '', result)
        
        if self.collapse_repeats:
            import re
            result = re.sub(r'(.)\1{2,}', r'\1\1', result)
        
        return result.strip()


# MARK: - Message Deduplicator
class MessageDeduplicator:
    def compute_hash(self, content: str) -> str:
        if not content:
            return ""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:64]
    
    def compute_quality_score(
        self,
        content: str,
        cleaned_text: Optional[str] = None,
        tokens: Optional[List[str]] = None,
    ) -> float:
        if not content:
            return 0.0
        
        score = 1.0
        
        # Penalize short messages
        if len(content) < 10:
            score *= 0.5
        elif len(content) < 30:
            score *= 0.8
        
        # Penalize too many URLs
        import re
        url_count = len(re.findall(r'https?://\S+', content))
        if url_count > 2:
            score *= 0.7
        
        # Penalize too many mentions
        mention_count = len(re.findall(r'<@\d+>', content))
        if mention_count > 5:
            score *= 0.8
        
        return max(0.0, min(1.0, score))


# MARK: - Collector Client
class DiscordCollector(discord.Client):
    """Collect Discord messages and store normalized events."""

    # MARK: - Init
    def __init__(self, config: dict, db: Database):
        self.config = config
        self.db = db

        targets = config.get("targets", {})
        guild_ids = targets.get("guild_ids", [])
        channel_ids = targets.get("channel_ids", [])

        self.target_guilds: Optional[Set[int]] = self._parse_id_set(guild_ids)
        self.target_channels: Optional[Set[int]] = self._parse_id_set(channel_ids)
        self.target_regions: List[dict[str, Any]] = list(targets.get("regions", []) or [])
        self.channel_registry: Dict[int, dict[str, Any]] = {
            int(channel_id): dict(metadata)
            for channel_id, metadata in (targets.get("channel_registry", {}) or {}).items()
            if str(channel_id).strip()
        }

        collector_cfg = config.get("collector", {})
        backfill_cfg = collector_cfg.get("backfill", {})
        self.backfill_enabled = self._to_bool(backfill_cfg.get("enabled", False))
        self.backfill_limit_per_channel = self._to_int(backfill_cfg.get("limit_per_channel", 0), default=0)
        self.backfill_oldest_first = self._to_bool(backfill_cfg.get("oldest_first", True))
        self._backfill_started = False

        processor_cfg = config.get("processor", {})
        self.detector = SimpleLangDetector(
            min_confidence=float(processor_cfg.get("language_threshold", 0.2))
        )
        self.cleaner = SimpleTextCleaner(
            remove_urls=processor_cfg.get("remove_urls", True),
            remove_mentions=processor_cfg.get("remove_mentions", True),
            collapse_repeats=processor_cfg.get("collapse_repeats", True),
        )
        self.min_text_length = int(processor_cfg.get("min_text_length", 1))
        self.deduplicator = MessageDeduplicator()

        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True

        discord_cfg = config.get("discord", {})
        proxy_url = (
            str(discord_cfg.get("proxy") or "").strip()
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("HTTP_PROXY")
            or os.getenv("http_proxy")
            or None
        )

        super().__init__(intents=intents, proxy=proxy_url)

        self.metrics: Dict[str, object] = {
            "received": 0,
            "stored": 0,
            "errors": 0,
            "last_message_at": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "backfill_channels": 0,
            "backfill_messages": 0,
            "backfill_done": False,
        }
        self._status_every_n_messages = 50

    # MARK: - Config Parsing
    def _parse_id_set(self, value: object) -> Optional[Set[int]]:
        parsed: Set[int] = set()
        if value is None:
            return None
        if isinstance(value, list):
            for x in value:
                text = str(x).strip()
                if not text:
                    continue
                try:
                    parsed.add(int(text))
                except Exception:
                    continue
            return parsed or None
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    json_items = json.loads(raw)
                    if isinstance(json_items, list):
                        for x in json_items:
                            try:
                                parsed.add(int(str(x).strip()))
                            except Exception:
                                continue
                        return parsed or None
                except Exception:
                    raw = raw[1:-1]
            parts = [x.strip() for x in raw.split(",") if x.strip()]
            for x in parts:
                try:
                    parsed.add(int(x))
                except Exception:
                    continue
            return parsed or None
        return None

    def _to_bool(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(value, int):
            return value != 0
        return bool(value)

    def _to_int(self, value: object, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _format_target_groups(self) -> list[str]:
        if not self.target_regions:
            return []

        lines: list[str] = []
        for region in self.target_regions:
            region_name = str(region.get("name") or region.get("key") or "Region").strip()
            lines.append(region_name)
            for channel in region.get("channels") or []:
                channel_id = channel.get("id")
                channel_name = str(channel.get("name") or f"channel {channel_id}").strip()
                lines.append(f"  - {channel_name} ({channel_id})")
        return lines

    def _infer_channel_group(self, channel_name: str) -> str:
        lowered = channel_name.strip().lower()
        if any(token in lowered for token in ["bug", "报错", "report-bugs", "error"]):
            return "bug"
        if any(token in lowered for token in ["反馈", "suggest", "feedback"]):
            return "feedback"
        if "nsfw" in lowered:
            return "nsfw"
        return "chat"

    def _channel_id(self, channel: object) -> Optional[int]:
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return None
        try:
            return int(channel_id)
        except Exception:
            return None

    def _is_thread_channel(self, channel: object) -> bool:
        if isinstance(channel, discord.Thread):
            return True
        channel_type = getattr(channel, "type", None)
        return channel_type in {
            getattr(discord.ChannelType, "news_thread", None),
            getattr(discord.ChannelType, "public_thread", None),
            getattr(discord.ChannelType, "private_thread", None),
        } or bool(getattr(channel, "is_thread", False))

    def _parent_channel_id(self, channel: object) -> Optional[int]:
        if not self._is_thread_channel(channel):
            return None
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is None:
            parent = getattr(channel, "parent", None)
            parent_id = getattr(parent, "id", None)
        if parent_id is None:
            return None
        try:
            return int(parent_id)
        except Exception:
            return None

    def _message_target_channel_ids(self, message: discord.Message) -> Set[int]:
        ids: Set[int] = set()
        channel_id = self._channel_id(message.channel)
        parent_id = self._parent_channel_id(message.channel)
        if channel_id is not None:
            ids.add(channel_id)
        if parent_id is not None:
            ids.add(parent_id)
        return ids

    def _storage_channel_id_for_channel(self, channel: object) -> int:
        parent_id = self._parent_channel_id(channel)
        channel_id = self._channel_id(channel)
        if parent_id is not None:
            return parent_id
        return int(channel_id or 0)

    def _message_storage_channel_id(self, message: discord.Message) -> int:
        return self._storage_channel_id_for_channel(message.channel)

    def _channel_display_name(self, channel: object, channel_id: int) -> str:
        parent = getattr(channel, "parent", None)
        if parent is not None and self._parent_channel_id(channel) == channel_id:
            parent_name = getattr(parent, "name", "")
            if parent_name:
                return str(parent_name).strip()
        channel_name = getattr(channel, "name", "")
        return str(channel_name or f"channel {channel_id}").strip()

    def _message_has_media_context(self, message: discord.Message) -> bool:
        if list(getattr(message, "attachments", []) or []):
            return True
        return bool(list(getattr(message, "embeds", []) or []))

    def _attachment_urls(self, message: discord.Message) -> list[str]:
        urls: list[str] = []
        for attachment in list(getattr(message, "attachments", []) or []):
            url = (
                getattr(attachment, "url", None)
                or getattr(attachment, "proxy_url", None)
                or getattr(attachment, "filename", None)
            )
            if url:
                urls.append(str(url))
        return urls

    def _embed_summaries(self, message: discord.Message) -> list[str]:
        summaries: list[str] = []
        for embed in list(getattr(message, "embeds", []) or []):
            parts = [
                str(value).strip()
                for value in [
                    getattr(embed, "title", None),
                    getattr(embed, "url", None),
                ]
                if value
            ]
            if parts:
                summaries.append(" | ".join(parts))
        return summaries

    def _message_content_for_storage(self, message: discord.Message) -> str:
        parts: list[str] = []
        thread_title = str(getattr(message.channel, "name", "") or "").strip()
        if self._parent_channel_id(message.channel) is not None and thread_title:
            parts.append(f"Post title: {thread_title}")

        content = str(getattr(message, "content", "") or "").strip()
        if content:
            parts.append(content)

        for url in self._attachment_urls(message):
            parts.append(f"Attachment: {url}")
        for summary in self._embed_summaries(message):
            parts.append(f"Embed: {summary}")

        return "\n".join(parts).strip()

    def _is_forum_like_channel(self, channel: object) -> bool:
        forum_channel = getattr(discord, "ForumChannel", None)
        if forum_channel is not None and isinstance(channel, forum_channel):
            return True
        media_channel = getattr(discord, "MediaChannel", None)
        if media_channel is not None and isinstance(channel, media_channel):
            return True
        channel_type = getattr(channel, "type", None)
        return channel_type in {
            getattr(discord.ChannelType, "forum", None),
            getattr(discord.ChannelType, "media", None),
        }

    def _is_history_channel(self, channel: object) -> bool:
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return True
        return callable(getattr(channel, "history", None)) and not self._is_forum_like_channel(channel)

    async def _expand_forum_threads(self, forum_channel: object) -> list[discord.Thread]:
        threads: list[discord.Thread] = []
        seen: set[int] = set()

        def add_thread(thread: object) -> None:
            thread_id = self._channel_id(thread)
            if thread_id is None or thread_id in seen:
                return
            seen.add(thread_id)
            threads.append(thread)

        active_threads = getattr(forum_channel, "threads", []) or []
        if isinstance(active_threads, dict):
            active_threads = active_threads.values()
        for thread in active_threads:
            add_thread(thread)

        archived_threads = getattr(forum_channel, "archived_threads", None)
        if callable(archived_threads):
            try:
                async for thread in archived_threads(limit=None):
                    add_thread(thread)
            except TypeError:
                async for thread in archived_threads():
                    add_thread(thread)

        return threads

    async def _resolve_channel(self, channel_id: int) -> object | None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                channel = None
        return channel

    # MARK: - Filtering
    def _should_process(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        raw_content = str(getattr(message, "content", "") or "")
        has_text = len(raw_content.strip()) >= self.min_text_length
        if not has_text and not self._message_has_media_context(message):
            return False

        if self.target_guilds is not None:
            if message.guild is None or message.guild.id not in self.target_guilds:
                return False

        if self.target_channels is not None and not (self._message_target_channel_ids(message) & self.target_channels):
            return False

        return True

    # MARK: - Transform
    def _build_payload(self, message: discord.Message, event_type: str = "create") -> Optional[dict]:
        if not self._should_process(message):
            return None

        storage_content = self._message_content_for_storage(message)
        if not storage_content:
            return None

        lang, confidence = self.detector.detect(storage_content)
        
        cleaned_text = self.cleaner.clean(storage_content)
        tokens = []

        content_hash = self.deduplicator.compute_hash(cleaned_text or storage_content)
        quality_score = self.deduplicator.compute_quality_score(
            content=storage_content,
            cleaned_text=cleaned_text,
            tokens=tokens,
        )

        created_at = message.created_at or datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)
        channel_id = self._message_storage_channel_id(message)
        channel_meta = self.channel_registry.get(channel_id, {})
        channel_name = str(
            channel_meta.get("channel_name")
            or self._channel_display_name(message.channel, channel_id)
            or f"channel {channel_id}"
        ).strip()
        region_key = str(channel_meta.get("region_key") or "default").strip()
        region_name = str(channel_meta.get("region_name") or "Default").strip()
        channel_group = str(channel_meta.get("channel_group") or self._infer_channel_group(channel_name)).strip()
        scope_key = f"{region_key}:{channel_id}"

        return {
            "message_id": int(message.id),
            "guild_id": int(message.guild.id if message.guild else 0),
            "channel_id": channel_id,
            "author_id": int(message.author.id),
            "region_key": region_key,
            "region_name": region_name,
            "channel_name": channel_name,
            "channel_group": channel_group,
            "scope_key": scope_key,
            "content": storage_content,
            "detected_language": lang,
            "detected_language_confidence": float(confidence),
            "cleaned_text": cleaned_text,
            "tokens": tokens,
            "event_type": event_type,
            "is_deleted": False,
            "content_hash": content_hash,
            "is_duplicate": False,
            "quality_score": quality_score,
            "created_at": created_at,
            "updated_at": now,
            "deleted_at": None,
        }

    # MARK: - Event Hooks
    async def on_ready(self) -> None:
        print(f"[bot] ready user={self.user} guilds={len(self.guilds)} targets={len(self.target_channels or []) or 'ALL'}")
        for line in self._format_target_groups():
            print(f"[targets] {line}")
        if self.target_channels is None:
            print("[targets] ALL visible channels")
        print(f"[bot] backfill={'on' if self.backfill_enabled else 'off'} limit={self.backfill_limit_per_channel}")
        if self.backfill_enabled and not self._backfill_started:
            self._backfill_started = True
            asyncio.create_task(self._run_backfill())

    # MARK: - Backfill
    async def _run_backfill(self) -> None:
        channels = await self._resolve_backfill_channels()
        self.metrics["backfill_channels"] = len(channels)

        total = 0
        history_limit: Optional[int] = None if self.backfill_limit_per_channel <= 0 else self.backfill_limit_per_channel

        for channel in channels:
            channel_new = 0
            channel_id = self._storage_channel_id_for_channel(channel)
            channel_meta = self.channel_registry.get(channel_id, {})
            region_name = str(channel_meta.get("region_name") or "Default").strip()
            channel_name = str(channel_meta.get("channel_name") or self._channel_display_name(channel, channel_id)).strip()
            try:
                async for message in channel.history(limit=history_limit, oldest_first=self.backfill_oldest_first):
                    payload = self._build_payload(message, event_type="create")
                    if payload is None:
                        continue
                    inserted = self.db.upsert_message(payload)
                    if inserted:
                        total += 1
                        channel_new += 1
            except Exception as exc:
                self.metrics["errors"] = int(self.metrics["errors"]) + 1
                print(f"[backfill] [{region_name}] - [{channel_name}] error={exc}")
            else:
                print(f"[backfill] [{region_name}] - [{channel_name}] +{channel_new}")

        self.metrics["backfill_messages"] = total
        self.metrics["backfill_done"] = True
        print(f"[backfill] done channels={len(channels)} messages={total}")

    async def _fetch_thread_starter_message(self, thread: discord.Thread) -> Optional[discord.Message]:
        fetch_message = getattr(thread, "fetch_message", None)
        if callable(fetch_message):
            try:
                return await fetch_message(int(thread.id))
            except Exception:
                pass

        try:
            async for message in thread.history(limit=1, oldest_first=True):
                return message
        except Exception:
            return None
        return None

    async def _resolve_backfill_channels(self) -> List[discord.abc.Messageable]:
        channels: List[discord.abc.Messageable] = []

        if self.target_channels is not None:
            for cid in self.target_channels:
                channel = await self._resolve_channel(cid)
                if channel is None:
                    continue
                if self._is_forum_like_channel(channel):
                    channels.extend(await self._expand_forum_threads(channel))
                elif self._is_history_channel(channel):
                    channels.append(channel)
            return channels

        guilds = self.guilds
        if self.target_guilds is not None:
            guilds = [g for g in self.guilds if g.id in self.target_guilds]

        for guild in guilds:
            channels.extend(guild.text_channels)
            channels.extend(guild.threads)
            for forum_channel in getattr(guild, "forum_channels", []) or []:
                channels.extend(await self._expand_forum_threads(forum_channel))
            for media_channel in getattr(guild, "media_channels", []) or []:
                channels.extend(await self._expand_forum_threads(media_channel))

        return channels

    # MARK: - Message Events
    async def on_message(self, message: discord.Message) -> None:
        self.metrics["received"] = int(self.metrics["received"]) + 1

        payload = self._build_payload(message, event_type="create")
        if payload is None:
            return

        try:
            inserted = self.db.upsert_message(payload)
            if inserted:
                self.metrics["stored"] = int(self.metrics["stored"]) + 1
                print(
                    f"[collect] +1 [{payload['region_name']}] - [{payload['channel_name']}] - message_id ={message.id}"
                )
            self.metrics["last_message_at"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        payload = self._build_payload(after, event_type="update")
        if payload is None:
            return
        try:
            self.db.upsert_message(payload)
            print(f"[update] [{payload['region_name']}] - [{payload['channel_name']}] - message_id ={after.id}")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1

    async def on_thread_create(self, thread: discord.Thread) -> None:
        starter_message = await self._fetch_thread_starter_message(thread)
        if starter_message is None:
            return

        payload = self._build_payload(starter_message, event_type="create")
        if payload is None:
            return

        try:
            inserted = self.db.upsert_message(payload)
            if inserted:
                self.metrics["stored"] = int(self.metrics["stored"]) + 1
                print(
                    f"[thread] +1 [{payload['region_name']}] - [{payload['channel_name']}] - message_id ={starter_message.id}"
                )
            self.metrics["last_message_at"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1

    async def on_message_delete(self, message: discord.Message) -> None:
        try:
            deleted_at = datetime.now(timezone.utc)
            self.db.mark_deleted(int(message.id), deleted_at=deleted_at)
            print(f"[delete] message_id ={message.id}")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1
