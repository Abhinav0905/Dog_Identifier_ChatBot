"""Network-free coverage for city-specific rescue routing across India.

These tests use explicit model plans and generic search results. They verify
that any Indian city can flow through open animal-help search while preserving
immediate care and geography, without freezing organisations or contact numbers.
"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app
from services.web_search import SearchResult
from services.query_router import TextAction, TextTurn


CITIES = (
    ("Srinagar", "Jammu and Kashmir", 34.0837, 74.7973),
    ("Shimla", "Himachal Pradesh", 31.1048, 77.1734),
    ("Chandigarh", "Chandigarh", 30.7333, 76.7794),
    ("New Delhi", "Delhi", 28.6139, 77.2090),
    ("Jaipur", "Rajasthan", 26.9124, 75.7873),
    ("Lucknow", "Uttar Pradesh", 26.8467, 80.9462),
    ("Patna", "Bihar", 25.5941, 85.1376),
    ("Guwahati", "Assam", 26.1445, 91.7362),
    ("Kolkata", "West Bengal", 22.5726, 88.3639),
    ("Ranchi", "Jharkhand", 23.3441, 85.3096),
    ("Bhubaneswar", "Odisha", 20.2961, 85.8245),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777),
    ("Pune", "Maharashtra", 18.5204, 73.8567),
    ("Ahmedabad", "Gujarat", 23.0225, 72.5714),
    ("Bhopal", "Madhya Pradesh", 23.2599, 77.4126),
    ("Hyderabad", "Telangana", 17.3850, 78.4867),
    ("Bengaluru", "Karnataka", 12.9716, 77.5946),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707),
    ("Kochi", "Kerala", 9.9312, 76.2673),
    ("Thiruvananthapuram", "Kerala", 8.5241, 76.9366),
)

PHRASINGS = (
    (
        "explicit_contact",
        "I found an injured dog in {city}. Which local animal-rescue NGO can I contact?",
    ),
    ("plain_bleeding", "A dog is bleeding in {city}. Please help."),
    (
        "plain_car_hit",
        "A dog in {city} has been hit by a car. What should I do?",
    ),
)

PLAIN_EMERGENCY_KINDS = {"plain_bleeding", "plain_car_hit"}
FORBIDDEN_DIRECTORY_WORDS = re.compile(
    r"\b(?:ngo|ngos|rescue|organization|organizations|organisation|organisations)\b",
    re.IGNORECASE,
)


class TestIndiaCityRescueRouting(unittest.TestCase):
    """Exercise 20 cities and three independently routed phrasings per city."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app.app)

    def _resolution(self, city, state, lat, lng):
        return app.region_scope.place_resolver.PlaceResolution(
            app.region_scope.INDIA,
            display_name=f"{city}, {state}, India",
            lat=lat,
            lng=lng,
            country_code="in",
            city=city,
            region=state,
        )

    def test_model_search_plans_preserve_each_named_city_and_emergency_context(self):
        for city, _state, _lat, _lng in CITIES:
            for kind, template in PHRASINGS:
                message = template.format(city=city)
                with self.subTest(city=city, phrasing=kind):
                    if kind in PLAIN_EMERGENCY_KINDS:
                        self.assertIsNone(FORBIDDEN_DIRECTORY_WORDS.search(message))
                    client = MagicMock()
                    client.responses.create.return_value.output_text = json.dumps({
                        "action": "search", "contextual_request": message,
                        "location_kind": "named_place", "location_text": city,
                        "needs_immediate_guidance": True, "clarification_question": "",
                    })
                    with patch.object(app.query_router, "client", client):
                        turn = app.query_router.plan_text_turn(message, [])
                    self.assertEqual(turn.action, TextAction.SEARCH)
                    self.assertEqual(turn.location_kind, "named_place")
                    self.assertEqual(turn.location_text, city)
                    self.assertTrue(turn.needs_immediate_guidance)
                    self.assertEqual(turn.contextual_request, message)

    def test_chat_endpoint_searches_each_named_city_for_every_phrasing(self):
        guidance = "Immediate safety guidance."
        result = SearchResult(response="Current local animal-help options.", searched=True, result_kind="search_answer")
        with patch.object(app, "_resolve_request_session", return_value="city-routing-test"), \
             patch.object(app.db, "get_chat_history", return_value=[]), \
             patch.object(app.db, "get_session_case_location", return_value=None), \
             patch.object(app.db, "save_session_case_location"), \
             patch.object(app.db, "save_chat_message"), \
             patch.object(app.triage, "immediate_safety_response", return_value=guidance), \
             patch.object(app.triage, "generate_chat_response") as chat, \
             patch.object(app.web_search, "search_verified_india_local_help") as legacy, \
             patch.object(app.db, "get_ngo_search_cache") as cache, \
             patch.object(app.region_scope.place_resolver, "resolve_service_city") as city_resolver:
            for city, state, lat, lng in CITIES:
                for kind, template in PHRASINGS:
                    message = template.format(city=city)
                    turn = TextTurn(
                        action=TextAction.SEARCH, contextual_request=message,
                        location_kind="named_place", location_text=city,
                        needs_immediate_guidance=True,
                    )
                    with self.subTest(city=city, phrasing=kind), \
                         patch.object(app.query_router, "plan_text_turn", return_value=turn), \
                         patch.object(app.region_scope.place_resolver, "resolve_named_place", return_value=self._resolution(city, state, lat, lng)) as resolve_named, \
                         patch.object(app.web_search, "search_animal_question", return_value=result) as search:
                        response = self.client.post("/v1/chat/query", json={"message": message, "session_id": "city-routing-test"})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()["response"], f"{guidance}\n\n{result.response}")
                        resolve_named.assert_called_once_with(city, hint_lat=None, hint_lng=None)
                        search.assert_called_once()
                        self.assertEqual(search.call_args.args, (message, []))
                        kwargs = search.call_args.kwargs
                        self.assertEqual(kwargs["resolved_place"], f"{city}, {state}, India")
                        self.assertEqual(kwargs["resolved_country_code"], "in")
                        self.assertEqual(kwargs["tool_choice"], "required")
                        self.assertEqual(kwargs["contextual_request"], message)
                        self.assertAlmostEqual(kwargs["lat"], lat)
                        self.assertAlmostEqual(kwargs["lng"], lng)
        chat.assert_not_called()
        legacy.assert_not_called()
        cache.assert_not_called()
        city_resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
