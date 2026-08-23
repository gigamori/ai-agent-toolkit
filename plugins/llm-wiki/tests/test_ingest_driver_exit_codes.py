import json

import pytest

from llmwiki.ingest import ingest_driver as drv
from llmwiki.ingest import apply_finish as af


_SCHEMA = """---
config:
  activation_scope: scoped
  read_grounding:  implicit
  write_mode:      explicit
  write_autocommit: auto
  override_scope:  operation
  apply_fanout_k:  10
  max_count:       100
  max_bytes:       10485760
---
# SCHEMA
"""


def _init_wiki(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "index.md").write_text("# Index\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n", encoding="utf-8")


def test_missing_verb_is_ex_usage():
    assert drv.main([]) == drv.EX_USAGE


def test_unknown_verb_is_ex_usage():
    assert drv.main(["no-such-verb"]) == drv.EX_USAGE


@pytest.mark.parametrize("argv", [
    ["begin", "/only-one-arg"],
    ["plan-fanout", "/only-one-arg"],
    ["finish", "/only-one-arg"],
    ["abort"],
    ["enumerate", "/only-one-arg"],
    ["project-batch", "/only-one-arg"],
    ["project-batch-cleanup"],
], ids=lambda a: "_".join(a))
def test_argv_shape_errors_are_ex_usage(argv):
    assert drv.main(argv) == drv.EX_USAGE


def test_finish_bad_outcome_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    assert drv.main(["finish", str(tmp_path), "bogus-outcome"]) == drv.EX_USAGE


def test_begin_bad_kind_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src.txt"
    src.write_text("hello", encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src), "--kind=bogus"])
    assert rc == drv.EX_USAGE


def test_apply_finish_usage_is_ex_usage():
    assert drv.main(["apply-finish"]) == af.EX_USAGE


def test_begin_unknown_flag_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src_uf.txt"
    src.write_text("hello", encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src), "--origin=fe_b"])
    assert rc == drv.EX_USAGE


def test_begin_empty_value_flag_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src_ev.txt"
    src.write_text("hello", encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src), "--kind"])
    assert rc == drv.EX_USAGE


def test_begin_excess_positional_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src_ex.txt"
    src.write_text("hello", encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src), "extra-arg"])
    assert rc == drv.EX_USAGE


def test_session_plan_space_form_pj_not_ex_usage(tmp_path, monkeypatch):
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sid-a.json").write_text(
        json.dumps({"project": "my-project", "origin": "cc"}), encoding="utf-8")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    out = drv.session_plan(str(tmp_path), pj="my-project", kind="auto")
    assert out["sids"] == ["sid-a"]
    rc = drv.main(["session-plan", str(tmp_path), "--pj", "my-project"])
    assert rc != drv.EX_USAGE
    assert rc == 0


def test_begin_no_marker_is_sentinel_rc2(tmp_path):
    src = tmp_path.parent / "src2.txt"
    src.write_text("hello", encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src)])
    assert rc == 2


def test_finish_no_sidecar_is_sentinel_rc2(tmp_path):
    _init_wiki(tmp_path)
    assert drv.main(["finish", str(tmp_path), "fail"]) == 2


def test_abort_ownership_mismatch_is_sentinel_rc2(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src3.txt"
    src.write_text("owned by A", encoding="utf-8")
    drv.main(["begin", str(tmp_path), str(src), "--kind=fe_b"])
    (tmp_path / drv.transaction.LOCK_NAME).write_text(
        json.dumps({"pid": 999999, "token": "FOREIGN"}), encoding="utf-8")
    rc = drv.main(["abort", str(tmp_path)])
    assert rc == 2
    assert rc != drv.EX_USAGE


def test_enumerate_zero_match_is_sentinel_rc2(tmp_path):
    _init_wiki(tmp_path)
    rc = drv.main(["enumerate", str(tmp_path), "*.no-such-ext"])
    assert rc == 2


def test_begin_jsonl_auto_kind_is_sentinel_rc2(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "some-sid.jsonl"
    src.write_text('{"turns": []}\n', encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src)])
    assert rc == 2
    assert rc != drv.EX_USAGE


def test_begin_jsonl_explicit_fe_b_is_not_sentinel(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "some-sid.jsonl"
    src.write_text('{"turns": []}\n', encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src), "--kind=fe_b"])
    assert rc == 0
    drv.main(["abort", str(tmp_path)])


def test_begin_non_utf8_source_is_operational_rc3(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "binary.bin"
    src.write_bytes(b"\xff\xfe\x00\x01binary-not-utf8")
    rc = drv.main(["begin", str(tmp_path), str(src)])
    assert rc == 3


def test_begin_missing_source_is_operational_rc3(tmp_path):
    _init_wiki(tmp_path)
    missing = tmp_path.parent / "does-not-exist.txt"
    rc = drv.main(["begin", str(tmp_path), str(missing)])
    assert rc == 3


def test_subclasses_are_driver_error():
    assert issubclass(drv.DriverUsageError, drv.DriverError)
    assert issubclass(drv.DriverOpError, drv.DriverError)
