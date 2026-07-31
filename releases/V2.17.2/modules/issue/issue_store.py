"""SQLite persistence for minimal Issue lifecycle records."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .issue_model import IssueRecord, ISSUE_STATUS_CLOSED, ISSUE_STATUS_TICKETED, now_text


class IssueStore:
    """Own the independent Issue database without touching existing stores."""

    def __init__(self, db_path, busy_timeout_ms=3000):
        self.db_path = str(Path(db_path))
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    original_question TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    ticket_id TEXT,
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    final_result TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_issues_user_conversation "
                "ON issues(user_id, conversation_id, updated_at DESC)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_issues_ticket "
                "ON issues(ticket_id) WHERE ticket_id IS NOT NULL AND ticket_id != ''"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status, updated_at DESC)")
            conn.execute("PRAGMA user_version=1")

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        result = dict(row)
        try:
            steps = json.loads(result.get("steps_json") or "[]")
            result["steps_json"] = steps if isinstance(steps, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            result["steps_json"] = []
        return result

    def create(self, issue: IssueRecord):
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO issues(
                    issue_id, user_id, original_question, conversation_id,
                    status, ticket_id, steps_json, final_result, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    issue.issue_id,
                    issue.user_id,
                    issue.original_question,
                    issue.conversation_id,
                    issue.status,
                    issue.ticket_id or None,
                    json.dumps(issue.steps_json, ensure_ascii=False),
                    issue.final_result,
                    issue.created_at,
                    issue.updated_at,
                ),
            )
            row = conn.execute("SELECT * FROM issues WHERE issue_id=?", (issue.issue_id,)).fetchone()
        return self._row_to_dict(row)

    def get(self, issue_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        return self._row_to_dict(row)

    def find_recent_unlinked(self, user_id, conversation_id, since_text):
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM issues
                WHERE user_id=? AND conversation_id=? AND status=?
                  AND (ticket_id IS NULL OR ticket_id='') AND updated_at>=?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (user_id, conversation_id, "open", since_text),
            ).fetchone()
        return self._row_to_dict(row)

    def find_by_ticket_id(self, ticket_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM issues WHERE ticket_id=?", (ticket_id,)).fetchone()
        return self._row_to_dict(row)

    def link_ticket(self, issue_id, ticket_id):
        now = now_text()
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE issues SET ticket_id=?, status=?, updated_at=? WHERE issue_id=?",
                (ticket_id, ISSUE_STATUS_TICKETED, now, issue_id),
            )
            row = conn.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        return self._row_to_dict(row)

    def close_by_ticket(self, ticket_id, final_result):
        now = now_text()
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE issues SET status=?, final_result=?, updated_at=? WHERE ticket_id=?",
                (ISSUE_STATUS_CLOSED, final_result or "", now, ticket_id),
            )
            row = conn.execute("SELECT * FROM issues WHERE ticket_id=?", (ticket_id,)).fetchone()
        return self._row_to_dict(row)
