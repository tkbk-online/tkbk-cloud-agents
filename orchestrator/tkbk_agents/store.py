"""SQLite task store + in-memory secret vault.

Client secrets (cr_ key, git token) are NEVER written to the database: they live in
`SecretVault` for the lifetime of the task and are dropped when it ends.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    ACTIVE_STATUSES,
    RESERVING_STATUSES,
    TaskMetrics,
    TaskResult,
    TaskStatus,
    TaskView,
    UsageRow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'repo',
    key_id TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    repo TEXT,
    base_branch TEXT,
    work_branch TEXT,
    template TEXT NOT NULL,
    instruction TEXT NOT NULL DEFAULT '',
    issue INTEGER,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    deliver TEXT NOT NULL,
    push INTEGER NOT NULL DEFAULT 0,
    max_minutes INTEGER NOT NULL,
    max_iterations INTEGER NOT NULL,
    concurrency_limit INTEGER NOT NULL DEFAULT 1,
    client_ref TEXT,
    callback_url TEXT,
    keep_workspace INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    minutes_billed INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    result_json TEXT,
    events INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tasks_key_created ON tasks(key_id, created_at);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
"""

COLS = [
    "id", "status", "phase", "source", "key_id", "key_prefix", "plan", "repo", "base_branch", "work_branch",
    "template", "instruction", "issue", "model", "provider", "deliver", "push", "max_minutes", "max_iterations",
    "concurrency_limit", "client_ref", "callback_url", "keep_workspace", "created_at",
]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def new_task_id() -> str:
    return "t_" + secrets.token_hex(8)


@dataclass
class TaskSecrets:
    api_key: str
    git_token: str | None = None


class SecretVault:
    def __init__(self) -> None:
        self._data: dict[str, TaskSecrets] = {}
        self._lock = threading.Lock()

    def put(self, task_id: str, s: TaskSecrets) -> None:
        with self._lock:
            self._data[task_id] = s

    def get(self, task_id: str) -> TaskSecrets | None:
        with self._lock:
            return self._data.get(task_id)

    def drop(self, task_id: str) -> None:
        with self._lock:
            self._data.pop(task_id, None)

    def __len__(self) -> int:
        return len(self._data)


class TaskStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- writes
    def create(self, **fields: Any) -> TaskView:
        fields.setdefault("id", new_task_id())
        fields.setdefault("status", TaskStatus.QUEUED)
        fields.setdefault("created_at", utcnow())
        row = {c: fields.get(c) for c in COLS}
        row["phase"] = row["phase"] or ""
        row["instruction"] = row["instruction"] or ""
        row["plan"] = row["plan"] or ""
        row["source"] = str(row["source"] or "repo")
        row["push"] = 1 if row["push"] else 0
        row["keep_workspace"] = 1 if row["keep_workspace"] else 0
        row["concurrency_limit"] = int(row["concurrency_limit"] or 1)
        row["created_at"] = _iso(row["created_at"])
        for enum_col in ("status", "template", "provider", "deliver"):
            row[enum_col] = str(row[enum_col])
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tasks ({', '.join(COLS)}) VALUES ({', '.join('?' for _ in COLS)})",
                [row[c] for c in COLS],
            )
        return self.get(fields["id"])  # type: ignore[return-value]

    def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if isinstance(v, datetime):
                v = _iso(v)
            elif isinstance(v, TaskMetrics):
                k, v = "metrics_json", json.dumps(v.model_dump())
            elif isinstance(v, TaskResult):
                k, v = "result_json", json.dumps(v.model_dump())
            elif isinstance(v, bool):
                v = 1 if v else 0
            elif hasattr(v, "value"):
                v = str(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(task_id)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)

    def set_status(self, task_id: str, status: TaskStatus, phase: str = "", **extra: Any) -> None:
        self.update(task_id, status=status, phase=phase, **extra)

    def request_cancel(self, task_id: str) -> bool:
        """Mark cancellation. Waiting tasks are cancelled right away; active ones at the next checkpoint."""
        with self._lock:
            row = self._conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return False
            status = TaskStatus(row["status"])
            if status.terminal:
                return False
            if status in (TaskStatus.QUEUED, TaskStatus.AWAITING_UPLOAD):
                self._conn.execute(
                    "UPDATE tasks SET status = ?, finished_at = ?, cancel_requested = 1 WHERE id = ?",
                    (str(TaskStatus.CANCELLED), _iso(utcnow()), task_id),
                )
            else:
                self._conn.execute("UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,))
            return True

    def cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def claim_next_queued(self, busy_keys: dict[str, int] | None = None) -> TaskView | None:
        """Atomically move the oldest queued task whose client is under its concurrency limit to `preparing`."""
        busy_keys = busy_keys or {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id, key_id, concurrency_limit FROM tasks WHERE status = ? ORDER BY created_at, id LIMIT 50",
                    (str(TaskStatus.QUEUED),),
                ).fetchall()
                chosen = None
                for row in rows:
                    if busy_keys.get(row["key_id"], 0) < int(row["concurrency_limit"]):
                        chosen = row
                        break
                if chosen is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE tasks SET status = ?, phase = 'claimed', started_at = ? WHERE id = ?",
                    (str(TaskStatus.PREPARING), _iso(utcnow()), chosen["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get(chosen["id"])

    def fail_active_on_restart(self, reason: str) -> list[str]:
        """Tasks that were waiting or active when the process died cannot be resumed (secrets were in memory)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM tasks WHERE status IN ({', '.join('?' for _ in RESERVING_STATUSES)})",
                [str(s) for s in RESERVING_STATUSES],
            ).fetchall()
            ids = [r["id"] for r in rows]
            for tid in ids:
                self._conn.execute(
                    "UPDATE tasks SET status = ?, phase = '', error = ?, finished_at = ? WHERE id = ?",
                    (str(TaskStatus.FAILED), reason, _iso(utcnow()), tid),
                )
        return ids

    # ----------------------------------------------------------------- reads
    def get(self, task_id: str) -> TaskView | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._view(row) if row else None

    def list(self, key_id: str | None = None, status: TaskStatus | None = None, limit: int = 50) -> list[TaskView]:
        where, vals = [], []
        if key_id:
            where.append("key_id = ?")
            vals.append(key_id)
        if status:
            where.append("status = ?")
            vals.append(str(status))
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        vals.append(max(1, min(limit, 500)))
        with self._lock:
            rows = self._conn.execute(sql, vals).fetchall()
        return [self._view(r) for r in rows]

    def count_active(self, key_id: str | None = None) -> int:
        sql = f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({', '.join('?' for _ in ACTIVE_STATUSES)})"
        vals: list[Any] = [str(s) for s in ACTIVE_STATUSES]
        if key_id:
            sql += " AND key_id = ?"
            vals.append(key_id)
        with self._lock:
            row = self._conn.execute(sql, vals).fetchone()
        return int(row["n"])

    def active_by_key(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT key_id, COUNT(*) AS n FROM tasks WHERE status IN ({', '.join('?' for _ in ACTIVE_STATUSES)}) "
                "GROUP BY key_id", [str(s) for s in ACTIVE_STATUSES],
            ).fetchall()
        return {r["key_id"]: int(r["n"]) for r in rows}

    def minutes_this_month(self, key_id: str, month: str | None = None) -> int:
        """Billed minutes for a key in a calendar month (YYYY-MM, UTC). Waiting/active tasks reserve max_minutes."""
        month = month or utcnow().strftime("%Y-%m")
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN status IN ('awaiting_upload','queued','preparing','running','delivering')
                                            THEN max_minutes ELSE minutes_billed END), 0) AS m
                   FROM tasks WHERE key_id = ? AND substr(created_at, 1, 7) = ?""",
                (key_id, month),
            ).fetchone()
        return int(row["m"])

    def usage(self, month: str, key_id: str | None = None) -> list[UsageRow]:
        where = "substr(created_at, 1, 7) = ?"
        vals: list[Any] = [month]
        if key_id:
            where += " AND key_id = ?"
            vals.append(key_id)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT key_id, key_prefix, COUNT(*) AS tasks, COALESCE(SUM(minutes_billed), 0) AS minutes, "
                f"GROUP_CONCAT(metrics_json, '\n') AS metrics FROM tasks WHERE {where} GROUP BY key_id, key_prefix "
                f"ORDER BY minutes DESC",
                vals,
            ).fetchall()
        out: list[UsageRow] = []
        for r in rows:
            p = c = 0
            cost = 0.0
            for chunk in (r["metrics"] or "").split("\n"):
                if not chunk.strip():
                    continue
                try:
                    m = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                p += int(m.get("prompt_tokens", 0))
                c += int(m.get("completion_tokens", 0))
                cost += float(m.get("cost_usd_reported", 0.0))
            out.append(UsageRow(key_id=r["key_id"], key_prefix=r["key_prefix"], tasks=int(r["tasks"]),
                                minutes=int(r["minutes"]), prompt_tokens=p, completion_tokens=c,
                                cost_usd_reported=round(cost, 6)))
        return out

    def instruction_and_issue(self, task_id: str) -> tuple[str, int | None]:
        with self._lock:
            row = self._conn.execute("SELECT instruction, issue FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return (row["instruction"], row["issue"]) if row else ("", None)

    def callback_url(self, task_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT callback_url FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row["callback_url"] if row and row["callback_url"] else None

    def finished_before(self, before_iso: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE finished_at IS NOT NULL AND finished_at < ?", (before_iso,)
            ).fetchall()
        return [r["id"] for r in rows]

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _view(row: sqlite3.Row) -> TaskView:
        metrics = TaskMetrics(**json.loads(row["metrics_json"] or "{}"))
        result = TaskResult(**json.loads(row["result_json"])) if row["result_json"] else None
        return TaskView(
            id=row["id"], status=TaskStatus(row["status"]), phase=row["phase"] or "", source=row["source"],
            template=row["template"], repo=row["repo"], base_branch=row["base_branch"],
            work_branch=row["work_branch"], model=row["model"], provider=row["provider"], deliver=row["deliver"],
            push=bool(row["push"]), key_id=row["key_id"], key_prefix=row["key_prefix"], plan=row["plan"] or "",
            client_ref=row["client_ref"], created_at=_dt(row["created_at"]), started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]), max_minutes=int(row["max_minutes"]),
            max_iterations=int(row["max_iterations"]), minutes_billed=int(row["minutes_billed"]),
            keep_workspace=bool(row["keep_workspace"]), concurrency_limit=int(row["concurrency_limit"]),
            metrics=metrics, error=row["error"], result=result, events=int(row["events"] or 0),
        )
