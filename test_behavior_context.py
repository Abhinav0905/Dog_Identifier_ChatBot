"""Offline regressions for humane encounter advice and conversation context."""

import unittest
from unittest.mock import MagicMock, patch

from services import guardrails, query_router, triage


CHASE_QUESTION = "What should I do if a dog is chasing me on my motor bike"


class TestBehaviorConversation(unittest.TestCase):
    def test_model_paraphrase_cannot_drop_recent_motorbike_context(self):
        history = [
            {"role": "user", "content": CHASE_QUESTION},
            {"role": "user", "content": "He may bite me if I slow down"},
        ]
        current = "It happens every day at the same intersection. How can I make it stop"
        contextual = "How can I stop a dog chasing me every day at the same intersection?"
        model = MagicMock()
        with (
            patch.object(triage, "client", model),
            patch("services.rag.retrieve") as retrieve,
        ):
            answer = triage.generate_chat_response(
                current, history, "incomplete-paraphrase", contextual_message=contextual
            )
        self.assertIn("Road safety comes first", answer)
        self.assertIn("Do not brake suddenly", answer)
        self.assertIn("Do not assume these community dogs have an owner", answer)
        self.assertIn("regular feeders", answer)
        model.responses.create.assert_not_called()
        retrieve.assert_not_called()

    def test_router_outage_current_message_does_not_override_recovered_context(self):
        history = [{"role": "user", "content": CHASE_QUESTION}]
        current = "He may bite me if I slow down"
        router_model = MagicMock()
        router_model.responses.create.side_effect = RuntimeError("offline")
        with patch.object(query_router, "client", router_model):
            turn = query_router.plan_text_turn(current, history)
        self.assertTrue(turn.routing_failed)
        self.assertEqual(turn.contextual_request, current)
        protected = triage.immediate_safety_response(current, history, turn.contextual_request)
        self.assertIsNotNone(protected)
        self.assertIn("Road safety comes first", protected)
        with patch.object(triage, "client", None):
            answer = triage.generate_chat_response(
                current, history, "router-outage", contextual_message=turn.contextual_request
            )
        self.assertEqual(answer, protected)
        self.assertIn("does not guarantee", answer)

    def test_screenshot_followups_keep_riding_and_community_context_during_outage(self):
        history = [
            {"role": "user", "content": "Where can I get animal help in Palampur?"},
            {
                "role": "assistant",
                "content": "Verified animal hospital contact.",
                "metadata": {"model_history_policy": "omit"},
            },
        ]
        questions = [
            CHASE_QUESTION,
            "hmm.. He may bite me if I slow down",
            "I said he may bite me if I slow down",
            "It happens every day at the same intersection. How can I make it stop",
            "These are street dogs living in the area",
        ]
        unavailable = MagicMock()
        unavailable.responses.create.side_effect = RuntimeError("offline")
        with (
            patch.object(triage, "client", unavailable),
            patch("services.rag.retrieve") as retrieve,
        ):
            for question in questions:
                with self.subTest(question=question):
                    answer = triage.generate_chat_response(question, history, "behavior")
                    self.assertIn("Road safety comes first", answer)
                    self.assertIn("Do not brake suddenly", answer)
                    self.assertIn("Do not throw stones", answer)
                    self.assertIn("Do not use a horn, whistle", answer)
                    self.assertNotIn("How can I help you today?", answer)
                    self.assertNotIn("What to do after a dog bite", answer)
                    self.assertFalse(guardrails.has_unsafe_behavior_advice(answer))
                    if "may bite" in question:
                        self.assertIn("does not guarantee", answer)
                    history.extend([
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": answer},
                    ])
        self.assertIn("nearby residents", answer)
        self.assertIn("Do not assume these community dogs have an owner", answer)
        self.assertIn("regular feeders", answer)
        retrieve.assert_not_called()
        unavailable.responses.create.assert_not_called()

    def test_actual_bite_takes_priority_over_previous_chase(self):
        history = [{"role": "user", "content": CHASE_QUESTION}]
        with patch.object(triage, "client", None):
            answer = triage.generate_chat_response("He bit me just now", history, "bite")
        self.assertIn("Wash the bite", answer)
        self.assertIn("medical attention the same day", answer)
        self.assertIn("rabies PEP", answer)
        self.assertNotIn("passing through", answer)

    def test_model_context_cannot_invent_actual_bite(self):
        with patch.object(triage, "client", None):
            answer = triage.generate_chat_response(
                "He may bite me if I slow down",
                [],
                "bite-fear",
                contextual_message="The user was bitten by a dog and needs care.",
            )
        self.assertIn("does not mean you have already been bitten", answer)
        self.assertNotIn("Wash the bite", answer)

    def test_clarified_chase_request_works_without_raw_history(self):
        with patch.object(triage, "client", None):
            answer = triage.generate_chat_response(
                "It keeps happening there every day",
                [],
                "clarified",
                contextual_message="A street dog chases my scooter every day at the same spot.",
            )
        self.assertIn("Road safety comes first", answer)
        self.assertIn("regular feeders", answer)

    def test_new_food_topic_does_not_reuse_chase_guidance(self):
        history = [{"role": "user", "content": CHASE_QUESTION}]
        with patch.object(triage, "client", None):
            answer = triage.generate_chat_response(
                "What food is safe for community dogs?", history, "new-topic"
            )
        self.assertIn("Safe food for community dogs", answer)
        self.assertNotIn("Road safety", answer)

    def test_unrelated_intervening_topic_stops_old_chase_context(self):
        history = [
            {"role": "user", "content": CHASE_QUESTION},
            {"role": "user", "content": "How often should an adult dog sleep?"},
        ]
        self.assertIsNone(triage.immediate_safety_response("It happens every day", history))

    def test_playful_chasing_does_not_trigger_human_encounter_instructions(self):
        self.assertIsNone(triage.immediate_safety_response("Why does my dog chase its tail?"))

    def test_contact_request_has_no_care_welcome_or_protected_response(self):
        self.assertIsNone(triage.immediate_safety_response(
            "Where can I get help for an animal in Palampur?"
        ))
        self.assertIsNone(triage.immediate_safety_response(
            "Give me the veterinary college number",
            [{"role": "user", "content": CHASE_QUESTION}],
        ))


class TestHumaneOutputValidation(unittest.TestCase):
    def test_affirmative_deterrents_are_removed_from_mocked_model_output(self):
        samples = [
            "Toss a small object like a stick or a stone in the opposite direction to distract the dog.",
            "Use your horn: a short beep might startle the dog and distract it.",
            "Blow a whistle to deter it.",
            "Do not throw stones, but use a whistle to scare it away.",
            "Avoid shouting, use your horn instead.",
        ]
        for unsafe in samples:
            with self.subTest(unsafe=unsafe):
                model = MagicMock()
                model.responses.create.return_value = MagicMock(
                    output_text="Keep a safe distance.\n" + unsafe
                )
                with (
                    patch.object(triage, "client", model),
                    patch("services.rag.retrieve", return_value=[]),
                ):
                    answer = triage.generate_chat_response(
                        "How can I deal with noisy street dogs?", [], "unsafe-model"
                    )
                model.responses.create.assert_called_once()
                self.assertNotIn(unsafe, answer)
                self.assertIn("Keep a safe distance", answer)
                self.assertIn("Do not throw stones or sticks", answer)
                self.assertFalse(guardrails.has_unsafe_behavior_advice(answer))

    def test_safe_negated_advice_and_citations_are_preserved(self):
        samples = [
            "Do not throw stones or sticks. Never use a horn or whistle to startle a dog.",
            "Avoid honking, shouting or throwing stones.",
            "Throwing stones is dangerous. Do not rev the engine.",
            "Do not throw stones, sticks, or other objects at the dog.",
        ]
        for safe in samples:
            with self.subTest(safe=safe):
                self.assertFalse(guardrails.has_unsafe_behavior_advice(safe))
                self.assertEqual(guardrails.sanitize_text_response(safe), safe)

    def test_text_sanitizer_keeps_public_institution_names_and_sources(self):
        answer = (
            "SPCA and municipal animal services can discuss local options.\n"
            "[Veterinary College](https://example.org/college) — phone 01894 123456."
        )
        self.assertEqual(guardrails.sanitize_text_response(answer), answer)
        self.assertNotEqual(guardrails.sanitize_response(answer), answer)


if __name__ == "__main__":
    unittest.main()
