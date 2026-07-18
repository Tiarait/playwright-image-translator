"""Load config.json and use it as argparse defaults (CLI wins)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .paths import CONFIG_PATH, ROOT

# Keys allowed in config.json (CLI flag name without leading --, underscores OK)
KNOWN_KEYS = frozenset(
    {
        "source",
        "translated",
        "logs",
        "profiles",
        "profile",
        "proxy_file",
        "require_proxy",
        "delay",
        "delay_jitter",
        "timeout",
        "limit",
        "only",
        "sl",
        "tl",
        "headless",
        "quiet",
        "verbose",
        "no_fail_shots",
        "fail_shots",
        "fail_shot_quality",
        "workers",
        "stale_lock",
        "reload_every",
        "download_settle",
        "fail_cooldown",
    }
)

PATH_KEYS = frozenset(
    {"source", "translated", "logs", "profiles", "profile", "proxy_file"}
)


def _normalize_key(key: str) -> str:
    return key.strip().lstrip("-").replace("-", "_")


def resolve_path(value: str | Path, base: Path = ROOT) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    else:
        path = path.resolve()
    return path


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load JSON config. Missing file → empty dict. Unknown keys ignored with warning."""
    cfg_path = (path or CONFIG_PATH).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = (ROOT / cfg_path).resolve()
    else:
        cfg_path = cfg_path.resolve()

    if not cfg_path.is_file():
        return {}

    try:
        text = cfg_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"Bad config file {cfg_path}: {exc}") from exc

    if not text:
        return {}

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Bad config file {cfg_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SystemExit(f"Config must be a JSON object: {cfg_path}")

    out: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if key.startswith("_") or key in ("$schema", "comment", "comments"):
            continue
        norm = _normalize_key(str(key))
        if norm not in KNOWN_KEYS:
            unknown.append(str(key))
            continue
        if norm in PATH_KEYS and value is not None and value != "":
            out[norm] = resolve_path(value)
        else:
            out[norm] = value

    if unknown:
        logging.warning("config: ignoring unknown keys: %s", ", ".join(unknown))

    out["_config_path"] = cfg_path
    return out


def cfg_get(cfg: dict[str, Any], key: str, default: Any) -> Any:
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return default
