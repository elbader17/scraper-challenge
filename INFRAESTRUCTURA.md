# Infrastructure: Postgres + S3, and where Snowflake fits (or doesn't)

**Date:** 2026-10-13 · **Status:** proposal, pending the cost decision.

---

## Verdict in one line

**Postgres as the source of truth, S3 for the images, and Snowflake probably not yet.**
At 100k posts/month the dataset is 1.2M rows per year: Postgres handles it in
milliseconds for a fraction of the cost. Snowflake only earns its place if consumption
is shared BI at a much larger volume.

---

## 1. The workload we actually have (measured, not assumed)

| Data | Value | Source |
|---|---|---|
| Extraction rate | ~4,000 posts/hour | 2 runs: 1.27 and 1.37 posts/s |
| Rate limited by | `--rate 0.4` (2 requests/post), not Instagram | 0.8 s/post = 1.25 posts/s exactly |
| Average image size | **170 KB** | 109 real images |
| Image volume | **17 GB/month**, ~3.4 TB/year | 100k × 170 KB |
| Metadata | ~30 columns × 100k = **1.2M rows/year** | current schema |
| Unique authors per batch | ~100 out of 110 posts | 110-post run |
| Posts with enriched metadata | 109/110 (99%) | 110-post run |

**The bottleneck is the rate limit, not data volume.** That matters for sizing: we do
not need compute power, we need writes that do not collide between scrapers.

---

## 2. Why Postgres is mandatory (not a preference)

The requirement of **several scraper processes in parallel** rules out SQLite for
concrete reasons, demonstrated in this repo.

### 2.1 The current store does not claim work

`PostStore.pending()` is a plain `SELECT` with no locking. With two processes reading
the same pool:

```
pending posts: 20
process-A: P00000001..P00000005
process-B: P00000001..P00000005
>> POSTS BOTH PROCESSES WILL EXTRACT: 5
```

Both extract the same five, download the same images twice, and race to write. With
five scrapers this multiplies.

The standard fix is `SELECT ... FOR UPDATE SKIP LOCKED`, which **SQLite does not
support**. That is the decisive reason to migrate.

### 2.2 Concrete SQLite dependencies

Migrating is real work, not a config change:

| Location | SQLite | Postgres |
|---|---|---|
| `ig_google_store.py:170` | `INSERT OR IGNORE` | `ON CONFLICT (shortcode) DO NOTHING` |
| `:116-117` | `PRAGMA journal_mode=WAL`, `synchronous` | connection parameters |
| `:83, :92` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED ALWAYS AS IDENTITY` |
| `:137` | `PRAGMA table_info` | `information_schema.columns` |
| `:122` | `sqlite3.Row` | psycopg rows |
| `:112` | `check_same_thread=False` | irrelevant (one connection per process) |

### 2.3 What SQLite does get right

SQLite has genuine virtues worth keeping: **zero operations** (a single file), ACID
transactions, and per-write autocommit. For a single scraper it is the right choice.
The problem is exclusively multi-process concurrency.

> **Recommendation:** keep the SQLite backend for local development and tests (the 100
> checks keep running with no infrastructure), and add a Postgres backend behind the
> same interface. `PostStore` is already well encapsulated for that.

---

## 3. Target architecture

```
   ┌──────────────┐   GET /p/{code}/embed/captioned/
   │  Instagram   │◄──────────────────────────────┐
   └──────────────┘                               │
                                                  │  2 requests/post
   ┌──────────────┐                               │
   │  scraper #1  │───┐                           │
   │  scraper #2  │───┤                           │
   │  scraper #N  │───┤  SELECT … FOR UPDATE      │
   └──────────────┘   │  SKIP LOCKED              │
                      ▼                           │
            ┌─────────────────────┐               │
            │  POSTGRES           │               │
            │  source of truth    │               │
            │  · status/claims    │               │
            │  · metadata (30 col)│               │
            └──────────┬──────────┘               │
                       │                           │
                       ▼                           │
            ┌─────────────────────┐               │
            │  S3                 │◄──────────────┘
            │  images             │   PUT 170 KB/post
            │  dt=YYYY-MM-DD/     │
            └──────────┬──────────┘
                       │ (optional, batch)
                       ▼
            ┌─────────────────────┐
            │  Snowflake / BI     │
            │  read-only          │
            └─────────────────────┘
```

### Why three tiers and not one

- **Postgres** owns the write path: status, checkpoints, resume, concurrency. It is an
  OLTP problem (upsert by key, state machine).
- **S3** owns immutable, cheap blobs. Images are not queried with SQL.
- **The analytics tier is replaceable.** Today it can be Metabase on Postgres; tomorrow
  Snowflake. The design commits to neither.

---

## 4. S3 design

### Key layout

```
s3://ig-posts/images/dt=2026-10-13/Dc3IVNnAQPS.jpg
s3://ig-posts/images/dt=2026-10-13/Dc3IVNnAQPS.webp
```

**Partition by `dt` = scrape date**, not by publication date. Reason: the publication
date is *inferred* (decoded from the snowflake) and may be missing or wrong; the
scrape date is always known. Analytics can partition by either afterwards, but writes
have to be deterministic.

**Determinism gives idempotency.** The same key for the same shortcode means
reprocessing a post overwrites instead of duplicating. With `dt` by scrape date, a
reprocess the next day would write to another partition: that duplicate is accepted in
S3 but prevented by a condition in the database (see §5.3).

### Considerations

- **Compression:** the images are already compressed JPEG/WEBP. Recompressing gains
  nothing. Upload as-is.
- **Content-Type:** `image/jpeg` / `image/webp` as detected by `detect_image_format`
  (which reads bytes, not the extension — already fixed).
- **Lifecycle:** after a few months, transition to an infrequent-access class. The raw
  material is never needed again.
- **Versioning:** optional. Instagram images are effectively immutable.

---

## 5. Postgres schema changes

### 5.1 New columns

```sql
-- Store the object key instead of a local path.
s3_key           TEXT,         -- 'images/dt=2026-10-13/Dc3IVNnAQPS.jpg'
image_bytes      INTEGER,      -- real size
image_sha256     TEXT,         -- dedupe and integrity checks
discovered_at    TIMESTAMPTZ,  -- when discovery first saw it
updated_at       TIMESTAMPTZ
```

`local_image_path` can stay nullable for local/dev mode.

### 5.2 Indexes for the real access pattern

```sql
CREATE INDEX idx_posts_pending ON posts (status, discovered_at)
    WHERE status IN ('discovered', 'failed');   -- partial index
CREATE INDEX idx_posts_published ON posts (post_published_at DESC);
CREATE INDEX idx_posts_author ON posts (author_id);
```

The partial index over in-flight states is small and covers the most frequent query
(`pending()`).

### 5.3 Work claiming between processes

```sql
-- Instead of a plain SELECT, each process claims its own rows.
SELECT shortcode, query FROM posts
WHERE status = 'discovered'
ORDER BY discovered_at
FOR UPDATE SKIP LOCKED
LIMIT 50;
```

Each process gets a disjoint batch. `SKIP LOCKED` makes the second process skip the
rows the first already took instead of waiting. It is the canonical work-queue pattern
in Postgres.

**This also fixes `--limit`:** today the trim `pendientes[:args.limit]` is per-process;
with the claim in the database, the limit becomes global.

---

## 6. Snowflake evaluation

### The honest case

**For:**
- Managed: no servers to operate.
- Native BI integrations (Looker, Tableau, dbt).
- Reads Parquet straight from S3, which fits well if the images live there anyway.
- For a team that wants shared dashboards without running infrastructure, it is
  convenient.

**Against (decisive at this scale):**
- 1.2M rows/year is trivial for Postgres. Snowflake shines with billions.
- Snowflake bills by **compute credit**, and even the smallest warehouse (X-Small) is
  billed per hour of *activity* — even if the query returns 10 rows.
- As a system of record it is **worse**: no primary keys, point lookups scan
  micro-partitions, and the pattern "insert one row, then read it back" (exactly what
  the scraper does at checkpoint time) is its most expensive anti-pattern.

### Verdict

> **Do not use Snowflake as the scraper's database.** Not now, and not at the volume
> being discussed. If the team adopts it later for BI, it must be a **read-only
> consumer** loaded in batch from Postgres or from Parquet in S3 — never on the
> critical write path.

### When the recommendation would change

Reconsider when **any** of these hold:
- Consumption shifts from 10 people to a data product with an SLA.
- History grows to hundreds of millions of rows.
- Joins against large external sources get added, which cannot be moved out.

---

## 7. Costs



Verified list prices, AWS us-east-1, October 2026.

| Component | Calculation | Monthly |
|---|---|---|
| S3 Standard storage | 300 GB × $0.023/GB-mo | **$6.90** |
| S3 PUT requests | 100k/mo ÷ 1000 × $0.005 | **$0.50** |
| S3 Standard-IA (after ~6 months) | 300 GB × $0.0125/GB-mo | $3.75 |
| Postgres (self-hosted on a VPS) | 2 GB / 10 concurrent | ~$5–10 (VPS) |
| Metabase OSS (self-hosted) | open source | **$0** |
| **Total on self-hosted Postgres** | | **~$6–17/mo** |
| Managed Postgres (Neon free tier) | <0.5 GB, <5 compute hrs | **$0** |
| **Total on Neon + S3** | | **~$7.40/mo** |
| **Snowflake, if adopted** | 1-4 h/day of dashboards | **+$60–240/mo** |

**S3 is the only material cost here**, and it is trivial: $7.40/month for 300 GB. The
100k images/year are simply not expensive to store.

### What Snowflake would actually add

At AWS us-east-1 on-demand list prices (Service Consumption Table, Sept 2026):

- Credit price **$2.00** (Standard edition)
- **X-Small warehouse = 1 credit/hour** Gen1; **1.35** on Gen2, which is the default
  for new warehouses since June 2026
- Storage **$23/TB/month**
- Per-second billing, 60-second minimum per resume

For this workload — dashboards a few hours per business day:

| Usage | Credits/mo | Cost |
|---|---:|---:|
| 1 h/day (22 h/mo), Gen2 | 30 | ~$60 |
| 4 h/day (88 h/mo), Gen2 | 119 | ~$238 |
| Storage, 0.05 TB | | ~$1.15 |

So Snowflake lands at **$60–240/month** to query **1.2M rows/year** that Postgres
would serve for a few dollars. That is the number worth pushing back on.

Sources: [Snowflake pricing explained](https://www.owox.com/blog/articles/snowflake-pricing-cost-control) ·
[Snowflake cost docs](https://docs.snowflake.com/en/user-guide/cost-understanding-overall) ·
[AWS S3 pricing](https://aws.amazon.com/s3/pricing/) ·
[S3 breakdown 2026](https://www.devzero.io/blog/aws-s3-pricing)

---

## 8. Phased migration plan

### Phase 0 — Today, no cost (30 min)
- [ ] Copy the current database to Postgres with a migration script
- [ ] Add the `s3_key`, `image_bytes`, etc. columns
- [ ] Implement `claim_work()` with `FOR UPDATE SKIP LOCKED`
- [ ] Postgres backend behind the same `PostStore` interface
- [ ] The 100 tests keep running against SQLite (no infra needed)

### Phase 1 — Images to S3 (1 day)
- [ ] Migration script: `data/sneakers_110/*.jpg` → S3
- [ ] Upload from the scraper: direct PUT right after download
- [ ] Verify integrity by comparing `image_sha256`

### Phase 2 — Parallel scrapers (1 day)
- [ ] Launch N processes with disjoint hashtags
- [ ] Verify no post is claimed twice
- [ ] Monitor `pg_locks` / conflicts

### Phase 3 — BI (when needed)
- [ ] Connect Metabase to Postgres (read replica if volume demands it)
- [ ] Only then evaluate Snowflake for historical analytics

---

## 9. Risks and what I am NOT proposing

- **I do not propose BigQuery or ClickHouse** at this scale: Postgres resolves it and
  is simpler to operate.
- **I do not propose partitioning by author** in Postgres now. With 1.2M rows it adds
  nothing; revisit if the history grows.
- **I do not propose Kafka or a real queue.** The claim in the database *is* the queue.
  Adding a broker at this scale is over-engineering.
- **The real risk is not technical, it is compliance:** this is still scraping third
  parties' public content at scale. The architecture does not change Instagram's Terms
  of Use.

---

## 10. Decisions I need from you

1. Managed Postgres (Neon/Supabase) or self-hosted?
2. Start with the Postgres migration now, or S3 first and the database after?
3. Metabase for the team dashboard, or is there already a BI tool in the company?

All three take minutes once decided. The design in §5 and §6 does not depend on them.