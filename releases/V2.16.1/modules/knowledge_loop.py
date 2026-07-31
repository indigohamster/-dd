"""知识闭环：回答效果、候选知识、审核、版本、评分与导出。"""

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


class KnowledgeLoopStore:
    def __init__(self, db_path, export_dir=None):
        self.db_path = str(db_path)
        self.export_dir = Path(export_dir or Path(self.db_path).parent / "knowledge_exports")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        """Commit or roll back a unit of work and always release the Windows file handle."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, question TEXT, answer TEXT,
              sources_json TEXT DEFAULT '[]', has_kb INTEGER DEFAULT 0, engine TEXT,
              outcome TEXT DEFAULT 'pending', ticket_id TEXT DEFAULT '', created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge_source_metrics (
              source_key TEXT PRIMARY KEY, title TEXT, source_kb TEXT, use_count INTEGER DEFAULT 0,
              success_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0, last_used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge_candidates (
              id TEXT PRIMARY KEY, title TEXT, problem TEXT, solution TEXT, scope TEXT DEFAULT '',
              validation TEXT DEFAULT '', fallback TEXT DEFAULT '', source_ticket_id TEXT DEFAULT '',
              source_event_id INTEGER, status TEXT DEFAULT 'draft', quality_score REAL DEFAULT 0,
              duplicate_of TEXT DEFAULT '', ima_sync_status TEXT DEFAULT 'not_synced',
              created_at TEXT, updated_at TEXT, published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS knowledge_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, version INTEGER,
              snapshot_json TEXT, action TEXT, created_at TEXT
            );
            """)

    def record_answer(self, conv_id, question, answer, sources=None, engine=""):
        now = self._now()
        sources = sources or []
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO knowledge_events(conv_id,question,answer,sources_json,has_kb,engine,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (conv_id, question, answer, json.dumps(sources, ensure_ascii=False), int(bool(sources)), engine, now, now))
            for source in sources:
                title, kb = source.get("title", ""), source.get("source_kb", "")
                key = f"{kb}|{title}"
                conn.execute("INSERT INTO knowledge_source_metrics(source_key,title,source_kb,use_count,last_used_at) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(source_key) DO UPDATE SET use_count=use_count+1,last_used_at=excluded.last_used_at",
                             (key, title, kb, 1, now))
            return cur.lastrowid

    def record_outcome(self, event_id, resolved, ticket_id=""):
        if not event_id:
            return None
        outcome = "resolved" if resolved else "unresolved"
        now = self._now()
        with self._lock, self._connection() as conn:
            event = conn.execute("SELECT * FROM knowledge_events WHERE id=?", (event_id,)).fetchone()
            if not event:
                return None
            conn.execute("UPDATE knowledge_events SET outcome=?,ticket_id=?,updated_at=? WHERE id=?",
                         (outcome, ticket_id, now, event_id))
            for source in json.loads(event["sources_json"] or "[]"):
                key = f"{source.get('source_kb','')}|{source.get('title','')}"
                field = "success_count" if resolved else "fail_count"
                conn.execute(f"UPDATE knowledge_source_metrics SET {field}={field}+1 WHERE source_key=?", (key,))
            if resolved and not event["has_kb"]:
                return self._create_candidate_locked(conn, event["question"], event["answer"], source_event_id=event_id)
        return None

    def create_from_ticket(self, ticket):
        return self.create_candidate(
            title=ticket.get("title", "已解决运维问题"), problem=ticket.get("description") or ticket.get("title", ""),
            solution=ticket.get("resolution") or "", source_ticket_id=ticket.get("ticket_id", ""),
            scope=ticket.get("category", ""), validation="由工单处理人解决，并经工单提交人确认恢复")

    def create_candidate(self, title, problem, solution, source_ticket_id="", source_event_id=None,
                         scope="", validation="", fallback=""):
        with self._lock, self._connection() as conn:
            return self._create_candidate_locked(conn, problem, solution, title, source_ticket_id,
                                                 source_event_id, scope, validation, fallback)

    def _create_candidate_locked(self, conn, problem, solution, title=None, source_ticket_id="",
                                 source_event_id=None, scope="", validation="", fallback=""):
        if source_ticket_id:
            existing = conn.execute("SELECT id FROM knowledge_candidates WHERE source_ticket_id=?", (source_ticket_id,)).fetchone()
            if existing:
                return existing["id"]
        candidate_id = self._next_id(conn)
        title = self._sanitize(title or problem[:60] or "运维知识草稿")
        duplicate = self._find_duplicate_locked(conn, title, problem)
        now = self._now()
        score = 45 + (15 if source_ticket_id else 5) - (10 if duplicate else 0)
        conn.execute("INSERT INTO knowledge_candidates(id,title,problem,solution,scope,validation,fallback,source_ticket_id,source_event_id,quality_score,duplicate_of,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (candidate_id, title, self._sanitize(problem), self._sanitize(solution), scope, validation,
                      self._sanitize(fallback), source_ticket_id, source_event_id, score, duplicate or "", now, now))
        row = conn.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
        self._save_version(conn, dict(row), "create")
        return candidate_id

    def list_candidates(self, status=None, limit=200):
        with self._connection() as conn:
            if status:
                rows = conn.execute("SELECT * FROM knowledge_candidates WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM knowledge_candidates ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_candidate(self, candidate_id):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
            return dict(row) if row else None

    def update_candidate(self, candidate_id, changes, action="edit"):
        allowed = ("title", "problem", "solution", "scope", "validation", "fallback", "status")
        with self._lock, self._connection() as conn:
            current = conn.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
            if not current:
                return None
            self._save_version(conn, dict(current), action)
            fields, values = [], []
            for key in allowed:
                if key in changes:
                    fields.append(f"{key}=?")
                    values.append(self._sanitize(changes[key]) if key != "status" else changes[key])
            fields.append("updated_at=?"); values.append(self._now()); values.append(candidate_id)
            conn.execute(f"UPDATE knowledge_candidates SET {','.join(fields)} WHERE id=?", values)
            row = conn.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
            return dict(row)

    def publish(self, candidate_id):
        candidate = self.get_candidate(candidate_id)
        if not candidate or candidate["status"] not in ("approved", "published"):
            raise ValueError("知识必须先审核通过才能发布")
        content = self.to_markdown(candidate)
        target = self.export_dir / f"{candidate_id}-{self._safe_filename(candidate['title'])}.md"
        target.write_text(content, encoding="utf-8")
        now = self._now()
        with self._lock, self._connection() as conn:
            self._save_version(conn, candidate, "publish")
            conn.execute("UPDATE knowledge_candidates SET status='published',ima_sync_status='exported',published_at=?,updated_at=? WHERE id=?", (now, now, candidate_id))
        return str(target)

    def versions(self, candidate_id):
        with self._connection() as conn:
            rows = conn.execute("SELECT id,candidate_id,version,action,created_at FROM knowledge_versions WHERE candidate_id=? ORDER BY version DESC", (candidate_id,)).fetchall()
            return [dict(row) for row in rows]

    def rollback(self, candidate_id, version):
        with self._lock, self._connection() as conn:
            current = conn.execute("SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
            saved = conn.execute("SELECT snapshot_json FROM knowledge_versions WHERE candidate_id=? AND version=?", (candidate_id, version)).fetchone()
            if not current or not saved:
                raise ValueError("知识版本不存在")
            self._save_version(conn, dict(current), "rollback")
            snapshot = json.loads(saved["snapshot_json"])
            fields = ("title", "problem", "solution", "scope", "validation", "fallback", "status", "quality_score", "duplicate_of", "ima_sync_status")
            conn.execute("UPDATE knowledge_candidates SET " + ",".join(f"{f}=?" for f in fields) + ",updated_at=? WHERE id=?",
                         [snapshot.get(f, "") for f in fields] + [self._now(), candidate_id])
        return self.get_candidate(candidate_id)

    def unresolved(self, limit=100):
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM knowledge_events WHERE outcome='unresolved' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def source_metrics(self, limit=100):
        with self._connection() as conn:
            rows = conn.execute("SELECT *,ROUND((success_count*100.0)/MAX(1,success_count+fail_count),1) success_rate FROM knowledge_source_metrics ORDER BY fail_count DESC,use_count DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def _find_duplicate_locked(self, conn, title, problem):
        rows = conn.execute("SELECT id,title,problem FROM knowledge_candidates WHERE status!='rejected'").fetchall()
        target = f"{title} {problem}"
        best_id, best_score = "", 0
        for row in rows:
            score = SequenceMatcher(None, target, f"{row['title']} {row['problem']}").ratio()
            if score > best_score:
                best_id, best_score = row["id"], score
        return best_id if best_score >= 0.62 else ""

    def _save_version(self, conn, candidate, action):
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM knowledge_versions WHERE candidate_id=?", (candidate["id"],)).fetchone()[0]
        conn.execute("INSERT INTO knowledge_versions(candidate_id,version,snapshot_json,action,created_at) VALUES(?,?,?,?,?)",
                     (candidate["id"], version, json.dumps(candidate, ensure_ascii=False), action, self._now()))

    def _next_id(self, conn):
        prefix = datetime.now().strftime("KBC-%Y%m%d-")
        row = conn.execute("SELECT id FROM knowledge_candidates WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",)).fetchone()
        seq = int(row["id"].split("-")[-1]) + 1 if row else 1
        return f"{prefix}{seq:03d}"

    @staticmethod
    def _sanitize(value):
        text = str(value or "")
        text = re.sub(r"(?i)(api[_ -]?key|token|password|密码)\s*[:=：]\s*\S+", r"\1: [已脱敏]", text)
        text = re.sub(r"1[3-9]\d{9}", "[手机号已脱敏]", text)
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已脱敏]", text)
        return text

    @staticmethod
    def _safe_filename(value):
        return re.sub(r'[<>:"/\\|?*]+', "_", value)[:60]

    @staticmethod
    def to_markdown(candidate):
        return (f"# {candidate['title']}\n\n## 适用范围\n{candidate['scope'] or '待补充'}\n\n"
                f"## 问题现象\n{candidate['problem']}\n\n## 处理步骤\n{candidate['solution']}\n\n"
                f"## 验证方式\n{candidate['validation'] or '确认问题已恢复'}\n\n## 无效时\n{candidate['fallback'] or '转人工进一步处理'}\n\n"
                f"---\n来源工单：{candidate['source_ticket_id'] or '无'}\n知识编号：{candidate['id']}\n")

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
