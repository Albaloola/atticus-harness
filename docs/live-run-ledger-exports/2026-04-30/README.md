# Live Run Ledger Exports - 2026-04-30

These files are committed as project diagnostics for the Atticus hardening review. They are exported from the local live matter DB after the stopped DeepSeek/OpenRouter run.

The export intentionally does **not** include plaintext provider keys, SQLite database files, raw candidate outputs, raw artifacts, source documents, or `.atticus-runs/` files. Secret-like string patterns are redacted during export.

| File | Rows | Description |
| --- | ---: | --- |
| `aggregates.json` | 1 | Counts and grouped summaries for fast review. |
| `certifications.full.jsonl` | 10 | Matter/task certifications present at stop. |
| `error_logs.full.jsonl` | 187 | Full `error_logs` table, with payload JSON parsed and secret patterns redacted. |
| `events.full.jsonl` | 7448 | Full event chain from `events`, with payload JSON parsed and secret patterns redacted. |
| `human_attention.full.jsonl` | 373 | Full `human_attention` table. |
| `leases.full.jsonl` | 779 | Full lease lifecycle table. |
| `orchestrator_events.full.jsonl` | 187 | Full `orchestrator_events` table. |
| `provider_runs.full.jsonl` | 418 | Full `provider_runs` telemetry; usage JSON parsed where valid. |
| `tasks.control_state.jsonl` | 361 | Task control state without task instruction bodies, to avoid embedding case work product text. |
| `validation_results.full.jsonl` | 1031 | Full validation result ledger. |

Use this alongside `../atticus_live_run_hardening_report_2026-04-30.md`. The report explains the failure mechanisms; these JSONL files preserve the machine-readable control-plane trail.

Important: these logs are engineering evidence. They are not legal evidence and do not replace the live matter ledger.
