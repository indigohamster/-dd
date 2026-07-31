import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from modules.knowledge_loop import KnowledgeLoopStore
from modules.ticket.ticket_manager import TicketManager
from handlers.chat_handler import BotMessageHandler
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

    def test_title_only_result_is_hydrated_from_media_download(self):
        client = IMAClient("client", "key")
        search_response = Mock()
        search_response.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"info_list": [{
                "title": "VPN recovery",
                "media_id": "media-1",
                "media_type": 13,
                "highlight_content": "",
            }]},
        }
        media_response = Mock()
        media_response.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {
                "media_type": 13,
                "url_info": {"url": "https://download.example/file.txt", "headers": {}},
            },
        }
        client._session.post = Mock(side_effect=[search_response, media_response])
        download = Mock()
        download.headers = {"Content-Type": "text/plain; charset=utf-8"}
        download.encoding = "utf-8"
        download.iter_content.return_value = [
            "Restart the VPN service and clear the cached credential before reconnecting."
            .encode("utf-8")
        ]

        with patch("utils.ima_client.requests.get", return_value=download):
            results = client.search_knowledge("VPN", "kb-1")

        self.assertTrue(client.last_search_ok)
        self.assertTrue(client.last_request_ok)
        self.assertIn("clear the cached credential", results[0]["content"])
        self.assertEqual(results[0]["content_source"], "ima_media")

    def test_media_permission_failure_is_reported_without_faking_search_failure(self):
        client = IMAClient("client", "key")
        search_response = Mock()
        search_response.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"info_list": [{
                "title": "Restricted item",
                "media_id": "media-2",
                "media_type": 99,
                "highlight_content": "",
            }]},
        }
        media_response = Mock()
        media_response.json.return_value = {
            "code": 220030,
            "msg": "该文件获取失败，请至ima内查看处理",
            "data": {},
        }
        client._session.post = Mock(side_effect=[search_response, media_response])

        results = client.search_knowledge("restricted", "kb-1")

        self.assertTrue(client.last_search_ok)
        self.assertTrue(client.last_request_ok)
        self.assertNotIn("content", results[0])
        self.assertIn("IMA可连接，但正文不可读取", client.last_content_error)
        self.assertEqual(client.last_content_code, 220030)

    def test_docx_text_extraction_uses_document_xml(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="urn:test"><w:body><w:p><w:r><w:t>'
                "OpsPilot unique recovery step"
                "</w:t></w:r></w:p></w:body></w:document>",
            )
        self.assertIn(
            "OpsPilot unique recovery step",
            IMAClient._extract_docx_text(payload.getvalue()),
        )


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

    def test_confirmed_ticket_becomes_published_and_matches_similar_question(self):
        manager = TicketManager(
            os.path.join(self.tmp.name, "tickets.db"), backup_enabled=False,
        )
        ticket = manager.create_ticket(
            "conversation-1",
            "IT support",
            "user-1",
            "Employee",
            "BlueWhale printer shows NP-7421 and cannot print",
        )
        manager.resolve_ticket(
            ticket["ticket_id"],
            "Restart PrintFlux and run the OPK-17 verification page",
            "admin-1",
            "Operator",
        )
        confirmed = manager.confirm_resolution(
            ticket["ticket_id"], "user-1", "Employee",
        )
        candidate_id = self.store.create_from_ticket(confirmed)
        self.assertEqual(self.store.get_candidate(candidate_id)["status"], "draft")
        self.store.update_candidate(candidate_id, {"status": "approved"}, "approve")
        self.store.publish(candidate_id)

        results = self.store.search_published(
            "BlueWhale printer NP-7421 cannot print"
        )
        self.assertEqual(results[0]["candidate_id"], candidate_id)
        self.assertIn("PrintFlux", results[0]["content"])
        self.assertIn("OPK-17", results[0]["content"])

    def test_ai_prompt_receives_published_local_knowledge(self):
        candidate_id = self.store.create_candidate(
            "BlueWhale printer NP-7421",
            "BlueWhale printer cannot print and displays NP-7421",
            "Restart PrintFlux and run the OPK-17 verification page",
        )
        self.store.update_candidate(candidate_id, {"status": "approved"}, "approve")
        self.store.publish(candidate_id)

        handler = BotMessageHandler.__new__(BotMessageHandler)
        handler.logger = Mock()
        handler.ima_client = None
        handler.ima_config = {}
        handler.llm_client = Mock()
        handler.cb_client = None
        handler.model_router = Mock()
        handler.model_router.chat.return_value = ("Use the published procedure.", "test-model")
        handler.resolution_engine = Mock()
        handler.resolution_engine.record_answer.side_effect = (
            lambda _session, _query, answer: answer
        )
        handler.resolution_engine.has_session.return_value = False
        handler.knowledge_loop_enabled = True
        handler.knowledge_loop = self.store
        handler.conversation_states = Mock()
        handler.ticket_enabled = False
        handler.reply_text = Mock()
        handler._set_pending_feedback = Mock()
        handler._user_session_key = Mock(return_value="session:user-1")
        handler._get_ai_engine_override = Mock(return_value=None)
        info = {
            "content": "BlueWhale printer NP-7421 cannot print",
            "conv_id": "conversation-1",
            "msg_id": "message-1",
            "sender_nick": "Employee",
            "sender_id": "user-1",
            "conv_label": "direct",
            "conv_title": "",
        }

        with (
            patch("handlers.chat_handler.bot_status.set_last_engine"),
            patch("handlers.chat_handler.msg_logger.record"),
        ):
            handler._handle_ai_chat(object(), info, target_kbs=[])

        messages = handler.model_router.chat.call_args.args[0]
        system_prompt = messages[0]["content"]
        self.assertIn("PrintFlux", system_prompt)
        self.assertIn("OPK-17", system_prompt)
        self.assertIn("OpsPilot 本地已发布知识", system_prompt)


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
