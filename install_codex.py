#!/usr/bin/env python3
"""Install the global unified-brain Codex skill, MCP server, and hooks."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib

BASE = Path(__file__).resolve().parent


def _section(text, header, values):
    pattern = re.compile(r"(?m)^\[" + re.escape(header) + r"\][ \t]*$")
    match = pattern.search(text)
    if not match:
        text = text.rstrip() + "\n\n[" + header + "]\n"
        match = pattern.search(text)
    end = re.search(r"(?m)^\[", text[match.end():])
    stop = match.end() + end.start() if end else len(text)
    body = text[match.end():stop]
    for key, value in values.items():
        line = key + " = " + value
        key_pattern = re.compile(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=.*$")
        if key_pattern.search(body):
            body = key_pattern.sub(lambda _: line, body, count=1)
        else:
            body = body.rstrip() + "\n" + line + "\n"
    return text[:match.end()] + body.rstrip() + "\n\n" + text[stop:].lstrip("\n")


def git_excludes_path():
    result = subprocess.run(["git", "config", "--path", "--get", "core.excludesfile"],
                            capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "git" / "ignore"


def install(codex_home, excludes_path=None):
    codex_home = Path(codex_home)
    codex_home.mkdir(parents=True, exist_ok=True)
    config = codex_home / "config.toml"
    original = config.read_text() if config.exists() else ""
    parsed = tomllib.loads(original)
    existing = parsed.get("mcp_servers", {}).get("unified-brain", {})
    if existing and str(BASE / "unified_brain_mcp.py") not in existing.get("args", []):
        raise RuntimeError("A different unified-brain MCP server is configured; leave it unchanged and resolve the conflict")
    quoted = '[mcp_servers."unified-brain"]' in original
    prefix = 'mcp_servers."unified-brain"' if quoted else "mcp_servers.unified-brain"
    text = _section(original, prefix, {
        "command": json.dumps(sys.executable),
        "args": json.dumps([str(BASE / "unified_brain_mcp.py")]),
        "tool_timeout_sec": "1800",
    })
    text = _section(text, prefix + ".env", {"UNIFIED_BRAIN_MODE": '"auto"'})
    tomllib.loads(text)

    hooks_path = codex_home / "hooks.json"
    hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    hook_map = hooks.setdefault("hooks", {})
    command = shlex.join([sys.executable, str(BASE / "codex_hook.py")])
    handler = {"type": "command", "command": command, "async": True,
               "timeout": 1800, "statusMessage": "Updating unified-brain index"}
    for event, matcher in (("SessionStart", "startup|resume"), ("PostToolUse", "^Bash$")):
        groups = hook_map.setdefault(event, [])
        if not any(any(h.get("command") == command for h in group.get("hooks", [])) for group in groups):
            groups.append({"matcher": matcher, "hooks": [handler]})

    # This background hook lives for the session and catches commits made by
    # other processes. Codex cancels it at session end.
    watch_command = shlex.join([sys.executable, str(BASE / "auto_watch.py")])
    groups = hook_map.setdefault("SessionStart", [])
    if not any(any(h.get("command") == watch_command for h in group.get("hooks", [])) for group in groups):
        groups.append({"matcher": "startup|resume", "hooks": [{
            "type": "command", "command": watch_command, "async": True,
            "timeout": 2592000, "statusMessage": "Watching for repository commits"}]})

    skill_dir = codex_home / "skills" / "unified-brain"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill = skill_dir / "SKILL.md"
    source = BASE / "skills" / "unified-brain" / "SKILL.md"
    content = source.read_bytes()
    if skill.is_symlink() or not skill.exists() or skill.read_bytes() != content:
        if (skill.exists() or skill.is_symlink()) and not (skill.is_symlink() and skill.resolve() == source):
            backup = skill_dir / "SKILL.md.before-unified-brain-install"
            if not backup.exists():
                backup.write_bytes(skill.read_bytes())
        if skill.exists() or skill.is_symlink():
            skill.unlink()
        skill.write_bytes(content)
    config.write_text(text)
    hooks_path.write_text(json.dumps(hooks, indent=2) + "\n")
    if excludes_path is not None:
        excludes_path = Path(excludes_path)
        excludes_path.parent.mkdir(parents=True, exist_ok=True)
        original_ignore = excludes_path.read_text() if excludes_path.exists() else ""
        patterns = (".unified-brain.db", ".unified-brain.db-shm", ".unified-brain.db-wal")
        missing = [pattern for pattern in patterns if pattern not in original_ignore.splitlines()]
        if missing:
            with excludes_path.open("a") as output:
                if original_ignore and not original_ignore.endswith("\n"):
                    output.write("\n")
                output.write("\n".join(missing) + "\n")
    return {"config": str(config), "hooks": str(hooks_path), "skill": str(skill),
            "git_excludes": str(excludes_path) if excludes_path is not None else None}


if __name__ == "__main__":
    print(json.dumps(install(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")), git_excludes_path())))
