"""Initialize single-package repositories without moving the checkout."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiset import init_team  # noqa: E402


class SingleRepoTest(unittest.TestCase):
    def test_root_python_project_initializes_once_and_stays_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            (root / ".git").mkdir(parents=True)
            (root / "src" / "pkg").mkdir(parents=True)
            (root / "src" / "pkg" / "__init__.py").write_text("")
            (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
            with patch.dict(os.environ, {"AISET_HOME": str(Path(tmp) / "home")}), \
                 patch.object(sys, "argv", ["aiset-init", str(root), "--no-launcher"]), \
                 patch("builtins.print"):
                init_team.main()
                init_team.main()
            roster = json.loads((root / ".claude/team/roster.json").read_text())
            owners = [m for m in roster["members"] if m.get("repos") == ["."]]
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0]["id"], "root")
            self.assertEqual(owners[0]["checks"], ["pytest"])
            self.assertTrue((root / ".claude/agents/root.md").is_file())
            self.assertIn("/.claude/", (root / ".git/info/exclude").read_text())

    def test_root_node_and_existing_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "package.json").write_text('{"scripts":{"test":"node --test"}}')
            repos = init_team.scan(str(root))
            self.assertEqual([r["name"] for r in repos], ["."])
            self.assertEqual(repos[0]["checks"], ["npm run test"])
            (root / "api").mkdir()
            (root / "api/pyproject.toml").write_text('[project]\nname="api"\n')
            self.assertEqual([r["name"] for r in init_team.scan(str(root))], ["api", "."])


def _init(root, tmp, *args):
    with patch.dict(os.environ, {"AISET_HOME": str(Path(tmp) / "home")}), \
         patch.object(sys, "argv", ["aiset-init", str(root), "--no-launcher", *args]), \
         patch("builtins.print") as out:
        init_team.main()
    return json.loads((root / ".claude/team/roster.json").read_text()), out


class UniqueMemberIdTest(unittest.TestCase):
    def _single_package_repo(self, tmp):
        root = Path(tmp) / "project"
        (root / ".git").mkdir(parents=True)
        (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
        return root

    def test_service_called_root_added_after_root_unit_gets_its_own_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._single_package_repo(tmp)
            _init(root, tmp)
            (root / "root").mkdir()
            (root / "root/pyproject.toml").write_text('[project]\nname="svc"\n')
            roster, _ = _init(root, tmp)
            ids = [m["id"] for m in roster["members"]]
            self.assertEqual(len(ids), len(set(ids)))
            owners = {m["id"]: m["repos"] for m in roster["members"] if m.get("repos")}
            self.assertEqual(owners["root"], ["."])
            self.assertEqual(owners["root-2"], ["root"])
            files = {m["knowledge"] for m in roster["members"] if m.get("knowledge")}
            self.assertEqual(len(files), len([m for m in roster["members"] if m.get("knowledge")]))
            self.assertTrue((root / ".claude/agents/root.md").is_file())
            self.assertTrue((root / ".claude/agents/root-2.md").is_file())

    def test_service_named_like_a_fixed_member_does_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mono"
            (root / ".git").mkdir(parents=True)
            for name in ("pm", "tester", "api"):
                (root / name).mkdir()
                (root / name / "pyproject.toml").write_text('[project]\nname="%s"\n' % name)
            roster, _ = _init(root, tmp)
            ids = [m["id"] for m in roster["members"]]
            self.assertEqual(len(ids), len(set(ids)))
            by_repo = {r: m["id"] for m in roster["members"] for r in m.get("repos") or []}
            self.assertEqual(by_repo["pm"], "pm-2")
            self.assertEqual(by_repo["tester"], "tester-2")
            self.assertEqual(by_repo["api"], "api")
            self.assertIn("Project Manager", next(m["role"] for m in roster["members"] if m["id"] == "pm"))

    def test_root_owner_keeps_the_root_but_is_scoped_once_a_service_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._single_package_repo(tmp)
            first, _ = _init(root, tmp)
            self.assertNotIn("service folders", next(m for m in first["members"] if m["id"] == "root")["duty"])
            (root / "svc").mkdir()
            (root / "svc/pyproject.toml").write_text('[project]\nname="svc"\n')
            second, out = _init(root, tmp)
            owner = next(m for m in second["members"] if m["id"] == "root")
            self.assertEqual(owner["repos"], ["."])
            self.assertEqual(owner["duty"].count("service folders"), 1)
            root_md = (root / ".claude/agents/root.md").read_text(encoding="utf-8")
            self.assertIn("pytest", root_md)  # the root engineer keeps its own checks
            self.assertIn("You own:\n\n- `.` at ", root_md)  # its own unit, not the new service's
            self.assertNotIn("- `svc` (", root_md)
            self.assertTrue(any("still owns the repository root" in str(c.args[0]) for c in out.call_args_list if c.args))
            third, _ = _init(root, tmp)  # idempotent: the note is not appended twice
            self.assertEqual(next(m for m in third["members"] if m["id"] == "root")["duty"].count("service folders"), 1)


class RootPackageOwnershipTest(unittest.TestCase):
    """A monorepo root with a manifest keeps its root unit (id root) alongside service folders."""

    SUPPORT_SHAPES = (("tests", "requirements.txt"), ("scripts", "requirements.txt"),
                      ("tools", "Dockerfile"), ("docs", "package.json"))
    MANIFEST_CONTENT = {"requirements.txt": "somepkg\n",
                        "package.json": '{"scripts": {"test": "node --test"}}',
                        "Dockerfile": "FROM python:3.12\n"}
    SUPPORT_CHECKS = {"requirements.txt": "pytest", "Dockerfile": "docker compose -f <file> config -q"}

    def test_root_unit_scanned_alongside_support_folder_units(self):
        for folder, manifest in self.SUPPORT_SHAPES:
            with self.subTest(shape=(folder, manifest)):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "mono"
                    (root / ".git").mkdir(parents=True)
                    (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
                    (root / folder).mkdir()
                    (root / folder / manifest).write_text(self.MANIFEST_CONTENT[manifest])
                    expected_units = ["."] if folder == "docs" else [folder, "."]  # scan() skips docs/
                    self.assertEqual([r["name"] for r in init_team.scan(str(root))], expected_units)
                    first, _ = _init(root, tmp)
                    second, _ = _init(root, tmp)
                    self.assertEqual([m["id"] for m in first["members"]], [m["id"] for m in second["members"]])
                    for roster in (first, second):
                        owners = [m for m in roster["members"] if m.get("repos") == ["."]]
                        self.assertEqual(len(owners), 1)
                        self.assertEqual(owners[0]["id"], "root")
                        self.assertEqual(owners[0]["checks"], ["pytest"])
                    if folder != "docs":  # scan() skips docs/, so it gets no engineer anywhere
                        svc = next(m for m in second["members"] if m.get("repos") == [folder])
                        self.assertEqual(svc["checks"], [f"cd {folder} && {self.SUPPORT_CHECKS[manifest]}"])

    def test_root_unit_kept_alongside_a_real_service_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mono"
            (root / ".git").mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
            (root / "api").mkdir()
            (root / "api/pyproject.toml").write_text('[project]\nname="api"\n')
            self.assertEqual([r["name"] for r in init_team.scan(str(root))], ["api", "."])
            roster, _ = _init(root, tmp)
            by_repo = {r: m for m in roster["members"] for r in m.get("repos") or []}
            self.assertEqual(by_repo["api"]["id"], "api")
            self.assertEqual(by_repo["."]["id"], "root")
            self.assertIsNot(by_repo["api"], by_repo["."])
            self.assertIn("service folders", by_repo["."]["duty"])

    def test_group_stack_one_engineer_owns_root_and_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mono"
            (root / ".git").mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
            (root / "api").mkdir()
            (root / "api/pyproject.toml").write_text('[project]\nname="api"\n')
            roster, _ = _init(root, tmp, "--group", "stack")
            owners = [m for m in roster["members"] if m.get("repos")]
            self.assertEqual(len(owners), 1)
            self.assertEqual(set(owners[0]["repos"]), {"api", "."})
            self.assertNotIn("service folders", owners[0]["duty"])

    def test_root_without_manifest_stays_unowned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mono"
            (root / ".git").mkdir(parents=True)
            (root / "api").mkdir()
            (root / "api/pyproject.toml").write_text('[project]\nname="api"\n')
            self.assertEqual([r["name"] for r in init_team.scan(str(root))], ["api"])

    def test_root_unit_survives_deleting_the_root_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mono"
            (root / ".git").mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nname="demo"\n')
            _init(root, tmp)
            (root / "pyproject.toml").unlink()
            (root / "api").mkdir()
            (root / "api/pyproject.toml").write_text('[project]\nname="api"\n')
            roster, _ = _init(root, tmp)
            owner = next(m for m in roster["members"] if m.get("repos") == ["."])
            self.assertEqual(owner["id"], "root")
            self.assertEqual(owner["checks"], ["pytest"])  # kept from the first init's roster
            self.assertEqual(next(m for m in roster["members"] if m.get("repos") == ["api"])["id"], "api")
            root_md = (root / ".claude/agents/root.md").read_text(encoding="utf-8")
            self.assertIn("You own:\n\n- `.` at ", root_md)  # carry_root_unit kept the root unit in repos
            self.assertNotIn("- `api`", root_md)
