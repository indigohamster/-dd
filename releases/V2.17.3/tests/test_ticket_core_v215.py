import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

from modules.ticket.ticket_manager import (
    STATUS_CLOSED,
    STATUS_RESOLVED,
    TicketManager,
)


class TicketCoreV215Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = TicketManager(
            os.path.join(self.tmp.name, "tickets.db"), backup_enabled=False,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, title="VPN 无法连接", **kwargs):
        return self.manager.create_ticket(
            "conv", "IT", "user-1", "用户一", title,
            category="网络", **kwargs,
        )

    def test_creation_snapshots_service_group_and_priority_sla(self):
        ticket = self.create(
            priority="urgent", request_type="major_incident",
            service_code="NET-001", service_name="网络故障",
            assignment_group="网络组",
        )
        self.assertEqual(ticket["request_type"], "major_incident")
        self.assertEqual(ticket["assignment_group"], "网络组")
        self.assertEqual(ticket["sla_status"], "normal")
        created = datetime.strptime(ticket["created_at"], "%Y-%m-%d %H:%M:%S")
        response_due = datetime.strptime(ticket["response_due_at"], "%Y-%m-%d %H:%M:%S")
        resolution_due = datetime.strptime(ticket["resolution_due_at"], "%Y-%m-%d %H:%M:%S")
        self.assertAlmostEqual((response_due - created).total_seconds(), 1800, delta=2)
        self.assertAlmostEqual((resolution_due - created).total_seconds(), 7200, delta=2)

    def test_first_human_action_sets_first_response_once(self):
        ticket = self.create()
        commented = self.manager.add_comment(
            ticket["ticket_id"], "已联系用户", "admin", "管理员",
        )
        first = commented["first_response_at"]
        self.assertTrue(first)
        assigned = self.manager.assign_ticket(
            ticket["ticket_id"], "admin", "管理员", "admin", "管理员",
        )
        self.assertEqual(assigned["first_response_at"], first)

    def test_creator_comment_does_not_satisfy_response_sla(self):
        ticket = self.create()
        supplemented = self.manager.add_comment(
            ticket["ticket_id"], "补充错误码 691", "user-1", "用户一",
            visibility="public",
        )
        self.assertIsNone(supplemented["first_response_at"])
        responded = self.manager.add_comment(
            ticket["ticket_id"], "已收到，正在排查", "admin", "管理员",
            visibility="public",
        )
        self.assertTrue(responded["first_response_at"])

    def test_assigned_ticket_can_be_transferred(self):
        ticket = self.create()
        self.manager.assign_ticket(
            ticket["ticket_id"], "admin-1", "管理员一", "lead", "值班主管",
        )
        transferred = self.manager.assign_ticket(
            ticket["ticket_id"], "admin-2", "管理员二", "lead", "值班主管",
        )
        self.assertIsNotNone(transferred)
        self.assertEqual(transferred["assignee_id"], "admin-2")

    def test_concurrent_creation_generates_unique_ticket_ids(self):
        ids, errors, lock = [], [], threading.Lock()

        def create_one(index):
            try:
                ticket = self.create(title=f"并发报修 {index}")
                with lock:
                    ids.append(ticket["ticket_id"])
            except Exception as exc:  # pragma: no cover - assertion reports details
                with lock:
                    errors.append(str(exc))

        threads = [threading.Thread(target=create_one, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)

    def test_two_manager_instances_allocate_unique_ticket_ids(self):
        second = TicketManager(self.manager.db_path, backup_enabled=False)
        ids, errors, lock = [], [], threading.Lock()

        def create_one(manager, index):
            try:
                ticket = manager.create_ticket(
                    "conv", "IT", f"user-{index}", "用户", f"双实例并发 {index}",
                )
                with lock:
                    ids.append(ticket["ticket_id"])
            except Exception as exc:  # pragma: no cover - assertion reports details
                with lock:
                    errors.append(str(exc))

        threads = [
            threading.Thread(target=create_one, args=(self.manager if i % 2 else second, i))
            for i in range(30)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)

    def test_non_numeric_import_id_does_not_break_daily_sequence(self):
        today = datetime.now().strftime("%Y%m%d")
        result = self.manager.import_tickets([
            {
                "ticket_id": f"TKT-{today}-ZZZ", "title": "外部编号",
                "creator_id": "u2", "creator_nick": "用户二",
            },
            {
                "ticket_id": f"TKT-{today}-99999", "title": "大流水号",
                "creator_id": "u3", "creator_nick": "用户三",
            },
        ])
        self.assertEqual(result["imported"], 2)
        created = self.create(title="正常建单")
        self.assertEqual(created["ticket_id"], f"TKT-{today}-100000")

    def test_old_logs_keep_public_status_timeline_but_hide_legacy_comments(self):
        ticket = self.create()
        with self.manager._lock:
            conn = self.manager._connect()
            try:
                conn.execute("DROP TABLE ticket_log")
                conn.execute("""CREATE TABLE ticket_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
                    action TEXT NOT NULL, operator_id TEXT NOT NULL, operator_nick TEXT NOT NULL,
                    detail TEXT DEFAULT '', created_at TEXT NOT NULL
                )""")
                for action in ("create", "resolve", "comment"):
                    conn.execute(
                        "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (ticket["ticket_id"], action, "admin", "管理员", action,
                         "2026-07-20 10:00:00"),
                    )
                conn.commit()
            finally:
                conn.close()
        migrated = TicketManager(self.manager.db_path, backup_enabled=False)
        public_actions = {
            row["action"] for row in migrated.get_ticket_logs(
                ticket["ticket_id"], include_internal=False,
            )
        }
        self.assertIn("create", public_actions)
        self.assertIn("resolve", public_actions)
        self.assertNotIn("comment", public_actions)

    def test_visibility_backfill_is_idempotent_for_preview_schema(self):
        ticket = self.create()
        with self.manager._lock:
            conn = self.manager._connect()
            try:
                conn.execute(
                    "UPDATE ticket_log SET visibility='internal' WHERE ticket_id=?",
                    (ticket["ticket_id"],),
                )
                conn.execute(
                    "INSERT INTO ticket_log(ticket_id,action,operator_id,operator_nick,detail,visibility,created_at) "
                    "VALUES(?,'resolve','admin','管理员','已处理','internal','2026-07-20 10:00:00')",
                    (ticket["ticket_id"],),
                )
                conn.commit()
            finally:
                conn.close()
        migrated = TicketManager(self.manager.db_path, backup_enabled=False)
        visible = migrated.get_ticket_logs(ticket["ticket_id"], include_internal=False)
        self.assertEqual({row["action"] for row in visible}, {"create", "resolve"})

    def test_creator_withdrawal_is_closed_but_not_counted_as_resolved(self):
        ticket = self.create(title="不再需要的软件安装")
        self.assertIsNone(
            self.manager.withdraw_ticket(ticket["ticket_id"], "other", "其他用户"),
        )
        withdrawn = self.manager.withdraw_ticket(
            ticket["ticket_id"], "user-1", "用户一", "已找到替代软件",
        )
        self.assertEqual(withdrawn["status"], STATUS_CLOSED)
        self.assertEqual(withdrawn["user_confirmation"], "withdrawn")
        self.assertIsNone(withdrawn["resolved_at"])
        self.assertEqual(self.manager.get_stats()["resolved"], 0)
        self.assertTrue(any(
            row["action"] == "withdraw"
            for row in self.manager.get_ticket_logs(ticket["ticket_id"], include_internal=False)
        ))

    def test_archive_requires_confirmed_closed_solution(self):
        ticket = self.create(title="不得提前归档")
        self.assertIsNone(self.manager.archive_as_experience(ticket["ticket_id"]))
        self.manager.resolve_ticket(ticket["ticket_id"], "重建配置", "admin", "管理员")
        self.assertIsNone(self.manager.archive_as_experience(ticket["ticket_id"]))
        self.manager.confirm_resolution(ticket["ticket_id"], "user-1", "用户一", score=5)
        archived = self.manager.archive_as_experience(ticket["ticket_id"], "admin", "管理员")
        self.assertEqual(archived["is_experience"], 1)

    def test_reopen_history_survives_second_resolution(self):
        ticket = self.create(title="需要二次处理")
        self.manager.resolve_ticket(ticket["ticket_id"], "第一次方案", "admin", "管理员")
        reopened = self.manager.reopen_ticket(ticket["ticket_id"], "user-1", "用户一", "仍未恢复")
        self.assertEqual(reopened["status"], "in_progress")
        self.assertIsNone(reopened["resolved_at"])
        self.assertTrue(any(
            row["action"] == "reopen" for row in self.manager.get_ticket_logs(ticket["ticket_id"])
        ))
        self.manager.resolve_ticket(ticket["ticket_id"], "第二次方案", "admin", "管理员")
        self.manager.confirm_resolution(ticket["ticket_id"], "user-1", "用户一")
        self.assertEqual(self.manager.get_reopen_metrics(), {"reopened": 1, "resolved": 1})

    def test_overdue_uses_resolution_deadline_without_100_row_cap(self):
        ids = []
        past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        for index in range(105):
            ticket = self.create(title=f"批量故障 {index}")
            ids.append(ticket["ticket_id"])
        with self.manager._lock:
            conn = self.manager._connect()
            try:
                conn.executemany(
                    "UPDATE tickets SET resolution_due_at=? WHERE ticket_id=?",
                    [(past, ticket_id) for ticket_id in ids],
                )
                conn.commit()
            finally:
                conn.close()
        overdue = self.manager.get_overdue_tickets()
        self.assertEqual(len(overdue), 105)
        self.assertEqual({row["ticket_id"] for row in overdue}, set(ids))
        self.assertTrue(all(row["sla_status"] == "breached" for row in overdue))

    def test_sla_decorates_at_risk_met_and_breached(self):
        at_risk = self.create(title="即将超时")
        due_soon = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with self.manager._lock:
            conn = self.manager._connect()
            try:
                conn.execute(
                    "UPDATE tickets SET first_response_at=created_at,resolution_due_at=? WHERE ticket_id=?",
                    (due_soon, at_risk["ticket_id"]),
                )
                conn.commit()
            finally:
                conn.close()
        self.assertEqual(self.manager.get_ticket(at_risk["ticket_id"])["sla_status"], "at_risk")

        met = self.create(title="按时解决", resolution_hours=2)
        self.manager.resolve_ticket(met["ticket_id"], "已恢复", "admin", "管理员")
        self.assertEqual(self.manager.get_ticket(met["ticket_id"])["sla_status"], "met")

        breached = self.create(title="超时解决")
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        with self.manager._lock:
            conn = self.manager._connect()
            try:
                conn.execute(
                    "UPDATE tickets SET resolution_due_at=? WHERE ticket_id=?",
                    (past, breached["ticket_id"]),
                )
                conn.commit()
            finally:
                conn.close()
        self.manager.resolve_ticket(breached["ticket_id"], "迟到恢复", "admin", "管理员")
        self.assertEqual(self.manager.get_ticket(breached["ticket_id"])["sla_status"], "breached")

    def test_creator_confirms_resolution_and_feedback_is_idempotent(self):
        ticket = self.create()
        self.manager.resolve_ticket(ticket["ticket_id"], "重建配置", "admin", "管理员")
        self.assertIsNone(self.manager.confirm_resolution(ticket["ticket_id"], "other", score=5))
        self.assertIsNone(self.manager.confirm_resolution(ticket["ticket_id"], "user-1", score=6))
        confirmed = self.manager.confirm_resolution(
            ticket["ticket_id"], "user-1", "用户一", score="5", comment="恢复正常",
        )
        self.assertEqual(confirmed["status"], STATUS_CLOSED)
        self.assertEqual(confirmed["user_confirmation"], "confirmed")
        self.assertEqual(confirmed["satisfaction_score"], 5)
        self.assertTrue(confirmed["confirmed_at"])
        self.assertEqual(len(self.manager.list_feedback(ticket["ticket_id"])), 1)
        again = self.manager.confirm_resolution(ticket["ticket_id"], "user-1", score=4)
        self.assertEqual(again["satisfaction_score"], 5)
        self.assertEqual(len(self.manager.list_feedback(ticket["ticket_id"])), 1)
        public_logs = self.manager.get_ticket_logs(
            ticket["ticket_id"], include_internal=False,
        )
        self.assertTrue(any(row["action"] == "confirm" for row in public_logs))

    def test_combined_query_aliases_filters_and_pages(self):
        first = self.create(
            title="财务 VPN 中断", priority="high", request_type="incident",
            service_code="VPN", service_name="VPN 服务", assignment_group="网络组",
        )
        self.manager.assign_ticket(first["ticket_id"], "admin", "管理员", "admin", "管理员")
        self.create(
            title="申请 VPN 权限", priority="low", request_type="service_request",
            service_code="VPN", service_name="VPN 服务",
        )
        result = self.manager.query_tickets(
            keyword="VPN", status=["assigned"], priority="high", category="网络",
            assignee="admin", service_code="VPN", request_type="incident",
            assignment_group="网络组", page=1, page_size=1,
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["ticket_id"], first["ticket_id"])
        unassigned = self.manager.query_tickets(service_item="VPN", unassigned=True)
        self.assertEqual(unassigned["total"], 1)
        self.manager.resolve_ticket(first["ticket_id"], "刷新网关令牌", "admin", "管理员")
        by_resolution = self.manager.query_tickets(keyword="网关令牌")
        self.assertEqual(by_resolution["items"][0]["ticket_id"], first["ticket_id"])

    def test_import_accepts_v215_fields_and_feedback(self):
        result = self.manager.import_tickets([{
            "ticket_id": "TKT-IMPORT-V215", "title": "账号申请",
            "creator_id": "u2", "creator_nick": "用户二", "status": "closed",
            "priority": "low", "ticket_type": "service_request",
            "service_item": "ACCOUNT", "service_name": "账号服务",
            "assignment_group": "应用组", "user_confirmation": "confirmed",
            "satisfaction_score": "4", "satisfaction_comment": "处理及时",
        }])
        self.assertEqual(result["imported"], 1)
        ticket = self.manager.get_ticket("TKT-IMPORT-V215")
        self.assertEqual(ticket["request_type"], "service_request")
        self.assertEqual(ticket["assignment_group"], "应用组")
        self.assertEqual(self.manager.get_ticket_feedback(ticket["ticket_id"])["score"], 4)


if __name__ == "__main__":
    unittest.main()
