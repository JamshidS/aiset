"""The `claude` command for an `aiset run` session: PM agent, session id, gate settings, deny rules, start prompt.

The prompt is fixed core text plus the board path, never issue text (issue.md is read by path). `claude` is
often a `.cmd` shim on Windows, so arguments pass through cmd.exe: the gate settings go in a file rather than
inline JSON, and the prompt is one line without cmd metacharacters. `--disallowedTools` takes several values,
so the prompt goes after `--` (otherwise claude reads it as one more deny rule).
"""
import json
import os
import re
import subprocess
import sys

from . import RunError, program
from ..atomic import write_text_atomic

HERE = os.path.dirname(os.path.realpath(__file__))
PROMPT_FILE = os.path.join(os.path.dirname(HERE), "templates", "run_prompt.md")
DENY = ("Bash(git push:*)", "Bash(gh pr:*)", "Bash(gh issue:*)", "Bash(gh api:*)", "Bash(gh release:*)")
# The gate hook's own limit (seconds; its alias lookups stop after gate.LOOKUP_DEADLINE) and onFailure "block":
# a hook that times out or fails blocks the tool call instead of letting it through (checked with claude 2.1.312).
HOOK_TIMEOUT = 30
CMD_UNSAFE = re.compile(r'[\r\n"%^&|<>!]')
PATH_UNSAFE = re.compile(r'[\r\n"%^&|<>!,;=]')


def _slash(path):
    return path.replace("\\", "/")


def check_path(board_dir, windows=None):
    """Refuse a board path that cmd.exe would mangle on Windows: it goes through the `claude.cmd` shim as the
    --settings value and in the prompt, where `&`, `%VAR%`, `!` or `,` could split it or run something else."""
    if (os.name == "nt" if windows is None else windows):
        bad = sorted(set(PATH_UNSAFE.findall(board_dir)))
        if bad:
            raise RunError(f"the board path {board_dir} has characters cmd.exe treats specially ({' '.join(bad)}); "
                           "move the workspace to a folder without them")


def gate_command():
    """The hook command: this Python running gate.py by path, so it works without aiset on the hook's PATH."""
    return f'"{_slash(sys.executable)}" "{_slash(os.path.join(HERE, "gate.py"))}"'


def gate_settings():
    hook = {"type": "command", "command": gate_command(), "timeout": HOOK_TIMEOUT, "onFailure": "block"}
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}


def write_gate_settings(board_dir):
    path = os.path.join(board_dir, "gate-settings.json")
    write_text_atomic(path, json.dumps(gate_settings(), indent=2) + "\n")
    return path


def start_prompt(board_dir, *, interactive=True):
    """One line: the template with {{BOARD}} filled in, comments and line breaks folded away."""
    with open(PROMPT_FILE, encoding="utf-8") as f:
        text = f.read()
    finish = ("tell the user to type /exit so aiset run can show the result" if interactive else
              "finish your response and return control to the host; no manual exit command is needed")
    text = text.replace("{{FINISH}}", finish)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = " ".join(text.replace("{{BOARD}}", _slash(board_dir)).split())
    return CMD_UNSAFE.sub("", text)


def claude_argv(board_dir, session_id, *, resume=False, model=None, settings_path=None, name=None):
    """argv[0] is plain `claude` (for --dry-run); run_claude puts the resolved path in its place."""
    argv = ["claude", "--agent", "pm"]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    if name:
        argv += ["-n", CMD_UNSAFE.sub("", name)]
    argv += ["--settings", _slash(settings_path or os.path.join(board_dir, "gate-settings.json"))]
    if model:
        argv += ["--model", model]
    argv += ["--disallowedTools", *DENY]
    if not resume:
        argv += ["--", start_prompt(board_dir)]
    return argv


def permission_warnings(workspace_dir):
    """Settings files whose default permission mode skips prompts. Warn, don't refuse (design §11 Q7):
    reading every settings layer reliably isn't possible from outside Claude Code."""
    out = []
    files = [os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
             os.path.join(workspace_dir, ".claude", "settings.json"),
             os.path.join(workspace_dir, ".claude", "settings.local.json")]
    for path in files:
        try:
            with open(path, encoding="utf-8-sig") as f:
                mode = ((json.load(f).get("permissions") or {}).get("defaultMode"))
        except (OSError, ValueError, AttributeError):
            continue
        if mode in ("bypassPermissions", "auto"):
            out.append(f"{_slash(path)} sets defaultMode {mode}: tool calls in this session may run without asking you.")
    return out


def run_claude(argv, cwd):
    """Runs the session in this terminal (it inherits the TTY) and returns claude's exit code. `claude` is found on
    PATH outside the workspace (`cwd`), never in it: a planted claude.cmd there would run outside the gate."""
    exe = program("claude", cwd, hint=": install Claude Code (https://claude.com/claude-code)")
    return subprocess.call([exe, *argv[1:]], cwd=cwd)
