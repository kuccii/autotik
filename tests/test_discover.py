import json
from pathlib import Path

import httpx
import pytest

from autotik import discover

FIXTURE = Path(__file__).parent / "fixtures" / "tikwm_feed.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_region_filter_is_client_side():
    items = discover.parse_items(payload()["data"], "RW")
    assert [i["video_id"] for i in items] == ["7691089168365145350"]


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
    out = discover.rank(items, keep=2)
    assert [i["video_id"] for i in out] == ["b", "a"]
    assert out[0]["score"] == pytest.approx(1.0)


def test_rank_zero_metrics_does_not_divide_by_zero():
    items = [{"video_id": "z", "digg_count": 0, "play_count": 0, "share_count": 0, "score": 0}]
    assert discover.rank(items, keep=5)[0]["score"] == 0.0


def test_fetch_returns_data_and_hits_endpoint_once():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = discover.fetch_feed(client, region="RW", count=20)
    assert len(data) == 3
    assert len(seen) == 1
    assert "region=RW" in seen[0]
    assert "count=20" in seen[0]


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
