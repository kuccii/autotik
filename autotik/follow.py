"""Follow-for-growth from TikTok LIVE rooms.

Targets the "grow your account" style lives, where commenting is the entry
ticket, and follows the people who comment. Kept deliberately conservative:
a hard daily cap, human-paced gaps, a per-user dedupe table, and a block
detector that stops the run the moment TikTok complains.
"""

import random
import time

SEARCH_URL = "https://www.tiktok.com/search?q={query}"
PROFILE_URL = "https://www.tiktok.com/@{user}"
LIVE_URL = "https://www.tiktok.com/@{user}/live"

BLOCK_MARKERS = (
    "following too fast",
    "too many requests",
    "unusual activity",
    "action blocked",
    "drag the slider",
    "fit the puzzle",
    "verify you're human",
    "security check",
)

# Transient UI noise that must NOT be read as a block ("Something went wrong"
# appears in lazily-loaded modules on healthy pages).
SOFT_MARKERS = ("try again later", "something went wrong")

CHAT_USERNAMES_JS = """
() => {
  const roots = Array.from(document.querySelectorAll(
    "[data-e2e='live-chat-container'],[data-e2e='chat-message'],[data-e2e*='chat']"
  ));
  const scope = roots.length ? roots : [document.body];
  const hrefs = [];
  const names = [];
  for (const root of scope) {
    for (const a of root.querySelectorAll("a[href^='/@']")) {
      hrefs.push(a.getAttribute("href"));
    }
    for (const n of root.querySelectorAll("[data-e2e='message-owner-name']")) {
      const t = (n.innerText || "").trim();
      if (t) names.push("/@" + t.replace(/^@/, ""));
    }
  }
  return {hrefs: hrefs, names: names};
}
"""

CHAT_COUNT_JS = """
() => document.querySelectorAll("[data-e2e='chat-message']").length
"""

# The chat rail is not rendered below ~1500px wide on tiktok.com/live.
CHAT_VIEWPORT = {"width": 1600, "height": 950}

LIVE_HOSTS_JS = """
() => Array.from(document.querySelectorAll("a[href*='/live']"))
  .map((a) => a.getAttribute("href"))
"""


class FollowBlocked(RuntimeError):
    pass


def parse_usernames(hrefs, exclude=()):
    """Pull unique @handles out of hrefs, preserving order."""
    blocked = {e.lower().lstrip("@") for e in exclude if e}
    seen = set()
    out = []
    for href in hrefs or ():
        if not isinstance(href, str) or not href.startswith("/@"):
            continue
        handle = href[2:].split("/")[0].split("?")[0].strip()
        if not handle or len(handle) > 24:
            continue
        if not all(c.isalnum() or c in "._" for c in handle):
            continue
        key = handle.lower()
        if key in blocked or key in seen:
            continue
        seen.add(key)
        out.append(handle)
    return out


def parse_live_hosts(hrefs, exclude=()):
    """Handles whose href is a live-room link (/@user/live)."""
    live = [
        h for h in (hrefs or ())
        if isinstance(h, str) and h.startswith("/@") and "/live" in h
    ]
    return parse_usernames(live, exclude)


def looks_blocked(text):
    low = (text or "").lower()
    return any(marker in low for marker in BLOCK_MARKERS)


def looks_soft_errored(text):
    low = (text or "").lower()
    return any(marker in low for marker in SOFT_MARKERS)


def looks_logged_out(text):
    low = (text or "").lower()
    return "log in to tiktok" in low or "log in to make the most" in low


def follow_state(button_text):
    text = (button_text or "").strip().lower()
    if text in ("following", "friends", "requested", "pending"):
        return "already"
    if text == "follow":
        return "follow"
    return "unknown"


def run(
    cfg, conn, max_follows=None, dry_run=True, keywords=None, lives=3,
    page_factory=None, sleep=None, rng=None, log=None,
):
    """Join growth lives and follow the people commenting in them.

    page_factory() must return a context manager yielding (context, page).
    Everything network-facing goes through it so the loop stays testable.
    """
    from autotik import db

    sleep = sleep or time.sleep
    rng = rng or random.Random()
    log = log or (lambda msg: None)
    keywords = keywords or list(cfg.follow_keywords)
    cap = min(max_follows or cfg.follow_daily_cap, cfg.follow_daily_cap)
    cur = conn.cursor()

    spent = db.follows_since(cur, db.iso_days_ago(1))
    budget = max(0, cap - spent)
    known = db.followed_usernames(cur)
    result = {
        "dry_run": dry_run, "budget": budget, "lives": 0,
        "collected": 0, "followed": 0, "already": 0, "errors": 0,
        "blocked": False, "skipped_known": 0,
    }
    if budget <= 0:
        log(f"follow: daily cap reached ({spent}/{cap})")
        return result

    factory = page_factory or _real_factory(cfg)
    with factory() as (context, page):
        try:
            handles = []
            for keyword in keywords:
                if result["blocked"]:
                    break
                if len(handles) >= budget * 4 or result["lives"] >= lives:
                    break
                for host in _search_lives(page, keyword, exclude=(cfg.account,)):
                    if result["lives"] >= lives:
                        break
                    result["lives"] += 1
                    try:
                        names = _join_and_collect(
                            page, host, exclude=(cfg.account,)
                        )
                    except FollowBlocked as exc:
                        result["blocked"] = True
                        log(f"follow: blocked entering live ({exc})")
                        break
                    log(f"live @{host}: {len(names)} commenters")
                    handles.extend(names)

            result["collected"] = len(handles)
            for handle in handles:
                if result["followed"] + result["already"] >= budget:
                    break
                if handle.lower() in known:
                    result["skipped_known"] += 1
                    continue
                try:
                    state = _follow(page, handle, dry_run=dry_run)
                except FollowBlocked as exc:
                    db.follow_log_add(cur, handle, "live", "", "blocked")
                    conn.commit()
                    result["blocked"] = True
                    log(f"follow: blocked ({exc})")
                    break
                except Exception as exc:
                    result["errors"] += 1
                    db.follow_log_add(cur, handle, "live", "", f"error:{exc}"[:120])
                    conn.commit()
                    continue
                known.add(handle.lower())
                if state == "followed":
                    result["followed"] += 1
                    db.follow_log_add(
                        cur, handle, "live", "", "would" if dry_run else "followed"
                    )
                elif state == "already":
                    result["already"] += 1
                    db.follow_log_add(cur, handle, "live", "", "already")
                else:
                    result["errors"] += 1
                    db.follow_log_add(cur, handle, "live", "", "no-button")
                conn.commit()
                sleep(rng.uniform(cfg.follow_min_gap_s, cfg.follow_max_gap_s))
        finally:
            context.close()
    return result


def _real_factory(cfg):
    from autotik import publish, session

    try:
        from phantomwright_driver.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright

    def factory():
        class _Ctx:
            def __enter__(self):
                self._p = sync_playwright()
                pw = self._p.__enter__()
                cookie = publish._stage_cookie(cfg, cfg.account)
                self._ctx, page = session.open_context(pw, str(cookie))
                return self._ctx, page

            def __exit__(self, *exc):
                try:
                    self._ctx.close()
                finally:
                    self._p.__exit__(*exc)

        return _Ctx()

    return factory


def _search_lives(page, keyword, exclude=()):
    from urllib.parse import quote

    page.goto(
        SEARCH_URL.format(query=quote(keyword)),
        wait_until="domcontentloaded", timeout=60000,
    )
    page.wait_for_timeout(6000)
    hosts = parse_live_hosts(page.evaluate(LIVE_HOSTS_JS), exclude=exclude)
    if hosts:
        return hosts
    page.goto("https://www.tiktok.com/live", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_timeout(6000)
    return parse_live_hosts(page.evaluate(LIVE_HOSTS_JS), exclude=exclude)


def _join_and_collect(page, host, exclude=()):
    try:
        page.set_viewport_size(CHAT_VIEWPORT)
    except Exception:
        pass
    page.goto(LIVE_URL.format(user=host), wait_until="domcontentloaded",
              timeout=60000)
    waited = 0
    while waited < 24000:
        page.wait_for_timeout(3000)
        waited += 3000
        try:
            if page.evaluate(CHAT_COUNT_JS):
                break
        except Exception:
            break
    if looks_blocked(page.inner_text("body")):
        raise FollowBlocked("blocked while opening live")
    data = page.evaluate(CHAT_USERNAMES_JS) or {}
    handles = parse_usernames(data.get("hrefs"), exclude=tuple(exclude) + (host,))
    if not handles:
        # Chat rows sometimes expose only a display name; /@display resolves
        # case-insensitively, but it is a weaker signal.
        handles = parse_usernames(data.get("names"), exclude=tuple(exclude) + (host,))
    return handles


def find_follow_button(page, root=None):
    """The profile follow button, never the "Followers" tab or an inbox row.

    data-e2e is authoritative; the text fallback requires an exact match
    because ':has-text("Follow")' also hits the Followers tab.
    """
    if root is None:
        root = page
    primary = root.locator("button[data-e2e='follow-button']")
    if primary.count():
        return primary.first
    every = root.locator("button")
    total = min(every.count(), 25)
    for i in range(total):
        candidate = every.nth(i)
        try:
            label = (candidate.inner_text() or "").strip().lower()
        except Exception:
            continue
        if label in ("follow", "following", "friends", "requested"):
            return candidate
    return None


def _profile_root(page):
    """Scope to the profile page body so notification widgets are excluded."""
    try:
        root = page.locator("[data-e2e='user-page']")
        if root.count():
            return root
    except Exception:
        pass
    return page


PROFILE_READY_JS = """
() => !!document.querySelector("[data-e2e='user-page'],[data-e2e='user-title']")
  || /this account is private/i.test(document.body.innerText)
"""


def _open_profile(page, handle):
    # A hard reset drops SPA state; otherwise a previously opened notification
    # panel stays mounted and its "Follow back" buttons look like profile ones.
    page.goto("about:blank")
    for attempt in range(2):
        page.goto(PROFILE_URL.format(user=handle),
                  wait_until="domcontentloaded", timeout=60000)
        waited = 0
        while waited < 12000:
            page.wait_for_timeout(2000)
            waited += 2000
            try:
                if page.evaluate(PROFILE_READY_JS):
                    return True
            except Exception:
                break
        if attempt == 0:
            page.wait_for_timeout(1500)
    return False


def _follow(page, handle, dry_run=False):
    if not _open_profile(page, handle):
        return "no-profile"
    body = page.inner_text("body")
    if looks_blocked(body):
        raise FollowBlocked("blocked while opening profile")
    if looks_logged_out(body):
        raise FollowBlocked("session served a logged-out shell")
    button = None
    for _ in range(6):
        button = find_follow_button(page, root=_profile_root(page))
        if button is not None:
            break
        page.wait_for_timeout(2000)
    if button is None:
        return "no-button"
    state = follow_state(button.inner_text())
    if state == "already":
        return "already"
    if state != "follow":
        return "no-button"
    if dry_run:
        return "followed"
    button.click(timeout=8000)
    for _ in range(4):
        page.wait_for_timeout(1200)
        if looks_blocked(page.inner_text("body")):
            raise FollowBlocked("blocked after clicking follow")
        again = find_follow_button(page, root=_profile_root(page))
        if again is not None and follow_state(again.inner_text()) == "already":
            return "followed"
    return "no-button"
