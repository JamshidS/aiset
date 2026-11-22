"""Notifications: hook events, the switches, the board watcher, the PM hooks and the launcher's tab title."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import init_team, notify, registry, settings  # noqa: E402


class NotifyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.team = os.path.join(self.tmp.name, "ws", ".claude", "team")
        os.makedirs(self.team)
        with open(os.path.join(self.team, "roster.json"), "w", encoding="utf-8") as f:
            json.dump({"team": "Shop", "members": []}, f)
        p = patch.object(notify, "desktop_toast", return_value=True)
        self.toast = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def switch(self, **kw):
        with open(os.path.join(self.team, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"notify": kw}, f)


class SendTest(NotifyTestCase):
    def test_all_on_by_default_logs_and_toasts_with_the_team_name(self):
        rec = notify.send(self.team, "waiting", "Allow Bash?")
        self.assertEqual(rec["kind"], "waiting")
        self.toast.assert_called_once()
        self.assertEqual(self.toast.call_args[0][0], "Shop")
        self.assertIn("Allow Bash?", self.toast.call_args[0][1])
        self.assertEqual(notify.recent(self.team)[0]["message"], "Allow Bash?")

    def test_a_kind_that_is_off_sends_nothing(self):
        self.switch(done=False)
        self.assertIsNone(notify.send(self.team, "done", "finished"))
        self.toast.assert_not_called()
        self.assertEqual(notify.recent(self.team), [])

    def test_office_off_skips_the_log_and_desktop_off_skips_the_toast(self):
        self.switch(office=False)
        notify.send(self.team, "done", "x")
        self.assertEqual(notify.recent(self.team), [])
        self.assertEqual(self.toast.call_count, 1)
        self.switch(desktop=False)
        notify.send(self.team, "done", "y")
        self.assertEqual(self.toast.call_count, 1)
        self.assertEqual(notify.recent(self.team)[0]["message"], "y")

    def test_unknown_kind_and_broken_settings_never_raise(self):
        self.assertIsNone(notify.send(self.team, "nope", "x"))
        with open(os.path.join(self.team, "settings.json"), "w") as f:
            f.write("{not json")
        self.assertTrue(notify.send(self.team, "done", "x"))

    def test_recent_is_newest_first(self):
        for i in range(3):
            notify.send(self.team, "done", f"m{i}")
        self.assertEqual([n["message"] for n in notify.recent(self.team)], ["m2", "m1", "m0"])


class HookTest(NotifyTestCase):
    def test_notification_hook_uses_the_message_field(self):
        notify.run_hook(self.team, "waiting", json.dumps({"message": "Claude needs your permission to use Bash"}))
        self.assertEqual(notify.recent(self.team)[0]["message"], "Claude needs your permission to use Bash")

    def test_stop_hook_without_or_with_bad_json_still_notifies(self):
        notify.run_hook(self.team, "done", "")
        notify.run_hook(self.team, "done", "garbage")
        self.assertEqual(len(notify.recent(self.team)), 2)

    def test_the_script_exits_zero_even_for_nonsense(self):
        script = os.path.join(os.path.dirname(notify.__file__), "notify.py")
        for args in (["hook"], ["hook", os.path.join(self.tmp.name, "nowhere"), "bogus"], []):
            r = subprocess.run([sys.executable, script, *args], input="{", capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0)


class BoardWatcherTest(NotifyTestCase):
    def board(self, name="t1", status="open", handoff=None):
        d = os.path.join(self.team, "tasks", name)
        os.makedirs(os.path.join(d, "handoff"), exist_ok=True)
        with open(os.path.join(d, "brief.md"), "w", encoding="utf-8") as f:
            f.write(f"TITLE: Fix login\nSTATUS: {status}\n")
        for who, text in (handoff or {}).items():
            with open(os.path.join(d, "handoff", who + ".md"), "w", encoding="utf-8") as f:
                f.write(text)

    def test_first_scan_only_learns_then_changes_are_reported_once(self):
        self.board(status="ready", handoff={"tester": "x\nSTATUS: fail\n"})
        sent = []
        w = notify.BoardWatcher(self.team, lambda kind, msg: sent.append((kind, msg)))
        self.assertEqual(w.scan(), 0)
        self.board(status="closed")
        self.assertEqual(w.scan(), 1)
        self.assertEqual(w.scan(), 0)
        self.assertEqual(sent[0][0], "board")
        self.assertIn("closed", sent[0][1])

    def test_ready_and_failing_handoffs_are_reported_but_done_is_not(self):
        self.board()
        sent = []
        w = notify.BoardWatcher(self.team, lambda kind, msg: sent.append(msg))
        w.scan()
        self.board(status="ready", handoff={"root": "report\nSTATUS: done\n", "tester": "r\nSTATUS: issues-found\n"})
        self.assertEqual(w.scan(), 2)
        self.assertTrue(any("ready" in m for m in sent))
        self.assertTrue(any("tester" in m and "issues-found" in m for m in sent))

    def test_default_sender_honours_the_board_switch(self):
        self.board()
        w = notify.BoardWatcher(self.team)
        w.scan()
        self.switch(board=False)
        self.board(status="closed")
        w.scan()
        self.assertEqual(notify.recent(self.team), [])
        self.toast.assert_not_called()


class SettingsTest(unittest.TestCase):
    def test_defaults_are_all_on_and_saved_values_merge(self):
        self.assertEqual(settings.DEFAULTS["notify"], {k: True for k in notify.SWITCHES})
        cleaned = settings._clean({"notify": {"done": False, "bogus": False}})
        self.assertFalse(cleaned["notify"]["done"])
        self.assertTrue(cleaned["notify"]["waiting"])
        self.assertNotIn("bogus", cleaned["notify"])


class HooksInAgentFileTest(unittest.TestCase):
    def test_pm_frontmatter_has_notification_and_stop_hooks(self):
        text = init_team.notify_hooks(os.path.join(tempfile.gettempdir(), "ws"))
        self.assertIn("  Notification:", text)
        self.assertIn("  Stop:", text)
        self.assertIn("notify.py", text)
        self.assertIn(" hook ", text)
        self.assertIn(" waiting'", text)
        self.assertIn(" done'", text)


class LauncherTitleTest(unittest.TestCase):
    def test_launcher_sets_the_tab_title_and_stops_claude_overwriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            path = registry.write_launcher("Intercom", ws, os.path.join(tmp, "bin"))
            with open(path, encoding="utf-8") as f:
                text = f.read()
        self.assertIn("CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1", text)
        self.assertIn("Intercom", text.split("claude --agent pm")[0].split("CLAUDE_CODE")[0])
        self.assertLess(text.index("CLAUDE_CODE_DISABLE_TERMINAL_TITLE"), text.index("claude --agent pm"))


class StdinEncodingTest(NotifyTestCase):
    TURKISH = "Test: PM cevabını bekliyor ı ş ğ ü ö ç İ"

    def hook_bytes(self, text="ı ş ğ ü ö ç İ"):
        return json.dumps({"hook_event_name": "Notification", "message": f"Test: PM cevabını bekliyor {text}"},
                          ensure_ascii=False).encode("utf-8")

    def test_read_stdin_decodes_utf8_bytes_even_when_the_text_layer_is_cp1252(self):
        # On Windows sys.stdin is cp1252; the reader must go to the bytes and decode them as UTF-8.
        stream = io.TextIOWrapper(io.BytesIO(self.hook_bytes()), encoding="cp1252")
        self.assertEqual(json.loads(notify.read_stdin(stream))["message"], self.TURKISH)

    def test_a_utf8_bom_is_dropped(self):
        stream = io.TextIOWrapper(io.BytesIO(bytes([0xEF, 0xBB, 0xBF]) + self.hook_bytes()), encoding="cp1252")
        self.assertTrue(notify.read_stdin(stream).startswith("{"))

    def test_hook_through_main_keeps_turkish_in_the_log(self):
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(self.hook_bytes()), encoding="cp1252")):
            notify.main(["hook", self.team, "waiting"])
        self.assertEqual(notify.recent(self.team)[0]["message"], "Test: PM cevabını bekliyor ı ş ğ ü ö ç İ")

    def test_the_script_keeps_turkish_from_a_real_stdin_pipe(self):
        script = os.path.join(os.path.dirname(notify.__file__), "notify.py")
        r = subprocess.run([sys.executable, script, "hook", self.team, "waiting"], check=False,
                           input=self.hook_bytes(), capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        with open(notify.log_path(self.team), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["message"], "Test: PM cevabını bekliyor ı ş ğ ü ö ç İ")


if __name__ == "__main__":
    unittest.main()
