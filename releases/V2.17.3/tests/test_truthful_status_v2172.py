import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from modules.knowledge_loop import KnowledgeLoopStore
from modules.ticket.ticket_manager import TicketManager
from utils.admin_server import create_admin_app
from utils.ima_client import IMAClient


ROOT = Path(__file__).resolve().parents[1]


class FakeIMAClient(IMAClient):
    def __init__(self, results):
        super().__init__("client", "key")
        self.results = results

    def list_knowledge_bases(self, query="", cursor="", limit=20):
        self.last_request_ok = True
        return [{"kb_id": "kb-1", "kb_name": "测试知识库"}]

    def search_knowledge_bases(self, query, kb_list):
        self.last_request_ok = True
        return self.results


class TruthfulStatusV2172Tests(unittest.TestCase):
    def test_active_close_becomes_resolved_and_requires_solution(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = TicketManager(Path(temp) / "tickets.db", backup_enabled=False)
            ticket = manager.create_ticket("conv", "会话", "u1", "用户", "打印机故障")
            self.assertIsNone(manager.close_ticket(ticket["ticket_id"], "admin", "管理员", ""))
            resolved = manager.close_ticket(ticket["ticket_id"], "admin", "管理员", "重启打印服务")
            self.assertEqual(resolved["status"], "resolved")
            self.assertEqual(resolved["resolution"], "重启打印服务")
            self.assertIsNone(manager.close_ticket(ticket["ticket_id"], "admin", "管理员", "再次关闭"))
            closed = manager.confirm_resolution(ticket["ticket_id"], "u1", "用户")
            self.assertEqual(closed["status"], "closed")
            self.assertEqual(closed["user_confirmation"], "confirmed")

    def test_special_closure_requires_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = TicketManager(Path(temp) / "tickets.db", backup_enabled=False)
            withdraw = manager.create_ticket("c1", "会话", "u1", "用户", "撤回测试")
            invalid = manager.create_ticket("c2", "会话", "u2", "用户", "无效测试")
            self.assertIsNone(manager.withdraw_ticket(withdraw["ticket_id"], "u1", "用户", ""))
            self.assertIsNone(manager.mark_as_invalid(invalid["ticket_id"], "admin", "管理员", ""))
            self.assertEqual(manager.withdraw_ticket(withdraw["ticket_id"], "u1", "用户", "不再需要")["status"], "closed")
            self.assertEqual(manager.mark_as_invalid(invalid["ticket_id"], "admin", "管理员", "重复提交")["status"], "closed")

    def test_ima_health_distinguishes_title_only_results(self):
        client = FakeIMAClient([{"title": "文档标题", "highlight_content": ""}])
        health = client.test_health("打印机")
        self.assertTrue(health["connected"])
        self.assertTrue(health["search_ok"])
        self.assertFalse(health["content_available"])
        self.assertIn("无 AI 可用正文", health["detail"])

    def test_shutdown_endpoint_calls_supplied_callback(self):
        called = threading.Event()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.yaml"
            config.write_text("credentials: {}\n", encoding="utf-8")
            app = create_admin_app(config, root / "settings.json", shutdown_callback=called.set)
            response = app.test_client().post("/api/runtime/shutdown", json={})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(called.wait(2))

    def test_published_knowledge_export_can_be_viewed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "knowledge.db"
            export_dir = root / "exports"
            config = root / "config.yaml"
            config.write_text(
                "credentials: {}\n"
                "knowledge_loop:\n"
                f"  database_path: '{db_path.as_posix()}'\n"
                f"  export_dir: '{export_dir.as_posix()}'\n",
                encoding="utf-8",
            )
            store = KnowledgeLoopStore(db_path, export_dir)
            candidate_id = store.create_candidate(
                title="Printer recovery",
                problem="The office printer is unavailable",
                solution="Restart the print spooler and verify a test page",
            )
            store.update_candidate(candidate_id, {"status": "approved"}, "approve")
            store.publish(candidate_id)

            app = create_admin_app(config, root / "settings.json")
            response = app.test_client().get(
                f"/api/knowledge/candidates/{candidate_id}/export"
            )
            try:
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Printer recovery", response.data)
                self.assertEqual(response.mimetype, "text/markdown")
            finally:
                response.close()

    def test_open_knowledge_export_directory_uses_configured_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "knowledge.db"
            export_dir = root / "exports"
            config = root / "config.yaml"
            config.write_text(
                "credentials: {}\n"
                "knowledge_loop:\n"
                f"  database_path: '{db_path.as_posix()}'\n"
                f"  export_dir: '{export_dir.as_posix()}'\n",
                encoding="utf-8",
            )
            app = create_admin_app(config, root / "settings.json")
            with mock.patch.object(os, "startfile", create=True) as startfile:
                response = app.test_client().post(
                    "/api/knowledge/exports/open-directory", json={}
                )
            self.assertEqual(response.status_code, 200)
            startfile.assert_called_once_with(str(export_dir.resolve()))

    def test_open_export_directory_failure_is_chinese_and_directory_still_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "knowledge.db"
            export_dir = root / "new-exports"
            config = root / "config.yaml"
            config.write_text(
                "credentials: {}\n"
                "knowledge_loop:\n"
                f"  database_path: '{db_path.as_posix()}'\n"
                f"  export_dir: '{export_dir.as_posix()}'\n",
                encoding="utf-8",
            )
            app = create_admin_app(config, root / "settings.json")
            with mock.patch.object(
                os, "startfile", create=True, side_effect=OSError("blocked")
            ):
                response = app.test_client().post(
                    "/api/knowledge/exports/open-directory", json={}
                )
            self.assertEqual(response.status_code, 500)
            self.assertTrue(export_dir.is_dir())
            self.assertIn("Windows 无法打开知识导出目录", response.get_json()["message"])

    def test_unknown_api_route_never_returns_default_english_404(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.yaml"
            config.write_text("credentials: {}\n", encoding="utf-8")
            app = create_admin_app(config, root / "settings.json")
            response = app.test_client().post("/api/knowledge/not-a-route", json={})
            self.assertEqual(response.status_code, 404)
            message = response.get_json()["message"]
            self.assertIn("接口不存在", message)
            self.assertNotIn("requested URL", message)

    def test_admin_ui_has_visible_refresh_and_shutdown_contracts(self):
        html = (ROOT / "admin_ui" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "admin_ui" / "app.js").read_text(encoding="utf-8")
        shell_js = (ROOT / "admin_ui" / "console-shell.js").read_text(encoding="utf-8")
        self.assertIn("ticket-last-updated", html)
        self.assertIn("关闭服务", html)
        self.assertIn("/api/runtime/shutdown", app_js)
        self.assertIn("最后更新：", app_js)
        self.assertIn("visibilitychange", shell_js)
        self.assertIn("打开导出目录", html)
        self.assertIn("查看导出文件", app_js)
        self.assertIn("/api/knowledge/exports/open-directory", app_js)
        self.assertIn("当前后台仍是旧版本", app_js)
        self.assertIn("导出文件接口不可用", app_js)


if __name__ == "__main__":
    unittest.main()
