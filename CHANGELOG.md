# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `aiset --version`, `aiset-init --version` and `teammap --version` print the package version without initializing a workspace.

### Security

- The office now refuses read requests whose `Host` isn't its own address. Before, only writes were checked, so a web page using DNS rebinding could read the dashboard's data, including transcript excerpts.
- Task ids in `/api/board` and `/api/task` must be plain task names. `..` previously resolved to the team folder.

### Fixed

- Settings, rosters, the workspace registry, launchers, agent definitions, and knowledge briefs are written by atomic replacement, so a failed write no longer leaves a truncated file.
- A team file that isn't UTF-8 (PowerShell 5.1 writes the ANSI codepage by default) no longer breaks the office: Markdown and text files are read with a replacement character for the bad bytes, and a name sync keeps them as they were. JSON files saved with a UTF-8 BOM (`roster.json`, `settings.json`, the workspace registry, a repo's `package.json`) load as usual instead of failing or silently falling back to defaults.
- Architecture scans use forward-slash repository paths on Windows, so workflow files and DAG folders are detected and test files stay out of infrastructure evidence. `teammap` preserves non-ASCII Git filenames, and `teammap file` resolves importers through the same Python/TypeScript resolver as the code graph, including tsconfig aliases.
- Sessions are matched to their workspace the way Claude Code names project folders. `slug()` lowercased before replacing, and `"İ".lower()` is two characters, so a workspace under a non-ASCII path (or one with an emoji in it) never matched and the office showed no sessions at all. Sibling folders no longer leak in either: with workspace `C:\work\app`, sessions from `C:\work\apples` and `C:\work\app2` used to appear on the floor and in the token and cost totals.
- The office footer now suggests the `<team>-pm` launcher when `aiset-init` writes it and the command is on `PATH`; `--no-launcher` and unavailable launchers keep the roster's existing PM command, and so does a `pmCommand` you edited in Settings.
- Code-scanning findings: explicit returns, documented intentional `except` blocks, unused variables in the dashboard, and a `Map` for the route cache. The `<team>-pm` launcher is now made executable for its owner only.

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

[Unreleased]: https://github.com/JamshidS/aiset/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JamshidS/aiset/releases/tag/v0.1.0
