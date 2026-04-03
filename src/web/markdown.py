from __future__ import annotations

from functools import lru_cache
from typing import Any

try:
    import mistune
except ModuleNotFoundError:
    mistune = None


MARKDOWN_PLUGINS = ("strikethrough", "table", "task_lists", "url")


@lru_cache(maxsize=1)
def _get_renderer():
    if mistune is None:
        return None
    return mistune.create_markdown(escape=True, plugins=list(MARKDOWN_PLUGINS))


def render_markdown_html(markdown_text: str | None) -> str | None:
    renderer = _get_renderer()
    if renderer is None:
        return None

    value = str(markdown_text or "")
    if not value.strip():
        return ""
    return renderer(value)


def enrich_daily_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reports = []
    for report in payload.get("reports", []) or []:
        item = dict(report)
        rendered = render_markdown_html(item.get("content_cn"))
        if rendered is not None:
            item["content_html"] = rendered
        reports.append(item)
    return {**payload, "reports": reports}

