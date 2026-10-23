"""Cross-component contract: a breaker-escalated card must stay stopped until an
explicit, evidence-backed recovery clears its hold (#kanban-reentry incident
2026-10-23: block_loop_detected triaged a card, the auto-decomposer re-promoted
it every dispatch tick with no new evidence — 5 idle worker runs).

Red state: these tests failed against the pre-fix kernel (list/specify/fanout
accepted any triage card; claim had no hold gate). They are the acceptance
tests for the recovery-hold contract.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """Isolated board DB; no hooks, no aux LLM, no live-board writes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "fixture.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **kw: None)
    kb.init_db(tmp_path / "fixture.db")
    with kbc.connect(tmp_path / "fixture.db") as conn:
        yield conn


def _escalate(conn) -> str:
    """Drive the REAL breaker: two dependency blocks on the same card."""
    tid = kb.create_task(conn, title="QA waits for a changed candidate", assignee="worker")
    for i in range(kb.BLOCK_RECURRENCE_LIMIT):
        if i:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="fixture")
        assert kb.block_task(conn, tid, reason="unchanged candidate", kind="dependency")
    task = kb.get_task(conn, tid)
    assert task.status == "triage"
    return tid


# --- the hold exists and is visible -------------------------------------------------

def test_breaker_escalation_sets_recovery_hold(board):
    tid = _escalate(board)
    assert kb.get_task(board, tid).recovery_hold == 1


def test_fresh_triage_has_no_hold(board):
    tid = kb.create_task(board, title="New bounded task", assignee="worker", triage=True)
    assert kb.get_task(board, tid).recovery_hold == 0


# --- automatic planning entries must refuse a held card -----------------------------

def test_held_card_not_selected_by_triage_sweeps(board):
    tid = _escalate(board)
    assert tid not in decomp.list_triage_ids()
    assert tid not in kanban_specify.list_triage_ids()


def test_specify_refused_while_held(board):
    tid = _escalate(board)
    assert kb.specify_triage_task(board, tid, title="Cosmetic rewrite") is False
    task = kb.get_task(board, tid)
    assert task.status == "triage" and task.recovery_hold == 1
    kinds = [e["kind"] for e in board.execute(
        "SELECT kind FROM task_events WHERE task_id=?", (tid,)).fetchall()]
    assert "specified" not in kinds


def test_decompose_fanout_refused_while_held(board):
    tid = _escalate(board)
    assert decompose_triage_task(
        board, tid, root_assignee="worker",
        children=[{"title": "Repeat same QA", "assignee": "worker"}], author="fixture",
    ) is None
    assert kb.get_task(board, tid).status == "triage"
    assert board.execute(
        "SELECT count(*) FROM task_links WHERE child_id=?", (tid,)).fetchone()[0] == 0


def test_decompose_task_refused_while_held(board):
    """Full decompose path (the gateway auto-decompose entry), aux stubbed."""
    tid = _escalate(board)
    routing = decomp._Routing("worker", "worker", True, [], {"worker"})
    with patch.object(decomp, "_load_routing", return_value=routing), patch.object(
        decomp, "_call_aux", return_value=(json.dumps(
            {"fanout": False, "title": "Rewrite", "body": "b"}), "")
    ):
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    assert outcome.ok is False
    assert "recovery hold" in (outcome.reason or "")
    task = kb.get_task(board, tid)
    assert task.status == "triage" and task.recovery_hold == 1


def test_claim_refused_while_held_even_if_forced_ready(board):
    """Final fallback: hold survives a bypassing status flip; claim still refuses."""
    tid = _escalate(board)
    claimed_before = board.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='claimed'", (tid,)).fetchone()[0]
    runs_before = board.execute(
        "SELECT count(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
    board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(board, tid, claimer="fixture") is None
    assert board.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='claimed'",
        (tid,)).fetchone()[0] == claimed_before  # no NEW claim
    rejected = board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='claim_rejected' "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert json.loads(rejected["payload"])["reason"] == "recovery_hold"
    assert board.execute(
        "SELECT count(*) FROM task_runs WHERE task_id=?",
        (tid,)).fetchone()[0] == runs_before  # no NEW run
    assert kb.get_task(board, tid).recovery_hold == 1


# --- recovery is explicit, evidence-backed and idempotent ---------------------------

def test_recover_requires_evidence(board):
    tid = _escalate(board)
    ok, reason = kb.recover_task(board, tid, evidence="   ", author="atlas")
    assert ok is False and "evidence" in reason
    assert kb.get_task(board, tid).recovery_hold == 1


def test_recover_clears_hold_and_lands_ready(board):
    tid = _escalate(board)
    ok, reason = kb.recover_task(
        board, tid, evidence="candidate sha abcdef0 reviewed and environment rebuilt",
        author="atlas-cto", idempotency_key="rec-1")
    assert ok is True, reason
    board2 = kb.get_task(board, tid)
    assert board2.status == "ready" and board2.recovery_hold == 0
    event = board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='recovery_granted'",
        (tid,)).fetchone()
    payload = json.loads(event["payload"])
    assert payload["evidence"].startswith("candidate sha")
    assert payload["author"] == "atlas-cto" and payload["idempotency_key"] == "rec-1"
    assert kb.claim_task(board, tid, claimer="fixture") is not None


def test_recover_lands_todo_when_parents_open(board):
    parent = kb.create_task(board, title="repair", assignee="worker")
    tid = _escalate(board)
    kb.link_tasks(board, parent, tid)
    ok, _ = kb.recover_task(board, tid, evidence="fix branch pushed", author="atlas")
    assert ok is True
    assert kb.get_task(board, tid).status == "todo"  # waits, no context-free respawn


def test_recover_idempotency_key_replay_is_noop(board):
    tid = _escalate(board)
    first = kb.recover_task(board, tid, evidence="e1", author="a", idempotency_key="k1")
    assert first[0] is True
    replay = kb.recover_task(board, tid, evidence="e1", author="a", idempotency_key="k1")
    assert replay[0] is True and "already" in replay[1]
    assert board.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='recovery_granted'",
        (tid,)).fetchone()[0] == 1
    again = kb.recover_task(board, tid, evidence="e2", author="a", idempotency_key="k2")
    assert again[0] is False  # hold already cleared; a second recovery is meaningless


def test_reblocked_after_recovery_sets_a_fresh_hold(board):
    """One recovery buys one execution window; another loop re-arms the hold."""
    tid = _escalate(board)
    assert kb.recover_task(board, tid, evidence="e1", author="a")[0] is True
    assert kb.claim_task(board, tid, claimer="fixture") is not None
    assert kb.block_task(board, tid, reason="failed again", kind="needs_input")
    assert kb.unblock_task(board, tid)
    assert kb.claim_task(board, tid, claimer="fixture") is not None
    assert kb.block_task(board, tid, reason="failed again", kind="needs_input")
    task = kb.get_task(board, tid)
    assert task.status == "triage" and task.recovery_hold == 1


# --- legitimate flows are untouched --------------------------------------------------

def test_fresh_triage_still_specifiable_and_plannable(board):
    tid = kb.create_task(board, title="New bounded task", assignee="worker", triage=True)
    assert tid in decomp.list_triage_ids()
    assert kb.specify_triage_task(board, tid, body="A concrete task") is True
    assert kb.get_task(board, tid).status == "ready"


def test_single_block_and_unblock_flow_unaffected(board):
    tid = kb.create_task(board, title="normal", assignee="worker")
    assert kb.claim_task(board, tid, claimer="fixture") is not None
    assert kb.block_task(board, tid, reason="waiting on reply", kind="needs_input")
    assert kb.get_task(board, tid).status == "blocked"
    assert kb.unblock_task(board, tid) is True
    assert kb.get_task(board, tid).recovery_hold == 0
    assert kb.claim_task(board, tid, claimer="fixture") is not None


def test_real_dependency_wait_still_silent(board):
    parent = kb.create_task(board, title="repair", assignee="worker")
    child = kb.create_task(board, title="QA", assignee="worker", parents=[parent])
    for _ in range(10):
        kb.recompute_ready(board)
        assert kb.claim_task(board, child, claimer="fixture") is None
    assert kb.get_task(board, child).status == "todo"
    assert board.execute(
        "SELECT count(*) FROM task_runs WHERE task_id=?", (child,)).fetchone()[0] == 0


# --- fanout inherits the root's open external prerequisites -------------------------

def test_fanout_children_inherit_open_external_parents(board):
    external = kb.create_task(board, title="external prerequisite", assignee="worker")
    root = kb.create_task(board, title="fanout me", assignee="worker", triage=True)
    kb.link_tasks(board, external, root)
    child_ids = decompose_triage_task(
        board, root, root_assignee="worker",
        children=[{"title": "c1", "assignee": "worker"},
                  {"title": "c2", "assignee": "worker", "parents": [0]}],
        author="fixture")
    assert child_ids is not None
    for cid in child_ids:
        assert board.execute(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
            (external, cid)).fetchone() is not None
    # A done/archived external parent must NOT be re-linked as a constraint.
    kb.complete_task(board, external, result="ok", summary="done")
    root2 = kb.create_task(board, title="fanout me 2", assignee="worker", triage=True)
    kb.link_tasks(board, external, root2)
    child_ids2 = decompose_triage_task(
        board, root2, root_assignee="worker",
        children=[{"title": "c3", "assignee": "worker"}], author="fixture")
    assert child_ids2 is not None
    assert board.execute(
        "SELECT count(*) FROM task_links WHERE parent_id=? AND child_id=?",
        (external, child_ids2[0])).fetchone()[0] == 0


# --- migration backfills holds for already-escalated legacy cards --------------------

def test_migration_backfills_hold_for_escalated_triage_cards(tmp_path, monkeypatch):
    """A legacy DB (no recovery_hold column) that already holds an escalated
    triage card must come back up WITH the hold after the new binary opens it."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **kw: None)
    kb.init_db(db_path)
    with kbc.connect(db_path) as conn:
        tid = _escalate(conn)
    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE tasks DROP COLUMN recovery_hold")
    raw.commit()
    raw.close()
    kb.init_db(db_path)
    with kbc.connect(db_path) as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "triage" and task.recovery_hold == 1
        fresh = kb.create_task(conn, title="fresh plan", assignee="worker", triage=True)
        assert kb.get_task(conn, fresh).recovery_hold == 0


def test_backfill_negative_specified_after_escalation_stays_unheld(tmp_path, monkeypatch):
    """A legacy card whose breaker escalation was ALREADY followed by a real
    specification must NOT be re-armed by the backfill (whitelist branch)."""
    import sqlite3

    db_path = tmp_path / "legacy2.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **kw: None)
    kb.init_db(db_path)
    with kbc.connect(db_path) as conn:
        tid = _escalate(conn)
    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE tasks DROP COLUMN recovery_hold")
    raw.commit()
    # Simulate the OLD binary having re-specified the card after escalation:
    # title rewrite + 'specified' event AFTER the block_loop_detected record.
    raw.execute("UPDATE tasks SET title='rewritten by old specifier' WHERE id=?", (tid,))
    raw.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'specified', NULL, strftime('%s','now'))",
        (tid,))
    raw.commit()
    raw.close()
    kb.init_db(db_path)
    with kbc.connect(db_path) as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "triage" and task.recovery_hold == 0


def test_recover_lands_review_when_that_is_where_it_left_off(board):
    parent_free = kb.create_task(board, title="review-bound card", assignee="worker")
    assert kb.claim_task(board, parent_free, claimer="fixture") is not None
    assert kb.request_review(board, parent_free, summary="implemented") is True
    assert kb.claim_review_task(board, parent_free, claimer="fixture") is not None
    assert kb.block_task(board, parent_free, reason="blocked while in review", kind="needs_input")
    assert kb.unblock_task(board, parent_free)
    # A review-resumed card lands back in review (not ready), so the second
    # cycle goes through the reviewer claim.
    assert kb.claim_review_task(board, parent_free, claimer="fixture") is not None
    assert kb.block_task(board, parent_free, reason="blocked again", kind="needs_input")
    assert kb.get_task(board, parent_free).status == "triage"
    ok, _ = kb.recover_task(board, parent_free, evidence="reviewer replied with approval", author="atlas")
    assert ok is True
    assert kb.get_task(board, parent_free).status == "review"
