"""Strict schema for the model-produced problem understanding result."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


INTENTS = frozenset({
    "capability_consultation",
    "knowledge_consultation",
    "incident_help",
    "supplement",
    "confirmed_resolved",
    "reported_unresolved",
    "human_handoff",
    "ticket_operation",
    "unknown",
})

INFORMATION_COMPLETENESS = frozenset({
    "complete", "partial", "insufficient", "unknown",
})

SUGGESTED_ACTIONS = frozenset({
    "show_capabilities",
    "retrieve_and_answer",
    "retrieve_then_clarify",
    "ask_clarification",
    "start_troubleshooting",
    "continue_current_state",
    "handoff",
    "defer_to_ticket_parser",
    "legacy_fallback",
})

ACTIVE_STATE_TYPES = frozenset({"none", "ticket_triage", "problem_resolution"})
ACTIVE_STATE_RELATIONS = frozenset({
    "new_problem",
    "supplement",
    "answer_to_pending_question",
    "feedback",
    "uncertain",
})

_ROOT_FIELDS = frozenset({
    "schema_version",
    "intent",
    "topic",
    "knowledge_domains",
    "candidate_kb_ids",
    "is_continuation",
    "information_completeness",
    "missing_information",
    "suggested_action",
    "confidence",
    "active_state_attribution",
})
_STATE_FIELDS = frozenset({"state_type", "relation", "confidence"})


class ProblemUnderstandingValidationError(ValueError):
    """Raised when a model response violates the understanding schema."""


def _require_exact_fields(value: Mapping[str, Any], required: frozenset[str], label: str):
    actual = frozenset(value.keys())
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise ProblemUnderstandingValidationError(
            f"{label}字段不匹配: missing={missing}, extra={extra}"
        )


def _enum(value: Any, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProblemUnderstandingValidationError(f"{field}枚举值无效")
    return value


def _bounded_text(value: Any, field: str, limit: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ProblemUnderstandingValidationError(f"{field}必须是字符串")
    normalized = " ".join(value.split())
    if not allow_empty and not normalized:
        raise ProblemUnderstandingValidationError(f"{field}不能为空")
    if len(normalized) > limit:
        raise ProblemUnderstandingValidationError(f"{field}长度超过限制")
    return normalized


def _bounded_list(value: Any, field: str, *, max_items: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProblemUnderstandingValidationError(f"{field}必须是数组")
    if len(value) > max_items:
        raise ProblemUnderstandingValidationError(f"{field}数量超过限制")
    result = []
    for item in value:
        normalized = _bounded_text(item, field, item_limit, allow_empty=False)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProblemUnderstandingValidationError(f"{field}必须是数字")
    normalized = float(value)
    if normalized < 0 or normalized > 1:
        raise ProblemUnderstandingValidationError(f"{field}必须在0到1之间")
    return normalized


@dataclass(frozen=True)
class ActiveStateAttribution:
    state_type: str
    relation: str
    confidence: float


@dataclass(frozen=True)
class ProblemUnderstandingResult:
    schema_version: str
    intent: str
    topic: str
    knowledge_domains: tuple[str, ...]
    candidate_kb_ids: tuple[str, ...]
    is_continuation: bool
    information_completeness: str
    missing_information: tuple[str, ...]
    suggested_action: str
    confidence: float
    active_state_attribution: ActiveStateAttribution

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        allowed_kb_ids: set[str] | frozenset[str],
        max_candidate_kbs: int = 3,
    ):
        if not isinstance(payload, Mapping):
            raise ProblemUnderstandingValidationError("根对象必须是JSON对象")
        _require_exact_fields(payload, _ROOT_FIELDS, "根对象")
        if payload.get("schema_version") != "1.0":
            raise ProblemUnderstandingValidationError("schema_version必须为1.0")
        if not isinstance(payload.get("is_continuation"), bool):
            raise ProblemUnderstandingValidationError("is_continuation必须是布尔值")

        domains = _bounded_list(
            payload.get("knowledge_domains"), "knowledge_domains",
            max_items=5, item_limit=80,
        )
        candidates = _bounded_list(
            payload.get("candidate_kb_ids"), "candidate_kb_ids",
            max_items=max(1, int(max_candidate_kbs)), item_limit=512,
        )
        unknown_ids = sorted(set(candidates) - set(allowed_kb_ids))
        if unknown_ids:
            raise ProblemUnderstandingValidationError("candidate_kb_ids包含未配置知识库")

        state = payload.get("active_state_attribution")
        if not isinstance(state, Mapping):
            raise ProblemUnderstandingValidationError("active_state_attribution必须是对象")
        _require_exact_fields(state, _STATE_FIELDS, "active_state_attribution")

        return cls(
            schema_version="1.0",
            intent=_enum(payload.get("intent"), INTENTS, "intent"),
            topic=_bounded_text(payload.get("topic"), "topic", 160),
            knowledge_domains=domains,
            candidate_kb_ids=candidates,
            is_continuation=payload["is_continuation"],
            information_completeness=_enum(
                payload.get("information_completeness"),
                INFORMATION_COMPLETENESS,
                "information_completeness",
            ),
            missing_information=_bounded_list(
                payload.get("missing_information"), "missing_information",
                max_items=3, item_limit=160,
            ),
            suggested_action=_enum(
                payload.get("suggested_action"), SUGGESTED_ACTIONS,
                "suggested_action",
            ),
            confidence=_confidence(payload.get("confidence"), "confidence"),
            active_state_attribution=ActiveStateAttribution(
                state_type=_enum(
                    state.get("state_type"), ACTIVE_STATE_TYPES,
                    "active_state_attribution.state_type",
                ),
                relation=_enum(
                    state.get("relation"), ACTIVE_STATE_RELATIONS,
                    "active_state_attribution.relation",
                ),
                confidence=_confidence(
                    state.get("confidence"),
                    "active_state_attribution.confidence",
                ),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
