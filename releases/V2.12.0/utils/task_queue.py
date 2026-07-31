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
            current = self.get_task(task_id)
            if current and current["status"] == "cancelled":
                return
            self._update(task_id, status="running", progress=5, started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            try:
                summary = func(progress) or "任务完成"
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
            finally:
                with self._lock:
                    self._futures.pop(task_id, None)

        future = self._executor.submit(runner)
        with self._lock:
            self._futures[task_id] = future
        return self.get_task(task_id)

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
                    self._futures.pop(task_id, None)
                self._update(task_id, status="cancelled", finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            else:
                self._update(task_id, status="cancellation_requested", result_summary="正在安全停止任务")
        return True

    def shutdown(self, wait=False):
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
