"""Minimal Issue data model used by the V2.17 shadow recorder."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


ISSUE_STATUS_OPEN = "open"
ISSUE_STATUS_TICKETED = "ticketed"
ISSUE_STATUS_CLOSED = "closed"


def now_text() -> str:
    """Return a timestamp in the format used by the existing SQLite stores."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class IssueRecord:
    issue_id: str
    user_id: str
    original_question: str
    conversation_id: str
    status: str = ISSUE_STATUS_OPEN
    ticket_id: str = ""
    steps_json: list[dict[str, Any]] = field(default_factory=list)
    final_result: str = ""
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

