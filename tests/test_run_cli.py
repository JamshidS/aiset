"""`aiset` dispatch (src/aiset/cli.py) and the `aiset run` flow (src/aiset/run/cli.py), end to end against a temp git
repo with a local bare remote. gh, claude and the terminal are mocked: no network, no Claude, no GitHub."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace
from unittest import TestCase, main, mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from aiset import cli as aiset_cli  # noqa: E402
from aiset import run as run_pkg  # noqa: E402
from aiset.run import board, cli, fetch, gate, gitops, launch, publish  # noqa: E402

with open(os.path.join(HERE, "fixtures", "run", "issue_123.json"), encoding="utf-8") as _f:
    ISSUE = json.load(_f)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def isolate_git(test):
    """Real git in these tests reads no user or system config (hooks, aliases, templates, autocrlf)."""
    p = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    p.start()
    test.addCleanup(p.stop)
    for k in ("GIT_TEMPLATE_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_COUNT"):
        os.environ.pop(k, None)


class DispatchTest(TestCase):
    def fake_server(self):
        fake = types.ModuleType("aiset.server")
        fake.main = mock.Mock(return_value=None)
        return fake

    def test_no_args_and_options_start_the_office(self):
        for argv in (["aiset"], ["aiset", "--port", "0", "--no-browser"], ["aiset", "--workspace", "x"]):
            fake = self.fake_server()
            with mock.patch.dict(sys.modules, {"aiset.server": fake}), mock.patch.object(sys, "argv", argv):
                aiset_cli.main()
            fake.main.assert_called_once_with()

    def test_run_dispatches_without_importing_the_server(self):
        fake = self.fake_server()
        fake.main.side_effect = AssertionError("server must not start")
        with mock.patch.dict(sys.modules, {"aiset.server": fake}), \
                mock.patch.object(sys, "argv", ["aiset", "run", "123", "--dry-run"]), \
                mock.patch("aiset.run.cli.main", return_value=0) as run_main:
            self.assertEqual(aiset_cli.main(), 0)
        run_main.assert_called_once_with(["123", "--dry-run"])
        fake.main.assert_not_called()

    def test_entry_point(self):
        with open(os.path.join(HERE, "..", "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn('aiset = "aiset.cli:main"', f.read())


class RunFlowTest(TestCase):
    def setUp(self):
        isolate_git(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.remote = os.path.join(tmp.name, "remote.git")
        self.ws = os.path.join(tmp.name, "ws")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        os.makedirs(os.path.join(self.ws, ".claude", "team"))
        git(self.ws, "init", "-q")
        git(self.ws, "symbolic-ref", "HEAD", "refs/heads/master")
        for k, v in (("user.name", "Test"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git(self.ws, "config", k, v)
        with open(os.path.join(self.ws, "app.py"), "w") as f:
            f.write("print('hi')\n")
        with open(os.path.join(self.ws, ".claude", "team", "roster.json"), "w") as f:
            f.write('{"team": "t", "members": []}')
        git(self.ws, "add", "app.py")
        git(self.ws, "commit", "-q", "-m", "init")
        git(self.ws, "remote", "add", "origin", self.remote)
        self.boards = os.path.join(self.ws, ".claude", "team", "tasks")
        self.claude_calls = []
        self.gh = mock.Mock(return_value="https://github.com/o/r/pull/7\n")
        ns = SimpleNamespace(path=self.ws, boards=self.boards, repos=["."])
        for p in (mock.patch.object(cli, "_workspace", return_value=ns),
                  mock.patch.object(cli, "_interactive", return_value=True),
                  mock.patch.object(gitops, "origin_repo", return_value="o/r"),
                  mock.patch.object(gitops, "push_urls", return_value=["https://github.com/o/r.git"]),
                  mock.patch.object(gitops, "gh", self.gh),
                  mock.patch.object(fetch, "issue", return_value=dict(ISSUE)),
                  mock.patch.object(fetch, "default_branch", return_value="master"),
                  mock.patch.object(launch, "run_claude", side_effect=self.fake_claude),
                  mock.patch.object(publish, "_usage", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.finish = True
        self.change = True
        self.agent = None  # what else the session does: a function of the board dir

    def fake_claude(self, argv, cwd):
        """The PM session: changes code, writes handoffs and summary, and (when self.finish) sets STATUS: ready."""
        self.claude_calls.append(argv)
        self.assertEqual(cwd, self.ws)
        bdir = os.path.dirname(argv[argv.index("--settings") + 1])
        if self.change:
            with open(os.path.join(self.ws, "app.py"), "a") as f:
                f.write("print('fixed')\n")
            with open(os.path.join(self.ws, "new_test.py"), "w") as f:
                f.write("# test\n")
        if self.agent:
            self.agent(bdir)
        board.write_text(bdir, "handoff/root.md", "AC-1: pass\nSTATUS: done\n")
        board.write_text(bdir, "summary.md", "Fixed it for @maintainer, fixes #9.\n[S] Windows too.\n")
        if self.finish:
            brief = board.read_text(bdir, "brief.md").replace("STATUS: open", "STATUS: ready")
            board.write_text(bdir, "brief.md", brief)
        return 0

    def run_cli(self, argv, answers=()):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("builtins.input", side_effect=list(answers)) as ask, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(ask.call_count, len(answers), out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def only_board(self):
        names = os.listdir(self.boards)
        self.assertEqual(len(names), 1)
        return os.path.join(self.boards, names[0])

    def remote_branches(self):
        return git(self.remote, "branch", "--list").split()

    def test_codex_native_session_uses_shared_gates_and_remembers_id(self):
        from aiset.run import codex_launch
        ws = SimpleNamespace(path=self.ws, boards=self.boards, repos=["."], provider="codex", roster={"members": []})
        def fake_codex(argv, cwd, bdir, remember):
            self.assertEqual(argv[-3:], ["exec", "--json", "-"])
            self.assertNotIn("--model", argv)
            remember("native-codex-session")
            with open(os.path.join(cwd, "app.py"), "a") as f:
                f.write("# Codex change\n")
            board.write_text(bdir, "brief.md", board.read_text(bdir, "brief.md").replace("STATUS: open", "STATUS: ready"))
            return 0
        with mock.patch.object(cli, "_workspace", return_value=ws), \
                mock.patch.object(codex_launch, "ensure_hooks"), \
                mock.patch.object(codex_launch, "run", side_effect=fake_codex) as run:
            code, out, err = self.run_cli(["123"], ["y", "n"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(run.call_count, 1)
        state = board.read_run(self.only_board())
        self.assertEqual(state["provider"], "codex")
        self.assertEqual(state["sessions"], ["native-codex-session"])
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(self.remote_branches(), [])
        self.gh.assert_not_called()

    def test_codex_dry_run_never_starts_model_or_hook_server(self):
        from aiset.run import codex_launch
        ws = SimpleNamespace(path=self.ws, boards=self.boards, repos=["."], provider="codex", roster={"members": []})
        with mock.patch.object(cli, "_workspace", return_value=ws), \
                mock.patch.object(codex_launch, "ensure_hooks") as hooks, \
                mock.patch.object(codex_launch, "run") as run:
            code, out, err = self.run_cli(["123", "--dry-run"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("codex --sandbox workspace-write", out)
        hooks.assert_not_called()
        run.assert_not_called()
        self.assertFalse(os.path.exists(self.boards))

    def test_codex_resume_preserves_native_id_and_explicit_pm_model(self):
        from aiset.run import codex_launch
        ws = SimpleNamespace(path=self.ws, boards=self.boards, repos=["."], provider="codex",
                             roster={"members": [{"id": "pm", "codex": {"model": "gpt-test", "reasoning": "low"}}]})
        calls = []
        def fake_codex(argv, cwd, bdir, remember):
            calls.append(argv)
            self.assertIn("gpt-test", argv)
            remember("native-id")
            if len(calls) == 2:
                with open(os.path.join(cwd, "app.py"), "a") as f:
                    f.write("# resumed\n")
                board.write_text(bdir, "brief.md", board.read_text(bdir, "brief.md").replace("STATUS: open", "STATUS: ready"))
                return 0
            return 1
        with mock.patch.object(cli, "_workspace", return_value=ws), \
                mock.patch.object(codex_launch, "ensure_hooks"), \
                mock.patch.object(codex_launch, "run", side_effect=fake_codex):
            code, out, err = self.run_cli(["123"], ["y", "n"])
            self.assertEqual(code, 0, out + err)
            code, out, err = self.run_cli(["123", "--resume"], ["n"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("resume", calls[1])
        self.assertIn("native-id", calls[1])
        self.assertEqual(board.read_run(self.only_board())["sessions"], ["native-id"])
        self.gh.assert_not_called()

    def test_codex_publish_uses_shared_review_and_excludes_native_config(self):
        from aiset.run import codex_launch
        ws = SimpleNamespace(path=self.ws, boards=self.boards, repos=["."], provider="codex", roster={"members": []})
        def fake_codex(argv, cwd, bdir, remember):
            remember("native-id")
            with open(os.path.join(cwd, "app.py"), "a") as f:
                f.write("# finished\n")
            os.makedirs(os.path.join(cwd, ".codex"))
            with open(os.path.join(cwd, ".codex", "config.toml"), "w") as f:
                f.write("# local\n")
            board.write_text(bdir, "brief.md", board.read_text(bdir, "brief.md").replace("STATUS: open", "STATUS: ready"))
            return 0
        with mock.patch.object(cli, "_workspace", return_value=ws), \
                mock.patch.object(codex_launch, "ensure_hooks"), \
                mock.patch.object(codex_launch, "run", side_effect=fake_codex):
            code, out, err = self.run_cli(["123"], ["y", "y"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(git(self.ws, "show", "--name-only", "--format=", "HEAD").split(), ["app.py"])
        self.assertEqual(self.gh.call_args[0][0][:3], ["pr", "create", "--draft"])

    def test_dry_run_writes_and_launches_nothing(self):
        code, out, _ = self.run_cli(["123", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("AC-1: `aiset` starts with 0 members", out)
        self.assertIn("--disallowedTools", out)
        self.assertIn("Dry run: nothing written, nothing launched.", out)
        self.assertFalse(os.path.exists(self.boards))
        self.assertEqual(self.claude_calls, [])
        self.assertEqual(git(self.ws, "branch", "--show-current").strip(), "master")

    def test_gate1_decline_starts_nothing(self):
        code, out, _ = self.run_cli(["123"], answers=["n"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing started.", out)
        self.assertFalse(os.path.exists(self.boards))
        self.assertEqual(self.claude_calls, [])
        self.assertFalse(gitops.branch_exists(self.ws, "aiset/issue-123-crash-when-the-roster-is-empty"))

    def test_no_terminal_refuses_at_gate1(self):
        with mock.patch.object(cli, "_interactive", return_value=False):
            code, _, err = self.run_cli(["123"])
        self.assertEqual(code, 1)
        self.assertIn("terminal", err)
        self.assertFalse(os.path.exists(self.boards))

    def test_dirty_tree_refused(self):
        with open(os.path.join(self.ws, "stray.txt"), "w") as f:
            f.write("x")
        code, _, err = self.run_cli(["123"])
        self.assertEqual(code, 1)
        self.assertIn("stray.txt", err)
        self.assertFalse(os.path.exists(self.boards))

    def test_full_run_publishes_after_gate2(self):
        code, out, err = self.run_cli(["123"], answers=["y", "y"])
        self.assertEqual(code, 0, err)
        bdir = self.only_board()
        branch = "aiset/issue-123-crash-when-the-roster-is-empty"
        # the session: PM agent, a fresh session id, the gate settings, deny rules, the fixed prompt after --
        argv = self.claude_calls[0]
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[-2], "--")
        self.assertNotIn("roster is empty", argv[-1])  # issue text never goes into the prompt
        self.assertTrue(os.path.isfile(os.path.join(bdir, "gate-settings.json")))
        # board
        self.assertIn("```text", board.read_text(bdir, "issue.md"))
        self.assertIn("- AC-1: `aiset` starts with 0 members", board.read_text(bdir, "brief.md"))
        # commit: only the repo's files, no .claude/, no AI trailer
        self.assertEqual(git(self.ws, "branch", "--show-current").strip(), branch)
        files = git(self.ws, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), ["app.py", "new_test.py"])
        message = git(self.ws, "log", "-1", "--format=%B")
        self.assertEqual(message.strip(), "Crash when the roster is empty (#123)")
        self.assertNotIn("Co-Authored-By", message)
        # pushed to the remote, draft PR with the rendered body
        self.assertIn(branch, self.remote_branches())
        args = self.gh.call_args[0][0]
        self.assertEqual(args[:3], ["pr", "create", "--draft"])
        body_file = next(a for a in args if a.startswith("--body-file="))[len("--body-file="):]
        self.assertIn("--base=master", args)
        body = board.read_text(bdir, "pr.md")
        self.assertEqual(os.path.realpath(body_file), os.path.realpath(os.path.join(bdir, "pr.md")))
        self.assertTrue(body.startswith("Closes #123"))
        self.assertIn("`@maintainer`", body)
        self.assertNotIn("fixes #9", body)
        run = board.read_run(bdir)
        self.assertEqual((run["phase"], run["pr"], run["pushed"]), ("published", "https://github.com/o/r/pull/7", True))
        self.assertIn("gate 2 approved", board.read_text(bdir, "log.md"))
        # a second run just reports the PR
        code, out, _ = self.run_cli(["123"])
        self.assertEqual(code, 0)
        self.assertIn("Already published: https://github.com/o/r/pull/7", out)

    def test_gate2_decline_pushes_nothing_and_keeps_the_branch(self):
        head = git(self.ws, "rev-parse", "HEAD")
        code, out, _ = self.run_cli(["123"], answers=["y", "v", "d", "n"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing committed or pushed", out)
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.ws, "branch", "--show-current").strip(), "aiset/issue-123-crash-when-the-roster-is-empty")
        self.assertEqual(self.remote_branches(), [])
        self.gh.assert_not_called()
        bdir = self.only_board()
        self.assertEqual(board.read_run(bdir)["phase"], "ready")
        self.assertIn("gate 2 declined", board.read_text(bdir, "log.md"))
        # --publish later goes straight to gate 2 without a new session
        code, _, _ = self.run_cli(["123", "--publish"], answers=["y"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(board.read_run(bdir)["phase"], "published")

    def test_unfinished_session_then_resume(self):
        self.finish = False
        code, out, _ = self.run_cli(["123"], answers=["y", "n"])  # gate 1 yes; "publish anyway?" no
        self.assertEqual(code, 0)
        self.assertIn("--resume", out)
        bdir = self.only_board()
        run = board.read_run(bdir)
        self.assertEqual(run["phase"], "working")
        sid = run["sessions"][0]
        self.assertEqual(self.remote_branches(), [])
        self.finish = True
        code, _, _ = self.run_cli(["123", "--resume"], answers=["n"])  # session done; decline gate 2
        self.assertEqual(code, 0)
        argv = self.claude_calls[-1]
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(board.read_run(bdir)["sessions"], [sid])
        self.assertEqual(board.read_run(bdir)["phase"], "ready")

    def test_push_failure_keeps_the_commit_for_a_retry(self):
        git(self.ws, "remote", "set-url", "origin", os.path.join(self.ws, "no-such-remote.git"))
        code, _, err = self.run_cli(["123"], answers=["y", "y"])
        self.assertEqual(code, 1)
        self.assertIn("git push failed", err)
        run = board.read_run(self.only_board())
        self.assertEqual((run["phase"], run["pushed"]), ("ready", False))
        self.assertTrue(run["commit"])
        self.gh.assert_not_called()
        git(self.ws, "remote", "set-url", "origin", self.remote)
        # the fixed URL is a git config change since gate 1: shown loudly and confirmed on its own
        code, out, _ = self.run_cli(["123", "--publish"], answers=["y", "y"])
        self.assertEqual(code, 0)
        self.assertIn("git config remote.origin.url: changed", out)
        self.assertIn("Already committed by aiset run", out)
        self.assertEqual(git(self.ws, "rev-list", "--count", "HEAD").strip(), "2")  # no second commit

    def test_existing_branch_needs_fresh(self):
        git(self.ws, "branch", "aiset/issue-123-crash-when-the-roster-is-empty")
        code, _, err = self.run_cli(["123"])
        self.assertEqual(code, 1)
        self.assertIn("--fresh", err)
        code, out, _ = self.run_cli(["123", "--fresh", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("aiset/issue-123-crash-when-the-roster-is-empty-2", out)

    def test_bad_ref(self):
        code, _, err = self.run_cli(["nonsense"])
        self.assertEqual(code, 1)
        self.assertIn("not an issue number", err)

    # --- gate 2 trusts neither the session nor run.json (integration check F1, F2, F10) ---

    def commit_in_session(self, paths, message):
        def agent(bdir):
            git(self.ws, "add", "--", *paths)
            git(self.ws, "commit", "-q", "-m", message)
        return agent

    def test_start_sha_and_guard_recorded_at_gate1(self):
        head = git(self.ws, "rev-parse", "HEAD").strip()
        self.run_cli(["123"], answers=["y", "n"])
        run = board.read_run(self.only_board())
        self.assertEqual(run["startSha"], head)
        self.assertEqual(set(run["gitGuard"]), {"config", "hooks"})

    def test_agent_commits_are_shown_and_need_their_own_yes(self):
        self.agent = self.commit_in_session(["app.py"], "agent: fix the crash")
        code, out, _ = self.run_cli(["123"], answers=["y", "n"])  # gate 1; "push the session's commits?" no
        self.assertEqual(code, 0)
        self.assertIn("Commits made in the session since gate 1", out)
        self.assertIn("agent: fix the crash", out)
        self.assertEqual(self.remote_branches(), [])
        self.gh.assert_not_called()
        code, out, _ = self.run_cli(["123", "--publish"], answers=["y", "y"])
        self.assertEqual(code, 0)
        log = git(self.ws, "log", "--format=%s", "-3").split("\n")
        self.assertEqual(log[:2], ["Crash when the roster is empty (#123)", "agent: fix the crash"])
        self.assertIn("aiset/issue-123-crash-when-the-roster-is-empty", self.remote_branches())

    def test_no_changes_counts_commits(self):
        self.agent = self.commit_in_session(["app.py", "new_test.py"], "agent: everything")
        code, out, err = self.run_cli(["123"], answers=["y", "y", "y"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Uncommitted files", out)
        self.assertEqual(git(self.ws, "log", "-1", "--format=%s").strip(), "agent: everything")  # no empty commit
        self.assertEqual(board.read_run(self.only_board())["phase"], "published")

    def test_nothing_changed_is_refused(self):
        self.change = False
        code, _, err = self.run_cli(["123"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("no changes", err)

    def test_agent_commit_touching_claude_folder_is_refused(self):
        def agent(bdir):
            os.makedirs(os.path.join(self.ws, "sub", ".Claude"), exist_ok=True)
            with open(os.path.join(self.ws, "sub", ".Claude", "x.md"), "w") as f:
                f.write("x\n")
            git(self.ws, "add", "-f", "--", "sub/.Claude/x.md", "app.py")
            git(self.ws, "commit", "-q", "-m", "agent: notes")
        self.agent = agent
        code, out, err = self.run_cli(["123"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("touch .claude/", err)
        self.assertIn("git reset --soft", err)
        self.assertEqual(self.remote_branches(), [])
        self.gh.assert_not_called()

    def test_agent_commit_with_ai_trailer_is_refused(self):
        self.agent = self.commit_in_session(["app.py"], "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        code, out, err = self.run_cli(["123"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("AI trailer", err)
        self.assertIn("AI trailer", out)
        self.assertEqual(self.remote_branches(), [])

    def test_push_url_must_be_the_issue_repo(self):
        with mock.patch.object(gitops, "push_urls", return_value=["https://user:tok3n@github.com/evil/r.git"]):
            code, _, err = self.run_cli(["123"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("origin pushes to https://***@github.com/evil/r.git, not only github.com/o/r", err)
        self.assertNotIn("tok3n", err)
        self.assertEqual(self.remote_branches(), [])
        self.gh.assert_not_called()

    def test_every_push_url_must_be_the_issue_repo(self):  # integration re-check K2
        urls = ["https://github.com/o/r.git", "https://u:p@ss@github.com/evil/r.git"]
        with mock.patch.object(gitops, "push_urls", return_value=urls):
            code, _, err = self.run_cli(["123"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("origin pushes to https://***@github.com/evil/r.git, not only github.com/o/r", err)
        self.assertNotIn("ss@", err)
        self.assertEqual(self.remote_branches(), [])
        with mock.patch.object(gitops, "push_urls", return_value=[]):
            code, _, err = self.run_cli(["123", "--publish"])
        self.assertEqual(code, 1)
        self.assertIn("(nothing)", err)

    def test_all_push_urls_are_shown(self):  # K2
        urls = ["https://github.com/o/r.git", "git@github.com:o/r.git"]
        with mock.patch.object(gitops, "push_urls", return_value=urls):
            code, out, _ = self.run_cli(["123"], answers=["y", "n"])
        self.assertEqual(code, 0)
        self.assertIn("push to https://github.com/o/r.git\n  push to git@github.com:o/r.git", out)

    def test_earlier_unpushed_commits_are_checked_and_need_their_own_yes(self):  # K7
        os.makedirs(os.path.join(self.ws, "notes", ".claude"))
        with open(os.path.join(self.ws, "notes", ".claude", "x.md"), "w") as f:
            f.write("x\n")
        git(self.ws, "add", "-f", "--", "notes/.claude/x.md")
        git(self.ws, "commit", "-q", "-m", "mine: notes")
        with open(os.path.join(self.ws, "app.py"), "a") as f:
            f.write("# tweak\n")
        git(self.ws, "commit", "-q", "-am", "mine: tweak\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        code, out, _ = self.run_cli(["123"], answers=["y", "n"])  # gate 1; "push them anyway?" no
        self.assertEqual(code, 0)
        self.assertIn("The push also sends 3 earlier commit(s)", out)
        self.assertRegex(out, r"mine: notes\s+<-- touches notes/\.claude/x\.md")
        self.assertRegex(out, r"mine: tweak\s+<-- AI trailer")
        self.assertEqual(self.remote_branches(), [])
        code, out, _ = self.run_cli(["123", "--publish"], answers=["y", "y"])  # the separate yes, then gate 2
        self.assertEqual(code, 0)
        self.assertIn("aiset/issue-123-crash-when-the-roster-is-empty", self.remote_branches())

    def test_new_files_are_shown_in_the_diff_and_github_changes_flagged(self):  # K12
        def agent(bdir):
            os.makedirs(os.path.join(self.ws, ".github", "workflows"))
            with open(os.path.join(self.ws, ".github", "workflows", "ci.yml"), "w") as f:
                f.write("on: pull_request_target\n")
        self.agent = agent
        code, out, _ = self.run_cli(["123"], answers=["y", "d", "n"])
        self.assertEqual(code, 0)
        self.assertIn("!! .github/ CHANGED", out)
        self.assertIn("!!   .github/workflows/ci.yml", out)
        self.assertIn("+# test", out)  # new_test.py's content, not only its name
        self.assertIn("+on: pull_request_target", out)
        self.assertNotIn("not shown", out)
        self.assertEqual(git(self.ws, "diff", "--cached", "--name-only").strip(), "")  # the index is untouched

    def test_push_url_is_shown(self):
        code, out, _ = self.run_cli(["123"], answers=["y", "n"])
        self.assertIn("push to https://github.com/o/r.git", out)
        self.assertIn("draft PR into master on o/r", out)

    def test_hooks_and_config_changed_in_session_warn_and_gate2_runs_hooks_off(self):
        marker = os.path.join(self.ws, "..", "hook-ran").replace("\\", "/")
        script = f"#!/bin/sh\necho ran >> '{marker}'\n"

        def agent(bdir):
            hooks = git(self.ws, "rev-parse", "--git-path", "hooks").strip()
            hooks = hooks if os.path.isabs(hooks) else os.path.join(self.ws, hooks)
            other = os.path.join(self.ws, "..", "evil-hooks")
            for d in (hooks, other):
                os.makedirs(d, exist_ok=True)
                for name in ("pre-commit", "post-commit", "pre-push"):
                    with open(os.path.join(d, name), "w", newline="\n") as f:
                        f.write(script)
                    os.chmod(os.path.join(d, name), 0o700)
            git(self.ws, "config", "alias.ship", "push")
            git(self.ws, "config", "core.hooksPath", other.replace("\\", "/"))
        self.agent = agent
        code, out, _ = self.run_cli(["123"], answers=["y", "n"])  # "publish anyway?" no
        self.assertEqual(code, 0)
        self.assertIn("WARNING: git config or hooks changed since gate 1", out)
        self.assertIn("git config alias.ship: added", out)
        self.assertIn("git config core.hookspath: added", out)
        self.assertEqual(self.remote_branches(), [])
        code, out, _ = self.run_cli(["123", "--publish"], answers=["y", "y"])
        self.assertEqual(code, 0)
        self.assertIn("aiset/issue-123-crash-when-the-roster-is-empty", self.remote_branches())
        self.assertFalse(os.path.exists(marker), "a hook ran during gate 2's commit or push")

    def tamper(self, **fields):
        bdir = self.only_board()
        run = board.read_run(bdir)
        run.update(fields)
        board.write_run(bdir, run)

    def test_publish_checks_run_json_against_the_repo(self):
        self.run_cli(["123"], answers=["y", "n"])
        good = board.read_run(self.only_board())
        for fields, expect in (({"branch": "master"}, "only publishes its own aiset/issue-123"),
                               ({"base": "--web"}, "odd base branch"),
                               ({"startSha": "0" * 40}, "not an ancestor"),
                               ({"startSha": None}, "not an ancestor"),
                               ({"repoPath": self.remote.replace("\\", "/")}, "this issue's repo is")):
            self.tamper(**fields)
            code, _, err = self.run_cli(["123", "--publish"])
            self.assertEqual(code, 1, fields)
            self.assertIn(expect, err, fields)
            self.tamper(**good)
        with mock.patch.object(gitops, "origin_repo", side_effect=["o/r", "o/other"]):
            code, _, err = self.run_cli(["123", "--publish"])
        self.assertEqual(code, 1)
        self.assertIn("not o/r", err)
        self.assertEqual(self.remote_branches(), [])
        self.gh.assert_not_called()

    def test_pushed_flag_in_run_json_does_not_skip_the_push(self):
        self.run_cli(["123"], answers=["y", "n"])
        self.tamper(pushed=True, commit="f" * 40)
        code, out, _ = self.run_cli(["123", "--publish"], answers=["y"])
        self.assertEqual(code, 0)
        self.assertNotIn("Already committed", out)  # a commit sha that isn't HEAD is ignored
        self.assertIn("aiset/issue-123-crash-when-the-roster-is-empty", self.remote_branches())
        files = git(self.ws, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), ["app.py", "new_test.py"])

    def test_cmd_unsafe_board_path_refused_on_windows(self):
        real = launch.check_path
        with mock.patch.object(launch, "check_path", side_effect=lambda p: real(p, windows=True)):
            with mock.patch.object(board, "new_id", return_value="2026-10-29-issue-123-a&b"):
                code, _, err = self.run_cli(["123"])
        self.assertEqual(code, 1)
        self.assertIn("cmd.exe", err)
        self.assertFalse(os.path.exists(self.boards))
        self.assertEqual(self.claude_calls, [])


class GitopsTest(TestCase):
    """The git facts gate 2 relies on, against a real repo."""

    def setUp(self):
        isolate_git(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        git(self.repo, "init", "-q")
        for k, v in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            git(self.repo, "config", k, v)
        git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")

    def test_push_url_follows_pushurl_and_insteadof(self):
        self.assertEqual(gitops.push_urls(self.repo), ["https://github.com/o/r.git"])
        git(self.repo, "config", "url.https://github.com/evil/.pushInsteadOf", "https://github.com/o/")
        self.assertEqual([gitops.github_repo(u) for u in gitops.push_urls(self.repo)], ["evil/r"])
        git(self.repo, "config", "remote.origin.pushurl", "git@github.com:other/x.git")
        self.assertEqual([gitops.github_repo(u) for u in gitops.push_urls(self.repo)], ["other/x"])

    def test_every_pushurl_is_listed(self):  # K2: a push goes to all of them, not just the first
        git(self.repo, "config", "remote.origin.pushurl", "https://github.com/o/r.git")
        git(self.repo, "config", "--add", "remote.origin.pushurl", "https://github.com/evil/r.git")
        self.assertEqual([gitops.github_repo(u) for u in gitops.push_urls(self.repo)], ["o/r", "evil/r"])
        git(self.repo, "remote", "remove", "origin")
        self.assertEqual(gitops.push_urls(self.repo), [])

    def test_shown_url_masks_a_password_with_an_at(self):  # K9
        self.assertEqual(gitops.shown_url("https://u:p@ss@github.com/o/r.git"), "https://***@github.com/o/r.git")
        self.assertEqual(gitops.shown_url("https://github.com/o/r.git"), "https://github.com/o/r.git")
        self.assertEqual(gitops.shown_url("git@github.com:o/r.git"), "git@github.com:o/r.git")

    def test_untracked_diff_shows_content(self):  # K12
        with open(os.path.join(self.repo, "new file.txt"), "w") as f:
            f.write("hello\n")
        out = gitops.untracked_diff(self.repo, "new file.txt")
        self.assertIn("+hello", out)
        self.assertIn("new file mode", out)
        self.assertEqual(gitops.github_dir([".github/workflows/x.yml", ".GitHub/a", "src/.github/x", "a.py"]),
                         [".github/workflows/x.yml", ".GitHub/a"])

    def test_guard_snapshot_hashes_values_and_sees_hooks(self):
        git(self.repo, "remote", "set-url", "origin", "https://u:s3cret@github.com/o/r.git")
        before = gitops.guard_snapshot(self.repo)
        self.assertNotIn("s3cret", json.dumps(before))
        self.assertIn("remote.origin.url", before["config"])
        self.assertEqual(gitops.guard_changes(self.repo, before), [])
        hooks = os.path.join(self.repo, ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        with open(os.path.join(hooks, "pre-push"), "w") as f:
            f.write("#!/bin/sh\n")
        git(self.repo, "config", "url.x.insteadOf", "y")
        self.assertEqual(gitops.guard_changes(self.repo, before),
                         ["git config url.x.insteadof: added", "hook pre-push: added"])
        self.assertTrue(gitops.guard_changes(self.repo, {"oops": 1}))  # no snapshot is a change too

    def test_literal_pathspecs(self):
        for name in ("a.txt", "[ab].txt"):
            with open(os.path.join(self.repo, name), "w") as f:
                f.write("x\n")
        sha = gitops.commit(self.repo, ["[ab].txt"], "one file")
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=", sha).split(), ["[ab].txt"])
        self.assertEqual(gitops.changed_files(self.repo), ["a.txt"])

    def test_git_is_never_taken_from_the_repo(self):  # K1
        with mock.patch.object(gate, "resolve_program", return_value=None) as resolve:
            with self.assertRaises(run_pkg.RunError) as e:
                gitops.git(self.repo, "status")
        self.assertIn("`git` was not found on PATH", str(e.exception))
        self.assertIn(self.repo, resolve.call_args[0])

    def test_editor_is_resolved_outside_the_workspace(self):  # K1: never an editor.exe planted in the repo
        seen = []

        def call(argv):
            seen.append(argv)
            with open(argv[-1], "w", encoding="utf-8") as f:
                f.write("- one\n")
            return 0
        with mock.patch.dict(os.environ, {"VISUAL": "myedit --wait"}), \
                mock.patch.object(gate, "resolve_program", return_value="/abs/myedit") as resolve, \
                mock.patch.object(cli.subprocess, "call", side_effect=call):
            acs = cli._edit_acs(())
        self.assertEqual(seen[0][:2], ["/abs/myedit", "--wait"])
        self.assertEqual(resolve.call_args[0][0], "myedit")
        self.assertEqual([a.text for a in acs], ["one"])

    def test_claude_folder_excluded_in_any_case(self):
        for p in (".claude/x", ".Claude/x", "sub/.CLAUDE/y.md"):
            self.assertTrue(gitops.excluded(p), p)
        for p in ("claude.md", ".claude", "src/.claudeish/x"):
            self.assertFalse(gitops.excluded(p), p)


if __name__ == "__main__":
    main()
