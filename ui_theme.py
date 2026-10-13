"""Shared night-sky theme: one CssProvider for all desktop pages."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk

FOCUS_CSS: bytes = b"""
.focus-page { background-color: #070a18; }
.focus-row { background-color: rgba(22,24,40,0.92); border-radius: 16px; margin: 6px 4px; }
.focus-row:selected { background-color: rgba(60,70,110,0.95); }
.focus-row-label { font-size: 18px; font-weight: 700; color: #ffffff; }
.focus-meta { font-size: 14px; color: rgba(255,255,255,0.6); }
.focus-value { font-size: 17px; font-weight: 700; color: #8ab4f8; }
.focus-entry { font-size: 16px; padding: 10px 14px; border-radius: 12px; }
.focus-hero { font-size: 42px; font-weight: 800; }
.focus-check { min-width: 44px; min-height: 44px; }
button.focus-big { min-height: 48px; font-size: 17px; font-weight: 700; padding: 8px 22px; }
list, viewport { background-color: transparent; }
.focus-card { background: rgba(18, 18, 28, 0.88); border-radius: 18px; }
.focus-pill { background: #ffffff; color: #14141e; border-radius: 999px; font-size: 20px; font-weight: 800; padding: 10px 44px; }
.focus-ghost { color: rgba(255,255,255,0.7); }
.focus-nav { color: rgba(255,255,255,0.55); font-weight: 600; }
.focus-nav-active { color: #ffffff; font-weight: 800; }
.focus-check { border-radius: 999px; border: 2px solid rgba(255,255,255,0.75); min-width: 44px; min-height: 44px; background: transparent; color: transparent; }
"""

_theme_loaded = False


def apply_focus_theme() -> None:
    """Install FOCUS_CSS once; silently skip when headless/unavailable."""
    global _theme_loaded
    if _theme_loaded:
        return
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(FOCUS_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _theme_loaded = True
    except Exception:
        pass
