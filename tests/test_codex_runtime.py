"""Native telemetry/hook contracts: synthetic recordings, no model calls."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import core, workspace  # noqa: E402
from aiset.providers import codex_hooks, pricing  # noqa: E402
from aiset.run import RunError, codex_launch  # noqa: E402
from aiset.run.launch import start_prompt  # noqa: E402


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = self.root / "project"
        self.ws.mkdir()
        self.sessions = self.root / "codex/sessions/2026/10/31"
        self.sessions.mkdir(parents=True)
        self.roster = {"provider": "codex", "members": [{"id": "pm"}, {"id": "api"}]}
        self.office = core.Office(self.roster)
        for p in (mock.patch.dict(os.environ, {"AISET_CODEX_HOME": str(self.root / "codex")}),
                  mock.patch.object(workspace, "WORKSPACE", str(self.ws))):
            p.start()
            self.addCleanup(p.stop)

    def write(self, name="parent", cwd=None, parent=None, extra=()):
        path = self.sessions / (name + ".jsonl")
        meta = {"id": name, "cwd": str(cwd or self.ws), "source": "cli"}
        if parent:
            meta.update({"source": {"subagent": {"thread_spawn": {"parent_thread_id": parent,
                         "agent_path": "/root/build", "agent_role": "aiset_api"}}}})
        rows = [{"type": "session_meta", "payload": meta},
                {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-6-luna"}}, *extra]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path

    @staticmethod
    def usage(identifier="response", **kwargs):
        return {"type": "token_usage_record", "payload": {"response_id": identifier, "usage": {
            "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10, **kwargs}}}

    def test_scope_parent_roles_precise_usage_and_replay(self):
        self.write(extra=[{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"text": "Read .codex/aiset/pm.md. Task board: .claude/team/tasks/2026-10-31-build"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "spawn",
             "arguments": json.dumps({"agent_type": "aiset_api", "task_name": "build", "message": "MODEL: cheap for a small change"})}},
            self.usage(), self.usage()])
        self.write("child", parent="parent", extra=[self.usage()])
        self.write("other", cwd=self.root / "unrelated", extra=[self.usage(input_tokens=9000)])
        found = self.office.scan()
        self.assertEqual(len(found), 2)
        child = next(t for t in found if t.kind == "sub")
        self.assertEqual(child.agent_type, "api")
        self.assertEqual(child.parent.session_id, "parent")
        self.assertEqual(child.meta["toolUseId"], "spawn")
        self.assertEqual(child.tokens()["total"], 110)
        self.assertEqual(child.tokens()["input"], 60)
        self.assertEqual(child.tokens()["cacheRead"], 40)
        self.assertEqual(child.tokens()["turns"], 1)
        self.assertEqual(child.tokens()["costBasis"], "configured-model-api-reference")
        before = [t.tokens() for t in found]
        self.assertEqual(before, [t.tokens() for t in self.office.scan()])

    def test_cumulative_snapshots_replaced_by_precise_records(self):
        snapshot = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}}
        path = self.write(extra=[snapshot, snapshot])
        self.assertEqual(self.office.scan()[0].tokens()["total"], 110)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.usage()) + "\n")
        session = self.office.scan()[0]
        self.assertEqual(session.tokens()["total"], 110)
        self.assertEqual(session.tokens()["turns"], 1)

    def test_partial_record_and_truncation(self):
        path = self.write()
        encoded = json.dumps(self.usage())
        with path.open("a", encoding="utf-8") as f:
            f.write(encoded[:20])
        self.assertEqual(self.office.scan()[0].tokens()["total"], 0)
        with path.open("a", encoding="utf-8") as f:
            f.write(encoded[20:] + "\n")
        self.assertEqual(self.office.scan()[0].tokens()["total"], 110)
        self.write(extra=[self.usage("new", input_tokens=50, cached_input_tokens=0, output_tokens=1)])
        self.assertEqual(self.office.scan()[0].tokens()["total"], 51)

    def test_confirmed_patch_replaces_proposal_and_powershell_read(self):
        path = str(self.ws / "api.py")
        self.write(extra=[{"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
            "call_id": "patch", "input": "*** Update File: " + path}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "FileChange",
             "id": "patch", "status": "completed", "changes": {path: {"type": "update"}}}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution",
             "id": "read", "status": "completed", "command": ["pwsh", "-Command", "Get-Content '" + path + "'"]}}}])
        session = self.office.scan()[0]
        self.assertEqual(len(session.steps), 2)
        self.assertEqual(session.steps[0]["confidence"], "confirmed")
        self.assertEqual(session.files[("edit", path)], 1)
        self.assertEqual(session.steps[1]["targets"][0]["kind"], "read")
        self.assertEqual(session.files[("read", core.norm(path))], 1)

    def test_lifecycle_stale_and_unknown_prices(self):
        self.write(extra=[{"type": "event_msg", "payload": {"type": "task_started"}}, self.usage()])
        session = self.office.scan()[0]
        self.assertEqual(session.state(time.time())[0], core.THINKING)
        self.assertEqual(session.state(time.time() + 181)[0], core.IDLE)
        session._record({"type": "event_msg", "payload": {"type": "task_complete"}})
        self.assertEqual(session.state(time.time())[0], core.DONE)
        session._record({"type": "turn_context", "payload": {"model": "unknown-model"}})
        session._record(self.usage("unknown"))
        self.assertIsNone(session.tokens()["cost"])
        self.assertIsNone(core.add_tokens([session.tokens()])["cost"])

    def test_prices_are_references_with_context_threshold(self):
        self.assertAlmostEqual(pricing.estimate("gpt-6-luna", 1000000, 0, 0, 1000000), .95)
        self.assertAlmostEqual(pricing.estimate("gpt-6.1-sol", 100, 0, 40, 10), .000224)
        self.assertIsNone(pricing.estimate("gpt-new", 100, 0, 0, 10))
        self.assertIsNone(pricing.estimate("gpt-6-luna", 100, 10, 0, 10))

    def test_provider_switch_clears_history_before_next_task_poll(self):
        history = core.Office({"members": []})
        history.transcripts["old-claude-session"] = object()
        self.office.history = history
        self.office._history_scan = time.time()
        self.office.scan()
        self.assertEqual(history.transcripts, {})
        self.assertEqual(history.roster["provider"], "codex")
        self.assertEqual(self.office._history_scan, 0)


class HookTest(unittest.TestCase):
    def test_native_guard_shell_patch_and_mcp(self):
        for name, inp in (("Bash", {"command": "git push origin HEAD"}),
                          ("exec_command", {"cmd": "gh pr create"}),
                          ("apply_patch", {"command": "*** Update File: .git/config\n"}),
                          ("apply_patch", {"command": "*** Update File: app.py\n*** Move to: .git/hooks/run\n"}),
                          ("mcp__github__create_pull_request", {})):
            with self.subTest(name=name, inp=inp):
                self.assertFalse(codex_hooks.decide({"tool_name": name, "tool_input": inp})[0])
        self.assertTrue(codex_hooks.decide({"tool_name": "Bash", "tool_input": {"command": "git status"}})[0])
        with mock.patch.object(codex_hooks, "working", return_value=False), \
                mock.patch("aiset.providers.read_roster", side_effect=ValueError("mid-edit")):
            self.assertEqual(codex_hooks.handle(".", {"tool_name": "Bash", "tool_input": {"command": "git status"}}), {})
        with mock.patch.object(codex_hooks, "working", return_value=True), \
                mock.patch("aiset.providers.read_roster", side_effect=ValueError("mid-edit")):
            denied = codex_hooks.handle(".", {"tool_name": "Bash", "tool_input": {"command": "git push"}})
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_run_refuses_untrusted_and_disabled_hooks(self):
        h = {"eventName": "preToolUse", "command": codex_hooks.command("."), "enabled": True,
             "trustStatus": "trusted", "matcher": ".*", "timeoutSec": 30}
        with mock.patch.object(codex_launch, "program", return_value="codex"):
            for change in ({}, {"trustStatus": "untrusted"}, {"enabled": False}, {"matcher": "Bash"}):
                with mock.patch.object(codex_launch, "hooks", return_value={"data": [{"hooks": [{**h, **change}]}]}):
                    if change:
                        with self.assertRaises(RunError):
                            codex_launch.ensure_hooks(".")
                    else:
                        codex_launch.ensure_hooks(".")

    def test_run_argv_inherits_model_unless_pinned(self):
        args = codex_launch.argv("board")
        self.assertNotIn("--model", args)
        self.assertNotIn("--dangerously-bypass-hook-trust", args)
        self.assertIn("workspace-write", args)
        self.assertEqual(args[-3:], ["exec", "--json", "-"])
        resumed = codex_launch.argv("board", session_id="native-id", model="gpt-test", effort="low")
        self.assertIn("native-id", resumed)
        self.assertIn("gpt-test", resumed)
        self.assertIn("tell the user to type /exit", start_prompt("board"))
        self.assertNotIn("tell the user to type /exit", start_prompt("board", interactive=False))


if __name__ == "__main__":
    unittest.main()
