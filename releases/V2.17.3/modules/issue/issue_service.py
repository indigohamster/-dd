"""Fail-safe application service for Issue shadow recording."""

import logging
import uuid
from datetime import datetime, timedelta

from .issue_model import IssueRecord
from .issue_store import IssueStore


class IssueService:
    """Record lifecycle observations without influencing business decisions."""

    def __init__(self, enabled=False, db_path="data/issues.db", logger=None, active_ttl_seconds=1800):
        self.enabled = bool(enabled)
        self.logger = logger or logging.getLogger(__name__)
        self.active_ttl_seconds = max(300, int(active_ttl_seconds))
        self.store = None
        if self.enabled:
            try:
                self.store = IssueStore(db_path)
            except Exception as exc:
                self.enabled = False
                self.logger.warning("Issue旁路存储初始化失败，已降级为仅运行原业务: %s", exc)

    @staticmethod
    def _issue_id(dedupe_key=""):
        if dedupe_key:
            value = uuid.uuid5(uuid.NAMESPACE_URL, f"opspilot-issue:{dedupe_key}")
        else:
            value = uuid.uuid4()
        return f"ISS-{value.hex[:16].upper()}"

    def _warn(self, action, exc):
        self.logger.warning("Issue旁路记录失败: action=%s error=%s", action, exc)

    def record_question(self, user_id, original_question, conversation_id, message_id=""):
        if not self.enabled or self.store is None:
            return None
        try:
            issue = self.store.create(IssueRecord(
                issue_id=self._issue_id(message_id),
                user_id=str(user_id or ""),
                original_question=str(original_question or "").strip(),
                conversation_id=str(conversation_id or ""),
            ))
            return issue
        except Exception as exc:
            self._warn("record_question", exc)
            return None

    def link_ticket(self, ticket_id, user_id, conversation_id, original_question="", force_new=False):
        if not self.enabled or self.store is None or not ticket_id:
            return None
        try:
            existing = self.store.find_by_ticket_id(str(ticket_id))
            if existing:
                return existing
            issue = None
            if not force_new:
                since = (datetime.now() - timedelta(seconds=self.active_ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
                issue = self.store.find_recent_unlinked(str(user_id or ""), str(conversation_id or ""), since)
            if not issue:
                issue = self.record_question(
                    user_id=user_id,
                    original_question=original_question or "工单问题",
                    conversation_id=conversation_id,
                    message_id=f"ticket:{ticket_id}",
                )
            if not issue:
                return None
            linked = self.store.link_ticket(issue["issue_id"], str(ticket_id))
            return linked
        except Exception as exc:
            self._warn("link_ticket", exc)
            return None

    def close_ticket(self, ticket_id, final_result=""):
        if not self.enabled or self.store is None or not ticket_id:
            return None
        try:
            return self.store.close_by_ticket(str(ticket_id), str(final_result or ""))
        except Exception as exc:
            self._warn("close_ticket", exc)
            return None

    def get_issue(self, issue_id):
        if not self.enabled or self.store is None:
            return None
        try:
            return self.store.get(issue_id)
        except Exception as exc:
            self._warn("get_issue", exc)
            return None

    def get_by_ticket_id(self, ticket_id):
        if not self.enabled or self.store is None or not ticket_id:
            return None
        try:
            return self.store.find_by_ticket_id(str(ticket_id))
        except Exception as exc:
            self._warn("get_by_ticket_id", exc)
            return None

    def list_issues(self, status="", linked=None, limit=500):
        if not self.enabled or self.store is None:
            return []
        return self.store.list_issues(status=status, linked=linked, limit=limit)
