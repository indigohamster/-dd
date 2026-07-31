"""可视化后台的持久化设置。"""

import json
import os
import sys
import threading
from pathlib import Path


def runtime_root() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent


class AdminSettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else runtime_root() / "data" / "admin_settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

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
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        return data
