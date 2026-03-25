# Configuration loader
from __future__ import annotations

import os
import re
from io import StringIO
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values
import yaml

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}$")
_ENV_ALIASES = {
    "DISCORD_GUILD_IDS": ("TARGET_GUILD_IDS",),
    "DISCORD_CHANNEL_IDS": ("TARGET_CHANNEL_IDS",),
    "DISCORD_REGION_CHANNELS": ("TARGET_REGION_CHANNELS",),
}
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_MULTILINE_LITERAL_KEYS = {"DISCORD_REGION_CHANNELS", "TARGET_REGION_CHANNELS"}


# MARK: - Resolver
def _parse_default_literal(default: Optional[str]) -> Any:
    if default is None:
        return None
    if default == "":
        return ""
    try:
        return yaml.safe_load(default)
    except Exception:
        return default


def _coerce_env_value(raw: str, default: Optional[str]) -> Any:
    template = _parse_default_literal(default)
    if template is None:
        return raw
    if isinstance(template, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return template
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(raw)
        except Exception:
            return template
    if isinstance(template, float):
        try:
            return float(raw)
        except Exception:
            return template
    if isinstance(template, (list, dict)):
        try:
            parsed = yaml.safe_load(raw)
        except Exception:
            return template
        return parsed if isinstance(parsed, type(template)) else template
    return raw


def _resolve_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v) for v in value]
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value)
        if match:
            env_key, default = match.groups()
            env_value = os.getenv(env_key)
            if env_value is not None:
                return _coerce_env_value(env_value, default)
            parsed_default = _parse_default_literal(default)
            return "" if parsed_default is None else parsed_default
        return value
    return value


def _apply_env_aliases() -> None:
    for canonical_key, aliases in _ENV_ALIASES.items():
        if os.getenv(canonical_key):
            continue
        for alias in aliases:
            alias_value = os.getenv(alias)
            if alias_value:
                os.environ[canonical_key] = alias_value
                break


def _literal_balance_delta(value: str) -> int:
    return value.count("[") - value.count("]")


def _load_project_env(env_path: Path, override: bool = False) -> None:
    if not env_path.exists():
        return

    raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    normal_lines: list[str] = []
    custom_values: dict[str, str] = {}
    index = 0

    while index < len(raw_lines):
        line = raw_lines[index]
        match = _ENV_ASSIGNMENT_PATTERN.match(line)
        if not match:
            normal_lines.append(line)
            index += 1
            continue

        key, raw_value = match.groups()
        stripped_value = raw_value.strip()
        if key in _MULTILINE_LITERAL_KEYS and stripped_value.startswith("[") and _literal_balance_delta(stripped_value) > 0:
            collected = [raw_value]
            balance = _literal_balance_delta(raw_value)
            index += 1
            while index < len(raw_lines) and balance > 0:
                next_line = raw_lines[index]
                collected.append(next_line)
                balance += _literal_balance_delta(next_line)
                index += 1
            custom_values[key] = "\n".join(collected).strip()
            continue

        normal_lines.append(line)
        index += 1

    parsed_values = dotenv_values(stream=StringIO("\n".join(normal_lines)))
    for key, value in parsed_values.items():
        if value is None:
            continue
        if override or key not in os.environ:
            os.environ[key] = value

    for key, value in custom_values.items():
        if override or key not in os.environ:
            os.environ[key] = value


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(str(value).strip())
    except Exception:
        return None


def _coerce_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[int] = []
        for item in value:
            coerced = _coerce_int(item)
            if coerced is not None:
                result.append(coerced)
        return result
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = yaml.safe_load(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return _coerce_int_list(parsed)
        return _coerce_int_list([part.strip() for part in raw.split(",") if part.strip()])
    coerced = _coerce_int(value)
    return [coerced] if coerced is not None else []


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def _normalize_targets_config(config: dict[str, Any]) -> dict[str, Any]:
    targets = config.setdefault("targets", {})
    raw_regions = targets.get("regions") or []
    flat_guild_ids = set(_coerce_int_list(targets.get("guild_ids")))
    flat_channel_ids = set(_coerce_int_list(targets.get("channel_ids")))
    normalized_regions: list[dict[str, Any]] = []
    channel_registry: dict[int, dict[str, Any]] = {}

    if isinstance(raw_regions, list) and raw_regions:
        for index, raw_region in enumerate(raw_regions, start=1):
            if not isinstance(raw_region, dict):
                continue

            region_name = str(raw_region.get("name") or f"Region {index}").strip()
            region_key = str(raw_region.get("key") or _slugify(region_name, f"region-{index}"))
            region_guild_ids = sorted(
                set(
                    _coerce_int_list(raw_region.get("guild_ids"))
                    + _coerce_int_list(raw_region.get("guild_id"))
                )
            )
            normalized_channels: list[dict[str, Any]] = []

            for channel_index, raw_channel in enumerate(raw_region.get("channels") or [], start=1):
                if isinstance(raw_channel, dict):
                    channel_id = _coerce_int(raw_channel.get("id") or raw_channel.get("channel_id"))
                    channel_name = str(
                        raw_channel.get("name")
                        or raw_channel.get("title")
                        or (f"channel {channel_id}" if channel_id is not None else f"channel-{channel_index}")
                    ).strip()
                    channel_group = str(raw_channel.get("group") or raw_channel.get("kind") or "").strip()
                    channel_guild_ids = sorted(
                        set(
                            _coerce_int_list(raw_channel.get("guild_ids"))
                            + _coerce_int_list(raw_channel.get("guild_id"))
                        )
                    ) or list(region_guild_ids)
                else:
                    channel_id = _coerce_int(raw_channel)
                    channel_name = f"channel {channel_id}" if channel_id is not None else f"channel-{channel_index}"
                    channel_group = ""
                    channel_guild_ids = list(region_guild_ids)

                if channel_id is None:
                    continue

                channel_payload = {
                    "id": channel_id,
                    "name": channel_name,
                    "group": channel_group,
                    "guild_ids": channel_guild_ids,
                }
                normalized_channels.append(channel_payload)
                channel_registry[channel_id] = {
                    "region_key": region_key,
                    "region_name": region_name,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "channel_group": channel_group,
                    "guild_ids": channel_guild_ids,
                }
                flat_channel_ids.add(channel_id)
                flat_guild_ids.update(channel_guild_ids)

            normalized_regions.append(
                {
                    "key": region_key,
                    "name": region_name,
                    "guild_ids": region_guild_ids,
                    "channels": normalized_channels,
                }
            )

    if not normalized_regions and (flat_guild_ids or flat_channel_ids):
        default_channels = []
        for channel_id in sorted(flat_channel_ids):
            default_channels.append(
                {
                    "id": channel_id,
                    "name": f"channel {channel_id}",
                    "group": "",
                    "guild_ids": sorted(flat_guild_ids),
                }
            )
            channel_registry[channel_id] = {
                "region_key": "default",
                "region_name": "Default",
                "channel_id": channel_id,
                "channel_name": f"channel {channel_id}",
                "channel_group": "",
                "guild_ids": sorted(flat_guild_ids),
            }
        normalized_regions.append(
            {
                "key": "default",
                "name": "Default",
                "guild_ids": sorted(flat_guild_ids),
                "channels": default_channels,
            }
        )

    targets["guild_ids"] = sorted(flat_guild_ids)
    targets["channel_ids"] = sorted(flat_channel_ids)
    targets["regions"] = normalized_regions
    targets["channel_registry"] = channel_registry
    return config


# MARK: - Main
def load_config(config_path: Optional[str] = None) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    _load_project_env(project_root / ".env", override=False)
    _apply_env_aliases()
    default_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    path = Path(config_path) if config_path else default_path
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    resolved = _resolve_value(raw)
    return _normalize_targets_config(resolved)
