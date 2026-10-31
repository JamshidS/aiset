"""git and `gh pr create` for `aiset run`. The only code that commits, pushes or opens the PR, and only cli.py's
gate 2 calls those three. Everything under a `.claude/` folder (the team folder and the board) stays out of the
commit.

Gate 2 trusts neither the session nor run.json: it lists the commits made since gate 1's start sha (refusing
ones that touch `.claude/` or carry an AI trailer), shows where `origin` really pushes, and compares the git
config keys and hooks that can redirect a push or run code with gate 1's snapshot. Its own commit and push run
with hooks off (an empty core.hooksPath and --no-verify). Every git call here uses --literal-pathspecs (a file
named `:(glob)*` is a file name) and core.fsmonitor off (no configured program runs on `git status`); diffs use
--no-ext-diff --no-textconv. Only built-in git commands are called, and git never lets an alias shadow one.
git itself is found by resolve_program (never in the repo, the workspace or the current folder).
"""
import hashlib
import os
import re
import subprocess
import tempfile

from . import RunError, program
from .fetch import gh
from .gate import RISKY_CONFIG

EXCLUDED = ".claude"
AI_TRAILER = re.compile(r"^\s*(?:co-authored-by|generated-by|assisted-by|ai-assisted|ai-generated|made-with)\s*:"
                        r"|noreply@anthropic\.com|generated with \[?claude", re.I | re.M)
MAX_COMMITS = 200
SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def git(repo, *args, input=None, check=True):
    exe = program("git", repo)
    p = subprocess.run([exe, "-C", repo, "--literal-pathspecs", "-c", "core.fsmonitor=false", *args], input=input,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        sub = next((a for a in args if not a.startswith("-") and "=" not in a), args[0])
        raise RunError(f"git {sub} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout if check else p


def toplevel(path):
    p = git(path, "rev-parse", "--show-toplevel", check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def github_repo(url):
    """owner/name from a github.com remote URL (https, ssh or scp form), else None."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
                 r"([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$", (url or "").strip())
    return m.group(1) if m else None


def origin_repo(repo):
    p = git(repo, "remote", "get-url", "origin", check=False)
    return github_repo(p.stdout) if p.returncode == 0 else None


def push_urls(repo):
    """Every URL `git push origin` sends to (a push goes to all of origin's pushurls): pushurl, insteadOf and
    pushInsteadOf applied. [] without origin."""
    p = git(repo, "remote", "get-url", "--push", "--all", "origin", check=False)
    return [u.strip() for u in p.stdout.splitlines() if u.strip()] if p.returncode == 0 else []


def shown_url(url):
    """A remote URL for the terminal, user:password@ masked up to the last @ (a password can hold an @)."""
    return re.sub(r"(://)[^/]*@", r"\1***@", url or "")


def current_branch(repo):
    return git(repo, "branch", "--show-current").strip()


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def is_ancestor(repo, old, new="HEAD"):
    return bool(SHA.match(old or "")) and git(repo, "merge-base", "--is-ancestor", old, new, check=False).returncode == 0


def branch_exists(repo, name):
    return git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + name, check=False).returncode == 0


def excluded(path):
    """True for a path with a `.claude` folder in it, in any case (`.Claude/` is the same folder on Windows)."""
    return any(p.lower() in (EXCLUDED, ".codex") for p in path.replace("\\", "/").split("/")[:-1])


def _status_paths(repo):
    out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries, paths, i = out.split("\0"), [], 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        paths.append(e[3:])
        if e[0] in "RC":  # a rename or copy: the next entry is the source path
            paths.append(entries[i])
            i += 1
    return {p for p in paths if p}


def changed_files(repo):
    """Paths with uncommitted changes (tracked or untracked), relative to the repo, `.claude/` left out."""
    return sorted(p for p in _status_paths(repo) if not excluded(p))


def left_out(repo):
    """Uncommitted paths under a `.claude/` folder: never committed, listed at gate 2 so they aren't a surprise."""
    return sorted(p for p in _status_paths(repo) if excluded(p))


def create_branch(repo, name):
    git(repo, "switch", "-c", name)


def switch(repo, name):
    git(repo, "switch", name)


def files_since(repo, start):
    """Tracked files that differ between `start` and the working tree (commits and uncommitted changes)."""
    out = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", start)
    return sorted(p for p in set(out.split("\0")) - {""} if not excluded(p))


def diffstat(repo, files, since="HEAD"):
    tracked = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--stat", since, "--", *files).rstrip() if files else ""
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", *files).split("\0") if files else []
    return "\n".join([tracked] + [f" {u} (new file)" for u in untracked if u]).strip()


def untracked_diff(repo, path):
    """A new, untracked file as a diff against the null device, without touching the index."""
    p = git(repo, "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--", os.devnull, path, check=False)
    if p.returncode not in (0, 1):
        return f"new file (could not show it: {(p.stderr or '').strip()}): {path}\n"
    return p.stdout or f"new file (empty): {path}\n"


def diff(repo, files, since="HEAD"):
    if not files:
        return ""
    text = git(repo, "diff", "--no-ext-diff", "--no-textconv", since, "--", *files)
    untracked = [u for u in git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", *files).split("\0") if u]
    return text + "".join("\n" + untracked_diff(repo, u) for u in untracked)


def github_dir(files):
    """The paths under the top-level `.github/` folder (workflows and actions run on GitHub), in any case."""
    return [f for f in files if f.replace("\\", "/").lower().startswith(".github/")]


def commit_info(repo, sha):
    """{sha, subject, files, body, claude, trailer} of one commit: its files (against every parent of a merge),
    the ones under a .claude/ folder, and whether its message carries an AI trailer."""
    files = git(repo, "diff-tree", "-r", "-m", "--root", "--no-renames", "--no-commit-id", "--name-only", "-z",
                sha).split("\0")
    body = git(repo, "log", "-1", "--format=%B", sha)
    files = sorted(set(files) - {""})
    return {"sha": sha, "subject": (body.strip().split("\n") or [""])[0], "files": files, "body": body,
            "claude": [f for f in files if excluded(f)], "trailer": bool(AI_TRAILER.search(body))}


def commits_since(repo, start):
    """[{sha, subject, files, claude, trailer}] oldest first: the commits on HEAD after `start`."""
    shas = git(repo, "rev-list", "--reverse", f"{start}..HEAD").split()
    if len(shas) > MAX_COMMITS:
        raise RunError(f"{len(shas)} commits since gate 1; more than {MAX_COMMITS} is not something to publish blind")
    return [commit_info(repo, sha) for sha in shas]


def unpushed(repo, skip=()):
    """(commits, more): HEAD's commits that no origin ref has (everything a push of this branch sends) except
    `skip`, newest first, each as commit_info. At most MAX_COMMITS are checked; `more` counts the rest."""
    out = git(repo, "rev-list", "HEAD", "--not", "--remotes=origin", check=False)
    skip = set(skip)
    shas = [s for s in out.stdout.split() if s not in skip] if out.returncode == 0 else []
    return [commit_info(repo, s) for s in shas[:MAX_COMMITS]], max(0, len(shas) - MAX_COMMITS)


def _hooks_dir(repo):
    d = git(repo, "rev-parse", "--git-path", "hooks").strip()
    return d if os.path.isabs(d) else os.path.join(repo, d)


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def guard_snapshot(repo):
    """Hashes of the git config keys that can redirect a push or run code (gate.RISKY_CONFIG, every config level)
    and of the active hooks. Values are hashed, not stored: remote URLs can hold tokens."""
    config = {}
    for entry in git(repo, "config", "--list", "-z").split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        if RISKY_CONFIG.match(key.lower()):
            config.setdefault(key, []).append(value)
    hooks, d = {}, _hooks_dir(repo)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(d, name)
        if name.endswith(".sample") or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                hooks[name] = _sha(f.read())
        except OSError:
            hooks[name] = "unreadable"
    return {"config": {k: _sha("\0".join(v)) for k, v in config.items()}, "hooks": hooks}


def guard_changes(repo, before):
    """What changed since gate 1's snapshot, as lines for the terminal; [] when nothing did."""
    if not isinstance(before, dict) or not isinstance(before.get("config"), dict) or not isinstance(before.get("hooks"), dict):
        return ["no gate 1 snapshot of git config and hooks in run.json to compare with"]
    now, out = guard_snapshot(repo), []
    for kind, label in (("config", "git config"), ("hooks", "hook")):
        old, new = before[kind], now[kind]
        for k in sorted(set(old) | set(new)):
            if k not in old:
                out.append(f"{label} {k}: added")
            elif k not in new:
                out.append(f"{label} {k}: removed")
            elif old[k] != new[k]:
                out.append(f"{label} {k}: changed")
    return out


def _no_hooks(tmp):
    return ["-c", f"core.hooksPath={tmp.replace(os.sep, '/')}"]


def commit(repo, files, message):
    """Stage exactly `files` and commit them with hooks off; returns the new commit sha. No trailer is added."""
    if not files:
        raise RunError("nothing to commit")
    git(repo, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input="\0".join(files))
    staged = set(git(repo, "diff", "--cached", "--name-only", "-z").split("\0")) - {""}
    extra = sorted(p for p in staged if p not in files)
    if extra:
        raise RunError("the index has changes outside the approved list: " + ", ".join(extra))
    with tempfile.TemporaryDirectory(prefix="aiset-nohooks-") as tmp:
        git(repo, *_no_hooks(tmp), "commit", "--no-verify", "-m", message)
    return head(repo)


def push(repo, branch):
    """Push the branch to origin with hooks off. cli.py checks the push URL first."""
    with tempfile.TemporaryDirectory(prefix="aiset-nohooks-") as tmp:
        git(repo, *_no_hooks(tmp), "push", "--no-verify", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}")


def create_pr(repo, github_repo_name, base, branch, title, body_file):
    """Opens the draft PR; returns its URL. `--opt=value` so a value starting with `-` stays a value."""
    out = gh(["pr", "create", "--draft", f"--repo={github_repo_name}", f"--base={base}", f"--head={branch}",
              f"--title={title}", f"--body-file={body_file}"], cwd=repo)
    urls = re.findall(r"https://\S+/pull/\d+", out)
    return urls[-1] if urls else out.strip()
