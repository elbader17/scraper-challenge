#!/usr/bin/env python3
"""
Prueba de Concepto: extracción de publicaciones públicas de Instagram por hashtag
usando únicamente la ruta pública de *embeds* (sin login, sin cookies de usuario).

Técnicas verificadas empíricamente contra el sitio real:

  Fase 1 - Descubrimiento
      https://www.instagram.com/explore/tags/{hashtag}/
      El HTML renderizado en servidor contiene dos fuentes de shortcodes:
        a) anclas  <a href="/reel/<shortcode>/?utm_source=popular_topic_grid">
        b) blobs   "code":"<shortcode>"
      Sin paginación efectiva: ?page=2 devuelve el mismo grid (~12-14 posts).

  Fase 2 - Embed
      https://www.instagram.com/p/{shortcode}/embed/captioned/
      Devuelve 200 sin sesión. Observaciones importantes sobre el DOM actual:
        - NO hay <meta property="og:*">, NO hay JSON-LD, NO hay <time datetime>.
          El parseo debe ser 100% basado en clases CSS del DOM.
        - La foto principal es `img.EmbeddedMediaImage`
          (alt="Instagram post shared by @usuario").
        - El avatar del autor también es un <img>, sin clase, con
          alt="{username}" y su CDN contiene el segmento "t51.2885-19".
          -> confundirse con él produce imágenes de 40x40 en lugar del post.
        - El autor se obtiene del <a href="https://www.instagram.com/<user>/?utm_source=ig_embed">
          que cuelga del bloque `.Caption`.
        - En reels, el "frame" es una imagen estática servida por el CDN.

Uso:
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
# Dependencias
# --------------------------------------------------------------------------- #
try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Falta beautifulsoup4 ->  pip install -r requirements.txt")


LOG: logging.Logger = logging.getLogger("ig_scraper")

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

USER_AGENT_FALLBACK: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TAG_PAGE: Final[str] = "https://www.instagram.com/explore/tags/{tag}/"
EMBED_PAGE: Final[str] = "https://www.instagram.com/p/{shortcode}/embed/captioned/"

# Orden: primero anclas reales, luego blobs JSON de respaldo.
RE_ANCHOR: Final[re.Pattern[str]] = re.compile(
    r'href="/(?:p|reel|tv)/([A-Za-z0-9_\-]{5,})/'
)
RE_JSON_CODE: Final[re.Pattern[str]] = re.compile(r'"code":"([A-Za-z0-9_\-]{5,})"')

# Autor: enlace colgado del bloque Caption.
RE_AUTHOR_LINK: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/(?:\?|$)"
)
# Respaldo: alt="Instagram post shared by @usuario"
RE_AUTHOR_ALT: Final[re.Pattern[str]] = re.compile(
    r"Instagram post shared by @([A-Za-z0-9._]+)"
)
# Avatar: nunca es la foto del post.
RE_AVATAR_CDN: Final[re.Pattern[str]] = re.compile(r"/t51\.2885-19/")
# Tokens de configuración que el regex JSON puede confundir con un shortcode.
# Ejemplo real: {"IntlCurrentLocale",[],"code":"en_US"} -> "en_US".
RE_LOCALE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2,3}[_-][A-Z]{2}$")
# Los shortcodes de Instagram tienen 10-11 caracteres (12 con margen).
SHORTCODE_LEN: Final[tuple[int, int]] = (10, 12)
# Instagram responde 200 (no 404) a shortcodes eliminados, privados o inválidos,
# mostrando en su lugar esta página de aviso.
SOFT_404_MARKERS: Final[tuple[str, ...]] = (
    "may be broken, or the post may have been removed",
    "This page isn't available",
    "Sorry, this page isn't available",
)
# Ruido que Instagram concatena al final del texto del bloque Caption.
RE_COMMENTS_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s*View all ([\d.,]+[KkMm]? )?comments?\.?\s*$", re.IGNORECASE
)

# Alphabet oficial usado por los shortcodes de Instagram (URL-safe base64).
SHORTCODE_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# Epoch del "snowflake" de media IDs, derivado del primer post histórico
# de Instagram (id 12705937228168714 @ 2010-10-06T17:05:40Z).
MEDIA_ID_EPOCH_MS: Final[int] = 1284870074250

SLEEP_MIN: Final[float] = 1.5
SLEEP_MAX: Final[float] = 3.5


# --------------------------------------------------------------------------- #
# Modelo de datos
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Post:
    """Una publicación extraída del embed público."""

    shortcode: str
    author: Optional[str]
    caption: Optional[str]
    image_url: Optional[str]
    local_image_path: Optional[str] = None
    # ISO-8601 UTC del momento de la extracción. Siempre presente.
    scraped_at: str = ""
    # El embed público ya no expone la fecha de publicación (sin <time>,
    # sin JSON-LD, sin og:). Sólo se llena con --decode-post-time, y en ese
    # caso se marca como inferido, no verificado contra el servidor.
    post_published_at: Optional[str] = None
    post_published_at_inferred: bool = False
    is_video: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScrapeStats:
    """Contadores del pipeline, útiles para inspección y logs."""

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
# Transporte HTTP
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """Fallo recuperable al descargar una URL."""


class NotFoundError(FetchError):
    """HTTP 404 - post eliminado, privado, o hashtag inexistente."""


class RateLimitedError(FetchError):
    """HTTP 429 - límite de peticiones alcanzado."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Client(Protocol):
    """Interfaz mínima de transporte; permite swapear curl_cffi / httpx."""

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]: ...
    def close(self) -> None: ...


def _normalize_proxy(proxy: Optional[str]) -> Optional[dict[str, str]]:
    """curl_cffi espera un dict de schemes; httpx acepta una sola URL."""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


class CurlCffiClient:
    """Cliente por defecto: imita el handshake TLS/fingerprint de Chrome.

    Es el que evita la mayoría de los bloqueos 401/403 de Instagram, ya que el
    bot de Instagram valida la huella JA3 del cliente además de las cabeceras.
    """

    def __init__(self, cfg: ScraperConfig) -> None:
        from curl_cffi import requests as cc_requests  # import perezoso

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
                # Identificarse como embed es coherente con la ruta que pedimos
                # y reduce la probabilidad de que nos devuelvan un login wall.
                "Referer": "https://www.instagram.com/",
                "Upgrade-Insecure-Requests": "1",
            }
        )

    def get(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        r = self._s.get(url, allow_redirects=True)
        headers = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code == 404:
            raise NotFoundError(f"404 en {url}")
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            raise RateLimitedError(f"429 en {url}", float(ra) if ra else None)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} en {url}")
        return r.status_code, r.content, headers

    def close(self) -> None:
        self._s.close()


class HttpxClient:
    """Respaldo si curl_cffi no está disponible o falla al compilar/instalarse.

    Menos resistente al fingerprinting, pero suficiente para la ruta de embed
    cuando no se bloquea la IP.
    """

    def __init__(self, cfg: ScraperConfig) -> None:
        import httpx  # import perezoso

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
            raise NotFoundError(f"404 en {url}")
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            raise RateLimitedError(f"429 en {url}", float(ra) if ra else None)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code} en {url}")
        return r.status_code, r.content, headers

    def close(self) -> None:
        self._s.close()


def build_client(cfg: ScraperConfig) -> Client:
    if cfg.proxy_url:
        LOG.info("Proxy activo: %s", _redact_proxy(cfg.proxy_url))
    try:
        return CurlCffiClient(cfg)
    except Exception as exc:  # noqa: BLE001 - queremos degradar, no abortar
        LOG.warning("curl_cffi no disponible (%s); uso httpx como respaldo", exc)
        return HttpxClient(cfg)


def _redact_proxy(url: str) -> str:
    """Oculta credenciales embebidas en la URL del proxy antes de loguear."""
    return re.sub(r"//[^@/]*@", "//***:***@", url)


# --------------------------------------------------------------------------- #
# Utilidades de red
# --------------------------------------------------------------------------- #
def human_delay(extra: float = 0.0) -> None:
    """Pausa aleatoria para emular tráfico humano (1.5 - 3.5 s)."""
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
    """Descarga una URL con reintentos exponenciales ante 429/5xx.

    404 no se reintenta: significa que el recurso no existe, no que esté
    temporalmente caído.
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
                "429 rate limit en %s%s -> espero %.0fs (intento %d/%d)",
                label, url, wait, attempt, retries,
            )
            time.sleep(min(wait, 120.0))
            backoff *= 2
        except (FetchError, Exception) as exc:  # noqa: BLE001
            last = exc
            LOG.warning(
                "Fallo en %s%s (%s: %s) -> reintento %d/%d",
                label, url, type(exc).__name__, exc, attempt, retries,
            )
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2

    raise FetchError(f"No se pudo descargar {url}: {last}") from last


# --------------------------------------------------------------------------- #
# Fase 1 - Descubrimiento de shortcodes
# --------------------------------------------------------------------------- #
def is_plausible_shortcode(token: str) -> bool:
    """Filtra falsos positivos del regex de descubrimiento.

    Necesario porque el HTML incluye blobs de configuración del cliente (p.ej.
    {"IntlCurrentLocale",[],"code":"en_US"}) cuyos valores satisfacen un regex
    genérico de identificadores pero no son publicaciones.
    """
    if not SHORTCODE_LEN[0] <= len(token) <= SHORTCODE_LEN[1]:
        return False
    if RE_LOCALE.match(token):          # en_US, es_ES, pt_BR...
        return False
    return all(c in SHORTCODE_ALPHABET for c in token)


def _page_title(html: str) -> str:
    """Extrae el <title> de la página; sirve para diagnosticar respuestas vacías."""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return m.group(1).strip()[:60] if m else "(sin title)"


def discover_shortcodes(client: Client, cfg: ScraperConfig) -> list[str]:
    """Extrae shortcodes públicos de la página del hashtag.

    Prioridad: anclas reales del DOM (fuente completa y fiable; se verificó que
    todo shortcode real aparece también como "code" en los blobs JSON). Los
    blobs JSON se usan sólo como complemento porque contienen claves de
    configuración ajenas a las publicaciones.

    Reintenta ante resultados vacíos: Instagram sirve de forma intermitente una
    "página shell" genérica (title="Instagram", ~490 KB, cero anclas) incluso
    para hashtags válidos y muy populares. Se comprobó con #moda, que devolvió
    12 posts en una corrida y 0 en la siguiente. Un único intento convertiría
    ese falso negativo en un error definitivo.
    """
    url = TAG_PAGE.format(tag=cfg.hashtag)
    LOG.info("Fase 1: descubriendo posts en %s", url)

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
        # dict.from_keys = dedup estable (conserva el orden de aparición).
        ordered = list(dict.fromkeys(from_anchors + from_json))

        if ordered:
            break

        LOG.warning(
            "Fase 1: 0 shortcodes en el intento %d/%d "
            "(title=%r, %d bytes). Instagram puede estar sirviendo su página "
            "shell genérica; reintento.",
            attempt, attempts, last_title, len(html),
        )
        if attempt < attempts:
            time.sleep(random.uniform(4.0, 7.0))

    cfg.stats.discovered = len(ordered)

    LOG.info(
        "Fase 1: %d shortcodes únicos descubiertos (%d anclas, %d blobs JSON)",
        len(ordered), len(from_anchors), len(from_json),
    )
    if len(ordered) < cfg.limit:
        LOG.warning(
            "Se pidieron %d posts pero la página pública sólo expone %d "
            "(no hay paginación en el HTML sin sesión; '?page=2' devuelve "
            "el mismo grid). Se procesarán los %d disponibles.",
            cfg.limit, len(ordered), len(ordered),
        )
    if not ordered:
        LOG.warning(
            "Tras %d intentos no se halló ningún post para #%s "
            "(último title=%r). Causas probables: el hashtag no tiene grid "
            "público, o Instagram está limitando esta IP.",
            attempts, cfg.hashtag, last_title,
        )
    return ordered[: cfg.limit]


# --------------------------------------------------------------------------- #
# Fase 2 - Parseo del embed
# --------------------------------------------------------------------------- #
def parse_embed(html: str, shortcode: str) -> Post:
    """Extrae autor, caption e imagen desde el HTML del embed.

    El DOM actual de Instagram no expone og:*, JSON-LD ni <time>, así que
    todo se resuelve por clases CSS y por las URLs que cuelgan del DOM.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Autor ------------------------------------------------------------ #
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
        # El bloque .Caption incluye el propio nombre de usuario al inicio.
        if author and text.lower().startswith(author.lower()):
            text = text[len(author):].strip()
            if text.lower().startswith("·"):
                text = text.lstrip("· ").strip()
        caption = text or None

    # --- Imagen principal ------------------------------------------------- #
    image_url, is_video = _extract_media(soup, author)

    # --- Autor por respaldo (alt del propio medio) ------------------------ #
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
    """Detecta el "soft 404" de Instagram.

    Importante: para shortcodes inexistentes, privados o eliminados, el embed
    NO devuelve 404 sino HTTP 200 con una página de aviso
    ("The link to this photo or video may be broken..."). Sin esta comprobación
    esos casos se registrarían como posts válidos con todos los campos nulos.
    """
    if any(marker in html for marker in SOFT_404_MARKERS):
        return True
    return post.image_url is None and post.caption is None


def _extract_media(soup: BeautifulSoup, author: str | None) -> tuple[str | None, bool]:
    """Devuelve (url_media, es_video).

    Jerarquía de selección:
      1. `img.EmbeddedMediaImage` - ancla estable para la foto del post.
      2. `<video>` / `<source>`    - reels servidos como MP4.
      3. Mayor URL .jpg del CDN que no sea el avatar del autor.
    """
    # 1) La foto del post.
    node = soup.select_one("img.EmbeddedMediaImage")
    if node and node.get("src"):
        return node["src"], False

    # 2) Un reel en MP4: guardamos el frame de portada vía poster.
    video = soup.find("video")
    if video is not None:
        poster = video.get("poster")
        if poster:
            return poster, True
        source = video.find("source", src=True)
        if source:
            return source["src"], True

    # 3) Respaldo: mayor candidata del CDN, excluyendo avatares y el propio
    #    usuario, cuyo avatar lleva alt="{username}" y el segmento t51.2885-19.
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
        # Las URLs de alta resolución (-15) son más largas; desempata por largo.
        return max(candidates, key=len), False

    return None, False


def shortcode_to_media_id(shortcode: str) -> int:
    """Decodifica un shortcode al media ID numérico de Instagram."""
    value = 0
    for char in shortcode:
        try:
            value = value * 64 + SHORTCODE_ALPHABET.index(char)
        except ValueError as exc:
            raise ValueError(f"shortcode inválido: {shortcode!r}") from exc
    return value


def infer_published_at(shortcode: str) -> Optional[str]:
    """Fecha de publicación *inferida* desde el shortcode (snowflake).

    ADVERTENCIA: es una estimación derivada del ID, no un dato devuelto por
    Instagram. Se marca como inferida en el JSON resultante.
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
    # Descarta fechas en el futuro o anteriores a 2010 (ID corrupto).
    if dt.year < 2010 or dt > datetime.now(timezone.utc):
        return None
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# Fase 3 - Descarga de imágenes y persistencia
# --------------------------------------------------------------------------- #
def download_image(
    client: Client, cfg: ScraperConfig, post: Post
) -> Optional[Path]:
    """Descarga la imagen del post a {output_dir}/{shortcode}.jpg."""
    if not post.image_url:
        LOG.warning("[%s] sin URL de imagen; sólo metadata", post.shortcode)
        return None

    dest = cfg.output_dir / f"{post.shortcode}.jpg"
    if dest.exists() and dest.stat().st_size > 0 and not cfg.overwrite:
        LOG.info("[%s] ya existe, no descargo de nuevo", post.shortcode)
        post.local_image_path = str(dest)
        return dest

    try:
        body = fetch_with_retry(
            client, post.image_url, retries=cfg.retries, label=f"[{post.shortcode}] "
        )
    except FetchError as exc:
        LOG.warning("[%s] falló la descarga de imagen: %s", post.shortcode, exc)
        return None

    if len(body) < 1024:
        LOG.warning("[%s] respuesta de imagen sospechosamente chica", post.shortcode)
        return None

    dest.write_bytes(body)
    LOG.info("[%s] imagen guardada (%d bytes)", post.shortcode, len(body))
    post.local_image_path = str(dest)
    return dest


def write_metadata(posts: list[Post], cfg: ScraperConfig) -> Path:
    """Escribe metadata.json con los posts procesados y las estadísticas."""
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
    """Pipeline completo: descubrir -> parsear -> descargar -> persistir."""
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    client = build_client(cfg)
    posts: list[Post] = []

    try:
        # --- Fase 1 ------------------------------------------------------- #
        shortcodes = discover_shortcodes(client, cfg)
        if not shortcodes:
            return posts

        # --- Fase 2 + 3 --------------------------------------------------- #
        LOG.info("Fase 2: procesando %d posts", len(shortcodes))
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
                    "[%s] 404: post eliminado, privado o inexistente; lo omito",
                    shortcode,
                )
                cfg.stats.skipped_private += 1
                human_delay()
                continue
            except RateLimitedError as exc:
                cfg.stats.rate_limited += 1
                LOG.error("[%s] rate limit sostenido: %s", shortcode, exc)
                break  # seguir insistence empeoraría el bloqueo
            except FetchError as exc:
                LOG.error("[%s] error irrecuperable: %s", shortcode, exc)
                cfg.stats.failed += 1
                human_delay()
                continue

            html = body.decode("utf-8", errors="replace")
            post = parse_embed(html, shortcode)
            post.scraped_at = datetime.now(timezone.utc).isoformat()

            # Soft 404: Instagram devuelve 200 con un aviso en lugar del post.
            if is_unavailable(post, html):
                LOG.warning(
                    "[%s] no disponible (eliminado o privado); Instagram "
                    "responde 200 con aviso de enlace roto; lo omito",
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
                    "[%s] caption vacío; el embed puede haber cambiado de layout",
                    shortcode,
                )

            if download_image(client, cfg, post) is not None:
                cfg.stats.images_saved += 1

            posts.append(post)
            cfg.stats.processed += 1

            # Ritmo humano: también después de la última petición.
            human_delay()

    finally:
        client.close()

    # --- Persistencia ----------------------------------------------------- #
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
            "PoC: extrae posts públicos de Instagram por hashtag vía la ruta "
            "de embed (sin login ni credenciales)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--hashtag", required=True, help="Hashtag objetivo, sin el '#' (ej. sneakers)"
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Máximo de posts a procesar (la página pública expone ~12-14)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Carpeta de salida (por defecto ./downloads/{hashtag})",
    )
    parser.add_argument(
        "--proxy-url", default=None,
        help="Proxy HTTP/HTTPS/SOCKS5, ej. socks5://127.0.0.1:9050",
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="Reintentos por petición ante 429/5xx"
    )
    parser.add_argument("--timeout", type=float, default=25.0, help="Timeout HTTP (s)")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Volver a descargar imágenes ya presentes",
    )
    parser.add_argument(
        "--decode-post-time", action="store_true",
        help="Añade post_published_at INFERIDO desde el shortcode "
             "(el embed ya no expone la fecha real)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log en nivel DEBUG"
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
        print("Error: --hashtag no puede quedar vacío.", file=sys.stderr)
        return 2
    if args.limit < 1:
        print("Error: --limit debe ser >= 1.", file=sys.stderr)
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

    LOG.info("Hashtag #%s | limit=%d | salida=%s", hashtag, cfg.limit, output_dir)
    posts = run(cfg)

    s = cfg.stats
    LOG.info(
        "Terminado: %d procesados (%d imágenes), %d 404 omitidos, "
        "%d rate-limited, %d fallidos",
        s.processed, s.images_saved, s.skipped_private, s.rate_limited, s.failed,
    )

    if not posts:
        print(
            "\nNo se obtuvo ningún post. Causas probables: hashtag inexistente, "
            "login wall de Instagram, o IP bloqueada (probá con --proxy-url).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())