"""teammap impact / failures / relevant: parsers on real test output, and the commands end to end on a small workspace."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

from aiset import research  # noqa: E402
from aiset.workspace import APP_DIR  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "research")
TEAMMAP = os.path.join(SRC, "aiset", "teammap.py")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def parse(name, fmt=None):
    res, fails = research.failures(fixture(name), fmt)
    return res, fails or []


class ParserTest(unittest.TestCase):
    def test_formats_are_detected(self):
        for name, fmt in [("pytest.log", "pytest"), ("pytest_short.log", "pytest"), ("pytest_tbno.log", "pytest"),
                          ("pytest_param_space.log", "pytest"),
                          ("pytest_native.log", "pytest"), ("unittest.log", "unittest"), ("tap.log", "tap"),
                          ("junit.xml", "junit"), ("jest.log", "jest"), ("vitest.log", "vitest"), ("pass.log", "passed-only")]:
            self.assertEqual(research.detect_format(fixture(name)), fmt, name)

    def test_pytest_parametrized_id_with_space(self):
        _, fails = parse("pytest_param_space.log")
        by_test = {f["test"]: f for f in fails}
        self.assertIn("test_split[a b]", by_test)
        f1 = by_test["test_split[a b]"]
        self.assertEqual(f1["file"], "test_p.py")
        self.assertEqual(f1["type"], "AssertionError")
        self.assertIn("assert ' ' not in 'a b'", f1["message"])

        self.assertIn("test_z[a - b]", by_test)
        f2 = by_test["test_z[a - b]"]
        self.assertEqual(f2["file"], "test_p.py")
        self.assertEqual(f2["type"], "AssertionError")
        self.assertIn("assert ' ' not in 'a - b'", f2["message"])

    def test_python_runners_give_the_same_failures(self):
        want = {"test_total_one": "6 != 7", "test_total_two": "5 != 6", "test_missing_price": "'price'",
                "test_parse_bad": "'four'", "test_parse_bad2": "'five'"}
        for name in ("pytest.log", "pytest_short.log", "pytest_native.log", "unittest.log", "junit.xml"):
            _, fails = parse(name)
            got = {f["test"].split(".")[-1]: f for f in fails}
            self.assertEqual(set(got), set(want), name)
            for test, msg in want.items():
                self.assertIn(msg, got[test]["message"], f"{name} {test}")
            self.assertEqual(got["test_parse_bad"]["type"], "ValueError", name)
            self.assertFalse(got["test_parse_bad"]["message"].startswith("ValueError"), name)  # no "ValueError: ValueError:"

    def test_innermost_frame_is_last(self):
        _, fails = parse("pytest.log")
        f = next(f for f in fails if f["test"].endswith("test_parse_bad"))
        self.assertEqual([(fr["path"], fr["line"]) for fr in f["frames"]][-2:],
                         [("app/tests/test_billing.py", 19), ("app/billing.py", 6)])
        _, js = parse("jest.log")
        disc = next(f for f in js if f["test"] == "cart › applies discount")
        self.assertEqual([(fr["path"], fr["line"]) for fr in disc["frames"]], [("src/cart.test.ts", 12), ("src/cart.ts", 11)])
        _, vi = parse("vitest.log")
        disc = next(f for f in vi if f["test"] == "cart > applies discount")
        self.assertEqual(disc["frames"][-1]["path"], "src/cart.ts")

    def test_js_runners(self):
        res, fails = parse("jest.log")
        self.assertEqual(sorted(f["test"] for f in fails), ["cart › applies discount", "cart › totals two items"])  # not "Console"
        self.assertEqual(next(f for f in fails if "discount" in f["test"])["type"], "TypeError")
        _, fails = parse("vitest.log")
        self.assertEqual(len(fails), 2)
        self.assertEqual(next(f for f in fails if "totals" in f["test"])["type"], "AssertionError")
        _, fails = parse("tap.log")
        self.assertEqual(sorted(f["test"] for f in fails), ["adds big", "adds small"])
        self.assertIn("2 !== 3", next(f for f in fails if f["test"] == "adds small")["message"])

    def test_repeated_errors_are_grouped_and_no_root_cause_is_invented(self):
        res, _ = parse("unittest.log")
        groups = res["sections"]["groups"]
        self.assertEqual(groups[0]["count"], 2)
        self.assertTrue(groups[0]["file"].endswith("app/billing.py"))  # not in an indexed repo: shown as printed
        self.assertEqual(groups[0]["line"], 6)
        self.assertEqual(sorted(t.split(".")[-1] for t in groups[0]["tests"]), ["test_parse_bad", "test_parse_bad2"])
        self.assertTrue(all(g["root_cause"] is None for g in groups))
        self.assertTrue(any("root cause: not determined" in s for s in res["summary"]))
        self.assertTrue(all(g["log_lines"] for g in groups))

    def test_runner_shortened_messages_are_flagged(self):
        res, _ = parse("pytest_tbno.log")
        self.assertTrue(any("shortened by the test runner" in g for g in res["gaps"]))

    def test_unknown_output_is_not_guessed(self):
        res, fails = research.failures("compiling...\nERROR something odd happened\ndone\n")
        self.assertEqual(res["status"], "unrecognized")
        self.assertIsNone(fails)
        self.assertEqual(res["sections"], {})
        res, fails = research.failures(fixture("pass.log"))
        self.assertEqual((res["status"], fails), ("ok", []))

    def test_output_limit_keeps_the_rest_reachable(self):
        res, _ = parse("pytest.log")
        res.update(id="abc123", record="/x/abc123.json")
        out = research.limited(res, 1)
        self.assertEqual(out["truncated"]["failed tests"], {"shown": 1, "total": 5})
        self.assertIn("research show abc123", research.render(out))


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


HAS_TS = shutil.which("node") and os.path.isdir(os.path.join(APP_DIR, "node_modules", "typescript"))

BACKEND = {
    "app/__init__.py": "",
    "app/main.py": "from fastapi import FastAPI\nfrom app.routers import items\n\napp = FastAPI()\n"
                   "app.include_router(items.router, prefix=\"/api/v1\")\n",
    "app/routers/__init__.py": "",
    "app/routers/items.py": (
        "from fastapi import APIRouter\nfrom app.schemas import ItemIn, ItemOut\nfrom app.services.items import ItemService\n\n"
        "router = APIRouter(prefix=\"/items\")\n\n\n"
        "@router.get(\"/{item_id}\", response_model=ItemOut)\ndef get_item(item_id: int):\n    return ItemService().fetch(item_id)\n\n\n"
        "@router.post(\"/\", response_model=ItemOut)\ndef create_item(body: ItemIn):\n    return ItemService().create(body)\n"),
    "app/schemas.py": "from pydantic import BaseModel\n\n\nclass ItemIn(BaseModel):\n    name: str\n    price: float\n\n\n"
                      "class ItemOut(ItemIn):\n    id: int\n",
    "app/services/__init__.py": "",
    "app/services/items.py": "class ItemService:\n    def fetch(self, item_id):\n        return {\"id\": item_id}\n\n"
                             "    def create(self, body):\n        raise ValueError(\"price must be positive\")\n",
    "app/tests/__init__.py": "",
    "app/tests/test_items.py": "from app.services.items import ItemService\n\n\ndef test_fetch():\n    assert ItemService().fetch(1)\n",
}
FRONTEND = {
    "tsconfig.json": '{\n  // Next.js style alias\n  "compilerOptions": { "paths": { "@/*": ["./*"] } }\n}\n',
    "lib/api.ts": "export const api = {\n  items: {\n    get: (id: number) => request(`/items/${id}`),\n"
                  "    create: (body: unknown) => request('/items/', { method: 'POST', body }),\n  },\n};\n\n"
                  "function request(path: string, init?: object) { return fetch(path, init); }\n",
    "lib/direct.ts": "import { http } from './http';\n\nexport function loadItem(id: number) {\n"
                     "  return http.get(`/api/v1/items/${id}`);\n}\n",
    "lib/http.ts": "export const http = { get: (u: string) => fetch(u) };\n",
    "components/item.tsx": "import { api } from '@/lib/api';\n\nexport function ItemView() {\n  api.items.get(1);\n  return null;\n}\n",
}


class WorkspaceTest(unittest.TestCase):
    """The commands as an agent runs them, on a workspace with a backend and (when node is here) a frontend."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = cls.tmp.name
        repos = [("shop", BACKEND)] + ([("web", FRONTEND)] if HAS_TS else [])
        for name, files in repos:
            root = os.path.join(cls.ws, name)
            for rel, text in files.items():
                write(root, rel, text)
            git(root, "init", "-q")
            git(root, "add", ".")
            git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        members = [{"id": n, "name": n, "role": "Engineer", "repos": [n]} for n, _ in repos]
        write(cls.ws, ".claude/team/roster.json", json.dumps({"team": "t", "members": members}))
        cls.before = {n: cls.status(n) for n, _ in repos}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def status(cls, repo):
        return subprocess.run(["git", "-C", os.path.join(cls.ws, repo), "status", "--porcelain", "--untracked-files=all"],
                              capture_output=True, text=True).stdout

    def run_tm(self, *args, ws=None):
        env = dict(os.environ, AISET_WORKSPACE=ws or self.ws)
        env.pop("MSYSTEM", None)
        p = subprocess.run([sys.executable, TEAMMAP, *args], cwd=ws or self.ws, env=env, capture_output=True, text=True, encoding="utf-8")
        return p.returncode, p.stdout + p.stderr

    def json_of(self, *args):
        rc, out = self.run_tm(*args, "--json")
        return rc, json.loads(out)

    def rows(self, res, section):
        return res["sections"].get(section, [])

    def test_contract(self):
        rc, res = self.json_of("impact", "ItemService")
        self.assertEqual(rc, 0)
        for key in ("tool", "version", "params", "summary", "sections", "gaps", "inputs", "id", "record", "truncated", "status", "cached"):
            self.assertIn(key, res)
        self.assertTrue(res["inputs"]["fingerprint"])
        self.assertIn("shop", res["inputs"]["repos"])
        self.assertTrue(os.path.exists(res["record"]))
        same = lambda p: os.path.normcase(os.path.realpath(p))
        self.assertTrue(same(res["record"]).startswith(same(os.path.join(self.ws, ".claude", "team", "graph", "research"))))
        for rows in res["sections"].values():
            for r in rows:
                self.assertIn(r["certainty"], ("exact", "inferred"))
                self.assertTrue(r["why"])

    def test_impact_endpoint(self):
        rc, res = self.json_of("impact", "/api/v1/items/{item_id}", "GET")
        self.assertEqual((rc, res["status"]), (0, "ok"))
        d = self.rows(res, "definition")[0]
        self.assertEqual((d["file"], d["symbol"], d["certainty"]), ("app/routers/items.py", "get_item", "exact"))
        self.assertEqual([m["symbol"] for m in self.rows(res, "models")], ["ItemOut"])
        tests = {t["file"]: t for t in self.rows(res, "tests")}
        self.assertIn("app/tests/test_items.py", tests)  # through the service the handler calls
        self.assertEqual(tests["app/tests/test_items.py"]["certainty"], "inferred")
        if HAS_TS:
            cons = {(c["file"], c["certainty"]) for c in self.rows(res, "consumers")}
            self.assertIn(("lib/direct.ts", "exact"), cons)          # http.get with the same URL
            self.assertIn(("lib/api.ts", "inferred"), cons)          # path literal, same shape and method
            self.assertIn(("components/item.tsx", "inferred"), cons)  # calls api.items.get()
            self.assertFalse(any(c["file"] == "lib/api.ts" and c["line"] == 4 for c in self.rows(res, "consumers")))  # the POST

    def test_impact_symbol_and_model(self):
        rc, res = self.json_of("impact", "ItemService")
        imp = {r["file"]: r for r in self.rows(res, "importers")}
        self.assertEqual(set(imp), {"app/routers/items.py", "app/tests/test_items.py"})
        self.assertTrue(all(r["certainty"] == "exact" and r["imports_name"] for r in imp.values()))
        self.assertEqual([t["file"] for t in self.rows(res, "tests")], ["app/tests/test_items.py"])
        self.assertEqual(self.rows(res, "tests")[0]["certainty"], "exact")
        rc, res = self.json_of("impact", "ItemIn")
        self.assertEqual(res["target"]["kind"], "model")
        self.assertEqual([s["symbol"] for s in self.rows(res, "subclasses")], ["ItemOut"])
        self.assertTrue(any("POST" in e["text"] and e["certainty"] == "exact" for e in self.rows(res, "endpoints")))

    def test_not_found_and_ambiguous(self):
        rc, res = self.json_of("impact", "no_such_thing_anywhere")
        self.assertEqual((rc, res["status"]), (1, "not-found"))
        self.assertTrue(res["gaps"])
        rc, res = self.json_of("impact", "/items")
        self.assertEqual(res["status"], "ambiguous")
        self.assertEqual(len(self.rows(res, "candidates")), 2)

    def test_relevant(self):
        rc, res = self.json_of("relevant", "create item with a price")
        self.assertEqual(rc, 0)
        files = [f"{r['repo']}/{r['file']}" for r in self.rows(res, "files")]
        self.assertIn("shop/app/services/items.py", files[:6])
        top = self.rows(res, "symbols")[0]
        self.assertTrue(top["why"].startswith("matched:"))
        self.assertTrue(any("price" in r["why"] for r in self.rows(res, "models")))
        rc, res = self.json_of("relevant", "ödeme sayfası çeviri")
        self.assertEqual(res["status"], "not-found")
        self.assertTrue(any("lexical" in g for g in res["gaps"]))

    def test_stored_result_is_reused_until_a_file_changes(self):
        _, a = self.json_of("impact", "fetch")
        _, b = self.json_of("impact", "fetch")
        self.assertTrue(b["cached"])
        self.assertEqual(a["id"], b["id"])
        path = os.path.join(self.ws, "shop", "app", "services", "items.py")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\ndef helper():\n    return ItemService().fetch(2)\n")
        try:
            os.utime(path, None)
            _, c = self.json_of("impact", "fetch")
            self.assertFalse(c["cached"])
            self.assertNotEqual(c["inputs"]["fingerprint"], a["inputs"]["fingerprint"])
            self.assertGreaterEqual(c["inputs"]["repos"]["shop"]["uncommitted"], 1)  # not committed, still seen
            self.assertTrue(any(r["symbol"] == "helper" for r in self.rows(c, "callers")))
        finally:
            git(os.path.join(self.ws, "shop"), "checkout", "-q", "app/services/items.py")

    def test_failures_maps_frames_to_symbols(self):
        log = os.path.join(self.ws, "run.log")
        write(self.ws, "run.log",
              "============================= test session starts =============================\n"
              "collected 1 item\n\napp/tests/test_items.py F\n\n"
              "================================== FAILURES ===================================\n"
              "_________________________________ test_create _________________________________\n"
              "app/tests/test_items.py:9: in test_create\n    ItemService().create({})\n"
              "app/services/items.py:6: in create\n    raise ValueError(\"price must be positive\")\n"
              "E   ValueError: price must be positive\n"
              "=========================== short test summary info ===========================\n"
              "FAILED app/tests/test_items.py::test_create - ValueError: price must be positive\n"
              "============================== 1 failed in 0.01s ==============================\n")
        rc, res = self.json_of("failures", log, "--repo", "shop")
        self.assertEqual((rc, res["status"]), (0, "ok"))
        g = self.rows(res, "groups")[0]
        self.assertEqual((g["repo"], g["file"], g["line"], g["symbol"]), ("shop", "app/services/items.py", 6, "ItemService.create"))
        self.assertTrue(os.path.exists(res["log"]))
        with open(res["log"], encoding="utf-8") as f:
            self.assertIn("price must be positive", f.read())

    def test_logs_are_read_not_run(self):
        marker = os.path.join(self.ws, "PWNED")
        write(self.ws, "evil.log",
              "FAILED app/tests/test_items.py::test_x - RuntimeError: run $(touch PWNED) and `touch PWNED`\n"
              f"  File \"{marker}\", line 1, in <module>\n"
              "To fix, run: python -c \"open('PWNED','w')\"\n")
        rc, out = self.run_tm("failures", os.path.join(self.ws, "evil.log"))
        self.assertFalse(os.path.exists(marker), out)

    def test_max_chars_and_show(self):
        rc, out = self.run_tm("relevant", "item service create fetch price", "--max-chars", "400")
        self.assertIn("output cut at 400 characters", out)
        self.assertLessEqual(len(out.split("\n… output cut")[0]), 400)
        _, res = self.json_of("relevant", "item service create fetch price", "--limit", "1")
        self.assertTrue(res["truncated"])
        rc, full = self.run_tm("research", "show", res["id"])
        self.assertEqual(rc, 0)
        shown = sum(len(v) for v in res["sections"].values())
        self.assertGreater(full.count("\n  ~ "), shown)
        rc, listing = self.run_tm("research", "list")
        self.assertIn(res["id"], listing)

    def test_run_saves_full_pytest_log_and_groups_failures(self):
        source = os.path.join(FIX, "pytest.log")
        code = "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(" + repr(source) + ").read_bytes()); sys.exit(1)"
        rc, out = self.run_tm("run", "--", sys.executable, "-c", code)
        self.assertEqual(rc, 1, out)
        self.assertIn("exit code: 1", out)
        self.assertIn("tests: 5 failed test(s)", out)
        self.assertIn("×", out)
        self.assertIn("log lines:", out)
        match = re.search(r"full log: (.+)", out)
        self.assertIsNotNone(match, out)
        log = match.group(1)
        self.assertTrue(log.endswith(".log"), log)
        with open(log, "rb") as f:
            with open(source, "rb") as original:
                self.assertEqual(f.read(), original.read())

    def test_run_pass_log_and_unknown_output(self):
        source = os.path.join(FIX, "pass.log")
        code = "import pathlib,sys; sys.stdout.write(pathlib.Path(" + repr(source) + ").read_text())"
        rc, out = self.run_tm("run", "--", sys.executable, "-c", code)
        self.assertEqual(rc, 0, out)
        self.assertIn("1 passed", out)
        self.assertIn("full log:", out)

        code = "for i in range(35): print('line', i)"
        rc, out = self.run_tm("run", "--", sys.executable, "-c", code)
        self.assertEqual(rc, 0, out)
        self.assertIn("output tail (last 30 lines):", out)
        self.assertNotIn("line 4\n", out)
        self.assertIn("line 5\n", out)
        self.assertIn("line 34\n", out)
        self.assertIn("full log:", out)

    def test_run_nonzero_unparsed_output_shows_tail(self):
        code = "print('coverage threshold not met'); raise SystemExit(2)"
        rc, out = self.run_tm("run", "--", sys.executable, "-c", code)
        self.assertEqual(rc, 2, out)
        self.assertIn("output tail (last 30 lines):", out)
        self.assertIn("coverage threshold not met", out)
        self.assertIn("full log:", out)

    def test_run_interrupt_keeps_log_and_returns_130(self):
        with tempfile.TemporaryDirectory() as store:
            old_store = research.STORE
            research.STORE = store
            try:
                with patch("aiset.research.subprocess.run", side_effect=KeyboardInterrupt):
                    from contextlib import redirect_stdout
                    from io import StringIO
                    output = StringIO()
                    with redirect_stdout(output):
                        rc = research._run_cmd([sys.executable, "-c", "pass"])
                self.assertEqual(rc, 130)
                self.assertIn("full log:", output.getvalue())
                self.assertEqual(len([name for name in os.listdir(os.path.join(store, "runs")) if name.endswith(".log")]), 1)
            finally:
                research.STORE = old_store

    def test_no_index(self):
        with tempfile.TemporaryDirectory() as empty:
            write(empty, ".claude/team/roster.json", json.dumps({"team": "e", "members": [{"id": "pm", "name": "P", "role": "PM"}]}))
            rc, out = self.run_tm("impact", "anything", "--json", ws=empty)
            res = json.loads(out)
            self.assertEqual(rc, 1)
            self.assertTrue(any("no repositories to index" in g for g in res["gaps"]))

    def test_zz_repos_untouched(self):
        for repo, before in self.before.items():
            self.assertEqual(self.status(repo), before, repo)


if __name__ == "__main__":
    unittest.main()
