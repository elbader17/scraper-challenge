#!/usr/bin/env python3
"""
Prueba de Concepto: posts de Instagram indexados en GOOGLE, sin login.

    Google SERP  --(dork site:instagram.com/p/ + start=N)-->  URLs indexadas
                                                                    |
                                                             shortcodes
                                                                    |
        https://www.instagram.com/p/{code}/embed/captioned/  -->  datos + foto

Por qué Google y no DuckDuckGo (`ig_serp_scraper.py`):

  * Google SOPORTA paginación real con `start=0,100,200...` y `num=100`.
    DDG no: `&s=10` devuelve HTTP 202. Esta es la ventaja estructural.
  * Google indexa mucho más contenido de Instagram que DDG, así que el techo
    de ~10 shortcodes por consulta se levanta.

Lo que se midió en la práctica desde una IP de datacenter (ver `findings()`):

  1. La vía HTTP (curl_cffi con TLS de Chrome) devuelve la SERP de Google como
     un SHELL que exige JavaScript: ~92 KB, 3 anclas, cero resultados.
     Es un 200, no un error, así que un scraper ingenuo reporta "0 resultados"
     cuando en realidad nunca vio la SERP. `JS_SHELL_MARKERS` lo detecta.
  2. Con Chromium headless (`--render`) el DOM sí se renderiza, pero Google
     responde CAPTCHA ("unusual traffic") al vuelo de IP de datacenter.
     `CAPTCHA_MARKERS` lo detecta.
  3. Conclusión: Google no es accesible desde una IP de datacenter/proxy de
     centro de datos. Hace falta un proxy RESIDENCIAL o MÓVIL con buena
     reputación. `--proxy-url` ya lo soporta; sin proxy, el script falla
     con un mensaje explícito en lugar de fingir que no encontró nada.

Los TLD (`google.es`, `.com.ar`, `.co.uk`, `.ca`, `.mx`) responden idéntico:
el bloqueo es por IP, no por dominio. `--tld` sólo sirve para ajustar región.

Uso:
    # Requiere proxy residencial; sin él Google responde CAPTCHA.
    python ig_google_scraper.py --query "running shoes" --limit 30 \
        --proxy-url http://usuario:clave@host:puerto

    # Render real con Chromium (además del proxy, para cuando el shell JS aparece)
    python ig_google_scraper.py --query sneakers --limit 20 --render

    # Sin Google: shortcodes ya conocidos -> va directo al embed
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

from ig_google_store import PostStore  # noqa: E402

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Falta beautifulsoup4 ->  pip install -r requirements.txt")

try:
    from pydantic import BaseModel, Field, field_validator  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("Falta pydantic ->  pip install -r requirements.txt")


LOG: logging.Logger = logging.getLogger("google_scraper")

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Gecko/20100101 Firefox/128.0"
)
EMBED_URL: Final[str] = "https://www.instagram.com/p/{shortcode}/embed/captioned/"

# Shortcode de Instagram. Google envuelve los enlaces en /url?q=..., y a veces
# van percent-encoded, por eso se aplica `unquote` antes de aplicar el patrón.
RE_SHORTCODE: Final[re.Pattern[str]] = re.compile(
    r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_\-]{10,12})(?![A-Za-z0-9_\-])"
)
RE_AUTHOR_LINK: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/(?:\?|$)"
)
RE_AUTHOR_ALT: Final[re.Pattern[str]] = re.compile(
    r"Instagram post shared by @([A-Za-z0-9._]+)"
)
# Avatar del autor: nunca es la foto del post.
RE_AVATAR_CDN: Final[re.Pattern[str]] = re.compile(r"/t51\.2885-19/")
RE_COMMENTS_TAIL: Final[re.Pattern[str]] = re.compile(
    r"\s*View all ([\d.,]+[KkMm]? )?comments?\.?\s*$", re.IGNORECASE
)

SOFT_404_MARKERS: Final[tuple[str, ...]] = (
    "may be broken, or the post may have been removed",
    "This page isn't available",
    "Sorry, this page isn't available",
)
# Google responde 200 con un aviso de tráfico automatizado, no 429.
# OJO: NO se usa "/sorry/index" como marcador. Esa ruta aparece enlazada en
# una SERP normal, y producía un falso positivo que hacía esperar un captcha
# inexistente (medido: SERP legítima con 11 shortcodes marcada como bloqueada).
# La detección precisa de la ruta se hace aparte con `location.pathname`.
CAPTCHA_MARKERS: Final[tuple[str, ...]] = (
    "our systems have detected unusual traffic",
    "detected unusual traffic",
    "systems have detected",
    "captcha-form",
    "are you a robot",
    "to continue, please type the characters",
)
# Google sirve la SERP como shell que exige JS. Un 200 con estas marcas significa
# "nunca vi la SERP", que es distinto de "no hay resultados".
JS_SHELL_MARKERS: Final[tuple[str, ...]] = (
    "enable javascript on your web browser",
    "before you continue",
    "javascript is disabled",
)
# Una SERP real de Google trae decenas de anclas de resultado incluso cuando no
# encuentra nada. Los shells y el CAPTCHA traen 3-5. Umbral conservador.
MIN_RESULT_ANCHORS: Final[int] = 10

# Rotación de dorks. Cada plantilla devuelve un conjunto distinto, así que es
# el mecanismo complementario cuando la paginación por `start` se agota.
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
# Modelos de datos
# --------------------------------------------------------------------------- #
class InstagramPost(BaseModel):
    shortcode: str
    author: Optional[str] = None
    caption: Optional[str] = None
    image_url: Optional[str] = None
    local_image_path: Optional[str] = None
    # URL de la SERP que reveló este shortcode: trazabilidad del descubrimiento.
    source_serp_url: str
    scraped_at: datetime
    is_video: bool = False
    source_engine: str = "google"

    # --- Metadata enriquecida (blob GraphQL del embed) ---
    # Viene en el `contextJSON`. Es null cuando Instagram sirve el embed
    # "liviano" (isRichEmbed=false), lo cual se midió en 2 de 12 posts.
    rich_metadata: bool = False
    media_id: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    author_id: Optional[int] = None
    author_followers: Optional[int] = None
    author_verified: Optional[bool] = None
    width: Optional[int] = None
    height: Optional[int] = None
    # Alt-text que genera Instagram. Muy útil para filtrado semántico.
    alt_text: Optional[str] = None
    carousel_count: int = 0
    carousel_shortcodes: list[str] = Field(default_factory=list)

    # Fecha INFERIDA: decodificada del media ID, no un campo oficial.
    # El embed ya no expone `taken_at` en ninguna variante sin login.
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
    # Un 429 NO es lo mismo que "sin resultados": son contadores separados a
    # propósito, porque el mensaje final tiene que decir la verdad.
    rate_limited_queries: int = 0
    discovered: int = 0
    processed: int = 0
    images_saved: int = 0
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
# Excepciones
# --------------------------------------------------------------------------- #
class FetchError(RuntimeError):
    """Fallo recuperable en una descarga."""


class NotFoundError(FetchError):
    """HTTP 404 - recurso inexistente."""


class RateLimitedError(FetchError):
    """HTTP 429 - límite de tasa."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """`Retry-After` en segundos, o fecha HTTP (RFC 7231). Devuelve segundos."""
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
    except Exception:  # noqa: BLE001 - header malformado: se ignora
        return None


class CaptchaError(FetchError):
    """Google respondió con verificación humana / bloqueo por reputación de IP."""


class JsShellError(FetchError):
    """Google devolvió el shell que exige JavaScript, no la SERP."""


class IpBlockedError(FetchError):
    """Ninguna consulta pudo completarse por bloqueo de IP."""


# --------------------------------------------------------------------------- #
# Transporte HTTP
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
    """Transporte con impersonación de TLS de Chrome (JA3/JA4 reales)."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0,
                 cookies: Optional[dict[str, str]] = None) -> None:
        from curl_cffi import requests as cc  # import perezoso

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._s = cc.Session(impersonate="chrome", proxies=proxies, timeout=timeout)
        if cookies:
            # NID/SNID/GOOGLE_ABUSE_EXEMPTION: sin estas cookies Google sirve el
            # shell de JS o el CAPTCHA aunque el TLS sea perfecto (medido).
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
            raise RateLimitedError(f"HTTP 429 en {url}", parse_retry_after(retry_after))
        if status == 404:
            raise NotFoundError(f"404 en {url}")
        if status >= 400:
            raise FetchError(f"HTTP {status} en {url}")

    def close(self) -> None:
        self._s.close()


class HttpxClient:
    """Respaldo si curl_cffi no está disponible."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 25.0,
                 cookies: Optional[dict[str, str]] = None) -> None:
        import httpx  # import perezoso

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
        LOG.info("Proxy activo: %s", re.sub(r"//[^@/]*@", "//***:***@", proxy_url))
    try:
        return CurlCffiClient(proxy_url, timeout, cookies)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("curl_cffi no disponible (%s); uso httpx", exc)
        return HttpxClient(proxy_url, timeout, cookies)


# --------------------------------------------------------------------------- #
# Render con Chromium (para cuando Google devuelve el shell JS)
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
    """Renderiza la URL con Chromium headless y devuelve el DOM resultante.

    Chromium tiene su propio stack anti-bot: algunos fingerprints pasan y otros
    disparan el CAPTCHA igual. No es un sustituto del proxy, sólo un complemento.
    """
    binary = find_chromium()
    if not binary:
        raise FetchError(
            "No se encontró Chromium/Chrome para --render. Instalalo o usá la vía HTTP."
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
        raise FetchError(f"Chromium tardó demasiado: {exc}") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    dom = proc.stdout.decode("utf-8", errors="replace")
    if not dom.strip():
        raise FetchError("Chromium no devolvió DOM (posible crash o bloqueo).")
    return dom


# --------------------------------------------------------------------------- #
# Modo interactivo: Chrome real, con ventana, controlado por CDP
# --------------------------------------------------------------------------- #
def find_wayland_socket() -> Optional[tuple[str, str]]:
    """Busca un socket Wayland del sistema. Devuelve (WAYLAND_DISPLAY, XDG_RUNTIME_DIR).

    Hace falta porque en un shell de agente las variables del compositor no
    suelen estar exportadas, y X11 sin `XAUTHORITY` rechaza la conexión con
    "Authorization required" (medido en niri/Xwayland).
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
    """Chrome headful real, controlado por el Chrome DevTools Protocol.

    Es la única vía donde una persona puede resolver el CAPTCHA: Google lo sirve
    como una página normal, así que alcanza con abrir la ventana, hacer el
    challenge a mano y dejar que el script siga leyendo el DOM.

    Se usa un perfil persistente para que las cookies sobrevivan entre corridas:
    una vez resuelto el CAPTCHA, las consultas siguientes no lo vuelven a pedir.
    """

    def __init__(self, profile_dir: Optional[str] = None, port: int = 9222,
                 proxy_url: Optional[str] = None, wait_captcha: int = 180) -> None:
        self._binary = find_chromium()
        if not self._binary:
            raise FetchError("No se encontró Chromium/Chrome para --interactive.")
        self._profile = profile_dir or str(Path.home() / ".ig_google_profile")
        self._port = port
        self._proxy = proxy_url
        self._wait_captcha = wait_captcha
        self._proc: Optional[subprocess.Popen] = None

    # -- ciclo de vida ----------------------------------------------------
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

        # Se prefiere Wayland: la sesión puede ser X11 con XAUTHORITY inaccesible,
        # en cuyo caso Chrome muere con "Authorization required, but no
        # authorization protocol specified". Wayland no necesita ese cookie.
        log = Path(tempfile.gettempdir()) / "ig_google_chrome.log"
        attempts: list[tuple[dict[str, str], list[str]]] = []
        if wayland:
            wl_display, runtime_dir = wayland
            wl_env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
            wl_env["WAYLAND_DISPLAY"] = wl_display
            if runtime_dir:
                wl_env["XDG_RUNTIME_DIR"] = runtime_dir
            attempts.append((wl_env, ["--ozone-platform=wayland"]))
        # Respaldo: X11 tal cual, por si en otro entorno sí funcione.
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
                LOG.info("Chrome listo (%s). Perfil: %s",
                         "wayland" if extra else "x11", self._profile)
                return
            except FetchError as exc:
                proc.terminate()
                LOG.debug("  arranque falló (%s): %s", extra or ["x11"], exc)

        tail = ""
        if log.exists():
            tail = log.read_text(errors="replace").strip().splitlines()[-3:]
        raise FetchError(
            "Chrome no arrancó. Últimas líneas del log:\n" + "\n".join(tail)
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
            except Exception:  # noqa: BLE001 - aún no arrancó
                time.sleep(0.5)
        raise FetchError("Chrome no respondió en el puerto de depuración.")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # -- navegación -------------------------------------------------------
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
        """Abre la búsqueda, espera a que el usuario resuelva el CAPTCHA y
        devuelve las cookies de Google para reutilizarlas por HTTP.

        Es la pieza que hace viable todo lo demás: sin un navegador real que
        resuelva el challenge, Google devuelve shell de JS o CAPTCHA. Con las
        cookies resultantes, curl_cffialone ya recibe la SERP completa.
        """
        try:
            import asyncio  # import perezoso

            import websockets  # type: ignore
        except ImportError:  # pragma: no cover
            raise FetchError(
                "El modo --interactive necesita 'websockets' -> "
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
                    # La ruta /sorry es la señal fiable del challenge; el texto
                    # "unusual traffic" sirve de respaldo.
                    if path.startswith("/sorry") or any(m in low for m in CAPTCHA_MARKERS):
                        if not warned:
                            LOG.warning(
                                "  Google pide verificación. Resolvela en la ventana de "
                                "Chrome que se acaba de abrir (tienes %ds); el script "
                                "sigue solo.", self._wait_captcha,
                            )
                            warned = True
                        continue
                    if len(re.findall(r"<a\s", low)) < MIN_RESULT_ANCHORS:
                        continue  # todavía no renderizó la SERP
                    LOG.info("  SERP cargada (%d shortcodes); exportando cookies…",
                             len(extract_shortcodes(html)))
                    break
                else:
                    raise CaptchaError(
                        f"La verificación no se resolvió en {self._wait_captcha}s. "
                        "Volvé a correr: el perfil conserva lo ya resuelto."
                    )

                res = await cmd("Network.getAllCookies")
                jar = {
                    c["name"]: c["value"] for c in res.get("cookies", [])
                    if "google" in c.get("domain", "") and c.get("value")
                }
                LOG.info("  Cookies obtenidas: %s", ", ".join(sorted(jar)))
                return jar
        finally:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/close/{target['id']}", timeout=5
                ).close()

    def fetch(self, url: str, poll_seconds: float = 3.0) -> str:
        """Abre la URL y devuelve el DOM una vez que hay shortcodes.

        Si Google sirve CAPTCHA, avisa y sigue esperando: el usuario lo resuelve
        en la ventana y el polling sigue hasta encontrar resultados.
        """
        try:
            import asyncio  # import perezoso

            import websockets  # type: ignore
        except ImportError:  # pragma: no cover
            raise FetchError(
                "El modo --interactive necesita 'websockets' -> "
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
                        LOG.info("  SERP lista: %d shortcodes",
                                 len(extract_shortcodes(html)))
                        return html
                    low = html.lower()
                    if any(m in low for m in CAPTCHA_MARKERS):
                        if not warned:
                            LOG.warning(
                                "  Google pide CAPTCHA. Resolvelo en la ventana de "
                                "Chrome que se acaba de abrir; el script sigue solo "
                                "(hasta %ds).", self._wait_captcha,
                            )
                            warned = True
                        continue
                    if any(m in low for m in JS_SHELL_MARKERS):
                        raise JsShellError(
                            "La ventana de Chrome no renderizó la SERP (shell JS)."
                        )
                raise CaptchaError(
                    f"El CAPTCHA no se resolvió en {self._wait_captcha}s. "
                    "Volvé a correr; el perfil ya conserva lo que se resolvió."
                )
        finally:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/close/{target['id']}", timeout=5
                ).close()


# --------------------------------------------------------------------------- #
# Fase 1 - Descubrimiento en Google
# --------------------------------------------------------------------------- #
def build_queries(keyword: str, max_queries: int) -> list[str]:
    """Rotación de dorks: cada plantilla consulta un conjunto distinto."""
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
    """Saca shortcodes del HTML de la SERP.

    Google envuelve los enlaces en `/url?q=...` y a veces percent-encoda el
    destino, así que se busca sobre el texto decodificado y sobre el original.
    """
    found = set(RE_SHORTCODE.findall(html))
    if "%3A%2F%2F" in html or "%2F" in html:
        found |= set(RE_SHORTCODE.findall(unquote(html)))
    return found


def build_serp_url(tld: str, query: str, start: int, num: int) -> str:
    """URL de la SERP de Google. `pws=0` desactiva la personalización por cookie."""
    return (
        f"https://www.google.{tld}/search?q={quote_plus(query)}"
        f"&start={start}&num={num}&hl=en&pws=0&filter=0"
    )


class GoogleSerpClient:
    """Cliente de la SERP de Google."""

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
        """Ejecuta una consulta. Devuelve (shortcodes, url_serp)."""
        serp_url = self._build_url(query, start, num)

        if self._browser is not None:
            # Modo interactivo: el CAPTCHA se resuelve a mano en la ventana.
            html = self._browser.fetch(serp_url)
        elif self._render:
            html = render_dom(serp_url, self._proxy)
        else:
            _, html = self._c.get_text(serp_url, engine="google")

        low = html.lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            raise CaptchaError(
                "Google respondió CAPTCHA/bloqueo por reputación de IP "
                "('unusual traffic'). Hace falta un proxy residencial o móvil."
            )

        codes = extract_shortcodes(html)

        # Un 200 que NO es la SERP no es "no hay resultados": es "nunca vi la
        # SERP". Google alterna entre dos respuestas no-SERP:
        #   a) el shell "please enable javascript on your web browser"
        #   b) el CAPTCHA "unusual traffic"
        # y una SERP real siempre trae decenas de anclas de resultado, incluso
        # cuando no encuentra nada. El conteo de anclas es la señal estructural
        # que no depende del texto exacto con el que Google Localiza el aviso.
        anchors = len(re.findall(r"<a\s", low))
        if not codes and (any(m in low for m in JS_SHELL_MARKERS) or anchors < MIN_RESULT_ANCHORS):
            raise JsShellError(
                f"Google no devolvió la SERP (0 shortcodes, sólo {anchors} anclas; "
                f"html={len(html)}B). Es el shell de JS o un bloqueo, no cero "
                f"resultados. Reintentá con --render o con un proxy residencial."
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
    """Fase 1: rota consultas x páginas hasta reunir `limit` shortcodes."""
    discovered: dict[str, str] = {}
    queries = build_queries(keyword, max_queries)
    LOG.info("Fase 1: %d consultas para %r", len(queries), keyword)

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
                # Un 429 de Google aquí es lo más habitual en volumen: se
                # reintenta con backoff antes de dar la consulta por perdida.
                found, serp_url = with_retries(
                    lambda: serp.search(query, start=start, num=num_per_page),
                    attempts=retries, base_delay=5.0, max_delay=45.0,
                )
            except CaptchaError as exc:
                stats.blocked_queries += 1
                LOG.warning("      BLOQUEADO: %s", exc)
                break  # seguir insistiendo contra un CAPTCHA sólo agrava el bloqueo
            except JsShellError as exc:
                stats.js_shell_queries += 1
                LOG.warning("      shell JS: %s", exc)
                break
            except RateLimitedError as exc:
                # Distinto de "no hay resultados": Google nos está frenando.
                stats.rate_limited_queries += 1
                LOG.warning("      Google limita el ritmo (429) tras %d intentos; "
                            "corré --rate más alto o esperá", retries)
                break
            except FetchError as exc:
                stats.rate_limited_queries += 1
                LOG.warning("      fallo de consulta: %s", exc)
                break

            new = {c: serp_url for c in found if c not in discovered}
            discovered.update(new)
            LOG.info("      +%d nuevos (total %d)", len(new), len(discovered))
            if not found:
                break  # esta consulta no tiene más páginas útiles
            time.sleep(random.uniform(*delay))

    stats.discovered = len(discovered)
    LOG.info("Fase 1: %d shortcodes únicos descubiertos", len(discovered))
    return discovered


# --------------------------------------------------------------------------- #
# Fase 2 - Parseo del embed público de Instagram
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
    # El DOM siempre aporta una imagen; el blob puede subir la resolución.
    if not enrich.get("image_url"):
        enrich["image_url"] = image_url

    return InstagramPost(
        shortcode=shortcode, author=author, caption=caption,
        local_image_path=None, source_serp_url=source_serp_url,
        scraped_at=datetime.now(timezone.utc), is_video=is_video,
        **enrich,
    )


def _enrich_from_gql(html: str, shortcode: str) -> dict[str, Any]:
    """Suma la metadata del blob GraphQL y la fecha inferida.

    Nunca lanza: si el blob no está (embed liviano) devuelve sólo la fecha, que
    depende únicamente del shortcode y por eso está disponible siempre.
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
    # La mejor resolución del CDN suele superar la del <img> del DOM.
    if meta.get("best_url"):
        out["image_url"] = meta["best_url"]
    return out


def _extract_media(soup: BeautifulSoup, author: str | None) -> tuple[Optional[str], bool]:
    """Foto del post: `img.EmbeddedMediaImage`.

    El avatar del autor también es un `<img>` y NO debe confundirse con el post
    (README, hallazgo 2).
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
    """Soft 404: Instagram devuelve 200 con aviso de enlace roto."""
    if any(m in html for m in SOFT_404_MARKERS):
        return True
    return post.image_url is None and post.caption is None


# --------------------------------------------------------------------------- #
# Fase 3 - Descarga y persistencia
# --------------------------------------------------------------------------- #
def detect_image_format(data: bytes) -> tuple[str, str]:
    """Devuelve (extensión, mime) leyendo los bytes, no la URL.

    El CDN de Instagram sirve WebP aunque la URL termine en `.jpg`: medido 2 de
    24 posts en una corrida real. Guardar WebP con extensión .jpg rompe
    cualquier consumidor que confíe en la extensión (PIL, torchvision, ffmpeg).
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
    if not post.image_url:
        return False
    try:
        data = client.get_bytes(post.image_url)
    except FetchError as exc:
        LOG.warning("[%s] falló la descarga de imagen: %s", post.shortcode, exc)
        return False
    if len(data) < 1024:
        LOG.warning("[%s] respuesta de imagen sospechosamente chica", post.shortcode)
        return False

    ext, _mime = detect_image_format(data)
    if ext == "bin":
        LOG.warning("[%s] formato de imagen desconocido; no se guarda", post.shortcode)
        return False

    # La extensión se decide recién ahora, así que el chequeo de "ya existe"
    # tiene que buscar cualquier archivo previo del shortcode.
    existing = [p for p in out_dir.glob(f"{post.shortcode}.*") if p.stat().st_size > 0]
    if existing and not overwrite:
        post.local_image_path = str(existing[0])
        return False

    dest = out_dir / f"{post.shortcode}.{ext}"
    dest.write_bytes(data)
    post.local_image_path = str(dest)
    LOG.info("[%s] imagen guardada (%d bytes, %s)", post.shortcode, len(data), ext)
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
    """Espacia las peticiones globalmente, aunque haya N workers.

    El slot se reserva bajo lock pero la espera ocurre FUERA del lock: si no,
    los workers se serializarían de a uno y la concurrencia no serviría.
    Instagram no publica su límite; este es el freno que se puede bajar con
    `--rate` sin tocar código, y `--rate 0` lo desactiva.
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
    """Ejecuta `fn` reintentando ante fallos transitorios.

    Escala de decisión, toda medida:
      - `NotFoundError`  -> no se reintenta: el post no existe.
      - `RateLimitedError` -> se espera `Retry-After` si el servidor lo manda.
      - otro `FetchError` -> backoff exponencial.

    Es lo que evita que una corrida larga pierda cientos de posts en silencio:
    un 429 puntual se recuperarse en vez de contarse como fallo.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except NotFoundError:
            raise  # terminal por definición
        except (RateLimitedError, FetchError) as exc:
            last = exc
            if on_error is not None:
                on_error(attempt, exc)
            if attempt >= attempts:
                break
            if isinstance(exc, RateLimitedError) and exc.retry_after:
                wait = min(max(exc.retry_after, base_delay), max_delay)
                LOG.warning("      429; esperando %.0fs (Retry-After)", wait)
            else:
                wait = min(base_delay * (2 ** (attempt - 1)), max_delay)
                # jitter: evita que N workers reintenten en lockstep
                wait *= random.uniform(0.8, 1.2)
                LOG.warning("      %s; reintento %d/%d en %.1fs",
                            type(exc).__name__, attempt + 1, attempts, wait)
            time.sleep(wait)
    assert last is not None
    raise last


def write_metadata_from_store(store: "PostStore", stats: ScrapeStats, query: str,
                              args: argparse.Namespace, out_dir: Path) -> Path:
    """Escribe metadata.json a partir de TODOS los posts terminados en la base.

    No de los de esta corrida: así el archivo es la foto completa del
    conocimiento acumulado, aunque la corrida se haya cortado a la mitad.
    """
    path = out_dir / "metadata.json"
    rows = store.done_posts()
    posts = [
        InstagramPost(
            shortcode=r["shortcode"], author=r["author"], caption=r["caption"],
            image_url=r["image_url"], local_image_path=r["local_image_path"],
            source_serp_url=r["source_serp_url"] or "",
            scraped_at=_parse_dt(r["scraped_at"]),
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


def _parse_dt(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Metadata enriquecida: el blob GraphQL que el embed trae y nadie parsea
# --------------------------------------------------------------------------- #
# El embed renderizado incluye un `contextJSON` con `gql_data.shortcode_media`,
# que es una respuesta GraphQL real: likes, comentarios, seguidores, verificado,
# dimensiones, alt-text y las diapositivas del carrusel. Todo eso se estaba
# descartando porque el parseomiraba sólo el DOM.
#
# Cobertura medida: 10/12 posts. Cuando falta, Instagram sirve
# `contextJSON: null` con `isRichEmbed: false`. NO es transitorio (probado con
# 4 reintentos) y ninguna variante de URL lo recupera, así que se guarda como
# null y no se reintenta.
RE_CONTEXT_JSON: Final[re.Pattern[str]] = re.compile(
    r'"contextJSON"\s*:\s*'
)
SHORTCODE_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
# Epoch del esquema de IDs de Instagram (el high-bits del media ID es un
# timestamp). Validado: el id derivado del shortcode coincide exacto con el `id`
# del embed, y las fechas decodificadas coinciden con captions que declaran su
# propio mes/año ("dropping in August 2026" -> 2026-08-01).
IG_ID_EPOCH_MS: Final[int] = 1314220021721


def shortcode_to_media_id(shortcode: str) -> int:
    """Convierte el shortcode base64url de Instagram en su media ID numérico."""
    n = 0
    for ch in shortcode:
        idx = SHORTCODE_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"shortcode inválido: {shortcode!r}")
        n = n * 64 + idx
    return n


def shortcode_to_datetime(shortcode: str) -> Optional[datetime]:
    """Fecha de publicación INFERIDA, decodificada del media ID.

    No es un campo oficial de Instagram: es la marca de creación que su esquema
    de IDs ya tiene codificada. Es exacta, pero se marca como inferida.
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
    """Extrae el `contextJSON` del embed tolerando los escapes de Instagram.

    No se puede hacer con un regex ingenuo: el valor es un JSON anidado y
    escapado dentro del HTML, y un patrón simple falla en casos reales
    (medido: 1 de 2). Se escanea el string respetando los escapes.
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


def _first_int(node: Any, *path: str) -> Optional[int]:
    cur: Any = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, int) else None


def parse_gql_media(data: dict[str, Any]) -> dict[str, Any]:
    """Normaliza `gql_data.shortcode_media` a campos planos y utilizables."""
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
        "media_id": sm.get("id") if isinstance(sm.get("id"), int) else None,
        "likes": _first_int(sm, "edge_liked_by", "count"),
        "comments": _first_int(sm, "edge_media_to_comment", "count"),
        "author_id": owner.get("id") if isinstance(owner.get("id"), int) else None,
        "author_followers": _first_int(owner, "edge_followed_by", "count"),
        "author_verified": owner.get("is_verified")
        if isinstance(owner.get("is_verified"), bool) else None,
        "width": dims.get("width") if isinstance(dims.get("width"), int) else None,
        "height": dims.get("height") if isinstance(dims.get("height"), int) else None,
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
      - cada worker tiene su propia sesión HTTP (session no es thread-safe),
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
            LOG.warning("[%s] falló la imagen: %s", code, exc)

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
    """Arma la cola de keywords desde --query (repetible) y --queries-file.

    El techo de posts viene del breadth, no de la profundidad: una keyword da
    ~60-120 shortcodes y rotar dorks sobre la misma rinde poco (78% de
    solapamiento medido). Por eso la cola es el mecanismo para llegar a miles.
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
    # Google no entrega la SERP a clientes HTTP sin una sesión previa. Se abre
    # un Chrome de verdad, el usuario resuelve la verificación una vez, y se
    # exportan sus cookies. A partir de ahí el scraping corre por HTTP, que es
    # mucho más rápido y no vuelve a pedir la verificación.
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
    store = PostStore(db_path)
    store.start_run(keyword, {"limit": args.limit, "interactive": args.interactive,
                              "proxy": bool(args.proxy_url)})
    LOG.info("Base incremental: %s", db_path)

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
            # Freno propio para Google: es el host que más limita, y el ritmo
            # de la fase 2/3 (Instagram) no lo protege.
            discovery_limiter = RateLimiter(
                args.discovery_rate if args.discovery_rate > 0 else args.rate)
            LOG.info("Fase 1: %d keyword(s) en cola, %.1fs entre consultas",
                     len(keywords), discovery_limiter._min)
            for n_kw, kw in enumerate(keywords, start=1):
                if args.limit > 0 and len(discovered) >= args.limit:
                    LOG.info("  discovery completo: límite alcanzado")
                    break
                restante = (args.limit - len(discovered)) if args.limit > 0 else 0
                LOG.info("  [%d/%d] keyword: %r", n_kw, len(keywords), kw)
                try:
                    d = discover(serp, kw, restante or 10_000, args.max_queries,
                                 args.max_pages, args.num_per_page, delay, stats,
                                 limiter=discovery_limiter,
                                 retries=max(1, args.discovery_retries))
                except FetchError as exc:
                    LOG.warning("  discovery de %r falló: %s", kw, exc)
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
                        "Hace falta un proxy residencial o móvil (--proxy-url)."
                    )
                if bloqueado:
                    LOG.warning(
                        "Discovery bloqueado por Google, pero la base tiene %d posts "
                        "pendientes: continúo con --resume sobre lo ya descubierto.",
                        en_base)
                elif stats.rate_limited_queries:
                    LOG.warning(
                        "Google limitó el ritmo en %d consultas (429): no es que no "
                        "haya resultados. Subí --rate o esperá antes de reintentar.",
                        stats.rate_limited_queries)
                else:
                    LOG.warning("Google no devolvió resultados para ninguna keyword.")

        # ---- Fase 2 + 3 ---------------------------------------------------
        # El discovery se persiste primero: si la corrida se corta, locostoso
        # (las consultas a Google) no se pierde y --resume lo reutiliza.
        nuevos = store.record_discovered(discovered.items(), keyword)
        LOG.info("Discovery persistido: %d nuevos (total %d)",
                 nuevos, len(discovered))

        pendientes = store.pending(include_failed=args.retry_failed)
        # En --resume lo que ya está en la base se omite; sin --resume se
        # procesa lo pendiente de todas las corridas.
        if args.resume:
            ya_hechos = {r["shortcode"] for r in store.done_posts()}
            pendientes = [r for r in pendientes if r["shortcode"] not in ya_hechos]

        total = len(pendientes)
        LOG.info("Fase 2/3: %d posts a procesar (límite %d)%s", total, args.limit,
                 " [resume]" if args.resume else "")

        # El límite se aplica acá, no dentro del bucle: con workers es más
        # simple y barato recortar la lista que llevar un contador compartido.
        if args.limit > 0 and len(pendientes) > args.limit:
            LOG.info("  límite %d: %d quedan para --resume",
                     args.limit, len(pendientes) - args.limit)
            pendientes = pendientes[:args.limit]
            total = len(pendientes)

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
                        # Nada se traga en silencio: una excepción aquí sería
                        # pérdida de datos invisible (ya pasó una vez).
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

    LOG.info("Listo: %d posts en esta corrida, %d imágenes -> %s",
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
                   help="Archivo con una keyword por línea (usa # para comentarios)")
    p.add_argument("--limit", type=int, default=10,
                   help="Máximo de posts a devolver (default 10)")
    p.add_argument("--workers", type=int, default=1,
                   help="Workers concurrentes en la fase 2/3 (default 1)")
    p.add_argument("--rate", type=float, default=0.0,
                   help="Segundos mínimos entre requests a Instagram. 0 = sin freno")
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
                   help="Chrome real con ventana: resolvés el CAPTCHA a mano y sigue")
    p.add_argument("--profile", default=None,
                   help="Perfil persistente de Chrome (default ~/.ig_google_profile)")
    p.add_argument("--cdp-port", type=int, default=9222)
    p.add_argument("--wait-captcha", type=int, default=180,
                   help="Segundos de espera para resolver el CAPTCHA (default 180)")
    p.add_argument("--max-queries", type=int, default=6,
                   help="Máximo de dorks a rotar (default 6)")
    p.add_argument("--max-pages", type=int, default=2,
                   help="Páginas por dork vía start= (default 2)")
    p.add_argument("--num-per-page", type=int, default=100,
                   help="Resultados por página (default 100)")
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
                   help="Base SQLite (default <output-dir>/posts.sqlite3)")
    p.add_argument("--resume", action="store_true",
                   help="Omitir los posts ya terminados y seguir con los pendientes")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-descargar imágenes ya presentes")
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