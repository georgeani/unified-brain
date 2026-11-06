import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import repository_identity as identity


class FakeStore:
    def __init__(self, values=None, run=None):
        self.values, self.run = values or {}, run or {}

    def get(self, key):
        return self.values.get(key)

    def hgetall(self, key):
        return self.run


class RepositoryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "repo"
        self.path.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.path / "file").write_text("first\n")
        self.git("add", "file")
        self.git("commit", "-qm", "first")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def index(self, checkout, **overrides):
        return {"repository": {k: checkout[k] for k in ("roots", "shallow", "remote_fingerprint")},
                "indexed_commit": checkout["head"], "ingestion_status": "done", "history_complete": True, **overrides}

    def test_matching_stale_and_dirty_checkout(self):
        old = identity.checkout_identity(str(self.path))
        index = self.index(old)
        self.assertEqual(identity.compare(index, old)["revision"], "current")
        self.assertEqual(identity.compare(index, old)["repository"], "matching_history")
        (self.path / "file").write_text("changed\n")
        self.assertTrue(any("uncommitted" in x for x in identity.compare(index, identity.checkout_identity(str(self.path)))["warnings"]))
        self.git("add", "file")
        self.git("commit", "-qm", "second")
        self.assertEqual(identity.compare(index, identity.checkout_identity(str(self.path)))["revision"], "different")

    def test_mismatch_shallow_legacy_and_failed_ingestion(self):
        checkout = identity.checkout_identity(str(self.path))
        index = self.index(checkout)
        other = {**checkout, "roots": ["a" * 40]}
        self.assertEqual(identity.compare(index, other)["repository"], "mismatch")
        self.assertEqual(identity.compare(index, {**other, "shallow": True})["repository"], "unknown")
        self.assertEqual(identity.compare({**index, "repository": {}}, checkout)["repository"], "unknown")
        self.assertEqual(identity.compare({**index, "ingestion_status": "failed"}, checkout)["revision"], "unknown")
        self.assertEqual(identity.compare(index, {"available": False})["repository"], "unknown")

    def test_remote_normalization_removes_credentials(self):
        self.assertEqual(identity._remote_fingerprint("https://user:secret@example.com/a/b.git"),
                         identity._remote_fingerprint("git@example.com:a/b"))

    def test_index_metadata_and_invalid_legacy_metadata(self):
        store = FakeStore({"repository_identity": "bad json", "resume_point": "abc"}, {"status": "done"})
        self.assertEqual(identity.index_identity(store), {"repository": {}, "indexed_commit": "abc", "ingestion_status": "done", "history_complete": False})

    def test_real_sqlite_store_metadata(self):
        import store
        db = store.open(str(Path(self.tmp.name) / "index.db"))
        checkout = identity.checkout_identity(str(self.path))
        db.set("repository_identity", json.dumps({k: checkout[k] for k in ("roots", "shallow", "remote_fingerprint")}))
        db.set("resume_point", checkout["head"])
        db.set("index_history_complete", "1")
        db.hset("ingest_run", mapping={"status": "done"})
        result = identity.compare(identity.index_identity(db), checkout)
        self.assertEqual((result["repository"], result["revision"], result["warnings"]), ("matching_history", "current", []))


class MCPCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        httpx = types.ModuleType("httpx")
        httpx.HTTPError = type("HTTPError", (Exception,), {})
        httpx.get = lambda *a, **k: None
        httpx.post = lambda *a, **k: None
        mcp = types.ModuleType("mcp")
        server = types.ModuleType("mcp.server")
        fastmcp = types.ModuleType("mcp.server.fastmcp")

        class Server:
            def __init__(self, *args, **kwargs): pass
            def tool(self): return lambda function: function

        fastmcp.FastMCP = Server
        cls.modules = patch.dict(sys.modules, {"httpx": httpx, "mcp": mcp,
                                             "mcp.server": server, "mcp.server.fastmcp": fastmcp})
        cls.modules.start()
        cls.mode = patch.dict("os.environ", {"UNIFIED_BRAIN_MODE": "external"})
        cls.mode.start()
        cls.client = importlib.import_module("unified_brain_mcp")

    @classmethod
    def tearDownClass(cls):
        cls.mode.stop()
        cls.modules.stop()
        sys.modules.pop("unified_brain_mcp", None)

    def test_mismatch_stops_before_expensive_request(self):
        checkout = {"available": True, "roots": ["a"], "shallow": False, "head": "1", "dirty": False}
        index = {"repository": {"roots": ["b"], "shallow": False}, "indexed_commit": "2", "ingestion_status": "done"}
        response = types.SimpleNamespace(status_code=200, json=lambda: index)
        with patch.object(self.client.repository_identity, "checkout_identity", return_value=checkout), \
             patch.object(self.client.httpx, "get", return_value=response), \
             patch.object(self.client.httpx, "post") as post:
            result = json.loads(self.client.retrieve("question"))
        self.assertEqual(result["repository_check"]["repository"], "mismatch")
        post.assert_not_called()

    def test_old_service_reports_identity_unknown(self):
        missing = types.SimpleNamespace(status_code=404)
        result = types.SimpleNamespace(status_code=200, json=lambda: {"evidence": [], "leads": []})
        with patch.object(self.client.repository_identity, "checkout_identity", return_value={"available": False}), \
             patch.object(self.client.httpx, "get", return_value=missing), \
             patch.object(self.client.httpx, "post", return_value=result):
            output = json.loads(self.client.retrieve("question"))
        self.assertEqual(output["repository_check"]["repository"], "unknown")
        self.assertIn("does not expose", output["repository_check"]["warnings"][0])

    def test_matching_checkout_is_sent_and_service_warnings_survive(self):
        checkout = {"available": True, "roots": ["a"], "shallow": False, "head": "1", "dirty": True}
        index = {"repository": {"roots": ["a"], "shallow": False}, "indexed_commit": "1",
                 "ingestion_status": "done", "history_complete": True}
        preflight = types.SimpleNamespace(status_code=200, json=lambda: index)
        check = identity.compare(index, checkout)
        response = types.SimpleNamespace(status_code=200, json=lambda: {
            "index": index, "repository_check": check, "evidence": [], "leads": []})
        with patch.object(self.client.repository_identity, "checkout_identity", return_value=checkout), \
             patch.object(self.client.httpx, "get", return_value=preflight), \
             patch.object(self.client.httpx, "post", return_value=response) as post:
            output = json.loads(self.client.retrieve("question", repo_path="/target"))
        self.assertEqual(post.call_args.kwargs["json"]["checkout"], checkout)
        self.assertTrue(any("uncommitted" in warning for warning in output["repository_check"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
