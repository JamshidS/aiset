# Aiset user guide

[Back to README](../README.md)

Detailed setup, office views, research tools, and settings.

## Install

Python 3.9+. The Python package has no third-party runtime dependencies. Repository discovery uses Git; TypeScript indexing also requires Node.js and a TypeScript installation. Creating and running agent sessions requires Claude Code. From this folder:

```sh
pipx install aiset-cli    # adds the commands aiset, aiset-init and teammap (from a clone: pip install -e .)
```

Check which version is installed with `aiset --version` (also `aiset-init --version`, `teammap --version`); it doesn't start the office or touch a workspace.

Without installing, run the files directly: `python src/aiset/server.py`, `python src/aiset/teammap.py`, `python src/aiset/init_team.py`. Tests: `python -m unittest discover -s tests`.

The `Makefile` wraps the usual commands (`make` lists them): `make test`, `make check`, `make install`, `make office WS=~/work/shop`, `make init WS=… TEAM=…`, `make index WS=…`, `make impact WS=… Q='/items/{item_id} GET'`, `make relevant WS=… Q="…"`, `make failures WS=… Q=<log>`, `make eval WS=…`, `make eval-agent WS=… RUNS=3` (spends tokens), `make clean`.

Layout: the package is `src/aiset/` (code, `static/`, `templates/`, the sample `roster.json`, `teammap_ts.js`); `tests/` and `docs/` sit next to it.

## Use it on any project

Point it at a folder of git repositories (Windows, macOS or Linux):

```sh
aiset-init ~/work/example-project --team example-project  # scan repos, write the team into <folder>/.claude
example-project-pm                                        # talk to the PM, from any folder; first ask: "build the knowledge briefs"
aiset --workspace example-project                         # the office for that team (team name or folder)
```

`aiset-init` also records the team in `~/.aiset/workspaces.json` (`AISET_HOME` moves it) and writes a `<team>-pm` launcher next to its own command (`<team>-pm.cmd` on Windows, so PowerShell's script policy doesn't block it). The launcher runs `claude --agent pm` in the workspace and passes on extra arguments, e.g. `example-project-pm --model sonnet`. When the launcher is on `PATH`, the office footer suggests its command. If it is unavailable or you pass `--no-launcher`, the existing `pmCommand` stays in the roster. The office picks its team in this order: `--workspace` (folder or team name), the workspace you're in, then the team `aiset-init` set up most recently; it prints which one it chose.

`init_team.py` makes one engineer per repo (stack detected from `package.json`, `requirements.txt`/`pyproject.toml`, Gradle/Maven, `go.mod`; compose/nginx-only repos get a DevOps engineer), plus the PM, QA engineer and integration checker. Everything lives in the workspace, so several teams can coexist:

- `<folder>/.claude/agents/*.md`: project agents, rendered from `src/aiset/templates/`. Claude Code loads them for sessions started in the folder.
- `<folder>/.claude/team/`: `roster.json`, `knowledge/`, `tasks/`, `episodes/`, `graph/`.

**Monorepos** work too: when the folder is itself one git repository, every top-level folder with a build file (`pom.xml`, `package.json`, `requirements.txt`, `Dockerfile`…) is a service. Above 12 services (or with `--group stack`) the team gets one engineer per stack (Java, Node.js, Python, frontend) instead of one per service, and the DevOps engineer owns the root CI and build files. A monorepo's `.claude/` is added to `.git/info/exclude`, so the team stays on your machine; `--share` lets git see it.

A repository whose root has a manifest gets a root unit (`root`, checks run at the repository root) — the only unit in a single-package repository, alongside the service units in a monorepo. When service folders exist, the root engineer is told the service folders belong to their own engineers (unless one engineer owns both, `--group stack`); member ids stay unique (a service called `root`, `pm` or `tester` gets `root-2`, `pm-2`, ...).

Edit names, roles and duties in `<folder>/.claude/team/roster.json` and run `init_team.py` again: new repos are added, your names are kept, and agent files are re-rendered (except the ones where you deleted the "generated" marker line). `teammap` finds the workspace from the current directory; it parses TypeScript with the repo's own `typescript`, or with aiset's (`npm install --no-save typescript` in this folder) before the frontends have `node_modules`.

Without a workspace (no `--workspace`, not inside one, and for the office: no team set up with `aiset-init`), everything below works as before: `~/.claude/agents`, `~/.claude/team`, the checkout root as the code root (the package directory in a non-editable installation) and the package's `src/aiset/roster.json`.

## Use the example team

The bundled roster is a sample team with no repositories assigned. Set each engineer's `repos` list to your repository folder names, or run `init_team.py` for your workspace to generate a team from its repositories. The roster defines the office display; use `init_team.py` to create agent files.

```sh
claude --agent pm          # talk to the PM (a Claude Code session in the application folder)
aiset               # open the office in your browser (http://127.0.0.1:8765)
```

Tell the PM what you need. The PM clarifies the request with you, writes acceptance criteria, and assigns the work. Engineers report back, the QA engineer tests the changes, the integration checker checks the other services, and the PM validates the result before reporting to you. Nothing is committed unless you ask.

| Agent id | Name (from roster.json) | Role | Knowledge brief |
|---|---|---|---|
| `pm` | Deniz | Project Manager: your contact, plans and validates | (reads all briefs) |
| `backend` | Emre | Backend engineering | `backend.md` |
| `frontend` | Selin | Frontend engineering | `frontend.md` |
| `platform` | Arda | Platform engineering | `platform.md` |
| `tester` | Derya | QA: tests, lint, type-check | (uses service briefs) |
| `integration-checker` | Ece | cross-service contracts | `cross-service.md` |
| `devops` | Bora | CI/CD, deployment, observability | `devops.md` |
| `security` | Can | Authentication, authorization, security | `security.md` |
| `research` | Ada | Research and technical exploration | `research.md` |

- Agent definitions live in `~/.claude/agents/*.md`. Edit them there or with `/agents`.
- Knowledge briefs live in `~/.claude/team/knowledge/`. Agents add to their own brief under `## Field notes`. To regenerate a brief, ask your PM, for example "refresh Emre's knowledge".
- You can also use any agent from a normal Claude session, for example "ask Emre why …".

## The office

The floor is drawn isometrically by default (rooms, desks, seated bots, a whiteboard with the open task boards); **Classic** in the top-right corner switches to the flat card layout. Hover a desk for its details.

- **Desks** show each team member as an animated bot. The animation and colour show the state:
  - working: typing
  - thinking: thought bubble
  - needs you: waving, and a red banner appears at the top
  - done: coffee
  - idle: asleep
- **Speech bubbles** show what the agent is doing right now.
- **Desk cards** show the agent's current task and who assigned it.
- **Assignment lines** run from the PM to each agent with a running task.
- **Tasks** (side panel) lists the PM's tasks with the tokens and estimated cost all agents spent on each: total, a bar split by agent, and each agent's share. Click one for the breakdown by agent, every run (click a run to open that agent), and the board (brief, contract, handoffs, log) and episode. Open `/#tab=boards` to land on it.
  - A task is named by its board (`.claude/team/tasks/<id>/`) or its episode (`.claude/team/episodes/<id>.md`). A subagent belongs to the task in its `BOARD:` or `TASK:` line, or else to the task the PM was on when it assigned it. The PM's own turns go to the task it is on: the one its latest assignment or board/episode write named. Turns after a task closes (its episode is written) go to the next task the PM names, so the clarifying questions for a new request count toward it. Work with no board or episode yet shows as **open** while the session is active, and as **untracked** afterwards.
  - Tokens come from the last `--brain-days` of transcripts, not just the `--hours` window, so a long-running task's total is complete.
  - **Routes** (the drawer's second tab) shows where each agent went on the task: one line per run (files, reads, edits, searches, tests, DB queries, assignments, time), a **files × agents** grid (filled dot = edited, ring = read, dashed = searched; files more than one agent opened are highlighted; filter by repo), and each run's step-by-step route with consecutive repeats folded (`×n`). Click a file in the grid to trace only the steps that touched it. Steps are read from the whole transcript when you open the tab, including files read or searched through Bash (`cat`, `sed -n`, `grep`, `rg`, `find`…, following `cd` and `VAR=…`). The PM's steps are limited to its turns on this task. Open `/#open=task:<id>&view=routes` to land on it. Each task card has **🧭 Agent routes** (this tab) and **🧠 Map**.
  - **Map** opens `/brain#task=<id>`: the Brain shows only the places the task's agents went (files, brief sections, endpoints, models) and draws each agent's route over them in its colour. The agent opened in the side panel stands out; the others fade. Each step is listed with its time; click one to select its node. `exit` goes back to the live view.
- **Task board** groups assignments into: needs clarification, in progress, failed review, and done. It's built from the agents' reports (`STATUS:` lines).
- **Live feed** lists every tool call and message across the team.
- **Clicking a desk** opens the agent's knowledge brief status, assignment, report, and activity timeline.

- **Tokens and models**: each desk, task card and drawer shows the model (opus/sonnet/haiku) and the tokens used (fresh input, cache write, cache read, output) plus the current context size. Next to the tokens is an estimated cost at Anthropic list prices (`PRICES` in `core.py`); cache writes are priced by their TTL, since subagents write a 5 minute cache and lead sessions a 1 hour one. Most of a task's cost is usually cache writes, not the far larger cache-read token count. The top bar totals the team (the PM and the agents it assigned) and all sessions. A task card says "inherited" when the lead didn't choose a model, and shows the PM's `MODEL:` reason when it did.

## The Brain (`/brain`)

A graph of what the team knows and touched, with the same items as a tree on the side:

- **Agents** own **briefs** (split into their `##`/`###` **sections**, each with a token estimate) and **repos**.
- **Files** link to the agents that read or edited them (from transcripts, `--brain-days`, default 30) and to the sections that mention them.
- **Episodes** are task summaries the PM writes to `~/.claude/team/episodes/` when a task closes (format in `~/.claude/agents/pm.md`). They link to the agents involved and the files touched.
- **Code graph**: any `~/.claude/team/graph/*.json` file (`{"nodes": [{"id", "type", "label", ...}], "edges": [{"source", "target", "type"}]}`) is merged in, so a code indexer can extend the Brain without changes here. Node ids without a `prefix:` are namespaced as `code:`; use `file:<repo>/<path>` to attach to the file nodes above.

**Live**: the panel at the top right lists running agents (and, with a longer window, recently finished ones). Each agent's last steps are drawn on the map as a trail of arrows in its colour, a small bot marks the node it is on now, and the files it has touched in this task pulse. A step is placed by what it touched: a file it read or edited, the brief section at the line it read, or the endpoint, model or file a `teammap` query asked about. "Follow" keeps the camera on an agent; open `/brain#live=<agent-id>` (or "Watch in Brain" in the office drawer) to start following one.

Click a node or tree row for details and neighbours, double-click (or "Focus on this") to show only its neighbourhood, `/` to search, Esc to clear.

Options: `aiset --hours 24` (time window), `--port 9000`, `--no-browser`. Open `http://127.0.0.1:8765/#open=backend` to go straight to one agent.

It reads session transcripts without modifying them. It also synchronizes agent names and writes brief indexes and graph caches. It parses the transcripts Claude Code already writes in `~/.claude/projects` (`core.py`), builds the Brain graph (`brain.py`), serves both as JSON (`server.py`), and draws them (`src/aiset/static/index.html`, `src/aiset/static/brain.html`). See the installation requirements above.

"Needs you" is detected when the PM asks you a question (AskUserQuestion). If a session's tool call has had no result for 2 minutes, the office also shows "waiting for your approval?". This is a guess, because permission prompts aren't written to the transcript.

## Architecture (`/architecture`)

What each repository in the workspace is built with and what it connects to, read statically from the code and its configuration (`archmap.py`):

- **System map** in four columns: clients (frontends), services (APIs), async work and pipelines (Celery, Airflow, workers), and data and infrastructure (databases, caches, brokers, storage, auth, monitoring, services outside the workspace). Libraries, algorithms and scripts are listed below the map; **Libraries & scripts** puts them on it.
- **Links** are coloured by kind (HTTP, database, broker, jobs, cache, auth, storage). Solid links are certain: a frontend call matched to a backend route by URL (from `teammap`), or a dependency the code uses and configures. Dashed links are inferred from names, such as `BackendServiceClient` or `BACKEND_SERVICE_URL` → `backend`; **Inferred links** hides them.
- **Big systems**: above ~16 services the cards turn into one line (name, framework, a coloured dot per component) and the services column splits in two. **Clicking a box focuses on it**: the map is laid out again with only that box and what it links to, so everything fits on one screen; click another box to move along, Esc or **Show everything** to go back (**Focus on click** turns this off). **Find a service** (or `/`) highlights matches; Enter focuses the first.
- **Click a repo** for its languages, stack, how it runs (Dockerfile, compose, Makefile, `package.json`), how it's built and deployed, the infrastructure it uses and the services it calls and is called by. Every item has its evidence (`file:line` and the line itself); links open in VS Code.
- **Confidence**: *wired* (dependency or code, plus configuration), *used* (the code uses it), *declared* (only a dependency), *inferred* (from names), *exact* (URL match).
- **Mermaid** gives the same map as a Mermaid diagram for docs and PRs. **Rescan** reads the repositories again (otherwise the map is cached for a minute).
- Environment variables are read by **name only**: `.env` files and `secrets/` folders are never opened, and credentials in evidence lines are masked.
- Agents get the same in text, and more: `teammap arch [repo]`, `teammap arch <component>`, `teammap arch impact <service>`, `teammap arch path <a> <b>` (add `--json` for JSON). The scan is cached in `<team>/graph/index/arch.json` until a file changes, so a query takes ~0.2 s. Engineers, the integration checker and the PM are told when to use them (templates and agent files). Detectors are rules in `archmap.py` (`DETECTORS`); adding a technology is adding one rule.

## Token-saving setup (the agents use these)

- **Tool whitelists**: every agent file has a `tools:` line. Without one, an agent carries every tool, connector and skill definition (~31k tokens) on every turn; with it, it starts at ~5k.
- **Brief indexes**: `brief_index.py` writes `<name>.index.md` (sections, line ranges, token sizes) next to each knowledge brief; agents read the index and then only the sections they need. The office refreshes stale indexes on every poll; run `python src/aiset/brief_index.py <name>.md` after rewriting a brief.
- **teammap nudge**: the engineer, QA and integration-checker files carry a `PreToolUse` hook (`nudge.py`). When one of them greps for a symbol or a route, or reads a long source file whole, the agent gets one line with the `teammap` command that answers it (`symbol`, `code`, `endpoint`, `file`). The call still runs; each suggestion comes once per session; plain-text searches, non-code files and repos teammap can't index are left alone. It only runs while those agents work, not in your own sessions. Claude Code runs hooks from agent files only in a folder you've trusted: the first `claude` (or `<team>-pm`) in the workspace asks, and a `claude -p` run doesn't count as accepting. `aiset-init --no-nudge` leaves it out (remembered as `"nudge": false` in `roster.json`). The task detail shows each agent's teammap calls (🗺️) next to its reads (📖) and searches (🔎), so you can see whether it's used.
- **Task boards**: shared memory per request (brief, contract, handoffs, log), so the PM doesn't relay between agents. Rules are in `pm.md` and each engineer's file.
- **`teammap`** (`src/aiset/teammap.py`, installed as the `teammap` command): a code map of the repositories configured in your team's roster.

  ```sh
  teammap endpoint items        # handler, request/response models, frontend callers
  teammap symbol get_item           # definition, signature, callers, callees
  teammap code list_items            # just that function's source, with line numbers (a class: header + method list)
  teammap arch backend      # stack, how it runs, infrastructure it uses, services it calls and is called by
  teammap arch rabbitmq                # who uses a component (mongodb, redis, kafka…), with env names and file:line
  teammap arch impact auth-server      # who calls it, two levels up: what a change can break
  teammap arch path api-gateway backend    # the HTTP hops from one service to another
  teammap model CreateItemRequest  # fields, endpoints, matching frontend type + field diff
  teammap file items_router
  teammap search item_status
  teammap index                        # refresh everything, print counts
  ```

  Python is parsed with `ast`, TypeScript with frontend's own `typescript` package (`teammap_ts.js`). The cache in `~/.claude/team/graph/index/` refreshes changed files on every query, and each refresh writes the Brain graph (`~/.claude/team/graph/<repo>.json`): every indexed file with the imports between them (Python modules, relative and tsconfig-alias TypeScript imports), plus endpoints, models and API calls. On the Brain page all files are in the Repositories tree; on the map, files no agent has opened are under **Code files**, off by default. Frontend calls are matched to backend routes by resolved URL; "no backend match" usually means the route doesn't exist on the current branch.

Repository-relative paths use `/` on every platform, including Windows architecture evidence. Git-tracked non-ASCII filenames are indexed. `teammap file` resolves its importers with the same Python/TypeScript resolver as the code graph, including relative imports, directory indexes and tsconfig aliases.

## Research tools (`teammap impact`, `failures`, `relevant`)

Three questions agents keep answering from scratch, with greps and reads, now take one call each. They reuse the code index, the import graph, the briefs and the episodes, run on the current code every time, and are told to the agents in their files (engineer, tester, integration checker, PM).

```sh
teammap impact /products/{product_id} PATCH   # endpoint: handler, models, consumers, direct callers, tests, episodes
teammap impact ProductService                  # function/class: definitions, callers, importers, endpoints, tests
teammap impact ProductResponse                 # model: endpoints using it, subclasses, importers, frontend mirrors, tests
teammap failures /tmp/example-api-test.log      # pytest, unittest, jest, vitest, node --test (TAP), JUnit XML
teammap relevant "product batch stock adjustment"   # files, symbols, endpoints, models, brief sections, episodes
teammap research show <id>                      # a stored result in full;  teammap research list
```

Options: `--json`, `--limit N` (rows per section; 8, or 5 for `relevant`), `--all`, `--max-chars N`, `--repo NAME`, `--fresh`, `--format F` (failures). A missing index, an unknown name or an unreadable test output gives a result that says so (exit code 1), not a guess.

**What a result contains.** The research type and parameters; a short summary; findings by section, each with repo, file, line and symbol where there is one, why it was picked and whether it is **exact** (`=`: from the code's structure, such as a route decorator, a resolved import or a type annotation) or **inferred** (`~`: matched by name, by URL suffix, by a path literal or by words); **gaps** (what the index can't see for this question); the inputs' fingerprint, plus each repo's HEAD and number of uncommitted changes; the stored record's path and id; and which sections were cut. `--limit` and `--max-chars` cut the printed text only: the record keeps every row, and the cut is named with the way to see the rest (`--all`, `research show <id>`). Sizes are in characters; nothing claims to count tokens.

**How each one works.**
- `impact` resolves the query to an endpoint (a path part, with an optional method), a model or a function/class. For an endpoint, consumers are frontend HTTP calls whose resolved URL is the same (exact) or ends the same (inferred). Where a frontend builds URLs at runtime behind a client wrapper (`request('/products/')`), it finds the path literal with the same path shape and HTTP method (inferred) and then the code that calls that API-client property (`api.products.update()`, inferred). Tests are those that import the code (exact), mention its name or URL (inferred), or import a service whose function the handler calls (inferred).
- `failures` reads a saved test output (a file or `-` for stdin) and lists each failed test with its error type, message and stack frames. Frames inside the indexed repos are mapped to the function around them; other project paths are shown as printed. Failures with the same error type, the same message (numbers and quoted values ignored) and the same innermost project frame are grouped. Each failure keeps its line range in the full log, which is copied to `<team>/graph/research/logs/`. The root cause is always reported as not determined: frames show where an error surfaced, not why. The output is only read; nothing in it is run.
- `relevant` ranks the index, the briefs' sections and the episodes by the words they share with the query (camelCase and snake_case split, prefixes of five or more letters, rarer words weigh more) and says which words matched where. It has no embeddings and no model behind it, so it doesn't know synonyms or translate: describe the task in the code's language.

**Validity and storage.** Every call refreshes the index for changed files (by mtime, so uncommitted edits count) and computes a fingerprint over the indexed files' mtimes, the briefs and episodes (impact, relevant) or the log's SHA-256 and the files its frames point into (failures), and the research and index code itself. A stored result is reused only when that fingerprint and the parameters match; otherwise it is recomputed and replaced. `research show` prints a stored result without re-checking it, and says so. Records live in `<team>/graph/research/` in the workspace; the tools never write to the repositories or to the package.

**Limits.** Python and TypeScript only (other repos are listed as not covered, with `teammap arch impact` for service-level callers). Callers are matched by name, so same-named functions elsewhere show up and calls through dependency injection, `getattr` or callbacks may not. URLs assembled at runtime are found only through path literals. Test discovery is by imports, names and URLs, not by running tests or coverage. The parsers know the formats above; another reporter's output is reported as unrecognized.

**Measuring it.** `eval/research_eval.py` compares the current way (the existing teammap queries, then grep and reads) with the research commands, on the same tasks and the same repository state:

```sh
python eval/research_eval.py ~/work/shop/.claude/team/eval/tasks.json --workspace ~/work/shop      # size mode
python eval/research_eval.py ~/work/shop/.claude/team/eval/tasks.json --workspace ~/work/shop --mode agent --confirm-cost --runs 3
```

A task file belongs to one workspace (it names its files), so keep it in `<workspace>/.claude/team/eval/`. It has `repos` (checked for an unchanged state in agent mode) and `tasks`, each with an `id`, a `question` (agent mode), `expect` (strings the answer should contain) and `baseline` and `treatment` steps: `{"cmd": "teammap impact /x GET"}` (run in the workspace, or in a repo with `"cwd"`; no shell) or `{"read": "repo/path", "lines": [a, b]}` (stands for a Read call). Put only read-only commands in it: they run as written. `python eval/research_eval.py --help` has the details.

Size mode runs each task's scripted steps (commands and reads) for both variants and records steps, reads, output characters, seconds and whether the expected facts appear. It compares output sizes, not tokens, and a scripted baseline is not an agent. Agent mode runs `claude -p` on each task's question, told once not to use the research commands and once to use them, and records what Claude Code reports: input, output, cache-read and cache-write tokens, tool calls by name, reads, turns, duration, cost and whether the answer has the expected facts. It checks that the repositories' HEAD, status and diff are the same before and after every run, and stops if a run changed them. Both modes time a cold and a warm `teammap index` and report the size of the index and of the stored results. Results from 2026-10-17 are in [docs/RESEARCH-TOOLS.md](RESEARCH-TOOLS.md).

## Settings (`/settings`)

Everything you'd otherwise edit by hand, in one page (**Settings** in the top bar):

- **Agents & models**: each agent's model (Default, Inherit, Opus, Sonnet, Haiku, Fable, or any model id), name, role, colour and duty; "Set every engineer to …" changes many at once. Saving writes `roster.json` (`"model"` per member) and the agent files: the model goes into each file's `model:` frontmatter line, and the PM's file gets a **Pinned models** section so it stops passing its own `model` for those agents. `init_team.py` keeps the models when it re-renders. Running sessions keep their model until they restart.
- **Team**: team name and the PM command shown in the office.
- **Office layout**: close any side-panel tab (or the whole panel), rooms (Claude sessions, Quality, Infrastructure, Contractors), the Brain and Architecture links, the token pills, assignment lines, the "needs you" banner and the PM hint; pick the first tab and the floor view. Each side-panel tab also has an **×** in the office; closed tabs come back from Settings.
- **Time windows**: the office window and the Brain/task history, applied without a restart. `--hours` and `--brain-days` still override them for that run.

Layout and windows are stored in `<team>/settings.json`. The page only accepts writes from the office itself (same host, JSON requests).

## Renaming agents

Use **Settings**, or edit the `name` fields in `<workspace>/.claude/team/roster.json`, or in `src/aiset/roster.json` when using the bundled sample from a source checkout. The running office picks the change up within a couple of seconds and copies the names into `~/.claude/agents/*.md`, so the agents use their new names too. (You can also run `python src/aiset/sync_names.py` by hand.) Agent sessions that are already running keep the old names until they're restarted.
