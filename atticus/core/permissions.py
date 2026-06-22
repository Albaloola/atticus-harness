"""Permission modes system — ported from Claude Code's permission infrastructure.

Provides a mode-based tool approval system with rule matching, bypass support,
and fine-grained allow/deny/ask behavior for tool execution decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypedDict


class AccessMode(str, Enum):
    """Tool permission operating modes.

    Values:
        DEFAULT:       Standard interaction — ask before potentially risky actions.
        ACCEPT_EDITS:  Auto-accept file editing operations.
        PLAN:          Plan-only mode — describe actions without executing.
        BYPASS:        Skip all permission prompts entirely.
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypass"


class PermissionBehavior(str, Enum):
    """Outcome of a permission check applied to a tool invocation.

    Values:
        ALLOW:  Proceed with the tool call. May carry an updated (rewritten) input.
        DENY:   Block the tool call. The message explains why.
        ASK:    Defer to the user. The message describes what will happen.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionRule:
    """A single permission rule that matches tool invocations.

    Attributes:
        tool_name: Name of the tool this rule applies to (e.g. 'Bash', 'Write').
        pattern:   Optional pattern to match against tool input. For Bash this is
                   typically the command string (e.g. 'git *'). Uses simple glob
                   matching. If ``None`` the rule matches any input for the tool.
        behavior:  The permission outcome when this rule matches.
        source:    Where the rule came from — 'alwaysAllow', 'alwaysDeny', or 'policy'.
    """

    tool_name: str
    behavior: PermissionBehavior
    source: str
    pattern: str | None = None


AccessRulesBySource = dict[str, list[PermissionRule]]
"""Maps a source identifier to its list of permission rules.

Typical keys: ``"alwaysAllow"``, ``"alwaysDeny"``, ``"policy"``.
"""


# ---------------------------------------------------------------------------
# PermissionResult — discriminated union via TypedDict + Literal
# ---------------------------------------------------------------------------

class _AllowResult(TypedDict):
    behavior: Literal["allow"]
    updated_input: dict[str, Any]


class _DenyResult(TypedDict):
    behavior: Literal["deny"]
    message: str


class _AskResult(TypedDict):
    behavior: Literal["ask"]
    message: str
    tool_name: str


PermissionResult = _AllowResult | _DenyResult | _AskResult
"""Discriminated union representing the outcome of a permission check.

- *allow*: ``{behavior: 'allow', updated_input: dict}``
- *deny*:  ``{behavior: 'deny',  message: str}``
- *ask*:   ``{behavior: 'ask',   message: str, tool_name: str}``
"""


# ---------------------------------------------------------------------------
# AccessContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccessContext:
    """Frozen snapshot of all permission state needed to decide a tool call.

    Attributes:
        mode:                          Current operating mode.
        always_allow_rules:            Rules that unconditionally allow matching calls.
        always_deny_rules:             Rules that unconditionally deny matching calls.
        always_ask_rules:              Rules that force a user prompt for matching calls.
        is_bypass_available:           Whether bypass mode is permitted in this session.
        should_avoid_permission_prompts: If ``True``, the system tries to resolve every
                                       check without showing a prompt (ASK falls through
                                       to ALLOW in bypass mode).
    """

    mode: AccessMode
    always_allow_rules: AccessRulesBySource = field(default_factory=dict)
    always_deny_rules: AccessRulesBySource = field(default_factory=dict)
    always_ask_rules: AccessRulesBySource = field(default_factory=dict)
    is_bypass_available: bool = False
    should_avoid_permission_prompts: bool = False


# ---------------------------------------------------------------------------
# Simple inline glob matching (stdlib only, no fnmatch)
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> str:
    """Convert a simple glob pattern to a regex for matching.

    Handles ``*`` (any sequence) and ``?`` (single char).
    """
    result: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            result.append(".*")
        elif ch == "?":
            result.append(".")
        elif ch in r".+^${}()|[]\\":
            result.append("\\" + ch)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def matches_rule(rule: PermissionRule, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Determine whether a permission rule matches a tool invocation.

    Matching logic (AND joined):

    1. ``rule.tool_name`` must equal *tool_name* (case-insensitive).
    2. If ``rule.pattern`` is set, the tool input must satisfy the glob.

    Args:
        rule:       The rule to test.
        tool_name:  Name of the tool being invoked.
        tool_input: The full tool input dictionary.

    Returns:
        ``True`` if the rule applies to this invocation.
    """
    # Tool name match (case-insensitive, like Claude Code)
    if rule.tool_name.lower() != tool_name.lower():
        return False

    # No pattern → matches any input for this tool
    if rule.pattern is None:
        return True

    # Pattern match: flatten the tool input into a single string for comparison.
    # For Bash this is typically the 'command' key; for others we use str().
    candidate = tool_input.get("command", "")
    if not candidate:
        candidate = str(tool_input)

    regex = _glob_to_regex(rule.pattern)
    return re.fullmatch(regex, candidate) is not None


# ---------------------------------------------------------------------------
# Core permission resolver
# ---------------------------------------------------------------------------

def check_tool_permission(
    tool_name: str, tool_input: dict[str, Any], context: AccessContext
) -> PermissionResult:
    """Evaluate whether a tool call should be allowed, denied, or deferred.

    **Resolution order** (first match wins):

    1. **Always-deny rules** — iterate all sources. If any rule matches → DENY.
    2. **Always-ask rules** — iterate all sources. If any rule matches → ASK.
    3. **Always-allow rules** — iterate all sources. If any rule matches → ALLOW.
    4. **Fallback**:
       - Non-bypass mode (or bypass unavailable) → ASK.
       - Bypass mode (and bypass available) → ALLOW.

    Args:
        tool_name:  Name of the tool being invoked (e.g. ``"Bash"``).
        tool_input: The tool's input parameters as a dictionary.
        context:    The frozen permission context carrying mode and rules.

    Returns:
        A :class:`PermissionResult` discriminated union.
    """

    def _match_in_sources(rules_by_source: AccessRulesBySource) -> PermissionRule | None:
        """Return the first matching rule across all sources, or None."""
        for _source, rules in rules_by_source.items():
            for rule in rules:
                if matches_rule(rule, tool_name, tool_input):
                    return rule
        return None

    # 1. Deny rules
    deny_match = _match_in_sources(context.always_deny_rules)
    if deny_match is not None:
        return _DenyResult(
            behavior="deny",
            message=f"Blocked by always-deny rule from '{deny_match.source}'",
        )

    # 2. Ask rules
    ask_match = _match_in_sources(context.always_ask_rules)
    if ask_match is not None:
        return _AskResult(
            behavior="ask",
            message=f"Requires user confirmation per rule from '{ask_match.source}'",
            tool_name=tool_name,
        )

    # 3. Allow rules
    allow_match = _match_in_sources(context.always_allow_rules)
    if allow_match is not None:
        return _AllowResult(behavior="allow", updated_input=tool_input)

    # 4. Fallback
    if is_bypass_available(context):
        return _AllowResult(behavior="allow", updated_input=tool_input)

    return _AskResult(
        behavior="ask",
        message=f"Tool '{tool_name}' requires permission approval",
        tool_name=tool_name,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_empty_permission_context() -> AccessContext:
    """Return a default permission context with no rules.

    Mode is :attr:`AccessMode.DEFAULT` and all rule collections are empty.
    """
    return AccessContext(mode=AccessMode.DEFAULT)


def is_bypass_available(context: AccessContext) -> bool:
    """Return ``True`` if bypass mode is both permitted and currently active.

    Requires **both**:
    - ``context.is_bypass_available`` is ``True`` (session allows bypass).
    - ``context.mode`` is :attr:`AccessMode.BYPASS`.
    """
    return context.is_bypass_available and context.mode == AccessMode.BYPASS
