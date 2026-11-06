"""Architecture view: a Python service's dependencies and framework from pyproject.toml."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import archmap  # noqa: E402

PYPROJECT_STANDARD = """[project]
name = "svc"
dependencies = [
    "fastapi[standard]>=0.115",
    "redis",
    "celery[redis]",
    "sqlalchemy>=2",
]
"""

PYPROJECT_POETRY = """[tool.poetry]
name = "svc"
version = "0.1.0"

[tool.poetry.dependencies]
python = "^3.11"
fastapi = {extras = ["all"], version = "^0.110"}
celery = {extras = ["redis"], version = "^5.3"}
"""


class PyprojectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = tmp.name
        self.repo = os.path.join(self.workspace, "svc")
        os.makedirs(self.repo)

    def write_pyproject(self, content):
        path = os.path.join(self.repo, "pyproject.toml")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path

    def test_dependencies_with_extras_standard_pep621(self):
        pyproject_path = self.write_pyproject(PYPROJECT_STANDARD)
        py, *_ = archmap._deps(self.repo, [(pyproject_path, "pyproject.toml")])
        self.assertIn("fastapi", py)
        self.assertIn("celery", py)
        self.assertIn("redis", py)
        self.assertIn("sqlalchemy", py)

        with patch.object(archmap, "CODE_ROOT", self.workspace):
            r = archmap.scan_repo("svc")
        self.assertEqual(r["framework"], "FastAPI")
        self.assertEqual(r["kind"], "api")

    def test_dependencies_with_extras_poetry(self):
        pyproject_path = self.write_pyproject(PYPROJECT_POETRY)
        py, *_ = archmap._deps(self.repo, [(pyproject_path, "pyproject.toml")])
        self.assertIn("fastapi", py)
        self.assertIn("celery", py)

        with patch.object(archmap, "CODE_ROOT", self.workspace):
            r = archmap.scan_repo("svc")
        self.assertEqual(r["framework"], "FastAPI")
        self.assertEqual(r["kind"], "api")


if __name__ == "__main__":
    unittest.main()
