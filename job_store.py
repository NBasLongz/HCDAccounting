"""Small SQLite repository used by the demo to retain completed jobs."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# SQLite updates should not trigger uvicorn --reload in the source directory.
DB_PATH = Path(tempfile.gettempdir()) / "receiptflow" / "receipt_jobs.sqlite3"


def _connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
        total_files INTEGER NOT NULL, result_json TEXT NOT NULL, excel_path TEXT,
        error_message TEXT
    )""")
    return con


def save_job(job_id: str, result: dict[str, Any], excel_path: str) -> None:
    with _connection() as con:
        con.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, datetime.now(timezone.utc).isoformat(), "completed", result["stats"]["receipt_count"], json.dumps(result, ensure_ascii=False), excel_path, None),
        )


def get_job(job_id: str) -> dict[str, Any] | None:
    with _connection() as con:
        row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    result = json.loads(row["result_json"])
    result.update({"job_id": row["id"], "status": row["status"], "created_at": row["created_at"]})
    return result


def get_excel_path(job_id: str) -> str | None:
    with _connection() as con:
        row = con.execute("SELECT excel_path FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["excel_path"] if row else None


def recent_jobs(limit: int = 10) -> list[dict[str, Any]]:
    with _connection() as con:
        rows = con.execute("SELECT id, created_at, status, total_files, result_json FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"job_id": row["id"], "created_at": row["created_at"], "status": row["status"], "total_files": row["total_files"], "stats": json.loads(row["result_json"])["stats"]} for row in rows]
