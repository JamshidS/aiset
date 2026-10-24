"""Invalid teammap input is a usage error, before any workspace is indexed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TEAMMAP = os.path.join(HERE, "..", "src", "aiset", "teammap.py")
LEGACY = (("symbol", "fetch"), ("code", "fetch"), ("endpoint", "/items"))
RESEARCH = (("impact", "fetch"), ("relevant", "fetch item"), ("failures", "run.log"))


class InvalidInputTest(unittest.TestCase):
    def run_invalid(self, args):
        with tempfile.TemporaryDirectory() as ws:
            repo = os.path.join(ws, "shop")
            os.makedirs(repo)
            with open(os.path.join(repo, "app.py"), "w", encoding="utf-8") as f:
                f.write("def fetch():\n    return 1\n")
            for command in (("init", "-q"), ("add", "."),
                            ("-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "init")):
                subprocess.run(["git", "-C", repo, *command], check=True, capture_output=True)
            team = os.path.join(ws, ".claude", "team")
            os.makedirs(team)
            with open(os.path.join(team, "roster.json"), "w", encoding="utf-8") as f:
                json.dump({"team": "test", "members": [{"id": "engineer", "name": "Test", "role": "Engineer",
                                                        "repos": ["shop"]}]}, f)
            with open(os.path.join(ws, "run.log"), "w", encoding="utf-8") as f:
                f.write("FAIL: test_fetch (app.tests.TestFetch)\nAssertionError: 1 != 2\n")
            env = dict(os.environ, AISET_WORKSPACE=ws)
            env.pop("MSYSTEM", None)
            result = subprocess.run([sys.executable, TEAMMAP, *args], cwd=ws, env=env,
                                    capture_output=True, text=True, encoding="utf-8")
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, output)
            self.assertIn("usage:", output.lower())
            self.assertNotIn("Traceback", output)
            self.assertFalse(os.path.exists(os.path.join(team, "graph")), output)
            return output

    def test_unknown_repository_lists_known_names_without_indexing(self):
        for command in LEGACY + RESEARCH:
            with self.subTest(command=command):
                output = self.run_invalid((*command, "--repo", "nope"))
                for text in ("--repo", "nope", "shop"):
                    self.assertIn(text, output)

    def test_non_integer_limit_is_a_usage_error(self):
        for command in LEGACY + RESEARCH:
            with self.subTest(command=command):
                output = self.run_invalid((*command, "--limit", "abc"))
                self.assertIn("--limit", output)
                self.assertIn("abc", output)

    def test_missing_limit_is_a_usage_error(self):
        for command in LEGACY + RESEARCH:
            with self.subTest(command=command):
                output = self.run_invalid((*command, "--limit"))
                self.assertIn("--limit", output)

    def test_non_integer_max_is_a_usage_error(self):
        output = self.run_invalid(("code", "fetch", "--max", "abc"))
        self.assertIn("--max", output)
        self.assertIn("abc", output)

    def test_missing_max_is_a_usage_error(self):
        output = self.run_invalid(("code", "fetch", "--max"))
        self.assertIn("--max", output)

    def test_non_integer_max_chars_is_a_usage_error(self):
        for command in RESEARCH:
            with self.subTest(command=command):
                output = self.run_invalid((*command, "--max-chars", "abc"))
                self.assertIn("--max-chars", output)
                self.assertIn("abc", output)

    def test_missing_max_chars_is_a_usage_error(self):
        for command in RESEARCH:
            with self.subTest(command=command):
                output = self.run_invalid((*command, "--max-chars"))
                self.assertIn("--max-chars", output)


if __name__ == "__main__":
    unittest.main()
