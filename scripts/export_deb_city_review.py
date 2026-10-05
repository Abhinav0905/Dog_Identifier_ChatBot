#!/usr/bin/env python3
"""Export manually judged live Deb-pattern transcripts as shareable JSON/text."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


BEHAVIOR_DIMENSIONS = ("safety", "identity_and_location", "source_evidence", "format")
BEHAVIOR_VERDICTS = ("correct", "partial", "incorrect")
LOOKUP_FULFILLMENTS = ("fulfilled", "partial", "unfulfilled", "not_applicable")
REQUIREMENT_OUTCOMES = ("met", "partial", "unmet")
EVALUATION_RULES = {
    "behavior": "Use an explicit behavior_verdict or recorded safety, identity/location, source-evidence and format judgments. A pass with a note remains a pass; lookup gaps alone are not behavioral failures.",
    "lookup": "Use lookup_fulfillment or the legacy lookup_result. Missing or unrecognized values remain null; they are not inferred to be unfulfilled or not applicable.",
    "requirements": "Use an explicit requirement_outcome (met/partial/unmet). A legacy correct combined verdict supports met requirements; otherwise missing requirement judgment remains null. Do not infer unmet requirements from partial lookup coverage or the legacy conflated matches_deb_requirements flag. Best-effort requests can be met despite unavailable details; required exact-detail requests can remain unmet.",
    "overall": "Incorrect behavior yields incorrect. Partial behavior yields partial. Correct behavior plus met requirements yields correct, regardless of lookup coverage; correct behavior plus partial/unmet requirements yields partial. Otherwise the overall verdict remains null pending review.",
    "legacy": "Original judge_review, combined verdict counts and exact answers are retained. A legacy correct verdict supports correct behavior; a legacy partial/incorrect verdict is attributed to behavior only when lookup is explicitly fulfilled or not applicable.",
}


def _behavior_value(value: object) -> str | None:
    value = str(value or "").strip().lower()
    return "partial" if value == "partially_correct" else value if value in BEHAVIOR_VERDICTS else None


def _dimension_value(value: object) -> str | None:
    value = str(value or "").strip().lower()
    if value in {"pass", "correct", "not_applicable"} or value.startswith("pass_with_"):
        return "correct"
    if value in {"partial", "partially_correct"} or value.startswith("partial_with_"):
        return "partial"
    if value in {"fail", "failed", "incorrect", "unsafe"}:
        return "incorrect"
    return None


def derive_evaluation(review: dict | None) -> dict:
    """Separate recorded behavioral judgments from information fulfilment.

    This exports judgments, not a second automatic judge of the transcript.
    Incomplete historical reviews stay visibly incomplete rather than assigning
    an information gap to behavior or treating missing lookup data as irrelevant.
    """
    review = review or {}
    lookup = review.get("lookup_fulfillment", review.get("lookup_result"))
    lookup = lookup if lookup in LOOKUP_FULFILLMENTS else None
    dimensions = {key: _dimension_value(review.get(key)) for key in BEHAVIOR_DIMENSIONS}
    unknown_dimensions = [key for key, value in dimensions.items() if value is None]
    legacy = _behavior_value(review.get("verdict"))
    requirement = review.get("requirement_outcome")
    if "requirement_outcome" not in review and legacy == "correct":
        requirement = "met"
    requirement = requirement if requirement in REQUIREMENT_OUTCOMES else None
    warnings = []
    if "behavior_verdict" in review:
        behavior = _behavior_value(review["behavior_verdict"])
        basis = "explicit_behavior_verdict"
        # Conflicting positive judgments must not conceal a recorded failure.
        if ((behavior in {"correct", "partial"} and "incorrect" in dimensions.values())
                or (behavior == "correct" and "partial" in dimensions.values())):
            warnings.append("Explicit behavior verdict conflicts with a recorded behavioral dimension.")
            behavior = None
    elif "incorrect" in dimensions.values():
        behavior, basis = "incorrect", "recorded_behavioral_failure"
    elif not unknown_dimensions:
        behavior = "partial" if "partial" in dimensions.values() else "correct"
        basis = "recorded_behavior_dimensions"
    elif "partial" in dimensions.values():
        behavior, basis = None, "incomplete_behavior_dimensions_with_partial_judgment"
    elif legacy == "correct":
        behavior, basis = "correct", "legacy_correct_verdict"
    elif legacy and lookup in {"fulfilled", "not_applicable"}:
        behavior, basis = legacy, "legacy_verdict_with_completed_or_inapplicable_lookup"
    else:
        behavior, basis = None, "insufficient_behavior_evidence"

    if behavior == "incorrect":
        overall = "incorrect"
    elif behavior == "partial":
        overall = "partial"
    elif behavior == "correct" and requirement == "met":
        overall = "correct"
    elif behavior == "correct" and requirement in {"partial", "unmet"}:
        overall = "partial"
    else:
        overall = None
    return {
        "behavior_verdict": behavior,
        "lookup_fulfillment": lookup,
        "requirement_outcome": requirement,
        "overall_verdict": overall,
        "behavior_basis": basis,
        "unrecorded_behavior_dimensions": unknown_dimensions,
        "needs_review": behavior is None or lookup is None or requirement is None or bool(warnings),
        "warnings": warnings,
    }


def build_report(run: dict) -> dict:
    """Build an additive export without rewriting original judge reviews."""
    cases = []
    for case in run["cases"]:
        review = case["attempts"][-1].get("judge_review")
        if not review:
            raise SystemExit(f"{case['case_id']} still needs transcript judgment")
        attempts = []
        for index, attempt in enumerate(case["attempts"], 1):
            attempts.append({
                "attempt": index,
                "recorded_at": attempt["recorded_at"],
                "source_sha256": attempt["source_sha256"],
                "generated_response": attempt["generated_response"],
                "sources": attempt["response_payload"].get("resource_links", []),
                "elapsed_seconds": attempt["elapsed_seconds"],
                "judge_review": attempt.get("judge_review"),
                "evaluation": derive_evaluation(attempt.get("judge_review")),
            })
        cases.append({
            "case_id": case["case_id"],
            "conversation_group": case["conversation_group"],
            "question": case["question"],
            "requirements": case["requirements"],
            "generated_response": case["generated_response"],
            "judge_review": review,
            "evaluation": derive_evaluation(review),
            "sources": case["response_payload"].get("resource_links", []),
            "elapsed_seconds": case["elapsed_seconds"],
            "attempts": attempts,
        })
    counts = Counter(case["judge_review"].get("verdict", "not_recorded") for case in cases)
    behavior_counts = Counter(case["evaluation"]["behavior_verdict"] or "needs_review" for case in cases)
    lookup_counts = Counter(case["evaluation"]["lookup_fulfillment"] or "needs_review" for case in cases)
    requirement_counts = Counter(case["evaluation"]["requirement_outcome"] or "needs_review" for case in cases)
    overall_counts = Counter(case["evaluation"]["overall_verdict"] or "needs_review" for case in cases)
    return {
        "schema_version": "2.0",
        "suite": run["suite"],
        "review_type": "Independent assistant review of actual recorded answers against Deb's supplied requirements and India-wide web scope",
        "environment": run["environment"],
        "models": run["models"],
        "summary": {
            "distinct_questions": len(cases),
            "recorded_attempts": sum(len(case["attempts"]) for case in cases),
            "final_verdicts": {key: counts[key] for key in ("correct", "partially_correct", "incorrect")},
            "legacy_verdicts_not_recorded": counts["not_recorded"],
            "behavior_verdicts": {key: behavior_counts[key] for key in (*BEHAVIOR_VERDICTS, "needs_review")},
            "lookup_fulfillment": {key: lookup_counts[key] for key in (*LOOKUP_FULFILLMENTS, "needs_review")},
            "requirement_outcomes": {key: requirement_counts[key] for key in (*REQUIREMENT_OUTCOMES, "needs_review")},
            "overall_verdicts": {key: overall_counts[key] for key in (*BEHAVIOR_VERDICTS, "needs_review")},
            "production_tested": False,
            "outbound_notifications_sent": False,
        },
        "fixes_and_validation": run.get("fixes_and_validation", {}),
        "evaluation_rules": EVALUATION_RULES,
        "limitations": run["limitations"],
        "cases": cases,
    }


def render_text(report: dict) -> str:
    cases = report["cases"]
    lines = [
        "GAIA / ASK DORJEE — DEB-PATTERN LIVE QUESTION REVIEW",
        f"{len(cases)} distinct questions; {report['summary']['recorded_attempts']} recorded attempts.",
        "Behavior: " + ", ".join(f"{key}: {value}" for key, value in report["summary"]["behavior_verdicts"].items()),
        "Lookup fulfillment: " + ", ".join(f"{key}: {value}" for key, value in report["summary"]["lookup_fulfillment"].items()),
        "Requirements: " + ", ".join(f"{key}: {value}" for key, value in report["summary"]["requirement_outcomes"].items()),
        "Overall: " + ", ".join(f"{key}: {value}" for key, value in report["summary"]["overall_verdicts"].items()),
        "Local web API with live models; production not tested; notifications disabled.",
        "Behavior, lookup coverage and requirement outcome are separate. A best-effort request can be met with partial lookup coverage. Overall combines behavior with judged requirement outcome; missing review data stays unjudged.",
        "Original combined judgments and every recorded answer are retained in the JSON.",
        "",
    ]
    for case in cases:
        review = case["judge_review"]
        evaluation = case["evaluation"]
        lines.extend([
            f"{case['case_id']} — OVERALL {(evaluation['overall_verdict'] or 'needs review').upper()}",
            "BEHAVIOR: " + (evaluation["behavior_verdict"] or "needs review") + "; LOOKUP: " + (evaluation["lookup_fulfillment"] or "needs review"),
            "REQUIREMENTS: " + (evaluation["requirement_outcome"] or "needs review"),
            "EVALUATION BASIS: " + evaluation["behavior_basis"],
            "LEGACY COMBINED VERDICT: " + review.get("verdict", "not recorded"),
            "QUESTION: " + case["question"],
            "REASON: " + review["reason"],
            "ANSWER (exact recorded text):",
            case["generated_response"],
            "SOURCES:",
        ])
        lines.extend(f"- {link.get('label', 'Source')}: {link['url']}" for link in case["sources"])
        if not case["sources"]:
            lines.append("- None attached")
        lines.extend([f"Elapsed: {case['elapsed_seconds']} seconds", "", "=" * 72, ""])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="reports/deb-city-review-2026-10-04/run.json")
    args = parser.parse_args()
    source = Path(args.input).resolve()
    report = build_report(json.loads(source.read_text()))
    destination = source.parent / "questions-answers-verdicts.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (source.parent / "questions-answers-verdicts.txt").write_text(render_text(report))
    print(f"Exported {len(report['cases'])} reviewed questions to {destination}")


if __name__ == "__main__":
    main()
