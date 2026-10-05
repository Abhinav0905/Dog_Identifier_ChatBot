#!/usr/bin/env python3
"""Focused regressions for veterinary safety during model outages and normal care."""

import re
import unittest
from unittest.mock import ANY, MagicMock, patch

from services import triage


class TestVeterinarySafetyResponses(unittest.TestCase):
    def _deterministic_answer(self, query: str) -> str:
        with (
            patch.object(triage, "client", None),
            patch("services.rag.retrieve") as retrieve,
        ):
            answer = triage.generate_chat_response(query, [], "safety-test")

        retrieve.assert_not_called()
        self.assertNotRegex(answer, re.compile(r"https?://|www\.|\+91|\b\d{10}\b", re.I))
        return answer

    def test_human_dog_bite_outage_guidance_is_time_sensitive(self):
        answer = self._deterministic_answer(
            "A dog bit me. What should I do?"
        ).lower()

        self.assertIn("wash the bite", answer)
        self.assertIn("at least 15 minutes", answer)
        self.assertIn("medical attention the same day", answer)
        self.assertIn("rabies pep", answer)
        self.assertIn("do not wait for symptoms", answer)

    def test_bleeding_dog_uses_firm_continuous_pressure_and_escalates(self):
        answer = self._deterministic_answer(
            "A dog is bleeding in Manali. Please help."
        ).lower()

        self.assertTrue(answer.startswith("**control the bleeding now and arrange immediate veterinary care.**"))
        self.assertIn("firm, continuous, direct pressure", answer)
        self.assertIn("do not lift", answer)
        self.assertIn("add more clean layers on top", answer)
        self.assertIn("immediate veterinary care", answer)
        self.assertNotIn("gentle pressure", answer)

    def test_vehicle_trauma_is_an_emergency_even_with_visible_bleeding(self):
        answer = self._deterministic_answer(
            "A dog is bleeding and is hurt by a car in Dharmsala. Please help."
        ).lower()

        self.assertIn("needs emergency veterinary care", answer)
        self.assertIn("internal bleeding", answer)
        self.assertIn("may be hidden", answer)
        self.assertIn("do not make the dog walk", answer)
        self.assertIn("minimise movement of the head, neck, and spine", answer)
        self.assertIn("firm, continuous, direct pressure", answer)
        self.assertIn("add more layers on top", answer)
        self.assertNotIn("gentle pressure", answer)

    def test_community_dog_food_does_not_recommend_milk_as_a_default(self):
        answer = self._deterministic_answer(
            "What food is safe for community dogs?"
        ).lower()

        self.assertIn("complete and balanced", answer)
        self.assertIn("supplements, not a complete long-term diet", answer)
        self.assertIn("do not make milk a routine food", answer)
        self.assertIn("lactose", answer)
        self.assertIn("should not be soaked in milk", answer)
        self.assertIn("onion or garlic", answer)
        self.assertNotIn("chapati with milk", answer)

    def test_frightened_unfamiliar_dog_guidance_prioritises_distance(self):
        answer = self._deterministic_answer(
            "How should I approach a frightened dog?"
        ).lower()

        self.assertIn("safe distance", answer)
        self.assertIn("do not block the dog's escape route", answer)
        self.assertIn("do not crouch close", answer)
        self.assertIn("do not use food to lure", answer)
        self.assertIn("do not hand-feed", answer)
        self.assertIn("let the dog choose whether to approach", answer)
        self.assertNotIn("crouch down", answer)


class TestGeneralDogCareRag(unittest.TestCase):
    def test_model_outage_still_gives_distressed_dog_immediate_guidance(self):
        model_client = MagicMock()
        model_client.responses.create.side_effect = RuntimeError("rate limited")
        query = "I see a dog in Pune. He is very distress. What do I do?"

        with (
            patch.object(triage, "client", model_client),
            patch("services.rag.retrieve", return_value=[]),
        ):
            answer = triage.generate_chat_response(query, [], "outage-test")

        self.assertIn("**What to do now**", answer)
        self.assertIn("prompt assessment", answer)
        self.assertNotIn("How can I help you today?", answer)

    def test_contact_bearing_rag_preserves_care_and_masks_unverified_number(self):
        query = "How can I help a street dog in hot weather?"
        safe_chunk = {
            "title": "Hot weather care",
            "content": "Provide shade and clean water.",
        }
        contact_chunk = {
            "title": "Contact a rescue",
            "content": "Provide shade. Call Example Rescue at +91 99999 99999.",
        }
        model_client = MagicMock()
        model_client.responses.create.return_value = MagicMock(
            output_text="Provide shade and clean water."
        )

        with (
            patch.object(triage, "client", model_client),
            patch(
                "services.rag.retrieve", return_value=[safe_chunk, contact_chunk]
            ),
            patch(
                "services.rag.format_context",
                return_value="## KNOWLEDGE BASE\nProvide shade and clean water.",
            ) as format_context,
        ):
            triage.generate_chat_response(query, [], "contact-rag-test")

        format_context.assert_called_once()
        formatted_chunks = format_context.call_args.args[0]
        self.assertEqual(formatted_chunks[0], safe_chunk)
        self.assertIn("Provide shade.", formatted_chunks[1]["content"])
        self.assertNotIn("99999", str(formatted_chunks))
        model_input = model_client.responses.create.call_args.kwargs["input"]
        self.assertNotIn("99999", str(model_input))

    def test_sick_dog_not_eating_does_not_take_community_food_shortcut(self):
        query = "A sick community dog is not eating. What should I do?"
        model_client = MagicMock()
        model_client.responses.create.return_value = MagicMock(
            output_text="Loss of appetite with illness needs veterinary assessment."
        )

        with (
            patch.object(triage, "client", model_client),
            patch("services.rag.retrieve", return_value=[]) as retrieve,
        ):
            answer = triage.generate_chat_response(query, [], "appetite-test")

        self.assertEqual(
            answer, "Loss of appetite with illness needs veterinary assessment."
        )
        retrieve.assert_called_once_with(query, k=5, deadline=ANY)
        model_client.responses.create.assert_called_once()
        self.assertNotIn("Safe food for community dogs", answer)

    def test_vehicle_accident_prevention_does_not_take_trauma_shortcut(self):
        query = "How can I prevent street dogs from being hit by cars?"
        model_client = MagicMock()
        model_client.responses.create.return_value = MagicMock(
            output_text="Use calm traffic-safety and community measures."
        )

        with (
            patch.object(triage, "client", model_client),
            patch("services.rag.retrieve", return_value=[]) as retrieve,
        ):
            answer = triage.generate_chat_response(query, [], "prevention-test")

        self.assertEqual(answer, "Use calm traffic-safety and community measures.")
        retrieve.assert_called_once_with(query, k=5, deadline=ANY)
        model_client.responses.create.assert_called_once()
        self.assertNotIn("needs emergency veterinary care", answer.lower())

    def test_non_safety_question_uses_at_most_three_knowledge_chunks(self):
        query = "How often should an adult dog be fed?"
        chunks = [{"title": "Dog care", "content": f"Use regular meal times. Reference {i}."} for i in range(5)]
        model_client = MagicMock()
        model_client.responses.create.return_value = MagicMock(
            output_text="Adult dogs benefit from regular meal times."
        )

        with (
            patch.object(triage, "client", model_client),
            patch("services.rag.retrieve", return_value=chunks) as retrieve,
            patch(
                "services.rag.format_context",
                return_value="## KNOWLEDGE BASE\nUse regular meal times.",
            ) as format_context,
        ):
            answer = triage.generate_chat_response(query, [], "rag-test")

        self.assertEqual(answer, "Adult dogs benefit from regular meal times.")
        retrieve.assert_called_once_with(query, k=5, deadline=ANY)
        format_context.assert_called_once_with(chunks[:3])
        request = model_client.responses.create.call_args.kwargs
        self.assertIn("## KNOWLEDGE BASE", request["input"][0]["content"])

    def test_rag_failure_does_not_suppress_general_answer(self):
        model_client = MagicMock()
        model_client.responses.create.return_value = MagicMock(
            output_text="Keep fresh water available."
        )

        with (
            patch.object(triage, "client", model_client),
            patch("services.rag.retrieve", side_effect=RuntimeError("offline")),
        ):
            answer = triage.generate_chat_response(
                "How much water does a dog need?", [], "rag-fallback-test"
            )

        self.assertEqual(answer, "Keep fresh water available.")
        model_client.responses.create.assert_called_once()


class TestWitnessAndRidingOutputSafety(unittest.TestCase):
    RIDER_FOLLOWUP = "If I slow down there, I am afraid a dog will bite me. I cannot safely stop in the traffic. What should I do on tomorrow's ride?"
    EXPOSURE_FOOTER = "If there is an actual bite, scratch breaking skin, or saliva on broken skin/eyes/mouth, wash with soap and running water for 15 minutes and get medical care promptly."

    def _rider_history(self, prior_footer=True):
        return [
            {"role": "user", "content": "Street dogs chase my motorbike every day at a busy junction with heavy traffic."},
            {"role": "assistant", "content": "Prioritise traffic safety. Keep control of the bike." + ("\n\n" + self.EXPOSURE_FOOTER if prior_footer else "")},
        ]

    def test_qualified_rider_reply_loses_only_repeated_footer_without_extra_preface(self):
        body = ("That sounds scary. For tomorrow, prioritise traffic safety: avoid the junction if possible; "
                "otherwise keep a predictable line and speed that matches traffic, and do not brake suddenly or stop in the junction.\n"
                "If a dog starts chasing, continue to a safe place off the moving road, where you can fully stop, and only then pause.")
        answer = body + "\n" + self.EXPOSURE_FOOTER
        self.assertEqual(triage.validate_generated_care_response(
            answer, self.RIDER_FOLLOWUP, self._rider_history(),
        ), body)

    def test_first_time_hypothetical_footer_is_retained(self):
        answer = "Keep control of the bike in moving traffic.\n\n" + self.EXPOSURE_FOOTER
        self.assertEqual(triage.validate_generated_care_response(
            answer, self.RIDER_FOLLOWUP, self._rider_history(prior_footer=False),
        ), answer)

    def test_terminal_inline_reminder_does_not_erase_reporting_advice(self):
        body = ("That's scary. For tomorrow, avoid that junction if you can. If you must go through, "
                "ride predictably with traffic, without sudden slowing or stopping for the dog, "
                "and continue until you can get fully off the road safely.\n\n"
                "After the ride, report that exact junction for on-site assessment, because a daily "
                "chase at a busy crossing needs local action.")
        answer = body + " " + self.EXPOSURE_FOOTER
        self.assertEqual(triage.validate_generated_care_response(
            answer, self.RIDER_FOLLOWUP, self._rider_history(),
        ), body)

    def test_new_exposure_and_explicit_first_aid_keep_washing_and_medical_help(self):
        answer = "Wash the wound with soap and running water for at least 15 minutes. Get medical care promptly for a rabies assessment."
        for message in ("The dog just bit me and broke my skin.", "What should I do if it bites me?", "If it bites me, how should I wash the wound?"):
            with self.subTest(message=message):
                self.assertEqual(triage.validate_generated_care_response(
                    answer, message, self._rider_history(),
                ), answer)

    def test_repetition_cleanup_preserves_needed_traffic_correction(self):
        answer = "Stop and use your bike as a barrier.\n\n" + self.EXPOSURE_FOOTER
        result = triage.validate_generated_care_response(answer, self.RIDER_FOLLOWUP, self._rider_history())
        self.assertIn("only after you can safely leave moving traffic and stop", result)
        self.assertIn("Stop and use your bike as a barrier.", result)
        self.assertNotIn(self.EXPOSURE_FOOTER, result)

    def test_unknown_poison_handling_is_replaced_before_sanitizing(self):
        query = "Someone is trying to poison a community dog outside. How can I help safely?"
        unsafe_replies = (
            "If reachable without approaching the dog, use gloves/polythene/cardboard to remove or cover it.",
            "Take a photo/sample of the substance for the vet if safe.",
            "Collect samples of the bait using a plastic bag.",
        )
        for reply in unsafe_replies:
            with self.subTest(reply=reply):
                result = triage.validate_generated_care_response(reply, query)
                self.assertNotEqual(result, reply)
                self.assertIn("Do not touch, move, cover or collect", result)
                self.assertIn("photos from a distance", result)
                self.assertIn("local police", result)
                self.assertIn("urgent veterinary help", result)

    def test_photo_only_evidence_and_handling_prohibition_are_retained(self):
        reply = ("Do not touch, move, cover or collect suspected bait. "
                 "Take a photo of the substance from a safe distance. Contact local authorities.")
        self.assertFalse(triage._advises_unknown_substance_handling(reply))
        self.assertEqual(triage.validate_generated_care_response(
            reply, "Someone is poisoning a dog; how can I help safely?",
        ), reply)

    def test_dog_behavior_ending_is_not_a_rider_stopping_instruction(self):
        self.assertFalse(triage._needs_riding_qualification("Feeding is not a reliable way to stop chasing."))
        self.assertFalse(triage._needs_riding_qualification("A plan may help stop the dogs from chasing bikes."))
        self.assertTrue(triage._needs_riding_qualification("Stop and put your bike between you and the dog."))
        self.assertFalse(triage._needs_riding_qualification(
            "Only after safely leaving moving traffic, stop and use the bike as a barrier.",
        ))

    def test_delayed_household_wound_remedy_ambiguity_is_rejected(self):
        safe_actions = "घाव को साबुन और पानी से 15 मिनट धोएँ। तुरंत डॉक्टर के पास जाएँ।\n\n"
        ambiguous = safe_actions + "हल्दी बाद में भी श्रद्धा से रख सकते हैं, पर घाव पर अभी नहीं लगानी चाहिए।"
        self.assertFalse(triage._human_exposure_answer_is_safe(ambiguous))
        safe = safe_actions + "पूजा साथ में कर सकते हैं, पर इलाज में देर न करें। घाव पर हल्दी कभी भी न लगाएँ।"
        self.assertTrue(triage._human_exposure_answer_is_safe(safe))
        result = triage.validate_generated_care_response(
            ambiguous, "आज कुत्ते ने मुझे काटा और त्वचा टूट गई। दादी ने हल्दी और पूजा की सलाह दी है।", language="hi",
        )
        self.assertNotIn("हल्दी बाद में", result)
        self.assertIn("15 मिनट", result)
        self.assertIn("डॉक्टर", result)

    def test_animal_only_case_loses_only_unsolicited_human_footer(self):
        animal = "Go to a veterinarian now. Let your dog stay in the position that makes breathing easiest."
        footer = "If any person was also bitten, wash with soap and running water for 15 minutes and get prompt medical care."
        result = triage.validate_generated_care_response(
            animal + "\n\n" + footer, "A dog bit my dog. He is not bleeding but is struggling to breathe.",
        )
        self.assertEqual(result, animal)

    def test_real_simultaneous_human_exposure_keeps_human_guidance(self):
        answer = ("Take your dog to a vet immediately.\n\n"
                  "If any person was also bitten, wash with soap and running water for 15 minutes and get prompt medical care.")
        for query in (
            "A dog bit my dog and bit me.",
            "A dog bit my dog and me.",
            "A dog bit my dog; saliva got into my eyes.",
        ):
            with self.subTest(query=query):
                self.assertEqual(triage._remove_unrequested_human_exposure_footer(answer, query), answer)
        self.assertTrue(triage._human_bite_guidance_requested("A dog bit my dog and me."))


class TestHumaneBehaviourAndDonorPolicy(unittest.TestCase):
    GENERIC_INTAKE = (
        "Please tell me what happened, who is affected (person or animal), and your town/district/state in India "
        "if you need local help. If there is a bite, scratch breaking skin, or saliva in eyes/mouth/broken skin, "
        "wash with soap and running water for 15 minutes and get medical care now."
    )

    def test_current_rider_turn_reaches_model_unchanged_and_useful_answer_survives(self):
        query = ("I am being chased by a dog on my motobike every day in Jaipur. What can I do to stop it? "
                 "These are street dogs living at a busy junction, and traffic is heavy.")
        answer = "That sounds frightening. Keep control of the bike; do not stop in moving traffic. Plan a safer route before the next ride."
        model_client = MagicMock()
        model_client.responses.create.return_value.output_text = answer
        with (
            patch.object(triage, "client", model_client),
            patch("services.rag.retrieve", return_value=[{"content": "Give dogs space.", "title": "Humane care"}]),
            patch("services.rag.format_context", return_value="Reference: give dogs space."),
            patch.object(triage.web_operations, "record_event"),
        ):
            result = triage.generate_chat_response(
                query, [], "rider-input", contextual_message=query.replace("motobike", "motorbike"),
            )
        model_input = model_client.responses.create.call_args.kwargs["input"]
        self.assertEqual(model_input[-1], {"role": "user", "content": query})
        self.assertEqual([entry["role"] for entry in model_input], ["system", "user"])
        self.assertGreater(model_input[0]["content"].index("FINAL RESPONSE CONTRACT"), model_input[0]["content"].index("Reference: give dogs space."))
        self.assertEqual(result, answer)

    def test_generic_intake_does_not_replace_guidance_for_an_established_chase(self):
        query = "Street dogs chase my scooter every day at a busy crossing in heavy traffic. How can I stay safe?"
        with patch.object(triage.web_operations, "record_event") as record:
            result = triage.validate_generated_care_response(self.GENERIC_INTAKE, query)
        self.assertNotIn("tell me what happened", result.lower())
        self.assertIn("Road safety comes first", result)
        self.assertIn("moving traffic", result)
        record.assert_called_once_with("care:validation", "fallback", error_type="UnansweredEstablishedCase")
        # A genuine missing case and a useful, specific clarification are not
        # converted to a predetermined chase answer.
        self.assertEqual(triage.validate_generated_care_response(self.GENERIC_INTAKE, "I need help with a dog."), self.GENERIC_INTAKE)
        specific = "Give the dog space and walk calmly away when safe. Does the chasing occur only near its resting place?"
        self.assertEqual(triage.validate_generated_care_response(specific, "A dog keeps chasing me when I walk home."), specific)

    def test_rider_followup_stays_concise_after_a_failed_prior_answer(self):
        history = [
            {"role": "user", "content": "Street dogs chase my motobike at a busy junction in heavy traffic every day."},
            {"role": "assistant", "content": self.GENERIC_INTAKE},
        ]
        query = "If I slow down there, I am afraid a dog will bite me. I cannot safely stop in the traffic. What should I do on tomorrow's ride?"
        for raw in ("Keep riding; do not kick them.", self.GENERIC_INTAKE):
            with self.subTest(raw=raw), patch.object(triage.web_operations, "record_event"):
                result = triage.validate_generated_care_response(raw, query, history)
            self.assertIn("fear is understandable", result)
            self.assertIn("stopping in moving traffic is not a safe response", result)
            self.assertIn("Before the next trip", result)
            self.assertLess(len(result.split()), 110)
            self.assertNotRegex(result.lower(), r"kick|wash|15 minutes|1\.")

    def test_unsolicited_aversive_prohibition_keeps_urgent_traffic_guidance(self):
        result = triage.validate_generated_care_response(
            "Do not brake suddenly, swerve, or try to kick them. Keep a steady speed.",
            "Community dogs chase my motorbike in heavy traffic every day. What should I do?",
        )
        self.assertNotIn("kick", result.lower())
        self.assertIn("moving traffic", result)
        self.assertIn("traffic conditions", result)

    def test_explicit_tactic_question_retains_direct_humane_warning(self):
        answer = "Do not throw stones at the dog. Give it space and move calmly."
        self.assertEqual(triage.validate_generated_care_response(
            answer, "Should I throw stones when community dogs chase me?",
        ), answer)

    def test_prior_assistant_tactic_does_not_authorize_repetition_on_fear_followup(self):
        history = [
            {"role": "user", "content": "Dogs chase my motorbike every day."},
            {"role": "assistant", "content": "Road safety first; do not kick the dog or stop in moving traffic."},
        ]
        result = triage.validate_generated_care_response(
            "Do not kick them. Keep riding steadily.", "He may bite me if I slow down. I am afraid.", history,
        )
        self.assertNotIn("kick", result.lower())
        self.assertIn("understandable", result)
        self.assertNotIn("wash", result.lower())
        self.assertNotIn("1.", result)

    def test_chasing_cures_are_rejected_but_correct_distinctions_pass(self):
        context = "Community dogs chase me when I ride past."
        for claim in (
            "Humane long-term fix: get the dogs vaccinated and sterilised.",
            "Sterilisation will stop chasing.",
            "Once sterilised, the dogs will not chase again.",
        ):
            with self.subTest(claim=claim):
                self.assertTrue(triage._claims_chasing_cure(claim, context))
        self.assertFalse(triage._claims_chasing_cure(
            "Vaccination prevents rabies. Sterilisation is not a cure for chasing; assess the triggers with a behaviour professional.", context,
        ))

    def test_ungrounded_donor_ranking_becomes_conditional_plan(self):
        query = "I have ₹5000 to help community dogs. How should I prioritise food, vaccination and sterilisation?"
        result = triage.validate_generated_care_response(
            "Vaccination first, sterilisation second, food third. Put most of the budget into getting at least one dog sterilised.", query,
        )
        self.assertIn("unmet needs", result)
        self.assertIn("current vaccination", result)
        self.assertIn("food and water needs", result)
        self.assertIn("total quote", result)
        self.assertNotIn("at least one dog", result)

    def test_grounded_donor_allocation_is_preserved(self):
        query = ("My budget supports community dogs. Food and water needs are already met, current vaccination "
                 "and sterilisation status were checked, and the public vet confirmed the total cost is covered. What next?")
        answer = "Prioritise vaccination first for the dogs whose records show it is due, coordinating with the public vet."
        self.assertEqual(triage.validate_generated_care_response(answer, query), answer)

    def test_donor_outage_retains_real_emergency_before_budget_planning(self):
        with patch.object(triage, "client", None):
            answer = triage.generate_chat_response(
                "I have a budget for a community dog that has collapsed and cannot swallow. Should I spend it on food?", [], "donor-emergency",
            )
        self.assertIn("urgent veterinary", answer.lower())
        self.assertIn("Do not force food or water", answer)
        self.assertLess(answer.lower().index("urgent veterinary"), answer.index("Base the budget"))

    def test_hindi_unsolicited_tactic_uses_humane_language(self):
        self.assertTrue(triage.guardrails.has_unsolicited_aversive_mentions(
            "कुत्ते को लात न मारें।", "कुत्ते से सुरक्षित दूरी कैसे रखें?",
        ))
        answer = triage.validate_generated_care_response(
            "कुत्ते को लात न मारें।", "कुत्ते से सुरक्षित दूरी कैसे रखें?", language="hi",
        )
        self.assertNotIn("लात", answer)
        self.assertIn("शांत", answer)


if __name__ == "__main__":
    unittest.main()
