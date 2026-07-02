"""Read-profile import-closure test (D-2 enforcement / spec §read-profile 閉包).

D-2 is enforced by the IMPORT GRAPH, not by extras: the read CLI verbs must not
drag `llmwiki.write` / `llmwiki.ingest` into their import closure. `cli.py`
achieves this with branch-local lazy imports; this test pins that structurally.

Each verb is dispatched in a FRESH interpreter (so the closure is the verb's own,
not contaminated by a sibling verb run earlier in the same process), then the
loaded `llmwiki.*` modules are inspected.

Classification (verified against spec §CLI verb 契約 L79-80/L89 and the migrated
`cli.py` source — NOT assumed):
  - PURE read verbs (closure must contain NEITHER write NOR ingest):
        resolve-root, scan-pages, search, marker-detect, declare
    `declare` is read-only (D5 config read) and imports `core` only. `search`
    dispatches index|qmd internally (DEC-3) but imports only `read`/`core` (and
    the external qmd CLI is a subprocess, not a Python import), so its closure is
    write/ingest-free on both branches.
  - promote-check: read-only (NO move), the read-path INTO write/promote. By
    construction it lazy-imports `llmwiki.write.promote` (and transitively
    core.wiki_index) but NEVER `promote.promote` (the move) and NEVER
    `llmwiki.ingest`. So its assert is the WEAKER "no ingest" (write is expected,
    ingest is forbidden) — this is the spec's "read 部を move から分離".
  - file / promote / ingest-apply / floor-check are write/ingest-side verbs and
    are intentionally NOT covered by the read-profile closure assert.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

import llmwiki

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(llmwiki.__file__)))


def _verb_closure(argv, stdin=""):
    """Dispatch `argv` via cli.main in a fresh interpreter; return loaded
    `llmwiki.*` modules after the verb's branch-local imports executed."""
    driver = textwrap.dedent(
        """
        import sys, json
        sys.path.insert(0, sys.argv[1])
        from llmwiki import cli
        argv = json.loads(sys.argv[2])
        try:
            cli.main(argv)
        except SystemExit:
            pass
        except Exception:
            # Closure is captured even if the verb errors after importing
            # (e.g. NO-WIKI exit). The import has already happened by then.
            pass
        mods = sorted(m for m in sys.modules if m.startswith("llmwiki."))
        sys.stderr.write("\\n__MODS__=" + json.dumps(mods) + "\\n")
        """
    )
    r = subprocess.run(
        [sys.executable, "-c", driver, _PKG_ROOT, json.dumps(argv)],
        input=stdin, capture_output=True, text=True,
    )
    marker = "__MODS__="
    lines = [l for l in r.stderr.splitlines() if l.startswith(marker)]
    assert lines, f"closure driver produced no module list; stderr={r.stderr!r}"
    return set(json.loads(lines[-1][len(marker):]))


def _has_write(mods):
    return any(m == "llmwiki.write" or m.startswith("llmwiki.write.") for m in mods)


def _has_ingest(mods):
    return any(m == "llmwiki.ingest" or m.startswith("llmwiki.ingest.") for m in mods)


@pytest.fixture
def nonwiki_dir(tmp_path):
    # A plain dir with no .llmwiki marker: read verbs import their branch closure
    # then exit early (NO-WIKI / NO-MARKER), which is enough to capture the closure.
    return str(tmp_path)


@pytest.mark.parametrize("argv_factory", [
    lambda d: ["resolve-root", "--root", d],
    lambda d: ["scan-pages", d],
    lambda d: ["search", d, "--q", "some phrased question"],
    lambda d: ["marker-detect", d],
    lambda d: ["declare", d],
], ids=["resolve-root", "scan-pages", "search", "marker-detect", "declare"])
def test_pure_read_verb_closure_excludes_write_and_ingest(argv_factory, nonwiki_dir):
    mods = _verb_closure(argv_factory(nonwiki_dir))
    assert not _has_write(mods), f"pure read verb pulled in write/: {sorted(mods)}"
    assert not _has_ingest(mods), f"pure read verb pulled in ingest/: {sorted(mods)}"


def test_promote_check_closure_excludes_ingest_but_uses_write(tmp_path):
    # promote-check is the read-only read-path into write/promote: it MAY import
    # llmwiki.write (the preview reads derived_to_source_path / detect_contamination)
    # but must NOT import llmwiki.ingest, and must not call the move (asserted by
    # the unit-level promote tests; here we pin the import boundary).
    f = tmp_path / "wiki" / "derived" / "x.md"
    f.parent.mkdir(parents=True)
    f.write_text("derived page", encoding="utf-8")
    mods = _verb_closure(["promote-check", str(tmp_path), "wiki/derived/x.md"])
    assert _has_write(mods), (
        "promote-check is expected to import the write/promote read-path; "
        f"closure={sorted(mods)}"
    )
    assert not _has_ingest(mods), (
        f"promote-check must not pull in ingest/: {sorted(mods)}"
    )
