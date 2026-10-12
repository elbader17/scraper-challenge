#!/usr/bin/env python3
"""
Prueba de Concepto: Estrategia de Indexación Indirecta (SERP Scraping).

Descubre publicaciones públicas de Instagram SIN tocar directamente la
indexación de Instagram y SIN credenciales de usuario:

    Motor de búsqueda  --(dork site:instagram.com)-->  URLs indexadas
                                                              |
                                                       shortcodes
                                                              |
        https://www.instagram.com/p/{code}/embed/captioned/  -->  datos + foto

Hallazgos medidos contra los motores reales (ver README):

  * DuckDuckGo HTML (`html.duckduckgo.com/html/`) es el ÚNICO motor que
    devuelve URLs de posts con el operador `site:instagram.com/p/`.
      - Google: responde 200 pero devuelve la página JS de consentimiento, 0 URLs.
      - Bing:   IGNORA el operador `site:` -> devuelve sitios de noticias, 0 URLs.
  * La paginación clásica de DDG está BLOQUEADA: `&s=10` devuelve HTTP 202.
    Por eso la escala se logra ROTANDO DORKS: cada dork distinto aporta ~9-10
    shortcodes nuevos, hasta un máximo de ~10 por dork.
  * DDG responde HTTP 202 como señal de throttling (no 429). Requiere esperas
    amplias; con 2-4 s entre consultas se bloquea en la 2ª o 3ª.
  * Los shortcodes así obtenidos SÍ resuelven a posts reales y vivos: se
    validó que 6/6 devolvieron caption e imagen desde el embed público.

Uso:
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
    sys.exit("Falta beautifulsoup4 ->  pip install -r requirements.txt")

try:
    from pydantic import BaseModel, Field, HttpUrl, field_validator  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Falta pydantic ->  pip install -r requirements.txt")


LOG: logging.Logger = logging.getLogger("serp_scraper")

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
EMBED_URL: Final[str] = "https://www.instagram.com/p/{shortcode}/embed/captioned/"
DDG_HTML_URL: Final[str] = "https://html.duckduckgo.com/html/"

# Shortcode de Instagram en las URLs indexadas.
RE_SHORTCODE: Final[re.Pattern[str]] = re.compile(
    r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_\-]{10,12})"
)
# Autor colgado del bloque Caption del embed.
RE_AUTHOR_LINK: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/(?:\?|$)"
)
RE_AUTHOR_ALT: Final[re.Pattern[str]] = re.compile(
    r"Instagram post shared by @([A-Za-z0-9._]+)"
)
# Avatar: nunca es la foto del post.
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

# Plantillas de dork. La rotación entre ellas es el mecanismo de "paginación"
# del que dispone esta técnica: cada plantilla devuelve un conjunto distinto.
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
# Modelos de datos (pydantic)
# --------------------------------------------------------------------------- #
class InstagramPost(BaseModel):
    """Una publicación extraída del embed público."""

    shortcode: str
    author: Optional[str] = None
    caption: Optional[str] = None
    image_url: Optional[str] = None
    local_image_path: Optional[str] = None
    # URL de la SERP que reveló este shortcode: trazabilidad del descubrimiento.
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
# Excepciones
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """Fallo recuperable en una descarga."""


class NotFoundError(FetchError):
    """HTTP 404 - recurso inexistente."""


class RateLimitedError(FetchError):
    """HTTP 429 o 202 - el motor está limitando o pidiendo verificación."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CaptchaError(FetchError):
    """El motor respondió con una verificación humana/captcha."""


# --------------------------------------------------------------------------- #
# Cliente HTTP
# --------------------------------------------------------------------------- #
class HttpClient(Protocol):
    def get_text(self, url: str, *, params: Optional[dict[str, str]] = None,
                 engine: str = "embed") -> tuple[int, str]: ...
    def get_bytes(self, url: str) -> bytes: ...
    def close(self) -> None: ...


def _headers(engine: str) -> dict[str, str]:
    """Headers completos de escritorio.

    Los `Sec-Fetch-*` replican una navegación real iniciada desde la barra de
    direcciones; su ausencia es una señal trivial de automatización.
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
    else:  # embed de Instagram
        h["Referer"] = "https://www.instagram.com/"
    return h


class CurlCffiClient:
    """Transporte con impersonación de TLS de Chrome (JA3/JA4 reales)."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0) -> None:
        from curl_cffi import requests as cc  # import perezoso

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
        # DDG usa 202 (y no 429) como señal de throttling.
        if status in (429, 202):
            raise RateLimitedError(f"HTTP {status} en {url}")
        if status == 404:
            raise NotFoundError(f"404 en {url}")
        if status >= 400:
            raise FetchError(f"HTTP {status} en {url}")

    def close(self) -> None:
        self._s.close()


class HttpxClient:
    """Respaldo si curl_cffi no está disponible."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0) -> None:
        import httpx  # import perezoso

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
        LOG.info("Proxy activo: %s", re.sub(r"//[^@/]*@", "//***:***@", proxy_url))
    try:
        return CurlCffiClient(proxy_url, timeout)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("curl_cffi no disponible (%s); uso httpx", exc)
        return HttpxClient(proxy_url, timeout)


# --------------------------------------------------------------------------- #
# Fase 1 - Descubrimiento en SERPs
# --------------------------------------------------------------------------- #
def build_dorks(keyword: str, max_dorks: int) -> list[str]:
    """Genera la rotación de dorks para una keyword."""
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
    """Cliente de DuckDuckGo HTML.

    Se midieron otros motores y no son viables para esta técnica:
      - Google: 200 pero devuelve la página JS de consentimiento (0 URLs).
      - Bing:   ignora `site:` y devuelve sitios de noticias (0 URLs de IG).
    """

    def __init__(self, client: HttpClient, engine: str = "ddg") -> None:
        self._c = client
        self._engine = engine

    def search(self, dork: str) -> tuple[dict[str, str], str]:
        """Ejecuta un dork. Devuelve ({shortcode: url_serp}, url_serp_usada)."""
        if self._engine == "ddg":
            return self._search_ddg(dork)
        raise FetchError(f"Motor {self._engine!r} no implementado (ver README).")

    def _search_ddg(self, dork: str) -> tuple[dict[str, str], str]:
        serp_url = f"{DDG_HTML_URL}?q={dork.replace(' ', '+').replace('"', '%22')}"
        _, html = self._c.get_text(DDG_HTML_URL, params={"q": dork}, engine="ddg")

        low = html.lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            raise CaptchaError("DDG devolvió verificación/captcha")

        # dict comprehension: dedup conservando el shortcode como clave.
        return {c: serp_url for c in set(RE_SHORTCODE.findall(html))}, serp_url


def discover(
    serp: SerpClient,
    keyword: str,
    limit: int,
    max_dorks: int,
    delay: tuple[float, float],
) -> dict[str, str]:
    """Fase 1: rota dorks hasta reunir `limit` shortcodes.

    Devuelve {shortcode: url_de_la_serp_que_lo_reveló}.
    """
    discovered: dict[str, str] = {}
    dorks = build_dorks(keyword, max_dorks)
    LOG.info("Fase 1: %d dorks para %r", len(dorks), keyword)

    for i, dork in enumerate(dorks, start=1):
        if len(discovered) >= limit:
            break
        LOG.info("  [%d/%d] dork: %s", i, len(dorks), dork)
        try:
            found, serp_url = serp.search(dork)
            new = {c: u for c, u in found.items() if c not in discovered}
            discovered.update(new)
            LOG.info("      +%d nuevos (total %d)", len(new), len(discovered))
        except (RateLimitedError, CaptchaError) as exc:
            LOG.warning("      motor bloqueado (%s); salto este dork", type(exc).__name__)
        except FetchError as exc:
            LOG.warning("      fallo del dork: %s", exc)

        if i < len(dorks) and len(discovered) < limit:
            time.sleep(random.uniform(*delay))

    LOG.info("Fase 1: %d shortcodes únicos descubiertos", len(discovered))
    return discovered


# --------------------------------------------------------------------------- #
# Fase 2 - Parseo del embed público
# --------------------------------------------------------------------------- #
def parse_embed(html: str, shortcode: str, source_serp_url: str) -> InstagramPost:
    """Extrae autor, caption e imagen del embed público de Instagram."""
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
    """Foto del post: `img.EmbeddedMediaImage`. El avatar del autor también es un
    <img> y NO debe confundirse con el post."""
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
    """Soft 404: Instagram devuelve 200 (no 404) con aviso de enlace roto."""
    if any(m in html for m in SOFT_404_MARKERS):
        return True
    return post.image_url is None and post.caption is None


# --------------------------------------------------------------------------- #
# Fase 3 - Descarga y persistencia
# --------------------------------------------------------------------------- #
def download_image(client: HttpClient, out_dir: Path, post: InstagramPost,
                   overwrite: bool) -> bool:
    """Descarga la imagen del post a {output_dir}/{shortcode}.jpg."""
    if not post.image_url:
        return False
    dest = out_dir / f"{post.shortcode}.jpg"
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        post.local_image_path = str(dest)
        return False
    try:
        data = client.get_bytes(post.image_url)
    except FetchError as exc:
        LOG.warning("[%s] falló la descarga de imagen: %s", post.shortcode, exc)
        return False
    if len(data) < 1024:
        LOG.warning("[%s] respuesta de imagen sospechosamente chica", post.shortcode)
        return False
    dest.write_bytes(data)
    post.local_image_path = str(dest)
    LOG.info("[%s] imagen guardada (%d bytes)", post.shortcode, len(data))
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
        # Fase 1
        if args.shortcodes:
            LOG.info("Fase 1: salteada (--shortcodes, %d provistos)", len(args.shortcodes))
            discovered = {
                sc.strip(): args.source_serp_url or f"(shortcodes provistos manualmente)"
                for sc in args.shortcodes
            }
        else:
            serp = SerpClient(client, args.search_engine)
            discovered = discover(
                serp, keyword, args.limit, args.max_dorks, (args.delay_min, args.delay_max)
            )
        stats.discovered = len(discovered)
        if not discovered:
            print("\nNo se hallaron URLs. ¿Motor bloqueado o keyword demasiado específica?",
                  file=sys.stderr)
            return posts

        # Fase 2
        LOG.info("Fase 2: %d shortcodes a procesar", len(discovered))
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
                LOG.error("  rate limit en embed (%s); parando", exc)
                break
            except FetchError as exc:
                stats.failed += 1
                LOG.warning("  fallo embed: %s", exc)
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                continue

            post = parse_embed(html, shortcode, serp_url)
            if is_unavailable(post, html):
                LOG.warning("  no disponible (privado/eliminado); omito")
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

    # Fase 3
    meta = write_metadata(posts, stats, keyword, args.search_engine, args.limit, out_dir)
    LOG.info("Metadata -> %s", meta)
    return posts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ig_serp_scraper.py",
        description="PoC: descubre posts públicos de Instagram vía indexación SERP "
                    "(dorks) y extrae datos vía embed público, sin credenciales.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--query", required=True, help="Keyword o hashtag, ej. 'running shoes'")
    p.add_argument("--limit", type=int, default=20, help="Máximo de shorts a procesar")
    p.add_argument("--search-engine", default="ddg", choices=["ddg", "bing", "google"],
                   help="Motor de SERP (sólo ddg fue viable en pruebas; ver README)")
    p.add_argument("--output-dir", default=None, help="Salida (por defecto ./output/{slug})")
    p.add_argument("--proxy-url", default=None, help="Proxy HTTP/HTTPS/SOCKS5")
    p.add_argument("--max-dorks", type=int, default=8,
                   help="Máximo de dorks a rotar (cada uno ~+9 shortcodes)")
    p.add_argument("--shortcodes", type=str, default=None,
                   help="Comma-separated shortcodes: saltear la Fase 1 y procesar "
                        "directamente. Útil cuando el motor está limitado (DDG 202) "
                        "o para reprocesar posts ya descubiertos.")
    p.add_argument("--source-serp-url", default="",
                   help="URL de SERP a atribuir cuando se saltea la Fase 1 con --shortcodes")
    p.add_argument("--delay-min", type=float, default=8.0, help="Delay mínimo (s)")
    p.add_argument("--delay-max", type=float, default=15.0, help="Delay máximo (s)")
    p.add_argument("--timeout", type=float, default=25.0, help="Timeout HTTP (s)")
    p.add_argument("--overwrite", action="store_true", help="Re-descargar imágenes")
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
            "El motor %r fue medido como NO viable para esta técnica "
            "(Google devuelve la página de consentimiento; Bing ignora `site:`). "
            "Se usará ddg.", args.search_engine,
        )
        args.search_engine = "ddg"
    if args.delay_max < args.delay_min:
        print("Error: --delay-max debe ser >= --delay-min.", file=sys.stderr)
        return 2

    LOG.info("query=%r limit=%d engine=%s", args.query, args.limit, args.search_engine)
    posts = run(args)

    if not posts:
        print("\n0 posts. Causas probables: motor limitado (DDG responde 202), "
              "keyword sin resultados indexados, o IP bloqueada (usá --proxy-url).",
              file=sys.stderr)
        return 1
    LOG.info("Listo: %d posts, %d imágenes", len(posts),
             sum(1 for p in posts if p.local_image_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())