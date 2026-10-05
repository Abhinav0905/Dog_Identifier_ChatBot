"""Isolated web-operation checks: no user data, models or network senders."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class WebOperationsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network disabled in web operation checks"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="gaia-web-ops-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = types.ModuleType("config")
        for key, value in {
            "DB_PATH": self.folder / "test.db", "STORAGE_DIR": self.folder / "images",
            "SLACK_WEBHOOK_URL": "", "ALERT_WEBHOOK_URL": "",
            "RAG_VECTOR_BACKEND": "sqlite", "OPENAI_API_KEY": "offline-fixture",
            "CHROMA_PERSIST_DIR": self.folder / "chroma", "CHROMA_COLLECTION_NAME": "fixture",
            "PINECONE_API_KEY": "",
        }.items():
            setattr(self.config, key, value)
        self.config.STORAGE_DIR.mkdir()
        self.modules = patch.dict(sys.modules, {"config": self.config})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.db = load_module("_ops_test_database", "database.py")
        sys.modules["database"] = self.db
        self.db.init_db(); self.db.init_db()
        self.alerts = load_module("_ops_test_alerts", "services/alerts.py")
        self.log = patch.object(self.alerts, "_log_alert")
        self.log.start(); self.addCleanup(self.log.stop)
        self.ops = load_module("_ops_test_operations", "services/web_operations.py")
        self.ops.initialize()
        self.sender = patch.object(self.alerts, "_send_webhook")
        self.send = self.sender.start(); self.addCleanup(self.sender.stop)
        self.slack = patch.object(self.alerts, "_send_slack")
        self.slack.start(); self.addCleanup(self.slack.stop)
        self.alerts.ALERT_WEBHOOK_URL = "https://example.invalid/fixture"
        self.triage = {"severity": "high", "severity_score": 8, "confidence": .9}

    def due(self):
        with self.db.get_db() as conn:
            conn.execute("UPDATE alerts SET delivery_retry_at='2000-01-01T00:00:00+00:00'")

    def test_alert_retry_ack_and_duplicate_suppression(self):
        incident = self.db.create_incident("web-fixture")
        self.send.side_effect = TimeoutError("fixture")
        first = self.alerts.send_alert(incident, self.triage)
        self.assertEqual(first.status, "failed")
        self.assertFalse(self.alerts.acknowledge_alert(first.alert_id, "operator"))
        repeated = self.alerts.send_alert(incident, self.triage)
        self.assertEqual(repeated.alert_id, first.alert_id)
        self.assertEqual(self.send.call_count, 1)
        self.due(); self.send.side_effect = None
        self.assertEqual(self.alerts.retry_failed_alerts()["delivered"], 1)
        self.assertTrue(self.alerts.acknowledge_alert(first.alert_id, "operator"))
        self.assertEqual(self.alerts.retry_failed_alerts()["attempted"], 0)
        self.assertEqual(self.db.get_incident(incident)["status"], "alerted")

    def test_alert_retry_cap_and_operator_visibility(self):
        incident = self.db.create_incident("web-fixture")
        self.send.side_effect = TimeoutError("fixture")
        self.alerts.send_alert(incident, self.triage)
        for _ in range(4):
            self.due(); self.alerts.retry_failed_alerts()
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.ops.snapshot()["alerts_needing_operator_attention"], 1)

    def test_alert_logs_keep_ids_and_types_without_private_payloads(self):
        self.log.stop()
        incident = self.db.create_incident("web-fixture")
        self.send.side_effect = TimeoutError("https://private.example/secret-token")
        triage = {**self.triage, "indicators": ["private-clinical-detail"]}
        with self.assertLogs(self.alerts.logger, level="INFO") as captured:
            result = self.alerts.send_alert(incident, triage, {"lat": 12.34567, "lng": 76.54321, "source": "private-source"})
        output = "\n".join(captured.output)
        self.assertIn(incident, output)
        self.assertIn(result.alert_id, output)
        self.assertIn("TimeoutError", output)
        self.assertIn("status=failed", output)
        for private in ("12.34567", "76.54321", "private-source", "private-clinical-detail", "secret-token", "private.example"):
            self.assertNotIn(private, output)

    def test_retention_is_preview_by_default_and_preserves_active_shared_images(self):
        photo = self.config.STORAGE_DIR / "shared.jpg"; photo.write_bytes(b"fixture")
        closed = self.db.create_incident("expired-web", image_blob_path=str(photo), status="closed")
        active = self.db.create_incident("active-web", image_blob_path=str(photo), status="new")
        with self.db.get_db() as conn:
            conn.execute("UPDATE incidents SET updated_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.ops.retention(closed_incident_days=30)["eligible_reports"], 1)
        self.assertTrue(photo.exists()); self.assertIsNotNone(self.db.get_incident(closed)["image_blob_path"])
        self.assertEqual(self.ops.retention(closed_incident_days=30, apply=True)["scrubbed_reports"], 1)
        self.assertTrue(photo.exists()); self.assertIsNone(self.db.get_incident(closed)["image_blob_path"])
        self.assertEqual(self.db.get_incident(active)["reporter_session_id"], "active-web")

    def test_readiness_is_passive_and_records_failures_without_error_payloads(self):
        self.db.insert_rag_chunk("fixture", "fixture", 0, "knowledge fixture", None)
        self.assertEqual(self.ops.readiness()["models"], "not_observed")
        self.ops.record_event("model:care", "ok", 12)
        self.assertEqual(self.ops.readiness()["models"], "recent_success")
        self.ops.record_event("model:care", "error", 12, "https://secret.invalid/token")
        state = self.ops.readiness()
        self.assertEqual(state["models"], "recent_failure")
        self.assertEqual(state["model_observations"][0]["error_type"], "")
        self.send.assert_not_called()

    def test_readiness_checks_active_vector_collection_and_embedding_warmth(self):
        from services import chroma_rag
        self.config.RAG_VECTOR_BACKEND = "chroma"
        self.config.CHROMA_PERSIST_DIR.mkdir()
        (self.config.CHROMA_PERSIST_DIR / "chroma.sqlite3").write_bytes(b"fixture")
        client = MagicMock()
        client.get_collection.return_value.count.return_value = 7
        embedder = MagicMock()
        embedder.cache_info.return_value = types.SimpleNamespace(currsize=0)
        with patch.object(chroma_rag, "_client", return_value=client), patch.object(chroma_rag, "_embedder", embedder):
            self.assertEqual(self.ops.readiness()["knowledge"]["status"], "embedding_cold")
            embedder.cache_info.return_value = types.SimpleNamespace(currsize=1)
            state = self.ops.readiness()
            self.assertEqual(state["knowledge"]["chunks"], 7)
            self.assertEqual(state["knowledge"]["embedding_status"], "warm")
            embedder.assert_not_called()

    def test_backup_restore_check_and_overwrite_protection(self):
        backup = load_module("_ops_test_backup", "scripts/web_release_backup.py")
        photo = self.config.STORAGE_DIR / "photo.jpg"; photo.write_bytes(b"fixture")
        self.db.create_incident("web-fixture", image_blob_path=str(photo))
        destination = self.folder / "backup"
        with self.assertRaises(ValueError):
            backup.create_backup(self.config.DB_PATH, self.config.STORAGE_DIR, destination)
        result = backup.create_backup(self.config.DB_PATH, self.config.STORAGE_DIR, destination, quiesced=True)
        self.assertEqual(result["restore_check"], "passed")
        with self.assertRaises(ValueError):
            backup.create_backup(self.config.DB_PATH, self.config.STORAGE_DIR, destination, quiesced=True)
        (destination / "storage" / "photo.jpg").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            backup.verify_backup(destination)


if __name__ == "__main__":
    unittest.main()
