"""
CodeBuddy Code HTTP API 客户端
通过本地 CodeBuddy Code HTTP 服务（codebuddy --serve）调用 AI 对话能力

API 文档: https://www.codebuddy.ai/docs/cli/http-api
接口格式:
  - POST /api/v1/runs            → 发起 Agent 执行（异步，返回 runId，状态码 202）
  - GET  /api/v1/runs/:id/stream  → SSE 流式获取执行结果
  - GET  /api/v1/health           → 健康检查
  - POST /api/v1/daemon/start     → 启动 Agent Worker
  - GET  /api/v1/daemon/status    → 查询 Agent Worker 状态

认证: Bearer Token（Authorization: Bearer ck_xxx）
必需请求头: X-CodeBuddy-Request: 1

注意: CodeBuddy HTTP 服务包含两层:
  1. HTTP Gateway（codebuddy --serve）— 处理 API 请求
  2. Agent Worker (daemon) — 实际执行 AI 任务，需额外启动
"""

import json
import re
import time
import uuid
import requests
from typing import Optional

from utils.logger import get_logger

logger = get_logger("codebuddy-client")

# 默认配置
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_TIMEOUT = 30    # 总超时（秒），含 stream + 轮询
MAX_ACTIVE_WAIT = 20    # run 处于 active 状态的最大等待时间（秒）


class CodeBuddyClient:
    """CodeBuddy Code 本地 HTTP API 客户端"""

    def __init__(
        self,
        password: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.password = password
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self._session = requests.Session()
        self._session.headers.update({
            "X-CodeBuddy-Request": "1",
            "Authorization": f"Bearer {self.password}",
            "Content-Type": "application/json",
        })

    def test_connection(self) -> bool:
        """
        测试 CodeBuddy HTTP 服务是否可用
        同时检查 HTTP Gateway 和 Agent Worker (daemon) 状态
        """
        try:
            # 1. 健康检查
            url = f"{self.base_url}/api/v1/health"
            resp = self._session.get(url, timeout=5)
            if resp.status_code != 200:
                logger.warning(f"CodeBuddy 健康检查返回非200: {resp.status_code}")
                return False

            # 2. 检查并拉起 daemon
            daemon_ok = self._ensure_daemon()
            if not daemon_ok:
                logger.warning("CodeBuddy Agent Worker (daemon) 未运行，AI 功能可能不可用")

            return True

        except requests.exceptions.ConnectionError:
            logger.warning("CodeBuddy HTTP 服务未启动（连接被拒绝），请确认已运行 codebuddy --serve")
            return False
        except Exception as e:
            logger.error(f"CodeBuddy 连接测试失败: {e}")
            return False

    def _ensure_daemon(self) -> bool:
        """
        确保 Agent Worker (daemon) 正在运行
        如未运行则尝试拉起，返回是否成功
        每次 chat() 前调用，防止 daemon 中途挂掉
        """
        try:
            d_resp = self._session.get(
                f"{self.base_url}/api/v1/daemon/status", timeout=3
            )
            if d_resp.status_code == 200:
                d_data = d_resp.json().get("data", {})
                d_status = d_data.get("status", "unknown")
                if d_status == "running":
                    logger.debug("CodeBuddy daemon 状态正常 (running)")
                    return True
                else:
                    logger.warning(f"CodeBuddy daemon 状态: {d_status}，尝试启动...")
            else:
                logger.warning(f"CodeBuddy daemon/status 返回 {d_resp.status_code}，尝试启动...")
        except Exception as de:
            logger.warning(f"CodeBuddy daemon 状态检查失败: {de}，尝试启动...")

        # 尝试启动 daemon（Windows 冷启动需要 15~30 秒，timeout 设宽松）
        try:
            start_resp = self._session.post(
                f"{self.base_url}/api/v1/daemon/start", timeout=35
            )
            if start_resp.status_code in (200, 202):
                logger.info("CodeBuddy daemon 启动成功")
                # 确保使用自定义 DeepSeek 模型（防止 daemon 重启后丢失设置）
                self._ensure_custom_model()
                return True
            else:
                logger.warning(f"CodeBuddy daemon 启动失败: {start_resp.status_code} {start_resp.text[:200]}")
        except Exception as se:
            _se_str = str(se).lower()
            if "read timed out" in _se_str or "read timeout" in _se_str:
                # 冷启动超时属正常，daemon 仍在后台初始化，不算失败
                logger.info(f"CodeBuddy daemon 冷启动超时（属正常），将在首次 chat 时重新检测: {se}")
                return True  # 乐观返回，让后续 run 创建来真实检测
            logger.warning(f"CodeBuddy daemon 启动异常: {se}")

        return False

    def _ensure_custom_model(self):
        """
        确保 CodeBuddy 使用自定义 DeepSeek 模型
        防止 daemon 重启后 model 设置被重置回内置模型（走云端 API → 429）
        """
        try:
            model_resp = self._session.get(
                f"{self.base_url}/api/v1/settings/model", timeout=3
            )
            if model_resp.status_code == 200:
                current_model = model_resp.json().get("data", {}).get("value", "")
                if current_model == "custom-local:deepseek-chat":
                    return  # 已经是正确的模型
                logger.info(f"CodeBuddy 当前模型: {current_model}，切换为自定义 DeepSeek...")
        except Exception:
            pass

        try:
            set_resp = self._session.put(
                f"{self.base_url}/api/v1/settings/model",
                json={"value": "custom-local:deepseek-chat"},
                timeout=5,
            )
            if set_resp.status_code in (200, 204):
                logger.info("CodeBuddy 模型已切换为 custom-local:deepseek-chat")
            else:
                logger.warning(f"CodeBuddy 模型设置失败: {set_resp.status_code}")
        except Exception as me:
            logger.warning(f"CodeBuddy 模型设置异常: {me}")

    def chat(self, messages: list, system_prompt: str = None, timeout: int = None) -> str:
        """
        发送对话请求到 CodeBuddy Agent

        流程:
        1. 将 messages 格式化为 prompt 文本
        2. POST /api/v1/runs 发起执行
        3. 轮询 run 状态 + 读取 SSE stream 获取结果
        4. 解析并返回回复文本

        :param messages: 消息列表 [{"role": "system/user/assistant", "content": "..."}]
        :param system_prompt: 可选的系统提示词
        :param timeout: 超时时间（秒）
        :return: CodeBuddy Agent 回复文本
        """
        timeout = timeout or self.timeout

        prompt = self._format_prompt(messages, system_prompt)
        logger.info(f"CodeBuddy chat: prompt 长度={len(prompt)} 字符")

        # 确保 daemon 活着（防止中途挂掉导致 stream 无数据）
        daemon_ok = self._ensure_daemon()
        if not daemon_ok:
            logger.warning("CodeBuddy daemon 无法启动，返回空（由调用方 fallback 到 DeepSeek）")

        start_time = time.time()

        try:
            # 步骤1: 创建 run
            run_id = self._create_run(prompt)
            if not run_id:
                return None  # 返回空，让调用方 fallback 到 DeepSeek

            logger.info(f"CodeBuddy run 已创建: {run_id[:16]}...")

            # 步骤2: 获取结果（SSE stream + 状态轮询）
            answer = self._get_run_result(run_id, timeout)

            duration = time.time() - start_time

            # 记录 API 调用
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("CodeBuddy AI", "/api/v1/runs + stream", bool(answer), duration)
            except Exception:
                pass

            if answer:
                logger.info(f"CodeBuddy 回复成功 (耗时 {duration:.1f}s, 长度={len(answer)} 字符)")
                return answer
            else:
                return self._error_reply("CodeBuddy 未返回有效回复")

        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("CodeBuddy AI", "/api/v1/runs + stream", False, duration)
            except Exception:
                pass
            logger.warning(f"CodeBuddy 请求超时 ({timeout}s)，返回空（由调用方 fallback）")
            return None  # 返回空，让调用方 fallback 到 DeepSeek
        except Exception as e:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("CodeBuddy AI", "/api/v1/runs + stream", False, duration)
            except Exception:
                pass
            logger.error(f"CodeBuddy 调用失败: {e}")
            return None  # 返回空，让调用方 fallback 到 DeepSeek

    def _format_prompt(self, messages: list, system_prompt: str = None) -> str:
        """将 messages 列表格式化为 CodeBuddy 能理解的 prompt 文本"""
        parts = []

        if system_prompt:
            parts.append(f"[系统指令]\n{system_prompt}")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"[系统指令]\n{content}")
            elif role == "user":
                parts.append(f"[用户]\n{content}")
            elif role == "assistant":
                parts.append(f"[助手]\n{content}")

        return "\n\n".join(parts)

    def _create_run(self, prompt: str) -> Optional[str]:
        """
        POST /api/v1/runs 创建 Agent 执行

        优先使用 OpenAPI 文档格式，400 时回退到 generic message 格式

        :return: runId 或 None
        """
        url = f"{self.base_url}/api/v1/runs"
        msg_id = str(uuid.uuid4())[:8]

        # 格式1: OpenAPI 文档格式 {"text": ..., "sender": {"id": ..., "name": ...}}
        body_openapi = {
            "text": prompt,
            "sender": {"id": "dingtalk-bot", "name": "钉钉运维助手"},
        }

        # 格式2: Generic webhook message 格式 {"id": ..., "type": "text", "text": ...}
        body_generic = {
            "id": msg_id,
            "type": "text",
            "text": prompt,
        }

        # 优先使用 Generic message 格式（实测稳定可用）
        run_id = self._try_create_run(url, body_generic, "Generic")
        if run_id:
            return run_id

        # 回退到 OpenAPI 文档格式（保留兼容）
        logger.info("Generic 格式失败，尝试 OpenAPI 格式...")
        run_id = self._try_create_run(url, body_openapi, "OpenAPI")
        return run_id

    def _try_create_run(self, url: str, body: dict, fmt_name: str) -> Optional[str]:
        """尝试用指定格式创建 run"""
        try:
            resp = self._session.post(url, json=body, timeout=10)

            # 接受 200 (旧版) 和 202 (OpenAPI 规范)
            if resp.status_code in (200, 202):
                data = resp.json()
                run_id = data.get("data", {}).get("runId")
                if run_id:
                    logger.info(f"CodeBuddy run 创建成功 ({fmt_name}格式): {run_id[:16]}...")
                    return run_id
                else:
                    logger.error(f"CodeBuddy run 响应无 runId ({fmt_name}): {data}")
                return None

            if resp.status_code == 400:
                logger.warning(
                    f"CodeBuddy run 400 ({fmt_name}): {resp.text[:500]}\n"
                    f"  请求体: {json.dumps(body, ensure_ascii=False)[:200]}"
                )
                return None

            if resp.status_code == 429:
                logger.warning("CodeBuddy API 限流，返回空（由调用方 fallback）")
                return None

            logger.error(f"CodeBuddy run 创建失败 ({fmt_name}): {resp.status_code} {resp.text[:200]}")
            return None

        except requests.exceptions.ConnectionError:
            logger.error("CodeBuddy HTTP 服务连接失败")
            return None
        except Exception as e:
            logger.error(f"CodeBuddy run 创建异常 ({fmt_name}): {e}")
            return None

    def _get_run_result(self, run_id: str, timeout: int) -> str:
        """
        获取 run 结果: 同时尝试 SSE stream 和状态轮询

        策略:
        1. 先尝试 SSE stream 读取（实时获取 Agent 输出）
        2. 同时轮询 run 状态，等待 completed/error
        3. 如果 stream 无数据但 run 已完成，从 run 详情提取结果
        """
        stream_parts = []
        run_status = "active"
        deadline = time.time() + timeout
        active_deadline = time.time() + MAX_ACTIVE_WAIT  # active 状态最大等待时间
        poll_interval = 2  # 秒

        try:
            # 尝试 SSE stream
            stream_url = f"{self.base_url}/api/v1/runs/{run_id}/stream"
            try:
                resp = self._session.get(
                    stream_url,
                    stream=True,
                    timeout=min(timeout, 10),
                    headers={"Accept": "text/event-stream"},
                )
                if resp.status_code == 200:
                    # 强制 UTF-8 解码，防止 CodeBuddy 返回错误 charset 导致 Latin-1 误读
                    resp.encoding = "utf-8"
                    for line in resp.iter_lines(decode_unicode=True):
                        if time.time() > deadline:
                            logger.warning("CodeBuddy stream 读取超时")
                            break

                        if not line:
                            continue

                        text = self._parse_sse_line(line)
                        if text:
                            stream_parts.append(text)

                        # 检查完成标记
                        if self._is_completion_line(line):
                            break
            except requests.exceptions.ReadTimeout:
                logger.debug("CodeBuddy stream 读取超时，切换到轮询模式")
            except Exception as e:
                logger.debug(f"CodeBuddy stream 异常: {e}，切换到轮询模式")

            # 如果 stream 没拿到数据，轮询 run 状态
            if not stream_parts:
                logger.info("Stream 无数据，开始轮询 run 状态...")
                status_url = f"{self.base_url}/api/v1/runs/{run_id}"
                poll_start = time.time()
                last_log = 0
                while time.time() < deadline:
                    now = time.time()
                    time.sleep(poll_interval)

                    # active 状态超过 MAX_ACTIVE_WAIT 秒，主动放弃
                    if run_status == "active" and now > active_deadline:
                        logger.warning(
                            f"run 处于 active 状态超过 {MAX_ACTIVE_WAIT}s，主动放弃"
                            f"（可能模型调用失败或限流）"
                        )
                        break

                    # 每 5 秒打一次进度日志
                    if now - last_log >= 5:
                        elapsed = int(now - poll_start)
                        logger.info(f"  轮询中... 已等待 {elapsed}s | run={run_id[:12]}... | status={run_status}")
                        last_log = now

                    try:
                        r = self._session.get(status_url, timeout=5)
                        if r.status_code == 200:
                            data = r.json().get("data", {})
                            run_status = data.get("status", "unknown")
                            logger.debug(f"  run status: {run_status}")

                            if run_status == "completed":
                                # 尝试从 run 数据提取结果（使用 _extract_content 安全处理各种格式）
                                raw_result = (
                                    data.get("result")
                                    or data.get("output")
                                    or data.get("text", "")
                                )
                                if raw_result:
                                    extracted = self._extract_content(raw_result)
                                    if extracted:
                                        stream_parts = [extracted]
                                    else:
                                        # _extract_content 返回空但 raw_result 有值 → 降级保留原文
                                        stream_parts = [str(raw_result)]
                                else:
                                    # 结果可能在 traces 或 messages 里
                                    traces = data.get("traces") or []
                                    if traces:
                                        stream_parts = [
                                            t.get("content", "")
                                            for t in traces
                                            if t.get("content")
                                        ]

                                # 额外尝试: 如果 data 本身包含 version+content，也从 data 提取
                                if not "".join(stream_parts).strip() and "content" in data:
                                    extracted = self._extract_content(data)
                                    if extracted:
                                        stream_parts = [extracted]

                                break
                            elif run_status in ("error", "cancelled"):
                                error_msg = data.get("error", "unknown error")
                                logger.error(f"CodeBuddy run {run_status}: {error_msg}")
                                break
                    except Exception:
                        pass

            return "".join(stream_parts).strip()

        except Exception as e:
            logger.error(f"CodeBuddy 结果获取异常: {e}")
            return ""

    def _extract_content(self, raw) -> str:
        """
        从 CodeBuddy 响应的各种格式中提取文本内容。
        支持:
          - 纯文本字符串
          - {"version":"1.0","status":"completed","content":{"markdown":"..."}}  (CodeBuddy 直接响应)
          - {"content": {"markdown": "..."}}  (简化版)
          - {"content": "..."}  (纯文本 content)
        :return: 提取的文本内容，解析失败返回空字符串
        """
        # 已经是纯文本
        if isinstance(raw, str):
            stripped = raw.strip()
            # 可能是 JSON 字符串，尝试解析
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    return self._extract_content(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            return self._fix_mojibake(raw)
        elif isinstance(raw, dict):
            # 格式: {"version": ..., "status": ..., "content": {...}}
            content = raw.get("content")
            if content is not None:
                if isinstance(content, dict):
                    # {"content": {"markdown": "...", "text": "..."}}
                    text = content.get("markdown") or content.get("text") or ""
                elif isinstance(content, str):
                    text = content
                else:
                    text = ""
                return self._fix_mojibake(text)
            # 格式: {"markdown": "..."} 或 {"text": "..."}
            text = raw.get("markdown") or raw.get("text") or ""
            return self._fix_mojibake(text)
        elif isinstance(raw, list):
            # ["text1", "text2"] → 拼接
            return self._fix_mojibake("".join(str(item) for item in raw if item))
        return self._fix_mojibake(str(raw)) if raw else ""

    def _fix_mojibake(self, text: str) -> str:
        """
        修复 UTF-8 双重编码（Latin-1 误读为 Unicode）导致的乱码。
        典型症状: "é£\u008eå" → 实际应为中文 "风..."
        原理: UTF-8 字节 → 被误当作 Latin-1 字符 → JSON 反序列化出乱码字符
        """
        if not text or not isinstance(text, str):
            return text

        # 快速检测: 如果文本看起来正常（高比例中文/ASCII），跳过
        normal_chars = sum(1 for c in text if c.isascii() or '\u4e00' <= c <= '\u9fff')
        if normal_chars / max(len(text), 1) > 0.5:
            return text

        # 尝试 Latin-1 → UTF-8 反向修复
        try:
            decoded = text.encode("latin-1").decode("utf-8")
            # 验证修复后的文本更"正常"（中文比例更高）
            orig_normal = sum(1 for c in text if c.isascii() or '\u4e00' <= c <= '\u9fff')
            fixed_normal = sum(1 for c in decoded if c.isascii() or '\u4e00' <= c <= '\u9fff')
            if fixed_normal > orig_normal:
                logger.info(f"🔧 Mojibake 修复: {len(text)} → {len(decoded)} 字符, 正常比例 {orig_normal/len(text):.0%} → {fixed_normal/len(decoded):.0%}")
                return decoded
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

        return text

    def _parse_sse_line(self, line: str) -> str:
        """
        解析 SSE 行，提取文本内容
        支持格式:
          - data: {"data": {"text": "..."}}  (OpenAPI 信封)
          - data: {"choices": [{"delta": {"content": "..."}}]}  (OpenAI 兼容)
          - data: {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "..."}}  (ACP 风格)
          - data: {"text": "..."}}  (直接文本)
          - data: 纯文本
          - data: {"version": "1.0", "content": {"markdown": "..."}}  (CodeBuddy 直接响应)
          - {"version": "1.0", "content": {...}}  (裸 JSON，无 SSE 前缀)
        """
        # 处理裸 JSON 响应（无 "data: " SSE 前缀）
        raw_line = line.strip()
        if raw_line and raw_line.startswith("{") and not line.startswith("data: "):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                return raw_line + "\n"

            text = self._extract_content(event)
            if text:
                return text + "\n"

            # 尝试从 choices 提取
            choices = event.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {}) or choices[0].get("message", {})
                text = delta.get("content") or delta.get("text", "")
                if text:
                    return text

            return ""

        if not line.startswith("data: "):
            return ""

        json_str = line[6:]  # 去掉 "data: " 前缀

        # 结束标记
        if json_str.strip() in ("[DONE]", ""):
            return ""

        try:
            event = json.loads(json_str)
        except json.JSONDecodeError:
            # 非 JSON，可能就是纯文本
            if json_str.strip():
                return json_str.strip() + "\n"
            return ""

        # 格式1: 信封格式 {"data": {"text": ...}}
        data = event.get("data", {})
        if isinstance(data, dict):
            text = data.get("text") or data.get("content") or data.get("message") or ""
            if text:
                # data.content 可能是 JSON 字符串 → 用 _extract_content 安全提取
                if isinstance(text, str) and (text.strip().startswith("{") or text.strip().startswith("[")):
                    extracted = self._extract_content(text)
                    if extracted and extracted != text:
                        return extracted + "\n"
                return text + "\n"

        # 格式2: ACP 风格 {"sessionUpdate": "agent_message_chunk", "content": {...}}
        if "sessionUpdate" in event:
            content = event.get("content", {})
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text", "")
                if text:
                    return text

        # 格式3: OpenAI 兼容 {"choices": [{"delta": {"content": "..."}}]}
        choices = event.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            text = delta.get("content") or delta.get("text", "")
            if text:
                return text

        # 格式4: 直接 text 字段
        text = event.get("text", "")
        if text:
            return text + "\n"

        # 格式5: CodeBuddy 直接响应 {"version": "1.0", "status": "completed", "content": {...}}
        if "version" in event and "content" in event:
            text = self._extract_content(event)
            if text:
                return text + "\n"

        return ""

    def _is_completion_line(self, line: str) -> bool:
        """判断 SSE 行是否表示完成"""
        # 支持 SSE 格式 (data: ...) 和裸 JSON
        json_str = None
        if line.startswith("data: "):
            json_str = line[6:]
            if json_str.strip() in ("[DONE]", ""):
                return True
        else:
            raw = line.strip()
            if raw.startswith("{"):
                json_str = raw

        if not json_str:
            return False

        try:
            event = json.loads(json_str)
            # 优先级1: 信封内的 status
            data = event.get("data", {})
            if isinstance(data, dict):
                status = data.get("status") or data.get("runStatus", "")
                if status in ("completed", "error", "cancelled"):
                    return True
            # 优先级2: 顶层的 status（CodeBuddy 直接响应）
            status = event.get("status") or event.get("runStatus", "")
            if status in ("completed", "error", "cancelled"):
                return True
        except json.JSONDecodeError:
            pass

        return False

    def _error_reply(self, reason: str) -> str:
        """返回友好的错误提示"""
        return f"CodeBuddy AI 出了点问题: {reason} (´・ω・`)"
