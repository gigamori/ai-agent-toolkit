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


vd = _load("validate_deck")

CLEAN_DECK = (FIXTURES / "deck_clean.md").read_text(encoding="utf-8")
CLEAN_REPORT = (FIXTURES / "report_clean.md").read_text(encoding="utf-8")


def run(deck_text, report_text=CLEAN_REPORT):
    deck_lines = deck_text.splitlines()
    report_lines = report_text.splitlines()
    errors = []
    fm, slides = vd.parse_deck(deck_lines, errors)
    vd.check_structure(fm, slides, report_lines, errors)
    vd.check_trace(slides, report_lines, errors)
    vd.check_coverage(slides, report_lines, errors)
    vd.check_confidence(slides, report_lines, errors)
    vd.check_numbers(slides, report_lines, errors)
    vd.check_tables(slides, report_lines, errors)
    vd.check_charts(slides, deck_lines, report_lines, errors)
    vd.check_forbidden(slides, report_lines, errors)
    return errors


def replace_once(text, old, new):
    assert text.count(old) >= 1, f"mutation anchor absent from the fixture: {old!r}"
    return text.replace(old, new, 1)


class CleanControl(unittest.TestCase):
    def test_hand_authored_deck_passes(self):
        self.assertEqual(
            run(CLEAN_DECK), [],
            "the hand-authored deck-spec is the clean control arm: a violation here is a "
            "false positive in a predicate, not a defect in the deck")


class Structure(unittest.TestCase):
    def test_missing_required_slot_fails(self):
        deck = replace_once(CLEAN_DECK, "- confidence: ○ 小規模試行を推奨できる強さ\n", "")
        self.assertTrue(any("is missing '- confidence:'" in e for e in run(deck)))

    def test_unknown_archetype_fails(self):
        deck = replace_once(CLEAN_DECK, "## S7 evidence", "## S7 exhibit")
        self.assertTrue(any("is not one of" in e for e in run(deck)))

    def test_slide_number_gap_fails(self):
        deck = replace_once(CLEAN_DECK, "## S7 evidence", "## S8 evidence")
        self.assertTrue(any("breaks the sequence" in e for e in run(deck)))

    def test_slot_foreign_to_archetype_fails(self):
        deck = replace_once(CLEAN_DECK, "## S26 appendix\ntrace: 付録/検証詳細",
                            "## S26 appendix\ntrace: 付録/検証詳細\n- headline: x")
        self.assertTrue(any("is not allowed on a appendix slide" in e for e in run(deck)))

    def test_title_not_verbatim_fails(self):
        deck = replace_once(CLEAN_DECK, "45,211案件）\"\n---", "45,211件）\"\n---")
        self.assertTrue(any("not the report title verbatim" in e for e in run(deck)))


class Trace(unittest.TestCase):
    def test_unresolvable_claim_trace_fails(self):
        deck = replace_once(CLEAN_DECK, "trace: F-12", "trace: F-99")
        self.assertTrue(any("does not resolve to a claim block" in e for e in run(deck)))

    def test_unresolvable_appendix_trace_fails(self):
        deck = replace_once(CLEAN_DECK, "trace: 付録/検証詳細", "trace: 付録/実験計画")
        self.assertTrue(any("does not resolve to a" in e for e in run(deck)))

    def test_missing_trace_fails(self):
        deck = replace_once(CLEAN_DECK, "## S22 section\ntrace: 限界と注意事項\n",
                            "## S22 section\n")
        self.assertTrue(any("is missing 'trace:'" in e for e in run(deck)))

    def test_trace_on_title_slide_fails(self):
        deck = replace_once(CLEAN_DECK, "## S1 title\n", "## S1 title\ntrace: エグゼクティブサマリ\n")
        self.assertTrue(any("must not carry 'trace:'" in e for e in run(deck)))


class Coverage(unittest.TestCase):
    def test_dropping_one_claim_fails(self):
        start = CLEAN_DECK.index("## S21 finding")
        end = CLEAN_DECK.index("## S22 section")
        deck = CLEAN_DECK[:start] + CLEAN_DECK[end:]
        errors = run(deck)
        self.assertTrue(any("claim [F-12] has no body slide" in e for e in errors))

    def test_dropping_the_limits_section_fails(self):
        start = CLEAN_DECK.index("## S22 section")
        end = CLEAN_DECK.index("## S24 appendix")
        deck = CLEAN_DECK[:start] + CLEAN_DECK[end:]
        self.assertTrue(any("section '限界と注意事項' has no body slide" in e for e in run(deck)))

    def test_dropping_a_required_limit_slot_fails(self):
        deck = replace_once(
            CLEAN_DECK,
            "- limit: 2010年は年間2,620行（全体の5.8%）しかなく",
            "- notes: 2010年は年間2,620行（全体の5.8%）しかなく")
        self.assertTrue(any("no '- limit:' slide" in e for e in run(deck)))


class Confidence(unittest.TestCase):
    def test_reworded_confidence_fails(self):
        deck = replace_once(CLEAN_DECK, "- confidence: ○ 小規模試行を推奨できる強さ",
                            "- confidence: ○ 中程度")
        self.assertTrue(any("not the report 確信度 verbatim" in e for e in run(deck)))


class NumericProvenance(unittest.TestCase):
    def test_single_digit_change_fails(self):
        deck = replace_once(CLEAN_DECK, "5.052%（n=27,729", "5.053%（n=27,729")
        self.assertTrue(any("numeric token '5.053'" in e for e in run(deck)))

    def test_unit_reformat_fails(self):
        deck = replace_once(CLEAN_DECK, "45,211案件について", "4.52万案件について")
        self.assertTrue(any("numeric token '4.52'" in e for e in run(deck)))

    def test_label_swap_passes_and_is_a_blind_spot(self):
        deck = replace_once(
            CLEAN_DECK,
            "前回成功 64.725%（n=1,511、申込978件）vs 失敗 12.610%（n=4,901、618件）",
            "前回失敗 64.725%（n=1,511、申込978件）vs 成功 12.610%（n=4,901、618件）")
        self.assertEqual(
            run(deck), [],
            "a correct number under a swapped label is invisible to every deterministic "
            "predicate here; QUALIFIER_DROP / UNTRACEABLE in the LLM judge is the only "
            "thing that sees it")

    def test_structure_lines_are_not_numeric_sources(self):
        self.assertEqual(
            run(CLEAN_DECK), [],
            "slide numbers in '## S<n>' and claim ids in 'trace: F-n' are structure, not "
            "quantities: counting them would make every deck fail")


class SourceTables(unittest.TestCase):
    def test_deleting_one_row_fails(self):
        deck = replace_once(CLEAN_DECK, "| 2009年 | 14,862 | 2,536 | 17.064 |\n", "")
        self.assertTrue(any("differs from the report table row for row" in e for e in run(deck)))

    def test_changing_one_cell_fails(self):
        deck = replace_once(CLEAN_DECK, "| 2008年 | 27,729 | 1,401 | 5.052 |",
                            "| 2008年 | 27,729 | 1,402 | 5.052 |")
        self.assertTrue(any("differs from the report table row for row" in e for e in run(deck)))

    def test_dropping_a_column_under_a_chart_fails(self):
        deck = replace_once(CLEAN_DECK, "| 期間 | 案件数 | 申込件数 | 申込率 |",
                            "| 期間 | 案件数 | 申込率 |")
        self.assertTrue(
            any("is not a source table" in e for e in run(deck)),
            "a column-dropped source table stops matching any report header, so it is caught "
            "as an authored table under a chart annotation; a source table with no chart "
            "above it would degrade to an authored table silently")


class AuthoredTables(unittest.TestCase):
    def test_number_change_to_an_absent_token_fails(self):
        deck = replace_once(CLEAN_DECK, "45,211行 × 17列", "45,211行 × 170列")
        self.assertTrue(any("numeric token '170'" in e for e in run(deck)))

    def test_number_change_to_a_token_used_elsewhere_passes(self):
        deck = replace_once(CLEAN_DECK, "45,211行 × 17列", "45,211行 × 18列")
        self.assertEqual(
            run(deck), [],
            "18 occurs elsewhere in the report, so token-set membership admits it here: the "
            "predicate checks provenance of the digits, never that they belong to this quantity")

    def test_row_recombination_passes_and_is_a_blind_spot(self):
        deck = replace_once(
            CLEAN_DECK,
            "| 目的変数 | 定期預金の申込。陽性 5,289件 / 45,211件 = 11.70%（不均衡 7.55:1） |",
            "| 目的変数 | 定期預金の申込。陽性 5,289件 / 45,211件 = 16.05%（不均衡 7.55:1） |")
        self.assertEqual(
            run(deck), [],
            "16.05% is a real report number attached to the wrong quantity: token-set "
            "membership cannot see a recombination across rows")


class Charts(unittest.TestCase):
    def test_axis_swap_fails(self):
        deck = replace_once(CLEAN_DECK, "chart: bar x=期間 y=申込率", "chart: bar x=申込率 y=期間")
        self.assertTrue(any("chart annotation does not occur in the report" in e
                            for e in run(deck)))

    def test_invented_chart_fails(self):
        deck = replace_once(
            CLEAN_DECK,
            "<!-- chart: bar x=期間 y=申込率 title=\"年別の申込率（分母=当年の案件）\" -->",
            "<!-- chart: pie x=期間 y=申込率 title=\"年別の申込率（分母=当年の案件）\" -->")
        self.assertTrue(any("chart annotation does not occur in the report" in e
                            for e in run(deck)))

    def test_chart_without_a_source_table_fails(self):
        deck = replace_once(
            CLEAN_DECK,
            "| 期間 | 案件数 | 申込件数 | 申込率 |\n|---|---|---|---|\n"
            "| 2008年 | 27,729 | 1,401 | 5.052 |",
            "| 期間 | 申込率 |\n|---|---|\n| 2008年 | 5.052 |")
        self.assertTrue(any("is not a source table" in e for e in run(deck)))


class ForbiddenWording(unittest.TestCase):
    def test_action_verb_under_findings_fails(self):
        deck = replace_once(CLEAN_DECK, "- headline: 申込率の変動は接触時期に支配され",
                            "- headline: 接触時期を軸に配分を見直すべき。申込率の変動は接触時期に支配され")
        self.assertTrue(any("action-oriented wording" in e for e in run(deck)))


if __name__ == "__main__":
    unittest.main()
