import re
from pathlib import Path

import pytest

from autotik import cli, db


def env(monkeypatch, **over):
    base = {
        "REGION": "RW", "TIKTOK_ACCOUNT": "@demo",
        "OPENROUTER_API_KEY": "sk-test", "N8N_WEBHOOK_URL": "",
        "DATA_DIR": "", "COOKIE_DIR": "",
        "DISCOVER_MIN_LIKES": "0",
    }
    base.update(over)
    for k, v in base.items():
        if v == "":
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_usage_error_is_exit_2(monkeypatch):
    env(monkeypatch)
    assert cli.main(["nonsense"]) == 2


def test_approve_unknown_id_is_exit_2(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    assert cli.main(["approve", "does-not-exist"]) == 2


def test_discover_zero_rows_is_exit_1(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(cli, "run_discover", lambda cfg, conn, limit: 0)
    assert cli.main(["discover"]) == 1


def test_discover_with_rows_is_exit_0(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(cli, "run_discover", lambda cfg, conn, limit: 3)
    assert cli.main(["discover"]) == 0


def test_publish_stage_only_is_exit_0(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(cli, "run_publish", lambda cfg, conn, auto: {"posted": 0, "failed": 0, "skipped": 0})
    assert cli.main(["publish"]) == 0
    assert cli.main(["publish", "--stage-only"]) == 0


def test_publish_auto_flag_propagates(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    seen = {}
    monkeypatch.setattr(cli, "run_publish", lambda cfg, conn, auto: seen.update(auto=auto) or {"posted": 1, "failed": 0, "skipped": 0})
    assert cli.main(["publish", "--auto"]) == 0
    assert seen["auto"] is True


def test_approve_all_transitions_rows(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    conn = db.init_db(tmp_path / "autotik.sqlite")
    db.insert_candidates(conn.cursor(), [{"video_id": "v1", "desc_orig": "d"}])
    conn.commit()
    conn.close()
    assert cli.main(["approve", "--all"]) == 0
    conn = db.init_db(tmp_path / "autotik.sqlite")
    assert db.counts(conn.cursor())["approved"] == 1


def test_approve_all_demotes_rows_below_likes_floor(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path), DISCOVER_MIN_LIKES="1000000")
    conn = db.init_db(tmp_path / "autotik.sqlite")
    db.insert_candidates(conn.cursor(), [
        {"video_id": "viral", "desc_orig": "d", "digg_count": 2_000_000},
        {"video_id": "small", "desc_orig": "d", "digg_count": 99_000},
    ])
    conn.commit()
    conn.close()
    assert cli.main(["approve", "--all"]) == 0
    conn = db.init_db(tmp_path / "autotik.sqlite")
    assert db.get(conn.cursor(), "viral")["status"] == "approved"
    assert db.get(conn.cursor(), "small")["status"] == "skipped"


def test_approve_explicit_id_below_likes_floor_exits_2(tmp_path, monkeypatch, capsys):
    env(monkeypatch, DATA_DIR=str(tmp_path), DISCOVER_MIN_LIKES="1000000")
    conn = db.init_db(tmp_path / "autotik.sqlite")
    db.insert_candidates(conn.cursor(), [
        {"video_id": "small", "desc_orig": "d", "digg_count": 5},
    ])
    conn.commit()
    conn.close()
    assert cli.main(["approve", "small"]) == 2
    conn = db.init_db(tmp_path / "autotik.sqlite")
    assert db.get(conn.cursor(), "small")["status"] == "skipped"


def test_approve_all_demotes_rows_older_than_age_floor(tmp_path, monkeypatch):
    import time as _time
    env(monkeypatch, DATA_DIR=str(tmp_path), DISCOVER_MIN_LIKES="1000000",
        DISCOVER_MAX_AGE_DAYS="30")
    conn = db.init_db(tmp_path / "autotik.sqlite")
    old_ct = int(_time.time()) - 60 * 86400
    fresh_ct = int(_time.time()) - 2 * 86400
    db.insert_candidates(conn.cursor(), [
        {"video_id": "stale", "desc_orig": "d", "digg_count": 2_000_000,
         "create_time": old_ct},
        {"video_id": "fresh", "desc_orig": "d", "digg_count": 2_000_000,
         "create_time": fresh_ct},
    ])
    conn.commit()
    conn.close()
    assert cli.main(["approve", "--all"]) == 0
    conn = db.init_db(tmp_path / "autotik.sqlite")
    assert db.get(conn.cursor(), "fresh")["status"] == "approved"
    assert db.get(conn.cursor(), "stale")["status"] == "skipped"


def test_status_is_exit_0(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    assert cli.main(["status"]) == 0


def test_config_error_is_exit_2(monkeypatch):
    env(monkeypatch, REGION="", TIKTOK_ACCOUNT="", OPENROUTER_API_KEY="")
    assert cli.main(["status"]) == 2


def test_publish_auto_zero_posts_alerts_error(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(cli, "run_publish", lambda cfg, conn, auto: {"posted": 0, "failed": 0, "skipped": 0})
    sent = []

    def fake_send(url, level, message, client=None):
        sent.append((level, message))
        return False

    monkeypatch.setattr(cli.alert, "send", fake_send)
    assert cli.main(["publish", "--auto"]) == 1
    assert sent and sent[0][0] == "error"
    assert "0 posts" in sent[0][1]


def test_publish_stage_only_flag_overrides_env(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path), STAGE_ONLY="0")
    seen = {}

    def fake_publish(cfg, conn, auto):
        seen.update(cfg=cfg, auto=auto)
        return {"posted": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(cli, "run_publish", fake_publish)
    assert cli.main(["publish", "--stage-only"]) == 0
    assert seen["cfg"].stage_only is True
    assert seen["auto"] is False


def test_duplicate_hash_mid_discovery_continues(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    feed = [
        {"video_id": "v1", "region": "RW", "author": {"unique_id": "u1", "nickname": "U1"},
         "play": "https://x/1.mp4", "title": "first", "digg_count": 300,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v2", "region": "RW", "author": {"unique_id": "u2", "nickname": "U2"},
         "play": "https://x/2.mp4", "title": "second", "digg_count": 200,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v3", "region": "RW", "author": {"unique_id": "u3", "nickname": "U3"},
         "play": "https://x/3.mp4", "title": "third", "digg_count": 100,
         "play_count": 10, "share_count": 1, "duration": 12},
    ]
    hashes = {"v1": "h1", "v2": "h1", "v3": "h2"}
    sent = []

    def fake_send(url, level, message, client=None):
        sent.append((level, message))
        return False

    def fake_download(item, dest_dir, client):
        return tmp_path / f"{item['video_id']}.mp4", hashes[item["video_id"]]

    monkeypatch.setattr(cli.discover, "fetch_feed", lambda client, region, count: feed)
    monkeypatch.setattr(cli.download, "download", fake_download)
    monkeypatch.setattr(
        cli.transform, "transform",
        lambda src, dst, account, enabled=True, style="bar", variant="control": dst,
    )
    monkeypatch.setattr(cli.alert, "send", fake_send)

    assert cli.main(["discover"]) == 0

    failure_alerts = [(lv, m) for lv, m in sent if "failed" in m]
    assert [lv for lv, _ in failure_alerts] == ["warn"]
    assert len(failure_alerts) == 1
    assert any("only 2 new videos staged" in m for _, m in sent)
    conn = db.init_db(tmp_path / "autotik.sqlite")
    assert db.counts(conn.cursor())["staged"] == 2
    dup = conn.execute(
        "SELECT count(*) c FROM post_log WHERE video_id='v2' AND event='duplicate_hash'"
    ).fetchone()
    assert dup["c"] == 1
    conn.close()


def test_auto_and_stage_only_conflict_is_exit_2(monkeypatch):
    env(monkeypatch)
    assert cli.main(["publish", "--auto", "--stage-only"]) == 2


def test_discover_dedups_known_rows_before_ranking(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    conn = db.init_db(tmp_path / "autotik.sqlite")
    # seed the top-ranked video so it must not consume a --limit slot
    db.insert_candidates(
        conn.cursor(),
        [{"video_id": "v1", "desc_orig": "seen", "status": "failed"}],
    )
    conn.commit()
    conn.close()
    feed = [
        {"video_id": "v1", "region": "RW", "author": {"unique_id": "u1", "nickname": "U1"},
         "play": "https://cdn.test/1.mp4", "title": "first", "digg_count": 3000,
         "play_count": 1000, "share_count": 50, "duration": 12},
        {"video_id": "v2", "region": "RW", "author": {"unique_id": "u2", "nickname": "U2"},
         "play": "https://cdn.test/2.mp4", "title": "second", "digg_count": 200,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v3", "region": "RW", "author": {"unique_id": "u3", "nickname": "U3"},
         "play": "https://cdn.test/3.mp4", "title": "third", "digg_count": 100,
         "play_count": 10, "share_count": 1, "duration": 12},
    ]
    downloaded = []

    def fake_download(item, dest_dir, client):
        downloaded.append(item["video_id"])
        return tmp_path / f"{item['video_id']}.mp4", "h" + item["video_id"]

    monkeypatch.setattr(cli.discover, "fetch_feed", lambda client, region, count: feed)
    monkeypatch.setattr(cli.download, "download", fake_download)
    monkeypatch.setattr(
        cli.transform, "transform",
        lambda src, dst, account, enabled=True, style="bar", variant="control": dst,
    )
    _spy = []
    monkeypatch.setattr(
        cli.alert, "send",
        lambda url, level, message, client=None: _spy.append((level, message)) or False,
    )

    assert cli.main(["discover", "--limit", "2"]) == 0

    # dedup happens BEFORE ranking: the known v1 never downloads and both
    # --limit slots go to genuinely new rows
    assert downloaded == ["v2", "v3"]
    conn = db.init_db(tmp_path / "autotik.sqlite")
    counts = db.counts(conn.cursor())
    assert counts["staged"] == 2
    assert db.get(conn.cursor(), "v1")["status"] == "failed"
    conn.close()

def test_transform_failure_marks_failed_row_and_continues(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    feed = [
        {"video_id": "v1", "region": "RW", "author": {"unique_id": "u1", "nickname": "U1"},
         "play": "https://cdn.test/1.mp4", "title": "first", "digg_count": 300,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v2", "region": "RW", "author": {"unique_id": "u2", "nickname": "U2"},
         "play": "https://cdn.test/2.mp4", "title": "second", "digg_count": 200,
         "play_count": 10, "share_count": 1, "duration": 12},
    ]
    sent = []

    def fake_send(url, level, message, client=None):
        sent.append((level, message))
        return False

    def fake_download(item, dest_dir, client):
        return tmp_path / f"{item['video_id']}.mp4", "h" + item["video_id"]

    def fake_transform(src, dst, account, enabled=True, style="bar", variant="control"):
        if Path(src).name == "v1.mp4":
            raise cli.transform.TransformError(
                "ffmpeg exited 1: Conversion failed! frame=42 speed=1x"
            )
        return dst

    monkeypatch.setattr(cli.discover, "fetch_feed", lambda client, region, count: feed)
    monkeypatch.setattr(cli.download, "download", fake_download)
    monkeypatch.setattr(cli.transform, "transform", fake_transform)
    monkeypatch.setattr(cli.alert, "send", fake_send)

    assert cli.main(["discover"]) == 0

    conn = db.init_db(tmp_path / "autotik.sqlite")
    counts = db.counts(conn.cursor())
    assert counts["failed"] == 1
    assert counts["staged"] == 1
    failed = conn.execute("SELECT * FROM videos WHERE status='failed'").fetchone()
    assert failed["video_id"] == "v1"
    assert "frame=42" in failed["error"]
    assert failed["desc_orig"] == "first"
    assert failed["file_hash"] == "hv1"
    assert failed["region"] == "RW"
    logs = conn.execute(
        "SELECT count(*) c FROM post_log WHERE video_id='v1' AND event='transform_failed'"
    ).fetchone()
    assert logs["c"] == 1
    assert any(level == "warn" for level, _ in sent)
    conn.close()


def test_download_failures_send_single_summary_alert(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    feed = [
        {"video_id": "v1", "region": "RW", "author": {"unique_id": "u1", "nickname": "U1"},
         "play": "https://cdn.test/1.mp4", "title": "first", "digg_count": 300,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v2", "region": "RW", "author": {"unique_id": "u2", "nickname": "U2"},
         "play": "https://cdn.test/2.mp4", "title": "second", "digg_count": 200,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v3", "region": "RW", "author": {"unique_id": "u3", "nickname": "U3"},
         "play": "https://cdn.test/3.mp4", "title": "third", "digg_count": 100,
         "play_count": 10, "share_count": 1, "duration": 12},
    ]
    sent = []

    def fake_send(url, level, message, client=None):
        sent.append((level, message))
        return False

    def fake_download(item, dest_dir, client):
        if item["video_id"] in ("v1", "v2"):
            raise cli.download.DownloadError(
                "host not allowlisted: 'v19.tiktokcdn-eu.com'"
            )
        return tmp_path / f"{item['video_id']}.mp4", "h3"

    monkeypatch.setattr(cli.discover, "fetch_feed", lambda client, regions, count: feed)
    monkeypatch.setattr(cli.download, "download", fake_download)
    monkeypatch.setattr(
        cli.transform, "transform",
        lambda src, dst, account, enabled=True, style="bar", variant="control": dst,
    )
    monkeypatch.setattr(cli.alert, "send", fake_send)

    assert cli.main(["discover"]) == 0

    summary = [(lv, m) for lv, m in sent if "failed" in m]
    assert len(summary) == 1, sent
    level, message = summary[0]
    assert level == "warn"
    assert "2 failed" in message
    assert "download v1" in message and "download v2" in message
    conn = db.init_db(tmp_path / "autotik.sqlite")
    logs = conn.execute(
        "SELECT count(*) c FROM post_log WHERE event='download_failed'"
    ).fetchone()
    assert logs["c"] == 2
    assert db.counts(conn.cursor())["staged"] == 1
    conn.close()


def _spy_alert(monkeypatch):
    sent = []

    def fake_send(url, level, message, client=None):
        sent.append((level, message))
        return False

    monkeypatch.setattr(cli.alert, "send", fake_send)
    return sent


def test_publish_auto_not_due_exits_0_without_alert(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(
        cli, "run_publish",
        lambda cfg, conn, auto: {
            "posted": 0, "failed": 0, "skipped": 0,
            "not_due": True, "reason": "not_due",
        },
    )
    sent = _spy_alert(monkeypatch)
    assert cli.main(["publish", "--auto"]) == 0
    assert sent == []


def test_publish_auto_budget_spent_exits_0_without_alert(tmp_path, monkeypatch, capsys):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(
        cli, "run_publish",
        lambda cfg, conn, auto: {
            "posted": 0, "failed": 0, "skipped": 0,
            "not_due": False, "reason": "budget_spent",
        },
    )
    sent = _spy_alert(monkeypatch)
    assert cli.main(["publish", "--auto"]) == 0
    assert sent == []
    assert "budget spent" in capsys.readouterr().out


def test_publish_auto_due_with_budget_and_nothing_approved_alerts(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(
        cli, "run_publish",
        lambda cfg, conn, auto: {
            "posted": 0, "failed": 0, "skipped": 0,
            "not_due": False, "reason": None,
        },
    )
    sent = _spy_alert(monkeypatch)
    assert cli.main(["publish", "--auto"]) == 1
    assert sent and sent[0][0] == "error"
    assert "0 posts" in sent[0][1]


def test_run_daily_publishes_after_successful_discover(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    calls = {}

    def fake_discover(cfg, conn, limit):
        calls["limit"] = limit
        return 3

    def fake_publish(cfg, conn, auto):
        calls["auto"] = auto
        return {
            "posted": 0, "failed": 0, "skipped": 0,
            "not_due": True, "reason": "not_due",
        }

    monkeypatch.setattr(cli, "run_discover", fake_discover)
    monkeypatch.setattr(cli, "run_publish", fake_publish)
    _spy_alert(monkeypatch)
    assert cli.main(["run-daily", "--auto"]) == 0
    assert calls == {"limit": None, "auto": True}


def test_run_daily_skips_publish_when_discover_fails(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(cli, "run_discover", lambda cfg, conn, limit: 0)
    monkeypatch.setattr(
        cli, "run_publish",
        lambda cfg, conn, auto: pytest.fail("publish must not run"),
    )
    assert cli.main(["run-daily", "--auto"]) == 1


def test_run_daily_propagates_publish_failure(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(cli, "run_discover", lambda cfg, conn, limit: 3)
    monkeypatch.setattr(
        cli, "run_publish",
        lambda cfg, conn, auto: {
            "posted": 0, "failed": 0, "skipped": 0,
            "not_due": False, "reason": None,
        },
    )
    _spy_alert(monkeypatch)
    assert cli.main(["run-daily", "--auto"]) == 1


def test_run_discover_persists_desc_orig(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    feed = [
        {"video_id": "v1", "region": "RW", "author": {"unique_id": "u1", "nickname": "U1"},
         "play": "https://cdn.test/1.mp4", "title": "first", "digg_count": 300,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v2", "region": "RW", "author": {"unique_id": "u2", "nickname": "U2"},
         "play": "https://cdn.test/2.mp4", "title": "second", "digg_count": 200,
         "play_count": 10, "share_count": 1, "duration": 12},
    ]

    def fake_download(item, dest_dir, client):
        return tmp_path / f"{item['video_id']}.mp4", "h" + item["video_id"]

    monkeypatch.setattr(cli.discover, "fetch_feed", lambda client, region, count: feed)
    monkeypatch.setattr(cli.download, "download", fake_download)
    monkeypatch.setattr(
        cli.transform, "transform",
        lambda src, dst, account, enabled=True, style="bar", variant="control": dst,
    )
    monkeypatch.setattr(cli.alert, "send", lambda *a, **k: False)

    assert cli.main(["discover"]) == 0

    expected = cli.discover.parse_items(feed, "RW")[0]["desc"]
    assert expected == "first"
    conn = db.init_db(tmp_path / "autotik.sqlite")
    rows = {r["video_id"]: r for r in conn.execute("SELECT * FROM videos")}
    conn.close()
    assert rows["v1"]["desc_orig"] == expected
    assert rows["v2"]["desc_orig"] == "second"


def test_status_reports_last_run_and_next_slot(tmp_path, monkeypatch, capsys):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    conn = db.init_db(tmp_path / "autotik.sqlite")
    db.insert_candidates(conn.cursor(), [{"video_id": "v1", "desc_orig": "d"}])
    db.log(conn.cursor(), "v1", "staged", "path")
    conn.commit()
    conn.close()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    last = [ln for ln in out.splitlines() if ln.startswith("last run: ")]
    assert last and "staged" in last[0]
    nxt = [ln for ln in out.splitlines() if ln.startswith("next slot: ")]
    assert nxt and re.match(r"next slot: \d{2}:\d{2} (today|tomorrow)$", nxt[0])


def test_status_without_history_says_never(tmp_path, monkeypatch, capsys):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    assert cli.main(["status"]) == 0
    assert "last run: never" in capsys.readouterr().out

def test_duplicate_hash_on_failed_path_continues_discovery(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    feed = [
        {"video_id": "v1", "region": "RW", "author": {"unique_id": "u1", "nickname": "U1"},
         "play": "https://cdn.test/1.mp4", "title": "first", "digg_count": 300,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v2", "region": "RW", "author": {"unique_id": "u2", "nickname": "U2"},
         "play": "https://cdn.test/2.mp4", "title": "second", "digg_count": 200,
         "play_count": 10, "share_count": 1, "duration": 12},
        {"video_id": "v3", "region": "RW", "author": {"unique_id": "u3", "nickname": "U3"},
         "play": "https://cdn.test/3.mp4", "title": "third", "digg_count": 100,
         "play_count": 10, "share_count": 1, "duration": 12},
    ]
    hashes = {"v1": "h1", "v2": "h1", "v3": "h2"}
    sent = []

    def fake_send(url, level, message, client=None):
        sent.append((level, message))
        return False

    def fake_download(item, dest_dir, client):
        return tmp_path / f"{item['video_id']}.mp4", hashes[item["video_id"]]

    def fake_transform(src, dst, account, enabled=True, style="bar", variant="control"):
        if Path(src).name == "v2.mp4":
            raise cli.transform.TransformError(
                "ffmpeg exited 1: Conversion failed! frame=42 speed=1x"
            )
        return dst

    monkeypatch.setattr(cli.discover, "fetch_feed", lambda client, region, count: feed)
    monkeypatch.setattr(cli.download, "download", fake_download)
    monkeypatch.setattr(cli.transform, "transform", fake_transform)
    monkeypatch.setattr(cli.alert, "send", fake_send)

    assert cli.main(["discover"]) == 0

    conn = db.init_db(tmp_path / "autotik.sqlite")
    counts = db.counts(conn.cursor())
    assert counts["staged"] == 2
    assert counts["failed"] == 0
    logs = conn.execute(
        "SELECT event FROM post_log WHERE video_id='v2'"
    ).fetchall()
    events = [r["event"] for r in logs]
    assert "duplicate_hash" in events
    assert "transform_failed" in events
    conn.close()
    assert not any("crashed" in msg for _, msg in sent)


def test_session_check_ok_exits_0(tmp_path, monkeypatch, capsys):
    env(monkeypatch, DATA_DIR=str(tmp_path))
    monkeypatch.setattr(
        cli.publish, "session_check", lambda cfg: {"ok": True, "posts": 7}
    )
    assert cli.main(["session-check"]) == 0
    assert "session ok: posts=7" in capsys.readouterr().out


def test_session_check_login_wall_alerts_and_exits_1(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path))

    def dead(cfg):
        raise cli.publish.PublishError(
            "tiktok session invalid (redirected to login wall); "
            "re-export cookies and replace the cookie file"
        )

    monkeypatch.setattr(cli.publish, "session_check", dead)
    sent = _spy_alert(monkeypatch)
    assert cli.main(["session-check"]) == 1
    assert sent and sent[0][0] == "error"
    assert "session check FAILED" in sent[0][1]
    assert "login wall" in sent[0][1]


def test_session_check_missing_cookie_alerts_and_exits_1(tmp_path, monkeypatch):
    env(monkeypatch, DATA_DIR=str(tmp_path), COOKIE_DIR=str(tmp_path / "cookies"))
    sent = _spy_alert(monkeypatch)
    assert cli.main(["session-check"]) == 1
    assert sent and sent[0][0] == "error"
    assert "cookie file not found" in sent[0][1]