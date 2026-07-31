"""First-run setup workflow for the local administration console."""

from __future__ import annotations

import copy
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
import yaml

from utils.config import (
    atomic_write_yaml,
    atomic_update_config,
    is_runtime_configured,
    load_config,
)
from utils.module_registry import ModuleRegistry


PURPOSES = {"ops_service", "knowledge_assistant", "light_ticket", "custom"}
PURPOSE_ALIASES = {
    "ops": "ops_service", "operations": "ops_service", "it_ops": "ops_service",
    "knowledge": "knowledge_assistant", "assistant": "knowledge_assistant",
    "ticket": "light_ticket", "lightweight_ticket": "light_ticket",
}
PURPOSE_PUBLIC_NAMES = {
    "ops_service": "operations",
    "knowledge_assistant": "knowledge",
    "light_ticket": "ticket",
    "custom": "custom",
}
SECRET_PLACEHOLDERS = {"", "********", "***********", "••••••••", "__keep__", "unchanged"}


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _text(value: Any, limit: int = 2048) -> str:
    return str(value or "").strip()[:limit]


def _origin(value: Any) -> tuple[str, str, int | None]:
    parsed = urlparse(_text(value, 1000))
    try:
        port = parsed.port
    except ValueError:
        port = None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _wizard_provider(value: Any, base_url: Any) -> str:
    provider = _text(value, 40).lower()
    if provider in {"deepseek", "openai", "ollama", "custom", "none"}:
        return provider
    url = _text(base_url, 1000).lower()
    if "deepseek" in url:
        return "deepseek"
    if "openai.com" in url:
        return "openai"
    if "11434" in url or "ollama" in url:
        return "ollama"
    return "custom" if url else "deepseek"


class SetupService:
    """Persist a setup draft and promote it to the runtime configuration."""

    def __init__(self, config_path, settings_store=None, module_registry=None):
        self.config_path = Path(config_path).resolve()
        self.draft_path = self.config_path.with_suffix(".setup-draft.yaml")
        self.settings_store = settings_store
        self.modules = module_registry or ModuleRegistry(self.config_path, settings_store)
        self._lock = threading.RLock()
        # Wizard secrets intentionally live only in memory until completion.
        # A process restart asks the user to paste them again instead of leaving
        # a second plaintext copy beside config.yaml.
        self._draft_secrets = {"client_secret": "", "api_key": ""}
        self._draft_secret_state = {"client_secret": "config", "api_key": "config"}

    def _config_draft(self) -> dict[str, Any]:
        config = load_config(
            self.config_path, allow_incomplete=True, apply_environment=False
        )
        credentials = config.get("credentials", {})
        llm = config.get("llm", {})
        setup = config.get("setup", {})
        people = []
        if self.settings_store:
            people = self.settings_store.load().get("people", [])
        return {
            "purpose": _text(setup.get("purpose") or "ops_service", 40),
            "dingtalk": {
                "client_id": _text(credentials.get("client_id"), 256),
                "client_secret": _text(credentials.get("client_secret"), 1024),
                "app_id": _text(credentials.get("app_id"), 256),
                "agent_id": _text(credentials.get("agent_id"), 256),
                "corp_id": _text(credentials.get("corp_id"), 256),
            },
            "ai": {
                "enabled": bool(llm.get("enabled", False)),
                "provider": _wizard_provider(llm.get("provider"), llm.get("base_url")),
                "base_url": _text(llm.get("base_url"), 1000),
                "chat_completions_path": _text(
                    llm.get("chat_completions_path") or "/v1/chat/completions", 500
                ),
                "model": _text(llm.get("model"), 256),
                "api_key": _text(llm.get("api_key"), 2048),
                "timeout": llm.get("timeout", 30),
            },
            "modules": {
                item["id"]: bool(item["enabled"] and item["available"])
                for item in self.modules.list_modules()
            },
            "team": copy.deepcopy(people),
        }

    def _load_stored_draft(self) -> dict[str, Any]:
        if not self.draft_path.exists():
            return {}
        try:
            value = yaml.safe_load(self.draft_path.read_text(encoding="utf-8")) or {}
            if not isinstance(value, dict):
                return {}
            saved_state = value.pop("_secret_state", {})
            if isinstance(saved_state, Mapping):
                for key in self._draft_secret_state:
                    if saved_state.get(key) in {"config", "memory", "cleared"}:
                        self._draft_secret_state[key] = saved_state[key]
            # Migrate an older draft written before secrets became memory-only.
            dingtalk = value.get("dingtalk", {})
            ai = value.get("ai", {})
            migrated = False
            if isinstance(dingtalk, dict):
                secret = dingtalk.pop("client_secret", None)
                if secret:
                    self._draft_secrets["client_secret"] = str(secret)
                    self._draft_secret_state["client_secret"] = "memory"
                    migrated = True
            if isinstance(ai, dict):
                secret = ai.pop("api_key", None)
                if secret:
                    self._draft_secrets["api_key"] = str(secret)
                    self._draft_secret_state["api_key"] = "memory"
                    migrated = True
            if migrated:
                persisted = copy.deepcopy(value)
                persisted["_secret_state"] = copy.deepcopy(self._draft_secret_state)
                atomic_write_yaml(self.draft_path, persisted)
            return value
        except (OSError, ValueError, yaml.YAMLError):
            return {}

    def _current_draft(self) -> dict[str, Any]:
        draft = _merge(self._config_draft(), self._load_stored_draft())
        if self._draft_secret_state["client_secret"] != "config":
            draft.setdefault("dingtalk", {})["client_secret"] = (
                self._draft_secrets["client_secret"]
                if self._draft_secret_state["client_secret"] == "memory" else ""
            )
        if self._draft_secret_state["api_key"] != "config":
            draft.setdefault("ai", {})["api_key"] = (
                self._draft_secrets["api_key"]
                if self._draft_secret_state["api_key"] == "memory" else ""
            )
        return draft

    @staticmethod
    def _unwrap(payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("请求内容必须是 JSON 对象")
        if isinstance(payload.get("draft"), Mapping):
            draft = dict(payload["draft"])
            if "step" in payload:
                draft["step"] = payload["step"]
            return draft
        return dict(payload)

    def _normalize_update(self, payload: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
        allowed_top = {"purpose", "dingtalk", "ai", "modules", "team", "step"}
        unknown = set(payload) - allowed_top
        if unknown:
            raise ValueError(f"不支持的向导字段: {', '.join(sorted(unknown))}")
        update: dict[str, Any] = {}
        if "purpose" in payload:
            purpose = _text(payload.get("purpose"), 40).lower()
            purpose = PURPOSE_ALIASES.get(purpose, purpose)
            if purpose not in PURPOSES:
                raise ValueError("未知的使用场景")
            update["purpose"] = purpose

        if "dingtalk" in payload:
            value = payload["dingtalk"]
            if not isinstance(value, Mapping):
                raise ValueError("dingtalk 必须是对象")
            aliases = {
                "clientId": "client_id", "clientSecret": "client_secret",
                "appId": "app_id", "agentId": "agent_id", "corpId": "corp_id",
            }
            normalized = {aliases.get(key, key): item for key, item in value.items()}
            allowed = {"client_id", "client_secret", "app_id", "agent_id", "corp_id"}
            unknown = set(normalized) - allowed - {"has_client_secret", "clear_client_secret"}
            if unknown:
                raise ValueError(f"不支持的钉钉字段: {', '.join(sorted(unknown))}")
            result = {}
            for key in allowed:
                if key not in normalized:
                    continue
                candidate = _text(normalized[key], 1024 if key == "client_secret" else 256)
                if key == "client_secret" and candidate.lower() in SECRET_PLACEHOLDERS:
                    # Empty or masked secrets mean "keep the existing secret".
                    continue
                result[key] = candidate
            if normalized.get("clear_client_secret") is True:
                result["client_secret"] = ""
            elif (
                "client_id" in result
                and result["client_id"] != _text(current.get("dingtalk", {}).get("client_id"), 256)
                and "client_secret" not in result
            ):
                # A secret belongs to one application identity. Never carry it
                # over silently when the Client ID changes.
                result["client_secret"] = ""
            update["dingtalk"] = result

        if "ai" in payload:
            value = payload["ai"]
            if not isinstance(value, Mapping):
                raise ValueError("ai 必须是对象")
            aliases = {
                "type": "provider", "baseUrl": "base_url", "apiKey": "api_key",
                "apiPath": "chat_completions_path", "api_path": "chat_completions_path",
            }
            normalized = {aliases.get(key, key): item for key, item in value.items()}
            allowed = {
                "enabled", "provider", "base_url", "chat_completions_path",
                "model", "api_key", "timeout",
            }
            unknown = set(normalized) - allowed - {"has_api_key", "clear_api_key"}
            if unknown:
                raise ValueError(f"不支持的 AI 字段: {', '.join(sorted(unknown))}")
            result = {}
            for key in allowed:
                if key not in normalized:
                    continue
                if key == "enabled":
                    result[key] = bool(normalized[key])
                elif key == "timeout":
                    try:
                        result[key] = min(max(int(normalized[key]), 2), 300)
                    except (TypeError, ValueError):
                        raise ValueError("AI 超时时间必须是 2 到 300 秒的整数") from None
                else:
                    candidate = _text(normalized[key], 2048 if key == "api_key" else 1000)
                    if key == "api_key" and candidate.lower() in SECRET_PLACEHOLDERS:
                        continue
                    result[key] = candidate
            current_ai = current.get("ai", {})
            provider_changed = (
                "provider" in result
                and _text(result["provider"]).lower() != _text(current_ai.get("provider")).lower()
            )
            origin_changed = (
                "base_url" in result
                and _origin(result["base_url"]) != _origin(current_ai.get("base_url"))
            )
            if normalized.get("clear_api_key") is True:
                result["api_key"] = ""
            elif (provider_changed or origin_changed) and "api_key" not in result:
                # API keys are scoped to a provider/origin. Carrying the old
                # value to a new endpoint could disclose it to another vendor.
                result["api_key"] = ""
            update["ai"] = result

        if "modules" in payload:
            value = payload["modules"]
            if not isinstance(value, Mapping):
                raise ValueError("modules 必须是模块开关对象")
            normalized_modules = {}
            unknown = []
            availability = {
                item["id"]: bool(item["available"])
                for item in self.modules.list_modules()
            }
            for key, item in value.items():
                try:
                    definition = self.modules.get(str(key))
                except KeyError:
                    unknown.append(str(key))
                    continue
                # Vision can become available from the AI endpoint submitted in
                # this same wizard request, so preserve the user's selection and
                # let completion validate the merged candidate configuration.
                normalized_modules[definition.id] = bool(item) and (
                    definition.id == "vision" or availability.get(definition.id, True)
                )
            if unknown:
                raise ValueError(f"未知模块: {', '.join(sorted(unknown))}")
            update["modules"] = normalized_modules

        if "team" in payload:
            value = payload["team"]
            if isinstance(value, Mapping):
                container = value
                value = container.get("people", container.get("members", container.get("admins")))
                if value is None:
                    # Also accept a single administrator object in the wizard.
                    value = [container]
            if not isinstance(value, list):
                raise ValueError("team 必须是人员数组")
            if len(value) > 200:
                raise ValueError("最多可配置 200 名人员")
            people = []
            for item in value:
                if not isinstance(item, Mapping):
                    raise ValueError("人员信息必须是对象")
                person = {
                    "name": _text(item.get("name") or item.get("admin_name") or item.get("user_nick"), 80),
                    "staff_id": _text(item.get("staff_id") or item.get("admin_staff_id"), 128),
                    "user_id": _text(item.get("user_id"), 128),
                    "role": _text(item.get("role") or "工单管理员", 80),
                }
                if person["name"] or person["staff_id"] or person["user_id"]:
                    people.append(person)
            update["team"] = people

        if "step" in payload:
            try:
                update["step"] = min(max(int(payload["step"]), 1), 5)
            except (TypeError, ValueError):
                raise ValueError("step 必须是 1 到 5 的整数") from None
        return update

    @staticmethod
    def _safe_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
        safe = copy.deepcopy(dict(draft))
        safe.pop("_secret_state", None)
        safe["purpose"] = PURPOSE_PUBLIC_NAMES.get(
            str(safe.get("purpose") or ""), safe.get("purpose") or "operations"
        )
        dingtalk = safe.setdefault("dingtalk", {})
        client_secret = bool(dingtalk.pop("client_secret", ""))
        dingtalk["has_client_secret"] = client_secret
        ai = safe.setdefault("ai", {})
        api_key = bool(ai.pop("api_key", ""))
        ai["has_api_key"] = api_key
        team = safe.get("team", [])
        if isinstance(team, list):
            leader = team[0] if team else {}
            safe["team"] = {
                "admin_name": leader.get("name", ""),
                "admin_staff_id": leader.get("staff_id", ""),
                "user_id": leader.get("user_id", ""),
                "role": leader.get("role", "工单管理员"),
            }
        return safe

    def _missing(self, draft: Mapping[str, Any]) -> list[str]:
        missing = []
        dingtalk = draft.get("dingtalk", {})
        if not _text(dingtalk.get("client_id")):
            missing.append("dingtalk.client_id")
        if not _text(dingtalk.get("client_secret")):
            missing.append("dingtalk.client_secret")
        ai = draft.get("ai", {})
        if ai.get("enabled"):
            provider = _text(ai.get("provider") or "openai_compatible").lower()
            if not _text(ai.get("base_url")):
                missing.append("ai.base_url")
            if not _text(ai.get("model")):
                missing.append("ai.model")
            if provider not in {"local", "ollama", "custom", "none"} and not _text(ai.get("api_key")):
                missing.append("ai.api_key")
        return missing

    def _derived_step(self, draft: Mapping[str, Any], completed: bool) -> int:
        if completed:
            return 5
        if not draft.get("purpose"):
            return 1
        if any(item.startswith("dingtalk.") for item in self._missing(draft)):
            return 2
        explicit = draft.get("step")
        if isinstance(explicit, int):
            return explicit
        return 3 if draft.get("ai", {}).get("enabled") else 4

    @staticmethod
    def _redact_error(message: Any, draft: Mapping[str, Any]) -> str:
        rendered = str(message).replace("\r", " ").replace("\n", " ")
        for secret in (
            draft.get("dingtalk", {}).get("client_secret"),
            draft.get("ai", {}).get("api_key"),
        ):
            secret = str(secret or "")
            if secret:
                rendered = rendered.replace(secret, "***")
        return rendered[:240] or "验证失败"

    def status(self) -> dict[str, Any]:
        with self._lock:
            config = load_config(self.config_path, allow_incomplete=True)
            persisted_config = load_config(
                self.config_path, allow_incomplete=True, apply_environment=False
            )
            draft = self._current_draft()
            runtime_ready = is_runtime_configured(config)
            setup_marker = bool(persisted_config.get("setup", {}).get("completed"))
            completed = runtime_ready
            missing = self._missing(draft)
            if runtime_ready:
                missing = [item for item in missing if not item.startswith("dingtalk.")]
            if is_runtime_configured(persisted_config):
                credential_source = "config"
            elif runtime_ready:
                credential_source = "environment"
            else:
                credential_source = "missing"
            first_run = not setup_marker and not self.draft_path.exists() and not runtime_ready
            return {
                "completed": completed,
                "setup_required": not completed,
                "runtime_ready": runtime_ready,
                "setup_completed_marker": setup_marker,
                "credential_source": credential_source,
                "step": 1 if first_run and not completed else self._derived_step(draft, completed),
                "draft": self._safe_draft(draft),
                "modules": self.modules.list_modules(),
                "missing": missing,
                "config_exists": self.config_path.exists(),
                "restart_required": False,
            }

    def save_draft(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            body = self._unwrap(payload)
            current = self._current_draft()
            update = self._normalize_update(body, current)
            merged = _merge(current, update)
            if "client_secret" in update.get("dingtalk", {}):
                secret = str(update["dingtalk"].get("client_secret") or "")
                self._draft_secrets["client_secret"] = secret
                self._draft_secret_state["client_secret"] = "memory" if secret else "cleared"
            if "api_key" in update.get("ai", {}):
                secret = str(update["ai"].get("api_key") or "")
                self._draft_secrets["api_key"] = secret
                self._draft_secret_state["api_key"] = "memory" if secret else "cleared"
            persisted = copy.deepcopy(merged)
            persisted.setdefault("dingtalk", {}).pop("client_secret", None)
            persisted.setdefault("ai", {}).pop("api_key", None)
            persisted["_secret_state"] = copy.deepcopy(self._draft_secret_state)
            atomic_write_yaml(self.draft_path, persisted)
            return self._safe_draft(merged)

    @staticmethod
    def _validate_url(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AI 地址必须是有效的 http:// 或 https:// 地址")
        if parsed.username or parsed.password:
            raise ValueError("AI 地址不能包含用户名或密码，请使用 API Key 字段")

    def _validate_dingtalk(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        cfg = draft.get("dingtalk", {})
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            raise ValueError("请填写钉钉 Client ID 和 Client Secret")
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
            # DingTalk's legacy token endpoint puts credentials in query
            # parameters, so propagating a requests exception could disclose
            # the secret via its rendered URL.
            raise RuntimeError("钉钉连接失败，请核对凭证及网络后重试") from None
        return {"component": "dingtalk", "ok": True, "message": "钉钉连接成功"}

    def _validate_ai(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        cfg = draft.get("ai", {})
        if not cfg.get("enabled") or str(cfg.get("provider") or "").lower() == "none":
            return {"component": "ai", "ok": True, "skipped": True, "message": "已选择暂不启用 AI"}
        base_url = _text(cfg.get("base_url")).rstrip("/")
        self._validate_url(base_url)
        model = _text(cfg.get("model"))
        provider = _text(cfg.get("provider") or "openai_compatible").lower()
        api_key = _text(cfg.get("api_key"))
        if not model or (provider not in {"local", "ollama", "custom"} and not api_key):
            raise ValueError("请填写模型名称和 API Key")
        path = _text(cfg.get("chat_completions_path") or "/v1/chat/completions")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = requests.post(
                base_url + (path if path.startswith("/") else f"/{path}"),
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": "只回复 OK"}], "max_tokens": 8},
                timeout=min(max(int(cfg.get("timeout") or 30), 2), 45),
            )
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError("AI API 连接失败，请核对地址、模型、密钥及网络") from None
        return {"component": "ai", "ok": True, "message": "AI API 连接成功"}

    def _validate_modules(self, draft: Mapping[str, Any]) -> list[dict[str, Any]]:
        results = []
        selected = draft.get("modules", {})
        definitions = {item["id"]: item for item in self.modules.list_modules()}
        for module_id, enabled in selected.items():
            if not enabled:
                continue
            item = definitions.get(module_id)
            if not item:
                continue
            if module_id == "vision":
                ai = draft.get("ai", {})
                candidate_config = load_config(
                    self.config_path, allow_incomplete=True, apply_environment=False
                )
                candidate_config["llm"] = {
                    **candidate_config.get("llm", {}),
                    "enabled": bool(ai.get("enabled")),
                    "provider": ai.get("provider", ""),
                    "base_url": ai.get("base_url", ""),
                    "model": ai.get("model", ""),
                    "api_key": ai.get("api_key", ""),
                }
                configured = self.modules.is_configured("vision", candidate_config)
                result = {
                    "ok": configured,
                    "message": "图片模型配置完整" if configured else "图片识别需要先配置可用的 AI 模型",
                }
            elif module_id == "aitable":
                candidate_config = load_config(
                    self.config_path, allow_incomplete=True, apply_environment=False
                )
                if "tickets" in selected:
                    candidate_config.setdefault("ticket", {})["enabled"] = bool(selected["tickets"])
                configured = self.modules.is_configured("aitable", candidate_config)
                result = {
                    "ok": configured,
                    "message": (
                        "智能表格依赖配置完整" if configured
                        else "智能表格同步需要先启用工单并配置 dws、Base ID 和 Table ID"
                    ),
                }
            else:
                result = self.modules.test(module_id)
            results.append({
                **result,
                "component": f"module:{module_id}",
                "name": item["name"],
            })
        return results

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = self._unwrap(payload)
        requested_step = _text(payload.get("step") if isinstance(payload, Mapping) else "")
        body.pop("step", None)
        with self._lock:
            current = self._current_draft()
            update = self._normalize_update(body, current)
            draft = _merge(current, update)
        step = requested_step.lower() or "all"
        aliases = {"1": "purpose", "2": "dingtalk", "3": "ai", "4": "modules", "5": "all"}
        step = aliases.get(step, step)
        if step not in {"purpose", "dingtalk", "ai", "modules", "team", "all"}:
            raise ValueError("未知的向导步骤")

        results: list[dict[str, Any]] = []
        started = time.monotonic()
        checks = ["dingtalk", "ai", "modules", "team"] if step == "all" else [step]
        for check in checks:
            try:
                if check == "purpose":
                    if draft.get("purpose") not in PURPOSES:
                        raise ValueError("请选择使用场景")
                    results.append({"component": check, "ok": True, "message": "使用场景有效"})
                elif check == "dingtalk":
                    results.append(self._validate_dingtalk(draft))
                elif check == "ai":
                    results.append(self._validate_ai(draft))
                elif check == "modules":
                    results.extend(self._validate_modules(draft))
                elif check == "team":
                    results.append({"component": check, "ok": True, "message": "人员配置有效"})
            except Exception as exc:
                results.append({
                    "component": check,
                    "ok": False,
                    "message": self._redact_error(exc, draft),
                })
        return {
            "ok": all(item.get("ok") for item in results),
            "step": step,
            "results": results,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

    def _to_config_updates(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        dingtalk = draft.get("dingtalk", {})
        ai = draft.get("ai", {})
        updates: dict[str, Any] = {
            "credentials": {
                "client_id": _text(dingtalk.get("client_id"), 256),
                "client_secret": _text(dingtalk.get("client_secret"), 1024),
                "app_id": _text(dingtalk.get("app_id"), 256),
                "agent_id": _text(dingtalk.get("agent_id"), 256),
                "corp_id": _text(dingtalk.get("corp_id"), 256),
            },
            "llm": {
                "enabled": bool(ai.get("enabled", False)) and _text(ai.get("provider")).lower() != "none",
                "provider": _text(ai.get("provider") or "openai_compatible", 40),
                "base_url": _text(ai.get("base_url"), 1000),
                "chat_completions_path": _text(
                    ai.get("chat_completions_path") or "/v1/chat/completions", 500
                ),
                "model": _text(ai.get("model"), 256),
                "api_key": _text(ai.get("api_key"), 2048),
                "timeout": min(max(int(ai.get("timeout") or 30), 2), 300),
            },
            "setup": {
                "completed": True,
                "purpose": _text(draft.get("purpose") or "ops_service", 40),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        for module_id, enabled in draft.get("modules", {}).items():
            try:
                definition = self.modules.get(module_id)
            except KeyError:
                continue
            if definition.id == "aitable":
                continue
            updates.setdefault(definition.section, {})["enabled"] = bool(enabled)
        return updates

    def complete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            body = self._unwrap(payload)
            body.pop("step", None)
            current = self._current_draft()
            update = self._normalize_update(body, current)
            draft = _merge(current, update)
            missing = self._missing(draft)
            if missing:
                return {
                    "ok": False,
                    "message": "配置尚未完成",
                    "missing": missing,
                    "restart_required": False,
                }

            module_results = self._validate_modules(draft)
            failed_modules = [item for item in module_results if not item.get("ok")]
            if failed_modules:
                return {
                    "ok": False,
                    "message": "所选模块尚未就绪，请按提示修正后重试",
                    "results": module_results,
                    "missing": [item.get("component", "module") for item in failed_modules],
                    "restart_required": False,
                }

            def commit_settings_and_config():
                settings_before = None
                if self.settings_store is not None:
                    settings = self.settings_store.load()
                    settings_before = copy.deepcopy(settings)
                    if "aitable" in draft.get("modules", {}):
                        sync = dict(settings.get("aitable_ticket_sync", {}))
                        sync["enabled"] = bool(draft["modules"]["aitable"])
                        settings["aitable_ticket_sync"] = sync
                    existing_people = list(settings.get("people", []))
                    for person in copy.deepcopy(draft.get("team", [])):
                        identity = person.get("staff_id") or person.get("user_id") or person.get("name")
                        match = next((
                            index for index, current in enumerate(existing_people)
                            if identity and identity in {
                                current.get("staff_id"), current.get("user_id"), current.get("name")
                            }
                        ), None)
                        if match is None:
                            existing_people.append(person)
                        else:
                            existing_people[match] = {**existing_people[match], **person}
                    settings["people"] = existing_people
                    self.settings_store.save(settings)
                try:
                    # config.yaml is the final commit point observed by main.py.
                    atomic_update_config(self.config_path, self._to_config_updates(draft))
                except Exception:
                    if settings_before is not None:
                        try:
                            self.settings_store.save(settings_before)
                        except Exception:
                            pass
                    raise

            if self.settings_store is not None:
                with self.settings_store.transaction():
                    commit_settings_and_config()
            else:
                commit_settings_and_config()
            try:
                self.draft_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._draft_secrets = {"client_secret": "", "api_key": ""}
            self._draft_secret_state = {"client_secret": "config", "api_key": "config"}
            return {
                "ok": True,
                "completed": True,
                "message": "首次配置已保存，机器人将开始连接钉钉",
                "restart_required": False,
                "runtime_started": False,
            }
