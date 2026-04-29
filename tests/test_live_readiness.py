from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from pathlib import Path
from typing import cast

from typing_extensions import override


import pytest

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.migration.import_old_run import import_candidates
from atticus.providers.deepseek import OPENROUTER_FREE_MODEL_ORDER
from atticus.providers import live_readiness
from atticus.providers.live_readiness import check_live_provider_policy, live_readiness_report, probe_live_openrouter
from atticus.providers.openrouter import OpenRouterClient, OpenRouterError
from atticus.scheduler import live_orchestrator
from atticus.scheduler.lease import acquire_lease
from atticus.scheduler.live_orchestrator import prepare_live_resume
from atticus.workers.runtime import WorkerExecutionBlocked, execute_openrouter_work_order
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def _object_dicts(value: object) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], value)


def _strings(value: object) -> list[str]:
    return [str(item) for item in cast(list[object], value)]


def _json_mapping(text: str) -> Mapping[str, object]:
    value = json.loads(text)
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(float(str(row["n"])))


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _provider_packet(task_id: str, *, summary: str = "Fake OpenRouter worker completed the bounded work order.") -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": summary,
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "candidate-only finding",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"candidate/{task_id}/openrouter_result.json",
                "artifact_type": "provider_result",
                "stage": "S0",
                "title": "provider result",
                "content": "{}",
            }
        ],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


class FakeOpenRouterClient:
    def __init__(self, *, content: object | None = None, model: str = "deepseek/deepseek-v4-pro", usage: dict[str, object] | None = None) -> None:
        self.content: object = content or _provider_packet("live-task")
        self.model: str = model
        self.usage: dict[str, object] = usage or {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}
        self.calls: list[dict[str, object]] = []

    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        return {
            "provider": "openrouter",
            "model": self.model,
            "content": self.content,
            "usage": dict(self.usage),
            "raw": {"id": "chatcmpl-test"},
        }


def test_live_provider_policy_requires_openrouter_key_and_no_fallback():
    blocked = check_live_provider_policy(
        {"provider": "deepseek", "model": "deepseek-v4-pro", "allow_fallback": False},
        env={"OPENROUTER_API_KEY": "sk-test"},
    )
    assert not blocked.allowed
    assert any("provider must be openrouter" in reason for reason in blocked.reasons)

    fallback = check_live_provider_policy(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": True},
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )
    assert not fallback.allowed
    assert any("fallback must be disabled" in reason for reason in fallback.reasons)

    missing_key = check_live_provider_policy(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        env={},
    )
    assert not missing_key.allowed
    assert any("OPENROUTER_API_KEY" in reason for reason in missing_key.reasons)

    allowed = check_live_provider_policy(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )
    assert allowed.allowed
    assert allowed.reasons == []


def test_live_provider_policy_allows_known_failover_model_list():
    model_a, model_b = OPENROUTER_FREE_MODEL_ORDER[:2]
    decision = check_live_provider_policy(
        {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "allow_fallback": False,
            "openrouter_failover": {"enabled": True, "models": [model_a, model_b]},
        },
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert decision.allowed
    assert decision.reasons == []


def test_env_enabled_failover_without_explicit_models_does_not_replace_deepseek_policy():
    decision = check_live_provider_policy(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        env={
            "OPENROUTER_API_KEY": "sk-test",
            "ATTICUS_ENABLE_LIVE_OPENROUTER": "1",
            "ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1",
        },
    )

    assert not decision.allowed
    assert any("explicit OpenRouter failover models" in reason for reason in decision.reasons)


def test_live_provider_policy_allows_explicit_env_failover_pool_with_allow_fallback():
    model_a, model_b = OPENROUTER_FREE_MODEL_ORDER[:2]
    decision = check_live_provider_policy(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": True},
        env={
            "OPENROUTER_API_KEY": "sk-test",
            "ATTICUS_ENABLE_LIVE_OPENROUTER": "1",
            "ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1",
            "ATTICUS_OPENROUTER_FAILOVER_MODELS": f"{model_a},{model_b}",
        },
    )

    assert decision.allowed
    assert decision.reasons == []


def test_live_provider_policy_rejects_malformed_failover_model_config():
    decision = check_live_provider_policy(
        {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "allow_fallback": False,
            "openrouter_failover": {"enabled": True, "models": {"bad": "shape"}},
        },
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert not decision.allowed
    assert any("models" in reason for reason in decision.reasons)


def test_openrouter_probe_requires_live_opt_in_before_client_call(monkeypatch: pytest.MonkeyPatch):
    class ExplodingClient:
        def __init__(self, *args: object, **kwargs: object):
            raise AssertionError("probe client must not be constructed without live opt-in")

    monkeypatch.setattr(live_readiness, "OpenRouterClient", ExplodingClient)
    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        env={"OPENROUTER_API_KEY": "sk-test"},
    )

    assert result["ok"] is False
    assert "ATTICUS_ENABLE_LIVE_OPENROUTER" in str(result["reason"])


def test_live_openrouter_probe_blocks_malformed_response_shapes_without_throwing():
    class ListResponseClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> list[str]:
            del model, messages, max_tokens, temperature
            return ["not", "a", "mapping"]

    class MalformedUsageClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            del model, messages, max_tokens, temperature
            return {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "content": {"ok": True, "probe": "atticus-live-openrouter"},
                "usage": ["not", "a", "mapping"],
            }

    policy = {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False}
    env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
    list_result = probe_live_openrouter(policy, client=ListResponseClient(), env=env)
    usage_result = probe_live_openrouter(policy, client=MalformedUsageClient(), env=env)

    assert not list_result["ok"]
    assert "JSON object" in str(list_result["reason"])
    assert not usage_result["ok"]
    assert "usage metadata" in str(usage_result["reason"])


def test_live_openrouter_probe_blocks_malformed_usage_scalars_from_injected_client():
    client = FakeOpenRouterClient(
        content={"ok": True, "probe": "atticus-live-openrouter"},
        usage={"prompt_tokens": True, "completion_tokens": 1},
    )

    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        client=client,
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert not result["ok"]
    assert "usage metadata is invalid" in str(result["reason"])
    assert result["provider_policy_result"] == "probe_failed"


def test_live_openrouter_probe_rotates_failover_models_with_injected_client():
    model_a, model_b = OPENROUTER_FREE_MODEL_ORDER[:2]

    class RotatingProbeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            del messages, max_tokens, temperature
            self.calls.append(model)
            if model == model_a:
                raise OpenRouterError("rate limit", status_code=429)
            return {
                "provider": "openrouter",
                "model": model,
                "content": {"ok": True, "probe": "atticus-live-openrouter"},
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    client = RotatingProbeClient()
    result = probe_live_openrouter(
        {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "allow_fallback": False,
            "openrouter_failover": {
                "enabled": True,
                "models": [model_a, model_b],
                "max_failed_cycles": 1,
                "cooldown_seconds": 0,
                "backoff_seconds": 0,
                "jitter_seconds": 0,
            },
        },
        client=client,
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert result["ok"] is True
    assert result["model"] == model_b
    assert result["requested_model"] == model_b
    assert result["configured_models"] == [model_a, model_b]
    assert client.calls == [model_a, model_b]


def test_live_openrouter_probe_fails_closed_on_unconfigured_requested_model():
    unconfigured = OPENROUTER_FREE_MODEL_ORDER[0]

    class ForgedRequestedModelClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            del model, messages, max_tokens, temperature
            return {
                "provider": "openrouter",
                "model": unconfigured,
                "requested_model": unconfigured,
                "content": {"ok": True, "probe": "atticus-live-openrouter"},
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        client=ForgedRequestedModelClient(),
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert result["ok"] is False
    assert result["requested_model"] == unconfigured
    assert result["provider_policy_result"] == "failed_closed"
    assert "not in configured model list" in str(result["reason"])


def test_live_openrouter_probe_allows_paid_deepseek_endpoint_provider_provenance():
    class EndpointProviderProbeClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            del messages, max_tokens, temperature
            return {
                "provider": "DeepSeek",
                "model": model,
                "requested_model": model,
                "content": {"ok": True, "probe": "atticus-live-openrouter"},
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        client=EndpointProviderProbeClient(),
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert result["ok"] is True
    assert result["provider"] == "DeepSeek"
    assert result["model"] == "deepseek/deepseek-v4-pro"
    assert result["provider_policy_result"] == "openrouter_endpoint_provenance"


def test_openrouter_runtime_requires_explicit_live_enable(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="live-task",
                title="Live task",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="live-task", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked):
            _ = execute_openrouter_work_order(
                conn,
                task_id="live-task",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=FakeOpenRouterClient(),
                env={"OPENROUTER_API_KEY": "sk-test"},
            )
        assert _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs") == 0
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'live-task'").fetchone())
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "ATTICUS_ENABLE_LIVE_OPENROUTER" in str(task["blocked_reasons_json"])


def test_openrouter_runtime_wrong_worker_fails_lease_and_blocks_task(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="wrong-worker-openrouter",
                title="Wrong worker OpenRouter",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="wrong-worker-openrouter", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="belongs to worker"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="wrong-worker-openrouter",
                lease_id=lease_id,
                worker_id="impostor-worker",
                output_dir=tmp_path / "out",
                client=FakeOpenRouterClient(),
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'wrong-worker-openrouter'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'wrong-worker-openrouter'")
        provider_run_count = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs WHERE task_id = 'wrong-worker-openrouter'")

    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "belongs to worker" in str(task["blocked_reasons_json"])
    assert candidate_count == 0
    assert provider_run_count == 0


def test_openrouter_runtime_cross_task_lease_mismatch_fails_actual_lease_before_client_call(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient()
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="openrouter-actual-lease",
                title="OpenRouter actual lease",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="openrouter-claimed-task",
                title="OpenRouter claimed task",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="openrouter-actual-lease", worker_id="openrouter-worker")

    with pytest.raises(WorkerExecutionBlocked, match="belongs to task openrouter-actual-lease"):
        with repo.db_connection(db_path) as conn:
            _ = execute_openrouter_work_order(
                conn,
                task_id="openrouter-claimed-task",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )

    with repo.db_connection(db_path) as conn:
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        actual_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'openrouter-actual-lease'").fetchone())
        provider_run_count = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")

    assert client.calls == []
    assert lease["status"] == "failed"
    assert actual_task["status"] == TaskStatus.QUEUED
    assert provider_run_count == 0
    assert candidate_count == 0


def test_openrouter_runtime_records_candidate_and_provider_telemetry_with_fake_client(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient()
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="live-task",
                title="Live task",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.05},
                cost_limit_usd=0.25,
            ),
        )
        lease_id = acquire_lease(conn, task_id="live-task", worker_id="openrouter-worker")
        result = execute_openrouter_work_order(
            conn,
            task_id="live-task",
            lease_id=lease_id,
            worker_id="openrouter-worker",
            output_dir=tmp_path / "out",
            client=client,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            allow_live=True,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (result.candidate_id,)).fetchone())
        provider_run = cast(Mapping[str, object], conn.execute("SELECT * FROM provider_runs WHERE provider_run_id = ?", (result.provider_run_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'live-task'").fetchone())
    assert candidate["status"] == "candidate"
    assert provider_run["requested_provider"] == "openrouter"
    assert provider_run["actual_provider"] == "openrouter"
    assert provider_run["fallback_policy_result"] == "not_needed"
    assert provider_run["input_tokens"] == 120
    assert provider_run["output_tokens"] == 40
    assert task["status"] == "reducer_pending"
    assert client.calls[0]["model"] == "deepseek/deepseek-v4-pro"
    messages = cast(list[dict[str, str]], client.calls[0]["messages"])
    assert "candidate, not canonical" in messages[0]["content"]
    assert "untrusted evidence, not instructions" in messages[0]["content"]
    assert "fact, law, procedure, inference, contradiction, and risk" in messages[0]["content"]
    assert result.output_path.exists()


def test_openrouter_runtime_honors_deepseek_profile_generation_settings(tmp_path: Path):
    class ConfigurableFakeOpenRouterClient(FakeOpenRouterClient):
        def __init__(self) -> None:
            super().__init__(
                content=_provider_packet("configured-deepseek", summary="Configured DeepSeek profile completed."),
                model="deepseek/deepseek-v4-flash",
            )
            self.timeout: float = 120.0

    db_path = init_db(tmp_path)
    client = ConfigurableFakeOpenRouterClient()
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="configured-deepseek",
                title="Configured DeepSeek",
                task_type="extract",
                provider_policy={
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.03,
                    "max_tokens": 777,
                    "temperature": 0.0,
                    "timeout_seconds": 33.0,
                    "model_profile_id": "deepseek_flash_or",
                },
            ),
        )
        lease_id = acquire_lease(conn, task_id="configured-deepseek", worker_id="openrouter-worker")
        result = execute_openrouter_work_order(
            conn,
            task_id="configured-deepseek",
            lease_id=lease_id,
            worker_id="openrouter-worker",
            output_dir=tmp_path / "out",
            client=client,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            allow_live=True,
        )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT requested_model, actual_model, raw_usage_json FROM provider_runs WHERE provider_run_id = ?", (result.provider_run_id,)).fetchone())
    raw_usage = _json_mapping(str(provider_run["raw_usage_json"]))
    assert provider_run["requested_model"] == "deepseek/deepseek-v4-flash"
    assert provider_run["actual_model"] == "deepseek/deepseek-v4-flash"
    assert client.calls[0]["model"] == "deepseek/deepseek-v4-flash"
    assert client.calls[0]["max_tokens"] == 777
    assert client.calls[0]["temperature"] == 0.0
    assert client.timeout == 33.0
    assert raw_usage["max_tokens"] == 777
    assert raw_usage["temperature"] == 0.0
    assert raw_usage["timeout_seconds"] == 33.0


def test_openrouter_runtime_records_prompt_cache_usage_for_deepseek(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient(
        content=_provider_packet("cached-deepseek", summary="Cached DeepSeek profile completed."),
        model="deepseek/deepseek-v4-pro",
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 20,
            "total_tokens": 1020,
            "prompt_tokens_details": {"cached_tokens": 700, "cache_write_tokens": 300},
        },
    )
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="cached-deepseek",
                title="Cached DeepSeek",
                task_type="extract",
                provider_policy={
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.03,
                },
            ),
        )
        lease_id = acquire_lease(conn, task_id="cached-deepseek", worker_id="openrouter-worker")
        result = execute_openrouter_work_order(
            conn,
            task_id="cached-deepseek",
            lease_id=lease_id,
            worker_id="openrouter-worker",
            output_dir=tmp_path / "out",
            client=client,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            allow_live=True,
        )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, raw_usage_json FROM provider_runs WHERE provider_run_id = ?", (result.provider_run_id,)).fetchone())

    raw_usage = _json_mapping(str(provider_run["raw_usage_json"]))
    assert provider_run["input_tokens"] == 1000
    assert provider_run["output_tokens"] == 20
    assert provider_run["cache_hit_tokens"] == 700
    assert provider_run["cache_miss_tokens"] == 300
    usage = cast(Mapping[str, object], raw_usage["usage"])
    details = cast(Mapping[str, object], usage["prompt_tokens_details"])
    assert details["cached_tokens"] == 700


def test_openrouter_paid_deepseek_allows_endpoint_provider_provenance(tmp_path: Path):
    class EndpointProviderClient(FakeOpenRouterClient):
        @override
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            response = super().chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
            response["provider"] = "DeepSeek"
            response["requested_model"] = model
            return response

    db_path = init_db(tmp_path)
    client = EndpointProviderClient(
        content=_provider_packet("endpoint-provider", summary="Endpoint provider provenance accepted.")
    )
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="endpoint-provider",
                title="Endpoint provider",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
            ),
        )
        lease_id = acquire_lease(conn, task_id="endpoint-provider", worker_id="openrouter-worker")
        result = execute_openrouter_work_order(
            conn,
            task_id="endpoint-provider",
            lease_id=lease_id,
            worker_id="openrouter-worker",
            output_dir=tmp_path / "out",
            client=client,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            allow_live=True,
        )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT actual_provider, actual_model, fallback_policy_result FROM provider_runs WHERE provider_run_id = ?", (result.provider_run_id,)).fetchone())

    assert provider_run["actual_provider"] == "DeepSeek"
    assert provider_run["actual_model"] == "deepseek/deepseek-v4-pro"
    assert provider_run["fallback_policy_result"] == "openrouter_endpoint_provenance"


def test_openrouter_runtime_records_final_failover_requested_model_in_telemetry(tmp_path: Path):
    model_a, model_b = OPENROUTER_FREE_MODEL_ORDER[:2]

    class RuntimeFailoverClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            del messages, max_tokens, temperature
            self.calls.append(model)
            if model == model_a:
                raise OpenRouterError("rate limit", status_code=429)
            return {
                "provider": "openrouter",
                "model": model,
                "content": _provider_packet("runtime-failover", summary="Fake OpenRouter worker completed failover work."),
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                "raw": {"id": "chatcmpl-failover"},
            }

    db_path = init_db(tmp_path)
    client = RuntimeFailoverClient()
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="runtime-failover",
                title="Runtime failover",
                task_type="extract",
                provider_policy={
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.0,
                    "openrouter_failover": {
                        "enabled": True,
                        "models": [model_a, model_b],
                        "max_failed_cycles": 1,
                        "cooldown_seconds": 0,
                        "backoff_seconds": 0,
                        "jitter_seconds": 0,
                    },
                },
            ),
        )
        lease_id = acquire_lease(conn, task_id="runtime-failover", worker_id="openrouter-worker")
        result = execute_openrouter_work_order(
            conn,
            task_id="runtime-failover",
            lease_id=lease_id,
            worker_id="openrouter-worker",
            output_dir=tmp_path / "out",
            client=client,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            allow_live=True,
        )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT requested_model, actual_model, fallback_allowed, fallback_policy_result, raw_usage_json FROM provider_runs WHERE provider_run_id = ?", (result.provider_run_id,)).fetchone())
    raw_usage = _json_mapping(str(provider_run["raw_usage_json"]))
    assert client.calls == [model_a, model_b]
    assert provider_run["requested_model"] == model_b
    assert provider_run["actual_model"] == model_b
    assert provider_run["fallback_allowed"] == 1
    assert provider_run["fallback_policy_result"] == "not_needed"
    assert raw_usage["requested_model"] == model_b
    assert raw_usage["configured_models"] == [model_a, model_b]
    events = cast(list[object], raw_usage["openrouter_failover_events"])
    assert any(isinstance(event, Mapping) and cast(Mapping[str, object], event).get("event") == "failover_advance" for event in events)


def test_openrouter_runtime_fails_closed_on_unconfigured_requested_model(tmp_path: Path):
    unconfigured = OPENROUTER_FREE_MODEL_ORDER[0]

    class ForgedRuntimeClient(FakeOpenRouterClient):
        @override
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            response = super().chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
            response["model"] = unconfigured
            response["requested_model"] = unconfigured
            return response

    db_path = init_db(tmp_path)
    client = ForgedRuntimeClient(model=unconfigured)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="forged-requested-model",
                title="Forged requested model",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.02},
            ),
        )
        lease_id = acquire_lease(conn, task_id="forged-requested-model", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="not in configured model list"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="forged-requested-model",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT requested_model, actual_model, fallback_policy_result, raw_usage_json FROM provider_runs WHERE task_id = 'forged-requested-model'").fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'forged-requested-model'").fetchone())
    raw_usage = _json_mapping(str(provider_run["raw_usage_json"]))
    assert provider_run["requested_model"] == unconfigured
    assert provider_run["actual_model"] == unconfigured
    assert provider_run["fallback_policy_result"] == "failed_closed"
    assert raw_usage["configured_models"] == ["deepseek/deepseek-v4-pro"]
    assert task["status"] == TaskStatus.BLOCKED


def test_openrouter_runtime_requires_valid_live_estimated_cost_after_lease(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient()
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="missing-cost-task",
                title="Missing cost task",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
            ),
        )
        lease_id = acquire_lease(conn, task_id="missing-cost-task", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="estimated_cost_usd"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="missing-cost-task",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'missing-cost-task'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'missing-cost-task'")

    assert lease["status"] == "failed"
    assert task["status"] == "blocked"
    assert "estimated_cost_usd" in str(task["blocked_reasons_json"])
    assert candidate_count == 0
    assert client.calls == []


def test_openrouter_runtime_fails_closed_when_response_metadata_missing(tmp_path: Path):
    class MissingMetadataClient(FakeOpenRouterClient):
        @override
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            response = super().chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
            _ = response.pop("provider")
            _ = response.pop("model")
            return response

    db_path = init_db(tmp_path)
    client = MissingMetadataClient()
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="missing-runtime-metadata",
                title="Missing runtime metadata",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.02},
            ),
        )
        lease_id = acquire_lease(conn, task_id="missing-runtime-metadata", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="provider/model metadata"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="missing-runtime-metadata",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'missing-runtime-metadata'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'missing-runtime-metadata'")
        provider_run = cast(Mapping[str, object], conn.execute("SELECT actual_provider, actual_model FROM provider_runs WHERE task_id = 'missing-runtime-metadata'").fetchone())
        budget_entry = cast(Mapping[str, object], conn.execute("SELECT amount_usd FROM budget_entries").fetchone())
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "provider/model metadata" in str(task["blocked_reasons_json"])
    assert candidate_count == 0
    assert provider_run["actual_provider"] == "missing"
    assert provider_run["actual_model"] == "missing"
    assert budget_entry["amount_usd"] == 0.02


def test_openrouter_runtime_charges_budget_when_response_content_is_non_object(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient(content=["not", "a", "candidate", "object"])
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="bad-content-runtime",
                title="Bad content runtime",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.03},
            ),
        )
        lease_id = acquire_lease(conn, task_id="bad-content-runtime", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="JSON object candidate packet"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="bad-content-runtime",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'bad-content-runtime'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'bad-content-runtime'")
        provider_run_count = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs WHERE task_id = 'bad-content-runtime'")
        budget_entry = cast(Mapping[str, object], conn.execute("SELECT amount_usd FROM budget_entries").fetchone())
    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "JSON object candidate packet" in str(task["blocked_reasons_json"])
    assert candidate_count == 0
    assert provider_run_count == 1
    assert budget_entry["amount_usd"] == 0.03


def test_openrouter_runtime_charges_budget_when_provider_response_is_unusable(tmp_path: Path):
    class UnusableResponseClient(FakeOpenRouterClient):
        @override
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature})
            raise OpenRouterError("OpenRouter usage metadata must be a JSON object")

    db_path = init_db(tmp_path)
    client = UnusableResponseClient()
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="unusable-provider-response",
                title="Unusable provider response",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.04},
            ),
        )
        lease_id = acquire_lease(conn, task_id="unusable-provider-response", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="provider call failed"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="unusable-provider-response",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'unusable-provider-response'").fetchone())
        provider_run = cast(Mapping[str, object], conn.execute("SELECT fallback_policy_result FROM provider_runs WHERE task_id = 'unusable-provider-response'").fetchone())
        budget_entry = cast(Mapping[str, object], conn.execute("SELECT amount_usd FROM budget_entries").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'unusable-provider-response'")

    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert provider_run["fallback_policy_result"] == "provider_error"
    assert budget_entry["amount_usd"] == 0.04
    assert candidate_count == 0
    assert len(client.calls) == 1


def test_openrouter_client_requires_provider_model_metadata_for_fallback_detection():
    raw = {
        "choices": [{"message": {"content": json.dumps({"ok": True, "probe": "atticus-live-openrouter"})}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    test_api_key = "sk-" + "test"
    client = OpenRouterClient(api_key=test_api_key, transport=lambda req, timeout: json.dumps(raw).encode("utf-8"))

    with pytest.raises(OpenRouterError, match="missing provider/model metadata"):
        _ = client.chat_json(model="deepseek/deepseek-v4-pro", messages=[{"role": "user", "content": "{}"}], max_tokens=8, temperature=0.0)


def test_openrouter_client_blocks_malformed_usage_metadata():
    for malformed_usage in (["not", "a", "mapping"], None, 0, False):
        raw = {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "choices": [{"message": {"content": json.dumps({"ok": True, "probe": "atticus-live-openrouter"})}}],
            "usage": malformed_usage,
        }
        test_api_key = "sk-" + "test"
        client = OpenRouterClient(api_key=test_api_key, transport=lambda req, timeout, raw=raw: json.dumps(raw).encode("utf-8"))

        with pytest.raises(OpenRouterError, match="usage metadata"):
            _ = client.chat_json(model="deepseek/deepseek-v4-pro", messages=[{"role": "user", "content": "{}"}], max_tokens=8, temperature=0.0)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": [1], "completion_tokens": 1},
        {"prompt_tokens": {"n": 1}, "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 1.5, "completion_tokens": 1},
        {"prompt_tokens": float("nan"), "completion_tokens": 1},
        {"prompt_tokens": float("inf"), "completion_tokens": 1},
        {"prompt_tokens": "1.5", "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": -2},
    ],
)
def test_openrouter_client_blocks_malformed_usage_token_scalars(usage: dict[str, object]) -> None:
    raw = {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "choices": [{"message": {"content": json.dumps({"ok": True, "probe": "atticus-live-openrouter"})}}],
        "usage": usage,
    }
    test_api_key = "sk-" + "test"
    client = OpenRouterClient(api_key=test_api_key, transport=lambda req, timeout: json.dumps(raw).encode("utf-8"))

    with pytest.raises(OpenRouterError, match="usage field"):
        _ = client.chat_json(model="deepseek/deepseek-v4-pro", messages=[{"role": "user", "content": "{}"}], max_tokens=8, temperature=0.0)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": ["bad"]},
        {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": True}},
        {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cache_write_tokens": -1}},
    ],
)
def test_openrouter_client_blocks_malformed_cache_usage_details(usage: dict[str, object]) -> None:
    raw = {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "choices": [{"message": {"content": json.dumps({"ok": True, "probe": "atticus-live-openrouter"})}}],
        "usage": usage,
    }
    test_api_key = "sk-" + "test"
    client = OpenRouterClient(api_key=test_api_key, transport=lambda req, timeout: json.dumps(raw).encode("utf-8"))

    with pytest.raises(OpenRouterError, match="cache usage"):
        _ = client.chat_json(model="deepseek/deepseek-v4-pro", messages=[{"role": "user", "content": "{}"}], max_tokens=8, temperature=0.0)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": [1], "completion_tokens": 1},
        {"prompt_tokens": {"n": 1}, "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 1.5, "completion_tokens": 1},
        {"prompt_tokens": float("nan"), "completion_tokens": 1},
        {"prompt_tokens": float("inf"), "completion_tokens": 1},
        {"prompt_tokens": "1.5", "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": -2},
    ],
)
def test_openrouter_runtime_blocks_malformed_usage_scalars_after_dispatch_with_telemetry(tmp_path: Path, usage: dict[str, object]) -> None:
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient(usage=usage)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="bad-usage-runtime",
                title="Bad usage runtime",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.06},
            ),
        )
        lease_id = acquire_lease(conn, task_id="bad-usage-runtime", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked, match="usage metadata is invalid"):
            _ = execute_openrouter_work_order(
                conn,
                task_id="bad-usage-runtime",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'bad-usage-runtime'").fetchone())
        provider_run = cast(Mapping[str, object], conn.execute("SELECT fallback_policy_result, input_tokens, output_tokens FROM provider_runs WHERE task_id = 'bad-usage-runtime'").fetchone())
        budget_entry = cast(Mapping[str, object], conn.execute("SELECT amount_usd FROM budget_entries").fetchone())
        attempt = cast(Mapping[str, object], conn.execute("SELECT status, error_json FROM worker_attempts WHERE task_id = 'bad-usage-runtime'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE task_id = 'bad-usage-runtime'")

    assert lease["status"] == "failed"
    assert task["status"] == TaskStatus.BLOCKED
    assert "usage metadata is invalid" in str(task["blocked_reasons_json"])
    assert provider_run["fallback_policy_result"] == "failed_closed"
    assert provider_run["input_tokens"] == 0
    assert provider_run["output_tokens"] == 0
    assert budget_entry["amount_usd"] == 0.06
    assert attempt["status"] == "failed"
    assert candidate_count == 0


def test_openrouter_runtime_accepts_whole_number_usage_strings(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient(
        content=_provider_packet("string-usage-runtime"),
        usage={"prompt_tokens": "12", "completion_tokens": "4", "total_tokens": "16"},
    )
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="string-usage-runtime",
                title="String usage runtime",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="string-usage-runtime", worker_id="openrouter-worker")
        result = execute_openrouter_work_order(
            conn,
            task_id="string-usage-runtime",
            lease_id=lease_id,
            worker_id="openrouter-worker",
            output_dir=tmp_path / "out",
            client=client,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            allow_live=True,
        )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT input_tokens, output_tokens FROM provider_runs WHERE provider_run_id = ?", (result.provider_run_id,)).fetchone())
    assert provider_run["input_tokens"] == 12
    assert provider_run["output_tokens"] == 4


def test_openrouter_runtime_policy_preflight_fails_lease_and_blocks_task(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="fallback-task",
                title="Fallback task",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": True, "estimated_cost_usd": 0.0},
            ),
        )
        lease_id = acquire_lease(conn, task_id="fallback-task", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked):
            _ = execute_openrouter_work_order(
                conn,
                task_id="fallback-task",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=FakeOpenRouterClient(),
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'fallback-task'").fetchone())
    assert lease["status"] == "failed"
    assert task["status"] == "blocked"
    assert any("fallback must be disabled" in reason for reason in _strings(json.loads(str(task["blocked_reasons_json"]))))


def test_openrouter_runtime_fails_closed_on_provider_model_fallback(tmp_path: Path):
    db_path = init_db(tmp_path)
    client = FakeOpenRouterClient(model="deepseek/deepseek-v4-flash")
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="matter", scope_id="atticus", limit_usd=1.0)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="live-task",
                title="Live task",
                task_type="extract",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.05},
            ),
        )
        lease_id = acquire_lease(conn, task_id="live-task", worker_id="openrouter-worker")
        with pytest.raises(WorkerExecutionBlocked):
            _ = execute_openrouter_work_order(
                conn,
                task_id="live-task",
                lease_id=lease_id,
                worker_id="openrouter-worker",
                output_dir=tmp_path / "out",
                client=client,
                env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
                allow_live=True,
            )
        provider_run = cast(Mapping[str, object], conn.execute("SELECT fallback_policy_result FROM provider_runs").fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'live-task'").fetchone())
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")
        budget_entry = cast(Mapping[str, object], conn.execute("SELECT amount_usd FROM budget_entries").fetchone())
    assert provider_run["fallback_policy_result"] == "failed_closed"
    assert task["status"] == "blocked"
    assert any("fallback" in reason for reason in _strings(json.loads(str(task["blocked_reasons_json"]))))
    assert candidate_count == 0
    assert budget_entry["amount_usd"] == 0.05


def test_live_readiness_report_blocks_until_foundation_and_budget_are_safe(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="late-stage",
                title="Late stage",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.10},
                status=TaskStatus.QUEUED,
            ),
        )
        report = live_readiness_report(conn, capacity=15, env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"})

    assert not report["ready"]
    assert report["capacity_requested"] == 15
    assert report["capacity_safe"] == 0
    assert report["runnable_task_ids"] == []
    assert any("missing certification" in reason for item in _object_dicts(report["blocked_tasks"]) for reason in _strings(item["reasons"]))


def test_live_readiness_and_resume_reconsider_previously_blocked_safe_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="blocked-safe-live",
                title="Blocked safe live",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.BLOCKED,
            ),
        )
        _ = conn.execute(
            "UPDATE tasks SET blocked_reasons_json = ? WHERE task_id = ?",
            (json.dumps(["old transient blocker"]), "blocked-safe-live"),
        )
        report = live_readiness_report(conn, capacity=15, env=env)
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env=env,
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
            worker_prefix="blocked-live",
        )
        task = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'blocked-safe-live'").fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT worker_id, status FROM leases WHERE task_id = 'blocked-safe-live'").fetchone())
    assert report["ready"]
    assert report["runnable_task_ids"] == ["blocked-safe-live"]
    assert plan["ready"]
    assert plan["runnable_task_ids"] == ["blocked-safe-live"]
    assert task["status"] == TaskStatus.LEASED
    assert task["blocked_reasons_json"] == "[]"
    assert lease["status"] == "active"
    assert str(lease["worker_id"]).startswith("blocked-live-")


def test_live_readiness_report_blocks_malformed_provider_policy_without_throwing(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="corrupt-policy",
                title="Corrupt policy",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                status=TaskStatus.QUEUED,
            ),
        )
        _ = conn.execute("PRAGMA ignore_check_constraints = ON")
        _ = conn.execute("UPDATE tasks SET provider_policy_json = ? WHERE task_id = ?", ("{not valid json", "corrupt-policy"))
        report = live_readiness_report(conn, capacity=15, env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"})

    assert not report["ready"]
    assert report["runnable_task_ids"] == []
    blocked_tasks = _object_dicts(report["blocked_tasks"])
    assert blocked_tasks[0]["task_id"] == "corrupt-policy"
    assert any("malformed provider policy" in reason for reason in _strings(blocked_tasks[0]["reasons"]))


def test_live_readiness_and_resume_block_malformed_gate_metadata_without_throwing(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="corrupt-gates",
                title="Corrupt gate metadata",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        _ = conn.execute("PRAGMA ignore_check_constraints = ON")
        _ = conn.execute("UPDATE tasks SET source_dependencies_json = ? WHERE task_id = ?", ("{not valid json", "corrupt-gates"))
        env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
        report = live_readiness_report(conn, capacity=15, env=env)
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env=env,
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
        )

    assert not report["ready"]
    assert report["runnable_task_ids"] == []
    blocked_tasks = _object_dicts(report["blocked_tasks"])
    assert blocked_tasks[0]["task_id"] == "corrupt-gates"
    assert any("malformed task gate metadata" in reason for reason in _strings(blocked_tasks[0]["reasons"]))
    assert not plan["ready"]
    assert plan["runnable_task_ids"] == []
    assert plan["leases"] == []
    assert any("malformed task gate metadata" in reason for item in _object_dicts(plan["blocked_tasks"]) for reason in _strings(item["reasons"]))


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("source_dependencies_json", "{}", "source_dependencies_json must be a JSON array"),
        ("source_dependencies_json", "123", "source_dependencies_json must be a JSON array"),
        ("source_dependencies_json", "false", "source_dependencies_json must be a JSON array"),
        ("artifact_dependencies_json", "{}", "artifact_dependencies_json must be a JSON array"),
        ("artifact_dependencies_json", "123", "artifact_dependencies_json must be a JSON array"),
        ("artifact_dependencies_json", "false", "artifact_dependencies_json must be a JSON array"),
        ("task_dependencies_json", "{}", "task_dependencies_json must be a JSON array"),
        ("task_dependencies_json", "123", "task_dependencies_json must be a JSON array"),
        ("task_dependencies_json", "false", "task_dependencies_json must be a JSON array"),
        ("required_certifications_json", "{}", "required_certifications_json must be a JSON array"),
        ("required_certifications_json", "123", "required_certifications_json must be a JSON array"),
        ("required_certifications_json", "false", "required_certifications_json must be a JSON array"),
        ("required_certifications_json", "[123]", "required_certifications_json[0]"),
        ("required_certifications_json", '[{"subject_type":"matter"}]', "malformed certification requirement"),
    ],
)
def test_live_readiness_blocks_json_valid_corrupt_gate_metadata_shapes_without_leasing(tmp_path: Path, field: str, value: str, expected: str) -> None:
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="shape-corrupt-gates",
                title="Shape corrupt gates",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        _ = conn.execute("PRAGMA ignore_check_constraints = ON")
        _ = conn.execute(f"UPDATE tasks SET {field} = ? WHERE task_id = ?", (value, "shape-corrupt-gates"))
        env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
        report = live_readiness_report(conn, capacity=15, env=env)
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env=env,
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
        )
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases")

    assert not report["ready"]
    assert report["runnable_task_ids"] == []
    assert any(expected in reason for item in _object_dicts(report["blocked_tasks"]) for reason in _strings(item["reasons"]))
    assert not plan["ready"]
    assert plan["leases"] == []
    assert lease_count == 0
    assert any(expected in reason for item in _object_dicts(plan["blocked_tasks"]) for reason in _strings(item["reasons"]))


def test_live_readiness_report_blocks_invalid_estimated_cost_without_throwing(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="negative-cost",
                title="Negative cost",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": -0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        report = live_readiness_report(conn, capacity=15, env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"})

    assert not report["ready"]
    assert report["runnable_task_ids"] == []
    blocked_tasks = _object_dicts(report["blocked_tasks"])
    assert blocked_tasks[0]["task_id"] == "negative-cost"
    assert any("estimated_cost_usd" in reason for reason in _strings(blocked_tasks[0]["reasons"]))


def test_legacy_import_creates_openrouter_only_validation_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "legacy"
    workspace.mkdir()
    _ = (workspace / "source_index.json").write_text(json.dumps({"sources": ["a.pdf"]}), encoding="utf-8")

    with repo.db_connection(db_path) as conn:
        result = import_candidates(conn, workspace=workspace, dry_run=False)
        task = cast(Mapping[str, object], conn.execute("SELECT provider_policy_json, stage FROM tasks WHERE task_type = 'legacy_validation'").fetchone())
    provider_policy = _json_mapping(str(task["provider_policy_json"]))
    assert result.validation_tasks_created == 1
    assert provider_policy["provider"] == "openrouter"
    assert provider_policy["model"] == "deepseek/deepseek-v4-flash"
    assert provider_policy["allow_fallback"] is False
    assert task["stage"] == "S0"


def test_openrouter_probe_fails_closed_on_reported_fallback_model():
    client = FakeOpenRouterClient(
        content={"ok": True, "probe": "atticus-live-openrouter"},
        model="deepseek/deepseek-v4-flash",
    )

    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        client=client,
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert not result["ok"]
    assert result["provider_policy_result"] == "failed_closed"
    assert "fallback" in str(result["reason"])


def test_openrouter_probe_requires_literal_boolean_true():
    client = FakeOpenRouterClient(content={"ok": "false", "probe": "atticus-live-openrouter"})

    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        client=client,
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert result["ok"] is False
    assert result["provider_policy_result"] == "not_needed"
    assert "literal ok=true" in str(result["reason"])


def test_openrouter_probe_fails_closed_when_probe_metadata_missing():
    class MissingMetadataClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
            del model, messages, max_tokens, temperature
            return {"content": {"ok": True, "probe": "atticus-live-openrouter"}, "usage": {}}

    result = probe_live_openrouter(
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False},
        client=MissingMetadataClient(),
        env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
    )

    assert result["ok"] is False
    assert result["provider_policy_result"] == "probe_failed"
    assert "missing provider/model metadata" in str(result["reason"])


def test_live_orchestrator_requires_successful_probe_before_leasing(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="safe-s0",
                title="Safe S0",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": False, "reason": "probe failed"},
            write_leases=True,
        )
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases")

    assert not plan["ready"]
    assert plan["leases"] == []
    assert lease_count == 0
    assert any("probe failed" in reason for reason in _strings(plan["reasons"]))


def test_live_orchestrator_rejects_truthy_non_boolean_probe_ok(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="truthy-probe-task",
                title="Truthy probe task",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": "false", "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
        )
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases")

    assert not plan["ready"]
    assert plan["leases"] == []
    assert lease_count == 0
    assert any("literal ok=true" in reason for reason in _strings(plan["reasons"]))


def test_live_orchestrator_refuses_probe_model_that_does_not_match_runnable_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="safe-pro-task",
                title="Safe pro task",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
            write_leases=True,
        )
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases")

    assert not plan["ready"]
    assert lease_count == 0
    assert any("probe does not match" in reason for reason in _strings(plan["reasons"]))


def test_live_orchestrator_filters_probe_mismatches_and_leases_matching_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        for task_id, model, value in (
            ("flash-task", "deepseek/deepseek-v4-flash", 100),
            ("pro-task", "deepseek/deepseek-v4-pro", 50),
        ):
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=task_id,
                    title=task_id,
                    task_type="source_inventory",
                    stage=LegalStage.S0_SOURCE_INVENTORY,
                    provider_policy={"provider": "openrouter", "model": model, "allow_fallback": False, "estimated_cost_usd": 0.01},
                    status=TaskStatus.QUEUED,
                    expected_value=value,
                ),
            )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
            worker_prefix="mixed-probe",
        )
        leases = cast(list[Mapping[str, object]], conn.execute("SELECT task_id, status FROM leases ORDER BY task_id").fetchall())
        tasks = cast(list[Mapping[str, object]], conn.execute("SELECT task_id, status FROM tasks ORDER BY task_id").fetchall())
    assert plan["ready"]
    assert plan["runnable_task_ids"] == ["pro-task"]
    assert plan["capacity_safe"] == 1
    assert plan["reasons"] == []
    assert [row["task_id"] for row in leases] == ["pro-task"]
    assert [row["status"] for row in leases] == ["active"]
    assert {row["task_id"]: row["status"] for row in tasks} == {"flash-task": TaskStatus.QUEUED, "pro-task": TaskStatus.LEASED}
    assert any(item["task_id"] == "flash-task" and "probe does not match" in _strings(item["reasons"])[0] for item in _object_dicts(plan["blocked_tasks"]))


def test_live_orchestrator_accepts_openrouter_endpoint_provider_provenance(tmp_path: Path):
    db_path = init_db(tmp_path)
    env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="endpoint-provenance-resume",
                title="Endpoint provenance resume",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env=env,
            probe_result={
                "ok": True,
                "provider": "DeepSeek",
                "model": "deepseek/deepseek-v4-pro",
                "requested_model": "deepseek/deepseek-v4-pro",
                "provider_policy_result": "openrouter_endpoint_provenance",
            },
            write_leases=True,
            worker_prefix="endpoint-provenance",
        )
        lease = cast(Mapping[str, object], conn.execute("SELECT task_id, status FROM leases WHERE task_id = 'endpoint-provenance-resume'").fetchone())

    assert plan["ready"] is True
    assert plan["runnable_task_ids"] == ["endpoint-provenance-resume"]
    assert lease["status"] == "active"


def test_live_orchestrator_rejects_untrusted_free_model_provider_mismatch(tmp_path: Path):
    model = OPENROUTER_FREE_MODEL_ORDER[0]
    db_path = init_db(tmp_path)
    env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="untrusted-free-probe",
                title="Untrusted free probe",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": model, "allow_fallback": False, "estimated_cost_usd": 0.0},
                status=TaskStatus.QUEUED,
            ),
        )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env=env,
            probe_result={
                "ok": True,
                "provider": "not-openrouter",
                "model": model,
                "requested_model": model,
            },
            write_leases=True,
            worker_prefix="untrusted-free",
        )
        leases = _count(conn, "SELECT COUNT(*) AS n FROM leases")

    assert plan["ready"] is False
    assert plan["runnable_task_ids"] == []
    assert any("probe does not match" in reason for reason in _strings(plan["reasons"]))
    assert leases == 0


def test_live_orchestrator_leases_task_when_probe_matches_any_failover_model(tmp_path: Path):
    model_a, model_b = OPENROUTER_FREE_MODEL_ORDER[:2]
    db_path = init_db(tmp_path)
    env = {"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"}
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="failover-resume-task",
                title="Failover resume task",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                    "allow_fallback": False,
                    "estimated_cost_usd": 0.01,
                    "openrouter_failover": {"enabled": True, "models": [model_a, model_b]},
                },
                status=TaskStatus.QUEUED,
            ),
        )
        report = live_readiness_report(conn, capacity=15, env=env)
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env=env,
            probe_result={"ok": True, "provider": "openrouter", "model": model_b},
            write_leases=True,
            worker_prefix="failover-resume",
        )
        lease = cast(Mapping[str, object], conn.execute("SELECT task_id, status FROM leases WHERE task_id = 'failover-resume-task'").fetchone())
    assert _object_dicts(report["runnable_tasks"])[0]["models"] == [model_a, model_b]
    assert plan["ready"] is True
    assert plan["runnable_task_ids"] == ["failover-resume-task"]
    assert lease["status"] == "active"


def test_live_orchestrator_underfills_15_slots_with_only_safe_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        for task_id, value in (("safe-1", 20), ("safe-2", 10)):
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=task_id,
                    title=task_id,
                    task_type="source_inventory",
                    stage=LegalStage.S0_SOURCE_INVENTORY,
                    provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                    status=TaskStatus.QUEUED,
                    expected_value=value,
                ),
            )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="blocked-draft",
                title="Blocked draft",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
                expected_value=999,
            ),
        )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
            worker_prefix="live-test",
        )
        leases = cast(list[Mapping[str, object]], conn.execute("SELECT task_id, worker_id, status FROM leases ORDER BY task_id").fetchall())
    assert plan["ready"]
    assert plan["capacity_requested"] == 15
    assert plan["capacity_safe"] == 2
    assert plan["runnable_task_ids"] == ["safe-1", "safe-2"]
    assert [row["task_id"] for row in leases] == ["safe-1", "safe-2"]
    assert all(row["status"] == "active" for row in leases)
    assert all(str(row["worker_id"]).startswith("live-test-") for row in leases)


def test_live_orchestrator_expires_stale_active_lease_before_live_resume(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="stale-live-task",
                title="Stale live task",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="unrelated-stale-task",
                title="Unrelated stale task",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                status=TaskStatus.QUEUED,
            ),
        )
        stale_lease = acquire_lease(conn, task_id="stale-live-task", worker_id="old-live-worker", seconds=-1)
        unrelated_stale_lease = "lease-unrelated-stale"
        expired_at = "2000-01-01T00:00:00+00:00"
        _ = conn.execute(
            """
            INSERT INTO leases(lease_id, task_id, worker_id, status, fencing_token, expires_at, created_at, updated_at)
            VALUES (?, 'unrelated-stale-task', 'old-live-worker', 'active', 1, ?, ?, ?)
            """,
            (unrelated_stale_lease, expired_at, expired_at, expired_at),
        )
        _ = conn.execute("UPDATE tasks SET status = ? WHERE task_id = 'unrelated-stale-task'", (TaskStatus.LEASED,))
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
            worker_prefix="fresh-live",
        )
        leases = cast(list[Mapping[str, object]], conn.execute("SELECT lease_id, worker_id, status FROM leases WHERE task_id = ? ORDER BY created_at", ("stale-live-task",)).fetchall())
        unrelated_task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'unrelated-stale-task'").fetchone())
    assert plan["ready"] is True
    assert set(_strings(plan["expired_leases"])) == {stale_lease, unrelated_stale_lease}
    assert [row["status"] for row in leases] == ["expired", "active"]
    assert str(leases[1]["worker_id"]).startswith("fresh-live-")
    assert unrelated_task["status"] == TaskStatus.QUEUED


def test_live_orchestrator_rolls_back_partial_leases_when_acquisition_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    calls: list[str] = []
    real_acquire_lease = acquire_lease

    def flaky_acquire_lease(conn: sqlite3.Connection, *, task_id: str, worker_id: str, seconds: int = 900, dry_run: bool = False) -> str:
        calls.append(task_id)
        if len(calls) == 3:
            raise RuntimeError("simulated lease store failure")
        return real_acquire_lease(conn, task_id=task_id, worker_id=worker_id, seconds=seconds, dry_run=dry_run)

    monkeypatch.setattr(live_orchestrator, "acquire_lease", flaky_acquire_lease)
    with repo.db_connection(db_path) as conn:
        for task_id, value in (("rollback-1", 30), ("rollback-2", 20), ("rollback-3", 10)):
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=task_id,
                    title=task_id,
                    task_type="source_inventory",
                    stage=LegalStage.S0_SOURCE_INVENTORY,
                    provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "allow_fallback": False, "estimated_cost_usd": 0.01},
                    status=TaskStatus.QUEUED,
                    expected_value=value,
                ),
            )
        plan = prepare_live_resume(
            conn,
            capacity=15,
            env={"OPENROUTER_API_KEY": "sk-test", "ATTICUS_ENABLE_LIVE_OPENROUTER": "1"},
            probe_result={"ok": True, "provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            write_leases=True,
            worker_prefix="rollback-test",
        )
        leases = cast(list[Mapping[str, object]], conn.execute("SELECT task_id, status FROM leases ORDER BY task_id").fetchall())
        tasks = cast(list[Mapping[str, object]], conn.execute("SELECT task_id, status FROM tasks ORDER BY task_id").fetchall())
        event = cast(Mapping[str, object], conn.execute("SELECT event_type FROM events WHERE event_type = 'live_resume.rollback_leases'").fetchone())
    assert not plan["ready"]
    assert plan["leases"] == []
    assert calls == ["rollback-1", "rollback-2", "rollback-3"]
    assert [row["status"] for row in leases] == ["failed", "failed"]
    assert [row["status"] for row in tasks] == [TaskStatus.QUEUED, TaskStatus.QUEUED, TaskStatus.QUEUED]
    assert any("rolled back 2 leases" in reason for reason in _strings(plan["reasons"]))
    assert event is not None
