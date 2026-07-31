import importlib.util
import logging
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER_PATH = PROJECT_ROOT / "utils" / "logger.py"


def _load_fresh_logger_module():
    module_name = f"test_runtime_logger_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, LOGGER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PersistentLoggingTests(unittest.TestCase):
    def tearDown(self):
        # Tests use unique logger names; close and detach their handlers so the
        # temporary log directories can be removed reliably on Windows.
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if not name.startswith("v2161-test-") or not isinstance(logger, logging.Logger):
                continue
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                if getattr(handler, "_opspilot_file_handler", False):
                    handler.close()

    def test_source_frozen_and_override_log_paths(self):
        module = _load_fresh_logger_module()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(module.sys, "frozen", False, create=True):
                self.assertEqual(module._get_log_dir(), PROJECT_ROOT / "logs")

            executable = PROJECT_ROOT / "dist" / "OpsPilot.exe"
            with patch.object(module.sys, "frozen", True, create=True), patch.object(
                module.sys, "executable", str(executable)
            ):
                self.assertEqual(module._get_log_dir(), executable.parent / "logs")

        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "custom-logs"
            with patch.dict(os.environ, {"OPSPILOT_LOG_DIR": str(override)}):
                self.assertEqual(module._get_log_dir(), override.resolve())

    def test_warning_is_persisted_info_is_not_and_secrets_are_redacted(self):
        module = _load_fresh_logger_module()
        temp_dir = tempfile.TemporaryDirectory()
        try:
            with patch.dict(os.environ, {"OPSPILOT_LOG_DIR": temp_dir.name}):
                logger = module.setup_logger(f"v2161-test-{uuid.uuid4().hex}")
                logger.info("info-only-marker")
                logger.warning(
                    "secrets Bearer bearer-value sk-live_example "
                    "api_key=plain-key client_secret:'quoted secret' "
                    'access_token="access-value" token=token-value password: hunter2 '
                    'DEEPSEEK_API_KEY=provider-key IMA_ACCESS_TOKEN="ima-token" '
                    'refresh_token=refresh-value DB_PASSWORD=db-password '
                    'prompt_tokens=321 max_tokens=1024'
                )
                for handler in logger.handlers:
                    handler.flush()

                log_text = (Path(temp_dir.name) / "opspilot-error.log").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("info-only-marker", log_text)
                for secret in (
                    "bearer-value",
                    "sk-live_example",
                    "plain-key",
                    "quoted secret",
                    "access-value",
                    "token-value",
                    "hunter2",
                    "provider-key",
                    "ima-token",
                    "refresh-value",
                    "db-password",
                ):
                    self.assertNotIn(secret, log_text)
                self.assertIn("Bearer ***", log_text)
                self.assertIn("prompt_tokens=321", log_text)
                self.assertIn("max_tokens=1024", log_text)
                self.assertGreaterEqual(log_text.count("***"), 11)
        finally:
            if module._file_handler is not None:
                module._file_handler.close()
            temp_dir.cleanup()

    def test_file_handler_is_shared_and_repeated_setup_does_not_duplicate(self):
        module = _load_fresh_logger_module()
        temp_dir = tempfile.TemporaryDirectory()
        try:
            with patch.dict(os.environ, {"OPSPILOT_LOG_DIR": temp_dir.name}):
                first_name = f"v2161-test-{uuid.uuid4().hex}"
                second_name = f"v2161-test-{uuid.uuid4().hex}"
                first = module.setup_logger(first_name)
                first_handler_count = len(first.handlers)
                self.assertIs(first, module.setup_logger(first_name))
                self.assertEqual(len(first.handlers), first_handler_count)

                second = module.setup_logger(second_name)
                first_file = next(
                    handler
                    for handler in first.handlers
                    if getattr(handler, "_opspilot_file_handler", False)
                )
                second_file = next(
                    handler
                    for handler in second.handlers
                    if getattr(handler, "_opspilot_file_handler", False)
                )
                self.assertIs(first_file, second_file)

                first.warning("single-write-marker")
                first_file.flush()
                log_text = (Path(temp_dir.name) / "opspilot-error.log").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(log_text.count("single-write-marker"), 1)
        finally:
            if module._file_handler is not None:
                module._file_handler.close()
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
