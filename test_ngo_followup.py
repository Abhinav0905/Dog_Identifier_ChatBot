#!/usr/bin/env python3
"""Structured NGO follow-up regression tests."""

from __future__ import annotations

import unittest

from services import ngo_followup


ORGANIZATIONS = [
    {
        "name": "Pune Animal Rescue",
        "service_area": "Pune",
        "animal_rescue_evidence": "Rescues injured dogs in Pune",
        "official_url": "https://example.org/pune-rescue",
        "phone": "+91 20 1234 5678",
        "address": "Kothrud, Pune",
        "opening_hours": "",
    },
    {
        "name": "Second Chance Dogs",
        "service_area": "Pune",
        "animal_rescue_evidence": "Accepts sick and injured stray dogs",
        "official_url": "https://example.org/second-chance",
        "phone": "",
        "address": "",
        "opening_hours": "10:00-18:00 daily",
    },
]


def history(selected=None):
    metadata = {
        "organizations": ORGANIZATIONS,
        "service_city": "Pune",
    }
    if selected is not None:
        metadata["selected_ngo_index"] = selected
    return [
        {"role": "user", "content": "List dog NGOs in Pune"},
        {
            "role": "assistant",
            "content": "Verified options",
            "metadata": metadata,
        },
    ]


class TestNgoFollowUp(unittest.TestCase):
    def test_latest_snapshot_carries_contacts_without_rendering_directory(self):
        snapshot = ngo_followup.latest_snapshot(history(selected=1))

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.response, "")
        self.assertEqual(snapshot.resource_links, [])
        self.assertEqual(snapshot.organizations, ORGANIZATIONS)
        self.assertEqual(snapshot.selected_ngo_index, 1)
        self.assertEqual(snapshot.service_city, "Pune")

    def test_contact_follow_up_uses_snapshot_without_inventing_phone(self):
        answer = ngo_followup.answer_from_history(
            "Give me their address and phone number",
            history(),
        )
        self.assertIsNotNone(answer)
        self.assertIn("Kothrud, Pune", answer.response)
        self.assertIn("+91 20 1234 5678", answer.response)
        self.assertIn("Phone: not verified", answer.response)
        self.assertEqual(len(answer.resource_links), 2)
        self.assertEqual(answer.resource_links[0]["phone"], "+91 20 1234 5678")
        self.assertEqual(answer.resource_links[0]["address"], "Kothrud, Pune")
        self.assertNotIn("phone", answer.resource_links[1])

    def test_ordinal_follow_up_selects_stable_result(self):
        answer = ngo_followup.answer_from_history(
            "Tell me about the second one",
            history(),
        )
        self.assertEqual(answer.selected_ngo_index, 1)
        self.assertIn("Second Chance Dogs", answer.response)
        self.assertNotIn("Pune Animal Rescue", answer.response)
        self.assertEqual(len(answer.resource_links), 1)

    def test_pronoun_reuses_previous_selection(self):
        answer = ngo_followup.answer_from_history(
            "What is its phone number?",
            history(selected=1),
        )
        self.assertEqual(answer.selected_ngo_index, 1)
        self.assertIn("Second Chance Dogs", answer.response)

    def test_hours_follow_up_reports_only_verified_hours(self):
        answer = ngo_followup.answer_from_history("Are they open now?", history())
        self.assertIn("were not verified", answer.response)
        self.assertIn("10:00-18:00 daily", answer.response)
        self.assertNotIn("They are open", answer.response)

    def test_closest_follow_up_does_not_invent_distance(self):
        answer = ngo_followup.answer_from_history("Which one is closest?", history())
        self.assertIn("cannot reliably rank", answer.response)

    def test_new_unrelated_request_does_not_use_old_snapshot(self):
        self.assertIsNone(
            ngo_followup.answer_from_history("List dog NGOs in Mumbai", history())
        )

    def test_intervening_assistant_turn_clears_old_snapshot(self):
        stale_history = history() + [
            {"role": "user", "content": "How can I stay safe around community dogs?"},
            {
                "role": "assistant",
                "content": "Keep a safe distance and do not chase a frightened dog.",
                "metadata": {"organizations": [], "service_city": ""},
            },
        ]

        self.assertIsNone(
            ngo_followup.answer_from_history(
                "What is their phone number?",
                stale_history,
            )
        )

    def test_invalid_latest_snapshot_does_not_revive_older_valid_snapshot(self):
        stale_history = history() + [
            {
                "role": "assistant",
                "content": "A newer response without a usable NGO snapshot.",
                "metadata": {"organizations": [{"name": "Incomplete"}]},
            }
        ]

        self.assertIsNone(
            ngo_followup.answer_from_history("Tell me about the first one", stale_history)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
