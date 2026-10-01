import sqlite3
from datetime import datetime, timezone
from pathlib import Path

STATUSES = frozenset({"staged", "approved", "posted", "failed", "skipped"})

VALID = {
    "staged": frozenset({"approved", "failed", "skipped"}),
    "approved": frozenset({"posted", "failed"}),
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
                status, error, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                video_id, r.get("author"), r.get("region"),
                r.get("desc_orig"), r.get("digg_count", 0),
                r.get("play_count", 0), r.get("share_count", 0),
                r.get("score", 0.0), r.get("file_path"),
                digest, r.get("source_url"), status,
                r.get("error"), _now(),
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


def last_log(cur):
    return cur.execute(
        "SELECT * FROM post_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
