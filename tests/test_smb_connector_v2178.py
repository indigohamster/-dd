import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from handlers.chat_handler import BotMessageHandler
from utils.admin_server import create_admin_app
from utils.file_connector import SMBReadOnlyConnector, normalize_smb_root
from utils.user_security import UserSecurityPolicy


ROOT = Path(__file__).resolve().parents[1]


class SMBConnectorCoreTests(unittest.TestCase):
    def test_normalizes_unc_and_smb_url(self):
        self.assertEqual(normalize_smb_root(r"\\192.168.10.20\公共资料\制度"),
                         r"\\192.168.10.20\公共资料\制度")
        self.assertEqual(normalize_smb_root("smb://nas01/Public/Templates"),
                         r"\\nas01\Public\Templates")

    def test_rejects_traversal_and_embedded_credentials(self):
        for value in (r"\\nas01\Public\..\Secret", "smb://user:pass@nas01/Public"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_smb_root(value)

    def test_search_is_metadata_only_and_honors_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "报价模板.xlsx").write_bytes(b"not-opened")
            (root / "报价说明.txt").write_text("sensitive body", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "历史报价.xlsx").write_bytes(b"history")
            results = SMBReadOnlyConnector._search_tree(
                str(root), "报价", max_depth=3, max_results=10,
                extensions={".xlsx"}, include_directories=True, timeout=2,
            )
            self.assertEqual({item["name"] for item in results}, {"报价模板.xlsx", "历史报价.xlsx"})
            self.assertTrue(all("content" not in item for item in results))
            self.assertTrue(all(item["type"] == "file" for item in results))

    def test_query_cannot_be_a_path(self):
        for query in ("../secret", r"部门\工资", "/etc/passwd"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                SMBReadOnlyConnector._validate_query(query)

    def test_bot_parser_requires_explicit_nas_search(self):
        self.assertEqual(BotMessageHandler._extract_file_connector_query("帮我找NAS里的报价模板"),
                         (True, "报价模板"))
        self.assertEqual(BotMessageHandler._extract_file_connector_query("NAS连接不上"), (False, ""))
        self.assertEqual(BotMessageHandler._extract_file_connector_query("找一下U9操作说明"), (False, ""))

    def test_group_nas_search_is_private(self):
        policy = UserSecurityPolicy({"enabled": True, "group_privacy": True})
        self.assertTrue(policy.group_requires_private({
            "conv_type": "2", "msgtype": "text", "content": "查NAS报价模板",
        }))
        self.assertFalse(policy.group_requires_private({
            "conv_type": "2", "msgtype": "text", "content": "NAS是什么",
        }))


class SMBConnectorAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = root / "config.yaml"
        self.settings = root / "settings.json"
        self.config.write_text("{}\n", encoding="utf-8")
        app = create_admin_app(self.config, self.settings)
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_saves_masked_read_only_smb_connector(self):
        response = self.client.put("/api/settings/connectors", json=[{
            "id": "nas-public", "name": "公共资料", "type": "smb", "enabled": True,
            "url": r"\\192.168.1.20\Public", "username": "reader",
            "auth_value": "do-not-echo", "max_depth": 4, "max_results": 20,
            "allowed_extensions": ["pdf", "xlsx"], "access_scope": "public",
        }])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_data(as_text=True)
        self.assertNotIn("do-not-echo", body)
        item = response.get_json()["data"][0]
        self.assertTrue(item["read_only"])
        self.assertTrue(item["has_auth_value"])
        loaded = self.client.get("/api/settings").get_data(as_text=True)
        self.assertNotIn("do-not-echo", loaded)

    def test_password_is_not_reused_after_username_changes(self):
        base = {"id": "nas-public", "name": "公共资料", "type": "smb", "enabled": True,
                "url": r"\\nas01\Public", "username": "reader-one", "auth_value": "old-secret"}
        self.assertEqual(self.client.put("/api/settings/connectors", json=[base]).status_code, 200)
        changed = dict(base, username="reader-two", auth_value="")
        self.assertEqual(self.client.put("/api/settings/connectors", json=[changed]).status_code, 200)
        saved = json.loads(self.settings.read_text(encoding="utf-8"))["connectors"][0]
        self.assertFalse(saved.get("auth_value"))

    @patch("utils.admin_server.SMBReadOnlyConnector.search")
    def test_execute_allows_search_only(self, search):
        self.client.put("/api/settings/connectors", json=[{
            "id": "nas-public", "name": "公共资料", "type": "smb", "enabled": True,
            "url": r"\\nas01\Public", "max_depth": 4, "max_results": 20,
        }])
        search.return_value = {"read_only": True, "count": 1, "duration_ms": 2,
                               "results": [{"name": "报价.xlsx", "path": r"\\nas01\Public\报价.xlsx"}]}
        result = self.client.post("/api/connectors/execute", json={
            "id": "nas-public", "action": "search", "variables": {"query": "报价"},
        })
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()["result"]["read_only"])
        denied = self.client.post("/api/connectors/execute", json={
            "id": "nas-public", "action": "delete", "variables": {"query": "报价"},
        })
        self.assertEqual(denied.status_code, 400)
        search.assert_called_once()

    def test_frontend_exposes_smb_read_only_configuration(self):
        app_js = (ROOT / "admin_ui" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "admin_ui" / "index.html").read_text(encoding="utf-8")
        for marker in ("SMB / NAS 公共文件夹（只读）", "NAS 共享路径", "queryConnector",
                       "allowed_extensions", "access_scope"):
            self.assertIn(marker, app_js)
        self.assertIn("只读 NAS 公共文件夹", html)


if __name__ == "__main__":
    unittest.main()
