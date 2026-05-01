# Atticus Operator Control Panel

Atticus is a harness meant to be operated by people and by agents acting for people. The low-level commands remain auditable, but the normal entrypoint should be a control panel, not a pile of internal recovery commands.

## Goals

- One command shows whether a matter is done, blocked, or safe for an agent to continue.
- Human-attention does not mean “make Omer debug the harness”. Internal failures are routed back to the scheduler, orchestrator, provider control plane, or reducer.
- The harness emits an agent handoff packet so OpenClaw, Hermes, Codex, or another runner knows when to ask Omer a clear question and when to keep working without interrupting him.
- Any answer from Omer is carried back through `human-response submit`, with optional files registered as supplemental sources.

## Human-facing status

```bash
python -m atticus.cli control-panel status \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears
```

Use JSON for UIs:

```bash
python -m atticus.cli control-panel status \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --json
```

The panel reports:

- overall state: `complete`, `agent_can_continue`, `needs_human_answer`, `needs_legal_review`, or `blocked`
- the exact next safe command
- open/routed attention counts
- final-gate state
- a small set of recommended commands

## Agent handoff packet

```bash
python -m atticus.cli control-panel agent-packet \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --json
```

The packet is stable JSON with this contract:

- `needs_human=true`: ask Omer the provided question, then submit the answer using `on_answer` / `human-response submit`.
- `needs_human=false` and `may_run_without_asking_human=true`: the agent may run the supplied `next_command` subject to local policy and approval.
- `needs_legal_review=true`: do not auto-accept or canonically write a high-risk reducer/legal decision.
- `requires_live_provider=true`: the command needs provider capability, but this is operational metadata, not a separate human-interruption gate in the control panel.

## Carrying Omer's answer back

```bash
python -m atticus.cli human-response submit \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --attention-id 123 \
  --response-type provided_best_available \
  --statement "This is the best available copy; proceed with caveat." \
  --write
```

If Omer provides files, repeat `--file PATH`. The harness registers them as operator-submitted supplemental sources and links the response to the blocker.

## Future terminal UI

The control-panel payload is designed so a terminal UI can be added without changing the lower-level harness. A TUI can poll or subscribe to this one payload and expose familiar controls:

- start / resume matter
- pause / stop run
- show live provider readiness/cost metadata without turning normal provider use into a repeated human blocker
- show current worker activity and leases
- open the exact human question and submit the response
- show final gate / reducer review status

That UI can be inspired by opencode-style terminal interaction, but the harness should not depend on any specific frontend. The product contract is the JSON packet.
