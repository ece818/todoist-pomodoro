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


def test_list_all_returns_dated_and_dateless_sorted_across_two_pages():
    today = date.today()
    early = (today - timedelta(days=1)).isoformat()
    late = (today + timedelta(days=5)).isoformat()
    stub = StubSession()
    pages = {
        None: Resp(
            200,
            {
                "results": [
                    make_task_payload("d1", "late task", late, priority=1),
                    make_task_payload("n1", "no due low", None, priority=1),
                ],
                "next_cursor": "cur1",
            },
        ),
        "cur1": Resp(
            200,
            {
                "results": [
                    make_task_payload("n2", "no due high", None, priority=4),
                    make_task_payload("d2", "early task", early, priority=1),
                ]
            },
        ),
    }

    def handler(url, headers, params, timeout):
        return pages.get(params.get("cursor"), Resp(200, {"results": []}))

    stub.get_handler = handler
    client = TodoistClient("tok", session=stub)
    tasks = client.list_all()
    assert [t.id for t in tasks] == ["d2", "d1", "n2", "n1"]
    assert len(stub.get_calls) == 2


def test_search_matches_case_insensitively_and_ranks_dated_first():
    today = date.today()
    early = (today - timedelta(days=1)).isoformat()
    stub = StubSession()
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    store._all_tasks = [
        Task(id="n1", content="Buy MILK tomorrow", priority=4),
        Task(id="d1", content="buy milk today", due_date=today, priority=1),
        Task(id="d2", content="BUY MILK overdue", due_date=today - timedelta(days=1), priority=1),
        Task(id="x1", content="unrelated", due_date=early, priority=4),
    ]
    results = store.search("buy milk")
    assert [t.id for t in results] == ["d2", "d1", "n1"]


def test_search_on_empty_cache_triggers_fetch():
    today_s = date.today().isoformat()
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(
        200, {"results": [make_task_payload("a1", "Read book", today_s)]}
    )
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    assert store._all_tasks == []
    results = store.search("read")
    assert [t.id for t in results] == ["a1"]
    assert len(stub.get_calls) == 2  # tasks fetch + projects enrichment
    assert [t.id for t in store._all_tasks] == ["a1"]


def test_search_blank_query_returns_empty():
    stub = StubSession()
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    assert store.search("") == []
    assert store.search("   ") == []
    assert len(stub.get_calls) == 0  # no fetch on blank query


# --- appended: Todoist filter-query support ---

def test_tasks_by_filter_paginates_two_pages_and_sends_query():
    today_s = date.today().isoformat()
    stub = StubSession()
    pages = {
        None: Resp(
            200,
            {
                "results": [make_task_payload("f1", "F one", today_s)],
                "next_cursor": "cur1",
            },
        ),
        "cur1": Resp(
            200, {"results": [make_task_payload("f2", "F two", today_s)]}
        ),
    }

    def handler(url, headers, params, timeout):
        assert url.endswith("/tasks/filter")
        assert params["query"] == "today"
        assert params["limit"] == 200
        return pages.get(params.get("cursor"), Resp(200, {"results": []}))

    stub.get_handler = handler
    client = TodoistClient("tok", session=stub)
    tasks = client.tasks_by_filter("today")
    assert [t.id for t in tasks] == ["f1", "f2"]  # API order preserved
    assert len(stub.get_calls) == 2


def test_tasks_by_filter_blank_query_raises():
    client = TodoistClient("tok", session=StubSession())
    with pytest.raises(ValueError):
        client.tasks_by_filter("")
    with pytest.raises(ValueError):
        client.tasks_by_filter("   ")


def test_filter_store_roundtrip_and_overwrite(tmp_path):
    from sync import FilterStore

    path = tmp_path / "sub" / "filters.json"
    store = FilterStore(path)
    assert store.list() == []  # missing file -> []
    store.add("today", "today")
    store.add("work", "project:work")
    assert store.get("today") == "today"
    assert {e["name"] for e in store.list()} == {"today", "work"}
    store.add("today", "overdue")  # same-name overwrites
    assert store.get("today") == "overdue"
    assert len(store.list()) == 2
    store.remove("work")
    assert store.get("work") is None
    assert store.list() == [{"name": "today", "query": "overdue"}]
    store.remove("absent")  # no-op, still persists
    assert store.list() == [{"name": "today", "query": "overdue"}]
    # reload from disk
    assert FilterStore(path).list() == [{"name": "today", "query": "overdue"}]


def test_filter_store_corrupt_file_returns_empty(tmp_path):
    from sync import FilterStore

    path = tmp_path / "filters.json"
    path.write_text("not-json{{{", encoding="utf-8")
    store = FilterStore(path)
    assert store.list() == []
    assert store.get("anything") is None


def test_filter_store_blank_name_or_query_raises(tmp_path):
    from sync import FilterStore

    store = FilterStore(tmp_path / "filters.json")
    with pytest.raises(ValueError):
        store.add("", "today")
    with pytest.raises(ValueError):
        store.add("name", "   ")


def test_set_filter_clears_cache():
    stub = StubSession()
    store = SyncStore(TodoistClient("tok", session=stub))
    store._all_tasks = [Task(id="x", content="X")]
    store.set_filter("today")
    assert store.active_filter == "today"
    assert store._all_tasks == []
    store.set_filter("   ")
    assert store.active_filter is None
    store.set_filter(None)
    assert store.active_filter is None


def test_search_honors_active_filter():
    from todoist import Task as TTask

    calls: list[str] = []

    class StubClient:
        def list_all(self):
            calls.append("list_all")
            return [TTask(id="a", content="buy milk")]

        def tasks_by_filter(self, query):
            calls.append(f"tasks_by_filter:{query}")
            assert query == "today"
            return [
                TTask(id="f1", content="buy milk filtered"),
                TTask(id="f2", content="unrelated"),
            ]

    store = SyncStore(StubClient())  # type: ignore[arg-type]
    store.set_filter("today")
    results = store.search("milk")
    assert [t.id for t in results] == ["f1"]
    assert any(c.startswith("tasks_by_filter") for c in calls)
    assert "list_all" not in calls


def test_list_projects_parses_and_skips_blanks():
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(
        200,
        {
            "results": [
                {"id": "p1", "name": "Work"},
                {"id": "p2", "name": "Personal"},
                {"id": "", "name": "NoId"},
                {"id": "p3", "name": "  "},
            ]
        },
    )
    client = TodoistClient("tok", session=stub)
    projects = client.list_projects()
    assert [(p.id, p.name) for p in projects] == [("p1", "Work"), ("p2", "Personal")]
    assert "/projects" in stub.get_calls[0]["url"]


def test_project_name_mapping_and_search_matches_project():
    stub = StubSession()
    stub.get_handler = lambda *a: Resp(
        200, {"results": [{"id": "w1", "name": "Work"}]}
    )
    stub.post_handler = None
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    store._projects = {"w1": "Work"}
    assert store.project_name("w1") == "Work"
    assert store.project_name("nope") == ""
    assert [pid for pid, _ in store.projects()] == ["w1"]
    # search matches project name as well as content
    store._all_tasks = [
        Task(id="t1", content="write report", project_id="w1"),
        Task(id="t2", content="buy milk", project_id=""),
    ]
    ids = [t.id for t in store.search("work")]
    assert ids == ["t1"]


def test_session_log_and_stats(tmp_path):
    from datetime import datetime
    from sessions import SessionStore, format_duration

    path = tmp_path / "sessions.jsonl"
    store = SessionStore(path)
    today = datetime.now().astimezone().date()
    store.log(task_id="t1", content="Write report", project_name="Work",
              duration_sec=1500, finished=True)
    store.log(task_id="t2", content="Read book", project_name="Personal",
              duration_sec=300, finished=False)
    data = store.stats(today=today)
    assert data["today_sec"] == 1800
    assert data["week_sec"] == 1800
    assert data["by_project"] == {"Work": 1500, "Personal": 300}
    assert data["by_task"]["Write report"] == 1500
    assert data["streak"] == 1
    assert format_duration(5400) == "1h 30m"
    assert format_duration(1500) == "25m"
    assert format_duration(45) == "45s"
    # corrupt lines are skipped, rest still counts
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert store.stats(today=today)["today_sec"] == 1800
    # missing id falls back to "(no project)"
    store.log(content="loose task", duration_sec=60)
    assert store.stats(today=today)["by_project"].get("(no project)") == 60


# --- appended: v2 task actions (quick-add + reschedule) ---

def test_quick_add_sends_text_and_request_id_and_parses_timed_due():
    stub = StubSession()

    def handler(url, headers, payload, timeout):
        assert url.endswith("/tasks/quick")
        assert payload == {"text": "Buy milk Mon 2pm"}
        assert headers.get("X-Request-Id")
        assert timeout == 10
        return Resp(
            200,
            {
                "id": "q1",
                "content": "Buy milk",
                "description": "",
                "priority": 1,
                "due": {"date": "2026-10-12T18:00:00Z"},
            },
        )

    stub.post_handler = handler
    client = TodoistClient("tok", session=stub)
    task = client.quick_add("Buy milk Mon 2pm")
    assert task.id == "q1"
    assert task.due_date == date(2026, 10, 12)


def test_quick_add_blank_raises_value_error():
    client = TodoistClient("tok", session=StubSession())
    with pytest.raises(ValueError):
        client.quick_add("")
    with pytest.raises(ValueError):
        client.quick_add("   ")
    # store-level blank never queues
    store = SyncStore(client)
    with pytest.raises(ValueError):
        store.quick_add("  ")
    assert store.pending_adds == []


def test_transient_quick_add_queues_with_kind_and_replays_via_quick_endpoint():
    stub = StubSession()
    stub.post_handler = lambda *a: (_ for _ in ()).throw(
        requests.ConnectionError("down")
    )
    client = TodoistClient("tok", session=stub)
    store = SyncStore(client)
    with pytest.raises(TodoistNetworkError):
        store.quick_add("Buy milk Mon 2pm")
    assert len(store.pending_adds) == 1
    entry = store.pending_adds[0]
    assert len(entry) == 3 and entry[0] == "quick" and entry[1] == "Buy milk Mon 2pm"
    original_rid = entry[2]

    urls: list[str] = []

    def succeed(url, headers, payload, timeout):
        urls.append(url)
        assert headers.get("X-Request-Id") == original_rid
        return Resp(200, make_task_payload("n1", "Buy milk"))

    stub.post_handler = succeed
    result = store.retry_pending()
    assert result == {"added": 1, "still_pending": 0}
    assert urls and "/tasks/quick" in urls[0]


def test_legacy_two_tuple_pending_entries_replay_via_plain_add():
    stub = StubSession()
    urls: list[str] = []

    def handler(url, headers, payload, timeout):
        urls.append(url)
        return Resp(200, make_task_payload("x1", payload["content"]))

    stub.post_handler = handler
    store = SyncStore(TodoistClient("tok", session=stub))
    store.pending_adds = [("one", "rid-1")]
    result = store.retry_pending()
    assert result == {"added": 1, "still_pending": 0}
    assert urls and urls[0].endswith("/tasks")
    assert "/tasks/quick" not in urls[0]


def test_reschedule_posts_due_string_verbatim_and_returns_updated_task():
    stub = StubSession()

    def handler(url, headers, payload, timeout):
        assert url.endswith("/tasks/t1")
        assert payload == {"due_string": "next Friday"}
        assert timeout == 10
        return Resp(200, make_task_payload("t1", "T", "2026-10-16"))

    stub.post_handler = handler
    client = TodoistClient("tok", session=stub)
    updated = client.update_due("t1", "next Friday")
    assert updated.id == "t1"
    assert updated.due_date == date(2026, 10, 16)

    # store-level delegation posts verbatim too
    seen: list[dict[str, Any]] = []

    def handler2(url, headers, payload, timeout):
        seen.append({"url": url, "json": payload})
        return Resp(200, make_task_payload("t1", "T", "2026-10-16"))

    stub.post_handler = handler2
    store = SyncStore(client)
    out = store.reschedule(Task(id="t1", content="T"), "next Friday")
    assert out.due_date == date(2026, 10, 16)
    assert seen[0]["json"] == {"due_string": "next Friday"}


def test_reschedule_blank_raises_value_error():
    client = TodoistClient("tok", session=StubSession())
    with pytest.raises(ValueError):
        client.update_due("t1", "")
    with pytest.raises(ValueError):
        client.update_due("t1", "   ")
    store = SyncStore(client)
    with pytest.raises(ValueError):
        store.reschedule(Task(id="t1", content="T"), "  ")
