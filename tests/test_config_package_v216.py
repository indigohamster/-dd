import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from utils.admin_settings import AdminSettingsStore
from utils.admin_server import create_admin_app
from utils.config_package import (
    apply_package,
    create_public_package,
    preview_package,
    validate_package,
)


class ConfigPackageV216Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "config.yaml"
        self.settings = AdminSettingsStore(root / "admin_settings.json")
        self.config_path.write_text(
            yaml.safe_dump({
                "credentials": {"client_id": "ding-id", "client_secret": "ding-secret"},
                "llm": {"enabled": True, "api_key": "llm-secret", "model": "model-a"},
                "spreadsheet": {"node_executable": r"C:\\private\\node.exe"},
                "ticket": {"database_path": "data/tickets.db"},
                "setup": {"completed": True, "completed_at": "today", "purpose": "ops_service"},
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        values = self.settings.load()
        values["connectors"].append({
            "id": "private",
            "type": "http",
            "url": "https://url-user:url-pass@example.invalid/api?api_key=url-secret&tenant=public#oauth-token",
            "auth_value": "connector-secret",
            "headers": '{"Authorization":"Bearer hidden","X-Auth-Token":"header-secret","X-Custom-Metadata":"unknown-secret","X-Tenant":"public"}',
            "body_template": '{"query":"{{input}}","access_token":"body-secret"}',
            "metadata": {"nested": {"accessToken": "nested-secret"}},
        })
        self.settings.save(values)

    def tearDown(self):
        self.temp.cleanup()

    def test_export_is_secret_free_and_portable(self):
        package = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        self.assertEqual(package["config"]["credentials"]["client_secret"], "")
        self.assertEqual(package["config"]["llm"]["api_key"], "")
        self.assertEqual(package["settings"]["connectors"][-1]["auth_value"], "")
        exported_headers = package["settings"]["connectors"][-1]["headers"]
        self.assertNotIn("Bearer hidden", exported_headers)
        self.assertNotIn("header-secret", exported_headers)
        self.assertNotIn("unknown-secret", exported_headers)
        self.assertIn('"X-Auth-Token": ""', exported_headers)
        self.assertIn('"X-Custom-Metadata": ""', exported_headers)
        self.assertIn("X-Tenant", exported_headers)
        self.assertNotIn("body-secret", package["settings"]["connectors"][-1]["body_template"])
        self.assertIn('"access_token": ""', package["settings"]["connectors"][-1]["body_template"])
        exported_connector = package["settings"]["connectors"][-1]
        self.assertNotIn("url-user", exported_connector["url"])
        self.assertNotIn("url-pass", exported_connector["url"])
        self.assertNotIn("url-secret", exported_connector["url"])
        self.assertIn("api_key=", exported_connector["url"])
        self.assertIn("tenant=public", exported_connector["url"])
        self.assertEqual(exported_connector["metadata"]["nested"]["accessToken"], "")
        self.assertEqual(package["config"]["spreadsheet"]["node_executable"], "")
        self.assertEqual(package["config"]["ticket"]["database_path"], "data/tickets.db")
        self.assertNotIn("completed", package["config"].get("setup", {}))
        self.assertFalse(package["manifest"]["includes_secrets"])

    def test_preview_and_apply_preserve_existing_secrets(self):
        package = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        package["config"]["llm"]["model"] = "model-b"
        preview = preview_package(package, self.config_path, self.settings)
        self.assertIn("llm", preview["config_sections"])
        apply_package(package, self.config_path, self.settings)
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["credentials"]["client_secret"], "ding-secret")
        self.assertEqual(saved["llm"]["api_key"], "llm-secret")
        self.assertEqual(saved["llm"]["model"], "model-b")

    def test_invalid_package_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "OpsPilot"):
            validate_package({"manifest": {"schema": "other", "format_version": 1}})

    def test_semantically_invalid_admin_port_is_rejected(self):
        package = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        for bad_port in (0, 65536, True, "8765"):
            with self.subTest(port=bad_port):
                package["config"]["admin_ui"]["port"] = bad_port
                with self.assertRaisesRegex(ValueError, "admin_ui.port"):
                    validate_package(package)

    def test_invalid_settings_container_types_are_rejected(self):
        package = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        for section, value in (
            ("model_routing", []),
            ("response_style", []),
            ("people", {}),
            ("service_catalog", {}),
        ):
            with self.subTest(section=section):
                candidate = create_public_package(
                    self.config_path, self.settings, app_version="V2.16.0"
                )
                candidate["settings"][section] = value
                with self.assertRaisesRegex(ValueError, f"settings.{section}"):
                    validate_package(candidate)

    def test_cross_environment_identities_clear_bound_credentials(self):
        package = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        package["config"]["credentials"]["client_id"] = "other-dingtalk-app"
        package["config"]["llm"].update({
            "provider": "custom",
            "base_url": "https://other-llm.invalid/v1",
            "model": "other-model",
        })
        apply_package(package, self.config_path, self.settings)
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["credentials"]["client_secret"], "")
        self.assertEqual(saved["llm"]["api_key"], "")

    def test_list_secrets_only_follow_the_same_connector_and_model_identity(self):
        values = self.settings.load()
        values["connectors"] = [{
            "id": "cmdb", "type": "http", "url": "https://cmdb.invalid/api",
            "name": "CMDB", "auth_value": "connector-key",
            "headers": '{"X-Auth-Token":"header-key","X-Public":"old"}',
        }]
        values["models"] = [{
            "id": "primary", "provider": "openai_compatible",
            "base_url": "https://models.invalid/v1", "model": "old-model",
            "api_key": "model-key",
        }]
        self.settings.save(values)

        same = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        same["settings"]["connectors"][0]["name"] = "CMDB renamed"
        same["settings"]["models"][0]["model"] = "new-model"
        apply_package(same, self.config_path, self.settings)
        saved = self.settings.load()
        self.assertEqual(saved["connectors"][0]["auth_value"], "connector-key")
        self.assertIn("header-key", saved["connectors"][0]["headers"])
        self.assertEqual(saved["models"][0]["api_key"], "model-key")

        changed = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        changed["settings"]["connectors"][0]["url"] = "https://other-cmdb.invalid/api"
        changed["settings"]["models"][0]["base_url"] = "https://other-models.invalid/v1"
        apply_package(changed, self.config_path, self.settings)
        saved = self.settings.load()
        self.assertFalse(saved["connectors"][0].get("auth_value"))
        self.assertNotIn("header-key", saved["connectors"][0].get("headers", ""))
        self.assertFalse(saved["models"][0].get("api_key"))

    def test_admin_routes_export_preview_and_apply(self):
        app = create_admin_app(self.config_path, self.settings.path)
        app.testing = True
        client = app.test_client()
        exported = client.get("/api/config-package/export")
        self.assertEqual(exported.status_code, 200)
        self.assertNotIn(b"ding-secret", exported.data)
        package = exported.get_json()
        package["config"]["llm"]["model"] = "model-c"
        preview = client.post("/api/config-package/import/preview", json=package)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("llm", preview.get_json()["config_sections"])
        applied = client.post("/api/config-package/import/apply", json=package)
        self.assertEqual(applied.status_code, 200)
        self.assertTrue(applied.get_json()["restart_required"])

    def test_settings_routes_never_echo_connector_credentials_or_headers(self):
        app = create_admin_app(self.config_path, self.settings.path)
        app.testing = True
        client = app.test_client()
        loaded = client.get("/api/settings")
        self.assertEqual(loaded.status_code, 200)
        self.assertNotIn(b"connector-secret", loaded.data)
        self.assertNotIn(b"Bearer hidden", loaded.data)
        self.assertNotIn(b"header-secret", loaded.data)
        self.assertNotIn(b"unknown-secret", loaded.data)
        connectors = loaded.get_json()["connectors"]

        saved = client.put("/api/settings/connectors", json=connectors)
        self.assertEqual(saved.status_code, 200)
        for secret in (b"connector-secret", b"Bearer hidden", b"header-secret", b"unknown-secret", b"body-secret"):
            self.assertNotIn(secret, saved.data)
        persisted = next(
            item for item in self.settings.load()["connectors"] if item.get("id") == "private"
        )
        self.assertEqual(persisted["auth_value"], "connector-secret")
        self.assertIn("header-secret", persisted["headers"])

    def test_import_success_is_reported_even_if_post_snapshot_fails(self):
        app = create_admin_app(self.config_path, self.settings.path)
        app.testing = True
        client = app.test_client()
        package = create_public_package(self.config_path, self.settings, app_version="V2.16.0")
        package["config"]["llm"]["model"] = "updated-model"
        with patch(
            "utils.admin_server.ConfigHistory.record",
            side_effect=[{"version": "before"}, OSError("history disk full")],
        ), patch.object(app.logger, "warning"):
            response = client.post("/api/config-package/import/apply", json=package)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["history_warning"])
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["llm"]["model"], "updated-model")


if __name__ == "__main__":
    unittest.main()
