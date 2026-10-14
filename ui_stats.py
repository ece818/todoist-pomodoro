"""Stats page: today / week totals, streak, top projects and tasks."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from sessions import SessionStore, format_duration


def _top(items: list[tuple[str, int]], limit: int = 5) -> list[tuple[str, int]]:
    return sorted(items, key=lambda kv: kv[1], reverse=True)[:limit]


class StatsPanel(Gtk.Box):
    """Read-only rollup over the local session log.

    :param session_store: SessionStore backing the stats.
    :param project_name: optional ``fn(project_id) -> str`` used to
        resolve names for ids the sessions file never recorded.
    """

    def __init__(
        self,
        session_store: SessionStore,
        project_name: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("focus-page")
        self._sessions = session_store
        self._project_name = project_name

        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Stats")
        title.add_css_class("title-1")
        title.set_xalign(0.0)
        title.set_hexpand(True)
        toolbar.append(title)
        refresh_button = Gtk.Button(label="Refresh")
        refresh_button.set_tooltip_text("Reload stats from session log")
        refresh_button.add_css_class("suggested-action")
        refresh_button.add_css_class("focus-big")
        refresh_button.connect("clicked", self._on_refresh_clicked)
        toolbar.append(refresh_button)
        self.append(toolbar)

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hero.set_homogeneous(True)
        self._today_value = self._hero_card(hero, "Today", "focus-hero-blue")
        self._week_value = self._hero_card(hero, "7 days", "focus-hero-green")
        self._streak_value = self._hero_card(hero, "Streak", "focus-hero-white")
        self.append(hero)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scrolled)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.set_child(self._list)

        self.refresh()

    # -- public ------------------------------------------------------

    def refresh(self) -> None:
        try:
            data = self._sessions.stats()
        except Exception:
            for label in (
                self._today_value,
                self._week_value,
                self._streak_value,
            ):
                label.set_text("–")
            return
        self._today_value.set_text(format_duration(data["today_sec"]))
        self._week_value.set_text(format_duration(data["week_sec"]))
        self._streak_value.set_text(str(data["streak"]))
        self._rebuild(data)

    # -- internals ---------------------------------------------------

    @staticmethod
    def _hero_card(parent: Gtk.Box, caption: str, value_class: str) -> Gtk.Label:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.add_css_class("focus-card")
        card.set_margin_top(12)
        card.set_margin_bottom(12)
        card.set_margin_start(12)
        card.set_margin_end(12)
        value = Gtk.Label(label="–")
        value.add_css_class(value_class)
        value.set_xalign(0.5)
        caption_label = Gtk.Label(label=caption)
        caption_label.add_css_class("focus-meta")
        caption_label.set_xalign(0.5)
        card.append(value)
        card.append(caption_label)
        parent.append(card)
        return value

    # -- internals ---------------------------------------------------

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self.refresh()

    def _rebuild(self, data: dict) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        self._append_section("By project", self._resolve_projects(data))
        self._append_section("By task", list(data["by_task"].items()))

    def _resolve_projects(self, data: dict) -> list[tuple[str, int]]:
        resolved: dict[str, int] = {}
        for key, seconds in data["by_project"].items():
            name = key
            if key.startswith("id:") and self._project_name is not None:
                try:
                    known = self._project_name(key[3:]) or ""
                except Exception:
                    known = ""
                name = known or key
            resolved[name] = resolved.get(name, 0) + seconds
        return list(resolved.items())

    def _append_section(self, heading: str, items: list[tuple[str, int]]) -> None:
        header = Gtk.ListBoxRow()
        header.set_activatable(False)
        header.set_selectable(False)
        label = Gtk.Label(label=heading)
        label.add_css_class("title-3")
        label.set_xalign(0.0)
        label.set_margin_top(8)
        header.set_child(label)
        self._list.append(header)
        ranked = _top(items)
        if not ranked:
            self._append_line("Nothing yet.", "")
            return
        for name, seconds in ranked:
            self._append_line(name, format_duration(seconds))

    def _append_line(self, name: str, value: str) -> None:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.add_css_class("focus-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(8)
        box.set_margin_end(8)
        name_label = Gtk.Label(label=name)
        name_label.add_css_class("focus-row-label")
        name_label.set_xalign(0.0)
        name_label.set_hexpand(True)
        name_label.set_wrap(True)
        box.append(name_label)
        value_label = Gtk.Label(label=value)
        value_label.add_css_class("dim-label")
        value_label.add_css_class("focus-value")
        box.append(value_label)
        row.set_child(box)
        self._list.append(row)
