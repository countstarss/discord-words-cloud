# Configuration loader
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
import yaml

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}$")
_ENV_ALIASES = {
    "DISCORD_GUILD_IDS": ("TARGET_GUILD_IDS",),
    "DISCORD_CHANNEL_IDS": ("TARGET_CHANNEL_IDS",),
}


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


# MARK: - Main
def load_config(config_path: Optional[str] = None) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    _apply_env_aliases()
    default_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    path = Path(config_path) if config_path else default_path
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _resolve_value(raw)
