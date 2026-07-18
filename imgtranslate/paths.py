"""Project root and default data / runtime paths."""

from __future__ import annotations

from pathlib import Path

# imgtranslate/ → project root
ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
SOURCE_DIR = DATA_DIR / "source"
TRANSLATED_DIR = DATA_DIR / "translated"

RUNTIME_DIR = ROOT / "runtime"
LOGS_DIR = RUNTIME_DIR / "logs"
FAILURES_DIR = LOGS_DIR / "failures"
QUEUE_DIR = RUNTIME_DIR / "queue"
LOCKS_DIR = QUEUE_DIR / "locks"
COOLDOWN_DIR = QUEUE_DIR / "cooldown"
PROFILES_DIR = RUNTIME_DIR / "browser-profiles"
DEFAULT_PROFILE = RUNTIME_DIR / "browser-profile"

CONFIG_PATH = ROOT / "config.json"
CONFIG_EXAMPLE_PATH = ROOT / "config.example.json"
PROXY_FILE = ROOT / "proxy.txt"
PROXY_EXAMPLE_FILE = ROOT / "proxy.example.txt"
