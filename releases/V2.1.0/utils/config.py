# ============================================================
# 钉钉 Stream 机器人配置加载器
# ============================================================

import os
import sys
import yaml
from pathlib import Path


def _get_base_dir() -> Path:
    """
    获取项目根目录。
    PyInstaller 打包后，使用 exe 所在目录；
    开发环境使用源码根目录。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后的 exe
        return Path(sys.executable).parent
    else:
        # 开发环境
        return Path(__file__).parent.parent


DEFAULT_CONFIG_PATH = _get_base_dir() / "config.yaml"


def load_config(config_path: str | None = None) -> dict:
    """
    加载配置文件，支持 YAML 文件和环境变量覆盖。

    环境变量优先级高于配置文件：
    - DINGTALK_CLIENT_ID     覆盖 credentials.client_id
    - DINGTALK_CLIENT_SECRET 覆盖 credentials.client_secret

    Args:
        config_path: 配置文件路径，默认为项目根目录下 config.yaml

    Returns:
        配置字典
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 环境变量覆盖（用于 Docker / CI 等场景）
    if os.getenv("DINGTALK_CLIENT_ID"):
        config.setdefault("credentials", {})["client_id"] = os.getenv("DINGTALK_CLIENT_ID")
    if os.getenv("DINGTALK_CLIENT_SECRET"):
        config.setdefault("credentials", {})["client_secret"] = os.getenv("DINGTALK_CLIENT_SECRET")

    # 基本校验
    creds = config.get("credentials", {})
    if not creds.get("client_id"):
        raise ValueError("缺少 credentials.client_id，请在 config.yaml 或环境变量 DINGTALK_CLIENT_ID 中配置")
    if not creds.get("client_secret"):
        raise ValueError("缺少 credentials.client_secret，请在 config.yaml 或环境变量 DINGTALK_CLIENT_SECRET 中配置")

    return config
