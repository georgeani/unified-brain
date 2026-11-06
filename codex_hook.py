#!/usr/bin/env python3
"""Codex SessionStart/PostToolUse hook: refresh the session checkout."""
import json
import os
import sys

import auto_sync
import repository_identity


def main():
    try:
        event = json.load(sys.stdin)
        path = event.get("cwd") or os.getcwd()
        # PostToolUse can run after a shell command with a different workdir.
        if isinstance(event.get("tool_input"), dict):
            path = event["tool_input"].get("workdir") or path
        if not repository_identity._git(path, "rev-parse", "--show-toplevel"):
            return
        result = auto_sync.sync_repo(path)
        if result["action"] != "current":
            print(json.dumps({"systemMessage": f"unified-brain index {result['action']} for {result['repository']}"}))
    except Exception as exc:
        print(json.dumps({"systemMessage": f"unified-brain automatic index update failed: {exc}"}))


if __name__ == "__main__":
    main()
