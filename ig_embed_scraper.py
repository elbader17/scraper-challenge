#!/usr/bin/env python3
"""
Proof of Concept: extraction of public Instagram posts by hashtag
using only the public *embed* route (no login, no user cookies).

Techniques empirically verified against the real site:

  Phase 1 - Discovery
      https://www.instagram.com/explore/tags/{hashtag}/
      The server-rendered HTML contains two sources of shortcodes:
        a) anchors  <a href="/reel/<shortcode>/?utm_source=popular_topic_grid">
        b) blobs   "code":"<shortcode>"
      No effective pagination: ?page=2 returns the same grid (~12-14 posts).

  Phase 2 - Embed
      https://www.instagram.com/p/{shortcode}/embed/captioned/
      Returns 200 without a session. Important observations about the current DOM:
        - There is NO <meta property="og:*">, NO JSON-LD, NO <time datetime>.
          Parsing must be 100% based on CSS classes of the DOM.
        - The main photo is `img.EmbeddedMediaImage`
          (alt="Instagram post shared by @usuario").
        - The author's avatar is also an <img>, with no class, with
          alt="{username}" and its CDN contains the segment "t51.2885-19".
          -> mistaking it for the main photo yields 40x40 images instead of the post.
        - The author is obtained from the <a href="https://www.instagram.com/<user>/?utm_source=ig_embed">
          that hangs from the `.Caption` block.
        - In reels, the "frame" is a static image served by the CDN.

Usage:
    python ig_embed_scraper.py --hashtag sneakers --limit 5
    python ig_embed_scraper.py --hashtag sneakers --limit 20 --proxy socks5://127.0.0.1:9050
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Optional, Protocol

# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Missing beautifulsoup4 ->  pip install -r requirements.txt")


LOG: logging.Logger = logging.getLogger("ig_scraper")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

USER_AGENT_FALLBACK: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TAG_PAGE: Final[str] = "https://www.instagram.com/explore/tags/{tag}/"
EMBED_PAGE: Final[str] = "https://www.instagram.com/p/{shortcode}/embed/captioned/"

# Order: real anchors first, then fallback JSON blobs.
RE_ANCHOR: Final[re.Pattern[str]] = re.compile(
    r'href="/(?:p|reel|tv)/([A-Za-z0-9_\-]{5,})/'
)
RE_JSON_CODE: Final[re.Pattern[str]] = re.compile(r'"code":"([A-Za-z0-9_\-]{5,})"')

# Author: link hanging from the Caption block.
RE_AUTHOR_LINK: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/(?:\?|$)"
)
# Fallback: alt="Instagram post shared by @usuario"
RE_AUTHOR_ALT: Final[re.Pattern[str]] = re.compile(
    r"Instagram post shared by @([A-Za-z0-9._]+)"
)
# Avatar: never the post's photo.
RE_AVATAR_CDN: Final[re.Pattern[str]] = re.compile(r"/t51\.2885-19/")
# Config tokens that the JSON regex can mistake for a shortcode.
# Real example: {"IntlCurrentLocale",[],"code":"en_US"} -> "en_US".
RE_LOCALE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2,3}[_-][A-Z]{2}$")
# Instagram shortcodes are 10-11 characters (12 as margin).
SHORTCODE_LEN: Final[tuple[int, int]] = (10, 12)
# Instagram replies 200 (not 404) to deleted, private or invalid shortcodes,
# showing this notice page instead.
SOFT_404_MARKERS: Final[tuple[str, ...]] = (
    "may be broken, or the post may have been removed",
    "This page isn't available",
    "Sorry, this page isn't available",
)
# Noise that Instagram appends at the end of the Caption block text.
RE_COMMENTS_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s*View all ([\d.,]+[KkMm]? )?comments?\.?\s*$", re.IGNORECASE
)

# Official alphabet used by Instagram shortcodes (URL-safe base64).
SHORTCODE_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# Epoch of the media ID "snowflake" — the millisecond timestamp the ID scheme is
# offset from. It must match the value used by `ig_google_scraper.py`.
#
# Validated, not guessed: decoding the shortcode `DN292ZY3K4y` reproduces the
# official `id` from the embed's GraphQL blob (3978685437899244498), and captions
# that declare their own month/year match ("dropping in August 2026" -> 2026-08).
# The previously used value (1284870074250) was ~340 days lower and produced
# plausible-looking but wrong dates.
MEDIA_ID_EPOCH_MS: Final[int] = 1314220021721

SLEEP_MIN: Final[float] = 1.5
SLEEP_MAX: Final[float] = 3.5


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Post:
    """A post extracted from the public embed."""

    shortcode: str
    author: Optional[str]
    caption: Optional[str]
    image_url: Optional[str]
    local_image_path: Optional[str] = None
    # ISO-8601 UTC of the extraction moment. Always present.
    scraped_at: str = ""
    # The public embed no longer exposes the publication date (no <time>,
    # no JSON-LD, no og:). It is only filled with --decode-post-time, and in
    # that case it is marked as inferred, not verified against the server.
    post_published_at: Optional[str] = None
    post_published_at_inferred: bool = False
    is_video: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScrapeStats:
    """Pipeline counters, useful for inspection and logs."""

    discovered: int = 0
    processed: int = 0
    images_saved: int = 0
    skipped_private: int = 0
    failed: int = 0
    rate_limited: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class ScraperConfig:
    hashtag: str
    limit: int
    output_dir: Path
    proxy_url: Optional[str] = None
    retries: int = 3
    timeout: float = 25.0
    decode_post_time: bool = False
    overwrite: bool = False
    stats: ScrapeStats = field(default_factory=ScrapeStats)


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """Recoverable failure while downloading a URL."""


class NotFoundError(FetchError):
    """HTTP 404 - deleted post, private, or nonexistent hashtag."""


class RateLimitedError(FetchError):
    """HTTP 429 - request limit reached."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Client(Protocol):
    """Minimal transport interface; allows swapping curl_cffi / httpx."""

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]: ...
    def close(self) -> None: ...


def _normalize_proxy(proxy: Optional[str]) -> Optional[dict[str, str]]:
    """curl_cffi expects a dict of schemes; httpx accepts a single URL."""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


class CurlCffiClient:
    """Default client: mimics Chrome's TLS/fingerprint handshake.

    This is the one that avoids most of Instagram's 401/403 blocks, since
    Instagram's bot validates the client's JA3 fingerprint besides the headers.
    """

    def __init__(self, cfg: ScraperConfig) -> None:
        from curl_cffi import requests as cc_requests  # lazy import

        self._r = cc_requests
        self._cfg = cfg
        self._s = cc_requests.Session(
            impersonate="chrome",
            proxies=_normalize_proxy(cfg.proxy_url),
            timeout=cfg.timeout,
        )
        self._s.headers.update(
            {
                "User-Agent": USER_AGENT_FALLBACK,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                # Identifying as an embed is consistent with the route we ask
                # for and reduces the chance of being served a login wall.
                "Referer": "https://www.instagram.com/",
                "Upgrade-Insecure-Requests": "1",
            }
        )

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        r = self._s.get(url, allow_redirects=True)
        headers = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code == 404:
            raise NotFoundError(f"404 at {url}")
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            raise RateLimitedError(f"429 at {url}", float(ra) if ra else None)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} at {url}")
        return r.status_code, r.content, headers

    def close(self) -> None:
        self._s.close()


class HttpxClient:
    """Fallback if curl_cffi is not available or fails to compile/install.

    Less resistant to fingerprinting, but good enough for the embed route
    when the IP is not blocked.
    """

    def __init__(self, cfg: ScraperConfig) -> None:
        import httpx  # lazy import

        self._h = httpx
        self._cfg = cfg
        self._s = httpx.Client(
            http2=False,
            timeout=cfg.timeout,
            follow_redirects=True,
            proxy=cfg.proxy_url,
            headers={
                "User-Agent": USER_AGENT_FALLBACK,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.instagram.com/",
            },
        )

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        r = self._s.get(url)
        headers = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code == 404:
            raise NotFoundError(f"404 at {url}")
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            raise RateLimitedError(f"429 at {url}", float(ra) if ra else None)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} at {url}")
        return r.status_code, r.content, headers

    def close(self) -> None:
        self._s.close()


def build_client(cfg: ScraperConfig) -> Client:
    if cfg.proxy_url:
        LOG.info("Proxy active: %s", _redact_proxy(cfg.proxy_url))
    try:
        return CurlCffiClient(cfg)
    except Exception as exc:  # noqa: BLE001 - we want to degrade, not abort
        LOG.warning("curl_cffi not available (%s); using httpx as fallback", exc)
        return HttpxClient(cfg)


def _redact_proxy(url: str) -> str:
    """Hides credentials embedded in the proxy URL before logging."""
    return re.sub(r"//[^@/]*@", "//***:***@", url)


# --------------------------------------------------------------------------- #
# Network utilities
# --------------------------------------------------------------------------- #
def human_delay(extra: float = 0.0) -> None:
    """Random pause to emulate human traffic (1.5 - 3.5 s)."""
    delay = random.uniform(SLEEP_MIN, SLEEP_MAX) + extra
    LOG.debug("sleep %.2fs", delay)
    time.sleep(delay)


def fetch_with_retry(
    client: Client,
    url: str,
    *,
    retries: int,
    label: str = "",
) -> bytes:
    """Download a URL with exponential retries on 429/5xx.

    404 is not retried: it means the resource does not exist, not that it is
    temporarily down.
    """
    backoff = 5.0
    last: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            _, body, _ = client.get(url)
            return body
        except NotFoundError:
            raise
        except RateLimitedError as exc:
            last = exc
            wait = exc.retry_after if exc.retry_after else backoff
            LOG.warning(
                "429 rate limit at %s%s -> waiting %.0fs (attempt %d/%d)",
                label, url, wait, attempt, retries,
            )
            time.sleep(min(wait, 120.0))
            backoff *= 2
        except (FetchError, Exception) as exc:  # noqa: BLE001
            last = exc
            LOG.warning(
                "Failure at %s%s (%s: %s) -> retry %d/%d",
                label, url, type(exc).__name__, exc, attempt, retries,
            )
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2

    raise FetchError(f"Could not download {url}: {last}") from last


# --------------------------------------------------------------------------- #
# Phase 1 - Shortcode discovery
# --------------------------------------------------------------------------- #
def is_plausible_shortcode(token: str) -> bool:
    """Filters false positives out of the discovery regex.

    Needed because the HTML includes client config blobs (e.g.
    {"IntlCurrentLocale",[],"code":"en_US"}) whose values satisfy a generic
    identifier regex but are not posts.
    """
    if not SHORTCODE_LEN[0] <= len(token) <= SHORTCODE_LEN[1]:
        return False
    if RE_LOCALE.match(token):          # en_US, es_ES, pt_BR...
        return False
    return all(c in SHORTCODE_ALPHABET for c in token)


def _page_title(html: str) -> str:
    """Extracts the page <title>; useful to diagnose empty responses."""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return m.group(1).strip()[:60] if m else "(no title)"


def discover_shortcodes(client: Client, cfg: ScraperConfig) -> list[str]:
    """Extracts public shortcodes from the hashtag page.

    Priority: real DOM anchors (complete and reliable source; it was verified
    that every real shortcode also shows up as "code" in the JSON blobs). The
    JSON blobs are used only as a complement because they hold config keys
    unrelated to posts.

    Retries on empty results: Instagram intermittently serves a generic
    "shell page" (title="Instagram", ~490 KB, zero anchors) even for valid and
    very popular hashtags. It was checked with #moda, which returned 12 posts
    on one run and 0 on the next. A single attempt would turn that false
    negative into a definitive error.
    """
    url = TAG_PAGE.format(tag=cfg.hashtag)
    LOG.info("Phase 1: discovering posts at %s", url)

    attempts = max(3, cfg.retries)
    ordered: list[str] = []
    from_anchors: list[str] = []
    from_json: list[str] = []
    last_title = ""

    for attempt in range(1, attempts + 1):
        body = fetch_with_retry(client, url, retries=cfg.retries, label="[tag] ")
        html = body.decode("utf-8", errors="replace")
        last_title = _page_title(html)

        from_anchors = [c for c in RE_ANCHOR.findall(html) if is_plausible_shortcode(c)]
        from_json = [c for c in RE_JSON_CODE.findall(html) if is_plausible_shortcode(c)]
        # dict.from_keys = stable dedup (preserves order of appearance).
        ordered = list(dict.fromkeys(from_anchors + from_json))

        if ordered:
            break

        LOG.warning(
            "Phase 1: 0 shortcodes on attempt %d/%d "
            "(title=%r, %d bytes). Instagram may be serving its generic shell "
            "page; retrying.",
            attempt, attempts, last_title, len(html),
        )
        if attempt < attempts:
            time.sleep(random.uniform(4.0, 7.0))

    cfg.stats.discovered = len(ordered)

    LOG.info(
        "Phase 1: %d unique shortcodes discovered (%d anchors, %d JSON blobs)",
        len(ordered), len(from_anchors), len(from_json),
    )
    if len(ordered) < cfg.limit:
        LOG.warning(
            "%d posts were requested but the public page only exposes %d "
            "(there is no pagination in the HTML without a session; '?page=2' "
            "returns the same grid). The %d available ones will be processed.",
            cfg.limit, len(ordered), len(ordered),
        )
    if not ordered:
        LOG.warning(
            "After %d attempts no post was found for #%s "
            "(last title=%r). Likely causes: the hashtag has no public "
            "grid, or Instagram is rate-limiting this IP.",
            attempts, cfg.hashtag, last_title,
        )
    return ordered[: cfg.limit]


# --------------------------------------------------------------------------- #
# Phase 2 - Embed parsing
# --------------------------------------------------------------------------- #
def parse_embed(html: str, shortcode: str) -> Post:
    """Extracts author, caption and image from the embed HTML.

    Instagram's current DOM exposes no og:*, no JSON-LD and no <time>, so
    everything is resolved through CSS classes and DOM-hanging URLs.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Author ----------------------------------------------------------- #
    author: str | None = None
    caption_node = soup.find(class_=re.compile(r"\bCaption\b"))

    if caption_node is not None:
        for link in caption_node.find_all("a", href=True):
            m = RE_AUTHOR_LINK.match(link["href"].strip())
            if m:
                author = m.group(1)
                break

    # --- Caption ---------------------------------------------------------- #
    caption: str | None = None
    if caption_node is not None:
        text = caption_node.get_text(" ", strip=True)
        text = RE_COMMENTS_TAIL.sub("", text).strip()
        # The .Caption block starts with the username itself.
        if author and text.lower().startswith(author.lower()):
            text = text[len(author):].strip()
            if text.lower().startswith("·"):
                text = text.lstrip("· ").strip()
        caption = text or None

    # --- Main image ------------------------------------------------------- #
    image_url, is_video = _extract_media(soup, author)

    # --- Fallback author (alt of the media itself) ------------------------ #
    if author is None:
        for img in soup.find_all("img", alt=True):
            m = RE_AUTHOR_ALT.search(img["alt"])
            if m:
                author = m.group(1)
                break

    return Post(
        shortcode=shortcode,
        author=author,
        caption=caption,
        image_url=image_url,
        is_video=is_video,
    )


def is_unavailable(post: Post, html: str) -> bool:
    """Detects Instagram's "soft 404".

    Important: for nonexistent, private or deleted shortcodes the embed does
    NOT return 404 but HTTP 200 with a notice page
    ("The link to this photo or video may be broken..."). Without this check
    those cases would be logged as valid posts with all fields null.
    """
    if any(marker in html for marker in SOFT_404_MARKERS):
        return True
    return post.image_url is None and post.caption is None


def _extract_media(soup: BeautifulSoup, author: str | None) -> tuple[str | None, bool]:
    """Returns (media_url, is_video).

    Selection hierarchy:
      1. `img.EmbeddedMediaImage` - stable anchor for the post's photo.
      2. `<video>` / `<source>`    - reels served as MP4.
      3. Longest .jpg CDN URL that is not the author's avatar.
    """
    # 1) The post's photo.
    node = soup.select_one("img.EmbeddedMediaImage")
    if node and node.get("src"):
        return node["src"], False

    # 2) An MP4 reel: we keep the cover frame via poster.
    video = soup.find("video")
    if video is not None:
        poster = video.get("poster")
        if poster:
            return poster, True
        source = video.find("source", src=True)
        if source:
            return source["src"], True

    # 3) Fallback: largest CDN candidate, excluding avatars and the user
    #    themself, whose avatar carries alt="{username}" and t51.2885-19.
    candidates: list[str] = []
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src.startswith("http"):
            continue
        if RE_AVATAR_CDN.search(src):
            continue
        alt = (img.get("alt") or "").strip()
        if author and alt == author:
            continue
        if not re.search(r"\.(?:jpg|jpeg|png)(\?|$)", src, re.IGNORECASE):
            continue
        candidates.append(src)

    if candidates:
        # High-resolution URLs (-15) are longer; ties broken by length.
        return max(candidates, key=len), False

    return None, False


def shortcode_to_media_id(shortcode: str) -> int:
    """Decodes a shortcode into Instagram's numeric media ID."""
    value = 0
    for char in shortcode:
        try:
            value = value * 64 + SHORTCODE_ALPHABET.index(char)
        except ValueError as exc:
            raise ValueError(f"invalid shortcode: {shortcode!r}") from exc
    return value


def infer_published_at(shortcode: str) -> Optional[str]:
    """*Inferred* publication date from the shortcode (snowflake).

    WARNING: it is an estimate derived from the ID, not data returned by
    Instagram. It is flagged as inferred in the resulting JSON.
    """
    try:
        media_id = shortcode_to_media_id(shortcode)
    except ValueError:
        return None
    seconds = ((media_id >> 23) + MEDIA_ID_EPOCH_MS) / 1000.0
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    # Discards future dates or ones before 2010 (corrupt ID).
    if dt.year < 2010 or dt > datetime.now(timezone.utc):
        return None
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# Phase 3 - Image download and persistence
# --------------------------------------------------------------------------- #
def download_image(
    client: Client, cfg: ScraperConfig, post: Post
) -> Optional[Path]:
    """Downloads the post's image to {output_dir}/{shortcode}.jpg."""
    if not post.image_url:
        LOG.warning("[%s] no image URL; metadata only", post.shortcode)
        return None

    dest = cfg.output_dir / f"{post.shortcode}.jpg"
    if dest.exists() and dest.stat().st_size > 0 and not cfg.overwrite:
        LOG.info("[%s] already exists, not downloading again", post.shortcode)
        post.local_image_path = str(dest)
        return dest

    try:
        body = fetch_with_retry(
            client, post.image_url, retries=cfg.retries, label=f"[{post.shortcode}] "
        )
    except FetchError as exc:
        LOG.warning("[%s] image download failed: %s", post.shortcode, exc)
        return None

    if len(body) < 1024:
        LOG.warning("[%s] suspiciously small image response", post.shortcode)
        return None

    dest.write_bytes(body)
    LOG.info("[%s] image saved (%d bytes)", post.shortcode, len(body))
    post.local_image_path = str(dest)
    return dest


def write_metadata(posts: list[Post], cfg: ScraperConfig) -> Path:
    """Writes metadata.json with the processed posts and the statistics."""
    path = cfg.output_dir / "metadata.json"
    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "hashtag": cfg.hashtag,
        "requested_limit": cfg.limit,
        "method": "public embed endpoint (no auth)",
        "stats": cfg.stats.as_dict(),
        "posts": [p.to_dict() for p in posts],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def run(cfg: ScraperConfig) -> list[Post]:
    """Full pipeline: discover -> parse -> download -> persist."""
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    client = build_client(cfg)
    posts: list[Post] = []

    try:
        # --- Phase 1 ------------------------------------------------------ #
        shortcodes = discover_shortcodes(client, cfg)
        if not shortcodes:
            return posts

        # --- Phase 2 + 3 -------------------------------------------------- #
        LOG.info("Phase 2: processing %d posts", len(shortcodes))
        for index, shortcode in enumerate(shortcodes, start=1):
            LOG.info(
                "[%d/%d] %s", index, len(shortcodes), shortcode
            )
            url = EMBED_PAGE.format(shortcode=shortcode)

            try:
                body = fetch_with_retry(
                    client, url, retries=cfg.retries, label=f"[{shortcode}] "
                )
            except NotFoundError:
                LOG.warning(
                    "[%s] 404: post deleted, private or nonexistent; skipping",
                    shortcode,
                )
                cfg.stats.skipped_private += 1
                human_delay()
                continue
            except RateLimitedError as exc:
                cfg.stats.rate_limited += 1
                LOG.error("[%s] sustained rate limit: %s", shortcode, exc)
                break  # further insistence would worsen the block
            except FetchError as exc:
                LOG.error("[%s] unrecoverable error: %s", shortcode, exc)
                cfg.stats.failed += 1
                human_delay()
                continue

            html = body.decode("utf-8", errors="replace")
            post = parse_embed(html, shortcode)
            post.scraped_at = datetime.now(timezone.utc).isoformat()

            # Soft 404: Instagram returns 200 with a notice instead of the post.
            if is_unavailable(post, html):
                LOG.warning(
                    "[%s] not available (deleted or private); Instagram "
                    "answers 200 with a broken-link notice; skipping",
                    shortcode,
                )
                cfg.stats.skipped_private += 1
                human_delay()
                continue

            if cfg.decode_post_time:
                post.post_published_at = infer_published_at(shortcode)
                post.post_published_at_inferred = post.post_published_at is not None

            if cfg.decode_post_time and not post.caption:
                LOG.warning(
                    "[%s] empty caption; the embed layout may have changed",
                    shortcode,
                )

            if download_image(client, cfg, post) is not None:
                cfg.stats.images_saved += 1

            posts.append(post)
            cfg.stats.processed += 1

            # Human pacing: also after the last request.
            human_delay()

    finally:
        client.close()

    # --- Persistence ------------------------------------------------------ #
    meta_path = write_metadata(posts, cfg)
    LOG.info("Metadata -> %s", meta_path)
    return posts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ig_embed_scraper.py",
        description=(
            "PoC: extracts public Instagram posts by hashtag via the embed "
            "route (no login, no credentials)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--hashtag", required=True, help="Target hashtag, without the '#' (e.g. sneakers)"
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Max posts to process (the public page exposes ~12-14)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output folder (default ./downloads/{hashtag})",
    )
    parser.add_argument(
        "--proxy-url", default=None,
        help="Proxy HTTP/HTTPS/SOCKS5, e.g. socks5://127.0.0.1:9050",
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="Retries per request on 429/5xx"
    )
    parser.add_argument("--timeout", type=float, default=25.0, help="HTTP timeout (s)")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Download again images already present",
    )
    parser.add_argument(
        "--decode-post-time", action="store_true",
        help="Adds INFERRED post_published_at from the shortcode "
             "(the embed no longer exposes the real date)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log at DEBUG level"
    )
    return parser


def normalize_hashtag(raw: str) -> str:
    return raw.strip().lstrip("#@").strip().lower()


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    hashtag = normalize_hashtag(args.hashtag)
    if not hashtag:
        print("Error: --hashtag cannot be empty.", file=sys.stderr)
        return 2
    if args.limit < 1:
        print("Error: --limit must be >= 1.", file=sys.stderr)
        return 2

    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path.cwd() / "downloads" / hashtag
    )

    cfg = ScraperConfig(
        hashtag=hashtag,
        limit=args.limit,
        output_dir=output_dir,
        proxy_url=args.proxy_url,
        retries=args.retries,
        timeout=args.timeout,
        decode_post_time=args.decode_post_time,
        overwrite=args.overwrite,
    )

    LOG.info("Hashtag #%s | limit=%d | output=%s", hashtag, cfg.limit, output_dir)
    posts = run(cfg)

    s = cfg.stats
    LOG.info(
        "Done: %d processed (%d images), %d 404 skipped, "
        "%d rate-limited, %d failed",
        s.processed, s.images_saved, s.skipped_private, s.rate_limited, s.failed,
    )

    if not posts:
        print(
            "\nNo posts were obtained. Likely causes: nonexistent hashtag, "
            "Instagram login wall, or blocked IP (try --proxy-url).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())