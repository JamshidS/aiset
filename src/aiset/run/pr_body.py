"""P6 PR description: `render_pr_body(PrInput) -> str` (pr.md, exactly what is posted) and `neutralize(text)`.

STUB with a security floor: `neutralize` already turns @mentions, #12, GH-12, owner/repo#12 and issue/PR URLs
(any case, with or without the scheme) into inline code outside code spans and fences, escapes `<` before a
tag-like character (raw HTML: `<!--`, `<img>`, `<details>`) and `&` before entities other than lt/gt/amp/quot/
apos/nbsp (`&#64;` would turn back into `@`), so issue and agent text in the PR can't ping, backlink, close
issues, load images or hide text. Fences follow CommonMark where it matters for safety: at most 3 spaces of
indent, a backtick fence's info string has no backtick (```x``` is a code span), the closing fence uses the same
character and is at least as long; a code span never crosses a blank line or starts after a backslash. When in
doubt the text is treated as plain, so the error is over-neutralising, never a live reference. `render_pr_body`
writes the sections in order with a simple layout and a plain length cap. Still open for the contributor: the
exact layout and wording per verdict status, cutting at a line boundary, the golden fixture
tests/fixtures/run/pr_body.md, and the remaining neutralize edge cases (a reference right next to a stray
backtick, fences inside list items and block quotes are neutralised as plain text). Issue: board
2026-10-29-aiset-run-design, issues/07-p6-pr-body.md, interfaces.md "P6". Keep the signatures.
"""
from __future__ import annotations

import re

from .datatypes import PrInput

NOTE = ("> Draft opened by `aiset run` after the maintainer approved it. The change was made by an AI agent team; "
        "review it as you would any outside contribution.")
MAX_CHARS = 60000
TRUNCATED = "\n\n[truncated; the full report is on the board]"

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_CODE_SPAN = re.compile(r"(?<![`\\])(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_BLANK_LINE = re.compile(r"(\n[ \t]*\n)")
_ENTITY = re.compile(r"&(?!(?:lt|gt|amp|quot|apos|nbsp);)(?=#?[A-Za-z0-9]+;)", re.I)
_TAG = re.compile(r"<(?=[A-Za-z/!?])")
_REF = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/(?:issues|pull)/\d+[^\s)\]>`]*"
    r"|(?<![\w/.])[A-Za-z0-9-]+/[A-Za-z0-9._-]+#\d+"
    r"|(?<!\w)GH-\d+\b"
    r"|(?<![\w&/])#\d+\b"
    r"|(?<![\w.+-])@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_.-]+)?",
    re.I,
)


def _plain(text: str) -> str:
    text = _TAG.sub("&lt;", _ENTITY.sub("&amp;", text))
    return _REF.sub(lambda m: f"`{m.group(0)}`", text)


def _paragraph(text: str) -> str:
    out, pos = [], 0
    for m in _CODE_SPAN.finditer(text):
        out += [_plain(text[pos:m.start()]), m.group(0)]
        pos = m.end()
    out.append(_plain(text[pos:]))
    return "".join(out)


def _outside_spans(text: str) -> str:
    """Code spans live inside one paragraph: a blank line ends any span that's still open."""
    parts = _BLANK_LINE.split(text)
    return "".join(p if i % 2 else _paragraph(p) for i, p in enumerate(parts))


def _fence_open(line: str):
    m = _FENCE_OPEN.match(line)
    if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
        return None  # ```x``` on one line is a code span, not a fence
    return m.group(1)


def _fence_closes(line: str, marker: str) -> bool:
    m = _FENCE_CLOSE.match(line)
    return bool(m) and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker)


def neutralize(text: str) -> str:
    """Issue or agent text made safe to post: no pings, no cross-references, no closing keywords that work,
    no raw HTML (hidden comments, images, details). Code spans and fenced blocks are left as written."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    chunk, out, fence = [], [], None
    for line in lines:
        if fence is None:
            marker = _fence_open(line)
            if marker:
                out.append(_outside_spans("\n".join(chunk)) if chunk else None)
                chunk, fence = [], marker
                out.append(line)
            else:
                chunk.append(line)
        else:
            out.append(line)
            if _fence_closes(line, fence):
                fence = None
    if chunk:
        out.append(_outside_spans("\n".join(chunk)))
    return "\n".join(x for x in out if x is not None)


def _ac_line(ac, verdict) -> str:
    status = verdict.status if verdict else None
    evidence = f" · {neutralize(verdict.evidence)}" if verdict and verdict.evidence else ""
    if status == "pass":
        return f"- [x] **{ac.id}** {neutralize(ac.text)}: pass{evidence}"
    if status == "done":
        return f"- [ ] **{ac.id}** {neutralize(ac.text)}: done, not verified{evidence}"
    if status in (None, "not-mine"):
        return f"- [ ] **{ac.id}** {neutralize(ac.text)}: open (no verdict)"
    return f"- [ ] **{ac.id}** {neutralize(ac.text)}: {status}{evidence}"


def render_pr_body(pr: PrInput) -> str:
    closes = f"Closes #{pr.number}"
    parts = [NOTE, "", "## Summary", neutralize(pr.summary.strip()) or "(no summary written)", "",
             "## Acceptance criteria"]
    parts += [_ac_line(ac, pr.verdicts.get(ac.id)) for ac in pr.acs] or ["(no acceptance criteria)"]
    parts += ["", "## Tests"]
    if pr.tester_status is None and not pr.checks:
        parts.append("No tester report.")
    else:
        parts.append(f"Tester: {pr.tester_status or 'no status'}")
        parts += [neutralize(c) for c in pr.checks]
    if pr.open_claims:
        parts += ["", "## Assumptions and open questions"]
        parts += [f"- [{c.label}] {neutralize(c.text)}" for c in pr.open_claims]
    counts = pr.claim_counts
    parts += ["", f"Claims: {counts.get('V', 0)} verified · {counts.get('S', 0)} assumed · {counts.get('?', 0)} open",
              "", "## Cost", neutralize(pr.cost), "",
              f"<sub>Board `{pr.board}` · branch `{pr.branch}`</sub>"]
    body = "\n".join(parts)
    room = MAX_CHARS - len(closes) - 2
    if len(body) > room:
        body = body[:room - len(TRUNCATED)] + TRUNCATED
    return f"{closes}\n\n{body}\n"
