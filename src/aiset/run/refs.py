"""P7: parse bare, qualified and GitHub URL issue references without network access."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from .datatypes import IssueRef

_REPO = r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}"
_REF = re.compile(rf"(?:({_REPO})#|#?)([0-9]+)")
_PATH = re.compile(rf"/({_REPO})/(issues|pull)/([0-9]+)/?")


def parse_issue_ref(text: str, default_repo: Optional[str] = None) -> IssueRef:
    text = (text or "").strip()
    error = f"not an issue number or URL: {text!r}"
    if any(c in text for c in "\r\n\t"):
        raise ValueError(error)  # urlsplit would silently remove these inside owner/name
    m = _REF.fullmatch(text)
    kind = "issues"
    if m:
        repo, number = m.group(1) or default_repo, int(m.group(2))
    else:
        # A shorthand's # is significant; URL queries and fragments are not.
        has_scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text)
        url = urlsplit(text if has_scheme else "https://" + text)
        if url.netloc.lower() not in ("github.com", "www.github.com"):
            if has_scheme or "/" in text:
                raise ValueError("only github.com issues are supported")
            raise ValueError(error)
        m = _PATH.fullmatch(url.path)
        if url.scheme.lower() != "https" or not m:
            raise ValueError(error)
        repo, kind, digits = m.groups()
        number = int(digits)
    if number < 1:
        raise ValueError("issue numbers start at 1")
    if kind == "pull":
        raise ValueError(f"#{number} is a pull request, not an issue")
    return IssueRef(repo=repo, number=number)
