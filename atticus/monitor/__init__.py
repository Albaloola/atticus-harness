"""Interactive terminal monitor for the Atticus harness.

Provides a curses-based TUI and a non-interactive ``--once --json`` mode.
"""

from atticus.monitor.state import MonitorState, build_monitor_state, run_once
from atticus.monitor.tui import run_tui, run_once_json
from atticus.monitor import actions

__all__ = [
    "MonitorState",
    "build_monitor_state",
    "run_once",
    "run_tui",
    "run_once_json",
    "actions",
]
