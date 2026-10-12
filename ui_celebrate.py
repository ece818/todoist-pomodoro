"""Celebration window shown on explicit user task-done action.

This window is NEVER shown automatically at timer end. It appears only
when the user presses an explicit "task done" affordance (e.g. the
"Mark task done" button on TimerView).
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from todoist import TodoistAuthError, TodoistError


class CelebrationWindow(Gtk.Window):
    """Congratulations + single Complete button for ``task``.

    :param task: the Todoist task declared done by the user.
    :param on_complete: callback ``fn(task)`` performing the actual
        close (typically ``store.close``); may raise TodoistError on
        transient failure. Runs in a worker thread.
    """

    def __init__(self, task, on_complete: Callable) -> None:
        super().__init__()
        self.set_title("Done!")
        self.set_default_size(420, 280)
        self._task = task
        self._on_complete = on_complete
        self._settling = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(32)
        box.set_margin_bottom(32)
        box.set_margin_start(32)
        box.set_margin_end(32)

        congrats = Gtk.Label(label="Congratulations!")
        congrats.add_css_class("title-1")
        box.append(congrats)

        content = Gtk.Label(label=getattr(task, "content", ""))
        content.set_wrap(True)
        box.append(content)

        self._error_label = Gtk.Label(label="")
        self._error_label.add_css_class("error")
        self._error_label.set_wrap(True)
        box.append(self._error_label)

        self._complete_button = Gtk.Button(label="Complete")
        self._complete_button.add_css_class("suggested-action")
        self._complete_button.connect("clicked", self._on_complete_clicked)
        box.append(self._complete_button)

        self.set_child(box)

    # -- handlers -----------------------------------------------------

    def _on_complete_clicked(self, _button: Gtk.Button) -> None:
        if self._settling:
            return
        # Disable on first click until the close call settles (re-entrancy
        # guard on the UI side; store.close has its own guard inside).
        self._settling = True
        self._complete_button.set_sensitive(False)
        self._complete_button.set_label("Completing…")
        self._error_label.set_text("")
        threading.Thread(target=self._complete_worker, daemon=True).start()

    # -- workers -------------------------------------------------------

    def _complete_worker(self) -> None:
        try:
            self._on_complete(self._task)
        except TodoistAuthError:
            GLib.idle_add(self._on_auth_error)
        except TodoistError as exc:
            GLib.idle_add(self._on_complete_failed, str(exc))
        except Exception as exc:
            GLib.idle_add(self._on_complete_failed, str(exc))
        else:
            GLib.idle_add(self._on_complete_succeeded)

    # -- idle_add callbacks (UI thread) --------------------------------

    def _on_complete_succeeded(self) -> bool:
        self.close()
        return False

    def _on_complete_failed(self, message: str) -> bool:
        self._settling = False
        self._error_label.set_text(f"Could not complete: {message}")
        self._complete_button.set_label("Retry")
        self._complete_button.set_sensitive(True)
        return False

    def _on_auth_error(self) -> bool:
        # 401: the token is dead, so Retry could never succeed. Say so and
        # leave the button disabled instead of offering a hopeless retry.
        self._error_label.set_text(
            "Token invalid — update .env with a valid Todoist token and restart."
        )
        self._complete_button.set_label("Complete")
        self._complete_button.set_sensitive(False)
        return False
