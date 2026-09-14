#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import notes_push as np  # noqa: E402

PASS = 0
FAIL = 0
SID = "notespush1-2222-3333-4444-555566667777"

ROWS = (
    "| File | Description | Tags | Updated |\n"
    "|---|---|---|---|\n"
    "| specs/n1.md | capture hook design | capture, hook | 2026-01-03 |\n"
    "| specs/n2.md | ledger writer notes | ledger, writer | 2026-01-02 |\n"
    "| specs/n3.md | kanban port derivation | kanban, port | 2026-01-01 |\n"
    "| specs/n4.md | capture ledger design 捕捉 の 設計 memo | capture, ledger | 2026-01-04 |\n"
    "| specs/n5.md | unrelated wiki\\|toggle row | wiki | 2026-01-05 |\n"
    "| _archive/old.md | archived capture note | capture | 2025-12-01 |\n"
)


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def make_ws(root: Path) -> tuple[Path, Path, Path]:
    ws = root / "ws"
    project = ws / "_projects" / "proj"
    notes = project / "project-notes"
    write(ws / "src" / "a.py", "def main():\n    pass\n")
    write(ws / "src" / "b.py", "x = 1\n")
    write(ws / "other" / "c.py", "y = 2\n")
    write(ws / "tmp" / "scratch.py", "z = 3\n")
    write(ws / "_projects" / "proj" / "index.md", "# proj\n")
    (ws / "_projects" / "_state").mkdir(parents=True)
    write(notes / "index.md", ROWS)
    write(notes / "specs" / "n1.md", "---\nsummary: |-\n  s\nanchors:\n  - \"src/a.py::main\"\n---\n\n# n1\n")
    write(notes / "specs" / "n2.md", "---\r\nanchors: [src/b.py]\r\n---\r\n\r\n# n2\r\n")
    write(notes / "specs" / "n3.md", "---\nanchors: ['other/c.py', 'other\\\\c.py::run']\n---\n\n# n3\n")
    write(notes / "specs" / "n4.md", "# n4 without frontmatter\n")
    write(notes / "specs" / "n5.md", "---\ntags: [wiki]\n---\n\n# n5\n")
    write(notes / "_archive" / "old.md", "---\nanchors: [src/a.py]\n---\n\n# old\n")
    return ws, project, notes


def rows_of(notes: Path) -> list[dict]:
    return np.active_rows(np.parse_index_rows((notes / "index.md").read_text(encoding="utf-8")))


def test_subject_text() -> None:
    print("--- derive_subject_text ---")
    check(np.derive_subject_text("pj:x /progress -y 続き norouter --deep") == "続き",
          "control slug, slash command, flags and norouter are dropped")
    check(np.derive_subject_text("/c/home/x.py stays and -3 stays") == "/c/home/x.py stays and -3 stays",
          "a multi-segment path and a numeric dash token stay")
    check(np.derive_subject_text("pj:x -y") == "", "an all-mechanism prompt yields an empty subject")


def test_prompt_paths(root: Path) -> None:
    print("--- prompt_paths ---")
    ws, _, _ = make_ws(root)
    text = (f"see src/a.py:12 and {ws.as_posix()}/src/b.py plus tmp/scratch.py, "
            "_projects/proj/index.md and src/missing.py")
    got = np.prompt_paths(text, str(ws))
    check(got == ["src/a.py", "src/b.py"],
          f"existing repo paths kept, :line dropped, tmp/ _projects/ and missing paths excluded (got {got})")


def test_anchors_parser() -> None:
    print("--- parse_anchors_block / read_anchors ---")
    check(np.parse_anchors_block("anchors: [a/b.py, \"c/d.py::f\"]") == ["a/b.py", "c/d.py::f"],
          "flow list with mixed quoting")
    check(np.parse_anchors_block("x: 1\nanchors:\n  - \"a/b.py\"\n  - 'c\\d.py'\ntags: [t]") == ["a/b.py", "c/d.py"],
          "block list, quotes stripped, backslash normalised, stops at the next key")
    check(np.parse_anchors_block("summary: |-\n  anchors: not this\ntags: [t]") == [],
          "an indented anchors: inside a block scalar is not the key")


def test_location(root: Path) -> None:
    print("--- location_matches ---")
    ws, _, notes = make_ws(root)
    rows = rows_of(notes)
    check([r["file"] for r in rows] == ["specs/n1.md", "specs/n2.md", "specs/n3.md", "specs/n4.md", "specs/n5.md"],
          "active rows exclude _archive/ and keep index order")
    l1, l2 = np.location_matches(rows, str(notes), ["src/a.py"])
    check([r["file"] for r in l1] == ["specs/n1.md"], f"L1 = exact anchor path match (got {[r['file'] for r in l1]})")
    check([r["file"] for r in l2] == ["specs/n2.md"], f"L2 = same leaf directory (got {[r['file'] for r in l2]})")
    l1b, l2b = np.location_matches(rows, str(notes), ["Other/C.py"])
    check([r["file"] for r in l1b] == ["specs/n3.md"] and l2b == [],
          "path comparison is case-insensitive and a CRLF/quoted anchors block parses")


def test_ledger_tail(root: Path) -> None:
    print("--- turn_paths with ledgers ---")
    ws, _, _ = make_ws(root)
    state = ws / "_projects" / "_state"
    write(state / f"{SID}.reads", "\n".join(f"src/r{i}.py" for i in range(50)) + "\nsrc/b.py\n")
    write(state / f"{SID}.touched", "_projects/proj/tasks/1_in_progress/t.md\nother/c.py\ntorn")
    got = np.turn_paths("pj:x 続き", str(ws), str(state), SID)
    check("src/b.py" in got and "other/c.py" in got, f"both ledgers feed the turn paths (got {got})")
    check("src/r0.py" not in got and len([g for g in got if g.startswith("src/r")]) == np.LEDGER_TAIL - 1,
          "only the ledger tail is used")
    check(not any(g.startswith("_projects/") for g in got), "_projects/ lines are not location signals")


def test_lexical(root: Path) -> None:
    print("--- rank_lexical ---")
    ws, _, notes = make_ws(root)
    rows = rows_of(notes)
    ranked = np.rank_lexical(rows, "writer capture")
    check([r["file"] for r in ranked] == ["specs/n2.md"],
          f"'capture' (2 of 5 rows, above the 30% cut) scores nothing; 'writer' text + tag rank n2 alone (got {[r['file'] for r in ranked]})")
    ranked2 = np.rank_lexical(rows, "kanban writer")
    check([r["file"] for r in ranked2] == ["specs/n2.md", "specs/n3.md"],
          f"equal scores order by Updated desc (got {[r['file'] for r in ranked2]})")
    ranked_cjk = np.rank_lexical(rows, "捕捉の設計")
    check([r["file"] for r in ranked_cjk] == ["specs/n4.md"],
          f"a CJK run falls back to bigram hits when the whole run is absent (got {[r['file'] for r in ranked_cjk]})")
    check(np.rank_lexical(rows, "the and for") == [], "stopwords alone rank nothing")
    check(rows[4]["description"] == "unrelated wiki|toggle row", "an escaped pipe stays inside one Description cell")


def test_query_doc() -> None:
    print("--- build_query_doc / sanitize ---")
    doc = np.build_query_doc('claude -p で "run" -2 の設計 mode-orchestrator')
    lines = doc.split("\n")
    check(lines[0] == "vec: claude p で run 2 の設計 mode-orchestrator",
          f"vec line drops quotes and dashes after whitespace (got {lines[0]!r})")
    check(lines[1:] == ["lex: claude", "lex: mode-orchestrator", "lex: 設計"],
          f"one lex line per term: ASCII words minus stopwords, CJK split on hiragana (got {lines[1:]})")


def test_timeout_env() -> None:
    print("--- resolve_qmd_timeout ---")
    check(np.resolve_qmd_timeout({}) == 6.0, f"default budget is 6 s (got {np.resolve_qmd_timeout({})})")
    check(np.resolve_qmd_timeout({"TASKFLOW_QMD_TIMEOUT": "2.5"}) == 2.5, "TASKFLOW_QMD_TIMEOUT overrides the budget")
    check(np.resolve_qmd_timeout({"TASKFLOW_QMD_TIMEOUT": "abc"}) == 6.0, "a non-numeric value falls back to the default")
    check(np.resolve_qmd_timeout({"TASKFLOW_QMD_TIMEOUT": "0"}) == 6.0, "a non-positive value falls back to the default")
    seen = {}

    def runner(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    try:
        np.run_qmd("BIN", "NOTES", "vec: x", 5, timeout=np.resolve_qmd_timeout({"TASKFLOW_QMD_TIMEOUT": "9"}), runner=runner)
    except subprocess.TimeoutExpired:
        pass
    check(seen.get("timeout") == 9.0, f"the resolved budget reaches the spawn call (got {seen.get('timeout')})")


def test_qmd_rank() -> None:
    print("--- qmd_rank / run_qmd ---")
    results = [
        {"file": "qmd://project-notes/specs/n2.md", "score": 1, "explain": {"ftsScores": [1.2]}},
        {"file": "qmd://project-notes/specs/n2.md", "score": 0.5},
        {"file": "qmd://project-notes/specs/n1.md", "score": 0.3,
         "explain": {"ftsScores": [], "vectorScores": {"v": 0.1}}},
        {"file": "qmd://project-notes/specs/n3.md", "score": 0.2,
         "explain": {"ftsScores": [], "vectorScores": [0.4]}},
    ]
    order, hits = np.qmd_rank(results)
    check(order == ["specs/n2.md", "specs/n1.md", "specs/n3.md"], f"files deduped in rank order, uri prefix stripped (got {order})")
    check(hits == {"specs/n2.md", "specs/n3.md"},
          f"a hit needs a BM25 score or a vector score >= VECTOR_TAU; a result with neither key counts (got {hits})")
    check("specs/n1.md" in order and "specs/n1.md" not in hits,
          "a weak-vector-only result still holds its rank and only drops out of M")

    flat = [{"file": "qmd://project-notes/specs/n1.md", "score": 0.3,
             "ftsScores": [], "vectorScores": {"v": 0.1}}]
    check(np.qmd_rank(flat)[1] == {"specs/n1.md"},
          "scores at the TOP level are not the shape qmd emits, so the predicate must not find them there; reading the top level is what made VECTOR_TAU dead once")

    def runner_ok(cmd, **kw):
        check(cmd[0] == "node" and "--no-rerank" in cmd and "--json" in cmd and kw.get("cwd") == "NOTES",
              "qmd is spawned as node <bin> ... --no-rerank --json in the notes dir")
        return subprocess.CompletedProcess(cmd, 0, stdout='Searching...\n[{"file": "qmd://c/specs/n1.md"}]\n', stderr="")

    got = np.run_qmd("BIN", "NOTES", "vec: x", 5, runner=runner_ok)
    check(got == [{"file": "qmd://c/specs/n1.md"}], "JSON array is extracted after progress lines")

    def runner_fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Negation")

    try:
        np.run_qmd("BIN", "NOTES", "vec: x", 5, runner=runner_fail)
        bad("non-zero exit must raise")
    except np.QmdError as e:
        check("exit 1" in str(e), "non-zero exit raises QmdError with the code")


def test_union_and_compose(root: Path) -> None:
    print("--- union_interleave / compose / render ---")
    ws, _, notes = make_ws(root)
    rows = rows_of(notes)
    ui = np.union_interleave(["specs/n1.md", "specs/n2.md"], ["specs/n2.md", "specs/n3.md"])
    check(ui == ["specs/n1.md", "specs/n3.md", "specs/n2.md"],
          f"union_interleave alternates and drops the duplicate from list b (got {ui})")
    check(np.union_interleave([], ["specs/n3.md"]) == ["specs/n3.md"],
          "union_interleave passes a single list through")
    check(np.union_interleave(["specs/n1.md"], []) == ["specs/n1.md"],
          "union_interleave passes the other single list through")
    l1 = [rows[0]]
    l2 = [rows[1], rows[2], rows[3], rows[4], rows[0]]
    shown, matched = np.compose(rows, l1, l2, ["specs/n5.md"], {"specs/n5.md"}, k=4)
    check([r["file"] for r in shown] == ["specs/n1.md", "specs/n2.md", "specs/n3.md", "specs/n4.md"],
          f"L1 first, L2 capped at ceil(K/2)... within K, no duplicates (got {[r['file'] for r in shown]})")
    check(matched == 5, f"M is L1 ∪ L2 ∪ BM25 hit ∪ vector ≥ VECTOR_TAU, uncapped (got {matched})")
    text = np.render(shown, matched, len(rows))
    check(text.startswith(np.HEADER + "\n| specs/n1.md |"), "block starts with the header and verbatim rows")
    check(text.endswith("project-notes matched: 5 of 5 rows (4 shown)"), f"footer carries M, N and K shown (got {text.splitlines()[-1]!r})")


def test_contract_literals() -> None:
    print("--- boundary contract literals ---")
    check(np.K_ROWS == 8,
          f"K is 8 in the notes data contract; changing it is a consumer-propagating change (got {np.K_ROWS})")
    check(np.L2_CAP == 4,
          f"L2 cap is 4 in the notes data contract; changing it is a consumer-propagating change (got {np.L2_CAP})")
    check(np.HEADER == "project-notes (index rows):",
          f"consumers transcribe this header literal; changing it is a consumer-propagating change (got {np.HEADER!r})")
    footer = np.render([], 0, 0).splitlines()[-1]
    check(footer == "project-notes matched: 0 of 0 rows (0 shown)",
          f"consumers transcribe this footer literal; changing it is a consumer-propagating change (got {footer!r})")


def test_build_end_to_end(root: Path) -> None:
    print("--- build_notes_push ---")
    ws, project, notes = make_ws(root)
    state = ws / "_projects" / "_state"
    write(state / f"{SID}.reads", "src/a.py\n")
    fake_bin = ws / "fakeqmd"
    write(fake_bin, "")
    env_auto = {"TASKFLOW_QMD_BIN": str(fake_bin), "TASKFLOW_NOTES_PUSH": "auto"}

    def runner(cmd, **kw):
        check(cmd[cmd.index("-n") + 1] == str(np.K_ROWS + np.QMD_MARGIN),
              f"qmd is asked for K_ROWS + QMD_MARGIN rows; widening the window is a measured change (got {cmd})")
        return subprocess.CompletedProcess(cmd, 0, stdout='[{"file": "qmd://project-notes/specs/n5.md"}]', stderr="")

    (notes / ".qmd").mkdir()
    block, reason, probe = np.build_notes_push(str(project), str(ws), "pj:proj kanban", SID, str(state), env_auto, runner)
    lines = block.splitlines()
    check(reason is None, "no degrade reason when qmd answers")
    check(lines[0] == np.HEADER and lines[1].startswith("| specs/n1.md |") and lines[2].startswith("| specs/n2.md |"),
          f"L1 row from the .reads ledger, then its L2 sibling, come first (got {lines[:3]})")
    check(lines[3].startswith("| specs/n5.md |") and lines[4].startswith("| specs/n3.md |"),
          f"qmd (n5) and lexical (n3) results merge by taking the two lists alternately (got {lines[3:5]})")
    check(lines[-1] == "project-notes matched: 4 of 5 rows (4 shown)", f"footer (got {lines[-1]!r})")

    (notes / ".qmd").rmdir()
    block2, reason2, _ = np.build_notes_push(str(project), str(ws), "kanban", SID, str(state), env_auto, runner)
    check(reason2 is None and "| specs/n5.md |" not in block2 and "| specs/n3.md |" in block2,
          "without project-notes/.qmd/ qmd is not consulted, no notice, lexical still ranks")

    (notes / ".qmd").mkdir()

    def runner_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    block3, reason3, _ = np.build_notes_push(str(project), str(ws), "kanban", SID, str(state), env_auto, runner_timeout)
    check(reason3 is not None and "timeout" in reason3, f"a qmd timeout degrades with a reason (got {reason3!r})")
    check("| specs/n3.md |" in block3, "the lexical rank still fills the topic layer on degrade")

    block4, reason4, _ = np.build_notes_push(str(project), str(ws), "kanban", SID, str(state),
                                          {"TASKFLOW_QMD_BIN": str(ws / "nope"), "TASKFLOW_NOTES_PUSH": "auto"}, runner)
    check(reason4 is not None and "not found" in reason4, f"an unresolvable binary degrades with a reason (got {reason4!r})")

    block5, reason5, _ = np.build_notes_push(str(project), str(ws), "kanban", SID, str(state),
                                          {"TASKFLOW_NOTES_PUSH": "off"}, runner)
    check(reason5 is None and "| specs/n1.md |" in block5 and "| specs/n3.md |" not in block5,
          "mode off keeps the location layer only")

    block6, _, _ = np.build_notes_push(str(project), str(ws), "pj:proj -y", SID, str(state), env_auto, runner)
    check("| specs/n1.md |" in block6 and "| specs/n5.md |" not in block6,
          "an all-mechanism prompt skips the topic layer but keeps location rows")

    check(np.build_notes_push(str(ws / "_projects" / "none"), str(ws), "x", SID, str(state), env_auto, runner) == ("", None, None),
          "a project without project-notes/index.md yields no block")
    check("_archive/old.md" not in block and "_archive/old.md" not in block3, "_archive/ rows never appear")


def test_symbol_route_due() -> None:
    print("--- symbol_route_due ---")
    code = ["src/a.py"]
    prose = ["docs/a.md"]

    check(np.symbol_route_due(code, matched=9, shown=8) is True,
          "a code turn whose block overflowed is the turn the symbol route is written for")
    check(np.symbol_route_due(code, matched=8, shown=8) is False,
          "a block that fit needs no symbol route; M == K is not overflow")
    check(np.symbol_route_due(prose, matched=9, shown=8) is False,
          "overflow on a turn touching no code is not a symbol-route turn")
    check(np.symbol_route_due([], matched=9, shown=8) is False,
          "overflow with no location signal at all is not a symbol-route turn")
    check(np.symbol_route_due(["src/a.TS"], matched=9, shown=8) is True,
          "the suffix test ignores case so a shouted extension still counts")


def test_probe_shape(root: Path) -> None:
    print("--- probe shape ---")
    ws, project, notes = make_ws(root)
    state = ws / "_projects" / "_state"
    write(state / f"{SID}.reads", "src/a.py\n")
    env_off = {"TASKFLOW_NOTES_PUSH": "off"}

    _, _, probe = np.build_notes_push(str(project), str(ws), "kanban", SID, str(state), env_off)
    check(sorted(probe) == ["code", "due", "k", "m"],
          f"the probe carries the verdict and the three numbers that decided it (got {sorted(probe)})")
    check(probe["code"] == 1, f"one code path came from the .reads ledger (got {probe['code']})")
    check(probe["due"] is (probe["m"] > probe["k"]),
          "due agrees with the overflow it is derived from")
    check(np.symbol_route_due(["src/a.py"], probe["m"], probe["k"]) == probe["due"],
          "the probe's verdict is the predicate's, not a second implementation")


def main() -> int:
    print("=== notes_push.py unit tests ===")
    test_subject_text()
    test_anchors_parser()
    test_query_doc()
    test_timeout_env()
    test_qmd_rank()
    test_contract_literals()
    test_symbol_route_due()
    for t in (test_prompt_paths, test_location, test_ledger_tail, test_lexical, test_union_and_compose, test_build_end_to_end,
                test_probe_shape):
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
