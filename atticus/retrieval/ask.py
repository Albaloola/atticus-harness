"""Read-only legal memory query foundation."""

from __future__ import annotations

from dataclasses import dataclass

from atticus.core.policies import enforce_read_only_intent
from atticus.db.repo import db_connection
from atticus.retrieval.citations import Citation
from atticus.retrieval.search import search_memory
from atticus.retrieval.trust import confidence, trust_level


@dataclass(frozen=True)
class AskAnswer:
    answer: str
    citations: list[Citation]
    trust_level: str
    confidence: str
    follow_up_task: str | None = None


def answer_question(db_path: str, question: str, *, worker_launcher: object | None = None) -> AskAnswer:
    """Answer from existing memory only.

    The worker_launcher parameter is intentionally unused. It exists so tests can
    prove ask mode does not launch or inspect execution machinery.
    """

    decision = enforce_read_only_intent(question)
    if not decision.allowed:
        return AskAnswer(
            answer=f"Blocked: {decision.reason}. Intent={decision.intent}. No workers were launched.",
            citations=[],
            trust_level="blocked",
            confidence="none",
            follow_up_task=None,
        )

    with db_connection(db_path, read_only=True) as conn:
        rows = search_memory(conn, question)

    citations = [
        Citation(
            citation_id=f"C{i}",
            record_type=row["record_type"],
            record_id=row["record_id"],
            path=row["path"],
            trust_status=row["trust_status"],
            stale=bool(row["stale"]),
            snippet=(row.get("content") or row.get("title") or row["path"])[:300],
        )
        for i, row in enumerate(rows, start=1)
    ]
    if not citations:
        return AskAnswer(
            answer="The answer is not safely supportable from current Atticus memory.",
            citations=[],
            trust_level="unsupported",
            confidence="low",
            follow_up_task="Create an explicit work order to inventory or extract the relevant sources.",
        )

    labels = ", ".join(f"[{c.citation_id}] {c.path} ({c.trust_status})" for c in citations)
    tl = trust_level(rows)
    conf = confidence(rows)
    if tl in {"candidate-only", "stale-or-mixed"}:
        answer = (
            "Current memory has potentially relevant material, but it is not certified. "
            f"Use it as a lead only: {labels}."
        )
    else:
        answer = f"Current memory contains relevant support: {labels}."
    return AskAnswer(answer=answer, citations=citations, trust_level=tl, confidence=conf)
