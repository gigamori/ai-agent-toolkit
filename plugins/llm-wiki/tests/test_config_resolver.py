from pathlib import Path

import pytest

from llmwiki.core import config_resolver as cr

_PKG = Path(__file__).resolve().parents[1]
_SCHEMA = _PKG / "templates" / "SCHEMA.md"


def test_load_config_from_template_schema():
    cfg = cr.load_config(_SCHEMA)
    assert cfg.get("activation_scope") == "scoped"
    assert cfg.get("write_mode") == "explicit"
    assert cfg.get("override_scope") == "operation"
    assert "transcript" not in cfg, "doc_type_profiles block keys do not leak into config"
    assert "paper" not in cfg


def test_precedence_prompt_wins():
    r = cr.resolve("write_mode", prompt_value="implicit",
                   wiki_config={"write_mode": "explicit"})
    assert r.value == "implicit"
    assert r.source == "prompt"


def test_precedence_wiki_when_no_prompt():
    r = cr.resolve("write_mode", prompt_value=None,
                   wiki_config={"write_mode": "implicit"})
    assert r.value == "implicit"
    assert r.source == "wiki"


def test_precedence_default_when_empty():
    r = cr.resolve("write_mode", prompt_value="", wiki_config={})
    assert r.value == "explicit"
    assert r.source == "default"


def test_axes_resolve_independently():
    res = cr.resolve_all(
        {"write_mode": "implicit"},
        {"read_grounding": "explicit"},
    )
    assert res["write_mode"].source == "prompt"
    assert res["read_grounding"].source == "wiki"
    assert res["activation_scope"].source == "default"


def test_declare_one_line():
    r = cr.resolve("write_mode", prompt_value="implicit", wiki_config={})
    line = cr.declare(r)
    assert "\n" not in line
    assert "write_mode" in line and "implicit" in line and "prompt" in line


def test_override_persists_session_only():
    op = cr.resolve_all({}, {"override_scope": "operation"})
    se = cr.resolve_all({}, {"override_scope": "session"})
    assert cr.override_persists(op) is False
    assert cr.override_persists(se) is True


def test_implicit_write_mode_flags():
    res = cr.resolve_all({"write_mode": "implicit"}, {})
    assert cr.write_mode_skips_confirmation(res) is True
    assert cr.autocommit_forced(res) is True



def test_resolve_all_returns_all_axes():
    res = cr.resolve_all({}, {})
    assert len(res) == len(cr.AXES) == 11
    assert "max_count" in res and "max_bytes" in res
    assert res["search_backend"].value == "index"
    assert res["qmd_bin"].value == "qmd"
    assert res["qmd_page_threshold"].value == "100"
    for ax in ("search_backend", "qmd_bin", "qmd_page_threshold"):
        assert res[ax].source == "default"


def test_budget_axes_default_values():
    res = cr.resolve_all({}, {})
    assert res["max_count"].value == "100"
    assert res["max_count"].source == "default"
    assert res["max_bytes"].value == "10485760"
    assert res["max_bytes"].source == "default"


def test_consistency_check_passes_when_k_le_max_count():
    res = cr.resolve_all({}, {})
    assert cr.check_consistency(res) is None


def test_consistency_check_passes_on_equality():
    res = cr.resolve_all({"apply_fanout_k": "100", "max_count": "100"}, {})
    assert cr.check_consistency(res) is None


def test_consistency_check_fails_when_k_gt_max_count():
    res = cr.resolve_all({"apply_fanout_k": "200"}, {})
    with pytest.raises(cr.ConfigInconsistency):
        cr.check_consistency(res)


def test_consistency_check_fails_on_non_integer():
    res = cr.resolve_all({"max_count": "lots"}, {})
    with pytest.raises(cr.ConfigInconsistency):
        cr.check_consistency(res)
