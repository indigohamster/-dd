"""Spreadsheet task orchestration backed by @oai/artifact-tool."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class SpreadsheetService:
    SUPPORTED_EXTENSIONS = {".xlsx", ".csv", ".tsv"}

    def __init__(self, config=None, logger=None):
        self.config = config or {}
        self.logger = logger

    @property
    def enabled(self):
        return bool(self.config.get("enabled", True))

    def _runtime(self):
        node = os.path.expandvars(str(self.config.get("node_executable") or os.getenv("SPREADSHEET_NODE") or ""))
        modules = os.path.expandvars(str(self.config.get("node_modules_path") or os.getenv("SPREADSHEET_NODE_MODULES") or ""))
        node = node if node and os.path.isfile(node) else shutil.which("node")
        if not node:
            raise RuntimeError("未找到 Node.js，请在后台配置表格运行环境")
        if not modules or not Path(modules, "@oai", "artifact-tool").exists():
            raise RuntimeError("未找到 @oai/artifact-tool，请配置 spreadsheet.node_modules_path")
        return node, modules

    @staticmethod
    def _worker_path():
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS) / "utils" / "spreadsheet_worker.mjs"
        return Path(__file__).with_name("spreadsheet_worker.mjs")

    @staticmethod
    def parse_instruction(text):
        value = (text or "").strip()
        if "去重" in value:
            match = re.search(r"按(.+?)(?:列)?去重", value)
            return {"operation": "dedupe", "column": match.group(1).strip() if match else ""}
        match = re.search(r"按(.+?)(?:列)?汇总(.+?)(?:列)?(?:$|的|金额|数量)", value)
        if match:
            return {"operation": "aggregate", "group_column": match.group(1).strip(), "value_column": match.group(2).strip()}
        match = re.search(r"筛选(.+?)(?:列)?(?:为|等于|=)(.+)", value)
        if match:
            return {"operation": "filter", "column": match.group(1).strip(), "value": match.group(2).strip()}
        if any(word in value for word in ("异常", "检查", "空值", "重复项")):
            return {"operation": "anomaly"}
        return {"operation": "overview"}

    def process(self, input_path, instruction, output_dir):
        path = Path(input_path)
        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            raise ValueError("当前仅支持 .xlsx、.csv 和 .tsv")
        max_mb = int(self.config.get("max_file_mb", 20))
        if path.stat().st_size > max_mb * 1024 * 1024:
            raise ValueError(f"表格超过 {max_mb}MB")
        node, modules = self._runtime()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{path.stem}-处理结果.xlsx"
        job = {"input": str(path), "output": str(output_path), **self.parse_instruction(instruction)}
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump(job, handle, ensure_ascii=False)
            job_path = handle.name
        env = os.environ.copy()
        env["NODE_PATH"] = modules
        try:
            result = subprocess.run(
                [node, str(self._worker_path()), job_path], capture_output=True, text=True,
                encoding="utf-8", timeout=int(self.config.get("timeout", 90)), env=env,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "表格处理失败")[-500:])
            metadata = json.loads(result.stdout.strip().splitlines()[-1])
            return str(output_path), metadata
        finally:
            try:
                os.remove(job_path)
            except OSError:
                pass
