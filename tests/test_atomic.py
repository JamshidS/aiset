"""Atomic text writes keep the previous file when a staged write fails."""

import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from aiset import atomic  # noqa: E402


class PartialWriter:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()

    def write(self, text):
        self.stream.write(text[:4])
        raise OSError("simulated partial write failure")


class AtomicWriteTest(unittest.TestCase):
    def test_failed_partial_write_keeps_old_file_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("old settings\n")

            real_fdopen = atomic.os.fdopen

            def partial_fdopen(fd, *args, **kwargs):
                return PartialWriter(real_fdopen(fd, *args, **kwargs))

            with patch.object(atomic.os, "fdopen", side_effect=partial_fdopen):
                with self.assertRaisesRegex(OSError, "simulated partial write failure"):
                    atomic.write_text_atomic(path, "new settings\n", newline="\n")

            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "old settings\n")
            self.assertEqual(os.listdir(directory), ["settings.json"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_replacement_preserves_existing_permission_bits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("old\n")
            os.chmod(path, 0o640)

            atomic.write_text_atomic(path, "new\n", newline="\n")

            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
