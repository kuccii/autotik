import argparse
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from autotik import alert, db, discover, download, publish, transform
from autotik.config import ConfigError, load_config


def db_path(cfg):
    return Path(cfg.data_dir) / "autotik.sqlite"


def videos_dir(cfg):
    return Path(cfg.data_dir) / "videos"


def run_discover(cfg, conn, limit=None):
    fetch = cfg.discover_fetch
    keep = limit if limit is not None else cfg.discover_keep
    import httpx

    now = time.time()
    cur = conn.cursor()
    with httpx.Client(follow_redirects=True) as client:
        feed = discover.fetch_feed(client, cfg.discover_regions, fetch)
        items = discover.parse_items(feed)
        items = discover.filter_by_age(items, now, cfg.discover_max_age_days)
        items = discover.filter_by_likes(items, cfg.discover_min_likes)
        items = [i for i in items if db.get(cur, i["video_id"]) is None]
        items = discover.rank(items, keep, now)
        if not items:
            return 0
        staged = 0
        failures = []
        for item in items:
            try:
                path, digest = download.download(item, videos_dir(cfg), client)
            except download.DownloadError as exc:
                db.log(cur, item["video_id"], "download_failed", str(exc)[:300])
                conn.commit()
                failures.append(f"download {item['video_id']}: {exc}")
                continue
            try:
                out = transform.transform(
                    path, path.with_name(f"{item['video_id']}_tf.mp4"),
                    cfg.overlay_account, cfg.overlay_enabled, cfg.overlay_style,
                )
            except transform.TransformError as exc:
                failures.append(f"transform {item['video_id']}: {exc}")
                db.log(cur, item["video_id"], "transform_failed", str(exc)[:300])
                try:
                    db.insert_candidates(
                        cur,
                        [
                            dict(
                                item,
                                desc_orig=item["desc"],
                                file_path=None,
                                file_hash=digest,
                                status="failed",
                                error=str(exc)[:500],
                            )
                        ],
                    )
                except db.DuplicateHash as dup:
                    failures.append(f"duplicate {item['video_id']}: {dup}")
                    db.log(cur, item["video_id"], "duplicate_hash", str(dup)[:300])
                    conn.commit()
                    continue
                conn.commit()
                continue
            try:
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
            except db.DuplicateHash as exc:
                failures.append(f"duplicate {item['video_id']}: {exc}")
                db.log(cur, item["video_id"], "duplicate_hash", str(exc)[:300])
                conn.commit()
                continue
            staged += 1
            conn.commit()
        if failures:
            detail = " | ".join(failures)
            if len(detail) > 400:
                detail = detail[:397] + "..."
            alert.send(
                cfg.n8n_webhook_url,
                "warn",
                f"discover: {len(failures)} failed: {detail}",
            )
        if 0 < staged < 3:
            alert.send(
                cfg.n8n_webhook_url,
                "warn",
                f"discover: only {staged} new videos staged",
            )
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
        approved = 0
        demoted = 0
        for row in rows:
            if cfg.discover_min_likes and (row["digg_count"] or 0) < cfg.discover_min_likes:
                db.set_status(cur, row["video_id"], "skipped", "below DISCOVER_MIN_LIKES")
                db.log(
                    cur, row["video_id"], "below_min_likes",
                    f"{row['digg_count']} < {cfg.discover_min_likes}",
                )
                demoted += 1
            else:
                db.set_status(cur, row["video_id"], "approved")
                approved += 1
        conn.commit()
        msg = f"approved {approved}"
        if demoted:
            msg += f", skipped {demoted} below {cfg.discover_min_likes} likes"
        print(msg)
        return 0
    if not args.video_ids:
        print("provide video ids or --all", file=sys.stderr)
        return 2
    approved = 0
    demoted = 0
    for vid in args.video_ids:
        row = db.get(cur, vid)
        if row is None:
            print(f"unknown video_id: {vid}", file=sys.stderr)
            return 2
        if row["status"] != "staged":
            print(f"{vid} is not staged", file=sys.stderr)
            return 2
        if cfg.discover_min_likes and (row["digg_count"] or 0) < cfg.discover_min_likes:
            db.set_status(cur, vid, "skipped", "below DISCOVER_MIN_LIKES")
            db.log(
                cur, vid, "below_min_likes",
                f"{row['digg_count']} < {cfg.discover_min_likes}",
            )
            demoted += 1
        else:
            db.set_status(cur, vid, "approved")
            approved += 1
    conn.commit()
    if demoted and not approved:
        print(f"skipped {demoted} below {cfg.discover_min_likes} likes", file=sys.stderr)
        return 2
    print(f"approved {approved}" + (f", skipped {demoted} below likes floor" if demoted else ""))
    return 0


def cmd_publish(cfg, conn, args):
    if args.stage_only:
        cfg = replace(cfg, stage_only=True)
    auto = args.auto
    if cfg.stage_only and not auto:
        print("stage-only mode: pass --auto to upload")
    result = run_publish(cfg, conn, auto)
    print(result)
    if result.get("failed", 0) > 0:
        return 1
    if auto and result.get("posted", 0) == 0 and result.get("skipped", 0) == 0:
        reason = result.get("reason")
        if reason == "not_due":
            return 0
        if reason == "budget_spent":
            print("budget spent")
            return 0
        alert.send(
            cfg.n8n_webhook_url, "error",
            "auto publish produced 0 posts: no approved candidates",
        )
        return 1
    return 0


def cmd_session_check(cfg, conn, args):
    try:
        result = publish.session_check(cfg)
    except publish.PublishError as exc:
        print(f"session check FAILED: {exc}", file=sys.stderr)
        alert.send(
            cfg.n8n_webhook_url, "error",
            f"tiktok session check FAILED: {exc}",
        )
        return 1
    print(f"session ok: posts={result['posts']}")
    return 0


def cmd_status(cfg, conn, args):
    cur = conn.cursor()
    c = db.counts(cur)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    allowed = publish.allowed_today(db.posted_today(cur, today), cfg.posts_per_day, cfg.max_posts_per_day)
    print(f"date={today} region={cfg.region} regions={','.join(cfg.discover_regions)} stage_only={cfg.stage_only}")
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print(f"remaining today: {allowed}")
    last = db.last_log(cur)
    if last is None:
        print("last run: never")
    else:
        print(f"last run: {last['ts']} {last['event']}")
    print(f"next slot: {publish.next_slot(cfg)}")
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
    d.add_argument("--limit", type=int, default=None, help="max new rows to stage (default: DISCOVER_KEEP)")

    a = sub.add_parser("approve", help="move staged rows to approved")
    a.add_argument("video_ids", nargs="*")
    a.add_argument("--all", action="store_true")

    pub = sub.add_parser("publish", help="upload approved rows")
    g = pub.add_mutually_exclusive_group()
    g.add_argument("--auto", action="store_true", help="bypass stage-only guard")
    g.add_argument("--stage-only", action="store_true", help="explicit no-op")

    sub.add_parser("status", help="counts and remaining budget")

    sub.add_parser(
        "session-check",
        help="open the persistent profile, verify the session, keep it alive",
    )

    r = sub.add_parser("run-daily", help="discover then publish")
    r.add_argument("--auto", action="store_true")
    return p


COMMANDS = {
    "discover": cmd_discover,
    "approve": cmd_approve,
    "publish": cmd_publish,
    "session-check": cmd_session_check,
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
    conn = None
    try:
        conn = db.init_db(db_path(cfg))
        return COMMANDS[args.command](cfg, conn, args)
    except db.BadTransition as exc:
        print(f"bad transition: {exc}", file=sys.stderr)
        alert.send(
            cfg.n8n_webhook_url, "error",
            f"autotik {args.command} bad transition: {exc}",
        )
        return 2
    except Exception:
        traceback.print_exc()
        alert.send(
            cfg.n8n_webhook_url, "error",
            f"autotik {args.command} crashed: {sys.exc_info()[1]}",
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
