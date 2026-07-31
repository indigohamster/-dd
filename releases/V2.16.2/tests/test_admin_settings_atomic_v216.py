import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from utils.admin_settings import AdminSettingsStore


class AdminSettingsAtomicV216Tests(unittest.TestCase):
    def test_stores_for_same_path_share_process_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_settings.json"
            first = AdminSettingsStore(path)
            second = AdminSettingsStore(path.parent / "." / path.name)

            self.assertIs(first._lock, second._lock)
            self.assertEqual(first.path, second.path)

    def test_save_uses_fsynced_unique_temp_and_cleans_failed_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "admin_settings.json"
            store = AdminSettingsStore(path)
            store.save({"version": 1})
            original = path.read_bytes()

            real_fsync = os.fsync
            with patch("utils.admin_settings.os.fsync", wraps=real_fsync) as fsync_mock:
                with patch("utils.admin_settings.os.replace", side_effect=OSError("replace failed")):
                    with self.assertRaises(OSError):
                        store.save({"version": 2})

            fsync_mock.assert_called_once()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_concurrent_multi_instance_load_and_save_never_corrupts_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_settings.json"
            stores = [AdminSettingsStore(path) for _ in range(12)]
            stores[0].save({"writer": -1, "version": 0, "marker": "-1:0"})
            barrier = threading.Barrier(len(stores))

            def assert_consistent(saved):
                self.assertEqual(saved["marker"], f"{saved['writer']}:{saved['version']}")

            def writer(index):
                barrier.wait()
                for version in range(80):
                    stores[index].save({
                        "writer": index,
                        "version": version,
                        "marker": f"{index}:{version}",
                    })
                    assert_consistent(stores[(index + 1) % len(stores)].load())

            def reader(index):
                barrier.wait()
                for _ in range(160):
                    assert_consistent(stores[index].load())

            jobs = []
            with ThreadPoolExecutor(max_workers=len(stores)) as executor:
                for index in range(8):
                    jobs.append(executor.submit(writer, index))
                for index in range(8, len(stores)):
                    jobs.append(executor.submit(reader, index))
                for job in jobs:
                    job.result(timeout=30)

            final = json.loads(path.read_text(encoding="utf-8"))
            assert_consistent(final)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_concurrent_mutations_do_not_lose_unrelated_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "admin_settings.json"
            stores = [AdminSettingsStore(path) for _ in range(8)]
            stores[0].save({"counters": {str(index): 0 for index in range(len(stores))}})

            def increment(index):
                for _ in range(60):
                    def update(data):
                        counters = data.setdefault("counters", {})
                        counters[str(index)] = counters.get(str(index), 0) + 1
                    stores[index].mutate(update)

            with ThreadPoolExecutor(max_workers=len(stores)) as executor:
                jobs = [executor.submit(increment, index) for index in range(len(stores))]
                for job in jobs:
                    job.result(timeout=30)

            counters = stores[0].load()["counters"]
            self.assertEqual(counters, {str(index): 60 for index in range(len(stores))})


if __name__ == "__main__":
    unittest.main()
