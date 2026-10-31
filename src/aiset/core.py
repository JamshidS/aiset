"""Read Claude Code transcripts (~/.claude/projects) and turn them into office state.

Nothing here writes to the transcripts; files are parsed incrementally (only
appended bytes are read on each refresh), so polling every second is cheap.
"""
import json
import os
import re
import time
from collections import Counter, deque
from datetime import datetime
from glob import glob

from .workspace import BOARDS, CODE_ROOT, EPISODES, HOME, KNOWLEDGE, PROJECTS, TEAM, WORKSPACE, norm, relative, slug

HOME_PREFIX = slug(HOME) + "-"
# with a workspace, only its own sessions (and those in its repos) are on the floor
WS_SLUG = slug(WORKSPACE) if WORKSPACE else None
# "downloads-" for ~/Downloads/<workspace>, so projects are named from the workspace down
WS_PARENT = slug(os.path.dirname(WORKSPACE))[len(HOME_PREFIX):] + "-" if WORKSPACE and slug(os.path.dirname(WORKSPACE)).startswith(HOME_PREFIX) else None
TAIL_BYTES = 768 * 1024

WORKING, THINKING, WAITING, DONE, IDLE, OFF = "working", "thinking", "waiting", "done", "idle", "off"
STATE_RANK = {WAITING: 5, WORKING: 4, THINKING: 3, DONE: 2, IDLE: 1, OFF: 0}


def local_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().timestamp()
    except (ValueError, AttributeError):
        return None


def clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name, inp):
    """Short human description of a tool call, plus an activity kind for the UI."""
    inp = inp or {}
    base = lambda p: os.path.basename(str(p).rstrip("/")) or str(p)
    if name == "Bash":
        cmd = inp.get("command", "")
        kind = "test" if re.search(r"\b(pytest|jest|vitest|npm (run )?test|tsc|ruff|eslint|lint)\b", cmd) else "shell"
        return kind, inp.get("description") or clip(cmd, 120)
    if name == "Read":
        return "read", f"Reading {base(inp.get('file_path', ''))}"
    if name in ("Edit", "Write", "NotebookEdit"):
        return "edit", f"{'Writing' if name == 'Write' else 'Editing'} {base(inp.get('file_path') or inp.get('notebook_path', ''))}"
    if name in ("Grep", "Glob"):
        return "search", f"Searching for {clip(inp.get('pattern', ''), 60)}"
    if name in ("Agent", "Task"):
        return "delegate", f"Assigned {inp.get('subagent_type') or 'an agent'}: {inp.get('description', '')}"
    if name == "SendMessage":
        return "delegate", f"Message to {inp.get('to', 'agent')}: {clip(inp.get('summary') or inp.get('message', ''), 80)}"
    if name == "AskUserQuestion":
        qs = inp.get("questions") or [{}]
        return "ask", "Asking you: " + clip(qs[0].get("question", ""), 120)
    if name == "SubagentHandback":
        return "handback", "Handed the report back"
    if name in ("WebSearch", "WebFetch"):
        return "web", f"Web: {clip(inp.get('query') or inp.get('url', ''), 100)}"
    if name == "Skill":
        return "tool", f"Using skill {inp.get('skill', '')}"
    if name.startswith("mcp__"):
        return "tool", "Using " + name.split("__", 2)[-1].replace("_", " ")
    return "tool", name


def model_family(model):
    """claude-opus-5-5 -> "opus"; None for synthetic/unknown messages."""
    return next((f for f in ("opus", "sonnet", "haiku", "fable") if f in (model or "")), None)


# $ per million tokens: input, output, cache read, cache write (5 min), cache write (1 h).
# Anthropic list prices, Oct 2026. Subagents write the cache with a 5 minute TTL, leads with 1 hour.
PRICES = {
    "claude-fable-5-1": (10, 50, 0.25, 12.5, 20), "claude-fable-5": (10, 50, 1, 12.5, 20),
    "claude-opus-5-5": (4, 20, 0.20, 5, 8), "claude-opus-5": (5, 25, 0.50, 6.25, 10),
    "claude-sonnet-5-5": (2, 10, 0.20, 2.5, 4), "claude-sonnet-5": (2, 10, 0.20, 2.5, 4),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20),
    # older models still in use (pinned in Settings, or inside the Brain window)
    "claude-opus-4-8": (5, 25, 0.50, 6.25, 10), "claude-opus-4-7": (5, 25, 0.50, 6.25, 10),
    "claude-opus-4-6": (5, 25, 0.50, 6.25, 10), "claude-sonnet-4-6": (3, 15, 0.30, 3.75, 6),
    "claude-haiku-4-5": (1, 5, 0.10, 1.25, 2),
}
FAMILY_PRICES = {"fable": PRICES["claude-fable-5-1"], "opus": PRICES["claude-opus-5-5"],
                 "sonnet": PRICES["claude-sonnet-5-5"], "haiku": PRICES["claude-haiku-5-5"]}
# A prompt (input plus cache tokens) over the limit is billed at these rates instead.
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05, 0.625, 1))}


def usage_cost(model, u):
    """Dollar cost of one API message's usage block."""
    key = next((k for k in sorted(PRICES, key=len, reverse=True) if (model or "").startswith(k)), None)
    p = PRICES.get(key) or FAMILY_PRICES.get(model_family(model))
    if not p:
        return 0.0
    write = u.get("cache_creation_input_tokens") or 0
    long_prompt = LONG_PROMPT_PRICES.get(key)
    if long_prompt and (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + write > long_prompt[0]:
        p = long_prompt[1]
    split = u.get("cache_creation") or {}
    w1h = split.get("ephemeral_1h_input_tokens") or 0
    w5m = split.get("ephemeral_5m_input_tokens", write - w1h) or 0
    return ((u.get("input_tokens") or 0) * p[0] + (u.get("output_tokens") or 0) * p[1]
            + (u.get("cache_read_input_tokens") or 0) * p[2] + w5m * p[3] + w1h * p[4]) / 1e6


FILE_OPS = {"Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit"}
# file tool calls in the part of a long transcript we don't json-parse
# A PM task is named by its board (team/tasks/<id>) or its episode (team/episodes/<id>.md); both use the
# same <date>-<slug>. A lead "works on" a task when it assigns with BOARD:/TASK: or writes a board or
# episode file; writing the episode closes the task. Older PM templates named boards <yyyymmdd>-<slug>;
# those are accepted too and keyed by the canonical <yyyy-mm-dd>-<slug> (see task_id).
TASK_ID = r"(\d{4}-?\d\d-?\d\d-[A-Za-z0-9_-]+)"
BOARD_RE = re.compile(r"team[\\/]tasks[\\/]" + TASK_ID)
ASSIGN_RE = re.compile(r"^\s*(?:BOARD:\s*\S*team[\\/]tasks[\\/]|TASK:\s*)" + TASK_ID, re.M)
WRITE_RE = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?|\bmkdir\s+(?:-p\s+)?)\s*[\"']?\S*?\b(tasks|episodes)[\\/]" + TASK_ID)
SHELL_VAR_RE = re.compile(r"\b([A-Za-z_]\w*)=([\"']?)([^\s;&|\"']+)\2")


def task_id(name):
    """Canonical task id: 20261017-foo -> 2026-10-17-foo, so a legacy board and its episode share one key."""
    return re.sub(r"^(\d{4})(\d\d)(\d\d)-", r"\1-\2-\3-", name)


def _legacy_id(tid):
    return re.sub(r"^(\d{4})-(\d\d)-(\d\d)-", r"\1\2\3-", tid)


TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _inside(base, name, suffix=""):
    """base/name+suffix when name is a plain task name; else None.

    Task ids arrive in HTTP requests. A plain name (no separators, no leading dot) can't leave base; the normpath
    check states that without resolving symlinks, so a task folder that is a symlink still works.
    """
    if not TASK_NAME.fullmatch(name):
        return None
    root = os.path.normpath(base)
    path = os.path.normpath(os.path.join(root, name + suffix))
    return path if path.startswith(root + os.sep) else None


def _board_dir(tid):
    """Board folder of a task, under either date format; None if there is none."""
    tid = task_id(os.path.basename(tid))
    dirs = (_inside(BOARDS, n) for n in (tid, _legacy_id(tid)))
    return next((d for d in dirs if d and os.path.isdir(d)), None)


def _episode_path(tid):
    """Episode file of a task, under either date format; None for a name that isn't a task id."""
    tid = task_id(os.path.basename(tid))
    paths = [p for p in (_inside(EPISODES, n, ".md") for n in (tid, _legacy_id(tid))) if p]
    return next((p for p in paths if os.path.exists(p)), paths[0] if paths else None)


def expand_vars(cmd):
    """Substitute simple VAR=value assignments (T=~/.claude/team; cat > $T/...) so written paths are visible."""
    for name, _, value in SHELL_VAR_RE.findall(cmd):
        cmd = re.sub(r"\$\{?" + name + r"\b\}?", lambda _m: value, cmd)
    return cmd
PATH_RE = re.compile(r"team[\\/](tasks|episodes)[\\/]" + TASK_ID)
FILE_OP_RE = re.compile(rb'"name":"(Read|Edit|Write|NotebookEdit)","input":\{"(?:file_path|notebook_path)":"([^"]+)"')


def report_status(text):
    m = re.search(r"^\s*STATUS:\s*([a-z-]+)", text or "", re.M | re.I)
    return m.group(1).lower() if m else None


class Transcript:
    """One session (kind=main) or subagent (kind=sub) transcript, parsed incrementally."""

    def __init__(self, path, kind, project, meta=None):
        self.path, self.kind, self.project = path, kind, project
        self.meta = meta or {}
        self.session_id = os.path.basename(path)[:-6]
        self.agent_setting = None  # e.g. "pm" when started with `claude --agent pm`
        self.title = self.meta.get("description", "")
        self.prompt = ""
        self.report = ""
        self.offset, self.size, self.mtime = 0, -1, 0
        self.events = deque(maxlen=80)  # dicts: t, kind, text
        self.last = None
        self.pending = {}  # tool_use_id -> (kind, text) not yet answered
        self.calls = {}  # Agent tool_use_id -> {agent, description, prompt, t, model, modelReason}
        self.parent = None
        # token usage: one entry per API message (a message is logged once per content block)
        self.usage = {}  # message id -> (family, input, cache_write, cache_read, output)
        self.totals = Counter()
        self.model = None  # family of the latest message
        self.context = 0  # prompt size of the latest message
        self.files = Counter()  # (op, path) -> count, op is "read" or "edit"
        self.trail = deque(maxlen=200)  # structured tool steps for the Brain's live view
        # task attribution (see TASK_ID): a subagent belongs to the task in its prompt, or else to the
        # task its lead was on when it assigned it; a lead's turns go to the task it is on, and turns
        # between tasks (a new request being clarified) go to the next task it names
        self.task = None
        self.task_of = {}  # message id -> task id (None until known)
        self.call_msg = {}  # Agent tool_use id -> message id that made the call
        self.closed = set()  # tasks whose episode is written; later writes to them are follow-ups
        self.open_title = None  # first request since the last task closed (names work not yet on a board)

    # -- parsing ---------------------------------------------------------------
    def refresh(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return
        self.mtime = st.st_mtime
        if st.st_size == self.size:
            return
        with open(self.path, "rb") as f:
            if self.offset == 0 and st.st_size > TAIL_BYTES:
                data = f.read()
                # small records that may sit early in a long file
                for marker in (b'"type":"agent-setting"', b'"type":"ai-title"'):
                    i = data.rfind(marker)
                    if i != -1:
                        self._parse(data[data.rfind(b"\n", 0, i) + 1 : data.find(b"\n", i)])
                if self.kind == "sub":
                    self._parse(data[: data.find(b"\n")], first_only=True)
                chunk = data[-TAIL_BYTES:]
                chunk = chunk[chunk.find(b"\n") + 1 :]
                # the head isn't replayed into events, but its tokens and file touches still count
                head = data[: len(data) - len(chunk)]
                for line in head.splitlines():
                    if b'"usage"' in line and b'"type":"assistant"' in line:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        self._take_assistant(d)
                for name, path in FILE_OP_RE.findall(head):
                    self.files[(FILE_OPS[name.decode()], path.decode("utf-8", "replace"))] += 1
            else:
                f.seek(self.offset)
                chunk = f.read()
        end = chunk.rfind(b"\n")
        if end == -1:
            return
        for line in chunk[: end + 1].splitlines():
            self._parse(line)
        self.offset = st.st_size - (len(chunk) - end - 1)
        self.size = st.st_size

    def _take_usage(self, d):
        msg = d.get("message") or {}
        u, fam = msg.get("usage"), model_family(msg.get("model"))
        if not u or not fam:
            return
        row = (fam, u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
               u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0, usage_cost(msg.get("model"), u))
        key = msg.get("id") or d.get("uuid")
        old = self.usage.get(key)
        if old == row:
            return
        for sign, r in ((-1, old), (1, row)):
            if r:
                self.totals[r[0]] += sign * sum(r[1:5])
                self.totals["cost"] += sign * r[5]
                for k, v in zip(("input", "cacheWrite", "cacheRead", "output"), r[1:5]):
                    self.totals[k] += sign * v
        if not old:
            self.totals["turns"] += 1
        self.usage[key] = row
        self.model = fam
        self.context = row[1] + row[2] + row[3]

    def _switch_task(self, tid):
        for k, v in self.task_of.items():
            if v is None:
                self.task_of[k] = tid
        self.task = tid
        self.open_title = None

    def _take_assistant(self, d):
        """Token usage of one assistant line, plus which task it belongs to (leads only)."""
        self._take_usage(d)
        if self.kind != "main":
            return
        msg = d.get("message") or {}
        key = msg.get("id") or d.get("uuid")
        close = None
        for b in msg.get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b.get("name", ""), b.get("input") or {}
            if name in ("Agent", "Task"):
                self.call_msg[b.get("id")] = key
                m = ASSIGN_RE.search(inp.get("prompt", ""))
                if m:
                    self._switch_task(task_id(m.group(1)))
            elif name in FILE_OPS:
                m = PATH_RE.search(inp.get("file_path") or "")
                if m:
                    self._switch_task(task_id(m.group(2)))
                    close = task_id(m.group(2)) if m.group(1) == "episodes" else close
            elif name == "Bash":
                for kind, tid in WRITE_RE.findall(expand_vars(inp.get("command", ""))):
                    tid = task_id(tid)
                    self._switch_task(tid)
                    close = tid if kind == "episodes" else close
        if key in self.usage and self.task_of.get(key) is None:
            self.task_of[key] = self.task
        if close:
            self.closed.add(close)
        if self.task in self.closed:
            self.task = None

    def tokens(self, keys=None):
        if keys is not None:
            return tokens_of([self.usage[k] for k in keys])
        t = self.totals
        return {
            "total": t["input"] + t["cacheWrite"] + t["cacheRead"] + t["output"],
            "input": t["input"], "cacheWrite": t["cacheWrite"], "cacheRead": t["cacheRead"], "output": t["output"],
            "turns": t["turns"], "context": self.context, "model": self.model, "cost": round(t["cost"], 4),
            "byModel": {f: t[f] for f in ("opus", "sonnet", "haiku", "fable") if t[f]},
        }

    def _parse(self, line, first_only=False):
        try:
            d = json.loads(line)
        except ValueError:
            return
        t = d.get("type")
        if t == "agent-setting":
            self.agent_setting = d.get("agentSetting")
            return
        if t == "ai-title" and self.kind == "main":
            self.title = d.get("aiTitle") or self.title
            return
        if t not in ("user", "assistant"):
            return
        if t == "assistant":
            self._take_assistant(d)
        ts = local_time(d.get("timestamp")) or time.time()
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content = content or []
        meaningful = False
        for b in content:
            bt = b.get("type")
            if t == "user" and bt == "text":
                text = b.get("text", "")
                if d.get("isMeta") or not text.strip() or text.lstrip().startswith("<"):
                    continue
                if self.kind == "sub" and not self.prompt:
                    self.prompt = text
                    found = ASSIGN_RE.search(text) or BOARD_RE.search(text)
                    if found:
                        self.task = task_id(found.group(1))
                    meaningful = True
                    if first_only:
                        return
                    continue
                if self.kind == "main" and self.task is None and self.open_title is None \
                        and not text.startswith("Another Claude session sent a message"):
                    self.open_title = clip(text, 120)
                who = "assignment" if self.kind == "sub" else "you"
                self.events.append({"t": ts, "kind": who, "text": clip(text, 400)})
                if not self.title:
                    self.title = clip(text, 80)
                meaningful = True
            elif t == "user" and bt == "tool_result":
                self.pending.pop(b.get("tool_use_id"), None)
                meaningful = True
            elif t == "assistant" and bt == "tool_use":
                name, inp = b.get("name", ""), b.get("input") or {}
                kind, text = describe_tool(name, inp)
                self.events.append({"t": ts, "kind": kind, "text": text})
                if name in ("Read", "Edit", "Write", "NotebookEdit", "Grep", "Glob", "Bash"):
                    self.trail.append({"t": ts, "tool": name, "kind": kind, "text": text,
                                       "path": inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "",
                                       "offset": inp.get("offset"), "limit": inp.get("limit"),
                                       "cmd": inp.get("command", "")[:400] if name == "Bash" else "",
                                       "pattern": inp.get("pattern", "")[:120]})
                self.pending[b.get("id")] = (kind, text)
                if name in FILE_OPS and (inp.get("file_path") or inp.get("notebook_path")):
                    self.files[(FILE_OPS[name], inp.get("file_path") or inp["notebook_path"])] += 1
                if name in ("Agent", "Task"):
                    reason = re.search(r"^\s*MODEL:\s*(.+)$", inp.get("prompt", ""), re.M)
                    self.calls[b.get("id")] = {
                        "agent": inp.get("subagent_type") or "general-purpose",
                        "description": inp.get("description", ""),
                        "prompt": inp.get("prompt", ""),
                        "t": ts,
                        "model": inp.get("model"),
                        "modelReason": clip(reason.group(1), 160) if reason else "",
                    }
                if name == "SubagentHandback":
                    self.report = inp.get("message", "")
                meaningful = True
            elif t == "assistant" and bt == "text" and b.get("text", "").strip():
                self.events.append({"t": ts, "kind": "say", "text": clip(b["text"], 600)})
                if self.kind == "sub":
                    self.report = b["text"]
                meaningful = True
        if meaningful:
            self.last = d

    # -- derived ---------------------------------------------------------------
    @property
    def agent_type(self):
        return self.meta.get("agentType") if self.kind == "sub" else (self.agent_setting or "lead")

    def state(self, now):
        """(state, current activity text)."""
        age = now - self.mtime
        if self.last is None:
            return OFF, ""
        current = self.events[-1]["text"] if self.events else ""
        if self.kind == "sub" and self.report and self.events and self.events[-1]["kind"] in ("handback", "say"):
            return DONE, "Report delivered"
        if self.pending:
            kind, text = list(self.pending.values())[-1]
            if kind == "ask":
                return WAITING, text
            if kind == "delegate" and self.kind == "main":
                return (WORKING if age < 600 else IDLE), text
            if self.kind == "main":
                if age > 1800:
                    return IDLE, "Stopped mid-task · " + text
                if age > 120:
                    return WAITING, "Waiting for your approval? · " + text
                return WORKING, text
            # subagents write to their log constantly; silence means the run was stopped or crashed
            if age > 180:
                return IDLE, f"Stopped {int(age // 60)}m ago (no activity) · " + text
            return WORKING, text
        if self.last.get("type") == "assistant":
            return (DONE if age < 4 * 3600 else IDLE), current
        if self.kind == "sub" and age < 600:
            # a tool just returned; the agent is mid-task
            last_tool = next((e["text"] for e in reversed(self.events) if e["kind"] not in ("say", "assignment")), "")
            return WORKING, last_tool or "Working…"
        return (THINKING if age < 600 else IDLE), "Thinking…"


def project_name(dirname):
    dirname = dirname.lower()
    name = dirname[len(HOME_PREFIX):] if dirname.startswith(HOME_PREFIX) else dirname.lstrip("-")
    prefixes = ("projects-learnings-", "projects-")
    for prefix in ((WS_PARENT,) if WS_PARENT else ()) + prefixes:
        if name.startswith(prefix) and name != prefix.rstrip("-"):
            return name[len(prefix):]
    return name


def workspace_project(dirname):
    """True when a ~/.claude/projects folder holds this workspace's own sessions.

    Claude Code names the folder after the workspace path, and folders inside
    the workspace get ``<workspace slug>-<path inside it>``. Matching on the
    bare prefix therefore lets siblings through: with workspace
    ``C:\\work\\app``, the folder for ``C:\\work\\apples`` also starts with
    ``c--work-app``, so its sessions, tokens and cost landed on the floor too.
    Requiring an exact match or a ``-`` boundary keeps them out.

    ``app`` and ``app-old`` still share a prefix; telling those apart needs the
    ``cwd`` recorded in the transcript, which this filter doesn't read.
    """
    folder = str(dirname or "").lower()
    if not WS_SLUG:
        return True
    return folder == WS_SLUG or folder.startswith(WS_SLUG + "-")


def knowledge_info(filename):
    if not filename:
        return None
    path = os.path.join(KNOWLEDGE, filename)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {"file": path, "exists": False}
    notes = text.split("## Field notes", 1)
    return {
        "file": path,
        "exists": True,
        "lines": text.count("\n") + 1,
        "updated": os.path.getmtime(path),
        "fieldNotes": notes[1].count("\n- ") if len(notes) > 1 else 0,
        "header": clip(next((l for l in text.splitlines() if l.startswith(">")), "").lstrip("> "), 160),
    }


def _read(path, limit=20000):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def boards(window):
    """Task boards (shared memory per request): open ones, plus closed ones touched within the window."""
    now, out = time.time(), []
    for d in glob(os.path.join(BOARDS, "*", "")):
        bid = task_id(os.path.basename(d.rstrip("/\\")))
        brief = _read(os.path.join(d, "brief.md")) or ""
        files = [os.path.join(d, f) for f in ("brief.md", "contract.md", "log.md")] + glob(os.path.join(d, "handoff", "*.md"))
        mtime = max([os.path.getmtime(f) for f in files if os.path.exists(f)] or [os.path.getmtime(d)])
        status = (re.search(r"^STATUS:\s*(\S+)", brief, re.M) or [None, "open"])[1].lower()
        if status != "open" and now - mtime > window:
            continue
        log = [l for l in (_read(os.path.join(d, "log.md")) or "").splitlines() if l.strip() and not l.startswith("#")]
        out.append({
            "id": bid, "path": d, "status": status, "mtime": mtime,
            "title": (re.search(r"^TITLE:\s*(.+)$", brief, re.M) or [None, bid])[1].strip(),
            "hasContract": os.path.exists(os.path.join(d, "contract.md")),
            "handoffs": sorted(os.path.basename(h)[:-3] for h in glob(os.path.join(d, "handoff", "*.md"))),
            "log": log[-4:],
        })
    out.sort(key=lambda b: (b["status"] != "open", -b["mtime"]))
    return out


def board_detail(bid):
    d = _board_dir(bid)
    if not d:
        return None
    return {
        "id": task_id(os.path.basename(bid)), "path": d,
        "brief": _read(os.path.join(d, "brief.md")), "contract": _read(os.path.join(d, "contract.md")),
        "log": _read(os.path.join(d, "log.md")),
        "handoffs": {os.path.basename(h)[:-3]: _read(h) for h in sorted(glob(os.path.join(d, "handoff", "*.md")))},
    }


def tokens_of(rows):
    """Token totals (same shape as Transcript.tokens) for a subset of usage rows."""
    out = Counter()
    unknown_cost = False
    models = set()
    model_totals = Counter()
    for fam, *vals, cost in rows:
        models.add(fam)
        for k, v in zip(("input", "cacheWrite", "cacheRead", "output"), vals):
            out[k] += v
        model_totals[fam] += sum(vals)
        unknown_cost = unknown_cost or cost is None
        out["cost"] += cost or 0
    return {
        "total": out["input"] + out["cacheWrite"] + out["cacheRead"] + out["output"],
        "input": out["input"], "cacheWrite": out["cacheWrite"], "cacheRead": out["cacheRead"], "output": out["output"],
        "turns": len(rows), "context": 0, "model": rows[-1][0] if rows else None,
        "cost": None if unknown_cost else round(out["cost"], 4),
        "byModel": {f: model_totals[f] for f in sorted(models) if model_totals[f]},
    }




def _field(text, name):
    m = re.search(r"^" + name + r":\s*(.+)$", text or "", re.M)
    return m.group(1).strip() if m else None


def task_meta(tid):
    """Title and status of a PM task from its board brief and/or episode."""
    tid = task_id(tid)
    board = _board_dir(tid)
    brief = _read(os.path.join(board, "brief.md")) if board else None
    episode = _read(_episode_path(tid))
    status = (_field(brief, "STATUS") or ("closed" if episode else "open")).lower()
    return {
        "id": tid, "title": _field(brief, "TITLE") or _field(episode, "TASK") or tid, "status": status,
        "hasBoard": brief is not None, "hasEpisode": episode is not None, "outcome": _field(episode, "OUTCOME"),
    }


def current_meta(office, tid, now):
    """A PM session's work that has no board or episode (yet): open while the session is active."""
    lead = office.transcripts.get(tid[8:]) or (office.history.transcripts.get(tid[8:]) if office.history else None)
    active = lead is not None and (now - lead.mtime < 3600 or lead.state(now)[0] in (WORKING, THINKING, WAITING))
    return {"id": tid, "title": (lead.open_title or lead.title) if lead else "PM session", "status": "open" if active else "untracked",
            "hasBoard": False, "hasEpisode": False, "outcome": None if active else "No board or episode written"}


def task_detail(office, tid):
    if tid.startswith("current:"):
        meta = current_meta(office, tid, time.time())
    else:
        tid = task_id(os.path.basename(tid))
        if not TASK_NAME.fullmatch(tid):
            return None
        meta = task_meta(tid)
    return {**meta, "board": board_detail(tid) if meta["hasBoard"] else None,
            "episode": _read(_episode_path(tid)) if meta["hasEpisode"] else None,
            "usage": office.task_usage.get(tid)}


# ---- routes: every step each agent took on a task, read from the whole transcript on demand ----
_route_cache = {}  # transcript path -> (size, steps)
MAX_ROUTE_STEPS = 600


def place(path):
    """(area, label) for a path: the repo under the code root, the team folder, or home."""
    p = norm(path)
    if re.match(r"^/tmp/claude-\d+/", p):
        return "tmp", p
    rel = relative(p, TEAM)
    if rel is not None:
        return "team", rel
    rel = relative(p, CODE_ROOT)
    if rel is not None:
        return rel.split("/", 1)[0], rel
    rel = relative(p, HOME)
    return ("home", "~/" + rel) if rel is not None else ("other", p)


def _shell_segments(cmd):
    """Token lists of the simple commands in cmd, split on && || ; | and newlines outside quotes."""
    import shlex
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    seg = []
    try:
        for t in lex:
            if t and set(t) <= set(";&|"):
                if seg:
                    yield seg
                seg = []
            else:
                seg.append(t)
    except ValueError:  # unbalanced quotes: give up on the rest
        pass
    if seg:
        yield seg


READ_CMDS = {"cat", "head", "tail", "nl", "less", "more", "wc", "awk", "bat", "jq"}
SEARCH_CMDS = {"grep", "egrep", "rg", "ag", "find", "ls", "tree", "fd"}
FILE_EXT_RE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|md|json|sql|ya?ml|toml|ini|cfg|txt|csv|html|css|scss|sh|env|xml|lock)$")


def bash_targets(cmd):
    """Files a shell command reads, searches or writes: [(kind, absolute path, lines)].

    Follows `cd`, simple VAR=value assignments, and the usual read (cat/sed -n/head…),
    search (grep/rg/find…) and write (sed -i, tee, > file) forms.
    """
    cmd = expand_vars(cmd)
    # heredoc bodies are data, not commands
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?(.*?)\n.*?\n\s*\1\s*(?=\n|$)", r"\2", cmd, flags=re.S)
    cwd, out = None, []
    for tok in _shell_segments(cmd):
        while tok and re.match(r"^[A-Za-z_]\w*=", tok[0]):
            tok = tok[1:]
        if not tok:
            continue
        verb, args = os.path.basename(tok[0]), tok[1:]
        resolve = lambda a: os.path.normpath(os.path.join(cwd or "", os.path.expanduser(a))) if not os.path.isabs(os.path.expanduser(a)) and cwd else os.path.expanduser(a)
        if verb == "cd":
            if args:
                cwd = resolve(args[0])
            continue
        for i, a in enumerate(args):
            if a in (">", ">>") and i + 1 < len(args) and not any(c in args[i + 1] for c in "*?{}$`"):
                out.append(("edit", os.path.normpath(resolve(args[i + 1])), None))
        args = [a for i, a in enumerate(args) if a not in (">", ">>", "2>&1") and not (i and args[i - 1] in (">", ">>"))]
        lines = None
        if verb == "sed":
            kind = "edit" if any(a.startswith("-i") for a in args) else "read"
            pos = [a for a in args if not a.startswith("-")]
            m = re.match(r"^(\d+),(\d+)p", pos[0]) if pos else None
            lines = f"{m.group(1)}–{m.group(2)}" if m else None
            pos = pos[1:]
        elif verb == "tee":
            kind, pos = "edit", [a for a in args if not a.startswith("-")]
        elif verb in READ_CMDS:
            kind, pos = "read", [a for a in args if not a.startswith("-") and not re.fullmatch(r"\d+", a)]
            if verb in ("awk", "jq") and pos:
                pos = pos[1:]
        elif verb in SEARCH_CMDS:
            kind = "search"
            pos, skip = [], False
            for a in args:
                if skip:
                    skip = False
                    continue
                if a in ("-e", "-f", "--include", "--exclude", "-g", "--glob", "-name", "-iname", "-type", "-path", "-A", "-B", "-C", "-m", "--max-count", "-maxdepth"):
                    skip = True
                    continue
                if not a.startswith("-"):
                    pos.append(a)
            if verb in ("grep", "egrep", "rg", "ag") and not any(a in ("-e", "-f") for a in args):
                pos = pos[1:]  # the first positional is the pattern
        else:
            continue
        for a in pos:
            if any(c in a for c in "*?{}$`") or a.startswith("http"):
                continue
            path = os.path.normpath(resolve(a))
            if FILE_EXT_RE.search(path) or os.path.exists(path):
                out.append((kind, path, lines))
    return out


def _route_step(name, inp, ts, key):
    kind, text = describe_tool(name, inp)
    step = {"t": ts, "key": key, "tool": name, "kind": kind, "text": text}
    target = inp.get("file_path") or inp.get("notebook_path")
    if name in ("Grep", "Glob"):
        step["pattern"] = clip(inp.get("pattern", ""), 120)
        target = inp.get("path") or ""
    elif name == "Bash":
        cmd = inp.get("command", "")
        step["cmd"] = clip(cmd, 300)
        first = cmd.strip().split()[0] if cmd.strip() else ""
        if "teammap" in cmd.split("|")[0]:
            step["kind"] = "map"
        elif re.search(r"\bpsql\b", cmd):
            step["kind"] = "db"
        elif first == "git" or cmd.lstrip().startswith("git "):
            step["kind"] = "git"
        targets = [] if step["kind"] in ("map", "db", "git") else bash_targets(cmd)
        if targets:
            step["targets"] = [{"kind": k, **dict(zip(("area", "path"), place(p))), "lines": ln, "abs": norm(p)} for k, p, ln in targets[:12]]
            if step["kind"] == "shell":
                kinds = {t["kind"] for t in step["targets"]}
                step["kind"] = "edit" if "edit" in kinds else "read" if "read" in kinds else "search"
    elif name in ("Agent", "Task"):
        step["agent"] = inp.get("subagent_type") or "general-purpose"
    if target:
        step["area"], step["path"] = place(target)
        step["abs"] = norm(target)
        if inp.get("offset") or inp.get("limit"):
            start = int(inp.get("offset") or 1)
            step["lines"] = f"{start}–{start + int(inp.get('limit') or 0) - 1}" if inp.get("limit") else f"{start}–"
    return step


def transcript_steps(path):
    """All tool steps in a transcript, in order (cached until the file grows)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    hit = _route_cache.get(path)
    if hit and hit[0] == size:
        return hit[1]
    steps, failed = [], set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"tool_use"' not in line and '"is_error":true' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and d.get("type") == "assistant":
                    st = _route_step(b.get("name", ""), b.get("input") or {}, local_time(d.get("timestamp")),
                                     msg.get("id") or d.get("uuid"))
                    st["id"] = b.get("id")
                    steps.append(st)
                elif b.get("type") == "tool_result" and b.get("is_error"):
                    failed.add(b.get("tool_use_id"))
    for st in steps:
        if st.pop("id", None) in failed:
            st["failed"] = True
    _route_cache[path] = (size, steps)
    return steps


def _public_step(s):
    out = {k: v for k, v in s.items() if k not in ("key", "abs")}
    if s.get("targets"):
        out["targets"] = [{k: v for k, v in t.items() if k != "abs"} for t in s["targets"]]
    return out


def task_runs_steps(office, tid):
    """[(run, steps)] for every agent run on a task, the lead's steps limited to its turns on it."""
    usage = office.task_usage.get(tid)
    if not usage:
        return []
    every = {**(office.history.transcripts if office.history else {}), **office.transcripts}
    out = []
    for r in sorted(usage["runs"], key=lambda r: r["t"]):
        session = every.get(r["key"])
        steps = list(session.steps) if hasattr(session, "steps") else transcript_steps(r["key"])
        if r["lead"]:
            lead = every.get(r["key"])
            mine = {k for k, v in lead.task_of.items() if (v or "current:" + lead.path) == tid} if lead else set()
            steps = [s for s in steps if (s.get("taskId") or "current:" + lead.path) == tid] if hasattr(lead, "steps") else [s for s in steps if s["key"] in mine]
        if steps:
            out.append((r, steps))
    return out


def task_routes(office, tid):
    """Each agent run on a task with the steps it took, plus which files each agent touched."""
    if not office.task_usage.get(tid):
        return None
    runs, files = [], {}
    for r, steps in task_runs_steps(office, tid):
        for s in steps:
            if s.get("failed"):
                continue
            if s.get("targets"):
                s["targets"] = [tg for tg in s["targets"] if tg["area"] != "tmp"]
            touched = s.get("targets") or ([{"kind": s["kind"], "area": s["area"], "path": s["path"]}]
                                           if s.get("path") and s["kind"] in ("read", "edit", "search") else [])
            for tg in touched:
                f = files.setdefault(tg["path"], {"path": tg["path"], "area": tg["area"], "agents": {}, "visits": 0})
                a = f["agents"].setdefault(r["agent"], {"read": 0, "edit": 0, "search": 0})
                a[tg["kind"]] += 1
                f["visits"] += 1
        counts = Counter(s["kind"] for s in steps)
        runs.append({"key": r["key"], "agent": r["agent"], "lead": r["lead"], "description": r["description"],
                     "model": r["tokens"].get("model"), "start": steps[0]["t"], "end": steps[-1]["t"],
                     "counts": dict(counts),
                     "files": len({p for s in steps for p in ([t["path"] for t in s.get("targets", [])] or [s.get("path")]) if p}),
                     "steps": [_public_step(s) for s in steps[-MAX_ROUTE_STEPS:]],
                     "truncated": max(0, len(steps) - MAX_ROUTE_STEPS)})
    files = sorted(files.values(), key=lambda f: (all(not a["read"] and not a["edit"] for a in f["agents"].values()),
                                                  -any(a["edit"] for a in f["agents"].values()),
                                                  -len(f["agents"]), -f["visits"], f["path"]))
    return {"id": tid, "runs": runs, "files": files}


def add_tokens(items):
    out = Counter(dict.fromkeys(("total", "input", "cacheWrite", "cacheRead", "output", "turns", "cost"), 0))
    unknown_cost = False
    bases = set()
    for x in items:
        if x.get("costBasis"):
            bases.add(x["costBasis"])
        unknown_cost = unknown_cost or (x.get("cost") is None and x.get("total", 0) > 0)
        for k in ("total", "input", "cacheWrite", "cacheRead", "output", "turns", "cost"):
            out[k] += x.get(k) or 0
        for f, n in x["byModel"].items():
            out["model:" + f] += n
    by_model = {k[6:]: v for k, v in out.items() if k.startswith("model:")}
    out["cost"] = None if unknown_cost else round(out["cost"], 4)
    result = {**{k: v for k, v in out.items() if not k.startswith("model:")}, "byModel": by_model}
    if bases:
        result["costBasis"] = next(iter(bases)) if len(bases) == 1 else "mixed-reference"
    return result


class Office:
    def __init__(self, roster, hours=12):
        self.roster = roster
        self.window = hours * 3600
        self.transcripts = {}
        self.history = None  # a longer-window Office, so a board's tokens include work older than the window
        self._history_scan = 0
        self.task_usage = {}

    def _get(self, path, kind, project, meta=None):
        t = self.transcripts.get(path)
        if t is None:
            t = self.transcripts[path] = Transcript(path, kind, project, meta)
        return t

    def scan(self):
        from .providers.runtime import runtime
        if getattr(self, "_runtime_provider", None) != self.roster.get("provider", "claude"):
            self.transcripts.clear()
            self.task_usage.clear()
            self._history_scan = 0
            if self.history:
                self.history.roster = self.roster
                self.history.transcripts.clear()
                self.history.task_usage.clear()
            self._runtime_provider = self.roster.get("provider", "claude")
        return runtime(self.roster).scan(self)

    def _scan_claude(self):
        now = time.time()
        live = []
        for path in glob(os.path.join(PROJECTS, "*", "*.jsonl")):
            try:
                mtime = os.path.getmtime(path)
                subs = [(s, os.path.getmtime(s)) for s in glob(os.path.join(path[:-6], "subagents", "*.jsonl"))]
            except OSError:
                continue
            if now - max([mtime] + [m for _, m in subs]) > self.window:
                continue
            if not workspace_project(os.path.basename(os.path.dirname(path))):
                continue
            project = project_name(os.path.basename(os.path.dirname(path)))
            lead = self._get(path, "main", project)
            live.append(lead)
            for s, sm in subs:
                if now - sm > self.window:
                    continue
                if s not in self.transcripts:
                    try:
                        with open(s[:-6] + ".meta.json", encoding="utf-8") as f:
                            meta = json.load(f)
                    except (OSError, ValueError):
                        meta = {}
                    self._get(s, "sub", project, meta).parent = lead
                live.append(self.transcripts[s])
        for t in live:
            t.refresh()
        return live

    def tasks_usage(self, now):
        """Tokens per PM task, from every agent that worked on it: {task id: {tokens, agents, runs}}."""
        src = self.history or self
        if src is not self and now - self._history_scan > 10:
            src.scan()
            self._history_scan = now
        everything = {**src.transcripts, **self.transcripts}
        runs = {}
        for t in everything.values():
            if t.kind == "sub" and t.totals["turns"]:
                tid = t.task
                lead = t.parent
                if not tid and lead and lead.agent_setting == "pm":
                    tid = lead.task_of.get(lead.call_msg.get(t.meta.get("toolUseId"))) or "current:" + lead.path
                if tid:
                    runs.setdefault(tid, []).append({
                        "key": t.path, "agent": t.agent_type, "lead": False,
                        "description": t.meta.get("description") or t.title, "t": t.mtime,
                        "state": t.state(now)[0], "reportStatus": report_status(t.report), "tokens": t.tokens(),
                    })
            elif t.kind == "main" and t.agent_setting == "pm":
                keys = {}
                for k in t.usage:
                    keys.setdefault(t.task_of.get(k) or "current:" + t.path, []).append(k)
                for tid, ks in keys.items():
                    runs.setdefault(tid, []).append({
                        "key": t.path, "agent": "pm", "lead": True, "description": t.title, "t": t.mtime,
                        "state": t.state(now)[0], "reportStatus": None, "tokens": t.tokens(ks),
                    })
        out = {}
        for tid, rs in runs.items():
            rs.sort(key=lambda r: -r["tokens"]["total"])
            agents = {}
            for r in rs:
                agents.setdefault(r["agent"], []).append(r["tokens"])
            out[tid] = {
                "tokens": add_tokens(r["tokens"] for r in rs),
                "agents": sorted(({"agent": a, "runs": len(ts), "tokens": add_tokens(ts)} for a, ts in agents.items()),
                                 key=lambda x: -x["tokens"]["total"]),
                "runs": rs, "t": max(r["t"] for r in rs),
            }
        self.task_usage = out
        return out

    def state(self):
        now = time.time()
        live = self.scan()
        roster_ids = {m["id"] for m in self.roster["members"]}
        pm_name = next((m["name"] for m in self.roster["members"] if m["id"] == "pm"), "PM")
        lead_name = lambda t: (f"{pm_name} (PM)" if t.agent_setting == "pm" else ("Codex" if getattr(t, "provider", "claude") == "codex" else "Claude")) + f" · {t.project}"

        names = {"Explore": "Scout", "general-purpose": "Gen", "Plan": "Archie",
                 **{m["id"]: m["name"] for m in self.roster["members"]}}

        def summary(t, full=False):
            st, cur = t.state(now)
            if t.kind == "main" and st in (DONE, THINKING, IDLE):
                # a lead that delegated is still busy while its agents work
                busy = [c for c in live if c.kind == "sub" and c.parent is t
                        and c.state(now)[0] in (WORKING, THINKING)]
                if busy:
                    who = sorted({names.get(c.agent_type, c.agent_type) for c in busy})
                    st, cur = WORKING, "Waiting for " + " & ".join(who) + " to report back"
            out = {
                "key": t.path, "kind": t.kind, "agentType": t.agent_type, "project": t.project,
                "title": t.title or "(untitled)", "state": st, "current": cur, "mtime": t.mtime,
                "assignedBy": lead_name(t.parent) if t.parent else None,
                "reportStatus": report_status(t.report),
                "tokens": t.tokens(),
            }
            if full:
                out["events"] = list(t.events)[-60:]
                out["prompt"] = t.prompt[:6000]
                out["report"] = t.report[:12000]
            return out

        # desks for roster members: every instance in the window, best/most recent first
        members = []
        for m in self.roster["members"]:
            if m["id"] == "pm":
                inst = [t for t in live if t.kind == "main" and t.agent_setting == "pm"]
            else:
                inst = [t for t in live if t.kind == "sub" and t.agent_type == m["id"]]
            inst.sort(key=lambda t: (STATE_RANK[t.state(now)[0]] >= STATE_RANK[THINKING], t.mtime), reverse=True)
            members.append({
                **m,
                "tokens": add_tokens(t.tokens() for t in inst),
                "knowledge": knowledge_info(m.get("knowledge")),
                "instances": [summary(t, full=(i == 0)) for i, t in enumerate(inst[:6])],
                "active": sum(1 for t in inst if t.state(now)[0] in (WORKING, THINKING, WAITING)),
            })

        contractors = [summary(t, full=True) for t in live
                       if t.kind == "sub" and t.agent_type not in roster_ids]
        contractors.sort(key=lambda c: -c["mtime"])
        sessions = [summary(t, full=True) for t in live if t.kind == "main" and t.agent_setting != "pm"]
        sessions.sort(key=lambda s: -s["mtime"])

        # task board: every Agent call made by a lead, matched to its subagent transcript
        by_tool_use = {t.meta.get("toolUseId"): t for t in live if t.kind == "sub"}
        tasks = []
        for lead in (t for t in live if t.kind == "main"):
            for tid, call in lead.calls.items():
                sub = by_tool_use.get(tid)
                st = sub.state(now)[0] if sub else (WORKING if now - call["t"] < 60 else IDLE)
                rs = report_status(sub.report) if sub else None
                by_pm = lead.agent_setting == "pm"
                column = ("clarify" if rs in ("needs-clarification", "blocked")
                          else "review" if rs in ("fail", "issues-found")
                          else "done" if st in (DONE, IDLE) and (not sub or sub.report)
                          else "progress")
                tasks.append({
                    "id": tid, "agent": call["agent"], "description": call["description"],
                    "prompt": call["prompt"][:3000], "assignedBy": lead_name(lead), "byPm": by_pm, "t": call["t"],
                    "state": st, "column": column, "reportStatus": rs,
                    "model": (sub.model if sub and sub.model else call.get("model")),
                    "modelRequested": call.get("model"), "modelReason": call.get("modelReason", ""),
                    "tokens": sub.tokens() if sub else None,
                    "report": (sub.report[:4000] if sub else ""),
                    "subKey": sub.path if sub else None, "current": sub.state(now)[1] if sub else "",
                })
        tasks.sort(key=lambda x: -x["t"])

        # live feed across everyone
        feed = []
        for t in live:
            who = t.agent_type if t.kind == "sub" else ("pm" if t.agent_setting == "pm" else "lead")
            for e in list(t.events)[-25:]:
                feed.append({**e, "who": who, "project": t.project, "key": t.path})
        feed.sort(key=lambda e: -e["t"])

        team = [t for t in live if (t.kind == "sub" and t.parent and t.parent.agent_setting == "pm")
                or (t.kind == "main" and t.agent_setting == "pm")]
        self._summary = summary
        usage = self.tasks_usage(now)
        board_list = boards(self.window)[:20]
        pm_tasks = []
        for tid, u in usage.items():
            meta = task_meta(tid) if not tid.startswith("current:") else None
            if now - u["t"] > self.window and not (meta and meta["hasBoard"] and meta["status"] == "open"):
                continue
            if meta is None:
                meta = current_meta(self, tid, now)
            pm_tasks.append({**meta, "t": u["t"], "tokens": u["tokens"],
                             "agents": [{"agent": a["agent"], "runs": a["runs"], "total": a["tokens"]["total"]}
                                        for a in u["agents"]]})
        pm_tasks.sort(key=lambda x: (x["status"] != "open", -x["t"]))
        return {
            "now": now, "windowHours": self.window / 3600,
            "tokens": {"all": add_tokens(t.tokens() for t in live), "team": add_tokens(t.tokens() for t in team)},
            "members": members, "contractors": contractors[:12], "sessions": sessions[:10],
            "tasks": tasks[:60], "feed": feed[:120], "boards": board_list, "pmTasks": pm_tasks[:30],
            "helpers": self.roster.get("helpers"),
        }

    def detail(self, key):
        """Full view of one transcript (activity, assignment, report) for the drawer."""
        t = self.transcripts.get(key) or (self.history.transcripts.get(key) if self.history else None)
        if t is None or not hasattr(self, "_summary"):
            return None
        t.refresh()
        return self._summary(t, full=True)
