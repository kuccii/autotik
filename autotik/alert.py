import json
from datetime import datetime, timezone

import httpx

LEVELS = ("info", "warn", "error")


def send(webhook_url, level, message, client=None):
    if not webhook_url:
        return False
    if level not in LEVELS:
        level = "error"
    payload = {
        "level": level,
        "message": message,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "autotik",
    }
    own = client is None
    if own:
        client = httpx.Client(timeout=10.0)
    try:
        resp = client.post(
            webhook_url,
            content=json.dumps(payload, separators=(",", ":")),
            headers={"Content-Type": "application/json"},
        )
        return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        return False
    finally:
        if own:
            client.close()
