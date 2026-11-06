# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Starting the office on a busy or unavailable port now prints a concise message with a `--port 0` hint instead of a traceback (#125).

### Added

- Architecture view: Go services. Web frameworks (Gin, Echo, Chi, Fiber, Gorilla Mux, Iris) and databases, Redis, RabbitMQ and Kafka clients are read from the direct requirements in `go.mod`, for any major version (#17).
- `teammap outline <file>` prints indexed imports, symbol signatures, line ranges, sizes and routes; the long-read nudge points agents to it.
- `AISET_CLAUDE_HOME` selects an alternate Claude Code data root for transcripts and, without a workspace, the team and agents folders.
- `aiset run <issue>` (preview) works a GitHub issue with the team and opens a draft pull request after you approve it. It fetches the issue with `gh`, writes a board with an untrusted `issue.md` snapshot and numbered acceptance criteria, asks you to confirm them (gate 1), creates a branch and starts the PM in your terminal. When the PM is done, it shows where `origin` really pushes, the session's own commits, the files and diffstat since gate 1, any git config or hook changes since then, and the PR description, and only on your `y` commits, pushes (git hooks off) and runs `gh pr create --draft` (gate 2). Session commits that touch `.claude/` or carry an AI trailer are refused. `--dry-run`, `--resume`, `--fresh` and `--publish` cover previewing, interrupted sessions and publishing later. Agents in the session can't push or write to GitHub: deny rules and a session-only PreToolUse hook block it, subagents included. Several parts are simple stubs for now (acceptance criteria from task lists only, no hidden-character warnings, no snapshot size limits, a dollars-only cost line); see the user guide.
- `teammap run -- <command>` runs a test command from the current directory, captures stdout and stderr together, saves the full output under `graph/research/runs/`, and prints parsed test counts, grouped failures with log line ranges, or the last 30 lines when a command fails without parseable failures. It streams output to disk, handles interruption, and returns shell-style exit codes.

### Changed

- `teammap arch path` and `teammap arch impact` print usage and exit with status 2 when required arguments are missing.
- The `aiset` command is now `aiset.cli:main`: `aiset run …` goes to the new command and everything else starts the office as before. Reinstall (`pip install -e .` or `pipx reinstall aiset-cli`) to pick up the new entry point.
- Agent templates: the PM writes acceptance criteria as `AC-1`, `AC-2`, … (#58); engineers and QA report one `AC-n:` line per criterion and label claims `[V]` verified, `[S]` assumed, `[?]` open (#61). The PM has a short section on working an issue for `aiset run`.
- The tester and engineer templates use `teammap run`, and the nudge hook recommends it when a known test runner is piped into `tail` or `head`.

### Fixed

- Architecture view: pyproject dependencies with extras (e.g. `fastapi[standard]`) are recognized and mapped to their framework and kind (#126).

## [0.2.0] - 2026-10-28

### Added

- `aiset --version`, `aiset-init --version` and `teammap --version` print the package version without initializing a workspace.
- Engineers, QA and the integration checker get a PreToolUse hook (`nudge.py`) that suggests the matching `teammap` command when they grep for a symbol or route, or read a long source file whole. It never blocks a call, makes each suggestion once per session, and runs only while those agents work. `aiset-init --no-nudge` leaves it out. The task detail counts each agent's `teammap` calls (🗺️) next to its reads and searches.

### Changed

- `teammap code` and `teammap symbol` now list cross-file candidates (exit 3), accept `--path` to select a file, show same-name alternatives for qualified matches, and return exit 4 when no symbol matches.

### Security

- Hand-edited `roster.json` values can no longer inject markup into the office. A role starting with `<` was inserted into the isometric nameplate as raw HTML, and member and helper colours went into `style` attributes unchecked. Roles are now always escaped, and colours that aren't `#hex` fall back to grey when the roster is loaded.
- The office now refuses read requests whose `Host` isn't its own address. Before, only writes were checked, so a web page using DNS rebinding could read the dashboard's data, including transcript excerpts.
- Task ids in `/api/board` and `/api/task` must be plain task names. `..` previously resolved to the team folder.

### Fixed

- Cost uses each older model's own price (Opus 4.6–4.8, Sonnet 4.6, Haiku 4.5) instead of falling back to the current generation's, which put Haiku 4.5 about ten times too low. Haiku 5.5 prompts over 100K tokens use the long-prompt rate.
- The office no longer prints a traceback when the browser closes a request before the reply is written (a reload, leaving the page or closing the tab while `/api/brain` is still building). Windows reported it as `ConnectionAbortedError` (WinError 10053), Linux and macOS as `BrokenPipeError`.
- Team files, workspace registries, launchers and briefs use atomic replacement, so failed writes no longer leave them truncated.
- Brain now consumes a `#live=` link after it follows the agent, so toggling follow off or panning the graph is not undone by the next live refresh.
- The Brain reads code graphs and indexes for a single-repository unit named `.`.
- `teammap` expands `api_route` decorators into their declared HTTP methods (GET when omitted), deduplicates method names, records unreadable methods as unknown, and refreshes older cached indexes. Failure reports keep indexed project `internal/` frames while excluding older Node runtime frames. Unknown `--repo` names and invalid or missing numeric option values now produce a usage error (exit 2) before indexing.
- `aiset-init` supports single-package repositories whose manifest is at the root, with one root engineer and root-relative checks when no service subfolders are found. Member ids are allocated uniquely (`root`, then `root-2`, ...; a service folder named like `pm` or `tester` no longer overwrites that agent), and the root engineer is scoped once service folders appear.
- Malformed input and invalid request shapes no longer crash the office server handler: non-object bodies and malformed roster/member edits on `/api/settings` return a clean 400 response, non-float `since` query parameters on `/api/brain/live` fall back to 1800, and `Content-Length` is clamped to prevent negative-length blocking reads. Invalid roster shapes without members or valid agent records are rejected in `reload_roster` while preserving the last good roster.
- A team file that isn't UTF-8 (PowerShell 5.1 writes the ANSI codepage by default) no longer breaks the office: Markdown and text files are read with a replacement character for the bad bytes, and a name sync keeps them as they were. JSON files saved with a UTF-8 BOM (`roster.json`, `settings.json`, the workspace registry, a repo's `package.json`) load as usual instead of failing or silently falling back to defaults.
- Architecture scans use forward-slash repository paths on Windows, so workflow files and DAG folders are detected and test files stay out of infrastructure evidence. `teammap` preserves non-ASCII Git filenames, and `teammap file` resolves importers through the same Python/TypeScript resolver as the code graph, including tsconfig aliases.
- Sessions are matched to their workspace the way Claude Code names project folders. `slug()` lowercased before replacing, and `"İ".lower()` is two characters, so a workspace under a non-ASCII path (or one with an emoji in it) never matched and the office showed no sessions at all. Sibling folders no longer leak in either: with workspace `C:\work\app`, sessions from `C:\work\apples` and `C:\work\app2` used to appear on the floor and in the token and cost totals.
- The office footer now suggests the `<team>-pm` launcher when `aiset-init` writes it and the command is on `PATH`; `--no-launcher` and unavailable launchers keep the roster's existing PM command, and so does a `pmCommand` you edited in Settings.
- Code-scanning findings: explicit returns, documented intentional `except` blocks, unused variables in the dashboard, and a `Map` for the route cache. The `<team>-pm` launcher is now made executable for its owner only.
- In a monorepo, a root package with a manifest keeps an owner when subfolders have manifests too: the root unit (`.`) is scanned alongside the service folders instead of only when no service folder was found, and the root engineer's duty notes the service folders belong to their own engineers (omitted when the same engineer owns the root and all service folders, `--group stack`), and the root unit's code index leaves the service folders out, so `teammap` and research list each file under exactly one unit. The init header and automatic stack grouping count service folders separately from the root unit (a root-only repository still shows one service).

## [0.1.0] - 2026-10-18

### Added

- First public release, on PyPI as `aiset-cli`.
- `aiset-init`: scans a folder of repositories or a monorepo, detects each stack, and generates a team in `<folder>/.claude/`: a PM, one engineer per repo or stack, a QA engineer and an integration checker, with knowledge brief stubs.
- `<team>-pm` launcher, written by `aiset-init`, that starts the PM from any folder (`.cmd` on Windows).
- Workspace registry (`~/.aiset/workspaces.json`): the office finds the most recent team when started outside a workspace, and `aiset --workspace` accepts a team name.
- The office (`aiset`), a local dashboard:
  - **Office**: live view of agents, assignments, task boards, models, tokens and estimated cost.
  - **Brain**: graph of agents, briefs, episodes, files, endpoints, models and API calls, with live trails.
  - **Architecture**: services and infrastructure read from code and config, with evidence and a Mermaid export.
  - **Settings**: names, roles, duties, colours and the model per agent.
- `teammap`: code map and research commands (`endpoint`, `symbol`, `model`, `arch`, `impact`, `relevant`, `failures`) for Python and TypeScript repositories.
- Research evaluation scripts in `eval/`.
- CI on Linux, Windows and macOS for Python 3.9 to 3.13, lint, package build checks and CodeQL.

[Unreleased]: https://github.com/JamshidS/aiset/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/JamshidS/aiset/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/JamshidS/aiset/releases/tag/v0.1.0
