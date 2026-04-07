from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol

import httpx

from ..storage import Database, DailyReport
from .service import DailyReportService, ReportScope


class FeishuSender(Protocol):
    def send_text(self, text: str) -> dict[str, Any]:
        ...


# These delivery defaults are intentionally fixed in code to keep Feishu setup minimal.
DEFAULT_FEISHU_SCOPE_KEY = "global"
DEFAULT_FEISHU_DAILY_SEND_HOUR = 9
DEFAULT_FEISHU_DAILY_SEND_MINUTE = 0
DEFAULT_FEISHU_MAX_MESSAGE_CHARS = 20_000
DEFAULT_FEISHU_TITLE_PREFIX = "Discord Global 日报"


@dataclass(frozen=True)
class FeishuDeliveryConfig:
    enabled: bool = False
    webhook_url: str = ""
    sign_secret: str = ""
    keyword: str = ""
    scope_key: str = DEFAULT_FEISHU_SCOPE_KEY
    daily_send_hour: int = DEFAULT_FEISHU_DAILY_SEND_HOUR
    daily_send_minute: int = DEFAULT_FEISHU_DAILY_SEND_MINUTE
    max_message_chars: int = DEFAULT_FEISHU_MAX_MESSAGE_CHARS
    title_prefix: str = DEFAULT_FEISHU_TITLE_PREFIX

    @classmethod
    def from_config(cls, raw: Optional[dict[str, Any]]) -> "FeishuDeliveryConfig":
        payload = raw or {}
        return cls(
            enabled=bool(payload.get("enabled", False)),
            webhook_url=str(payload.get("webhook_url") or "").strip(),
            sign_secret=str(payload.get("sign_secret") or "").strip(),
            keyword=str(payload.get("keyword") or "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.webhook_url)

    @property
    def target_key(self) -> str:
        digest = hashlib.sha256(self.webhook_url.encode("utf-8")).hexdigest()[:16]
        return f"feishu:{digest}"


@dataclass(frozen=True)
class FeishuDeliveryResult:
    status: str
    report_date: date
    scope_key: str
    sent_message_count: int = 0
    report_found: bool = False


class FeishuBotClient:
    def __init__(
        self,
        webhook_url: str,
        *,
        sign_secret: str = "",
        timeout: float = 20.0,
    ) -> None:
        if not webhook_url.strip():
            raise ValueError("Feishu webhook URL is required")
        self.webhook_url = webhook_url.strip()
        self.sign_secret = sign_secret.strip()
        self.timeout = timeout

    def _build_payload(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self.sign_secret:
            timestamp = int(datetime.now(tz=timezone.utc).timestamp())
            string_to_sign = f"{timestamp}\n{self.sign_secret}"
            digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(digest).decode("utf-8")
        return payload

    def send_text(self, text: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.webhook_url, json=self._build_payload(text))
        response.raise_for_status()
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {}
        if int(body.get("code") or 0) != 0:
            raise RuntimeError(f"Feishu bot rejected message: {body.get('msg') or body}")
        return body


class DailyReportFeishuNotifier:
    def __init__(
        self,
        *,
        db: Database,
        config: FeishuDeliveryConfig,
        client: Optional[FeishuSender] = None,
    ) -> None:
        self.db = db
        self.config = config
        self.client = client
        if self.client is None and config.is_configured:
            self.client = FeishuBotClient(
                config.webhook_url,
                sign_secret=config.sign_secret,
            )

    def is_enabled(self) -> bool:
        return self.config.is_configured

    def deliver_previous_day_report(
        self,
        service: DailyReportService,
        *,
        now: Optional[datetime] = None,
    ) -> FeishuDeliveryResult:
        report_date, _, _ = service.build_previous_day_window(now=now)
        return self.deliver_report_for_date(service, report_date=report_date)

    def deliver_report_for_date(
        self,
        service: DailyReportService,
        *,
        report_date: date,
    ) -> FeishuDeliveryResult:
        scope_key = self.config.scope_key
        if not self.is_enabled():
            return FeishuDeliveryResult(
                status="disabled",
                report_date=report_date,
                scope_key=scope_key,
            )

        if self.db.get_report_delivery(
            delivery_channel="feishu",
            target_key=self.config.target_key,
            report_date=report_date,
            scope_key=scope_key,
        ) is not None:
            return FeishuDeliveryResult(
                status="already_delivered",
                report_date=report_date,
                scope_key=scope_key,
                report_found=True,
            )

        report = service.get_daily_report(report_date, scope_key=scope_key)
        if report is None:
            report = service.generate_daily_report_from_hourly_reports(
                report_date,
                scope=self._resolve_scope(service, scope_key),
            )

        messages = self._build_messages(report)
        if self.client is None:
            raise RuntimeError("Feishu notifier is enabled but no sender client is available")
        for message in messages:
            self.client.send_text(message)

        self.db.upsert_report_delivery(
            {
                "delivery_channel": "feishu",
                "target_key": self.config.target_key,
                "report_date": report_date,
                "scope_key": scope_key,
                "message_count": len(messages),
                "detail": f"feishu:{self.config.title_prefix}",
                "delivered_at": datetime.now(tz=timezone.utc),
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        return FeishuDeliveryResult(
            status="sent",
            report_date=report_date,
            scope_key=scope_key,
            sent_message_count=len(messages),
            report_found=True,
        )

    def _resolve_scope(self, service: DailyReportService, scope_key: str) -> ReportScope:
        for scope in service.scopes:
            if scope.scope_key == scope_key:
                return scope
        if scope_key == "global":
            return ReportScope(scope_type="global", scope_key="global")
        raise RuntimeError(f"Configured Feishu scope_key is not available: {scope_key}")

    def _build_messages(self, report: DailyReport) -> list[str]:
        title = f"{self.config.title_prefix} {report.report_date.isoformat()}"
        summary_lines = [
            f"范围：{report.scope_key}",
            f"消息数：{int(report.source_message_count or 0)}",
            f"候选消息数：{int(report.candidate_message_count or 0)}",
        ]
        body = str(report.content_cn or report.content or "").strip()
        if not body:
            body = "日报内容为空。"

        prefix = f"{self.config.keyword} " if self.config.keyword else ""
        first_header = f"{prefix}{title}\n" + "\n".join(summary_lines)
        chunk_bodies = self._split_body(body, header_len=len(first_header))
        if len(chunk_bodies) == 1:
            return [f"{first_header}\n\n{chunk_bodies[0]}"]

        total = len(chunk_bodies)
        messages: list[str] = []
        for index, chunk in enumerate(chunk_bodies, start=1):
            if index == 1:
                header = f"{prefix}{title}（1/{total}）\n" + "\n".join(summary_lines)
            else:
                header = f"{prefix}{title}（{index}/{total}）"
            messages.append(f"{header}\n\n{chunk}")
        return messages

    def _split_body(self, body: str, *, header_len: int) -> list[str]:
        budget = max(200, self.config.max_message_chars - header_len - 24)
        remaining = body.strip()
        chunks: list[str] = []

        while remaining:
            if len(remaining) <= budget:
                chunks.append(remaining)
                break

            split_at = remaining.rfind("\n\n", 0, budget)
            if split_at <= 0:
                split_at = remaining.rfind("\n", 0, budget)
            if split_at <= 0:
                split_at = budget

            chunk = remaining[:split_at].strip()
            if not chunk:
                chunk = remaining[:budget].strip()
                split_at = budget
            chunks.append(chunk)
            remaining = remaining[split_at:].lstrip()

        return chunks or [body]
