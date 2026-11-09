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
