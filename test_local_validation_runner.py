"""Offline checks for local validation data isolation and Chroma snapshots."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_local_validation as runner


class TestLocalValidationRunner(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def knowledge_database(path, with_data=False):
        with closing(sqlite3.connect(path)) as database, database:
            database.executescript(
                "CREATE TABLE rag_chunks (id INTEGER PRIMARY KEY, doc_file TEXT, title TEXT, "
                "chunk_index INTEGER, content TEXT, embedding TEXT);"
                "CREATE TABLE incidents (secret TEXT);"
                "CREATE TABLE conversations (secret TEXT);"
                "CREATE TABLE ngo_search_cache (secret TEXT);"
            )
            if with_data:
                database.execute(
                    "INSERT INTO rag_chunks VALUES (42, 'care.pdf', 'Dog care', 1, 'Care guidance', NULL)"
                )
                for table in ("incidents", "conversations", "ngo_search_cache"):
                    database.execute(f"INSERT INTO {table} VALUES ('source-private-data')")

    @staticmethod
    def vector_store(path):
        path.mkdir()
        with closing(sqlite3.connect(path / "chroma.sqlite3")) as database, database:
            database.executescript(
                "CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);"
                "CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT);"
                "CREATE TABLE embeddings (segment_id TEXT, embedding_id TEXT);"
                "INSERT INTO collections VALUES ('collection-id', 'dar-rag');"
                "INSERT INTO segments VALUES ('segment-id', 'collection-id');"
                "INSERT INTO embeddings VALUES ('segment-id', 'chunk-42');"
            )
        (path / "segment-id").mkdir()
        (path / "segment-id" / "data_level0.bin").write_bytes(b"test-index-data")

    def test_only_knowledge_rows_are_copied_with_original_ids(self):
        source, local = self.root / "source.db", self.root / "local.db"
        self.knowledge_database(source, with_data=True)
        self.knowledge_database(local)
        original_bytes = source.read_bytes()

        self.assertEqual(runner.copy_rag(source, local), 1)

        with closing(sqlite3.connect(local)) as database:
            self.assertEqual(database.execute("SELECT id FROM rag_chunks").fetchall(), [(42,)])
            for table in ("incidents", "conversations", "ngo_search_cache"):
                self.assertEqual(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(source.read_bytes(), original_bytes)
        with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
            runner.copy_rag(source, local)

    def test_source_connection_rejects_writes(self):
        source = self.root / "source.db"
        self.knowledge_database(source, with_data=True)
        with runner.readonly_database(source) as database:
            with self.assertRaises(sqlite3.OperationalError):
                database.execute("DELETE FROM rag_chunks")

    def test_chroma_snapshot_copies_database_and_index(self):
        source, local = self.root / "vectors", self.root / "local-vectors"
        self.vector_store(source)
        before = runner.tree_state(source)

        self.assertEqual(runner.copy_chroma(source, local, "dar-rag"), 1)
        self.assertEqual(runner.tree_state(source), before)
        self.assertEqual((local / "segment-id" / "data_level0.bin").read_bytes(), b"test-index-data")
        self.assertEqual(runner.chroma_count(local, "dar-rag"), 1)

    def test_source_write_during_chroma_copy_is_rejected(self):
        source = self.root / "vectors"
        self.vector_store(source)
        with patch.object(runner, "tree_state", side_effect=[{"index": (1, 1)}, {"index": (1, 2)}]):
            with self.assertRaisesRegex(RuntimeError, "changed during copying"):
                runner.copy_chroma(source, self.root / "local-vectors", "dar-rag")

    def test_missing_chroma_is_explicitly_empty(self):
        local = self.root / "local-vectors"
        self.assertEqual(runner.copy_chroma(self.root / "missing", local, "dar-rag"), 0)
        self.assertTrue(local.is_dir())
        self.assertEqual(runner.chroma_count(local, "dar-rag"), 0)

    def test_local_overrides_bypass_file_first_cookie_and_twilio_settings(self):
        configuration = SimpleNamespace(
            CONVERSATION_COOKIE_SECURE=True,
            TWILIO_ACCOUNT_SID="test-configured-account",
            TWILIO_AUTH_TOKEN="test-configured-token",
            TWILIO_WEBHOOK_BASE_URL="https://example.invalid",
            ALERT_WEBHOOK_URL="https://example.invalid/alert",
            SLACK_WEBHOOK_URL="https://example.invalid/slack",
        )
        with patch.dict("sys.modules", {"config": configuration}), patch.dict(os.environ, {}):
            runner.configure_local(self.root, "test-collection")
            self.assertEqual(os.environ["DB_PATH"], str(self.root / "dharamsala.db"))
            self.assertEqual(os.environ["CHROMA_PERSIST_DIR"], str(self.root / "chroma_db"))
            self.assertEqual(os.environ["STORAGE_DIR"], str(self.root / "storage"))
            self.assertEqual(os.environ["CHROMA_COLLECTION_NAME"], "test-collection")
            self.assertFalse(configuration.CONVERSATION_COOKIE_SECURE)
            for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ALERT_WEBHOOK_URL", "SLACK_WEBHOOK_URL"):
                self.assertEqual(getattr(configuration, name), "")

    def test_restart_preserves_local_conversations_and_does_not_recopy(self):
        state = self.root / "state"
        state.mkdir()
        local = state / "dharamsala.db"
        self.knowledge_database(local, with_data=True)
        (state / runner.MANIFEST).write_text(json.dumps({
            "version": 1, "collection": "dar-rag", "rag_chunks": 1, "chroma_vectors": 0,
        }))
        source = {
            "database": self.root / "source.db", "chroma": self.root / "source-vectors",
            "storage": self.root / "source-storage", "collection": "dar-rag",
        }
        configuration = SimpleNamespace()
        with patch.object(runner, "configure_local"), \
             patch.object(runner, "copy_rag") as copy_rag, \
             patch.object(runner, "copy_chroma") as copy_chroma, \
             patch.dict("sys.modules", {
                 "config": configuration,
                 "services": SimpleNamespace(chroma_rag=SimpleNamespace()),
             }), patch.dict(os.environ, {}):
            result = runner.prepare(state, source)
        self.assertIn("SQLite fallback", result["backend"])
        copy_rag.assert_not_called()
        copy_chroma.assert_not_called()
        with closing(sqlite3.connect(local)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)

    def test_original_database_directory_is_rejected_before_imports(self):
        source = {
            "database": self.root / "dharamsala.db", "chroma": self.root / "chroma_db",
            "storage": self.root / "storage", "collection": "dar-rag",
        }
        with patch.object(runner, "configure_local") as configure:
            with self.assertRaisesRegex(RuntimeError, "separate from the original"):
                runner.prepare(self.root, source)
        configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
