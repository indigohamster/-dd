"""将 SQLite 工单幂等镜像到钉钉 AI 表格。"""

import json
import os
import subprocess
import threading
from pathlib import Path


DEFAULT_FIELD_NAMES = {
    "ticket_id": "工单编号", "title": "标题", "description": "问题描述",
    "status": "状态", "priority": "优先级", "category": "分类",
    "request_type": "请求类型", "service_code": "服务代码", "service_name": "服务项目",
    "assignment_group": "处理组",
    "creator_nick": "提交人", "creator_staff_id": "提交人员工编号",
    "assignee_nick": "负责人", "resolution": "处理结果",
    "created_at": "创建时间", "updated_at": "更新时间", "resolved_at": "解决时间",
    "response_due_at": "首次响应时限", "resolution_due_at": "解决时限",
    "first_response_at": "首次响应时间", "confirmed_at": "用户确认时间",
    "satisfaction_score": "满意度评分", "satisfaction_comment": "满意度评价",
}


class AITableSyncError(RuntimeError):
    pass


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


class TicketAITableSync:
    """通过钉钉官方 dws CLI 创建或更新 AI 表格工单记录。"""

    def __init__(self, ticket_manager, settings, state_path=None, logger=None):
        self.tickets = ticket_manager
        self.settings = dict(settings or {})
        self.logger = logger
        self.state_path = Path(state_path or "data/aitable_ticket_sync.json").resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def configured(self):
        return bool(self.settings.get("enabled") and self.settings.get("base_id") and
                    self.settings.get("table_id") and self.settings.get("dws_executable"))

    def _run(self, *args, timeout=90):
        executable = os.path.expandvars(str(self.settings.get("dws_executable") or "dws"))
        if (os.path.sep in executable or "/" in executable) and not Path(executable).is_file():
            raise AITableSyncError(f"找不到 dws 可执行文件: {executable}")
        command = [executable, "aitable", *args, "--format", "json"]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AITableSyncError(f"AI 表格命令执行失败: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "未知错误").strip()
            raise AITableSyncError(message[-500:])
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise AITableSyncError("dws 未返回有效 JSON") from exc

    def test_connection(self):
        if not self.configured:
            raise AITableSyncError("请先启用同步并填写 dws 路径、baseId 和 tableId")
        return self._run("field", "get", "--base-id", self.settings["base_id"],
                         "--table-id", self.settings["table_id"])

    def _field_ids(self):
        response = self.test_connection()
        by_name = {}
        for node in _walk(response):
            if isinstance(node, dict):
                name = node.get("fieldName") or node.get("name")
                field_id = node.get("fieldId") or node.get("id")
                if name and field_id:
                    by_name[str(name)] = str(field_id)
        configured = self.settings.get("field_mapping") or {}
        result, missing = {}, []
        for local_name, default_name in DEFAULT_FIELD_NAMES.items():
            target = configured.get(local_name) or default_name
            if target in by_name:
                result[local_name] = by_name[target]
            elif str(target).startswith("fld"):
                result[local_name] = str(target)
            else:
                missing.append(str(target))
        if "ticket_id" not in result:
            raise AITableSyncError("AI 表格缺少“工单编号”字段，无法进行幂等同步")
        return result, missing

    def _load_state(self):
        if not self.state_path.exists():
            return {"records": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"records": {}}

    def _save_state(self, state):
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

    @staticmethod
    def _record_id(response):
        for node in _walk(response):
            if isinstance(node, dict) and (node.get("recordId") or node.get("record_id")):
                return str(node.get("recordId") or node.get("record_id"))
        return ""

    @staticmethod
    def _cells(ticket, field_ids):
        cells = {}
        for name, field_id in field_ids.items():
            value = ticket.get(name)
            if value is not None:
                cells[field_id] = value if isinstance(value, (bool, int, float)) else str(value)
        return cells

    def sync_all(self, limit=5000):
        if not self.configured:
            raise AITableSyncError("AI 表格工单同步尚未配置")
        with self._lock:
            field_ids, missing = self._field_ids()
            state = self._load_state()
            if (state.get("base_id") and state.get("base_id") != self.settings["base_id"]) or \
                    (state.get("table_id") and state.get("table_id") != self.settings["table_id"]):
                state = {"records": {}}
            records = state.setdefault("records", {})
            created = updated = failed = 0
            errors = []
            for ticket in self.tickets.list_all_tickets("all", limit=min(int(limit), 5000)):
                ticket_id = ticket["ticket_id"]
                payload = [{"cells": self._cells(ticket, field_ids)}]
                try:
                    record_id = records.get(ticket_id, {}).get("record_id", "")
                    if record_id:
                        payload[0]["recordId"] = record_id
                        self._run("record", "update", "--base-id", self.settings["base_id"],
                                  "--table-id", self.settings["table_id"],
                                  "--records", json.dumps(payload, ensure_ascii=False))
                        updated += 1
                    else:
                        response = self._run("record", "create", "--base-id", self.settings["base_id"],
                                             "--table-id", self.settings["table_id"],
                                             "--records", json.dumps(payload, ensure_ascii=False))
                        record_id = self._record_id(response)
                        if not record_id:
                            raise AITableSyncError("创建成功但未返回 recordId")
                        records[ticket_id] = {"record_id": record_id,
                                              "updated_at": ticket.get("updated_at", "")}
                        created += 1
                except Exception as exc:
                    failed += 1
                    errors.append({"ticket_id": ticket_id, "message": str(exc)[:300]})
            state.update({"base_id": self.settings["base_id"], "table_id": self.settings["table_id"]})
            self._save_state(state)
            return {"created": created, "updated": updated, "failed": failed,
                    "missing_fields": missing, "errors": errors[:20], "mapped": len(records)}
