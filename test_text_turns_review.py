"""Regressions for location reuse and search outages found during review."""

import unittest
from unittest.mock import patch

import app
import database as db
import test_text_turns as fixtures
from services import query_router as router, region_scope, web_search


class TestTextTurnReview(unittest.TestCase):
    # Reuse only the isolated-database setup and request helper, not the other
    # TestCase's tests (which would otherwise be discovered and run twice).
    setUp = fixtures.TestTextConversations.setUp
    post = fixtures.TestTextConversations.post

    def test_literal_selected_place_survives_geocoder_rename_and_followup(self):
        question = "Only the veterinary college number in Mhow, Madhya Pradesh."
        canonical = region_scope.ScopeDecision(
            region_scope.INDIA, place="Dr. Ambedkar Nagar, Indore, Madhya Pradesh, India",
            country_code="in", explicit_location=True,
        )
        result = web_search.SearchResult(response="07324-276622", searched=True, result_kind="search_answer")
        with patch.object(router, "plan_text_turn", return_value=fixtures.planned("search", question, "Mhow, Madhya Pradesh")), \
             patch.object(region_scope, "classify_text_scope", return_value=canonical), \
             patch.object(web_search, "search_animal_question", return_value=result) as search:
            self.post(question)
        self.assertEqual(search.call_args.kwargs["requested_location_text"], "Mhow, Madhya Pradesh")
        with patch.object(router, "plan_text_turn", return_value=fixtures.planned("search", "Only its number again.")), \
             patch.object(web_search, "search_animal_question", return_value=result) as followup:
            self.post("Only its number again.")
        self.assertEqual(followup.call_args.kwargs["requested_location_text"], "Mhow, Madhya Pradesh")
        self.assertEqual(followup.call_args.kwargs["resolved_place"], canonical.place)

    def test_new_case_without_location_cannot_reuse_literal_old_city(self):
        place = "Mhow, Madhya Pradesh, India"
        db.save_session_case_location(self.session, {"scope": region_scope.INDIA, "place": place})
        db.save_chat_message(self.session, "assistant", "An older college contact.", metadata={
            "case_place": place, "requested_location_text": "Mhow, Madhya Pradesh",
        })
        with patch.object(router, "plan_text_turn", return_value=fixtures.planned("search", "New case: find the clinic.", new_case=True)), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(response="I need to establish that clinic.")) as search:
            self.post("New case: find the clinic.")
        self.assertEqual(search.call_args.kwargs["requested_location_text"], "")
        self.assertIsNone(db.get_session_case_location(self.session))

    def test_explicit_number_only_request_keeps_its_format_during_router_outage(self):
        question = "A dog is bleeding; give only the veterinary college's number."
        with patch.object(router, "plan_text_turn", return_value=router.TextTurn(source="model_unavailable")), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(response="07324-276622", searched=True, result_kind="search_answer")):
            answer = self.post(question)
        self.assertEqual(answer, "07324-276622")
        self.assertTrue(db.get_chat_history(self.session)[-1]["metadata"]["phone_only"])

    def test_stale_outside_location_does_not_block_new_general_web_question(self):
        db.save_session_case_location(self.session, {
            "scope": region_scope.OUTSIDE_INDIA,
            "place": "San Jose, USA", "country_code": "us",
        })
        question = "Look up WHO guidance on preventing dog bites."
        answer = "WHO recommends avoiding contact with unfamiliar dogs."
        with patch.object(router, "plan_text_turn", return_value=fixtures.planned("search", question)), \
             patch.object(region_scope, "classify_text_scope") as geocode, \
             patch.object(app.triage, "client", None), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response=answer, searched=True, result_kind="search_answer",
             )) as search:
            response = self.post(question)
        geocode.assert_not_called()
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], question)
        self.assertEqual(response, answer)
        self.assertNotEqual(response, region_scope.INDIA_ONLY_RESPONSE)
        self.assertTrue(db.get_chat_history(self.session)[-1]["metadata"]["web_search_ran"])

    def test_router_outage_keeps_injury_guidance_and_requested_contact_search(self):
        question = "A dog is bleeding in Palampur; find the veterinary college phone number."
        turn = router.TextTurn(contextual_request=question, source="model_unavailable")
        with patch.object(router, "plan_text_turn", return_value=turn), \
             patch.object(region_scope, "classify_text_scope") as geocode, \
             patch.object(app.triage, "client", None), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response="College clinical contact: 01894-230123 [source](https://example.edu/clinic).",
                 searched=True, result_kind="search_answer",
             )) as search:
            answer = self.post(question)
        geocode.assert_not_called()
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], question)
        self.assertEqual(search.call_args.kwargs["tool_choice"], "auto")
        self.assertIn("pressure", answer.lower())
        self.assertIn("01894-230123", answer)
        self.assertIn("https://example.edu/clinic", answer)
        self.assertTrue(db.get_chat_history(self.session)[-1]["metadata"]["web_search_ran"])

    def test_college_search_exception_uses_neutral_failure_without_ngo_redirect(self):
        question = "Give me only the veterinary college phone number."
        with patch.object(router, "plan_text_turn", return_value=fixtures.planned("search", question)), \
             patch.object(region_scope, "classify_text_scope") as geocode, \
             patch.object(app.triage, "client", None), \
             patch.object(web_search, "search_animal_question", side_effect=TimeoutError("offline")) as search:
            answer = self.post(question)
        geocode.assert_not_called()
        search.assert_called_once()
        self.assertIn("could not", answer.lower())
        self.assertNotIn("NGO", answer)
        self.assertNotIn("contact a local", answer.lower())
        self.assertNotIn("offline", answer)
        row = db.get_chat_history(self.session)[-1]
        self.assertEqual(row["metadata"]["result_kind"], "unavailable")
        self.assertFalse(row["metadata"]["web_search_ran"])

    def test_router_outage_preserves_valid_browser_location_for_nearby_search(self):
        question = "Find a veterinary clinic near me."
        turn = router.TextTurn(contextual_request=question, source="model_unavailable")
        with patch.object(router, "plan_text_turn", return_value=turn), \
             patch.object(region_scope, "classify_text_scope") as geocode, \
             patch.object(app.triage, "client", None), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(
                 response="I will use the shared location to look for animal care.",
                 searched=True, result_kind="search_answer",
             )) as search:
            self.post(question, lat=32.11, lng=76.54)
        geocode.assert_not_called()
        self.assertEqual(search.call_args.kwargs["lat"], 32.11)
        self.assertEqual(search.call_args.kwargs["lng"], 76.54)
        self.assertEqual(search.call_args.kwargs["tool_choice"], "auto")
        self.assertEqual(search.call_args.kwargs["resolved_country_code"], "")

    def test_browser_context_fallback_respects_validity_and_existing_geography(self):
        turn = router.TextTurn(source="model_unavailable")
        # Bypass request validation only to exercise the defensive internal
        # coordinate check; the normal API rejects this latitude beforehand.
        invalid = app.ChatQueryRequest.model_construct(message="Find help nearby", lat=91, lng=76.54)
        scope = app._text_turn_scope(turn, invalid, None)
        self.assertIsNone(scope.lat)
        self.assertIsNone(scope.lng)

        request = app.ChatQueryRequest(message="Find help in Mysuru", lat=32.11, lng=76.54)
        saved = {"scope": region_scope.INDIA, "place": "Mysuru, India",
                 "country_code": "in", "lat": 12.3, "lng": 76.65}
        scope = app._text_turn_scope(turn, request, saved)
        self.assertEqual((scope.lat, scope.lng), (12.3, 76.65))
        self.assertEqual(scope.source, "session_case")

        with patch.object(region_scope, "classify_text_scope", return_value=region_scope.ScopeDecision(
            region_scope.AMBIGUOUS, place="Mysuru", explicit_location=True,
        )):
            scope = app._text_turn_scope(fixtures.planned("search", request.message, "Mysuru"), request, None)
        self.assertIsNone(scope.lat)
        self.assertIsNone(scope.lng)
        self.assertEqual(scope.place, "Mysuru")


if __name__ == "__main__":
    unittest.main()
