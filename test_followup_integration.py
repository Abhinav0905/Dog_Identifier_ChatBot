#!/usr/bin/env python3
"""Offline endpoint regressions for model-directed follow-ups with legacy history."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from services.query_router import TextAction, TextTurn
from services.web_search import SearchResult


PUNE_ORGANIZATIONS = [
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


class TestStructuredFollowUpRouting(unittest.TestCase):
    def setUp(self):
        self.history = [
            {"role": "user", "content": "List dog NGOs in Pune"},
            {
                "role": "assistant",
                "content": "Verified options",
                "metadata": {
                    "organizations": PUNE_ORGANIZATIONS,
                    "service_city": "Pune",
                },
            },
        ]
        self.case = {
            "scope": app.region_scope.INDIA,
            "place": "Pune, Maharashtra, India",
            "city": "Pune",
            "region": "Maharashtra",
            "lat": 18.5214,
            "lng": 73.8545,
            "country_code": "in",
            "source": "session_case",
            "in_dharamsala": False,
        }

    def request(self, message, turn, *, search_response="Current provider details.", care_response="Condition-specific model care.", search_error=None):
        with ExitStack() as stack:
            def mocked(owner, name, **kwargs):
                return stack.enter_context(patch.object(owner, name, **kwargs))

            mocked(app, "_resolve_request_session", return_value="owned-test")
            mocked(app.query_router, "client", new=None)
            plan = mocked(app.query_router, "plan_text_turn", return_value=turn)
            mocked(app.db, "get_chat_history", return_value=self.history)
            mocked(app.db, "get_session_case_location", return_value=self.case)
            mocked(app.db, "save_session_case_location")
            saved = mocked(app.db, "save_chat_message")
            city = mocked(app.region_scope.place_resolver, "resolve_service_city")
            snapshot = mocked(app.ngo_followup, "answer_from_history")
            cache = mocked(app.db, "get_ngo_search_cache")
            legacy_search = mocked(app.web_search, "search_verified_india_local_help")
            search = mocked(app.web_search, "search_animal_question", side_effect=search_error, return_value=SearchResult(
                response=search_response, searched=True, result_kind="search_answer",
                resource_links=[{"label": "Contact source", "url": "https://example.org/contact"}],
            ))
            chat = mocked(app.triage, "generate_chat_response", return_value=care_response)
            response = TestClient(app.app).post(
                "/v1/chat/query", json={"message": message, "session_id": "owned-test"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        city.assert_not_called()
        snapshot.assert_not_called()
        cache.assert_not_called()
        legacy_search.assert_not_called()
        plan.assert_called_once_with(message, self.history, case_location=self.case, language="en")
        return SimpleNamespace(response=response.json(), search=search, chat=chat, metadata=saved.call_args.kwargs["metadata"])

    def test_contact_follow_up_searches_with_history_instead_of_using_snapshot(self):
        message = "Give me their address and phone number"
        turn = TextTurn(action=TextAction.SEARCH, contextual_request="Find current contact details for the Pune animal-help providers just listed")
        result = self.request(message, turn)
        self.assertEqual(result.response["response"], "Current provider details.")
        self.assertEqual(result.search.call_args.args, (message, self.history))
        self.assertEqual(result.search.call_args.kwargs["resolved_place"], self.case["place"])
        self.assertEqual(result.search.call_args.kwargs["tool_choice"], "required")
        self.assertTrue(result.metadata["web_search_ran"])
        self.assertNotIn("organizations", result.metadata)
        result.chat.assert_not_called()

    def test_ordinal_follow_up_passes_selected_provider_context_to_search(self):
        message = "Tell me about the second one"
        turn = TextTurn(action=TextAction.SEARCH, contextual_request="Find current information about Second Chance Dogs, the second Pune provider")
        result = self.request(message, turn, search_response="Current Second Chance Dogs details.")
        self.assertEqual(result.search.call_args.kwargs["contextual_request"], turn.contextual_request)
        self.assertEqual(result.search.call_args.args[1], self.history)
        self.assertEqual(result.response["response"], "Current Second Chance Dogs details.")
        self.assertEqual(result.metadata["model_history_policy"], app.triage.MODEL_HISTORY_INCLUDE)

    def test_dharamsala_text_rescue_turn_persists_search_evidence_without_ngo_snapshot(self):
        self.case = {**self.case, "place": "Dharamshala, Himachal Pradesh, India", "city": "Dharamshala", "region": "Himachal Pradesh"}
        message = "A dog was hit by a car here. Who can help?"
        turn = TextTurn(action=TextAction.SEARCH, contextual_request="Find help for a dog hit by a car in Dharamshala", needs_immediate_guidance=True)
        result = self.request(message, turn, search_response="Current Dharamshala veterinary help.")
        self.assertIn("emergency veterinary care", result.response["response"].lower())
        self.assertTrue(result.response["response"].endswith("Current Dharamshala veterinary help."))
        self.assertTrue(result.metadata["web_search_ran"])
        self.assertEqual(result.metadata["resource_links"][0]["url"], result.response["resource_links"][0]["url"])
        self.assertNotIn("organizations", result.metadata)

    def test_pune_distress_keeps_guidance_when_router_and_web_are_unavailable(self):
        message = "A dog is bleeding heavily. What should I do?"
        turn = TextTurn(contextual_request=message, source="model_unavailable")
        result = self.request(message, turn, search_error=RuntimeError("Search unavailable"))
        self.assertIn("firm, continuous, direct pressure", result.response["response"].lower())
        result.search.assert_called_once()
        self.assertEqual(result.search.call_args.kwargs["tool_choice"], "auto")
        result.chat.assert_not_called()
        self.assertFalse(result.metadata["web_search_ran"])
        self.assertEqual(result.metadata["result_kind"], "unavailable")

    def _assert_care_follow_up(self, message, contextual_request, care_response):
        turn = TextTurn(action=TextAction.ANSWER, contextual_request=contextual_request)
        result = self.request(message, turn, care_response=care_response)
        self.assertEqual(result.response["response"], care_response)
        self.assertEqual(result.response["resource_links"], [])
        result.chat.assert_called_once_with(message, self.history, "owned-test", "en", contextual_message=contextual_request)
        result.search.assert_not_called()
        self.assertFalse(result.metadata["web_search_ran"])
        self.assertEqual(result.metadata["model_history_policy"], app.triage.MODEL_HISTORY_INCLUDE)

    def test_action_only_follow_up_uses_model_without_repeating_search(self):
        self._assert_care_follow_up("What should I do now?", "What immediate care can I give the street dog in this case?", "Condition-specific model care.")

    def test_pronoun_care_follow_up_does_not_become_provider_detail_request(self):
        self._assert_care_follow_up("What can I do for it right now?", "How can I safely help the dog described earlier?", "Model care for the dog.")

    def test_same_case_condition_update_uses_model_without_repeating_search(self):
        self._assert_care_follow_up(
            "The dog is bleeding now. What should I do?", "The street dog in the same case is now bleeding; give immediate first aid",
            "Apply firm, continuous, direct pressure with a clean cloth and seek urgent help.",
        )


if __name__ == "__main__":
    unittest.main()
