"""VSCode-style command palette for task search.

Loads candidates ONCE in a worker thread (``store.candidates()`` so the
active saved filter applies, with a ``refresh_all()`` / ``refresh_today()``
fallback for older stores), then filters locally as the user types — no
per-keystroke network. ``store.search("")`` returns ``[]`` for blank
queries by contract, so it is not used for loading.

When a ``filter_store`` (``sync.FilterStore``) is passed, a filter row
above the search entry offers saved filters plus a "Custom…" one-off
query and a "save current query as filter" flow.
"""

from __future__ import annotations

import datetime
import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from todoist import TodoistAuthError, TodoistError
from ui_common import add_close_shortcuts, header_with_close


def _is_overdue(task) -> bool:
    """Best-effort overdue check; unknown/unparseable dates are not overdue."""
    due = getattr(task, "due_date", None)
    if not due:
        return False
    try:
        today = datetime.date.today()
        if isinstance(due, datetime.datetime):
            return due.date() < today
        if isinstance(due, datetime.date):
            return due < today
        day = datetime.date.fromisoformat(str(due).strip()[:10])
        return day < today
    except (ValueError, TypeError):
        return False


def _meta_label(task) -> str:
    parts: list[str] = []
    due = getattr(task, "due_date", None)
    if _is_overdue(task):
        parts.append("overdue")
    elif due:
        try:
            parts.append(str(due)[:10])
        except Exception:
            pass
    priority = getattr(task, "priority", 1) or 1
    if priority >= 2:
        parts.append(f"P{priority}")
    if getattr(task, "is_recurring", False):
        parts.append("recurring")
    return " · ".join(parts)


class PaletteWindow(Gtk.Window):
    """Centered command palette (NOT fullscreen) for picking a task.

    :param store: backend SyncStore (network calls run in worker threads).
    :param on_pick: callback ``fn(task)`` fired on Enter/click, then closes.
    :param filter_store: optional ``sync.FilterStore`` enabling the saved-
        filter picker row; when None the filter UI is hidden.
    """

    # Last-selected filter within this session (in-memory only; the saved
    # filters themselves persist via FilterStore). Restored on each open.
    _last_filter_name: Optional[str] = None
    _last_custom: bool = False

    _ALL_TASKS_LABEL = "All tasks"
    _CUSTOM_LABEL = "Custom…"

    def __init__(self, store, on_pick: Optional[Callable] = None, filter_store=None) -> None:
        super().__init__()
        self._store = store
        self._on_pick = on_pick
        self._filters = filter_store
        self._candidates: list = []
        self._auth_failed = False
        self._filter_names: list[str] = []
        self._syncing_dropdown = False
        self._filter_dropdown: Optional[Gtk.DropDown] = None
        self._filter_name_entry: Optional[Gtk.Entry] = None

        self.set_title("Search tasks")
        self.set_modal(True)
        self.set_titlebar(header_with_close("Search tasks"))
        add_close_shortcuts(self)
        # ~600px wide, compact height; the window manager centers it.
        # (GTK4 has no portable "top-third" positioning for windows, so we
        # keep it short and let it center over the transient parent.)
        self.set_default_size(600, 400)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        self.set_child(box)

        if self._filters is not None:
            filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.append(filter_row)

            self._filter_dropdown = Gtk.DropDown()
            self._filter_dropdown.set_hexpand(True)
            self._filter_dropdown.set_tooltip_text("Filter tasks by saved filter")
            filter_row.append(self._filter_dropdown)

            self._filter_name_entry = Gtk.Entry()
            self._filter_name_entry.set_placeholder_text("Save query as… e.g. today & p1")
            filter_row.append(self._filter_name_entry)

            save_button = Gtk.Button(label="Save")
            save_button.set_tooltip_text("Save current search as a named filter")
            save_button.connect("clicked", self._on_save_filter)
            filter_row.append(save_button)

            # Populate + re-assert the persisted filter on the store before
            # the single worker load below (no reload here).
            self._apply_initial_filter_selection()
            assert self._filter_dropdown is not None
            self._filter_dropdown.connect("notify::selected", self._on_filter_selected)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Type to search tasks…")
        self._search.connect("search-changed", self._on_query_changed)
        key_ctl = Gtk.EventControllerKey()
        key_ctl.connect("key-pressed", self._on_key_pressed)
        self._search.add_controller(key_ctl)
        box.append(self._search)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.append(scrolled)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self._list)

        self.connect("show", self._on_show)
        self._show_info_row("Loading…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    # -- workers (never touch GTK directly; marshal via idle_add) -----

    def _load_worker(self) -> None:
        try:
            # Contract: SyncStore.candidates() respects the active filter
            # (else all tasks). Older stores only have refresh_all() /
            # refresh_today(); fall back so the palette still works.
            candidates = getattr(self._store, "candidates", None)
            if callable(candidates):
                tasks = candidates()
            else:
                refresh = getattr(self._store, "refresh_all", None)
                if callable(refresh):
                    tasks = refresh()
                else:
                    tasks = self._store.refresh_today()
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
        except TodoistError as exc:
            GLib.idle_add(self._on_load_failed, str(exc))
        except Exception as exc:  # defensive: never kill a daemon silently
            GLib.idle_add(self._on_load_failed, str(exc))
        else:
            GLib.idle_add(self._on_candidates_loaded, tasks)

    # -- saved filters -------------------------------------------------

    def _saved_filter_names(self) -> list[str]:
        try:
            entries = self._filters.list() or []
        except Exception:
            return []
        names = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(entry["name"])
        return names

    def _set_store_filter(self, query) -> None:
        set_filter = getattr(self._store, "set_filter", None)
        if callable(set_filter):
            set_filter(query)

    def _rebuild_filter_model(self, selected: int) -> None:
        assert self._filter_dropdown is not None
        items = [self._ALL_TASKS_LABEL] + self._filter_names + [self._CUSTOM_LABEL]
        self._syncing_dropdown = True
        try:
            self._filter_dropdown.set_model(Gtk.StringList.new(items))
            self._filter_dropdown.set_selected(selected)
        finally:
            self._syncing_dropdown = False

    def _apply_initial_filter_selection(self) -> None:
        """Populate the drop-down and re-assert the persisted filter.

        Runs before the single worker load; never starts loading itself.
        """
        self._filter_names = self._saved_filter_names()
        index = 0  # "All tasks"
        if type(self)._last_custom:
            index = len(self._filter_names) + 1
        elif type(self)._last_filter_name in self._filter_names:
            name = type(self)._last_filter_name
            assert name is not None
            query = self._filters.get(name)
            if query is not None:
                self._set_store_filter(query)
                index = self._filter_names.index(name) + 1
            else:
                # Deleted out from under us; fall back to All tasks.
                type(self)._last_filter_name = None
                self._set_store_filter(None)
        else:
            type(self)._last_filter_name = None
            self._set_store_filter(None)
        self._rebuild_filter_model(index)

    def _on_filter_selected(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._filters is None or self._syncing_dropdown:
            return
        index = dropdown.get_selected()
        if index <= 0:
            # "All tasks" (or nothing selected).
            type(self)._last_filter_name = None
            type(self)._last_custom = False
            self._set_store_filter(None)
            self._reload_candidates()
        elif index <= len(self._filter_names):
            name = self._filter_names[index - 1]
            try:
                query = self._filters.get(name)
            except Exception:
                query = None
            if query is None:
                # Deleted out from under us; refresh and fall back to All.
                type(self)._last_filter_name = None
                type(self)._last_custom = False
                self._filter_names = self._saved_filter_names()
                self._set_store_filter(None)
                self._rebuild_filter_model(0)
                self._reload_candidates()
                return
            type(self)._last_filter_name = name
            type(self)._last_custom = False
            self._set_store_filter(query)
            self._reload_candidates()
        else:
            # "Custom…": one-off use of the current search text as the query.
            type(self)._last_custom = True
            query = self._search.get_text().strip()
            self._set_store_filter(query or None)
            self._reload_candidates()

    def _on_save_filter(self, _button: Gtk.Button) -> None:
        if self._filters is None or self._filter_dropdown is None:
            return
        assert self._filter_name_entry is not None
        name = self._filter_name_entry.get_text().strip()
        query = self._search.get_text().strip()
        try:
            self._filters.add(name, query)
        except ValueError:
            # Blank name or query: nudge the user, leave results untouched.
            self._filter_name_entry.grab_focus()
            self._filter_name_entry.select_region(0, -1)
            return
        except Exception:
            return
        type(self)._last_filter_name = name
        type(self)._last_custom = False
        self._filter_names = self._saved_filter_names()
        try:
            index = self._filter_names.index(name) + 1
        except ValueError:
            index = 0
        self._filter_name_entry.set_text("")
        if self._filter_dropdown.get_selected() == index:
            # Re-saving the already-selected filter with an edited query:
            # notify won't fire, so apply + reload directly.
            self._set_store_filter(query)
            self._reload_candidates()
        else:
            # Fires _on_filter_selected, which applies the filter + reloads.
            self._filter_dropdown.set_selected(index)

    def _reload_candidates(self) -> None:
        self._auth_failed = False
        self._candidates = []
        self._clear_list()
        self._show_info_row("Loading…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    # -- idle_add callbacks (UI thread) --------------------------------

    def _on_candidates_loaded(self, tasks: list) -> bool:
        self._candidates = list(tasks or [])
        self._apply_filter()
        return False

    def _on_load_failed(self, message: str) -> bool:
        self._candidates = []
        self._clear_list()
        self._show_info_row(f"Could not load tasks: {message}")
        return False

    def _on_auth_error(self) -> bool:
        # 401: inline error row, never retry automatically.
        self._auth_failed = True
        self._candidates = []
        self._clear_list()
        self._show_info_row("Unauthorized — check your Todoist token.")
        return False

    # -- signal handlers -----------------------------------------------

    def _on_show(self, _window: Gtk.Window) -> None:
        self._search.grab_focus()

    def _on_query_changed(self, _entry: Gtk.SearchEntry) -> None:
        # Local-only filter; no network per keystroke.
        if self._auth_failed:
            return
        self._apply_filter()

    def _on_row_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task = getattr(row, "_task", None)
        if task is not None:
            self._pick(task)

    def _on_key_pressed(self, _ctl, keyval: int, _keycode: int, _state) -> bool:
        if keyval == Gdk.KEY_Down:
            self._move_selection(1)
            return True
        if keyval == Gdk.KEY_Up:
            self._move_selection(-1)
            return True
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            row = self._list.get_selected_row()
            task = getattr(row, "_task", None) if row is not None else None
            if task is not None:
                self._pick(task)
                return True
        return False

    # -- internals -----------------------------------------------------

    def _pick(self, task) -> None:
        if self._on_pick is not None:
            self._on_pick(task)
        self.close()

    def _move_selection(self, delta: int) -> None:
        selected = self._list.get_selected_row()
        if selected is None:
            first = self._list.get_row_at_index(0)
            if first is not None and getattr(first, "_task", None) is not None:
                self._list.select_row(first)
            return
        idx = selected.get_index()
        nxt = self._list.get_row_at_index(idx + delta)
        if nxt is not None and getattr(nxt, "_task", None) is not None:
            self._list.select_row(nxt)

    def _apply_filter(self) -> None:
        query = self._search.get_text().strip().lower()
        if not query:
            matches = list(self._candidates)
        else:
            matches = []
            for task in self._candidates:
                try:
                    content = str(task.content).lower()
                except Exception:
                    content = ""
                if query in content:
                    matches.append(task)
        self._clear_list()
        if not matches:
            if self._candidates:
                self._show_info_row("No matches.")
            else:
                self._show_info_row("No tasks.")
            return
        for task in matches:
            self._append_result_row(task)
        first = self._list.get_row_at_index(0)
        if first is not None:
            self._list.select_row(first)

    def _clear_list(self) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt

    def _show_info_row(self, text: str) -> None:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        label = Gtk.Label(label=text)
        label.add_css_class("dim-label")
        label.set_xalign(0.0)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_margin_start(8)
        label.set_margin_end(8)
        row.set_child(label)
        self._list.append(row)

    def _append_result_row(self, task) -> None:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row._task = task  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        content = Gtk.Label(label=getattr(task, "content", ""))
        content.set_xalign(0.0)
        content.set_hexpand(True)
        content.set_wrap(True)
        box.append(content)

        meta = _meta_label(task)
        if meta:
            meta_label = Gtk.Label(label=meta)
            meta_label.add_css_class("dim-label")
            box.append(meta_label)

        row.set_child(box)
        self._list.append(row)
