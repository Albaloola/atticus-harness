"""Tests for evidence-ingest CLI subcommand parser."""

import argparse
from pathlib import Path

import pytest

from atticus.evidence_ingest.cli import add_evidence_ingest_subparser
from atticus.cli import build_parser


def _build_evidence_ingest_parser() -> argparse.ArgumentParser:
    """Build a minimal parser with only the evidence-ingest subcommand."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_evidence_ingest_subparser(subparsers)
    return parser


class TestScanSubcommand:
    """Test scan subcommand argument parsing."""

    def test_scan_requires_workspace_and_source_dir(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "scan"])

    def test_scan_parses_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "scan",
            "--workspace", "/tmp/workspace",
            "--source-dir", "/tmp/source",
        ])

        assert args.command == "evidence-ingest"
        assert args.evidence_action == "scan"
        assert args.workspace == "/tmp/workspace"
        assert args.source_dir == "/tmp/source"
        assert hasattr(args, "func")

    def test_scan_has_correct_help(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "scan", "--help"])


class TestAnalyseSubcommand:
    """Test analyse subcommand argument parsing."""

    def test_analyse_requires_workspace_and_source_dir(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "analyse"])

    def test_analyse_parses_required_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "analyse",
            "--workspace", "/tmp/workspace",
            "--source-dir", "/tmp/source",
        ])

        assert args.evidence_action == "analyse"
        assert args.workspace == "/tmp/workspace"
        assert args.source_dir == "/tmp/source"
        assert args.provider is None
        assert args.model is None
        assert hasattr(args, "func")

    def test_analyse_parses_optional_provider_and_model(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "analyse",
            "--workspace", "/tmp/workspace",
            "--source-dir", "/tmp/source",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-pro",
        ])

        assert args.provider == "openrouter"
        assert args.model == "deepseek/deepseek-v4-pro"


class TestResolveSubcommand:
    """Test resolve subcommand argument parsing."""

    def test_resolve_requires_workspace(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "resolve"])

    def test_resolve_parses_required_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "resolve",
            "--workspace", "/tmp/workspace",
        ])

        assert args.evidence_action == "resolve"
        assert args.workspace == "/tmp/workspace"
        assert args.provider is None
        assert args.model is None
        assert hasattr(args, "func")

    def test_resolve_parses_optional_provider_and_model(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "resolve",
            "--workspace", "/tmp/workspace",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-pro",
        ])

        assert args.provider == "openrouter"
        assert args.model == "deepseek/deepseek-v4-pro"


class TestPlanSubcommands:
    """Test plan subcommand group argument parsing."""

    def test_plan_requires_subaction(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "plan"])

    def test_plan_validate_parses_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "plan", "validate",
            "--workspace", "/tmp/workspace",
        ])

        assert args.evidence_action == "plan"
        assert args.plan_action == "validate"
        assert args.workspace == "/tmp/workspace"
        assert hasattr(args, "func")

    def test_plan_inspect_parses_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "plan", "inspect",
            "--workspace", "/tmp/workspace",
        ])

        assert args.evidence_action == "plan"
        assert args.plan_action == "inspect"
        assert args.workspace == "/tmp/workspace"
        assert hasattr(args, "func")

    def test_plan_override_parses_required_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "plan", "override",
            "--workspace", "/tmp/workspace",
        ])

        assert args.evidence_action == "plan"
        assert args.plan_action == "override"
        assert args.workspace == "/tmp/workspace"
        assert args.source_id is None
        assert args.override_json is None
        assert hasattr(args, "func")

    def test_plan_override_parses_optional_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "plan", "override",
            "--workspace", "/tmp/workspace",
            "--source-id", "src-123",
            "--override-json", '{"action": "force_ingest"}',
        ])

        assert args.source_id == "src-123"
        assert args.override_json == '{"action": "force_ingest"}'

    def test_plan_accept_parses_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "plan", "accept",
            "--workspace", "/tmp/workspace",
        ])

        assert args.evidence_action == "plan"
        assert args.plan_action == "accept"
        assert args.workspace == "/tmp/workspace"
        assert hasattr(args, "func")


class TestExecuteSubcommand:
    """Test execute subcommand argument parsing."""

    def test_execute_requires_workspace(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "execute"])

    def test_execute_defaults_to_dry_run(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "execute",
            "--workspace", "/tmp/workspace",
        ])

        assert args.evidence_action == "execute"
        assert args.workspace == "/tmp/workspace"
        assert args.dry_run is True
        assert hasattr(args, "func")

    def test_execute_with_write_flag(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "execute",
            "--workspace", "/tmp/workspace",
            "--write",
        ])

        assert args.dry_run is False


class TestRegisterSubcommand:
    """Test register subcommand argument parsing."""

    def test_register_requires_workspace_and_db(self):
        parser = _build_evidence_ingest_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["evidence-ingest", "register"])

    def test_register_parses_required_arguments(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "register",
            "--workspace", "/tmp/workspace",
            "--db", "/tmp/db.sqlite",
        ])

        assert args.evidence_action == "register"
        assert args.workspace == "/tmp/workspace"
        assert args.db == "/tmp/db.sqlite"
        assert args.provider is None
        assert args.model is None
        assert hasattr(args, "func")

    def test_register_parses_optional_provider_and_model(self):
        parser = _build_evidence_ingest_parser()
        args = parser.parse_args([
            "evidence-ingest", "register",
            "--workspace", "/tmp/workspace",
            "--db", "/tmp/db.sqlite",
            "--provider", "openrouter",
            "--model", "deepseek/deepseek-v4-pro",
        ])

        assert args.provider == "openrouter"
        assert args.model == "deepseek/deepseek-v4-pro"


class TestIntegrationWithMainParser:
    """Test that evidence-ingest subparser integrates with main CLI parser."""

    def test_main_parser_includes_evidence_ingest(self):
        parser = build_parser()

        args = parser.parse_args([
            "evidence-ingest", "scan",
            "--workspace", "/tmp/workspace",
            "--source-dir", "/tmp/source",
        ])

        assert args.command == "evidence-ingest"
        assert args.evidence_action == "scan"

    def test_main_parser_evidence_ingest_all_subcommands(self):
        parser = build_parser()

        subcommands = [
            (["evidence-ingest", "scan", "--workspace", "/w", "--source-dir", "/s"], "scan"),
            (["evidence-ingest", "analyse", "--workspace", "/w", "--source-dir", "/s"], "analyse"),
            (["evidence-ingest", "resolve", "--workspace", "/w"], "resolve"),
            (["evidence-ingest", "plan", "validate", "--workspace", "/w"], "plan"),
            (["evidence-ingest", "plan", "inspect", "--workspace", "/w"], "plan"),
            (["evidence-ingest", "plan", "override", "--workspace", "/w"], "plan"),
            (["evidence-ingest", "plan", "accept", "--workspace", "/w"], "plan"),
            (["evidence-ingest", "execute", "--workspace", "/w"], "execute"),
            (["evidence-ingest", "register", "--workspace", "/w", "--db", "/d"], "register"),
        ]

        for argv, expected_action in subcommands:
            args = parser.parse_args(argv)
            assert args.command == "evidence-ingest"
            if expected_action == "plan":
                assert args.evidence_action == "plan"
            else:
                assert args.evidence_action == expected_action

    def test_main_parser_other_commands_still_work(self):
        parser = build_parser()

        args = parser.parse_args(["status", "--db", "/tmp/db.sqlite"])
        assert args.command == "status"
