"""Small registry used by the simple-mode console to manage capabilities."""

from __future__ import annotations

import importlib.util
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from utils.config import atomic_update_config, load_config
from utils.credential_scope import llm_profile_is_usable
from utils.spreadsheet_service import SpreadsheetService


@dataclass(frozen=True)
class ModuleDefinition:
    id: str
    name: str
    description: str
    category: str
    section: str
    default_enabled: bool = True
    required: bool = False
    restart_required: bool = True


MODULE_DEFINITIONS = (
    ModuleDefinition("tickets", "工单中心", "创建、处理和跟踪运维工单", "operations", "ticket", True),
    ModuleDefinition("knowledge", "知识闭环", "从已解决工单沉淀知识草稿", "operations", "knowledge_loop", True),
    ModuleDefinition("spreadsheet", "表格助手", "异步处理 Excel 和 CSV 文件", "files", "spreadsheet", True),
    ModuleDefinition("vision", "图片识别", "识别截图中的报错和关键信息", "files", "vision", True),
    ModuleDefinition("voice", "语音识别", "将钉钉语音转换为文本", "files", "voice", True),
    ModuleDefinition("aitable", "智能表格同步", "将工单记录同步到钉钉智能表格", "connection", "aitable_ticket_sync", False),
)

MODULE_ALIASES = {"ticket": "tickets", "knowledge_loop": "knowledge"}


class ModuleRegistry:
    """Read module state, change switches, and perform explicit diagnostics."""

    def __init__(self, config_path: str | os.PathLike[str], settings_store=None):
        self.config_path = Path(config_path).resolve()
        self.settings_store = settings_store
        self._definitions = {item.id: item for item in MODULE_DEFINITIONS}

    def _config(self) -> dict[str, Any]:
        return load_config(self.config_path, allow_incomplete=True)

    def _enabled(self, definition: ModuleDefinition, config: Mapping[str, Any]) -> bool:
        if definition.id == "aitable":
            settings = self.settings_store.load() if self.settings_store else {}
            return bool(settings.get("aitable_ticket_sync", {}).get("enabled", False))
        section = config.get(definition.section, {})
        return bool(section.get("enabled", definition.default_enabled))

    def _configured(self, module_id: str, config: Mapping[str, Any]) -> bool:
        if module_id == "llm":
            cfg = config.get("llm", {})
            local = str(cfg.get("provider") or "").lower() in {"local", "ollama", "custom"}
            return bool(cfg.get("base_url") and cfg.get("model") and (local or cfg.get("api_key")))
        if module_id == "ima":
            cfg = config.get("ima", {})
            return bool(cfg.get("client_id") and cfg.get("api_key"))
        if module_id == "spreadsheet":
            cfg = config.get("spreadsheet", {})
            try:
                # Keep the readiness contract identical to real task execution:
                # Node alone is not enough; the worker also requires
                # @oai/artifact-tool on NODE_PATH.
                SpreadsheetService(cfg)._runtime()
                return True
            except (OSError, RuntimeError, ValueError):
                return False
        if module_id == "voice":
            # DingTalk normally supplies recognition text with a voice message.
            # Local Whisper is an optional fallback, not a prerequisite.
            return True
        if module_id == "vision":
            settings = self.settings_store.load() if self.settings_store else {}
            if not isinstance(settings, Mapping):
                settings = {}
            base = config.get("llm", {})
            profiles = {
                str(item.get("id") or ""): item
                for item in settings.get("models", [])
                if isinstance(item, Mapping) and item.get("enabled", True) and item.get("id")
            }
            routing = settings.get("model_routing", {})
            if not isinstance(routing, Mapping):
                routing = {}
            route_ids = []
            for route_id in (routing.get("vision"), routing.get("fallback")):
                route_id = str(route_id or "")
                if route_id and route_id not in route_ids:
                    route_ids.append(route_id)
            if not route_ids:
                # ModelRouter uses this endpoint as the implicit route until an
                # explicit vision/fallback profile is selected.
                return llm_profile_is_usable(base)
            for route_id in route_ids:
                profile = profiles.get(route_id)
                if profile and llm_profile_is_usable(profile, base=base):
                    return True
            return False
        if module_id == "aitable":
            settings = self.settings_store.load() if self.settings_store else {}
            sync = settings.get("aitable_ticket_sync", {})
            executable = str(sync.get("dws_executable") or "dws")
            ticket_enabled = bool(config.get("ticket", {}).get("enabled", True))
            return bool(
                ticket_enabled
                and sync.get("base_id")
                and sync.get("table_id")
                and (shutil.which(executable) or Path(executable).is_file())
            )
        return True

    def list_modules(self) -> list[dict[str, Any]]:
        config = self._config()
        result = []
        for definition in MODULE_DEFINITIONS:
            configured = self._configured(definition.id, config)
            dependencies = []
            if definition.id == "vision":
                dependencies = ["llm_or_model_profile"]
            elif definition.id == "spreadsheet":
                dependencies = ["node", "@oai/artifact-tool"]
            elif definition.id == "aitable":
                dependencies = ["tickets", "dws", "base_id", "table_id"]
            item = {
                "id": definition.id,
                "name": definition.name,
                "description": definition.description,
                "category": definition.category,
                "enabled": self._enabled(definition, config),
                "configured": configured,
                "available": configured or definition.id not in {"spreadsheet", "vision", "aitable"},
                "required": definition.required,
                "restart_required": definition.restart_required,
                "dependencies": dependencies,
                "testable": True,
            }
            if definition.id == "voice":
                item["local_fallback_configured"] = importlib.util.find_spec("whisper") is not None
            result.append(item)
        return result

    def get(self, module_id: str) -> ModuleDefinition:
        module_id = MODULE_ALIASES.get(module_id, module_id)
        try:
            return self._definitions[module_id]
        except KeyError:
            raise KeyError(f"未知模块: {module_id}") from None

    def is_configured(self, module_id: str, config: Mapping[str, Any] | None = None) -> bool:
        definition = self.get(module_id)
        return self._configured(definition.id, config if config is not None else self._config())

    def set_enabled(self, module_id: str, enabled: bool) -> dict[str, Any]:
        definition = self.get(module_id)
        module_id = definition.id
        if definition.required and not enabled:
            raise ValueError("钉钉连接是机器人运行所需模块，不能停用；可修改连接凭证")
        config = self._config()
        if enabled and module_id == "spreadsheet" and not self.is_configured(module_id, config):
            raise ValueError("表格助手缺少 Node.js 或 @oai/artifact-tool，请先配置运行环境")
        if enabled and module_id == "vision" and not self.is_configured(module_id, config):
            raise ValueError("图片识别缺少可用的 AI 模型，请先配置模型")
        if enabled and module_id == "aitable" and not self.is_configured(module_id, config):
            raise ValueError("智能表格同步需要先启用工单并配置 dws、Base ID 和 Table ID")
        if module_id == "aitable":
            if self.settings_store is None:
                raise ValueError("智能表格设置存储不可用")
            def update(settings):
                sync = dict(settings.get("aitable_ticket_sync", {}))
                sync["enabled"] = bool(enabled)
                settings["aitable_ticket_sync"] = sync

            self.settings_store.mutate(update)
        else:
            if module_id == "tickets" and not enabled and self.settings_store is not None:
                def disable_sync(settings):
                    sync = dict(settings.get("aitable_ticket_sync", {}))
                    if sync.get("enabled"):
                        sync["enabled"] = False
                        settings["aitable_ticket_sync"] = sync

                self.settings_store.mutate(disable_sync)
            atomic_update_config(self.config_path, {definition.section: {"enabled": bool(enabled)}})
        return next(item for item in self.list_modules() if item["id"] == module_id)

    def test(self, module_id: str) -> dict[str, Any]:
        definition = self.get(module_id)
        module_id = definition.id
        config = self._config()
        started = time.monotonic()
        try:
            detail = self._run_test(module_id, config)
            return {
                "ok": True,
                "module_id": module_id,
                "message": detail,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            # Do not include request bodies, headers, or credentials in the
            # response. Known clients should raise concise messages; all other
            # details remain in server-side logs.
            message = str(exc).replace("\r", " ").replace("\n", " ")
            for secret in (
                config.get("credentials", {}).get("client_secret"),
                config.get("llm", {}).get("api_key"),
                config.get("ima", {}).get("api_key"),
            ):
                secret = str(secret or "")
                if secret:
                    message = message.replace(secret, "***")
            message = message[:240]
            return {
                "ok": False,
                "module_id": definition.id,
                "message": message or "模块检测失败",
                "latency_ms": round((time.monotonic() - started) * 1000),
            }

    def _run_test(self, module_id: str, config: Mapping[str, Any]) -> str:
        if module_id == "dingtalk":
            cfg = config.get("credentials", {})
            if not cfg.get("client_id") or not cfg.get("client_secret"):
                raise ValueError("钉钉 Client ID 或 Client Secret 未配置")
            try:
                response = requests.get(
                    "https://oapi.dingtalk.com/gettoken",
                    params={
                        "appkey": str(cfg["client_id"]),
                        "appsecret": str(cfg["client_secret"]),
                    },
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("errcode") != 0 or not data.get("access_token"):
                    raise RuntimeError("invalid credentials")
            except Exception:
                raise RuntimeError("钉钉连接失败，请核对凭证及网络后重试") from None
            return "钉钉 OpenAPI 连接成功"

        if module_id == "llm":
            cfg = config.get("llm", {})
            base_url = str(cfg.get("base_url") or "").rstrip("/")
            api_path = str(cfg.get("chat_completions_path") or "/v1/chat/completions")
            model = str(cfg.get("model") or "")
            api_key = str(cfg.get("api_key") or "")
            local = str(cfg.get("provider") or "").lower() in {"local", "ollama", "custom"}
            if not base_url or not model or (not local and not api_key):
                raise ValueError("AI 地址、模型或 API Key 未配置完整")
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("AI 地址必须是有效的 http:// 或 https:// 地址")
            if parsed.username or parsed.password:
                raise ValueError("AI 地址不能包含用户名或密码")
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                response = requests.post(
                    base_url + (api_path if api_path.startswith("/") else f"/{api_path}"),
                    headers=headers,
                    json={"model": model, "messages": [{"role": "user", "content": "只回复 OK"}], "max_tokens": 8},
                    timeout=min(max(int(cfg.get("timeout") or 30), 2), 45),
                )
                response.raise_for_status()
            except requests.RequestException:
                raise RuntimeError("AI API 连接失败，请核对地址、模型、密钥及网络") from None
            return "AI API 连接成功"

        if module_id == "ima":
            cfg = config.get("ima", {})
            if not cfg.get("client_id") or not cfg.get("api_key"):
                raise ValueError("IMA Client ID 或 API Key 未配置")
            from utils.ima_client import IMAClient

            client = IMAClient(str(cfg["client_id"]), str(cfg["api_key"]))
            health = client.test_health(str(cfg.get("health_check_query") or ""))
            if not health["connected"]:
                raise RuntimeError(f"IMA 连接失败：{health['detail']}")
            if not health["search_ok"]:
                raise RuntimeError(f"IMA 已连接，但知识检索失败：{health['detail']}")
            if not health["content_available"]:
                raise RuntimeError(f"IMA 已连接且可检索，但 AI 知识来源不可用：{health['detail']}")
            return f"IMA 连接、检索和正文读取均正常：{health['detail']}"

        if module_id in {"tickets", "knowledge"}:
            section = self.get(module_id).section
            cfg = config.get(section, {})
            db_path = Path(os.path.expandvars(str(cfg.get("database_path") or f"data/{section}.db")))
            if not db_path.is_absolute():
                db_path = self.config_path.parent / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            probe = db_path.parent / ".write-test"
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
            if module_id == "knowledge" and config.get("ima", {}).get("enabled", False):
                ima = config.get("ima", {})
                if not ima.get("client_id") or not ima.get("api_key"):
                    raise ValueError("IMA Client ID 或 API Key 未配置")
                from utils.ima_client import IMAClient
                client = IMAClient(str(ima["client_id"]), str(ima["api_key"]))
                health = client.test_health(str(ima.get("health_check_query") or ""))
                if not health["connected"]:
                    raise RuntimeError(f"本地知识目录可写，但 IMA 连接失败：{health['detail']}")
                if not health["search_ok"]:
                    raise RuntimeError(f"本地知识目录可写、IMA 已连接，但检索失败：{health['detail']}")
                if not health["content_available"]:
                    raise RuntimeError(
                        f"本地知识目录可写、IMA 已连接且可检索，但 AI 无可用正文：{health['detail']}"
                    )
                return f"本地知识目录可写，IMA 知识读取正常：{health['detail']}"
            return "数据目录可写"

        if module_id == "spreadsheet":
            try:
                node, _modules = SpreadsheetService(config.get("spreadsheet", {}))._runtime()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(str(exc)) from None
            return f"表格运行环境已就绪: {Path(node).name} + @oai/artifact-tool"

        if module_id == "voice":
            if importlib.util.find_spec("whisper") is None:
                return "钉钉原生语音识别可用；本地 Whisper 未安装（可选备用）"
            ffmpeg = shutil.which("ffmpeg")
            return "钉钉原生语音识别可用；Whisper 已就绪" + ("，ffmpeg 已就绪" if ffmpeg else "，未检测到 ffmpeg")

        if module_id == "vision":
            if not self._configured("vision", config):
                raise ValueError("未配置支持图片输入的模型")
            return "图片模型路由已配置"

        if module_id == "aitable":
            if not config.get("ticket", {}).get("enabled", True):
                raise ValueError("工单模块已停用，无法启用智能表格同步")
            settings = self.settings_store.load() if self.settings_store else {}
            sync = settings.get("aitable_ticket_sync", {})
            if not sync.get("base_id") or not sync.get("table_id"):
                raise ValueError("请先填写智能表格 Base ID 和 Table ID")
            executable = str(sync.get("dws_executable") or "dws")
            if not (shutil.which(executable) or Path(executable).is_file()):
                raise ValueError("未检测到 dws 命令")
            return "智能表格基础配置完整"

        return "模块配置有效"
