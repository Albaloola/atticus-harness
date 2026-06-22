"""Vim-style keybinding state machine for the Atticus TUI monitor.

Port of Claude Code's src/vim/ state machine pattern: modes, motions,
operators, count prefixes, dot-repeat, and text object support.
Pure state machine — no curses dependency, usable by both TUI and agents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VimMode(Enum):
    NORMAL = "normal"
    INSERT = "insert"
    VISUAL = "visual"
    VISUAL_LINE = "visual_line"
    COMMAND = "command"


class VimOperator(Enum):
    DELETE = "d"
    YANK = "y"
    CHANGE = "c"


@dataclass(frozen=True)
class VimState:
    mode: VimMode = VimMode.NORMAL
    cursor_line: int = 0
    cursor_col: int = 0
    selection_start: tuple[int, int] | None = None
    selection_end: tuple[int, int] | None = None
    command_buffer: str = ""
    count_prefix: int | None = None
    pending_operator: VimOperator | None = None
    last_command: str | None = None
    text_lines: tuple[str, ...] = ()
    clipboard: str = ""
    prefix: str = ""


def handle_key(state: VimState, key: str) -> VimState:
    """Main dispatch: route keypress to the appropriate mode handler."""
    if state.mode == VimMode.NORMAL:
        return _handle_normal(state, key)
    if state.mode == VimMode.INSERT:
        return _handle_insert(state, key)
    if state.mode == VimMode.VISUAL:
        return _handle_visual(state, key)
    if state.mode == VimMode.VISUAL_LINE:
        return _handle_visual_line(state, key)
    if state.mode == VimMode.COMMAND:
        return _handle_command(state, key)
    return state


def enter_normal(state: VimState) -> VimState:
    return VimState(
        mode=VimMode.NORMAL,
        cursor_line=state.cursor_line,
        cursor_col=state.cursor_col,
        text_lines=state.text_lines,
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


def enter_insert(state: VimState) -> VimState:
    return VimState(
        mode=VimMode.INSERT,
        cursor_line=state.cursor_line,
        cursor_col=state.cursor_col,
        text_lines=state.text_lines,
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


def enter_visual(state: VimState) -> VimState:
    return VimState(
        mode=VimMode.VISUAL,
        cursor_line=state.cursor_line,
        cursor_col=state.cursor_col,
        selection_start=(state.cursor_line, state.cursor_col),
        text_lines=state.text_lines,
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


def enter_visual_line(state: VimState) -> VimState:
    return VimState(
        mode=VimMode.VISUAL_LINE,
        cursor_line=state.cursor_line,
        cursor_col=0,
        selection_start=(state.cursor_line, 0),
        selection_end=(state.cursor_line, _line_len(state.text_lines, state.cursor_line)),
        text_lines=state.text_lines,
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


def _handle_normal(state: VimState, key: str) -> VimState:
    # Multi-key motion prefix handling (e.g. 'gg')
    if state.prefix:
        motion = _MOTIONS.get(state.prefix + key)
        if motion is not None:
            new_line, new_col = motion(state, 1)
            return VimState(
                mode=VimMode.NORMAL,
                cursor_line=new_line,
                cursor_col=new_col,
                text_lines=state.text_lines,
                clipboard=state.clipboard,
                last_command=f"g{key}",
            )
        # Unrecognised two-key sequence — discard prefix and continue
        state = VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )

    if state.count_prefix is not None and key.isdigit():
        count = state.count_prefix * 10 + int(key)
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            count_prefix=count,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )

    if key.isdigit() and key != "0":
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            count_prefix=int(key),
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )

    count = state.count_prefix or 1

    if key == "i":
        return enter_insert(state)
    if key == "v":
        return enter_visual(state)
    if key == "V":
        return enter_visual_line(state)
    if key == ":":
        return VimState(
            mode=VimMode.COMMAND,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            command_buffer="",
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )
    if key == "." and state.last_command:
        return _replay_last(state)

    if state.pending_operator is not None:
        return _execute_pending_operator(state, key, count)

    operator = _OPERATORS.get(key)
    if operator is not None:
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            pending_operator=operator,
            count_prefix=count,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )

    motion = _MOTIONS.get(key)
    if motion is not None:
        new_line, new_col = motion(state, count)
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=new_line,
            cursor_col=new_col,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )

    # Check for multi-key motion prefix (e.g. 'g' starts 'gg')
    if key == "g":
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
            prefix="g",
        )

    return VimState(
        mode=VimMode.NORMAL,
        cursor_line=state.cursor_line,
        cursor_col=state.cursor_col,
        text_lines=state.text_lines,
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


def _replay_last(state: VimState) -> VimState:
    """Dot-repeat: replay the last normal-mode command character by character."""
    if state.last_command is None:
        return state
    s = state
    for ch in state.last_command:
        s = handle_key(s, ch)
    return s


# ── insert mode ─────────────────────────────────────


def _handle_insert(state: VimState, key: str) -> VimState:
    if key == "\x1b":
        return enter_normal(state)
    if key == "\x7f" or key == "\b":
        return _backspace(state)
    if key == "\n" or key == "\r":
        return _insert_newline(state)
    if len(key) == 1 and key.isprintable():
        return _insert_char(state, key)
    return state


def _insert_char(state: VimState, char: str) -> VimState:
    lines = list(state.text_lines)
    if not lines:
        lines = [""]
    if state.cursor_line >= len(lines):
        lines.append("")
        line = ""
    else:
        line = lines[state.cursor_line]
    col = min(state.cursor_col, len(line))
    new_line = line[:col] + char + line[col:]
    lines[state.cursor_line] = new_line
    return VimState(
        mode=VimMode.INSERT,
        cursor_line=state.cursor_line,
        cursor_col=col + 1,
        text_lines=tuple(lines),
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


def _backspace(state: VimState) -> VimState:
    lines = list(state.text_lines)
    if state.cursor_col > 0 and state.cursor_line < len(lines):
        line = lines[state.cursor_line]
        lines[state.cursor_line] = line[: state.cursor_col - 1] + line[state.cursor_col :]
        return VimState(
            mode=VimMode.INSERT,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col - 1,
            text_lines=tuple(lines),
            clipboard=state.clipboard,
            last_command=state.last_command,
        )
    return state


def _insert_newline(state: VimState) -> VimState:
    lines = list(state.text_lines)
    if not lines:
        lines = [""]
    if state.cursor_line < len(lines):
        line = lines[state.cursor_line]
        before = line[: state.cursor_col]
        after = line[state.cursor_col :]
        lines[state.cursor_line] = before
        lines.insert(state.cursor_line + 1, after)
    else:
        lines.append("")
    return VimState(
        mode=VimMode.INSERT,
        cursor_line=state.cursor_line + 1,
        cursor_col=0,
        text_lines=tuple(lines),
        clipboard=state.clipboard,
        last_command=state.last_command,
    )


# ── visual mode ─────────────────────────────────────


def _handle_visual(state: VimState, key: str) -> VimState:
    if key == "\x1b":
        return enter_normal(state)

    if state.pending_operator is None:
        operator = _OPERATORS.get(key)
        if operator is not None:
            start = state.selection_start or (state.cursor_line, state.cursor_col)
            end = (state.cursor_line, state.cursor_col)
            return _execute_visual_operator(state, operator, start, end)

        motion = _MOTIONS.get(key)
        if motion is not None:
            new_line, new_col = motion(state, 1)
            return VimState(
                mode=VimMode.VISUAL,
                cursor_line=new_line,
                cursor_col=new_col,
                selection_start=state.selection_start,
                selection_end=(new_line, new_col),
                text_lines=state.text_lines,
                clipboard=state.clipboard,
                last_command=state.last_command,
            )
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )

    # Execute pending operator on visual selection
    op = state.pending_operator
    start = state.selection_start or (0, 0)
    end = state.selection_end or (state.cursor_line, state.cursor_col)
    return _execute_visual_operator(state, op, start, end)


def _execute_visual_operator(
    state: VimState,
    op: VimOperator,
    start: tuple[int, int],
    end: tuple[int, int],
) -> VimState:
    lines = list(state.text_lines)
    lo_line = min(start[0], end[0])
    hi_line = max(start[0], end[0])

    if op == VimOperator.YANK:
        selection = _extract_selection(lines, start, end)
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=lo_line,
            cursor_col=0,
            text_lines=tuple(lines),
            clipboard=selection,
            last_command=f"v{op.value}",
        )

    if op in (VimOperator.DELETE, VimOperator.CHANGE):
        deleted = _extract_selection(lines, start, end)
        for i in range(hi_line, lo_line - 1, -1):
            del lines[i]
        if not lines:
            lines = [""]
        new_line = min(lo_line, len(lines) - 1)
        target_mode = VimMode.INSERT if op == VimOperator.CHANGE else VimMode.NORMAL
        return VimState(
            mode=target_mode,
            cursor_line=new_line,
            cursor_col=0,
            text_lines=tuple(lines),
            clipboard=deleted,
            last_command=f"v{op.value}",
        )

    return enter_normal(state)


def _handle_visual_line(state: VimState, key: str) -> VimState:
    if key == "\x1b":
        return enter_normal(state)

    if state.pending_operator is None:
        operator = _OPERATORS.get(key)
        if operator is not None:
            start = state.selection_start or (state.cursor_line, 0)
            end = state.selection_end or (state.cursor_line, _line_len(state.text_lines, state.cursor_line))
            return _execute_visual_operator(state, operator, start, end)

        if key == "j":
            new_line = min(state.cursor_line + 1, len(state.text_lines) - 1)
            return VimState(
                mode=VimMode.VISUAL_LINE,
                cursor_line=new_line,
                cursor_col=0,
                selection_start=state.selection_start,
                selection_end=(new_line, _line_len(state.text_lines, new_line)),
                text_lines=state.text_lines,
                clipboard=state.clipboard,
                last_command=state.last_command,
            )
        if key == "k":
            new_line = max(state.cursor_line - 1, 0)
            return VimState(
                mode=VimMode.VISUAL_LINE,
                cursor_line=new_line,
                cursor_col=0,
                selection_start=state.selection_start,
                selection_end=(new_line, _line_len(state.text_lines, new_line)),
                text_lines=state.text_lines,
                clipboard=state.clipboard,
                last_command=state.last_command,
            )
        return enter_normal(state)

    op = state.pending_operator
    start = state.selection_start or (0, 0)
    end = state.selection_end or (state.cursor_line, _line_len(state.text_lines, state.cursor_line))
    return _execute_visual_operator(state, op, start, end)


# ── command mode ────────────────────────────────────


def _handle_command(state: VimState, key: str) -> VimState:
    if key == "\x1b":
        return enter_normal(state)
    if key == "\n" or key == "\r":
        return enter_normal(state)
    if key == "\x7f" or key == "\b":
        new_buf = state.command_buffer[:-1]
        return VimState(
            mode=VimMode.COMMAND,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            command_buffer=new_buf,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )
    if len(key) == 1 and key.isprintable():
        return VimState(
            mode=VimMode.COMMAND,
            cursor_line=state.cursor_line,
            cursor_col=state.cursor_col,
            command_buffer=state.command_buffer + key,
            text_lines=state.text_lines,
            clipboard=state.clipboard,
            last_command=state.last_command,
        )
    return state


def _execute_pending_operator(state: VimState, key: str, count: int) -> VimState:
    op = state.pending_operator
    if op is None:
        return enter_normal(state)

    if key == op.value:
        return _execute_line_operator(state, op, count)

    motion = _MOTIONS.get(key)
    if motion is not None:
        target_line, _target_col = motion(state, count)
        return _execute_range_operator(state, op, target_line)

    return enter_normal(state)


def _execute_line_operator(state: VimState, op: VimOperator, count: int) -> VimState:
    lines = list(state.text_lines)
    start = state.cursor_line
    end = min(start + count - 1, len(lines) - 1)

    if op == VimOperator.YANK:
        yanked = "\n".join(lines[start : end + 1])
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=0,
            text_lines=tuple(lines),
            clipboard=yanked,
            last_command=f"{count}{op.value}{op.value}",
        )

    if op in (VimOperator.DELETE, VimOperator.CHANGE):
        for i in range(end, start - 1, -1):
            del lines[i]
        if not lines:
            lines = [""]
        new_line = min(start, len(lines) - 1)
        target_mode = VimMode.INSERT if op == VimOperator.CHANGE else VimMode.NORMAL
        return VimState(
            mode=target_mode,
            cursor_line=new_line,
            cursor_col=0,
            text_lines=tuple(lines),
            clipboard=state.clipboard,
            last_command=f"{count}{op.value}{op.value}",
        )

    return enter_normal(state)


def _execute_range_operator(state: VimState, op: VimOperator, target_line: int) -> VimState:
    lines = list(state.text_lines)
    start = min(state.cursor_line, target_line)
    end = max(state.cursor_line, target_line)

    if op == VimOperator.YANK:
        yanked = "\n".join(lines[start : end + 1])
        return VimState(
            mode=VimMode.NORMAL,
            cursor_line=state.cursor_line,
            cursor_col=0,
            text_lines=tuple(lines),
            clipboard=yanked,
            last_command=f"{op.value}j",
        )

    if op in (VimOperator.DELETE, VimOperator.CHANGE):
        for i in range(end, start - 1, -1):
            del lines[i]
        if not lines:
            lines = [""]
        new_line = min(start, len(lines) - 1)
        target_mode = VimMode.INSERT if op == VimOperator.CHANGE else VimMode.NORMAL
        return VimState(
            mode=target_mode,
            cursor_line=new_line,
            cursor_col=0,
            text_lines=tuple(lines),
            clipboard=state.clipboard,
            last_command=f"{op.value}j",
        )

    return enter_normal(state)


# ── motions ─────────────────────────────────────────

MotionFunc = Callable[[VimState, int], tuple[int, int]]


def _move_left(state: VimState, count: int) -> tuple[int, int]:
    new_col = max(0, state.cursor_col - count)
    return state.cursor_line, new_col


def _move_down(state: VimState, count: int) -> tuple[int, int]:
    max_line = len(state.text_lines) - 1
    new_line = min(max_line, state.cursor_line + count)
    line_len = _line_len(state.text_lines, new_line)
    return new_line, min(state.cursor_col, line_len)


def _move_up(state: VimState, count: int) -> tuple[int, int]:
    new_line = max(0, state.cursor_line - count)
    line_len = _line_len(state.text_lines, new_line)
    return new_line, min(state.cursor_col, line_len)


def _move_right(state: VimState, count: int) -> tuple[int, int]:
    line_len = _line_len(state.text_lines, state.cursor_line)
    new_col = min(line_len, state.cursor_col + count)
    return state.cursor_line, new_col


def _next_word(state: VimState, count: int) -> tuple[int, int]:
    line = _safe_line(state.text_lines, state.cursor_line)
    col = state.cursor_col
    for _ in range(count):
        while col < len(line) and line[col].isalnum():
            col += 1
        while col < len(line) and not line[col].isalnum() and line[col] != " ":
            col += 1
        while col < len(line) and line[col] == " ":
            col += 1
    return state.cursor_line, min(col, len(line))


def _prev_word(state: VimState, count: int) -> tuple[int, int]:
    line = _safe_line(state.text_lines, state.cursor_line)
    col = state.cursor_col
    for _ in range(count):
        while col > 0 and line[col - 1] == " ":
            col -= 1
        while col > 0 and line[col - 1].isalnum():
            col -= 1
    return state.cursor_line, max(0, col)


def _start_of_line(state: VimState, _count: int) -> tuple[int, int]:
    return state.cursor_line, 0


def _end_of_line(state: VimState, _count: int) -> tuple[int, int]:
    return state.cursor_line, _line_len(state.text_lines, state.cursor_line)


def _start_of_file(state: VimState, _count: int) -> tuple[int, int]:
    return 0, 0


def _end_of_file(state: VimState, _count: int) -> tuple[int, int]:
    last = max(0, len(state.text_lines) - 1)
    return last, _line_len(state.text_lines, last)


# ── helpers ─────────────────────────────────────────


def _safe_line(lines: tuple[str, ...], idx: int) -> str:
    if 0 <= idx < len(lines):
        return lines[idx]
    return ""


def _line_len(lines: tuple[str, ...], idx: int) -> int:
    if 0 <= idx < len(lines):
        return len(lines[idx])
    return 0


def _extract_selection(
    lines: list[str], start: tuple[int, int], end: tuple[int, int]
) -> str:
    lo_line = min(start[0], end[0])
    hi_line = max(start[0], end[0])
    if lo_line == hi_line:
        lo_col = min(start[1], end[1])
        hi_col = max(start[1], end[1])
        return lines[lo_line][lo_col:hi_col]
    parts = [lines[lo_line][min(start[1], end[1]) :]]
    for i in range(lo_line + 1, hi_line):
        parts.append(lines[i])
    parts.append(lines[hi_line][: max(start[1], end[1])])
    return "\n".join(parts)


# ── motion and operator registries ──────────────────

_MOTIONS: dict[str, MotionFunc] = {
    "h": _move_left,
    "j": _move_down,
    "k": _move_up,
    "l": _move_right,
    "w": _next_word,
    "b": _prev_word,
    "0": _start_of_line,
    "$": _end_of_line,
    "gg": _start_of_file,
    "G": _end_of_file,
}

_OPERATORS: dict[str, VimOperator] = {
    "d": VimOperator.DELETE,
    "y": VimOperator.YANK,
    "c": VimOperator.CHANGE,
}
