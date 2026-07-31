"""Regression coverage for the V2.16.1 runtime and answer-routing fixes."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import unittest

from handlers.chat_handler import (
    BotMessageHandler,
    _ai_failure_message,
    _filter_usable_knowledge_results,
    _is_legacy_knowledge_command,
    _resolve_runtime_path,
)
from modules.ticket.ticket_parser import TicketIntent, TicketParser
from utils.deepseek_client import DeepSeekAPIError


class RuntimePathRegressionTests(unittest.TestCase):
    def test_relative_ticket_path_is_resolved_from_persistent_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_root = Path(tmp_dir)
            with patch(
                "handlers.chat_handler._get_base_dir",
                return_value=runtime_root,
            ):
                resolved = _resolve_runtime_path("data/tickets.db")

        self.assertEqual(resolved, runtime_root / "data" / "tickets.db")


class LegacyKnowledgeRoutingRegressionTests(unittest.TestCase):
    TRIGGERS = ["搜", "查", "找", "知识库"]

    @classmethod
    def _would_route_to_legacy_knowledge(cls, content: str) -> bool:
        parsed = TicketParser.parse(content, user_id="user-1", is_admin=False)
        return (
            parsed.intent == TicketIntent.NONE
            and _is_legacy_knowledge_command(content, cls.TRIGGERS)
        )

    def test_ticket_list_phrase_is_not_intercepted_by_knowledge_search(self):
        action = TicketParser.parse("查看我的工单", "user-1", False)

        self.assertEqual(action.intent, TicketIntent.LIST_MINE)
        self.assertFalse(self._would_route_to_legacy_knowledge("查看我的工单"))

    def test_ticket_id_query_is_not_intercepted_by_knowledge_search(self):
        content = "查询工单 TKT-20260727-001"
        action = TicketParser.parse(content, "user-1", False)

        self.assertEqual(action.intent, TicketIntent.VIEW)
        self.assertEqual(action.ticket_id, "TKT-20260727-001")
        self.assertFalse(self._would_route_to_legacy_knowledge(content))

    def test_explicit_search_prefix_still_routes_to_knowledge_search(self):
        content = "查 VPN"

        self.assertEqual(
            TicketParser.parse(content, "user-1", False).intent,
            TicketIntent.NONE,
        )
        self.assertTrue(self._would_route_to_legacy_knowledge(content))

    @staticmethod
    def _dispatch_handler():
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.ticket_enabled = True
        handler.triage_engine = Mock()
        handler.triage_engine.has_session.return_value = False
        handler.resolution_enabled = False
        handler.resolution_engine = Mock()
        handler.ima_config = {
            "expert_triggers": [],
            "trigger_keywords": ["搜", "查", "找", "知识库"],
        }
        handler.conversation_states = Mock()
        handler.conversation_states.get_spreadsheet.return_value = None
        handler.llm_client = object()
        handler.cb_client = None
        handler._get_expert_state = Mock(return_value=None)
        handler._handle_ai_chat = Mock()
        handler._handle_ticket = Mock(return_value=True)
        handler._handle_knowledge_search = Mock()
        handler._is_ticket_admin = Mock(return_value=False)
        handler.reply_text = Mock()
        return handler

    @staticmethod
    def _message_info(content: str) -> dict:
        return {
            "content": content,
            "conv_id": "conversation-1",
            "sender_id": "user-1",
            "sender_nick": "Test User",
            "staff_id": "staff-1",
            "conv_title": "",
        }

    def test_dispatch_routes_ticket_query_before_legacy_search(self):
        handler = self._dispatch_handler()

        handler._dispatch_text_message(
            object(), self._message_info("查看我的工单")
        )

        handler._handle_ticket.assert_called_once()
        handler._handle_knowledge_search.assert_not_called()

    def test_ordinary_ai_message_skips_admin_database_lookup(self):
        handler = self._dispatch_handler()

        handler._dispatch_text_message(
            object(), self._message_info("打印机颜色有点偏淡")
        )

        handler._is_ticket_admin.assert_not_called()
        handler._handle_ai_chat.assert_called_once()


class KnowledgeQualityRegressionTests(unittest.TestCase):
    def test_title_only_and_short_hits_are_dropped_but_real_body_is_kept(self):
        title_only = {"title": "VPN 使用手册"}
        short_excerpt = {"title": "短摘要", "description": "A" * 29}
        exact_threshold = {"title": "有效摘要", "description": "B" * 30}
        long_body_with_short_highlight = {
            "title": "正文有效",
            "highlight": "命中词",
            "content": "C" * 40,
        }

        usable = _filter_usable_knowledge_results(
            [
                title_only,
                short_excerpt,
                exact_threshold,
                long_body_with_short_highlight,
                "invalid-result",
            ],
            min_content_chars=30,
        )

        self.assertEqual(usable, [exact_threshold, long_body_with_short_highlight])


class AIFailureMessageRegressionTests(unittest.TestCase):
    def test_status_specific_messages_do_not_leak_raw_exception(self):
        cases = (
            (429, "当前繁忙"),
            (401, "认证失败"),
            (504, "响应超时"),
            (500, "暂时不可用"),
        )
        for status_code, expected_reason in cases:
            with self.subTest(status_code=status_code):
                raw_detail = f"upstream secret token for HTTP {status_code}"
                error = DeepSeekAPIError(raw_detail, status_code=status_code)
                message = _ai_failure_message(
                    "ERR-V2161",
                    getattr(error, "status_code", None),
                )

                self.assertIn(expected_reason, message)
                self.assertIn("错误编号：ERR-V2161", message)
                self.assertNotIn(raw_detail, message)
                self.assertNotIn("secret token", message)


class AIFailureFlowRegressionTests(unittest.TestCase):
    def test_deepseek_failure_does_not_advance_resolution_or_feedback_state(self):
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.logger = Mock()
        handler.ima_client = None
        handler.ima_config = {}
        handler.llm_client = Mock()
        handler.cb_client = None
        handler.model_router = Mock()
        handler.model_router.chat.return_value = (None, None)
        handler.llm_client.chat.side_effect = DeepSeekAPIError(
            "upstream secret token must not reach the user",
            status_code=429,
        )
        handler.resolution_engine = Mock()
        handler.knowledge_loop_enabled = True
        handler.knowledge_loop = Mock()
        handler.conversation_states = Mock()
        handler.ticket_enabled = True
        handler.reply_text = Mock()
        handler._set_pending_feedback = Mock()
        handler._build_system_prompt = Mock(return_value="system prompt")
        handler._user_session_key = Mock(return_value="session:user-1")
        handler._get_ai_engine_override = Mock(return_value=None)

        info = {
            "content": "VPN 连接失败",
            "conv_id": "conversation-1",
            "msg_id": "message-1",
            "sender_nick": "Test User",
            "sender_id": "user-1",
            "conv_label": "单聊",
            "conv_title": "",
        }

        with (
            patch("handlers.chat_handler.bot_status.set_last_engine") as set_engine,
            patch("handlers.chat_handler.msg_logger.record") as record_message,
            patch(
                "handlers.chat_handler.uuid.uuid4",
                return_value=SimpleNamespace(hex="a1b2c3d4ffff"),
            ),
        ):
            handler._handle_ai_chat(
                object(),
                info,
                active_expert=None,
                forced_query=None,
                target_kbs=[],
            )

        handler.resolution_engine.record_answer.assert_not_called()
        handler.knowledge_loop.record_answer.assert_not_called()
        handler._set_pending_feedback.assert_not_called()
        handler.conversation_states.pop_feedback.assert_called_once_with(
            "session:user-1"
        )
        set_engine.assert_called_once_with("none", None)
        record_message.assert_called_once()

        self.assertEqual(handler.reply_text.call_count, 2)
        failure_reply = handler.reply_text.call_args_list[-1].args[0]
        self.assertIn("当前繁忙", failure_reply)
        self.assertIn("错误编号：A1B2C3D4", failure_reply)
        self.assertNotIn("upstream secret token", failure_reply)

    def test_ticket_disabled_answer_does_not_offer_impossible_ticket_upgrade(self):
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.logger = Mock()
        handler.ima_client = None
        handler.ima_config = {}
        handler.llm_client = Mock()
        handler.llm_client.chat.return_value = "请先重新连接 VPN。"
        handler.cb_client = None
        handler.model_router = Mock()
        handler.model_router.chat.return_value = (None, None)
        handler.resolution_engine = Mock()
        handler.resolution_engine.record_answer.side_effect = (
            lambda _session, _query, answer: answer
        )
        handler.resolution_engine.has_session.return_value = False
        handler.knowledge_loop_enabled = False
        handler.conversation_states = Mock()
        handler.ticket_enabled = False
        handler.reply_text = Mock()
        handler._set_pending_feedback = Mock()
        handler._build_system_prompt = Mock(return_value="system prompt")
        handler._user_session_key = Mock(return_value="session:user-1")
        handler._get_ai_engine_override = Mock(return_value=None)

        info = {
            "content": "VPN 如何重新连接",
            "conv_id": "conversation-1",
            "msg_id": "message-2",
            "sender_nick": "Test User",
            "sender_id": "user-1",
            "conv_label": "单聊",
            "conv_title": "",
        }

        with (
            patch("handlers.chat_handler.bot_status.set_last_engine"),
            patch("handlers.chat_handler.msg_logger.record"),
        ):
            handler._handle_ai_chat(object(), info, target_kbs=[])

        answer_reply = handler.reply_text.call_args_list[-1].args[0]
        self.assertNotIn("升级为工单", answer_reply)
        handler._set_pending_feedback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
