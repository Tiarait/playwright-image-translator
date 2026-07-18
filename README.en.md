# playwright-image-translator (detailed guide)

Automated translation of images through the [Google Translate → Images](https://translate.google.com/?op=images) web interface using Playwright.

The script:

- takes files from `data/source/`;
- saves translations to `data/translated/` with the **same relative paths and names**;
- can run in multiple windows (workers) sharing one queue;
- optionally uses proxies from `proxy.txt`;
- writes detailed logs to disk and shows progress and errors in the terminal.

Short README: [`README.md`](./README.md) · Detailed Russian guide: [`README.ru.md`](./README.ru.md).

---

## Table of contents

1. [Project structure](#project-structure)
2. [`fix_suspicious.py` — accept copied originals](#fix_suspiciouspy--accept-copied-originals)
3. [How it works](#how-it-works)
4. [config.json — all keys](#configjson--all-keys)
5. [translate.py — all CLI options](#translatepy--all-cli-options)
6. [Proxies](#proxies)
7. [Workers and queue](#workers-and-queue)
8. [Logs and failures](#logs-and-failures)
9. [check.py — statistics](#checkpy--statistics)
10. [Stopping and failures](#stopping-and-failures)
11. [Common problems](#common-problems)
12. [Command examples](#command-examples)

---

## Project structure

```
python-image-translate/
├── translate.py              # run translation
├── check.py                  # statistics / ETA / problem list
├── fix_suspicious.py         # recompress intentionally-copied originals
├── config.example.json       # settings template (in git)
├── config.json               # your settings (usually not in git)
├── proxy.example.txt
├── proxy.txt                 # proxy list (optional)
├── requirements.txt
├── README.md                 # short
├── README.en.md              # this file
├── README.ru.md              # detailed Russian guide
│
├── imgtranslate/             # library (do not run directly)
│   ├── config.py             # load JSON + merge with CLI
│   ├── paths.py              # default paths
│   ├── proxy.py              # parse / validate proxies
│   ├── qc.py                 # quality control of the downloaded file
│   ├── queue_claim.py        # atomic queue locks
│   └── progress.py           # progress bar in the terminal
│
├── data/                     # DATA (your files)
│   ├── source/               # originals (subfolders allowed)
│   └── translated/           # results (mirrored paths)
│
└── runtime/                  # RUNTIME (created automatically)
    ├── logs/                 # session logs
    │   └── failures/         # failures.jsonl + JPEG screenshots
    ├── queue/
    │   ├── locks/            # which worker took which file
    │   └── cooldown/         # temporary ban after 3/3 fail
    └── browser-profiles/
        ├── w1/               # Chromium profile of worker 1
        ├── w2/
        └── …
```

### What goes where

| Folder | Purpose |
|--------|---------|
| `data/source/` | Source `.jpg` / `.jpeg` / `.png` / `.webp` |
| `data/translated/` | Finished translations — **do not edit by hand** while a session runs |
| `runtime/` | Temporary state: logs, locks, browser profiles |

The relative path is preserved:

```
data/source/Volume (19)/page-0001.jpg
  → data/translated/Volume (19)/page-0001.jpg
```

Already-translated files are **skipped** (never touched again).

---

## `fix_suspicious.py` — accept copied originals

If you copied originals into `translated/` on purpose (to skip translation), QC marks them **suspicious** (`same size as source`). This script recompresses those JPEGs so the size drops and QC passes.

```bash
python fix_suspicious.py              # dry-run
python fix_suspicious.py --write      # overwrite translated/ files
python fix_suspicious.py --write --only "Volume (25)"
```

| Flag | Meaning |
|------|---------|
| `--write` | Actually overwrite (without it: preview only) |
| `--quality N` | Start JPEG quality (default 75) |
| `--min-quality N` | Lower bound while searching (default 40) |
| `--only SUBSTR` | Only matching paths |

Needs Pillow: `pip install Pillow` (already in `requirements.txt`).

> Note: this only satisfies the size-based QC heuristic. It does **not** verify that the image was actually translated — use it only for files you intentionally left untranslated.

---

## How it works

1. The script scans `source/` and looks for files that are missing (or "bad") in `translated/`.
2. A worker atomically **claims** a file via a lock in `runtime/queue/locks/`.
3. It opens Google Translate Images in Chromium (Playwright).
4. It uploads the image, waits for the translation UI, pauses for `--download-settle`, then downloads the result.
5. **QC** checks the file: is it non-empty, is it not a copy of the original by size, etc.
6. If QC fails — up to 3 attempts on the same file. After 3/3 — a record in `failures.jsonl` (+ screenshot) and a cooldown.
7. The lock is released. The next file is taken.

Between files the page is usually **cleared (Clear)** without a full reload. A full reload happens if clear did not work, after an error, or on `--reload-every`.

Browser windows are titled **Worker 1**, **Worker 2**, … (tab title). In the macOS Dock the app may still be called "Google Chrome for Testing" — that is a Playwright limitation.

---

## config.json — all keys

Copy `config.example.json` → `config.json` and edit it.

**Priority:** CLI flags **override** `config.json` **which overrides** built-in defaults.

Paths in the config are **relative to the project root** (the folder that holds `translate.py`), not relative to the current terminal directory:

```json
"source": "./data/source"
```

Absolute paths are also allowed.

Keys starting with `_` and fields like `comment` are ignored.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `source` | path | `./data/source` | Originals folder |
| `translated` | path | `./data/translated` | Translations folder |
| `logs` | path | `./runtime/logs` | Session logs and `failures/` |
| `profiles` | path | `./runtime/browser-profiles` | Profiles `w1`, `w2`, … |
| `profile` | path | — | A single specific profile (usually not needed) |
| `proxy_file` | path | `./proxy.txt` | Proxy list |
| `require_proxy` | bool | `false` | Exit if there is no valid slot |
| `sl` | string | `ka` | Source language (Google code) |
| `tl` | string | `ru` | Target language |
| `workers` | int | `1` | Number of workers (1–32) |
| `quiet` | bool | `false` | Minimize browser windows |
| `headless` | bool | `false` | No window (often worse with CAPTCHA) |
| `verbose` | bool | `false` | INFO to terminal (otherwise progress + ERROR) |
| `delay` | int (ms) | `3000` | Pause between files |
| `delay_jitter` | float (%) | `0` | Random spread ±N% on `delay` (`25` → 3000±750 ms) |
| `timeout` | int (ms) | `120000` | Timeout waiting for translation/download |
| `download_settle` | int (ms) | `3500` | Pause **after** the UI is ready **before** Download |
| `reload_every` | int | `0` | Full reload every N successful jobs (`0` = only when clear fails) |
| `limit` | int | `0` | Max jobs **per process** (`0` = unlimited) |
| `only` | string | `""` | Filter: only paths containing this substring |
| `stale_lock` | int (s) | `900` | Reclaim a lock older than N seconds (hung worker) |
| `fail_cooldown` | int (s) | `180` | After 3/3 fail, do not take the file for N seconds |
| `fail_shots` | bool | `true` | JPEG screenshot on final failure |
| `no_fail_shots` | bool | — | Alternative: `true` = do not save screenshots |
| `fail_shot_quality` | int 1–100 | `35` | JPEG quality of the screenshot (viewport) |

Minimal `config.json` example:

```json
{
  "source": "./data/source",
  "translated": "./data/translated",
  "logs": "./runtime/logs",
  "profiles": "./runtime/browser-profiles",
  "proxy_file": "./proxy.txt",
  "sl": "ka",
  "tl": "ru",
  "workers": 3,
  "quiet": true,
  "delay": 5000,
  "download_settle": 4500
}
```

A different config for one run:

```bash
python translate.py --config /path/to/other.json
```

---

## translate.py — all CLI options

All flags override `config.json`.

| Option | Description |
|--------|-------------|
| `--config FILE` | JSON settings |
| `--source DIR` | Originals |
| `--translated DIR` | Translations |
| `--logs DIR` | Logs |
| `--profiles DIR` | Directory of `wN` profiles |
| `--profile DIR` | Profile for this process |
| `--proxy-file FILE` | Proxy file |
| `--proxy URL` | A single proxy / `direct` for this process |
| `--require-proxy` / `--no-require-proxy` | Require valid proxies |
| `--delay MS` | Pause between files |
| `--delay-jitter PCT` | Random ±P% on delay (0 = fixed) |
| `--timeout MS` | Wait timeout |
| `--download-settle MS` | Pause before clicking Download |
| `--reload-every N` | Reload every N successes |
| `--limit N` | Job limit per process |
| `--only SUBSTR` | Filter by path |
| `--sl` / `--tl` | Languages |
| `--headless` / `--no-headless` | No window / with window |
| `--quiet` / `--no-quiet` | Minimize windows |
| `-v` / `--verbose` | INFO to terminal |
| `--progress` / `--no-progress` | Progress bar |
| `--fail-shots` / `--no-fail-shots` | Screenshots of final failures |
| `--fail-shot-quality N` | JPEG quality 1–100 |
| `--workers N` | Number of workers 1–32 |
| `--worker-id ID` | Internal (set by the launcher) |
| `--stale-lock SEC` | Age of a "stale" lock |
| `--fail-cooldown SEC` | Cooldown after 3/3 |

Help: `python translate.py -h`

### What you see in the terminal

By default:

- a progress line with a bar and %:
  `translate  45.1%|██████████░░░░░░░░░░░░░░| 110/244 [07:35, ~7.86s/img = ~17:32]`
- **ERROR** on the final failure of a file (after 3 attempts);
- a short message on CAPTCHA;
- at multi-worker start: `worker 1 → …` (without the long command line).

Detailed INFO/WARNING goes only to the **log file**, or to the terminal with `-v`.

---

## Proxies

The `proxy.txt` file (template — `proxy.example.txt`) is **optional**.

Format — one entry per line, comments after `#`:

```
203.0.113.10:3188 # Main
http://198.51.100.20:8080
https://user:pass@198.51.100.30:3128
direct            # current IP, no proxy
```

`direct` synonyms: `none`, `local`, `no-proxy`, `-`.

Before start, every **real** proxy is checked (TCP + HTTP probe). Broken lines are skipped. `direct` always counts as a valid slot.

In the terminal the address is masked, for example:

```text
worker 1 → http://203.***.***.10:3188 (Main)
```

The full address is used only to connect and is not printed next to the worker. When launching child workers the proxy is also not passed via command-line arguments (so the login/password are not visible in the process list).

### Assigning slots to workers

Line order = slots. Worker `i` gets slot `(i - 1) % M` (round-robin).

**3 workers, 2 proxies + direct:**

```
proxy1
proxy2
direct
```

| Worker | Slot |
|--------|------|
| Worker 1 | proxy1 |
| Worker 2 | proxy2 |
| Worker 3 | current IP |

**3 workers, only 2 proxies (no direct):** Worker 3 gets proxy1 again.

If the file is missing / empty — everyone runs without a proxy (unless `require_proxy: true`).

---

## Workers and queue

```bash
python translate.py --workers 3
# or in config.json: "workers": 3
```

- Each worker is a separate process + its own profile `runtime/browser-profiles/wN`.
- The queue is shared: a lock in `runtime/queue/locks/`.
- One file is never processed by two workers at once.
- `--limit` applies **per** process (e.g. `--limit 5 --workers 2` ≈ up to 10 files total).

### Queue (`runtime/queue/`)

| Folder | Meaning |
|--------|---------|
| `locks/` | The file is currently being worked on by a worker |
| `cooldown/` | After a final fail the file is temporarily not taken |

Behavior:

- After a successful/unsuccessful job the lock is **released**.
- While a file is in progress, the lock is refreshed periodically (heartbeat) so a long job is not reclaimed via `stale_lock`.
- On `Ctrl+C` each worker releases **only its own** lock; the launcher cleans remaining `locks/` after all workers stop.
- Workers run in a separate process group: the first `Ctrl+C` stops gracefully via the launcher, the second forces exit.
- `cooldown` is **not** cleared on stop — it protects against instantly retrying a bad file (it lives for `fail_cooldown` seconds).
- If a process was killed hard, a lock may remain up to `stale_lock` seconds, then another worker may reclaim it.
- If you **manually closed a window** of one worker — that worker releases its lock and exits; the others continue.

Manual lock cleanup (if stuck):

```bash
rm -f runtime/queue/locks/*.lock
```

---

## Logs and failures

### Session logs

`runtime/logs/translate_YYYYMMDD_HHMMSS_wN.log`

- full DEBUG/INFO/WARNING/ERROR;
- upload, retry, QC, proxy, stop, etc.

A name with `_w1`, `_w2` tells which worker the log is from.

### Failures

Directory: `runtime/logs/failures/`

| File | When |
|------|------|
| `failures.jsonl` | Only after **3/3** failures on a file |
| `*.jpg` | Viewport screenshot on final failure (if `fail_shots`) |

Intermediate `try 1/3`, `try 2/3` (often "downloaded too early") go **only to the session log**, without failures.

A line in `failures.jsonl` (JSON):

```json
{
  "time": "2026-07-16T22:10:01",
  "worker": "2",
  "file": "Volume (19)/page-0012.jpg",
  "attempts": 3,
  "reason": "QC failed: same size as source",
  "screenshot": "20260716_221001_w2_....jpg"
}
```

Common QC reasons:

| Reason | Meaning |
|--------|---------|
| `same size as source` | Downloaded the original / translation not ready yet |
| `size almost identical to source` | Looks like the original |
| `suspiciously large (likely original)` | Too similar to a heavy scan |
| `too small` / `not a valid image header` | Corrupted or empty file |
| Timeout / CAPTCHA | Google did not respond / block |

Increasing `download_settle` (e.g. `4500`–`6000`) often helps.

---

## check.py — statistics

```bash
python check.py
python check.py -v
```

Shows:

- how many total / translated / remaining;
- missing and suspicious (bad QC);
- active locks and cooldowns;
- speed (~s/file) and ETA;
- top reasons from `failures.jsonl`.

With `-v` — file lists and the latest errors.

| Option | Description |
|--------|-------------|
| `--config` | Takes `source` / `translated` / `logs` |
| `--source` / `--translated` | Override paths |
| `--failures` | Folder with `failures.jsonl` |
| `-v` / `--verbose` | Detailed lists |

`check.py` output is in English (like the rest of the CLI).

---

## Stopping and failures

| Action | Behavior |
|--------|----------|
| `Ctrl+C` once | Graceful exit: lock released, browser closed, no traceback |
| `Ctrl+C` twice | Forced exit |
| Closed a Worker N window | That worker stops; the others continue |
| CAPTCHA | Message in the terminal; solve it in the browser window |

Do not Kill the process unless necessary — prefer `Ctrl+C`.

---

## Common problems

**"Did not translate on the first try" / QC same size**
Google has not drawn the overlay yet, or OCR did not find text ("Text not found…"). The script retries up to 3 times and does **not** download while it sees this message. Increase `download_settle`. `delay_jitter` helps look less like a robot, but has almost no direct effect on QC.

**"Text not found. It may be in an unsupported language."**
A Google message: OCR did not see text or is unsure of the language. On Georgian scans (`sl=ka`) it happens on empty/blurry fragments or while processing is still running. It often coincides with `QC failed: same size as source` — the original was downloaded too early. Check `sl`/`tl`, scan quality, and increase `download_settle`.

**Two windows on one file**
Usually this is a retry by the same worker (`retry 2/3`), not a parallel claim. Parallel access is blocked by the lock.

**CAPTCHA / block**
Solve it manually in the window. `headless` catches blocks more often. Different proxies and profiles help.

**A lock hangs after a crash**
Wait for `stale_lock` or delete `runtime/queue/locks/*.lock`.

**Progress "stalls"**
All remaining files are in cooldown or already claimed. See `python check.py -v`.

**The window is called Chrome for Testing**
The tab title is `Worker N`. Playwright does not change the app name in the Dock.

---

## Command examples

```bash
# Everything from config.json
python translate.py

# 3 workers, minimized window, no extra INFO in the terminal
python translate.py --workers 3 --quiet

# One worker, verbose terminal, test only
python translate.py --workers 1 --no-quiet -v --limit 2

# Only files with a substring in the path
python translate.py --only "Volume (19)"

# Longer pause before Download
python translate.py --download-settle 6000 --delay 5000

# Without failure screenshots
python translate.py --no-fail-shots

# Statistics
python check.py
python check.py -v
```

---

## Notes

- This is **web-UI** automation, not an official Google API. CAPTCHAs, limits, and layout changes are possible.
