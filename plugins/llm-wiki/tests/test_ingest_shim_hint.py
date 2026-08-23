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
    rc = _SHIM.main([verb, "arg1", "arg2"])
    err = capsys.readouterr().err
    assert rc == _SHIM.EX_USAGE == 64
    assert "did you mean" in err
    assert f"llmwiki-ingest ingest {verb}" in err


def test_late_added_verbs_fire_hint(capsys):
    for verb in ("apply-finish", "project-batch-cleanup"):
        _SHIM.main([verb])
        err = capsys.readouterr().err
        assert "did you mean" in err
        assert verb in err


def test_unknown_first_token_is_usage_no_hint(capsys):
    rc = _SHIM.main(["totally-unknown"])
    err = capsys.readouterr().err
    assert rc == _SHIM.EX_USAGE
    assert "did you mean" not in err
    assert "usage: llmwiki-ingest ingest" in err


def test_usage_banner_lists_all_nine_verbs(capsys):
    _SHIM.main([])
    err = capsys.readouterr().err
    for verb in drv.INGEST_VERBS:
        assert verb in err
    assert len(drv.INGEST_VERBS) == 9


def test_ingest_group_passes_through(capsys, monkeypatch):
    seen = {}

    def _fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(_SHIM.ingest_driver, "main", _fake_main)
    rc = _SHIM.main(["ingest", "begin", "/root", "/src"])
    assert rc == 0
    assert seen["argv"] == ["begin", "/root", "/src"]
