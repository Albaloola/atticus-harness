from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.model_policy import (
    ModelPolicyError,
    default_smart_model_policy,
    load_model_routing_policy,
    normalize_legacy_provider_policy,
    provider_policy_for_route,
    smart_provider_policy_for_route,
)
from atticus.providers.policy import ProviderRequest, check_provider_policy
from atticus.providers.live_readiness import check_live_provider_policy
from atticus.reducer import reducer as reducer_module
from atticus.reducer.reducer import reduce_candidate
from atticus.scheduler.free_loop import run_free_loop_once
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION
from atticus.workers.work_order import build_work_order


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _json_mapping(text: str) -> Mapping[str, object]:
    value = json.loads(text)
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _mapping_value(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _v2_packet(task_id: str, *, proposed_tasks: list[dict[str, object]] | None = None, path: str = "candidate/parent.json") -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "done",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "finding",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [{"path": path, "artifact_type": "note", "stage": "S0", "title": "Note", "content": "{}"}],
        "proposed_tasks": proposed_tasks or [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def _text_value(conn: sqlite3.Connection, sql: str) -> str:
    row = conn.execute(sql).fetchone()
    assert row is not None
    value = row[0]
    assert value is not None
    return str(value)


def _scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    value = row[0]
    assert value is not None
    return int(str(value))


def _policy_file(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "model-policy.json"
    _ = path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return path


def _rich_policy_payload() -> dict[str, object]:
    return {
        "version": 1,
        "profiles": {
            "gpt55_codex": {
                "provider": "openai-codex",
                "model": "openai-codex/gpt-5.5",
                "runtime": "codex",
                "allow_fallback": False,
                "estimated_cost_usd": 0.0,
                "capabilities": ["legal_reasoning", "coding_agent"],
            },
            "deepseek_flash_or": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "runtime": "openrouter",
                "allow_fallback": False,
                "estimated_cost_usd": 0.01,
                "capabilities": ["triage", "indexing", "structured_extraction"],
            },
            "deepseek_pro_or": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "runtime": "openrouter",
                "allow_fallback": False,
                "estimated_cost_usd": 0.03,
                "capabilities": ["legal_reasoning", "hostile_review", "synthesis"],
            },
        },
        "pools": {
            "pro_then_flash": {
                "strategy": "fallback_loop",
                "profiles": ["deepseek_pro_or", "deepseek_flash_or"],
                "max_failed_cycles": 3,
                "cooldown_seconds": 4.0,
            },
        },
        "routes": {
            "default": "gpt55_codex",
            "layers": {
                "worker": "deepseek_flash_or",
                "subagent": "deepseek_flash_or",
                "reducer": "deepseek_pro_or",
                "hostile_review": "deepseek_pro_or",
                "verifier": "deepseek_pro_or",
            },
            "stages": {
                "S0": "deepseek_flash_or",
                "S1": "deepseek_flash_or",
                "S7": "deepseek_pro_or",
                "S8": "gpt55_codex",
            },
            "task_types": {
                "source_inventory": "deepseek_flash_or",
            },
            "task_ids": {
                "exact-task": "gpt55_codex",
            },
        },
    }


def test_legacy_flat_provider_policy_normalizes_without_routing_shape():
    policy = normalize_legacy_provider_policy(
        {"provider": "openai-codex", "model": "openai-codex/gpt-5.5", "allow_fallback": False, "estimated_cost_usd": 0.0}
    )
    resolved = provider_policy_for_route(policy, layer="worker", stage="S8", task_type="draft", task_id="t")

    assert resolved["provider"] == "openai-codex"
    assert resolved["model"] == "gpt-5.5"
    assert resolved["allow_fallback"] is False
    assert resolved["model_profile_id"] == "legacy"
    assert "model_routing" not in resolved


def test_legacy_flat_provider_policy_rejects_pool_like_keys():
    with pytest.raises(ModelPolicyError, match="openrouter_failover"):
        _ = load_model_routing_policy(
            {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "allow_fallback": True,
                "estimated_cost_usd": 0.01,
                "openrouter_failover": {"enabled": True, "models": ["deepseek/deepseek-v4-pro"]},
            }
        )


def test_model_policy_rejects_non_finite_numeric_fields():
    bad_profile = {
        "version": 1,
        "profiles": {
            "bad": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "runtime": "openrouter",
                "estimated_cost_usd": "NaN",
            }
        },
        "routes": {"default": "bad"},
    }
    bad_pool = _rich_policy_payload()
    cast(dict[str, object], cast(dict[str, object], bad_pool["pools"])["pro_then_flash"])["cooldown_seconds"] = "Infinity"

    with pytest.raises(ModelPolicyError, match="finite"):
        _ = load_model_routing_policy(bad_profile)
    with pytest.raises(ModelPolicyError, match="finite"):
        _ = load_model_routing_policy(bad_pool)


def test_model_policy_resolves_all_one_model_for_every_layer_stage_and_subagent():
    policy = load_model_routing_policy(
        {
            "version": 1,
            "profiles": {
                "all": {
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "runtime": "codex",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                }
            },
            "routes": {"default": "all"},
        }
    )

    for layer in ("worker", "reducer", "subagent", "verifier"):
        for stage in ("S0", "S7", "S8"):
            resolved = provider_policy_for_route(policy, layer=layer, stage=stage, task_type="anything", task_id="task")
            assert resolved["provider"] == "openai-codex"
            assert resolved["model"] == "gpt-5.5"
            assert resolved["runtime"] == "codex"


def test_model_policy_precedence_and_openrouter_deepseek_syntax():
    policy = load_model_routing_policy(_rich_policy_payload())

    worker = provider_policy_for_route(policy, layer="worker", stage="S8", task_type="draft", task_id="worker-task")
    reducer = provider_policy_for_route(policy, layer="reducer", stage="S0", task_type="draft", task_id="reducer-task")
    stage_s7 = provider_policy_for_route(policy, layer="", stage="S7", task_type="draft", task_id="stage-task")
    task_type = provider_policy_for_route(policy, layer="worker", stage="S0", task_type="source_inventory", task_id="type-task")
    exact = provider_policy_for_route(policy, layer="worker", stage="S0", task_type="source_inventory", task_id="exact-task")

    assert worker["model"] == "deepseek/deepseek-v4-flash"
    assert reducer["model"] == "deepseek/deepseek-v4-pro"
    assert stage_s7["model"] == "deepseek/deepseek-v4-pro"
    assert task_type["model"] == "deepseek/deepseek-v4-flash"
    assert "openrouter_failover" not in task_type
    assert exact["provider"] == "openai-codex"
    assert exact["model"] == "gpt-5.5"


def test_model_policy_rejects_unknown_direct_deepseek_and_unsafe_cross_provider_pool():
    unknown = {"version": 1, "profiles": {"bad": {"provider": "openrouter", "model": "unknown/model", "runtime": "openrouter"}}, "routes": {"default": "bad"}}
    direct = {"version": 1, "profiles": {"bad": {"provider": "deepseek", "model": "deepseek-v4-pro", "runtime": "deepseek"}}, "routes": {"default": "bad"}}
    cross = _rich_policy_payload()
    cast(dict[str, object], cross["pools"])["mixed"] = {"profiles": ["gpt55_codex", "deepseek_flash_or"], "allow_cross_provider_fallback": True}
    cast(dict[str, object], cross["routes"])["default"] = "mixed"

    for payload in (unknown, direct, cross):
        try:
            _ = load_model_routing_policy(payload)
        except ModelPolicyError:
            pass
        else:
            raise AssertionError("invalid model policy should fail closed")


def test_set_provider_policy_policy_file_dry_run_write_and_work_order_audit(tmp_path: Path):
    db_path = init_db(tmp_path)
    policy_file = _policy_file(tmp_path, _rich_policy_payload())
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="source-task",
                title="Source task",
                task_type="source_inventory",
                matter_scope="napier",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                status=TaskStatus.QUEUED,
            ),
        )

    assert cli_main(["set-provider-policy", "--db", str(db_path), "--matter", "napier", "--policy-file", str(policy_file)]) == 0
    with repo.db_connection(db_path) as conn:
        assert _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'source-task'")) == {}

    assert cli_main(["set-provider-policy", "--db", str(db_path), "--matter", "napier", "--policy-file", str(policy_file), "--write"]) == 0
    with repo.db_connection(db_path) as conn:
        task_policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'source-task'"))
        order = build_work_order(conn, task_id="source-task", persist_context=False)
        provider_runs = _scalar_int(conn, "SELECT COUNT(*) FROM provider_runs")

    assert task_policy["model_profile_id"] == "deepseek_flash_or"
    work_order_policy = _mapping_value(order.as_dict()["provider_policy"])
    assert _mapping_value(work_order_policy["resolved_model"])["profile_id"] == "deepseek_flash_or"
    assert provider_runs == 0


def test_model_policy_cli_validate_and_resolve_smokes(tmp_path: Path):
    policy_file = _policy_file(tmp_path, _rich_policy_payload())

    assert cli_main(["model-policy", "validate", "--policy-file", str(policy_file)]) == 0
    assert cli_main(
        [
            "model-policy",
            "resolve",
            "--policy-file",
            str(policy_file),
            "--stage",
            "S0",
            "--layer",
            "worker",
            "--task-type",
            "source_inventory",
            "--task-id",
            "type-task",
        ]
    ) == 0
    assert cli_main(
        [
            "model-policy",
            "decide",
            "--policy-file",
            str(policy_file),
            "--stage",
            "S7",
            "--layer",
            "hostile_review",
            "--task-type",
            "hostile_opponent_review",
            "--task-id",
            "hostile-task",
        ]
    ) == 0


def test_smart_model_policy_routes_flash_pro_and_codex_with_audit_fingerprints():
    policy = default_smart_model_policy()

    flash = smart_provider_policy_for_route(policy, layer="worker", stage="S0", task_type="source_inventory", task_id="source-task")
    pro = smart_provider_policy_for_route(policy, layer="hostile_review", stage="S7", task_type="hostile_opponent_review", task_id="hostile-task")
    codex = smart_provider_policy_for_route(policy, layer="worker", stage="S0", task_type="schema_migration", task_id="code-task")

    assert flash["model"] == "deepseek/deepseek-v4-flash"
    assert pro["model"] == "deepseek/deepseek-v4-pro"
    assert codex["provider"] == "openai-codex"
    assert codex["model"] == "gpt-5.5"
    for resolved in (flash, pro, codex):
        decision = _mapping_value(resolved["model_decision"])
        assert decision["policy_fingerprint"]
        assert decision["input_fingerprint"]
        assert resolved["model"] == decision["model"]


def test_smart_model_policy_routes_routine_bundle_children_by_base_task_type():
    policy = default_smart_model_policy()

    production_bundle = smart_provider_policy_for_route(
        policy,
        layer="worker",
        stage="S3",
        task_type="production_mapping_bundle",
        task_id="production-bundle",
        source_count=6,
    )
    authority_bundle = smart_provider_policy_for_route(
        policy,
        layer="worker",
        stage="S6",
        task_type="authority_map_bundle",
        task_id="authority-bundle",
        source_count=3,
    )

    assert production_bundle["model"] == "deepseek/deepseek-v4-flash"
    assert _mapping_value(production_bundle["model_decision"])["decision_tier"] == "flash_worker"
    assert authority_bundle["model"] == "deepseek/deepseek-v4-pro"
    assert _mapping_value(authority_bundle["model_decision"])["decision_tier"] == "pro_orchestrator"


def test_smart_model_policy_blocks_flash_override_for_pro_required_work():
    policy = default_smart_model_policy()

    resolved = smart_provider_policy_for_route(
        policy,
        layer="verifier",
        stage="S9",
        task_type="final_quality_gate",
        task_id="final-gate",
        operator_override="flash",
    )

    decision = _mapping_value(resolved["model_decision"])
    assert resolved["blocked"] is True
    assert resolved["provider"] == "blocked"
    assert decision["decision_tier"] == "blocked"
    assert "Flash downgrade blocked" in str(resolved["model_decision_reason"])


def test_smart_model_policy_keeps_codex_exact_route_only():
    policy = default_smart_model_policy()

    test_generation = smart_provider_policy_for_route(policy, layer="worker", stage="S0", task_type="test_generation", task_id="test-generation-task")
    repository_refactor = smart_provider_policy_for_route(policy, layer="worker", stage="S0", task_type="repository_refactor", task_id="repository-refactor-task")

    raw = policy.as_dict()
    routes = cast(dict[str, object], raw["routes"])
    routes["task_ids"] = {"repository-refactor-exact": "gpt55_codex"}
    exact_policy = load_model_routing_policy(raw)
    exact = smart_provider_policy_for_route(
        exact_policy,
        layer="worker",
        stage="S0",
        task_type="repository_refactor",
        task_id="repository-refactor-exact",
    )

    assert test_generation["provider"] == "openrouter"
    assert test_generation["model"] == "deepseek/deepseek-v4-flash"
    assert _mapping_value(test_generation["model_decision"])["decision_tier"] == "flash_worker"
    assert repository_refactor["provider"] == "openrouter"
    assert repository_refactor["model"] == "deepseek/deepseek-v4-flash"
    assert _mapping_value(repository_refactor["model_decision"])["decision_tier"] == "flash_worker"
    assert exact["provider"] == "openai-codex"
    assert exact["model"] == "gpt-5.5"
    assert _mapping_value(exact["model_decision"])["decision_tier"] == "codex_exact"


def test_smart_model_policy_does_not_override_explicit_reserved_anthropic_route():
    raw = default_smart_model_policy().as_dict()
    routes = cast(dict[str, object], raw["routes"])
    routes["task_ids"] = {"reserved-task": "anthropic_opus_reserved"}
    policy = load_model_routing_policy(raw)

    resolved = smart_provider_policy_for_route(policy, layer="worker", stage="S0", task_type="source_inventory", task_id="reserved-task")

    assert resolved["blocked"] is True
    assert resolved["provider"] == "blocked"
    assert "reserved Anthropic" in str(resolved["model_decision_reason"])


def test_held_openrouter_free_models_are_not_routable_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ATTICUS_ENABLE_HELD_OPENROUTER_MODELS", raising=False)
    monkeypatch.delenv("ATTICUS_ALLOW_HELD_MODELS_FOR_LIVE", raising=False)

    decision = check_provider_policy(ProviderRequest("openrouter", "qwen/qwen3-coder:free", allow_fallback=False))
    live_decision = check_live_provider_policy(
        {"provider": "openrouter", "model": "qwen/qwen3-coder:free", "allow_fallback": False},
        env={"OPENROUTER_API_KEY": "test-key"},
    )

    assert not decision.allowed
    assert "held OpenRouter model" in decision.reason
    assert not live_decision.allowed
    assert any("unknown or unsupported OpenRouter model" in reason for reason in live_decision.reasons)


def test_held_openrouter_models_require_separate_live_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATTICUS_ENABLE_HELD_OPENROUTER_MODELS", "1")
    monkeypatch.delenv("ATTICUS_ALLOW_HELD_MODELS_FOR_LIVE", raising=False)

    policy = load_model_routing_policy(
        {
            "version": 1,
            "profiles": {
                "held": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3-coder:free",
                    "runtime": "openrouter",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                }
            },
            "routes": {"default": "held"},
        }
    )
    resolved = provider_policy_for_route(policy)
    live_decision = check_live_provider_policy(resolved, env={"OPENROUTER_API_KEY": "test-key", "ATTICUS_ENABLE_HELD_OPENROUTER_MODELS": "1"})

    assert resolved["model"] == "qwen/qwen3-coder:free"
    assert not live_decision.allowed
    assert any("unknown or unsupported OpenRouter model" in reason for reason in live_decision.reasons)


def test_set_provider_policy_smart_defaults_writes_decision_metadata(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="hostile-smart-task",
                title="Hostile smart task",
                task_type="hostile_opponent_review",
                matter_scope="napier",
                stage=LegalStage.S7_HOSTILE_REVIEW,
                status=TaskStatus.QUEUED,
            ),
        )

    assert cli_main(["set-provider-policy", "--db", str(db_path), "--matter", "napier", "--smart-defaults", "--write"]) == 0
    with repo.db_connection(db_path) as conn:
        task_policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'hostile-smart-task'"))

    assert task_policy["model"] == "deepseek/deepseek-v4-pro"
    assert _mapping_value(task_policy["model_decision"])["decision_tier"] == "pro_orchestrator"


def test_model_policy_decide_reads_task_policy_from_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="hostile-db-decide",
                title="Hostile DB decide",
                task_type="hostile_opponent_review",
                matter_scope="napier",
                stage=LegalStage.S7_HOSTILE_REVIEW,
                status=TaskStatus.QUEUED,
            ),
        )

    assert cli_main(["set-provider-policy", "--db", str(db_path), "--matter", "napier", "--smart-defaults", "--write"]) == 0
    _ = capsys.readouterr()

    assert cli_main(["model-policy", "decide", "--db", str(db_path), "--task-id", "hostile-db-decide", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)

    resolved = _mapping_value(output["resolved"])
    assert resolved["model"] == "deepseek/deepseek-v4-pro"
    assert _mapping_value(resolved["model_decision"])["decision_tier"] == "pro_orchestrator"


def test_proposed_tasks_inherit_or_normalize_against_parent_model_routing_policy(tmp_path: Path):
    db_path = init_db(tmp_path)
    policy = provider_policy_for_route(
        load_model_routing_policy(_rich_policy_payload()),
        layer="worker",
        stage="S0",
        task_type="source_inventory",
        task_id="parent-task",
    )
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="parent-task",
                title="Parent",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy=policy,
            ),
        )
        lease_id = acquire_lease(conn, task_id="parent-task", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="parent-task",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_v2_packet(
                "parent-task",
                proposed_tasks=[
                    {
                        "task_id": "inherits-subagent",
                        "title": "Inherits",
                        "task_type": "followup",
                        "stage": "S7",
                        "matter_scope": "atticus",
                        "instructions": "Follow up with inherited policy.",
                    },
                    {
                        "task_id": "bad-policy-subagent",
                        "title": "Bad policy",
                        "task_type": "followup",
                        "stage": "S7",
                        "matter_scope": "atticus",
                        "instructions": "Follow up with normalized policy.",
                        "provider_policy": {
                            "provider": "openrouter",
                            "model": "not/in-policy",
                            "allow_fallback": True,
                            "estimated_cost_usd": 99.0,
                        },
                    },
                ],
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False)
        inherited = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'inherits-subagent'"))
        normalized = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'bad-policy-subagent'"))
        attention = _scalar_int(conn, "SELECT COUNT(*) FROM human_attention")

    assert result["reduced_candidates"] == [candidate_id]
    assert inherited["model"] == "deepseek/deepseek-v4-flash"
    assert inherited["model_profile_id"] == "deepseek_flash_or"
    assert normalized["model"] == "deepseek/deepseek-v4-flash"
    assert str(_mapping_value(normalized["model_policy_audit"])["reason"]).startswith("proposed task provider policy")
    assert attention == 0


def test_proposed_tasks_from_smart_parent_recompute_model_decision(tmp_path: Path):
    db_path = init_db(tmp_path)
    parent_policy = smart_provider_policy_for_route(
        default_smart_model_policy(),
        layer="worker",
        stage="S0",
        task_type="source_inventory",
        task_id="smart-parent",
    )
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="smart-parent",
                title="Smart parent",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy=parent_policy,
            ),
        )
        lease_id = acquire_lease(conn, task_id="smart-parent", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="smart-parent",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_v2_packet(
                "smart-parent",
                proposed_tasks=[
                    {
                        "task_id": "smart-hostile-followup",
                        "title": "Smart hostile followup",
                        "task_type": "hostile_opponent_review",
                        "stage": "S7",
                        "matter_scope": "atticus",
                        "instructions": "Run a hostile follow-up review.",
                    },
                ],
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False)
        followup_policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'smart-hostile-followup'"))

    decision = _mapping_value(followup_policy["model_decision"])
    assert result["reduced_candidates"] == [candidate_id]
    assert followup_policy["model"] == "deepseek/deepseek-v4-pro"
    assert followup_policy["model_profile_id"] == "deepseek_pro_or"
    assert decision["decision_tier"] == "pro_orchestrator"
    assert decision["model"] == followup_policy["model"]


def test_proposed_tasks_from_smart_parent_preserve_blocked_explicit_routes(tmp_path: Path):
    db_path = init_db(tmp_path)
    raw_policy = default_smart_model_policy().as_dict()
    routes = cast(dict[str, object], raw_policy["routes"])
    routes["task_ids"] = {"blocked-followup": "anthropic_opus_reserved"}
    smart_policy = load_model_routing_policy(raw_policy)
    parent_policy = smart_provider_policy_for_route(
        smart_policy,
        layer="worker",
        stage="S0",
        task_type="source_inventory",
        task_id="smart-parent",
    )
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="smart-parent",
                title="Smart parent",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy=parent_policy,
            ),
        )
        lease_id = acquire_lease(conn, task_id="smart-parent", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="smart-parent",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_v2_packet(
                "smart-parent",
                proposed_tasks=[
                    {
                        "task_id": "blocked-followup",
                        "title": "Blocked followup",
                        "task_type": "followup",
                        "stage": "S0",
                        "matter_scope": "atticus",
                        "instructions": "This explicit reserved route must remain blocked.",
                    },
                ],
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False)
        followup_policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'blocked-followup'"))

    decision = _mapping_value(followup_policy["model_decision"])
    assert result["reduced_candidates"] == [candidate_id]
    assert followup_policy["blocked"] is True
    assert followup_policy["provider"] == "blocked"
    assert decision["decision_tier"] == "blocked"
    assert "reserved Anthropic" in str(followup_policy["model_decision_reason"])


def test_run_free_loop_capacity_zero_does_not_make_provider_calls(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="queued", title="Queued", task_type="source_inventory"))
        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=True, runtime="openrouter")
        provider_runs = _scalar_int(conn, "SELECT COUNT(*) FROM provider_runs")
        leases = _scalar_int(conn, "SELECT COUNT(*) FROM leases")

    assert result["leased_tasks"] == []
    assert provider_runs == 0
    assert leases == 0


def test_proposed_tasks_without_parent_or_explicit_policy_do_not_default_to_free_model(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="parent-without-policy", title="Parent without policy", task_type="source_inventory"))
        lease_id = acquire_lease(conn, task_id="parent-without-policy", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="parent-without-policy",
            lease_id=lease_id,
            worker_id="worker-01",
            payload=_v2_packet(
                "parent-without-policy",
                proposed_tasks=[
                    {
                        "task_id": "missing-policy-followup",
                        "title": "Missing policy followup",
                        "task_type": "followup",
                        "stage": "S0",
                        "matter_scope": "atticus",
                        "instructions": "Follow up without a policy.",
                    },
                ],
            ),
        )

        result = run_free_loop_once(conn, output_dir=tmp_path / "out", capacity=0, execute_workers=False)
        followup_count = _scalar_int(conn, "SELECT COUNT(*) FROM tasks WHERE task_id = 'missing-policy-followup'")
        attention = _scalar_int(conn, "SELECT COUNT(*) FROM human_attention")

    assert result["reduced_candidates"] == [candidate_id]
    assert result["reduction_errors"] == []
    assert followup_count == 0
    assert attention == 1


def test_reducer_acceptance_rolls_back_if_proposed_task_import_crashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="atomic-parent", title="Atomic parent", task_type="source_inventory"))
        worker_lease_id = acquire_lease(conn, task_id="atomic-parent", worker_id="worker-01", dry_run=False)
        candidate_id = record_worker_result(
            conn,
            task_id="atomic-parent",
            lease_id=worker_lease_id,
            worker_id="worker-01",
            payload=_v2_packet(
                "atomic-parent",
                path="canonical/atomic-parent.json",
                proposed_tasks=[
                    {
                        "task_id": "would-crash-import",
                        "title": "Would crash import",
                        "task_type": "followup",
                        "stage": "S0",
                        "matter_scope": "atticus",
                        "instructions": "This import will be monkeypatched to fail.",
                        "provider_policy": {
                            "provider": "openrouter",
                            "model": "deepseek/deepseek-v4-flash",
                            "allow_fallback": False,
                            "estimated_cost_usd": 0.01,
                        },
                    },
                ],
            ),
        )
        reducer_lease_id = acquire_lease(conn, task_id="atomic-parent", worker_id="reducer-01", dry_run=False, lease_role="reducer")

        def crashing_import(conn: sqlite3.Connection, candidate: Mapping[str, object]) -> list[str]:
            del conn, candidate
            raise ValueError("simulated proposed task import crash")

        monkeypatch.setattr(reducer_module, "import_proposed_tasks_from_candidate", crashing_import)
        with pytest.raises(ValueError, match="simulated proposed task import crash"):
            _ = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease_id, dry_run=False)

        artifacts = _scalar_int(conn, "SELECT COUNT(*) FROM artifacts")
        reducer_packets = _scalar_int(conn, "SELECT COUNT(*) FROM reducer_packets")
        followups = _scalar_int(conn, "SELECT COUNT(*) FROM tasks WHERE task_id = 'would-crash-import'")
        candidate_status = _text_value(conn, "SELECT status FROM candidate_outputs WHERE candidate_id = '%s'" % candidate_id)

    assert artifacts == 0
    assert reducer_packets == 0
    assert followups == 0
    assert candidate_status == "candidate"


def test_live_policy_allows_explicit_openrouter_pool_but_not_silent_fallback():
    payload = _rich_policy_payload()
    cast(dict[str, object], cast(dict[str, object], payload["routes"])["task_types"])["source_inventory"] = "pro_then_flash"
    policy = provider_policy_for_route(load_model_routing_policy(payload), task_type="source_inventory")

    allowed = check_live_provider_policy(policy, env={"OPENROUTER_API_KEY": "test-key"})
    silent = check_live_provider_policy(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": True, "estimated_cost_usd": 0.01},
        env={"OPENROUTER_API_KEY": "test-key"},
    )

    assert allowed.allowed
    assert not silent.allowed
    assert any("fallback" in reason for reason in silent.reasons)
