# Collector client for Discord
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import discord

from ..storage import Database


# MARK: - Simple Language Detector
class SimpleLangDetector:
    """Simplified language detection for Thai and other languages."""
    
    THAI_START = 0x0E00
    THAI_END = 0x0E7F
    
    def __init__(self, min_confidence: float = 0.2):
        self.min_confidence = min_confidence
    
    def detect(self, text: str) -> tuple[str, float]:
        if not text:
            return "unknown", 0.0
        
        thai_chars = 0
        total_chars = 0
        
        for char in text:
            code = ord(char)
            if self.THAI_START <= code <= self.THAI_END:
                thai_chars += 1
            if char.isalpha() or char.isdigit():
                total_chars += 1
        
        if total_chars == 0:
            return "unknown", 0.0
        
        ratio = thai_chars / total_chars
        if ratio >= 0.3:
            return "th", min(ratio, 1.0)
        
        return "other", ratio


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

        super().__init__(intents=intents)

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

    # MARK: - Filtering
    def _should_process(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        if not message.content:
            return False
        if len(message.content.strip()) < self.min_text_length:
            return False

        if self.target_guilds is not None:
            if message.guild is None or message.guild.id not in self.target_guilds:
                return False

        if self.target_channels is not None and message.channel.id not in self.target_channels:
            return False

        return True

    # MARK: - Transform
    def _build_payload(self, message: discord.Message, event_type: str = "create") -> Optional[dict]:
        if not self._should_process(message):
            return None

        lang, confidence = self.detector.detect(message.content)
        
        # Configurable target language (default: Thai)
        target_lang = self.config.get("processor", {}).get("target_language", "th")
        is_target = lang == target_lang and confidence >= self.detector.min_confidence

        cleaned_text = None
        tokens = []

        if is_target:
            cleaned_text = self.cleaner.clean(message.content)

        content_hash = self.deduplicator.compute_hash(cleaned_text or message.content)
        quality_score = self.deduplicator.compute_quality_score(
            content=message.content,
            cleaned_text=cleaned_text,
            tokens=tokens,
        )

        created_at = message.created_at or datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)
        channel_id = int(message.channel.id)
        channel_meta = self.channel_registry.get(channel_id, {})
        channel_name = str(
            channel_meta.get("channel_name")
            or getattr(message.channel, "name", "")
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
            "content": message.content,
            "is_target_language": bool(is_target),
            "language": lang,
            "lang_confidence": float(confidence),
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
        print(f"Collector logged in as {self.user} ({self.user.id})")
        print(f"Connected guilds: {len(self.guilds)}")
        print(f"Target guild IDs: {sorted(self.target_guilds) if self.target_guilds else 'ALL'}")
        print(f"Target channel IDs: {sorted(self.target_channels) if self.target_channels else 'ALL'}")
        for line in self._format_target_groups():
            print(f"[collector-targets] {line}")
        if self.target_channels is None:
            print("[collector] no target channel configured; live collection is currently reading ALL visible channels")
        print(f"[collector] backfill enabled={self.backfill_enabled} limit_per_channel={self.backfill_limit_per_channel}")
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
            try:
                async for message in channel.history(limit=history_limit, oldest_first=self.backfill_oldest_first):
                    payload = self._build_payload(message, event_type="create")
                    if payload is None:
                        continue
                    inserted = self.db.upsert_message(payload)
                    if inserted:
                        total += 1
                        channel_new += 1
                        if channel_new % 25 == 0:
                            print(f"[backfill] channel={channel.id} new={channel_new}")
            except Exception as exc:
                self.metrics["errors"] = int(self.metrics["errors"]) + 1
                print(f"[backfill] failed channel={channel.id}: {exc}")
            else:
                print(f"[backfill] channel={channel.id} completed new={channel_new}")

        self.metrics["backfill_messages"] = total
        self.metrics["backfill_done"] = True
        print(f"[backfill] completed channels={len(channels)} messages={total}")

    async def _resolve_backfill_channels(self) -> List[discord.abc.Messageable]:
        channels: List[discord.abc.Messageable] = []

        if self.target_channels is not None:
            for cid in self.target_channels:
                channel = self.get_channel(cid)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(cid)
                    except Exception:
                        channel = None
                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    channels.append(channel)
            return channels

        guilds = self.guilds
        if self.target_guilds is not None:
            guilds = [g for g in self.guilds if g.id in self.target_guilds]

        for guild in guilds:
            channels.extend(guild.text_channels)
            channels.extend(guild.threads)

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
                    f"[collect] +1 new message total_new={self.metrics['stored']} "
                    f"channel={message.channel.id} message_id={message.id}"
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
            print(f"[collect] message updated channel={after.channel.id} message_id={after.id}")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1

    async def on_message_delete(self, message: discord.Message) -> None:
        try:
            deleted_at = datetime.now(timezone.utc)
            self.db.mark_deleted(int(message.id), deleted_at=deleted_at)
            print(f"[collect] message deleted message_id={message.id}")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1
