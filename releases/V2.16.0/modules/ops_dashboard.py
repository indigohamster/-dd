"""Daily operations dashboard aggregation."""

import re
from collections import Counter
from datetime import datetime
from statistics import median


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

    def _all_tickets(self):
        """读取完整工单集；兼容旧版 TicketManager 和测试替身。"""
        query = getattr(self.tickets, "query_tickets", None)
        if not query:
            return self.tickets.list_all_tickets("all", limit=10000)
        items, page = [], 1
        while True:
            result = query(page=page, page_size=500, sort="created_at", order="desc")
            batch = result.get("items", [])
            items.extend(batch)
            if not batch or len(items) >= int(result.get("total", len(items))):
                return items
            page += 1

    @staticmethod
    def _percent(numerator, denominator):
        return round(numerator * 100 / denominator, 1) if denominator else 0

    @staticmethod
    def _percentile(values, percentile):
        if not values:
            return 0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
        return round(ordered[index], 1)

    def build(self, date_str=None):
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        all_tickets = self._all_tickets()
        valid_tickets = [
            t for t in all_tickets
            if not t.get("is_invalid") and t.get("user_confirmation") != "withdrawn"
        ]
        today = [t for t in all_tickets if str(t.get("created_at", "")).startswith(date_str)]
        resolved_today = [
            t for t in all_tickets
            if str(t.get("resolved_at", "")).startswith(date_str)
            and not t.get("is_invalid") and t.get("user_confirmation") != "withdrawn"
        ]
        pending = [t for t in all_tickets if t.get("status") in ("open", "assigned", "in_progress")]
        overdue = self.tickets.get_overdue_tickets()
        urgent = [t for t in pending if t.get("priority") in ("urgent", "high")]

        durations = []
        for ticket in resolved_today:
            start, end = self._parse(ticket.get("created_at")), self._parse(ticket.get("resolved_at"))
            if start and end and end >= start:
                durations.append((end - start).total_seconds() / 3600)

        now = datetime.now()
        response_eligible = []
        resolution_eligible = []
        for ticket in valid_tickets:
            response_due = self._parse(ticket.get("response_due_at"))
            first_response = self._parse(ticket.get("first_response_at"))
            if response_due and (first_response or response_due <= now):
                response_eligible.append(bool(first_response and first_response <= response_due))
            resolution_due = self._parse(ticket.get("resolution_due_at"))
            resolved_at = (self._parse(ticket.get("resolved_at"))
                           if ticket.get("status") in ("resolved", "closed") else None)
            if resolution_due and (resolved_at or resolution_due <= now):
                resolution_eligible.append(bool(resolved_at and resolved_at <= resolution_due))

        feedback = []
        list_feedback = getattr(self.tickets, "list_feedback", None)
        if list_feedback:
            try:
                feedback = list_feedback(limit=10000)
            except (TypeError, AttributeError):
                feedback = []
        scores = [int(row["score"]) for row in feedback if row.get("score") not in (None, "")]
        reopen_metrics = {"reopened": sum(
            1 for t in valid_tickets if t.get("user_confirmation") == "reopened"
        ), "resolved": sum(1 for t in valid_tickets if t.get("resolution"))}
        get_reopen_metrics = getattr(self.tickets, "get_reopen_metrics", None)
        if get_reopen_metrics:
            try:
                reopen_metrics = get_reopen_metrics()
            except (TypeError, AttributeError):
                pass
        reopened = int(reopen_metrics.get("reopened", 0))
        resolved_for_reopen = int(reopen_metrics.get("resolved", 0))
        confirmed = sum(1 for t in valid_tickets if t.get("confirmed_at"))
        confirmation_eligible = sum(
            1 for t in valid_tickets
            if t.get("status") in ("resolved", "closed") and t.get("resolution")
        )
        at_risk = [t for t in pending if t.get("sla_status") == "at_risk"]
        breached = [t for t in pending if t.get("sla_status") == "breached"]

        backlog_age = {"0_1d": 0, "1_3d": 0, "3_7d": 0, "over_7d": 0}
        for ticket in pending:
            created = self._parse(ticket.get("created_at"))
            if not created:
                continue
            age_days = max(0, (now - created).total_seconds() / 86400)
            key = "0_1d" if age_days < 1 else "1_3d" if age_days < 3 else "3_7d" if age_days < 7 else "over_7d"
            backlog_age[key] += 1

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
                "p50_resolution_hours": round(median(durations), 1) if durations else 0,
                "p95_resolution_hours": self._percentile(durations, 0.95),
                "response_sla_percent": self._percent(sum(response_eligible), len(response_eligible)),
                "resolution_sla_percent": self._percent(sum(resolution_eligible), len(resolution_eligible)),
                "sla_at_risk": len(at_risk), "sla_breached": len(breached),
                "awaiting_confirmation": sum(1 for t in all_tickets if t.get("status") == "resolved"),
                "confirmation_rate": self._percent(confirmed, confirmation_eligible),
                "csat": round(sum(scores) / len(scores), 2) if scores else 0,
                "reopen_rate": self._percent(reopened, resolved_for_reopen),
                "knowledge_gaps": len(unresolved), "low_knowledge_sources": len(low_sources),
            },
            "overdue_tickets": overdue[:10],
            "urgent_tickets": urgent[:10],
            "repeated_issues": repeated[:10],
            "top_categories": [{"category": key, "count": value} for key, value in category_counts.most_common(8)],
            "top_services": [{"service": key, "count": value} for key, value in
                             Counter(t.get("service_name") or "未归类" for t in today).most_common(8)],
            "assignee_load": [{"assignee": key, "count": value} for key, value in
                              Counter(t.get("assignee_nick") or "未分配" for t in pending).most_common(12)],
            "backlog_age": backlog_age,
            "knowledge_gaps": unresolved[:10],
            "low_knowledge_sources": low_sources[:10],
        }
