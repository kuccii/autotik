from datetime import datetime, timezone

from autotik import publish


def cfg(**over):
    from autotik.config import load_config
    env = {
        "REGION": "RW", "TIKTOK_ACCOUNT": "@demo",
        "OPENROUTER_API_KEY": "sk-test", "N8N_WEBHOOK_URL": "",
        "STAGE_ONLY": "false",
    }
    env.update(over)
    return load_config(env)


def test_slots_elapsed_counts_in_chronological_order():
    c = cfg()
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 7, 59)) == 0
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 8, 0)) == 1
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 13, 29)) == 1
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 13, 30)) == 2
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 19, 30)) == 3
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 23, 59)) == 3


def test_slots_elapsed_converts_aware_datetimes_to_kigali():
    c = cfg()
    utc_morning = datetime(2026, 9, 30, 6, 30, tzinfo=timezone.utc)  # 08:30 Kigali
    assert publish.slots_elapsed(c, now=utc_morning) == 1
    utc_before_first = datetime(2026, 9, 30, 4, 59, tzinfo=timezone.utc)  # 06:59 Kigali
    assert publish.slots_elapsed(c, now=utc_before_first) == 0


def test_slots_elapsed_handles_unsorted_slot_config():
    c = cfg(PUBLISH_SLOTS="19:30,08:00")
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 10, 0)) == 1
    assert publish.slots_elapsed(c, now=datetime(2026, 9, 30, 20, 0)) == 2


def test_next_slot_before_first_slot_is_today():
    c = cfg()
    assert publish.next_slot(c, now=datetime(2026, 9, 30, 7, 59)) == "08:00 today"


def test_next_slot_between_slots_is_later_today():
    c = cfg()
    assert publish.next_slot(c, now=datetime(2026, 9, 30, 8, 0)) == "13:30 today"
    assert publish.next_slot(c, now=datetime(2026, 9, 30, 13, 30)) == "19:30 today"


def test_next_slot_after_last_slot_is_tomorrow_first():
    c = cfg()
    assert publish.next_slot(c, now=datetime(2026, 9, 30, 19, 31)) == "08:00 tomorrow"
    assert publish.next_slot(c, now=datetime(2026, 9, 30, 23, 59)) == "08:00 tomorrow"
