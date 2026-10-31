"""P5 per-task cost: `cost_line(usage) -> str`, `cost_table(usage) -> str`, from one value of
`Office.tasks_usage(now)` (src/aiset/core.py).

STUB: `cost_line` prints only `$x.xx` and `cost_table` returns "". Tokens, runs, agents, model families and
the "unknown cost" cases are the contributor issue: board 2026-10-29-aiset-run-design,
issues/06-p5-cost-line.md, interfaces.md "P5". Keep the signatures.
"""
from __future__ import annotations

from typing import Optional


def cost_line(usage: Optional[dict]) -> str:
    tokens = (usage or {}).get("tokens") or {}
    if not tokens.get("total"):
        return "cost not available (no transcripts found for this task)"
    if tokens.get("cost") is None:
        return "cost not available (model or price unknown)"
    if tokens.get("costBasis") == "configured-model-api-reference":
        return f"API reference estimate ${tokens['cost']:.2f} (configured model; not subscription billing)"
    return f"${tokens.get('cost') or 0:.2f}"


def cost_table(usage: Optional[dict]) -> str:
    return ""
