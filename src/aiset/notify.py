#!/usr/bin/env python3
"""Notifications: the PM waits for you, the PM finished, a task board changed state.

  python notify.py hook <team-dir> <waiting|done>   a Claude Code hook; reads the hook's JSON on stdin
  python notify.py send <team-dir> <kind> <message> from a script or by hand

init_team.py writes the hook into the PM agent file. The office server (watch_boards) calls send() for
board changes. Each kind has a switch in <team>/settings.json ("notify": waiting, done, board; all on by
default): a kind that is off sends nothing. "office" decides whether the event is kept in
<team>/notifications.jsonl, which the office shows (GET /api/notifications); "desktop" decides whether an
OS notification pops up (Windows: toast through powershell, macOS: osascript, Linux: notify-send).

Standard library only, and it never raises: a hook that fails or hangs would get in the PM's way, so every
error is swallowed and the exit code is always 0.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

KINDS = ("waiting", "done", "board")
SWITCHES = KINDS + ("office", "desktop")
LABELS = {"waiting": "Needs you", "done": "Done", "board": "Task board"}
LOG_NAME = "notifications.jsonl"
KEEP = 200            # lines kept when the log is trimmed
TRIM_AT = 256 * 1024  # bytes
FAIL_STATUSES = {"fail", "failed", "issues-found"}
BRIEF_STATUSES = {"ready", "closed"}


def settings(team_dir):
    """The notify switches (all on unless settings.json turns one off)."""
    out = {k: True for k in SWITCHES}
    try:
        with open(os.path.join(team_dir, "settings.json"), encoding="utf-8-sig") as f:
            stored = json.load(f).get("notify", {})
        for k in SWITCHES:
            if isinstance(stored, dict) and k in stored:
                out[k] = bool(stored[k])
    except (OSError, ValueError, AttributeError):
        pass
    return out


def team_name(team_dir):
    try:
        with open(os.path.join(team_dir, "roster.json"), encoding="utf-8-sig") as f:
            name = str(json.load(f).get("team") or "")
            if name:
                return name
    except (OSError, ValueError, AttributeError):
        pass
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(team_dir)))) or "aiset"


def _clip(text, n=160):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def _read(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def log_path(team_dir):
    return os.path.join(team_dir, LOG_NAME)


def _append(team_dir, record):
    path = log_path(team_dir)
    os.makedirs(team_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if os.path.getsize(path) > TRIM_AT:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-KEEP:]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)


def recent(team_dir, limit=30):
    """The newest notifications first (at most limit); [] when there are none."""
    try:
        with open(log_path(team_dir), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# WinRT toast (Windows 10/11, no modules); falls back to a tray balloon. Title and text come in
# through the environment so no quoting can break the script.
TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
$t = $env:AISET_NOTIFY_TITLE; $m = $env:AISET_NOTIFY_TEXT
try {
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
  $x = New-Object Windows.Data.Xml.Dom.XmlDocument
  $x.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + [Security.SecurityElement]::Escape($t) + '</text><text>' + [Security.SecurityElement]::Escape($m) + '</text></binding></visual></toast>')
  $id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))
} catch {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $n = New-Object System.Windows.Forms.NotifyIcon
  $n.Icon = [System.Drawing.SystemIcons]::Information
  $n.Visible = $true
  $n.ShowBalloonTip(8000, $t, $m, [System.Windows.Forms.ToolTipIcon]::Info)
  Start-Sleep -Seconds 9
  $n.Dispose()
}
"""


def _applescript_text(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def desktop_toast(title, text):
    """Show an OS notification without waiting for it. Returns True when a sender was started."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if os.name == "nt":
            env = dict(os.environ, AISET_NOTIFY_TITLE=title, AISET_NOTIFY_TEXT=text)
            flags = 0x00000008 | 0x08000000 | 0x00000200  # DETACHED_PROCESS | CREATE_NO_WINDOW | NEW_PROCESS_GROUP
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                              "-ExecutionPolicy", "Bypass", "-Command", TOAST_PS],
                             env=env, creationflags=flags, close_fds=True, **quiet)
        elif sys.platform == "darwin":
            script = f'display notification "{_applescript_text(text)}" with title "{_applescript_text(title)}"'
            subprocess.Popen(["osascript", "-e", script], start_new_session=True, **quiet)
        else:
            subprocess.Popen(["notify-send", title, text], start_new_session=True, **quiet)
        return True
    except Exception:  # no powershell / osascript / notify-send: nothing to show with
        return False


def send(team_dir, kind, message, team=None):
    """Record and show one notification, as far as the switches allow. Never raises.

    Returns the record, or None when the kind is switched off (or on any error)."""
    try:
        cfg = settings(team_dir)
        if kind not in KINDS or not cfg[kind]:
            return None
        team = team or team_name(team_dir)
        text = _clip(message) or LABELS[kind]
        record = {"ts": time.time(), "at": datetime.datetime.now().isoformat(timespec="seconds"),
                  "kind": kind, "team": team, "message": text}
        if cfg["office"]:
            _append(team_dir, record)
        if cfg["desktop"]:
            desktop_toast(team, f"{LABELS[kind]}: {text}")
        return record
    except Exception:
        return None


def hook_message(kind, payload):
    """The text for a hook event from Claude Code's JSON (fields: message, last_assistant_message)."""
    if not isinstance(payload, dict):
        payload = {}
    if kind == "waiting":
        return _clip(payload.get("message")) or "The PM is waiting for you"
    return _clip(payload.get("last_assistant_message")) or "The PM finished its turn"


def run_hook(team_dir, kind, stdin_text):
    try:
        payload = json.loads(stdin_text) if stdin_text and stdin_text.strip() else {}
    except ValueError:
        payload = {}
    send(team_dir, kind, hook_message(kind, payload))


def _status(text, pattern):
    found = re.findall(pattern, text or "", re.M | re.I)
    return found[-1].strip().lower() if found else None


class BoardWatcher:
    """Notices when a task board's brief turns ready/closed or a handoff ends with a failing STATUS.

    The first scan only learns what is already there; later scans report changes."""

    def __init__(self, team_dir, send_fn=None):
        self.team_dir = team_dir
        self.tasks = os.path.join(team_dir, "tasks")
        self.send = send_fn or (lambda kind, msg: send(self.team_dir, kind, msg))
        self.seen = None  # {key: status}

    def snapshot(self):
        snap = {}
        try:
            names = sorted(os.listdir(self.tasks))
        except OSError:
            return snap
        for name in names:
            d = os.path.join(self.tasks, name)
            brief = _read(os.path.join(d, "brief.md"))
            if brief is not None:
                snap[("brief", name)] = (_status(brief, r"^STATUS:\s*([a-z-]+)") or "open",
                                         _status(brief, r"^TITLE:\s*(.+)$") or "")
            hand = os.path.join(d, "handoff")
            try:
                files = sorted(f for f in os.listdir(hand) if f.endswith(".md"))
            except OSError:
                continue
            for f in files:
                snap[("handoff", name, f)] = (_status(_read(os.path.join(hand, f)), r"^\s*STATUS:\s*([a-z-]+)"), "")
        return snap

    def scan(self):
        """Compare with the last scan and send one board notification per change. Returns the count."""
        try:
            snap = self.snapshot()
        except Exception:
            return 0
        old, self.seen = self.seen, {k: v[0] for k, v in snap.items()}
        if old is None:
            return 0
        sent = 0
        for key, (status, title) in snap.items():
            if old.get(key) == status or status is None:
                continue
            if key[0] == "brief" and status in BRIEF_STATUSES:
                self.send("board", f"{key[1]} is {status}" + (f": {title}" if title else ""))
            elif key[0] == "handoff" and status in FAIL_STATUSES:
                self.send("board", f"{key[1]}: {key[2][:-3]} reports {status}")
            else:
                continue
            sent += 1
        return sent


def watch_boards(team_dir, interval=5.0, stop=None):
    """Poll the task boards forever (run it in a daemon thread); `stop` is an optional threading.Event."""
    watcher = BoardWatcher(team_dir)
    while not (stop and stop.is_set()):
        watcher.scan()
        if stop:
            stop.wait(interval)
        else:
            time.sleep(interval)


def read_stdin(stream=None):
    """The hook's JSON from stdin, decoded as UTF-8 whatever the console code page is.

    Claude Code sends UTF-8. On Windows sys.stdin decodes with the console code page (cp1252), which garbles
    Turkish and other non-ASCII text, so the raw bytes are read from the buffer. A BOM is dropped."""
    stream = sys.stdin if stream is None else stream
    if stream is None or stream.isatty():
        return ""
    buf = getattr(stream, "buffer", None)
    data = buf.read() if buf is not None else stream.read()
    if isinstance(data, str):
        return data.lstrip("﻿")
    return data.decode("utf-8-sig", "replace")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) >= 3 and argv[0] == "hook":
            run_hook(argv[1], argv[2], read_stdin())
        elif len(argv) >= 4 and argv[0] == "send":
            send(argv[1], argv[2], " ".join(argv[3:]))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
