"""Exit-code contract: generate_wiki_view.main (2026-07-16, generalizing the
theme1 i:39 cli.py contract to this entrypoint).

  rc 0  = success
  rc 2  = SENTINEL (no wiki resolved — normal-data state notice)
  rc 3  = OPERATIONAL error (port already in use)
  rc 64 = EX_USAGE (usage/protocol error: --serve omitted, bad argv)

Model-free and dependency-free (no uv / network beyond loopback).
"""
import socket

from llmwiki.view import generate_wiki_view as gwv


def _make(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "derived").mkdir()
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# usage / protocol errors -> EX_USAGE (64)
# --------------------------------------------------------------------------- #
def test_serve_omitted_is_ex_usage(tmp_path):
    _make(tmp_path)
    rc = gwv.main(["--root", str(tmp_path)])
    assert rc == gwv.EX_USAGE


def test_bad_argv_is_ex_usage(tmp_path):
    _make(tmp_path)
    rc = gwv.main(["--root", str(tmp_path), "--no-such-flag"])
    assert rc == gwv.EX_USAGE


def test_help_is_rc0():
    rc = gwv.main(["--help"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# genuine SENTINEL stays rc2
# --------------------------------------------------------------------------- #
def test_no_wiki_resolved_is_sentinel_rc2(tmp_path, monkeypatch):
    # `--root` is an explicit override and is NOT existence-gated
    # (wiki_root_resolver.resolve, prompt scope) — the sentinel path is only
    # reachable when no `--root` is passed and pj/workspace/cwd all fail.
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)   # empty cwd, no .llmwiki marker
    rc = gwv.main(["--serve", "--no-open"])
    assert rc == 2


# --------------------------------------------------------------------------- #
# OPERATIONAL error (port bind failure) -> rc3
# --------------------------------------------------------------------------- #
def test_port_in_use_is_operational_rc3(tmp_path):
    _make(tmp_path)
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind((gwv.SERVE_HOST, 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    try:
        rc = gwv.main(["--serve", "--no-open", "--root", str(tmp_path),
                       "--port", str(port)])
        assert rc == 3
    finally:
        occupied.close()
