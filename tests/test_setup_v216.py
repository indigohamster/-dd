import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from utils.admin_server import create_admin_app
from utils.admin_settings import AdminSettingsStore
from utils.config import is_runtime_configured, load_config
from utils.module_registry import ModuleRegistry
from utils.setup_service import SetupService


class SetupV216Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "config.yaml"
        self.settings = AdminSettingsStore(root / "admin_settings.json")

    def tearDown(self):
        self.temp.cleanup()

    def draft(self):
        return {
            "purpose": "operations",
            "dingtalk": {
                "client_id": "ding-client",
                "client_secret": "ding-secret-value",
                "agent_id": "agent-1",
            },
            "ai": {"enabled": False, "provider": "none"},
            "modules": {"tickets": True, "knowledge": True, "voice": True},
            "team": [{"name": "管理员", "staff_id": "staff-1", "role": "工单管理员"}],
        }

    def test_missing_config_has_safe_setup_defaults_but_strict_load_fails(self):
        with self.assertRaises(FileNotFoundError):
            load_config(self.config_path)
        config = load_config(self.config_path, allow_incomplete=True)
        self.assertFalse(is_runtime_configured(config))
        self.assertTrue(config["admin_ui"]["enabled"])
        self.assertTrue(config["ticket"]["enabled"])

    def test_admin_port_can_be_overridden_for_zero_config_deployment(self):
        with patch.dict("os.environ", {
            "OPSPILOT_ADMIN_PORT": "19091",
            "OPSPILOT_ADMIN_AUTO_OPEN": "false",
        }):
            config = load_config(self.config_path, allow_incomplete=True)
        self.assertEqual(config["admin_ui"]["port"], 19091)
        self.assertFalse(config["admin_ui"]["auto_open"])

    def test_draft_never_persists_or_returns_secrets(self):
        service = SetupService(self.config_path, self.settings)
        response = service.save_draft(self.draft())
        self.assertTrue(response["dingtalk"]["has_client_secret"])
        self.assertNotIn("client_secret", response["dingtalk"])
        disk = service.draft_path.read_text(encoding="utf-8")
        self.assertNotIn("ding-secret-value", disk)
        status = service.status()
        self.assertNotIn("client_secret", status["draft"]["dingtalk"])

    def test_complete_commits_config_and_team(self):
        service = SetupService(self.config_path, self.settings)
        service.save_draft(self.draft())
        result = service.complete({})
        self.assertTrue(result["ok"])
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["credentials"]["client_secret"], "ding-secret-value")
        self.assertEqual(saved["credentials"]["agent_id"], "agent-1")
        self.assertTrue(saved["setup"]["completed"])
        self.assertFalse(service.draft_path.exists())
        people = self.settings.load()["people"]
        self.assertEqual(people[0]["staff_id"], "staff-1")

    def test_module_toggle_changes_runtime_section(self):
        registry = ModuleRegistry(self.config_path, self.settings)
        item = registry.set_enabled("vision", False)
        self.assertFalse(item["enabled"])
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["vision"]["enabled"])

    def test_spreadsheet_readiness_requires_node_and_artifact_tool(self):
        node = Path(self.temp.name) / "node.exe"
        node.touch()
        modules = Path(self.temp.name) / "node_modules"
        config = {
            "spreadsheet": {
                "enabled": False,
                "node_executable": str(node),
                "node_modules_path": str(modules),
            }
        }
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        registry = ModuleRegistry(self.config_path, self.settings)

        item = next(value for value in registry.list_modules() if value["id"] == "spreadsheet")
        self.assertFalse(item["configured"])
        self.assertFalse(item["available"])
        with self.assertRaisesRegex(ValueError, "artifact-tool"):
            registry.set_enabled("spreadsheet", True)

        (modules / "@oai" / "artifact-tool").mkdir(parents=True)
        item = next(value for value in registry.list_modules() if value["id"] == "spreadsheet")
        self.assertTrue(item["configured"])
        self.assertTrue(registry.test("spreadsheet")["ok"])

    def test_vision_accepts_default_llm_and_requires_route_for_profiles(self):
        base = {
            "enabled": True,
            "provider": "local",
            "base_url": "http://127.0.0.1:11434",
            "model": "vision-model",
            "api_key": "",
        }
        self.config_path.write_text(
            yaml.safe_dump({"llm": base, "vision": {"enabled": False}}),
            encoding="utf-8",
        )
        registry = ModuleRegistry(self.config_path, self.settings)
        item = next(value for value in registry.list_modules() if value["id"] == "vision")
        self.assertTrue(item["configured"])
        self.assertTrue(item["available"])
        self.assertTrue(registry.set_enabled("vision", True)["enabled"])

        settings = self.settings.load()
        settings["models"] = [{
            "id": "bad-cloud", "enabled": True,
            "provider": "openai_compatible",
            "base_url": "https://vision.invalid/v1", "model": "vision-model",
            "api_key": "",
        }]
        settings["model_routing"] = {"vision": "bad-cloud", "fallback": ""}
        self.settings.save(settings)
        self.assertFalse(registry.is_configured("vision"))

        # A disabled default endpoint must not make an unrelated, unrouted
        # profile appear ready.
        self.config_path.write_text(
            yaml.safe_dump({"llm": {**base, "enabled": False}, "vision": {"enabled": False}}),
            encoding="utf-8",
        )
        settings = self.settings.load()
        settings["models"] = [{
            "id": "profile-vision", "enabled": True, "provider": "local",
            "base_url": "http://127.0.0.1:8000", "model": "profile-model",
        }]
        settings["model_routing"] = {"vision": "", "fallback": ""}
        self.settings.save(settings)
        self.assertFalse(registry.is_configured("vision"))
        settings["model_routing"]["vision"] = "profile-vision"
        self.settings.save(settings)
        self.assertTrue(registry.is_configured("vision"))
        settings["model_routing"] = {"vision": "", "fallback": "", "fallback_order": ["profile-vision"]}
        self.settings.save(settings)
        self.assertTrue(registry.is_configured("vision"))

    def test_setup_can_complete_with_default_ai_as_vision_route(self):
        service = SetupService(self.config_path, self.settings)
        draft = self.draft()
        draft["ai"] = {
            "enabled": True,
            "provider": "local",
            "base_url": "http://127.0.0.1:11434",
            "model": "vision-model",
            "api_key": "",
        }
        draft["modules"]["vision"] = True
        service.save_draft(draft)
        result = service.complete({})
        self.assertTrue(result["ok"], result)
        saved = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["vision"]["enabled"])

    def test_setup_rejects_aitable_when_tickets_are_disabled_in_same_draft(self):
        executable = Path(self.temp.name) / "dws.exe"
        executable.touch()
        settings = self.settings.load()
        settings["aitable_ticket_sync"].update({
            "enabled": False,
            "dws_executable": str(executable),
            "base_id": "base-example",
            "table_id": "table-example",
        })
        self.settings.save(settings)
        service = SetupService(self.config_path, self.settings)
        draft = self.draft()
        draft["modules"].update({"tickets": False, "aitable": True})
        service.save_draft(draft)
        result = service.complete({})
        self.assertFalse(result["ok"])
        self.assertIn("module:aitable", result["missing"])
        self.assertFalse(self.config_path.exists())

    def test_flask_setup_contract_works_without_config_file(self):
        app = create_admin_app(self.config_path, self.settings.path)
        app.testing = True
        client = app.test_client()
        status = client.get("/api/setup/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.get_json()["setup_required"])
        saved = client.put("/api/setup/draft", json=self.draft())
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("client_secret", saved.get_json()["draft"]["dingtalk"])
        completed = client.post("/api/setup/complete", json={})
        self.assertEqual(completed.status_code, 200)
        self.assertTrue(completed.get_json()["completed"])


if __name__ == "__main__":
    unittest.main()
