"""Attachment inserts compatible with boards retaining the legacy channel guard."""
from __future__ import annotations

import secrets
import sqlite3


def insert_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str,
    content_type: str | None, size: int, uploaded_by: str | None, created_at: int,
) -> sqlite3.Cursor:
    """Insert inside the caller's transaction, honoring any installed channel guard."""
    columns = "task_id, filename, stored_path, content_type, size, uploaded_by, created_at"
    values: tuple = (task_id, filename, stored_path, content_type, size, uploaded_by, created_at)
    schema = {row[1] for row in conn.execute("PRAGMA table_info(task_attachments)")}
    if "channel_token" in schema:
        required = {
            "trg_task_attachments_channel_guard",
            "trg_task_attachments_consume_grant",
            "trg_task_attachments_stored_path_alias",
        }
        installed = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='task_attachments'"
        )}
        if not required.issubset(installed):
            raise sqlite3.IntegrityError("legacy attachment channel guard is incomplete")
        if not conn.in_transaction:
            raise sqlite3.ProgrammingError("attachment channel grants require a write transaction")
        # These boards outlive the code that installed their triggers. Keep their
        # one-use, task/author-bound grant contract instead of dropping the guard.
        token = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO channel_grants (token, task_id, uploaded_by, granted_at) VALUES (?, ?, ?, ?)",
            (token, task_id, uploaded_by, created_at),
        )
        columns += ", channel_token"
        values += (token,)
    return conn.execute(
        f"INSERT INTO task_attachments ({columns}) VALUES ({','.join('?' for _ in values)})",
        values,
    )
