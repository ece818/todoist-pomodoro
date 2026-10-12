"""Local sync store: pending-add queue + in-flight close guard."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from todoist import (
    TodoistAuthError,
    TodoistError,
    TodoistNetworkError,
    TodoistRateLimited,
    Task,
)

if TYPE_CHECKING:
    from todoist import TodoistClient


class FilterStore:
    """LOCAL named Todoist filter queries (no server list endpoint)."""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)

    def _load(self) -> list[dict[str, str]]:
        try:
            if not self.state_path.exists():
                return []
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        items: list[dict[str, str]] = []
        try:
            if isinstance(data, dict):
                raw = data.get("filters", [])
            elif isinstance(data, list):
                raw = data
            else:
                return []
            if not isinstance(raw, list):
                return []
            for entry in raw:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and isinstance(entry.get("query"), str)
                ):
                    items.append({"name": entry["name"], "query": entry["query"]})
        except Exception:
            return []
        return items

    def _save(self, items: list[dict[str, str]]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps({"filters": items}), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def list(self) -> list[dict[str, str]]:
        return self._load()

    def add(self, name: str, query: str) -> None:
        name = (name or "").strip()
        query = (query or "").strip()
        if not name:
            raise ValueError("filter name must not be blank")
        if not query:
            raise ValueError("filter query must not be blank")
        items = [i for i in self._load() if i["name"] != name]
        items.append({"name": name, "query": query})
        self._save(items)

    def remove(self, name: str) -> None:
        items = [i for i in self._load() if i.get("name") != name]
        self._save(items)

    def get(self, name: str) -> str | None:
        for entry in self._load():
            if entry["name"] == name:
                return entry["query"]
        return None


class SyncStore:
    """Local state + failure contract over a TodoistClient."""

    def __init__(self, client: TodoistClient, state_path: str | Path | None = None):
        self.client = client
        self.state_path = Path(state_path) if state_path is not None else None
        # List of (content, request_id) tuples; request_id is stable per add
        # and reused on retry for duplicate protection.
        self.pending_adds: list[tuple[str, str]] = []
        self._closing: set[str] = set()
        self._all_tasks: list[Task] = []
        self.active_filter: str | None = None
        self._projects: dict[str, str] = {}  # project_id -> name

    # -- projects -----------------------------------------------------
    def refresh_projects(self) -> dict[str, str]:
        """Fetch all projects; cache id->name. Propagates client errors."""
        self._projects = {p.id: p.name for p in self.client.list_projects()}
        return self._projects

    def projects(self) -> list[tuple[str, str]]:
        """Cached (id, name) pairs sorted by name; empty when never loaded."""
        return sorted(self._projects.items(), key=lambda kv: kv[1].lower())

    def project_name(self, project_id: str) -> str:
        return self._projects.get(str(project_id or ""), "")

    def _refresh_projects_quietly(self) -> None:
        """Project names are enrichment: never break the main flow."""
        try:
            self.refresh_projects()
        except Exception:
            pass

    # -- read ---------------------------------------------------------
    def refresh_today(self) -> list[Task]:
        tasks = self.client.list_today()
        self._refresh_projects_quietly()
        return tasks

    def refresh_all(self) -> list[Task]:
        tasks = self.client.list_all()
        self._all_tasks = list(tasks)
        self._refresh_projects_quietly()
        return self._all_tasks

    def set_filter(self, query: str | None) -> None:
        if query is None:
            self.active_filter = None
        else:
            stripped = query.strip()
            self.active_filter = stripped if stripped else None
        self._all_tasks = []

    def candidates(self) -> list[Task]:
        if self.active_filter:
            tasks = self.client.tasks_by_filter(self.active_filter)
        else:
            tasks = self.refresh_all()
        self._refresh_projects_quietly()
        return tasks

    def search(self, query: str) -> list[Task]:
        if not query or not query.strip():
            return []
        if self.active_filter:
            pool = self.candidates()
        else:
            if not self._all_tasks:
                # Propagate any client exceptions to the caller.
                self.refresh_all()
            else:
                self._refresh_projects_quietly()
            pool = self._all_tasks
        needle = query.strip().lower()
        matched = [
            t
            for t in pool
            if needle in t.content.lower()
            or needle in self.project_name(t.project_id).lower()
        ]
        dated = [t for t in matched if t.due_date is not None]
        dateless = [t for t in matched if t.due_date is None]
        dated.sort(key=lambda t: (t.due_date, -t.priority))  # type: ignore[union-attr]
        dateless.sort(key=lambda t: -t.priority)
        return dated + dateless

    # -- add ----------------------------------------------------------
    def add(self, content: str) -> Task:
        request_id = uuid.uuid4().hex
        try:
            task = self.client.add_task(content, request_id=request_id)
        except (TodoistNetworkError, TodoistRateLimited):
            self.pending_adds.append((content, request_id))
            self._persist_quietly()
            raise
        # Defensive: drop any stale entry with this request_id on success.
        self.pending_adds = [
            (c, r) for (c, r) in self.pending_adds if r != request_id
        ]
        return task

    def retry_pending(self) -> dict[str, int]:
        added = 0
        queue = list(self.pending_adds)
        for content, request_id in queue:
            try:
                self.client.add_task(content, request_id=request_id)
            except TodoistAuthError:
                raise
            except (TodoistNetworkError, TodoistRateLimited):
                continue
            except Exception:
                # Non-transient API errors: keep queued, move on.
                continue
            else:
                self.pending_adds = [
                    (c, r)
                    for (c, r) in self.pending_adds
                    if not (c == content and r == request_id)
                ]
                added += 1
        self._persist_quietly()
        return {"added": added, "still_pending": len(self.pending_adds)}

    # -- close --------------------------------------------------------
    def close(self, task: Task) -> None:
        task_id = str(task.id)
        if task_id in self._closing:
            raise RuntimeError(f"Close already in-flight for task {task_id}")
        self._closing.add(task_id)
        try:
            # Propagate TodoistAuthError without retry.
            self.client.close_task(task_id)
            # Reconcile: re-fetch and confirm the id is gone before returning.
            # A recurring task legitimately reappears as its next occurrence.
            try:
                current = self.client.list_today()
            except Exception:
                current = None
            if current is not None:
                still_there = any(str(t.id) == task_id for t in current)
                if still_there and not task.is_recurring:
                    raise TodoistError(
                        f"Task {task_id} still present after close"
                    )
            return None
        finally:
            self._closing.discard(task_id)

    # -- persistence --------------------------------------------------
    def _persist_quietly(self) -> None:
        """Best-effort save; persistence must never break the core flow."""
        try:
            self.save_state()
        except Exception:
            pass

    def save_state(self) -> None:
        if self.state_path is None:
            return
        data = {
            "pending_adds": [
                {"content": c, "request_id": r} for (c, r) in self.pending_adds
            ]
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    def load_state(self) -> None:
        if self.state_path is None:
            return
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        items = data.get("pending_adds", []) if isinstance(data, dict) else []
        restored: list[tuple[str, str]] = []
        for entry in items:
            if isinstance(entry, dict) and "content" in entry and "request_id" in entry:
                restored.append((str(entry["content"]), str(entry["request_id"])))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                restored.append((str(entry[0]), str(entry[1])))
        self.pending_adds = restored
