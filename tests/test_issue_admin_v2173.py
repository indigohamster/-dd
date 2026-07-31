import tempfile
import unittest
from pathlib import Path

import yaml

from modules.issue import IssueRecord, IssueStore
from modules.ticket import TicketManager
from utils.admin_server import create_admin_app


class IssueAdminV2173Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.issue_db = root / "issues.db"
        self.ticket_db = root / "tickets.db"
        ticket_manager = TicketManager(self.ticket_db, backup_enabled=False)
        self.linked_ticket = ticket_manager.create_ticket(
            "conv-linked", "邮箱问题", "user-linked", "测试用户", "邮箱无法登录"
        )
        self.unlinked_ticket = ticket_manager.create_ticket(
            "conv-legacy", "旧工单", "user-legacy", "旧用户", "没有 Issue 的旧工单"
        )
        self.store = IssueStore(self.issue_db)
        self.store.create(IssueRecord(
            issue_id="ISS-OPEN",
            user_id="user-open",
            original_question="无法连接公司网络",
            conversation_id="conv-open",
            steps_json=[{"step": "检查网线", "result": "正常"}],
        ))
        self.store.create(IssueRecord(
            issue_id="ISS-LINKED",
            user_id="user-linked",
            original_question="邮箱无法登录",
            conversation_id="conv-linked",
        ))
        self.store.link_ticket("ISS-LINKED", self.linked_ticket["ticket_id"])
        self.store.create(IssueRecord(
            issue_id="ISS-CLOSED",
            user_id="user-closed",
            original_question="打印机离线",
            conversation_id="conv-closed",
        ))
        self.store.link_ticket("ISS-CLOSED", "TKT-20990101-999")
        self.store.close_by_ticket("TKT-20990101-999", "重新安装打印机驱动后恢复")

        self.config_path = root / "config.yaml"
        self.settings_path = root / "admin_settings.json"
        self.config_path.write_text(
            yaml.safe_dump({
                "issue_context": {
                    "enabled": True,
                    "database_path": str(self.issue_db),
                },
                "ticket": {
                    "enabled": True,
                    "database_path": str(self.ticket_db),
                },
            }, allow_unicode=True),
            encoding="utf-8",
        )
        app = create_admin_app(
            config_path=self.config_path,
            settings_path=self.settings_path,
        )
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_store_filters_existing_schema_without_migration(self):
        self.assertEqual(len(self.store.list_issues()), 3)
        self.assertEqual(
            [item["issue_id"] for item in self.store.list_issues(status="open")],
            ["ISS-OPEN"],
        )
        self.assertEqual(
            [item["issue_id"] for item in self.store.list_issues(status="closed")],
            ["ISS-CLOSED"],
        )
        self.assertEqual(len(self.store.list_issues(linked=True)), 2)
        self.assertEqual(
            [item["issue_id"] for item in self.store.list_issues(linked=False)],
            ["ISS-OPEN"],
        )

    def test_admin_list_supports_required_filters(self):
        expected = {
            "all": 3,
            "open": 1,
            "closed": 1,
            "linked": 2,
            "unlinked": 1,
        }
        for filter_name, count in expected.items():
            with self.subTest(filter_name=filter_name):
                response = self.client.get(f"/api/issues?filter={filter_name}")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["enabled"])
                self.assertEqual(response.get_json()["total"], count)

    def test_issue_detail_returns_existing_fields_and_steps(self):
        response = self.client.get("/api/issues/ISS-OPEN")
        self.assertEqual(response.status_code, 200)
        issue = response.get_json()["issue"]
        self.assertEqual(issue["original_question"], "无法连接公司网络")
        self.assertEqual(issue["user_id"], "user-open")
        self.assertEqual(issue["conversation_id"], "conv-open")
        self.assertEqual(issue["steps_json"][0]["result"], "正常")
        self.assertIn("created_at", issue)
        self.assertIn("updated_at", issue)
        self.assertIn("final_result", issue)

    def test_invalid_filter_and_missing_issue_are_clear_errors(self):
        invalid = self.client.get("/api/issues?filter=unknown")
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("不支持", invalid.get_json()["message"])
        missing = self.client.get("/api/issues/ISS-NOT-FOUND")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["message"], "Issue 不存在")

    def test_ticket_detail_includes_issue_and_old_ticket_remains_compatible(self):
        linked = self.client.get(f"/api/tickets/{self.linked_ticket['ticket_id']}")
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(linked.get_json()["issue"]["issue_id"], "ISS-LINKED")
        self.assertEqual(linked.get_json()["issue"]["original_question"], "邮箱无法登录")

        legacy = self.client.get(f"/api/tickets/{self.unlinked_ticket['ticket_id']}")
        self.assertEqual(legacy.status_code, 200)
        self.assertIsNone(legacy.get_json()["issue"])
        self.assertEqual(legacy.get_json()["ticket"]["title"], "没有 Issue 的旧工单")

    def test_frontend_contains_issue_page_filters_detail_and_ticket_jump(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "admin_ui" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "admin_ui" / "app.js").read_text(encoding="utf-8")
        shell_js = (root / "admin_ui" / "console-shell.js").read_text(encoding="utf-8")
        for marker in (
            'id="issues"',
            'data-issue-filter="open"',
            'data-issue-filter="closed"',
            'data-issue-filter="linked"',
            'data-issue-filter="unlinked"',
            'id="issue-detail"',
        ):
            self.assertIn(marker, html)
        self.assertIn("/api/issues?filter=", app_js)
        self.assertIn("/api/issues/${encodeURIComponent(id)}", app_js)
        self.assertIn("openIssueTicket", app_js)
        self.assertIn("ticketIssueContextMarkup(d.issue)", app_js)
        self.assertIn("AI 是否处理过", app_js)
        self.assertIn("未记录，无法确认", app_js)
        self.assertIn("issues:()=>loadIssues()", shell_js)


if __name__ == "__main__":
    unittest.main()
