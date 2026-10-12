"""Pomodoro countdown view plus fullscreen timer window.

Timer end NEVER completes a task: it stops the clock, plays the end
sound, and leaves pause/reset/start-break options visible. Task
completion is an explicit user action handled by CelebrationWindow.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, Gdk, GLib, Gtk

from ui_common import add_close_shortcuts


def _format_seconds(total: int) -> str:
    total = max(0, int(total))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


_TIMER_CSS = b"""
.timer-clock { font-size: 120px; font-weight: 800; }
.timer-clock-full { font-size: 220px; font-weight: 800; }
.timer-task { font-size: 28px; font-weight: 600; }
.timer-task-full { font-size: 48px; font-weight: 600; }
"""

_css_loaded = False


def _ensure_timer_css() -> None:
    """Load the oversized-timer styles once; silently skip if unavailable."""
    global _css_loaded
    if _css_loaded:
        return
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(_TIMER_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _css_loaded = True
    except Exception:
        pass


def play_end_sound() -> None:
    """Play the timer-end bell; every backend is guarded with fallbacks."""
    try:
        from gi.repository import Canberra  # type: ignore

        ctx = Canberra.Context.get_default()
        if ctx is not None:
            ctx.play(0, Canberra.PROP_EVENT_ID, "message-bell")
            return
    except Exception:
        pass
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            try:
                settings = Gtk.Settings.get_default()
                if settings is not None:
                    # Best-effort read; absence of the key must not raise.
                    try:
                        settings.get_property("gtk-error-bell-name")
                    except Exception:
                        pass
            except Exception:
                pass
            display.beep()
            return
    except Exception:
        pass
    try:
        print("\a", flush=True)
    except Exception:
        pass


class FullscreenTimerWindow(Gtk.Window):
    """Fullscreen countdown with giant label + task name.

    Screensaver/sleep is inhibited via org.gnome.SessionManager while
    visible; when D-Bus/GNOME is unavailable the window still works,
    just without the inhibit (graceful fallback).
    """

    def __init__(self, task_name: str = "", remaining_seconds: int = 0) -> None:
        super().__init__()
        self.set_title("Focus")
        self.set_default_size(800, 600)
        _ensure_timer_css()
        self._inhibit_cookie: Optional[int] = None
        self._inhibit_proxy = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(48)
        box.set_margin_end(48)

        self._task_label = Gtk.Label(label=task_name)
        self._task_label.add_css_class("timer-task-full")
        self._task_label.set_wrap(True)
        self._task_label.set_xalign(0.5)
        box.append(self._task_label)

        self._clock_label = Gtk.Label(label=_format_seconds(remaining_seconds))
        self._clock_label.add_css_class("timer-clock-full")
        self._clock_label.set_xalign(0.5)
        box.append(self._clock_label)

        close_btn = Gtk.Button(label="Exit fullscreen (Esc)")
        close_btn.connect("clicked", lambda _b: self.close())
        box.append(close_btn)

        self.set_child(box)
        self.connect("close-request", self._on_close_request)
        add_close_shortcuts(self)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

    # -- public ------------------------------------------------------

    def update(self, task_name: str, remaining_seconds: int) -> None:
        self._task_label.set_text(task_name)
        self._clock_label.set_text(_format_seconds(remaining_seconds))

    def present_fullscreen(self) -> None:
        self._inhibit()
        self.fullscreen()
        self.present()

    # -- inhibit ------------------------------------------------------

    def _inhibit(self) -> None:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            proxy = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager",
                None,
            )
            # Flags 4 (suspend) | 8 (idle).
            result = proxy.call_sync(
                "Inhibit",
                GLib.Variant(
                    "(susu)",
                    ("todoist-pomodoro", 0, "Pomodoro focus session", 12),
                ),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            self._inhibit_proxy = proxy
            if result is not None:
                self._inhibit_cookie = result.unpack()[0]
        except Exception:
            self._inhibit_proxy = None
            self._inhibit_cookie = None

    def _uninhibit(self) -> None:
        try:
            if self._inhibit_proxy is not None and self._inhibit_cookie is not None:
                self._inhibit_proxy.call_sync(
                    "UnInhibit",
                    GLib.Variant("(u)", (self._inhibit_cookie,)),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
        except Exception:
            pass
        finally:
            self._inhibit_proxy = None
            self._inhibit_cookie = None

    # -- handlers -----------------------------------------------------

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state,
    ) -> bool:
        try:
            from gi.repository import Gdk

            if keyval == Gdk.KEY_Escape:
                self.close()
                return True
        except Exception:
            pass
        return False

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self._uninhibit()
        self.unfullscreen()
        return False  # allow the close to proceed


class TimerView(Gtk.Box):
    """Focus / short-break countdown.

    :param focus_min: focus session length in minutes.
    :param short_break_min: short break length in minutes.
    :param on_done: optional ``fn(mode)`` called (UI thread) at timer end.
    :param task: optionally bound task (see :meth:`bind_task`).
    """

    MODE_FOCUS = "focus"
    MODE_BREAK = "break"

    def __init__(
        self,
        focus_min: int = 25,
        short_break_min: int = 5,
        on_done: Optional[Callable[[str], None]] = None,
        task=None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        _ensure_timer_css()
        self._focus_min = focus_min
        self._short_break_min = short_break_min
        self._on_done = on_done
        self._on_mark_done: Optional[Callable] = None
        self._on_session: Optional[Callable] = None
        self._task = task

        self._mode = self.MODE_FOCUS
        self._total = focus_min * 60
        self._remaining = self._total
        self._tick_id: Optional[int] = None
        self._session_start: Optional[str] = None
        self._fullscreen_win: Optional[FullscreenTimerWindow] = None

        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # Center stage: fills the page, clock dominates.
        stage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        stage.set_vexpand(True)
        stage.set_valign(Gtk.Align.CENTER)
        stage.set_halign(Gtk.Align.CENTER)
        self.append(stage)

        self._task_label = Gtk.Label(label=self._task_name())
        self._task_label.add_css_class("timer-task")
        self._task_label.set_wrap(True)
        self._task_label.set_xalign(0.5)
        stage.append(self._task_label)

        self._clock_label = Gtk.Label(label=_format_seconds(self._remaining))
        self._clock_label.add_css_class("timer-clock")
        self._clock_label.set_xalign(0.5)
        stage.append(self._clock_label)

        self._state_label = Gtk.Label(label="Ready")
        self._state_label.add_css_class("dim-label")
        self._state_label.set_xalign(0.5)
        stage.append(self._state_label)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_halign(Gtk.Align.CENTER)
        self._start_button = Gtk.Button(label="Start")
        self._start_button.connect("clicked", self._on_start_clicked)
        controls.append(self._start_button)
        self._pause_button = Gtk.Button(label="Pause")
        self._pause_button.connect("clicked", self._on_pause_clicked)
        self._pause_button.set_sensitive(False)
        controls.append(self._pause_button)
        self._reset_button = Gtk.Button(label="Reset")
        self._reset_button.connect("clicked", self._on_reset_clicked)
        controls.append(self._reset_button)
        self._break_button = Gtk.Button(label="Start Break")
        self._break_button.connect("clicked", self._on_break_clicked)
        controls.append(self._break_button)
        self.append(controls)

        extra = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        extra.set_halign(Gtk.Align.CENTER)
        self._fullscreen_button = Gtk.Button(label="Fullscreen")
        self._fullscreen_button.connect(
            "clicked", self._on_fullscreen_clicked
        )
        extra.append(self._fullscreen_button)
        self._mark_done_button = Gtk.Button(label="Mark task done")
        self._mark_done_button.add_css_class("suggested-action")
        self._mark_done_button.connect(
            "clicked", self._on_mark_done_clicked
        )
        self._mark_done_button.set_sensitive(task is not None)
        extra.append(self._mark_done_button)
        self.append(extra)

    # -- public ------------------------------------------------------

    def stop_if_bound(self, task_id) -> bool:
        """Stop and reset when the bound task was completed elsewhere.

        Returns True when this view was running this task's timer.
        """
        task = self._task
        if task is None or str(getattr(task, "id", "")) != str(task_id):
            return False
        if self._tick_id is None:
            return False
        # Log the work first (>= 60s counts as a partial session).
        self._emit_session(False)
        self._stop_ticking()
        self._remaining = self._total
        self._render()
        self._show_idle_state()
        self._state_label.set_text("Ready — task completed.")
        return True

    def set_durations(self, focus_min: int, short_break_min: int) -> None:
        """Apply new durations (e.g. from Settings).

        Takes effect immediately when idle; a running session keeps its
        current countdown and the new values apply to the next one.
        """
        self._focus_min = max(1, int(focus_min))
        self._short_break_min = max(1, int(short_break_min))
        if self._tick_id is None:
            self._switch_mode(self.MODE_FOCUS, autostart=False)
            self._state_label.set_text("Ready")

    def bind_task(self, task) -> None:
        """Bind (or rebind) this view to a task. Resets to a fresh focus."""
        if self._tick_id is not None:
            # A session is running: do not silently discard elapsed time.
            self._state_label.set_text(
                "Finish or reset the current session before switching tasks."
            )
            return
        self._task = task
        self._task_label.set_text(self._task_name())
        self._mark_done_button.set_sensitive(task is not None)
        self._switch_mode(self.MODE_FOCUS, autostart=False)
        self._state_label.set_text("Ready")

    def set_on_mark_done(self, callback: Optional[Callable]) -> None:
        """Register the explicit task-done handler (opens Celebration)."""
        self._on_mark_done = callback

    # kept for main.py wiring convenience
    def connect_mark_done(self, callback: Callable) -> None:
        self.set_on_mark_done(callback)

    def set_on_session(self, callback: Optional[Callable]) -> None:
        """Register ``fn(task, elapsed_sec, finished, started_at)``.

        Fired when a focus countdown ends (finished=True) or is reset
        partway with >= 60s elapsed (finished=False). Breaks never log.
        """
        self._on_session = callback

    # -- internals ----------------------------------------------------

    def _task_name(self) -> str:
        if self._task is None:
            return "No task selected"
        return getattr(self._task, "content", "")

    def _switch_mode(self, mode: str, autostart: bool) -> None:
        self._stop_ticking()
        self._mode = mode
        minutes = (
            self._focus_min if mode == self.MODE_FOCUS else self._short_break_min
        )
        self._total = max(1, minutes) * 60
        self._remaining = self._total
        self._render()
        if autostart:
            self._start_ticking()
        else:
            self._show_idle_state()

    def _start_ticking(self) -> None:
        self._stop_ticking()
        if self._session_start is None:
            try:
                from datetime import datetime as _dt

                self._session_start = _dt.now().astimezone().isoformat(
                    timespec="seconds"
                )
            except Exception:
                self._session_start = None
        self._tick_id = GLib.timeout_add_seconds(1, self._on_tick)
        self._start_button.set_sensitive(False)
        self._pause_button.set_sensitive(True)
        self._state_label.set_text(
            "Focusing…" if self._mode == self.MODE_FOCUS else "On break…"
        )

    def _stop_ticking(self) -> None:
        if self._tick_id is not None:
            try:
                GLib.source_remove(self._tick_id)
            except Exception:
                pass
            self._tick_id = None

    def _on_tick(self) -> bool:
        self._remaining -= 1
        if self._remaining <= 0:
            self._remaining = 0
            self._render()
            self._finish()
            return False  # do not repeat
        self._render()
        return True

    def _emit_session(self, finished: bool) -> None:
        """Report a focus block to the session logger, if registered."""
        if self._mode != self.MODE_FOCUS or self._on_session is None:
            return
        elapsed = self._total - self._remaining
        if elapsed < 60:
            return
        try:
            self._on_session(
                self._task, elapsed, finished, self._session_start
            )
        except Exception:
            pass
        finally:
            self._session_start = None

    def _render(self) -> None:
        self._clock_label.set_text(_format_seconds(self._remaining))
        if self._fullscreen_win is not None:
            try:
                self._fullscreen_win.update(self._task_name(), self._remaining)
            except Exception:
                pass

    def _finish(self) -> None:
        """Timer end: stop, sound, rest-state. Never completes any task."""
        self._stop_ticking()
        self._emit_session(True)
        play_end_sound()
        self._start_button.set_sensitive(False)
        self._start_button.set_label("Start")
        self._pause_button.set_sensitive(False)
        self._reset_button.set_sensitive(True)
        self._break_button.set_sensitive(True)
        self._state_label.set_text("Done — take a break or reset.")
        if self._on_done is not None:
            try:
                self._on_done(self._mode)
            except Exception:
                pass

    def _show_idle_state(self) -> None:
        self._start_button.set_sensitive(True)
        self._start_button.set_label("Start")
        self._pause_button.set_sensitive(False)
        self._reset_button.set_sensitive(True)
        self._break_button.set_sensitive(True)

    # -- handlers -----------------------------------------------------

    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        if self._remaining <= 0:
            self._remaining = self._total
        self._start_ticking()

    def _on_pause_clicked(self, _button: Gtk.Button) -> None:
        self._stop_ticking()
        self._start_button.set_sensitive(True)
        self._start_button.set_label("Resume")
        self._pause_button.set_sensitive(False)
        self._state_label.set_text("Paused")

    def _on_reset_clicked(self, _button: Gtk.Button) -> None:
        # Log partial focus work (>= 60s) before discarding the countdown.
        self._emit_session(False)
        self._stop_ticking()
        self._remaining = self._total
        self._render()
        self._show_idle_state()
        self._state_label.set_text("Ready")

    def _on_break_clicked(self, _button: Gtk.Button) -> None:
        # Plain short-break countdown: no task binding, no completion.
        # Log any focus work in progress first (>= 60s counts as partial).
        self._emit_session(False)
        self._switch_mode(self.MODE_BREAK, autostart=True)

    def _on_fullscreen_clicked(self, _button: Gtk.Button) -> None:
        try:
            if self._fullscreen_win is None:
                self._fullscreen_win = FullscreenTimerWindow(
                    self._task_name(), self._remaining
                )
                self._fullscreen_win.connect(
                    "close-request", self._on_fullscreen_closed
                )
            else:
                self._fullscreen_win.update(self._task_name(), self._remaining)
            self._fullscreen_win.present_fullscreen()
        except Exception:
            self._fullscreen_win = None

    def _on_fullscreen_closed(self, _window: Gtk.Window) -> bool:
        self._fullscreen_win = None
        return False

    def _on_mark_done_clicked(self, _button: Gtk.Button) -> None:
        # Explicit user task-done action only; never fired at timer end.
        if self._on_mark_done is not None and self._task is not None:
            try:
                self._on_mark_done(self._task)
            except Exception:
                pass
