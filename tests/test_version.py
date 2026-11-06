"""Version flags must work without an initialized workspace."""

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiset import __version__  # noqa: E402


class VersionTest(unittest.TestCase):
    def test_version_flags(self):
        source = Path(__file__).resolve().parents[1] / "src"
        for module in ("server", "init_team", "teammap"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as home:
                env = dict(os.environ)
                env.update(HOME=home, USERPROFILE=home, AISET_HOME=home,
                           AISET_WORKSPACE=home, PYTHONPATH=str(source),
                           PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run(
                    [sys.executable, "-m", "aiset." + module, "--version"],
                    cwd=home, env=env, capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().split()[-1], __version__)
                self.assertEqual(list(Path(home).iterdir()), [])

    def test_busy_port_fails_without_traceback(self):
        source = Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory() as home:
            team = Path(home) / ".claude" / "team"
            team.mkdir(parents=True)
            (team / "roster.json").write_text(
                '{"team":"t","members":[]}', encoding="utf-8"
            )
            env = dict(os.environ)
            env.update(HOME=home, USERPROFILE=home, AISET_HOME=home,
                       AISET_WORKSPACE=home, PYTHONPATH=str(source),
                       PYTHONDONTWRITEBYTECODE="1")
            listener = socket.socket()
            self.addCleanup(listener.close)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = subprocess.run(
                [sys.executable, "-m", "aiset.server", "--no-browser", "--port", str(port)],
                cwd=home, env=env, capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn(str(port), result.stderr)
            self.assertIn("--port 0", result.stderr)
