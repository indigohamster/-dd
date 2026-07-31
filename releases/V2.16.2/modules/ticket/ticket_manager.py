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
SCHEMA_VERSION = 3

# 工单状态枚举
STATUS_OPEN = "open"
STATUS_ASSIGNED = "assigned"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"
REQUEST_TYPES = {"incident", "service_request", "major_incident"}

STATUS_LABELS = {
    STATUS_OPEN: "待处理",
    STATUS_ASSIGNED: "已分配",
    STATUS_IN_PROGRESS: "处理中",
    STATUS_RESOLVED: "待用户确认",
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
PRIORITY_RESPONSE_HOURS = {
    PRIORITY_URGENT: 0.5,
    PRIORITY_HIGH:   2,
    PRIORITY_MEDIUM: 4,
    PRIORITY_LOW:    8,
}
CATEGORIES = {
    "设备问题": ["硬件", "电脑", "打印机", "显示器", "键盘", "鼠标", "服务器", "硬盘",
                 "内存", "网络", "vpn", "wifi", "网线", "断网", "ip", "路由器", "交换机"],
    "业务系统问题": ["u9", "erp", "料号", "凭证", "财务系统", "账套", "采购", "供应链"],
    "CRM问题":  ["crm", "客户", "售后", "服务单", "工单回访"],
    "软件问题": ["软件", "系统", "安装", "蓝屏", "卡死", "office", "wps", "钉钉", "浏览器",
                 "账号", "密码", "权限", "登录", "重置"],
    "其他":     [],
}

# Product builds never contain tenant knowledge-base IDs. Classification uses
# the configured knowledge-base name and ticket text instead.
KB_ID_TO_CATEGORY: dict[str, str] = {}

# KB 名称关键词 → 工单分类（名称匹配兜底）
KB_NAME_TO_CATEGORY: dict[str, str] = {
    "u9":    "业务系统问题",
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
                        visibility      TEXT DEFAULT 'internal',
                        created_at      TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS ticket_feedback (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticket_id       TEXT NOT NULL UNIQUE REFERENCES tickets(ticket_id) ON DELETE CASCADE,
                        user_id         TEXT NOT NULL,
                        user_nick       TEXT DEFAULT '',
                        score           INTEGER DEFAULT NULL CHECK(score IS NULL OR score BETWEEN 1 AND 5),
                        comment         TEXT DEFAULT '',
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
                    CREATE INDEX IF NOT EXISTS idx_ticket_feedback_created
                        ON ticket_feedback(created_at DESC);
                """)
                conn.commit()
                # 对已有表补充新字段（兼容旧数据库）
                self._migrate_columns(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_followup "
                    "ON tickets(status, followup_due_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_resolution_due "
                    "ON tickets(status, resolution_due_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_service_type "
                    "ON tickets(service_code, request_type, created_at DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tickets_assignment_group "
                    "ON tickets(assignment_group, status, created_at DESC)"
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
            ("request_type", "TEXT DEFAULT 'incident'"),
            ("service_code", "TEXT DEFAULT ''"),
            ("service_name", "TEXT DEFAULT ''"),
            ("assignment_group", "TEXT DEFAULT ''"),
            ("response_due_at", "TEXT DEFAULT NULL"),
            ("resolution_due_at", "TEXT DEFAULT NULL"),
            ("first_response_at", "TEXT DEFAULT NULL"),
            ("confirmed_at", "TEXT DEFAULT NULL"),
            ("satisfaction_score", "INTEGER DEFAULT NULL"),
            ("satisfaction_comment", "TEXT DEFAULT ''"),
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

        log_existing = {row[1] for row in conn.execute("PRAGMA table_info(ticket_log)").fetchall()}
        if "visibility" not in log_existing:
            try:
                conn.execute("ALTER TABLE ticket_log ADD COLUMN visibility TEXT DEFAULT 'internal'")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"DB迁移失败(ticket_log.visibility): {e}")
        # 可重复执行的可见性回填也覆盖曾由早期 V2.15 预览版添加过字段、
        # 但尚未分类旧日志的数据库。处理备注仍保持 internal。
        try:
            conn.execute(
                "UPDATE ticket_log SET visibility='public' WHERE action IN "
                "('assign','assigned','in_progress','resolve','resolved','close','closed',"
                "'reopen','confirm','withdraw')"
            )
            conn.execute(
                "UPDATE ticket_log SET visibility='system' WHERE action IN "
                "('create','priority_change','archive','mark_invalid','import')"
            )
            conn.commit()
        except Exception as e:
            if self.logger:
                self.logger.warning(f"DB迁移失败(ticket_log.visibility 回填): {e}")

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

        # 为旧工单补齐可计算的 SLA 截止时间。只填空值，避免覆盖已按服务项
        # 固化的 SLA 快照。
        try:
            conn.execute(
                "UPDATE tickets SET response_due_at=datetime(created_at, '+4 hours') "
                "WHERE response_due_at IS NULL AND created_at IS NOT NULL"
            )
            conn.execute(
                "UPDATE tickets SET resolution_due_at=datetime(created_at, "
                "CASE priority WHEN 'urgent' THEN '+2 hours' WHEN 'high' THEN '+8 hours' "
                "WHEN 'low' THEN '+72 hours' ELSE '+24 hours' END) "
                "WHERE resolution_due_at IS NULL AND created_at IS NOT NULL"
            )
            conn.commit()
        except Exception as e:
            if self.logger:
                self.logger.warning(f"DB迁移失败(SLA 截止时间回填): {e}")

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

    _DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def _parse_datetime(cls, value) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in (cls._DATETIME_FORMAT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:len(datetime.now().strftime(fmt))], fmt)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def _deadline_status(cls, created_at, due_at, completed_at=None,
                         now: Optional[datetime] = None) -> str:
        """计算单个 SLA 阶段状态。最后 25%（至少一小时）视为有风险。"""
        created = cls._parse_datetime(created_at)
        due = cls._parse_datetime(due_at)
        completed = cls._parse_datetime(completed_at)
        now = now or datetime.now()
        if completed:
            return "met" if not due or completed <= due else "breached"
        if not due:
            return "normal"
        if now >= due:
            return "breached"
        total_seconds = (due - created).total_seconds() if created else 0
        # 兼顾短 SLA 与长 SLA：风险窗口最少 15 分钟、最多 1 小时，
        # 中间按总时长的 25% 计算，避免 30 分钟 SLA 创建后立即变为风险。
        risk_seconds = min(max(total_seconds * 0.25, 900), 3600)
        return "at_risk" if (due - now).total_seconds() <= risk_seconds else "normal"

    def _decorate_ticket(self, ticket: dict, now: Optional[datetime] = None) -> dict:
        """补齐兼容字段并附加可直接用于 API 筛选/展示的 SLA 状态。"""
        data = dict(ticket or {})
        defaults = {
            "creator_staff_id": "", "category": "OTHER", "description": "",
            "priority": PRIORITY_MEDIUM, "status": STATUS_OPEN,
            "assignee_id": "", "assignee_nick": "", "resolver_id": "",
            "resolver_nick": "", "resolution": "", "source_qa_id": "",
            "is_invalid": 0, "is_experience": 0, "followup_due_at": None,
            "followup_sent_at": None, "user_confirmation": "",
            "request_type": "incident", "service_code": "", "service_name": "",
            "assignment_group": "", "response_due_at": None,
            "resolution_due_at": None, "first_response_at": None,
            "confirmed_at": None, "satisfaction_score": None,
            "satisfaction_comment": "",
        }
        for key, value in defaults.items():
            if data.get(key) is None and value is not None:
                data[key] = value
            elif key not in data:
                data[key] = value
        if data.get("priority") == "normal":
            data["priority"] = PRIORITY_MEDIUM

        now = now or datetime.now()
        completed = data.get("resolved_at") if data.get("status") in (STATUS_RESOLVED, STATUS_CLOSED) else None
        response_completed = data.get("first_response_at") or completed
        response_status = self._deadline_status(
            data.get("created_at"), data.get("response_due_at"), response_completed, now,
        )
        resolution_status = self._deadline_status(
            data.get("created_at"), data.get("resolution_due_at"), completed, now,
        )
        if "breached" in (response_status, resolution_status):
            overall = "breached"
        elif data.get("status") in (STATUS_RESOLVED, STATUS_CLOSED):
            overall = "met"
        elif "at_risk" in (response_status, resolution_status):
            overall = "at_risk"
        else:
            overall = "normal"
        data["sla_status"] = overall
        data["response_sla_status"] = response_status
        data["resolution_sla_status"] = resolution_status
        due = self._parse_datetime(data.get("resolution_due_at"))
        data["sla_remaining_seconds"] = int((due - now).total_seconds()) if due else None
        return data

    def _ticket_dict(self, row, now: Optional[datetime] = None) -> Optional[dict]:
        if row is None:
            return None
        if isinstance(row, dict):
            data = dict(row)
        elif hasattr(row, "keys"):
            data = {key: row[key] for key in row.keys()}
        else:
            data = self._row_to_dict(row, decorate=False)
        return self._decorate_ticket(data, now=now)

    def _generate_ticket_id(self, conn: sqlite3.Connection = None) -> str:
        """生成工单编号: TKT-YYYYMMDD-NNN。

        create_ticket 会传入已持有 BEGIN IMMEDIATE 写锁的连接，使“读取最大号、
        生成编号、插入”成为同一个数据库事务；因此不同 TicketManager 实例甚至
        不同进程也不会取到同一编号。
        """
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"TKT-{today}-"
        owns_connection = conn is None
        if owns_connection:
            conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ticket_id FROM tickets WHERE ticket_id LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
            pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
            numbers = []
            for row in rows:
                match = pattern.fullmatch(str(row[0] or ""))
                if match:
                    numbers.append(int(match.group(1)))
            return f"{prefix}{(max(numbers, default=0) + 1):03d}"
        finally:
            if owns_connection:
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
        request_type: str = "incident",
        service_code: str = "",
        service_name: str = "",
        assignment_group: str = "",
        response_hours: float = None,
        resolution_hours: float = None,
    ) -> dict:
        """创建工单，返回工单字典"""
        priority = priority if priority in PRIORITY_ORDER else PRIORITY_MEDIUM
        request_type = str(request_type or "incident").strip().lower()
        if request_type == "request":
            request_type = "service_request"
        if request_type not in REQUEST_TYPES:
            request_type = "incident"
        response_hours = (PRIORITY_RESPONSE_HOURS[priority]
                          if response_hours is None else max(0.1, float(response_hours)))
        resolution_hours = (PRIORITY_OVERDUE_HOURS[priority]
                            if resolution_hours is None else max(0.1, float(resolution_hours)))
        now_dt = datetime.now()
        now = now_dt.strftime(self._DATETIME_FORMAT)
        response_due = (now_dt + timedelta(hours=response_hours)).strftime(self._DATETIME_FORMAT)
        resolution_due = (now_dt + timedelta(hours=resolution_hours)).strftime(self._DATETIME_FORMAT)
        with self._lock:
            conn = self._connect()
            try:
                # SQLite 写事务把编号分配与插入串行化，跨管理器/跨进程仍唯一。
                conn.execute("BEGIN IMMEDIATE")
                ticket_id = self._generate_ticket_id(conn)
                conn.execute(
                    """INSERT INTO tickets (ticket_id, conv_id, conv_title, creator_id, creator_nick,
                       creator_staff_id, category, title, description, priority, status, created_at, updated_at,
                       request_type, service_code, service_name, assignment_group, response_due_at, resolution_due_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ticket_id, conv_id, conv_title, creator_id, creator_nick,
                     creator_staff_id, category, title, description, priority, STATUS_OPEN, now, now,
                     request_type, service_code, service_name, assignment_group, response_due, resolution_due),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, 'create', ?, ?, ?, 'system', ?)",
                    (ticket_id, creator_id, creator_nick, f"创建工单: {title}", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_ticket(self, ticket_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                return [self._ticket_dict(r) for r in rows]
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
                return [self._ticket_dict(r) for r in rows]
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
                return [self._ticket_dict(r) for r in rows]
            finally:
                conn.close()

    def query_tickets(
        self, q="", statuses=None, priorities=None, categories=None, assignee="",
        service_item="", ticket_type="", sla_status="", created_from="",
        created_to="", page=1, page_size=20, sort="created_at", order="desc",
        unassigned=False, **aliases,
    ) -> dict:
        """组合筛选并分页查询工单，兼容管理端与旧字段命名。"""
        q = aliases.pop("keyword", q)
        statuses = aliases.pop("status", statuses)
        priorities = aliases.pop("priority", priorities)
        categories = aliases.pop("category", categories)
        service_item = aliases.pop("service_code", service_item)
        ticket_type = aliases.pop("request_type", ticket_type)
        created_from = aliases.pop("date_from", created_from)
        created_to = aliases.pop("date_to", created_to)
        assignment_group = str(aliases.pop("assignment_group", "") or "").strip()
        confirmation = str(aliases.pop("confirmation", "") or "").strip()
        exact_date = str(aliases.pop("date", "") or "").strip()
        if exact_date:
            created_from = created_from or exact_date
            created_to = created_to or exact_date

        def values(value):
            if value is None:
                return []
            if isinstance(value, (list, tuple, set)):
                source = value
            else:
                source = str(value).split(",")
            return [str(item).strip() for item in source if str(item).strip()]

        def truthy(value):
            return value is True or str(value or "").strip().lower() in ("1", "true", "yes", "on")

        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 20), 500))
        clauses, params = [], []
        keyword = str(q or "").strip()
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(ticket_id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR "
                "description LIKE ? ESCAPE '\\' OR creator_nick LIKE ? ESCAPE '\\' OR "
                "creator_id LIKE ? ESCAPE '\\' OR creator_staff_id LIKE ? ESCAPE '\\' OR "
                "assignee_nick LIKE ? ESCAPE '\\' OR assignee_id LIKE ? ESCAPE '\\' OR "
                "resolution LIKE ? ESCAPE '\\' OR service_code LIKE ? ESCAPE '\\' OR "
                "service_name LIKE ? ESCAPE '\\' OR assignment_group LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 12)
        for column, requested in (("status", statuses), ("priority", priorities), ("category", categories)):
            selected = values(requested)
            if selected:
                clauses.append(f"{column} IN ({','.join('?' for _ in selected)})")
                params.extend(selected)
        if truthy(unassigned):
            clauses.append("COALESCE(assignee_id,'')='' AND COALESCE(assignee_nick,'')=''")
        elif str(assignee or "").strip():
            assignee_value = str(assignee).strip()
            clauses.append("(assignee_id=? OR assignee_nick=?)")
            params.extend([assignee_value, assignee_value])
        if str(service_item or "").strip():
            service_value = str(service_item).strip()
            clauses.append("(service_code=? OR service_name=?)")
            params.extend([service_value, service_value])
        if str(ticket_type or "").strip():
            clauses.append("request_type=?")
            params.append(str(ticket_type).strip())
        if assignment_group:
            clauses.append("assignment_group=?")
            params.append(assignment_group)
        if confirmation:
            clauses.append("user_confirmation=?")
            params.append(confirmation)
        if created_from:
            value = str(created_from).strip()
            clauses.append("created_at>=?")
            params.append(f"{value} 00:00:00" if len(value) == 10 else value)
        if created_to:
            value = str(created_to).strip()
            if len(value) == 10:
                end = datetime.strptime(value, "%Y-%m-%d") + timedelta(days=1)
                clauses.append("created_at<?")
                params.append(end.strftime(self._DATETIME_FORMAT))
            else:
                clauses.append("created_at<=?")
                params.append(value)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sort_columns = {
            "ticket_id", "created_at", "updated_at", "resolved_at", "status", "category",
            "assignee_nick", "service_code", "request_type", "response_due_at",
            "resolution_due_at", "first_response_at", "satisfaction_score",
        }
        if sort == "priority":
            order_by = "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
        else:
            sort = sort if sort in sort_columns else "created_at"
            order_by = sort
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        selected_sla = set(values(sla_status))

        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                if selected_sla:
                    rows = conn.execute(
                        f"SELECT * FROM tickets{where} ORDER BY {order_by} {direction}, ticket_id DESC",
                        params,
                    ).fetchall()
                    decorated = [self._ticket_dict(row) for row in rows]
                    decorated = [row for row in decorated if row["sla_status"] in selected_sla]
                    total = len(decorated)
                    start = (page - 1) * page_size
                    items = decorated[start:start + page_size]
                else:
                    total = conn.execute(f"SELECT COUNT(*) FROM tickets{where}", params).fetchone()[0]
                    query_params = [*params, page_size, (page - 1) * page_size]
                    rows = conn.execute(
                        f"SELECT * FROM tickets{where} ORDER BY {order_by} {direction}, ticket_id DESC LIMIT ? OFFSET ?",
                        query_params,
                    ).fetchall()
                    items = [self._ticket_dict(row) for row in rows]
                return {"items": items, "total": total, "page": page, "page_size": page_size}
            finally:
                conn.close()

    def get_ticket_logs(self, ticket_id: str, limit: int = 20,
                        include_internal: bool = True) -> list:
        """获取工单操作日志"""
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                internal_clause = "" if include_internal else " AND visibility!='internal'"
                rows = conn.execute(
                    f"SELECT * FROM ticket_log WHERE ticket_id=?{internal_clause} "
                    "ORDER BY created_at DESC,id DESC LIMIT ?",
                    (ticket_id, max(1, min(int(limit), 1000))),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def search_tickets_exact(self, field: str, value: str, limit: int = 100) -> list:
        """按白名单字段精确匹配工单。"""
        allowed = {"ticket_id", "creator_id", "creator_nick", "creator_staff_id", "status",
                   "priority", "category", "assignee_id", "assignee_nick", "title", "conv_id",
                   "request_type", "service_code", "service_name", "assignment_group", "sla_status"}
        if field not in allowed:
            raise ValueError("不支持的精确搜索字段")
        value = str(value or "").strip()
        if not value:
            return []
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                if field == "sla_status":
                    rows = conn.execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()
                    return [ticket for ticket in (self._ticket_dict(row) for row in rows)
                            if ticket["sla_status"] == value][:max(1, min(int(limit), 500))]
                rows = conn.execute(
                    f"SELECT * FROM tickets WHERE {field}=? ORDER BY created_at DESC LIMIT ?",
                    (value, max(1, min(int(limit), 500))),
                ).fetchall()
                return [self._ticket_dict(row) for row in rows]
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
        now_dt = datetime.now()
        now = now_dt.strftime(self._DATETIME_FORMAT)
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
                    created_dt = self._parse_datetime(created_at) or now_dt
                    updated_at = str(raw.get("updated_at") or created_at).strip()
                    request_type = str(raw.get("request_type") or raw.get("ticket_type") or "incident").strip().lower()
                    if request_type == "request":
                        request_type = "service_request"
                    if request_type not in REQUEST_TYPES:
                        errors.append(f"第 {index} 行工单类型无效")
                        continue
                    score_raw = raw.get("satisfaction_score")
                    score = None
                    if score_raw not in (None, ""):
                        try:
                            score = int(score_raw)
                        except (TypeError, ValueError):
                            score = 0
                        if score < 1 or score > 5:
                            errors.append(f"第 {index} 行满意度评分无效")
                            continue
                    resolved_at = raw.get("resolved_at") or None
                    if status in (STATUS_RESOLVED, STATUS_CLOSED) and not resolved_at:
                        resolved_at = updated_at
                    response_due_at = raw.get("response_due_at") or (
                        created_dt + timedelta(hours=PRIORITY_RESPONSE_HOURS[priority])
                    ).strftime(self._DATETIME_FORMAT)
                    resolution_due_at = raw.get("resolution_due_at") or (
                        created_dt + timedelta(hours=PRIORITY_OVERDUE_HOURS[priority])
                    ).strftime(self._DATETIME_FORMAT)
                    confirmation = str(raw.get("user_confirmation") or "")
                    confirmed_at = raw.get("confirmed_at") or None
                    if confirmation == "confirmed" and not confirmed_at:
                        confirmed_at = updated_at
                    record = {
                        "ticket_id": ticket_id,
                        "conv_id": str(raw.get("conv_id") or "import"),
                        "conv_title": str(raw.get("conv_title") or "导入"),
                        "creator_id": str(raw.get("creator_id") or "import"),
                        "creator_nick": str(raw.get("creator_nick") or "导入用户"),
                        "creator_staff_id": str(raw.get("creator_staff_id") or ""),
                        "category": str(raw.get("category") or "OTHER"),
                        "title": title,
                        "description": str(raw.get("description") or ""),
                        "priority": priority,
                        "status": status,
                        "assignee_id": str(raw.get("assignee_id") or ""),
                        "assignee_nick": str(raw.get("assignee_nick") or ""),
                        "resolver_id": str(raw.get("resolver_id") or ""),
                        "resolver_nick": str(raw.get("resolver_nick") or ""),
                        "resolution": str(raw.get("resolution") or ""),
                        "source_qa_id": str(raw.get("source_qa_id") or ""),
                        "is_invalid": int(str(raw.get("is_invalid") or "0").lower() in ("1", "true", "yes")),
                        "is_experience": int(str(raw.get("is_experience") or "0").lower() in ("1", "true", "yes")),
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "resolved_at": resolved_at,
                        "followup_due_at": raw.get("followup_due_at") or None,
                        "followup_sent_at": raw.get("followup_sent_at") or None,
                        "user_confirmation": confirmation,
                        "request_type": request_type,
                        "service_code": str(raw.get("service_code") or raw.get("service_item") or ""),
                        "service_name": str(raw.get("service_name") or ""),
                        "assignment_group": str(raw.get("assignment_group") or ""),
                        "response_due_at": response_due_at,
                        "resolution_due_at": resolution_due_at,
                        "first_response_at": raw.get("first_response_at") or None,
                        "confirmed_at": confirmed_at,
                        "satisfaction_score": score,
                        "satisfaction_comment": str(raw.get("satisfaction_comment") or ""),
                    }
                    columns = tuple(record)
                    conn.execute(
                        f"INSERT INTO tickets ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(record[column] for column in columns),
                    )
                    conn.execute("INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,visibility,created_at) "
                                 "VALUES(?,'import',?,?,?,'system',?)",
                                 (ticket_id, operator_id, operator_nick, "从管理后台批量导入", now))
                    if confirmation == "confirmed" or score is not None or record["satisfaction_comment"]:
                        conn.execute(
                            "INSERT OR REPLACE INTO ticket_feedback(ticket_id,user_id,user_nick,score,comment,created_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (ticket_id, record["creator_id"], record["creator_nick"], score,
                             record["satisfaction_comment"], confirmed_at or updated_at),
                        )
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
                    operator_nick: str = "", visibility: str = "internal") -> Optional[dict]:
        """添加处理记录但不改变工单状态。"""
        detail = str(detail or "").strip()
        visibility = str(visibility or "internal").strip().lower()
        if not detail or visibility not in ("internal", "public", "system"):
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT creator_id FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                # 员工补充信息或催办不属于运维首次响应，不能因此虚假满足响应 SLA。
                if str(operator_id or "") and str(operator_id) != str(current[0] or ""):
                    conn.execute(
                        "UPDATE tickets SET updated_at=?, first_response_at=COALESCE(first_response_at,?) "
                        "WHERE ticket_id=?",
                        (now, now, ticket_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tickets SET updated_at=? WHERE ticket_id=?", (now, ticket_id),
                    )
                conn.execute(
                    "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,visibility,created_at) "
                    "VALUES(?,'comment',?,?,?,?,?)",
                    (ticket_id, operator_id, operator_nick, detail, visibility, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current or current[0] not in (STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS):
                    return None
                conn.execute(
                    "UPDATE tickets SET priority=?, updated_at=? WHERE ticket_id=?",
                    (priority, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, 'priority_change', '', 'system', ?, 'system', ?)",
                    (ticket_id, f"优先级变更为 {PRIORITY_LABELS.get(priority, priority)}", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
            finally:
                conn.close()

    def get_overdue_tickets(self) -> list:
        """返回所有解决 SLA 已违约的活跃工单，不受工作台分页限制。"""
        now = datetime.now()
        now_text = now.strftime(self._DATETIME_FORMAT)
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM tickets WHERE status IN (?,?,?) "
                    "AND resolution_due_at IS NOT NULL AND resolution_due_at<? "
                    "ORDER BY resolution_due_at ASC, ticket_id ASC",
                    (STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS, now_text),
                ).fetchall()
                overdue = []
                for row in rows:
                    ticket = self._ticket_dict(row, now=now)
                    due = self._parse_datetime(ticket.get("resolution_due_at"))
                    ticket["overdue_hours"] = round((now - due).total_seconds() / 3600, 1)
                    overdue.append(ticket)
                return overdue
            finally:
                conn.close()

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
                return self._ticket_dict(row)
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
                return [self._ticket_dict(r) for r in rows]
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
            ticket = self._ticket_dict(row)
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
                    if new_status in (STATUS_ASSIGNED, STATUS_IN_PROGRESS):
                        conn.execute(
                            "UPDATE tickets SET first_response_at=COALESCE(first_response_at,?) WHERE ticket_id=?",
                            (now, ticket_id),
                        )
                        conn.commit()
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
                    ).fetchone()
                    return self._ticket_dict(row)
                if new_status not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    if self.logger:
                        self.logger.warning(
                            f"拒绝非法工单状态流转: {ticket_id} {current[0]} -> {new_status}"
                        )
                    return None
                if new_status in (STATUS_RESOLVED, STATUS_CLOSED):
                    conn.execute(
                        "UPDATE tickets SET status=?, updated_at=?, resolved_at=?, "
                        "first_response_at=COALESCE(first_response_at,?) WHERE ticket_id=?",
                        (new_status, now, now, now, ticket_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tickets SET status=?, updated_at=?, "
                        "first_response_at=CASE WHEN ? IN (?,?) THEN COALESCE(first_response_at,?) ELSE first_response_at END "
                        "WHERE ticket_id=?",
                        (new_status, now, new_status, STATUS_ASSIGNED, STATUS_IN_PROGRESS, now, ticket_id),
                    )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'public', ?)",
                    (ticket_id, new_status, operator_id, operator_nick, detail, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                # 已分配工单允许转派给另一位处理人；解决/关闭后仍禁止分派。
                if (current[0] != STATUS_ASSIGNED and
                        STATUS_ASSIGNED not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set())):
                    return None
                conn.execute(
                    "UPDATE tickets SET assignee_id=?, assignee_nick=?, status=?, updated_at=?, "
                    "first_response_at=COALESCE(first_response_at,?) WHERE ticket_id=?",
                    (assignee_id, assignee_nick, STATUS_ASSIGNED, now, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, 'assign', ?, ?, ?, 'public', ?)",
                    (ticket_id, operator_id, operator_nick,
                     f"分配给 {assignee_nick}({assignee_id})", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                    "resolved_at=?, followup_due_at=?, followup_sent_at=NULL, user_confirmation='', "
                    "first_response_at=COALESCE(first_response_at,?), confirmed_at=NULL, "
                    "satisfaction_score=NULL, satisfaction_comment='' WHERE ticket_id=?",
                    (STATUS_RESOLVED, resolution, operator_id, operator_nick, now, now, followup_due, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'public', ?)",
                    (ticket_id, "resolve", operator_id, operator_nick, resolution, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                return [self._ticket_dict(row) for row in rows]
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
        """将待确认工单重新打开，并保留可用于重开率的独立历史动作。"""
        now = datetime.now().strftime(self._DATETIME_FORMAT)
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,),
                ).fetchone()
                if not current or current[0] != STATUS_RESOLVED:
                    return None
                conn.execute(
                    "UPDATE tickets SET status=?, updated_at=?, resolved_at=NULL, "
                    "user_confirmation='reopened', followup_due_at=NULL WHERE ticket_id=?",
                    (STATUS_IN_PROGRESS, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,visibility,created_at) "
                    "VALUES(?,'reopen',?,?,?,'public',?)",
                    (ticket_id, operator_id, operator_nick,
                     str(detail or "用户确认问题未解决").strip(), now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
            finally:
                conn.close()

    def get_reopen_metrics(self) -> dict:
        """返回有效工单的历史重开数与已给出解决方案数。"""
        with self._lock:
            conn = self._connect()
            try:
                valid = "t.is_invalid=0 AND COALESCE(t.user_confirmation,'')<>'withdrawn'"
                resolved = conn.execute(
                    f"SELECT COUNT(*) FROM tickets t WHERE {valid} "
                    "AND COALESCE(t.resolution,'')<>''"
                ).fetchone()[0]
                reopened = conn.execute(
                    f"SELECT COUNT(DISTINCT t.ticket_id) FROM tickets t WHERE {valid} AND ("
                    "t.user_confirmation='reopened' OR EXISTS(SELECT 1 FROM ticket_log l "
                    "WHERE l.ticket_id=t.ticket_id AND l.action='reopen'))"
                ).fetchone()[0]
                return {"reopened": reopened, "resolved": resolved}
            finally:
                conn.close()

    def confirm_resolution(self, ticket_id, user_id, operator_nick="", score=None,
                           comment="") -> Optional[dict]:
        """由创建人确认解决并关闭工单，同时原子写入满意度与审计日志。"""
        normalized_score = None
        if score not in (None, ""):
            try:
                normalized_score = int(score)
            except (TypeError, ValueError):
                return None
            if normalized_score < 1 or normalized_score > 5:
                return None
        user_id = str(user_id or "").strip()
        comment = str(comment or "").strip()
        now = datetime.now().strftime(self._DATETIME_FORMAT)
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                current = conn.execute(
                    "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if not current or current["creator_id"] != user_id:
                    return None
                if current["status"] == STATUS_CLOSED and current["user_confirmation"] == "confirmed":
                    return self._ticket_dict(current)
                if current["status"] != STATUS_RESOLVED:
                    return None
                nick = str(operator_nick or current["creator_nick"] or "用户")
                conn.execute(
                    "UPDATE tickets SET status=?, updated_at=?, confirmed_at=?, "
                    "user_confirmation='confirmed', satisfaction_score=?, satisfaction_comment=?, "
                    "followup_due_at=NULL WHERE ticket_id=?",
                    (STATUS_CLOSED, now, now, normalized_score, comment, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_feedback(ticket_id,user_id,user_nick,score,comment,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(ticket_id) DO UPDATE SET "
                    "user_id=excluded.user_id,user_nick=excluded.user_nick,score=excluded.score,"
                    "comment=excluded.comment,created_at=excluded.created_at",
                    (ticket_id, user_id, nick, normalized_score, comment, now),
                )
                detail = "用户确认问题已解决"
                if normalized_score is not None:
                    detail += f"，满意度 {normalized_score}/5"
                if comment:
                    detail += f"：{comment}"
                conn.execute(
                    "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,visibility,created_at) "
                    "VALUES(?,'confirm',?,?,?,'public',?)",
                    (ticket_id, user_id, nick, detail, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def list_feedback(self, ticket_id: str = "", limit: int = 20) -> list:
        """查询满意度记录；ticket_id 为空时返回最近记录。"""
        limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                if ticket_id:
                    rows = conn.execute(
                        "SELECT * FROM ticket_feedback WHERE ticket_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                        (ticket_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM ticket_feedback ORDER BY created_at DESC,id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def get_ticket_feedback(self, ticket_id: str) -> Optional[dict]:
        rows = self.list_feedback(ticket_id=ticket_id, limit=1)
        return rows[0] if rows else None

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
                    "UPDATE tickets SET status=?, resolver_id=?, resolver_nick=?, updated_at=?, resolved_at=?, "
                    "first_response_at=COALESCE(first_response_at,?) WHERE ticket_id=?",
                    (STATUS_CLOSED, operator_id, operator_nick, now, now, now, ticket_id),
                )
                comment = detail or "工单已关闭"
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, 'close', ?, ?, ?, 'public', ?)",
                    (ticket_id, operator_id, operator_nick, comment, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
            finally:
                conn.close()

    def withdraw_ticket(self, ticket_id: str, user_id: str, user_nick: str,
                        detail: str = "") -> Optional[dict]:
        """由创建者撤回尚未解决的工单；关闭但不计入已解决指标。"""
        now = datetime.now().strftime(self._DATETIME_FORMAT)
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT creator_id,status FROM tickets WHERE ticket_id=?", (ticket_id,),
                ).fetchone()
                if (not current or str(current[0]) != str(user_id) or
                        current[1] not in (STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS)):
                    return None
                reason = str(detail or "用户不再需要处理").strip()
                conn.execute(
                    "UPDATE tickets SET status=?, updated_at=?, resolved_at=NULL, "
                    "followup_due_at=NULL, user_confirmation='withdrawn' WHERE ticket_id=?",
                    (STATUS_CLOSED, now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,visibility,created_at) "
                    "VALUES(?,'withdraw',?,?,?,'public',?)",
                    (ticket_id, user_id, user_nick, reason, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                    conn.execute(
                        "UPDATE tickets SET first_response_at=COALESCE(first_response_at,?) WHERE ticket_id=?",
                        (now, ticket_id),
                    )
                    conn.commit()
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
                    ).fetchone()
                    return self._ticket_dict(row)
                if STATUS_IN_PROGRESS not in ALLOWED_STATUS_TRANSITIONS.get(current[0], set()):
                    return None
                conn.execute(
                    "UPDATE tickets SET status=?, updated_at=?, first_response_at=COALESCE(first_response_at,?) "
                    "WHERE ticket_id=?",
                    (STATUS_IN_PROGRESS, now, now, ticket_id),
                )
                comment = detail or "用户反馈不满意，升级为处理中"
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'public', ?)",
                    (ticket_id, "in_progress", operator_id, operator_nick, comment, now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
            finally:
                conn.close()

    def archive_as_experience(self, ticket_id: str, operator_id: str = "",
                               operator_nick: str = "", detail: str = "") -> Optional[dict]:
        """仅将用户已确认、包含解决方案的关闭工单归档为经验。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute(
                    "SELECT status,user_confirmation,resolution,is_invalid FROM tickets WHERE ticket_id=?",
                    (ticket_id,)
                ).fetchone()
                if not current:
                    return None
                if (current[0] != STATUS_CLOSED or current[1] != "confirmed" or
                        not str(current[2] or "").strip() or bool(current[3])):
                    return None
                conn.execute(
                    "UPDATE tickets SET is_experience=1, updated_at=? WHERE ticket_id=?",
                    (now, ticket_id),
                )
                conn.execute(
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, 'archive', ?, ?, ?, 'system', ?)",
                    (ticket_id, operator_id, operator_nick, detail or "归档为经验", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                    "INSERT INTO ticket_log (ticket_id, action, operator_id, operator_nick, detail, visibility, created_at)"
                    " VALUES (?, 'mark_invalid', ?, ?, ?, 'system', ?)",
                    (ticket_id, operator_id, operator_nick, detail or "标记为无效工单", now),
                )
                conn.commit()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
                return self._ticket_dict(row)
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
                return self._ticket_dict(row)
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
                resolved_sql = (
                    "SELECT COUNT(*) FROM tickets WHERE status IN (?,?) AND is_invalid=0 "
                    "AND COALESCE(user_confirmation,'')<>'withdrawn'"
                )
                resolved_params = [STATUS_RESOLVED, STATUS_CLOSED]
                if since:
                    resolved_sql += " AND created_at>=?"
                    resolved_params.append(since)
                resolved = conn.execute(resolved_sql, resolved_params).fetchone()[0]
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
                    "SELECT COUNT(*) FROM tickets WHERE resolved_at>=? AND resolved_at<? "
                    "AND is_invalid=0 AND COALESCE(user_confirmation,'')<>'withdrawn'",
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

    def _row_to_dict(self, row, decorate: bool = True) -> dict:
        """将 sqlite3 行转为字典（兼容旧版 DB，自动处理字段缺失）"""
        if row is None:
            return None
        if isinstance(row, dict):
            data = dict(row)
            return self._decorate_ticket(data) if decorate else data
        if hasattr(row, "keys"):
            data = {key: row[key] for key in row.keys()}
            return self._decorate_ticket(data) if decorate else data

        def _val(idx, default=None):
            if idx >= len(row):
                return default
            v = row[idx]
            if v is None:
                return default
            return v
        data = {
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
        return self._decorate_ticket(data) if decorate else data

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
