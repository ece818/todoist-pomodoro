"""Today task list panel.

Selection model: activating a row (click / Enter) or toggling its
checkbox selects the task for the timer via ``on_task_selected``.
The right-click menu's Complete item routes through the explicit
``on_mark_done`` (CelebrationWindow) flow; only when that callback is
absent does it call ``actions.complete`` directly in a worker thread.
"""

from __future__ import annotations

import datetime
import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk

from sync import SyncStore
from todoist import Task, TodoistAuthError, TodoistError
from ui_common import show_reschedule_dialog


# Preset labels with the due_string each sends to TaskActions.reschedule.
_RESCHEDULE_PRESETS: tuple[tuple[str, str], ...] = (
    ("Tomorrow", "tomorrow"),
    ("Next week", "next week"),
    ("Next Monday", "next monday"),
)


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
    :param actions: optional ``actions.TaskActions`` driving the right-click
        menu (start timer / complete / reschedule / quick-add). When None
        no context menu is attached.
    :param on_mark_done: optional explicit task-done callback ``fn(task)``
        (the same one the timer's Mark-done button uses); the menu's
        Complete item calls it. When None, Complete falls back to
        ``actions.complete`` in a worker thread with status feedback.
    """

    def __init__(
        self,
        store: SyncStore,
        on_task_selected: Optional[Callable[[Task], None]] = None,
        actions=None,
        on_mark_done: Optional[Callable[[Task], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._store = store
        self._on_task_selected = on_task_selected
        self._actions = actions
        self._on_mark_done = on_mark_done
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
        self._refresh_button.set_tooltip_text("Reload tasks from Todoist")
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
        self._entry.set_placeholder_text("Try 'Call mom Mon 2pm…'")
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

    def _quick_add_call(self, content: str) -> Task:
        """Quick-add via actions, else store.quick_add, else legacy add."""
        if self._actions is not None:
            quick = getattr(self._actions, "quick_add", None)
            if callable(quick):
                return quick(content)
        store_quick = getattr(self._store, "quick_add", None)
        if callable(store_quick):
            return store_quick(content)
        return self._store.add(content)

    def _add_worker(self, content: str) -> None:
        try:
            task = self._quick_add_call(content)
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
            GLib.idle_add(self._on_add_failed, content, "unauthorized")
        except TodoistError as exc:
            GLib.idle_add(self._on_add_failed, content, str(exc))
        except ValueError as exc:
            # Blank/invalid input rejected by quick_add; nothing to retry.
            GLib.idle_add(self._on_add_failed, content, str(exc) or "invalid input")
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

    def _on_check_toggled(self, btn: Gtk.CheckButton, task: Task) -> None:
        if getattr(btn, "_firing", False):
            return
        if not btn.get_active():
            return  # uncheck: no-op
        # Reset the visual state first (guarded against re-entrancy),
        # then run the normal explicit completion flow.
        btn._firing = True  # type: ignore[attr-defined]
        try:
            btn.set_active(False)
        finally:
            btn._firing = False  # type: ignore[attr-defined]
        self._complete_task(task)

    def _on_row_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task = getattr(row, "_task", None)
        if task is not None and self._on_task_selected is not None:
            self._on_task_selected(task)

    def _on_retry_clicked(self, _button: Gtk.Button) -> None:
        threading.Thread(target=self._retry_worker, daemon=True).start()

    # -- public (in-app hotkey support) --------------------------------

    def get_selected_task(self) -> Optional[Task]:
        """Return the selected row's task, or None when nothing selected."""
        try:
            row = self._list.get_selected_row()
        except Exception:
            return None
        if row is None:
            return None
        return getattr(row, "_task", None)

    def open_reschedule_for_selected(self) -> bool:
        """Pop the row menu for the selected task (Ctrl+R entry point).

        Returns True when a menu was shown. The menu's Reschedule…
        submenu holds the presets, keeping a single menu code path.
        """
        if self._actions is None:
            return False
        try:
            row = self._list.get_selected_row()
        except Exception:
            return False
        if row is None or getattr(row, "_task", None) is None:
            return False
        try:
            self._show_row_menu(row, 8, 8)
        except Exception:
            return False
        return True

    # -- right-click context menu --------------------------------------

    def _attach_row_menu(self, row: Gtk.ListBoxRow) -> None:
        gesture = Gtk.GestureClick.new()
        gesture.set_button(Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self._on_row_right_pressed, row)
        row.add_controller(gesture)

    def _on_row_right_pressed(
        self, gesture: Gtk.GestureClick, _n_press: int, x: float, y: float, row: Gtk.ListBoxRow
    ) -> None:
        if self._actions is None:
            return
        try:
            button = gesture.get_current_button()
        except Exception:
            button = Gdk.BUTTON_SECONDARY
        if button != Gdk.BUTTON_SECONDARY:  # == 3
            return
        if getattr(row, "_task", None) is None:
            return
        try:
            self._show_row_menu(row, x, y)
        except Exception:
            pass

    def _show_row_menu(self, row: Gtk.ListBoxRow, x: float, y: float) -> None:
        menu = Gio.Menu.new()
        menu.append("Start timer", "row.start")
        menu.append("Complete", "row.done")
        sub = Gio.Menu.new()
        sub.append("Tomorrow", "row.rs_tomorrow")
        sub.append("Next week", "row.rs_next_week")
        sub.append("Next Monday", "row.rs_next_monday")
        sub.append("Custom…", "row.rs_custom")
        menu.append_submenu("Reschedule…", sub)
        menu.append("Change date…", "row.change_date")

        group = Gio.SimpleActionGroup.new()

        def _add(name: str, fn) -> None:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda _a, _p: fn(row))
            group.add_action(act)

        _add("start", self._menu_start_timer)
        _add("done", self._menu_complete)
        _add("rs_tomorrow", lambda r: self._menu_reschedule(r, "tomorrow"))
        _add("rs_next_week", lambda r: self._menu_reschedule(r, "next week"))
        _add("rs_next_monday", lambda r: self._menu_reschedule(r, "next monday"))
        _add("rs_custom", self._menu_custom_date)
        _add("change_date", self._menu_custom_date)
        row.insert_action_group("row", group)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(row)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda _p: _p.unparent())
        popover.popup()

    def _row_task(self, row: Gtk.ListBoxRow) -> Optional[Task]:
        # Look up live so a rescheduled row never acts on a stale object.
        return getattr(row, "_task", None)

    def _menu_start_timer(self, row: Gtk.ListBoxRow) -> None:
        task = self._row_task(row)
        if task is None:
            return
        if self._actions is not None:
            start = getattr(self._actions, "start_timer", None)
            if callable(start):
                try:
                    start(task)
                    return
                except RuntimeError:
                    pass  # unregistered: fall through to on_task_selected
                except Exception as exc:
                    self._set_status(f"Could not start timer: {exc}")
                    return
        if self._on_task_selected is not None:
            try:
                self._on_task_selected(task)
            except Exception:
                pass

    def _menu_complete(self, row: Gtk.ListBoxRow) -> None:
        task = self._row_task(row)
        if task is None:
            return
        self._complete_task(task)

    def _complete_task(self, task: Task) -> None:
        """Explicit completion: celebration flow first, worker fallback."""
        # Explicit flow first: same callback the timer's Mark-done uses.
        if self._on_mark_done is not None:
            try:
                self._on_mark_done(task)
            except Exception:
                pass
            return
        if self._actions is None:
            return
        threading.Thread(target=self._complete_worker, args=(task,), daemon=True).start()

    def _menu_reschedule(self, row: Gtk.ListBoxRow, due_string: str) -> None:
        task = self._row_task(row)
        if task is None or self._actions is None:
            return
        if not (due_string or "").strip():
            self._set_status("Enter a date like 'tomorrow'.")
            return
        threading.Thread(
            target=self._reschedule_worker,
            args=(task, due_string.strip()),
            daemon=True,
        ).start()

    def _menu_custom_date(self, row: Gtk.ListBoxRow) -> None:
        if self._row_task(row) is None:
            return
        try:
            parent = row.get_root()
        except Exception:
            parent = None
        show_reschedule_dialog(
            parent if isinstance(parent, Gtk.Window) else None,
            lambda due: self._menu_reschedule(row, due),
        )

    # -- menu workers (never touch GTK directly; marshal via idle_add) -

    def _complete_worker(self, task: Task) -> None:
        try:
            self._actions.complete(task)
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
        except TodoistError as exc:
            GLib.idle_add(self._set_status_idle, f"Could not complete: {exc}")
        except Exception as exc:
            GLib.idle_add(self._set_status_idle, f"Could not complete: {exc}")
        else:
            GLib.idle_add(self._on_complete_done, getattr(task, "id", None))

    def _reschedule_worker(self, task: Task, due_string: str) -> None:
        try:
            updated = self._actions.reschedule(task, due_string)
        except ValueError as exc:
            GLib.idle_add(
                self._set_status_idle, f"Could not reschedule: {exc}"
            )
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
        except TodoistError as exc:
            GLib.idle_add(
                self._set_status_idle, f"Could not reschedule: {exc}"
            )
        except Exception as exc:
            GLib.idle_add(
                self._set_status_idle, f"Could not reschedule: {exc}"
            )
        else:
            GLib.idle_add(self._on_rescheduled, task, updated)

    def _set_status_idle(self, text: str) -> bool:
        self._set_status(text)
        return False

    def remove_task_row(self, task_id) -> bool:
        """Drop one row by task id (banner completion path)."""
        try:
            row = self._rows.pop(str(task_id), None) if task_id is not None else None
            if row is None:
                return False
            try:
                self._list.remove(row)
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _on_complete_done(self, task_id) -> bool:
        row = self._rows.pop(str(task_id), None) if task_id is not None else None
        if row is not None:
            try:
                self._list.remove(row)
            except Exception:
                pass
        self._set_status("Task completed.")
        return False

    def _on_rescheduled(self, old_task: Task, updated: Task) -> bool:
        old_id = getattr(old_task, "id", None)
        row = self._rows.get(str(old_id)) if old_id is not None else None
        if row is not None:
            row._task = updated  # type: ignore[attr-defined]
            try:
                row.set_child(self._build_task_box(updated))
            except Exception:
                pass
            new_id = getattr(updated, "id", old_id)
            if new_id is not None and str(new_id) != str(old_id):
                self._rows.pop(str(old_id), None)
                self._rows[str(new_id)] = row
            self._apply_filter()
        due = getattr(updated, "due_date", None)
        self._set_status(f"Rescheduled to {due}." if due else "Rescheduled.")
        return False

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
        row.set_child(self._build_task_box(task))
        if self._actions is not None:
            try:
                self._attach_row_menu(row)
            except Exception:
                pass
        self._list.append(row)
        task_id = getattr(task, "id", None)
        if task_id is not None:
            self._rows[str(task_id)] = row
        # Keep single-row adds consistent with any active filter text.
        self._apply_filter()

    def _build_task_box(self, task: Task) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        check = Gtk.CheckButton()
        try:
            short = str(task.content or "").strip()
            if len(short) > 60:
                short = short[:57] + "…"
            check.set_tooltip_text(f"Mark complete: {short}" if short else "Mark complete")
        except Exception:
            check.set_tooltip_text("Mark complete")
        # Checking completes the task (celebration flow); uncheck is a
        # no-op. Row click still selects the task for the timer.
        check.connect(
            "toggled", lambda btn, t=task: self._on_check_toggled(btn, t)
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
            if _is_overdue(task):
                meta_label.add_css_class("error")
            else:
                meta_label.add_css_class("dim-label")
            box.append(meta_label)
        return box

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
