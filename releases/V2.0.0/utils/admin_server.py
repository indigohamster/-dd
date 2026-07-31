"""本机企业管理后台：无登录，仅监听 127.0.0.1。"""

import os
import sys
import threading
import webbrowser
from pathlib import Path

import requests
import yaml
from flask import Flask, jsonify, request, send_from_directory

from utils.config import DEFAULT_CONFIG_PATH
from utils.console import BOT_VERSION, _mask_secret, _update_llm_config, bot_status
from utils.admin_settings import AdminSettingsStore


def _runtime_root() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent


def _asset_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "admin_ui"
    return Path(__file__).parent.parent / "admin_ui"


def create_admin_app(config_path=None, settings_path=None):
    config_file = Path(config_path).resolve() if config_path else Path(DEFAULT_CONFIG_PATH).resolve()
    store = AdminSettingsStore(settings_path)
    assets = _asset_root()
    app = Flask(__name__, static_folder=None)

    def read_config():
        return yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}

    @app.after_request
    def local_security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    def index():
        return send_from_directory(assets, "index.html")

    @app.get("/assets/<path:name>")
    def asset(name):
        return send_from_directory(assets, name)

    @app.get("/api/overview")
    def overview():
        state = bot_status.snapshot()
        settings = store.load()
        return jsonify({
            "version": BOT_VERSION,
            "status": state,
            "connector_count": len(settings["connectors"]),
            "people_count": len(settings["people"]),
        })

    @app.get("/api/llm")
    def get_llm():
        llm = dict(read_config().get("llm", {}))
        llm["api_key_masked"] = _mask_secret(str(llm.get("api_key") or ""))
        llm["has_api_key"] = bool(llm.get("api_key"))
        llm.pop("api_key", None)
        return jsonify(llm)

    @app.put("/api/llm")
    def save_llm():
        payload = request.get_json(silent=True) or {}
        allowed = {"enabled", "base_url", "chat_completions_path", "model", "api_key", "temperature", "max_tokens", "timeout"}
        for field in allowed:
            if field in payload and payload[field] not in (None, ""):
                _update_llm_config(config_file, field, payload[field])
        return jsonify({"ok": True, "message": "API 配置已保存，重启机器人后全部生效"})

    @app.post("/api/llm/test")
    def test_llm():
        cfg = read_config().get("llm", {})
        payload = request.get_json(silent=True) or {}
        base_url = str(payload.get("base_url") or cfg.get("base_url") or "").rstrip("/")
        api_path = str(payload.get("chat_completions_path") or cfg.get("chat_completions_path") or "/v1/chat/completions")
        api_key = payload.get("api_key") or cfg.get("api_key")
        model = payload.get("model") or cfg.get("model")
        if not base_url or not api_key or not model:
            return jsonify({"ok": False, "message": "请先填写地址、模型和 API Key"}), 400
        try:
            response = requests.post(
                base_url + (api_path if api_path.startswith("/") else "/" + api_path),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "仅回复 OK"}], "max_tokens": 8},
                timeout=min(int(payload.get("timeout") or cfg.get("timeout") or 30), 45),
            )
            response.raise_for_status()
            return jsonify({"ok": True, "message": "连接成功，模型已返回响应"})
        except Exception as exc:
            return jsonify({"ok": False, "message": f"连接失败：{str(exc)[:180]}"}), 502

    @app.get("/api/settings")
    def get_settings():
        return jsonify(store.load())

    @app.put("/api/settings/<section>")
    def save_settings(section):
        if section not in ("connectors", "people", "response_style"):
            return jsonify({"ok": False, "message": "未知配置项"}), 404
        data = store.load()
        payload = request.get_json(silent=True)
        data[section] = payload
        store.save(data)
        return jsonify({"ok": True, "data": data[section]})

    @app.post("/api/connectors/test")
    def test_connector():
        connector = request.get_json(silent=True) or {}
        if connector.get("type") in ("dingtalk", "ima"):
            return jsonify({"ok": True, "message": "内置连接器配置存在；实际连接状态请查看总览"})
        url = str(connector.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return jsonify({"ok": False, "message": "请填写有效的 HTTP 地址"}), 400
        try:
            response = requests.get(url, timeout=8)
            return jsonify({"ok": response.status_code < 500, "message": f"HTTP {response.status_code}"})
        except Exception as exc:
            return jsonify({"ok": False, "message": f"连接失败：{str(exc)[:160]}"}), 502

    return app


def start_admin_server(config_path=None, host="127.0.0.1", port=8765, auto_open=True, logger=None):
    app = create_admin_app(config_path)

    def serve():
        if logger:
            logger.info("可视化管理后台已启动: http://%s:%s", host, port)
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    thread = threading.Thread(target=serve, name="admin-ui", daemon=True)
    thread.start()
    if auto_open:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    return thread
