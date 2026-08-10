"""
IMA OpenAPI 客户端 - 知识库搜索
文档: https://ima.qq.com/agent-interface
"""

import copy
import io
import re
import requests
import threading
import time
import zipfile
from xml.etree import ElementTree
from typing import Optional

from utils.logger import get_logger

logger = get_logger("ima-client")

# IMA OpenAPI 基础地址
IMA_BASE_URL = "https://ima.qq.com"

# 默认超时（秒）
DEFAULT_TIMEOUT = 15
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_EXCERPT_CHARS = 12000
MEDIA_CACHE_TTL_SECONDS = 600
SEARCH_CACHE_TTL_SECONDS = 120
RATE_LIMIT_COOLDOWN_SECONDS = 60
MAX_SEARCH_CACHE_ENTRIES = 128
RATE_LIMIT_MARKERS = (
    "\u8bf7\u6c42\u9891\u7387\u8d85\u9650",
    "\u9891\u7387\u8d85\u9650",
    "rate limit",
    "too many requests",
)


class IMAClient:
    """IMA OpenAPI 客户端，支持知识库搜索和列表查询"""

    def __init__(self, client_id: str, api_key: str, base_url: str = IMA_BASE_URL):
        self.client_id = client_id
        self.api_key = api_key
        self.base_url = base_url
        self.last_error = ""
        self.last_code = None
        self.last_request_ok = False
        self.last_search_ok = False
        self.last_content_code = None
        self.last_content_error = ""
        self._media_cache = {}
        self._media_cache_lock = threading.RLock()
        self._search_cache = {}
        self._search_cache_lock = threading.RLock()
        self._rate_limit_lock = threading.RLock()
        self._rate_limited_until = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "ima-openapi-clientid": self.client_id,
            "ima-openapi-apikey": self.api_key,
            "Content-Type": "application/json",
        })

    def _rate_limit_remaining(self) -> int:
        with self._rate_limit_lock:
            remaining = self._rate_limited_until - time.monotonic()
        return max(0, int(remaining + 0.999))

    def _activate_rate_limit_cooldown(self) -> None:
        with self._rate_limit_lock:
            self._rate_limited_until = max(
                self._rate_limited_until,
                time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS,
            )

    @staticmethod
    def _is_rate_limit_response(response, data: dict) -> bool:
        if getattr(response, "status_code", None) == 429:
            return True
        message = str(data.get("msg") or data.get("message") or "").casefold()
        return any(marker in message for marker in RATE_LIMIT_MARKERS)

    def _post(self, path: str, body: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """发送 POST 请求到 IMA API"""
        cooldown = self._rate_limit_remaining()
        if cooldown:
            message = f"IMA \u8bf7\u6c42\u6b63\u5728\u9650\u6d41\u51b7\u5374\u4e2d\uff0c\u8bf7 {cooldown} \u79d2\u540e\u91cd\u8bd5"
            self.last_request_ok = False
            self.last_error = message
            logger.info("IMA cooldown active: path=%s remaining=%ss", path, cooldown)
            return {"code": -2, "msg": message, "cooldown_seconds": cooldown}

        url = f"{self.base_url}/{path}"
        start_time = time.time()
        try:
            resp = self._session.post(url, json=body, timeout=timeout)
            data = resp.json()
            success = data.get("code") == 0
            if not success and self._is_rate_limit_response(resp, data):
                self._activate_rate_limit_cooldown()
            self.last_code = data.get("code")
            self.last_request_ok = success
            self.last_error = "" if success else str(data.get("msg") or "IMA API 返回错误")
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("IMA 知识库", path, success, duration)
            except Exception:
                pass
            if not success:
                logger.warning(f"IMA API error: {data.get('msg')} (path={path})")
            return data
        except requests.exceptions.Timeout:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("IMA 知识库", path, False, duration)
            except Exception:
                pass
            logger.error(f"IMA API timeout: {path}")
            self.last_code = -1
            self.last_request_ok = False
            self.last_error = "IMA API 请求超时"
            return {"code": -1, "msg": "IMA API 请求超时"}
        except Exception as e:
            duration = time.time() - start_time
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("IMA 知识库", path, False, duration)
            except Exception:
                pass
            logger.error(f"IMA API request failed: {e}")
            self.last_code = -1
            self.last_request_ok = False
            self.last_error = f"IMA API 请求失败: {e}"
            return {"code": -1, "msg": f"IMA API 请求失败: {e}"}

    # ── 知识库列表 ──

    def list_knowledge_bases(self, query: str = "", limit: int = 20) -> list:
        """
        搜索知识库列表
        :param query: 搜索关键词，空字符串返回所有
        :param limit: 最大返回数量（API 限制 1-20）
        :return: 知识库列表 [{"title": "...", "kb_id": "..."}]
        """
        data = self._post("openapi/wiki/v1/search_knowledge_base", {
            "query": query,
            "cursor": "",
            "limit": limit,
        })
        if data.get("code") == 0:
            return data.get("data", {}).get("info_list", [])
        return []

    def get_knowledge_base_info(self, kb_id: str) -> Optional[dict]:
        """获取知识库详细信息"""
        data = self._post("openapi/wiki/v1/get_knowledge_base", {
            "ids": [kb_id],
        })
        if data.get("code") == 0:
            kb_list = data.get("data", {}).get("knowledge_base_list", [])
            return kb_list[0] if kb_list else None
        return None

    # ── 知识库搜索 ──

    def search_knowledge(self, query: str, knowledge_base_id: str, cursor: str = "",
                         hydrate: bool = True) -> list:
        """
        在指定知识库中搜索
        :param query: 搜索关键词
        :param knowledge_base_id: 知识库 ID
        :param cursor: 分页游标，首次传空字符串
        :return: 搜索结果列表
        """
        data = self._post("openapi/wiki/v1/search_knowledge", {
            "query": query,
            "knowledge_base_id": knowledge_base_id,
            "cursor": cursor,
        })
        self.last_search_ok = data.get("code") == 0
        self.last_content_code = None
        self.last_content_error = ""
        if self.last_search_ok:
            results = data.get("data", {}).get("info_list", [])
            if hydrate:
                self._hydrate_search_results(results)
            # 正文读取是搜索后的独立步骤。读取失败不能把已成功的搜索误报为失败。
            self.last_request_ok = True
            self.last_error = ""
            return results
        return []

    def search_knowledge_bases(self, query: str, kb_list: list) -> list:
        """
        在指定的知识库列表中搜索，返回汇总结果
        :param query: 搜索关键词
        :param kb_list: 知识库列表 [{"id": "kb_id", "name": "知识库名"}]
        :return: [{"title": "...", "source_kb": "...", "source_kb_id": "...", "media_id": "...", "media_type": 0}]
        """
        if not kb_list:
            return []

        normalized_query = " ".join(str(query or "").split()).casefold()
        cache_key = (
            normalized_query,
            tuple(str(kb.get("id") or "").strip() for kb in kb_list),
        )
        now = time.monotonic()
        with self._search_cache_lock:
            cached = self._search_cache.get(cache_key)
            if cached and now - cached[0] < SEARCH_CACHE_TTL_SECONDS:
                results, content_code, content_error = copy.deepcopy(cached[1:])
                self.last_search_ok = True
                self.last_request_ok = True
                self.last_error = ""
                self.last_content_code = content_code
                self.last_content_error = content_error
                logger.info("IMA search cache hit: knowledge_bases=%s", len(kb_list))
                return results
            if cached:
                self._search_cache.pop(cache_key, None)

        all_results = []
        for kb in kb_list:
            kb_id = kb.get("id", "")
            kb_name = kb.get("name", "未知知识库")
            if not kb_id:
                continue

            results = self.search_knowledge(query, kb_id, hydrate=False)
            for item in results:
                item["source_kb"] = kb_name
                item["source_kb_id"] = kb_id
                all_results.append(item)
            # 同一轮使用相同凭证；首次请求失败后继续遍历只会制造大量
            # 重复请求和日志，下一条用户消息仍会重新尝试。
            if not self.last_request_ok:
                break

        search_ok = self.last_request_ok
        search_error = self.last_error
        self.last_content_code = None
        self.last_content_error = ""
        self._hydrate_search_results(all_results, limit=5)
        self.last_search_ok = search_ok
        self.last_request_ok = search_ok
        self.last_error = search_error
        if search_ok and not self._rate_limit_remaining():
            with self._search_cache_lock:
                if len(self._search_cache) >= MAX_SEARCH_CACHE_ENTRIES:
                    oldest = min(self._search_cache, key=lambda key: self._search_cache[key][0])
                    self._search_cache.pop(oldest, None)
                self._search_cache[cache_key] = (
                    now,
                    copy.deepcopy(all_results),
                    self.last_content_code,
                    self.last_content_error,
                )
        return all_results

    def search_all_knowledge_bases(self, query: str) -> list:
        """
        在所有知识库中搜索（较慢，会遍历全部知识库）
        :param query: 搜索关键词
        :return: 搜索结果列表
        """
        kb_list = self.list_knowledge_bases(query=query)
        if not kb_list:
            return []

        # API 返回的字段是 kb_id / kb_name
        kb_map = [{"id": kb.get("kb_id", ""), "name": kb.get("kb_name", "未知知识库")} for kb in kb_list]
        return self.search_knowledge_bases(query, kb_map)

    # ── 获取媒体内容 ──

    def get_media_info(self, media_id: str) -> Optional[dict]:
        """获取媒体/文件信息"""
        data = self._post("openapi/wiki/v1/get_media_info", {
            "media_id": media_id,
        })
        if data.get("code") == 0:
            return data.get("data", {})
        return None

    @staticmethod
    def _extract_docx_text(payload: bytes) -> str:
        """Extract plain text from a DOCX package without adding a runtime dependency."""
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                document = archive.read("word/document.xml")
            root = ElementTree.fromstring(document)
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
            return ""

        parts = []
        for element in root.iter():
            tag = str(element.tag)
            if tag.endswith("}t") and element.text:
                parts.append(element.text)
            elif tag.endswith("}tab"):
                parts.append("\t")
            elif tag.endswith("}br") or tag.endswith("}p"):
                parts.append("\n")
        return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()

    @staticmethod
    def _clean_downloaded_text(text: str, content_type: str) -> str:
        if "html" in content_type:
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _download_media_text(self, media_info: dict) -> tuple[str, str]:
        url_info = media_info.get("url_info") if isinstance(media_info, dict) else None
        if not isinstance(url_info, dict):
            return "", "IMA 未返回原文件读取地址"
        url = str(url_info.get("url") or "").strip()
        if not url.lower().startswith("https://"):
            return "", "IMA 返回的内容不是可下载文件"
        headers = url_info.get("headers") if isinstance(url_info.get("headers"), dict) else {}
        try:
            response = requests.get(url, headers=headers, timeout=20, stream=True)
            response.raise_for_status()
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > MAX_MEDIA_BYTES:
                return "", f"原文件超过 {MAX_MEDIA_BYTES // 1024 // 1024}MB 读取上限"
            chunks, total = [], 0
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    return "", f"原文件超过 {MAX_MEDIA_BYTES // 1024 // 1024}MB 读取上限"
                chunks.append(chunk)
            payload = b"".join(chunks)
            content_type = str(response.headers.get("Content-Type") or "").lower()
        except requests.RequestException as exc:
            return "", f"原文件下载失败：{type(exc).__name__}"
        finally:
            if "response" in locals():
                response.close()

        if "wordprocessingml.document" in content_type:
            text = self._extract_docx_text(payload)
        elif content_type.startswith("text/") or any(
            marker in content_type for marker in ("json", "xml", "markdown")
        ):
            text = self._clean_downloaded_text(
                payload.decode(response.encoding or "utf-8", errors="replace"),
                content_type,
            )
        else:
            return "", f"原文件格式暂不支持正文提取（{content_type or '未知格式'}）"
        if len(text) < 30:
            return "", "原文件可读取，但没有足够的文本正文"
        return text[:MAX_EXCERPT_CHARS], ""

    def _hydrate_search_results(self, results: list, limit: int = 5) -> None:
        """Fill title-only search hits from IMA's media-info download flow."""
        candidates = [item for item in (results or []) if isinstance(item, dict)]
        # 文本、Markdown 和 DOCX 可直接形成 AI 上下文，优先读取这些类型；
        # 避免大量不可读取的订阅/链接条目占满本轮下载预算。
        preferred_types = {3, 7, 13}
        candidates.sort(
            key=lambda item: 0 if item.get("media_type") in preferred_types else 1
        )
        for item in candidates[:max(1, int(limit))]:
            if not isinstance(item, dict) or self._usable_excerpt(item):
                continue
            media_id = str(item.get("media_id") or "").strip()
            if not media_id:
                self.last_content_error = "IMA可连接，但正文不可读取：搜索结果没有 media_id"
                continue

            now = time.time()
            with self._media_cache_lock:
                cached = self._media_cache.get(media_id)
            if cached and now - cached[0] < MEDIA_CACHE_TTL_SECONDS:
                text, error, code = cached[1:]
            else:
                media_info = self.get_media_info(media_id)
                code = self.last_code
                if media_info:
                    text, error = self._download_media_text(media_info)
                else:
                    text = ""
                    error = self.last_error or "IMA 未返回可读取的原文件"
                with self._media_cache_lock:
                    if len(self._media_cache) >= 256:
                        oldest = min(self._media_cache, key=lambda key: self._media_cache[key][0])
                        self._media_cache.pop(oldest, None)
                    self._media_cache[media_id] = (now, text, error, code)

            if text:
                item["content"] = text
                item["content_source"] = "ima_media"
            else:
                self.last_content_code = code
                media_type = item.get("media_type", "unknown")
                self.last_content_error = (
                    f"IMA可连接，但正文不可读取：{error}（media_type={media_type}）"
                )

    def test_connection(self) -> bool:
        """测试 API 连接是否正常"""
        try:
            self.list_knowledge_bases(limit=1)
            return self.last_request_ok
        except Exception:
            return False

    @staticmethod
    def _usable_excerpt(item: dict) -> str:
        """Return content that the current AI answer path can actually consume."""
        if not isinstance(item, dict):
            return ""
        values = []
        for key in ("highlight", "highlight_content", "description", "content"):
            value = re.sub(r"<[^>]+>", " ", str(item.get(key) or ""))
            value = re.sub(r"\s+", " ", value).strip()
            if value:
                values.append(value)
        return max(values, key=len, default="")

    def test_health(self, query: str = "", kb_list: Optional[list] = None) -> dict:
        """Check connection, retrieval, and AI-usable content separately."""
        health = {
            "connected": False,
            "search_ok": False,
            "content_available": False,
            "result_count": 0,
            "usable_count": 0,
            "configured_count": len(kb_list or []),
            "valid_configured_count": 0,
            "detail": "",
        }
        try:
            knowledge_bases = self.list_knowledge_bases(limit=20)
            health["connected"] = bool(self.last_request_ok)
            if not health["connected"]:
                health["detail"] = self.last_error or "IMA 连接失败"
                return health
            if not knowledge_bases:
                health["detail"] = "连接正常，但账号下没有可检索的知识库"
                return health

            probe = str(query or "").strip()
            if not probe:
                first_name = str(knowledge_bases[0].get("kb_name") or "").strip()
                probe = first_name[:24]
            if not probe:
                health["detail"] = "连接正常，但缺少健康检查关键词"
                return health

            listed_ids = {str(item.get("kb_id") or "") for item in knowledge_bases}
            if kb_list:
                kb_map = [
                    {"id": item.get("id", ""), "name": item.get("name", "未知知识库")}
                    for item in kb_list
                    if str(item.get("id") or "") in listed_ids
                ]
                health["valid_configured_count"] = len(kb_map)
                if not kb_map:
                    health["detail"] = "IMA可连接，但当前默认知识库 ID 均不可用，请重新选择知识库"
                    return health
            else:
                kb_map = [
                    {"id": item.get("kb_id", ""), "name": item.get("kb_name", "未知知识库")}
                    for item in knowledge_bases
                ]
                health["valid_configured_count"] = len(kb_map)
            results = self.search_knowledge_bases(probe, kb_map)
            health["search_ok"] = bool(self.last_request_ok)
            health["result_count"] = len(results)
            usable = [item for item in results if len(self._usable_excerpt(item)) >= 30]
            health["usable_count"] = len(usable)
            health["content_available"] = bool(usable)
            if not health["search_ok"]:
                health["detail"] = self.last_error or "知识检索失败"
            elif not results:
                health["detail"] = f"连接正常，但健康检查关键词“{probe}”没有检索结果"
            elif not usable:
                health["detail"] = self.last_content_error or (
                    f"IMA可连接，但正文不可读取：检索到 {len(results)} 条结果，均无 AI 可用正文"
                )
            else:
                health["detail"] = f"检索到 {len(results)} 条结果，其中 {len(usable)} 条正文可用"
                if self.last_content_error:
                    health["detail"] += f"；部分条目不可读取：{self.last_content_error}"
            return health
        except Exception as exc:
            health["detail"] = str(exc)[:200] or "IMA 健康检查异常"
            return health
