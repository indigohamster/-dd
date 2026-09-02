"""本机企业管理后台：无登录，仅监听 127.0.0.1。"""

import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from waitress import create_server
from werkzeug.exceptions import HTTPException

from utils.config import DEFAULT_CONFIG_PATH, atomic_update_config, load_config
from utils.console import BOT_VERSION, _mask_secret, bot_status
from utils.admin_settings import AdminSettingsStore
from utils.config_history import ConfigHistory
from utils.connector_executor import HTTPConnectorExecutor
from utils.file_connector import SMBReadOnlyConnector
from modules.ticket import TicketManager
from modules.knowledge_loop import KnowledgeLoopStore
from modules.issue import IssueService
from modules.ops_dashboard import DailyOpsDashboard
from utils.task_queue import PersistentTaskQueue
from utils.dingtalk_api import DingTalkAPI
from utils.ticket_notifier import TicketStatusNotifier
from utils.observability import ObservabilityStore
from utils.aitable_ticket_sync import TicketAITableSync, AITableSyncError
from utils.network_diagnostics import NetworkDiagnostics
from utils.module_registry import ModuleRegistry
from utils.setup_service import SetupService
from utils.config_package import (
    apply_package,
    create_public_package,
    merge_connector_headers_preserving_secrets,
    preview_package,
    public_sanitized_copy,
)
from utils.credential_scope import (
    LOCAL_LLM_PROVIDERS,
    normalize_endpoint,
    resolve_scoped_api_key,
    same_credential_scope,
)


def _runtime_root() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent


def _asset_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "admin_ui"
    return Path(__file__).parent.parent / "admin_ui"


DEFAULT_KNOWLEDGE_EXPORT_DIR = "data/knowledge_exports"


def _resolve_knowledge_export_dir(path_value, runtime_root=None):
    """Normalize a configured export directory without allowing relative traversal."""
    raw = str(path_value or "").strip()
    configured = raw
    if not raw:
        raw = DEFAULT_KNOWLEDGE_EXPORT_DIR
        configured = ""
    if "\x00" in raw:
        raise ValueError("知识导出目录包含无效字符")

    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if ".." in candidate.parts:
        raise ValueError("知识导出目录不能包含“..”路径")

    root = Path(runtime_root or _runtime_root()).expanduser().resolve()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        stored = str(resolved)
    else:
        resolved = (root / candidate).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            raise ValueError("相对知识导出目录不能超出程序运行目录") from None
        stored = str(relative)
    return resolved, (stored if configured else "")


def _prepare_knowledge_export_dir(path_value, runtime_root=None, test_write=False):
    """Create and optionally write-test an export directory."""
    export_dir, configured = _resolve_knowledge_export_dir(path_value, runtime_root)
    if export_dir.exists() and not export_dir.is_dir():
        raise ValueError("知识导出路径指向文件，请选择目录")
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建知识导出目录：{exc}") from None

    if test_write:
        probe = export_dir / f".opspilot-write-test-{uuid.uuid4().hex}.tmp"
        try:
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("OpsPilot directory write test")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ValueError(f"知识导出目录不可写：{exc}") from None
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError as exc:
                raise ValueError(f"知识导出目录测试文件无法清理：{exc}") from None
    return export_dir, configured


def _select_windows_directory():
    if os.name != "nt":
        raise RuntimeError("当前系统不支持目录选择器，请手动输入路径")
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$shell=New-Object -ComObject Shell.Application;"
        "$folder=$shell.BrowseForFolder(0,'选择知识导出目录',65,0);"
        "if($folder){[Console]::Write($folder.Self.Path)}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法启动 Windows 目录选择器：{exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:200]
        raise RuntimeError(f"目录选择器运行失败：{detail or '未知错误'}")
    return (result.stdout or "").strip()


def _normalize_service_catalog(payload):
    """Validate and normalize the lightweight service catalog stored by the admin UI."""
    if not isinstance(payload, list):
        raise ValueError("服务目录必须是数组")
    if len(payload) > 200:
        raise ValueError("服务目录最多允许 200 个服务项")

    normalized, seen = [], set()
    for index, raw in enumerate(payload, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个服务项格式无效")
        code = str(raw.get("code") or raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not code or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
            raise ValueError(f"第 {index} 个服务项编码只能包含字母、数字、点、下划线和连字符")
        if code.lower() in seen:
            raise ValueError(f"服务项编码重复：{code}")
        if not name or len(name) > 80:
            raise ValueError(f"第 {index} 个服务项名称不能为空且不能超过 80 字")
        seen.add(code.lower())

        request_type = str(raw.get("request_type") or "incident").strip().lower()
        if request_type == "request":
            request_type = "service_request"
        if request_type not in ("incident", "service_request"):
            raise ValueError(f"服务项 {code} 的类型必须是 incident 或 service_request")
        priority = str(raw.get("priority") or raw.get("default_priority") or "medium").strip().lower()
        if priority not in ("urgent", "high", "medium", "low"):
            raise ValueError(f"服务项 {code} 的默认优先级无效")
        try:
            response_hours = float(raw.get("response_hours", 4))
            resolution_hours = float(raw.get("resolution_hours", 24))
        except (TypeError, ValueError):
            raise ValueError(f"服务项 {code} 的 SLA 必须是数字") from None
        if not (0.1 <= response_hours <= 8760 and 0.1 <= resolution_hours <= 8760):
            raise ValueError(f"服务项 {code} 的 SLA 必须在 0.1 到 8760 小时之间")
        if response_hours > resolution_hours:
            raise ValueError(f"服务项 {code} 的首次响应目标不能晚于解决目标")

        keywords = raw.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [item.strip() for item in re.split(r"[,，\n]", keywords) if item.strip()]
        if not isinstance(keywords, list):
            raise ValueError(f"服务项 {code} 的关键词必须是数组或逗号分隔文本")
        keywords = [str(item).strip()[:40] for item in keywords if str(item).strip()][:50]

        normalized.append({
            "code": code,
            "name": name,
            "enabled": raw.get("enabled", True) is not False,
            "request_type": request_type,
            "category": str(raw.get("category") or "其他").strip()[:80],
            "default_priority": priority,
            "priority": priority,
            "owner_id": str(raw.get("owner_id") or "").strip()[:128],
            "owner_name": str(raw.get("owner_name") or "").strip()[:80],
            "assignment_group": str(raw.get("assignment_group") or "").strip()[:80],
            "response_hours": response_hours,
            "resolution_hours": resolution_hours,
            "description": str(raw.get("description") or "").strip()[:1000],
            "keywords": keywords,
            "approval_required": bool(raw.get("approval_required", False)),
            "instructions": str(raw.get("instructions") or "").strip()[:2000],
        })
    return normalized


def create_admin_app(config_path=None, settings_path=None, shutdown_callback=None):
    config_file = Path(config_path).resolve() if config_path else Path(DEFAULT_CONFIG_PATH).resolve()
    store = AdminSettingsStore(settings_path)
    history = ConfigHistory(config_file, store.path)
    assets = _asset_root()
    app = Flask(__name__, static_folder=None)
    service_lock = threading.RLock()
    services = {}
    side_effect_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin-side-effect")
    app.extensions["opspilot_side_effect_executor"] = side_effect_executor
    sync_schedule_lock = threading.Lock()
    sync_schedule_state = {"scheduled": False}
    module_registry = ModuleRegistry(config_file, store)
    setup_service = SetupService(config_file, store, module_registry)

    def read_config():
        # The local console is also the first-run setup surface, so it must be
        # usable before config.yaml and DingTalk credentials exist.
        return load_config(config_file, allow_incomplete=True)

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

    def issue_context_service():
        if "issues" not in services:
            with service_lock:
                if "issues" not in services:
                    cfg = read_config().get("issue_context", {})
                    db_path = Path(os.path.expandvars(cfg.get("database_path", "data/issues.db")))
                    if not db_path.is_absolute():
                        db_path = _runtime_root() / db_path
                    services["issues"] = IssueService(
                        enabled=cfg.get("enabled", False),
                        db_path=db_path,
                        logger=app.logger,
                    )
        return services["issues"]

    def knowledge_store():
        if "knowledge" not in services:
            with service_lock:
                if "knowledge" not in services:
                    cfg = read_config().get("knowledge_loop", {})
                    db_path = Path(os.path.expandvars(cfg.get("database_path", "data/knowledge_loop.db")))
                    export_dir, _ = _prepare_knowledge_export_dir(
                        cfg.get("export_dir", DEFAULT_KNOWLEDGE_EXPORT_DIR)
                    )
                    if not db_path.is_absolute():
                        db_path = _runtime_root() / db_path
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

    def submit_side_effect(func, *args):
        try:
            side_effect_executor.submit(func, *args)
            return True
        except RuntimeError:
            app.logger.warning("管理后台正在关闭，后台操作未能提交", exc_info=True)
            return False

    def notify_ticket_async(ticket, action, detail=""):
        if not ticket:
            return
        notifier = ticket_notifier()
        if not notifier.api:
            return
        submit_side_effect(notifier.notify, ticket, action, "管理后台", detail)

    def sync_tickets_async():
        settings = store.load().get("aitable_ticket_sync", {})
        if not settings.get("enabled"):
            return
        with sync_schedule_lock:
            if sync_schedule_state["scheduled"]:
                return
            sync_schedule_state["scheduled"] = True

        def run():
            try:
                aitable_sync().sync_all()
            except Exception as exc:
                app.logger.warning("管理后台触发 AI 表格同步失败: %s", exc)
            finally:
                with sync_schedule_lock:
                    sync_schedule_state["scheduled"] = False

        if not submit_side_effect(run):
            with sync_schedule_lock:
                sync_schedule_state["scheduled"] = False

    def split_query_values(*names):
        values = []
        for name in names:
            for raw in request.args.getlist(name):
                values.extend(part.strip() for part in str(raw).split(",") if part.strip())
        return list(dict.fromkeys(values))

    def ticket_query_filters(page_size_default=50, page_size_max=500):
        try:
            page = max(1, int(request.args.get("page", 1)))
            page_size = max(1, min(int(request.args.get("page_size", page_size_default)), page_size_max))
        except (TypeError, ValueError):
            raise ValueError("页码和每页数量必须是整数") from None

        statuses = split_query_values("status", "statuses")
        legacy_filter = request.args.get("filter", "").strip()
        if not statuses and legacy_filter == "pending":
            statuses = ["open", "assigned", "in_progress"]
        elif not statuses and legacy_filter == "done":
            statuses = ["resolved", "closed"]
        return {
            "q": request.args.get("q", "").strip(),
            "statuses": statuses,
            "priorities": split_query_values("priority", "priorities"),
            "categories": split_query_values("category", "categories"),
            "assignee": request.args.get("assignee", "").strip(),
            "assignment_group": request.args.get("assignment_group", "").strip(),
            "service_code": request.args.get("service_code", request.args.get("service", "")).strip(),
            "request_type": request.args.get("request_type", "").strip(),
            "sla_status": request.args.get("sla_status", "").strip(),
            "confirmation": request.args.get("confirmation", "").strip(),
            "created_from": request.args.get("created_from", "").strip(),
            "created_to": request.args.get("created_to", "").strip(),
            "unassigned": request.args.get("unassigned", "").lower() in ("1", "true", "yes"),
            "sort": request.args.get("sort", "created_at").strip(),
            "order": request.args.get("order", "desc").strip(),
            "page": page,
            "page_size": page_size,
        }

    def query_ticket_page(filters):
        manager = ticket_manager()
        query = getattr(manager, "query_tickets", None)
        if query:
            try:
                result = query(**filters)
                if isinstance(result, dict):
                    items = result.get("items", [])
                    return {
                        "items": items,
                        "total": int(result.get("total", len(items))),
                        "page": int(result.get("page", filters["page"])),
                        "page_size": int(result.get("page_size", filters["page_size"])),
                    }
                if isinstance(result, list):
                    return {"items": result, "total": len(result), "page": filters["page"],
                            "page_size": filters["page_size"]}
            except TypeError as exc:
                # During rolling upgrades an older TicketManager can coexist with the new UI.
                app.logger.info("query_tickets 合同尚未就绪，使用兼容查询: %s", exc)

        # Compatibility fallback for an older TicketManager. It intentionally scans up to
        # 10k records so the UI remains useful until the indexed query implementation loads.
        rows = manager.list_all_tickets("all", limit=10000)
        q = filters["q"].casefold()
        if q:
            searchable = ("ticket_id", "title", "description", "creator_nick", "assignee_nick",
                          "service_name", "service_code")
            rows = [row for row in rows if any(q in str(row.get(key, "")).casefold() for key in searchable)]
        if filters["statuses"]:
            allowed = set(filters["statuses"]); rows = [row for row in rows if row.get("status") in allowed]
        if filters["priorities"]:
            allowed = set(filters["priorities"]); rows = [row for row in rows if row.get("priority") in allowed]
        if filters["categories"]:
            allowed = set(filters["categories"]); rows = [row for row in rows if row.get("category") in allowed]
        if filters["assignee"]:
            value = filters["assignee"]
            rows = [row for row in rows if value in (row.get("assignee_id"), row.get("assignee_nick"))]
        if filters["unassigned"]:
            rows = [row for row in rows if not row.get("assignee_id")]
        for key in ("assignment_group", "service_code", "request_type", "sla_status"):
            if filters[key]:
                rows = [row for row in rows if str(row.get(key, "")) == filters[key]]
        if filters["confirmation"]:
            rows = [row for row in rows if str(row.get("user_confirmation", "")) == filters["confirmation"]]
        if filters["created_from"]:
            rows = [row for row in rows if str(row.get("created_at", "")) >= filters["created_from"]]
        if filters["created_to"]:
            end = filters["created_to"] + " 23:59:59" if len(filters["created_to"]) == 10 else filters["created_to"]
            rows = [row for row in rows if str(row.get("created_at", "")) <= end]
        sort_key = filters["sort"] if filters["sort"] in {
            "created_at", "updated_at", "priority", "status", "response_due_at", "resolution_due_at"
        } else "created_at"
        rows.sort(key=lambda row: str(row.get(sort_key, "")), reverse=filters["order"].lower() != "asc")
        total = len(rows)
        start = (filters["page"] - 1) * filters["page_size"]
        return {"items": rows[start:start + filters["page_size"]], "total": total,
                "page": filters["page"], "page_size": filters["page_size"]}

    @app.before_request
    def local_request_boundary():
        # This console deliberately has no login and is local-only. Reject DNS
        # rebinding and cross-origin browser requests before they can reach a
        # configuration write endpoint.
        hostname = (request.host.split(":", 1)[0] or "").lower()
        if hostname not in {"127.0.0.1", "localhost"}:
            return jsonify({"ok": False, "message": "管理台仅允许本机访问"}), 403
        origin = request.headers.get("Origin", "").strip()
        if origin:
            origin_host = (urlparse(origin).hostname or "").lower()
            if origin_host not in {"127.0.0.1", "localhost"}:
                return jsonify({"ok": False, "message": "拒绝跨站请求"}), 403

    @app.after_request
    def local_security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(Exception)
    def api_error_response(exc):
        if not request.path.startswith("/api/"):
            return exc
        if isinstance(exc, HTTPException):
            if exc.code == 404:
                return jsonify({
                    "ok": False,
                    "message": "请求的后台接口不存在；如果刚完成升级，请重启 OpsPilot 服务后再试",
                }), 404
            return jsonify({"ok": False, "message": exc.description}), exc.code
        error_id = f"ERR-{uuid.uuid4().hex[:8].upper()}"
        app.logger.exception("管理后台请求失败 [%s]", error_id)
        return jsonify({"ok": False, "message": "服务器处理失败", "error_id": error_id}), 500

    @app.get("/")
    def index():
        return send_from_directory(assets, "index.html")

    @app.get("/assets/<path:name>")
    def asset(name):
        return send_from_directory(assets, name)

    @app.get("/api/setup/status")
    def setup_status():
        return jsonify(setup_service.status())

    @app.put("/api/setup/draft")
    def save_setup_draft():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "请求内容必须是 JSON 对象"}), 400
        try:
            draft = setup_service.save_draft(payload)
            return jsonify({"ok": True, "draft": draft, "message": "配置草稿已保存"})
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.post("/api/setup/validate")
    def validate_setup_step():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "请求内容必须是 JSON 对象"}), 400
        try:
            return jsonify(setup_service.validate(payload))
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc), "results": []}), 400

    @app.post("/api/setup/complete")
    def complete_setup():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "请求内容必须是 JSON 对象"}), 400
        try:
            result = setup_service.complete(payload)
            if result.get("ok"):
                history.record("setup", "完成首次配置向导")
                # A health request made before setup may have cached a notifier
                # without credentials. Recreate it on the next request.
                with service_lock:
                    services.pop("notifier", None)
            return jsonify(result), (200 if result.get("ok") else 400)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.get("/api/modules")
    def list_modules():
        modules = module_registry.list_modules()
        return jsonify({"modules": modules, "items": modules})

    @app.patch("/api/modules/<module_id>")
    def update_module(module_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return jsonify({"ok": False, "message": "enabled 必须是布尔值"}), 400
        try:
            item = module_registry.set_enabled(module_id, payload["enabled"])
            history.record("modules", f"修改模块 {module_id} 开关")
            return jsonify({
                "ok": True,
                "module": item,
                "message": "模块设置已保存，重启机器人后生效",
                "restart_required": bool(item.get("restart_required", True)),
            })
        except KeyError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.post("/api/modules/<module_id>/test")
    def test_module(module_id):
        try:
            result = module_registry.test(module_id)
            return jsonify(result), (200 if result.get("ok") else 422)
        except KeyError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 404

    @app.get("/api/config-package/export")
    def export_config_package():
        package = create_public_package(config_file, store, app_version=BOT_VERSION)
        payload = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            payload,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="opspilot-{BOT_VERSION}-config.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/config-package/import/preview")
    def preview_config_package():
        payload = request.get_json(silent=True)
        try:
            return jsonify(preview_package(payload, config_file, store))
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.post("/api/config-package/import/apply")
    def import_config_package():
        payload = request.get_json(silent=True)
        try:
            preview = preview_package(payload, config_file, store)
            history.record("config_package_pre_import", "导入配置包前自动快照")
            apply_package(payload, config_file, store)
            history_warning = ""
            try:
                history.record("config_package", "安全配置包导入成功")
            except Exception:
                history_warning = "；配置已生效，但结果快照写入失败，请检查数据目录写权限"
                app.logger.warning("配置包已导入，但结果快照写入失败", exc_info=True)
            with service_lock:
                task_service = services.get("tasks")
                if task_service is not None:
                    try:
                        task_service.shutdown(wait=False)
                    except Exception:
                        app.logger.warning("导入配置包后关闭旧任务查询服务失败", exc_info=True)
                services.clear()
            return jsonify({
                "ok": True,
                "message": "配置包已导入；密钥按服务身份安全合并，运行参数将在重启后全部生效" + history_warning,
                "restart_required": True,
                "history_warning": bool(history_warning),
                "preview": preview,
            })
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

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

    @app.post("/api/runtime/shutdown")
    def shutdown_runtime():
        """Request a graceful process shutdown after the HTTP response is sent."""
        if not callable(shutdown_callback):
            return jsonify({"ok": False, "message": "当前启动方式不支持从管理台关闭服务"}), 503

        def request_stop():
            try:
                shutdown_callback()
            except Exception:
                app.logger.exception("管理台请求关闭服务失败")

        timer = threading.Timer(0.35, request_stop)
        timer.daemon = True
        timer.start()
        return jsonify({"ok": True, "message": "关闭请求已接受，服务正在安全退出"}), 202

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
        if not observability_store().claim_delivery_for_retry(delivery_id):
            return jsonify({"message": "该通知已被其他请求提交重试"}), 409
        if not submit_side_effect(ticket_notifier().retry, delivery_id):
            observability_store().update_delivery(delivery_id, "failed", "重试任务调度失败")
            return jsonify({"message": "重试任务调度失败"}), 503
        return jsonify({"ok": True, "message": "已提交重试"})

    @app.get("/api/monitor/health")
    def monitor_health():
        data, state = read_config(), bot_status.snapshot()
        sheet = data.get("spreadsheet", {})
        checks = [
            {"component": "钉钉 Stream", "ok": bool(state["stream_connected"]), "detail": "已连接" if state["stream_connected"] else "未连接或后台独立运行"},
            {"component": "钉钉 OpenAPI", "ok": bool(state["dtalk_connected"]), "detail": "已验证" if state["dtalk_connected"] else "未验证"},
            {"component": "IMA 连接", "ok": bool(state["ima_connected"] or not state["ima_enabled"]), "detail": "连接正常" if state["ima_connected"] else "未启用或连接失败"},
            {"component": "IMA 知识检索", "ok": bool(state.get("ima_search_ok") or not state["ima_enabled"]), "detail": "检索正常" if state.get("ima_search_ok") else (state.get("ima_health_detail") or "不可用")},
            {"component": "AI 知识来源", "ok": bool(state.get("ima_content_available") or not state["ima_enabled"]), "detail": "知识正文可用" if state.get("ima_content_available") else (state.get("ima_health_detail") or "无可用正文")},
            {"component": "AI API", "ok": bool(state["llm_connected"] or not state["llm_enabled"]), "detail": "已连接" if state["llm_connected"] else "未启用或未连接"},
            {"component": "工单数据库", "ok": Path(ticket_manager().db_path).exists(), "detail": ticket_manager().db_path},
            {"component": "任务数据库", "ok": Path(task_store().db_path).exists(), "detail": task_store().db_path},
            {"component": "Node.js 表格环境", "ok": Path(os.path.expandvars(sheet.get("node_executable", ""))).is_file(), "detail": sheet.get("node_executable", "未配置")},
        ]
        return jsonify(checks)

    @app.get("/api/network/diagnose")
    def diagnose_network():
        include_speed = request.args.get("speed", "false").lower() in ("1", "true", "yes")
        return jsonify(NetworkDiagnostics(timeout=8).diagnose(include_speed=include_speed))

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
        updates = {
            field: payload[field] for field in allowed
            if field in payload and payload[field] not in (None, "")
        }
        if updates:
            atomic_update_config(config_file, {"spreadsheet": updates})
        return jsonify({"ok": True, "message": "文件助手配置已保存，重启机器人后生效"})

    @app.get("/api/aitable-ticket-sync")
    def get_aitable_ticket_sync():
        return jsonify(store.load().get("aitable_ticket_sync", {}))

    @app.put("/api/aitable-ticket-sync")
    def save_aitable_ticket_sync():
        payload = request.get_json(silent=True) or {}
        allowed = {"enabled", "dws_executable", "base_id", "table_id",
                   "sync_interval_minutes", "field_mapping"}
        history.record("aitable_ticket_sync", "修改工单与钉钉 AI 表格同步配置")
        def update(data):
            current = dict(data.get("aitable_ticket_sync", {}))
            current.update({key: payload[key] for key in allowed if key in payload})
            data["aitable_ticket_sync"] = current
            return current

        current = store.mutate(update)
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
        allowed = {"enabled", "provider", "base_url", "chat_completions_path", "model", "api_key", "temperature", "max_tokens", "timeout"}
        current = read_config().get("llm", {})
        updates = {
            field: payload[field] for field in allowed
            if field in payload and payload[field] not in (None, "")
        }
        candidate = {**current, **updates}
        if not same_credential_scope(current, candidate) and not str(payload.get("api_key") or "").strip():
            updates["api_key"] = ""
        if updates:
            atomic_update_config(config_file, {"llm": updates})
        return jsonify({"ok": True, "message": "API 配置已保存，重启机器人后全部生效"})

    @app.post("/api/llm/test")
    def test_llm():
        cfg = read_config().get("llm", {})
        payload = request.get_json(silent=True) or {}
        candidate = {**cfg, **{key: value for key, value in payload.items() if value not in (None, "")}}
        candidate["api_key"] = str(payload.get("api_key") or "").strip()
        base_url = str(candidate.get("base_url") or "").rstrip("/")
        api_path = str(candidate.get("chat_completions_path") or "/v1/chat/completions")
        api_key = resolve_scoped_api_key(candidate, existing=cfg)
        model = candidate.get("model")
        provider = str(candidate.get("provider") or "openai_compatible").lower()
        local = provider in LOCAL_LLM_PROVIDERS
        if not base_url or not model or (not local and not api_key):
            return jsonify({"ok": False, "message": "请先填写地址、模型和所需的 API Key"}), 400
        try:
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = requests.post(
                base_url + (api_path if api_path.startswith("/") else "/" + api_path),
                headers=headers,
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
        data.setdefault("service_catalog", [])
        data.pop("models", None)
        safe_connectors = []
        for connector in data.get("connectors", []):
            raw = dict(connector)
            item = public_sanitized_copy(raw)
            item["has_auth_value"] = bool(raw.get("auth_value"))
            item["auth_value_masked"] = _mask_secret(str(raw.get("auth_value") or ""))
            item.pop("auth_value", None)
            safe_connectors.append(item)
        data["connectors"] = safe_connectors
        return jsonify(data)

    @app.put("/api/settings/<section>")
    def save_settings(section):
        if section not in ("connectors", "people", "response_style", "model_routing", "service_catalog"):
            return jsonify({"ok": False, "message": "未知配置项"}), 404
        payload = request.get_json(silent=True)
        if section == "service_catalog":
            try:
                payload = _normalize_service_catalog(payload)
            except ValueError as exc:
                return jsonify({"ok": False, "message": str(exc)}), 400
        elif section == "people":
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                return jsonify({"ok": False, "message": "人员配置必须是对象数组"}), 400
        elif section in {"response_style", "model_routing"} and not isinstance(payload, dict):
            return jsonify({"ok": False, "message": f"{section} 配置必须是对象"}), 400
        if section == "connectors":
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                return jsonify({"ok": False, "message": "连接器配置必须是对象数组"}), 400
        history.record(section, f"修改 {section} 配置")

        def update(data):
            incoming = payload
            if section == "connectors":
                existing = {item.get("id"): item for item in data.get("connectors", [])}
                merged = []
                for connector in incoming or []:
                    item = dict(connector)
                    item.pop("auth_value_masked", None)
                    item.pop("has_auth_value", None)
                    old = existing.get(item.get("id"))
                    same_target = bool(old) and (
                        str(old.get("type") or "").strip().lower()
                        == str(item.get("type") or "").strip().lower()
                        and normalize_endpoint(public_sanitized_copy(str(old.get("url") or ""), "url"))
                        == normalize_endpoint(public_sanitized_copy(str(item.get("url") or ""), "url"))
                    )
                    if same_target and str(item.get("type") or "").strip().lower() == "smb":
                        same_target = (
                            str(old.get("username") or "").strip().casefold()
                            == str(item.get("username") or "").strip().casefold()
                            and str(old.get("domain") or "").strip().casefold()
                            == str(item.get("domain") or "").strip().casefold()
                        )
                    if not item.get("auth_value") and same_target:
                        item["auth_value"] = old.get("auth_value", "")
                    elif not item.get("auth_value"):
                        item.pop("auth_value", None)
                    if same_target and old:
                        if "headers" in item:
                            item["headers"] = merge_connector_headers_preserving_secrets(
                                old.get("headers"), item.get("headers")
                            )
                        elif "headers" in old:
                            item["headers"] = old.get("headers")
                    if str(item.get("type") or "").strip().lower() == "smb":
                        normalized = SMBReadOnlyConnector.validate_config(item)
                        item["url"] = normalized["root"]
                        item["max_depth"] = normalized["max_depth"]
                        item["max_results"] = normalized["max_results"]
                        item["allowed_extensions"] = sorted(normalized["extensions"])
                        item["include_directories"] = normalized["include_directories"]
                        item["access_scope"] = normalized["access_scope"]
                        for access_key in ("allowed_staff_ids", "allowed_departments", "allowed_roles"):
                            item[access_key] = normalized[access_key]
                        item["read_only"] = True
                    merged.append(item)
                incoming = merged
            data[section] = incoming
            return incoming

        try:
            response_data = store.mutate(update)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if section == "connectors":
            safe_response = []
            for connector in response_data:
                raw = dict(connector)
                item = public_sanitized_copy(raw)
                item["has_auth_value"] = bool(raw.get("auth_value"))
                item["auth_value_masked"] = _mask_secret(str(raw.get("auth_value") or ""))
                item.pop("auth_value", None)
                safe_response.append(item)
            response_data = safe_response
        return jsonify({"ok": True, "data": response_data})

    @app.post("/api/connectors/test")
    def test_connector():
        connector = dict(request.get_json(silent=True) or {})
        old = next((item for item in store.load().get("connectors", [])
                    if item.get("id") == connector.get("id")), None)
        same_target = bool(old) and (
            str(old.get("type") or "").strip().lower()
            == str(connector.get("type") or "").strip().lower()
            and normalize_endpoint(old.get("url")) == normalize_endpoint(connector.get("url"))
        )
        if same_target and str(connector.get("type") or "").strip().lower() == "smb":
            same_target = (
                str(old.get("username") or "").strip().casefold()
                == str(connector.get("username") or "").strip().casefold()
                and str(old.get("domain") or "").strip().casefold()
                == str(connector.get("domain") or "").strip().casefold()
            )
        if same_target and not connector.get("auth_value"):
            connector["auth_value"] = old.get("auth_value", "")
        if connector.get("type") in ("dingtalk", "ima"):
            return jsonify({"ok": True, "message": "内置连接器配置存在；实际连接状态请查看总览"})
        try:
            if str(connector.get("type") or "").strip().lower() == "smb":
                result = SMBReadOnlyConnector.test(connector, timeout=8)
                observability_store().record_event(
                    "connector", f"test:{connector.get('id', 'smb')}", "success",
                    duration_ms=result.get("duration_ms", 0), actor_id="web-admin",
                    detail="read_only=true",
                )
                return jsonify({"ok": True, "message": "NAS 连接成功（只读）", "result": result})
            result = HTTPConnectorExecutor.execute(connector, {"input": connector.get("test_input", "test")}, timeout=8)
            return jsonify({"ok": True, "message": f"HTTP {result['status_code']}", "result": result["data"]})
        except Exception as exc:
            observability_store().record_event(
                "connector", f"test:{connector.get('id', 'unknown')}", "error",
                actor_id="web-admin", detail=type(exc).__name__,
            )
            return jsonify({"ok": False, "message": f"连接失败：{str(exc)[:160]}"}), 502

    @app.post("/api/connectors/execute")
    def execute_connector():
        payload = request.get_json(silent=True) or {}
        connector = next((c for c in store.load().get("connectors", []) if c.get("id") == payload.get("id")), None)
        if not connector or not connector.get("enabled", True):
            return jsonify({"ok": False, "message": "连接器不存在或未启用"}), 404
        connector_type = str(connector.get("type") or "").strip().lower()
        if connector_type not in {"http", "smb"}:
            return jsonify({"ok": False, "message": "该连接器不支持手动查询"}), 400
        started = time.perf_counter()
        try:
            variables = payload.get("variables") or {}
            if connector_type == "smb":
                action = str(payload.get("action") or "search").strip().lower()
                if action != "search":
                    return jsonify({"ok": False, "message": "NAS 连接器仅支持只读查询"}), 400
                result = SMBReadOnlyConnector.search(connector, variables.get("query") or variables.get("input"), timeout=10)
            else:
                result = HTTPConnectorExecutor.execute(connector, variables)
            observability_store().record_event(
                "connector", f"query:{connector.get('id', connector_type)}", "success",
                duration_ms=(time.perf_counter() - started) * 1000, actor_id="web-admin",
                detail=f"type={connector_type};results={result.get('count', 1) if isinstance(result, dict) else 1}",
            )
            return jsonify({"ok": True, "result": result})
        except Exception as exc:
            observability_store().record_event(
                "connector", f"query:{connector.get('id', connector_type)}", "error",
                duration_ms=(time.perf_counter() - started) * 1000, actor_id="web-admin",
                detail=type(exc).__name__,
            )
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
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("models", []), list)
            or any(not isinstance(item, dict) for item in payload.get("models", []))
            or not isinstance(payload.get("routing", {}), dict)
        ):
            return jsonify({"ok": False, "message": "模型和路由配置格式无效"}), 400
        models = payload.get("models", [])
        routing = payload.get("routing", {})
        if len(models) > 20:
            return jsonify({"ok": False, "message": "最多可配置 20 个 AI API"}), 400
        known_ids = set()
        allowed_providers = {"deepseek", "openai", "openai_compatible", "ollama", "local", "custom"}
        for index, profile in enumerate(models, start=1):
            model_id = str(profile.get("id") or "").strip()
            model = str(profile.get("model") or "").strip()
            base_url = str(profile.get("base_url") or "").strip()
            provider = str(profile.get("provider") or "custom").strip().lower()
            parsed = urlparse(base_url)
            if not model_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", model_id):
                return jsonify({"ok": False, "message": f"第 {index} 个 API 的配置 ID 无效"}), 400
            if model_id in known_ids:
                return jsonify({"ok": False, "message": "API 配置 ID 不能重复"}), 400
            if not model or not base_url:
                return jsonify({"ok": False, "message": f"第 {index} 个 API 必须填写模型和 Base URL"}), 400
            if provider not in allowed_providers:
                return jsonify({"ok": False, "message": f"第 {index} 个 API 服务类型无效"}), 400
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                return jsonify({"ok": False, "message": f"第 {index} 个 API 的 Base URL 无效"}), 400
            known_ids.add(model_id)
        for route_name in ("general", "ticket", "vision", "fallback"):
            selected = str(routing.get(route_name) or "").strip()
            if selected and selected not in known_ids:
                return jsonify({"ok": False, "message": f"路由 {route_name} 引用了不存在的 API"}), 400
        fallback_order = routing.get("fallback_order", [])
        if not isinstance(fallback_order, list):
            return jsonify({"ok": False, "message": "备用 API 顺序必须是数组"}), 400
        normalized_fallbacks = []
        for model_id in fallback_order:
            model_id = str(model_id or "").strip()
            if model_id not in known_ids:
                return jsonify({"ok": False, "message": "备用顺序引用了不存在的 API"}), 400
            if model_id not in normalized_fallbacks:
                normalized_fallbacks.append(model_id)
        routing = {
            route_name: str(routing.get(route_name) or "").strip()
            for route_name in ("general", "ticket", "vision", "fallback")
        }
        routing["fallback_order"] = normalized_fallbacks
        base_llm = read_config().get("llm", {})
        history.record("models", "修改多模型与主备路由")

        def update(data):
            old = {m.get("id"): m for m in data.get("models", [])}
            merged = []
            for profile in payload.get("models", []):
                item = dict(profile)
                item.pop("api_key_masked", None)
                item.pop("has_api_key", None)
                previous = old.get(item.get("id"))
                if not str(item.get("api_key") or "").strip():
                    if previous and same_credential_scope(
                        previous,
                        item,
                        current_fallback=base_llm,
                        candidate_fallback=base_llm,
                    ):
                        item["api_key"] = previous.get("api_key", "")
                    else:
                        item.pop("api_key", None)
                merged.append(item)
            data["models"] = merged
            data["model_routing"] = routing

        store.mutate(update)
        return jsonify({"ok": True, "message": "多个 AI API 与自动切换顺序已保存，即时生效"})

    @app.post("/api/models/test")
    def test_model():
        profile = request.get_json(silent=True) or {}
        existing = next((m for m in store.load().get("models", []) if m.get("id") == profile.get("id")), {})
        llm = read_config().get("llm", {})
        test_payload = {**llm, **{key: value for key, value in profile.items() if value not in (None, "")}}
        test_payload["api_key"] = str(profile.get("api_key") or "").strip()
        key = resolve_scoped_api_key(test_payload, existing=existing, base=llm)
        provider = str(test_payload.get("provider") or "openai_compatible").lower()
        base_url = str(test_payload.get("base_url") or "").rstrip("/")
        model = str(test_payload.get("model") or "").strip()
        if not base_url or not model or (provider not in LOCAL_LLM_PROVIDERS and not key):
            return jsonify({"ok": False, "message": "请填写地址、模型和该端点所需的 API Key"}), 400
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        api_path = str(test_payload.get("chat_completions_path") or "/v1/chat/completions")
        try:
            response = requests.post(
                base_url + (api_path if api_path.startswith("/") else "/" + api_path),
                headers=headers,
                json={"model": model, "messages": [{"role": "user", "content": "仅回复 OK"}], "max_tokens": 8}, timeout=20)
            response.raise_for_status()
            return jsonify({"ok": True, "message": "模型连接成功"})
        except Exception as exc:
            return jsonify({"ok": False, "message": f"模型连接失败：{str(exc)[:160]}"}), 502

    def apply_ticket_action(manager, ticket_id, action, payload):
        """Apply one action and return (ticket, notification action, notification detail)."""
        result, notification_action, notification_detail = None, "", ""
        if action == "priority":
            result = manager.update_priority(ticket_id, payload.get("priority", "medium"))
            notification_action = "priority"
            notification_detail = f"优先级已调整为 {payload.get('priority', 'medium')}"
        elif action == "comment":
            visibility = str(payload.get("visibility") or "public").lower()
            if visibility not in ("internal", "public"):
                raise ValueError("处理记录可见性必须是 internal 或 public")
            try:
                result = manager.add_comment(
                    ticket_id, payload.get("detail", ""), "web-admin", "管理后台",
                    visibility=visibility,
                )
            except TypeError:
                result = manager.add_comment(ticket_id, payload.get("detail", ""), "web-admin", "管理后台")
            if visibility == "public":
                notification_action = "comment"
                notification_detail = payload.get("detail", "")
        elif action == "assign":
            result = manager.assign_ticket(
                ticket_id, payload.get("assignee_id", ""), payload.get("assignee_nick", ""),
                "web-admin", "管理后台",
            )
            notification_action = "assign"
            notification_detail = f"工单已分派给 {payload.get('assignee_nick', '')}"
        elif action == "progress":
            progress_detail = payload.get("detail") or "运维人员已开始处理"
            result = manager.set_in_progress(ticket_id, "web-admin", "管理后台", progress_detail)
            notification_action = "progress"
            notification_detail = progress_detail
        elif action == "resolve":
            result = manager.resolve_ticket(ticket_id, payload.get("resolution", ""), "web-admin", "管理后台")
            notification_action = "resolved"
            notification_detail = payload.get("resolution", "")
        elif action == "close":
            # Detail is the existing batch-operation field; single-ticket UI
            # supplies the dedicated resolution field.
            resolution = str(payload.get("resolution") or payload.get("detail") or "").strip()
            result = manager.close_ticket(ticket_id, "web-admin", "管理后台", resolution)
            notification_action, notification_detail = "resolved", resolution
        elif action == "reopen":
            detail = payload.get("detail") or "管理后台重新打开"
            result = manager.reopen_ticket(ticket_id, "web-admin", "管理后台", detail)
            notification_action, notification_detail = "reopened", detail
        elif action == "invalid":
            detail = payload.get("detail") or "管理后台标记无效"
            result = manager.mark_as_invalid(ticket_id, "web-admin", "管理后台", detail)
            notification_action, notification_detail = "closed", detail
        else:
            raise ValueError("未知工单操作")
        if result and action == "invalid":
            issue_context_service().close_ticket(ticket_id, notification_detail)
        return result, notification_action, notification_detail

    @app.get("/api/tickets")
    def list_tickets():
        # Preserve the historical list response for old clients that only send
        # filter/field/value. The V2.15 UI always sends page and receives an envelope.
        modern_keys = {
            "page", "page_size", "q", "status", "statuses", "priority", "priorities",
            "category", "categories", "assignee", "assignment_group", "service_code", "service",
            "request_type", "sla_status", "confirmation", "created_from", "created_to", "unassigned",
            "sort", "order",
        }
        modern = request.args.get("format") == "page" or any(key in request.args for key in modern_keys)
        if not modern:
            filter_type = request.args.get("filter", "all")
            field, value = request.args.get("field", "").strip(), request.args.get("value", "").strip()
            try:
                limit = max(1, min(int(request.args.get("limit", 200)), 500))
                tickets = (ticket_manager().search_tickets_exact(field, value, limit)
                           if field and value else ticket_manager().list_all_tickets(filter_type, limit=limit))
            except ValueError as exc:
                return jsonify({"message": str(exc)}), 400
            return jsonify(tickets)
        try:
            return jsonify(query_ticket_page(ticket_query_filters()))
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.post("/api/tickets")
    def create_ticket_from_admin():
        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title") or "").strip()
        creator_nick = str(payload.get("creator_nick") or "").strip()
        if not title or len(title) > 200:
            return jsonify({"ok": False, "message": "标题不能为空且不能超过 200 字"}), 400
        if not creator_nick or len(creator_nick) > 80:
            return jsonify({"ok": False, "message": "创建人不能为空且不能超过 80 字"}), 400

        service_code = str(payload.get("service_code") or "").strip()
        catalog = store.load().get("service_catalog", [])
        service = next((item for item in catalog if item.get("code") == service_code), None)
        if service_code and (not service or service.get("enabled") is False):
            return jsonify({"ok": False, "message": "所选服务项不存在或已停用"}), 400
        service = service or {}
        creator_id = str(payload.get("creator_id") or payload.get("creator_staff_id") or creator_nick).strip()
        kwargs = {
            "conv_id": str(payload.get("conv_id") or f"admin:{creator_id}"),
            "conv_title": str(payload.get("conv_title") or "管理台新建"),
            "creator_id": creator_id,
            "creator_nick": creator_nick,
            "creator_staff_id": str(payload.get("creator_staff_id") or "").strip(),
            "title": title,
            "description": str(payload.get("description") or "").strip()[:5000],
            "category": service.get("category") or str(payload.get("category") or "其他"),
            "priority": service.get("default_priority") or service.get("priority") or str(payload.get("priority") or "medium"),
            "request_type": service.get("request_type") or str(payload.get("request_type") or "incident"),
            "service_code": service.get("code", ""),
            "service_name": service.get("name", ""),
            "assignment_group": service.get("assignment_group", ""),
            "response_hours": service.get("response_hours") if service else None,
            "resolution_hours": service.get("resolution_hours") if service else None,
        }
        manager = ticket_manager()
        try:
            ticket = manager.create_ticket(**kwargs)
        except TypeError:
            # Compatibility with V2.14 while the core schema is rolling forward.
            kwargs.pop("assignment_group", None)
            ticket = manager.create_ticket(**kwargs)

        owner_id = str(service.get("owner_id") or "").strip()
        owner_name = str(service.get("owner_name") or "").strip()
        if owner_id or owner_name:
            # Older service-catalog entries may only have owner_name. Keep those
            # entries functional by using the name as a deterministic assignee ID.
            assignee_id = owner_id or owner_name
            assignee_nick = owner_name or owner_id
            try:
                assigned = manager.assign_ticket(
                    ticket["ticket_id"], assignee_id, assignee_nick,
                    "web-admin", "管理后台（服务目录自动分派）",
                )
            except Exception:
                app.logger.exception("服务目录自动分派失败: %s", ticket["ticket_id"])
                observability_store().record_event(
                    "ticket", "auto_assign", "failure", actor_id="web-admin",
                    detail=ticket["ticket_id"],
                )
                return jsonify({
                    "ok": False,
                    "message": "工单已创建，但服务负责人自动分派失败，请在工单工作台手动分派",
                    "ticket_id": ticket["ticket_id"],
                }), 500
            if not assigned:
                observability_store().record_event(
                    "ticket", "auto_assign", "failure", actor_id="web-admin",
                    detail=ticket["ticket_id"],
                )
                return jsonify({
                    "ok": False,
                    "message": "工单已创建，但服务负责人自动分派失败，请在工单工作台手动分派",
                    "ticket_id": ticket["ticket_id"],
                }), 500
            ticket = assigned
        notify_ticket_async(ticket, "created", "管理后台已创建工单")
        sync_tickets_async()
        observability_store().record_event("ticket", "create", "success", actor_id="web-admin",
                                           detail=ticket["ticket_id"])
        return jsonify({"ok": True, "ticket": ticket}), 201

    @app.get("/api/tickets/export")
    def export_tickets():
        manager = ticket_manager()
        field, value = request.args.get("field", "").strip(), request.args.get("value", "").strip()
        try:
            if field and value:
                allowed = {"ticket_id", "creator_id", "creator_nick", "creator_staff_id", "status",
                           "priority", "category", "assignee_id", "assignee_nick", "title", "conv_id"}
                if field not in allowed:
                    raise ValueError("不支持的精确搜索字段")
                rows = [row for row in manager.list_all_tickets("all", limit=10000)
                        if str(row.get(field, "")) == value][:10000]
            else:
                filters = ticket_query_filters(page_size_default=1000, page_size_max=1000)
                filters.update({"page": 1, "page_size": 1000})
                rows, page = [], 1
                while len(rows) < 10000:
                    filters["page"] = page
                    result = query_ticket_page(filters)
                    batch = result["items"]
                    rows.extend(batch)
                    if not batch or len(rows) >= result["total"]:
                        break
                    page += 1
                rows = rows[:10000]
        except ValueError as exc:
            return jsonify({"message": str(exc)}), 400
        columns = ["ticket_id", "title", "description", "category", "priority", "status", "creator_id",
                   "creator_nick", "creator_staff_id", "assignee_id", "assignee_nick", "resolution",
                   "request_type", "service_code", "service_name", "assignment_group", "sla_status",
                   "response_due_at", "resolution_due_at", "first_response_at", "user_confirmation",
                   "confirmed_at", "satisfaction_score", "satisfaction_comment", "conv_id", "conv_title",
                   "created_at", "updated_at", "resolved_at"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        body = "\ufeff" + output.getvalue()
        return Response(body, content_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": "attachment; filename=tickets-export.csv",
        })

    @app.post("/api/tickets/batch")
    def batch_update_tickets():
        payload = request.get_json(silent=True) or {}
        ticket_ids = list(dict.fromkeys(str(item).strip() for item in payload.get("ticket_ids", []) if str(item).strip()))
        action = str(payload.get("action") or "").strip()
        if not ticket_ids:
            return jsonify({"ok": False, "message": "请至少选择一条工单"}), 400
        if len(ticket_ids) > 100:
            return jsonify({"ok": False, "message": "每次最多批量处理 100 条工单"}), 400
        if action not in ("assign", "priority", "progress", "close", "invalid"):
            return jsonify({"ok": False, "message": "不支持的批量操作"}), 400
        if action == "assign" and not str(payload.get("assignee_id") or "").strip():
            return jsonify({"ok": False, "message": "批量分派必须选择负责人"}), 400
        if action == "priority" and payload.get("priority") not in ("urgent", "high", "medium", "low"):
            return jsonify({"ok": False, "message": "批量优先级无效"}), 400
        if action in ("close", "invalid") and not str(payload.get("detail") or "").strip():
            return jsonify({"ok": False, "message": "批量提交解决或标记无效必须填写处理内容"}), 400

        manager, results, changed = ticket_manager(), [], []
        for ticket_id in ticket_ids:
            try:
                ticket, notify_action, notify_detail = apply_ticket_action(manager, ticket_id, action, payload)
                if not ticket:
                    results.append({"ticket_id": ticket_id, "ok": False,
                                    "message": "当前状态不允许此操作或工单不存在"})
                    continue
                changed.append(ticket)
                results.append({"ticket_id": ticket_id, "ok": True})
                if notify_action:
                    notify_ticket_async(ticket, notify_action, notify_detail)
            except Exception as exc:
                app.logger.warning("批量工单操作失败 %s: %s", ticket_id, exc)
                results.append({"ticket_id": ticket_id, "ok": False, "message": str(exc)[:160]})
        if changed:
            sync_tickets_async()
        succeeded = sum(item["ok"] for item in results)
        observability_store().record_event(
            "ticket", f"batch_{action}", "success" if succeeded == len(results) else "partial",
            actor_id="web-admin", detail=f"成功 {succeeded}/{len(results)}",
        )
        return jsonify({"ok": succeeded == len(results), "succeeded": succeeded,
                        "failed": len(results) - succeeded, "results": results})

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
        ticket = dict(ticket)
        ticket.setdefault("feedback_score", ticket.get("satisfaction_score"))
        feedback = []
        if hasattr(manager, "list_feedback"):
            feedback = manager.list_feedback(ticket_id=ticket_id, limit=20)
        issue = issue_context_service().get_by_ticket_id(ticket_id)
        return jsonify({"ticket": ticket, "logs": manager.get_ticket_logs(ticket_id, 100),
                        "feedback": feedback, "issue": issue})

    @app.get("/api/issues")
    def list_issues():
        filter_name = str(request.args.get("filter") or "all").strip().lower()
        filters = {
            "all": ("", None),
            "open": ("open", None),
            "closed": ("closed", None),
            "linked": ("", True),
            "unlinked": ("", False),
        }
        if filter_name not in filters:
            return jsonify({"ok": False, "message": "不支持的 Issue 筛选条件"}), 400
        service = issue_context_service()
        if not service.enabled:
            return jsonify({
                "ok": True,
                "enabled": False,
                "items": [],
                "total": 0,
                "message": "Issue 记录当前未启用",
            })
        status, linked = filters[filter_name]
        try:
            items = service.list_issues(status=status, linked=linked, limit=500)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({
            "ok": True,
            "enabled": True,
            "items": items,
            "total": len(items),
            "filter": filter_name,
        })

    @app.get("/api/issues/<issue_id>")
    def issue_detail(issue_id):
        service = issue_context_service()
        if not service.enabled:
            return jsonify({"ok": False, "message": "Issue 记录当前未启用"}), 409
        issue = service.get_issue(issue_id)
        if not issue:
            return jsonify({"ok": False, "message": "Issue 不存在"}), 404
        return jsonify({"ok": True, "issue": issue})

    @app.patch("/api/tickets/<ticket_id>")
    def update_ticket(ticket_id):
        payload = request.get_json(silent=True) or {}
        manager = ticket_manager()
        action = str(payload.get("action") or "").strip()
        if action == "assign" and not str(payload.get("assignee_id") or "").strip():
            return jsonify({"ok": False, "message": "请选择负责人"}), 400
        if action == "comment" and not str(payload.get("detail") or "").strip():
            return jsonify({"ok": False, "message": "请填写处理记录"}), 400
        if action == "resolve" and not str(payload.get("resolution") or "").strip():
            return jsonify({"ok": False, "message": "请填写解决方案"}), 400
        if action == "close" and not str(payload.get("resolution") or "").strip():
            return jsonify({"ok": False, "message": "关闭前必须填写解决方案；提交后工单将等待用户确认"}), 400
        if action == "invalid" and not str(payload.get("detail") or "").strip():
            return jsonify({"ok": False, "message": "标记无效前必须填写原因"}), 400
        try:
            result, notification_action, notification_detail = apply_ticket_action(
                manager, ticket_id, action, payload,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if not result:
            return jsonify({"ok": False, "message": "操作失败，可能不符合当前状态流转规则"}), 400
        if notification_action:
            notify_ticket_async(result, notification_action, notification_detail)
        sync_tickets_async()
        observability_store().record_event("ticket", action, "success", actor_id="web-admin", detail=ticket_id)
        return jsonify({"ok": True, "ticket": result})

    @app.post("/api/tickets/<ticket_id>/confirm")
    def confirm_ticket_resolution(ticket_id):
        payload = request.get_json(silent=True) or {}
        manager = ticket_manager()
        ticket = manager.get_ticket(ticket_id)
        if not ticket:
            return jsonify({"ok": False, "message": "工单不存在"}), 404
        confirmed = payload.get("confirmed", True) is not False
        if not confirmed:
            result = manager.reopen_ticket(
                ticket_id, ticket.get("creator_id", ""), "管理后台代用户确认",
                str(payload.get("comment") or "用户确认问题未解决"),
            )
            notification_action, notification_detail = "reopened", str(payload.get("comment") or "用户确认问题未解决")
        else:
            score = payload.get("score")
            if score not in (None, ""):
                try:
                    score = int(score)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "message": "评分必须是 1 到 5 的整数"}), 400
                if not 1 <= score <= 5:
                    return jsonify({"ok": False, "message": "评分必须是 1 到 5 的整数"}), 400
            else:
                score = None
            result = manager.confirm_resolution(
                ticket_id, ticket.get("creator_id", ""), operator_nick="管理后台代用户确认",
                score=score, comment=str(payload.get("comment") or "").strip(),
            )
            notification_action, notification_detail = "confirmed", "用户已确认问题恢复"
        if not result:
            return jsonify({"ok": False, "message": "当前状态不允许确认"}), 400
        if confirmed:
            issue_context_service().close_ticket(
                ticket_id,
                result.get("resolution") or str(payload.get("comment") or "用户已确认问题恢复"),
            )
            knowledge_store().create_from_ticket(result)
        notify_ticket_async(result, notification_action, notification_detail)
        sync_tickets_async()
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

    @app.get("/api/knowledge/access")
    def get_knowledge_access():
        config = read_config()
        items = []
        for raw in config.get("ima", {}).get("default_knowledge_bases", []):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            items.append({
                "id": str(raw.get("id")), "name": str(raw.get("name") or raw.get("id")),
                "access_scope": str(raw.get("access_scope") or "public"),
                "allowed_staff_ids": raw.get("allowed_staff_ids") or [],
                "allowed_departments": raw.get("allowed_departments") or [],
                "allowed_roles": raw.get("allowed_roles") or [],
            })
        local = config.get("knowledge_loop", {})
        items.append({
            "id": "local-published", "name": "OpsPilot 本地已发布知识",
            "access_scope": str(local.get("user_access_scope") or "public"),
            "allowed_staff_ids": local.get("allowed_staff_ids") or [],
            "allowed_departments": local.get("allowed_departments") or [],
            "allowed_roles": local.get("allowed_roles") or [],
        })
        return jsonify({"items": items})

    @app.put("/api/knowledge/access")
    def save_knowledge_access():
        payload = request.get_json(silent=True) or {}
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return jsonify({"ok": False, "message": "知识访问配置格式无效"}), 400
        scopes = {"public", "restricted", "admin"}
        normalized = {}
        for item in items:
            kb_id = str(item.get("id") or "").strip()
            scope = str(item.get("access_scope") or "public").strip().lower()
            if not kb_id or kb_id in normalized or scope not in scopes:
                return jsonify({"ok": False, "message": "知识库 ID 重复或访问范围无效"}), 400
            entry = {"access_scope": scope}
            for key in ("allowed_staff_ids", "allowed_departments", "allowed_roles"):
                values = item.get(key, [])
                if not isinstance(values, list):
                    return jsonify({"ok": False, "message": f"{key} 必须是数组"}), 400
                entry[key] = list(dict.fromkeys(
                    str(value).strip() for value in values if str(value).strip()
                ))[:100]
            normalized[kb_id] = entry
        config = read_config()
        current_kbs = config.get("ima", {}).get("default_knowledge_bases", [])
        known = {str(item.get("id")) for item in current_kbs if isinstance(item, dict)} | {"local-published"}
        if set(normalized) - known:
            return jsonify({"ok": False, "message": "访问配置包含未知知识库"}), 400
        updated_kbs = []
        for raw in current_kbs:
            item = dict(raw) if isinstance(raw, dict) else raw
            if isinstance(item, dict) and str(item.get("id")) in normalized:
                item.update(normalized[str(item.get("id"))])
            updated_kbs.append(item)
        local_access = normalized.get("local-published", {})
        history.record("knowledge_access", "修改员工知识访问范围")
        atomic_update_config(config_file, {
            "ima": {"default_knowledge_bases": updated_kbs},
            "knowledge_loop": {
                "user_access_scope": local_access.get("access_scope", "public"),
                "allowed_staff_ids": local_access.get("allowed_staff_ids", []),
                "allowed_departments": local_access.get("allowed_departments", []),
                "allowed_roles": local_access.get("allowed_roles", []),
            },
        })
        return jsonify({"ok": True, "message": "知识访问范围已保存；机器人重启后生效"})

    @app.get("/api/knowledge/candidates/<candidate_id>")
    def knowledge_candidate(candidate_id):
        candidate = knowledge_store().get_candidate(candidate_id)
        if not candidate:
            return jsonify({"message": "知识草稿不存在"}), 404
        return jsonify({"candidate": candidate, "versions": knowledge_store().versions(candidate_id)})

    @app.get("/api/knowledge/export-directory")
    def get_knowledge_export_directory():
        raw = str(read_config().get("knowledge_loop", {}).get("export_dir") or "").strip()
        try:
            export_dir, _ = _resolve_knowledge_export_dir(raw)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({
            "ok": True,
            "configured_path": raw,
            "resolved_path": str(export_dir),
            "default_path": DEFAULT_KNOWLEDGE_EXPORT_DIR,
            "using_default": not bool(raw),
        })

    @app.put("/api/knowledge/export-directory")
    def save_knowledge_export_directory():
        payload = request.get_json(silent=True) or {}
        raw = payload.get("path", "")
        try:
            export_dir, configured = _prepare_knowledge_export_dir(raw, test_write=True)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

        history.record("knowledge_loop.export_dir", "修改知识导出目录")
        atomic_update_config(config_file, {"knowledge_loop": {"export_dir": configured}})
        with service_lock:
            if "knowledge" in services:
                services["knowledge"].export_dir = export_dir
        return jsonify({
            "ok": True,
            "message": "知识导出目录已保存并验证可写",
            "configured_path": configured,
            "resolved_path": str(export_dir),
            "using_default": not bool(configured),
        })

    @app.post("/api/knowledge/export-directory/test")
    def test_knowledge_export_directory():
        payload = request.get_json(silent=True) or {}
        try:
            export_dir, _ = _prepare_knowledge_export_dir(
                payload.get("path", ""), test_write=True
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({
            "ok": True,
            "message": "目录可用，创建和写入测试均已通过",
            "resolved_path": str(export_dir),
        })

    @app.post("/api/knowledge/export-directory/select")
    def select_knowledge_export_directory():
        try:
            selected = _select_windows_directory()
            if not selected:
                return jsonify({"ok": True, "cancelled": True, "message": "已取消选择目录"})
            export_dir, _ = _resolve_knowledge_export_dir(selected)
        except (RuntimeError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({
            "ok": True,
            "cancelled": False,
            "path": str(export_dir),
            "resolved_path": str(export_dir),
        })

    @app.post("/api/knowledge/exports/open-directory")
    def open_knowledge_export_directory():
        payload = request.get_json(silent=True) or {}
        raw = payload.get("path")
        if raw is None:
            raw = read_config().get("knowledge_loop", {}).get("export_dir", "")
        try:
            export_dir, _ = _prepare_knowledge_export_dir(raw, test_write=True)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if not hasattr(os, "startfile"):
            return jsonify({
                "ok": False,
                "message": "当前系统不支持自动打开目录",
                "path": str(export_dir),
            }), 501
        try:
            os.startfile(str(export_dir))
        except Exception as exc:
            return jsonify({
                "ok": False,
                "message": f"Windows 无法打开知识导出目录：{exc}",
                "path": str(export_dir),
            }), 500
        return jsonify({
            "ok": True,
            "message": "已打开知识导出目录",
            "path": str(export_dir),
        })

    @app.get("/api/knowledge/candidates/<candidate_id>/export")
    def view_knowledge_export(candidate_id):
        store_ = knowledge_store()
        candidate = store_.get_candidate(candidate_id)
        if not candidate:
            return jsonify({"message": "知识草稿不存在"}), 404
        if candidate.get("status") != "published":
            return jsonify({"message": "该知识尚未发布，没有可查看的导出文件"}), 409

        raw = read_config().get("knowledge_loop", {}).get("export_dir", "")
        try:
            export_dir, _ = _resolve_knowledge_export_dir(raw)
        except ValueError as exc:
            return jsonify({"message": str(exc)}), 400
        filename = f"{candidate_id}-{store_._safe_filename(candidate.get('title') or candidate_id)}.md"
        target = (export_dir / filename).resolve()
        try:
            target.relative_to(export_dir)
        except ValueError:
            return jsonify({"message": "导出文件路径无效"}), 400
        if not target.is_file():
            return jsonify({"message": "导出文件不存在，请重新发布该知识"}), 404
        return send_from_directory(
            str(export_dir),
            filename,
            as_attachment=False,
            mimetype="text/markdown; charset=utf-8",
        )

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
                current = store_.get_candidate(candidate_id)
                if not current:
                    return jsonify({"message": "知识草稿不存在"}), 404
                if not str(current.get("solution") or "").strip():
                    return jsonify({"ok": False, "message": "请先补充经过验证的处理步骤"}), 400
                result = store_.update_candidate(candidate_id, {"status": "approved"}, "approve")
                return jsonify({"ok": bool(result), "candidate": result})
            if action == "reject":
                result = store_.update_candidate(candidate_id, {"status": "rejected"}, "reject")
                return jsonify({"ok": bool(result), "candidate": result})
            if action == "publish":
                path = store_.publish(candidate_id)
                return jsonify({"ok": True, "message": "已导出 Markdown，请导入 IMA；本地知识已可用于回答", "path": path})
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

    @app.post("/api/knowledge/unresolved/<int:event_id>/candidate")
    def unresolved_to_candidate(event_id):
        candidate_id = knowledge_store().create_from_event(event_id)
        if not candidate_id:
            return jsonify({"ok": False, "message": "未解决问题不存在或状态已变化"}), 404
        return jsonify({
            "ok": True,
            "candidate_id": candidate_id,
            "message": "已转为知识草稿，请补充经过验证的处理步骤",
        })

    @app.get("/api/knowledge/metrics")
    def knowledge_metrics():
        return jsonify(knowledge_store().source_metrics())

    return app


def start_admin_server(config_path=None, settings_path=None, host="127.0.0.1", port=8765,
                       auto_open=True, logger=None, threads=8, shutdown_callback=None):
    app = create_admin_app(config_path, settings_path, shutdown_callback=shutdown_callback)
    try:
        worker_threads = max(4, min(int(threads), 32))
    except (TypeError, ValueError):
        worker_threads = 8
    try:
        server = create_server(
            app,
            host=host,
            port=int(port),
            threads=worker_threads,
            ident=f"OpsPilot/{BOT_VERSION}",
            expose_tracebacks=False,
            clear_untrusted_proxy_headers=True,
        )
    except (OSError, ValueError):
        raise RuntimeError(f"管理台端口 {host}:{port} 已被占用或不可用") from None

    stop_lock = threading.Lock()
    stopped = threading.Event()

    def stop_server():
        with stop_lock:
            if stopped.is_set():
                return
            stopped.set()
            server.close()
            # Waitress keeps accepted keep-alive channels in its private socket
            # map. Close them as part of an explicit application shutdown so a
            # normal EXE exit does not wait for channel_timeout.
            for channel in list(getattr(server, "_map", {}).values()):
                try:
                    channel.close()
                except Exception:
                    pass
            server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)

    def serve():
        if logger:
            logger.info("可视化管理后台已启动（Waitress）: http://%s:%s", host, port)
        try:
            server.run()
        except Exception:
            if logger:
                logger.exception("可视化管理后台运行异常")
        finally:
            stop_server()
            side_effects = app.extensions.get("opspilot_side_effect_executor")
            if side_effects is not None:
                side_effects.shutdown(wait=False, cancel_futures=True)

    thread = threading.Thread(target=serve, name="admin-ui", daemon=True)
    thread.opspilot_server = server
    thread.opspilot_stop = stop_server
    thread.start()
    if auto_open:
        timer = threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}"))
        timer.daemon = True
        timer.start()
    return thread
