"""Terminal progress bar. Detailed logs stay in log files."""

from __future__ import annotations

import sys
import time

_BAR_WIDTH = 28


def _fmt_clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _render_bar(pct: float, width: int = _BAR_WIDTH) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "█" * filled + "░" * (width - filled)


class ProgressBar:
    """Rate/ETA use only files finished in THIS session (not pre-existing done).

    Elapsed wall time starts when the bar is created. Rate/ETA ignore browser
    startup: the clock for rate starts at the first session completion, and
    speed is measured from the interval between completions.

    Example: translate  87.7%|████████████████████████░░░░| 214/244 [00:51, ~18.50s/img = ~09:15]
    """

    def __init__(self, total: int, done: int = 0, *, desc: str = "translate") -> None:
        self._desc = desc
        self._wall_started = time.time()
        self._first_done_at: float | None = None
        self._total = max(int(total), 0)
        self._done = max(0, min(int(done), self._total)) if self._total else 0
        # Files already finished before this run - exclude from rate math
        self._baseline_done = self._done
        self._last_line = ""
        self.refresh_counts(self._total, self._done)

    def refresh_counts(self, total: int, done: int) -> None:
        self._total = max(int(total), 0)
        self._done = max(0, min(int(done), self._total)) if self._total else 0
        if self._baseline_done > self._done:
            self._baseline_done = self._done

        now = time.time()
        session_done = max(self._done - self._baseline_done, 0)
        if session_done > 0 and self._first_done_at is None:
            self._first_done_at = now

        elapsed = now - self._wall_started
        elapsed_s = _fmt_clock(elapsed)
        pct = (self._done / self._total * 100.0) if self._total else 100.0
        left_imgs = max(self._total - self._done, 0)

        # Need at least 2 session completions so startup time is excluded.
        if (
            session_done >= 2
            and self._first_done_at is not None
            and now > self._first_done_at
        ):
            rate = (now - self._first_done_at) / (session_done - 1)
            eta = rate * left_imgs
            rate_s = f"~{rate:.2f}s/img"
            eta_s = f"~{_fmt_clock(eta)}"
        else:
            rate_s = "~?s/img"
            eta_s = "~?:??"

        bar = _render_bar(pct)
        line = (
            f"{self._desc} {pct:5.1f}%|{bar}| "
            f"{self._done}/{self._total} [{elapsed_s}, {rate_s} = {eta_s}]"
        )
        if line != self._last_line:
            self._last_line = line
            sys.stderr.write("\r" + line + " " * 4)
            sys.stderr.flush()

    def close(self) -> None:
        if self._last_line:
            sys.stderr.write("\r" + self._last_line + "\n")
            sys.stderr.flush()
