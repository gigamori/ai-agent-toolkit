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

REPORT = (FIXTURES / "report_clean.md").read_text(encoding="utf-8").splitlines()
BASELINE_VIEW = (FIXTURES / "view_text_baseline.txt").read_text(encoding="utf-8")
CHART_JSON = (FIXTURES / "query_chart_probe.json").read_text(encoding="utf-8")
EMPTY_CHART_JSON = '{"success": true, "data": {"matches": 0, "results": []}}'

CIRCLE = "○"
CROSS = "×"
ALL_IDS = [str(n) for n in range(1, 13)]

CLEAN_VIEW = f"""=== /slide[1] ===
定期預金キャンペーン 申込率の要因分析
対象期間 2008-05-05 〜 2010-11-17 ／ 45,211案件
1
=== /slide[2] ===
F-1 ／ 確信度 {CIRCLE}
2008年 5.052%（n=27,729）→ 2010年 51.603%（n=2,620）
2
=== /slide[3] ===
F-2 ／ 確信度 {CIRCLE}
前回成功 64.725%（n=1,511）vs 前回失敗 12.610%（n=4,901）
3
=== /slide[4] ===
F-3 ／ 確信度 {CIRCLE}
4
=== /slide[5] ===
F-4 ／ 確信度 {CIRCLE}
5
=== /slide[6] ===
F-5 ／ 確信度 {CIRCLE}
6
=== /slide[7] ===
F-6 ／ 確信度 {CIRCLE}
7
=== /slide[8] ===
F-7 ／ 確信度 {CIRCLE}
1回目 5.665%（n=45,211）
8
=== /slide[9] ===
F-8 ／ 確信度 {CIRCLE}
9
=== /slide[10] ===
F-9 ／ 確信度 {CROSS}
10
=== /slide[11] ===
F-10・F-11・F-12 ／ 棄却 ／ 確信度 {CROSS}
11
"""

SCOPED_VIEW = f"=== /slide[1] ===\nF-6 ／ 確信度 {CIRCLE}\nF-7 ／ 確信度 {CIRCLE}\n1\n"


def replace_once(text, old, new):
    assert text.count(old) >= 1, f"mutation anchor absent from the fixture: {old!r}"
    return text.replace(old, new, 1)


def listed_slides(listing):
    return {int(line.split(":")[0].split()[1]) for line in listing}


class CleanControl(unittest.TestCase):
    def test_synthetic_deck_passes(self):
        self.assertEqual(cpt.check(CLEAN_VIEW, REPORT), [])

    def test_synthetic_deck_lists_no_numbers(self):
        self.assertEqual(cpt.unknown_numbers(CLEAN_VIEW, REPORT), [])

    def test_synthetic_deck_has_every_slide_parsed(self):
        slides = {slide for slide, _ in cpt.rendered_lines(CLEAN_VIEW)}
        self.assertEqual(
            slides, set(range(1, 12)),
            "a zero or partial slide count would make every check vacuous")

    def test_report_marks_are_read_from_the_confidence_line(self):
        marks = cpt.claim_marks(REPORT)
        self.assertEqual(
            marks, {**{c: CIRCLE for c in ALL_IDS[:8]}, **{c: CROSS for c in ALL_IDS[8:]}},
            "the mark is the first character after '- 確信度:'; U+25CB is the "
            "circle the contract uses and must not be normalized to U+3007 or O")

    def test_baseline_capture_passes_and_lists_its_section_numbers(self):
        slides = {slide for slide, _ in cpt.rendered_lines(BASELINE_VIEW)}
        self.assertEqual(len(slides), 24)
        self.assertEqual(cpt.check(BASELINE_VIEW, REPORT), [],
                         "every F-1..F-12 chip sits on a slide with its mark in the capture")
        listing = cpt.unknown_numbers(BASELINE_VIEW, REPORT)
        self.assertEqual(len(listing), 13)
        self.assertEqual(
            listed_slides(listing), {3, 5, 7, 11, 15, 19},
            "the capture carries divider numbers 01-04 and a derived 88.30% the report "
            "never states; they are listed for the judge, not judged here, and the "
            "fixture is a real capture that is not edited to shrink the listing")


class NumericListing(unittest.TestCase):
    def test_one_digit_change_is_listed_naming_the_token(self):
        view = replace_once(CLEAN_VIEW, "5.052%", "5.053%")
        self.assertEqual(
            cpt.unknown_numbers(view, REPORT),
            ["slide 2: '5.053': 2008年 5.053%（n=27,729）→ 2010年 51.603%（n=2,620）"])
        self.assertEqual(cpt.check(view, REPORT), [],
                         "a number the report never states is listed, never a violation")

    def test_number_invented_on_a_label_line_is_listed(self):
        view = replace_once(CLEAN_VIEW, "=== /slide[4] ===\n", "=== /slide[4] ===\n上位97531件\n")
        self.assertEqual(cpt.unknown_numbers(view, REPORT), ["slide 4: '97531': 上位97531件"])
        self.assertEqual(cpt.check(view, REPORT), [])

    def test_own_slide_number_is_not_listed_but_another_number_alone_is(self):
        self.assertEqual(cpt.unknown_numbers("=== /slide[4] ===\n4\n", REPORT), [])
        self.assertEqual(
            cpt.unknown_numbers("=== /slide[4] ===\n9999\n", REPORT),
            ["slide 4: '9999': 9999"],
            "only the slide's own page number is left out of the listing")

    def test_commas_and_trailing_dots_are_stripped_on_both_sides(self):
        self.assertEqual(cpt.numeric_tokens("45,211件。 5.052% 2008."),
                         {"45211", "5.052", "2008"})


class ClaimCoverage(unittest.TestCase):
    def test_chip_removed_from_every_slide_fails_naming_it(self):
        view = replace_once(CLEAN_VIEW, f"F-4 ／ 確信度 {CIRCLE}\n", "")
        self.assertEqual(cpt.check(view, REPORT), ["F-4: not on any slide"])

    def test_f1_is_not_satisfied_by_f12(self):
        view = f"=== /slide[1] ===\nF-12 ／ 確信度 {CROSS}\n1\n"
        self.assertEqual(
            cpt.check(view, REPORT, scope=["1"]), ["F-1: not on any slide"],
            "F-1 must match on a digit boundary; a substring match would let "
            "F-12 stand in for F-1")
        self.assertEqual(cpt.chip_ids("F-12 F-1・F-10 F-100"), {"12", "1", "10", "100"})


class ConfidenceMark(unittest.TestCase):
    def test_mark_changed_on_the_only_slide_with_the_chip_fails(self):
        view = replace_once(CLEAN_VIEW, f"F-4 ／ 確信度 {CIRCLE}", "F-4 ／ 確信度 △")
        self.assertEqual(
            cpt.check(view, REPORT),
            [f"F-4: no slide carries both the chip and its confidence mark '{CIRCLE}'"],
            "the expected mark in the message is the report's, quoted as U+25CB")

    def test_chip_and_mark_on_different_slides_fails(self):
        view = replace_once(CLEAN_VIEW, f"F-4 ／ 確信度 {CIRCLE}\n5\n",
                            f"F-4\n5\n=== /slide[12] ===\n{CIRCLE}\n12\n")
        self.assertEqual(
            cpt.check(view, REPORT),
            [f"F-4: no slide carries both the chip and its confidence mark '{CIRCLE}'"],
            "a chip without its mark is not a violation by itself; only the absence "
            "of any slide carrying both is")

    def test_report_with_a_lookalike_mark_is_rejected(self):
        report = [line.replace(f"- 確信度: {CIRCLE}", "- 確信度: 〇", 1) for line in REPORT]
        with self.assertRaises(cpt.UsageError):
            cpt.check(CLEAN_VIEW, report)


class Scope(unittest.TestCase):
    def test_scoped_run_passes_on_a_deck_carrying_only_those_claims(self):
        self.assertEqual(cpt.check(SCOPED_VIEW, REPORT, scope=["6", "7"]), [])

    def test_unscoped_run_on_the_same_deck_fails_the_other_ten(self):
        violations = cpt.check(SCOPED_VIEW, REPORT)
        self.assertEqual(
            violations,
            [f"F-{c}: not on any slide" for c in ALL_IDS if c not in ("6", "7")])

    def test_scope_id_absent_from_the_report_is_a_usage_error(self):
        with self.assertRaises(cpt.UsageError):
            cpt.check(CLEAN_VIEW, REPORT, scope=["99"])

    def test_scope_flag_parses_comma_separated_chips_only(self):
        self.assertEqual(cpt.parse_scope("F-6,F-7"), ["6", "7"])
        self.assertEqual(cpt.parse_args(["a.pptx", "r.md", "--scope", "F-6"])[2], ["6"])
        with self.assertRaises(cpt.UsageError):
            cpt.parse_scope("6")
        with self.assertRaises(cpt.UsageError):
            cpt.parse_args(["a.pptx", "r.md", "--scope"])


class ChartData(unittest.TestCase):
    def test_chart_query_shape_is_pinned(self):
        self.assertEqual(
            cpt.chart_lines(CHART_JSON),
            [(1, "chart title: probe chart"),
             (1, "chart categories: 1回目 / 2回目 / 3回目"),
             (1, "chart series: Series1"),
             (1, "chart values: 5.665 / 5.064 / 4.076")],
            "officecli query <pptx> chart --json joins categories and values with "
            "commas; they are split so 5.665,5.064 is not read as one number")

    def test_deck_without_charts_yields_no_chart_lines(self):
        self.assertEqual(cpt.chart_lines(EMPTY_CHART_JSON), [])

    def test_chart_values_from_the_report_are_not_listed(self):
        self.assertEqual(cpt.unknown_numbers(CLEAN_VIEW, REPORT, chart_json=CHART_JSON), [])
        self.assertEqual(cpt.check(CLEAN_VIEW, REPORT, chart_json=CHART_JSON), [])

    def test_chart_value_not_in_the_report_is_listed_with_its_chart_label(self):
        chart = CHART_JSON.replace("5.665,5.064,4.076", "5.666,5.064,4.076")
        self.assertEqual(
            cpt.unknown_numbers(CLEAN_VIEW, REPORT, chart_json=chart),
            ["slide 1: '5.666': chart values: 5.666 / 5.064 / 4.076"],
            "view text never shows native chart data, so this is the only path "
            "by which a chart number reaches the listing")
        self.assertEqual(cpt.check(CLEAN_VIEW, REPORT, chart_json=chart), [])


class OutputShape(unittest.TestCase):
    def test_slide_marker_shape_is_pinned(self):
        self.assertEqual(
            cpt.rendered_lines("=== /slide[12] ===\nx\n"), [(12, "x")],
            "officecli view text frames each slide with this exact marker; a shape "
            "change makes every line fall on slide 0 instead of failing loudly")

    def test_text_before_any_marker_is_attributed_to_slide_zero(self):
        self.assertEqual(cpt.rendered_lines("stray\n"), [(0, "stray")])


if __name__ == "__main__":
    unittest.main()
