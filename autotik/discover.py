import math
import re
import time
import unicodedata

import httpx

API_URL = "https://www.tikwm.com/api/feed/list"
CHALLENGE_SEARCH_URL = "https://www.tikwm.com/api/challenge/search"
CHALLENGE_POSTS_URL = "https://www.tikwm.com/api/challenge/posts"
RETRY_DELAYS = (5, 15, 45)
BACKOFF_STATUSES = {429, 500, 502, 503, 531}
REGION_SPACING = 1.1  # tikwm free tier allows 1 request/second


class DiscoveryError(RuntimeError):
    pass


def fetch_feed(client, regions, count):
    if isinstance(regions, str):
        regions = (regions,)
    regions = tuple(regions)
    if not regions:
        raise DiscoveryError("no discovery regions configured")
    merged = []
    errors = []
    for idx, region in enumerate(regions):
        if idx:
            time.sleep(REGION_SPACING)
        try:
            merged.extend(_fetch_one(client, region, count))
        except DiscoveryError as exc:
            errors.append(str(exc))
            if _rate_limited(str(exc)):
                time.sleep(10)
                continue
    if not merged:
        raise DiscoveryError(errors[-1] if errors else "no discovery regions configured")
    return merged


def _rate_limited(message):
    lowered = message.lower()
    return "http 429" in lowered or "http 531" in lowered or "limit" in lowered


def _fetch_one(client, region, count):
    last = "unknown"
    for attempt, delay in enumerate((0,) + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            resp = client.get(
                API_URL,
                params={"region": region, "count": count},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            last = f"http error: {exc}"
            continue
        if resp.status_code in BACKOFF_STATUSES:
            last = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}"
            continue
        try:
            body = resp.json()
        except ValueError:
            last = "non-JSON body"
            continue
        if body.get("code") != 0:
            last = f"code={body.get('code')} msg={body.get('msg')}"
            continue
        data = body.get("data") or []
        if not data:
            last = "empty data"
            continue
        return data
    raise DiscoveryError(
        f"tikwm feed for region {region} failed after retries: {last}"
    )


def _get_json(client, url, params, what):
    """GET a tikwm endpoint, retrying the same way the region feed does."""
    last = "unknown"
    for delay in (0,) + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            resp = client.get(url, params=params, timeout=30.0)
        except httpx.HTTPError as exc:
            last = f"http error: {exc}"
            continue
        if resp.status_code in BACKOFF_STATUSES:
            last = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}"
            continue
        try:
            body = resp.json()
        except ValueError:
            last = "non-JSON body"
            continue
        if body.get("code") != 0:
            last = f"code={body.get('code')} msg={body.get('msg')}"
            continue
        return body.get("data")
    raise DiscoveryError(f"{what} failed after retries: {last}")


def resolve_challenge(client, tag):
    """Map a hashtag to its numeric challenge id.

    tikwm returns near-duplicate names ('funny', 'funny_', 'funny 😂') for one
    query, so prefer an exact case-insensitive match and fall back to the first
    result only when there is none.
    """
    name = str(tag or "").strip().lstrip("#")
    if not name:
        return None
    try:
        data = _get_json(
            client,
            CHALLENGE_SEARCH_URL,
            {"keywords": name},
            f"tikwm challenge search for {name!r}",
        ) or {}
    except DiscoveryError:
        return None
    wanted = name.lower()
    entries = data.get("challenge_list") or []
    for entry in entries:
        if (entry.get("cha_name") or "").strip().lower() == wanted:
            return str(entry["id"]) if entry.get("id") else None
    first = entries[0] if entries else {}
    return str(first.get("id")) if first.get("id") else None


def fetch_hashtags(client, tags, pages=3, spacing=REGION_SPACING):
    """Collect videos from TikTok hashtag feeds, cursor by cursor.

    Region feeds are not really regional: asking tikwm for FR/GB returned 16
    countries and almost no French content. Hashtag feeds are the opposite --
    the tag *is* the niche, and each video carries a genuine source region we
    can gate on. Pagination is cursor-based and effectively unbounded, so a
    handful of tags out-yields the same number of regions by an order of
    magnitude.
    """
    if isinstance(tags, str):
        tags = (tags,)
    tags = tuple(t for t in tags if str(t).strip())
    if not tags:
        raise DiscoveryError("no discovery hashtags configured")
    merged = []
    seen = set()
    errors = []
    for idx, tag in enumerate(tags):
        if idx:
            time.sleep(spacing)
        challenge_id = resolve_challenge(client, tag)
        if not challenge_id:
            errors.append(f"no challenge id for #{tag}")
            continue
        cursor = 0
        for page in range(max(1, pages)):
            if page:
                time.sleep(spacing)
            try:
                data = _get_json(
                    client,
                    CHALLENGE_POSTS_URL,
                    {"challenge_id": challenge_id, "count": 30, "cursor": cursor},
                    f"tikwm challenge posts for #{tag}",
                ) or {}
            except DiscoveryError as exc:
                errors.append(str(exc))
                break
            videos = data.get("videos") or []
            if not videos:
                break
            for raw in videos:
                video_id = (raw or {}).get("video_id")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                merged.append(raw)
            next_cursor = data.get("cursor")
            if not data.get("hasMore") or next_cursor in (None, cursor):
                break
            cursor = next_cursor
    if not merged:
        raise DiscoveryError(
            errors[-1] if errors else "hashtag discovery returned nothing"
        )
    return merged


def canonical_url(author, video_id):
    return f"https://www.tiktok.com/@{author}/video/{video_id}"


def parse_items(payload, regions=None):
    allowed = None
    if isinstance(regions, str):
        allowed = {regions.upper()}
    elif regions is not None:
        allowed = {r.upper() for r in regions}
    items = []
    seen = set()
    for raw in payload:
        if raw.get("is_ad"):
            continue
        if allowed is not None and (raw.get("region") or "").upper() not in allowed:
            continue
        author = raw.get("author") or {}
        unique_id = author.get("unique_id") or ""
        video_id = raw.get("video_id")
        raw_title = raw.get("title")
        if not isinstance(raw_title, str):
            continue
        title = raw_title.strip()
        if not video_id or not unique_id or not raw.get("play") or not title:
            continue
        if video_id in seen:
            continue
        seen.add(video_id)
        items.append(
            {
                "video_id": video_id,
                "author": unique_id,
                "nickname": author.get("nickname") or unique_id,
                "region": raw.get("region"),
                "desc": title,
                "play_url": raw["play"],
                "digg_count": int(raw.get("digg_count") or 0),
                "play_count": int(raw.get("play_count") or 0),
                "share_count": int(raw.get("share_count") or 0),
                "duration": int(raw.get("duration") or 0),
                "source_url": canonical_url(unique_id, video_id),
                "create_time": int(raw.get("create_time") or 0),
                "score": 0.0,
            }
        )
    return items


def filter_by_age(items, now, max_age_days):
    kept = []
    for item in items:
        created = item.get("create_time") or 0
        if created and (now - created) / 86400.0 > max_age_days:
            continue
        kept.append(item)
    return kept


def filter_by_likes(items, min_likes):
    if min_likes <= 0:
        return items
    return [i for i in items if (i.get("digg_count") or 0) >= min_likes]


def filter_by_regions(items, allowed):
    """Keep only videos TikTok attributes to the account's target markets.

    This is the gate that makes the pivot real. Hashtag feeds still return a
    majority of US videos (the global default), which is exactly the mismatch
    we set out to fix, so FR/UK targeting needs an explicit source-region
    allow-list. An empty allow-list disables the gate.
    """
    if isinstance(allowed, str):
        allowed = (allowed,)
    codes = {str(c).strip().upper() for c in (allowed or ()) if str(c).strip()}
    if not codes:
        return items
    return [i for i in items if (i.get("region") or "").upper() in codes]


def _fold(text):
    """Lowercase and strip accents so 'scene' matches 'scène'."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def filter_by_keywords(items, keywords):
    """Keep only items whose caption mentions the account's niche.

    An empty keyword list means no filter, so the pipeline still works without
    a niche configured. Matching is accent- and case-insensitive and respects
    word boundaries, so 'filming' does not match 'film'.
    """
    terms = [t for t in (keywords or ()) if str(t).strip()]
    if not terms:
        return items
    patterns = [
        re.compile(r"\b" + re.escape(_fold(t).strip()) + r"\b", re.IGNORECASE)
        for t in terms
    ]
    kept = []
    for item in items:
        haystack = _fold(item.get("desc") or "")
        if any(p.search(haystack) for p in patterns):
            kept.append(item)
    return kept


def filter_by_duration(items, max_seconds):
    """Drop videos longer than the cap. A slot is worth ~90s of viewer attention,
    and a long source costs minutes of ffmpeg: a 5:52 clip once held the encode
    for over an hour. Unknown durations (0) are kept."""
    if max_seconds <= 0:
        return items
    return [
        i for i in items
        if not (i.get("duration") or 0) or (i["duration"] <= max_seconds)
    ]


def score_item(item, now):
    er = item["digg_count"] / max(item["play_count"], 1)
    age_d = (
        max((now - item["create_time"]) / 86400.0, 0.25)
        if item.get("create_time")
        else 7.0
    )
    vph = item["play_count"] / (age_d * 24.0)
    freshness = 1.0 if age_d <= 7 else 0.5 if age_d <= 21 else 0.15
    return round(
        0.35 * min(er / 0.08, 1.0)
        + 0.35 * min(math.log10(max(vph, 1)) / 6, 1.0)
        + 0.30 * freshness,
        6,
    )


def rank(items, keep, now=None):
    if now is None:
        now = time.time()
    for item in items:
        item["score"] = score_item(item, now)
    return sorted(items, key=lambda i: i["score"], reverse=True)[:keep]
