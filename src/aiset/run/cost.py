"""Format one task's usage from `Office.tasks_usage` as text or Markdown."""
from __future__ import annotations

from typing import Optional


def _task_tokens(usage: dict) -> Optional[dict]:
    tokens = usage.get("tokens") or {}
    return tokens if usage.get("runs") and tokens.get("total") else None


def _format_tokens(total: int) -> str:
    if total < 1_000:
        return str(total)
    if total < 1_000_000:
        return f"{(total + 500) // 1_000}k"
    return f"{total / 1_000_000:.2f}M"


def _format_cost(tokens: dict) -> str:
    cost = tokens.get("cost") or 0
    if tokens.get("total"):
        if cost == 0:
            return "cost unknown"
        if 0 < cost < 0.01:
            return "<$0.01"
    return f"${cost:.2f}"


def _agent_name(agent: dict) -> str:
    return " ".join(str(agent.get("agent") or "").splitlines()).strip() or "unknown"


def cost_line(usage: Optional[dict]) -> str:
    usage = usage or {}
    tokens = _task_tokens(usage)
    if tokens is None:
        return "cost not available (no transcripts found for this task)"

    runs = sum(agent["runs"] for agent in usage["agents"])
    run_label = "run" if runs == 1 else "runs"
    names = []
    for agent in usage["agents"]:
        name = _agent_name(agent)
        if agent["runs"] > 1:
            name += f" ×{agent['runs']}"
        names.append(name)
    return (f"{_format_cost(tokens)} · {_format_tokens(tokens['total'])} tokens · {runs} {run_label}: "
            f"{', '.join(names)} (estimated at API list prices)")


def _table_row(name: str, runs: int, tokens: dict) -> str:
    by_model = tokens.get("byModel") or {}
    models = "+".join(sorted(by_model, key=by_model.get, reverse=True))
    return f"| {name} | {runs} | {models} | {_format_tokens(tokens['total'])} | {_format_cost(tokens)} |"


def cost_table(usage: Optional[dict]) -> str:
    usage = usage or {}
    tokens = _task_tokens(usage)
    if tokens is None:
        return ""

    rows = ["| Agent | Runs | Model | Tokens | Cost |", "|---|---:|---|---:|---:|"]
    runs = 0
    for agent in usage["agents"]:
        name = _agent_name(agent).replace("\\", "\\\\").replace("|", "\\|")
        rows.append(_table_row(name, agent["runs"], agent["tokens"]))
        runs += agent["runs"]
    rows.append(_table_row("**Total**", runs, tokens))
    return "\n".join(rows)
