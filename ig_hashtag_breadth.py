#!/usr/bin/env python3
"""
Discovery en anchura sobre los hashtags públicos de Instagram. Sin buscadores,
sin CAPTCHA, sin intervención humana.

POR QUÉ EXISTE
--------------
Las tres estrategias anteriores de este proyecto dependen de un motor de búsqueda
externo, y Google —el único que tiene índice suficiente— exige resolver un CAPTCHA
por sesión. Eso ata el scraper a una persona sentada frente a la pantalla.

Este script elimina el buscador de la ecuación. En vez de *buscar* posts, visita
directamente la página pública del hashtag y lee el grid que Instagram ya renderiza.
Medido: 20 hashtags -> 180 shortcodes únicos, sin CAPTCHA, sin cookies, sin proxy.

EL PRECIO Y POR QUÉ RINDE
-------------------------
Cada hashtag expone ~12-14 posts y no pagina (`?page=2` devuelve el mismo grid).
O sea: el techo es 12 por etiqueta, no por corrida. Pero un hashtag es una unidad
barata y plentiful, y los posts de etiquetas distintas se solapan sólo ~25%:

    20 hashtags  ->  180 únicos   (~12-14 c/u, 25% solapamiento)
    100 hashtags ->  ~850 únicos
    500 hashtags -> ~4000 únicos

Para llegar a miles se crece la lista de etiquetas, no el límite por etiqueta. Es
exactamente lapalanca que el README recomienda para Google ("el techo es breadth, no
profundidad"), pero sin el CAPTCHA encima.

Es la opción más lenta en requests (1 request por etiqueta) y la más rápida en
obstáculos: no hay nada que destrabar.

REUTILIZA LAS PIEZAS YA PROBADAS
--------------------------------
Discovery + `is_unavailable` + detección de shortcode de `ig_embed_scraper`;
enriquecimiento del blob GraphQL, snowflake y `detect_image_format` de
`ig_google_scraper`; y el store incremental de `ig_google_store`. No se reimplementa
ninguna heurística que ya esté medida y documentada: el riesgo de un parser nuevo es
mayor que el de reutilizar uno que funciona.

Uso:
    python ig_hashtag_breadth.py --hashtag running --limit 20
    python ig_hashtag_breadth.py --hashtags-file tags.txt --limit 500 --workers 6
    python ig_hashtag_breadth.py --hashtags-file tags.txt --resume
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional

from ig_embed_scraper import (
    build_client,
    is_plausible_shortcode,
    ScraperConfig,
    FetchError as EmbedFetchError,
    NotFoundError as EmbedNotFoundError,
    RateLimitedError as EmbedRateLimitedError,
    RE_ANCHOR,
    RE_JSON_CODE,
)
from ig_google_scraper import (
    EMBED_URL,
    InstagramPost,
    RateLimitedError,
    NotFoundError,
    FetchError,
    _enrich_from_gql,
    _extract_media,
    detect_image_format,
    is_unavailable,
    parse_retry_after,
    RE_AUTHOR_ALT,
    RE_AUTHOR_LINK,
    RE_COMMENTS_TAIL,
)
from ig_google_store import PostStore

LOG: logging.Logger = logging.getLogger("breadth")

TAG_PAGE: Final[str] = "https://www.instagram.com/explore/tags/{tag}/"

# Posts por etiqueta que el grid público expone. No es un límite imposed por este
# script: es lo que Instagram renderiza, y se mide en runtime (stats.discovered_per_tag).
EXPECTED_PER_TAG: Final[int] = 12


# --------------------------------------------------------------------------- #
# Fase 1 — discovery en anchura
# --------------------------------------------------------------------------- #
def discover_from_tag(client: Any, tag: str, *, attempts: int = 3,
                      timeout: float = 25.0) -> list[str]:
    """Shortcodes públicos de una etiqueta.

    Reintenta ante resultado vacío: Instagram sirve a veces una "página shell"
    genérica (title="Instagram", ~490 KB, cero anclas) incluso para etiquetas
    válidas y muy populares. Se comprobó con #moda: 12 posts en una corrida y 0 en
    la siguiente. Un solo intento convertiría ese falso negativo en error definitivo.
    """
    url = TAG_PAGE.format(tag=tag)
    for attempt in range(1, attempts + 1):
        try:
            status, body, _hdrs = client.get(url)
        except (EmbedFetchError, EmbedRateLimitedError) as exc:
            LOG.warning("  #%s intento %d/%d: %s", tag, attempt, attempts, exc)
            if attempt >= attempts:
                return []
            time.sleep(random.uniform(4.0, 7.0))
            continue

        if status == 429:
            LOG.warning("  #%s: 429 de Instagram", tag)
            return []

        html = body.decode("utf-8", errors="replace")
        from_anchors = [c for c in RE_ANCHOR.findall(html) if is_plausible_shortcode(c)]
        from_json = [c for c in RE_JSON_CODE.findall(html) if is_plausible_shortcode(c)]
        # dict.fromkeys = dedup estable que conserva el orden de aparición.
        ordered = list(dict.fromkeys(from_anchors + from_json))
        if ordered:
            return ordered

        LOG.warning("  #%s: 0 shortcodes en el intento %d/%d (%d bytes); "
                    "Instagram puede estar sirviendo su página shell.",
                    tag, attempt, attempts, len(html))
        if attempt < attempts:
            time.sleep(random.uniform(4.0, 7.0))
    return []


def parse_embed(html: str, shortcode: str) -> InstagramPost:
    """Reusa el parseo del embed público (DOM + blob GraphQL + snowflake).

    Idéntico al de `ig_google_scraper.parse_embed`, salvo que la URL de origen es la
    página del hashtag y no una SERP de Google.
    """
    from bs4 import BeautifulSoup

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
    # El DOM siempre aporta una imagen; el blob puede subir la resolución.
    if not enrich.get("image_url"):
        enrich["image_url"] = image_url

    return InstagramPost(
        shortcode=shortcode, author=author, caption=caption,
        local_image_path=None,
        source_serp_url=f"hashtag://{shortcode}",  # marcador de procedencia
        scraped_at=datetime.now(timezone.utc), is_video=is_video,
        **enrich,
    )


def download_image(client: Any, out_dir: Path, post: InstagramPost,
                   overwrite: bool) -> bool:
    """Descarga la imagen decidiendo la extensión POR LOS BYTES.

    El CDN de Instagram sirve WebP aunque la URL termine en `.jpg` (medido 2 de 24).
    Guardar WebP como `.jpg` rompe PIL, torchvision y ffmpeg.
    """
    if not post.image_url:
        return False
    # OJO: `client.get()` devuelve la tupla (status, body, headers), no los bytes
    # sueltos. Hay que desempaquetar: tratar la tupla como si fueran los bytes
    # hace que len() dé 3 y caiga siempre en "sospechosamente chica".
    try:
        _status, data, _headers = client.get(post.image_url)
    except (EmbedFetchError, EmbedRateLimitedError) as exc:
        LOG.warning("[%s] falló la descarga de imagen: %s", post.shortcode, exc)
        return False
    if len(data) < 1024:
        LOG.warning("[%s] respuesta de imagen sospechosamente chica (%d bytes)",
                    post.shortcode, len(data))
        return False

    ext, _mime = detect_image_format(data)
    if ext == "bin":
        LOG.warning("[%s] formato desconocido; no se guarda", post.shortcode)
        return False

    existing = [p for p in out_dir.glob(f"{post.shortcode}.*") if p.stat().st_size > 0]
    if existing and not overwrite:
        post.local_image_path = str(existing[0])
        return False

    dest = out_dir / f"{post.shortcode}.{ext}"
    dest.write_bytes(data)
    post.local_image_path = str(dest)
    LOG.info("[%s] imagen guardada (%d bytes, %s)", post.shortcode, len(data), ext)
    return True


# --------------------------------------------------------------------------- #
# Rate limiter global
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Espacia las peticiones globalmente, aunque haya N workers.

    El slot se reserva bajo lock pero la espera ocurre FUERA del lock: si no, los
    workers se serializarían de a uno y la concurrencia no serviría.
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


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class BreadthProcessor:
    """Procesa un shortcode: embed -> parseo -> imagen -> checkpoint.

    Seguro desde varios hilos: cada worker tiene su propia sesión HTTP, el store
    serializa escrituras con lock, y el rate limiter es global.
    """

    def __init__(self, store: PostStore, out_dir: Path, args: argparse.Namespace,
                 limiter: RateLimiter) -> None:
        self.store = store
        self.out_dir = out_dir
        self.args = args
        self.limiter = limiter
        self._tls = threading.local()
        self._clients: list[Any] = []
        self._clients_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {"ok": 0, "unavailable": 0, "failed": 0, "images": 0}

    def _new_client(self) -> Any:
        # Se reutiliza `build_client` del embed scraper en vez de construir la
        # sesión a mano: él ya resuelve la impersonación de TLS, el fallback a
        # httpx y la redacción de credenciales del proxy.
        return build_client(ScraperConfig(
            hashtag="probe", limit=0, output_dir=self.out_dir,
            proxy_url=self.args.proxy_url, timeout=self.args.timeout,
        ))

    def client(self) -> Any:
        c = getattr(self._tls, "client", None)
        if c is None:
            c = self._new_client()
            self._tls.client = c
            with self._clients_lock:
                self._clients.append(c)
        return c

    def bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] += n

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for c in clients:
            try:
                c.close()
            except Exception:
                pass

    def process(self, code: str, tag: str) -> Optional[InstagramPost]:
        a = self.args
        client = self.client()

        # --- Fase 2: embed público (200 sin sesión) ---
        self.limiter.acquire()
        html = ""
        try:
            for attempt in range(1, a.retries + 1):
                try:
                    _status, body, hdrs = client.get(EMBED_URL.format(shortcode=code))
                    if hdrs.get("retry-after"):
                        wait = parse_retry_after(hdrs.get("retry-after"))
                        LOG.warning("[%s] 429; esperando %ss", code, wait)
                        time.sleep(min(wait or 5.0, a.retry_max_delay))
                        continue
                    html = body.decode("utf-8", errors="replace")
                    break
                except (EmbedNotFoundError, EmbedRateLimitedError,
                        EmbedFetchError) as exc:
                    LOG.warning("[%s] intento %d/%d: %s", code, attempt, a.retries, exc)
                    if attempt >= a.retries:
                        raise
                    wait = (exc.retry_after if isinstance(exc, EmbedRateLimitedError)
                            and exc.retry_after else None)
                    delay = min(wait or a.retry_delay * (2 ** (attempt - 1))
                                * random.uniform(0.8, 1.2), a.retry_max_delay)
                    time.sleep(delay)
        except EmbedNotFoundError:
            self.store.mark_terminal(code, "unavailable", "404")
            self.bump("unavailable")
            return None
        except (EmbedFetchError, EmbedRateLimitedError) as exc:
            self.store.mark_terminal(code, "failed", f"{type(exc).__name__}: {exc}",
                                     attempts=a.retries)
            self.store.log_error(code, a.retries, type(exc).__name__, str(exc))
            self.bump("failed")
            return None

        post = parse_embed(html, code)
        if is_unavailable(post, html):
            self.store.mark_terminal(code, "unavailable", "soft-404")
            self.bump("unavailable")
            LOG.info("[%s] no disponible (privado/eliminado)", code)
            return None

        # --- Fase 3: imagen + checkpoint ---
        self.limiter.acquire()
        saved = download_image(client, self.out_dir, post, a.overwrite)
        if saved:
            self.bump("images")

        self.store.mark_ok(
            code, query=tag, author=post.author, caption=post.caption,
            image_url=post.image_url, local_image_path=post.local_image_path,
            is_video=post.is_video, source_serp_url=f"hashtag://{tag}",
            scraped_at=post.scraped_at.isoformat(), attempts=a.retries,
            extra=post.model_dump(),
        )
        self.bump("ok")
        return post


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #
def load_tags(args: argparse.Namespace) -> list[str]:
    """Arma la lista de etiquetas desde --hashtag (repetible) y --hashtags-file.

    La lista ES la unidad de escala: 12 posts por etiqueta, así que para miles de
    posts hacen falta cientos de etiquetas, no un límite más alto.
    """
    raw: list[str] = list(args.hashtag or [])
    if args.hashtags_file:
        for line in Path(args.hashtags_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)
    seen: list[str] = []
    for t in raw:
        t = re.sub(r"^#+", "", t.strip()).strip().lower()
        if t and t not in seen:
            seen.append(t)
    if not seen:
        raise SystemExit("Hacen falta etiquetas: --hashtag o --hashtags-file")
    return seen


def run(args: argparse.Namespace) -> int:
    tags = load_tags(args)
    out_dir = Path(args.output_dir or f"output/hashtags_{len(tags)}tags")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Cuántas etiquetas hacen falta para el límite pedido. Cada una rinde ~12, y
    # un cuarto se solapa, así que se pide un poco más de las necesarias.
    if args.limit > 0:
        need = min(len(tags), max(1, int(args.limit / (EXPECTED_PER_TAG * 0.75)) + 1))
    else:
        need = len(tags)
    plan = tags[:need]

    LOG.info("Discovery en anchura: %d etiquetas (objetivo ~%d posts)",
             len(plan), args.limit)

    db_path = Path(args.db or (out_dir / "posts.sqlite3"))
    store = PostStore(db_path)
    store.start_run(f"{len(plan)}tags", {"limit": args.limit, "workers": args.workers})

    # Cliente solo para el discovery (secuencial a propósito: un request por
    # etiqueta, y el objetivo es no exceder el ritmo de Instagram, no la velocidad).
    dclient = build_client(ScraperConfig(
        hashtag="probe", limit=0, output_dir=out_dir,
        proxy_url=args.proxy_url, timeout=args.timeout))

    discovered: dict[str, str] = {}   # shortcode -> tag de origen
    empty_tags: list[str] = []
    try:
        for i, tag in enumerate(plan, start=1):
            if args.limit > 0 and len(discovered) >= args.limit:
                LOG.info("  límite alcanzado en %d/%d etiquetas", i - 1, len(plan))
                break
            codes = discover_from_tag(dclient, tag, attempts=args.tag_retries,
                                      timeout=args.timeout)
            nuevos = [c for c in codes if c not in discovered]
            for c in codes:
                discovered.setdefault(c, tag)
            if not codes:
                empty_tags.append(tag)
            LOG.info("  [%d/%d] #%s -> %d cortos (+%d nuevos) [acum %d]",
                     i, len(plan), tag, len(codes), len(nuevos), len(discovered))
            if args.limit > 0 and len(discovered) >= args.limit:
                break
            time.sleep(args.tag_delay)

        LOG.info("Discovery: %d shortcodes únicos de %d etiquetas (%d sin grid)",
                 len(discovered), len(plan), len(empty_tags))

        # Persistir el discovery antes de procesar: si la corrida se corta, el
        # trabajo de discovery no se pierde.
        new = store.record_discovered(
            ((c, f"hashtag://{t}") for c, t in discovered.items()), f"{len(plan)}tags")
        LOG.info("Discovery persistido: %d nuevos", new)

        # --- Procesamiento (concurrente, con checkpoint por post) ---
        rows = store.pending(include_failed=args.retry_failed)
        ya_hechos = set()
        if args.resume:
            ya_hechos = {r["shortcode"] for r in store.done_posts()}
            rows = [r for r in rows if r["shortcode"] not in ya_hechos]

        if args.limit > 0 and len(rows) > args.limit:
            LOG.info("  recorte a %d para --resume (quedan %d)",
                     args.limit, len(rows) - args.limit)
            rows = rows[:args.limit]
        total = len(rows)

        limiter = RateLimiter(args.rate)
        proc = BreadthProcessor(store, out_dir, args, limiter)
        LOG.info("Fase 2/3: %d posts, %d worker(s), %.2fs entre requests",
                 total, args.workers, args.rate)

        posts: list[InstagramPost] = []
        try:
            if args.workers > 1:
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    futs = {ex.submit(proc.process, r["shortcode"],
                                      discovered.get(r["shortcode"], "?")): r["shortcode"]
                            for r in rows}
                    for n, fut in enumerate(as_completed(futs), start=1):
                        try:
                            p = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            proc.bump("failed")
                            LOG.error("[%s] error inesperado: %s: %s",
                                      futs[fut], type(exc).__name__, exc)
                            continue
                        if p is not None:
                            posts.append(p)
                        if n % 10 == 0 or n == total:
                            LOG.info("  [%d/%d] completados", n, total)
            else:
                for n, r in enumerate(rows, start=1):
                    p = proc.process(r["shortcode"],
                                     discovered.get(r["shortcode"], "?"))
                    if p is not None:
                        posts.append(p)
                    if n % 10 == 0 or n == total:
                        LOG.info("  [%d/%d] completados", n, total)
        finally:
            proc.close()
    finally:
        dclient.close()

    # --- metadata.json desde la base (refleja todo lo acumulado) ---
    rows = store.done_posts()
    import json as _json
    meta = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "method": "hashtag breadth on Instagram public grid (no search engine, no auth)",
        "tags_used": plan,
        "empty_tags": empty_tags,
        "requested_limit": args.limit,
        "stats": {
            "discovered": len(discovered),
            "processed": len(rows),
            **proc._stats,
        },
        "posts": [
            {
                "shortcode": r["shortcode"], "author": r["author"],
                "caption": r["caption"], "image_url": r["image_url"],
                "local_image_path": r["local_image_path"],
                "scraped_at": r["scraped_at"], "is_video": bool(r["is_video"]),
                "likes": r["likes"], "comments": r["comments"],
                "author_followers": r["author_followers"],
                "author_verified": None if r["author_verified"] is None
                else bool(r["author_verified"]),
                "media_id": r["media_id"], "width": r["width"], "height": r["height"],
                "alt_text": r["alt_text"],
                "carousel_count": r["carousel_count"] or 0,
                "post_published_at": r["post_published_at"],
                "post_published_at_inferred": bool(r["post_published_at_inferred"]),
                "rich_metadata": bool(r["rich_metadata"]),
            }
            for r in rows
        ],
    }
    meta_path = out_dir / "metadata.json"
    meta_path.write_text(_json.dumps(meta, indent=2, ensure_ascii=False, default=str),
                         encoding="utf-8")

    LOG.info("Listo: %d posts, %d imágenes -> %s", len(rows), proc._stats["images"],
             meta_path)
    LOG.info("Stats: %s", proc._stats)
    store.close()
    return 0 if rows else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=("Discovery en anchura sobre hashtags públicos de Instagram. "
                     "Sin buscadores, sin CAPTCHA, sin intervención humana."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hashtag", action="append", default=None,
                   help="Etiqueta sin '#'. Repetible. Cada una rinde ~12 posts.")
    p.add_argument("--hashtags-file", default=None,
                   help="Archivo con una etiqueta por línea (usa # para comentarios)")
    p.add_argument("--limit", type=int, default=20,
                   help="Máximo de posts (default 20)")
    p.add_argument("--output-dir", default=None,
                   help="Carpeta de salida (default output/hashtags_{n}tags)")
    p.add_argument("--db", default=None,
                   help="Base SQLite (default <output-dir>/posts.sqlite3)")
    p.add_argument("--workers", type=int, default=4,
                   help="Workers concurrentes en la fase 2/3 (default 4)")
    p.add_argument("--rate", type=float, default=0.4,
                   help="Segundos entre requests a Instagram (default 0.4)")
    p.add_argument("--tag-delay", type=float, default=2.5,
                   help="Pausa entre etiquetas en el discovery (default 2.5)")
    p.add_argument("--tag-retries", type=int, default=3,
                   help="Intentos por etiqueta ante página shell (default 3)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--retry-delay", type=float, default=2.0)
    p.add_argument("--retry-max-delay", type=float, default=60.0)
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--overwrite", action="store_true",
                   help="Re-descargar imágenes ya presentes")
    p.add_argument("--resume", action="store_true",
                   help="Omitir posts ya terminados")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--proxy-url", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.warning("Interrumpido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())