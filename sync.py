"""Local sync store: pending-add queue + in-flight close guard."""
from __future__ import annotations

import json
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


class SyncStore:
    """Local state + failure contract over a TodoistClient."""

    def __init__(self, client: TodoistClient, state_path: str | Path | None = None):
        self.client = client
        self.state_path = Path(state_path) if state_path is not None else None
        # List of (content, request_id) tuples; request_id is stable per add
        # and reused on retry for duplicate protection.
        self.pending_adds: list[tuple[str, str]] = []
        self._closing: set[str] = set()

    # -- read ---------------------------------------------------------
    def refresh_today(self) -> list[Task]:
        return self.client.list_today()

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
