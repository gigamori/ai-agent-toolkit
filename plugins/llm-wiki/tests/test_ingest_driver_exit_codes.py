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


# --------------------------------------------------------------------------- #
# subclass taxonomy: DriverUsageError / DriverOpError are still DriverError
# --------------------------------------------------------------------------- #
def test_subclasses_are_driver_error():
    assert issubclass(drv.DriverUsageError, drv.DriverError)
    assert issubclass(drv.DriverOpError, drv.DriverError)
