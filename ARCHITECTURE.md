# ARCHITECTURE.md — Guía para otro LLM

Documento de orientación para entender el proyecto **sin leer los 3.934 líneas de código**
y sin volver a descubrir lo que ya se midió empíricamente. El `README.md` original es
una bitácora de experimentos; este archivo es el mapa.

> **Al leer este archivo, tratá las secciones «Hallazgos verificados» y «Bugs
> conocidos» como verdad.** Son cosas que cuestan horas de red y CPU descubrir, y varias
> ya son trampas conocidas: cambiarlas "para limpiar el código" rompe el scraper.

---

## 1. Qué es este proyecto

Un extractor de **posts públicos de Instagram** por hashtag o keyword, **sin login, sin
cookies de usuario y sin credenciales**. Prueba de concepto (PoC) que grew de forma
orgánica: funciona, está medido, y su valor real está en el conocimiento acumulado
sobre cómo responde el HTML de Instagram y de Google hoy.

**Idioma del código:** comentarios y documentación están en español (rioplatense).
Mantener ese idioma al agregar código.

**Estado:** 3.934 líneas Python, sin dependencias fuera de `requirements.txt`,
sin git, sin CI. `python test_ig_google.py` → 75 checks, todos pasan.

---

## 2. Mapa de archivos

| Archivo | Líneas | Rol | ¿Vivo? |
|---|---:|---|---|
| `ig_google_scraper.py` | 1.735 | **Script principal.** Discovery en Google → embed → imagen + SQLite | Sí, el que importa |
| `ig_google_store.py` | 253 | Store SQLite incremental (autocommit, thread-safe) | Sí, importado por el anterior |
| `test_ig_google.py` | 486 | 75 checks sin red | Sí |
| `ig_embed_scraper.py` | 839 | PoC original: grid público del hashtag | Legado. **Ver bug de epoch** |
| `ig_serp_scraper.py` | 621 | Discovery vía DuckDuckGo | Legado, sin paginación real |
| `README.md` | 17 KB | Bitácora de hallazgos y mediciones | Leer para el *porqué* |
| `output/`, `downloads/` | — | Resultados de corridas reales | Datos de referencia |

### Dependencias (`requirements.txt`)

`curl_cffi` (impersonación TLS de Chrome, núcleo del anti-fingerprinting), `beautifulsoup4`,
`httpx` (respaldo), `pydantic` (modelos). Opcionales y comentadas: `websockets` (sólo
para `--interactive`), `httpx[socks]` (proxy SOCKS5).

---

## 3. Flujo principal (`ig_google_scraper.py`)

```
                    ┌─────────────────────────────────────────┐
   --query ────────►│ FASE 1 — DISCOVERY (Google)             │
                    │  rota dorks × páginas (start=0,100,200) │
                    │  extrae shortcodes de la SERP            │
                    └──────────────────┬──────────────────────┘
                                       │  dict[shortcode → url_serp]
                    ┌──────────────────▼──────────────────────┐
                    │ FASE 2 — EMBED (Instagram, sin auth)    │
                    │  GET /p/{code}/embed/captioned/        │
                    │  parsea DOM + blob GraphQL contextJSON  │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │ FASE 3 — PERSISTENCIA                   │
                    │  descarga imagen + mark_ok() en SQLite │
                    │  (autocommit por post)                  │
                    └──────────────────┬──────────────────────┘
                                       │
                              metadata.json (reconstruido desde la base)
```

Puntos de entrada: `main()` → `run()` (`ig_google_scraper.py:1467`) →
`PostProcessor.process()` (`:1380`) por post. `discover()` (`:852`) hace la Fase 1.

### Modelo de datos

`InstagramPost` (pydantic, `:163`) — campos base (`shortcode`, `author`, `caption`,
`image_url`, `local_image_path`, `source_serp_url`, `scraped_at`, `is_video`) más
metadata enriquecida del blob: `likes`, `comments`, `author_followers`,
`author_verified`, `width`/`height`, `alt_text`, `carousel_*`, `media_id`,
`post_published_at`, `rich_metadata`.

`ScrapeStats` (`:205`) — contadores **separados a propósito**: `blocked_queries`
(CAPTCHA), `js_shell_queries` (nunca vi la SERP) y `rate_limited_queries` (nos frenan)
**no son** «cero resultados». No los unifiques nunca.

### Esquema SQLite (`ig_google_store.py:35`)

Tabla `posts` con `shortcode` como PK y estados `discovered → ok | unavailable | failed`;
`errors` (shortcode, attempt, kind, message); `runs` (auditoría de corridas).
`_migrate()` (`:121`) agrega columnas nuevas a bases viejas — si tocás el esquema,
agregá la columna ahí también.

---

## 4. Hallazgos verificados (empíricos, no asumir)

Esto se midió contra respuestas reales. Es la parte más costosa de conseguir.

### Google: la SERP exige una sesión resuelta, siempre

1. **HTTP pelado devuelve un shell de JS**, 200 OK, ~92 KB, **3 anclas**, cero
   resultados. Un scraper ingenuo reporta «0 posts» cuando nunca vio la SERP.
2. **Chromium headless renderiza el DOM pero Google responde CAPTCHA** («unusual
   traffic»), también como un 200 más.
3. **Los TLD se comportan igual** (`.com`, `.es`, `.co.uk`…). El bloqueo no es de
   dominio. Probado sin éxito: `CONSENT`, `gbv=1`, `udm=14`, `client=firefox-b-d`, UA de
   Lynx, `pws=0`, `safe=off`.
4. **Los `<a>` de resultado no tienen `href`** en el DOM renderizado. Leer el DOM del
   navegador **no sirve**: hay que volver a pedir la página por HTTP.
5. **La solución son las cookies de una sesión que ya pasó el CAPTCHA.** `--interactive`
   abre un Chrome real, el usuario resuelve la verificación una vez, y exporta cookies
   por CDP (`NID`, `SNID`, `GOOGLE_ABUSE_EXEMPTION`, `AEC`, `DV`). **Después el
   scraping corre por HTTP**, que es más rápido y no vuelve a pedir la verificación.
   El perfil es persistente: corridas siguientes reutilizan lo ya resuelto.

**Trampa documentada:** `/sorry/index` **NO** sirve como detector de CAPTCHA — una
SERP legítima *contiene* ese enlace en su HTML. Usarlo dio falsos positivos que
hacían esperar un captcha inexistente. La detección correcta combina
`location.pathname` + el texto «unusual traffic» (`ig_google_scraper.py:121`).

**Clasificación estructurada** (`:826-848`): CAPTCHA por marcadores de texto;
shell JS por marcadores **o** `< MIN_RESULT_ANCHORS` (10) anclas. Una SERP real trae
decenas de anclas incluso sin resultados. Esa es la señal estructural.

### Google: lo que sí aporta es la paginación

`start=0` dio 14 shortcodes, `start=100` dio 10 más, todos nuevos. DuckDuckGo **no**
paga (`&s=10` devuelve 202), por eso esa estrategia rota dorks.

El techo real es **breadth, no profundidad**: una keyword rinde ~63 shortcodes únicos
(8 dorks → 81 devueltos, **78 % de solapamiento**). Google indexa lo mismo bajo todos
esos filtros, así que rotar dorks rinde poco; **la cola de keywords (`--query`
repetible / `--queries-file`) es el mecanismo real para llegar a miles**.

### Instagram: qué se puede y qué no se puede sacar

- **El embed público responde 200 sin sesión** y trae autor, caption e imagen. Es la
  vía que funciona.
- **La foto del post es `img.EmbeddedMediaImage`.** El avatar del autor *también* es un
  `<img>` (`alt="{username}"`, segmento CDN `t51.2885-19`). Tomar el primer `<img>`
  produce fotos de 40×40 en vez del post.
- **No hay `og:*`, ni JSON-LD, ni `<time>`.** El parseo **debe** apoyarse en clases
  CSS (`Caption`, `EmbeddedMediaImage`).
- **Soft-404:** para shortcodes inexistentes, privados o eliminados, Instagram devuelve
  **200** (no 404) con «The link to this photo or video may be broken». Se detecta en
  `is_unavailable()` (`:1018`) para no registrar posts fantasma con campos nulos.
- **Falso positivo `en_US`:** los blobs de config del cliente producen un «shortcode»
  espurio. `is_plausible_shortcode()` (en `ig_embed_scraper.py:362`) lo filtra.
- **Sólo ~12-14 posts por hashtag.** La página pública no pagina: `?page=2` devuelve el
  mismo grid. Es el techo duro del método público.
- **La página del hashtag se sirve de forma intermitente:** devuelve a veces una «página
  shell» (~490 KB, cero anclas) incluso para hashtags válidos y enormes. `#moda` dio 12
  posts en una corrida y 0 en la siguiente. Por eso la Fase 1 **reintenta** ante vacío:
  un solo intento convertiría un falso negativo en error definitivo.
- **El blob `contextJSON` no siempre está:** Instagram sirve `isRichEmbed: false` con
  `contextJSON: null`. Medido 2 de 12. **No es transitorio** (4 reintentos, ninguna
  variante de URL lo recupera; `?__a=1&__d=dis` ahora da 404). Los campos quedan `null`
  y `rich_metadata: false` lo marca. Cobertura: **83 % del enriquecido, 100 % de la
  fecha**.
- **`extract_context_json` no puede ser un regex ingenuo** (`:1256`): el JSON viene
  anidado y escapado; falla en casos reales. Escanea respetando escapes.
- **El CDN sirve WebP aunque la URL termine en `.jpg`** (medido 2 de 24). Por eso la
  extensión se decide **por los bytes** (`detect_image_format`, `:1028`). Guardar WebP
  como `.jpg` rompe PIL, torchvision y ffmpeg.

### La fecha: decodificada del snowflake, nunca oficial

```
media_id = base64url_decode(shortcode)
epoch_ms = (media_id >> 23) + 1314220021721
```

Dos validaciones independientes, **ambas en `test_ig_google.py:465-470`**:

- El ID derivado del shortcode coincide **exacto** con el `id` oficial del blob
  (`shortcode_to_media_id("DN292ZY3K4y") == 3708423362135764530`).
- Captions que declaran su propio mes/año aciertan: *«dropping in August 2026»* →
  `2026-08-01`; *«in March 2026»* → `2026-03-01`.

Cubre el **100 %** de los posts (depende sólo del shortcode). Se marca
`post_published_at_inferred: true`: es un decode del esquema de IDs, **no** un campo
que Instagram exponga.

---

## 5. Bugs conocidos (verificados por mí, no están en el README)

### 🔴 BUG 1 — Los reintentos no aplican a la descarga de imagen

`ig_google_scraper.py:1050-1054`. `download_image()` captura `FetchError` internamente
y devuelve `False`. En `PostProcessor.process` (`:1422-1429`) la función está envuelta
en `with_retries(attempts=a.retries)`, pero como **nunca propaga la excepción**, el
wrapper ve un «éxito» y **no reintenta nunca**.

Reproducido con un cliente que falla 2× con 429 y después entrega una JPEG válida:

```
intentos HTTP realizados: 1 (attempts=3)
with_retries devolvio: False
archivos guardados: []
```

**Impacto:** un 429 transitorio del CDN pierde la imagen **de forma permanente** — el
post se marca `ok` igual, así que ni `--retry-failed` ni `--resume` lo recuperan
(`pending()` excluye `ok`). Los flags `--retries`, `--retry-delay` y
`--retry-max-delay` son no-ops para imágenes, lo que contradice la garantía del README
(«Un 429 no pierde posts»).

**Causa raíz del bug:** el test de `download_image` (`test_ig_google.py:233`) usa un
cliente que **siempre funciona**; nunca se prueba el caso «falla y se recupera».

**Fix sugerido:** dejar que `download_image` propague `FetchError` y que el
`try/except` del caller haga el log y el `return False`.

### 🟠 BUG 2 — Epoch de snowflake incorrecto en `ig_embed_scraper.py`

`ig_embed_scraper.py:110` usa `MEDIA_ID_EPOCH_MS = 1284870074250`; el correcto es
`1314220021721` (`ig_google_scraper.py:1225`). Difieren en **29.349.947.471 ms ≈ 340
días**.

Consecuencia: las fechas inferidas por el scraper del hashtag salen **~340 días
adelantadas** (erróneas). Verificado sobre los 12 posts reales de
`output/google_running_shoes/`, donde los captions declaran mes/año:

| shortcode | epoch correcto (google) | epoch del embed scraper | caption |
|---|---|---|---|
| `DbggwHEER81` | **2026-08-01** ✅ | 2025-08-27 ❌ | «dropping in August 2026» |
| `DVWQjTbjmri` | **2026-03-01** ✅ | 2025-03-26 ❌ | «dropping in March 2026» |

El guard de `infer_published_at()` (`ig_embed_scraper.py:590`) descarta fechas `< 2010`
o futuras, así que **el bug pasa silenciosamente**: devuelve una fecha plausible pero
340 días equivocada. Ambos scripts decodifican el mismo `media_id`; sólo difiere el
epoch.

**Impacto limitado:** `post_published_at` sale `null` salvo que se pase
`--decode-post-time`, y los `metadata.json` existentes (`downloads/`, `output/`) tienen
la fecha en `null`. El daño es latente, no histórico.

### 🟡 BUG 3 — Adaptador datetime deprecado de sqlite3

`mark_ok()` (`ig_google_store.py:189`) recibe `post_published_at` como `datetime` (viene
de `post.model_dump()`) y SQLite lo serializa con el **adaptador por defecto,
deprecado desde Python 3.12**:

```
DeprecationWarning: The default datetime adapter is deprecated as of Python 3.12
```

Funciona en 3.14.7 y el roundtrip es correcto, pero se va a romper. Fix de una línea:
`x.get("post_published_at").isoformat() if ... else None`.

### 🟡 BUG 4 — `_parse_dt` enmascara datos corruptos

`ig_google_scraper.py:1193`: cualquier timestamp ilegible se sustituye
**silenciosamente** por `datetime.now()`. Un `scraped_at` corrupto se vuelve «ahora
mismo» y esconde el problema. Preferible `None` o un warning.

### 🟡 Nota — Fallos de imagen no se contabilizan

Un post cuya imagen falla queda `status=ok` con `local_image_path: null` y **no suma a
`stats.failed`**. Es defendible (el objetivo son los metadatos), pero conviene que sea
explícito en `stats` para no leer `ok` como «post completo».

---

## 6. Decisiones de diseño que importan a escala

1. **Autocommit.** Cada post se escribe en SQLite en cuanto se procesa
   (`isolation_level=None`). Sin `close()`: se verificó que un corte abrupto no pierde lo
   ya hecho.
2. **`metadata.json` se reconstruye desde la base**, no desde la corrida
   (`write_metadata_from_store`, `:1154`). El archivo refleja todo el conocimiento
   acumulado aunque la corrida se haya cortado a mitad.
3. **El store es thread-safe:** `check_same_thread=False` + `threading.Lock` sobre las
   escrituras. Una conexión SQLite compartida sin serializar corrompe la base.
4. **`--resume` no re-lanza discovery bloqueado** (`:1547`): si Google está trabado pero
   hay posts pendientes en la base, se sigue con lo ya descubierto. Sin esto, retomar
   una corrida obligaba a rehacer el bootstrap del CAPTCHA.
5. **El rate limiter reserva el slot bajo lock pero espera FUERA del lock**
   (`RateLimiter.acquire`, `:1104`). Si no, los workers se serializarían y la
   concurrencia no serviría.
6. **Cada worker tiene su propia sesión HTTP** (`threading.local` en
   `PostProcessor.client()`, `:1364`): las sessions no son thread-safe.
7. **El límite se aplica a la lista, no en el bucle** (`:1587`): con workers es más
   simple recortar la lista que llevar un contador compartido.
8. **Nada se traga en silencio** (`:1610`): una excepción inesperada en un worker se
   loguea como `error inesperado` e incrementa `stats.failed`, no se pierde.

---

## 7. Rendimiento (medido, misma máquina)

| Config | 12 posts |
|---|---:|
| 1 worker, sin freno | 9,24 s |
| 6 workers, sin freno | **2,09 s** (4,4×) |
| 6 workers, `--rate 0.3` | 7,23 s |

Proyectado a 3.000 posts: **~4,4 h → ~30 min**. Instagram no publica su límite: medido
empíricamente, a ~3 req/s (`--rate 0.3`) no hubo **un solo** 429. `--discovery-rate 6`
evitó los 429 de Google por completo.

---

## 8. Uso

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Recomendado: resolvés el CAPTCHA una vez y seguí
python ig_google_scraper.py --query "running shoes" --limit 30 --interactive

# Cola de keywords, concurrencia y frenado
python ig_google_scraper.py --queries-file keywords.txt --limit 500 \
  --interactive --workers 6 --rate 0.4 --discovery-rate 6

# Retomar una corrida cortada
python ig_google_scraper.py --queries-file keywords.txt --limit 500 --resume

# Sin depender de Google: shortcodes conocidos -> directo al embed
python ig_google_scraper.py --query sneakers --shortcodes DYHxVb0iEI2,DceGJtwimbX
```

### Flags (`ig_google_scraper.py:1654`)

`--query` (repetible) · `--queries-file` · `--limit` (10) · `--workers` (1) ·
`--rate` (0) · `--discovery-rate` (0) · `--discovery-retries` (2) · `--tld` (com) ·
`--render` · `--interactive` · `--profile` · `--cdp-port` (9222) · `--wait-captcha` (180) ·
`--max-queries` (6) · `--max-pages` (2) · `--num-per-page` (100) · `--delay-min` (3) ·
`--delay-max` (7) · `--timeout` (25) · `--retries` (3) · `--retry-delay` (2) ·
`--retry-max-delay` (60) · `--retry-failed` · `--db` · `--resume` · `--overwrite` ·
`--shortcodes` · `--output-dir` · `--proxy-url` · `-v`

**Salida:** `output/google_{slug}/{shortcode}.jpg` + `metadata.json` + `posts.sqlite3`.

---

## 9. Tests

```bash
python test_ig_google.py    # 75 checks, sin red, ~1 s
```

Es un script de asserts lineales (no pytest): cada `check(cond, msg)` imprime
`PASS`/`FAIL`. Cubren estados del store, supervivencia a corte abrupto, política de
reintentos, parsing de `Retry-After`, clasificación de errores HTTP, espaciado del rate
limiter bajo concurrencia, escritura concurrente en SQLite (200 posts × 8 hilos), saneo
de la cola de keywords, clasificación de fallos del discovery (429 / CAPTCHA / shell JS
como hechos **distintos**), detección de formato por bytes, extracción de `contextJSON`
(incluyendo escapes y embed liviano) y decode del snowflake validado contra captions
reales.

**Al agregar código:** agregá el check al final del script, mantené el idioma de los
mensajes y no introduzcas dependencia de red — el valor de la suite es que corre
offline y rápido.

---

## 10. Qué NO se puede obtener (y por qué no insistas)

`taken_at` **oficial**, **vistas de video**, guardados, listas de seguidores. No están
en ninguna variante del embed sin login. Requerirían la Graph API de Meta con una
Business Account: login y permisos, que es exactamente lo que este proyecto evita.

> Hueco sin cerrar: no se llegó a probar un **reel** para confirmar si los videos
> traen campos extra (duración, vistas) además de los comunes.

---

## 11. Responsabilidad y estado legal

Sólo contenido público, con ritmo limitado. Antes de escalar volumen, verificá los
Términos de Uso de Instagram y la normativa aplicable. Los proxies residenciales que
recomienda el README para destrabar el CAPTCHA de Google tienen implicaciones de costo
y cumplimiento que conviene evaluar explícitamente.

---

## 12. Contexto para otro LLM que trabaje acá

Si sos otro LLM trabajando acá:

- **Leé `README.md` para el porqué**, este archivo para el mapa. No son redundantes:
  el README narra los experimentos, éste describe la estructura.
- **No "simplifiques" los reintentos, los delays, ni los guards.** Parecen
  sobreingeniería; cada uno tapa un fallo real medido.
- **Antes de tocar `RE_SHORTCODE`, `CAPTCHA_MARKERS` o los parsers del embed**,
  entendé que cada constante corresponde a una respuesta real observada.
- **Si agregás un campo al modelo:** `InstagramPost` (pydantic), el `UPDATE` de
  `mark_ok()` (`ig_google_store.py:189`), `_migrate()`, y
  `write_metadata_from_store()` (`:1154`). Si te olvidás de alguno, el campo se pierde
  en silencio al reconstruir el JSON.
- **Verificá con datos reales** (`output/google_running_shoes/metadata.json`) antes de
  cambiar algo que altera la interpretación de los resultados.