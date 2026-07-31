# ============================================================
# 事件处理器（审批 / 日历 / 通讯录 / 考勤 等）
# ============================================================
#
# 处理钉钉平台推送的事件通知。
# 事件类型通过 EventHandler.process() 回调接收，
# 具体订阅哪些事件需要在钉钉开放平台后台配置。
#
# 配置路径：钉钉开放平台 → 你的应用 → 事件与回调 → 订阅事件
# ============================================================

import json
import logging
import time

from dingtalk_stream import AckMessage, EventHandler, EventMessage


class BotEventHandler(EventHandler):
    """
    企业事件处理器。

    继承自 dingtalk_stream.EventHandler，处理所有订阅的企业事件。
    使用 register_all_event_handler() 注册，自动接收所有事件，
    在 process() 中按 event_type 分发处理。
    """

    def __init__(self, logger: logging.Logger, config: dict):
        super().__init__()
        self.logger = logger
        self.events_config = config.get("events", {})
        self._logger_ref = logger  # EventHandler 基类使用 self.logger

    async def process(self, event: EventMessage) -> tuple:
        """
        处理接收到的事件。

        EventMessage 结构：
            event.headers.event_type          事件类型标识
            event.headers.event_id            事件唯一 ID
            event.headers.event_born_time     事件产生时间（毫秒时间戳）
            event.headers.event_corp_id       企业 ID
            event.headers.event_unified_app_id 应用 ID
            event.data                        事件数据体（dict）
        """
        # 检查事件处理总开关
        if not self.events_config.get("enabled", True):
            return AckMessage.STATUS_OK, "OK"

        # 订阅事件白名单过滤（空列表 = 接收全部）
        subscribed = self.events_config.get("subscribed_events", [])
        event_type = event.headers.event_type if event.headers else "unknown"
        if subscribed and event_type not in subscribed:
            return AckMessage.STATUS_OK, "OK"

        # 计算事件延迟
        born_time = event.headers.event_born_time if event.headers else 0
        delay_ms = int(time.time() * 1000) - born_time if born_time else 0

        self.logger.info(
            f"收到事件 | 类型={event_type} | "
            f"事件ID={event.headers.event_id if event.headers else ''} | "
            f"延迟={delay_ms}ms | "
            f"企业ID={event.headers.event_corp_id if event.headers else ''}"
        )

        # 安全打印事件数据
        try:
            data_preview = json.dumps(event.data, ensure_ascii=False, default=str)
            if len(data_preview) > 500:
                data_preview = data_preview[:500] + "..."
        except Exception:
            data_preview = str(event.data)[:500]

        self.logger.debug(f"事件数据: {data_preview}")

        # ================================================================
        # 在这里编写你的事件处理逻辑 ↓↓↓
        # ================================================================

        # 按事件类型分发
        handler_map = {
            # --- 通讯录事件 ---
            "user_add_org": self._on_user_join,
            "user_modify_org": self._on_user_modify,
            "user_leave_org": self._on_user_leave,
            # --- 群事件 ---
            "chat_add_user": self._on_chat_add_user,
            "chat_remove_user": self._on_chat_remove_user,
            "chat_update_title": self._on_chat_update_title,
            # --- 审批事件 ---
            "approval_instance": self._on_approval_change,
            # --- 日历事件 ---
            "calendar_event_create": self._on_calendar_create,
            "calendar_event_change": self._on_calendar_change,
            # --- 考勤事件 ---
            "check_in": self._on_check_in,
        }

        handler = handler_map.get(event_type)
        if handler:
            try:
                await handler(event)
            except Exception as e:
                self.logger.error(f"事件处理异常 [{event_type}]: {e}", exc_info=True)
        else:
            self.logger.info(f"未注册的事件类型: {event_type}，已忽略")

        # ================================================================
        # 在这里编写你的事件处理逻辑 ↑↑↑
        # ================================================================

        return AckMessage.STATUS_OK, "OK"

    # ----------------------------------------------------------------
    # 以下为各类事件的示例处理方法
    # 根据实际需求修改或扩展
    # ----------------------------------------------------------------

    async def _on_user_join(self, event: EventMessage):
        """员工入职事件"""
        data = event.data or {}
        user_id = data.get("userId", "")
        name = data.get("name", "")
        dept_ids = data.get("deptIdList", [])
        self.logger.info(f"[入职] 员工: {name}({user_id}) | 部门: {dept_ids}")

    async def _on_user_modify(self, event: EventMessage):
        """员工信息变更事件"""
        data = event.data or {}
        user_id = data.get("userId", "")
        self.logger.info(f"[信息变更] 员工ID: {user_id}")

    async def _on_user_leave(self, event: EventMessage):
        """员工离职事件"""
        data = event.data or {}
        user_id = data.get("userId", "")
        self.logger.warning(f"[离职] 员工ID: {user_id}")

    async def _on_chat_add_user(self, event: EventMessage):
        """群成员增加事件"""
        data = event.data or {}
        chat_id = data.get("chatId", "")
        self.logger.info(f"[群成员增加] 群ID: {chat_id}")

    async def _on_chat_remove_user(self, event: EventMessage):
        """群成员移除事件"""
        data = event.data or {}
        chat_id = data.get("chatId", "")
        self.logger.info(f"[群成员移除] 群ID: {chat_id}")

    async def _on_chat_update_title(self, event: EventMessage):
        """群名称变更事件"""
        data = event.data or {}
        chat_id = data.get("chatId", "")
        new_title = data.get("title", "")
        self.logger.info(f"[群名变更] 群ID: {chat_id} | 新名称: {new_title}")

    async def _on_approval_change(self, event: EventMessage):
        """审批实例变更事件"""
        data = event.data or {}
        process_code = data.get("processCode", "")
        status = data.get("result", "unknown")
        self.logger.info(f"[审批变更] 流程: {process_code} | 状态: {status}")

    async def _on_calendar_create(self, event: EventMessage):
        """日历事件创建"""
        data = event.data or {}
        summary = data.get("summary", "")
        self.logger.info(f"[日历事件] 创建: {summary}")

    async def _on_calendar_change(self, event: EventMessage):
        """日历事件变更"""
        data = event.data or {}
        summary = data.get("summary", "")
        self.logger.info(f"[日历事件] 变更: {summary}")

    async def _on_check_in(self, event: EventMessage):
        """考勤打卡事件"""
        data = event.data or {}
        user_id = data.get("userId", "")
        work_date = data.get("workDate", "")
        self.logger.info(f"[考勤] 用户: {user_id} | 日期: {work_date}")
