"""P1a tests for visible text and counts of hidden content."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset.run.datatypes import Visible  # noqa: E402
from aiset.run.sanitize import visible_text  # noqa: E402


class VisibleTextTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(visible_text(""), Visible("", {}))

    def test_plain_text_is_unchanged_with_no_counts(self):
        text = "Crash on empty roster.\n\t- na\u00efve, \u65e5\u672c\u8a9e, \U0001f469\u200d\U0001f4bb"
        v = visible_text(text)
        self.assertIsInstance(v, Visible)
        self.assertEqual((v.text, v.counts), (text, {}))

    def test_line_endings_are_normalized_without_counts(self):
        self.assertEqual(visible_text("\r\nfirst\t\nsecond\rthird\r"), Visible("\nfirst\t\nsecond\nthird\n", {}))

    def test_format_characters(self):
        cases = (
            ("\u200b\u200c\u200e\u200f", "<U+200B><U+200C><U+200E><U+200F>"),
            ("\u202a\u202b\u202c\u202d\u202e", "<U+202A><U+202B><U+202C><U+202D><U+202E>"),
            ("\u2060\u2061\u2062\u2063\u2064", "<U+2060><U+2061><U+2062><U+2063><U+2064>"),
            ("\u2066\u2067\u2068\u2069\ufeff\ufeff", "<U+2066><U+2067><U+2068><U+2069><U+FEFF><U+FEFF>"),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(visible_text(original), Visible(expected, {"format": len(original)}))

    def test_zwj_requires_immediate_symbol_neighbors(self):
        cases = (
            ("\u200d\U0001f469", "<U+200D>\U0001f469"),
            ("\U0001f469\u200d", "\U0001f469<U+200D>"),
            ("a\u200db", "a<U+200D>b"),
            ("\u2764\ufe0f\u200d\U0001f525", "\u2764\ufe0f<U+200D>\U0001f525"),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(visible_text(original), Visible(expected, {"format": 1}))
        symbols = "\u2603\u200d\u2602"
        self.assertEqual(visible_text(symbols), Visible(symbols, {}))

    def test_variation_selectors_ordinary_emoji_and_literal_marker(self):
        original = "\u2764\ufe0e \u2764\ufe0f \U0001f600 \u0130\u015f\u6f22 <U+200B>"
        self.assertEqual(visible_text(original), Visible(original, {}))

    def test_tag_range_includes_unassigned_code_points(self):
        original = "\U000dffff\U000e0000\U000e0001\U000e0061\U000e007f\U000e0080"
        expected = "\U000dffff<U+E0000><U+E0001><U+E0061><U+E007F>\U000e0080"
        self.assertEqual(visible_text(original), Visible(expected, {"tag": 4}))

    def test_control_and_private_use_characters(self):
        original = "\u0000\u001b\u007f\u0085\u009f\ue000\uf8ff\U000f0000\U0010fffd"
        expected = "<U+0000><U+001B><U+007F><U+0085><U+009F><U+E000><U+F8FF><U+F0000><U+10FFFD>"
        self.assertEqual(visible_text(original), Visible(expected, {"control": 5, "private": 4}))

    def test_html_comments_are_counted_and_retained(self):
        cases = (
            ("<!-- -->", 1),
            ("a<!-- first\nsecond -->b<!-- last -->", 2),
            ("a<!-- open\n<!-- nested opener", 1),
            ("<!-- outer <!-- inner -->between<!-- last -->", 2),
            ("a<!--", 1),
            ("--> <! -- > &lt;!-- --&gt;", 0),
        )
        for original, count in cases:
            with self.subTest(original=original):
                counts = {"html_comment": count} if count else {}
                self.assertEqual(visible_text(original), Visible(original, counts))

    def test_text_inside_and_outside_html_comments_is_sanitized(self):
        original = "before\u200b<!-- \r\n\u200b\U000e007f\u0001\U000f0000\r-->\t<!-- end\u0001"
        expected = "before<U+200B><!-- \n<U+200B><U+E007F><U+0001><U+F0000>\n-->\t<!-- end<U+0001>"
        counts = {"format": 2, "tag": 1, "control": 2, "private": 1, "html_comment": 2}
        self.assertEqual(visible_text(original), Visible(expected, counts))


if __name__ == "__main__":
    unittest.main()
