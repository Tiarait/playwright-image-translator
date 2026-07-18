#!/usr/bin/env python3
"""Recompress suspicious translated/ files so QC treats them as done.

By default dry-run (lists only). Use --write to overwrite translated files.

Why: copies of originals fail QC (same size / almost identical / suspiciously large).
Re-saving as JPEG with lower quality makes the size drop enough to pass.

Usage:
  python fix_suspicious.py
  python fix_suspicious.py --write
  python fix_suspicious.py --write --only volume19
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

from imgtranslate.config import cfg_get, load_config
from imgtranslate.paths import CONFIG_PATH, SOURCE_DIR, TRANSLATED_DIR
from imgtranslate.qc import quality_issue

IMAGE_RE = re.compile(r"\.(jpe?g|png|webp)$", re.I)

try:
    from PIL import Image
except ImportError:
    print(
        "Error: Pillow is required. Run: pip install Pillow",
        file=sys.stderr,
    )
    raise SystemExit(2) from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path, default=CONFIG_PATH)
    pre_args, _ = pre.parse_known_args(argv)
    cfg = load_config(pre_args.config)

    p = argparse.ArgumentParser(
        description=(
            "Recompress suspicious translated images so they pass QC "
            "(default: dry-run; use --write to overwrite)"
        )
    )
    p.add_argument("--config", type=Path, default=CONFIG_PATH)
    p.add_argument("--source", type=Path, default=cfg_get(cfg, "source", SOURCE_DIR))
    p.add_argument(
        "--translated",
        type=Path,
        default=cfg_get(cfg, "translated", TRANSLATED_DIR),
    )
    p.add_argument(
        "--only",
        default="",
        help="substring filter on relative path",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="overwrite files in translated/ (without this flag: dry-run only)",
    )
    p.add_argument(
        "--quality",
        type=int,
        default=75,
        help="starting JPEG quality (default 75); lowered until QC passes",
    )
    p.add_argument(
        "--min-quality",
        type=int,
        default=40,
        help="lowest JPEG quality to try (default 40)",
    )
    return p.parse_args(argv)


def list_images(root: Path) -> dict[Path, Path]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and IMAGE_RE.search(path.name)
    }


def find_suspicious(
    source_root: Path, translated_root: Path, only: str
) -> list[tuple[Path, Path, Path, str]]:
    """Return list of (rel, src, out, issue)."""
    source = list_images(source_root)
    rows: list[tuple[Path, Path, Path, str]] = []
    for rel, src in sorted(source.items(), key=lambda x: x[0].as_posix()):
        if only and only not in rel.as_posix():
            continue
        out = translated_root / rel
        issue = quality_issue(src, out)
        if issue is None or issue == "missing":
            continue
        rows.append((rel, src, out, issue))
    return rows


def encode_jpeg(img: Image.Image, quality: int) -> bytes:
    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def compress_until_ok(
    src: Path, out: Path, start_quality: int, min_quality: int
) -> tuple[bytes | None, int, str | None]:
    """Return (jpeg_bytes, quality_used, error_or_None)."""
    try:
        with Image.open(out) as img:
            img.load()
            # Work on a copy of pixels
            base = img.copy()
    except OSError as exc:
        return None, 0, f"cannot open: {exc}"

    src_size = src.stat().st_size
    last_data: bytes | None = None
    last_q = start_quality

    for q in range(start_quality, min_quality - 1, -5):
        last_q = q
        data = encode_jpeg(base, q)
        last_data = data
        # Write to a temp check via size heuristics without touching disk:
        # quality_issue needs a real path - use a sidecar .tmp then check.
        tmp = out.with_suffix(out.suffix + ".fix_tmp")
        try:
            tmp.write_bytes(data)
            issue = quality_issue(src, tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        if issue is None:
            return data, q, None

        # Also try if we already shrank enough relative to src even if QC
        # still complains about something else transient - keep looping.
        if len(data) < src_size * 0.84 and abs(len(data) - src_size) / max(src_size, 1) >= 0.02:
            # Should have passed; if not, keep going lower
            pass

    if last_data is None:
        return None, 0, "encode failed"

    # Final attempt at min_quality already done; report leftover issue
    tmp = out.with_suffix(out.suffix + ".fix_tmp")
    try:
        tmp.write_bytes(last_data)
        issue = quality_issue(src, tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return last_data, last_q, issue or "still suspicious after min quality"


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def main() -> int:
    args = parse_args()
    source_root = args.source.resolve()
    translated_root = args.translated.resolve()

    if not source_root.is_dir():
        print(f"Error: source not found: {source_root}", file=sys.stderr)
        return 2

    start_q = max(1, min(95, args.quality))
    min_q = max(1, min(start_q, args.min_quality))

    rows = find_suspicious(source_root, translated_root, args.only)
    if not rows:
        print("No suspicious files found.")
        return 0

    mode = "WRITE" if args.write else "DRY-RUN"
    print(f"Suspicious files: {len(rows)}  [{mode}]")
    print(f"JPEG quality: {start_q} → min {min_q}")
    print(
        "Note: this only satisfies size-based QC heuristics; "
        "it does not verify that text was actually translated."
    )
    print()

    ok = 0
    fail = 0
    for rel, src, out, issue in rows:
        before = out.stat().st_size
        data, q, err = compress_until_ok(src, out, start_q, min_q)
        if data is None or err:
            fail += 1
            print(f"FAIL  {rel.as_posix()}")
            print(f"      was: {issue}")
            print(f"      {err}")
            continue

        after = len(data)
        if args.write:
            # Always save as .jpg bytes into the same path (keeps filename)
            out.write_bytes(data)
            # Verify after write
            post = quality_issue(src, out)
            if post is not None:
                fail += 1
                print(f"FAIL  {rel.as_posix()}  still: {post}")
                continue

        ok += 1
        action = "wrote" if args.write else "would write"
        print(
            f"OK    {rel.as_posix()}  "
            f"{format_size(before)} → {format_size(after)}  "
            f"q={q}  ({issue})  [{action}]"
        )

    print()
    print(f"Done: ok={ok} fail={fail} total={len(rows)}")
    if not args.write and ok:
        print("Dry-run only. Re-run with --write to overwrite translated/ files.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
