"""Spreadsheet task orchestration backed by @oai/artifact-tool."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
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
        sheet_match = re.search(r"(?:处理|选择)?工作表[：:\s]*([^，,。]+)", value)
        sheet_name = sheet_match.group(1).strip() if sheet_match else ""
        if any(word in value for word in ("工作表列表", "有哪些工作表", "列出工作表")):
            return {"operation": "sheets"}
        compare = re.search(r"(?:按(.+?)(?:列)?)?对比(?:这)?两份表", value)
        if compare:
            return {"operation": "compare", "key_column": (compare.group(1) or "").strip()}
        if "脱敏" in value:
            columns = re.search(r"(?:对|将)?(.+?)(?:列)?脱敏", value)
            return {"operation": "mask", "columns": [x.strip() for x in re.split(r"[、,，和]", columns.group(1))] if columns else [], "sheet_name": sheet_name}
        sort_match = re.search(r"按(.+?)(?:列)?(升序|降序)排序", value)
        if sort_match:
            return {"operation": "sort", "column": sort_match.group(1).strip(), "descending": sort_match.group(2) == "降序", "sheet_name": sheet_name}
        if "去重" in value:
            match = re.search(r"按(.+?)(?:列)?去重", value)
            return {"operation": "dedupe", "column": match.group(1).strip() if match else "", "sheet_name": sheet_name}
        match = re.search(r"按(.+?)(?:列)?(?:透视)?汇总(.+?)(?:列)?(?:并生成图表|生成图表|$|的)", value)
        if match:
            return {"operation": "pivot" if ("透视" in value or "图表" in value) else "aggregate", "group_column": match.group(1).strip(), "value_column": match.group(2).strip(), "chart": "图表" in value, "sheet_name": sheet_name}
        if value.startswith("筛选"):
            parts = re.split(r"\s*(?:且|并且|同时|，|,)\s*", value[2:])
            conditions = []
            for part in parts:
                match = re.match(r"(.+?)(?:列)?(?:为|等于|=)(.+)", part.strip())
                if match:
                    conditions.append({"column": match.group(1).strip(), "value": match.group(2).strip()})
            if conditions:
                return {
                    "operation": "filter", "conditions": conditions,
                    "column": conditions[0]["column"], "value": conditions[0]["value"],
                    "sheet_name": sheet_name,
                }
        if any(word in value for word in ("异常", "检查", "空值", "重复项")):
            return {"operation": "anomaly", "sheet_name": sheet_name}
        return {"operation": "overview", "sheet_name": sheet_name}

    def process(self, input_path, instruction, output_dir, second_input_path=None):
        path = Path(input_path)
        if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
            raise ValueError("当前仅支持 .xlsx、.csv 和 .tsv")
        max_mb = int(self.config.get("max_file_mb", 20))
        if path.stat().st_size > max_mb * 1024 * 1024:
            raise ValueError(f"表格超过 {max_mb}MB")
        node, modules = self._runtime()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{path.stem}-处理结果-{uuid.uuid4().hex[:8]}.xlsx"
        job = {"input": str(path), "output": str(output_path), **self.parse_instruction(instruction)}
        if second_input_path:
            second = Path(second_input_path)
            if second.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                raise ValueError("第二份文件仅支持 .xlsx、.csv 和 .tsv")
            if second.stat().st_size > max_mb * 1024 * 1024:
                raise ValueError(f"第二份表格超过 {max_mb}MB")
            job["input2"] = str(second)
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
