"""Persistent background task registry backed by SQLite and a bounded executor."""

import sqlite3
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class PersistentTaskQueue:
    TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}

    def __init__(self, db_path, max_workers=2, logger=None, recover_incomplete=True):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="ops-task")
        self._futures = {}
        self._closed = False
        self._init_db(recover_incomplete)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self, recover_incomplete=True):
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS background_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    owner_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    result_summary TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_owner_created ON background_tasks(owner_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON background_tasks(status, created_at DESC);
            """)
            if recover_incomplete:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "UPDATE background_tasks SET status='interrupted',error_message='程序重启，任务执行中断',"
                    "finished_at=? WHERE status IN ('queued','running','cancellation_requested')", (now,),
                )

    @staticmethod
    def _new_id():
        return f"TASK-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"

    def submit(self, task_type, owner_key, title, func):
        task_id, now = self._new_id(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if self._closed:
                raise RuntimeError("后台任务队列已关闭")
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO background_tasks(task_id,task_type,owner_key,title,status,progress,created_at) "
                    "VALUES(?,?,?,?, 'queued',0,?)", (task_id, task_type, owner_key, title, now),
                )

        def progress(value, summary=""):
            current = self.get_task(task_id)
            if current and current["status"] in ("cancelled", "cancellation_requested"):
                raise RuntimeError("任务已取消")
            self._update(task_id, progress=max(0, min(int(value), 99)), result_summary=summary)

        def runner():
            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not self._transition(
                task_id, {"queued"}, status="running", progress=5, started_at=started_at,
            ):
                return
            try:
                summary = func(progress) or "任务完成"
                current = self.get_task(task_id)
                if current and current["status"] == "cancellation_requested":
                    self._update(
                        task_id, status="cancelled", error_message="",
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                else:
                    self._update(task_id, status="succeeded", progress=100, result_summary=str(summary)[:500],
                                 finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as exc:
                current = self.get_task(task_id)
                if current and current["status"] in ("cancelled", "cancellation_requested"):
                    self._update(task_id, status="cancelled", error_message="", finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    return
                if self.logger:
                    self.logger.error(f"后台任务失败 {task_id}: {exc}\n{traceback.format_exc()}")
                self._update(task_id, status="failed", error_message=str(exc)[:500],
                             finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("后台任务队列已关闭")
                future = self._executor.submit(runner)
                self._futures[task_id] = future
                # A very fast task may finish before submit() stores its Future.
                # Register cleanup after storage; add_done_callback executes
                # immediately for an already-completed Future and closes the race.
                future.add_done_callback(lambda completed, key=task_id: self._forget_future(key, completed))
        except Exception:
            self._update(
                task_id, status="interrupted", error_message="任务调度失败",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            raise
        return self.get_task(task_id)

    def _forget_future(self, task_id, future):
        with self._lock:
            if self._futures.get(task_id) is future:
                self._futures.pop(task_id, None)

    def _update(self, task_id, **fields):
        allowed = {"status", "progress", "result_summary", "error_message", "started_at", "finished_at"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        with self._connection() as conn:
            conn.execute(
                f"UPDATE background_tasks SET {','.join(f'{key}=?' for key in fields)} WHERE task_id=?",
                (*fields.values(), task_id),
            )

    def _transition(self, task_id, from_statuses, **fields):
        """Atomically update a task only while it remains in an expected state."""
        allowed = {"status", "progress", "result_summary", "error_message", "started_at", "finished_at"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        statuses = tuple(dict.fromkeys(str(value) for value in from_statuses))
        if not fields or not statuses:
            return False
        assignments = ",".join(f"{key}=?" for key in fields)
        placeholders = ",".join("?" for _ in statuses)
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE background_tasks SET {assignments} WHERE task_id=? AND status IN ({placeholders})",
                (*fields.values(), task_id, *statuses),
            )
            return cursor.rowcount == 1

    def get_task(self, task_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM background_tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, owner_key="", status="", limit=100):
        clauses, params = [], []
        if owner_key:
            clauses.append("owner_key=?"); params.append(owner_key)
        if status:
            clauses.append("status=?"); params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM background_tasks{where} ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
            return [dict(row) for row in rows]

    def cancel(self, task_id):
        with self._lock:
            future = self._futures.get(task_id)
            task = self.get_task(task_id)
            if not task or task["status"] in self.TERMINAL:
                return False
            if task["status"] == "queued":
                if future:
                    future.cancel()
                if self._transition(
                    task_id, {"queued"}, status="cancelled",
                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ):
                    return True
                task = self.get_task(task_id)
            if task and task["status"] == "running":
                return self._transition(
                    task_id, {"running"}, status="cancellation_requested",
                    result_summary="正在安全停止任务",
                )
            if task and task["status"] == "cancellation_requested":
                return True
        return True

    def shutdown(self, wait=False):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = list(self._futures.items())
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for task_id, future in futures:
            if future.cancel():
                self._transition(
                    task_id, {"queued"}, status="interrupted",
                    error_message="程序关闭，排队任务未执行", finished_at=now,
                )
            else:
                if self._transition(
                    task_id, {"queued"}, status="interrupted",
                    error_message="程序关闭，任务尚未开始", finished_at=now,
                ):
                    continue
                self._transition(
                    task_id, {"running"}, status="cancellation_requested",
                    result_summary="程序正在关闭",
                )
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
