"""Regression coverage for route methods and application traceback frames (#31)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aiset import research, teammap  # noqa: E402


def fixture(name):
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "research", name), encoding="utf-8") as f:
        return f.read()


class RouteMethodsTest(unittest.TestCase):
    def test_api_route_expands_methods_and_preserves_metadata(self):
        data = teammap.py_extract(fixture("api_route.py"))
        self.assertEqual([r["method"] for r in data["routes"]], ["GET", "POST"])
        for route in data["routes"]:
            self.assertEqual({k: route[k] for k in ("path", "router", "handler", "response", "line")},
                             {"path": "/items", "router": "router", "handler": "items", "response": "Item", "line": 6})

    def test_api_route_default_and_tuple_methods(self):
        data = teammap.py_extract(
            '@app.api_route("/default")\ndef default():\n    pass\n'
            '@app.api_route("/tuple", methods=("PUT", "DELETE"))\ndef update():\n    pass\n'
            '@app.post("/regular")\ndef regular():\n    pass\n')
        self.assertEqual([(r["path"], r["method"]) for r in data["routes"]],
                         [("/default", "GET"), ("/tuple", "PUT"), ("/tuple", "DELETE"), ("/regular", "POST")])

    def test_nonliteral_api_route_methods_are_unknown_instead_of_get(self):
        for value in ("METHODS", "None", '["GET", METHOD]'):
            with self.subTest(value=value):
                data = teammap.py_extract(f'@app.api_route("/items", methods={value})\ndef items():\n    pass\n')
                self.assertEqual([r["method"] for r in data["routes"]], ["UNKNOWN"])

    def test_api_route_methods_are_deduplicated_after_case_normalization(self):
        data = teammap.py_extract(
            '@app.api_route("/items", methods=["GET", "get", "post", "POST"])\ndef items():\n    pass\n')
        self.assertEqual([r["method"] for r in data["routes"]], ["GET", "POST"])

    def test_existing_index_is_refreshed_without_source_changes(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = os.path.join(workspace, "shop")
            index = os.path.join(workspace, "index")
            os.makedirs(root)
            os.makedirs(index)
            path = os.path.join(root, "api.py")
            source = '@app.api_route("/items", methods=["GET", "POST"])\ndef items():\n    pass\n'
            with open(path, "w", encoding="utf-8") as f:
                f.write(source)
            subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
            subprocess.run(["git", "-C", root, "add", "api.py"], check=True, capture_output=True)
            stale = teammap.py_extract(source)
            stale["routes"] = [dict(stale["routes"][0], method="API_ROUTE")]
            with open(os.path.join(index, "shop.json"), "w", encoding="utf-8") as f:
                json.dump({"version": 6, "files": {"api.py": {
                    "mtime": os.path.getmtime(path), "lang": "py", "data": stale}}}, f)
            with patch.object(teammap, "CODE_ROOT", workspace), patch.object(teammap, "INDEX", index):
                repo = teammap.load_repo("shop")
            self.assertEqual(repo["changed"], 1)
            linked = teammap.link([repo])
            self.assertEqual({r["method"] for r in linked["endpoints"]}, {"GET", "POST"})
            self.assertEqual(research.impact([repo], linked, "/items", "GET")["status"], "ok")

    def test_cached_dynamic_methods_are_rebuilt_without_source_changes(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = os.path.join(workspace, "shop")
            index = os.path.join(workspace, "index")
            os.makedirs(root)
            os.makedirs(index)
            path = os.path.join(root, "api.py")
            source = '@app.api_route("/items", methods=METHODS)\ndef items():\n    pass\n'
            with open(path, "w", encoding="utf-8") as f:
                f.write(source)
            subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
            subprocess.run(["git", "-C", root, "add", "api.py"], check=True, capture_output=True)
            stale = teammap.py_extract(source)
            stale["routes"] = [dict(stale["routes"][0], method="GET")]
            with open(os.path.join(index, "shop.json"), "w", encoding="utf-8") as f:
                json.dump({"version": 7, "files": {"api.py": {
                    "mtime": os.path.getmtime(path), "lang": "py", "data": stale}}}, f)
            with patch.object(teammap, "CODE_ROOT", workspace), patch.object(teammap, "INDEX", index):
                repo = teammap.load_repo("shop")
            self.assertEqual(repo["changed"], 1)
            linked = teammap.link([repo])
            self.assertEqual([r["method"] for r in linked["endpoints"]], ["UNKNOWN"])
            self.assertEqual(research.impact([repo], linked, "/items", "GET")["status"], "not-found")


class InternalFramesTest(unittest.TestCase):
    def test_project_internal_folders_are_not_runtime_libraries(self):
        for path in ("app/internal/boom.py", "internal/boom.go", "service/internal/runner.py",
                     "app/internal/modules/billing.py", "service/internal/process/runner.py", "internal/modules/billing.py",
                     r"C:\work\shop\app\internal\boom.py"):
            with self.subTest(path=path):
                self.assertFalse(research._is_library(path))

    def test_known_runtime_and_dependency_frames_stay_excluded(self):
        for path in ("node:internal/timers", "internal/modules/cjs/loader.js", "internal/process/task_queues.js",
                     "internal/bootstrap/node.js", "internal/errors.js", "internal/validators.js", "internal/timers.js",
                     "internal/main/run_main_module.js", "internal/fs/utils.js", "internal/example.cjs", "internal/example.mjs",
                     "node_modules/pkg/index.js", "venv/lib/python3.12/foo.py",
                     "venv/site-packages/pkg.py", "dist-packages/pkg.py", "<frozen importlib._bootstrap>"):
            with self.subTest(path=path):
                self.assertTrue(research._is_library(path))

    def test_old_node_frames_do_not_hide_unindexed_project_frame(self):
        result, failures = research.failures(fixture("jest_old_node.log"), "jest")
        self.assertEqual(result["sections"]["groups"][0]["file"], "src/app.test.js")
        self.assertEqual([fr["rel"] for fr in failures[0]["shown_frames"]], ["src/app.test.js"])

    def test_indexed_project_internal_javascript_frames_are_mapped_first(self):
        for path in ("internal/errors.js", "internal/modules/billing.js", "internal/helper.cjs", "internal/helper.mjs"):
            with self.subTest(path=path):
                repo = {"name": "shop", "root": "/workspace/shop", "files": {path: {"data": {"symbols": []}}}}
                log = (' FAIL  src/app.test.js\n\n  ● app fails\n\n    TypeError: boom\n\n'
                       f'      at explode ({path}:2:3)\n'
                       '      at Object.<anonymous> (src/app.test.js:5:3)\n\nTests: 1 failed, 1 total\n')
                result, _ = research.failures(log, "jest", [repo], "shop")
                group = result["sections"]["groups"][0]
                self.assertEqual((group["repo"], group["file"]), ("shop", path))

    def test_existing_dependency_files_are_not_mapped_as_project_code(self):
        with tempfile.TemporaryDirectory() as root:
            path = "node_modules/pkg/index.js"
            os.makedirs(os.path.join(root, "node_modules", "pkg"))
            with open(os.path.join(root, path), "w", encoding="utf-8") as f:
                f.write("throw Error('dependency');\n")
            repo = {"name": "shop", "root": root, "files": {}}
            self.assertIsNone(research._map_frame({"path": path}, {"shop": repo}, "shop"))

    def test_failure_groups_at_innermost_internal_project_function(self):
        source = 'def explode():\n    raise ValueError("boom")\n'
        repo = {"name": "shop", "root": "/workspace/shop", "files": {
            "app/internal/boom.py": {"lang": "py", "data": teammap.py_extract(source)},
            "tests/test_boom.py": {"lang": "py", "data": teammap.py_extract("def test_boom():\n    explode()\n")}}}
        result, failures = research.failures(fixture("app_internal.log"), "pytest", [repo], "shop")
        group = result["sections"]["groups"][0]
        self.assertEqual((group["repo"], group["file"], group["symbol"]), ("shop", "app/internal/boom.py", "explode"))
        self.assertEqual(failures[0]["raised_at"]["rel"], "app/internal/boom.py")
        self.assertFalse(any("outside the indexed repos" in gap for gap in result["gaps"]))
