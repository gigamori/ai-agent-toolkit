import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpt = _load("check_pptx_text")

DECK_LINES = (FIXTURES / "deck_clean.md").read_text(encoding="utf-8").splitlines()
CLEAN_VIEW = (FIXTURES / "view_text_clean.txt").read_text(encoding="utf-8")


def replace_once(text, old, new):
    assert text.count(old) >= 1, f"mutation anchor absent from the fixture: {old!r}"
    return text.replace(old, new, 1)


class CleanControl(unittest.TestCase):
    def test_faithful_render_passes(self):
        self.assertEqual(
            cpt.check(CLEAN_VIEW, DECK_LINES), [],
            "a violation on the clean render is a false positive in the allowed-string set, "
            "not a defect in the rendering")

    def test_every_slide_block_is_parsed(self):
        slides = {slide for slide, _ in cpt.rendered_lines(CLEAN_VIEW)}
        self.assertEqual(
            slides, {1, 2, 3, 6, 7},
            "a zero or partial slide count would make every containment check vacuous")


class WordingDrift(unittest.TestCase):
    def test_one_character_change_fails(self):
        view = replace_once(CLEAN_VIEW, "+46.551ポイント動き", "+46.552ポイント動き")
        self.assertEqual(len(cpt.check(view, DECK_LINES)), 1)

    def test_dropped_qualifier_fails(self):
        view = replace_once(CLEAN_VIEW, "（n=27,729、分母=当年の案件）", "（n=27,729）")
        self.assertEqual(len(cpt.check(view, DECK_LINES)), 1)

    def test_invented_sentence_fails(self):
        view = replace_once(CLEAN_VIEW, "主要な発見\n", "主要な発見\n接触時期を軸に配分を見直す。\n")
        self.assertTrue(any("接触時期を軸に配分を見直す。" in v
                            for v in cpt.check(view, DECK_LINES)))

    def test_table_cell_change_fails(self):
        view = replace_once(CLEAN_VIEW, "\n27,729\n", "\n27,728\n")
        self.assertEqual(len(cpt.check(view, DECK_LINES)), 1)


class AllowedExtras(unittest.TestCase):
    def test_layout_labels_are_allowed(self):
        for label in cpt.LAYOUT_LABELS:
            view = f"=== /slide[2] ===\n{label}\n"
            self.assertEqual(cpt.check(view, DECK_LINES), [])

    def test_slide_number_is_allowed_only_on_its_own_slide(self):
        self.assertEqual(cpt.check("=== /slide[4] ===\n4\n", DECK_LINES), [])
        self.assertEqual(len(cpt.check("=== /slide[4] ===\n9999\n", DECK_LINES)), 1)

    def test_trace_with_a_label_is_allowed(self):
        self.assertEqual(cpt.check("=== /slide[9] ===\nF-2 根拠\n", DECK_LINES), [])

    def test_chart_title_is_allowed(self):
        view = "=== /slide[9] ===\n前回キャンペーン結果別の申込率（分母=前回接触ありの案件）\n"
        self.assertEqual(
            cpt.check(view, DECK_LINES), [],
            "officecli renders a chart title as slide text, so the title= value of a chart "
            "annotation has to be in the allowed set")

    def test_a_word_outside_the_label_set_fails(self):
        self.assertEqual(len(cpt.check("=== /slide[2] ===\n参考\n", DECK_LINES)), 1)


class OutputShape(unittest.TestCase):
    def test_slide_marker_shape_is_pinned(self):
        self.assertEqual(
            cpt.rendered_lines("=== /slide[12] ===\nx\n"), [(12, "x")],
            "officecli view text frames each slide with this exact marker; a shape change "
            "makes every line fall on slide 0 instead of failing loudly")

    def test_text_before_any_marker_is_attributed_to_slide_zero(self):
        self.assertEqual(cpt.rendered_lines("stray\n"), [(0, "stray")])


if __name__ == "__main__":
    unittest.main()
