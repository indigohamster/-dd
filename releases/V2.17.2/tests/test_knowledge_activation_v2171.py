import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from modules.knowledge_loop import KnowledgeLoopStore
from utils.admin_server import create_admin_app
from utils.ima_client import IMAClient


ROOT = Path(__file__).resolve().parents[1]


class ImaConnectionTruthTests(unittest.TestCase):
    @staticmethod
    def failed_client():
        client = IMAClient("client", "key")
        response = Mock()
        response.json.return_value = {"code": 401, "msg": "skill auth failed"}
        client._session.post = Mock(return_value=response)
        return client

    def test_auth_failure_is_not_reported_as_connected(self):
        client = self.failed_client()
        self.assertFalse(client.test_connection())
        self.assertFalse(client.last_request_ok)
        self.assertEqual(client.last_error, "skill auth failed")

    def test_failed_credentials_stop_current_multi_kb_search(self):
        client = self.failed_client()
        results = client.search_knowledge_bases("VPN", [
            {"id": "kb-1", "name": "一"},
            {"id": "kb-2", "name": "二"},
            {"id": "kb-3", "name": "三"},
        ])
        self.assertEqual(results, [])
        self.assertEqual(client._session.post.call_count, 1)


class LocalPublishedKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = KnowledgeLoopStore(
            os.path.join(self.tmp.name, "knowledge.db"),
            os.path.join(self.tmp.name, "exports"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_published_knowledge_is_searchable(self):
        draft = self.store.create_candidate(
            "VPN 连接失败", "VPN 提示认证失败", "重新配置 VPN 账号并连接",
        )
        self.assertEqual(self.store.search_published("VPN认证失败"), [])
        self.store.update_candidate(draft, {"status": "approved"}, "approve")
        self.store.publish(draft)
        results = self.store.search_published("VPN认证失败")
        self.assertEqual(results[0]["candidate_id"], draft)
        self.assertEqual(results[0]["source_kb"], "OpsPilot 本地已发布知识")

    def test_editing_published_content_requires_review_and_republish(self):
        candidate_id = self.store.create_candidate(
            "邮箱配额已满", "邮箱无法接收新邮件", "清理邮箱空间后重新同步",
        )
        self.store.update_candidate(candidate_id, {"status": "approved"}, "approve")
        first_path = Path(self.store.publish(candidate_id))
        self.assertTrue(first_path.is_file())

        updated = self.store.update_candidate(candidate_id, {
            "solution": "归档历史邮件，释放空间后重新同步客户端",
        })
        self.assertEqual(updated["status"], "draft")
        self.assertEqual(updated["ima_sync_status"], "outdated")
        self.assertIsNone(updated["published_at"])
        self.assertEqual(self.store.search_published("邮箱无法接收邮件"), [])

        self.store.update_candidate(candidate_id, {"status": "approved"}, "approve")
        second_path = Path(self.store.publish(candidate_id))
        self.assertEqual(first_path, second_path)
        self.assertIn("归档历史邮件", second_path.read_text(encoding="utf-8"))
        self.assertEqual(list(second_path.parent.glob("*.tmp")), [])

    def test_unresolved_event_becomes_one_editable_draft(self):
        event = self.store.record_answer("c", "打印机一直离线", "重启看看", [], "llm")
        self.store.record_outcome(event, False, "TKT-1")
        first = self.store.create_from_event(event)
        second = self.store.create_from_event(event)
        self.assertEqual(first, second)
        candidate = self.store.get_candidate(first)
        self.assertEqual(candidate["status"], "draft")
        self.assertEqual(candidate["solution"], "")
        self.assertEqual(candidate["source_ticket_id"], "TKT-1")


class KnowledgeWorkbenchActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name.replace(os.sep, "/")
        self.config_path = os.path.join(self.tmp.name, "config.yaml")
        self.settings_path = os.path.join(self.tmp.name, "settings.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                f"ticket:\n  database_path: {root}/tickets.db\n"
                f"knowledge_loop:\n  enabled: true\n  database_path: {root}/knowledge.db\n"
                f"  export_dir: {root}/exports\n"
                f"async_tasks:\n  database_path: {root}/tasks.db\n"
                f"observability:\n  database_path: {root}/observability.db\n"
                "ticket_notifications:\n  enabled: false\n"
            )
        self.store = KnowledgeLoopStore(
            os.path.join(self.tmp.name, "knowledge.db"),
            os.path.join(self.tmp.name, "exports"),
        )
        app = create_admin_app(self.config_path, self.settings_path)
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_gap_can_be_converted_completed_and_published(self):
        event = self.store.record_answer("c", "邮箱无法收信", "重新登录", [], "llm")
        self.store.record_outcome(event, False, "TKT-2")
        converted = self.client.post(f"/api/knowledge/unresolved/{event}/candidate")
        self.assertEqual(converted.status_code, 200)
        candidate_id = converted.get_json()["candidate_id"]

        incomplete = self.client.post(f"/api/knowledge/candidates/{candidate_id}/approve")
        self.assertEqual(incomplete.status_code, 400)
        self.assertIn("处理步骤", incomplete.get_json()["message"])

        saved = self.client.put(f"/api/knowledge/candidates/{candidate_id}", json={
            "solution": "检查邮箱配额，释放空间后重新同步",
            "validation": "发送测试邮件并确认收取成功",
        })
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/knowledge/candidates/{candidate_id}/approve").status_code,
            200,
        )
        published = self.client.post(f"/api/knowledge/candidates/{candidate_id}/publish")
        self.assertEqual(published.status_code, 200)
        self.assertIn("本地知识已可用于回答", published.get_json()["message"])

    def test_frontend_exposes_gap_actions_and_local_publish(self):
        html = (ROOT / "admin_ui" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "admin_ui" / "app.js").read_text(encoding="utf-8")
        shell_js = (ROOT / "admin_ui" / "console-shell.js").read_text(encoding="utf-8")
        handler = (ROOT / "handlers" / "chat_handler.py").read_text(encoding="utf-8")
        self.assertIn('id="knowledge-unresolved-list"', html)
        self.assertIn("createKnowledgeFromUnresolved", app_js)
        self.assertIn("发布到本地并导出", app_js)
        self.assertIn("currentView()!=='knowledge'", shell_js)
        self.assertIn("search_published(query, max_results)", handler)


if __name__ == "__main__":
    unittest.main()
