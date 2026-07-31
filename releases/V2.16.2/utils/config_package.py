"""Portable, secret-free configuration packages for OpsPilot.

The package deliberately excludes runtime completion state, credentials and
machine-specific absolute paths.  Blank credentials are preserved on import
only while the owning service identity is unchanged.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from utils.config import atomic_write_yaml, load_config
from utils.credential_scope import normalize_endpoint, same_credential_scope


SCHEMA = "opspilot-config"
FORMAT_VERSION = 1
_SECRET_NAMES = {
    "secret", "client_secret", "app_secret", "api_key", "password",
    "token", "access_token", "refresh_token", "auth_value", "private_key",
    "authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie",
}
_MACHINE_PATH_NAMES = {
    "node_executable", "node_modules_path", "dws_executable", "ffmpeg_path",
}
_PUBLIC_SECURITY_METADATA_NAMES = {
    "auth_type", "authentication_type", "has_api_key", "api_key_masked",
    "has_auth_value", "auth_value_masked", "credentials",
}
_SENSITIVE_NAME_PARTS = {
    "secret", "password", "passwd", "token", "key", "auth",
    "authorization", "credential", "signature", "cookie",
}
_SENSITIVE_COMPACT_MARKERS = {
    "apikey", "accesskey", "privatekey", "authkey", "authtoken",
    "accesstoken", "refreshtoken", "clientsecret", "appsecret",
    "authorization", "credential", "signature", "password", "passwd",
}
_PUBLIC_HEADER_VALUE_NAMES = {
    "accept", "accept-encoding", "accept-language", "content-language",
    "content-type", "user-agent", "x-api-version", "x-client-version",
    "x-requested-with", "x-tenant", "x-tenant-id",
}


def _is_secret_key(key: str) -> bool:
    name = str(key).strip().lower()
    if name in _PUBLIC_SECURITY_METADATA_NAMES:
        return False
    if name in _SECRET_NAMES or name.endswith(
        ("_secret", "_password", "_api_key", "_access_token", "_refresh_token", "_private_key")
    ):
        return True
    parts = {part for part in re.split(r"[^a-z0-9]+", name) if part}
    compact = re.sub(r"[^a-z0-9]", "", name)
    return bool(parts & _SENSITIVE_NAME_PARTS) or any(
        marker in compact for marker in _SENSITIVE_COMPACT_MARKERS
    )


def _header_value_is_public(name: str) -> bool:
    return str(name).strip().lower() in _PUBLIC_HEADER_VALUE_NAMES


def _looks_absolute_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return (
        Path(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or text.startswith(("\\\\", "//"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", text))
    )


def _sanitize_url(value: str) -> str:
    """Remove user-info and credential-like query values from a URL."""
    text = str(value or "")
    if "://" not in text:
        return text
    try:
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return text
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        query = [
            (name, "" if _is_secret_key(name) else item_value)
            for name, item_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        # OAuth-style fragments can also contain bearer material and are not
        # required for an HTTP API endpoint, so public packages omit them.
        return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), ""))
    except (TypeError, ValueError):
        return text.rsplit("@", 1)[-1] if "@" in text else text


def _public_copy(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            name = str(child_key)
            if key == "setup" and name in {"completed", "completed_at"}:
                continue
            if (key == "headers" and not _header_value_is_public(name)) or _is_secret_key(name):
                result[name] = ""
            else:
                result[name] = _public_copy(child_value, name)
        return result
    if isinstance(value, list):
        return [_public_copy(item, key) for item in value]
    if isinstance(value, str):
        normalized = key.strip().lower()
        if normalized in {"headers", "body_template"}:
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return "{}" if normalized == "headers" else ""
            context = "headers" if normalized == "headers" else "body_template"
            return json.dumps(_public_copy(parsed, context), ensure_ascii=False)
        is_path_field = normalized in _MACHINE_PATH_NAMES or normalized.endswith(
            ("_path", "_dir", "_directory", "_executable")
        )
        if is_path_field and _looks_absolute_path(value):
            return ""
        return _sanitize_url(value)
    return copy.deepcopy(value)


def public_sanitized_copy(value: Any, key: str = "") -> Any:
    """Public API for producing a recursively secret-free response value."""
    return _public_copy(value, key)


def create_public_package(config_path, settings_store, *, app_version: str) -> dict[str, Any]:
    """Return a JSON-serializable configuration package without secrets."""
    config = load_config(config_path, allow_incomplete=True, apply_environment=False)
    settings = settings_store.load()
    return {
        "manifest": {
            "schema": SCHEMA,
            "format_version": FORMAT_VERSION,
            "app_version": str(app_version),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "includes_secrets": False,
        },
        "config": _public_copy(config),
        "settings": _public_copy(settings),
    }


def validate_package(package: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(package, Mapping):
        raise ValueError("配置包必须是 JSON 对象")
    manifest = package.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("schema") != SCHEMA:
        raise ValueError("不是有效的 OpsPilot 配置包")
    try:
        version = int(manifest.get("format_version"))
    except (TypeError, ValueError):
        raise ValueError("配置包版本无效") from None
    if version != FORMAT_VERSION:
        raise ValueError(f"暂不支持配置包版本 {version}")
    if manifest.get("includes_secrets") is not False:
        raise ValueError("配置包 manifest 必须明确标记 includes_secrets=false")
    config = package.get("config", {})
    settings = package.get("settings", {})
    if not isinstance(config, Mapping) or not isinstance(settings, Mapping):
        raise ValueError("配置包中的 config 和 settings 必须是对象")

    admin_ui = config.get("admin_ui")
    if admin_ui is not None:
        if not isinstance(admin_ui, Mapping):
            raise ValueError("config.admin_ui 必须是对象")
        if "port" in admin_ui:
            port = admin_ui.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("config.admin_ui.port 必须是 1 到 65535 的整数")
    for section in ("connectors", "models"):
        if section in settings:
            items = settings.get(section)
            if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
                raise ValueError(f"settings.{section} 必须是对象数组")
    for section in ("people", "service_catalog"):
        if section in settings:
            items = settings.get(section)
            if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
                raise ValueError(f"settings.{section} 必须是对象数组")
    for section in ("model_routing", "response_style", "aitable_ticket_sync"):
        if section in settings and not isinstance(settings.get(section), Mapping):
            raise ValueError(f"settings.{section} 必须是对象")
    return {
        "manifest": dict(manifest),
        "config": copy.deepcopy(dict(config)),
        "settings": copy.deepcopy(dict(settings)),
    }


def _deep_merge_preserving_secrets(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in update.items():
        name = str(key)
        if _is_secret_key(name) and value in (None, ""):
            continue
        if isinstance(value, Mapping) and isinstance(result.get(name), Mapping):
            result[name] = _deep_merge_preserving_secrets(result[name], value)
        else:
            result[name] = copy.deepcopy(value)
    return result


def _item_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def _same_connector_identity(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return (
        bool(_item_id(current))
        and _item_id(current) == _item_id(candidate)
        and str(current.get("type") or "").strip().lower()
        == str(candidate.get("type") or "").strip().lower()
        and normalize_endpoint(_sanitize_url(str(current.get("url") or "")))
        == normalize_endpoint(_sanitize_url(str(candidate.get("url") or "")))
    )


def _same_model_identity(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return (
        bool(_item_id(current))
        and _item_id(current) == _item_id(candidate)
        and str(current.get("provider") or "").strip().lower()
        == str(candidate.get("provider") or "").strip().lower()
        and normalize_endpoint(_sanitize_url(str(current.get("base_url") or "")))
        == normalize_endpoint(_sanitize_url(str(candidate.get("base_url") or "")))
    )


def _header_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return copy.deepcopy(dict(parsed)) if isinstance(parsed, Mapping) else {}
    return {}


def _merge_connector_headers(current: Any, candidate: Any) -> Any:
    """Preserve blank/missing sensitive headers only for the same connector."""
    old_headers = _header_mapping(current)
    new_headers = _header_mapping(candidate)
    old_by_name = {str(name).lower(): (name, value) for name, value in old_headers.items()}
    new_by_name = {str(name).lower(): name for name in new_headers}
    for lower_name, (old_name, old_value) in old_by_name.items():
        if _header_value_is_public(lower_name):
            continue
        candidate_name = new_by_name.get(lower_name)
        if candidate_name is None:
            new_headers[old_name] = copy.deepcopy(old_value)
        elif new_headers.get(candidate_name) in (None, ""):
            new_headers[candidate_name] = copy.deepcopy(old_value)
    if isinstance(candidate, str):
        return json.dumps(new_headers, ensure_ascii=False)
    return new_headers


def merge_connector_headers_preserving_secrets(current: Any, candidate: Any) -> Any:
    """Public API used by admin saves to keep masked headers on one endpoint."""
    return _merge_connector_headers(current, candidate)


def _merge_secret_items(current: Any, candidate: Any, *, kind: str) -> list[dict[str, Any]]:
    existing = {
        _item_id(item): item for item in current or []
        if isinstance(item, Mapping) and _item_id(item)
    }
    merged: list[dict[str, Any]] = []
    for raw_item in candidate or []:
        item = dict(raw_item)
        old = existing.get(_item_id(item))
        same_identity = bool(old) and (
            _same_connector_identity(old, item)
            if kind == "connectors" else _same_model_identity(old, item)
        )
        result = _deep_merge_preserving_secrets(old if same_identity else {}, item)
        if kind == "connectors" and same_identity and "headers" in item:
            result["headers"] = _merge_connector_headers(old.get("headers"), item.get("headers"))
        merged.append(result)
    return merged


def _merge_config(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = _deep_merge_preserving_secrets(current, incoming)

    old_credentials = current.get("credentials", {})
    new_credentials = merged.get("credentials", {})
    if isinstance(old_credentials, Mapping) and isinstance(new_credentials, Mapping):
        if str(old_credentials.get("client_id") or "") != str(new_credentials.get("client_id") or ""):
            merged.setdefault("credentials", {})["client_secret"] = ""

    old_llm = current.get("llm", {})
    new_llm = merged.get("llm", {})
    if isinstance(old_llm, Mapping) and isinstance(new_llm, Mapping):
        if not same_credential_scope(old_llm, new_llm):
            merged.setdefault("llm", {})["api_key"] = ""

    # IMA credentials are scoped to its client ID just like DingTalk app
    # credentials, so the same cross-tenant rule applies.
    old_ima = current.get("ima", {})
    new_ima = merged.get("ima", {})
    if isinstance(old_ima, Mapping) and isinstance(new_ima, Mapping):
        if str(old_ima.get("client_id") or "") != str(new_ima.get("client_id") or ""):
            merged.setdefault("ima", {})["api_key"] = ""
    return merged


def _merge_settings(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = _deep_merge_preserving_secrets(current, incoming)
    for section in ("connectors", "models"):
        if section in incoming:
            merged[section] = _merge_secret_items(
                current.get(section, []), incoming.get(section, []), kind=section
            )
    return merged


def preview_package(package: Mapping[str, Any], config_path, settings_store) -> dict[str, Any]:
    checked = validate_package(package)
    current_config = load_config(config_path, allow_incomplete=True, apply_environment=False)
    current_settings = settings_store.load()
    incoming_config = _public_copy(checked["config"])
    incoming_settings = _public_copy(checked["settings"])
    merged_config = _merge_config(current_config, incoming_config)
    merged_settings = _merge_settings(current_settings, incoming_settings)
    config_sections = sorted(
        key for key in incoming_config if current_config.get(key) != merged_config.get(key)
    )
    setting_sections = sorted(
        key for key in incoming_settings if current_settings.get(key) != merged_settings.get(key)
    )
    credentials = merged_config.get("credentials", {})
    llm = merged_config.get("llm", {})
    ima = merged_config.get("ima", {})
    missing = []
    if not credentials.get("client_id"):
        missing.append("钉钉 Client ID")
    if not credentials.get("client_secret"):
        missing.append("钉钉 Client Secret")
    if (
        llm.get("enabled")
        and not llm.get("api_key")
        and str(llm.get("provider", "")).lower() not in {"local", "ollama", "custom"}
    ):
        missing.append("AI API Key")
    if ima.get("enabled") and not ima.get("api_key"):
        missing.append("IMA API Key")
    return {
        "ok": True,
        "config_sections": config_sections,
        "settings_sections": setting_sections,
        "change_count": len(config_sections) + len(setting_sections),
        "missing": missing,
        "package_version": checked["manifest"].get("app_version", ""),
    }


def apply_package(package: Mapping[str, Any], config_path, settings_store) -> dict[str, Any]:
    """Merge a validated public package and roll back both stores on failure."""
    checked = validate_package(package)
    path = Path(config_path).expanduser().resolve()
    incoming_config = _public_copy(checked["config"])
    incoming_settings = _public_copy(checked["settings"])
    with settings_store.transaction():
        old_config = load_config(path, allow_incomplete=True, apply_environment=False)
        old_settings = settings_store.load()
        new_config = _merge_config(old_config, incoming_config)
        new_settings = _merge_settings(old_settings, incoming_settings)
        try:
            atomic_write_yaml(path, new_config)
            settings_store.save(new_settings)
        except Exception:
            atomic_write_yaml(path, old_config)
            settings_store.save(old_settings)
            raise
    return {"config": new_config, "settings": new_settings}
