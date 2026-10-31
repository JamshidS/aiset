"""The office's own settings (layout, time windows), kept in <team>/settings.json, and the
roster edits the Settings page makes.

The team itself (names, roles, colours, duties, models) stays in roster.json, where init_team.py
and the agent files already read it; this module only validates what the page sends.
"""
import copy
import json
import os
import re

from .atomic import write_text_atomic
from . import providers, workspace

PATH = workspace.TEAM + "/settings.json"

DEFAULTS = {
    "hours": 12,          # office time window
    "brainDays": 30,      # how far back the Brain looks
    "ui": {
        "view": "iso",                                                  # iso | classic
        "tabs": {"board": True, "boards": True, "feed": True},          # side-panel tabs
        "defaultTab": "board",
        "rooms": {"lounge": True, "qa": True, "infra": True, "bench": True},
        "pages": {"brain": True, "architecture": True},
        "sidePanel": True,
        "stats": True,      # token and cost pills in the top bar
        "hint": True,       # "talk to your PM" line under the side panel
        "beams": True,      # assignment lines from the PM to the agents
        "alert": True,      # red "needs you" banner
    },
}

# what an agent file's `model:` line accepts: an alias or a full model id
MODELS = [
    {"id": "", "label": "Default", "note": "Claude Code decides (usually the same model as the session)"},
    {"id": "inherit", "label": "Inherit", "note": "the model of the session that assigns the work"},
    {"id": "opus", "label": "Opus", "note": "hardest design and debugging work"},
    {"id": "sonnet", "label": "Sonnet", "note": "everyday feature work"},
    {"id": "haiku", "label": "Haiku", "note": "lookups and mechanical edits, cheapest"},
    {"id": "fable", "label": "Fable", "note": "the newest frontier model"},
]
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-\[\]]{0,63}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def models(roster):
    if providers.name(roster) == "codex":
        return [{"id": "", "label": "PM decides", "note": "The PM chooses per assignment, or inherits the session. A custom model id pins your choice."}]
    return MODELS


def agents_path(roster):
    return providers.agents_dir(workspace.WORKSPACE, "codex") if providers.name(roster) == "codex" else workspace.AGENTS


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k not in base:
            continue  # unknown keys are dropped, so a typo can't break the pages
        if isinstance(base[k], dict):
            out[k] = _merge(base[k], v if isinstance(v, dict) else {})
        elif isinstance(base[k], bool):
            out[k] = bool(v)
        elif isinstance(base[k], (int, float)) and not isinstance(v, bool) and isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(base[k], str) and isinstance(v, str):
            out[k] = v
    return out


def _clean(s):
    s = _merge(DEFAULTS, s)
    s["hours"] = min(24 * 30, max(0.25, float(s["hours"])))
    s["brainDays"] = min(365, max(1, float(s["brainDays"])))
    ui = s["ui"]
    if ui["view"] not in ("iso", "classic"):
        ui["view"] = "iso"
    shown = [t for t, on in ui["tabs"].items() if on]
    if ui["defaultTab"] not in shown:
        ui["defaultTab"] = shown[0] if shown else "board"
    return s


def load():
    try:
        with open(PATH, encoding="utf-8-sig") as f:
            return _clean(json.load(f))
    except (OSError, ValueError):
        return _clean({})


def save(patch):
    """Merge patch into the stored settings and write them; returns the new settings."""
    cur = load()
    new = _clean(_merge(cur, patch))
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    write_text_atomic(PATH, json.dumps(new, indent=2), newline="\n")
    return new


def _text(v, limit, field):
    """One line of plain text: no characters that would break the agent files' YAML or Markdown."""
    s = "".join(c for c in str(v or "") if c not in '"`<>\\|*·')
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        raise ValueError(f"{field} can't be empty")
    return s[:limit]


def apply_roster(roster, edit):
    """A copy of roster with the Settings page's edits; raises ValueError on bad input.

    Only these fields change: team, pmCommand, and per existing member name, role, color, duty, model.
    Members are matched by id; ids, zones, repos and knowledge files stay as init_team.py wrote them.
    """
    if not isinstance(edit, dict):
        raise ValueError("roster edit must be an object")
    out = copy.deepcopy(roster)
    if not isinstance(out, dict) or not isinstance(out.get("members"), list):
        raise ValueError("roster must be an object with a 'members' list")
    if "team" in edit:
        out["team"] = _text(edit["team"], 60, "Team name")
    if "pmCommand" in edit:
        out["pmCommand"] = _text(edit["pmCommand"], 160, "PM command")
    by_id = {m["id"]: m for m in out["members"] if isinstance(m, dict) and "id" in m}
    members = edit.get("members")
    if members is not None and not isinstance(members, list):
        raise ValueError("members must be a list")
    for e in members or []:
        if not isinstance(e, dict):
            raise ValueError("each member edit must be an object")
        if not isinstance(e.get("id"), str):
            raise ValueError("each member edit needs a string 'id'")
        m = by_id.get(e.get("id"))
        if m is None:
            raise ValueError(f"unknown agent {e.get('id')!r}")
        if "name" in e:
            m["name"] = _text(e["name"], 40, "Name")
        if "role" in e:
            m["role"] = _text(e["role"], 60, "Role")
        if "duty" in e:
            m["duty"] = _text(e["duty"], 400, "Duty")
        if "color" in e:
            if not COLOR_RE.match(str(e["color"])):
                raise ValueError(f"{m['name']}: colour must look like #22c55e")
            m["color"] = e["color"].lower()
        if "model" in e:
            model = str(e["model"] or "").strip().lower()
            if model and not MODEL_RE.match(model):
                raise ValueError(f"{m['name']}: {model!r} isn't a model alias or id")
            if providers.name(out) == "codex":
                from .providers.codex import options
                m.setdefault("codex", {})["model"] = model
                options(m)
            elif model:
                m["model"] = model
            else:
                m.pop("model", None)
        if "reasoning" in e and providers.name(out) == "codex":
            from .providers.codex import options
            m.setdefault("codex", {})["reasoning"] = e["reasoning"]
            options(m)
    return out


def agent_files(roster):
    """Per member: whether its agent file exists, the model it has now, and whether init_team regenerates it."""
    if providers.name(roster) == "codex":
        from .providers.codex import agent_files as codex_files
        return codex_files(workspace.WORKSPACE, roster)
    out = {}
    for m in roster["members"]:
        path = os.path.join(workspace.AGENTS, m["id"] + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            out[m["id"]] = {"exists": False}
            continue
        front = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
        model = re.search(r"^model:\s*(.+?)\s*$", front.group(1), re.M) if front else None
        out[m["id"]] = {"exists": True, "path": workspace.norm(path), "model": model.group(1) if model else "",
                        "generated": "<!-- generated by " in text}
    return out
