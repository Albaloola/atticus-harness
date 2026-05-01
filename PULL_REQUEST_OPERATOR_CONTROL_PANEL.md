# Pull Request: Add operator control panel and agent handoff for human-run harness

## Summary

This pull request treats the Atticus harness as a product surface for humans and agents, not just an internal developer toolkit.

The harness is good code, but it must be runnable, monitorable, and recoverable by a normal human user without expecting that user to understand internal scheduler/orchestrator failures or manually debug accumulated “human blockers”.

This PR adds a human-facing control layer that answers:

- what is the matter state?
- what can the harness or agent safely do next?
- is this genuinely a question for Omer, or is it an internal scheduler/orchestrator/provider/reducer issue?
- what exact command carries Omer’s answer back into the harness?
- what provider capability is needed operationally, without making that a repeated human blocker?

## Problem

The current harness exposes too much of its internal machinery to the operator.

When something breaks, the harness can accumulate “human attention” items even where the issue should be handled automatically by the harness itself. That is not acceptable for a tool intended to be run by a real human user.

A user should not be expected to:

- understand task graphs;
- inspect internal scheduler failures;
- manually clean stale blockers;
- know which recovery command to run;
- determine whether a blocker belongs to the user, scheduler, orchestrator, provider control plane, or reducer;
- babysit normal provider-backed continuation;
- act as the glue between OpenClaw/Hermes/Codex and the harness.

The harness needs a single human-facing control surface and a stable agent contract.

## Key changes

### 1. Add an operator control panel

Add `atticus/operator_control.py` with a read-only operator control panel.

The control panel provides one product-level view of a matter:

- matter completion state;
- whether the harness can continue;
- final-gate state;
- current blocker classification;
- next exact command;
- routed human-attention counts;
- recommended commands;
- agent handoff packet.

CLI entrypoints:

```bash
python -m atticus.cli control-panel status --db DB --matter MATTER
python -m atticus.cli control-panel status --db DB --matter MATTER --json
python -m atticus.cli control-panel agent-packet --db DB --matter MATTER --json
```

Alias:

```bash
python -m atticus.cli panel status --db DB --matter MATTER
```

### 2. Add a stable agent handoff packet

Add an `atticus.agent_handoff.v1` JSON packet so OpenClaw, Hermes, Codex, or any other runner can know what to do next.

The packet must distinguish between:

- work the agent can continue without asking Omer;
- a genuine question that must be asked to Omer;
- high-risk reducer/legal review that must not be auto-accepted;
- provider capability needed operationally.

Contract:

```json
{
  "schema": "atticus.agent_handoff.v1",
  "matter": "napier-accommodation-arrears",
  "needs_human": false,
  "needs_legal_review": false,
  "may_run_without_asking_human": true,
  "requires_live_provider": true,
  "live_provider_gate": "not_a_human_blocker",
  "owner": "scheduler",
  "next_command": "...",
  "reason": "..."
}
```

If `needs_human=true`, the agent must ask Omer the supplied plain-language question and then submit the answer back to the harness.

If `needs_human=false` and `may_run_without_asking_human=true`, the agent can continue the harness without interrupting Omer.

### 3. Remove the live-provider human-interruption gate from the control-panel UX

The control panel must not stop merely to ask Omer about normal provider-backed continuation.

Provider-backed work may still expose:

```json
"requires_live_provider": true
```

But that is operational metadata, not a separate human blocker.

The control panel should not emit a state such as:

```text
needs_live_approval
```

The desired behaviour is:

- if the next action belongs to the scheduler/orchestrator/provider control plane and is otherwise safe, the control panel may report `agent_can_continue`;
- if a provider is required, expose that fact as metadata;
- do not interrupt Omer merely because the next normal continuation uses a live provider;
- do still preserve legal/reducer safety boundaries.

### 4. Add structured human request and response flow

Add or preserve CLI commands allowing the harness to ask Omer only when necessary:

```bash
python -m atticus.cli human-request next --db DB --matter MATTER
python -m atticus.cli human-request show --db DB --matter MATTER
```

The response path should carry Omer’s answer back into the harness:

```bash
python -m atticus.cli human-response submit \
  --db DB \
  --matter MATTER \
  --attention-id ATTENTION_ID \
  --response-type provided_best_available \
  --statement "This is the best available copy; proceed with caveat." \
  --write
```

If files are supplied, repeat:

```bash
--file PATH
```

Those files should be registered as operator-submitted supplemental sources and linked to the blocker/response.

### 5. Route internal failures away from Omer

Human-attention must be classified so internal failures do not accumulate as user blockers.

Internal problems should route to the correct owner:

- scheduler;
- orchestrator;
- provider control plane;
- reducer;
- proof/citation repair;
- validation repair;
- cleanup of stale or superseded items.

Only genuine operator questions should route to Omer.

Examples of things that should not become Omer blockers by default:

- stale local-stub failures;
- transient provider/network errors;
- validation failures that can create repair work;
- proof/citation repair issues;
- stale quarantined outputs;
- superseded no-progress signals;
- internal task dependency repair.

### 6. Add pause/stop controls

Add user-facing run controls so the operator can pause/stop work without understanding internals:

```bash
python -m atticus.cli run stop --db DB --run-id RUN_ID --write
python -m atticus.cli run stop-current --db DB --matter MATTER --write
```

These should support cancellation of active run state and related continuations/leases where appropriate.

### 7. Add documentation

Add docs at:

```text
docs/operator-control-panel.md
```

The docs should describe:

- control-panel status;
- JSON mode for UIs;
- agent handoff packet;
- human request/response flow;
- how provider capability is exposed without creating a repeated human blocker;
- future opencode-style terminal UI direction.

The future terminal UI should be able to show:

- start / resume matter;
- pause / stop run;
- current worker activity and leases;
- final-gate status;
- reducer-review status;
- exact human question, if one genuinely exists;
- response submission;
- provider readiness/cost metadata.

The UI can be inspired by opencode-style terminal interaction, but the harness should not depend on any specific frontend. The stable contract is the control-panel JSON.

## Safety requirements

- Control-panel status and agent-packet must be read-only.
- Live provider need is operational metadata, not a repeated human blocker.
- High-risk reducer/legal review remains non-automatic.
- No external legal action is introduced.
- No sending, filing, uploading, contacting, paying, or legal commitment is introduced.
- Agents may continue operational harness work, but must ask Omer only for genuine human questions.

## Expected files

Likely files touched or added:

```text
atticus/operator_control.py
atticus/cli.py
atticus/commands/registry.py
atticus/status/completion.py
atticus/status/human_attention_cleanup.py
atticus/db/repo.py
atticus/db/schema.py
atticus/scheduler/free_loop.py
atticus/scheduler/live_orchestrator.py
docs/operator-control-panel.md
tests/test_human_interface.py
```

The exact set may differ depending on current branch state.

## Validation

Run:

```bash
python -m pytest -q
python -m compileall -q atticus tests
git diff --check
```

Current validation seen on the working PR branch:

```text
568 passed, 1 skipped
```

## Acceptance criteria

The PR is acceptable when:

- `control-panel status` gives a human-readable matter state;
- `control-panel status --json` gives a UI/agent-friendly full payload;
- `control-panel agent-packet --json` gives a stable `atticus.agent_handoff.v1` packet;
- normal provider-backed continuation is not reported as a human blocker merely because it needs a live provider;
- `requires_live_provider=true` remains available as metadata;
- genuine human questions are presented plainly and include a response command;
- internal failures route to harness owners rather than accumulating as Omer blockers;
- high-risk reducer/legal review is still protected;
- tests pass.
