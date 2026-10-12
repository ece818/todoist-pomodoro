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


def show_reschedule_dialog(parent, on_apply) -> None:
    """Date dialog: calendar pick + optional time, or free-text entry.

    On Apply with free text present, that text is used verbatim (Todoist
    parses "tomorrow", "Mon 2pm", …). Otherwise the calendar date plus
    the optional time field are composed ("YYYY-MM-DD [time]").
    ``on_apply`` receives the due string; blank is impossible by design.
    """
    dlg = Gtk.Dialog(title="Change date…")
    try:
        if isinstance(parent, Gtk.Window):
            dlg.set_transient_for(parent)
    except Exception:
        pass
    dlg.set_modal(True)
    dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dlg.add_button("Apply", Gtk.ResponseType.APPLY)
    dlg.set_default_response(Gtk.ResponseType.APPLY)
    try:
        area = dlg.get_content_area()
    except Exception:
        area = None

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    body.set_margin_top(12)
    body.set_margin_bottom(12)
    body.set_margin_start(12)
    body.set_margin_end(12)

    cal = Gtk.Calendar()
    body.append(cal)

    time_entry = Gtk.Entry()
    time_entry.set_placeholder_text("Time — e.g. 2pm (optional)")
    time_entry.set_activates_default(True)
    body.append(time_entry)

    free_entry = Gtk.Entry()
    free_entry.set_placeholder_text("Or type it — e.g. tomorrow, Mon 2pm")
    free_entry.set_hexpand(True)
    free_entry.set_activates_default(True)
    body.append(free_entry)

    if area is not None:
        area.append(body)
    else:
        dlg.set_child(body)

    def _on_response(d: Gtk.Dialog, resp: int) -> None:
        if resp == Gtk.ResponseType.APPLY:
            free = free_entry.get_text().strip()
            if free:
                on_apply(free)
            else:
                try:
                    moment = cal.get_date()
                    day = (
                        f"{moment.get_year():04d}-{moment.get_month():02d}-"
                        f"{moment.get_day_of_month():02d}"
                    )
                except Exception:
                    day = ""
                picked_time = time_entry.get_text().strip()
                due = f"{day} {picked_time}".strip() if day else picked_time
                if due:
                    on_apply(due)
        try:
            d.close()
        except Exception:
            pass

    dlg.connect("response", _on_response)
    try:
        dlg.present()
    except Exception:
        pass
    try:
        free_entry.grab_focus()
    except Exception:
        pass


def header_with_close(title: str) -> Gtk.HeaderBar:
    """A title bar with the window-manager close button enabled."""
    header = Gtk.HeaderBar()
    title_label = Gtk.Label(label=title)
    header.set_title_widget(title_label)
    header.set_show_title_buttons(True)
    return header
