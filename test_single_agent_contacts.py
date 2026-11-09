"""Contact evidence regressions: citation presence must not authorize a phone."""
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from simple_agent.agent import AgentConfig, SingleAgent
from simple_agent.contacts import ContactVerificationTool
from test_simple_agent_modular import response, message

URL = "https://example.org/contact"
CLAIM = {"institution": "Veterinary Hospital Shamshi", "phone": "01902 265074", "source_url": URL}
PAGE = "Veterinary Hospital Shamshi\nShamshi, Kullu, Himachal Pradesh\nPhone: 01902 265074"


class ContactVerificationTests(unittest.TestCase):
    def check(self, page, claim=None):
        tool = ContactVerificationTool()
        with patch("simple_agent.contacts._fetch_sources", return_value={URL: page}):
            result = tool.execute(claim or CLAIM, time.monotonic() + 10)
        return tool, result

    def test_directory_without_phone_cannot_support_model_recall(self):
        _, result = self.check("Veterinary Hospital Shamshi, Kullu. Address and reviews.")
        self.assertFalse(result["verified"])

    def test_phone_for_other_hospital_on_same_page_is_rejected(self):
        _, result = self.check("Veterinary Hospital Shamshi\nOther Veterinary Hospital\nPhone: 01902 265074")
        self.assertFalse(result["verified"])

    def test_fictional_name_is_not_mapped_to_real_hospital(self):
        _, result = self.check(PAGE, {**CLAIM, "institution": "Dorjee Memorial Veterinary Hospital"})
        self.assertFalse(result["verified"])

    def test_same_entity_contact_and_country_code_are_accepted(self):
        tool, result = self.check(PAGE)
        self.assertTrue(result["verified"])
        self.assertIn("01902 265074", result["evidence_quote"])
        self.assertEqual(tool.unsupported_spans("Phone: +91 1902 265074"), [])

    def test_general_phone_does_not_require_an_unspecified_branch_address(self):
        with patch("simple_agent.contacts._publisher_branch_phones", return_value=set()) as branches:
            _, result = self.check(PAGE)
            self.assertTrue(result["verified"])
            branches.assert_not_called()

    def test_phone_cannot_be_paired_with_another_branch_address(self):
        with patch("simple_agent.contacts._publisher_branch_phones", return_value={"1111111111"}):
            _, result = self.check(PAGE, {**CLAIM, "address": "Shamshi, Kullu, Himachal Pradesh"})
            self.assertTrue(result["verified"])
            self.assertEqual(result["phone"], "")
            self.assertIn("phone", result["unverified_fields"])

    def test_valid_address_survives_unverified_phone(self):
        _, result = self.check(PAGE, {**CLAIM, "phone": "01902 111111", "address": "Shamshi, Kullu, Himachal Pradesh"})
        self.assertTrue(result["verified"])
        self.assertEqual(result["phone"], "")
        self.assertEqual(result["address"], "Shamshi, Kullu, Himachal Pradesh")

    def test_city_qualifier_must_be_bound_to_source_entity(self):
        _, result = self.check(PAGE, {**CLAIM, "institution": "Veterinary Hospital Shamshi, Kullu"})
        self.assertTrue(result["verified"])
        _, wrong_city = self.check(PAGE, {**CLAIM, "institution": "Veterinary Hospital Shamshi, Shimla"})
        self.assertFalse(wrong_city["verified"])

    def test_last_step_reserves_answer_instead_of_another_tool_loop(self):
        client = MagicMock()
        call = {"type": "function_call", "name": "verify_contact", "call_id": "c1", "arguments": json.dumps(CLAIM)}
        client.responses.create.side_effect = [response([call]), response([message("I could not verify its phone.")], "I could not verify its phone.")]
        with patch("simple_agent.contacts._fetch_sources", return_value={}):
            row = SingleAgent(client, AgentConfig(max_steps=2)).turn("Find its phone")
        self.assertEqual(row["status"], "completed")
        self.assertNotIn("tools", client.responses.create.call_args_list[-1].kwargs)

    def test_unreadable_source_fails_closed(self):
        tool = ContactVerificationTool()
        with patch("simple_agent.contacts._fetch_sources", return_value={}):
            self.assertFalse(tool.execute(CLAIM, time.monotonic() + 10)["verified"])

    def test_address_cannot_mix_streets_with_a_different_office(self):
        page = "District Veterinary Centre Ernakulam\nPark Avenue Road, Ernakulam\nDistrict Animal Husbandry Office\nCollectorate, Kakkanad"
        _, result = self.check(page, {"institution": "District Veterinary Centre Ernakulam", "phone": "", "address": "Park Avenue Road, near the District Collectorate, Kakkanad", "source_url": URL})
        self.assertFalse(result["verified"])

    def test_source_bound_address_is_verified_without_a_phone(self):
        _, result = self.check(PAGE, {**CLAIM, "phone": "", "address": "Shamshi, Kullu, Himachal Pradesh"})
        self.assertTrue(result["verified"])
        self.assertEqual(result["address"], "Shamshi, Kullu, Himachal Pradesh")

    def test_unsafe_url_and_expired_budget_never_fetch(self):
        tool = ContactVerificationTool()
        with patch("simple_agent.contacts._fetch_sources") as fetch:
            for url in ["http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd"]:
                self.assertFalse(tool.execute({**CLAIM, "source_url": url}, time.monotonic() + 10)["verified"])
            self.assertFalse(tool.execute(CLAIM, time.monotonic() - 1)["verified"])
            fetch.assert_not_called()

    def test_url_ids_postcodes_and_dates_are_not_removed(self):
        text = "Kullu 175101. Checked 2026-11-09. https://example.org/doc/1234567890"
        self.assertEqual(ContactVerificationTool().withhold_unsupported(text), text)

    def test_repair_can_keep_address_and_remove_unsupported_phone(self):
        client = MagicMock()
        client.responses.create.side_effect = [
            response([message("Veterinary Hospital Shamshi: 01902 265074")], "Veterinary Hospital Shamshi: 01902 265074"),
            response([message("Veterinary Hospital Shamshi, Kullu. I could not verify its phone.")], "Veterinary Hospital Shamshi, Kullu. I could not verify its phone.")]
        row = SingleAgent(client).turn("Give its address and phone")
        self.assertEqual(row["status"], "completed")
        self.assertNotIn("265074", row["answer"])
        self.assertIn("Kullu", row["answer"])
        self.assertTrue(any(e["stage"] == "contact_answer_repair" for e in row["trace"]))

    def test_final_guard_withholds_number_if_model_ignores_verification(self):
        client = MagicMock()
        client.responses.create.return_value = response([message("Phone: 01902 265074")], "Phone: 01902 265074")
        row = SingleAgent(client, AgentConfig(max_steps=1)).turn("Give the phone")
        self.assertNotIn("265074", row["answer"])
        self.assertTrue(any(e["stage"] == "contact_withheld" for e in row["trace"]))

    def test_verified_number_only_followup_uses_new_evidence(self):
        client = MagicMock()
        call = {"type": "function_call", "name": "verify_contact", "call_id": "c1", "arguments": json.dumps(CLAIM)}
        client.responses.create.side_effect = [response([call]), response([message("01902 265074")], "01902 265074")]
        history = [{"role": "assistant", "content": "Veterinary Hospital Shamshi, phone 01902 265074"}]
        with patch("simple_agent.contacts._fetch_sources", return_value={URL: PAGE}):
            row = SingleAgent(client).turn("Only its phone number", history)
        self.assertEqual(row["answer"], "01902 265074")
        self.assertEqual(row["citations"][0]["url"], URL)

    def test_ordinary_small_numeric_answer_is_preserved(self):
        self.assertEqual(ContactVerificationTool().withhold_unsupported("Wash for 15 minutes."), "Wash for 15 minutes.")


if __name__ == "__main__":
    unittest.main()
