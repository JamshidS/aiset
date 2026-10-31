"""Native Codex hook adapter: reuse Aiset's nudge and publishing tripwire.

Hook trust belongs to Codex and the user. Aiset never writes its trust store.
The publishing gate is active only while an aiset run board is working. Like
the Claude gate this is a tripwire, not a sandbox; Codex itself may fail open
on a hook timeout, so the independent host-side publish confirmation remains.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from glob import glob

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
    __package__ = "aiset.providers"


def command(ws):
    args = [sys.executable, os.path.realpath(__file__), os.path.abspath(ws)]
    return subprocess.list2cmdline(args) if os.name == "nt" else " ".join(shlex.quote(a) for a in args)


def config(ws):
    return {"description": "Aiset teammap hints and aiset run publishing gate. Review with /hooks.",
            "hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [
                {"type": "command", "command": command(ws), "timeout": 30,
                 "statusMessage": "Aiset team guidance and publishing gate"}]}]}}


def working(ws):
    for path in glob(os.path.join(ws, ".claude", "team", "tasks", "*", "run.json")):
        try:
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("phase") == "working":
                    return True
        except (ValueError, OSError, AttributeError):
            # A damaged existing run state must not silently turn off its guard.
            return True
    return False


def decide(data):
    from ..run.gate import decide as shared_decide
    name, inp = data.get("tool_name"), data.get("tool_input") or {}
    if not isinstance(inp, dict):
        return False, "Aiset could not inspect this tool input."
    if name == "apply_patch":
        patch = inp.get("command") or inp.get("patch") or ""
        for path in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M):
            allowed, reason = shared_decide("Edit", {"file_path": path}, data.get("cwd"))
            if not allowed:
                return allowed, reason
        for path in re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M):
            allowed, reason = shared_decide("Write", {"file_path": path}, data.get("cwd"))
            if not allowed:
                return allowed, reason
        return True, ""
    if name in ("exec_command", "shell", "shell_command"):
        name = "Bash"
        inp = {"command": inp.get("cmd") or inp.get("command")}
    return shared_decide(name, inp, data.get("cwd"))


def handle(ws, data):
    if working(ws):
        try:
            allowed, reason = decide(data)
        except Exception:
            allowed, reason = False, "Aiset could not inspect this tool call; blocked."
        if not allowed:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
    from .. import nudge, providers, workspace
    try:
        if not providers.read_roster(ws).get("nudge", True):
            return {}
        hit = nudge.suggest(ws, data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd"),
                            workspace.script_command("teammap", sys.executable))
        if hit and not nudge._seen(data.get("session_id"), hit[0]):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": hit[1]}}
    except Exception:
        pass  # an optional hint must never block work, including while the roster is mid-edit
    return {}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        data = json.load(sys.stdin)
        result = handle(os.path.abspath(argv[0]), data)
        if result:
            print(json.dumps(result))
        return 0
    except Exception:
        print("Aiset hook could not inspect this call; blocked.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
