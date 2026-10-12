# Instagram Hashtag Scraper (PoC) — via public Embeds

Extracts public Instagram posts from a hashtag **without login and without user
credentials**, using the public *embed* path. Proven end-to-end.

The project has **four** discovery strategies, all unauthenticated:

| Script | Discovers posts via | Status |
|---|---|---|
| `ig_embed_scraper.py` | Hashtag public grid | Works (ceiling ~12-14 posts) |
| `ig_serp_scraper.py` | DuckDuckGo (`site:` dorks) | Works with slow pacing |
| `ig_google_scraper.py` | **Google** (dorks + `start=` pagination) | Works with `--interactive`; retries + checkpoint |
| `ig_hashtag_breadth.py` | **Hashtag grid, no search engine** | **No CAPTCHA, no browser, $0** |

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Basic (default output: ./downloads/sneakers)
python ig_embed_scraper.py --hashtag sneakers --limit 5

# With proxy (HTTP/HTTPS/SOCKS5)
python ig_embed_scraper.py --hashtag sneakers --limit 20 --proxy-url socks5://127.0.0.1:9050

# Add INFERRED publication date + verbose
python ig_embed_scraper.py --hashtag sneakers --limit 5 --decode-post-time -v
```

| Flag | Default | Description |
|---|---|---|
| `--hashtag` | *(required)* | Hashtag without `#` |
| `--limit` | `10` | Maximum posts to process |
| `--output-dir` | `./downloads/{hashtag}` | Output folder |
| `--proxy-url` | — | `http://`, `https://` or `socks5://` |
| `--retries` | `3` | Retries on 429/5xx |
| `--timeout` | `25` | HTTP timeout (s) |
| `--overwrite` | `False` | Re-download images already present |
| `--decode-post-time` | `False` | Adds inferred `post_published_at` |
| `-v/--verbose` | `False` | DEBUG logging |

## Output

```
downloads/sneakers/
├── Dcs-O8mxFaj.jpg      # photo at full resolution of the post
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

## How it works

**Phase 1 — Discovery.** `GET /explore/tags/{hashtag}/` returns server-rendered HTML.
Shortcodes come from two sources: `<a href="/reel/{code}/">` anchors and
`"code":"{code}"` blobs.

**Phase 2 — Embed.** `GET /p/{shortcode}/embed/captioned/` responds 200 without a
session and contains author, caption and image.

**Phase 3 — Persistence.** Downloads `{shortcode}.jpg` and writes `metadata.json`.

## The CAPTCHA-free strategy — `ig_hashtag_breadth.py`

**Start here.** It removes the search engine from the picture entirely: it visits the
hashtag's public page directly and reads the grid Instagram already renders.

- **No CAPTCHA.** No search engine means nothing to unblock.
- **No cookies, no Chrome, no browser profile, $0.**
- **No human in the loop.** Fully unattended.

```bash
# 20 posts, no interaction whatsoever
python ig_hashtag_breadth.py --hashtag running --limit 20

# Several tags
python ig_hashtag_breadth.py --hashtag running --hashtag sneakers --hashtag yoga --limit 20

# Scale: from a file, with concurrency
python ig_hashtag_breadth.py --hashtags-file tags.txt --limit 500 --workers 6 --rate 0.4
```

Verified run: **20/20 posts, 20/20 images, 0 failures**, all with enriched metadata
(likes, followers, inferred date, author, caption).

**The ceiling is breadth, not depth.** Each hashtag exposes ~12-14 posts and does not
paginate, but distinct tags overlap only ~25%, so the unit of scale is the *number of
tags*: 100 tags ≈ 850 posts, 500 tags ≈ 4,000 posts.

Full rationale and measurements: [`BREADTH-SOLUTION.md`](./BREADTH-SOLUTION.md).

## Google strategy — `ig_google_scraper.py`

Instagram posts **indexed in Google**, discovery via dorks and real pagination,
and extraction through the same public embed (which does work without blocking).

```bash
# Recommended: opens a Chrome, you solve the verification once, then it continues
python ig_google_scraper.py --query "running shoes" --limit 30 --interactive

# With proxy (only if you are also behind a blocked IP)
python ig_google_scraper.py --query "running shoes" --limit 30 \
  --proxy-url http://user:pass@host:port

# Without depending on Google: known shortcodes -> straight to the embed
python ig_google_scraper.py --query sneakers --shortcodes DYHxVb0iEI2,DceGJtwimbX
```

`--interactive` requires `pip install websockets`. Google has one structural
advantage over DDG: **it does paginate**, with `start=0,100,200…` and `num=100`.
DDG does not (`&s=10` returns 202), which is why that strategy rotates dorks.

### Critical finding: Google does not deliver the SERP without a resolved session

Empirically measured, in this order:

1. **The bare HTTP path returns a shell that demands JavaScript.** ~92 KB, 200 OK,
   **3 anchors**, zero results. That is not an error: a naive scraper reports
   "0 posts" when it actually never saw the SERP.
2. **With headless Chromium the DOM renders, but Google answers with a CAPTCHA**
   ("Our systems have detected unusual traffic…"), also as a 200.
3. **TLDs behave identically** (`.com`, `.es`, `.com.ar`, `.co.uk`, `.ca`, `.mx`):
   the block is not domain-based. Tried unsuccessfully: `CONSENT` cookie,
   `gbv=1`, `udm=14`, `udm=2`, `tbm=isch`, `tbm=vid`, `client=firefox-b-d`,
   `client=ubuntu`, Lynx user-agent, `pws=0`, `safe=off`. It is not transient
   either (retried after 75 s).
4. **Google's DOM hides the links.** The rendered SERP shows results (author,
   like count, date) but the result `<a>` elements **have no `href`**, and they do
   not appear when walking shadow DOM either. That is why reading the browser DOM
   does not work: you have to request the page over HTTP again.
5. **The solution: cookies from a session that already passed the CAPTCHA.** With a
   real Chrome where the user solves the verification once, and exporting its
   cookies over CDP (`NID`, `SNID`, `GOOGLE_ABUSE_EXEMPTION`, `AEC`, `DV`), the
   common HTTP path starts returning the full SERP: 579 KB and 10 shortcodes on the
   first query.

Hence the `--interactive` design: the browser is **not** used to scrape, only to
unblock the CAPTCHA. Afterwards scraping runs over HTTP, which is faster and does
not ask for verification again. The profile is persistent, so subsequent runs
reuse what was already resolved.

### Trap: `/sorry/index` is useless as a CAPTCHA detector

A legitimate SERP **contains** a link to `/sorry/index` in its HTML. Using it as a
marker produced a false positive that made it wait for a nonexistent captcha (the
script ended up looping over a valid SERP with 11 shortcodes). Correct detection
combines `location.pathname` with the text "unusual traffic".

### End-to-end verification

```
Chrome ready (wayland). Profile: /root/.ig_google_profile
  SERP loaded (11 shortcodes); exporting cookies…
  Cookies obtained: AEC, DV, GOOGLE_ABUSE_EXEMPTION, NID, SEARCH_SAMESITE, SNID, _GRECAPTCHA, __Secure-STRP
Phase 1: 13 unique shortcodes discovered
Phase 2/3: 12 posts, 12 images -> output/google_running_shoes/metadata.json
```

Without proxy, without login, without credentials: 13 shortcodes discovered in
Google and 12 posts with a full-resolution image, all with real author and caption.

### Scale: concurrency, keyword queue and throttling

```bash
# Several keywords queued (repeatable or from a file)
python ig_google_scraper.py --query "running shoes" --query "trail running" \
  --limit 500 --interactive --workers 6 --rate 0.4 --discovery-rate 6

# From a file (uses # for comments)
python ig_google_scraper.py --queries-file keywords.txt --limit 500 --interactive

# If it gets interrupted (Ctrl-C, crash, network), it resumes where it left off
python ig_google_scraper.py --queries-file keywords.txt --limit 500 --resume
```

| Flag | Default | For what |
|---|---|---|
| `--query` | — | Keyword. **Repeatable** to build a queue |
| `--queries-file` | — | File with one keyword per line |
| `--workers` | `1` | Concurrent workers in phase 2/3 |
| `--rate` | `0` | Seconds between requests to Instagram. `0` = no brake |
| `--discovery-rate` | `0` | Seconds between Google queries. If `0` uses `--rate` |
| `--discovery-retries` | `2` | Retries per Google query on 429 |
| `--db` | `<output-dir>/posts.sqlite3` | Incremental database |
| `--resume` | `False` | Skips finished posts and continues with pending ones |
| `--retry-failed` | `False` | Re-includes those left as `failed` |
| `--retries` | `3` | Attempts per post on 429/5xx |
| `--retry-delay` / `--retry-max-delay` | `2` / `60` | Exponential backoff, in seconds |

**Measured performance** (12 posts, same machine):

| Config | Time |
|---|---|
| 1 worker, no brake | 9.24 s |
| 6 workers, no brake | **2.09 s** (4.4x) |
| 6 workers, `--rate 0.3` | 7.23 s |

Projected to 3000 posts: **~4.4 h → ~30 min**. The global brake is what lets you
lower the speed without touching code when Instagram starts cutting.

**The rate limiter reserves the slot under the lock but waits outside it.** If not,
workers would serialize one at a time and concurrency would be pointless. Instagram
does not publish its limit: measured empirically, at ~3 req/s (`--rate 0.3`) there
was not a single 429.

### Why Google and not only dorks: the ceiling

One keyword yields ~63 unique shortcodes (measured with 8 dorks: 81 returned, 63
unique, **78% overlap**). Google indexes the same content under all those
filters, so rotating dorks yields little and the keyword queue is the real
mechanism to reach thousands.

What **does** help is the pages: `start=0` gave 14 shortcodes and `start=100` gave
10 more, all new.

### Google will throttle you if you don't throttle it

After sustained use, Google starts returning **429** on discovery queries. The
script retries with backoff and, if it persists, says so in its own words:

```
WARNING  Google is rate limiting (429) after 3 attempts; run with a higher --rate or wait
```

`--discovery-rate 6` (one query every 6 s) avoided 429s entirely in test runs. The
`stats` counters deliberately separate three things that used to be conflated:
`blocked_queries` (captcha), `js_shell_queries` (never saw the SERP) and
`rate_limited_queries` (it is throttling us). **None of the three is "no results"**,
and the final message says so precisely.

### Design decisions that matter at scale

1. **Autocommit.** Every post is written to SQLite as soon as it is processed.
   Without `close()`: it was verified that an abrupt interruption does not lose
   what was already done.
2. **`metadata.json` is rebuilt from the database**, not from the run. That is why
   the final file reflects all accumulated knowledge even if the run was cut in
   half.
3. **A 429 does not lose posts.** It waits for the `Retry-After` the server sends
   (capped by `--retry-max-delay`) and retries with exponential backoff plus jitter.
   A 404 is never retried: it is terminal by definition.
4. **Failures are logged** in the `errors` table, with shortcode, attempt and
   message. `failed` is not retried on its own in the next run unless you ask for
   `--retry-failed`, so it does not cycle forever over an unreachable post.
5. **With `--resume`, a blocked discovery does not abort the run**: it continues
   with what is already in the database. Without this, resuming a Google run forced
   redoing the CAPTCHA bootstrap.
6. **The store is thread-safe**: `check_same_thread=False` + lock around writes. A
   shared SQLite connection without serializing corrupts the database.
7. **The extension is decided by the bytes, not by the URL.** Instagram's CDN serves
   WebP even when the URL ends in `.jpg` — measured in 2 of 24 posts of a real run.
   Saving WebP as `.jpg` breaks PIL, torchvision and ffmpeg.

### Enriched metadata: the blob that was already there and being discarded

The rendered embed includes a `contextJSON` with `gql_data.shortcode_media`: a real
GraphQL response. The scraper now parses it and adds fields that were previously
lost, **with not a single extra request**.

| Field | What it is |
|---|---|
| `likes`, `comments` | `edge_liked_by.count`, `edge_media_to_comment.count` |
| `author_followers` | `owner.edge_followed_by.count` |
| `author_verified` | `owner.is_verified` |
| `width`, `height` | real dimensions |
| `alt_text` | `accessibility_caption` (Instagram alt-text) |
| `carousel_count`, `carousel_shortcodes` | carousel slides |
| `media_id`, `author_id` | numeric IDs |
| `post_published_at` | **inferred** date, decoded from the media ID |

This makes the **engagement rate** `(likes+comments)/followers` computable, which is
the most useful metric for influencer ranking.

### Coverage: 83% of the enriched metadata, 100% of the date

Measured on 12 real posts: **10 of 12 carry the blob**. The 2 that don't, Instagram
serves as `isRichEmbed: false` with `contextJSON: null`.

**It is not transient**: each was retried 4 times and it never appeared, and no URL
variant recovers it (`/embed/` without `/captioned/`, with `?img_index=1`). The
`?__a=1&__d=dis` endpoint now returns 404. That is why the fields stay `null` and
are not retried: `rich_metadata: false` marks the case.

### The date: decoded from the snowflake, not official

```
media_id   = base64url(shortcode)
epoch_ms   = (media_id >> 23) + 1314220021721
```

Two independent validations:

- The ID derived from the shortcode matches **exactly** the `id` the blob carries.
- Captions that declare their own month/year hit: *"Running shoes dropping in
  August 2026"* → `2026-08-01`; *"in March 2026"* → `2026-03-01`; *"guide to
  running shoes in 2026"* → `2026-05-09`.

Covers **100%** of posts, because it only depends on the shortcode, which is always
known. It is marked `post_published_at_inferred: true`: it is exact, but it is a
decode of the ID scheme, not a field Instagram exposes.

### What still cannot be obtained

Official `taken_at`, **video view counts**, saves and follower lists. They are not
in any variant of the embed. They would require Meta's Graph API with a Business
Account, meaning login and permissions.

> Open gap: a **reel** was never tested to confirm whether videos carry extra fields
> (duration, views) beyond the common ones. Google started returning 429 during the
> investigation and it was not worth punishing it further.

## Tests

```bash
python test_ig_google.py    # 75 checks, no network
```

They cover discovery idempotency, store states, survival of an interruption
without `close()`, retry policy (including that 404 is not retried and that
`Retry-After` is capped), `Retry-After` parsing, HTTP error classification, rate
limiter spacing under concurrency, concurrent SQLite writes (200 posts x 8 threads,
without corruption), keyword queue sanitization, discovery failure classification
(429 / captcha / JS shell as *distinct* facts), image format detection, `contextJSON`
extraction (including escapes and the lightweight embed) and snowflake date decode
validated against real captions.

## Findings about Instagram's current HTML

Verified empirically, not assumed:

1. **No `og:*`, no JSON-LD, no `<time>`.** Parsing must rely on CSS classes. That is
   why `scraped_at` (the moment of extraction) is the reliable timestamp, and the real
   publication date **is no longer available** in the embed.
2. **The post photo is `img.EmbeddedMediaImage`.** The author's avatar *is also* an
   `<img>` (no class, `alt="{username}"`, with CDN segment `t51.2885-19`). Taking the
   first `<img>` yields 40×40 photos instead of the post.
3. **Soft-404.** For nonexistent, private or deleted shortcodes, Instagram returns
   **200** (not 404) with the notice *"The link to this photo or video may be broken"*.
   It is detected in `is_unavailable()` so ghost posts with null fields are not recorded.
4. **`en_US` false positive.** Client config blobs
   (`{"IntlCurrentLocale",[],"code":"en_US"}`) produce a spurious "shortcode".
   `is_plausible_shortcode()` filters it by length, alphabet and locale shape.
5. **Only ~12-14 posts per hashtag.** The public page does not paginate: `?page=2`
   returns the same grid. A high `--limit` is warned about in the log but is not
   reachable through this path.
6. **The hashtag page is served intermittently.** Instagram sometimes returns a generic
   "shell page" (`<title>Instagram</title>`, ~490 KB, zero anchors) even for valid and
   huge hashtags: it was verified that **`#moda` returned 12 posts in one run and 0 in
   the next**. That is why Phase 1 retries up to 3 times on an empty result; a single
   attempt would turn that false negative into a definitive error. The log includes
   the `<title>` and size of each attempt so the diagnosis is verifiable.

## Tags without a public grid

Not every tag has content in the public view, and that is **not a script bug**.
Verified with `#ropa`: it consistently returns the shell page, no posts, after 3
attempts —while `#moda`, `#vestimenta` and `#fashion` return 12 each. Curiously, an
invented tag like `#ropaaa` returned 8 posts: the grid responds to trending
popularity, not to the existence or validity of the tag.

If a tag returns 0, the log distinguishes it explicitly from a failure.

Measured across 20 consecutive tags: 15 returned a grid (75%), 5 returned nothing.
Expect roughly that hit rate when planning tag lists.

## Limitations

- **No pagination:** ~12-14 posts per hashtag. This is the hard ceiling of the public
  method. To scale, use more tags (breadth), not a higher limit.
- **Only featured/popular content** from the tag, not the full chronological feed.
- **`post_published_at` is inferred, not real.** With `--decode-post-time` the
  shortcode snowflake is decoded; it is marked `post_published_at_inferred: true`. It
  must not be used as a verified publication date.
- **Reels** deliver their cover frame via CDN; the MP4 requires another path.
- **IP blocking:** after high volume Instagram responds 429. The script handles it
  with exponential backoff while respecting `Retry-After`, and **stops** execution if
  the 429 persists. Sustained scraping requires proxy rotation.
- The Google script is sequential with delays of 1.5–3.5 s on purpose: it mimics human
  traffic. Adding asyncio concurrency would void that protection.

## Responsible use

Public content only, at a limited pace. Check Instagram's Terms of Use and the
regulations applicable to your case before scaling volume.

---

## Further reading

| Document | What it covers |
|---|---|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Code map and orientation for another LLM; verified findings; **known bugs** |
| [`CAPTCHA-RESEARCH.md`](./CAPTCHA-RESEARCH.md) | Benchmark of 6 search engines; why none replaces Google |
| [`BREADTH-SOLUTION.md`](./BREADTH-SOLUTION.md) | The CAPTCHA-free hashtag-breadth strategy and how to scale it |