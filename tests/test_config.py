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


def test_blank_stage_only_treated_as_unset_defaults_to_true():
    assert load_config(base_env(STAGE_ONLY="")).stage_only is True
    assert load_config(base_env(STAGE_ONLY="   ")).stage_only is True


def test_unrecognized_boolean_token_rejected():
    with pytest.raises(ConfigError) as exc:
        load_config(base_env(STAGE_ONLY="ture"))
    assert "unrecognized boolean value" in str(exc.value)
    assert "ture" in str(exc.value)


def test_overlay_enabled_no_parses_as_false():
    assert load_config(base_env(OVERLAY_ENABLED="no")).overlay_enabled is False


def test_malformed_slot_rejected_at_load():
    with pytest.raises(ConfigError) as exc:
        load_config(base_env(PUBLISH_SLOTS="08:00,8am,13:30"))
    assert "8am" in str(exc.value)


def test_slot_range_and_zero_padding_enforced():
    for bad in ("8:00", "08:5", "24:00", "08:60", "0800", "08:00:00"):
        with pytest.raises(ConfigError) as exc:
            load_config(base_env(PUBLISH_SLOTS=bad))
        assert bad in str(exc.value)


def test_valid_slots_parse_to_same_minutes():
    from autotik import publish

    cfg = load_config(base_env(PUBLISH_SLOTS="07:45,21:05,00:00,23:59"))
    assert cfg.publish_slots == ("07:45", "21:05", "00:00", "23:59")
    assert [publish._slot_minutes(s) for s in cfg.publish_slots] == [
        465, 1265, 0, 1439,
    ]