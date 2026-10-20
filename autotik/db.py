import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = frozenset({"staged", "approved", "posted", "failed", "skipped"})

VALID = {
    "staged": frozenset({"approved", "failed", "skipped"}),
    "approved": frozenset({"posted", "failed", "skipped"}),
    "posted": frozenset(),
    "failed": frozenset({"staged"}),
    "skipped": frozenset(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id    TEXT PRIMARY KEY,
  author      TEXT,
  region      TEXT,
  desc_orig   TEXT,
  caption     TEXT,
  digg_count  INTEGER DEFAULT 0,
  play_count  INTEGER DEFAULT 0,
  share_count INTEGER DEFAULT 0,
  score       REAL DEFAULT 0,
  file_path   TEXT,
  file_hash   TEXT,
  source_url  TEXT,
  create_time INTEGER,
  transform_variant TEXT,
  status      TEXT NOT NULL DEFAULT 'staged',
  error       TEXT,
  created_at  TEXT NOT NULL,
  posted_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_hash ON videos(file_hash)
  WHERE file_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS post_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT,
  event TEXT NOT NULL,
  detail TEXT,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_post_log_video ON post_log(video_id, id);
CREATE TABLE IF NOT EXISTS post_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  views INTEGER,
  avg_watch_s REAL,
  full_watch_pct REAL,
  new_followers INTEGER,
  restricted INTEGER,
  captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_post_stats_video ON post_stats(video_id, id);
CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  followers INTEGER,
  captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS follows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  source TEXT,
  live_host TEXT,
  result TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_follows_username ON follows(username, id);
CREATE INDEX IF NOT EXISTS idx_follows_ts ON follows(ts);
"""


class DuplicateHash(RuntimeError):
    pass


class BadTransition(RuntimeError):
    pass


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    if "create_time" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN create_time INTEGER")
    if "transform_variant" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN transform_variant TEXT")
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_candidates(cur, rows):
    new = 0
    for r in rows:
        video_id = r["video_id"]
        status = r.get("status", "staged")
        if status not in STATUSES:
            raise BadTransition(f"unknown status {status!r}")
        existing = get(cur, video_id)
        if existing is not None:
            if r.get("file_hash") and existing["file_hash"] == r["file_hash"]:
                continue
        digest = r.get("file_hash")
        if digest:
            clash = cur.execute(
                "SELECT video_id FROM videos WHERE file_hash=? AND video_id<>?",
                (digest, video_id),
            ).fetchone()
            if clash is not None:
                raise DuplicateHash(
                    f"file_hash collision: {video_id} vs {clash['video_id']}"
                )
        cur.execute(
            """INSERT OR IGNORE INTO videos
               (video_id, author, region, desc_orig, digg_count, play_count,
                share_count, score, file_path, file_hash, source_url,
                create_time, transform_variant, status, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                video_id, r.get("author"), r.get("region"),
                r.get("desc_orig"), r.get("digg_count", 0),
                r.get("play_count", 0), r.get("share_count", 0),
                r.get("score", 0.0), r.get("file_path"),
                digest, r.get("source_url"), r.get("create_time"),
                r.get("transform_variant"),
                status, r.get("error"), _now(),
            ),
        )
        new += cur.rowcount
    return new


def set_status(cur, video_id, status, error=None):
    if status not in STATUSES:
        raise BadTransition(f"unknown status {status!r}")
    row = get(cur, video_id)
    if row is None:
        raise BadTransition(f"unknown video_id {video_id!r}")
    if status not in VALID[row["status"]]:
        raise BadTransition(
            f"{video_id}: {row['status']} -> {status} is not allowed"
        )
    posted_at = _now() if status == "posted" else row["posted_at"]
    cur.execute(
        "UPDATE videos SET status=?, error=?, posted_at=? WHERE video_id=?",
        (status, error, posted_at, video_id),
    )


def get(cur, video_id):
    return cur.execute(
        "SELECT * FROM videos WHERE video_id=?", (video_id,)
    ).fetchone()


def rows_with_status(cur, status):
    return cur.execute(
        "SELECT * FROM videos WHERE status=? ORDER BY score DESC, created_at ASC",
        (status,),
    ).fetchall()


def posted_today(cur, day_iso):
    row = cur.execute(
        "SELECT count(*) c FROM videos WHERE posted_at LIKE ?",
        (day_iso + "%",),
    ).fetchone()
    return row["c"]


def counts(cur):
    out = {s: 0 for s in STATUSES}
    for r in cur.execute("SELECT status, count(*) c FROM videos GROUP BY status"):
        out[r["status"]] = r["c"]
    return out


def log(cur, video_id, event, detail=""):
    cur.execute(
        "INSERT INTO post_log (video_id, event, detail, ts) VALUES (?,?,?,?)",
        (video_id, event, detail, _now()),
    )


def iso_days_ago(days):
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")


def add_post_stats(cur, video_id, views=None, avg_watch_s=None,
                   full_watch_pct=None, new_followers=None, restricted=None):
    cur.execute(
        "INSERT INTO post_stats (video_id, views, avg_watch_s, full_watch_pct, "
        "new_followers, restricted, captured_at) VALUES (?,?,?,?,?,?,?)",
        (
            video_id, views, avg_watch_s, full_watch_pct, new_followers,
            1 if restricted else (0 if restricted is not None else None),
            _now(),
        ),
    )


def add_account_snapshot(cur, followers):
    cur.execute(
        "INSERT INTO account_snapshots (followers, captured_at) VALUES (?,?)",
        (followers, _now()),
    )


def last_log(cur):
    return cur.execute(
        "SELECT * FROM post_log ORDER BY id DESC LIMIT 1"
    ).fetchone()


def follow_log_add(cur, username, source, live_host, result):
    cur.execute(
        "INSERT INTO follows (username, source, live_host, result, ts) "
        "VALUES (?,?,?,?,?)",
        (username, source, live_host, result, _now()),
    )


def followed_usernames(cur):
    return {
        row[0].lower()
        for row in cur.execute(
            "SELECT DISTINCT username FROM follows WHERE result IN "
            "('followed','would','already')"
        )
    }


def follows_since(cur, ts_iso):
    return cur.execute(
        "SELECT COUNT(*) FROM follows WHERE ts >= ? AND result IN "
        "('followed','would')",
        (ts_iso,),
    ).fetchone()[0]
