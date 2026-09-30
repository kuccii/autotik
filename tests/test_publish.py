import httpx
import pytest

from autotik import db, publish


def row(vid, tmp_path):
    path = tmp_path / "videos" / f"{vid}_tf.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"v" * 2_000_000)
    return {
        "video_id": vid, "author": "a", "region": "RW", "desc_orig": "orig",
        "digg_count": 1, "play_count": 1, "share_count": 1, "score": 1.0,
        "file_path": str(path), "file_hash": f"h{vid}",
        "source_url": f"https://www.tiktok.com/@a/video/{vid}",
    }


def make_conn(tmp_path):
    conn = db.init_db(tmp_path / "t.sqlite")
    db.insert_candidates(conn.cursor(), [row(v, tmp_path) for v in ("v1", "v2", "v3")])
    for vid in ("v1", "v2", "v3"):
        db.set_status(conn.cursor(), vid, "approved")
    conn.commit()
    return conn


def cfg(**over):
    from autotik.config import load_config
    env = {
        "REGION": "RW", "TIKTOK_ACCOUNT": "@demo",
        "OPENROUTER_API_KEY": "sk-test", "N8N_WEBHOOK_URL": "",
        "STAGE_ONLY": "false",
    }
    env.update(over)
    return load_config(env)


def no_net(content="rewritten"):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_stage_only_blocks_upload(tmp_path):
    conn = make_conn(tmp_path)
    c = cfg(STAGE_ONLY="true")
    out = publish.publish_batch(c, conn, auto=False, uploader=lambda *a: pytest.fail("uploaded"))
    assert out == {"posted": 0, "failed": 0, "skipped": 3}
    assert db.counts(conn.cursor())["approved"] == 3


def test_auto_posts_until_daily_cap(tmp_path):
    conn = make_conn(tmp_path)
    calls = []
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="1"), conn, auto=True,
        uploader=lambda path, caption, account: calls.append(path),
        client=no_net(),
    )
    assert out["posted"] == 1
    assert len(calls) == 1
    assert db.counts(conn.cursor())["posted"] == 1
    assert db.counts(conn.cursor())["approved"] == 2


def test_cap_accounts_for_already_posted_today(tmp_path):
    conn = make_conn(tmp_path)
    cur = conn.cursor()
    db.set_status(cur, "v1", "posted")
    cur.execute("UPDATE videos SET posted_at='2026-09-30T08:00:00' WHERE video_id='v1'")
    conn.commit()
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda *a: None, client=no_net(),
    )
    assert out["posted"] == 2


def test_failure_marks_row_and_stops_batch(tmp_path):
    conn = make_conn(tmp_path)
    calls = []

    def flaky(path, caption, account):
        calls.append(path)
        raise RuntimeError("cookie expired")

    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True, uploader=flaky, client=no_net()
    )
    assert out["posted"] == 0
    assert out["failed"] == 1
    assert len(calls) == 1
    cur = conn.cursor()
    assert db.counts(cur)["failed"] == 1
    assert db.counts(cur)["approved"] == 2
    assert "cookie expired" in db.get(cur, "v1")["error"]


def test_missing_file_marks_failed_but_batch_continues(tmp_path):
    conn = make_conn(tmp_path)
    (tmp_path / "videos" / "v1_tf.mp4").unlink()
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda *a: None, client=no_net(),
    )
    assert out["failed"] == 1
    assert out["posted"] == 2
    cur = conn.cursor()
    assert db.counts(cur)["failed"] == 1
    assert db.counts(cur)["posted"] == 2
    assert "file missing" in db.get(cur, "v1")["error"]


def test_allowed_today_math():
    assert publish.allowed_today(posted=0, posts_per_day=1, max_posts=3) == 1
    assert publish.allowed_today(posted=2, posts_per_day=1, max_posts=3) == 0
    assert publish.allowed_today(posted=1, posts_per_day=9, max_posts=3) == 2


def test_rewrite_caption_falls_back_to_original_on_error(tmp_path):
    def handler(request):
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg_obj = cfg()
    assert publish.rewrite_caption(client, cfg_obj, "hello world") == "hello world"


def test_rewrite_caption_uses_model_response():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "rewritten #tags"}}]
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = publish.rewrite_caption(client, cfg(), "original")
    assert out == "rewritten #tags"
