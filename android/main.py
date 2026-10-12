"""Todoist Pomodoro for Android (Kivy).

Sideload build of the desktop app's logic: Today list, timer, stats,
settings. Reuses backend/ (config, todoist, sync, sessions, actions)
unchanged — only this UI layer is Android-specific.

Entry point for buildozer (android/main.py).
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from actions import TaskActions
from config import TimerSettings, load_timer_settings, save_timer_settings
from sessions import SessionStore, format_duration
from sync import FilterStore, SyncStore
from todoist import TodoistAuthError, TodoistClient, TodoistError


def _fmt_clock(total_sec):
    total_sec = max(0, int(total_sec))
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _display(text, limit=None):
    """Phone-safe display text.

    The bundled device font has no color-emoji glyphs (tofu boxes) and
    Kivy's on-device shorten/text measurement misbehaves, so strip
    astral-plane characters and truncate in Python. Data untouched.
    """
    if limit is None:
        limit = _U.get("trunc", 30)
    clean = "".join(
        ch for ch in str(text or "")
        if ord(ch) < 0x10000 and ch not in ("\uFE0F", "\u200D")
    ).strip()
    if len(clean) > limit:
        return clean[: limit - 1] + "…"
    return clean


def compute_ui(w, h):
    """Screen-fraction sizes: ~7 rows visible on any density.

    Pure function (tested). Floors are phone-first: init_ui() runs once
    at build time when Window.size may still be a small desktop fallback
    (e.g. 800x600), so floors must already be usable on a phone. The
    timer screen re-applies sizes on Window.size changes (see
    TimerScreen._on_window_size), which corrects any early mis-measure.
    """
    w = max(320.0, float(w or 0))
    h = max(480.0, float(h or 0))
    row_h = max(104.0, h / 9.0)
    name_font = max(26.0, row_h * 0.19)
    top_h = max(64.0, h * 0.036)
    nav_h = max(64.0, h * 0.036)
    add_h = max(68.0, h * 0.045)
    banner_h = max(56.0, h * 0.032)
    # Timer: width-driven so a tall/narrow phone still gets a big clock
    # even when height is reduced by system bars at measure time.
    timer_btn_h = max(76.0, h * 0.062)
    clock_font = min(max(h * 0.13, w * 0.24), w * 0.29)
    clock_font = max(min(132.0, w * 0.29), clock_font)
    popup_btn_h = max(64.0, h * 0.045)
    return {
        "row_h": row_h,
        "check_w": max(64.0, row_h * 0.48),
        "menu_w": max(64.0, row_h * 0.5),
        "menu_font": max(28.0, row_h * 0.19),
        "name_font": name_font,
        "meta_font": max(20.0, row_h * 0.115),
        "trunc": max(24, int(w * 0.68 / max(10.0, name_font * 0.55))),
        "meta_trunc": max(40, int(w * 0.80 / max(12.0, max(20.0, row_h * 0.115) * 0.55))),
        "top_h": top_h,
        "title_font": max(28.0, top_h * 0.44),
        "title_w": max(96.0, w * 0.11),
        "refresh_w": max(110.0, w * 0.13),
        "btn_font": max(24.0, top_h * 0.30),
        "filter_font": max(24.0, top_h * 0.30),
        "nav_h": nav_h,
        "nav_font": max(26.0, nav_h * 0.34),
        "status_h": max(36.0, h * 0.015),
        "status_font": max(20.0, h * 0.009),
        "add_h": add_h,
        "add_font": max(24.0, add_h * 0.30),
        "add_btn_w": max(110.0, w * 0.11),
        "banner_h": banner_h,
        "timer_back_h": max(56.0, h * 0.04),
        "timer_back_font": max(20.0, h * 0.012),
        "timer_task_font": min(max(32.0, w * 0.075), 64.0),
        "clock_font": clock_font,
        "timer_state_font": max(19.0, h * 0.011),
        "timer_mode_font": max(20.0, h * 0.011),
        "timer_prog_font": max(17.0, h * 0.009),
        "timer_btn_h": timer_btn_h,
        "timer_btn_font": max(21.0, timer_btn_h * 0.30),
        "setup_title": min(max(30.0, h * 0.024), 64.0),
        "setup_entry_h": max(56.0, h * 0.04),
        "setup_entry_font": min(30.0, max(18.0, h * 0.011)),
        "setup_btn_h": max(64.0, h * 0.05),
        "setup_btn_font": min(30.0, max(20.0, h * 0.012)),
        "stats_body": min(24.0, max(16.0, h * 0.008)),
        "popup_menu_h": h * 0.30,
        "popup_btn_h": popup_btn_h,
        "popup_btn_font": min(26.0, max(18.0, h * 0.01)),
        "popup_date_h": h * 0.22,
    }


_U = {}


def init_ui():
    """Fill _U from the real window (or POMO_SIM_W/H override for tests)."""
    import os

    try:
        w = float(os.environ.get("POMO_SIM_W") or Window.width or 0)
        h = float(os.environ.get("POMO_SIM_H") or Window.height or 0)
    except Exception:
        w, h = 0, 0
    _U.clear()
    _U.update(compute_ui(w, h))


def _left_aligned(text, props):
    """Left-aligned label without text_size/shorten.

    On-device SDL text measurement misbehaves (mirrored text), so align
    with an AnchorLayout and size the label from its own texture.
    """
    anchor = AnchorLayout(anchor_x="left", anchor_y="center")
    label = Label(text=text, size_hint=(None, None), **props)
    label.bind(texture_size=label.setter("size"))
    anchor.add_widget(label)
    return anchor


# -- shared dark theme (matches the TimerScreen redesign) ----------------
_FG = (0.90, 0.91, 0.97, 1)
_DIM = (0.65, 0.68, 0.78, 1)
_ACCENT = (0.54, 0.71, 0.98, 1)
_GREEN = (0.65, 0.89, 0.63, 1)
_RED = (0.95, 0.55, 0.66, 1)
_CARD = (0.19, 0.19, 0.27, 1)
_BG = (0.07, 0.07, 0.11, 1)
_BTN = (0.27, 0.28, 0.35, 1)
_DARK_TEXT = (0.07, 0.07, 0.11, 1)


def _paint_bg(widget, color, radius=0):
    """Flat (rounded) rect behind a layout; tracks size/pos. No-ops on failure."""
    try:
        with widget.canvas.before:
            Color(*color)
            rect = RoundedRectangle(pos=widget.pos, size=widget.size,
                                    radius=[radius])
        widget.bind(pos=lambda _i, v: setattr(rect, "pos", v),
                    size=lambda _i, v: setattr(rect, "size", v))
    except Exception:
        pass


def _flat_button(label, font_size, bg=_BTN, fg=_FG, **kwargs):
    """Borderless flat button in theme colors."""
    return Button(text=label, font_size=font_size, bold=True,
                  background_normal="", background_color=bg, color=fg,
                  **kwargs)


def _dark_entry(hint, font_size, **kwargs):
    """TextInput in theme colors (no default white/gray)."""
    return TextInput(hint_text=hint, multiline=False, font_size=font_size,
                     background_normal="", background_color=(0.10, 0.10, 0.14, 1),
                     foreground_color=_FG, hint_text_color=_DIM,
                     cursor_color=_ACCENT, **kwargs)


class StatusBanner(BoxLayout):
    """Bottom banner: message + optional Undo, auto-hides."""

    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=0, **kwargs)
        self._label = Label(text="", halign="left", valign="middle")
        self._label.bind(size=self._label.setter("text_size"))
        self._undo = Button(text="Undo", font_size=_U.get("btn_font", 16),
                            size_hint_x=None, width=_U.get("refresh_w", 110))
        self._undo.bind(on_release=self._on_undo)
        self._task = None
        self._hide_ev = None
        self.add_widget(self._label)
        self.add_widget(self._undo)
        self._undo.opacity = 0
        self._undo.disabled = True

    def show(self, text, task=None, seconds=5):
        app = App.get_running_app()
        self._label.text = text
        self._task = task
        can_undo = task is not None
        self._undo.opacity = 1 if can_undo else 0
        self._undo.disabled = not can_undo
        self.height = _U.get("banner_h", 72)
        if self._hide_ev is not None:
            self._hide_ev.cancel()
        self._hide_ev = Clock.schedule_once(lambda _dt: self.hide(), seconds)

    def hide(self):
        self.height = 0
        self._task = None
        self._undo.opacity = 0
        self._undo.disabled = True

    def _on_undo(self, _btn):
        task = self._task
        if task is None:
            return
        if self._hide_ev is not None:
            self._hide_ev.cancel()
            self._hide_ev = None
        self.hide()
        App.get_running_app().reopen_task(task)


class SetupScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="setup", **kwargs)
        box = BoxLayout(orientation="vertical", padding=24, spacing=12)
        box.add_widget(Label(text="Todoist Pomodoro", font_size=_U.get("setup_title", 38),
                             size_hint_y=None, height=_U.get("setup_title", 38) * 2))
        box.add_widget(Label(
            text="Paste your Todoist personal API token.\nTodoist > Settings > Integrations > Developer.",
            font_size=_U.get("setup_entry_font", 20),
            halign="center"))
        self._entry = TextInput(hint_text="Todoist API token", multiline=False,
                                password=True, size_hint_y=None,
                                height=_U.get("setup_entry_h", 64),
                                font_size=_U.get("setup_entry_font", 20))
        box.add_widget(self._entry)
        self._status = Label(text="", halign="center")
        box.add_widget(self._status)
        go = Button(text="Connect", font_size=_U.get("setup_btn_font", 22),
                      size_hint_y=None, height=_U.get("setup_btn_h", 72),
                      background_normal="", background_color=(0.2, 0.5, 0.9, 1))
        go.bind(on_release=self._on_connect)
        box.add_widget(go)
        self.add_widget(box)

    def _on_connect(self, _btn):
        token = self._entry.text.strip()
        if not token:
            self._status.text = "Enter a token first."
            return
        self._status.text = "Connecting…"
        threading.Thread(target=self._worker, args=(token,), daemon=True).start()

    def _worker(self, token):
        try:
            client = TodoistClient(token, timeout=15)
            store = SyncStore(client)
            store.refresh_projects()
        except TodoistAuthError:
            Clock.schedule_once(lambda _dt: setattr(self._status, "text", "Invalid token."))
        except Exception as exc:
            Clock.schedule_once(lambda _dt, _msg=str(exc): setattr(self._status, "text", f"Failed: {_msg}"))
        else:
            Clock.schedule_once(lambda _dt: App.get_running_app().finish_setup(token))


def _task_is_overdue(task) -> bool:
    """Best-effort overdue check from a due_date (date/datetime/ISO string)."""
    import datetime as _dt

    due = getattr(task, "due_date", None)
    if not due:
        return False
    try:
        today = _dt.date.today()
        if isinstance(due, _dt.datetime):
            return due.date() < today
        if isinstance(due, _dt.date):
            return due < today
        return _dt.date.fromisoformat(str(due).strip()[:10]) < today
    except (ValueError, TypeError):
        return False


class TaskRow(ButtonBehavior, BoxLayout):
    """One task: checkbox completes, single tap opens the timer."""

    def __init__(self, task, meta, overdue=False, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=_U.get("row_h", 150), spacing=8, padding=(12, 8),
                         **kwargs)
        _paint_bg(self, _CARD, radius=18)
        self.task = task
        check = CheckBox(size_hint_x=None, width=_U.get("check_w", 72),
                         color=_ACCENT)
        check.bind(active=self._on_check)
        self.add_widget(check)
        mid = BoxLayout(orientation="vertical", spacing=2)
        mid.add_widget(_left_aligned(
            _display(task.content), {"font_size": _U.get("name_font", 30),
                                     "bold": True, "color": _FG}))
        if meta:
            mid.add_widget(_left_aligned(
                _display(meta, _U.get("meta_trunc", 40)),
                {"font_size": _U.get("meta_font", 20),
                 "bold": bool(overdue),
                 "color": _RED if overdue else _DIM}))
        self.add_widget(mid)
        menu = Button(text="···", font_size=_U.get("menu_font", 24),
                      bold=True, size_hint_x=None, width=_U.get("menu_w", 72),
                      background_normal="",
                      background_color=(0.19, 0.19, 0.27, 1),
                      color=_ACCENT)
        menu.bind(on_release=lambda _b: App.get_running_app().open_task_menu(task))
        self.add_widget(menu)

    def on_release(self):
        App.get_running_app().open_timer(self.task)

    def _on_check(self, _box, active):
        if active:
            App.get_running_app().complete_task(self.task)


class TodayScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="today", **kwargs)
        root = BoxLayout(orientation="vertical", padding=16, spacing=10)
        _paint_bg(root, _BG)
        top = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("top_h", 72), spacing=10)
        self._title = Label(text="Today", bold=True,
                            font_size=_U.get("title_font", 30), color=_FG,
                            size_hint_x=None, width=_U.get("title_w", 100),
                            halign="left", valign="middle")
        self._title.bind(size=self._title.setter("text_size"))
        top.add_widget(self._title)
        self._filter = _dark_entry("Filter tasks…",
                                   _U.get("filter_font", 20))
        self._filter.bind(text=lambda _i, _v: self._apply_filter())
        top.add_widget(self._filter)
        self._refresh_btn = _flat_button("Refresh", _U.get("btn_font", 20),
                                         size_hint_x=None,
                                         width=_U.get("refresh_w", 120))
        self._refresh_btn.bind(on_release=lambda _b: self.refresh())
        top.add_widget(self._refresh_btn)
        root.add_widget(top)

        nav = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("nav_h", 72), spacing=10)
        self._nav_buttons: list = []
        for label, screen in (("Timer", "timer"), ("Stats", "stats"), ("Settings", "settings")):
            b = _flat_button(label, _U.get("nav_font", 22))
            b.bind(on_release=lambda _b, s=screen: self._go(s))
            self._nav_buttons.append(b)
            nav.add_widget(b)
        root.add_widget(nav)

        self._status = Label(text="Loading…", size_hint_y=None,
                             height=_U.get("status_h", 32),
                             font_size=_U.get("status_font", 17),
                             color=_DIM, halign="left", valign="middle")
        self._status.bind(size=self._status.setter("text_size"))
        root.add_widget(self._status)
        scroll = ScrollView()
        self._list = GridLayout(cols=1, size_hint_y=None, spacing=8)
        self._list.bind(minimum_height=self._list.setter("height"))
        scroll.add_widget(self._list)
        root.add_widget(scroll)

        add_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=_U.get("add_h", 76), spacing=10)
        self._add_entry = _dark_entry("Try 'Call mom Mon 2pm…'",
                                      _U.get("add_font", 20))
        self._add_entry.bind(on_text_validate=lambda _i: self._on_add())
        add_row.add_widget(self._add_entry)
        self._add_btn = _flat_button("Add", _U.get("add_font", 20),
                                     bg=_ACCENT, fg=_DARK_TEXT,
                                     size_hint_x=None,
                                     width=_U.get("add_btn_w", 110))
        self._add_btn.bind(on_release=lambda _b: self._on_add())
        add_row.add_widget(self._add_btn)
        root.add_widget(add_row)

        self.banner = StatusBanner()
        root.add_widget(self.banner)
        self.add_widget(root)
        self._tasks = []
        try:
            Window.bind(size=self._on_window_size)
        except Exception:
            pass

    def _on_window_size(self, _win, _size) -> None:
        try:
            init_ui()
        except Exception:
            return
        try:
            self._title.font_size = _U.get("title_font", 30)
            self._filter.font_size = _U.get("filter_font", 20)
            self._refresh_btn.font_size = _U.get("btn_font", 20)
            for b in self._nav_buttons:
                b.font_size = _U.get("nav_font", 22)
            self._status.font_size = _U.get("status_font", 17)
            self._add_entry.font_size = _U.get("add_font", 20)
            self._add_btn.font_size = _U.get("add_font", 20)
        except Exception:
            pass
        try:
            self._apply_filter()
        except Exception:
            pass

    def _go(self, screen):
        if screen == "timer" and App.get_running_app().timer_task is None:
            self.banner.show("Pick a task first (tap its ··· → Start timer).", None, 4)
            return
        self.manager.current = screen

    def on_pre_enter(self):
        self.refresh()

    def refresh(self):
        self._status.text = "Loading…"
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        app = App.get_running_app()
        if getattr(app, "store", None) is None:
            # Connect still in flight (first launch): wait for it.
            Clock.schedule_once(
                lambda _dt: setattr(self._status, "text", "Connecting…"))
            return
        try:
            tasks = app.store.refresh_today()
            try:
                app.store.refresh_projects()
            except Exception:
                pass
            metas = {}
            for t in tasks:
                try:
                    metas[str(t.id)] = app.describe(t)
                except Exception:
                    metas[str(t.id)] = ""
        except TodoistAuthError:
            Clock.schedule_once(lambda _dt: setattr(self._status, "text", "Token invalid — update in Settings."))
        except Exception as exc:
            Clock.schedule_once(lambda _dt, _msg=str(exc): setattr(self._status, "text", f"Failed: {_msg}"))
        else:
            Clock.schedule_once(lambda _dt: self._show(tasks, metas))

    def _show(self, tasks, metas):
        self._tasks = list(tasks)
        self._metas = dict(metas)
        self._apply_filter()
        self._status.text = f"{len(tasks)} due" if tasks else "Nothing due — enjoy the calm."

    def _apply_filter(self):
        q = self._filter.text.strip().lower()
        self._list.clear_widgets()
        for t in self._tasks:
            if q and q not in str(t.content or "").lower():
                continue
            self._list.add_widget(TaskRow(t, self._metas.get(str(t.id), ""),
                                          overdue=_task_is_overdue(t)))

    def drop_row(self, task_id):
        self._tasks = [t for t in self._tasks if str(t.id) != str(task_id)]
        self._apply_filter()

    def _on_add(self):
        text = self._add_entry.text.strip()
        if not text:
            return
        self._add_entry.text = ""
        self._status.text = "Adding…"
        threading.Thread(target=self._add_worker, args=(text,), daemon=True).start()

    def _add_worker(self, text):
        app = App.get_running_app()
        try:
            app.actions.quick_add(text)
        except Exception as exc:
            Clock.schedule_once(lambda _dt, _msg=str(exc): self.banner.show(f"Could not add: {_msg}", None, 5))
        else:
            Clock.schedule_once(lambda _dt: self.refresh())


class TimerScreen(Screen):
    _FG = (0.90, 0.91, 0.97, 1)
    _DIM = (0.65, 0.68, 0.78, 1)
    _ACCENT = (0.54, 0.71, 0.98, 1)
    _GREEN = (0.65, 0.89, 0.63, 1)

    def __init__(self, **kwargs):
        super().__init__(name="timer", **kwargs)
        root = BoxLayout(orientation="vertical", padding=20, spacing=10)
        _paint_bg(root, _BG)
        top = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("timer_back_h", 64), spacing=10)
        self._back_btn = Button(text="< Today",
                                font_size=_U.get("timer_back_font", 20),
                                size_hint_x=0.38, bold=True,
                                background_normal="",
                                background_color=(0.27, 0.28, 0.35, 1))
        self._back_btn.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        top.add_widget(self._back_btn)
        self._mode_label = Label(text="FOCUS", bold=True,
                                 font_size=_U.get("timer_mode_font", 20),
                                 color=self._ACCENT, halign="center",
                                 valign="middle", size_hint_x=0.62)
        self._mode_label.bind(size=self._mode_label.setter("text_size"))
        top.add_widget(self._mode_label)
        root.add_widget(top)
        self._task_label = Label(text="No task", bold=True,
                                 font_size=_U.get("timer_task_font", 34),
                                 color=self._FG, halign="center",
                                 valign="middle", size_hint_y=0.42)
        self._task_label.bind(size=self._task_label.setter("text_size"))
        root.add_widget(self._task_label)
        self._clock = Label(text="25:00", bold=True,
                            font_size=_U.get("clock_font", 170),
                            color=self._FG, halign="center",
                            valign="middle", size_hint_y=1.5,
                            shorten=False)
        self._clock.bind(size=self._clock.setter("text_size"))
        root.add_widget(self._clock)
        self._progress = Label(text="", font_size=_U.get("timer_prog_font", 17),
                               color=self._DIM, halign="center",
                               size_hint_y=None, height=30)
        root.add_widget(self._progress)
        self._state = Label(text="Ready", font_size=_U.get("timer_state_font", 19),
                            color=self._DIM, halign="center",
                            size_hint_y=None, height=34)
        root.add_widget(self._state)
        row1 = BoxLayout(orientation="horizontal", size_hint_y=None,
                         height=_U.get("timer_btn_h", 88), spacing=10)
        self._ctl_buttons: list = []
        for label, fn in (("Start", self._start), ("Pause", self._pause),
                          ("Reset", self._reset), ("Break", self._break)):
            is_primary = (label == "Start")
            b = Button(text=label, bold=True,
                       font_size=_U.get("timer_btn_font", 24),
                       background_normal="",
                       background_color=(0.54, 0.71, 0.98, 1) if is_primary else (0.27, 0.28, 0.35, 1),
                       color=(0.07, 0.07, 0.11, 1) if is_primary else (0.90, 0.91, 0.97, 1))
            b.bind(on_release=lambda _b, f=fn: f())
            self._ctl_buttons.append(b)
            row1.add_widget(b)
        root.add_widget(row1)
        self._done_btn = Button(text="Mark task done", bold=True,
                                size_hint_y=None,
                                height=_U.get("timer_btn_h", 88),
                                font_size=_U.get("timer_btn_font", 24),
                                background_normal="",
                                background_color=(0.25, 0.55, 0.35, 1),
                                color=(1, 1, 1, 1))
        self._done_btn.bind(on_release=lambda _b: self._mark_done())
        root.add_widget(self._done_btn)
        self.banner = StatusBanner()
        root.add_widget(self.banner)
        self.add_widget(root)
        self._tick_ev = None
        self._remaining = 25 * 60
        self._total = 25 * 60
        self._mode = "focus"
        self._run_start = None
        self._elapsed_base = 0
        self._set_mode("focus")
        try:
            Window.bind(size=self._on_window_size)
        except Exception:
            pass

    def _on_window_size(self, _win, _size) -> None:
        try:
            init_ui()
        except Exception:
            return
        self._apply_sizes()

    def _apply_sizes(self) -> None:
        try:
            self._back_btn.font_size = _U.get("timer_back_font", 20)
            self._back_btn.height = _U.get("timer_back_h", 64)
            self._mode_label.font_size = _U.get("timer_mode_font", 20)
            self._task_label.font_size = _U.get("timer_task_font", 34)
            self._clock.font_size = _U.get("clock_font", 170)
            self._progress.font_size = _U.get("timer_prog_font", 17)
            self._state.font_size = _U.get("timer_state_font", 19)
            for b in self._ctl_buttons:
                b.font_size = _U.get("timer_btn_font", 24)
                b.height = _U.get("timer_btn_h", 88)
            self._done_btn.font_size = _U.get("timer_btn_font", 24)
            self._done_btn.height = _U.get("timer_btn_h", 88)
        except Exception:
            pass

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        is_break = (mode == "break")
        try:
            self._mode_label.text = "BREAK" if is_break else "FOCUS"
            self._mode_label.color = self._GREEN if is_break else self._ACCENT
        except Exception:
            pass

    def on_pre_enter(self):
        app = App.get_running_app()
        s = app.settings
        if self._tick_ev is None:
            self._total = s.focus_min * 60
            self._remaining = self._total
            self._elapsed_base = 0
            self._set_mode("focus")
        task = app.timer_task
        self._task_label.text = _display(task.content, 60) if task is not None else "No task"
        self._apply_sizes()
        self._render()

    def _render(self):
        self._clock.text = _fmt_clock(self._remaining)
        try:
            total = max(1, int(self._total))
            done = max(0, total - max(0, int(self._remaining)))
            pct = int(round(100.0 * done / total))
            self._progress.text = f"{_fmt_clock(done)} of {_fmt_clock(total)} · {pct}%"
        except Exception:
            pass

    def _start(self):
        if self._tick_ev is not None:
            return
        if self._remaining <= 0:
            self._remaining = self._total
        self._run_start = time.monotonic()
        self._tick_ev = Clock.schedule_interval(self._tick, 1)
        self._state.text = "Focusing…"

    def _tick(self, _dt):
        self._remaining = max(0, self._total - (self._elapsed_base + int(time.monotonic() - self._run_start)))
        self._render()
        if self._remaining <= 0:
            self._finish()

    def _pause(self):
        if self._tick_ev is None:
            return
        self._elapsed_base += int(time.monotonic() - self._run_start)
        self._tick_ev.cancel()
        self._tick_ev = None
        self._remaining = max(0, self._total - self._elapsed_base)
        self._render()
        self._state.text = "Paused"

    def _worked_sec(self):
        if self._tick_ev is not None:
            return self._elapsed_base + int(time.monotonic() - self._run_start)
        return self._elapsed_base

    def _stop_clock(self):
        if self._tick_ev is not None:
            self._tick_ev.cancel()
            self._tick_ev = None

    def _reset(self, log_partial=True):
        worked = self._worked_sec()
        self._stop_clock()
        if log_partial and self._mode == "focus" and worked >= 60:
            App.get_running_app().log_session(worked, finished=False)
        self._elapsed_base = 0
        self._remaining = self._total
        self._render()
        self._state.text = "Ready"

    def _break(self):
        self._reset(log_partial=True)
        self._set_mode("break")
        self._total = App.get_running_app().settings.short_break_min * 60
        self._remaining = self._total
        self._render()
        self._state.text = "On a break"
        self._start()

    def _finish(self):
        self._stop_clock()
        if self._mode == "focus":
            App.get_running_app().log_session(self._total, finished=True)
        self._elapsed_base = 0
        self._set_mode("focus")
        self._total = App.get_running_app().settings.focus_min * 60
        self._remaining = self._total
        self._render()
        self._state.text = "Done — take a break or reset."

    def _mark_done(self):
        task = App.get_running_app().timer_task
        if task is None:
            return
        self._reset(log_partial=True)
        App.get_running_app().complete_task(task)

    def stop_if_bound(self, task_id):
        app = App.get_running_app()
        task = app.timer_task
        if task is None or str(task.id) != str(task_id):
            return False
        if self._tick_ev is None and self._elapsed_base == 0:
            return False
        self._reset(log_partial=True)
        self._state.text = "Ready — task completed."
        return True


class StatsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="stats", **kwargs)
        root = BoxLayout(orientation="vertical", padding=12, spacing=8)
        top = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("top_h", 72), spacing=8)
        back = Button(text="< Today", font_size=_U.get("btn_font", 18),
                      size_hint_x=None, width=_U.get("title_w", 120))
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        top.add_widget(back)
        top.add_widget(Label(text="Stats", font_size=_U.get("title_font", 30), halign="left"))
        refresh = Button(text="Refresh", font_size=_U.get("btn_font", 18),
                         size_hint_x=None, width=_U.get("refresh_w", 110))
        refresh.bind(on_release=lambda _b: self.refresh())
        top.add_widget(refresh)
        root.add_widget(top)
        scroll = ScrollView()
        self._body = Label(text="", font_size=_U.get("stats_body", 18),
                             halign="left", valign="top", size_hint_y=None)
        self._body.bind(texture_size=lambda _i, v: setattr(self._body, "height", v[1]),
                        width=lambda _i, v: setattr(self._body, "text_size", (v, None)))
        scroll.add_widget(self._body)
        root.add_widget(scroll)
        self.add_widget(root)

    def on_pre_enter(self):
        self.refresh()

    def refresh(self):
        try:
            data = App.get_running_app().session_store.stats()
        except Exception as exc:
            self._body.text = f"Failed: {exc}"
            return
        lines = [
            f"Today: {format_duration(data['today_sec'])}",
            f"Last 7 days: {format_duration(data['week_sec'])}",
            f"Day streak: {data['streak']}",
            "",
            "By project:",
        ]
        if data["by_project"]:
            for name, sec in sorted(data["by_project"].items(), key=lambda kv: -kv[1]):
                lines.append(f"  {name}: {format_duration(sec)}")
        else:
            lines.append("  Nothing yet.")
        lines.append("")
        lines.append("By task:")
        if data["by_task"]:
            for name, sec in sorted(data["by_task"].items(), key=lambda kv: -kv[1])[:20]:
                lines.append(f"  {name}: {format_duration(sec)}")
        else:
            lines.append("  Nothing yet.")
        self._body.text = "\n".join(lines)


class SettingsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="settings", **kwargs)
        root = BoxLayout(orientation="vertical", padding=24, spacing=12)
        back = Button(text="< Today", font_size=_U.get("timer_back_font", 20),
                      size_hint_y=None, height=_U.get("timer_back_h", 64))
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        root.add_widget(back)
        root.add_widget(Label(text="Settings", font_size=_U.get("title_font", 30),
                                 size_hint_y=None, height=_U.get("top_h", 72)))
        root.add_widget(Label(text="Focus minutes:", font_size=_U.get("setup_entry_font", 20),
                                 size_hint_y=None, height=_U.get("setup_entry_font", 20) * 2))
        self._focus = TextInput(multiline=False, input_filter="int", size_hint_y=None,
                                height=_U.get("setup_entry_h", 64),
                                font_size=_U.get("setup_entry_font", 22))
        root.add_widget(self._focus)
        root.add_widget(Label(text="Break minutes:", font_size=_U.get("setup_entry_font", 20),
                                 size_hint_y=None, height=_U.get("setup_entry_font", 20) * 2))
        self._break = TextInput(multiline=False, input_filter="int", size_hint_y=None,
                                height=_U.get("setup_entry_h", 64),
                                font_size=_U.get("setup_entry_font", 22))
        root.add_widget(self._break)
        root.add_widget(Label(text="Todoist API token:", font_size=_U.get("setup_entry_font", 20),
                                 size_hint_y=None, height=_U.get("setup_entry_font", 20) * 2))
        self._token = TextInput(multiline=False, password=True, size_hint_y=None,
                                height=_U.get("setup_entry_h", 64),
                                font_size=_U.get("setup_entry_font", 20))
        root.add_widget(self._token)
        self._status = Label(text="")
        root.add_widget(self._status)
        save = Button(text="Save", font_size=_U.get("setup_btn_font", 22), size_hint_y=None, height=_U.get("setup_btn_h", 72))
        save.bind(on_release=lambda _b: self._save())
        root.add_widget(save)
        self.add_widget(root)

    def on_pre_enter(self):
        app = App.get_running_app()
        self._focus.text = str(app.settings.focus_min)
        self._break.text = str(app.settings.short_break_min)
        self._token.text = app.token or ""

    def _save(self):
        app = App.get_running_app()
        try:
            focus = max(1, min(180, int(self._focus.text or app.settings.focus_min)))
            brk = max(1, min(60, int(self._break.text or app.settings.short_break_min)))
        except ValueError:
            self._status.text = "Durations must be numbers."
            return
        app.settings = TimerSettings(focus_min=focus, short_break_min=brk)
        save_timer_settings(app.settings_path, app.settings)
        token = self._token.text.strip()
        if token and token != app.token:
            app.switch_token(token)
            self._status.text = "Saved — reconnecting…"
        else:
            self._status.text = "Saved."


class PomodoroApp(App):
    def build(self):
        init_ui()
        self.data_dir = self.user_data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.token_path = os.path.join(self.data_dir, "token.txt")
        self.settings_path = os.path.join(self.data_dir, "settings.json")
        self.session_path = os.path.join(self.data_dir, "sessions.jsonl")
        self.filter_path = os.path.join(self.data_dir, "filters.json")
        self.settings = load_timer_settings(self.settings_path)
        self.token = self._read_token()
        self.session_store = SessionStore(self.session_path)
        self.timer_task = None
        self.sm = ScreenManager()
        self.today = TodayScreen()
        self.timer = TimerScreen()
        self.sm.add_widget(self.today)
        self.sm.add_widget(self.timer)
        self.sm.add_widget(StatsScreen())
        self.sm.add_widget(SettingsScreen())
        if self.token:
            self._connect(self.token, self._on_first_connect,
                          lambda msg: self.today.banner.show(msg, None, 5))
        else:
            self.sm.add_widget(SetupScreen())
            self.sm.current = "setup"
        return self.sm

    def _go(self, screen):
        self.sm.current = screen

    def _read_token(self):
        try:
            with open(self.token_path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _write_token(self, token):
        with open(self.token_path, "w", encoding="utf-8") as fh:
            fh.write(token.strip())

    def finish_setup(self, token):
        self._write_token(token)
        self._connect(token, self._on_first_connect, lambda msg: None)

    def _on_first_connect(self):
        self._go("today")
        self.today.refresh()

    def switch_token(self, token):
        self.token = token
        self._write_token(token)
        self._connect(token, lambda: self.today.refresh(), lambda msg: None)

    def _connect(self, token, on_ok, on_fail):
        def worker():
            try:
                client = TodoistClient(token, timeout=15)
                store = SyncStore(client)
                store.refresh_projects()
            except Exception as exc:
                Clock.schedule_once(lambda _dt, _msg=str(exc): on_fail(f"Connect failed: {_msg}"))
            else:
                def apply(_dt):
                    self.token = token
                    self.store = store
                    self.actions = TaskActions(store, on_start_timer=self._ui_start_timer)
                    on_ok()
                Clock.schedule_once(apply)
        threading.Thread(target=worker, daemon=True).start()

    def _ui_start_timer(self, task):
        self.timer_task = task
        self._go("timer")
        return None

    def describe(self, task):
        parts = []
        try:
            name = self.store.project_name(str(task.project_id or ""))
        except Exception:
            name = ""
        if name:
            parts.append(name)
        due = getattr(task, "due_date", None)
        if due:
            parts.append(f"due {str(due)[:10]}")
        if getattr(task, "priority", 1) and task.priority >= 2:
            parts.append(f"P{task.priority}")
        return " · ".join(parts)

    # -- shared command paths (mirror the desktop banner flow) --------

    def open_timer(self, task):
        self.timer_task = task
        self._go("timer")

    def open_task_menu(self, task):
        layout = BoxLayout(orientation="vertical", spacing=8, padding=12)
        popup = Popup(title=_display(task.content, 40), content=layout,
                      size_hint=(0.9, None), height=_U.get("popup_menu_h", 480))
        start = Button(text="Start timer", font_size=_U.get("popup_btn_font", 20), size_hint_y=None, height=_U.get("popup_btn_h", 68))
        start.bind(on_release=lambda _b: (popup.dismiss(), self.open_timer(task)))
        layout.add_widget(start)
        done = Button(text="Complete", font_size=_U.get("popup_btn_font", 20), size_hint_y=None, height=_U.get("popup_btn_h", 68))
        done.bind(on_release=lambda _b: (popup.dismiss(), self.complete_task(task)))
        layout.add_widget(done)
        for label, due in (("Tomorrow", "tomorrow"), ("Next week", "next week")):
            b = Button(text=f"Reschedule: {label}", font_size=_U.get("popup_btn_font", 20), size_hint_y=None, height=_U.get("popup_btn_h", 68))
            b.bind(on_release=lambda _b, d=due: (popup.dismiss(), self.reschedule_task(task, d)))
            layout.add_widget(b)
        custom = Button(text="Change date…", font_size=_U.get("popup_btn_font", 20), size_hint_y=None, height=_U.get("popup_btn_h", 68))
        custom.bind(on_release=lambda _b: (popup.dismiss(), self.open_custom_date(task)))
        layout.add_widget(custom)
        popup.open()

    def open_custom_date(self, task):
        layout = BoxLayout(orientation="vertical", spacing=8, padding=12)
        entry = TextInput(hint_text="e.g. tomorrow, Mon 2pm", multiline=False,
                          size_hint_y=None, height=64, font_size=20)
        layout.add_widget(entry)
        popup = Popup(title="Change date", content=layout, size_hint=(0.9, None), height=_U.get("popup_date_h", 340))
        entry.bind(on_text_validate=lambda _i: (popup.dismiss(), self.reschedule_task(task, entry.text.strip())))
        go = Button(text="Save", font_size=_U.get("setup_btn_font", 22), size_hint_y=None, height=_U.get("setup_btn_h", 72))
        go.bind(on_release=lambda _b: (popup.dismiss(), self.reschedule_task(task, entry.text.strip())))
        layout.add_widget(go)
        popup.open()

    def complete_task(self, task):
        content = _display(task.content or "Task", 60)
        threading.Thread(target=self._complete_worker, args=(task, content), daemon=True).start()

    def _complete_worker(self, task, content):
        try:
            self.store.close(task)
        except Exception as exc:
            Clock.schedule_once(
                lambda _dt, _msg=str(exc): self.today.banner.show(f"Could not complete: {_msg}", None, 5))
        else:
            task_id = str(task.id)
            def apply(_dt):
                self.today.drop_row(task_id)
                self.timer.stop_if_bound(task_id)
                self.today.banner.show(f"Completed: {content}", task, 5)
            Clock.schedule_once(apply)

    def reopen_task(self, task):
        threading.Thread(target=self._reopen_worker, args=(task,), daemon=True).start()

    def _reopen_worker(self, task):
        try:
            self.store.reopen(task)
        except Exception as exc:
            Clock.schedule_once(
                lambda _dt, _msg=str(exc): self.today.banner.show(f"Could not undo: {_msg}", None, 5))
        else:
            Clock.schedule_once(lambda _dt: self.today.refresh())
            Clock.schedule_once(
                lambda _dt: self.today.banner.show("Task restored.", None, 3))

    def reschedule_task(self, task, due_string):
        if not due_string.strip():
            self.today.banner.show("Enter a date like 'tomorrow'.", None, 4)
            return
        threading.Thread(target=self._reschedule_worker, args=(task, due_string), daemon=True).start()

    def _reschedule_worker(self, task, due_string):
        try:
            self.actions.reschedule(task, due_string.strip())
        except Exception as exc:
            Clock.schedule_once(
                lambda _dt, _msg=str(exc): self.today.banner.show(f"Could not reschedule: {_msg}", None, 5))
        else:
            Clock.schedule_once(lambda _dt: self.today.refresh())
            Clock.schedule_once(
                lambda _dt: self.today.banner.show(f"Moved to {due_string.strip()}.", None, 3))

    def log_session(self, duration_sec, finished):
        try:
            task = self.timer_task
            self.session_store.log(
                task_id=str(task.id) if task is not None else "",
                content=task.content if task is not None else "",
                project_id=str(getattr(task, "project_id", "") or "") if task else "",
                duration_sec=int(duration_sec),
                finished=finished,
            )
        except Exception:
            pass


if __name__ == "__main__":
    PomodoroApp().run()
