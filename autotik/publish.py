import json
from datetime import date, datetime, timezone
from pathlib import Path

from autotik import alert, db

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SYSTEM_PROMPT = (
    "You rewrite TikTok captions. Return ONLY the new caption: a punchy first "
    "line, then 3-5 relevant hashtags. Keep it under 150 characters. Never add "
    "quotes, prefixes, or explanations."
)


class PublishError(RuntimeError):
    pass


def allowed_today(posted, posts_per_day, max_posts):
    return max(0, min(posts_per_day, max_posts) - posted)


def rewrite_caption(client, cfg, desc):
    body = {
        "model": cfg.openrouter_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Rewrite this TikTok caption:\n{desc}"},
        ],
        "max_tokens": 200,
        "temperature": 0.9,
    }
    try:
        resp = client.post(
            OPENROUTER_URL,
            content=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.openrouter_api_key}",
            },
            timeout=45.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return text or desc
    except Exception:
        return desc


def default_uploader(cfg):
    def upload(video_path, caption, account):
        from tiktokautouploader import upload_tiktok

        upload_tiktok(
            video=str(video_path),
            description=caption,
            accountname=account.lstrip("@"),
        )

    return upload


def publish_batch(cfg, conn, now=None, auto=False, uploader=None, client=None):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = conn.cursor()

    if cfg.stage_only and not auto:
        n = len(db.rows_with_status(cur, "approved"))
        return {"posted": 0, "failed": 0, "skipped": n}

    already = db.posted_today(cur, day)
    budget = allowed_today(already, cfg.posts_per_day, cfg.max_posts_per_day)
    if budget <= 0:
        return {"posted": 0, "failed": 0, "skipped": 0}

    candidates = [r for r in db.rows_with_status(cur, "approved") if r["file_path"]]
    missing = [r for r in db.rows_with_status(cur, "approved") if not r["file_path"]]

    result = {"posted": 0, "failed": 0, "skipped": len(missing)}
    for row in missing:
        db.set_status(cur, row["video_id"], "failed", "no file_path recorded")
        alert.send(cfg.n8n_webhook_url, "warn", f"missing file for {row['video_id']}")
    conn.commit()

    own_client = client is None
    if own_client:
        import httpx
        client = httpx.Client()
    upload = uploader or default_uploader(cfg)

    try:
        for row in candidates[:budget]:
            vid = row["video_id"]
            path = Path(row["file_path"])
            if not path.exists():
                db.set_status(cur, vid, "failed", f"file missing: {path}")
                result["failed"] += 1
                alert.send(cfg.n8n_webhook_url, "error", f"file missing: {path}")
                conn.commit()
                continue
            caption = rewrite_caption(client, cfg, row["desc_orig"] or "")
            try:
                upload(path, caption, cfg.account)
            except Exception as exc:
                db.set_status(cur, vid, "failed", str(exc)[:500])
                db.log(cur, vid, "upload_failed", str(exc)[:500])
                result["failed"] += 1
                alert.send(
                    cfg.n8n_webhook_url, "error",
                    f"upload failed for {vid}: {exc}",
                )
                conn.commit()
                break
            cur.execute(
                "UPDATE videos SET caption=? WHERE video_id=?", (caption, vid)
            )
            db.set_status(cur, vid, "posted")
            db.log(cur, vid, "posted", caption[:120])
            result["posted"] += 1
            conn.commit()
    finally:
        if own_client:
            client.close()

    if result["posted"] == 0 and result["failed"] > 0:
        alert.send(cfg.n8n_webhook_url, "error", "publish produced 0 posts")
    return result
