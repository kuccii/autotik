import argparse
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from autotik import alert, db, discover, download, publish, transform
from autotik.config import ConfigError, load_config

TZ = ZoneInfo("Africa/Kigali")


def db_path(cfg):
    return Path(cfg.data_dir) / "autotik.sqlite"


def videos_dir(cfg):
    return Path(cfg.data_dir) / "videos"


def run_discover(cfg, conn, limit=None):
    fetch = cfg.discover_fetch
    keep = limit or cfg.discover_keep
    import httpx

    with httpx.Client(follow_redirects=True) as client:
        feed = discover.fetch_feed(client, cfg.region, fetch)
        items = discover.rank(discover.parse_items(feed, cfg.region), keep)
        if not items:
            return 0
        staged = 0
        cur = conn.cursor()
        for item in items:
            if db.get(cur, item["video_id"]) is not None:
                continue
            try:
                path, digest = download.download(item, videos_dir(cfg), client)
            except download.DownloadError as exc:
                alert.send(cfg.n8n_webhook_url, "warn", str(exc))
                db.log(cur, item["video_id"], "download_failed", str(exc)[:300])
                conn.commit()
                continue
            try:
                out = transform.transform(
                    path, path.with_name(f"{item['video_id']}_tf.mp4"),
                    cfg.overlay_account, cfg.overlay_enabled, cfg.overlay_style,
                )
            except transform.TransformError as exc:
                alert.send(cfg.n8n_webhook_url, "warn", str(exc))
                db.log(cur, item["video_id"], "transform_failed", str(exc)[:300])
                conn.commit()
                continue
            db.insert_candidates(
                cur,
                [
                    dict(
                        item,
                        desc_orig=item["desc"],
                        file_path=str(out),
                        file_hash=digest,
                    )
                ],
            )
            db.log(cur, item["video_id"], "staged", str(out))
            staged += 1
            conn.commit()
        return staged


def run_publish(cfg, conn, auto):
    import httpx

    with httpx.Client() as client:
        return publish.publish_batch(cfg, conn, auto=auto, client=client)


def cmd_discover(cfg, conn, args):
    staged = run_discover(cfg, conn, args.limit)
    if staged == 0:
        alert.send(cfg.n8n_webhook_url, "error", "discover returned 0 new rows")
        print("no new rows", file=sys.stderr)
        return 1
    print(f"staged {staged}")
    return 0


def cmd_approve(cfg, conn, args):
    cur = conn.cursor()
    if args.all:
        rows = db.rows_with_status(cur, "staged")
        if not rows:
            print("nothing staged")
            return 0
        for row in rows:
            db.set_status(cur, row["video_id"], "approved")
        conn.commit()
        print(f"approved {len(rows)}")
        return 0
    if not args.video_ids:
        print("provide video ids or --all", file=sys.stderr)
        return 2
    for vid in args.video_ids:
        if db.get(cur, vid) is None:
            print(f"unknown video_id: {vid}", file=sys.stderr)
            return 2
        if db.get(cur, vid)["status"] != "staged":
            print(f"{vid} is not staged", file=sys.stderr)
            return 2
        db.set_status(cur, vid, "approved")
    conn.commit()
    print(f"approved {len(args.video_ids)}")
    return 0


def cmd_publish(cfg, conn, args):
    auto = args.auto and not args.stage_only
    if cfg.stage_only and not auto:
        print("stage-only mode: pass --auto to upload")
    result = run_publish(cfg, conn, auto)
    print(result)
    if result["failed"] > 0:
        return 1
    if auto and result["posted"] == 0 and result["skipped"] == 0:
        return 1
    return 0


def cmd_status(cfg, conn, args):
    cur = conn.cursor()
    c = db.counts(cur)
    today = datetime.now(TZ).date().isoformat()
    allowed = publish.allowed_today(db.posted_today(cur, today), cfg.posts_per_day, cfg.max_posts_per_day)
    print(f"date={today} region={cfg.region} stage_only={cfg.stage_only}")
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print(f"remaining today: {allowed}")
    return 0


def cmd_run_daily(cfg, conn, args):
    rc = cmd_discover(cfg, conn, argparse.Namespace(limit=None))
    if rc == 1:
        return 1
    return cmd_publish(cfg, conn, argparse.Namespace(auto=args.auto, stage_only=False))


def build_parser():
    p = argparse.ArgumentParser(prog="autotik")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="fetch, download, transform, stage")
    d.add_argument("--limit", type=int, default=None)

    a = sub.add_parser("approve", help="move staged rows to approved")
    a.add_argument("video_ids", nargs="*")
    a.add_argument("--all", action="store_true")

    pub = sub.add_parser("publish", help="upload approved rows")
    g = pub.add_mutually_exclusive_group()
    g.add_argument("--auto", action="store_true", help="bypass stage-only guard")
    g.add_argument("--stage-only", action="store_true", help="explicit no-op")

    sub.add_parser("status", help="counts and remaining budget")

    r = sub.add_parser("run-daily", help="discover then publish")
    r.add_argument("--auto", action="store_true")
    return p


COMMANDS = {
    "discover": cmd_discover,
    "approve": cmd_approve,
    "publish": cmd_publish,
    "status": cmd_status,
    "run-daily": cmd_run_daily,
}


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code is not None else 2
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    conn = db.init_db(db_path(cfg))
    try:
        return COMMANDS[args.command](cfg, conn, args)
    except Exception:
        traceback.print_exc()
        alert.send(
            cfg.n8n_webhook_url, "error",
            f"autotik {args.command} crashed: {sys.exc_info()[1]}",
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
