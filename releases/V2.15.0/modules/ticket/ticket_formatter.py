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
        if ticket.get("request_type") == "service_request":
            service_name = ticket.get("service_name") or ticket.get("service_code") or "服务申请"
            body = body.replace(
                f"**类型**: {ticket['category']}\n",
                f"**请求类型**: 服务申请\n**服务项目**: {service_name}\n**分类**: {ticket['category']}\n",
            )
        sla_lines = TicketFormatter._format_sla_lines(ticket)
        if sla_lines:
            body = body.replace("\n\n已通知管理员", "\n" + "\n".join(sla_lines) + "\n\n已通知管理员")
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
            status_label = ("待用户确认" if t["status"] == "resolved"
                            else STATUS_LABELS.get(t["status"], t["status"]))
            lines.append(
                f"- {status_icon} {prio_label} **[{t['ticket_id']}]** {t['title'][:30]}"
            )
            lines.append(f"  > {t['category']} | {status_label}"
                         f" | 负责人：{t.get('assignee_nick') or '待分派'}"
                         f" | 更新：{(t.get('updated_at') or t['created_at'])[:16]}")

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
            f"**状态**: {status_icon} "
            f"{'待用户确认' if ticket['status'] == 'resolved' else STATUS_LABELS.get(ticket['status'], ticket['status'])}",
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
        if ticket.get("service_name"):
            lines.insert(4, f"**服务项目**: {ticket['service_name']}")
        lines.extend(TicketFormatter._format_sla_lines(ticket))

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
                    "comment": "补充/处理记录",
                    "reopen": "重新打开",
                    "confirm": "用户确认恢复",
                    "withdraw": "用户撤回",
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
        if new_status == "resolved":
            label = "已解决（等待用户确认）"
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
    def format_help(is_admin: bool = False) -> tuple:
        """面向员工的服务台首页与使用帮助。"""
        title = "🧭 IT 服务台"
        body = (
            "### 🧭 IT 服务台首页\n\n"
            "直接描述故障，我会先给出安全的自助排查步骤；需要人工时会带着上下文创建工单。\n\n"
            "#### 🚀 常用入口\n"
            "- 「**服务目录**」或「**我要申请**」→ 账号、权限、设备等标准服务\n"
            "- 「**报修 + 问题描述**」→ 创建故障工单\n"
            "- 「**我的工单**」→ 查看我提交的工单\n"
            "- 「**工单 TKT-编号**」→ 查看状态、负责人、SLA 和时间线\n"
            "- 「**确认恢复 TKT-编号 评分5 评价**」→ 确认恢复并关闭\n"
            "- 「**重开 TKT-编号 未解决原因**」→ 问题仍未解决时重新处理\n"
            "- 「**转人工**」→ 将当前问题和排查记录交给运维\n"
            "- 「**网络状态**」→ 快速检查 IP、DNS、延迟和丢包\n\n"
            "#### 📋 工单说明\n"
            "工单解决后处于等待确认阶段；请确认恢复并评分，或说明原因后重开。\n"
        )
        if is_admin:
            body += (
                "\n#### 🛠️ 管理员快捷指令\n"
                "- 「**待处理**」→ 查看待处理队列\n"
                "- 「**接手 TKT-编号**」→ 接手并开始处理\n"
                "- 「**解决 TKT-编号 解决方案**」→ 标记解决并等待用户确认\n"
                "- 「**工单统计**」/「**日报**」→ 查看运营统计\n"
            )
        body += "\n> 💡 随时发送「帮助」返回本页。"
        return title, body

    @staticmethod
    def format_service_catalog(items: list, notice: str = "") -> tuple:
        """展示后台启用的结构化服务目录。"""
        title = "🧰 IT 服务目录"
        lines = ["### 🧰 IT 服务目录", ""]
        if notice:
            lines.extend([f"> ⚠️ {notice}", ""])
        if not items:
            lines.extend(["当前没有可申请的服务项目。", "请发送「报修 + 问题描述」或「转人工」。"])
            return title, "\n".join(lines)
        lines.append("请选择服务代码，并补充具体需求：\n")
        for item in items:
            code = str(item.get("code", "")).upper()
            name = item.get("name") or code
            description = item.get("description", "")
            request_type = {
                "incident": "故障事件",
                "service_request": "服务请求",
                "request": "服务请求",
            }.get(str(item.get("request_type", "incident")), "服务事项")
            assignment_group = item.get("assignment_group", "")
            instructions = item.get("instructions", "")
            response_hours = item.get("response_hours", 4)
            resolution_hours = item.get("resolution_hours", 24)
            approval = "｜需审批" if item.get("approval_required") else ""
            lines.append(f"- **{code}｜{name}**（{request_type}{approval}）")
            if description:
                lines.append(f"  > {description}")
            target = f"响应目标 {response_hours} 小时｜解决目标 {resolution_hours} 小时"
            if assignment_group:
                target += f"｜处理组 {assignment_group}"
            lines.append(f"  > {target}")
            if instructions:
                lines.append(f"  > 提交指引：{instructions}")
        lines.extend(["", "**申请格式**：`申请服务 CODE 具体说明`",
                      "示例：`申请服务 ACCESS 需要开通财务系统只读权限`"])
        return title, "\n".join(lines)

    @staticmethod
    def format_resolution_confirmed(ticket: dict, score: int = None, comment: str = "") -> tuple:
        title = f"✅ 已确认恢复 — {ticket['ticket_id']}"
        lines = [
            "### ✅ 感谢确认，工单已关闭", "",
            f"**工单编号**: {ticket['ticket_id']}",
            f"**问题**: {ticket.get('title', '')}",
        ]
        if score is not None:
            lines.append(f"**服务评分**: {'⭐' * score}（{score}/5）")
        if comment:
            lines.append(f"**评价**: {comment}")
        lines.extend(["", "如问题再次出现，可发送「报修」重新提交。"])
        return title, "\n".join(lines)

    @staticmethod
    def _format_sla_lines(ticket: dict) -> list:
        lines = []
        sla_status = ticket.get("sla_status")
        if sla_status:
            sla_label = {
                "normal": "正常", "at_risk": "即将超时", "breached": "已超时",
                "met": "已达标",
            }.get(sla_status, sla_status)
            lines.append(f"**SLA 状态**: {sla_label}")
        if ticket.get("response_due_at"):
            lines.append(f"**响应目标**: {ticket['response_due_at']}")
        if ticket.get("resolution_due_at"):
            lines.append(f"**解决目标**: {ticket['resolution_due_at']}")
        return lines

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
            f"请回复「**确认恢复 {ticket['ticket_id']} 评分5 可选评价**」确认并关闭；\n"
            f"如仍未解决，请回复「**重开 {ticket['ticket_id']} 未解决原因**」。\n\n"
            f"> 💡 回复「**我的工单**」查看所有工单"
        )
        return title, body

    @staticmethod
    def format_user_status_notification(ticket: dict, action: str, operator_nick: str = "", detail: str = "") -> tuple:
        """创建者可读的统一状态通知，覆盖分派、处理、解决、关闭和重开。"""
        status = ticket.get("status", "open")
        label = STATUS_LABELS.get(status, status)
        if status == "resolved":
            label = "运维已解决，等待你确认"
        icon = TicketFormatter._status_icon(status)
        action_labels = {
            "assign": "已分派", "progress": "开始处理", "resolved": "已解决",
            "closed": "已关闭", "reopen": "已重新打开",
        }
        title = f"{icon} 工单{action_labels.get(action, '状态更新')} — {ticket['ticket_id']}"
        lines = [
            f"### {icon} 您的工单{action_labels.get(action, '状态已更新')}", "",
            f"**工单编号**: {ticket['ticket_id']}", f"**标题**: {ticket['title']}",
            f"**当前状态**: {label}",
        ]
        if ticket.get("assignee_nick"):
            lines.append(f"**负责人**: {ticket['assignee_nick']}")
        if operator_nick:
            lines.append(f"**本次操作人**: {operator_nick}")
        if detail:
            lines.append(f"\n**处理说明**:\n> {detail[:500]}")
        if ticket.get("resolution"):
            lines.append(f"\n**解决方案**:\n> {ticket['resolution'][:500]}")
        lines.extend(TicketFormatter._format_sla_lines(ticket))
        next_steps = {
            "assigned": "负责人已收到工单，请等待处理进展。",
            "in_progress": "工单正在处理中，完成后会再次通知您。",
            "resolved": (
                f"请回复「确认恢复 {ticket['ticket_id']} 评分5 可选评价」确认并关闭；"
                f"如仍未恢复，请回复「重开 {ticket['ticket_id']} 未解决原因」。"
            ),
            "closed": "该工单流程已结束，可发送“我的工单”查看历史。",
            "open": "工单已重新进入待处理队列。",
        }
        lines.extend(["", next_steps.get(status, "可发送“我的工单”查看最新状态。"),
                      "", f"> 📋 回复「工单 {ticket['ticket_id']}」查看完整详情"])
        return title, "\n".join(lines)

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
    def format_admin_urge_notification(ticket: dict, user_nick: str = "",
                                       reason: str = "") -> tuple:
        """员工催办通知，避免被误标为新工单。"""
        title = f"⏰ 用户催办 — {ticket['ticket_id']}"
        lines = [
            "### ⏰ 用户催办工单", "",
            f"**工单编号**: {ticket['ticket_id']}",
            f"**用户**: {user_nick or ticket.get('creator_nick', '')}",
            f"**标题**: {ticket.get('title', '')}",
            f"**当前状态**: {STATUS_LABELS.get(ticket.get('status'), ticket.get('status', ''))}",
            f"**负责人**: {ticket.get('assignee_nick') or '待分派'}",
        ]
        if reason:
            lines.extend(["", f"**催办原因**:\n> {reason[:300]}"])
        lines.extend(["", f"> 请发送「工单 {ticket['ticket_id']}」查看详情并尽快跟进。"])
        return title, "\n".join(lines)

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
