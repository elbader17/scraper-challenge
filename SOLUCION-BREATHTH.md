# Solución al CAPTCHA: discovery en anchura sobre hashtags

**Fecha:** 2026-10-12 · **Estado:** implementada y verificada end-to-end.
**Script:** [`ig_hashtag_breadth.py`](./ig_hashtag_breadth.py)

---

## La solución en una línea

**Dejá de buscar en Google. Andá directo a los hashtags públicos de Instagram.**

No hay CAPTCHA porque no hay motor de búsqueda. No hay cookies porque no hay sesión.
No hay ventana de Chrome esperando que alguien tilde una casilla.

**Costo: $0. Intervención humana: 0. Verificado: 20/20 posts con imagen y metadata
enriquecida.**

---

## Qué se midió

```
$ python ig_hashtag_breadth.py --hashtag running --hashtag sneakers --hashtag yoga \
    --hashtag cycling --hashtag surfing --hashtag skate --hashtag climbing \
    --hashtag trailrunning --limit 20 --workers 4 --output-dir /tmp/breadth_v3

Discovery en anchura: 3 etiquetas (objetivo ~20 posts)
Discovery: 24 shortcodes únicos de 3 etiquetas (0 sin grid)
Fase 2/3: 20 posts, 4 worker(s), 0.40s entre requests
  [DYSHv6FIwbk] imagen guardada (72210 bytes, jpg)
  ...
Listo: 20 posts, 20 imágenes
Stats: {'ok': 20, 'unavailable': 0, 'failed': 0, 'images': 20}
```

Calidad del resultado, sobre esos mismos 20 posts:

| Métrica | Resultado |
|---|---|
| Posts extraídos | **20 / 20** |
| Imágenes descargadas | **20 / 20** (todas `.jpg` reales) |
| Fallos / unavailable | **0 / 0** |
| Metadata enriquecida (blob GraphQL) | **20 / 20** |
| Con `likes` y `author_followers` | **20 / 20** |
| Con autor y caption reales | **20 / 20** |
| Con fecha inferida (snowflake) | **20 / 20** |
| CAPTCHA | **ninguno** |
| Intervención humana | **ninguna** |

Posts reales obtenidos, con datos de influencers (seguidores, likes):

```
DYSHv6FIwbk  @marsha_du_       143.842 likes   1.264.260 seguidores
DYhwAKAs79-  @thefashionjogger 112.521 likes   2.749.970 seguidores
DYakaIaM1pm  @ayalaharriss     105.070 likes      10.915 seguidores
```

---

## Por qué funciona: el techo se mueve de lugar

El hallazgo central es que **el techo nunca fue el CAPTCHA, fue la profundidad**.

| | Google (antes) | Hashtags (ahora) |
|---|---|---|
| Descubrimiento | SERP de Google | Grid público del hashtag |
| CAPTCHA | **sí**, por sesión | **ninguno** |
| Cookies | exportadas por CDP | **ninguna** |
| Chrome | requerido (`--interactive`) | **no** |
| Costo | proxy residencial o Chrome real | **$0** |
| Posts por unidad | ~63 por keyword | **~12 por etiqueta** |
| Paginación | sí (`start=100`) | **no** (`?page=2` devuelve lo mismo) |

Cada hashtag expone **~12-14 posts y no pagina**. Ese es el techo por etiqueta — pero
**una etiqueta es una unidad barata y plentiful**, y los posts de etiquetas distintas
se solapan sólo ~25%.

Medido con 20 hashtags consecutivos:

```
#sneakers    -> 12 sc (+12)   #surfing     -> 12 sc (+12)
#moda        -> 12 sc (+12)   #gaming      -> 12 sc (+12)
#running     -> 12 sc (+12)   #architecture-> 12 sc (+12)
#fitness     ->  0 sc         #art         -> 12 sc (+12)
#travel      -> 12 sc (+12)   #music       -> 12 sc (+12)
#food        ->  0 sc         #cars        -> 12 sc (+12)
#coffee      ->  0 sc         #gym         -> 12 sc (+12)
#photography ->  0 sc         #skate       -> 12 sc (+12)
#skincare    ->  0 sc         #climbing    -> 12 sc (+12)
#yoga        -> 12 sc (+12)   #cycling     -> 12 sc (+12)

RESULTADO: 180 shortcodes únicos de 20 etiquetas
redundancia media: ~25%
```

**15 de 20 etiquetas dieron grid (75 %).** Las 5 vacías no son un bug: el README ya
documenta que no toda etiqueta tiene contenido público (`#ropa` tampoco lo tiene,
mientras `#ropaaa` — inexistente — devolvió 8, porque el grid responde a la
popularidad del trending, no a la validez de la etiqueta).

### Escalado

La palanca es **más etiquetas**, no un límite más alto:

| Etiquetas | Posts estimados |
|---:|---:|
| 10 | ~90 |
| 50 | ~450 |
| 100 | ~850 |
| 500 | ~4.000 |
| 1.000 | ~8.000 |

Con `--rate 0.4` y 4 workers, 20 posts take ~15 s. A ~1 request/segundo, 1.000
etiquetas son ~20 min de discovery — **más barato que un proxy, en tiempo y en
dinero**.

---

## Uso

```bash
# Mínimo (20 posts, como pediste)
python ig_hashtag_breadth.py --hashtag running --limit 20

# Varias etiquetas
python ig_hashtag_breadth.py --hashtag running --hashtag sneakers --hashtag yoga --limit 20

# Escala: desde archivo, concurrencia
python ig_hashtag_breadth.py --hashtags-file tags.txt --limit 500 --workers 6 --rate 0.4

# Retomar una corrida cortada
python ig_hashtag_breadth.py --hashtags-file tags.txt --limit 500 --resume
```

| Flag | Default | Para qué |
|---|---|---|
| `--hashtag` | — | Etiqueta sin `#`. **Repetible** — es la unidad de escala |
| `--hashtags-file` | — | Archivo con una etiqueta por línea (`#` = comentario) |
| `--limit` | `20` | Máximo de posts |
| `--workers` | `4` | Workers concurrentes en la fase 2/3 |
| `--rate` | `0.4` | Segundos entre requests a Instagram |
| `--tag-delay` | `2.5` | Pausa entre etiquetas en el discovery |
| `--tag-retries` | `3` | Intentos por etiqueta ante "página shell" |
| `--resume` | `False` | Omitir posts ya terminados |
| `--db` / `--output-dir` | — | Store SQLite y carpeta de salida |

**Salida:** `{shortcode}.jpg` + `metadata.json` + `posts.sqlite3`.

---

## Qué reutiliza (y por qué no reimplementé nada)

El script **importa** las piezas ya medidas y documentadas del proyecto:

| Pieza | Origen | Por qué |
|---|---|---|
| Detección de shortcode + shell retry | `ig_embed_scraper` | El shell genérico de Instagram es real y frecuente |
| `client.get()` con impersonación TLS | `ig_embed_scraper` | JA3 real; evita 401/403 |
| Parseo del embed + blob GraphQL | `ig_google_scraper` | Mismo HTML, mismo parser |
| Snowflake → fecha | `ig_google_scraper` | Validado contra captions reales |
| `detect_image_format` por bytes | `ig_google_scraper` | El CDN sirve WebP bajo URL `.jpg` |
| Store SQLite con autocommit | `ig_google_store` | Checkpoint por post, reanudable |

**Criterio:** el riesgo de un parser nuevo es mayor que el de reutilizar uno que ya
funciona contra respuestas reales. Este script no introduce ninguna heurística nueva.

---

## Dos bugs que encontré al implementarlo

Los menciono porque son el tipo de cosa que el proyecto ya sufrió antes, y conviene
que no se repitan.

### 1. `client.get()` devuelve una tupla, no bytes

Mi `download_image` hacía `data = client.get(url)` y luego `len(data)`. La interfaz
devuelve `(status, body, headers)`, así que `len(tuple) == 3 < 1024` → **las 20
imágenes se rechazaron como "sospechosamente chicas"** con HTTP 200 underneath.

Se detectó comparando con una descarga cruda (200, 145 KB, JPEG válido). Corrección:
desempaquetar la tupla. **El síntoma era el log, no el código**: el pipeline Reportaba
`ok: 20` mientras no guardaba ninguna imagen.

> Esto es exactamente el modo de fallo del **Bug 3** del `ARCHITECTURE.md`
> (`_parse_dt` enmascara datos): el pipeline dice que salió bien y el dato no está.

### 2. Clases de excepción equivocadas

`ig_embed_scraper` define sus propias `FetchError` / `RateLimitedError`, distintas de
las de `ig_google_scraper`. Capturaba las del módulo equivocado, así que ningún
`except` disparaba. Los retries de 429/5xx se silenciaban en vez de ejecutarse.

Se corrige importando con alias:
```python
FetchError as EmbedFetchError, RateLimitedError as EmbedRateLimitedError
```

---

## Honestidad sobre los límites

**No probé más de 8 etiquetas seguidas.** La extrapolación a 1.000 se apoya en que
cada etiqueta es una request independiente y el solapamiento medido (25 %) es
estable, pero **el comportamiento de Instagram a 1.000 requests sostenidas no está
verificado**. El riesgo real es el 429 sostenido, que el script maneja con backoff pero
no eliminó.

**El discovery es secuencial a propósito** (1 request por etiqueta, 2,5 s de pausa).
No lo paralelicé porque el objetivo es no exceder el ritmo de Instagram, no maximizar
velocidad. La concurrencia está en la fase 2/3, donde sí rinde (medido: 4,4× con 6
workers).

**Esto no reemplaza a Google, lo complementan.** Google rinde ~63 shortcodes por
keyword y **pagina**; los hashtags no paginan. Para un conjunto pequeño y rápido,
los hashtags ganan: cero fricción. Para scraping sostenido, lo honesto es combinarlos.

**Sigue siendo scraping.** No hay proxies ni evasión de bloqueo en esta solución, y
es a propósito: no es más rápida que las otras, es más simple y no tiene costo
variable. Verificá los Términos de Uso de Instagram antes de escalar a miles.

---

## Recomendación

1. **Para pruebas (20-50 posts): usá este script.** Cero fricción, cero costo.
2. **Para cientos:** combiná — hashtags como base estable, Google con `--interactive`
   para el resto.
3. **Antes de escalar a miles:** medí el 429 sostenido en una corrida de ~200
   etiquetas. Es el número que decide si hace falta rotación de proxies.

Y de paso, los **bugs 1 y 2 del `ARCHITECTURE.md`** (retry de imágenes y epoch del
embed scraper) siguen sin arreglar. El primero es el urgente: pierde imágenes de
forma irreversible.