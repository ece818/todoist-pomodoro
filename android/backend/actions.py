"""Single command core for task actions; all UI surfaces call through here."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from todoist import Task

if TYPE_CHECKING:
    from sync import SyncStore


class TaskActions:
    """UI-agnostic task commands over a SyncStore.

    No GTK imports. No network at import.
    """

    def __init__(
        self,
        store: SyncStore,
        on_start_timer: Callable[[Task], Any] | None = None,
    ):
        self.store = store
        self.on_start_timer = on_start_timer

    def complete(self, task: Task) -> None:
        self.store.close(task)

    def reopen(self, task: Task) -> None:
        self.store.reopen(task)

    def reschedule(self, task: Task, due_string: str) -> Task:
        if not due_string or not due_string.strip():
            raise ValueError("due_string must not be blank")
        return self.store.reschedule(task, due_string)

    def quick_add(self, text: str) -> Task:
        return self.store.quick_add(text)

    def start_timer(self, task: Task) -> Any:
        if self.on_start_timer is None:
            raise RuntimeError("no timer handler")
        return self.on_start_timer(task)
