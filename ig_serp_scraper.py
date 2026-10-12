#!/usr/bin/env python3
"""
Proof of Concept: Indirect Indexing Strategy (SERP Scraping).

Discovers public Instagram posts WITHOUT touching Instagram's indexing
directly and WITHOUT user credentials:

    Search engine      --(dork site:instagram.com)-->  Indexed URLs
                                                              |
                                                       shortcodes
                                                              |
        https://www.instagram.com/p/{code}/embed/captioned/  -->  data + photo

Findings measured against the real engines (see README):

  * DuckDuckGo HTML (`html.duckduckgo.com/html/`) is the ONLY engine that
    returns post URLs with the `site:instagram.com/p/` operator.
      - Google: replies 200 but returns the JS consent page, 0 URLs.
      - Bing:   IGNORES the `site:` operator -> returns news sites, 0 URLs.
  * DDG's classic pagination is BLOCKED: `&s=10` returns HTTP 202.
    That is why scale is achieved by ROTATING DORKS: each distinct dork
    contributes ~9-10 new shortcodes, up to a maximum of ~10 per dork.
  * DDG replies HTTP 202 as a throttling signal (not 429). It needs wide
    waits; with 2-4 s between queries it blocks on the 2nd or 3rd.
  * The shortcodes obtained this way DO resolve to real, live posts: it was
    validated that 6/6 returned caption and image from the public embed.

Usage:
    python ig_serp_scraper.py --query "running shoes" --limit 20
    python ig_serp_scraper.py --query sneakers --limit 40 --delay-min 8 --delay-max 15
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Optional, Protocol

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Missing beautifulsoup4 ->  pip install -r requirements.txt")

try:
    from pydantic import BaseModel, Field, HttpUrl, field_validator  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Missing pydantic ->  pip install -r requirements.txt")


LOG: logging.Logger = logging.getLogger("serp_scraper")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
EMBED_URL: Final[str] = "https://www.instagram.com/p/{shortcode}/embed/captioned/"
DDG_HTML_URL: Final[str] = "https://html.duckduckgo.com/html/"

# Instagram shortcode inside the indexed URLs.
RE_SHORTCODE: Final[re.Pattern[str]] = re.compile(
    r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_\-]{10,12})"
)
# Author hanging from the embed's Caption block.
RE_AUTHOR_LINK: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/(?:\?|$)"
)
RE_AUTHOR_ALT: Final[re.Pattern[str]] = re.compile(
    r"Instagram post shared by @([A-Za-z0-9._]+)"
)
# Avatar: never the post's photo.
RE_AVATAR_CDN: Final[re.Pattern[str]] = re.compile(r"/t51\.2885-19/")
RE_COMMENTS_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s*View all ([\d.,]+[KkMm]? )?comments?\.?\s*$", re.IGNORECASE
)
SOFT_404_MARKERS: Final[tuple[str, ...]] = (
    "may be broken, or the post may have been removed",
    "This page isn't available",
    "Sorry, this page isn't available",
)
CAPTCHA_MARKERS: Final[tuple[str, ...]] = (
    "captcha", "unusual traffic", "are you a robot", "challenge-form",
    "anomaly.js", "bots use duckduckgo too",
)

# Dork templates. Rotating between them is the "pagination" mechanism this
# technique has: each template returns a different set.
DORK_TEMPLATES: Final[tuple[str, ...]] = (
    'site:instagram.com/p/ "{kw}"',
    'site:instagram.com/reel/ "{kw}"',
    'site:instagram.com/p/ {kw} review',
    'site:instagram.com/reel/ {kw} haul',
    'site:instagram.com/p/ {kw} outfit',
    'site:instagram.com/p/ best {kw}',
    'site:instagram.com/p/ {kw} unboxing',
    'site:instagram.com/reel/ {kw} outfit',
    'site:instagram.com/p/ {kw} collection',
    'site:instagram.com/p/ new {kw}',
    'site:instagram.com/p/ {kw} try on',
    'site:instagram.com/reel/ {kw} routine',
)


# --------------------------------------------------------------------------- #
# Data models (pydantic)
# --------------------------------------------------------------------------- #
class InstagramPost(BaseModel):
    """A post extracted from the public embed."""

    shortcode: str
    author: Optional[str] = None
    caption: Optional[str] = None
    image_url: Optional[str] = None
    local_image_path: Optional[str] = None
    # SERP URL that revealed this shortcode: discovery traceability.
    source_serp_url: str
    scraped_at: datetime
    is_video: bool = False
    source_engine: str = "ddg"

    @field_validator("scraped_at")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class ScrapeStats(BaseModel):
    dorks_executed: int = 0
    dorks_throttled: int = 0
    discovered: int = 0
    processed: int = 0
    images_saved: int = 0
    skipped_unavailable: int = 0
    failed: int = 0


class MetadataFile(BaseModel):
    query: str
    engine: str
    requested_limit: int
    method: str = "SERP indirect indexing + public embed (no auth)"
    scraped_at: datetime
    stats: ScrapeStats
    posts: list[InstagramPost]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """Recoverable failure in a download."""


class NotFoundError(FetchError):
    """HTTP 404 - nonexistent resource."""


class RateLimitedError(FetchError):
    """HTTP 429 or 202 - the engine is rate-limiting or asking for verification."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CaptchaError(FetchError):
    """The engine replied with a human/captcha verification."""


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class HttpClient(Protocol):
    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]: ...
    def get_bytes(self, url: str) -> bytes: ...
    def close(self) -> None: ...


def _headers(engine: str) -> dict[str, str]:
    """Full desktop headers.

    The `Sec-Fetch-*` replicate a real navigation started from the address
    bar; their absence is a trivial automation signal.
    """
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    if engine == "ddg":
        h["Referer"] = "https://duckduckgo.com/"
    elif engine == "bing":
        h["Referer"] = "https://www.bing.com/"
    else:  # Instagram embed
        h["Referer"] = "https://www.instagram.com/"
    return h


class CurlCffiClient:
    """Transport with Chrome TLS impersonation (real JA3/JA4)."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0) -> None:
        from curl_cffi import requests as cc  # lazy import

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._s = cc.Session(impersonate="chrome", proxies=proxies, timeout=timeout)

    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]:
        r = self._s.get(url, params=params, headers=_headers(engine), allow_redirects=True)
        self._check(r.status_code, url)
        return r.status_code, r.text

    def get_bytes(self, url: str) -> bytes:
        r = self._s.get(url, headers=_headers("embed"), allow_redirects=True, timeout=30)
        self._check(r.status_code, url)
        return r.content

    @staticmethod
    def _check(status: int, url: str) -> None:
        # DDG uses 202 (not 429) as its throttling signal.
        if status in (429, 202):
            raise RateLimitedError(f"HTTP {status} at {url}")
        if status == 404:
            raise NotFoundError(f"404 at {url}")
        if status >= 400:
            raise FetchError(f"HTTP {status} at {url}")

    def close(self) -> None:
        self._s.close()


class HttpxClient:
    """Fallback if curl_cffi is not available."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0) -> None:
        import httpx  # lazy import

        self._s = httpx.Client(timeout=timeout, follow_redirects=True, proxy=proxy_url)

    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]:
        r = self._s.get(url, params=params, headers=_headers(engine))
        CurlCffiClient._check(r.status_code, url)
        return r.status_code, r.text

    def get_bytes(self, url: str) -> bytes:
        r = self._s.get(url, headers=_headers("embed"), timeout=30)
        CurlCffiClient._check(r.status_code, url)
        return r.content

    def close(self) -> None:
        self._s.close()


def build_client(proxy_url: Optional[str], timeout: float) -> HttpClient:
    if proxy_url:
        LOG.info("Proxy active: %s", re.sub(r"//[^@/]*@", "//***:***@", proxy_url))
    try:
        return CurlCffiClient(proxy_url, timeout)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("curl_cffi not available (%s); using httpx", exc)
        return HttpxClient(proxy_url, timeout)


# --------------------------------------------------------------------------- #
# Phase 1 - Discovery in SERPs
# --------------------------------------------------------------------------- #
def build_dorks(keyword: str, max_dorks: int) -> list[str]:
    """Builds the dork rotation for a keyword."""
    kw = keyword.strip().strip('"')
    seen: list[str] = []
    for tpl in DORK_TEMPLATES:
        dork = tpl.format(kw=kw)
        if dork not in seen:
            seen.append(dork)
        if len(seen) >= max_dorks:
            break
    return seen


class SerpClient:
    """DuckDuckGo HTML client.

    Other engines were measured and are not viable for this technique:
      - Google: 200 but returns the JS consent page (0 URLs).
      - Bing:   ignores `site:` and returns news sites (0 IG URLs).
    """

    def __init__(self, client: HttpClient, engine: str = "ddg") -> None:
        self._c = client
        self._engine = engine

    def search(self, dork: str) -> tuple[dict[str, str], str]:
        """Runs a dork. Returns ({shortcode: serp_url}, used_serp_url)."""
        if self._engine == "ddg":
            return self._search_ddg(dork)
        raise FetchError(f"Engine {self._engine!r} not implemented (see README).")

    def _search_ddg(self, dork: str) -> tuple[dict[str, str], str]:
        serp_url = f"{DDG_HTML_URL}?q={dork.replace(' ', '+').replace('"', '%22')}"
        _, html = self._c.get_text(DDG_HTML_URL, params={"q": dork}, engine="ddg")

        low = html.lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            raise CaptchaError("DDG returned verification/captcha")

        # dict comprehension: dedup keeping the shortcode as key.
        return {c: serp_url for c in set(RE_SHORTCODE.findall(html))}, serp_url


def discover(
    serp: SerpClient,
    keyword: str,
    limit: int,
    max_dorks: int,
    delay: tuple[float, float],
) -> dict[str, str]:
    """Phase 1: rotate dorks until `limit` shortcodes are gathered.

    Returns {shortcode: serp_url_that_revealed_it}.
    """
    discovered: dict[str, str] = {}
    dorks = build_dorks(keyword, max_dorks)
    LOG.info("Phase 1: %d dorks for %r", len(dorks), keyword)

    for i, dork in enumerate(dorks, start=1):
        if len(discovered) >= limit:
            break
        LOG.info("  [%d/%d] dork: %s", i, len(dorks), dork)
        try:
            found, serp_url = serp.search(dork)
            new = {c: u for c, u in found.items() if c not in discovered}
            discovered.update(new)
            LOG.info("      +%d new (total %d)", len(new), len(discovered))
        except (RateLimitedError, CaptchaError) as exc:
            LOG.warning("      engine blocked (%s); skipping this dork", type(exc).__name__)
        except FetchError as exc:
            LOG.warning("      dork failure: %s", exc)

        if i < len(dorks) and len(discovered) < limit:
            time.sleep(random.uniform(*delay))

    LOG.info("Phase 1: %d unique shortcodes discovered", len(discovered))
    return discovered


# --------------------------------------------------------------------------- #
# Phase 2 - Public embed parsing
# --------------------------------------------------------------------------- #
def parse_embed(html: str, shortcode: str, source_serp_url: str) -> InstagramPost:
    """Extracts author, caption and image from Instagram's public embed."""
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

    return InstagramPost(
        shortcode=shortcode, author=author, caption=caption, image_url=image_url,
        local_image_path=None, source_serp_url=source_serp_url,
        scraped_at=datetime.now(timezone.utc), is_video=is_video,
    )


def _extract_media(soup: BeautifulSoup, author: str | None) -> tuple[Optional[str], bool]:
    """Post photo: `img.EmbeddedMediaImage`. The author's avatar is also an
    <img> and must NOT be confused with the post."""
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
    """Soft 404: Instagram returns 200 (not 404) with a broken-link notice."""
    if any(m in html for m in SOFT_404_MARKERS):
        return True
    return post.image_url is None and post.caption is None


# --------------------------------------------------------------------------- #
# Phase 3 - Download and persistence
# --------------------------------------------------------------------------- #
def download_image(client: HttpClient, out_dir: Path, post: InstagramPost,
                   overwrite: bool) -> bool:
    """Downloads the post's image to {output_dir}/{shortcode}.jpg."""
    if not post.image_url:
        return False
    dest = out_dir / f"{post.shortcode}.jpg"
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        post.local_image_path = str(dest)
        return False
    try:
        data = client.get_bytes(post.image_url)
    except FetchError as exc:
        LOG.warning("[%s] image download failed: %s", post.shortcode, exc)
        return False
    if len(data) < 1024:
        LOG.warning("[%s] suspiciously small image response", post.shortcode)
        return False
    dest.write_bytes(data)
    post.local_image_path = str(dest)
    LOG.info("[%s] image saved (%d bytes)", post.shortcode, len(data))
    return True


def write_metadata(
    posts: list[InstagramPost], stats: ScrapeStats, query: str, engine: str, limit: int, out_dir: Path
) -> Path:
    path = out_dir / "metadata.json"
    payload = MetadataFile(
        query=query, engine=engine, requested_limit=limit,
        scraped_at=datetime.now(timezone.utc), stats=stats, posts=posts,
    )
    path.write_text(payload.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> list[InstagramPost]:
    keyword = args.query.strip().strip('"')
    out_dir = Path(args.output_dir or f"output/{re.sub(r'[^a-z0-9]+', '_', keyword.lower()).strip('_')}")
    out_dir.mkdir(parents=True, exist_ok=True)

    client = build_client(args.proxy_url, args.timeout)
    stats = ScrapeStats()
    posts: list[InstagramPost] = []

    try:
        # Phase 1
        if args.shortcodes:
            LOG.info("Phase 1: skipped (--shortcodes, %d provided)", len(args.shortcodes))
            discovered = {
                sc.strip(): args.source_serp_url or f"(shortcodes provided manually)"
                for sc in args.shortcodes
            }
        else:
            serp = SerpClient(client, args.search_engine)
            discovered = discover(
                serp, keyword, args.limit, args.max_dorks, (args.delay_min, args.delay_max)
            )
        stats.discovered = len(discovered)
        if not discovered:
            print("\nNo URLs found. Engine blocked, or keyword too specific?",
                  file=sys.stderr)
            return posts

        # Phase 2
        LOG.info("Phase 2: %d shortcodes to process", len(discovered))
        for i, (shortcode, serp_url) in enumerate(discovered.items(), start=1):
            LOG.info("  [%d/%d] %s", i, len(discovered), shortcode)
            try:
                _, html = client.get_text(EMBED_URL.format(shortcode=shortcode))
            except NotFoundError:
                stats.skipped_unavailable += 1
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                continue
            except RateLimitedError as exc:
                stats.failed += 1
                LOG.error("  rate limit on embed (%s); stopping", exc)
                break
            except FetchError as exc:
                stats.failed += 1
                LOG.warning("  embed failure: %s", exc)
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                continue

            post = parse_embed(html, shortcode, serp_url)
            if is_unavailable(post, html):
                LOG.warning("  not available (private/deleted); skipping")
                stats.skipped_unavailable += 1
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                continue

            if download_image(client, out_dir, post, args.overwrite):
                stats.images_saved += 1
            posts.append(post)
            stats.processed += 1

            if i < len(discovered):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
    finally:
        client.close()

    # Phase 3
    meta = write_metadata(posts, stats, keyword, args.search_engine, args.limit, out_dir)
    LOG.info("Metadata -> %s", meta)
    return posts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ig_serp_scraper.py",
        description="PoC: discovers public Instagram posts via SERP indexing "
                    "(dorks) and extracts data via the public embed, no credentials.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--query", required=True, help="Keyword or hashtag, e.g. 'running shoes'")
    p.add_argument("--limit", type=int, default=20, help="Max shorts to process")
    p.add_argument("--search-engine", default="ddg", choices=["ddg", "bing", "google"],
                   help="SERP engine (only ddg was viable in testing; see README)")
    p.add_argument("--output-dir", default=None, help="Output (default ./output/{slug})")
    p.add_argument("--proxy-url", default=None, help="Proxy HTTP/HTTPS/SOCKS5")
    p.add_argument("--max-dorks", type=int, default=8,
                   help="Max dorks to rotate (each one ~+9 shortcodes)")
    p.add_argument("--shortcodes", type=str, default=None,
                   help="Comma-separated shortcodes: skip Phase 1 and process "
                        "directly. Useful when the engine is rate-limited (DDG 202) "
                        "or to reprocess already discovered posts.")
    p.add_argument("--source-serp-url", default="",
                   help="SERP URL to attribute when Phase 1 is skipped with --shortcodes")
    p.add_argument("--delay-min", type=float, default=8.0, help="Minimum delay (s)")
    p.add_argument("--delay-max", type=float, default=15.0, help="Maximum delay (s)")
    p.add_argument("--timeout", type=float, default=25.0, help="HTTP timeout (s)")
    p.add_argument("--overwrite", action="store_true", help="Re-download images")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.shortcodes = (
        [s for s in (args.shortcodes or "").split(",") if s.strip()]
        if args.shortcodes else []
    )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    if args.search_engine != "ddg":
        LOG.warning(
            "Engine %r was measured as NOT viable for this technique "
            "(Google returns the consent page; Bing ignores `site:`). "
            "ddg will be used.", args.search_engine,
        )
        args.search_engine = "ddg"
    if args.delay_max < args.delay_min:
        print("Error: --delay-max must be >= --delay-min.", file=sys.stderr)
        return 2

    LOG.info("query=%r limit=%d engine=%s", args.query, args.limit, args.search_engine)
    posts = run(args)

    if not posts:
        print("\n0 posts. Likely causes: rate-limited engine (DDG replies 202), "
              "keyword with no indexed results, or blocked IP (use --proxy-url).",
              file=sys.stderr)
        return 1
    LOG.info("Done: %d posts, %d images", len(posts),
             sum(1 for p in posts if p.local_image_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())