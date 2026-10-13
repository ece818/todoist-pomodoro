"""Pomodoro countdown view plus fullscreen timer window.

Timer end NEVER completes a task: it stops the clock, plays the end
sound, and leaves pause/reset/start-break options visible. Task
completion is an explicit user action handled by CelebrationWindow.
"""

from __future__ import annotations

import math
import random
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
.focus-clock { font-size: 120px; font-weight: 800; color: #ffffff; }
.focus-task { font-size: 26px; font-weight: 700; color: #ffffff; }
.focus-stats { font-size: 15px; font-weight: 600; color: #f88379; }
.focus-dim { font-size: 14px; color: rgba(255,255,255,0.6); }
.focus-card { background: rgba(18, 18, 28, 0.88); border-radius: 18px; }
.focus-pill { background: #ffffff; color: #14141e; border-radius: 999px; font-size: 20px; font-weight: 800; padding: 10px 44px; }
.focus-ghost { color: rgba(255,255,255,0.7); }
.focus-nav { color: rgba(255,255,255,0.55); font-weight: 600; }
.focus-nav-active { color: #ffffff; font-weight: 800; }
.focus-check { border-radius: 999px; border: 2px solid rgba(255,255,255,0.75); min-width: 30px; min-height: 30px; background: transparent; color: transparent; }
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


_NIGHT_STOPS = (
    (0.00, (0.03, 0.04, 0.13)),
    (0.36, (0.10, 0.10, 0.28)),
    (0.55, (0.24, 0.21, 0.44)),
    (0.60, (0.09, 0.08, 0.21)),
    (0.66, (0.06, 0.06, 0.15)),
    (1.00, (0.015, 0.015, 0.045)),
)


def _night_rgb(p: float) -> tuple:
    """Sky/lake gradient color at fraction p from the top (0..1)."""
    stops = _NIGHT_STOPS
    if p <= stops[0][0]:
        return stops[0][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p <= p1:
            t = (p - p0) / max(1e-6, p1 - p0)
            return (c0[0] + (c1[0] - c0[0]) * t,
                    c0[1] + (c1[1] - c0[1]) * t,
                    c0[2] + (c1[2] - c0[2]) * t)
    return stops[-1][1]


def _ridge_peaks(width: float, base: float, height: float, seed_phase: float,
                 fuji: bool) -> list:
    """Ridge silhouette points with a central Fuji-like cone."""
    n_seg = 26
    cx = width * 0.5
    hw = width * 0.17
    ht = height * (0.115 if fuji else 0.0)
    peaks = []
    for i in range(n_seg + 1):
        px = width * i / n_seg
        n = height * (0.015 + 0.035 * abs(
            math.sin(i * 1.7 + seed_phase) * 0.6
            + math.sin(i * 0.6 + seed_phase * 2) * 0.4))
        if fuji:
            cone = ht * max(0.0, 1 - abs(px - cx) / hw)
            n = max(n, cone)
        peaks.append((px, base + n))
    return peaks


class FocusNightBackdrop(Gtk.DrawingArea):
    """Procedural starry-night lake + mountain scene (no image assets)."""

    def __init__(self) -> None:
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw, None)

    def _draw(self, _area, ctx, w: int, h: int, _data) -> None:
        try:
            bands = 28
            for i in range(bands):
                ctx.set_source_rgb(*_night_rgb((i + 0.5) / bands))
                ctx.rectangle(0, h * i / bands, w, h / bands + 1)
                ctx.fill()
            rng = random.Random(11)
            for _ in range(150):
                ctx.set_source_rgba(1, 1, 1, 0.2 + rng.random() * 0.65)
                s = 1 + rng.random() * 1.6
                ctx.rectangle(rng.random() * w, h * (0.02 + rng.random() * 0.54),
                              s, s)
                ctx.fill()
            ctx.set_source_rgba(0.90, 0.92, 1, 0.10)
            ctx.arc(w * 0.78, h * 0.30, 60, 0, 2 * math.pi)
            ctx.fill()
            ctx.set_source_rgba(0.95, 0.96, 1, 0.9)
            ctx.arc(w * 0.78, h * 0.30, 8, 0, 2 * math.pi)
            ctx.fill()
            horizon = h * 0.60
            for base, color, phase, fuji in (
                    (horizon, (0.09, 0.09, 0.19), 0.0, True),
                    (horizon - h * 0.015, (0.03, 0.03, 0.07), 100.0, False)):
                ctx.set_source_rgb(*color)
                peaks = _ridge_peaks(w, base, h, phase, fuji)
                ctx.move_to(0, base - h * 0.02)
                for px, py in peaks:
                    ctx.line_to(px, py)
                ctx.line_to(w, base - h * 0.02)
                ctx.close_path()
                ctx.fill()
            for k in range(8):
                ly = horizon + h * (0.03 + k * 0.028)
                lw = w * (0.10 + rng.random() * 0.30)
                lx = (w - lw) / 2 + (rng.random() - 0.5) * w * 0.3
                ctx.set_source_rgba(0.55, 0.58, 0.85, 0.10)
                ctx.set_line_width(1.2)
                ctx.move_to(lx, ly)
                ctx.line_to(lx + lw, ly)
                ctx.stroke()
        except Exception:
            pass


class FocusTickRing(Gtk.DrawingArea):
    """120-tick progress ring; ticks light up with elapsed fraction."""

    TICKS = 120

    def __init__(self, size: int = 360) -> None:
        super().__init__()
        self._progress = 0.0
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw, None)

    def set_progress(self, value: float) -> None:
        self._progress = min(1.0, max(0.0, float(value)))
        try:
            self.queue_draw()
        except Exception:
            pass

    def _draw(self, _area, ctx, w: int, h: int, _data) -> None:
        try:
            cx, cy = w / 2, h / 2
            radius = min(w, h) / 2 - 12
            if radius <= 0:
                return
            ctx.set_line_width(3)
            ctx.set_line_cap(1)  # round caps
            for i in range(self.TICKS):
                ang = -math.pi / 2 + 2 * math.pi * i / self.TICKS
                if (i / self.TICKS) < self._progress:
                    ctx.set_source_rgba(1, 1, 1, 0.92)
                else:
                    ctx.set_source_rgba(1, 1, 1, 0.16)
                c, s = math.cos(ang), math.sin(ang)
                ctx.move_to(cx + (radius - 11) * c, cy + (radius - 11) * s)
                ctx.line_to(cx + radius * c, cy + radius * s)
                ctx.stroke()
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
        self._task_label.add_css_class("focus-task")
        self._task_label.set_wrap(True)
        self._task_label.set_xalign(0.5)
        box.append(self._task_label)

        ring_overlay = Gtk.Overlay()
        self._ring = FocusTickRing(size=400)
        ring_overlay.set_child(self._ring)
        self._clock_label = Gtk.Label(label=_format_seconds(remaining_seconds))
        self._clock_label.add_css_class("focus-clock")
        self._clock_label.set_xalign(0.5)
        self._clock_label.set_halign(Gtk.Align.CENTER)
        self._clock_label.set_valign(Gtk.Align.CENTER)
        ring_overlay.add_overlay(self._clock_label)
        box.append(ring_overlay)

        close_btn = Gtk.Button(label="Exit fullscreen (Esc)")
        close_btn.add_css_class("focus-ghost")
        close_btn.connect("clicked", lambda _b: self.close())
        box.append(close_btn)

        overlay = Gtk.Overlay()
        overlay.set_child(FocusNightBackdrop())
        overlay.add_overlay(box)
        self.set_child(overlay)
        self._ring_total = max(1, int(remaining_seconds))
        self._ring.set_progress(0.0)
        self.connect("close-request", self._on_close_request)
        add_close_shortcuts(self)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

    # -- public ------------------------------------------------------

    def update(self, task_name: str, remaining_seconds: int) -> None:
        self._task_label.set_text(task_name)
        self._clock_label.set_text(_format_seconds(remaining_seconds))
        try:
            done = max(0, self._ring_total - max(0, int(remaining_seconds)))
            self._ring.set_progress(done / self._ring_total)
        except Exception:
            pass

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

        self._on_navigate: Optional[Callable[[str], None]] = None
        self._session_store = None
        self._pill_fn: Optional[Callable[[], None]] = None
        self._pill_key = ""

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(FocusNightBackdrop())
        self.append(overlay)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(16)
        page.set_margin_bottom(12)
        page.set_margin_start(16)
        page.set_margin_end(16)
        overlay.add_overlay(page)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.add_css_class("focus-card")
        card.set_margin_top(10)
        card.set_margin_bottom(10)
        card.set_margin_start(14)
        card.set_margin_end(14)
        check = Gtk.Button(label="")
        check.add_css_class("focus-check")
        check.set_tooltip_text("Complete this task in Todoist")
        check.set_valign(Gtk.Align.CENTER)
        check.connect("clicked", self._on_mark_done_clicked)
        card.append(check)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(True)
        title_box.set_valign(Gtk.Align.CENTER)
        self._task_label = Gtk.Label(label=self._task_name())
        self._task_label.add_css_class("focus-task")
        self._task_label.set_wrap(True)
        self._task_label.set_xalign(0.0)
        title_box.append(self._task_label)
        self._task_stats = Gtk.Label(label="")
        self._task_stats.add_css_class("focus-stats")
        self._task_stats.set_xalign(0.0)
        title_box.append(self._task_stats)
        card.append(title_box)
        card_close = Gtk.Button(label="X")
        card_close.add_css_class("focus-ghost")
        card_close.set_tooltip_text("Back to Today")
        card_close.set_valign(Gtk.Align.CENTER)
        card_close.connect("clicked", lambda _b: self._emit_navigate("today"))
        card.append(card_close)
        page.append(card)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        center.set_vexpand(True)
        center.set_valign(Gtk.Align.CENTER)
        ring_overlay = Gtk.Overlay()
        self._ring = FocusTickRing(size=360)
        ring_overlay.set_child(self._ring)
        self._clock_label = Gtk.Label(label=_format_seconds(self._remaining))
        self._clock_label.add_css_class("focus-clock")
        self._clock_label.set_halign(Gtk.Align.CENTER)
        self._clock_label.set_valign(Gtk.Align.CENTER)
        ring_overlay.add_overlay(self._clock_label)
        center.append(ring_overlay)
        page.append(center)

        self._progress_label = Gtk.Label(label="")
        self._progress_label.add_css_class("focus-dim")
        self._progress_label.set_xalign(0.5)
        page.append(self._progress_label)

        self._state_label = Gtk.Label(label="Ready")
        self._state_label.add_css_class("focus-dim")
        self._state_label.set_xalign(0.5)
        page.append(self._state_label)

        pill_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pill_row.set_halign(Gtk.Align.CENTER)
        self._pill = Gtk.Button(label="Start")
        self._pill.add_css_class("focus-pill")
        self._pill.set_tooltip_text("Start / pause the timer")
        self._pill.connect("clicked", self._on_pill_clicked)
        pill_row.append(self._pill)
        page.append(pill_row)

        sec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        sec_row.set_halign(Gtk.Align.CENTER)
        reset_btn = Gtk.Button(label="Reset")
        reset_btn.add_css_class("focus-ghost")
        reset_btn.set_tooltip_text("Reset to a fresh focus session")
        reset_btn.connect("clicked", self._on_reset_clicked)
        sec_row.append(reset_btn)
        full_btn = Gtk.Button(label="Fullscreen")
        full_btn.add_css_class("focus-ghost")
        full_btn.set_tooltip_text("Show a large fullscreen clock")
        full_btn.connect("clicked", self._on_fullscreen_clicked)
        sec_row.append(full_btn)
        self._mark_done_button = Gtk.Button(label="Task done")
        self._mark_done_button.add_css_class("focus-ghost")
        self._mark_done_button.set_tooltip_text("Complete this task in Todoist")
        self._mark_done_button.connect("clicked", self._on_mark_done_clicked)
        self._mark_done_button.set_sensitive(task is not None)
        sec_row.append(self._mark_done_button)
        page.append(sec_row)

        nav_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        nav_row.set_halign(Gtk.Align.CENTER)
        self._nav_btns: dict = {}
        for label, target in (("Today", "today"), ("Timer", "timer"),
                              ("Stats", "stats")):
            b = Gtk.Button(label=label)
            b.add_css_class("focus-nav-active" if target == "timer"
                            else "focus-nav")
            b.connect("clicked", lambda _b, t=target: self._emit_navigate(t))
            self._nav_btns[target] = b
            nav_row.append(b)
        page.append(nav_row)
        self._refresh_task_card()
        self._update_pill()

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
        self._refresh_task_card()
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
        self._refresh_task_card()
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

    def set_session_store(self, store) -> None:
        """Session log used for the per-task focus stats subtitle."""
        self._session_store = store
        self._refresh_task_card()

    def set_on_navigate(self, callback: Optional[Callable[[str], None]]) -> None:
        """Register ``fn(target)`` for the bottom nav (today/timer/stats)."""
        self._on_navigate = callback

    def _emit_navigate(self, target: str) -> None:
        if target == "timer" or self._on_navigate is None:
            return
        try:
            self._on_navigate(target)
        except Exception:
            pass

    def _refresh_task_card(self) -> None:
        """Task title + per-task focus stats (hours / finished sessions)."""
        try:
            if self._task is None:
                self._task_label.set_text("No task selected")
                self._task_stats.set_text("")
                self._mark_done_button.set_sensitive(False)
                return
            self._task_label.set_text(self._task_name())
            tid = str(getattr(self._task, "id", "") or "")
            total, count = 0, 0
            store = self._session_store
            if store is not None:
                try:
                    for s in store.read_all():
                        if (str(getattr(s, "task_id", "") or "") == tid
                                and getattr(s, "finished", False)):
                            total += int(getattr(s, "duration_sec", 0) or 0)
                            count += 1
                except Exception:
                    pass
            if count:
                self._task_stats.set_text(f"{total / 3600:.1f} / {count}")
            else:
                self._task_stats.set_text("No focus yet")
            self._mark_done_button.set_sensitive(True)
        except Exception:
            pass

    def _pill_spec(self):
        """(label, handler) for the single primary pill button."""
        running = self._tick_id is not None
        if running:
            return ("Pause", self._on_pause_clicked)
        if self._mode == self.MODE_FOCUS and self._remaining <= 0:
            return ("Start Break", self._on_break_clicked)
        if self._remaining < self._total:
            return ("Resume", self._on_start_clicked)
        return ("Start", self._on_start_clicked)

    def _update_pill(self) -> None:
        try:
            label, fn = self._pill_spec()
            if self._pill_key != label:
                self._pill.set_label(label)
                self._pill_key = label
            self._pill_fn = fn
        except Exception:
            pass

    def _on_pill_clicked(self, _button: Gtk.Button) -> None:
        fn = self._pill_fn or self._on_start_clicked
        try:
            fn(None)
        except Exception:
            pass

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
        self._state_label.set_text(
            "Focusing…" if self._mode == self.MODE_FOCUS else "On break…"
        )
        self._update_pill()

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
        try:
            total = max(1, int(self._total))
            done = max(0, total - max(0, int(self._remaining)))
            pct = int(round(100.0 * done / total))
            self._progress_label.set_text(
                f"{_format_seconds(done)} of {_format_seconds(total)} · {pct}%"
            )
            self._ring.set_progress(done / total)
        except Exception:
            pass
        self._update_pill()
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
        self._state_label.set_text("Done — take a break or reset.")
        self._refresh_task_card()
        self._update_pill()
        if self._on_done is not None:
            try:
                self._on_done(self._mode)
            except Exception:
                pass

    def _show_idle_state(self) -> None:
        self._update_pill()

    # -- handlers -----------------------------------------------------

    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        if self._remaining <= 0:
            self._remaining = self._total
        self._start_ticking()

    def _on_pause_clicked(self, _button: Gtk.Button) -> None:
        self._stop_ticking()
        self._state_label.set_text("Paused")
        self._update_pill()

    def _on_reset_clicked(self, _button: Gtk.Button) -> None:
        # Log partial focus work (>= 60s) before discarding the countdown.
        self._emit_session(False)
        self._stop_ticking()
        self._remaining = self._total
        self._render()
        self._show_idle_state()
        self._state_label.set_text("Ready")
        self._refresh_task_card()

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
