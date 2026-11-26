"""Interactive session resume picker modal for Hermes CLI (prompt_toolkit).

Provides an interactive scrollable overlay inside the chat session when `/resume`
or `/sessions` is entered without an argument. Allows navigating with arrow keys
(Up/Down), typing to search/filter in real-time, and pressing Enter to resume.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from agent.i18n import t
from hermes_cli.agy_session_picker import (
    _clip,
    _format_steps,
    _format_workspace,
    _session_title,
    load_all_sessions,
)
from hermes_cli.timefmt import relative_time


def open_resume_picker(cli: Any) -> None:
    """Open the in-TUI interactive session resume picker."""
    if getattr(cli, "_resume_picker_state", None) is not None:
        return

    # Don't stack over other blocking modals
    for attr in (
        "_clarify_state",
        "_approval_state",
        "_slash_confirm_state",
        "_sudo_state",
        "_secret_state",
        "_connection_state",
        "_model_picker_state",
        "_command_palette_state",
    ):
        if getattr(cli, attr, None):
            return

    sessions = []
    store = getattr(cli, "_session_db", None)
    if store is not None:
        try:
            sessions = load_all_sessions(store=store)
        except Exception:
            sessions = []

    if not sessions and hasattr(cli, "_list_recent_sessions"):
        try:
            sessions = cli._list_recent_sessions(limit=50)
        except Exception:
            sessions = []

    if not sessions:
        from cli import _cprint
        _cprint("  No previous sessions found to resume.")
        return

    if hasattr(cli, "_capture_modal_input_snapshot"):
        cli._capture_modal_input_snapshot()

    cli._resume_picker_state = {
        "sessions": sessions,
        "selected": 0,
        "filter": "",
        "_scroll_offset": 0,
        "current_id": getattr(cli, "session_id", ""),
    }

    if hasattr(cli, "_invalidate"):
        cli._invalidate(min_interval=0.0)


def close_resume_picker(cli: Any) -> None:
    """Close the in-TUI session resume picker."""
    cli._resume_picker_state = None
    if hasattr(cli, "_restore_modal_input_snapshot"):
        cli._restore_modal_input_snapshot()
    if hasattr(cli, "_invalidate"):
        cli._invalidate(min_interval=0.0)


def get_resume_picker_visible_entries(cli: Any) -> list[dict[str, Any]]:
    """Return sessions matching the active filter query."""
    state = getattr(cli, "_resume_picker_state", None) or {}
    sessions = state.get("sessions") or []
    q = (state.get("filter", "") or "").strip().lower()
    if not q:
        return list(sessions)

    filtered = []
    for s in sessions:
        title = (s.get("title") or "").lower()
        preview = (s.get("preview") or "").lower()
        cwd = (s.get("cwd") or "").lower()
        sid = (s.get("id") or "").lower()
        if q in title or q in preview or q in cwd or q in sid:
            filtered.append(s)
    return filtered


def get_resume_picker_display_fragments(cli: Any) -> list[Any]:
    """Generate display fragments for prompt_toolkit rendering."""
    state = getattr(cli, "_resume_picker_state", None)
    if not state:
        return []

    visible = get_resume_picker_visible_entries(cli)
    state["_visible_count"] = len(visible)
    total = len(state.get("sessions") or [])
    query = state.get("filter", "") or ""

    labels: list[str] = []
    cur_id = state.get("current_id", "")

    for s in visible:
        title = _clip(_session_title(s), 38)
        ws = f"[{_clip(_format_workspace(s.get('cwd')), 14)}]"
        steps = _format_steps(s)
        time_str = relative_time(s.get("last_active") or s.get("last_activity_at"), session_id=s.get("id", ""))
        cur_badge = " • current" if s.get("id") == cur_id else ""
        labels.append(f"{title:<40} {ws:<16} {steps:>8}  {time_str:<10}{cur_badge}")

    labels.append(t("cli.shared.cancel"))

    if query:
        hint = f'Filter: "{query}" ({len(visible)} of {total}) • ↑↓ Navigate • ↵ Select • Backspace delete • Esc clear'
    else:
        hint = f"{total} sessions • ↑↓ Navigate • ↵ Select • Type to filter • Esc cancel"

    title = "Resume Session"
    return cli._render_scroll_list_panel(
        state, title, hint, labels, min_width=55, max_width=100, indent="  "
    )


def handle_resume_picker_selection(cli: Any) -> None:
    """Handle Enter selection on the resume picker."""
    state = getattr(cli, "_resume_picker_state", None)
    if not state:
        return

    visible = get_resume_picker_visible_entries(cli)
    selected = state.get("selected", 0)

    # Cancel selected (the trailing item)
    if selected >= len(visible):
        close_resume_picker(cli)
        return

    chosen = visible[selected]
    target_id = chosen.get("id")
    close_resume_picker(cli)

    if target_id:
        if hasattr(cli, "_handle_resume_command"):
            cli._handle_resume_command(f"/resume {target_id}")


def resume_picker_up(cli: Any, event: Any) -> None:
    """Navigate up."""
    state = getattr(cli, "_resume_picker_state", None)
    if state:
        state["selected"] = max(0, state.get("selected", 0) - 1)
        event.app.invalidate()


def resume_picker_down(cli: Any, event: Any) -> None:
    """Navigate down."""
    state = getattr(cli, "_resume_picker_state", None)
    if state:
        n = state.get("_visible_count", len(get_resume_picker_visible_entries(cli)))
        # +1 for Cancel option
        max_idx = n
        state["selected"] = min(max_idx, state.get("selected", 0) + 1)
        event.app.invalidate()


def resume_picker_escape(cli: Any, event: Any) -> None:
    """Esc clears filter first, then closes."""
    state = getattr(cli, "_resume_picker_state", None)
    if state and state.get("filter"):
        state["filter"] = ""
        state["selected"] = 0
        state["_scroll_offset"] = 0
        event.app.invalidate()
        return

    close_resume_picker(cli)
    if hasattr(event.app, "current_buffer") and event.app.current_buffer:
        event.app.current_buffer.reset()
    event.app.invalidate()


def resume_picker_backspace(cli: Any, event: Any) -> None:
    """Backspace shortens the search query."""
    state = getattr(cli, "_resume_picker_state", None)
    if state:
        state["filter"] = (state.get("filter", "") or "")[:-1]
        state["selected"] = 0
        state["_scroll_offset"] = 0
        event.app.invalidate()


def make_resume_filter_char_handler(cli: Any, ch: str):
    """Handler for printable characters to filter session list."""
    def handler(event: Any) -> None:
        state = getattr(cli, "_resume_picker_state", None)
        if not state:
            return
        state["filter"] = (state.get("filter", "") or "") + ch
        state["selected"] = 0
        state["_scroll_offset"] = 0
        event.app.invalidate()

    return handler
