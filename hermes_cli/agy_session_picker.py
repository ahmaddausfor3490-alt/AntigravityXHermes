"""AGY-style interactive session / conversation resume picker for APA & Hermes.

Provides a full-screen interactive TUI with:
- Tabs: [CLI], [Other], [All] to filter session sources
- Live instant search filtering over title, workspace, preview, model, and id
- Workspace badge [basename], step/tool call counts, and relative time
- Keyboard shortcuts: Up/Down navigate, Enter resume, Tab switch tab,
  F2 inline rename, F4 inline delete, Esc/q cancel
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Any, Optional

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

from hermes_cli.timefmt import relative_time


def _safe_create_output():
    """Create prompt_toolkit output, falling back to Vt100_Output if no console buffer exists."""
    try:
        from prompt_toolkit.output import create_output

        return create_output()
    except Exception:
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output.vt100 import Vt100_Output

        def get_size():
            sz = shutil.get_terminal_size((80, 24))
            return Size(rows=sz.lines, columns=sz.columns)

        return Vt100_Output(sys.stdout, get_size)


def _format_workspace(cwd: str | None) -> str:
    """Format workspace directory into a short recognizable folder basename."""
    if not cwd:
        return "—"
    try:
        norm = os.path.normpath(str(cwd))
        user_home = os.path.normpath(os.path.expanduser("~"))
        if norm.lower() == user_home.lower():
            return "~"
        base = os.path.basename(norm)
        return base if base else norm
    except Exception:
        return "—"


def _format_steps(s: dict) -> str:
    """Format steps or message count."""
    steps = s.get("tool_call_count")
    if isinstance(steps, int) and steps > 0:
        return f"{steps} steps"
    msgs = s.get("message_count")
    if isinstance(msgs, int) and msgs > 0:
        return f"{msgs} msgs"
    return "0 steps"


def _status_info(status: Optional[str]) -> tuple[str, str]:
    """Map session status to badge text and style class."""
    st = (status or "").lower()
    if st in ("complete", "done"):
        return "done", "class:status-done"
    if st in ("interrupted", "intr", "resumed_other"):
        return "intr", "class:status-intr"
    if st in ("error", "err"):
        return "err", "class:status-err"
    if st == "empty":
        return "empty", "class:status-empty"
    return "-", "class:dim"


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 3)] + "..."


def _session_title(s: dict) -> str:
    """Extract human-readable title, preview, or fallback to session id."""
    title = (s.get("title") or "").strip()
    if title:
        return title
    preview = (s.get("preview") or "").strip()
    if preview:
        return preview
    return s.get("id", "Untitled")


def load_all_sessions(store: Any = None) -> list[dict]:
    """Load session records from store or state.db."""
    if store is not None:
        try:
            from hermes_cli.sessions_cmd import _surface_reader

            reader = _surface_reader(store)
            sessions = reader.list_sessions_rich(limit=500)
            if sessions:
                return sessions
        except Exception:
            pass

    try:
        from hermes_constants import get_hermes_home
        from persistence_assembly import open_persistence_reader
        from hermes_cli.sessions_cmd import _surface_reader

        path = get_hermes_home() / "state.db"
        if path.exists():
            with open_persistence_reader(path) as r:
                reader = _surface_reader(r)
                return reader.list_sessions_rich(limit=500)
    except Exception:
        pass

    try:
        import sqlite3
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "state.db"
        if db_path.exists():
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                rows = cur.execute(
                    "SELECT id, title, source, cwd, message_count, tool_call_count, last_activity_at, "
                    "started_at, ended_at, end_reason, model FROM sessions ORDER BY last_activity_at DESC LIMIT 500"
                ).fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    d["last_active"] = d.get("last_activity_at") or d.get("started_at")
                    results.append(d)
                return results
    except Exception:
        pass
    return []


def rename_session_in_db(session_id: str, new_title: str, store: Any = None) -> bool:
    """Update session title in storage."""
    title = new_title.strip()
    if not title:
        return False
    if store is not None and hasattr(store, "sessions") and hasattr(store.sessions, "set_title"):
        try:
            return bool(store.sessions.set_title(session_id, title))
        except Exception:
            pass
    try:
        import sqlite3
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "state.db"
        if db_path.exists():
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
                conn.commit()
            return True
    except Exception:
        pass
    return False


def delete_session_in_db(session_id: str, store: Any = None) -> bool:
    """Delete session from storage."""
    if store is not None and hasattr(store, "sessions") and hasattr(store.sessions, "delete"):
        try:
            from gateway.session_ingress_media import native_inputs_root
            from hermes_constants import get_hermes_home
            from session_retention import after_sessions_removed

            sessions_dir = get_hermes_home() / "sessions"
            removed = store.sessions.delete(session_id, refuse_guarded=True, media_root=str(native_inputs_root()))
            if removed:
                after_sessions_removed(
                    store, store.scope.profile_dir, removed, reason="delete", primary=session_id, sessions_dir=sessions_dir
                )
                return True
        except Exception:
            pass
    try:
        import sqlite3
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "state.db"
        if db_path.exists():
            with sqlite3.connect(db_path) as conn:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
            return True
    except Exception:
        pass
    return False


class AGYSessionPicker:
    """Interactive full-screen AGY-style session picker using prompt_toolkit."""

    TABS = ["CLI", "Other", "All"]

    def __init__(self, sessions: list[dict], store: Any = None, default_tab: str = "CLI"):
        self.sessions = list(sessions)
        self.store = store
        self.active_tab = default_tab if default_tab in self.TABS else "CLI"
        self.search_query = ""
        self.cursor_idx = 0
        self.scroll_offset = 0

        # Modal interaction states: "normal", "rename", "delete_confirm"
        self.mode = "normal"
        self.rename_input = ""
        self.flash_message = ""
        self.flash_is_error = False

        self.filtered: list[dict] = []
        self._refilter(reset_cursor=True)

    def _matches_tab(self, s: dict) -> bool:
        src = (s.get("source") or "").lower()
        if self.active_tab == "CLI":
            return src in ("local", "cli", "term", "")
        if self.active_tab == "Other":
            return src not in ("local", "cli", "term", "")
        return True

    def _matches_search(self, s: dict) -> bool:
        if not self.search_query:
            return True
        q = self.search_query.lower()
        title = (s.get("title") or "").lower()
        preview = (s.get("preview") or "").lower()
        sid = (s.get("id") or "").lower()
        cwd = (s.get("cwd") or "").lower()
        src = (s.get("source") or "").lower()
        model = (s.get("model") or "").lower()
        return q in title or q in preview or q in sid or q in cwd or q in src or q in model

    def _refilter(self, reset_cursor: bool = False):
        self.filtered = [s for s in self.sessions if self._matches_tab(s) and self._matches_search(s)]
        if reset_cursor:
            self.cursor_idx = 0
            self.scroll_offset = 0
        else:
            if self.filtered:
                self.cursor_idx = max(0, min(self.cursor_idx, len(self.filtered) - 1))
            else:
                self.cursor_idx = 0
                self.scroll_offset = 0

    def tab_counts(self) -> dict[str, int]:
        counts = {"CLI": 0, "Other": 0, "All": len(self.sessions)}
        for s in self.sessions:
            src = (s.get("source") or "").lower()
            if src in ("local", "cli", "term", ""):
                counts["CLI"] += 1
            else:
                counts["Other"] += 1
        return counts

    def cycle_tab(self, forward: bool = True):
        idx = self.TABS.index(self.active_tab)
        next_idx = (idx + 1) % len(self.TABS) if forward else (idx - 1) % len(self.TABS)
        self.active_tab = self.TABS[next_idx]
        self._refilter(reset_cursor=True)

    def move_cursor(self, delta: int, visible_height: int = 15):
        if not self.filtered:
            return
        self.cursor_idx = max(0, min(len(self.filtered) - 1, self.cursor_idx + delta))
        if self.cursor_idx < self.scroll_offset:
            self.scroll_offset = self.cursor_idx
        elif self.cursor_idx >= self.scroll_offset + visible_height:
            self.scroll_offset = self.cursor_idx - visible_height + 1

    def selected_session(self) -> Optional[dict]:
        if self.filtered and 0 <= self.cursor_idx < len(self.filtered):
            return self.filtered[self.cursor_idx]
        return None

    def render_header(self) -> FormattedText:
        res = []
        # Title bar
        res.append(("class:title-brand", " ☤ APA "))
        res.append(("class:title-sub", " · Resume Conversation   "))

        # Tabs
        counts = self.tab_counts()
        for tab in self.TABS:
            is_active = tab == self.active_tab
            cnt = counts.get(tab, 0)
            if is_active:
                res.append(("class:tab-active", f" [{tab} ({cnt})] "))
            else:
                res.append(("class:tab-inactive", f"  {tab} ({cnt})  "))
            res.append(("", " "))
        res.append(("", "\n"))

        # Filter row
        res.append(("class:search-label", " 🔍 Filter: "))
        if self.search_query:
            res.append(("class:search-text", self.search_query))
            res.append(("class:cursor", "█"))
        else:
            res.append(("class:search-placeholder", "Type to filter conversations (Esc clear/exit)..."))
        res.append(("", "\n"))

        # Divider / Table header
        cols = shutil.get_terminal_size((80, 24)).columns
        fix_width = 3 + 20 + 7 + 11 + 11 + 12
        name_width = max(20, cols - fix_width)
        hdr = (
            f"   {'Conversation Title':<{name_width}} "
            f"{'Workspace':<20} "
            f"{'Status':<7} "
            f"{'Steps':>9}  "
            f"{'Active':<10} "
            f"{'ID':<10}"
        )
        res.append(("class:table-header", _clip(hdr, cols - 1) + "\n"))
        return FormattedText(res)

    def render_list(self) -> FormattedText:
        res = []
        cols = shutil.get_terminal_size((80, 24)).columns
        rows = shutil.get_terminal_size((80, 24)).lines
        visible_height = max(5, rows - 7)

        if not self.filtered:
            res.append(("", "\n   "))
            res.append(("class:dim", "No conversations found matching current tab & filter.\n"))
            return FormattedText(res)

        # Ensure scroll offset bounds
        if self.cursor_idx < self.scroll_offset:
            self.scroll_offset = self.cursor_idx
        elif self.cursor_idx >= self.scroll_offset + visible_height:
            self.scroll_offset = self.cursor_idx - visible_height + 1

        end_idx = min(len(self.filtered), self.scroll_offset + visible_height)
        fix_width = 3 + 20 + 7 + 11 + 11 + 12
        name_width = max(20, cols - fix_width)

        for i in range(self.scroll_offset, end_idx):
            s = self.filtered[i]
            is_selected = i == self.cursor_idx

            title = _clip(_session_title(s), name_width)
            ws_raw = _clip(_format_workspace(s.get("cwd")), 17)
            ws = f"[{ws_raw}]"
            steps = _format_steps(s)
            active = _clip(relative_time(s.get("last_active"), session_id=s.get("id")), 9)
            st_text, st_cls = _status_info(s.get("_status") or s.get("end_reason"))
            sid = s.get("id", "")[:10]

            row_prefix = " ❯ " if is_selected else "   "
            row_cls = "class:row-selected" if is_selected else "class:row-normal"

            res.append(("class:pointer" if is_selected else "", row_prefix))
            res.append((f"{row_cls}-title", f"{title:<{name_width}} "))
            res.append(("class:workspace-selected" if is_selected else "class:workspace", f"{ws:<20} "))
            res.append((st_cls, f"{st_text:<7} "))
            res.append((f"{row_cls}-steps", f"{steps:>9}  "))
            res.append(("class:active-time", f"{active:<10} "))
            res.append(("class:dim", f"{sid:<10}\n"))

        return FormattedText(res)

    def render_footer(self) -> FormattedText:
        res = []
        cols = shutil.get_terminal_size((80, 24)).columns

        if self.mode == "rename":
            res.append(("class:modal-rename", " ✎ Rename Conversation Title: "))
            res.append(("class:modal-input", self.rename_input))
            res.append(("class:cursor", "█"))
            res.append(("class:dim", "  (Enter: Save  ·  Esc: Cancel)"))
        elif self.mode == "delete_confirm":
            s = self.selected_session()
            t = _clip(_session_title(s) if s else "Selected", 40)
            res.append(("class:modal-delete", f" ⚠ Delete conversation '{t}'? "))
            res.append(("class:modal-delete-prompt", "[y/N]"))
            res.append(("class:dim", "  (Press 'y' to confirm, any other key to cancel)"))
        elif self.flash_message:
            cls = "class:flash-error" if self.flash_is_error else "class:flash-success"
            res.append((cls, f" {self.flash_message} "))
        else:
            res.append(
                (
                    "class:footer-keys",
                    " ↑↓ Navigate  ·  Enter Resume  ·  Tab Switch Tab  ·  F2 Rename  ·  F4 Delete  ·  Esc Exit",
                )
            )
            count_info = f"[{self.cursor_idx + 1 if self.filtered else 0}/{len(self.filtered)}] "
            pad = max(1, cols - 80 - len(count_info))
            res.append(("", " " * pad))
            res.append(("class:footer-count", count_info))

        return FormattedText(res)


def show_agy_session_picker(
    sessions: list[dict] | None = None, store: Any = None, default_tab: str = "CLI"
) -> Optional[str]:
    """Display full-screen AGY-style session picker and return chosen session_id or None."""
    if not sys.stdin.isatty():
        # Fallback to non-interactive list when terminal is not a TTY
        from hermes_cli.sessions_cmd_browse import _fallback_picker

        items = sessions or load_all_sessions(store)
        return _fallback_picker(items)

    all_sessions = sessions if sessions is not None else load_all_sessions(store)
    if not all_sessions:
        print("\nNo saved conversations found.\n")
        return None

    picker = AGYSessionPicker(all_sessions, store=store, default_tab=default_tab)
    result_session_id: list[Optional[str]] = [None]

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        if picker.mode == "normal":
            picker.move_cursor(-1)
            event.app.invalidate()

    @kb.add("down")
    def _down(event):
        if picker.mode == "normal":
            picker.move_cursor(1)
            event.app.invalidate()

    @kb.add("pageup")
    def _pageup(event):
        if picker.mode == "normal":
            picker.move_cursor(-10)
            event.app.invalidate()

    @kb.add("pagedown")
    def _pagedown(event):
        if picker.mode == "normal":
            picker.move_cursor(10)
            event.app.invalidate()

    @kb.add("home")
    def _home(event):
        if picker.mode == "normal":
            picker.cursor_idx = 0
            picker.scroll_offset = 0
            event.app.invalidate()

    @kb.add("end")
    def _end(event):
        if picker.mode == "normal":
            picker.cursor_idx = max(0, len(picker.filtered) - 1)
            event.app.invalidate()

    @kb.add("tab")
    def _tab(event):
        if picker.mode == "normal":
            picker.cycle_tab(forward=True)
            event.app.invalidate()

    @kb.add("s-tab")
    def _stab(event):
        if picker.mode == "normal":
            picker.cycle_tab(forward=False)
            event.app.invalidate()

    @kb.add("enter")
    def _enter(event):
        if picker.mode == "normal":
            sel = picker.selected_session()
            if sel and sel.get("id"):
                result_session_id[0] = sel["id"]
                event.app.exit(result=sel["id"])
        elif picker.mode == "rename":
            sel = picker.selected_session()
            if sel and sel.get("id") and picker.rename_input.strip():
                ok = rename_session_in_db(sel["id"], picker.rename_input.strip(), store=picker.store)
                if ok:
                    sel["title"] = picker.rename_input.strip()
                    picker.flash_message = f"✓ Renamed to: {picker.rename_input.strip()}"
                    picker.flash_is_error = False
                else:
                    picker.flash_message = "✕ Rename failed"
                    picker.flash_is_error = True
            picker.mode = "normal"
            picker._refilter()
            event.app.invalidate()
        elif picker.mode == "delete_confirm":
            picker.mode = "normal"
            event.app.invalidate()

    @kb.add("escape")
    def _esc(event):
        if picker.mode in ("rename", "delete_confirm"):
            picker.mode = "normal"
            picker.flash_message = ""
            event.app.invalidate()
        elif picker.search_query:
            picker.search_query = ""
            picker._refilter(reset_cursor=True)
            event.app.invalidate()
        else:
            result_session_id[0] = None
            event.app.exit(result=None)

    @kb.add("c-c")
    def _ctrl_c(event):
        result_session_id[0] = None
        event.app.exit(result=None)

    @kb.add("f2")
    def _f2(event):
        if picker.mode == "normal":
            sel = picker.selected_session()
            if sel:
                picker.mode = "rename"
                picker.rename_input = _session_title(sel)
                picker.flash_message = ""
                event.app.invalidate()

    @kb.add("f4")
    def _f4(event):
        if picker.mode == "normal":
            sel = picker.selected_session()
            if sel:
                picker.mode = "delete_confirm"
                picker.flash_message = ""
                event.app.invalidate()

    @kb.add("delete")
    def _delete(event):
        if picker.mode == "normal" and not picker.search_query:
            sel = picker.selected_session()
            if sel:
                picker.mode = "delete_confirm"
                picker.flash_message = ""
                event.app.invalidate()

    @kb.add("backspace")
    def _backspace(event):
        if picker.mode == "normal":
            if picker.search_query:
                picker.search_query = picker.search_query[:-1]
                picker._refilter(reset_cursor=True)
                event.app.invalidate()
        elif picker.mode == "rename":
            if picker.rename_input:
                picker.rename_input = picker.rename_input[:-1]
                event.app.invalidate()

    @kb.add("<any>")
    def _handle_any(event):
        char = event.data
        if not char or len(char) != 1:
            return

        if picker.mode == "normal":
            if ord(char) >= 32:
                # If filter is empty and user presses 'd', trigger delete
                if not picker.search_query and char.lower() == "d":
                    sel = picker.selected_session()
                    if sel:
                        picker.mode = "delete_confirm"
                        picker.flash_message = ""
                        event.app.invalidate()
                    return
                # If filter is empty and user presses 'r', trigger rename
                if not picker.search_query and char.lower() == "r":
                    sel = picker.selected_session()
                    if sel:
                        picker.mode = "rename"
                        picker.rename_input = _session_title(sel)
                        picker.flash_message = ""
                        event.app.invalidate()
                    return

                picker.flash_message = ""
                picker.search_query += char
                picker._refilter(reset_cursor=True)
                event.app.invalidate()

        elif picker.mode == "rename":
            if ord(char) >= 32:
                picker.rename_input += char
                event.app.invalidate()

        elif picker.mode == "delete_confirm":
            if char.lower() == "y":
                sel = picker.selected_session()
                if sel and sel.get("id"):
                    sid = sel["id"]
                    ok = delete_session_in_db(sid, store=picker.store)
                    if ok:
                        picker.sessions = [s for s in picker.sessions if s.get("id") != sid]
                        picker.flash_message = "✓ Session deleted."
                        picker.flash_is_error = False
                    else:
                        picker.flash_message = "✕ Failed to delete session."
                        picker.flash_is_error = True
                picker.mode = "normal"
                picker._refilter(reset_cursor=False)
                event.app.invalidate()
            else:
                picker.mode = "normal"
                picker.flash_message = "Delete cancelled."
                picker.flash_is_error = False
                event.app.invalidate()

    style = Style.from_dict(
        {
            "title-brand": "bold #38bdf8 bg:#0f172a",
            "title-sub": "#e2e8f0 bg:#0f172a",
            "tab-active": "bold #0b0f14 bg:#38bdf8",
            "tab-inactive": "#94a3b8 bg:#1e293b",
            "search-label": "bold #38bdf8",
            "search-text": "bold #ffffff",
            "search-placeholder": "#64748b italic",
            "cursor": "bold #38bdf8",
            "table-header": "bold #64748b",
            "pointer": "bold #38bdf8",
            "row-selected-title": "bold #ffffff bg:#1e293b",
            "row-selected-steps": "#f8fafc bg:#1e293b",
            "row-normal-title": "#e2e8f0",
            "row-normal-steps": "#94a3b8",
            "workspace-selected": "bold #38bdf8 bg:#1e293b",
            "workspace": "#38bdf8",
            "active-time": "#64748b",
            "dim": "#64748b",
            "status-done": "#4ade80",
            "status-intr": "#facc15",
            "status-err": "#f87171",
            "status-empty": "#64748b",
            "footer-keys": "#94a3b8 bg:#0f172a",
            "footer-count": "bold #38bdf8 bg:#0f172a",
            "modal-rename": "bold #38bdf8",
            "modal-input": "bold #ffffff underline",
            "modal-delete": "bold #ef4444",
            "modal-delete-prompt": "bold #ffffff bg:#b91c1c",
            "flash-success": "bold #4ade80",
            "flash-error": "bold #f87171",
        }
    )

    layout = Layout(
        HSplit(
            [
                Window(content=FormattedTextControl(picker.render_header), height=Dimension(exact=3)),
                Window(content=FormattedTextControl(picker.render_list)),
                Window(content=FormattedTextControl(picker.render_footer), height=Dimension(exact=1)),
            ]
        )
    )

    out = _safe_create_output()
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=True,
        output=out,
        mouse_support=False,
    )

    try:
        app.run()
    except Exception:
        pass

    return result_session_id[0]
