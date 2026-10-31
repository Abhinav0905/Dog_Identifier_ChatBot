"""Regressions for the 50-question trace audit; no live API calls."""
import unittest
from unittest.mock import patch
from services import search_evidence as evidence, text_contract, triage, web_search, query_router, region_scope
import test_search_evidence as source_fixtures
import test_text_turns as turn_fixtures


class TestEvidenceRetention(unittest.TestCase):
    setUp = source_fixtures.TestSearchEvidence.setUp
    result = source_fixtures.TestSearchEvidence.result

    def provider(self, name, place, quote, page, **kwargs):
        value = self.result(answer="Treatment option", claims=[], provider_claims=[{
            "institution": name, "location": place, "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": quote,
        }])
        return evidence._validate_review(value, {self.url: page}, clinical_discovery=True,
                                        require_small_animal=True, **kwargs)

    def test_city_alias_keeps_provider_but_rejects_wrong_state(self):
        requested = "Chhatrapati Sambhajinagar (Aurangabad)"
        self.assertTrue(text_contract.locality_matches(requested, "Aurangabad, Maharashtra", "Maharashtra"))
        self.assertTrue(text_contract.locality_matches(requested, "Chhatrapati Sambhajinagar, Maharashtra", "Maharashtra"))
        self.assertFalse(text_contract.locality_matches(requested, "Aurangabad, Bihar", "Maharashtra"))

    def test_cross_script_city_keeps_address(self):
        name = "Desert Veterinary Clinic"
        quote = name + " treats dogs and cats."
        place = "M P Colony, Bikaner, Rajasthan"
        page = web_search.PublicPageText(name + '\n' + place + '\n' + quote, blocks=[name, place, quote])
        result = self.provider(name, place, quote, page, requested_location_text="बीकानेर, राजस्थान",
                               requested_city="Bikaner", requested_place="Bikaner, Rajasthan, India")
        self.assertTrue(result.request_satisfied)
        self.assertIn(place, result.answer)
        self.assertTrue(text_contract.locality_matches("Bikaner", place, "Rajasthan"))
        self.assertFalse(text_contract.locality_matches("Bikaner", "Bikaner, MP 334001", "Rajasthan"))
        self.assertEqual(evidence._discovery_locality("Mhow, Madhya Pradesh", "Indore, Madhya Pradesh", "Indore"), "Mhow")

    def test_directory_service_and_own_address_can_be_separate(self):
        name = "River Veterinary Clinic"
        place = "12 River Road, Lakeside"
        quote = name + " provides treatment for dogs and cats."
        page = web_search.PublicPageText(name+'\n'+place+'\n'+quote, blocks=[name, place, quote])
        result = self.provider(name, place, quote, page, requested_location_text="Lakeside", address_required=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn(place, result.answer)
        self.assertIn("12 river road lakeside", [x['value'] for x in result.validated_facts])

    def test_separate_address_cannot_cross_into_another_provider(self):
        name = "River Veterinary Clinic"
        other_address = "12 Other Road, Lakeside"
        quote = name + " provides treatment for dogs and cats in Lakeside."
        page = web_search.PublicPageText(quote+'\nMoonlight Animal Hospital\n'+other_address,
                                        blocks=[quote, "Moonlight Animal Hospital", other_address])
        result = self.provider(name, other_address, quote, page, requested_location_text="Lakeside", address_required=True)
        self.assertFalse(result.request_satisfied)
        self.assertNotIn(other_address, result.answer)

    def test_published_clinic_category_is_a_lead_without_admission_promise(self):
        name = "River Care Centre"
        quote = name + ", 12 River Road, Lakeside. Category: Veterinary clinic."
        result = self.provider(name, "12 River Road, Lakeside", quote, quote, requested_location_text="Lakeside")
        self.assertTrue(result.request_satisfied)
        self.assertIn("Call ahead", result.answer)
        self.assertNotIn("clinical treatment access is unconfirmed", result.answer)

    def test_address_or_phone_does_not_require_both(self):
        name = "River Veterinary Clinic"
        quote = name + ", 12 River Road, Lakeside treats dogs and cats."
        self.fetch.return_value = quote
        self.result(answer="Treatment option", claims=[], provider_claims=[{
            "institution":name,"location":"12 River Road, Lakeside","service_type":"clinical_treatment",
            "source_url":self.url,"evidence_quote":quote}])
        result = evidence.review_contact_answer("Find treatment for a dog in Lakeside, with an address or phone number.",
                     [], "Find veterinary treatment in Lakeside with an address or phone number.", "draft", [self.url], requested_institution="", requested_place="Lakeside")
        self.assertTrue(result.request_satisfied)
        self.assertNotIn("phone number remains unconfirmed",result.answer)

    def test_full_address_keeps_locality_as_partial(self):
        name = "River Veterinary Clinic"
        row = name + ", Lakeside, Maharashtra"
        page = web_search.PublicPageText(row, blocks=[row])
        result = evidence._grounded_address_answer([], {self.url:page}, [self.url], name, "en", full_address_required=True)
        self.assertEqual(result.answer, "Lakeside, Maharashtra")
        self.assertFalse(result.request_satisfied)
        full = name + ", 12 River Road, Lakeside, Maharashtra 400001"
        result = evidence._grounded_address_answer([], {self.url:web_search.PublicPageText(full,blocks=[full])}, [self.url], name, "en", full_address_required=True)
        self.assertTrue(result.request_satisfied)

    def test_availability_does_not_invent_tonight_or_a_listed_phone(self):
        answer = triage.provider_availability_guidance("Can they pick it up?", "en", [])
        self.assertNotIn("tonight", answer)
        self.assertNotIn("listed contact", answer)
        self.assertIn("not confirmed", answer)
        answer = triage.provider_availability_guidance("Can they pick it up?", "en", [{"kind":"phone"}])
        self.assertIn("Call the listed contact", answer)


class TestInjuryGuidanceRetention(unittest.TestCase):
    setUp = turn_fixtures.TestTextConversations.setUp
    post = turn_fixtures.TestTextConversations.post

    def test_injured_kitten_gets_care_even_if_lookup_is_partial(self):
        message = "An injured kitten is in Bikaner. Where can I take her?"
        with patch.object(query_router, "plan_text_turn", return_value=turn_fixtures.planned("search",message,needs_immediate_guidance=True)), \
             patch.object(web_search,"search_animal_question",return_value=web_search.SearchResult(response="Provider details unconfirmed.",result_kind="contact_unconfirmed")):
            answer = self.post(message)
        self.assertIn("Keep people and traffic away from the cat", answer)
        self.assertIn("Provider details unconfirmed", answer)
        self.assertNotIn("the dog", answer)

    def test_injured_animal_gets_care_while_city_is_clarified(self):
        message = "There is an injured kitten in Rampur. Where can I get treatment?"
        with patch.object(query_router,"plan_text_turn",return_value=turn_fixtures.planned("clarify",message,"Rampur",needs_immediate_guidance=True,local_help=True,clarification_question="Which state?")), \
             patch.object(region_scope,"classify_text_scope",return_value=region_scope.ScopeDecision("ambiguous",place="Rampur",explicit_location=True)):
            answer = self.post(message)
        self.assertIn("prompt assessment",answer)
        self.assertIn("cat",answer)



class TestGeocoderTelemetry(unittest.TestCase):
    def test_queue_timeout_returns_unresolved_instead_of_name_error(self):
        from services import place_resolver as places
        with patch.object(places, '_primary_geocoder_provider', return_value='nominatim'), \
             patch.object(places, '_geocoder_slot', side_effect=TimeoutError('queue busy')), \
             patch.object(places.web_operations, 'record_event') as event:
            self.assertIsNone(places._nominatim_request('Gajuwaka, Visakhapatnam', '', {}, lambda x: True))
        self.assertEqual(event.call_args.args[:2], ('geocoder:nominatim', 'timeout'))

    def test_successful_autocomplete_does_not_lose_candidates_to_telemetry(self):
        from services import place_resolver as places
        from unittest.mock import MagicMock
        response=MagicMock(status_code=200)
        response.json.return_value=[{'name':'Gajuwaka'}]
        with patch.object(places, '_primary_geocoder_provider', return_value='locationiq'), \
             patch.object(places.config, 'LOCATIONIQ_API_KEY', 'fixture'), \
             patch.object(places, '_geocoder_slot'), \
             patch.object(places, '_bounded_geocoder_get', return_value=response), \
             patch.object(places.web_operations, 'record_event') as event:
            self.assertEqual(places._locationiq_autocomplete_search('Gajuwaka'), [{'name':'Gajuwaka'}])
        self.assertEqual(event.call_args.args[:2], ('geocoder:locationiq', 'ok'))

class TestPhoneNamePunctuation(unittest.TestCase):
    def test_punctuation_does_not_discard_the_same_hospitals_phone(self):
        name = "Veterinary Hospital Junglighat"
        row = "1) Veterinary Hospital, Junglighat: 03192-232252"
        page = web_search.PublicPageText(name+" provides care.\n"+row, blocks=[name+" provides care.",row])
        self.assertEqual(evidence._source_bound_excerpt(page,name,"03192-232252",row),row)
        self.assertFalse(evidence._claim_binds_institution(name,"03192-252823",row+"; Veterinary Hospital Garacharma: 03192-252823"))

if __name__ == '__main__':
    unittest.main()
