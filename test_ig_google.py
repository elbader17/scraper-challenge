#!/usr/bin/env python3
"""
Tests del store incremental y de la política de reintentos.

Sin red: todo se verifica con dobles de prueba. El objetivo es que estas reglas
no se rompan en silencio, porque en una corrida de miles de posts un error acá
se traduce en datos perdidos que nadie nota hasta el final.

    python test_ig_google.py
"""

from __future__ import annotations

import sys
import tempfile
import json
import time
from pathlib import Path

import ig_google_store as S
from ig_google_store import PostStore
import ig_google_scraper as G

FALLOS: list[str] = []


def check(nombre: str, cond: bool, detalle: str = "") -> None:
    if cond:
        print(f"  PASS  {nombre}")
    else:
        print(f"  FAIL  {nombre} {detalle}")
        FALLOS.append(nombre)


def tmpdb() -> Path:
    return Path(tempfile.mkdtemp(prefix="igtest-")) / "t.sqlite3"


# --------------------------------------------------------------------------- #
print("\n== store: persistencia y estados ==")
# --------------------------------------------------------------------------- #
st = PostStore(tmpdb())
check("descubrir es idempotente",
      st.record_discovered([("AAA1111111", "u1"), ("BBB2222222", "u2")], "q") == 2
      and st.record_discovered([("AAA1111111", "u1")], "q") == 0,
      "re-registrar no debe contar como nuevo")

check("pending incluye lo descubierto", len(st.pending()) == 2)
st.mark_ok("AAA1111111", query="q", author="a", caption="c",
           image_url="i", local_image_path="p", is_video=False,
           source_serp_url="u", scraped_at="2026-01-01T00:00:00+00:00",
           attempts=1)
st.mark_terminal("BBB2222222", "unavailable", "soft-404")
check("pending excluye ok/unavailable", len(st.pending()) == 0,
      f"quedan {[r['shortcode'] for r in st.pending()]}")
check("pending vacío => nada que procesar en la segunda corrida",
      all(r["shortcode"] not in ("AAA1111111", "BBB2222222") for r in st.pending()))

st2 = PostStore(tmpdb())
st2.record_discovered([("CCC3333333", "u")], "q")
st2.mark_terminal("CCC3333333", "failed", "boom", attempts=3)
check("failed NO aparece en pending por defecto", len(st2.pending()) == 0)
check("failed SÍ aparece con include_failed", len(st2.pending(include_failed=True)) == 1,
      "si no, un post fallido queda enganchado para siempre")

st3 = PostStore(tmpdb())
st3.record_discovered([("DDD4444444", "u")], "q")
st3.mark_ok("DDD4444444", query="q", author="@x", caption="hola",
            image_url="http://i/x.jpg", local_image_path="l.jpg", is_video=True,
            source_serp_url="u", scraped_at="2026-01-01T00:00:00+00:00", attempts=1)
rows = st3.done_posts()
check("done_posts conserva los datos", rows and rows[0]["author"] == "@x"
      and rows[0]["is_video"] == 1 and rows[0]["caption"] == "hola")

# --------------------------------------------------------------------------- #
print("\n== store: sobrevive a un corte abrupto (autocommit) ==")
# --------------------------------------------------------------------------- #
p_cut = tmpdb()
s_ab = PostStore(p_cut)
s_ab.record_discovered([("EEE5555555", "u")], "q")
s_ab.mark_ok("EEE5555555", query="q", author="z", caption="c", image_url="i",
             local_image_path="l", is_video=False, source_serp_url="u",
             scraped_at="2026-01-01T00:00:00+00:00", attempts=1)
del s_ab  # sin close(): simula SIGKILL a mitad de corrida
s_reopened = PostStore(p_cut)
check("el post sobrevive sin close()", len(s_reopened.done_posts()) == 1,
      "si falla, el checkpoint no sirve para nada")

# --------------------------------------------------------------------------- #
print("\n== store: registro de errores ==")
# --------------------------------------------------------------------------- #
s_e = PostStore(tmpdb())
s_e.record_discovered([("FFF6666666", "u")], "q")
for i in (1, 2, 3):
    s_e.log_error("FFF6666666", i, "RateLimitedError", "HTTP 429")
errs = s_e.errors_for("FFF6666666")
check("queda un error por intento fallido", len(errs) == 3, f"hay {len(errs)}")

# --------------------------------------------------------------------------- #
print("\n== retries: política de reintentos ==")
# --------------------------------------------------------------------------- #


class Boom:
    def __init__(self, exc, n_before):
        self.exc, self.n, self.calls = exc, n_before, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.n:
            raise self.exc
        return "ok"


t0 = time.time()
r = G.with_retries(Boom(G.FetchError("500"), 2), attempts=3,
                   base_delay=0.05, max_delay=0.2)
check("reintenta y termina bien", r == "ok")
check("no duerme entre el último intento", time.time() - t0 < 0.6,
      f"tardó {time.time()-t0:.2f}s")

f = Boom(G.RateLimitedError("429", retry_after=0.1), 1)
r = G.with_retries(f, attempts=3, base_delay=0.05, max_delay=0.5)
check("respeta Retry-After y reintenta", r == "ok" and f.calls == 2)

f2 = Boom(G.RateLimitedError("429", retry_after=30.0), 1)
t0 = time.time()
try:
    G.with_retries(f2, attempts=2, base_delay=0.05, max_delay=0.2)
except G.RateLimitedError:
    pass
check("Retry-After se capped por max_delay", time.time() - t0 < 1.0,
      "esperar 30s de golpe rompería la corrida; debe estar acotado")

nf = Boom(G.NotFoundError("404"), 5)
try:
    G.with_retries(nf, attempts=5, base_delay=0.05)
except G.NotFoundError:
    pass
check("404 no se reintenta (es terminal)", nf.calls == 1, f"llamó {nf.calls} veces")

boom = Boom(G.FetchError("500"), 99)
try:
    G.with_retries(boom, attempts=3, base_delay=0.01, max_delay=0.05)
except G.FetchError:
    pass
check("agotado el límite, propaga el error", boom.calls == 3, f"llamó {boom.calls}")

errs_logged = []
G.with_retries(Boom(G.FetchError("500"), 2), attempts=3, base_delay=0.01,
               max_delay=0.02, on_error=lambda a, e: errs_logged.append(a))
check("on_error notifica cada fallo", errs_logged == [1, 2], f"recibió {errs_logged}")

# --------------------------------------------------------------------------- #
print("\n== Retry-After: parsing ==")
# --------------------------------------------------------------------------- #
check("header en segundos", G.parse_retry_after("120") == 120.0)
check("header ausente -> None", G.parse_retry_after(None) is None)
check("header basura -> None", G.parse_retry_after("pronto") is None)
futuro = G.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
check("header en fecha HTTP", futuro is not None and futuro > 100000,
      f"devolvió {futuro}")
check("header en pasado -> 0", G.parse_retry_after("Wed, 21 Oct 2020 07:28:00 GMT") == 0.0)

# --------------------------------------------------------------------------- #
print("\n== _check: clasificación de errores HTTP ==")
# --------------------------------------------------------------------------- #
for status, exc in ((429, G.RateLimitedError), (404, G.NotFoundError),
                    (500, G.FetchError), (403, G.FetchError)):
    try:
        G.CurlCffiClient._check(status, "u", "7")
        check(f"{status} lanza excepción", False, "no lanzó")
    except Exception as e:
        check(f"{status} -> {exc.__name__}", isinstance(e, exc),
              f"lanzó {type(e).__name__}")
    except G.FetchError:
        pass

try:
    G.CurlCffiClient._check(429, "u", "7")
except G.RateLimitedError as e:
    check("429 propaga Retry-After", e.retry_after == 7.0)

# --------------------------------------------------------------------------- #
print("\n== metadata desde la base ==")
# --------------------------------------------------------------------------- #
import argparse


class _A:
    limit = 10
    render = False
    proxy_url = None


out = Path(tempfile.mkdtemp(prefix="igmeta-"))
s_m = PostStore(out / "db.sqlite3")
s_m.record_discovered([("GGG7777777", "serp-url")], "q")
s_m.mark_ok("GGG7777777", query="q", author="@ana", caption="hola",
            image_url="http://img/x.jpg", local_image_path=str(out / "x.jpg"),
            is_video=False, source_serp_url="serp-url",
            scraped_at="2026-01-01T00:00:00+00:00", attempts=1)
s_m.mark_terminal("HHH8888888", "unavailable", "soft-404")
mp = G.write_metadata_from_store(s_m, G.ScrapeStats(), "q", _A(), out)
import json as _json

data = _json.loads(mp.read_text())
check("metadata incluye el post ok", len(data["posts"]) == 1
      and data["posts"][0]["author"] == "@ana")
check("metadata excluye unavailable", all(p["shortcode"] != "HHH8888888"
                                          for p in data["posts"]))

# --------------------------------------------------------------------------- #
print("\n== formato de imagen por bytes, no por URL ==")
# --------------------------------------------------------------------------- #
jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 2000
webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBPVP8X" + b"\x00" * 2000
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
gif = b"GIF89a" + b"\x00" * 2000
check("detecta JPEG", G.detect_image_format(jpg) == ("jpg", "image/jpeg"))
check("detecta WebP (Instagram lo sirve con URL .jpg)",
      G.detect_image_format(webp) == ("webp", "image/webp"))
check("detecta PNG", G.detect_image_format(png) == ("png", "image/png"))
check("detecta GIF", G.detect_image_format(gif) == ("gif", "image/gif"))
check("formato desconocido -> bin, no se guarda",
      G.detect_image_format(b"<?xml version='1.0'?>" + b"\x00" * 2000)[0] == "bin")


class _ImgClient:
    def __init__(self, data):
        self.data = data

    def get_bytes(self, url):
        return self.data

    def get_text(self, *a, **k):
        return 200, ""

    def close(self):
        pass


out_i = Path(tempfile.mkdtemp(prefix="igimg-"))
from ig_google_scraper import InstagramPost
from datetime import datetime as _dt, timezone as _tz

p_webp = InstagramPost(shortcode="ABC1234567", source_serp_url="u",
                       scraped_at=_dt.now(_tz.utc), image_url="http://cdn/x.jpg")
check("guarda WebP con extensión .webp",
      G.download_image(_ImgClient(webp), out_i, p_webp, False)
      and (out_i / "ABC1234567.webp").exists(),
      f"archivos: {[f.name for f in out_i.iterdir()]}")
check("no re-descarga si ya existe (idempotente)",
      not G.download_image(_ImgClient(webp), out_i, p_webp, False))
check("local_image_path apunta a la extensión correcta",
      p_webp.local_image_path.endswith("ABC1234567.webp"),
      f"apunta a {p_webp.local_image_path}")
check("--overwrite re-descarga",
      G.download_image(_ImgClient(webp), out_i, p_webp, True))

# --------------------------------------------------------------------------- #
print("\n== rate limiter ==")
# --------------------------------------------------------------------------- #
import threading
from concurrent.futures import ThreadPoolExecutor as _TPE

rl = G.RateLimiter(0.05)
hits: list[float] = []


def worker() -> None:
    rl.acquire()
    hits.append(time.monotonic())


t0 = time.monotonic()
with _TPE(max_workers=8) as ex:
    list(ex.map(lambda _: worker(), range(8)))
dur = time.monotonic() - t0
check("N slots quedan espaciados >= min_interval",
      all(b - a >= 0.045 for a, b in zip(sorted(hits), sorted(hits)[1:])),
      f"separaciones={[round(b-a,3) for a,b in zip(sorted(hits), sorted(hits)[1:])]}")
check("8 slots a 0.05s tardan ~0.35s (no 8x0.05 en paralelo)", 0.30 < dur < 1.2,
      f"tardó {dur:.2f}s")

rl0 = G.RateLimiter(0)
t0 = time.monotonic()
with _TPE(max_workers=8) as ex:
    list(ex.map(lambda _: rl0.acquire(), range(64)))
check("--rate 0 no frena", time.monotonic() - t0 < 0.1)

# --------------------------------------------------------------------------- #
print("\n== store thread-safe ==")
# --------------------------------------------------------------------------- #
s_t = PostStore(tmpdb())
codes = [f"TS{i:08d}" for i in range(200)]
s_t.record_discovered([(c, "u") for c in codes], "q")


def hammer() -> None:
    for c in codes:
        s_t.mark_ok(c, query="q", author="a", caption="c", image_url="i",
                    local_image_path="l", is_video=False, source_serp_url="u",
                    scraped_at="2026-01-01T00:00:00+00:00", attempts=1)
        s_t.log_error(c, 1, "Fake", "boom")


with _TPE(max_workers=8) as ex:
    list(ex.map(lambda _: hammer(), range(8)))
check("200 posts escritos por 8 hilos sin corromper",
      s_t.counts().get("ok") == 200, f"quedaron {s_t.counts()}")
check("1600 errores registrados sin corromper",
      len(s_t._ex("SELECT 1 FROM errors").fetchall()) == 1600)
s_t.close()
s_t2 = PostStore(tmpdb())
s_t2.close()
check("la base sigue legible tras el test concurrente", True)

# --------------------------------------------------------------------------- #
print("\n== cola de keywords ==")
# --------------------------------------------------------------------------- #
qf = Path(tempfile.mkdtemp(prefix="igq-")) / "q.txt"
qf.write_text("zapatillas\n# comentario\n  running shoes  \n\nzapatillas\n")
qa = argparse.Namespace(query=["zapatillas", "trail running"], queries_file=str(qf))
kws = G.resolve_keywords(qa)
check("dedup y sanea la cola",
      kws == ["zapatillas", "trail running", "running shoes"], f"obtuvo {kws}")
check("descarta comentarios y líneas vacías", "# comentario" not in kws)
check("quita comillas", G.resolve_keywords(
    argparse.Namespace(query=['"moda"'], queries_file=None)) == ["moda"])
try:
    G.resolve_keywords(argparse.Namespace(query=None, queries_file=None))
    check("sin keywords debe fallar", False, "no falló")
except SystemExit:
    check("sin keywords debe fallar", True)

# --------------------------------------------------------------------------- #
print("\n== discovery: clasificación de fallos ==")
# --------------------------------------------------------------------------- #


class _Serp:
    """SERP falsa que falla con la excepción que se le pida."""

    def __init__(self, exc, n_ok=0):
        self.exc, self.n_ok, self.calls = exc, n_ok, 0

    def search(self, query, start=0, num=100):
        self.calls += 1
        if self.calls <= self.n_ok:
            return {"ZZZ0000000"}, "u"
        if self.exc is None:
            return set(), "u"  # consulta válida que no devuelve nada
        raise self.exc


st_d = G.ScrapeStats()
G.discover(_Serp(G.RateLimitedError("429", retry_after=0.01)), "kw", 10, 3, 1, 100,
           (0, 0), st_d, retries=2)
check("un 429 cuenta como rate_limited, no como 'sin resultados'",
      st_d.rate_limited_queries > 0 and st_d.blocked_queries == 0,
      f"stats={st_d.model_dump()}")
check("un 429 no se confunde con 0 resultados en el mensaje",
      st_d.discovered == 0 and st_d.rate_limited_queries >= 1)

st_c = G.ScrapeStats()
G.discover(_Serp(G.CaptchaError("captcha")), "kw", 10, 3, 1, 100, (0, 0), st_c)
check("captcha cuenta como blocked", st_c.blocked_queries > 0)

st_j = G.ScrapeStats()
G.discover(_Serp(G.JsShellError("shell")), "kw", 10, 3, 1, 100, (0, 0), st_j)
check("shell JS se cuenta aparte", st_j.js_shell_queries > 0)

st_r = G.ScrapeStats()
s_ok = _Serp(None, n_ok=1)
r = G.discover(s_ok, "kw", 10, 3, 1, 100, (0, 0), st_r)
check("una consulta buena avanza el contador",
      st_r.discovered == 1 and "ZZZ0000000" in r, f"stats={st_r.model_dump()}")

st_back = G.ScrapeStats()
s_backoff = _Serp(G.RateLimitedError("429", retry_after=0.01), n_ok=0)
t0 = time.time()
G.discover(s_backoff, "kw", 10, 1, 1, 100, (0, 0), st_back, retries=3)
check("discovery reintenta el 429 con backoff (no lo pierde de una)",
      s_backoff.calls >= 2, f"llamó {s_backoff.calls} veces")

# --------------------------------------------------------------------------- #
print("\n== metadata enriquecida: blob GraphQL del embed ==")
# --------------------------------------------------------------------------- #
# Fixture tomada de una respuesta real del embed (post DN292ZY3K4y): el
# contextJSON viene como string JSON escapado dentro del HTML.
_REAL_CTX = {
    "context": {"type": "GraphSidecar", "shortcode": "DN292ZY3K4y",
                "copyright_blocked": False},
    "gql_data": {"shortcode_media": {
        "__typename": "GraphSidecar", "id": 3708423362135764530,
        "shortcode": "DN292ZY3K4y", "is_video": False,
        "display_url": "https://cdn/x/720.jpg",
        "display_resources": [
            {"src": "https://cdn/x/720.jpg", "config_width": 720,
             "config_height": 960},
            {"src": "https://cdn/x/1440.jpg", "config_width": 1440,
             "config_height": 1920},
            {"src": "https://cdn/x/1080.jpg", "config_width": 1080,
             "config_height": 1440},
        ],
        "dimensions": {"height": 1920, "width": 1440},
        "accessibility_caption": None,
        "edge_liked_by": {"count": 394},
        "edge_media_to_comment": {"count": 74},
        "edge_media_to_caption": {"edges": [{"node": {"text": "hola"}}]},
        "edge_sidecar_to_children": {"edges": [
            {"node": {"shortcode": "DN292RBXED5"}},
            {"node": {"shortcode": "DN291KHXOgP"}},
            {"node": {"shortcode": "DN292REXMRr"}},
        ]},
        "owner": {"id": 9461917, "username": "amberup4arun",
                  "is_verified": False, "edge_followed_by": {"count": 8684}},
    }},
}
_ESCAPED = json.dumps(json.dumps(_REAL_CTX, ensure_ascii=False))
HTML_RICH = f'<html><body><script>["PolarisEmbedSimple","init",[],[' \
           f'[{{"contextJSON":{_ESCAPED}}}]]</script></body></html>'
HTML_LIGHT = '<html><body>["PolarisEmbedSimple","init",[],' \
             '[{"contextJSON":null}]]</body></html>'

d = G.extract_context_json(HTML_RICH)
check("extrae el contextJSON escapado", d is not None
      and "shortcode_media" in d["gql_data"])
m = G.parse_gql_media(d)
check("likes", m["likes"] == 394, f"obtuvo {m['likes']}")
check("comments", m["comments"] == 74, f"obtuvo {m['comments']}")
check("followers del owner", m["author_followers"] == 8684)
check("owner id", m["author_id"] == 9461917, f"obtuvo {m['author_id']}")
check("is_verified False no se pierde", m["author_verified"] is False,
      f"obtuvo {m['author_verified']!r}")
check("dimensiones", m["width"] == 1440 and m["height"] == 1920)
check("alt_text null se preserva como None", m["alt_text"] is None)
check("carousel: 3 hijos y sus shortcodes",
      m["carousel_count"] == 3
      and m["carousel_shortcodes"] == ["DN292RBXED5", "DN291KHXOgP", "DN292REXMRr"],
      f"obtuvo {m['carousel_count']}")
check("elige la resolución más grande (1440, no 720)",
      m["best_url"].endswith("1440.jpg"), f"eligió {m['best_url']}")

check("embed liviano -> sin metadata, no rompe", G.extract_context_json(HTML_LIGHT) is None)
check("HTML sin contextJSON -> None", G.extract_context_json("<html>nada</html>") is None)
check("contextJSON corrupto -> None, no lanza",
      G.extract_context_json('<script>"contextJSON":"{\\"roto":' ) is None)

enr = G._enrich_from_gql(HTML_RICH, "DN292ZY3K4y")
check("enriquecimiento con blob: rich_metadata=True", enr["rich_metadata"] is True)
check("enriquecimiento sube la URL a la mejor resolución",
      enr["image_url"].endswith("1440.jpg"))
enr_null = G._enrich_from_gql(HTML_LIGHT, "DWOQtEyDc1Q")
check("embed liviano: rich_metadata ausente, pero la fecha sigue",
      not enr_null.get("rich_metadata")
      and enr_null["post_published_at"] is not None)

# --------------------------------------------------------------------------- #
print("\n== fecha inferida desde el shortcode ==")
# --------------------------------------------------------------------------- #
from datetime import datetime as _dt2, timezone as _tz2

check("shortcode -> media id",
      G.shortcode_to_media_id("DN292ZY3K4y") == 3708423362135764530,
      f"obtuvo {G.shortcode_to_media_id('DN292ZY3K4y')}")
dt = G.shortcode_to_datetime("DN292ZY3K4y")
check("fecha coincide con la del id del embed",
      dt.isoformat().startswith("2025-08-27T12:42:16"), f"obtuvo {dt}")
# Validación cruzada contra captions que declaran su propio mes/año:
# "Running shoes dropping in August 2026" y "... in March 2026".
check("'dropping in August 2026' -> 2026-08",
      G.shortcode_to_datetime("DbggwHEER81").strftime("%Y-%m") == "2026-08",
      G.shortcode_to_datetime("DbggwHEER81").isoformat())
check("'dropping in March 2026' -> 2026-03",
      G.shortcode_to_datetime("DVWQjTbjmri").strftime("%Y-%m") == "2026-03",
      G.shortcode_to_datetime("DVWQjTbjmri").isoformat())
check("'guide to running shoes in 2026' -> 2026",
      G.shortcode_to_datetime("DYHxVb0iEI2").year == 2026)
check("shortcode inválido no lanza", G.shortcode_to_datetime("corto!") is None)

print("\n" + "=" * 58)
if FALLOS:
    print(f"FALLARON {len(FALLOS)}: {FALLOS}")
    sys.exit(1)
print("TODOS LOS TESTS PASARON")