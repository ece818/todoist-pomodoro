"""Application entry point: Today -> Timer wiring + bottom banner."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except (ImportError, ValueError) as _gi_exc:
    print(
        "This app needs GTK4 + libadwaita + PyGObject "
        f"(could not import gi: {_gi_exc}). "
        "On Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 "
        "gir1.2-adwaita-1.",
        file=sys.stderr,
    )
    sys.exit(2)

from config import (
    ConfigError,
    TimerSettings,
    load_config,
    load_timer_settings,
    save_timer_settings,
)
from sessions import SessionStore
from sync import FilterStore, SyncStore
from todoist import TodoistAuthError, TodoistClient, TodoistError
from ui_common import add_close_shortcuts

try:
    # Implemented by the backend agent; absent until it lands.
    from actions import TaskActions
except ImportError:
    TaskActions = None  # type: ignore[assignment]
from ui_palette import PaletteWindow
from ui_settings import SettingsDialog
from ui_stats import StatsPanel
from ui_timer import TimerView
from ui_today import TodayPanel


class PomodoroApp(Adw.Application):
    """Adw.Application with stacked Today / Timer views."""

    def __init__(
        self,
        store: SyncStore,
        focus_min: int,
        short_break_min: int,
        filter_store: FilterStore | None = None,
        settings: TimerSettings | None = None,
        settings_path=None,
        session_store: SessionStore | None = None,
    ) -> None:
        super().__init__(application_id="dev.todoist.pomodoro")
        self._store = store
        self._filter_store = filter_store
        self._session_store = session_store
        self._settings = settings or TimerSettings(
            focus_min=focus_min, short_break_min=short_break_min
        )
        self._settings_path = settings_path
        self._focus_min = focus_min
        self._short_break_min = short_break_min
        self._stack: Gtk.Stack | None = None
        self._timer_view: TimerView | None = None
        self._stats_panel: StatsPanel | None = None
        self._today_panel: TodayPanel | None = None
        self._actions = None

    def do_activate(self) -> None:
        win = Adw.ApplicationWindow(application=self)
        win.set_title("Todoist Pomodoro")
        win.set_default_size(640, 720)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        if TaskActions is not None:
            self._actions = TaskActions(
                self._store, on_start_timer=self._on_task_selected
            )
        else:
            self._actions = None
        today = TodayPanel(
            self._store,
            on_task_selected=self._on_task_selected,
            actions=self._actions,
            on_mark_done=self._on_mark_task_done,
        )
        self._today_panel = today
        self._stack.add_titled(today, "today", "Today")

        self._timer_view = TimerView(
            focus_min=self._focus_min,
            short_break_min=self._short_break_min,
            on_done=self._on_timer_done,
        )
        self._timer_view.set_on_mark_done(self._on_mark_task_done)
        if self._session_store is not None:
            self._timer_view.set_on_session(self._on_session_logged)

        timer_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        back_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_bar.set_margin_top(8)
        back_bar.set_margin_start(8)
        back = Gtk.Button(label="← Today")
        back.connect("clicked", lambda _b: self._show("today"))
        back_bar.append(back)
        timer_page.append(back_bar)
        timer_page.append(self._timer_view)
        self._stack.add_titled(timer_page, "timer", "Timer")

        if self._session_store is not None:
            stats_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            stats_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            stats_bar.set_margin_top(8)
            stats_bar.set_margin_start(8)
            stats_back = Gtk.Button(label="← Today")
            stats_back.connect("clicked", lambda _b: self._show("today"))
            stats_bar.append(stats_back)
            stats_page.append(stats_bar)
            self._stats_panel = StatsPanel(
                self._session_store,
                project_name=self._store.project_name,
            )
            stats_page.append(self._stats_panel)
            self._stack.add_titled(stats_page, "stats", "Stats")

        # Visible title bar with the window-manager close button.
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Todoist Pomodoro"))
        stats_button = Gtk.Button(label="Stats")
        stats_button.set_tooltip_text("Focus stats")
        stats_button.connect("clicked", self._on_stats_clicked)
        header.pack_end(stats_button)
        settings_button = Gtk.Button(label="Settings")
        settings_button.set_tooltip_text("Timer durations")
        settings_button.connect("clicked", self._on_settings_clicked)
        header.pack_end(settings_button)
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._stack)

        # Bottom banner (revealer): completion confirm + Undo, auto-hides.
        overlay = Gtk.Overlay()
        overlay.set_child(toolbar_view)
        self._banner = Gtk.Revealer()
        self._banner.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._banner.set_reveal_child(False)
        self._banner.set_halign(Gtk.Align.CENTER)
        self._banner.set_valign(Gtk.Align.END)
        self._banner.set_margin_bottom(16)
        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner_box.add_css_class("card")
        banner_box.set_margin_top(10)
        banner_box.set_margin_bottom(10)
        banner_box.set_margin_start(16)
        banner_box.set_margin_end(16)
        self._banner_label = Gtk.Label(label="")
        banner_box.append(self._banner_label)
        self._banner_undo = Gtk.Button(label="Undo")
        self._banner_undo.connect("clicked", self._on_banner_undo)
        banner_box.append(self._banner_undo)
        self._banner.set_child(banner_box)
        overlay.add_overlay(self._banner)
        self._banner_task = None
        self._banner_timer: int | None = None

        win.set_content(overlay)
        add_close_shortcuts(win)

        palette_shortcut = Gtk.Shortcut.new(
            trigger=Gtk.ShortcutTrigger.parse_string("<Control>p"),
            action=Gtk.CallbackAction.new(self._on_palette_shortcut),
        )
        shortcut_controller = Gtk.ShortcutController()
        shortcut_controller.add_shortcut(palette_shortcut)
        quit_shortcut = Gtk.Shortcut.new(
            trigger=Gtk.ShortcutTrigger.parse_string("<Control>q"),
            action=Gtk.CallbackAction.new(self._on_quit_shortcut),
        )
        shortcut_controller.add_shortcut(quit_shortcut)
        win.add_controller(shortcut_controller)

        # In-app hotkeys for the Today list's selected row (GLOBAL scope).
        hotkey_controller = Gtk.ShortcutController()
        hotkey_controller.set_scope(Gtk.ShortcutScope.GLOBAL)
        hotkey_controller.add_shortcut(
            Gtk.Shortcut.new(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control>Return"),
                action=Gtk.CallbackAction.new(self._on_hotkey_start_timer),
            )
        )
        hotkey_controller.add_shortcut(
            Gtk.Shortcut.new(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control>KP_Enter"),
                action=Gtk.CallbackAction.new(self._on_hotkey_start_timer),
            )
        )
        hotkey_controller.add_shortcut(
            Gtk.Shortcut.new(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control><Shift>C"),
                action=Gtk.CallbackAction.new(self._on_hotkey_mark_done),
            )
        )
        hotkey_controller.add_shortcut(
            Gtk.Shortcut.new(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control>r"),
                action=Gtk.CallbackAction.new(self._on_hotkey_reschedule),
            )
        )
        win.add_controller(hotkey_controller)

        win.present()

    # -- wiring --------------------------------------------------------

    def _show(self, name: str) -> None:
        if self._stack is not None:
            if name == "stats" and self._stats_panel is not None:
                self._stats_panel.refresh()
            self._stack.set_visible_child_name(name)

    def _on_stats_clicked(self, _button: Gtk.Button) -> None:
        self._show("stats")

    def _on_session_logged(
        self, task, elapsed_sec: int, finished: bool, started_at
    ) -> None:
        if self._session_store is None:
            return
        try:
            project_id = str(getattr(task, "project_id", "") or "")
            content = str(getattr(task, "content", "") or "")
            task_id = str(getattr(task, "id", "") or "")
        except Exception:
            project_id, content, task_id = "", "", ""
        try:
            project_name = self._store.project_name(project_id)
        except Exception:
            project_name = ""
        try:
            self._session_store.log(
                task_id=task_id,
                content=content or "(no task)",
                project_id=project_id,
                project_name=project_name,
                duration_sec=elapsed_sec,
                finished=finished,
                started_at=started_at,
            )
        except Exception:
            pass

    def _on_task_selected(self, task) -> None:
        # Selecting a task opens the TimerView bound to it.
        if self._timer_view is not None:
            self._timer_view.bind_task(task)
        self._show("timer")

    def _open_task(self, task) -> None:
        # Palette pick: same as selecting a task from the Today list.
        self._on_task_selected(task)

    def _on_quit_shortcut(self, _widget, _args) -> bool:
        self.quit()
        return True

    # -- in-app hotkeys (Today list's selected row) ----------------------

    def _selected_today_task(self):
        if self._today_panel is None:
            return None
        try:
            return self._today_panel.get_selected_task()
        except Exception:
            return None

    def _focus_in_editable(self) -> bool:
        """True when keyboard focus sits inside a text entry."""
        try:
            win = self.get_active_window()
            focus = win.get_focus() if win is not None else None
            return isinstance(focus, Gtk.Editable)
        except Exception:
            return False

    def _on_hotkey_start_timer(self, _widget, _args) -> bool:
        # Ctrl+Enter: never steal keys from text entries.
        if self._focus_in_editable():
            return False
        task = self._selected_today_task()
        if task is None or self._actions is None:
            return False
        try:
            self._actions.start_timer(task)
        except RuntimeError:
            # Unregistered: same fallback the row menu uses.
            try:
                self._on_task_selected(task)
            except Exception:
                return False
        except Exception:
            return False
        return True

    def _on_hotkey_mark_done(self, _widget, _args) -> bool:
        task = self._selected_today_task()
        if task is None or self._actions is None:
            return False
        try:
            self._on_mark_task_done(task)
        except Exception:
            return False
        return True

    def _on_hotkey_reschedule(self, _widget, _args) -> bool:
        if self._today_panel is None or self._actions is None:
            return False
        try:
            return bool(self._today_panel.open_reschedule_for_selected())
        except Exception:
            return False

    def _on_settings_clicked(self, _button: Gtk.Button) -> None:
        dialog = SettingsDialog(
            focus_min=self._settings.focus_min,
            short_break_min=self._settings.short_break_min,
            on_save=self._on_settings_saved,
        )
        dialog.set_transient_for(self.get_active_window())
        dialog.present()

    def _on_settings_saved(self, focus_min: int, short_break_min: int) -> None:
        self._settings = TimerSettings(
            focus_min=max(1, focus_min), short_break_min=max(1, short_break_min)
        )
        if self._settings_path is not None:
            try:
                save_timer_settings(self._settings_path, self._settings)
            except OSError:
                pass
        self._focus_min = self._settings.focus_min
        self._short_break_min = self._settings.short_break_min
        if self._timer_view is not None:
            self._timer_view.set_durations(
                self._settings.focus_min, self._settings.short_break_min
            )

    def _on_palette_shortcut(self, _widget, _args) -> bool:
        palette = PaletteWindow(
            self._store,
            on_pick=self._open_task,
            filter_store=self._filter_store,
            actions=self._actions,
        )
        parent = self.get_active_window()
        if parent is not None:
            palette.set_transient_for(parent)
        palette.set_modal(True)
        palette.present()
        return True

    def _on_timer_done(self, _mode: str) -> None:
        # Timer end: nothing else to do — TimerView already stopped,
        # sounded, and shows its rest state. Deliberately no celebration.
        return None

    def _on_mark_task_done(self, task) -> None:
        # Explicit user task-done action: close now, confirm via the
        # bottom banner with Undo (no modal popup).
        threading.Thread(
            target=self._complete_worker, args=(task,), daemon=True
        ).start()

    def _complete_worker(self, task) -> None:
        try:
            self._store.close(task)
        except TodoistAuthError:
            GLib.idle_add(
                self._show_banner, "Token invalid — update .env and restart.",
                None, False,
            )
        except TodoistError as exc:
            GLib.idle_add(
                self._show_banner, f"Could not complete: {exc}", None, False
            )
        except Exception as exc:
            GLib.idle_add(
                self._show_banner, f"Could not complete: {exc}", None, False
            )
        else:
            content = str(getattr(task, "content", "") or "Task")
            GLib.idle_add(
                self._show_banner, f"Completed: {content}", task, True
            )

    def _show_banner(
        self, text: str, task=None, undoable: bool = False, seconds: int = 5
    ) -> bool:
        """Show the bottom banner; auto-hide after ``seconds``."""
        if self._banner_timer is not None:
            try:
                GLib.source_remove(self._banner_timer)
            except Exception:
                pass
            self._banner_timer = None
        self._banner_label.set_text(text)
        self._banner_task = task if undoable else None
        self._banner_undo.set_visible(undoable)
        self._banner.set_reveal_child(True)
        self._banner_timer = GLib.timeout_add_seconds(
            seconds, self._hide_banner
        )
        return False

    def _hide_banner(self) -> bool:
        self._banner.set_reveal_child(False)
        self._banner_task = None
        self._banner_timer = None
        return False

    def _on_banner_undo(self, _button: Gtk.Button) -> None:
        task = self._banner_task
        if task is None:
            return
        if self._banner_timer is not None:
            try:
                GLib.source_remove(self._banner_timer)
            except Exception:
                pass
            self._banner_timer = None
        self._banner.set_reveal_child(False)
        self._banner_task = None
        threading.Thread(
            target=self._reopen_worker, args=(task,), daemon=True
        ).start()

    def _reopen_worker(self, task) -> None:
        try:
            self._store.reopen(task)
        except Exception as exc:
            GLib.idle_add(
                self._show_banner, f"Could not undo: {exc}", None, False
            )
        else:
            GLib.idle_add(
                self._show_banner, "Task restored.", None, False, 3
            )


def main(argv: list[str] | None = None) -> int:
    # Resolve .env next to the app itself so `timer` works from any cwd.
    app_dir = Path(__file__).resolve().parent
    try:
        config = load_config(env_path=app_dir / ".env")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    client = TodoistClient(config.token, timeout=10)
    state_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "todoist-pomodoro"
    store = SyncStore(client, state_path=state_dir / "pending.json")
    store.load_state()
    filter_store = FilterStore(state_dir / "filters.json")
    session_store = SessionStore(state_dir / "sessions.jsonl")
    settings_path = state_dir / "settings.json"
    settings = load_timer_settings(
        settings_path,
        focus_default=config.focus_min,
        break_default=config.short_break_min,
    )
    app = PomodoroApp(
        store,
        focus_min=settings.focus_min,
        short_break_min=settings.short_break_min,
        filter_store=filter_store,
        settings=settings,
        settings_path=settings_path,
        session_store=session_store,
    )
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
