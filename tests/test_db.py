import pytest

from autotik import db


def fresh(tmp_path):
    conn = db.init_db(tmp_path / "t.sqlite")
    conn.execute("BEGIN")
    return conn


def row(vid, **over):
    r = {
        "video_id": vid,
        "author": "someone",
        "region": "RW",
        "desc_orig": "orig desc",
        "digg_count": 10,
        "play_count": 100,
        "share_count": 5,
        "score": 0.5,
        "file_path": f"data/videos/{vid}_tf.mp4",
        "file_hash": f"hash-{vid}",
        "source_url": f"https://www.tiktok.com/@someone/video/{vid}",
    }
    r.update(over)
    return r


def test_insert_dedups_by_video_id(tmp_path):
    conn = fresh(tmp_path)
    assert db.insert_candidates(conn.cursor(), [row("v1"), row("v1")]) == 1
    assert db.insert_candidates(conn.cursor(), [row("v1"), row("v2")]) == 1
    assert db.counts(conn.cursor()) == {
        "staged": 2,
        "approved": 0,
        "posted": 0,
        "failed": 0,
        "skipped": 0,
    }


def test_file_hash_collision_under_different_id_raises(tmp_path):
    conn = fresh(tmp_path)
    cur = conn.cursor()
    db.insert_candidates(cur, [row("v1", file_hash="same")])
    with pytest.raises(db.DuplicateHash):
        db.insert_candidates(cur, [row("v2", file_hash="same")])


def test_transition_rules(tmp_path):
    conn = fresh(tmp_path)
    cur = conn.cursor()
    db.insert_candidates(cur, [row("v1")])
    db.set_status(cur, "v1", "approved")
    db.set_status(cur, "v1", "posted")
    with pytest.raises(db.BadTransition):
        db.set_status(cur, "v1", "approved")


def test_unknown_video_id_raises(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(db.BadTransition):
        db.set_status(conn.cursor(), "nope", "approved")


def test_posted_today(tmp_path):
    conn = fresh(tmp_path)
    cur = conn.cursor()
    db.insert_candidates(cur, [row("a"), row("b")])
    db.set_status(cur, "a", "approved")
    db.set_status(cur, "a", "posted")
    cur.execute(
        "UPDATE videos SET posted_at=? WHERE video_id='a'", ("2026-09-30T08:00:00",)
    )
    assert db.posted_today(cur, "2026-09-30") == 1
    assert db.posted_today(cur, "2026-10-01") == 0


def test_log_and_status_kept_in_sync(tmp_path):
    conn = fresh(tmp_path)
    cur = conn.cursor()
    db.insert_candidates(cur, [row("v1")])
    db.log(cur, "v1", "downloaded", "ok")
    assert db.get(cur, "v1")["status"] == "staged"
    assert cur.execute("SELECT count(*) c FROM post_log").fetchone()["c"] == 1
