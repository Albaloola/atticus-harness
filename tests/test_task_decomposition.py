from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from atticus.agents.decomposition import compact_decomposed_parent_if_needed, decompose_broad_task_if_needed
from atticus.core.policies import LegalStage
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.model_policy import default_smart_model_policy, smart_provider_policy_for_route
from atticus.reducer.reducer import _effective_source_dependencies
from atticus.scheduler.planner import select_runnable_tasks


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _add_sources(conn, matter_scope: str, count: int) -> list[str]:
    return [
        repo.add_source(
            conn,
            source_id=f"{matter_scope.upper()}-SRC-{index:04d}",
            matter_scope=matter_scope,
            path=f"/{matter_scope}/{index:04d}.pdf",
            sha256=f"{index:064x}"[-64:],
        )
        for index in range(1, count + 1)
    ]


def _add_extracted_text(conn, *, matter_scope: str, source_id: str, chars: int) -> str:
    return repo.add_artifact(
        conn,
        matter_scope=matter_scope,
        path=f"/derived/{source_id}.txt",
        artifact_type="extracted_text",
        title=f"Extracted text for {source_id}",
        content="x" * chars,
        source_ids=[source_id],
    )


def _certify_s2_foundation(conn, matter_scope: str) -> None:
    for gate in ("source_inventory", "extraction_coverage"):
        validation_id = repo.record_validation(
            conn,
            target_type="matter",
            target_id=matter_scope,
            gate_name=gate,
            passed=True,
            details={"test": "foundation satisfied"},
            matter_scope=matter_scope,
        )
        _ = repo.add_certification(
            conn,
            subject_type="matter",
            subject_id=matter_scope,
            certification_type=gate,
            validator="test",
            validation_result_id=validation_id,
        )


def _add_broad_evidence_task(conn, *, task_id: str, matter_scope: str, source_ids: list[str]) -> None:
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            title="Map all evidence",
            task_type="evidence_issue_map",
            instructions="Create an evidence-led issue map from the bounded source set.",
            matter_scope=matter_scope,
            stage=LegalStage.S2_EVIDENCE_REGISTRY,
            source_dependencies=source_ids,
            provider_policy={
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "runtime": "openrouter",
                "allow_fallback": False,
                "max_tokens": 16000,
                "estimated_cost_usd": 0.12,
            },
            expected_value=12.0,
        ),
    )


def _add_broad_production_task(conn, *, task_id: str, matter_scope: str, source_ids: list[str]) -> None:
    provider_policy = smart_provider_policy_for_route(
        default_smart_model_policy(),
        layer="worker",
        stage="S3",
        task_type="production_mapping",
        task_id=task_id,
        matter_scope=matter_scope,
        source_count=len(source_ids),
    )
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            title="Map production order",
            task_type="production_mapping",
            instructions="Create a source production crosswalk from the bounded source set.",
            matter_scope=matter_scope,
            stage=LegalStage.S3_PRODUCTION_STATUS,
            source_dependencies=source_ids,
            provider_policy=provider_policy,
            expected_value=12.0,
        ),
    )


def test_decompose_broad_task_creates_source_bundles_and_synthesis_parent(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_ids = _add_sources(conn, "alpha", 31)
        _add_broad_evidence_task(conn, task_id="parent-evidence-map", matter_scope="alpha", source_ids=source_ids)

        result = decompose_broad_task_if_needed(
            conn,
            task_id="parent-evidence-map",
            reason="OpenRouter response did not contain a JSON message: Unterminated string",
            write=True,
        )
        second = decompose_broad_task_if_needed(
            conn,
            task_id="parent-evidence-map",
            reason="OpenRouter response did not contain a JSON message: Unterminated string",
            write=True,
        )
        parent = cast(
            dict[str, object],
            conn.execute("SELECT * FROM tasks WHERE task_id = 'parent-evidence-map'").fetchone(),
        )
        children = conn.execute(
            """
            SELECT task_id, task_type, parent_task_id, source_dependencies_json,
                   provider_policy_json, task_provenance_json, instructions
            FROM tasks
            WHERE parent_task_id = 'parent-evidence-map'
            ORDER BY task_id
            """
        ).fetchall()
        event = conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'task.decomposed' AND matter_scope = 'alpha'"
        ).fetchone()

    child_ids = cast(list[str], result["child_task_ids"])
    parent_sources = json.loads(str(parent["source_dependencies_json"]))
    parent_dependencies = json.loads(str(parent["task_dependencies_json"]))
    parent_provenance = json.loads(str(parent["task_provenance_json"]))
    child_source_counts = [len(json.loads(str(child["source_dependencies_json"]))) for child in children]
    child_policy = json.loads(str(children[0]["provider_policy_json"]))
    child_provenance = json.loads(str(children[0]["task_provenance_json"]))

    assert result["applied"] is True
    assert second["reason"] == "already_decomposed"
    assert len(child_ids) == 6
    assert len(children) == 6
    assert parent["status"] == "blocked"
    assert parent_sources == []
    assert parent_dependencies == child_ids
    assert parent_provenance["source_bundle_decomposition"]["original_source_dependencies"] == source_ids
    assert child_source_counts == [6, 6, 6, 6, 6, 1]
    assert children[0]["task_type"] == "evidence_issue_map_bundle"
    assert children[0]["parent_task_id"] == "parent-evidence-map"
    assert child_policy["max_tokens"] == 4096
    assert child_policy["estimated_cost_usd"] == 0.02
    assert child_provenance["source_bundle_decomposition"]["role"] == "child"
    assert child_provenance["source_bundle_decomposition"]["estimated_source_tokens"] == 6
    assert parent_provenance["source_bundle_decomposition"]["target_source_tokens"] == 6000
    assert "Do not claim full-matter completion" in str(children[0]["instructions"])
    assert "Document ledger for this bundle" in str(children[0]["instructions"])
    assert "Synthesis retry after source-bundle decomposition" in str(parent["instructions"])
    assert event is not None


def test_scheduler_decomposes_broad_task_before_provider_dispatch(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_ids = _add_sources(conn, "alpha", 26)
        _certify_s2_foundation(conn, "alpha")
        _add_broad_evidence_task(conn, task_id="wide-evidence-map", matter_scope="alpha", source_ids=source_ids)

        first = select_runnable_tasks(conn, capacity=15)
        second = select_runnable_tasks(conn, capacity=15)
        parent = cast(
            dict[str, object],
            conn.execute("SELECT status, source_dependencies_json, task_dependencies_json FROM tasks WHERE task_id = 'wide-evidence-map'").fetchone(),
        )

    assert first == []
    assert parent["status"] == "blocked"
    assert json.loads(str(parent["source_dependencies_json"])) == []
    assert len(json.loads(str(parent["task_dependencies_json"]))) == 5
    assert [str(task["task_id"]) for task in second] == json.loads(str(parent["task_dependencies_json"]))


def test_decomposition_uses_local_token_estimates_for_bundle_shape(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_ids = _add_sources(conn, "alpha", 27)
        _add_extracted_text(conn, matter_scope="alpha", source_id=source_ids[0], chars=28_000)
        for source_id in source_ids[1:7]:
            _add_extracted_text(conn, matter_scope="alpha", source_id=source_id, chars=4_000)
        _add_broad_evidence_task(conn, task_id="token-aware-map", matter_scope="alpha", source_ids=source_ids)

        result = decompose_broad_task_if_needed(
            conn,
            task_id="token-aware-map",
            reason="pre_dispatch_token_budget",
            write=True,
        )
        children = conn.execute(
            """
            SELECT task_id, source_dependencies_json, task_provenance_json
            FROM tasks
            WHERE parent_task_id = 'token-aware-map'
            ORDER BY task_id
            """
        ).fetchall()

    child_sources = [json.loads(str(child["source_dependencies_json"])) for child in children]
    first_child_provenance = json.loads(str(children[0]["task_provenance_json"]))
    bundle_estimates = cast(list[dict[str, object]], result["bundle_token_estimates"])

    assert child_sources[0] == [source_ids[0]]
    assert first_child_provenance["source_bundle_decomposition"]["estimated_source_tokens"] == 7000
    assert bundle_estimates[0]["estimated_source_tokens"] == 7000
    assert any(len(bundle) > 1 for bundle in child_sources[1:])


def test_decomposition_recomputes_smart_child_policy_instead_of_copying_pro_parent(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_ids = _add_sources(conn, "alpha", 31)
        _add_broad_production_task(conn, task_id="production-parent", matter_scope="alpha", source_ids=source_ids)

        result = decompose_broad_task_if_needed(
            conn,
            task_id="production-parent",
            reason="pre_dispatch_token_budget",
            write=True,
        )
        parent_policy = json.loads(
            str(conn.execute("SELECT provider_policy_json FROM tasks WHERE task_id = 'production-parent'").fetchone()[0])
        )
        child_policies = [
            json.loads(str(row["provider_policy_json"]))
            for row in conn.execute(
                """
                SELECT provider_policy_json
                FROM tasks
                WHERE parent_task_id = 'production-parent'
                ORDER BY task_id
                """
            )
        ]

    assert result["applied"] is True
    assert parent_policy["model"] == "deepseek/deepseek-v4-pro"
    assert parent_policy["model_decision"]["decision_tier"] == "pro_orchestrator"
    assert child_policies
    assert {policy["model"] for policy in child_policies} == {"deepseek/deepseek-v4-flash"}
    assert {policy["model_decision"]["decision_tier"] for policy in child_policies} == {"flash_worker"}


def test_compact_decomposed_parent_requeues_bounded_synthesis_retry(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_ids = _add_sources(conn, "alpha", 31)
        _add_broad_evidence_task(conn, task_id="parent-evidence-map", matter_scope="alpha", source_ids=source_ids)
        _ = decompose_broad_task_if_needed(
            conn,
            task_id="parent-evidence-map",
            reason="pre_dispatch_token_budget",
            write=True,
        )
        repo.update_task_blocked(
            conn,
            "parent-evidence-map",
            ["OpenRouter response did not contain a JSON message: Unterminated string"],
        )

        result = compact_decomposed_parent_if_needed(
            conn,
            task_id="parent-evidence-map",
            reason="OpenRouter response did not contain a JSON message: Unterminated string",
            write=True,
        )
        parent = cast(
            dict[str, object],
            conn.execute(
                "SELECT status, blocked_reasons_json, instructions, provider_policy_json, task_provenance_json FROM tasks WHERE task_id = 'parent-evidence-map'"
            ).fetchone(),
        )

    provider_policy = json.loads(str(parent["provider_policy_json"]))
    provenance = json.loads(str(parent["task_provenance_json"]))

    assert result["applied"] is True
    assert parent["status"] == "queued"
    assert json.loads(str(parent["blocked_reasons_json"])) == []
    assert provider_policy["max_tokens"] == 2048
    assert "Synthesis retry after overlong provider JSON" in str(parent["instructions"])
    assert provenance["synthesis_compact_retry"]["max_tokens"] == 2048


def test_reducer_recovers_original_sources_for_decomposed_parent_graph_writes(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_ids = _add_sources(conn, "alpha", 31)
        _add_broad_production_task(conn, task_id="production-parent", matter_scope="alpha", source_ids=source_ids)
        _ = decompose_broad_task_if_needed(
            conn,
            task_id="production-parent",
            reason="pre_dispatch_token_budget",
            write=True,
        )
        parent = cast(
            dict[str, object],
            conn.execute("SELECT source_dependencies_json, task_provenance_json FROM tasks WHERE task_id = 'production-parent'").fetchone(),
        )

    assert json.loads(str(parent["source_dependencies_json"])) == []
    assert _effective_source_dependencies(parent) == source_ids
