"""Shared window wiring: close shortcuts for every app window.

Alt+F4 is normally caught by the window manager, but GTK4 apps with
client-side decorations can swallow it; Ctrl+W is never bound by
default. Binding both explicitly on each window makes close behavior
reliable regardless of desktop environment.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


def add_close_shortcuts(window: Gtk.Window) -> None:
    """Bind Ctrl+W and Alt+F4 to ``window.close()``."""

    def _close(_widget, _args, win: Gtk.Window = window) -> bool:
        win.close()
        return True

    controller = Gtk.ShortcutController()
    controller.set_scope(Gtk.ShortcutScope.GLOBAL)
    for trigger in ("<Control>w", "<Alt>F4"):
        controller.add_shortcut(
            Gtk.Shortcut.new(
                trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                action=Gtk.CallbackAction.new(_close),
            )
        )
    window.add_controller(controller)


def header_with_close(title: str) -> Gtk.HeaderBar:
    """A title bar with the window-manager close button enabled."""
    header = Gtk.HeaderBar()
    title_label = Gtk.Label(label=title)
    header.set_title_widget(title_label)
    header.set_show_title_buttons(True)
    return header
