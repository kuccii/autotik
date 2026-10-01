from pathlib import Path
from autotik import cli, db


def env(monkeypatch, **over):
    base = {
        "REGION": "RW", "TIKTOK_ACCOUNT": "@demo",
        "OPENROUTER_API_KEY": "sk-test", "N8N_WEBHOOK_URL": "",
        "DATA_DIR": "", "COOKIE_DIR": "",
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
        lambda src, dst, account, enabled=True, style="bar": dst,
    )
    monkeypatch.setattr(cli.alert, "send", fake_send)

    assert cli.main(["discover"]) == 0

    assert [level for level, _ in sent] == ["warn"]
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

    def fake_transform(src, dst, account, enabled=True, style="bar"):
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