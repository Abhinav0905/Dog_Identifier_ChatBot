"""Offline India-web geography regressions; no model or geocoder requests.

Run: .venv/bin/python -B -m unittest test_web_region_release
The module isolates configuration/storage and rejects outbound connections.
"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class WebRegionReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="gaia-region-check-")
        cls.addClassCleanup(cls.temp.cleanup)
        env = patch.dict(os.environ, {
            "GAIA_ENV_FILE": "/dev/null", "OPENAI_API_KEY": "",
            "PINECONE_API_KEY": "", "LOCATIONIQ_API_KEY": "",
            "DB_PATH": str(Path(cls.temp.name) / "checks.db"),
            "STORAGE_DIR": str(Path(cls.temp.name) / "storage"),
        })
        env.start(); cls.addClassCleanup(env.stop)
        network = patch("socket.socket.connect", side_effect=AssertionError("Network disabled in region checks"))
        network.start(); cls.addClassCleanup(network.stop)
        from services import region_scope, place_resolver, query_router, location
        cls.scope, cls.places, cls.router, cls.location = region_scope, place_resolver, query_router, location
        enabled = patch.object(region_scope.config, "INDIA_ONLY_SCOPE_ENABLED", True)
        enabled.start(); cls.addClassCleanup(enabled.stop)

    def setUp(self):
        self.history = [{"role": "assistant", "content": "Which district and state is that place in?",
                         "metadata": {"awaiting_clarification": True}}]
        self.pending = {"scope": self.places.AMBIGUOUS, "place": "Rampur",
                        "source": "small_locality_needs_parent"}
        self.foreign = {"scope": self.places.OUTSIDE_INDIA, "place": "Pokhara, Nepal",
                        "country_code": "np", "lat": 28.2, "lng": 83.98}

    def reference(self, place):
        return self.places.PlaceReference(self.places.NAMED_PLACE, place)

    def unresolved(self, reference):
        return self.places.PlaceResolution(self.places.AMBIGUOUS, display_name=reference.place, source="unresolved")

    def test_single_clarification_qualifies_indian_locality_without_coordinates(self):
        with patch.object(self.places, "resolve_named_place", side_effect=self.unresolved) as resolve:
            result = self.scope.classify_text_scope(
                "Assam", self.history, case_location=self.pending, place_reference=self.reference("Assam"),
            )
        self.assertEqual(result.scope, self.places.INDIA)
        self.assertTrue(result.clarification_asked)
        self.assertIsNone(result.lat)
        self.assertEqual(resolve.call_args.args[0].place, "Rampur, Assam")
        self.assertEqual(resolve.call_args.kwargs, {})

    def test_explicit_correction_replaces_pending_location_and_browser_bias(self):
        with patch.object(self.places, "resolve_named_place", side_effect=self.unresolved) as resolve:
            result = self.scope.classify_text_scope(
                "Actually, Kochi, Kerala", self.history, lat=32.2, lng=76.3,
                case_location=self.pending, place_reference=self.reference("Kochi, Kerala"),
            )
        self.assertEqual((result.scope, result.place), (self.places.INDIA, "Kochi, Kerala"))
        self.assertFalse(result.clarification_asked)
        self.assertIsNone(result.lat)
        self.assertEqual(resolve.call_args.kwargs, {})

    def test_qualified_indian_place_survives_geocoder_timeout_without_fake_pin(self):
        with patch.object(self.places, "resolve_named_place", side_effect=TimeoutError):
            result = self.scope.classify_text_scope(
                "animal in Kochi, Kerala", place_reference=self.reference("Kochi, Kerala"),
            )
        self.assertEqual(result.scope, self.places.INDIA)
        self.assertEqual(result.source, "india_place_unresolved")
        self.assertIsNone(result.lat)
        self.assertIsNone(result.lng)

    def test_explicit_foreign_country_is_rejected_without_geocoder_network(self):
        result = self.scope.classify_text_scope(
            "dog in Pokhara, Nepal", place_reference=self.reference("Pokhara, Nepal"),
        )
        self.assertEqual(result.scope, self.places.OUTSIDE_INDIA)
        self.assertEqual(result.source, "explicit_foreign_country")

    def test_router_outage_preserves_current_location_correction(self):
        with patch.object(self.router, "client", None):
            turn = self.router.plan_text_turn("Actually, Kochi, Kerala", self.history, case_location=self.pending)
        self.assertEqual(turn.location_text, "Kochi, Kerala")
        self.assertEqual(turn.location_kind, "named_place")
        self.assertTrue(self.router._location_was_requested(self.history))

    def test_short_acknowledgment_is_not_a_place_clarification(self):
        reference = self.places.PlaceReference(self.places.NONE)
        self.assertEqual(self.scope._pending_location_clarification("okay", reference), "")

    def test_case_relative_nearby_keeps_foreign_animal_location(self):
        reference = self.places.PlaceReference(self.places.NEAR_ME)
        for message in ("Any nearby clinic for that dog?", "Any nearby clinic?", "Any clinic near me for that dog?"):
            with self.subTest(message=message):
                result = self.scope.classify_text_scope(
                    message, case_location=self.foreign, lat=19.1, lng=72.9, place_reference=reference,
                )
                self.assertEqual(result.scope, self.places.OUTSIDE_INDIA)
                self.assertEqual(result.place, "Pokhara, Nepal")

    def test_explicit_reporter_location_can_replace_foreign_case_for_new_search(self):
        with patch.object(self.location, "is_in_india", return_value=True), \
             patch.object(self.location, "is_in_dharamsala_region", return_value=False):
            result = self.scope.classify_text_scope(
                "Find a clinic near me at my current location", case_location=self.foreign,
                lat=19.1, lng=72.9, place_reference=self.places.PlaceReference(self.places.NEAR_ME),
            )
        self.assertEqual(result.scope, self.places.INDIA)
        self.assertEqual(result.source, "browser_near_me")


if __name__ == "__main__":
    unittest.main()
