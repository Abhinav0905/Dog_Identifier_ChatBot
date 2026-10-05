"""Offline contact-evidence review with actual fetched-page fixtures."""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from services import query_router, search_evidence as evidence, web_search


class TestSearchEvidence(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.enterContext(patch.object(query_router, "client", self.client))
        self.enterContext(patch.object(evidence.web_operations, "record_event"))
        self.fetch = self.enterContext(patch.object(web_search, "_fetch_public_page_text"))
        self.url = "https://university.example/contacts"
        self.quote = "Madras Veterinary College Teaching Hospital clinical phone: 044-25381509."
        self.fetch.return_value = web_search.PublicPageText(self.quote)

    def result(self, *, answer="Clinical contact: 044-25381509.", claims=None, urls=None, request_satisfied=True, provider_claims=None):
        if claims is None:
            claims = [{
                "institution": "Madras Veterinary College Teaching Hospital",
                "phone": "044-25381509", "source_url": self.url,
                "evidence_quote": self.quote, "matches_requested_entity": True,
            }]
        value = {"answer": answer, "request_satisfied": request_satisfied, "contact_claims": claims,
                 "provider_claims": provider_claims or [],
                 "cited_source_urls": [self.url] if urls is None else urls}
        self.client.responses.create.return_value = MagicMock(output_text=json.dumps(value))
        return value

    def review(self, message="Give me only its number.", *, draft="044-25381509", history=None, urls=None):
        return evidence.review_contact_answer(
            message, history or [], "The veterinary college's clinical contact", draft,
            [self.url] if urls is None else urls,
        )

    def test_its_number_uses_prior_target_and_actual_fetched_source(self):
        self.result()
        history = [{"role": "assistant", "content": "Madras Veterinary College in Chennai.",
                    "metadata": {"resource_links": [{"label": "College", "url": self.url}]}}]
        result = self.review(history=history)
        self.assertEqual(result.status, "approved")
        self.assertEqual("044-25381509", result.answer)
        self.assertEqual(result.links[0]["url"], self.url)
        kwargs = self.client.responses.create.call_args.kwargs
        data = json.loads(kwargs["input"][1]["content"])
        self.assertEqual(data["current_message"], "Give me only its number.")
        self.assertIn("Madras Veterinary College", str(data["recent_conversation"]))
        self.assertEqual(data["fetched_sources"][0]["text"], self.quote)
        self.assertFalse(kwargs["store"])
        self.assertNotIn("tools", kwargs)

    def test_explicit_fictional_college_correction_does_not_reuse_old_contact(self):
        self.result(answer="I could not establish the requested fictional college's phone number.",
                    claims=[], urls=[])
        history = [{"role": "assistant", "content": "Madras Veterinary College: 044-25381509."}]
        result = self.review("I mean Moonlight Veterinary College in Chennai, not the previous college.",
                             history=history)
        self.assertFalse(result.request_satisfied)
        self.assertNotIn("25381509", result.answer)
        data = json.loads(self.client.responses.create.call_args.kwargs["input"][1]["content"])
        self.assertIn("Moonlight Veterinary College", data["current_message"])
        self.assertIn("Madras Veterinary College", str(data["recent_conversation"]))

    def test_false_entity_match_rejects_even_a_real_source_phone(self):
        value = self.result()
        value["contact_claims"][0]["matches_requested_entity"] = False
        self.client.responses.create.return_value.output_text = json.dumps(value)
        result = self.review("Moonlight Veterinary College phone only")
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("25381509", result.answer)

    def test_missing_draft_citation_or_phone_can_be_repaired_from_available_source(self):
        self.result()
        result = self.review(draft="I could not establish a phone number from the search snippets.")
        self.assertEqual(result.status, "approved")
        self.assertIn("044-25381509", result.answer)
        self.assertEqual("044-25381509", result.answer)
        self.assertEqual(self.url, result.links[0]["url"])

    def test_fabricated_quote_is_rejected_even_when_phone_is_real(self):
        value = self.result()
        value["contact_claims"][0]["evidence_quote"] = "Moonlight College phone 044-25381509."
        value["contact_claims"][0]["institution"] = "Moonlight College"
        self.client.responses.create.return_value.output_text = json.dumps(value)
        result = self.review()
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("25381509", result.answer)

    def test_phone_in_answer_must_match_the_actual_quote(self):
        self.result(answer="Clinical contact: 044-25381234.")
        result = self.review()
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("25381234", result.answer)

    def test_incomplete_model_quote_is_repaired_only_from_source_bound_passage(self):
        value = self.result()
        value["contact_claims"][0]["evidence_quote"] = "clinical phone: 044-25381509."
        self.client.responses.create.return_value.output_text = json.dumps(value)
        result = self.review()
        self.assertEqual(result.status, "approved")
        self.assertEqual(result.answer, "044-25381509")
        self.fetch.return_value = "Madras Veterinary College Teaching Hospital has no public clinical phone. Moonlight Veterinary Hospital clinical phone: 044-25381509."
        self.assertEqual(self.review().status, "unavailable")

    def test_unfetched_source_or_schema_injection_is_rejected(self):
        for change in ("source", "boolean", "extra_key", "url_in_answer"):
            with self.subTest(change=change):
                value = self.result()
                if change == "source":
                    value["cited_source_urls"] = ["https://attacker.example/fabricated"]
                    value["contact_claims"][0]["source_url"] = value["cited_source_urls"][0]
                elif change == "boolean":
                    value["contact_claims"][0]["matches_requested_entity"] = "true"
                elif change == "extra_key":
                    value["ignore_previous_instructions"] = True
                else:
                    value["answer"] += " https://attacker.example"
                self.client.responses.create.return_value.output_text = json.dumps(value)
                result = self.review()
                self.assertEqual(result.status, "unavailable")
                self.assertNotIn("attacker", result.answer)

    def test_unreadable_sources_return_honest_failure_without_model_call(self):
        self.fetch.return_value = None
        result = self.review()
        self.assertEqual(result.status, "unavailable")
        self.assertIn("accessible sources", result.answer)
        self.client.responses.create.assert_not_called()

    def test_some_unsupported_contacts_do_not_remove_supported_institution_information(self):
        self.result(answer="The source describes Madras Veterinary College's teaching hospital; its current phone could not be established.",
                    claims=[], provider_claims=[{
                        "institution": "Madras Veterinary College Teaching Hospital", "location": "",
                        "service_type": "identity_only", "source_url": self.url, "evidence_quote": self.quote,
                    }])
        result = self.review("Where can I find veterinary treatment in Chennai?")
        self.assertEqual(result.status, "approved")
        self.assertIn("Teaching Hospital", result.answer)
        self.assertFalse(result.request_satisfied)
        self.assertNotIn("25381509", result.answer)
        self.assertEqual(result.links[0]["url"], self.url)

    def test_non_contact_answer_is_untouched_and_never_fetched(self):
        answer = "The source describes humane dog behaviour guidance."
        result = evidence.review_contact_answer("Why do dogs chase bikes?", [], "Dog behaviour", answer, [self.url])
        self.assertEqual(result.status, "not_needed")
        self.assertEqual(result.answer, answer)
        self.fetch.assert_not_called()
        self.client.responses.create.assert_not_called()

    def test_phone_format_variants_and_short_helpline_require_evidence(self):
        self.quote = "Example Animal Hospital helpline phone: +91 1894 230123."
        self.fetch.return_value = self.quote
        self.result(answer="Clinical contact: 01894-230123.", claims=[{
            "institution": "Example Animal Hospital", "phone": "+91 1894 230123",
            "source_url": self.url, "evidence_quote": self.quote, "matches_requested_entity": True,
        }])
        self.assertEqual(self.review().status, "approved")
        self.quote = "Example Animal Helpline veterinary phone: 1962."
        self.fetch.return_value = self.quote
        self.result(answer="Call 1962.", claims=[{
            "institution": "Example Animal Helpline", "phone": "1962",
            "source_url": self.url, "evidence_quote": self.quote, "matches_requested_entity": True,
        }])
        self.assertEqual(self.review().status, "approved")
        self.result(answer="Call 112.", claims=[], urls=[])
        self.assertEqual(self.review().status, "unavailable")

    def test_country_code_plus_domestic_trunk_prefix_matches_local_format(self):
        for published, displayed in (
            ("+91-044-25381366", "044-25381366"),
            ("+91-01894-230327", "01894-230327"),
        ):
            with self.subTest(published=published):
                self.quote = f"Example Veterinary College clinical phone: {published}."
                self.fetch.return_value = self.quote
                self.result(answer=f"Clinical contact: {displayed}.", claims=[{
                    "institution": "Example Veterinary College", "phone": published,
                    "source_url": self.url, "evidence_quote": self.quote,
                    "matches_requested_entity": True,
                }])
                result = self.review()
                self.assertEqual(result.status, "approved")
                self.assertEqual(displayed, result.answer)
                self.assertEqual(self.url, result.links[0]["url"])

    def test_fetch_budget_limits_source_count_and_excludes_failed_pages(self):
        urls = [f"https://example.org/{index}" for index in range(evidence.MAX_SOURCES + 2)]
        self.fetch.side_effect = lambda url, *, deadline, allow_public_redirects: "Institution phone information" if url != urls[1] else None
        pages = evidence._fetch_sources(urls)
        self.assertEqual(self.fetch.call_count, evidence.MAX_SOURCES)
        self.assertEqual(list(pages), [url for url in urls[:evidence.MAX_SOURCES] if url != urls[1]])
        self.assertTrue(all("deadline" in call.kwargs for call in self.fetch.call_args_list))

    def test_request_satisfied_is_required_and_strictly_boolean(self):
        for bad in ("true", 1, None):
            with self.subTest(value=bad):
                value = self.result(request_satisfied=bad)
                self.assertIsNone(evidence._validate_review(value, {self.url: self.quote}))
        value = self.result()
        del value["request_satisfied"]
        self.assertIsNone(evidence._validate_review(value, {self.url: self.quote}))

    def test_multiple_phone_claim_with_ellipsis_uses_actual_clinical_source(self):
        self.quote = "Harbour Veterinary Hospital, Lakeside. Clinical phone: 044-25381509 / 044-25381234."
        self.fetch.return_value = web_search.PublicPageText(self.quote)
        self.result(answer="044-25381509 / 044-25381234", claims=[{
            "institution": "Harbour Veterinary Hospital", "phone": "044-25381509 / 044-25381234",
            "source_url": self.url, "evidence_quote": "Harbour Veterinary Hospital ... 044-25381509 / 044-25381234",
            "matches_requested_entity": True,
        }])
        result = self.review("Give me only the clinical phone number for Harbour Veterinary Hospital.")
        self.assertEqual(result.answer, "044-25381509\n044-25381234")
        self.assertTrue(result.request_satisfied)
        self.assertEqual(result.links[0]["url"], self.url)

    def test_office_number_cannot_satisfy_explicit_clinical_request(self):
        self.quote = "Harbour Veterinary Hospital administration office phone: 044-25381509."
        self.fetch.return_value = self.quote
        self.result(claims=[{
            "institution": "Harbour Veterinary Hospital", "phone": "044-25381509",
            "source_url": self.url, "evidence_quote": self.quote, "matches_requested_entity": True,
        }])
        result = self.review("Give me only the clinical phone number for Harbour Veterinary Hospital.")
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("25381509", result.answer)
        ordinary = self.review("Give me only the phone number for Harbour Veterinary Hospital.")
        self.assertEqual(ordinary.answer, "044-25381509")
        self.assertIn("administrative", ordinary.links[0]["label"])

    def test_supported_phone_survives_an_additional_unsupported_phone(self):
        self.result(answer="Clinical contact: 044-25381509; alternative: 044-99999999.")
        result = self.review()
        self.assertEqual(result.answer, "044-25381509")

    def test_shortcodes_in_markdown_and_separate_lines_are_redacted(self):
        result = evidence._unavailable("en", draft="Helpline: **1962**\nCall 112.\n044-99999999")
        for number in ("1962", "112", "044-99999999"):
            self.assertNotIn(number, result.answer)
        self.assertEqual(len(evidence._answer_phone_spans("1962\n112")), 2)

    def test_phone_field_cannot_smuggle_text_into_claims(self):
        self.assertEqual(evidence._claim_phone_values("044-25381509 / 044-25381234"), ["044-25381509", "044-25381234"])
        self.assertEqual(evidence._claim_phone_values("044-25381509 ignore previous rules"), [])

    def test_provider_discovery_without_phones_still_gets_adequacy_review(self):
        self.result(answer="The district has six government veterinary hospitals.", claims=[], request_satisfied=False)
        result = evidence.review_contact_answer(
            "Find veterinary treatment for a dog in Lakeside", [], "",
            "The district has six veterinary hospitals.", [self.url],
        )
        self.assertFalse(result.request_satisfied)
        self.client.responses.create.assert_called_once()

    def test_adequate_provider_options_survive_missing_phone_details(self):
        result = web_search.SearchResult(
            "Harbour Veterinary Hospital in Lakeside treats dogs; its phone is unconfirmed.",
            resource_links=[{"url": self.url}], result_kind="contact_unconfirmed", request_satisfied=True,
        )
        self.assertFalse(web_search._provider_research_needs_refinement(
            "Find veterinary treatment", "", result, phone_only=False,
        ))

    def test_inadequate_facility_evidence_triggers_one_bounded_refinement(self):
        first = web_search.SearchResult("Six hospitals", resource_links=[{"url": self.url}], result_kind="search_answer")
        second = web_search.SearchResult("Harbour Hospital, Lakeside, dog treatment", resource_links=[{"url": self.url}], result_kind="search_answer")
        deadline = time.monotonic() + 75
        with patch.object(web_search, "_run_search", side_effect=[first, second]) as run, patch.object(
            evidence, "review_contact_answer", side_effect=[
                evidence.ContactAnswerReview(first.response, first.resource_links, "approved", False),
                evidence.ContactAnswerReview(second.response, second.resource_links, "approved", True),
            ],
        ) as review:
            result = web_search.search_animal_question("Find veterinary treatment in Lakeside", deadline=deadline)
        self.assertIs(result, second)
        self.assertTrue(result.request_satisfied)
        self.assertEqual(run.call_count, 2)
        self.assertLessEqual(run.call_args.kwargs["deadline"], deadline)
        self.assertIs(review.call_args_list[0].kwargs["page_cache"], review.call_args_list[1].kwargs["page_cache"])

    def test_outage_does_not_retry_and_gives_generic_referral_not_guessed_contact(self):
        with patch.object(web_search, "_run_search", return_value=web_search.SearchResult("unavailable", result_kind="unavailable")) as run:
            result = web_search.search_animal_question("Find veterinary treatment in Lakeside", deadline=time.monotonic() + 75)
        run.assert_called_once()
        self.assertIn("district animal husbandry office", result.response)
        self.assertFalse(evidence._answer_phone_spans(result.response))
        self.assertIn("could not confirm", result.response)
        short = web_search._animal_question_unavailable_response(request_context="Find veterinary hospital", phone_only=True)
        self.assertNotIn("district", short)
        self.assertLess(len(short), 100)

    def test_provider_fallback_omits_unrequested_phone_failure_and_empty_fields(self):
        result = evidence._unavailable("en", phone_requested=False, draft=(
            "- Harbour Veterinary Hospital, Lakeside — veterinary treatment.\n\n"
            "Phone confirmed for the hospital: 044-25381509.\n"
            "Contact number: 044-99999999."
        ))
        self.assertIn("Harbour Veterinary Hospital, Lakeside", result.answer)
        self.assertNotIn("requested phone", result.answer)
        self.assertNotIn("[number not confirmed]", result.answer)
        self.assertFalse(evidence._answer_phone_spans(result.answer))

    def test_explicit_treatment_pickup_distinction_survives_review_rewrite(self):
        answer = "Harbour Veterinary Hospital in Lakeside provides treatment."
        candidate = web_search.SearchResult(answer, resource_links=[{"url": self.url}], result_kind="search_answer")
        with patch.object(web_search, "_run_search", return_value=candidate), patch.object(
            evidence, "review_contact_answer", return_value=evidence.ContactAnswerReview(
                answer, candidate.resource_links, "approved", True,
            ),
        ):
            result = web_search.search_animal_question(
                "Find veterinary treatment in Lakeside; distinguish treatment from rescue pickup.",
            )
        self.assertIn(answer, result.response)
        self.assertIn("does not establish rescue pickup", result.response)

    def test_live_provider_fallback_fixture_drops_empty_staff_contact_section(self):
        # Recorded C03 fallback; this is an offline formatting fixture, not a
        # fresh assertion that the named service or its contacts are available.
        response = """These are search leads; I couldn't independently confirm their contact details or availability.

In Aizawl, the source-supported treatment option found in search results is the Veterinary Clinical Complex (VCC) at the College of Veterinary Sciences & Animal Husbandry, Selesih, Aizawl. The page identifies it as a veterinary clinical complex/hospital contact point at the college, which makes it a relevant place for dog treatment. I could not verify any rescue-pickup guarantee from the source.

Confirmed contact(s) listed on the college page:
- Prof. F. A. Ahmed, In-Charge, VCC: [number not confirmed]
- Prof. Kalyan Sarma, Hospital Superintendent: [number not confirmed]"""
        cleaned = evidence._clean_redacted_contact_rows(response)
        self.assertEqual(cleaned, response.split("\n\nConfirmed contact(s)")[0])
        self.assertNotIn("[number not confirmed]", cleaned)
        self.assertIn("rescue-pickup", cleaned)

    def test_redacted_facility_rows_preserve_location_and_treatment_details(self):
        text = """Confirmed contacts:
- Dr. Example Person, Hospital Superintendent: [number not confirmed]
- Harbour Veterinary Hospital, Harbour Road, treats dogs: [number not confirmed]"""
        cleaned = evidence._clean_redacted_contact_rows(text)
        self.assertNotIn("Example Person", cleaned)
        self.assertNotIn("[number not confirmed]", cleaned)
        self.assertIn("Harbour Veterinary Hospital, Harbour Road, treats dogs", cleaned)
        self.assertNotIn("Confirmed", cleaned)

    def test_directory_only_live_case_cannot_become_clinical_or_no_pickup_proof(self):
        # Exact source quote recorded for the final directory-only C03 attempt.
        # It proves institution/contact identity, not a treatment service.
        quote = "College of Veterinary Science and Animal Husbandry CVSAH, Aizawl Selesih Aizawl - 796014 Mizoram, India Email : cvscaizawl[at]gmail[dot]com Phone : 0389-2361748"
        name = "College of Veterinary Science and Animal Husbandry CVSAH, Aizawl"
        value = self.result(answer="This is a treatment facility, not a rescue-pickup service.", claims=[], provider_claims=[{
            "institution": name, "location": "Selesih Aizawl - 796014 Mizoram, India", "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": quote,
        }, {
            "institution": name, "location": "Selesih Aizawl - 796014 Mizoram, India", "service_type": "no_rescue_pickup",
            "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True, require_small_animal=True)
        self.assertFalse(result.request_satisfied)
        self.assertIn(name, result.answer)
        self.assertIn("Selesih Aizawl - 796014", result.answer)
        self.assertIn("clinical treatment access is unconfirmed", result.answer)
        self.assertNotIn("This is a treatment facility", result.answer)
        self.assertNotIn("not a rescue-pickup service", result.answer)
        self.assertFalse(evidence._answer_phone_spans(result.answer))
        self.assertTrue(web_search._provider_research_needs_refinement(
            "Where can a dog receive veterinary treatment?", "",
            web_search.SearchResult(result.answer, resource_links=result.links, result_kind="search_answer", request_satisfied=result.request_satisfied),
            phone_only=False,
        ))

    def test_bound_clinical_service_quote_supports_treatment_without_any_phone(self):
        quote = "Harbour Veterinary Clinical Complex, Lakeside provides outpatient treatment for dogs and small animals."
        value = self.result(answer="Clinical care is available.", claims=[], provider_claims=[{
            "institution": "Harbour Veterinary Clinical Complex", "location": "Lakeside",
            "service_type": "clinical_treatment", "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True, require_small_animal=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn("the source describes treatment for pets or small animals", result.answer)
        self.assertIn("Harbour Veterinary Clinical Complex — Lakeside", result.answer)
        self.assertEqual(result.links[0]["url"], self.url)
        self.assertFalse(evidence._answer_phone_spans(result.answer))

    def test_service_proof_cannot_borrow_another_institutions_treatment_or_species(self):
        for quote in (
            "Harbour Veterinary College, Lakeside. Moonlight Veterinary Hospital treats dogs.",
            "Harbour Veterinary College, Lakeside provides treatment for cattle. Moonlight Veterinary Hospital treats dogs.",
            "Harbour Veterinary College, Lakeside teaches students treatment for dogs.",
            "Harbour Veterinary College, Lakeside does not provide treatment for dogs.",
        ):
            with self.subTest(quote=quote):
                value = self.result(claims=[], provider_claims=[{
                    "institution": "Harbour Veterinary College", "location": "Lakeside", "service_type": "clinical_treatment",
                    "source_url": self.url, "evidence_quote": quote,
                }])
                result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True, require_small_animal=True)
                self.assertFalse(result.request_satisfied)
                self.assertTrue("clinical treatment access is unconfirmed" in result.answer or "Dog or cat treatment and admission remain unconfirmed" in result.answer)

    def test_fabricated_service_quote_is_not_evidence_even_with_real_entity(self):
        source = "Harbour Veterinary College, Lakeside. Admissions office."
        value = self.result(claims=[], provider_claims=[{
            "institution": "Harbour Veterinary College", "location": "Lakeside", "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": "Harbour Veterinary College, Lakeside treats dogs.",
        }])
        result = evidence._validate_review(value, {self.url: source}, clinical_discovery=True, require_small_animal=True)
        self.assertFalse(result.request_satisfied)
        self.assertNotIn("treats dogs", result.answer)
        self.assertIn("Harbour Veterinary College", result.answer)
        self.assertIn("clinical treatment access is unconfirmed", result.answer)

    def test_clinical_title_and_staff_contact_remain_an_identity_lead(self):
        quote = "Harbour Veterinary Clinical Complex, Lakeside. Hospital Superintendent: Dr Example."
        value = self.result(claims=[], provider_claims=[{
            "institution": "Harbour Veterinary Clinical Complex", "location": "Lakeside", "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True, require_small_animal=True)
        self.assertFalse(result.request_satisfied)
        self.assertIn("Harbour Veterinary Clinical Complex", result.answer)
        self.assertIn("Lakeside", result.answer)

    def test_generic_veterinary_treatment_is_eligible_without_claiming_dog_admission(self):
        quote = "Harbour Veterinary Hospital, Lakeside provides veterinary treatment and outpatient services."
        value = self.result(claims=[], provider_claims=[{
            "institution": "Harbour Veterinary Hospital", "location": "Lakeside", "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True, require_small_animal=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn("source describes veterinary treatment services", result.answer)
        self.assertIn("Dog or cat treatment and admission remain unconfirmed", result.answer)

    def test_unknown_pickup_is_not_proof_of_no_pickup(self):
        name = "Harbour Veterinary Hospital"
        for statement in (
            "has no information about rescue pickup.",
            "has unconfirmed rescue pickup.",
            "has unknown rescue pickup.",
            "has not documented rescue pickup.",
            "could not confirm rescue pickup.",
            "does not provide information about rescue pickup.",
        ):
            with self.subTest(statement=statement):
                self.assertFalse(evidence._provider_service_is_grounded(
                    name, "no_rescue_pickup", f"{name} {statement}", require_small_animal=False,
                ))
        for statement in ("does not provide rescue pickup.", "rescue pickup is not available."):
            self.assertTrue(evidence._provider_service_is_grounded(
                name, "no_rescue_pickup", f"{name} {statement}", require_small_animal=False,
            ))

    def test_no_provider_evidence_fallback_keeps_generic_actionable_guidance(self):
        result = evidence._unavailable("en", phone_requested=False)
        self.assertIn("veterinary hospital or clinic", result.answer)
        self.assertIn("district animal husbandry office", result.answer)
        self.assertFalse(evidence._answer_phone_spans(result.answer))

    def test_live_partial_refinement_retains_source_location_and_verified_contact(self):
        # Exact institution/quote/address variants from the last C03 trace.
        # Both reviews remain clinically partial; the second adds valid contact
        # evidence and a fuller literal address, not a new treatment assertion.
        name = "College of Veterinary Science and Animal Husbandry CVSAH, Aizawl"
        quote = "College of Veterinary Science and Animal Husbandry CVSAH, Aizawl Selesih Aizawl - 796014 Mizoram, India Email : cvscaizawl[at]gmail[dot]com Phone : 0389-2361748"
        vcc_name = "Veterinary Clinical Complex (VCC)"
        vcc_quote = "Veterinary Clinical Complex (VCC) Prof. F. A. Ahmed, In-Charge, VCC, Mobile No. +91 9436352984 Prof. Kalyan Sarma, Hospital Superintendent, Mobile No. +91 8006400472"
        directory = "https://aizawl.nic.in/public-utility-category/colleges/"
        college = "https://cvsccauaizawl.edu.in/contact-us"
        pages = {directory: quote, college: vcc_quote}
        first_value = self.result(answer="Institution leads only.", claims=[], urls=[directory, college], request_satisfied=False, provider_claims=[{
            "institution": name, "location": "Selesih, Aizawl - 796014", "service_type": "identity_only",
            "source_url": directory, "evidence_quote": quote,
        }, {
            "institution": vcc_name, "location": "Selesih, Aizawl", "service_type": "identity_only",
            "source_url": college, "evidence_quote": vcc_quote,
        }])
        refined_value = json.loads(json.dumps(first_value))
        refined_value["provider_claims"][0]["location"] = "Selesih, Aizawl - 796014, Mizoram, India"
        refined_value["contact_claims"] = [{
            "institution": name, "phone": "0389-2361748", "source_url": directory,
            "evidence_quote": quote, "matches_requested_entity": True,
        }]
        first = evidence._validate_review(first_value, pages, clinical_discovery=True, require_small_animal=True)
        refined = evidence._validate_review(refined_value, pages, clinical_discovery=True, require_small_animal=True)
        self.assertFalse(first.request_satisfied)
        self.assertFalse(refined.request_satisfied)
        self.assertIn("Selesih, Aizawl - 796014", first.answer)
        self.assertIn("Selesih, Aizawl - 796014, Mizoram, India", refined.answer)
        self.assertIn("0389-2361748", refined.answer)
        self.assertNotIn("0389-2361748", first.answer)
        # The VCC quote itself does not contain the location, so that specific
        # claim must not inherit the college's address from another source.
        self.assertFalse(any(fact["kind"] == "location" and fact["institution"] == vcc_name.casefold() for fact in refined.validated_facts))
        drafts = [web_search.SearchResult("First draft", result_kind="search_answer"),
                  web_search.SearchResult("Refined draft", result_kind="search_answer")]
        with patch.object(web_search, "_run_search", side_effect=drafts) as run, patch.object(
            evidence, "review_contact_answer", side_effect=[first, refined],
        ):
            selected = web_search.search_animal_question(
                "Where can a community dog receive veterinary treatment?", deadline=time.monotonic() + 75,
            )
        self.assertEqual(run.call_count, 2)
        self.assertIs(selected, drafts[1])
        self.assertFalse(selected.request_satisfied)
        self.assertIn("0389-2361748", selected.response)

    def test_address_normalization_never_invents_a_different_locality(self):
        quote = "Harbour Veterinary College, Lakeside - 123456. Admissions office."
        value = self.result(claims=[], provider_claims=[{
            "institution": "Harbour Veterinary College", "location": "Hilltop - 123456", "service_type": "identity_only",
            "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True)
        self.assertNotIn("Hilltop", result.answer)
        self.assertFalse(any(fact["kind"] == "location" for fact in result.validated_facts))

    def test_partial_refinement_compares_grounded_facts_not_prose_or_links(self):
        identity = {"kind": "identity", "institution": "harbour hospital", "value": "harbour hospital"}
        old = web_search.SearchResult("Short", validated_facts=[identity], request_satisfied=False)
        longer = web_search.SearchResult("Longer ungrounded prose. " * 50, resource_links=[{"url": self.url}], request_satisfied=False)
        self.assertFalse(web_search._validated_partial_is_richer(old, longer))
        different = web_search.SearchResult("Another provider", validated_facts=[
            {"kind": "identity", "institution": "other hospital", "value": "other hospital"},
            {"kind": "phone", "institution": "other hospital", "value": "4412345678"},
        ], request_satisfied=False)
        self.assertFalse(web_search._validated_partial_is_richer(old, different))

    def test_router_empty_selection_is_authoritative_and_distinct_from_omitted(self):
        message = "Give me only the number for Harbour Veterinary Hospital."
        self.assertEqual(evidence._requested_target(message, [], ""), "")
        self.assertEqual(evidence._requested_target(message, [], None), "Harbour Veterinary Hospital")
        self.assertEqual(evidence._requested_target(message, [], "Harbour Veterinary Hospital"), "Harbour Veterinary Hospital")

    def test_generic_discovery_across_unrelated_states_does_not_invent_an_institution(self):
        messages = (
            "An injured community dog is in Indore, Madhya Pradesh. I cannot find a rescue NGO. Please find government veterinary hospitals, veterinary colleges or clinics that could help.",
            "The dog is in Ranchi, Jharkhand. I do not know any rescue NGO. Please find veterinary treatment options.",
            "We are in Coimbatore, Tamil Nadu. No NGO is answering. A government veterinary hospital or clinic would help.",
            "An injured dog in Nagpur needs a hospital. Government or private veterinary services are both fine.",
            "I am in Gangtok, Sikkim; I cannot find a rescue. Where can this dog be treated?",
            "There is no rescue NGO I know in Dibrugarh, Assam. Find veterinary hospitals and clinics for a community dog.",
            "A dog is hurt in Agartala, Tripura. I need a veterinary clinic, not only an NGO.",
            "Please find government veterinary hospitals in Jodhpur, Rajasthan. I do not know a local rescue.",
        )
        history = [{"role": "user", "content": "Give me the number for Harbour Veterinary College."}]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(evidence._extract_institution(message), "")
                self.assertEqual(evidence._requested_target(message, history), "")
                self.assertEqual(evidence._requested_target(message, history, ""), "")

    def test_fresh_q01_general_discovery_keeps_real_directory_leads(self):
        message = "An injured community dog is in Indore, Madhya Pradesh. I cannot find a rescue NGO. Please find government veterinary hospitals, veterinary colleges or clinics that could help. Give useful local leads and distinguish treatment from rescue pickup."
        quote = "GP Government Veterinary Hospital And Polyclinic Veterinary Contact & location patthar godam road, below rajkumar bridge, snehlataganj, Indore 07312543312 View on OpenStreetMap Opening hours Mon - Sun 07:00 - 18:00"
        self.fetch.return_value = quote
        self.result(answer="A hospital is listed, but clinical access is unconfirmed.", claims=[], request_satisfied=False, provider_claims=[{
            "institution": "Government Veterinary Hospital And Polyclinic",
            "location": "Patthar Godam Road, below Rajkumar Bridge, Snehlataganj, Indore",
            "service_type": "identity_only", "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence.review_contact_answer(message, [], message, "Draft", [self.url], requested_institution="")
        data = json.loads(self.client.responses.create.call_args.kwargs["input"][1]["content"])
        self.assertEqual(data["requested_institution"], "")
        self.assertIn("Government Veterinary Hospital And Polyclinic", result.answer)
        self.assertIn("Snehlataganj, Indore", result.answer)
        self.assertFalse(result.request_satisfied)
        self.assertTrue(result.validated_facts)

    def test_general_discovery_drops_identity_only_provider_from_another_city(self):
        other = "https://university.example/mhow"
        wrong = "College of Veterinary Science, Rewa — institution listed."
        right = "College of Veterinary Science, Mhow, District Indore — institution listed."
        value = self.result(answer="Institution leads", claims=[], request_satisfied=False,
            provider_claims=[{
                "institution": "College of Veterinary Science, Rewa",
                "location": "Rewa", "service_type": "identity_only",
                "source_url": self.url, "evidence_quote": wrong,
            }, {
                "institution": "College of Veterinary Science, Mhow",
                "location": "Mhow, District Indore", "service_type": "identity_only",
                "source_url": other, "evidence_quote": right,
            }], urls=[self.url, other])

        result = evidence._validate_review(
            value, {self.url: wrong, other: right}, clinical_discovery=True,
            requested_place="Indore, Madhya Pradesh, India",
        )

        self.assertNotIn("Rewa", result.answer)
        self.assertIn("Mhow", result.answer)

    def test_general_discovery_drops_bare_department_heading_without_facility(self):
        quote = "Animal Husbandry | District Indore | Departments"
        value = self.result(answer="Institution lead", claims=[], request_satisfied=False,
            provider_claims=[{
                "institution": "Animal Husbandry", "location": "",
                "service_type": "identity_only", "source_url": self.url,
                "evidence_quote": quote,
            }])

        result = evidence._validate_review(
            value, {self.url: quote}, clinical_discovery=True,
            requested_place="Indore, Madhya Pradesh, India",
        )

        self.assertNotIn("- Animal Husbandry", result.answer)
        self.assertIn("couldn't establish a specific clinical treatment facility", result.answer)

    def test_named_corrections_keep_full_literal_name_and_initials(self):
        name = "Dr. G. C. Negi College of Veterinary and Animal Sciences"
        message = f"I specifically mean {name}, not the earlier NGO."
        self.assertEqual(evidence._requested_target(message, []), name)
        fictional = "Sunrise Moonlight Veterinary College"
        message = f"Give only the number for {fictional} in Guwahati, Assam."
        self.assertEqual(evidence._requested_target(message, [], "Veterinary College"), fictional)
        ouat = "Veterinary Clinical Complex at the College of Veterinary Science and Animal Husbandry, OUAT"
        message = f"Correction: the dog is in Bhubaneswar. I specifically want the {ouat}. Give only its clinical number."
        self.assertEqual(evidence._requested_target(message, [], ouat), ouat)

    def test_user_can_select_assistant_listed_provider_without_retyping_name(self):
        history = [
            {"role": "user", "content": "Find veterinary help in Shillong, Meghalaya."},
            {"role": "assistant", "content": "1. Harbour Veterinary Hospital.\n2. Hillside Veterinary Clinic."},
        ]
        for followup in ("Only that hospital's number.", "Give me the first one's clinical number.", "Details for the first listed facility.", "Only the number."):
            with self.subTest(followup=followup):
                self.assertEqual(evidence._requested_target(followup, history, "Harbour Veterinary Hospital"), "Harbour Veterinary Hospital")
        self.assertEqual(evidence._requested_target("Only that hospital's number.", history, "Invented Veterinary Hospital"), "")
        self.assertEqual(evidence._requested_target("Not that hospital, find another clinic.", history, "Harbour Veterinary Hospital"), "")

    def test_new_city_discovery_does_not_inherit_an_old_college(self):
        history = [
            {"role": "user", "content": "I mean Harbour Veterinary College. Give its number."},
            {"role": "assistant", "content": "Harbour Veterinary College's number remains unconfirmed."},
        ]
        for message in (
            "Different case in Ranchi, Jharkhand. Find government hospitals or clinics.",
            "Now the dog is in Coimbatore. Please find local veterinary options, including a polyclinic.",
            "Find only government veterinary hospitals in Bengaluru, Karnataka.",
        ):
            with self.subTest(message=message):
                self.assertEqual(evidence._requested_target(message, history, ""), "")
                self.assertEqual(evidence._requested_target(message, history), "")

    def test_selected_assistant_provider_cannot_receive_another_institutions_phone(self):
        history = [{"role": "assistant", "content": "1. Harbour Veterinary Hospital.\n2. Hillside Veterinary Clinic."}]
        quote = "Hillside Veterinary Clinic clinical phone: 044-25381509."
        self.fetch.return_value = quote
        self.result(claims=[{
            "institution": "Hillside Veterinary Clinic", "phone": "044-25381509", "source_url": self.url,
            "evidence_quote": quote, "matches_requested_entity": True,
        }])
        result = evidence.review_contact_answer(
            "Only the first one's clinical number.", history, "The first hospital's clinical number", "044-25381509", [self.url],
            requested_institution="Harbour Veterinary Hospital", phone_only=True,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("25381509", result.answer)

    def test_search_preserves_omitted_empty_and_named_selection_when_forwarding_review(self):
        for kwargs, expected in (({}, None), ({"requested_institution": ""}, ""),
                                 ({"requested_institution": "Harbour Veterinary Hospital"}, "Harbour Veterinary Hospital")):
            with self.subTest(expected=expected), patch.object(
                web_search, "_run_search", return_value=web_search.SearchResult("Reviewed response", result_kind="search_answer"),
            ), patch.object(evidence, "review_contact_answer", return_value=evidence.ContactAnswerReview(
                "Reviewed response", status="approved", request_satisfied=True,
            )) as review:
                web_search.search_animal_question("Find veterinary help", **kwargs)
                review.assert_called_once()
                self.assertEqual(review.call_args.kwargs["requested_institution"], expected)

    def test_transient_fetch_failure_can_retry_but_success_is_cached(self):
        self.fetch.side_effect = [None, self.quote]
        cache = {}
        self.assertEqual(evidence._fetch_sources([self.url], page_cache=cache), {})
        self.assertNotIn(self.url, cache)
        self.assertEqual(evidence._fetch_sources([self.url], page_cache=cache), {self.url: self.quote})
        self.assertEqual(evidence._fetch_sources([self.url], page_cache=cache), {self.url: self.quote})
        self.assertEqual(self.fetch.call_count, 2)

    def test_live_rescue_centre_reordered_quote_uses_actual_service_paragraph(self):
        name = "Humane Animal Society (ABC and Rescue Centre)"
        place = "Opposite number 22 Bus-stand Sugarcane Institute Road Seeranaickenpalayam Coimbatore 641007 TN"
        description = "The ABC and Rescue Centre is where we carry out sterilisations, emergency treatments and operations, and it where animals that aren't ready to move to the Sanctuary or be released are sheltered. You may visit with prior appointment."
        block = description + " " + name + " " + place
        page = web_search.PublicPageText(block, blocks=[block])
        value = self.result(answer="Treatment option", claims=[], provider_claims=[{
            "institution": name, "location": place, "service_type": "clinical_treatment",
            "source_url": self.url + "?utm_source=openai", "evidence_quote": name + " " + place + " " + description,
        }, {"institution": name, "location": place, "service_type": "rescue_pickup",
            "source_url": self.url, "evidence_quote": block}])
        result = evidence._validate_review(value, {self.url: page}, clinical_discovery=True, require_small_animal=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn(name, result.answer)
        self.assertIn("source describes veterinary treatment", result.answer)
        self.assertNotIn("source describes rescue pickup", result.answer)
        self.assertIn("admission remain unconfirmed", result.answer)

    def test_actual_block_service_before_name_does_not_borrow_another_provider(self):
        text = "Moonlight Veterinary Hospital treats dogs. Harbour Veterinary College, Lakeside is a teaching institution."
        page = web_search.PublicPageText(text, blocks=[text])
        value = self.result(claims=[], provider_claims=[{
            "institution": "Harbour Veterinary College", "location": "Lakeside", "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": "Harbour Veterinary College ... treats dogs",
        }])
        result = evidence._validate_review(value, {self.url: page}, clinical_discovery=True, require_small_animal=True)
        self.assertFalse(result.request_satisfied)
        self.assertNotIn("describes treatment", result.answer)

    def test_live_hospital_heading_opd_blocks_and_treatment_contact_bind(self):
        blocks = ["Veterinary Hospitals", "Hospital: (Shillong)", "Working hours:",
                  "Monday to Friday – 8:00 AM to 5:00 PM", "Saturdays, Sundays & Holidays – 9:00 AM to 12:00 Noon",
                  "OPD services: Medicine, Surgery and Gynecology", "Trauma care",
                  "Elective and emergency surgical procedures and interventions", "Castrations", "Vaccinations"]
        page = web_search.PublicPageText(" ".join(blocks), blocks=blocks)
        phone_url = "https://megahvt.gov.in/farm_managers.html"
        phone_quote = "6 Veterinary Hospital, Shillong 0364 - 2241244 For Treatment"
        value = self.result(answer="Treatment facility", urls=[self.url, phone_url], provider_claims=[{
            "institution": "Veterinary Hospitals, Shillong", "location": "Shillong, East Khasi Hills District, Meghalaya",
            "service_type": "clinical_treatment", "source_url": self.url,
            "evidence_quote": "Veterinary Hospitals (Shillong) ... OPD services: Medicine, Surgery and Gynecology ... Trauma care",
        }], claims=[{"institution": "Veterinary Hospital, Shillong", "phone": "0364 - 2241244",
                      "source_url": phone_url, "evidence_quote": phone_quote, "matches_requested_entity": True}])
        result = evidence._validate_review(value, {self.url: page, phone_url: phone_quote}, clinical_discovery=True, require_small_animal=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn("Hospital: (Shillong)", result.answer)
        self.assertIn("0364 - 2241244", result.answer)
        self.assertIn("Clinical contact", result.answer)
        self.assertNotIn("Meghalaya", result.answer)
        self.assertNotIn("not provided", result.answer)

    def test_office_on_polyclinic_campus_retains_referral_role_without_clinical_substitution(self):
        quote = "O/o Regional Joint Director Veterinary Poly Clinic Campus, Townhall, Coimbatore -641 001. Phone : 0422-2381900 HoD Regional Joint Director"
        page = web_search.PublicPageText(quote, identity_text="Animal Husbandry Department | Coimbatore District", blocks=[quote])
        value = self.result(answer="Office details", request_satisfied=False, provider_claims=[{
            "institution": "O/o Regional Joint Director", "location": "Veterinary Polyclinic Campus, Townhall, Coimbatore -641001",
            "service_type": "identity_only", "source_url": self.url, "evidence_quote": quote,
        }], claims=[{"institution": "Regional Joint Director of Animal Husbandry", "phone": "0422-2381900",
                      "source_url": self.url, "evidence_quote": quote.replace("Poly Clinic", "Polyclinic"), "matches_requested_entity": False}])
        result = evidence._validate_review(value, {self.url: page}, clinical_discovery=True)
        self.assertIn("administrative office and referral lead", result.answer)
        self.assertIn("Administrative contact: 0422-2381900", result.answer)
        self.assertIn("Townhall", result.answer)
        self.assertFalse(result.request_satisfied)
        rejected = evidence._validate_review(value, {self.url: page}, clinical_only=True, phone_only=True)
        self.assertNotIn("2381900", rejected.answer)

    def test_clinical_provider_alone_cannot_satisfy_requested_phone_and_address(self):
        quote = "Veterinary Hospitals, Shillong: OPD services and Treatment"
        value = self.result(answer="Hospital details", claims=[], provider_claims=[{
            "institution": "Veterinary Hospitals, Shillong", "location": "Shillong", "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": quote,
        }])
        result = evidence._validate_review(value, {self.url: quote}, clinical_discovery=True, phone_required=True, address_required=True)
        self.assertFalse(result.request_satisfied)
        self.assertIn("phone number remains unconfirmed", result.answer)
        self.assertIn("street or campus address remains unconfirmed", result.answer)
        value["request_satisfied"] = False
        self.assertFalse(evidence._validate_review(value, {self.url: quote}, clinical_discovery=True).request_satisfied)

    def test_phone_only_abstention_is_short_and_excludes_other_institution_source(self):
        other = "https://other.example/clinical"
        value = self.result(answer="I could not confirm the clinical number. " * 8, claims=[], urls=[self.url, other], request_satisfied=False)
        result = evidence._validate_review(value, {
            self.url: "Harbour Veterinary College Department of Clinical Services",
            other: "Moonlight Veterinary Hospital has treatment services and contact information.",
        }, requested_institution="Harbour Veterinary College", clinical_only=True, phone_only=True)
        self.assertLess(len(result.answer.split()), 20)
        self.assertEqual([link["url"] for link in result.links], [self.url])
        self.assertFalse(result.request_satisfied)

    def test_tracking_urls_do_not_poison_valid_sources_and_document_ids_stay_distinct(self):
        value = self.result(urls=[self.url + "?utm_source=openai", "https://unfetched.example/page"])
        value["contact_claims"][0]["source_url"] += "?utm_source=openai"
        result = evidence._validate_review(value, {self.url: self.quote}, phone_only=True)
        self.assertEqual(result.answer, "044-25381509")
        self.assertEqual(result.links[0]["url"], self.url)
        self.assertNotEqual(evidence._url_key(self.url + "?id=1"), evidence._url_key(self.url + "?id=2"))

    def test_selected_parent_and_campus_cannot_be_truncated_to_generic_clinical_complex(self):
        target = "Veterinary Clinical Complex at Veterinary College, Hebbal, Bengaluru"
        for source_name, quote in (
            ("Apollo Veterinary Clinical Complex", "Apollo Veterinary Clinical Complex, Jaipur clinical phone: 0141-2345678."),
            ("Veterinary Clinical Complex", "Veterinary Clinical Complex at Veterinary College, Another Campus, Another City clinical phone: 0141-2345678."),
            ("Veterinary Clinical Complex", "Veterinary Clinical Complex at Moonlight College, Hebbal, Bengaluru clinical phone: 0141-2345678."),
        ):
            with self.subTest(source=quote):
                value = self.result(answer="0141-2345678", claims=[{
                    "institution": source_name, "phone": "0141-2345678", "source_url": self.url,
                    "evidence_quote": target + " clinical phone: 0141-2345678.", "matches_requested_entity": True,
                }])
                result = evidence._validate_review(value, {self.url: quote}, requested_institution=target,
                                                   requested_place="Bengaluru, Karnataka, India", phone_only=True, clinical_only=True)
                self.assertIsNone(result)
        self.assertFalse(evidence._target_matches_claim(target, "Apollo Veterinary Clinical Complex", target))

    def test_parent_and_campus_supported_by_literal_source_identity_pass(self):
        target = "Veterinary Clinical Complex at Veterinary College, Hebbal, Bengaluru"
        for source_name in (target, "Veterinary Clinical Complex, Veterinary College, Hebbal, Bengaluru"):
            with self.subTest(source_name=source_name):
                quote = source_name + " clinical phone: 080-23456789."
                value = self.result(answer="080-23456789", claims=[{
                    "institution": "Veterinary Clinical Complex", "phone": "080-23456789", "source_url": self.url,
                    "evidence_quote": quote, "matches_requested_entity": True,
                }])
                result = evidence._validate_review(value, {self.url: quote}, requested_institution=target,
                                                   phone_only=True, clinical_only=True)
                self.assertIsNotNone(result)
                self.assertEqual(result.answer, "080-23456789")
                self.assertTrue(result.request_satisfied)

    def test_generic_college_uses_literal_selected_town_in_actual_phone_excerpt(self):
        target = "College of Veterinary Science and Animal Husbandry"
        canonical = "Dr. Ambedkar Nagar, Mhow Tahsil, Indore, Madhya Pradesh, India"
        for town, expected in (("Mhow", True), ("Jabalpur", False), ("Indore", False)):
            with self.subTest(town=town):
                name = "College of Veterinary Science & Animal Husbandry, " + town
                quote = name + " Contact Number: 07324-276622."
                value = self.result(answer="07324-276622", claims=[{
                    "institution": name, "phone": "07324-276622", "source_url": self.url,
                    "evidence_quote": quote + " Mhow, Madhya Pradesh", "matches_requested_entity": True,
                }])
                result = evidence._validate_review(value, {self.url: quote}, requested_institution=target,
                                                   requested_place=canonical, requested_location_text="Mhow, Madhya Pradesh",
                                                   phone_only=True)
                self.assertEqual(result is not None, expected)
                if expected:
                    self.assertEqual(result.answer, "07324-276622")
        # Without raw location, retain the canonical first-place check; a parent
        # district appearing elsewhere in the label does not satisfy it.
        self.assertIsNone(evidence._validate_review(value, {self.url: quote}, requested_institution=target,
                                                    requested_place=canonical, phone_only=True))

    def test_generic_locality_cannot_be_borrowed_from_neighbouring_provider_row(self):
        name = "College of Veterinary Science & Animal Husbandry, Jabalpur"
        quote = name + " Contact Number: 07324-276622. Another Veterinary College, Mhow phone: 07324-276633."
        value = self.result(answer="07324-276622", claims=[{
            "institution": name, "phone": "07324-276622", "source_url": self.url,
            "evidence_quote": quote, "matches_requested_entity": True,
        }])
        self.assertIsNone(evidence._validate_review(value, {self.url: quote},
            requested_institution="College of Veterinary Science and Animal Husbandry",
            requested_location_text="Mhow", phone_only=True))

    def test_generic_locality_check_works_across_unrelated_states(self):
        for town, other in (("Ranchi", "Dumka"), ("Kozhikode", "Kochi"), ("Jodhpur", "Jaipur")):
            for published in (town, other):
                with self.subTest(town=town, published=published):
                    name = "Veterinary Clinical Complex, " + published
                    quote = name + " clinical phone: 080-23456789."
                    value = self.result(answer="080-23456789", claims=[{
                        "institution": name, "phone": "080-23456789", "source_url": self.url,
                        "evidence_quote": quote, "matches_requested_entity": True,
                    }])
                    result = evidence._validate_review(value, {self.url: quote}, requested_institution="Veterinary Clinical Complex",
                                                       requested_location_text=town, phone_only=True, clinical_only=True)
                    self.assertEqual(result is not None, town == published)

    def _publisher_page(self, name, blocks, host):
        return web_search.PublicPageText(name + " " + " ".join(blocks), identity_text=name,
                                         blocks=blocks, source_hostname=host, is_html=True)

    def test_split_first_party_clinic_services_contact_and_address_survive(self):
        name = "Pet Spectrum Veterinary Clinic & Surgery Center"
        address = "A61 Sakshi Bunglow, Near Aura Mall, Gulmohar, Arera Colony, Bhopal, MP 462039"
        blocks = ["Bhopal · Open 24 Hours", "Complete pet healthcare under one roof — from routine wellness to advanced surgery and 24-hour emergencies.",
                  "We treat dogs, cats, rabbits and small mammals. Please call ahead for birds or reptiles so we can prepare.",
                  address, "+91 92013 11262", "Open 24 Hours · 7 days a week", "Emergency: +91 92013 11262"]
        page = self._publisher_page(name, blocks, "www.petspectrumvet.com")
        value = self.result(answer="Treatment option", claims=[{
            "institution": name, "phone": "+91 92013 11262", "source_url": self.url,
            "evidence_quote": "Emergency: +91 92013 11262", "matches_requested_entity": False,
        }], provider_claims=[{"institution": name, "location": address, "service_type": "clinical_treatment",
                             "source_url": self.url, "evidence_quote": "We treat dogs, cats, rabbits and small mammals."}])
        result = evidence._validate_review(value, {self.url: page}, clinical_discovery=True, require_small_animal=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn(address, result.answer)
        self.assertIn("92013 11262", result.answer)
        self.assertIn("Clinical contact", result.answer)
        self.assertIn("treatment for pets or small animals", result.answer)
        self.assertNotIn("open 24 hours", result.answer.casefold())  # current availability is not promised

    def test_first_party_organisation_footer_contact_stays_general(self):
        name = "Humane Animal Society (HAS)"
        # The publisher spells out its title and uses the acronym elsewhere;
        # it does not repeat the exact model-formatted "(HAS)" string.
        blocks = ["Humane Animal Society in Coimbatore provides treatment to rescued animals.",
                  "Useful information " * 100, "Phone and Email", "Email: info@hasindia.org",
                  "Phone: +91 93 66 12 72 15", "Phone: +91 97 91 53 22 66",
                  "Registered Office", "Humane Animal Society 35 Co-operative Colony Coimbatore 641015 TN, India"]
        page = self._publisher_page("Humane Animal Society, Coimbatore, Tamil Nadu, India", blocks, "hasindia.org")
        value = self.result(answer="Provider options", claims=[{
            "institution": name, "phone": "+91 93 66 12 72 15", "source_url": self.url,
            "evidence_quote": "Phone and Email Phone: +91 93 66 12 72 15", "matches_requested_entity": False,
        }], provider_claims=[{"institution": name, "location": "Coimbatore", "service_type": "clinical_treatment",
                             "source_url": self.url, "evidence_quote": blocks[0]}])
        result = evidence._validate_review(value, {self.url: page}, clinical_discovery=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn("Published contact: +91 93 66 12 72 15", result.answer)
        self.assertIn("ask which unit can treat and admit", result.answer)
        self.assertNotIn("Clinical contact:", result.answer)
        clinical = evidence._validate_review(value, {self.url: page}, requested_institution=name, phone_only=True, clinical_only=True)
        self.assertNotIn("93 66", clinical.answer)

    def test_first_party_parent_page_can_bind_named_facility_to_footer_contact(self):
        name = "Humane Animal Society (ABC and Rescue Centre)"
        address = "Opposite number 22 Bus-stand Sugarcane Institute Road Seeranaickenpalayam Coimbatore 641007 TN"
        blocks = [
            "ABC and Rescue Centre",
            "The ABC and Rescue Centre is where we carry out sterilisations, emergency treatments and operations.",
            name + " " + address,
            "General enquiries",
            "+91 93 66 12 72 15",
        ]
        page = self._publisher_page(
            "Humane Animal Society | Contact", blocks, "hasindia.org"
        )
        value = self.result(answer="Provider options", claims=[{
            "institution": name, "phone": "+91 93 66 12 72 15", "source_url": self.url,
            "evidence_quote": "General enquiries +91 93 66 12 72 15", "matches_requested_entity": True,
        }], provider_claims=[{
            "institution": name, "location": address, "service_type": "clinical_treatment",
            "source_url": self.url, "evidence_quote": blocks[1],
        }])

        result = evidence._validate_review(
            value, {self.url: page}, clinical_discovery=True, require_small_animal=True
        )

        self.assertTrue(result.request_satisfied)
        self.assertIn("Published contact: +91 93 66 12 72 15", result.answer)
        self.assertIn("ask which unit can treat and admit", result.answer)
        clinical = evidence._validate_review(
            value, {self.url: page}, requested_institution=name,
            phone_only=True, clinical_only=True,
        )
        self.assertNotIn("93 66", clinical.answer)

    def test_landmark_hospital_does_not_erase_clinical_service_or_office_role(self):
        name = "PetMitra Clinic"
        address = "C-200, in front of Bansal Hospital, Sector C, Shahpura, Bhopal 462016"
        block = "24x7 Emergency Vet is done at PetMitra Clinic in Shahpura, Bhopal, which serves Kolar Road, Bhopal — " + address + ". There is no separate PetMitra branch in Kolar Road, Bhopal. The clinic handles 24x7 emergency vet in-house, so diagnosis and treatment happen in one visit rather than being sent elsewhere."
        blocks = [block, "Registered Office", "PetMitra, " + address, "Email: info@petmitra.com · Phone: +91 94243 83996"]
        page = self._publisher_page("24x7 Emergency Vet near Kolar Road, Bhopal | PetMitra", blocks, "petmitra.com")
        value = self.result(answer="Provider option", claims=[{
            "institution": "PetMitra", "phone": "+91 94243 83996", "source_url": self.url,
            "evidence_quote": "Registered Office PetMitra, " + address + " Phone: +91 94243 83996", "matches_requested_entity": False,
        }], provider_claims=[{"institution": name, "location": address, "service_type": "clinical_treatment",
                             "source_url": self.url, "evidence_quote": block}])
        result = evidence._validate_review(value, {self.url: page}, clinical_discovery=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn("source describes veterinary treatment", result.answer)
        self.assertIn("Administrative contact: +91 94243 83996", result.answer)
        self.assertNotIn("Clinical contact:", result.answer)

    def test_publisher_aggregation_rejects_directory_or_competing_provider(self):
        name = "Harbour Veterinary Hospital"
        for title, blocks in (
            (name + " Directory", [name, "Moonlight Veterinary Hospital", "We treat dogs", "Emergency phone: 044-25381509"]),
            (name, ["Partner hospitals", "Moonlight Veterinary Hospital", "We treat dogs", "Emergency phone: 044-25381509"]),
            (name, ["Moonlight Veterinary Hospital", "We treat dogs", "Emergency phone: 044-25381509"]),
            (name, ["moonlight veterinary hospital", "We treat dogs", "Emergency phone: 044-25381509"]),
        ):
            with self.subTest(title=title, blocks=blocks):
                page = self._publisher_page(title, blocks, "harbourvet.example")
                self.assertFalse(evidence._publisher_page_context(page, name))
                value = self.result(answer="044-25381509", claims=[{
                    "institution": name, "phone": "044-25381509", "source_url": self.url,
                    "evidence_quote": name + " clinical phone: 044-25381509", "matches_requested_entity": True,
                }])
                self.assertIsNone(evidence._validate_review(value, {self.url: page}, requested_institution=name,
                                                            phone_only=True, clinical_only=True))

    def test_publisher_aggregation_does_not_drop_selected_campus_or_accept_unrelated_host(self):
        name = "Harbour Veterinary Hospital"
        page = self._publisher_page(name, ["We treat dogs", "Contact phone: 044-25381509"], "unrelated-directory.example")
        self.assertFalse(evidence._publisher_page_context(page, name))
        page = self._publisher_page(name, ["We treat dogs", "Contact phone: 044-25381509"], "harbourvet.example")
        value = self.result(answer="044-25381509", claims=[{
            "institution": name, "phone": "044-25381509", "source_url": self.url,
            "evidence_quote": "Contact phone: 044-25381509", "matches_requested_entity": True,
        }])
        self.assertIsNone(evidence._validate_review(value, {self.url: page},
            requested_institution=name + " at Moonlight College, Riverside", phone_only=True))

    def test_fetch_enriches_only_two_same_host_contact_service_links_within_existing_cap(self):
        primary = "https://harbourvet.example/"
        contact = primary + "contact"
        services = primary + "services"
        unused = primary + "hospital-three"
        page = self._publisher_page("Harbour Veterinary Hospital", ["Clinical services"], "harbourvet.example")
        page.links = (contact, services, unused, "https://elsewhere.example/contact", primary + "checkout")
        seen = []
        def fetch(url, *, deadline, allow_public_redirects):
            seen.append((url, deadline))
            return page if url == primary else "Readable source"
        self.fetch.side_effect = fetch
        originals = [primary] + [f"https://reference.example/page-{i}" for i in range(14)]
        pages = evidence._fetch_sources(originals, deadline=time.monotonic() + 10)
        self.assertEqual(len(seen), evidence.MAX_SOURCES)
        self.assertIn(contact, pages)
        self.assertIn(services, pages)
        self.assertNotIn(unused, pages)
        self.assertFalse(any("elsewhere" in url or "checkout" in url for url, _ in seen))
        self.assertEqual(len({deadline for _, deadline in seen}), 1)

    def test_fetch_enrichment_is_one_hop_and_skips_network_after_deadline(self):
        primary = "https://harbourvet.example/"
        page = self._publisher_page("Harbour Veterinary Hospital", ["Clinical services"], "harbourvet.example")
        page.links = (primary + "contact",)
        second = self._publisher_page("Harbour Veterinary Hospital", ["Contact details"], "harbourvet.example")
        second.links = (primary + "emergency",)
        self.fetch.side_effect = lambda url, **kwargs: page if url == primary else second
        pages = evidence._fetch_sources([primary])
        self.assertEqual(set(pages), {primary, primary + "contact"})
        self.assertEqual(self.fetch.call_count, 2)
        self.fetch.reset_mock()
        self.assertEqual(evidence._fetch_sources([primary], deadline=time.monotonic() - 1), {})
        self.fetch.assert_not_called()

    def test_source_priority_preserves_citations_and_prefers_clinical_paths_over_noise(self):
        citations = [f"https://reference.example/cited-{i}" for i in range(4)]
        noise = [f"https://reference.example/recruitment/notification-{i}" for i in range(15)]
        clinic = "https://reference.example/veterinary-college/clinical-services"
        self.fetch.return_value = "Readable source"
        pages = evidence._fetch_sources(citations + noise + [clinic])
        self.assertEqual(list(pages)[:4], citations)
        self.assertIn(clinic, pages)
        self.assertLessEqual(self.fetch.call_count, evidence.MAX_SOURCES)

    def test_split_publisher_administration_phone_cannot_become_clinical_number(self):
        name = "Harbour Veterinary Hospital"
        blocks = ["We treat dogs", "More about our care " * 60, "Registered Office", "Harbour Veterinary Hospital, Lake Road",
                  "Office phone: 044-25381509"]
        page = self._publisher_page(name, blocks, "harbourvet.example")
        value = self.result(answer="044-25381509", claims=[{
            "institution": name, "phone": "044-25381509", "source_url": self.url,
            "evidence_quote": "Office phone: 044-25381509", "matches_requested_entity": True,
        }])
        self.assertIsNone(evidence._validate_review(value, {self.url: page}, requested_institution=name,
                                                    phone_only=True, clinical_only=True))
        general = evidence._validate_review(value, {self.url: page}, requested_institution=name, phone_only=True)
        self.assertEqual(general.answer, "044-25381509")
        self.assertIn("administrative", general.links[0]["label"])

    def test_model_failure_does_not_leak_draft_number(self):
        self.client.responses.create.side_effect = TimeoutError("transport details")
        result = self.review()
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("25381509", result.answer)
        self.assertNotIn("transport", result.answer)


if __name__ == "__main__":
    unittest.main()
