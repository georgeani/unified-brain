"""Repository identity and index freshness checks shared by service and MCP."""
import hashlib
import os
import re
import subprocess
from urllib.parse import urlsplit


def _git(path, *args):
    try:
        result = subprocess.run(["git", "-C", path, *args], capture_output=True,
                                text=True, timeout=15, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _remote_fingerprint(remote):
    """Hash a normalized origin, excluding credentials and transport details."""
    if not remote:
        return ""
    if "://" in remote:
        url = urlsplit(remote)
        value = (url.hostname or "").lower() + url.path
    else:
        match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", remote)
        value = (match.group(1).lower() + "/" + match.group(2)
                 if match else os.path.realpath(remote))
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return hashlib.sha256(value.encode()).hexdigest()


def checkout_identity(path):
    root = _git(path, "rev-parse", "--show-toplevel")
    head = _git(path, "rev-parse", "--verify", "HEAD") if root else None
    if not root or not head:
        return {"available": False}
    roots = _git(root, "rev-list", "--max-parents=0", "HEAD")
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    return {"available": True, "head": head,
            "roots": sorted(roots.splitlines()) if roots is not None else [],
            "shallow": shallow != "false", "dirty": None if dirty is None else bool(dirty),
            "remote_fingerprint": _remote_fingerprint(_git(root, "remote", "get-url", "origin"))}


def index_identity(store):
    import json
    try:
        metadata = json.loads(store.get("repository_identity") or "{}")
        if not isinstance(metadata, dict):
            metadata = {}
    except (ValueError, TypeError):
        metadata = {}
    run = store.hgetall("ingest_run") or {}
    return {"repository": metadata, "indexed_commit": store.get("resume_point") or "",
            "ingestion_status": run.get("status", "unknown"),
            "history_complete": store.get("index_history_complete") == "1"}


def compare(index, checkout):
    """Compare metadata without assuming that absent or partial evidence matches."""
    result = {"repository": "unknown", "revision": "unknown", "warnings": []}
    warnings = result["warnings"]
    if not checkout.get("available"):
        warnings.append("The client checkout could not be determined; pass repo_path or set UNIFIED_BRAIN_REPO.")
        return result
    indexed = index.get("repository") or {}
    a, b = set(indexed.get("roots") or []), set(checkout.get("roots") or [])
    if a and b and not indexed.get("shallow", True) and not checkout.get("shallow", True):
        result["repository"] = "matching_history" if a & b else "mismatch"
    if result["repository"] == "mismatch":
        warnings.append("The index and checkout have different Git root histories.")
    elif result["repository"] == "unknown":
        warnings.append("Repository identity is unverified (index metadata or complete Git history is missing).")
    remote_a, remote_b = indexed.get("remote_fingerprint"), checkout.get("remote_fingerprint")
    if remote_a and remote_b and remote_a != remote_b:
        warnings.append("The index and checkout have different origin remotes; they may be different forks or a remote may have changed.")
    commit = index.get("indexed_commit") or ""
    if commit:
        result["revision"] = "current" if commit == checkout.get("head") else "different"
        if result["revision"] == "different":
            warnings.append("The last processed commit differs from checkout HEAD; the index may be stale or from another branch.")
    else:
        warnings.append("The index has no recorded last processed commit; its revision is unknown.")
    if index.get("ingestion_status") != "done":
        result["revision"] = "unknown"
        warnings.append("Ingestion is not recorded as successfully complete; the indexed revision cannot be confirmed.")
    if not index.get("history_complete"):
        warnings.append("Complete history coverage is unverified; the index may have been built from a partial range.")
    if checkout.get("dirty"):
        warnings.append("The checkout has uncommitted tracked changes that are not represented by the index.")
    elif checkout.get("dirty") is None:
        warnings.append("Could not determine whether the checkout has uncommitted tracked changes.")
    return result
