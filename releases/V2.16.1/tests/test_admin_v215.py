import os
import tempfile
import unittest
from unittest.mock import patch

from modules.ticket import TicketManager
from utils.admin_server import create_admin_app


class AdminV215Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name.replace(os.sep, "/")
        self.config_path = os.path.join(self.tmp.name, "config.yaml")
        self.settings_path = os.path.join(self.tmp.name, "admin_settings.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "credentials:\n  client_id: ''\n  client_secret: ''\n"
                f"ticket:\n  database_path: {root}/tickets.db\n"
                f"knowledge_loop:\n  database_path: {root}/knowledge.db\n  export_dir: {root}/exports\n"
                f"async_tasks:\n  database_path: {root}/tasks.db\n"
                "ticket_notifications:\n  enabled: false\n"
                f"observability:\n  database_path: {root}/observability.db\n"
            )
        app = create_admin_app(self.config_path, self.settings_path)
        app.testing = True
        self.client = app.test_client()
        self.manager = TicketManager(os.path.join(self.tmp.name, "tickets.db"), backup_enabled=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_catalog_validation_and_admin_ticket_creation(self):
        catalog = [{
            "code": "VPN", "name": "VPN 服务", "request_type": "request",
            "category": "设备问题", "priority": "high", "assignment_group": "网络组",
            "owner_id": "staff-net", "owner_name": "网络管理员",
            "response_hours": 1, "resolution_hours": 6, "keywords": "VPN,远程",
            "approval_required": True, "instructions": "填写地点", "enabled": True,
        }]
        saved = self.client.put("/api/settings/service_catalog", json=catalog)
        self.assertEqual(saved.status_code, 200)
        item = saved.get_json()["data"][0]
        self.assertEqual(item["request_type"], "service_request")
        self.assertEqual(item["priority"], "high")
        self.assertEqual(item["keywords"], ["VPN", "远程"])
        self.assertTrue(item["approval_required"])

        created = self.client.post("/api/tickets", json={
            "creator_nick": "测试员工", "creator_staff_id": "staff-1",
            "service_code": "VPN", "title": "申请远程访问", "description": "出差使用",
        })
        self.assertEqual(created.status_code, 201)
        ticket = created.get_json()["ticket"]
        self.assertEqual(ticket["service_code"], "VPN")
        self.assertEqual(ticket["request_type"], "service_request")
        self.assertEqual(ticket["assignment_group"], "网络组")
        self.assertEqual(ticket["priority"], "high")
        self.assertEqual(ticket["status"], "assigned")
        self.assertEqual(ticket["assignee_id"], "staff-net")
        self.assertEqual(ticket["assignee_nick"], "网络管理员")

        invalid = self.client.put("/api/settings/service_catalog", json=[{
            "code": "bad code", "name": "错误", "response_hours": 8, "resolution_hours": 1,
        }])
        self.assertEqual(invalid.status_code, 400)

    def test_service_owner_name_fallback_and_assignment_failure_contract(self):
        catalog = [{
            "code": "LEGACY", "name": "历史服务项", "owner_name": "旧版负责人",
            "response_hours": 4, "resolution_hours": 24,
        }]
        saved = self.client.put("/api/settings/service_catalog", json=catalog)
        self.assertEqual(saved.status_code, 200)

        created = self.client.post("/api/tickets", json={
            "creator_nick": "测试员工", "service_code": "LEGACY", "title": "历史服务请求",
        })
        self.assertEqual(created.status_code, 201)
        ticket = created.get_json()["ticket"]
        self.assertEqual(ticket["status"], "assigned")
        self.assertEqual(ticket["assignee_id"], "旧版负责人")
        self.assertEqual(ticket["assignee_nick"], "旧版负责人")

        with patch.object(TicketManager, "assign_ticket", return_value=None):
            failed = self.client.post("/api/tickets", json={
                "creator_nick": "测试员工", "service_code": "LEGACY", "title": "模拟分派失败",
            })
        self.assertEqual(failed.status_code, 500)
        body = failed.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("自动分派失败", body["message"])
        self.assertTrue(body["ticket_id"].startswith("TKT-"))

    def test_modern_query_keeps_legacy_response_and_batch_updates(self):
        first = self.manager.create_ticket("c1", "IT", "u1", "甲", "网络中断", priority="urgent")
        second = self.manager.create_ticket("c2", "IT", "u2", "乙", "软件安装", priority="medium")
        legacy = self.client.get("/api/tickets?filter=all")
        self.assertIsInstance(legacy.get_json(), list)

        modern = self.client.get("/api/tickets?page=1&page_size=1&status=open").get_json()
        self.assertEqual(modern["total"], 2)
        self.assertEqual(len(modern["items"]), 1)

        result = self.client.post("/api/tickets/batch", json={
            "ticket_ids": [first["ticket_id"], second["ticket_id"]],
            "action": "priority", "priority": "high",
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["succeeded"], 2)
        self.assertEqual(self.manager.get_ticket(first["ticket_id"])["priority"], "high")
        too_many = self.client.post("/api/tickets/batch", json={
            "ticket_ids": [f"TKT-{i}" for i in range(101)], "action": "progress",
        })
        self.assertEqual(too_many.status_code, 400)

    def test_internal_comment_visibility_and_confirmed_knowledge(self):
        ticket = self.manager.create_ticket("c", "IT", "u", "用户", "VPN 故障")
        comment = self.client.patch(f"/api/tickets/{ticket['ticket_id']}", json={
            "action": "comment", "detail": "仅内部排查记录", "visibility": "internal",
        })
        self.assertEqual(comment.status_code, 200)
        detail = self.client.get(f"/api/tickets/{ticket['ticket_id']}").get_json()
        self.assertEqual(detail["logs"][0]["visibility"], "internal")

        self.manager.set_in_progress(ticket["ticket_id"], "admin", "管理员")
        resolved = self.client.patch(f"/api/tickets/{ticket['ticket_id']}", json={
            "action": "resolve", "resolution": "重新配置 VPN",
        })
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(self.client.get("/api/knowledge/candidates").get_json(), [])
        confirmed = self.client.post(f"/api/tickets/{ticket['ticket_id']}/confirm", json={
            "confirmed": True, "score": 5, "comment": "已恢复",
        })
        self.assertEqual(confirmed.status_code, 200)
        detail = self.client.get(f"/api/tickets/{ticket['ticket_id']}").get_json()
        self.assertEqual(detail["ticket"]["satisfaction_score"], 5)
        self.assertEqual(detail["feedback"][0]["comment"], "已恢复")
        self.assertEqual(len(self.client.get("/api/knowledge/candidates").get_json()), 1)


if __name__ == "__main__":
    unittest.main()
