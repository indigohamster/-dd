"""本机企业管理后台：无登录，仅监听 127.0.0.1。"""

import csv
import io
import os
import sys
import threading
import webbrowser
from pathlib import Path

import requests
import yaml
from flask import Flask, Response, jsonify, request, send_from_directory

from utils.config import DEFAULT_CONFIG_PATH
from utils.console import BOT_VERSION, _mask_secret, _update_llm_config, _update_yaml_section_config, bot_status
from utils.admin_settings import AdminSettingsStore
from utils.config_history import ConfigHistory
from utils.connector_executor import HTTPConnectorExecutor
from modules.ticket import TicketManager
from modules.knowledge_loop import KnowledgeLoopStore
from modules.ops_dashboard import DailyOpsDashboard
from utils.task_queue import PersistentTaskQueue
from utils.dingtalk_api import DingTalkAPI
from utils.ticket_notifier import TicketStatusNotifier
from utils.observability import ObservabilityStore
from utils.aitable_ticket_sync import TicketAITableSync, AITableSyncError


def _runtime_root() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent


def _asset_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "admin_ui"
    return Path(__file__).parent.parent / "admin_ui"


def create_admin_app(config_path=None, settings_path=None):
    config_file = Path(config_path).resolve() if config_path else Path(DEFAULT_CONFIG_PATH).resolve()
    store = AdminSettingsStore(settings_path)
    history = ConfigHistory(config_file, store.path)
    assets = _asset_root()
    app = Flask(__name__, static_folder=None)
    service_lock = threading.RLock()
    services = {}

    def read_config():
        return yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}

    def ticket_manager():
        if "tickets" not in services:
            with service_lock:
                if "tickets" not in services:
                    cfg = read_config().get("ticket", {})
                    db_path = Path(os.path.expandvars(cfg.get("database_path", "data/tickets.db")))
                    if not db_path.is_absolute():
                        db_path = _runtime_root() / db_path
                    services["tickets"] = TicketManager(
                        str(db_path), backup_enabled=False, integrity_check=True,
                    )
        return services["tickets"]

    def knowledge_store():
        if "knowledge" not in services:
            with service_lock:
                if "knowledge" not in services:
                    cfg = read_config().get("knowledge_loop", {})
                    db_path = Path(os.path.expandvars(cfg.get("database_path", "data/knowledge_loop.db")))
                    export_dir = Path(os.path.expandvars(cfg.get("export_dir", "data/knowledge_exports")))
                    if not db_path.is_absolute():
                        db_path = _runtime_root() / db_path
                    if not export_dir.is_absolute():
                        export_dir = _runtime_root() / export_dir
                    services["knowledge"] = KnowledgeLoopStore(db_path, export_dir)
        return services["knowledge"]

    def task_store():
        if "tasks" not in services:
            with service_lock:
                if "tasks" not in services:
                    cfg = read_config().get("async_tasks", {})
                    db_path = Path(os.path.expandvars(cfg.get("database_path", "data/tasks.db")))
                    if not db_path.is_absolute():
                        db_path = _runtime_root() / db_path
                    # 后台只用于查询和取消排队任务，不承载机器人执行线程。
                    services["tasks"] = PersistentTaskQueue(db_path, max_workers=1, recover_incomplete=False)
        return services["tasks"]

    def observability_store():
        if "observability" not in services:
            with service_lock:
                if "observability" not in services:
                    cfg = read_config().get("observability", {})
                    db_path = Path(os.path.expandvars(cfg.get("database_path", "data/observability.db")))
                    if not db_path.is_absolute():
                        db_path = _runtime_root() / db_path
                    services["observability"] = ObservabilityStore(db_path, cfg.get("retention_days", 30))
        return services["observability"]

    def ticket_notifier():
        if "notifier" not in services:
            with service_lock:
                if "notifier" not in services:
                    data = read_config()
                    enabled = data.get("ticket_notifications", {}).get("enabled", True)
                    creds = data.get("credentials", {})
                    api = None
                    if enabled and creds.get("client_id") and creds.get("client_secret"):
                        api = DingTalkAPI(creds["client_id"], creds["client_secret"], str(creds.get("agent_id", "")))
                    services["notifier"] = TicketStatusNotifier(api, app.logger, observability_store())
        return services["notifier"]

    def aitable_sync():
        settings = store.load().get("aitable_ticket_sync", {})
        return TicketAITableSync(
            ticket_manager(), settings,
            _runtime_root() / "data" / "aitable_ticket_sync.json", app.logger,
        )

    def notify_ticket_async(ticket, action, detail=""):
        if not ticket:
            return
        notifier = ticket_notifier()
        if not notifier.api:
            return
        threading.Thread(
            target=notifier.notify,
            args=(ticket, action, "管理后台", detail),
            name=f"ticket-notify-{ticket['ticket_id']}", daemon=True,
        ).start()

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

    @app.get("/api/dashboard/daily")
    def daily_dashboard():
        dashboard = DailyOpsDashboard(ticket_manager(), knowledge_store())
        return jsonify(dashboard.build(request.args.get("date") or None))

    @app.get("/api/monitor/summary")
    def monitor_summary():
        summary = observability_store().summary()
        tasks = task_store().list_tasks(limit=500)
        summary["tasks_queued"] = sum(t["status"] == "queued" for t in tasks)
        summary["tasks_running"] = sum(t["status"] in ("running", "cancellation_requested") for t in tasks)
        summary["tasks_failed"] = sum(t["status"] in ("failed", "interrupted") for t in tasks)
        summary["runtime"] = bot_status.snapshot()
        return jsonify(summary)

    @app.get("/api/monitor/events")
    def monitor_events():
        return jsonify(observability_store().list_events(
            status=request.args.get("status", ""), category=request.args.get("category", ""),
            limit=request.args.get("limit", 100),
        ))

    @app.get("/api/monitor/notifications")
    def monitor_notifications():
        return jsonify(observability_store().list_deliveries(
            status=request.args.get("status", ""), limit=request.args.get("limit", 100),
        ))

    @app.post("/api/monitor/notifications/<int:delivery_id>/retry")
    def retry_notification(delivery_id):
        delivery = observability_store().get_delivery(delivery_id)
        if not delivery:
            return jsonify({"message": "通知记录不存在"}), 404
        if delivery["status"] != "failed":
            return jsonify({"message": "只能重试失败的通知"}), 409
        threading.Thread(target=ticket_notifier().retry, args=(delivery_id,),
                         name=f"notify-retry-{delivery_id}", daemon=True).start()
        return jsonify({"ok": True, "message": "已提交重试"})

    @app.get("/api/monitor/health")
    def monitor_health():
        data, state = read_config(), bot_status.snapshot()
        sheet = data.get("spreadsheet", {})
        checks = [
            {"component": "钉钉 Stream", "ok": bool(state["stream_connected"]), "detail": "已连接" if state["stream_connected"] else "未连接或后台独立运行"},
            {"component": "钉钉 OpenAPI", "ok": bool(state["dtalk_connected"]), "detail": "已验证" if state["dtalk_connected"] else "未验证"},
            {"component": "IMA 知识库", "ok": bool(state["ima_connected"] or not state["ima_enabled"]), "detail": "已连接" if state["ima_connected"] else "未启用或未连接"},
            {"component": "AI API", "ok": bool(state["llm_connected"] or not state["llm_enabled"]), "detail": "已连接" if state["llm_connected"] else "未启用或未连接"},
            {"component": "工单数据库", "ok": Path(ticket_manager().db_path).exists(), "detail": ticket_manager().db_path},
            {"component": "任务数据库", "ok": Path(task_store().db_path).exists(), "detail": task_store().db_path},
            {"component": "Node.js 表格环境", "ok": Path(os.path.expandvars(sheet.get("node_executable", ""))).is_file(), "detail": sheet.get("node_executable", "未配置")},
        ]
        return jsonify(checks)

    @app.get("/api/tasks")
    def list_background_tasks():
        return jsonify(task_store().list_tasks(
            status=request.args.get("status", ""), limit=request.args.get("limit", 200),
        ))

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_background_task(task_id):
        if not task_store().cancel(task_id):
            return jsonify({"ok": False, "message": "只能取消仍在排队的任务"}), 409
        return jsonify({"ok": True, "message": "任务已取消"})

    @app.get("/api/spreadsheet")
    def get_spreadsheet_settings():
        return jsonify(read_config().get("spreadsheet", {}))

    @app.put("/api/spreadsheet")
    def save_spreadsheet_settings():
        payload = request.get_json(silent=True) or {}
        allowed = {"enabled", "max_file_mb", "timeout", "max_concurrent", "queue_timeout", "verify_render", "node_executable", "node_modules_path"}
        history.record("spreadsheet", "修改企业文件助手配置")
        for field in allowed:
            if field in payload and payload[field] not in (None, ""):
                _update_yaml_section_config(config_file, "spreadsheet", field, payload[field])
        return jsonify({"ok": True, "message": "文件助手配置已保存，重启机器人后生效"})

    @app.get("/api/aitable-ticket-sync")
    def get_aitable_ticket_sync():
        return jsonify(store.load().get("aitable_ticket_sync", {}))

    @app.put("/api/aitable-ticket-sync")
    def save_aitable_ticket_sync():
        payload = request.get_json(silent=True) or {}
        allowed = {"enabled", "dws_executable", "base_id", "table_id",
                   "sync_interval_minutes", "field_mapping"}
        data = store.load()
        current = dict(data.get("aitable_ticket_sync", {}))
        current.update({key: payload[key] for key in allowed if key in payload})
        history.record("aitable_ticket_sync", "修改工单与钉钉 AI 表格同步配置")
        data["aitable_ticket_sync"] = current
        store.save(data)
        return jsonify({"ok": True, "data": current})

    @app.post("/api/aitable-ticket-sync/test")
    def test_aitable_ticket_sync():
        try:
            aitable_sync().test_connection()
            return jsonify({"ok": True, "message": "AI 表格连接成功，已读取字段结构"})
        except AITableSyncError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 502

    @app.post("/api/aitable-ticket-sync/sync")
    def run_aitable_ticket_sync():
        try:
            result = aitable_sync().sync_all()
            return jsonify({"ok": result["failed"] == 0, "result": result})
        except AITableSyncError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 502

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
        history.record("llm", "修改 AI API 配置")
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
        data = store.load()
        data.pop("models", None)
        safe_connectors = []
        for connector in data.get("connectors", []):
            item = dict(connector)
            item["has_auth_value"] = bool(item.get("auth_value"))
            item["auth_value_masked"] = _mask_secret(str(item.get("auth_value") or ""))
            item.pop("auth_value", None)
            safe_connectors.append(item)
        data["connectors"] = safe_connectors
        return jsonify(data)

    @app.put("/api/settings/<section>")
    def save_settings(section):
        if section not in ("connectors", "people", "response_style", "model_routing"):
            return jsonify({"ok": False, "message": "未知配置项"}), 404
        data = store.load()
        payload = request.get_json(silent=True)
        history.record(section, f"修改 {section} 配置")
        if section == "connectors":
            existing = {item.get("id"): item for item in data.get("connectors", [])}
            merged = []
            for connector in payload or []:
                item = dict(connector)
                item.pop("auth_value_masked", None)
                item.pop("has_auth_value", None)
                if not item.get("auth_value") and item.get("id") in existing:
                    item["auth_value"] = existing[item["id"]].get("auth_value", "")
                merged.append(item)
            payload = merged
        data[section] = payload
        store.save(data)
        return jsonify({"ok": True, "data": data[section]})

    @app.post("/api/connectors/test")
    def test_connector():
        connector = request.get_json(silent=True) or {}
        if connector.get("type") in ("dingtalk", "ima"):
            return jsonify({"ok": True, "message": "内置连接器配置存在；实际连接状态请查看总览"})
        try:
            result = HTTPConnectorExecutor.execute(connector, {"input": connector.get("test_input", "test")}, timeout=8)
            return jsonify({"ok": True, "message": f"HTTP {result['status_code']}", "result": result["data"]})
        except Exception as exc:
            return jsonify({"ok": False, "message": f"连接失败：{str(exc)[:160]}"}), 502

    @app.post("/api/connectors/execute")
    def execute_connector():
        payload = request.get_json(silent=True) or {}
        connector = next((c for c in store.load().get("connectors", []) if c.get("id") == payload.get("id")), None)
        if not connector or connector.get("type") != "http" or not connector.get("enabled", True):
            return jsonify({"ok": False, "message": "连接器不存在或未启用"}), 404
        try:
            result = HTTPConnectorExecutor.execute(connector, payload.get("variables") or {})
            return jsonify({"ok": True, "result": result})
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)[:200]}), 502

    @app.get("/api/models")
    def get_models():
        data = store.load()
        models = []
        for profile in data.get("models", []):
            item = dict(profile)
            item["has_api_key"] = bool(item.get("api_key"))
            item["api_key_masked"] = _mask_secret(str(item.get("api_key") or ""))
            item.pop("api_key", None)
            models.append(item)
        return jsonify({"models": models, "routing": data.get("model_routing", {})})

    @app.put("/api/models")
    def save_models():
        payload = request.get_json(silent=True) or {}
        data = store.load()
        old = {m.get("id"): m for m in data.get("models", [])}
        merged = []
        for profile in payload.get("models", []):
            item = dict(profile)
            if not item.get("api_key") and item.get("id") in old:
                item["api_key"] = old[item["id"]].get("api_key", "")
            merged.append(item)
        history.record("models", "修改多模型与主备路由")
        data["models"] = merged
        data["model_routing"] = payload.get("routing", data.get("model_routing", {}))
        store.save(data)
        return jsonify({"ok": True})

    @app.post("/api/models/test")
    def test_model():
        profile = request.get_json(silent=True) or {}
        existing = next((m for m in store.load().get("models", []) if m.get("id") == profile.get("id")), {})
        key = profile.get("api_key") or existing.get("api_key") or read_config().get("llm", {}).get("api_key")
        llm = read_config().get("llm", {})
        test_payload = {**llm, **profile, "api_key": key}
        try:
            response = requests.post(
                str(test_payload.get("base_url", "")).rstrip("/") + test_payload.get("chat_completions_path", "/v1/chat/completions"),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": test_payload.get("model"), "messages": [{"role": "user", "content": "仅回复 OK"}], "max_tokens": 8}, timeout=20)
            response.raise_for_status()
            return jsonify({"ok": True, "message": "模型连接成功"})
        except Exception as exc:
            return jsonify({"ok": False, "message": f"模型连接失败：{str(exc)[:160]}"}), 502

    @app.get("/api/tickets")
    def list_tickets():
        filter_type = request.args.get("filter", "all")
        field, value = request.args.get("field", "").strip(), request.args.get("value", "").strip()
        limit = min(int(request.args.get("limit", 200)), 500)
        try:
            tickets = ticket_manager().search_tickets_exact(field, value, limit) if field and value else ticket_manager().list_all_tickets(filter_type, limit=limit)
        except ValueError as exc:
            return jsonify({"message": str(exc)}), 400
        return jsonify(tickets)

    @app.get("/api/tickets/export")
    def export_tickets():
        manager = ticket_manager()
        field, value = request.args.get("field", "").strip(), request.args.get("value", "").strip()
        try:
            rows = manager.search_tickets_exact(field, value, 500) if field and value else manager.list_all_tickets(request.args.get("filter", "all"), 500)
        except ValueError as exc:
            return jsonify({"message": str(exc)}), 400
        columns = ["ticket_id", "title", "description", "category", "priority", "status", "creator_id",
                   "creator_nick", "creator_staff_id", "assignee_id", "assignee_nick", "resolution",
                   "conv_id", "conv_title", "created_at", "updated_at", "resolved_at"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        body = "\ufeff" + output.getvalue()
        return Response(body, content_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": "attachment; filename=tickets-export.csv",
        })

    @app.post("/api/tickets/import")
    def import_tickets():
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename.lower().endswith(".csv"):
            return jsonify({"message": "请选择 CSV 文件"}), 400
        raw = uploaded.stream.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            return jsonify({"message": "导入文件不能超过 5MB"}), 413
        try:
            text = raw.decode("utf-8-sig")
            records = list(csv.DictReader(io.StringIO(text)))
            if not records:
                return jsonify({"message": "CSV 没有可导入的数据"}), 400
            result = ticket_manager().import_tickets(records)
            return jsonify({"ok": True, **result})
        except UnicodeDecodeError:
            return jsonify({"message": "CSV 必须使用 UTF-8 编码"}), 400
        except Exception as exc:
            return jsonify({"message": f"导入失败：{str(exc)[:160]}"}), 400

    @app.get("/api/tickets/history")
    def ticket_history():
        try:
            rows = ticket_manager().search_history(
                ticket_id=request.args.get("ticket_id", ""), action=request.args.get("action", ""),
                operator_id=request.args.get("operator_id", ""), date_from=request.args.get("date_from", ""),
                date_to=request.args.get("date_to", ""), limit=request.args.get("limit", 200),
            )
            return jsonify(rows)
        except ValueError as exc:
            return jsonify({"message": str(exc)}), 400

    @app.get("/api/tickets/<ticket_id>")
    def ticket_detail(ticket_id):
        manager = ticket_manager()
        ticket = manager.get_ticket(ticket_id)
        if not ticket:
            return jsonify({"message": "工单不存在"}), 404
        return jsonify({"ticket": ticket, "logs": manager.get_ticket_logs(ticket_id, 100)})

    @app.patch("/api/tickets/<ticket_id>")
    def update_ticket(ticket_id):
        payload = request.get_json(silent=True) or {}
        manager = ticket_manager()
        action = payload.get("action")
        result = None
        notification_action = ""
        notification_detail = ""
        if action == "priority":
            result = manager.update_priority(ticket_id, payload.get("priority", "medium"))
        elif action == "assign":
            result = manager.assign_ticket(ticket_id, payload.get("assignee_id", "web"), payload.get("assignee_nick", "未命名"), "web-admin", "管理后台")
            notification_action = "assign"
            notification_detail = f"工单已分派给 {payload.get('assignee_nick', '未命名')}"
        elif action == "progress":
            progress_detail = payload.get("detail") or payload.get("resolution") or "运维人员已开始处理"
            result = manager.set_in_progress(ticket_id, "web-admin", "管理后台", progress_detail)
            notification_action = "progress"
            notification_detail = progress_detail
        elif action == "resolve":
            result = manager.resolve_ticket(ticket_id, payload.get("resolution", ""), "web-admin", "管理后台")
            notification_action = "resolved"
            notification_detail = payload.get("resolution", "")
            if result:
                knowledge_store().create_from_ticket(result)
        elif action == "close":
            result = manager.close_ticket(ticket_id, "web-admin", "管理后台", payload.get("detail", "管理后台关闭"))
            notification_action = "closed"
            notification_detail = payload.get("detail", "管理后台关闭")
        if not result:
            return jsonify({"ok": False, "message": "操作失败，可能不符合当前状态流转规则"}), 400
        if notification_action:
            notify_ticket_async(result, notification_action, notification_detail)
        observability_store().record_event("ticket", action, "success", actor_id="web-admin", detail=ticket_id)
        return jsonify({"ok": True, "ticket": result})

    @app.get("/api/config-history")
    def config_history():
        return jsonify(history.list())

    @app.post("/api/config-history/<version>/rollback")
    def rollback_config(version):
        try:
            history.rollback(version)
            return jsonify({"ok": True, "message": "配置已回滚，请重启机器人"})
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.get("/api/knowledge/candidates")
    def knowledge_candidates():
        return jsonify(knowledge_store().list_candidates(request.args.get("status") or None))

    @app.get("/api/knowledge/candidates/<candidate_id>")
    def knowledge_candidate(candidate_id):
        candidate = knowledge_store().get_candidate(candidate_id)
        if not candidate:
            return jsonify({"message": "知识草稿不存在"}), 404
        return jsonify({"candidate": candidate, "versions": knowledge_store().versions(candidate_id)})

    @app.put("/api/knowledge/candidates/<candidate_id>")
    def update_knowledge_candidate(candidate_id):
        candidate = knowledge_store().update_candidate(candidate_id, request.get_json(silent=True) or {})
        if not candidate:
            return jsonify({"message": "知识草稿不存在"}), 404
        return jsonify({"ok": True, "candidate": candidate})

    @app.post("/api/knowledge/candidates/<candidate_id>/<action>")
    def review_knowledge_candidate(candidate_id, action):
        store_ = knowledge_store()
        try:
            if action == "approve":
                result = store_.update_candidate(candidate_id, {"status": "approved"}, "approve")
                return jsonify({"ok": bool(result), "candidate": result})
            if action == "reject":
                result = store_.update_candidate(candidate_id, {"status": "rejected"}, "reject")
                return jsonify({"ok": bool(result), "candidate": result})
            if action == "publish":
                path = store_.publish(candidate_id)
                return jsonify({"ok": True, "message": "已生成 IMA 待同步 Markdown", "path": path})
            return jsonify({"message": "未知操作"}), 400
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.post("/api/knowledge/candidates/<candidate_id>/rollback/<int:version>")
    def rollback_knowledge_candidate(candidate_id, version):
        try:
            return jsonify({"ok": True, "candidate": knowledge_store().rollback(candidate_id, version)})
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.get("/api/knowledge/unresolved")
    def unresolved_knowledge():
        return jsonify(knowledge_store().unresolved())

    @app.get("/api/knowledge/metrics")
    def knowledge_metrics():
        return jsonify(knowledge_store().source_metrics())

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
