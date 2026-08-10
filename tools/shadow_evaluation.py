#!/usr/bin/env python3
"""Export and summarize privacy-bounded OpsPilot shadow evaluations."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable


HUMAN_FIELDS = [
    "intent_correct",
    "continuation_correct",
    "domains_correct",
    "candidate_kbs_correct",
    "completeness_correct",
    "suggested_action_correct",
    "expected_intent",
    "expected_domains",
    "expected_kb_ids",
    "reviewer_note",
]

OUTPUT_FIELDS = [
    "record_id", "timestamp", "intent", "is_continuation",
    "knowledge_domains", "candidate_kb_ids", "information_completeness",
    "suggested_action", "confidence", "model_latency_ms", "schema_valid",
    "degraded", "degrade_reason", "error_type", "legacy_action",
    "legacy_candidate_kb_ids", *HUMAN_FIELDS,
]


def _json_list(value: Any) -> str:
    return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)


def read_shadow_log(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}") from exc
        if item.get("record_type", "shadow_result") != "shadow_result":
            continue
        record_id = str(item.get("record_id") or "").strip()
        if not record_id:
            # Backward-compatible ID for Phase 1 logs created before record_id.
            record_id = "LEGACY-" + str(item.get("message_fingerprint") or line_number)
        rows.append({
            "record_id": record_id,
            "timestamp": item.get("timestamp", ""),
            "intent": item.get("intent", "unknown"),
            "is_continuation": item.get("is_continuation", False),
            "knowledge_domains": _json_list(item.get("knowledge_domains")),
            "candidate_kb_ids": _json_list(item.get("candidate_kb_ids")),
            "information_completeness": item.get("information_completeness", "unknown"),
            "suggested_action": item.get("suggested_action", "legacy_fallback"),
            "confidence": item.get("confidence", 0),
            "model_latency_ms": item.get("model_latency_ms", 0),
            "schema_valid": item.get("schema_valid", False),
            "degraded": item.get("degraded", False),
            "degrade_reason": item.get("degrade_reason", ""),
            "error_type": item.get("error_type", ""),
            "legacy_action": item.get("legacy_action", ""),
            "legacy_candidate_kb_ids": _json_list(item.get("legacy_candidate_kb_ids")),
        })
    return rows


def export_review_csv(log_path: Path, output_path: Path) -> int:
    existing = {}
    if output_path.exists():
        with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
            existing = {row.get("record_id", ""): row for row in csv.DictReader(handle)}
    rows = read_shadow_log(log_path)
    for row in rows:
        previous = existing.get(row["record_id"], {})
        for field in HUMAN_FIELDS:
            row[field] = previous.get(field, "")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _bool(value: Any) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "是", "正确"}:
        return True
    if normalized in {"0", "false", "no", "n", "否", "错误"}:
        return False
    return None


def _list(value: Any) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
    except ValueError:
        pass
    return {item.strip() for item in text.replace("，", ",").split(",") if item.strip()}


def _accuracy(rows: Iterable[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [_bool(row.get(field)) for row in rows]
    reviewed = [value for value in values if value is not None]
    return {
        "reviewed": len(reviewed),
        "correct": sum(value is True for value in reviewed),
        "accuracy": round(sum(value is True for value in reviewed) / len(reviewed), 4)
        if reviewed else None,
    }


def summarize(review_path: Path) -> dict[str, Any]:
    with review_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    metrics = {
        "total_shadow_samples": len(rows),
        "intent": _accuracy(rows, "intent_correct"),
        "continuation": _accuracy(rows, "continuation_correct"),
        "knowledge_domains": _accuracy(rows, "domains_correct"),
        "candidate_kbs": _accuracy(rows, "candidate_kbs_correct"),
        "information_completeness": _accuracy(rows, "completeness_correct"),
        "suggested_action": _accuracy(rows, "suggested_action_correct"),
    }
    expected_total = selected_total = omitted_total = 0
    for row in rows:
        if _bool(row.get("candidate_kbs_correct")) is None:
            continue
        expected = _list(row.get("expected_kb_ids"))
        predicted = _list(row.get("candidate_kb_ids"))
        expected_total += len(expected)
        selected_total += len(expected & predicted)
        omitted_total += len(expected - predicted)
    metrics["candidate_kb_hit_rate"] = (
        round(selected_total / expected_total, 4) if expected_total else None
    )
    metrics["candidate_kb_omission_rate"] = (
        round(omitted_total / expected_total, 4) if expected_total else None
    )
    metrics["candidate_kb_expected_total"] = expected_total
    metrics["low_confidence_samples"] = sum(
        str(row.get("degrade_reason")) == "low_confidence" for row in rows
    )
    technical_failures = sum(
        bool(str(row.get("error_type") or "").strip())
        or _bool(row.get("schema_valid")) is False
        for row in rows
    )
    metrics["technical_degradation_rate"] = (
        round(technical_failures / len(rows), 4) if rows else None
    )
    latencies = []
    for row in rows:
        try:
            latencies.append(float(row.get("model_latency_ms") or 0))
        except (TypeError, ValueError):
            continue
    metrics["average_model_latency_ms"] = (
        round(sum(latencies) / len(latencies), 1) if latencies else None
    )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsPilot shadow evaluation helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="export or update review CSV")
    export_parser.add_argument("--log", default="data/problem_understanding_shadow.jsonl")
    export_parser.add_argument("--output", default="data/problem_understanding_review.csv")
    summary_parser = subparsers.add_parser("summary", help="summarize human review CSV")
    summary_parser.add_argument("--input", default="data/problem_understanding_review.csv")
    summary_parser.add_argument("--output", default="data/problem_understanding_review_summary.json")
    args = parser.parse_args()
    if args.command == "export":
        count = export_review_csv(Path(args.log), Path(args.output))
        print(f"exported {count} shadow records to {args.output}")
        return 0
    result = summarize(Path(args.input))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
