# ============================================================
# 消息日志记录器 — 结构化日志 + 分类导出
# ============================================================
#
# 功能：
#   1. 按天自动创建 logs/messages/YYYY-MM-DD.jsonl
#   2. 每条消息记录一行 JSON
#   3. 支持按提问人分类导出
#   4. 支持按专家模式分类导出
# ============================================================

import json
import os
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger("message-logger")


class MessageLogger:
    """消息日志记录器，记录 + 导出按提问人/专家分类的消息日志"""

    def __init__(self, log_dir: str = "logs/messages"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

    def _log_file(self, day: Optional[date] = None) -> str:
        """获取指定日期的日志文件路径"""
        d = day or date.today()
        return os.path.join(self.log_dir, f"{d.isoformat()}.jsonl")

    def record(self, entry: dict):
        """
        记录一条消息日志。

        entry 字段:
            time: str         — 时间戳 (HH:MM:SS)
            msg_id: str       — 消息ID
            sender_nick: str  — 发送者昵称
            sender_id: str    — 发送者ID
            conv_type: str    — 会话类型 (单聊/群聊)
            conv_title: str   — 群聊标题 (单聊为空)
            content: str      — 用户消息内容 (完整)
            expert_mode: str  — 专家模式 (普通对话 / 专家名称)
            expert_scope: str — 专家知识库范围
            has_kb: bool      — 是否搜到知识库结果
            kb_results: int   — 知识库搜索结果数
            engine: str       — AI引擎 (deepseek / codebuddy / none)
            answer_len: int   — AI回答长度
            answer_preview: str — AI回答摘要 (前100字)
        """
        entry["date"] = date.today().isoformat()
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        log_file = self._log_file()
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.error(f"写入消息日志失败: {e}")

    def load_day(self, day: Optional[date] = None) -> list:
        """
        加载指定日期的所有消息记录。
        :return: 消息条目列表
        """
        log_file = self._log_file(day)
        if not os.path.isfile(log_file):
            return []

        entries = []
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"读取消息日志失败: {e}")
        return entries

    def list_available_dates(self) -> list:
        """列出所有有日志的日期"""
        if not os.path.isdir(self.log_dir):
            return []
        dates = []
        for fname in os.listdir(self.log_dir):
            if fname.endswith(".jsonl"):
                try:
                    dates.append(fname.replace(".jsonl", ""))
                except ValueError:
                    continue
        return sorted(dates)

    # ------------------------------------------------------------------
    # 导出功能
    # ------------------------------------------------------------------

    def _format_entry(self, e: dict, idx: int) -> str:
        """格式化单条消息为易读文本"""
        lines = [
            f"{'─' * 50}",
            f"[{idx}] {e.get('time', '?')} | {e.get('sender_nick', '?')} | {e.get('conv_type', '?')}",
            f"    消息ID: {e.get('msg_id', '?')}",
            f"    群聊: {e.get('conv_title', '-')}",
            f"    专家: {e.get('expert_mode', '普通对话')}",
            f"    引擎: {e.get('engine', '?')} | 知识库: {e.get('kb_results', 0)}条 | 回答: {e.get('answer_len', 0)}字",
            f"    用户: {e.get('content', '')[:200]}",
            f"    回答: {e.get('answer_preview', '')[:200]}",
        ]
        return "\n".join(lines)

    def export_by_sender(self, day: Optional[date] = None, output_dir: str = None) -> dict:
        """
        按提问人分类导出。
        为每个提问人创建独立文件：{sender_nick}({sender_id}).txt
        :return: {"files": [...], "total_senders": int}
        """
        entries = self.load_day(day)
        if not entries:
            return {"files": [], "total_senders": 0}

        d = day or date.today()
        output_dir = output_dir or os.path.join(self.log_dir, "exports", f"by-sender-{d.isoformat()}")
        os.makedirs(output_dir, exist_ok=True)

        # 按发送者分组
        by_sender = {}
        for e in entries:
            key = f"{e.get('sender_nick', '未知')}({e.get('sender_id', '?')})"
            if key not in by_sender:
                by_sender[key] = []
            by_sender[key].append(e)

        files = []
        for sender_key, sender_entries in sorted(by_sender.items()):
            safe_name = sender_key.replace("/", "_").replace("\\", "_").replace(":", "_")
            filepath = os.path.join(output_dir, f"{safe_name}.txt")

            lines = [
                f"消息日志 — 按提问人分类",
                f"日期: {d.isoformat()}",
                f"提问人: {sender_key}",
                f"消息总数: {len(sender_entries)}",
                f"",
            ]
            for i, entry in enumerate(sender_entries, 1):
                lines.append(self._format_entry(entry, i))
                lines.append("")

            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                files.append(filepath)
            except Exception as e:
                logger.error(f"导出失败 ({sender_key}): {e}")

        return {"files": files, "total_senders": len(by_sender)}

    def export_by_expert(self, day: Optional[date] = None, output_dir: str = None) -> dict:
        """
        按专家模式分类导出。
        为每个专家模式创建独立文件。
        :return: {"files": [...], "total_groups": int}
        """
        entries = self.load_day(day)
        if not entries:
            return {"files": [], "total_groups": 0}

        d = day or date.today()
        output_dir = output_dir or os.path.join(self.log_dir, "exports", f"by-expert-{d.isoformat()}")
        os.makedirs(output_dir, exist_ok=True)

        # 按专家模式分组
        by_expert = {}
        for e in entries:
            key = e.get("expert_mode", "普通对话")
            if key not in by_expert:
                by_expert[key] = []
            by_expert[key].append(e)

        files = []
        for expert_key, expert_entries in sorted(by_expert.items()):
            safe_name = expert_key.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")
            filepath = os.path.join(output_dir, f"{safe_name}.txt")

            lines = [
                f"消息日志 — 按专家模式分类",
                f"日期: {d.isoformat()}",
                f"专家模式: {expert_key}",
                f"消息总数: {len(expert_entries)}",
                f"",
            ]
            for i, entry in enumerate(expert_entries, 1):
                lines.append(self._format_entry(entry, i))
                lines.append("")

            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                files.append(filepath)
            except Exception as e:
                logger.error(f"导出失败 ({expert_key}): {e}")

        return {"files": files, "total_groups": len(by_expert)}

    def export_summary(self, day: Optional[date] = None, output_dir: str = None) -> dict:
        """
        导出当日日志总览（两个维度都导出）。
        :return: {"sender": {...}, "expert": {...}}
        """
        d = day or date.today()
        base_dir = output_dir or os.path.join(self.log_dir, "exports", d.isoformat())

        sender_result = self.export_by_sender(day, os.path.join(base_dir, "by-sender"))
        expert_result = self.export_by_expert(day, os.path.join(base_dir, "by-expert"))

        return {
            "date": d.isoformat(),
            "sender": sender_result,
            "expert": expert_result,
        }


# 全局单例
msg_logger = MessageLogger()
