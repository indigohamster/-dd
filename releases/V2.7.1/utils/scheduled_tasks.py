# ============================================================
# 后台定时任务 — 逾期提醒 + 每周统计报告
# ============================================================

import time
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from handlers.chat_handler import BotMessageHandler

from modules.ticket.ticket_manager import (
    PRIORITY_LABELS, PRIORITY_OVERDUE_HOURS, STATUS_LABELS,
)


class OverdueTicketChecker:
    """
    逾期工单提醒器。
    每 30 分钟检查一次，根据优先级对应的超时阈值判断是否逾期，
    逾期工单通知管理员（避免重复提醒）。
    """

    CHECK_INTERVAL = 1800  # 30 分钟

    def __init__(self, handler: "BotMessageHandler", logger: logging.Logger):
        self.handler = handler
        self.logger = logger
        self._notified_ids = set()  # 已提醒过的工单 ID

    def run(self):
        """主循环（daemon 线程）"""
        # 启动后等 60 秒再开始（等待 Stream 连接建立）
        time.sleep(60)
        while True:
            try:
                self._check()
            except Exception as e:
                self.logger.error(f"逾期检查异常: {e}")
            time.sleep(self.CHECK_INTERVAL)

    def _check(self):
        """执行一次逾期检查"""
        overdue = self.handler.ticket_mgr.get_overdue_tickets()
        if not overdue:
            return

        new_overdue = [t for t in overdue if t["ticket_id"] not in self._notified_ids]
        if not new_overdue:
            return

        self.logger.info(f"发现 {len(new_overdue)} 条新逾期工单")

        # 通知管理员
        for t in new_overdue[:10]:  # 一次最多通知 10 条
            self._notify_admin(t)
            self._notified_ids.add(t["ticket_id"])

        # 清理过旧的已提醒 ID（避免内存泄漏）
        if len(self._notified_ids) > 1000:
            self._cleanup_notified_ids()

    def _notify_admin(self, ticket: dict):
        """通知管理员某工单逾期"""
        prio = PRIORITY_LABELS.get(ticket.get("priority", "medium"), "🟡 中")
        threshold = PRIORITY_OVERDUE_HOURS.get(ticket.get("priority", "medium"), 24)
        overdue_h = ticket.get("overdue_hours", 0)

        title = f"⚠️ 工单逾期提醒 — {ticket['ticket_id']}"
        body = (
            f"### ⚠️ 工单逾期提醒\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**优先级**: {prio}\n"
            f"**标题**: {ticket['title'][:50]}\n"
            f"**分类**: {ticket['category']}\n"
            f"**创建人**: {ticket['creator_nick']}\n"
            f"**创建时间**: {ticket['created_at']}\n"
            f"**已超时**: {overdue_h} 小时（阈值: {threshold} 小时）\n\n"
            f"---\n"
            f"📌 请尽快处理该工单\n"
            f"💬 回复「**解决 {ticket['ticket_id']} [解决方案]**」标记为已解决"
        )

        # 通过钉钉私聊通知所有管理员
        try:
            admins = self.handler.ticket_mgr.list_admins()
            for admin in admins:
                admin_staff_id = admin.get("staff_id", "")
                if admin_staff_id and self.handler.dingtalk_api:
                    self.handler.dingtalk_api.send_markdown_to_user(
                        admin_staff_id, title, body,
                    )
        except Exception as e:
            self.logger.error(f"逾期通知发送失败: {e}")

    def _cleanup_notified_ids(self):
        """清理已关闭/已解决的工单 ID"""
        try:
            all_tickets = self.handler.ticket_mgr.list_all_tickets(filter_type="done", limit=500)
            done_ids = {t["ticket_id"] for t in all_tickets}
            self._notified_ids -= done_ids
        except Exception:
            self._notified_ids.clear()


class WeeklyReportScheduler:
    """
    每周统计报告调度器。
    每天中午 12:00 检查是否为周五，如果是则生成并报告。
    发送给配置中的接收人（默认何佳效）。
    """

    CHECK_INTERVAL = 3600  # 1 小时检查一次
    TARGET_WEEKDAY = 4     # 周五（0=周一）
    TARGET_HOUR = 12       # 12:00

    def __init__(self, handler: "BotMessageHandler", logger: logging.Logger):
        self.handler = handler
        self.logger = logger
        self._last_report_date = None  # 避免重复发送
        # 接收人列表（staff_id），可从 config 或数据库获取
        self._recipients = []  # 延迟加载

    def _get_recipients(self) -> list:
        """获取报告接收人列表"""
        if self._recipients:
            return self._recipients

        # 从 config 读取接收人
        config = getattr(self.handler, "config", {})
        report_config = config.get("weekly_report", {})
        recipient_nicks = report_config.get("recipients", ["何佳效"])

        # 尝试解析为 staff_id
        staff_ids = []
        for nick in recipient_nicks:
            # 先从管理员表找
            admins = self.handler.ticket_mgr.list_admins()
            found = False
            for admin in admins:
                if admin.get("user_nick") == nick and admin.get("staff_id"):
                    staff_ids.append(admin["staff_id"])
                    found = True
                    break
            if not found and self.handler.dingtalk_api:
                try:
                    sid = self.handler.dingtalk_api.resolve_staff_id_by_nick(nick)
                    if sid:
                        staff_ids.append(sid)
                except Exception:
                    self.logger.warning(f"无法解析接收人 '{nick}' 的 staff_id")

        self._recipients = staff_ids
        self.logger.info(f"每周报告接收人: {recipient_nicks} → staff_ids={staff_ids}")
        return self._recipients

    def run(self):
        """主循环（daemon 线程）"""
        time.sleep(120)  # 启动后等 2 分钟
        while True:
            try:
                self._check()
            except Exception as e:
                self.logger.error(f"每周报告检查异常: {e}")
            time.sleep(self.CHECK_INTERVAL)

    def _check(self):
        """检查是否该发送报告"""
        now = datetime.now()

        # 周五 12:00~13:00 之间触发
        if now.weekday() != self.TARGET_WEEKDAY:
            return
        if now.hour != self.TARGET_HOUR:
            return

        # 避免同一天重复发送
        today = now.strftime("%Y-%m-%d")
        if self._last_report_date == today:
            return

        self._last_report_date = today
        self._send_report()

    def _send_report(self):
        """生成并发送每周统计报告"""
        self.logger.info("正在生成每周工单统计报告...")

        now = datetime.now()
        week_ago = now - timedelta(days=7)

        # 统计本周数据
        try:
            all_tickets = self.handler.ticket_mgr.list_all_tickets(filter_type="all", limit=500)
        except Exception as e:
            self.logger.error(f"获取工单数据失败: {e}")
            return

        # 按时间筛选本周工单
        week_tickets = []
        for t in all_tickets:
            try:
                created = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
                if created >= week_ago:
                    week_tickets.append(t)
            except Exception:
                continue

        # 计算统计指标
        total = len(week_tickets)
        created_count = total
        resolved_count = len([t for t in week_tickets if t["status"] in ("resolved", "closed")])
        pending_count = len([t for t in week_tickets if t["status"] in ("open", "assigned", "in_progress")])

        # 按分类统计
        category_counts = {}
        for t in week_tickets:
            cat = t.get("category", "其他")
            category_counts[cat] = category_counts.get(cat, 0) + 1

        # 平均解决时长
        resolve_times = []
        for t in week_tickets:
            if t["status"] in ("resolved", "closed") and t.get("resolved_at") and t.get("created_at"):
                try:
                    created = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
                    resolved = datetime.strptime(t["resolved_at"], "%Y-%m-%d %H:%M:%S")
                    hours = (resolved - created).total_seconds() / 3600
                    if hours >= 0:
                        resolve_times.append(hours)
                except Exception:
                    continue

        avg_resolve_h = sum(resolve_times) / len(resolve_times) if resolve_times else 0

        # 逾期工单数
        overdue = self.handler.ticket_mgr.get_overdue_tickets()
        overdue_count = len(overdue)

        # 生成报告
        period = f"{week_ago.strftime('%m/%d')} ~ {now.strftime('%m/%d')}"
        title = f"📊 每周工单统计报告 ({period})"

        lines = [
            f"### 📊 每周工单统计报告",
            f"",
            f"**统计周期**: {period}",
            f"",
            f"#### 📈 总体概览",
            f"- 本周新增工单: **{created_count}** 条",
            f"- 已解决/关闭: **{resolved_count}** 条",
            f"- 仍在处理: **{pending_count}** 条",
            f"- 逾期未处理: **{overdue_count}** 条",
            f"- 平均解决时长: **{avg_resolve_h:.1f}** 小时",
            f"- 解决率: **{(resolved_count/total*100) if total else 0:.0f}%**",
            f"",
            f"#### 📋 分类统计",
        ]

        for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {cat}: {count} 条")

        if overdue:
            lines.append(f"\n#### ⚠️ 逾期工单（{len(overdue)}条）")
            for t in overdue[:5]:
                prio = PRIORITY_LABELS.get(t.get("priority", "medium"), "")
                lines.append(f"- {prio} **{t['ticket_id']}** {t['title'][:30]}")

        lines.append(f"\n---\n🦊 运维数字人工单系统 | 自动生成于 {now.strftime('%Y-%m-%d %H:%M')}")

        body = "\n".join(lines)

        # 发送给接收人
        recipients = self._get_recipients()
        if not recipients:
            self.logger.warning("没有找到报告接收人，跳过发送")
            return

        for staff_id in recipients:
            try:
                if self.handler.dingtalk_api:
                    self.handler.dingtalk_api.send_markdown_to_user(staff_id, title, body)
                    self.logger.info(f"每周报告已发送给 staff_id={staff_id[:12]}...")
            except Exception as e:
                self.logger.error(f"每周报告发送失败 (staff_id={staff_id[:12]}): {e}")


class TicketFollowupScheduler:
    """定时向已解决工单的创建者确认问题是否真正恢复。"""
    CHECK_INTERVAL = 1800

    def __init__(self, handler: "BotMessageHandler", logger: logging.Logger):
        self.handler, self.logger = handler, logger

    def run(self):
        time.sleep(180)
        while True:
            try:
                self._check()
            except Exception as exc:
                self.logger.error(f"工单回访检查异常: {exc}")
            time.sleep(self.CHECK_INTERVAL)

    def _check(self):
        if not self.handler.dingtalk_api:
            return
        for ticket in self.handler.ticket_mgr.get_due_followups():
            staff_id = ticket.get("creator_staff_id", "")
            if not staff_id:
                self.logger.warning(f"工单 {ticket['ticket_id']} 缺少 creator_staff_id，无法自动回访")
                continue
            title = f"✅ 请确认工单是否恢复 — {ticket['ticket_id']}"
            body = (f"### ✅ 工单解决效果确认\n\n**工单**: {ticket['ticket_id']}\n"
                    f"**问题**: {ticket['title']}\n**解决方案**: {ticket.get('resolution', '')[:300]}\n\n"
                    f"如果问题仍未解决，请回复：**重开 {ticket['ticket_id']}**\n\n如果已经恢复，无需回复。")
            self.handler.dingtalk_api.send_markdown_to_user(staff_id, title, body)
            self.handler.ticket_mgr.mark_followup_sent(ticket["ticket_id"])
            self.logger.info(f"工单回访已发送: {ticket['ticket_id']}")


class DailyOpsSummaryScheduler:
    """每天向管理员发送一份聚焦待办的运维摘要。"""
    CHECK_INTERVAL = 1800

    def __init__(self, handler: "BotMessageHandler", logger: logging.Logger):
        self.handler, self.logger = handler, logger
        cfg = handler.config.get("daily_ops_summary", {})
        self.enabled = cfg.get("enabled", True)
        self.target_hour = int(cfg.get("hour", 9))
        self._last_date = None

    def run(self):
        time.sleep(120)
        while True:
            try:
                self._check()
            except Exception as exc:
                self.logger.error(f"每日运维摘要异常: {exc}")
            time.sleep(self.CHECK_INTERVAL)

    def _check(self, now: datetime = None):
        if not self.enabled or not self.handler.dingtalk_api:
            return
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour != self.target_hour or self._last_date == today:
            return
        self._send(today)
        self._last_date = today

    def _send(self, today: str):
        pending = self.handler.ticket_mgr.list_all_tickets(filter_type="pending", limit=500)
        overdue = self.handler.ticket_mgr.get_overdue_tickets()
        daily = self.handler.ticket_mgr.get_daily_summary(today)
        followups = self.handler.ticket_mgr.get_due_followups(limit=500)
        urgent = [t for t in pending if t.get("priority") in ("urgent", "high")]
        title = f"🧭 每日运维摘要 — {today}"
        lines = ["### 🧭 今日运维工作台", "",
                 f"- 待处理：**{len(pending)}** 条", f"- 高优先级：**{len(urgent)}** 条",
                 f"- 已逾期：**{len(overdue)}** 条", f"- 等待用户确认：**{len(followups)}** 条",
                 f"- 今日新建：**{daily.get('new_today', 0)}** 条",
                 f"- 今日解决：**{daily.get('resolved_today', 0)}** 条", ""]
        if urgent:
            lines.append("#### 🔥 优先处理")
            lines.extend(f"- **{t['ticket_id']}** {t['title'][:40]}" for t in urgent[:5])
        if overdue:
            lines.append("\n#### ⚠️ 已逾期")
            lines.extend(f"- **{t['ticket_id']}** {t['title'][:40]}" for t in overdue[:5])
        lines.append("\n回复「待处理」查看完整队列。")
        body = "\n".join(lines)
        for admin in self.handler.ticket_mgr.list_admins():
            staff_id = admin.get("staff_id", "")
            if staff_id:
                self.handler.dingtalk_api.send_markdown_to_user(staff_id, title, body)
        self.logger.info("每日运维摘要已发送")
