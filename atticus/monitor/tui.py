"""Interactive terminal monitor for the Atticus harness.

Two entry points:

* ``run_tui()`` — curses-based interactive loop (requires a TTY).
* ``run_once_json()`` — non-interactive JSON dump (CI / logging).
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import shlex
import sqlite3
import sys
import textwrap
from typing import cast

from atticus.monitor.state import MonitorState, build_monitor_state, run_once
from atticus.monitor import actions
from atticus.db import repo

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_tui(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    db_path: str,
    output_dir: str = "OUT",
    refresh_seconds: int = 2,
) -> int:
    """Open an interactive curses terminal monitor.

    Falls back to ``--once`` text output if curses is unavailable.
    """
    try:
        import curses
    except ImportError:
        print("curses not available; falling back to --once mode", file=sys.stderr)
        result = run_once(conn, matter_scope=matter_scope, db_path=db_path, output_dir=output_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    return curses.wrapper(
        _curses_main,
        conn,
        matter_scope=matter_scope,
        db_path=db_path,
        output_dir=output_dir,
        refresh_seconds=refresh_seconds,
    )


def run_once_json(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    db_path: str,
    output_dir: str = "OUT",
) -> str:
    """Return the monitor state as a pretty-printed JSON string."""
    data = run_once(
        conn,
        matter_scope=matter_scope,
        db_path=db_path,
        output_dir=output_dir,
    )
    return json.dumps(data, indent=2, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Curses main loop
# ---------------------------------------------------------------------------


def _curses_main(
    stdscr,
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    db_path: str,
    output_dir: str,
    refresh_seconds: int,
) -> int:
    import curses

    curses.curs_set(0)  # hide cursor
    stdscr.nodelay(1)  # non-blocking getch
    stdscr.keypad(True)
    curses.use_default_colors()

    # Colour pairs
    curses.init_pair(1, curses.COLOR_CYAN, -1)      # headers / titles
    curses.init_pair(2, curses.COLOR_GREEN, -1)     # good state
    curses.init_pair(3, curses.COLOR_YELLOW, -1)    # warning
    curses.init_pair(4, curses.COLOR_RED, -1)       # error / blocked
    curses.init_pair(5, curses.COLOR_WHITE, -1)     # normal text
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)  # reversed

    state = _refresh_state(conn, matter_scope, db_path, output_dir)
    frame = 0
    while True:
        state = _refresh_state(conn, matter_scope, db_path, output_dir)
        _draw_all(stdscr, state, frame)
        curses.doupdate()

        key = _get_key(stdscr, timeout_ms=refresh_seconds * 1000)
        if key is None:
            frame += 1
            continue

        action_result = _handle_key(
            key,
            conn=conn,
            state=state,
            db_path=db_path,
            matter_scope=matter_scope,
            output_dir=output_dir,
            stdscr=stdscr,
        )
        if action_result == "exit":
            break
        if action_result == "refresh":
            state = _refresh_state(conn, matter_scope, db_path, output_dir)

        frame += 1

    return 0


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _draw_all(stdscr, state: MonitorState, frame: int) -> None:
    import curses

    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    panel = state.panel

    # -- Title bar (row 0) --
    title = f" Atticus Monitor — {state.matter} "
    _draw_bar(stdscr, 0, cols, title, curses.color_pair(1) | curses.A_BOLD)
    _draw_refresh_bar(stdscr, 1, cols, state, panel)

    # -- Main body --
    y = 2
    if y >= rows - 1:
        return

    # Two-column layout: Matter (left) | Next action (right)
    col_mid = cols // 2
    body_height = max(4, (rows - y) // 3)

    # Left column: Matter counts
    y = _draw_matter_panel(stdscr, y, col_mid, panel)
    # Right column: Next action
    y = _draw_next_action_panel(stdscr, 2, col_mid + 1, cols - col_mid - 1, panel)

    # Events / worker activity
    y = max(y, 2 + body_height + 1)
    _draw_events_panel(stdscr, y, cols, state)

    # -- Action bar (bottom) --
    _draw_action_bar(stdscr, rows - 1, cols)

    # Frame counter
    try:
        stdscr.addstr(0, cols - 12, f"  [{frame}]", curses.A_DIM)
    except curses.error:
        pass


def _draw_refresh_bar(stdscr, y: int, cols: int, state: MonitorState, panel: Mapping) -> None:
    import curses

    sp = panel.get("state", "unknown")
    state_col = curses.color_pair(2)
    if sp in ("blocked", "needs_human_answer"):
        state_col = curses.color_pair(4)
    elif sp == "needs_legal_review":
        state_col = curses.color_pair(3)

    fg = panel.get("final_gate", {})
    fg_state = str(fg.get("state", "unknown")) if isinstance(fg, Mapping) else "unknown"
    agent = panel.get("agent_packet", {})
    live = ""
    if isinstance(agent, Mapping):
        live = " | Provider: " + ("ready" if agent.get("requires_live_provider") else "not required")

    text = f" State: {sp}  |  Final gate: {fg_state}{live}"
    try:
        stdscr.addnstr(y, 0, text[:cols - 1], cols - 1, state_col)
    except curses.error:
        pass


def _draw_matter_panel(stdscr, y: int, width: int, panel: Mapping) -> int:
    import curses

    counts = panel.get("counts", {})
    if not isinstance(counts, Mapping):
        counts = {}

    _draw_label(stdscr, y, 0, " Matter ", curses.color_pair(1) | curses.A_BOLD)
    y += 1
    items = [
        ("runnable", "runnable", 2),
        ("failed", "failed", 4),
        ("blocked", "blocked", 4),
        ("reducer_pending", "reducer", 3),
        ("human_attention_current", "human", 3),
    ]
    for key, label, colour in items:
        val = counts.get(key, 0)
        if isinstance(val, (int, float)):
            line = f"  {label}: {val}"
            stdscr.addnstr(y, 0, line, width, curses.color_pair(colour) if colour != 5 else curses.A_NORMAL)
            y += 1

    return y


def _draw_next_action_panel(stdscr, y: int, x: int, width: int, panel: Mapping) -> int:
    import curses

    _draw_label(stdscr, y, x, " Next action ", curses.color_pair(1) | curses.A_BOLD)
    y += 1

    action = panel.get("next_action", {})
    if not isinstance(action, Mapping):
        return y

    rows_data = [
        ("owner", str(action.get("owner", ""))),
        ("type", str(action.get("type", ""))),
        ("reason", str(action.get("reason", ""))[:60]),
    ]
    for label, value in rows_data:
        line = f"  {label}: {value}"
        try:
            stdscr.addnstr(y, x, line[:width], width)
        except curses.error:
            pass
        y += 1

    command = action.get("resume_command", "")
    if command:
        cmd_display = str(command)[:width - 4]
        try:
            stdscr.addnstr(y, x, f"  cmd: {cmd_display}", width, curses.A_DIM)
        except curses.error:
            pass
        y += 1

    return y


def _draw_events_panel(stdscr, y: int, cols: int, state: MonitorState) -> None:
    import curses

    _draw_label(stdscr, y, 0, " Events / activity ", curses.color_pair(1) | curses.A_BOLD)
    y += 1

    events = state.recent_events
    rows, _ = stdscr.getmaxyx()
    max_rows = rows - y - 2  # leave room for action bar
    if max_rows <= 0:
        return

    if not events:
        try:
            stdscr.addnstr(y, 0, "  (no recent events)", cols - 1, curses.A_DIM)
        except curses.error:
            pass
        return

    # Show the most recent events that fit
    shown = 0
    for evt in reversed(events):
        if shown >= max_rows:
            break
        if not isinstance(evt, Mapping):
            continue
        ts = str(evt.get("created_at", ""))[11:19]  # HH:MM:SS
        etype = str(evt.get("event_type", ""))
        line = f"  {ts} {etype}"
        try:
            stdscr.addnstr(y, 0, line[:cols - 1], cols - 1)
        except curses.error:
            pass
        y += 1
        shown += 1


def _draw_action_bar(stdscr, y: int, cols: int) -> None:
    import curses

    actions_text = (
        " [r]esume  [p]ause/stop  [q]uestion  [a]nswer  [g]ate"
        "  [v]reviews  [l]eases  [e]vents  [c]ommand  [R]efresh  [h]elp  [x]exit"
    )
    try:
        stdscr.addnstr(y, 0, actions_text[:cols - 1], cols - 1, curses.A_REVERSE)
    except curses.error:
        pass


def _draw_bar(stdscr, y: int, cols: int, text: str, attr) -> None:
    import curses

    try:
        stdscr.addnstr(y, 0, text[:cols - 1], cols - 1, attr)
    except curses.error:
        pass


def _draw_label(stdscr, y: int, x: int, text: str, attr) -> None:
    try:
        stdscr.addnstr(y, x, text, len(text), attr)
    except curses.error:
        pass


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


_KEY_ACTIONS: dict[str, str] = {
    "r": "resume",
    "p": "stop",
    "q": "question",
    "a": "answer",
    "g": "final_gate",
    "v": "reducer_reviews",
    "l": "leases",
    "e": "events",
    "c": "command",
    "R": "refresh",
    "h": "help",
    "x": "exit",
}


def _get_key(stdscr, *, timeout_ms: int) -> str | None:
    """Read a keypress with the given timeout.

    Returns a key action name or None on timeout.
    """
    import curses

    # curses.halfdelay works in tenths of seconds
    curses.halfdelay(max(1, timeout_ms // 100))

    try:
        ch = stdscr.getch()
    except KeyboardInterrupt:
        return "exit"

    if ch in (-1, curses.ERR):
        return None

    # Map keycode to action
    try:
        char = chr(ch)
    except (ValueError, OverflowError):
        return None

    return _KEY_ACTIONS.get(char)


def _handle_key(
    key: str,
    *,
    conn: sqlite3.Connection,
    state: MonitorState,
    db_path: str,
    matter_scope: str,
    output_dir: str,
    stdscr,
) -> str:
    """Dispatch a key action.  Returns 'exit', 'refresh', or None."""
    import curses

    if key == "exit":
        return "exit"

    if key == "refresh":
        return "refresh"

    if key == "help":
        _show_help_screen(stdscr)
        return None

    if key == "resume":
        result = actions.action_resume(
            conn, state=state, db_path=db_path, output_dir=output_dir,
        )
        if result.get("can_run") and not result.get("confirmation_required"):
            command = str(result.get("command", ""))
            if command and _confirm_action(stdscr, f"Run: {result.get('summary', command)[:60]}"):
                _flash_message(stdscr, f"Executing: {command[:60]}...")
                import subprocess as _sp
                try:
                    proc = _sp.run(shlex.split(command), capture_output=True, text=True, timeout=120)
                    _show_action_result(
                        stdscr, "Resume result",
                        {"summary": f"exit code {proc.returncode}", "stdout": proc.stdout[-200:], "stderr": proc.stderr[-200:]},
                    )
                except _sp.TimeoutExpired:
                    _show_action_result(stdscr, "Resume timeout", {"summary": "Command timed out after 120s."})
                except Exception as exc:
                    _show_action_result(stdscr, "Resume error", {"summary": str(exc)})
            elif not command:
                _show_action_result(stdscr, "Resume", result)
        elif result.get("can_run") and result.get("confirmation_required"):
            _show_action_result(stdscr, "Resume", result)
        else:
            _show_action_result(stdscr, "Resume", result)
        return "refresh"

    if key == "stop":
        result = actions.action_stop(
            conn, state=state, db_path=db_path, matter_scope=matter_scope,
        )
        if result.get("can_run") and _confirm_action(stdscr, str(result.get("summary", ""))):
            exec_result = actions.action_execute_stop(
                conn,
                run_id=str(result.get("stop_run_id", "")),
                reason="operator requested stop from monitor",
            )
            _show_action_result(stdscr, "Stop executed", exec_result)
            conn.commit()
        else:
            _show_action_result(stdscr, "Stop", result)
        return "refresh"

    if key == "question":
        _show_human_question(stdscr, state)
        return None

    if key == "answer":
        _answer_human_request(stdscr, conn, state, db_path, matter_scope)
        return "refresh"

    if key == "final_gate":
        result = actions.action_show_final_gate(state)
        _show_action_result(stdscr, "Final gate", result)
        return None

    if key == "reducer_reviews":
        result = actions.action_show_reducer_reviews(state)
        _show_action_result(stdscr, "Reducer reviews", result)
        return None

    if key == "leases":
        result = actions.action_show_leases(state)
        _show_action_result(stdscr, "Leases", result)
        return None

    if key == "events":
        result = {"summary": f"{len(state.recent_events)} recent events"}
        _show_action_result(stdscr, "Recent events", result)
        return None

    if key == "command":
        result = actions.action_show_command(state)
        _show_action_result(stdscr, "Next command", result)
        return None

    return "refresh"


def _show_human_question(stdscr, state: MonitorState) -> None:
    """Display the current human question in an overlay."""
    operator_request = state.panel.get("operator_request", {})
    human_request = state.human_request

    if not isinstance(operator_request, Mapping) or not operator_request:
        _show_action_result(stdscr, "Human question", {"summary": "No human question pending."})
        return

    attention_id = operator_request.get("attention_id", "?")
    question = str(operator_request.get("question", "No question text"))
    why = str(operator_request.get("why_needed", ""))
    acceptable = operator_request.get("acceptable_responses", [])
    if isinstance(acceptable, str):
        acceptable = []
    response_template = str(operator_request.get("response_command_template", ""))

    lines = [
        f"Human Question #{attention_id}",
        "",
        f"Question: {question}",
    ]
    if why:
        lines.append(f"Why needed: {why}")
    if acceptable:
        lines.append("")
        lines.append("Acceptable response types:")
        for resp in acceptable:
            lines.append(f"  - {resp}")
    if response_template:
        lines.append("")
        lines.append("Response command template:")
        lines.append(f"  {response_template}")
    lines.append("")
    lines.append("Press 'a' to submit an answer, or any other key to dismiss.")

    _show_overlay(stdscr, "Human question", "\n".join(lines))
    stdscr.nodelay(0)
    stdscr.getch()
    stdscr.nodelay(1)


def _answer_human_request(
    stdscr,
    conn: sqlite3.Connection,
    state: MonitorState,
    db_path: str,
    matter_scope: str,
) -> None:
    """Walk the user through answering a human request."""
    import curses

    result = actions.action_answer_human(
        conn, state=state, db_path=db_path, matter_scope=matter_scope,
    )
    if not result.get("can_run"):
        _show_action_result(stdscr, "Answer", result)
        return

    attention_id = result.get("attention_id", 0)
    question = str(result.get("question", "No question"))
    response_types = result.get("response_types", [])
    if isinstance(response_types, str):
        response_types = []

    # Show the question
    info_lines = [
        f"Attention #{attention_id}",
        "",
        f"Question: {question}",
    ]
    if response_types:
        info_lines.append("")
        info_lines.append("Acceptable response types:")
        for rt in response_types:
            info_lines.append(f"  - {rt}")
    info_lines.append("")
    info_lines.append("Press any key to continue with answer submission.")
    _show_overlay(stdscr, "Answer human request", "\n".join(info_lines))
    stdscr.nodelay(0)
    stdscr.getch()
    stdscr.nodelay(1)

    # Prompt for response type
    if response_types:
        chosen = _prompt_choice(stdscr, "Response type", response_types)
        if chosen is None:
            _flash_message(stdscr, "Answer cancelled.")
            return
    else:
        chosen = "provided_best_available"

    # Prompt for statement
    statement = _prompt_string(stdscr, "Statement (optional, press Enter to skip)")
    if statement is None:
        _flash_message(stdscr, "Answer cancelled.")
        return

    # Confirm
    confirm_text = (
        f"Submit response to attention #{attention_id}?\n"
        f"  Type: {chosen}\n"
        f"  Statement: {statement or '(none)'}\n"
        f"\nProceed? (y/N): "
    )
    if not _confirm_action(stdscr, confirm_text.split("\n")[0]):
        _flash_message(stdscr, "Answer cancelled.")
        return

    _show_overlay(stdscr, "Submitting", "Executing human-response submit...")

    from atticus.db import repo

    repo.record_human_response(
        conn,
        attention_id=attention_id,
        response_type=chosen,
        statement=statement,
    )
    conn.commit()

    _flash_message(stdscr, f"Answer submitted for attention #{attention_id}.")


def _prompt_choice(stdscr, title: str, options: list) -> str | None:
    """Show a numbered list and let the user pick one.  Returns the chosen value or None."""
    import curses

    lines = [f"{i + 1}. {opt}" for i, opt in enumerate(options)]
    lines.append("")
    lines.append("Enter number (1-{}) or any other key to cancel:".format(len(options)))
    _show_overlay(stdscr, title, "\n".join(lines))

    stdscr.nodelay(0)
    try:
        ch = stdscr.getch()
    except KeyboardInterrupt:
        return None
    finally:
        stdscr.nodelay(1)

    if ord("0") <= ch <= ord("9"):
        idx = ch - ord("0")
        if idx == 0:
            return None
        if 1 <= idx <= len(options):
            return options[idx - 1]
    return None


def _prompt_string(stdscr, prompt: str) -> str | None:
    """Show an overlay and read a line of text input.

    Returns the input string, ``""`` for empty+Enter, or ``None`` on ESC/cancel.
    """
    import curses

    rows, cols = stdscr.getmaxyx()
    overlay_y = rows // 2 - 2
    overlay_x = max(0, (cols - 60) // 2)

    sub = curses.newwin(5, 60, overlay_y, overlay_x)
    sub.box()
    sub.addnstr(1, 2, prompt[:56], 56)
    sub.addnstr(2, 2, "> ", 2)

    curses.curs_set(1)
    sub.nodelay(0)
    curses.echo()

    input_str = ""
    try:
        ch = sub.getch()
        if ch == curses.ERR or ch == 27:  # ESC
            return None
        input_str = chr(ch) if 32 <= ch <= 126 else ""
        # Read remaining chars until Enter
        while True:
            ch = sub.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                break
            if ch == 27:  # ESC
                return None
            if 32 <= ch <= 126:
                input_str += chr(ch)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                input_str = input_str[:-1]
    except KeyboardInterrupt:
        input_str = None
    finally:
        curses.noecho()
        curses.curs_set(0)
        sub.nodelay(1)

    return input_str


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------


def _show_action_result(stdscr, title: str, result: dict[str, object]) -> None:
    """Display an action result in an overlay, then wait for a keypress."""
    import curses

    _show_overlay(stdscr, title, str(result.get("summary", "")))
    stdscr.nodelay(0)
    stdscr.getch()
    stdscr.nodelay(1)


def _confirm_action(stdscr, prompt: str) -> bool:
    """Show a confirmation prompt.  Returns True if user presses 'y'."""
    import curses

    _show_overlay(stdscr, "Confirm", f"{prompt}\n\nProceed? (y/N): ")
    stdscr.nodelay(0)
    try:
        ch = stdscr.getch()
    except KeyboardInterrupt:
        return False
    finally:
        stdscr.nodelay(1)
    return ch in (ord("y"), ord("Y"))


def _flash_message(stdscr, msg: str) -> None:
    """Show a temporary message at the bottom."""
    import curses

    rows, cols = stdscr.getmaxyx()
    try:
        stdscr.addnstr(rows - 2, 0, f" {msg} ", cols - 1, curses.A_REVERSE)
        stdscr.refresh()
    except curses.error:
        pass


def _show_help_screen(stdscr) -> None:
    """Show the keyboard shortcut help overlay."""
    help_text = """KEYBOARD SHORTCUTS

  r  Resume / continue safe harness work
  p  Pause / stop current run
  q  Show genuine human question
  a  Submit answer to human request
  g  Show final-gate details
  v  Show reducer-review queue
  l  Show active leases
  e  Show recent events / logs
  c  Show exact next command
  R  Refresh now
  h  Show this help screen
  x  Exit monitor (does NOT stop the harness)

SAFETY
  - Stop/pause, human response, and reducer actions require
    explicit confirmation before execution.
  - High-risk reducer/legal auto-run is blocked.
  - Provider capability is shown as metadata only.
  - The monitor is read-only unless you confirm a write action.

Press any key to close.
"""
    _show_overlay(stdscr, "Help", help_text)
    stdscr.nodelay(0)
    stdscr.getch()
    stdscr.nodelay(1)


def _show_overlay(stdscr, title: str, body: str) -> None:
    """Show a centered overlay with the given title and body text."""
    import curses

    rows, cols = stdscr.getmaxyx()

    # Build lines
    lines = [f" {title} ".center(cols - 4)]
    for raw_line in body.split("\n"):
        wrapped = textwrap.wrap(raw_line, width=cols - 8) or [""]
        lines.extend(wrapped)

    overlay_height = min(len(lines) + 4, rows - 4)
    overlay_width = min(max(len(l) for l in lines) + 4, cols - 4)
    start_y = max(0, (rows - overlay_height) // 2)
    start_x = max(0, (cols - overlay_width) // 2)

    sub = curses.newwin(overlay_height, overlay_width, start_y, start_x)
    sub.box()
    for i, line in enumerate(lines[: overlay_height - 2]):
        try:
            sub.addnstr(i + 1, 2, line[: overlay_width - 4], overlay_width - 4)
        except curses.error:
            pass
    sub.refresh()


# ---------------------------------------------------------------------------
# State refresh helper
# ---------------------------------------------------------------------------


def _refresh_state(
    conn: sqlite3.Connection,
    matter_scope: str,
    db_path: str,
    output_dir: str,
) -> MonitorState:
    return build_monitor_state(
        conn,
        matter_scope=matter_scope,
        db_path=db_path,
        output_dir=output_dir,
    )
