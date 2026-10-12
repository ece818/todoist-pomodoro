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


class StatusBanner(BoxLayout):
    """Bottom banner: message + optional Undo, auto-hides."""

    def __init__(self, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=0, **kwargs)
        self._label = Label(text="", halign="left", valign="middle")
        self._label.bind(size=self._label.setter("text_size"))
        self._undo = Button(text="Undo", size_hint_x=None, width=110)
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
        self.height = 56
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
        box.add_widget(Label(text="Todoist Pomodoro", font_size=28, size_hint_y=None, height=60))
        box.add_widget(Label(
            text="Paste your Todoist personal API token.\nTodoist → Settings → Integrations → Developer.",
            halign="center"))
        self._entry = TextInput(hint_text="Todoist API token", multiline=False,
                                password=True, size_hint_y=None, height=48)
        box.add_widget(self._entry)
        self._status = Label(text="", halign="center")
        box.add_widget(self._status)
        go = Button(text="Connect", size_hint_y=None, height=56)
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


class TaskRow(BoxLayout):
    def __init__(self, task, meta, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=56,
                         spacing=8, padding=(8, 4), **kwargs)
        self.task = task
        check = CheckBox(size_hint_x=None, width=48)
        check.bind(active=self._on_check)
        self.add_widget(check)
        mid = BoxLayout(orientation="vertical")
        name = Label(text=task.content, halign="left", valign="bottom",
                     shorten=True, shorten_from="right")
        name.bind(size=name.setter("text_size"))
        mid.add_widget(name)
        if meta:
            sub = Label(text=meta, font_size=12, color=(0.55, 0.55, 0.55, 1),
                        halign="left", valign="top", shorten=True)
            sub.bind(size=sub.setter("text_size"))
            mid.add_widget(sub)
        self.add_widget(mid)
        menu = Button(text="···", size_hint_x=None, width=56)
        menu.bind(on_release=lambda _b: App.get_running_app().open_task_menu(task))
        self.add_widget(menu)
        self.bind(on_touch_down=self._on_touch)

    def _on_touch(self, _w, touch):
        if self.collide_point(*touch.pos) and touch.is_double_tap:
            App.get_running_app().open_timer(self.task)
            return True
        return False

    def _on_check(self, _box, active):
        if active:
            App.get_running_app().complete_task(self.task)


class TodayScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(name="today", **kwargs)
        root = BoxLayout(orientation="vertical", padding=12, spacing=8)
        top = BoxLayout(orientation="horizontal", size_hint_y=None, height=48, spacing=8)
        top.add_widget(Label(text="Today", font_size=24, size_hint_x=None, width=100,
                             halign="left"))
        self._filter = TextInput(hint_text="Filter tasks…", multiline=False)
        self._filter.bind(text=lambda _i, _v: self._apply_filter())
        top.add_widget(self._filter)
        refresh = Button(text="Refresh", size_hint_x=None, width=110)
        refresh.bind(on_release=lambda _b: self.refresh())
        top.add_widget(refresh)
        root.add_widget(top)

        nav = BoxLayout(orientation="horizontal", size_hint_y=None, height=44, spacing=8)
        for label, screen in (("Timer", "timer"), ("Stats", "stats"), ("Settings", "settings")):
            b = Button(text=label)
            b.bind(on_release=lambda _b, s=screen: self._go(s))
            nav.add_widget(b)
        root.add_widget(nav)

        self._status = Label(text="Loading…", size_hint_y=None, height=24, font_size=13)
        root.add_widget(self._status)
        scroll = ScrollView()
        self._list = GridLayout(cols=1, size_hint_y=None, spacing=2)
        self._list.bind(minimum_height=self._list.setter("height"))
        scroll.add_widget(self._list)
        root.add_widget(scroll)

        add_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=52, spacing=8)
        self._add_entry = TextInput(hint_text="Try 'Call mom Mon 2pm…'", multiline=False)
        self._add_entry.bind(on_text_validate=lambda _i: self._on_add())
        add_row.add_widget(self._add_entry)
        add_btn = Button(text="Add", size_hint_x=None, width=100)
        add_btn.bind(on_release=lambda _b: self._on_add())
        add_row.add_widget(add_btn)
        root.add_widget(add_row)

        self.banner = StatusBanner()
        root.add_widget(self.banner)
        self.add_widget(root)
        self._tasks = []

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
            self._list.add_widget(TaskRow(t, self._metas.get(str(t.id), "")))

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
    def __init__(self, **kwargs):
        super().__init__(name="timer", **kwargs)
        root = BoxLayout(orientation="vertical", padding=24, spacing=12)
        back = Button(text="← Today", size_hint_y=None, height=44)
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        root.add_widget(back)
        self._task_label = Label(text="No task", font_size=22, halign="center")
        root.add_widget(self._task_label)
        self._clock = Label(text="25:00", font_size=84, halign="center")
        root.add_widget(self._clock)
        self._state = Label(text="Ready", font_size=15, color=(0.55, 0.55, 0.55, 1))
        root.add_widget(self._state)
        row1 = BoxLayout(orientation="horizontal", size_hint_y=None, height=52, spacing=8)
        for label, fn in (("Start", self._start), ("Pause", self._pause),
                          ("Reset", self._reset), ("Break", self._break)):
            b = Button(text=label)
            b.bind(on_release=lambda _b, f=fn: f())
            row1.add_widget(b)
        root.add_widget(row1)
        done = Button(text="Mark task done", size_hint_y=None, height=52)
        done.bind(on_release=lambda _b: self._mark_done())
        root.add_widget(done)
        self.banner = StatusBanner()
        root.add_widget(self.banner)
        self.add_widget(root)
        self._tick_ev = None
        self._remaining = 25 * 60
        self._total = 25 * 60
        self._mode = "focus"
        self._run_start = None
        self._elapsed_base = 0

    def on_pre_enter(self):
        app = App.get_running_app()
        s = app.settings
        if self._tick_ev is None:
            self._total = s.focus_min * 60
            self._remaining = self._total
        task = app.timer_task
        self._task_label.text = task.content if task is not None else "No task"
        self._render()

    def _render(self):
        self._clock.text = _fmt_clock(self._remaining)

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
        self._mode = "break"
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
        self._mode = "focus"
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
        top = BoxLayout(orientation="horizontal", size_hint_y=None, height=48, spacing=8)
        back = Button(text="← Today", size_hint_x=None, width=120)
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        top.add_widget(back)
        top.add_widget(Label(text="Stats", font_size=24, halign="left"))
        refresh = Button(text="Refresh", size_hint_x=None, width=110)
        refresh.bind(on_release=lambda _b: self.refresh())
        top.add_widget(refresh)
        root.add_widget(top)
        scroll = ScrollView()
        self._body = Label(text="", halign="left", valign="top", size_hint_y=None)
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
        back = Button(text="← Today", size_hint_y=None, height=44)
        back.bind(on_release=lambda _b: setattr(self.manager, "current", "today"))
        root.add_widget(back)
        root.add_widget(Label(text="Settings", font_size=24, size_hint_y=None, height=40))
        root.add_widget(Label(text="Focus minutes:", size_hint_y=None, height=24))
        self._focus = TextInput(multiline=False, input_filter="int", size_hint_y=None, height=48)
        root.add_widget(self._focus)
        root.add_widget(Label(text="Break minutes:", size_hint_y=None, height=24))
        self._break = TextInput(multiline=False, input_filter="int", size_hint_y=None, height=48)
        root.add_widget(self._break)
        root.add_widget(Label(text="Todoist API token:", size_hint_y=None, height=24))
        self._token = TextInput(multiline=False, password=True, size_hint_y=None, height=48)
        root.add_widget(self._token)
        self._status = Label(text="")
        root.add_widget(self._status)
        save = Button(text="Save", size_hint_y=None, height=52)
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
        popup = Popup(title=task.content[:40], content=layout,
                      size_hint=(0.85, None), height=380)
        start = Button(text="Start timer", size_hint_y=None, height=52)
        start.bind(on_release=lambda _b: (popup.dismiss(), self.open_timer(task)))
        layout.add_widget(start)
        done = Button(text="Complete", size_hint_y=None, height=52)
        done.bind(on_release=lambda _b: (popup.dismiss(), self.complete_task(task)))
        layout.add_widget(done)
        for label, due in (("Tomorrow", "tomorrow"), ("Next week", "next week")):
            b = Button(text=f"Reschedule: {label}", size_hint_y=None, height=52)
            b.bind(on_release=lambda _b, d=due: (popup.dismiss(), self.reschedule_task(task, d)))
            layout.add_widget(b)
        custom = Button(text="Change date…", size_hint_y=None, height=52)
        custom.bind(on_release=lambda _b: (popup.dismiss(), self.open_custom_date(task)))
        layout.add_widget(custom)
        popup.open()

    def open_custom_date(self, task):
        layout = BoxLayout(orientation="vertical", spacing=8, padding=12)
        entry = TextInput(hint_text="e.g. tomorrow, Mon 2pm", multiline=False,
                          size_hint_y=None, height=48)
        layout.add_widget(entry)
        popup = Popup(title="Change date", content=layout, size_hint=(0.9, None), height=260)
        entry.bind(on_text_validate=lambda _i: (popup.dismiss(), self.reschedule_task(task, entry.text.strip())))
        go = Button(text="Save", size_hint_y=None, height=52)
        go.bind(on_release=lambda _b: (popup.dismiss(), self.reschedule_task(task, entry.text.strip())))
        layout.add_widget(go)
        popup.open()

    def complete_task(self, task):
        content = task.content or "Task"
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
