import pytest

from autotik import cli, db


def env(monkeypatch, **over):
    base = {
        "REGION": "RW", "TIKTOK_ACCOUNT": "@demo",
        "OPENROUTER_API_KEY": "sk-test", "N8N_WEBHOOK_URL": "",
        "DATA_DIR": "", "COOKIE_DIR": "",
    }
    base.update(over)
    for k, v in base.items():
        if v != "":
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
