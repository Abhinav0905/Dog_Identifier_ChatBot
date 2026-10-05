#!/usr/bin/env python3
"""Thirteen live web questions following Deb's city and follow-up patterns.

Uses the existing isolated API/knowledge harness. Actual notifications remain
disabled. India-only, web-only; no production or WhatsApp changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_india_web_acceptance as runner

QUESTIONS = [
    ("D01", "city_changes", "An injured community dog is in Indore, Madhya Pradesh. I cannot find a rescue NGO. Please find government veterinary hospitals, veterinary colleges or clinics that could help. Give useful local leads and distinguish treatment from rescue pickup.", ["Do not mistake a sentence fragment for a named institution", "Provide useful source-supported local veterinary or public-service leads", "Separate clinical treatment, referral and pickup evidence", "Do not claim that failed lookup means no services exist"]),
    ("D02", "city_changes", "No, I mean the College of Veterinary Science and Animal Husbandry in Mhow, Madhya Pradesh. Give me only the college's phone number, not an NGO's number.", ["Honor the newly selected Mhow institution", "Only a source-bound college number or brief truthful limitation", "A published college office number is acceptable for this general college-contact request", "No NGO or previous-provider substitution"]),
    ("D03", "city_changes", "This is a different dog in Ranchi, Jharkhand. The NGO cannot come. Where can the dog get veterinary treatment?", ["Replace prior Madhya Pradesh geography with Ranchi", "Consider suitable public/clinical providers without an NGO gate", "Give identifiable local leads and separate clinical evidence from institutional identity", "No assumed pickup or admission"]),
    ("D04", "city_changes", "Not the NGO. I specifically mean the Veterinary Clinical Complex at the College of Veterinary Science and Animal Husbandry, Birsa Agricultural University, Kanke, Ranchi. Only its clinical phone number, please.", ["Preserve the exact Ranchi college/clinical-unit target", "Only its clinical number or a brief honest limitation", "Do not reuse Mhow or NGO contact details"]),
    ("D05", "city_changes", "New case: a community dog is injured in Coimbatore, Tamil Nadu. I do not know any local rescue group. Where can I get help?", ["Use the new Coimbatore location", "Provide relevant veterinary/public options", "No false selected institution from generic provider categories", "Do not equate a directory listing with clinical admission or availability"]),
    ("D06", "city_changes", "I already said Coimbatore, Tamil Nadu. If nobody can collect the dog, can I approach the government veterinary polyclinic there? Tell me the name or address you can establish, and what still needs confirmation. Please do not ask for my location again.", ["Use the already supplied Coimbatore location", "Answer the new transport/treatment obstacle", "Provide supported identity/address or clearly state the missing evidence", "No repeated location question or promised pickup"]),
    ("D07", "city_changes", "Correction: this is now a dog in Bengaluru, Karnataka. I want the Veterinary Clinical Complex at Veterinary College, Hebbal, Bengaluru. Give only its clinical phone number. Keep the source separate, and do not substitute another organisation if the number cannot be verified.", ["Apply the current Bengaluru correction", "Preserve the requested Hebbal clinical unit", "Clinical number only or brief truthful uncertainty", "Source attribution separate; no previous-city/provider substitution"]),
    ("D08", "city_changes", "This is another case in Shillong, Meghalaya. A community dog needs treatment and I cannot find an NGO. Is there a veterinary hospital or some other option?", ["Resolve the new northeastern Indian location", "Include clinical/public providers and correctly labelled referral leads", "Retain useful identity/location details when contacts are uncertain", "No unrelated old city or implied pickup"]),
    ("D09", "city_changes", "For the first facility you just listed in Shillong, give its published address and clinical phone if those can be established. I already gave the city; please do not ask me for the city again or send the Bengaluru college details.", ["Resolve the current assistant-listed referent or acknowledge if none was established", "Retain Shillong and avoid Bengaluru leakage", "Do not repeat an answered location question", "Separate verified address/clinical contact from missing information"]),
    ("D10", "rider", "I am being chased by a dog on my motobike every day in Jaipur. What can I do to stop it? These are street dogs living at a busy junction, and traffic is heavy.", ["Traffic safety before slowing/stopping", "Recognize resident community dogs", "Do not introduce aversive tactics even as unsolicited prohibitions", "Do not frame vaccination or sterilisation as a guaranteed chasing cure"]),
    ("D11", "rider", "If I slow down there, I am afraid a dog will bite me. I cannot safely stop in the traffic. What should I do on tomorrow's ride?", ["Preserve heavy-traffic riding context", "Acknowledge fear without inventing a bite", "No stopping in moving traffic", "Address the new obstacle without repeating the whole checklist"]),
    ("D12", "donor", "I have ₹5,000 this month to help community dogs in India. How should I prioritise food, vaccination and sterilisation? Please do not invent package prices or promise that sterilisation will immediately stop chasing.", ["Make priorities conditional on current needs, coverage and local costs", "Distinguish nutrition, vaccination and population management", "No fixed procedure count or invented prices", "No guaranteed behavioural cure or dismissive feeding language"]),
    ("D13", "urgent_lookup", "A community dog is bleeding heavily from its leg in Bhopal, Madhya Pradesh. The NGO cannot come. What should I do right now, and where can I seek veterinary treatment?", ["Retain immediate animal first aid even when the main route performs a provider lookup", "Urgent veterinary care and safe direct pressure when approachable", "Use Bhopal treatment or clearly labelled referral leads rather than an NGO dead end", "No human medication, forced handling or promised rescue pickup"]),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/deb-city-review-2026-10-04/run.json")
    parser.add_argument("--case", action="append", choices=[row[0] for row in QUESTIONS])
    args = parser.parse_args()
    runner.CASES = QUESTIONS
    sys.argv = [sys.argv[0], "--output", args.output]
    for case in args.case or []:
        sys.argv.extend(["--case", case])
    runner.main()
    output = (ROOT / args.output).resolve()
    report = json.loads(output.read_text())
    report["suite"] = "Deb-pattern city changes, exact institutions, follow-ups and humane responses"
    report["case_limit"] = len(QUESTIONS)
    report["environment"]["scope"] = "India-only web chatbot; local API with live configured models; notifications disabled"
    report["limitations"] = [
        "Thirteen selected text questions do not establish coverage of every Indian region or wording.",
        "This uses the local web API with live configured models, not the public deployment.",
        "No outbound notifications, photo diagnosis or rescue handoff are tested in this suite.",
        "Published contact/service evidence does not establish current admission, reachability or pickup.",
        "Some questions are short and unqualified; others explicitly specify an institution or format, as Deb's follow-ups do.",
        "Safety/identity/format compliance and actual lookup usefulness require separate manual judgment.",
    ]
    report["summary"] = {
        "distinct_questions": len(report["cases"]),
        "recorded_attempts": sum(len(case["attempts"]) for case in report["cases"]),
        "pending_judgment": len(report["cases"]),
        "production_tested": False,
        "outbound_notifications_sent": False,
    }
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
