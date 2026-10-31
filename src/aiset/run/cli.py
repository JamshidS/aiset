"""aiset run <issue>: fetch a GitHub issue, confirm its criteria, work it with the team, publish a draft PR.

  aiset run 123                      issue 123 of the repo you're in (its github.com `origin`)
  aiset run https://github.com/OWNER/NAME/issues/123
  aiset run 123 --repo OWNER/NAME    when the workspace has several repos
  aiset run 123 --dry-run            show the snapshot, criteria, branch and claude command; change nothing
  aiset run 123 --resume | --fresh   continue the last session on the board, or start a new board
  aiset run 123 --publish            go straight to gate 2 for an existing board

Gate 1: you confirm (or edit) the acceptance criteria before any agent starts; run.json records the start sha
and a hash snapshot of push-related git config and hooks. Gate 2: after the session, it checks run.json against
the repo, shows the push URL (it must be the issue's repo), the session's own commits (refused if they touch
.claude/ or carry an AI trailer, else asked about), the files and diffstat since the start, config/hook changes
(warned and asked about) and pr.md; only on `y` does aiset run commit, push and open a DRAFT pull request, with
git hooks off.
Agents in the session can't push or write to GitHub (deny rules and gate.py); the issue reaches them only as
the fenced, untrusted issue.md on the board. git, gh, claude and the editor are found on PATH outside the repo,
the workspace and the current folder (resolve_program).

Known limits (tripwires, not fixed in v1):
- (K6) run.json's gitGuard snapshot sits on the board, so an agent can recompute it after changing config/hooks.
- (K8) an agent can make a commit with gate 2's own message and put its sha in run.json; it then shows as
  "already committed by aiset run" (still checked for .claude/ and AI trailers).
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from types import SimpleNamespace

from . import AVOID, RunError, board, fetch, gitops, launch, program, publish
from .ac import extract_ac
from .issue_md import render_issue_md
from .names import board_slug, branch_name
from .refs import parse_issue_ref
from .sanitize import visible_text
from .datatypes import AcItem, IssueRef

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _show(text):
    """Untrusted text made safe for the terminal: no escape sequences that could rewrite what's on screen."""
    return CONTROL.sub(lambda m: f"<U+{ord(m.group(0)):04X}>", text or "")


def _interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question, choices, default="n"):
    """One letter from choices; Enter gives the default. Gates refuse to run without a terminal."""
    if not _interactive():
        raise RunError("this step needs your answer in a terminal; run aiset run from an interactive shell")
    while True:
        a = input(question).strip().lower()[:1] or default
        if a in choices:
            return a


def _parser():
    ap = argparse.ArgumentParser(prog="aiset run", description="Work a GitHub issue with the team; open a draft PR after your approval.")
    ap.add_argument("issue", help="123, #123, OWNER/NAME#123 or the issue URL")
    ap.add_argument("--repo", help="OWNER/NAME on github.com, when the issue is a bare number")
    ap.add_argument("--workspace", help="folder made by aiset-init, or its team name (default: the workspace you're in)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; write and launch nothing")
    ap.add_argument("--comments", action="store_true", help="include comments by the repo's owner, members and collaborators")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--fresh", action="store_true", help="start a new board and branch even if one exists")
    mode.add_argument("--resume", action="store_true", help="resume the last session on the existing board")
    mode.add_argument("--publish", action="store_true", help="skip the session: gate 2 for the existing board")
    ap.add_argument("--base", help="base branch of the PR (default: the repo's default branch)")
    ap.add_argument("--model", help="model for the PM session (default: the PM agent's)")
    return ap


def _workspace(arg):
    """The team workspace, settled the way server.py does it: --workspace first, before workspace.py is imported."""
    if arg:
        from .. import registry
        os.environ["AISET_WORKSPACE"] = registry.resolve(arg)
    from .. import workspace
    if not workspace.WORKSPACE or not os.path.isfile(workspace.ROSTER):
        raise RunError("no aiset team here. Run `aiset-init <folder>` first, or pass --workspace <folder|team>")
    from .. import providers
    roster = workspace.load_roster()
    return SimpleNamespace(path=workspace.WORKSPACE, boards=workspace.BOARDS, repos=workspace.roster_repos(),
                           provider=providers.name(roster), roster=roster)


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _target(ws, repo):
    """(local repo path, OWNER/NAME) the issue is worked in."""
    top = gitops.toplevel(ws.path)
    if top and _same(top, ws.path):
        candidates = [ws.path]
    else:
        candidates = [os.path.join(ws.path, r) for r in ws.repos if os.path.isdir(os.path.join(ws.path, r))]
        candidates = [c for c in candidates if gitops.toplevel(c)]
    if repo:
        for c in candidates:
            if (gitops.origin_repo(c) or "").lower() == repo.lower():
                return c, repo
        raise RunError(f"no repo in {ws.path} has {repo} on github.com as its origin")
    here = gitops.toplevel(os.getcwd())
    picked = [c for c in candidates if here and _same(c, here)] or (candidates if len(candidates) == 1 else [])
    if not picked:
        raise RunError("the workspace has several repos: run from inside one, or pass --repo OWNER/NAME")
    found = gitops.origin_repo(picked[0])
    if not found:
        raise RunError(f"the origin of {picked[0]} is not a github.com repo; pass --repo OWNER/NAME")
    return picked[0], found


def _edit_acs(acs):
    """Open the criteria in $VISUAL/$EDITOR; lines `AC-n: text` or `- text`, renumbered in order."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    fd, path = tempfile.mkstemp(suffix=".md", prefix="aiset-ac-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# One criterion per line (AC-n: text, or - text). Lines starting with # are ignored.\n")
            f.write("".join(f"{a.id}: {a.text}\n" for a in acs))
        cmd = shlex.split(editor, posix=os.name != "nt")
        if cmd and not os.path.isabs(cmd[0]):  # never an editor.exe planted in the repo or workspace
            cmd[0] = program(cmd[0], hint="; set VISUAL or EDITOR to a full path")
        subprocess.call([*cmd, path])
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().split("\n")
    finally:
        os.unlink(path)
    texts = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        texts.append(re.sub(r"^(?:AC-\d+\s*:|[-*+])\s*", "", line))
    return tuple(AcItem(f"AC-{i}", t, 0) for i, t in enumerate([t for t in texts if t], 1))


def _print_acs(acs):
    if not acs:
        print("  (no acceptance criteria found in the issue: add them with e, or the PM agrees them with you)")
    for a in acs:
        print(f"  {a.id}: {_show(a.text)}")


def _unique_branch(repo_path, name, fresh):
    if not gitops.branch_exists(repo_path, name):
        return name
    if not fresh:
        raise RunError(f"branch {name} already exists and no board of this issue owns it; --fresh picks a new name")
    n = 2
    while gitops.branch_exists(repo_path, f"{name}-{n}"):
        n += 1
    return f"{name}-{n}"


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except RunError as e:
        print(f"aiset run: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naiset run: stopped. The board and branch stay as they are; run the same command to continue.")
        return 130


def run(args):
    ws = _workspace(args.workspace)
    AVOID[:] = [ws.path]  # programs are never taken from inside the workspace
    try:
        ref = parse_issue_ref(args.issue, default_repo=args.repo)
    except ValueError as e:
        raise RunError(str(e)) from None
    repo_path, repo = _target(ws, ref.repo)
    ref = IssueRef(repo, ref.number)
    found = board.find(ws.boards, repo, ref.number)
    if args.dry_run and (args.publish or (found and not args.fresh and found[1].get("phase") in ("ready", "published"))):
        print(f"Board {found[0] if found else '(none)'}: phase {found[1].get('phase') if found else '-'}. "
              "Dry run: would go to gate 2. Nothing changed.")
        return 0
    if args.publish:
        if not found:
            raise RunError(f"no aiset run board for {repo}#{ref.number}; start one with: aiset run {args.issue}")
        return gate2(ws, *found, target=(repo_path, repo))
    if found and not args.fresh:
        bdir, state = found
        phase = state.get("phase")
        if phase == "published":
            print(f"Already published: {state.get('pr')}  (--fresh starts over on a new board and branch)")
            return 0
        if phase == "ready":
            return gate2(ws, bdir, state, target=(repo_path, repo))
        print(f"Board {bdir} exists (phase {phase}).")
        if args.dry_run:
            print("Dry run: would offer to resume its last session. Nothing changed.")
            return 0
        _warn_if_edited(state)
        choice = "r" if args.resume else ask(
            "[r]esume the last session / [n]ew session on this board / [f]resh board / [q]uit? ", "rnfq", "q")
        if choice == "q":
            return 0
        if choice != "f":
            return session(ws, bdir, state, resume=choice == "r" and bool(state.get("sessions")), model=args.model,
                           target=(repo_path, repo))
    elif args.resume:
        raise RunError(f"no board for {repo}#{ref.number} to resume")
    return start(ws, args, repo_path, ref)


def _warn_if_edited(state):
    issue = state.get("issue") or {}
    try:
        now = fetch.issue(issue["repo"], issue["number"])
    except (RunError, KeyError, ValueError):
        return  # offline or gh trouble: resuming doesn't need the issue
    if now.get("updatedAt") != issue.get("updatedAt"):
        print(f"Note: the issue was edited since the snapshot ({issue.get('updatedAt')} -> {now.get('updatedAt')}). "
              "issue.md and the criteria are the old version; --fresh re-reads it.")


def start(ws, args, repo_path, ref):
    data = fetch.issue(ref.repo, ref.number, comments=args.comments)
    base = args.base or fetch.default_branch(ref.repo)
    fetched = board.now_iso()
    snap = render_issue_md(data, repo=ref.repo, fetched_at=fetched, include_comments=args.comments)
    title = " ".join(visible_text(data.get("title") or "").text.split())
    body = visible_text((data.get("body") or "").replace("\r\n", "\n"))
    found = extract_ac(body.text)
    acs = found.items
    bid = board.new_id(ws.boards, board_slug(ref.number, title))
    bdir = os.path.join(ws.boards, bid)
    branch = _unique_branch(repo_path, branch_name(ref.number, title), args.fresh)
    dirty = gitops.changed_files(repo_path)

    print(f"Issue {ref.repo}#{ref.number}: {_show(title)}\n  {data.get('url')}")
    print(f"Board:  {bdir}\nBranch: {branch} (from {gitops.current_branch(repo_path) or 'HEAD'}, PR into {base})")
    print("Acceptance criteria:")
    _print_acs(acs)
    warnings = [f"{v} {k.replace('_', ' ')}" for k, v in sorted((snap.counts or body.counts).items())]
    if warnings:
        print("Hidden content in the issue: " + ", ".join(warnings) + " (shown as <U+...> in issue.md)")
    if snap.truncated:
        print("Cut to size: " + ", ".join(snap.truncated))
    if found.dropped:
        print(f"{found.dropped} more criteria were dropped (limit {len(acs)})")
    provider = getattr(ws, "provider", "claude")
    for w in (launch.permission_warnings(ws.path) if provider == "claude" else []):
        print("Warning: " + w)
    if args.dry_run:
        if dirty:
            print("The working tree isn't clean; a real run would stop here: " + ", ".join(dirty[:10]))
        print("Would write: " + ", ".join(os.path.join(bdir, n) for n in ("issue.md", "brief.md", "log.md", "run.json")))
        if provider == "codex":
            from . import codex_launch
            argv = codex_launch.argv(bdir, model=args.model)
        else:
            argv = launch.claude_argv(bdir, "<new session id>", model=args.model, name=f"aiset run #{ref.number}")
        print("Would run in " + ws.path + ":\n  " + subprocess.list2cmdline(argv))
        print("Dry run: nothing written, nothing launched.")
        return 0
    if dirty:
        raise RunError(f"{repo_path} has uncommitted changes; commit or stash them first:\n  " + "\n  ".join(dirty[:20]))
    launch.check_path(bdir)
    if provider == "codex":
        from . import codex_launch
        codex_launch.ensure_hooks(ws.path)

    while True:  # gate 1
        a = ask("Work on these criteria? [y/N/e(dit)] ", "yne")
        if a == "n":
            print("Nothing started.")
            return 0
        if a == "y":
            break
        acs = _edit_acs(acs)
        _print_acs(acs)

    os.makedirs(bdir)
    board.write_text(bdir, "issue.md", snap.markdown)
    board.write_text(bdir, "brief.md", board.brief_md(ref.repo, ref.number, title, data.get("url"), acs, board.now_iso()))
    board.write_text(bdir, "log.md", "")
    state = {"version": 1, "provider": provider,
             "issue": {"repo": ref.repo, "number": ref.number, "url": data.get("url"),
                       "updatedAt": data.get("updatedAt"), "bodySha256": snap.body_sha256},
             "title": title, "repoPath": repo_path.replace("\\", "/"), "base": base,
             "startBranch": gitops.current_branch(repo_path), "startSha": gitops.head(repo_path),
             "gitGuard": gitops.guard_snapshot(repo_path), "branch": branch, "sessions": [],
             "phase": "confirmed", "pr": None, "commit": None, "pushed": False, "createdAt": board.now_iso()}
    board.write_run(bdir, state)
    board.log(bdir, f"gate 1 approved: {len(acs)} criteria, board opened for {ref.repo}#{ref.number}")
    gitops.create_branch(repo_path, branch)
    return session(ws, bdir, state, resume=False, model=args.model, target=(repo_path, ref.repo))


def _verify(state, target):
    """run.json is on the board, where agents can write: check what gate 2 acts on against the repo itself.
    target: (repo path, OWNER/NAME) found from the workspace and the issue ref, not from run.json."""
    issue = state.get("issue") or {}
    repo_path, num, branch, base = state.get("repoPath"), issue.get("number"), state.get("branch"), state.get("base")
    if not isinstance(repo_path, str) or not isinstance(num, int) or not isinstance(branch, str) or not isinstance(base, str):
        raise RunError("run.json is missing repoPath, issue.number, branch or base")
    if target:
        if not _same(repo_path, target[0]):
            raise RunError(f"run.json says the repo is {repo_path}, but this issue's repo is {target[0]}")
        if (issue.get("repo") or "").lower() != target[1].lower():
            raise RunError(f"run.json says the issue is in {issue.get('repo')}, not {target[1]}")
    origin = gitops.origin_repo(repo_path)
    if not origin or origin.lower() != (issue.get("repo") or "").lower():
        raise RunError(f"the origin of {repo_path} is {origin or 'not a github.com repo'}, not {issue.get('repo')}")
    if not re.match(rf"^aiset/issue-{num}(?:-[a-z0-9]+)*$", branch):
        raise RunError(f"run.json names branch {branch!r}; aiset run only publishes its own aiset/issue-{num}-... branch")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", base) or ".." in base or base == branch:
        raise RunError(f"run.json names an odd base branch {base!r}")


def session(ws, bdir, state, resume, model=None, target=None):
    _verify(state, target)
    provider = getattr(ws, "provider", "claude")
    if state.get("provider", "claude") != provider:
        raise RunError("this board belongs to another provider; switch the team back or use --fresh")
    if provider == "codex":
        from . import codex_launch
        codex_launch.ensure_hooks(ws.path)
    repo_path, branch = state["repoPath"], state["branch"]
    launch.check_path(bdir)
    if gitops.current_branch(repo_path) != branch:
        if gitops.changed_files(repo_path):
            raise RunError(f"{repo_path} is on another branch with uncommitted changes; switch to {branch} yourself")
        if gitops.branch_exists(repo_path, branch):
            gitops.switch(repo_path, branch)
        else:
            gitops.create_branch(repo_path, branch)
    sid = state["sessions"][-1] if resume else (None if provider == "codex" else str(uuid.uuid4()))
    if not resume and sid:
        state["sessions"].append(sid)
    state["phase"] = "working"
    board.write_run(bdir, state)
    if provider == "codex":
        from ..providers.codex import options
        member = next((m for m in ws.roster.get("members", []) if m["id"] == "pm"), {"id": "pm"})
        pinned_model, effort = options(member)
        argv = codex_launch.argv(bdir, session_id=sid, model=model or pinned_model, effort=effort)
    else:
        settings = launch.write_gate_settings(bdir)
        for w in launch.permission_warnings(ws.path):
            print("Warning: " + w)
        argv = launch.claude_argv(bdir, sid, resume=resume, model=model, settings_path=settings,
                                  name=f"aiset run #{state['issue']['number']}")
    board.log(bdir, f"{'resumed' if resume else 'started'} PM session {sid}")
    print(f"Starting the PM ({provider}, {'resume ' if resume else ''}session {sid or 'new'})." +
          (" Type /exit when the PM says the work is ready." if provider == "claude" else " Progress appears below."))
    if provider == "codex":
        def remember(native_id):
            nonlocal sid
            sid = native_id
            if native_id not in state["sessions"]:
                state["sessions"].append(native_id)
            board.write_run(bdir, state)
            board.log(bdir, f"Codex session {native_id}")
        code = codex_launch.run(argv, ws.path, bdir, remember)
    else:
        code = launch.run_claude(argv, ws.path)
    board.log(bdir, f"PM session {sid} ended (exit code {code})")
    if board.brief_status(bdir) != "ready" or (provider == "codex" and code):
        print(f"The session ended (exit code {code}); it failed or brief.md doesn't say STATUS: ready.")
        if ask("Go to the publish step anyway? [y/N] ", "yn") != "y":
            print(f"Nothing published. Continue later with: aiset run {state['issue']['number']} --resume")
            return 0
    state["phase"] = "ready"
    board.write_run(bdir, state)
    return gate2(ws, bdir, state, target=target)


def _own_commit(state, commits, message):
    """The sha of gate 2's own earlier commit (a retry after a failed push), when run.json's claim checks out:
    it is HEAD, it is after the start sha, and its message is the one gate 2 writes."""
    sha = state.get("commit")
    last = commits[-1] if commits else None
    if last and sha == last["sha"] and last["body"].strip() == message and not last["claude"] and not last["trailer"]:
        return sha
    return None


def _print_commits(commits):
    for c in commits:
        flags = (["touches " + ", ".join(c["claude"][:5])] if c["claude"] else []) + (["AI trailer"] if c["trailer"] else [])
        print(f"  {c['sha'][:10]} {_show(c['subject'])[:72]}" + (f"   <-- {'; '.join(flags)}" if flags else ""))
        for f in c["files"][:15]:
            print(f"      {_show(f)}")
        if len(c["files"]) > 15:
            print(f"      ... {len(c['files']) - 15} more")


def _print_earlier(earlier, more):
    for c in earlier[:20]:
        flags = (["touches " + ", ".join(c["claude"][:5])] if c["claude"] else []) + (["AI trailer"] if c["trailer"] else [])
        print(f"  {c['sha'][:10]} {_show(c['subject'])[:72]}" + (f"   <-- {'; '.join(flags)}" if flags else ""))
    hidden = [c for c in earlier[20:] if c["claude"] or c["trailer"]]
    if len(earlier) > 20:
        print(f"  ... {len(earlier) - 20} more" + (f", {len(hidden)} of them marked:" if hidden else ""))
    for c in hidden[:20]:
        print(f"  {c['sha'][:10]} {_show(c['subject'])[:72]}   <-- marked")
    if more:
        print(f"  ... and {more} older commit(s) not checked (more than {gitops.MAX_COMMITS})")


def gate2(ws, bdir, state, target=None):
    _verify(state, target)
    repo_path, branch, num, repo = state["repoPath"], state["branch"], state["issue"]["number"], state["issue"]["repo"]
    if gitops.current_branch(repo_path) != branch:
        raise RunError(f"{repo_path} is not on {branch}; switch to it to publish")
    start = state.get("startSha")
    if not gitops.is_ancestor(repo_path, start):
        raise RunError(f"run.json's start commit {start!r} is not an ancestor of {branch}'s HEAD; "
                       "inspect the branch with git log and start over with --fresh")
    title = publish.safe_title(state.get("title") or f"Issue {num}")
    message = f"{title} (#{num})"
    commits = gitops.commits_since(repo_path, start)
    own = _own_commit(state, commits, message)
    agent_commits = [c for c in commits if c["sha"] != own]
    files = gitops.changed_files(repo_path)
    if not files and not commits:
        raise RunError(f"no changes in {repo_path} since gate 1 (no new commits, nothing uncommitted) to publish")
    bad = [c for c in agent_commits if c["claude"] or c["trailer"]]
    if bad:
        print(f"\nCommits made in the session since gate 1 ({start[:10]}):")
        _print_commits(agent_commits)
        raise RunError(f"{len(bad)} commit(s) on {branch} touch .claude/ or carry an AI trailer (Co-Authored-By ...), "
                       "and a push would publish them. Rewrite them first, e.g. `git reset --soft "
                       f"{start[:10]}` keeps the changes uncommitted, then: aiset run {num} --publish")
    urls = gitops.push_urls(repo_path)
    wrong = [u for u in urls if (gitops.github_repo(u) or "").lower() != repo.lower()]
    if not urls or wrong:
        shown_urls = ", ".join(gitops.shown_url(u) for u in (wrong or urls)) or "(nothing)"
        raise RunError(f"origin pushes to {_show(shown_urls)}, not only github.com/{repo}. Check `git remote -v` "
                       "and the url.*.insteadOf / pushurl config before publishing")
    changes = gitops.guard_changes(repo_path, state.get("gitGuard"))
    pr_path, text = publish.render(bdir, state)

    since = gitops.files_since(repo_path, start)
    shown = sorted(set(since) | set(files))
    print(f"\nRepo {repo_path}, branch {branch}")
    print("\n".join(f"  push to {_show(gitops.shown_url(u))}" for u in urls))
    print(f"  draft PR into {_show(state['base'])} on {repo}\n  title: {_show(message)}")
    if agent_commits:
        print(f"\nCommits made in the session since gate 1 ({start[:10]}), pushed as they are ({len(agent_commits)}):")
        _print_commits(agent_commits)
    if own:
        print(f"\nAlready committed by aiset run: {own[:10]}")
    if files:
        print(f"\nUncommitted files to commit on {branch} ({len(files)}):")
        print("\n".join("  " + _show(f) for f in files))
    print(f"\nAll changes since gate 1 ({len(shown)} files):")
    print(_show(gitops.diffstat(repo_path, shown, since=start)))
    held = gitops.left_out(repo_path)
    if held:
        print(f"Left out of the commit (under .claude/ or .codex/): {len(held)} files")
    earlier, more = gitops.unpushed(repo_path, skip=[c["sha"] for c in commits])
    marked = [c for c in earlier if c["claude"] or c["trailer"]]
    if earlier or more:
        print(f"\nThe push also sends {len(earlier) + more} earlier commit(s) that origin doesn't have yet:")
        _print_earlier(earlier, more)
    workflows = gitops.github_dir(shown + [f for c in commits + earlier for f in c["files"]])
    if workflows:
        print("\n!! .github/ CHANGED: workflows and actions there run on GitHub with the repo's tokens once the PR")
        print("!! is opened. Read these yourself before you say y:")
        print("\n".join("!!   " + _show(f) for f in sorted(set(workflows))))
    print(f"\nPR description: {pr_path}\n" + "\n".join(_show(text).split("\n")[:25]) + "\n  ...")
    if changes:
        print("\n!! WARNING: git config or hooks changed since gate 1. They can redirect a push or run code when you")
        print("!! (or this step) run git. aiset run commits and pushes with hooks off, but check these yourself:")
        print("\n".join("!!   " + _show(c) for c in changes))
        if ask("Publish anyway? [y/N] ", "yn") != "y":
            board.log(bdir, "gate 2 stopped: git config or hooks changed since gate 1")
            print(f"Nothing committed or pushed. Publish later with: aiset run {num} --publish")
            return 0
    if agent_commits and ask(f"The session made {len(agent_commits)} commit(s) of its own (above). Push them as they "
                             "are? [y/N] ", "yn") != "y":
        board.log(bdir, "gate 2 stopped: the session's own commits were not accepted")
        print(f"Nothing committed or pushed. Rewrite them (git reset --soft {start[:10]}), then: aiset run {num} --publish")
        return 0
    if (marked or more) and ask(
            f"{len(marked)} earlier commit(s) touch .claude/ or carry an AI trailer" + (f" and {more} were not checked"
            if more else "") + " (marked above); the push sends them too. Push them anyway? [y/N] ", "yn") != "y":
        board.log(bdir, "gate 2 stopped: earlier unpushed commits were not accepted")
        print(f"Nothing committed or pushed. Check them with git log, then: aiset run {num} --publish")
        return 0
    while True:  # gate 2
        a = ask(f"Commit, push {branch} and open a DRAFT PR into {state['base']} on {repo}? [y/N/v(iew)/d(iff)] ",
                "ynvd")
        if a == "v":
            print(_show(text))
        elif a == "d":
            print(_show(gitops.diff(repo_path, shown, since=start)))
        else:
            break
    if a != "y":
        board.log(bdir, "gate 2 declined")
        print(f"Nothing committed or pushed. Publish later with: aiset run {num} --publish")
        return 0
    board.log(bdir, "gate 2 approved")
    if files:
        state["commit"] = gitops.commit(repo_path, files, message)
        board.write_run(bdir, state)
    gitops.push(repo_path, branch)  # always: run.json's `pushed` is a note, not a reason to skip
    state["pushed"] = True
    board.write_run(bdir, state)
    state["pr"] = gitops.create_pr(repo_path, repo, state["base"], branch, title, pr_path)
    state["phase"] = "published"
    board.write_run(bdir, state)
    board.log(bdir, f"draft PR opened: {state['pr']}")
    print(f"Draft PR: {state['pr']}")
    return 0
