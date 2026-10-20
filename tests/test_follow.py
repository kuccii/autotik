import sqlite3
from types import SimpleNamespace

import pytest

from autotik import db, follow


def make_cfg(**over):
    base = dict(
        account="shadowmorarch",
        follow_daily_cap=60,
        follow_min_gap_s=0.01,
        follow_max_gap_s=0.02,
        follow_keywords=("follow train",),
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeList:
    """Locator collection double: supports count()/nth()/first."""

    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]

    @property
    def first(self):
        return self.items[0]


class FakeButton:
    def __init__(self, text):
        self.text = text
        self.clicked = 0

    def count(self):
        return 1

    def inner_text(self):
        return self.text

    def click(self, timeout=None):
        self.clicked += 1
        self.text = "Following"


class FakePage:
    def __init__(self, lives=None, chat=None, buttons=None, body="ok"):
        self.lives = lives or ["growlive"]
        self.chat = chat or ["/@alice", "/@bob", "/@alice"]
        self.buttons = dict(buttons or {})
        self.body = body
        self.visits = []
        self.button = FakeButton("Follow")

    def goto(self, url, **kw):
        self.visits.append(url)

    def set_viewport_size(self, size):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        if "hrefs" in js:
            return {"hrefs": self.chat, "names": []}
        if "length" in js:
            return len(self.chat)
        return ["/@%s/live" % h for h in self.lives]

    def inner_text(self, sel):
        return self.body

    def locator(self, sel):
        return self

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def inner_text(self, sel=None):  # noqa: F811 - locator chain
        if sel is None:
            return self.button.text
        return self.body

    def click(self, timeout=None):
        self.button.click(timeout)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    yield c
    c.close()


class Ctx:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self, self.page

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def test_parse_usernames_filters_and_dedupes():
    hrefs = ["/@alice", "/@alice/", "/@bob?lang=en", "https://x/@carl",
             "/@bad handle", "/@zoe", "/@ZOE"]
    assert follow.parse_usernames(hrefs, exclude=("zoe",)) == ["alice", "bob"]


def test_parse_live_hosts_only_live_links():
    hrefs = ["/@a/live", "/@b", "/@c/live?x=1", "/video/1"]
    assert follow.parse_live_hosts(hrefs) == ["a", "c"]


def test_looks_blocked():
    assert follow.looks_blocked("You're following too fast. Try again later.")
    assert follow.looks_blocked("Drag the slider to fit the puzzle")
    assert not follow.looks_blocked("Something went wrong")
    assert follow.looks_soft_errored("Something went wrong")
    assert not follow.looks_blocked("Follow")


def test_looks_logged_out():
    assert follow.looks_logged_out("Log in to TikTok\nThis creator turned on audience controls")
    assert follow.looks_logged_out("Log in to make the most of TikTok")
    assert not follow.looks_logged_out("Follow\n1.2M Followers")


def test_follow_state_mapping():
    assert follow.follow_state("Follow") == "follow"
    assert follow.follow_state(" Following ") == "already"
    assert follow.follow_state("Friends") == "already"
    assert follow.follow_state("") == "unknown"


def test_dry_run_records_would_and_spends_budget(conn):
    page = FakePage()
    result = follow.run(
        make_cfg(), conn, max_follows=2, dry_run=True,
        page_factory=lambda: Ctx(page), sleep=lambda s: None,
    )
    assert result["followed"] == 2
    assert result["collected"] == 2
    assert page.button.clicked == 0
    rows = conn.execute("SELECT username, result FROM follows").fetchall()
    assert [r["result"] for r in rows] == ["would", "would"]
    assert db.follows_since(conn.cursor(), db.iso_days_ago(1)) == 2


def test_commit_clicks_follow_and_respects_known(conn):
    db.follow_log_add(conn.cursor(), "alice", "live", "", "followed")
    conn.commit()
    page = FakePage()
    result = follow.run(
        make_cfg(), conn, max_follows=5, dry_run=False,
        page_factory=lambda: Ctx(page), sleep=lambda s: None,
    )
    assert result["skipped_known"] == 1
    assert result["followed"] == 1
    assert page.button.clicked == 1
    assert conn.execute(
        "SELECT result FROM follows WHERE username='bob'"
    ).fetchone()[0] == "followed"


def test_daily_cap_stops_run(conn):
    cur = conn.cursor()
    for i in range(3):
        db.follow_log_add(cur, f"u{i}", "live", "", "followed")
    conn.commit()
    result = follow.run(
        make_cfg(follow_daily_cap=3), conn, dry_run=True,
        page_factory=lambda: Ctx(FakePage()), sleep=lambda s: None,
    )
    assert result["budget"] == 0
    assert result["followed"] == 0


def test_block_stops_and_flags(conn):
    page = FakePage(body="You're following too fast")
    result = follow.run(
        make_cfg(), conn, max_follows=5, dry_run=False,
        page_factory=lambda: Ctx(page), sleep=lambda s: None,
    )
    assert result["blocked"] is True
    assert result["followed"] == 0


def test_find_follow_button_scopes_to_profile_root():
    inbox = FakeButton("Follow")
    page = SimpleNamespace(
        locator=lambda sel: FakeList([inbox]) if sel == "button" else FakeList([])
    )
    scoped = SimpleNamespace(locator=lambda sel: FakeList([]))
    assert follow.find_follow_button(page) is inbox
    assert follow.find_follow_button(page, root=scoped) is None


def test_find_follow_button_ignores_followers_tab():
    tab = FakeButton("Followers")
    real = FakeButton("Follow")
    page = SimpleNamespace(
        locator=lambda sel: FakeList([tab, real]) if sel == "button" else FakeList([])
    )
    assert follow.find_follow_button(page) is real


def test_find_follow_button_prefers_e2e():
    primary = FakeButton("Follow")
    page = SimpleNamespace(
        locator=lambda sel: FakeList([primary]) if "follow-button" in sel else FakeList([])
    )
    assert follow.find_follow_button(page) is primary


def test_find_follow_button_none_when_absent():
    page = SimpleNamespace(locator=lambda sel: FakeList([]))
    assert follow.find_follow_button(page) is None


def test_run_joins_each_live_once(conn):
    page = FakePage(lives=["one", "two"])
    follow.run(
        make_cfg(), conn, max_follows=1, dry_run=True, lives=2,
        page_factory=lambda: Ctx(page), sleep=lambda s: None,
    )
    assert sum("one/live" in v for v in page.visits) == 1
    assert sum("two/live" in v for v in page.visits) == 1
