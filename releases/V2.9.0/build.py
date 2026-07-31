#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉企业运维助手 V2.9.0 EXE 打包脚本。"""

import os
import shutil
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(PROJECT_DIR, "dist")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")
EXE_NAME = "dingtalk-ops-v2.9.0"


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


def main():
    print("=" * 50)
    print("  钉钉企业运维助手 V2.9.0 打包")
    print("=" * 50)

    print("\n[1/4] 检查打包依赖...")
    run(f'"{sys.executable}" -c "import dingtalk_stream, yaml, requests, PyInstaller"')

    print("\n[2/4] 检查 PyInstaller...")
    run(f'"{sys.executable}" -m PyInstaller --version')

    print("\n[3/4] 清理旧产物...")
    # dist 可能正被资源管理器打开，保留目录本身，只替换本版本目标文件。
    if os.path.exists(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(DIST_DIR, exist_ok=True)
    target_exe = os.path.join(DIST_DIR, EXE_NAME + ".exe")
    if os.path.exists(target_exe):
        os.remove(target_exe)
    spec_file = os.path.join(PROJECT_DIR, f"{EXE_NAME}.spec")
    if os.path.exists(spec_file):
        os.remove(spec_file)

    print("\n[4/4] 打包中...")
    # Whisper/Torch 是可选的本地语音兜底，单独部署；不打入轻量主程序。
    cmd = (
        f'"{sys.executable}" -m PyInstaller '
        f'--onefile --name "{EXE_NAME}" --console --noconfirm --clean '
        f'--hidden-import "dingtalk_stream" '
        f'--hidden-import "dingtalk_stream.chatbot" '
        f'--hidden-import "yaml" --hidden-import "requests" '
        f'--hidden-import "flask" --add-data "admin_ui;admin_ui" '
        f'--add-data "utils/spreadsheet_worker.mjs;utils" '
        f'--exclude-module "whisper" --exclude-module "torch" '
        f'--exclude-module "numpy" --exclude-module "numba" '
        f'--exclude-module "llvmlite" --exclude-module "onnxruntime" '
        f'main.py'
    )
    run(cmd)

    config_src = os.path.join(PROJECT_DIR, "config.yaml")
    config_dst = os.path.join(DIST_DIR, "config.yaml")
    if os.path.exists(config_src):
        shutil.copy2(config_src, config_dst)
        print(f"\n  已复制 config.yaml -> {config_dst}")

    exe_path = os.path.join(DIST_DIR, EXE_NAME + ".exe")
    print("\n[完成] 打包成功！")
    print(f"  exe 路径: {exe_path}")
    print("  配置文件: config.yaml（已复制到 dist 目录）")
    print("\n  注意: 本地 Whisper 为可选独立组件；钉钉原生语音识别不受影响。")


if __name__ == "__main__":
    main()
