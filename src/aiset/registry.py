"""The teams aiset-init has made on this machine, so the office and the launchers work from any folder.

  ~/.aiset/workspaces.json   {"workspaces": [{"team": "beanboard", "path": "C:/work/beanboard", "updated": "..."}]}
  <scripts dir>/<team>-pm    a launcher: cd into the workspace, then `claude --agent pm` (.cmd on Windows)

AISET_HOME overrides ~/.aiset. This module has no side effects on import (unlike workspace.py), so
server.py can use it to pick the workspace before the other modules read their paths.
"""
import datetime
import json
import os
import re
import shlex
import shutil
import sys

from .atomic import write_text_atomic

MARKER = "aiset launcher"


def home():
    return os.environ.get("AISET_HOME") or os.path.join(os.path.expanduser("~"), ".aiset")


def _file():
    return os.path.join(home(), "workspaces.json")


def _has_team(path):
    return os.path.isfile(os.path.join(path, ".claude", "team", "roster.json"))


def enclosing(d):
    """The nearest folder at or above d that has a team, or None."""
    d = os.path.abspath(d)
    while True:
        if _has_team(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def workspaces():
    """Registered workspaces that still have a team, most recently initialised first."""
    try:
        with open(_file(), encoding="utf-8-sig") as f:
            items = json.load(f).get("workspaces", [])
    except (OSError, ValueError):
        return []
    items = [w for w in items if isinstance(w, dict) and w.get("path") and _has_team(w["path"])]
    return sorted(items, key=lambda w: w.get("updated", ""), reverse=True)


def register(team, path):
    path = os.path.abspath(path)
    key = os.path.normcase(path)
    items = [w for w in workspaces() if os.path.normcase(os.path.abspath(w["path"])) != key]
    items.insert(0, {"team": team, "path": path, "updated": datetime.datetime.now().isoformat(timespec="seconds")})
    os.makedirs(home(), exist_ok=True)
    text = json.dumps({"workspaces": items}, indent=2, ensure_ascii=False) + "\n"
    write_text_atomic(_file(), text, newline="\n")


def lookup(name):
    """A registered workspace path by team name (or launcher slug), case-insensitive; None if unknown."""
    want = name.strip().lower()
    for w in workspaces():
        if want in (str(w.get("team", "")).lower(), slug(w.get("team", ""))):
            return w["path"]
    return None


def slug(team):
    """The launcher name stem: "Bean Board" -> "bean-board"."""
    return re.sub(r"[^a-z0-9]+", "-", str(team).lower()).strip("-") or "team"


def scripts_dir():
    """The folder aiset's own commands are in when it's on PATH, else ~/.aiset/bin (the caller says so)."""
    for cmd in ("aiset-init", "aiset", "teammap"):
        found = shutil.which(cmd)
        if found:
            return os.path.dirname(os.path.abspath(found)), True
    return os.path.join(home(), "bin"), False


def write_launcher(team, path, bin_dir, provider="claude"):
    """Write <slug>-pm into bin_dir. Returns its path, or None when a file of ours can't be written there
    (a command of the same name that aiset didn't make is left alone)."""
    name = slug(team) + "-pm"
    path = os.path.abspath(path)
    if provider not in ("claude", "codex"):
        raise ValueError(f"unknown coding provider {provider!r}")
    command = "claude --agent pm"
    if provider == "codex":
        script = os.path.join(os.path.dirname(__file__), "launch_pm.py")
        if os.name == "nt":
            command = (f'"{sys.executable.replace("%", "%%")}" "{script.replace("%", "%%")}" '
                       f'--workspace "{path.replace("%", "%%")}"')
        else:
            command = shlex.quote(sys.executable) + " " + shlex.quote(script) + " --workspace " + shlex.quote(path)
    if os.name == "nt":
        target = os.path.join(bin_dir, name + ".cmd")
        # cmd.exe runs claude.cmd here, so PowerShell's script execution policy doesn't apply;
        # setlocal gives the folder back to an interactive cmd.exe when the session ends
        text = (f"@echo off\r\nrem {MARKER} for the {team} team's PM (made by aiset-init)\r\nsetlocal\r\n"
                f'cd /d "{path.replace("%", "%%")}" || exit /b 1\r\n{command} %*\r\n')
        if provider == "codex":
            text = text.replace("setlocal\r\n", "setlocal DisableDelayedExpansion\r\n")
    else:
        target = os.path.join(bin_dir, name)
        text = (f"#!/bin/sh\n# {MARKER} for the {team} team's PM (made by aiset-init)\n"
                f"cd {shlex.quote(path)} || exit 1\nexec {command} \"$@\"\n")
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8", errors="replace") as f:
                if MARKER not in f.read(400):
                    return None
        except OSError:
            return None
    os.makedirs(bin_dir, exist_ok=True)
    write_text_atomic(target, text, newline="")
    if os.name != "nt":
        mode = os.stat(target).st_mode
        os.chmod(target, mode | (mode & 0o444) >> 2)  # like chmod +x: executable by whoever may read it (umask decides)
    return target


def resolve(arg):
    """--workspace accepts a folder or a registered team name."""
    if os.path.isdir(arg):
        return os.path.abspath(arg)
    return lookup(arg) or os.path.abspath(arg)
