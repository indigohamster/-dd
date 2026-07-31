# -*- coding: utf-8 -*-

import importlib.util
import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config.example.yaml"
BUILD_SCRIPT = ROOT / "build.py"
PRIVATE_MARKER_HASHES = {
    2: {"e270aeb347f2165574c3a5c5bf11d038bcd3acd5abfdb5ae8a1b52d91cb842f0"},
    3: {"a03b221c6c6eae7122ca51695d456d5222e524889136394944b2f9763b483615"},
    7: {"4ef566502f45a57067510b893d72ae5395889757df3c17cb5a8d916283d24dd2"},
    9: {"2d457ada7969959e77dd61a02ba2dbae9b621ebd7256548b2d4386d0a2fc943b"},
    15: {"bd86fa0e8f5e4b443750a7a33fb5e551cdd17a7a2b33bdbbe45881872e1e2808"},
    16: {
        "d79a3757f1bffb9a2f40992d87f171878717d5fafb419b332f07900a40eaa039",
        "5a13f37762d2c7deeecf2991ef2d36002fa9ed14fe61b0575fd652fd677c46ff",
        "1516b9698128e79cdb1e7109b46c02416916bf33c14ce649c5cfd35d221db76c",
    },
    18: {"cda4769ebe3a7acaaa433cf1b0b766d9f01375e5239af9fc6be8e796686a65ce"},
}


def load_build_module():
    spec = importlib.util.spec_from_file_location("distribution_build", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def walk_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value


def private_marker_found(text):
    """Detect known private identifiers without storing them in distributable source."""
    segments = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_+=-]{15,}", text)
    for segment in segments:
        for length, expected in PRIVATE_MARKER_HASHES.items():
            if len(segment) < length:
                continue
            for start in range(len(segment) - length + 1):
                digest = hashlib.sha256(segment[start:start + length].encode("utf-8")).hexdigest()
                if digest in expected:
                    return True
    return False


class SafeDistributionTests(unittest.TestCase):
    def test_example_config_contains_no_credentials_or_machine_paths(self):
        source_files = (BUILD_SCRIPT, TEMPLATE, Path(__file__).resolve())
        for source_file in source_files:
            source_text = source_file.read_text(encoding="utf-8")
            for mojibake in ("\u00e9\u2019\u2030", "\u00d5\u00d9", "\ufffd"):
                self.assertNotIn(mojibake, source_text, f"文件包含乱码: {source_file.name}")

        config = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))

        self.assertEqual(config["credentials"], {
            "client_id": "", "client_secret": "", "app_id": "", "agent_id": "",
        })
        self.assertEqual(config["ima"]["client_id"], "")
        self.assertEqual(config["ima"]["api_key"], "")
        self.assertEqual(config["llm"]["api_key"], "")
        self.assertEqual(config["codebuddy"]["password"], "")
        self.assertEqual(config["ticket"]["admins"], [])
        self.assertEqual(config["weekly_report"]["recipients"], [])
        self.assertEqual(config["ima"]["default_knowledge_bases"], [])

        text = TEMPLATE.read_text(encoding="utf-8")
        for value in walk_strings(config):
            self.assertIsNone(
                re.match(r"^[A-Za-z]:[\\/]", value),
                f"模板包含 Windows 绝对路径: {value}",
            )
            self.assertFalse(value.startswith("\\\\"), f"模板包含 UNC 路径: {value}")
        self.assertFalse(private_marker_found(text), "模板包含私有租户标记")

    def test_runtime_sources_contain_no_tenant_identity_or_knowledge_base_ids(self):
        runtime_files = [ROOT / "main.py", ROOT / "build.py", TEMPLATE]
        for folder in ("handlers", "modules", "utils", "admin_ui"):
            runtime_files.extend(
                path for path in (ROOT / folder).rglob("*")
                if path.is_file() and path.suffix.lower() in {".py", ".js", ".css", ".html", ".mjs"}
            )
        for source_file in runtime_files:
            source = source_file.read_text(encoding="utf-8")
            self.assertFalse(
                private_marker_found(source),
                f"运行源码包含租户标记: {source_file}",
            )

    def test_build_installs_template_instead_of_working_config(self):
        build = load_build_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project"
            dist = project / "dist"
            project.mkdir()
            (project / "config.yaml").write_text(
                "credentials:\n  client_secret: development-only-secret\n",
                encoding="utf-8",
            )

            target = Path(build.install_default_config(str(dist), str(TEMPLATE)))

            self.assertEqual(target.read_bytes(), TEMPLATE.read_bytes())
            self.assertNotIn("development-only-secret", target.read_text(encoding="utf-8"))

    def test_build_script_does_not_copy_project_config(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('CONFIG_TEMPLATE = os.path.join(PROJECT_DIR, "config.example.yaml")', source)
        self.assertNotRegex(
            source,
            r"(?:copy|copy2)\s*\([^\n]*(?:PROJECT_DIR[^\n]*)?[\"']config\.yaml[\"']",
        )
        self.assertIn("--distpath", source)
        self.assertIn("assert_safe_package", source)
        self.assertIn('EXE_NAME + "-package"', source)

    def test_package_gate_rejects_extra_runtime_data(self):
        build = load_build_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            package = Path(temp_dir) / "package"
            package.mkdir()
            (package / (build.EXE_NAME + ".exe")).write_bytes(
                b"MZ" + b"\0" * build.MIN_EXE_BYTES
            )
            (package / "config.yaml").write_bytes(TEMPLATE.read_bytes())
            (package / build.PACKAGE_README).write_text("readme", encoding="utf-8")
            self.assertTrue(build.assert_safe_package(str(package)))
            (package / "data").mkdir()
            with self.assertRaisesRegex(RuntimeError, "白名单"):
                build.assert_safe_package(str(package))

    def test_package_gate_rejects_future_secret_fields_and_absolute_paths(self):
        build = load_build_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            package = Path(temp_dir) / "package"
            package.mkdir()
            (package / (build.EXE_NAME + ".exe")).write_bytes(
                b"MZ" + b"\0" * build.MIN_EXE_BYTES
            )
            (package / build.PACKAGE_README).write_text("readme", encoding="utf-8")
            config = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
            config["future_connector"] = {"access_token": "must-not-ship"}
            (package / "config.yaml").write_text(
                yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "敏感字段"):
                build.assert_safe_package(str(package))

            config["future_connector"]["access_token"] = ""
            config["spreadsheet"]["node_executable"] = r"C:\\private\\node.exe"
            (package / "config.yaml").write_text(
                yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "绝对路径"):
                build.assert_safe_package(str(package))

    def test_package_gate_rejects_camel_case_credentials_and_sensitive_urls(self):
        build = load_build_module()
        for candidate in (
            {"future": {"secretKey": "must-not-ship"}},
            {"future": {"credential": "must-not-ship"}},
            {"future": {"api_key": {"value": "must-not-ship"}}},
            {"future": {"token": ["must-not-ship"]}},
            {"future": {"webhook_url": "https://example.invalid/hook?access_token=must-not-ship"}},
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(RuntimeError):
                    build._assert_sanitized_value(candidate)

    def test_prepare_package_refuses_to_delete_runtime_data_or_filled_config(self):
        build = load_build_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            dist = Path(temp_dir) / "dist"
            package = dist / "package"
            package.mkdir(parents=True)
            (package / "data").mkdir()
            with patch.object(build, "DIST_DIR", str(dist)):
                with self.assertRaisesRegex(RuntimeError, "运行数据"):
                    build.prepare_package_dir(str(package))
            self.assertTrue((package / "data").exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            dist = Path(temp_dir) / "dist"
            package = dist / "package"
            package.mkdir(parents=True)
            unsafe = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
            unsafe["credentials"]["client_id"] = "configured-client"
            (package / "config.yaml").write_text(
                yaml.safe_dump(unsafe, allow_unicode=True), encoding="utf-8"
            )
            with patch.object(build, "DIST_DIR", str(dist)):
                with self.assertRaisesRegex(RuntimeError, "非空凭证"):
                    build.prepare_package_dir(str(package))
            self.assertTrue((package / "config.yaml").exists())


if __name__ == "__main__":
    unittest.main()
