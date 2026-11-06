"""The PreToolUse hook suggests teammap for greps and long reads it can answer, and stays out of the way otherwise."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)

from aiset import init_team, nudge  # noqa: E402


class NudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "ws")
        self.api = os.path.join(self.ws, "api")
        os.makedirs(os.path.join(self.api, "app"))
        os.makedirs(os.path.join(self.ws, "infra"))  # no Python or TypeScript: nothing for teammap
        with open(os.path.join(self.api, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname = 'api'\n")
        self.big = os.path.join(self.api, "app", "big.py")
        with open(self.big, "w", encoding="utf-8") as f:
            f.write("".join(f"def f{i}():\n    return {i}\n\n" for i in range(150)))

    def tearDown(self):
        self.tmp.cleanup()

    def suggest(self, tool, inp, cwd=None):
        hit = nudge.suggest(self.ws, tool, inp, cwd or self.ws, "teammap")
        return hit and hit[1]

    def test_grep_for_names_and_routes(self):
        self.assertIn("`teammap code update_bean`", self.suggest("Grep", {"pattern": "def update_bean", "path": self.api}))
        self.assertIn("`teammap code Bean`", self.suggest("Grep", {"pattern": r"^class Bean\b", "path": self.api}))
        self.assertIn("`teammap symbol BeanService`", self.suggest("Grep", {"pattern": "BeanService", "path": self.api}))
        self.assertIn("`teammap symbol save_all`", self.suggest("Grep", {"pattern": r"save_all\(", "path": self.api}))
        self.assertIn("`teammap endpoint /api/items`", self.suggest("Grep", {"pattern": r"\/api\/items", "path": self.api}))
        # a function and its model in one go, as agents search
        self.assertIn("`teammap symbol update_bean` gives its definition, signature, callers and callees in one call, then `symbol UpdateBean`",
                      self.suggest("Grep", {"pattern": "update_bean|UpdateBean", "path": self.api}))
        self.assertIsNone(self.suggest("Grep", {"pattern": "update_bean|timeout.*", "path": self.api}))

    def test_piped_test_runner_suggests_teammap_run(self):
        for runner in ("pytest -q", "npm test", "jest --runInBand", "vitest run", "go test ./...", "make test"):
            for truncator in ("tail -20", "head -20"):
                hit = self.suggest("Bash", {"command": f"{runner} | {truncator}"})
                self.assertIsNotNone(hit, (runner, truncator))
                self.assertIn(f"teammap run -- {runner}", hit)
                self.assertIn("full log", hit)
        self.assertIsNone(self.suggest("Bash", {"command": "pytest -q"}))
        self.assertIsNone(self.suggest("Bash", {"command": "echo pytest -q | tail -20"}))
        for command, expected in (
            ("pytest -q 2>&1 | tail -20", "pytest -q"),
            ("python -m pytest -q | tail -20", "python -m pytest -q"),
            ("uv run pytest -q | head -20", "uv run pytest -q"),
            ("npx jest --runInBand | tail -20", "npx jest --runInBand"),
            ("npx vitest run | head -20", "npx vitest run"),
        ):
            hit = self.suggest("Bash", {"command": command})
            self.assertIn(f"teammap run -- {expected}", hit, command)

    def test_grep_it_leaves_alone(self):
        for inp in ({"pattern": "todo", "path": self.api},  # a plain word: as likely text as a name
                    {"pattern": "timeout.*retry", "path": self.api},  # a regex
                    {"pattern": "BeanService", "path": self.api, "glob": "*.md"},  # docs
                    {"pattern": "BeanService", "path": os.path.join(self.ws, "infra")},  # nothing indexable
                    {"pattern": "BeanService", "path": os.path.join(self.ws, ".claude")},
                    {"pattern": "BeanService", "path": self.tmp.name}):  # outside the workspace
            self.assertIsNone(self.suggest("Grep", inp), inp)

    def test_workspace_wide_grep_once_something_is_indexed(self):
        self.assertIsNone(self.suggest("Grep", {"pattern": "BeanService"}))
        index = os.path.join(self.ws, ".claude", "team", "graph", "index")
        os.makedirs(index)
        open(os.path.join(index, "api.json"), "w").close()
        self.assertIn("symbol BeanService", self.suggest("Grep", {"pattern": "BeanService"}))

    def test_whole_reads_of_long_source_files(self):
        msg = self.suggest("Read", {"file_path": self.big})
        self.assertIn("api/app/big.py has 450 lines", msg)
        self.assertIn("`teammap outline big.py` shows its parts and their sizes", msg)
        self.assertIsNone(self.suggest("Read", {"file_path": self.big, "offset": 10, "limit": 40}))
        self.assertIsNone(self.suggest("Read", {"file_path": os.path.join(self.api, "pyproject.toml")}))
        small = os.path.join(self.api, "app", "small.py")
        with open(small, "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        self.assertIsNone(self.suggest("Read", {"file_path": small}))

    def test_bash_greps_and_cat(self):
        self.assertIn("symbol BeanService", self.suggest("Bash", {"command": "grep -rn BeanService app | head -20"}, self.api))
        self.assertIn("symbol BeanService", self.suggest("Bash", {"command": "cd api && rg -n -e BeanService app"}, self.api))
        self.assertIn("symbol BeanService", self.suggest("Bash", {"command": "git grep BeanService"}, self.api))
        # what an agent actually ran: cd into the repo, grep with BRE alternation, then a sed
        cmd = f'cd "{self.api}" && grep -rn "BeanUpdate\\|update_bean\\|@router" --include=*.py . ; echo ---; sed -n 1,5p app/big.py'
        self.assertIn("symbol BeanUpdate` gives", self.suggest("Bash", {"command": cmd}))
        self.assertIn("has 450 lines", self.suggest("Bash", {"command": "cat app/big.py"}, self.api))
        self.assertIn("has 450 lines", self.suggest("Bash", {"command": f'cd "{self.api}" && cat app/big.py'}))
        self.assertIsNone(self.suggest("Bash", {"command": "cat app/big.py | head -40"}, self.api))
        self.assertIsNone(self.suggest("Bash", {"command": "python teammap.py symbol BeanService; grep -rn BeanService app"}, self.api))
        self.assertIsNone(self.suggest("Bash", {"command": "pytest -q"}, self.api))

    def run_hook(self, payload):
        p = subprocess.run([sys.executable, os.path.join(SRC, "aiset", "nudge.py"), self.ws],
                           input=payload, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_hook_output_once_per_session_and_never_a_decision(self):
        session = "test-" + uuid.uuid4().hex
        payload = json.dumps({"session_id": session, "cwd": self.ws, "tool_name": "Grep",
                              "tool_input": {"pattern": "def update_bean", "path": self.api}})
        try:
            out = json.loads(self.run_hook(payload))
            self.assertEqual(set(out["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
            self.assertIn("code update_bean", out["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(self.run_hook(payload), "")  # the same suggestion again: silent
        finally:
            seen = os.path.join(tempfile.gettempdir(), f"aiset-nudge-{session}.txt")
            if os.path.exists(seen):
                os.remove(seen)
        self.assertEqual(self.run_hook("not json"), "")


class InitHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "it's a ws")  # a quote, to check the YAML quoting
        os.makedirs(os.path.join(self.ws, "api", ".git"))
        with open(os.path.join(self.ws, "api", "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname = 'api'\n")
        self.env = patch.dict(os.environ, {"AISET_HOME": os.path.join(self.tmp.name, "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def init(self, *args):
        with patch.object(sys, "argv", ["aiset-init", self.ws, "--no-launcher", *args]), patch("builtins.print"):
            init_team.main()
        files = {}
        for name in ("api", "tester", "integration-checker", "pm"):
            with open(os.path.join(self.ws, ".claude", "agents", name + ".md"), encoding="utf-8") as f:
                files[name] = f.read()
        return files

    def test_hook_in_the_working_agents_only(self):
        files = self.init()
        for name in ("api", "tester", "integration-checker"):
            front = files[name].split("---\n")[1]
            self.assertIn('    - matcher: "Grep|Read|Bash"\n', front, name)
            cmd = re.search(r"command: '(.*)'\n", front).group(1).replace("''", "'")
            self.assertIn("nudge.py", cmd)
            self.assertTrue(cmd.endswith(f' "{self.ws.replace(os.sep, "/")}"'), cmd)
        self.assertNotIn("nudge.py", files["pm"])  # the PM has the notify hooks instead
        self.assertIn("notify.py", files["pm"])

    def test_no_nudge_is_remembered(self):
        self.init("--no-nudge")
        files = self.init()  # a later run without the flag keeps it off
        self.assertNotIn("hooks:", files["api"])
        with open(os.path.join(self.ws, ".claude", "team", "roster.json"), encoding="utf-8") as f:
            self.assertIs(json.load(f)["nudge"], False)


if __name__ == "__main__":
    unittest.main()
