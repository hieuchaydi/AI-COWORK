# Crawler Layer — Implementation Plan v1

Companion document to `BROWSER_CONTROL_PLANE_PLAN.md` (referred to below as **BCP**).
Status: DRAFT — ready for implementation
Owner: <fill>

---

## 0. Rules for whoever implements this (including coding agents)

1. This layer sits **on top of** the BCP high-level API (`Page`, `Locator`, …). It must never import a
   transport type, and never a CDP type. Same CI grep rule as BCP §5.
2. **HTTP-first.** A browser is the expensive fallback, not the default. See §3. A connector that
   opens a browser for a server-rendered page will be rejected in review.
3. The **Compliance engine (§4) is not optional and not bypassable.** No connector may issue a request
   that did not pass through it. There is no "skip robots" flag, and no config key that disables it.
4. Selectors are **never** hardcoded from memory or from a single page view. Every extractor ships with
   recorded HTML fixtures and golden-output tests (§5.4, §10). An extractor without fixtures is not
   done.
5. Every connector implements the same `Source` interface (§2) regardless of whether it fetches over
   HTTP, over a browser, or from an authenticated vendor API. The runtime must not know the difference.
6. Out of scope, do not implement under this plan: anti-bot evasion, fingerprint spoofing, CAPTCHA
   solving, rotating identities to defeat rate limits, or any workaround for an access control. If a
   task appears to need one of those, stop and ask.
7. Facts marked **[VERIFIED 2026-08-28]** were checked against the live site on that date. Facts marked
   **[VERIFY]** must be re-checked at implementation time. Do not treat anything else as ground truth.

---

## 1. Where this layer sits

```
      Connectors  (vnexpress, shopee-api, …)          §11, §12
            |
      Crawl runtime: Frontier, Scheduler, Fetcher, Extractor, Sink      §2
            |
      Compliance engine (robots, rate limit, authorization)             §4   <- mandatory gate
            |
      +-------------------+---------------------------+
      |                   |                           |
  HttpFetcher       BrowserFetcher (BCP Page)     ApiFetcher
                          |
                    BCP  IBrowserTransport -> bcp-agent -> Chromium
```

Reasoning for the split: crawling is 90% scheduling, politeness, extraction, dedup and storage.
Browser control is the remaining 10%, and most targets do not need it at all.

---

## 2. Object model

```
Source          one target site/API. Declares: discovery, fetch mode, extractors, policy, schema.
  Discovery     produces Task seeds  (rss | sitemap | api-cursor | listing-page)
  Task          { taskId, sourceId, kind, url|params, depth, priority, attempt, dedupKey }
  Fetcher       Task -> FetchResult { status, headers, body, finalUrl, fromCache, timings }
  Extractor     FetchResult -> Record[] | Task[]   (extraction and link discovery are separate)
  Record        typed, validated, versioned business object
  Sink          Record[] -> storage (idempotent, upsert by dedupKey)
Frontier        durable queue: pending / in-flight / done / dead-letter, per-source partitioned
Policy          rate limit, concurrency, retry, timeouts, freshness, authorization
```

Hard invariants:

- A `Task` is **idempotent**. Running it twice produces the same `Record` set (modulo `fetched_at`).
- `dedupKey` is derived from **stable identity**, never from the full URL (URLs carry slugs, tracking
  params, and A/B variants). See §6.
- Extraction is a **pure function** of `FetchResult` — no network, no clock, no randomness. This is
  what makes golden-file tests possible. Inject `fetched_at` from the caller.
- Discovery and extraction never write to storage directly; only the `Sink` writes.

---

## 3. Fetch strategy — the HTTP-first ladder

Decide per URL pattern, cache the decision, and record it in the connector config:

| Rung | Method | Use when | Cost |
|---|---|---|---|
| 0 | Structured feed (RSS / sitemap / JSON API) | Site publishes one | ~1 ms |
| 1 | Plain HTTP GET + HTML parse | Server-rendered content | ~5 ms |
| 2 | HTTP GET + extract embedded JSON (`__NEXT_DATA__`, JSON-LD, inline state) | SPA that still ships data in HTML | ~5 ms |
| 3 | HTTP call to the site's own XHR/JSON endpoint, discovered from the network log | Content loads via a documented-in-page API | ~10 ms |
| 4 | **Browser (BCP)** | Content genuinely requires JS execution, or the flow needs a real session | ~300–2000 ms, ~80 MB RAM |

**Mandatory promotion rule:** a connector starts at the lowest rung that works and is promoted only
when a test proves the lower rung fails. Write the evidence in the connector README
(`why-browser.md`) — one paragraph, with the failing fixture. No evidence, no browser.

Rung-4 cost is not theoretical: at rung 1 one worker sustains hundreds of pages/minute; at rung 4 it
is a handful, and you are paying for a Chromium per concurrency slot.

---

## 4. Compliance engine (mandatory, non-bypassable)

Every outbound request passes through `Compliance.authorize(task) -> Allow | Deny(reason) | Delay(ms)`.

### 4.1 robots.txt

- Fetch `/robots.txt` per host, cache 24 h, re-fetch on expiry, fail **closed** on a 5xx (deny until
  fetched successfully). A 404 means no restrictions.
- Group selection: match the **User-Agent token the crawler actually declares**. Implement standard
  longest-match precedence for `Allow`/`Disallow`.
- Honor `Crawl-delay` when present; when absent use the connector's own limit (§4.3), never faster.
- **The declared User-Agent must be honest and stable.** Choosing or changing a UA token in order to
  land in a more permissive robots group is prohibited — that is circumventing the rule, not reading
  it. If the site names your crawler technology in a `Disallow` group, that group applies to you.
- UA format: `<ProductName>/<version> (+https://<contact-page>)`. A contact URL or email is required;
  it is the difference between a rate-limit conversation and a block.

### 4.2 Authorization gate (source classification)

Every `Source` declares `access_class`, and the engine enforces it:

| Class | Meaning | Allowed fetch modes |
|---|---|---|
| `public` | Public content, robots allows, no login, no ToS restriction on reading | HTTP, browser |
| `feed` | Publisher-provided feed/sitemap intended for machine consumption | HTTP |
| `api` | Vendor API under credentials you legitimately hold | API only |
| `restricted` | Requires login, or ToS forbids automated access, or the site actively gates it | **Blocked** unless `authorization_ref` points to a documented agreement/ownership record |

A `restricted` source without `authorization_ref` fails at config-load time, not at runtime.

### 4.3 Rate limiting and politeness (defaults; per-source override allowed only downward)

- 1 in-flight request per host by default; max 2.
- ≥ 1000 ms between requests to the same host; token bucket, jittered.
- Honor `429` and `503` + `Retry-After` exactly. On repeated 429, halve the rate and do not restore it
  for 30 minutes.
- Circuit breaker per host: 5 consecutive 5xx or 3 consecutive 429 → open for 10 min, then half-open.
- Conditional GET everywhere: store `ETag` / `Last-Modified`, send `If-None-Match` /
  `If-Modified-Since`, treat `304` as success-unchanged. Accept gzip/br.
- Global kill switch per source, hot-reloadable.

> These defaults are what keep the crawler welcome. A crawler that gets blocked usually got blocked
> for hammering, not for being detected as automated.

---

## 5. Extraction contract

### 5.1 Declarative rules

Extractors are data, not code, wherever possible:

```yaml
record: Article
version: 3
fields:
  title:
    - jsonld: NewsArticle.headline
    - meta: og:title
    - css: "h1.title-detail"
      text: true
  published_at:
    - jsonld: NewsArticle.datePublished
    - meta: "article:published_time"
    - css: "span.date"
      parse: vn_datetime
    required: true
  body_html:
    - css: "article.fck_detail"
      html: true
      remove: ["script", "style", ".box-tinlienquan", "figure.tplCaption > figcaption"]
    required: true
```

**Fallback chain is mandatory** for every field: structured data first (JSON-LD / microdata),
then meta tags, then CSS/XPath last. Sites redesign their DOM far more often than they change their
JSON-LD or OpenGraph tags — this ordering is the single biggest driver of crawler lifespan.

### 5.2 Typed records + validation

Each `Record` type has a schema (JSON Schema or the stack's equivalent) with required fields, types
and ranges. A record failing validation goes to the **quarantine** table with the raw HTML reference —
never silently dropped, never written half-populated.

### 5.3 Versioning

`extractor_version` is stored on every record. Bumping it enables re-extraction from stored raw HTML
without re-fetching. **Store the raw response body** (compressed, with a TTL) — re-crawling to fix an
extraction bug is the most common avoidable cost in this kind of system.

### 5.4 Golden fixtures

Per extractor: ≥ 5 recorded real pages covering the normal case plus known variants (photo article,
liveblog, video article, paywalled/teaser, redirect). Test asserts extracted output equals a checked-in
golden JSON. Refresh fixtures monthly via a script; a diff in CI is the early warning that the site
changed.

---

## 6. Identity, dedup, incremental

- **Canonicalize URL**: lowercase host, strip fragment, strip tracking params
  (`utm_*`, `fbclid`, `gclid`, `source`, `ref`), resolve to `<link rel="canonical">` when present,
  follow redirects and store `final_url`.
- **dedupKey** = stable site-native identity where one exists (e.g. the numeric article ID in the
  URL), else `sha256(canonical_url)`. Never the slug — slugs get rewritten after publication.
- **content_hash** = sha256 of normalized `body_text` + `title`. Unchanged hash on re-fetch → update
  `last_seen_at` only, do not write a new version, do not re-notify downstream.
- **Change history**: when `content_hash` changes, write a new row in `article_versions` (news
  articles are edited after publication; capturing that is usually the point of crawling them).
- **Incremental**: poll the feed / sitemap `lastmod`. Full re-crawl only on demand.
- **Backfill** is a separate job class with its own lower priority and its own rate budget, so it can
  never starve the incremental path.

---

## 7. Reliability

Retry matrix, keyed on BCP `error.kind` (BCP §4.5) and HTTP status — never on message text:

| Condition | Retry | Backoff | Max | Then |
|---|---|---|---|---|
| Connection reset / DNS / `NETWORK_ERROR` | yes | exp 1s→60s, jitter | 5 | dead-letter |
| HTTP 429 / 503 + `Retry-After` | yes | honor header | 3 | open circuit |
| HTTP 5xx | yes | exp 2s→120s | 4 | dead-letter |
| HTTP 404 / 410 | no | — | — | mark gone, stop re-queuing |
| HTTP 401 / 403 | no | — | — | alert; do **not** retry with different identity |
| `TIMEOUT`, `TARGET_CRASHED`, `TRANSPORT_CLOSED` | yes | exp | 3 | dead-letter |
| `ELEMENT_NOT_FOUND` after full wait | no | — | — | quarantine + extractor-drift alert |
| Validation failure | no | — | — | quarantine |

- **Never respond to 403 by changing identity, UA, or IP.** A 403 is an answer. Alert a human.
- Checkpoint the frontier durably; a process restart resumes without re-fetching completed tasks.
- Dead-letter queue is inspectable and re-drivable. Empty DLQ ≠ healthy; alert on DLQ growth rate.
- Every job run gets a `run_id`; every record records the `run_id` that produced it.

---

## 8. Storage schema (minimum)

```
sources(source_id, name, access_class, authorization_ref, config_hash, enabled)
tasks(task_id, source_id, dedup_key, kind, url, state, attempt, priority,
      next_run_at, last_error_kind, run_id)
raw_responses(response_id, task_id, fetched_at, status, headers_json,
              body_blob_ref, content_hash, ttl_at)
articles(article_id, source_id, dedup_key, canonical_url, title, lead, body_text,
         body_html_ref, published_at, updated_at, authors_json, category_path,
         tags_json, images_json, content_hash, extractor_version,
         first_seen_at, last_seen_at, run_id)
article_versions(article_id, content_hash, captured_at, diff_ref)
quarantine(id, task_id, reason, validation_errors_json, response_id, created_at)
```

---

## 9. Observability and data-quality alarms

Operational metrics alone are not enough for a crawler — a crawler that is "up" while extracting
nothing is the normal failure mode.

- Per source per field: **extraction success rate**. Alert when any required field drops > 10
  percentage points over a 1 h window → the site changed its DOM.
- Records/hour vs the same hour last week; alert on a > 50% drop.
- HTTP status histogram; alert on any 403, or on 429 rate > 1%.
- Fetch rung distribution — alert if browser-rung share rises (a silent cost explosion).
- Freshness lag: `now - max(published_at)` per source.
- p95 fetch and extract duration, frontier depth, DLQ depth, quarantine rate.

---

## 10. Testing

- **No network in CI.** All connector tests run against recorded fixtures.
- Unit: canonicalization, dedupKey, robots matcher (use the standard robots test corpus), retry matrix,
  rate limiter.
- Golden: extractor output vs checked-in JSON (§5.4).
- Contract: every `Source` implementation runs a shared conformance test (implements all interface
  methods, declares `access_class`, config validates, no direct network call outside `Fetcher`).
- Integration (nightly, real network, tiny budget): 10 URLs per source, assert required fields present.
  This is what catches a site redesign before the alarm does.

---

## 11. Connector A — VnExpress (`access_class: public` + `feed`)

Facts below were checked live on 2026-08-28.

### 11.1 Compliance findings **[VERIFIED 2026-08-28]**

`https://vnexpress.net/robots.txt`:

- `User-agent: *` → `Allow: /`. General crawling of article pages is permitted for a crawler that is
  not named in a more specific group.
- A sitemap index is declared, plus a Google News sitemap and an images sitemap.
- The file contains an explicit **deny-list of named bot tokens**, including AI-training crawlers
  (`GPTBot`, `ClaudeBot`, `Claude-Web`, `Google-Extended`, `Applebot-Extended`, `CCBot`,
  `Bytespider`, `cohere-ai`, `anthropic-ai`) and scraper/aggregator tokens (`Scrapy`, `Diffbot`,
  `archive.org_bot`, `NewsNow`, `Quora-Bot`, `TurnitinBot`, and others). Certain search-assistant
  bots and `Facebookexternalhit` are allowed.

Two consequences the implementer must encode, not paraphrase:

1. **If the crawler is built on a framework named in the deny-list and declares that token, the deny
   applies.** Declare your own honest product token; do not declare a framework token you are not, and
   do not swap tokens to escape a rule.
2. **The site has explicitly refused AI-training crawlers.** If the purpose of this pipeline is
   building a training corpus, VnExpress has already answered. Route that use case to a licensing
   conversation, not to a crawler.

Set: `user_agent: "<YourProduct>Bot/1.0 (+https://<your-domain>/bot)"`, `crawl_delay: 1s`,
`max_concurrency_per_host: 1`.

### 11.2 Discovery **[VERIFIED 2026-08-28]**

Two independent channels — implement both; RSS for latency, sitemap for completeness.

**RSS** — `https://vnexpress.net/rss/<feed>.rss`:

`tin-moi-nhat` (latest), `tin-noi-bat` (featured), `tin-xem-nhieu` (most-read), `the-gioi`, `thoi-su`,
`kinh-doanh`, `giai-tri`, `the-thao`, `phap-luat`, `giao-duc`, `goc-nhin`, `bat-dong-san`, `suc-khoe`,
`gia-dinh`, `du-lich`, `khoa-hoc-cong-nghe`, `oto-xe-may`, `y-kien`, `tam-su`, `vne-go`, `thu-gian`,
`spotlight`.

`<item>` fields present: `title`, `link`, `guid` (equals link), `description` (HTML: thumbnail + lead),
`pubDate` (RFC 2822), `enclosure` (image url/type/length). The category feed the item came from is the
category signal — RSS items do not carry a reliable `<category>` element.

Poll `tin-moi-nhat` every 5 min; each category feed every 15 min, staggered.

**Sitemap** — `https://vnexpress.net/sitemap.xml` is a **sitemap index** containing:
`categories-sitemap.xml`, `google-news-sitemap.xml`, and date-partitioned article sitemaps (per
month, and per day for the current period) with `lastmod` timestamps in `+07:00`, going back to at
least 2024-10.

Incremental: read the index, fetch only child sitemaps whose `lastmod` is newer than the last run.
Backfill: walk the date-partitioned children oldest-to-newest as a low-priority job class.

### 11.3 URL pattern and identity **[VERIFIED 2026-08-28]**

```
https://vnexpress.net/<slug>-<id>.html
e.g. https://vnexpress.net/chay-dua-cuu-nguoi-sau-lu-quet-nepal-trung-quoc-5114372.html
```

- `id` is numeric, currently 7 digits. **Do not hardcode the digit count** — older articles have
  shorter ids. Regex: `-(\d{6,9})\.html$`.
- **`dedupKey` = that numeric id.** The slug is regenerated when an editor changes the headline, so a
  URL-hash dedup key will duplicate the same article.
- Canonicalize: strip everything after `.html`, drop query and fragment, prefer `rel=canonical`.

### 11.4 Fetch rung

**Rung 1 (plain HTTP).** Article pages are server-rendered; RSS and sitemaps are static XML.
No browser. If a specific article type (interactive/longform/video) fails the rung-1 check, add a
narrow URL-pattern override to rung 4 with a fixture proving it — do not promote the whole connector.

### 11.5 Record schema (`Article`, version 1)

```
article_id (int, from URL)   canonical_url        title           lead/description
body_html                    body_text            published_at    updated_at
authors[]                    category_path[]      tags[]          images[{url,caption}]
source_id="vnexpress"        fetched_at           extractor_version   content_hash
```

Extraction ordering per §5.1: JSON-LD `NewsArticle` → OpenGraph/meta (`og:title`, `og:description`,
`og:image`, `article:published_time`) → CSS. **[VERIFY]** which of these VnExpress emits today, from a
recorded fixture — do not assume from this document, and do not paste selectors from memory.

Body cleanup: drop related-article boxes, ad slots, share widgets, "Xem thêm" blocks; keep figure
captions as structured `images[].caption`; normalize whitespace before hashing.

Timezone: publication times are Vietnam local (`+07:00`). Store UTC, keep the original offset.

### 11.6 Definition of done

Robots respected with an honest UA; both discovery channels running; 500 articles crawled with
required-field extraction rate ≥ 99%; 5+ golden fixtures; re-run produces zero duplicate rows;
edit-detection produces a second `article_versions` row on a known edited article.

---

## 12. Connector B — Shopee (`access_class: api`)

### 12.1 Decision

Shopee is specified here as an **authorized API connector, not a browser crawler.**

Reasons, in order of weight: (1) product, price and stock data for a shop you operate is available
through the official API in a clean, paginated, typed form — a crawler is a worse version of a
solved problem; (2) automated access to the storefront is restricted by their terms, so it falls in
`restricted` and the §4.2 gate blocks it without a documented authorization; (3) the browser path is
strictly more expensive and more fragile.

I am not specifying a way around Shopee's access controls, and no such code belongs in this repo.
If you hold a partnership that grants storefront access beyond the API, record it in
`authorization_ref` and we spec that separately against what the agreement actually permits.

### 12.2 What the official route gives you

- **Shop APIs** (for shops you own or are authorized to manage): product list/detail, stock and price,
  orders, logistics, returns, shop performance, **and buyer ratings/reviews on your own items plus
  seller replies** (see §12.6).
- **Affiliate / marketing APIs**: product and campaign data under an affiliate agreement.
- Coverage boundary: it does **not** give arbitrary competitor catalogue or competitor review data. If
  that is the real requirement, this is a data-licensing problem, not an engineering one — say so
  early rather than building a crawler that will be blocked and will produce partial, unreliable data
  anyway. Licensed VN e-commerce market-intelligence vendors exist for exactly this; buying the feed
  is cheaper than losing the arms race. **[VERIFY current vendors and coverage.]**

### 12.3 Integration outline **[VERIFY all specifics against current Shopee Open Platform docs — versions and signature rules change]**

- Register a partner app → `partner_id` + `partner_key`. Sandbox host and production host differ.
- Shop authorization: redirect the shop owner to Shopee's auth URL → receive `code` → exchange for
  `access_token` + `refresh_token`, scoped per `shop_id`.
- Tokens are short-lived (on the order of hours). Implement **proactive refresh** on a timer with a
  safety margin, plus a single-flight lock so concurrent workers cannot trigger a refresh storm and
  invalidate each other's token.
- Every request is signed: HMAC-SHA256 over a base string of (path, `partner_id`, timestamp,
  `access_token`, `shop_id`) using `partner_key`. **[VERIFY the exact base-string composition per
  endpoint class — it differs between shop-level and public endpoints.]**
- Request timestamps must be within the allowed skew: sync clocks, use server time, never local time
  in a container with drift.
- Pagination is cursor/offset per endpoint; always drive to exhaustion and checkpoint the cursor.
- Rate limits are per partner and per shop; treat their limit response exactly like §4.3 and back off.

### 12.4 How it reuses this layer

`ApiFetcher` implements the same `Fetcher` interface, so the connector inherits the frontier,
retry matrix, checkpointing, dedup, quarantine, sink and metrics for free. This is the payoff of §2:
one runtime, three fetch modes.

Record types: `ShopProduct`, `ShopOrder`, `StockSnapshot` — each with `shop_id` in the dedup key,
and time-series snapshots for price/stock rather than destructive updates.

### 12.5 Definition of done

OAuth + refresh survives a 24 h soak with zero auth failures; full product catalogue of one
authorized shop synced with 0 gaps; rate-limit responses handled without a single retry storm;
credentials in a secret store, never in config files or logs.

---

### 12.6 Sub-connector: shop reviews / ratings (own or authorized shops)

This is a first-class, fully in-scope feature. It is specified via the API, not via the storefront.

**Source** **[VERIFY exact endpoint names and versions against current Open Platform docs]**:
the product comment endpoints (list buyer comments/ratings for the authorized shop's items, and post
seller replies), plus order-level rating fields. Everything below is transport-agnostic and survives an
endpoint rename.

**Record `ShopReview` (version 1)**

```
review_id          shop_id            item_id           model_id (variant)
rating_star (1-5)  comment_text       media[] {type,url}
buyer_ref          create_time        edit_time         order_ref (if exposed)
reply_text         reply_time         reply_by
status             { visible | hidden | removed }
fetched_at         last_seen_at       content_hash      extractor_version
```

- `dedupKey = (shop_id, review_id)`.
- `content_hash` over `rating_star + comment_text + reply_text` — a change means the buyer edited the
  review or you replied; write an `article_versions`-style history row (§6) so rating trends stay
  auditable.

**Incremental strategy** — reviews are append-mostly but **not** append-only. Buyers edit them, sellers
reply later, and Shopee can hide or remove them. Naive cursor-only sync silently rots.

1. Forward sync: cursor by `create_time` descending until you hit a review you already have; checkpoint
   the cursor per `item_id`.
2. Rolling re-scan: re-read the last 30 days every night to catch edits and replies.
3. Disappearance handling: **never hard-delete.** A review present yesterday and absent today gets
   `status = removed`, `last_seen_at` frozen. Hard-deleting makes rating-trend analysis lie.
4. Rate limits are per shop — sync item-by-item through the §4.3 limiter, not in a burst.

**Personal data — this is the part crawl plans usually miss.** Reviews are personal data: a buyer
identifier, their purchase, their opinion, sometimes their photo. Vietnam's Nghị định 13/2023/NĐ-CP
(personal data protection) applies to processing it. **[VERIFY with counsel]**; the engineering
requirements that follow regardless:

- Purpose limitation — declare in the connector config what the data is for (e.g. quality triage,
  reply-SLA reporting). Do not repurpose it later without revisiting the basis.
- Store `buyer_ref` as a salted hash unless the raw identifier is genuinely required to reply to that
  buyer. For aggregate analytics, the hash is enough.
- Do not store buyer avatars or profile media unless a named feature needs them.
- Retention policy with an actual TTL job, not an aspiration. Suggested: raw review text 24 months,
  aggregates indefinitely.
- Never re-publish reviewer identities outside the shop context, and never merge review data with
  other datasets to profile individuals.
- Reviews go in a separate table with its own access control and audit log, not into a general
  analytics dump everyone can read.

**What this actually buys you** (the reason to build it): per-SKU rating trend over time, negative-review
triage queue, reply-SLA tracking, defect keyword extraction from `comment_text`, and correlation of
rating drops with a specific batch or variant. All of that needs only your own shop's data.

**DoD**: full review history of one authorized shop synced with zero gaps; an edited review produces a
history row rather than an overwrite; a removed review flips to `status = removed` and is never
resurrected; buyer identifiers hashed; retention job proven on a seeded old record.

---

## 13. Connector config format

```yaml
source_id: vnexpress
access_class: public
enabled: true
user_agent: "AcmeBot/1.0 (+https://acme.example/bot)"
politeness:
  max_concurrency_per_host: 1
  min_interval_ms: 1000
  respect_crawl_delay: true
discovery:
  - kind: rss
    urls: ["https://vnexpress.net/rss/tin-moi-nhat.rss", "..."]
    interval: 5m
  - kind: sitemap_index
    url: "https://vnexpress.net/sitemap.xml"
    interval: 30m
    incremental_by: lastmod
fetch:
  default_rung: http
  overrides:
    - url_pattern: "^https://vnexpress\\.net/interactive/"
      rung: browser
      justification_doc: "connectors/vnexpress/why-browser.md"
identity:
  id_regex: "-(\\d{6,9})\\.html$"
  dedup_key: "match:1"
extract:
  record: Article
  rules_file: "connectors/vnexpress/article.rules.yaml"
sink:
  table: articles
  on_conflict: upsert_by_dedup_key
  version_on_content_change: true
```

Config is schema-validated at load. An unknown key is an error, not a warning.

---

## 14. Task checklist

```
C1   Frontier + durable task store + run_id                                     -
C2   Compliance engine: robots parser/cache, UA policy, access_class gate       -
C3   Rate limiter, circuit breaker, conditional GET                             (dep C2)
C4   HttpFetcher                                                                (dep C3)
C5   Fetcher interface + BrowserFetcher over BCP Page API                       (dep C4, BCP T21)
C6   Raw response store (compressed, TTL) + re-extract-from-raw job             (dep C4)
C7   Declarative extractor engine: jsonld/meta/css chain, transforms, cleanup   (dep C6)
C8   Record schemas + validation + quarantine                                   (dep C7)
C9   Canonicalization, dedupKey, content_hash, article_versions                 (dep C8)
C10  Sink: idempotent upsert                                                    (dep C9)
C11  Retry matrix + DLQ + resume-from-checkpoint                                (dep C1,C4)
C12  Metrics + data-quality alarms (§9)                                         (dep C10)
C13  Test harness: fixture recorder, golden runner, Source conformance suite    (dep C7)
C14  Connector vnexpress: discovery (rss + sitemap index)                       (dep C4,C13)
C15  Connector vnexpress: extractor + 5 fixtures + goldens                      (dep C14)
C16  Connector vnexpress: soak 500 articles, verify DoD §11.6                   (dep C15)
C17  ApiFetcher: signing, token refresh single-flight, cursor checkpointing     (dep C11)
C18  Connector shopee-api: auth flow + secret store                             (dep C17)
C19  Connector shopee-api: product/stock sync + snapshots, DoD §12.5            (dep C18)
C20  Nightly live-integration job (10 URLs/source) + drift alerting             (dep C12,C16)
C21  Shopee reviews §12.6: forward cursor + 30d re-scan + removal handling      (dep C19)
C22  PII handling for reviews: buyer_ref hashing, retention job, access control (dep C21)
```

---

## 15. Open questions

1. Storage target: Postgres, ClickHouse, object store + warehouse? Drives §8.
2. Retention for `raw_responses` — it is the single biggest storage line, and the thing that saves
   you from re-crawling. Suggested default: 90 days.
3. Volume target per source (articles/day, shops, SKUs)? Determines whether the frontier can be a
   database table or needs a real queue.
4. Is edit-tracking (`article_versions`) actually needed, or is latest-wins enough?
5. Scheduler runtime: does this run under the existing job system, or standalone?
6. For Shopee: which shops, and is the authorization shop-ownership or an affiliate agreement? That
   answer decides which API surface §12.2 you get.
