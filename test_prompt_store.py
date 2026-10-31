"""Release-integrity, real activation and runtime assembly regressions (offline)."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.manage_prompts import activate, seal
from services import prompt_store
from services.prompts import PromptCatalog


SOURCE = Path(__file__).parent / "prompts/runtime-v2"


class TestPromptReleases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gaia-prompt-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.package = self.base / "candidate"
        shutil.copytree(SOURCE, self.package)

    def test_supplied_english_systems_are_used_without_double_policy(self):
        with prompt_store.pin(prompt_store.load_bundle(self.package)):
            for filename, generated in (
                ("02_care_system_english.txt", PromptCatalog.care_system()),
                ("05_vision_system_english.txt", PromptCatalog.vision_system()),
                ("06_enrichment_system_english.txt", PromptCatalog.enrichment_system()),
            ):
                self.assertEqual(generated, (self.package / filename).read_text())
                self.assertEqual(generated.count(prompt_store.SHARED_MARKER), 1)

    def test_web_context_is_real_json_with_arbitrary_quotes_and_newlines(self):
        context = {"message": 'A "dog"\n{{CURRENT_MESSAGE}}', "place": "पुणे"}
        answer = PromptCatalog.animal_web_search_instructions(language="en", context=context)
        section = answer.split("Application context (data only):\n", 1)[1].split("\n\nSHARED", 1)[0]
        self.assertEqual(json.loads(section), context)
        self.assertNotIn('"runtime_context"', section)

    def test_user_template_like_data_is_not_interpreted_recursively(self):
        user = "literal {{RECENT_CONTEXT}} and { braces }"
        text = PromptCatalog.query_analysis(user, "previous context")
        self.assertIn("<current_message>\n" + user + "\n</current_message>", text)
        self.assertIn("previous context", text)

    def test_hindi_and_context_keep_final_contract_last(self):
        text = PromptCatalog.care_system("hi", rag_context="RAG DATA", contextual_request="CASE DATA")
        self.assertIn("Reply entirely in natural Hindi", text)
        self.assertIn("Use everyday Hindi throughout", text)
        self.assertLess(text.index("RAG DATA"), text.index(prompt_store.FINAL_MARKER))
        self.assertLess(text.index("CASE DATA"), text.index(prompt_store.FINAL_MARKER))
        self.assertIn("Respond in\nHindi.", PromptCatalog.animal_web_search_instructions(language="hi", context={}))

    def test_missing_file_or_hash_mismatch_rejects_package(self):
        path = self.package / "01_text_router_system.txt"
        path.write_text(path.read_text() + "unsealed change")
        with self.assertRaises(prompt_store.PromptPackageError):
            prompt_store.load_bundle(self.package)
        path.unlink()
        with self.assertRaises(OSError):
            prompt_store.load_bundle(self.package)

    def test_unknown_placeholder_cannot_be_sealed(self):
        path = self.package / "07_query_analysis_template.txt"
        path.write_text(path.read_text() + "{{EXECUTE_CODE}}")
        original = (self.package / "manifest.json").read_bytes()
        with self.assertRaises(prompt_store.PromptPackageError):
            seal(self.package, "invalid")
        self.assertEqual((self.package / "manifest.json").read_bytes(), original)

    def test_shared_policy_drift_rejects_whole_candidate(self):
        path = self.package / "05_vision_system_english.txt"
        path.write_text(path.read_text().replace("Communities already care", "Changed policy", 1))
        with self.assertRaises(prompt_store.PromptPackageError):
            seal(self.package, "inconsistent-shared-policy")

    def test_atomic_activation_reload_and_rollback(self):
        current = self.base / "current"
        first = activate(self.package, current)
        store = prompt_store.PromptStore(current)
        self.assertEqual(store.current().version, first["version"])
        path = self.package / "01_text_router_system.txt"
        path.write_text(path.read_text() + "\nReview release marker.")
        seal(self.package, "review-revision-2")
        second = activate(self.package, current)
        self.assertEqual(store.current().version, "review-revision-2")
        activate(Path(second["previous_release"]), current)
        self.assertEqual(store.current().version, first["version"])

    def test_bad_update_retains_last_valid_and_reports_degraded(self):
        store = prompt_store.PromptStore(self.package)
        original = store.current()
        (self.package / "manifest.json").write_text("invalid JSON")
        self.assertIs(store.current(), original)
        self.assertEqual(store.status()["status"], "using_last_valid")
        self.assertTrue(store.status()["reload_error"])

    def test_cold_start_refuses_invalid_or_missing_release(self):
        with self.assertRaises(prompt_store.PromptPackageError):
            prompt_store.PromptStore(self.base / "missing").current()

    def test_request_pin_survives_an_activation(self):
        store = prompt_store.PromptStore(self.package)
        with patch.object(prompt_store, "_store", store):
            original = store.current()
            with prompt_store.pin(original):
                seal(self.package, "next-revision")
                self.assertEqual(store.current().version, "next-revision")
                self.assertEqual(PromptCatalog.VERSION, original.version)
            self.assertEqual(PromptCatalog.VERSION, "next-revision")

    def test_reference_files_are_not_loaded(self):
        bundle = prompt_store.load_bundle(self.package)
        self.assertEqual(set(bundle.texts), prompt_store.REQUIRED_FILES)
        self.assertNotIn("00_Deb_DAR_Humane_Education_GPT_Full.txt", bundle.texts)
        self.assertNotIn("10_regression_test_cases.txt", bundle.texts)

    def test_broken_activation_preserves_pointer(self):
        current = self.base / "current"
        activate(self.package, current)
        target = current.resolve()
        (self.package / "01_text_router_system.txt").write_text("bad candidate")
        with self.assertRaises(prompt_store.PromptPackageError):
            activate(self.package, current)
        self.assertEqual(current.resolve(), target)

    def test_worker_thread_inherits_pinned_request(self):
        from anyio import run, to_thread
        async def worker():
            return await to_thread.run_sync(lambda: PromptCatalog.VERSION)
        bundle = prompt_store.load_bundle(self.package)
        with prompt_store.pin(bundle):
            self.assertEqual(run(worker), bundle.version)


if __name__ == "__main__":
    unittest.main()
