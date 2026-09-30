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

1. Log into tiktok.com in your own browser.
2. Copy the `sessionid` cookie value (and `sid_guard` if present).
3. On the VPS: `mkdir -p cookies && printf 'sessionid=XXXX' > cookies/tiktok.txt && chmod 600 cookies/tiktok.txt`

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

`POSTS_PER_DAY` is the real cap: extra publish runs find the budget spent
and exit 0. A failed 08:00 row is not retried (re-staging requires an
explicit approve); the 13:30 run posts the next approved row instead —
the failure consumed no budget.

## Acceptance gate

Run `STAGE_ONLY=true` for two weeks. Only after clean staged→posted
history set `STAGE_ONLY=false` (or keep passing `--auto`).

## Known limits

- Region param biases, does not filter — filtering happens client-side.
- tikwm can answer HTTP 531; one call per day, backoff, alert if 0 rows twice.
- Reposting can trip TikTok's unoriginal-content policy. Use a dedicated
  account; the ffmpeg overlay reduces but does not remove that risk.
