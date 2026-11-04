"""Automatic re-login for TikTok when the stored session dies.

The persistent browser profile holds the session; when TikTok revokes it the
Studio page redirects to the login wall. This module drives the email/password
form once, then proves it worked by asking TikTok who we are -- the same
identity check `publish.assert_account` uses.

Nothing here echoes the password: reasons and log lines never include it.
"""

from __future__ import annotations

LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"
QR_LOGIN_URL = "https://www.tiktok.com/login"
QR_SHOT = "/app/data/qr_login.png"
QR_TRIGGER_SELECTORS = (
    'button:has-text("Use QR code")',
    'a:has-text("Use QR code")',
    'div[role="button"]:has-text("Use QR code")',
    'text="Use QR code"',
)
QR_SELECTORS = (
    'div[data-e2e="qrcode"] canvas',
    'div[data-e2e="qrcode"] img',
    'canvas',
)

EMAIL_SELECTORS = (
    'input[name="username"]',
    'input[name="email"]',
    'input[type="text"][placeholder*="mail" i]',
    'input[type="text"]',
)
PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button:has-text("Log in")',
    'button:has-text("Login")',
)
CAPTCHA_MARKERS = (
    "drag the slider",
    "drag the puzzle",
    "verify to continue",
    "security check",
    "verify you are human",
    "too many attempts",
    "try again later",
)
CONSENT_SELECTORS = (
    'button:has-text("Decline optional cookies")',
    'button:has-text("Decline all")',
)

READY_TIMEOUT_S = 45
POLL_S = 1.0


class LoginError(RuntimeError):
    pass


def looks_like_captcha(text):
    low = (text or "").lower()
    return any(marker in low for marker in CAPTCHA_MARKERS)


def _first_visible(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _dismiss_consent(page):
    for sel in CONSENT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=1500)
                time.sleep(0.3)
        except Exception:
            pass


def _identity(page):
    from autotik import publish

    return publish.account_identity(page)


def do_login(page, cfg, log=print):
    """Drive the email/password form. Returns True on a verified login.

    Raises LoginError with a short reason when it cannot proceed (no
    credentials, captcha, wrong account)."""
    email = (getattr(cfg, "tiktok_email", "") or "").strip()
    password = getattr(cfg, "tiktok_password", "") or ""
    if not email or not password:
        raise LoginError("no TIKTOK_EMAIL/TIKTOK_PASSWORD configured")

    expected = (cfg.account or "").strip().lstrip("@")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    _dismiss_consent(page)

    already = _identity(page)
    if already and already.lower() == expected.lower():
        log(f"[login] already signed in as @{already}")
        return True

    email_box = _first_visible(page, EMAIL_SELECTORS)
    pass_box = _first_visible(page, PASSWORD_SELECTORS)
    if email_box is None or pass_box is None:
        text = page.inner_text("body") if hasattr(page, "inner_text") else ""
        if looks_like_captcha(text):
            raise LoginError("captcha on login page")
        raise LoginError("login form not found")

    email_box.click(timeout=5000)
    email_box.fill(email)
    page.wait_for_timeout(400)
    pass_box.click(timeout=5000)
    pass_box.fill(password)
    page.wait_for_timeout(400)

    submit = _first_visible(page, SUBMIT_SELECTORS)
    if submit is None:
        raise LoginError("login submit button not found")
    submit.click(timeout=5000)
    log("[login] submitted credentials")

    deadline_attempts = max(1, int(READY_TIMEOUT_S / POLL_S))
    last_text = ""
    for _ in range(deadline_attempts):
        page.wait_for_timeout(int(POLL_S * 1000))
        try:
            last_text = page.inner_text("body")
        except Exception:
            last_text = ""
        if looks_like_captcha(last_text):
            raise LoginError("captcha after submit")
        who = _identity(page)
        if who and who.lower() == expected.lower():
            log(f"[login] verified as @{who}")
            return True
        if who and who.lower() != expected.lower():
            raise LoginError(
                f"logged in as @{who}, expected @{expected}"
            )
    raise LoginError("login did not take effect in time")


def qr_flow(page, cfg, log=print, timeout_s=240, shot=QR_SHOT):
    """Show the QR code and wait for the phone to confirm. Page-level only."""
    from pathlib import Path

    expected = (cfg.account or "").strip().lstrip("@")
    who = _identity(page)
    if who and who.lower() == expected.lower():
        log(f"[login] already signed in as @{who}")
        return True
    page.goto(QR_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    _dismiss_consent(page)
    node = _first_visible(page, QR_SELECTORS)
    if node is None:
        # the login page opens on the method chooser; the QR lives one click in
        trigger = _first_visible(page, QR_TRIGGER_SELECTORS)
        if trigger is not None:
            try:
                trigger.click(timeout=4000)
            except Exception:
                pass
            for _ in range(10):
                page.wait_for_timeout(1000)
                node = _first_visible(page, QR_SELECTORS)
                if node is not None:
                    break
    if node is None:
        text = page.inner_text("body")
        if looks_like_captcha(text):
            raise LoginError("captcha instead of QR code")
        raise LoginError("QR code not found on login page")
    Path(shot).parent.mkdir(parents=True, exist_ok=True)
    try:
        node.screenshot(path=shot)
    except Exception:
        page.screenshot(path=shot)
    log(f"[login] QR code saved to {shot} (valid ~2 min, scan with TikTok app)")

    attempts = max(1, int(timeout_s / 3))
    for _ in range(attempts):
        page.wait_for_timeout(3000)
        who = _identity(page)
        if who and who.lower() == expected.lower():
            log(f"[login] verified as @{who}")
            return True
    raise LoginError("QR not scanned in time")


def qr_login(cfg, log=print, timeout_s=240, shot=QR_SHOT):
    """Log in by scanning a QR code with the TikTok app.

    No form, no captcha. Writes the code to `shot` so it can be shown to the
    operator, then polls until the phone confirms or the code expires."""
    from autotik import publish, session

    try:
        from phantomwright_driver.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright

    cookie_path = publish._stage_cookie(cfg, cfg.account)
    with sync_playwright() as p:
        context, page = session.open_context(p, str(cookie_path))
        try:
            return qr_flow(page, cfg, log=log, timeout_s=timeout_s, shot=shot)
        finally:
            try:
                context.close()
            except Exception:
                pass


def login(cfg, log=print):
    """Open the persistent profile, log in, and leave the session there."""
    from autotik import publish, session

    try:
        from phantomwright_driver.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright

    cookie_path = publish._stage_cookie(cfg, cfg.account)
    with sync_playwright() as p:
        context, page = session.open_context(p, str(cookie_path))
        try:
            return do_login(page, cfg, log=log)
        finally:
            try:
                context.close()
            except Exception:
                pass
