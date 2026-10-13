#!/usr/bin/env python3
"""
Proof of Concept: Instagram posts indexed in GOOGLE, no login.

    Google SERP  --(dork site:instagram.com/p/ + start=N)-->   indexed URLs
                                                                    |
                                                             shortcodes
                                                                    |
        https://www.instagram.com/p/{code}/embed/captioned/  -->  data + photo

Why Google and not DuckDuckGo (`ig_serp_scraper.py`):

  * Google SUPPORTS real pagination with `start=0,100,200...` and `num=100`.
    DDG does not: `&s=10` returns HTTP 202. This is the structural advantage.
  * Google indexes far more Instagram content than DDG, so the ceiling of
    ~10 shortcodes per query is lifted.

What was measured in practice from a datacenter IP (see `findings()`):

  1. The HTTP path (curl_cffi with Chrome TLS) returns the Google SERP as a
     SHELL that requires JavaScript: ~92 KB, 3 anchors, zero results.
     It is a 200, not an error, so a naive scraper reports "0 results"
     when it actually never saw the SERP. `JS_SHELL_MARKERS` detects it.
  2. With headless Chromium (`--render`) the DOM does render, but Google
     answers with a CAPTCHA ("unusual traffic") on the fly for a datacenter IP.
     `CAPTCHA_MARKERS` detects it.
  3. Conclusion: Google is not accessible from a datacenter/center data-center
     IP. A RESIDENTIAL or MOBILE proxy with good reputation is required.
     `--proxy-url` already supports it; without a proxy, the script fails
     with an explicit message instead of pretending it found nothing.

The TLDs (`google.es`, `.com.ar`, `.co.uk`, `.ca`, `.mx`) respond identically:
the block is by IP, not by domain. `--tld` only serves to adjust the region.

Usage:
    # Requires a residential proxy; without it Google answers CAPTCHA.
    python ig_google_scraper.py --query "running shoes" --limit 30 \
        --proxy-url http://user:pass@host:port

    # Real render with Chromium (in addition to the proxy, for when the JS shell appears)
    python ig_google_scraper.py --query sneakers --limit 20 --render

    # Without Google: already-known shortcodes -> goes straight to the embed
    python ig_google_scraper.py --query sneakers --shortcodes DYHxVb0iEI2,DceGJtwimbX
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional, Protocol
from urllib.parse import quote_plus, unquote

from ig_store_factory import make_store  # noqa: E402

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Missing beautifulsoup4 ->  pip install -r requirements.txt")

try:
    from pydantic import BaseModel, Field, field_validator  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Missing pydantic ->  pip install -r requirements.txt")


LOG: logging.Logger = logging.getLogger("google_scraper")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Gecko/20100101 Firefox/128.0"
)
EMBED_URL: Final[str] = "https://www.instagram.com/p/{shortcode}/embed/captioned/"

# Instagram shortcode. Google wraps links in /url?q=..., and sometimes they
# come percent-encoded, hence `unquote` is applied before applying the pattern.
RE_SHORTCODE: Final[re.Pattern[str]] = re.compile(
    r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_\-]{10,12})(?![A-Za-z0-9_\-])"
)
RE_AUTHOR_LINK: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/(?:\?|$)"
)
RE_AUTHOR_ALT: Final[re.Pattern[str]] = re.compile(
    r"Instagram post shared by @([A-Za-z0-9._]+)"
)
# Author avatar: it is never the post photo.
RE_AVATAR_CDN: Final[re.Pattern[str]] = re.compile(r"/t51\.2885-19/")
RE_COMMENTS_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s*View all ([\d.,]+[KkMm]? )?comments?\.?\s*$", re.IGNORECASE
)

SOFT_404_MARKERS: Final[tuple[str, ...]] = (
    "may be broken, or the post may have been removed",
    "This page isn't available",
    "Sorry, this page isn't available",
)
# Google answers 200 with an automated-traffic notice, not 429.
# CAREFUL: "/sorry/index" is NOT used as a marker. That path appears linked in
# a normal SERP, and it produced a false positive that made us wait for a
# nonexistent captcha (measured: legitimate SERP with 11 shortcodes flagged as
# blocked). Precise detection of the path is done separately with `location.pathname`.
CAPTCHA_MARKERS: Final[tuple[str, ...]] = (
    "our systems have detected unusual traffic",
    "detected unusual traffic",
    "systems have detected",
    "captcha-form",
    "are you a robot",
    "to continue, please type the characters",
)
# Google serves the SERP as a shell that requires JS. A 200 with these markers
# means "I never saw the SERP", which is different from "there are no results".
JS_SHELL_MARKERS: Final[tuple[str, ...]] = (
    "enable javascript on your web browser",
    "before you continue",
    "javascript is disabled",
)
# A real Google SERP brings dozens of result anchors even when it finds
# nothing. The shells and the CAPTCHA bring 3-5. Conservative threshold.
MIN_RESULT_ANCHORS: Final[int] = 10

# Dork rotation. Each template returns a different set, so it is
# the complementary mechanism when `start` pagination runs out.
DORK_TEMPLATES: Final[tuple[str, ...]] = (
    'site:instagram.com/p/ "{kw}"',
    'site:instagram.com/reel/ "{kw}"',
    "site:instagram.com {kw}",
    "site:instagram.com/p/ {kw}",
    "site:instagram.com {kw} review",
    "site:instagram.com {kw} haul",
    "site:instagram.com {kw} outfit",
    "site:instagram.com {kw} unboxing",
    "site:instagram.com {kw} best",
    "site:instagram.com {kw} collection",
    "site:instagram.com {kw} try on",
    "site:instagram.com {kw} new",
    "site:instagram.com {kw} routine",
    "site:instagram.com {kw} outfit ideas",
)


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
class InstagramPost(BaseModel):
    shortcode: str
    author: Optional[str] = None
    caption: Optional[str] = None
    image_url: Optional[str] = None
    local_image_path: Optional[str] = None
    # SERP URL that revealed this shortcode: discovery traceability.
    source_serp_url: str
    scraped_at: datetime
    is_video: bool = False
    source_engine: str = "google"

    # --- Enriched metadata (embed GraphQL blob) ---
    # It comes in the `contextJSON`. It is null when Instagram serves the "light"
    # embed (isRichEmbed=false), which was measured in 2 of 12 posts.
    rich_metadata: bool = False
    media_id: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    author_id: Optional[int] = None
    author_followers: Optional[int] = None
    author_verified: Optional[bool] = None
    width: Optional[int] = None
    height: Optional[int] = None
    # Alt-text that Instagram generates. Very useful for semantic filtering.
    alt_text: Optional[str] = None
    carousel_count: int = 0
    carousel_shortcodes: list[str] = Field(default_factory=list)

    # INFERRED date: decoded from the media ID, not an official field.
    # The embed no longer exposes `taken_at` in any variant without login.
    post_published_at: Optional[datetime] = None
    post_published_at_inferred: bool = False

    @field_validator("scraped_at", "post_published_at")
    @classmethod
    def _ensure_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class ScrapeStats(BaseModel):
    queries_executed: int = 0
    blocked_queries: int = 0
    js_shell_queries: int = 0
    # A 429 is NOT the same as "no results": they are separate counters on
    # purpose, because the final message has to tell the truth.
    rate_limited_queries: int = 0
    discovered: int = 0
    processed: int = 0
    images_saved: int = 0
    # A post can end up `ok` with its image missing. That case used to be
    # indistinguishable from a complete post; `images_failed` makes it explicit.
    images_failed: int = 0
    skipped_unavailable: int = 0
    failed: int = 0


class MetadataFile(BaseModel):
    query: str
    engine: str = "google"
    requested_limit: int
    method: str = "Google SERP (dorks + start= pagination) + public embed (no auth)"
    rendered: bool = False
    proxy_used: bool = False
    scraped_at: datetime
    stats: ScrapeStats
    posts: list[InstagramPost]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """Recoverable failure on a download."""


class NotFoundError(FetchError):
    """HTTP 404 - non-existent resource."""


class RateLimitedError(FetchError):
    """HTTP 429 - rate limit."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """`Retry-After` in seconds, or HTTP date (RFC 7231). Returns seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when is None:
            return None
        from datetime import timezone as _tz

        if when.tzinfo is None:
            when = when.replace(tzinfo=_tz.utc)
        return max(0.0, (when - datetime.now(_tz.utc)).total_seconds())
    except Exception:  # noqa: BLE001 - malformed header: ignored
        return None


class CaptchaError(FetchError):
    """Google answered with human verification / IP reputation block."""


class JsShellError(FetchError):
    """Google returned the shell that requires JavaScript, not the SERP."""


class IpBlockedError(FetchError):
    """No query could be completed due to an IP block."""


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #
def _headers(engine: str) -> dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    if engine == "google":
        h["Referer"] = "https://www.google.com/"
    else:
        h["Referer"] = "https://www.instagram.com/"
    return h


class HttpClient(Protocol):
    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]: ...
    def get_bytes(self, url: str) -> bytes: ...
    def close(self) -> None: ...


class CurlCffiClient:
    """Transport with Chrome TLS impersonation (real JA3/JA4)."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0,
                 cookies: Optional[dict[str, str]] = None) -> None:
        from curl_cffi import requests as cc  # lazy import

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._s = cc.Session(impersonate="chrome", proxies=proxies, timeout=timeout)
        if cookies:
            # NID/SNID/GOOGLE_ABUSE_EXEMPTION: without these cookies Google serves
            # the JS shell or the CAPTCHA even if the TLS is perfect (measured).
            self._s.cookies.update(cookies)

    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]:
        r = self._s.get(url, params=params, headers=_headers(engine), allow_redirects=True)
        self._check(r.status_code, url, r.headers.get("Retry-After"))
        return r.status_code, r.text

    def get_bytes(self, url: str) -> bytes:
        r = self._s.get(url, headers=_headers("embed"), allow_redirects=True, timeout=30)
        self._check(r.status_code, url, r.headers.get("Retry-After"))
        return r.content

    @staticmethod
    def _check(status: int, url: str, retry_after: Optional[str] = None) -> None:
        if status == 429:
            raise RateLimitedError(f"HTTP 429 on {url}", parse_retry_after(retry_after))
        if status == 404:
            raise NotFoundError(f"404 on {url}")
        if status >= 400:
            raise FetchError(f"HTTP {status} on {url}")

    def close(self) -> None:
        self._s.close()


class HttpxClient:
    """Fallback if curl_cffi is not available."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0,
                 cookies: Optional[dict[str, str]] = None) -> None:
        import httpx  # lazy import

        self._s = httpx.Client(timeout=timeout, follow_redirects=True, proxy=proxy_url)
        if cookies:
            self._s.cookies.update(cookies)

    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]:
        r = self._s.get(url, params=params, headers=_headers(engine))
        CurlCffiClient._check(r.status_code, url, r.headers.get("Retry-After"))
        return r.status_code, r.text

    def get_bytes(self, url: str) -> bytes:
        r = self._s.get(url, headers=_headers("embed"), timeout=30)
        CurlCffiClient._check(r.status_code, url, r.headers.get("Retry-After"))
        return r.content

    def close(self) -> None:
        self._s.close()


def build_client(proxy_url: Optional[str], timeout: float,
                 cookies: Optional[dict[str, str]] = None) -> HttpClient:
    if proxy_url:
        LOG.info("Proxy active: %s", re.sub(r"//[^@/]*@", "//***:***@", proxy_url))
    try:
        return CurlCffiClient(proxy_url, timeout, cookies)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("curl_cffi not available (%s); using httpx", exc)
        return HttpxClient(proxy_url, timeout, cookies)


# --------------------------------------------------------------------------- #
# Rendering with Chromium (for when Google returns the JS shell)
# --------------------------------------------------------------------------- #
CHROMIUM_CANDIDATES: Final[tuple[str, ...]] = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome",
)


def find_chromium() -> Optional[str]:
    for name in CHROMIUM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def render_dom(url: str, proxy_url: Optional[str] = None,
               budget_ms: int = 20000) -> str:
    """Renders the URL with headless Chromium and returns the resulting DOM.

    Chromium has its own anti-bot stack: some fingerprints pass and others
    trigger the CAPTCHA anyway. It is not a substitute for the proxy, only a complement.
    """
    binary = find_chromium()
    if not binary:
        raise FetchError(
            "Chromium/Chrome not found for --render. Install it or use the HTTP path."
        )
    profile = tempfile.mkdtemp(prefix="iggoogle-")
    cmd = [
        binary, "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}", "--disable-extensions",
        f"--virtual-time-budget={budget_ms}", "--dump-dom", url,
    ]
    if proxy_url:
        cmd.insert(-1, f"--proxy-server={proxy_url}")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=budget_ms / 1000 + 60)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"Chromium took too long: {exc}") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    dom = proc.stdout.decode("utf-8", errors="replace")
    if not dom.strip():
        raise FetchError("Chromium returned no DOM (possible crash or block).")
    return dom


# --------------------------------------------------------------------------- #
# Interactive mode: real Chrome, with a window, controlled via CDP
# --------------------------------------------------------------------------- #
def find_wayland_socket() -> Optional[tuple[str, str]]:
    """Looks for a system Wayland socket. Returns (WAYLAND_DISPLAY, XDG_RUNTIME_DIR).

    It is needed because in an agent shell the compositor variables are
    usually not exported, and X11 without `XAUTHORITY` rejects the connection
    with "Authorization required" (measured on niri/Xwayland).
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        return os.environ["WAYLAND_DISPLAY"], os.environ.get("XDG_RUNTIME_DIR", "")
    for runtime in Path("/run/user").glob("*"):
        if not runtime.is_dir():
            continue
        for sock in runtime.glob("wayland-*"):
            return sock.name, str(runtime)
    return None


class CdpBrowser:
    """Real headful Chrome, controlled through the Chrome DevTools Protocol.

    This is the only path where a person can solve the CAPTCHA: Google serves it
    as a normal page, so it is enough to open the window, do the challenge by hand
    and let the script keep reading the DOM.

    A persistent profile is used so the cookies survive between runs: once the
    CAPTCHA is solved, subsequent queries do not ask for it again.
    """

    def __init__(self, profile_dir: Optional[str] = None, port: int = 9222,
                 proxy_url: Optional[str] = None, wait_captcha: int = 180) -> None:
        self._binary = find_chromium()
        if not self._binary:
            raise FetchError("Chromium/Chrome not found for --interactive.")
        self._profile = profile_dir or str(Path.home() / ".ig_google_profile")
        self._port = port
        self._proxy = proxy_url
        self._wait_captcha = wait_captcha
        self._proc: Optional[subprocess.Popen] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        Path(self._profile).mkdir(parents=True, exist_ok=True)
        wayland = find_wayland_socket()

        cmd = [
            self._binary, "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,OptimizationHints",
            f"--user-data-dir={self._profile}",
            f"--remote-debugging-port={self._port}",
        ]
        if self._proxy:
            cmd.append(f"--proxy-server={self._proxy}")

        # Wayland is preferred: the session may be X11 with an inaccessible
        # XAUTHORITY, in which case Chrome dies with "Authorization required, but no
        # authorization protocol specified". Wayland does not need that cookie.
        log = Path(tempfile.gettempdir()) / "ig_google_chrome.log"
        attempts: list[tuple[dict[str, str], list[str]]] = []
        if wayland:
            wl_display, runtime_dir = wayland
            wl_env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
            wl_env["WAYLAND_DISPLAY"] = wl_display
            if runtime_dir:
                wl_env["XDG_RUNTIME_DIR"] = runtime_dir
            attempts.append((wl_env, ["--ozone-platform=wayland"]))
        # Fallback: X11 as is, in case it works in some other environment.
        attempts.append((dict(os.environ), []))

        for env, extra in attempts:
            with open(log, "wb") as lf:
                proc = subprocess.Popen(
                    [*cmd, *extra, "about:blank"],
                    env=env, stdout=lf, stderr=lf,
                )
            try:
                self._wait_ready(timeout=25)
                self._proc = proc
                LOG.info("Chrome ready (%s). Profile: %s",
                         "wayland" if extra else "x11", self._profile)
                return
            except FetchError as exc:
                proc.terminate()
                LOG.debug("  startup failed (%s): %s", extra or ["x11"], exc)

        tail = ""
        if log.exists():
            tail = log.read_text(errors="replace").strip().splitlines()[-3:]
        raise FetchError(
            "Chrome did not start. Last log lines:\n" + "\n".join(tail)
        )

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/version", timeout=2
                ) as r:
                    if r.status == 200:
                        return
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.5)
        raise FetchError("Chrome did not respond on the debugging port.")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # -- navigation -------------------------------------------------------
    async def _eval(self, tab_ws: Any, expression: str, asyncio: Any,
                    websockets: Any) -> Any:
        async with websockets.connect(tab_ws, max_size=64 * 1024 * 1024) as ws:
            mid = 0

            async def cmd(method: str, params: Optional[dict] = None) -> dict:
                nonlocal mid
                mid += 1
                await ws.send(json.dumps(
                    {"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        return msg.get("result", {})

            await cmd("Network.enable")
            await cmd("Page.enable")
            await cmd("Runtime.enable")
            return await cmd("Runtime.evaluate",
                             {"expression": expression, "returnByValue": True})

    def solve_and_export(self, probe_url: str) -> dict[str, str]:
        """Opens the search, waits for the user to solve the CAPTCHA and
        returns the Google cookies so they can be reused over HTTP.

        This is the piece that makes everything else viable: without a real browser
        that solves the challenge, Google returns a JS shell or a CAPTCHA. With the
        resulting cookies, curl_cffi alone already receives the full SERP.
        """
        try:
            import asyncio  # lazy import

            import websockets  # type: ignore
        except ImportError:  # pragma: no cover
            raise FetchError(
                "--interactive mode requires 'websockets' -> "
                "pip install websockets"
            ) from None

        return asyncio.run(self._solve_async(probe_url, asyncio, websockets))

    async def _solve_async(self, probe_url: str, asyncio: Any, websockets: Any) -> dict[str, str]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/json/new?{urllib.parse.quote(probe_url, safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            target = json.load(r)

        try:
            async with websockets.connect(
                target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024
            ) as ws:
                mid = 0

                async def cmd(method: str, params: Optional[dict] = None) -> dict:
                    nonlocal mid
                    mid += 1
                    await ws.send(json.dumps(
                        {"id": mid, "method": method, "params": params or {}}))
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == mid:
                            return msg.get("result", {})

                await cmd("Page.enable")
                await cmd("Runtime.enable")
                await cmd("Page.navigate", {"url": probe_url})

                deadline = time.time() + self._wait_captcha
                warned = False
                while time.time() < deadline:
                    await asyncio.sleep(3)
                    res = await cmd("Runtime.evaluate", {
                        "expression":
                            "JSON.stringify({p: location.pathname,"
                            " h: document.documentElement.outerHTML})",
                        "returnByValue": True,
                    })
                    payload = json.loads(res.get("result", {}).get("value", "{}") or "{}")
                    path = payload.get("p", "")
                    html = payload.get("h", "")
                    if not html:
                        continue
                    low = html.lower()
                    # The /sorry path is the reliable challenge signal; the
                    # "unusual traffic" text serves as a fallback.
                    if path.startswith("/sorry") or any(m in low for m in CAPTCHA_MARKERS):
                        if not warned:
                            LOG.warning(
                                "  Google asks for verification. Solve it in the "
                                "Chrome window that just opened (you have %ds); the "
                                "script keeps going on its own.", self._wait_captcha,
                            )
                            warned = True
                        continue
                    if len(re.findall(r"<a\s", low)) < MIN_RESULT_ANCHORS:
                        continue  # it has not rendered the SERP yet
                    LOG.info("  SERP loaded (%d shortcodes); exporting cookies…",
                             len(extract_shortcodes(html)))
                    break
                else:
                    raise CaptchaError(
                        f"Verification was not solved within {self._wait_captcha}s. "
                        "Run it again: the profile keeps what was already solved."
                    )

                res = await cmd("Network.getAllCookies")
                jar = {
                    c["name"]: c["value"] for c in res.get("cookies", [])
                    if "google" in c.get("domain", "") and c.get("value")
                }
                LOG.info("  Cookies obtained: %s", ", ".join(sorted(jar)))
                return jar
        finally:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/close/{target['id']}", timeout=5
                ).close()

    def fetch(self, url: str, poll_seconds: float = 3.0) -> str:
        """Opens the URL and returns the DOM once there are shortcodes.

        If Google serves a CAPTCHA, it warns and keeps waiting: the user solves it
        in the window and polling continues until results are found.
        """
        try:
            import asyncio  # lazy import

            import websockets  # type: ignore
        except ImportError:  # pragma: no cover
            raise FetchError(
                "--interactive mode requires 'websockets' -> "
                "pip install websockets"
            ) from None

        return asyncio.run(self._fetch_async(url, poll_seconds, websockets, asyncio))

    async def _fetch_async(self, url: str, poll_seconds: float,
                           websockets: Any, asyncio: Any) -> str:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/json/new?{urllib.parse.quote(url, safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            target = json.load(r)

        try:
            async with websockets.connect(
                target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024
            ) as ws:
                mid = 0

                async def cmd(method: str, params: Optional[dict] = None) -> dict:
                    nonlocal mid
                    mid += 1
                    await ws.send(json.dumps(
                        {"id": mid, "method": method, "params": params or {}}))
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == mid:
                            return msg.get("result", {})

                await cmd("Page.enable")
                await cmd("Runtime.enable")
                await cmd("Page.navigate", {"url": url})

                deadline = time.time() + self._wait_captcha
                warned = False
                while time.time() < deadline:
                    await asyncio.sleep(poll_seconds)
                    res = await cmd("Runtime.evaluate", {
                        "expression": "document.documentElement.outerHTML",
                        "returnByValue": True,
                    })
                    html = res.get("result", {}).get("value", "")
                    if not html:
                        continue
                    if extract_shortcodes(html):
                        LOG.info("  SERP ready: %d shortcodes",
                                 len(extract_shortcodes(html)))
                        return html
                    low = html.lower()
                    if any(m in low for m in CAPTCHA_MARKERS):
                        if not warned:
                            LOG.warning(
                                "  Google asks for a CAPTCHA. Solve it in the "
                                "Chrome window that just opened; the script keeps "
                                "going on its own (up to %ds).", self._wait_captcha,
                            )
                            warned = True
                        continue
                    if any(m in low for m in JS_SHELL_MARKERS):
                        raise JsShellError(
                            "The Chrome window did not render the SERP (JS shell)."
                        )
                raise CaptchaError(
                    f"The CAPTCHA was not solved within {self._wait_captcha}s. "
                    "Run it again; the profile already keeps what was solved."
                )
        finally:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/close/{target['id']}", timeout=5
                ).close()


# --------------------------------------------------------------------------- #
# Phase 1 - Discovery in Google
# --------------------------------------------------------------------------- #
def build_queries(keyword: str, max_queries: int) -> list[str]:
    """Dork rotation: each template queries a different set."""
    kw = keyword.strip().strip('"')
    seen: list[str] = []
    for tpl in DORK_TEMPLATES:
        dork = tpl.format(kw=kw)
        if dork not in seen:
            seen.append(dork)
        if len(seen) >= max_queries:
            break
    return seen


def extract_shortcodes(html: str) -> set[str]:
    """Extracts shortcodes from the SERP HTML.

    Google wraps links in `/url?q=...` and sometimes percent-encodes the
    destination, so the search is done over the decoded text and the original.
    """
    found = set(RE_SHORTCODE.findall(html))
    if "%3A%2F%2F" in html or "%2F" in html:
        found |= set(RE_SHORTCODE.findall(unquote(html)))
    return found


def build_serp_url(tld: str, query: str, start: int, num: int) -> str:
    """Google SERP URL. `pws=0` disables cookie-based personalization."""
    return (
        f"https://www.google.{tld}/search?q={quote_plus(query)}"
        f"&start={start}&num={num}&hl=en&pws=0&filter=0"
    )


class GoogleSerpClient:
    """Google SERP client."""

    def __init__(self, client: HttpClient, tld: str = "com",
                 render: bool = False, proxy_url: Optional[str] = None,
                 browser: Optional["CdpBrowser"] = None) -> None:
        self._c = client
        self._tld = tld
        self._host = f"https://www.google.{tld}"
        self._render = render
        self._proxy = proxy_url
        self._browser = browser

    def _build_url(self, query: str, start: int, num: int) -> str:
        return build_serp_url(self._tld, query, start, num)

    def search(self, query: str, start: int = 0, num: int = 100) -> tuple[set[str], str]:
        """Runs one query. Returns (shortcodes, url_serp)."""
        serp_url = self._build_url(query, start, num)

        if self._browser is not None:
            # Interactive mode: the CAPTCHA is solved by hand in the window.
            html = self._browser.fetch(serp_url)
        elif self._render:
            html = render_dom(serp_url, self._proxy)
        else:
            _, html = self._c.get_text(serp_url, engine="google")

        low = html.lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            raise CaptchaError(
                "Google answered with a CAPTCHA/IP reputation block "
                "('unusual traffic'). A residential or mobile proxy is required."
            )

        codes = extract_shortcodes(html)

        # A 200 that is NOT the SERP is not "no results": it is "never saw
        # the SERP". Google alternates between two non-SERP responses:
        #   a) the "please enable javascript on your web browser" shell
        #   b) the "unusual traffic" CAPTCHA
        # and a real SERP always brings dozens of result anchors, even
        # when it finds nothing. The anchor count is the structural signal
        # that does not depend on the exact text Google localizes the notice with.
        anchors = len(re.findall(r"<a\s", low))
        if not codes and (any(m in low for m in JS_SHELL_MARKERS) or anchors < MIN_RESULT_ANCHORS):
            raise JsShellError(
                f"Google did not return the SERP (0 shortcodes, only {anchors} anchors; "
                f"html={len(html)}B). It is the JS shell or a block, not zero "
                f"results. Retry with --render or with a residential proxy."
            )
        return codes, serp_url


def discover(
    serp: GoogleSerpClient,
    keyword: str,
    limit: int,
    max_queries: int,
    max_pages: int,
    num_per_page: int,
    delay: tuple[float, float],
    stats: ScrapeStats,
    limiter: Optional[RateLimiter] = None,
    retries: int = 2,
) -> dict[str, str]:
    """Phase 1: rotates queries x pages until gathering `limit` shortcodes."""
    discovered: dict[str, str] = {}
    queries = build_queries(keyword, max_queries)
    LOG.info("Phase 1: %d queries for %r", len(queries), keyword)

    for i, query in enumerate(queries, start=1):
        if len(discovered) >= limit:
            break
        for start in range(0, max_pages * num_per_page, num_per_page):
            if len(discovered) >= limit:
                break
            stats.queries_executed += 1
            LOG.info("  [%d/%d] dork: %s (start=%d)",
                     i, len(queries), query, start)
            if limiter is not None:
                limiter.acquire()
            try:
                # A 429 from Google here is the most common thing at volume: it
                # is retried with backoff before declaring the query lost.
                found, serp_url = with_retries(
                    lambda: serp.search(query, start=start, num=num_per_page),
                    attempts=retries, base_delay=5.0, max_delay=45.0,
                )
            except CaptchaError as exc:
                stats.blocked_queries += 1
                LOG.warning("      BLOCKED: %s", exc)
                break  # pushing on against a CAPTCHA only worsens the block
            except JsShellError as exc:
                stats.js_shell_queries += 1
                LOG.warning("      shell JS: %s", exc)
                break
            except RateLimitedError as exc:
                # Different from "no results": Google is throttling us.
                stats.rate_limited_queries += 1
                LOG.warning("      Google rate-limited (429) after %d attempts; "
                            "run --rate higher or wait", retries)
                break
            except FetchError as exc:
                stats.rate_limited_queries += 1
                LOG.warning("      query failed: %s", exc)
                break

            new = {c: serp_url for c in found if c not in discovered}
            discovered.update(new)
            LOG.info("      +%d new (total %d)", len(new), len(discovered))
            if not found:
                break  # this query has no more useful pages
            time.sleep(random.uniform(*delay))

    stats.discovered = len(discovered)
    LOG.info("Phase 1: %d unique shortcodes discovered", len(discovered))
    return discovered


# --------------------------------------------------------------------------- #
# Phase 2 - Parsing Instagram's public embed
# --------------------------------------------------------------------------- #
def parse_embed(html: str, shortcode: str, source_serp_url: str) -> InstagramPost:
    soup = BeautifulSoup(html, "html.parser")

    caption_node = soup.find(class_=re.compile(r"\bCaption\b"))

    author: str | None = None
    if caption_node is not None:
        for link in caption_node.find_all("a", href=True):
            m = RE_AUTHOR_LINK.match(link["href"].strip())
            if m:
                author = m.group(1)
                break

    caption: str | None = None
    if caption_node is not None:
        text = RE_COMMENTS_TAIL.sub("", caption_node.get_text(" ", strip=True)).strip()
        if author and text.lower().startswith(author.lower()):
            text = text[len(author):].strip().lstrip("·").strip()
        caption = text or None

    image_url, is_video = _extract_media(soup, author)

    if author is None:
        for img in soup.find_all("img", alt=True):
            m = RE_AUTHOR_ALT.search(img["alt"])
            if m:
                author = m.group(1)
                break

    enrich = _enrich_from_gql(html, shortcode)
    # The DOM always provides an image; the blob can raise the resolution.
    if not enrich.get("image_url"):
        enrich["image_url"] = image_url

    return InstagramPost(
        shortcode=shortcode, author=author, caption=caption,
        local_image_path=None, source_serp_url=source_serp_url,
        scraped_at=datetime.now(timezone.utc), is_video=is_video,
        **enrich,
    )


def _enrich_from_gql(html: str, shortcode: str) -> dict[str, Any]:
    """Adds the GraphQL blob metadata and the inferred date.

    Never raises: if the blob is missing (light embed) it returns only the date,
    which depends solely on the shortcode and is therefore always available.
    """
    out: dict[str, Any] = {
        "post_published_at": shortcode_to_datetime(shortcode),
        "post_published_at_inferred": True,
    }
    data = extract_context_json(html)
    if not data:
        return out
    meta = parse_gql_media(data)
    out.update({k: v for k, v in meta.items() if k != "best_url"})
    out["rich_metadata"] = True
    # The best CDN resolution usually exceeds the one from the DOM's <img>.
    if meta.get("best_url"):
        out["image_url"] = meta["best_url"]
    return out


def _extract_media(soup: BeautifulSoup, author: str | None) -> tuple[Optional[str], bool]:
    """Post photo: `img.EmbeddedMediaImage`.

    The author avatar is also an `<img>` and must NOT be confused with the post
    (README, finding 2).
    """
    node = soup.select_one("img.EmbeddedMediaImage")
    if node and node.get("src"):
        return node["src"], False

    video = soup.find("video")
    if video is not None:
        if video.get("poster"):
            return video["poster"], True
        src = video.find("source", src=True)
        if src:
            return src["src"], True

    cands: list[str] = []
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src.startswith("http") or RE_AVATAR_CDN.search(src):
            continue
        if author and (img.get("alt") or "").strip() == author:
            continue
        if not re.search(r"\.(?:jpg|jpeg|png)(\?|$)", src, re.I):
            continue
        cands.append(src)
    if cands:
        return max(cands, key=len), False
    return None, False


def is_unavailable(post: InstagramPost, html: str) -> bool:
    """Soft 404: Instagram returns 200 with a broken-link notice."""
    if any(m in html for m in SOFT_404_MARKERS):
        return True
    return post.image_url is None and post.caption is None


# --------------------------------------------------------------------------- #
# Phase 3 - Download and persistence
# --------------------------------------------------------------------------- #
def detect_image_format(data: bytes) -> tuple[str, str]:
    """Returns (extension, mime) by reading the bytes, not the URL.

    Instagram's CDN serves WebP even when the URL ends in `.jpg`: measured in 2 of
    24 posts in a real run. Saving WebP with a .jpg extension breaks
    any consumer that trusts the extension (PIL, torchvision, ffmpeg).
    """
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:4] == b"GIF8":
        return "gif", "image/gif"
    return "bin", "application/octet-stream"


def download_image(client: HttpClient, out_dir: Path, post: InstagramPost,
                   overwrite: bool) -> bool:
    """Downloads the image. Returns True if a new file was written.

    Transient failures are PROPAGATED as `FetchError` on purpose: the caller wraps
    this in `with_retries`, and it can only retry if the exception actually reaches
    it. Catching them here and returning `False` made `with_retries` see a "success"
    and never retry, which lost the image permanently on a one-off CDN 429 (see
    ARCHITECTURE.md section 5).

    Returns `False` without raising in the two cases that are not failures:
      - the post has no image URL;
      - the file already exists and `--overwrite` was not passed (idempotency).
    """
    if not post.image_url:
        return False

    # `client.get_bytes` already translates 429/5xx into this exception hierarchy.
    data = client.get_bytes(post.image_url)

    if len(data) < 1024:
        raise FetchError(
            f"suspiciously small image response for {post.shortcode} "
            f"({len(data)} bytes)")

    ext, _mime = detect_image_format(data)
    if ext == "bin":
        raise FetchError(
            f"unknown image format for {post.shortcode}; not saving")

    # The extension is decided only now, so the "already exists" check
    # has to look for any previous file of the shortcode.
    existing = [p for p in out_dir.glob(f"{post.shortcode}.*") if p.stat().st_size > 0]
    if existing and not overwrite:
        post.local_image_path = str(existing[0])
        return False

    dest = out_dir / f"{post.shortcode}.{ext}"
    dest.write_bytes(data)
    post.local_image_path = str(dest)
    LOG.info("[%s] image saved (%d bytes, %s)", post.shortcode, len(data), ext)
    return True


def write_metadata(posts: list[InstagramPost], stats: ScrapeStats, query: str,
                   limit: int, out_dir: Path, render: bool,
                   proxy_used: bool) -> Path:
    path = out_dir / "metadata.json"
    payload = MetadataFile(
        query=query, requested_limit=limit, scraped_at=datetime.now(timezone.utc),
        stats=stats, posts=posts, rendered=render, proxy_used=proxy_used,
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    return path


class RateLimiter:
    """Spaces out requests globally, even with N workers.

    The slot is reserved under a lock but the wait happens OUTSIDE the lock: if
    not, the workers would serialize one by one and concurrency would be useless.
    Instagram does not publish its limit; this is the brake that can be lowered
    with `--rate` without touching code, and `--rate 0` disables it.
    """

    def __init__(self, min_interval: float) -> None:
        self._min = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._min <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._min
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def with_retries(fn: Any, attempts: int, *, base_delay: float = 2.0,
                 max_delay: float = 60.0, on_error: Any = None) -> Any:
    """Runs `fn` retrying on transient failures.

    Decision scale, all measured:
      - `NotFoundError`  -> no retry: the post does not exist.
      - `RateLimitedError` -> wait `Retry-After` if the server sends it.
      - other `FetchError` -> exponential backoff.

    This is what prevents a long run from silently losing hundreds of posts:
    a sporadic 429 recovers instead of being counted as a failure.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except NotFoundError:
            raise  # terminal by definition
        except (RateLimitedError, FetchError) as exc:
            last = exc
            if on_error is not None:
                on_error(attempt, exc)
            if attempt >= attempts:
                break
            if isinstance(exc, RateLimitedError) and exc.retry_after:
                wait = min(max(exc.retry_after, base_delay), max_delay)
                LOG.warning("      429; waiting %.0fs (Retry-After)", wait)
            else:
                wait = min(base_delay * (2 ** (attempt - 1)), max_delay)
                # jitter: prevents N workers from retrying in lockstep
                wait *= random.uniform(0.8, 1.2)
                LOG.warning("      %s; retry %d/%d in %.1fs",
                            type(exc).__name__, attempt + 1, attempts, wait)
            time.sleep(wait)
    assert last is not None
    raise last


def write_metadata_from_store(store: "PostStore", stats: ScrapeStats, query: str,
                              args: argparse.Namespace, out_dir: Path) -> Path:
    """Writes metadata.json from ALL the finished posts in the database.

    Not just this run: that way the file is the complete picture of the
    accumulated knowledge, even if the run was cut in half.
    """
    path = out_dir / "metadata.json"
    rows = store.done_posts()
    posts = [
        InstagramPost(
            shortcode=r["shortcode"], author=r["author"], caption=r["caption"],
            image_url=r["image_url"], local_image_path=r["local_image_path"],
            source_serp_url=r["source_serp_url"] or "",
            scraped_at=_parse_dt(r["scraped_at"]) or _fallback_now(),
            is_video=bool(r["is_video"]),
            rich_metadata=bool(r["rich_metadata"]),
            media_id=r["media_id"], likes=r["likes"], comments=r["comments"],
            author_id=r["author_id"], author_followers=r["author_followers"],
            author_verified=None if r["author_verified"] is None
            else bool(r["author_verified"]),
            width=r["width"], height=r["height"], alt_text=r["alt_text"],
            carousel_count=r["carousel_count"] or 0,
            carousel_shortcodes=json.loads(r["carousel_shortcodes"] or "[]"),
            post_published_at=_parse_dt(r["post_published_at"])
            if r["post_published_at"] else None,
            post_published_at_inferred=bool(r["post_published_at_inferred"]),
        )
        for r in rows
    ]
    payload = MetadataFile(
        query=query, requested_limit=args.limit,
        scraped_at=datetime.now(timezone.utc), stats=stats, posts=posts,
        rendered=args.render, proxy_used=bool(args.proxy_url),
    )
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    return path


def _fallback_now() -> datetime:
    """Current UTC time. Used only when a required timestamp is absent or corrupt.

    `_parse_dt` returns None for bad input; `scraped_at` is a required field of
    `InstagramPost`, so a row with no usable timestamp falls back to "now" rather
    than failing the whole rebuild of metadata.json.
    """
    return datetime.now(timezone.utc)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parses an ISO timestamp, returning None when it is missing or corrupt.

    It used to fall back to `datetime.now()` on any failure, which turned a
    corrupted `scraped_at` into "right now" and silently hid the corruption from
    whoever consumed `metadata.json`. A missing timestamp is now honestly None;
    the extraction moment is still always available as the post's own `scraped_at`
    field, so no information is actually lost.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        LOG.warning("unparseable timestamp %r; leaving it empty", value)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Rich metadata: the GraphQL blob the embed carries and nobody parses
# --------------------------------------------------------------------------- #
# The rendered embed includes a `contextJSON` with `gql_data.shortcode_media`,
# which is a real GraphQL response: likes, comments, followers, verified,
# dimensions, alt-text and the carousel slides. All of that was being
# discarded because the parser only looked at the DOM.
#
# Measured coverage: 10/12 posts. When it is missing, Instagram serves
# `contextJSON: null` with `isRichEmbed: false`. It is NOT transient (tested with
# 4 retries) and no URL variant recovers it, so it is saved as null and not retried.
RE_CONTEXT_JSON: Final[re.Pattern[str]] = re.compile(
    r'"contextJSON"\s*:\s*'
)
SHORTCODE_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# Epoch of Instagram's ID scheme (the high bits of the media ID are a
# timestamp). Validated: the id derived from the shortcode matches exactly the
# `id` of the embed, and the decoded dates match captions that declare their
# own month/year ("dropping in August 2026" -> 2026-08-01).
IG_ID_EPOCH_MS: Final[int] = 1314220021721


def shortcode_to_media_id(shortcode: str) -> int:
    """Converts an Instagram base64url shortcode into its numeric media ID."""
    n = 0
    for ch in shortcode:
        idx = SHORTCODE_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid shortcode: {shortcode!r}")
        n = n * 64 + idx
    return n


def shortcode_to_datetime(shortcode: str) -> Optional[datetime]:
    """INFERRED publication date, decoded from the media ID.

    It is not an official Instagram field: it is the creation timestamp that its
    ID scheme already encodes. It is exact, but it is flagged as inferred.
    """
    try:
        media_id = shortcode_to_media_id(shortcode)
    except ValueError:
        return None
    ms = (media_id >> 23) + IG_ID_EPOCH_MS
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def extract_context_json(html: str) -> Optional[dict[str, Any]]:
    """Extracts the `contextJSON` from the embed tolerating its escapes.

    It cannot be done with a naive regex: the value is nested JSON,
    escaped inside the HTML, and a simple pattern fails on real cases
    (measured: 1 of 2). The string is scanned respecting the escapes.
    """
    key = '"contextJSON":"'
    search_from = 0
    while True:
        i = html.find(key, search_from)
        if i < 0:
            return None
        i += len(key)
        out: list[str] = []
        j = i
        while j < len(html):
            ch = html[j]
            if ch == "\\" and j + 1 < len(html):
                out.append(html[j:j + 2])
                j += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            j += 1
        search_from = j
        try:
            data = json.loads(json.loads('"' + "".join(out) + '"'))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("gql_data"), dict):
            return data
    return None


def _coerce_int(value: Any) -> Optional[int]:
    """Accepts an int OR a numeric string.

    Instagram does not serve every number as a JSON number: the blob's `id` and
    `owner.id` currently arrive as quoted strings (`"3978685437899244498"`), while
    `edge_liked_by.count` is still a real int. A strict `isinstance(x, int)` check
    silently dropped `media_id` and `author_id` on every live post, which is why the
    local database showed NULL for them despite `rich_metadata=1`.

    `bool` is rejected explicitly: it is a subclass of `int` but is never a count.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_int(node: Any, *path: str) -> Optional[int]:
    cur: Any = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return _coerce_int(cur)


def parse_gql_media(data: dict[str, Any]) -> dict[str, Any]:
    """Normalizes `gql_data.shortcode_media` into flat, usable fields."""
    sm = data.get("gql_data", {}).get("shortcode_media")
    if not isinstance(sm, dict):
        return {}
    owner = sm.get("owner") if isinstance(sm.get("owner"), dict) else {}
    children = (sm.get("edge_sidecar_to_children") or {}).get("edges") or []
    dims = sm.get("dimensions") if isinstance(sm.get("dimensions"), dict) else {}
    resources = sm.get("display_resources") or []

    best_url, best_w = None, 0
    for r in resources:
        if isinstance(r, dict) and isinstance(r.get("config_width"), int):
            if r["config_width"] > best_w and r.get("src"):
                best_url, best_w = r["src"], r["config_width"]

    return {
        "media_id": _coerce_int(sm.get("id")),
        "likes": _first_int(sm, "edge_liked_by", "count"),
        "comments": _first_int(sm, "edge_media_to_comment", "count"),
        "author_id": _coerce_int(owner.get("id")),
        "author_followers": _first_int(owner, "edge_followed_by", "count"),
        "author_verified": owner.get("is_verified")
        if isinstance(owner.get("is_verified"), bool) else None,
        "width": _coerce_int(dims.get("width")),
        "height": _coerce_int(dims.get("height")),
        "alt_text": sm.get("accessibility_caption")
        if isinstance(sm.get("accessibility_caption"), str) else None,
        "carousel_count": len(children),
        "carousel_shortcodes": [
            n.get("shortcode") for n in
            ((e.get("node") or {}) for e in children if isinstance(e, dict))
            if isinstance(n.get("shortcode"), str)
        ],
        "display_url": sm.get("display_url"),
        "best_url": best_url,
    }


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class PostProcessor:
    """Procesa un shortcode: embed -> parseo -> imagen -> checkpoint.

    Un objeto por corrida. Es seguro usarlo desde varios hilos porque:
      - each worker has its own HTTP session (sessions are not thread-safe),
      - el store serializa sus escrituras con un lock,
      - el rate limiter es global.
    """

    def __init__(self, store: "PostStore", out_dir: Path, args: argparse.Namespace,
                 cookies: Optional[dict[str, str]], limiter: RateLimiter) -> None:
        self.store = store
        self.out_dir = out_dir
        self.args = args
        self.cookies = cookies
        self.limiter = limiter
        self._tls = threading.local()
        self._clients: list[HttpClient] = []
        self._clients_lock = threading.Lock()
        self._stats_lock = threading.Lock()

    def client(self) -> HttpClient:
        c = getattr(self._tls, "client", None)
        if c is None:
            c = build_client(self.args.proxy_url, self.args.timeout, self.cookies)
            self._tls.client = c
            with self._clients_lock:
                self._clients.append(c)
        return c

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for c in clients:
            with contextlib.suppress(Exception):
                c.close()

    def process(self, code: str, src: str, keyword: str, stats: ScrapeStats) -> Optional[InstagramPost]:
        a = self.args
        client = self.client()
        store = self.store

        def on_err(attempt: int, exc: BaseException) -> None:
            store.log_error(code, attempt, type(exc).__name__, str(exc))

        self.limiter.acquire()
        try:
            _, html = with_retries(
                lambda: client.get_text(EMBED_URL.format(shortcode=code), engine="embed"),
                attempts=a.retries, base_delay=a.retry_delay,
                max_delay=a.retry_max_delay, on_error=on_err,
            )
        except NotFoundError:
            store.mark_terminal(code, "unavailable", "404")
            with self._stats_lock:
                stats.skipped_unavailable += 1
            LOG.info("[%s] 404: no existe", code)
            return None
        except FetchError as exc:
            store.mark_terminal(code, "failed", f"{type(exc).__name__}: {exc}",
                                attempts=a.retries)
            store.log_error(code, a.retries, type(exc).__name__, str(exc))
            with self._stats_lock:
                stats.failed += 1
            LOG.warning("[%s] fallo definitivo tras %d intentos: %s",
                        code, a.retries, exc)
            return None

        post = parse_embed(html, code, src)
        if is_unavailable(post, html):
            store.mark_terminal(code, "unavailable", "soft-404")
            with self._stats_lock:
                stats.skipped_unavailable += 1
            LOG.info("[%s] no disponible (privado/eliminado)", code)
            return None

        saved = False
        self.limiter.acquire()
        try:
            saved = with_retries(
                lambda: download_image(client, self.out_dir, post, a.overwrite),
                attempts=a.retries, base_delay=a.retry_delay,
                max_delay=a.retry_max_delay, on_error=on_err,
            )
        except FetchError as exc:
            store.log_error(code, a.retries, f"image:{type(exc).__name__}", str(exc))
            with self._stats_lock:
                stats.images_failed += 1
            LOG.warning("[%s] image download gave up after %d attempts: %s",
                        code, a.retries, exc)

        store.mark_ok(
            code, query=keyword, author=post.author, caption=post.caption,
            image_url=post.image_url, local_image_path=post.local_image_path,
            is_video=post.is_video, source_serp_url=src,
            scraped_at=post.scraped_at.isoformat(), attempts=a.retries,
            extra=post.model_dump(),
        )
        if saved:
            with self._stats_lock:
                stats.images_saved += 1
        return post


def resolve_keywords(args: argparse.Namespace) -> list[str]:
    """Builds the keyword queue from --query (repeatable) and --queries-file.

    The post ceiling comes from breadth, not depth: one keyword gives ~60-120
    shortcodes and rotating dorks over it yields little (78% overlap measured).
    That is why the queue is the mechanism to reach thousands.
    """
    kws: list[str] = list(args.query or [])
    if args.queries_file:
        for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                kws.append(line)
    seen: list[str] = []
    for k in kws:
        k = k.strip().strip('"')
        if k and k not in seen:
            seen.append(k)
    if not seen:
        raise SystemExit("Hacen falta keywords: --query o --queries-file")
    return seen


def run(args: argparse.Namespace) -> int:
    keywords = resolve_keywords(args)
    keyword = keywords[0]
    slug = (re.sub(r"[^a-z0-9]+", "_", keyword.lower()).strip("_")
            if len(keywords) == 1 else f"{len(keywords)}k")
    out_dir = Path(args.output_dir or f"output/google_{slug}")
    out_dir.mkdir(parents=True, exist_ok=True)

    stats = ScrapeStats()
    posts: list[InstagramPost] = []
    delay = (args.delay_min, args.delay_max)
    browser: Optional[CdpBrowser] = None
    cookies: Optional[dict[str, str]] = None

    # ---- Bootstrap: resolver el CAPTCHA con un Chrome real ---------------
    # Google does not deliver the SERP to plain HTTP clients without a prior
    # session. A real Chrome is opened, the user solves the verification once,
    # and its cookies are exported. From there scraping runs over HTTP, which is
    # much faster and does not ask for verification again.
    if args.interactive and not args.shortcodes:
        browser = CdpBrowser(profile_dir=args.profile, port=args.cdp_port,
                             proxy_url=args.proxy_url, wait_captcha=args.wait_captcha)
        browser.start()
        try:
            probe = build_serp_url(args.tld, DORK_TEMPLATES[0].format(kw=keyword),
                                   0, args.num_per_page)
            cookies = browser.solve_and_export(probe)
        finally:
            browser.stop()
            browser = None
        if not cookies:
            raise IpBlockedError("No se pudieron exportar cookies de Google.")

    client = build_client(args.proxy_url, args.timeout, cookies)
    db_path = Path(args.db or (out_dir / "posts.sqlite3"))
    store = make_store(str(args.db) if args.db else str(db_path),
                       backend=getattr(args, "db_backend", None))
    store.start_run(keyword, {"limit": args.limit, "interactive": args.interactive,
                              "proxy": bool(args.proxy_url)})
    LOG.info("Incremental store: %s", type(store).__name__)

    try:
        # ---- Fase 1 -------------------------------------------------------
        if args.shortcodes:
            LOG.info("Fase 1: salteada (--shortcodes, %d provistos)", len(args.shortcodes))
            discovered = {sc.strip(): "(shortcodes provistos manualmente)"
                          for sc in args.shortcodes if sc.strip()}
        else:
            serp = GoogleSerpClient(client, tld=args.tld, render=args.render,
                                    proxy_url=args.proxy_url)
            discovered: dict[str, str] = {}
            # Its own brake for Google: it is the host that throttles most, and
            # de la fase 2/3 (Instagram) no lo protege.
            discovery_limiter = RateLimiter(
                args.discovery_rate if args.discovery_rate > 0 else args.rate)
            LOG.info("Fase 1: %d keyword(s) en cola, %.1fs entre consultas",
                     len(keywords), discovery_limiter._min)
            for n_kw, kw in enumerate(keywords, start=1):
                if args.limit > 0 and len(discovered) >= args.limit:
                    LOG.info("  discovery complete: limit reached")
                    break
                restante = (args.limit - len(discovered)) if args.limit > 0 else 0
                LOG.info("  [%d/%d] keyword: %r", n_kw, len(keywords), kw)
                try:
                    d = discover(serp, kw, restante or 10_000, args.max_queries,
                                 args.max_pages, args.num_per_page, delay, stats,
                                 limiter=discovery_limiter,
                                 retries=max(1, args.discovery_retries))
                except FetchError as exc:
                    LOG.warning("  discovery for %r failed: %s", kw, exc)
                    continue
                nuevos = {c: u for c, u in d.items() if c not in discovered}
                discovered.update(nuevos)
                LOG.info("      +%d nuevos (acumulado %d)", len(nuevos), len(discovered))

            if not discovered:
                bloqueado = bool(stats.blocked_queries or stats.js_shell_queries)
                en_base = len(store.pending())
                # Con --resume, un discovery bloqueado NO debe abortar la
                # corrida: los posts ya descubiertos siguen en la base y son
                # justamente los que hay que terminar.
                if bloqueado and not (args.resume and en_base):
                    raise IpBlockedError(
                        "Ninguna consulta a Google pudo completarse "
                        f"({stats.blocked_queries} bloqueadas por CAPTCHA, "
                        f"{stats.js_shell_queries} con shell JS). "
                        "A residential or mobile proxy is required (--proxy-url)."
                    )
                if bloqueado:
                    LOG.warning(
                        "Discovery bloqueado por Google, pero la base tiene %d posts "
                        "pending: continuing with --resume over what was already discovered.",
                        en_base)
                elif stats.rate_limited_queries:
                    LOG.warning(
                        "Google rate limited %d queries (429): that is not the same as "
                        "no results. Raise --rate or wait before retrying.",
                        stats.rate_limited_queries)
                else:
                    LOG.warning("Google returned no results for any keyword.")

        # ---- Fase 2 + 3 ---------------------------------------------------
        # El discovery se persiste primero: si la corrida se corta, locostoso
        # (las consultas a Google) no se pierde y --resume lo reutiliza.
        nuevos = store.record_discovered(discovered.items(), keyword)
        LOG.info("Discovery persistido: %d nuevos (total %d)",
                 nuevos, len(discovered))

        # Atomic claim: on Postgres this runs FOR UPDATE SKIP LOCKED inside a
        # single statement, so a second scraper skips whatever this one already
        # reserved. A plain pending() read would hand both processes the same
        # posts (measured: 80% duplicated work with five processes).
        claim_n = args.limit if args.limit > 0 else 10_000
        pendientes = store.claim_work(limit=claim_n)

        total = len(pendientes)
        LOG.info("Phase 2/3: %d posts claimed (limit %d)%s", total, args.limit,
                 " [resume]" if args.resume else "")

        limiter = RateLimiter(args.rate)
        proc = PostProcessor(store, out_dir, args, cookies, limiter)
        LOG.info("Fase 2/3: %d posts, %d worker(s), %.2fs entre requests",
                 total, args.workers, args.rate)

        try:
            if args.workers > 1:
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    futs = {
                        ex.submit(proc.process, r["shortcode"],
                                  r["source_serp_url"] or "(sin url de serp)",
                                  keyword, stats): r["shortcode"]
                        for r in pendientes
                    }
                    for n, fut in enumerate(as_completed(futs), start=1):
                        # Nothing is swallowed silently: an exception here would be
                        # invisible data loss (it happened once already).
                        try:
                            p = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            stats.failed += 1
                            LOG.error("[%s] error inesperado: %s: %s",
                                      futs[fut], type(exc).__name__, exc)
                            continue
                        if p is not None:
                            posts.append(p)
                        stats.processed = n
                        if n % 10 == 0 or n == total:
                            LOG.info("  [%d/%d] completados", n, total)
            else:
                for n, r in enumerate(pendientes, start=1):
                    p = proc.process(r["shortcode"],
                                     r["source_serp_url"] or "(sin url de serp)",
                                     keyword, stats)
                    if p is not None:
                        posts.append(p)
                    stats.processed = n
                    if n % 10 == 0 or n == total:
                        LOG.info("  [%d/%d] completados", n, total)
        finally:
            proc.close()

        store_counts_cache = store.counts()
        path = write_metadata_from_store(store, stats, keyword, args, out_dir)
    finally:
        store.close()
        client.close()

    LOG.info("Done: %d posts in this run, %d images -> %s",
             len(posts), stats.images_saved, path)
    LOG.info("Acumulado en base: %s", store_counts_cache)
    if stats.failed:
        LOG.warning("%d posts quedaron como 'failed'; se reprocesan con "
                    "--retry-failed.", stats.failed)
    LOG.info("Metadata: %s", path)
    return 0 if posts or store_counts_cache.get("ok") else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scraper de posts de Instagram indexados en Google (sin login).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--query", action="append", default=None,
                   help='Keyword, ej. "running shoes". Repetible para una cola.')
    p.add_argument("--queries-file", default=None,
                   help="File with one keyword per line (uses # for comments)")
    p.add_argument("--limit", type=int, default=10,
                   help="Maximum posts to return (default 10)")
    p.add_argument("--workers", type=int, default=1,
                   help="Workers concurrentes en la fase 2/3 (default 1)")
    p.add_argument("--rate", type=float, default=0.0,
                   help="Minimum seconds between requests to Instagram. 0 = no brake")
    p.add_argument("--discovery-rate", type=float, default=0.0,
                   help="Segundos entre consultas a Google. Si es 0 usa --rate")
    p.add_argument("--discovery-retries", type=int, default=2,
                   help="Reintentos por consulta de Google ante 429 (default 2)")
    p.add_argument("--output-dir", default=None,
                   help="Carpeta de salida (default ./output/google_{slug})")
    p.add_argument("--proxy-url", default=None,
                   help="http://, https:// o socks5://. Recomendado para Google.")
    p.add_argument("--tld", default="com", help="Dominio de Google: com, es, com.ar…")
    p.add_argument("--render", action="store_true",
                   help="Renderizar con Chromium headless en vez de HTTP plano")
    p.add_argument("--interactive", action="store_true",
                   help="Real Chrome with a window: you solve the CAPTCHA by hand and it continues")
    p.add_argument("--profile", default=None,
                   help="Perfil persistente de Chrome (default ~/.ig_google_profile)")
    p.add_argument("--cdp-port", type=int, default=9222)
    p.add_argument("--wait-captcha", type=int, default=180,
                   help="Segundos de espera para resolver el CAPTCHA (default 180)")
    p.add_argument("--max-queries", type=int, default=6,
                   help="Maximum dorks to rotate (default 6)")
    p.add_argument("--max-pages", type=int, default=2,
                   help="Pages per dork via start= (default 2)")
    p.add_argument("--num-per-page", type=int, default=100,
                   help="Results per page (default 100)")
    p.add_argument("--delay-min", type=float, default=3.0)
    p.add_argument("--delay-max", type=float, default=7.0)
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--retries", type=int, default=3,
                   help="Intentos por post ante 429/5xx (default 3)")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Base del backoff exponencial, en segundos (default 2)")
    p.add_argument("--retry-max-delay", type=float, default=60.0,
                   help="Techo de espera entre reintentos (default 60)")
    p.add_argument("--retry-failed", action="store_true",
                   help="Reprocesar los posts que quedaron como 'failed'")
    p.add_argument("--db", default=None,
                   help="SQLite path or postgres:// URI "
                        "(default <output-dir>/posts.sqlite3)")
    p.add_argument("--db-backend", choices=("sqlite", "postgres"),
                   default=None,
                   help="Force a backend. Defaults to postgres when --db is a "
                        "postgres:// URI or IG_POSTGRES_DSN is set")
    p.add_argument("--resume", action="store_true",
                   help="Omitir los posts ya terminados y seguir con los pendientes")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-download images already present")
    p.add_argument("--shortcodes", default=None,
                   help="Lista de shortcodes separados por coma; saltea Google")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.shortcodes:
        args.shortcodes = [s for s in args.shortcodes.split(",") if s.strip()]
    try:
        return run(args)
    except IpBlockedError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.warning("Interrumpido.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())