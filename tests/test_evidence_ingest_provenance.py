from pathlib import Path

import pytest

from atticus.evidence_ingest.provenance import ProvenanceLogger
from atticus.tools.registry import ToolContext


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(stage="evidence-ingest-register", workspace_path=tmp_path)


@pytest.fixture
def logger(tmp_path: Path, tool_context: ToolContext) -> ProvenanceLogger:
    return ProvenanceLogger(workspace_path=tmp_path, context=tool_context)


class TestProvenanceLoggerInit:
    def test_creates_directory(self, tmp_path: Path, tool_context: ToolContext):
        ProvenanceLogger(workspace_path=tmp_path, context=tool_context)
        assert (tmp_path / "02-registers").is_dir()

    def test_sets_provenance_path(self, tmp_path: Path, tool_context: ToolContext):
        logger = ProvenanceLogger(workspace_path=tmp_path, context=tool_context)
        assert logger.provenance_path == tmp_path / "02-registers" / "physical_provenance.jsonl"


class TestProvenanceLoggerLog:
    def test_log_appends_json_line(self, logger: ProvenanceLogger, tmp_path: Path):
        logger.log("write_file", path="/foo/bar.txt")
        log_file = tmp_path / "02-registers" / "physical_provenance.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        import json
        entry = json.loads(lines[0])
        assert entry["operation"] == "write_file"
        assert entry["path"] == "/foo/bar.txt"
        assert "timestamp" in entry

    def test_multiple_log_entries_appended(self, logger: ProvenanceLogger, tmp_path: Path):
        logger.log("operation_a", key="value_a")
        logger.log("operation_b", key="value_b")
        logger.log("operation_c", key="value_c")
        log_file = tmp_path / "02-registers" / "physical_provenance.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3
        import json
        entries = [json.loads(line) for line in lines]
        assert entries[0]["operation"] == "operation_a"
        assert entries[1]["operation"] == "operation_b"
        assert entries[2]["operation"] == "operation_c"


class TestProvenanceLoggerGetLog:
    def test_get_log_returns_empty_when_no_file(self, logger: ProvenanceLogger):
        assert logger.get_log() == []

    def test_get_log_reads_and_parses_all_lines(self, logger: ProvenanceLogger):
        logger.log("op1", meta="data1")
        logger.log("op2", meta="data2")
        entries = logger.get_log()
        assert len(entries) == 2
        assert entries[0]["operation"] == "op1"
        assert entries[0]["meta"] == "data1"
        assert entries[1]["operation"] == "op2"
        assert entries[1]["meta"] == "data2"

    def test_get_log_returns_parsed_dicts(self, logger: ProvenanceLogger):
        logger.log("test_op", number=42, flag=True)
        entries = logger.get_log()
        assert isinstance(entries, list)
        assert isinstance(entries[0], dict)
        assert entries[0]["number"] == 42
        assert entries[0]["flag"] is True
