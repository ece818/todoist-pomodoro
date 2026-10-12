"""Stdlib-only web server: same backend as the desktop app, browser UI.

Run:  python3 web/server.py [--port 8000]
Then open http://localhost:8000

Reuses config / todoist / sync / sessions / actions unchanged.
No new dependencies (stdlib http.server + existing `requests` backend).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    load_config,
    load_timer_settings,
    save_timer_settings,
    save_user_token,
)
from sessions import SessionStore  # noqa: E402
from sync import FilterStore, SyncStore  # noqa: E402
from todoist import Task, TodoistAuthError, TodoistClient, TodoistError  # noqa: E402

try:
    from actions import TaskActions  # noqa: E402
except ImportError:  # pragma: no cover
    TaskActions = None  # type: ignore[assignment]

STATIC_DIR = APP_DIR / "static"


def _task_to_dict(task: Task, store: SyncStore) -> dict:
    try:
        project = store.project_name(str(getattr(task, "project_id", "") or ""))
    except Exception:
        project = ""
    due = getattr(task, "due_date", None)
    return {
        "id": str(getattr(task, "id", "")),
        "content": str(getattr(task, "content", "") or ""),
        "description": str(getattr(task, "description", "") or ""),
        "due_date": due.isoformat() if due is not None else None,
        "priority": int(getattr(task, "priority", 1) or 1),
        "is_recurring": bool(getattr(task, "is_recurring", False)),
        "project_id": str(getattr(task, "project_id", "") or ""),
        "project_name": project,
    }


class State:
    """Shared backend state (mirrors main.py wiring)."""

    def __init__(self) -> None:
        app_env = ROOT / ".env"
        self.config = load_config(env_path=app_env, allow_missing_token=True)
        base = Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        ) / "todoist-pomodoro"
        self.client = TodoistClient(self.config.token, timeout=10)
        self.store = SyncStore(self.client, state_path=base / "pending.json")
        self.store.load_state()
        self.filters = FilterStore(base / "filters.json")
        self.sessions = SessionStore(base / "sessions.jsonl")
        self.settings_path = base / "settings.json"
        self.settings = load_timer_settings(
            self.settings_path,
            focus_default=self.config.focus_min,
            break_default=self.config.short_break_min,
        )
        self.lock = threading.Lock()
        if TaskActions is not None:
            self.actions = TaskActions(self.store, on_start_timer=None)
        else:
            self.actions = None

    # -- helpers ------------------------------------------------------

    def token_status(self) -> str:
        try:
            current = str(getattr(getattr(self.store, "client", None), "token", "") or "")
        except Exception:
            current = ""
        return f"Set (…{current[-4:]})" if current else "Not set"

    def find_task(self, task_id: str) -> Task:
        """Locate a Task by id from cache, else refresh, else stub (close only needs id)."""
        tid = str(task_id or "")
        for pool in (getattr(self.store, "_all_tasks", []) or [],):
            for task in pool:
                if str(getattr(task, "id", "")) == tid:
                    return task
        try:
            tasks = self.store.refresh_all()
            for task in tasks:
                if str(getattr(task, "id", "")) == tid:
                    return task
        except Exception:
            pass
        return Task(id=tid, content="")


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    state: State  # set by serve()

    # -- plumbing -----------------------------------------------------

    def log_message(self, *args):  # quieter than default
        sys.stderr.write("%s - %s\n" % (self.address_string(), args[0] % args[1:]))

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 500) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_static(self, rel: str) -> None:
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self.send_error(403)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            self.send_error(404)
            return
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing ------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        st = self.state

        if path == "/" or not path.startswith("/api/"):
            if path == "/":
                return self._serve_static("index.html")
            return self._serve_static(path.lstrip("/"))

        try:
            if path == "/api/today":
                with st.lock:
                    tasks = st.store.refresh_today()
                    out = [_task_to_dict(t, st.store) for t in tasks]
                return self._send_json({"ok": True, "tasks": out})
            if path == "/api/all":
                with st.lock:
                    tasks = st.store.refresh_all()
                    out = [_task_to_dict(t, st.store) for t in tasks]
                return self._send_json({"ok": True, "tasks": out})
            if path == "/api/candidates":
                with st.lock:
                    tasks = st.store.candidates()
                    out = [_task_to_dict(t, st.store) for t in tasks]
                    active = st.store.active_filter
                return self._send_json(
                    {"ok": True, "tasks": out, "active_filter": active}
                )
            if path == "/api/projects":
                with st.lock:
                    try:
                        st.store.refresh_projects()
                    except Exception:
                        pass
                    pairs = st.store.projects()
                return self._send_json(
                    {"ok": True, "projects": [{"id": pid, "name": n} for pid, n in pairs]}
                )
            if path == "/api/search":
                q = (query.get("q", [""])[0] or "")
                with st.lock:
                    tasks = st.store.search(q)
                    out = [_task_to_dict(t, st.store) for t in tasks]
                return self._send_json({"ok": True, "tasks": out})
            if path == "/api/stats":
                with st.lock:
                    data = st.sessions.stats()
                return self._send_json({"ok": True, "stats": data})
            if path == "/api/settings":
                with st.lock:
                    s = st.settings
                    status = st.token_status()
                    has = bool(getattr(getattr(st.store, "client", None), "token", ""))
                return self._send_json(
                    {
                        "ok": True,
                        "focus_min": s.focus_min,
                        "short_break_min": s.short_break_min,
                        "token_status": status,
                        "has_token": has,
                    }
                )
            if path == "/api/filters":
                with st.lock:
                    items = st.filters.list()
                return self._send_json({"ok": True, "filters": items})
            if path == "/api/pending":
                with st.lock:
                    n = len(st.store.pending_adds)
                return self._send_json({"ok": True, "pending": n})
            if path == "/api/health":
                return self._send_json({"ok": True})
        except TodoistAuthError:
            return self._send_error_json(
                "Unauthorized — check your Todoist token.", status=401
            )
        except TodoistError as exc:
            return self._send_error_json(str(exc), status=502)
        except Exception as exc:  # defensive: API never crashes the socket
            return self._send_error_json(str(exc), status=500)
        return self.send_error(404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        st = self.state
        body = self._read_json()

        try:
            if path == "/api/add":
                text = str(body.get("text", "") or "").strip()
                if not text:
                    return self._send_error_json("text must not be blank", 400)
                with st.lock:
                    if st.actions is not None and hasattr(st.actions, "quick_add"):
                        task = st.actions.quick_add(text)
                    else:
                        task = st.store.quick_add(text)
                    out = _task_to_dict(task, st.store)
                return self._send_json({"ok": True, "task": out})

            if path == "/api/complete":
                task_id = str(body.get("id", "") or "")
                if not task_id:
                    return self._send_error_json("id required", 400)
                with st.lock:
                    task = st.find_task(task_id)
                    st.store.close(task)
                return self._send_json({"ok": True, "id": task_id})

            if path == "/api/reopen":  # Undo a completion
                task_id = str(body.get("id", "") or "")
                if not task_id:
                    return self._send_error_json("id required", 400)
                with st.lock:
                    task = st.find_task(task_id)
                    st.store.reopen(task)
                return self._send_json({"ok": True, "id": task_id})

            if path == "/api/reschedule":
                task_id = str(body.get("id", "") or "")
                due = str(body.get("due_string", "") or "").strip()
                if not task_id or not due:
                    return self._send_error_json("id + due_string required", 400)
                with st.lock:
                    task = st.find_task(task_id)
                    if st.actions is not None:
                        updated = st.actions.reschedule(task, due)
                    else:
                        updated = st.store.reschedule(task, due)
                    out = _task_to_dict(updated, st.store)
                return self._send_json({"ok": True, "task": out})

            if path == "/api/session":  # timer worked-minutes log (>=60s client-side)
                with st.lock:
                    session = st.sessions.log(
                        task_id=str(body.get("task_id", "") or ""),
                        content=str(body.get("content", "") or ""),
                        project_id=str(body.get("project_id", "") or ""),
                        project_name=str(body.get("project_name", "") or ""),
                        duration_sec=int(body.get("duration_sec", 0) or 0),
                        finished=bool(body.get("finished", False)),
                        started_at=body.get("started_at"),
                    )
                return self._send_json({"ok": True, "logged_sec": session.duration_sec})

            if path == "/api/settings":
                focus = int(body.get("focus_min", st.settings.focus_min))
                brk = int(body.get("short_break_min", st.settings.short_break_min))
                token = str(body.get("token", "") or "").strip()
                with st.lock:
                    from config import TimerSettings

                    st.settings = TimerSettings(
                        focus_min=max(1, focus), short_break_min=max(1, brk)
                    )
                    try:
                        save_timer_settings(st.settings_path, st.settings)
                    except OSError as exc:
                        return self._send_error_json(f"Could not save: {exc}", 500)
                    if token:
                        try:
                            TodoistClient(token, timeout=15).list_projects()
                        except TodoistAuthError:
                            return self._send_error_json(
                                "Token invalid — check and try again.", 401
                            )
                        except Exception as exc:
                            return self._send_error_json(
                                f"Could not verify token: {exc}", 502
                            )
                        try:
                            save_user_token(token)
                        except Exception as exc:
                            return self._send_error_json(
                                f"Could not save token: {exc}", 500
                            )
                        st.store.client = TodoistClient(token, timeout=10)
                return self._send_json(
                    {
                        "ok": True,
                        "focus_min": st.settings.focus_min,
                        "short_break_min": st.settings.short_break_min,
                        "token_status": st.token_status(),
                    }
                )

            if path == "/api/filters":
                name = str(body.get("name", "") or "").strip()
                fquery = str(body.get("query", "") or "").strip()
                if not name or not fquery:
                    return self._send_error_json("name + query required", 400)
                with st.lock:
                    st.filters.add(name, fquery)
                return self._send_json({"ok": True})

            if path == "/api/filters/delete":
                name = str(body.get("name", "") or "")
                with st.lock:
                    st.filters.remove(name)
                return self._send_json({"ok": True})

            if path == "/api/filter/select":  # set/clear active saved-filter query
                fquery = body.get("query")
                with st.lock:
                    st.store.set_filter(fquery if isinstance(fquery, str) else None)
                    active = st.store.active_filter
                return self._send_json({"ok": True, "active_filter": active})

            if path == "/api/retry":
                with st.lock:
                    result = st.store.retry_pending()
                return self._send_json({"ok": True, **result})

        except TodoistAuthError:
            return self._send_error_json(
                "Unauthorized — check your Todoist token.", status=401
            )
        except (TodoistError, ValueError, RuntimeError) as exc:
            # ValueError: blank quick-add / reschedule input (client error).
            code = 400 if isinstance(exc, (ValueError, RuntimeError)) else 502
            return self._send_error_json(str(exc), status=code)
        except Exception as exc:
            return self._send_error_json(str(exc), status=500)
        return self.send_error(404)

    # DELETE maps to filters/delete for simplicity behind some proxies.
    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/filters":
            body = self._read_json()
            with self.state.lock:
                self.state.filters.remove(str(body.get("name", "") or ""))
            return self._send_json({"ok": True})
        return self.send_error(404)


def serve(port: int) -> ThreadingHTTPServer:
    state = State()
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Todoist Pomodoro web: http://localhost:{port}  (Ctrl+C to stop)")
    if not getattr(getattr(state.store, "client", None), "token", ""):
        print("No Todoist token yet — open Settings in the page to connect.")
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Todoist Pomodoro website (stdlib).")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = serve(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
