"""Codex setup, preservation, launch and provider boundaries. No model/network calls."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import cli, core, init_team, launch_pm, providers, registry, settings, sync_names, workspace  # noqa: E402
from aiset.providers import codex  # noqa: E402
from aiset.run import cli as run_cli  # noqa: E402

try:
    import tomllib
except ImportError:
    tomllib = None  # Python 3.9/3.10 still run generation/preservation tests.


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aiset-codex-")
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name) / "my team"
        (self.ws / "api" / ".git").mkdir(parents=True)
        (self.ws / "api" / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"AISET_HOME": str(Path(self.temp.name) / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.bin = str(Path(self.temp.name) / "bin")
        self.repos = init_team.scan(str(self.ws))
        self.roster, _ = init_team.build_roster(str(self.ws), "Demo", self.repos, None)
        self.roster["provider"] = "codex"
        self.roster["pmCommand"] = "aiset pm"

    def initialize(self, *args):
        with mock.patch.object(sys, "argv", ["aiset-init", str(self.ws), "--team", "Demo", *args]), \
                mock.patch.object(registry, "scripts_dir", return_value=(self.bin, True)), \
                mock.patch("aiset.providers.shutil.which", return_value="codex"), mock.patch("builtins.print"):
            init_team.main()
        return providers.read_roster(str(self.ws))

    def generate(self, force=False):
        return codex.write_agents(str(self.ws), self.roster, self.repos, force)

    def test_default_and_explicit_provider(self):
        self.assertEqual(providers.name({}), "claude")
        for bad in ({"provider": "other"}, {"provider": None}, []):
            with self.assertRaises(ValueError):
                providers.name(bad)

    def test_codex_initialization_and_remembered_provider(self):
        roster = self.initialize("--provider", "codex")
        self.assertEqual(roster["provider"], "codex")
        self.assertEqual(roster["pmCommand"], "demo-pm")
        for member in roster["members"]:
            self.assertTrue((self.ws / ".codex" / "agents" / (member["id"] + ".toml")).is_file())
        self.assertTrue((self.ws / ".claude/team/knowledge/api.md").is_file())
        self.assertFalse((self.ws / ".claude/agents").exists())
        self.assertEqual(self.initialize()["provider"], "codex")

    def test_missing_cli_fails_before_team_creation(self):
        with mock.patch.object(sys, "argv", ["aiset-init", str(self.ws), "--provider", "codex"]), \
                mock.patch("aiset.providers.shutil.which", return_value=None), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                init_team.main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.ws / ".claude").exists())
        self.assertFalse((self.ws / ".codex").exists())

    def test_explicit_switch_keeps_claude_files_models_and_memory(self):
        roster = self.initialize()
        path = self.ws / ".claude/team/roster.json"
        roster["members"][1]["model"] = "sonnet"
        path.write_text(json.dumps(roster), encoding="utf-8")
        brief = self.ws / ".claude/team/knowledge/api.md"
        brief.write_text("User knowledge\n", encoding="utf-8")
        original = (self.ws / ".claude/agents/api.md").read_bytes()
        roster = self.initialize("--provider", "codex")
        self.assertEqual(roster["members"][1]["model"], "sonnet")
        self.assertNotIn("sonnet", (self.ws / ".codex/agents/api.toml").read_text(encoding="utf-8"))
        self.assertEqual((self.ws / ".claude/agents/api.md").read_bytes(), original)
        self.assertEqual(brief.read_text(), "User knowledge\n")
        self.assertEqual(self.initialize("--provider", "claude")["provider"], "claude")
        self.assertTrue((self.ws / ".codex/agents/api.toml").exists())

    def test_reinit_preserves_edited_toml_even_with_generated_comment(self):
        self.generate()
        path = self.ws / ".codex/agents/api.toml"
        path.write_text(path.read_text(encoding="utf-8") + "# my custom instructions\n", encoding="utf-8")
        before = path.read_bytes()
        self.roster["members"][1]["name"] = "New name"
        _, kept = self.generate()
        self.assertIn(".codex/agents/api.toml", kept)
        self.assertEqual(path.read_bytes(), before)
        self.generate(force=True)
        self.assertNotEqual(path.read_bytes(), before)

    def test_force_never_claims_foreign_file(self):
        path = self.ws / ".codex/agents/api.toml"
        path.parent.mkdir(parents=True)
        path.write_text("# owned by the user\n", encoding="utf-8")
        self.generate(force=True)
        self.assertEqual(path.read_text(), "# owned by the user\n")

    def test_project_instructions_and_user_edits_outside_block_survive(self):
        path = self.ws / "AGENTS.md"
        path.write_bytes(b"\xef\xbb\xbf# User rules\r\nKeep these.\r\n")
        self.generate()
        text = path.read_bytes()
        self.assertTrue(text.startswith(b"\xef\xbb\xbf# User rules\r\nKeep these.\r\n"))
        path.write_bytes(text + b"\nMore user rules.\n")
        self.roster["team"] = "Renamed"
        self.generate()
        text = path.read_text(encoding="utf-8")
        self.assertIn("More user rules.", text)
        self.assertIn("Renamed", text)
        self.assertEqual(text.count(codex.START), 1)

    def test_edited_managed_project_block_is_preserved(self):
        self.generate()
        path = self.ws / "AGENTS.md"
        path.write_text(path.read_text().replace("## Aiset team", "## My team"), encoding="utf-8")
        before = path.read_bytes()
        _, kept = self.generate(force=True)
        self.assertIn("AGENTS.md (edited Aiset section)", kept)
        self.assertEqual(path.read_bytes(), before)

    def test_broken_markers_fail_before_any_codex_files(self):
        (self.ws / "AGENTS.md").write_text(codex.START + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.generate()
        self.assertFalse((self.ws / ".codex").exists())

    def test_manifest_without_ownership_does_not_overwrite(self):
        self.generate()
        before = (self.ws / ".codex/agents/api.toml").read_bytes()
        (self.ws / codex.MANIFEST).unlink()
        self.roster["members"][1]["name"] = "Changed"
        self.generate()
        self.assertEqual((self.ws / ".codex/agents/api.toml").read_bytes(), before)

    def test_bad_manifest_and_invalid_ids_are_rejected(self):
        self.generate()
        path = self.ws / codex.MANIFEST
        path.write_text('[]', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.generate()
        path.unlink()
        self.roster["members"][1]["id"] = "../../outside"
        with self.assertRaisesRegex(ValueError, "invalid Codex agent id"):
            self.generate()

    def test_outputs_and_role_instructions_are_native(self):
        self.generate()
        for path in (self.ws / ".codex/agents").glob("*.toml"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("subagent_type", text)
            self.assertNotIn("claude --agent", text)
            self.assertNotIn("{{", text)
            self.assertIn("developer_instructions = ", text)
            if tomllib:
                parsed = tomllib.loads(text)
                self.assertIn(".claude/team", parsed["developer_instructions"])
                self.assertEqual(parsed["name"], codex.agent_name(path.stem))

    def test_native_names_avoid_reserved_root_and_normalization_collisions(self):
        self.assertEqual(codex.agent_name("root"), "aiset_root")
        names = [codex.agent_name(mid) for mid in ("root", "foo-bar", "foo.bar", "foo_bar", "Türkçe")]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(re.fullmatch(r"[a-zA-Z0-9_]+", value) for value in names))

    @unittest.skipUnless(tomllib, "stdlib TOML reader needs Python 3.11+")
    def test_toml_quotes_unicode_newlines_and_controls(self):
        member = {"id": "engineer", "name": 'Özge "A"', "role": "Engineer", "codex": {"model": "gpt-example", "reasoning": "high"}}
        text = 'Türkçe "quotes"\nC:\\space folder\t\b\f\x00\x1f\x7f'
        parsed = tomllib.loads(codex.agent_toml(member, text))
        self.assertEqual(parsed["developer_instructions"], text)
        self.assertEqual(parsed["model"], "gpt-example")
        self.assertEqual(parsed["model_reasoning_effort"], "high")

    def test_settings_keep_claude_and_codex_preferences_separate(self):
        self.roster["members"][1]["model"] = "sonnet"
        out = settings.apply_roster(self.roster, {"members": [{"id": "api", "model": "gpt-example", "reasoning": "high"}]})
        self.assertEqual(out["members"][1]["model"], "sonnet")
        self.assertEqual(out["members"][1]["codex"], {"model": "gpt-example", "reasoning": "high"})
        for fields in ({"model": "opus"}, {"reasoning": "invented"}):
            with self.assertRaises(ValueError):
                settings.apply_roster(out, {"members": [{"id": "api", **fields}]})
        self.assertEqual([x["id"] for x in settings.models(out)], [""])
        for bad in (None, [], False, "custom"):
            with self.assertRaises(ValueError):
                codex.options({"id": "api", "codex": bad})

    def test_sync_updates_only_unedited_codex_files(self):
        self.generate()
        path = self.ws / ".codex/agents/api.toml"
        out = settings.apply_roster(self.roster, {"members": [{"id": "api", "name": "Ali", "model": "gpt-example"}]})
        with mock.patch.object(workspace, "WORKSPACE", str(self.ws)):
            sync_names.sync(out)
            self.assertIn("gpt-example", path.read_text(encoding="utf-8"))
            self.assertTrue(settings.agent_files(out)["api"]["generated"])
            path.write_text(path.read_text(encoding="utf-8") + "# custom\n", encoding="utf-8")
            before = path.read_bytes()
            sync_names.sync(settings.apply_roster(out, {"members": [{"id": "api", "model": ""}]}))
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(settings.agent_files(out)["api"]["generated"])

    def test_pm_dispatch_does_not_start_dashboard(self):
        with mock.patch.object(sys, "argv", ["aiset", "pm", "--workspace", str(self.ws)]), \
                mock.patch.object(launch_pm, "main", return_value=0) as call:
            self.assertEqual(cli.main(), 0)
        call.assert_called_once_with(["--workspace", str(self.ws)])

    def test_pm_launches_native_codex_with_model_and_workspace(self):
        self.initialize("--provider", "codex", "--no-launcher")
        roster = providers.read_roster(str(self.ws))
        roster["members"][0]["codex"] = {"model": "gpt-example", "reasoning": "high"}
        (self.ws / ".claude/team/roster.json").write_text(json.dumps(roster), encoding="utf-8")
        with mock.patch("shutil.which", return_value="codex.exe"), mock.patch("subprocess.call", return_value=7) as call:
            self.assertEqual(launch_pm.main(["--workspace", str(self.ws), "--model", "gpt-override", "--no-alt-screen"]), 7)
        command = call.call_args.args[0]
        self.assertEqual(command[:5], ["codex.exe", "--model", "gpt-override", "-c", 'model_reasoning_effort="high"'])
        self.assertEqual(command[-1], launch_pm.PROMPT)
        self.assertIn("--no-alt-screen", command)
        self.assertEqual(call.call_args.kwargs["cwd"], str(self.ws))

    def test_codex_office_never_reads_claude_transcripts(self):
        office = core.Office(self.roster)
        office.transcripts["stale-claude"] = object()
        with mock.patch.object(core, "glob", side_effect=AssertionError("must not read Claude transcripts")):
            self.assertEqual(office.scan(), [])
        self.assertEqual(office.transcripts, {})

    def test_aiset_run_selects_codex_provider(self):
        self.initialize("--provider", "codex", "--no-launcher")
        with mock.patch.object(workspace, "WORKSPACE", str(self.ws)), \
                mock.patch.object(workspace, "ROSTER", str(self.ws / ".claude/team/roster.json")):
            self.assertEqual(run_cli._workspace(None).provider, "codex")

    def test_default_model_policy_belongs_to_pm_and_hook_edits_are_preserved(self):
        self.generate()
        text = (self.ws / ".codex/aiset/pm.md").read_text(encoding="utf-8")
        self.assertIn("You choose the model", text)
        self.assertNotIn("gpt-6-luna", text)
        self.roster["members"][1]["codex"] = {"model": "gpt-custom", "reasoning": "high"}
        self.generate()
        text = (self.ws / ".codex/aiset/pm.md").read_text(encoding="utf-8")
        self.assertIn("model=gpt-custom, reasoning=high", text)
        hook = self.ws / ".codex/hooks.json"
        hook.write_text('{"hooks": {}}', encoding="utf-8")
        _, kept = self.generate()
        self.assertIn(".codex/hooks.json", kept)
        self.assertEqual(hook.read_text(encoding="utf-8"), '{"hooks": {}}')

    def test_single_repository_local_setup_preserves_project_instructions(self):
        (self.ws / "api/.git").rmdir()  # this fixture is now one repository, not a folder of repositories
        (self.ws / ".git/info").mkdir(parents=True)
        project = self.ws / "AGENTS.md"
        self.initialize("--provider", "codex", "--no-launcher")
        self.assertFalse(project.exists())
        project.write_text("# Existing project rules\n", encoding="utf-8")
        template = Path(self.temp.name) / "empty-git-template"
        template.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TEMPLATE_DIR=str(template))
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.ws), *args], env=env, text=True, stderr=subprocess.STDOUT)
        git("init", "-q")
        git("add", "AGENTS.md", "api/pyproject.toml")
        git("-c", "user.name=Aiset test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "isolated test baseline")
        roster = self.initialize("--provider", "codex", "--no-launcher")
        self.assertEqual(project.read_text(encoding="utf-8"), "# Existing project rules\n")
        self.assertEqual(git("status", "--porcelain"), "")
        self.assertTrue((self.ws / ".codex/aiset/pm.md").is_file())
        data = codex.manifest(str(self.ws))
        self.assertFalse(data["projectGuidance"])
        codex.sync(str(self.ws), roster)
        self.assertEqual(project.read_text(encoding="utf-8"), "# Existing project rules\n")
        self.assertEqual(git("status", "--porcelain"), "")
        self.initialize("--provider", "codex", "--share", "--no-launcher")
        self.assertIn(codex.START, project.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows launcher")
    def test_generated_cmd_reaches_native_codex_and_preserves_arguments(self):
        self.initialize("--provider", "codex", "--no-launcher")
        stub = Path(self.temp.name) / "codex.cmd"
        stub.write_text("@echo off\n@echo %CD%\n@echo %*\n", encoding="utf-8")
        launcher = registry.write_launcher("Demo", str(self.ws), self.bin, provider="codex")
        env = dict(os.environ, PATH=str(stub.parent) + os.pathsep + os.environ["PATH"],
                   AISET_WORKSPACE=str(Path(self.temp.name) / "another-team"))
        result = subprocess.run(["cmd", "/c", launcher, "--model", "gpt-example"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.ws), result.stdout)
        self.assertIn("--model gpt-example", result.stdout)
        self.assertIn(".codex/aiset/pm.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
