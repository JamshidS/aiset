"""Bounded, read-only app-server RPC; no thread/start, turn/start or trust writes."""
import json
import queue
import subprocess
import threading
import time


def hooks(executable, cwd, timeout=20):
    process = subprocess.Popen([executable, "app-server"], cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace")
    messages = queue.Queue()

    def reader():
        try:
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            messages.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def response(identifier):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex hook inspection timed out")
            item = messages.get(timeout=remaining)
            if item is None:
                raise RuntimeError("Codex app-server closed during hook inspection")
            if item.get("id") != identifier:
                continue
            if "error" in item:
                raise RuntimeError("Codex hook inspection is unavailable; update Codex CLI")
            return item.get("result") or {}

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "aiset", "version": "0.2.0"},
            "capabilities": {"experimentalApi": True}}})
        response(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}})
        return response(2)
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        thread.join(timeout=1)
        process.stdout.close()
