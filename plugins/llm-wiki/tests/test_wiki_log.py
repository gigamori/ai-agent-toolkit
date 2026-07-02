"""Tests: log append + parse (design §4, grep-parseable prefix grammar).

Covers: header format; append starts at line-begin with "## ["; parse reads
op/tag/title back; front-end dispatch helpers; tail.
"""
from llmwiki.core import wiki_log


def test_format_header():
    h = wiki_log.format_header("2026-06-26", "ingest", "source", "A Title")
    assert h == "## [2026-06-26] ingest|source | A Title"


def test_append_and_parse(tmp_path):
    log = tmp_path / "log.md"
    log.write_text("# Log\n", encoding="utf-8")
    wiki_log.append(log, "ingest", "source", "First", date="2026-06-26")
    wiki_log.append(log, "file", "cc-log", "Second", date="2026-06-27")
    entries = wiki_log.parse(log)
    assert len(entries) == 2
    assert entries[0].op == "ingest" and entries[0].tag == "source"
    assert entries[0].title == "First"
    assert entries[1].op == "file" and entries[1].tag == "cc-log"


def test_headers_start_at_line_begin(tmp_path):
    log = tmp_path / "log.md"
    log.write_text("# Log\n", encoding="utf-8")
    wiki_log.append(log, "file", "derived", "X", date="2026-06-26",
                    body="some body text")
    text = log.read_text(encoding="utf-8")
    # grep "^## \[" must find the header.
    header_lines = [ln for ln in text.splitlines() if ln.startswith("## [")]
    assert header_lines == ["## [2026-06-26] file|derived | X"]


def test_parses_template_example_spacing():
    # The template shows "file|derived  | <Title>" with extra spacing.
    entries = []
    import io
    line = "## [2026-06-26] file|derived  | Spaced Title"
    # parse() works on a file; emulate via a tmp file path through a small helper.
    # Instead validate the regex tolerance directly.
    m = wiki_log._HEADER_RE.match(line)
    assert m is not None
    assert m.group(2) == "file" and m.group(3) == "derived"
    assert m.group(4).strip() == "Spaced Title"


def test_fe_dispatch_helpers():
    assert wiki_log.header_for_fe_a() == ("file", "derived")
    assert wiki_log.header_for_fe_b() == ("ingest", "source")
    assert wiki_log.header_for_fe_b_prime() == ("file", "cc-log")


def test_tail(tmp_path):
    log = tmp_path / "log.md"
    log.write_text("# Log\n", encoding="utf-8")
    for i in range(5):
        wiki_log.append(log, "ingest", "source", f"T{i}", date="2026-06-26")
    last2 = wiki_log.tail(log, 2)
    assert [e.title for e in last2] == ["T3", "T4"]
