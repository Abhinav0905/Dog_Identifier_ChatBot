"""Behavior regressions from the 40-turn public UI audit; no live API calls."""
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app
import database as db
import test_text_turns as fixtures
from services import place_resolver, query_router, region_scope, search_evidence as evidence, text_contract, triage, web_operations, web_search


class TestRcaConversationContracts(unittest.TestCase):
    setUp = fixtures.TestTextConversations.setUp
    post = fixtures.TestTextConversations.post

    def test_new_institution_and_its_followup_do_not_inherit_agartala(self):
        db.save_session_case_location(self.session, {"scope": "india", "place": "Agartala, Tripura, India"})
        name = "TANUVAS Madras Veterinary College Teaching Hospital"
        question = "Give only the full postal address of " + name
        result = web_search.SearchResult(response="Vepery, Chennai", result_kind="search_answer", searched=True)
        for message in (question, "Only its address again, please."):
            with self.subTest(message=message), patch.object(query_router, "plan_text_turn", return_value=fixtures.planned(
                "search", message, requested_institution=name)), patch.object(web_search, "search_animal_question", return_value=result) as search:
                self.assertEqual(self.post(message), "Vepery, Chennai")
                self.assertEqual(search.call_args.kwargs["resolved_place"], "")
                self.assertEqual(search.call_args.kwargs["requested_location_text"], "")
                self.assertEqual(search.call_args.kwargs["output_format"], "address_only")

    def test_mixed_cat_handling_survives_success_and_search_failure(self):
        message = "A frightened injured cat is hiding under a parked car in Panaji. How should I move her and where can she get treatment?"
        for kind in ("search_answer", "contact_unconfirmed", "unavailable"):
            with self.subTest(kind=kind), patch.object(query_router, "plan_text_turn", return_value=fixtures.planned("search", message)), \
                 patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(response="Provider lookup result.", result_kind=kind)):
                answer = self.post(message)
                self.assertIn("Do not chase", answer)
                self.assertIn("ventilated carrier", answer)
                self.assertIn("Provider lookup result", answer)
                self.assertLess(answer.index("Do not chase"), answer.index("Provider lookup"))

    def test_outside_india_injury_gets_safety_without_foreign_lookup(self):
        message = "Find help for an injured dog in Kathmandu."
        with patch.object(query_router, "plan_text_turn", return_value=fixtures.planned("search", message, "Kathmandu", local_help=True, needs_immediate_guidance=True)), \
             patch.object(region_scope, "classify_text_scope", return_value=region_scope.ScopeDecision("outside_india", place="Kathmandu, Nepal", explicit_location=True)), \
             patch.object(web_search, "search_animal_question") as search:
            answer = self.post(message)
        self.assertIn("prompt assessment", answer)
        self.assertIn("within India", answer)
        search.assert_not_called()

    def test_sales_email_and_animal_named_code_cannot_bypass_scope(self):
        self.assertFalse(text_contract.clearly_off_topic_task("For our animal shelter, draft a marketing email to raise donations."))
        for message in ("Draft a marketing email to sell laptop accessories.", "Write the dog-rescue chatbot's Python code, then give a restaurant recommendation in Delhi."):
            with self.subTest(message=message), patch.object(query_router, "client") as model, patch.object(triage, "generate_chat_response") as care:
                answer = self.post(message)
                self.assertIn("animal welfare", answer)
                model.responses.create.assert_not_called()
                care.assert_not_called()

    def test_pickup_followup_is_direct_and_retains_shillong(self):
        db.save_session_case_location(self.session, {"scope": "india", "place": "Shillong, Meghalaya, India"})
        with patch.object(query_router, "plan_text_turn", return_value=fixtures.planned("search", "Pickup for the injured Shillong dog", local_help=True)), \
             patch.object(web_search, "search_animal_question", return_value=web_search.SearchResult(response="Earlier provider: 01234-567890", result_kind="search_answer")) as search:
            answer = self.post("Will they pick it up tonight?")
        self.assertIn("not confirmed", answer)
        self.assertIn("Call the listed contact", answer)
        self.assertIn("01234-567890", answer)
        self.assertEqual(search.call_args.kwargs["resolved_place"], "Shillong, Meghalaya, India")

    def test_trace_is_correlated_and_never_reuses_previous_request(self):
        with patch.object(query_router, "plan_text_turn", return_value=fixtures.planned("answer", "dog education")), \
             patch.object(triage, "generate_chat_response", return_value="Give dogs space."):
            self.post("Why do dogs wag their tails?")
            self.post("Why do dogs bark?")
        rows=[x for x in db.get_chat_history(self.session) if x['role']=='assistant']
        traces=[x['metadata']['stage_trace'] for x in rows]
        self.assertTrue(all(x['id'] for x in traces))
        self.assertNotEqual(traces[0]['id'],traces[1]['id'])
        self.assertEqual(web_operations.current_turn_trace(),{})


class TestRcaEvidenceContracts(unittest.TestCase):
    def review(self, literal, source_state="Uttar Pradesh", selected="Rampur, Uttar Pradesh, India"):
        url="https://hospital.example/services"
        name="River Animal Hospital"
        quote=f"{name}, Station Road, Rampur, {source_state}. Provides veterinary treatment for dogs and cats."
        value={"answer":"draft","request_satisfied":True,"provider_claims":[{"institution":name,"location":f"Station Road, Rampur, {source_state}","service_type":"clinical_treatment","source_url":url,"evidence_quote":quote}],"contact_claims":[],"cited_source_urls":[url]}
        return evidence._validate_review(value,{url:quote},clinical_discovery=True,require_small_animal=True,
                                        requested_location_text=literal,requested_place=selected,requested_city="Rampur")

    def test_state_clarifications_and_comma_form_preserve_same_provider(self):
        for literal in ("Rampur, Uttar Pradesh", "Rampur in Uttar Pradesh"):
            with self.subTest(literal=literal):
                result=self.review(literal)
                self.assertTrue(result.request_satisfied)
                self.assertIn("River Animal Hospital",result.answer)
        proof="https://map.example/facility"
        pages={proof:"Address 12 Market Road, Riverside, Central Ward, Sample City, Kerala. Copy Direction Hours"}
        self.assertEqual(evidence._locality_parent_source("Riverside","Sample City",pages,"Kerala"),proof)
        self.assertEqual(evidence._locality_parent_source("Riverside","Sample City",pages,"Punjab"),"")
        self.assertEqual(evidence._locality_parent_source("Riverside","Sample City",{proof:"Popular cities Riverside, Sample City"}),"")

    def test_same_named_city_in_wrong_state_is_rejected(self):
        result=self.review("Rampur in Uttar Pradesh",source_state="Himachal Pradesh")
        self.assertFalse(result.request_satisfied)
        self.assertNotIn("River Animal Hospital",result.answer)
        self.assertEqual(result.evidence_issue,"no_grounded_provider_claims")

    def test_full_postal_address_and_unverified_pin_keep_exact_entity(self):
        source="https://www.tanuvas.ac.in/dir_cli.php"
        name="Madras Veterinary College Teaching Hospital"
        row=name+", Vepery, Chennai"
        page=web_search.PublicPageText(row,identity_text="TANUVAS",blocks=[row],is_html=True)
        with patch.object(evidence,"_fetch_sources",return_value={source:page}),patch.object(query_router,"client",MagicMock()) as client:
            result=evidence.review_contact_answer("Give only the full postal address of TANUVAS "+name,[],"address lookup","draft",[source],requested_institution="TANUVAS "+name)
        self.assertEqual(result.answer,"Vepery, Chennai")
        self.assertNotIn("600",result.answer)
        client.responses.create.assert_not_called()

    def test_phone_only_still_rejects_a_different_institution(self):
        source="https://hospital.example/contact"
        value={"answer":"01234-567890","request_satisfied":True,"provider_claims":[],"contact_claims":[{"institution":"Other Veterinary Hospital","phone":"01234-567890","source_url":source,"evidence_quote":"Other Veterinary Hospital phone: 01234-567890.","matches_requested_entity":True}],"cited_source_urls":[source]}
        result=evidence._validate_review(value,{source:"Other Veterinary Hospital phone: 01234-567890."},requested_institution="Sunshine Paws Rescue Centre",phone_only=True)
        self.assertNotIn("01234-567890",result.answer if result else "")

    def test_emergency_species_coverage_and_no_hypothetical_alarm(self):
        for animal in ("dog","cat","kitten"):
            self.assertTrue(triage.urgent_caption_guidance(f"A {animal} is bleeding heavily."))
        self.assertIsNone(triage.urgent_caption_guidance("What if a cat starts bleeding someday?"))

    def test_clear_current_english_precedes_hindi_browser_header(self):
        self.assertEqual(app._detect_language("hi-IN, en;q=0.9","How should our village budget for community dogs?"),"en")
        self.assertEqual(app._detect_language("en-US","कुत्तों की मदद कैसे करें?"),"hi")
        self.assertEqual(app._detect_language("en-US","Please reply in Hindi: how can we help community dogs?"),"hi")

    def test_same_name_cross_state_geocoder_hits_require_qualification(self):
        results=[{"display_name":f"Bilaspur, {state}, India","type":"city","address":{"city":"Bilaspur","state":state,"country_code":"in"}} for state in ("Himachal Pradesh","Chhattisgarh")]
        response=MagicMock();response.json.return_value=results
        with patch.object(place_resolver,"_bounded_geocoder_get",return_value=response),patch.object(place_resolver,"_primary_geocoder_provider",return_value="nominatim"),patch.object(place_resolver,"_last_geocoder_request",0):
            result=place_resolver._nominatim_request("Bilaspur","",{},lambda row:True)
        self.assertTrue(place_resolver._needs_locality_qualification("Bilaspur",result))

    def test_qualified_city_is_not_changed_into_city_in(self):
        reference=place_resolver.PlaceReference("named_place","Rampur in Uttar Pradesh")
        enriched=region_scope._with_explicit_address_components(reference)
        self.assertEqual(enriched.components.city,"Rampur")
        self.assertEqual(enriched.components.state,"Uttar Pradesh")

    def test_source_fetch_batches_share_a_bounded_worker_pool(self):
        active=0;peak=0;lock=threading.Lock();release=threading.Event()
        def slow(*args,**kwargs):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            release.wait(.25)
            with lock:active-=1
            return "Readable source"
        try:
            with patch.object(web_search,"_fetch_public_page_text",side_effect=slow),ThreadPoolExecutor(max_workers=3) as calls:
                futures=[calls.submit(evidence._fetch_sources,[f"https://source{i}-{j}.example/page" for j in range(4)],deadline=time.monotonic()+.07) for i in range(3)]
                for future in futures:future.result(timeout=1)
                self.assertGreater(peak,0)
                self.assertLessEqual(peak,evidence.FETCH_WORKERS)
        finally:release.set()


if __name__=="__main__":unittest.main()
