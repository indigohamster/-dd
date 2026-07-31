# ============================================================
# 工单意图解析器 — 从用户消息中识别工单相关操作
# ============================================================

import re
from typing import Optional

# 工单编号正则
TICKET_ID_PATTERN = re.compile(r"(TKT-\d{8}-\d{3,})(?!\d)", re.IGNORECASE)
TICKET_FLEX_PATTERN = re.compile(
    r"#?TKT\s*[-\s]?\s*(\d{8})\s*[-\s]?\s*(\d{3,})(?!\d)",
    re.IGNORECASE,
)


class TicketIntent:
    """工单操作意图"""

    CREATE = "create"           # 创建工单
    LIST_MINE = "list_mine"     # 查看我的工单
    LIST_PENDING = "list_pending"  # 查看待处理工单（管理员）
    LIST_ALL = "list_all"       # 查看全部工单（管理员）
    VIEW = "view"               # 查看工单详情
    RESOLVE = "resolve"         # 解决工单
    CLOSE = "close"             # 关闭工单
    REOPEN = "reopen"           # 用户确认未解决，重新打开
    START = "start"             # 管理员接手并开始处理
    ARCHIVE = "archive"         # 归档为经验（管理员）
    INVALID = "invalid"         # 标记为无效工单（管理员）
    REMINDER = "reminder"       # 主动工单提醒（管理员）
    BIND_STAFF_ID = "bind_staff_id"  # 绑定企业工号/staffId（管理员）
    STATS = "stats"             # 查看统计
    DAILY = "daily"             # 每日工单摘要
    FEEDBACK_YES = "feedback_yes"  # 用户回答"是"——满意
    FEEDBACK_NO = "feedback_no"    # 用户回答"否"——不满意
    HELP = "help"                   # 服务台首页/帮助
    SERVICE_CATALOG = "service_catalog"  # 查看服务目录
    SERVICE_APPLY = "service_apply"      # 创建结构化服务请求
    CONFIRM_RESOLUTION = "confirm_resolution"  # 用户确认恢复并可评分
    ADD_COMMENT = "add_comment"          # 创建者补充公开信息
    URGE = "urge"                        # 创建者催办
    WITHDRAW = "withdraw"                # 创建者撤回未解决工单
    NONE = "none"               # 非工单消息


class ParsedTicketAction:
    """解析后的工单操作"""

    def __init__(self, intent: str, ticket_id: str = None, extra: str = None):
        self.intent = intent
        self.ticket_id = ticket_id
        self.extra = extra          # 附加内容（如问题描述、解决方案等）

    def __repr__(self):
        return f"ParsedTicketAction(intent={self.intent}, ticket_id={self.ticket_id}, extra={self.extra})"


class TicketParser:
    """从用户消息中解析工单操作意图"""

    # ── 创建工单触发词 ──
    CREATE_KEYWORDS = [
        "提交工单", "创建工单", "新建工单", "报修", "上报问题",
        "提个工单", "帮我报修", "提交一个问题", "有问题要报",
        "直接报修", "转人工",
    ]

    # ── 查看我的工单 ──
    LIST_MINE_KEYWORDS = [
        "我的工单", "我提交的工单", "我的报修", "工单列表",
        "查看工单", "查工单",
    ]

    # ── 管理员：查看待处理 ──
    LIST_PENDING_KEYWORDS = [
        "待处理", "未处理", "处理中工单",
        "待解决工单", "工单队列",
    ]

    # ── 管理员：查看全部工单（含已处理/未处理/所有） ──
    LIST_ALL_KEYWORDS = [
        "所有工单", "全部工单", "全量工单",
        "已处理工单", "已完成工单", "已解决工单", "历史工单",
        "查所有", "查全部", "工单总览", "工单全览",
    ]

    # ── 关闭工单 ──
    CLOSE_KEYWORDS = [
        "关闭工单", "完结工单", "关闭", "解决了", "搞定了",
        "已经好了", "可以关了", "修好了",
    ]

    REOPEN_KEYWORDS = ["重开", "重新打开", "问题未解决", "还没解决", "仍未解决"]

    # ── 开始处理 ──
    START_KEYWORDS = [
        "开始处理", "接手", "处理工单",
    ]

    # ── 归档为经验（管理员） ──
    ARCHIVE_KEYWORDS = [
        "归档", "归档为经验", "存为经验", "保存经验",
    ]

    # ── 标记为无效（管理员） ──
    INVALID_KEYWORDS = [
        "无效", "无效工单", "不归档", "不需要归档",
    ]

    # ── 主动工单提醒（管理员） ──
    REMINDER_KEYWORDS = [
        "工单提醒", "提醒工单", "待办提醒", "未处理提醒",
    ]

    # ── 绑定企业工号/staffId（管理员） ──
    BIND_STAFF_ID_KEYWORDS = [
        "绑定工号", "绑定staff", "设置工号", "设置staff",
        "绑定ID", "绑定id", "设置ID", "设置id",
    ]

    # ── 统计 ──
    STATS_KEYWORDS = [
        "工单统计", "工单报告", "报修统计", "运维统计",
        "今日工单", "本周工单", "本月工单",
        "工单数量", "工单概况",
    ]

    # ── 每日工单摘要 ──
    DAILY_KEYWORDS = [
        "每日工单", "今日摘要", "工单日报", "日报",
        "工单汇总", "工单概要", "运维修理日报",
    ]

    HELP_KEYWORDS = {
        "帮助", "菜单", "使用帮助", "工单帮助", "服务台", "服务台首页",
        "能做什么", "你能做什么", "怎么用",
    }
    SERVICE_CATALOG_KEYWORDS = {
        "服务目录", "服务列表", "我要申请", "我要申请服务", "申请服务",
    }

    NEGATED_CREATE_PHRASES = (
        "不创建工单", "不要创建工单", "不想创建工单", "无需创建工单",
        "不用创建工单", "不提交工单", "不要提交工单", "不想提交工单",
        "不要建单", "不想建单", "先不要建单", "不要报修", "不想报修",
    )

    # ── 反馈回复检测（"是"/"否"等单字或短语） ──
    FEEDBACK_YES_KEYWORDS = [
        "是", "是的", "是的有", "有", "有用", "有帮助",
        "解决了", "搞定了", "可以", "好了", "行了",
        "yes", "y", "ok",
    ]
    FEEDBACK_NO_KEYWORDS = [
        "否", "不是", "没有", "没用", "无", "不可以",
        "不行", "没解决", "没搞定", "不对",
        "no", "n",
    ]

    @classmethod
    def parse(cls, content: str, user_id: str = "", is_admin: bool = False) -> ParsedTicketAction:
        """
        解析用户消息，返回 ParsedTicketAction。

        优先级：
        1. 每日工单摘要
        2. 统计指令
        3. 包含工单编号的消息（查看/关闭/解决/归档/无效）
        4. 查看全部工单（管理员）
        5. 查看待处理队列
        6. 主动工单提醒
        7. 绑定企业工号
        8. 纯关闭意图（不带编号）
        9. 查看我的工单
        10. 创建工单
        11. 非工单消息
        """
        content_stripped = content.strip()

        # ── 服务台入口（使用精确短语，避免普通咨询误触发） ──
        if content_stripped in cls.HELP_KEYWORDS:
            return ParsedTicketAction(intent=TicketIntent.HELP)

        # ── 结构化服务申请：申请服务 CODE 说明 ──
        service_match = re.match(
            r"^申请服务\s+([A-Za-z0-9_-]+)(?:\s+(.+))?$",
            content_stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if service_match:
            return ParsedTicketAction(
                intent=TicketIntent.SERVICE_APPLY,
                extra={
                    "service_code": service_match.group(1).upper(),
                    "description": (service_match.group(2) or "").strip(),
                },
            )
        if content_stripped in cls.SERVICE_CATALOG_KEYWORDS:
            return ParsedTicketAction(intent=TicketIntent.SERVICE_CATALOG)

        # ── 1. 每日工单摘要指令 ──
        for kw in cls.DAILY_KEYWORDS:
            daily_match = re.fullmatch(
                rf"{re.escape(kw)}(?:\s+(\d{{4}}-\d{{2}}-\d{{2}}))?",
                content_stripped,
            )
            if daily_match:
                return ParsedTicketAction(
                    intent=TicketIntent.DAILY, extra=daily_match.group(1),
                )

        # ── 2. 统计指令 ──
        stats_suffix = re.fullmatch(
            r"(工单统计|工单报告|报修统计|运维统计|工单数量|工单概况)\s*(今日|本周|本月)?",
            content_stripped,
        )
        if stats_suffix:
            period_word = stats_suffix.group(2) or "今日"
            period = {"本周": "week", "本月": "month"}.get(period_word, "today")
            return ParsedTicketAction(intent=TicketIntent.STATS, extra=period)
        for kw in cls.STATS_KEYWORDS:
            if content_stripped == kw:
                period = "today"
                if "本周" in content_stripped:
                    period = "week"
                elif "本月" in content_stripped:
                    period = "month"
                return ParsedTicketAction(intent=TicketIntent.STATS, extra=period)

        # ── 3. 包含工单编号的消息 ──
        ticket_id = cls._extract_ticket_id(content_stripped)
        if ticket_id:
            # 用户确认恢复，可同时提交 1-5 分及文字评价。
            if any(kw in content_stripped for kw in ("确认恢复", "确认已恢复", "确认解决", "确认已解决")):
                score_match = re.search(r"(?:评分\s*(\d+)|(\d+)\s*分)", content_stripped)
                score = int(score_match.group(1) or score_match.group(2)) if score_match else None
                comment = TICKET_ID_PATTERN.sub("", content_stripped)
                comment = TICKET_FLEX_PATTERN.sub("", comment)
                comment = re.sub(r"确认(?:已)?(?:恢复|解决)", "", comment)
                comment = re.sub(r"(?:评分\s*\d+|\d+\s*分)", "", comment)
                comment = comment.strip(" ：:,，。")
                return ParsedTicketAction(
                    intent=TicketIntent.CONFIRM_RESOLUTION,
                    ticket_id=ticket_id,
                    extra={"score": score, "comment": comment},
                )
            if re.match(r"^\s*(?:补充|追加|留言)", content_stripped):
                detail = TICKET_ID_PATTERN.sub("", content_stripped)
                detail = TICKET_FLEX_PATTERN.sub("", detail)
                detail = re.sub(r"^\s*(?:补充|追加|留言)(?:工单)?\s*", "", detail).strip(" ：:,，")
                return ParsedTicketAction(TicketIntent.ADD_COMMENT, ticket_id, detail)
            if re.match(r"^\s*(?:催办|催一下|加急)", content_stripped):
                detail = TICKET_ID_PATTERN.sub("", content_stripped)
                detail = TICKET_FLEX_PATTERN.sub("", detail)
                detail = re.sub(r"^\s*(?:催办|催一下|加急)(?:工单)?\s*", "", detail).strip(" ：:,，")
                return ParsedTicketAction(TicketIntent.URGE, ticket_id, detail)
            if re.match(r"^\s*(?:(?:撤回|撤销)(?:工单)?|取消工单)", content_stripped):
                detail = TICKET_ID_PATTERN.sub("", content_stripped)
                detail = TICKET_FLEX_PATTERN.sub("", detail)
                detail = re.sub(
                    r"^\s*(?:(?:撤回|撤销)(?:工单)?|取消工单)\s*", "", detail,
                ).strip(" ：:,，")
                return ParsedTicketAction(TicketIntent.WITHDRAW, ticket_id, detail)
            # 重开必须优先于“解决”匹配，因为“未解决”也包含“解决”二字
            for kw in cls.REOPEN_KEYWORDS:
                if kw in content_stripped:
                    detail = TICKET_ID_PATTERN.sub("", content_stripped)
                    detail = TICKET_FLEX_PATTERN.sub("", detail)
                    detail = re.sub(r"(?:重开|重新打开|问题未解决|还没解决|仍未解决)", "", detail)
                    return ParsedTicketAction(
                        intent=TicketIntent.REOPEN, ticket_id=ticket_id,
                        extra=detail.strip(" ：:,，。"),
                    )
            for kw in cls.START_KEYWORDS:
                if kw in content_stripped:
                    return ParsedTicketAction(intent=TicketIntent.START, ticket_id=ticket_id, extra=content_stripped)
            # 关闭意图（带编号）
            for kw in cls.CLOSE_KEYWORDS:
                if kw in content_stripped:
                    return ParsedTicketAction(intent=TicketIntent.CLOSE, ticket_id=ticket_id)

            # 解决意图（带编号）
            if "解决" in content_stripped or "修复" in content_stripped:
                return ParsedTicketAction(intent=TicketIntent.RESOLVE, ticket_id=ticket_id, extra=content_stripped)

            # 否定归档必须先于正向“归档”，避免“不归档”执行反向操作。
            if re.search(r"(?:不|不要|无需|取消)(?:需要)?归档", content_stripped):
                return ParsedTicketAction(intent=TicketIntent.INVALID, ticket_id=ticket_id)
            for kw in cls.INVALID_KEYWORDS:
                if kw in content_stripped:
                    return ParsedTicketAction(intent=TicketIntent.INVALID, ticket_id=ticket_id)

            # 归档为经验（带编号）
            for kw in cls.ARCHIVE_KEYWORDS:
                if kw in content_stripped:
                    return ParsedTicketAction(intent=TicketIntent.ARCHIVE, ticket_id=ticket_id)

            # 默认：查看工单详情
            return ParsedTicketAction(intent=TicketIntent.VIEW, ticket_id=ticket_id)

        # ── V2.17.1-A：员工工单自然指代（明确编号规则仍优先） ──
        if re.fullmatch(r"(?:查看|查|看看)?我的(?:未处理|待处理)工单", content_stripped):
            return ParsedTicketAction(
                intent=TicketIntent.LIST_MINE,
                extra={"status_filter": "pending"},
            )

        reference_match = re.fullmatch(
            r"(?:(查看|看看|打开|关闭|完结))?(?:第一个|第1个)(?:工单)?",
            content_stripped,
        )
        if reference_match:
            action = reference_match.group(1) or "查看"
            intent = TicketIntent.CLOSE if action in ("关闭", "完结") else TicketIntent.VIEW
            return ParsedTicketAction(
                intent=intent,
                extra={"ticket_reference": {"type": "ordinal", "value": 1}},
            )

        recent_match = re.fullmatch(
            r"(?:(查看|看看|打开|关闭|完结))?(?:最近|最新)(?:的|一个)?工单",
            content_stripped,
        )
        if recent_match:
            action = recent_match.group(1) or "查看"
            intent = TicketIntent.CLOSE if action in ("关闭", "完结") else TicketIntent.VIEW
            return ParsedTicketAction(
                intent=intent,
                extra={"ticket_reference": {"type": "recent"}},
            )

        # ── 4. 查看全部工单（管理员） ──
        for kw in cls.LIST_ALL_KEYWORDS:
            if kw in content_stripped:
                # 识别过滤类型：已处理 / 未处理 / 全部
                if any(w in content_stripped for w in ("已处理", "已完成", "已解决", "历史")):
                    filter_type = "done"
                elif any(w in content_stripped for w in ("未处理", "待处理", "待解决")):
                    filter_type = "pending"
                else:
                    filter_type = "all"
                return ParsedTicketAction(intent=TicketIntent.LIST_ALL, extra=filter_type)

        # ── 5. 查看待处理队列 ──
        for kw in cls.LIST_PENDING_KEYWORDS:
            if kw in content_stripped:
                return ParsedTicketAction(intent=TicketIntent.LIST_PENDING)

        # ── 6. 主动工单提醒 ──
        for kw in cls.REMINDER_KEYWORDS:
            if kw in content_stripped:
                return ParsedTicketAction(intent=TicketIntent.REMINDER)

        # ── 7. 绑定企业工号/staffId（管理员） ──
        for kw in cls.BIND_STAFF_ID_KEYWORDS:
            if kw in content_stripped:
                # 提取工号：去除关键词后剩余部分即工号
                staff_id = content_stripped.replace(kw, "").strip()
                # 去除常见分隔符
                staff_id = re.sub(r"^[：:\s\-]+|[：:\s\-]+$", "", staff_id)
                if staff_id:
                    return ParsedTicketAction(
                        intent=TicketIntent.BIND_STAFF_ID, extra=staff_id
                    )
                # 只输入了关键词，没有工号，仍返回意图（handler 会提示输入）
                return ParsedTicketAction(
                    intent=TicketIntent.BIND_STAFF_ID, extra=""
                )

        # ── 8. 纯关闭意图（不带编号） ──
        if content_stripped in ("解决了", "搞定了", "关闭工单", "完结工单", "修好了", "已经好了"):
            return ParsedTicketAction(intent=TicketIntent.CLOSE)

        # ── 9. 查看我的工单 ──
        for kw in cls.LIST_MINE_KEYWORDS:
            if kw in content_stripped:
                return ParsedTicketAction(intent=TicketIntent.LIST_MINE)

        # ── 10. 创建工单意图 ──
        if any(phrase in content_stripped for phrase in cls.NEGATED_CREATE_PHRASES):
            return ParsedTicketAction(intent=TicketIntent.NONE)
        for kw in cls.CREATE_KEYWORDS:
            if kw in content_stripped:
                return ParsedTicketAction(intent=TicketIntent.CREATE, extra=content_stripped)

        # ── 11. 非工单消息 ──
        return ParsedTicketAction(intent=TicketIntent.NONE)

    @classmethod
    def _extract_ticket_id(cls, content: str) -> Optional[str]:
        """从消息中提取工单编号"""
        m = TICKET_ID_PATTERN.search(content)
        if m:
            return m.group(1).upper()
        m = TICKET_FLEX_PATTERN.search(content)
        if m:
            return f"TKT-{m.group(1)}-{m.group(2)}".upper()
        return None

    @classmethod
    def detect_feedback(cls, content: str) -> Optional[str]:
        """
        检测用户消息是否为对反馈提示的回复。
        返回 TicketIntent.FEEDBACK_YES / FEEDBACK_NO，或 None（非反馈回复）。

        仅在消息较短（≤10字）时触发，避免长文本误判。
        """
        stripped = content.strip()
        if len(stripped) > 10:
            return None  # 长消息不判定为反馈回复

        lower = stripped.lower()

        # 精确匹配"是"/"否"系列
        for kw in cls.FEEDBACK_YES_KEYWORDS:
            if lower == kw:
                return TicketIntent.FEEDBACK_YES

        for kw in cls.FEEDBACK_NO_KEYWORDS:
            if lower == kw:
                return TicketIntent.FEEDBACK_NO

        return None
