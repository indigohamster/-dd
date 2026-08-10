import unittest
from unittest.mock import Mock

from handlers.chat_handler import BotMessageHandler
from modules.conversation_state import ConversationStateStore
from modules.problem_resolution import ProblemResolutionEngine
from modules.ticket import TriageEngine


class ShadowRouteParitySmokeTests(unittest.TestCase):
    """Prove that the observer does not alter legacy routing or replies."""

    @staticmethod
    def info(content):
        return {
            "conv_id": "smoke-conversation",
            "sender_id": "smoke-user",
            "staff_id": "smoke-user",
            "sender_nick": "Smoke User",
            "content": content,
            "conv_title": "Smoke",
            "msg_id": "smoke-message",
        }

    def handler(self, shadow_enabled, *, resolution=True):
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.ticket_enabled = True
        handler.triage_engine = TriageEngine()
        handler.resolution_enabled = resolution
        handler.resolution_engine = ProblemResolutionEngine()
        handler.ima_config = {"expert_triggers": [], "trigger_keywords": [], "default_knowledge_bases": []}
        handler.conversation_states = ConversationStateStore()
        handler.task_queue = Mock()
        handler.task_queue.get_task.return_value = None
        handler.task_queue.list_tasks.return_value = []
        handler.llm_client = object()
        handler.cb_client = None
        handler.logger = Mock()
        handler.problem_understanding = Mock()
        handler.problem_understanding.enabled = shadow_enabled
        handler.problem_understanding.submit_shadow.return_value = True
        handler.business_trace = []
        handler.reply_text = lambda text, _msg: handler.business_trace.append(("reply", text))
        handler._handle_ai_chat = lambda _msg, info, _expert: handler.business_trace.append(
            ("ai", info["content"])
        )
        handler._handle_ticket = lambda _msg, _info, parsed, _admin: handler.business_trace.append(
            ("ticket", getattr(parsed.intent, "value", parsed.intent))
        ) or True
        handler._is_ticket_admin = lambda *args, **kwargs: False
        handler._record_issue_question = lambda _info: handler.business_trace.append(("issue", "record"))
        handler._get_expert_state = lambda _key: None
        handler._resolve_target_kbs = lambda _expert=None: []
        handler._record_knowledge_outcome = lambda *args, **kwargs: None
        handler._auto_escalate_issue = lambda _msg, _info, result: handler.business_trace.append(
            ("handoff", result.get("reason", ""))
        )
        return handler

    def run_case(self, content, *, prepare=None, resolution=True):
        traces = []
        shadow_calls = []
        for enabled in (False, True):
            handler = self.handler(enabled, resolution=resolution)
            info = self.info(content)
            if prepare:
                prepare(handler, info)
            handler._dispatch_text_message(object(), info)
            traces.append(handler.business_trace)
            shadow_calls.append(handler.problem_understanding.submit_shadow.call_count)
        self.assertEqual(traces[0], traces[1])
        return traces[1], shadow_calls[1]

    def test_eight_representative_routes_keep_legacy_behavior(self):
        cases = [
            ("普通知识咨询", "如何修改个人密码", None, False, True),
            ("故障描述", "我的打印机突然无法打印", None, True, True),
            ("信息不足故障", "打印机坏了", None, True, True),
            (
                "上一句补充", "就是提交的时候",
                lambda h, i: h.resolution_engine.begin(h._user_session_key(i), "打印机坏了"),
                True, True,
            ),
            (
                "明确新问题", "换个问题，邮箱怎么设置",
                lambda h, i: h.resolution_engine.begin(h._user_session_key(i), "打印机坏了"),
                True, True,
            ),
            ("能力咨询", "你能处理哪些问题", None, False, True),
            (
                "请求人工", "转人工",
                lambda h, i: h.resolution_engine.begin(h._user_session_key(i), "打印机坏了"),
                True, True,
            ),
            ("明确工单操作", "查看工单 TKT-20260810-001", None, False, False),
        ]
        for label, content, prepare, resolution, expect_shadow in cases:
            with self.subTest(label=label):
                trace, shadow_calls = self.run_case(
                    content, prepare=prepare, resolution=resolution,
                )
                self.assertTrue(trace, label)
                self.assertEqual(shadow_calls > 0, expect_shadow, label)

    def test_empty_message_never_reaches_shadow(self):
        trace, shadow_calls = self.run_case("   ", resolution=False)
        self.assertEqual(trace, [])
        self.assertEqual(shadow_calls, 0)


if __name__ == "__main__":
    unittest.main()
