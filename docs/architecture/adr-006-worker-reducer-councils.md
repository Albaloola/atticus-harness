# ADR-006: Worker, Reducer, And Council Architecture

Status: Implemented

## Decision

Workers are bounded proposers. Reducers are the only canonical writers. Councils are structured reviewer/proposer flows with explicit reducer decision logic.

## Implementation

- `WorkerEnvelope` and `WorkOrder` define bounded task packets.
- `candidate_outputs` stores worker result packets and quarantined late/invalid outputs.
- `record_worker_result()` validates active lease and packet schema; expired or invalid outputs are quarantined.
- `reduce_candidate()` requires reducer role, active reducer lease, candidate status, and reducer validations before canonical artifact creation.
- `reducer_packets` preserves reducer decisions and dissent fields.
- `council_runs` and `council_votes` persist council state; `reduce_votes()` blocks on explicit rejection and otherwise selects majority candidate.

## Consequences

- Workers cannot write canonical legal memory.
- Reducers are auditable canonical writers.
- High-value council work can fan out, but convergence is controlled by reducer logic.
