"""Application entry point: Today -> Timer -> Celebration wiring."""

from __future__ import annotations

import os
import sys
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
from sync import FilterStore, SyncStore
from todoist import TodoistClient
from ui_celebrate import CelebrationWindow
from ui_common import add_close_shortcuts
from ui_palette import PaletteWindow
from ui_settings import SettingsDialog
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
    ) -> None:
        super().__init__(application_id="dev.todoist.pomodoro")
        self._store = store
        self._filter_store = filter_store
        self._settings = settings or TimerSettings(
            focus_min=focus_min, short_break_min=short_break_min
        )
        self._settings_path = settings_path
        self._focus_min = focus_min
        self._short_break_min = short_break_min
        self._stack: Gtk.Stack | None = None
        self._timer_view: TimerView | None = None

    def do_activate(self) -> None:
        win = Adw.ApplicationWindow(application=self)
        win.set_title("Todoist Pomodoro")
        win.set_default_size(640, 720)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        today = TodayPanel(
            self._store, on_task_selected=self._on_task_selected
        )
        self._stack.add_titled(today, "today", "Today")

        self._timer_view = TimerView(
            focus_min=self._focus_min,
            short_break_min=self._short_break_min,
            on_done=self._on_timer_done,
        )
        self._timer_view.set_on_mark_done(self._on_mark_task_done)

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

        # Visible title bar with the window-manager close button.
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Todoist Pomodoro"))
        settings_button = Gtk.Button(label="Settings")
        settings_button.set_tooltip_text("Timer durations")
        settings_button.connect("clicked", self._on_settings_clicked)
        header.pack_end(settings_button)
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._stack)
        win.set_content(toolbar_view)
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

        win.present()

    # -- wiring --------------------------------------------------------

    def _show(self, name: str) -> None:
        if self._stack is not None:
            self._stack.set_visible_child_name(name)

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
        # Explicit user task-done action: open the CelebrationWindow whose
        # on_complete closes the task in Todoist via the sync store.
        def do_close(t) -> None:
            self._store.close(t)

        celebration = CelebrationWindow(task, on_complete=do_close)
        celebration.set_transient_for(self.get_active_window())
        celebration.set_modal(True)
        celebration.present()


def main(argv: list[str] | None = None) -> int:
    try:
        config = load_config(env_path=".env")
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
    )
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
