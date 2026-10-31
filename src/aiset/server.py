#!/usr/bin/env python3
"""aiset: a live office view of your Claude Code agent team, in the browser.

  aiset                        start on http://127.0.0.1:8765 and open the browser
  aiset --port 9000            use another port
  aiset --hours 24             include agents active in the last 24 hours (default 12)
  aiset --no-browser           don't open a browser tab
  aiset --brain-days 60        how far back the Brain page looks (default 30)
  aiset --workspace DIR|TEAM   show the team of a workspace made by init_team.py, by folder or team name
                               (default: the workspace you're in, else the team aiset-init set up most
                               recently, else the original ~/.claude team)

Settings (/settings) edits the roster (names, roles, colours, duties, models) and the office's
own options (<team>/settings.json); --hours and --brain-days override the stored windows.
"""
import argparse
import errno
import json
import math
import os
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys

if __name__ == "__main__" and not __package__:  # run as a file: python src/aiset/server.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    __package__ = "aiset"
HERE = os.path.dirname(os.path.realpath(__file__))
from . import providers, registry  # noqa: E402
from .atomic import write_text_atomic  # noqa: E402

# every module reads its paths from workspace.py on import, so settle the workspace first:
# --workspace (a folder or a team name aiset-init registered), the workspace you're in,
# else the team aiset-init set up most recently
_picked = None
for _i, _a in enumerate(sys.argv):
    if _a == "--workspace" and _i + 1 < len(sys.argv):
        os.environ["AISET_WORKSPACE"] = registry.resolve(sys.argv[_i + 1])
    elif _a.startswith("--workspace="):
        os.environ["AISET_WORKSPACE"] = registry.resolve(_a.split("=", 1)[1])
if not (os.environ.get("AISET_WORKSPACE") or os.environ.get("AGENT_OFFICE_WORKSPACE") or registry.enclosing(os.getcwd())):
    _known = registry.workspaces()
    if _known:
        os.environ["AISET_WORKSPACE"] = _known[0]["path"]
        _picked = _known
from . import brain, brief_index, settings, sync_names, workspace  # noqa: E402
from .core import Office, board_detail, task_detail, task_routes  # noqa: E402


ROSTER = workspace.ROSTER
_roster_mtime = None
COLOR = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
FALLBACK_COLOR = "#94a3b8"


def safe_colors(roster):
    """Colours go into style attributes on the office and Brain pages, and roster.json is edited by hand:
    anything that isn't a #hex colour falls back to grey."""
    def ok(c):
        return isinstance(c, str) and COLOR.fullmatch(c)
    for m in roster["members"]:
        if "color" in m and not ok(m["color"]):
            m["color"] = FALLBACK_COLOR
    helpers = roster.get("helpers")
    if isinstance(helpers, dict) and isinstance(helpers.get("agents"), dict):
        for a in helpers["agents"].values():
            if isinstance(a, list) and len(a) > 2 and not ok(a[2]):
                a[2] = FALLBACK_COLOR


def reload_roster(office, force=False):
    """Pick up edits to roster.json without a restart, and copy new names and models into the agent files."""
    global _roster_mtime
    try:
        mtime = os.path.getmtime(ROSTER)
        if mtime == _roster_mtime and not force:
            return None
        roster = workspace.load_roster()
        if not isinstance(roster, dict) or not isinstance(roster.get("members"), list):
            raise ValueError("roster must be an object with a 'members' list")
        for m in roster["members"]:
            if (
                not isinstance(m, dict)
                or not isinstance(m.get("id"), str)
                or not m["id"]
                or not isinstance(m.get("name"), str)
                or not m["name"]
            ):
                raise ValueError("each member must be an object with non-empty string 'id' and 'name'")
        if providers.name(roster) == "codex":
            from .providers.codex import options
            for member in roster["members"]:
                options(member)
        safe_colors(roster)
    except (OSError, ValueError) as e:  # mid-edit, invalid JSON or bad shape: keep the last good roster
        print(f"roster.json not loaded: {e}")
        return None
    first = _roster_mtime is None
    _roster_mtime = mtime
    if office is not None:
        office.roster = roster
        if Handler.brain_office is not None:
            Handler.brain_office.roster = roster
    try:
        changed = sync_names.sync(roster)
        if changed and not first:
            print("roster changed → updated agent files:", ", ".join(changed))
    except (OSError, ValueError, TypeError) as e:
        print(f"agent files not updated: {e}")
    return roster


def apply_windows(s):
    """Use the stored time windows, unless --hours / --brain-days were given."""
    if Handler.office is not None and not Handler.fixed_hours:
        Handler.office.window = s["hours"] * 3600
    if Handler.brain_office is not None and not Handler.fixed_days:
        Handler.brain_office.window = s["brainDays"] * 86400


class Handler(BaseHTTPRequestHandler):
    office = None
    brain_office = None  # same parser, longer window
    fixed_hours = fixed_days = False
    port = 8765
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # the browser left (reload, navigation, closed tab) before the reply was written; nothing to do

    def do_GET(self):
        if not self._host_ok():  # a page on another domain that resolves to 127.0.0.1 (DNS rebinding) can't read
            return self._send(403, b"forbidden", "text/plain")
        path = self.path.split("?", 1)[0]
        # Explicit allowlist: never expose arbitrary files from the package.
        assets = {
            "/favicon.ico": ("favicon.ico", "image/x-icon"),
            "/site.webmanifest": ("site.webmanifest", "application/manifest+json"),
            **{"/static/" + name: (name, "image/png") for name in (
                "aiset-icon.png", "favicon-16.png", "favicon-32.png", "favicon-48.png",
                "apple-touch-icon.png", "icon-192.png", "icon-512.png",
            )},
        }
        if path in assets:
            filename, content_type = assets[path]
            with open(os.path.join(HERE, "static", filename), "rb") as f:
                return self._send(200, f.read(), content_type)
        if path == "/api/state":
            with self.lock:
                reload_roster(self.office)
                brief_index.refresh()
                state = self.office.state()
                state["ui"] = settings.load()["ui"]
                state["provider"] = providers.name(self.office.roster)
                state["telemetrySupported"] = True
                state["telemetry"] = {"source": "native-rollouts" if state["provider"] == "codex" else "claude-transcripts",
                                      "fileReads": "command-analysis", "cost": "reference-or-unavailable"}
            return self._send(200, json.dumps(state).encode(), "application/json")
        if path == "/api/detail":
            from urllib.parse import parse_qs, urlparse
            key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
            with self.lock:
                item = self.office.detail(key)
            return self._send(200 if item else 404, json.dumps(item).encode(), "application/json")
        if path == "/api/board":
            from urllib.parse import parse_qs, urlparse
            item = board_detail(parse_qs(urlparse(self.path).query).get("id", [""])[0])
            return self._send(200 if item else 404, json.dumps(item).encode(), "application/json")
        if path == "/api/task/routes":
            from urllib.parse import parse_qs, urlparse
            tid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            with self.lock:
                item = task_routes(self.office, tid) if tid else None
            return self._send(200 if item else 404, json.dumps(item).encode(), "application/json")
        if path == "/api/task":
            from urllib.parse import parse_qs, urlparse
            tid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            with self.lock:
                item = task_detail(self.office, tid) if tid else None
            return self._send(200 if item else 404, json.dumps(item).encode(), "application/json")
        if path == "/api/brain/task":
            from urllib.parse import parse_qs, urlparse
            tid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            with self.lock:
                if not self.office.task_usage:
                    self.office.state()
                data = brain.task_trails(self.office, self.office.roster, tid) if tid else None
            return self._send(200 if data else 404, json.dumps(data).encode(), "application/json")
        if path == "/api/brain/live":
            from urllib.parse import parse_qs, urlparse
            try:
                since = float(parse_qs(urlparse(self.path).query).get("since", ["1800"])[0])
                if not math.isfinite(since) or since < 0:
                    since = 1800.0
            except (ValueError, TypeError):
                since = 1800.0
            with self.lock:
                data = brain.live(self.office, self.office.roster, since)
            return self._send(200, json.dumps(data).encode(), "application/json")
        if path == "/api/brain":
            with self.lock:
                reload_roster(self.office)
                graph = brain.build(self.brain_office, self.office.roster)
            return self._send(200, json.dumps(graph).encode(), "application/json")
        if path in ("/api/arch", "/api/arch/mermaid"):
            from urllib.parse import parse_qs, urlparse
            from . import archmap
            q = parse_qs(urlparse(self.path).query)
            with self.lock:
                data = archmap.get(0 if q.get("refresh") else 60)
            if path.endswith("mermaid"):
                return self._send(200, archmap.mermaid(data, bool(q.get("all"))).encode(), "text/plain; charset=utf-8")
            return self._send(200, json.dumps(data).encode(), "application/json")
        if path == "/api/settings":
            with self.lock:
                return self._send(200, json.dumps(self.settings_payload()).encode(), "application/json")
        page = {"/": "index.html", "/index.html": "index.html", "/brain": "brain.html", "/architecture": "architecture.html",
                "/settings": "settings.html"}.get(path)
        if page:
            with open(os.path.join(HERE, "static", page), encoding="utf-8") as f:
                html = f.read()
            # the pages were written for the first team; a workspace roster names its own
            roster = self.office.roster
            safe = lambda s: "".join(c for c in str(s) if c not in "\"'`<>\\")  # lands in JS strings and HTML
            html = html.replace("__PM_COMMAND__", safe(roster.get("pmCommand", "claude --agent pm"))).replace("__TEAM_NAME__", safe(roster.get("team", "AI Team")))
            html = html.replace("__PROVIDER_LABEL__", "Codex" if providers.name(roster) == "codex" else "Claude")
            if providers.name(roster) == "codex":
                notice = ('<p role="status" style="margin:0;padding:12px 22px;background:#312e81;color:#fff;font:14px system-ui">'
                          'Codex: activity and tokens come from native session logs. Shell file reads are inferred. '
                          'Costs, when available, are reference estimates, not subscription charges.</p>')
                html = html.replace("<body>", '<body data-provider="codex">' + notice)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        return self._send(404, b"not found", "text/plain")

    def settings_payload(self):
        roster = self.office.roster
        return {
            "settings": settings.load(), "roster": roster, "models": settings.models(roster),
            "provider": providers.name(roster),
            "agentFiles": settings.agent_files(roster),
            "fixed": {"hours": self.fixed_hours, "brainDays": self.fixed_days},
            "paths": {"roster": workspace.norm(ROSTER), "agents": settings.agents_path(roster), "settings": settings.PATH,
                      "workspace": workspace.norm(workspace.WORKSPACE) if workspace.WORKSPACE else None},
        }

    def _is_us(self, host):
        """host[:port] names this server: 127.0.0.1 or localhost (any case, trailing dot), on our port.
        Browsers leave out the port when it's 80."""
        name, _, port = (host or "").strip().lower().rpartition(":")
        if not name or not port.isdigit():
            name, port = (host or "").strip().lower(), "80"
        return name.rstrip(".") in ("127.0.0.1", "localhost") and int(port) == self.port

    def _host_ok(self):
        """The request names this server as its Host, so no other domain can reach it by DNS rebinding."""
        return self._is_us(self.headers.get("Host"))

    def _same_origin(self):
        """Writes come only from the office's own pages: the right Host and no foreign Origin."""
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        return origin is None or (origin.lower().startswith("http://") and self._is_us(origin[len("http://"):]))

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path != "/api/settings":
            return self._send(404, b"not found", "text/plain")
        # a JSON content type makes a cross-site request preflight, which this server never answers
        if not self._same_origin() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, b'{"error": "forbidden"}', "application/json")
        try:
            length = max(0, min(int(self.headers.get("Content-Length") or 0), 1 << 20))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return self._send(400, b'{"error": "not JSON"}', "application/json")
        if not isinstance(body, dict):
            return self._send(400, b'{"error": "body must be an object"}', "application/json")
        with self.lock:
            try:
                if "roster" in body:
                    if not isinstance(body["roster"], dict):
                        raise ValueError("roster must be an object")
                    roster = settings.apply_roster(self.office.roster, body["roster"])
                    write_text_atomic(ROSTER, json.dumps(roster, indent=2, ensure_ascii=False) + "\n", newline="\n")
                    reload_roster(self.office, force=True)
                if "settings" in body:
                    if not isinstance(body["settings"], dict):
                        raise ValueError("settings must be an object")
                    apply_windows(settings.save(body["settings"]))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except OSError as e:
                return self._send(500, json.dumps({"error": f"couldn't write: {e}"}).encode(), "application/json")
            return self._send(200, json.dumps(self.settings_payload()).encode(), "application/json")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    ap = argparse.ArgumentParser(description="Live office view of your Claude Code agents.",
                                 epilog="Also: aiset run <issue> works a GitHub issue with the team and opens a draft PR after your approval (aiset run --help).")
    from . import __version__
    ap.add_argument("--version", action="version", version="aiset " + __version__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--hours", type=float, help="show agents active within this many hours (default: Settings, 12)")
    ap.add_argument("--brain-days", type=float, help="how far back the Brain page looks (default: Settings, 30)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--workspace", help="folder made by init_team.py, or its team name (read before argparse, see top of file)")
    args = ap.parse_args()
    if args.workspace and not os.path.isfile(workspace.ROSTER):
        sys.exit(f"no team in {workspace.WORKSPACE} (expected {workspace.ROSTER}).\n"
                 "Run init_team.py on it first. In Git Bash write the path with / (C:/Users/... or /c/Users/...), not \\.")
    roster = reload_roster(None)
    if roster is None:
        sys.exit(f"could not read {ROSTER}")
    if _picked:
        print(f"Not inside a workspace: showing '{_picked[0]['team']}', the team aiset-init set up most recently.")
        if len(_picked) > 1:
            print("   Others: " + ", ".join(w["team"] for w in _picked[1:]) + "  (aiset --workspace <team name or folder>)")
    elif not workspace.WORKSPACE:
        print("No workspace found and no team set up with aiset-init: showing the original ~/.claude team.\n"
              "   Run aiset-init <folder> first, or use --workspace <folder>.")
    stored = settings.load()
    Handler.fixed_hours, Handler.fixed_days, Handler.port = args.hours is not None, args.brain_days is not None, args.port
    Handler.office = Office(roster, args.hours if Handler.fixed_hours else stored["hours"])
    Handler.brain_office = Office(roster, (args.brain_days if Handler.fixed_days else stored["brainDays"]) * 24)
    Handler.office.history = Handler.brain_office
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE or isinstance(e, PermissionError):
            detail = " (port in use or unavailable; another aiset office may be running)"
        else:
            detail = f": {e.strerror or e}"
        sys.exit(
            f"aiset: can't listen on 127.0.0.1:{args.port}{detail}. "
            f"Open http://127.0.0.1:{args.port}, or start with --port 0 for a free port."
        )
    Handler.port = server.server_address[1]  # the real port, also when --port 0 picked one
    url = f"http://127.0.0.1:{Handler.port}"
    print(f"🏢 {roster.get('team', 'Agent')} office running at {url}  (Ctrl+C to stop)")
    print(f"   team: {workspace.TEAM}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
