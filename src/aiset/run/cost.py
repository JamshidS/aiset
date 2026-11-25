"""Format one task's usage from `Office.tasks_usage` as text or Markdown."""
from __future__ import annotations

import math
from typing import Optional


def _num(value) -> Optional[float]:
    """A finite int/float (not bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _agents(usage: dict) -> list:
    agents = usage.get("agents") or []
    return [agent for agent in agents if isinstance(agent, dict)] if isinstance(agents, list) else []


def _runs(agent: dict) -> int:
    runs = agent.get("runs")
    return runs if isinstance(runs, int) and not isinstance(runs, bool) and runs > 0 else 0


def _task_tokens(usage: dict) -> Optional[dict]:
    tokens = usage.get("tokens") or {}
    if not isinstance(tokens, dict) or not _num(tokens.get("total")):
        return None
    return tokens if sum(_runs(agent) for agent in _agents(usage)) else None


def _format_tokens(total) -> str:
    total = int(round(_num(total) or 0))
    if total < 1_000:
        return str(total)
    thousands = (total + 500) // 1_000
    if thousands < 1_000:
        return f"{thousands}k"
    return f"{total / 1_000_000:.2f}M"


def _format_cost(tokens: dict) -> str:
    cost = _num(tokens.get("cost")) or 0
    if _num(tokens.get("total")):
        if cost == 0:
            return "cost unknown"
        if 0 < cost < 0.01:
            return "<$0.01"
    return f"${cost:.2f}"


def _agent_name(agent: dict) -> str:
    return " ".join(str(agent.get("agent") or "").splitlines()).strip() or "unknown"


def cost_line(usage: Optional[dict]) -> str:
    if not isinstance(usage, dict):
        usage = {}
    tokens = _task_tokens(usage)
    if tokens is None:
        return "cost not available (no transcripts found for this task)"

    agents = _agents(usage)
    runs = sum(_runs(agent) for agent in agents)
    run_label = "run" if runs == 1 else "runs"
    names = []
    for agent in agents:
        name = _agent_name(agent)
        if _runs(agent) > 1:
            name += f" ×{_runs(agent)}"
        names.append(name)
    return (f"{_format_cost(tokens)} · {_format_tokens(tokens.get('total'))} tokens · {runs} {run_label}: "
            f"{', '.join(names)} (estimated at API list prices)")


def _table_row(name: str, runs: int, tokens: Optional[dict]) -> str:
    if tokens is None:  # agent reported no token data: don't claim $0.00
        return f"| {name} | {runs} | | - | - |"
    by_model = tokens.get("byModel") or {}
    by_model = by_model if isinstance(by_model, dict) else {}
    models = "+".join(sorted(by_model, key=lambda m: _num(by_model[m]) or 0, reverse=True))
    return f"| {name} | {runs} | {models} | {_format_tokens(tokens.get('total'))} | {_format_cost(tokens)} |"


def cost_table(usage: Optional[dict]) -> str:
    if not isinstance(usage, dict):
        usage = {}
    tokens = _task_tokens(usage)
    if tokens is None:
        return ""

    rows = ["| Agent | Runs | Model | Tokens | Cost |", "|---|---:|---|---:|---:|"]
    runs = 0
    for agent in _agents(usage):
        name = _agent_name(agent).replace("\\", "\\\\").replace("|", "\\|")
        agent_tokens = agent.get("tokens")
        rows.append(_table_row(name, _runs(agent), agent_tokens if isinstance(agent_tokens, dict) else None))
        runs += _runs(agent)
    rows.append(_table_row("**Total**", runs, tokens))
    return "\n".join(rows)
