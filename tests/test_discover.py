import json
import math
from pathlib import Path

import httpx
import pytest

from autotik import discover

FIXTURE = Path(__file__).parent / "fixtures" / "tikwm_feed.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def titled_payload():
    data = [dict(item, title=item.get("title") or "placeholder title") for item in payload()["data"]]
    return dict(payload(), data=data)


def test_region_filter_is_client_side():
    items = discover.parse_items(payload()["data"], "RW")
    assert [i["video_id"] for i in items] == ["7691089168365145350"]


def test_parse_without_region_filter_keeps_every_country():
    items = discover.parse_items(titled_payload()["data"])
    assert [i["region"] for i in items] == ["PK", "RW", "US"]


def test_parse_skips_items_without_title():
    items = discover.parse_items(payload()["data"])
    assert [i["region"] for i in items] == ["RW", "US"]


def test_parse_accepts_region_list_and_is_case_insensitive():
    items = discover.parse_items(payload()["data"], ["rw", "US"])
    assert [i["region"] for i in items] == ["RW", "US"]


def test_parse_dedupes_video_ids_across_merged_feeds():
    data = titled_payload()["data"] + titled_payload()["data"][:2]
    items = discover.parse_items(data)
    assert [i["video_id"] for i in items] == [
        "7690264381417950496",
        "7691089168365145350",
        "7668358855683853590",
    ]


def test_parse_skips_items_with_dict_title():
    # some tikwm payloads return a dict for title; must skip, not crash the run
    data = titled_payload()["data"]
    data = [dict(item, title={"text": "hi"}) if item.get("region") == "RW" else item for item in data]
    items = discover.parse_items(data)
    assert [i["region"] for i in items] == ["PK", "US"]


def test_normalized_shape():
    item = discover.parse_items(payload()["data"], "RW")[0]
    assert item["author"] == "akabenzi_vs_abayisiramu"
    assert item["region"] == "RW"
    assert item["digg_count"] == 5112
    assert item["play_count"] == 86103
    assert item["share_count"] == 207
    assert item["duration"] == 49
    assert item["play_url"].startswith("https://")
    assert item["source_url"] == (
        "https://www.tiktok.com/@akabenzi_vs_abayisiramu/video/7691089168365145350"
    )
    assert item["score"] == 0.0


def test_rank_orders_by_weighted_score():
    items = [
        {"video_id": "a", "digg_count": 10, "play_count": 100, "share_count": 5, "score": 0},
        {"video_id": "b", "digg_count": 100, "play_count": 1000, "share_count": 50, "score": 0},
        {"video_id": "c", "digg_count": 1, "play_count": 1, "share_count": 0, "score": 0},
    ]
    out = discover.rank(items, keep=2, now=1_790_000_000.0)
    assert [i["video_id"] for i in out] == ["b", "a"]
    # no create_time -> age falls back to 7d (fresh); engagement rate capped
    age_d = 7.0
    expected_b = (
        0.35 * min((100 / 1000) / 0.08, 1.0)
        + 0.35 * min(math.log10(1000 / (age_d * 24)) / 6, 1.0)
        + 0.30
    )
    assert out[0]["score"] == pytest.approx(expected_b, abs=1e-6)


def test_rank_zero_metrics_does_not_divide_by_zero():
    items = [{"video_id": "z", "digg_count": 0, "play_count": 0, "share_count": 0, "score": 0}]
    score = discover.rank(items, keep=5, now=1_790_000_000.0)[0]["score"]
    # zero engagement -> er and velocity terms are 0; freshness floor remains
    assert score == pytest.approx(0.30)


def test_rank_is_batch_invariant():
    # scores must not depend on which other items share the batch
    a = {"video_id": "a", "digg_count": 10, "play_count": 100, "share_count": 5, "score": 0}
    b = {"video_id": "b", "digg_count": 100, "play_count": 1000, "share_count": 50, "score": 0}
    now = 1_790_000_000.0
    alone = discover.rank([dict(a)], keep=1, now=now)[0]
    in_pair = next(
        i for i in discover.rank([dict(a), dict(b)], keep=2, now=now)
        if i["video_id"] == "a"
    )
    assert alone["score"] == in_pair["score"]
    assert in_pair["score"] > 0


def test_filter_by_age_rejects_stale_items():
    now = 1_790_000_000.0
    day = 86400
    items = [
        {"video_id": "fresh", "create_time": int(now - 2 * day)},
        {"video_id": "boundary", "create_time": int(now - 14 * day)},
        {"video_id": "stale", "create_time": int(now - 30 * day)},
        {"video_id": "no_time", "create_time": 0},
    ]
    kept = discover.filter_by_age(items, now, 14.0)
    assert [i["video_id"] for i in kept] == ["fresh", "boundary", "no_time"]


def test_filter_by_likes_enforces_threshold():
    items = [
        {"video_id": "viral", "digg_count": 2_500_000},
        {"video_id": "exact", "digg_count": 1_000_000},
        {"video_id": "close", "digg_count": 999_999},
        {"video_id": "zero", "digg_count": 0},
    ]
    kept = discover.filter_by_likes(items, 1_000_000)
    assert [i["video_id"] for i in kept] == ["viral", "exact"]


def test_filter_by_likes_disabled_when_zero():
    items = [{"video_id": "any", "digg_count": 5}]
    assert discover.filter_by_likes(items, 0) == items


def test_fetch_returns_data_and_hits_endpoint_once():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = discover.fetch_feed(client, regions="RW", count=20)
    assert len(data) == 3
    assert len(seen) == 1
    assert "region=RW" in seen[0]
    assert "count=20" in seen[0]


def test_fetch_merges_multiple_regions_with_rate_limit_spacing(monkeypatch):
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = discover.fetch_feed(client, ["RW", "US", "GB"], 20)
    assert len(seen) == 3
    assert "region=RW" in seen[0]
    assert "region=US" in seen[1]
    assert "region=GB" in seen[2]
    assert len(data) == 9


def test_fetch_spaces_region_calls_by_one_second(monkeypatch):
    sleeps = []
    monkeypatch.setattr(discover.time, "sleep", sleeps.append)

    def handler(request):
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    discover.fetch_feed(client, ["RW", "US"], 20)
    assert sleeps == [discover.REGION_SPACING]


def test_fetch_with_no_regions_raises():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(discover.DiscoveryError, match="no discovery regions"):
        discover.fetch_feed(client, [], 20)


def test_fetch_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(531, text="rate limited")
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(discover.fetch_feed(client, "RW", 20)) == 3
    assert calls["n"] == 2


def test_fetch_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)

    def handler(request):
        return httpx.Response(531, text="rate limited")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(discover.DiscoveryError):
        discover.fetch_feed(client, "RW", 20)


def test_fetch_continues_past_transient_region_error(monkeypatch):
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)

    def handler(request):
        if "region=RW" in str(request.url):
            return httpx.Response(200, json={"code": -1, "msg": "Oops, an error occurred! Try again"})
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = discover.fetch_feed(client, ["RW", "US"], 20)
    assert [i["region"] for i in data] == ["PK", "RW", "US"]


def test_fetch_rate_limited_region_keeps_partial_results(monkeypatch):
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)

    def handler(request):
        if "region=US" in str(request.url):
            return httpx.Response(531, text="rate limited")
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = discover.fetch_feed(client, ["RW", "US"], 20)
    assert len(data) == 3


def test_fetch_rate_limited_first_region_still_tries_next(monkeypatch):
    # a 429 on one region must not abandon the remaining regions
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)

    def handler(request):
        if "region=RW" in str(request.url):
            return httpx.Response(429, text="too many requests")
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = discover.fetch_feed(client, ["RW", "US"], 20)
    assert len(data) == 3


def test_fetch_every_region_failing_raises(monkeypatch):
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if "region=RW" in str(request.url):
            return httpx.Response(531, text="rate limited")
        return httpx.Response(200, json={"code": -1, "msg": "Oops, an error occurred! Try again"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(discover.DiscoveryError):
        discover.fetch_feed(client, ["RW", "US"], 20)
    assert calls["n"] > 0
