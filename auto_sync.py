#!/usr/bin/env python3
"""Create and refresh the current checkout's unified-brain index for Codex."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import repository_identity
import store

BASE = Path(__file__).resolve().parent


def git_root(path):
    root = repository_identity._git(str(path), "rev-parse", "--show-toplevel")
    if not root:
        raise RuntimeError("The target is not a Git checkout with a commit")
    return Path(root).resolve()


@contextlib.contextmanager
def repo_lock(root):
    lock_dir = Path(tempfile.gettempdir()) / f"unified-brain-{os.getuid()}"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(str(root).encode()).hexdigest() + ".lock"
    with (lock_dir / name).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _settings():
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("Install the unified-brain requirements in its virtual environment") from exc
    load_dotenv(BASE / ".env")
    provider = os.environ.get("QA_VECTOR_STORE", "voyage")
    providers = [x.strip() for x in os.environ.get("CODE_EMBED_PASS2", "voyage").split(",")]
    keys = {"voyage": "VOYAGE_API_KEY", "qwen": "DEEPINFRA_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
    if provider not in keys or provider not in providers or not os.environ.get(keys.get(provider, "")):
        raise RuntimeError("Configure QA_VECTOR_STORE, the same provider in CODE_EMBED_PASS2, and its API key")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("Configure OPENROUTER_API_KEY for retrieval")
    return provider


def _ingest(root, *flags):
    command = [sys.executable, str(BASE / "next_claude.py"), str(root), *flags]
    result = subprocess.run(command, cwd=str(BASE), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        # Avoid returning provider responses, which may contain credentials.
        raise RuntimeError("Indexing or embedding failed; run next_claude.py for details")


def sync_repo(path):
    root = git_root(path)
    with repo_lock(root):
        checkout = repository_identity.checkout_identity(str(root))
        if not checkout.get("available"):
            raise RuntimeError("The target checkout has no readable HEAD commit")
        provider = _settings()
        db_path = root / store.DB_FILE_NAME
        existed = db_path.exists()
        with contextlib.closing(store.open(str(db_path))) as db:
            state = repository_identity.index_identity(db)
            check = repository_identity.compare(state, checkout)
            previous = state["indexed_commit"]
            if check["repository"] == "mismatch":
                raise RuntimeError("The existing index belongs to a different Git history; it was left untouched")
            if previous and repository_identity._git(str(root), "merge-base", "--is-ancestor", previous, checkout["head"]) is None:
                raise RuntimeError("The index and checkout have diverged or history was rewritten; the existing index was left untouched")
            if state["ingestion_status"] in ("failed", "running"):
                raise RuntimeError("The previous ingestion was interrupted or failed; the existing index needs repair")
            if existed and not previous and db.zrange("commit_order", 0, 0):
                raise RuntimeError("The existing index has no resume point; it was left untouched")
            action = "current"
            if previous and previous == checkout["head"] and not state["repository"]:
                db.set("repository_identity", json.dumps({key: checkout[key] for key in
                       ("roots", "shallow", "remote_fingerprint")}))
                state = repository_identity.index_identity(db)
            if not previous or previous != checkout["head"]:
                _ingest(root, *(["--resume"] if previous else []), "--no-embed")
                action = "updated" if previous else "created"
                state = repository_identity.index_identity(db)
            if repository_identity._git(str(root), "rev-parse", "HEAD") != checkout["head"]:
                raise RuntimeError("HEAD changed during indexing; retry to finish the update")
            marker = db.get(f"auto_sync:embedded:{provider}") or ""
            if marker != checkout["head"]:
                _ingest(root, "--embed-only")
                # Do not mark a newer checkout as indexed when HEAD moves mid-run.
                if repository_identity._git(str(root), "rev-parse", "HEAD") != checkout["head"]:
                    raise RuntimeError("HEAD changed during indexing; retry to finish the update")
                db.set(f"auto_sync:embedded:{provider}", checkout["head"])
            check = repository_identity.compare(state, checkout)
            return {"repository": str(root), "db_path": str(db_path), "action": action,
                    "index": state, "repository_check": check}


def main():
    try:
        print(json.dumps(sync_repo(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
