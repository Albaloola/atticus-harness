from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import cast

from atticus.cli import main
from atticus.db import repo
from atticus.db.doctor import verify_schema


def _init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite"
    repo.initialize_database(db_path)
    return db_path


def _drop_control_table_and_claim_current(db_path: Path, table: str) -> None:
    raw = sqlite3.connect(db_path)
    try:
        _ = raw.execute(f"DROP TABLE {table}")
        _ = raw.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
        raw.commit()
    finally:
        raw.close()


def _json_output(text: str) -> dict[str, object]:
    loaded = json.loads(text)
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def test_schema_meta_v6_missing_error_logs_is_detected_by_doctor(tmp_path: Path):
    db_path = _init_db(tmp_path)
    _drop_control_table_and_claim_current(db_path, "error_logs")

    with repo.db_connection(db_path, read_only=True) as conn:
        check = verify_schema(conn)

    assert check.ok is False
    assert check.schema_meta_version == "6"
    assert "error_logs" in check.missing_tables
    assert check.dangerous is True


def test_readonly_status_on_stale_schema_returns_schema_mismatch_not_operational_error(
    tmp_path: Path,
    capsys,
):
    db_path = _init_db(tmp_path)
    _drop_control_table_and_claim_current(db_path, "error_logs")

    code = main(["status", "--db", str(db_path)])
    output = _json_output(capsys.readouterr().out)

    assert code == 2
    assert output["ok"] is False
    assert output["reason"] == "schema_mismatch"
    assert output["schema_meta_version"] == "6"
    assert "error_logs" in output["missing_tables"]


def test_maintenance_status_on_old_db_returns_repair_command(tmp_path: Path, capsys):
    db_path = _init_db(tmp_path)
    _drop_control_table_and_claim_current(db_path, "maintenance_runs")

    code = main(["maintenance", "status", "--db", str(db_path)])
    output = _json_output(capsys.readouterr().out)

    assert code == 2
    assert output["ok"] is False
    assert output["reason"] == "schema_mismatch"
    assert "maintenance_runs" in output["missing_tables"]
    assert "doctor --db" in str(output["repair_command"])


def test_write_doctor_repairs_missing_v6_control_table(tmp_path: Path, capsys):
    db_path = _init_db(tmp_path)
    _drop_control_table_and_claim_current(db_path, "error_logs")

    code = main(["doctor", "--db", str(db_path), "--repair", "--write"])
    output = _json_output(capsys.readouterr().out)

    assert code == 0
    assert output["ok"] is True
    assert output["repaired"] is True
    with repo.db_connection(db_path, read_only=True) as conn:
        check = verify_schema(conn)
        table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'error_logs'").fetchone()

    assert check.ok is True
    assert table is not None
