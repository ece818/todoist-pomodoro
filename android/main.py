"""Todoist Pomodoro for Android (Kivy).

Sideload build of the desktop app's logic: Today list, timer, stats,
settings. Reuses backend/ (config, todoist, sync, sessions, actions)
unchanged — only this UI layer is Android-specific.

Entry point for buildozer (android/main.py).
"""

import os
import math
import random
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.properties import NumericProperty
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

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
    timer_btn_h = max(88.0, h * 0.068)
    clock_font = min(max(h * 0.13, w * 0.24), w * 0.29)
    clock_font = max(min(132.0, w * 0.29), clock_font)
    status_font = max(34.0, h * 0.02)
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
        "btn_font": min(max(28.0, top_h * 0.34), w * 0.06),
        "filter_font": max(28.0, top_h * 0.34),
        "nav_h": nav_h,
        "nav_font": min(max(34.0, nav_h * 0.44), w * 0.06),
        "status_font": status_font,
        "status_h": max(56.0, status_font + 10),
        "banner_font": max(30.0, h * 0.012),
        "stat_hero": min(max(64.0, h * 0.028), ((w - 52) / 3 - 24) / 4.5),
        "stat_cap": max(28.0, h * 0.011),
        "stat_row": max(120.0, h * 0.045),
        "add_h": add_h,
        "add_font": max(24.0, add_h * 0.30),
        "add_btn_w": max(110.0, w * 0.11),
        "banner_h": banner_h,
        "card_h": max(112.0, h * 0.085),
        "pill_h": max(96.0, h * 0.072),
        "pill_font": max(32.0, h * 0.016),
        "check_d": max(56.0, h * 0.032),
        "ghost_font": max(26.0, h * 0.013),
        "timer_back_h": max(64.0, h * 0.045),
        "timer_back_font": max(26.0, h * 0.014),
        "timer_task_font": min(max(32.0, w * 0.075), 64.0),
        "clock_font": clock_font,
        "timer_state_font": max(24.0, h * 0.013),
        "timer_mode_font": max(26.0, h * 0.013),
        "timer_prog_font": max(22.0, h * 0.011),
        "timer_btn_h": timer_btn_h,
        # Width-capped: measured bold "Break" ≈ 3.25em wide; keep 16px
        # padding inside each of the 4 row buttons (row = w - 40 pad - 30 gaps).
        "timer_btn_font": min(max(26.0, timer_btn_h * 0.32),
                              ((w - 70) / 4 - 16) / 3.25),
        "setup_title": min(max(40.0, h * 0.024), 76.0),
        "setup_entry_h": max(64.0, h * 0.04),
        "setup_entry_font": max(34.0, h * 0.013),
        "setup_btn_h": max(72.0, h * 0.055),
        "setup_btn_font": max(34.0, h * 0.013),
        "stats_body": max(34.0, h * 0.013),
        "popup_menu_h": h * 0.30,
        "popup_btn_h": popup_btn_h,
        "popup_btn_font": max(30.0, h * 0.012),
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


def _flat_button(label, font_size, bg=_BTN, fg=_FG, radius=18, **kwargs):
    """Borderless rounded button in theme colors.

    The default bg image is disabled (transparent) and a rounded rect
    is painted behind, so corners stay round in all states.
    """
    b = Button(text=label, font_size=font_size, bold=True,
               background_normal="", background_down="",
               background_color=(0, 0, 0, 0), color=fg, **kwargs)
    _paint_bg(b, bg, radius=radius)
    return b


def _dark_entry(hint, font_size, radius=14, **kwargs):
    """TextInput in theme colors with a rounded background."""
    t = TextInput(hint_text=hint, multiline=False, font_size=font_size,
                  background_normal="", background_active="",
                  background_color=(0, 0, 0, 0),
                  foreground_color=_FG, hint_text_color=_DIM,
                  cursor_color=_ACCENT, padding=[16, 12], **kwargs)
    _paint_bg(t, (0.10, 0.10, 0.14, 1), radius=radius)
    return t


def _paste_into(entry, status=None) -> bool:
    """Fill a TextInput from the system clipboard; True when pasted.

    Long-press/Gboard paste is unreliable in Kivy's SDL text input
    (worse on password fields), so token fields get a real button.
    """
    try:
        from kivy.core.clipboard import Clipboard
        text = (Clipboard.paste() or "").strip()
    except Exception:
        text = ""
    if text:
        try:
            entry.text = text
        except Exception:
            return False
        if status is not None:
            try:
                status.text = ""
            except Exception:
                pass
        return True
    if status is not None:
        try:
            status.text = "Clipboard is empty — copy the token first."
        except Exception:
            pass
    return False


class FastScrollRail(Widget):
    """Floating slim thumb: overlays content, hides after 3s idle.

    Hold-and-pull fast scrolling. The rail floats over the list's right
    edge (no layout space taken) and only appears while scrolling: any
    scroll_y change or rail touch shows it and reschedules the 3s hide.
    While hidden it lets all touches pass through to the rows beneath.
    """

    def __init__(self, scrollview, width=44, **kwargs):
        super().__init__(size_hint_x=None, size_hint_y=1.0, width=width,
                         pos_hint={"right": 1, "y": 0}, **kwargs)
        self.sv = scrollview
        self._shown = False
        self.opacity = 0
        self._hide_ev = None
        with self.canvas:
            Color(0.27, 0.28, 0.35, 0.55)
            self._track = RoundedRectangle(pos=self.pos, size=self.size,
                                           radius=[4])
            Color(*_ACCENT)
            self._thumb = RoundedRectangle(pos=self.pos, size=(12, 100),
                                           radius=[6])
        self.bind(pos=lambda *_a: self._redraw(),
                  size=lambda *_a: self._redraw())
        try:
            scrollview.bind(scroll_y=lambda *_a: self._on_scroll())
            for child in list(scrollview.children):
                try:
                    child.bind(minimum_height=lambda *_a: self._redraw())
                    child.bind(height=lambda *_a: self._redraw())
                except Exception:
                    pass
        except Exception:
            pass
        self._redraw()
        self.bind(pos=lambda *_a: self._redraw(),
                  size=lambda *_a: self._redraw())
        try:
            scrollview.bind(scroll_y=lambda *_a: self._redraw())
            for child in list(scrollview.children):
                try:
                    child.bind(minimum_height=lambda *_a: self._redraw())
                    child.bind(height=lambda *_a: self._redraw())
                except Exception:
                    pass
        except Exception:
            pass
        self._redraw()

    def _redraw(self, *_args) -> None:
        try:
            box = self.sv
            content = 1.0
            if box.children:
                try:
                    content = max(1.0, float(box.children[0].height or 0))
                except Exception:
                    pass
            viewport = max(1.0, float(box.height or 0))
            frac = min(1.0, viewport / max(content, viewport))
            try:
                sy = min(1.0, max(0.0, float(box.scroll_y)))
            except Exception:
                sy = 1.0
            H = float(self.height or 0)
            if frac >= 1.0 or H <= 0:
                self._track.size = (0, 0)
                self._thumb.size = (0, 0)
                return
            self._track.pos = (self.x + (self.width - 8) / 2, self.y)
            self._track.size = (8, H)
            th = max(80.0, frac * H)
            self._thumb.pos = (self.x + (self.width - 12) / 2,
                               self.y + sy * (H - th))
            self._thumb.size = (12, th)
        except Exception:
            pass

    def _on_scroll(self, *_args) -> None:
        self._redraw()
        self._poke()

    def _poke(self) -> None:
        """Show the rail and reschedule the 3s auto-hide."""
        try:
            if not self._shown:
                self._shown = True
                self.opacity = 1
            if self._hide_ev is not None:
                try:
                    self._hide_ev.cancel()
                except Exception:
                    pass
            self._hide_ev = Clock.schedule_once(self._hide, 3)
        except Exception:
            pass

    def _hide(self, _dt) -> None:
        try:
            self._shown = False
            self.opacity = 0
        except Exception:
            pass
        finally:
            self._hide_ev = None

    def on_touch_down(self, touch):
        try:
            if self._shown and self.collide_point(*touch.pos):
                touch.grab(self)
                self._poke()
                self._drag_to(touch.y)
                return True
        except Exception:
            pass
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if touch.grab_current is self:
            try:
                self._drag_to(touch.y)
            except Exception:
                pass
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            try:
                touch.ungrab(self)
            except Exception:
                pass
            return True
        return super().on_touch_up(touch)

    def _drag_to(self, y: float) -> None:
        try:
            H = float(self.height or 0)
            if H <= 0:
                return
            rel = (float(y) - float(self.y)) / H
            self.sv.scroll_y = min(1.0, max(0.0, rel))
        except Exception:
            pass


class StatusBanner(BoxLayout):
    """Bottom banner: message + optional Undo, auto-hides."""

    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=0, **kwargs)
        self._label = Label(text="", halign="left", valign="middle",
                            font_size=_U.get("banner_font", 30), color=_FG)
        self._label.bind(size=self._label.setter("text_size"))
        self._undo = _flat_button("Undo", _U.get("btn_font", 28),
                                  fg=_ACCENT,
                                  size_hint_x=None,
                                  width=_U.get("refresh_w", 110))
        self._undo.bind(on_release=self._on_undo)
        self._task = None
        self._hide_ev = None
        self.add_widget(self._label)
        self.add_widget(self._undo)
        self._undo.opacity = 0
        self._undo.disabled = True

    def show(self, text, task=None, seconds=5):
        app = App.get_running_app()
        try:
            self._label.font_size = _U.get("banner_font", 30)
            self._undo.font_size = _U.get("btn_font", 28)
        except Exception:
            pass
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
        _paint_bg(box, _BG)
        box.add_widget(Label(text="Todoist Pomodoro", bold=True, color=_FG,
                             font_size=_U.get("setup_title", 40),
                             size_hint_y=None, height=_U.get("setup_title", 40) * 2))
        box.add_widget(Label(
            text="Paste your Todoist personal API token.\nTodoist > Settings > Integrations > Developer.",
            color=_DIM,
            font_size=_U.get("setup_entry_font", 34),
            halign="center"))
        token_row = BoxLayout(orientation="horizontal", spacing=10,
                                size_hint_y=None,
                                height=_U.get("setup_entry_h", 64))
        self._entry = _dark_entry("Todoist API token",
                                  _U.get("setup_entry_font", 34))
        self._entry.password = True
        token_row.add_widget(self._entry)
        paste_btn = _flat_button("Paste", _U.get("setup_entry_font", 34),
                                 bg=_ACCENT, fg=_DARK_TEXT,
                                 size_hint_x=None, width=180)
        paste_btn.bind(on_release=lambda _b: _paste_into(self._entry,
                                                         self._status))
        token_row.add_widget(paste_btn)
        box.add_widget(token_row)
        self._status = Label(text="", halign="center", color=_DIM)
        box.add_widget(self._status)
        box.add_widget(Widget(size_hint_y=1))
        go = _flat_button("Connect", _U.get("setup_btn_font", 34),
                          bg=_ACCENT, fg=_DARK_TEXT,
                          size_hint_y=None, height=_U.get("setup_btn_h", 72))
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
        menu = Button(text="···", font_size=_U.get("menu_font", 28),
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
        self._refresh_btn = _flat_button("Refresh", _U.get("btn_font", 28),
                                         bg=_ACCENT, fg=_DARK_TEXT,
                                         size_hint_x=None,
                                         width=_U.get("refresh_w", 120))
        self._refresh_btn.bind(on_release=lambda _b: self.refresh())
        top.add_widget(self._refresh_btn)
        root.add_widget(top)

        nav = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("nav_h", 72), spacing=10)
        self._nav_buttons: list = []
        for label, screen in (("Timer", "timer"), ("Stats", "stats"), ("Settings", "settings")):
            b = _flat_button(label, _U.get("nav_font", 30), fg=_ACCENT)
            b.bind(on_release=lambda _b, s=screen: self._go(s))
            self._nav_buttons.append(b)
            nav.add_widget(b)
        root.add_widget(nav)

        self._status = Label(text="Loading…", bold=True, size_hint_y=None,
                             height=_U.get("status_h", 56),
                             font_size=_U.get("status_font", 34),
                             color=_DIM, halign="left", valign="middle")
        self._status.bind(size=self._status.setter("text_size"))
        root.add_widget(self._status)
        list_wrap = FloatLayout()
        scroll = ScrollView(bar_width=8, bar_color=(0, 0, 0, 0),
                            bar_inactive_color=(0, 0, 0, 0),
                            scroll_type=["content"])
        self._list = GridLayout(cols=1, size_hint_y=None, spacing=8)
        self._list.bind(minimum_height=self._list.setter("height"))
        scroll.add_widget(self._list)
        list_wrap.add_widget(scroll)
        self._rail = FastScrollRail(scroll)
        list_wrap.add_widget(self._rail)
        root.add_widget(list_wrap)

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
            self._refresh_btn.font_size = _U.get("btn_font", 28)
            for b in self._nav_buttons:
                b.font_size = _U.get("nav_font", 30)
            self._status.font_size = _U.get("status_font", 34)
            self._add_entry.font_size = _U.get("add_font", 20)
            self._add_btn.font_size = _U.get("add_font", 20)
        except Exception:
            pass
        try:
            self._apply_filter()
        except Exception:
            pass

    def _go(self, screen):
        # Timer opens even with no task bound (shows "No task", Mark-done
        # stays disabled until a task is picked).
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


# -- focus-todo night theme (procedural backdrop, no image assets) ---------
_NIGHT_STOPS = (
    (0.00, (0.03, 0.04, 0.13, 1)),
    (0.36, (0.10, 0.10, 0.28, 1)),
    (0.55, (0.24, 0.21, 0.44, 1)),
    (0.60, (0.09, 0.08, 0.21, 1)),
    (0.66, (0.06, 0.06, 0.15, 1)),
    (1.00, (0.015, 0.015, 0.045, 1)),
)
_CARD_BG = (0.10, 0.10, 0.15, 0.94)
_STAT_RED = (0.98, 0.55, 0.55, 1)
_WHITE = (1, 1, 1, 1)
_NAV_IDLE = (0.60, 0.62, 0.70, 1)


def _night_color(p):
    """Sky/lake gradient color at fraction p from the top (0..1)."""
    stops = _NIGHT_STOPS
    if p <= stops[0][0]:
        return stops[0][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p <= p1:
            t = (p - p0) / max(1e-6, p1 - p0)
            return (c0[0] + (c1[0] - c0[0]) * t,
                    c0[1] + (c1[1] - c0[1]) * t,
                    c0[2] + (c1[2] - c0[2]) * t, 1)
    return stops[-1][1]


class NightBackdrop(Widget):
    """Procedural starry-night lake + mountain scene.

    Painted once per resize from a fixed seed, so the layout is stable
    and no image asset needs shipping.
    """

    BANDS = 28
    STARS = 150

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=lambda *_a: self._redraw(),
                  size=lambda *_a: self._redraw())
        self._redraw()

    def _redraw(self, *_args):
        try:
            x, y = float(self.x), float(self.y)
            w, h = float(self.width), float(self.height)
            if w <= 0 or h <= 0:
                return
            self.canvas.clear()
            from kivy.graphics import (Color, Ellipse, Line, Point,
                                       Rectangle, Triangle)
            with self.canvas:
                for i in range(self.BANDS):
                    p0, p1 = i / self.BANDS, (i + 1) / self.BANDS
                    Color(*_night_color((p0 + p1) / 2))
                    y_top = y + h * (1 - p0)
                    y_bot = y + h * (1 - p1)
                    Rectangle(pos=(x, y_bot), size=(w, y_top - y_bot))
                rng = random.Random(11)
                buckets: dict = {}
                for _ in range(self.STARS):
                    sx = x + rng.random() * w
                    sy = y + h * (0.44 + rng.random() * 0.54)
                    key = (round(0.2 + rng.random() * 0.65, 1),
                           round(1 + rng.random() * 1.6, 1))
                    buckets.setdefault(key, []).extend([sx, sy])
                for (alpha, size), pts in buckets.items():
                    Color(1, 1, 1, alpha)
                    Point(points=pts, pointsize=size)
                Color(0.90, 0.92, 1, 0.10)
                Ellipse(pos=(x + w * 0.78 - 60, y + h * 0.70 - 60),
                        size=(120, 120))
                Color(0.95, 0.96, 1, 0.9)
                Ellipse(pos=(x + w * 0.78 - 8, y + h * 0.70 - 8),
                        size=(16, 16))
                horizon = y + h * 0.40
                self._ridge(x, w, h, horizon, (0.09, 0.09, 0.19, 1),
                            seed_phase=0.0, fuji=True)
                self._ridge(x, w, h, horizon - h * 0.015,
                            (0.03, 0.03, 0.07, 1),
                            seed_phase=100.0, fuji=False)
                for k in range(8):
                    ly = horizon - h * (0.03 + k * 0.028)
                    lw = w * (0.10 + rng.random() * 0.30)
                    lx = (x + (w - lw) / 2
                          + (rng.random() - 0.5) * w * 0.3)
                    Color(0.55, 0.58, 0.85, 0.10)
                    Line(points=[lx, ly, lx + lw, ly], width=1.2)
        except Exception:
            pass

    def _ridge(self, x, w, h, base, color, seed_phase, fuji):
        """Filled ridge silhouette with a central Fuji-like cone."""
        from kivy.graphics import Color, Triangle
        n_seg = 26
        fuji_cx = x + w * 0.5
        fuji_hw = w * 0.17
        fuji_ht = h * (0.115 if fuji else 0.0)
        peaks = []
        for i in range(n_seg + 1):
            px = x + w * i / n_seg
            n = h * (0.015 + 0.035 * abs(
                math.sin(i * 1.7 + seed_phase) * 0.6
                + math.sin(i * 0.6 + seed_phase * 2) * 0.4))
            if fuji:
                cone = fuji_ht * max(0.0, 1 - abs(px - fuji_cx) / fuji_hw)
                n = max(n, cone)
            peaks.append((px, base + n))
        Color(*color)
        skirt = base - h * 0.02
        for i in range(n_seg):
            x0, y0 = peaks[i]
            x1, y1 = peaks[i + 1]
            Triangle(points=[x0, skirt, x1, skirt, x1, y1])
            Triangle(points=[x0, skirt, x1, y1, x0, y0])


class TickRing(Widget):
    """120-tick progress ring; ticks light up with elapsed fraction."""

    progress = NumericProperty(0.0)
    ticks = 120

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=lambda *_a: self._redraw(),
                  size=lambda *_a: self._redraw(),
                  progress=lambda *_a: self._redraw())
        self._redraw()

    def _redraw(self, *_args):
        try:
            from kivy.graphics import Color, Line
            w, h = float(self.width), float(self.height)
            if w <= 0 or h <= 0:
                return
            self.canvas.clear()
            cx, cy = float(self.x) + w / 2, float(self.y) + h / 2
            radius = min(w, h) / 2 - 12
            if radius <= 0:
                return
            done = min(1.0, max(0.0, float(self.progress)))
            with self.canvas:
                for i in range(self.ticks):
                    ang = -math.pi / 2 + 2 * math.pi * i / self.ticks
                    if (i / self.ticks) < done:
                        Color(1, 1, 1, 0.92)
                    else:
                        Color(1, 1, 1, 0.16)
                    c, s = math.cos(ang), math.sin(ang)
                    Line(points=[cx + (radius - 11) * c,
                                 cy + (radius - 11) * s,
                                 cx + radius * c, cy + radius * s],
                         width=3)
        except Exception:
            pass


class CircleCheck(ButtonBehavior, Widget):
    """Ring checkbox: tap completes the bound task."""

    def __init__(self, diameter=60, **kwargs):
        kwargs.setdefault("size_hint", (None, None))
        super().__init__(**kwargs)
        self.size = (diameter, diameter)
        self.bind(pos=lambda *_a: self._redraw(),
                  size=lambda *_a: self._redraw())
        self._redraw()

    def _redraw(self, *_args):
        try:
            from kivy.graphics import Color, Line
            d = min(float(self.width), float(self.height))
            if d <= 0:
                return
            self.canvas.clear()
            cx = float(self.x) + float(self.width) / 2
            cy = float(self.y) + float(self.height) / 2
            with self.canvas:
                Color(0.72, 0.74, 0.84, 1)
                Line(circle=(cx, cy, d / 2 - 3), width=3)
        except Exception:
            pass


class TimerScreen(Screen):
    _FG = (0.90, 0.91, 0.97, 1)
    _DIM = (0.65, 0.68, 0.78, 1)
    _ACCENT = (0.54, 0.71, 0.98, 1)
    _GREEN = (0.65, 0.89, 0.63, 1)

    def __init__(self, **kwargs):
        super().__init__(name="timer", **kwargs)
        self._tick_ev = None
        self._remaining = 25 * 60
        self._total = 25 * 60
        self._mode = "focus"
        self._run_start = None
        self._elapsed_base = 0
        self._pill_fn = self._start
        self._pill_key = ""
        root = FloatLayout()
        root.add_widget(NightBackdrop())
        content = BoxLayout(orientation="vertical", padding=(20, 14),
                            spacing=8)
        card = BoxLayout(orientation="horizontal", size_hint_y=None,
                         height=_U.get("card_h", 112), spacing=12,
                         padding=(16, 10))
        _paint_bg(card, _CARD_BG, radius=24)
        self._check = CircleCheck(diameter=_U.get("check_d", 60))
        self._check.bind(on_release=lambda *_a: self._complete_bound())
        card.add_widget(self._check)
        mid = BoxLayout(orientation="vertical", spacing=2)
        self._task_title = Label(
            text="No task", bold=True,
            font_size=_U.get("timer_task_font", 34), color=_WHITE,
            halign="left", valign="middle")
        self._task_title.bind(size=self._task_title.setter("text_size"))
        mid.add_widget(self._task_title)
        self._task_stats = Label(
            text="", font_size=_U.get("timer_prog_font", 22),
            color=_STAT_RED, halign="left", valign="middle")
        self._task_stats.bind(size=self._task_stats.setter("text_size"))
        mid.add_widget(self._task_stats)
        card.add_widget(mid)
        close = Button(
            text="X", font_size=_U.get("timer_back_font", 26), bold=True,
            size_hint_x=None, width=64, background_normal="",
            background_down="", background_color=(0, 0, 0, 0), color=_DIM)
        close.bind(on_release=lambda _b: setattr(self.manager, "current",
                                                 "today"))
        card.add_widget(close)
        content.add_widget(card)
        content.add_widget(Widget(size_hint_y=0.10))
        zone = FloatLayout(size_hint_y=1.35)
        self._ring = TickRing()
        zone.add_widget(self._ring)
        self._clock = Label(
            text="25:00", bold=True, font_size=_U.get("clock_font", 170),
            color=_WHITE, halign="center", valign="middle", shorten=False)
        self._clock.bind(size=self._clock.setter("text_size"))
        zone.add_widget(self._clock)
        content.add_widget(zone)
        self._progress = Label(
            text="", font_size=_U.get("timer_prog_font", 22),
            color=(1, 1, 1, 0.55), halign="center",
            size_hint_y=None, height=30)
        content.add_widget(self._progress)
        self._state = Label(
            text="Ready", font_size=_U.get("timer_state_font", 24),
            color=(1, 1, 1, 0.65), halign="center",
            size_hint_y=None, height=30)
        content.add_widget(self._state)
        pill_wrap = AnchorLayout(anchor_x="center", anchor_y="center",
                                size_hint_y=None,
                                height=_U.get("pill_h", 96))
        self._pill = _flat_button(
            "Start", _U.get("pill_font", 32), bg=_WHITE, fg=_DARK_TEXT,
            radius=60, size_hint=(0.72, None),
            height=_U.get("pill_h", 96))
        self._pill.bind(on_release=lambda _b: self._pill_action())
        pill_wrap.add_widget(self._pill)
        content.add_widget(pill_wrap)
        sec = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=56, spacing=0)
        sec.add_widget(Widget())
        self._reset_link = Button(
            text="Reset", font_size=_U.get("ghost_font", 26),
            size_hint_x=None, width=180, background_normal="",
            background_down="", background_color=(0, 0, 0, 0),
            color=_DIM)
        self._reset_link.bind(on_release=lambda _b: self._reset())
        sec.add_widget(self._reset_link)
        self._done_link = Button(
            text="Task done", font_size=_U.get("ghost_font", 26),
            size_hint_x=None, width=220, background_normal="",
            background_down="", background_color=(0, 0, 0, 0),
            color=self._GREEN)
        self._done_link.bind(on_release=lambda _b: self._mark_done())
        sec.add_widget(self._done_link)
        sec.add_widget(Widget())
        content.add_widget(sec)
        self.banner = StatusBanner()
        content.add_widget(self.banner)
        nav = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("nav_h", 72) + 8, spacing=4)
        self._nav_btns: dict = {}
        for label, screen in (("Today", "today"), ("Timer", "timer"),
                              ("Stats", "stats"), ("Settings", "settings")):
            active = (label == "Timer")
            b = Button(
                text=label, font_size=_U.get("nav_font", 30),
                bold=active, background_normal="", background_down="",
                background_color=(0, 0, 0, 0),
                color=_WHITE if active else _NAV_IDLE)
            b.bind(on_release=lambda _b, s=screen: setattr(self.manager,
                                                           "current", s))
            self._nav_btns[label] = b
            nav.add_widget(b)
        content.add_widget(nav)
        root.add_widget(content)
        self.add_widget(root)
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
            self._task_title.font_size = _U.get("timer_task_font", 34)
            self._task_stats.font_size = _U.get("timer_prog_font", 22)
            self._clock.font_size = _U.get("clock_font", 170)
            self._progress.font_size = _U.get("timer_prog_font", 22)
            self._state.font_size = _U.get("timer_state_font", 24)
            self._pill.font_size = _U.get("pill_font", 32)
            self._pill.height = _U.get("pill_h", 96)
            self._reset_link.font_size = _U.get("ghost_font", 26)
            self._done_link.font_size = _U.get("ghost_font", 26)
            for b in self._nav_btns.values():
                b.font_size = _U.get("nav_font", 30)
        except Exception:
            pass

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._update_pill()

    def _pill_spec(self):
        """(label, action) for the single primary pill button."""
        running = self._tick_ev is not None
        if running:
            return ("Pause", self._pause)
        if self._mode == "focus" and self._remaining <= 0:
            return ("Start Break", self._break)
        if self._remaining < self._total or self._elapsed_base > 0:
            return ("Resume", self._start)
        return ("Start", self._start)

    def _update_pill(self) -> None:
        try:
            label, fn = self._pill_spec()
            if getattr(self, "_pill_key", None) != label:
                self._pill.text = label
                self._pill_key = label
            self._pill_fn = fn
        except Exception:
            pass

    def _pill_action(self) -> None:
        try:
            fn = getattr(self, "_pill_fn", None) or self._start
        except Exception:
            fn = self._start
        fn()

    def _complete_bound(self) -> None:
        try:
            task = App.get_running_app().timer_task
        except Exception:
            task = None
        if task is not None:
            App.get_running_app().complete_task(task)

    def _refresh_task_card(self) -> None:
        """Task title + per-task focus stats (hours / finished sessions)."""
        try:
            app = App.get_running_app()
            task = app.timer_task
        except Exception:
            return
        try:
            if task is None:
                self._task_title.text = "No task"
                self._task_stats.text = ""
                self._done_link.disabled = True
                self._done_link.opacity = 0.4
                return
            self._task_title.text = _display(task.content, 60)
            tid = str(getattr(task, "id", "") or "")
            total, count = 0, 0
            try:
                for s in app.session_store.read_all():
                    if (str(getattr(s, "task_id", "") or "") == tid
                            and getattr(s, "finished", False)):
                        total += int(getattr(s, "duration_sec", 0) or 0)
                        count += 1
            except Exception:
                pass
            if count:
                self._task_stats.text = f"{total / 3600:.1f} / {count}"
            else:
                self._task_stats.text = "No focus yet"
            self._done_link.disabled = False
            self._done_link.opacity = 1
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
        self._refresh_task_card()
        self._apply_sizes()
        self._render()

    def _render(self):
        self._clock.text = _fmt_clock(self._remaining)
        try:
            total = max(1, int(self._total))
            done = max(0, total - max(0, int(self._remaining)))
            pct = int(round(100.0 * done / total))
            self._progress.text = f"{_fmt_clock(done)} of {_fmt_clock(total)} · {pct}%"
            self._ring.progress = done / total
        except Exception:
            pass
        self._update_pill()

    def _start(self):
        if self._tick_ev is not None:
            return
        if self._remaining <= 0:
            self._remaining = self._total
        self._run_start = time.monotonic()
        self._tick_ev = Clock.schedule_interval(self._tick, 1)
        self._state.text = "Focusing…"
        self._update_pill()

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
        self._update_pill()

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
        self._refresh_task_card()
        self._update_pill()

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
        self._refresh_task_card()
        self._update_pill()

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
        self._update_pill()
        return True


class StatsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="stats", **kwargs)
        root = BoxLayout(orientation="vertical", padding=16, spacing=10)
        _paint_bg(root, _BG)
        top = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=_U.get("top_h", 72), spacing=10)
        back = _flat_button("< Today", _U.get("btn_font", 28),
                            size_hint_x=None,
                            width=max(_U.get("title_w", 120),
                                      _U.get("btn_font", 28) * 4.6))
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        top.add_widget(back)
        top.add_widget(Label(text="Stats", bold=True, font_size=_U.get("title_font", 30), color=_FG, halign="left"))
        refresh = _flat_button("Refresh", _U.get("btn_font", 28),
                               size_hint_x=None, width=_U.get("refresh_w", 110))
        refresh.bind(on_release=lambda _b: self.refresh())
        top.add_widget(refresh)
        root.add_widget(top)
        stats_wrap = FloatLayout()
        scroll = ScrollView(bar_width=8, bar_color=(0, 0, 0, 0),
                            bar_inactive_color=(0, 0, 0, 0),
                            scroll_type=["content"])
        self._stats_list = GridLayout(cols=1, size_hint_y=None, spacing=10)
        self._stats_list.bind(minimum_height=self._stats_list.setter("height"))
        scroll.add_widget(self._stats_list)
        stats_wrap.add_widget(scroll)
        stats_wrap.add_widget(FastScrollRail(scroll))
        root.add_widget(stats_wrap)
        self.add_widget(root)

    def on_pre_enter(self):
        self.refresh()

    def _stat_card(self, value: str, caption: str, color) -> BoxLayout:
        card = BoxLayout(orientation="vertical", padding=12, spacing=4)
        _paint_bg(card, _CARD, radius=18)
        num = Label(text=value, bold=True,
                    font_size=_U.get("stat_hero", 87), color=color,
                    halign="center", valign="middle", size_hint_y=0.68)
        num.bind(size=num.setter("text_size"))
        cap = Label(text=caption, font_size=_U.get("stat_cap", 34),
                    color=_DIM, halign="center", valign="middle",
                    size_hint_y=0.32)
        cap.bind(size=cap.setter("text_size"))
        card.add_widget(num)
        card.add_widget(cap)
        return card

    def _stat_row(self, name: str, value: str) -> BoxLayout:
        row = BoxLayout(orientation="horizontal", spacing=8, padding=(16, 8),
                        size_hint_y=None, height=_U.get("stat_row", 140))
        _paint_bg(row, _CARD, radius=18)
        left = Label(text=_display(name, 40), bold=True,
                     font_size=_U.get("stat_cap", 34), color=_FG,
                     halign="left", valign="middle")
        left.bind(size=left.setter("text_size"))
        left.size_hint_x = 0.65
        right = Label(text=value, bold=True,
                      font_size=_U.get("stat_cap", 34), color=_ACCENT,
                      halign="right", valign="middle")
        right.bind(size=right.setter("text_size"))
        right.size_hint_x = 0.35
        row.add_widget(left)
        row.add_widget(right)
        return row

    def _stat_section(self, title: str, mapping: dict, limit: int) -> None:
        header = Label(text=title, bold=True,
                       font_size=_U.get("title_font", 40), color=_FG,
                       halign="left", valign="middle",
                       size_hint_y=None, height=_U.get("top_h", 112))
        header.bind(size=header.setter("text_size"))
        self._stats_list.add_widget(header)
        top = sorted(mapping.items(), key=lambda kv: -kv[1])[:limit]
        if not top:
            self._stats_list.add_widget(Label(text="Nothing yet.", color=_DIM,
                                              font_size=_U.get("stat_cap", 34),
                                              size_hint_y=None, height=60))
            return
        for name, sec in top:
            self._stats_list.add_widget(
                self._stat_row(str(name), format_duration(sec)))

    def refresh(self):
        try:
            data = App.get_running_app().session_store.stats()
        except Exception as exc:
            self._stats_list.clear_widgets()
            self._stats_list.add_widget(Label(text=f"Failed: {exc}", color=_RED))
            return
        self._stats_list.clear_widgets()
        hero = BoxLayout(orientation="horizontal", spacing=10,
                         size_hint_y=None,
                         height=_U.get("stat_hero", 87) + 110)
        hero.add_widget(self._stat_card(format_duration(data["today_sec"]),
                                        "Today", _ACCENT))
        hero.add_widget(self._stat_card(format_duration(data["week_sec"]),
                                        "7 days", _GREEN))
        hero.add_widget(self._stat_card(str(data["streak"]),
                                        "Streak", _FG))
        self._stats_list.add_widget(hero)
        self._stat_section("By project", data["by_project"] or {}, 5)
        self._stat_section("By task", data["by_task"] or {}, 20)


class SettingsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="settings", **kwargs)
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        _paint_bg(root, _BG)
        back = _flat_button("< Today", _U.get("timer_back_font", 26),
                            size_hint_y=None, height=_U.get("timer_back_h", 64))
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        root.add_widget(back)
        root.add_widget(Label(text="Settings", bold=True,
                              font_size=_U.get("title_font", 30), color=_FG,
                              size_hint_y=None, height=_U.get("top_h", 72)))
        root.add_widget(Label(text="Focus length:", color=_FG,
                              font_size=_U.get("setup_entry_font", 34),
                              size_hint_y=None, height=_U.get("setup_entry_font", 34) * 2))
        self._focus_val = 25
        self._focus_btn = _flat_button("", _U.get("setup_entry_font", 34),
                                       size_hint_y=None,
                                       height=_U.get("setup_entry_h", 64))
        self._focus_btn.bind(on_release=lambda _b: self._open_number_popup(
            "Focus length", self._focus_val, 1, 180, 5, self._set_focus))
        root.add_widget(self._focus_btn)
        root.add_widget(Label(text="Break length:", color=_FG,
                              font_size=_U.get("setup_entry_font", 34),
                              size_hint_y=None, height=_U.get("setup_entry_font", 34) * 2))
        self._break_val = 5
        self._break_btn = _flat_button("", _U.get("setup_entry_font", 34),
                                       size_hint_y=None,
                                       height=_U.get("setup_entry_h", 64))
        self._break_btn.bind(on_release=lambda _b: self._open_number_popup(
            "Break length", self._break_val, 1, 60, 5, self._set_break))
        root.add_widget(self._break_btn)
        root.add_widget(Label(text="Todoist API token:", color=_FG,
                              font_size=_U.get("setup_entry_font", 34),
                              size_hint_y=None, height=_U.get("setup_entry_font", 34) * 2))
        self._token = _dark_entry("", _U.get("setup_entry_font", 34))
        self._token.password = True
        token_row = BoxLayout(orientation="horizontal", spacing=10,
                              size_hint_y=None,
                              height=_U.get("setup_entry_h", 64))
        token_row.add_widget(self._token)
        paste_btn = _flat_button("Paste", _U.get("setup_entry_font", 34),
                                 bg=_ACCENT, fg=_DARK_TEXT,
                                 size_hint_x=None, width=180)
        paste_btn.bind(on_release=lambda _b: _paste_into(self._token,
                                                         self._status))
        token_row.add_widget(paste_btn)
        root.add_widget(token_row)
        self._status = Label(text="", color=_DIM,
                             font_size=_U.get("setup_entry_font", 34))
        root.add_widget(self._status)
        root.add_widget(Widget(size_hint_y=1))
        save = _flat_button("Save", _U.get("setup_btn_font", 34),
                            bg=_ACCENT, fg=_DARK_TEXT,
                            size_hint_y=None, height=_U.get("setup_btn_h", 72))
        save.bind(on_release=lambda _b: self._save())
        root.add_widget(save)
        self.add_widget(root)

    def on_pre_enter(self):
        app = App.get_running_app()
        self._focus_val = app.settings.focus_min
        self._break_val = app.settings.short_break_min
        self._focus_btn.text = f"{self._focus_val} min"
        self._break_btn.text = f"{self._break_val} min"
        self._token.text = app.token or ""

    def _set_focus(self, value: int) -> None:
        self._focus_val = value
        self._focus_btn.text = f"{value} min"

    def _set_break(self, value: int) -> None:
        self._break_val = value
        self._break_btn.text = f"{value} min"

    def _open_number_popup(self, title: str, current: int, minimum: int,
                           maximum: int, step: int, on_pick) -> None:
        """Stepper popup: big value with - / + controls, then Done."""
        layout = BoxLayout(orientation="vertical", spacing=12, padding=20)
        _paint_bg(layout, _BG)
        state = {"v": min(maximum, max(minimum, int(current)))}
        big = Label(text=str(state["v"]), bold=True,
                    font_size=_U.get("stat_hero", 87), color=_FG,
                    halign="center", valign="middle")
        big.bind(size=big.setter("text_size"))
        layout.add_widget(big)
        unit = Label(text="minutes", font_size=_U.get("stat_cap", 34),
                     color=_DIM, halign="center", size_hint_y=None, height=48)
        layout.add_widget(unit)

        def _adj(delta: int) -> None:
            state["v"] = min(maximum, max(minimum, state["v"] + delta))
            big.text = str(state["v"])

        row = BoxLayout(orientation="horizontal", spacing=12,
                        size_hint_y=None, height=_U.get("setup_btn_h", 156))
        minus = _flat_button("-", _U.get("setup_btn_font", 41))
        minus.bind(on_release=lambda _b: _adj(-step))
        row.add_widget(minus)
        plus = _flat_button("+", _U.get("setup_btn_font", 41))
        plus.bind(on_release=lambda _b: _adj(step))
        row.add_widget(plus)
        layout.add_widget(row)
        popup = Popup(title=title, title_size=28, title_color=_FG,
                      separator_color=_ACCENT, content=layout,
                      size_hint=(0.85, None),
                      height=max(560.0, _U.get("stat_hero", 87) + 380))
        done = _flat_button("Done", _U.get("setup_btn_font", 41),
                            bg=_ACCENT, fg=_DARK_TEXT,
                            size_hint_y=None, height=_U.get("setup_btn_h", 156))
        done.bind(on_release=lambda _b: (popup.dismiss(), on_pick(state["v"])))
        layout.add_widget(done)
        popup.open()

    def _save(self):
        app = App.get_running_app()
        focus = min(180, max(1, int(self._focus_val)))
        brk = min(60, max(1, int(self._break_val)))
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
        layout = BoxLayout(orientation="vertical", spacing=10, padding=16)
        _paint_bg(layout, _BG)
        popup = Popup(title=_display(task.content, 40), title_size=28,
                      title_color=_FG, separator_color=_ACCENT,
                      content=layout,
                      size_hint=(0.9, None), height=_U.get("popup_menu_h", 480))
        start = _flat_button("Start timer", _U.get("popup_btn_font", 30),
                             bg=_ACCENT, fg=_DARK_TEXT,
                             size_hint_y=None, height=_U.get("popup_btn_h", 68))
        start.bind(on_release=lambda _b: (popup.dismiss(), self.open_timer(task)))
        layout.add_widget(start)
        done = _flat_button("Complete", _U.get("popup_btn_font", 30),
                            size_hint_y=None, height=_U.get("popup_btn_h", 68))
        done.bind(on_release=lambda _b: (popup.dismiss(), self.complete_task(task)))
        layout.add_widget(done)
        for label, due in (("Tomorrow", "tomorrow"), ("Next week", "next week")):
            b = _flat_button(f"Reschedule: {label}", _U.get("popup_btn_font", 30), size_hint_y=None, height=_U.get("popup_btn_h", 68))
            b.bind(on_release=lambda _b, d=due: (popup.dismiss(), self.reschedule_task(task, d)))
            layout.add_widget(b)
        custom = _flat_button("Change date…", _U.get("popup_btn_font", 30), size_hint_y=None, height=_U.get("popup_btn_h", 68))
        custom.bind(on_release=lambda _b: (popup.dismiss(), self.open_custom_date(task)))
        layout.add_widget(custom)
        popup.open()

    def open_custom_date(self, task):
        layout = BoxLayout(orientation="vertical", spacing=10, padding=16)
        _paint_bg(layout, _BG)
        entry = _dark_entry("e.g. tomorrow, Mon 2pm",
                            _U.get("setup_entry_font", 34),
                            size_hint_y=None, height=72)
        layout.add_widget(entry)
        popup = Popup(title="Change date", title_size=28, title_color=_FG,
                      separator_color=_ACCENT, content=layout,
                      size_hint=(0.9, None), height=_U.get("popup_date_h", 340))
        entry.bind(on_text_validate=lambda _i: (popup.dismiss(), self.reschedule_task(task, entry.text.strip())))
        go = _flat_button("Save", _U.get("setup_btn_font", 34),
                          bg=_ACCENT, fg=_DARK_TEXT,
                          size_hint_y=None, height=_U.get("setup_btn_h", 72))
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
