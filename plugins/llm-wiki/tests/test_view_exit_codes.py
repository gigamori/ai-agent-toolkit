import socket

from llmwiki.view import generate_wiki_view as gwv


def _make(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "derived").mkdir()
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                       encoding="utf-8")
    (tmp_path / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")


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


def test_no_wiki_resolved_is_sentinel_rc2(tmp_path, monkeypatch):
    monkeypatch.delenv("TASKFLOW_PROJECT_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    rc = gwv.main(["--serve", "--no-open"])
    assert rc == 2, (
        "the sentinel is reachable only without --root: --root is an explicit "
        "override and is not existence-gated"
    )


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
