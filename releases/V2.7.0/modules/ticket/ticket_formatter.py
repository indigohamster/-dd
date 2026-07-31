# ============================================================
# 工单消息格式化器 — 生成工单相关的 Markdown 回复
# ============================================================

from datetime import datetime

from .ticket_manager import STATUS_LABELS, PRIORITY_LABELS


class TicketFormatter:
    """将工单数据格式化为钉钉 Markdown 消息"""

    # ── 反馈提示文案 ──
    FEEDBACK_PROMPT = (
        "\n\n---\n"
        "🤔 **这个回答是否为您提供了帮助？**\n"
        "回复「**是**」→ 感谢反馈\n"
        "回复「**否**」→ 升级为工单处理"
    )

    @staticmethod
    def format_ticket_created(ticket: dict) -> tuple:
        """工单创建成功的消息"""
        title = f"✅ 工单已创建 — {ticket['ticket_id']}"
        body = (
            f"### ✅ 工单已创建\n\n"
            f"**编号**: {ticket['ticket_id']}\n"
            f"**类型**: {ticket['category']}\n"
            f"**优先级**: {PRIORITY_LABELS.get(ticket.get('priority', 'medium'), ticket.get('priority', '中'))}\n"
            f"**标题**: {ticket['title']}\n"
            f"**状态**: {STATUS_LABELS.get(ticket['status'], ticket['status'])}\n"
            f"**创建时间**: {ticket['created_at']}\n\n"
            f"已通知管理员，会尽快处理 🦊\n\n"
            f"> 💡 回复「**我的工单**」查看进度\n"
            f"> 📋 回复「**工单 {ticket['ticket_id']}**」查看详情\n"
            f"> ✅ 问题解决后管理员会私聊通知你"
        )
        related = ticket.get("related_active", [])
        if related:
            body += f"\n\n> 🔎 检测到 {len(related)} 条近期相似报障，管理员会统一排查。"
        return title, body

    @staticmethod
    def format_ticket_list(tickets: list, user_nick: str = "") -> tuple:
        """工单列表"""
        if not tickets:
            title = "📭 暂无工单"
            body = "### 📭 暂无工单\n\n你还没有提交过工单哦～\n\n💡 遇到问题直接说「**报修**」然后描述问题就行！"
            return title, body

        title = f"📋 工单列表 — {user_nick}" if user_nick else "📋 工单列表"

        lines = [f"### 📋 工单列表", ""]
        if user_nick:
            lines.append(f"**{user_nick}** 的工单：\n")

        for t in tickets:
            status_icon = TicketFormatter._status_icon(t["status"])
            prio_label = PRIORITY_LABELS.get(t.get("priority", "medium"), "")
            lines.append(
                f"- {status_icon} {prio_label} **[{t['ticket_id']}]** {t['title'][:30]}"
            )
            lines.append(f"  > {t['category']} | {STATUS_LABELS.get(t['status'], t['status'])}"
                         f" | {t['created_at'][:16]}")

        lines.append(f"\n共 {len(tickets)} 条工单")
        return title, "\n".join(lines)

    @staticmethod
    def format_ticket_detail(ticket: dict, logs: list = None) -> tuple:
        """工单详情"""
        status_icon = TicketFormatter._status_icon(ticket["status"])
        title = f"工单 {ticket['ticket_id']}"

        lines = [
            f"### 📋 工单详情 — {ticket['ticket_id']}",
            "",
            f"**标题**: {ticket['title']}",
            f"**分类**: {ticket['category']}",
            f"**状态**: {status_icon} {STATUS_LABELS.get(ticket['status'], ticket['status'])}",
            f"**优先级**: {PRIORITY_LABELS.get(ticket.get('priority', 'medium'), ticket.get('priority', '中'))}",
            f"**创建人**: {ticket['creator_nick']}",
            f"**创建时间**: {ticket['created_at']}",
        ]

        # 处理人（与创建人分开显示）
        resolver_nick = ticket.get("resolver_nick", "")
        if resolver_nick:
            lines.append(f"**处理人**: {resolver_nick}")

        if ticket.get("assignee_nick"):
            lines.append(f"**负责人**: {ticket['assignee_nick']}")
        if ticket.get("description"):
            lines.append(f"\n**问题描述**:\n{ticket['description']}")
        if ticket.get("resolution"):
            lines.append(f"\n**解决方案**:\n{ticket['resolution']}")
        if ticket.get("resolved_at"):
            lines.append(f"**解决时间**: {ticket['resolved_at']}")

        # 归档/无效标记
        if ticket.get("is_experience"):
            lines.append("\n> 📚 此工单已归档为经验")
        elif ticket.get("is_invalid"):
            lines.append("\n> 🚫 此工单已标记为无效")

        if logs:
            lines.append("\n### 📝 操作日志")
            for log in logs:
                action_label = {
                    "create": "创建",
                    "assign": "分配",
                    "in_progress": "开始处理",
                    "resolve": "解决",
                    "resolved": "已解决",
                    "close": "关闭",
                    "closed": "已关闭",
                    "archive": "归档为经验",
                    "mark_invalid": "标记无效",
                }.get(log["action"], log["action"])
                lines.append(
                    f"- [{log['created_at'][:16]}] {log['operator_nick']} — {action_label}"
                )
                if log.get("detail"):
                    lines.append(f"  > {log['detail'][:80]}")

        return title, "\n".join(lines)

    @staticmethod
    def format_ticket_status_updated(ticket_id: str, new_status: str, detail: str = "") -> tuple:
        """状态更新通知"""
        label = STATUS_LABELS.get(new_status, new_status)
        icon = TicketFormatter._status_icon(new_status)
        title = f"工单 {ticket_id} — {label}"
        body = (
            f"### {icon} 工单状态已更新\n\n"
            f"**{ticket_id}** → {label}\n"
        )
        if detail:
            body += f"\n{detail}"
        return title, body

    @staticmethod
    def format_ticket_stats(stats: dict, period: str = "today") -> tuple:
        """工单统计"""
        period_label = {"today": "今日", "week": "本周", "month": "本月"}.get(period, "今日")
        title = f"📊 工单统计 — {period_label}"

        lines = [
            f"### 📊 工单统计 — {period_label}",
            "",
            f"**工单总数**: {stats['total']}",
            f"**待处理**: {stats['open']}",
            f"**已解决**: {stats['resolved']}",
        ]

        if stats.get("by_category"):
            lines.append("\n**分类分布**:")
            for cat, count in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
                lines.append(f"- {cat}: {count}")

        if stats.get("by_status"):
            lines.append("\n**状态分布**:")
            for status, count in stats["by_status"].items():
                label = STATUS_LABELS.get(status, status)
                lines.append(f"- {label}: {count}")

        return title, "\n".join(lines)

    @staticmethod
    def format_daily_summary(summary: dict) -> tuple:
        """每日工单摘要"""
        date_str = summary.get("date", "")
        title = f"📊 工单日报 — {date_str}"

        new_today = summary.get("new_today", 0)
        resolved_today = summary.get("resolved_today", 0)
        pending = summary.get("pending_total", 0)

        lines = [
            f"### 📊 工单日报",
            f"**日期**: {date_str}",
            "",
            f"| 指标 | 数量 |",
            f"|------|------|",
            f"| 🆕 今日新建 | **{new_today}** |",
            f"| ✅ 今日解决 | **{resolved_today}** |",
            f"| 📋 待处理 | **{pending}** |",
        ]

        by_cat = summary.get("by_category", {})
        if by_cat:
            lines.append("")
            lines.append("**今日分类分布**:")
            for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
                bar = "█" * min(count * 2, 20)
                lines.append(f"  {cat:8s} {count:2d} {bar}")

        pending_by_cat = summary.get("pending_by_category", {})
        if pending_by_cat:
            lines.append("")
            lines.append("**待处理工单分类**:")
            for cat, count in sorted(pending_by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"  {cat:8s} {count:2d}")

        lines.append("")
        lines.append(f"> 💡 回复「**我的工单**」查看你的工单")
        lines.append(f"> 📋 回复「**待处理**」查看待处理队列")
        return title, "\n".join(lines)

    @staticmethod
    def format_pending_queue(tickets: list) -> tuple:
        """待处理工单队列"""
        if not tickets:
            return "📭 暂无待处理工单", "### 📭 暂无待处理工单\n\n所有工单都已处理完毕 🎉"

        title = f"📋 待处理工单队列"

        lines = [
            f"### 📋 待处理工单队列",
            f"",
            f"共 {len(tickets)} 条待处理工单：",
            "",
        ]

        for t in tickets:
            prio = PRIORITY_LABELS.get(t.get("priority", "medium"), "🟡 中")
            lines.append(
                f"{prio} **[{t['ticket_id']}]** {t['title'][:40]}"
            )
            lines.append(
                f"> {t['category']} | {STATUS_LABELS.get(t['status'], t['status'])}"
                f" | {t['creator_nick']} | {t['created_at'][:16]}"
            )
            lines.append("")

        lines.append("\n💡 回复「**解决了**」关闭工单")
        return title, "\n".join(lines)

    @staticmethod
    def format_help() -> tuple:
        """工单帮助"""
        title = "📋 工单系统帮助"
        body = (
            "### 📋 工单系统 — 使用指南\n\n"
            "#### 💬 提交工单\n"
            "- 直接说「**报修**」或描述问题 → 自动创建工单（根据知识库智能分类）\n"
            "- 工单创建后管理员会自动收到通知\n\n"
            "#### 📋 查看工单\n"
            "- 「**我的工单**」→ 查看我提交的工单\n"
            "- 「**工单 TKT-编号**」→ 查看详情（仅可查看自己的工单）\n"
            "- 「**待处理**」或「**工单队列**」→ 查看待处理队列（管理员）\n"
            "- 「**所有工单**」/ 「**全部工单**」→ 查看所有工单（管理员）\n"
            "- 「**已处理工单**」/ 「**历史工单**」→ 查看已完成工单（管理员）\n\n"
            "#### ✅ 管理工单（管理员）\n"
            "- 「**解决 TKT-xxx 解决方案**」→ 标记工单已解决，用户会自动收到通知\n"
            "- 「**归档 TKT-xxx**」→ 归档为运维经验\n"
            "- 「**无效 TKT-xxx**」→ 标记为无效工单（不归档为经验）\n"
            "- 「**关闭 TKT-xxx**」→ 关闭工单\n"
            "- 「**工单提醒**」→ 主动查看待处理工单\n"
            "- 「**工单统计**」→ 查看统计\n"
            "- 「**日报**」→ 查看每日工单摘要\n"
            "- 「**绑定工号 01234567**」→ 手动设置企业工号（staffId）\n\n"
            "#### 📊 统计分类\n"
            "- **设备问题** / **U9问题** / **CRM问题** / **软件问题** / **其他**\n\n"
            "> 💡 工单基于 IMA 知识库自动分类，数据全部存储在本地 SQLite 🦊"
        )
        return title, body

    @staticmethod
    def format_user_resolved_notification(ticket: dict, resolver_nick: str = "") -> tuple:
        """
        发送给工单创建者的解决通知。
        管理员解决工单后，私聊通知工单创建者。
        """
        title = f"✅ 工单已解决 — {ticket['ticket_id']}"
        body = (
            f"### ✅ 您的工单已解决！\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**标题**: {ticket['title']}\n"
            f"**分类**: {ticket['category']}\n"
        )
        if resolver_nick:
            body += f"**处理人**: {resolver_nick}\n"
        if ticket.get("resolution"):
            body += f"\n**解决方案**:\n> {ticket['resolution'][:300]}\n"
        body += (
            f"\n**解决时间**: {ticket.get('resolved_at', '')}\n\n"
            f"如果问题仍未解决，可以直接回复「**报修**」重新提交 🦊\n\n"
            f"> 💡 回复「**我的工单**」查看所有工单"
        )
        return title, body

    @staticmethod
    def _status_icon(status: str) -> str:
        icons = {
            "open": "🆕",
            "assigned": "📌",
            "in_progress": "🔧",
            "resolved": "✅",
            "closed": "🔒",
        }
        return icons.get(status, "📋")

    @staticmethod
    def format_feedback_yes() -> tuple:
        """用户满意反馈（V1.3.5 后不再关联工单，仅表示感谢）"""
        title = "🎉 感谢反馈！"
        body = "### 🎉 很高兴能帮到你！\n\n如果还有其他问题随时问我～ 🦊"
        return title, body

    @staticmethod
    def format_feedback_no(ticket: dict) -> tuple:
        """用户不满意反馈 → 工单已升级"""
        title = f"📋 工单已升级 — {ticket['ticket_id']}"
        body = (
            f"### 😔 抱歉没能解决您的问题\n\n"
            f"已自动创建工单并通知管理员处理：\n\n"
            f"**编号**: {ticket['ticket_id']}\n"
            f"**分类**: {ticket['category']}\n"
            f"**标题**: {ticket['title']}\n"
            f"**状态**: 🔧 处理中\n\n"
            f"管理员会尽快跟进，请耐心等待 🦊\n\n"
            f"> 💡 回复「**{ticket['ticket_id']}**」可随时查看进度"
        )
        return title, body

    @staticmethod
    def format_admin_new_ticket_notification(ticket: dict) -> tuple:
        """
        发送给管理员的新工单通知。
        用户直接创建工单时触发，通知管理员有新工单待处理。
        """
        title = f"🆕 新工单 — {ticket['ticket_id']}"
        body = (
            f"### 🆕 新工单待处理\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**用户**: {ticket['creator_nick']}\n"
            f"**分类**: {ticket['category']}\n"
            f"**优先级**: {PRIORITY_LABELS.get(ticket.get('priority', 'medium'), ticket.get('priority', '中'))}\n"
            f"**标题**: {ticket['title']}\n"
            f"**状态**: {STATUS_LABELS.get(ticket['status'], ticket['status'])}\n"
            f"**创建时间**: {ticket['created_at']}\n"
        )
        if ticket.get("description"):
            body += f"\n**问题描述**:\n> {ticket['description'][:200]}"
        related = ticket.get("related_active", [])
        if related:
            label = "🚨 疑似群体故障" if ticket.get("incident_suspected") else "🔁 疑似重复报障"
            ids = "、".join(item["ticket_id"] for item in related[:5])
            body += f"\n\n**{label}**: 近期相似工单 {ids}"
        body += (
            f"\n\n---\n"
            f"📌 请尽快处理该工单\n"
            f"📋 回复「**待处理**」查看所有待处理工单\n"
            f"💬 回复「**解决 {ticket['ticket_id']} [解决方案]**」标记为已解决\n"
            f"🦊 解决后用户将自动收到通知"
        )
        return title, body

    @staticmethod
    def format_admin_feedback_notification(ticket: dict, user_nick: str,
                                           question: str = "",
                                           answer_preview: str = "") -> tuple:
        """
        发送给管理员的反馈不满意通知。
        包含工单信息 + 用户问题 + AI 回答摘要。
        """
        title = f"⚠️ 工单需处理 — {ticket['ticket_id']}"
        body = (
            f"### ⚠️ 用户反馈不满意 — 需人工处理\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**用户**: {user_nick}\n"
            f"**分类**: {ticket['category']}\n"
            f"**时间**: {ticket['created_at']}\n"
        )
        if question:
            body += f"\n**用户问题**:\n> {question[:200]}"
        if answer_preview:
            body += f"\n\n**AI 回答摘要**:\n> {answer_preview[:150]}"
        body += (
            f"\n\n---\n"
            f"📌 请尽快处理该工单\n"
            f"💬 回复「**解决 {ticket['ticket_id']} [方案]**」标记为已解决\n"
            f"🦊 处理完毕后用户将收到通知"
        )
        return title, body

    @staticmethod
    def format_archive_prompt(ticket: dict) -> tuple:
        """管理员：归档提示（工单解决后追问）"""
        title = f"📚 是否归档为经验？— {ticket['ticket_id']}"
        body = (
            f"### 📚 工单已解决，是否归档为经验？\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**标题**: {ticket['title']}\n"
            f"**分类**: {ticket['category']}\n"
            f"**处理人**: {ticket.get('resolver_nick', '')}\n\n"
            f"---\n"
            f"✅ 回复「**归档 {ticket['ticket_id']}**」→ 归档为运维经验\n"
            f"🚫 回复「**无效 {ticket['ticket_id']}**」→ 标记为无效工单（不归档）\n\n"
            f"> 💡 归档后此工单解决方案将沉淀为运维经验库"
        )
        return title, body

    @staticmethod
    def format_admin_self_ticket_confirmation(ticket: dict) -> tuple:
        """
        管理员自己创建工单时的副本确认。
        轻量级提醒，区别于发给其他管理员的完整通知。
        """
        title = f"📋 工单副本 — {ticket['ticket_id']}"
        body = (
            f"### 📋 您创建的工单已记录\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**分类**: {ticket['category']}\n"
            f"**标题**: {ticket['title']}\n"
            f"**状态**: {STATUS_LABELS.get(ticket['status'], ticket['status'])}\n"
            f"**创建时间**: {ticket['created_at']}\n"
        )
        if ticket.get("description"):
            body += f"\n**问题描述**:\n> {ticket['description'][:200]}"
        body += (
            f"\n\n---\n"
            f"💡 此消息为工单创建副本，已同时通知其他管理员\n"
            f"💬 回复「**解决 {ticket['ticket_id']} [方案]**」可直接标记为已解决"
        )
        return title, body

    @staticmethod
    def format_all_tickets_admin(tickets: list, filter_type: str = "all") -> tuple:
        """
        管理员全量工单视图，按状态分组展示。

        filter_type: "all" / "pending" / "done"
        """
        FILTER_LABELS = {
            "all":     "全部工单",
            "pending": "未处理工单",
            "done":    "已处理工单",
        }
        PENDING_STATUSES = {"open", "assigned", "in_progress"}
        DONE_STATUSES = {"resolved", "closed"}

        label = FILTER_LABELS.get(filter_type, "全部工单")

        if not tickets:
            empty_tips = {
                "all":     "还没有任何工单记录",
                "pending": "🎉 没有待处理工单，太棒了！",
                "done":    "暂无已处理工单",
            }
            return (
                f"📋 {label}",
                f"### 📋 {label}\n\n{empty_tips.get(filter_type, '暂无工单')}"
            )

        title = f"📋 {label}（共 {len(tickets)} 条）"

        # 按状态分组
        groups = {}
        for t in tickets:
            s = t["status"]
            groups.setdefault(s, []).append(t)

        # 分组展示顺序
        STATUS_ORDER = ["open", "assigned", "in_progress", "resolved", "closed"]
        STATUS_GROUP_TITLE = {
            "open":        "🔴 待处理（open）",
            "assigned":    "🟠 已分配（assigned）",
            "in_progress": "🔵 处理中（in_progress）",
            "resolved":    "🟢 已解决（resolved）",
            "closed":      "⚫ 已关闭（closed）",
        }

        lines = [f"### 📋 {label}", f"", f"共 **{len(tickets)}** 条工单", ""]

        for status in STATUS_ORDER:
            group = groups.get(status)
            if not group:
                continue
            lines.append(f"#### {STATUS_GROUP_TITLE.get(status, status)}（{len(group)} 条）")
            lines.append("")
            for t in group:
                priority_tag = ""
                if t.get("priority") == "urgent":
                    priority_tag = "🔴 "
                elif t.get("priority") == "high":
                    priority_tag = "🟠 "
                creator = t.get("creator_nick", "")
                resolver = t.get("resolver_nick", "")
                handler_info = f"创建: {creator}"
                if resolver:
                    handler_info += f" → 处理: {resolver}"
                lines.append(
                    f"- {priority_tag}**[{t['ticket_id']}]** {t['title'][:40]}"
                )
                lines.append(
                    f"  > {t['category']} | {handler_info} | {t['created_at'][:16]}"
                )
            lines.append("")

        # 快捷指令提示
        if filter_type in ("all", "pending"):
            lines.append("---")
            lines.append("💡 「**解决 TKT-xxx 解决方案**」→ 解决工单 | 「**工单 TKT-xxx**」→ 查看详情")
        else:
            lines.append("---")
            lines.append("💡 「**工单 TKT-xxx**」→ 查看详情 | 「**归档 TKT-xxx**」→ 归档为经验")

        return title, "\n".join(lines)

    @staticmethod
    def format_archived_confirmation(ticket: dict) -> tuple:
        """归档为经验确认"""
        title = f"📚 已归档为经验 — {ticket['ticket_id']}"
        body = (
            f"### 📚 工单已归档为经验！\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**标题**: {ticket['title']}\n"
            f"**分类**: {ticket['category']}\n"
        )
        if ticket.get("resolution"):
            body += f"**解决方案**: {ticket['resolution'][:200]}\n"
        body += (
            f"\n> ✅ 此工单已沉淀为运维经验，可复用于后续类似问题 🦊"
        )
        return title, body

    @staticmethod
    def format_invalid_confirmation(ticket: dict) -> tuple:
        """标记为无效确认"""
        title = f"🚫 已标记为无效工单 — {ticket['ticket_id']}"
        body = (
            f"### 🚫 工单已标记为无效\n\n"
            f"**工单编号**: {ticket['ticket_id']}\n"
            f"**标题**: {ticket['title']}\n"
            f"**分类**: {ticket['category']}\n\n"
            f"> 此工单不会归档为经验"
        )
        return title, body

    @staticmethod
    def format_reminder(pending_tickets: list, admin_nick: str = "") -> tuple:
        """主动工单提醒"""
        if not pending_tickets:
            return "📭 无待处理工单", "### 📭 当前无待处理工单\n\n所有工单都已处理完毕 🎉"

        title = f"🔔 工单提醒 — {len(pending_tickets)} 条待处理"
        lines = [
            f"### 🔔 工单提醒",
            f"",
        ]
        if admin_nick:
            lines.append(f"**{admin_nick}**，以下工单待处理：\n")

        for t in pending_tickets:
            priority_tag = ""
            if t.get("priority") == "urgent":
                priority_tag = "🔴 "
            elif t.get("priority") == "high":
                priority_tag = "🟠 "
            lines.append(
                f"{priority_tag}**[{t['ticket_id']}]** {t['title'][:40]}"
            )
            lines.append(
                f"> {t['category']} | {t['creator_nick']} | {t['created_at'][:16]}"
            )
            lines.append("")

        lines.append("---")
        lines.append(f"💬 回复「**解决 TKT-xxx 方案**」标记已解决")
        lines.append(f"📋 回复「**待处理**」查看完整队列")
        return title, "\n".join(lines)
