import os
import tempfile
import unittest
from datetime import datetime, timedelta

from modules.knowledge_loop import KnowledgeLoopStore
from modules.ops_dashboard import DailyOpsDashboard
from modules.ticket import TicketManager
from utils.aitable_ticket_sync import DEFAULT_FIELD_NAMES


class DashboardV215Tests(unittest.TestCase):
    def test_dashboard_exposes_sla_service_and_feedback_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TicketManager(os.path.join(tmp, "tickets.db"), backup_enabled=False)
            knowledge = KnowledgeLoopStore(os.path.join(tmp, "knowledge.db"))
            resolved = manager.create_ticket(
                "conv", "IT", "user-1", "用户一", "VPN 无法连接",
                service_code="NETWORK", service_name="网络与 VPN 报修",
                assignment_group="网络运维", priority="high",
            )
            manager.assign_ticket(
                resolved["ticket_id"], "admin-1", "网络管理员", "lead", "主管",
            )
            manager.resolve_ticket(resolved["ticket_id"], "刷新 VPN 凭据", "admin-1", "网络管理员")
            manager.confirm_resolution(
                resolved["ticket_id"], "user-1", "用户一", score=4, comment="已恢复",
            )
            manager.create_ticket(
                "conv", "IT", "user-2", "用户二", "打印机脱机",
                service_code="DEVICE", service_name="电脑与外设报修",
            )

            result = DailyOpsDashboard(manager, knowledge).build()

        self.assertIn("response_sla_percent", result["metrics"])
        self.assertIn("resolution_sla_percent", result["metrics"])
        self.assertEqual(result["metrics"]["csat"], 4)
        self.assertEqual(result["metrics"]["confirmation_rate"], 100)
        self.assertTrue(any(row["service"] == "网络与 VPN 报修" for row in result["top_services"]))
        self.assertTrue(any(row["assignee"] == "未分配" for row in result["assignee_load"]))
        self.assertIn("over_7d", result["backlog_age"])

    def test_ai_table_mapping_contains_service_sla_and_csat_fields(self):
        for field in (
            "request_type", "service_code", "service_name", "assignment_group",
            "response_due_at", "resolution_due_at", "first_response_at",
            "confirmed_at", "satisfaction_score", "satisfaction_comment",
        ):
            self.assertIn(field, DEFAULT_FIELD_NAMES)

    def test_sla_excludes_withdrawn_and_reopen_rate_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TicketManager(os.path.join(tmp, "tickets.db"), backup_enabled=False)
            knowledge = KnowledgeLoopStore(os.path.join(tmp, "knowledge.db"))
            valid = manager.create_ticket("conv", "IT", "u1", "用户一", "有效工单")
            manager.assign_ticket(valid["ticket_id"], "admin", "管理员", "admin", "管理员")
            manager.resolve_ticket(valid["ticket_id"], "第一次恢复", "admin", "管理员")
            manager.reopen_ticket(valid["ticket_id"], "u1", "用户一", "问题复现")
            manager.resolve_ticket(valid["ticket_id"], "第二次恢复", "admin", "管理员")
            manager.confirm_resolution(valid["ticket_id"], "u1", "用户一", score=5)

            withdrawn = manager.create_ticket("conv", "IT", "u2", "用户二", "撤回工单")
            manager.withdraw_ticket(withdrawn["ticket_id"], "u2", "用户二", "不再需要")
            past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            with manager._lock:
                conn = manager._connect()
                try:
                    conn.execute(
                        "UPDATE tickets SET response_due_at=?,resolution_due_at=? WHERE ticket_id=?",
                        (past, past, withdrawn["ticket_id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
            metrics = DailyOpsDashboard(manager, knowledge).build()["metrics"]

        self.assertEqual(metrics["response_sla_percent"], 100)
        self.assertEqual(metrics["resolution_sla_percent"], 100)
        self.assertEqual(metrics["reopen_rate"], 100)


if __name__ == "__main__":
    unittest.main()
