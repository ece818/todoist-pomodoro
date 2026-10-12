"""Hand-rolled .env config loader (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class Config:
    token: str
    focus_min: int = 25
    short_break_min: int = 5


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _parse_env_file(env_path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path(env_path)
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        # Strip inline comments only when preceded by whitespace? Keep it
        # simple: full-line # already handled; inline '#' is part of value
        # unless we can safely detect. We do NOT strip inline '#' to avoid
        # breaking tokens. Only strip surrounding whitespace/quotes.
        val = _strip_quotes(val.strip())
        values[key] = val
    return values


def _parse_int(values: dict[str, str], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        if key in values and values[key] != "":
            try:
                return int(values[key].strip())
            except ValueError:
                raise ConfigError(f"Invalid integer for {key}: {values[key]!r}")
    return default


def load_config(env_path: str | Path = ".env") -> Config:
    """Load Config from a .env file (plus environment fallback for token).

    Token lookup order: TODOIST_API_TOKEN then TODOIST_API_KEY.
    """
    import os

    file_values = _parse_env_file(env_path)

    token = (
        file_values.get("TODOIST_API_TOKEN")
        or os.environ.get("TODOIST_API_TOKEN")
        or file_values.get("TODOIST_API_KEY")
        or os.environ.get("TODOIST_API_KEY")
        or ""
    ).strip()
    # file value already stripped; env value stripped here
    token = _strip_quotes(token)
    if not token:
        raise ConfigError(
            "Missing Todoist API token: set TODOIST_API_TOKEN "
            "(fallback TODOIST_API_KEY) in .env or environment."
        )

    focus_min = _parse_int(file_values, ("FOCUS_MIN", "FOCUS_MINUTES"), 25)
    short_break_min = _parse_int(
        file_values, ("SHORT_BREAK_MIN", "SHORT_BREAK_MINUTES"), 5
    )
    # Environment overrides file for durations if present.
    # File-level aliases (FOCUS_MINUTES/SHORT_BREAK_MINUTES) are honored here too.
    for keys, attr in (
        (("FOCUS_MIN", "FOCUS_MINUTES"), "focus"),
        (("SHORT_BREAK_MIN", "SHORT_BREAK_MINUTES"), "break"),
    ):
        for key in keys:
            if os.environ.get(key):
                try:
                    val = int(os.environ[key].strip())
                except ValueError:
                    raise ConfigError(
                        f"Invalid integer for {key}: {os.environ[key]!r}"
                    )
                if attr == "focus":
                    focus_min = val
                else:
                    short_break_min = val
                break

    return Config(token=token, focus_min=focus_min, short_break_min=short_break_min)
