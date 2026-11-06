"""P7 issue reference parsing: supported forms, URL decorations and validation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset.run.datatypes import IssueRef  # noqa: E402
from aiset.run.refs import parse_issue_ref  # noqa: E402


class ParseIssueRefTest(unittest.TestCase):
    def test_bare_number_uses_the_default_repo(self):
        self.assertEqual(parse_issue_ref("58", default_repo="JamshidS/aiset"), IssueRef("JamshidS/aiset", 58))
        self.assertEqual(parse_issue_ref(" 7 "), IssueRef(None, 7))

    def test_issue_url(self):
        self.assertEqual(parse_issue_ref("https://github.com/JamshidS/aiset/issues/58"), IssueRef("JamshidS/aiset", 58))

    def test_supported_forms(self):
        for text in ("58", "#58", " 58 ", " #58 ", "0058"):
            for default in (None, "other/repo"):
                with self.subTest(text=text, default=default):
                    self.assertEqual(parse_issue_ref(text, default), IssueRef(default, 58))
        self.assertEqual(parse_issue_ref(" JamshidS/aiset#58 ", "other/repo"), IssueRef("JamshidS/aiset", 58))

    def test_url_variants(self):
        for scheme in ("", "https://"):
            for host in ("github.com", "www.github.com", "GITHUB.COM"):
                for suffix in ("", "/", "?tab=foo", "#issuecomment-1", "/?tab=foo#issuecomment-1",
                               "?next=https://example.com", "#https://example.com"):
                    text = f" {scheme}{host}/JamshidS/aiset/issues/58{suffix} "
                    with self.subTest(text=text):
                        self.assertEqual(parse_issue_ref(text, "other/repo"), IssueRef("JamshidS/aiset", 58))

    def test_repository_validation(self):
        for owner, name, valid in (("a", "b", True), ("a" * 39, "b" * 100, True),
                                   ("a" * 40, "b", False), ("a", "b" * 101, False),
                                   ("", "b", False), ("a", "", False), ("a_b", "b", False),
                                   ("a", "na me", False), ("a", "b!", False), ("é", "b", False),
                                   ("a\tb", "c", False), ("a", "b\nc", False),
                                   ("Owner-1", "Repo._-1", True)):
            for text in (f"{owner}/{name}#1", f"https://github.com/{owner}/{name}/issues/1"):
                with self.subTest(text=text):
                    if valid:
                        self.assertEqual(parse_issue_ref(text), IssueRef(f"{owner}/{name}", 1))
                    else:
                        with self.assertRaises(ValueError):
                            parse_issue_ref(text)

    def test_specific_errors(self):
        for text, message in (
                ("#0", "issue numbers start at 1"),
                ("o/r#0", "issue numbers start at 1"),
                ("https://github.com/o/r/issues/0", "issue numbers start at 1"),
                ("https://github.com/o/r/pull/45", "#45 is a pull request, not an issue"),
                ("www.github.com/o/r/pull/45/?x=y#discussion", "#45 is a pull request, not an issue"),
                ("https://gitlab.com/o/r/issues/1", "only github.com issues are supported"),
                ("gitlab.com/o/r/issues/1", "only github.com issues are supported"),
                ("https://github.com.evil/o/r/issues/1", "only github.com issues are supported")):
            with self.subTest(text=text), self.assertRaises(ValueError) as error:
                parse_issue_ref(text)
            self.assertEqual(str(error.exception), message)

    def test_malformed_references(self):
        for text in ("-1", "#-1", "o/r#-1", "abc", "", " ", "#", "o/r#abc",
                     "https://github.com/o/r/issues/-1", "https://github.com/o/r/issues/abc",
                     "https://github.com/o/r/issues/", "https://github.com/o/r/issues/1/extra",
                     "http://github.com/o/r/issues/1", "o/r#1?x=y", "https://[bad"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_issue_ref(text)

    def test_errors_are_value_errors(self):
        for bad in ("0", "abc", "", "https://github.com/JamshidS/aiset/pull/45"):
            with self.assertRaises(ValueError, msg=bad):
                parse_issue_ref(bad)


if __name__ == "__main__":
    unittest.main()
