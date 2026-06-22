"""Load bundled Atticus worker skills.

Skills are markdown packages under the repository-local ``skills/`` directory.
They are not execution adapters and never perform external actions; they are
instruction bundles that can be attached to bounded work orders.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"


@dataclass(frozen=True)
class Skill:
    skill_id: str
    path: Path
    manifest: dict[str, object]
    body: str
    references: tuple[str, ...]
    examples: tuple[str, ...]

    def as_work_order_context(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "path": str(self.path),
            "manifest": self.manifest,
            "body": self.body,
            "references": list(self.references),
            "examples": list(self.examples),
        }


def list_skills(*, root: Path | None = None) -> list[Skill]:
    """Return all bundled skills with valid ``SKILL.md`` files."""

    skills_root = root or SKILLS_ROOT
    if not skills_root.exists():
        return []
    skills: list[Skill] = []
    for package_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill_file = package_dir / "SKILL.md"
        if skill_file.exists():
            skills.append(_load_skill_from_dir(package_dir))
    return skills


def load_skill(skill_id: str, *, root: Path | None = None) -> Skill:
    """Load one bundled skill by directory id."""

    safe_id = _safe_skill_id(skill_id)
    package_dir = (root or SKILLS_ROOT) / safe_id
    if not package_dir.is_dir():
        raise KeyError(f"unknown skill: {skill_id}")
    return _load_skill_from_dir(package_dir)


def skills_for_task(*, task_type: str, stage: str, title: str = "") -> list[Skill]:
    """Resolve default skills for a task without changing provider policy."""

    if _needs_scots_legal_humanizer(task_type=task_type, stage=stage, title=title):
        return [load_skill("scots-legal-humanizer")]
    return []


def _load_skill_from_dir(package_dir: Path) -> Skill:
    raw = (package_dir / "SKILL.md").read_text(encoding="utf-8")
    manifest, body = _split_front_matter(raw)
    skill_id = package_dir.name
    manifest_name = str(manifest.get("name") or skill_id)
    if manifest_name != skill_id:
        raise ValueError(f"skill manifest name {manifest_name!r} does not match package {skill_id!r}")
    return Skill(
        skill_id=skill_id,
        path=package_dir,
        manifest=manifest,
        body=body.strip(),
        references=_relative_files(package_dir / "references", package_dir),
        examples=_relative_files(package_dir / "examples", package_dir),
    )


def _split_front_matter(raw: str) -> tuple[dict[str, object], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    try:
        _start, front_matter, body = raw.split("---", 2)
    except ValueError:
        return {}, raw
    return _parse_front_matter(front_matter), body


def _parse_front_matter(text: str) -> dict[str, object]:
    manifest: dict[str, object] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value == "|":
            block: list[str] = []
            index += 1
            while index < len(lines) and (lines[index].startswith(" ") or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            manifest[key] = "\n".join(item for item in block if item).strip()
            continue
        if not value:
            items: list[str] = []
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith("- "):
                items.append(lines[index].lstrip()[2:].strip())
                index += 1
            manifest[key] = items
            continue
        manifest[key] = value
        index += 1
    return manifest


def _relative_files(directory: Path, package_dir: Path) -> tuple[str, ...]:
    if not directory.exists():
        return ()
    return tuple(
        str(path.relative_to(package_dir))
        for path in sorted(cast(Iterable[Path], directory.rglob("*")))
        if path.is_file()
    )


def _needs_scots_legal_humanizer(*, task_type: str, stage: str, title: str) -> bool:
    task_type_l = task_type.lower()
    stage_l = stage.upper()
    title_l = title.lower()
    explicit_triggers = (
        "humanize",
        "humanise",
        "de-ai",
    )
    drafting_triggers = (
        "draft",
        "complaint",
        "letter",
        "response",
        "note of argument",
        "simple procedure",
        "sheriff court",
        "scottish",
        "scots",
    )
    if any(term in f"{task_type_l} {title_l}" for term in explicit_triggers):
        return True
    if task_type_l in {"draft", "draft_preparation", "complaint_draft", "letter_draft", "humanize_draft", "humanise_draft"}:
        return True
    if stage_l == "S8" and any(term in title_l for term in ("draft", "complaint", "letter", "response", "submission")):
        return True
    # All S4+ tasks that produce prose content
    content_producing_types = {
        "evidence_issue_map", "evidence_issue_map_bundle",
        "chronology_event_extraction", "source_verification",
        "production_mapping", "authority_map", "authority_map_expansion",
        "citation_audit", "hostile_review", "draft_preparation",
        "internal_repair", "evidence_triage", "complaint_draft",
        "letter_draft", "draft", "review_note", "evidence_organization_plan",
        "hostile_opponent_review", "privacy_review", "privacy_redaction_audit",
        "privacy_redaction_review", "privacy_redaction_verification",
        "redaction_review", "redaction_verification",
    }
    if task_type_l in content_producing_types:
        return True
    return False


def _safe_skill_id(skill_id: str) -> str:
    if not skill_id or "/" in skill_id or "\\" in skill_id or skill_id in {".", ".."}:
        raise KeyError(f"unknown skill: {skill_id}")
    return skill_id
