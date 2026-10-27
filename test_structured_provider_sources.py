"""Published structured contacts must survive fetching, without cross-unit repair."""
import json
import unittest
from bs4 import BeautifulSoup
from services import search_evidence as evidence, web_search


class TestStructuredProviderSources(unittest.TestCase):
    def test_fetched_graph_preserves_same_clinic_address_phone_and_animal_service(self):
        clinic={"@type":["VeterinaryCare","LocalBusiness"],"name":"River Pet Clinic",
                "description":"Treats dogs and cats in Lakeside. " + "Published clinic background information. " * 8,"telephone":"04842345678",
                "address":{"@type":"PostalAddress","streetAddress":"12 River Road","addressLocality":"Lakeside","addressRegion":"Kerala","postalCode":600001,"addressCountry":"IN"}}
        soup=BeautifulSoup('<script type="application/ld+json">'+json.dumps({"@graph":[clinic]})+'</script>','html.parser')
        blocks=web_search._structured_contact_blocks(soup)
        self.assertEqual(len(blocks),1)
        address="12 River Road, Lakeside, Kerala, 600001, IN"
        self.assertIn(address,blocks[0]);self.assertIn("04842345678",blocks[0])
        page=web_search.PublicPageText(blocks[0],identity_text="River Pet Clinic",blocks=blocks,source_hostname="riverpet.example",is_html=True)
        url="https://riverpet.example/contact"
        review={"answer":"draft","request_satisfied":True,"provider_claims":[{"institution":"River Pet Clinic","location":address,"service_type":"clinical_treatment","source_url":url,"evidence_quote":blocks[0]}],
                "contact_claims":[{"institution":"River Pet Clinic","phone":"04842345678","source_url":url,"evidence_quote":blocks[0],"matches_requested_entity":True}],"cited_source_urls":[url]}
        result=evidence._validate_review(review,{url:page},clinical_discovery=True,require_small_animal=True,requested_place="Lakeside, Kerala, India",requested_city="Lakeside",requested_location_text="Lakeside, Kerala",phone_required=True,address_required=True)
        self.assertTrue(result.request_satisfied)
        self.assertIn("04842345678",result.answer);self.assertIn(address,result.answer)
        wrong=evidence._validate_review(review,{url:page},requested_institution="Other College Clinical Complex",phone_only=True,clinical_only=True)
        self.assertNotIn("04842345678",wrong.answer if wrong else "")

    def test_staff_foreign_records_and_research_only_cannot_become_local_clinical_proof(self):
        records=[{"@type":"Person","name":"Professor River","telephone":"9876543210"},
                 {"@type":"VeterinaryCare","name":"Foreign Animal Clinic","telephone":"9876543210","address":{"addressCountry":"US","addressLocality":"Lakeside"}},
                 {"@type":"VeterinaryCare","name":"Research Animal Unit","telephone":"9876543210","description":"No clinical services; research only."}]
        soup=BeautifulSoup('<script type="application/ld+json">'+json.dumps(records)+'</script><script type="application/ld+json">not valid json</script>','html.parser')
        blocks=web_search._structured_contact_blocks(soup)
        self.assertEqual(len(blocks),1)
        self.assertIn("Research Animal Unit",blocks[0]);self.assertNotIn("Professor",blocks[0])
        self.assertFalse(evidence._provider_service_is_grounded("Research Animal Unit","clinical_treatment",blocks[0],require_small_animal=False))


if __name__=="__main__":unittest.main()
