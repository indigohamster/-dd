import os
import sys
import unittest
from types import SimpleNamespace


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.chat_handler import BotMessageHandler
from modules.ticket.ticket_formatter import TicketFormatter
from modules.ticket.ticket_parser import ParsedTicketAction, TicketIntent, TicketParser


TICKET_ID = "TKT-20260720-001"


class TicketParserV215Tests(unittest.TestCase):
    def test_service_desk_and_catalog_intents(self):
        self.assertEqual(TicketParser.parse("帮助").intent, TicketIntent.HELP)
        self.assertEqual(TicketParser.parse("服务目录").intent, TicketIntent.SERVICE_CATALOG)
        self.assertEqual(TicketParser.parse("我要申请").intent, TicketIntent.SERVICE_CATALOG)
        parsed = TicketParser.parse("申请服务 account 开通财务系统只读权限")
        self.assertEqual(parsed.intent, TicketIntent.SERVICE_APPLY)
        self.assertEqual(parsed.extra, {
            "service_code": "ACCOUNT", "description": "开通财务系统只读权限",
        })

    def test_ticket_id_is_canonicalized(self):
        for value in ("tkt-20260720-001", "tkt 20260720 001", "#TKT20260720001"):
            self.assertEqual(TicketParser.parse(value).ticket_id, TICKET_ID)
        large = TicketParser.parse("工单 TKT-20260720-100000")
        self.assertEqual(large.intent, TicketIntent.VIEW)
        self.assertEqual(large.ticket_id, "TKT-20260720-100000")

    def test_negated_create_and_archive_are_safe(self):
        self.assertEqual(
            TicketParser.parse("我不想创建工单，只是咨询").intent,
            TicketIntent.NONE,
        )
        self.assertEqual(
            TicketParser.parse(f"不需要归档 {TICKET_ID}").intent,
            TicketIntent.INVALID,
        )

    def test_reports_require_command_boundaries(self):
        self.assertEqual(TicketParser.parse("日报").intent, TicketIntent.DAILY)
        self.assertEqual(TicketParser.parse("日报 2026-07-20").extra, "2026-07-20")
        self.assertEqual(TicketParser.parse("日报怎么导出").intent, TicketIntent.NONE)
        self.assertEqual(TicketParser.parse("工单统计 本周").extra, "week")
        self.assertEqual(TicketParser.parse("工单统计怎么导出").intent, TicketIntent.NONE)

    def test_confirmation_score_comment_and_reopen_reason(self):
        parsed = TicketParser.parse(f"确认恢复 {TICKET_ID} 评分5 很满意")
        self.assertEqual(parsed.intent, TicketIntent.CONFIRM_RESOLUTION)
        self.assertEqual(parsed.extra, {"score": 5, "comment": "很满意"})
        reopened = TicketParser.parse(f"仍未解决 {TICKET_ID} VPN 仍报 691")
        self.assertEqual(reopened.intent, TicketIntent.REOPEN)
        self.assertIn("691", reopened.extra)

    def test_employee_ticket_actions(self):
        cases = {
            f"补充 {TICKET_ID} 完整报错已上传": TicketIntent.ADD_COMMENT,
            f"催办 {TICKET_ID} 已影响业务": TicketIntent.URGE,
            f"撤回工单 {TICKET_ID} 已自行解决": TicketIntent.WITHDRAW,
        }
        for content, intent in cases.items():
            with self.subTest(content=content):
                parsed = TicketParser.parse(content)
                self.assertEqual(parsed.intent, intent)
                self.assertTrue(parsed.extra)


class TicketFormatterV215Tests(unittest.TestCase):
    def test_help_and_catalog_are_actionable(self):
        _, help_body = TicketFormatter.format_help()
        self.assertIn("服务目录", help_body)
        self.assertIn("确认恢复", help_body)
        _, body = TicketFormatter.format_service_catalog([
            {"code": "ACCOUNT", "name": "账号与权限", "description": "权限申请",
             "response_hours": 4, "resolution_hours": 24},
        ])
        self.assertIn("申请服务 CODE", body)
        self.assertIn("响应目标 4 小时", body)

    def test_resolved_notification_requests_confirmation_and_shows_sla(self):
        ticket = {
            "ticket_id": TICKET_ID, "title": "VPN 无法连接", "status": "resolved",
            "resolution": "重建配置", "response_due_at": "2026-07-20 12:00:00",
            "resolution_due_at": "2026-07-21 08:00:00",
        }
        _, body = TicketFormatter.format_user_status_notification(ticket, "resolved", "张工")
        self.assertIn("等待你确认", body)
        self.assertIn("确认恢复", body)
        self.assertIn("重开", body)
        self.assertIn("响应目标", body)
        self.assertIn("解决目标", body)


class FakeSettingsStore:
    def load(self):
        return {"service_catalog": [
            {"code": "ACCOUNT", "name": "账号与权限", "description": "账号开通",
             "category": "软件问题", "priority": "medium", "response_hours": 4,
             "resolution_hours": 24, "assignment_group": "IT-SERVICE",
             "request_type": "service_request", "enabled": True},
            {"code": "DEVICE", "name": "电脑与外设", "description": "设备故障",
             "category": "设备问题", "priority": "medium", "response_hours": 4,
             "resolution_hours": 24, "assignment_group": "IT-DESK",
             "request_type": "incident", "enabled": True},
            {"code": "OFF", "name": "停用服务", "enabled": False},
        ]}


class FakeTicketManager:
    def __init__(self):
        self.created = None
        self.ticket = None
        self.assign_calls = []
        self.assign_result = True

    def create_ticket(self, **kwargs):
        self.created = kwargs
        self.ticket = {
            **kwargs, "ticket_id": TICKET_ID, "status": "open",
            "created_at": "2026-07-20 10:00:00",
            "response_due_at": "2026-07-20 14:00:00",
            "resolution_due_at": "2026-07-21 10:00:00",
        }
        return self.ticket

    def get_ticket(self, ticket_id):
        return self.ticket

    def assign_ticket(self, ticket_id, assignee_id, assignee_nick,
                      operator_id, operator_nick):
        self.assign_calls.append({
            "ticket_id": ticket_id,
            "assignee_id": assignee_id,
            "assignee_nick": assignee_nick,
            "operator_id": operator_id,
            "operator_nick": operator_nick,
        })
        if not self.assign_result:
            return None
        self.ticket = {
            **self.ticket, "status": "assigned", "assignee_id": assignee_id,
            "assignee_nick": assignee_nick,
        }
        return self.ticket


class FakeAdminTicketManager:
    def __init__(self, admins=None):
        self.admins = [dict(item) for item in (admins or [])]
        self.added = []
        self.auto_add_called = False

    def is_admin(self, user_id):
        return any(item.get("user_id") == user_id for item in self.admins)

    def list_admins(self):
        return [dict(item) for item in self.admins]

    def add_admin(self, user_id, user_nick, operator_id="", staff_id=""):
        item = {
            "user_id": user_id, "user_nick": user_nick,
            "staff_id": staff_id,
        }
        self.added.append(item)
        self.admins = [row for row in self.admins if row.get("user_id") != user_id]
        self.admins.append(item)

    def add_admin_from_dingtalk(self, *args, **kwargs):
        self.auto_add_called = True


def make_handler():
    handler = object.__new__(BotMessageHandler)
    handler.logger = SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
    handler.ticket_enabled = True
    handler.ticket_mgr = FakeTicketManager()
    handler.admin_settings_store = FakeSettingsStore()
    handler.replies = []
    handler.reply_text = lambda body, msg: handler.replies.append(("text", "", body))
    handler.reply_markdown = lambda title, body, msg: handler.replies.append(("markdown", title, body))
    handler._notify_admin_new_ticket = lambda ticket, info: None
    handler.knowledge_loop_enabled = False
    return handler


class TicketHandlerV215Tests(unittest.TestCase):
    def test_service_application_passes_structured_contract(self):
        handler = make_handler()
        info = {"conv_id": "c", "conv_title": "", "sender_id": "u1",
                "sender_nick": "员工", "staff_id": "s1"}
        parsed = TicketParser.parse("申请服务 ACCOUNT 开通财务只读权限")
        self.assertTrue(handler._ticket_apply_service(object(), info, parsed))
        created = handler.ticket_mgr.created
        self.assertEqual(created["request_type"], "service_request")
        self.assertEqual(created["service_code"], "ACCOUNT")
        self.assertEqual(created["assignment_group"], "IT-SERVICE")
        self.assertEqual(created["response_hours"], 4)
        handler._ticket_apply_service(
            object(), info, TicketParser.parse("申请服务 DEVICE 打印机无法出纸"),
        )
        self.assertEqual(handler.ticket_mgr.created["request_type"], "incident")

    def test_service_application_auto_assigns_catalog_owner(self):
        handler = make_handler()
        handler.admin_settings_store = SimpleNamespace(load=lambda: {
            "service_catalog": [{
                "code": "VPN", "name": "VPN 服务", "enabled": True,
                "owner_id": "staff-net", "owner_name": "网络管理员",
                "response_hours": 2, "resolution_hours": 8,
            }],
        })
        info = {"conv_id": "c", "conv_title": "", "sender_id": "u1",
                "sender_nick": "员工", "staff_id": "s1"}

        handler._ticket_apply_service(
            object(), info, TicketParser.parse("申请服务 VPN 无法连接"),
        )

        self.assertEqual(len(handler.ticket_mgr.assign_calls), 1)
        self.assertEqual(handler.ticket_mgr.assign_calls[0]["assignee_id"], "staff-net")
        self.assertEqual(handler.ticket_mgr.ticket["status"], "assigned")
        self.assertNotIn("自动分派", handler.replies[-1][2])

    def test_service_application_reports_auto_assignment_failure(self):
        handler = make_handler()
        handler.ticket_mgr.assign_result = False
        handler.admin_settings_store = SimpleNamespace(load=lambda: {
            "service_catalog": [{
                "code": "LEGACY", "name": "历史服务", "enabled": True,
                "owner_name": "旧版负责人",
                "response_hours": 4, "resolution_hours": 24,
            }],
        })
        info = {"conv_id": "c", "conv_title": "", "sender_id": "u1",
                "sender_nick": "员工", "staff_id": "s1"}

        handler._ticket_apply_service(
            object(), info, TicketParser.parse("申请服务 LEGACY 需要协助"),
        )

        self.assertEqual(handler.ticket_mgr.assign_calls[0]["assignee_id"], "旧版负责人")
        self.assertEqual(handler.ticket_mgr.ticket["status"], "open")
        self.assertIn("自动分派", handler.replies[-1][2])
        self.assertIn("失败", handler.replies[-1][2])

    def test_admin_identity_never_bootstraps_from_empty_table_or_nickname(self):
        handler = make_handler()
        handler.ticket_mgr = FakeAdminTicketManager()
        handler.ticket_admins = []
        handler.dtalk_api = None
        self.assertFalse(handler._is_ticket_admin("employee-1", "普通员工", "staff-1"))
        self.assertFalse(handler.ticket_mgr.auto_add_called)
        self.assertEqual(handler.ticket_mgr.added, [])

        handler.ticket_admins = [{"user_nick": "同名管理员"}]
        self.assertFalse(handler._is_ticket_admin("attacker", "同名管理员", "staff-x"))
        self.assertEqual(handler.ticket_mgr.added, [])

    def test_admin_identity_accepts_only_matching_stable_ids(self):
        cases = (
            ([{"user_id": "configured-open", "user_nick": "配置管理员"}],
             "configured-open", "任意显示名", "staff-new"),
            ([{"staff_id": "staff-config", "user_nick": "配置管理员"}],
             "new-open", "任意显示名", "staff-config"),
        )
        for config_admins, user_id, user_nick, staff_id in cases:
            with self.subTest(config_admins=config_admins):
                handler = make_handler()
                handler.ticket_mgr = FakeAdminTicketManager()
                handler.ticket_admins = config_admins
                handler.dtalk_api = None
                self.assertTrue(
                    handler._is_ticket_admin(user_id, user_nick, staff_id),
                )
                self.assertTrue(handler.ticket_mgr.is_admin(user_id))

        handler = make_handler()
        handler.ticket_mgr = FakeAdminTicketManager([{
            "user_id": "old-open", "user_nick": "已有管理员",
            "staff_id": "staff-existing",
        }])
        handler.ticket_admins = []
        handler.dtalk_api = None
        self.assertTrue(
            handler._is_ticket_admin("new-open", "已有管理员", "staff-existing"),
        )
        self.assertTrue(handler.ticket_mgr.is_admin("new-open"))

    def test_people_admin_role_requires_exact_staff_id(self):
        for role in ("admin", "ADMIN", "管理员", "运维管理员", "工单管理员"):
            with self.subTest(role=role):
                handler = make_handler()
                handler.ticket_mgr = FakeAdminTicketManager()
                handler.ticket_admins = []
                handler.admin_settings_store = SimpleNamespace(load=lambda value=role: {
                    "people": [{
                        "name": "值班员", "staff_id": "staff-admin", "role": value,
                    }],
                })
                self.assertTrue(
                    handler._is_ticket_admin("open-admin", "其他显示名", "staff-admin"),
                )
                self.assertTrue(handler.ticket_mgr.is_admin("open-admin"))

        handler = make_handler()
        handler.ticket_mgr = FakeAdminTicketManager()
        handler.ticket_admins = []
        handler.admin_settings_store = SimpleNamespace(load=lambda: {
            "people": [{
                "name": "同名管理员", "staff_id": "real-staff", "role": "工单管理员",
            }],
        })
        self.assertFalse(
            handler._is_ticket_admin("attacker", "同名管理员", "other-staff"),
        )
        self.assertEqual(handler.ticket_mgr.added, [])

    def test_archive_requires_valid_confirmed_closed_ticket(self):
        rejected = (
            {"status": "open", "user_confirmation": ""},
            {"status": "resolved", "user_confirmation": ""},
            {"status": "closed", "user_confirmation": ""},
            {"status": "closed", "user_confirmation": "withdrawn"},
            {"status": "closed", "user_confirmation": "confirmed", "is_invalid": 1},
        )
        for state in rejected:
            with self.subTest(state=state):
                handler = make_handler()
                handler.ticket_mgr.ticket = {
                    "ticket_id": TICKET_ID, "title": "VPN 故障", "category": "网络",
                    "resolution": "重建配置", "is_experience": 0, "is_invalid": 0,
                    **state,
                }
                archive_calls = []
                handler.ticket_mgr.archive_as_experience = (
                    lambda *args, **kwargs: archive_calls.append(args)
                )
                handler._ticket_archive(
                    object(), {"sender_id": "admin", "sender_nick": "管理员"},
                    ParsedTicketAction(TicketIntent.ARCHIVE, TICKET_ID),
                )
                self.assertEqual(archive_calls, [])
                self.assertIn("不能", handler.replies[-1][2])

        handler = make_handler()
        handler.ticket_mgr.ticket = {
            "ticket_id": TICKET_ID, "title": "VPN 故障", "category": "网络",
            "resolution": "重建配置", "status": "closed",
            "user_confirmation": "confirmed", "is_experience": 0, "is_invalid": 0,
        }
        calls = []
        handler.ticket_mgr.archive_as_experience = lambda *args: (
            calls.append(args) or {**handler.ticket_mgr.ticket, "is_experience": 1}
        )
        handler._ticket_archive(
            object(), {"sender_id": "admin", "sender_nick": "管理员"},
            ParsedTicketAction(TicketIntent.ARCHIVE, TICKET_ID),
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("已归档", handler.replies[-1][1])

    def test_non_admin_cannot_list_pending(self):
        handler = make_handler()
        handled = handler._handle_ticket(
            object(), {"sender_id": "u1", "sender_nick": "员工"},
            ParsedTicketAction(TicketIntent.LIST_PENDING), False,
        )
        self.assertTrue(handled)
        self.assertIn("仅管理员", handler.replies[-1][2])

    def test_close_failure_is_not_reported_as_success(self):
        handler = make_handler()
        handler.ticket_mgr.ticket = {
            "ticket_id": TICKET_ID, "creator_id": "u1", "status": "closed",
        }
        handler.ticket_mgr.close_ticket = lambda *args, **kwargs: None
        handler._ticket_close(
            object(), {"sender_id": "u1", "sender_nick": "员工"},
            ParsedTicketAction(TicketIntent.CLOSE, TICKET_ID), "u1", True,
        )
        self.assertIn("无法执行关闭", handler.replies[-1][2])

    def test_creator_close_resolved_uses_confirmation_contract(self):
        handler = make_handler()
        handler.ticket_mgr.ticket = {
            "ticket_id": TICKET_ID, "creator_id": "u1", "creator_nick": "员工",
            "title": "VPN 无法连接", "status": "resolved",
        }
        calls = []
        handler.ticket_mgr.confirm_resolution = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {**handler.ticket_mgr.ticket, "status": "closed"}
        )
        handler._ticket_close(
            object(), {"sender_id": "u1", "sender_nick": "员工"},
            ParsedTicketAction(TicketIntent.CLOSE, TICKET_ID), "u1", False,
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("工单已关闭", handler.replies[-1][2])

    def test_auto_escalation_preserves_request_type(self):
        for classification, expected in (
                ("service_request", "service_request"),
                ("major_incident", "major_incident"),
                ("security", "incident")):
            with self.subTest(classification=classification):
                handler = make_handler()
                handler.resolution_engine = SimpleNamespace(
                    classify=lambda text, value=classification: value,
                )
                captured = {}
                handler._handle_ticket = lambda msg, info, parsed, is_admin: captured.update(info) or True
                info = {"content": "问题描述", "sender_id": "u1", "sender_nick": "员工"}
                handler._auto_escalate_issue(
                    object(), info, {"reason": "需要人工", "summary": "问题描述"},
                )
                self.assertEqual(captured["ticket_request_type"], expected)


if __name__ == "__main__":
    unittest.main()
