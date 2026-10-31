"""The team's Brain: what the agents know, what they touched, and what they did, as one graph.

Sources (all read-only):
  - roster.json: agents, the briefs and repositories they own
  - knowledge briefs (<team>/knowledge/*.md, see workspace.py): one node per brief and per section
  - transcripts (via an Office): which files each agent read or edited
  - episodes (<team>/episodes/*.md): task summaries the PM writes when a task closes
  - code graphs (<team>/graph/*.json): optional, {"nodes": [...], "edges": [...]}
    produced by a code indexer; merged as-is so the Brain grows when one exists
"""
import json
import os
import re
import time
from collections import Counter, defaultdict
from glob import glob

from .workspace import CODE_ROOT, EPISODES, GRAPHS, KNOWLEDGE, norm, relative, root_pattern

EPISODE_FIELDS = ("TASK", "DATE", "AGENTS", "OUTCOME", "TOUCHED", "CONTRACT", "DECISIONS", "GOTCHAS", "COST")


def _json_files(directory):
    """Sorted JSON files, including hidden names produced by the root unit."""
    try:
        with os.scandir(directory) as entries:
            return sorted(
                entry.path
                for entry in entries
                if entry.name.endswith(".json") and entry.is_file()
            )
    except OSError:
        return []


def brief_sections(filename):
    """[(level, title, first line, text)] for the ## and ### headings of a brief."""
    try:
        with open(os.path.join(KNOWLEDGE, filename), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for i, line in enumerate(lines):
        m = re.match(r"^(##|###) (.+)", line)
        if m:
            out.append([len(m.group(1)), m.group(2).strip(), i + 1, []])
        elif out:
            out[-1][3].append(line)
    return [(lvl, title, ln, "\n".join(body).strip()) for lvl, title, ln, body in out]


def read_episodes():
    eps = []
    for path in sorted(glob(os.path.join(EPISODES, "*.md")), reverse=True):
        if os.path.basename(path) == "index.md":
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        fields = {}
        for k in EPISODE_FIELDS:
            m = re.search(rf"^{k}:\s*(.+)$", text, re.M)
            fields[k] = m.group(1).strip() if m else ""
        eps.append({"file": path, "name": os.path.basename(path)[:-3], "text": text, **fields})
    return eps


def split_path(path):
    """Absolute path -> ("code", repo, relpath) | ("brief", filename, None) | None."""
    # the knowledge folder sits inside a workspace, so check it before the repos
    if relative(path, KNOWLEDGE) is not None and path.endswith(".md"):
        return ("brief", os.path.basename(norm(path)).replace(".index.md", ".md"), None)
    rel = relative(path, CODE_ROOT)
    if rel is not None:
        repo, _, rest = rel.partition("/")
        if not rest or repo.startswith(".") or re.search(r"(^|/)(\.git|\.venv|venv|node_modules|__pycache__|dist|build|\.next)/", rest):
            return None
        return ("code", repo, rest)
    return None


def build(office, roster):
    nodes, edges = {}, []

    def node(nid, type_, label, **info):
        n = nodes.get(nid)
        if n is None:
            n = nodes[nid] = {"id": nid, "type": type_, "label": label, "weight": 0, "info": {}}
        n["info"].update(info)
        return n

    def edge(a, b, type_, weight=1):
        edges.append({"source": a, "target": b, "type": type_, "weight": weight})

    members = {m["id"]: m for m in roster["members"]}
    brief_owner = {}
    section_text = {}
    tree_team = []

    # -- agents, briefs, sections ---------------------------------------------------
    for m in roster["members"]:
        aid = "agent:" + m["id"]
        node(aid, "agent", m["name"], role=m.get("role", ""), duty=m.get("duty", ""), color=m.get("color"), agentId=m["id"])
        agent_tree = {"id": aid, "label": m["name"], "type": "agent", "children": []}
        for repo in m.get("repos") or []:
            node("repo:" + repo, "repo", repo, path=os.path.join(CODE_ROOT, repo))
            edge(aid, "repo:" + repo, "owns")
        brief = m.get("knowledge")
        if brief:
            bid = "brief:" + brief
            brief_owner[brief] = m["id"]
            path = os.path.join(KNOWLEDGE, brief)
            node(bid, "brief", brief, path=path, exists=os.path.exists(path),
                 tokens=os.path.getsize(path) // 4 if os.path.exists(path) else 0)
            edge(aid, bid, "owns")
            btree = {"id": bid, "label": brief, "type": "brief", "children": []}
            parent_sec, parent_tree = bid, btree
            for lvl, title, ln, text in brief_sections(brief):
                sid = f"sec:{brief}#{ln}"
                node(sid, "section", title, path=path, line=ln, text=text[:1800], tokens=len(text) // 4, level=lvl)
                section_text[sid] = text
                item = {"id": sid, "label": title, "type": "section", "children": []}
                if lvl == 2 or parent_sec == bid:
                    edge(bid, sid, "section")
                    btree["children"].append(item)
                    parent_sec, parent_tree = sid, item
                else:
                    edge(parent_sec, sid, "section")
                    parent_tree["children"].append(item)
            agent_tree["children"].append(btree)
        tree_team.append(agent_tree)

    # -- files the agents touched ---------------------------------------------------
    touches = defaultdict(Counter)  # file node id -> Counter((agent, op))
    for t in office.scan():
        if t.kind == "sub":
            agent = t.agent_type
        elif t.agent_setting == "pm":
            agent = "pm"
        else:
            continue  # your own sessions aren't team memory
        if agent not in members:
            continue
        for (op, path), n in t.files.items():
            sp = split_path(path)
            if not sp:
                continue
            if sp[0] == "brief":
                if sp[1] in brief_owner:
                    touches["brief:" + sp[1]][(agent, op)] += n
                continue
            _, repo, rel = sp
            fid = f"file:{repo}/{rel}"
            node(fid, "file", os.path.basename(rel), repo=repo, rel=rel, path=path)
            node("repo:" + repo, "repo", repo, path=os.path.join(CODE_ROOT, repo))
            touches[fid][(agent, op)] += n
    for nid, c in touches.items():
        by_agent = defaultdict(Counter)
        for (agent, op), n in c.items():
            by_agent[agent][op] += n
        nodes[nid]["weight"] += sum(c.values())
        nodes[nid]["info"]["reads"] = sum(n for (a, op), n in c.items() if op == "read")
        nodes[nid]["info"]["edits"] = sum(n for (a, op), n in c.items() if op == "edit")
        nodes[nid]["info"]["byAgent"] = {a: dict(v) for a, v in by_agent.items()}
        for agent, ops in by_agent.items():
            edge("agent:" + agent, nid, "edited" if ops.get("edit") else "read", sum(ops.values()))

    # -- episodes ---------------------------------------------------------------------
    tree_eps = []
    for ep in read_episodes():
        eid = "ep:" + ep["name"]
        node(eid, "episode", ep["TASK"] or ep["name"], **{k.lower(): ep[k] for k in EPISODE_FIELDS},
             path=ep["file"], text=ep["text"][:4000])
        tree_eps.append({"id": eid, "label": f"{ep['DATE'] or ''} {ep['TASK'] or ep['name']}".strip(), "type": "episode", "children": []})
        for a in re.findall(r"([a-z][\w-]*)\s*(?:\(|,|$)", ep["AGENTS"]):
            if a in members:
                edge(eid, "agent:" + a, "by")
        for p in filter(None, (x.strip() for x in ep["TOUCHED"].split(","))):
            target = match_file(nodes, p)
            if target:
                edge(eid, target, "touched")

    # -- files belong to repos; sections that name a file point at it ----------------
    for n in list(nodes.values()):
        if n["type"] == "file":
            edge(n["id"], "repo:" + n["info"]["repo"], "in")
    for sid, text in section_text.items():
        for f in nodes.values():
            if f["type"] == "file" and len(f["label"]) >= 10 and f["label"] in text:
                edge(sid, f["id"], "mentions")

    # -- external code graphs ---------------------------------------------------------
    tree_code = []
    for path in _json_files(GRAPHS):
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, ValueError):
            continue
        # only files in the graph format ({"nodes": [{"id"…}], "edges": [{"source", "target"}]}) are merged
        if not isinstance(g, dict) or not all(isinstance(n, dict) and "id" in n for n in g.get("nodes", [])) \
                or not all(isinstance(e, dict) and "source" in e and "target" in e for e in g.get("edges", [])):
            continue
        name = os.path.basename(path)[:-5]
        gt = {"id": "graph:" + name, "label": name, "type": "graph", "children": g.get("tree") or []}
        for gn in g.get("nodes", []):
            nid = gn["id"] if ":" in gn["id"] else "code:" + gn["id"]
            node(nid, gn.get("type", "symbol"), gn.get("label", gn["id"]), **{k: v for k, v in gn.items() if k not in ("id", "type", "label")})
            if not g.get("tree"):
                gt["children"].append({"id": nid, "label": gn.get("label", gn["id"]), "type": gn.get("type", "symbol"), "children": []})
        for ge in g.get("edges", []):
            fix = lambda x: x if ":" in x else "code:" + x
            edge(fix(ge["source"]), fix(ge["target"]), ge.get("type", "link"), ge.get("weight", 1))
        if gt["children"]:
            tree_code.append(gt)

    edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]
    degree = Counter()
    for e in edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    for n in nodes.values():
        n["degree"] = degree[n["id"]]

    tree = [{"id": "group:team", "label": "Team & knowledge", "type": "group", "children": tree_team},
            {"id": "group:repos", "label": "Repositories", "type": "group", "children": repo_tree(nodes)},
            {"id": "group:episodes", "label": "Episodes", "type": "group", "children": tree_eps}]
    if tree_code:
        tree.append({"id": "group:code", "label": "Code graph", "type": "group", "children": tree_code})
    return {"nodes": list(nodes.values()), "edges": edges, "tree": tree,
            "sources": {"knowledge": KNOWLEDGE, "episodes": EPISODES, "graphs": GRAPHS,
                        "windowDays": round(office.window / 86400)}}


def match_file(nodes, p):
    """An episode's TOUCHED entry (full path, repo-relative path or basename) -> node id."""
    sp = split_path(p) if os.path.isabs(p) else None
    if sp and sp[0] == "code":
        return f"file:{sp[1]}/{sp[2]}"
    hits = [n["id"] for n in nodes.values() if n["type"] == "file"
            and (n["info"]["rel"].endswith(p) or f"{n['info']['repo']}/{n['info']['rel']}".endswith(p))]
    return hits[0] if len(hits) == 1 else None


def repo_tree(nodes):
    """Repositories -> folders -> files, folders with a single child folded into one row."""
    roots = {}
    for n in sorted((n for n in nodes.values() if n["type"] == "file"), key=lambda n: n["info"]["rel"]):
        repo = n["info"]["repo"]
        cur = roots.setdefault(repo, {"id": "repo:" + repo, "label": repo, "type": "repo", "children": [], "_dirs": {}})
        for part in n["info"]["rel"].split("/")[:-1]:
            d = cur["_dirs"].get(part)
            if d is None:
                d = cur["_dirs"][part] = {"id": f"{cur['id']}/{part}", "label": part, "type": "dir", "children": [], "_dirs": {}}
                cur["children"].append(d)
            cur = d
        cur["children"].append({"id": n["id"], "label": n["label"], "type": "file", "children": []})

    def fold(d):
        d.pop("_dirs", None)
        for c in d["children"]:
            fold(c)
        while d["type"] == "dir" and len(d["children"]) == 1 and d["children"][0]["type"] == "dir":
            c = d["children"][0]
            d["label"] += "/" + c["label"]
            d["id"], d["children"] = c["id"], c["children"]
        return d

    for r in nodes.values():  # owned repos show up even before anyone touches them
        if r["type"] == "repo" and r["label"] not in roots:
            roots[r["label"]] = {"id": r["id"], "label": r["label"], "type": "repo", "children": [], "_dirs": {}}
    return [fold(r) for _, r in sorted(roots.items())]


# -- live view: where each running agent is in the graph -----------------------------------------

CONTRACTOR_COLORS = {"Explore": "#94a3b8", "general-purpose": "#a3a3a3", "Plan": "#c084fc", "claude-code-guide": "#fbbf24"}
_cache = {}


def _cached(key, stamp, make):
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    val = make()
    _cache[key] = (stamp, val)
    return val


def _graph_nodes():
    """Endpoint/model/file nodes from the code graphs, for resolving teammap queries."""
    files = _json_files(GRAPHS)
    stamp = tuple(os.path.getmtime(f) for f in files)

    def make():
        out = {"endpoint": [], "model": [], "file": []}
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    for n in json.load(fh).get("nodes", []):
                        if n.get("type") in out:
                            out[n["type"]].append(n)
            except (OSError, ValueError):
                pass  # a graph file being rewritten or invalid: skip it this round
        return out
    return _cached("graph", stamp, make)


def _symbol_files():
    """symbol name -> [(repo, rel)] from teammap's index, refreshed when the index changes."""
    files = _json_files(os.path.join(GRAPHS, "index"))
    stamp = tuple(os.path.getmtime(f) for f in files)

    def make():
        out = defaultdict(list)
        for f in files:
            repo = os.path.basename(f)[:-5]
            try:
                with open(f, encoding="utf-8") as fh:
                    idx = json.load(fh)
            except (OSError, ValueError):
                continue
            for rel, e in idx.get("files", {}).items():
                for s in e.get("data", {}).get("symbols", []):
                    out[s["name"]].append((repo, rel))
        return out
    return _cached("symbols", stamp, make)


def _section_at(brief, line):
    path = os.path.join(KNOWLEDGE, brief)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    secs = _cached("sec:" + brief, stamp, lambda: brief_sections(brief))
    best = None
    for lvl, title, ln, _ in secs:
        if ln <= line:
            best = f"sec:{brief}#{ln}"
    return best


def _file_target(path, offset=None):
    """(node id, descriptor for a node the client may not have yet) for an absolute path."""
    sp = split_path(path)
    if not sp:
        return None, None
    if sp[0] == "brief":
        if offset and not path.endswith(".index.md"):
            sec = _section_at(sp[1], int(offset))
            if sec:
                return sec, None
        return "brief:" + sp[1], None
    _, repo, rel = sp
    nid = f"file:{repo}/{rel}"
    return nid, {"id": nid, "type": "file", "label": os.path.basename(rel),
                 "info": {"repo": repo, "rel": rel, "path": path}}


PATH_RE = re.compile(r"((?:/home/|/Users/|/[a-zA-Z]/|[a-zA-Z]:[\\/])[^\s'\"|;&<>()`]+)")


def resolve_step(step):
    """Graph node ids a tool step touched, best first, plus descriptors for new file nodes."""
    ids, extra = [], []

    def add(nid, desc=None):
        if nid and nid not in ids:
            ids.append(nid)
            if desc:
                extra.append(desc)

    if step["tool"] in ("Read", "Edit", "Write", "NotebookEdit") and step["path"]:
        add(*_file_target(step["path"], step.get("offset")))
    elif step["tool"] in ("Grep", "Glob") and step["path"]:
        sp = split_path(step["path"].rstrip("/") + "/x")
        if os.path.isfile(step["path"]):
            add(*_file_target(step["path"]))
        elif relative(step["path"], CODE_ROOT):
            add("repo:" + relative(step["path"], CODE_ROOT).split("/")[0])
        del sp
    elif step["tool"] == "Bash":
        cmd = step["cmd"]
        g = None
        m = re.search(r"\bteammap(?:\.py)?['\"]?\s+(endpoint|who-uses|model|file|symbol|search)\s+(?:--\S+\s+\S+\s+)*['\"]?([^\s'\"|;&]+)", cmd)
        if m:
            g = _graph_nodes()
            what, q = m.group(1), m.group(2)
            ql = q.lower()
            if what in ("endpoint", "who-uses"):
                for n in [n for n in g["endpoint"] if ql in n["label"].lower()][:3]:
                    add(n["id"])
            elif what == "model":
                hits = [n for n in g["model"] if n["label"] == q] or [n for n in g["model"] if ql in n["label"].lower()]
                for n in hits[:2]:
                    add(n["id"])
            elif what == "file":
                for n in [n for n in g["file"] if q in n.get("rel", "")][:2]:
                    add(*_file_target(n["path"]))
            elif what == "symbol":
                for repo, rel in _symbol_files().get(q.split(".")[-1], [])[:2]:
                    add(*_file_target(os.path.join(CODE_ROOT, repo, rel)))
            elif what == "search":
                for n in [n for n in g["endpoint"] + g["model"] if ql in n["label"].lower()][:3]:
                    add(n["id"])
        base = re.search(r"(?:-C|cd)\s+['\"]?(" + root_pattern(CODE_ROOT) + r"[\\/][\w.-]+)", cmd)
        for p in PATH_RE.findall(cmd):
            p = norm(p.rstrip(":,."))
            if os.path.isfile(p):
                add(*_file_target(p))
        if base:  # relative paths after `cd <repo>` or `git -C <repo>`
            for rel in re.findall(r"(?<![/\w.-])((?:[\w.-]+/)+[\w.-]+\.(?:py|tsx?|md|json|ya?ml))", cmd):
                full = os.path.join(base.group(1), rel)
                if os.path.isfile(full):
                    add(*_file_target(full))
            if not ids:
                add("repo:" + os.path.basename(base.group(1)))
    return ids, extra


def live(office, roster, since=1800):
    """Running (or recently active) agents with their trail through the graph and what they touched."""
    from .core import WORKING, THINKING, WAITING
    now = time.time()
    members = {m["id"]: m for m in roster["members"]}
    out, extra_all = [], {}
    for t in office.scan():
        if t.kind == "sub":
            agent = t.agent_type
        elif t.agent_setting == "pm":
            agent = "pm"
        else:
            continue
        st, cur = t.state(now)
        if st not in (WORKING, THINKING, WAITING) and now - t.mtime > since:
            continue
        steps = []
        for s in list(t.trail):
            if now - s["t"] > max(since, 1800) and st not in (WORKING, THINKING, WAITING):
                continue
            ids, extra = resolve_step(s)
            for e in extra:
                extra_all[e["id"]] = e
            if ids:
                steps.append({"t": s["t"], "tool": s["tool"], "kind": s["kind"], "text": s["text"], "ids": ids})
        touched = []
        for (op, path), n in t.files.items():
            nid, desc = _file_target(path)
            if nid:
                touched.append(nid)
                if desc:
                    extra_all[nid] = desc
        m = members.get(agent)
        out.append({
            "key": t.path, "agent": agent, "name": m["name"] if m else agent,
            "color": (m or {}).get("color") or CONTRACTOR_COLORS.get(agent, "#94a3b8"),
            "state": st, "current": cur, "title": t.title, "mtime": t.mtime,
            "steps": steps[-40:], "touched": sorted(set(touched)),
            "parent": (t.parent.agent_setting or "lead") if t.parent else None,
        })
    out.sort(key=lambda a: (a["state"] not in (WORKING, THINKING, WAITING), -a["mtime"]))
    return {"now": now, "agents": out[:12], "extra": list(extra_all.values())}


def task_trails(office, roster, tid):
    """Every agent run on one task as a trail through the graph, in the shape live() returns."""
    from .core import task_runs_steps, task_meta, current_meta
    members = {m["id"]: m for m in roster["members"]}
    agents, extra_all = [], {}
    for i, (r, steps) in enumerate(task_runs_steps(office, tid)):
        trail = []
        for st in steps:
            if st.get("failed"):
                continue
            # a shell step that read several files becomes one step per file
            first = lambda ln: int(re.match(r"\d+", ln).group()) if ln and re.match(r"\d+", ln) else None
            pseudo = ([{"tool": {"edit": "Edit", "search": "Grep"}.get(t["kind"], "Read"), "path": t["abs"], "offset": first(t.get("lines"))}
                       for t in st.get("targets", []) if t.get("abs")]
                      or [{"tool": st["tool"], "path": st.get("abs") or "", "cmd": st.get("cmd", ""), "offset": first(st.get("lines"))}])
            for p in pseudo:
                ids, extra = resolve_step({"cmd": "", **p})
                for e in extra:
                    extra_all[e["id"]] = e
                if ids:
                    verb = {"Read": "Reading", "Edit": "Editing", "Write": "Writing", "Grep": "Searching"}.get(p["tool"])
                    text = f"{verb} {os.path.basename(p['path'].rstrip('/'))}" if verb and p.get("path") else st["text"]
                    trail.append({"t": st["t"], "tool": p["tool"], "kind": st["kind"], "text": text, "ids": ids})
        if not trail:
            continue
        m = members.get(r["agent"])
        agents.append({
            "key": r["key"] + "#" + str(i), "agent": r["agent"],
            "name": (m["name"] if m else r["agent"]) + (" (lead)" if r["lead"] else ""),
            "color": (m or {}).get("color") or CONTRACTOR_COLORS.get(r["agent"], "#94a3b8"),
            "state": "done", "current": r["description"] or "", "title": r["description"] or "", "mtime": r["t"],
            "steps": trail, "touched": sorted({i for s in trail for i in s["ids"][:1]}), "parent": None,
        })
    meta = current_meta(office, tid, time.time()) if tid.startswith("current:") else task_meta(tid)
    return {"now": time.time(), "task": {"id": tid, "title": meta["title"], "status": meta["status"]},
            "agents": agents, "extra": list(extra_all.values())}

