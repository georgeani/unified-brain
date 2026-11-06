#!/usr/bin/env python3
"""Watch the Codex session checkout for commits made by any Git client."""
import json
import os
import sys
import time

import auto_sync
import repository_identity

POLL_SECONDS = 5
RETRY_SECONDS = 30


def watch(path, *, sleep=time.sleep, clock=time.monotonic):
    """Synchronize the initial HEAD and each later HEAD; retry failures."""
    root = auto_sync.git_root(path)
    indexed_head = None
    retry_at = 0
    while True:
        head = repository_identity._git(str(root), "rev-parse", "--verify", "HEAD")
        now = clock()
        if head and head != indexed_head and now >= retry_at:
            try:
                result = auto_sync.sync_repo(root)
                # sync_repo checks for a moving HEAD; only remember its result.
                indexed_head = result["index"]["indexed_commit"]
                retry_at = 0
            except Exception as exc:
                # Hooks are long-lived, so log without exposing provider replies.
                print(f"unified-brain automatic index update failed: {exc}",
                      file=sys.stderr, flush=True)
                retry_at = clock() + RETRY_SECONDS
        sleep(POLL_SECONDS)


def main():
    try:
        event = json.load(sys.stdin)
        path = event.get("cwd") or os.getcwd()
        if repository_identity._git(path, "rev-parse", "--show-toplevel"):
            watch(path)
    except Exception as exc:
        print(f"unified-brain watcher failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
