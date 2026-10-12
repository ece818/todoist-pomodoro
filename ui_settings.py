"""Settings dialog: focus / short-break durations."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk


class SettingsDialog(Gtk.Dialog):
    """Modal dialog with two duration spinners.

    :param on_save: callback ``fn(focus_min, short_break_min)`` fired
        when the user presses Save. The caller persists + applies.
    """

    def __init__(
        self,
        focus_min: int = 25,
        short_break_min: int = 5,
        on_save: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        super().__init__(title="Settings")
        self.set_modal(True)
        self.set_default_size(320, 180)
        self._on_save = on_save

        area = self.get_content_area()
        area.set_margin_top(16)
        area.set_margin_bottom(16)
        area.set_margin_start(16)
        area.set_margin_end(16)
        area.set_spacing(12)

        self._focus_spin = self._add_row(
            area, "Focus (minutes)", focus_min, 1, 180
        )
        self._break_spin = self._add_row(
            area, "Short break (minutes)", short_break_min, 1, 60
        )

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.connect("response", self._on_response)

    @staticmethod
    def _add_row(
        area: Gtk.Box, label_text: str, value: int, lo: int, hi: int
    ) -> Gtk.SpinButton:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        label.set_hexpand(True)
        row.append(label)
        spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
        spin.set_value(max(lo, min(hi, int(value or lo))))
        spin.set_numeric(True)
        row.append(spin)
        area.append(row)
        return spin

    def _on_response(self, _dialog: Gtk.Dialog, response: int) -> None:
        if response == Gtk.ResponseType.OK and self._on_save is not None:
            try:
                self._on_save(
                    int(self._focus_spin.get_value()),
                    int(self._break_spin.get_value()),
                )
            except Exception:
                pass
        self.close()
