import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import auto_sync
import codex_hook
import install_codex
import repository_identity
import store


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "file.py").write_text("a = 1\n")
        self.git("add", "file.py")
        self.git("commit", "-qm", "initial")
        self.calls = []

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def fake_ingest(self, root, *flags):
        self.calls.append(flags)
        if "--embed-only" in flags:
            return
        checkout = repository_identity.checkout_identity(str(root))
        db = store.open(str(root / store.DB_FILE_NAME))
        db.set("repository_identity", json.dumps({k: checkout[k] for k in ("roots", "shallow", "remote_fingerprint")}))
        db.set("resume_point", checkout["head"])
        db.set("index_history_complete", "1")
        db.hset("ingest_run", mapping={"status": "done"})

    def test_create_repeat_and_refresh_after_commit(self):
        with patch.object(auto_sync, "_settings", return_value="openrouter"), \
             patch.object(auto_sync, "_ingest", side_effect=self.fake_ingest):
            self.assertEqual(auto_sync.sync_repo(self.repo)["action"], "created")
            self.assertEqual(auto_sync.sync_repo(self.repo)["action"], "current")
            self.assertEqual(self.calls, [("--no-embed",), ("--embed-only",)])
            (self.repo / "file.py").write_text("a = 2\n")
            self.git("add", "file.py")
            self.git("commit", "-qm", "second")
            result = auto_sync.sync_repo(self.repo)
            self.assertEqual(result["action"], "updated")
            self.assertEqual(result["index"]["indexed_commit"], self.git("rev-parse", "HEAD"))
            self.assertEqual(self.calls[-2:], [("--resume", "--no-embed"), ("--embed-only",)])

    def test_failed_embedding_is_retried(self):
        def fail_embedding(root, *flags):
            if "--embed-only" in flags:
                raise RuntimeError("embedding failed")
            self.fake_ingest(root, *flags)
        with patch.object(auto_sync, "_settings", return_value="openrouter"), \
             patch.object(auto_sync, "_ingest", side_effect=fail_embedding):
            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                auto_sync.sync_repo(self.repo)
        with patch.object(auto_sync, "_settings", return_value="openrouter"), \
             patch.object(auto_sync, "_ingest", side_effect=self.fake_ingest):
            self.assertEqual(auto_sync.sync_repo(self.repo)["action"], "current")
            self.assertEqual(self.calls[-1], ("--embed-only",))

    def test_divergent_history_does_not_modify_index(self):
        with patch.object(auto_sync, "_settings", return_value="openrouter"), \
             patch.object(auto_sync, "_ingest", side_effect=self.fake_ingest):
            auto_sync.sync_repo(self.repo)
            self.git("checkout", "--orphan", "other")
            (self.repo / "file.py").write_text("other\n")
            self.git("add", "file.py")
            self.git("commit", "-qm", "other")
            with self.assertRaisesRegex(RuntimeError, "different Git history"):
                auto_sync.sync_repo(self.repo)
            self.assertEqual(len(self.calls), 2)

    def test_codex_hook_uses_tool_workdir(self):
        event = {"cwd": "/unused", "tool_input": {"workdir": str(self.repo)}}
        with patch("sys.stdin", io.StringIO(json.dumps(event))), \
             patch.object(auto_sync, "sync_repo", return_value={"action": "current"}) as sync, \
             contextlib.redirect_stdout(io.StringIO()):
            codex_hook.main()
        sync.assert_called_once_with(str(self.repo))

    def test_real_ingester_tracks_new_commits(self):
        real_ingest = auto_sync._ingest
        def ingest_without_external_embedding(root, *flags):
            if "--embed-only" not in flags:
                real_ingest(root, *flags)
        with patch.object(auto_sync, "_settings", return_value="openrouter"), \
             patch.object(auto_sync, "_ingest", side_effect=ingest_without_external_embedding):
            self.assertEqual(auto_sync.sync_repo(self.repo)["action"], "created")
            (self.repo / "file.py").write_text("a = 2\n")
            self.git("add", "file.py")
            self.git("commit", "-qm", "second")
            updated = auto_sync.sync_repo(self.repo)
        db = store.open(str(self.repo / store.DB_FILE_NAME))
        try:
            self.assertEqual(updated["index"]["indexed_commit"], self.git("rev-parse", "HEAD"))
            self.assertEqual(len(db.zrange("commit_order", 0, -1)), 2)
        finally:
            db.close()


class InstallerTests(unittest.TestCase):
    def test_install_preserves_other_config_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text('[mcp_servers.other]\ncommand = "other"\n')
            (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": []}]}}))
            excludes = home / "git" / "ignore"
            excludes.parent.mkdir()
            excludes.write_text("existing-rule")
            install_codex.install(home, excludes)
            install_codex.install(home, excludes)
            config = tomllib.loads((home / "config.toml").read_text())
            self.assertEqual(config["mcp_servers"]["other"]["command"], "other")
            self.assertEqual(config["mcp_servers"]["unified-brain"]["tool_timeout_sec"], 1800)
            self.assertEqual(config["mcp_servers"]["unified-brain"]["env"]["UNIFIED_BRAIN_MODE"], "auto")
            hooks = json.loads((home / "hooks.json").read_text())["hooks"]
            self.assertEqual(len(hooks["SessionStart"]), 1)
            self.assertEqual(len(hooks["PostToolUse"]), 1)
            self.assertIn("Stop", hooks)
            self.assertTrue((home / "skills/unified-brain/SKILL.md").is_symlink())
            self.assertEqual(excludes.read_text().splitlines(), ["existing-rule", ".unified-brain.db",
                             ".unified-brain.db-shm", ".unified-brain.db-wal"])

    def test_existing_config_and_skill_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = str(install_codex.BASE / "unified_brain_mcp.py")
            (home / "config.toml").write_text(f'[mcp_servers."unified-brain"]\nargs = [{json.dumps(source)}]\ntool_timeout_sec = 180\n[mcp_servers."unified-brain".env]\nFOO = "bar"\n')
            skill = home / "skills/unified-brain/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("old skill")
            install_codex.install(home)
            config = tomllib.loads((home / "config.toml").read_text())
            self.assertEqual(config["mcp_servers"]["unified-brain"]["env"]["FOO"], "bar")
            self.assertEqual((skill.parent / "SKILL.md.before-unified-brain-install").read_text(), "old skill")


if __name__ == "__main__":
    unittest.main()
