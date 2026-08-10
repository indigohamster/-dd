import csv
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from modules.problem_understanding import ProblemUnderstandingService
from tools.shadow_evaluation import export_review_csv, summarize


def payload():
    return {
        "schema_version": "1.0",
        "intent": "incident_help",
        "topic": "submission error",
        "knowledge_domains": ["business documents"],
        "candidate_kb_ids": ["kb-a"],
        "is_continuation": False,
        "information_completeness": "partial",
        "missing_information": ["error details"],
        "suggested_action": "retrieve_then_clarify",
        "confidence": 0.8,
        "active_state_attribution": {
            "state_type": "none",
            "relation": "new_problem",
            "confidence": 0.9,
        },
    }


class SamplingAndShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.router = Mock()
        self.router.chat.return_value = (json.dumps(payload()), "test-model")
        self.ima = {"default_knowledge_bases": [{"id": "kb-a", "name": "KB A"}]}

    def tearDown(self):
        self.temp.cleanup()

    def service(self, sample_rate):
        return ProblemUnderstandingService({
            "enabled": True,
            "mode": "shadow",
            "sample_rate": sample_rate,
            "log_path": "shadow.jsonl",
            "metrics_path": "metrics.json",
        }, self.ima, self.router, base_dir=self.root)

    def test_zero_sampling_never_calls_model(self):
        service = self.service(0)
        self.assertFalse(service.submit_shadow(message="question", legacy_action="legacy_fallback"))
        service.shutdown(wait=True)
        self.router.chat.assert_not_called()
        self.assertFalse((self.root / "shadow.jsonl").exists())

    def test_one_sampling_calls_model_and_creates_record_id(self):
        service = self.service(1)
        self.assertTrue(service.submit_shadow(
            message="question", legacy_action="legacy_fallback", message_id="m-1",
        ))
        service.shutdown(wait=True)
        row = json.loads((self.root / "shadow.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(row["record_type"], "shadow_result")
        self.assertTrue(row["record_id"].startswith("SHD-"))
        self.router.chat.assert_called_once()

    def test_intermediate_sampling_is_deterministic(self):
        service = self.service(0.5)
        first = service._is_sampled("stable-message-id")
        self.assertEqual(first, service._is_sampled("stable-message-id"))
        service.shutdown(wait=True)

    def test_empty_message_is_never_sampled(self):
        service = self.service(1)
        self.assertFalse(service.submit_shadow(message="   ", legacy_action="legacy_fallback"))
        service.shutdown(wait=True)
        self.router.chat.assert_not_called()

    def test_shutdown_rejects_new_work_and_records_queue_disposition(self):
        service = self.service(1)
        result = service.shutdown(wait=False)
        self.assertEqual(result, {"discarded": 0, "in_flight": 0})
        self.assertFalse(service.submit_shadow(message="later", legacy_action="legacy_fallback"))
        metrics = json.loads((self.root / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["shutdown_discarded"], 0)


class HumanEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "shadow.jsonl"
        self.review = self.root / "review.csv"
        rows = [
            {
                "record_type": "shadow_result", "record_id": "SHD-1", "timestamp": "2026-08-10",
                "intent": "incident_help", "is_continuation": False,
                "knowledge_domains": ["network"], "candidate_kb_ids": ["kb-a"],
                "information_completeness": "partial", "suggested_action": "ask_clarification",
                "confidence": 0.8, "model_latency_ms": 100, "schema_valid": True,
                "degraded": False, "degrade_reason": "", "error_type": "",
                "legacy_action": "retrieve_and_answer", "legacy_candidate_kb_ids": [],
            },
            {
                "record_type": "shadow_result", "record_id": "SHD-2", "timestamp": "2026-08-10",
                "intent": "unknown", "is_continuation": False, "knowledge_domains": [],
                "candidate_kb_ids": [], "information_completeness": "unknown",
                "suggested_action": "legacy_fallback", "confidence": 0,
                "model_latency_ms": 300, "schema_valid": False, "degraded": True,
                "degrade_reason": "model_or_validation_error", "error_type": "TimeoutError",
                "legacy_action": "retrieve_and_answer", "legacy_candidate_kb_ids": [],
            },
        ]
        self.log.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_export_preserves_existing_human_annotations(self):
        self.assertEqual(export_review_csv(self.log, self.review), 2)
        with self.review.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["intent_correct"] = "1"
        rows[0]["expected_kb_ids"] = '["kb-a", "kb-b"]'
        rows[0]["candidate_kbs_correct"] = "0"
        with self.review.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        export_review_csv(self.log, self.review)
        with self.review.open("r", encoding="utf-8-sig", newline="") as handle:
            refreshed = list(csv.DictReader(handle))
        self.assertEqual(refreshed[0]["intent_correct"], "1")
        self.assertEqual(refreshed[0]["expected_kb_ids"], '["kb-a", "kb-b"]')

    def test_summary_uses_only_human_review_for_accuracy(self):
        export_review_csv(self.log, self.review)
        with self.review.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0].update({
            "intent_correct": "1", "continuation_correct": "1", "domains_correct": "1",
            "candidate_kbs_correct": "0", "completeness_correct": "1",
            "suggested_action_correct": "0", "expected_kb_ids": '["kb-a", "kb-b"]',
        })
        with self.review.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        report = summarize(self.review)
        self.assertEqual(report["intent"], {"reviewed": 1, "correct": 1, "accuracy": 1.0})
        self.assertEqual(report["suggested_action"]["accuracy"], 0.0)
        self.assertEqual(report["candidate_kb_hit_rate"], 0.5)
        self.assertEqual(report["candidate_kb_omission_rate"], 0.5)
        self.assertEqual(report["technical_degradation_rate"], 0.5)
        self.assertEqual(report["average_model_latency_ms"], 200.0)


if __name__ == "__main__":
    unittest.main()
