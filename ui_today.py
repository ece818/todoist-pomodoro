"""Today task list panel.

Selection model: activating a row (click / Enter) or toggling its
checkbox selects the task for the timer via ``on_task_selected``.
Nothing here ever closes/completes a task — completion happens only
through the explicit CelebrationWindow flow.
"""

from __future__ import annotations

import datetime
import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from sync import SyncStore
from todoist import Task, TodoistAuthError, TodoistError


def _is_overdue(task: Task) -> bool:
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
        text = str(due).strip()
        # Accept "YYYY-MM-DD" or full ISO datetimes.
        day = datetime.date.fromisoformat(text[:10])
        return day < today
    except (ValueError, TypeError):
        return False


def _meta_label(task: Task, project: str = "") -> str:
    parts: list[str] = []
    if project:
        parts.append(project)
    if _is_overdue(task):
        parts.append("overdue")
    priority = getattr(task, "priority", 1) or 1
    if priority >= 2:
        parts.append(f"P{priority}")
    if getattr(task, "is_recurring", False):
        parts.append("recurring")
    return " · ".join(parts)


class TodayPanel(Gtk.Box):
    """Today + overdue task list with add-box and refresh.

    :param store: backend SyncStore (network calls run in worker threads).
    :param on_task_selected: callback ``fn(task)`` fired on row activation.
    """

    def __init__(
        self,
        store: SyncStore,
        on_task_selected: Optional[Callable[[Task], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._store = store
        self._on_task_selected = on_task_selected
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        self._auth_failed = False
        self._project_ids: list[str] = [""]  # "" == all projects
        self._syncing_projects = False

        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        # Auth banner (hidden until a 401 proves the token is invalid).
        self._banner = Gtk.Label(
            label="Token invalid — update .env with a valid Todoist token and restart."
        )
        self._banner.add_css_class("error")
        self._banner.set_wrap(True)
        self._banner.set_visible(False)
        self.append(self._banner)

        # Client-side filter: substring match on loaded rows only, no network.
        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Filter tasks…")
        self._search.connect("search-changed", self._on_filter_changed)
        self.append(self._search)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Today")
        title.add_css_class("title-1")
        title.set_xalign(0.0)
        title.set_hexpand(True)
        toolbar.append(title)
        self._project_dropdown = Gtk.DropDown()
        self._project_dropdown.set_tooltip_text("Show tasks from one project")
        toolbar.append(self._project_dropdown)
        self._refresh_button = Gtk.Button(label="Refresh")
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        toolbar.append(self._refresh_button)
        self.append(toolbar)
        self._rebuild_project_model(0)
        self._project_dropdown.connect(
            "notify::selected", self._on_project_selected
        )

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scrolled)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.connect("row-activated", self._on_row_activated)
        scrolled.set_child(self._list)

        self._status = Gtk.Label(label="")
        self._status.set_xalign(0.0)
        self._status.set_wrap(True)
        self.append(self._status)

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._entry = Gtk.Entry()
        self._entry.set_placeholder_text("Add a task…")
        self._entry.set_hexpand(True)
        self._entry.connect("activate", self._on_add_activated)
        add_box.append(self._entry)
        self._add_button = Gtk.Button(label="Add")
        self._add_button.connect("clicked", self._on_add_clicked)
        add_box.append(self._add_button)
        self.append(add_box)

        self.refresh()

    # -- public ------------------------------------------------------

    def refresh(self) -> None:
        """Re-run store.refresh_today() off the UI thread."""
        if self._auth_failed:
            return
        self._set_status("Loading…")
        self._refresh_button.set_sensitive(False)
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    # -- workers (never touch GTK directly; marshal via idle_add) -----

    def _refresh_worker(self) -> None:
        try:
            tasks = self._store.refresh_today()
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
        except TodoistError as exc:
            GLib.idle_add(self._on_load_failed, str(exc))
        except Exception as exc:  # defensive: never kill a daemon silently
            GLib.idle_add(self._on_load_failed, str(exc))
        else:
            GLib.idle_add(self._on_tasks_loaded, tasks)

    def _add_worker(self, content: str) -> None:
        try:
            task = self._store.add(content)
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
            GLib.idle_add(self._on_add_failed, content, "unauthorized")
        except TodoistError as exc:
            GLib.idle_add(self._on_add_failed, content, str(exc))
        except Exception as exc:
            GLib.idle_add(self._on_add_failed, content, str(exc))
        else:
            GLib.idle_add(self._on_task_added, task)

    def _retry_worker(self) -> None:
        try:
            # Contract assumption: retry_pending() returns a dict such as
            # {"added": [...], "failed": [...]} or similar; anything it
            # reports is reconciled by simply reloading the list below.
            self._store.retry_pending()
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
        except TodoistError as exc:
            GLib.idle_add(self._on_load_failed, str(exc))
        except Exception as exc:
            GLib.idle_add(self._on_load_failed, str(exc))
        else:
            GLib.idle_add(self._on_retry_done)

    # -- idle_add callbacks (UI thread) --------------------------------

    def _on_tasks_loaded(self, tasks: list[Task]) -> bool:
        self._refresh_button.set_sensitive(True)
        self._refresh_project_model()
        self._rebuild(tasks)
        self._set_status("" if tasks else "Nothing due — enjoy the calm.")
        return False

    # -- project filter ------------------------------------------------

    def _refresh_project_model(self) -> None:
        """Rebuild the project drop-down from the store's cached projects."""
        try:
            pairs = self._store.projects()
        except Exception:
            pairs = []
        current = (
            self._project_ids[self._project_dropdown.get_selected()]
            if 0 <= self._project_dropdown.get_selected() < len(self._project_ids)
            else ""
        )
        self._project_ids = [""] + [pid for pid, _name in pairs]
        labels = ["All projects"] + [name for _pid, name in pairs]
        self._syncing_projects = True
        try:
            self._project_dropdown.set_model(Gtk.StringList.new(labels))
            self._project_dropdown.set_selected(
                self._project_ids.index(current)
                if current in self._project_ids
                else 0
            )
        finally:
            self._syncing_projects = False

    def _rebuild_project_model(self, selected: int) -> None:
        self._syncing_projects = True
        try:
            self._project_dropdown.set_model(
                Gtk.StringList.new(["All projects"])
            )
            self._project_dropdown.set_selected(selected)
        finally:
            self._syncing_projects = False

    def _on_project_selected(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._syncing_projects:
            return
        self._apply_filter()

    def _selected_project_id(self) -> str:
        index = self._project_dropdown.get_selected()
        if 0 <= index < len(self._project_ids):
            return self._project_ids[index]
        return ""
    def _on_load_failed(self, message: str) -> bool:
        self._refresh_button.set_sensitive(True)
        self._set_status(f"Could not load tasks: {message}")
        return False

    def _on_auth_error(self) -> bool:
        # 401: show the re-token banner and never retry automatically.
        self._auth_failed = True
        self._refresh_button.set_sensitive(True)
        self._banner.set_visible(True)
        self._set_status("Unauthorized — check your Todoist token.")
        return False

    def _on_task_added(self, task: Task) -> bool:
        self._add_button.set_sensitive(True)
        self._entry.set_text("")
        self._append_task_row(task)
        self._set_status("")
        return False

    def _on_add_failed(self, content: str, message: str) -> bool:
        self._add_button.set_sensitive(True)
        if message != "unauthorized":
            self._append_pending_row(content, message)
        return False

    def _on_retry_done(self) -> bool:
        self._set_status("Retry finished — reloading…")
        self._auth_failed = False
        self.refresh()
        return False

    # -- signal handlers -----------------------------------------------

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self.refresh()

    def _on_filter_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Show only rows matching the text filter AND project filter.

        Client-side only: operates on already-loaded rows, no network.
        Clearing the search restores all rows. Rows without a task
        (e.g. pending rows) always stay visible.
        """
        query = self._search.get_text().strip().lower()
        project_id = self._selected_project_id()
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            task = getattr(child, "_task", None)
            if task is None:
                child.set_visible(True)
            else:
                visible = True
                if query:
                    try:
                        content = str(task.content).lower()
                    except Exception:
                        content = ""
                    visible = query in content
                if visible and project_id:
                    visible = str(getattr(task, "project_id", "") or "") == project_id
                child.set_visible(visible)
            child = nxt

    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        self._submit_add()

    def _on_add_activated(self, _entry: Gtk.Entry) -> None:
        self._submit_add()

    def _submit_add(self) -> None:
        if self._auth_failed:
            return
        content = self._entry.get_text().strip()
        if not content:
            return
        self._add_button.set_sensitive(False)
        threading.Thread(
            target=self._add_worker, args=(content,), daemon=True
        ).start()

    def _on_row_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task = getattr(row, "_task", None)
        if task is not None and self._on_task_selected is not None:
            self._on_task_selected(task)

    def _on_retry_clicked(self, _button: Gtk.Button) -> None:
        threading.Thread(target=self._retry_worker, daemon=True).start()

    # -- row builders ---------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._status.set_text(text)

    def _rebuild(self, tasks: list[Task]) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        self._rows.clear()
        for task in tasks:
            self._append_task_row(task)
        self._apply_filter()

    def _append_task_row(self, task: Task) -> None:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        row._task = task  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        check = Gtk.CheckButton()
        check.set_tooltip_text("Select for timer")
        # Selecting (not completing) — completion is explicit via Celebration.
        check.connect(
            "toggled",
            lambda _btn, t=task: self._on_task_selected(t)
            if self._on_task_selected is not None
            else None,
        )
        box.append(check)

        content = Gtk.Label(label=task.content)
        content.set_xalign(0.0)
        content.set_hexpand(True)
        content.set_wrap(True)
        box.append(content)

        try:
            project = self._store.project_name(
                str(getattr(task, "project_id", "") or "")
            )
        except Exception:
            project = ""
        meta = _meta_label(task, project)
        if meta:
            meta_label = Gtk.Label(label=meta)
            meta_label.add_css_class("dim-label")
            box.append(meta_label)

        row.set_child(box)
        self._list.append(row)
        task_id = getattr(task, "id", None)
        if task_id is not None:
            self._rows[str(task_id)] = row
        # Keep single-row adds consistent with any active filter text.
        self._apply_filter()

    def _append_pending_row(self, content: str, message: str) -> None:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        label = Gtk.Label(label=f"{content} (pending — {message})")
        label.set_xalign(0.0)
        label.set_hexpand(True)
        label.set_wrap(True)
        box.append(label)

        retry = Gtk.Button(label="Retry")
        retry.connect("clicked", self._on_retry_clicked)
        box.append(retry)

        row.set_child(box)
        self._list.append(row)
        self._set_status("Add failed — kept as pending. Press Retry.")
