"""管理后台配置快照和回滚。"""

import json
import shutil
from datetime import datetime
from pathlib import Path

class ConfigHistory:
    def __init__(self, config_path, settings_path):
        self.config_path = Path(config_path)
        self.settings_path = Path(settings_path)
        self.directory = self.settings_path.parent / "config_history"
        self.directory.mkdir(parents=True, exist_ok=True)

    def record(self, section, description):
        version = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.directory / version
        target.mkdir()
        if self.config_path.exists():
            shutil.copy2(self.config_path, target / "config.yaml")
        if self.settings_path.exists():
            shutil.copy2(self.settings_path, target / "admin_settings.json")
        meta = {"version": version, "section": section, "description": description,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def list(self, limit=50):
        result = []
        for meta_path in sorted(self.directory.glob("*/meta.json"), reverse=True)[:limit]:
            try:
                result.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return result

    def rollback(self, version):
        target = (self.directory / version).resolve()
        if target.parent != self.directory.resolve() or not target.is_dir():
            raise ValueError("配置版本不存在")
        self.record("rollback", f"回滚前自动快照，目标版本 {version}")
        if (target / "config.yaml").exists():
            shutil.copy2(target / "config.yaml", self.config_path)
        if (target / "admin_settings.json").exists():
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target / "admin_settings.json", self.settings_path)
        return True
