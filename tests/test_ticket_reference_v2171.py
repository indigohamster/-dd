import unittest
from types import SimpleNamespace

from handlers.chat_handler import BotMessageHandler
from modules.conversation_state import ConversationStateStore
from modules.ticket.ticket_formatter import TicketFormatter
from modules.ticket.ticket_parser import ParsedTicketAction, TicketIntent, TicketParser
from modules.ticket.ticket_reference import TicketReferenceResolver


class TicketReferenceParserTests(unittest.TestCase):
    def test_explicit_ticket_id_commands_remain_compatible(self):
        ticket_id = "TKT-20260730-001"
        self.assertEqual(TicketParser.parse(ticket_id).intent, TicketIntent.VIEW)
        close = TicketParser.parse(f"关闭 {ticket_id}")
        self.assertEqual((close.intent, close.ticket_id), (TicketIntent.CLOSE, ticket_id))
        confirm = TicketParser.parse(f"确认解决 {ticket_id}")
        self.assertEqual(
            (confirm.intent, confirm.ticket_id),
            (TicketIntent.CONFIRM_RESOLUTION, ticket_id),
        )

    def test_my_pending_tickets_is_an_employee_list(self):
        parsed = TicketParser.parse("我的未处理工单")
        self.assertEqual(parsed.intent, TicketIntent.LIST_MINE)
        self.assertEqual(parsed.extra, {"status_filter": "pending"})

    def test_first_ticket_references(self):
        view = TicketParser.parse("查看第一个工单")
        close = TicketParser.parse("关闭第一个工单")
        follow_up = TicketParser.parse("第一个")
        self.assertEqual(view.intent, TicketIntent.VIEW)
        self.assertEqual(close.intent, TicketIntent.CLOSE)
        self.assertEqual(follow_up.intent, TicketIntent.VIEW)
        for parsed in (view, close, follow_up):
            self.assertEqual(
                parsed.extra["ticket_reference"],
                {"type": "ordinal", "value": 1},
            )

    def test_recent_ticket_reference(self):
        view = TicketParser.parse("最近一个工单")
        close = TicketParser.parse("关闭最近一个工单")
        self.assertEqual(view.intent, TicketIntent.VIEW)
        self.assertEqual(close.intent, TicketIntent.CLOSE)
        self.assertEqual(view.extra["ticket_reference"], {"type": "recent"})


class TicketReferenceResolverTests(unittest.TestCase):
    def setUp(self):
        self.tickets = [
            {"ticket_id": "TKT-20260730-002"},
            {"ticket_id": "TKT-20260730-001"},
        ]

    def test_ordinal_uses_supplied_display_order(self):
        parsed = TicketParser.parse("第一个")
        result = TicketReferenceResolver.resolve(parsed, self.tickets)
        self.assertTrue(result.resolved)
        self.assertEqual(result.ticket_id, "TKT-20260730-002")
        self.assertEqual(result.intent, TicketIntent.VIEW)

    def test_pending_confirmation_overrides_follow_up_intent(self):
        parsed = TicketParser.parse("第一个")
        result = TicketReferenceResolver.resolve(
            parsed, self.tickets, {"pending_intent": TicketIntent.CLOSE},
        )
        self.assertEqual(result.ticket_id, "TKT-20260730-002")
        self.assertEqual(result.intent, TicketIntent.CLOSE)

    def test_recent_uses_first_newest_candidate(self):
        parsed = TicketParser.parse("最近一个工单")
        result = TicketReferenceResolver.resolve(parsed, self.tickets)
        self.assertEqual(result.ticket_id, "TKT-20260730-002")

    def test_empty_candidates_do_not_resolve(self):
        parsed = TicketParser.parse("第一个")
        result = TicketReferenceResolver.resolve(parsed, [])
        self.assertFalse(result.resolved)
        self.assertEqual(result.reason, "out_of_range")


class TicketReferenceStateAndFormattingTests(unittest.TestCase):
    def test_ticket_context_is_isolated_and_expires(self):
        store = ConversationStateStore(ticket_context_ttl=1)
        store.set_ticket_context("user-a", {"ticket_ids": ["TKT-A"]})
        self.assertEqual(store.get_ticket_context("user-a")["ticket_ids"], ["TKT-A"])
        self.assertIsNone(store.get_ticket_context("user-b"))
        store._ticket_contexts["user-a"]["timestamp"] -= 2
        self.assertIsNone(store.get_ticket_context("user-a"))

    def test_ticket_list_has_stable_numbers(self):
        ticket = {
            "ticket_id": "TKT-20260730-001",
            "title": "邮箱无法登录",
            "category": "账号权限",
            "status": "open",
            "priority": "medium",
            "created_at": "2026-07-30 10:00:00",
            "updated_at": "2026-07-30 10:05:00",
            "assignee_nick": "",
        }
        _title, body = TicketFormatter.format_ticket_list(
            [ticket], "员工甲", "未处理",
        )
        self.assertIn("工单列表（未处理）", body)
        self.assertIn("1.", body)
        self.assertIn("查看第一个工单", body)

    def test_empty_pending_list_has_accurate_message(self):
        title, body = TicketFormatter.format_ticket_list([], "员工甲", "未处理")
        self.assertIn("暂无未处理工单", title)
        self.assertIn("当前没有未处理工单", body)


class TicketReferenceHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tickets = [
            {
                "ticket_id": "TKT-20260730-002", "creator_id": "user-1",
                "title": "邮箱无法登录", "category": "账号权限", "status": "open",
                "priority": "medium", "created_at": "2026-07-30 11:00:00",
                "updated_at": "2026-07-30 11:00:00", "assignee_nick": "",
            },
            {
                "ticket_id": "TKT-20260730-001", "creator_id": "user-1",
                "title": "打印机离线", "category": "办公设备", "status": "assigned",
                "priority": "medium", "created_at": "2026-07-30 10:00:00",
                "updated_at": "2026-07-30 10:00:00", "assignee_nick": "运维甲",
            },
        ]
        manager = SimpleNamespace(
            list_tickets_by_user=lambda _user_id, status=None, limit=20: list(self.tickets)[:limit],
            get_ticket=lambda ticket_id: next(
                (ticket for ticket in self.tickets if ticket["ticket_id"] == ticket_id), None,
            ),
        )
        self.replies = []
        self.handler = SimpleNamespace(
            ticket_mgr=manager,
            conversation_states=ConversationStateStore(),
            _user_session_key=lambda info: ConversationStateStore.user_key(info),
            reply_text=lambda text, _msg: self.replies.append(("text", text)),
            reply_markdown=lambda title, body, _msg: self.replies.append((title, body)),
        )
        self.info = {
            "conv_id": "conv-1", "sender_id": "user-1", "sender_nick": "员工甲",
        }

    def test_list_records_display_order_then_first_resolves(self):
        BotMessageHandler._ticket_list_mine(
            self.handler, object(), self.info,
            ParsedTicketAction(TicketIntent.LIST_MINE),
        )
        context = self.handler.conversation_states.get_ticket_context(
            ConversationStateStore.user_key(self.info),
        )
        self.assertEqual(
            context["ticket_ids"],
            ["TKT-20260730-002", "TKT-20260730-001"],
        )
        parsed, handled = BotMessageHandler._resolve_ticket_reference(
            self.handler, object(), self.info, TicketParser.parse("查看第一个工单"),
        )
        self.assertFalse(handled)
        self.assertEqual(parsed.ticket_id, "TKT-20260730-002")

    def test_close_multiple_records_confirmation_intent(self):
        handled = BotMessageHandler._ticket_close(
            self.handler, object(), self.info,
            ParsedTicketAction(TicketIntent.CLOSE), "user-1", False,
        )
        self.assertTrue(handled)
        context = self.handler.conversation_states.get_ticket_context(
            ConversationStateStore.user_key(self.info),
        )
        self.assertEqual(context["pending_intent"], TicketIntent.CLOSE)
        self.assertIn("第一个", self.replies[-1][1])

        parsed, consumed = BotMessageHandler._resolve_ticket_reference(
            self.handler, object(), self.info, TicketParser.parse("第一个"),
        )
        self.assertFalse(consumed)
        self.assertEqual(parsed.intent, TicketIntent.CLOSE)
        self.assertEqual(parsed.ticket_id, "TKT-20260730-002")

    def test_pending_list_filters_closed_items(self):
        self.tickets.append({
            "ticket_id": "TKT-20260729-001", "creator_id": "user-1",
            "title": "旧问题", "category": "其他", "status": "closed",
            "priority": "medium", "created_at": "2026-07-29 10:00:00",
            "updated_at": "2026-07-29 12:00:00", "assignee_nick": "运维甲",
        })
        BotMessageHandler._ticket_list_mine(
            self.handler, object(), self.info, TicketParser.parse("我的未处理工单"),
        )
        context = self.handler.conversation_states.get_ticket_context(
            ConversationStateStore.user_key(self.info),
        )
        self.assertNotIn("TKT-20260729-001", context["ticket_ids"])
        self.assertIn("未处理", self.replies[-1][1])


if __name__ == "__main__":
    unittest.main()
