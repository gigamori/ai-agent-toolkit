import json
import os
import subprocess
import sys
import textwrap

import pytest

import llmwiki

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(llmwiki.__file__)))


def _verb_closure(argv, stdin=""):
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
        input=stdin, capture_output=True, text=True, encoding="utf-8",
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
