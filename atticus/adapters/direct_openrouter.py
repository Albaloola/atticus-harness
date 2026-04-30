"""Direct OpenRouter adapter for explicit live provider-backed work."""

from __future__ import annotations

import json
from typing import Protocol, cast

from atticus.adapters.base import ExecutionAdapter
from atticus.context.sections import UNTRUSTED_EVIDENCE_BOUNDARY
from atticus.providers.openrouter import OpenRouterClient
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION, result_packet_json_schema


class ChatJsonClient(Protocol):
    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]: ...


class DirectOpenRouterAdapter(ExecutionAdapter):
    name: str = "direct_openrouter"

    def __init__(self, *, client: object | None = None, timeout_seconds: float | None = None) -> None:
        client_obj = client or OpenRouterClient(timeout=timeout_seconds or 120.0)
        if client is not None and timeout_seconds is not None and hasattr(client_obj, "timeout"):
            setattr(client_obj, "timeout", timeout_seconds)
        self.client: ChatJsonClient = cast(ChatJsonClient, client_obj)

    def run(self, work_order: dict[str, object], *, model: str, max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded Atticus legal harness worker. Return only valid JSON. "
                    "Your output is candidate, not canonical; reducers decide what becomes trusted. "
                    "Use only matter-scoped context in the work order. "
                    f"{UNTRUSTED_EVIDENCE_BOUNDARY} "
                    "Do not return narrative analysis outside the JSON object. Keep broad evidence-map and source-review "
                    "packets compact: capture only the strongest supported findings first and propose bounded follow-up "
                    "tasks for expansion, gaps, or low-confidence OCR instead of exhausting the output budget. For broad "
                    "tasks, return at most 4 findings, 6 citations, 3 uncertainties, 3 risk_flags, 3 redaction_flags, "
                    "and 1 proposed_task. Keep summary under 600 characters, citation quotes under 180 characters, "
                    "finding text under 280 characters, and non-draft proposed_artifacts[0].content under 1200 characters. "
                    "Every proposed_artifacts[].path must be a relative candidate path like candidate/<task_id>/result.md; "
                    "never return an absolute filesystem path such as /home/... or a path containing '..'. "
                    "Draft artifacts, draft_complaint artifacts, and redacted_draft artifacts must contain complete replacement text; "
                    "do not use placeholders such as '[remaining unchanged]', '[conclusion unchanged]', or omitted sections. "
                    "If a complete draft cannot fit, return a drafting_note or scoped follow-up task instead of a partial draft artifact. "
                    "For redaction verification, treat the original unredacted artifact as comparison evidence only; an identifier in "
                    "the original is not a privacy defect unless the same unsafe identifier remains in the redacted target artifact. "
                    "Separate fact, law, procedure, inference, contradiction, and risk. Cite every factual, legal, procedural, "
                    "contradiction, or risk finding to an allowed context target, or mark it uncertain "
                    "or needs_research. For extracted/OCR source_materials, cite target_type='source' with the source_id; "
                    "do not cite generated extraction artifacts unless citation_targets explicitly allows the artifact. "
                    "If citation_ids is empty, never label a fact, law, procedure, risk, or contradiction as supported; "
                    "use reasoning_status='uncertain' or 'needs_research', or use finding_type='drafting_note' for task limitations. "
                    "If uncertainties, contradictions, risk_flags, or redaction_flags include citation_ids, every id must exist in citations. "
                    "Supported law findings must cite at least one allowed target_type='authority'; matter sources can support facts "
                    "about what happened, but they cannot by themselves prove a legal rule. "
                    "When auditing a draft, citation, or redaction issue, cite the draft/review artifact that contains the defect; "
                    "if the missing or fabricated target itself is absent from context, do not use an uncited supported contradiction. "
                    "Negative or absence findings about a reviewed source must cite that reviewed source, or be marked uncertain; "
                    "never assert a supported absence finding with empty citation_ids. "
                    "Absence of source_materials in this work order only means no task-specific source text was supplied; "
                    "it is not proof that the matter has no records, no evidence, or no support. "
                    "Use procedure only for source-supported legal, university, court, or administrative procedure; "
                    "use drafting_note with uncertain reasoning for harness limitations, task feasibility, tool availability, "
                    "OCR capability gaps, or operational next steps. Do not propose work requiring unconfigured external tools "
                    "or services such as cloud OCR, email, filing, upload, or contact workflows. "
                    "Do not invent citations, authorities, documents, dates, quotes, "
                    "amounts, admissions, deadlines, remedies, or procedural posture. Flag stale evidence, "
                    "weak support, contradictions, privacy/redaction concerns, and missing certifications. "
                    "Do not include quoted_text_hash unless the work order provides the exact SHA-256 hex digest; "
                    "never guess, summarize, or placeholder a hash. "
                    "Do not claim to file, send, serve, email, upload, contact, message, or perform external "
                    "legal actions. The JSON content object must exactly follow "
                    f"{RESULT_PACKET_SCHEMA_VERSION}. Every finding must have finding_id, finding_type, "
                    "citation_ids, confidence, and reasoning_status."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "work_order": work_order,
                        "required_result_packet_schema": result_packet_json_schema(),
                    },
                    sort_keys=True,
                ),
            },
        ]
        return self.client.chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
