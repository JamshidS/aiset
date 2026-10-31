"""Runtime adapter boundary shared by the Office, Brain and task views."""


class ClaudeRuntime:
    name = "claude"

    def scan(self, office):
        return office._scan_claude()


class CodexRuntime:
    name = "codex"

    def scan(self, office):
        from .codex_runtime import scan
        return scan(office)


def runtime(roster):
    from . import name
    return CodexRuntime() if name(roster) == "codex" else ClaudeRuntime()
