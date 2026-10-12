"""Persistent TikTok browser session with a coherent spoofed fingerprint.

Ground truth for the fingerprint came from the operator's real Chrome on
Windows (captured 2026-10-11, Chrome 154/Playwright-channel + raw
--dump-dom, both agreeing). We claim engine-true Chrome 145 (the bundled
chromium-1208 build) instead of 154: UA/feature consistency beats a newer
version string the engine cannot back up. Key fixes vs the old path:
HeadlessChrome leaked into sec-ch-ua, platform said Linux under a Windows
UA, plugins=0, WebGL=SwiftShader, tz America/New_York vs a Paris IP, and a
fresh context (no storage) on every run.
"""

import hashlib
import json
import os
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.7632.6 Safari/537.36"
)

# sec-ch-ua as Chromium itself builds it (grease brand included), minus the
# HeadlessChrome brand the default header advertised. Same brands array is
# fed to CDP so JS navigator.userAgentData and the wire header agree.
SEC_CH_UA = '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'

UA_METADATA = {
    "brands": [
        {"brand": "Chromium", "version": "145"},
        {"brand": "Google Chrome", "version": "145"},
        {"brand": "Not:A-Brand", "version": "99"},
    ],
    "fullVersionList": [
        {"brand": "Chromium", "version": "145.0.7632.6"},
        {"brand": "Google Chrome", "version": "145.0.7632.6"},
        {"brand": "Not:A-Brand", "version": "99.0.0.0"},
    ],
    "platform": "Windows",
    "platformVersion": "10.0.0",
    "architecture": "x86",
    "bitness": "64",
    "model": "",
    "mobile": False,
    "wow64": False,
    "formFactors": ["Desktop"],
}

EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# Real-machine WebGL strings captured verbatim (ANGLE on AMD Radeon).
GL_VENDOR = "Google Inc. (AMD)"
GL_RENDERER = (
    "ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001638) "
    "Direct3D11 vs_5_0 ps_5_0, D3D11)"
)

FINGERPRINT_JS = """
(() => {
  const define = (obj, prop, get) => {
    try {
      Object.defineProperty(obj, prop, { get, configurable: true, enumerable: true });
    } catch (e) {}
  };
  define(Navigator.prototype, "platform", () => "Win32");
  define(Navigator.prototype, "hardwareConcurrency", () => 8);
  if ("deviceMemory" in Navigator.prototype) {
    define(Navigator.prototype, "deviceMemory", () => 8);
  }
  const LANGS = Object.freeze(["en-US", "en"]);
  define(Navigator.prototype, "languages", () => LANGS);
  define(Navigator.prototype, "language", () => "en-US");
  define(Navigator.prototype, "vendor", () => "Google Inc.");

  const pdfMimes = [
    { type: "application/pdf", suffixes: "pdf", description: "" },
    { type: "text/pdf", suffixes: "pdf", description: "" },
  ];
  const pluginDefs = [
    ["PDF Viewer", "internal-pdf-viewer"],
    ["Chrome PDF Viewer", "chrome-pdf-viewer"],
    ["Chromium PDF Viewer", "chromium-pdf-viewer"],
    ["Microsoft Edge PDF Viewer", "edge-pdf-viewer"],
    ["WebKit built-in PDF", "webkit-built-in-pdf"],
  ];
  const plugins = pluginDefs.map(([name, filename]) => {
    const p = { name, filename, description: "", length: 2 };
    pdfMimes.forEach((m, i) => {
      p[i] = { type: m.type, suffixes: m.suffixes, description: "", enabledPlugin: p };
    });
    return p;
  });
  const mimeEntries = pdfMimes.map((m) => ({
    type: m.type, suffixes: m.suffixes, description: "", enabledPlugin: plugins[0],
  }));
  const collection = (values, keyOf) => {
    const arr = values.slice();
    arr.item = (i) => (i >= 0 && i < values.length ? values[i] : null);
    arr.namedItem = (k) => values.find((v) => keyOf(v) === k) || null;
    arr.refresh = () => {};
    values.forEach((v) => { const k = keyOf(v); if (k) arr[k] = v; });
    return arr;
  };
  define(Navigator.prototype, "plugins", () => collection(plugins, (p) => p.name));
  define(Navigator.prototype, "mimeTypes", () => collection(mimeEntries, (m) => m.type));

  const GLV = __GL_VENDOR__;
  const GLR = __GL_RENDERER__;
  const patchGL = (proto) => {
    if (!proto) return;
    const origGet = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 0x9245) return GLV;
      if (p === 0x9246) return GLR;
      if (p === 0x1f00) return "WebKit";
      if (p === 0x1f01) return "WebKit WebGL";
      return origGet.call(this, p);
    };
    const origExt = proto.getExtension;
    proto.getExtension = function (name) {
      const r = origExt.call(this, name);
      if (name === "WEBGL_debug_renderer_info" && !r) {
        return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
      }
      return r;
    };
  };
  if (typeof WebGLRenderingContext !== "undefined") patchGL(WebGLRenderingContext.prototype);
  if (typeof WebGL2RenderingContext !== "undefined") patchGL(WebGL2RenderingContext.prototype);

  // headless shell reports Notification.permission "denied"; real Chrome "default"
  try {
    if (typeof Notification !== "undefined") {
      Object.defineProperty(Notification, "permission", {
        get: () => "default", configurable: true,
      });
    }
  } catch (e) {}

  // screen must be >= viewport (old path had 1280x720 screen under a 900px-tall
  // viewport); outer = viewport + window chrome, matching real Windows Chrome
  define(Screen.prototype, "width", () => 1920);
  define(Screen.prototype, "height", () => 1080);
  define(Screen.prototype, "availWidth", () => 1920);
  define(Screen.prototype, "availHeight", () => 1040);
  define(window, "outerWidth", () => window.innerWidth + 16);
  define(window, "outerHeight", () => window.innerHeight + 88);

  // ground truth from real Chrome 154: pdfViewerEnabled true
  define(Navigator.prototype, "pdfViewerEnabled", () => true);

  if (typeof window.chrome === "undefined") window.chrome = {};
  // headless shell chrome object lacks runtime; every real Chrome page has it
  if (typeof window.chrome.runtime === "undefined") {
    window.chrome.runtime = {
      connect: function connect() {},
      sendMessage: function sendMessage() {},
      id: undefined,
    };
  }
})();
"""

FINGERPRINT_JS = (
    FINGERPRINT_JS
    .replace("__GL_VENDOR__", json.dumps(GL_VENDOR))
    .replace("__GL_RENDERER__", json.dumps(GL_RENDERER))
)


def profile_dir():
    """Persistent profile under DATA_DIR (volume-mounted: /app/data)."""
    return Path(os.environ.get("DATA_DIR") or "data") / "tiktok_profile"


def _marker_path(pdir):
    return Path(str(pdir) + ".cookie_sha")


def cookie_sha(cookie_path):
    return hashlib.sha256(Path(cookie_path).read_bytes()).hexdigest()


def needs_cookie_import(pdir, cookie_path):
    """Import the cookie export only when the profile is fresh or the export
    content changed (a re-export after TikTok invalidates the session).
    Otherwise the profile's own refreshed cookies win -- that is the whole
    point of persisting storage across runs."""
    pdir = Path(pdir)
    marker = _marker_path(pdir)
    if not pdir.exists() or not marker.exists():
        return True
    try:
        return marker.read_text(encoding="utf-8").strip() != cookie_sha(cookie_path)
    except OSError:
        return True


def mark_cookie_import(pdir, cookie_path):
    marker = _marker_path(Path(pdir))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(cookie_sha(cookie_path), encoding="utf-8")


# keep CDP session objects alive for the process lifetime (browser-side the
# override persists regardless, but never rely on GC behaviour)
_CDP_SESSIONS = []


def _apply_client_hints(ctx, page):
    cdp = ctx.new_cdp_session(page)
    payload = {"userAgent": USER_AGENT, "userAgentMetadata": dict(UA_METADATA)}
    try:
        cdp.send("Emulation.setUserAgentOverride", payload)
    except Exception:
        meta = {k: v for k, v in UA_METADATA.items() if k != "formFactors"}
        payload = {"userAgent": USER_AGENT, "userAgentMetadata": meta}
        cdp.send("Emulation.setUserAgentOverride", payload)
    _CDP_SESSIONS.append(cdp)


def open_context(p, cookie_path, headless=True):
    """Open the persistent profile and return (context, page).

    Cookie import is bootstrapped/refreshed by sha marker; client hints are
    forced through CDP so sec-ch-ua no longer advertises HeadlessChrome."""
    pdir = profile_dir()
    pdir.parent.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        headless=headless,
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="Europe/Paris",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
        ],
    )
    try:
        ctx = p.chromium.launch_persistent_context(
            str(pdir), extra_http_headers=dict(EXTRA_HEADERS), **kwargs
        )
    except Exception:
        ctx = p.chromium.launch_persistent_context(str(pdir), **kwargs)
    try:
        ctx.add_init_script(FINGERPRINT_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _apply_client_hints(ctx, page)
        if needs_cookie_import(pdir, cookie_path):
            cookies = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
            for c in cookies:
                if not c.get("url") and not (c.get("domain") and c.get("path")):
                    c.setdefault("domain", ".tiktok.com")
                    c.setdefault("path", "/")
            ctx.add_cookies(cookies)
            mark_cookie_import(pdir, cookie_path)
    except Exception:
        ctx.close()
        raise
    return ctx, page
