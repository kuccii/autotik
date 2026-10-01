# autotik

Trending-TikTok discovery → ffmpeg overlay → staged approval → daily upload.

Read `../docs/superpowers/specs/2026-09-30-autotik-design.md` first.

## Setup

```bash
cp .env.example .env      # then edit
python -m pytest          # unit tests, no network
set -a; . ./.env; set +a
python -m autotik.cli status
```

Under `docker compose run` the env is injected automatically via `env_file`;
the `set -a; . ./.env; set +a` line is only needed for bare-metal shells.

## Cookie export (required before `publish --auto`)

tiktokautouploader 6.1 reads `TK_cookies_<account>.json` (JSON array, its own
format) from the process CWD — autotik stages it from `COOKIE_DIR` before
every upload and fails closed with `cookie file not found: ... — see README`
when it is absent, so the library never falls into its interactive login.

1. Log into tiktok.com in your own browser.
2. Export all tiktok.com cookies as a JSON array; each entry needs `name`,
   `value` and `sameSite` (`Strict`, `Lax` or `None` — `Lax` is assumed when
   absent), plus `expires` **or** `expirationDate` (unix seconds) so expiry
   can be checked.
3. The session must include at least one of `sessionid`, `sid_tt`,
   `sessionid_ss`, `passport_auth_status` with a **future** expiry — with
   none of them the library treats the file as expired, deletes it and drops
   into an interactive browser login that hangs under Docker.

   Placeholder shape only (real values required):

   ```json
   [
     {"name": "sessionid", "value": "XXXX", "sameSite": "Lax", "expires": 4102444800}
   ]
   ```

4. On the VPS:

   ```bash
   mkdir -p cookies && cp ~/TK_cookies.json cookies/TK_cookies_shadowmorarch.json && chmod 600 cookies/TK_cookies_*.json
   ```

   The filename must match the account: `TK_cookies_<handle-without-@>.json`.

Recovery on expiry: the alert webhook fires, `publish` exits 1, nothing is
retried silently. Re-export the cookie and re-run `publish --auto`.

## Docker (VPS)

```bash
cd /root/autotik && docker compose build
docker compose run --rm autotik status
docker compose run --rm autotik discover
```

## Cron (Africa/Kigali)

```cron
0 5 * * *  docker compose -f /root/autotik/docker-compose.yml run --rm autotik discover  >> /var/log/autotik.log 2>&1
0 8 * * *  docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
30 13 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish  >> /var/log/autotik.log 2>&1
30 19 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish  >> /var/log/autotik.log 2>&1
```

Container `TZ` does not affect host cron: set the host clock with
`timedatectl set-timezone Africa/Kigali` or the schedule fires 2h late.

`POSTS_PER_DAY` is the real cap and publish counts today's slots
(`PUBLISH_SLOTS`, Africa/Kigali): a `publish --auto` run before the first
slot exits 0 with `not_due` (no alert), a run after the budget is spent
exits 0 with `budget spent`, and only a due run with budget left and
nothing approved exits 1 and alerts. A failed 08:00 row is not retried —
failed rows are terminal (there is no CLI path from `failed` back to
`staged`); the 13:30 run posts the next approved row instead — the
failure consumed no budget.

## Acceptance gate

Run `STAGE_ONLY=true` for two weeks. Only after clean staged→posted
history set `STAGE_ONLY=false` (or keep passing `--auto`).

## Known limits

- Region param biases, does not filter — filtering happens client-side.
- tikwm can answer HTTP 531; one call per day, backoff, alert if 0 rows twice.
- Reposting can trip TikTok's unoriginal-content policy. Use a dedicated
  account; the ffmpeg overlay reduces but does not remove that risk.


## Verified (2026-09-30)

Live end-to-end smoke on the VPS (`root@72.60.188.94`, Docker 29.5.2 /
Compose v5.1.4, image built from `mcr.microsoft.com/playwright/python:v1.61.0-noble`),
`REGION=RW`, `TIKTOK_ACCOUNT=@shadowmorarch`, `STAGE_ONLY=true`.

| Step | Command | Result |
|------|---------|--------|
| 1. Unit suite | `python -m pytest -v` (local) | **63 passed, 1 skipped** (skipped: ffmpeg not installed locally) |
| 2. Discover x2 | `docker compose run --rm autotik discover` (twice) | Run 1: `staged 4` exit 0. Run 2: `staged 4` exit 0 -- the tikwm feed rotates, so run 2 returned 4 *new* ids; DB holds **8 distinct video_id, 0 duplicates, 0 `duplicate_hash` events**. Deterministic feed-replay probe with an already-known id: `no new rows` -> **exit 1** -- dedup proven; zero-row exit-1 path exercised (its alert send returned False -- see step 7). One `transform_failed` row logged: `output too small (683237 < 1000000)` (min-size guard). |
| 3. Transform (real ffmpeg) | ffprobe of every `*_tf.mp4` vs its source | 9 downloaded / 8 transformed; **all portrait 576x1024 (one 576x1022)**, duration delta vs source **0.00 s** on all 8 -> `ALL_OK` |
| 4. Caption (OpenRouter) | `rewrite_caption(...)` | **Fell back to the original text, no traceback** (contract holds). Live rewrite not reachable: the key on the VPS is an OpenCode Zen key (`OPENROUTER_BASE_URL=https://opencode.ai/zen/v1` in sibling `.env`s) and `openrouter.ai` answers **HTTP 401 "Missing Authentication header"**. The rewrite branch is covered by unit tests; put a real `sk-or-v1-...` key in `.env` to light it up. Sample input, returned unchanged by the fallback: `Funny pig comedy from Rwanda`. |
| 5. Stage-only gate | `approve --all` then `publish` | `approved 8`; `{'posted': 0, 'failed': 0, 'skipped': 8}` **exit 0** -- guard held live (`status`: approved=8, staged=0) |
| 6. Real upload | `publish --auto` | **DEFERRED -- cookie pending; stage-only gate active** (not attempted) |
| 7. Alert path | `alert.send(webhook, "error", ...)` | **`False`** -- `POST https://n8n.qivook.com/webhook/autotik-alert` answers **404** `The requested webhook "POST autotik-alert" is not registered` (workflow not activated for the production URL). Alerts are currently dropped; activate the n8n workflow to close this. |

**Go/no-go: the pipeline is NOT ready for cron or `publish --auto` until all three open items are closed -- a real `sk-or-v1-...` OpenRouter key in `.env`, the n8n `autotik-alert` workflow activated, and the Step 6 session cookie exported.** Unconfirmed-upload failures may still have posted -- visually check the TikTok account before re-approving any failed row.

Dockerfile confirmation (Task 9 open item): `RUN phantomwright_driver install chromium`
**works as written** -- `phantomwright-driver 1.58.3` ships with `tiktokautouploader==6.1`,
the build step exited 0 and pulled Chrome for Testing 145.0.7632.6. No change needed.
