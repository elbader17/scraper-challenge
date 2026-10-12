# Instagram Hashtag Scraper (PoC) — vía Embeddings públicos

Extrae publicaciones públicas de Instagram a partir de un hashtag **sin login y sin
credenciales de usuario**, usando la ruta pública de *embeds*. Probado end-to-end.

El proyecto tiene **tres** estrategias de descubrimiento, todas sin autenticación:

| Script | Descubre posts vía | Estado |
|---|---|---|
| `ig_embed_scraper.py` | Grid público del hashtag | Funciona (techo ~12-14 posts) |
| `ig_serp_scraper.py` | DuckDuckGo (dorks `site:`) | Funciona con pacing lento |
| `ig_google_scraper.py` | **Google** (dorks + paginación `start=`) | Funciona con `--interactive`; reintentos + checkpoint |

## Instalación

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
# Básico (salida por defecto: ./downloads/sneakers)
python ig_embed_scraper.py --hashtag sneakers --limit 5

# Con proxy (HTTP/HTTPS/SOCKS5)
python ig_embed_scraper.py --hashtag sneakers --limit 20 --proxy-url socks5://127.0.0.1:9050

# Añadir fecha de publicación INFERIDA + verbose
python ig_embed_scraper.py --hashtag sneakers --limit 5 --decode-post-time -v
```

| Flag | Default | Descripción |
|---|---|---|
| `--hashtag` | *(obligatorio)* | Hashtag sin `#` |
| `--limit` | `10` | Máximo de posts a procesar |
| `--output-dir` | `./downloads/{hashtag}` | Carpeta de salida |
| `--proxy-url` | — | `http://`, `https://` o `socks5://` |
| `--retries` | `3` | Reintentos ante 429/5xx |
| `--timeout` | `25` | Timeout HTTP (s) |
| `--overwrite` | `False` | Re-descargar imágenes ya presentes |
| `--decode-post-time` | `False` | Añade `post_published_at` inferido |
| `-v/--verbose` | `False` | Log DEBUG |

## Salida

```
downloads/sneakers/
├── Dcs-O8mxFaj.jpg      # foto a resolución completa del post
├── DbCInidPZid.jpg
└── metadata.json
```

```json
{
  "scraped_at": "2026-10-06T18:08:58.512Z",
  "hashtag": "sneakers",
  "requested_limit": 3,
  "method": "public embed endpoint (no auth)",
  "stats": { "discovered": 12, "processed": 3, "images_saved": 3,
             "skipped_private": 0, "failed": 0, "rate_limited": 0 },
  "posts": [
    {
      "shortcode": "DbCInidPZid",
      "author": "umsneakers",
      "caption": "Prices You'll ONLY FIND At UMSNEAKERS.COM ! 🤯 #reels ...",
      "image_url": "https://instagram.faep9-2.fna.fbcdn.net/v/t51.82787-15/...jpg",
      "local_image_path": "downloads/sneakers/DbCInidPZid.jpg",
      "scraped_at": "2026-10-06T18:08:55.001Z",
      "post_published_at": null,
      "post_published_at_inferred": false,
      "is_video": false
    }
  ]
}
```

## Cómo funciona

**Fase 1 — Descubrimiento.** `GET /explore/tags/{hashtag}/` devuelve el HTML renderizado
en servidor. Los shortcodes salen de dos fuentes: anclas `<a href="/reel/{code}/">` y
blobs `"code":"{code}"`.

**Fase 2 — Embed.** `GET /p/{shortcode}/embed/captioned/` responde 200 sin sesión y
contiene autor, caption y la imagen.

**Fase 3 — Persistencia.** Descarga `{shortcode}.jpg` y escribe `metadata.json`.

## Estrategia Google — `ig_google_scraper.py`

Posts de Instagram **indexados en Google**, discovery por dorks y paginación real,
y extracción vía el mismo embed público (que sí funciona sin bloqueo).

```bash
# Recomendado: abre un Chrome, resolvés la verificación una vez y sigue
python ig_google_scraper.py --query "running shoes" --limit 30 --interactive

# Con proxy (sólo si además estás detrás de una IP bloqueada)
python ig_google_scraper.py --query "running shoes" --limit 30 \
  --proxy-url http://usuario:clave@host:puerto

# Sin depender de Google: shortcodes ya conocidos -> directo al embed
python ig_google_scraper.py --query sneakers --shortcodes DYHxVb0iEI2,DceGJtwimbX
```

`--interactive` requiere `pip install websockets`. Google tiene una ventaja
estructural sobre DDG: **sí pagina**, con `start=0,100,200…` y `num=100`. DDG
no (`&s=10` devuelve 202), por eso esa estrategia rota dorks.

### Hallazgo crítico: Google no entrega la SERP sin una sesión resuelta

Medido empíricamente, en este orden:

1. **La vía HTTP pelada devuelve un shell que exige JavaScript.** ~92 KB, 200 OK,
   **3 anclas**, cero resultados. No es un error: un scraper ingenuo reporta
   "0 posts" cuando en realidad nunca vio la SERP.
2. **Con Chromium headless el DOM se renderiza, pero Google responde CAPTCHA**
   ("Our systems have detected unusual traffic…"), también como un 200 más.
3. **Los TLD se comportan igual** (`.com`, `.es`, `.com.ar`, `.co.uk`, `.ca`,
   `.mx`): el bloqueo no es de dominio. Probado sin éxito: cookie `CONSENT`,
   `gbv=1`, `udm=14`, `udm=2`, `tbm=isch`, `tbm=vid`, `client=firefox-b-d`,
   `client=ubuntu`, UA de Lynx, `pws=0`, `safe=off`. Tampoco es transitorio
   (reintentado a los 75 s).
4. **El DOM de Google esconde los enlaces.** La SERP renderizada muestra los
   resultados (autor, like, fecha) pero los `<a>` de resultado **no tienen
   `href`**, y tampoco aparecen al recorrer shadow DOM. Por eso leer el DOM del
   navegador no sirve: hay que volver a pedir la página por HTTP.
5. **La solución: las cookies de una sesión que ya pasó el CAPTCHA.** Con un
   Chrome real donde el usuario resuelve la verificación una vez, y exportando
   sus cookies por CDP (`NID`, `SNID`, `GOOGLE_ABUSE_EXEMPTION`, `AEC`, `DV`),
   la vía HTTP común pasa a devolver la SERP completa: 579 KB y 10 shortcodes
   en la primera consulta.

De ahí el diseño de `--interactive`: el navegador **no** se usa para scrapear,
sino sólo para destrabar el CAPTCHA. Después el scraping corre por HTTP, que es
más rápido y no vuelve a pedir la verificación. El perfil es persistente, así
que las corridas siguientes reutilizan lo ya resuelto.

### Trampa: `/sorry/index` no sirve como detector de CAPTCHA

Una SERP legítima **contiene** un enlace a `/sorry/index` en su HTML. Usarla
como marcador produjo un falso positivo que hacía esperar un captcha inexistente
(el script quedó en loop sobre una SERP válida con 11 shortcodes). La detección
correcta combina `location.pathname` con el texto "unusual traffic".

### Verificación end-to-end

```
Chrome listo (wayland). Perfil: /root/.ig_google_profile
  SERP cargada (11 shortcodes); exportando cookies…
  Cookies obtenidas: AEC, DV, GOOGLE_ABUSE_EXEMPTION, NID, SEARCH_SAMESITE, SNID, _GRECAPTCHA, __Secure-STRP
Fase 1: 13 shortcodes únicos descubiertos
Fase 2/3: 12 posts, 12 imágenes -> output/google_running_shoes/metadata.json
```

Sin proxy, sin login, sin credenciales: 13 shortcodes descubiertos en Google y
12 posts con imagen a resolución completa, todos con autor y caption reales.

### Escala: concurrencia, cola de keywords y frenado

```bash
# Varios keywords en cola (repetible o desde archivo)
python ig_google_scraper.py --query "running shoes" --query "trail running" \
  --limit 500 --interactive --workers 6 --rate 0.4 --discovery-rate 6

# Desde archivo (usa # para comentarios)
python ig_google_scraper.py --queries-file keywords.txt --limit 500 --interactive

# Si se corta (Ctrl-C, caída, red), se retoma desde donde quedó
python ig_google_scraper.py --queries-file keywords.txt --limit 500 --resume
```

| Flag | Default | Para qué |
|---|---|---|
| `--query` | — | Keyword. **Repetible** para armar una cola |
| `--queries-file` | — | Archivo con una keyword por línea |
| `--workers` | `1` | Workers concurrentes en la fase 2/3 |
| `--rate` | `0` | Segundos entre requests a Instagram. `0` = sin freno |
| `--discovery-rate` | `0` | Segundos entre consultas a Google. Si es `0` usa `--rate` |
| `--discovery-retries` | `2` | Reintentos por consulta de Google ante 429 |
| `--db` | `<output-dir>/posts.sqlite3` | Base incremental |
| `--resume` | `False` | Omite posts ya terminados y sigue con los pendientes |
| `--retry-failed` | `False` | Reincluye los que quedaron en `failed` |
| `--retries` | `3` | Intentos por post ante 429/5xx |
| `--retry-delay` / `--retry-max-delay` | `2` / `60` | Backoff exponencial, en segundos |

**Rendimiento medido** (12 posts, misma máquina):

| Config | Tiempo |
|---|---|
| 1 worker, sin freno | 9.24 s |
| 6 workers, sin freno | **2.09 s** (4.4x) |
| 6 workers, `--rate 0.3` | 7.23 s |

Proyectado a 3000 posts: **~4.4 h → ~30 min**. El freno global es lo que te
permite bajar la velocidad sin tocar código cuando Instagram empieza a cortar.

**El rate limiter reserva el slot bajo lock pero espera fuera del lock.** Si no,
los workers se serializarían de a uno y la concurrencia no serviría de nada.
Instagram no publica su límite: medido empíricamente, a ~3 req/s (`--rate 0.3`)
no hubo un solo 429.

### Por qué Google y no sólo dorks: el techo

Una keyword rinde ~63 shortcodes únicos (medido con 8 dorks: 81 devueltos, 63
únicos, **78% de solapamiento**). Google indexa lo mismo bajo todos esos
filtros, así que rotar dorks rinde poco y la cola de keywords es el mecanismo
real para llegar a miles.

Lo que **sí** aporta son las páginas: `start=0` dio 14 shortcodes y `start=100`
dio 10 más, todos nuevos.

### Google te va a frenar si no lo frenás vos

Después de un uso sostenido, Google empieza a devolver **429** en las consultas
de discovery. El script reintenta con backoff y, si sigue, lo dice con sus
palabras:

```
WARNING  Google limita el ritmo (429) tras 3 intentos; corré --rate más alto o esperá
```

`--discovery-rate 6` (una consulta cada 6 s) evitó los 429 por completo en las
corridas de prueba. Los contadores de `stats` separan a propósito tres cosas que
antes se confundían: `blocked_queries` (captcha), `js_shell_queries` (no vi la
SERP) y `rate_limited_queries` (nos está frenando). **Ninguna de las tres es
"no hay resultados"**, y el mensaje final lo dice con precisión.

### Decisiones de diseño que importan a escala

1. **Autocommit.** Cada post se escribe en SQLite en cuanto se procesa. Sin
   `close()`: se verificó que un corte abrupto no pierde lo ya hecho.
2. **`metadata.json` se reconstruye desde la base**, no desde la corrida. Por eso
   el archivo final refleja todo el conocimiento acumulado aunque la corrida se
   haya cortado a la mitad.
3. **Un 429 no pierde posts.** Se espera el `Retry-After` que manda el servidor
   (acotado por `--retry-max-delay`) y se reintenta con backoff exponencial con
   jitter. Un 404 no se reintenta nunca: es terminal por definición.
4. **Los fallos quedan registrados** en la tabla `errors`, con shortcode, intento
   y mensaje. `failed` no se reintenta solo en la corrida siguiente salvo que
   pidas `--retry-failed`, para no Ciclar eternamente sobre un post inalcanzable.
5. **Con `--resume`, un discovery bloqueado no aborta la corrida**: se sigue con
   lo que ya está en la base. Sin esto, retomar una corrida de Google obligaba a
   rehacer el bootstrap del CAPTCHA.
6. **El store es thread-safe**: `check_same_thread=False` + lock sobre las
   escrituras. Una conexión SQLite compartida sin serializar corrompe la base.
7. **La extensión se decide por los bytes**, no por la URL. El CDN de Instagram
   sirve WebP aunque la URL termine en `.jpg` — medido en 2 de 24 posts de una
   corrida real. Guardar WebP como `.jpg` rompe PIL, torchvision y ffmpeg.

### Metadata enriquecida: el blob que ya venía y se descartaba

El embed renderizado incluye un `contextJSON` con `gql_data.shortcode_media`: una
respuesta GraphQL real. El scraper ahora la parsea y agrega campos que antes se
perdía, **sin un solo request extra**.

| Campo | Qué es |
|---|---|
| `likes`, `comments` | `edge_liked_by.count`, `edge_media_to_comment.count` |
| `author_followers` | `owner.edge_followed_by.count` |
| `author_verified` | `owner.is_verified` |
| `width`, `height` | dimensiones reales |
| `alt_text` | `accessibility_caption` (alt-text de Instagram) |
| `carousel_count`, `carousel_shortcodes` | diapositivas del carrusel |
| `media_id`, `author_id` | IDs numéricos |
| `post_published_at` | fecha **inferida**, decodificada del media ID |

Con esto se vuelve calculable el **engagement rate** `(likes+comentarios)/seguidores`,
que es la métrica más útil para ranking de influencers.

### Cobertura: 83% del metadata enriquecido, 100% de la fecha

Medido sobre 12 posts reales: **10 de 12 traen el blob**. Los 2 que no,
Instagram los sirve como `isRichEmbed: false` con `contextJSON: null`.

**No es transitorio**: se reintentó 4 veces cada uno y nunca apareció, y ninguna
variante de URL lo recupera (`/embed/` sin `/captioned/`, con `?img_index=1`).
El endpoint `?__a=1&__d=dis` ahora devuelve 404. Por eso los campos quedan en
`null` y no se reintenta: `rich_metadata: false` marca el caso.

### La fecha: decodificada del snowflake, no oficial

```
media_id   = base64url(shortcode)
epoch_ms   = (media_id >> 23) + 1314220021721
```

Dos validaciones independientes:

- El ID derivado del shortcode coincide **exacto** con el `id` que trae el blob.
- Captions que declaran su propio mes/año aciertan: *"Running shoes dropping in
  August 2026"* → `2026-08-01`; *"in March 2026"* → `2026-03-01`; *"guide to
  running shoes in 2026"* → `2026-05-09`.

Cubre el **100%** de los posts, porque depende sólo del shortcode, que siempre se
conoce. Se marca `post_published_at_inferred: true`: es exacto, pero es un decode
del esquema de IDs, no un campo que Instagram exponga.

### Lo que sigue sin obtenerse

`taken_at` oficial, **vistas de video**, guardados y listas de seguidores. No
están en ninguna variante del embed. Requerirían la Graph API de Meta con una
Business Account, es decir login y permisos.

> Hueco sin cerrar: no se llegó a probar un **reel** para confirmar si los videos
> traen campos extra (duración, vistas) además de los comunes. Google empezó a
> devolver 429 durante la investigación y no se quiso seguir castigándolo.

## Tests

```bash
python test_ig_google.py    # 75 checks, sin red
```

Cubren idempotencia del discovery, estados del store, supervivencia a un corte
sin `close()`, política de reintentos (incluido que 404 no se reintenta y que
`Retry-After` se acota), parsing de `Retry-After`, clasificación de errores HTTP,
espaciado del rate limiter bajo concurrencia, escritura concurrente en SQLite
(200 posts x 8 hilos, sin corromper), saneo de la cola de keywords,
clasificación de fallos del discovery (429 / captcha / shell JS como hechos
distintos), detección de formato de imagen, extracción del `contextJSON`
(incluyendo escapes y embed liviano) y decode de la fecha por snowflake validado
contra captions reales.

## Hallazgos sobre el HTML actual de Instagram

Verificados empíricamente, no asumidos:

1. **No hay `og:*`, ni JSON-LD, ni `<time>`.** El parseo debe apoyarse en clases CSS.
   Por eso `scraped_at` (momento de la extracción) es el timestamp fiable, y la fecha
   de publicación real **ya no está disponible** en el embed.
2. **La foto del post es `img.EmbeddedMediaImage`.** El avatar del autor *también* es un
   `<img>` (sin clase, `alt="{username}"`, con el segmento CDN `t51.2885-19`). Tomar el
   primer `<img>` produce fotos de 40×40 en vez del post.
3. **Soft-404.** Para shortcodes inexistentes, privados o eliminados, Instagram devuelve
   **200** (no 404) con el aviso *"The link to this photo or video may be broken"*.
   Se detecta en `is_unavailable()` para no registrar posts fantasma con campos nulos.
4. **Falso positivo `en_US`.** Los blobs de configuración del cliente
   (`{"IntlCurrentLocale",[],"code":"en_US"}`) producen un "shortcode" espurio.
   `is_plausible_shortcode()` lo filtra por longitud, alfabeto y forma de locale.
5. **Sólo ~12-14 posts por hashtag.** La página pública no pagina: `?page=2` devuelve el
   mismo grid. Un `--limit` alto se avisa por log pero no es alcanzable por esta vía.
6. **La página del hashtag se sirve de forma intermitente.** Instagram devuelve a veces una
   "página shell" genérica (`<title>Instagram</title>`, ~490 KB, cero anclas) incluso para
   hashtags válidos y enormes: se comprobó que **`#moda` devolvió 12 posts en una corrida y
   0 en la siguiente**. Por eso la Fase 1 reintenta hasta 3 veces ante un resultado vacío;
   un solo intento convertiría ese falso negativo en un error definitivo. El log incluye el
   `<title>` y el tamaño de cada intento para que el diagnóstico sea verificable.

## Etiquetas sin grid público

No todas las etiquetas tienen contenido en la vista pública, y eso **no es un bug del
script**. Verificado con `#ropa`: devuelve consistentemente la página shell, sin posts,
tras 3 intentos —mientras `#moda`, `#vestimenta` y `#fashion` devuelven 12 cada una.
Curiosamente, una etiqueta inventada como `#ropaaa` devolvió 8 posts: el grid responde a
la popularidad del trending, no a la existencia o validez de la etiqueta.

Si una etiqueta devuelve 0, el log lo distingue de forma explícita de un fallo.

## Limitaciones

- **No hay paginación:** ~12-14 posts por hashtag. Es el techo duro del método público.
- **Sólo contenido destacado/popular** de la etiqueta, no el feed cronológico completo.
- **`post_published_at` es inferido, no real.** Con `--decode-post-time` se decodifica el
  snowflake del shortcode; se marca `post_published_at_inferred: true`. No debe usarse
  como fecha de publicación verificada.
- **Los reels** entregan su frame de portada vía CDN; el MP4 requiere otra ruta.
- **Bloqueo por IP:** tras volumen alto Instagram responde 429. El script lo maneja con
  backoff exponencial y respetando `Retry-After`, y **detiene** la ejecución si el 429
  persiste. Para scraping sostenido hace falta rotación de proxies.
- El script es secuencial con retardos de 1.5–3.5 s a propósito: imita tráfico humano.
  Meter concurrencia asyncio anularía esa protección.

## Uso responsable

Sólo contenido público, con ritmo limitado. Verificá los Términos de Uso de Instagram y
la normativa aplicable a tu caso antes de escalar volumen.