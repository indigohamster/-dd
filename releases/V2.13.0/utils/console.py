# ============================================================
# 控制台仪表盘 - 显示机器人运行状态 + 命令监听
# ============================================================
#
# 功能：
#   1. 启动时显示：版本号、连接状态、知识库配置
#   2. 控制台命令：status / api / version / help / quit
#   3. 运行状态追踪：消息数、专家会话数、API 调用统计
# ============================================================

import threading
import time
import sys
import os
import re
import json
from pathlib import Path
from datetime import datetime

# Windows 非阻塞键盘输入
if sys.platform == "win32":
    import msvcrt


# ── 机器人版本 ──
BOT_VERSION = "V2.13.0"


# ── 全局运行状态 ──

class BotStatus:
    """机器人全局运行状态（线程安全）"""

    def __init__(self):
        self.version = BOT_VERSION
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.stream_connected = False
        self.ima_enabled = False
        self.ima_connected = False
        self.llm_enabled = False
        self.llm_connected = False
        self.cb_enabled = False
        self.cb_connected = False
        self.dtalk_enabled = False
        self.dtalk_connected = False
        self.message_count = 0
        self.expert_sessions = 0
        # 引擎使用追踪
        self.last_engine = None
        self.last_engine_time = None
        self.last_engine_expert = None
        self._lock = threading.Lock()

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def inc_messages(self):
        with self._lock:
            self.message_count += 1

    def set_expert_sessions(self, count: int):
        with self._lock:
            self.expert_sessions = count

    def set_last_engine(self, engine: str, expert_label: str = None):
        with self._lock:
            self.last_engine = engine
            self.last_engine_time = datetime.now().strftime("%H:%M:%S")
            self.last_engine_expert = expert_label

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.version,
                "start_time": self.start_time,
                "stream_connected": self.stream_connected,
                "ima_enabled": self.ima_enabled,
                "ima_connected": self.ima_connected,
                "llm_enabled": self.llm_enabled,
                "llm_connected": self.llm_connected,
                "cb_enabled": self.cb_enabled,
                "cb_connected": self.cb_connected,
                "dtalk_enabled": self.dtalk_enabled,
                "dtalk_connected": self.dtalk_connected,
                "message_count": self.message_count,
                "expert_sessions": self.expert_sessions,
                "last_engine": self.last_engine,
                "last_engine_time": self.last_engine_time,
                "last_engine_expert": self.last_engine_expert,
            }


# 全局单例
bot_status = BotStatus()


# ── 格式化工具 ──

def format_uptime(start_time_str: str) -> str:
    """格式化运行时长"""
    try:
        start = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        delta = datetime.now() - start
        total_sec = int(delta.total_seconds())
        h, rem = divmod(total_sec, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}小时{m}分{s}秒"
        elif m > 0:
            return f"{m}分{s}秒"
        else:
            return f"{s}秒"
    except Exception:
        return "-"


def status_icon(enabled: bool, connected: bool) -> str:
    """生成连接状态图标 + 文字"""
    if connected:
        return "🟢 已连接"
    elif enabled:
        return "🟡 已启用(未验证)"
    else:
        return "🔴 未启用"


# ── 仪表盘显示 ──

def show_startup_banner(config: dict):
    """
    启动时的欢迎横幅（精简版，后续用完整仪表盘补充）。
    仅在 main.py 启动阶段调用一次。
    """
    creds = config.get("credentials", {})
    ima_cfg = config.get("ima", {})
    kbs = ima_cfg.get("default_knowledge_bases", [])

    sep = "═" * 56

    lines = [
        "",
        sep,
        f"  🦊 钉钉 Stream 机器人 · {BOT_VERSION} 启动中...",
        sep,
        f"  📌 Client ID: {creds.get('client_id', 'N/A')}",
        f"  📌 App ID:    {creds.get('app_id', 'N/A')}",
        f"  📌 Agent ID:  {creds.get('agent_id', 'N/A')}",
    ]

    # 知识库配置
    if kbs:
        lines.append("─" * 56)
        lines.append(f"  📚 已配置 {len(kbs)} 个知识库:")
        for kb in kbs:
            lines.append(f"     • {kb.get('name', '未知')}")

    lines.append(sep)

    # 操作指令提示
    lines.append("  💬 群聊指令:")
    lines.append("    召唤XX专家 [问题]    — 激活专家模式（自动走CodeBuddy引擎）")
    lines.append("    搜/查/找 [关键词]   — 直接搜索知识库原始结果")
    lines.append("    切换CodeBuddy        — 手动锁定 CodeBuddy 引擎")
    lines.append("    切换DeepSeek         — 手动锁定 DeepSeek 引擎")
    lines.append("    自动调度             — 恢复智能调度规则")
    lines.append("    当前引擎             — 查看当前引擎调度状态")
    lines.append("    测试文件             — 测试文件发送功能")
    lines.append("")
    lines.append("  🖥 控制台命令:")
    lines.append("    status / 仪表盘      — 显示完整状态面板")
    lines.append("    api / 调用           — 显示 API 调用统计")
    lines.append("    version / 版本       — 显示版本号")
    lines.append("    help / 帮助          — 显示帮助")
    lines.append("    quit / 退出          — 关闭机器人")
    lines.append(sep)
    lines.append("")
    return "\n".join(lines)


def show_connection_test(results: dict):
    """
    显示 API 连接检测结果。
    results: {"IMA 知识库": (bool, float), "DeepSeek LLM": (bool, float), "钉钉 OpenAPI": (bool, float)}
    """
    sep_thin = "─" * 56
    lines = [
        sep_thin,
        "  🔗 连接检测:",
    ]

    for name, (ok, dur) in results.items():
        icon = "✅ 连接正常" if ok else "❌ 连接失败"
        dur_str = f"({dur:.2f}s)" if dur > 0 else ""
        lines.append(f"    {name:18s}  {icon} {dur_str}")

    return "\n".join(lines)


def show_dashboard():
    """
    完整仪表盘（控制台命令触发）。
    显示：版本、运行时长、消息数、连接状态、API 统计、最近调用。
    """
    from utils.api_tracker import api_tracker

    s = bot_status.snapshot()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime = format_uptime(s["start_time"])

    api_stats = api_tracker.get_stats()
    recent = api_tracker.get_recent(5)

    sep_thick = "═" * 56
    sep_thin = "─" * 56

    lines = [
        "",
        sep_thick,
        f"  🦊 钉钉 Stream 机器人 · 控制台仪表盘",
        sep_thick,
        f"  📌 版本:       {s['version']}",
        f"  🕐 当前时间:    {now}",
        f"  ⏱ 运行时长:    {uptime}",
        f"  📨 已处理消息:  {s['message_count']} 条",
        f"  👨‍💼 专家会话:    {s['expert_sessions']} 个活跃",
        sep_thin,
        "  🤖 AI 引擎状态",
    ]

    # 显示引擎连接状态
    lines.append(f"    DeepSeek LLM         {status_icon(s['llm_enabled'], s['llm_connected'])}")
    lines.append(f"    CodeBuddy AI         {status_icon(s['cb_enabled'], s['cb_connected'])}")

    # 显示调度规则
    lines.append("    调度规则: 专家模式→CodeBuddy优先 | 普通模式→DeepSeek优先")

    # 显示最后一次 AI 回复使用的引擎
    engine_names = {"deepseek": "DeepSeek", "codebuddy": "CodeBuddy", "none": "无"}
    last_e = engine_names.get(s["last_engine"], s["last_engine"] or "暂无")
    last_t = s["last_engine_time"] or "-"
    last_expert = s["last_engine_expert"] or "普通对话"
    lines.append(f"    最近一次回复: [{last_t}] {last_e} (场景: {last_expert})")

    lines.append(sep_thin)
    lines.append("  🔗 连接状态")
    lines.append(f"    Stream (WebSocket)   {status_icon(True, s['stream_connected'])}")
    lines.append(f"    IMA 知识库           {status_icon(s['ima_enabled'], s['ima_connected'])}")
    lines.append(f"    钉钉 OpenAPI         {status_icon(s['dtalk_enabled'], s['dtalk_connected'])}")

    if api_stats:
        lines.append(sep_thin)
        lines.append("  📊 API 调用统计")
        for name, st in api_stats.items():
            total = st["total"]
            ok = st["success"]
            fail = st["fail"]
            avg_dur = st["total_duration"] / total if total > 0 else 0
            lines.append(
                f"    {name:18s}  总计 {total:>4d}  "
                f"✅{ok} ❌{fail}  "
                f"平均 {avg_dur:.2f}s  "
                f"最近 {st['last_time']}"
            )

    if recent:
        lines.append(sep_thin)
        lines.append("  📋 最近 API 调用记录")
        for c in recent:
            icon = "✅" if c["success"] else "❌"
            ep = c["endpoint"][:32]
            lines.append(
                f"    {icon} [{c['time']}] {c['api']:14s} {ep}  ({c['duration']}s)"
            )

    lines.append(sep_thin)
    lines.append("  💡 控制台命令:  status · api · version · help · quit")
    lines.append(sep_thick)
    lines.append("")

    return "\n".join(lines)


def show_help():
    """显示控制台命令帮助"""
    sep = "─" * 56
    lines = [
        "",
        sep,
        "  🦊 钉钉 Stream 机器人 · 控制台命令帮助",
        sep,
        "  status / 仪表盘         — 显示完整仪表盘",
        "  api / 调用              — 显示 API 调用详情",
        "  api-config              — 查看自定义 LLM API 配置（密钥掩码）",
        "  api-config on|off       — 启用或停用自定义 LLM API",
        "  api-config url <地址>   — 设置 OpenAI 兼容 API Base URL",
        "  api-config path <路径>  — 设置聊天接口路径",
        "  api-config model <模型> — 设置模型名称",
        "  api-config key <密钥>   — 设置 API Key（不会回显）",
        "  version / 版本          — 显示当前版本号",
        "  export user / 导出用户   — 按提问人分类导出今日日志",
        "  export expert / 导出专家 — 按专家模式分类导出今日日志",
        "  export all / 导出全部    — 全量导出（两个维度）",
        "  export list / 导出列表   — 列出可导出的日志日期",
        "  ticket / 工单           — 工单系统帮助",
        "  ticket stats / 工单统计 — 工单统计数据",
        "  ticket list / 工单列表  — 列出最近工单",
        "  help / 帮助             — 显示本帮助信息",
        "  quit / 退出             — 关闭机器人",
        "",
        "  💡 支持中文命令，退出前会自动导出当天日志到本地",
        sep,
        "",
    ]
    return "\n".join(lines)


# ── 工单控制台命令 ──

def _ticket_console_help():
    """工单控制台帮助"""
    import os as _os
    db_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data")
    lines = [
        "",
        "─" * 56,
        "  📋 工单系统 · 控制台命令",
        "─" * 56,
        "  ticket / 工单            — 显示本帮助",
        "  ticket stats / 工单统计  — 查看工单统计数据",
        "  ticket list / 工单列表   — 列出最近 50 条工单",
        "─" * 56,
        f"  💾 数据存储: {db_dir}/tickets.db",
        "  📋 用户指令（群聊/单聊）:",
        "    报修 / 提交工单          — 创建工单",
        "    我的工单                 — 查看我的工单",
        "    待处理                   — 查看待处理队列",
        "    工单 TKT-xxx             — 查看工单详情",
        "    解决了                   — 关闭工单",
        "    工单统计                 — 统计数据",
        "    日报 / 工单日报          — 每日工单摘要",
        "",
        "  🔄 AI 回答反馈（群聊中自动触发）:",
        "    是 / 否                  — AI 回答后反馈满意度",
        "    → 是: 感谢反馈（工单由管理员手动处理）",
        "    → 否: 升级工单 + 通知管理员",
        "─" * 56,
        "",
    ]
    return "\n".join(lines)


def _ticket_console_stats():
    """控制台查看工单统计"""
    try:
        from modules.ticket import TicketManager
        mgr = TicketManager()
        stats = mgr.get_stats()
        by_status = stats.get("by_status", {})
        by_category = stats.get("by_category", {})

        lines = [
            "",
            "─" * 56,
            "  📊 工单统计",
            "─" * 56,
            f"  总工单数: {stats['total']}",
            f"  待处理:   {stats['open']}",
            f"  已解决:   {stats['resolved']}",
            "",
            "  状态分布:",
        ]
        for s, c in by_status.items():
            from modules.ticket.ticket_manager import STATUS_LABELS
            label = STATUS_LABELS.get(s, s)
            bar = "█" * min(c, 30)
            lines.append(f"    {label:6s} {c:3d} {bar}")

        if by_category:
            lines.append("")
            lines.append("  分类分布:")
            for cat, c in sorted(by_category.items(), key=lambda x: -x[1]):
                lines.append(f"    {cat:12s} {c:3d}")

        lines.append("─" * 56)
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"  ❌ 工单统计失败: {e}\n"


def _ticket_console_list():
    """控制台列出现有工单"""
    try:
        from modules.ticket import TicketManager
        from modules.ticket.ticket_manager import STATUS_LABELS
        mgr = TicketManager()
        tickets = mgr.list_tickets_by_status(None, limit=50)

        if not tickets:
            return "  📭 暂无工单记录\n"

        lines = [
            "",
            "─" * 56,
            f"  📋 最近工单（共 {len(tickets)} 条）",
            "─" * 56,
        ]
        for t in tickets[:20]:
            status = STATUS_LABELS.get(t["status"], t["status"])
            lines.append(
                f"  [{t['ticket_id']}] {status:5s} | {t['category']:8s} "
                f"| {t['creator_nick']:8s} | {t['title'][:30]}"
            )
        if len(tickets) > 20:
            lines.append(f"  ... 还有 {len(tickets) - 20} 条")
        lines.append("─" * 56)
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"  ❌ 工单列表失败: {e}\n"


# ── 导出命令处理 ──

def _handle_export(cmd: str):
    """处理导出命令，返回控制台输出文本"""
    from utils.message_logger import msg_logger
    from datetime import date

    parts = cmd.strip().split()
    sub = parts[1].lower() if len(parts) > 1 else ""

    # export list — 列出可用日期
    if sub in ("list", "列表"):
        dates = msg_logger.list_available_dates()
        if not dates:
            return "  📭 暂无日志记录\n"
        lines = ["", "─" * 56, "  📅 可导出的日志日期", "─" * 56]
        for d in dates:
            entries = len(msg_logger.load_day(date.fromisoformat(d)))
            lines.append(f"    {d} — {entries} 条消息")
        lines.append("─" * 56)
        lines.append("  💡 使用: export user YYYY-MM-DD / export expert YYYY-MM-DD")
        lines.append("")
        return "\n".join(lines)

    # 解析日期参数
    target_day = None
    date_arg = None
    for p in parts[1:]:
        if re.match(r'\d{4}-\d{2}-\d{2}', p):
            date_arg = p
            break

    if date_arg:
        try:
            target_day = date.fromisoformat(date_arg)
        except ValueError:
            return f"  ❌ 日期格式错误: {date_arg}（应为 YYYY-MM-DD）\n"

    # export user — 按提问人导出
    if sub in ("user", "sender", "用户", "提问人"):
        result = msg_logger.export_by_sender(day=target_day)
        day_label = target_day.isoformat() if target_day else date.today().isoformat()
        lines = [
            "",
            "─" * 56,
            f"  👤 按提问人导出 — {day_label}",
            "─" * 56,
            f"  导出提问人数: {result['total_senders']}",
            f"  文件数: {len(result['files'])}",
        ]
        for f in result["files"]:
            lines.append(f"    📄 {os.path.basename(f)}")
        lines.append("─" * 56)
        lines.append(f"  📁 导出目录: {os.path.dirname(result['files'][0]) if result['files'] else 'logs/messages/exports/'}")
        lines.append("")
        return "\n".join(lines)

    # export expert — 按专家模式导出
    if sub in ("expert", "专家"):
        result = msg_logger.export_by_expert(day=target_day)
        day_label = target_day.isoformat() if target_day else date.today().isoformat()
        lines = [
            "",
            "─" * 56,
            f"  🔧 按专家模式导出 — {day_label}",
            "─" * 56,
            f"  导出分组数: {result['total_groups']}",
            f"  文件数: {len(result['files'])}",
        ]
        for f in result["files"]:
            lines.append(f"    📄 {os.path.basename(f)}")
        lines.append("─" * 56)
        lines.append(f"  📁 导出目录: {os.path.dirname(result['files'][0]) if result['files'] else 'logs/messages/exports/'}")
        lines.append("")
        return "\n".join(lines)

    # export all / export summary — 全量导出
    if sub in ("all", "summary", "全部", "全量"):
        result = msg_logger.export_summary(day=target_day)
        day_label = target_day.isoformat() if target_day else date.today().isoformat()
        lines = [
            "",
            "─" * 56,
            f"  📦 全量导出 — {day_label}",
            "─" * 56,
            f"  👤 按提问人: {result['sender']['total_senders']} 人, {len(result['sender']['files'])} 个文件",
            f"  🔧 按专家:   {result['expert']['total_groups']} 个分组, {len(result['expert']['files'])} 个文件",
        ]
        if result['sender']['files']:
            lines.append("  ── 提问人分类 ──")
            for f in result['sender']['files']:
                lines.append(f"    📄 {os.path.basename(f)}")
        if result['expert']['files']:
            lines.append("  ── 专家分类 ──")
            for f in result['expert']['files']:
                lines.append(f"    📄 {os.path.basename(f)}")
        base_dir = os.path.join("logs/messages/exports", day_label)
        lines.append("─" * 56)
        lines.append(f"  📁 导出目录: {os.path.abspath(base_dir)}")
        lines.append("")
        return "\n".join(lines)

    # 无子命令 — 提示
    return (
        "\n  📋 export 命令用法:\n"
        "    export user [日期]     — 按提问人分类导出\n"
        "    export expert [日期]   — 按专家模式分类导出\n"
        "    export all [日期]      — 全量导出\n"
        "    export list            — 列出可导出日志日期\n"
        "\n  💡 日期格式: YYYY-MM-DD（如 2026-06-03），省略则为今天\n"
    )


# ── 控制台命令监听（独立线程） ──

_console_input_buffer = ""


def _resolve_config_path(config_path=None) -> Path:
    if config_path:
        return Path(config_path).expanduser().resolve()
    from utils.config import DEFAULT_CONFIG_PATH
    return Path(DEFAULT_CONFIG_PATH).resolve()


def _mask_secret(value: str) -> str:
    if not value:
        return "（未设置）"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-3:]}"


def _update_yaml_section_config(config_path, section: str, field: str, value):
    """仅修改指定 YAML 顶级段，保留其他配置及注释。"""
    path = _resolve_config_path(config_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    section_start = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(?:#.*)?$", line.rstrip("\r\n"))), None)
    if section_start is None:
        raise ValueError(f"配置文件中缺少 {section}: 配置段")
    section_end = len(lines)
    for i in range(section_start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
            section_end = i
            break
    if value is True:
        rendered = "true"
    elif value is False:
        rendered = "false"
    elif isinstance(value, (int, float)):
        rendered = str(value)
    else:
        rendered = json.dumps(str(value), ensure_ascii=False)
    field_re = re.compile(rf"^(\s+)({re.escape(field)}):.*$")
    for i in range(section_start + 1, section_end):
        match = field_re.match(lines[i].rstrip("\r\n"))
        if match:
            newline = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = f"{match.group(1)}{field}: {rendered}{newline}"
            break
    else:
        lines.insert(section_end, f"  {field}: {rendered}\n")
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text("".join(lines), encoding="utf-8")
    os.replace(temp_path, path)


def _update_llm_config(config_path, field: str, value):
    """仅修改 YAML 的 llm 段，保留其他配置及注释。"""
    return _update_yaml_section_config(config_path, "llm", field, value)


def _handle_api_config(cmd: str, config_path=None) -> str:
    import yaml
    path = _resolve_config_path(config_path)
    parts = cmd.strip().split(maxsplit=2)
    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() in ("show", "查看")):
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        llm = config.get("llm", {})
        return (
            "\n────────────────────────────────────────────────────────\n"
            "  自定义 LLM API 配置（OpenAI 兼容）\n"
            "────────────────────────────────────────────────────────\n"
            f"  状态: {'已启用' if llm.get('enabled') else '未启用'}\n"
            f"  Base URL: {llm.get('base_url') or '（未设置）'}\n"
            f"  接口路径: {llm.get('chat_completions_path', '/v1/chat/completions')}\n"
            f"  模型: {llm.get('model') or '（未设置）'}\n"
            f"  API Key: {_mask_secret(str(llm.get('api_key') or ''))}\n"
            f"  配置文件: {path}\n"
            "  提示: 保存成功后请重启 EXE 使配置生效。\n"
        )
    sub = parts[1].lower()
    if sub in ("on", "off", "启用", "停用"):
        _update_llm_config(path, "enabled", sub in ("on", "启用"))
        return "\n  ✅ API 状态已保存，请重启 EXE 使配置生效。\n"
    fields = {"url": "base_url", "path": "chat_completions_path", "model": "model", "key": "api_key"}
    if sub not in fields or len(parts) < 3 or not parts[2].strip():
        return "\n  用法: api-config [on|off|url <地址>|path <路径>|model <模型>|key <密钥>]\n"
    value = parts[2].strip()
    if sub == "path" and not value.startswith("/"):
        value = "/" + value
    _update_llm_config(path, fields[sub], value)
    label = "API Key" if sub == "key" else fields[sub]
    return f"\n  ✅ {label} 已保存，请重启 EXE 使配置生效。\n"

def _read_command_nonblocking():
    """在独立线程中使用系统原生命令行输入，确保字符正常回显。"""
    try:
        line = input("\nbot> ")
        return line.strip() or None
    except EOFError:
        return None
    except KeyboardInterrupt:
        return "quit"


def console_listener(config_path=None):
    """
    控制台命令监听线程。
    本函数本身运行在独立线程，input() 只阻塞监听线程，不影响机器人收消息。
    系统原生输入提供可见回显、退格、中文输入法和粘贴支持。
    """
    _cmd_aliases = {
        "status": "dashboard", "面板": "dashboard", "仪表盘": "dashboard",
        "info": "dashboard", "状态": "dashboard",
        "api": "dashboard", "调用": "dashboard",
        "version": "version", "版本": "version", "ver": "version", "v": "version",
        "help": "help", "帮助": "help", "h": "help", "?": "help",
        "quit": "quit", "exit": "quit", "q": "quit", "退出": "quit",
        "ticket": "ticket_help", "工单": "ticket_help",
        "ticket stats": "ticket_stats", "工单统计": "ticket_stats",
        "ticket list": "ticket_list", "工单列表": "ticket_list",
    }
    # 导出类命令需要保留原始参数，不通过别名映射
    EXPORT_PREFIXES = ("export", "导出")
    API_CONFIG_PREFIXES = ("api-config", "api config", "api配置", "自定义api")

    while True:
        cmd = _read_command_nonblocking()
        if cmd is None:
            time.sleep(0.2)
            continue
        if not cmd:
            continue

        # 导出命令 — 通过前缀匹配，保留子命令参数
        cmd_lower = cmd.lower().strip()
        if cmd_lower.startswith(API_CONFIG_PREFIXES):
            try:
                print(_handle_api_config(cmd, config_path), flush=True)
            except Exception as exc:
                print(f"\n  ❌ API 配置失败: {exc}\n", flush=True)
            continue
        if cmd_lower.startswith(EXPORT_PREFIXES):
            print(_handle_export(cmd_lower), flush=True)
            continue

        action = _cmd_aliases.get(cmd_lower)
        if action is None:
            print(f"\n  ❓ 未知命令: {cmd}")
            print(f"  💡 输入 help 查看可用命令\n", flush=True)
            continue

        if action == "quit":
            print("\n  🦊 正在关闭机器人...", flush=True)
            time.sleep(0.5)
            os._exit(0)

        elif action == "dashboard":
            print(show_dashboard(), flush=True)

        elif action == "version":
            print(f"\n  🦊 当前版本: {BOT_VERSION}\n", flush=True)

        elif action == "help":
            print(show_help(), flush=True)

        elif action == "ticket_help":
            print(_ticket_console_help(), flush=True)

        elif action == "ticket_stats":
            print(_ticket_console_stats(), flush=True)

        elif action == "ticket_list":
            print(_ticket_console_list(), flush=True)
