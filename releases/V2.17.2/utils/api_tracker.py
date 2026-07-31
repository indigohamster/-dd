# ============================================================
# API 调用追踪器 - 记录各 API 的调用情况
# ============================================================
#
# 为控制台仪表盘提供数据支撑，记录每次 API 调用的：
#   - API 名称、端点、成功/失败、耗时、时间
#   - 汇总统计：总次数、成功率、平均耗时
# ============================================================

import threading
import time
from datetime import datetime
from collections import defaultdict


class APITracker:
    """线程安全的 API 调用追踪器"""

    def __init__(self):
        self._lock = threading.Lock()
        self._calls = []  # 调用记录列表
        self._MAX_CALLS = 1000  # 防止内存泄漏上限
        self._stats = defaultdict(lambda: {
            "total": 0,
            "success": 0,
            "fail": 0,
            "last_time": "-",
            "total_duration": 0.0,
        })

    def record(self, api_name: str, endpoint: str, success: bool, duration: float = 0):
        """
        记录一次 API 调用。

        :param api_name: API 名称（如 "IMA 知识库"、"DeepSeek LLM"、"钉钉 OpenAPI"）
        :param endpoint: API 端点路径（如 "openapi/wiki/v1/search_knowledge"）
        :param success: 是否成功
        :param duration: 调用耗时（秒）
        """
        with self._lock:
            now = datetime.now().strftime("%H:%M:%S")
            self._calls.append({
                "api": api_name,
                "endpoint": endpoint,
                "success": success,
                "duration": round(duration, 3),
                "time": now,
            })
            # 超过上限时淘汰最早的记录
            if len(self._calls) > self._MAX_CALLS:
                self._calls = self._calls[-self._MAX_CALLS:]
            s = self._stats[api_name]
            s["total"] += 1
            if success:
                s["success"] += 1
            else:
                s["fail"] += 1
            s["last_time"] = now
            s["total_duration"] += duration

    def get_stats(self) -> dict:
        """获取 API 调用汇总统计"""
        with self._lock:
            return {k: dict(v) for k, v in self._stats.items()}

    def get_recent(self, limit: int = 10) -> list:
        """获取最近 N 条调用记录"""
        with self._lock:
            return list(self._calls[-limit:])

    def get_total(self) -> int:
        """获取总调用次数"""
        with self._lock:
            return len(self._calls)

    def reset(self):
        """重置所有统计数据"""
        with self._lock:
            self._calls.clear()
            self._stats.clear()


# 全局单例
api_tracker = APITracker()
