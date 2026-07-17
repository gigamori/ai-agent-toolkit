"""Exit-code contract: ingest_driver.main / apply_finish.run_apply_finish_cli
(2026-07-16, generalizing the theme1 i:39 cli.py contract to this entrypoint).

  rc 0  = success
  rc 2  = SENTINEL (state notice — NO-MARKER / no sidecar / REFUSED / zero-match)
  rc 3  = OPERATIONAL error (runtime/environment/verification failure)
  rc 64 = EX_USAGE (usage/protocol error: bad argv, unknown verb, bad --kind)

Model-free and dependency-free (no uv / duckdb / network).
"""
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


# --------------------------------------------------------------------------- #
# usage / protocol errors -> EX_USAGE (64)
# --------------------------------------------------------------------------- #
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
    # too few positional args + no --manifest.
    assert drv.main(["apply-finish"]) == af.EX_USAGE


# --- begin strict arg contract (DEC-a; item3) -> EX_USAGE ------------------- #
def test_begin_unknown_flag_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src_uf.txt"
    src.write_text("hello", encoding="utf-8")
    # `--origin` is not a begin flag (origin is selected via --kind).
    rc = drv.main(["begin", str(tmp_path), str(src), "--origin=fe_b"])
    assert rc == drv.EX_USAGE


def test_begin_empty_value_flag_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src_ev.txt"
    src.write_text("hello", encoding="utf-8")
    # `--kind` with no `=value` (space-form, unsupported for begin) is rejected.
    rc = drv.main(["begin", str(tmp_path), str(src), "--kind"])
    assert rc == drv.EX_USAGE


def test_begin_excess_positional_is_ex_usage(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "src_ex.txt"
    src.write_text("hello", encoding="utf-8")
    # A 3rd positional was silently ignored before strict arity (item3).
    rc = drv.main(["begin", str(tmp_path), str(src), "extra-arg"])
    assert rc == drv.EX_USAGE


# --- session-plan space-form --pj must NOT regress (guard is begin-only) ---- #
def test_session_plan_space_form_pj_not_ex_usage(tmp_path, monkeypatch):
    # `--pj name` (space form) is intentional for session-plan (L1663-1668); the
    # begin-only strict-flag guard (item3) must not reach it. Hermetic: pin the
    # state dir + ts ordering (mirrors test_session_plan.py) so the space-form
    # `--pj` is proven to resolve to the project name, not an empty-value reject.
    _init_wiki(tmp_path)
    state_dir = tmp_path / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sid-a.json").write_text(
        json.dumps({"project": "my-project", "origin": "cc"}), encoding="utf-8")
    monkeypatch.setattr(drv, "_state_dir", lambda cwd=None: state_dir)
    monkeypatch.setattr(drv, "_order_sids_by_started_ts", lambda sids: sorted(sids))

    # Direct call proves the space form resolves pj -> the project name.
    out = drv.session_plan(str(tmp_path), pj="my-project", kind="auto")
    assert out["sids"] == ["sid-a"]
    # Via the CLI entrypoint: `--pj my-project` must NOT be a usage error.
    rc = drv.main(["session-plan", str(tmp_path), "--pj", "my-project"])
    assert rc != drv.EX_USAGE
    assert rc == 0


# --------------------------------------------------------------------------- #
# genuine SENTINELs stay rc2
# --------------------------------------------------------------------------- #
def test_begin_no_marker_is_sentinel_rc2(tmp_path):
    src = tmp_path.parent / "src2.txt"
    src.write_text("hello", encoding="utf-8")
    rc = drv.main(["begin", str(tmp_path), str(src)])
    assert rc == 2


def test_finish_no_sidecar_is_sentinel_rc2(tmp_path):
    _init_wiki(tmp_path)
    assert drv.main(["finish", str(tmp_path), "fail"]) == 2


def test_enumerate_zero_match_is_sentinel_rc2(tmp_path):
    _init_wiki(tmp_path)
    rc = drv.main(["enumerate", str(tmp_path), "*.no-such-ext"])
    assert rc == 2


# --------------------------------------------------------------------------- #
# OPERATIONAL errors -> rc3
# --------------------------------------------------------------------------- #
def test_begin_non_utf8_source_is_operational_rc3(tmp_path):
    _init_wiki(tmp_path)
    src = tmp_path.parent / "binary.bin"
    src.write_bytes(b"\xff\xfe\x00\x01binary-not-utf8")
    rc = drv.main(["begin", str(tmp_path), str(src)])
    assert rc == 3


def test_begin_missing_source_is_operational_rc3(tmp_path):
    # A non-existent source must be a clean DriverOpError (exit 3), NOT an
    # uncaught FileNotFoundError traceback (item2 / DEC-b). Returning (not
    # raising) already proves no traceback escaped.
    _init_wiki(tmp_path)
    missing = tmp_path.parent / "does-not-exist.txt"
    rc = drv.main(["begin", str(tmp_path), str(missing)])
    assert rc == 3


# --------------------------------------------------------------------------- #
# subclass taxonomy: DriverUsageError / DriverOpError are still DriverError
# --------------------------------------------------------------------------- #
def test_subclasses_are_driver_error():
    assert issubclass(drv.DriverUsageError, drv.DriverError)
    assert issubclass(drv.DriverOpError, drv.DriverError)
