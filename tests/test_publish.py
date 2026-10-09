import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


@pytest.fixture(autouse=True)
def slots_always_elapsed(monkeypatch):
    monkeypatch.setattr(publish, "slots_elapsed", lambda cfg, now=None: 3)


def test_stage_only_blocks_upload(tmp_path):
    conn = make_conn(tmp_path)
    c = cfg(STAGE_ONLY="true")
    out = publish.publish_batch(c, conn, auto=False, uploader=lambda *a: pytest.fail("uploaded"))
    assert out == {"posted": 0, "failed": 0, "skipped": 3, "not_due": False, "reason": None}
    assert db.counts(conn.cursor())["approved"] == 3


def test_not_due_before_first_slot_has_no_side_effects(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    monkeypatch.setattr(publish, "slots_elapsed", lambda cfg, now=None: 0)
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda *a: pytest.fail("uploaded"), client=no_net(),
    )
    assert out == {"posted": 0, "failed": 0, "skipped": 0, "not_due": True, "reason": "not_due"}
    assert db.counts(conn.cursor())["approved"] == 3
    log_rows = conn.execute("SELECT count(*) c FROM post_log").fetchone()
    assert log_rows["c"] == 0


def test_budget_spent_reports_reason(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    cur = conn.cursor()
    db.set_status(cur, "v1", "posted")
    conn.commit()
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="1"), conn, auto=True,
        uploader=lambda *a: pytest.fail("uploaded"), client=no_net(),
    )
    assert out["not_due"] is False
    assert out["reason"] == "budget_spent"
    assert out["posted"] == 0


def test_slots_elapsed_caps_allowed(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    monkeypatch.setattr(publish, "slots_elapsed", lambda cfg, now=None: 1)
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda path, caption, account: None, client=no_net(),
    )
    assert out["posted"] == 1
    assert db.counts(conn.cursor())["approved"] == 2


def test_due_with_budget_and_nothing_approved_is_reason_none(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "t.sqlite")
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda *a: pytest.fail("uploaded"), client=no_net(),
    )
    assert out == {"posted": 0, "failed": 0, "skipped": 0, "not_due": False, "reason": None}


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
    db.insert_candidates(cur, [row("v4", tmp_path)])
    db.set_status(cur, "v4", "approved")
    db.set_status(cur, "v4", "posted")
    stamp = datetime.now(ZoneInfo("Africa/Kigali")).astimezone(timezone.utc)
    cur.execute(
        "UPDATE videos SET posted_at=? WHERE video_id=?",
        (stamp.isoformat(timespec="seconds"), "v4"),
    )
    conn.commit()
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="2"), conn, auto=True,
        uploader=lambda *a: None, client=no_net(),
    )
    assert out["posted"] == 1
    counts = db.counts(cur)
    assert counts["posted"] == 2
    assert counts["approved"] == 2


def test_one_missed_slot_does_not_double_post(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    cur = conn.cursor()
    db.insert_candidates(cur, [row("v4", tmp_path)])
    db.set_status(cur, "v4", "approved")
    db.set_status(cur, "v4", "posted")
    stamp = datetime.now(ZoneInfo("Africa/Kigali")).astimezone(timezone.utc)
    cur.execute(
        "UPDATE videos SET posted_at=? WHERE video_id=?",
        (stamp.isoformat(timespec="seconds"), "v4"),
    )
    conn.commit()
    monkeypatch.setattr(publish, "slots_elapsed", lambda cfg, now=None: 2)
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="4"), conn, auto=True,
        uploader=lambda *a: None, client=no_net(),
    )
    # 2 slots elapsed, 1 already posted -> exactly ONE more this run
    # (the old formula allowed min(cap-already, elapsed) = 2: a burst)
    assert out["posted"] == 1
    counts = db.counts(cur)
    assert counts["posted"] == 2
    assert counts["approved"] == 2


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


def test_empty_desc_row_fails_and_batch_continues(tmp_path):
    conn = make_conn(tmp_path)
    cur = conn.cursor()
    cur.execute("UPDATE videos SET desc_orig='' WHERE video_id='v1'")
    conn.commit()
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda *a: None, client=no_net(),
    )
    assert out["failed"] == 1
    assert out["posted"] == 2
    cur = conn.cursor()
    assert db.counts(cur)["failed"] == 1
    assert db.counts(cur)["posted"] == 2
    assert "empty caption" in db.get(cur, "v1")["error"]


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

def test_error_return_marks_failed_and_alerts(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    sent = []
    monkeypatch.setattr(
        publish.alert, "send",
        lambda url, level, msg, client=None: sent.append((level, msg)) or False,
    )
    out = publish.publish_batch(
        cfg(POSTS_PER_DAY="3"), conn, auto=True,
        uploader=lambda *a: "Error", client=no_net(),
    )
    assert out["posted"] == 0
    assert out["failed"] == 1
    cur = conn.cursor()
    counts = db.counts(cur)
    assert counts["failed"] == 1
    assert counts["posted"] == 0
    assert counts["approved"] == 2
    row = db.get(cur, "v1")
    assert row["status"] == "failed"
    assert "Error" in (row["error"] or "")
    assert any(level == "error" and "Error" in msg for level, msg in sent)
    assert not any(level == "error" and "posted" == msg for level, msg in sent)


def _fake_upload_fn(monkeypatch, exc=None, calls=None):
    if calls is None:
        calls = []

    def fake(video, caption, cookie_path, accountname):
        calls.append(
            {
                "video": video,
                "caption": caption,
                "cookie": cookie_path,
                "accountname": accountname,
            }
        )
        if exc is not None:
            raise exc

    monkeypatch.setattr(publish, "_run_tiktok_upload", fake)
    return calls


def test_default_uploader_missing_cookie_raises_before_uploader_call(tmp_path, monkeypatch):
    calls = []
    _fake_upload_fn(monkeypatch, calls=calls)
    c = cfg(COOKIE_DIR=str(tmp_path / "cookies"))
    upload = publish.default_uploader(c)
    with pytest.raises(publish.PublishError, match="cookie file not found"):
        upload(tmp_path / "v.mp4", "cap", "@demo")
    assert calls == []


def test_default_uploader_copies_cookie_to_cwd_and_calls_uploader(tmp_path, monkeypatch):
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "TK_cookies_demo.json").write_text(
        '[{"name": "sessionid", "value": "X", "expires": 4102444800}]'
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    calls = []
    _fake_upload_fn(monkeypatch, calls=calls)
    c = cfg(COOKIE_DIR=str(cookie_dir))
    upload = publish.default_uploader(c)
    upload(tmp_path / "v.mp4", "cap", "@demo")
    assert (cwd / "TK_cookies_demo.json").exists()
    assert len(calls) == 1
    assert calls[0]["accountname"] == "demo"
    assert calls[0]["video"].endswith("v.mp4")


def test_stage_cookie_normalizes_minimal_export_without_domain(tmp_path, monkeypatch):
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "TK_cookies_demo.json").write_text(
        '[{"name": "sessionid", "value": "X", "expires": 4102444800,'
        ' "sameSite": "Lax"}]'
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    _fake_upload_fn(monkeypatch)
    c = cfg(COOKIE_DIR=str(cookie_dir))
    publish.default_uploader(c)(tmp_path / "v.mp4", "cap", "@demo")
    staged = json.loads((cwd / "TK_cookies_demo.json").read_text())
    assert staged[0]["domain"] == ".tiktok.com"
    assert staged[0]["path"] == "/"
    src = json.loads((cookie_dir / "TK_cookies_demo.json").read_text())
    assert "domain" not in src[0]


def test_default_uploader_raises_on_error_return(tmp_path, monkeypatch):
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "TK_cookies_demo.json").write_text("[]")
    monkeypatch.chdir(tmp_path)
    calls = []
    _fake_upload_fn(
        monkeypatch,
        exc=publish.PublishError("uploader returned 'Error' (expected success)"),
        calls=calls,
    )
    c = cfg(COOKIE_DIR=str(cookie_dir))
    upload = publish.default_uploader(c)
    with pytest.raises(publish.PublishError, match="Error"):
        upload(tmp_path / "v.mp4", "cap", "@demo")
    assert len(calls) == 1


def test_publish_batch_default_uploader_error_marks_failed(tmp_path, monkeypatch):
    cookie_dir = tmp_path / "cookies"
    cookie_dir.mkdir()
    (cookie_dir / "TK_cookies_demo.json").write_text("[]")
    monkeypatch.chdir(tmp_path)
    _fake_upload_fn(
        monkeypatch,
        exc=publish.PublishError("uploader returned 'Error' (expected success)"),
    )
    sent = []
    monkeypatch.setattr(
        publish.alert, "send",
        lambda url, level, msg, client=None: sent.append((level, msg)) or False,
    )
    conn = make_conn(tmp_path)
    c = cfg(POSTS_PER_DAY="3")
    out = publish.publish_batch(
        c, conn, auto=True, uploader=publish.default_uploader(c), client=no_net(),
    )
    assert out["posted"] == 0
    assert out["failed"] == 1
    counts = db.counts(conn.cursor())
    assert counts["failed"] == 1
    assert counts["posted"] == 0
    assert any(level == "error" for level, _ in sent)