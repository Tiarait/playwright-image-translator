"""Output quality checks shared by translate.py and check.py."""

from __future__ import annotations

from pathlib import Path


def is_valid_image_bytes(data: bytes) -> bool:
    if len(data) < 24:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True  # JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True  # PNG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def is_valid_image(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return is_valid_image_bytes(f.read(64))
    except OSError:
        return False


def quality_issue(src: Path, out: Path) -> str | None:
    """Return None if OK, else short reason why output is bad."""
    if not out.exists():
        return "missing"
    try:
        out_size = out.stat().st_size
        src_size = src.stat().st_size
    except OSError as exc:
        return f"stat error: {exc}"

    if out_size < 1024:
        return f"too small ({out_size} B)"
    if not is_valid_image(out):
        return "not a valid image header"
    if out_size == src_size:
        return "same size as source"
    if abs(out_size - src_size) / max(src_size, 1) < 0.02:
        return "size almost identical to source"
    # Google overlay downloads are usually much smaller than 300dpi scans
    if out_size > src_size * 0.85 and out_size > 500_000:
        return "suspiciously large (likely original)"
    return None


def looks_translated(src: Path, out: Path) -> bool:
    return quality_issue(src, out) is None
