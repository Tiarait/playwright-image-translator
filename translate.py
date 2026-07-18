#!/usr/bin/env python3
"""Batch image translation via Google Translate Images (Playwright).

Layout:
  config.json - defaults (CLI overrides)
  proxy.txt - optional HTTP(S) proxies
  data/source/ - originals
  data/translated/ - results (mirrored paths)
  runtime/logs/ - session logs + failures/
  runtime/queue/ - locks + cooldowns
  runtime/browser-profiles/ - Chromium profile per worker
  imgtranslate/ - library

Usage:
  python translate.py
  python translate.py --workers 4
  python translate.py --workers 2 --quiet
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright

from imgtranslate import queue_claim
from imgtranslate.config import cfg_get, load_config
from imgtranslate.paths import (
    CONFIG_PATH,
    DEFAULT_PROFILE,
    LOGS_DIR,
    PROFILES_DIR,
    PROXY_FILE,
    ROOT,
    SOURCE_DIR,
    TRANSLATED_DIR,
)
from imgtranslate.proxy import (
    Proxy,
    assign_proxy,
    load_valid_proxies,
    parse_proxy_cli,
    validate_proxy,
)
from imgtranslate.progress import ProgressBar
from imgtranslate.qc import looks_translated, quality_issue

HERE = ROOT
IMAGE_RE = re.compile(r"\.(jpe?g|png|webp)$", re.I)
MAX_WORKERS = 32

# Graceful shutdown (Ctrl+C / SIGTERM)
_stop_requested = False
_stop_force = False
# Active job lock - refreshed during long waits so stale reclaim cannot steal it
_active_lock_rel: Path | None = None
_last_lock_touch = 0.0
# Child worker processes (launcher only) - force-kill on second Ctrl+C
_child_procs: list[subprocess.Popen] = []


class StopRequested(Exception):
    """Raised when the user asks to stop; not an error."""


class BrowserClosed(Exception):
    """Raised when the user closes the browser window manually."""


def request_stop(signum=None, frame=None) -> None:
    global _stop_requested, _stop_force
    if _stop_requested:
        _stop_force = True
        msg = "force stop (second signal) - exiting now"
        try:
            logging.warning(msg)
        except Exception:
            pass
        print(f"\n{msg}", file=sys.stderr)
        for proc in list(_child_procs):
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        os._exit(130)

    _stop_requested = True
    msg = "stop requested - finishing current job cleanup (Ctrl+C again to force)"
    try:
        logging.warning(msg)
    except Exception:
        pass
    print(f"\n{msg}", file=sys.stderr)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)


def should_stop() -> bool:
    return _stop_requested


def check_stop() -> None:
    if _stop_requested:
        raise StopRequested()


def set_active_lock(rel: Path | None) -> None:
    """Track the lock currently owned by this worker (for heartbeat)."""
    global _active_lock_rel, _last_lock_touch
    _active_lock_rel = rel
    _last_lock_touch = 0.0
    if rel is not None:
        queue_claim.touch(rel)
        _last_lock_touch = time.time()


def heartbeat_lock(min_interval_sec: float = 30.0) -> None:
    """Refresh lock mtime so long jobs are not treated as stale."""
    global _last_lock_touch
    rel = _active_lock_rel
    if rel is None:
        return
    now = time.time()
    if now - _last_lock_touch < min_interval_sec:
        return
    queue_claim.touch(rel)
    _last_lock_touch = now


def sleep_ms(ms: int) -> None:
    """Interruptible sleep - reacts to Ctrl+C within ~200ms."""
    end = time.time() + ms / 1000.0
    while True:
        check_stop()
        heartbeat_lock()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


def sleep_delay_ms(base_ms: int, jitter_pct: float = 0) -> None:
    """Pause between jobs; optional ±jitter_pct% randomization (0 = fixed delay)."""
    if base_ms <= 0:
        return
    if jitter_pct <= 0:
        sleep_ms(base_ms)
        return
    spread = jitter_pct / 100.0
    factor = 1.0 + random.uniform(-spread, spread)
    actual = max(0, int(base_ms * factor))
    logging.debug(
        "delay jitter: %s ms (base=%s, jitter=±%s%%)", actual, base_ms, jitter_pct
    )
    sleep_ms(actual)



@dataclass
class Job:
    rel: Path
    src: Path
    dest: Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)

    # First pass: only --config, so config.json can set argparse defaults.
    # Explicit CLI flags still win (argparse only uses defaults when flag absent).
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path, default=CONFIG_PATH)
    pre_args, _ = pre.parse_known_args(argv)
    cfg = load_config(pre_args.config)

    p = argparse.ArgumentParser(
        description="Translate images via Google Translate Images (Playwright)"
    )
    p.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"JSON settings file (default: {CONFIG_PATH.name}); CLI overrides it",
    )
    p.add_argument("--source", type=Path, default=cfg_get(cfg, "source", SOURCE_DIR))
    p.add_argument(
        "--translated", type=Path, default=cfg_get(cfg, "translated", TRANSLATED_DIR)
    )
    p.add_argument("--logs", type=Path, default=cfg_get(cfg, "logs", LOGS_DIR))
    p.add_argument(
        "--profiles",
        type=Path,
        default=cfg_get(cfg, "profiles", PROFILES_DIR),
        help="directory for browser-profiles/wN (multi-worker)",
    )
    p.add_argument("--profile", type=Path, default=cfg_get(cfg, "profile", None))
    p.add_argument(
        "--proxy-file",
        type=Path,
        default=cfg_get(cfg, "proxy_file", PROXY_FILE),
        help="path to proxy.txt (host:port per line); missing = no proxy",
    )
    p.add_argument(
        "--proxy",
        default=os.environ.get("IMAGE_TRANSLATE_PROXY", ""),
        help="single proxy for this process (overrides proxy-file assignment)",
    )
    p.add_argument(
        "--require-proxy",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg_get(cfg, "require_proxy", False)),
        help="exit if proxy.txt has no valid proxies",
    )
    p.add_argument(
        "--delay",
        type=int,
        default=cfg_get(cfg, "delay", 3000),
        help="pause between images, ms",
    )
    p.add_argument(
        "--delay-jitter",
        type=float,
        default=float(cfg_get(cfg, "delay_jitter", 0)),
        help="random ±N%% on delay between images (0 = fixed; e.g. 25 = 3000±750ms)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=cfg_get(cfg, "timeout", 120_000),
        help="wait timeout, ms",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=cfg_get(cfg, "limit", 0),
        help="max jobs this process claims",
    )
    p.add_argument(
        "--only",
        default=cfg_get(cfg, "only", ""),
        help="substring filter on relative path",
    )
    p.add_argument("--sl", default=cfg_get(cfg, "sl", "ka"))
    p.add_argument("--tl", default=cfg_get(cfg, "tl", "ru"))
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg_get(cfg, "headless", False)),
        help="no browser window (less reliable vs Google); --no-headless to force window",
    )
    p.add_argument(
        "--quiet",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg_get(cfg, "quiet", False)),
        help="minimize browser windows; console stays error-only unless --verbose",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=bool(cfg_get(cfg, "verbose", False)),
        help="print INFO lines to the terminal (default: progress + errors only)",
    )
    p.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show progress line in the terminal (default on)",
    )
    p.add_argument(
        "--fail-shots",
        action=argparse.BooleanOptionalAction,
        default=not bool(cfg_get(cfg, "no_fail_shots", False))
        if "fail_shots" not in cfg
        else bool(cfg_get(cfg, "fail_shots", True)),
        help="save failure screenshots (default on); use --no-fail-shots to disable",
    )
    p.add_argument(
        "--fail-shot-quality",
        type=int,
        default=cfg_get(cfg, "fail_shot_quality", 35),
        help="JPEG quality for failure shots 1-100 (default 35)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=cfg_get(cfg, "workers", 1),
        help=f"spawn N worker processes (1–{MAX_WORKERS}), each with own profile/proxy",
    )
    p.add_argument(
        "--worker-id",
        default="",
        help="internal: id of this worker (set by --workers launcher)",
    )
    p.add_argument(
        "--stale-lock",
        type=int,
        default=cfg_get(cfg, "stale_lock", 900),
        help="reclaim locks older than N seconds (default 900)",
    )
    p.add_argument(
        "--reload-every",
        type=int,
        default=cfg_get(cfg, "reload_every", 0),
        help="force full page reload every N jobs (0=never, only on clear failure)",
    )
    p.add_argument(
        "--download-settle",
        type=int,
        default=cfg_get(cfg, "download_settle", 3500),
        help="pause after translation UI is ready, before download click (ms)",
    )
    p.add_argument(
        "--fail-cooldown",
        type=int,
        default=cfg_get(cfg, "fail_cooldown", 180),
        help="after permanent fail, block other workers from this file for N seconds",
    )
    args = p.parse_args(argv)
    args._config_path = cfg.get("_config_path")
    if args.workers < 1:
        p.error("--workers must be >= 1")
    if args.workers > MAX_WORKERS:
        p.error(f"--workers must be <= {MAX_WORKERS}")
    if args.delay_jitter < 0 or args.delay_jitter > 100:
        p.error("--delay-jitter must be between 0 and 100")
    return args


def setup_logging(
    logs_dir: Path, worker_id: str, *, verbose: bool = False
) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_w{worker_id}" if worker_id else ""
    log_path = logs_dir / f"translate_{stamp}{suffix}.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Terminal: ERROR only by default (final failures). --verbose adds INFO.
    # Detailed WARNING/INFO always go to the log file.
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO if verbose else logging.ERROR)
    sh.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    return log_path


def minimize_window(page: Page) -> None:
    """Best-effort minimize via CDP (works better than --start-minimized on macOS)."""
    try:
        session = page.context.new_cdp_session(page)
        target = session.send("Browser.getWindowForTarget")
        window_id = target.get("windowId")
        if window_id is not None:
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
            )
            logging.info("browser window minimized")
    except Exception as exc:
        logging.debug("minimize failed: %s", exc)


def worker_window_title(worker_id: str) -> str:
    return f"Worker {worker_id}"


_TITLE_KEEP_JS = """
(title) => {
  window.__workerTitle = title;
  document.title = title;
  if (window.__workerTitleHooked) return;
  window.__workerTitleHooked = true;
  const apply = () => {
    if (document.title !== window.__workerTitle) {
      document.title = window.__workerTitle;
    }
  };
  const hookTitleEl = () => {
    const el = document.querySelector('title');
    if (!el || el.__workerObserved) return;
    el.__workerObserved = true;
    new MutationObserver(apply).observe(el, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  };
  hookTitleEl();
  setInterval(() => {
    hookTitleEl();
    apply();
  }, 800);
}
"""


def set_worker_title(page: Page, worker_id: str) -> None:
    """Set tab/window title to Worker N and keep it against Google overwrites."""
    if page.is_closed():
        return
    title = worker_window_title(worker_id)
    try:
        page.evaluate(_TITLE_KEEP_JS, title)
    except Exception as exc:
        logging.debug("could not set window title: %s", exc)


def is_browser_closed_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "target closed",
        "target page, context or browser has been closed",
        "browser has been closed",
        "context or browser has been closed",
        "connection closed",
        "page closed",
    )
    return any(n in msg for n in needles)


def ensure_page_alive(page: Page) -> None:
    if page.is_closed():
        raise BrowserClosed("page closed")
    try:
        page.evaluate("1")
    except Exception as exc:
        if page.is_closed() or is_browser_closed_error(exc):
            raise BrowserClosed(str(exc)) from exc
        raise


def raise_if_browser_closed(exc: BaseException, page: Page | None = None) -> None:
    if isinstance(exc, BrowserClosed):
        raise exc
    if page is not None and page.is_closed():
        raise BrowserClosed(str(exc)) from exc
    if is_browser_closed_error(exc):
        raise BrowserClosed(str(exc)) from exc


def install_browser_close_watch(context, page: Page, worker_id: str) -> None:
    """Log when the user closes the window; work loop raises BrowserClosed next."""

    def _on_close(_page=None) -> None:
        logging.warning("browser window closed by user (worker=%s)", worker_id)

    try:
        context.on("close", lambda: _on_close())
    except Exception:
        pass
    try:
        page.on("close", lambda: _on_close())
    except Exception:
        pass
    try:
        page.on("framenavigated", lambda _frame: set_worker_title(page, worker_id))
    except Exception:
        pass


def images_url(sl: str, tl: str) -> str:
    return f"https://translate.google.com/?hl=en&sl={sl}&tl={tl}&op=images"


def ensure_mirror_dirs(source_dir: Path, translated_dir: Path) -> None:
    translated_dir.mkdir(parents=True, exist_ok=True)
    if not source_dir.is_dir():
        source_dir.mkdir(parents=True, exist_ok=True)
        return
    for path in sorted(source_dir.rglob("*")):
        if path.is_dir():
            dest = translated_dir / path.relative_to(source_dir)
            if not dest.exists():
                dest.mkdir(parents=True, exist_ok=True)
                logging.info("created mirror dir: translated/%s", dest.relative_to(translated_dir).as_posix())


def iter_source_images(source_dir: Path, only: str) -> list[Job]:
    if not source_dir.is_dir():
        return []
    jobs: list[Job] = []
    for src in sorted(
        p
        for p in source_dir.rglob("*")
        if p.is_file() and IMAGE_RE.search(p.name) and not p.name.startswith(".")
    ):
        rel = src.relative_to(source_dir)
        if only and only not in rel.as_posix():
            continue
        jobs.append(Job(rel=rel, src=src, dest=Path()))  # dest filled by caller
    return jobs


def count_progress(
    source_dir: Path, translated_dir: Path, only: str = ""
) -> tuple[int, int, int]:
    """Return (total, done, remaining) by scanning disks - shared across workers."""
    total = 0
    done = 0
    for item in iter_source_images(source_dir, only):
        total += 1
        dest = translated_dir / item.rel
        if looks_translated(item.src, dest):
            done += 1
    return total, done, total - done


def open_progress_bar(
    source_dir: Path, translated_dir: Path, only: str = "", *, enabled: bool = True
) -> ProgressBar | None:
    if not enabled:
        return None
    total, done, _ = count_progress(source_dir, translated_dir, only)
    return ProgressBar(total, done, desc="translate")


def refresh_progress_bar(
    bar: ProgressBar | None,
    source_dir: Path,
    translated_dir: Path,
    only: str = "",
) -> None:
    if bar is None:
        return
    total, done, _ = count_progress(source_dir, translated_dir, only)
    bar.refresh_counts(total, done)


def is_visible(loc: Locator, timeout: float = 800) -> bool:
    try:
        return loc.first.is_visible(timeout=timeout)
    except Exception:
        return False


def dismiss_consent(page: Page) -> None:
    for label in (r"Accept all", r"I agree"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if is_visible(btn, 1200):
                btn.click()
                logging.debug("dismissed consent banner")
        except Exception:
            pass


def find_image_file_input(page: Page) -> Locator | None:
    by_accept = page.locator(
        'input[type="file"][accept*="image"], '
        'input[type="file"][accept*=".jpg"], '
        'input[type="file"][accept*="jpeg"], '
        'input[type="file"][accept*="png"]'
    )
    if by_accept.count() > 0:
        return by_accept.first

    inputs = page.locator('input[type="file"]')
    for i in range(inputs.count()):
        el = inputs.nth(i)
        accept = (el.get_attribute("accept") or "").lower()
        if "pdf" in accept or "docx" in accept:
            continue
        if (not accept) or ("image" in accept) or ("jpg" in accept):
            return el
    return None


def click_images_mode_once(page: Page) -> None:
    if find_image_file_input(page) is not None:
        return
    logging.debug("image input missing - switching to Images tab")
    tab = (
        page.get_by_role("tab", name=re.compile(r"images|photos", re.I))
        .or_(page.get_by_role("button", name=re.compile(r"images|photos", re.I)))
        .or_(page.locator('[data-value="images"], a[href*="op=images"]'))
        .first
    )
    try:
        if is_visible(tab, 2500):
            tab.click()
            sleep_ms(600)
    except Exception:
        pass


def wait_for_image_input(page: Page, timeout_ms: int = 30_000) -> Locator:
    deadline = time.time() + timeout_ms / 1000.0
    clicked = False
    while time.time() < deadline:
        found = find_image_file_input(page)
        if found is not None:
            return found
        if not clicked:
            click_images_mode_once(page)
            clicked = True
        else:
            sleep_ms(400)
    raise RuntimeError(
        "Image file input not found. Open the Images tab manually if needed."
    )


def open_images_page(page: Page, sl: str, tl: str, worker_id: str = "1") -> None:
    url = images_url(sl, tl)
    logging.debug("goto %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    dismiss_consent(page)
    wait_for_image_input(page)
    set_worker_title(page, worker_id)


def clear_current_image(page: Page) -> bool:
    """Try to clear the current image without full reload. True if upload ready."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"clear|close|remove", re.I)),
        page.locator('button[aria-label*="Clear" i]'),
        page.locator('button[aria-label*="Close" i]'),
        page.locator('button[aria-label*="Remove" i]'),
    ]
    for loc in candidates:
        try:
            btn = loc.first
            if is_visible(btn, 500):
                btn.click(timeout=2000)
                sleep_ms(900)
                if find_image_file_input(page) is not None:
                    logging.debug("cleared image via UI button")
                    return True
        except Exception:
            continue
    return find_image_file_input(page) is not None


def prepare_for_upload(
    page: Page, sl: str, tl: str, force_reload: bool = False, worker_id: str = "1"
) -> None:
    if force_reload:
        open_images_page(page, sl, tl, worker_id=worker_id)
        return
    if clear_current_image(page) and find_image_file_input(page) is not None:
        set_worker_title(page, worker_id)
        return
    logging.info("clear failed - full reload")
    open_images_page(page, sl, tl, worker_id=worker_id)


def upload_image(page: Page, file_path: Path) -> None:
    inp = wait_for_image_input(page, 15_000)
    logging.info("upload: %s", file_path.name)
    inp.set_input_files(str(file_path))


def looks_like_captcha(text: str) -> bool:
    return bool(
        re.search(
            r"unusual traffic|captcha|are you a robot",
            text,
            re.I,
        )
    )


def find_download_trigger(page: Page) -> Locator | None:
    preferred = page.get_by_role(
        "button",
        name=re.compile(r"download translation|save translation", re.I),
    ).first
    if is_visible(preferred):
        return preferred

    role_btn = page.get_by_role("button", name=re.compile(r"download", re.I)).first
    if is_visible(role_btn):
        return role_btn

    aria = page.locator(
        'button[aria-label*="Download translation" i], '
        'button[aria-label*="Download" i]'
    ).first
    if is_visible(aria):
        return aria

    link = page.locator("a[download]").first
    if is_visible(link):
        return link
    return None


def has_no_text_error(body: str) -> bool:
    """Google OCR could not find text / unsupported language message."""
    return bool(
        re.search(
            r"text not found|language.{0,60}not supported",
            body,
            re.I,
        )
    )


def translation_ui_ready(page: Page) -> bool:
    markers = [
        page.get_by_role(
            "button", name=re.compile(r"show original|original", re.I)
        ),
        page.get_by_text(re.compile(r"show original", re.I)),
        page.get_by_role(
            "button",
            name=re.compile(r"download translation|save translation", re.I),
        ),
        page.locator(
            'button[aria-label*="Show original" i], '
            'button[aria-label*="Download translation" i]'
        ),
    ]
    return any(is_visible(m.first) for m in markers)


def wait_until_ready(page: Page, timeout_ms: int, download_settle_ms: int) -> dict:
    """Wait until translation overlay is ready, then settle before download."""
    deadline = time.time() + timeout_ms / 1000.0
    started = time.time()
    ready_since: float | None = None

    while time.time() < deadline:
        check_stop()
        try:
            body = (page.locator("body").inner_text(timeout=1000) or "")[:2500]
        except Exception:
            body = ""

        if looks_like_captcha(body):
            raise RuntimeError(
                "Looks like CAPTCHA / Google block. Solve it in the browser window and retry."
            )

        if has_no_text_error(body):
            # Google shows this when OCR finds no text - do not download yet.
            ready_since = None
            if (time.time() - started) > 5:
                logging.debug("Google: no text / unsupported language - still waiting")
            sleep_ms(500)
            continue

        # Still processing - reset settle timer
        if re.search(
            r"translating|processing",
            body,
            re.I,
        ):
            ready_since = None
            sleep_ms(400)
            continue

        ui_ready = translation_ui_ready(page)
        if ui_ready:
            if ready_since is None:
                ready_since = time.time()
                logging.debug(
                    "translation UI ready - settling %s ms before download",
                    download_settle_ms,
                )
            settled_ms = (time.time() - ready_since) * 1000
            if settled_ms < download_settle_ms:
                sleep_ms(min(400, int(download_settle_ms - settled_ms)))
                continue

            trigger = find_download_trigger(page)
            if trigger is not None:
                # Final short pause right before click
                sleep_ms(500)
                trigger = find_download_trigger(page) or trigger
                return {"kind": "download", "trigger": trigger}
            return {"kind": "capture", "trigger": None}

        # Do NOT download just because a generic Download button appeared early.
        # Only after a long wait as last resort (and still settle).
        elapsed_ms = (time.time() - started) * 1000
        trigger = find_download_trigger(page)
        if trigger is not None and elapsed_ms > 20_000:
            logging.warning(
                "UI markers missing after %.0fs - cautious download after settle",
                elapsed_ms / 1000,
            )
            sleep_ms(download_settle_ms)
            if translation_ui_ready(page) or elapsed_ms > 45_000:
                trigger2 = find_download_trigger(page) or trigger
                return {"kind": "download", "trigger": trigger2}

        sleep_ms(500)

    raise RuntimeError("Timeout: translated image UI did not appear")


def save_via_download(
    page: Page, trigger: Locator, dest: Path, timeout_ms: int
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    with page.expect_download(timeout=timeout_ms) as dl_info:
        trigger.click()
    download = dl_info.value
    download.save_as(str(tmp))
    tmp.replace(dest)


CAPTURE_JS = """
async () => {
  const score = (el) => {
    const r = el.getBoundingClientRect();
    return r.width * r.height;
  };

  const canvases = [...document.querySelectorAll("canvas")].filter(
    (c) => score(c) > 200 * 200
  );
  canvases.sort((a, b) => score(b) - score(a));
  if (canvases[0]) {
    try {
      return canvases[0].toDataURL("image/jpeg", 0.92);
    } catch (e) {}
  }

  const imgs = [...document.querySelectorAll("img")].filter((img) => {
    const r = img.getBoundingClientRect();
    return r.width > 200 && r.height > 200 && img.src;
  });
  imgs.sort((a, b) => score(b) - score(a));

  for (const img of imgs) {
    if (img.src.startsWith("blob:") || img.src.startsWith("data:")) {
      const res = await fetch(img.src);
      const blob = await res.blob();
      return await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
    }
    try {
      const c = document.createElement("canvas");
      c.width = img.naturalWidth || img.width;
      c.height = img.naturalHeight || img.height;
      const ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0);
      return c.toDataURL("image/jpeg", 0.92);
    } catch (e) {}
  }
  return null;
}
"""


def save_via_capture(page: Page, dest: Path) -> None:
    data_url = page.evaluate(CAPTURE_JS)
    if not data_url or not str(data_url).startswith("data:"):
        raise RuntimeError("Failed to capture translated image from the page")

    b64 = str(data_url).split(",", 1)[1]
    buf = base64.b64decode(b64)
    if len(buf) < 1024:
        raise RuntimeError("Captured image file is too small")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    tmp.write_bytes(buf)
    tmp.replace(dest)


def safe_failure_stem(rel: Path, worker_id: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    wid = worker_id or "main"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", rel.as_posix())[:100]
    return f"{stamp}_w{wid}_{slug}"


def append_failure_record(
    failures_dir: Path,
    rel: Path,
    worker_id: str,
    reason: str,
    attempts: int,
    shot: str | None,
) -> None:
    """Append one line to the shared failures.jsonl (all failures in one file)."""
    failures_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "worker": worker_id,
        "file": rel.as_posix(),
        "attempts": attempts,
        "reason": reason,
        "screenshot": shot,
    }
    line = json.dumps(record, ensure_ascii=False)
    log_path = failures_dir / "failures.jsonl"
    try:
        # Append is atomic enough for short lines across workers on local FS
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        logging.warning("could not write failures.jsonl: %s", exc)


def save_failure_shot(
    page: Page,
    failures_dir: Path,
    rel: Path,
    worker_id: str,
    reason: str,
    quality: int = 35,
) -> str | None:
    """Save a small JPEG viewport shot. Returns filename or None."""
    failures_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_failure_stem(rel, worker_id)
    jpg_path = failures_dir / f"{stem}.jpg"
    q = max(1, min(95, quality))
    try:
        page.screenshot(
            path=str(jpg_path),
            type="jpeg",
            quality=q,
            full_page=False,
            scale="css",
        )
        size_kb = jpg_path.stat().st_size / 1024
        logging.info("failure shot: %s (%.0f KB) - %s", jpg_path.name, size_kb, reason)
        return jpg_path.name
    except Exception as exc:
        logging.warning("could not save failure shot: %s", exc)
        return None


def translate_one(
    page: Page,
    job: Job,
    sl: str,
    tl: str,
    timeout_ms: int,
    force_reload: bool,
    download_settle_ms: int,
    worker_id: str = "1",
) -> None:
    ensure_page_alive(page)
    prepare_for_upload(
        page, sl, tl, force_reload=force_reload, worker_id=worker_id
    )
    set_worker_title(page, worker_id)
    upload_image(page, job.src)
    logging.info("waiting for translation…")

    ready = wait_until_ready(page, timeout_ms, download_settle_ms)

    if ready["kind"] == "download":
        logging.info(
            "translation ready - downloading (settled %s ms)", download_settle_ms
        )
        try:
            save_via_download(page, ready["trigger"], job.dest, timeout_ms)
        except Exception as err:
            raise_if_browser_closed(err, page)
            logging.warning("download failed (%s) - capture fallback", err)
            save_via_capture(page, job.dest)
    else:
        logging.info("translation ready - capturing from page")
        save_via_capture(page, job.dest)

    issue = quality_issue(job.src, job.dest)
    if issue:
        try:
            job.dest.unlink()
        except OSError:
            pass
        raise RuntimeError(f"QC failed: {issue}")

    size_kb = job.dest.stat().st_size / 1024
    logging.info("saved: translated/%s (%0.0f KB)", job.rel.as_posix(), size_kb)
    set_worker_title(page, worker_id)



def launch_workers(args: argparse.Namespace) -> int:
    install_signal_handlers()
    n = max(1, min(MAX_WORKERS, args.workers))
    profiles_dir = Path(args.profiles).resolve()
    profiles_dir.mkdir(parents=True, exist_ok=True)

    # Validate proxies once in the launcher, then pin one per worker
    proxies: list[Proxy] = []
    if args.proxy:
        proxies = [parse_proxy_cli(args.proxy)]
        ok, reason = validate_proxy(proxies[0])
        if not ok:
            msg = f"invalid --proxy ({reason}): {proxies[0].masked_display}"
            if args.require_proxy:
                print(msg, file=sys.stderr)
                return 2
            print(f"warning: {msg} - continuing without proxy", file=sys.stderr)
            proxies = []
        else:
            print(f"proxy OK [{reason}]: {proxies[0].masked_display}")
    else:
        proxies, proxy_path = load_valid_proxies(args.proxy_file)
        if proxy_path is None:
            print("proxy: no proxy.txt - workers without proxy")
        elif not proxies:
            msg = f"no valid proxies in {proxy_path}"
            if args.require_proxy:
                print(msg, file=sys.stderr)
                return 2
            print(f"warning: {msg} - continuing without proxy")
        else:
            print(f"proxy: {len(proxies)} valid from {proxy_path}")

    global _child_procs
    procs: list[subprocess.Popen] = []
    _child_procs = procs
    print(f"Launching {n} workers…")
    print("Ctrl+C once = graceful stop (locks released, browsers closed).")
    for i in range(1, n + 1):
        profile = profiles_dir / f"w{i}"
        proxy = assign_proxy(proxies, i)
        worker_env = os.environ.copy()
        cmd = [
            sys.executable,
            str(HERE / "translate.py"),
            "--worker-id",
            str(i),
            "--profile",
            str(profile),
            "--profiles",
            str(profiles_dir),
            "--source",
            str(args.source),
            "--translated",
            str(args.translated),
            "--logs",
            str(args.logs),
            "--proxy-file",
            str(args.proxy_file),
            "--delay",
            str(args.delay),
            "--delay-jitter",
            str(args.delay_jitter),
            "--timeout",
            str(args.timeout),
            "--limit",
            str(args.limit),
            "--sl",
            args.sl,
            "--tl",
            args.tl,
            "--stale-lock",
            str(args.stale_lock),
            "--reload-every",
            str(args.reload_every),
            "--download-settle",
            str(args.download_settle),
            "--fail-cooldown",
            str(args.fail_cooldown),
            "--workers",
            "1",
        ]
        if args.only:
            cmd.extend(["--only", args.only])
        if proxy is not None:
            # Keep proxy credentials out of the process command line.
            worker_env["IMAGE_TRANSLATE_PROXY"] = proxy.cli_value()
            print(f"  worker {i} → {proxy.masked_display}")
        else:
            worker_env.pop("IMAGE_TRANSLATE_PROXY", None)
            print(f"  worker {i} → direct (current IP)")
        cmd.append("--headless" if args.headless else "--no-headless")
        cmd.append("--quiet" if args.quiet else "--no-quiet")
        cmd.append("--fail-shots" if args.fail_shots else "--no-fail-shots")
        cmd.append("--require-proxy" if args.require_proxy else "--no-require-proxy")
        # Parent owns the single progress bar for multi-worker runs
        cmd.append("--no-progress")
        if args.verbose:
            cmd.append("--verbose")
        cmd.extend(["--fail-shot-quality", str(args.fail_shot_quality)])
        # Avoid child re-applying config.json on top of already-resolved args
        cmd.extend(["--config", os.devnull])
        # Own session so terminal Ctrl+C hits only the launcher; we forward SIGINT once.
        procs.append(
            subprocess.Popen(
                cmd,
                cwd=str(HERE),
                env=worker_env,
                start_new_session=True,
            )
        )

    bar = open_progress_bar(
        Path(args.source),
        Path(args.translated),
        args.only,
        enabled=args.progress,
    )
    announced_exit: set[int] = set()
    last_progress_scan = 0.0
    try:
        while True:
            check_stop()
            now = time.time()
            # Full disk QC scan is expensive - refresh at most every 2s
            if bar is not None and now - last_progress_scan >= 2.0:
                refresh_progress_bar(
                    bar, Path(args.source), Path(args.translated), args.only
                )
                last_progress_scan = now
            for i, p in enumerate(procs, start=1):
                code = p.poll()
                if code is not None and i not in announced_exit:
                    announced_exit.add(i)
                    if code == 0:
                        print(f"worker {i} finished", flush=True)
                    else:
                        print(f"worker {i} exited (code={code})", flush=True)
            if all(p.poll() is not None for p in procs):
                break
            time.sleep(0.5)
    except StopRequested:
        print("Stopping workers…", flush=True)
        for p in procs:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGINT)
                except Exception:
                    pass
        deadline = time.time() + 45
        while time.time() < deadline:
            if all(p.poll() is not None for p in procs):
                break
            time.sleep(0.3)
        for p in procs:
            if p.poll() is None:
                print(f"  terminating pid {p.pid}…", flush=True)
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(1.0)
        for p in procs:
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
    finally:
        refresh_progress_bar(
            bar, Path(args.source), Path(args.translated), args.only
        )
        if bar is not None:
            bar.close()

    codes = [p.wait() for p in procs]
    # 130 / -SIGINT = user interrupt
    real_fail = sum(1 for c in codes if c not in (0, 130, -2, -15))
    print(f"Workers finished: codes={codes}")
    if should_stop():
        removed = queue_claim.clear_locks()
        if removed:
            print(f"Cleared queue locks: {removed}")
        if real_fail == 0:
            print("Stopped cleanly.")
            return 0
        print("Stopped, but some workers did not exit cleanly.", file=sys.stderr)
        return 1
    return 0 if real_fail == 0 else 1


def run_worker(args: argparse.Namespace) -> int:
    install_signal_handlers()
    source_dir = args.source.resolve()
    translated_dir = args.translated.resolve()
    logs_dir = args.logs.resolve()
    failures_dir = logs_dir / "failures"
    profiles_dir = Path(args.profiles).resolve()
    worker_id = args.worker_id or "1"

    if args.profile:
        profile = args.profile.resolve()
    elif args.worker_id:
        profile = (profiles_dir / f"w{worker_id}").resolve()
    else:
        profile = DEFAULT_PROFILE.resolve()

    # Resolve proxy for this process
    proxy_settings: dict | None = None
    proxy_obj: Proxy | None = None
    if args.proxy:
        proxy_obj = parse_proxy_cli(args.proxy)
        ok, reason = validate_proxy(proxy_obj)
        if ok:
            proxy_settings = proxy_obj.playwright_dict()
        else:
            msg = f"invalid --proxy ({reason}): {proxy_obj.masked_display}"
            if args.require_proxy:
                print(msg, file=sys.stderr)
                return 2
            logging.warning("%s - continuing without proxy", msg)
            proxy_obj = None
    elif not args.worker_id:
        # Single-process mode: pick first valid from file (if any)
        proxies, proxy_path = load_valid_proxies(args.proxy_file)
        if proxies:
            proxy_obj = proxies[0]
            proxy_settings = proxy_obj.playwright_dict()
        elif args.require_proxy:
            print(
                f"require_proxy: no valid proxies in {proxy_path or args.proxy_file}",
                file=sys.stderr,
            )
            return 2

    log_path = setup_logging(logs_dir, worker_id, verbose=args.verbose)
    logging.info("session start worker=%s", worker_id)
    if getattr(args, "_config_path", None):
        logging.info("config: %s", args._config_path)
    else:
        logging.info("config: (none / empty)")
    logging.info("log file: %s", log_path)
    logging.info("source: %s", source_dir)
    logging.info("translated: %s", translated_dir)
    logging.info("profile: %s", profile)
    logging.info("proxy: %s", proxy_obj.masked_display if proxy_obj else "(none)")
    logging.info("lang: %s → %s", args.sl, args.tl)
    logging.info("delay: %s ms (jitter ±%s%%)", args.delay, args.delay_jitter)
    logging.info("download-settle: %s ms", args.download_settle)
    logging.info("fail-cooldown: %s s", args.fail_cooldown)
    logging.info(
        "mode: headless=%s quiet=%s verbose=%s fail_shots=%s progress=%s",
        args.headless,
        args.quiet,
        args.verbose,
        args.fail_shots,
        args.progress,
    )

    source_dir.mkdir(parents=True, exist_ok=True)
    translated_dir.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    failures_dir.mkdir(parents=True, exist_ok=True)
    queue_claim.locks_dir()
    ensure_mirror_dirs(source_dir, translated_dir)

    ok = 0
    fail = 0
    claimed_count = 0
    jobs_done_in_browser = 0
    current_rel: Path | None = None
    stopped = False
    browser_closed_by_user = False
    context = None
    progress = open_progress_bar(
        source_dir, translated_dir, args.only, enabled=args.progress
    )

    try:
        with sync_playwright() as p:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if args.quiet and not args.headless:
                launch_args.append("--start-minimized")

            launch_kwargs: dict = {
                "user_data_dir": str(profile),
                "headless": args.headless,
                "accept_downloads": True,
                "viewport": {"width": 1280, "height": 900},
                "locale": "en-US",
                "args": launch_args,
            }
            if proxy_settings:
                launch_kwargs["proxy"] = proxy_settings

            context = p.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            install_browser_close_watch(context, page, worker_id)
            open_images_page(page, args.sl, args.tl, worker_id=worker_id)
            if args.quiet and not args.headless:
                minimize_window(page)
            logging.info(
                "browser ready as %s - CAPTCHA: solve in the window if needed",
                worker_window_title(worker_id),
            )
            logging.info("Ctrl+C = graceful stop (releases lock, closes browser)")
            refresh_progress_bar(progress, source_dir, translated_dir, args.only)

            stop_loop = False
            while not stop_loop:
                check_stop()
                ensure_page_alive(page)
                candidates = iter_source_images(source_dir, args.only)
                progress_made = False

                for item in candidates:
                    check_stop()
                    ensure_page_alive(page)
                    if args.limit > 0 and claimed_count >= args.limit:
                        stop_loop = True
                        break

                    job = Job(
                        rel=item.rel,
                        src=item.src,
                        dest=translated_dir / item.rel,
                    )

                    if looks_translated(job.src, job.dest):
                        continue

                    if job.dest.exists():
                        issue = quality_issue(job.src, job.dest)
                        logging.warning(
                            "removing bad output (%s): %s",
                            issue,
                            job.rel.as_posix(),
                        )
                        try:
                            job.dest.unlink()
                        except OSError as exc:
                            logging.error("cannot remove %s: %s", job.dest, exc)
                            continue

                    if not queue_claim.claim(
                        job.rel,
                        worker_id,
                        stale_sec=args.stale_lock,
                        cooldown_sec=args.fail_cooldown,
                    ):
                        if queue_claim.in_cooldown(job.rel, args.fail_cooldown):
                            logging.debug("cooldown: %s", job.rel.as_posix())
                        else:
                            logging.debug("already claimed: %s", job.rel.as_posix())
                        continue

                    claimed_count += 1
                    progress_made = True
                    current_rel = job.rel
                    set_active_lock(job.rel)
                    logging.info(
                        "--- claimed [%s] %s",
                        claimed_count,
                        job.rel.as_posix(),
                    )

                    success = False
                    last_error = ""
                    attempts_used = 0
                    try:
                        for attempt in range(1, 4):
                            check_stop()
                            ensure_page_alive(page)
                            attempts_used = attempt
                            try:
                                if attempt > 1:
                                    logging.warning(
                                        "retry %s/3 same file after error: %s",
                                        attempt,
                                        job.rel.as_posix(),
                                    )

                                force_reload = attempt > 1
                                if (
                                    not force_reload
                                    and args.reload_every > 0
                                    and jobs_done_in_browser > 0
                                    and jobs_done_in_browser % args.reload_every == 0
                                ):
                                    force_reload = True

                                translate_one(
                                    page,
                                    job,
                                    args.sl,
                                    args.tl,
                                    args.timeout,
                                    force_reload=force_reload,
                                    download_settle_ms=args.download_settle,
                                    worker_id=worker_id,
                                )
                                ok += 1
                                success = True
                                jobs_done_in_browser += 1
                                break
                            except StopRequested:
                                raise
                            except BrowserClosed:
                                raise
                            except Exception as err:
                                raise_if_browser_closed(err, page)
                                last_error = str(err)
                                # Intermediate failures are expected (early download);
                                # log only, no screenshot yet.
                                logging.warning(
                                    "try %s/3 failed for %s: %s",
                                    attempt,
                                    job.rel.as_posix(),
                                    err,
                                )
                                if re.search(r"CAPTCHA|Google block", last_error, re.I):
                                    logging.warning("waiting 60s for CAPTCHA…")
                                    print(
                                        "CAPTCHA detected - solve in the browser "
                                        "(waiting 60s)…",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                                    sleep_ms(60_000)
                                    open_images_page(
                                        page, args.sl, args.tl, worker_id=worker_id
                                    )
                                elif attempt < 3:
                                    sleep_ms(2000)
                    finally:
                        set_active_lock(None)
                        queue_claim.release(job.rel)
                        current_rel = None

                    if not success:
                        fail += 1
                        # Screenshot + record ONLY on final failure (after all retries)
                        shot = None
                        if args.fail_shots:
                            try:
                                shot = save_failure_shot(
                                    page,
                                    failures_dir,
                                    job.rel,
                                    worker_id,
                                    reason=last_error,
                                    quality=args.fail_shot_quality,
                                )
                            except Exception as shot_err:
                                raise_if_browser_closed(shot_err, page)
                                logging.warning(
                                    "failure shot skipped: %s", shot_err
                                )
                        append_failure_record(
                            failures_dir,
                            job.rel,
                            worker_id,
                            reason=last_error,
                            attempts=attempts_used,
                            shot=shot,
                        )
                        queue_claim.set_cooldown(job.rel, worker_id)
                        logging.error(
                            "giving up on %s after %s tries - cooldown %ss",
                            job.rel.as_posix(),
                            attempts_used,
                            args.fail_cooldown,
                        )
                        try:
                            open_images_page(
                                page, args.sl, args.tl, worker_id=worker_id
                            )
                        except Exception as reopen_err:
                            raise_if_browser_closed(reopen_err, page)
                    else:
                        queue_claim.clear_cooldown(job.rel)

                    refresh_progress_bar(
                        progress, source_dir, translated_dir, args.only
                    )

                    if args.limit > 0 and claimed_count >= args.limit:
                        stop_loop = True
                        break

                    logging.debug("pause between jobs (delay=%s jitter=±%s%%)", args.delay, args.delay_jitter)
                    sleep_delay_ms(args.delay, args.delay_jitter)

                if stop_loop:
                    break
                if not progress_made:
                    logging.info("no claimable pending jobs left")
                    break
                sleep_ms(500)

            try:
                context.close()
            except Exception:
                pass
            context = None

    except BrowserClosed:
        stopped = True
        browser_closed_by_user = True
        logging.warning(
            "browser closed by user - stopping worker=%s cleanly", worker_id
        )
        print(
            f"{worker_window_title(worker_id)} closed - worker stopped",
            file=sys.stderr,
            flush=True,
        )
        if current_rel is not None:
            set_active_lock(None)
            queue_claim.release(current_rel)
            logging.info("released lock: %s", current_rel.as_posix())
            current_rel = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
            context = None
    except StopRequested:
        stopped = True
        logging.warning("graceful stop worker=%s", worker_id)
        if current_rel is not None:
            set_active_lock(None)
            queue_claim.release(current_rel)
            logging.info("released lock: %s", current_rel.as_posix())
            current_rel = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
    except KeyboardInterrupt:
        stopped = True
        logging.warning("KeyboardInterrupt - cleaning up")
        if current_rel is not None:
            set_active_lock(None)
            queue_claim.release(current_rel)
            current_rel = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
    finally:
        if progress is not None:
            refresh_progress_bar(progress, source_dir, translated_dir, args.only)
            progress.close()

    logging.info(
        "session summary worker=%s: ok=%s fail=%s claimed=%s stopped=%s browser_closed=%s",
        worker_id,
        ok,
        fail,
        claimed_count,
        stopped,
        browser_closed_by_user,
    )
    logging.info("session end")
    if browser_closed_by_user:
        # Not a failure - other workers keep going; this one just left.
        return 0
    if stopped:
        # Only release our own lock - never wipe siblings' claims.
        return 130
    return 0 if fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.workers > 1 and not args.worker_id:
            return launch_workers(args)
        if not args.worker_id:
            args.worker_id = "1"
        return run_worker(args)
    except StopRequested:
        print("Stopped.", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
