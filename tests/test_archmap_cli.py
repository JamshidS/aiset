"""Command-line argument handling for the architecture map."""
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset import archmap  # noqa: E402


class OutputBuffer(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


class MissingArgumentTest(unittest.TestCase):
    def test_arch_commands_show_usage_and_return_invalid_argument_status(self):
        cases = (
            (["path", "a"], "usage: teammap arch path <from> <to>"),
            (["path"], "usage: teammap arch path <from> <to>"),
            (["impact"], "usage: teammap arch impact <service>"),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                output = OutputBuffer()
                with patch.object(archmap, "cached", return_value={"repos": [], "nodes": [], "edges": []}), patch.object(
                    archmap.sys, "stdout", output
                ):
                    status = archmap.main(argv)
                self.assertEqual(status, 2)
                self.assertEqual(output.getvalue().strip(), expected)

    def test_arch_command_without_arguments_still_prints_the_overview(self):
        output = OutputBuffer()
        with patch.object(archmap, "cached", return_value={"repos": [], "nodes": [], "edges": []}), patch.object(
            archmap.sys, "stdout", output
        ):
            status = archmap.main([])
        self.assertEqual(status, 0)
        self.assertTrue(output.getvalue())


if __name__ == "__main__":
    unittest.main()
