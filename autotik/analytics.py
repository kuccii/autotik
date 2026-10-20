"""Read our own TikTok Studio analytics.

Follower growth is decided by two things per post: whether TikTok lets it into
the For You feed at all ("not eligible for recommendation"), and whether
people watch past the first couple of seconds. Both are visible only on the
Studio analytics pages, so this module scrapes them and stores snapshots.

Everything here is read-only and runs against the operator's own account.
"""

import re

CONTENT_URL = "https://www.tiktok.com/tiktokstudio/analytics/content"
FOLLOWERS_URL = "https://www.tiktok.com/tiktokstudio/analytics/followers"
DETAIL_URL = "https://www.tiktok.com/tiktokstudio/analytics/{video_id}/overview"

_NUM = r"([\d,.]+)\s*([KMB])?"
_MULT = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}

# "01:03\n<caption>" in one cell, or a bare "01:03" in its own cell
_DUR_PREFIX = re.compile(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:\n|\r|$)")
_VIEWS_CELL = re.compile(r"^[\d,.]+\s*[KMB]?$", re.IGNORECASE)

_ROW_JS = """
() => Array.from(document.querySelectorAll('table tbody tr')).map(
    tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText))
"""


def _to_int(raw, suffix=""):
    try:
        value = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return int(value * _MULT.get((suffix or "").upper(), 1))


def parse_short_number(text):
    match = re.search(_NUM, text or "")
    return _to_int(match.group(1), match.group(2)) if match else None


def parse_duration(text):
    """mm:ss / h:mm:ss -> seconds."""
    parts = [p.strip() for p in (text or "").strip().split(":")]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def parse_content_rows(rows):
    """Studio content table -> [{caption, duration_s, views}].

    Recent Studio builds merge duration and caption into one cell
    ("01:03\\n<caption>"); older ones keep them in separate cells, so both
    shapes are accepted.
    """
    out = []
    for cells in rows or []:
        cells = [str(c or "") for c in cells]
        duration = None
        caption = ""
        start = 0
        for idx, cell in enumerate(cells):
            match = _DUR_PREFIX.match(cell)
            if match:
                duration = parse_duration(match.group(1))
                caption = cell[match.end():].strip()
                start = idx + 1
                if not caption and start < len(cells):
                    # older layout: duration and caption in separate cells
                    caption = cells[start].strip()
                    start += 1
                break
        if not caption:
            continue
        views = None
        for cell in cells[start:]:
            if _VIEWS_CELL.match(cell.strip()):
                views = parse_short_number(cell)
                break
        out.append({"caption": caption, "duration_s": duration, "views": views})
    return out


def parse_overview(text):
    flat = " ".join((text or "").split())
    out = {
        "views": None,
        "avg_watch_s": None,
        "full_watch_pct": None,
        "new_followers": None,
        "restricted": "not eligible for recommendation" in flat.lower(),
    }
    match = re.search(r"Video views\s+" + _NUM, flat)
    if match:
        out["views"] = _to_int(match.group(1), match.group(2))
    match = re.search(r"Average watch time\s+([\d.]+)\s*s", flat)
    if match:
        out["avg_watch_s"] = float(match.group(1))
    match = re.search(r"Watched full video\s+([\d.]+)\s*%", flat)
    if match:
        out["full_watch_pct"] = float(match.group(1))
    match = re.search(r"New followers\s+(-?\d+)", flat)
    if match:
        out["new_followers"] = int(match.group(1))
    return out


def parse_followers(text):
    flat = " ".join((text or "").split())
    match = re.search(r"Total followers\s+" + _NUM, flat)
    return _to_int(match.group(1), match.group(2)) if match else None


def match_caption(caption, posted_captions, prefix=40):
    """Studio truncates long captions; match on a normalized prefix."""
    norm = " ".join((caption or "").split()).lower()
    if not norm:
        return None
    for video_id, stored in posted_captions:
        stored_norm = " ".join((stored or "").split()).lower()
        if not stored_norm:
            continue
        head = stored_norm[:prefix]
        if norm.startswith(head) or stored_norm.startswith(norm[:prefix]):
            return video_id
    return None


def _open(cfg):
    try:
        from phantomwright_driver.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright
    return sync_playwright


def sync(cfg, conn, days=14, limit=10, wait_ms=9000):
    """Snapshot per-post views/retention and follower count.

    Studio's per-video analytics page renders an empty shell on a cold URL
    load ("Video views --") and only fills in when reached by clicking the
    row's "View data" link from the content page, so the pass is click-driven
    and returns to the table after each video.

    Returns a summary dict and writes post_stats / account_snapshots rows.
    """
    from autotik import db, publish, session

    cookie = publish._stage_cookie(cfg, cfg.account)
    cur = conn.cursor()
    cutoff = db.iso_days_ago(days)
    posted = cur.execute(
        "SELECT video_id, caption, posted_at FROM videos "
        "WHERE status='posted' AND caption IS NOT NULL AND caption != '' "
        "AND posted_at >= ? ORDER BY posted_at DESC",
        (cutoff,),
    ).fetchall()
    pairs = [(r["video_id"], r["caption"]) for r in posted]

    stats = {}
    followers = None
    sync_playwright = _open(cfg)
    with sync_playwright() as p:
        context, page = session.open_context(p, str(cookie))
        try:
            page.goto(CONTENT_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(12000)
            if "/login" in page.url:
                raise publish.PublishError("studio session invalid (login wall)")
            table = parse_content_rows(page.evaluate(_ROW_JS))

            matched = []
            for index, entry in enumerate(table):
                video_id = match_caption(entry["caption"], pairs)
                if video_id:
                    matched.append((index, video_id, entry))

            for index, video_id, entry in matched[:limit]:
                row = {
                    "views": entry["views"], "avg_watch_s": None,
                    "full_watch_pct": None, "new_followers": None,
                    "restricted": None,
                }
                try:
                    link = page.locator("table tbody tr").nth(index).locator(
                        "a:has-text('View data'), button:has-text('View data')"
                    ).first
                    link.click(timeout=8000)
                    page.wait_for_timeout(11000)
                    parsed = parse_overview(page.inner_text("body"))
                    if parsed["avg_watch_s"] is not None:
                        row.update(parsed)
                        if parsed["views"] is None:
                            row["views"] = entry["views"]
                except Exception:
                    pass
                stats[video_id] = row
                db.add_post_stats(cur, video_id=video_id, **row)
                conn.commit()
                try:
                    page.go_back()
                    page.wait_for_timeout(8000)
                except Exception:
                    page.goto(CONTENT_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(10000)

            page.goto(FOLLOWERS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait_ms)
            followers = parse_followers(page.inner_text("body"))
        finally:
            context.close()

    if followers is not None:
        db.add_account_snapshot(cur, followers)
        conn.commit()

    return {
        "followers": followers,
        "checked": len(stats),
        "restricted": sum(1 for s in stats.values() if s["restricted"]),
        "views": sum(s["views"] or 0 for s in stats.values()),
    }


def summary(conn):
    cur = conn.cursor()
    latest = cur.execute(
        "SELECT * FROM post_stats ORDER BY id DESC LIMIT 200"
    ).fetchall()
    seen = {}
    for row in latest:
        seen.setdefault(row["video_id"], row)
    rows = list(seen.values())
    followers = cur.execute(
        "SELECT followers FROM account_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    restricted = [r for r in rows if r["restricted"]]
    watch = [r["avg_watch_s"] for r in rows if r["avg_watch_s"] is not None]
    full = [r["full_watch_pct"] for r in rows if r["full_watch_pct"] is not None]
    return {
        "followers": followers["followers"] if followers else None,
        "videos": len(rows),
        "views": sum(r["views"] or 0 for r in rows),
        "restricted": len(restricted),
        "avg_watch_s": round(sum(watch) / len(watch), 2) if watch else None,
        "avg_full_watch_pct": round(sum(full) / len(full), 2) if full else None,
        "latest": rows,
    }


def variant_report(conn):
    """Restriction rate and reach per transform variant.

    Uses the newest snapshot per posted video so repeated syncs do not
    double count.
    """
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT v.transform_variant AS variant, "
        "       COUNT(*) AS videos, "
        "       SUM(CASE WHEN s.restricted = 1 THEN 1 ELSE 0 END) AS restricted, "
        "       AVG(s.views) AS avg_views, "
        "       AVG(s.avg_watch_s) AS avg_watch_s, "
        "       AVG(s.full_watch_pct) AS avg_full_watch_pct "
        "FROM videos v JOIN post_stats s ON s.video_id = v.video_id "
        "WHERE v.status = 'posted' "
        "  AND s.id = (SELECT MAX(id) FROM post_stats WHERE video_id = v.video_id) "
        "GROUP BY v.transform_variant "
        "ORDER BY v.transform_variant"
    ).fetchall()
    out = []
    for row in rows:
        videos = row["videos"] or 0
        restricted = row["restricted"] or 0
        out.append({
            "variant": row["variant"],
            "videos": videos,
            "restricted": restricted,
            "restricted_pct": round(100.0 * restricted / videos, 1) if videos else 0.0,
            "avg_views": round(row["avg_views"], 1) if row["avg_views"] else None,
            "avg_watch_s": (
                round(row["avg_watch_s"], 2) if row["avg_watch_s"] else None
            ),
            "avg_full_watch_pct": (
                round(row["avg_full_watch_pct"], 2)
                if row["avg_full_watch_pct"] else None
            ),
        })
    return out