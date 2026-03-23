from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import discord

from ..aggregator.dedup import MessageDeduplicator
from ..processor import LanguageDetector, TextCleaner, ThaiTokenizer
from ..storage import Database


# MARK: - Collector Client
# 采集职责：
# - 实时监听 Discord 消息事件（创建/编辑/删除）
# - 进行轻量预处理（语言识别、清洗、分词）
# - 幂等写入数据库并上报采集状态
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

        collector_cfg = config.get("collector", {})
        backfill_cfg = collector_cfg.get("backfill", {})
        self.backfill_enabled = self._to_bool(backfill_cfg.get("enabled", True))
        self.backfill_limit_per_channel = self._to_int(backfill_cfg.get("limit_per_channel", 0), default=0)
        self.backfill_oldest_first = self._to_bool(backfill_cfg.get("oldest_first", True))
        self._backfill_started = False

        processor_cfg = config.get("processor", {})
        self.detector = LanguageDetector(min_confidence=float(processor_cfg.get("language_threshold", 0.2)))
        self.tokenizer = ThaiTokenizer(
            engine=processor_cfg.get("tokenizer_engine", "newmm"),
            keep_whitespace=False,
        )
        self.cleaner = TextCleaner(
            remove_urls=True,
            remove_mentions=True,
            collapse_repeats=True,
            min_token_length=int(processor_cfg.get("min_token_length", 1)),
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
    # 支持 list 或逗号分隔字符串两种 target 配置格式。
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

            # 兼容常见配置写法：
            # - "123,456"
            # - "[123,456]"（用户误把列表写成字符串）
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
            return int(value)  # type: ignore[arg-type]
        except Exception:
            return default

    # MARK: - Filtering
    # 仅保留目标频道/服务器中的可分析文本消息。
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
    # 将 Discord Message 转换成数据库可写入结构。
    # 注意：非泰语消息也会入库，用于后续统计基线。
    def _build_payload(self, message: discord.Message, event_type: str = "create") -> Optional[dict]:
        if not self._should_process(message):
            return None

        lang, confidence = self.detector.detect(message.content)
        is_thai = lang == "th" and confidence >= self.detector.min_confidence

        cleaned_text = None
        tokens = []

        if is_thai:
            cleaned_text = self.cleaner.clean(message.content)
            tokens = self.tokenizer.tokenize(cleaned_text)

        # V2: 计算内容指纹和质量评分
        content_hash = self.deduplicator.compute_hash(cleaned_text or message.content)
        quality_score = self.deduplicator.compute_quality_score(
            content=message.content,
            cleaned_text=cleaned_text,
            tokens=tokens,
        )

        created_at = message.created_at or datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)

        return {
            "message_id": int(message.id),
            "guild_id": int(message.guild.id if message.guild else 0),
            "channel_id": int(message.channel.id),
            "author_id": int(message.author.id),
            "content": message.content,
            "is_thai": bool(is_thai),
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
        self._push_status("running")
        if self.backfill_enabled and not self._backfill_started:
            self._backfill_started = True
            asyncio.create_task(self._run_backfill())

    # MARK: - Status
    # 状态写入失败时不应阻塞采集主循环。
    def _push_status(self, state: str) -> None:
        try:
            self.db.upsert_service_status(
                service_name="collector",
                status={
                    "state": state,
                    "metrics": self.metrics,
                    "target_guild_count": len(self.target_guilds or []),
                    "target_channel_count": len(self.target_channels or []),
                },
            )
        except Exception:
            # Keep collector loop alive even if status write fails.
            pass

    # MARK: - Backfill
    # 启动后对目标频道执行历史回填，保证“历史 + 实时”两条链路完整。
    async def _run_backfill(self) -> None:
        channels = await self._resolve_backfill_channels()
        self.metrics["backfill_channels"] = len(channels)
        self._push_status("backfill_running")

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
                self._push_status("degraded")
                print(f"[backfill] failed channel={channel.id}: {exc}")
            else:
                print(f"[backfill] channel={channel.id} completed new={channel_new}")

        self.metrics["backfill_messages"] = total
        self.metrics["backfill_done"] = True
        self._push_status("running")
        print(f"[backfill] completed channels={len(channels)} messages={total}")

    async def _resolve_backfill_channels(self) -> List[discord.abc.Messageable]:
        channels: List[discord.abc.Messageable] = []

        # 优先使用显式 channel_ids，最精准。
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

        # 若只指定 guild_ids，则抓这些 guild 下所有文本频道。
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
            if int(self.metrics["received"]) % self._status_every_n_messages == 0:
                self._push_status("running")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1
            self._push_status("degraded")

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        payload = self._build_payload(after, event_type="update")
        if payload is None:
            return
        try:
            self.db.upsert_message(payload)
            print(f"[collect] message updated channel={after.channel.id} message_id={after.id}")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1
            self._push_status("degraded")

    async def on_message_delete(self, message: discord.Message) -> None:
        try:
            deleted_at = datetime.now(timezone.utc)
            self.db.mark_deleted(int(message.id), deleted_at=deleted_at)
            print(f"[collect] message deleted message_id={message.id}")
        except Exception:
            self.metrics["errors"] = int(self.metrics["errors"]) + 1
            self._push_status("degraded")
