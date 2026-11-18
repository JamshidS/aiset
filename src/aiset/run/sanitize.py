"""Make hidden characters in issue text visible and count HTML comments."""
from __future__ import annotations

import re
import unicodedata

from .datatypes import Visible


_HIDDEN_CATEGORIES = {"Cf": "format", "Cc": "control", "Co": "private"}
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)


def visible_text(text: str) -> Visible:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    comments = sum(1 for _ in _HTML_COMMENT.finditer(text))
    counts = {}
    fragments = []
    for index, char in enumerate(text):
        codepoint = ord(char)
        if 0xE0000 <= codepoint <= 0xE007F:
            kind = "tag"
        elif char in "\n\t":
            kind = None
        elif (char == "\u200d" and 0 < index < len(text) - 1
              and unicodedata.category(text[index - 1]) == "So"
              and unicodedata.category(text[index + 1]) == "So"):
            kind = None
        else:
            kind = _HIDDEN_CATEGORIES.get(unicodedata.category(char))

        if kind is None:
            fragments.append(char)
        else:
            fragments.append(f"<U+{codepoint:04X}>")
            counts[kind] = counts.get(kind, 0) + 1

    if comments:
        counts["html_comment"] = comments
    return Visible("".join(fragments), counts)
