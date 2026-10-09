import os
import re
from dataclasses import dataclass

REQUIRED = ("REGION", "TIKTOK_ACCOUNT", "OPENROUTER_API_KEY")
ACCOUNT_RE = re.compile(r"^@?[A-Za-z0-9_.]{1,30}$")
SLOT_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
REGION_CODE_RE = re.compile(r"^[A-Z]{2}$")
TRUTHY = ("1", "true", "yes", "on")
FALSY = ("0", "false", "no", "off")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    region: str
    account: str
    openrouter_api_key: str
    openrouter_model: str
    n8n_webhook_url: str
    data_dir: str
    cookie_dir: str
    discover_fetch: int
    discover_keep: int
    discover_regions: tuple[str, ...]
    posts_per_day: int
    max_posts_per_day: int
    publish_slots: tuple[str, ...]
    stage_only: bool
    overlay_enabled: bool
    overlay_account: str
    overlay_style: str


def _flag(raw, default):
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    raise ConfigError(f"unrecognized boolean value: {raw!r}")


def _int(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise ConfigError("missing required env vars: " + ", ".join(missing))

    overlay_account = env.get("OVERLAY_ACCOUNT") or env.get("TIKTOK_ACCOUNT")
    if not ACCOUNT_RE.match(overlay_account):
        raise ConfigError(
            f"OVERLAY_ACCOUNT {overlay_account!r} must match {ACCOUNT_RE.pattern}"
        )

    slots = tuple(
        s.strip() for s in env.get("PUBLISH_SLOTS", "08:00,13:30,19:30").split(",")
        if s.strip()
    )
    if not slots:
        raise ConfigError("PUBLISH_SLOTS must contain at least one HH:MM slot")
    for slot in slots:
        if not SLOT_RE.match(slot):
            raise ConfigError(
                f"PUBLISH_SLOTS entry {slot!r} must match {SLOT_RE.pattern}"
            )

    max_posts = max(1, _int(env.get("MAX_POSTS_PER_DAY"), 3))
    posts = min(max(1, _int(env.get("POSTS_PER_DAY"), 1)), max_posts)

    region = env.get("REGION", "RW").strip().upper()
    regions = tuple(dict.fromkeys(
        r.strip().upper()
        for r in env.get("DISCOVER_REGIONS", "").split(",")
        if r.strip()
    ))
    for code in regions:
        if not REGION_CODE_RE.match(code):
            raise ConfigError(
                f"DISCOVER_REGIONS entry {code!r} must be a 2-letter ISO country code"
            )

    return Config(
        region=region,
        discover_regions=regions or (region,),
        account=env["TIKTOK_ACCOUNT"].strip(),
        openrouter_api_key=env["OPENROUTER_API_KEY"].strip(),
        openrouter_model=env.get("OPENROUTER_MODEL", "openrouter/free").strip(),
        n8n_webhook_url=env.get("N8N_WEBHOOK_URL", "").strip(),
        data_dir=env.get("DATA_DIR", "data"),
        cookie_dir=env.get("COOKIE_DIR", "cookies"),
        discover_fetch=max(1, _int(env.get("DISCOVER_FETCH"), 20)),
        discover_keep=max(1, _int(env.get("DISCOVER_KEEP"), 6)),
        posts_per_day=posts,
        max_posts_per_day=max_posts,
        publish_slots=slots,
        stage_only=_flag(env.get("STAGE_ONLY"), True),
        overlay_enabled=_flag(env.get("OVERLAY_ENABLED"), True),
        overlay_account=overlay_account,
        overlay_style=env.get("OVERLAY_STYLE", "bar").strip().lower(),
    )
