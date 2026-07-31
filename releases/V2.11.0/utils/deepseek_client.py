"""
DeepSeek LLM 客户端
使用 OpenAI 兼容 API 格式调用 DeepSeek 模型
API 文档: https://platform.deepseek.com/api-docs
"""

import requests
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("deepseek-client")

# 默认超时（秒）
DEFAULT_TIMEOUT = 30


class DeepSeekAPIError(Exception):
    """DeepSeek API 调用异常"""
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


class DeepSeekClient:
    """DeepSeek API 客户端（OpenAI 兼容格式）"""

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com",
                 chat_completions_path: str = "/v1/chat/completions",
                 temperature: float = 0.3, max_tokens: int = 1024,
                 timeout: int = DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.chat_completions_path = "/" + chat_completions_path.strip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def chat(self, messages: list, temperature: float = None,
             max_tokens: int = None, stream: bool = False) -> str:
        """
        发送对话请求

        :param messages: 消息列表 [{"role": "system/user/assistant", "content": "..."}]
        :param temperature: 温度参数（可选，覆盖默认值）
        :param max_tokens: 最大 token 数（可选，覆盖默认值）
        :param stream: 是否流式返回（目前不支持，固定 False）
        :return: 模型回复文本
        """
        url = f"{self.base_url}{self.chat_completions_path}"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }

        start_time = time.time()
        try:
            resp = self._session.post(url, json=body, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            duration = time.time() - start_time

            # 记录 API 调用
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("LLM API", self.chat_completions_path, True, duration)
            except Exception:
                pass

            # OpenAI 兼容格式：choices[0].message.content
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # 记录 token 用量
            usage = data.get("usage", {})
            if usage:
                logger.info(f"DeepSeek 调用: prompt_tokens={usage.get('prompt_tokens', 0)}, "
                           f"completion_tokens={usage.get('completion_tokens', 0)}")

            return content.strip()

        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("DeepSeek LLM", "/v1/chat/completions", False, duration)
            except Exception:
                pass
            logger.error("DeepSeek API 请求超时")
            raise DeepSeekAPIError("AI 回复超时，请稍后再试", status_code=504)
        except requests.exceptions.HTTPError as e:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("DeepSeek LLM", "/v1/chat/completions", False, duration)
            except Exception:
                pass
            logger.error(f"DeepSeek API HTTP 错误: {e}")
            status_code = e.response.status_code if e.response is not None else None
            raise DeepSeekAPIError(f"AI 服务错误 (HTTP {status_code})", status_code=status_code)
        except Exception as e:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("DeepSeek LLM", "/v1/chat/completions", False, duration)
            except Exception:
                pass
            logger.error(f"DeepSeek API 调用失败: {e}")
            raise DeepSeekAPIError(f"AI 调用失败: {e}")

    def test_connection(self) -> bool:
        """测试 API 连接"""
        try:
            result = self.chat([{"role": "user", "content": "hi"}], max_tokens=5)
            return len(result) > 0
        except Exception:
            return False
