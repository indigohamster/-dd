import os
import sys
import tempfile
import threading
import time
import unittest
import yaml
from unittest.mock import patch
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.chat_handler import BotMessageHandler
from dingtalk_stream import ChatbotMessage
from modules.ticket.triage import TriageEngine
from modules.problem_resolution import ProblemResolutionEngine
from modules.knowledge_loop import KnowledgeLoopStore
from modules.conversation_state import ConversationStateStore
from modules.vision_analysis import VisionAnalysisService
from modules.ops_dashboard import DailyOpsDashboard
from modules.ticket.ticket_parser import TicketIntent, TicketParser
from utils.scheduled_tasks import DailyOpsSummaryScheduler, TicketFollowupScheduler
from utils.console import _handle_api_config, _read_command_nonblocking
from utils.admin_server import create_admin_app
from utils.connector_executor import HTTPConnectorExecutor
from utils.spreadsheet_service import SpreadsheetService
from utils.config_history import ConfigHistory
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
    def test_console_uses_visible_native_input(self):
        with patch("builtins.input", return_value="  api-config  ") as mocked:
            self.assertEqual(_read_command_nonblocking(), "api-config")
            mocked.assert_called_once_with("\nbot> ")

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


class AdminUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.yaml")
        self.settings_path = os.path.join(self.tmp.name, "admin_settings.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "credentials:\n  client_id: test\n  client_secret: test\n"
                "llm:\n  enabled: true\n  api_key: secret-value\n"
                "  model: test-model\n  base_url: https://example.invalid\n"
                "  chat_completions_path: /v1/chat/completions\n"
                "  temperature: 0.3\n  max_tokens: 100\n  timeout: 5\n"
                f"ticket:\n  database_path: {os.path.join(self.tmp.name, 'tickets.db').replace(os.sep, '/')}\n"
                f"knowledge_loop:\n  database_path: {os.path.join(self.tmp.name, 'knowledge.db').replace(os.sep, '/')}\n"
                f"  export_dir: {os.path.join(self.tmp.name, 'exports').replace(os.sep, '/')}\n"
                "spreadsheet:\n  enabled: true\n  max_file_mb: 20\n  timeout: 90\n"
            )
        app = create_admin_app(self.config_path, self.settings_path)
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_llm_endpoint_masks_secret_and_updates_numeric_fields(self):
        result = self.client.get("/api/llm").get_json()
        self.assertNotIn("api_key", result)
        self.assertNotIn("secret-value", str(result))
        response = self.client.put("/api/llm", json={"temperature": 0.7, "max_tokens": 512})
        self.assertEqual(response.status_code, 200)
        with open(self.config_path, encoding="utf-8") as handle:
            llm = yaml.safe_load(handle)["llm"]
        self.assertEqual(llm["temperature"], 0.7)
        self.assertEqual(llm["max_tokens"], 512)

    def test_admin_home_is_served(self):
        response = self.client.get("/")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn("企业运维助手".encode("utf-8"), response.data)
        finally:
            response.close()

    def test_people_and_response_settings_are_persisted(self):
        people = [{"name": "张工", "staff_id": "001", "role": "运维人员"}]
        self.assertEqual(self.client.put("/api/settings/people", json=people).status_code, 200)
        style = {"assistant_name": "内部助手", "tone": "简洁"}
        self.assertEqual(self.client.put("/api/settings/response_style", json=style).status_code, 200)
        saved = self.client.get("/api/settings").get_json()
        self.assertEqual(saved["people"][0]["name"], "张工")
        self.assertEqual(saved["response_style"]["assistant_name"], "内部助手")

    def test_model_and_connector_secrets_are_never_returned(self):
        self.client.put("/api/settings/connectors", json=[{
            "id": "http-1", "name": "CMDB", "type": "http", "enabled": True,
            "url": "https://example.invalid", "auth_value": "connector-secret"
        }])
        self.client.put("/api/models", json={"models": [{
            "id": "m1", "name": "主模型", "model": "test", "base_url": "https://example.invalid",
            "api_key": "model-secret", "enabled": True
        }], "routing": {"general": "m1", "fallback": ""}})
        self.assertNotIn("connector-secret", self.client.get("/api/settings").get_data(as_text=True))
        self.assertNotIn("model-secret", self.client.get("/api/models").get_data(as_text=True))

    def test_ticket_workbench_lists_and_updates_ticket(self):
        manager = TicketManager(os.path.join(self.tmp.name, "tickets.db"), backup_enabled=False)
        ticket = manager.create_ticket("c", "IT", "u", "用户", "打印机无法打印")
        rows = self.client.get("/api/tickets?filter=all").get_json()
        self.assertEqual(rows[0]["ticket_id"], ticket["ticket_id"])
        response = self.client.patch(f"/api/tickets/{ticket['ticket_id']}", json={"action": "priority", "priority": "urgent"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(manager.get_ticket(ticket["ticket_id"])["priority"], "urgent")

    def test_config_history_can_rollback_settings(self):
        history = ConfigHistory(self.config_path, self.settings_path)
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write('{"people":[{"name":"旧值"}]}')
        meta = history.record("people", "保存旧值")
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write('{"people":[{"name":"新值"}]}')
        history.rollback(meta["version"])
        with open(self.settings_path, encoding="utf-8") as handle:
            self.assertIn("旧值", handle.read())

    def test_resolved_ticket_creates_knowledge_candidate_in_workbench(self):
        manager = TicketManager(os.path.join(self.tmp.name, "tickets.db"), backup_enabled=False)
        ticket = manager.create_ticket("c", "IT", "u", "用户", "VPN 无法连接", description="VPN 报错 691")
        manager.set_in_progress(ticket["ticket_id"], "admin", "管理员")
        response = self.client.patch(f"/api/tickets/{ticket['ticket_id']}", json={"action": "resolve", "resolution": "重新配置 VPN 凭据"})
        self.assertEqual(response.status_code, 200)
        candidates = self.client.get("/api/knowledge/candidates").get_json()
        self.assertEqual(candidates[0]["source_ticket_id"], ticket["ticket_id"])

    def test_daily_dashboard_endpoint(self):
        response = self.client.get("/api/dashboard/daily")
        self.assertEqual(response.status_code, 200)
        self.assertIn("metrics", response.get_json())

    def test_spreadsheet_settings_endpoint(self):
        response = self.client.put("/api/spreadsheet", json={"max_file_mb": 12, "timeout": 60})
        self.assertEqual(response.status_code, 200)
        settings = self.client.get("/api/spreadsheet").get_json()
        self.assertEqual(settings["max_file_mb"], 12)
        self.assertEqual(settings["timeout"], 60)


class ConnectorExecutorTests(unittest.TestCase):
    @patch("utils.connector_executor.requests.request")
    def test_http_template_and_response_mapping(self, mocked):
        response = mocked.return_value
        response.status_code = 200
        response.json.return_value = {"data": {"status": "online", "disk": 42}}
        response.raise_for_status.return_value = None
        result = HTTPConnectorExecutor.execute({
            "method": "POST", "url": "https://cmdb.example/{{server}}",
            "headers": '{"X-Source":"{{source}}"}',
            "body_template": '{"query":"{{input}}"}',
            "response_mapping": "状态=data.status\n磁盘=data.disk",
        }, {"server": "srv01", "source": "bot", "input": "health"})
        self.assertEqual(result["data"], {"状态": "online", "磁盘": 42})
        self.assertEqual(mocked.call_args.args[1], "https://cmdb.example/srv01")


class KnowledgeLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = KnowledgeLoopStore(
            os.path.join(self.tmp.name, "knowledge.db"),
            os.path.join(self.tmp.name, "exports"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_ima_source_effectiveness_is_tracked(self):
        event = self.store.record_answer("c", "VPN 怎么处理", "重新连接", [{"title": "VPN 指南", "source_kb": "运维库"}], "llm")
        self.store.record_outcome(event, True)
        metric = self.store.source_metrics()[0]
        self.assertEqual(metric["use_count"], 1)
        self.assertEqual(metric["success_count"], 1)
        self.assertEqual(metric["success_rate"], 100.0)

    def test_unresolved_answer_enters_problem_pool(self):
        event = self.store.record_answer("c", "新问题", "通用建议", [], "llm")
        self.store.record_outcome(event, False, "TKT-1")
        self.assertEqual(self.store.unresolved()[0]["ticket_id"], "TKT-1")

    def test_success_without_ima_creates_sanitized_candidate(self):
        event = self.store.record_answer("c", "邮箱 user@example.com 无法登录", "password: abc123 重置后恢复", [], "llm")
        candidate_id = self.store.record_outcome(event, True)
        candidate = self.store.get_candidate(candidate_id)
        self.assertNotIn("user@example.com", candidate["problem"])
        self.assertNotIn("abc123", candidate["solution"])

    def test_candidate_review_publish_and_rollback(self):
        candidate_id = self.store.create_candidate("打印机脱机", "无法打印", "取消脱机状态")
        self.store.update_candidate(candidate_id, {"solution": "重启打印服务"})
        self.store.update_candidate(candidate_id, {"status": "approved"}, "approve")
        path = self.store.publish(candidate_id)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.store.get_candidate(candidate_id)["status"], "published")
        self.store.rollback(candidate_id, 1)
        self.assertEqual(self.store.get_candidate(candidate_id)["solution"], "取消脱机状态")

    def test_similar_candidate_is_marked_duplicate(self):
        first = self.store.create_candidate("HP 打印机脱机", "打印机显示脱机不能打印", "取消脱机")
        second = self.store.create_candidate("HP 打印机脱机问题", "打印机显示脱机无法打印", "检查队列")
        self.assertEqual(self.store.get_candidate(second)["duplicate_of"], first)


class EngineIsolationTests(unittest.TestCase):
    def setUp(self):
        self.handler = BotMessageHandler.__new__(BotMessageHandler)
        self.handler.conversation_states = ConversationStateStore(engine_ttl=1800)

    def test_overrides_are_conversation_scoped(self):
        self.handler.conversation_states.set_engine("a", "codebuddy")
        self.assertEqual(self.handler._get_ai_engine_override("a"), "codebuddy")
        self.assertIsNone(self.handler._get_ai_engine_override("b"))

    def test_expired_override_is_removed(self):
        self.handler.conversation_states = ConversationStateStore(engine_ttl=0)
        self.handler.conversation_states.set_engine("a", "codebuddy")
        time.sleep(0.001)
        self.assertIsNone(self.handler._get_ai_engine_override("a"))


class ConversationStateStoreTests(unittest.TestCase):
    def test_attachment_replacement_returns_previous_user_file(self):
        store = ConversationStateStore()
        self.assertIsNone(store.replace_attachment("group-user-a", {"file_name": "a.png"}))
        previous = store.replace_attachment("group-user-a", {"file_name": "b.png"})
        store.replace_attachment("group-user-b", {"file_name": "other.png"})
        self.assertEqual(previous["file_name"], "a.png")
        self.assertEqual(store.pop_attachment("group-user-a")["file_name"], "b.png")
        self.assertEqual(store.pop_attachment("group-user-b")["file_name"], "other.png")

    def test_knowledge_event_is_consumed_once(self):
        store = ConversationStateStore()
        store.set_knowledge_event("session", 42)
        self.assertEqual(store.pop_knowledge_event("session"), 42)
        self.assertIsNone(store.pop_knowledge_event("session"))

    def test_spreadsheet_tasks_are_user_scoped(self):
        store = ConversationStateStore()
        store.replace_spreadsheet("group-a", {"file_name": "a.xlsx"})
        store.replace_spreadsheet("group-b", {"file_name": "b.xlsx"})
        self.assertEqual(store.get_spreadsheet("group-a")["file_name"], "a.xlsx")
        self.assertEqual(store.pop_spreadsheet("group-b")["file_name"], "b.xlsx")


class SpreadsheetServiceTests(unittest.TestCase):
    def test_parses_first_phase_instructions(self):
        self.assertEqual(SpreadsheetService.parse_instruction("检查异常")["operation"], "anomaly")
        self.assertEqual(SpreadsheetService.parse_instruction("去重")["operation"], "dedupe")
        filtered = SpreadsheetService.parse_instruction("筛选状态为未完成")
        self.assertEqual((filtered["operation"], filtered["column"], filtered["value"]), ("filter", "状态", "未完成"))
        aggregate = SpreadsheetService.parse_instruction("按部门汇总金额")
        self.assertEqual(aggregate["operation"], "aggregate")
        self.assertEqual(aggregate["group_column"], "部门")


class SpreadsheetSecondPhaseTests(unittest.TestCase):
    def test_parses_second_phase_instructions(self):
        filtered = SpreadsheetService.parse_instruction("筛选状态为未完成，且部门为运维")
        self.assertEqual(len(filtered["conditions"]), 2)
        self.assertEqual(SpreadsheetService.parse_instruction("按金额降序排序")["operation"], "sort")
        pivot = SpreadsheetService.parse_instruction("按部门汇总金额并生成图表")
        self.assertEqual((pivot["operation"], pivot["chart"]), ("pivot", True))
        self.assertEqual(SpreadsheetService.parse_instruction("手机号和邮箱脱敏")["operation"], "mask")
        self.assertEqual(SpreadsheetService.parse_instruction("按工号对比两份表")["key_column"], "工号")

    def test_keeps_two_spreadsheets_for_comparison(self):
        store = ConversationStateStore()
        self.assertEqual(store.append_spreadsheet("u", {"save_path": "a.xlsx"}), [])
        self.assertEqual(store.append_spreadsheet("u", {"save_path": "b.xlsx"}), [])
        self.assertEqual([x["save_path"] for x in store.get_spreadsheet("u")["files"]], ["a.xlsx", "b.xlsx"])
        removed = store.append_spreadsheet("u", {"save_path": "c.xlsx"})
        self.assertEqual(removed[0]["save_path"], "a.xlsx")


class VisionAnalysisTests(unittest.TestCase):
    def test_builds_openai_compatible_image_message(self):
        class Router:
            def chat(self, messages, route):
                self.messages, self.route = messages, route
                return "错误码 0x800，建议检查网络", "vision-model"

        router = Router()
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "error.png")
            with open(image, "wb") as handle:
                handle.write(b"fake-png-content")
            result, model = VisionAnalysisService(router).analyze(image)
        self.assertEqual(router.route, "vision")
        self.assertEqual(model, "vision-model")
        self.assertIn("错误码", result)
        image_url = router.messages[0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))


class DailyOpsDashboardTests(unittest.TestCase):
    def test_aggregates_repeated_issues_and_knowledge_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TicketManager(os.path.join(tmp, "tickets.db"), backup_enabled=False)
            knowledge = KnowledgeLoopStore(os.path.join(tmp, "knowledge.db"))
            for user in ("a", "b"):
                manager.create_ticket("group", "IT", user, user, "财务打印机脱机", category="设备问题")
            event = knowledge.record_answer("group", "VPN 仍然失败", "检查网络", [], "llm")
            knowledge.record_outcome(event, False, "TKT-X")
            source_event = knowledge.record_answer(
                "group", "U9 报错", "参考知识", [{"title": "旧 U9 文档", "source_kb": "U9"}], "llm")
            knowledge.record_outcome(source_event, False)
            result = DailyOpsDashboard(manager, knowledge).build()
        self.assertEqual(result["metrics"]["created"], 2)
        self.assertEqual(result["repeated_issues"][0]["count"], 2)
        self.assertEqual(result["metrics"]["knowledge_gaps"], 2)
        self.assertEqual(result["low_knowledge_sources"][0]["title"], "旧 U9 文档")


class MessageDispatchIsolationTests(unittest.TestCase):
    @staticmethod
    def info(user, content="补充信息"):
        return {
            "conv_id": "same-group", "sender_id": user, "staff_id": user,
            "sender_nick": user, "content": content, "conv_title": "测试群",
        }

    def base_handler(self):
        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.ticket_enabled = True
        handler.triage_engine = TriageEngine()
        handler.resolution_enabled = False
        handler.resolution_engine = ProblemResolutionEngine()
        handler.ima_config = {"expert_triggers": [], "trigger_keywords": []}
        handler.conversation_states = ConversationStateStore()
        handler.reply_text = lambda *args, **kwargs: None
        return handler

    def test_group_members_have_distinct_state_keys(self):
        handler = self.base_handler()
        a = handler._user_session_key(self.info("user-a"))
        b = handler._user_session_key(self.info("user-b"))
        self.assertNotEqual(a, b)
        handler._set_pending_feedback(a, "A 的问题")
        handler._set_pending_feedback(b, "B 的问题")
        self.assertEqual(handler._get_pending_feedback(a)["question"], "A 的问题")
        self.assertEqual(handler._get_pending_feedback(b)["question"], "B 的问题")

    def test_completed_triage_creates_ticket_without_entering_resolution(self):
        handler = self.base_handler()
        info = self.info("user-a", "HP 1020，重启无效，只影响我")
        key = handler._user_session_key(info)
        handler.triage_engine.start(key, "打印机坏了", "设备问题")
        calls = []
        handler._handle_ticket = lambda msg, payload, parsed, is_admin: calls.append(dict(payload)) or True
        handler._dispatch_text_message(object(), info)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["triage_completed"])
        self.assertIn("HP 1020", calls[0]["content"])

    def test_resolution_ready_does_not_access_triage_result(self):
        handler = self.base_handler()
        handler.ticket_enabled = False
        handler.resolution_enabled = True
        handler.llm_client = object()
        handler.cb_client = None
        handler._get_expert_state = lambda key: None
        calls = []
        handler._handle_ai_chat = lambda msg, payload, expert: calls.append(payload["content"])
        info = self.info("user-a", "HP 1020，重启无效，只影响我")
        key = handler._user_session_key(info)
        handler.resolution_engine.begin(key, "打印机不能打印")
        handler._dispatch_text_message(object(), info)
        self.assertEqual(len(calls), 1)
        self.assertIn("用户补充", calls[0])

    def test_one_group_member_does_not_consume_anothers_triage(self):
        handler = self.base_handler()
        a_info = self.info("user-a")
        b_info = self.info("user-b")
        a_key = handler._user_session_key(a_info)
        b_key = handler._user_session_key(b_info)
        handler.triage_engine.start(a_key, "A 打印机坏了", "设备问题")
        handler.triage_engine.start(b_key, "B VPN 不通", "软件问题")
        handler._handle_ticket = lambda *args, **kwargs: True
        handler._dispatch_text_message(object(), a_info)
        self.assertFalse(handler.triage_engine.has_session(a_key))
        self.assertTrue(handler.triage_engine.has_session(b_key))


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
    def test_dynamic_triage_asks_once_and_builds_complete_description(self):
        engine = TriageEngine(min_description_length=30)
        self.assertTrue(engine.needs_triage("打印机坏了"))
        first = engine.start("conv", "打印机坏了", "设备问题")
        self.assertIn("一次补充", first)
        self.assertLessEqual(first.count("\n1." ) + first.count("\n2."), 2)
        result = engine.consume("conv", "HP 1020，财务室，今天开始，重启无效，三个人不能打印")
        self.assertEqual(result["status"], "completed")
        self.assertIn("HP 1020", result["description"])
        self.assertFalse(engine.has_session("conv"))

    def test_triage_can_be_cancelled(self):
        engine = TriageEngine()
        engine.start("conv", "VPN 不行", "设备问题")
        self.assertEqual(engine.consume("conv", "取消报修")["status"], "cancelled")


class ProblemResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = ProblemResolutionEngine(max_failures=2)

    def test_complete_incident_goes_directly_to_solution(self):
        self.assertEqual(TicketParser.parse("打印机坏了").intent, TicketIntent.NONE)
        result = self.engine.begin("c", "财务部打印机今天早上脱机，重启无效，目前三个人不能打印")
        self.assertEqual(result["action"], "continue")
        self.assertTrue(self.engine.has_session("c"))

    def test_short_incident_only_asks_up_to_two_questions(self):
        result = self.engine.begin("c", "打印机不能打印")
        self.assertEqual(result["action"], "clarify")
        self.assertLessEqual(result["message"].count("\n1.") + result["message"].count("\n2."), 2)
        ready = self.engine.consume("c", "HP 1020，重启无效，只影响我")
        self.assertEqual(ready["action"], "ready")

    def test_two_failures_escalate_with_attempt_history(self):
        self.engine.begin("c", "VPN 今天无法连接，重启电脑无效，只影响我自己")
        formatted = self.engine.record_answer("c", "VPN", "请重新连接 VPN")
        self.assertIn("第 1 步", formatted)
        first = self.engine.consume("c", "还是不行")
        self.assertEqual(first["action"], "retry")
        self.engine.record_answer("c", first["content"], "请清理 VPN 缓存")
        second = self.engine.consume("c", "没变化")
        self.assertEqual(second["action"], "escalate")
        self.assertIn("第 2 次排查", second["summary"])

    def test_natural_success_and_manual_handoff(self):
        self.engine.begin("a", "U9 今天打不开，重新登录无效，只影响我")
        self.engine.record_answer("a", "U9", "清理浏览器缓存")
        self.assertEqual(self.engine.consume("a", "已经恢复了")["action"], "resolved")
        self.engine.begin("b", "网络无法连接")
        self.assertEqual(self.engine.consume("b", "转人工")["action"], "escalate")

    def test_security_and_service_requests_escalate_immediately(self):
        self.assertEqual(self.engine.begin("s", "电脑疑似中毒")["action"], "escalate")
        self.assertEqual(self.engine.begin("p", "帮我申请财务系统权限")["action"], "escalate")


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
