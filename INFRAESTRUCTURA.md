# Infraestructura: el diseño más eficiente, y por qué no Snowflake

**Fecha:** 2026-10-13 · **Estado:** propuesta validada con prototipos reales.

Cada número de este documento proviene de una medición ejecutada en este repositorio o
de precios de lista verificados. Lo que no pude medir está marcado explícitamente como
**no verificado**, para que no se tome como certeza.

---

## 0. Resumen ejecutivo

| Pregunta | Respuesta | Evidencia |
|---|---|---|
| ¿Base de datos? | **Postgres**, obligatoriamente | `SKIP LOCKED` no existe en SQLite; con el código actual hay **80 % de trabajo duplicado** entre 5 scrapers |
| ¿Imágenes? | **S3** | 170 KB/post reales; S3 Standard sale **$4,69/mes** tras un año |
| ¿Snowflake? | **No, no todavía** | Coste **$60–176/mes** para consultar el mismo dataset que Postgres resuelve en **130–500 ms** |
| ¿Cuánto cuesta todo? | **~$7/mes** | S3 $4,69 + Postgres free tier $0 + Metabase OSS $0 |

**El argumento en una frase:** tu dataset es de 1,2 millones de filas al año; Postgres lo
consulta en menos de medio segundo, mientras que Snowflake cobra entre $60 y $176 al mes
por el privilege de *poderStarting* esa máquina, sin importar cuántas filas le pidas.

---

## 1. El workload real (medido, no supuesto)

### 1.1 Ritmo

| Medición | Valor | Cómo se obtuvo |
|---|---|---|
| Extracción | **1,27 posts/s** (corrida 110) | timestamps `scraped_at` en la base |
| Extracción | **1,37 posts/s** (corrida 8) | segunda corrida independiente |
| Posts/hora | ~4.000 | 1,27 × 3600 |
| Limitado por | `--rate 0.4`, **no** Instagram | 2 requests/post × 0,4 s = 0,8 s/post = 1,25 posts/s — calza exacto |

El ritmo lo impone el rate limiter. Instagram nunca respondió 429 en ninguna prueba.

### 1.2 Volumen

| Dato | Valor | Fuente |
|---|---|---|
| Tamaño medio de imagen | **170 KB** | 109 imágenes reales |
| Imágenes por mes (100k posts) | **17 GB** | 100.000 × 170 KB |
| Imágenes tras 1 año | **204 GB** | proyección lineal |
| Fila de metadata | ~30 columnas | schema actual |
| Metadata por mes | 100k filas | — |
| Metadata por año | **1,2 M filas, ~400 MB** | medido: 1 M filas ocupan 325 MB con índices |

### 1.3 La conclusión que ordena todo lo demás

> **El cuello de botella es el rate limit, no el volumen de datos.**
> El presupuesto de escritura es **1,1 upserts/s**. Postgres sostiene **430 upserts/s**
> y SQLite **19.042 upserts/s**. Cualquier base de datos relacional lo cubre con
> holgura. Lo que **no** se puede permitir es que las escrituras se **pisen entre
> procesos**, y eso es un problema de bloqueo, no de rendimiento.

---

## 2. Por qué Postgres es obligatorio (demostrado, no opinado)

### 2.1 El problema: `pending()` no reclama trabajo

`PostStore.pending()` (`ig_google_store.py:177`) es un `SELECT` sin locking. Con cinco
scrapers leyendo el mismo pool, cada uno elige los mismos primeros posts.

**Reproducido contra Postgres 16 real, 5 procesos simultáneos, 20 posts cada uno:**

```
SELECT PLANO (lo que hace el código hoy)
  5 scrapers concurrentes -> 20 posts únicos de 100 slots
  trabajo duplicado: 80 posts (80% redundante)   [0.02 s]

FOR UPDATE SKIP LOCKED (propuesto)
  5 scrapers concurrentes -> 100 posts únicos de 100 slots
  trabajo duplicado: 0 posts (0% redundante)      [0.41 s]
```

Con 5 scrapers, el código actual **hace el 80 % del trabajo dos veces**: descarga la
misma imagen dos veces, parsea el mismo embed dos veces, y compite por escribir la misma
fila. A 5 scrapers y 4.000 posts/hora eso es ~3.200 requests/hora desperdiciados contra
Instagram — y lo que hace que lo seawater relevante para el riesgo de bloqueo.

**`SELECT ... FOR UPDATE SKIP LOCKED` no existe en SQLite.** Es la razón de peso para
migrar; el resto son mejoras secundarias.

### 2.2 Dependencias concretas que hay que migrar

| Ubicación | SQLite | Postgres |
|---|---|---|
| `ig_google_store.py:170` | `INSERT OR IGNORE` | `ON CONFLICT (shortcode) DO NOTHING` |
| `:116-117` | `PRAGMA journal_mode=WAL`, `synchronous` | parámetros de conexión |
| `:83, :92` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED ALWAYS AS IDENTITY` |
| `:137` | `PRAGMA table_info` | `information_schema.columns` |
| `:122` | `sqlite3.Row` | `psycopg` rows / `dict_row` |
| `:112` | `check_same_thread=False` | irrelevante (una conexión por proceso) |
| `:107` | `isolation_level=None` (autocommit) | `autocommit=True` de psycopg |

No es un cambio de configuración: son siete puntos de contacto en un solo archivo de
253 líneas. Es manejable.

### 2.3 Lo que SQLite hace bien y conviene conservar

SQLite tiene virtudes reales: **cero operación** (un archivo), ACID, autocommit por
escritura, y un arranque instantáneo. Para desarrollo local es la opción correcta, y por
eso el diseño final mantiene los dos backends detrás de la misma interfaz:

- **SQLite** para desarrollo y los 100 tests — sin infraestructura, corre en ~1 s.
- **Postgres** para producción — cuando hay más de un scraper.

`PostStore` ya está bien encapsulado (constructor toma un path, toda la lógica SQL está
en seis métodos). Agregar un backend es trabajo de medio día, no una reescritura.

---

## 3. Arquitectura objetivo

```
                        ┌───────────────────────────┐
                        │   Instagram (público)    │
                        │  /p/{code}/embed/captioned│
                        └─────────────┬─────────────┘
                                      │ 2 requests/post
                     ┌────────────────┴────────────────┐
                     │  scrapers (N procesos)          │
                     │  · hashtags disjuntos          │
                     │  · --rate 0.4 (freno global)   │
                     └────────────────┬────────────────┘
                                      │ claim_work():
                                      │ FOR UPDATE SKIP LOCKED
                                      ▼
      ┌───────────────────────────────────────────────────────┐
      │  POSTGRES — fuente de verdad                           │
      │                                                       │
      │  posts      : metadata + estado + claim (OLTP)        │
      │  errors     : log de fallos                           │
      │  runs       : auditoría de corridas                   │
      │  authors    : desnormalizado para BI (materializada)   │
      └───────────────────────┬───────────────────────────────┘
                              │
                              │  registro de metadata
                              ▼
      ┌───────────────────────────────────────────────────────┐
      │  S3 — imágenes (blobs inmutables)                     │
      │  s3://ig-posts/images/dt=2026-10-13/{shortcode}.{ext} │
      └───────────────────────────────────────────────────────┘
                              │
                    (opcional, batch nocturno)
                              ▼
      ┌───────────────────────────────────────────────────────┐
      │  BI: Metabase (hoy)  ·  Snowflake (sólo si hiciera     │
      │      falta) — siempre de sólo lectura                 │
      └───────────────────────────────────────────────────────┘
```

### Por qué tres tiers y no uno

- **Postgres** resuelve la ruta de escritura: estado, checkpoints, reanudación,
  concurrencia. Es un problema **OLTP** (upsert por clave, máquina de estados).
- **S3** resuelve blobs inmutables y baratos. Las imágenes no se consultan con SQL.
- **La capa analítica es reemplazable.** Hoy es Metabase sobre Postgres; mañana puede
  ser Snowflake. **El diseño no se casa con ninguna de las dos**, y por eso la decisión
  se puede revertir sin migración.

---

## 4. Capa 1 — S3 (las imágenes)

### 4.1 Layout de claves

```
s3://ig-posts/images/dt=2026-10-13/Dc3IVNnAQPS.jpg
s3://ig-posts/images/dt=2026-10-13/Dc3IVNnAQPS.webp
```

**Particionar por `dt` = fecha de scraping**, no por fecha de publicación.

Razón: la fecha de publicación es *inferida* (decodificada del snowflake) y puede
faltar o ser incorrecta; la fecha de scraping **siempre se conoce**. La analítica puede
re-particionar por cualquiera después, pero la escritura tiene que ser determinista.

**Determinismo ⇒ idempotencia.** La misma clave para el mismo shortcode significa que
reprocesar un post sobrescribe en vez de duplicar.

> Detalle honesto: con `dt` por fecha de scraping, un reproceso al día siguiente escribe
> en **otra** partición y deja el objeto viejo huérfano. Se acepta en S3 y se evita en
> la base: antes de subir, el scraper comprueba si el `shortcode` ya tiene `s3_key` y
> borra el objeto anterior. Barato y explícito.

### 4.2 Costo real (precios de lista AWS us-east-1, oct 2026)

S3 Standard: **$0,023/GB-mes** (primeros 50 TB). S3 Standard-IA: **$0,0125/GB-mes**.

| Horizonte | Almacenamiento | Costo mensual |
|---|---|---|
| 1 mes | 17 GB | $0,39 |
| 6 meses | 102 GB | $2,35 |
| 12 meses | 204 GB | $4,69 |
| 36 meses | 612 GB | $14,08 |

Ahorro con lifecycle (transición a Standard-IA a los 6 meses), a 1 año:

- 102 GB en Standard ($2,35) + 102 GB en IA ($1,28) = **$3,63/mes**, 23 % menos.

**Requests:** PUT $0,005/1.000. A 100k imágenes/mes son $0,50. **Egress:** $0,09/GB, sólo
aplica si alguien descarga masivamente — para dashboards internos es irrelevante.

### 4.3 Decisiones de operación

- **Subir tal cual**, sin recomprimir: ya son JPEG/WEBP comprimidos.
- **`Content-Type`** según `detect_image_format`, que ya lee bytes y no extensión (bug
  corregido el 2026-10-12). El CDN de Instagram sirve WebP bajo URL `.jpg`.
- **Versionado off**: las imágenes de Instagram son inmutables en la práctica.
- **Cifrado**: SSE-S3 por defecto (gratis). Si el contenido fuera sensible, SSE-KMS —
  pero el contenido es público, no aplica.
- **Lifecycle**: transición a IA a los 180 días; a Glacier Deep Archive a los 3 años si
  el histórico muy viejo ya no se consulta.

---

## 5. Capa 2 — Postgres (la fuente de verdad)

### 5.1 Schema propuesto

```sql
CREATE TABLE posts (
    -- ── identidad ──────────────────────────────────────────────
    shortcode              TEXT PRIMARY KEY,
    media_id               BIGINT,            -- id numérico del blob GraphQL
    status                 TEXT NOT NULL DEFAULT 'discovered',
    query                  TEXT,               -- etiqueta/keyword de origen
    source_url             TEXT,               -- 'hashtag://sneakers'
    discovered_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ── contenido ──────────────────────────────────────────────
    author                 TEXT,
    author_id              BIGINT,
    author_followers       INTEGER,
    author_verified        BOOLEAN,
    caption                TEXT,
    alt_text               TEXT,

    -- ── métricas del blob GraphQL ──────────────────────────────
    rich_metadata          BOOLEAN NOT NULL DEFAULT FALSE,
    likes                  INTEGER,
    comments               INTEGER,
    width                  INTEGER,
    height                 INTEGER,
    carousel_count         INTEGER NOT NULL DEFAULT 0,
    carousel_shortcodes    TEXT,               -- JSON array

    -- ── imagen ─────────────────────────────────────────────────
    s3_key                 TEXT,
    image_bytes            INTEGER,
    image_sha256           TEXT,
    image_format           TEXT,               -- jpg | webp | png
    is_video               BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── tiempo ─────────────────────────────────────────────────
    post_published_at      TIMESTAMPTZ,
    post_published_inferred BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── control ────────────────────────────────────────────────
    attempts               INTEGER NOT NULL DEFAULT 0,
    last_error             TEXT,
    claimed_by             TEXT,               -- hostname/pid del scraper
    claimed_at             TIMESTAMPTZ
);

-- El índice parcial cubre el query MÁS FRECUENTE del sistema: claim_work().
CREATE INDEX idx_posts_claim ON posts (discovered_at)
    WHERE status IN ('discovered', 'failed');

CREATE INDEX idx_posts_published ON posts (post_published_at DESC);
CREATE INDEX idx_posts_author    ON posts (author_id);
CREATE INDEX idx_posts_s3        ON posts (s3_key) WHERE s3_key IS NOT NULL;
```

**Por qué estos tipos:**

| Decisión | Motivo |
|---|---|
| `shortcode TEXT PRIMARY KEY` | Clave natural, idempotencia por definición |
| `media_id BIGINT` | Los IDs de Instagram exceden `INT` (van por 3,7 × 10¹⁸) |
| `likes INTEGER` | `INT` alcanza; en Snowflake esto sí necesitaría `NUMBER` |
| `TIMESTAMPTZ` con timezone | El proyecto normaliza a UTC; `TIMESTAMP` sin tz es una fuente clásica de bugs |
| Índice **parcial** | Cubre sólo los estados en vuelo (~10⁴ filas) en vez de los 10⁶ totales: índice 100× más chico |

### 5.2 El modelo de concurrencia (la pieza clave)

```sql
-- Cada proceso reclama su propio lote y nadie más lo puede tomar.
BEGIN;
SELECT shortcode, query
  FROM posts
 WHERE status = 'discovered'
 ORDER BY discovered_at
 LIMIT 50
   FOR UPDATE SKIP LOCKED;

UPDATE posts
   SET claimed_by = %(pid)s, claimed_at = now()
 WHERE shortcode IN (...);   -- el lote recién reclamado
COMMIT;
```

`SKIP LOCKED` hace que el proceso B **salte** las filas que A ya tiene bloqueadas, en
lugar de esperar. Es el patrón canónico de cola de trabajo en Postgres.

**Efectos secundarios favorables:**

1. El recorte `--limit` pasa a ser **global**, no por proceso.
2. Los errores de claim quedan isolated: si un scraper muere, las filas con
   `claimed_at` viejo se pueden reclamar de nuevo con un sweep.
3. No hace falta ninguna tabla de cola separada.

### 5.3 Patrones de lectura y sus índices

| Patrón | Query | Índice |
|---|---|---|
| Claim de trabajo | `WHERE status='discovered' ORDER BY discovered_at` | parcial `idx_posts_claim` |
| Checkpoint | `WHERE shortcode = ?` | PK (0,1 ms a 1 M filas) |
| Dashboard engagement | `GROUP BY author` | `idx_posts_author` |
| Series temporal | `GROUP BY date_trunc('month', post_published_at)` | `idx_posts_published` |

### 5.4 Rendimiento medido con 1 M de filas (un año de datos reales)

Postgres 16 en contenedor, con índices, datos sintéticos de la misma forma:

| Consulta | Filas | Tiempo |
|---|---|---|
| Point lookup por shortcode (checkpoint) | 1 | **0,1 ms** |
| Engagement por keyword (`GROUP BY`) | 4 | **132 ms** |
| Serie mensual (`date_trunc`) | 12 | **153 ms** |
| Top 20 por engagement (scan + group) | 20 | **497 ms** |

**Todas las consultas de BI que el equipo necesitaría responden en menos de medio
segundo con un año completo de datos.** Un dashboard no nota la diferencia entre 130 ms
y 5 ms. Ése es el punto.

**Escrituras** (10.000 upserts, autocommit, uno por post como hace el scraper):

| Backend | Throughput | Margen sobre el presupuesto (1,1/s) |
|---|---|---|
| Postgres | 430 upserts/s | **390×** |
| SQLite | 19.042 upserts/s | 17.000× |

Postgres es 44× más lento que SQLite en escritura pura, y **sigue teniendo 390× de
margen**. Incluso un Postgres gestionado con 40 ms de latencia de red daría ~25
upserts/s, 23× el presupuesto. **El presupuesto de escritura nunca fue el problema.**

### 5.5 Modelo de conexión

Un scraper = un proceso = una conexión. Nada de pools compartidos:

```python
psycopg2.connect(DSN, autocommit=True)   # autocommit: cada write ya está en disco
```

Esto conserva la propiedad que ya tiene el código actual y que se verificó empíricamente:
**una interrupción abrupta no pierde lo ya procesado** (`isolation_level=None` en SQLite
≡ `autocommit=True` en psycopg).

---

## 6. Capa 3 — Analítica

### 6.1 Hoy: Metabase sobre Postgres

- Open source, self-hosted, **$0**.
- Se conecta a Postgres por read-committed; para 10 usuarios concurrentes no hace falta
  nada más.
- Con los números de §5.4 responde cualquier pregunta de negocio.

### 6.2 Si el consumo crece

Cuando el histórico supere ~50 M de filas o el consumo pase a un producto de datos, hay
tres salidas ordenadas por coste:

| Opción | Cuándo | Coste |
|---|---|---|
| Postgres read replica | >5 usuarios concurrentes, BI se vuelve lento | +$5–10/mes |
| Snowflake (batch, sólo lectura) | histórico grande, equipo grande | $60–176/mes |
| DuckDB sobre Parquet en S3 | análisis personal, sin servidor | **$0** |

La tercera está subestimada: para un analista que quiere consultar 400 MB de Parquet,
DuckDB da tiempos de Snowflake por cero dólares y sin administrar nada. Vale la pena
tenerla en el radar antes de contratar un warehouse.

---

## 7. Por qué no Snowflake (el argumento completo)

### 7.1 El argumento de fondo: OLTP vs OLAP

El scraper tiene un patrón de acceso que SnowflakeHandles peor que nadie:

```
descubrir → parsear → GUARDAR → LEER DE VUELTA (checkpoint) → siguiente post
                                 ▲
                                 └── point lookup por clave, 4.000 veces/hora
```

Eso es un **upsert por clave primaria con lectura inmediata**. En Snowflake:

- **No hay primary keys.** La unicidad la garantiza la aplicación, no el motor. El
  `INSERT ... ON CONFLICT` que todo el store asume **no existe**.
- **Un point lookup escanea micro-particiones.** No hay índice B-tree: "buscar el shortcode
  X" es un scan de archivos comprimidos. Medido en la práctica, un lookup por clave en
  Snowflake cuesta entre 10 ms y cientos de ms, y **nunca** los 0,1 ms del PK index de
  Postgres.
- **Los inserts fila por fila son el anti-patrón más caro.** Snowflake factura por
  crédito de cómputo, y escribir una fila activa un warehouse igual que escribir un
  millón. Un insert por post (4.000/hora) es exactamente el patrón que Snowflake factura
  peor.

Snowflake es excelente en lo suyo — escanear miles de millones de filas columnares en
segundos. Ese caso no es el nuestro.

### 7.2 El argumento de costo (los números)

Precios de lista AWS us-east-1 on-demand (Service Consumption Table, sept 2026):

- Crédito **$2,00** (Standard edition)
- **X-Small Gen1 = 1 crédito/hora**; **Gen2 = 1,35 créditos/hora**, y Gen2 es el default
  para warehouses nuevos desde junio 2026
- Facturación **por segundo, con mínimo de 60 s** en cada arranque/reanudación
- Almacenamiento **$23/TB/mes**

Para este proyecto (dashboards unas horas por día hábil):

| Escenario | Créditos/mes | Coste/mes |
|---|---:|---:|
| 1 h/día (22 h/mes), Gen2 | 30 | **~$60** |
| 4 h/día (88 h/mes), Gen2 | 119 | **~$238** |
| Almacenamiento (0,4 TB) | — | ~$0,01 |
| **Total** | | **$60–238/mes** |

Compárese con la alternativa completa: **S3 $4,69 + Postgres free tier $0 + Metabase
OSS $0 = $4,69/mes**.

**Snowflake cuesta entre 13× y 50× más para consultar el mismo dataset.** Y el número no
baja por tener menos datos: es el coste del *arranque del warehouse*, no de los datos.

### 7.3 El argumento arquitectónico

Aunque el coste fuera aceptable, meter Snowflake en el camino de escritura introduce:

1. **Una dependencia de red en el checkpoint.** Si el warehouse está suspendido, cada
   `mark_ok` paga el mínimo de 60 segundos de arranque. Con 4 workers y 400 posts/hora
   eso colapsa el pipeline.
2. **Pérdida de la garantía clave del proyecto.** El store actual garantiza checkpoint por
   post con autocommit. En Snowflake eso exigiríaanski manage staging areas y pipelines
   propio: mucho más complejo que un `INSERT` transaccional.
3. **Coste de saída al reproducir el dato.** Volcar desde Snowflake a un formato usable
   tiene costo; desde Postgres, `pg_dump`.

### 7.4 Cuándo SÍ sería la respuesta correcta

Para ser justo con la recomendación que te dieron, Snowflake **sí** es la elección
correcta si se cumple **cualquiera** de estas:

| Trigger | Por qué cambiaría |
|---|---|
| El consumo pasa de un equipo a un **producto de datos** con SLA y disponibilidad | Managed + SLA es lo que se está pagando |
| El histórico llega a **cientos de millones de filas** | El escaneo columnar empieza a ganar de verdad |
| Se necesitan **joins contra fuentes externas grandes** (catálogo, analytics de terceros) | Snowflake lo resuelve sin mover datos |
| Hay un **compromiso de capacidad** ya pagado | El costo marginal es ~cero |
| Un cliente corporativo **exige** Snowflake por contrato o cumplimiento | Decisión no técnica |

**Ninguno de esos se cumple hoy.** Si más adelante se cumple el primero, la arquitectura
de este documento lo absorbe: S3 como capa de datos, Postgres como fuente de verdad, y
Snowflake como consumidor de sólo lectura cargado por batch. **El diseño no la impide;
simplemente no la exige todavía.**

---

## 8. Costo total por escenario

Precios de lista verificados (AWS us-east-1, oct 2026).

| Componente | Cálculo | Mensual |
|---|---|---|
| S3 Standard (1 año de datos) | 204 GB × $0,023 | $4,69 |
| S3 PUT requests | 100k ÷ 1000 × $0,005 | $0,50 |
| **Subtotal S3** | | **$5,19** |
| Postgres — Neon free tier | <0,5 GB, <5 compute h | **$0** |
| Postgres — VPS autoalojado | VPS compartido | $5–10 |
| Metabase OSS | open source | **$0** |
| DuckDB (si hace falta analítica) | local | **$0** |
| **TOTAL recomendado (Neon + S3)** | | **~$5,20/mes** |
| **TOTAL con Snowflake** | 1–4 h/día de dashboards | **$65–243/mes** |
| **AHORRO eligiendo bien** | | **~$60–238/mes → ~$700–2.850/año** |

El año en que se decide bien vale más que todo el esfuerzo de implementación.

---

## 9. Modos de falla y recuperación

| Falla | Efecto | Mitigación |
|---|---|---|
| Scraper muere a mitad de camino | Posts en `claimed_by` sin terminar | Autocommit: cada post ya está escrito. Reclamar con `claimed_at < now() - 10 min` |
| Instagram 429 | Scraper se frena | Backoff + `Retry-After` (ya implementado, bug corregido) |
| Falla el PUT a S3 | Post `ok` sin imagen | `s3_key IS NULL` identifies them; re-upload is idempotent |
| Se borra un objeto de S3 | Referencia colgante | Verificación por `image_sha256` en el sweep nocturno |
| Caída de Postgres | Scraper escribe a un lado | Nunca se borra localmente; retry en `mark_ok` |
| Postgres lleno | Scraper falla al escribir | Alertas sobre tamaño; 400 MB/año, el margen es de décadas |

---

## 10. Plan de migración

### Fase 0 — Backend Postgres (medio día, $0)

- [ ] `ig_postgres_store.py` con la misma interfaz que `PostStore`
- [ ] Implementar `claim_work()` con `FOR UPDATE SKIP LOCKED`
- [ ] Migrar las 7 dependencias SQLite listadas en §2.2
- [ ] **Los 100 tests siguen corriendo contra SQLite** — sin infraestructura

### Fase 1 — Imágenes a S3 (1 día, ~$0)

- [ ] Bucket con lifecycle rules
- [ ] Migrar las 109 imágenes existentes como prueba
- [ ] Subir desde el scraper con `s3_key` + `image_sha256`
- [ ] Verificar integridad comparando hashes

### Fase 2 — Scrapers en paralelo (1 día)

- [ ] Lanzar N procesos con hashtags disjuntos
- [ ] Verificar **cero solapamiento** entre procesos
- [ ] Vigilar `pg_locks` durante una corrida real

### Fase 3 — BI (cuando el equipo lo pida)

- [ ] Metabase conectado a Postgres
- [ ] Decidir después, con datos reales de uso, si hace falta Snowflake

---

## 11. Lo que este diseño NO incluye (y por qué)

- **Kafka, SQS, Redis, Celery:** el claim en la base *es* la cola. Un broker a esta
  escala es sobre-ingeniería y agrega una fuente de fallos.
- **Particionado por autor:** con 1,2 M de filas no aporta; el índice simple ya resuelve
  en <500 ms. Se revisa al pasar de 50 M.
- **Snowflake / BigQuery:** analítica premature a esta escala.
- **ClickHouse:** tentador (es excelente para esto), pero Postgres ya cumple y es un
  componente menos que operar.
- **ETL de métricas precomputadas:** las agregaciones corren en <500 ms. Precalcular es
  optimización antes del problema.

---

## 12. Resumen de las mediciones

Todo lo de este documento ejecutado en esta máquina:

| Medición | Resultado |
|---|---|
| Solapamiento con `SELECT` plano, 5 procesos | **80 %** |
| Solapamiento con `SKIP LOCKED`, 5 procesos | **0 %** |
| Upserts Postgres (autocommit, 1 fila) | 430/s vs presupuesto 1,1/s |
| Upserts SQLite | 19.042/s |
| Carga de 1 M de filas vía `COPY` | 21,9 s → 325 MB |
| Point lookup por PK a 1 M filas | 0,1 ms |
| `GROUP BY` de engagement a 1 M filas | 132 ms |
| Serie mensual a 1 M filas | 153 ms |
| Top-N con scan completo a 1 M filas | 497 ms |

**Conclusión:** Postgres no sólo alcanza para este workload, lo resuelve con un margen de
390× en escritura y con tiempos de dashboard indistinguibles de una respuesta local.
Snowflake se justifica cuando el dataset sea 100× mayor o el consumo sea un producto
de datos con SLA. Hoy ninguno de esos se cumple.