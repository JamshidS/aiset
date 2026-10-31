#!/usr/bin/env python3
"""Reusable research for agents: one call answers a question an agent would otherwise rebuild from greps and reads.

  teammap impact <path-part|symbol|Model> [METHOD]  what a change to an endpoint, function or model can reach:
                                                    definition, consumers, models, importers, tests, episodes
  teammap failures <test-output-file | -> [--format F]
                                                    failed tests from a saved test run: messages, in-repo stack frames,
                                                    repeated errors grouped; F: pytest, unittest, jest, vitest, tap, junit
  teammap run -- <command…>                       run a test command, save its full output, print a short summary
  teammap relevant "<task text or symbol>"          files, symbols, endpoints, models, brief sections and episodes
                                                    worth reading first, each with why it was picked
  teammap research show <id> [--section S]          a stored result in full (ids are printed with every result)
  teammap research list                             stored results, newest first

Options: --json (machine-readable), --limit N (rows per section; default 8, relevant 5), --all (no row limit),
         --max-chars N (cap the printed text), --repo NAME (impact/relevant: one repo; failures: base for relative paths),
         --fresh (recompute even when the stored result is still valid)

Every result says which findings are exact (from the code's structure: a route decorator, an import, a type annotation)
and which are inferred (matched by name, by URL suffix or by text), what the code index can't see, and the fingerprint
of the inputs it was computed from. A stored result is reused only while that fingerprint still matches the code:
index file mtimes (committed or not) plus the tool version and parameters; otherwise it is recomputed.
Results are stored in <team>/graph/research/ (see workspace.py); nothing is written into the repositories, and
saved test output is read, never executed; `run` executes only the command the caller explicitly supplies.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from glob import glob

if __name__ == "__main__" and not __package__:  # run as a file: python src/aiset/research.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    __package__ = "aiset"
from . import teammap  # noqa: E402
from .brain import brief_sections, read_episodes  # noqa: E402
from .workspace import CODE_ROOT, EPISODES, GRAPHS, KNOWLEDGE, WORKSPACE, norm, relative  # noqa: E402

TOOL_VERSION = 1
STORE = os.path.join(GRAPHS, "research")


def _code_digest():
    """The research and indexing code itself: a stored result is stale when the tool that made it changed."""
    h = hashlib.sha1()
    for mod in ("research.py", "teammap.py", "teammap_ts.js"):
        try:
            with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), mod), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(mod.encode())
    return h.hexdigest()[:12]


TOOL_CODE = _code_digest()
EXACT, INFERRED = "exact", "inferred"


# -- the result contract -------------------------------------------------------------------------------

def new_result(tool, params):
    return {"tool": tool, "version": TOOL_VERSION, "params": params, "target": None, "summary": [],
            "sections": {}, "gaps": [], "inputs": {}, "id": None, "record": None, "created": None,
            "cached": False, "status": "ok"}


def add(res, section, certainty, why, repo=None, file=None, line=None, symbol=None, text=None, **extra):
    """One finding. certainty: exact (from the code's structure) or inferred (by name, URL or text)."""
    row = {"certainty": certainty, "repo": repo, "file": file, "line": line, "symbol": symbol, "text": text, "why": why}
    row.update({k: v for k, v in extra.items() if v is not None})
    res["sections"].setdefault(section, []).append(row)
    return row


def _sha(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update((p if isinstance(p, str) else json.dumps(p, sort_keys=True, default=str)).encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def git_state(root):
    """HEAD and the number of uncommitted changes, for the record (validity itself comes from file mtimes)."""
    def run(*args):
        try:
            return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8", timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    head = run("rev-parse", "--short", "HEAD").strip()
    dirty = [l for l in run("status", "--porcelain", "--untracked-files=no", "--", ".").splitlines() if l.strip()]
    return {"head": head or None, "uncommitted": len(dirty)}


def _record_path(tool, rid):
    return os.path.join(STORE, tool, rid + ".json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stored_or_compute(tool, params, fingerprint, compute, fresh=False):
    """The stored result when its inputs fingerprint still matches, else a fresh one (stored for next time)."""
    rid = _sha(tool, TOOL_VERSION, params)[:12]
    path = _record_path(tool, rid)
    old = None if fresh else _load(path)
    if old and old.get("version") == TOOL_VERSION and old.get("inputs", {}).get("fingerprint") == fingerprint:
        old["cached"] = True
        return old
    t0 = time.time()
    res = compute()
    res["inputs"]["fingerprint"] = fingerprint
    res["inputs"]["compute_ms"] = round((time.time() - t0) * 1000)
    res.update(id=rid, record=norm(path), created=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if old:
        res["replaced"] = {"created": old.get("created"), "reason": "inputs changed since it was computed" if not fresh else "--fresh"}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    except OSError as e:
        res["gaps"].append(f"result not stored ({e}); it is still valid for this call")
        res["record"] = None
    return res


# -- the code index ---------------------------------------------------------------------------------------

def load_index(only=None):
    """(repos, links, gaps) from teammap's index, refreshed for changed files first."""
    gaps = []
    names = [r for r in teammap.REPOS if os.path.isdir(os.path.join(CODE_ROOT, r))]
    missing = [r for r in teammap.REPOS if r not in names]
    if missing:
        gaps.append(f"roster repos not found under {CODE_ROOT}: {', '.join(missing)}")
    if not names:
        where = WORKSPACE or "the default ~/.claude/team (no workspace found)"
        gaps.append(f"no repositories to index: the roster lists none that exist (workspace: {where})")
        return [], {"endpoints": [], "models": {}, "apifns": []}, gaps
    repos = [teammap.load_repo(n) for n in names]
    for r in repos:
        ts_tracked = len(teammap.git_files(r["root"], ["*.ts", "*.tsx"]))
        ts_indexed = sum(1 for e in r["files"].values() if e["lang"] == "ts")
        if ts_tracked > ts_indexed:
            gaps.append(f"{r['name']}: {ts_tracked - ts_indexed} TypeScript file(s) not indexed (node or the typescript package not found)")
        if not r["files"]:
            gaps.append(f"{r['name']}: no indexed files (only git-tracked .py/.ts/.tsx files are indexed)")
        broken = sum(1 for e in r["files"].values() if "error" in e["data"])
        if broken:
            gaps.append(f"{r['name']}: {broken} file(s) failed to parse and are left out")
    L = teammap.link(repos)
    if any(r["changed"] for r in repos):
        teammap.export(repos, L)  # keep the Brain's code graph in step, as teammap does
    if only:
        if only not in names:
            gaps.append(f"--repo {only}: not an indexed repo ({', '.join(names)})")
    return repos, L, gaps


def index_fingerprint(repos, extra=()):
    rows = sorted((r["name"], rel, e["mtime"]) for r in repos for rel, e in r["files"].items())
    return _sha(teammap.VERSION, TOOL_CODE, rows, list(extra))


def inputs_of(repos):
    return {"repos": {r["name"]: {"files": len(r["files"]), **git_state(r["root"])} for r in repos},
            "index_version": teammap.VERSION, "tool_code": TOOL_CODE}


def not_indexed_repos(repos):
    """Repos on the architecture map that the code index doesn't cover (e.g. Java services)."""
    try:
        from . import archmap
        units = archmap.repo_dirs()
    except Exception:
        return []
    have = {r["name"] for r in repos}
    return [u for u in units if u not in have]


def _imports_by_repo(repos):
    return {r["name"]: teammap.file_imports(r) for r in repos}


def enclosing_symbol(repos_by_name, repo, rel, line):
    """The innermost indexed definition around repo/rel:line, or None."""
    r = repos_by_name.get(repo)
    e = r and r["files"].get(rel)
    if not e or not line:
        return None
    best = None
    for s in e["data"].get("symbols", []):
        if s["line"] <= line <= s.get("end", s["line"]) and (best is None or s["line"] >= best["line"]):
            best = s
    return best["qual"] if best else None


# -- tests ---------------------------------------------------------------------------------------------

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$|\.(test|spec)\.[cm]?[jt]sx?$")
TEST_GLOBS = ["*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.cjs", "*.mjs"]


def test_files(repos):
    """{repo: [rel]} of test files, indexed or not (.js/.cjs tests aren't in the code index)."""
    out = {}
    for r in repos:
        out[r["name"]] = [f for f in teammap.git_files(r["root"], TEST_GLOBS) if TEST_FILE.search(f)]
    return out


def _read(path, limit=2_000_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def find_tests(res, repos, imports, tests, target_files, names, literals, limit_per_file=1):
    """Tests that import a target file (exact) or mention a target name or URL literal (inferred)."""
    found = set()
    for r in repos:
        imp = imports[r["name"]]
        for rel in tests.get(r["name"], []):
            for a, b in imp:
                if a == rel and (r["name"], b) in target_files:
                    add(res, "tests", EXACT, f"imports {b}", repo=r["name"], file=rel)
                    found.add((r["name"], rel))
                    break
            if (r["name"], rel) in found:
                continue
            text = _read(os.path.join(r["root"], rel))
            if not text:
                continue
            for pat, why in [(re.compile(r"\b%s\b" % re.escape(n)), f"mentions {n}") for n in names if n] + \
                            [(re.compile(re.escape(l) + r"(?=[/'\"`?$]|\b)"), f"contains the URL {l}") for l in literals]:
                m = pat.search(text)
                if m:
                    add(res, "tests", INFERRED, why, repo=r["name"], file=rel, line=text.count("\n", 0, m.start()) + 1)
                    found.add((r["name"], rel))
                    break
    return found


# -- impact --------------------------------------------------------------------------------------------

def _static_literal(path):
    """The static part of an endpoint path a caller would spell out: /api/v1/products/{id} -> /products."""
    segs = [s for s in path.split("/") if s]
    while segs and (segs[0] in ("api",) or re.fullmatch(r"v\d+", segs[0])):
        segs = segs[1:]
    static = []
    for s in segs:
        if "{" in s:
            break
        static.append(s)
    lit = "/" + "/".join(static) if static else ""
    return lit if len(lit) >= 5 else ""


def _literal_hits(text, lit, ep):
    """Lines where a frontend spells out an endpoint's path: [(line, same shape and method, property chain)].

    `request('/products/')` matches GET /products/; `request(`/products/${id}`, {method: 'PATCH'})` matches
    PATCH /products/{id}. Only the lines that agree with the endpoint are returned when any do."""
    tail = ep["norm"][ep["norm"].find(lit) + len(lit):] if lit in ep["norm"] else None
    lines = text.splitlines()
    hits = []
    for m in re.finditer(re.escape(lit) + r"(?=[/'\"`?$])", text):
        rest = re.match(r"[^'\"`?\s]*", text[m.end():]).group(0)
        shape = re.sub(r"\$\{[^}]*\}", "{}", rest).rstrip("/")
        i = text.count("\n", 0, m.start())
        call = [lines[i][m.start() - text.rfind("\n", 0, m.start()) - 1:]]  # from the literal on, plus the call's
        for ln in lines[i + 1:i + 3]:                                     # options below it, up to the next property
            if PROPERTY.match(ln):
                break
            call.append(ln)
        mm = re.search(r"method:\s*['\"](\w+)", " ".join(call))
        method = mm.group(1).upper() if mm else "GET"
        hits.append((i + 1, tail is not None and shape == tail and method == ep["method"], _property_path(lines, i)))
    strong = [h for h in hits if h[1]]
    return strong or hits[:1]


PROPERTY = re.compile(r"^(\s*)(\w+)\s*:\s*(?:async\s+)?(?:\(|function\b|\w+\s*=>)")


def _property_path(lines, i):
    """For `list: () => request('/x')` inside `products: {`, ["products", "list"]; None when the line isn't a property."""
    m = PROPERTY.match(lines[i])
    if not m:
        return None
    chain, indent = [m.group(2)], len(m.group(1))
    for j in range(i - 1, max(-1, i - 300), -1):
        ln = lines[j]
        if not ln.strip() or len(ln) - len(ln.lstrip()) >= indent:
            continue
        pm = re.match(r"^\s*(\w+)\s*:\s*\{\s*$", ln)
        if pm:
            chain.insert(0, pm.group(1))
        break
    return chain


def resolve_target(repos, L, query, method=None, kind=None, only=None):
    """("endpoint", [endpoints]) | ("model", [names]) | ("symbol", [hits]) | (None, []). only: a repo to look in."""
    def endpoints():
        q = query.lower()
        hits = [e for e in L["endpoints"] if q in e["path"].lower() and (not method or e["method"] == method)
                and (not only or e["repo"] == only)]
        exact = [e for e in hits if e["norm"] == teammap.norm(query) or e["path"] == query]
        return exact or hits
    if kind == "endpoint" or (kind is None and "/" in query):
        return "endpoint", endpoints()
    in_repo = lambda n: not only or any(m["repo"] == only for m in L["models"][n])
    if kind == "model" or (kind is None and query in L["models"] and in_repo(query)):
        names = [n for n in L["models"] if n == query and in_repo(n)] or [n for n in L["models"] if query.lower() in n.lower() and in_repo(n)]
        return ("model", names) if names else (None, [])
    hits = teammap._best_hits([r for r in repos if not only or r["name"] == only], query)
    exact = [h for h in hits if h[3]["name"] == query or h[3]["qual"] == query]
    if kind == "symbol" or exact:
        return ("symbol", exact or hits) if (exact or hits) else (None, [])
    eps = endpoints()
    if eps:
        return "endpoint", eps
    names = [n for n in L["models"] if query.lower() in n.lower() and in_repo(n)]
    if names:
        return "model", names
    return ("symbol", hits) if hits else (None, [])


def impact(repos, L, query, method=None, kind=None, only=None):
    res = new_result("impact", {"query": query, "method": method, "kind": kind, "repo": only})
    by_name = {r["name"]: r for r in repos}
    kind_found, items = resolve_target(repos, L, query, method, kind, only)
    if not kind_found or not items:
        res["status"] = "not-found"
        res["summary"].append(f"nothing in the code index matches {query!r} (endpoint path part, function/class name or model name)")
        res["gaps"].append("the index covers git-tracked Python and TypeScript files of the roster's repos; "
                           f"try `teammap search {query}` or a shorter name")
        return res
    if kind_found == "endpoint" and len(items) > 1 and len({(e["method"], e["norm"]) for e in items}) > 1:
        res["status"] = "ambiguous"
        res["summary"].append(f"{len(items)} endpoints match {query!r}; give the full path and a method")
        for e in sorted(items, key=lambda e: (len(e["path"]), e["path"])):
            add(res, "candidates", EXACT, "path contains the query", repo=e["repo"], file=e["file"], line=e["line"],
                symbol=e["handler"], text=f"{e['method']} {e['path']}")
        return res
    imports = _imports_by_repo(repos)
    tests = test_files(repos)
    if kind_found == "endpoint":
        _impact_endpoint(res, repos, L, items[0], imports, tests, by_name)
    elif kind_found == "model":
        _impact_model(res, repos, L, items[0], imports, tests, others=items[1:])
    else:
        _impact_symbol(res, repos, L, items, imports, tests)
    left = not_indexed_repos(repos)
    if left:
        res["gaps"].append(f"not in the code index (other languages or no tracked .py/.ts): {', '.join(left)}; "
                           "`teammap arch impact <service>` covers service-level callers there")
    return res


def _endpoint_consumers(res, repos, L, ep, by_name):
    exact_files = set()
    for a in ep.get("frontend", []):
        same = teammap.norm(a["path"]) == ep["norm"]
        add(res, "consumers", EXACT if same else INFERRED,
            "HTTP call to the same URL" if same else f"URL suffix match ({a['method']} {a['path']})",
            repo=a["repo"], file=a["file"], line=a["line"], symbol=a["in"], text=f"{a['method']} {a['path']}")
        exact_files.add((a["repo"], a["file"]))
        parts = a["in"].split(".")
        for c in teammap.call_sites([by_name[a["repo"]]], parts[-1], parts[-2] if len(parts) > 1 else None):
            add(res, "consumers", INFERRED, f"calls {a['in']}() (matched by name)", repo=c["repo"], file=c["file"],
                line=c["line"], symbol=c["caller"])
    lit = _static_literal(ep["path"])
    if lit:
        for r in repos:
            if r["name"] == ep["repo"]:
                continue
            for rel, e in r["files"].items():
                if (r["name"], rel) in exact_files:
                    continue
                text = _read(os.path.join(r["root"], rel))
                for line, strong, prop in _literal_hits(text, lit, ep) if text else []:
                    add(res, "consumers", INFERRED,
                        f"path literal {lit} with the same path shape and method" if strong else
                        f"contains the path literal {lit}; path shape or method differ or are unclear",
                        repo=r["name"], file=rel, line=line, symbol=enclosing_symbol(by_name, r["name"], rel, line))
                    if strong and prop and len(prop) > 1:
                        for c in teammap.call_sites([r], prop[-1], prop[-2]):
                            add(res, "consumers", INFERRED, f"calls {'.'.join(prop)}() (matched by property name)",
                                repo=c["repo"], file=c["file"], line=c["line"], symbol=c["caller"])
    unmatched = [a for a in L["apifns"] if not a["endpoint"]]
    if unmatched:
        where = Counter(f"{a['repo']}/{a['file']}" for a in unmatched)
        res["gaps"].append(f"{len(unmatched)} frontend HTTP call(s) build their URL at runtime and match no endpoint "
                           f"({', '.join(f'{w} ×{n}' for w, n in where.most_common(4))}); consumers going through them "
                           f"are found only by the path literal" + (f" {lit}" if lit else " (none usable for this path)"))
    return lit


def _impact_endpoint(res, repos, L, ep, imports, tests, by_name):
    res["target"] = {"kind": "endpoint", "method": ep["method"], "path": ep["path"], "repo": ep["repo"]}
    add(res, "definition", EXACT, "route registration", repo=ep["repo"], file=ep["file"], line=ep["line"],
        symbol=ep["handler"], text=f"{ep['method']} {ep['path']}")
    for p in ep["params"]:
        if teammap.injected(p):  # auth users, sessions, services: dependencies, not request data
            continue
        for mn in teammap.model_names(p["type"], L["models"]):
            m = L["models"][mn][0]
            add(res, "models", EXACT, f"request parameter {p['name']}: {p['type']}", repo=m["repo"], file=m["file"],
                line=m["line"], symbol=mn, text=f"{len(m['fields'])} fields")
    for mn in teammap.model_names(ep["response"], L["models"]):
        m = L["models"][mn][0]
        add(res, "models", EXACT, f"response_model {ep['response']}", repo=m["repo"], file=m["file"], line=m["line"],
            symbol=mn, text=f"{len(m['fields'])} fields")
    lit = _endpoint_consumers(res, repos, L, ep, by_name)
    handler = (ep["handler"] or "").split(".")[-1]
    if handler:
        for c in teammap.call_sites([by_name[ep["repo"]]], handler):
            add(res, "callers", INFERRED, f"calls {handler}() directly (matched by name)", repo=c["repo"], file=c["file"],
                line=c["line"], symbol=c["caller"])
    found = find_tests(res, repos, imports, tests, {(ep["repo"], ep["file"])}, [handler] if len(handler) > 3 else [], [lit] if lit else [])
    # tests of the code the handler calls: files the router imports that define a function the handler calls
    repo = by_name[ep["repo"]]
    used = {}
    for a, b in imports[ep["repo"]]:
        if a == ep["file"]:
            for s in repo["files"][b]["data"].get("symbols", []):
                if s["name"] in ep["calls"] and s["name"] not in ("get", "list", "update", "delete", "create"):
                    used.setdefault(b, s["qual"])
    for a, b in sorted(imports[ep["repo"]]):
        if a in tests.get(ep["repo"], []) and b in used and (ep["repo"], a) not in found:
            add(res, "tests", INFERRED, f"imports {b}, whose {used[b]}() the handler calls", repo=ep["repo"], file=a)
            found.add((ep["repo"], a))
    _episodes(res, [f"{ep['repo']}/{ep['file']}"])
    n_c = len(res["sections"].get("consumers", []))
    res["summary"].append(f"{ep['method']} {ep['path']} is handled by {ep['handler']}() in {ep['repo']}/{ep['file']}:{ep['line']}")
    res["summary"].append(f"{n_c} consumer(s) found ({sum(1 for c in res['sections'].get('consumers', []) if c['certainty'] == EXACT)} exact), "
                          f"{len(res['sections'].get('models', []))} model(s), {len(res['sections'].get('tests', []))} test file(s)")
    if not res["sections"].get("tests"):
        res["gaps"].append("no test file imports the handler's module or mentions the handler or path")


def _impact_symbol(res, repos, L, hits, imports, tests):
    r0, f0, lang, s = hits[0]
    name = s["name"]
    res["target"] = {"kind": "symbol", "name": s["qual"], "repo": r0, "file": f0, "line": s["line"]}
    for r, f, _, h in hits[:6]:
        add(res, "definition", EXACT, f"{h['kind']} definition", repo=r, file=f, line=h["line"], symbol=h["qual"],
            text=f"lines {h['line']}-{h.get('end', h['line'])}")
    if len(hits) > 1:
        res["gaps"].append(f"{len(hits)} definitions match {name!r}; callers are matched by name and may belong to any of them")
    for e in L["endpoints"]:
        if e["repo"] == r0 and e["file"] == f0 and (e["handler"] or "").split(".")[-1] == name:
            add(res, "endpoints", EXACT, "serves this route", repo=e["repo"], file=e["file"], line=e["line"],
                text=f"{e['method']} {e['path']}")
        elif name in e["calls"] and e["repo"] == r0:
            add(res, "endpoints", INFERRED, f"handler {e['handler']}() calls {name} (matched by name)", repo=e["repo"],
                file=e["file"], line=e["line"], text=f"{e['method']} {e['path']}")
    for c in teammap.call_sites(repos, name):
        if (c["repo"], c["file"], c["caller"]) != (r0, f0, s["qual"]):
            add(res, "callers", INFERRED, f"calls {name} (matched by name)", repo=c["repo"], file=c["file"],
                line=c["line"], symbol=c["caller"])
    target_files = {(r, f) for r, f, _, _ in hits[:1]}
    repo0 = next(r for r in repos if r["name"] == r0)
    for a, b in sorted(imports[r0]):
        if b == f0:
            exact_name = name in imported_names(repo0, a, b)
            add(res, "importers", EXACT, f"imports {name} from {b}" if exact_name else f"imports {b}", repo=r0, file=a,
                imports_name=exact_name)
    find_tests(res, repos, imports, tests, target_files, [name] if len(name) > 3 else [], [])
    _episodes(res, [f"{r0}/{f0}"])
    res["summary"].append(f"{s['kind']} {s['qual']} at {r0}/{f0}:{s['line']}")
    sec = res["sections"]
    res["summary"].append(f"{len(sec.get('callers', []))} call site(s) by name, {len(sec.get('importers', []))} importing file(s), "
                          f"{len(sec.get('endpoints', []))} endpoint(s), {len(sec.get('tests', []))} test file(s)")
    res["gaps"].append("calls through self.x, dependency injection, getattr or callbacks are matched by name only; "
                       "calls of a same-named function elsewhere are included")
    if not sec.get("tests"):
        res["gaps"].append(f"no test file imports {f0} or mentions {name}")


_resolvers = {}


def imported_names(repo, rel, target_rel):
    """The names file rel imports from target_rel (`from x import Name`, `import { Name } from "./x"`)."""
    key = (repo["name"], id(repo["files"]))
    if key not in _resolvers:
        _resolvers[key] = teammap.import_resolver(repo)
    target = _resolvers[key]
    out = set()
    for imp in repo["files"][rel]["data"].get("imports") or []:
        if target(rel, imp) == target_rel:
            if imp.get("name"):
                out.add(imp["name"])
            out.update(imp.get("names") or [])
    return out


def _impact_model(res, repos, L, n, imports, tests, others=()):
    m = L["models"][n][0]
    res["target"] = {"kind": "model", "name": n, "repo": m["repo"], "file": m["file"], "line": m["line"]}
    add(res, "definition", EXACT, f"model class ({', '.join(m['bases'])})", repo=m["repo"], file=m["file"], line=m["line"],
        symbol=n, text=teammap.fmt_fields(m["fields"], 10))
    for role, e in teammap.model_uses(L, n):
        add(res, "endpoints", EXACT, f"{role} type in the handler's annotations", repo=e["repo"], file=e["file"],
            line=e["line"], symbol=e["handler"], text=f"{e['method']} {e['path']}")
    for other, defs in L["models"].items():
        for d in defs:
            if n in [b.split(".")[-1] for b in d["bases"]]:
                add(res, "subclasses", EXACT, f"subclass of {n}", repo=d["repo"], file=d["file"], line=d["line"], symbol=other)
    repo_m = next(r for r in repos if r["name"] == m["repo"])
    for a, b in sorted(imports.get(m["repo"], ())):
        if b == m["file"] and n in imported_names(repo_m, a, b):
            add(res, "importers", EXACT, f"imports {n}", repo=m["repo"], file=a)
    for same, shared, r, f, t in teammap.mirror_types(repos, n, m)[:3]:
        add(res, "frontend types", INFERRED, ("same base name" if same else "shares fields") + f", {shared} shared field(s)",
            repo=r["name"], file=f, line=t["line"], symbol=t["name"])
    find_tests(res, repos, imports, tests, {(m["repo"], m["file"])}, [n], [])
    _episodes(res, [f"{m['repo']}/{m['file']}"])
    sec = res["sections"]
    res["summary"].append(f"model {n} at {m['repo']}/{m['file']}:{m['line']} ({len(m['fields'])} fields)")
    res["summary"].append(f"{len(sec.get('endpoints', []))} endpoint(s) use it, {len(sec.get('importers', []))} file(s) import it, "
                          f"{len(sec.get('frontend types', []))} likely frontend mirror(s), {len(sec.get('tests', []))} test file(s)")
    if others:
        res["gaps"].append(f"other models also match: {', '.join(others[:8])}")
    if len(L["models"][n]) > 1:
        res["gaps"].append(f"{n} is defined {len(L['models'][n])} times; this result covers {m['repo']}/{m['file']}")
    res["gaps"].append("only models found by type annotations are linked to endpoints; dicts built by hand are not")


def _episodes(res, files):
    for ep in read_episodes():
        touched = [t.strip() for t in ep["TOUCHED"].split(",") if t.strip()]
        if any(f.endswith(t) or t.endswith(os.path.basename(f)) for f in files for t in touched):
            add(res, "episodes", INFERRED, "its TOUCHED list names the file", file=ep["file"], text=f"{ep['DATE']} {ep['TASK']}")


# -- failures ------------------------------------------------------------------------------------------

PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>[^\s]+))?')
PYTEST_FRAME = re.compile(r"^(?P<path>[^\s:]+\.py):(?P<line>\d+):(?: in (?P<func>\S+))?")
JS_FRAME = re.compile(r"(?:\bat (?:(?P<func>[^\s(]+) )?\(?|❯ (?:\S+ (?=\S+\.[cm]?[jt]sx?:\d))?|(?P<loc>location): '?)"
                      r"(?P<path>(?:file:///)?(?:[A-Za-z]:)?[^\s():']+\.[cm]?[jt]sx?):(?P<line>\d+)(?::\d+)?")
JS_PAREN = re.compile(r"(?P<func>[^\s(]+) \((?P<path>(?:file:///)?(?:[A-Za-z]:)?[^\s():']+\.[cm]?[jt]sx?):(?P<line>\d+):\d+\)")
ERROR_LINE = re.compile(r"^\s*(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Failure|Exit|Interrupt|Warning)|AssertionError|assert)\b:?\s*(?P<msg>.*)$")
FORMATS = ("junit", "pytest", "unittest", "vitest", "jest", "tap")


def detect_format(text):
    head = text[:4000]
    if re.search(r"<testsuites?\b", head):
        return "junit"
    if re.search(r"^=+ (FAILURES|ERRORS|short test summary info) =+$", text, re.M) or re.search(r"^(FAILED|ERROR) \S+::", text, re.M):
        return "pytest"
    if re.search(r"^(FAIL|ERROR): \w+ \(", text, re.M):
        return "unittest"
    if re.search(r"^\s*(FAIL|×)\s+\S+\s+>\s+", text, re.M) or "❯ " in text and "Test Files" in text:
        return "vitest"
    if re.search(r"^\s*● .+", text, re.M):
        return "jest"
    if re.search(r"^\s*not ok \d+", text, re.M) or re.search(r"^✖ failing tests:", text, re.M):
        return "tap"
    if re.search(r"^=*\s*\d+ passed\b.* in [\d.]+s", text, re.M) or re.search(r"^Ran \d+ tests? in ", text, re.M) \
            or re.search(r"^\s*(# )?(pass|fail) \d+", text, re.M) or re.search(r"Tests:\s+\d+", text):
        return "passed-only"
    return None


def _frames(lines):
    """Stack frames, outermost first. Python prints them that way; JavaScript prints the innermost first,
    except TAP's `location:` (the test itself), which is the outermost."""
    py, js_loc, js = [], [], []
    for ln in lines:
        for rx, into in ((PY_FRAME, py), (PYTEST_FRAME, py), (JS_FRAME, js), (JS_PAREN, js)):
            m = rx.search(ln)
            if m:
                # TAP/YAML escapes backslashes; pytest on Windows prints app\tests\x.py
                path = m.group("path").replace("file:///", "").replace("\\\\", "\\").replace("\\", "/")
                fr = {"path": path, "line": int(m.group("line")), "func": m.group("func")}
                (js_loc if "loc" in m.groupdict() and m.group("loc") else into).append(fr)
                break
    out = []
    for fr in py + js_loc + js[::-1]:
        if not out or (out[-1]["path"], out[-1]["line"]) != (fr["path"], fr["line"]):  # pytest prints a frame twice
            out.append(fr)
    return out


def _message(lines):
    """(type, first message line) from the error lines of a block; pytest's `E` lines win over everything else."""
    e_lines = [ln[1:] for ln in lines if ln.startswith("E ")]
    for group in ([e_lines] if e_lines else []) + [[ln for ln in lines if not ln.startswith(("E ", ">"))]]:
        for ln in group:
            m = ERROR_LINE.match(ln)
            if m and not ln.lstrip().startswith(("File ", "at ")):
                if m.group("type") == "assert":
                    return "AssertionError", ln.strip()[:400]
                return m.group("type"), (m.group("msg") or ln.strip())[:400]
        if group is e_lines and e_lines:
            return None, e_lines[0].strip()[:400]
    return None, None


def parse_pytest(lines):
    fails, cur = [], None
    in_section = False
    for i, ln in enumerate(lines):
        if re.match(r"^=+ (FAILURES|ERRORS) =+$", ln):
            in_section = True
            continue
        if re.match(r"^=+ ", ln):
            in_section = False
            if cur:
                cur["end"] = i
                cur = None
            continue
        m = re.match(r"^_{3,} (?:(ERROR) (?:at \w+ of |collecting )?)?(.+?) _{3,}$", ln)
        if in_section and m:
            if cur:
                cur["end"] = i
            cur = {"test": m.group(2), "kind": "error" if m.group(1) else "failed", "start": i + 1, "end": None, "lines": []}
            fails.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)
    if cur and cur["end"] is None:
        cur["end"] = len(lines)
    by_name = {f["test"].split("::")[-1]: f for f in fails}
    for i, ln in enumerate(lines):  # short summary: fills message and file, or is all there is (--tb=no)
        m = re.match(r"^(FAILED|ERROR) (\S+?)(?:::(\S+))?(?: - (.*))?$", ln)
        if not m:
            continue
        name = (m.group(3) or m.group(2)).split("::")[-1]
        f = by_name.get(name) or next((x for x in fails if x["test"].endswith(name)), None)
        if not f:
            f = {"test": name, "kind": "failed" if m.group(1) == "FAILED" else "error", "start": i + 1, "end": i + 1, "lines": [ln]}
            fails.append(f)
            by_name[name] = f
        f["file"] = m.group(2)
        if m.group(4) and not f.get("summary_msg"):
            f["summary_msg"] = m.group(4)
    out = []
    for f in fails:
        etype, msg = _message(f["lines"])
        if not msg and f.get("summary_msg"):
            sm = ERROR_LINE.match(f["summary_msg"])
            etype, msg = (sm.group("type"), sm.group("msg")) if sm else (None, f["summary_msg"])
        out.append({"test": f["test"], "kind": f["kind"], "file": f.get("file"), "type": etype, "message": msg,
                    "frames": _frames(f["lines"]), "log_lines": [f["start"], f["end"]]})
    return out


def parse_unittest(lines):
    out = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)", ln)
        if not m:
            continue
        j = i + 1
        if j < len(lines) and re.match(r"^-{20,}$", lines[j]):
            j += 1
        block = []
        while j < len(lines) and not re.match(r"^(={20,}|-{20,})$", lines[j]):
            block.append(lines[j])
            j += 1
        # the exception line is the last unindented one; earlier ones belong to chained exceptions
        tail = [b for b in block if b.strip() and not b.startswith((" ", "Traceback", "During handling", "The above exception"))]
        etype, msg = _message(list(reversed(tail)))
        name = m.group(3) if m.group(3).endswith("." + m.group(2)) else f"{m.group(3)}.{m.group(2)}"
        out.append({"test": name, "kind": "failed" if m.group(1) == "FAIL" else "error", "file": None,
                    "type": etype, "message": msg, "frames": _frames(block), "log_lines": [i + 1, j]})
    return out


def parse_jest(lines):
    out, cur, suite_file = [], None, None
    for i, ln in enumerate(lines):
        mf = re.match(r"^\s*FAIL\s+(\S+\.[cm]?[jt]sx?)\s*$", ln)
        if mf:
            suite_file = mf.group(1)
            continue
        m = re.match(r"^\s*● (.+)$", ln)
        if m and m.group(1).strip() == "Console":
            if cur:
                cur["end"] = i
            cur = None
            continue
        if m:
            if cur:
                cur["end"] = i
            cur = {"test": m.group(1).strip(), "file": suite_file, "start": i + 1, "end": None, "lines": []}
            out.append(cur)
        elif cur is not None:
            if re.match(r"^(Test Suites|Tests):", ln.strip()):
                cur["end"] = i
                cur = None
                continue
            cur["lines"].append(ln)
    for f in out:
        f["end"] = f["end"] or len(lines)
    res, seen = [], set()
    for f in out:
        if (f["file"], f["test"]) in seen:  # "Summary of all failing tests" repeats them
            continue
        seen.add((f["file"], f["test"]))
        body = [l.strip() for l in f["lines"] if l.strip()]
        etype, msg = _message(body)
        if not msg:
            msg = next((l for l in body if not l.startswith("at ")), None)
        res.append({"test": f["test"], "kind": "failed", "file": f["file"], "type": etype, "message": msg,
                    "frames": _frames(f["lines"]), "log_lines": [f["start"], f["end"]]})
    return res


def parse_vitest(lines):
    out, cur = [], None
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(?:FAIL|×)\s+(\S+\.[cm]?[jt]sx?)\s+>\s+(.+?)(?:\s+\d+ms)?$", ln)
        if m:
            if cur:
                cur["end"] = i
            cur = {"test": m.group(2).strip(), "file": m.group(1), "start": i + 1, "end": None, "lines": []}
            out.append(cur)
        elif cur is not None:
            if re.match(r"^\s*(⎯{3,}|Test Files\b|Tests\b)", ln):
                cur["end"] = i
                cur = None
                continue
            cur["lines"].append(ln)
    for f in out:
        f["end"] = f["end"] or len(lines)
    seen, res = {}, []
    for f in out:
        etype, msg = _message([l.strip() for l in f["lines"] if l.strip()])
        key = (f["file"], f["test"])
        if key in seen:  # the run lists a failed test once in the progress and once with its error
            if msg and not seen[key]["message"]:
                seen[key].update(type=etype, message=msg, frames=_frames(f["lines"]), log_lines=[f["start"], f["end"]])
            continue
        seen[key] = {"test": f["test"], "kind": "failed", "file": f["file"], "type": etype, "message": msg,
                     "frames": _frames(f["lines"]), "log_lines": [f["start"], f["end"]]}
        res.append(seen[key])
    return res


def parse_tap(lines):
    out = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)not ok \d+ - (.+?)(?: # .*)?$", ln)
        if not m:
            continue
        indent, block, j = len(m.group(1)), [], i + 1
        while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
            block.append(lines[j])
            j += 1
        text = "\n".join(block)
        if re.search(r"failureType: '?subtestsFailed", text):
            continue  # a suite that failed only because a test in it did; the test is reported on its own
        msg = None
        for k, b in enumerate(block):
            em = re.match(r"^(\s*)error:\s*(.*)$", b)
            if em:
                v = em.group(2).strip()
                if v in ("", "|", "|-", ">", ">-"):  # a YAML block: the indented lines under it
                    deeper = []
                    for x in block[k + 1:]:
                        if x.strip() and len(x) - len(x.lstrip()) <= len(em.group(1)):
                            break
                        if x.strip():
                            deeper.append(x.strip())
                    v = " ".join(deeper[:3])
                msg = v.strip("'\"")[:400] or None
                break
        cm = re.search(r"^\s*code: '?(\w+)", text, re.M)
        tm = re.search(r"^\s*name: '?(\w+)", text, re.M)
        out.append({"test": m.group(2).strip(), "kind": "failed", "file": None, "type": (tm and tm.group(1)) or (cm and cm.group(1)),
                    "message": msg, "frames": _frames(block), "log_lines": [i + 1, j]})
    return out


def parse_junit(text):
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return None, f"not well-formed XML ({e})"
    out = []
    for tc in root.iter("testcase"):
        for kind in ("failure", "error"):
            el = tc.find(kind)
            if el is None:
                continue
            body = (el.text or "").splitlines()
            msg = el.get("message") or next((l.strip() for l in body if l.strip()), None)
            etype = el.get("type")
            if not etype and msg:
                em = ERROR_LINE.match(msg)
                etype = em and em.group("type")
            if etype and msg and msg.startswith(etype + ":"):
                msg = msg[len(etype) + 1:].strip()
            out.append({"test": ".".join(x for x in (tc.get("classname"), tc.get("name")) if x), "kind": "failed" if kind == "failure" else "error",
                        "file": tc.get("file"), "type": etype, "message": (msg or "")[:400] or None, "frames": _frames(body), "log_lines": None})
    return out, None


def _normalize(msg):
    s = msg or ""
    s = re.sub(r"0x[0-9a-fA-F]+", "0x…", s)
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s)
    s = re.sub(r"(['\"]).*?\1", "'…'", s)
    s = re.sub(r"\d+(\.\d+)?", "N", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


def _frame_loc(fr):
    return "/".join(x for x in (fr.get("repo"), fr["rel"]) if x) + f":{fr['line']}"


def _is_library(path):
    p = norm(path)
    return bool(re.search(r"(^|/)(site-packages|dist-packages|node_modules|lib/python\d[\d.]*|<frozen )", p)) \
        or bool(re.match(r"^internal/.*\.[cm]?js$", p)) \
        or p.startswith(("node:", "<"))


def _map_frame(fr, repos_by_name, base_repo):
    """repo/rel for a stack frame inside the workspace's repos; None for libraries and unknown paths."""
    p = norm(fr["path"])
    absolute = os.path.isabs(fr["path"]) or re.match(r"^[a-zA-Z]:", p)
    order = ([base_repo] if base_repo else []) + [n for n in repos_by_name if n != base_repo]
    if not absolute:
        p = p.lstrip("./")
        for name in order:
            r = repos_by_name.get(name)
            if r and p in r["files"]:  # indexed project files take precedence over Node's internal/ paths
                return name, p
    if _is_library(p):
        return None
    if absolute:
        rel = relative(p, CODE_ROOT)
        if rel:
            repo, _, rest = rel.partition("/")
            if repo in repos_by_name:
                return repo, rest
        for name, r in repos_by_name.items():  # a run inside a repo prints that repo's absolute paths
            rr = relative(p, r["root"])
            if rr:
                return name, rr
        return None
    for name in order:
        r = repos_by_name.get(name)
        if r and os.path.isfile(os.path.join(r["root"], p)):
            return name, p
    return None


def failures(text, fmt=None, repos=(), base_repo=None, source=None):
    res = new_result("failures", {"format": fmt, "repo": base_repo, "source": source})
    lines = text.splitlines()
    found = fmt or detect_format(text)
    res["params"]["detected_format"] = found
    if found is None:
        res["status"] = "unrecognized"
        res["summary"].append("no known test output format found; nothing was parsed")
        res["gaps"].append(f"tried: {', '.join(FORMATS)}; pass --format if you know it. Lines that look like errors were not guessed at.")
        return res, None
    if found == "passed-only":
        res["summary"].append("the output has a test summary but no failed tests")
        tail = next((l.strip() for l in reversed(lines) if re.search(r"\b(passed|failed|pass|fail|Ran \d+)\b", l)), "")
        if tail:
            res["summary"].append(f"summary line: {tail[:200]}")
        return res, []
    if found == "junit":
        fails, err = parse_junit(text)
        if fails is None:
            res["status"] = "unrecognized"
            res["summary"].append(f"JUnit XML could not be read: {err}")
            return res, None
    else:
        fails = {"pytest": parse_pytest, "unittest": parse_unittest, "jest": parse_jest, "vitest": parse_vitest, "tap": parse_tap}[found](lines)
    if not fails:
        res["status"] = "unrecognized" if fmt else "ok"
        res["summary"].append(f"format {found}, but no failed test could be read from it")
        res["gaps"].append("the output may be cut short, or use a reporter this parser doesn't know")
        return res, []
    by_name = {r["name"]: r for r in repos}
    for f in fails:
        for fr in f["frames"]:
            hit = _map_frame(fr, by_name, base_repo)
            if hit:
                fr["repo"], fr["rel"] = hit
                fr["symbol"] = enclosing_symbol(by_name, hit[0], hit[1], fr["line"])
            elif not _is_library(fr["path"]):  # project code the index doesn't cover: still worth showing, as is
                fr["rel"], fr["outside"] = norm(fr["path"]), True
    groups = defaultdict(list)
    for f in fails:
        own = [fr for fr in f["frames"] if fr.get("repo")] or [fr for fr in f["frames"] if fr.get("outside")]
        f["shown_frames"] = own
        raised = own[-1] if own else None  # the innermost project frame: where the error surfaced
        f["raised_at"] = raised
        groups[(f["type"], _normalize(f["message"]), raised and (raised.get("repo"), raised["rel"], raised["line"]))].append(f)
    ordered = sorted(groups.values(), key=lambda g: -len(g))
    for g in ordered:
        f = g[0]
        r = f["raised_at"] or {}
        row = add(res, "groups", EXACT, "same error type, message (numbers and quoted values ignored) and innermost project frame"
                  + (" (a path outside the indexed repos)" if r.get("outside") else ""),
                  repo=r.get("repo"), file=r.get("rel"), line=r.get("line"), symbol=r.get("symbol"),
                  text=f"{f['type'] + ': ' if f['type'] else ''}{f['message'] or '(no message found)'}",
                  count=len(g), tests=[x["test"] for x in g], log_lines=f["log_lines"])
        row["root_cause"] = None  # this tool reports where errors surface; it doesn't guess why
    for f in fails:
        add(res, "failed tests", EXACT, f"{f['kind']} in the {found} output", file=f.get("file"),
            symbol=f["test"], text=f"{f['type'] + ': ' if f['type'] else ''}{f['message'] or '(no message found)'}",
            log_lines=f["log_lines"], frames=[{k: fr.get(k) for k in ("repo", "rel", "line", "symbol", "func", "outside")}
                                              for fr in f["shown_frames"]][-6:])
    no_frames = sum(1 for f in fails if not f["raised_at"])
    outside = sum(1 for f in fails if f["raised_at"] and f["raised_at"].get("outside"))
    if outside:
        res["gaps"].append(f"{outside} failure(s) point at files outside the indexed repos (shown as printed, without "
                           "the enclosing function); run from the repo or pass --repo NAME if they belong to one")
    res["summary"].append(f"{len(fails)} failed test(s) in {len(ordered)} group(s) ({found} output)")
    if ordered and len(ordered[0]) > 1:
        g = ordered[0]
        res["summary"].append(f"largest group: {len(g)} tests with {g[0]['type'] or 'the same error'}"
                              + (f" at {_frame_loc(g[0]['raised_at'])}" if g[0]["raised_at"] else ""))
    res["summary"].append("root cause: not determined; frames show where each error surfaced, not why")
    if no_frames:
        res["gaps"].append(f"{no_frames} failure(s) have no stack frame inside the indexed repos (library code only, "
                           "paths that don't resolve, or a reporter without stack traces)")
    if found == "junit":
        res["gaps"].append("JUnit XML has no line numbers into the original console output")
    cut = sum(1 for f in fails if (f["message"] or "").endswith("...") or (f["type"] or "").endswith("..."))
    if cut:
        res["gaps"].append(f"{cut} message(s) were shortened by the test runner itself (it fits lines to the terminal); "
                           "the full message isn't in this output: rerun with tracebacks (pytest --tb=short) for it")
    return res, fails


# -- relevant ------------------------------------------------------------------------------------------

STOP = set("""a an and are as at be but by can do does for from has have how i if in into is it its make me my
need no not of on or our please should so that the their them then there these this to use using was we what when
where which while who why will with you your add fix change update implement support new make sure get set also all
ve bir bu şu için ile da de mi mı mu mü ne olarak gibi daha çok en her ama veya yada ya ki o şey olan olsun nasıl
neden yap yapalım ekle düzelt değiştir lazım gerek istiyorum""".split())


def tokens(text):
    """Lowercased words, with camelCase, snake_case and paths split; short and stop words dropped."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    out = []
    for w in re.findall(r"[^\W_]+", s):
        w = w.casefold()
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        out.append(_stem(w))
    return out


def _stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _match(q, words):
    """1 for an exact word, .5 for a shared prefix of at least 5 letters (product/products, ürün/ürünler), else 0."""
    if q in words:
        return 1.0
    if len(q) >= 5 and any(len(w) >= 5 and (w.startswith(q) or q.startswith(w)) for w in words):
        return 0.5
    return 0.0


def relevant(repos, L, query, only=None):
    res = new_result("relevant", {"query": query, "repo": only})
    q = list(dict.fromkeys(tokens(query)))
    raw = query.strip()
    if not q and not raw:
        res["status"] = "not-found"
        res["summary"].append("empty query")
        return res
    docs = []  # (section, fields {name: (weight, words)}, finding kwargs)
    for r in repos:
        if only and r["name"] != only:
            continue
        for rel, e in r["files"].items():
            path_words = set(tokens(rel))
            docs.append(("files", {"path": (2.0, path_words)}, dict(repo=r["name"], file=rel)))
            for s in e["data"].get("symbols", []):
                docs.append(("symbols", {"name": (3.0, set(tokens(s["name"]))), "class": (1.0, set(tokens(s["qual"]))), "path": (0.5, path_words)},
                             dict(repo=r["name"], file=rel, line=s["line"], symbol=s["qual"], text=s["kind"])))
    for e in L["endpoints"]:
        if only and e["repo"] != only:
            continue
        docs.append(("endpoints", {"path": (3.0, set(tokens(e["path"]))), "handler": (2.0, set(tokens(e["handler"])))},
                     dict(repo=e["repo"], file=e["file"], line=e["line"], symbol=e["handler"], text=f"{e['method']} {e['path']}")))
    for n, defs in L["models"].items():
        m = defs[0]
        if only and m["repo"] != only:
            continue
        docs.append(("models", {"name": (3.0, set(tokens(n))), "fields": (0.5, set(tokens(" ".join(f["name"] for f in m["fields"]))))},
                     dict(repo=m["repo"], file=m["file"], line=m["line"], symbol=n)))
    for brief in sorted(glob(os.path.join(KNOWLEDGE, "*.md"))):
        if brief.endswith(".index.md"):
            continue
        name = os.path.basename(brief)
        for lvl, title, ln, text in brief_sections(name):
            body = tokens(text)
            docs.append(("brief sections", {"title": (3.0, set(tokens(title))), "body": (1.0 / (1 + len(body) / 400), set(body))},
                         dict(file=brief, line=ln, symbol=f"{name} § {title}", text=f"~{len(text) // 4} tokens by a 4-chars estimate")))
    for ep in read_episodes():
        docs.append(("episodes", {"task": (3.0, set(tokens(ep["TASK"]))), "touched": (1.5, set(tokens(ep["TOUCHED"]))),
                                  "notes": (1.0, set(tokens(" ".join(ep[k] for k in ("DECISIONS", "GOTCHAS", "OUTCOME")))))},
                     dict(file=ep["file"], symbol=ep["name"], text=f"{ep['DATE']} {ep['TASK']}")))
    df = Counter()
    for _, fields, _ in docs:
        seen = set()
        for _, words in fields.values():
            seen |= words
        for t in q:
            if _match(t, seen):
                df[t] += 1
    import math
    N = max(1, len(docs))
    idf = {t: math.log(1 + N / (1 + df[t])) for t in q}
    scored = []
    for section, fields, kw in docs:
        score, why = 0.0, []
        for t in q:
            best = max(((w * _match(t, words), fname) for fname, (w, words) in fields.items()), default=(0, None))
            if best[0]:
                score += best[0] * idf[t]
                why.append(f"{t} ({best[1]}{'' if best[0] >= 1 * fields[best[1]][0] else ', prefix'})")
        if section == "symbols" and kw.get("symbol") and raw in (kw["symbol"], kw["symbol"].split(".")[-1]):
            score += 10
            why.insert(0, "exact name")
        if score > 0:
            scored.append((score, section, kw, why))
    scored.sort(key=lambda x: -x[0])
    # files: their own path plus their best symbol, so a file full of matching code ranks above a matching name alone
    file_score = defaultdict(float)
    file_why = defaultdict(list)
    for score, section, kw, why in scored:
        if section in ("files", "symbols", "endpoints") and kw.get("file"):
            key = (kw["repo"], kw["file"])
            file_score[key] += score if section == "files" else score * .5
            label = f"{kw.get('symbol') or kw.get('text')}"
            if section != "files" and len(file_why[key]) < 3 and label not in file_why[key]:
                file_why[key].append(label)
            elif section == "files":
                file_why[key].insert(0, "path: " + ", ".join(why))
    for (repo, rel), s in sorted(file_score.items(), key=lambda x: -x[1])[:60]:
        add(res, "files", INFERRED, "; ".join(file_why[(repo, rel)]) or "path", repo=repo, file=rel, score=round(s, 2))
    for score, section, kw, why in scored:
        if section == "files":
            continue
        if len(res["sections"].get(section, [])) >= 60:
            continue
        add(res, section, INFERRED, "matched: " + ", ".join(why), score=round(score, 2), **kw)
    matched = {t for _, _, _, why in scored for t in q if any(w.startswith(t + " ") for w in why)}
    if not scored:
        res["status"] = "not-found"
        res["summary"].append(f"nothing in the index, briefs or episodes shares a word with {query!r}")
    else:
        top = res["sections"].get("files", [])[:3]
        res["summary"].append(f"{sum(len(v) for v in res['sections'].values())} candidate(s); top files: "
                              + ", ".join(f"{t['repo']}/{t['file']}" for t in top))
    unmatched = [t for t in q if t not in matched]
    if unmatched:
        res["gaps"].append(f"no match for: {', '.join(unmatched)}")
    res["gaps"].append("ranking is lexical (shared words, camelCase/snake_case split, prefixes of 5+ letters); "
                       "it doesn't know synonyms or translate, so a task described in another language than the code "
                       "matches only the words they share")
    if not glob(os.path.join(KNOWLEDGE, "*.md")):
        res["gaps"].append("no knowledge briefs yet")
    if not os.path.isdir(EPISODES) or not glob(os.path.join(EPISODES, "*.md")):
        res["gaps"].append("no episodes yet")
    res["params"]["words"] = q
    return res


def knowledge_fingerprint():
    files = sorted(glob(os.path.join(KNOWLEDGE, "*.md")) + glob(os.path.join(EPISODES, "*.md")))
    return [(os.path.basename(f), os.path.getmtime(f)) for f in files if os.path.exists(f)]


# -- output ------------------------------------------------------------------------------------------

ORDER = ["candidates", "definition", "groups", "failed tests", "files", "symbols", "endpoints", "models", "consumers",
         "callers", "importers", "subclasses", "frontend types", "tests", "brief sections", "episodes"]


def limited(res, limit):
    """A copy with at most `limit` rows per section, and what was cut."""
    out = dict(res)
    out["sections"], cut = {}, {}
    for k, rows in res["sections"].items():
        out["sections"][k] = rows if limit is None else rows[:limit]
        if limit is not None and len(rows) > limit:
            cut[k] = {"shown": limit, "total": len(rows)}
    out["truncated"] = cut or None
    return out


def _loc(row):
    where = "/".join(x for x in (row.get("repo"), row.get("file")) if x)
    if where and row.get("line"):
        where += f":{row['line']}"
    return where


def render(res):
    lines = []
    tgt = res.get("target") or {}
    head = f"{res['tool']}"
    if tgt:
        head += " · " + " ".join(str(tgt[k]) for k in ("kind", "method", "path", "name") if tgt.get(k))
    else:
        head += " · " + " ".join(f"{v}" for k, v in res["params"].items() if v and k in ("query", "source", "detected_format"))
    state = "stored result, inputs unchanged" if res.get("cached") else "computed now"
    lines.append(f"{head}   [{res['status']}, {state}, id {res.get('id') or '-'}]")
    for s in res["summary"]:
        lines.append(f"  {s}")
    if res["sections"]:
        lines.append("  (= exact: from the code's structure · ~ inferred: by name, URL or text)")
    for k in sorted(res["sections"], key=lambda k: ORDER.index(k) if k in ORDER else 99):
        rows = res["sections"][k]
        cut = (res.get("truncated") or {}).get(k)
        lines.append(f"{k}" + (f" ({cut['shown']} of {cut['total']})" if cut else f" ({len(rows)})"))
        for r in rows:
            mark = "=" if r["certainty"] == EXACT else "~"
            bits = [b for b in (_loc(r), r.get("symbol") and f"{r['symbol']}", r.get("text")) if b]
            extra = ""
            if r.get("count"):
                extra += f" ×{r['count']}"
            if r.get("log_lines"):
                extra += f" [log {r['log_lines'][0]}-{r['log_lines'][1]}]"
            lines.append(f"  {mark} {'  '.join(bits)}{extra}")
            lines.append(f"      {r['why']}" if k not in ("failed tests", "groups") else
                         (f"      tests: {', '.join(r['tests'][:6])}{' …' if len(r['tests']) > 6 else ''}" if r.get("tests") else
                          ("      at " + " → ".join(f"{f['rel']}:{f['line']}" + (f" {f['symbol']}" if f.get('symbol') else "") for f in r["frames"][-3:]) if r.get("frames") else "      (no frame in the repos)")))
    if res["gaps"]:
        lines.append("gaps")
        for g in res["gaps"]:
            lines.append(f"  ! {g}")
    tail = []
    if res.get("truncated"):
        tail.append("more rows: --all, or `teammap research show " + (res.get("id") or "<id>") + "`")
    if res.get("log"):
        tail.append(f"full log: {res['log']}")
    if res.get("record"):
        tail.append(f"record: {res['record']}")
    fp = res.get("inputs", {}).get("fingerprint")
    if fp:
        tail.append(f"inputs {fp[:12]}")
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


def emit(res, as_json, limit, max_chars):
    out = limited(res, limit)
    if as_json:
        text = json.dumps(out, indent=1, default=str)
    else:
        text = render(out)
    if max_chars and len(text) > max_chars and not as_json:
        keep = text[:max_chars].rsplit("\n", 1)[0]
        dropped = text.count("\n") - keep.count("\n")
        text = keep + f"\n… output cut at {max_chars} characters ({dropped} more lines); full result: " + \
            (f"`teammap research show {res.get('id')}`" if res.get("id") else "--all")
    print(text)


# -- main -------------------------------------------------------------------------------------------

def _parse(argv):
    parser = argparse.ArgumentParser(prog="teammap", add_help=False, allow_abbrev=False)
    for name in ("json", "all", "fresh"):
        parser.add_argument("--" + name, action="store_true")
    for name in ("limit", "max-chars"):
        parser.add_argument("--" + name, type=int)
    parser.add_argument("--repo", choices=teammap.REPOS)
    for name in ("format", "kind", "section"):
        parser.add_argument("--" + name)
    opts, args = parser.parse_known_args(argv)
    return args, vars(opts)


def _run_cmd(command):
    if not command:
        print("usage: teammap run -- <command...>")
        return 2
    shown = " ".join(command)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", shown).strip("-._")[:60] or "command"
    log_path = norm(os.path.join(STORE, "runs", f"{stamp}-{slug}.log"))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    code = 0
    try:
        executable = shutil.which(command[0]) or command[0]
        with open(log_path, "wb") as log:
            try:
                proc = subprocess.run([executable, *command[1:]], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, check=False)
                code = 128 + abs(proc.returncode) if proc.returncode < 0 else proc.returncode
            except KeyboardInterrupt:
                code = 130
            except OSError as e:
                log.write((str(e) + "\n").encode("utf-8", errors="replace"))
                code = 127
    except OSError as e:
        print(f"could not save log: {e}")
        return 1
    try:
        logs = sorted(glob(os.path.join(STORE, "runs", "*.log")))
        for old in logs[:-50]:
            try:
                os.remove(old)
            except OSError:
                # Log retention is best-effort; keep the command result even if an old log cannot be removed.
                pass
    except OSError:
        # A failed retention scan must not hide the command result or its log.
        pass
    max_bytes = 2_000_000
    offset_lines = 0
    try:
        with open(log_path, "rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            if size > max_bytes:
                start = size - max_bytes
                log.seek(0)
                remaining = start
                while remaining:
                    chunk = log.read(min(65536, remaining))
                    if not chunk:
                        break
                    offset_lines += chunk.count(b"\n")
                    remaining -= len(chunk)
                log.seek(start)
                data = log.read(max_bytes)
                if b"\n" in data:
                    data = data.split(b"\n", 1)[1]
                    offset_lines += 1
                else:
                    offset_lines = 0
            else:
                log.seek(0)
                data = log.read(max_bytes)
    except OSError as e:
        print(f"command: {shown}\nexit code: {code}\nfull log: {log_path}\nCould not read log: {e}")
        return code
    output = data.decode("utf-8", errors="replace")
    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)
    print(f"command: {shown}\nexit code: {code}")
    if offset_lines:
        print("log excerpt: last 2 MB; line numbers below are absolute in the full log")
    repos = []
    if detect_format(output) not in (None, "passed-only"):
        repos, _, _ = load_index()
    result, _ = failures(output, repos=repos)
    groups = result.get("sections", {}).get("groups", [])
    if code != 0 and not groups:
        print("output tail (last 30 lines):")
        print("\n".join(output.splitlines()[-30:]) if output.splitlines() else "(no output)")
    elif result["status"] != "unrecognized":
        for line in result["summary"][:2]:
            print(f"tests: {line}")
        for group in groups[:10]:
            location = "/".join(x for x in (group.get("repo"), group.get("file")) if x)
            if location and group.get("line"):
                location += f":{group['line']}"
            tests = ", ".join(group.get("tests", [])[:5])
            if len(group.get("tests", [])) > 5:
                tests += ", …"
            print(f"  {group.get('count', 1)}× {location + ': ' if location else ''}{group.get('text', '(failure)')}")
            if tests:
                print(f"    tests: {tests}")
            lines = group.get("log_lines")
            if lines:
                print(f"    log lines: {lines[0] + offset_lines}-{lines[1] + offset_lines}")
    else:
        print("output tail (last 30 lines):")
        print("\n".join(output.splitlines()[-30:]) if output.splitlines() else "(no output)")
    print(f"full log: {log_path}")
    return code

def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if argv and argv[0] == "run":
        if len(argv) < 2 or argv[1] != "--":
            print("usage: teammap run -- <command...>")
            return 2
        return _run_cmd(argv[2:])
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, (args, opts) = argv[0], _parse(argv[1:])
    default = {"research": None, "relevant": 5}.get(cmd, 8)  # relevant is a starting point: fewer rows per section
    limit = None if opts["all"] else (opts["limit"] or default)
    if cmd == "research":
        return _research_cmd(args, opts, limit)
    if cmd == "failures":
        return _failures_cmd(args, opts, limit)
    if cmd not in ("impact", "relevant"):
        print(f"unknown research command {cmd!r}; one of: impact, failures, relevant, research")
        return 2
    if not args:
        print(f"usage: teammap {cmd} <query>   (teammap {cmd} --help)" if cmd == "relevant" else
              "usage: teammap impact <path-part|symbol|Model> [METHOD]")
        return 2
    repos, L, gaps = load_index(opts["repo"])
    if cmd == "impact":
        query = args[0]
        method = args[1].upper() if len(args) > 1 else None
        params = {"query": query, "method": method, "kind": opts["kind"], "repo": opts["repo"]}
        fp = index_fingerprint(repos, [knowledge_fingerprint()])
        res = stored_or_compute("impact", params, fp, lambda: _with(impact(repos, L, query, method, opts["kind"], opts["repo"]), repos, gaps),
                                opts["fresh"])
    else:
        query = " ".join(args)
        params = {"query": query, "repo": opts["repo"]}
        fp = index_fingerprint(repos, [knowledge_fingerprint()])
        res = stored_or_compute("relevant", params, fp, lambda: _with(relevant(repos, L, query, opts["repo"]), repos, gaps), opts["fresh"])
    emit(res, opts["json"], limit, opts["max_chars"])
    return 0 if res["status"] in ("ok", "ambiguous") else 1


def _with(res, repos, gaps):
    res["inputs"].update(inputs_of(repos))
    res["gaps"] = gaps + res["gaps"]
    if not repos and res["status"] == "ok":
        res["status"] = "no-index"
    return res


def _failures_cmd(args, opts, limit):
    if not args:
        print("usage: teammap failures <test-output-file | -> [--format pytest|unittest|jest|vitest|tap|junit] [--repo NAME]")
        return 2
    if opts["format"] and opts["format"] not in FORMATS:
        print(f"--format must be one of: {', '.join(FORMATS)}")
        return 2
    src = args[0]
    if src == "-":
        data = sys.stdin.buffer.read()
    else:
        try:
            with open(src, "rb") as f:
                data = f.read(200_000_000)
        except OSError as e:
            print(f"cannot read {src}: {e}")
            return 1
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)  # colours
    digest = hashlib.sha256(data).hexdigest()
    log_path = norm(os.path.join(STORE, "logs", digest[:16] + ".log"))
    try:
        if not os.path.exists(log_path):
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
    except OSError:
        log_path = None
    fmt_found = opts["format"] or detect_format(text)
    repos, gaps = [], []
    if fmt_found not in (None, "passed-only"):
        repos, _, gaps = load_index()
    by_name = {r["name"]: r for r in repos}
    pre_res, fails = failures(text, opts["format"], repos, opts["repo"], os.path.abspath(src) if src != "-" else "stdin")
    # validity: the log's content, the parser version, and the files its frames point into
    touched = sorted({(fr["repo"], fr["rel"]) for f in (fails or []) for fr in f["frames"] if fr.get("repo")})
    deps = [(r, p, by_name[r]["files"].get(p, {}).get("mtime")) for r, p in touched]
    params = {"log_sha256": digest, "format": opts["format"], "repo": opts["repo"]}
    fp = _sha(digest, deps, teammap.VERSION, TOOL_CODE)

    def compute():
        pre_res["inputs"].update({"log_sha256": digest, "log_bytes": len(data), "frame_files": [f"{r}/{p}" for r, p, _ in deps]})
        if repos:
            pre_res["inputs"].update(inputs_of([by_name[r] for r in {r for r, _, _ in deps}]))
        pre_res["gaps"] = gaps + pre_res["gaps"]
        pre_res["log"] = log_path
        return pre_res
    res = stored_or_compute("failures", params, fp, compute, opts["fresh"])
    res["log"] = res.get("log") or log_path
    emit(res, opts["json"], limit, opts["max_chars"])
    return 0 if res["status"] == "ok" else 1


def _research_cmd(args, opts, limit):
    sub = args[0] if args else "list"
    if sub == "list":
        rows = []
        for p in glob(os.path.join(STORE, "*", "*.json")):
            r = _load(p)
            if r:
                rows.append((r.get("created") or "", r["tool"], r.get("id"), r["params"].get("query") or r["params"].get("source") or "", r.get("status")))
        if opts["json"]:
            print(json.dumps([dict(zip(("created", "tool", "id", "query", "status"), r)) for r in sorted(rows, reverse=True)], indent=1))
        else:
            for r in sorted(rows, reverse=True)[:limit or None]:
                print(f"{r[0]}  {r[1]:9} {r[2]}  {r[4]:12} {r[3]}")
            if not rows:
                print(f"no stored results in {STORE}")
        return 0
    if sub == "show" and len(args) > 1:
        path = next(iter(glob(os.path.join(STORE, "*", args[1] + ".json"))), None)
        rec = path and _load(path)
        if not rec:
            print(f"no stored result {args[1]!r} in {STORE}")
            return 1
        if opts["section"]:
            rec["sections"] = {k: v for k, v in rec["sections"].items() if k == opts["section"]}
        emit(rec, opts["json"], limit, opts["max_chars"])
        if not opts["json"]:
            print(f"(stored at {rec.get('created')}; `show` doesn't re-check it against the code: rerun the command for that)")
        return 0
    print("usage: teammap research list | show <id> [--section S] [--json]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
