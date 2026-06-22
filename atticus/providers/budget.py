"""Budget gates for provider-backed work."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import cast

from atticus.db import repo


class BudgetExceeded(RuntimeError):
    """Raised when a hard budget gate would be exceeded."""


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    scope_type: str
    scope_id: str
    limit_usd: float | None
    spent_usd: float
    requested_usd: float
    remaining_usd: float | None
    reason: str


def budget_status(conn: sqlite3.Connection, *, scope_type: str, scope_id: str) -> BudgetDecision:
    row = cast(sqlite3.Row | None, cast(object, conn.execute(
        "SELECT limit_usd FROM budgets WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    ).fetchone()))
    spent = repo.budget_spent(conn, scope_type=scope_type, scope_id=scope_id)
    if row is None:
        return BudgetDecision(True, scope_type, scope_id, None, spent, 0.0, None, "no budget configured")
    limit = float(str(row["limit_usd"]))
    return BudgetDecision(True, scope_type, scope_id, limit, spent, 0.0, limit - spent, "budget configured")


def check_budget(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    requested_usd: float,
) -> BudgetDecision:
    row = cast(sqlite3.Row | None, cast(object, conn.execute(
        "SELECT limit_usd, hard_stop FROM budgets WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    ).fetchone()))
    spent = repo.budget_spent(conn, scope_type=scope_type, scope_id=scope_id)
    if row is None:
        return BudgetDecision(True, scope_type, scope_id, None, spent, requested_usd, None, "no budget configured")
    limit = float(str(row["limit_usd"]))
    remaining = limit - spent
    allowed = spent + requested_usd <= limit or not bool(row["hard_stop"])
    reason = "within budget" if allowed else "hard budget limit would be exceeded"
    return BudgetDecision(allowed, scope_type, scope_id, limit, spent, requested_usd, remaining, reason)


def require_budget(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    requested_usd: float,
) -> BudgetDecision:
    decision = check_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=requested_usd)
    if not decision.allowed:
        _ = repo.record_human_attention(
            conn,
            target_type=scope_type,
            target_id=scope_id,
            severity="blocker",
            reason=decision.reason,
        )
        raise BudgetExceeded(decision.reason)
    return decision


def charge_budget(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    amount_usd: float,
    provider_run_id: str | None = None,
) -> str | None:
    row = cast(sqlite3.Row | None, cast(object, conn.execute(
        "SELECT budget_id FROM budgets WHERE scope_type = ? AND scope_id = ?",
        (scope_type, scope_id),
    ).fetchone()))
    if row is None:
        return None
    decision = check_budget(conn, scope_type=scope_type, scope_id=scope_id, requested_usd=amount_usd)
    if not decision.allowed:
        raise BudgetExceeded(decision.reason)
    return repo.add_budget_entry(
        conn,
        budget_id=str(row["budget_id"]),
        amount_usd=amount_usd,
        provider_run_id=provider_run_id,
    )
