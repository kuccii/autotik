from datetime import datetime, timezone

from autotik import analytics, db


def test_parse_short_number_handles_suffixes_and_commas():
    assert analytics.parse_short_number("9.4K") == 9400
    assert analytics.parse_short_number("1,234") == 1234
    assert analytics.parse_short_number("2.1M") == 2_100_000
    assert analytics.parse_short_number("n/a") is None


def test_parse_duration():
    assert analytics.parse_duration("01:03") == 63
    assert analytics.parse_duration("1:02:03") == 3723
    assert analytics.parse_duration("05") == 5
    assert analytics.parse_duration("abc") is None


def test_parse_overview_extracts_retention_and_restriction():
    text = """
    Your video is not eligible for recommendation in the For You feed.
    Video views 305 Total play time 0h:24m:34s Average watch time 4.41s
    Watched full video 1.2% New followers 0
    """
    out = analytics.parse_overview(text)
    assert out["views"] == 305
    assert out["avg_watch_s"] == 4.41
    assert out["full_watch_pct"] == 1.2
    assert out["new_followers"] == 0
    assert out["restricted"] is True


def test_parse_overview_clean_video():
    out = analytics.parse_overview(
        "Video views 12.4K Average watch time 9.02s Watched full video 48.5% "
        "New followers 3"
    )
    assert out == {
        "views": 12400,
        "avg_watch_s": 9.02,
        "full_watch_pct": 48.5,
        "new_followers": 3,
        "restricted": False,
    }


def test_parse_followers():
    assert analytics.parse_followers("Total followers 43 All time Net") == 43
    assert analytics.parse_followers("Total followers 1.2K") == 1200
    assert analytics.parse_followers("no data") is None


def test_parse_content_rows_merged_duration_cell():
    rows = [
        ["1", "01:03\nAbelito encuentra a Sofi", "305", "305", "1w ago", "View data"],
        ["2", "00:07\nCaught me scrolling", "1.2K", "1.2K", "2d ago", "View data"],
    ]
    out = analytics.parse_content_rows(rows)
    assert out == [
        {"caption": "Abelito encuentra a Sofi", "duration_s": 63, "views": 305},
        {"caption": "Caught me scrolling", "duration_s": 7, "views": 1200},
    ]


def test_parse_content_rows_split_cells():
    rows = [
        ["1", "01:03", "Abelito encuentra a Sofi", "305", "305", "1w ago", "View data"],
        ["bad"],
    ]
    out = analytics.parse_content_rows(rows)
    assert out == [
        {"caption": "Abelito encuentra a Sofi", "duration_s": 63, "views": 305},
    ]


def test_match_caption_handles_truncation():
    pairs = [("111", "Caught me scrolling at 3am again 😅 #RelatableRwanda #EastAfricaVibes")]
    assert analytics.match_caption(
        "Caught me scrolling at 3am again 😅 #Relatable", pairs
    ) == "111"
    assert analytics.match_caption("totally different video", pairs) is None


def test_summary_uses_latest_snapshot_per_video(tmp_path):
    conn = db.init_db(tmp_path / "autotik.sqlite")
    cur = conn.cursor()
    db.add_post_stats(cur, "v1", views=100, avg_watch_s=2.0, full_watch_pct=1.0,
                      new_followers=0, restricted=True)
    db.add_post_stats(cur, "v1", views=300, avg_watch_s=4.4, full_watch_pct=1.2,
                      new_followers=1, restricted=True)
    db.add_post_stats(cur, "v2", views=200, avg_watch_s=8.0, full_watch_pct=40.0,
                      new_followers=2, restricted=False)
    db.add_account_snapshot(cur, 43)
    conn.commit()
    out = analytics.summary(conn)
    assert out["followers"] == 43
    assert out["videos"] == 2
    assert out["views"] == 500
    assert out["restricted"] == 1
    assert out["avg_watch_s"] == 6.2
    assert out["avg_full_watch_pct"] == 20.6
