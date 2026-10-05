"""Offline deployment control-flow checks; never connects to Docker or a host."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
root = pathlib.Path(os.environ["FAKE_DOCKER_ROOT"])
state_path = root / "state.json"
state = json.loads(state_path.read_text())
a = sys.argv[1:]
state["calls"].append(a)
exit_code = 0
cmd = a[0]
if cmd == "inspect":
    container = state["containers"].get(a[-1])
    if container is None:
        exit_code = 1
    elif "--format" in a:
        print(str(container["running"]).lower() if "Running" in a[2] else container["image"])
elif cmd == "image":
    print("sha256:candidate")
elif cmd == "create":
    if os.environ.get("FAKE_CREATE_FAIL"):
        exit_code = 1
    else:
        name = a[a.index("--name") + 1]
        mount = a[a.index("-v") + 1].split(":")[0]
        state["containers"][name] = {"running": False, "image": "sha256:candidate", "mount": mount}
elif cmd == "stop":
    state["containers"][a[-1]]["running"] = False
elif cmd == "rename":
    state["containers"][a[2]] = state["containers"].pop(a[1])
elif cmd == "start":
    container = state["containers"][a[1]]
    container["running"] = True
    if container["image"] == "sha256:candidate":
        (pathlib.Path(container["mount"]) / "database-fixture").write_text("candidate migrated")
elif cmd == "rm":
    state["containers"].pop(a[-1], None)
elif cmd == "cp":
    old_path = a[1].split(":", 1)[1]
    source = root / ("legacy-chroma" if old_path == "/app/chroma_db" else "legacy-hub")
    if source.is_dir():
        shutil.copytree(source, a[2])
    else:
        exit_code = 1
elif cmd == "exec":
    # Evaluate the deployment's actual readiness predicate with an in-memory
    # HTTP response. This never opens a network connection.
    import io, urllib.request
    health = {"database": "ready", "knowledge": {"backend": "chroma", "status": "ready"},
              "models": os.environ.get("FAKE_MODEL_STATE", "not_observed"),
              "india_only_scope": True, "india_boundary_loaded": True}
    urllib.request.urlopen = lambda *args, **kwargs: io.StringIO(json.dumps(health))
    try:
        exec(a[4], {})
    except SystemExit as exc:
        exit_code = exc.code
elif cmd != "build":
    raise RuntimeError("Unexpected mock command " + cmd)
state_path.write_text(json.dumps(state))
sys.exit(exit_code)
'''


class DeploymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        target = self.repo / "deploy" / "ec2"
        target.mkdir(parents=True)
        shutil.copyfile(Path(__file__).parent / "deploy/ec2/deploy.sh", target / "deploy.sh")
        self.data = self.repo / ".deploy-data"
        self.data.mkdir()
        (self.data / "database-fixture").write_text("original")
        (self.repo / ".env").write_text("EXAMPLE_ONLY=unchanged\n")
        for folder in ("legacy-chroma", "legacy-hub"):
            (self.root / folder).mkdir()
            (self.root / folder / "source-fixture").write_text(folder)
        state = {"calls": [], "containers": {"gaia-chatbot": {
            "running": True, "image": "sha256:original", "mount": str(self.data)}}}
        (self.root / "state.json").write_text(json.dumps(state))
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "docker").write_text(FAKE_DOCKER)
        (binary / "docker").chmod(0o755)
        self.env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                    "FAKE_DOCKER_ROOT": str(self.root), "STARTUP_TIMEOUT_SECONDS": "10"}
        for key in ("APP_NAME", "IMAGE_NAME", "HOST_PORT", "ENV_FILE", "DATA_DIR", "BACKUP_ROOT",
                    "CHROMA_SEED_DIR", "EMBEDDING_CACHE_SEED_DIR", "FAKE_CREATE_FAIL", "FAKE_MODEL_STATE"):
            self.env.pop(key, None)

    def run_deploy(self):
        return subprocess.run(["bash", str(self.repo / "deploy/ec2/deploy.sh")],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def state(self):
        return json.loads((self.root / "state.json").read_text())

    def test_candidate_creation_and_persistent_seed(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        calls = [call[0] for call in state["calls"]]
        self.assertLess(calls.index("create"), calls.index("stop"))
        self.assertTrue(state["containers"]["gaia-chatbot"]["running"])
        self.assertEqual((self.data / "chroma_db/source-fixture").read_text(), "legacy-chroma")
        self.assertEqual((self.data / "model-cache/hub/source-fixture").read_text(), "legacy-hub")
        self.assertEqual((self.repo / ".env").read_text(), "EXAMPLE_ONLY=unchanged\n")
        backup = next((self.repo / ".deploy-backups").iterdir())
        self.assertEqual((backup / "data/database-fixture").read_text(), "original")
        previous = [value for key, value in state["containers"].items() if "-previous-" in key]
        self.assertEqual(len(previous), 1)
        self.assertFalse(previous[0]["running"])

    def test_failed_health_restores_data_and_previous_container(self):
        self.env["STARTUP_TIMEOUT_SECONDS"] = "0"
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        state = self.state()
        self.assertEqual(list(state["containers"]), ["gaia-chatbot"])
        self.assertEqual(state["containers"]["gaia-chatbot"]["image"], "sha256:original")
        self.assertTrue(state["containers"]["gaia-chatbot"]["running"])
        self.assertEqual((self.data / "database-fixture").read_text(), "original")
        backup = next((self.repo / ".deploy-backups").iterdir())
        self.assertEqual((backup / "failed-data/database-fixture").read_text(), "candidate migrated")

    def test_known_model_failure_or_missing_configuration_blocks_startup(self):
        self.env["STARTUP_TIMEOUT_SECONDS"] = "1"
        for model_state in ("recent_failure", "not_configured"):
            with self.subTest(models=model_state):
                self.env["FAKE_MODEL_STATE"] = model_state
                result = self.run_deploy()
                self.assertNotEqual(result.returncode, 0)
                previous = self.state()["containers"]["gaia-chatbot"]
                self.assertEqual(previous["image"], "sha256:original")
                self.assertTrue(previous["running"])
                self.assertEqual((self.data / "database-fixture").read_text(), "original")

    def test_existing_vectors_not_reseeded_and_create_failure_leaves_old_running(self):
        (self.data / "chroma_db").mkdir()
        (self.data / "chroma_db/source-fixture").write_text("persistent")
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.data / "chroma_db/source-fixture").read_text(), "persistent")
        self.env["FAKE_CREATE_FAIL"] = "1"
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.state()["containers"]["gaia-chatbot"]["running"])
        self.assertEqual((self.data / "chroma_db/source-fixture").read_text(), "persistent")


if __name__ == "__main__":
    unittest.main()
