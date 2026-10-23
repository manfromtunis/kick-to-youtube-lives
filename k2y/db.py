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


def set_state(
    conn: sqlite3.Connection,
    vod_id: int,
    state: str,
    error: str | None = None,
    reset_attempts: bool = True,
) -> None:
    """Update state, error, and updated_at for a VOD. Resets attempts when error is None and reset_attempts is True."""
    with conn:
        if error is not None:
            conn.execute(
                "UPDATE vods SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                (state, error, _now_iso(), vod_id),
            )
        elif reset_attempts:
            conn.execute(
                "UPDATE vods SET state = ?, error = NULL, attempts = 0, updated_at = ? WHERE id = ?",
                (state, _now_iso(), vod_id),
            )
        else:
            conn.execute(
                "UPDATE vods SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now_iso(), vod_id),
            )


def set_duration(conn: sqlite3.Connection, vod_id: int, duration_s: int) -> None:
    """Update probed duration of a VOD."""
    with conn:
        conn.execute(
            "UPDATE vods SET duration_s = ?, updated_at = ? WHERE id = ?",
            (duration_s, _now_iso(), vod_id),
        )


def set_error(conn: sqlite3.Connection, vod_id: int, error: str) -> None:
    """Set error message for a VOD without incrementing attempts or changing state."""
    with conn:
        conn.execute(
            "UPDATE vods SET error = ?, updated_at = ? WHERE id = ?",
            (error, _now_iso(), vod_id),
        )


def record_error(conn: sqlite3.Connection, vod_id: int, error: str, max_attempts: int = 3) -> None:
    """Increment attempts and store error; mark 'failed' once max_attempts reached."""
    with conn:
        conn.execute(
            """
            UPDATE vods
            SET attempts = attempts + 1,
                error = ?,
                state = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE state END,
                updated_at = ?
            WHERE id = ?
            """,
            (error, max_attempts, _now_iso(), vod_id),
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
    file: Path | None = None,
    thumb: Path | None = None,
) -> None:
    """Insert or update upload plan item."""
    file_str = str(file) if file else None
    thumb_str = str(thumb) if thumb else None
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


def all_vods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all VODs ordered by started_at ASC."""
    return conn.execute("SELECT * FROM vods ORDER BY started_at ASC").fetchall()


def clear_pending_uploads(conn: sqlite3.Connection, vod_id: int) -> None:
    """Delete uploads for vod_id where youtube_id IS NULL."""
    with conn:
        conn.execute(
            "DELETE FROM uploads WHERE vod_id = ? AND youtube_id IS NULL",
            (vod_id,),
        )


def delete_uploads(conn: sqlite3.Connection, vod_id: int) -> None:
    """Delete uploads for vod_id where youtube_id and upload_session are NULL.

    Raises RuntimeError if any rows with youtube_id or upload_session remain for that VOD.
    """
    remaining = conn.execute(
        "SELECT idx, youtube_id, upload_session FROM uploads WHERE vod_id = ? AND (youtube_id IS NOT NULL OR upload_session IS NOT NULL)",
        (vod_id,),
    ).fetchall()
    if remaining:
        raise RuntimeError(
            f"cannot delete uploads for VOD {vod_id}: {len(remaining)} uploaded or in-progress rows remain"
        )
    with conn:
        conn.execute(
            "DELETE FROM uploads WHERE vod_id = ? AND youtube_id IS NULL AND upload_session IS NULL",
            (vod_id,),
        )


def clear_upload_sessions(conn: sqlite3.Connection, vod_id: int) -> int:
    """Clear upload_session (set to NULL) for upload rows without youtube_id. Returns number of rows updated."""
    with conn:
        cur = conn.execute(
            "UPDATE uploads SET upload_session = NULL WHERE vod_id = ? AND youtube_id IS NULL AND upload_session IS NOT NULL",
            (vod_id,),
        )
        return cur.rowcount


def get_upload(conn: sqlite3.Connection, vod_id: int, idx: int) -> sqlite3.Row | None:
    """Return upload row for (vod_id, idx) or None if not found."""
    cur = conn.execute("SELECT * FROM uploads WHERE vod_id = ? AND idx = ?", (vod_id, idx))
    return cur.fetchone()


def set_upload_youtube_id(conn: sqlite3.Connection, vod_id: int, idx: int, youtube_id: str) -> None:
    """Record that an upload row is on YouTube: set youtube_id, clear upload_session, keep thumb_done."""
    with conn:
        conn.execute(
            "UPDATE uploads SET youtube_id = ?, upload_session = NULL WHERE vod_id = ? AND idx = ?",
            (youtube_id, vod_id, idx),
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
