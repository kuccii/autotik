import math
import time

import httpx

API_URL = "https://www.tikwm.com/api/feed/list"
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
