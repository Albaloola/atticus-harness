from __future__ import annotations

from collections.abc import Mapping
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import cast

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.policy import ProviderActual, ProviderRequest, check_provider_policy
from atticus.workers.work_order import build_work_order


MATTER = "napier-accommodation-arrears"
PINNED_POLICY = {
    "provider": "openai-codex",
    "model": "gpt-5.5",
    "allow_fallback": False,
    "estimated_cost_usd": 0.0,
}


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(float(str(row["n"])))


def _json_mapping(text: str) -> Mapping[str, object]:
    value = json.loads(text)
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _text_value(conn: sqlite3.Connection, sql: str) -> str:
    row = conn.execute(sql).fetchone()
    assert row is not None
    value = row[0]
    assert value is not None
    return str(value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_inventory(workspace: Path) -> Path:
    stored = workspace / "stored"
    stored.mkdir(parents=True)
    first = stored / "first.txt"
    second = stored / "second.txt"
    _ = first.write_text("first local source", encoding="utf-8")
    _ = second.write_text("second local source", encoding="utf-8")
    inventory = workspace / "02-registers" / "file_inventory.csv"
    inventory.parent.mkdir(parents=True)
    rows = [
        {
            "source_id": "napier-src-001",
            "category": "text",
            "original_relative_path": "originals/first.txt",
            "stored_path": "stored/first.txt",
            "size_bytes": str(first.stat().st_size),
            "sha256": _sha256(first),
            "urgent_flag": "",
            "notes": "fixture row",
        },
        {
            "source_id": "napier-src-002",
            "category": "text",
            "original_relative_path": "originals/second.txt",
            "stored_path": "stored/second.txt",
            "size_bytes": str(second.stat().st_size),
            "sha256": _sha256(second),
            "urgent_flag": "yes",
            "notes": "",
        },
        {
            "source_id": "napier-src-missing",
            "category": "pdf",
            "original_relative_path": "missing.pdf",
            "stored_path": "stored/missing.pdf",
            "size_bytes": "123",
            "sha256": "f" * 64,
            "urgent_flag": "",
            "notes": "missing fixture row",
        },
    ]
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return inventory


def test_seed_matter_cli_is_dry_run_by_default_and_idempotent_with_write(tmp_path: Path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter-workspace"
    inventory = _write_inventory(workspace)

    dry_code = cli_main(
        [
            "seed-matter",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--workspace",
            str(workspace),
            "--inventory",
            str(inventory),
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.5",
            "--no-fallback",
        ]
    )
    assert dry_code == 0
    with repo.db_connection(db_path) as conn:
        assert _count(conn, "SELECT COUNT(*) AS n FROM matters WHERE matter_scope = ?", (MATTER,)) == 0
        assert _count(conn, "SELECT COUNT(*) AS n FROM sources WHERE matter_scope = ?", (MATTER,)) == 0
        assert _count(conn, "SELECT COUNT(*) AS n FROM tasks WHERE matter_scope = ?", (MATTER,)) == 0

    write_args = [
        "seed-matter",
        "--db",
        str(db_path),
        "--matter",
        MATTER,
        "--workspace",
        str(workspace),
        "--inventory",
        str(inventory),
        "--provider",
        "openai-codex",
        "--model",
        "gpt-5.5",
        "--no-fallback",
        "--write",
    ]
    assert cli_main(write_args) == 0
    assert cli_main(write_args) == 0

    with repo.db_connection(db_path) as conn:
        matter = cast(Mapping[str, object], conn.execute("SELECT * FROM matters WHERE matter_scope = ?", (MATTER,)).fetchone())
        sources = cast(list[Mapping[str, object]], conn.execute("SELECT * FROM sources WHERE matter_scope = ? ORDER BY source_id", (MATTER,)).fetchall())
        tasks = cast(list[Mapping[str, object]], conn.execute("SELECT * FROM tasks WHERE matter_scope = ? ORDER BY task_id", (MATTER,)).fetchall())
        snapshot_count = _count(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM source_snapshots ss
            JOIN sources s ON s.source_id = ss.source_id
            WHERE s.matter_scope = ?
            """,
            (MATTER,),
        )
        tracked_count = _count(conn, "SELECT COUNT(*) AS n FROM tracked_files WHERE matter_scope = ?", (MATTER,))
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases")
        candidate_count = _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs")
        provider_run_count = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")

    assert matter["status"] == "active"
    assert [source["source_id"] for source in sources] == ["napier-src-001", "napier-src-002"]
    assert sources[0]["sha256"] == _sha256(workspace / "stored" / "first.txt")
    assert snapshot_count == 2
    assert tracked_count == 2
    assert len(tasks) == 1
    assert tasks[0]["matter_scope"] == MATTER
    assert tasks[0]["stage"] == "S0"
    assert tasks[0]["status"] == str(TaskStatus.QUEUED)
    assert json.loads(str(tasks[0]["source_dependencies_json"])) == ["napier-src-001", "napier-src-002"]
    assert dict(_json_mapping(str(tasks[0]["provider_policy_json"]))) == PINNED_POLICY
    assert lease_count == 0
    assert candidate_count == 0
    assert provider_run_count == 0

    with repo.db_connection(db_path) as conn:
        order = build_work_order(conn, task_id=str(tasks[0]["task_id"]), persist_context=False)
    assert order.source_dependencies == ["napier-src-001", "napier-src-002"]


def test_seed_matter_repairs_existing_source_inventory_task_dependencies(tmp_path: Path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter-workspace"
    inventory = _write_inventory(workspace)
    write_args = [
        "seed-matter",
        "--db",
        str(db_path),
        "--matter",
        MATTER,
        "--workspace",
        str(workspace),
        "--inventory",
        str(inventory),
        "--provider",
        "openai-codex",
        "--model",
        "gpt-5.5",
        "--no-fallback",
        "--write",
    ]
    assert cli_main(write_args) == 0
    with repo.db_connection(db_path) as conn:
        _ = conn.execute(
            "UPDATE tasks SET source_dependencies_json = '[]' WHERE matter_scope = ? AND task_type = 'source_inventory'",
            (MATTER,),
        )

    assert cli_main(write_args) == 0

    with repo.db_connection(db_path) as conn:
        deps = json.loads(_text_value(conn, "SELECT source_dependencies_json FROM tasks WHERE matter_scope = 'napier-accommodation-arrears'"))
        events = _count(conn, "SELECT COUNT(*) AS n FROM events WHERE event_type = 'matter.seeded'")

    assert deps == ["napier-src-001", "napier-src-002"]
    assert events == 2


def test_seed_matter_skips_generated_harness_outputs_as_source_evidence(tmp_path: Path, capsys):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter-workspace"
    generated = workspace / "03-working" / "extracted-text"
    generated.mkdir(parents=True)
    generated_file = generated / "SRC-0001.txt"
    _ = generated_file.write_text("generated extraction should not be core evidence", encoding="utf-8")
    inventory = workspace / "02-registers" / "file_inventory.csv"
    inventory.parent.mkdir(parents=True)
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_id", "category", "stored_path", "size_bytes", "sha256"])
        writer.writeheader()
        writer.writerow(
            {
                "source_id": "generated-src",
                "category": "text",
                "stored_path": "03-working/extracted-text/SRC-0001.txt",
                "size_bytes": str(generated_file.stat().st_size),
                "sha256": _sha256(generated_file),
            }
        )

    assert (
        cli_main(
            [
                "seed-matter",
                "--db",
                str(db_path),
                "--matter",
                MATTER,
                "--workspace",
                str(workspace),
                "--inventory",
                str(inventory),
                "--provider",
                "openai-codex",
                "--model",
                "gpt-5.5",
                "--no-fallback",
                "--write",
            ]
        )
        == 0
    )
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))

    with repo.db_connection(db_path) as conn:
        sources = _count(conn, "SELECT COUNT(*) AS n FROM sources WHERE matter_scope = ?", (MATTER,))

    missing = cast(list[Mapping[str, object]], output["missing_files"])
    assert sources == 0
    assert output["sources_skipped"] == 1
    assert missing[0]["reason"] == "generated harness path is not source evidence"


def test_seed_matter_rejects_cross_matter_source_id_collision(tmp_path: Path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "collision-workspace"
    stored = workspace / "stored"
    stored.mkdir(parents=True)
    source_file = stored / "beta.txt"
    _ = source_file.write_text("beta source", encoding="utf-8")
    inventory = workspace / "inventory.csv"
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_id", "category", "stored_path", "size_bytes", "sha256"])
        writer.writeheader()
        writer.writerow(
            {
                "source_id": "shared-src",
                "category": "text",
                "stored_path": "stored/beta.txt",
                "size_bytes": str(source_file.stat().st_size),
                "sha256": _sha256(source_file),
            }
        )
    with repo.db_connection(db_path) as conn:
        _ = repo.add_source(conn, source_id="shared-src", matter_scope="alpha", path="/alpha/original.txt", sha256="a" * 64)

    code = cli_main(
        [
            "seed-matter",
            "--db",
            str(db_path),
            "--matter",
            "beta",
            "--workspace",
            str(workspace),
            "--inventory",
            str(inventory),
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.5",
            "--no-fallback",
            "--write",
        ]
    )

    with repo.db_connection(db_path) as conn:
        original = cast(Mapping[str, object], conn.execute("SELECT matter_scope, path FROM sources WHERE source_id = 'shared-src'").fetchone())
        beta_sources = _count(conn, "SELECT COUNT(*) AS n FROM sources WHERE matter_scope = 'beta'")

    assert code == 2
    assert original["matter_scope"] == "alpha"
    assert original["path"] == "/alpha/original.txt"
    assert beta_sources == 0


@pytest.mark.parametrize(
    ("source_id", "matter", "path_case"),
    [
        ("external-src", "external-path-test", "absolute"),
        ("relative-external-src", "relative-path-test", "relative_escape"),
        ("symlink-external-src", "symlink-path-test", "symlink_escape"),
    ],
    ids=["absolute", "relative_escape", "symlink_escape"],
)
def test_seed_matter_rejects_source_path_outside_workspace(
    tmp_path: Path,
    source_id: str,
    matter: str,
    path_case: str,
):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-source.txt"
    _ = outside.write_text("outside matter source", encoding="utf-8")
    if path_case == "absolute":
        stored_path = str(outside)
    elif path_case == "relative_escape":
        stored_path = "../outside-source.txt"
    else:
        symlink = workspace / "linked-source.txt"
        symlink.symlink_to(outside)
        stored_path = "linked-source.txt"
    inventory = workspace / "inventory.csv"
    with inventory.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_id", "category", "stored_path", "size_bytes", "sha256"])
        writer.writeheader()
        writer.writerow(
            {
                "source_id": source_id,
                "category": "text",
                "stored_path": stored_path,
                "size_bytes": str(outside.stat().st_size),
                "sha256": _sha256(outside),
            }
        )

    code = cli_main(
        [
            "seed-matter",
            "--db",
            str(db_path),
            "--matter",
            matter,
            "--workspace",
            str(workspace),
            "--inventory",
            str(inventory),
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.5",
            "--no-fallback",
            "--write",
        ]
    )

    with repo.db_connection(db_path) as conn:
        source_count = _count(conn, "SELECT COUNT(*) AS n FROM sources WHERE source_id = ?", (source_id,))

    assert code == 2
    assert source_count == 0


def test_set_provider_policy_updates_only_queued_tasks_for_matter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="napier-queued-1",
                title="Napier queued 1",
                task_type="source_inventory",
                matter_scope=MATTER,
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="napier-complete",
                title="Napier complete",
                task_type="source_inventory",
                matter_scope=MATTER,
                status=TaskStatus.COMPLETE,
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False},
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="other-queued",
                title="Other queued",
                task_type="source_inventory",
                matter_scope="other-matter",
            ),
        )

    dry_code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "openai-codex",
            "--model",
            "openai-codex/gpt-5.5",
            "--no-fallback",
        ]
    )
    assert dry_code == 0
    with repo.db_connection(db_path) as conn:
        dry_policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-queued-1'"))
    assert dry_policy == {}

    write_code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "openai-codex",
            "--model",
            "openai-codex/gpt-5.5",
            "--no-fallback",
            "--write",
        ]
    )
    assert write_code == 0

    with repo.db_connection(db_path) as conn:
        queued = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-queued-1'"))
        complete = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-complete'"))
        other = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'other-queued'"))
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")

    assert dict(queued) == PINNED_POLICY
    assert "openrouter_failover" not in queued
    assert complete["provider"] == "openrouter"
    assert other == {}
    assert provider_runs == 0


def test_codex_provider_policy_rejects_fallback_unknowns_and_drift_before_live(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="napier-queued-policy",
                title="Napier queued policy",
                task_type="source_inventory",
                matter_scope=MATTER,
            ),
        )

    fallback_code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.5",
            "--allow-fallback",
            "--write",
        ]
    )
    unknown_code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.4",
            "--no-fallback",
            "--write",
        ]
    )
    drift = check_provider_policy(
        ProviderRequest("openai-codex", "gpt-5.5", allow_fallback=False),
        actual=ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
    )
    fallback_request = check_provider_policy(ProviderRequest("openai-codex", "gpt-5.5", allow_fallback=True))

    with repo.db_connection(db_path) as conn:
        policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-queued-policy'"))
        provider_runs = _count(conn, "SELECT COUNT(*) AS n FROM provider_runs")

    assert fallback_code == 2
    assert unknown_code == 2
    assert not drift.allowed
    assert drift.result == "failed_closed"
    assert not fallback_request.allowed
    assert fallback_request.result == "failed_closed"
    assert policy == {}
    assert provider_runs == 0


def test_direct_deepseek_provider_is_rejected_on_flat_policy_surfaces(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="napier-queued-deepseek",
                title="Napier queued DeepSeek",
                task_type="source_inventory",
                matter_scope=MATTER,
            ),
        )

    provider_policy_code = cli_main(
        [
            "provider-policy",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-pro",
        ]
    )
    set_policy_code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-pro",
            "--no-fallback",
            "--write",
        ]
    )

    with repo.db_connection(db_path) as conn:
        policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-queued-deepseek'"))

    assert provider_policy_code == 2
    assert set_policy_code == 2
    assert policy == {}


def test_flat_openrouter_fallback_is_rejected_without_model_pool(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="napier-queued-openrouter",
                title="Napier queued OpenRouter",
                task_type="source_inventory",
                matter_scope=MATTER,
            ),
        )

    provider_policy_code = cli_main(
        [
            "provider-policy",
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-pro",
            "--actual-provider",
            "openrouter",
            "--actual-model",
            "deepseek/deepseek-v4-flash",
            "--allow-fallback",
        ]
    )
    set_policy_code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-pro",
            "--allow-fallback",
            "--write",
        ]
    )

    with repo.db_connection(db_path) as conn:
        policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-queued-openrouter'"))

    assert provider_policy_code == 2
    assert set_policy_code == 2
    assert policy == {}


def test_set_provider_policy_rejects_non_finite_estimated_cost(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="napier-nan-cost",
                title="Napier NaN cost",
                task_type="source_inventory",
                matter_scope=MATTER,
            ),
        )

    code = cli_main(
        [
            "set-provider-policy",
            "--db",
            str(db_path),
            "--matter",
            MATTER,
            "--provider",
            "openrouter",
            "--model",
            "deepseek/deepseek-v4-pro",
            "--no-fallback",
            "--estimated-cost-usd",
            "nan",
            "--write",
        ]
    )

    with repo.db_connection(db_path) as conn:
        policy = _json_mapping(_text_value(conn, "SELECT provider_policy_json FROM tasks WHERE task_id = 'napier-nan-cost'"))

    assert code == 2
    assert policy == {}
