"""pytest suite for todoist client + sync store (no network)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
import requests

from sync import SyncStore
from todoist import (
    Task,
    TodoistAuthError,
    TodoistClient,
    TodoistError,
    TodoistNetworkError,
    TodoistRateLimited,
)


class Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class StubSession:
    """Stub exposing .get/.post; optionally records calls and timeouts."""

    def __init__(self):
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.get_handler = None
        self.post_handler = None
        self.timeouts: list[Any] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.get_calls.append(
            {"url": url, "headers": headers, "params": params, "timeout": timeout}
        )
        self.timeouts.append(timeout)
        if self.get_handler:
            return self.get_handler(url, headers, params, timeout)
        return Resp(200, {"results": []})

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        self.timeouts.append(timeout)
        if self.post_handler:
            return self.post_handler(url, headers, json, timeout)
        return Resp(200, {})


def make_task_payload(tid, content, due_str=None, priority=1, recurring=False):
    payload = {"id": tid, "content": content, "description": "", "priority": priority}
    if due_str is not None:
        payload["due"] = {"date": due_str, "is_recurring": recurring}
    return payload


def test_today_filter_excludes_dateless_and_sorts():
    today = date.today()
    overdue = (today - timedelta(days=2)).isoformat()
    today_s = today.isoformat()
    future = (today + timedelta(days=3)).isoformat()
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(
        200,
        {
            "results": [
                make_task_payload("1", "overdue-low", overdue, priority=1),
                make_task_payload("2", "today-high", today_s, priority=4),
                make_task_payload("3", "no-due", None, priority=4),
                make_task_payload("4", "future", future, priority=4),
                make_task_payload("5", "overdue-high", overdue, priority=4),
                make_task_payload("6", "today-low", today_s, priority=1),
            ]
        },
    )
    client = TodoistClient("tok", session=stub)
    tasks = client.list_today()
    ids = [t.id for t in tasks]
    assert "3" not in ids  # date-less excluded
    assert "4" not in ids  # future excluded
    assert all(isinstance(t.id, str) for t in tasks)
    # overdue-first, then priority desc within same date
    assert ids == ["5", "1", "2", "6"]
    # timeout passed on every call
    assert stub.timeouts and all(t == 10 for t in stub.timeouts)


def test_pagination_across_two_pages():
    today_s = date.today().isoformat()
    stub = StubSession()
    pages = {
        None: Resp(
            200,
            {
                "results": [make_task_payload("a", "A", today_s)],
                "next_cursor": "cur1",
            },
        ),
        "cur1": Resp(
            200, {"results": [make_task_payload("b", "B", today_s)]}
        ),
    }

    def handler(url, headers, params, timeout):
        assert params["limit"] == 200
        return pages.get(params.get("cursor"), Resp(200, {"results": []}))

    stub.get_handler = handler
    client = TodoistClient("tok", session=stub)
    tasks = client.list_today()
    assert [t.id for t in tasks] == ["a", "b"]
    assert len(stub.get_calls) == 2


def test_add_sends_request_id_and_reuses_on_retry():
    stub = StubSession()
    sent_ids: list[str] = []

    def fail_once(url, headers, payload, timeout):
        sent_ids.append(headers.get("X-Request-Id"))
        raise requests.ConnectionError("down")

    stub.post_handler = fail_once
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    with pytest.raises(TodoistNetworkError):
        store.add("hello")
    assert len(store.pending_adds) == 1
    original_rid = store.pending_adds[0][1]
    assert sent_ids[0] == original_rid

    # retry reuses SAME request id
    def succeed(url, headers, payload, timeout):
        sent_ids.append(headers.get("X-Request-Id"))
        return Resp(200, make_task_payload("n1", payload["content"]))

    stub.post_handler = succeed
    result = store.retry_pending()
    assert result == {"added": 1, "still_pending": 0}
    assert sent_ids[1] == original_rid
    assert store.pending_adds == []


def test_retry_drops_on_success_keeps_on_transient():
    stub = StubSession()
    calls = {"n": 0}

    def handler(url, headers, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return Resp(200, make_task_payload("x1", payload["content"]))
        raise requests.Timeout("slow")

    stub.post_handler = handler
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    store.pending_adds = [("one", "rid-1"), ("two", "rid-2")]
    result = store.retry_pending()
    assert result == {"added": 1, "still_pending": 1}
    assert store.pending_adds == [("two", "rid-2")]


def test_401_raises_auth_and_aborts_retry():
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(401, {})
    stub.post_handler = lambda *a: Resp(401, {})
    client = TodoistClient("tok", session=stub)
    with pytest.raises(TodoistAuthError):
        client.list_today()
    store = SyncStore(client)
    store.pending_adds = [("a", "r1"), ("b", "r2")]
    with pytest.raises(TodoistAuthError):
        store.retry_pending()
    # aborted: nothing dropped
    assert len(store.pending_adds) == 2


def test_429_carries_retry_after():
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(429, {}, headers={"Retry-After": "17"})
    client = TodoistClient("tok", session=stub)
    with pytest.raises(TodoistRateLimited) as ei:
        client.list_today()
    assert ei.value.retry_after == 17

    stub2 = StubSession()
    stub2.get_handler = lambda *a: Resp(429, {}, headers={})
    client2 = TodoistClient("tok", session=stub2)
    with pytest.raises(TodoistRateLimited) as ei2:
        client2.list_today()
    assert ei2.value.retry_after == 60


def test_close_reconciles_via_refetch():
    today_s = date.today().isoformat()
    stub = StubSession()
    state = {"tasks": [make_task_payload("t1", "T", today_s)]}

    def post(url, headers, payload, timeout):
        assert url.endswith("/tasks/t1/close")
        state["tasks"] = []  # server closes it
        return Resp(204, None)

    def get(url, headers, params, timeout):
        return Resp(200, {"results": list(state["tasks"])})

    stub.post_handler = post
    stub.get_handler = get
    store = SyncStore(TodoistClient("tok", session=stub))
    assert store.close(Task(id="t1", content="T")) is None
    assert len(stub.get_calls) == 1  # reconciling re-fetch happened


def test_double_close_while_inflight_raises():
    stub = StubSession()

    def post(url, headers, payload, timeout):
        return Resp(204, None)

    stub.post_handler = post
    stub.get_handler = lambda *a: Resp(200, {"results": []})
    store = SyncStore(TodoistClient("tok", session=stub))
    store._closing.add("dup")
    try:
        with pytest.raises(RuntimeError):
            store.close(Task(id="dup", content="D"))
    finally:
        store._closing.discard("dup")
    # normal close afterwards works and releases guard
    store.close(Task(id="dup", content="D"))
    assert "dup" not in store._closing


def test_recurring_close_returns_none():
    stub = StubSession()
    today_s = date.today().isoformat()
    rec = make_task_payload("r1", "daily", today_s, recurring=True)
    stub.post_handler = lambda *a: Resp(204, None)
    stub.get_handler = lambda *a: Resp(200, {"results": []})
    store = SyncStore(TodoistClient("tok", session=stub))
    task = Task(id="r1", content="daily", due_date=date.today(), is_recurring=True)
    assert task.is_recurring is True
    assert store.close(task) is None


def test_other_4xx_maps_to_todoist_error_and_5xx_to_network():
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(400, {})
    with pytest.raises(TodoistError):
        TodoistClient("tok", session=stub).list_today()
    stub.get_handler = lambda *a: Resp(500, {})
    with pytest.raises(TodoistNetworkError):
        TodoistClient("tok", session=stub).list_today()


def test_save_load_state_roundtrip(tmp_path):
    stub = StubSession()
    path = tmp_path / "state.json"
    store = SyncStore(TodoistClient("tok", session=stub), state_path=path)
    store.pending_adds = [("a", "r1"), ("b", "r2")]
    store.save_state()
    store2 = SyncStore(TodoistClient("tok", session=stub), state_path=path)
    store2.load_state()
    assert store2.pending_adds == [("a", "r1"), ("b", "r2")]


def test_close_raises_when_non_recurring_still_present():
    today_s = date.today().isoformat()
    stub = StubSession()
    stub.post_handler = lambda *a: Resp(204, None)
    # Server claims close but the task is still listed and not recurring.
    stub.get_handler = (
        lambda *a: Resp(200, {"results": [make_task_payload("t1", "T", today_s)]})
    )
    store = SyncStore(TodoistClient("tok", session=stub))
    with pytest.raises(TodoistError):
        store.close(Task(id="t1", content="T"))
    assert "t1" not in store._closing  # guard released


def test_close_recurring_reappearance_is_ok():
    today_s = date.today().isoformat()
    stub = StubSession()
    stub.post_handler = lambda *a: Resp(204, None)
    # Next occurrence appears with the same id: legitimate for recurring.
    stub.get_handler = (
        lambda *a: Resp(
            200, {"results": [make_task_payload("r1", "daily", today_s, recurring=True)]}
        )
    )
    store = SyncStore(TodoistClient("tok", session=stub))
    task = Task(id="r1", content="daily", due_date=date.today(), is_recurring=True)
    assert store.close(task) is None


def test_pagination_cap_breaks_cursor_echo():
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(
        200, {"results": [], "next_cursor": "stuck"}
    )
    client = TodoistClient("tok", session=stub)
    assert client.list_today() == []
    assert len(stub.get_calls) == 50  # capped, never infinite
