"""SQLite state storage for the k2y VOD pipeline."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS vods (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL, started_at TEXT NOT NULL, duration_s INTEGER NOT NULL,
  source_url TEXT NOT NULL, category TEXT,
  state TEXT NOT NULL,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  plan_json TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
  vod_id INTEGER NOT NULL REFERENCES vods(id),
  idx INTEGER NOT NULL,
  title TEXT NOT NULL, start_s INTEGER NOT NULL, end_s INTEGER NOT NULL,
  file TEXT, thumb TEXT,
  youtube_id TEXT,
  upload_session TEXT,
  thumb_done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (vod_id, idx)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    """Connect to SQLite database, ensuring directory, pragmas, and schema exist."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_vod(conn: sqlite3.Connection, vod: dict) -> bool:
    """Insert a new VOD with state 'new' if unseen; never overwrite existing row."""
    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO vods (id, title, started_at, duration_s, source_url, category, state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                vod["id"],
                vod["title"],
                vod["started_at"],
                vod["duration_s"],
                vod["source_url"],
                vod.get("category"),
                _now_iso(),
            ),
        )
        return cur.rowcount > 0


def get_vod(conn: sqlite3.Connection, vod_id: int) -> sqlite3.Row | None:
    """Return VOD row or None if not found."""
    cur = conn.execute("SELECT * FROM vods WHERE id = ?", (vod_id,))
    return cur.fetchone()


def pending_vods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return unfinished VODs ordered by started_at."""
    cur = conn.execute(
        "SELECT * FROM vods WHERE state NOT IN ('done', 'failed', 'skipped') ORDER BY started_at"
    )
    return cur.fetchall()


def set_state(conn: sqlite3.Connection, vod_id: int, state: str, error: str | None = None) -> None:
    """Update state, error, and updated_at for a VOD."""
    with conn:
        conn.execute(
            "UPDATE vods SET state = ?, error = ?, updated_at = ? WHERE id = ?",
            (state, error, _now_iso(), vod_id),
        )


def record_error(conn: sqlite3.Connection, vod_id: int, error: str, max_attempts: int = 3) -> None:
    """Increment attempts and store error; mark 'failed' once max_attempts reached."""
    with conn:
        row = conn.execute("SELECT attempts FROM vods WHERE id = ?", (vod_id,)).fetchone()
        if row is None:
            return
        attempts = row["attempts"] + 1
        now = _now_iso()
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE vods SET attempts = ?, error = ?, state = 'failed', updated_at = ? WHERE id = ?",
                (attempts, error, now, vod_id),
            )
        else:
            conn.execute(
                "UPDATE vods SET attempts = ?, error = ?, updated_at = ? WHERE id = ?",
                (attempts, error, now, vod_id),
            )


def reset(conn: sqlite3.Connection, vod_id: int, state: str) -> None:
    """Clear error and attempts, setting a new state."""
    with conn:
        conn.execute(
            "UPDATE vods SET state = ?, error = NULL, attempts = 0, updated_at = ? WHERE id = ?",
            (state, _now_iso(), vod_id),
        )


def save_plan(conn: sqlite3.Connection, vod_id: int, plan: list[dict]) -> None:
    """Save segmentation plan as JSON."""
    with conn:
        conn.execute(
            "UPDATE vods SET plan_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(plan), _now_iso(), vod_id),
        )


def upsert_upload(
    conn: sqlite3.Connection,
    vod_id: int,
    idx: int,
    title: str,
    start_s: int,
    end_s: int,
    file: str | Path | None = None,
    thumb: str | Path | None = None,
) -> None:
    """Insert or update an upload row, preserving existing upload progress."""
    file_str = str(file) if file is not None else None
    thumb_str = str(thumb) if thumb is not None else None
    with conn:
        conn.execute(
            """
            INSERT INTO uploads (vod_id, idx, title, start_s, end_s, file, thumb)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vod_id, idx) DO UPDATE SET
              title = excluded.title,
              start_s = excluded.start_s,
              end_s = excluded.end_s,
              file = coalesce(excluded.file, uploads.file),
              thumb = coalesce(excluded.thumb, uploads.thumb)
            """,
            (vod_id, idx, title, start_s, end_s, file_str, thumb_str),
        )


def uploads(conn: sqlite3.Connection, vod_id: int) -> list[sqlite3.Row]:
    """Return all uploads for a VOD ordered by idx."""
    cur = conn.execute("SELECT * FROM uploads WHERE vod_id = ? ORDER BY idx", (vod_id,))
    return cur.fetchall()


def update_upload(conn: sqlite3.Connection, vod_id: int, idx: int, **fields) -> None:
    """Update existing upload columns only."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(uploads)")} - {"vod_id", "idx"}
    valid = {k: (str(v) if isinstance(v, Path) else v) for k, v in fields.items() if k in cols}
    if not valid:
        return
    set_clause = ", ".join(f"{col} = ?" for col in valid)
    with conn:
        conn.execute(
            f"UPDATE uploads SET {set_clause} WHERE vod_id = ? AND idx = ?",
            [*valid.values(), vod_id, idx],
        )
