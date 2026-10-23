"""The code and symbol queries make ambiguous and guessed matches explicit."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEAMMAP = os.path.join(ROOT, "src", "aiset", "teammap.py")


def _write(root, relative, text):
    path = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


class TeammapLookupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = tmp.name
        sources = {
            "alpha": {
                "src/api.py": (
                    "def get(item_id):\n    return 'alpha'\n\n"
                    "def save(item):\n    return 'alpha'\n"
                ),
            },
            "beta": {
                "src/api.py": (
                    "def get(item_id):\n    return 'beta'\n\n"
                    "def save(item):\n    return 'beta'\n"
                ),
            },
            "same": {
                "src/service.py": (
                    "class Alpha:\n    def get(self):\n        return 'alpha'\n\n"
                    "class Beta:\n    def get(self):\n        return 'beta'\n\n"
                    "class Service:\n    def get(self):\n        return 'service'\n"
                ),
            },
            "substring": {
                "src/models.py": "class BeanUpdate:\n    pass\n",
            },
        }
        for name, files in sources.items():
            root = os.path.join(self.workspace, name)
            for relative, text in files.items():
                _write(root, relative, text)
            _git(root, "init", "-q")
            _git(root, "add", ".")
            _git(
                root,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-qm",
                "fixture",
            )
        roster = {
            "team": "test",
            "members": [
                {"id": name, "name": name, "role": "Engineer", "repos": [name]}
                for name in sources
            ],
        }
        _write(self.workspace, ".claude/team/roster.json", json.dumps(roster))
        self.env = dict(os.environ, AISET_WORKSPACE=self.workspace)
        self.env.pop("AGENT_OFFICE_WORKSPACE", None)

    def run_teammap(self, *args):
        return subprocess.run(
            [sys.executable, TEAMMAP, *args],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_cross_file_code_match_lists_candidates_and_exits_three(self):
        result = self.run_teammap("code", "get")

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("ambiguous matches for 'get'", result.stdout)
        self.assertIn("function get alpha/src/api.py:1", result.stdout)
        self.assertIn("function get beta/src/api.py:1", result.stdout)
        self.assertIn("teammap code <qual>", result.stdout)
        self.assertIn("or add `--repo <name>`", result.stdout)
        self.assertNotIn("return 'alpha'", result.stdout)

    def test_repo_filter_keeps_full_output_and_exits_zero(self):
        result = self.run_teammap("code", "get", "--repo", "alpha")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("function get   alpha/src/api.py:1-2", result.stdout)
        self.assertIn("return 'alpha'", result.stdout)

    def test_qualified_name_keeps_full_output_and_exits_zero(self):
        result = self.run_teammap("code", "Service.get", "--repo", "same")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("method Service.get", result.stdout)
        self.assertIn("return 'service'", result.stdout)

    def test_same_file_overloads_keep_full_output(self):
        result = self.run_teammap("code", "get", "--repo", "same")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ambiguous matches", result.stdout)
        self.assertIn("method Alpha.get", result.stdout)
        self.assertIn("other matches: Beta.get", result.stdout)

        expanded = self.run_teammap("code", "get", "--repo", "same", "--limit", "5")
        self.assertEqual(expanded.returncode, 0, expanded.stdout + expanded.stderr)
        self.assertIn("method Alpha.get", expanded.stdout)
        self.assertIn("method Beta.get", expanded.stdout)
        self.assertIn("method Service.get", expanded.stdout)

    def test_same_file_symbol_overloads_keep_detailed_results(self):
        result = self.run_teammap("symbol", "get", "--repo", "same")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ambiguous matches", result.stdout)
        self.assertIn("method Alpha.get", result.stdout)
        self.assertIn("method Beta.get", result.stdout)

    def test_cross_file_symbol_match_lists_candidates(self):
        result = self.run_teammap("symbol", "save")

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("ambiguous matches for 'save'", result.stdout)
        self.assertIn("function save alpha/src/api.py:4", result.stdout)
        self.assertIn("function save beta/src/api.py:4", result.stdout)
        self.assertNotIn("called by", result.stdout)

    def test_substring_fallback_is_labeled(self):
        result = self.run_teammap("code", "Bean")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            result.stdout.startswith("substring match for 'Bean' (not an exact name):")
        )
        self.assertIn("BeanUpdate", result.stdout)

    def test_no_match_exits_four_and_keeps_search_hint(self):
        for command in ("code", "symbol"):
            with self.subTest(command=command):
                result = self.run_teammap(command, "missing_symbol")
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("no symbol matches 'missing_symbol'", result.stdout)
                self.assertIn("teammap search missing_symbol", result.stdout)

    def test_help_documents_ambiguous_and_missing_exit_statuses(self):
        result = self.run_teammap("--help")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "3  the query matches definitions in different files", result.stdout
        )
        self.assertIn("4  no symbol matches", result.stdout)


if __name__ == "__main__":
    unittest.main()
