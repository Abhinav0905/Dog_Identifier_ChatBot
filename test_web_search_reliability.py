#!/usr/bin/env python3
"""Focused regressions for deterministic exact-city NGO discovery."""

import time
import unittest
from unittest.mock import patch

from services import web_search


class TestNgoSearchReliability(unittest.TestCase):
    @staticmethod
    def _page(
        *blocks: str,
        identity: str,
        hostname: str,
        links: tuple[str, ...] = (),
    ) -> web_search.PublicPageText:
        return web_search.PublicPageText(
            " ".join(blocks),
            identity_text=identity,
            identity_blocks=(identity,),
            blocks=blocks,
            source_hostname=hostname,
            is_html=True,
            links=links,
        )

    @staticmethod
    def _renderable_option(
        name: str,
        official_url: str,
        *,
        phone: str = "",
    ) -> dict[str, str]:
        return {
            "name": name,
            "service_area": "Dharamshala, Himachal Pradesh",
            "official_url": official_url,
            "phone": phone,
            "phone_source_url": f"{official_url.rstrip('/')}/contact" if phone else "",
            "address": "",
            "address_source_url": "",
            "opening_hours": "",
            "opening_hours_source_url": "",
        }

    def test_manali_strays_official_service_page_is_retained(self):
        service_url = "https://manalistrays.org/veterinary-care/"
        root_url = "https://manalistrays.org/"
        rescue = (
            "Help us treat animals and sustain our rescue center by investing in us."
        )
        service_and_type = (
            "Manali Strays - the first animal rescue charity and veterinary hospital "
            "in Kullu district"
        )
        pages = {
            web_search._source_url_key(service_url): self._page(
                rescue,
                service_and_type,
                identity="Manali Strays",
                hostname="manalistrays.org",
            ),
            web_search._source_url_key(root_url): self._page(
                "Manali Strays animal rescue charity and veterinary hospital",
                identity="Manali Strays",
                hostname="manalistrays.org",
            ),
        }

        def fetch(url, *, deadline=None):
            del deadline
            return pages.get(web_search._source_url_key(url))

        with patch.object(web_search, "_fetch_public_page_text", side_effect=fetch):
            verified = web_search._deterministically_verify_discovered_candidates(
                [
                    {
                        "name": "Manali Strays",
                        "possible_official_url": service_url,
                    }
                ],
                raw_options=[],
                required_city="Manali",
                required_region="Himachal Pradesh",
                page_content_cache={},
                deadline=time.monotonic() + 5,
            )

        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["name"], "Manali Strays")
        self.assertEqual(verified[0]["animal_rescue_evidence_url"], service_url)
        self.assertEqual(verified[0]["official_url"], service_url)

    def test_core_city_seeds_are_discovery_only_and_location_scoped(self):
        manali = web_search._core_city_official_discovery_seeds(
            "Manali",
            "Himachal Pradesh",
        )
        dharamshala = web_search._core_city_official_discovery_seeds(
            "Dharamsala",
            "Himachal Pradesh",
        )
        unrelated = web_search._core_city_official_discovery_seeds(
            "Shimla",
            "Himachal Pradesh",
        )

        self.assertEqual([item["name"] for item in manali], ["Manali Strays"])
        self.assertEqual(
            [item["name"] for item in dharamshala],
            ["Dharamsala Animal Rescue", "Tibet Charity"],
        )
        self.assertTrue(all("phone" not in item for item in manali + dharamshala))
        self.assertEqual(unrelated, [])

    def test_tibet_charity_animal_care_seed_is_first_and_retained(self):
        animal_care_url = "https://tibetcharity.in/animal-care/"
        root_url = "https://tibetcharity.in/"
        about_url = "https://tibetcharity.in/about-us/"
        rescue = (
            "Medical treatment and rescue of sick and injured animals is provided "
            "through this program."
        )
        service = (
            "Our animal care program treats sick and injured animals throughout "
            "Dharamshala every day."
        )
        organization_type = (
            "Tibet Charity is a registered non-profit charitable organization "
            "supporting local communities."
        )
        pages = {
            web_search._source_url_key(animal_care_url): self._page(
                rescue,
                service,
                identity="Tibet Charity | Animal Care",
                hostname="tibetcharity.in",
                links=(about_url,),
            ),
            web_search._source_url_key(root_url): self._page(
                "Tibet Charity community programs",
                identity="Tibet Charity",
                hostname="tibetcharity.in",
                links=(about_url,),
            ),
            web_search._source_url_key(about_url): self._page(
                organization_type,
                identity="About Tibet Charity",
                hostname="tibetcharity.in",
            ),
        }
        fetched: list[str] = []

        def fetch(url, *, deadline=None):
            del deadline
            fetched.append(url)
            return pages.get(web_search._source_url_key(url))

        with patch.object(web_search, "_fetch_public_page_text", side_effect=fetch):
            verified = web_search._deterministically_verify_discovered_candidates(
                [
                    {
                        "name": "Tibet Charity Trust",
                        "possible_official_url": animal_care_url,
                    }
                ],
                raw_options=[],
                required_city="Dharamshala",
                required_region="Himachal Pradesh",
                page_content_cache={},
                deadline=time.monotonic() + 5,
            )

        self.assertEqual(fetched[0], animal_care_url)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["name"], "Tibet Charity Trust")
        self.assertEqual(verified[0]["animal_rescue_evidence_url"], animal_care_url)
        self.assertEqual(verified[0]["service_area_evidence_url"], animal_care_url)
        self.assertEqual(verified[0]["organization_type_evidence_url"], about_url)

    def test_legal_suffix_tolerance_remains_exact(self):
        self.assertTrue(
            web_search._organization_names_match(
                "Tibet Charity Trust",
                "Tibet Charity",
            )
        )
        self.assertFalse(
            web_search._organization_names_match(
                "Animal Rescue Trust",
                "Animal Rescue",
            )
        )
        self.assertFalse(
            web_search._organization_names_match(
                "Tibet Charity Trust",
                "Tibet Charity Foundation",
            )
        )

    def test_dharamsala_spelling_and_official_trust_name_are_valid_evidence(self):
        service_evidence = (
            "Dharamsala Animal Rescue runs a rescue program for the thousands "
            "of animals living on the streets of Dharamsala"
        )
        organization_type_evidence = (
            "Dharamsala Animal Rescue Trust Slate Godam Road VPO Rakkar "
            "Dharamshala Himachal Pradesh 176057"
        )

        self.assertTrue(
            web_search._service_evidence_matches_geography(
                service_evidence,
                "Dharamshala",
                "Himachal Pradesh",
            )
        )
        self.assertTrue(
            web_search._organization_type_evidence_is_valid(
                organization_type_evidence,
                "Dharamsala Animal Rescue",
            )
        )
        self.assertFalse(
            web_search._organization_type_evidence_is_valid(
                "Animal Rescue Trust helps local animals in the city",
                "Animal Rescue",
            )
        )

    def test_discovery_cleanup_and_crawl_priority_prefer_service_pages(self):
        self.assertEqual(
            web_search._clean_discovered_candidate_name(
                "Tibet Charity – Animal Care Section"
            ),
            "Tibet Charity",
        )
        service_priority = web_search._candidate_page_priority(
            "https://example.org/animal-care/",
            "Dharamshala",
        )
        contact_priority = web_search._candidate_page_priority(
            "https://example.org/contact/",
            "Dharamshala",
        )
        hindi_contact_priority = web_search._candidate_page_priority(
            "https://example.org/hi/contact/",
            "Dharamshala",
        )
        self.assertLess(service_priority, contact_priority)
        self.assertLess(contact_priority, hindi_contact_priority)

    def test_animal_care_phone_is_preferred_over_unrelated_department_numbers(self):
        page = self._page(
            "Phone/Whatsapp",
            "Director: +91 94180 80737",
            "Education: +91 86279 94233",
            "Animal Care: +91 98827 87777",
            identity="Tibet Charity",
            hostname="tibetcharity.in",
        )

        self.assertEqual(
            web_search._extract_phone_numbers_from_page(page),
            ["+91 98827 87777"],
        )

    def test_exact_city_verifier_keeps_multiple_contacts_after_phone_enrichment(self):
        candidates = [
            {
                "name": "Alpha Paws Trust",
                "possible_official_url": "https://alpha-paws.example/",
            },
            {
                "name": "Beta Tails Trust",
                "possible_official_url": "https://beta-tails.example/",
            },
        ]
        pages = {
            web_search._source_url_key("https://alpha-paws.example/"): self._page(
                "Alpha Paws Trust operates an animal rescue centre in Dharamshala "
                "and treats injured dogs every day.",
                "Alpha Paws Trust is a registered non-profit charity in India.",
                "Phone: +91 91234 56781",
                identity="Alpha Paws Trust",
                hostname="alpha-paws.example",
            ),
            web_search._source_url_key("https://beta-tails.example/"): self._page(
                "Beta Tails Trust operates an animal rescue centre in Dharamshala "
                "and treats injured dogs every day.",
                "Beta Tails Trust is a registered non-profit charity in India.",
                "Phone: +91 92345 67812",
                identity="Beta Tails Trust",
                hostname="beta-tails.example",
            ),
        }

        def fetch(url, *, deadline=None):
            del deadline
            return pages.get(web_search._source_url_key(url))

        with patch.object(web_search, "_fetch_public_page_text", side_effect=fetch):
            verified = web_search._deterministically_verify_discovered_candidates(
                candidates,
                raw_options=[],
                required_city="Dharamshala",
                required_region="Himachal Pradesh",
                page_content_cache={},
                deadline=time.monotonic() + 5,
            )

        self.assertEqual(
            [(option["name"], option["phone"]) for option in verified],
            [
                ("Alpha Paws Trust", "+91 91234 56781"),
                ("Beta Tails Trust", "+91 92345 67812"),
            ],
        )

    def test_model_timeout_preserves_completed_deterministic_discovery(self):
        candidate = {
            "name": "Example Animal Trust",
            "possible_official_url": "https://example.org/",
        }
        option = self._renderable_option(
            "Example Animal Trust",
            "https://example.org/",
        )
        with patch.object(
            web_search,
            "_discover_ngo_candidates",
            return_value=[candidate],
        ), patch.object(
            web_search,
            "_deterministically_verify_discovered_candidates",
            return_value=[option],
        ), patch.object(
            web_search,
            "_request_structured_web_search",
            return_value=None,
        ):
            result = web_search._run_structured_ngo_search(
                "Find current animal help",
                required_city="Dharamshala",
                required_region="Himachal Pradesh",
                no_results_place="Dharamshala, Himachal Pradesh, India",
                discover_exact_city_candidates=True,
            )

        self.assertEqual(result.result_kind, "verified_options")
        self.assertTrue(result.candidate_discovery_complete)
        self.assertEqual(result.organizations, [option])

    def test_candidate_discovery_cannot_consume_the_entire_search_deadline(self):
        with patch.object(
            web_search.config,
            "DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS",
            60.0,
        ), patch.object(
            web_search.time,
            "monotonic",
            return_value=100.0,
        ), patch.object(
            web_search,
            "_discover_ngo_candidates",
            return_value=[],
        ) as discover, patch.object(
            web_search,
            "_request_structured_web_search",
            return_value=({"organizations": []}, set()),
        ):
            web_search._run_structured_ngo_search(
                "Find current animal help",
                required_city="Patna",
                required_region="Bihar",
                no_results_place="Patna, Bihar, India",
                discover_exact_city_candidates=True,
            )

        self.assertEqual(discover.call_args.kwargs["deadline"], 130.0)

    def test_third_party_directory_identity_is_not_treated_as_official_site(self):
        listing_url = "https://directory.example/dharamshala/animal-help/123"
        listing_page = self._page(
            "Example Animal Trust rescues injured dogs in Dharamshala.",
            identity="Example Animal Trust - Directory Example",
            hostname="directory.example",
        )
        directory_root = self._page(
            "Search local businesses and services.",
            identity="Directory Example",
            hostname="directory.example",
        )

        def page_for(url, _cache, *, deadline=None):
            del deadline
            if web_search._source_url_key(url) == web_search._source_url_key(listing_url):
                return listing_page
            return directory_root

        with patch.object(
            web_search,
            "_cached_public_page_text",
            side_effect=page_for,
        ):
            self.assertFalse(
                web_search._first_party_site_identity_is_valid(
                    "Example Animal Trust",
                    listing_url,
                    page_content_cache={},
                    deadline=time.monotonic() + 5,
                )
            )

    def test_placeholder_phone_is_removed_from_rendered_output(self):
        for placeholder in (
            "123-456-7890",
            "9999999999",
            "1111111111",
            "9000000000",
            "0123456789",
        ):
            with self.subTest(phone=placeholder):
                option = self._renderable_option(
                    "Example Animal Trust",
                    "https://example.org/",
                    phone=placeholder,
                )
                response, links = web_search._render_verified_organizations(
                    [option],
                    language="en",
                )

                self.assertNotIn(placeholder, response)
                self.assertNotIn("phone", links[0])

    def test_mixed_valid_and_placeholder_phone_renders_only_the_valid_number(self):
        option = self._renderable_option(
            "Example Animal Trust",
            "https://example.org/",
            phone="+91 98100 36255, 123-456-7890",
        )

        response, links = web_search._render_verified_organizations(
            [option],
            language="en",
        )

        self.assertIn("+91 98100 36255", response)
        self.assertNotIn("123-456-7890", response)
        self.assertEqual(links[0]["phone"], "+91 98100 36255")

    def test_registered_office_cannot_prove_exact_city_rescue_coverage(self):
        option = {
            "service_area_evidence": (
                "Our rescue team treats injured dogs throughout New Delhi."
            ),
            "address": "Registered office: Jaipur, Rajasthan",
            "official_url": "https://example.org/jaipur-office",
        }

        self.assertFalse(
            web_search._source_backed_service_geography_matches(
                option,
                "Jaipur",
                "Rajasthan",
            )
        )
        for evidence in (
            "The founder was born in Jaipur, Rajasthan and later started a charity.",
            "We conducted an animal welfare awareness workshop in Jaipur in 2019.",
        ):
            with self.subTest(evidence=evidence):
                self.assertFalse(
                    web_search._source_backed_service_geography_matches(
                        {"service_area_evidence": evidence},
                        "Jaipur",
                        "Rajasthan",
                    )
                )

    def test_tenant_google_site_can_reach_first_party_identity_validation(self):
        self.assertFalse(
            web_search._is_government_or_directory_domain("sites.google.com")
        )
        tenant_root = "https://sites.google.com/view/pune-paws/"
        self.assertEqual(
            web_search._website_root_url(
                "https://sites.google.com/view/pune-paws/animal-care"
            ),
            tenant_root,
        )
        self.assertTrue(
            web_search._same_website(
                tenant_root,
                "https://sites.google.com/view/pune-paws/contact",
            )
        )
        self.assertFalse(
            web_search._same_website(
                tenant_root,
                "https://sites.google.com/view/other-rescue/contact",
            )
        )

    def test_model_claims_and_phone_are_rederived_from_official_pages(self):
        official_url = "https://pune-paws.example/"
        evidence = (
            "Pune Paws Trust is a registered non-profit charity that operates an "
            "animal rescue centre in Pune and treats injured dogs every day."
        )
        official_page = self._page(
            evidence,
            identity="Pune Paws Trust",
            hostname="pune-paws.example",
        )
        raw_option = {
            "name": "Pune Paws Trust",
            "official_url": official_url,
            "phone": "+91 98100 36255",
            "animal_rescue_evidence": "Fabricated model claim",
            "service_area_evidence": "Fabricated model claim for Pune",
        }

        with patch.object(
            web_search,
            "_discover_ngo_candidates",
            return_value=[],
        ), patch.object(
            web_search,
            "_request_structured_web_search",
            return_value=({"organizations": [raw_option]}, set()),
        ), patch.object(
            web_search,
            "_fetch_public_page_text",
            return_value=official_page,
        ):
            result = web_search._run_structured_ngo_search(
                "Find current Pune animal help",
                required_city="Pune",
                required_region="Maharashtra",
                no_results_place="Pune, Maharashtra, India",
                discover_exact_city_candidates=True,
            )

        self.assertEqual(result.result_kind, "verified_options")
        self.assertEqual(len(result.organizations), 1)
        self.assertEqual(result.organizations[0]["phone"], "")
        self.assertNotIn("98100", result.response)
        self.assertNotIn("Fabricated model claim", result.response)

    def test_identity_only_site_cannot_validate_model_service_claims(self):
        identity_only = self._page(
            "Pune Paws Trust",
            identity="Pune Paws Trust",
            hostname="pune-paws.example",
        )
        raw_option = {
            "name": "Pune Paws Trust",
            "official_url": "https://pune-paws.example/",
            "phone": "+91 98100 36255",
        }
        with patch.object(
            web_search,
            "_discover_ngo_candidates",
            return_value=[],
        ), patch.object(
            web_search,
            "_request_structured_web_search",
            return_value=({"organizations": [raw_option]}, set()),
        ), patch.object(
            web_search,
            "_fetch_public_page_text",
            return_value=identity_only,
        ):
            result = web_search._run_structured_ngo_search(
                "Find current Pune animal help",
                required_city="Pune",
                required_region="Maharashtra",
                no_results_place="Pune, Maharashtra, India",
                discover_exact_city_candidates=True,
            )

        self.assertEqual(result.result_kind, "no_results")
        self.assertEqual(result.organizations, [])
        self.assertNotIn("98100", result.response)

    def test_phone_filter_keeps_supported_formats_and_rejects_placeholders(self):
        page = " ".join(
            (
                "Call 1800 123 4567",
                "or +91–98828–58631",
                "or 0091 98827 87777",
                "not 0987654321",
                "not 123-123-1234",
            )
        )

        phones = web_search._extract_phone_numbers_from_page(page)

        self.assertIn("1800 123 4567", phones)
        self.assertIn("+91–98828–58631", phones)
        self.assertIn("0091 98827 87777", phones)
        self.assertFalse(any("0987654321" in phone for phone in phones))
        self.assertFalse(any("123-123-1234" in phone for phone in phones))

    def test_normal_exact_city_search_merges_discovery_and_model_candidates(self):
        dar = self._renderable_option(
            "Dharamsala Animal Rescue Trust",
            "https://dharamsalaanimalrescue.org/",
        )
        tibet_charity = self._renderable_option(
            "Tibet Charity",
            "https://tibetcharity.in/animal-care/",
            phone="+91 98827 87777",
        )
        candidates = [
            {
                "name": "Dharamsala Animal Rescue Trust",
                "possible_official_url": "https://dharamsalaanimalrescue.org/",
            },
        ]
        model_option = {
            "name": "Tibet Charity",
            "official_url": "https://tibetcharity.in/animal-care/",
            # This value must never be copied; deterministic verification
            # supplies the official-site phone in ``tibet_charity`` instead.
            "phone": "+91 98100 36255",
        }

        with patch.object(
            web_search,
            "_discover_ngo_candidates",
            return_value=candidates,
        ) as discover, patch.object(
            web_search,
            "_core_city_official_discovery_seeds",
            return_value=[],
        ), patch.object(
            web_search,
            "_deterministically_verify_discovered_candidates",
            side_effect=[[dar], [tibet_charity]],
        ) as deterministic, patch.object(
            web_search,
            "_request_structured_web_search",
            return_value=({"organizations": [model_option]}, set()),
        ) as model_search, patch.object(
            web_search,
            "_validate_ngo_option",
        ) as direct_validator:
            result = web_search._run_structured_ngo_search(
                "Find current Dharamshala animal-rescue NGOs",
                required_city="Dharamshala",
                required_region="Himachal Pradesh",
                no_results_place="Dharamshala, Himachal Pradesh, India",
                discover_exact_city_candidates=True,
            )

        discover.assert_called_once()
        self.assertEqual(deterministic.call_count, 2)
        model_search.assert_called_once()
        direct_validator.assert_not_called()
        self.assertEqual(
            [option["name"] for option in result.organizations],
            ["Dharamsala Animal Rescue Trust", "Tibet Charity"],
        )
        self.assertEqual(result.organizations[1]["phone"], "+91 98827 87777")
        self.assertNotIn("98100", result.response)
        self.assertTrue(result.candidate_discovery_complete)
        self.assertEqual(result.result_kind, "verified_options")

    def test_incomplete_exact_city_discovery_is_not_cached(self):
        result = web_search.SearchResult(
            response="One verified option",
            searched=True,
            result_kind="verified_options",
            organizations=[
                self._renderable_option(
                    "Tibet Charity",
                    "https://tibetcharity.in/animal-care/",
                )
            ],
            candidate_discovery_complete=False,
        )

        with patch.object(web_search.db, "save_ngo_search_cache") as save_cache:
            web_search._save_cached_ngo_result(
                "ngo:v21:in:himachal pradesh:dharamshala:en:5",
                result,
                city="Dharamshala",
                region="Himachal Pradesh",
                country_code="IN",
                language="en",
            )

        save_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
