from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from atticus.agents.context_sharing import build_cache_safe_context
from atticus.agents.coordinator import plan_adaptive_work
from atticus.agents.orchestrator import (
    orchestrator_plan_repair,
    orchestrator_tick,
    record_operator_signal,
    report_worker_failure_to_orchestrator,
)
from atticus.agents.subagents import SubagentSpec, create_subagent_task
from atticus.core.matter_profiles import (
    apply_matter_profile_adaptation,
    create_default_matter_profile,
    propose_matter_profile_adaptation,
    reset_matter_profile_to_default,
)
from atticus.core.policies import LegalStage
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.cache_observability import detect_prompt_cache_break, fingerprint_provider_policy
from atticus.providers.model_decision import ModelDecision
from atticus.retrieval.work_reuse import build_followup_context, explain_reuse_decision
from atticus.scheduler.planner import select_runnable_tasks
from atticus.work_runs import resume_work_run, start_work_run, summarize_reusable_work


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _decision(tier: str = "flash_worker") -> ModelDecision:
    return ModelDecision(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash" if tier == "flash_worker" else "deepseek/deepseek-v4-pro",
        runtime="openrouter",
        profile_id="deepseek_flash_or" if tier == "flash_worker" else "deepseek_pro_or",
        decision_reason="test decision",
        decision_tier=tier,
        fallback_allowed=False,
        required_human_review=False,
        policy_fingerprint="policy",
        input_fingerprint="input",
    )


def test_matter_profile_module_proposes_applies_resets_and_rejects_unsafe_changes(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        default_id = create_default_matter_profile(conn, "alpha")
        proposal = propose_matter_profile_adaptation(conn, "alpha", "inventory and extract sources", {})
        applied = apply_matter_profile_adaptation(conn, "alpha", proposal.as_dict(), write=True)
        beta_id = create_default_matter_profile(conn, "beta")
        reset = reset_matter_profile_to_default(conn, "alpha", write=True)

        alpha = repo.get_active_matter_profile(conn, matter_scope="alpha")
        beta = repo.get_active_matter_profile(conn, matter_scope="beta")

        unsafe = proposal.as_dict()
        stages = cast(list[dict[str, object]], unsafe["stages"])
        stages[8]["gate_policy"] = {"human_review_required": False}
        with pytest.raises(ValueError, match="human review"):
            apply_matter_profile_adaptation(conn, "alpha", unsafe, write=False)
        unsafe_missing_gates = proposal.as_dict()
        missing_gate_stages = cast(list[dict[str, object]], unsafe_missing_gates["stages"])
        missing_gate_stages[8] = {"stage": "S8", "enabled": True, "gate_policy": {"human_review_required": True}}
        with pytest.raises(ValueError, match="mandatory safety gates"):
            apply_matter_profile_adaptation(conn, "alpha", unsafe_missing_gates, write=False)

    assert applied["matter_profile_id"] != default_id
    assert beta is not None and beta["matter_profile_id"] == beta_id
    assert alpha is not None and alpha["matter_profile_id"] == reset["matter_profile_id"]
    assert alpha["matter_scope"] == "alpha"
    assert beta["matter_scope"] == "beta"


def test_orchestrator_failure_repair_and_tick_are_matter_scoped(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="alpha-task", title="Alpha", task_type="source_inventory", matter_scope="alpha"))
        repo.add_task(conn, TaskSpec(task_id="beta-task", title="Beta", task_type="source_inventory", matter_scope="beta"))

        dry = orchestrator_tick(conn, "alpha", 1, dry_run=True)
        event_id = report_worker_failure_to_orchestrator(conn, "alpha-task", "citation support missing")
        repair = orchestrator_plan_repair(conn, "alpha", event_id)
        write_tick = orchestrator_tick(conn, "alpha", 5, dry_run=False)
        alpha_leases = conn.execute("SELECT COUNT(*) AS n FROM leases l JOIN tasks t ON t.task_id = l.task_id WHERE t.matter_scope = 'alpha'").fetchone()
        beta_leases = conn.execute("SELECT COUNT(*) AS n FROM leases l JOIN tasks t ON t.task_id = l.task_id WHERE t.matter_scope = 'beta'").fetchone()

    assert dry["runnable_task_ids"] == ["alpha-task"]
    assert any(action["type"] == "verifier_task" for action in cast(list[dict[str, object]], repair["proposed_actions"]))
    assert write_tick["leased"][0]["task_id"] == "alpha-task"
    assert int(str(alpha_leases["n"])) == 1
    assert int(str(beta_leases["n"])) == 0


def test_scheduler_gate_block_signals_orchestrator_and_dedupes_attention(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="blocked-source-task",
                title="Blocked source",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=["missing-source"],
            ),
        )

        assert select_runnable_tasks(conn, capacity=5) == []
        assert select_runnable_tasks(conn, capacity=5) == []
        event = cast(
            dict[str, object],
            conn.execute(
                """
                SELECT event_type, payload_json
                FROM orchestrator_events
                WHERE matter_scope = 'alpha'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone(),
        )
        attention_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM human_attention
            WHERE target_id = 'blocked-source-task' AND status = 'open'
            """
        ).fetchone()
        tick = orchestrator_tick(conn, "alpha", 5, dry_run=True)
        orchestrator = repo.get_matter_orchestrator(conn, matter_scope="alpha")

    payload = cast(dict[str, object], json.loads(str(event["payload_json"])))
    repairs = cast(list[dict[str, object]], tick["blocked_repairs"])
    assert event["event_type"] == "orchestrator.task_blocked"
    assert payload["escalation_target"] == "worker_self_repair"
    assert payload["retry_policy"] == "no silent infinite retry"
    assert attention_count is not None and int(str(attention_count["n"])) == 1
    assert orchestrator is not None and orchestrator["status"] == "repair_required"
    assert repairs and repairs[0]["proposed_actions"][0]["type"] == "context_rebuild"


def test_operator_signal_is_routed_once_by_orchestrator_tick(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="alpha-task", title="Alpha", task_type="source_inventory", matter_scope="alpha"))

        dry = record_operator_signal(
            conn,
            "alpha",
            "directive",
            "Prioritize bank records before draft work",
            target_task_id="alpha-task",
            priority="high",
            write=False,
        )
        assert dry["dry_run"] is True
        assert dry["would_create_orchestrator"] is True
        assert conn.execute("SELECT COUNT(*) AS n FROM orchestrator_events").fetchone()["n"] == 0

        signal = record_operator_signal(
            conn,
            "alpha",
            "directive",
            "Prioritize bank records before draft work",
            target_task_id="alpha-task",
            priority="high",
            write=True,
        )
        dry_tick = orchestrator_tick(conn, "alpha", 0, dry_run=True)
        write_tick = orchestrator_tick(conn, "alpha", 0, dry_run=False)
        second_write_tick = orchestrator_tick(conn, "alpha", 0, dry_run=False)
        routed_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM orchestrator_events
            WHERE matter_scope = 'alpha' AND event_type = 'orchestrator.operator_signal_routed'
            """
        ).fetchone()
        master_count = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM events
            WHERE matter_scope = 'alpha' AND event_type IN (
                'master_orchestrator.operator_signal_received',
                'master_orchestrator.operator_signal_routed'
            )
            """
        ).fetchone()
        attention = conn.execute(
            """
            SELECT target_type, target_id, severity, reason
            FROM human_attention
            WHERE matter_scope = 'alpha'
            """
        ).fetchone()

    pending = cast(list[dict[str, object]], dry_tick["operator_signals"])
    routed = cast(list[dict[str, object]], write_tick["routed_operator_signals"])
    second_routed = cast(list[dict[str, object]], second_write_tick["routed_operator_signals"])
    assert signal["dry_run"] is False
    assert pending and pending[0]["operator_signal_event_id"] == signal["orchestrator_event_id"]
    assert routed and routed[0]["operator_signal_event_id"] == signal["orchestrator_event_id"]
    assert second_routed == []
    assert int(str(routed_count["n"])) == 1
    assert int(str(master_count["n"])) == 2
    assert attention["target_type"] == "task"
    assert attention["target_id"] == "alpha-task"
    assert attention["severity"] == "warning"
    assert "operator directive" in str(attention["reason"])


def test_operator_signal_rejects_wrong_matter_task(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="beta-task", title="Beta", task_type="source_inventory", matter_scope="beta"))
        with pytest.raises(ValueError, match="belongs to matter beta"):
            record_operator_signal(conn, "alpha", "attention", "Wrong matter", target_task_id="beta-task", write=True)


def test_s8_draft_does_not_wait_on_hostile_review_before_draft_exists(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="draft-after-foundation",
                title="Draft after foundation",
                task_type="draft_preparation",
                matter_scope="alpha",
                stage=LegalStage.S8_DRAFT_PREPARATION,
            ),
        )
        for gate in ("chronology_citations", "authority_map"):
            validation_id = repo.record_validation(
                conn,
                target_type="matter",
                target_id="alpha",
                gate_name=gate,
                passed=True,
                details={"test": "foundation satisfied"},
                matter_scope="alpha",
            )
            _ = repo.add_certification(
                conn,
                subject_type="matter",
                subject_id="alpha",
                certification_type=gate,
                validator="test",
                validation_result_id=validation_id,
            )

        runnable = select_runnable_tasks(conn, capacity=5)
        task = cast(dict[str, object], conn.execute("SELECT blocked_reasons_json FROM tasks WHERE task_id = 'draft-after-foundation'").fetchone())

    assert [row["task_id"] for row in runnable] == ["draft-after-foundation"]
    assert "hostile_review" not in str(task["blocked_reasons_json"])


def test_cache_context_subagent_and_followup_reuse_surfaces(tmp_path: Path):
    db_path = init_db(tmp_path)
    sections = [
        {"name": "stable_prefix", "content": "atticus"},
        {"name": "matter_posture", "content": {"matter_scope": "alpha"}},
        {"name": "required_output_schema", "content": {"schema": "worker_result_packet.v2"}},
        {"name": "available_tools", "content": ["read"]},
        {"name": "task_contract", "content": {"directive": "differs"}},
    ]
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="parent", title="Parent", task_type="source_inventory", matter_scope="alpha"))
        alpha_source = repo.add_source(conn, source_id="alpha-source", matter_scope="alpha", path="/alpha.pdf", sha256="a" * 64)
        _ = repo.add_source(conn, source_id="beta-source", matter_scope="beta", path="/beta.pdf", sha256="b" * 64)
        context_a = build_cache_safe_context(sections, model_decision=_decision().__dict__)
        context_b = build_cache_safe_context([*sections[:-1], {"name": "task_contract", "content": {"directive": "other"}}], model_decision=_decision().__dict__)
        spec = SubagentSpec(
            role="extractor",
            task_type="extraction_qa",
            matter_scope="alpha",
            parent_task_id="parent",
            model_decision=_decision(),
            allowed_source_ids=(alpha_source,),
            allowed_artifact_ids=(),
            tools=("read",),
            max_turns=1,
            async_allowed=False,
            cache_sharing_group_id="grp",
        )
        created = create_subagent_task(conn, spec, directive="check extraction", write=True)
        bad_spec = SubagentSpec(**{**spec.as_dict(), "allowed_source_ids": ("beta-source",), "model_decision": _decision()})
        with pytest.raises(ValueError, match="outside matter"):
            create_subagent_task(conn, bad_spec, directive="bad", write=False)
        pro_without_decision = SubagentSpec(
            **{
                **spec.as_dict(),
                "model_decision": ModelDecision(
                    provider="openrouter",
                    model="deepseek/deepseek-v4-pro",
                    runtime="openrouter",
                    profile_id="deepseek_flash_or",
                    decision_reason="bad manual model",
                    decision_tier="flash_worker",
                    fallback_allowed=False,
                    required_human_review=False,
                    policy_fingerprint="policy",
                    input_fingerprint="input",
                ),
            }
        )
        with pytest.raises(ValueError, match="decision layer selected Pro"):
            create_subagent_task(conn, pro_without_decision, directive="bad pro", write=False)
        recursive_spec = SubagentSpec(**{**spec.as_dict(), "parent_task_id": str(created["task"]["task_id"]), "model_decision": _decision()})
        with pytest.raises(ValueError, match="recursive subagent"):
            create_subagent_task(conn, recursive_spec, directive="recursive", write=False)

        run = start_work_run(conn, "alpha", "follow up on source")
        step_id = repo.record_work_run_step(
            conn,
            work_run_id=str(run["work_run_id"]),
            step_type="source_inventory",
            status="complete",
            input_fingerprint="source-goal",
        )
        _ = repo.record_work_reuse(conn, matter_scope="alpha", reused_from_step_id=step_id)
        reusable = summarize_reusable_work(conn, "alpha", "source")
        followup = build_followup_context(conn, "alpha", "source")
        explanation = explain_reuse_decision(
            conn,
            "alpha",
            [{"candidate_id": "candidate-only", "status": "candidate", "trusted_as_proof": False}],
        )
        resumed = resume_work_run(conn, str(run["resume_token"]))

    assert context_a.stable_fingerprint == context_b.stable_fingerprint
    assert created["task"]["candidate_only"] is True
    assert reusable["reusable_steps"]
    assert followup["rules"][2].startswith("candidate output")
    candidate_explanation = cast(list[dict[str, object]], explanation["reuse_explanations"])[0]
    assert candidate_explanation["reuse_allowed"] is False
    assert candidate_explanation["orientation_allowed"] is True
    assert resumed["ok"] is True


def test_cache_break_detection_and_adaptive_plan_are_explicit(tmp_path: Path):
    db_path = init_db(tmp_path)
    previous = {
        "model": "deepseek/deepseek-v4-flash",
        "system_fingerprint": "s",
        "tools_fingerprint": "t",
        "context_fingerprint": "c",
        "policy_fingerprint": "p",
        "cache_hit_tokens": 100,
    }
    current = {**previous, "cache_hit_tokens": 0}

    with repo.db_connection(db_path) as conn:
        source_plan = plan_adaptive_work(conn, matter_scope="alpha", goal="source inventory and extraction")
        plan = plan_adaptive_work(conn, matter_scope="alpha", goal="Draft a complaint", prior_work_state={"contradiction_count": 1})

    assert fingerprint_provider_policy({"model": "x"}) == fingerprint_provider_policy({"model": "x"})
    assert detect_prompt_cache_break(previous, current)["reason"].startswith("cache hit drop with unchanged")
    assert source_plan.selected_stages == ("S0", "S1")
    assert [task.task_type for task in source_plan.tasks] == ["source_inventory", "extraction_qa"]
    assert "S8" in plan.selected_stages
    assert "hostile_review" in plan.required_gates
    assert any(decision.decision_tier == "pro_orchestrator" for decision in plan.model_decisions)
