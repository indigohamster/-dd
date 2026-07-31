# ============================================================
# 聊天消息处理器（群聊 + 单聊 + 知识库 + AI 对话）
# ============================================================
#
# 处理钉钉机器人的所有消息回调：
#   - 群聊中 @机器人 的消息 (conversationType = 2)
#   - 单聊中发给机器人的消息 (conversationType = 1)
#
# 回复模式：
#   1. 召唤专家触发词（"召唤U9专家"/"召唤运维专家"/"召唤售后专家"）
#      → 设置会话专家状态，后续对话均基于此专家范围
#   2. 旧触发词（"搜"/"查"/"找"）→ 返回知识库原始搜索结果
#   3. 普通消息 → 若有激活专家则按专家范围搜知识库 + DeepSeek 生成回答
#                 否则搜全部默认知识库 + DeepSeek 生成回答
# ============================================================

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from dingtalk_stream import AckMessage, CallbackMessage, ChatbotHandler, ChatbotMessage

from utils.ima_client import IMAClient
from utils.deepseek_client import DeepSeekClient, DeepSeekAPIError
from utils.codebuddy_client import CodeBuddyClient
from utils.dingtalk_api import DingTalkAPI
from handlers.knowledge_handler import (
    match_expert_trigger,
    extract_query,
    search_and_format,
    strip_html,
)
from utils.console import bot_status
from utils.message_logger import msg_logger
from modules.ticket import TicketManager, TicketParser, TicketFormatter, TriageEngine
from modules.ticket.ticket_manager import STATUS_LABELS
from modules.ticket.ticket_parser import TicketIntent, ParsedTicketAction


class BotMessageHandler(ChatbotHandler):
    """
    机器人消息处理器。

    继承自 dingtalk_stream.ChatbotHandler，处理群聊 @bot 消息和单聊消息。
    支持集成 IMA 知识库搜索 + DeepSeek AI 对话 + 会话级专家状态记忆。
    """

    # 会话类型常量
    CONV_TYPE_P2P = "1"   # 单聊
    CONV_TYPE_GROUP = "2"  # 群聊

    # 会话专家状态过期时间（秒），30 分钟无交互自动清除
    EXPERT_STATE_TTL = 1800

    def __init__(self, logger: logging.Logger, config: dict):
        super().__init__()
        self.logger = logger
        self.config = config
        self.chat_config = config.get("chat", {})
        self._logger_ref = logger  # EventHandler 基类使用 self.logger

        # 会话级专家状态：conversation_id -> {trigger, timestamp}
        self.expert_states = {}
        self.__expert_lock__ = threading.Lock()

        # 已见过的会话 ID（用于首次对话引导）
        self._seen_convs = set()
        self.__seen_convs_lock__ = threading.Lock()
        self._SEEN_CONVS_MAX = 10000  # 防止内存泄漏上限

        # 初始化 IMA 客户端（如果配置了）
        self.ima_client = None
        self.ima_config = {}
        ima_config = config.get("ima", {})
        if ima_config.get("enabled", False):
            client_id = ima_config.get("client_id", "")
            api_key = ima_config.get("api_key", "")
            if client_id and api_key:
                self.ima_client = IMAClient(client_id, api_key)
                self.ima_config = ima_config
                self.logger.info("IMA 知识库搜索已启用")
            else:
                self.logger.warning("IMA 已启用但缺少凭证（client_id/api_key），知识库搜索不可用")

        # 初始化 DeepSeek 客户端（如果配置了）
        self.llm_client = None
        self.llm_config = {}
        llm_config = config.get("llm", {})
        if llm_config.get("enabled", False):
            api_key = llm_config.get("api_key", "")
            if api_key:
                self.llm_client = DeepSeekClient(
                    api_key=api_key,
                    model=llm_config.get("model", "deepseek-chat"),
                    base_url=llm_config.get("base_url", "https://api.deepseek.com"),
                    chat_completions_path=llm_config.get("chat_completions_path", "/v1/chat/completions"),
                    temperature=llm_config.get("temperature", 0.3),
                    max_tokens=llm_config.get("max_tokens", 1024),
                    timeout=llm_config.get("timeout", 30),
                )
                self.llm_config = llm_config
                self.logger.info(f"DeepSeek LLM 已启用 (model={llm_config.get('model', 'deepseek-chat')})")
            else:
                self.logger.warning("LLM 已启用但缺少 api_key，AI 对话不可用")

        # 初始化 CodeBuddy 客户端（如果配置了）
        self.cb_client = None
        self.cb_config = {}
        cb_config = config.get("codebuddy", {})
        if cb_config.get("enabled", False):
            password = cb_config.get("password", "")
            if password:
                self.cb_client = CodeBuddyClient(
                    password=password,
                    host=cb_config.get("host", "127.0.0.1"),
                    port=cb_config.get("port", 8080),
                    timeout=cb_config.get("timeout", 90),
                )
                self.cb_config = cb_config
                self.logger.info("CodeBuddy AI 已启用 (本地 HTTP API)")
            else:
                self.logger.warning("CodeBuddy 已启用但缺少 password，CodeBuddy AI 不可用")

        # 会话级 AI 引擎覆盖：conv_id -> {engine, timestamp}
        self._ai_engine_overrides: dict = {}
        self.__engine_lock__ = threading.Lock()
        self._ENGINE_OVERRIDE_TTL = 1800

        # 反馈状态追踪：conv_id -> {question, answer_preview, has_kb, category, ticket_id, timestamp}
        # AI 回答后记录，用于检测用户"是/否"反馈
        self._pending_feedback: dict = {}
        self.__feedback_lock__ = threading.Lock()
        self._FEEDBACK_TTL = 1800  # 30 分钟过期

        # 附件暂存：conv_id -> {file_name, file_type, media_id, save_path, timestamp}
        self._pending_attachments: dict = {}
        self.__attachment_lock__ = threading.Lock()
        self._ATTACHMENT_TTL = 3600  # 1 小时过期
        threading.Thread(
            target=self._attachment_cleanup_loop,
            name="attachment-cleaner",
            daemon=True,
        ).start()

        # 初始化钉钉 OpenAPI 客户端（用于发送文件/图片等富媒体消息）
        self.dtalk_api = None
        creds = config.get("credentials", {})
        if creds.get("client_id") and creds.get("client_secret"):
            self.dtalk_api = DingTalkAPI(
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                agent_id=str(creds.get("agent_id", "")),
            )
            self.logger.info("钉钉 OpenAPI 客户端已初始化（支持文件/图片发送）")

        # 初始化工单系统
        ticket_cfg = config.get("ticket", {})
        project_root = os.path.dirname(os.path.dirname(__file__))
        configured_db_path = os.path.expandvars(
            ticket_cfg.get("database_path", "data/tickets.db")
        )
        if not os.path.isabs(configured_db_path):
            configured_db_path = os.path.join(project_root, configured_db_path)
        configured_backup_dir = os.path.expandvars(
            ticket_cfg.get("backup_dir", "data/backups")
        )
        if not os.path.isabs(configured_backup_dir):
            configured_backup_dir = os.path.join(project_root, configured_backup_dir)
        self.ticket_mgr = TicketManager(
            db_path=configured_db_path,
            logger=logger,
            backup_enabled=ticket_cfg.get("backup_enabled", True),
            backup_dir=configured_backup_dir,
            backup_retention_days=ticket_cfg.get("backup_retention_days", 30),
            integrity_check=ticket_cfg.get("integrity_check", True),
            busy_timeout_ms=ticket_cfg.get("busy_timeout_ms", 5000),
            followup_delay_hours=ticket_cfg.get("followup_delay_hours", 24),
        )
        self.ticket_enabled = ticket_cfg.get("enabled", True)
        self.ticket_admins = ticket_cfg.get("admins", [])
        triage_cfg = ticket_cfg.get("triage", {})
        self.triage_engine = TriageEngine(
            enabled=triage_cfg.get("enabled", True),
            min_description_length=triage_cfg.get("min_description_length", 30),
            ttl_seconds=triage_cfg.get("ttl_seconds", 1800),
        )
        # 自动添加配置中的管理员
        for admin in self.ticket_admins:
            uid, unick = admin.get("user_id", ""), admin.get("user_nick", "")
            if uid:
                self.ticket_mgr.add_admin(uid, unick)
        if self.ticket_enabled:
            self.logger.info(
                f"工单系统已启用 (SQLite={self.ticket_mgr.db_path}, "
                f"schema={self.ticket_mgr.get_schema_version()})"
            )

    # ------------------------------------------------------------------
    # 首次对话引导
    # ------------------------------------------------------------------

    def _send_welcome(self, msg: ChatbotMessage, info: dict):
        """
        新会话首次对话时，发送使用引导。
        """
        conv_id = info["conv_id"]
        with self.__seen_convs_lock__:
            if conv_id in self._seen_convs:
                return
            # 超过上限时自动淘汰最早的
            if len(self._seen_convs) >= self._SEEN_CONVS_MAX:
                # set 不支持 pop(0)，转为 list 淘汰前一半
                old_items = list(self._seen_convs)[:self._SEEN_CONVS_MAX // 2]
                for item in old_items:
                    self._seen_convs.discard(item)
            self._seen_convs.add(conv_id)
        self.logger.info(f"新会话首次对话: {conv_id[:20]}...")

        expert_triggers = self.ima_config.get("expert_triggers", [])
        expert_lines = []
        for t in expert_triggers:
            desc = t.get("description", "")
            expert_lines.append(f"- **{t['phrase']}** — {desc}" if desc else f"- **{t['phrase']}**")

        if expert_lines:
            expert_section = "\n\n### 🔧 召唤专家\n" + "\n".join(expert_lines)
            expert_section += (
                "\n\n> 💡 召唤后自动基于该专家知识库回答。\n"
                "> 🔄 发送「**切换XXX专家**」更换专家\n"
                "> 🚪 发送「**退出专家模式**」回到普通模式\n"
                "> ⏰ 30 分钟无对话自动退出"
            )
        else:
            expert_section = ""

        title = "🦊 你好！我是日盛 IT 运维助手"
        md_text = (
            f"### 欢迎使用 IT 运维智能助手 🦊\n\n"
            f"你可以直接问我 IT 相关问题，我会搜索知识库为你解答。\n\n"
            f"### 📚 也可以直接搜索知识库\n"
            f"- 发送「搜 xxx」「查 xxx」「找 xxx」查看知识库原始搜索结果"
            f"{expert_section}"
        )
        self.reply_markdown(title, md_text, msg)

    # ------------------------------------------------------------------
    # 会话专家状态管理
    # ------------------------------------------------------------------

    def _set_expert_state(self, conv_id: str, trigger: dict):
        """设置会话的当前专家状态（线程安全）"""
        with self.__expert_lock__:
            self.expert_states[conv_id] = {
                "trigger": trigger,
                "timestamp": time.time(),
            }
            bot_status.set_expert_sessions(len(self.expert_states))
        self.logger.info(
            f"会话 {conv_id[:20]}... 激活专家: {trigger['phrase']} (scope={trigger.get('scope', 'default')})"
        )

    def _get_expert_state(self, conv_id: str) -> dict | None:
        """获取会话当前激活的专家状态，过期返回 None（线程安全）"""
        with self.__expert_lock__:
            state = self.expert_states.get(conv_id)
            if not state:
                return None
            # 检查是否过期
            if time.time() - state["timestamp"] > self.EXPERT_STATE_TTL:
                self.logger.info(f"会话 {conv_id[:20]}... 专家状态已过期，自动清除")
                self.expert_states.pop(conv_id, None)
                bot_status.set_expert_sessions(len(self.expert_states))
                return None
            # 刷新时间戳（延续会话）
            state["timestamp"] = time.time()
            return state["trigger"]

    def _resolve_target_kbs(self, active_expert: dict | None) -> list:
        """
        根据当前激活的专家，决定搜索哪些知识库。
        返回知识库列表，供 IMAClient.search_knowledge_bases 使用。
        """
        default_kbs = self.ima_config.get("default_knowledge_bases", [])

        if not active_expert:
            return default_kbs

        scope = active_expert.get("scope", "default")

        if scope == "u9_only":
            u9_kb_id = "Sr51b8h0cPdLCfawTU-BRYR6kUv6k-Fccsobv0IJz2Y="
            target = [kb for kb in default_kbs if kb["id"] == u9_kb_id]
            return target if target else [{"id": u9_kb_id, "name": "U9问题处理知识库"}]

        if scope == "service_only":
            service_kb_id = "-1urpedNGasp5vQigMVxkNjP6f3tgMuxXrWascZ4hno="
            target = [kb for kb in default_kbs if kb["id"] == service_kb_id]
            return target if target else [{"id": service_kb_id, "name": "日盛售后服务知识库"}]

        # default / 其他 scope
        return default_kbs

    # ------------------------------------------------------------------
    # 专家模式管理（退出/切换/列表/意图检测）
    # ------------------------------------------------------------------

    # ── 退出专家模式的关键词 ──
    _EXIT_EXPERT_KEYWORDS = [
        "退出专家", "退出专家模式", "关闭专家", "关闭专家模式",
        "取消专家", "取消专家模式", "不用专家", "不需要专家",
        "不用了", "不要了", "我自己来", "我自己查",
        "别用专家", "别用专家模式", "结束专家", "结束专家模式",
        "退出",  # 仅当当前在专家模式时才触发
        "关闭",  # 同上
    ]

    # ── 切换专家的正则 ──
    _SWITCH_EXPERT_PATTERN = re.compile(
        r"(切换|换成|换|转到|改用|转成)(.+?)(专家|模式)",
        re.IGNORECASE,
    )

    # ── 查看专家状态/列表 ──
    _EXPERT_LIST_KEYWORDS = [
        "当前专家", "什么专家", "有哪些专家", "专家列表",
        "专家模式", "什么模式", "还在专家模式吗",
    ]

    def _detect_expert_intent(self, content: str, active_expert: dict, expert_triggers: list) -> dict | None:
        """
        检测用户在专家模式下表达的意图。

        :return: {
            "action": "exit" | "switch" | "list" | "unrelated_hint" | None,
            "target_trigger": dict | None,  # 切换时的目标专家
            "target_phrase": str | None,    # 切换时匹配到的短语
        }
        """
        # ── 1. 检测退出意图 ──
        for kw in self._EXIT_EXPERT_KEYWORDS:
            if kw in content:
                return {"action": "exit", "target_trigger": None, "target_phrase": None}

        # ── 2. 检测切换意图 ──
        m = self._SWITCH_EXPERT_PATTERN.search(content)
        if m:
            target_name = m.group(2).strip()
            # 模糊匹配专家触发词
            for trigger in expert_triggers:
                phrase = trigger["phrase"]
                # 去掉"召唤"前缀后匹配
                short_name = phrase.replace("召唤", "")
                if target_name in short_name or short_name in target_name:
                    return {"action": "switch", "target_trigger": trigger, "target_phrase": phrase}
            # 部分匹配不成功的，返回提示
            return {"action": "switch_failed", "target_trigger": None, "target_phrase": target_name}

        # ── 3. 检测列表/状态询问 ──
        for kw in self._EXPERT_LIST_KEYWORDS:
            if kw in content:
                return {"action": "list", "target_trigger": None, "target_phrase": None}

        return None

    def _handle_expert_exit(self, conv_id: str, msg: ChatbotMessage, active_expert: dict):
        """退出专家模式，清除状态并通知用户"""
        expert_label = active_expert["phrase"]
        with self.__expert_lock__:
            self.expert_states.pop(conv_id, None)
            bot_status.set_expert_sessions(len(self.expert_states))
        self.logger.info(f"会话 {conv_id[:20]}... 手动退出专家模式: {expert_label}")
        self.reply_markdown(
            f"👋 已退出 {expert_label}",
            f"### 👋 已退出专家模式\n\n"
            f"已退出 **{expert_label}**，现在回复普通模式。\n\n"
            f"需要专家时随时说「召唤XXX专家」哦～",
            msg,
        )

    def _handle_expert_switch(self, conv_id: str, target_trigger: dict, target_phrase: str,
                               msg: ChatbotMessage, old_expert: dict):
        """切换到另一个专家"""
        old_label = old_expert["phrase"]
        self._set_expert_state(conv_id, target_trigger)
        self.logger.info(f"会话 {conv_id[:20]}... 专家切换: {old_label} → {target_phrase}")
        self.reply_markdown(
            f"🔄 已切换为 {target_phrase}",
            f"### 🔄 专家已切换\n\n"
            f"从 **{old_label}** → **{target_phrase}**\n\n"
            f"后续问题将基于 {target_phrase} 的知识库为您解答 🦊",
            msg,
        )

    def _handle_expert_list(self, msg: ChatbotMessage, active_expert: dict, expert_triggers: list):
        """显示专家模式状态和可用专家列表"""
        expert_label = active_expert["phrase"]
        lines = [
            f"### 🦊 当前专家状态",
            f"",
            f"💡 正在使用：**{expert_label}**",
            f"",
            f"### 🔧 可用专家",
        ]
        for t in expert_triggers:
            desc = t.get("description", "")
            arrow = "⬅️ 当前" if t["phrase"] == expert_label else ""
            lines.append(f"- **{t['phrase']}** {arrow}")
            if desc:
                lines.append(f"  > {desc}")
        lines.extend([
            "",
            "### 📋 操作",
            "- 发送「**退出专家模式**」→ 退出当前专家",
            "- 发送「**切换XXX专家**」→ 切换到其他专家",
            "- 发送「**有哪些专家**」→ 查看此列表",
            "",
            "> ⏰ 30 分钟未对话将自动退出专家模式",
        ])
        self.reply_markdown(f"专家状态 — {expert_label}", "\n".join(lines), msg)

    def _handle_switch_failed(self, msg: ChatbotMessage, target_name: str, expert_triggers: list):
        """切换失败时提示可用专家"""
        available = [t["phrase"] for t in expert_triggers]
        body = f"没有找到「{target_name}」专家哦 (´・ω・`)\n\n可用的专家有：\n"
        for name in available:
            body += f"- {name}\n"
        body += f"\n💡 试试「切换{'、'.join(available[:2])}」？"
        self.reply_text(body, msg)

    # ------------------------------------------------------------------
    # 工单系统
    # ------------------------------------------------------------------

    def _resolve_staff_id(self, user_id: str, user_nick: str, fallback_staff_id: str = "") -> str:
        """
        解析用户的企业 staffId（oToMessages/batchSend 要求）。

        解析优先级：
        1. 消息中直接携带的 sender_staff_id（需应用有权限）
        2. 通过 sender_id（可能是 unionId）调用 /topapi/user/getbyunionid 查询
        3. 通过用户昵称调用 /topapi/v2/user/list 在通讯录中匹配
        """
        # 1. 消息中直接携带的
        if fallback_staff_id:
            return fallback_staff_id

        # 2. 如果没有 DingTalkAPI 客户端，无法查询
        if not self.dtalk_api:
            self.logger.warning(
                f"无法解析 staffId: dtalk_api 未初始化，用户 {user_nick} 的私聊通知将失败"
            )
            return ""

        # 3. 尝试通过 sender_id（可能是 unionId）查询
        if user_id:
            try:
                staff_id = self.dtalk_api.resolve_staff_id_by_open_id(user_id)
                if staff_id:
                    self.logger.info(
                        f"通过 API 解析 staffId 成功(openId): {user_nick} -> {staff_id}"
                    )
                    return staff_id
            except Exception as e:
                self.logger.warning(f"通过 openId 解析 staffId 失败: {e}")

        # 4. 尝试通过昵称在通讯录中查找
        if user_nick and user_nick != "未知":
            try:
                staff_id = self.dtalk_api.resolve_staff_id_by_nick(user_nick)
                if staff_id:
                    self.logger.info(
                        f"通过 API 解析 staffId 成功(nick): {user_nick} -> {staff_id}"
                    )
                    return staff_id
            except Exception as e:
                self.logger.warning(f"通过昵称解析 staffId 失败: {e}")

        self.logger.warning(
            f"无法解析 staffId: 用户 {user_nick}({user_id[:15]}...)，"
            f"请使用「绑定工号」命令手动设置，或在钉钉开放平台为应用开通通讯录权限"
        )
        return ""

    def _is_ticket_admin(self, user_id: str, user_nick: str = "", staff_id: str = "") -> bool:
        """
        判断用户是否为工单管理员。
        匹配优先级：user_id 精确匹配 → config 中昵称匹配 → DB 记录匹配。
        首次匹配到 config 配置的昵称时，自动将该用户的 user_id 写入 DB。
        如果 staff_id 为空，会自动通过 API 解析。
        """
        # 1. DB 中已有记录（user_id 精确匹配）
        if self.ticket_mgr.is_admin(user_id):
            # 如果 DB 中没有 staff_id，尝试补录
            admins = self.ticket_mgr.list_admins()
            for admin in admins:
                if admin.get("user_id") == user_id and not admin.get("staff_id"):
                    # 尝试解析 staff_id（消息自带 → API 查询）
                    resolved = self._resolve_staff_id(user_id, user_nick, staff_id)
                    if resolved:
                        self.ticket_mgr.add_admin(user_id, user_nick, staff_id=resolved)
                        self.logger.info(f"管理员 staff_id 补录: {user_nick} staff_id={resolved}")
                    break
            return True

        # 2. 按 config.yaml 中配置的昵称匹配（不依赖 DB 预录）
        for admin in self.ticket_admins:
            cfg_nick = admin.get("user_nick", "")
            cfg_id   = admin.get("user_id", "")
            # user_id 匹配（配置中已填写了 ID）
            if cfg_id and cfg_id == user_id:
                # 确保写入 DB
                if not self.ticket_mgr.is_admin(user_id):
                    resolved = self._resolve_staff_id(user_id, user_nick, staff_id)
                    self.ticket_mgr.add_admin(user_id, cfg_nick or user_nick, staff_id=resolved)
                return True
            # 昵称匹配（配置中只填了昵称，尚未拿到 user_id）
            if cfg_nick and cfg_nick == user_nick:
                # 首次匹配：把当前用户的 user_id + staff_id 写入 DB
                if not self.ticket_mgr.is_admin(user_id):
                    resolved = self._resolve_staff_id(user_id, user_nick, staff_id)
                    self.ticket_mgr.add_admin(user_id, user_nick, staff_id=resolved)
                    self.logger.info(
                        f"工单管理员绑定成功: {user_nick} (ID: {user_id}, staffId: {resolved or '空'})"
                    )
                return True

        # 3. 管理员表为空时，首个交互用户自动成为管理员
        admins = self.ticket_mgr.list_admins()
        if not admins:
            resolved = self._resolve_staff_id(user_id, user_nick, staff_id)
            self.ticket_mgr.add_admin_from_dingtalk(user_id, user_nick or "auto", staff_id=resolved)
            self.logger.info(f"首个用户自动成为管理员: {user_nick} ({user_id}, staffId: {resolved or '空'})")
            return True

        return False

    def _handle_ticket(self, msg: ChatbotMessage, info: dict,
                        parsed: ParsedTicketAction, is_admin: bool) -> bool:
        """
        处理工单操作。
        返回 True 表示已处理（消息被消费），False 表示未处理（继续走 AI 对话）。
        """
        intent = parsed.intent
        user_id = info["sender_id"]
        user_nick = info["sender_nick"]

        # ── 创建工单 ──
        if intent == TicketIntent.CREATE:
            return self._ticket_create(msg, info, parsed)

        # ── 每日工单摘要（所有人可用） ──
        if intent == TicketIntent.DAILY:
            return self._ticket_daily(msg, parsed)

        # ── 统计（所有人可用） ──
        if intent == TicketIntent.STATS:
            return self._ticket_stats(msg, parsed)

        # ── 查看我的工单 ──
        if intent == TicketIntent.LIST_MINE:
            return self._ticket_list_mine(msg, info)

        # ── 查看待处理队列（管理员） ──
        if intent == TicketIntent.LIST_PENDING:
            return self._ticket_list_pending(msg)

        # ── 查看全部工单（仅管理员） ──
        if intent == TicketIntent.LIST_ALL:
            if not is_admin:
                self.reply_text("⚠️ 此功能仅管理员可用", msg)
                return True
            return self._ticket_list_all(msg, parsed)

        # ── 查看工单详情 ──
        if intent == TicketIntent.VIEW:
            return self._ticket_view(msg, info, parsed, is_admin)

        # ── 关闭工单 ──
        if intent == TicketIntent.CLOSE:
            return self._ticket_close(msg, info, parsed, user_id, is_admin)

        if intent == TicketIntent.REOPEN:
            return self._ticket_reopen(msg, info, parsed, is_admin)

        # ── 解决工单（仅管理员） ──
        if intent == TicketIntent.RESOLVE:
            if not is_admin:
                self.reply_text("⚠️ 只有管理员才能解决工单", msg)
                return True
            return self._ticket_resolve(msg, info, parsed)

        # ── 归档为经验（管理员） ──
        if intent == TicketIntent.ARCHIVE:
            if not is_admin:
                self.reply_text("⚠️ 只有管理员才能归档工单", msg)
                return True
            return self._ticket_archive(msg, info, parsed)

        # ── 标记为无效工单（管理员） ──
        if intent == TicketIntent.INVALID:
            if not is_admin:
                self.reply_text("⚠️ 只有管理员才能标记无效工单", msg)
                return True
            return self._ticket_invalid(msg, info, parsed)

        # ── 主动工单提醒（管理员） ──
        if intent == TicketIntent.REMINDER:
            if not is_admin:
                self.reply_text("⚠️ 只有管理员才能使用工单提醒", msg)
                return True
            return self._ticket_reminder(msg, info)

        # ── 绑定企业工号/staffId（管理员） ──
        if intent == TicketIntent.BIND_STAFF_ID:
            if not is_admin:
                self.reply_text("⚠️ 只有管理员才能设置工号", msg)
                return True
            return self._ticket_bind_staff_id(msg, info, parsed)

        return False

    # ── 具体操作方法 ──

    def _ticket_create(self, msg: ChatbotMessage, info: dict,
                        parsed: ParsedTicketAction) -> bool:
        """创建工单（基于 IMA 知识库搜索结果自动分类）"""
        content = info["content"].strip()
        # 去掉触发词，提取问题描述
        title = content
        for kw in TicketParser.CREATE_KEYWORDS:
            title = title.replace(kw, "", 1).strip()
        if not title:
            title = content[:50]  # 用原始内容截断

        # 描述过短时先进行结构化问诊，减少管理员后续追问。
        if not info.get("triage_completed") and self.triage_engine.needs_triage(title):
            category = TicketManager.classify(title)
            question = self.triage_engine.start(info["conv_id"], title, category)
            self.reply_text(question, msg)
            self.logger.info(f"启动工单问诊: conv={info['conv_id'][:16]} category={category}")
            return True

        # 基于 IMA 知识库搜索匹配分类（优先 KB 分类，兜底关键词）
        category = info.get("triage_category", "其他")
        if self.ima_client and not info.get("triage_completed"):
            try:
                # 搜索所有默认知识库
                from handlers.knowledge_handler import extract_keywords
                query = title if len(title) >= 4 else content
                default_kbs = self.ima_config.get("default_knowledge_bases", [])
                if default_kbs:
                    results = self.ima_client.search_knowledge_bases(query, default_kbs)
                else:
                    results = self.ima_client.search_all_knowledge_bases(query)
                category = TicketManager.classify_by_kb(results)
                self.logger.info(
                    f"工单 KB 分类: '{title[:30]}' → {category} "
                    f"(KB 命中 {len(results)} 条)"
                )
            except Exception as e:
                self.logger.warning(f"工单 KB 分类失败，回退关键词匹配: {e}")
                category = TicketManager.classify(content)
        elif not info.get("triage_completed"):
            category = TicketManager.classify(content)

        # AI 自动判断优先级（基于是否影响业务）
        priority = self._judge_priority(title, content)

        ticket = self.ticket_mgr.create_ticket(
            conv_id=info["conv_id"],
            conv_title=info.get("conv_title", ""),
            creator_id=info["sender_id"],
            creator_nick=info["sender_nick"],
            creator_staff_id=info.get("staff_id", ""),
            title=title[:80],
            description=content,
            category=category,
            priority=priority,
        )
        related_active = self.ticket_mgr.find_related_active_tickets(
            title=ticket["title"], category=ticket["category"],
            exclude_ticket_id=ticket["ticket_id"],
            hours=self.config.get("ticket", {}).get("incident_window_hours", 8),
        )
        if related_active:
            ticket["related_active"] = related_active
            ticket["incident_suspected"] = len(related_active) >= 2
        self.logger.info(
            f"工单创建: {ticket['ticket_id']} | {category} | {info['sender_nick']}"
        )
        title_str, body = TicketFormatter.format_ticket_created(ticket)
        self.reply_markdown(title_str, body, msg)

        # 检查是否有暂存的附件需要关联
        conv_id = info["conv_id"]
        with self.__attachment_lock__:
            pending_attach = self._pending_attachments.pop(conv_id, None)
        if pending_attach:
            self._add_attachment_to_ticket(
                ticket,
                pending_attach["file_name"],
                pending_attach["file_type"],
                pending_attach.get("media_id", ""),
                pending_attach.get("save_path", ""),
            )
            self._delete_local_attachment(pending_attach.get("save_path", ""))
            self.logger.info(f"暂存附件已关联工单 {ticket['ticket_id']}")

        # 通知管理员有新工单
        self._notify_admin_new_ticket(ticket, info)

        # 搜索相似工单（已解决的），给用户参考
        try:
            # 从标题中提取关键词
            keywords = [w for w in re.split(r"[,，。、\s]+", title) if len(w) >= 2][:5]
            if keywords:
                similar = self.ticket_mgr.search_similar_tickets(keywords, limit=3)
                if similar:
                    lines = ["### 💡 以下工单可能相关，供参考：", ""]
                    for s in similar[:3]:
                        status_label = STATUS_LABELS.get(s["status"], s["status"])
                        lines.append(
                            f"- **{s['ticket_id']}** {s['title'][:40]}\n"
                            f"  > {s['category']} | {status_label} | {s.get('resolver_nick', '—')}"
                        )
                    if s.get("resolution"):
                        lines.append(f"\n💡 最近的解决方案: {s['resolution'][:100]}")
                    self.reply_markdown("💡 相似工单", "\n".join(lines), msg)
        except Exception as e:
            self.logger.warning(f"相似工单搜索失败: {e}")

        # 记录日志
        try:
            msg_logger.record({
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg_id": info.get("msg_id", ""),
                "sender_nick": info["sender_nick"],
                "sender_id": info["sender_id"],
                "conv_type": info.get("conv_label", "?"),
                "conv_title": info.get("conv_title", ""),
                "content": content,
                "expert_mode": "工单-创建",
                "expert_scope": "",
                "has_kb": (category != "其他"),
                "kb_results": 1,
                "engine": "ticket",
                "answer_len": len(body),
                "answer_preview": f"[工单] {ticket['ticket_id']} {category}: {ticket['title'][:30]}",
            })
        except Exception:
            pass
        return True

    def _ticket_list_mine(self, msg: ChatbotMessage, info: dict) -> bool:
        """查看我的工单"""
        tickets = self.ticket_mgr.list_tickets_by_user(info["sender_id"])
        title_str, body = TicketFormatter.format_ticket_list(tickets, info["sender_nick"])
        self.reply_markdown(title_str, body, msg)
        return True

    def _ticket_list_pending(self, msg: ChatbotMessage) -> bool:
        """查看待处理队列"""
        tickets = self.ticket_mgr.list_all_tickets(filter_type="pending")
        title_str, body = TicketFormatter.format_pending_queue(tickets)
        self.reply_markdown(title_str, body, msg)
        return True

    def _ticket_list_all(self, msg: ChatbotMessage,
                         parsed: ParsedTicketAction) -> bool:
        """查看全量工单（管理员），支持按 filter_type 筛选"""
        filter_type = parsed.extra or "all"
        tickets = self.ticket_mgr.list_all_tickets(filter_type=filter_type)
        title_str, body = TicketFormatter.format_all_tickets_admin(tickets, filter_type)
        self.reply_markdown(title_str, body, msg)
        self.logger.info(
            f"管理员查询全量工单: filter={filter_type}, 共 {len(tickets)} 条"
        )
        return True

    def _ticket_view(self, msg: ChatbotMessage, info: dict,
                     parsed: ParsedTicketAction, is_admin: bool) -> bool:
        """查看工单详情（普通用户只能查看自己的工单，管理员可查看所有）"""
        ticket = self.ticket_mgr.get_ticket(parsed.ticket_id)
        if not ticket:
            self.reply_text(
                f"❌ 未找到工单 {parsed.ticket_id}\n\n请确认编号是否正确。",
                msg,
            )
            return True

        # 权限检查：非管理员只能查看自己创建的工单
        if not is_admin and ticket["creator_id"] != info["sender_id"]:
            self.reply_text("⚠️ 你只能查看自己创建的工单", msg)
            return True

        logs = self.ticket_mgr.get_ticket_logs(parsed.ticket_id, limit=10)
        title_str, body = TicketFormatter.format_ticket_detail(ticket, logs)
        self.reply_markdown(title_str, body, msg)

        # 管理员查看已解决工单且未归档/未标记无效时，追问是否归档
        if is_admin and ticket["status"] in ("resolved", "closed") \
                and not ticket.get("is_experience") and not ticket.get("is_invalid"):
            archive_title, archive_body = TicketFormatter.format_archive_prompt(ticket)
            self.reply_markdown(archive_title, archive_body, msg)

        return True

    def _ticket_daily(self, msg: ChatbotMessage, parsed: ParsedTicketAction) -> bool:
        """每日工单摘要"""
        date_str = parsed.extra  # 可能为 None（默认今天）
        summary = self.ticket_mgr.get_daily_summary(date_str)
        title_str, body = TicketFormatter.format_daily_summary(summary)
        self.reply_markdown(title_str, body, msg)
        return True

    def _ticket_close(self, msg: ChatbotMessage, info: dict,
                       parsed: ParsedTicketAction, user_id: str,
                       is_admin: bool) -> bool:
        """关闭工单"""
        # 无编号时：查找用户最近一个未关闭的工单
        if not parsed.ticket_id:
            tickets = self.ticket_mgr.list_tickets_by_user(user_id)
            open_tickets = [t for t in tickets
                            if t["status"] not in ("resolved", "closed")]
            if not open_tickets:
                self.reply_text("📭 你没有待处理的工单哦～", msg)
                return True
            if len(open_tickets) == 1:
                parsed.ticket_id = open_tickets[0]["ticket_id"]
            else:
                # 多个时列出让用户选
                lines = ["### 🤔 请指定要关闭哪个工单", ""]
                for t in open_tickets[:5]:
                    lines.append(f"- **[{t['ticket_id']}]** {t['title'][:30]}")
                lines.append(f"\n回复「**关闭 TKT-xxx**」指定")
                self.reply_markdown("请指定工单", "\n".join(lines), msg)
                return True

        ticket = self.ticket_mgr.get_ticket(parsed.ticket_id)
        if not ticket:
            self.reply_text(f"❌ 未找到工单 {parsed.ticket_id}", msg)
            return True

        # 权限检查：创建者或管理员可关闭
        if ticket["creator_id"] != user_id and not is_admin:
            self.reply_text("⚠️ 只有工单创建者或管理员才能关闭工单", msg)
            return True

        ticket = self.ticket_mgr.close_ticket(
            parsed.ticket_id, info["sender_id"], info["sender_nick"],
        )
        title_str, body = TicketFormatter.format_ticket_status_updated(
            parsed.ticket_id, "closed", "工单已关闭",
        )
        self.reply_markdown(title_str, body, msg)

        # 通知工单创建者（管理员关闭时，创建者应收到通知）
        if is_admin and ticket and ticket.get("creator_id") != info["sender_id"]:
            self._notify_user_resolved(ticket, info["sender_nick"])

        return True

    def _ticket_reopen(self, msg: ChatbotMessage, info: dict,
                       parsed: ParsedTicketAction, is_admin: bool) -> bool:
        ticket = self.ticket_mgr.get_ticket(parsed.ticket_id)
        if not ticket:
            self.reply_text(f"❌ 未找到工单 {parsed.ticket_id}", msg)
            return True
        if not is_admin and ticket["creator_id"] != info["sender_id"]:
            self.reply_text("⚠️ 你只能重开自己创建的工单", msg)
            return True
        reopened = self.ticket_mgr.reopen_ticket(
            parsed.ticket_id, info["sender_id"], info["sender_nick"]
        )
        if not reopened:
            self.reply_text(f"⚠️ 工单 {parsed.ticket_id} 当前状态不能重开", msg)
            return True
        self.reply_markdown(
            f"🔄 工单已重开 — {parsed.ticket_id}",
            f"### 🔄 工单已重新进入处理中\n\n"
            f"**编号**: {parsed.ticket_id}\n**问题**: {reopened['title']}\n\n"
            "已重新通知管理员继续处理。",
            msg,
        )
        self._notify_admin_new_ticket(reopened, info)
        return True

    # ------------------------------------------------------------------
    # AI 优先级判断
    # ------------------------------------------------------------------

    # 优先级快速关键词匹配（无需调 AI，毫秒级）
    _PRIORITY_KEYWORDS = {
        "urgent": [
            "系统瘫痪", "全公司", "全部无法", "完全无法", "宕机", "全部断网",
            "数据丢失", "服务器挂了", "全线停工", "整个系统", "所有人",
            "紧急", "生产停线", "产线停", "全线停", "无法生产",
        ],
        "high": [
            "多个人", "多个用户", "部分无法", "部门无法", "车间无法",
            "影响业务", "无法下单", "无法开单", "无法审核", "审批不了",
            "流程卡住", "系统卡死", "频繁掉线", "频繁断开",
        ],
        "low": [
            "咨询", "问一下", "想了解", "建议", "能不能", "是否可以",
            "怎么操作", "如何设置", "有没有", "能不能", "可以吗",
            "想问", "请教", "学习", "培训",
        ],
    }

    def _judge_priority(self, title: str, description: str) -> str:
        """
        AI 自动判断工单优先级（基于是否影响业务）。
        策略：先快速关键词匹配，再调 LLM 精确判断。
        """
        text = f"{title} {description}".lower()

        # 第一层：关键词快速匹配（毫秒级，无 API 成本）
        for level, keywords in self._PRIORITY_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                self.logger.info(f"优先级关键词匹配: '{title[:30]}' → {level}")
                return level

        # 第二层：LLM 精确判断（仅关键词未命中时调用）
        if self.llm_client:
            try:
                from modules.ticket.ticket_manager import PRIORITY_ORDER
                prompt = (
                    "请根据以下问题描述判断优先级，判断标准：是否影响业务运行。\n\n"
                    "🔴 urgent（紧急）：影响核心业务/系统瘫痪/数据丢失/大面积断网/生产停线\n"
                    "🟠 high（高）：影响部分业务/多个用户无法正常工作/关键流程受阻\n"
                    "🟡 medium（中）：影响单个用户/不影响核心业务流程\n"
                    "⚪ low（低）：咨询类/优化建议/不影响正常使用\n\n"
                    f"问题描述：{title}\n"
                    f"详细说明：{description[:200]}\n\n"
                    "只回复优先级英文标识，不要其他内容：urgent/high/medium/low"
                )
                messages = [
                    {"role": "system", "content": "你是一个工单优先级分类助手，只输出英文优先级标识。"},
                    {"role": "user", "content": prompt},
                ]
                result = self.llm_client.chat(
                    messages, temperature=0.0, max_tokens=10,
                ).strip().lower()
                if result in PRIORITY_ORDER:
                    self.logger.info(f"AI 优先级判断: '{title[:30]}' → {result}")
                    return result
                self.logger.warning(f"AI 优先级判断返回无效值: '{result}'，回退 medium")
            except DeepSeekAPIError as e:
                self.logger.warning(f"AI 优先级判断 API 错误: {e}，回退 medium")
            except Exception as e:
                self.logger.warning(f"AI 优先级判断失败: {e}，回退 medium")

        return "medium"  # 默认中优先级

    def _ticket_resolve(self, msg: ChatbotMessage, info: dict,
                         parsed: ParsedTicketAction) -> bool:
        """解决工单（仅管理员，已在 dispatch 处校验权限）"""
        if not parsed.ticket_id:
            self.reply_text("⚠️ 请指定工单编号，例如「解决 TKT-xxx 解决方案」", msg)
            return True

        # 从 extra（消息全文）中提取纯解决方案文本：去掉编号和"解决"前缀
        raw_extra = parsed.extra or ""
        resolution = raw_extra
        # 去掉工单编号
        import re as _re
        resolution = _re.sub(r"TKT-\d{8}-\d{3,4}", "", resolution).strip()
        # 去掉动词前缀
        for prefix in ("解决", "修复", "fix", "Fix"):
            if resolution.lower().startswith(prefix.lower()):
                resolution = resolution[len(prefix):].strip()
                break
        # 去掉开头的分隔符
        resolution = resolution.lstrip("：: ").strip()

        ticket = self.ticket_mgr.resolve_ticket(
            parsed.ticket_id, resolution,
            info["sender_id"], info["sender_nick"],
        )
        if not ticket:
            self.reply_text(f"❌ 未找到工单 {parsed.ticket_id}", msg)
            return True
        title_str, body = TicketFormatter.format_ticket_status_updated(
            parsed.ticket_id, "resolved",
            detail=f"解决方案：{resolution}" if resolution else "",
        )
        self.reply_markdown(title_str, body, msg)

        # 通知工单创建者：工单已解决
        self._notify_user_resolved(ticket, info["sender_nick"])

        # 追问管理员：是否归档为经验
        archive_title, archive_body = TicketFormatter.format_archive_prompt(ticket)
        self.reply_markdown(archive_title, archive_body, msg)

        return True

    def _ticket_stats(self, msg: ChatbotMessage, parsed: ParsedTicketAction) -> bool:
        """工单统计"""
        period = parsed.extra or "today"
        from datetime import datetime, timedelta
        since = None
        if period == "today":
            since = datetime.now().strftime("%Y-%m-%d")
        elif period == "week":
            since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        elif period == "month":
            since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        stats = self.ticket_mgr.get_stats(since=since)
        title_str, body = TicketFormatter.format_ticket_stats(stats, period)
        self.reply_markdown(title_str, body, msg)
        return True

    def _ticket_archive(self, msg: ChatbotMessage, info: dict,
                         parsed: ParsedTicketAction) -> bool:
        """归档工单为经验（管理员）"""
        if not parsed.ticket_id:
            self.reply_text("⚠️ 请指定工单编号，例如「归档 TKT-xxx」", msg)
            return True
        ticket = self.ticket_mgr.get_ticket(parsed.ticket_id)
        if not ticket:
            self.reply_text(f"❌ 未找到工单 {parsed.ticket_id}", msg)
            return True
        if ticket.get("is_experience"):
            self.reply_text(f"ℹ️ 工单 {parsed.ticket_id} 已经是经验了", msg)
            return True
        result = self.ticket_mgr.archive_as_experience(
            parsed.ticket_id, info["sender_id"], info["sender_nick"],
        )
        if not result:
            self.reply_text(f"❌ 归档失败: {parsed.ticket_id}", msg)
            return True
        title_str, body = TicketFormatter.format_archived_confirmation(result)
        self.reply_markdown(title_str, body, msg)
        self.logger.info(f"工单归档为经验: {parsed.ticket_id} by {info['sender_nick']}")
        return True

    def _ticket_invalid(self, msg: ChatbotMessage, info: dict,
                         parsed: ParsedTicketAction) -> bool:
        """标记工单为无效（管理员）"""
        if not parsed.ticket_id:
            self.reply_text("⚠️ 请指定工单编号，例如「无效 TKT-xxx」", msg)
            return True
        ticket = self.ticket_mgr.get_ticket(parsed.ticket_id)
        if not ticket:
            self.reply_text(f"❌ 未找到工单 {parsed.ticket_id}", msg)
            return True
        if ticket.get("is_invalid"):
            self.reply_text(f"ℹ️ 工单 {parsed.ticket_id} 已经标记为无效了", msg)
            return True
        result = self.ticket_mgr.mark_as_invalid(
            parsed.ticket_id, info["sender_id"], info["sender_nick"],
        )
        if not result:
            self.reply_text(f"❌ 标记失败: {parsed.ticket_id}", msg)
            return True
        title_str, body = TicketFormatter.format_invalid_confirmation(result)
        self.reply_markdown(title_str, body, msg)
        self.logger.info(f"工单标记无效: {parsed.ticket_id} by {info['sender_nick']}")
        return True

    def _ticket_bind_staff_id(self, msg: ChatbotMessage, info: dict,
                                parsed: ParsedTicketAction) -> bool:
        """
        绑定管理员的企业工号（staffId）。
        用法：绑定工号 01234567
              设置ID 01234567
        """
        user_id = info["sender_id"]
        user_nick = info["sender_nick"]
        staff_id = (parsed.extra or "").strip()

        if not staff_id:
            self.reply_text(
                "💡 请输入你的企业工号，例如：\n"
                "「绑定工号 01234567」\n"
                "工号即你的企业 userId，可在钉钉个人资料页查看。",
                msg,
            )
            return True

        # 写入 DB
        self.ticket_mgr.add_admin(user_id, user_nick, staff_id=staff_id)
        self.logger.info(
            f"管理员绑定工号: {user_nick}({user_id}) -> staff_id={staff_id}"
        )
        self.reply_text(
            f"✅ 工号绑定成功！\n"
            f"管理员 {user_nick} 的企业工号已设置为：`{staff_id}`\n"
            f"工单私聊通知将使用此工号发送 💪",
            msg,
        )
        return True

    def _ticket_reminder(self, msg: ChatbotMessage, info: dict) -> bool:
        """主动工单提醒（管理员）"""
        pending = self.ticket_mgr.list_all_tickets(filter_type="pending")
        title_str, body = TicketFormatter.format_reminder(pending, info["sender_nick"])
        self.reply_markdown(title_str, body, msg)
        return True

    # ------------------------------------------------------------------
    # 反馈闭环（AI 回答 → 用户"是/否" → 归档/升级工单）
    # ------------------------------------------------------------------

    def _set_pending_feedback(self, conv_id: str, question: str,
                               answer_preview: str = "", has_kb: bool = False):
        """记录一次待反馈的 AI 回答（每次"否"会独立创建新工单，不再绑定已有工单）"""
        with self.__feedback_lock__:
            self._pending_feedback[conv_id] = {
                "question": question,
                "answer_preview": answer_preview,
                "has_kb": has_kb,
                "timestamp": time.time(),
            }

    def _get_pending_feedback(self, conv_id: str) -> dict | None:
        """获取待反馈状态，过期返回 None"""
        with self.__feedback_lock__:
            fb = self._pending_feedback.get(conv_id)
            if not fb:
                return None
            if time.time() - fb["timestamp"] > self._FEEDBACK_TTL:
                self._pending_feedback.pop(conv_id, None)
                return None
            return fb

    def _handle_feedback_yes(self, msg: ChatbotMessage, info: dict,
                              pending: dict):
        """
        处理用户"是"反馈——满意，仅表示感谢。
        工单由管理员手动解决/归档，此处不自动改变工单状态。
        （V1.3.5 后每次"否"独立创建新工单，此时"是"不再关联工单）
        """
        self.logger.info(
            f"反馈闭环(YES): 用户 {info['sender_nick']} 对 AI 回答满意，无需升级工单"
        )
        title_str, body = TicketFormatter.format_feedback_yes()
        self.reply_markdown(title_str, body, msg)

    def _handle_feedback_no(self, msg: ChatbotMessage, info: dict,
                             pending: dict):
        """
        处理用户"否"反馈——不满意，升级为工单并通知管理员。

        流程：
        1. 每次反馈"否"都创建独立新工单
        2. 设置状态为「处理中」
        3. 发送钉钉消息通知管理员
        4. 提示用户等待
        """
        user_nick = info["sender_nick"]
        user_id = info["sender_id"]
        question = pending.get("question", "")

        # ── 1. 每次反馈"否"都创建独立新工单 ──
        category = TicketManager.classify(question)
        priority = self._judge_priority(question, f"用户对以下问题的 AI 回答不满意：\n{question}")
        ticket = self.ticket_mgr.create_ticket(
            conv_id=info["conv_id"],
            conv_title=info.get("conv_title", ""),
            creator_id=user_id,
            creator_nick=user_nick,
            creator_staff_id=info.get("staff_id", ""),
            title=question[:80] if question else "AI 回答反馈不满意",
            description=f"用户对以下问题的 AI 回答不满意：\n{question}",
            category=category,
            priority=priority,
        )
        ticket_id = ticket["ticket_id"]
        self.logger.info(
            f"反馈闭环(NO): 新建工单 {ticket_id} ({category})"
        )

        # ── 2. 设置处理中 ──
        self.ticket_mgr.set_in_progress(
            ticket_id,
            operator_id=user_id,
            operator_nick=user_nick,
            detail=f"用户 {user_nick} 表示 AI 回答未解决问题，已升级待人工处理",
        )

        # ── 3. 通知管理员 ──
        self._notify_admin_feedback(ticket or self.ticket_mgr.get_ticket(ticket_id),
                                     info, pending)

        # ── 4. 回复用户 ──
        updated_ticket = self.ticket_mgr.get_ticket(ticket_id) or ticket
        title_str, body = TicketFormatter.format_feedback_no(updated_ticket)
        self.reply_markdown(title_str, body, msg)

        # 记录日志
        try:
            msg_logger.record({
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg_id": info.get("msg_id", ""),
                "sender_nick": user_nick,
                "sender_id": user_id,
                "conv_type": info.get("conv_label", "?"),
                "conv_title": info.get("conv_title", ""),
                "content": info["content"],
                "expert_mode": "反馈-不满意",
                "expert_scope": "",
                "has_kb": False,
                "kb_results": 0,
                "engine": "feedback",
                "answer_len": len(body),
                "answer_preview": f"[反馈NO] 工单 {ticket_id} 已升级处理中",
            })
        except Exception:
            pass

    def _notify_admin_feedback(self, ticket: dict, info: dict, pending: dict):
        """
        通知管理员：用户对 AI 回答不满意，需要人工处理。

        同时尝试发送群聊消息 + 私聊消息给每个管理员。
        如果反馈者本身也是管理员，发送轻量级「副本确认」而非完整通知。
        私聊使用 staff_id（企业userId），而非 sender_id（openId）。
        """
        if not ticket or not self.dtalk_api:
            return

        sender_id = info.get("sender_id", "")
        user_nick = info.get("sender_nick", "未知")
        question = pending.get("question", "")
        answer_preview = pending.get("answer_preview", "")
        admin_title, admin_body = TicketFormatter.format_admin_feedback_notification(
            ticket, user_nick, question, answer_preview,
        )

        # 1. 发送到当前群聊（仅群聊时可用，单聊跳过）
        if info.get("conv_type") == self.CONV_TYPE_GROUP:
            try:
                self.dtalk_api.send_markdown_to_chat(
                    info["conv_id"], admin_title, admin_body,
                )
                self.logger.info(
                    f"管理员通知已发送至群聊: {ticket['ticket_id']}"
                )
            except Exception as e:
                self.logger.warning(f"群聊管理员通知发送失败: {e}")
        else:
            self.logger.info(
                f"当前为单聊，跳过群聊通知: {ticket['ticket_id']}"
            )

        # 2. 私聊通知每个管理员（反馈者收到副本确认，其他人收到完整通知）
        admins = self.ticket_mgr.list_admins()
        for admin in admins:
            admin_id = admin.get("user_id", "")
            admin_staff_id = admin.get("staff_id", "")
            if not admin_id:
                continue

            # 优先使用 staff_id（企业userId），这是 oToMessages/batchSend 要求的
            notify_id = admin_staff_id or admin_id
            if not notify_id:
                continue

            # 反馈者本人 → 发送副本确认；其他人 → 发送完整通知
            is_sender = (admin_id == sender_id)
            if is_sender:
                send_title, send_body = TicketFormatter.format_admin_self_ticket_confirmation(ticket)
                log_tag = "副本确认"
            else:
                send_title, send_body = admin_title, admin_body
                log_tag = "完整通知"

            if not admin_staff_id:
                self.logger.warning(
                    f"管理员 {admin.get('user_nick', '')} 缺少 staff_id，"
                    f"私聊{log_tag}可能失败（oToMessages/batchSend 需要 staffId）"
                )
            try:
                self.dtalk_api.send_markdown_to_user(
                    notify_id, send_title, send_body,
                )
                self.logger.info(
                    f"管理员私聊{log_tag}已发送: {admin.get('user_nick', notify_id[:10])}"
                    f" (staff_id={admin_staff_id[:10] if admin_staff_id else '空'})"
                )
            except Exception as e:
                self.logger.warning(
                    f"管理员私聊{log_tag}失败 ({admin.get('user_nick', notify_id[:10])}): {e}"
                )


    def _notify_admin_new_ticket(self, ticket: dict, info: dict):
        """
        通知管理员：有新工单创建，需要处理。

        同时发送群聊消息 + 私聊消息给每个管理员。
        如果创建者本身也是管理员，发送轻量级「副本确认」而非完整通知。
        私聊使用 staff_id（企业userId），而非 sender_id（openId）。
        """
        if not ticket or not self.dtalk_api:
            return

        sender_id = info.get("sender_id", "")
        admin_title, admin_body = TicketFormatter.format_admin_new_ticket_notification(ticket)

        # 1. 发送到当前群聊（仅群聊时可用，单聊跳过）
        if info.get("conv_type") == self.CONV_TYPE_GROUP:
            try:
                self.dtalk_api.send_markdown_to_chat(
                    info["conv_id"], admin_title, admin_body,
                )
                self.logger.info(
                    f"新工单通知已发送至群聊: {ticket['ticket_id']}"
                )
            except Exception as e:
                self.logger.warning(f"群聊新工单通知发送失败: {e}")
        else:
            self.logger.info(
                f"当前为单聊，跳过群聊通知: {ticket['ticket_id']}"
            )

        # 2. 私聊通知每个管理员（创建者收到副本确认，其他人收到完整通知）
        admins = self.ticket_mgr.list_admins()
        for admin in admins:
            admin_id = admin.get("user_id", "")
            admin_staff_id = admin.get("staff_id", "")
            if not admin_id:
                continue

            # 优先使用 staff_id（企业userId），这是 oToMessages/batchSend 要求的
            notify_id = admin_staff_id or admin_id
            if not notify_id:
                continue

            # 创建者本人 → 发送副本确认；其他人 → 发送完整通知
            is_creator = (admin_id == sender_id)
            if is_creator:
                send_title, send_body = TicketFormatter.format_admin_self_ticket_confirmation(ticket)
                log_tag = "副本确认"
            else:
                send_title, send_body = admin_title, admin_body
                log_tag = "完整通知"

            if not admin_staff_id:
                self.logger.warning(
                    f"管理员 {admin.get('user_nick', '')} 缺少 staff_id，"
                    f"私聊{log_tag}可能失败（oToMessages/batchSend 需要 staffId）"
                )

            try:
                self.dtalk_api.send_markdown_to_user(
                    notify_id, send_title, send_body,
                )
                self.logger.info(
                    f"新工单私聊{log_tag}已发送: {admin.get('user_nick', notify_id[:10])}"
                    f" (staff_id={admin_staff_id[:10] if admin_staff_id else '空'})"
                )
            except Exception as e:
                self.logger.warning(
                    f"新工单私聊{log_tag}失败 ({admin.get('user_nick', notify_id[:10])}): {e}"
                )

    def _notify_user_resolved(self, ticket: dict, resolver_nick: str = ""):
        """
        通知工单创建者：工单已解决。

        通过私聊消息通知工单创建者，包含解决方案。
        使用 creator_staff_id（企业userId）发送，而非 creator_id（openId）。
        如果解决者本人就是创建者，则不重复通知。
        """
        if not ticket or not self.dtalk_api:
            return

        creator_id = ticket.get("creator_id", "")
        creator_staff_id = ticket.get("creator_staff_id", "")
        if not creator_id:
            return

        # 如果解决者就是创建者本人，不需要通知
        resolver_id = ""
        # 从工单日志获取解决者的 user_id
        logs = self.ticket_mgr.get_ticket_logs(ticket["ticket_id"], limit=3)
        for log in logs:
            if log.get("action") in ("resolve", "resolved"):
                resolver_id = log.get("operator_id", "")
                break
        if resolver_id and resolver_id == creator_id:
            return

        # 优先使用 creator_staff_id（企业userId），这是 oToMessages/batchSend 要求的
        notify_id = creator_staff_id or creator_id
        if not notify_id:
            return

        # 如果没有 staff_id 只能用 openId 兜底，大概率会失败
        if not creator_staff_id:
            self.logger.warning(
                f"工单 {ticket['ticket_id']} 创建者 {ticket.get('creator_nick', '')} 缺少 staff_id，"
                f"解决通知可能失败（oToMessages/batchSend 需要 staffId）"
            )

        user_title, user_body = TicketFormatter.format_user_resolved_notification(
            ticket, resolver_nick,
        )
        try:
            self.dtalk_api.send_markdown_to_user(
                notify_id, user_title, user_body,
            )
            self.logger.info(
                f"工单解决通知已发送给创建者: {ticket.get('creator_nick', notify_id[:10])}"
                f" (staff_id={creator_staff_id[:10] if creator_staff_id else '空'})"
            )
        except Exception as e:
            self.logger.warning(
                f"工单解决通知发送失败 ({ticket.get('creator_nick', '')}): {e}"
            )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def process(self, callback: CallbackMessage) -> tuple:
        """
        处理接收到的机器人消息。
        """
        try:
            incoming_message = ChatbotMessage.from_dict(callback.data)
        except Exception as e:
            self.logger.error(f"消息解析失败: {e}")
            return AckMessage.STATUS_SYSTEM_EXCEPTION, str(e)

        # 全局异常兜底：确保任何未预期异常都不会导致消息处理中断无回复
        try:
            # 提取消息信息
            msg_info = self._extract_message_info(incoming_message)
            self.logger.info(
                f"收到消息 | 类型={msg_info['conv_label']} | "
                f"发送者={msg_info['sender_nick']}(sender_id={msg_info['sender_id'][:12]}..., staff_id={msg_info.get('staff_id', '')[:12] or '空'}...) | "
                f"内容={msg_info['content'][:100]}"
            )

            # 检查会话类型开关
            conv_type = msg_info["conv_type"]
            if conv_type == self.CONV_TYPE_P2P and not self.chat_config.get("enable_p2p", True):
                self.logger.debug("单聊消息已忽略（配置关闭）")
                return AckMessage.STATUS_OK, "OK"

            if conv_type == self.CONV_TYPE_GROUP and not self.chat_config.get("enable_group", True):
                self.logger.debug("群聊消息已忽略（配置关闭）")
                return AckMessage.STATUS_OK, "OK"

            # 关键词过滤
            if not self._check_keyword(msg_info["content"]):
                return AckMessage.STATUS_OK, "OK"

            # 自动回复（调试模式）
            if self.chat_config.get("auto_echo", False):
                self._handle_echo(incoming_message, msg_info)
                return AckMessage.STATUS_OK, "OK"

            # 计数消息（仅有效消息，非空且通过关键词过滤）
            bot_status.inc_messages()

            content = msg_info["content"].strip()

            # ── 非文本消息处理（文件/图片/语音） ──
            msgtype = msg_info.get("msgtype", "text")
            file_info = msg_info.get("file_info", {})
            if msgtype != "text" and file_info:
                return self._handle_file_message(incoming_message, msg_info)

            if not content:
                return AckMessage.STATUS_OK, "OK"

            # 首次对话引导（不阻断后续处理）
            if self.ima_client or self.llm_client:
                self._send_welcome(incoming_message, msg_info)

            # 文件发送测试指令（仅在 process 入口处理，不走 _dispatch）
            if content in ("测试文件", "发个测试文件", "测试发送"):
                self._handle_file_test(incoming_message, msg_info)
                return AckMessage.STATUS_OK, "OK"

            # 统一文本消息调度（专家召唤、触发词、引擎切换、反馈、工单、AI对话）
            return self._dispatch_text_message(incoming_message, msg_info)

        except Exception as e:
            # 全局异常兜底：记录错误并尝试回复用户
            self.logger.error(f"消息处理异常（全局兜底）: {e}", exc_info=True)
            try:
                self.reply_text(
                    "⚠️ 处理消息时发生内部错误，请稍后重试。\n"
                    "如果问题持续，请联系管理员。",
                    incoming_message,
                )
            except Exception:
                pass  # 连回复都失败，只能记日志
            return AckMessage.STATUS_OK, "OK"

    # ------------------------------------------------------------------
    # 文件发送测试
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 非文本消息处理（文件/图片/语音）
    # ------------------------------------------------------------------

    def _handle_file_message(self, msg: ChatbotMessage, info: dict) -> tuple:
        """
        处理文件/图片/语音消息。

        文件/图片 → 下载并关联到用户最近的待处理工单
        语音 → 下载 + Whisper 转文字 → 作为普通消息继续处理
        """
        file_info = info.get("file_info", {})
        file_type = file_info.get("type", "")
        download_code = file_info.get("download_code", "")

        # 某些钉钉客户端会直接附带语音识别文本，此时无需下载和 Whisper。
        recognition = (file_info.get("recognition", "") or "").strip()
        if file_type == "voice" and recognition:
            self.logger.info(f"使用钉钉语音识别文本: '{recognition[:50]}'")
            self.reply_text(f"🎤 语音识别: {recognition}\n---\n正在处理中...", msg)
            info["content"] = recognition
            info["msgtype"] = "text"
            info["voice_transcribed"] = True
            return self._dispatch_text_message(msg, info)

        if not download_code:
            self.logger.warning(f"收到 {file_type} 消息但无 downloadCode，跳过")
            self.reply_text(f"⚠️ 收到了{file_type}消息但无法下载，请同时发送文字描述问题", msg)
            return AckMessage.STATUS_OK, "OK"

        if not self.dtalk_api:
            self.logger.warning("收到附件消息，但钉钉 OpenAPI 客户端未初始化")
            self.reply_text("⚠️ 附件服务尚未配置，请联系管理员或改用文字描述问题", msg)
            return AckMessage.STATUS_OK, "OK"

        # ── 语音消息 → 转文字后继续处理 ──
        if file_type == "voice":
            return self._handle_voice_message(msg, info, file_info)

        # ── 文件/图片消息 → 下载并关联工单 ──
        try:
            # 下载文件
            file_name = self._safe_attachment_name(
                file_info.get("file_name", f"attachment_{int(time.time())}")
            )
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "attachments")
            os.makedirs(data_dir, exist_ok=True)
            save_path = os.path.join(data_dir, f"{uuid.uuid4().hex}_{file_name}")
            self.dingtalk_api.download_message_file(download_code, save_path)
            self.logger.info(f"附件下载成功: {save_path}")

            # 重新上传到钉钉获取 media_id（以便后续转发）
            media_id = ""
            if self.dingtalk_api:
                try:
                    upload_type = "image" if file_type == "image" else "file"
                    media_id = self.dingtalk_api.upload_file(save_path, upload_type)
                    self.logger.info(f"附件重新上传: media_id={media_id}")
                except Exception as e:
                    self.logger.warning(f"附件重新上传失败: {e}")

            # 查找用户最近的待处理工单
            user_tickets = self.ticket_mgr.list_tickets_by_user(
                info["sender_id"], limit=5,
            )
            recent_open = None
            now = datetime.now()
            for t in user_tickets:
                if t["status"] in ("open", "assigned", "in_progress"):
                    created = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
                    if (now - created).total_seconds() < 3600:  # 1小时内的工单
                        recent_open = t
                        break

            if recent_open:
                # 关联到现有工单
                self._add_attachment_to_ticket(recent_open, file_name, file_type, media_id, save_path)
                self._delete_local_attachment(save_path)
                type_label = "📷 图片" if file_type == "image" else "📄 文件"
                self.reply_text(
                    f"{type_label}已添加到工单 {recent_open['ticket_id']}\n"
                    f"工单标题: {recent_open['title'][:30]}",
                    msg,
                )
            else:
                # 没有最近工单 → 提示用户先描述问题
                type_label = "📷 图片" if file_type == "image" else "📄 文件"
                self.reply_text(
                    f"{type_label}已收到！\n\n"
                    f"请同时发送文字描述你的问题，我会自动创建工单并关联附件 🦊",
                    msg,
                )
                # 暂存附件信息，等待用户文字消息
                conv_id = info["conv_id"]
                with self.__attachment_lock__:
                    previous = self._pending_attachments.pop(conv_id, None)
                    self._pending_attachments[conv_id] = {
                        "file_name": file_name,
                        "file_type": file_type,
                        "media_id": media_id,
                        "save_path": save_path,
                        "timestamp": time.time(),
                    }
                if previous:
                    self._delete_local_attachment(previous.get("save_path", ""))

                # 清理过期的暂存附件
                self._cleanup_expired_attachments()

        except Exception as e:
            self.logger.error(f"处理附件失败: {e}")
            self.reply_text("⚠️ 附件处理失败，请用文字描述你的问题", msg)

        return AckMessage.STATUS_OK, "OK"

    def _add_attachment_to_ticket(self, ticket: dict, file_name: str,
                                   file_type: str, media_id: str, save_path: str):
        """将附件信息追加到工单的 description 字段"""
        desc = ticket.get("description", "")
        type_label = "📷" if file_type == "image" else "📄"
        attach_text = f"\n\n{type_label} 附件: {file_name}"
        if media_id:
            attach_text += f" (media_id={media_id})"
        new_desc = desc + attach_text
        self.ticket_mgr.update_description(ticket["ticket_id"], new_desc)
        self.logger.info(f"附件已关联工单 {ticket['ticket_id']}: {file_name}")

    def _cleanup_expired_attachments(self):
        """清理过期的暂存附件（超过 _ATTACHMENT_TTL 的记录）"""
        now = time.time()
        with self.__attachment_lock__:
            expired_keys = [
                k for k, v in self._pending_attachments.items()
                if now - v.get("timestamp", 0) > self._ATTACHMENT_TTL
            ]
            expired = [(k, self._pending_attachments.pop(k)) for k in expired_keys]
        for k, attach in expired:
            # 尝试删除磁盘上的临时文件
            save_path = attach.get("save_path", "")
            self._delete_local_attachment(save_path)
            self.logger.info(f"过期附件暂存已清理: conv_id={k[:20]}...")
        if expired_keys:
            self.logger.info(f"清理了 {len(expired_keys)} 条过期附件暂存")

    @staticmethod
    def _safe_attachment_name(file_name: str) -> str:
        """返回不含路径和控制字符的安全显示文件名。"""
        name = os.path.basename(str(file_name).replace("\\", "/")).strip()
        name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name)
        return (name[:180] or "attachment")

    def _delete_local_attachment(self, save_path: str):
        if save_path and os.path.isfile(save_path):
            try:
                os.remove(save_path)
            except OSError as exc:
                self.logger.warning(f"附件临时文件清理失败: {exc}")

    def _attachment_cleanup_loop(self):
        while True:
            time.sleep(300)
            try:
                self._cleanup_expired_attachments()
            except Exception as exc:
                self.logger.warning(f"附件定时清理失败: {exc}")

    # ------------------------------------------------------------------
    # 语音消息处理
    # ------------------------------------------------------------------

    def _handle_voice_message(self, msg: ChatbotMessage, info: dict,
                               file_info: dict) -> tuple:
        """下载语音 → Whisper 转文字 → 作为普通消息继续处理"""
        download_code = file_info.get("download_code", "")
        duration = file_info.get("duration", "0")
        voice_format = file_info.get("format", "amr")
        file_name = self._safe_attachment_name(
            file_info.get("file_name", f"voice_{int(time.time())}.{voice_format}")
        )

        if not self.dtalk_api:
            self.reply_text("⚠️ 语音服务尚未配置，请联系管理员或改用文字描述问题", msg)
            return AckMessage.STATUS_OK, "OK"

        try:
            # 1. 下载语音文件
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "voice")
            os.makedirs(data_dir, exist_ok=True)
            save_path = os.path.join(data_dir, f"{uuid.uuid4().hex}_{file_name}")
            self.dingtalk_api.download_message_file(download_code, save_path)
            self.logger.info(f"语音文件下载: {save_path} (格式={voice_format}, 时长={duration}s)")

            # 2. Whisper 转文字
            try:
                from utils.speech_to_text import SpeechToText
                if not SpeechToText.is_available():
                    self.reply_text(
                        "🎤 收到语音消息，但语音识别功能暂未启用\n"
                        "请用文字描述你的问题，谢谢！",
                        msg,
                    )
                    return AckMessage.STATUS_OK, "OK"

                stt = SpeechToText.get_instance()
                text = stt.transcribe(save_path, language="zh")
                if not text:
                    self.reply_text("🎤 语音识别结果为空，请用文字描述你的问题", msg)
                    return AckMessage.STATUS_OK, "OK"

                self.logger.info(f"语音识别: '{text[:50]}' (时长={duration}s)")

                # 3. 提示用户识别结果，然后作为普通消息处理
                self.reply_text(f"🎤 语音识别: {text}\n---\n正在处理中...", msg)

                # 替换消息内容为识别结果，走完整文本处理流程
                info["content"] = text
                info["msgtype"] = "text"  # 标记为文本消息
                info["voice_transcribed"] = True

                # 走完整文本处理流程（包含工单、反馈、引擎切换等全部逻辑）
                return self._dispatch_text_message(msg, info)

            except Exception as e:
                self.logger.error(f"语音识别失败: {e}")
                self.reply_text(
                    f"🎤 语音识别失败: {e}\n请用文字描述你的问题",
                    msg,
                )
                return AckMessage.STATUS_OK, "OK"
            finally:
                self._delete_local_attachment(save_path)

        except Exception as e:
            self.logger.error(f"语音文件下载失败: {e}")
            self.reply_text("⚠️ 语音消息下载失败，请用文字描述你的问题", msg)
            return AckMessage.STATUS_OK, "OK"

    def _dispatch_text_message(self, msg: ChatbotMessage, info: dict) -> tuple:
        """
        完整文本消息调度逻辑（从 process 中提取）。
        供语音识别后及 process() 共用，确保所有消息走相同处理流程。
        包含：专家召唤、旧触发词、AI引擎切换、专家模式意图、反馈检测、工单系统、AI对话。
        """
        content = info["content"].strip()
        if not content:
            return AckMessage.STATUS_OK, "OK"

        expert_triggers = self.ima_config.get("expert_triggers", [])
        trigger_keywords = self.ima_config.get("trigger_keywords", [])
        conv_id = info["conv_id"]

        # 进行中的工单问诊优先消费回复，避免被知识搜索/AI 指令误判。
        if self.ticket_enabled and self.triage_engine.has_session(conv_id):
            result = self.triage_engine.consume(conv_id, content)
            if result["status"] == "cancelled":
                self.reply_text("✅ 已取消本次报修问诊，未创建工单。", msg)
                return AckMessage.STATUS_OK, "OK"
            if result["status"] == "question":
                self.reply_text(result["message"], msg)
                return AckMessage.STATUS_OK, "OK"
            if result["status"] == "completed":
                info["content"] = result["description"]
                info["triage_completed"] = True
                info["triage_category"] = result["category"]
                self._handle_ticket(msg, info, ParsedTicketAction(TicketIntent.CREATE), False)
                return AckMessage.STATUS_OK, "OK"

        # 1. 专家召唤触发
        matched_trigger = match_expert_trigger(content, expert_triggers)
        if matched_trigger:
            self._set_expert_state(conv_id, matched_trigger)
            self._handle_expert_summon(msg, info, matched_trigger)
            return AckMessage.STATUS_OK, "OK"

        # 2. 旧触发词（"搜"/"查"/"找"）
        if trigger_keywords and any(kw in content for kw in trigger_keywords):
            self._handle_knowledge_search(msg, info)
            return AckMessage.STATUS_OK, "OK"

        # 3. AI 引擎切换指令
        if content in ("切换CodeBuddy", "切换CB", "用CodeBuddy", "用cb"):
            self._switch_ai_engine("codebuddy", msg, info)
            return AckMessage.STATUS_OK, "OK"
        if content in ("切换DeepSeek", "切换DS", "用DeepSeek", "用ds"):
            self._switch_ai_engine("deepseek", msg, info)
            return AckMessage.STATUS_OK, "OK"
        if content in ("自动调度", "恢复自动", "自动引擎"):
            self._switch_ai_engine("auto", msg, info)
            return AckMessage.STATUS_OK, "OK"
        if content in ("当前引擎", "什么引擎", "用的什么AI"):
            self._show_ai_engine(msg, info)
            return AckMessage.STATUS_OK, "OK"

        # 4. 专家模式意图检测（退出/切换/列表）
        active_expert = self._get_expert_state(conv_id)
        if active_expert:
            intent = self._detect_expert_intent(content, active_expert, expert_triggers)
            if intent:
                if intent["action"] == "exit":
                    self._handle_expert_exit(conv_id, msg, active_expert)
                    return AckMessage.STATUS_OK, "OK"
                if intent["action"] == "switch":
                    self._handle_expert_switch(
                        conv_id, intent["target_trigger"], intent["target_phrase"],
                        msg, active_expert,
                    )
                    return AckMessage.STATUS_OK, "OK"
                if intent["action"] == "list":
                    self._handle_expert_list(msg, active_expert, expert_triggers)
                    return AckMessage.STATUS_OK, "OK"
                if intent["action"] == "switch_failed":
                    self._handle_switch_failed(msg, intent["target_phrase"], expert_triggers)
                    return AckMessage.STATUS_OK, "OK"

        # 5. 反馈回复检测（"是"/"否"）
        feedback_intent = TicketParser.detect_feedback(content)
        if feedback_intent:
            pending = self._get_pending_feedback(conv_id)
            if pending:
                if feedback_intent == TicketIntent.FEEDBACK_YES:
                    self._handle_feedback_yes(msg, info, pending)
                else:
                    self._handle_feedback_no(msg, info, pending)
                with self.__feedback_lock__:
                    self._pending_feedback.pop(conv_id, None)
                return AckMessage.STATUS_OK, "OK"

        # 6. 工单系统指令
        if self.ticket_enabled:
            is_admin = self._is_ticket_admin(
                info["sender_id"], info.get("sender_nick", ""),
                staff_id=info.get("staff_id", ""),
            )
            parsed = TicketParser.parse(content, info["sender_id"], is_admin)
            if parsed.intent != TicketIntent.NONE:
                handled = self._handle_ticket(msg, info, parsed, is_admin)
                if handled:
                    return AckMessage.STATUS_OK, "OK"

        # 7. 普通消息 → AI 对话
        if self.llm_client or self.cb_client:
            active_expert = self._get_expert_state(conv_id)
            self._handle_ai_chat(msg, info, active_expert)
            return AckMessage.STATUS_OK, "OK"

        # 8. 没有配置 LLM 时的默认处理
        self._handle_default(msg, info)
        return AckMessage.STATUS_OK, "OK"

    def _handle_file_test(self, msg: ChatbotMessage, info: dict):
        """
        测试文件发送功能。
        生成一个简单的测试文件（txt + 一个测试图片）并发送到当前会话。
        """
        import tempfile
        from datetime import datetime

        self.reply_text("🧪 开始测试文件发送功能...", msg)

        # 生成测试文本文件
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        test_content = f"钉钉机器人文件发送测试\n生成时间: {timestamp}\n\n这是一条测试消息，如果你能看到这个文件，说明文件发送功能正常工作！🦊"
        tmp_dir = tempfile.mkdtemp(prefix="dingtalk_test_")

        # 发送文本文件
        txt_path = os.path.join(tmp_dir, "测试文件.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(test_content)
        self.reply_file(txt_path, msg, info)

        # 发送测试图片（用纯色 PNG）
        try:
            png_path = os.path.join(tmp_dir, "测试图片.png")
            self._generate_test_image(png_path)
            time.sleep(1)  # 避免发送太快
            self.reply_image(png_path, msg, info)
        except Exception as e:
            self.logger.warning(f"测试图片生成失败（不影响文件测试）: {e}")

        # 清理临时文件
        try:
            os.remove(txt_path)
            if os.path.exists(png_path):
                os.remove(png_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass

        self.logger.info("文件发送测试完成")

    def _generate_test_image(self, path: str):
        """生成一张简单的测试图片（纯色背景 + 文字）"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (400, 200), color=(73, 109, 137))
            draw = ImageDraw.Draw(img)
            draw.rectangle([20, 20, 380, 180], outline=(255, 255, 255), width=3)
            draw.text((200, 80), "File Send Test OK", fill=(255, 255, 255), anchor="mm")
            draw.text((200, 120), "🦊", fill=(255, 255, 255), anchor="mm")
            img.save(path, "PNG")
        except ImportError:
            # 没有 Pillow，生成一个最小的 1x1 PNG
            import struct, zlib
            def make_png(w, h):
                def chunk(chunk_type, data):
                    c = chunk_type + data
                    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
                raw = b""
                for _ in range(h):
                    raw += b"\x00" + b"\x49\x6d\xA6\x89" * w  # 灰蓝色像素
                return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
            with open(path, "wb") as f:
                f.write(make_png(100, 100))

    # ------------------------------------------------------------------
    # 专家召唤处理
    # ------------------------------------------------------------------

    def _handle_expert_summon(self, msg: ChatbotMessage, info: dict, trigger: dict):
        """
        处理专家召唤。
        如果用户附带问题（如"召唤U9专家 怎么改密码"），直接 AI 回答；
        如果只发召唤词，回复专家已就位。
        """
        content = info["content"].strip()
        expert_label = trigger["phrase"]
        trigger_keywords = self.ima_config.get("trigger_keywords", [])
        expert_triggers = self.ima_config.get("expert_triggers", [])

        # 提取搜索关键词（去掉专家触发词前缀）
        query = extract_query(
            content,
            trigger_keywords=trigger_keywords,
            expert_triggers=expert_triggers,
        )
        self.logger.info(f"专家召唤: expert={expert_label}, query='{query}'")

        # 只召唤了专家，没有附带问题
        if not query:
            self.reply_text(
                f"🦊 {expert_label}⚡ 已就位！\n"
                f"后续的问题我都会基于这位专家的知识库来回答哦～\n"
                f"（30 分钟内未对话会自动退出专家模式）",
                msg,
            )
            return

        # 附带了问题 → 直接按专家范围搜索知识库 + AI 回答
        target_kbs = self._resolve_target_kbs(trigger)
        self._handle_ai_chat(msg, info, active_expert=trigger, forced_query=query, target_kbs=target_kbs)

    # ------------------------------------------------------------------
    # AI 对话处理
    # ------------------------------------------------------------------

    def _handle_ai_chat(
        self,
        msg: ChatbotMessage,
        info: dict,
        active_expert: dict | None = None,
        forced_query: str | None = None,
        target_kbs: list | None = None,
    ):
        """
        AI 对话模式：
        1. 先搜索 IMA 知识库获取相关内容（按 active_expert 限定范围）
        2. 将用户问题 + 知识库内容组合成 prompt
        3. 调用 DeepSeek 生成回答
        """
        query = forced_query or info["content"].strip()
        expert_label = active_expert["phrase"] if active_expert else None

        # 确定搜索范围
        if target_kbs is None:
            target_kbs = self._resolve_target_kbs(active_expert)

        # 先搜索知识库获取上下文
        knowledge_context = ""
        has_kb_results = False
        if self.ima_client:
            try:
                max_results = self.ima_config.get("max_results", 5)
                # 专家模式：提升搜索数量上限，覆盖更多相关文档
                if active_expert:
                    max_results = max(max_results, 8)

                # ── IMA 搜索策略（适配词级匹配引擎） ──
                # IMA search_knowledge 是词级精确匹配：
                #   "U9" → 67条, "U9怎么用啊" → 0条
                # 策略：
                #   1. 先用完整 query 搜索
                #   2. 如果无结果，拆分关键词逐个搜索
                #   3. 合并去重

                from handlers.knowledge_handler import extract_keywords

                # 步骤1：完整 query 搜索
                if target_kbs:
                    results = self.ima_client.search_knowledge_bases(query, target_kbs)
                else:
                    results = self.ima_client.search_all_knowledge_bases(query)

                self.logger.info(
                    f"知识库搜索: query='{query}', results={len(results)}"
                    f"{' [专家=' + expert_label + ']' if expert_label else ' [默认范围]' }"
                )

                # 步骤2：无结果时，拆分关键词逐个搜索
                if not results:
                    kw_list = extract_keywords(query)
                    self.logger.info(f"完整query无结果，拆分关键词搜索: {kw_list}")

                    seen_titles = set()
                    for kw in kw_list:
                        if len(results) >= max_results * 2:
                            break
                        self.logger.info(f"  [关键词] 搜索: '{kw}'")
                        if target_kbs:
                            kw_results = self.ima_client.search_knowledge_bases(kw, target_kbs)
                        else:
                            kw_results = self.ima_client.search_all_knowledge_bases(kw)
                        for item in kw_results:
                            title = item.get("title", "")
                            if title and title not in seen_titles:
                                seen_titles.add(title)
                                results.append(item)
                        self.logger.info(
                            f"  [关键词] '{kw}' 搜到 {len(kw_results)} 条 "
                            f"(累计 {len(results)} 条)"
                        )

                if results:
                    has_kb_results = True
                    # ── 质量过滤：只保留 highlight 有实质内容的条目 ──
                    # highlight 为空或只有标题时，该条目对 AI 几乎没有参考价值
                    def _has_quality_content(item: dict) -> bool:
                        highlight = strip_html(item.get("highlight", "") or item.get("highlight_content", ""))
                        description = strip_html(item.get("description", "") or item.get("content", ""))
                        content = highlight or description
                        return len(content.strip()) >= 30  # 至少30字才算有实质内容

                    quality_results = [r for r in results if _has_quality_content(r)]
                    low_quality = len(results) - len(quality_results)
                    if quality_results:
                        if low_quality > 0:
                            self.logger.info(f"质量过滤: 过滤掉 {low_quality} 条摘要过短的结果，保留 {len(quality_results)} 条")
                        results = quality_results
                    else:
                        # 所有结果都低质量时保留全部（总比什么都没有强）
                        self.logger.info(f"质量过滤: 所有 {len(results)} 条结果摘要较短，保留全部")

                    # 构建知识库上下文
                    context_parts = []
                    for i, item in enumerate(results[:max_results], 1):
                        title = item.get("title", "")
                        source_kb = item.get("source_kb", "")
                        highlight = strip_html(item.get("highlight", ""))
                        if not highlight:
                            highlight = strip_html(item.get("highlight_content", ""))
                        if not highlight:
                            highlight = strip_html(item.get("description", ""))
                        if not highlight:
                            highlight = strip_html(item.get("content", ""))
                        if highlight:
                            context_parts.append(f"[{i}] {title}\n来源: {source_kb}\n{highlight}")
                        elif title:
                            context_parts.append(f"[{i}] {title}（来源: {source_kb}，暂无摘要内容）")
                    knowledge_context = "\n\n".join(context_parts)
                    self.logger.info(
                        f"知识库检索到 {len(results)} 条结果，构建上下文 {len(context_parts)} 条"
                    )
                else:
                    self.logger.info(
                        f"知识库无相关结果，将使用 AI 通用回答"
                        f"{' [专家=' + expert_label + ']' if expert_label else ''}"
                    )
            except Exception as e:
                self.logger.error(f"知识库搜索异常: {e}")

        # 构建 prompt 调用 AI
        system_prompt = self._build_system_prompt(knowledge_context, active_expert, has_kb_results)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        # 调用 AI
        thinking_text = f"🦊 {expert_label} 正在思考..." if expert_label else "🤔 正在思考..."
        self.reply_text(thinking_text, msg)

        # ── AI 引擎调度 ──
        # 规则：
        #   专家模式 → CodeBuddy 优先，失败降级 DeepSeek
        #   普通模式 → DeepSeek 优先
        #   手动切换 → 忽略以上规则，强制用指定引擎
        answer = None
        engine_used = "none"

        # 确定首选引擎
        engine_override = self._get_ai_engine_override(info["conv_id"])
        if engine_override:
            # 用户手动指定了引擎，忽略自动调度
            primary_engine = engine_override
        elif expert_label:
            # 专家模式 → CodeBuddy 优先
            primary_engine = "codebuddy"
        else:
            # 普通模式优先 DeepSeek；仅配置 CodeBuddy 时直接使用 CodeBuddy
            primary_engine = "deepseek" if self.llm_client else "codebuddy"

        # 记录到后台状态
        engine_label = {"deepseek": "DeepSeek", "codebuddy": "CodeBuddy Code"}.get(primary_engine, primary_engine)
        self.logger.info(f"AI 调度: 首选引擎={engine_label}, 专家模式={'是('+expert_label+')' if expert_label else '否'}")

        if primary_engine == "codebuddy":
            # CodeBuddy 优先
            if self.cb_client:
                try:
                    answer = self.cb_client.chat(messages, system_prompt=system_prompt)
                except Exception as e:
                    self.logger.warning(f"CodeBuddy 调用异常: {e}")
                if answer:
                    engine_used = "codebuddy"
                else:
                    self.logger.info("CodeBuddy 返回空，降级 DeepSeek")
            if not answer and self.llm_client:
                try:
                    answer = self.llm_client.chat(messages)
                except DeepSeekAPIError as e:
                    self.logger.error(f"DeepSeek API 错误: {e}")
                    answer = f"AI 服务暂时不可用: {e}"
                except Exception as e:
                    self.logger.error(f"DeepSeek 调用异常: {e}")
                if answer and engine_used == "none":
                    engine_used = "deepseek"
        elif primary_engine == "deepseek":
            # DeepSeek 优先
            if self.llm_client:
                try:
                    answer = self.llm_client.chat(messages)
                except DeepSeekAPIError as e:
                    self.logger.error(f"DeepSeek API 错误: {e}")
                    answer = f"AI 服务暂时不可用: {e}"
                except Exception as e:
                    self.logger.error(f"DeepSeek 调用异常: {e}")
                if answer and engine_used == "none":
                    engine_used = "deepseek"
            # DeepSeek 失败时不自动降级 CodeBuddy（普通模式不需要）
        else:
            self.logger.error(f"未知引擎类型: {primary_engine}")

        # 记录本次使用的引擎到后台状态
        bot_status.set_last_engine(engine_used, expert_label)

        # 回复
        if answer:
            # ── 安全性检查：确保 answer 不是原始 JSON ──
            # 如果 codebuddy_client 解析失败导致原始 JSON 被传回来，在此做最后清洗
            answer = self._sanitize_answer(answer)

            # 知识库无结果时，在回答前加提示
            if not has_kb_results:
                if engine_used == "codebuddy":
                    answer = "⚠️ **知识库暂无相关内容，以下为 AI 通用回答，仅供参考。**\n\n" + answer
                # 若处于专家模式且无知识库结果，追加切换/退出提示
                if expert_label:
                    answer += (
                        "\n\n---\n"
                        f"💡 当前由 **{expert_label}** 为您服务\n"
                        f"⚠️ 此问题未在 {expert_label} 知识库中找到相关内容。\n"
                        f"🔧 可发送「**退出专家模式**」回到普通模式，或「**切换XXX专家**」更换专家"
                    )
            elif expert_label:
                answer += f"\n\n---\n💡 当前由 **{expert_label}** 为您服务"

            # ── 追加反馈提示 ──
            answer += TicketFormatter.FEEDBACK_PROMPT
            self.reply_text(answer, msg)

            # ── 记录反馈状态（用于后续"是/否"检测） ──
            if self.ticket_enabled:
                self._set_pending_feedback(
                    conv_id=info["conv_id"],
                    question=query,
                    answer_preview=answer[:200],
                    has_kb=has_kb_results,
                )
        else:
            self.reply_text("抱歉，我暂时无法回答这个问题 (´・ω・`)", msg)

        # ── 记录结构化消息日志（用于分类导出） ──
        try:
            kb_count = len(re.findall(r'^\[(\d+)\]', knowledge_context, re.MULTILINE)) if knowledge_context else 0
            msg_logger.record({
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg_id": info.get("msg_id", ""),
                "sender_nick": info.get("sender_nick", "未知"),
                "sender_id": info.get("sender_id", "?"),
                "conv_type": info.get("conv_label", "?"),
                "conv_title": info.get("conv_title", ""),
                "content": info.get("content", ""),
                "expert_mode": expert_label or "普通对话",
                "expert_scope": active_expert.get("scope", "default") if active_expert else "default",
                "has_kb": has_kb_results,
                "kb_results": kb_count,
                "engine": engine_used,
                "answer_len": len(answer) if answer else 0,
                "answer_preview": (answer[:100] if answer else "(无回答)"),
            })
        except Exception as _le:
            self.logger.warning(f"消息日志记录失败: {_le}")

    def _build_system_prompt(self, knowledge_context: str, active_expert: dict | None = None, has_kb_results: bool = True) -> str:
        """
        构建系统提示词。
        若处于专家模式，在提示词中明确告知 AI 当前扮演的专家角色。
        has_kb_results=False 时，允许 AI 使用通用知识回答，但要客观说明。
        """
        expert_label = active_expert["phrase"] if active_expert else None
        expert_desc = active_expert.get("description", "") if active_expert else ""

        base_prompt = "你是日盛集团的 IT 运维助手，负责解答公司内部系统使用问题。"

        if expert_label:
            base_prompt += f"\n\n## 当前专家模式\n你当前正以 **{expert_label}** 的身份回答用户问题。"
            if expert_desc:
                base_prompt += f"（{expert_desc}）"
            base_prompt += "\n请优先基于该专家对应的知识库内容来回答，保持专业性和准确性。"

        if has_kb_results:
            # 有知识库结果 → 严格基于知识库回答
            base_prompt += """

## 回答规范
- 必须基于上面提供的知识库内容回答
- 如果知识库内容包含操作步骤，必须以编号列表形式输出（1. 2. 3.）
- 回答末尾须标注信息来源（[来源: 文档名]），若有多个来源分别标注
- 禁止编造知识库中没有的步骤或信息；知识库未提及的内容不要补充
- 如果知识库内容不能完全解答问题，可以补充通用知识，但须明确注明"以下为通用建议"
- 使用中文回答
- 回答控制在 800 字以内（操作步骤多时可适当放开）"""
        else:
            # 无知识库结果 → 允许 AI 用通用知识回答，但要提示用户
            base_prompt += """

## 回答规范
- 当前知识库中未检索到与用户问题直接相关的内容，请基于你的通用知识回答
- 在回答开头务必说明"知识库中暂未收录相关内容"
- 如果问题与 IT 运维无关，可以简短回答，但建议用户召唤专家获取更精准的帮助
- 使用中文回答
- 不要编造不存在的信息
- 回答控制在 500 字以内，除非问题需要详细步骤"""

        if knowledge_context and has_kb_results:
            base_prompt += f"""

## 知识库检索结果（按相关度排序）
以下是用户问题相关的知识库内容，请参考这些内容来回答：

{knowledge_context}

---
请基于以上知识库内容回答用户的问题。如果知识库内容不能完全解答，可以补充你的知识，但要明确区分。"""

        return base_prompt

    # ------------------------------------------------------------------
    # 知识库搜索处理（旧触发词模式，返回原始搜索结果）
    # ------------------------------------------------------------------

    def _handle_knowledge_search(self, msg: ChatbotMessage, info: dict):
        """
        知识库搜索处理（旧触发词模式："搜"/"查"/"找"）。
        返回原始搜索结果，方便用户直接查看来源。
        """
        content = info["content"].strip()
        expert_triggers = self.ima_config.get("expert_triggers", [])
        trigger_keywords = self.ima_config.get("trigger_keywords", [])

        # 提取搜索关键词
        query = extract_query(content, trigger_keywords=trigger_keywords, expert_triggers=expert_triggers)
        self.logger.info(f"触发知识库搜索(旧模式): query='{query}'")

        if not query:
            self.reply_text("请告诉我你想搜索什么内容～ 🦊", msg)
            return

        try:
            max_results = self.ima_config.get("max_results", 5)
            default_kbs = self.ima_config.get("default_knowledge_bases", [])
            md_text = search_and_format(self.ima_client, query, max_results, default_kbs)
            self.reply_markdown("🔍 知识库搜索结果", md_text, msg)
        except Exception as e:
            self.logger.error(f"知识库搜索失败: {e}")
            self.reply_text(f"知识库搜索出错了: {e} (´・ω・`)", msg)

    # ------------------------------------------------------------------
    # 通用工具方法
    # ------------------------------------------------------------------

    def _extract_message_info(self, msg: ChatbotMessage) -> dict:
        """从 ChatbotMessage 中提取关键信息。"""
        # ── 检测消息类型 ──
        # SDK 使用 message_type；旧代码读取 msgtype 会把所有非文本误判为 text。
        msgtype = (
            getattr(msg, "message_type", None)
            or getattr(msg, "msgtype", None)
            or "text"
        )
        extensions = getattr(msg, "extensions", {}) or {}
        raw_content = extensions.get("content", {})
        if not isinstance(raw_content, dict):
            raw_content = {}

        # ── 提取文本内容 ──
        try:
            content = ""
            if hasattr(msg, "text") and hasattr(msg.text, "content"):
                content = msg.text.content.strip()
            elif hasattr(msg, "text") and isinstance(msg.text, dict):
                content = msg.text.get("content", "").strip()
        except Exception:
            content = str(getattr(msg, "text", ""))

        # ── 提取文件/图片/语音信息 ──
        file_info = {}
        if msgtype in ("picture", "file", "voice", "audio", "richText", "rich_text"):
            # 图片消息
            if msgtype == "picture":
                pic = getattr(msg, "image_content", None) or getattr(msg, "picture", None) or raw_content
                if isinstance(pic, dict):
                    file_info = {
                        "type": "image",
                        "download_code": pic.get("downloadCode", "") or pic.get("pictureDownloadCode", ""),
                        "file_name": f"image_{int(time.time())}.jpg",
                    }
                elif hasattr(pic, "download_code") or hasattr(pic, "downloadCode"):
                    file_info = {
                        "type": "image",
                        "download_code": getattr(pic, "download_code", "") or getattr(pic, "downloadCode", "") or getattr(pic, "pictureDownloadCode", ""),
                        "file_name": f"image_{int(time.time())}.jpg",
                    }
            # 文件消息
            elif msgtype == "file":
                fobj = getattr(msg, "file", None) or raw_content
                if isinstance(fobj, dict):
                    file_info = {
                        "type": "file",
                        "download_code": fobj.get("downloadCode", ""),
                        "file_name": fobj.get("fileName", "unknown_file"),
                    }
                elif hasattr(fobj, "downloadCode"):
                    file_info = {
                        "type": "file",
                        "download_code": getattr(fobj, "downloadCode", ""),
                        "file_name": getattr(fobj, "fileName", "unknown_file"),
                    }
            # 语音消息
            elif msgtype in ("voice", "audio"):
                # 当前 SDK 未定义 VoiceContent，原始 content 会保存在 extensions。
                vobj = getattr(msg, "voice", None) or getattr(msg, "audio", None) or raw_content
                if isinstance(vobj, dict):
                    voice_format = vobj.get("format", "") or vobj.get("audioFormat", "") or "amr"
                    file_info = {
                        "type": "voice",
                        "download_code": vobj.get("downloadCode", "") or vobj.get("download_code", ""),
                        "duration": vobj.get("duration", "0") or vobj.get("durationMs", "0"),
                        "format": voice_format,
                        "recognition": vobj.get("recognition", "") or vobj.get("text", ""),
                        "file_name": f"voice_{int(time.time())}.{voice_format}",
                    }
                elif hasattr(vobj, "downloadCode") or hasattr(vobj, "download_code"):
                    voice_format = getattr(vobj, "format", "amr") or "amr"
                    file_info = {
                        "type": "voice",
                        "download_code": getattr(vobj, "downloadCode", "") or getattr(vobj, "download_code", ""),
                        "duration": getattr(vobj, "duration", "0"),
                        "format": voice_format,
                        "recognition": getattr(vobj, "recognition", ""),
                        "file_name": f"voice_{int(time.time())}.{voice_format}",
                    }

            # 如果文本为空，用文件类型生成占位内容
            if not content:
                type_labels = {"image": "[图片]", "file": "[文件]", "voice": "[语音]"}
                content = type_labels.get(file_info.get("type", ""), "[附件]")

        # dingtalk_stream SDK 属性名均为 snake_case
        conv_type = getattr(msg, "conversation_type", "1")
        conv_id = getattr(msg, "conversation_id", "")
        sender_id = getattr(msg, "sender_id", "")
        sender_nick = getattr(msg, "sender_nick", "") or getattr(msg, "sender_name", "") or "未知"
        # staff_id 是企业内 userId（oToMessages/batchSend 要求），
        # 注意：不回退到 sender_id（openId），否则 API 会报 staffId.notExisted
        sender_staff_id = getattr(msg, "sender_staff_id", "") or ""
        sender_corp_id = getattr(msg, "sender_corp_id", "")
        chatbot_user_id = getattr(msg, "chatbot_user_id", "") or ""
        conv_title = getattr(msg, "conversation_title", "") or ""

        # 调试日志：dump 原始消息关键字段（帮助排查 staffId 获取问题）
        self.logger.debug(
            f"消息原始字段 | sender_id={sender_id[:20] if sender_id else '空'} | "
            f"sender_staff_id={sender_staff_id[:20] if sender_staff_id else '空'} | "
            f"chatbot_user_id={chatbot_user_id[:20] if chatbot_user_id else '空'} | "
            f"sender_corp_id={sender_corp_id[:15] if sender_corp_id else '空'} | "
            f"sender_nick={sender_nick} | extensions_keys={list(msg.extensions.keys()) if hasattr(msg, 'extensions') and msg.extensions else '无'}"
        )

        return {
            "msg_id": getattr(msg, "message_id", ""),
            "msgtype": msgtype,
            "conv_type": conv_type,
            "conv_label": "单聊" if conv_type == self.CONV_TYPE_P2P else "群聊",
            "conv_id": conv_id,
            "conv_title": conv_title,
            "sender_id": sender_id,
            "sender_nick": sender_nick,
            "sender_corp_id": sender_corp_id,
            "staff_id": sender_staff_id,  # 企业内 userId，空=未获取到
            "chatbot_user_id": chatbot_user_id,
            "content": content,
            "file_info": file_info,  # 文件/图片/语音信息
            "create_at": getattr(msg, "create_at", int(time.time() * 1000)),
            "is_admin": getattr(msg, "is_admin", False),
        }

    def _check_keyword(self, content: str) -> bool:
        """关键词过滤，空列表表示不过滤"""
        keywords = self.chat_config.get("keyword_filter", [])
        if not keywords:
            return True
        return any(kw in content for kw in keywords)

    def _handle_echo(self, msg: ChatbotMessage, info: dict):
        """调试模式：原样回显消息"""
        label = "单聊" if info["conv_type"] == self.CONV_TYPE_P2P else "群聊"
        text = f"[{label} 回显] {info['content']}"
        self.reply_text(text, msg)

    # ------------------------------------------------------------------
    # AI 引擎切换
    # ------------------------------------------------------------------

    def _switch_ai_engine(self, engine: str, msg: ChatbotMessage, info: dict):
        """切换 AI 引擎（手动覆盖自动调度）"""
        conv_id = info["conv_id"]
        if engine == "auto":
            with self.__engine_lock__:
                self._ai_engine_overrides.pop(conv_id, None)
            self.logger.info("AI 引擎切换: 恢复自动调度")
            self.reply_text(
                "🦊 AI 引擎已恢复 **自动调度**\n"
                "- 专家模式 → CodeBuddy 优先（失败降级 DeepSeek）\n"
                "- 普通模式 → DeepSeek 优先（未配置时使用 CodeBuddy）",
                msg,
            )
            return

        if engine == "codebuddy" and not self.cb_client:
            self.reply_text("CodeBuddy AI 未配置或服务未启动，无法切换 (´・ω・`)", msg)
            return
        if engine == "deepseek" and not self.llm_client:
            self.reply_text("DeepSeek 未配置，无法切换 (´・ω・`)", msg)
            return

        with self.__engine_lock__:
            self._ai_engine_overrides[conv_id] = {
                "engine": engine,
                "timestamp": time.time(),
            }
        engine_names = {"deepseek": "DeepSeek", "codebuddy": "CodeBuddy Code"}
        name = engine_names.get(engine, engine)
        self.logger.info(f"AI 引擎手动切换: {engine}")
        self.reply_text(
            f"🦊 AI 引擎已手动锁定为 **{name}**\n"
            f"发送「自动调度」恢复智能调度（专家模式自动走CodeBuddy）",
            msg,
        )

    def _get_ai_engine_override(self, conv_id: str) -> str | None:
        with self.__engine_lock__:
            state = self._ai_engine_overrides.get(conv_id)
            if not state:
                return None
            if time.time() - state["timestamp"] > self._ENGINE_OVERRIDE_TTL:
                self._ai_engine_overrides.pop(conv_id, None)
                return None
            state["timestamp"] = time.time()
            return state["engine"]

    def _show_ai_engine(self, msg: ChatbotMessage, info: dict):
        """显示当前 AI 引擎调度规则和状态"""
        engine_names = {"deepseek": "DeepSeek", "codebuddy": "CodeBuddy Code"}
        ds_status = "✅ 可用" if self.llm_client else "❌ 未配置"
        cb_status = "✅ 可用" if self.cb_client else "❌ 未配置"

        engine_override = self._get_ai_engine_override(info["conv_id"])
        if engine_override:
            mode = f"本会话手动锁定 → **{engine_names.get(engine_override, engine_override)}**"
        else:
            mode = "**自动调度**"

        self.reply_text(
            f"🤖 AI 引擎调度规则: {mode}\n\n"
            f"引擎状态:\n"
            f"- DeepSeek: {ds_status}\n"
            f"- CodeBuddy: {cb_status}\n\n"
            f"自动调度规则:\n"
            f"- 专家模式 → CodeBuddy 优先（失败降级 DeepSeek）\n"
            f"- 普通模式 → DeepSeek 优先（未配置时使用 CodeBuddy）\n\n"
            f"指令:\n"
            f"- 「切换CodeBuddy」/「切换DeepSeek」手动锁定\n"
            f"- 「自动调度」恢复智能调度",
            msg,
        )

    # ------------------------------------------------------------------
    # 答案安全检查
    # ------------------------------------------------------------------

    def _sanitize_answer(self, answer: str) -> str:
        """
        安全检查：如果 answer 是未解析的 JSON 响应，提取真实内容。
        防止 codebuddy_client 解析失败导致原始 JSON 被发送到钉钉。
        """
        if not answer or not isinstance(answer, str):
            return answer

        stripped = answer.strip()
        # 只有看起来像 JSON 时才处理
        if not stripped.startswith("{"):
            return answer

        try:
            import json as _json
            parsed = _json.loads(stripped)
        except (ValueError, TypeError):
            return answer

        if not isinstance(parsed, dict):
            return answer

        # CodeBuddy 直接响应格式: {"version":"1.0","status":"completed","content":{...}}
        if "content" in parsed:
            content = parsed["content"]
            if isinstance(content, dict):
                text = content.get("markdown") or content.get("text") or ""
                if text:
                    self.logger.warning(
                        f"⚠️ 检测到未解析的 JSON 响应，已自动提取 content: {text[:50]}..."
                    )
                    return text
            elif isinstance(content, str):
                self.logger.warning(
                    f"⚠️ 检测到未解析的 JSON 响应，已自动提取 content 字符串: {content[:50]}..."
                )
                return content

        # 选择框格式: 取第一个选项的 text
        choices = parsed.get("choices", [])
        if choices:
            msg = choices[0].get("message", {}) or choices[0].get("delta", {})
            text = msg.get("content") or msg.get("text", "")
            if text:
                self.logger.warning(f"⚠️ 检测到未解析的 choices 格式 JSON，已自动提取")
                return text

        self.logger.warning(f"⚠️ 检测到未知 JSON 格式的 answer，保持原文: {stripped[:100]}...")
        return answer

    # ------------------------------------------------------------------
    # 默认消息处理
    # ------------------------------------------------------------------

    def _handle_default(self, msg: ChatbotMessage, info: dict):
        """
        默认消息处理（无 LLM 配置时的兜底）。
        """
        label = "单聊" if info["conv_type"] == self.CONV_TYPE_P2P else "群聊"

        help_tip = ""
        if self.ima_client:
            help_tip = "\n> 💡 发送「召唤运维专家 xxx」或「召唤U9专家 xxx」可以精准搜索知识库"

        title = f"🤖 消息已接收 [{label}]"
        md_text = (
            f"### 收到{label}消息\n\n"
            f"- **发送者**: {info['sender_nick']}\n"
            f"- **内容**: {info['content']}\n"
            f"- **会话ID**: {info['conv_id']}"
            f"{help_tip}\n\n"
            f"---\n"
            f"*消息处理时间: {time.strftime('%Y-%m-%d %H:%M:%S')}*"
        )
        self.reply_markdown(title, md_text, msg)

    # ------------------------------------------------------------------
    # 富媒体消息发送（文件/图片）
    # ------------------------------------------------------------------

    def reply_file(self, file_path: str, msg: ChatbotMessage, info: dict | None = None):
        """
        发送文件到当前会话（群聊或单聊）。
        通过钉钉 Media Upload API 上传文件后发送。

        Args:
            file_path: 本地文件路径
            msg: ChatbotMessage 对象（用于获取会话信息）
            info: 消息信息字典（可选，用于获取 conv_type/user_id）
        """
        if not self.dtalk_api:
            self.logger.warning("钉钉 API 未初始化，无法发送文件")
            self.reply_text("文件发送功能未启用 🦊", msg)
            return

        if not os.path.exists(file_path):
            self.logger.error(f"文件不存在: {file_path}")
            self.reply_text(f"文件不存在: {file_path} (´・ω・`)", msg)
            return

        # 判断文件类型
        ext = os.path.splitext(file_path)[1].lower()
        file_type = "image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp") else "file"

        try:
            # 上传文件
            self.reply_text(f"📎 正在发送文件 `{os.path.basename(file_path)}`...", msg)
            media_id = self.dtalk_api.upload_file(file_path, file_type)

            # 发送到会话
            conv_type = info["conv_type"] if info else getattr(msg, "conversation_type", "1")
            conv_id = info["conv_id"] if info else getattr(msg, "conversation_id", "")
            # 单聊时优先用 staff_id（钉钉企业 userId），其次用 sender_id
            user_id = (info.get("staff_id", "") or info.get("sender_id", "")) if info else (getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", ""))
            base_name = os.path.basename(file_path)

            self.logger.info(
                f"[reply_file] conv_type={conv_type}, conv_id={conv_id[:20] if conv_id else 'N/A'}..., "
                f"user_id={user_id[:10] if user_id else 'EMPTY'}..., file={base_name}"
            )

            if conv_type == self.CONV_TYPE_GROUP:
                result = self.dtalk_api.send_file_to_chat(conv_id, media_id, file_name=base_name)
            else:
                if not user_id:
                    self.logger.error("单聊场景下无法获取用户 user_id，无法发送文件")
                    self.reply_text("文件发送失败：无法获取你的用户ID，请联系管理员检查机器人权限配置 (´・ω・`)", msg)
                    return
                result = self.dtalk_api.send_file_to_user(user_id, media_id, file_name=base_name)

            if result.get("errcode") == 0:
                self.logger.info(f"文件发送成功: {file_path}")
            else:
                self.logger.warning(f"文件发送返回: {result}")
                self.reply_text(f"文件发送可能未成功，请检查日志 (´・ω・`)", msg)

        except Exception as e:
            self.logger.error(f"文件发送失败: {e}")
            self.reply_text(f"文件发送失败: {e} (´・ω・`)", msg)

    def reply_image(self, image_path: str, msg: ChatbotMessage, info: dict | None = None):
        """
        发送图片到当前会话。封装 reply_file，自动设为 image 类型。
        """
        self.reply_file(image_path, msg, info)

    def reply_with_file_and_text(
        self,
        text: str,
        file_path: str,
        msg: ChatbotMessage,
        info: dict | None = None,
    ):
        """
        同时发送文本 + 文件。先发文本说明，再发文件。
        """
        self.reply_text(text, msg)
        self.reply_file(file_path, msg, info)
