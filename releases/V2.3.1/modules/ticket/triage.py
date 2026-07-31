"""面向 IT 运维工单的多轮故障问诊。"""

import re
import threading
import time


TRIAGE_QUESTIONS = {
    "设备问题": [
        "请提供设备名称/型号、所在位置，以及完整报错信息（可同时发截图）。",
        "这个问题从什么时候开始？重启设备后是否仍然存在？",
        "目前影响你一人还是多人？是否已经完全无法工作？",
    ],
    "U9问题": [
        "请提供 U9 模块、操作步骤、单据号（敏感信息可打码）和完整报错。",
        "问题从什么时候开始？此前相同操作是否成功过？",
        "影响你一人还是多人？是否阻断采购、生产、发货或财务业务？",
    ],
    "CRM问题": [
        "请提供 CRM 功能模块、操作步骤、相关单据/客户编号和完整报错。",
        "问题从什么时候开始？换浏览器或重新登录后是否仍存在？",
        "影响你一人还是多人？是否阻断客户或售后业务？",
    ],
    "软件问题": [
        "请提供软件名称、版本、操作系统和完整错误提示（可同时发截图）。",
        "问题从什么时候开始？重启软件或电脑后是否仍然存在？",
        "影响你一人还是多人？当前是否有临时替代方式？",
    ],
    "其他": [
        "请描述涉及的系统/设备、你的操作步骤和完整错误提示（可同时发截图）。",
        "问题从什么时候开始？你已经尝试过哪些处理？",
        "影响你一人还是多人？是否阻断当前业务？",
    ],
}


class TriageEngine:
    """维护短期问诊会话；完成后将结果交给工单系统持久化。"""

    def __init__(self, enabled: bool = True, min_description_length: int = 30,
                 ttl_seconds: int = 1800):
        self.enabled = enabled
        self.min_description_length = max(5, int(min_description_length))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._sessions = {}
        self._lock = threading.Lock()

    def needs_triage(self, description: str) -> bool:
        text = (description or "").strip()
        return self.enabled and "直接提交" not in text and len(text) < self.min_description_length

    def start(self, conv_id: str, description: str, category: str) -> str:
        questions = self._select_questions(description, category)
        # 一次最多询问两个关键问题，用户可在一条消息中完整补充。
        combined = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
        questions = [combined]
        with self._lock:
            self._sessions[conv_id] = {
                "description": description.strip(), "category": category,
                "questions": questions, "answers": [], "index": 0,
                "timestamp": time.time(),
            }
        return self._format_question(questions[0], 1, len(questions))

    @staticmethod
    def _select_questions(description: str, category: str) -> list:
        text = (description or "").strip()
        candidates = TRIAGE_QUESTIONS.get(category, TRIAGE_QUESTIONS["其他"])
        selected = []
        has_detail = len(text) >= 18 or bool(re.search(r"报错|错误|提示|型号|版本|单据|模块", text))
        has_attempt = any(word in text for word in ("重启", "重新登录", "试过", "更换", "清理", "重新连接"))
        has_impact = any(word in text for word in ("一人", "多人", "所有人", "部门", "业务", "生产", "发货", "财务"))
        if not has_detail:
            selected.append(candidates[0])
        if not has_attempt:
            selected.append(candidates[1])
        if not has_impact:
            selected.append(candidates[2])
        return (selected or ["请补充当前仍未解决的现象和完整报错。"])[0:2]

    def has_session(self, conv_id: str) -> bool:
        return self._get_session(conv_id) is not None

    def consume(self, conv_id: str, answer: str) -> dict:
        text = (answer or "").strip()
        with self._lock:
            session = self._sessions.get(conv_id)
            if not session or time.time() - session["timestamp"] > self.ttl_seconds:
                self._sessions.pop(conv_id, None)
                return {"status": "expired"}
            if text in ("取消", "取消报修", "取消工单"):
                self._sessions.pop(conv_id, None)
                return {"status": "cancelled"}
            session["answers"].append(text)
            session["index"] += 1
            session["timestamp"] = time.time()
            if session["index"] < len(session["questions"]):
                index = session["index"]
                return {"status": "question", "message": self._format_question(
                    session["questions"][index], index + 1, len(session["questions"]))}
            self._sessions.pop(conv_id, None)

        details = [session["description"]]
        for question, value in zip(session["questions"], session["answers"]):
            details.append(f"问诊：{question}\n回答：{value}")
        return {"status": "completed", "description": "\n\n".join(details),
                "category": session["category"]}

    def _get_session(self, conv_id: str):
        with self._lock:
            session = self._sessions.get(conv_id)
            if session and time.time() - session["timestamp"] <= self.ttl_seconds:
                return session
            self._sessions.pop(conv_id, None)
            return None

    @staticmethod
    def _format_question(question: str, current: int, total: int) -> str:
        return (f"🩺 为减少来回沟通，请一次补充以下关键信息：\n\n"
                f"{question}\n\n回复「取消报修」可结束；首次描述加入「直接提交」可跳过问诊。")
