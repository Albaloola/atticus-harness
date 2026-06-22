from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from atticus.core.policies import LegalStage
from atticus.core.tasks import TaskSpec
from atticus.cli import main
from atticus.db import repo
from atticus.skills.registry import list_skills, load_skill, skills_for_task
from atticus.workers.work_order import build_work_order


def test_scots_legal_humanizer_skill_is_bundled_and_loadable():
    skill = load_skill("scots-legal-humanizer")

    assert skill.skill_id == "scots-legal-humanizer"
    assert skill.manifest["name"] == "scots-legal-humanizer"
    assert "Scottish legal" in str(skill.manifest["description"])
    assert "Preserve substance. Improve presentation." in skill.body
    assert "Atticus harness use" in skill.body
    assert "candidate, not canonical" in skill.body
    assert "references/scottish-legal-style-map.md" in skill.references
    assert "examples/simple-procedure-claim.md" in skill.examples


def test_list_skills_includes_scots_legal_humanizer():
    listed = {skill.skill_id: skill for skill in list_skills()}

    assert "scots-legal-humanizer" in listed
    assert listed["scots-legal-humanizer"].manifest["version"] == "1.1.0"


def test_humanizer_attaches_to_draft_and_humanize_tasks_but_not_foundation_tasks(tmp_path: Path):
    db_path = tmp_path / "skills.sqlite3"
    repo.initialize_database(db_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="humanize-letter",
                title="Humanise Scottish complaint letter",
                task_type="humanize_draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="source-inventory",
                title="Source inventory",
                task_type="source_inventory",
                stage=LegalStage.S0_SOURCE_INVENTORY,
            ),
        )

        humanize_order = build_work_order(conn, task_id="humanize-letter", persist_context=False).as_dict()
        inventory_order = build_work_order(conn, task_id="source-inventory", persist_context=False).as_dict()

    humanize_skills = cast(list[Mapping[str, object]], humanize_order["skills"])
    inventory_skills = cast(list[Mapping[str, object]], inventory_order["skills"])
    assert [skill["skill_id"] for skill in humanize_skills] == ["scots-legal-humanizer"]
    assert "Non-negotiable safeguards" in str(humanize_skills[0]["body"])
    assert inventory_skills == []


def test_skills_for_task_recognises_scottish_legal_humanizer_intents():
    assert [skill.skill_id for skill in skills_for_task(task_type="draft", stage="S8", title="Simple procedure response")] == [
        "scots-legal-humanizer"
    ]
    assert [skill.skill_id for skill in skills_for_task(task_type="extract", stage="S0", title="Source extraction")] == []
    assert [skill.skill_id for skill in skills_for_task(task_type="evidence_issue_map", stage="S2", title="Map evidence before drafting")] == [
        "scots-legal-humanizer"
    ]


def test_skill_cli_lists_and_shows_bundled_skill(capsys: pytest.CaptureFixture[str]):
    assert main(["skill", "list"]) == 0
    listed = capsys.readouterr().out
    assert "scots-legal-humanizer" in listed

    assert main(["skill", "show", "--skill-id", "scots-legal-humanizer"]) == 0
    shown = capsys.readouterr().out
    assert "Scottish legal" in shown
    assert "Non-negotiable safeguards" in shown
