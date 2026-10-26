# local-ops/launchd — host launchd job records

Byte-for-byte records of launchd job assets installed on the operator host
(Bruces-MacBook-Pro, ~/.hermes local stack). These files exist so board cards
can carry a verifiable integration proof (repo blob ↔ host file, byte-identical)
for fixes that live outside any host git repository.

## ai.hermes.kanban-stuck-watch (t_3fd6ce6f)

- Host script: `/Users/brucehe/.hermes/scripts/kanban_stuck_watch.py`
  (sha256 d9c7db256993358a748660cbfbc6cafdf38727a64722ad702a23c63375a2bb38)
- Host plist: `/Users/brucehe/Library/LaunchAgents/ai.hermes.kanban-stuck-watch.plist`
- Fix (card t_3fd6ce6f): KANBAN_DB env override + constant `/Users/brucehe/.hermes/kanban.db`
  fallback; read-only connection upgraded to rw + `PRAGMA query_only` for the sweep
  queries. HERMES_HOME env injection into the plist is intentionally NOT needed —
  the script no longer reads env for the DB path.
- Provisioning: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.kanban-stuck-watch.plist`
  (StartInterval 120; EnvironmentVariables carries PATH only)
