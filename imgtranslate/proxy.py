"""Load and validate HTTP(S) proxies from proxy.txt."""

from __future__ import annotations

import logging
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .paths import PROXY_FILE, ROOT

# host:port  or  http(s)://[user:pass@]host:port  (+ optional # comment)
_LINE_RE = re.compile(
    r"^(?:"
    r"(?P<scheme>https?)://(?:(?P<user>[^:@\s]+):(?P<password>[^@\s]+)@)?"
    r"(?P<host1>[^:/\s]+):(?P<port1>\d+)"
    r"|"
    r"(?P<host2>[^:/\s]+):(?P<port2>\d+)"
    r")"
    r"(?:\s*(?:#|//).*)?$",
    re.I,
)


@dataclass(frozen=True)
class Proxy:
    host: str
    port: int
    scheme: str = "http"
    username: str | None = None
    password: str | None = None
    label: str = ""
    direct: bool = False  # True = current IP, no proxy

    @classmethod
    def local(cls, label: str = "") -> Proxy:
        return cls(
            host="",
            port=0,
            scheme="direct",
            label=label or "current IP",
            direct=True,
        )

    @property
    def server(self) -> str:
        if self.direct:
            return "direct"
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def display(self) -> str:
        if self.direct:
            return f"direct ({self.label})" if self.label else "direct (current IP)"
        auth = f"{self.username}:***@" if self.username else ""
        base = f"{self.scheme}://{auth}{self.host}:{self.port}"
        return f"{base} ({self.label})" if self.label else base

    @property
    def masked_display(self) -> str:
        """Proxy label for terminal output without exposing the full host."""
        if self.direct:
            return self.display

        host = self.host
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
            parts = host.split(".")
            masked_host = f"{parts[0]}.***.***.{parts[-1]}"
        elif ":" in host:
            # IPv6: retain only the first and final groups.
            parts = host.split(":")
            masked_host = f"{parts[0]}:***:{parts[-1]}"
        elif "." in host:
            # Domain: retain the first character and public-looking suffix.
            parts = host.split(".")
            first = (parts[0][:1] or "*") + "***"
            suffix = ".".join(parts[-2:]) if len(parts) >= 2 else ""
            masked_host = f"{first}.{suffix}" if suffix else first
        else:
            masked_host = (host[:1] or "*") + "***"

        base = f"{self.scheme}://{masked_host}:{self.port}"
        return f"{base} ({self.label})" if self.label else base

    def playwright_dict(self) -> dict | None:
        if self.direct:
            return None
        out: dict = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    def cli_value(self) -> str:
        """Compact form for --proxy."""
        if self.direct:
            return "direct"
        if self.username and self.password:
            return f"{self.scheme}://{self.username}:{self.password}@{self.host}:{self.port}"
        return self.server


_DIRECT_NAMES = frozenset(
    {"direct", "none", "local", "no-proxy", "noproxy", "off", "-"}
)


def parse_proxy_line(line: str) -> Proxy | None:
    raw = line.strip()
    if not raw or raw.startswith("#") or raw.startswith("//"):
        return None

    label = ""
    if "#" in raw:
        raw, _, comment = raw.partition("#")
        label = comment.strip()
        raw = raw.strip()
    elif "//" in raw and not raw.lower().startswith(("http://", "https://")):
        raw, _, comment = raw.partition("//")
        label = comment.strip()
        raw = raw.strip()

    if raw.lower() in _DIRECT_NAMES:
        return Proxy.local(label=label or "current IP")

    m = _LINE_RE.match(raw)
    if not m:
        # Also accept bare URL with path stripped
        try:
            u = urlparse(raw if "://" in raw else f"http://{raw}")
            if u.hostname and u.port:
                return Proxy(
                    host=u.hostname,
                    port=u.port,
                    scheme=(u.scheme or "http").lower(),
                    username=u.username,
                    password=u.password,
                    label=label,
                )
        except Exception:
            pass
        return None

    host = m.group("host1") or m.group("host2")
    port_s = m.group("port1") or m.group("port2")
    scheme = (m.group("scheme") or "http").lower()
    return Proxy(
        host=host,
        port=int(port_s),
        scheme=scheme,
        username=m.group("user"),
        password=m.group("password"),
        label=label,
    )


def load_proxy_lines(path: Path) -> list[tuple[int, str, Proxy | None]]:
    """Return (lineno, raw, parsed|None) for every non-empty content line."""
    rows: list[tuple[int, str, Proxy | None]] = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows.append((i, line.rstrip(), parse_proxy_line(line)))
    return rows


def tcp_alive(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_via_proxy(proxy: Proxy, timeout: float = 8.0) -> bool:
    """GET a tiny URL through the proxy; success = reachable + speaks HTTP proxy."""
    if proxy.direct:
        return True
    handler = urllib.request.ProxyHandler(
        {"http": proxy.server, "https": proxy.server}
    )
    # Basic auth for proxy if needed
    if proxy.username and proxy.password:
        # urllib ProxyHandler doesn't embed auth well for all; put in URL
        auth_server = (
            f"{proxy.scheme}://{proxy.username}:{proxy.password}"
            f"@{proxy.host}:{proxy.port}"
        )
        handler = urllib.request.ProxyHandler(
            {"http": auth_server, "https": auth_server}
        )
    opener = urllib.request.build_opener(handler)
    # Prefer plain HTTP target - HTTPS CONNECT may fail on some cheap proxies
    try:
        with opener.open("http://example.com/", timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        try:
            with opener.open("http://httpbin.org/status/204", timeout=timeout) as resp:
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False


def validate_proxy(proxy: Proxy, timeout: float = 4.0) -> tuple[bool, str]:
    if proxy.direct:
        return True, "direct (no proxy)"
    if not (1 <= proxy.port <= 65535):
        return False, "bad port"
    if not tcp_alive(proxy.host, proxy.port, timeout=timeout):
        return False, "TCP connect failed"
    if http_via_proxy(proxy, timeout=max(timeout, 6.0)):
        return True, "ok"
    # TCP open is already useful; many proxies still work in Chromium
    # even if urllib probe is picky - mark as soft-ok
    return True, "TCP ok (HTTP probe inconclusive)"


def resolve_proxy_file(path: Path | None = None) -> Path:
    p = path or PROXY_FILE
    p = Path(p).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return p


def load_valid_proxies(
    path: Path | None = None,
    *,
    timeout: float = 4.0,
) -> tuple[list[Proxy], Path | None]:
    """
    Read proxy file if present. Returns (valid_proxies, path_or_None).
    Missing file → ([], None). Present but empty/invalid → ([], path).
    """
    cfg_path = resolve_proxy_file(path) if path is not None else resolve_proxy_file()
    if path is None and not cfg_path.is_file():
        return [], None
    if not cfg_path.is_file():
        logging.warning("proxy file not found: %s", cfg_path)
        return [], cfg_path

    rows = load_proxy_lines(cfg_path)
    if not rows:
        logging.info("proxy file empty: %s", cfg_path)
        return [], cfg_path

    valid: list[Proxy] = []
    for lineno, raw, parsed in rows:
        if parsed is None:
            logging.warning("proxy.txt:%s contains an unparseable proxy entry", lineno)
            continue
        ok, reason = validate_proxy(parsed, timeout=timeout)
        if ok:
            logging.info("proxy OK [%s]: %s", reason, parsed.masked_display)
            valid.append(parsed)
        else:
            logging.warning("proxy BAD [%s]: %s", reason, parsed.masked_display)

    return valid, cfg_path


def parse_proxy_cli(value: str) -> Proxy:
    p = parse_proxy_line(value)
    if p is None:
        raise SystemExit(f"Invalid --proxy value: {value}")
    return p


def assign_proxy(proxies: list[Proxy], worker_index: int) -> Proxy | None:
    """1-based worker index → proxy (round-robin). None if no proxies."""
    if not proxies:
        return None
    return proxies[(worker_index - 1) % len(proxies)]
