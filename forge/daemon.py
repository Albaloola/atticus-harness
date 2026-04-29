"""Bounded Forge loop daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import time

from forge.loop.run_one import run_one
from forge.state import read_state, stop_requested, update_state


def loop(
    repo: Path,
    *,
    policy: str = "default",
    engine_command: str | None = None,
    shell_engine_command: str | None = None,
    offline_review: bool = False,
    delay_seconds: int | None = None,
    max_iterations: int | None = None,
) -> dict[str, object]:
    from forge.config import load_config

    config = load_config(repo, policy=policy, engine_command=engine_command)
    delay = config.loop.delay_seconds if delay_seconds is None else delay_seconds
    failures = 0
    completed = 0
    update_state(repo, running=True)
    while not stop_requested(repo):
        if max_iterations is not None and completed >= max_iterations:
            break
        if completed >= config.loop.max_iterations_per_day:
            break
        if failures >= config.loop.max_consecutive_failures:
            break
        state = read_state(repo)
        if float(state.get("cost_today") or 0.0) > config.loop.daily_cost_limit_usd:
            break
        try:
            packet = run_one(repo, policy=policy, engine_command=engine_command, shell_engine_command=shell_engine_command, offline_review=offline_review)
            completed += 1
            failures = 0 if packet.final_decision == "committed" else failures + 1
        except Exception:
            failures += 1
        if stop_requested(repo):
            break
        time.sleep(max(0, delay))
    update_state(repo, running=False, consecutive_failures=failures, loop_stopped_at=datetime.now(UTC).replace(microsecond=0).isoformat())
    return {"iterations": completed, "consecutive_failures": failures, "stopped": stop_requested(repo)}
