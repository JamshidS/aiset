"""Conservative shell file targets, for display only; never executes a command."""
import os
import re
import shlex


def targets(command, cwd):
    from ..core import bash_targets
    out = []
    for kind, path, _ in bash_targets(command):
        out.append((kind, path if os.path.isabs(path) else os.path.join(cwd, path)))
    # Only literal PowerShell paths are inferred. Variables, globbing and expressions stay unknown.
    for segment in re.split(r"[;|\n]", command):
        try:
            words = shlex.split(segment, posix=False)
        except ValueError:
            continue
        if not words:
            continue
        verb = words.pop(0).lower()
        if verb not in ("get-content", "gc", "set-content", "add-content", "select-string"):
            continue
        path = None
        for i, word in enumerate(words):
            if word.lower() in ("-path", "-literalpath") and i + 1 < len(words):
                path = words[i + 1]
                break
        if path is None and words and not words[0].startswith("-") and verb != "select-string":
            path = words[0]
        if not path:
            continue
        quoted = len(path) > 1 and path[0] in "\"'" and path[-1] == path[0]
        path = path.strip("\"'")
        if any(c in path for c in ("$`*?[]" if quoted else "$`*?[]{},()")):
            continue
        path = path if os.path.isabs(path) else os.path.join(cwd, path)
        out.append(("edit" if verb in ("set-content", "add-content") else "search" if verb == "select-string" else "read", path))
    return list(dict.fromkeys(out))
