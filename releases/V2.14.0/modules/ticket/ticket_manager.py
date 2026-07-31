# ============================================================
# 工单管理器 — SQLite 本地存储，零外部依赖
# ============================================================

import sqlite3
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

# 数据库文件路径（项目根目录下的 data/ 目录）
DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "tickets.db")
SCHEMA_VERSION = 2

# 工单状态枚举
STATUS_OPEN = "open"
STATUS_ASSIGNED = "assigned"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"

STATUS_LABELS = {
    STATUS_OPEN: "待处理",
    STATUS_ASSIGNED: "已分配",
    STATUS_IN_PROGRESS: "处理中",
    STATUS_RESOLVED: "已解决",
    STATUS_CLOSED: "已关闭",
}

ALLOWED_STATUS_TRANSITIONS = {
    STATUS_OPEN: {STATUS_ASSIGNED, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_ASSIGNED: {STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_IN_PROGRESS: {STATUS_ASSIGNED, STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_RESOLVED: {STATUS_IN_PROGRESS, STATUS_CLOSED},
    STATUS_CLOSED: set(),
}

# 工单优先级枚举（数值越小越紧急）
PRIORITY_URGENT = "urgent"   # 🔴 影响核心业务
PRIORITY_HIGH   = "high"      # 🟠 影响部分业务
PRIORITY_MEDIUM = "medium"    # 🟡 影响单个用户
PRIORITY_LOW    = "low"       # ⚪ 咨询建议

PRIORITY_ORDER = {
    PRIORITY_URGENT: 0,
    PRIORITY_HIGH:   1,
    PRIORITY_MEDIUM: 2,
    PRIORITY_LOW:    3,
}

PRIORITY_LABELS = {
    PRIORITY_URGENT: "🔴 紧急",
    PRIORITY_HIGH:   "🟠 高",
    PRIORITY_MEDIUM: "🟡 中",
    PRIORITY_LOW:    "⚪ 低",
}

PRIORITY_OVERDUE_HOURS = {
    PRIORITY_URGENT: 2,    # 紧急 → 2小时
    PRIORITY_HIGH:   8,    # 高   → 8小时
    PRIORITY_MEDIUM: 24,   # 中   → 24小时
    PRIORITY_LOW:    72,   # 低   → 72小时（3天）
}
CATEGORIES = {
    "设备问题": ["硬件", "电脑", "打印机", "显示器", "键盘", "鼠标", "服务器", "硬盘",
                 "内存", "网络", "vpn", "wifi", "网线", "断网", "ip", "路由器", "交换机"],
    "U9问题":   ["u9", "erp", "料号", "凭证", "财务系统", "账套", "采购", "供应链"],
    "CRM问题":  ["crm", "客户", "售后", "服务单", "工单回访"],
    "软件问题": ["软件", "系统", "安装", "蓝屏", "卡死", "office", "wps", "钉钉", "浏览器",
                 "账号", "密码", "权限", "登录", "重置"],
    "其他":     [],
}

# IMA 知识库 ID → 工单分类 映射（基于 KB 匹配更精准）
KB_ID_TO_CATEGORY: dict[str, str] = {
    "Sr51b8h0cPdLCfawTU-BRYR6kUv6k-Fccsobv0IJz2Y=":  "U9问题",
    "-1urpedNGasp5vQigMVxkNjP6f3tgMuxXrWascZ4hno=": "CRM问题",
    "u1392jxIoa7HU80baSTqkSf_x5xMxrnolYl9-4iDcs8=": "设备问题",
    "MelAa21HiFlWZsoxZMGgUVfnS_KQCrE_U8j282U-wg0=": "设备问题",
    "3TBb5Uvahg8jsNNuSA8RpDB6Qx2gleLUEHVWG1RfbnM=": "设备问题",
}

# KB 名称关键词 → 工单分类（名称匹配兜底）
KB_NAME_TO_CATEGORY: dict[str, str] = {
    "u9":    "U9问题",
    "售后":   "CRM问题",
    "硬件":   "设备问题",
    "网络":   "设备问题",
}


class TicketManager:
    """本地 SQLite 工单管理器，线程安全。"""

    def __init__(self, db_path: str = DB_PATH, logger=None,
                 backup_enabled: bool = False, backup_dir: str = None,
                 backup_retention_days: int = 30,
                 integrity_check: bool = True,
                 busy_timeout_ms: int = 5000,
                 followup_delay_hours: int = 24):
        self.db_path = os.path.abspath(db_path)
        self.logger = logger
        self.backup_enabled = backup_enabled
        self.backup_dir = os.path.abspath(
            backup_dir or os.path.join(os.path.dirname(self.db_path), "backups")
        )
        self.backup_retention_days = max(1, int(backup_retention_days))
        self.integrity_check_enabled = integrity_check
        self.busy_timeout_ms = max(1000, int(busy_timeout_ms))
        self.followup_delay_hours = max(1, int(followup_delay_hours))
        self._lock = threading.RLock()
        self._init_db()
        if self.integrity_check_enabled:
            self.check_integrity()
        if self.backup_enabled:
            self.backup_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self):
        """初始化数据库和表结构"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS tickets (
                        ticket_id       TEXT PRIMARY KEY,
                        conv_id         TEXT NOT NULL,
                        conv_title      TEXT DEFAULT '',
                        creator_id      TEXT NOT NULL,
                        creator_nick    TEXT NOT NULL,
                        creator_staff_id TEXT DEFAULT '',
                        category        TEXT DEFAULT 'OTHER',
                        title           TEXT NOT NULL,
                        description     TEXT DEFAULT '',
                        priority        TEXT DEFAULT 'medium',
                        status          TEXT DEFAULT 'open',
                        assignee_id     TEXT DEFAULT '',
                        assignee_nick   TEXT DEFAULT '',
                        resolver_id     TEXT DEFAULT '',
                        resolver_nick   TEXT DEFAULT '',
                        resolution      TEXT DEFAULT '',
                        source_qa_id    TEXT DEFAULT '',
                        is_invalid      INTEGER DEFAULT 0,
                        is_experience   INTEGER DEFAULT 0,
                        created_at      TEXT NOT NULL,
                        updated_at      TEXT NOT NULL,
                        resolved_at     TEXT DEFAULT NULL
                    );

                    CREATE TABLE IF NOT EXISTS ticket_log (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticket_id       TEXT NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
                        action          TEXT NOT NULL,
                        operator_id     TEXT NOT NULL,
                        operator_nick   TEXT NOT NULL,
                        detail          TEXT DEFAULT '',
                        created_at      TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS admins (
                        user_id     TEXT PRIMARY KEY,
                        user_nick   TEXT NOT NULL,
                        staff_id    TEXT DEFAULT '',
                        role        TEXT DEFAULT 'admin',
                        added_at    TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key         TEXT PRIMARY KEY,
                        value       TEXT NOT NULL,
                        updated_at  TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_tickets_creator
                        ON tickets(creator_id);
                    CREATE INDEX IF NOT EXISTS idx_tickets_status
                        ON tickets(status);
                    CREATE INDEX IF NOT EXISTS idx_tickets_conv
                        ON tickets(conv_id);
                    CREATE INDEX IF NOT EXISTS idx_ticket_log_ticket
                        ON ticket_log(ticket_id);
                    CREATE INDEX IF NOT EXISTS idx_ticket_log_action_created
                        ON ticket_log(action, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_ticket_log_operator_created
                        ON ticket_log(operator_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_tickets_creator_created
                        ON tickets(creator_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_tickets_status_created
                        ON tickets(status, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_tickets_conv_status_updated
                        ON tickets(conv_id, status, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_tickets_created
                        ON tickets(created_at);
                    CREATE INDEX IF NOT EXISTS idx_tickets_resolved
                        ON tickets(resolved_at);
                    CREATE INDEX IF NOT EXISTS idx_tickets_staff
                        ON tickets(creator_staff_id);
                    CREATE INDEX IF NOT EXISTS idx_tickets_assignee
                        ON tickets(assignee_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_tickets_category
                        ON tickets(category, created_at DESC);
                """)
                conn.commit()
                # 对已有表补充新字段（兼容旧数据库）
                self._migrate_columns(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_followup "
                    "ON tickets(status, followup_due_at)"
                )
                conn.execute(
                    "INSERT INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (str(SCHEMA_VERSION), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
            finally:
                conn.close()

    def check_integrity(self) -> bool:
        """执行 SQLite 完整性检查；损坏时拒绝继续启动。"""
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
                ok = bool(result and result[0] == "ok")
                if not ok:
                    raise RuntimeError(f"工单数据库完整性检查失败: {result}")
                if self.logger:
                    self.logger.info(f"工单数据库完整性检查通过: {self.db_path}")
                return True
            finally:
                conn.close()

    def get_schema_version(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()

    def backup_database(self, force: bool = False) -> str:
        """使用 SQLite 在线备份 API 创建每日一致性备份。"""
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        backup_path = os.path.join(self.backup_dir, f"tickets-{stamp}.db")
        if os.path.exists(backup_path) and not force:
            self._cleanup_old_backups()
            return backup_path

        with self._lock:
            source = self._connect()
            target = sqlite3.connect(backup_path)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        self._cleanup_old_backups()
        if self.logger:
            self.logger.info(f"工单数据库备份完成: {backup_path}")
        return backup_path

    def _cleanup_old_backups(self):
        cutoff = datetime.now() - timedelta(days=self.backup_retention_days)
        if not os.path.isdir(self.backup_dir):
            return
        for name in os.listdir(self.backup_dir):
            if not name.startswith("tickets-") or not name.endswith(".db"):
                continue
            path = os.path.join(self.backup_dir, name)
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    os.remove(path)
            except OSError as exc:
                if self.logger:
                    self.logger.warning(f"清理旧数据库备份失败: {path}: {exc}")

    def _migrate_columns(self, conn):
        """对已有 tickets / admins 表补充新字段（兼容旧数据库）"""
        # --- tickets 表迁移 ---
        cursor = conn.execute("PRAGMA table_info(tickets)")
        existing = {row[1] for row in cursor.fetchall()}
        ticket_migrations = [
            ("resolver_id",       "TEXT DEFAULT ''"),
            ("resolver_nick",     "TEXT DEFAULT ''"),
            ("is_invalid",        "INTEGER DEFAULT 0"),
            ("is_experience",     "INTEGER DEFAULT 0"),
            ("creator_staff_id",  "TEXT DEFAULT ''"),
            ("followup_due_at",   "TEXT DEFAULT NULL"),
            ("followup_sent_at",  "TEXT DEFAULT NULL"),
            ("user_confirmation", "TEXT DEFAULT ''"),
        ]
        for col_name, col_def in ticket_migrations:
            if col_name not in existing:
                try:
                    conn.execute(
                        f"ALTER TABLE tickets ADD COLUMN {col_name} {col_def}"
                    )
                    if self.logger:
                        self.logger.info(f"DB迁移: tickets 表新增字段 {col_name}")
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"DB迁移失败({col_name}): {e}")

        # 迁移：将旧的 'normal' 优先级统一为 'medium'
        try:
            result = conn.execute(
                "UPDATE tickets SET priority='medium' WHERE priority='normal'"
            )
            conn.commit()
            if result.rowcount > 0 and self.logger:
                self.logger.info(f"DB迁移: {result.rowcount} 条工单 priority='normal' → 'medium'")
        except Exception as e:
            if self.logger:
                self.logger.warning(f"DB迁移失败(normal->medium): {e}")

        # --- admins 表迁移 ---
        cursor = conn.execute("PRAGMA table_info(admins)")
        admin_existing = {row[1] for row in cursor.fetchall()}
        admin_migrations = [
            ("staff_id", "TEXT DEFAULT ''"),
        ]
        for col_name, col_def in admin_migrations:
            if col_name not in admin_existing:
                try:
                    conn.execute(
                        f"ALTER TABLE admins ADD COLUMN {col_name} {col_def}"
                    )
                    if self.logger:
                        self.logger.info(f"DB迁移: admins 表新增字段 {col_name}")
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"DB迁移失败({col_name}): {e}")

    # ------------------------------------------------------------------
    # 工单 CRUD
    # ------------------------------------------------------------------

    def _generate_ticket_id(self) -> str:
        """生成工单编号: TKT-YYYYMMDD-NNN"""
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"TKT-{today}-"
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT ticket_id FROM tickets WHERE ticket_id LIKE ? ORDER BY ticket_id DESC LIMIT 1",
                    (f"{prefix}%",),
                ).fetchone()
                if row:
                    last_num = int(row[0].split("-")[-1])
                    num = last_num + 1
                else:
                    num = 1
                return f"{prefix}{num:03d}"
            finally:
                conn.close()

    def create_ticket(
        self,
        conv_id: str,
        conv_title: str,
        creator_id: str,
        creator_nick: str,
        title: str,
        description: str = "",
        category: str = "OTHER",
        priority: str = "medium",
        creator_staff_id: str = "",
    ) -> dict:
        """创建工单，返回工单字典"""
        ticket_id = self._generate_ticket_id()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO tickets (ticket_id, conv_id, conv_title, creator_id, creator_nick,
                       creator_staff_id, category, title, description, priority, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ticket_id, conv_id, conv_title, creator_id, creator_nick,
                     creator_staff_id, category, title, description, priority, STATUS_OPEN, now, now),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, 'create', ?, ?, ?, ?)",
                    (ticket_id, creator_id, creator_nick, f"创建工单: {title}", now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._row_to_dict(row)
            finally:
                conn.close()

    def get_ticket(self, ticket_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_tickets_by_user(self, user_id: str, status: str = None, limit: int = 20) -> list:
        """查询某用户创建的工单列表"""
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                if status:
                    rows = conn.execute(
                        "SELECT * FROM tickets WHERE creator_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
                        (user_id, status, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM tickets WHERE creator_id=? ORDER BY created_at DESC LIMIT ?",
                        (user_id, limit),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def list_tickets_by_status(self, status: str = None, limit: int = 50) -> list:
        """按状态查询工单列表（管理员用）"""
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                if status:
                    rows = conn.execute(
                        "SELECT * FROM tickets WHERE status=? ORDER BY "
                        "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def list_all_tickets(self, filter_type: str = "all", limit: int = 100) -> list:
        """
        查询全部工单（管理员用）。

        filter_type:
            "all"     — 所有工单（按创建时间倒序）
            "pending" — 未处理/处理中（open / assigned / in_progress）
            "done"    — 已处理（resolved / closed）
        """
        PENDING_STATUSES = ("open", "assigned", "in_progress")
        DONE_STATUSES = ("resolved", "closed")

        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                if filter_type == "pending":
                    placeholders = ",".join("?" * len(PENDING_STATUSES))
                    rows = conn.execute(
                        f"SELECT * FROM tickets WHERE status IN ({placeholders}) "
                        f"ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
                        f"created_at DESC LIMIT ?",
                        (*PENDING_STATUSES, limit),
                    ).fetchall()
                elif filter_type == "done":
                    placeholders = ",".join("?" * len(DONE_STATUSES))
                    rows = conn.execute(
                        f"SELECT * FROM tickets WHERE status IN ({placeholders}) "
                        f"ORDER BY created_at DESC LIMIT ?",
                        (*DONE_STATUSES, limit),
                    ).fetchall()
                else:  # "all"
                    rows = conn.execute(
                        "SELECT * FROM tickets ORDER BY "
                        "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
                        "created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_ticket_logs(self, ticket_id: str, limit: int = 20) -> list:
        """获取工单操作日志"""
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM ticket_log WHERE ticket_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                    (ticket_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def search_tickets_exact(self, field: str, value: str, limit: int = 100) -> list:
        """按白名单字段精确匹配工单。"""
        allowed = {"ticket_id", "creator_id", "creator_nick", "creator_staff_id", "status",
                   "priority", "category", "assignee_id", "assignee_nick", "title", "conv_id"}
        if field not in allowed:
            raise ValueError("不支持的精确搜索字段")
        value = str(value or "").strip()
        if not value:
            return []
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT * FROM tickets WHERE {field}=? ORDER BY created_at DESC LIMIT ?",
                    (value, max(1, min(int(limit), 500))),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def search_history(self, ticket_id="", action="", operator_id="", date_from="", date_to="", limit=200) -> list:
        """按工单、动作、操作人和日期范围查询操作历史。"""
        clauses, params = [], []
        for column, value in (("ticket_id", ticket_id), ("action", action), ("operator_id", operator_id)):
            value = str(value or "").strip()
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if date_from:
            clauses.append("created_at>=?")
            params.append(f"{date_from} 00:00:00" if len(date_from) == 10 else date_from)
        if date_to:
            end = datetime.strptime(date_to[:10], "%Y-%m-%d") + timedelta(days=1)
            clauses.append("created_at<?")
            params.append(end.strftime("%Y-%m-%d 00:00:00"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT * FROM ticket_log{where} ORDER BY created_at DESC,id DESC LIMIT ?", params,
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def import_tickets(self, records: list, operator_id="web-admin", operator_nick="管理后台") -> dict:
        """事务化导入工单；重复编号跳过，任何系统异常整批回滚。"""
        valid_status = {"open", "assigned", "in_progress", "resolved", "closed"}
        valid_priority = {"low", "medium", "high", "urgent"}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        imported, skipped, errors = 0, 0, []
        with self._lock:
            conn = self._connect()
            try:
                for index, raw in enumerate(records, start=2):
                    title = str(raw.get("title") or "").strip()
                    if not title:
                        errors.append(f"第 {index} 行缺少 title")
                        continue
                    ticket_id = str(raw.get("ticket_id") or f"TKT-IMP-{uuid.uuid4().hex[:10].upper()}").strip()
                    if conn.execute("SELECT 1 FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone():
                        skipped += 1
                        continue
                    status, priority = str(raw.get("status") or "open").strip(), str(raw.get("priority") or "medium").strip()
                    if status not in valid_status or priority not in valid_priority:
                        errors.append(f"第 {index} 行状态或优先级无效")
                        continue
                    created_at = str(raw.get("created_at") or now).strip()
                    values = (ticket_id, str(raw.get("conv_id") or "import"), str(raw.get("conv_title") or "导入"),
                              str(raw.get("creator_id") or "import"), str(raw.get("creator_nick") or "导入用户"),
                              str(raw.get("creator_staff_id") or ""), str(raw.get("category") or "OTHER"), title,
                              str(raw.get("description") or ""), priority, status, str(raw.get("assignee_id") or ""),
                              str(raw.get("assignee_nick") or ""), str(raw.get("resolution") or ""), created_at,
                              str(raw.get("updated_at") or created_at), raw.get("resolved_at") or None)
                    conn.execute("""INSERT INTO tickets (
                        ticket_id,conv_id,conv_title,creator_id,creator_nick,creator_staff_id,category,title,
                        description,priority,status,assignee_id,assignee_nick,resolution,created_at,updated_at,resolved_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                    conn.execute("INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,created_at) "
                                 "VALUES(?,'import',?,?,?,?)",
                                 (ticket_id, operator_id, operator_nick, "从管理后台批量导入", now))
                    imported += 1
                conn.commit()
                return {"imported": imported, "skipped": skipped, "errors": errors[:20]}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 工单操作
    # ------------------------------------------------------------------

    def add_comment(self, ticket_id: str, detail: str, operator_id: str = "",
                    operator_nick: str = "") -> Optional[dict]:
        """添加处理记录但不改变工单状态。"""
        detail = str(detail or "").strip()
        if not detail:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                if not conn.execute("SELECT 1 FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone():
                    return None
                conn.execute("UPDATE tickets SET updated_at=? WHERE ticket_id=?", (now, ticket_id))
                conn.execute(
                    "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,created_at) "
                    "VALUES(?,'comment',?,?,?,?)",
                    (ticket_id, operator_id, operator_nick, detail, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def update_priority(self, ticket_id: str, priority: str) -> Optional[dict]:
        """更新工单优先级"""
        if priority not in PRIORITY_ORDER:
            if self.logger:
                self.logger.warning(f"无效优先级: {priority}")
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                exists = conn.execute(
                    "SELECT 1 FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not exists:
                    return None
                conn.execute(
                    "UPDATE tickets SET priority=?, updated_at=? WHERE ticket_id=?",
                    (priority, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, 'priority_change', '', 'system', ?, ?)",
                    (ticket_id, f"优先级变更为 {PRIORITY_LABELS.get(priority, priority)}", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_overdue_tickets(self) -> list:
        """获取逾期未处理的工单（根据优先级对应的超时阈值）"""
        now = datetime.now()
        pending = self.list_all_tickets(filter_type="pending")
        overdue = []
        for t in pending:
            created = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
            hours = (now - created).total_seconds() / 3600
            threshold = PRIORITY_OVERDUE_HOURS.get(t.get("priority", "medium"), 24)
            if hours >= threshold:
                overdue.append({**t, "overdue_hours": round(hours, 1)})
        return overdue

    def update_description(self, ticket_id: str, description: str) -> Optional[dict]:
        """更新工单描述（用于追加附件信息等）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE tickets SET description=?, updated_at=? WHERE ticket_id=?",
                    (description, now, ticket_id),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def search_similar_tickets(self, keywords: list, limit: int = 5) -> list:
        """根据关键词搜索相似的已解决工单"""
        if not keywords:
            return []
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                # 构建模糊匹配条件
                conditions = []
                params = []
                for kw in keywords[:5]:  # 最多5个关键词
                    conditions.append("(title LIKE ? OR description LIKE ?)")
                    params.extend([f"%{kw}%", f"%{kw}%"])
                where = " AND ".join(conditions)
                # 只搜索已解决/已关闭的工单
                sql = (
                    f"SELECT * FROM tickets WHERE status IN ('resolved', 'closed') "
                    f"AND {where} "
                    f"ORDER BY created_at DESC LIMIT ?"
                )
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def find_related_active_tickets(self, title: str, category: str,
                                    exclude_ticket_id: str = "",
                                    hours: int = 8, limit: int = 10) -> list:
        """查找近期相似活跃工单，用于重复报障和群体故障识别。"""
        cutoff = (datetime.now() - timedelta(hours=max(1, hours))).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM tickets WHERE category=? AND status IN (?, ?, ?) "
                    "AND created_at>=? AND ticket_id<>? ORDER BY created_at DESC LIMIT 100",
                    (category, STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS,
                     cutoff, exclude_ticket_id),
                ).fetchall()
            finally:
                conn.close()

        source_tokens = self._similarity_tokens(title)
        related = []
        for row in rows:
            ticket = dict(row)
            target_tokens = self._similarity_tokens(ticket.get("title", ""))
            union = source_tokens | target_tokens
            score = len(source_tokens & target_tokens) / len(union) if union else 0
            # 中文短标题的双字片段集合较小，0.18 可命中同一设备/现象，
            # 同时仍要求分类一致且处于近期活跃窗口内以降低误报。
            if score >= 0.18:
                ticket["similarity"] = round(score, 2)
                related.append(ticket)
        related.sort(key=lambda item: item["similarity"], reverse=True)
        return related[:limit]

    @staticmethod
    def _similarity_tokens(text: str) -> set:
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (text or "").lower())
        if len(normalized) < 2:
            return {normalized} if normalized else set()
        return {normalized[i:i + 2] for i in range(len(normalized) - 1)}

    def update_status(self, ticket_id: str, new_status: str, operator_id: str,
                      operator_nick: str, detail: str = "") -> Optional[dict]:
        """更新工单状态"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if new_status == current[0]:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
                    ).fetchone()
                    return dict(row)
                if new_status not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    if self.logger:
                        self.logger.warning(
                            f"拒绝非法工单状态流转: {ticket_id} {current[0]} -> {new_status}"
                        )
                    return None
                if new_status in (STATUS_RESOLVED, STATUS_CLOSED):
                    conn.execute(
                        "UPDATE tickets SET status=?, updated_at=?, resolved_at=? WHERE ticket_id=?",
                        (new_status, now, now, ticket_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tickets SET status=?, updated_at=? WHERE ticket_id=?",
                        (new_status, now, ticket_id),
                    )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_id, new_status, operator_id, operator_nick, detail, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def assign_ticket(self, ticket_id: str, assignee_id: str, assignee_nick: str,
                      operator_id: str, operator_nick: str) -> Optional[dict]:
        """分配工单"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if STATUS_ASSIGNED not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET assignee_id=?, assignee_nick=?, status=?, updated_at=? WHERE ticket_id=?",
                    (assignee_id, assignee_nick, STATUS_ASSIGNED, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, 'assign', ?, ?, ?, ?)",
                    (ticket_id, operator_id, operator_nick,
                     f"分配给 {assignee_nick}({assignee_id})", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def resolve_ticket(self, ticket_id: str, resolution: str, operator_id: str,
                       operator_nick: str) -> Optional[dict]:
        """解决工单（填写解决方案，记录处理人）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        followup_due = (datetime.now() + timedelta(hours=self.followup_delay_hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if STATUS_RESOLVED not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET status=?, resolution=?, resolver_id=?, resolver_nick=?, updated_at=?, "
                    "resolved_at=?, followup_due_at=?, followup_sent_at=NULL, user_confirmation='' WHERE ticket_id=?",
                    (STATUS_RESOLVED, resolution, operator_id, operator_nick, now, now, followup_due, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_id, "resolve", operator_id, operator_nick, resolution, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_due_followups(self, limit: int = 50) -> list:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM tickets WHERE status=? AND followup_due_at IS NOT NULL "
                    "AND followup_due_at<=? AND followup_sent_at IS NULL ORDER BY followup_due_at LIMIT ?",
                    (STATUS_RESOLVED, now, limit),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def mark_followup_sent(self, ticket_id: str) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    "UPDATE tickets SET followup_sent_at=? WHERE ticket_id=? AND followup_sent_at IS NULL",
                    (now, ticket_id),
                )
                conn.commit()
                return result.rowcount > 0
            finally:
                conn.close()

    def reopen_ticket(self, ticket_id: str, operator_id: str, operator_nick: str,
                      detail: str = "用户确认问题未解决") -> Optional[dict]:
        ticket = self.update_status(
            ticket_id, STATUS_IN_PROGRESS, operator_id, operator_nick, detail
        )
        if not ticket:
            return None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE tickets SET user_confirmation='reopened', followup_due_at=NULL WHERE ticket_id=?",
                    (ticket_id,),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_ticket(ticket_id)

    def close_ticket(self, ticket_id: str, operator_id: str, operator_nick: str,
                     detail: str = "") -> Optional[dict]:
        """关闭工单（记录处理人）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if STATUS_CLOSED not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET status=?, resolver_id=?, resolver_nick=?, updated_at=?, resolved_at=? WHERE ticket_id=?",
                    (STATUS_CLOSED, operator_id, operator_nick, now, now, ticket_id),
                )
                comment = detail or "工单已关闭"
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, 'close', ?, ?, ?, ?)",
                    (ticket_id, operator_id, operator_nick, comment, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def set_in_progress(self, ticket_id: str, operator_id: str = "",
                        operator_nick: str = "", detail: str = "") -> Optional[dict]:
        """将工单状态设置为「处理中」"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if current[0] == STATUS_IN_PROGRESS:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
                    ).fetchone()
                    return dict(row)
                if STATUS_IN_PROGRESS not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET status=?, updated_at=? WHERE ticket_id=?",
                    (STATUS_IN_PROGRESS, now, ticket_id),
                )
                comment = detail or "用户反馈不满意，升级为处理中"
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_id, "in_progress", operator_id, operator_nick, comment, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def archive_as_experience(self, ticket_id: str, operator_id: str = "",
                               operator_nick: str = "", detail: str = "") -> Optional[dict]:
        """将工单归档为经验（is_experience=1，status → closed，表示彻底完结）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if current[0] != STATUS_CLOSED and STATUS_CLOSED not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET is_experience=1, status=?, updated_at=?, resolved_at=? WHERE ticket_id=?",
                    (STATUS_CLOSED, now, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, 'archive', ?, ?, ?, ?)",
                    (ticket_id, operator_id, operator_nick, detail or "归档为经验", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def mark_as_invalid(self, ticket_id: str, operator_id: str = "",
                         operator_nick: str = "", detail: str = "") -> Optional[dict]:
        """标记工单为无效工单（is_invalid=1，不归档为经验）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if current[0] != STATUS_CLOSED and STATUS_CLOSED not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET is_invalid=1, status=?, updated_at=?, resolved_at=? WHERE ticket_id=?",
                    (STATUS_CLOSED, now, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, created_at)"
                    " VALUES (?, 'mark_invalid', ?, ?, ?, ?)",
                    (ticket_id, operator_id, operator_nick, detail or "标记为无效工单", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def find_active_ticket_by_conv(self, conv_id: str) -> Optional[dict]:
        """
        查找某个会话对应的活跃工单（未关闭/已解决的）。
        返回最新的一条，没有则返回 None。
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM tickets WHERE conv_id=? AND status NOT IN (?, ?) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (conv_id, STATUS_RESOLVED, STATUS_CLOSED),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_stats(self, since: str = None) -> dict:
        """获取工单统计数据"""
        with self._lock:
            conn = self._connect()
            try:
                base = "SELECT COUNT(*), status FROM tickets"
                params = ()
                if since:
                    base = "SELECT COUNT(*), status FROM tickets WHERE created_at >= ?"
                    params = (since,)

                rows = conn.execute(f"{base} GROUP BY status", params).fetchall()
                status_counts = {row[1]: row[0] for row in rows}

                total = sum(status_counts.values())
                resolved = status_counts.get(STATUS_RESOLVED, 0) + status_counts.get(STATUS_CLOSED, 0)
                open_count = sum(
                    v for k, v in status_counts.items()
                    if k in (STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS)
                )

                # 按分类统计
                if since:
                    cat_rows = conn.execute(
                        "SELECT category, COUNT(*) FROM tickets WHERE created_at>=? GROUP BY category",
                        (since,),
                    ).fetchall()
                else:
                    cat_rows = conn.execute(
                        "SELECT category, COUNT(*) FROM tickets GROUP BY category"
                    ).fetchall()
                category_counts = {row[0]: row[1] for row in cat_rows}

                return {
                    "total": total,
                    "open": open_count,
                    "resolved": resolved,
                    "by_status": status_counts,
                    "by_category": category_counts,
                    "new_today": sum(
                        v for k, v in status_counts.items()
                        if k in (STATUS_OPEN, STATUS_ASSIGNED)
                    ),
                }
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 管理员
    # ------------------------------------------------------------------

    def is_admin(self, user_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
                return row is not None
            finally:
                conn.close()

    def add_admin(self, user_id: str, user_nick: str, operator_id: str = "", staff_id: str = ""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO admins (user_id, user_nick, staff_id, added_at) VALUES (?, ?, ?, ?)",
                    (user_id, user_nick, staff_id, now),
                )
                conn.commit()
                if self.logger:
                    self.logger.info(f"添加管理员: {user_nick}({user_id}) staff_id={staff_id or '空'}")
            finally:
                conn.close()

    def list_admins(self) -> list:
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM admins ORDER BY added_at").fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def classify(content: str) -> str:
        """根据内容关键词兜底分类"""
        content_lower = content.lower()
        for cat, keywords in CATEGORIES.items():
            if cat == "其他":
                continue
            for kw in keywords:
                if kw in content_lower:
                    return cat
        return "其他"

    @staticmethod
    def classify_by_kb(search_results: list) -> str:
        """
        根据 IMA 知识库搜索结果自动分类。
        返回匹配度最高的分类，无匹配时返回 '其他'。
        """
        if not search_results:
            return "其他"

        # 统计每个分类的命中次数
        cat_scores: dict[str, int] = {}
        for item in search_results:
            kb_id = item.get("source_kb_id", "")
            kb_name = item.get("source_kb", "")

            # 精准匹配 KB_ID
            if kb_id and kb_id in KB_ID_TO_CATEGORY:
                cat = KB_ID_TO_CATEGORY[kb_id]
                cat_scores[cat] = cat_scores.get(cat, 0) + 1
                continue

            # 名称关键词匹配
            name_lower = kb_name.lower()
            for keyword, cat in KB_NAME_TO_CATEGORY.items():
                if keyword in name_lower:
                    cat_scores[cat] = cat_scores.get(cat, 0) + 0.5
                    break

        if not cat_scores:
            return TicketManager.classify(
                " ".join(item.get("title", "") for item in search_results)
            )

        return max(cat_scores, key=cat_scores.get)

    def get_daily_summary(self, date_str: str = None) -> dict:
        """
        获取单日工单统计摘要。
        :param date_str: 日期字符串 YYYY-MM-DD，默认今天
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        start_at = f"{date_str} 00:00:00"
        end_at = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

        with self._lock:
            conn = self._connect()
            try:
                # 当日新建
                new_count = conn.execute(
                    "SELECT COUNT(*) FROM tickets WHERE created_at>=? AND created_at<?",
                    (start_at, end_at),
                ).fetchone()[0]

                # 当日解决/关闭
                resolved_count = conn.execute(
                    "SELECT COUNT(*) FROM tickets WHERE resolved_at>=? AND resolved_at<?",
                    (start_at, end_at),
                ).fetchone()[0]

                # 当日分类分布
                cat_rows = conn.execute(
                    "SELECT category, COUNT(*) FROM tickets WHERE created_at>=? AND created_at<? GROUP BY category",
                    (start_at, end_at),
                ).fetchall()

                # 待处理工单数（全部未关闭的）
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM tickets WHERE status NOT IN ('resolved', 'closed')",
                ).fetchone()[0]

                # 按分类的待处理数
                pending_by_cat = conn.execute(
                    "SELECT category, COUNT(*) FROM tickets WHERE status NOT IN ('resolved', 'closed') GROUP BY category",
                ).fetchall()

                # 当日状态分布
                status_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM tickets WHERE created_at>=? AND created_at<? GROUP BY status",
                    (start_at, end_at),
                ).fetchall()

                return {
                    "date": date_str,
                    "new_today": new_count,
                    "resolved_today": resolved_count,
                    "pending_total": pending_count,
                    "by_category": {r[0]: r[1] for r in cat_rows},
                    "pending_by_category": {r[0]: r[1] for r in pending_by_cat},
                    "by_status": {r[0]: r[1] for r in status_rows},
                }
            finally:
                conn.close()

    def _row_to_dict(self, row) -> dict:
        """将 sqlite3 行转为字典（兼容旧版 DB，自动处理字段缺失）"""
        def _val(idx, default=None):
            if idx >= len(row):
                return default
            v = row[idx]
            if v is None:
                return default
            return v
        return {
            "ticket_id":         _val(0),
            "conv_id":           _val(1),
            "conv_title":        _val(2, ""),
            "creator_id":        _val(3),
            "creator_nick":      _val(4),
            "creator_staff_id":  _val(5, ""),
            "category":          _val(6, "其他"),
            "title":             _val(7),
            "description":       _val(8, ""),
            "priority":          _val(9, "normal"),
            "status":            _val(10, "open"),
            "assignee_id":      _val(11, ""),
            "assignee_nick":    _val(12, ""),
            "resolver_id":      _val(13, ""),
            "resolver_nick":    _val(14, ""),
            "resolution":        _val(15, ""),
            "source_qa_id":     _val(16, ""),
            "is_invalid":       _val(17, 0),
            "is_experience":    _val(18, 0),
            "created_at":        _val(19),
            "updated_at":        _val(20),
            "resolved_at":       _val(21),
        }

    def add_admin_from_dingtalk(self, user_id: str, user_nick: str, staff_id: str = ""):
        """如果管理员表为空，自动添加第一个交互用户为管理员"""
        # 先在锁内检查管理员表是否为空
        should_add = False
        with self._lock:
            conn = self._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
                should_add = (count == 0)
            finally:
                conn.close()
        # 在锁外调用 add_admin（避免同锁重入死锁）
        if should_add:
            self.add_admin(user_id, user_nick, staff_id=staff_id)
