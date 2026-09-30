import time

import httpx

API_URL = "https://www.tikwm.com/api/feed/list"
RETRY_DELAYS = (5, 15, 45)
BACKOFF_STATUSES = {429, 500, 502, 503, 531}


class DiscoveryError(RuntimeError):
    pass


def fetch_feed(client, region, count):
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
    raise DiscoveryError(f"tikwm feed failed after retries: {last}")


def canonical_url(author, video_id):
    return f"https://www.tiktok.com/@{author}/video/{video_id}"


def parse_items(payload, region):
    items = []
    for raw in payload:
        if raw.get("is_ad"):
            continue
        if raw.get("region") != region:
            continue
        author = raw.get("author") or {}
        unique_id = author.get("unique_id") or ""
        video_id = raw.get("video_id")
        if not video_id or not unique_id or not raw.get("play"):
            continue
        items.append(
            {
                "video_id": video_id,
                "author": unique_id,
                "nickname": author.get("nickname") or unique_id,
                "region": raw.get("region"),
                "desc": (raw.get("title") or "").strip(),
                "play_url": raw["play"],
                "digg_count": int(raw.get("digg_count") or 0),
                "play_count": int(raw.get("play_count") or 0),
                "share_count": int(raw.get("share_count") or 0),
                "duration": int(raw.get("duration") or 0),
                "source_url": canonical_url(unique_id, video_id),
                "score": 0.0,
            }
        )
    return items


def _norm(values):
    top = max(values, default=0)
    if top <= 0:
        return [0.0 for _ in values]
    return [v / top for v in values]


def rank(items, keep):
    digg = _norm([i["digg_count"] for i in items])
    play = _norm([i["play_count"] for i in items])
    share = _norm([i["share_count"] for i in items])
    for i, (d, p, s) in enumerate(zip(digg, play, share)):
        items[i]["score"] = round(0.5 * d + 0.3 * p + 0.2 * s, 6)
    return sorted(items, key=lambda i: i["score"], reverse=True)[:keep]
