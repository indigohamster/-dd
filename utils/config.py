"""Configuration loading and durable writes.

The application has two configuration states:

* setup mode: the local admin console can run with safe defaults;
* runtime mode: DingTalk credentials are present and the bot may connect.

``load_config`` keeps its historical strict behaviour by default.  Callers
which only need the local setup console must opt in with
``allow_incomplete=True``.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

import yaml


def _get_base_dir() -> Path:
    """Return the writable runtime directory in source and frozen builds."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


DEFAULT_CONFIG_PATH = _get_base_dir() / "config.yaml"

# These defaults intentionally contain no credentials.  They are sufficient
# for the local console and local SQLite-backed modules to start safely.
DEFAULT_CONFIG: dict[str, Any] = {
    "credentials": {
        "client_id": "",
        "client_secret": "",
        "app_id": "",
        "agent_id": "",
        "corp_id": "",
    },
    "logging": {
        "level": "INFO",
        "format": "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S",
    },
    "admin_ui": {"enabled": True, "port": 8765, "auto_open": True, "threads": 8},
    "chat": {"auto_echo": False, "enable_p2p": True, "enable_group": True},
    "ticket": {
        "enabled": True,
        "database_path": "data/tickets.db",
        "integrity_check": True,
        "busy_timeout_ms": 5000,
        "backup_enabled": True,
        "backup_dir": "data/backups",
        "backup_retention_days": 30,
    },
    "weekly_report": {"recipients": []},
    "daily_ops_summary": {"enabled": False, "hour": 9},
    "issue_context": {
        "enabled": False,
        "database_path": "data/issues.db",
    },
    "knowledge_loop": {
        "enabled": True,
        "database_path": "data/knowledge_loop.db",
        "export_dir": "data/knowledge_exports",
        "publish_mode": "markdown_export",
    },
    "spreadsheet": {
        # Optional runtime dependencies are not bundled into the lightweight
        # EXE, so a fresh installation must opt in after the readiness check.
        "enabled": False,
        "max_file_mb": 20,
        "timeout": 90,
        "max_concurrent": 2,
        "queue_timeout": 10,
        "verify_render": False,
        "node_executable": "node",
        "node_modules_path": "",
    },
    "async_tasks": {"database_path": "data/tasks.db", "max_workers": 2},
    "events": {"enabled": True, "subscribed_events": []},
    "ticket_notifications": {"enabled": True},
    "observability": {
        "enabled": True,
        "database_path": "data/observability.db",
        "retention_days": 30,
    },
    "ima": {
        "enabled": False,
        "client_id": "",
        "api_key": "",
        "default_knowledge_bases": [],
    },
    "llm": {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "chat_completions_path": "/v1/chat/completions",
        "model": "deepseek-chat",
        "api_key": "",
        "temperature": 0.3,
        "max_tokens": 1024,
        "timeout": 30,
    },
    "codebuddy": {"enabled": False, "auto_start": False},
    "voice": {"enabled": True, "model": "base"},
    "vision": {"enabled": False},
}

_WRITE_LOCK = threading.RLock()


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_config_path(config_path: str | os.PathLike[str] | None = None) -> Path:
    return Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH.resolve()


def runtime_config_missing(config: Mapping[str, Any]) -> list[str]:
    """Return fields which prevent the DingTalk Stream bot from starting."""
    credentials = config.get("credentials", {})
    if not isinstance(credentials, Mapping):
        credentials = {}
    missing = []
    if not str(credentials.get("client_id") or "").strip():
        missing.append("credentials.client_id")
    if not str(credentials.get("client_secret") or "").strip():
        missing.append("credentials.client_secret")
    return missing


def is_runtime_configured(config: Mapping[str, Any]) -> bool:
    return not runtime_config_missing(config)


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    allow_incomplete: bool = False,
    apply_environment: bool = True,
) -> dict[str, Any]:
    """Load YAML configuration with defaults and environment overrides.

    Args:
        config_path: Optional YAML path.
        allow_incomplete: When true, a missing file or missing DingTalk
            credentials returns setup-safe defaults instead of raising.
        apply_environment: Apply runtime credential environment variables.
            Durable configuration writers set this to false so a runtime
            secret is never copied into config.yaml as a side effect.
    """
    path = resolve_config_path(config_path)
    if not path.exists():
        if not allow_incomplete:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        loaded: dict[str, Any] = {}
    else:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f"（第 {mark.line + 1} 行）" if mark is not None else ""
            raise ValueError(f"配置文件 YAML 格式错误{location}") from None
        if value is None:
            loaded = {}
        elif not isinstance(value, dict):
            raise ValueError("配置文件根节点必须是 YAML 对象")
        else:
            loaded = value

    config = _deep_merge(DEFAULT_CONFIG, loaded)
    invalid_sections = [
        section for section, default in DEFAULT_CONFIG.items()
        if isinstance(default, Mapping) and not isinstance(config.get(section), Mapping)
    ]
    if invalid_sections:
        if not allow_incomplete:
            raise ValueError(
                "配置段必须是 YAML 对象: " + ", ".join(sorted(invalid_sections))
            )
        for section in invalid_sections:
            config[section] = copy.deepcopy(DEFAULT_CONFIG[section])

    # Environment variables intentionally remain runtime-only overrides. They
    # are never written back by the setup wizard.
    if apply_environment:
        client_id = os.getenv("DINGTALK_CLIENT_ID")
        client_secret = os.getenv("DINGTALK_CLIENT_SECRET")
        if client_id:
            config["credentials"]["client_id"] = client_id
        if client_secret:
            config["credentials"]["client_secret"] = client_secret
        admin_port = os.getenv("OPSPILOT_ADMIN_PORT")
        if admin_port:
            try:
                parsed_port = int(admin_port)
                if not 1 <= parsed_port <= 65535:
                    raise ValueError
                config["admin_ui"]["port"] = parsed_port
            except ValueError:
                # main.py performs the final safe fallback and logs one concise
                # warning; never let a malformed environment value break setup.
                config["admin_ui"]["port"] = 8765
        auto_open = os.getenv("OPSPILOT_ADMIN_AUTO_OPEN")
        if auto_open:
            config["admin_ui"]["auto_open"] = auto_open.strip().lower() in {
                "1", "true", "yes", "on",
            }

    missing = runtime_config_missing(config)
    if missing and not allow_incomplete:
        labels = ", ".join(missing)
        raise ValueError(
            f"缺少 {labels}，请在本机配置向导、config.yaml 或环境变量中配置"
        )
    return config


def atomic_write_yaml(path_value: str | os.PathLike[str], data: Mapping[str, Any]) -> Path:
    """Atomically replace a YAML file without using a shared ``.tmp`` name."""
    path = Path(path_value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        dict(data), allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    with _WRITE_LOCK:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    return path


def atomic_update_config(
    path_value: str | os.PathLike[str], updates: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge updates into the on-disk configuration and replace it atomically."""
    path = Path(path_value).expanduser().resolve()
    with _WRITE_LOCK:
        current = load_config(path, allow_incomplete=True, apply_environment=False)
        merged = _deep_merge(current, updates)
        atomic_write_yaml(path, merged)
    return merged
