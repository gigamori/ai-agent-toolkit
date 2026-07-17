"""Contract: the `bin/llmwiki-ingest` shim rejects a driver verb that dropped
the required `ingest` area-entrypoint group with EX_USAGE(64) + a targeted
"did you mean" hint (item2).

The hint and the usage banner are BUILT from ingest_driver.INGEST_VERBS (the
single source of truth), so the hint fires for EVERY canonical verb — including
`apply-finish` and `project-batch-cleanup`, which the old drifted 7-verb banner
omitted.

The shim binary has no file extension; it is loaded in-process via
SourceFileLoader (dependency-free — the guard path under test returns before
any driver dispatch, so no uv / duckdb is needed). This mirrors the existing
tests' in-process invocation of the CLI rather than executing the PEP723 bin
(which on Windows would require `uv run --script bin/llmwiki-ingest`).
"""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from llmwiki.ingest import ingest_driver as drv

_BIN = Path(__file__).resolve().parent.parent / "bin" / "llmwiki-ingest"


def _load_shim():
    loader = importlib.machinery.SourceFileLoader("llmwiki_ingest_shim", str(_BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_SHIM = _load_shim()


@pytest.mark.parametrize("verb", list(drv.INGEST_VERBS))
def test_missing_ingest_group_hints_for_every_verb(verb, capsys):
    # e.g. `llmwiki-ingest begin ...` (the `ingest` group dropped).
    rc = _SHIM.main([verb, "arg1", "arg2"])
    err = capsys.readouterr().err
    assert rc == _SHIM.EX_USAGE == 64
    assert "did you mean" in err
    assert f"llmwiki-ingest ingest {verb}" in err


def test_previously_missing_verbs_fire_hint(capsys):
    # Regression: the drifted 7-verb banner omitted these two entirely.
    for verb in ("apply-finish", "project-batch-cleanup"):
        _SHIM.main([verb])
        err = capsys.readouterr().err
        assert "did you mean" in err
        assert verb in err


def test_unknown_first_token_is_usage_no_hint(capsys):
    rc = _SHIM.main(["totally-unknown"])
    err = capsys.readouterr().err
    assert rc == _SHIM.EX_USAGE
    assert "did you mean" not in err          # hint only for known driver verbs
    assert "usage: llmwiki-ingest ingest" in err


def test_usage_banner_lists_all_nine_verbs(capsys):
    _SHIM.main([])
    err = capsys.readouterr().err
    for verb in drv.INGEST_VERBS:
        assert verb in err
    assert len(drv.INGEST_VERBS) == 9


def test_ingest_group_passes_through(capsys, monkeypatch):
    # `ingest <verb>` delegates to the driver verbatim (no shim usage error).
    seen = {}

    def _fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(_SHIM.ingest_driver, "main", _fake_main)
    rc = _SHIM.main(["ingest", "begin", "/root", "/src"])
    assert rc == 0
    assert seen["argv"] == ["begin", "/root", "/src"]
