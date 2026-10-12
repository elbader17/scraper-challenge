# ARCHITECTURE.md — Guide for another LLM

Orientation document to understand the project **without reading all 3,934 lines of
code** and without rediscovering what was already measured empirically. The original
`README.md` is a log of experiments; this file is the map.

> **When reading this file, treat the "Verified findings" and "Known bugs" sections as
> fact.** These cost hours of network and CPU to discover, and several are known traps:
> changing them "to clean up the code" breaks the scraper.

---

## 1. What this project is

An extractor of **public Instagram posts** by hashtag or keyword, **without login,
without user cookies and without credentials**. It is a proof of concept (PoC) that
grew organically: it works, it is measured, and its real value is the accumulated
knowledge about how Instagram's and Google's HTML responds today.

**Code language:** comments and documentation are in English. Keep it that way when
adding code.

**Status:** 3,934 lines of Python, no dependencies outside `requirements.txt`, no CI.
`python test_ig_google.py` → 100 checks, all pass.

---

## 2. File map

| File | Lines | Role | Alive? |
|---|---:|---|---|
| `ig_google_scraper.py` | 1,735 | **Main script.** Google discovery → embed → image + SQLite | Yes, the one that matters |
| `ig_google_store.py` | 253 | Incremental SQLite store (autocommit, thread-safe) | Yes, imported by the above |
| `ig_hashtag_breadth.py` | ~590 | **CAPTCHA-free.** Hashtag-grid discovery at scale | Yes, recommended entry point |
| `test_ig_google.py` | 680 | 100 checks, no network | Yes |
| `ig_embed_scraper.py` | 839 | Original PoC: hashtag public grid | Legacy. **See epoch bug** |
| `ig_serp_scraper.py` | 621 | Discovery via DuckDuckGo | Legacy, no real pagination |
| `README.md` | ~400 | Log of findings and measurements | Read for the *why* |
| `output/`, `downloads/` | — | Results from real runs | Reference data (gitignored) |

### Dependencies (`requirements.txt`)

`curl_cffi` (Chrome TLS impersonation — the core of anti-fingerprinting),
`beautifulsoup4`, `httpx` (fallback), `pydantic` (models). Optional and commented
out: `websockets` (only for `--interactive`), `httpx[socks]` (SOCKS5 proxy).

---

## 3. Main flow (`ig_google_scraper.py`)

```
                    ┌─────────────────────────────────────────┐
   --query ────────►│ PHASE 1 — DISCOVERY (Google)            │
                    │  rotates dorks × pages (start=0,100,200)│
                    │  extracts shortcodes from the SERP      │
                    └──────────────────┬──────────────────────┘
                                       │  dict[shortcode → serp_url]
                    ┌──────────────────▼──────────────────────┐
                    │ PHASE 2 — EMBED (Instagram, no auth)    │
                    │  GET /p/{code}/embed/captioned/         │
                    │  parses DOM + GraphQL contextJSON blob  │
                    └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │ PHASE 3 — PERSISTENCE                   │
                    │  downloads image + mark_ok() in SQLite  │
                    │  (autocommit per post)                  │
                    └──────────────────┬──────────────────────┘
                                       │
                              metadata.json (rebuilt from the DB)
```

Entry points: `main()` → `run()` (`ig_google_scraper.py:1467`) →
`PostProcessor.process()` (`:1380`) per post. `discover()` (`:852`) does Phase 1.

### Data model

`InstagramPost` (pydantic, `:163`) — base fields (`shortcode`, `author`, `caption`,
`image_url`, `local_image_path`, `source_serp_url`, `scraped_at`, `is_video`) plus
metadata enriched from the blob: `likes`, `comments`, `author_followers`,
`author_verified`, `width`/`height`, `alt_text`, `carousel_*`, `media_id`,
`post_published_at`, `rich_metadata`.

`ScrapeStats` (`:205`) — **deliberately separate** counters: `blocked_queries`
(CAPTCHA), `js_shell_queries` (never saw the SERP) and `rate_limited_queries`
(throttling us) are **not** "zero results". Never unify them.

### SQLite schema (`ig_google_store.py:35`)

Table `posts` with `shortcode` as PK and states `discovered → ok | unavailable |
failed`; `errors` (shortcode, attempt, kind, message); `runs` (run audit trail).
`_migrate()` (`:121`) adds new columns to older databases — if you touch the schema,
add the column there too.

---

## 4. Verified findings (empirical, do not assume)

These were measured against real responses. This is the most expensive part to
obtain.

### Google: the SERP always requires a resolved session

1. **Bare HTTP returns a JS shell**, 200 OK, ~92 KB, **3 anchors**, zero results. A
   naive scraper reports "0 posts" when it never saw the SERP.
2. **Headless Chromium renders the DOM but Google answers with a CAPTCHA** ("unusual
   traffic"), also as a 200.
3. **TLDs behave identically** (`.com`, `.es`, `.co.uk`…). The block is not
   domain-based. Tried unsuccessfully: `CONSENT`, `gbv=1`, `udm=14`,
   `client=firefox-b-d`, Lynx UA, `pws=0`, `safe=off`.
4. **Result `<a>` elements have no `href`** in the rendered DOM. Reading the browser
   DOM **does not work**: you must request the page over HTTP again.
5. **The solution is cookies from a session that already passed the CAPTCHA.**
   `--interactive` opens a real Chrome, the user solves the verification once, and
   cookies are exported over CDP (`NID`, `SNID`, `GOOGLE_ABUSE_EXEMPTION`, `AEC`,
   `DV`). **Scraping then runs over HTTP**, which is faster and does not ask for
   verification again. The profile is persistent: subsequent runs reuse it.

**Documented trap:** `/sorry/index` is **NOT** a valid CAPTCHA detector — a legitimate
SERP *contains* that link in its HTML. Using it produced false positives that made the
script wait for a nonexistent captcha. Correct detection combines
`location.pathname` + the text "unusual traffic" (`ig_google_scraper.py:121`).

**Structured classification** (`:826-848`): CAPTCHA by text markers; JS shell by
markers **or** fewer than `MIN_RESULT_ANCHORS` (10) anchors. A real SERP brings dozens
of anchors even with no results. That is the structural signal.

### Google: what actually helps is pagination

`start=0` gave 14 shortcodes, `start=100` gave 10 more, all new. DuckDuckGo **does
not** paginate (`&s=10` returns 202), which is why that strategy rotates dorks.

The real ceiling is **breadth, not depth**: one keyword yields ~63 unique shortcodes
(8 dorks → 81 returned, **78% overlap**). Google indexes the same content under all
those filters, so rotating dorks yields little; **the keyword queue (`--query`
repeatable / `--queries-file`) is the real mechanism to reach thousands**.

### Instagram: what can and cannot be extracted

- **The public embed responds 200 without a session** and carries author, caption and
  image. That is the path that works.
- **The post photo is `img.EmbeddedMediaImage`.** The author's avatar *is also* an
  `<img>` (`alt="{username}"`, CDN segment `t51.2885-19`). Taking the first `<img>`
  yields 40×40 photos instead of the post.
- **No `og:*`, no JSON-LD, no `<time>`.** Parsing **must** rely on CSS classes
  (`Caption`, `EmbeddedMediaImage`).
- **Soft-404:** for nonexistent, private or deleted shortcodes, Instagram returns
  **200** (not 404) with "The link to this photo or video may be broken". It is
  detected in `is_unavailable()` (`:1018`) so ghost posts with null fields are not
  recorded.
- **`en_US` false positive:** client config blobs produce a spurious "shortcode".
  `is_plausible_shortcode()` (in `ig_embed_scraper.py:362`) filters it.
- **Only ~12-14 posts per hashtag.** The public page does not paginate: `?page=2`
  returns the same grid. This is the hard ceiling of the public method.
- **The hashtag page is served intermittently:** it sometimes returns a "shell page"
  (~490 KB, zero anchors) even for valid and huge hashtags. `#moda` gave 12 posts in
  one run and 0 in the next. That is why Phase 1 **retries** on an empty result: a
  single attempt would turn a false negative into a definitive error.
- **The `contextJSON` blob is not always present:** Instagram serves
  `isRichEmbed: false` with `contextJSON: null`. Measured 2 of 12. **It is not
  transient** (4 retries, no URL variant recovers it; `?__a=1&__d=dis` now returns
  404). The fields stay `null` and `rich_metadata: false` marks the case. Coverage:
  **83% of the enrichment, 100% of the date**.
- **`extract_context_json` cannot be a naive regex** (`:1256`): the JSON arrives
  nested and escaped; it fails on real cases. It scans while respecting escapes.
- **The CDN serves WebP even when the URL ends in `.jpg`** (measured 2 of 24). That is
  why the extension is decided **by the bytes** (`detect_image_format`, `:1028`).
  Saving WebP as `.jpg` breaks PIL, torchvision and ffmpeg.

### The date: decoded from the snowflake, never official

```
media_id = base64url_decode(shortcode)
epoch_ms = (media_id >> 23) + 1314220021721
```

Two independent validations, **both in `test_ig_google.py:465-470`**:

- The ID derived from the shortcode matches **exactly** the official `id` from the blob
  (`shortcode_to_media_id("DN292ZY3K4y") == 3708423362135764530`).
- Captions that declare their own month/year hit: *"dropping in August 2026"* →
  `2026-08-01`; *"in March 2026"* → `2026-03-01`.

Covers **100%** of posts (depends only on the shortcode). It is marked
`post_published_at_inferred: true`: a decode of the ID scheme, **not** a field
Instagram exposes.

---

## 5. Known bugs — all fixed on 2026-10-12

This section previously described four open bugs plus one note. All of them are
fixed, and a fifth bug was found and fixed during that work. The root causes are kept
here because they explain *why* the code looks the way it does, and because the same
class of mistake is easy to reintroduce.

### ✅ FIXED — Retries did not apply to image downloads

**Root cause:** `download_image()` caught `FetchError` internally and returned `False`.
It was wrapped in `with_retries(attempts=...)`, but since the exception never
propagated, the wrapper saw a "success" and **never retried**.

**Impact:** a transient CDN 429 lost the image **permanently**. The post was still
marked `ok`, so neither `--retry-failed` nor `--resume` recovered it (`pending()`
excludes `ok`). `--retries`, `--retry-delay` and `--retry-max-delay` were no-ops for
images, contradicting the README's guarantee that "a 429 does not lose posts".

**Why it survived:** the only `download_image` test used a client that **always
succeeds**. The "fails twice, then recovers" case was never exercised.

**Fix:** `download_image` now propagates `FetchError` (including the
suspiciously-small-response and unknown-format cases, which are also transient CDN
symptoms). It returns `False` *without* raising only for the two non-failure cases:
no image URL, or the file already exists and `--overwrite` was not passed.
`test_ig_google.py` now covers recovery, exhaustion, and that a 404 is not retried.

### ✅ FIXED — Wrong snowflake epoch in `ig_embed_scraper.py`

`MEDIA_ID_EPOCH_MS` was `1284870074250`; the correct value is `1314220021721`
(`ig_google_scraper.py`). They differ by **29,349,947,471 ms ≈ 340 days**.

Dates inferred by the hashtag scraper came out ~340 days early — and **silently**,
because the `infer_published_at()` guard only rejects dates `< 2010` or in the future,
so a wrong-but-plausible date passes. Validated against captions that declare their
own month/year: `DbggwHEER81` → `2026-08-01` (caption: "dropping in August 2026"),
`DVWQjTbjmri` → `2026-03-01` (caption: "dropping in March 2026").

**Fix:** the constant now matches the google scraper, and a test asserts the two
scripts agree and that decoding `DN292ZY3K4y` reproduces the official blob id.

### ✅ FIXED — Deprecated sqlite3 datetime adapter

`mark_ok()` passed a `datetime` straight to SQLite, which used the **default
adapter, deprecated since Python 3.12**. It worked on 3.14 but would break when
removed.

**Fix:** `ig_google_store._iso()` serializes datetimes explicitly. A test asserts no
`DeprecationWarning` is raised and that the stored value is still correct.

### ✅ FIXED — `_parse_dt` masked corrupted data

Any unparseable timestamp was **silently** replaced with `datetime.now()`, turning a
corrupted `scraped_at` into "right now" and hiding the corruption from whoever read
`metadata.json`.

**Fix:** `_parse_dt` returns `None` for missing/corrupt input and logs a warning. The
required `scraped_at` field falls back to `_fallback_now()` only at the single call
site that needs a value, so `post_published_at` stays honestly `None` instead of
becoming a fake "now".

### ✅ FIXED — Image failures were invisible in `stats`

A post whose image failed ended up `status=ok` with `local_image_path: null` and did
**not** increment `stats.failed`, so `ok` was read as "complete post".

**Fix:** `ScrapeStats.images_failed` counts them separately. `images_saved` still
means "a new file was written"; the idempotent "already exists" case is neither.

### 🔍 FOUND DURING THE FIX — numeric IDs now arrive as strings

While validating the epoch against a **live** embed, `media_id` turned out to be
`NULL` on every real post despite `rich_metadata=1`.

**Root cause:** Instagram no longer serves every number as a JSON number. The blob's
`id` and `owner.id` now arrive as quoted strings (`"3978685437899244498"`), and
`dimensions.width/height` as `"1080"`, while `edge_liked_by.count` is still a real
int. The strict `isinstance(x, int)` checks in `parse_gql_media` silently discarded
them — **no error, just missing data on every live post.**

**Fix:** `_coerce_int()` accepts an int or a numeric string (rejecting `bool`, which
is an `int` subclass but never a count). Verified against live embeds: the blob's
`media_id` now equals the ID decoded from the shortcode, which independently
re-confirms the epoch.

**Lesson:** the unit tests used a hand-written fixture with an integer `id`, so they
passed while production data was silently broken. **Test fixtures copied from an
older capture of a live response are a liability** — re-verify them against the real
endpoint periodically.

---

## 6. Design decisions that matter at scale

1. **Autocommit.** Every post is written to SQLite as soon as it is processed
   (`isolation_level=None`). Without `close()`: it was verified that an abrupt
   interruption does not lose what was already done.
2. **`metadata.json` is rebuilt from the database**, not from the run
   (`write_metadata_from_store`, `:1154`). The file reflects all accumulated knowledge
   even if the run was cut in half.
3. **`--resume` does not re-trigger blocked discovery** (`:1547`): if Google is stuck
   but there are pending posts in the database, it continues with what was already
   discovered. Without this, resuming a run forced redoing the CAPTCHA bootstrap.
4. **The rate limiter reserves the slot under the lock but waits OUTSIDE the lock**
   (`RateLimiter.acquire`, `:1104`). Otherwise workers would serialize and concurrency
   would be pointless.
5. **Each worker has its own HTTP session** (`threading.local` in
   `PostProcessor.client()`, `:1364`): sessions are not thread-safe.
6. **The limit is applied to the list, not inside the loop** (`:1587`): with workers
   it is simpler to trim the list than to carry a shared counter.
7. **Nothing is swallowed silently** (`:1610`): an unexpected exception in a worker is
   logged as `unexpected error` and increments `stats.failed`; it is never lost.

---

## 7. Performance (measured, same machine)

| Config | 12 posts |
|---|---:|
| 1 worker, no brake | 9.24 s |
| 6 workers, no brake | **2.09 s** (4.4x) |
| 6 workers, `--rate 0.3` | 7.23 s |

Projected to 3,000 posts: **~4.4 h → ~30 min**. Instagram does not publish its limit:
measured empirically, at ~3 req/s (`--rate 0.3`) there was **not a single** 429.
`--discovery-rate 6` avoided Google's 429s entirely.

The CAPTCHA-free strategy measured 20 posts (with images) in ~15 s at
`--workers 4 --rate 0.4`.

---

## 8. Usage

```bash
# Recommended: no CAPTCHA, no browser, $0
python ig_hashtag_breadth.py --hashtag running --limit 20

# Google: solve the CAPTCHA once and continue
python ig_google_scraper.py --query "running shoes" --limit 30 --interactive

# Keyword queue, concurrency and throttling
python ig_google_scraper.py --queries-file keywords.txt --limit 500 \
  --interactive --workers 6 --rate 0.4 --discovery-rate 6

# Resume an interrupted run
python ig_google_scraper.py --queries-file keywords.txt --limit 500 --resume
```

### Flags (`ig_google_scraper.py:1654`)

`--query` (repeatable) · `--queries-file` · `--limit` (10) · `--workers` (1) ·
`--rate` (0) · `--discovery-rate` (0) · `--discovery-retries` (2) · `--tld` (com) ·
`--render` · `--interactive` · `--profile` · `--cdp-port` (9222) · `--wait-captcha`
(180) · `--max-queries` (6) · `--max-pages` (2) · `--num-per-page` (100) ·
`--delay-min` (3) · `--delay-max` (7) · `--timeout` (25) · `--retries` (3) ·
`--retry-delay` (2) · `--retry-max-delay` (60) · `--retry-failed` · `--db` ·
`--resume` · `--overwrite` · `--shortcodes` · `--output-dir` · `--proxy-url` · `-v`

### Flags (`ig_hashtag_breadth.py`)

`--hashtag` (repeatable) · `--hashtags-file` · `--limit` (20) · `--workers` (4) ·
`--rate` (0.4) · `--tag-delay` (2.5) · `--tag-retries` (3) · `--resume` ·
`--retry-failed` · `--overwrite` · `--db` · `--output-dir` · `--proxy-url` · `-v`

**Output:** `output/google_{slug}/{shortcode}.jpg` + `metadata.json` +
`posts.sqlite3` (or `output/hashtags_{n}tags/` for the breadth script).

---

## 9. Tests

```bash
python test_ig_google.py    # 100 checks, no network, ~1 s
```

It is a linear assert script (not pytest): each `check(cond, msg)` prints
`PASS`/`FAIL`. They cover store states, survival of an abrupt interruption, retry
policy, `Retry-After` parsing, HTTP error classification, rate limiter spacing under
concurrency, concurrent SQLite writes (200 posts × 8 threads), keyword queue
sanitization, discovery failure classification (429 / CAPTCHA / JS shell as
**distinct** facts), format detection by bytes, `contextJSON` extraction (including
escapes and the lightweight embed) and snowflake date decode validated against real
captions.

**When adding code:** add the check at the end of the script, keep the message style,
and do not introduce a network dependency — the value of the suite is that it runs
offline and fast.

---

## 10. What CANNOT be obtained (and why you should not push)

Official `taken_at`, **video view counts**, saves, follower lists. They are not in any
variant of the login-less embed. They would require Meta's Graph API with a Business
Account: login and permissions, which is exactly what this project avoids.

> Open gap: a **reel** was never tested to confirm whether videos carry extra fields
> (duration, views) beyond the common ones.

---

## 11. Responsible use and legal status

Public content only, at a limited pace. Before scaling volume, check Instagram's Terms
of Use and the regulations applicable to your case. The residential proxies the docs
recommend to unblock Google's CAPTCHA have cost and compliance implications worth
evaluating explicitly.

---

## 12. Context for another LLM working here

- **Read `README.md` for the why**, this file for the map. They are not redundant:
  the README narrates the experiments, this describes the structure.
- **Do not "simplify" the retries, the delays, or the guards.** They look like
  over-engineering; each one covers a real measured failure.
- **Before touching `RE_SHORTCODE`, `CAPTCHA_MARKERS` or the embed parsers**,
  understand that each constant corresponds to an actual observed response.
- **If you add a model field:** `InstagramPost` (pydantic), the `UPDATE` in
  `mark_ok()` (`ig_google_store.py:189`), `_migrate()`, and
  `write_metadata_from_store()` (`:1154`). Miss one and the field is silently lost when
  the JSON is rebuilt.
- **Verify against real data** (`output/google_running_shoes/metadata.json`) before
  changing anything that alters how results are interpreted.
