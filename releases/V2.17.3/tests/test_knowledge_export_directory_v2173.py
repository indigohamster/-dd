import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from utils.admin_server import (
    DEFAULT_KNOWLEDGE_EXPORT_DIR,
    _prepare_knowledge_export_dir,
    _resolve_knowledge_export_dir,
    create_admin_app,
)
from modules.knowledge_loop import KnowledgeLoopStore


class KnowledgeExportPathTests(unittest.TestCase):
    def test_empty_path_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as root:
            resolved, configured = _resolve_knowledge_export_dir("", root)
            self.assertEqual(resolved, Path(root).resolve() / DEFAULT_KNOWLEDGE_EXPORT_DIR)
            self.assertEqual(configured, "")

    def test_relative_path_is_resolved_inside_runtime_root(self):
        with tempfile.TemporaryDirectory() as root:
            resolved, configured = _resolve_knowledge_export_dir("exports/knowledge", root)
            self.assertEqual(resolved, Path(root).resolve() / "exports" / "knowledge")
            self.assertEqual(Path(configured), Path("exports") / "knowledge")

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "不能包含"):
                _resolve_knowledge_export_dir("../outside", root)

    def test_file_cannot_be_used_as_directory(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "not-a-directory"
            target.write_text("file", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "指向文件"):
                _prepare_knowledge_export_dir(str(target), test_write=True)

    def test_write_probe_creates_directory_and_cleans_probe(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "new" / "exports"
            resolved, _ = _prepare_knowledge_export_dir(str(target), test_write=True)
            self.assertTrue(resolved.is_dir())
            self.assertEqual(list(resolved.glob(".opspilot-write-test-*")), [])


class KnowledgeExportDirectoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "config.yaml"
        self.settings_path = root / "admin_settings.json"
        self.database_path = root / "knowledge.db"
        self.default_export = root / "default-exports"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "knowledge_loop": {
                        "enabled": True,
                        "database_path": str(self.database_path),
                        "export_dir": str(self.default_export),
                    }
                },
                allow_unicode=True,
            ),
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

    def test_save_updates_config_and_cached_publish_directory(self):
        first = self.client.get("/api/knowledge/candidates")
        self.assertEqual(first.status_code, 200)
        external_store = KnowledgeLoopStore(self.database_path, self.default_export)
        candidate_id = external_store.create_candidate(
            "自定义目录验证", "验证问题", "执行验证步骤"
        )
        approved = self.client.post(
            f"/api/knowledge/candidates/{candidate_id}/approve"
        )
        self.assertEqual(approved.status_code, 200)
        new_export = Path(self.temp.name) / "custom-exports"

        response = self.client.put(
            "/api/knowledge/export-directory", json={"path": str(new_export)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(new_export.is_dir())
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["knowledge_loop"]["export_dir"], str(new_export.resolve()))
        published = self.client.post(
            f"/api/knowledge/candidates/{candidate_id}/publish"
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(Path(published.get_json()["path"]).parent, new_export.resolve())
        self.assertTrue(Path(published.get_json()["path"]).is_file())

    def test_test_endpoint_rejects_file_path_with_chinese_error(self):
        target = Path(self.temp.name) / "file.txt"
        target.write_text("x", encoding="utf-8")
        response = self.client.post(
            "/api/knowledge/export-directory/test", json={"path": str(target)}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("请选择目录", response.get_json()["message"])

    @mock.patch("utils.admin_server._select_windows_directory")
    def test_select_endpoint_returns_selected_directory(self, selector):
        selected = Path(self.temp.name) / "selected"
        selected.mkdir()
        selector.return_value = str(selected)
        response = self.client.post("/api/knowledge/export-directory/select", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["path"], str(selected.resolve()))

    @mock.patch("utils.admin_server.os.startfile", create=True)
    def test_open_endpoint_uses_supplied_valid_directory(self, startfile):
        target = Path(self.temp.name) / "open-this"
        response = self.client.post(
            "/api/knowledge/exports/open-directory", json={"path": str(target)}
        )
        self.assertEqual(response.status_code, 200)
        startfile.assert_called_once_with(str(target.resolve()))

    def test_open_endpoint_rejects_parent_traversal(self):
        response = self.client.post(
            "/api/knowledge/exports/open-directory", json={"path": "../outside"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能包含", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
