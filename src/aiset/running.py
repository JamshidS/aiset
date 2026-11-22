"""The offices running right now, so a second office can find the first and `aiset --list` can show them.

  <home>/running/<pid>.json   {"pid": 1234, "workspace": "C:/work/shop", "team": "shop",
                               "port": 8765, "url": "http://127.0.0.1:8765", "started": "..."}

An office writes its entry when it starts and removes it on exit. A crash leaves the file behind,
so reading skips entries whose pid is dead and deletes them. No side effects on import.
"""
import ctypes
import datetime
import glob
import json
import os

from . import registry
from .atomic import write_text_atomic

_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _dir():
    return os.path.join(registry.home(), "running")


def _key(path):
    return os.path.normcase(os.path.realpath(path))


def pid_alive(pid):
    """True if a process with this pid exists. Works on Windows, where os.kill(pid, 0) would kill it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.GetLastError() == _ERROR_ACCESS_DENIED  # exists, but not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def register(workspace, team, port):
    """Record this office. Returns the entry's path, to pass to unregister()."""
    url = f"http://127.0.0.1:{port}"
    entry = {
        "pid": os.getpid(),
        "workspace": os.path.abspath(workspace) if workspace else "",
        "team": team,
        "port": port,
        "url": url,
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(_dir(), exist_ok=True)
    path = os.path.join(_dir(), f"{os.getpid()}.json")
    write_text_atomic(path, json.dumps(entry, indent=2, ensure_ascii=False) + "\n", newline="\n")
    return path


def unregister(path):
    try:
        os.remove(path)
    except OSError:
        pass


def running():
    """The offices that are still alive, oldest first. Entries of dead processes are deleted."""
    found = []
    for path in glob.glob(os.path.join(_dir(), "*.json")):
        try:
            with open(path, encoding="utf-8-sig") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            unregister(path)
            continue
        entry["_file"] = path
        found.append(entry)
    return sorted(found, key=lambda e: str(e.get("started", "")))


def find(workspace):
    """The running office for this workspace folder (normalised compare), or None."""
    if not workspace:
        return None
    key = _key(workspace)
    for entry in running():
        if entry.get("workspace") and _key(entry["workspace"]) == key:
            return entry
    return None
