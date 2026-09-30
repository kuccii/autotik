import pytest

from autotik.config import ConfigError, load_config


def base_env(**over):
    env = {
        "REGION": "RW",
        "TIKTOK_ACCOUNT": "@demo",
        "OPENROUTER_API_KEY": "sk-test",
        "N8N_WEBHOOK_URL": "https://example.test/hook",
        "DATA_DIR": "data",
        "COOKIE_DIR": "cookies",
    }
    env.update(over)
    return env


def test_defaults():
    cfg = load_config(base_env())
    assert cfg.region == "RW"
    assert cfg.posts_per_day == 1
    assert cfg.max_posts_per_day == 3
    assert cfg.stage_only is True
    assert cfg.overlay_enabled is True
    assert cfg.publish_slots == ("08:00", "13:30", "19:30")
    assert cfg.overlay_account == "@demo"


def test_missing_required_lists_every_name():
    with pytest.raises(ConfigError) as exc:
        load_config({"DATA_DIR": "d"})
    msg = str(exc.value)
    assert "REGION" in msg
    assert "TIKTOK_ACCOUNT" in msg
    assert "OPENROUTER_API_KEY" in msg


def test_posts_per_day_clamped_to_max():
    cfg = load_config(base_env(POSTS_PER_DAY="9"))
    assert cfg.posts_per_day == 3


def test_invalid_overlay_account_rejected():
    with pytest.raises(ConfigError):
        load_config(base_env(OVERLAY_ACCOUNT="@bad:handle"))


def test_bool_parsing():
    assert load_config(base_env(STAGE_ONLY="false")).stage_only is False
    assert load_config(base_env(OVERLAY_ENABLED="0")).overlay_enabled is False
