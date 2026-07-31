import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from handlers.chat_handler import BotMessageHandler
from utils.admin_settings import AdminSettingsStore
from utils.admin_server import create_admin_app
from utils.deepseek_client import DeepSeekClient, llm_config_is_usable
from utils.model_router import ModelRouter


class LocalAIV216Tests(unittest.TestCase):
    def test_dingtalk_api_compatibility_alias_tracks_canonical_client(self):
        handler = object.__new__(BotMessageHandler)
        first = object()
        second = object()
        handler.dingtalk_api = first
        self.assertIs(handler.dtalk_api, first)
        handler.dtalk_api = second
        self.assertIs(handler.dingtalk_api, second)

    def test_local_provider_can_run_without_api_key(self):
        config = {
            "enabled": True,
            "provider": "local",
            "base_url": "http://127.0.0.1:8000",
            "model": "company-model",
            "api_key": "",
        }
        self.assertTrue(llm_config_is_usable(config))
        client = DeepSeekClient(
            api_key="", base_url=config["base_url"], model=config["model"]
        )
        self.assertNotIn("Authorization", client._session.headers)

    def test_cloud_provider_still_requires_key(self):
        config = {
            "enabled": True,
            "provider": "openai_compatible",
            "base_url": "https://example.invalid",
            "model": "cloud-model",
            "api_key": "",
        }
        self.assertFalse(llm_config_is_usable(config))
        config["api_key"] = "secret"
        self.assertTrue(llm_config_is_usable(config))
        client = DeepSeekClient(
            api_key="secret", base_url=config["base_url"], model=config["model"]
        )
        self.assertEqual(client._session.headers["Authorization"], "Bearer secret")

    def test_model_router_uses_default_llm_when_no_profile_is_routed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AdminSettingsStore(Path(temp_dir) / "settings.json")
            router = ModelRouter({
                "enabled": True,
                "provider": "local",
                "base_url": "http://127.0.0.1:11434",
                "model": "vision-model",
                "api_key": "",
            }, settings_store=store)
            client = Mock()
            client.chat.return_value = "识别结果"
            with patch.object(router, "_client", return_value=client) as create_client:
                answer, model_id = router.chat([{"role": "user", "content": "image"}], "vision")
            self.assertEqual((answer, model_id), ("识别结果", "default"))
            create_client.assert_called_once_with({})

    def test_explicit_unusable_vision_route_does_not_silently_use_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AdminSettingsStore(Path(temp_dir) / "settings.json")
            settings = store.load()
            settings["models"] = [{
                "id": "bad-vision", "enabled": True,
                "provider": "openai_compatible",
                "base_url": "https://vision.invalid/v1",
                "model": "vision-model", "api_key": "",
            }]
            settings["model_routing"]["vision"] = "bad-vision"
            store.save(settings)
            router = ModelRouter({
                "enabled": True, "provider": "local",
                "base_url": "http://127.0.0.1:11434",
                "model": "default-model", "api_key": "",
            }, settings_store=store)
            with patch.object(router, "_client") as create_client:
                with self.assertRaisesRegex(Exception, "不可用"):
                    router.chat([{"role": "user", "content": "image"}], "vision")
            create_client.assert_not_called()

    def test_disabled_voice_module_stops_before_download(self):
        handler = object.__new__(BotMessageHandler)
        handler.voice_enabled = False
        handler.reply_text = Mock()
        result = BotMessageHandler._handle_file_message(
            handler,
            object(),
            {"file_info": {"type": "voice", "download_code": "download-code"}},
        )
        self.assertEqual(result[1], "OK")
        handler.reply_text.assert_called_once()

    def test_disabled_ticket_module_does_not_query_or_upload_plain_attachment(self):
        handler = object.__new__(BotMessageHandler)
        handler.voice_enabled = True
        handler.vision_enabled = False
        handler.ticket_enabled = False
        handler.reply_text = Mock()
        handler.logger = Mock()
        handler.ticket_mgr = Mock()
        handler.spreadsheet_service = Mock(enabled=False)
        handler.vision_service = Mock()
        handler.dtalk_api = Mock()

        result = BotMessageHandler._handle_file_message(
            handler,
            object(),
            {
                "sender_id": "user-1",
                "file_info": {
                    "type": "file",
                    "download_code": "download-code",
                    "file_name": "readme.txt",
                },
            },
        )

        self.assertEqual(result[1], "OK")
        handler.ticket_mgr.list_tickets_by_user.assert_not_called()
        handler.dtalk_api.download_message_file.assert_not_called()
        handler.dtalk_api.upload_file.assert_not_called()
        handler.reply_text.assert_called_once()

    def test_downloaded_attachment_is_deleted_when_ticket_lookup_fails(self):
        handler = object.__new__(BotMessageHandler)
        handler.voice_enabled = True
        handler.vision_enabled = False
        handler.ticket_enabled = True
        handler.reply_text = Mock()
        handler.logger = Mock()
        handler.spreadsheet_service = Mock(enabled=False)
        handler.dtalk_api = Mock()
        handler.ticket_mgr = Mock()
        handler.ticket_mgr.list_tickets_by_user.side_effect = RuntimeError("database unavailable")
        handler._delete_local_attachment = Mock()

        result = BotMessageHandler._handle_file_message(
            handler,
            object(),
            {
                "sender_id": "user-1",
                "file_info": {
                    "type": "file",
                    "download_code": "download-code",
                    "file_name": "diagnostic.log",
                },
            },
        )

        self.assertEqual(result[1], "OK")
        handler.dtalk_api.download_message_file.assert_called_once()
        downloaded_path = handler.dtalk_api.download_message_file.call_args.args[1]
        handler._delete_local_attachment.assert_called_with(downloaded_path)

    def test_image_linked_to_recent_ticket_does_not_start_second_ticket_flow(self):
        handler = object.__new__(BotMessageHandler)
        handler.voice_enabled = True
        handler.vision_enabled = True
        handler.ticket_enabled = True
        handler.reply_text = Mock()
        handler.logger = Mock()
        handler.spreadsheet_service = Mock(enabled=False)
        handler.vision_service = Mock()
        handler.vision_service.analyze.return_value = ("识别到错误码 691", "vision-model")
        handler.dtalk_api = Mock()
        handler.ticket_mgr = Mock()
        handler.ticket_mgr.list_tickets_by_user.return_value = [{
            "ticket_id": "TKT-1",
            "title": "VPN 无法连接",
            "status": "open",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }]
        handler._add_attachment_to_ticket = Mock()
        handler._delete_local_attachment = Mock()
        handler._dispatch_text_message = Mock()

        result = BotMessageHandler._handle_file_message(
            handler,
            object(),
            {
                "sender_id": "user-1",
                "file_info": {
                    "type": "image",
                    "download_code": "download-code",
                    "file_name": "error.png",
                },
            },
        )

        self.assertEqual(result[1], "OK")
        handler._add_attachment_to_ticket.assert_called_once()
        handler._dispatch_text_message.assert_not_called()

    def test_feedback_escalation_consumes_pending_attachment(self):
        handler = object.__new__(BotMessageHandler)
        ticket = {
            "ticket_id": "TKT-1", "status": "open", "title": "VPN 故障",
            "category": "网络问题", "priority": "medium",
        }
        handler.ticket_mgr = Mock()
        handler.ticket_mgr.create_ticket.return_value = ticket
        handler.ticket_mgr.get_ticket.return_value = ticket
        handler._judge_priority = Mock(return_value="medium")
        handler._consume_pending_attachment = Mock()
        handler.knowledge_loop = Mock()
        handler.logger = Mock()
        handler._notify_admin_feedback = Mock()
        handler.reply_markdown = Mock()

        info = {
            "conv_id": "conv-1", "conv_title": "IT", "sender_id": "user-1",
            "sender_nick": "员工", "staff_id": "staff-1", "content": "没有解决",
        }
        BotMessageHandler._handle_feedback_no(
            handler, object(), info, {"question": "VPN 无法连接", "event_id": 1}
        )

        handler._consume_pending_attachment.assert_called_once_with(ticket, info)

    def test_disabled_ticket_module_does_not_auto_escalate(self):
        handler = object.__new__(BotMessageHandler)
        handler.ticket_enabled = False
        handler.reply_text = Mock()
        handler._handle_ticket = Mock()

        BotMessageHandler._auto_escalate_issue(
            handler,
            object(),
            {"content": "VPN 仍然不可用"},
            {"reason": "连续排障失败", "summary": "VPN 仍然不可用"},
        )

        handler._handle_ticket.assert_not_called()
        handler.reply_text.assert_called_once()

    def test_admin_can_switch_to_keyless_local_endpoint_without_leaking_old_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            settings_path = root / "settings.json"
            config_path.write_text(yaml.safe_dump({
                "credentials": {"client_id": "id", "client_secret": "secret"},
                "llm": {
                    "enabled": True,
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-chat",
                    "api_key": "old-cloud-key",
                },
            }, sort_keys=False), encoding="utf-8")
            app = create_admin_app(config_path, settings_path)
            app.testing = True
            client = app.test_client()
            saved = client.put("/api/llm", json={
                "enabled": True,
                "provider": "custom",
                "base_url": "http://127.0.0.1:8000",
                "model": "company-model",
            })
            self.assertEqual(saved.status_code, 200)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["llm"]["api_key"], "")
            response = Mock()
            response.raise_for_status.return_value = None
            with patch("utils.admin_server.requests.post", return_value=response) as request_post:
                tested = client.post("/api/llm/test", json={
                    "provider": "custom",
                    "base_url": "http://127.0.0.1:8000",
                    "model": "company-model",
                })
            self.assertEqual(tested.status_code, 200)
            self.assertNotIn("Authorization", request_post.call_args.kwargs["headers"])

    def test_llm_test_never_reuses_key_after_provider_or_endpoint_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump({
                "credentials": {"client_id": "id", "client_secret": "secret"},
                "llm": {
                    "enabled": True,
                    "provider": "deepseek",
                    "base_url": "https://old-llm.invalid/v1",
                    "model": "cloud-model",
                    "api_key": "old-cloud-key",
                },
            }, sort_keys=False), encoding="utf-8")
            app = create_admin_app(config_path, root / "settings.json")
            app.testing = True
            client = app.test_client()
            with patch("utils.admin_server.requests.post") as request_post:
                endpoint_changed = client.post("/api/llm/test", json={
                    "provider": "deepseek",
                    "base_url": "https://new-llm.invalid/v1",
                    "model": "cloud-model",
                })
                provider_changed = client.post("/api/llm/test", json={
                    "provider": "openai_compatible",
                    "base_url": "https://old-llm.invalid/v1",
                    "model": "cloud-model",
                })
            self.assertEqual(endpoint_changed.status_code, 400)
            self.assertEqual(provider_changed.status_code, 400)
            request_post.assert_not_called()

    def test_model_save_test_and_router_keep_keys_in_their_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            settings_path = root / "settings.json"
            config_path.write_text(yaml.safe_dump({
                "credentials": {"client_id": "id", "client_secret": "secret"},
                "llm": {
                    "enabled": True,
                    "provider": "openai_compatible",
                    "base_url": "https://base-models.invalid/v1",
                    "model": "base-model",
                    "api_key": "base-key",
                },
            }, sort_keys=False), encoding="utf-8")
            store = AdminSettingsStore(settings_path)
            settings = store.load()
            settings["models"] = [{
                "id": "m1", "provider": "openai_compatible",
                "base_url": "https://old-model.invalid/v1", "model": "old-model",
                "api_key": "profile-key", "enabled": True,
            }]
            settings["connectors"] = [{
                "id": "c1", "type": "http", "url": "https://old-service.invalid/api",
                "auth_value": "connector-key", "enabled": True,
            }]
            store.save(settings)
            app = create_admin_app(config_path, settings_path)
            app.testing = True
            client = app.test_client()

            saved_model = client.put("/api/models", json={"models": [{
                "id": "m1", "provider": "openai_compatible",
                "base_url": "https://new-model.invalid/v1", "model": "new-model",
                "enabled": True,
            }], "routing": {}})
            self.assertEqual(saved_model.status_code, 200)
            self.assertFalse(store.load()["models"][0].get("api_key"))

            # Restore an old scoped key, then prove a test edit cannot borrow it
            # or the unrelated default key.
            settings = store.load()
            settings["models"] = [{
                "id": "m1", "provider": "openai_compatible",
                "base_url": "https://old-model.invalid/v1", "model": "old-model",
                "api_key": "profile-key", "enabled": True,
            }]
            store.save(settings)
            with patch("utils.admin_server.requests.post") as request_post:
                rejected = client.post("/api/models/test", json={
                    "id": "m1", "provider": "openai_compatible",
                    "base_url": "https://new-model.invalid/v1", "model": "new-model",
                })
            self.assertEqual(rejected.status_code, 400)
            request_post.assert_not_called()

            response = Mock()
            response.raise_for_status.return_value = None
            with patch("utils.admin_server.requests.post", return_value=response) as request_post:
                local = client.post("/api/models/test", json={
                    "id": "m1", "provider": "custom",
                    "base_url": "http://127.0.0.1:11434/v1", "model": "local-model",
                })
            self.assertEqual(local.status_code, 200)
            self.assertNotIn("Authorization", request_post.call_args.kwargs["headers"])

            changed_connector = client.put("/api/settings/connectors", json=[{
                "id": "c1", "type": "http", "url": "https://new-service.invalid/api",
                "enabled": True,
            }])
            self.assertEqual(changed_connector.status_code, 200)
            self.assertFalse(store.load()["connectors"][0].get("auth_value"))

            router = ModelRouter({
                "provider": "openai_compatible",
                "base_url": "https://base-models.invalid/v1",
                "model": "base-model",
                "api_key": "base-key",
            })
            self.assertEqual(router._client({"model": "same-endpoint"}).api_key, "base-key")
            self.assertEqual(
                router._client({
                    "provider": "openai_compatible",
                    "base_url": "https://unrelated.invalid/v1",
                    "model": "other",
                }).api_key,
                "",
            )


if __name__ == "__main__":
    unittest.main()
