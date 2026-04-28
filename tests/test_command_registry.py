from __future__ import annotations

from collections.abc import Mapping
import json
from typing import cast

from atticus.cli import main as cli_main
from atticus.commands.registry import command_by_name, list_commands


def test_command_registry_classifies_safety_metadata():
    commands = {command.name: command for command in list_commands()}

    assert commands["context"].read_only_safe is True
    assert commands["coordinator"].supports_dry_run is True
    assert commands["coordinator"].read_only_safe is False
    assert commands["tools"].read_only_safe is True
    assert commands["session"].read_only_safe is True
    assert commands["run-free-loop"].requires_live is False
    assert commands["run-free-loop"].supports_dry_run is False
    assert commands["run-local"].requires_write is True
    assert commands["provider-probe"].requires_live is True
    assert all(not command.read_only_safe for command in commands.values() if command.requires_write or command.requires_live)
    assert command_by_name("context").name == "context"


def test_commands_cli_lists_and_shows_json(capsys):
    assert cli_main(["commands", "list", "--json"]) == 0
    listed = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    command_rows = cast(list[Mapping[str, object]], listed["commands"])
    assert any(item["name"] == "context" and item["read_only_safe"] is True for item in command_rows)

    assert cli_main(["command", "show", "run-local", "--json"]) == 0
    shown = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert shown["name"] == "run-local"
    assert shown["requires_write"] is True
