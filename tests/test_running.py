"""Running offices: each office records itself under AISET_HOME, dead pids are skipped, and a busy port moves on."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import running, server  # noqa: E402


def dead_pid():
    """The pid of a process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class RunningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.old_home = os.environ.get("AISET_HOME")
        os.environ["AISET_HOME"] = os.path.join(self.root, "aiset-home")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("AISET_HOME", None)
        else:
            os.environ["AISET_HOME"] = self.old_home
        self.tmp.cleanup()

    def workspace(self, name):
        ws = os.path.join(self.root, name)
        os.makedirs(ws, exist_ok=True)
        return ws

    def test_register_lists_and_unregister_removes(self):
        ws = self.workspace("shop")
        path = running.register(ws, "shop", 8765)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        self.assertEqual((entry["pid"], entry["team"], entry["port"]), (os.getpid(), "shop", 8765))
        self.assertEqual(entry["url"], "http://127.0.0.1:8765")
        self.assertEqual([e["team"] for e in running.running()], ["shop"])
        running.unregister(path)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(running.running(), [])

    def test_dead_pid_is_skipped_and_cleaned_up(self):
        stale = os.path.join(os.environ["AISET_HOME"], "running", "stale.json")
        os.makedirs(os.path.dirname(stale))
        with open(stale, "w", encoding="utf-8") as f:
            json.dump({"pid": dead_pid(), "team": "old", "workspace": "", "port": 8766, "url": "x"}, f)
        live = running.register(self.workspace("blog"), "blog", 8767)
        self.assertEqual([e["team"] for e in running.running()], ["blog"])
        self.assertFalse(os.path.exists(stale))
        running.unregister(live)

    def test_find_matches_workspace_by_normalised_path(self):
        ws = self.workspace("shop")
        path = running.register(ws, "shop", 8765)
        self.assertEqual(running.find(ws)["port"], 8765)
        self.assertEqual(running.find(os.path.join(ws, "..", "shop"))["port"], 8765)
        self.assertIsNone(running.find(self.workspace("other")))
        self.assertIsNone(running.find(""))
        running.unregister(path)

    def test_corrupt_entry_is_ignored(self):
        folder = os.path.join(os.environ["AISET_HOME"], "running")
        os.makedirs(folder)
        with open(os.path.join(folder, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(running.running(), [])


class PortTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("AISET_HOME")
        os.environ["AISET_HOME"] = os.path.join(self.tmp.name, "aiset-home")

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("AISET_HOME", None)
        else:
            os.environ["AISET_HOME"] = self.old_home
        self.tmp.cleanup()

    def occupy(self):
        """A socket that holds a port, as another office would. Returns (socket, port)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        return sock, sock.getsockname()[1]

    def test_default_port_busy_moves_to_a_free_one(self):
        _, busy = self.occupy()
        with unittest.mock.patch.object(server, "DEFAULT_PORT", busy):
            httpd = server.bind_office(None)
            self.addCleanup(httpd.server_close)
        self.assertNotEqual(httpd.server_address[1], busy)
        self.assertTrue(httpd.server_address[1] > 0)

    def test_explicit_busy_port_still_fails(self):
        _, busy = self.occupy()
        with self.assertRaises(OSError):
            server.bind_office(busy)

    def test_free_default_port_is_used(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()
        with unittest.mock.patch.object(server, "DEFAULT_PORT", free):
            httpd = server.bind_office(None)
            self.addCleanup(httpd.server_close)
        self.assertEqual(httpd.server_address[1], free)


if __name__ == "__main__":
    unittest.main()
