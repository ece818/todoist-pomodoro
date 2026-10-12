"""Thin Todoist API v1 client (stdlib + requests)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import requests

BASE_URL = "https://api.todoist.com/api/v1"


class TodoistError(Exception):
    """Base error for Todoist API failures."""


class TodoistAuthError(TodoistError):
    """401: token invalid/expired. Never retry; user must re-token."""


class TodoistRateLimited(TodoistError):
    """429: rate limited. Carries retry_after seconds."""

    def __init__(self, message: str = "Rate limited", retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


class TodoistNetworkError(TodoistError):
    """Transient: timeouts, connection errors, 5xx."""


@dataclass
class Task:
    id: str  # opaque string, never int
    content: str
    description: str = ""
    due_date: date | None = None
    priority: int = 1
    is_recurring: bool = False


class _Response(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...


def _parse_task(payload: dict[str, Any]) -> Task:
    due = payload.get("due") or {}
    due_date: date | None = None
    is_recurring = False
    if isinstance(due, dict):
        raw_date = due.get("date")
        if isinstance(raw_date, str) and len(raw_date) >= 10:
            try:
                due_date = date.fromisoformat(raw_date[:10])
            except ValueError:
                due_date = None
        is_recurring = bool(due.get("is_recurring", False))
    return Task(
        id=str(payload.get("id", "")),
        content=str(payload.get("content", "")),
        description=str(payload.get("description", "") or ""),
        due_date=due_date,
        priority=int(payload.get("priority", 1) or 1),
        is_recurring=is_recurring,
    )


def _retry_after_seconds(headers: Any) -> int:
    try:
        raw: Any = None
        if headers is None:
            raw = None
        elif isinstance(headers, dict):
            raw = headers.get("Retry-After", headers.get("retry-after"))
        else:  # requests CaseInsensitiveDict or similar
            try:
                raw = headers.get("Retry-After", None)
            except Exception:
                raw = None
        return int(str(raw).strip()) if raw is not None else 60
    except (ValueError, TypeError, AttributeError):
        return 60


class TodoistClient:
    """Thin wrapper over Todoist API v1.

    `session` is injectable for tests: any object exposing
    .get/.post returning objects with .status_code/.json()/headers.
    """

    def __init__(self, token: str, timeout: int | float = 10, session: Any = None):
        self.token = token
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()
        self.base_url = BASE_URL

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra:
            headers.update(extra)
        return headers

    def _raise_for_status(self, resp: _Response, context: str) -> None:
        code = resp.status_code
        if 200 <= code < 300:
            return
        headers = getattr(resp, "headers", {}) or {}
        if code == 401:
            raise TodoistAuthError(f"Unauthorized ({context}): invalid token")
        if code == 429:
            raise TodoistRateLimited(
                f"Rate limited ({context})",
                retry_after=_retry_after_seconds(headers),
            )
        if 500 <= code < 600:
            raise TodoistNetworkError(f"Server error {code} ({context})")
        if 400 <= code < 500:
            raise TodoistError(f"Request failed {code} ({context})")
        raise TodoistError(f"Unexpected status {code} ({context})")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> _Response:
        try:
            resp = self.session.get(
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TodoistNetworkError(f"Network error GET {path}: {exc}") from exc
        except requests.RequestException as exc:
            raise TodoistNetworkError(f"Network error GET {path}: {exc}") from exc
        return resp

    def _post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> _Response:
        try:
            resp = self.session.post(
                f"{self.base_url}{path}",
                headers=self._headers(extra_headers),
                json=json,
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TodoistNetworkError(f"Network error POST {path}: {exc}") from exc
        except requests.RequestException as exc:
            raise TodoistNetworkError(f"Network error POST {path}: {exc}") from exc
        return resp

    def _fetch_all_tasks(self) -> list[dict[str, Any]]:
        """GET /tasks paging through ALL pages; return raw payload dicts."""
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        # Hard cap: a misbehaving pager echoing cursors must not loop forever.
        for _page in range(50):
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = self._get("/tasks", params=params)
            self._raise_for_status(resp, "list tasks")
            try:
                payload = resp.json()
            except (ValueError, AttributeError) as exc:
                raise TodoistError(f"Invalid JSON in list tasks: {exc}") from exc
            if isinstance(payload, dict):
                items = payload.get("results", payload.get("tasks", payload.get("data", [])))
                next_cursor = payload.get("next_cursor")
            elif isinstance(payload, list):
                items = payload
                next_cursor = None
            else:
                raise TodoistError("Unexpected list tasks payload")
            if isinstance(items, list):
                collected.extend(i for i in items if isinstance(i, dict))
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return collected

    def list_today(self) -> list[Task]:
        """GET /tasks paging through ALL pages; filter due_date <= today.

        Excludes date-less tasks. Sorts overdue-first then priority
        descending (4 first).
        """
        today = date.today()
        collected = self._fetch_all_tasks()
        tasks = [_parse_task(item) for item in collected]
        # Exclude date-less; keep due_date <= today.
        filtered = [t for t in tasks if t.due_date is not None and t.due_date <= today]
        # Overdue-first (earliest due date first), then priority desc (4 first).
        filtered.sort(key=lambda t: (t.due_date, -t.priority))  # type: ignore[union-attr]
        return filtered

    def list_all(self) -> list[Task]:
        """Fetch ALL tasks (no date filter); dated first, dateless last."""
        collected = self._fetch_all_tasks()
        tasks = [_parse_task(item) for item in collected]
        dated = [t for t in tasks if t.due_date is not None]
        dateless = [t for t in tasks if t.due_date is None]
        dated.sort(key=lambda t: (t.due_date, -t.priority))  # type: ignore[union-attr]
        dateless.sort(key=lambda t: -t.priority)
        return dated + dateless

    def tasks_by_filter(self, query: str) -> list[Task]:
        """GET /tasks/filter for a Todoist filter query; API order, no sorting."""
        if not query or not query.strip():
            raise ValueError("filter query must not be blank")
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for _page in range(50):
            params: dict[str, Any] = {"query": query, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = self._get("/tasks/filter", params=params)
            self._raise_for_status(resp, "filter tasks")
            try:
                payload = resp.json()
            except (ValueError, AttributeError) as exc:
                raise TodoistError(f"Invalid JSON in filter tasks: {exc}") from exc
            if isinstance(payload, dict):
                items = payload.get("results", payload.get("tasks", payload.get("data", [])))
                next_cursor = payload.get("next_cursor")
            elif isinstance(payload, list):
                items = payload
                next_cursor = None
            else:
                raise TodoistError("Unexpected filter tasks payload")
            if isinstance(items, list):
                collected.extend(i for i in items if isinstance(i, dict))
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return [_parse_task(item) for item in collected]

    def add_task(self, content: str, request_id: str | None = None) -> Task:
        """POST /tasks with X-Request-Id idempotency header."""
        rid = request_id or uuid.uuid4().hex
        resp = self._post(
            "/tasks",
            json={"content": content},
            extra_headers={"X-Request-Id": rid},
        )
        self._raise_for_status(resp, "add task")
        try:
            payload = resp.json()
        except (ValueError, AttributeError) as exc:
            raise TodoistError(f"Invalid JSON in add task: {exc}") from exc
        if not isinstance(payload, dict):
            raise TodoistError("Unexpected add task payload")
        return _parse_task(payload)

    def close_task(self, task_id: str) -> None:
        """POST /tasks/{id}/close, expect 204.

        Recurring tasks close the current occurrence server-side.
        """
        resp = self._post(f"/tasks/{task_id}/close")
        if resp.status_code == 204:
            return None
        self._raise_for_status(resp, "close task")
        return None
