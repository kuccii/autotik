import json
import re
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from autotik import alert, db

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CONTENT_URL = "https://www.tiktok.com/tiktokstudio/content"
_CONSENT_SELECTORS = [
    'button:has-text("Allow all")',
    'button:has-text("Decline optional cookies")',
]
_POPUP_SELECTORS = [
    # Scoped to TUXModal: the upload page has its own page-level "Cancel"
    # button that opens a "cancel upload?" confirm modal covering the editor.
    ".TUXModal button:has-text('Cancel')",
    ".TUXModal button:has-text('Got it')",
    ".TUXModal button:has-text('Close')",
    ".TUXModal button:has-text('Skip')",
]
KIGALI = ZoneInfo("Africa/Kigali")
SYSTEM_PROMPT = (
    "You rewrite TikTok captions. Return ONLY the new caption: a punchy first "
    "line, then 3-5 relevant hashtags. Keep it under 150 characters. Never add "
    "quotes, prefixes, or explanations."
)


class PublishError(RuntimeError):
    pass


def allowed_today(posted, posts_per_day, max_posts):
    return max(0, min(posts_per_day, max_posts) - posted)


def _slot_minutes(slot):
    hour, minute = slot.split(":")
    return int(hour) * 60 + int(minute)


def _minutes_in_kigali(now):
    if now is None:
        now = datetime.now(KIGALI)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=KIGALI)
    else:
        now = now.astimezone(KIGALI)
    return now.hour * 60 + now.minute


def slots_elapsed(cfg, now=None):
    """Slots with time-of-day <= now, in Africa/Kigali. Slot scheduling only --
    deliberately separate from the UTC budget-day clock in publish_batch."""
    minutes = _minutes_in_kigali(now)
    return sum(1 for s in cfg.publish_slots if _slot_minutes(s) <= minutes)


def next_slot(cfg, now=None):
    minutes = _minutes_in_kigali(now)
    remaining = sorted(
        m for m in (_slot_minutes(s) for s in cfg.publish_slots) if m > minutes
    )
    if remaining:
        return f"{remaining[0] // 60:02d}:{remaining[0] % 60:02d} today"
    first = min(_slot_minutes(s) for s in cfg.publish_slots)
    return f"{first // 60:02d}:{first % 60:02d} tomorrow"


def rewrite_caption(client, cfg, desc):
    body = {
        "model": cfg.openrouter_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Rewrite this TikTok caption:\n{desc}"},
        ],
        "max_tokens": 200,
        "temperature": 0.9,
    }
    try:
        resp = client.post(
            OPENROUTER_URL,
            content=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.openrouter_api_key}",
            },
            timeout=45.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return text or desc
    except Exception:
        return desc


def _normalized_cookies(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    changed = False
    for cookie in data:
        if not isinstance(cookie, dict):
            continue
        if not cookie.get("url") and not (cookie.get("domain") and cookie.get("path")):
            cookie.setdefault("domain", ".tiktok.com")
            cookie.setdefault("path", "/")
            changed = True
    return data if changed else None


def _stage_cookie(cfg, account):
    accountname = account.lstrip("@")
    cookie_dir = Path(cfg.cookie_dir)
    expected = cookie_dir / f"TK_cookies_{accountname}.json"
    matches = (
        sorted(cookie_dir.glob(f"TK_cookies_{accountname}.json"))
        if cookie_dir.is_dir()
        else []
    )
    if not matches:
        raise PublishError(f"cookie file not found: {expected} — see README")
    src = matches[0]
    dest = Path.cwd() / src.name
    if src.resolve() != dest.resolve():
        normalized = _normalized_cookies(src)
        if normalized is None:
            shutil.copy2(src, dest)
        else:
            dest.write_text(json.dumps(normalized, indent=4), encoding="utf-8")
    return dest


def _dismiss_any(page, selectors, rounds=6):
    clicked = []
    for _ in range(rounds):
        acted = False
        for sel in selectors:
            loc = page.locator(sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    clicked.append(sel)
                    acted = True
                    time.sleep(0.7)
            except Exception:
                pass
        if not acted:
            break
    return clicked


def _diag_caption_failure(page, shot, log, exc):
    """Capture evidence when the caption editor cannot be clicked: screenshot
    plus what actually sits at the editor's centre and which dialogs are open."""
    log(f"caption click error: {str(exc)[:300]}")
    try:
        page.screenshot(path=str(shot))
        log(f"failure screenshot: {shot}")
    except Exception:
        pass
    try:
        info = page.evaluate(
            """() => {
                const out = {covered_by: null, dialogs: []};
                const el = document.querySelector('div[data-contents="true"]');
                if (el) {
                    const r = el.getBoundingClientRect();
                    const at = document.elementFromPoint(
                        r.left + r.width / 2, r.top + r.height / 2);
                    if (at) {
                        const chain = [];
                        let n = at;
                        while (n && chain.length < 5) {
                            chain.push(
                                n.tagName + '.' +
                                String(n.className || '').slice(0, 70));
                            n = n.parentElement;
                        }
                        out.covered_by = chain;
                    }
                }
                for (const d of document.querySelectorAll(
                        '.TUXModal-overlay, [role="dialog"]')) {
                    const r = d.getBoundingClientRect();
                    if (r.width && r.height) {
                        out.dialogs.push(
                            String(d.className || '').slice(0, 60) + ' :: ' +
                            String(d.innerText || '')
                                .replace(/\\s+/g, ' ').slice(0, 160));
                    }
                }
                return out;
            }"""
        )
        log(f"page diag: {json.dumps(info, ensure_ascii=False)[:900]}")
    except Exception as diag_exc:
        log(f"page diag failed: {str(diag_exc)[:150]}")


def _defuse_overlays(page, seconds=8):
    """TikTok renders modals AFTER the file is processed on an unpredictable
    delay ('Turn on automatic content checks?', coach marks); close whatever
    overlays the page until none is visible or the deadline passes."""
    deadline = time.time() + seconds
    while True:
        try:
            overlay = page.locator(".TUXModal-overlay")
            open_count = sum(
                1 for i in range(overlay.count()) if overlay.nth(i).is_visible()
            )
        except Exception:
            open_count = 0
        if open_count == 0:
            return True
        if time.time() > deadline:
            return False
        try:
            neutral = page.locator(
                "div.common-modal-footer > button[data-type='neutral']"
            ).first
            if neutral.is_visible():
                neutral.click(timeout=1500)
                time.sleep(0.5)
                continue
        except Exception:
            pass
        try:
            # cancel/discard-style confirm modals answer with No/Yes only
            no_btn = page.locator(".TUXModal button:text-is('No')").first
            if no_btn.is_visible():
                no_btn.click(timeout=1500)
                time.sleep(0.5)
                continue
        except Exception:
            pass
        _dismiss_any(page, _POPUP_SELECTORS, rounds=1)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.4)


def _content_snapshot(page, wait_ms=9000):
    if "/tiktokstudio/content" not in page.url:
        page.goto(CONTENT_URL, wait_until="domcontentloaded", timeout=60000)
        _dismiss_any(page, _CONSENT_SELECTORS, rounds=3)
    page.wait_for_timeout(wait_ms)
    text = page.inner_text("body")
    # a dead/invalid session redirects the Studio content page to the login
    # wall; surface it explicitly instead of the opaque "Posts count missing"
    if "/login" in page.url:
        raise PublishError(
            "tiktok session invalid (redirected to login wall); "
            "re-export cookies and replace the cookie file"
        )
    m = re.search(r"Posts\s+(\d+)", text)
    ids = set(re.findall(r"/video/(\d{15,25})", page.content()))
    return (int(m.group(1)) if m else None), ids, text


def _run_tiktok_upload(video, caption, cookie_path, accountname):
    try:
        from phantomwright_driver.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright
    from tiktokautouploader import function as fn

    from autotik import session

    def log(msg):
        print(f"[publish:{accountname}] {msg}", flush=True)

    caption = " ".join(caption.split())[:4000]
    if not caption:
        raise PublishError("empty caption, refusing to post")

    shot = Path("/app/data/upload_state.png")
    with sync_playwright() as p:
        context, page = session.open_context(p, cookie_path)
        try:
            baseline, ids_before, _ = _content_snapshot(page)
            if baseline is None:
                raise PublishError(
                    "cannot read content page baseline (Posts count missing)"
                )
            log(f"baseline posts={baseline}")

            sim = fn.SyncUserSimulator(page)
            fn._goto_with_retry(page, fn.UPLOAD_URL)
            time.sleep(2)
            _dismiss_any(page, _CONSENT_SELECTORS, rounds=3)
            sim.simulate_browsing(duration_ms=1500)
            if fn._wait_for_upload_or_captcha(page):
                log("captcha detected, solving")
                fn._solve_captcha_if_needed(page, False)

            page.set_input_files('input[type="file"][accept="video/*"]', video)
            log("file input set")
            page.wait_for_selector('div[data-contents="true"]', timeout=120000)
            time.sleep(1)
            _dismiss_any(page, _CONSENT_SELECTORS, rounds=3)
            _dismiss_any(page, _POPUP_SELECTORS)
            _defuse_overlays(page, seconds=8)

            desc_box = page.locator('div[data-contents="true"]').first
            last_exc = None
            for attempt in range(3):
                try:
                    sim.click(desc_box)
                    break
                except Exception as exc:
                    last_exc = exc
                    log(f"caption editor blocked (attempt {attempt + 1})")
                    _defuse_overlays(page, seconds=6)
                    _dismiss_any(page, _POPUP_SELECTORS, rounds=2)
                    time.sleep(0.5)
            else:
                _diag_caption_failure(page, shot, log, last_exc)
                raise PublishError(
                    "caption editor unreachable (overlay persists): "
                    f"{str(last_exc)[:200]}"
                )
            time.sleep(0.5)
            for _ in range(len(caption) + 5):
                page.keyboard.press("Backspace")
                page.keyboard.press("Delete")
            sim.type(desc_box, caption)
            log("description typed")

            footer = page.locator("div.common-modal-footer > button[data-type='neutral']")
            try:
                if footer.count() > 0 and footer.first.is_visible():
                    footer.first.click(timeout=2000)
            except Exception:
                pass

            page.wait_for_selector(
                'button:has-text("Post")[aria-disabled="false"]', timeout=600000
            )
            log("post button enabled")
            _dismiss_any(page, _CONSENT_SELECTORS, rounds=2)
            _dismiss_any(page, _POPUP_SELECTORS, rounds=2)
            time.sleep(1)
            _defuse_overlays(page, seconds=6)

            btn = page.locator('button[data-e2e="post_video_button"]').first
            try:
                page.screenshot(path=str(shot))
            except Exception:
                pass
            btn.scroll_into_view_if_needed(timeout=5000)
            clicked = None
            for how, fn_click in (
                ("normal", lambda: btn.click(timeout=10000)),
                ("force", lambda: btn.click(timeout=5000, force=True)),
                ("js", lambda: btn.evaluate("b => b.click()")),
            ):
                try:
                    fn_click()
                    clicked = how
                    break
                except Exception as exc:
                    log(f"{how} click failed: {str(exc)[:120]}")
            if not clicked:
                raise PublishError("could not click Post button")
            log(f"clicked via {clicked}")
            time.sleep(2)

            try:
                pn = page.locator('button:has-text("Post now")')
                if pn.count() > 0 and pn.first.is_visible():
                    pn.first.click(timeout=3000)
                    log("post-now popup clicked")
            except Exception:
                pass

            signal = None
            deadline = time.time() + 60
            while time.time() < deadline:
                if "/tiktokstudio/content" in page.url:
                    signal = "url"
                    break
                try:
                    html = page.content()
                except Exception:
                    html = ""
                if any(
                    t in html.lower()
                    for t in (
                        "video has been uploaded",
                        "upload success",
                        "check video",
                        "view post",
                    )
                ):
                    signal = "text"
                    break
                time.sleep(1)
            log(f"success signal: {signal}")

            after, ids_after, text = _content_snapshot(page, wait_ms=3000)
            new_ids = ids_after - ids_before
            if not (
                (baseline is not None and after is not None and after > baseline)
                or new_ids
            ):
                try:
                    page.screenshot(path=str(shot))
                except Exception:
                    pass
                raise PublishError(
                    f"upload not confirmed (signal={signal}, posts "
                    f"{baseline}->{after}, new_ids={sorted(new_ids)[:3]}, "
                    f"page={text[:200]!r})"
                )
            log(f"confirmed: posts {baseline}->{after}, new={sorted(new_ids)[:3]}")
        except PublishError:
            raise
        except Exception as exc:
            try:
                page.screenshot(path=str(shot))
            except Exception:
                pass
            raise PublishError(f"upload flow failed: {exc}") from exc
        finally:
            context.close()


def default_uploader(cfg):
    def upload(video_path, caption, account):
        cookie = _stage_cookie(cfg, account)
        _run_tiktok_upload(
            str(video_path), caption, str(cookie), account.lstrip("@")
        )

    return upload


def session_check(cfg):
    """Pre-flight + keep-alive: open the persistent profile, load the Studio
    content page (raises PublishError on the login wall), dwell like a human
    briefly, and confirm the session still reads the Posts count."""
    cookie = _stage_cookie(cfg, cfg.account)
    try:
        from phantomwright_driver.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright

    from autotik import session

    with sync_playwright() as p:
        context, page = session.open_context(p, str(cookie))
        try:
            posts, _ids, _text = _content_snapshot(page, wait_ms=4000)
            if posts is None:
                raise PublishError(
                    "cannot read content page baseline (Posts count missing)"
                )
            try:
                page.mouse.wheel(0, 800)
                page.wait_for_timeout(1500)
                page.mouse.wheel(0, -400)
                page.wait_for_timeout(1500)
                _content_snapshot(page, wait_ms=2000)
            except PublishError:
                raise
            except Exception:
                pass
            return {"ok": True, "posts": posts}
        finally:
            context.close()


def publish_batch(cfg, conn, auto=False, uploader=None, client=None):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = conn.cursor()

    if cfg.stage_only and not auto:
        n = len(db.rows_with_status(cur, "approved"))
        return {"posted": 0, "failed": 0, "skipped": n, "not_due": False, "reason": None}

    elapsed = slots_elapsed(cfg)
    if elapsed == 0:
        return {"posted": 0, "failed": 0, "skipped": 0, "not_due": True, "reason": "not_due"}

    already = db.posted_today(cur, day)
    # one post per elapsed slot, capped by the daily cap: a slot that passed
    # before its cron fired must not be "made up" as a back-to-back burst
    allowed = min(
        allowed_today(already, cfg.posts_per_day, cfg.max_posts_per_day),
        max(0, elapsed - already),
    )
    if allowed <= 0:
        return {
            "posted": 0, "failed": 0, "skipped": 0,
            "not_due": False, "reason": "budget_spent",
        }

    approved = db.rows_with_status(cur, "approved")
    candidates = [r for r in approved if r["file_path"]]
    missing = [r for r in approved if not r["file_path"]]
    # drop rows that cannot produce a caption before they can consume a slot
    unusable = [
        r for r in candidates
        if not (r["desc_orig"] or "").strip()
    ]
    unusable_ids = {r["video_id"] for r in unusable}
    candidates = [r for r in candidates if r["video_id"] not in unusable_ids]

    result = {"posted": 0, "failed": 0, "skipped": len(missing), "not_due": False, "reason": None}
    for row in missing:
        db.set_status(cur, row["video_id"], "failed", "no file_path recorded")
        alert.send(cfg.n8n_webhook_url, "warn", f"missing file for {row['video_id']}")
    for row in unusable:
        db.set_status(cur, row["video_id"], "failed", "empty caption: source desc is empty")
        db.log(cur, row["video_id"], "upload_failed", "empty caption, refusing to post")
        result["failed"] += 1
    if unusable:
        alert.send(
            cfg.n8n_webhook_url, "warn",
            f"{len(unusable)} approved row(s) skipped: empty source desc",
        )
    conn.commit()

    own_client = client is None
    if own_client:
        import httpx
        client = httpx.Client()
    upload = uploader or default_uploader(cfg)

    try:
        for row in candidates[:allowed]:
            vid = row["video_id"]
            path = Path(row["file_path"])
            if not path.exists():
                db.set_status(cur, vid, "failed", f"file missing: {path}")
                result["failed"] += 1
                alert.send(cfg.n8n_webhook_url, "error", f"file missing: {path}")
                conn.commit()
                continue
            source = (row["desc_orig"] or "").strip()
            if not source:
                db.set_status(
                    cur, vid, "failed", "empty caption: source desc is empty"
                )
                db.log(cur, vid, "upload_failed", "empty caption, refusing to post")
                result["failed"] += 1
                alert.send(
                    cfg.n8n_webhook_url, "warn",
                    f"empty caption for {vid}: source desc empty, skipping",
                )
                conn.commit()
                continue
            caption = rewrite_caption(client, cfg, source) or source
            try:
                ret = upload(path, caption, cfg.account)
                if ret == "Error":
                    raise PublishError(
                        f"uploader returned {ret!r}: cannot confirm if "
                        "uploaded successfully"
                    )
            except Exception as exc:
                db.set_status(cur, vid, "failed", str(exc)[:500])
                db.log(cur, vid, "upload_failed", str(exc)[:500])
                result["failed"] += 1
                alert.send(
                    cfg.n8n_webhook_url, "error",
                    f"upload failed for {vid}: {exc}",
                )
                conn.commit()
                break
            cur.execute(
                "UPDATE videos SET caption=? WHERE video_id=?", (caption, vid)
            )
            db.set_status(cur, vid, "posted")
            db.log(cur, vid, "posted", caption[:120])
            result["posted"] += 1
            conn.commit()
    finally:
        if own_client:
            client.close()

    if result["posted"] == 0 and result["failed"] > 0:
        alert.send(cfg.n8n_webhook_url, "error", "publish produced 0 posts")
    return result
