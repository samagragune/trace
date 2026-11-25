"""Local investigation history in SQLite (``~/.trace/history.db``).

Two tables:
* ``tasks``  - one row per debugging task: outcome, usage counters and final report.
* ``events`` - the ordered execution trace (model calls, tool calls, results, decisions).

Messages, tool calls, tool results and decisions from the spec's entity list are all
stored as typed events rather than separate tables. One append-only log is simpler to
write, and it is sufficient to reconstruct a run. Payloads must already be redacted
(wrap this sink in ``RedactingSink``); API keys are never stored here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trace_agent.agent.state import AgentTask
from trace_agent.observability.events import ExecutionEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    project_root TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    stop_reason TEXT,
    summary TEXT,
    report TEXT,
    iterations INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    actions INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
"""


@dataclass(frozen=True)
class TaskRow:
    id: str
    description: str
    project_root: str
    provider: str | None
    model: str | None
    mode: str | None
    status: str
    stop_reason: str | None
    summary: str | None
    report: dict[str, Any] | None
    iterations: int
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    actions: int
    created_at: str
    finished_at: str | None


@dataclass(frozen=True)
class EventRow:
    task_id: str
    type: str
    iteration: int
    timestamp: str
    payload: dict[str, Any]


class HistoryStore:
    """SQLite-backed EventSink plus task records. One connection per operation keeps it thread-safe and simple."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3's own context manager commits but does NOT close; on Windows an open
        # handle keeps the file locked, so we always close explicitly.
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- EventSink -------------------------------------------------------------------

    def emit(self, event: ExecutionEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (id, task_id, type, iteration, timestamp, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.task_id,
                    event.type.value,
                    event.iteration,
                    event.timestamp.isoformat(),
                    json.dumps(event.payload, default=str),
                ),
            )

    # ---- tasks -------------------------------------------------------------------------

    def save_task(self, task: AgentTask, *, provider: str | None, model: str | None, mode: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks (id, description, project_root, provider, model, mode, status, stop_reason,
                       summary, report, iterations, model_calls, prompt_tokens, completion_tokens, actions,
                       created_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, stop_reason=excluded.stop_reason,
                       summary=excluded.summary, report=excluded.report, iterations=excluded.iterations,
                       model_calls=excluded.model_calls, prompt_tokens=excluded.prompt_tokens,
                       completion_tokens=excluded.completion_tokens, actions=excluded.actions,
                       finished_at=excluded.finished_at""",
                (
                    task.id,
                    task.description,
                    str(task.project_root),
                    provider,
                    model,
                    mode,
                    task.status.value,
                    task.stop_reason.value if task.stop_reason else None,
                    task.summary,
                    json.dumps(task.report) if task.report is not None else None,
                    task.iteration,
                    task.model_calls,
                    task.prompt_tokens,
                    task.completion_tokens,
                    len(task.actions),
                    task.created_at.isoformat(),
                    task.finished_at.isoformat() if task.finished_at else None,
                ),
            )

    def list_tasks(self, limit: int = 20) -> list[TaskRow]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._task_row(r) for r in rows]

    def find_task(self, id_prefix: str) -> TaskRow | None:
        """Look up a task by id or unique id prefix (like git short hashes)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE id LIKE ? ORDER BY created_at DESC LIMIT 2", (id_prefix + "%",)
            ).fetchall()
        if len(rows) != 1:
            return None
        return self._task_row(rows[0])

    def events(self, task_id: str) -> list[EventRow]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,)).fetchall()
        return [EventRow(r["task_id"], r["type"], r["iteration"], r["timestamp"], json.loads(r["payload"])) for r in rows]

    def prune(self, retention_days: int | None, *, now: datetime | None = None) -> int:
        """Delete tasks (and their events) older than ``retention_days``. Returns tasks deleted."""
        if retention_days is None:
            return 0
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE created_at < ?", (cutoff,))]
            conn.executemany("DELETE FROM events WHERE task_id = ?", [(i,) for i in ids])
            conn.executemany("DELETE FROM tasks WHERE id = ?", [(i,) for i in ids])
        return len(ids)

    @staticmethod
    def _task_row(r: sqlite3.Row) -> TaskRow:
        data = dict(r)
        data["report"] = json.loads(data["report"]) if data["report"] else None
        return TaskRow(**data)
