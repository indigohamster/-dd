"""Daily operations dashboard aggregation."""

import re
from collections import Counter
from datetime import datetime


class DailyOpsDashboard:
    def __init__(self, ticket_manager, knowledge_store):
        self.tickets = ticket_manager
        self.knowledge = knowledge_store

    @staticmethod
    def _parse(value):
        try:
            return datetime.strptime(value or "", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _signature(ticket):
        title = re.sub(r"[\W_\d]+", "", ticket.get("title", "").lower())
        return f"{ticket.get('category', '其他')}|{title[:24]}"

    def build(self, date_str=None):
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        all_tickets = self.tickets.list_all_tickets("all", limit=500)
        today = [t for t in all_tickets if str(t.get("created_at", "")).startswith(date_str)]
        resolved_today = [t for t in all_tickets if str(t.get("resolved_at", "")).startswith(date_str)]
        pending = [t for t in all_tickets if t.get("status") in ("open", "assigned", "in_progress")]
        overdue = self.tickets.get_overdue_tickets()
        urgent = [t for t in pending if t.get("priority") in ("urgent", "high")]

        durations = []
        for ticket in resolved_today:
            start, end = self._parse(ticket.get("created_at")), self._parse(ticket.get("resolved_at"))
            if start and end and end >= start:
                durations.append((end - start).total_seconds() / 3600)

        counts = Counter(self._signature(t) for t in today)
        repeated = []
        for signature, count in counts.most_common():
            if count < 2:
                continue
            sample = next(t for t in today if self._signature(t) == signature)
            repeated.append({"title": sample.get("title"), "category": sample.get("category"), "count": count})

        category_counts = Counter(t.get("category") or "其他" for t in today)
        unresolved = self.knowledge.unresolved(limit=20)
        low_sources = [m for m in self.knowledge.source_metrics(limit=50) if m["fail_count"] > m["success_count"]]
        return {
            "date": date_str,
            "metrics": {
                "created": len(today), "resolved": len(resolved_today), "pending": len(pending),
                "overdue": len(overdue), "urgent": len(urgent),
                "avg_resolution_hours": round(sum(durations) / len(durations), 1) if durations else 0,
                "knowledge_gaps": len(unresolved), "low_knowledge_sources": len(low_sources),
            },
            "overdue_tickets": overdue[:10],
            "urgent_tickets": urgent[:10],
            "repeated_issues": repeated[:10],
            "top_categories": [{"category": key, "count": value} for key, value in category_counts.most_common(8)],
            "knowledge_gaps": unresolved[:10],
            "low_knowledge_sources": low_sources[:10],
        }

