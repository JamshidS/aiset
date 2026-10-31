"""Provider selection. Team memory stays in .claude/team for both providers."""
import json
import os
import shutil


def name(roster):
    if not isinstance(roster, dict):
        raise ValueError("roster must be an object")
    provider = roster.get("provider", "claude")
    if provider not in ("claude", "codex"):
        raise ValueError(f"unknown coding provider {provider!r}; choose claude or codex")
    return provider


def read_roster(ws):
    with open(os.path.join(ws, ".claude", "team", "roster.json"), encoding="utf-8-sig") as f:
        return json.load(f)


def check_cli(provider):
    """Keep legacy Claude initialization usable without its CLI, as before."""
    if provider == "codex" and not shutil.which("codex"):
        raise ValueError("Codex CLI was not found on PATH. Install Codex CLI, sign in, then run aiset-init again.")


def pm_command(provider):
    return "aiset pm" if provider == "codex" else "claude --agent pm"


def agents_dir(ws, provider):
    return os.path.join(ws, ".codex" if provider == "codex" else ".claude", "agents")
