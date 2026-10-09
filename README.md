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
0 5 * * *  (docker compose -f /root/autotik/docker-compose.yml run --rm autotik discover; docker compose -f /root/autotik/docker-compose.yml run --rm autotik approve --all)  >> /var/log/autotik.log 2>&1
5 15 * * *  (docker compose -f /root/autotik/docker-compose.yml run --rm autotik discover; docker compose -f /root/autotik/docker-compose.yml run --rm autotik approve --all)  >> /var/log/autotik.log 2>&1
0 8 * * *  docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
0 11 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
0 14 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
0 17 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
0 20 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
0 22 * * * docker compose -f /root/autotik/docker-compose.yml run --rm autotik publish   >> /var/log/autotik.log 2>&1
```

Container `TZ` does not affect host cron: set the host clock with
`timedatectl set-timezone Africa/Kigali` or the schedule fires 2h late.

`POSTS_PER_DAY` (currently 6, `MAX_POSTS_PER_DAY=6` is the hard
ceiling) is the real cap and publish counts today's slots
(`PUBLISH_SLOTS=08:00,11:00,14:00,17:00,20:00,22:00`,
Africa/Kigali): a
`publish --auto` run before the first slot exits 0 with `not_due`
(no alert), a run after the budget is spent exits 0 with `budget
spent`, and only a due run with budget left and nothing approved
exits 1 and alerts. Each run posts at most one video per elapsed
slot, so a missed slot stays missed instead of triggering a
back-to-back burst (TikTok flags upload bursts). A failed 08:00 row
is not retried — failed rows are terminal (there is no CLI path from
`failed` back to `staged`); the next slot's run posts the next
approved row instead — the failure consumed no budget.

## Acceptance gate

Run `STAGE_ONLY=true` for two weeks. Only after clean staged→posted
history set `STAGE_ONLY=false` (or keep passing `--auto`).

## Known limits

- Region param biases ranking per feed call; discovery merges every
  feed in `DISCOVER_REGIONS` (one call each, 1 req/s) and keeps items
  from all countries — no client-side country filter.
- tikwm can answer HTTP 531; one call per region per run (twice daily), backoff, alert if 0 rows twice.
- Reposting can trip TikTok's unoriginal-content policy. Use a dedicated
  account; the ffmpeg overlay reduces but does not remove that risk.


## Verified (2026-09-30)

Live end-to-end smoke on the VPS (`root@72.60.188.94`, Docker 29.5.2 /
Compose v5.1.4, image built from `mcr.microsoft.com/playwright/python:v1.61.0-noble`),
`REGION=RW`, `TIKTOK_ACCOUNT=@shadowmorarch`, `STAGE_ONLY=true`.

| Step | Command | Result |
|------|---------|--------|
| 1. Unit suite | `python -m pytest -v` (local) | **106 passed, 1 skipped, 1 deselected** (skipped: ffmpeg not installed locally; deselected: opt-in network test) |
| 2. Discover x2 | `docker compose run --rm autotik discover` (twice) | Run 1: `staged 4` exit 0. Run 2: `staged 4` exit 0 -- the tikwm feed rotates, so run 2 returned 4 *new* ids; DB holds **8 distinct video_id, 0 duplicates, 0 `duplicate_hash` events**. Deterministic feed-replay probe with an already-known id: `no new rows` -> **exit 1** -- dedup proven; zero-row exit-1 path exercised (its alert send returned False -- see step 7). One `transform_failed` row logged: `output too small (683237 < 1000000)` (min-size guard). |
| 3. Transform (real ffmpeg) | ffprobe of every `*_tf.mp4` vs its source | 9 downloaded / 8 transformed; **all portrait 576x1024 (one 576x1022)**, duration delta vs source **0.00 s** on all 8 -> `ALL_OK` |
| 4. Caption (OpenRouter) | `rewrite_caption(...)` | **Live and verified 2026-10-05** -- real `sk-or-v1-...` key in `.env`, model `poolside/laguna-s-2.1:free`, 2/2 clean rewrites through the real code path (`Discovering Kigali's tastiest street eats...`). Free-tier upstream pools 429 intermittently; the function falls back to the original caption silently (by design). Free quota: 50 req/day (production needs 1-3). |
| 5. Stage-only gate | `approve --all` then `publish` | `approved 8`; `{'posted': 0, 'failed': 0, 'skipped': 8}` **exit 0** -- guard held live (`status`: approved=8, staged=0) |
| 6. Real upload | `publish --auto` | **LIVE and verified 2026-10-08** -- first real post: video `7694249238062026006` (row `7670879147006250261`), publicly viewable logged-out. Uploader is now in-repo (`publish._run_tiktok_upload`): dismisses TikTok's cookie-consent banner + tutorial popups, scrolls the Post button into view (click ladder normal -> force -> js), and confirms success by Posts-count/video-id diff on `/tiktokstudio/content` (the upstream library's 2s confirm cannot see the banner or below-fold button). On failure it leaves `/app/data/upload_state.png` and raises `PublishError` with before/after counts. |
| 7. Alert path | `alert.send(webhook, "error", ...)` | **Live and verified 2026-10-07** -- n8n workflow `Send autotik alert message` (id `c6BLObhrtMaf7mv0`) active; `POST https://n8n.qivook.com/webhook/autotik-alert` returns 200 and execution 3488 completed `success` with WhatsApp delivered to +250782718957 via WABA A. Channel is WhatsApp, not Gmail: every Google OAuth refresh token on the instance returns `invalid_grant` (re-auth needed in the n8n UI if Gmail is wanted). Since 2026-10-08 the path is **template-primary** (`autotik_alert_nat_v1`, fallback `autotik_alert_plain_v1`) -- free-form text 470s when the 24h service window is closed; templates bypass the window (delivery confirmed, exec 3541). |

**Go/no-go: LIVE.** All blockers closed -- caption (2026-10-05), cookie (2026-10-05), alert path (2026-10-07, template-primary 2026-10-08), stage gate flipped 2026-10-08 (`STAGE_ONLY=false`), first real post confirmed 2026-10-08. Steady state: 1 post/day at the first elapsed slot, `budget_spent` exits 0 silently after the daily cap. A failed upload row was NOT posted if it says `upload not confirmed` with `posts N->N` (the verifier requires a Posts-count increase or a new video id); still worth a glance at `/app/data/upload_state.png` (last failure screenshot) when triaging.

Dockerfile confirmation (Task 9 open item): `RUN phantomwright_driver install chromium`
**works as written** -- `phantomwright-driver 1.58.3` ships with `tiktokautouploader==6.1`,
the build step exited 0 and pulled Chrome for Testing 145.0.7632.6. No change needed.
