from pathlib import Path

from autotik import session


def test_profile_dir_honors_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert session.profile_dir() == tmp_path / "tiktok_profile"


def test_profile_dir_defaults_under_data(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert session.profile_dir() == Path("data") / "tiktok_profile"


def _cookie_file(tmp_path, body='[{"name":"sessionid","value":"X"}]'):
    p = tmp_path / "cookies.json"
    p.write_text(body, encoding="utf-8")
    return p


def test_needs_import_when_profile_missing(tmp_path):
    cookie = _cookie_file(tmp_path)
    assert session.needs_cookie_import(tmp_path / "tiktok_profile", cookie) is True


def test_needs_import_false_after_mark_true_after_reexport(tmp_path):
    cookie = _cookie_file(tmp_path)
    pdir = tmp_path / "tiktok_profile"
    pdir.mkdir()
    assert session.needs_cookie_import(pdir, cookie) is True  # profile, no marker
    session.mark_cookie_import(pdir, cookie)
    assert session.needs_cookie_import(pdir, cookie) is False
    cookie.write_text('[{"name":"sessionid","value":"Y"}]', encoding="utf-8")
    assert session.needs_cookie_import(pdir, cookie) is True


def test_cookie_sha_changes_with_content(tmp_path):
    a = _cookie_file(tmp_path, '[{"value":"1"}]')
    sha1 = session.cookie_sha(a)
    a.write_text('[{"value":"2"}]', encoding="utf-8")
    assert session.cookie_sha(a) != sha1


def test_normalize_cookies_maps_cookie_editor_export():
    raw = [
        {
            "name": "sessionid", "value": "0d35", "domain": ".tiktok.com",
            "path": "/", "sameSite": None, "secure": True, "httpOnly": True,
            "expirationDate": 1807344556.156751, "storeId": None,
            "hostOnly": False, "session": False,
        },
        {
            "name": "s_v_web_id", "value": "verify_x", "domain": ".tiktok.com",
            "path": "/", "sameSite": "no_restriction", "secure": True,
            "session": True,
        },
        {
            "name": "tt_csrf_token", "value": "Kx4", "domain": ".tiktok.com",
            "path": "/", "sameSite": "lax", "expires": None,
        },
        {"name": "", "value": "x"},
        {"value": "no-name"},
        "junk",
    ]
    out = session.normalize_cookies(raw)
    assert [c["name"] for c in out] == ["sessionid", "s_v_web_id", "tt_csrf_token"]
    a = out[0]
    assert "sameSite" not in a
    assert a["expires"] == 1807344556.156751
    assert "storeId" not in a and "hostOnly" not in a and "session" not in a
    assert out[1]["sameSite"] == "None" and "expires" not in out[1]
    assert out[2]["sameSite"] == "Lax" and "expires" not in out[2]
    minimal = session.normalize_cookies([{"name": "d", "value": "4"}])[0]
    assert minimal["domain"] == ".tiktok.com" and minimal["path"] == "/"
    assert session.normalize_cookies({"not": "a list"}) == []


def test_user_agent_is_windows_chrome_without_headless():
    ua = session.USER_AGENT
    assert "HeadlessChrome" not in ua
    assert "Chrome/145.0.7632.6" in ua
    assert "Windows NT 10.0; Win64; x64" in ua
    assert "HeadlessChrome" not in session.SEC_CH_UA
    assert '"Google Chrome";v="145"' in session.SEC_CH_UA
    assert '"Chromium";v="145"' in session.SEC_CH_UA


def test_ua_metadata_claims_windows_not_linux():
    meta = session.UA_METADATA
    assert meta["platform"] == "Windows"
    assert meta["platformVersion"] == "10.0.0"
    assert meta["architecture"] == "x86" and meta["bitness"] == "64"
    assert meta["mobile"] is False
    assert meta["formFactors"] == ["Desktop"]
    for key in ("brands", "fullVersionList"):
        assert all(
            "Headless" not in b["brand"] for b in meta[key]
        ), meta[key]


def test_fingerprint_js_contains_probe_fixes():
    js = session.FINGERPRINT_JS
    assert '"Win32"' in js
    assert "hardwareConcurrency" in js
    assert '"PDF Viewer"' in js and '"WebKit built-in PDF"' in js
    assert "application/pdf" in js
    assert "0x9245" in js and "0x9246" in js
    assert "Europe/Paris" not in js  # tz is a context option, not JS
    assert "__GL_VENDOR__" not in js and "__GL_RENDERER__" not in js
    assert session.GL_VENDOR in js
    assert session.GL_RENDERER in js
    assert 'pdfViewerEnabled' in js and '() => true' in js
    assert 'window.chrome.runtime' in js
    assert 'sendMessage' in js


def test_context_options_claim_paris_timezone_and_locale():
    import inspect

    src = inspect.getsource(session.open_context)
    assert 'timezone_id="Europe/Paris"' in src
    assert 'locale="en-US"' in src
    assert "launch_persistent_context" in src
    assert "--disable-blink-features=AutomationControlled" in src


class FakeCdp:
    def __init__(self, fail_first=False):
        self.sent = []
        self.attempts = 0
        self.fail_first = fail_first

    def send(self, method, payload):
        self.attempts += 1
        if self.fail_first and self.attempts == 1:
            raise RuntimeError("formFactors unsupported")
        self.sent.append((method, payload))


class FakeCtx:
    def __init__(self, fail_first=False):
        self.cdp = FakeCdp(fail_first=fail_first)

    def new_cdp_session(self, page):
        return self.cdp


def test_apply_client_hints_sends_ua_and_windows_metadata():
    ctx, page = FakeCtx(), object()
    session._CDP_SESSIONS.clear()
    session._apply_client_hints(ctx, page)
    method, payload = ctx.cdp.sent[0]
    assert method == "Emulation.setUserAgentOverride"
    assert payload["userAgent"] == session.USER_AGENT
    meta = payload["userAgentMetadata"]
    assert meta["platform"] == "Windows"
    assert meta["formFactors"] == ["Desktop"]


def test_apply_client_hints_fallback_drops_form_factors():
    ctx, page = FakeCtx(fail_first=True), object()
    session._CDP_SESSIONS.clear()
    session._apply_client_hints(ctx, page)
    _method, payload = ctx.cdp.sent[0]
    assert "formFactors" not in payload["userAgentMetadata"]
    assert payload["userAgentMetadata"]["platform"] == "Windows"
