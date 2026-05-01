# Pull Request: Add interactive harness monitor / terminal control console

## Summary

Add an interactive terminal monitor for the Atticus harness, inspired by the user experience of OpenCode CLI: a live terminal interface where the user can see what the harness is doing, inspect matter state, pause/resume work, answer genuine harness questions, and monitor agents/workers without needing to understand the internal codebase.

The existing operator control panel and agent handoff packet are the backend contract. This PR adds the human-facing terminal layer on top.

The goal is not to make Omer run low-level commands. The goal is for Omer to run one command and get an operable, visible harness console.

## Product goal

A normal command-line user should be able to run:

```bash
python -m atticus.cli monitor --db DB --matter MATTER
```

or:

```bash
atticus monitor --db DB --matter MATTER
```

and get an interactive terminal UI that shows:

- current matter state;
- active run / worker state;
- next harness action;
- current logs/events;
- outstanding genuine human questions;
- final-gate status;
- reducer-review status;
- provider readiness/cost metadata;
- active leases/continuations;
- commands/actions available now.

The interface should let the user interact without knowing the underlying command names.

## Inspiration: OpenCode-style terminal UX

OpenCode CLI works well because it feels like a live cockpit, not a script dump.

For Atticus, the equivalent should be:

- persistent terminal screen;
- visible status panels;
- scrolling event/log stream;
- command/action palette;
- keyboard shortcuts;
- clear confirmation prompts for sensitive actions;
- no requirement that the user knows internal CLI commands;
- agents remain optional, not mandatory.

Do not copy OpenCode code unless licensing and attribution are explicitly reviewed. The target is the interaction model, not literal code reuse.

## Proposed command surface

### Main monitor

```bash
atticus monitor --db DB --matter MATTER
```

Options:

```bash
atticus monitor \
  --db DB \
  --matter MATTER \
  --output-dir OUT \
  --refresh-seconds 2
```

Optional non-interactive mode for CI/logging:

```bash
atticus monitor --db DB --matter MATTER --once --json
```

### Suggested aliases

```bash
atticus tui --db DB --matter MATTER
atticus console --db DB --matter MATTER
```

`monitor` should be the canonical name.

## Backend contract

The TUI should consume existing/product-level APIs first, not duplicate internal logic:

```bash
atticus control-panel status --db DB --matter MATTER --json
atticus control-panel agent-packet --db DB --matter MATTER --json
atticus matter-health --db DB --matter MATTER --json
atticus next-action --db DB --matter MATTER --json
atticus final-gate readiness --db DB --matter MATTER --json
atticus human-request next --db DB --matter MATTER --json
atticus human-attention --db DB --matter MATTER --current-only --classify --json
```

Where possible, prefer direct Python function calls over subprocess calls inside the implementation.

## Terminal layout

Suggested first version layout:

```text
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
│ Actions: [r] resume  [p] pause/stop  [q] answer question  [g] final gate      │
│          [v] reducer review  [l] leases  [h] help  [x] exit                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Keyboard actions

Minimum useful shortcuts:

| Key | Action |
| --- | --- |
| `r` | Resume / continue safe harness work using the current agent packet |
| `p` | Pause/stop current run |
| `q` | Open genuine human question, if one exists |
| `a` | Submit answer to current human request |
| `g` | Show final-gate details |
| `v` | Show reducer-review queue |
| `l` | Show leases/continuations |
| `e` | Show recent events/logs |
| `c` | Copy/show exact command for current next action |
| `R` | Refresh now |
| `h` | Help |
| `x` | Exit monitor only; do not stop harness |

Sensitive actions should show a confirmation prompt:

- stop current run;
- reject reducer candidate;
- accept reducer candidate;
- write human response;
- external action, if ever added.

High-risk legal/reducer acceptance must remain protected and should not be a one-key accidental action.

## Interaction flows

### Flow 1: user opens monitor

1. User runs `atticus monitor --db DB --matter MATTER`.
2. Monitor loads `control-panel status` payload.
3. Monitor shows matter state and current next action.
4. If `agent_packet.may_run_without_asking_human=true`, the UI displays “Resume available”.
5. If `agent_packet.needs_human=true`, the UI displays the actual question for Omer.

### Flow 2: resume work

1. User presses `r`.
2. TUI shows the command that will run.
3. If the action is routine scheduler/orchestrator/provider-control work, run it.
4. If it is high-risk reducer/legal review, do not auto-run; open review screen instead.
5. Stream logs/events back into the monitor.
6. Refresh control-panel status after completion/tick.

### Flow 3: answer a human request

1. Harness reports `needs_human=true`.
2. User presses `q` or `a`.
3. TUI shows:
   - question;
   - why it is needed;
   - acceptable response types;
   - optional file picker/path prompt;
   - statement prompt.
4. TUI executes `human-response submit --write` only after confirmation.
5. TUI refreshes state.

### Flow 4: pause/stop

1. User presses `p`.
2. TUI shows active run if one exists.
3. User confirms stop.
4. TUI executes:

```bash
atticus run stop-current --db DB --matter MATTER --write
```

5. TUI refreshes and shows stopped/cancelled state.

## Implementation approach

### Phase 1: minimal no-dependency terminal monitor

Because `pyproject.toml` currently has no runtime dependencies, implement the first version using only the Python standard library where possible:

- `curses` for interactive terminal rendering;
- `sqlite3` direct reads through repo helpers;
- existing control-panel functions for state;
- bounded refresh interval, default 2 seconds;
- graceful fallback to `--once` text output if terminal does not support curses.

Files likely needed:

```text
atticus/monitor/__init__.py
atticus/monitor/state.py
atticus/monitor/tui.py
atticus/monitor/actions.py
atticus/cli.py
tests/test_monitor.py
docs/harness-monitor.md
```

### Phase 2: richer OpenCode-like UI

If adding a runtime dependency is acceptable, evaluate `textual` or `rich` for a better UI:

- panels;
- scrollback;
- command palette;
- modal dialogs;
- keybinding help;
- better colours and layout.

This should be an optional enhancement, not a blocker for the first usable monitor.

## Data model / state polling

The monitor state should be assembled from product-level functions:

- `build_operator_control_panel(...)`;
- `build_agent_handoff_packet(...)`;
- `final_gate_readiness(...)`;
- human request helpers;
- run/lease/continuation summaries.

Add a small state object such as:

```python
@dataclass(frozen=True)
class MonitorState:
    matter: str
    panel: dict[str, object]
    active_run: dict[str, object] | None
    recent_events: tuple[dict[str, object], ...]
    leases: tuple[dict[str, object], ...]
    continuations: tuple[dict[str, object], ...]
    reducer_reviews: tuple[dict[str, object], ...]
    human_request: dict[str, object] | None
```

## Required safety behaviour

The monitor must not make legal or external decisions by accident.

Allowed from monitor:

- read status;
- refresh;
- run routine internal continuation;
- stop/pause run;
- submit Omer’s explicit answer to a harness question;
- open/show reducer review;
- show exact commands.

Not allowed without explicit confirmation / separate review:

- accept high-risk reducer candidate;
- send/file/upload/contact/pay;
- create legal commitments;
- suppress genuine unresolved human questions;
- silently discard evidence/citations.

## Documentation

Add:

```text
docs/harness-monitor.md
```

Docs should include:

- how to start the monitor;
- keyboard shortcuts;
- what each status means;
- how to answer a harness question;
- how to pause/stop;
- how agents use the same control-panel packet;
- safety boundaries.

## Tests

Add tests for:

- monitor state builder uses control-panel payload;
- `--once --json` returns stable JSON;
- live provider requirement does not become a human-blocking state;
- human question appears when `agent_packet.needs_human=true`;
- action planner refuses to auto-run high-risk reducer review;
- stop action maps to `run stop-current`;
- missing curses/TTY falls back gracefully.

Example commands:

```bash
python -m pytest tests/test_monitor.py tests/test_human_interface.py -q
python -m compileall -q atticus tests
git diff --check
```

## Acceptance criteria

The PR is accepted when:

- `atticus monitor --db DB --matter MATTER --once --json` works in non-interactive environments;
- `atticus monitor --db DB --matter MATTER` opens an interactive terminal monitor in a TTY;
- the UI shows matter state, next action, recent events, final-gate state, and human question status;
- the user can pause/stop current run from the UI;
- the user can answer a genuine harness question from the UI;
- routine scheduler/orchestrator/provider-control continuation can be resumed without exposing internal command complexity;
- high-risk reducer/legal actions remain protected;
- provider capability is shown as metadata, not as an annoying repeated approval blocker;
- tests pass.
