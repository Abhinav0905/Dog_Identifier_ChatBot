"""Offline fixtures for changed-Markdown refresh; no model or live-store calls."""
import sqlite3
import unittest

from scripts.refresh_web_knowledge import refresh_documents


class FakeCollection:
    def __init__(self):
        self.rows = {
            "stale-md": ("old unsafe advice", {"doc_file": "dog_behaviour.md"}),
            "pdf-id": ("PDF reference", {"doc_file": "existing.pdf"}),
            "unrelated-id": ("Other reference", {"doc_file": "other.md"}),
        }
        self.fail_upsert = False
        self.deleted = []

    def get(self, *, where, include):
        rows = [(key, value) for key, value in self.rows.items() if value[1]["doc_file"] == where["doc_file"]]
        return {"ids": [key for key, _ in rows], "documents": [value[0] for _, value in rows],
                "metadatas": [value[1] for _, value in rows]}

    def upsert(self, *, ids, documents, metadatas, embeddings):
        if self.fail_upsert:
            raise RuntimeError("controlled failed upsert")
        self.rows.update({key: (text, meta) for key, text, meta in zip(ids, documents, metadatas)})

    def delete(self, *, ids):
        self.deleted.extend(ids)
        for key in ids:
            self.rows.pop(key)


class KnowledgeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE rag_chunks (doc_file TEXT, title TEXT, chunk_index INTEGER, content TEXT, embedding TEXT)")
        self.conn.executemany("INSERT INTO rag_chunks VALUES (?, ?, 0, ?, ?)", [
            ("dog_behaviour.md", "Old", "old unsafe advice", None),
            ("existing.pdf", "PDF", "PDF reference", "[0.2]"),
            ("other.md", "Other", "Other reference", None),
        ])
        self.conn.commit()
        self.collection = FakeCollection()
        self.documents = {"dog_behaviour.md": [{"id": "fresh-md", "doc_file": "dog_behaviour.md",
                            "title": "Humane dog safety", "chunk_index": 0,
                            "content": "Keep distance; avoid threatening dogs.", "source_url": ""}]}

    def test_preview_then_refresh_removes_only_obsolete_changed_document_ids(self):
        before = list(self.conn.execute("SELECT * FROM rag_chunks"))
        plan = refresh_documents(self.documents, self.conn, collection=self.collection)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["changed_documents"][0]["obsolete_vector_ids"], 1)
        self.assertEqual(list(self.conn.execute("SELECT * FROM rag_chunks")), before)
        self.assertIn("stale-md", self.collection.rows)
        refresh_documents(self.documents, self.conn, collection=self.collection,
                          encode=lambda texts: [[1.0] for _ in texts], apply=True)
        self.assertEqual(self.collection.deleted, ["stale-md"])
        self.assertEqual(set(self.collection.rows), {"fresh-md", "pdf-id", "unrelated-id"})
        retained = list(self.conn.execute("SELECT * FROM rag_chunks WHERE doc_file != 'dog_behaviour.md'"))
        self.assertEqual(retained, before[1:])
        self.assertEqual(refresh_documents(self.documents, self.conn, collection=self.collection)["changed_documents"], [])

    def test_upsert_failure_preserves_old_sqlite_and_vectors(self):
        self.collection.fail_upsert = True
        before = list(self.conn.execute("SELECT * FROM rag_chunks"))
        with self.assertRaisesRegex(RuntimeError, "controlled failed upsert"):
            refresh_documents(self.documents, self.conn, collection=self.collection,
                              encode=lambda texts: [[1.0] for _ in texts], apply=True)
        self.assertEqual(list(self.conn.execute("SELECT * FROM rag_chunks")), before)
        self.assertEqual(self.collection.deleted, [])
        self.assertIn("stale-md", self.collection.rows)


if __name__ == "__main__":
    unittest.main()
