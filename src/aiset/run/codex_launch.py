"""Native Codex issue sessions; publishing stays in the shared two-gate runner."""
import json
import subprocess

from . import RunError, program
from .launch import start_prompt
from .sanitize import visible_text
from ..providers.codex import quoted
from ..providers.codex_hooks import command
from ..providers.codex_rpc import hooks


def ensure_hooks(cwd):
    exe = program("codex", cwd, hint=": install and sign in to Codex CLI")
    try:
        result = hooks(exe, cwd)
    except Exception as e:
        raise RunError("could not verify Codex publishing hook readiness (" + type(e).__name__ +
                       "). Run codex in this workspace, review /hooks, then retry.") from None
    expected = command(cwd)
    valid = any(h.get("eventName") == "preToolUse" and h.get("command") == expected
                and h.get("enabled") and h.get("trustStatus") in ("trusted", "managed")
                and h.get("matcher") == ".*" and not h.get("async", False)
                and h.get("timeoutSec") == 30
                for entry in result.get("data", []) if not entry.get("errors")
                for h in entry.get("hooks", []))
    if not valid:
        raise RunError("the Aiset publishing hook is missing, disabled or needs review. "
                       "Run aiset-init --provider codex, start aiset pm, and trust the Aiset hook in /hooks; then retry.")


def argv(board_dir, *, session_id=None, model=None, effort=None):
    # These are session-only overrides; never weaken the user's sandbox or approval settings.
    args = ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "never",
            "-c", "sandbox_workspace_write.network_access=false"]
    if model:
        args += ["--model", model]
    if effort:
        args += ["-c", "model_reasoning_effort=" + quoted(effort)]
    args += ["exec"]
    if session_id:
        args += ["resume", session_id]
    args += ["--json", "-"]
    return args


def run(args, cwd, board_dir, on_session):
    executable = program("codex", cwd)
    prompt = ("Read .codex/aiset/pm.md and act as the Aiset PM. " + start_prompt(board_dir, interactive=False) +
              " Publishing is performed only by the host after the user's gate 2 approval. "
              "Do not commit, push or open a PR. If blocked, record it and end the session.")
    # Issue text never becomes shell syntax or an argv prompt; stdin also avoids Windows length/quoting limits.
    process = subprocess.Popen([executable, *args[1:]], cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    failed = False
    try:
        process.stdin.write(prompt + "\n")
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                sid = event.get("thread_id")
                if isinstance(sid, str):
                    on_session(sid)
            elif event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    print(visible_text(item.get("text") or "").text)
                elif item.get("type") in ("command_execution", "file_change"):
                    print("Codex: " + item["type"].replace("_", " "))
            elif event.get("type") in ("error", "turn.failed"):
                failed = True
                print("Codex reported an error; inspect the session in Codex before publishing.")
        code = process.wait()
        return code or (1 if failed else 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
