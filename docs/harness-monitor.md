# Atticus Harness Monitor

The harness monitor is an interactive terminal UI (and non-interactive JSON mode) for the Atticus legal harness. It gives a human operator — whether User or an agent — a single cockpit view of a matter without requiring knowledge of internal CLI commands.

## Quick start

```bash
# Interactive monitor (requires a TTY with curses support)
python -m atticus.cli monitor --db data/my-matter.sqlite --matter my-matter

# Non-interactive JSON dump (CI, logging, or non-TTY environments)
python -m atticus.cli monitor --db data/my-matter.sqlite --matter my-matter --once --json

# With custom refresh rate and output directory
python -m atticus.cli monitor \
  --db data/my-matter.sqlite \
  --matter my-matter \
  --output-dir OUT \
  --refresh-seconds 5
```

### Aliases

All three are equivalent:

```bash
python -m atticus.cli monitor --db DB --matter MATTER
python -m atticus.cli tui --db DB --matter MATTER
python -m atticus.cli console --db DB --matter MATTER
```

## Interactive mode

When you run the monitor in a TTY with curses support, you get a live-updating terminal dashboard:

```
┌ Atticus Monitor ─ napier-accommodation-arrears ─────────────────────────────┐
│ State: agent_can_continue       Final gate: human_blocked / not ready       │
│ Next: scheduler tick            Provider: ready / metadata only             │
├──────────────────────────────┬──────────────────────────────────────────────┤
│ Matter                        │ Next action                                  │
│ - runnable: 2                 │ owner: scheduler                             │
│ - failed: 0                   │ type: supervisor_tick                        │
│ - reducer pending: 0          │ reason: runnable tasks remain                │
│ - human questions: 1          │ command: run-free-loop ...                   │
├──────────────────────────────┴──────────────────────────────────────────────┤
│ Events / worker activity                                                     │
│ 09:21 lease acquired: task-...                                                │
│ 09:22 worker output candidate written                                         │
│ 09:22 citation support verification queued                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ Actions: [r]esume  [p]ause/stop  [q]uestion  [a]nswer  [g]ate                │
│          [v]reviews  [l]eases  [e]vents  [c]ommand  [R]efresh  [h]elp  [x]it│
└──────────────────────────────────────────────────────────────────────────────┘
```

### Status states

| State | Meaning |
|-------|---------|
| `complete` | Matter completion requirements are satisfied |
| `agent_can_continue` | The harness can continue without operator input |
| `needs_human_answer` | The harness has a concrete question for the operator |
| `needs_legal_review` | A high-risk reducer/legal decision must be reviewed |
| `blocked` | The harness is blocked and needs triage |

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `r` | Resume / continue safe harness work using the current agent packet |
| `p` | Pause / stop current run (requires confirmation) |
| `q` | Show the genuine human question if one exists |
| `a` | Submit answer to a human request (requires confirmation) |
| `g` | Show final-gate detail |
| `v` | Show reducer-review queue |
| `l` | Show active leases |
| `e` | Show recent events / logs |
| `c` | Show the exact next-action command |
| `R` | Refresh the display immediately |
| `h` | Show help screen |
| `x` | Exit the monitor only (does NOT stop the harness) |

### Safety boundaries

The monitor is **read-only by default**. These actions require explicit confirmation (`y`/`N` prompt):

- **Stop/pause current run** — cancels the active run, revokes live provider approval, cancels continuations, and releases leases
- **Submit a human response** — writes `human-response submit --write` after showing the question and response form
- **High-risk reducer/legal auto-run is blocked** — the monitor will never auto-accept a reducer candidate; it opens the review screen instead

What the monitor **never** does without separate review:

- Accept high-risk reducer candidates
- Send, file, upload, contact, or pay
- Create legal commitments
- Suppress genuine unresolved human questions
- Silently discard evidence or citations

## Non-interactive mode

For CI, logging, or environments without curses support:

```bash
python -m atticus.cli monitor --db DB --matter MATTER --once --json
```

Returns a complete state payload as JSON:

```json
{
  "matter": "napier-accommodation-arrears",
  "state": "needs_human_answer",
  "done": false,
  "counts": { "runnable": 0, "failed": 1, ... },
  "next_action": { "owner": "orchestrator", ... },
  "agent_packet": {
    "schema": "atticus.agent_handoff.v1",
    "needs_human": true,
    ...
  },
  "final_gate": { "state": "human_blocked", ... },
  "active_run": null,
  "recent_events": [...],
  "leases": [],
  "continuations": [],
  "reducer_reviews": [],
  "human_request": { "attention_id": 614, ... }
}
```

The `--once` flag (without `--json`) also outputs JSON. Either flag enables non-interactive mode.

If curses is not available in the Python environment, interactive mode falls back gracefully to `--once` JSON output.

## How agents use the same control panel

The monitor is a UI layer over the same backend contract that OpenClaw, Hermes, and Codex use. The agent handoff packet (`atticus.agent_handoff.v1`) is the stable interface:

```json
{
  "schema": "atticus.agent_handoff.v1",
  "needs_human": false,
  "may_run_without_asking_human": true,
  "requires_live_provider": true,
  "live_provider_gate": "not_a_human_blocker",
  "owner": "scheduler",
  "next_command": "python -m atticus.cli run-free-loop ...",
  "reason": "runnable tasks remain"
}
```

- `needs_human=true` → The agent must ask User the supplied question
- `needs_human=false`, `may_run_without_asking_human=true` → The agent can continue without interruption
- `needs_legal_review=true` → Do not auto-accept; open for review
- `requires_live_provider=true` → Operational metadata only; not a human blocker

## Future terminal UI direction

The monitor payload (`MonitorState.as_dict()`) is designed so a richer TUI can be added later without changing the harness:

- **Textual** or **Rich** could provide panels, scrollback, command palette, modal dialogs, keybinding help
- A future TUI could show worker activity per lease, provider readiness/cost metadata, and inline response submission
- The product contract is the JSON payload — any future UI consumes the same data

## Architecture

```
atticus/monitor/
├── __init__.py    # Public API exports
├── state.py       # MonitorState dataclass + build_monitor_state()
├── actions.py     # Action handlers (resume, stop, answer, etc.)
└── tui.py         # curses TUI + run_once_json()
```

The monitor **does not duplicate internal harness logic**. It calls the existing product-level functions:

- `build_operator_control_panel()` — the primary state aggregate
- `build_agent_handoff_packet()` — the agent contract
- `final_gate_readiness()` — gate status
- `list_reducer_reviews()` — review queue
- Direct SQL queries for runs, leases, continuations, and events that the control panel does not expose
