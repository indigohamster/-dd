import os
import sys
import tempfile
import threading
import time
import unittest
import yaml
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.chat_handler import BotMessageHandler
from dingtalk_stream import ChatbotMessage
from modules.ticket.triage import TriageEngine
from modules.ticket.ticket_parser import TicketIntent, TicketParser
from utils.scheduled_tasks import DailyOpsSummaryScheduler, TicketFollowupScheduler
from utils.console import _handle_api_config
from modules.ticket.ticket_manager import (
    SCHEMA_VERSION,
    STATUS_CLOSED,
    STATUS_IN_PROGRESS,
    TicketManager,
)


class AttachmentSecurityTests(unittest.TestCase):
    def test_attachment_name_removes_parent_paths(self):
        self.assertEqual(
            BotMessageHandler._safe_attachment_name(r"..\..\secret.txt"),
            "secret.txt",
        )

    def test_attachment_name_removes_unsafe_characters(self):
        name = BotMessageHandler._safe_attachment_name('bad<name>:?.txt')
        self.assertNotRegex(name, r'[<>:"/\\|?*]')

    def test_real_sdk_audio_payload_is_extracted(self):
        payload = {
            "msgtype": "audio", "msgId": "m1", "conversationType": "1",
            "conversationId": "c1", "senderId": "u1", "senderNick": "User",
            "content": {
                "downloadCode": "voice-code", "duration": 2300,
                "format": "amr", "recognition": "打印机不能打印",
            },
        }
        msg = ChatbotMessage.from_dict(payload)
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.logger = type("Logger", (), {"debug": lambda *args: None})()
        info = handler._extract_message_info(msg)
        self.assertEqual(info["msgtype"], "audio")
        self.assertEqual(info["file_info"]["type"], "voice")
        self.assertEqual(info["file_info"]["download_code"], "voice-code")
        self.assertEqual(info["file_info"]["recognition"], "打印机不能打印")

    def test_real_sdk_picture_payload_is_extracted(self):
        msg = ChatbotMessage.from_dict({
            "msgtype": "picture", "msgId": "m2", "conversationType": "1",
            "content": {"downloadCode": "image-code"},
        })
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.logger = type("Logger", (), {"debug": lambda *args: None})()
        info = handler._extract_message_info(msg)
        self.assertEqual(info["msgtype"], "picture")
        self.assertEqual(info["file_info"]["download_code"], "image-code")


class ConsoleApiConfigTests(unittest.TestCase):
    def test_console_updates_api_config_and_preserves_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "# keep this comment\n"
                    "llm:\n"
                    "  enabled: false\n"
                    "  api_key: \"old-key\"\n"
                    "  model: \"old-model\"\n"
                    "  base_url: \"https://old.example/v1\"\n"
                    "other:\n"
                    "  enabled: true\n"
                )
            _handle_api_config("api-config on", path)
            _handle_api_config("api-config url https://llm.example/v1", path)
            _handle_api_config("api-config path custom/chat", path)
            _handle_api_config("api-config model ops-model", path)
            result = _handle_api_config("api-config key super-secret-key", path)
            self.assertNotIn("super-secret-key", result)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            config = yaml.safe_load(text)
            self.assertIn("# keep this comment", text)
            self.assertTrue(config["llm"]["enabled"])
            self.assertEqual(config["llm"]["base_url"], "https://llm.example/v1")
            self.assertEqual(config["llm"]["chat_completions_path"], "/custom/chat")
            self.assertEqual(config["llm"]["model"], "ops-model")
            self.assertEqual(config["llm"]["api_key"], "super-secret-key")


class EngineIsolationTests(unittest.TestCase):
    def setUp(self):
        self.handler = BotMessageHandler.__new__(BotMessageHandler)
        self.handler._ai_engine_overrides = {}
        self.handler.__engine_lock__ = threading.Lock()
        self.handler._ENGINE_OVERRIDE_TTL = 1800

    def test_overrides_are_conversation_scoped(self):
        self.handler._ai_engine_overrides["a"] = {
            "engine": "codebuddy", "timestamp": time.time()
        }
        self.assertEqual(self.handler._get_ai_engine_override("a"), "codebuddy")
        self.assertIsNone(self.handler._get_ai_engine_override("b"))

    def test_expired_override_is_removed(self):
        self.handler._ai_engine_overrides["a"] = {
            "engine": "codebuddy", "timestamp": 0
        }
        self.assertIsNone(self.handler._get_ai_engine_override("a"))


class TicketStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = TicketManager(os.path.join(self.tmp.name, "tickets.db"))
        self.ticket = self.manager.create_ticket(
            conv_id="conv",
            conv_title="test",
            creator_id="user",
            creator_nick="User",
            title="test ticket",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_ticket_does_not_create_log(self):
        self.assertIsNone(
            self.manager.update_status("TKT-MISSING", STATUS_IN_PROGRESS, "u", "U")
        )
        self.assertEqual(self.manager.get_ticket_logs("TKT-MISSING"), [])
        self.assertIsNone(self.manager.update_priority("TKT-MISSING", "high"))
        self.assertIsNone(self.manager.archive_as_experience("TKT-MISSING"))
        self.assertIsNone(self.manager.mark_as_invalid("TKT-MISSING"))
        self.assertEqual(self.manager.get_ticket_logs("TKT-MISSING"), [])

    def test_closed_ticket_cannot_be_reopened(self):
        ticket_id = self.ticket["ticket_id"]
        self.assertIsNotNone(self.manager.close_ticket(ticket_id, "user", "User"))
        self.assertIsNone(
            self.manager.set_in_progress(ticket_id, "user", "User")
        )
        self.assertEqual(self.manager.get_ticket(ticket_id)["status"], STATUS_CLOSED)

    def test_closed_ticket_cannot_be_assigned(self):
        ticket_id = self.ticket["ticket_id"]
        self.manager.close_ticket(ticket_id, "user", "User")
        self.assertIsNone(
            self.manager.assign_ticket(ticket_id, "admin", "Admin", "admin", "Admin")
        )


class DatabaseSafetyTests(unittest.TestCase):
    def test_schema_integrity_and_online_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tickets.db")
            backup_dir = os.path.join(tmp, "backups")
            manager = TicketManager(
                db_path,
                backup_enabled=True,
                backup_dir=backup_dir,
                integrity_check=True,
            )
            self.assertEqual(manager.get_schema_version(), SCHEMA_VERSION)
            self.assertTrue(manager.check_integrity())
            backup_path = manager.backup_database(force=True)
            self.assertTrue(os.path.isfile(backup_path))

            restored = TicketManager(backup_path, integrity_check=True)
            self.assertEqual(restored.get_schema_version(), SCHEMA_VERSION)

    def test_old_backups_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tickets.db")
            backup_dir = os.path.join(tmp, "backups")
            manager = TicketManager(
                db_path,
                backup_enabled=False,
                backup_dir=backup_dir,
                backup_retention_days=1,
            )
            os.makedirs(backup_dir, exist_ok=True)
            old_backup = os.path.join(backup_dir, "tickets-20000101.db")
            with open(old_backup, "wb") as handle:
                handle.write(b"old")
            os.utime(old_backup, (1, 1))
            manager._cleanup_old_backups()
            self.assertFalse(os.path.exists(old_backup))


class TriageTests(unittest.TestCase):
    def test_three_step_triage_builds_complete_description(self):
        engine = TriageEngine(min_description_length=30)
        self.assertTrue(engine.needs_triage("打印机坏了"))
        first = engine.start("conv", "打印机坏了", "设备问题")
        self.assertIn("1/3", first)
        self.assertEqual(engine.consume("conv", "HP 1020，财务室，报错脱机")["status"], "question")
        self.assertEqual(engine.consume("conv", "今天开始，重启无效")["status"], "question")
        result = engine.consume("conv", "财务部三个人都不能打印，影响开票")
        self.assertEqual(result["status"], "completed")
        self.assertIn("影响开票", result["description"])
        self.assertFalse(engine.has_session("conv"))

    def test_triage_can_be_cancelled(self):
        engine = TriageEngine()
        engine.start("conv", "VPN 不行", "设备问题")
        self.assertEqual(engine.consume("conv", "取消报修")["status"], "cancelled")


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = TicketManager(os.path.join(self.tmp.name, "tickets.db"), followup_delay_hours=1)

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, title, user="u1"):
        return self.manager.create_ticket("conv", "IT", user, user, title, category="设备问题")

    def test_related_active_ticket_detection(self):
        first = self._create("财务室打印机显示脱机无法打印", "u1")
        second = self._create("财务打印机脱机不能打印", "u2")
        related = self.manager.find_related_active_tickets(
            second["title"], second["category"], exclude_ticket_id=second["ticket_id"]
        )
        self.assertEqual(related[0]["ticket_id"], first["ticket_id"])

    def test_resolved_ticket_enters_followup_and_can_reopen(self):
        ticket = self._create("VPN 无法连接")
        resolved = self.manager.resolve_ticket(ticket["ticket_id"], "重建 VPN 配置", "admin", "Admin")
        self.assertIsNotNone(resolved.get("followup_due_at"))
        conn = self.manager._connect()
        try:
            past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE tickets SET followup_due_at=? WHERE ticket_id=?", (past, ticket["ticket_id"]))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.manager.get_due_followups()[0]["ticket_id"], ticket["ticket_id"])
        self.assertTrue(self.manager.mark_followup_sent(ticket["ticket_id"]))
        reopened = self.manager.reopen_ticket(ticket["ticket_id"], "u1", "u1")
        self.assertEqual(reopened["status"], STATUS_IN_PROGRESS)

    def test_parser_recognizes_reopen_before_resolve(self):
        parsed = TicketParser.parse("问题未解决，重开 TKT-20260715-001")
        self.assertEqual(parsed.intent, TicketIntent.REOPEN)

    def test_followup_and_daily_summary_send_without_network(self):
        class FakeAPI:
            def __init__(self):
                self.messages = []
            def send_markdown_to_user(self, staff_id, title, body):
                self.messages.append((staff_id, title, body))

        class FakeLogger:
            def info(self, *_): pass
            def warning(self, *_): pass
            def error(self, *_): pass

        api = FakeAPI()
        handler = type("Handler", (), {})()
        handler.ticket_mgr = self.manager
        handler.dingtalk_api = api
        handler.config = {"daily_ops_summary": {"enabled": True, "hour": 9}}
        self.manager.add_admin("admin", "Admin", staff_id="staff-admin")

        ticket = self.manager.create_ticket(
            "conv", "IT", "u1", "User", "打印机脱机", category="设备问题",
            creator_staff_id="staff-user",
        )
        self.manager.resolve_ticket(ticket["ticket_id"], "重新连接", "admin", "Admin")
        conn = self.manager._connect()
        try:
            conn.execute("UPDATE tickets SET followup_due_at='2000-01-01 00:00:00' WHERE ticket_id=?", (ticket["ticket_id"],))
            conn.commit()
        finally:
            conn.close()

        TicketFollowupScheduler(handler, FakeLogger())._check()
        self.assertTrue(any(message[0] == "staff-user" for message in api.messages))
        DailyOpsSummaryScheduler(handler, FakeLogger())._send(datetime.now().strftime("%Y-%m-%d"))
        self.assertTrue(any(message[0] == "staff-admin" for message in api.messages))


if __name__ == "__main__":
    unittest.main()
