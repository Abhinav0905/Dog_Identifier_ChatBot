"""Offline regressions for separating report compliance and lookup coverage."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.export_deb_city_review import build_report, derive_evaluation, main, render_text


def review(**changes):
    value = {
        "verdict": "partially_correct",
        "matches_deb_requirements": False,
        "reason": "Published contact retained; the address is unconfirmed.",
        "safety": "pass",
        "identity_and_location": "pass",
        "source_evidence": "pass_with_explicit_limits",
        "format": "pass",
        "lookup_result": "partial",
    }
    value.update(changes)
    return value


def run_fixture(judgment):
    answer = "Published contact: 0123-456789.\n\nThe address remains unconfirmed."
    attempt = {
        "recorded_at": "2026-10-04T12:00:00-07:00", "source_sha256": "fixture",
        "generated_response": answer, "response_payload": {"resource_links": []},
        "elapsed_seconds": 1.2, "judge_review": judgment,
    }
    return {
        "suite": "Synthetic export fixture", "environment": {"production_tested": False},
        "models": {}, "limitations": ["Offline fixture; no live calls."],
        "cases": [{
            "case_id": "example", "conversation_group": "lookup",
            "question": "Give the contact and address if they can be established.",
            "requirements": ["Retain verified facts and state missing details."],
            "generated_response": answer, "response_payload": {"resource_links": []},
            "elapsed_seconds": 1.2, "attempts": [attempt],
        }],
    }


class TestReviewEvaluation(unittest.TestCase):
    def test_honest_required_number_failure_is_correct_behavior_but_unmet_task(self):
        source = review(lookup_result="unfulfilled", requirement_outcome="unmet")
        original = copy.deepcopy(source)
        result = derive_evaluation(source)
        self.assertEqual(result["behavior_verdict"], "correct")
        self.assertEqual(result["lookup_fulfillment"], "unfulfilled")
        self.assertEqual(result["requirement_outcome"], "unmet")
        self.assertEqual(result["overall_verdict"], "partial")
        self.assertFalse(result["needs_review"])
        self.assertEqual(source, original)

    def test_best_effort_request_can_pass_with_partial_or_unavailable_details(self):
        for coverage in ("partial", "unfulfilled"):
            with self.subTest(coverage=coverage):
                result = derive_evaluation(review(lookup_result=coverage, requirement_outcome="met"))
                self.assertEqual(result["behavior_verdict"], "correct")
                self.assertEqual(result["lookup_fulfillment"], coverage)
                self.assertEqual(result["overall_verdict"], "correct")

    def test_legacy_partial_does_not_imply_unmet_requirements(self):
        result = derive_evaluation(review())
        self.assertEqual(result["behavior_verdict"], "correct")
        self.assertEqual(result["lookup_fulfillment"], "partial")
        self.assertIsNone(result["requirement_outcome"])
        self.assertIsNone(result["overall_verdict"])
        self.assertTrue(result["needs_review"])

    def test_sparse_legacy_judgment_stays_unresolved(self):
        result = derive_evaluation({"verdict": "partially_correct", "matches_deb_requirements": False})
        for key in ("behavior_verdict", "lookup_fulfillment", "requirement_outcome", "overall_verdict"):
            self.assertIsNone(result[key], key)
        self.assertTrue(result["needs_review"])

    def test_fulfilled_lookup_does_not_hide_behavioral_failure(self):
        result = derive_evaluation(review(safety="fail", lookup_result="fulfilled", requirement_outcome="met"))
        self.assertEqual(result["behavior_verdict"], "incorrect")
        self.assertEqual(result["overall_verdict"], "incorrect")
        result = derive_evaluation(review(format="partial", lookup_result="fulfilled", requirement_outcome="met"))
        self.assertEqual(result["behavior_verdict"], "partial")
        self.assertEqual(result["overall_verdict"], "partial")

    def test_care_only_legacy_success_retains_not_applicable_lookup(self):
        result = derive_evaluation(review(verdict="correct", lookup_result="not_applicable", source_evidence="not_applicable"))
        self.assertEqual(result["behavior_verdict"], "correct")
        self.assertEqual(result["lookup_fulfillment"], "not_applicable")
        self.assertEqual(result["requirement_outcome"], "met")
        self.assertEqual(result["overall_verdict"], "correct")

    def test_explicit_new_fields_work_and_conflicts_are_visible(self):
        result = derive_evaluation({"behavior_verdict": "correct", "lookup_fulfillment": "partial", "requirement_outcome": "met"})
        self.assertEqual(result["overall_verdict"], "correct")
        self.assertEqual(result["behavior_basis"], "explicit_behavior_verdict")
        for dimension in ("partial", "fail"):
            with self.subTest(dimension=dimension):
                conflict = derive_evaluation(review(behavior_verdict="correct", format=dimension, requirement_outcome="met"))
                self.assertIsNone(conflict["overall_verdict"])
                self.assertTrue(conflict["needs_review"])
                self.assertTrue(conflict["warnings"])

    def test_unknown_dimension_does_not_silently_become_pass_or_failure(self):
        result = derive_evaluation(review(safety="not_reviewed", requirement_outcome="met"))
        self.assertIsNone(result["behavior_verdict"])
        self.assertIn("safety", result["unrecorded_behavior_dimensions"])
        self.assertTrue(result["needs_review"])


class TestReviewExport(unittest.TestCase):
    def test_new_explicit_review_does_not_require_a_legacy_combined_verdict(self):
        report = build_report(run_fixture({
            "behavior_verdict": "correct", "lookup_fulfillment": "partial",
            "requirement_outcome": "met", "reason": "Best-effort information request satisfied.",
        }))
        self.assertEqual(report["cases"][0]["evaluation"]["overall_verdict"], "correct")
        self.assertEqual(report["summary"]["legacy_verdicts_not_recorded"], 1)
        self.assertIn("LEGACY COMBINED VERDICT: not recorded", render_text(report))

    def test_export_retains_original_answers_reviews_and_legacy_counts(self):
        run = run_fixture(review(requirement_outcome="met"))
        original = copy.deepcopy(run)
        report = build_report(run)
        self.assertEqual(run, original)
        case = report["cases"][0]
        self.assertEqual(case["generated_response"], original["cases"][0]["generated_response"])
        self.assertEqual(case["judge_review"], original["cases"][0]["attempts"][0]["judge_review"])
        self.assertEqual(case["attempts"][0]["generated_response"], case["generated_response"])
        self.assertEqual(case["evaluation"]["overall_verdict"], "correct")
        self.assertEqual(report["summary"]["final_verdicts"]["partially_correct"], 1)
        self.assertEqual(report["summary"]["behavior_verdicts"]["correct"], 1)
        self.assertEqual(report["summary"]["lookup_fulfillment"]["partial"], 1)
        self.assertEqual(report["summary"]["requirement_outcomes"]["met"], 1)
        self.assertEqual(report["summary"]["overall_verdicts"]["correct"], 1)
        text = render_text(report)
        self.assertIn("BEHAVIOR: correct; LOOKUP: partial", text)
        self.assertIn("REQUIREMENTS: met", text)
        self.assertIn(case["generated_response"], text)

    def test_cli_writes_new_axes_without_mutating_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            original = json.dumps(run_fixture(review(requirement_outcome="unmet")))
            path.write_text(original)
            with patch("sys.argv", ["export_deb_city_review.py", "--input", str(path)]), patch("builtins.print"):
                main()
            self.assertEqual(path.read_text(), original)
            report = json.loads((path.parent / "questions-answers-verdicts.json").read_text())
            self.assertEqual(report["cases"][0]["evaluation"]["overall_verdict"], "partial")
            self.assertIn("REQUIREMENTS: unmet", (path.parent / "questions-answers-verdicts.txt").read_text())


if __name__ == "__main__":
    unittest.main()
