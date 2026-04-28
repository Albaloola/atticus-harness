"""SQLite schema for the Atticus legal harness.

The schema intentionally keeps an append-only event stream beside mutable
projection tables. SQLite is the current durable store; every table here is
designed so it can later be replayed or migrated to Postgres without changing
the legal operating model.
"""

from __future__ import annotations

SCHEMA_VERSION = 2

DDL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS matters (
  matter_scope TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL DEFAULT 'atticus',
  state TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  budget_limit_usd REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  matter_scope TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  previous_hash TEXT,
  event_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  path TEXT NOT NULL,
  source_type TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  trust_status TEXT NOT NULL,
  stage TEXT NOT NULL,
  imported_from TEXT,
  chain_of_custody_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(chain_of_custody_json)),
  stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS sources_scope_path_uq ON sources(matter_scope, path);

CREATE TABLE IF NOT EXISTS source_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  captured_by TEXT NOT NULL,
  custody_note TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  path TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  stage TEXT NOT NULL,
  trust_status TEXT NOT NULL,
  sha256 TEXT,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  imported_from TEXT,
  produced_by_task_id TEXT,
  replaced_by_artifact_id TEXT,
  stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE IF NOT EXISTS artifact_versions (
  artifact_version_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  version_number INTEGER NOT NULL,
  sha256 TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by_task_id TEXT,
  created_by_role TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(artifact_id, version_number)
) STRICT;

CREATE TABLE IF NOT EXISTS artifact_sources (
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  dependency_type TEXT NOT NULL DEFAULT 'supports',
  PRIMARY KEY (artifact_id, source_id)
) STRICT;

CREATE TABLE IF NOT EXISTS artifact_dependencies (
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  dependency_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  dependency_type TEXT NOT NULL DEFAULT 'derived_from',
  created_at TEXT NOT NULL,
  PRIMARY KEY (artifact_id, dependency_artifact_id, dependency_type)
) STRICT;

CREATE TABLE IF NOT EXISTS extraction_records (
  extraction_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  method TEXT NOT NULL,
  coverage_status TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS ocr_records (
  ocr_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  engine TEXT NOT NULL,
  page_count INTEGER NOT NULL DEFAULT 0,
  coverage_status TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS transcription_records (
  transcription_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  engine TEXT NOT NULL,
  duration_seconds REAL,
  coverage_status TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS production_mappings (
  mapping_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
  artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  production_id TEXT NOT NULL,
  produced_path TEXT NOT NULL DEFAULT '',
  bates_start TEXT NOT NULL DEFAULT '',
  bates_end TEXT NOT NULL DEFAULT '',
  integrity_status TEXT NOT NULL DEFAULT 'candidate',
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS issues (
  issue_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  title TEXT NOT NULL,
  route TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS chronology_events (
  chronology_event_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  event_date TEXT NOT NULL DEFAULT '',
  event_date_precision TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'candidate',
  created_by_artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS legal_authorities (
  authority_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  jurisdiction TEXT NOT NULL DEFAULT '',
  citation TEXT NOT NULL,
  authority_type TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  source_url TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  claim_text TEXT NOT NULL,
  issue_id TEXT REFERENCES issues(issue_id) ON DELETE SET NULL,
  support_status TEXT NOT NULL DEFAULT 'candidate',
  created_by_artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS citation_spans (
  citation_span_id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
  artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  authority_id TEXT REFERENCES legal_authorities(authority_id) ON DELETE SET NULL,
  start_offset INTEGER,
  end_offset INTEGER,
  quoted_text_hash TEXT NOT NULL DEFAULT '',
  locator TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  created_at TEXT NOT NULL,
  CHECK(source_id IS NOT NULL OR artifact_id IS NOT NULL OR authority_id IS NOT NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS validation_results (
  validation_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  gate_name TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
  severity TEXT NOT NULL DEFAULT 'info',
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS certifications (
  certification_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  certification_type TEXT NOT NULL,
  status TEXT NOT NULL,
  validator TEXT NOT NULL,
  validation_result_id INTEGER NOT NULL REFERENCES validation_results(validation_result_id),
  evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  task_type TEXT NOT NULL,
  title TEXT NOT NULL,
  source_dependencies_json TEXT NOT NULL CHECK(json_valid(source_dependencies_json)),
  artifact_dependencies_json TEXT NOT NULL CHECK(json_valid(artifact_dependencies_json)),
  task_dependencies_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(task_dependencies_json)),
  matter_dependencies_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(matter_dependencies_json)),
  required_certifications_json TEXT NOT NULL CHECK(json_valid(required_certifications_json)),
  output_schema TEXT NOT NULL DEFAULT '',
  validation_gates_json TEXT NOT NULL CHECK(json_valid(validation_gates_json)),
  staleness_rules_json TEXT NOT NULL CHECK(json_valid(staleness_rules_json)),
  provider_policy_json TEXT NOT NULL CHECK(json_valid(provider_policy_json)),
  cost_limit_usd REAL,
  expected_value REAL NOT NULL DEFAULT 0,
  context_pack_id TEXT,
  human_attention_flags_json TEXT NOT NULL CHECK(json_valid(human_attention_flags_json)),
  blocked_reasons_json TEXT NOT NULL CHECK(json_valid(blocked_reasons_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS worker_attempts (
  worker_attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  lease_id TEXT,
  worker_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  output_path TEXT NOT NULL DEFAULT '',
  error_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(error_json))
) STRICT;

CREATE TABLE IF NOT EXISTS leases (
  lease_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS leases_one_active_per_task
ON leases(task_id)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS candidate_outputs (
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  lease_id TEXT,
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL,
  output_type TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  quarantined_reason TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE IF NOT EXISTS reducer_packets (
  reducer_packet_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidate_outputs(candidate_id) ON DELETE CASCADE,
  reducer_lease_id TEXT,
  decision TEXT NOT NULL,
  validation_result_id INTEGER REFERENCES validation_results(validation_result_id),
  canonical_artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
  dissent_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(dissent_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS council_runs (
  council_run_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  council_type TEXT NOT NULL,
  status TEXT NOT NULL,
  reducer_logic TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS council_votes (
  council_vote_id TEXT PRIMARY KEY,
  council_run_id TEXT NOT NULL REFERENCES council_runs(council_run_id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  candidate_id TEXT REFERENCES candidate_outputs(candidate_id) ON DELETE SET NULL,
  vote TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS context_packs (
  context_pack_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  task_id TEXT,
  pack_type TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  token_budget INTEGER NOT NULL,
  estimated_tokens INTEGER NOT NULL,
  cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
  cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
  sections_json TEXT NOT NULL CHECK(json_valid(sections_json)),
  created_at TEXT NOT NULL,
  UNIQUE(task_id, pack_type, fingerprint)
) STRICT;

CREATE TABLE IF NOT EXISTS compression_records (
  compression_id TEXT PRIMARY KEY,
  source_context_pack_id TEXT NOT NULL REFERENCES context_packs(context_pack_id) ON DELETE CASCADE,
  target_context_pack_id TEXT REFERENCES context_packs(context_pack_id) ON DELETE SET NULL,
  method TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  summary_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS tracked_files (
  tracked_file_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  absolute_path TEXT NOT NULL,
  relative_path TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  file_kind TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'needs_classification',
  provenance TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(matter_scope, absolute_path)
) STRICT;

CREATE TABLE IF NOT EXISTS index_rebuilds (
  index_rebuild_id TEXT PRIMARY KEY,
  index_name TEXT NOT NULL,
  matter_scope TEXT NOT NULL,
  status TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL DEFAULT '',
  output_fingerprint TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS search_index_entries (
  search_index_entry_id TEXT PRIMARY KEY,
  index_name TEXT NOT NULL,
  record_type TEXT NOT NULL,
  record_id TEXT NOT NULL,
  matter_scope TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  indexed_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(index_name, record_type, record_id, content_hash)
) STRICT;

CREATE TABLE IF NOT EXISTS legal_memories (
  memory_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'candidate',
  confidence REAL NOT NULL DEFAULT 0 CHECK(confidence >= 0 AND confidence <= 1),
  source_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_refs_json)),
  last_verified_at TEXT,
  stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
  staleness_trigger TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  matter_scope TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS session_messages (
  session_message_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL CHECK(json_valid(content_json)),
  context_pack_id TEXT REFERENCES context_packs(context_pack_id) ON DELETE SET NULL,
  provider_run_id TEXT REFERENCES provider_runs(provider_run_id) ON DELETE SET NULL,
  candidate_id TEXT REFERENCES candidate_outputs(candidate_id) ON DELETE SET NULL,
  reducer_packet_id TEXT REFERENCES reducer_packets(reducer_packet_id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS hook_invocations (
  hook_invocation_id TEXT PRIMARY KEY,
  hook_event TEXT NOT NULL,
  matter_scope TEXT NOT NULL,
  allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)),
  severity TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS provider_runs (
  provider_run_id TEXT PRIMARY KEY,
  task_id TEXT,
  run_id TEXT,
  stage TEXT NOT NULL DEFAULT '',
  requested_provider TEXT NOT NULL,
  requested_model TEXT NOT NULL,
  actual_provider TEXT NOT NULL,
  actual_model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
  cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  actual_cost_usd REAL,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  retries INTEGER NOT NULL DEFAULT 0,
  fallback_allowed INTEGER NOT NULL CHECK(fallback_allowed IN (0, 1)),
  fallback_policy_result TEXT NOT NULL,
  raw_usage_json TEXT NOT NULL CHECK(json_valid(raw_usage_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS budgets (
  budget_id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  limit_usd REAL NOT NULL,
  hard_stop INTEGER NOT NULL DEFAULT 1 CHECK(hard_stop IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(scope_type, scope_id)
) STRICT;

CREATE TABLE IF NOT EXISTS budget_entries (
  budget_entry_id TEXT PRIMARY KEY,
  budget_id TEXT NOT NULL REFERENCES budgets(budget_id) ON DELETE CASCADE,
  provider_run_id TEXT REFERENCES provider_runs(provider_run_id) ON DELETE SET NULL,
  amount_usd REAL NOT NULL,
  entry_type TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS human_attention (
  attention_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS external_action_blocks (
  block_id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS migration_reports (
  migration_report_id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  dry_run INTEGER NOT NULL CHECK(dry_run IN (0, 1)),
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS events_type_idx ON events(event_type, created_at);
CREATE INDEX IF NOT EXISTS tasks_status_stage_idx ON tasks(status, stage);
CREATE INDEX IF NOT EXISTS tasks_scope_stage_idx ON tasks(matter_scope, stage, status);
CREATE INDEX IF NOT EXISTS artifacts_trust_idx ON artifacts(trust_status, stale);
CREATE INDEX IF NOT EXISTS artifacts_type_stage_idx ON artifacts(artifact_type, stage);
CREATE INDEX IF NOT EXISTS sources_hash_idx ON sources(sha256);
CREATE INDEX IF NOT EXISTS certifications_subject_idx
ON certifications(subject_type, subject_id, certification_type, status);
CREATE INDEX IF NOT EXISTS validation_target_idx
ON validation_results(target_type, target_id, gate_name, passed);
CREATE INDEX IF NOT EXISTS provider_runs_task_idx ON provider_runs(task_id, created_at);
CREATE INDEX IF NOT EXISTS budget_entries_budget_idx ON budget_entries(budget_id, created_at);
CREATE INDEX IF NOT EXISTS citation_spans_target_idx ON citation_spans(target_type, target_id);
CREATE INDEX IF NOT EXISTS candidate_outputs_task_idx ON candidate_outputs(task_id, status);
CREATE INDEX IF NOT EXISTS tracked_files_status_idx ON tracked_files(status, file_kind);
CREATE INDEX IF NOT EXISTS search_index_entries_lookup_idx ON search_index_entries(index_name, record_type, record_id);
CREATE INDEX IF NOT EXISTS search_index_entries_scope_lookup_idx ON search_index_entries(index_name, matter_scope, record_type, record_id);
CREATE INDEX IF NOT EXISTS legal_memories_scope_type_idx ON legal_memories(matter_scope, type, status, stale);
CREATE INDEX IF NOT EXISTS sessions_scope_status_idx ON sessions(matter_scope, status, updated_at);
CREATE INDEX IF NOT EXISTS session_messages_session_idx ON session_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS hook_invocations_event_idx ON hook_invocations(hook_event, matter_scope, created_at);
"""
