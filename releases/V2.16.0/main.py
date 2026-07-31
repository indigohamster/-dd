#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# OpsPilot 企业运维助手 - 主入口
# 版本: V2.16.0
# ============================================================
#
# 启动方式:
#   python main.py                         # 使用默认 config.yaml
#   python main.py --config /path/to.yaml  # 指定配置文件
#   python main.py --debug                 # 开启 DEBUG 日志
#
# 环境变量（覆盖配置文件）:
#   DINGTALK_CLIENT_ID       应用 Client ID
#   DINGTALK_CLIENT_SECRET   应用 Client Secret
#
# 控制台命令（运行时输入）:
#   status   - 显示完整仪表盘
#   api      - 显示 API 调用详情
#   version  - 显示版本号
#   help     - 显示帮助
#   quit     - 关闭机器人
# ============================================================

import argparse
import asyncio
import io
import os
import signal
import subprocess
import sys
import threading
import time

# Windows 控制台 GBK 编码无法输出 emoji，强制设为 UTF-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import dingtalk_stream

from utils.config import is_runtime_configured, load_config
from utils.logger import setup_logger
from utils.console import (
    bot_status,
    show_startup_banner,
    show_connection_test,
    show_dashboard,
    console_listener,
    BOT_VERSION,
)
from handlers.chat_handler import BotMessageHandler
from handlers.event_handler import BotEventHandler
from utils.scheduled_tasks import (
    OverdueTicketChecker, WeeklyReportScheduler,
    TicketFollowupScheduler, DailyOpsSummaryScheduler,
)
from utils.admin_server import start_admin_server


def parse_args():
    parser = argparse.ArgumentParser(description="OpsPilot 企业运维助手")
    parser.add_argument(
        "--config", "-c",
        dest="config_path",
        default=None,
        help="配置文件路径 (默认: ./config.yaml)",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="开启 DEBUG 级别日志",
    )
    return parser.parse_args()


def test_api_connections(config: dict, logger):
    """
    启动时检测各 API 连接状态。
    返回连接检测结果字典。
    """
    results = {}
    ima_cfg = config.get("ima", {})
    llm_cfg = config.get("llm", {})
    creds = config.get("credentials", {})

    # 1. 测试 IMA 知识库
    if ima_cfg.get("enabled", False):
        bot_status.update(ima_enabled=True)
        try:
            from utils.ima_client import IMAClient
            client = IMAClient(ima_cfg["client_id"], ima_cfg["api_key"])
            start = time.time()
            kb_list = client.list_knowledge_bases(limit=1)
            dur = time.time() - start
            ok = kb_list is not None
            results["IMA 知识库"] = (ok, dur)
            if ok:
                bot_status.update(ima_connected=True)
                logger.info(f"IMA 知识库连接测试成功 ({dur:.2f}s)")
            else:
                logger.warning("IMA 知识库连接测试失败")
        except Exception as e:
            results["IMA 知识库"] = (False, 0)
            logger.warning(f"IMA 知识库连接测试异常: {e}")
    else:
        results["IMA 知识库"] = (None, 0)

    # 2. 测试 DeepSeek LLM
    if llm_cfg.get("enabled", False):
        bot_status.update(llm_enabled=True)
        try:
            from utils.deepseek_client import DeepSeekClient
            ds = DeepSeekClient(
                api_key=llm_cfg["api_key"],
                model=llm_cfg.get("model", "deepseek-chat"),
                base_url=llm_cfg.get("base_url", "https://api.deepseek.com"),
                chat_completions_path=llm_cfg.get("chat_completions_path", "/v1/chat/completions"),
            )
            start = time.time()
            reply = ds.chat([{"role": "user", "content": "hi"}], max_tokens=5)
            dur = time.time() - start
            ok = len(reply) > 0
            results["DeepSeek LLM"] = (ok, dur)
            if ok:
                bot_status.update(llm_connected=True)
                logger.info(f"DeepSeek LLM 连接测试成功 ({dur:.2f}s)")
            else:
                logger.warning("DeepSeek LLM 连接测试失败")
        except Exception as e:
            results["DeepSeek LLM"] = (False, 0)
            logger.warning(f"DeepSeek LLM 连接测试异常: {e}")
    else:
        results["DeepSeek LLM"] = (None, 0)

    # 2.5 测试 CodeBuddy AI（本地 HTTP API）
    cb_cfg = config.get("codebuddy", {})
    if cb_cfg.get("enabled", False):
        bot_status.update(cb_enabled=True)
        try:
            from utils.codebuddy_client import CodeBuddyClient
            cb = CodeBuddyClient(
                password=cb_cfg["password"],
                host=cb_cfg.get("host", "127.0.0.1"),
                port=cb_cfg.get("port", 8080),
            )
            start = time.time()
            ok = cb.test_connection()
            dur = time.time() - start
            results["CodeBuddy AI"] = (ok, dur)
            if ok:
                bot_status.update(cb_connected=True)
                logger.info(f"CodeBuddy AI 连接测试成功 ({dur:.2f}s)")
            else:
                logger.warning("CodeBuddy AI 连接测试失败（HTTP 服务可能未启动）")
        except Exception as e:
            results["CodeBuddy AI"] = (False, 0)
            logger.warning(f"CodeBuddy AI 连接测试异常: {e}")
    else:
        results["CodeBuddy AI"] = (None, 0)

    # 3. 测试钉钉 OpenAPI
    if creds.get("client_id") and creds.get("client_secret"):
        bot_status.update(dtalk_enabled=True)
        try:
            from utils.dingtalk_api import DingTalkAPI
            dtalk = DingTalkAPI(
                client_id=creds["client_id"],
                client_secret=creds["client_secret"],
                agent_id=str(creds.get("agent_id", "")),
            )
            start = time.time()
            token = dtalk.get_access_token()
            dur = time.time() - start
            ok = bool(token)
            results["钉钉 OpenAPI"] = (ok, dur)
            if ok:
                bot_status.update(dtalk_connected=True)
                logger.info(f"钉钉 OpenAPI 连接测试成功 ({dur:.2f}s)")
            else:
                logger.warning("钉钉 OpenAPI 连接测试失败")
        except Exception as e:
            results["钉钉 OpenAPI"] = (False, 0)
            logger.warning(f"钉钉 OpenAPI 连接测试异常: {e}")
    else:
        results["钉钉 OpenAPI"] = (None, 0)

    return results


def main():
    args = parse_args()

    # 1. 加载配置
    try:
        config = load_config(args.config_path, allow_incomplete=True)
    except (OSError, ValueError) as e:
        print(f"[ERROR] 配置加载失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. 初始化日志
    log_level = "DEBUG" if args.debug else config.get("logging", {}).get("level", "INFO")
    log_fmt = config.get("logging", {}).get(
        "format", "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    )
    log_datefmt = config.get("logging", {}).get("datefmt", "%Y-%m-%d %H:%M:%S")
    logger = setup_logger(level=log_level, fmt=log_fmt, datefmt=log_datefmt)

    # 本机可视化管理后台（无登录，仅绑定 127.0.0.1）
    admin_cfg = config.get("admin_ui", {})
    try:
        admin_port = int(admin_cfg.get("port", 8765))
        if not 1 <= admin_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        admin_port = 8765
        logger.warning("admin_ui.port 配置无效，已安全回退到 8765")
    runtime_ready = is_runtime_configured(config)
    if admin_cfg.get("enabled", True) or not runtime_ready:
        try:
            start_admin_server(
                config_path=args.config_path,
                host="127.0.0.1",
                port=admin_port,
                auto_open=bool(admin_cfg.get("auto_open", True)),
                logger=logger,
            )
        except RuntimeError as exc:
            logger.error("本机管理台启动失败: %s", exc)
            if not runtime_ready:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return

    # Missing credentials are a normal first-run state. Keep the local setup
    # console alive, then continue startup as soon as it atomically writes a
    # complete config.yaml.
    if not runtime_ready:
        logger.warning(
            "钉钉凭证尚未配置，机器人连接暂未启动；请在本机打开 http://127.0.0.1:%s 完成配置向导",
            admin_port,
        )
        print(f"[SETUP] 请打开 http://127.0.0.1:{admin_port} 完成首次配置。")
        try:
            while True:
                time.sleep(0.75)
                candidate = load_config(args.config_path, allow_incomplete=True)
                if is_runtime_configured(candidate):
                    config = candidate
                    logger.info("首次配置已完成，正在启动钉钉机器人")
                    break
        except KeyboardInterrupt:
            logger.info("首次配置等待已取消")
            return

    # 3. 显示启动横幅（含版本号、凭证、知识库配置）
    print(show_startup_banner(config))
    logger.info(f"版本: {BOT_VERSION}")

    # 4. 自动启动 CodeBuddy HTTP 服务（如果配置了且未运行）
    cb_process = None
    cb_cfg = config.get("codebuddy", {})
    if cb_cfg.get("enabled", False) and cb_cfg.get("auto_start", True):
        cb_host = cb_cfg.get("host", "127.0.0.1")
        cb_port = cb_cfg.get("port", 8080)
        cb_already_running = False

        # 检查端口是否已有人在监听
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((cb_host, cb_port))
            s.close()
            cb_already_running = True
        except (ConnectionRefusedError, OSError):
            pass

        if cb_already_running:
            logger.info(f"CodeBuddy HTTP 服务已在 {cb_host}:{cb_port} 运行，跳过启动")
        else:
            # 查找 codebuddy 可执行文件
            cb_cmd = None
            try:
                result = subprocess.run(
                    ["where", "codebuddy"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    cb_cmd = result.stdout.strip().splitlines()[0].strip()
            except Exception:
                pass

            # fallback: 常见安装路径
            if not cb_cmd:
                _cb_candidates = [
                    os.path.expanduser(r"~\.workbuddy\binaries\node\versions\22.22.2\codebuddy.cmd"),
                    os.path.expanduser(r"~\.workbuddy\binaries\node\versions\22.12.0\codebuddy.cmd"),
                    os.path.expanduser(r"~\AppData\Roaming\npm\codebuddy.cmd"),
                ]
                for _p in _cb_candidates:
                    if os.path.isfile(_p):
                        cb_cmd = _p
                        break

            if cb_cmd:
                logger.info(f"正在启动 CodeBuddy HTTP 服务 ({cb_host}:{cb_port})...")
                try:
                    cb_process = subprocess.Popen(
                        [cb_cmd, "--serve", "--port", str(cb_port)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    # 等待服务启动（最多 10 秒）
                    for i in range(20):
                        time.sleep(0.5)
                        try:
                            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            s.settimeout(1)
                            s.connect((cb_host, cb_port))
                            s.close()
                            logger.info(f"CodeBuddy HTTP 服务启动成功 (PID={cb_process.pid}, 耗时{(i+1)*0.5:.1f}s)")
                            break
                        except (ConnectionRefusedError, OSError):
                            continue
                    else:
                        logger.warning("CodeBuddy HTTP 服务启动超时（10秒内未就绪），将在连接检测中重试")

                    # 自动拉起 Agent Worker (daemon)
                    # timeout 设为 35 秒：daemon 在 Windows 上冷启动需要 15~30 秒
                    try:
                        import requests as _req
                        _daemon_resp = _req.post(
                            f"http://{cb_host}:{cb_port}/api/v1/daemon/start",
                            headers={"X-CodeBuddy-Request": "1"},
                            timeout=35,
                        )
                        if _daemon_resp.status_code in (200, 202):
                            logger.info("CodeBuddy Agent Worker (daemon) 已启动")
                        else:
                            logger.warning(f"CodeBuddy daemon 启动返回: {_daemon_resp.status_code}")
                    except Exception as _de:
                        # Read timed out 说明 daemon 还在冷启动中，属于正常现象，降级为 INFO
                        _de_str = str(_de).lower()
                        if "read timed out" in _de_str or "read timeout" in _de_str:
                            logger.info(
                                f"CodeBuddy daemon 正在启动中（冷启动超时属正常，将在首次调用时自动检测）: {_de}"
                            )
                        else:
                            logger.warning(f"CodeBuddy daemon 自动启动失败: {_de}")

                    # 自动设置模型为自定义 DeepSeek（绕过 CodeBuddy 云端 429 限流）
                    try:
                        _model_resp = _req.put(
                            f"http://{cb_host}:{cb_port}/api/v1/settings/model",
                            headers={
                                "X-CodeBuddy-Request": "1",
                                "Content-Type": "application/json",
                            },
                            json={"value": "custom-local:deepseek-chat"},
                            timeout=5,
                        )
                        if _model_resp.status_code in (200, 204):
                            logger.info("CodeBuddy 默认模型已设为 custom-local:deepseek-chat")
                        else:
                            logger.warning(f"CodeBuddy 模型设置返回: {_model_resp.status_code}")
                    except Exception as _me:
                        logger.warning(f"CodeBuddy 模型自动设置失败: {_me}")
                except Exception as e:
                    logger.warning(f"CodeBuddy HTTP 服务启动失败: {e}")
            else:
                logger.warning("未找到 codebuddy CLI，跳过自动启动（请先安装: npm install -g @tencent-ai/codebuddy-code）")
    else:
        logger.info("CodeBuddy AI 未启用或 auto_start=false，跳过自动启动")

    # 5. 检测 API 连接状态
    logger.info("正在检测 API 连接状态...")
    results = test_api_connections(config, logger)
    print(show_connection_test(results))
    print()

    # 5. 创建凭证和 Stream 客户端
    creds = config["credentials"]
    credential = dingtalk_stream.Credential(
        creds["client_id"],
        creds["client_secret"],
    )
    client = dingtalk_stream.DingTalkStreamClient(credential)

    # 6. 注册消息处理器（群聊 + 单聊）
    chat_handler = BotMessageHandler(logger, config)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        chat_handler,
    )
    logger.info("已注册聊天消息处理器 (群聊 @bot + 单聊)")

    # 7. 注册事件处理器
    if config.get("events", {}).get("enabled", True):
        event_handler = BotEventHandler(logger, config)
        client.register_all_event_handler(event_handler)
        logger.info("已注册事件处理器 (全部事件)")
    else:
        logger.info("事件处理器已禁用 (config.events.enabled = false)")

    # 8. 信号处理（优雅退出）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(sig):
        logger.info(f"收到信号 {sig.name}，正在关闭...")
        # 终止 CodeBuddy 子进程
        if cb_process and cb_process.poll() is None:
            logger.info(f"正在终止 CodeBuddy HTTP 服务 (PID={cb_process.pid})...")
            cb_process.terminate()
            try:
                cb_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cb_process.kill()
        try:
            client.close()
        except Exception:
            pass
        chat_handler.task_queue.shutdown(wait=False)
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown, sig)
        except NotImplementedError:
            pass

    # Windows 兼容的信号处理
    def win_shutdown(sig, frame):
        logger.info("收到中断信号，正在关闭...")
        # 终止 CodeBuddy 子进程
        if cb_process and cb_process.poll() is None:
            logger.info(f"正在终止 CodeBuddy HTTP 服务 (PID={cb_process.pid})...")
            cb_process.terminate()
            try:
                cb_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cb_process.kill()
        try:
            client.close()
        except Exception:
            pass
        chat_handler.task_queue.shutdown(wait=False)
        loop.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, win_shutdown)

    # 9. 启动控制台命令监听线程（daemon，主进程退出时自动结束）
    console_thread = threading.Thread(
        target=console_listener,
        args=(args.config_path,),
        name="console-listener",
        daemon=True,
    )
    console_thread.start()

    # 10. 启动工单后台定时任务。模块关闭时必须同时关闭其提醒、
    # 周报、回访和摘要，避免“界面显示已停用但后台仍在读写工单”。
    if chat_handler.ticket_enabled:
        # 10a. 逾期工单提醒（每30分钟检查一次）
        overdue_checker = OverdueTicketChecker(chat_handler, logger)
        overdue_thread = threading.Thread(
            target=overdue_checker.run,
            name="overdue-checker",
            daemon=True,
        )
        overdue_thread.start()
        logger.info("逾期工单提醒线程已启动 (每30分钟检查一次)")

        # 10b. 每周统计报告（每天12:00检查是否为周五）
        weekly_reporter = WeeklyReportScheduler(chat_handler, logger)
        report_thread = threading.Thread(
            target=weekly_reporter.run,
            name="weekly-reporter",
            daemon=True,
        )
        report_thread.start()
        logger.info("每周统计报告线程已启动 (每周五12:00发送)")

        followup_scheduler = TicketFollowupScheduler(chat_handler, logger)
        threading.Thread(target=followup_scheduler.run, name="ticket-followup", daemon=True).start()
        logger.info("工单解决效果回访线程已启动")

        daily_summary = DailyOpsSummaryScheduler(chat_handler, logger)
        threading.Thread(target=daily_summary.run, name="daily-ops-summary", daemon=True).start()
        logger.info("每日个人运维摘要线程已启动")
    else:
        logger.info("工单模块已停用，跳过全部工单提醒、周报、回访和摘要线程")

    # 10. 启动 Stream WebSocket 连接
    bot_status.update(stream_connected=True)
    logger.info("Stream WebSocket 连接启动中...")
    sep = "═" * 56
    print(f"{sep}")
    print(f"  🚀 Stream WebSocket 连接启动中... 已就绪！")
    print(f"  💡 在控制台输入 status 可随时查看仪表盘")
    print(f"{sep}")
    print("")

    try:
        client.start_forever()
    except KeyboardInterrupt:
        logger.info("用户中断，正在退出...")
    except Exception as e:
        logger.error(f"运行异常: {e}", exc_info=True)
        sys.exit(1)
    finally:
        bot_status.update(stream_connected=False)
        # 确保 CodeBuddy 子进程被清理
        if cb_process and cb_process.poll() is None:
            logger.info("正在清理 CodeBuddy HTTP 服务...")
            cb_process.terminate()
        logger.info("OpsPilot 企业运维助手已停止")


if __name__ == "__main__":
    main()
