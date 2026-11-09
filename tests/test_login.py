"""Login form driving + automatic session healing, without a browser."""

import pytest

from autotik import config, login, publish


class FakeField:
    def __init__(self, page, name):
        self.page = page
        self.name = name

    def count(self):
        return 1 if self.page.has(self.name) else 0

    def is_visible(self):
        return self.page.has(self.name)

    def click(self, timeout=None):
        self.page.clicked.append(self.name)
        if "submit" in self.name or "Log in" in self.name:
            self.page.submits += 1

    def fill(self, value):
        self.page.filled[self.name] = value

    @property
    def first(self):
        return self


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        return 1 if self.page.has(self.selector) else 0

    @property
    def first(self):
        return FakeField(self.page, self.selector)

    def __call__(self):
        return self

    def is_visible(self):
        return self.page.has(self.selector)


class FakePage:
    """Enough of Playwright for do_login: fields, submit, body text, identity."""

    def __init__(self, identity_before="", identity_after="shadowmorarch",
                 body="", captcha_after=None):
        self.identity_before = identity_before
        self.identity_after = identity_after
        self.filled = {}
        self.clicked = []
        self.gone = {}
        self.body = body
        self.captcha_after = captcha_after
        self.submits = 0
        self.url = ""

    def has(self, selector):
        if "username" in selector or "email" in selector or 'type="text"' in selector:
            return not self.gone.get("email")
        if "password" in selector:
            return not self.gone.get("password")
        if "submit" in selector or "Log in" in selector or "Login" in selector:
            return not self.gone.get("submit")
        if "Decline" in selector:
            return False
        return False

    def locator(self, selector):
        return FakeLocator(self, selector)

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def inner_text(self, selector=None):
        return self.captcha_after if self.submits else self.body

    def evaluate(self, js, arg=None):
        who = self.identity_after if self.submits else self.identity_before
        return {"data": {"username": who}} if who else {"data": {}}


def _cfg(**over):
    base = dict(
        region="RW",
        account="@shadowmorarch",
        openrouter_api_key="k",
        openrouter_model="m",
        n8n_webhook_url="",
        data_dir="data",
        cookie_dir="cookies",
        discover_fetch=1,
        discover_keep=1,
        discover_regions=("RW",),
        discover_max_age_days=30,
        discover_min_likes=0,
        discover_keywords=(),
        discover_max_duration_s=90,
        posts_per_day=1,
        max_posts_per_day=1,
        publish_slots=("08:00",),
        stage_only=False,
        overlay_enabled=False,
        overlay_account="",
        overlay_style="bar",
        transform_variant_ratio=0.0,
        follow_daily_cap=0,
        follow_min_gap_s=45,
        follow_max_gap_s=120,
        follow_keywords=(),
        tiktok_email="impatrickhi@gmail.com",
        tiktok_password="secret",
    )
    base.update(over)
    return config.Config(**base)


def test_login_fills_credentials_and_submits():
    page = FakePage()
    assert login.do_login(page, _cfg(), log=lambda *_: None) is True
    assert page.filled["input[name=\"username\"]"] == "impatrickhi@gmail.com"
    assert page.filled["input[type=\"password\"]"] == "secret"
    assert page.submits == 1
    assert page.url == login.LOGIN_URL


def test_login_skips_form_when_already_signed_in():
    page = FakePage(identity_before="shadowmorarch")
    assert login.do_login(page, _cfg(), log=lambda *_: None) is True
    assert page.filled == {}


def test_login_detects_captcha_after_submit():
    page = FakePage(captcha_after="Drag the slider to fit the puzzle")
    with pytest.raises(login.LoginError) as exc:
        login.do_login(page, _cfg(), log=lambda *_: None)
    assert "captcha" in str(exc.value).lower()


def test_login_refuses_without_credentials():
    with pytest.raises(login.LoginError) as exc:
        login.do_login(FakePage(), _cfg(tiktok_email="", tiktok_password=""))
    assert "TIKTOK_EMAIL" in str(exc.value)


def test_login_rejects_wrong_account():
    page = FakePage(identity_after="someone_else")
    with pytest.raises(login.LoginError) as exc:
        login.do_login(page, _cfg(), log=lambda *_: None)
    assert "expected @shadowmorarch" in str(exc.value)


def test_login_never_leaks_password_in_reason():
    page = FakePage(identity_after="someone_else")
    try:
        login.do_login(page, _cfg(), log=lambda *_: None)
    except login.LoginError as exc:
        assert "secret" not in str(exc)


def test_is_auth_error_matches_transient_and_permanent():
    assert publish.is_auth_error(publish.PublishError("redirected to login wall"))
    assert publish.is_auth_error(publish.PublishError("tiktok session invalid"))
    assert not publish.is_auth_error(publish.PublishError("caption editor unreachable"))


def test_heal_session_requires_credentials(monkeypatch):
    called = []
    monkeypatch.setattr(login, "login", lambda *a, **k: called.append(1) or True)
    assert publish.heal_session(_cfg(tiktok_email="", tiktok_password=""), log=lambda *_: None) is False
    assert called == []


def test_heal_session_runs_login_and_reports_truthfully(monkeypatch):
    monkeypatch.setattr(login, "login", lambda *a, **k: True)
    assert publish.heal_session(_cfg(), log=lambda *_: None) is True
    monkeypatch.setattr(login, "login", lambda *a, **k: False)
    assert publish.heal_session(_cfg(), log=lambda *_: None) is False


def test_heal_session_swallows_login_errors(monkeypatch):
    def boom(*a, **k):
        raise login.LoginError("captcha after submit")

    monkeypatch.setattr(login, "login", boom)
    assert publish.heal_session(_cfg(), log=lambda *_: None) is False


def test_session_check_relogs_once_when_wall_hit(monkeypatch):
    calls = {"check": 0, "heal": 0}

    def fake_check(cfg):
        calls["check"] += 1
        if calls["check"] == 1:
            raise publish.PublishError("tiktok session invalid (redirected to login wall)")
        return {"ok": True, "posts": 5}

    def fake_heal(cfg, log=print):
        calls["heal"] += 1
        return True

    monkeypatch.setattr(publish, "_session_check_once", fake_check)
    monkeypatch.setattr(publish, "heal_session", fake_heal)
    assert publish.session_check(_cfg())["posts"] == 5
    assert calls == {"check": 2, "heal": 1}


def test_session_check_does_not_heal_on_other_errors(monkeypatch):
    def fake_check(cfg):
        raise publish.PublishError("cannot read content page baseline")

    monkeypatch.setattr(publish, "_session_check_once", fake_check)
    with pytest.raises(publish.PublishError):
        publish.session_check(_cfg())

class QrPage:
    def __init__(self, has_qr=True, identity_before="", identity_after=None):
        self.has_qr = has_qr
        self.identity_before = identity_before
        self.identity_after = identity_after
        self.shots = []
        self.polls = 0
        self.gone = {}

    def has(self, selector):
        return self.has_qr and ("canvas" in selector or "qrcode" in selector)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms):
        self.polls += 1

    def inner_text(self, selector=None):
        return "Log in" if self.has_qr else "Drag the slider to fit the puzzle"

    def evaluate(self, js, arg=None):
        who = self.identity_before
        if self.identity_after and self.polls >= 2:
            who = self.identity_after
        return {"data": {"username": who}} if who else {"data": {}}

    def screenshot(self, path=None):
        self.shots.append(path)


def test_qr_login_saves_code_and_verifies(tmp_path):
    page = QrPage(identity_after="shadowmorarch")
    out = tmp_path / "qr.png"
    assert login.qr_flow(page, _cfg(), log=lambda *_: None, timeout_s=3, shot=str(out)) is True
    assert page.shots and page.shots[0] == str(out)


def test_qr_login_reports_captcha_instead_of_qr(tmp_path):
    page = QrPage(has_qr=False)
    with pytest.raises(login.LoginError) as exc:
        login.qr_flow(page, _cfg(), log=lambda *_: None, timeout_s=3, shot=str(tmp_path / "q.png"))
    assert "captcha" in str(exc.value)


def test_qr_login_returns_true_when_already_signed_in(tmp_path):
    page = QrPage(identity_before="shadowmorarch", has_qr=False)
    assert login.qr_flow(page, _cfg(), log=lambda *_: None, timeout_s=3, shot=str(tmp_path / "q.png")) is True
    assert page.shots == []
