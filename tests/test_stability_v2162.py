import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from utils.admin_server import start_admin_server
from utils.observability import ObservabilityStore
from utils.task_queue import PersistentTaskQueue


def wait_for_status(queue, task_id, statuses, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = queue.get_task(task_id)
        if task and task["status"] in statuses:
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在期限内进入 {statuses}: {queue.get_task(task_id)}")


class PersistentTaskQueueStabilityTests(unittest.TestCase):
    def test_fast_tasks_do_not_leave_completed_futures(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = PersistentTaskQueue(Path(tmp) / "tasks.db", max_workers=4)
            try:
                tasks = [queue.submit("fast", "tester", f"fast-{index}", lambda progress: "ok") for index in range(80)]
                for task in tasks:
                    wait_for_status(queue, task["task_id"], {"succeeded"})
                deadline = time.time() + 2
                while time.time() < deadline and queue._futures:
                    time.sleep(0.01)
                self.assertEqual({}, queue._futures)
            finally:
                queue.shutdown(wait=True)

    def test_running_task_cancel_request_wins_even_without_progress_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = PersistentTaskQueue(Path(tmp) / "tasks.db", max_workers=1)
            started = threading.Event()
            release = threading.Event()

            def work(progress):
                started.set()
                release.wait(3)
                return "should not become succeeded"

            try:
                task = queue.submit("slow", "tester", "slow", work)
                self.assertTrue(started.wait(2))
                self.assertTrue(queue.cancel(task["task_id"]))
                release.set()
                final = wait_for_status(queue, task["task_id"], {"cancelled"})
                self.assertEqual("cancelled", final["status"])
            finally:
                release.set()
                queue.shutdown(wait=True)

    def test_shutdown_is_idempotent_and_rejects_new_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = PersistentTaskQueue(Path(tmp) / "tasks.db", max_workers=1)
            queue.shutdown(wait=False)
            queue.shutdown(wait=False)
            with self.assertRaisesRegex(RuntimeError, "已关闭"):
                queue.submit("late", "tester", "late", lambda progress: "never")

    def test_shutdown_interrupts_queued_work_without_running_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = PersistentTaskQueue(Path(tmp) / "tasks.db", max_workers=1)
            started = threading.Event()
            release = threading.Event()
            queued_ran = threading.Event()

            def blocker(progress):
                started.set()
                release.wait(3)
                return "blocker done"

            first = queue.submit("slow", "tester", "blocker", blocker)
            self.assertTrue(started.wait(2))
            second = queue.submit(
                "queued", "tester", "queued",
                lambda progress: queued_ran.set() or "unexpected",
            )
            queue.shutdown(wait=False)
            release.set()
            wait_for_status(queue, first["task_id"], {"cancelled"})
            final_second = wait_for_status(queue, second["task_id"], {"interrupted"})
            self.assertEqual("interrupted", final_second["status"])
            self.assertFalse(queued_ran.is_set())


class NotificationRetryStabilityTests(unittest.TestCase):
    def test_only_one_concurrent_retry_can_claim_failed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObservabilityStore(Path(tmp) / "observability.db")
            delivery_id = store.create_delivery(
                {"ticket_id": "TKT-STABILITY", "creator_id": "user-1"},
                "user-1", "updated",
            )
            store.update_delivery(delivery_id, "failed", "temporary failure")

            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(lambda _: store.claim_delivery_for_retry(delivery_id), range(24)))

            self.assertEqual(1, sum(bool(value) for value in results))
            self.assertEqual("retrying", store.get_delivery(delivery_id)["status"])
            self.assertEqual(1, store.summary()["notifications_pending"])


class WaitressAdminServerTests(unittest.TestCase):
    def test_admin_server_uses_waitress_and_can_stop_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            settings_path = Path(tmp) / "admin_settings.json"
            config_path.write_text("{}\n", encoding="utf-8")
            thread = start_admin_server(
                config_path=config_path,
                settings_path=settings_path,
                host="127.0.0.1",
                port=0,
                auto_open=False,
                threads=4,
            )
            server = thread.opspilot_server
            url = f"http://127.0.0.1:{server.effective_port}/api/setup/status"
            try:
                response = None
                for _ in range(50):
                    try:
                        response = requests.get(url, timeout=1)
                        break
                    except requests.RequestException:
                        time.sleep(0.02)
                self.assertIsNotNone(response)
                self.assertEqual(200, response.status_code)
                self.assertIn("OpsPilot/V2.17.7", response.headers.get("Server", ""))
                with ThreadPoolExecutor(max_workers=12) as executor:
                    statuses = list(executor.map(
                        lambda _: requests.get(url, timeout=3).status_code,
                        range(48),
                    ))
                self.assertEqual([200] * 48, statuses)
            finally:
                thread.opspilot_stop()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
