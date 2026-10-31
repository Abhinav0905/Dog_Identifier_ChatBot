"""Offline regressions for model-directed conversations with real persistence."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app
import config
import database as db
from services import query_router as router, region_scope, web_search


def planned(action="search", message="", place="", **kwargs):
    return router.TextTurn(
        action=router.TextAction(action), contextual_request=message,
        location_kind="named_place" if place else "none", location_text=place,
        **kwargs,
    )


def model_payload(**overrides):
    value = {
        "action": "answer", "contextual_request": "A dog may bite the motorbike rider.",
        "location_kind": "none", "location_text": "", "clarification_question": "",
        "needs_immediate_guidance": True,
    }
    value.update(overrides)
    return MagicMock(output_text=json.dumps(value))


class TestTextActionModel(unittest.TestCase):
    def test_latest_behavior_decision_cannot_be_overridden_by_old_ngo_intent(self):
        model = MagicMock()
        model.responses.create.return_value = model_payload()
        history = [
            {"role": "user", "content": "Find NGOs in Palampur."},
            {"role": "assistant", "content": "An old NGO."},
            {"role": "user", "content": "A dog is chasing me on my motor bike."},
            {"role": "assistant", "content": "Keep road safety first."},
        ]
        with patch.object(router, "client", model), patch.object(
            router, "_deterministic_analysis", side_effect=AssertionError("Old NGO router used")
        ):
            turn = router.plan_text_turn("He may bite me if I slow down.", history)
        self.assertEqual(turn.action, router.TextAction.ANSWER)
        self.assertEqual(turn.location_kind, "none")
        sent = json.loads(model.responses.create.call_args.kwargs["input"][1]["content"])
        self.assertEqual(sent["recent_conversation"], history)
        self.assertFalse(model.responses.create.call_args.kwargs["store"])

    def test_provider_correction_and_source_history_reach_action_model(self):
        model = MagicMock()
        model.responses.create.return_value = model_payload(
            action="search", contextual_request="Only Veterinary College Palampur phone.",
            needs_immediate_guidance=False,
        )
        history = [{"role": "assistant", "content": "Regional NGO [source](https://example.org).",
                    "metadata": {"model_history_policy": "omit", "organizations": [{"name": "Old NGO"}]}}]
        with patch.object(router, "client", model):
            turn = router.plan_text_turn("Only the veterinary college number.", history,
                                        case_location={"place": "Palampur", "country_code": "in"})
        sent = json.loads(model.responses.create.call_args.kwargs["input"][1]["content"])
        self.assertIn("https://example.org", str(sent))
        self.assertEqual(sent["known_case_location"]["place"], "Palampur")
        self.assertEqual(turn.action, router.TextAction.SEARCH)

    def test_unstated_model_place_cannot_become_geocoder_input(self):
        model = MagicMock()
        model.responses.create.return_value = model_payload(
            location_kind="named_place", location_text="Palampur",
        )
        with patch.object(router, "client", model):
            turn = router.plan_text_turn("He may bite me if I slow down.")
        self.assertEqual((turn.location_kind, turn.location_text), ("none", ""))

    def test_router_failure_requests_model_fallback_without_place_guess(self):
        model = MagicMock()
        model.responses.create.side_effect = TimeoutError("offline")
        with patch.object(router, "client", model):
            turn = router.plan_text_turn("He may bite me if I slow down.")
        self.assertTrue(turn.routing_failed)
        self.assertEqual(turn.location_kind, "none")

    def test_missing_clarification_question_is_a_router_failure(self):
        model = MagicMock()
        model.responses.create.return_value = model_payload(action="clarify")
        with patch.object(router, "client", model):
            self.assertTrue(router.plan_text_turn("Help here").routing_failed)


class TestTextConversations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gaia-text-test-")
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "test.db"
        for target, attr, value in (
            (db, "DB_PATH", path), (config, "DB_PATH", path),
            (config, "CONVERSATION_COOKIE_SECURE", False),
        ):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)
        response = self.client.post("/v1/conversations")
        self.assertEqual(response.status_code, 200)
        self.session = response.json()["conversation_id"]
        for target, attr in (
            (web_search, "search_verified_india_local_help"),
            (db, "get_ngo_search_cache"), (db, "save_ngo_search_cache"),
            (app.ngo_followup, "answer_from_history"),
        ):
            if not hasattr(target, attr):
                continue
            patcher = patch.object(target, attr, side_effect=AssertionError("Legacy directory reuse"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, message, **kwargs):
        response = self.client.post("/v1/chat/query", json={
            "session_id": self.session, "message": message, **kwargs,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["response"]

    def test_complete_deb_conversation_preserves_correction_and_behavior(self):
        messages = [
            "Where can I get help for an animal in Palampur, Himachal Pradesh?",
            "Give me only the phone number for the Veterinary College in Palampur.",
            "What should I do if a dog is chasing me on my motor bike?",
            "He may bite me if I slow down.",
            "It happens every day at the same intersection. How can I make it stop?",
        ]
        location = region_scope.ScopeDecision(
            region_scope.INDIA, place="Palampur, Himachal Pradesh, India",
            country_code="in", city="Palampur", region="Himachal Pradesh",
            lat=32.11, lng=76.54, explicit_location=True,
        )
        turns = [
            planned("search", messages[0], "Palampur, Himachal Pradesh"),
            planned("search", messages[1], "Palampur"),
            planned("answer", messages[2]),
            planned("answer", "A motorbike rider fears the chasing dog may bite if they slow down."),
            planned("answer", "The community dog chases my motorbike every day at the same intersection."),
        ]
        sources = [
            web_search.SearchResult(response="A regional provider: confirm Palampur coverage.",
                                    searched=True, result_kind="search_answer"),
            web_search.SearchResult(response="01894-000000 [College source](https://example.edu/contact)",
                                    searched=True, result_kind="search_answer"),
        ]
        with patch.object(router, "plan_text_turn", side_effect=turns), patch.object(
            region_scope, "classify_text_scope", return_value=location
        ) as geocode, patch.object(
            web_search, "search_animal_question", side_effect=sources
        ) as search, patch.object(app.triage, "client", None):
            answers = [self.post(message) for message in messages]
        self.assertEqual(search.call_count, 2)
        self.assertEqual(geocode.call_count, 2)
        self.assertNotIn("Hello", answers[0])
        self.assertNotIn("regional provider", answers[1])
        self.assertIn("01894", answers[1])
        self.assertIn("Palampur", search.call_args.kwargs["resolved_place"])
        self.assertEqual(search.call_args.args[0], messages[1])
        self.assertIn(messages[0], str(search.call_args.args[1]))
        for answer in answers[2:]:
            self.assertNotIn("couldn't verify", answer)
            self.assertNotIn("How can I help you today", answer)
            self.assertIn("traffic", answer.lower())
        self.assertNotIn("wash the bite", answers[3].lower())
        rows = db.get_chat_history(self.session, limit=20)
        self.assertEqual(len(rows), 10)
        self.assertEqual([r["metadata"]["web_search_ran"] for r in rows if r["role"] == "assistant"],
                         [True, True, False, False, False])
        reload_response = self.client.get(f"/v1/conversations/{self.session}/messages")
        self.assertEqual(reload_response.status_code, 200)
        self.assertIn("College source", reload_response.text)

    def test_unresolved_named_institution_still_searches_original_request(self):
        question = "Veterinary college in Mysuru phone only"
        with patch.object(router, "plan_text_turn", return_value=planned("search", question, "Mysuru")), \
             patch.object(region_scope, "classify_text_scope", return_value=region_scope.ScopeDecision(
                 region_scope.AMBIGUOUS, place="Mysuru")), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response="I could not establish the college's phone from current sources.",
                 searched=True, result_kind="search_answer")) as search:
            answer = self.post(question)
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], question)
        self.assertNotIn("NGO", answer)

    def test_its_number_searches_with_previous_cited_target(self):
        prior = "A veterinary college in Mysuru [source](https://example.edu/clinic)."
        db.save_chat_message(self.session, "assistant", prior,
                             metadata={"resource_links": [{"label": "College", "url": "https://example.edu/clinic"}]})
        with patch.object(router, "plan_text_turn", return_value=planned("search", "Mysuru veterinary college phone")), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response="0821-000000 [source](https://example.edu/clinic)", searched=True,
                 result_kind="search_answer")) as search:
            self.post("Its number?")
        self.assertIn(prior, str(search.call_args.args[1]))
        self.assertEqual(search.call_args.kwargs["tool_choice"], "required")

    def test_router_outage_offers_model_optional_search(self):
        turn = router.TextTurn(source="model_unavailable", contextual_request="College phone")
        with patch.object(router, "plan_text_turn", return_value=turn), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response="I could not confirm this contact.", result_kind="model_answer")) as search:
            self.post("College phone")
        self.assertEqual(search.call_args.kwargs["tool_choice"], "auto")

    def test_router_outage_chase_followup_keeps_protected_context(self):
        db.save_chat_message(self.session, "user", "A dog is chasing me on my motorbike.")
        turn = router.TextTurn(source="model_unavailable")
        with patch.object(router, "plan_text_turn", return_value=turn), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response="Search is unavailable.", result_kind="unavailable")) as search, \
             patch.object(region_scope, "classify_text_scope") as scope:
            answer = self.post("He may bite me if I slow down.")
        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["tool_choice"], "auto")
        scope.assert_not_called()
        self.assertIn("traffic", answer.lower())
        self.assertNotIn("wash the bite", answer.lower())

    def test_search_failure_retains_real_injury_guidance(self):
        question = "A dog is bleeding in Palampur. Who can help?"
        with patch.object(router, "plan_text_turn", return_value=planned(
            "search", question, needs_immediate_guidance=True
        )), patch.object(web_search, "search_animal_question", side_effect=TimeoutError("offline")):
            answer = self.post(question)
        self.assertIn("pressure", answer.lower())
        self.assertNotIn("How can I help you today", answer)

    def test_outside_india_named_case_does_not_search(self):
        with patch.object(router, "plan_text_turn", return_value=planned(
            "search", "Animal help in San Jose, USA", "San Jose, USA"
        )), patch.object(region_scope, "classify_text_scope", return_value=region_scope.ScopeDecision(
            region_scope.OUTSIDE_INDIA, place="San Jose, USA", explicit_location=True,
        )), patch.object(web_search, "search_animal_question") as search:
            self.assertEqual(self.post("Animal help in San Jose, USA"), region_scope.INDIA_ONLY_RESPONSE)
        search.assert_not_called()

    def test_new_indian_place_overrides_old_outside_location(self):
        db.save_session_case_location(self.session, {"scope": region_scope.OUTSIDE_INDIA,
                                                    "place": "San Jose", "country_code": "us"})
        with patch.object(router, "plan_text_turn", return_value=planned(
            "search", "Animal help in Mysuru", "Mysuru"
        )), patch.object(region_scope, "classify_text_scope", return_value=region_scope.ScopeDecision(
            region_scope.INDIA, place="Mysuru, India", country_code="in", explicit_location=True,
        )), patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
            response="Mysuru local resources", searched=True, result_kind="search_answer",
        )) as search:
            self.assertIn("Mysuru", self.post("Animal help in Mysuru", lat=37.33, lng=-121.89))
        search.assert_called_once()

    def test_behavior_does_not_reuse_old_ambiguous_location(self):
        db.save_session_case_location(self.session, {"scope": region_scope.AMBIGUOUS,
                                                    "place": "He may bite me if I slow down"})
        with patch.object(router, "plan_text_turn", return_value=planned("answer", "Why do dogs wag their tails?")), \
             patch.object(region_scope, "classify_text_scope") as scope, \
             patch.object(app.triage, "generate_chat_response", return_value="Dogs use their tails to communicate."):
            answer = self.post("Why do dogs wag their tails?")
        scope.assert_not_called()
        self.assertIn("communicate", answer)

    def test_clarification_is_model_question_without_welcome(self):
        turn = router.TextTurn(action=router.TextAction.CLARIFY,
                               clarification_question="Which city is the animal in?")
        with patch.object(router, "plan_text_turn", return_value=turn), \
             patch.object(web_search, "search_animal_question") as search:
            self.assertEqual(self.post("Where can I get help?"), turn.clarification_question)
        search.assert_not_called()
        self.assertTrue(db.get_chat_history(self.session)[-1]["metadata"]["awaiting_clarification"])


if __name__ == "__main__":
    unittest.main()
