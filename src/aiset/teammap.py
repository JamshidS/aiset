#!/usr/bin/env python3
"""teammap: a code map of the team's repositories (Python and TypeScript), for agents and people.

Answers "where is X and who uses it" in one call, instead of a string of greps and file reads.

  teammap endpoint <path-part> [METHOD]  endpoint → handler, request/response models, frontend callers
  teammap symbol <name>                  definition(s), signature, callers, callees
  teammap arch [repo]                    what each repo is built with, the infrastructure it uses (DB, queues,
                                         auth…) and which repos it calls; one repo in detail with its name
  teammap arch <component>               who uses it: teammap arch rabbitmq, teammap arch mongodb
  teammap arch impact <service>          who calls it, and who calls those (what a change can break)
  teammap arch path <from> <to>          the HTTP hops from one service to another, with evidence
  teammap code <name> [--max N]          the source of one definition, with line numbers (just that function;
                                         for a class, its header and method list). Replaces grep + sed/Read.
  teammap model <Name>                   fields, endpoints using it, the matching frontend type and field diff
  teammap file <path-part>               symbols, routes and API calls in a file, who imports it
  teammap search <text>                  names that match (symbols, endpoints, models, types)
  teammap index [--force]                refresh the index and the Brain graph, print counts

Research (one call instead of a string of greps and reads; exact vs inferred findings, gaps, a stored result id):
  teammap impact <path-part|symbol|Model> [METHOD]  what a change can reach: consumers, models, importers, tests
  teammap failures <test-output-file>    failed tests, messages, in-repo frames, repeated errors grouped
  teammap relevant "<task text>"         files, symbols, endpoints, brief sections and episodes to read first
  teammap research show <id>             a stored result in full     (teammap impact --help for all options)

Options: --repo <name> limits a query to one repository; --limit N (default 5) caps the matches shown.

Exit status for `teammap code` and `teammap symbol`:
  0  one match, or multiple matches in the same file, were shown
  2  the command or its arguments are invalid
  3  the query matches definitions in different files; qualify the name or add --repo
  4  no symbol matches; try `teammap search <text>`

The index lives in <team>/graph/index/ (see workspace.py) and refreshes itself on every query: only files whose
mtime changed are parsed again (Python with ast, TypeScript with the repo's own typescript package).
Each refresh also writes <team>/graph/<repo>.json, which the office's Brain page shows.
"""
import ast
import json
import os
import posixpath
import re
import subprocess
import sys
from collections import defaultdict

if __name__ == "__main__" and not __package__:  # run as a file: python src/aiset/teammap.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    __package__ = "aiset"
from .workspace import APP_DIR, CODE_ROOT, GRAPHS, roster_repos  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))

INDEX = os.path.join(GRAPHS, "index")
# Index only the repositories configured in the active roster.
REPOS = roster_repos()
HTTP = {"get", "post", "put", "patch", "delete", "head", "options"}
VERSION = 6


# -- Python extraction ---------------------------------------------------------------------

def _str(node):
    """A literal string, or {"ref": NAME} for settings.NAME / NAME, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute):
        return {"ref": node.attr}
    if isinstance(node, ast.Name):
        return {"ref": node.id}
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _kw(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _src(node):
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def py_extract(text):
    tree = ast.parse(text)
    f = {"symbols": [], "routes": [], "routers": {}, "includes": [], "apps": {}, "mounts": [],
         "consts": {}, "imports": []}

    def route_from(call, cls, handler=None, line=None):
        """call is `<router>.<method>(path, ...)`; handler given for the chained style."""
        fn = call.func
        method = fn.attr
        path = _str(call.args[0]) if call.args else _str(_kw(call, "path")) or ""
        rm = _kw(call, "response_model")
        return {"method": method.upper(), "path": path if isinstance(path, str) else "", "router": _src(fn.value),
                "cls": cls, "handler": handler, "line": line or call.lineno,
                "response": _src(rm) if rm is not None else ""}

    def visit(node, stack, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                mod = child.module if isinstance(child, ast.ImportFrom) else None
                lvl = child.level if isinstance(child, ast.ImportFrom) else 0
                for a in child.names:
                    f["imports"].append({"module": mod if mod is not None else a.name, "level": lvl,
                                         "name": a.name if mod is not None else None, "as": a.asname or a.name})
            if isinstance(child, ast.ClassDef):
                fields = []
                for b in child.body:
                    if isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name):
                        fields.append({"name": b.target.id, "type": _src(b.annotation)[:80], "default": b.value is not None})
                        if isinstance(b.value, ast.Constant) and isinstance(b.value.value, str):
                            f["consts"][b.target.id] = b.value.value
                f["symbols"].append({"name": child.name, "qual": ".".join(stack + [child.name]), "kind": "class",
                                     "line": child.lineno, "end": child.end_lineno,
                                     "bases": [_src(x) for x in child.bases], "fields": fields})
                visit(child, stack + [child.name], child.name)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = ".".join(stack + [child.name])
                params = [{"name": a.arg, "type": _src(a.annotation)[:80] if a.annotation else ""}
                          for a in child.args.args + child.args.kwonlyargs if a.arg not in ("self", "cls")]
                calls = []
                for n in ast.walk(child):
                    if isinstance(n, ast.Call):
                        nm = n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id if isinstance(n.func, ast.Name) else None
                        if nm and nm not in calls:
                            calls.append(nm)
                f["symbols"].append({"name": child.name, "qual": qual, "kind": "method" if cls else "function",
                                     "line": child.lineno, "end": child.end_lineno, "params": params, "calls": calls[:80]})
                for d in child.decorator_list:
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in HTTP | {"api_route"}:
                        r = route_from(d, cls, qual, child.lineno)
                        f["routes"].append(r)
                visit(child, stack + [child.name], cls)
                continue
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                val = child.value
                for t in targets:
                    tname = _src(t)
                    if isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
                        if val.func.id == "APIRouter":
                            key = f"self@{cls}" if tname.startswith("self.") and cls else tname
                            f["routers"][key] = _str(_kw(val, "prefix")) or ""
                        elif val.func.id == "FastAPI":
                            f["apps"][tname] = _str(_kw(val, "root_path")) or ""
                    if isinstance(t, ast.Name) and isinstance(val, ast.Constant) and isinstance(val.value, str) and not stack:
                        f["consts"][t.id] = val.value
            if isinstance(child, ast.Call):
                fn = child.func
                if isinstance(fn, ast.Attribute) and fn.attr == "include_router" and child.args:
                    f["includes"].append({"target": _src(fn.value), "arg": _src(child.args[0]),
                                          "prefix": _str(_kw(child, "prefix")) or "", "cls": cls})
                elif isinstance(fn, ast.Attribute) and fn.attr == "mount" and len(child.args) >= 2:
                    f["mounts"].append({"target": _src(fn.value), "path": _str(child.args[0]) or "", "app": _src(child.args[1])})
                elif isinstance(fn, ast.Attribute) and fn.attr == "add_api_route" and len(child.args) >= 2:
                    methods = _kw(child, "methods")
                    ms = [m.value for m in getattr(methods, "elts", []) if isinstance(m, ast.Constant)] or ["GET"]
                    for m in ms:
                        f["routes"].append({"method": m.upper(), "path": _str(child.args[0]) or "", "router": _src(fn.value),
                                            "cls": cls, "handler": _src(child.args[1]).replace("self.", ""),
                                            "line": child.lineno, "response": _src(_kw(child, "response_model") or ast.Constant(""))})
                elif (len(child.args) >= 3 and isinstance(child.args[0], ast.Constant) and str(child.args[0].value).lower() in HTTP
                      and isinstance(child.args[1], ast.Constant) and isinstance(child.args[1].value, str)
                      and isinstance(child.args[2], (ast.Attribute, ast.Name))):  # add("get", "/x", self.handler) helpers
                    h = _src(child.args[2]).replace("self.", "")
                    rm = _kw(child, "response_model")
                    f["routes"].append({"method": str(child.args[0].value).upper(), "path": child.args[1].value,
                                        "router": "self.router" if cls else _src(fn), "cls": cls,
                                        "handler": f"{cls}.{h}" if cls else h, "line": child.lineno,
                                        "response": _src(rm) if rm is not None else ""})
                elif (isinstance(fn, ast.Call) and isinstance(fn.func, ast.Attribute) and fn.func.attr in HTTP
                      and child.args):  # router.post("/x")(self.handler)
                    h = _src(child.args[0]).replace("self.", "")
                    f["routes"].append(route_from(fn, cls, f"{cls}.{h}" if cls else h, child.lineno))
            visit(child, stack, cls) if not isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else None

    visit(tree, [], None)
    return f


# -- index with an mtime cache ---------------------------------------------------------------

def git_files(repo, patterns):
    try:
        out = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off", "ls-files", "-z", *patterns],
                             capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [rel for rel in out.split("\0") if rel and "/node_modules/" not in rel and not rel.endswith(".d.ts")]


def load_repo(name, force=False):
    root = os.path.join(CODE_ROOT, name)
    cache_path = os.path.join(INDEX, name + ".json")
    cache = {"version": VERSION, "files": {}}
    if not force:
        try:
            with open(cache_path, encoding="utf-8") as fh:
                c = json.load(fh)
            if c.get("version") == VERSION:
                cache = c
        except (OSError, ValueError):
            pass  # no usable cache: every file is parsed again
    files = {rel: "py" for rel in git_files(root, ["*.py"])}
    files.update({rel: "ts" for rel in git_files(root, ["*.ts", "*.tsx"])})
    old, new, todo_ts, changed = cache["files"], {}, [], 0
    for rel, lang in files.items():
        try:
            mt = os.path.getmtime(os.path.join(root, rel))
        except OSError:
            continue
        hit = old.get(rel)
        if hit and hit["mtime"] == mt:
            new[rel] = hit
            continue
        changed += 1
        if lang == "py":
            try:
                with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
                    data = py_extract(fh.read())
            except (SyntaxError, ValueError, RecursionError):
                data = {"error": "parse"}
            new[rel] = {"mtime": mt, "lang": "py", "data": data}
        else:
            todo_ts.append((rel, mt))
    if todo_ts:
        # the repo's own typescript; before `npm install`, another repo's or aiset's own
        tsmod = next((d for d in [os.path.join(root, "node_modules", "typescript")]
                      + [os.path.join(CODE_ROOT, r, "node_modules", "typescript") for r in REPOS]
                      + [os.path.join(APP_DIR, "node_modules", "typescript")] if os.path.isdir(d)), None)
        if tsmod:
            p = subprocess.run(["node", os.path.join(HERE, "teammap_ts.js"), tsmod, root],
                               input="\n".join(r for r, _ in todo_ts), capture_output=True, text=True, encoding="utf-8", timeout=300)
            parsed = json.loads(p.stdout or "{}")
            for rel, mt in todo_ts:
                if rel in parsed:
                    new[rel] = {"mtime": mt, "lang": "ts", "data": parsed[rel]}
    cache["files"] = new
    if changed or len(new) != len(old):
        os.makedirs(INDEX, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    return {"name": name, "root": root, "files": new, "changed": changed}


# -- linking: endpoints, models, frontend calls -------------------------------------------------

def norm(path):
    path = re.sub(r"\{[^}]*\}", "{}", path.split("?")[0])
    path = re.sub(r"/+", "/", path).rstrip("/")
    return path or "/"


def py_module_map(repo):
    m = {}
    for rel, e in repo["files"].items():
        if e["lang"] == "py":
            mod = rel[:-3].replace("/", ".")
            m[mod[:-9] if mod.endswith(".__init__") else mod] = rel
    return m


def resolve_import(mods, rel, imp):
    """(file, name) an import alias points to, if it is inside the repo."""
    mod = imp["module"] or ""
    if imp["level"]:
        base = ".".join(rel[:-3].split("/")[:-imp["level"]])
        mod = f"{base}.{mod}" if mod else base
    for cand in (mod, mod.split(".", 1)[-1]):
        if imp["name"] and f"{cand}.{imp['name']}" in mods:
            return mods[f"{cand}.{imp['name']}"], None  # `from pkg import module`
        if cand in mods:
            return mods[cand], imp["name"]
    return None, None


def tsconfig_aliases(root):
    """tsconfig "paths" ("@/*": ["./src/*"]) as [(prefix, repo-relative dir)], longest first.

    Read with regexes rather than json, since tsconfig allows comments and trailing commas."""
    try:
        with open(os.path.join(root, "tsconfig.json"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    base = (re.search(r'"baseUrl"\s*:\s*"([^"]*)"', text) or [None, "."])[1]
    block = re.search(r'"paths"\s*:\s*\{(.*?)\}', text, re.S)
    out = []
    for alias, target in re.findall(r'"([^"*]*)\*"\s*:\s*\[\s*"([^"*]*)\*"', block.group(1) if block else ""):
        d = posixpath.normpath(posixpath.join(base, target))
        out.append((alias, "" if d == "." else d.rstrip("/") + "/"))
    return sorted(out, key=lambda a: -len(a[0]))


TS_SUFFIXES = ("", ".ts", ".tsx", "/index.ts", "/index.tsx")


def import_resolver(repo):
    """f(rel, imp) -> the repo file one import of file rel points to, or None (a package, or not resolvable)."""
    files = repo["files"]
    mods, aliases = py_module_map(repo), tsconfig_aliases(repo["root"])

    def target(rel, imp):
        if files[rel]["lang"] == "py":
            return resolve_import(mods, rel, imp)[0]
        spec = imp.get("from") or ""
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
        else:
            base = next((d + spec[len(a):] for a, d in aliases if spec.startswith(a)), None)
        return base and next((base + s for s in TS_SUFFIXES if base + s in files), None)
    return target


def file_imports(repo):
    """(importer, imported) file pairs inside one repo; imports of packages are left out."""
    out, target = set(), import_resolver(repo)
    for rel, e in repo["files"].items():
        for imp in e["data"].get("imports") or []:
            t = target(rel, imp)
            if t and t != rel:
                out.add((rel, t))
    return out


def is_test(rel):
    return bool(re.search(r"(^|/)(tests?|testing)/|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$", rel))


def link(repos):
    """Endpoints, models, frontend API functions, plus lookup tables for the commands."""
    endpoints, models, apifns, consts_ts = [], {}, [], {}
    for repo in repos:
        files = repo["files"]
        py_all = {r: e["data"] for r, e in files.items() if e["lang"] == "py" and "error" not in e["data"]}
        # tests build their own apps and include routers without prefixes; they aren't the served API
        py = {r: d for r, d in py_all.items() if not is_test(r)}
        if py:
            consts = {}
            for d in py.values():
                consts.update(d["consts"])
            val = lambda v: consts.get(v["ref"], "") if isinstance(v, dict) else (v or "")
            mods = py_module_map(repo)
            classes = {s["name"]: (r, s) for r, d in py_all.items() for s in d["symbols"] if s["kind"] == "class" and not is_test(r)}
            # app prefixes: root_path, plus mount points
            app_prefix = {}
            for r, d in py.items():
                for a, rp in d["apps"].items():
                    app_prefix[(r, a)] = val(rp)
            for r, d in py.items():
                for mnt in d["mounts"]:
                    if (r, mnt["app"]) in app_prefix:
                        app_prefix[(r, mnt["app"])] = app_prefix.get((r, mnt["target"]), "") + val(mnt["path"]) + app_prefix[(r, mnt["app"])]
            # router key -> prefix contributed by include_router (and the app it lands on)
            inc_prefix = {}
            for r, d in py.items():
                aliases = {i["as"]: i for i in d["imports"]}
                for inc in d["includes"]:
                    base = app_prefix.get((r, inc["target"]), "") + val(inc["prefix"])
                    arg = inc["arg"]
                    m = re.match(r"(\w+)\(\)\.get_router\(\)", arg)
                    if m and m.group(1) in classes:
                        inc_prefix[(classes[m.group(1)][0], "self@" + m.group(1))] = base
                        continue
                    name = arg.split(".")[0]
                    if name in aliases:
                        tf, tn = resolve_import(mods, r, aliases[name])
                        key = (tf, tn or (arg.split(".", 1)[1] if "." in arg else "router"))
                        if tf and len(base) >= len(inc_prefix.get(key, "")):
                            inc_prefix[key] = base
                    elif name in d["routers"]:
                        inc_prefix[(r, name)] = base
            for r, d in py.items():
                for rt in d["routes"]:
                    if rt["router"].startswith("self") and rt["cls"]:
                        key = (r, "self@" + rt["cls"])
                        own = val(d["routers"].get("self@" + rt["cls"]) or next((v for k, v in d["routers"].items() if k.startswith("self@")), ""))
                        # GenericRouter subclasses: the APIRouter lives in the base class
                    else:
                        key = (r, rt["router"])
                        own = val(d["routers"].get(rt["router"], ""))
                    pre = inc_prefix.get(key)
                    if pre is None:
                        pre = app_prefix.get((r, rt["router"]), "")
                        if not pre and app_prefix:
                            pre = next(iter(app_prefix.values())) if len(set(app_prefix.values())) == 1 else ""
                    full = (pre + own + rt["path"]) or "/"
                    handler = next((s for s in d["symbols"] if s["qual"] == rt["handler"] or s["qual"].endswith("." + (rt["handler"] or "-"))), None)
                    endpoints.append({"repo": repo["name"], "method": rt["method"], "path": full, "norm": norm(full),
                                      "file": r, "line": handler["line"] if handler else rt["line"],
                                      "handler": rt["handler"], "params": handler.get("params", []) if handler else [],
                                      "calls": handler.get("calls", []) if handler else [], "response": rt["response"]})
            # pydantic-ish models: BaseModel subclasses, transitively
            names = {n for n, (_, s) in classes.items() if any(b.split(".")[-1] in ("BaseModel", "SQLModel", "BaseSchema") for b in s["bases"])}
            grew = True
            while grew:
                grew = False
                for n, (_, s) in classes.items():
                    if n not in names and any(b.split(".")[-1] in names for b in s["bases"]):
                        names.add(n)
                        grew = True
            for n in names:
                r, s = classes[n]
                models.setdefault(n, []).append({"repo": repo["name"], "file": r, "line": s["line"], "fields": s["fields"], "bases": s["bases"]})
        ts = {r: e["data"] for r, e in files.items() if e["lang"] == "ts"}
        for d in ts.values():
            for k, v in d["consts"].items():
                if v:
                    consts_ts[k] = v
        for r, d in ts.items():
            for h in d["http"]:
                apifns.append({"repo": repo["name"], "file": r, "line": h["line"], "in": h["in"] or "(top level)",
                               "method": h["method"], "tokens": h["url"]})

    def render(tokens, depth=0):
        out = ""
        for t in tokens:
            if "lit" in t:
                out += t["lit"]
            elif "id" in t:
                parts = t["id"].split(".")
                v = (consts_ts.get(".".join(parts[-2:])) or consts_ts.get(parts[-1])) if depth < 6 else None
                out += render(v, depth + 1) if v else "{" + t["id"] + "}"
            else:
                out += "{}"
        return out

    by_method = defaultdict(list)
    for ep in endpoints:
        by_method[ep["method"]].append(ep)
    for a in apifns:
        a["path"] = render(a["tokens"])
        n = norm(a["path"])
        cands = by_method.get(a["method"], []) if a["method"] != "REQUEST" else endpoints
        best, best_len = None, 1
        for ep in cands:
            if n == ep["norm"]:
                best, best_len = ep, 99
                break
            # suffix match on whole segments, at least two of them
            segs = ep["norm"].split("/")
            for k in range(len(segs) - 1, best_len, -1):
                if n.endswith("/" + "/".join(segs[-k:])) and k > best_len:
                    best, best_len = ep, k
                    break
        a["endpoint"] = best
        if best:
            best.setdefault("frontend", []).append(a)
    return {"endpoints": endpoints, "models": models, "apifns": apifns}


# -- queries ----------------------------------------------------------------------------------

def episodes_touching(files):
    try:
        from .brain import read_episodes
    except Exception:
        return []
    out = []
    for ep in read_episodes():
        touched = [t.strip() for t in ep["TOUCHED"].split(",") if t.strip()]
        if any(f.endswith(t) or t.endswith(os.path.basename(f)) for f in files for t in touched):
            out.append(f"{ep['DATE']} {ep['TASK']}")
    return out[:3]


def all_symbols(repos):
    for repo in repos:
        for rel, e in repo["files"].items():
            for s in e["data"].get("symbols", []):
                yield repo["name"], rel, e["lang"], s


def call_sites(repos, name, obj=None):
    """Where `name` is called, matched by name: [{repo, file, line, caller}]. Python gives the calling function's
    first line; TypeScript the call's own line. For TypeScript, obj (e.g. the client class) filters out
    same-named methods of other objects."""
    out = []
    for repo in repos:
        for rel, e in repo["files"].items():
            d = e["data"]
            if e["lang"] == "py":
                for s in d.get("symbols", []):
                    if name in s.get("calls", []) and s["name"] != name:
                        out.append({"repo": repo["name"], "file": rel, "line": s["line"], "caller": s["qual"]})
            else:
                seen = set()
                for c in d.get("calls", []):
                    if obj and (c.get("obj") or "").split(".")[-1] not in (obj, "this"):
                        continue
                    if c["name"] == name and (c["in"], c["line"]) not in seen:
                        seen.add((c["in"], c["line"]))
                        out.append({"repo": repo["name"], "file": rel, "line": c["line"], "caller": c["in"] or "(top level)"})
    return out


def callers_of(repos, name, limit=12, obj=None):
    out = [f"{c['repo']}/{c['file']}:{c['line']} {c['caller']}" + ("()" if c["file"].endswith(".py") else "")
           for c in call_sites(repos, name, obj)]
    return out[:limit], len(out)


def injected(p):
    """FastAPI dependencies (auth, sessions, services, clients) rather than request data."""
    return (p["name"] in ("current_user", "user", "_", "session", "db", "request", "background_tasks")
            or re.search(r"(Service|Client|Session|Token\w*|User|Repository|Settings)\b", p["type"] or "") is not None)


def fmt_fields(fields, n=12):
    s = ", ".join(f"{f['name']}{'' if f.get('default') is False else '?'}: {f['type']}" if "default" in f
                  else f"{f['name']}{'?' if f.get('optional') else ''}: {f['type']}" for f in fields[:n])
    return s + (f", … (+{len(fields) - n})" if len(fields) > n else "")


def model_names(text, models):
    return [n for n in re.findall(r"[A-Za-z_]\w*", text or "") if n in models]


def cmd_endpoint(repos, L, args, limit):
    q = args[0] if args else ""
    method = args[1].upper() if len(args) > 1 else None
    hits = [e for e in L["endpoints"] if q.lower() in e["path"].lower() and (not method or e["method"] == method)]
    if not hits:
        print(f"no endpoint matches {q!r}" + (" — try a shorter part of the path" if "/" in q else ""))
        return
    hits.sort(key=lambda e: (len(e["path"]), e["path"]))
    for e in hits[:limit]:
        print(f"{e['method']} {e['path']}   [{e['repo']}]")
        print(f"  handler   {e['file']}:{e['line']}  {e['handler']}()")
        params = [p for p in e["params"] if not injected(p)]
        req = [(p["name"], p["type"]) for p in params if model_names(p["type"], L["models"])]
        for pname, ptype in req:
            m = L["models"][model_names(ptype, L["models"])[0]][0]
            print(f"  request   {pname}: {ptype}  ({m['file']}:{m['line']}) {{{fmt_fields(m['fields'], 8)}}}")
        plain = [f"{p['name']}: {p['type'] or '?'}" for p in params if not model_names(p["type"], L["models"])]
        if plain:
            print(f"  params    {', '.join(plain[:8])}")
        if e["response"]:
            rn = model_names(e["response"], L["models"])
            loc = f"  ({L['models'][rn[0]][0]['file']}:{L['models'][rn[0]][0]['line']})" if rn else ""
            print(f"  response  {e['response']}{loc}")
        if e["calls"]:
            print(f"  calls     {', '.join(c for c in e['calls'] if c not in ('Depends', 'HTTPException', 'get', 'str', 'int', 'len'))[:200]}")
        for a in e.get("frontend", [])[:4]:
            parts = a["in"].split(".")
            print(f"  frontend  {a['repo']}/{a['file']}:{a['line']}  {a['in']}()")
            used, n = callers_of([r for r in repos if r["name"] == a["repo"]], parts[-1], 4, parts[-2] if len(parts) > 1 else None)
            if used:
                print(f"            used by {'; '.join(u.split('/', 1)[1] for u in used)}{f' (+{n - len(used)})' if n > len(used) else ''}")
        if not e.get("frontend"):
            print("  frontend  (no matching call found in the frontend repos)")
        eps = episodes_touching([f"{e['repo']}/{e['file']}"])
        if eps:
            print(f"  episodes  {'; '.join(eps)}")
    if len(hits) > limit:
        print(f"… {len(hits) - limit} more; narrow the path or add a method")


def cmd_symbol(repos, L, args, limit):
    q = args[0]
    hits, match_kind = _matching_hits(repos, q)
    if not hits:
        print(f"no symbol matches {q!r}  (try: teammap search {q})")
        return 4
    if _matches_multiple_files(hits):
        _print_candidates(q, hits, limit, "symbol", match_kind)
        return 3
    if match_kind == "substring":
        print(f"substring match for {q!r} (not an exact name):")
    shown_callers = set()
    for r, f, lang, s in hits[:limit]:
        sig = ", ".join(f"{p['name']}: {p['type']}" if p["type"] else p["name"] for p in s.get("params", [])) if lang == "py" else s.get("params", "")
        print(f"{s['kind']} {s['qual']}({sig if s['kind'] != 'class' else ''})   {r}/{f}:{s['line']}-{s['end']}")
        if s["kind"] == "class" and s.get("fields"):
            print(f"  fields    {fmt_fields(s['fields'])}")
        if s.get("bases"):
            print(f"  bases     {', '.join(s['bases'])}")
        if s.get("calls"):
            print(f"  calls     {', '.join(s['calls'][:20])}")
        eps = [e for e in L["endpoints"] if e["repo"] == r and e["file"] == f and (e["handler"] or "").endswith(s["name"])]
        for e in eps:
            print(f"  serves    {e['method']} {e['path']}")
        if s["name"] in shown_callers:  # callers are matched by name: the same list as above
            print("  called by (same as above)")
            continue
        shown_callers.add(s["name"])
        cs, n = callers_of(repos, s["name"])
        print(f"  called by {n} place(s){' (matched by name)' if cs else ''}{':' if cs else ''}")
        for c in cs[:8]:
            print(f"            {c}")
    if len(hits) > limit:
        print(f"… {len(hits) - limit} more: " + ", ".join(sorted({s['qual'] for _, _, _, s in hits[limit:limit + 12]})))
    return 0


def _matching_hits(repos, q):
    symbols = list(all_symbols(repos))
    qualified = [(r, f, lang, s) for r, f, lang, s in symbols if s["qual"] == q]
    exact = [(r, f, lang, s) for r, f, lang, s in symbols if s["name"] == q]
    substring = [
        (r, f, lang, s) for r, f, lang, s in symbols if q.lower() in s["qual"].lower()
    ]
    if qualified:
        hits, match_kind = qualified, "qualified"
    elif exact:
        hits, match_kind = exact, "exact"
    else:
        hits, match_kind = substring, "substring"
    _sort_hits(hits)
    return hits, match_kind


def _sort_hits(hits):
    # the real definition first: not a test, not a nested helper, the biggest body
    hits.sort(
        key=lambda h: (
            is_test(h[1]),
            h[3]["qual"].count(".") - (1 if h[3]["kind"] == "method" else 0),
            -(h[3]["end"] - h[3]["line"]),
        )
    )
    return hits


def _best_hits(repos, q):
    symbols = list(all_symbols(repos))
    exact = [
        (r, f, lang, s) for r, f, lang, s in symbols if s["name"] == q or s["qual"] == q
    ]
    hits = exact or [
        (r, f, lang, s) for r, f, lang, s in symbols if q.lower() in s["qual"].lower()
    ]
    return _sort_hits(hits)


def _matches_multiple_files(hits):
    return len({(repo, path) for repo, path, _lang, _symbol in hits}) > 1


def _print_candidates(q, hits, limit, command, match_kind):
    label = "substring matches" if match_kind == "substring" else "ambiguous matches"
    print(f"{label} for {q!r}:")
    for repo, path, _lang, symbol in hits[:limit]:
        print(f"  {symbol['kind']} {symbol['qual']} {repo}/{path}:{symbol['line']}")
    if len(hits) > limit:
        print(
            f"… {len(hits) - limit} more; add --limit {len(hits)} to see all candidates"
        )
    print(
        f"Try `teammap {command} <qual>` for an exact match, or add `--repo <name>` to narrow."
    )


def cmd_code(repos, L, args, limit=None, max_lines=150):
    """Print a definition's source so an agent reads exactly that function, in one call."""
    q = args[0]
    hits, match_kind = _matching_hits(repos, q)
    if not hits:
        print(f"no symbol matches {q!r}  (try: teammap search {q})")
        return 4
    if _matches_multiple_files(hits):
        _print_candidates(q, hits, limit or 5, "code", match_kind)
        return 3
    if match_kind == "substring":
        print(f"substring match for {q!r} (not an exact name):")
    for r, f, lang, s in hits[: limit or 1]:
        path = os.path.join(CODE_ROOT, r, f)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            print(f"{s['kind']} {s['qual']}   {r}/{f}:{s['line']}-{s['end']}  (file not readable)")
            continue
        start, end = s["line"], min(s["end"], len(lines))
        # decorators sit above the def line
        while start > 1 and lines[start - 2].lstrip().startswith("@"):
            start -= 1
        print(f"{s['kind']} {s['qual']}   {r}/{f}:{start}-{end}")
        if s["kind"] == "class":
            methods = sorted((m for _, mf, _, m in all_symbols([x for x in repos if x["name"] == r])
                              if mf == f and m["qual"].startswith(s["qual"] + ".") and m["qual"].count(".") == s["qual"].count(".") + 1),
                             key=lambda m: m["line"])
            head_end = min([m["line"] - 1 for m in methods] + [end, start + 40])
            for n in range(start, head_end + 1):
                print(f"{n:>6}  {lines[n - 1]}")
            if methods:
                print(f"  methods ({len(methods)}):")
                for m in methods:
                    print(f"    {m['line']:>5}-{m['end']:<5} {m['name']}")
                print(f"  → teammap code {s['qual']}.<method> for one of them")
        else:
            stop = min(end, start + max_lines - 1)
            for n in range(start, stop + 1):
                print(f"{n:>6}  {lines[n - 1]}")
            if stop < end:
                print(f"  … {end - stop} more lines: Read {path} offset={stop + 1} limit={end - stop}  (or --max {end - start + 1})")
        print()
    if len(hits) > (limit or 1):
        print(
            "other matches: "
            + ", ".join(
                f"{h[3]['qual']} ({h[0]}/{h[1]}:{h[3]['line']})"
                for h in hits[(limit or 1) : (limit or 1) + 6]
            )
        )
    return 0


def camel(s):
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), s)


def model_uses(L, n):
    """[("request"|"response", endpoint)] for the endpoints whose annotations name model n."""
    out = []
    for e in L["endpoints"]:
        if n in model_names(e["response"], L["models"]):
            out.append(("response", e))
        elif any(n in model_names(p["type"], L["models"]) for p in e["params"]):
            out.append(("request", e))
    return out


def mirror_types(repos, n, m):
    """Frontend types that most likely mirror model n (definition m): same base name, then most shared fields.
    [(same name, shared fields, repo, file, type)], best first."""
    base = re.sub(r"(Schema|Response|Request|Model|Dto|DTO)$", "", n)
    fset = {camel(x["name"]) for x in m["fields"]} | {x["name"] for x in m["fields"]}
    scored = []
    for r in repos:
        for f, e in r["files"].items():
            if e["lang"] != "ts":
                continue
            for t in e["data"].get("types", []):
                tb = re.sub(r"^I(?=[A-Z])", "", t["name"])
                tb = re.sub(r"(Response|Request|Model|Dto|DTO|Raw)$", "", tb)
                shared = len(fset & {x["name"] for x in t["members"]})
                if tb.lower() == base.lower() or (shared >= 3 and shared >= len(t["members"]) * .5):
                    scored.append((tb.lower() == base.lower(), shared, r, f, t))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored


def cmd_model(repos, L, args, limit):
    q = args[0]
    names = [n for n in L["models"] if n == q] or [n for n in L["models"] if q.lower() in n.lower()]
    if not names:
        print(f"no model matches {q!r}")
        return
    for n in names[:limit]:
        for m in L["models"][n]:
            print(f"model {n}   {m['repo']}/{m['file']}:{m['line']}   bases: {', '.join(m['bases'])}")
            print(f"  fields    {fmt_fields(m['fields'], 30)}")
            for role, e in model_uses(L, n)[:6]:
                print(f"  {role:9} {e['method']} {e['path']}")
            scored = mirror_types(repos, n, m)
            for same, shared, r, f, t in scored[:2]:
                tnames = {x["name"] for x in t["members"]}
                missing = [x["name"] for x in m["fields"] if x["name"] not in tnames and camel(x["name"]) not in tnames]
                extra = [x for x in tnames if x not in {y["name"] for y in m["fields"]} and x not in {camel(y["name"]) for y in m["fields"]}]
                print(f"  frontend  {t['kind']} {t['name']}   {r['name']}/{f}:{t['line']}   ({shared} shared field(s))")
                if missing:
                    print(f"            not in frontend type: {', '.join(missing[:15])}")
                if extra:
                    print(f"            only in frontend type: {', '.join(sorted(extra)[:15])}")
            if not scored:
                print("  frontend  (no matching type found)")


def cmd_file(repos, L, args, limit):
    q = args[0]
    hits = [(r, f, e) for r in repos for f, e in r["files"].items() if q in f]
    if not hits:
        print(f"no file matches {q!r}")
        return
    hits.sort(key=lambda x: len(x[1]))
    for r, f, e in hits[:limit]:
        d = e["data"]
        print(f"{r['name']}/{f}")
        syms = [s for s in d.get("symbols", [])]
        if syms:
            print("  defines   " + ", ".join(f"{s['qual']}:{s['line']}" for s in syms[:30]) + (f", … (+{len(syms) - 30})" if len(syms) > 30 else ""))
        for t in d.get("types", [])[:12]:
            print(f"  type      {t['name']}:{t['line']} ({len(t['members'])} fields)")
        for ep in [x for x in L["endpoints"] if x["repo"] == r["name"] and x["file"] == f][:20]:
            print(f"  route     {ep['method']} {ep['path']} → {ep['handler']}()")
        for a in [x for x in L["apifns"] if x["repo"] == r["name"] and x["file"] == f][:20]:
            tgt = f"→ {a['endpoint']['repo']} {a['endpoint']['file']}:{a['endpoint']['line']}" if a["endpoint"] else "(no backend match)"
            print(f"  api call  {a['in']}: {a['method']} {a['path']} {tgt}")
        # who imports it
        importers = sorted(source for source, target in file_imports(r) if target == f)
        if importers:
            print(f"  imported by {len(importers)}: " + ", ".join(importers[:10]) + (" …" if len(importers) > 10 else ""))
        eps = episodes_touching([f"{r['name']}/{f}"])
        if eps:
            print(f"  episodes  {'; '.join(eps)}")


def cmd_search(repos, L, args, limit):
    q = args[0].lower()
    rows = [f"endpoint {e['method']} {e['path']}  ({e['repo']}/{e['file']}:{e['line']})" for e in L["endpoints"] if q in e["path"].lower() or q in (e["handler"] or "").lower()]
    rows += [f"model    {n}  ({m[0]['repo']}/{m[0]['file']}:{m[0]['line']})" for n, m in L["models"].items() if q in n.lower()]
    rows += [f"{s['kind']:8} {s['qual']}  ({r}/{f}:{s['line']})" for r, f, _, s in all_symbols(repos) if q in s["qual"].lower()]
    rows += [f"type     {t['name']}  ({r['name']}/{f}:{t['line']})" for r in repos for f, e in r["files"].items() if e["lang"] == "ts" for t in e["data"].get("types", []) if q in t["name"].lower()]
    seen = []
    for row in sorted(rows, key=lambda r: bool(re.search(r"/tests?/|/test_", r))):  # tests last
        if row not in seen:
            seen.append(row)
    print("\n".join(seen[: limit * 6]) or f"nothing matches {q!r}")
    if len(seen) > limit * 6:
        print(f"… {len(seen) - limit * 6} more")


# -- Brain export -----------------------------------------------------------------------------

def export(repos, L):
    os.makedirs(GRAPHS, exist_ok=True)
    for repo in repos:
        name = repo["name"]
        nodes, edges, seen = [], [], set()

        def add(nid, type_, label, **info):
            if nid not in seen:
                seen.add(nid)
                nodes.append({"id": nid, "type": type_, "label": label, **info})

        def file_node(rel):
            add(f"file:{name}/{rel}", "file", os.path.basename(rel), repo=name, rel=rel, path=os.path.join(repo["root"], rel))
            return f"file:{name}/{rel}"

        # every indexed file, so the Brain shows the whole repo and not only routers and API callers
        for rel, e in sorted(repo["files"].items()):
            add(f"file:{name}/{rel}", "file", os.path.basename(rel), repo=name, rel=rel, path=os.path.join(repo["root"], rel),
                lang=e["lang"], symbols=len(e["data"].get("symbols") or []))
        for a, b in sorted(file_imports(repo)):
            edges.append({"source": f"file:{name}/{a}", "target": f"file:{name}/{b}", "type": "imports"})

        groups = defaultdict(list)
        for e in (x for x in L["endpoints"] if x["repo"] == name):
            eid = f"endpoint:{name} {e['method']} {e['path']}"
            add(eid, "endpoint", f"{e['method']} {e['path']}", handler=e["handler"], file=e["file"], line=e["line"])
            edges.append({"source": eid, "target": file_node(e["file"]), "type": "handled-in"})
            for mn in set(model_names(e["response"], L["models"])):
                mm = L["models"][mn][0]
                add(f"model:{mm['repo']}:{mn}", "model", mn, file=mm["file"], line=mm["line"], fields=len(mm["fields"]))
                edges.append({"source": eid, "target": f"model:{mm['repo']}:{mn}", "type": "response"})
            for p in e["params"]:
                for mn in set(model_names(p["type"], L["models"])):
                    mm = L["models"][mn][0]
                    add(f"model:{mm['repo']}:{mn}", "model", mn, file=mm["file"], line=mm["line"], fields=len(mm["fields"]))
                    edges.append({"source": eid, "target": f"model:{mm['repo']}:{mn}", "type": "request"})
            seg = [s for s in e["path"].split("/") if s and "{" not in s]
            groups[seg[min(len(seg) - 1, 3)] if seg else "/"].append({"id": eid, "label": f"{e['method']} {e['path']}", "type": "endpoint", "children": []})
        for a in (x for x in L["apifns"] if x["repo"] == name):
            aid = f"apifn:{name}:{a['file']}#{a['in']}"
            add(aid, "apifn", a["in"].split(".")[-1] + "()", qual=a["in"], file=a["file"], line=a["line"], url=f"{a['method']} {a['path']}")
            edges.append({"source": aid, "target": file_node(a["file"]), "type": "in"})
            if a["endpoint"]:
                ep = a["endpoint"]
                edges.append({"source": aid, "target": f"endpoint:{ep['repo']} {ep['method']} {ep['path']}", "type": "calls-api"})
        tree = []
        if groups:
            tree.append({"id": f"group:{name}:endpoints", "label": f"Endpoints ({sum(map(len, groups.values()))})", "type": "group",
                         "children": [{"id": f"group:{name}:ep:{g}", "label": g, "type": "dir", "children": sorted(v, key=lambda x: x["label"])} for g, v in sorted(groups.items())]})
        api = [n for n in nodes if n["type"] == "apifn"]
        if api:
            by_file = defaultdict(list)
            for n in api:
                by_file[n["file"]].append({"id": n["id"], "label": n["label"], "type": "apifn", "children": []})
            tree.append({"id": f"group:{name}:api", "label": f"API calls ({len(api)})", "type": "group",
                         "children": [{"id": f"group:{name}:api:{f}", "label": os.path.basename(f), "type": "dir", "children": v} for f, v in sorted(by_file.items())]})
        with open(os.path.join(GRAPHS, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"repo": name, "nodes": nodes, "edges": edges, "tree": tree}, fh)


# -- main -------------------------------------------------------------------------------------

def _msys_root():
    """Git for Windows' install folder (the "/" of Git Bash): the first folder above git.exe with usr/ and etc/."""
    import shutil
    git = shutil.which("git")
    d = os.path.dirname(os.path.realpath(git)) if git else ""
    while d and os.path.dirname(d) != d:
        if os.path.isdir(os.path.join(d, "usr")) and os.path.isdir(os.path.join(d, "etc")):
            return d.replace("\\", "/").rstrip("/")
        d = os.path.dirname(d)
    return None


def msys_unmangle(arg):
    """Git Bash on Windows rewrites an argument like /products into C:/Program Files/Git/products; undo that."""
    if os.name != "nt" or not os.environ.get("MSYSTEM") or not re.match(r"^[A-Za-z]:[\\/]", arg):
        return arg
    root = _msys_root()
    a = arg.replace("\\", "/")
    if root and a.lower().startswith(root.lower() + "/") and not os.path.exists(arg):
        return a[len(root):]
    return arg


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    if argv == ["--version"]:
        from . import __version__
        print("teammap " + __version__)
        return 0
    argv = [msys_unmangle(a) for a in argv]
    limit, only, force, args, max_lines = None, None, False, [], 150
    it = iter(argv)
    for a in it:
        if a == "--limit":
            limit = int(next(it, "5"))
        elif a == "--max":
            max_lines = int(next(it, "150"))
        elif a == "--repo":
            only = next(it, None)
        elif a == "--force":
            force = True
        else:
            args.append(a)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if args[0] == "arch":  # static scan of the whole workspace, no index needed
        from . import archmap
        return archmap.main(argv[argv.index("arch") + 1:])
    if args[0] in ("impact", "failures", "relevant", "research"):  # reusable research with stored, validated results
        from . import research
        return research.main(argv[argv.index(args[0]):])
    cmd, rest = args[0], args[1:]
    names = [only] if only else [r for r in REPOS if os.path.isdir(os.path.join(CODE_ROOT, r))]
    repos = [load_repo(n, force) for n in names]
    # export always covers every repo, so frontend/backend links stay whole
    all_repos = repos if not only else [load_repo(n) for n in REPOS if os.path.isdir(os.path.join(CODE_ROOT, n))]
    L = link(all_repos)
    if any(r["changed"] for r in all_repos) or cmd == "index":
        export(all_repos, L)
    cmds = {"endpoint": cmd_endpoint, "who-uses": cmd_endpoint, "symbol": cmd_symbol, "model": cmd_model,
            "file": cmd_file, "search": cmd_search, "code": cmd_code}
    if cmd == "index":
        for r in all_repos:
            n = len(r["files"])
            print(f"{r['name']:26} {n:5} files  {r['changed']:5} re-parsed  "
                  f"{sum(1 for e in L['endpoints'] if e['repo'] == r['name']):4} endpoints  "
                  f"{sum(1 for a in L['apifns'] if a['repo'] == r['name']):4} API calls")
        matched = sum(1 for a in L["apifns"] if a["endpoint"])
        print(f"frontend→backend matches: {matched}/{len(L['apifns'])} API calls · models: {len(L['models'])} · graph: {GRAPHS}")
        return 0
    if cmd not in cmds or not rest:
        print(f"usage: teammap {{{'|'.join(cmds)}}} <query>   (teammap --help)")
        return 2
    if only:  # answer from one repo, but keep cross-repo links
        L = link(all_repos)
        L["endpoints"] = [e for e in L["endpoints"] if e["repo"] == only] or L["endpoints"]
    if cmd == "code":
        return cmd_code(repos, L, rest, limit, max_lines)
    if cmd == "symbol":
        return cmd_symbol(repos, L, rest, limit or 5)
    cmds[cmd](all_repos if cmd in ("endpoint", "who-uses", "model") else repos, L, rest, limit or 5)
    return 0


def cli():
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
