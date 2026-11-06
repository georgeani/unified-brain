#!/usr/bin/env python3

# unified-brain — cheaper, faster more accurate AI coding agents, by DevLoops
# Copyright (C) 2026 DevLoops SINGLE MEMBER P.C.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
unified-brain — MCP server for Cursor, Claude Code and any MCP client
---------------------------------------------------------------------
The `retrieve` tool synchronizes the target checkout and returns source
evidence. `sync` updates its index without asking a question. By default,
the MCP server starts a local service for each checkout.

    UNIFIED_BRAIN_MODE  auto (default) or external
    UNIFIED_BRAIN_URL   external service URL (default http://127.0.0.1:8000)
    UNIFIED_BRAIN_REPO  optional default checkout when the client omits repo_path

Claude Code (.mcp.json in the project, or ~/.claude.json):
    {"mcpServers": {"unified-brain": {
        "command": "/path/to/unified-brain/.venv/bin/python",
        "args": ["/path/to/unified-brain/unified_brain_mcp.py"]}}}

Cursor (.cursor/mcp.json): the same object.
"""
import json
import os

import httpx
import auto_server
import auto_sync
import repository_identity
try:                                   # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

URL = os.environ.get("UNIFIED_BRAIN_URL", "http://127.0.0.1:8000").rstrip("/")

mcp = _Server(
    "unified-brain",
    instructions=(
        "Use retrieve for substantial repository investigations, passing "
        "the target checkout as repo_path. It creates or refreshes its index "
        "automatically. Check repository_check and window; verify current "
        "files before making edits."),
)


@mcp.tool()
def sync(repo_path: str = "") -> str:
    """Create or update the target checkout's index, without retrieving evidence."""
    try:
        return json.dumps(auto_sync.sync_repo(repo_path or os.environ.get("UNIFIED_BRAIN_REPO") or os.getcwd()))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
def retrieve(question: str, from_commit: str = "", to_commit: str = "", repo_path: str = "") -> str:
    """Get the evidence that answers a question about the repository.

    Call this ONCE, with the user's whole question as they wrote it. It runs
    the full search (embeddings → a judge model scores every candidate →
    the judge follows names through the code) and returns every body that
    answers the question, with file, symbol, line and the code in full.
    Consult repository_check and window before relying on the evidence.
    Verify current files before making changes. Takes 20–60 seconds.
    Give from_commit and to_commit to see the code as it was at to_commit.
    Give repo_path when the MCP process was not started in the target checkout.
    """
    path = repo_path or os.environ.get("UNIFIED_BRAIN_REPO") or os.getcwd()
    checkout = repository_identity.checkout_identity(path)
    url = URL
    if os.environ.get("UNIFIED_BRAIN_MODE", "auto") != "external":
        try:
            synced = auto_sync.sync_repo(path)
            url = auto_server.service_url(synced["db_path"])
        except Exception as exc:
            return json.dumps({"error": str(exc)})
    try:
        # Fail before the potentially costly retrieval if the service selected
        # an index for a different history. Old services remain usable, with
        # their inability to check made explicit in the response.
        identity_response = httpx.get(f"{url}/api/repository", timeout=10)
        if identity_response.status_code == 200:
            index = identity_response.json()
            check = repository_identity.compare(index, checkout)
            if check["repository"] == "mismatch":
                return json.dumps({"error": "The service index does not match the client checkout.",
                                   "index": index, "repository_check": check})
        else:
            check = {"repository": "unknown", "revision": "unknown",
                     "warnings": ["The service does not expose index identity; update and restart it."]}
        r = httpx.post(f"{url}/api/retrieve",
                       json={"question": question, "from_commit": from_commit,
                             "to_commit": to_commit, "checkout": checkout},
                       timeout=1800)
    except httpx.HTTPError as e:
        return json.dumps({"error": f"unified-brain service not reachable at {url}: {e}"})
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        return json.dumps({"error": f"HTTP {r.status_code}: {detail}"})
    d = r.json()
    n = len(d.get("evidence") or [])
    out = {
        "note": f"{n} evidence bodies returned. Check repository_check and window before relying on them.",
        "index": d.get("index") or (index if identity_response.status_code == 200 else None),
        "repository_check": d.get("repository_check") or check,
        "window": d.get("window", ""),
        "evidence": d.get("evidence") or [],
        "leads": d.get("leads") or [],
    }
    return json.dumps(out, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    mcp.run()
