"""Official attachment writers must cooperate with an existing channel guard."""
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def guarded_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        conn.executescript("""
            ALTER TABLE task_attachments ADD COLUMN channel_token TEXT;
            CREATE TABLE channel_grants (
                token TEXT UNIQUE, task_id TEXT, uploaded_by TEXT,
                granted_at INTEGER, consumed_at INTEGER, consumer_row INTEGER
            );
            CREATE TRIGGER trg_task_attachments_channel_guard BEFORE INSERT ON task_attachments
            WHEN NOT EXISTS (
                SELECT 1 FROM channel_grants g WHERE g.token=NEW.channel_token
                AND g.task_id=NEW.task_id AND g.uploaded_by IS NEW.uploaded_by
                AND g.consumed_at IS NULL
                AND NEW.created_at-g.granted_at BETWEEN 0 AND 600
            )
            BEGIN SELECT RAISE(ABORT, 'channel grant required'); END;
            CREATE TRIGGER trg_task_attachments_consume_grant AFTER INSERT ON task_attachments
            BEGIN UPDATE channel_grants SET consumed_at=NEW.created_at,
                consumer_row=NEW.id WHERE token=NEW.channel_token; END;
            CREATE TRIGGER trg_task_attachments_stored_path_alias BEFORE INSERT ON task_attachments
            WHEN EXISTS (SELECT 1 FROM task_attachments
                         WHERE stored_path=NEW.stored_path AND filename=NEW.filename)
            BEGIN SELECT RAISE(ABORT, 'recorded path'); END;
        """)
    return tmp_path


def test_official_upload_consumes_grant_and_preserves_bytes(guarded_board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="report")
        aid = kb.store_attachment_bytes(conn, tid, "report.txt", b"evidence", uploaded_by="qa")
        row = conn.execute("SELECT * FROM task_attachments WHERE id=?", (aid,)).fetchone()
        grant = conn.execute("SELECT * FROM channel_grants").fetchone()
        assert Path(row["stored_path"]).read_bytes() == b"evidence"
        assert grant["task_id"] == tid
        assert grant["uploaded_by"] == "qa"
        assert grant["consumer_row"] == aid
        assert grant["consumed_at"] is not None
        with pytest.raises(sqlite3.IntegrityError, match="channel grant required"):
            conn.execute(
                "INSERT INTO task_attachments "
                "(task_id,filename,stored_path,size,created_at,uploaded_by,channel_token) "
                "VALUES (?, 'replay', 'replay', 0, ?, 'qa', ?)",
                (tid, int(time.time()), grant["token"]),
            )


def test_completion_artifact_commits_with_guard(guarded_board):
    workspace = guarded_board / ".hermes" / "kanban" / "workspaces" / "report-task"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("completed analysis")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="read-only analysis", workspace_kind="scratch",
            workspace_path=str(workspace),
        )
        assert kb.complete_task(
            conn, tid, summary="analysis delivered", metadata={"artifacts": [str(report)]},
        )
        assert kb.get_task(conn, tid).status == "done"
        attachment = kb.list_attachments(conn, tid)[0]
        assert Path(attachment.stored_path).read_text() == "completed analysis"
        assert conn.execute(
            "SELECT COUNT(*) FROM channel_grants WHERE consumer_row=? AND consumed_at IS NOT NULL",
            (attachment.id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "grant_task,grant_author,age,token",
    [("task", "qa", 0, None), ("other", "qa", 0, "grant"),
     ("task", "other", 0, "grant"), ("task", "qa", 601, "grant")],
)
def test_invalid_direct_writes_stay_denied(guarded_board, grant_task, grant_author, age, token):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="report")
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO channel_grants VALUES ('grant', ?, ?, ?, NULL, NULL)",
                (tid if grant_task == "task" else "other", grant_author, now - age),
            )
        with pytest.raises(sqlite3.IntegrityError, match="channel grant required"):
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_attachments "
                    "(task_id,filename,stored_path,size,created_at,uploaded_by,channel_token) "
                    "VALUES (?, 'bad', 'bad', 0, ?, 'qa', ?)", (tid, now, token),
                )
        assert not kb.list_attachments(conn, tid)


def test_attachment_failure_rolls_back_grant(guarded_board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="report")
        conn.execute("""
            CREATE TRIGGER refuse_attachment BEFORE INSERT ON task_attachments
            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END
        """)
        with pytest.raises(sqlite3.IntegrityError):
            kb.store_attachment_bytes(conn, tid, "report.txt", b"evidence")
        assert conn.execute("SELECT COUNT(*) FROM channel_grants").fetchone()[0] == 0
        assert not kb.list_attachments(conn, tid)


def test_partial_guard_schema_fails_closed(guarded_board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="report")
        conn.execute("DROP TRIGGER trg_task_attachments_consume_grant")
        with pytest.raises(sqlite3.IntegrityError):
            kb.store_attachment_bytes(conn, tid, "report.txt", b"evidence")
        assert not kb.list_attachments(conn, tid)
        assert conn.execute("SELECT COUNT(*) FROM channel_grants").fetchone()[0] == 0
