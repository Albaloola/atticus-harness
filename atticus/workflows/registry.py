"""Markdown-backed legal workflow planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import json
import re
import sqlite3

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Workflow:
    name: str
    path: Path
    frontmatter: dict[str, object]
    body: str

    @property
    def required_certifications(self) -> list[str]:
        return _string_list(self.frontmatter.get("required_certifications"))

    @property
    def validation_gates(self) -> list[str]:
        return _string_list(self.frontmatter.get("validation_gates"))

    @property
    def creates_tasks(self) -> list[str]:
        return _string_list(self.frontmatter.get("creates_tasks"))

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "frontmatter": self.frontmatter,
            "required_certifications": self.required_certifications,
            "validation_gates": self.validation_gates,
            "creates_tasks": self.creates_tasks,
            "body": self.body,
        }


def list_workflows() -> list[Workflow]:
    return [load_workflow(path.stem) for path in sorted(WORKFLOW_DIR.glob("*.md"))]


def load_workflow(name: str) -> Workflow:
    safe = _safe_component(name)
    path = WORKFLOW_DIR / f"{safe}.md"
    if not path.exists():
        raise KeyError(f"unknown workflow: {name}")
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(text)
    workflow_name = str(frontmatter.get("name") or safe)
    return Workflow(workflow_name, path, frontmatter, body.strip())


def plan_workflow(
    conn: sqlite3.Connection,
    *,
    name: str,
    matter_scope: str,
    dry_run: bool = True,
) -> dict[str, object]:
    workflow = load_workflow(name)
    if not dry_run:
        repo.ensure_matter(conn, matter_scope)
    tasks = [_task_for_workflow(workflow, matter_scope=matter_scope, task_key=task_key) for task_key in workflow.creates_tasks]
    existing = {
        str(row["task_id"])
        for row in conn.execute(
            "SELECT task_id FROM tasks WHERE task_id IN (%s)" % ",".join("?" for _ in tasks),
            tuple(str(task["task_id"]) for task in tasks),
        ).fetchall()
    } if tasks else set()
    if not dry_run:
        for task in tasks:
            if str(task["task_id"]) in existing:
                continue
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=str(task["task_id"]),
                    title=str(task["title"]),
                    task_type=str(task["task_type"]),
                    matter_scope=matter_scope,
                    stage=LegalStage(str(task["stage"])),
                    status=TaskStatus.QUEUED,
                    required_certifications=[
                        {"subject_type": "matter", "subject_id": matter_scope, "certification_type": cert}
                        for cert in workflow.required_certifications
                    ],
                    validation_gates=workflow.validation_gates,
                ),
            )
        _ = repo.emit_event(
            conn,
            "workflow.tasks_created",
            matter_scope=matter_scope,
            payload={"workflow": workflow.name, "task_ids": [str(task["task_id"]) for task in tasks if str(task["task_id"]) not in existing]},
        )
    return {
        "dry_run": dry_run,
        "workflow": workflow.name,
        "matter_scope": matter_scope,
        "tasks": tasks,
        "existing_task_ids": sorted(existing),
        "created_task_ids": [] if dry_run else [str(task["task_id"]) for task in tasks if str(task["task_id"]) not in existing],
        "required_certifications": workflow.required_certifications,
        "validation_gates": workflow.validation_gates,
        "external_actions": "blocked",
    }


def _task_for_workflow(workflow: Workflow, *, matter_scope: str, task_key: str) -> dict[str, object]:
    stage = str(workflow.frontmatter.get("stage") or "S0")
    task_id = _safe_component(f"{matter_scope}-{workflow.name}-{task_key}")
    return {
        "task_id": task_id,
        "matter_scope": matter_scope,
        "status": str(TaskStatus.QUEUED),
        "stage": stage,
        "task_type": task_key,
        "title": f"{workflow.name}: {task_key.replace('_', ' ')}",
        "instructions": _instructions_for(workflow, task_key),
        "validation_gates": workflow.validation_gates,
        "required_certifications": workflow.required_certifications,
        "risks": workflow.frontmatter.get("risk_level", ""),
    }


def _instructions_for(workflow: Workflow, task_key: str) -> str:
    return (
        f"Workflow {workflow.name} task {task_key}. Produce {task_key.replace('_', ' ')} for the matter. "
        "Use only matter-scoped sources/artifacts. Return candidate packets only. "
        "Do not send, file, upload, contact, or perform external legal actions."
    )


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise ValueError("workflow markdown must start with frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("workflow markdown frontmatter is not closed")
    raw = text[4:end]
    body = text[end + 5 :]
    data: dict[str, object] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid workflow frontmatter line: {line}")
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError(f"workflow frontmatter {key} must be a list")
            data[key.strip()] = parsed
        else:
            data[key.strip()] = value
    required = {"name", "version", "jurisdiction", "stage", "creates_tasks", "required_certifications", "validation_gates", "risk_level", "description"}
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"workflow missing frontmatter keys: {', '.join(missing)}")
    return data, body


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _safe_component(value: str) -> str:
    return _SAFE_ID_RE.sub("-", value.strip()).strip(".-") or "workflow"
