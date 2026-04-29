"""JSON schema snippets for OpenRouter responses."""

REVIEWER_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "verdict": {"type": "string"},
        "confidence": {"type": "number"},
        "risk_level": {"type": "string"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "non_blocking_issues": {"type": "array", "items": {"type": "string"}},
        "recommended_repairs": {"type": "array", "items": {"type": "string"}},
        "files_of_concern": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["role", "verdict", "confidence", "risk_level", "blocking_issues", "non_blocking_issues", "recommended_repairs", "files_of_concern"],
    "additionalProperties": False,
}
