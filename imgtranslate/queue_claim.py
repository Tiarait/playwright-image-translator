"""Atomic job claims for multi-worker translation."""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from .paths import COOLDOWN_DIR, LOCKS_DIR

DEFAULT_LOCKS_DIR = LOCKS_DIR
DEFAULT_COOLDOWN_DIR = COOLDOWN_DIR

# Keep lock filenames filesystem-safe
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def lock_name(rel: str | Path) -> str:
    """Stable unique name: readable prefix + hash (avoids collisions after sanitizing)."""
    text = Path(rel).as_posix()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    safe = _SAFE.sub("_", text).strip("._-") or "job"
    return f"{safe[:100]}__{digest}.lock"


def locks_dir(path: Path | None = None) -> Path:
    d = path or DEFAULT_LOCKS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def cooldown_dir(path: Path | None = None) -> Path:
    d = path or DEFAULT_COOLDOWN_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def in_cooldown(
    rel: str | Path, cooldown_sec: int, cool_dir: Path | None = None
) -> bool:
    if cooldown_sec <= 0:
        return False
    path = cooldown_dir(cool_dir) / lock_name(rel)
    if not path.exists():
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age > cooldown_sec:
        try:
            path.unlink()
        except OSError:
            pass
        return False
    return True


def set_cooldown(
    rel: str | Path, worker_id: str, cool_dir: Path | None = None
) -> Path:
    path = cooldown_dir(cool_dir) / lock_name(rel)
    path.write_text(
        f"{worker_id}\n{time.time():.3f}\n{Path(rel).as_posix()}\n",
        encoding="utf-8",
    )
    return path


def clear_cooldown(rel: str | Path, cool_dir: Path | None = None) -> None:
    path = cooldown_dir(cool_dir) / lock_name(rel)
    try:
        path.unlink()
    except OSError:
        pass


def claim(
    rel: str | Path,
    worker_id: str,
    locks: Path | None = None,
    stale_sec: int = 900,
    cooldown_sec: int = 180,
    cool_dir: Path | None = None,
) -> bool:
    """Atomically claim a job. Returns True if this worker owns it."""
    if in_cooldown(rel, cooldown_sec, cool_dir):
        return False

    path = locks_dir(locks) / lock_name(rel)
    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > stale_sec:
            try:
                path.unlink()
            except OSError:
                return False
        else:
            return False

    payload = f"{worker_id}\n{time.time():.3f}\n{Path(rel).as_posix()}\n".encode()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    clear_cooldown(rel, cool_dir)
    return True


def release(rel: str | Path, locks: Path | None = None) -> None:
    path = locks_dir(locks) / lock_name(rel)
    try:
        path.unlink()
    except OSError:
        pass


def touch(rel: str | Path, locks: Path | None = None) -> None:
    """Refresh mtime so long jobs are not treated as stale."""
    path = locks_dir(locks) / lock_name(rel)
    try:
        path.touch()
    except OSError:
        pass


def list_locks(locks: Path | None = None) -> list[Path]:
    d = locks_dir(locks)
    return sorted(d.glob("*.lock"))


def list_cooldowns(cool_dir: Path | None = None) -> list[Path]:
    d = cooldown_dir(cool_dir)
    return sorted(d.glob("*.lock"))


def clear_locks(locks: Path | None = None) -> int:
    """Remove all active claim locks. Returns count removed."""
    count = 0
    for path in list_locks(locks):
        try:
            path.unlink()
            count += 1
        except OSError:
            pass
    return count
