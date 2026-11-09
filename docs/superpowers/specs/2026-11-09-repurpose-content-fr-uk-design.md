# Repurpose content for the French/UK audience

Date: 2026-11-09
Status: approved (approach A — hard pivot)

## Problem

Studio reports the account's viewers are ~70% France, then UK, but discovery
sources content from `RW,KE,NG,ZA,TZ,US,GB,CA,IN`. Posts therefore look like
`#peller #chidimma` — Nigerian/African trend content served to a French and
British audience. Low relevance suppresses engagement, which suppresses
distribution.

Two measured facts frame the problem:

| arm | videos | restricted for For You | avg views |
|---|---|---|---|
| none (pre-experiment) | 19 | 8 (42%) | 281 |
| control | 18 | 14 (78%) | 282 |
| heavy | 12 | 10 (83%) | 282 |

Followers: 653 -> 906 -> 1000, then flat for four days.

The heavy transform experiment is answered: it does not reduce restriction
(83% vs control's 78% at n=30) and costs roughly ten times the encode time.

## Decision

Change *what we source*, not how we caption it. Target FR/UK, locked to a
comedy / movie-clip niche.

## Changes

1. `DISCOVER_REGIONS=FR,GB,BE,CH` — BE/CH are French-speaking and share FR
   trending audio.
2. New `DISCOVER_KEYWORDS` niche filter over candidate captions, applied after
   the age/duration/likes filters and before ranking. Matching is accent- and
   case-insensitive with word boundaries (`scène` matches `scene`, `filming`
   does not match `film`). Empty list disables the filter.
   This is a filter over trending feeds because tikwm's `feed/search` endpoint
   is Cloudflare-blocked from the VPS; yield will be lower than generic
   trending and the keyword list gets tuned from real output.
3. `TRANSFORM_VARIANT_RATIO=0` — heavy arm retired.
4. New `DISCOVER_MAX_DURATION_S=90` plus a hard `ENCODE_TIMEOUT_S=300` in
   `transform.transform`. A 5:52 source held one encode for 62 minutes and
   wedged a container for an hour; a movie-clip niche will hit this constantly.
5. Purge the 51 approved videos sourced from the old regions to `skipped`, so
   the six daily slots go to on-audience content instead of delaying the pivot
   by about eight days.

## Risk

Movie clips are among the most aggressively rights-enforced content on TikTok.
Comedy is considerably safer. If restriction rises above the current ~75%, the
response is to narrow `DISCOVER_KEYWORDS` to comedy terms only. The filter is
env-configurable rather than hardcoded for exactly this reason.

Repurposing improves relevance among the ~25% of posts that do get distributed
and gives the algorithm a coherent audience signal. It does not by itself break
the restriction ceiling; that remains a separate, unresolved problem.

## Verification

- Unit tests for both filters (match, accents, word boundaries, empty list,
  duration edge at exactly the cap, unknown duration kept, disabled at 0).
- Unit test proving `transform` aborts when ffmpeg exceeds the timeout.
- Live discovery run on the VPS: confirm the niche filter logs
  `niche filter: N/M keep (...)` and that staged rows come from FR/GB/BE/CH.
- Confirm the next scheduled publish still completes end to end.

## Revision 1 — the first design was wrong about the data source

The approved design assumed `DISCOVER_REGIONS=FR,GB,BE,CH` would make
discovery regionally targeted, with a keyword filter over the trending feed.
Live measurement killed that assumption:

```
4 regions x fetch=40  -> 39 raw items
returned regions      -> 16 countries; 1 FR, 7 GB, 11 US
after age             -> 16
after <=90s           -> 13
after >=500k likes    ->  2
after niche keywords  ->  1
```

Two independent problems:

1. **`feed/list?region=` is not a regional feed.** It returns a global mix and
   ignores `count`. Region targeting through it is fiction.
2. **Keyword filtering trending captions is a weak proxy.** Trending captions
   are hashtag soup (`#btsdisband #army #funny`), so a caption gate yields
   1-2 per run. Sustaining 6 posts/day this way is impossible.

### The actual fix: hashtag feeds

tikwm's `challenge/search` and `challenge/posts` endpoints are **not**
Cloudflare-blocked (only `feed/search` and `user/info` are), and
`challenge/posts` returns the same item schema as `feed/list`, cursor-paginated
and effectively unbounded. The hashtag *is* the niche, so no caption guessing
is needed, and each item still carries a real source region.

Measured over 20 tags x 3 pages: **1,033 unique videos, 463 fresh (<=30d), 308
under 90s**, with a source-region mix of FR 108 / US 83 / GB 10 — versus 2 FR
from the entire region-feed approach.

### Consequence: the likes floor had to change

At >=500k likes on that pool, **every surviving candidate was US and zero were
FR**; French creators on FR comedy tags top out near 230k likes. The 500k floor
and the FR/UK pivot are mutually exclusive.

| floor | total | FR | GB |
|---|---|---|---|
| >=100k | 27 | 8 | 0 |
| >=200k | 18 | 5 | 0 |
| >=500k | 5 | 0 | 0 |

User chose **>=100k**, which is the only floor that reliably produces French
content while remaining a real quality filter.

### What changed in code

- `discover.fetch_hashtags` (cursor pagination, dedupe, per-tag failure
  tolerance), `discover.resolve_challenge` (prefers the exact hashtag name over
  tikwm's near-duplicate suggestions), `discover._get_json` (shared retry).
- `discover.filter_by_regions` — a hard source-region allow-list. Without it,
  hashtag feeds still return a US majority, which is the original mismatch.
- Config: `DISCOVER_HASHTAGS`, `DISCOVER_HASHTAG_PAGES`,
  `DISCOVER_ALLOW_REGIONS` (defaults to `DISCOVER_REGIONS`).
- The caption keyword gate now applies only to the region-feed fallback, since
  hashtags already define the niche. `feed/list` is retained solely as a
  fallback when every hashtag fails.

Live run after deploy: `hashtag discovery: 971 usable from 20 tags`,
`region gate: 178/462 keep`, `staged 7`. The 15:00 UTC publish slot posted two
French comedy videos from that queue.
