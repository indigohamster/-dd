"""
IMA OpenAPI 客户端 - 知识库搜索
文档: https://ima.qq.com/agent-interface
"""

import re
import requests
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("ima-client")

# IMA OpenAPI 基础地址
IMA_BASE_URL = "https://ima.qq.com"

# 默认超时（秒）
DEFAULT_TIMEOUT = 15


class IMAClient:
    """IMA OpenAPI 客户端，支持知识库搜索和列表查询"""

    def __init__(self, client_id: str, api_key: str, base_url: str = IMA_BASE_URL):
        self.client_id = client_id
        self.api_key = api_key
        self.base_url = base_url
        self.last_error = ""
        self.last_code = None
        self.last_request_ok = False
        self._session = requests.Session()
        self._session.headers.update({
            "ima-openapi-clientid": self.client_id,
            "ima-openapi-apikey": self.api_key,
            "Content-Type": "application/json",
        })

    def _post(self, path: str, body: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """发送 POST 请求到 IMA API"""
        url = f"{self.base_url}/{path}"
        start_time = time.time()
        try:
            resp = self._session.post(url, json=body, timeout=timeout)
            data = resp.json()
            success = data.get("code") == 0
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

    def search_knowledge(self, query: str, knowledge_base_id: str, cursor: str = "") -> list:
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
        if data.get("code") == 0:
            return data.get("data", {}).get("info_list", [])
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

        all_results = []
        for kb in kb_list:
            kb_id = kb.get("id", "")
            kb_name = kb.get("name", "未知知识库")
            if not kb_id:
                continue

            results = self.search_knowledge(query, kb_id)
            for item in results:
                item["source_kb"] = kb_name
                item["source_kb_id"] = kb_id
                all_results.append(item)
            # 同一轮使用相同凭证；首次请求失败后继续遍历只会制造大量
            # 重复请求和日志，下一条用户消息仍会重新尝试。
            if not self.last_request_ok:
                break

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

    def test_health(self, query: str = "") -> dict:
        """Check connection, retrieval, and AI-usable content separately."""
        health = {
            "connected": False,
            "search_ok": False,
            "content_available": False,
            "result_count": 0,
            "usable_count": 0,
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

            kb_map = [
                {"id": item.get("kb_id", ""), "name": item.get("kb_name", "未知知识库")}
                for item in knowledge_bases
            ]
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
                health["detail"] = f"检索到 {len(results)} 条结果，但均无 AI 可用正文"
            else:
                health["detail"] = f"检索到 {len(results)} 条结果，其中 {len(usable)} 条正文可用"
            return health
        except Exception as exc:
            health["detail"] = str(exc)[:200] or "IMA 健康检查异常"
            return health
