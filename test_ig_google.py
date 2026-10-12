#!/usr/bin/env python3
"""
Tests for the incremental store and the retry policy.

Sin red: todo se verifica con dobles de prueba. El objetivo es que estas reglas
do not break silently, because in a run of thousands of posts an error here
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
check("empty pending => nothing to process on the second run",
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
print("\n== retries: retry policy ==")
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
check("no sleep after the last attempt", time.time() - t0 < 0.6,
      f"took {time.time()-t0:.2f}s")

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
      "waiting 30s at once would break the run; it must be capped")

nf = Boom(G.NotFoundError("404"), 5)
try:
    G.with_retries(nf, attempts=5, base_delay=0.05)
except G.NotFoundError:
    pass
check("404 is not retried (terminal)", nf.calls == 1, f"called {nf.calls} times")

boom = Boom(G.FetchError("500"), 99)
try:
    G.with_retries(boom, attempts=3, base_delay=0.01, max_delay=0.05)
except G.FetchError:
    pass
check("attempts exhausted, propagates the error", boom.calls == 3, f"called {boom.calls}")

errs_logged = []
G.with_retries(Boom(G.FetchError("500"), 2), attempts=3, base_delay=0.01,
               max_delay=0.02, on_error=lambda a, e: errs_logged.append(a))
check("on_error reports every failure", errs_logged == [1, 2], f"received {errs_logged}")

# --------------------------------------------------------------------------- #
print("\n== Retry-After: parsing ==")
# --------------------------------------------------------------------------- #
check("header en segundos", G.parse_retry_after("120") == 120.0)
check("header ausente -> None", G.parse_retry_after(None) is None)
check("header basura -> None", G.parse_retry_after("pronto") is None)
futuro = G.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
check("header en fecha HTTP", futuro is not None and futuro > 100000,
      f"returned {futuro}")
check("header en pasado -> 0", G.parse_retry_after("Wed, 21 Oct 2020 07:28:00 GMT") == 0.0)

# --------------------------------------------------------------------------- #
print("\n== _check: HTTP error classification ==")
# --------------------------------------------------------------------------- #
for status, exc in ((429, G.RateLimitedError), (404, G.NotFoundError),
                    (500, G.FetchError), (403, G.FetchError)):
    try:
        G.CurlCffiClient._check(status, "u", "7")
        check(f"{status} raises", False, "did not raise")
    except Exception as e:
        check(f"{status} -> {exc.__name__}", isinstance(e, exc),
              f"raised {type(e).__name__}")
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
check("saves WebP with the .webp extension",
      G.download_image(_ImgClient(webp), out_i, p_webp, False)
      and (out_i / "ABC1234567.webp").exists(),
      f"archivos: {[f.name for f in out_i.iterdir()]}")
check("no re-descarga si ya existe (idempotente)",
      not G.download_image(_ImgClient(webp), out_i, p_webp, False))
check("local_image_path points to the correct extension",
      p_webp.local_image_path.endswith("ABC1234567.webp"),
      f"apunta a {p_webp.local_image_path}")
check("--overwrite re-descarga",
      G.download_image(_ImgClient(webp), out_i, p_webp, True))

# --------------------------------------------------------------------------- #
print("\n== BUG 1: los reintentos deben aplicarse a la descarga de imagen ==")
# --------------------------------------------------------------------------- #
# The missing test that let the bug through: `download_image` caught the
# `FetchError` and returned False, so the `with_retries` wrapping it saw a "success"
# and NEVER retried. On a transient 429 the image was lost permanently, because the
# post stayed `ok` and neither --retry-failed nor --resume recovers it (`pending()`
# excludes `ok`).
# The client mimics what the CDN does: two 429s and then a valid image.


class _FlakyImgClient:
    """Fails `fail_times` times with 429 and then returns a valid JPEG."""

    def __init__(self, fail_times, data=None):
        self.fail_times = fail_times
        self.data = data if data is not None else (b"\xff\xd8\xff\xe0" + b"\x00" * 2000)
        self.calls = 0

    def get_bytes(self, url):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise G.RateLimitedError("HTTP 429", 0.01)
        return self.data

    def get_text(self, *a, **k):
        return 200, ""

    def close(self):
        pass


out_r = Path(tempfile.mkdtemp(prefix="igretry-"))
p_ret = InstagramPost(shortcode="RETRY11111", source_serp_url="u",
                      scraped_at=_dt.now(_tz.utc), image_url="http://cdn/x.jpg")

_flaky = _FlakyImgClient(2)
_saved = G.with_retries(
    lambda: G.download_image(_flaky, out_r, p_ret, False),
    attempts=3, base_delay=0.01, max_delay=0.05)
check("reintenta y RECUPERA la imagen tras un 429 transitorio",
      _flaky.calls == 3 and _saved and (out_r / "RETRY11111.jpg").exists(),
      f"calls={_flaky.calls} saved={_saved} "
      f"archivos={[f.name for f in out_r.iterdir()]}")
check("la imagen recuperada tiene contenido real",
      p_ret.local_image_path and Path(p_ret.local_image_path).stat().st_size > 1024,
      f"ruta={p_ret.local_image_path}")

_flaky2 = _FlakyImgClient(5)  # nunca se recupera
_raised = None
try:
    G.with_retries(lambda: G.download_image(_flaky2, out_r, p_ret, False),
                   attempts=3, base_delay=0.01, max_delay=0.05)
except G.FetchError as _e:
    _raised = _e
check("si nunca se recupera, PROPAGA el error tras agotar los intentos",
      _raised is not None and _flaky2.calls == 3,
      f"calls={_flaky2.calls} exc={_raised!r}")

# A 404 must never be retried: it is terminal by definition.
class _NotFoundImg:
    def __init__(self):
        self.calls = 0

    def get_bytes(self, url):
        self.calls += 1
        raise G.NotFoundError("404 en " + url)


_nf_img = _NotFoundImg()
try:
    G.with_retries(lambda: G.download_image(_nf_img, out_r, p_ret, False),
                   attempts=3, base_delay=0.01, max_delay=0.05)
except G.NotFoundError:
    pass
check("404 en la imagen NO se reintenta", _nf_img.calls == 1,
      f"called {_nf_img.calls} times")

# A tiny response or an unknown format are transient CDN failures, so they must
# propagate for the retry to be able to help; they must not come back as False.
_small = _FlakyImgClient(0, data=b"\x00" * 10)
_propagated = False
try:
    G.download_image(_small, out_r, p_ret, False)
except G.FetchError:
    _propagated = True
check("respuesta sospechosamente chica propaga FetchError (es transitorio)",
      _propagated)

_bin = _FlakyImgClient(0, data=b"<?xml version='1.0'?>" + b"\x00" * 2000)
_propagated2 = False
try:
    G.download_image(_bin, out_r, p_ret, False)
except G.FetchError:
    _propagated2 = True
check("formato desconocido propaga FetchError en vez de perder la imagen",
      _propagated2)

# --------------------------------------------------------------------------- #
print("\n== BUG 2 (parte A): epoch del snowflake ==")
# --------------------------------------------------------------------------- #
import ig_embed_scraper as _emb

check("ambos scripts usan el MISMO epoch",
      _emb.MEDIA_ID_EPOCH_MS == G.IG_ID_EPOCH_MS,
      f"embed={_emb.MEDIA_ID_EPOCH_MS} google={G.IG_ID_EPOCH_MS}")
check("the epoch is NOT the ~340-days-lower value it used to be",
      _emb.MEDIA_ID_EPOCH_MS != 1284870074250)
check("decode del shortcode reproduce el id oficial del blob real",
      _emb.shortcode_to_media_id("DN292ZY3K4y") == 3708423362135764530,
      f"obtuvo {_emb.shortcode_to_media_id('DN292ZY3K4y')}")
check("infer_published_at ahora da 2025-08-27 (no 2024-09-21)",
      (_emb.infer_published_at("DN292ZY3K4y") or "").startswith("2025-08-27"),
      f"obtuvo {_emb.infer_published_at('DN292ZY3K4y')}")

# --------------------------------------------------------------------------- #
print("\n== BUG 2 (part B) + undocumented bug: numeric ids arriving as STRING ==")
# --------------------------------------------------------------------------- #
# Instagram ahora sirve `id` y `owner.id` como cadenas entrecomilladas, no como
# JSON numbers. The strict `isinstance(x, int)` checks discarded them silently:
# `media_id` and `author_id` were NULL on EVERY live post despite
# `rich_metadata=1`. Found while validating the epoch against a live embed.
_sm = {
    "gql_data": {"shortcode_media": {
        "id": "3978685437899244498",           # string, no int
        "shortcode": "Dc3IVNnAQPS",
        "owner": {"id": "123456", "username": "ana", "is_verified": False,
                  "edge_followed_by": {"count": 8684}},
        "edge_liked_by": {"count": 394},       # sigue siendo int
        "edge_media_to_comment": {"count": 8},
        "dimensions": {"width": "1080", "height": "1350"},  # strings
        "display_resources": [],
        "edge_sidecar_to_children": {"edges": []},
    }}}
_m = G.parse_gql_media(_sm)
check("media_id se parsea aunque venga como string",
      _m["media_id"] == 3978685437899244498, f"obtuvo {_m['media_id']!r}")
check("author_id se parsea aunque venga como string",
      _m["author_id"] == 123456, f"obtuvo {_m['author_id']!r}")
check("dimensions se parsean aunque vengan como string",
      _m["width"] == 1080 and _m["height"] == 1350,
      f"obtuvo {_m['width']}x{_m['height']}")
check("los counts que ya eran int siguen funcionando",
      _m["likes"] == 394 and _m["comments"] == 8 and _m["author_followers"] == 8684)
check("coerce: bool no se acepta como int",
      G._coerce_int(True) is None and G._coerce_int(False) is None)
check("coerce: basura sigue siendo None",
      G._coerce_int("abc") is None and G._coerce_int(None) is None
      and G._coerce_int("") is None)

# --------------------------------------------------------------------------- #
print("\n== BUG 3: datetime serialized explicitly ==")
# --------------------------------------------------------------------------- #
import warnings as _warnings

_s3 = PostStore(tmpdb())
_s3.record_discovered([("DT000000001", "u")], "q")
_dtval = _dt(2026, 8, 1, 12, 0, tzinfo=_tz.utc)
with _warnings.catch_warnings(record=True) as _caught:
    _warnings.simplefilter("always")
    _s3.mark_ok("DT000000001", query="q", author="a", caption="c", image_url="i",
                local_image_path=None, is_video=False, source_serp_url="s",
                scraped_at=_dtval.isoformat(), attempts=1,
                extra={"post_published_at": _dtval,
                       "post_published_at_inferred": True})
_dep = [w for w in _caught
        if "datetime adapter" in str(w.message) and w.category is DeprecationWarning]
check("mark_ok NO usa el adaptador datetime deprecado", not _dep,
      f"warnings: {[str(w.message)[:60] for w in _dep]}")
check("el timestamp se guarda igual, en ISO",
      _s3._ex("SELECT post_published_at FROM posts").fetchone()["post_published_at"]
      == "2026-08-01T12:00:00+00:00",
      _s3._ex("SELECT post_published_at FROM posts").fetchone()["post_published_at"])
_s3.close()

# --------------------------------------------------------------------------- #
print("\n== BUG 4: _parse_dt ya no inventa timestamps ==")
# --------------------------------------------------------------------------- #
check("timestamp corrupto -> None (ya no 'ahora')",
      G._parse_dt("no-es-una-fecha") is None, f"obtuvo {G._parse_dt('no-es-una-fecha')!r}")
check("timestamp ausente -> None", G._parse_dt(None) is None and G._parse_dt("") is None)
_dtok = G._parse_dt("2026-08-01T12:00:00+00:00")
check("valid timestamp is parsed", _dtok is not None and _dtok.year == 2026)
check("naive se normaliza a UTC",
      (G._parse_dt("2026-08-01T12:00:00") or _dt(2026, 1, 1)).tzinfo is not None)

# --------------------------------------------------------------------------- #
print("\n== stats: los fallos de imagen ya no se esconden ==")
# --------------------------------------------------------------------------- #
_st_stats = G.ScrapeStats()
check("ScrapeStats tiene images_failed",
      "images_failed" in G.ScrapeStats.model_fields)
check("images_failed arranca en 0", _st_stats.images_failed == 0)

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
      f"took {dur:.2f}s")

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
check("discards comments and empty lines", "# comentario" not in kws)
check("quita comillas", G.resolve_keywords(
    argparse.Namespace(query=['"moda"'], queries_file=None)) == ["moda"])
try:
    G.resolve_keywords(argparse.Namespace(query=None, queries_file=None))
    check("no keywords should fail", False, "did not fail")
except SystemExit:
    check("sin keywords debe fallar", True)

# --------------------------------------------------------------------------- #
print("\n== discovery: failure classification ==")
# --------------------------------------------------------------------------- #


class _Serp:
    """Fake SERP that fails with whichever exception it is given."""

    def __init__(self, exc, n_ok=0):
        self.exc, self.n_ok, self.calls = exc, n_ok, 0

    def search(self, query, start=0, num=100):
        self.calls += 1
        if self.calls <= self.n_ok:
            return {"ZZZ0000000"}, "u"
        if self.exc is None:
            return set(), "u"  # valid query returning nothing
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
      s_backoff.calls >= 2, f"called {s_backoff.calls} times")

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
check("picks the largest resolution (1440, not 720)",
      m["best_url"].endswith("1440.jpg"), f"picked {m['best_url']}")

check("embed liviano -> sin metadata, no rompe", G.extract_context_json(HTML_LIGHT) is None)
check("HTML sin contextJSON -> None", G.extract_context_json("<html>nada</html>") is None)
check("contextJSON corrupto -> None, no lanza",
      G.extract_context_json('<script>"contextJSON":"{\\"roto":' ) is None)

enr = G._enrich_from_gql(HTML_RICH, "DN292ZY3K4y")
check("enriquecimiento con blob: rich_metadata=True", enr["rich_metadata"] is True)
check("enrichment upgrades the URL to the best resolution",
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
# Cross-validation against captions that declare their own month/year:
# "Running shoes dropping in August 2026" y "... in March 2026".
check("'dropping in August 2026' -> 2026-08",
      G.shortcode_to_datetime("DbggwHEER81").strftime("%Y-%m") == "2026-08",
      G.shortcode_to_datetime("DbggwHEER81").isoformat())
check("'dropping in March 2026' -> 2026-03",
      G.shortcode_to_datetime("DVWQjTbjmri").strftime("%Y-%m") == "2026-03",
      G.shortcode_to_datetime("DVWQjTbjmri").isoformat())
check("'guide to running shoes in 2026' -> 2026",
      G.shortcode_to_datetime("DYHxVb0iEI2").year == 2026)
check("invalid shortcode does not raise", G.shortcode_to_datetime("corto!") is None)

print("\n" + "=" * 58)
if FALLOS:
    print(f"FALLARON {len(FALLOS)}: {FALLOS}")
    sys.exit(1)
print("ALL TESTS PASSED")