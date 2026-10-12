# CAPTCHA Research: Solutions to Google's Block

**Date:** 2026-10-12 · **Method:** empirical benchmark from this machine, not a
literature review. Every claim below is backed by a reproducible measurement.

> **Goal of this research:** find a discovery path that does not depend on solving
> Google's CAPTCHA, or determine that none exists and what the legitimate alternatives
> imply.

---

## Executive summary

**I found no search engine that replaces Google in this project.** Bing, Brave,
Startpage, Mojeek and Marginalia **do not work** for this case — each fails for a
different, verifiable reason. DDG is the only one without a CAPTCHA, but it yielded
**12% success** and a ceiling of **10 shortcodes**, far below what the project already
has.

The underlying reason is structural: **Google is, today, the only engine that indexes
enough Instagram posts to discover them via dork.** It is not that Google is hard to
scrape — it is that the others do not have the index.

The recommendation is **not to switch engines**, but to make the system degrade
honestly: treat Google as the primary source and add the hashtag-breadth strategy as a
CAPTCHA-free primary, accepting that Google yields more per unit.

---

## Benchmark: the 6 engines (run 2026-10-12)

Query used: `site:instagram.com/p/ "running shoes"`

| Engine | Status | Size | Anchors | Shortcodes | CAPTCHA |
|---|---:|---:|---:|---:|---|
| **duckduckgo** (html) | 200 | 49,785 B | 52 | **10** | ❌ no |
| **bing** | 200 | 127,306 B | 55 | **0** | ❌ no |
| **marginalia** | 200 | 62,578 B | 53 | **0** | ❌ no |
| **startpage** | 200 | 22,106 B | 1 | 0 | ❌ no |
| **mojeek** | **403** | 368 B | 1 | 0 | ❌ no |
| **brave** | **429** | 73,896 B | 2 | 0 | **yes** |

Key point: **`blocked=False` with `shortcodes=0` is not the same as "no results"**.
Bing, Marginalia and Startpage respond 200 but with no posts. Conflating the two is
the same conceptual error the project already made with Google (and documented
correctly in the README). My inherited `extract_shortcodes()` only detects it on one
side: Bing passes the "no CAPTCHA" filter but contributes nothing.

---

## Why each alternative fails

### ❌ Bing — 200 OK, zero posts. The most dangerous illusion

Bing is the only engine that responds **200 with 127 KB and 55 anchors** and no
CAPTCHA. By the project's own metrics (`MIN_RESULT_ANCHORS = 10`) **Bing would look
like a valid, functional SERP**. But it returns **zero shortcodes** on every query.

The cause is that Bing ignores the `site:` operator for this domain. Inspecting the
HTML, the result `<cite>` elements are running sites (`runfitners.com`,
`runningcorrer.com.ar`, `adidas.com.ar`), **not Instagram**. Result links are also
obfuscated in `/ck/a?` with parameters that **are not recoverable base64** (32
`u=a1...` signatures found, 0 decodable to destination URLs).

Four dork variants, zero shortcodes:

```
'site:instagram.com/p/ sneakers'        -> 0
'site:instagram.com/reel/ running'      -> 0
'instagram Running shoes'               -> 0
'site:instagram.com "zapatillas"'       -> 0
```

**Concrete risk:** if Bing were added as a fallback, the script would report success
with "many results" and discover no posts. That is worse than failing.

### ❌ Brave — direct CAPTCHA

**429** immediately with 2 anchors. The only engine that explicitly detects the block.

### ❌ Mojeek — hard 403

368 bytes, 1 anchor. Rejects the request outright.

### ❌ Startpage — empty shell

200 OK but 22 KB and **1 single anchor**. That is Startpage's JS shell: the SERP never
arrived.

### ❌ Marginalia — index too small

200 OK, 53 anchors, 0 shortcodes. Marginalia indexes small/old sites; it has no
Instagram coverage.

### ⚠️ DuckDuckGo — the only CAPTCHA-free one, but insufficient

DDG **does** deliver shortcodes without CAPTCHA and without cookies. But it has two
hard limits, both measured:

**1. It does not paginate.** Confirming what the README already said, now with my own
measurement:

```
s=None  -> 200  48,785 B  10 shortcodes
s=10    -> 202  14,217 B   0
s=30    -> 202  14,241 B   0   ... (identical up to s=200)
```

**2. Aggressive throttling.** The 202 is rate limiting, not "no results". With 8
dorks and 8 s pauses:

```
queries_OK=1  throttle=7   (12% success)
unique shortcodes: 9
```

Longer pauses do not help: with 20 s waits, 5 of 5 queries returned 202. The only
method that produced a result was **POST** to `html.duckduckgo.com/html/` instead of
GET — but the result is still intermittent (7 of 8 throttled).

**Absolute DDG ceiling: ~10 shortcodes per session.** The current project with Google
gets 13 on the first query and paginates. **DDG is a regression, not a solution.**

---

## The path that actually works: Google is already solved

The most important finding of this research is negative: **the CAPTCHA problem already
has a working solution inside the project.**

`--interactive` opens a real Chrome, the user solves the CAPTCHA **once**, and cookies
are exported over CDP. Scraping then runs over HTTP with the session already resolved,
and the persistent profile reuses it across runs.

I verified that the design decision holds: **Phase 2/3 does not depend on Google at
all.**

```
embed status 200 | 301,731 B
-> Phase 2/3 does NOT depend on Google: YES
```

Instagram's embed responds 200 with 301 KB from this IP, without cookies and without
intervention. **The only fragile point of the system is discovery**, and it is exactly
the point Google solves once per session.

This inverts the README's diagnosis: it is not that "Google is unscrapeable", it is
that **the rest of the system needs Google for nothing else**.

---

## The better answer: don't use a search engine at all

While researching alternatives to the CAPTCHA, a more efficient solution surfaced:
**remove the search engine from the flow instead of defeating it.** Visiting the
hashtag's public page directly yielded, measured:

- **20 hashtags → 180 unique shortcodes**, 75% of tags returning a grid
- **Zero CAPTCHAs, zero cookies, zero browser, zero human interaction**
- **$0**

That became [`ig_hashtag_breadth.py`](./ig_hashtag_breadth.py), verified end-to-end at
20/20 posts with images and enriched metadata. Full details in
[`BREADTH-SOLUTION.md`](./BREADTH-SOLUTION.md).

**This supersedes the search-engine question entirely.** If the hashtag grid yields 180
posts for free and unattended, no search engine — CAPTCHA-free or not — is worth the
complexity. Google remains useful only for one thing the hashtags cannot do:
**pagination**, since a single keyword yields ~63 unique shortcodes via `start=`.

---

## Legitimate alternatives (change of approach, not of engine)

If the goal is scale or data the embed does not expose, the way out is to stop
scraping. Research on the current state of the official APIs:

### 1. Instagram Graph API — for Business/Creator accounts

**Status:** active. It is the official path.

- Only works with **Business or Creator** accounts. Personal accounts are **not
  accessible**.
- Requires a Meta app + **App Review** (2-4 weeks, or 60+ days depending on source).
- **200 calls/hour** per user token.
- Provides what the embed lacks: **video view counts**, reach, impressions.
- Since January 2025 permissions were renamed to `instagram_business_*`.

### 2. Instagram Basic Display API — dead

**Shut down on December 4, 2024**, with no replacement for personal accounts. It was
the only official access to personal accounts; **today there is no official access to
personal Instagram account data**. That is a platform limitation, not a scraper one.

### 3. Meta Content Library API — most interesting for this project

Access to the full public content archive of Facebook and Instagram, near real-time.

- **Requires academic or non-profit research affiliation**, review by ICPSR (University
  of Michigan), **4-6 weeks**, and a Data Use Agreement.
- Programmatic access happens **inside a Virtual Data Enclave** — data cannot be
  downloaded.
- Coverage: public accounts **verified or above a follower threshold** (the threshold
  dropped to 100 in v6.0, from 25,000 in v5.0).
- Limits: 1,000 queries per 7 days.
- An academic study (arXiv 2601.12390) measured that only **~50% of posts visible in
  Explore** and **42% of metadata parameters** are accessible to researchers.

**Verdict:** technically superior and CAPTCHA-free, but the academic-affiliation
requirement makes it unviable unless the project is genuine research.

### 4. Third-party APIs (Phyllo and similar)

Social data providers handle auth and access. It is the standard commercial route,
**at the cost of paying and giving up some control**. Not evaluated in depth.

---

## Recommendation

### For the project as it is (PoC, no login)

**Use `ig_hashtag_breadth.py`.** It supersedes this entire research: 180 posts for
free, no CAPTCHA, unattended. Adding Bing would be actively harmful (see § Bing).

1. **Primary: hashtag breadth.** Zero friction, zero cost, verified.
2. **Secondary: Google with `--interactive`** when you need pagination or breadth
   beyond what a tag list gives.
3. **Resist the proxy.** The README recommends residential proxies; it is still the
   most effective option for Google, but it is recurring cost and a compliance
   problem. Before scaling, quantify whether the real bottleneck is the CAPTCHA or the
   ~63-shortcodes-per-keyword ceiling (§ breadth).

### About the real ceiling

Worth noting where the true limit is: **it is not the CAPTCHA, it is the index.**
Google pays out its 63 shortcodes per keyword. If the bottleneck is volume, the lever
is **more keywords and more dorks**, not more speed or more ingenious bypass. The
README already says this ("the ceiling is breadth, not depth"); this research confirms
it.

### If the goal changes to scale or rich data

The scraper stops being the right tool. Without login there is no official path for
personal accounts; with login, the Graph API covers Business/Creator and adds the
views this project cannot get today. **The project's design decision — "no login, no
credentials" — is what guarantees the CAPTCHA is a recurring problem.**

---

## Method and reproducibility

Everything measured was run from this machine against the engines live. Honest
limitations of this benchmark:

- **A single IP, a single network session.** A datacenter IP may be in a worse
  position than a residential one against several of these engines. DDG's numbers
  (12%) in particular **would improve with a residential IP** and get worse with more
  volume.
- **Blocking thresholds are dynamic.** What returns 202 today may return 200
  tomorrow. Re-measure before deciding.
- **Proxies were not tested**, deliberately: it is the variable the project already
  assumes as a solution, so it adds no information about the alternatives.
- **`extract_shortcodes()` was reused from the project**, which guarantees
  comparability but also inherits its assumptions.

---

## Conclusion

The question was whether another search engine could avoid the CAPTCHA. **The
empirical answer is that there is no better one: the others do not have the index.**
Bing is the trap — it responds 200 and looks healthy, but contributes not a single
post. DDG avoids the CAPTCHA and still yields 8x less than the project already had.

The real solution already existed inside the project: `--interactive` solves the
CAPTCHA once per session and everything else works without Google. And better still:
removing the search engine altogether (`ig_hashtag_breadth.py`) removes the problem
rather than solving it.

What remains unresolved is not technical but **strategic**: if the goal is to scale,
the login-less scraper is the structural bottleneck, and the design decision that
created it — avoiding credentials — is exactly what makes fighting the CAPTCHA
unavoidable.