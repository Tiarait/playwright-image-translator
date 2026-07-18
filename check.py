#!/usr/bin/env python3
"""Check source/ → translated/ progress without changing any files."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from imgtranslate import queue_claim
from imgtranslate.config import cfg_get, load_config
from imgtranslate.paths import (
    CONFIG_PATH,
    FAILURES_DIR,
    LOGS_DIR,
    SOURCE_DIR,
    TRANSLATED_DIR,
)
from imgtranslate.qc import quality_issue

IMAGE_RE = re.compile(r"\.(jpe?g|png|webp)$", re.I)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path, default=CONFIG_PATH)
    pre_args, _ = pre.parse_known_args(argv)
    cfg = load_config(pre_args.config)

    parser = argparse.ArgumentParser(
        description="Show image translation progress and file consistency"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="JSON settings (source/translated/logs); CLI overrides",
    )
    parser.add_argument(
        "--source", type=Path, default=cfg_get(cfg, "source", SOURCE_DIR)
    )
    parser.add_argument(
        "--translated",
        type=Path,
        default=cfg_get(cfg, "translated", TRANSLATED_DIR),
    )
    logs_default = cfg_get(cfg, "logs", LOGS_DIR)
    failures_default = Path(logs_default) / "failures"
    parser.add_argument("--failures", type=Path, default=failures_default)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list missing, suspicious, extras, locks and recent failures",
    )
    return parser.parse_args(argv)


def image_files(root: Path) -> dict[Path, Path]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and IMAGE_RE.search(path.name)
    }


def folder_count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_dir())


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def estimate_speed(translated_paths: list[Path], window: int = 20) -> float | None:
    """Average seconds per file from recent translated mtimes. None if not enough data."""
    if len(translated_paths) < 2:
        return None
    mtimes = sorted(p.stat().st_mtime for p in translated_paths)
    recent = mtimes[-(window + 1) :] if len(mtimes) > window + 1 else mtimes
    span = recent[-1] - recent[0]
    count = len(recent) - 1
    if count <= 0 or span <= 0:
        return None
    return span / count


def read_failures(failures_dir: Path) -> list[dict]:
    path = failures_dir / "failures.jsonl"
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def print_paths(title: str, paths: list[Path]) -> None:
    if not paths:
        return
    print(f"\n{title} ({len(paths)}):")
    for path in paths:
        print(f"  - {path.as_posix()}")


def main() -> int:
    args = parse_args()
    source_root = args.source.resolve()
    translated_root = args.translated.resolve()
    failures_dir = args.failures.resolve()

    if not source_root.is_dir():
        print(f"Error: source folder not found: {source_root}")
        return 2

    source = image_files(source_root)
    translated = image_files(translated_root)

    missing: list[Path] = []
    suspicious: list[tuple[Path, str]] = []
    ready: list[Path] = []
    ready_paths: list[Path] = []

    for relative, source_path in source.items():
        output_path = translated_root / relative
        if not output_path.exists():
            missing.append(relative)
            continue
        issue = quality_issue(source_path, output_path)
        if issue is None:
            ready.append(relative)
            ready_paths.append(output_path)
        else:
            suspicious.append((relative, issue))

    extras = sorted(set(translated) - set(source))
    missing.sort()
    suspicious.sort(key=lambda x: x[0].as_posix())
    ready.sort()

    locks = queue_claim.list_locks()
    cooldowns = queue_claim.list_cooldowns()
    total = len(source)
    done = len(ready)
    remaining = len(missing) + len(suspicious)
    progress = (done / total * 100) if total else 100.0
    source_size = sum(path.stat().st_size for path in source.values())
    translated_size = sum(path.stat().st_size for path in translated.values())

    speed = estimate_speed(ready_paths)
    failures = read_failures(failures_dir)

    print("Translation stats")
    print("=================")
    print(f"Source:                 {source_root}")
    print(f"Translated:             {translated_root}")
    print(f"Folders in source:      {folder_count(source_root)}")
    print(f"Images total:           {total}")
    print(f"Done:                   {done}")
    print(f"Remaining:              {remaining}")
    print(f"  missing:              {len(missing)}")
    print(f"  suspicious:           {len(suspicious)}")
    print(f"Extras in translated:   {len(extras)}")
    print(f"Active locks:           {len(locks)}")
    print(f"Cooldown:               {len(cooldowns)}")
    print(f"Progress:               {progress:.1f}%")
    if speed is not None:
        eta = speed * remaining
        finish = datetime.now() + timedelta(seconds=eta)
        print(f"Speed:                  ~{speed:.1f} s/file")
        print(f"ETA:                    ~{format_duration(eta)} (≈{finish:%H:%M})")
    else:
        print("Speed:                  (not enough data)")
    print(f"Source size:            {format_size(source_size)}")
    print(f"Translated size:        {format_size(translated_size)}")

    if failures:
        reasons = Counter(
            re.sub(r"\(.*?\)", "", str(f.get("reason", ""))).strip()[:40]
            for f in failures
        )
        print(f"\nFailures in failures.jsonl: {len(failures)}")
        for reason, count in reasons.most_common(5):
            print(f"  {count:>4}  {reason or '(no reason)'}")

    if args.verbose:
        print_paths("Not translated", missing)
        if suspicious:
            print(f"\nSuspicious outputs ({len(suspicious)}):")
            for relative, issue in suspicious:
                print(f"  - {relative.as_posix()}  [{issue}]")
        print_paths("Extra files in translated", extras)
        if locks:
            print(f"\nActive locks ({len(locks)}):")
            for lock in locks:
                print(f"  - {lock.name}")
        if cooldowns:
            print(f"\nCooldown ({len(cooldowns)}):")
            for path in cooldowns:
                print(f"  - {path.name}")
        if failures:
            print(f"\nRecent failures ({min(10, len(failures))}):")
            for f in failures[-10:]:
                print(
                    f"  {f.get('time', '?')}  w{f.get('worker', '?')}  "
                    f"{f.get('file', '?')}  [{f.get('reason', '?')}]"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
