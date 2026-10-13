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


@dataclass
class TimerSettings:
    """User-chosen durations, persisted to settings.json (see main.py)."""

    focus_min: int = 25
    short_break_min: int = 5


def _clamp_minutes(value: int, lo: int, hi: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def load_timer_settings(
    path: str | Path, focus_default: int = 25, break_default: int = 5
) -> TimerSettings:
    """Read settings.json; missing/corrupt file yields defaults."""
    import json

    focus_default = _clamp_minutes(focus_default, 1, 180, 25)
    break_default = _clamp_minutes(break_default, 1, 60, 5)
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return TimerSettings(focus_min=focus_default, short_break_min=break_default)
    if not isinstance(data, dict):
        return TimerSettings(focus_min=focus_default, short_break_min=break_default)
    return TimerSettings(
        focus_min=_clamp_minutes(
            data.get("focus_min", focus_default), 1, 180, focus_default
        ),
        short_break_min=_clamp_minutes(
            data.get("short_break_min", break_default), 1, 60, break_default
        ),
    )


def save_timer_settings(path: str | Path, settings: TimerSettings) -> None:
    """Atomic write of settings.json (tmp file + os.replace)."""
    import json
    import os
    import tempfile

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"focus_min": settings.focus_min, "short_break_min": settings.short_break_min}
    )
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def user_token_path() -> Path:
    """In-app token file: $XDG_CONFIG_HOME/todoist-pomodoro/token."""
    import os

    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "todoist-pomodoro" / "token"


def load_user_token() -> str:
    """Token saved via in-app Settings; empty string when never saved."""
    try:
        return _strip_quotes(Path(user_token_path()).read_text(encoding="utf-8").strip())
    except OSError:
        return ""


def save_user_token(token: str) -> Path:
    """Persist an in-app token with owner-only permissions. Returns path."""
    cleaned = _strip_quotes((token or "").strip())
    if not cleaned:
        raise ConfigError("Token must not be blank.")
    path = user_token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(cleaned + "\n")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_config(
    env_path: str | Path = ".env", allow_missing_token: bool = False
) -> Config:
    """Load Config from a .env file (plus environment fallback for token).

    Token lookup order: TODOIST_API_TOKEN then TODOIST_API_KEY, each
    from the .env file, then the environment, then the in-app token
    file saved via Settings (so first-run needs no .env at all).
    With ``allow_missing_token`` an absent token yields ``token=""``
    instead of raising, letting the app open into Settings setup.
    """
    import os

    file_values = _parse_env_file(env_path)

    token = (
        file_values.get("TODOIST_API_TOKEN")
        or os.environ.get("TODOIST_API_TOKEN")
        or file_values.get("TODOIST_API_KEY")
        or os.environ.get("TODOIST_API_KEY")
        or load_user_token()
        or ""
    ).strip()
    # file value already stripped; env value stripped here
    token = _strip_quotes(token)
    if not token and not allow_missing_token:
        raise ConfigError(
            "Missing Todoist API token: set TODOIST_API_TOKEN "
            "(fallback TODOIST_API_KEY) in .env or environment, "
            "or add it in the app's Settings."
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
