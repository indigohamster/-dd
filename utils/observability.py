"""Local observability event and notification delivery store."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


class ObservabilityStore:
    def __init__(self, db_path, retention_days=30):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.cleanup(retention_days)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS operation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
                    duration_ms REAL NOT NULL DEFAULT 0, actor_id TEXT DEFAULT '',
                    detail TEXT DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON operation_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_status_created ON operation_events(status,created_at DESC);
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, recipient TEXT NOT NULL,
                    action TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL, error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_status_updated ON notification_deliveries(status,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_delivery_ticket ON notification_deliveries(ticket_id,created_at DESC);
            """)

    def record_event(self, category, operation, status="success", duration_ms=0, actor_id="", detail=""):
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO operation_events(category,operation,status,duration_ms,actor_id,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                (category, operation, status, round(float(duration_ms), 2), actor_id, str(detail)[:1000], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

    def list_events(self, status="", category="", limit=100):
        clauses, params = [], []
        if status: clauses.append("status=?"); params.append(status)
        if category: clauses.append("category=?"); params.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM operation_events{where} ORDER BY created_at DESC,id DESC LIMIT ?", params,
            ).fetchall()]

    def create_delivery(self, ticket, recipient, action, operator_nick="", detail=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = json.dumps({"ticket": ticket, "action": action, "operator_nick": operator_nick, "detail": detail}, ensure_ascii=False)
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO notification_deliveries(ticket_id,recipient,action,status,payload_json,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?)",
                (ticket.get("ticket_id", ""), recipient, action, payload, now, now),
            )
            return cursor.lastrowid

    def update_delivery(self, delivery_id, status, error_message=""):
        with self._connection() as conn:
            conn.execute(
                "UPDATE notification_deliveries SET status=?,attempts=attempts+1,error_message=?,updated_at=? WHERE id=?",
                (status, str(error_message)[:1000], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), delivery_id),
            )

    def claim_delivery_for_retry(self, delivery_id):
        """Atomically reserve a failed delivery so repeated clicks cannot send duplicates."""
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE notification_deliveries SET status='retrying',error_message='',updated_at=? "
                "WHERE id=? AND status='failed'",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), delivery_id),
            )
            return cursor.rowcount == 1

    def get_delivery(self, delivery_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)).fetchone()
            return dict(row) if row else None

    def list_deliveries(self, status="", limit=100):
        with self._connection() as conn:
            if status:
                rows = conn.execute("SELECT * FROM notification_deliveries WHERE status=? ORDER BY updated_at DESC,id DESC LIMIT ?", (status, max(1,min(int(limit),500)))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM notification_deliveries ORDER BY updated_at DESC,id DESC LIMIT ?", (max(1,min(int(limit),500)),)).fetchall()
            return [dict(row) for row in rows]

    def summary(self):
        today = datetime.now().strftime("%Y-%m-%d")
        start = f"{today} 00:00:00"
        with self._connection() as conn:
            events = conn.execute("SELECT COUNT(*),SUM(status='error'),COALESCE(AVG(NULLIF(duration_ms,0)),0) FROM operation_events WHERE created_at>=?", (start,)).fetchone()
            deliveries = conn.execute(
                "SELECT COUNT(*),SUM(status='sent'),SUM(status='failed'),"
                "SUM(status IN ('pending','retrying')) FROM notification_deliveries WHERE created_at>=?",
                (start,),
            ).fetchone()
        return {"events_today": events[0] or 0, "errors_today": events[1] or 0, "avg_duration_ms": round(events[2] or 0, 1),
                "notifications_today": deliveries[0] or 0, "notifications_sent": deliveries[1] or 0,
                "notifications_failed": deliveries[2] or 0, "notifications_pending": deliveries[3] or 0}

    def cleanup(self, retention_days=30):
        cutoff = (datetime.now() - timedelta(days=max(1, int(retention_days)))).strftime("%Y-%m-%d %H:%M:%S")
        with self._connection() as conn:
            conn.execute("DELETE FROM operation_events WHERE created_at<?", (cutoff,))
            conn.execute("DELETE FROM notification_deliveries WHERE created_at<?", (cutoff,))

    @staticmethod
    def decode_payload(delivery):
        return json.loads(delivery["payload_json"])
