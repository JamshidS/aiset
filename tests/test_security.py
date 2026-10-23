"""The office's HTTP surface: task ids can't leave the team folder, and other hosts can't read (DNS rebinding)."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import core, server  # noqa: E402
from aiset.server import Handler  # noqa: E402


class TaskIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        team = os.path.join(self.tmp.name, "team")
        self.boards, self.episodes = os.path.join(team, "tasks"), os.path.join(team, "episodes")
        os.makedirs(os.path.join(self.boards, "2026-10-18-fix-bugs"))
        os.makedirs(self.episodes)
        with open(os.path.join(team, "brief.md"), "w") as f:  # what ".." would reach from the boards folder
            f.write("not a task\n")
        with open(os.path.join(self.episodes, "2026-10-18-fix-bugs.md"), "w") as f:
            f.write("TASK: fix bugs\n")
        patches = [unittest.mock.patch.object(core, "BOARDS", self.boards), unittest.mock.patch.object(core, "EPISODES", self.episodes)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_real_task_is_found(self):
        self.assertTrue(core._board_dir("2026-10-18-fix-bugs"))
        self.assertTrue(core._board_dir("20261018-fix-bugs"))  # legacy date format
        self.assertTrue(core._episode_path("2026-10-18-fix-bugs").endswith("2026-10-18-fix-bugs.md"))

    def test_ids_that_leave_the_folder_are_refused(self):
        for bad in ("..", ".", "../..", "a/../..", "..\\..", "", ".hidden", "x\0y"):
            with self.subTest(bad=bad):
                self.assertIsNone(core._board_dir(bad))
                self.assertIsNone(core.board_detail(bad))
                self.assertIsNone(core.task_detail(None, bad))
                self.assertIsNone(core._episode_path(bad))

    def test_paths_stay_as_configured(self):
        """No symlink resolution: the folder is reported under BOARDS as configured."""
        self.assertEqual(core._board_dir("2026-10-18-fix-bugs"), os.path.join(self.boards, "2026-10-18-fix-bugs"))


class HostTest(unittest.TestCase):
    def handler(self, host, origin=None):
        h = Handler.__new__(Handler)  # no socket: only the header checks are exercised
        h.port = 8765
        h.headers = {k: v for k, v in (("Host", host), ("Origin", origin)) if v is not None}
        return h

    def test_reads_need_this_servers_host(self):
        for host in ("127.0.0.1:8765", "localhost:8765", "LocalHost:8765", "localhost.:8765"):
            with self.subTest(host=host):
                self.assertTrue(self.handler(host)._host_ok())
        for host in ("evil.example:8765", "127.0.0.1:9999", "localhost.evil.example:8765", "127.0.0.1", "", None):
            with self.subTest(host=host):
                self.assertFalse(self.handler(host)._host_ok())

    def test_port_80_without_a_port_in_the_host(self):
        h = self.handler("127.0.0.1")
        h.port = 80
        self.assertTrue(h._host_ok())
        h.headers["Origin"] = "http://localhost"
        self.assertTrue(h._same_origin())

    def test_writes_also_refuse_a_foreign_origin(self):
        self.assertTrue(self.handler("127.0.0.1:8765", "http://127.0.0.1:8765")._same_origin())
        self.assertFalse(self.handler("127.0.0.1:8765", "https://evil.example")._same_origin())
        self.assertFalse(self.handler("evil.example:8765")._same_origin())


class ServerMalformedInputTest(unittest.TestCase):
    def setUp(self):
        self.handler = Handler.__new__(Handler)
        self.handler.port = 8765
        self.handler.lock = threading.Lock()
        self.handler.office = unittest.mock.MagicMock()
        self.handler.office.roster = {"team": "Test", "members": [{"id": "pm", "name": "PM"}]}
        self.sent = []
        self.handler._send = lambda code, body, ctype: self.sent.append((code, body, ctype))

    def test_brain_live_invalid_since_falls_back_to_1800(self):
        self.handler.headers = {"Host": "127.0.0.1:8765"}
        for bad_since in ("abc", "", "nan-not-a-number", "invalid"):
            with self.subTest(since=bad_since):
                self.handler.path = f"/api/brain/live?since={bad_since}"
                self.sent.clear()
                with unittest.mock.patch("aiset.brain.live", return_value={"live": True}) as mock_live:
                    self.handler.do_GET()
                    self.assertEqual(self.sent[0][0], 200)
                    mock_live.assert_called_once_with(self.handler.office, self.handler.office.roster, 1800.0)

    def test_post_settings_rejects_non_object_json(self):
        self.handler.path = "/api/settings"
        self.handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Type": "application/json",
        }
        for bad_body in (b"[]", b'"string"', b"123", b"true"):
            with self.subTest(body=bad_body):
                self.sent.clear()
                self.handler.headers["Content-Length"] = str(len(bad_body))
                self.handler.rfile = io.BytesIO(bad_body)
                self.handler.do_POST()
                self.assertEqual(self.sent[0][0], 400)
                self.assertIn(b"body must be an object", self.sent[0][1])

    def test_post_settings_rejects_malformed_roster_or_members(self):
        self.handler.path = "/api/settings"
        self.handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Type": "application/json",
        }
        for payload in (
            b'{"roster": "not-an-object"}',
            b'{"roster": [1, 2]}',
            b'{"roster": {"members": "not-a-list"}}',
            b'{"roster": {"members": ["not-a-dict"]}}',
            b'{"settings": "not-an-object"}',
        ):
            with self.subTest(payload=payload):
                self.sent.clear()
                self.handler.headers["Content-Length"] = str(len(payload))
                self.handler.rfile = io.BytesIO(payload)
                self.handler.do_POST()
                self.assertEqual(self.sent[0][0], 400)

    def test_post_settings_rejects_non_string_member_id(self):
        self.handler.path = "/api/settings"
        self.handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Type": "application/json",
        }
        for bad_payload in (
            b'{"roster": {"members": [{"id": ["x"]}]}}',
            b'{"roster": {"members": [{"id": {"nested": 1}}]}}',
            b'{"roster": {"members": [{"id": 42}]}}',
        ):
            with self.subTest(payload=bad_payload):
                self.sent.clear()
                self.handler.headers["Content-Length"] = str(len(bad_payload))
                self.handler.rfile = io.BytesIO(bad_payload)
                self.handler.do_POST()
                self.assertEqual(self.sent[0][0], 400)
                self.assertIn(b"each member edit needs a string 'id'", self.sent[0][1])

    def test_post_settings_negative_content_length_does_not_hang(self):
        self.handler.path = "/api/settings"
        self.handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Type": "application/json",
            "Content-Length": "-1",
        }
        self.sent.clear()
        self.handler.rfile = io.BytesIO(b'{"ignored": true}')
        self.handler.settings_payload = lambda: {"ok": True}
        self.handler.do_POST()
        self.assertEqual(self.sent[0][0], 200)


class ReloadRosterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roster_path = os.path.join(self.tmp.name, "roster.json")
        from aiset import workspace
        self.addCleanup(setattr, server, "ROSTER", server.ROSTER)
        self.addCleanup(setattr, workspace, "ROSTER", workspace.ROSTER)
        server.ROSTER = self.roster_path
        workspace.ROSTER = self.roster_path
        self.office = unittest.mock.MagicMock()
        self.good_roster = {"team": "Good", "members": [{"id": "pm", "name": "Deniz"}]}
        self.office.roster = self.good_roster
        with open(self.roster_path, "w", encoding="utf-8") as f:
            json.dump(self.good_roster, f)
        server.reload_roster(self.office, force=True)

    def test_reload_roster_rejects_missing_members_or_bad_member_shape(self):
        bad_shapes = [
            {"team": "Bad"},
            {"team": "Bad", "members": "not a list"},
            {"team": "Bad", "members": [{"role": "no id or name"}]},
            {"team": "Bad", "members": [{"id": "pm"}]},
            {"team": "Bad", "members": [{"name": "Deniz"}]},
            {"team": "Bad", "members": ["string"]},
        ]
        for bad in bad_shapes:
            with self.subTest(shape=bad):
                with open(self.roster_path, "w", encoding="utf-8") as f:
                    json.dump(bad, f)
                result = server.reload_roster(self.office, force=True)
                self.assertIsNone(result)
                self.assertEqual(self.office.roster, self.good_roster)


if __name__ == "__main__":
    unittest.main()
