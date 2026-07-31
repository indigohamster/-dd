"""Deterministic natural-language references to a user's ticket list."""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class TicketReferenceResult:
    """Result of resolving a parsed ticket reference against candidates."""

    ticket_id: Optional[str] = None
    intent: Optional[str] = None
    candidates: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.ticket_id)


class TicketReferenceResolver:
    """Resolve only the small, explicit reference grammar supported by V2.17.1-A."""

    @staticmethod
    def get_reference(parsed):
        extra = getattr(parsed, "extra", None)
        if not isinstance(extra, dict):
            return None
        reference = extra.get("ticket_reference")
        return reference if isinstance(reference, dict) else None

    @classmethod
    def has_reference(cls, parsed) -> bool:
        return bool(cls.get_reference(parsed))

    @classmethod
    def resolve(cls, parsed, tickets, context=None) -> TicketReferenceResult:
        reference = cls.get_reference(parsed)
        if not reference:
            return TicketReferenceResult(reason="no_reference")

        tickets = list(tickets or [])
        candidate_ids = tuple(
            ticket.get("ticket_id") for ticket in tickets if ticket.get("ticket_id")
        )
        intent = (context or {}).get("pending_intent") or parsed.intent
        reference_type = reference.get("type")

        if reference_type == "recent":
            if not tickets:
                return TicketReferenceResult(
                    intent=intent, candidates=candidate_ids, reason="no_candidates",
                )
            return TicketReferenceResult(
                ticket_id=tickets[0]["ticket_id"], intent=intent,
                candidates=candidate_ids,
            )

        if reference_type == "ordinal":
            try:
                position = int(reference.get("value", 0))
            except (TypeError, ValueError):
                position = 0
            if 1 <= position <= len(tickets):
                return TicketReferenceResult(
                    ticket_id=tickets[position - 1]["ticket_id"], intent=intent,
                    candidates=candidate_ids,
                )
            return TicketReferenceResult(
                intent=intent, candidates=candidate_ids, reason="out_of_range",
            )

        return TicketReferenceResult(
            intent=intent, candidates=candidate_ids, reason="unsupported_reference",
        )
