# playwright-image-translator

Batch-translate scanned images and documents through the **Google Translate → Images** web interface, fully automated with Playwright.

> Detailed guides: **[README.en.md](./README.en.md)** (English)

## Why this project exists

Google Translate is currently the best freely available tool for translating **images** and **whole pages**: its OCR + neural translation handles messy scans, mixed layouts, and rare languages (e.g. Georgian) far better than most alternatives — and it renders the translation directly on top of the original image.

The catch: **Google does not offer a public API for image translation.** The [image translation feature](https://translate.google.com/?op=images) exists only in the web UI, one file at a time, with a manual upload → wait → download loop. Translating a few hundred scanned book pages by hand is slow and mind-numbing.

This project closes that gap. It drives the real Google Translate web UI with Playwright and turns the manual loop into an unattended pipeline.

## What you get

- **Bulk, unattended translation** — drop images in a folder, come back to translated copies.
- **Mirrored output** — every result keeps the exact relative path and filename from the source tree.
- **Resumable** — already-translated files are skipped; stop and restart anytime.
- **Parallel workers** — run 1–32 browser windows sharing one atomic queue, so no file is processed twice.
- **Optional proxies** — round-robin HTTP(S) proxies per worker (with `direct` for your own IP); addresses are masked in the terminal and never leaked via process args.
- **Quality control** — downloaded files are checked and bad results are retried automatically.
- **Live progress** — a single progress bar with percent, speed and ETA; detailed logs go to disk.
- **Graceful lifecycle** — clean `Ctrl+C`, per-worker locks with heartbeat, cooldowns, and failure reports in `failures.jsonl`.

## How it works

1. Scan `data/source/` for images missing (or failing QC) in `data/translated/`.
2. A worker atomically claims a file via a lock in `runtime/queue/locks/`.
3. Chromium (Playwright) opens Google Translate Images and uploads the file.
4. It waits for the translated overlay, settles briefly, then downloads the result.
5. QC validates the output; up to 3 attempts, then a cooldown + `failures.jsonl` entry.
6. The lock is released and the next file is claimed.

> This automates the **public web UI**, not an official API. CAPTCHAs, rate limits, and UI changes are possible. Use responsibly and within Google's Terms of Service.


## Requirements

- **Python 3.11+**
- **Playwright** (Chromium) — automates the browser
- **Pillow** — used by `fix_suspicious.py`

## Installation

1. Clone the repository and navigate to the project folder:
```bash
git clone https://github.com/Tiarait/playwright-image-translator.git
cd playwright-image-translator
```

2. Set up a virtual environment and install Python dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
pip install -r requirements.txt
```

3. Install the required Playwright browser binaries:
```bash
playwright install chromium
```

4. Create your configuration files from templates:
```bash
cp config.example.json config.json
cp proxy.example.txt proxy.txt   # Optional, only if you use proxies
```

## Usage

1. Create the input directory and place your scanned images or documents there (subfolders are supported):
```bash
mkdir -p data/source
# Now put your images inside data/source/
```

2. Activate the virtual environment and run the translation script:
```bash
# Activate environment:
source .venv/bin/activate      # On macOS/Linux
# .venv\Scripts\activate       # On Windows

# Run the script:
python translate.py            # Uses workers, paths, and proxies from config.json
```


### Configuration

Settings are resolved as **CLI flags > `config.json` > built-in defaults**. Paths in the config are relative to the project root. 

*Note: `sl` stands for Source Language, and `tl` stands for Target Language (using ISO 639-1 codes, e.g., `"ka"` for Georgian, `"ru"` for Russian).*

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

For a full option reference, run: `python translate.py -h`. See **[README.en.md](./README.en.md)** for a detailed walkthrough of every key, proxies, the worker queue, logs, and troubleshooting.

### Workers and proxies

`--workers N` spawns N processes, each with its own Chromium profile (`runtime/browser-profiles/wN`) and a shared lock queue. 

* **Proxy Assignment**: If `proxy.txt` has valid entries, worker `i` gets slot `(i-1) % M` (round-robin style). Use the keyword `direct` to use your current local IP.
* **Security**: Proxy hosts are automatically masked in the terminal output for privacy (e.g., `203.***.***.10:3188`).

### Helper scripts

* **Check progress and stats:**
  ```bash
  python check.py [-v]
  ```
  Shows totals, remaining items, suspicious files, active locks, speed/ETA, and top failure reasons.

* **Fix quality control issues:**
  ```bash
  python fix_suspicious.py [--write]
  ```
  If you deliberately copied original images into the `translated/` directory, this script compresses them so they safely pass the size-based Quality Control (runs as a dry-run by default; use `--write` to apply changes).

## Built with

* [Playwright for Python](https://playwright.dev/python/) — Browser automation (Chromium)
* [Pillow](https://python-pillow.org/) — Image recompression and processing
* **Python Standard Library** — Built entirely using `argparse`, `logging`, `dataclasses`, and a custom terminal progress bar operating over an atomic file-based queue.
