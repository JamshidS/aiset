"""Incremental Codex rollout adapter for native CLI sessions and their children.

Reads session metadata first and never consumes another workspace's messages.
Supports recorded 0.166 rollout items and the older response-item layout.
Unknown records are ignored. No authentication files or state databases are read.
"""
import json
import os
import re
import time
from glob import glob
from urllib.parse import unquote, urlparse

from .. import core, workspace
from .codex import agent_name

MAX_LINE = 8 * 1024 * 1024
SUPPORTED = "Codex JSONL rollout (0.166 and response-item format)"


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("AISET_CODEX_HOME") or os.environ.get("CODEX_HOME") or "~/.codex"))


def local_path(path):
    if isinstance(path, str) and path.startswith("file://"):
        path = unquote(urlparse(path).path)
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
    return path


def inside(path, root):
    try:
        path, root = (os.path.normcase(os.path.realpath(local_path(p))) for p in (path, root))
        return os.path.commonpath((path, root)) == root
    except (TypeError, ValueError, OSError):
        return False


def metadata(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.loads(f.readline(MAX_LINE))
        p = d.get("payload", {}) if d.get("type") == "session_meta" else {}
        return {k: p[k] for k in ("id", "cwd", "source", "parent_thread_id", "agent_role", "agent_nickname") if k in p}
    except (OSError, ValueError, AttributeError):
        return {}


def source(meta):
    value = meta.get("source") or {}
    sub = value.get("subagent", {}) if isinstance(value, dict) else {}
    spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
    return spawn if isinstance(spawn, dict) else {}


def resolve_agent(role, roster):
    return next((m["id"] for m in roster["members"] if role in (m["id"], agent_name(m["id"]))), role or "worker")


def scan(office):
    root = workspace.WORKSPACE
    if not root:
        return []
    now, found = time.time(), []
    cache = getattr(office, "_codex_meta", {})
    for path in glob(os.path.join(home(), "sessions", "**", "*.jsonl"), recursive=True):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if now - stat.st_mtime > office.window:
            continue
        record = cache.get(path)
        identity = (stat.st_dev, stat.st_ino)
        if (not record or record[0] != identity or stat.st_size < record[2]
                or (stat.st_size == record[2] and stat.st_mtime_ns != record[3])):
            record = (identity, metadata(path), stat.st_size, stat.st_mtime_ns)
            cache[path] = record
        else:
            cache[path] = (identity, record[1], stat.st_size, stat.st_mtime_ns)
        meta = record[1]
        if not meta or not inside(meta.get("cwd"), root):
            continue
        old = office.transcripts.get(path)
        if not isinstance(old, CodexSession) or old.session_id != meta.get("id") or old.identity != identity:
            old = office.transcripts[path] = CodexSession(path, meta, office.roster)
            old.identity = identity
        old.refresh()
        found.append(old)
    office._codex_meta = {p: v for p, v in cache.items() if os.path.exists(p)}
    by_id = {t.session_id: t for t in found}
    for t in found:
        t.parent = by_id.get(t.parent_id)
        if t.parent:
            # Native role names and roster ids are different; the parent records the exact spawn role.
            hit = next(((cid, c) for cid, c in t.parent.calls.items()
                        if c.get("childId") == t.session_id or c.get("taskPath") == t.agent_path), None)
            if hit:
                cid, call = hit
                t.meta.update({"agentType": call["agent"], "toolUseId": cid})
                call["childId"] = t.session_id
            t.task = t.task or t.parent.task
    return found


class CodexSession(core.Transcript):
    provider = "codex"

    def _switch_task(self, tid):
        super()._switch_task(tid)
        for step in self.steps:
            if step.get("taskId") is None:
                step["taskId"] = tid

    def __init__(self, path, meta, roster):
        spawn = source(meta)
        parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
        role = resolve_agent(meta.get("agent_role") or spawn.get("agent_role"), roster)
        super().__init__(path, "sub" if parent else "main", os.path.basename(meta.get("cwd") or "workspace"),
                         {"agentType": role, "description": meta.get("agent_nickname") or spawn.get("agent_nickname") or role})
        self.session_id = meta.get("id") or self.session_id
        self.parent_id, self.agent_path = parent, spawn.get("agent_path")
        self.cwd = local_path(meta.get("cwd")) or workspace.WORKSPACE
        self.roster = roster
        self.lifecycle, self.current = "unknown", ""
        self._seen, self._usage_records = set(), False
        self._turn, self._model, self._snapshot = None, None, {}
        self._snapshots, self.steps = {}, []
        self._source_meta = meta
        self.identity = None

    def refresh(self):
        try:
            stat = os.stat(self.path)
            if stat.st_size < self.offset:
                identity = self.identity
                self.__init__(self.path, self._source_meta, self.roster)
                self.identity = identity
            self.mtime = stat.st_mtime
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                while True:
                    start = f.tell()
                    line = f.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        # Skip an oversized record without decoding or retaining its contents.
                        while line and not line.endswith(b"\n"):
                            line = f.readline(MAX_LINE + 1)
                        self.offset = f.tell()
                        continue
                    if not line.endswith(b"\n"):
                        self.offset = start
                        break
                    self.offset = f.tell()
                    try:
                        self._record(json.loads(line))
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue  # malformed/unknown records never take down the dashboard
            self.size = stat.st_size
        except OSError:
            return

    def _board(self, text):
        hit = core.ASSIGN_RE.search(text) or core.BOARD_RE.search(text)
        if hit:
            self._switch_task(core.task_id(hit.group(1)))

    def _event(self, ts, kind, text):
        self.events.append({"t": ts, "kind": kind, "text": core.clip(text, 600)})
        self.current = core.clip(text, 400)

    def _record(self, d):
        p = d.get("payload") or {}
        typ, kind = d.get("type"), p.get("type")
        ts = core.local_time(d.get("timestamp")) or self.mtime
        if typ == "turn_context":
            self._turn, self._model = p.get("turn_id"), p.get("model")
            self.model = self._model
        elif typ == "token_usage_record":
            if not self._usage_records:
                self.usage.clear()  # precise per-response records replace compatibility snapshots
                self.task_of.clear()
                self._usage_records = True
            self._put_usage(p.get("response_id"), p.get("usage"), p.get("turn_id"))
        elif typ == "event_msg":
            if kind in ("task_started", "turn_started"):
                if self.lifecycle == "failed":
                    self.current = "Resuming"
                self.lifecycle = "active"
                self._turn = p.get("turn_id") or self._turn
            elif kind in ("task_complete", "turn_complete"):
                self.lifecycle = "done"
                self.pending.clear()
                self.report = p.get("last_agent_message") or self.report
            elif kind in ("turn_aborted", "turn_failed", "error"):
                self.lifecycle = "failed"
                self.pending.clear()
                self._event(ts, "tool", "Session interrupted or failed")
            elif kind == "user_message":
                self._message("user", p.get("message", ""), ts)
            elif kind == "agent_message":
                self._message("assistant", p.get("message", ""), ts)
            elif kind == "token_count" and not self._usage_records:
                info = p.get("info") or {}
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    self._snapshot_usage(total)
                    self.context = (info.get("last_token_usage") or {}).get("input_tokens", 0)
            elif kind == "item_completed":
                self._item(p.get("item") or {}, ts)
        elif typ == "response_item":
            if kind == "message":
                text = "\n".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict))
                if p.get("role") in ("user", "assistant"):
                    self._message(p["role"], text, ts)
            elif kind == "function_call":
                try:
                    args = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    args = {}
                self._function(p.get("call_id"), p.get("name"), args, ts)
            elif kind in ("function_call_output", "custom_tool_call_output"):
                self.pending.pop(p.get("call_id"), None)
            elif kind == "custom_tool_call" and p.get("name") == "apply_patch":
                self._patch(p.get("input", ""), ts, p.get("call_id"), confirmed=False)
            elif kind == "custom_tool_call":
                self.pending[p.get("call_id")] = ("shell", "Executing " + str(p.get("name") or "tool"))

    def _message(self, role, text, ts):
        if not text.strip():
            return
        # Provider/system instructions are never retained, and mirrored messages are deduplicated.
        key = (role, text)
        if key in self._seen:
            return
        self._seen.add(key)
        if role == "user":
            self.prompt = self.prompt or text[:6000]
            self.title = self.title or core.clip(text, 100)
            self._board(text)
            if "Read .codex/aiset/pm.md" in text or "Aiset PM" in text or "run.json" in text:
                self.agent_setting = "pm" if self.kind == "main" else None
            self._event(ts, "assignment" if self.kind == "sub" else "you", text)
        else:
            self._event(ts, "say", text)
            self.report = text[:12000] if self.kind == "sub" else self.report

    def _function(self, cid, name, args, ts):
        if not isinstance(args, dict):
            return
        if name in ("spawn_agent", "Agent"):
            role = resolve_agent(args.get("agent_type") or args.get("subagent_type"), self.roster)
            prompt = args.get("message") or args.get("prompt") or ""
            task = args.get("task_name") or args.get("description") or role
            self._board(prompt)
            self.calls[cid] = {"agent": role, "description": task, "prompt": prompt[:6000], "t": ts,
                               "model": args.get("model"), "modelReason": self._reason(prompt),
                               "taskPath": "/root/" + task.strip("/")}
            self.call_msg[cid] = self._turn
            self._event(ts, "delegate", "Assigned " + role + ": " + task)
            self.pending[cid] = ("delegate", task)
        elif name in ("request_user_input", "request_permissions", "AskUserQuestion"):
            self.pending[cid] = ("ask", "Waiting for your answer or approval")
            self._event(ts, "ask", "Waiting for your answer or approval")
        elif name in ("exec_command", "shell", "shell_command"):
            cmd = args.get("cmd") or args.get("command") or ""
            if isinstance(cmd, list):
                cmd = " ".join(cmd)
            self.pending[cid] = ("shell", core.clip(cmd, 160))

    @staticmethod
    def _reason(prompt):
        hit = re.search(r"^\s*MODEL:\s*(.+)$", prompt, re.M)
        return core.clip(hit.group(1), 160) if hit else ""

    def _item(self, item, ts):
        kind = re.sub(r"[_-]", "", item.get("type", "")).lower()
        key = item.get("id")
        if not key or ("item", key) in self._seen:
            return
        self._seen.add(("item", key))
        if kind == "commandexecution":
            cmd = item.get("command") or ""
            if isinstance(cmd, list):
                cmd = cmd[-1] if len(cmd) > 1 and cmd[-2].lower() in ("-command", "-c", "-lc") else " ".join(cmd)
            failed = item.get("status") != "completed" or item.get("exit_code", item.get("exitCode")) not in (None, 0)
            self._step("Bash", {"command": cmd}, ts, key, failed=failed, confidence="command-analysis")
        elif kind == "filechange":
            changes = item.get("changes") or {}
            paths = changes if isinstance(changes, dict) else [v.get("path") for v in changes if isinstance(v, dict)]
            confirmed = item.get("status") == "completed"
            self.steps = [s for s in self.steps if s.get("callId") != key]
            for path in paths:
                if path:
                    self._step("Edit", {"file_path": path}, ts, key + str(path), failed=not confirmed,
                               confidence="confirmed" if confirmed else "proposed")
        elif kind in ("agentmessage",):
            text = item.get("text") or "\n".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))
            self._message("assistant", text, ts)
        elif kind == "subagentactivity":
            cid = item.get("id")
            call = self.calls.get(cid)
            if call:
                call["childId"] = item.get("agent_thread_id") or item.get("agentThreadId")
                call["taskPath"] = item.get("agent_path") or call["taskPath"]
        elif kind in ("collabagenttoolcall", "collabtoolcall"):
            if item.get("tool") in ("spawn_agent", "spawnAgent"):
                ids = item.get("receiverThreadIds") or item.get("receiver_thread_ids") or []
                if key in self.calls and ids:
                    self.calls[key]["childId"] = ids[0]

    def _patch(self, patch, ts, key, confirmed):
        # Older logs expose patch calls without final file-change items. Keep them explicitly proposed.
        for path in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M):
            self._step("Edit", {"file_path": path}, ts, str(key) + path, failed=not confirmed, confidence="proposed")
            self.steps[-1]["callId"] = key

    def _step(self, tool, args, ts, key, failed=False, confidence="confirmed"):
        for field in ("file_path", "path"):
            if args.get(field):
                path = local_path(args[field])
                args[field] = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        if tool == "Bash":
            self._board(args.get("command", ""))
        else:
            self._board(args.get("file_path", ""))
        step = core._route_step(tool, args, ts, self._turn)
        step.update({"failed": failed, "confidence": confidence, "taskId": self.task, "callId": key})
        if tool == "Bash":
            from .shell import targets
            inferred = targets(args.get("command", ""), self.cwd)
            if inferred:
                step["targets"] = [{"kind": kind, **dict(zip(("area", "path"), core.place(path))),
                                    "abs": core.norm(path), "lines": None} for kind, path in inferred[:12]]
                step["kind"] = "edit" if any(k == "edit" for k, _ in inferred) else "read"
        # _route_step's Bash parser also produces exact/inferred targets. Bound retained history.
        self.steps.append(step)
        if len(self.steps) > core.MAX_ROUTE_STEPS:
            self.steps.pop(0)
        event_kind, text = core.describe_tool(tool, args)
        if failed:
            text = ("Proposed: " if confidence == "proposed" else "Failed: ") + text
        self._event(ts, event_kind, text)
        if not failed:
            if step.get("targets"):
                for target in step["targets"]:
                    self.trail.append({"t": ts, "tool": {"edit": "Edit", "read": "Read", "search": "Grep"}[target["kind"]],
                                       "kind": target["kind"], "text": text, "path": target["abs"], "cmd": "",
                                       "confidence": confidence})
            else:
                self.trail.append({"t": ts, "tool": tool, "kind": event_kind, "text": text,
                                   "path": args.get("file_path", ""), "cmd": args.get("command", "")[:400],
                                   "confidence": confidence})
            if args.get("file_path"):
                self.files[("edit", args["file_path"])] += 1
            for target in step.get("targets", []):
                if target.get("abs"):
                    self.files[(target["kind"], target["abs"])] += 1

    def _put_usage(self, key, raw, turn):
        if not key or not isinstance(raw, dict):
            return
        vals = [raw.get(k, 0) for k in ("input_tokens", "cache_write_input_tokens", "cached_input_tokens", "output_tokens")]
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in vals):
            return
        if vals[1] + vals[2] > vals[0]:
            return
        # Codex input includes cached input. The common view separates fresh/cache portions.
        fresh = max(0, vals[0] - vals[1] - vals[2])
        from .pricing import estimate
        row = (self._model or "unknown", fresh, vals[1], vals[2], vals[3], estimate(self._model, *vals))
        self.usage[key] = row
        self.task_of[key] = self.task
        self.totals["turns"] = len(self.usage)
        self.context = vals[0]
        self._turn = turn or self._turn

    def _snapshot_usage(self, total):
        previous = self._snapshot
        keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
        if any(not isinstance(total.get(k, 0), int) or total.get(k, 0) < 0 for k in keys):
            return
        if any(total.get(k, 0) < previous.get(k, 0) for k in keys):
            return  # history resets need precise per-response records, not negative usage
        delta = {k: total.get(k, 0) - previous.get(k, 0) for k in keys}
        if any(delta.values()):
            self._put_usage("snapshot:" + str(len(self.usage)), delta, self._turn)
        self._snapshot = dict(total)

    def tokens(self, keys=None):
        selected = [self.usage[k] for k in keys] if keys is not None else list(self.usage.values())
        result = core.tokens_of(selected)
        result.update({"context": self.context, "model": self.model, "provider": "codex",
                       "modelSource": "session-configuration", "costBasis": "configured-model-api-reference",
                       "rateDate": "2026-10-31"})
        return result

    def state(self, now):
        if self.lifecycle == "failed":
            return core.IDLE, "Interrupted or failed; see the terminal"
        if any(kind == "ask" for kind, _ in self.pending.values()):
            return core.WAITING, self.current
        if self.lifecycle == "done":
            return core.DONE, "Report delivered" if self.kind == "sub" else self.current
        if self.lifecycle == "active":
            if now - self.mtime > 180:
                return core.IDLE, "No recent telemetry; session state unknown"
            return (core.WORKING if self.pending else core.THINKING), self.current or "Working"
        return core.OFF, "No lifecycle data"
