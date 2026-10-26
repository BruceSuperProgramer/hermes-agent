#!/usr/bin/env python3
"""Watch the Hermes kanban board and alert the user's WeChat when tasks get stuck.

Read-only against kanban.db; sends via `hermes send` so no agent loop is needed.
Alert conditions (per task):
  - status == blocked
  - running with no heartbeat update for HEARTBEAT_STALE_SECS
  - running past its max_runtime_seconds
  - running with an expired claim
Deduped through a small local state file; repeat alerts every REPEAT_SECS.
Signatures are category-stable (embedded numbers collapsed) and the state is
saved even when a send fails, so an unpaired/down channel backs off instead of
retrying the identical alert every frame.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HERMES_HOME = "/Users/brucehe/.hermes"
# Explicit KANBAN_DB override wins; otherwise the canonical absolute path.
# Do NOT fall back to a bare/relative path when the override is unset.
KANBAN_DB = os.environ.get("KANBAN_DB") or os.path.join(HERMES_HOME, "kanban.db")
STATE_PATH = os.path.join(HERMES_HOME, "kanban_stuck_watch_state.json")
HERMES_BIN = os.path.join(HERMES_HOME, "hermes-agent/.hermes/bin/hermes")
WEIXIN_TARGET = "weixin:o9cq80_v94rrDlotdhhywlpG9C_I@im.wechat"

HEARTBEAT_STALE_SECS = 15 * 60       # alert well before the 4h dispatcher reclaim
REPEAT_SECS = 6 * 60 * 60            # re-alert a still-stuck task every 6h
MAX_ROWS = 20                        # cap message size per alert


def find_stuck():
    now = int(time.time())
    # A read-only connection cannot create the WAL -shm wal-index: whenever the
    # last writer has closed cleanly (SQLite deletes -wal/-shm), the first
    # query fails with SQLITE_CANTOPEN "unable to open database file". Open a
    # read-write-capable connection like every other kanban reader and enforce
    # read-only semantics in SQL via PRAGMA query_only instead of mode=ro.
    conn = sqlite3.connect(KANBAN_DB, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, assignee, status, started_at, max_runtime_seconds,
               last_heartbeat_at, claim_expires, consecutive_failures,
               last_failure_error, worker_pid, current_run_id
        FROM tasks
        WHERE status = 'blocked'
           OR (status = 'running' AND (
                (max_runtime_seconds IS NOT NULL AND started_at IS NOT NULL
                   AND ? - started_at > max_runtime_seconds)
                OR (claim_expires IS NOT NULL AND claim_expires < ?)
                OR ((last_heartbeat_at IS NOT NULL AND ? - last_heartbeat_at > ?)
                    OR (last_heartbeat_at IS NULL AND started_at IS NOT NULL
                        AND ? - started_at > ?))
              ))
        """,
        (now, now, now, HEARTBEAT_STALE_SECS, now, HEARTBEAT_STALE_SECS),
    ).fetchall()
    conn.close()

    stuck = []
    for r in rows:
        reasons = []
        if r["status"] == "blocked":
            reasons.append("blocked" + (f' ({r["last_failure_error"]})' if r["last_failure_error"] else ""))
        else:
            if r["last_heartbeat_at"]:
                if now - r["last_heartbeat_at"] > HEARTBEAT_STALE_SECS:
                    reasons.append(f"no heartbeat for {now - r['last_heartbeat_at']}s")
            elif r["started_at"]:
                if now - r["started_at"] > HEARTBEAT_STALE_SECS:
                    reasons.append(f"running {now - r['started_at']}s without any heartbeat")
            if r["max_runtime_seconds"] and r["started_at"] and now - r["started_at"] > r["max_runtime_seconds"]:
                reasons.append(f"past max_runtime ({r['max_runtime_seconds']}s)")
            if r["claim_expires"] and r["claim_expires"] < now:
                reasons.append("claim expired")
        if reasons:
            stuck.append({"id": r["id"], "title": r["title"], "assignee": r["assignee"],
                          "status": r["status"], "reasons": "; ".join(reasons)})
    return stuck


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def send_weixin(text):
    result = subprocess.run(
        [HERMES_BIN, "send", "--to", WEIXIN_TARGET, text],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode != 0:
        print(f"send failed: {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)
        return False
    return True


def main():
    stuck = find_stuck()
    now = int(time.time())
    state = load_state()

    # Forget entries for tasks that recovered.
    for task_id in list(state):
        if not any(s["id"] == task_id for s in stuck):
            del state[task_id]

    to_alert = []
    for s in stuck:
        prev = state.get(s["id"])
        # Reasons embed evolving values ("no heartbeat for 1234s") that change
        # every frame, which defeated this dedup and re-alerted each cycle.
        # Collapse digit runs so the signature encodes only the reason
        # CATEGORY; a genuinely new category still differs and re-alerts.
        signature = s["status"] + "|" + re.sub(r"\d+", "N", s["reasons"])
        if not prev or prev.get("sig") != signature or now - prev.get("ts", 0) > REPEAT_SECS:
            to_alert.append(s)
            state[s["id"]] = {"sig": signature, "ts": now}

    if not to_alert:
        save_state(state)
        return 0

    lines = ["[Hermes Kanban] 卡住提醒"]
    for s in to_alert[:MAX_ROWS]:
        lines.append(f'- [{s["status"]}] {s["title"]} ({s["id"]}, {s["assignee"]}): {s["reasons"]}')
    if len(to_alert) > MAX_ROWS:
        lines.append(f"(另有 {len(to_alert) - MAX_ROWS} 个，请查看 kanban)")

    sent = send_weixin("\n".join(lines))
    # Land the dedup state even when the send failed. Skipping the save used
    # to leave the state file empty, so every launchd frame retried the same
    # alert (11k+ "send failed" log lines while the Weixin channel sat
    # unpaired). The saved ts is the backoff: retry no earlier than
    # REPEAT_SECS; a changed reason category still alerts immediately.
    if not sent:
        print(f"alert send failed; dedup state saved anyway, backing off {REPEAT_SECS}s", file=sys.stderr)
    save_state(state)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        msg = f"[Hermes Kanban] watchdog 测试消息 {time.strftime('%Y-%m-%d %H:%M:%S')}"
        sys.exit(0 if send_weixin(msg) else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        for s in find_stuck():
            print(s)
        sys.exit(0)
    sys.exit(main())
