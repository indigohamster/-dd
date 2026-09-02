"""Security controls for employee-facing DingTalk interactions."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping


_CREDENTIAL_RE = re.compile(
    r"(?i)(?P<label>密码|口令|password|passwd|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|authorization)\s*[:=：]\s*"
    r"(?P<value>[^\s,，;；]{4,})"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SK_RE = re.compile(r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{8,}")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_IP_RE = re.compile(
    r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"
)

_PROMPT_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"忽略(?:之前|以上|所有|系统).{0,12}(?:指令|规则|提示词)",
        r"(?:显示|输出|泄露|告诉我).{0,12}(?:系统提示词|system prompt|开发者指令|内部指令)",
        r"(?:显示|输出|泄露|列出).{0,12}(?:全部|所有).{0,8}(?:密钥|token|密码|知识库内容)",
        r"(?:你现在是|切换为|进入).{0,10}(?:无约束|开发者|系统|越狱).{0,8}(?:模式|身份)",
        r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions?",
        r"(?:reveal|print|show)\s+(?:the\s+)?(?:system\s+prompt|developer\s+message|secrets?)",
    )
)


def sensitive_types(value: object) -> set[str]:
    """Return categories of sensitive data found in *value*."""
    text = str(value or "")
    found = set()
    for name, pattern in (
        ("credential", _CREDENTIAL_RE), ("credential", _BEARER_RE),
        ("credential", _SK_RE), ("email", _EMAIL_RE), ("phone", _PHONE_RE),
        ("id_card", _ID_CARD_RE), ("ip_address", _IP_RE),
    ):
        if pattern.search(text):
            found.add(name)
    return found


def redact_sensitive_text(value: object) -> str:
    """Redact credentials and common employee identifiers without storing originals."""
    text = str(value or "")
    text = _CREDENTIAL_RE.sub(lambda m: f"{m.group('label')}：[已脱敏]", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SK_RE.sub("***", text)
    text = _EMAIL_RE.sub("[邮箱已脱敏]", text)
    text = _PHONE_RE.sub("[手机号已脱敏]", text)
    text = _ID_CARD_RE.sub("[身份证号已脱敏]", text)
    return _IP_RE.sub(
        lambda match: match.group(0) if match.group(0) == "127.0.0.1" else "[IP已脱敏]",
        text,
    )


def pseudonymous_actor_id(value: object) -> str:
    """Produce a stable, non-reversible identifier suitable for logs."""
    raw = str(value or "").strip()
    if not raw:
        return "anonymous"
    return "user-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def looks_like_prompt_injection(value: object) -> bool:
    text = str(value or "")[:4000]
    return any(pattern.search(text) for pattern in _PROMPT_INJECTION_PATTERNS)


class SlidingWindowLimiter:
    """Small in-memory per-actor limiter; failure never grants extra capacity."""

    def __init__(self, limit: int, window_seconds: int = 60, max_actors: int = 20000):
        self.limit = max(0, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self.max_actors = max(100, int(max_actors))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def allow(self, actor: str, now: float | None = None) -> tuple[bool, int]:
        if self.limit <= 0:
            return True, 0
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.window_seconds
        key = str(actor or "anonymous")[:256]
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry = max(1, int(self.window_seconds - (current - bucket[0])) + 1)
                return False, retry
            bucket.append(current)
            if len(self._events) > self.max_actors:
                empty = [item for item, values in self._events.items() if not values or values[-1] <= cutoff]
                for item in empty[: max(1, len(self._events) - self.max_actors)]:
                    self._events.pop(item, None)
            return True, 0


class UserSecurityPolicy:
    """Configuration-backed policy used by the DingTalk message handler."""

    PRIVATE_COMMAND_RE = re.compile(
        r"(?i)(?:我的(?:未处理)?工单|查看.*工单|工单状态|TKT-\d|网络状态|"
        r"检测网络|网络检测|公网IP|本机IP|查\s*IP|测速|网速|"
        r"(?:查|找|搜索|查询).{0,12}(?:NAS|公共文件夹|共享文件夹|文件服务器)|"
        r"(?:NAS|公共文件夹|共享文件夹|文件服务器).{0,12}(?:查|找|搜索|查询))"
    )

    def __init__(self, config: Mapping | None = None):
        cfg = dict(config or {})
        self.enabled = cfg.get("enabled", True) is not False
        self.group_privacy = cfg.get("group_privacy", True) is not False
        self.redact_input = cfg.get("redact_sensitive_input", True) is not False
        self.block_prompt_injection = cfg.get("block_prompt_injection", True) is not False
        rate = cfg.get("rate_limit", {}) if isinstance(cfg.get("rate_limit", {}), Mapping) else {}
        self.rate_enabled = rate.get("enabled", True) is not False
        self.message_limiter = SlidingWindowLimiter(rate.get("messages_per_minute", 30))
        self.ai_limiter = SlidingWindowLimiter(rate.get("ai_requests_per_minute", 10))

    @staticmethod
    def actor_key(info: Mapping) -> str:
        return str(info.get("staff_id") or info.get("sender_id") or "anonymous").strip()

    def allow_message(self, info: Mapping) -> tuple[bool, int]:
        if not self.enabled or not self.rate_enabled:
            return True, 0
        return self.message_limiter.allow(self.actor_key(info))

    def allow_ai(self, info: Mapping) -> tuple[bool, int]:
        if not self.enabled or not self.rate_enabled:
            return True, 0
        return self.ai_limiter.allow(self.actor_key(info))

    def group_requires_private(self, info: Mapping) -> bool:
        if not self.enabled or not self.group_privacy or str(info.get("conv_type")) != "2":
            return False
        if info.get("msgtype") != "text":
            return True
        content = str(info.get("content") or "")
        return bool(sensitive_types(content) or self.PRIVATE_COMMAND_RE.search(content))

    @staticmethod
    def knowledge_allowed(kb: Mapping, actor: Mapping) -> bool:
        if kb.get("enabled", True) is False:
            return False
        scope = str(kb.get("access_scope") or "public").strip().lower()
        if scope in {"", "public", "all", "company"}:
            return True
        if scope in {"admin", "admins"}:
            return bool(actor.get("is_admin"))
        staff_id = str(actor.get("staff_id") or "").strip()
        department = str(actor.get("department") or "").strip().casefold()
        roles = {str(value).strip().casefold() for value in actor.get("roles", []) if str(value).strip()}
        allowed_staff = {
            str(value).strip() for value in kb.get("allowed_staff_ids", []) if str(value).strip()
        }
        allowed_departments = {
            str(value).strip().casefold() for value in kb.get("allowed_departments", []) if str(value).strip()
        }
        allowed_roles = {
            str(value).strip().casefold() for value in kb.get("allowed_roles", []) if str(value).strip()
        }
        return bool(
            actor.get("is_admin")
            or (staff_id and staff_id in allowed_staff)
            or (department and department in allowed_departments)
            or (roles & allowed_roles)
        )
