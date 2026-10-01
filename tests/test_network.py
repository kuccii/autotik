import httpx
import pytest

from autotik import discover

pytestmark = pytest.mark.network


def test_live_tikwm_feed_fetches_and_parses():
    try:
        with httpx.Client(follow_redirects=True) as client:
            feed = discover.fetch_feed(client, "RW", 3)
    except (discover.DiscoveryError, httpx.HTTPError) as exc:
        pytest.skip(f"live tikwm unavailable: {exc}")
    assert feed, "live feed came back empty"
    items = discover.parse_items(feed, "RW")
    assert isinstance(items, list)
    for i in items:
        assert i["video_id"]
        assert i["region"] == "RW"
        assert i["play_url"].startswith("https://")
        assert i["source_url"].startswith("https://www.tiktok.com/")
