#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpsPilot 企业运维助手 V2.17.6 的安全 EXE 打包脚本。"""

import os
import hashlib
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qsl, urlsplit

import yaml

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(PROJECT_DIR, "dist")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")
EXE_NAME = "dingtalk-ops-v2.17.6"
CONFIG_TEMPLATE = os.path.join(PROJECT_DIR, "config.example.yaml")
PACKAGE_DIR = os.path.join(DIST_DIR, EXE_NAME + "-package")
PACKAGE_README = "首次使用说明.txt"
MIN_EXE_BYTES = 1024 * 1024
SENSITIVE_KEYS = {
    "secret", "token", "password", "api_key", "apikey",
    "auth_value", "authorization", "private_key",
}
SENSITIVE_KEY_SUFFIXES = (
    "_secret", "_token", "_password", "_api_key", "_apikey",
    "_auth_value", "_authorization", "_private_key",
)
SENSITIVE_COMPACT_SUFFIXES = (
    "secret", "secretkey", "token", "password", "passwd", "apikey",
    "authvalue", "authorization", "privatekey", "credential", "cookie",
    "signature",
)
NON_SECRET_TOKEN_KEYS = {"max_token", "max_tokens", "token_limit"}


def run(cmd):
    """在 UTF-8 环境执行命令并实时输出。"""
    print(f"\n>>> {cmd}")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, shell=True, cwd=PROJECT_DIR, env=env)
    if result.returncode != 0:
        print(f"\n[ERROR] 命令执行失败，退出码: {result.returncode}")
        sys.exit(1)
    return result


def _is_within(path, parent):
    return os.path.commonpath([os.path.abspath(path), os.path.abspath(parent)]) == os.path.abspath(parent)


def prepare_package_dir(package_dir=PACKAGE_DIR):
    """Recreate an isolated allow-list staging folder without touching live data."""
    if not _is_within(package_dir, DIST_DIR) or os.path.abspath(package_dir) == os.path.abspath(DIST_DIR):
        raise ValueError("发行暂存目录必须位于 dist 的独立子目录中")
    if os.path.isdir(package_dir):
        allowed = {EXE_NAME + ".exe", "config.yaml", PACKAGE_README}
        entries = set(os.listdir(package_dir))
        if entries - allowed or any(
            os.path.isdir(os.path.join(package_dir, name)) for name in entries
        ):
            raise RuntimeError("发行暂存目录包含运行数据或额外文件，拒绝自动删除")
        config_path = os.path.join(package_dir, "config.yaml")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            _assert_sanitized_value(config)
            _assert_known_credentials_empty(config)
        shutil.rmtree(package_dir)
    os.makedirs(package_dir, exist_ok=True)
    return package_dir


def install_default_config(dist_dir=PACKAGE_DIR, template_path=CONFIG_TEMPLATE):
    """Install the sanitized first-run configuration into a distribution."""
    if not os.path.isfile(template_path):
        raise FileNotFoundError(f"安全配置模板不存在: {template_path}")
    os.makedirs(dist_dir, exist_ok=True)
    target = os.path.join(dist_dir, "config.yaml")
    shutil.copy2(template_path, target)
    return target


def write_first_run_readme(package_dir=PACKAGE_DIR):
    path = os.path.join(package_dir, PACKAGE_README)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "OpsPilot V2.17.6 首次使用\n"
            "==========================\n\n"
            "1. 双击 dingtalk-ops-v2.17.6.exe。\n"
            "2. 浏览器会打开 http://127.0.0.1:8765。\n"
            "3. 按向导填写钉钉凭证和可选 AI 配置。\n"
            "4. 完成体检后机器人会自动开始连接。\n\n"
            "请先把整个目录复制到本机可写位置，并确保 8765 端口未被占用。\n"
            "不要直接放在 Program Files、只读共享盘或压缩包内运行。\n\n"
            "本目录的 config.yaml 是空白安全模板。不要用其他机器的正式配置覆盖后再分发。\n"
        )
    return path


def _assert_sanitized_value(value, path="config"):
    """Recursively reject future secret fields and machine-bound paths."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_name = str(key).lower().replace("-", "_")
            compact_name = re.sub(r"[^a-z0-9]", "", str(key).lower())
            item_path = f"{path}.{key}"
            sensitive = (
                key_name not in NON_SECRET_TOKEN_KEYS
                and (
                    key_name in SENSITIVE_KEYS
                    or key_name.endswith(SENSITIVE_KEY_SUFFIXES)
                    or compact_name.endswith(SENSITIVE_COMPACT_SUFFIXES)
                )
            )
            if sensitive:
                non_empty = bool(item) if isinstance(item, (dict, list)) else bool(
                    str(item or "").strip()
                )
                if non_empty:
                    raise RuntimeError(f"发行配置的敏感字段非空: {item_path}")
            _assert_sanitized_value(item, item_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_sanitized_value(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
            raise RuntimeError(f"发行配置包含本机绝对路径: {path}")
        if "://" in value:
            try:
                parsed = urlsplit(value)
                if parsed.username or parsed.password:
                    raise RuntimeError(f"发行配置 URL 包含用户凭证: {path}")
                for name, query_value in parse_qsl(parsed.query, keep_blank_values=True):
                    normalized = name.lower().replace("-", "_")
                    compact = re.sub(r"[^a-z0-9]", "", name.lower())
                    if query_value and (
                        normalized in SENSITIVE_KEYS
                        or normalized.endswith(SENSITIVE_KEY_SUFFIXES)
                        or compact.endswith(SENSITIVE_COMPACT_SUFFIXES)
                    ):
                        raise RuntimeError(f"发行配置 URL 包含敏感查询参数: {path}")
            except ValueError:
                raise RuntimeError(f"发行配置 URL 格式无效: {path}") from None


def _assert_known_credentials_empty(config):
    required_empty = (
        config.get("credentials", {}).get("client_id"),
        config.get("credentials", {}).get("client_secret"),
        config.get("credentials", {}).get("app_id"),
        config.get("credentials", {}).get("agent_id"),
        config.get("ima", {}).get("client_id"),
        config.get("ima", {}).get("api_key"),
        config.get("llm", {}).get("api_key"),
        config.get("codebuddy", {}).get("password"),
    )
    if any(str(value or "").strip() for value in required_empty):
        raise RuntimeError("发行配置包含非空凭证，已停止打包")


def assert_safe_package(package_dir=PACKAGE_DIR):
    """Fail the build if the hand-off folder contains anything outside the allow-list."""
    expected = {EXE_NAME + ".exe", "config.yaml", PACKAGE_README}
    actual = set(os.listdir(package_dir))
    if actual != expected:
        raise RuntimeError(f"发行目录文件不符合白名单: {sorted(actual)}")
    exe_path = os.path.join(package_dir, EXE_NAME + ".exe")
    if not os.path.isfile(exe_path) or os.path.getsize(exe_path) < MIN_EXE_BYTES:
        raise RuntimeError("发行 EXE 不存在或体积异常")
    with open(exe_path, "rb") as handle:
        if handle.read(2) != b"MZ":
            raise RuntimeError("发行 EXE 不是有效的 Windows 可执行文件")
    config_path = os.path.join(package_dir, "config.yaml")
    with open(config_path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    _assert_sanitized_value(config)
    _assert_known_credentials_empty(config)
    return True


def main():
    print("=" * 50)
    print("  OpsPilot 企业运维助手 V2.17.6 安全打包")
    print("=" * 50)

    print("\n[1/4] 检查打包依赖...")
    run(f'"{sys.executable}" -c "import dingtalk_stream, yaml, requests, waitress, PyInstaller"')

    print("\n[2/4] 检查 PyInstaller...")
    run(f'"{sys.executable}" -m PyInstaller --version')

    print("\n[3/4] 清理旧产物...")
    # 保留旧 dist 中的本机运行数据，但新交付物只写入独立白名单目录。
    if os.path.exists(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(DIST_DIR, exist_ok=True)
    prepare_package_dir()
    spec_file = os.path.join(PROJECT_DIR, f"{EXE_NAME}.spec")
    if os.path.exists(spec_file):
        os.remove(spec_file)

    print("\n[4/4] 打包中...")
    # Whisper/Torch 是可选的本地语音兜底，单独部署；不打入轻量主程序。
    cmd = (
        f'"{sys.executable}" -m PyInstaller '
        f'--onefile --name "{EXE_NAME}" --console --noconfirm --clean '
        f'--distpath "{PACKAGE_DIR}" --workpath "{BUILD_DIR}" '
        f'--hidden-import "dingtalk_stream" '
        f'--hidden-import "dingtalk_stream.chatbot" '
        f'--hidden-import "yaml" --hidden-import "requests" '
        f'--hidden-import "flask" --hidden-import "waitress" --add-data "admin_ui;admin_ui" '
        f'--add-data "utils/spreadsheet_worker.mjs;utils" '
        f'--exclude-module "whisper" --exclude-module "torch" '
        f'--exclude-module "numpy" --exclude-module "numba" '
        f'--exclude-module "llvmlite" --exclude-module "onnxruntime" '
        f'main.py'
    )
    run(cmd)

    config_dst = install_default_config(PACKAGE_DIR)
    write_first_run_readme(PACKAGE_DIR)
    assert_safe_package(PACKAGE_DIR)
    print(f"\n  已生成安全空白配置 -> {config_dst}")

    exe_path = os.path.join(PACKAGE_DIR, EXE_NAME + ".exe")
    with open(exe_path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    print("\n[完成] 打包成功！")
    print(f"  exe 路径: {exe_path}")
    print(f"  exe 大小: {os.path.getsize(exe_path)} bytes")
    print(f"  SHA-256: {digest}")
    print(f"  可交付目录: {PACKAGE_DIR}")
    print("  配置文件: config.yaml（由脱敏模板生成，首次使用时填写）")
    print("\n  注意: 本地 Whisper 是可选独立组件；钉钉原生语音识别不受影响。")


if __name__ == "__main__":
    main()
