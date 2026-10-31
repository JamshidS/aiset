"""Start the workspace's native Codex PM, without changing Codex permissions."""
import argparse
import os
import shutil
import subprocess
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    __package__ = "aiset"

from . import providers, registry  # noqa: E402
from .providers import codex  # noqa: E402

PROMPT = "Read .codex/aiset/pm.md and act as this Aiset team's project manager. Follow the project's existing instructions."


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start an Aiset Codex PM. Extra flags are passed to Codex CLI.")
    parser.add_argument("--workspace", help="workspace folder or registered team name")
    parser.add_argument("--model", "-m", help="override the PM's Codex model")
    args, extra = parser.parse_known_args(argv)
    ws = registry.resolve(args.workspace) if args.workspace else (
        os.environ.get("AISET_WORKSPACE") or registry.enclosing(os.getcwd()))
    if not ws:
        known = registry.workspaces()
        ws = known[0]["path"] if known else None
    try:
        if not ws:
            raise ValueError("no Aiset team yet; run aiset-init <folder> --provider codex first")
        roster = providers.read_roster(ws)
        if providers.name(roster) != "codex":
            raise ValueError("this workspace uses Claude Code; use its <team>-pm launcher or claude --agent pm")
        providers.check_cli("codex")
        path = codex.safe_path(ws, ".codex/aiset/pm.md")
        if not os.path.isfile(path):
            raise ValueError("Codex PM instructions are missing; rerun aiset-init --provider codex")
        member = next(m for m in roster["members"] if m["id"] == "pm")
        model, effort = codex.options(member)
        command = [shutil.which("codex")]
        if args.model or model:
            command += ["--model", args.model or model]
        if effort:
            command += ["-c", "model_reasoning_effort=" + codex.quoted(effort)]
        command += extra + [PROMPT]
        return subprocess.call(command, cwd=ws)
    except (OSError, ValueError, KeyError, StopIteration) as e:
        parser.exit(2, f"aiset pm: {e or 'PM member is missing'}\n")


if __name__ == "__main__":
    sys.exit(main())
