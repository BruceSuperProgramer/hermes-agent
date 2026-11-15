"""Failure recovery triage must not regenerate the original product work."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as specify


@pytest.fixture
def triage_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        fresh = kb.create_task(conn, title="new request", triage=True)
        failed = kb.create_task(conn, title="already delivered")
        assert kb.block_task(conn, failed, kind="capability", reason="hash unavailable")
        assert kb.unblock_task(conn, failed)
        assert kb.block_task(conn, failed, kind="capability", reason="attachment channel unavailable")
        assert kb.get_task(conn, failed).status == "triage"
    return fresh, failed


@pytest.mark.parametrize("module", [decomp, specify])
def test_failure_triage_is_excluded_from_automatic_work(triage_cards, module):
    fresh, failed = triage_cards
    assert module.list_triage_ids() == [fresh]
    task, reason = specify._load_triage_task(failed)
    assert task is None
    assert "recovery" in reason.lower()


def test_explicit_decompose_refuses_before_model_call(triage_cards, monkeypatch):
    _, failed = triage_cards

    def unexpected_model_call(*args, **kwargs):
        pytest.fail("failure recovery must not call the product decomposer")

    monkeypatch.setattr(decomp, "_call_aux", unexpected_model_call)
    outcome = decomp.decompose_task(failed)
    assert not outcome.ok
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, failed).status == "triage"
        assert not conn.execute("SELECT 1 FROM task_links WHERE child_id=?", (failed,)).fetchone()


def test_failure_escalation_during_model_call_cannot_commit_children(triage_cards):
    _, failed = triage_cards
    with kbc.connect_closing() as conn:
        assert decompose_triage_task(
            conn, failed, children=[{"title": "duplicate", "body": "redo work", "parents": []}],
            author="auto-decomposer", root_assignee=None,
        ) is None
        assert not kb.specify_triage_task(conn, failed, title="redo work")
        assert kb.get_task(conn, failed).status == "triage"
