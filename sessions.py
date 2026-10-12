"""Local focus-session log + stats (no network, JSONL append)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class Session:
    task_id: str = ""
    content: str = ""
    project_id: str = ""
    project_name: str = ""
    started_at: str = ""  # ISO-8601 UTC
    duration_sec: int = 0
    finished: bool = False  # True = timer reached 0; False = partial (reset)


def _parse_dt(raw: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=None)
    return moment


class SessionStore:
    """Append-only session log with day/week/project/task rollups."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # -- write --------------------------------------------------------
    def log(
        self,
        task_id: str = "",
        content: str = "",
        project_id: str = "",
        project_name: str = "",
        duration_sec: int = 0,
        finished: bool = False,
        started_at: str | None = None,
    ) -> Session:
        session = Session(
            task_id=str(task_id or ""),
            content=str(content or ""),
            project_id=str(project_id or ""),
            project_name=str(project_name or ""),
            started_at=started_at
            or datetime.now().astimezone().isoformat(timespec="seconds"),
            duration_sec=max(0, int(duration_sec)),
            finished=bool(finished),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "task_id": session.task_id,
                "content": session.content,
                "project_id": session.project_id,
                "project_name": session.project_name,
                "started_at": session.started_at,
                "duration_sec": session.duration_sec,
                "finished": session.finished,
            }
        )
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        finally:
            try:
                os.close(fd)
                os.unlink(tmp)
            except OSError:
                pass
        return session

    # -- read ---------------------------------------------------------
    def read_all(self) -> list[Session]:
        if not self.path.exists():
            return []
        sessions: list[Session] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue  # skip corrupt lines, keep the rest
            if not isinstance(data, dict):
                continue
            sessions.append(
                Session(
                    task_id=str(data.get("task_id", "") or ""),
                    content=str(data.get("content", "") or ""),
                    project_id=str(data.get("project_id", "") or ""),
                    project_name=str(data.get("project_name", "") or ""),
                    started_at=str(data.get("started_at", "") or ""),
                    duration_sec=max(0, int(data.get("duration_sec", 0) or 0)),
                    finished=bool(data.get("finished", False)),
                )
            )
        return sessions

    # -- stats --------------------------------------------------------
    def stats(self, today: date | None = None) -> dict:
        """Rollup: today/week seconds, per-project, per-task, day streak."""
        today = today or date.today()
        week_start = today - timedelta(days=6)
        today_sec = 0
        week_sec = 0
        by_project: dict[str, int] = {}
        by_task: dict[str, int] = {}
        active_days: set[date] = set()
        for session in self.read_all():
            moment = _parse_dt(session.started_at)
            day = moment.date() if moment is not None else None
            if day is None or day > today:
                continue
            if day == today:
                today_sec += session.duration_sec
            if day >= week_start:
                week_sec += session.duration_sec
            project = (
                session.project_name
                or (f"id:{session.project_id}" if session.project_id else "(no project)")
            )
            by_project[project] = by_project.get(project, 0) + session.duration_sec
            label = session.content or "(no task)"
            by_task[label] = by_task.get(label, 0) + session.duration_sec
            active_days.add(day)
        # Streak: consecutive active days ending today (or yesterday,
        # so today doesn't kill an alive streak before the first session).
        streak = 0
        cursor = today
        if cursor not in active_days:
            cursor -= timedelta(days=1)
        while cursor in active_days:
            streak += 1
            cursor -= timedelta(days=1)
        return {
            "today_sec": today_sec,
            "week_sec": week_sec,
            "by_project": by_project,
            "by_task": by_task,
            "streak": streak,
        }


def format_duration(total_sec: int) -> str:
    """'2h 15m', '45m', '30s', or '0m'."""
    total_sec = max(0, int(total_sec))
    hours, remainder = divmod(total_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    if seconds or total_sec == 0:
        return f"{seconds}s" if seconds else "0m"
    return "0m"
