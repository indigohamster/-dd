"""可视化后台的持久化设置。"""

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path):
    """Return the process-wide lock for one normalized settings path."""
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def runtime_root() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent


class AdminSettingsStore:
    def __init__(self, path=None):
        selected_path = Path(path) if path else runtime_root() / "data" / "admin_settings.json"
        self.path = selected_path.expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _path_lock(self.path)

    @staticmethod
    def defaults():
        return {
            "connectors": [
                {"id": "dingtalk", "name": "钉钉 Stream", "type": "dingtalk", "enabled": True, "url": ""},
                {"id": "ima", "name": "IMA 知识库", "type": "ima", "enabled": True, "url": ""},
            ],
            "people": [],
            "aitable_ticket_sync": {
                "enabled": False,
                "dws_executable": "dws",
                "base_id": "",
                "table_id": "",
                "sync_interval_minutes": 5,
                "field_mapping": {},
            },
            "service_catalog": [
                {"code": "ACCOUNT_RESET", "name": "密码重置与账号解锁", "request_type": "service_request", "category": "软件问题", "description": "忘记密码、账号锁定或登录异常", "keywords": "密码,解锁,登录", "priority": "medium", "assignment_group": "IT服务台", "approval_required": False, "instructions": "请说明系统名称和账号；不要发送密码。", "response_hours": 2, "resolution_hours": 8, "enabled": True},
                {"code": "ACCESS", "name": "权限申请", "request_type": "service_request", "category": "软件问题", "description": "业务系统、共享目录和应用权限开通或变更", "keywords": "权限,开通,访问", "priority": "medium", "assignment_group": "IT服务台", "approval_required": True, "instructions": "请说明系统、所需权限、用途和直属审批人。", "response_hours": 4, "resolution_hours": 24, "enabled": True},
                {"code": "SOFTWARE", "name": "软件安装", "request_type": "service_request", "category": "软件问题", "description": "办公及业务软件安装、升级和授权", "keywords": "安装,升级,软件", "priority": "low", "assignment_group": "桌面支持", "approval_required": False, "instructions": "请说明软件名称、版本和设备编号。", "response_hours": 4, "resolution_hours": 24, "enabled": True},
                {"code": "ONBOARD", "name": "新员工账号与设备", "request_type": "service_request", "category": "软件问题", "description": "入职账号、邮箱、权限和办公设备准备", "keywords": "入职,新员工,账号设备", "priority": "medium", "assignment_group": "IT服务台", "approval_required": True, "instructions": "请提供姓名、部门、岗位、入职日期和审批人。", "response_hours": 4, "resolution_hours": 48, "enabled": True},
                {"code": "OFFBOARD", "name": "离职回收", "request_type": "service_request", "category": "软件问题", "description": "账号停用、权限回收、数据交接和设备归还", "keywords": "离职,停用,回收", "priority": "high", "assignment_group": "IT服务台", "approval_required": True, "instructions": "请提供姓名、离职日期、数据交接人和审批人。", "response_hours": 2, "resolution_hours": 8, "enabled": True},
                {"code": "DEVICE", "name": "电脑与外设报修", "request_type": "incident", "category": "设备问题", "description": "电脑、打印机、显示器及其他办公设备故障", "keywords": "电脑,打印机,显示器,设备", "priority": "medium", "assignment_group": "桌面支持", "approval_required": False, "instructions": "请说明设备编号、故障现象、开始时间和影响范围。", "response_hours": 4, "resolution_hours": 24, "enabled": True},
                {"code": "NETWORK", "name": "网络与 VPN 报修", "request_type": "incident", "category": "设备问题", "description": "办公网络、Wi-Fi、VPN 和远程访问故障", "keywords": "网络,Wi-Fi,VPN,断网", "priority": "high", "assignment_group": "网络运维", "approval_required": False, "instructions": "请说明地点、网络类型、报错和影响人数。", "response_hours": 2, "resolution_hours": 8, "enabled": True},
                {"code": "BUSINESS", "name": "业务系统报修", "request_type": "incident", "category": "业务系统问题", "description": "ERP、CRM 及其他业务系统异常", "keywords": "ERP,CRM,业务系统", "priority": "high", "assignment_group": "应用运维", "approval_required": False, "instructions": "请提供系统、功能菜单、完整报错、发生时间和影响范围。", "response_hours": 2, "resolution_hours": 8, "enabled": True},
            ],
            "models": [],
            "model_routing": {"general": "", "ticket": "", "vision": "", "fallback": ""},
            "response_style": {
                "assistant_name": "企业运维助手", "tone": "专业、简洁、耐心", "length": "适中",
                "use_emoji": False, "cite_sources": True,
                "uncertain_action": "明确说明不确定，并建议转人工处理",
                "handoff_rules": "涉及账号权限、数据删除、财务和安全事件时转人工",
                "custom_prompt": "优先解决用户问题，步骤清晰，不编造系统状态。",
            },
        }

    def load(self):
        with self._lock:
            if not self.path.exists():
                return self.defaults()
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return self.defaults()
            base = self.defaults()
            base.update(saved)
            return base

    def save(self, data):
        with self._lock:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
                temp_path = None
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        return data

    def mutate(self, callback):
        """Atomically perform one read-modify-write transaction in-process.

        ``load()`` and ``save()`` are individually safe, but callers doing the
        two operations separately can otherwise overwrite an unrelated change
        made by a concurrent admin request. The shared path lock is re-entrant,
        so the existing methods remain the single serialization code path.
        """
        with self._lock:
            data = self.load()
            result = callback(data)
            self.save(data)
            return result

    @contextmanager
    def transaction(self):
        """Hold the shared path lock across a multi-file commit or rollback."""
        with self._lock:
            yield self
