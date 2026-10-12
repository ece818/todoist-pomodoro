# Todoist Pomodoro

A Pomodoro focus timer connected to Todoist. Pick a task, run the clock, mark it done — without opening Todoist.

Two builds from one backend:

- **Linux desktop** (Python + GTK4/libadwaita) — the full app.
- **Android** (Kivy, `android/`) — Today, timer, stats, settings; same sync logic.

## Features

- Two-way Todoist sync: complete in-app and it completes in Todoist; add tasks with natural language ("Call mom Mon 2pm").
- Timer end and task-done are separate events — the clock never auto-completes anything.
- Fullscreen countdown, end sound, focus/break durations in Settings.
- Ctrl+P floating palette: search every task, start timer, complete, reschedule, change date.
- Right-click menu on any task: start timer, complete, reschedule presets, custom date (calendar + time + free text).
- Checkbox completes a task with an Undo banner (5s); the row drops and Undo brings it back.
- Completing a task stops its running timer and logs worked minutes as a partial session.
- Stats page: today / 7-day totals, per-project and per-task breakdowns, day streak.
- Saved Todoist filters, selectable locally. Projects (Personal vs Work) visible and filterable.
- `timer` command runs it from any directory.

## Linux desktop — run it

Requirements: Python 3, GTK4 + libadwaita + PyGObject, `requests`.

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adwaita-1
pip install requests
git clone <repo-url> todoist-pomodoro && cd todoist-pomodoro
echo 'TODOIST_API_TOKEN=<your-token>' > .env
chmod 600 .env
python3 main.py
```

Token: Todoist → Settings → Integrations → Developer. Lookup order:
`TODOIST_API_TOKEN`, then `TODOIST_API_KEY` (`.env` first, environment fallback).

Optional launcher (`timer` from anywhere):

```bash
ln -s "$PWD/main.py" ~/.local/bin/timer   # ~/.local/bin on your PATH
```

Tests: `python3 -m pytest tests/ -q`.

## Android — APK

`android/` holds a Kivy port that reuses the desktop backend unchanged
(`config`, `todoist`, `sync`, `sessions`, `actions` — keep them in sync with
`sh android/sync_backend.sh` after backend changes).

Build it yourself:

```bash
pip install buildozer
cd android
export JAVA_HOME=<jdk-17-path>
buildozer android debug
# → bin/pomodoro-0.1-debug.apk, sideload onto the phone
```

On first launch, paste the Todoist token when asked. arm64, targets API 34.

## Layout

| Path | What |
|---|---|
| `main.py` | Desktop entry: Today → Timer wiring, banner + Undo, shortcuts |
| `ui_today.py`, `ui_palette.py`, `ui_timer.py` | Task list, Ctrl+P palette, countdown view |
| `ui_stats.py`, `ui_settings.py`, `ui_common.py` | Stats, durations dialog, shared dialogs |
| `todoist.py` | Todoist API v1 client (quick-add, close, reopen, reschedule) |
| `sync.py` | SyncStore + FilterStore, offline pending-add queue |
| `sessions.py` | Session log, stats rollups, streak |
| `actions.py` | Single command core: complete / reopen / reschedule / quick-add |
| `android/` | Kivy port + buildozer spec (see above) |
| `web/` | Browser build (stdlib server + SPA): same backend, all functions |
| `docs/designs/` | Approved v1 + v2 design docs |

## Website — run it

Stdlib only, no new dependencies. Same backend as the desktop app
(Today, timer + session log, stats, settings, palette + saved filters,
projects, quick-add, complete/Undo, reschedule, pending retry).

```bash
python3 web/server.py --port 8000
# → open http://localhost:8000
```

The timer never auto-completes — Mark-done is explicit, same as desktop.

## Notes

- `.env` (your token) is gitignored — never commit it.
- No auto-complete on timer end, by design. Done means done only when you say so.
