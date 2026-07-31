"""面向员工的智能问题处置状态机。"""

import re
import threading
import time


SUCCESS_WORDS = ("好了", "恢复了", "解决了", "可以了", "正常了", "搞定", "成功了", "能用了")
FAIL_WORDS = ("不行", "没用", "没解决", "还是", "无效", "报错", "失败", "没变化", "看不到", "找不到")
ESCALATE_WORDS = (
    "提交工单", "创建工单", "新建工单", "报修", "上报问题", "提个工单", "帮我报修",
    "提交一个问题", "有问题要报", "直接报修", "转人工", "找人工", "人工处理", "直接建单",
)
CANCEL_WORDS = ("取消处理", "不用了", "先不处理", "取消排查")
NEW_TOPIC_PREFIXES = ("另外", "还有个问题", "换个问题", "新问题", "我想问", "顺便问")
CONSULTATION_WORDS = (
    "怎么", "如何", "在哪", "怎样", "步骤", "教程", "流程",
    "需要什么", "需要哪些", "需要准备", "什么材料", "哪些材料", "所需材料",
    "有什么条件", "哪些条件",
)
SERVICE_REQUEST_WORDS = (
    "开权限", "申请权限", "重置密码", "忘记密码", "解锁账号", "新建账号", "安装软件",
    "新员工", "入职账号", "入职设备", "离职回收", "停用账号", "开通邮箱",
)


class ProblemResolutionEngine:
    def __init__(self, ttl_seconds=1800, max_failures=2):
        self.ttl_seconds = max(300, int(ttl_seconds))
        self.max_failures = max(1, int(max_failures))
        self._sessions = {}
        self._lock = threading.Lock()

    @staticmethod
    def classify(text):
        value = (text or "").lower()
        if any(k in value for k in ("攻击", "中毒", "勒索", "泄露", "钓鱼", "异常登录", "木马")):
            return "security"
        is_consultation = any(k in value for k in CONSULTATION_WORDS)
        is_service_request = (("申请" in value and "权限" in value) or
                              any(k in value for k in SERVICE_REQUEST_WORDS))
        # “怎么申请权限”是在询问信息，不应直接建单；没有咨询问法的
        # “帮我申请权限”“重置密码”等仍按明确服务请求处理。
        if is_service_request and not is_consultation:
            return "service_request"
        if any(k in value for k in ("所有人", "多人", "全公司", "整个部门", "都不能", "大面积")):
            return "major_incident"
        if any(k in value for k in ("报错", "不能", "无法", "失败", "故障", "打不开", "进不去", "连不上", "卡死", "脱机", "异常", "坏了", "蓝屏", "断网")):
            return "incident"
        if is_consultation:
            return "consultation"
        return "general"

    @staticmethod
    def _facts(text):
        value = text or ""
        return {
            "has_time": bool(re.search(r"今天|昨天|刚才|早上|下午|晚上|\d+[点时分]|开始", value)),
            "has_attempt": any(k in value for k in ("重启", "重新登录", "换了", "试过", "清理", "重新连接")),
            "has_impact": any(k in value for k in ("一人", "我自己", "多人", "所有人", "部门", "业务", "生产", "发货", "财务")),
            "has_target": len(value) >= 12 or any(k in value for k in ("打印机", "电脑", "ERP", "CRM", "网络", "VPN", "服务器", "邮箱", "软件")),
        }

    def begin(self, conv_id, text):
        category = self.classify(text)
        if category == "security":
            return {"action": "escalate", "reason": "检测到安全事件关键词", "summary": text,
                    "priority": "urgent", "request_type": "incident"}
        if category == "service_request":
            return {"action": "escalate", "reason": "已识别为服务请求，将按服务目录转交处理", "summary": text,
                    "priority": "medium", "request_type": "service_request"}
        if category not in ("incident", "major_incident"):
            return {"action": "continue", "category": category}
        facts = self._facts(text)
        questions = []
        if not facts["has_target"]:
            questions.append("具体是哪个系统、设备或功能？请附上完整报错。")
        if not facts["has_attempt"]:
            questions.append("你已经尝试过哪些操作？例如重启、重新登录或更换网络。")
        if not facts["has_impact"]:
            questions.append("目前影响你一人还是多人？是否已经影响正常业务？")
        # 信息较完整时直接处理；否则一次只问最关键的两个问题。
        if len(text.strip()) >= 28 or len(questions) <= 1:
            self._create_session(conv_id, text, category)
            return {"action": "continue", "category": category}
        selected = questions[:2]
        with self._lock:
            self._sessions[conv_id] = {"phase": "clarify", "original": text, "category": category,
                                       "questions": selected, "attempts": [], "failures": 0,
                                       "timestamp": time.time()}
        return {"action": "clarify", "message": self._clarify_message(selected)}

    def _create_session(self, conv_id, text, category):
        with self._lock:
            self._sessions[conv_id] = {"phase": "solving", "original": text, "category": category,
                                       "questions": [], "attempts": [], "failures": 0,
                                       "timestamp": time.time()}

    def has_session(self, conv_id):
        return self._session(conv_id) is not None

    def consume(self, conv_id, text):
        session = self._session(conv_id)
        if not session:
            return {"action": "none"}
        value = (text or "").strip()
        lower = value.lower()
        # 仅识别用户明确放在句首的新话题提示，避免把普通补充信息误判为
        # 新问题。清理旧会话后，调用方应让原消息继续走常规路由。
        if any(value.startswith(prefix) for prefix in NEW_TOPIC_PREFIXES):
            self.clear(conv_id)
            return {"action": "new_topic"}
        if any(k in value for k in CANCEL_WORDS):
            self.clear(conv_id)
            return {"action": "cancel"}
        if any(k in value for k in ESCALATE_WORDS):
            summary = self.summary(session, value)
            self.clear(conv_id)
            return {"action": "escalate", "reason": "用户要求人工处理", "summary": summary}
        if session["phase"] == "clarify":
            session["phase"] = "solving"
            session["clarification"] = value
            session["timestamp"] = time.time()
            combined = f"{session['original']}\n用户补充：{value}"
            return {"action": "ready", "content": combined, "attempt": 1}
        if lower in ("是", "是的", "yes", "y", "ok", "有用") or any(k in value for k in SUCCESS_WORDS):
            self.clear(conv_id)
            return {"action": "resolved", "attempts": len(session["attempts"])}
        if lower in ("否", "不是", "no", "n", "没用") or any(k in value for k in FAIL_WORDS):
            session["failures"] += 1
            session["timestamp"] = time.time()
            if session["failures"] >= self.max_failures:
                summary = self.summary(session, value)
                self.clear(conv_id)
                return {"action": "escalate", "reason": "连续处理方案未解决", "summary": summary}
            previous = session["attempts"][-1]["answer"] if session["attempts"] else ""
            return {"action": "retry", "content": (
                f"原始问题：{session['original']}\n用户反馈：{value}\n"
                f"上一方案未解决，请不要重复，给出下一种安全排查方案。上一方案：{previous[:600]}"),
                "attempt": session["failures"] + 1}
        # 非标准反馈作为新线索继续排查。
        return {"action": "retry", "content": f"原始问题：{session['original']}\n用户补充信息：{value}\n请基于新信息调整排查方案。", "attempt": session["failures"] + 1}

    def record_answer(self, conv_id, question, answer):
        session = self._session(conv_id)
        if not session:
            return answer
        session["phase"] = "solving"
        session["attempts"].append({"question": question, "answer": answer})
        session["timestamp"] = time.time()
        attempt = len(session["attempts"])
        risk = "中（执行前请确认不会中断其他人的业务）" if any(k in answer for k in ("重启服务", "删除", "重置", "卸载")) else "低"
        return (f"### 🧭 处理建议 · 第 {attempt} 步\n\n"
                f"**当前判断**：正在按“{self._label(session['category'])}”处理。\n\n"
                f"{answer}\n\n---\n预计耗时：约 2–10 分钟  |  风险：{risk}\n"
                "完成后直接回复“好了”“还是不行”“看不到这个选项”或“转人工”。")

    @staticmethod
    def summary(session, latest=""):
        lines = [f"原始问题：{session.get('original', '')}"]
        if session.get("clarification"):
            lines.append(f"用户补充：{session['clarification']}")
        for index, attempt in enumerate(session.get("attempts", []), 1):
            lines.append(f"第 {index} 次排查：{attempt['answer'][:500]}")
        if latest:
            lines.append(f"最新反馈：{latest}")
        return "\n\n".join(lines)

    def clear(self, conv_id):
        with self._lock:
            self._sessions.pop(conv_id, None)

    def _session(self, conv_id):
        with self._lock:
            session = self._sessions.get(conv_id)
            if session and time.time() - session["timestamp"] <= self.ttl_seconds:
                return session
            self._sessions.pop(conv_id, None)
            return None

    @staticmethod
    def _clarify_message(questions):
        lines = ["为了直接给出有效方案，我只需要再确认："]
        lines.extend(f"{i}. {question}" for i, question in enumerate(questions, 1))
        lines.append("\n可以一次回复以上信息；也可回复“直接报修”或“转人工”。")
        return "\n".join(lines)

    @staticmethod
    def _label(category):
        return {"incident": "单用户故障", "major_incident": "多人故障",
                "security": "安全事件", "service_request": "服务请求"}.get(category, "运维问题")
