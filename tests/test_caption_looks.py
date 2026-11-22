import importlib.util
import sys
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
sys.path.insert(0, str(HELPERS))
import caption_looks as cl  # noqa: E402


class CaptionLookTests(unittest.TestCase):
    def test_every_look_builds(self):
        words = [("So", 0.0, 0.2, "speaker_0"), ("the", 0.2, 0.4, "speaker_0"), ("revenue", 0.4, 0.9, "speaker_0"),
                 ("grew", 0.9, 1.2, "speaker_0"), ("40%", 1.2, 1.6, "speaker_0"), ("yeah", 1.7, 2.0, "speaker_1")]
        for name in cl.LOOKS:
            look = cl.resolve_look({"name": name, "accent": "#FF5A1F", "speakers": ["#F2EEE2", "#FFC7A8"]})
            pages = [{"a": p[0][1], "b": p[-1][2], "words": p} for p in cl.page_words(words, look)]
            ass = cl.build_ass(pages, look, 1080, 1920)
            self.assertIn("Dialogue:", ass, name)
            self.assertIn("&H001F5AFF", ass, name)  # brand accent, ASS BGR

    def test_pages_break_on_speaker_change(self):
        look = cl.resolve_look("pop")
        words = [("a", 0, .1, "s0"), ("b", .1, .2, "s1")]
        self.assertEqual(len(cl.page_words(words, look)), 2)

    def test_emphasis(self):
        self.assertEqual(cl._pick_emphasis(["we", "grew", "40%"]), 2)
        self.assertEqual(cl._pick_emphasis(["really", "*want*", "these"]), 1)
        self.assertEqual(cl._pick_emphasis(["i", "think", "would"]), -1)

    def test_text_cannot_inject_ass_tags(self):
        look = cl.resolve_look("cards")
        ass = cl.build_ass([{"a": 0, "b": 1, "words": cl.text_to_words(r"hi {\pos(0,0)\fs400} there", 0, 1)}], look, 1080, 1920)
        self.assertNotIn(r"\fs400", ass)

    def test_unknown_look_is_plain(self):
        self.assertIsNone(cl.resolve_look("sparkles"))


if __name__ == "__main__":
    unittest.main()
