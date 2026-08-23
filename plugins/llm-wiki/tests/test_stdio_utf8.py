import json
import os
import subprocess
import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent

_NONBMP = "\U00020BB7"
_JP_BODY = (
    "# 日本語 UTF-8 契約テスト\n\n"
    f"CANARY: {_NONBMP}野家 の {_NONBMP}、森鷗外 の 鷗\n"
    "ひらがな カタカナ 漢字 ①②③\n"
)
_NONBMP_UTF8 = _NONBMP.encode("utf-8")

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


def _init_wiki(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n",
                                   encoding="utf-8")
    (root / "SCHEMA.md").write_text(_SCHEMA, encoding="utf-8")
    (root / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / "log.md").write_text("# Log\n", encoding="utf-8")


def _run_cli(args: "list[str]", *, stdin_bytes: "bytes | None" = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PKG_ROOT)
    env["PYTHONIOENCODING"] = "cp932"
    env["PYTHONUTF8"] = "0"
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from llmwiki.cli import main; sys.exit(main())",
         *args],
        input=stdin_bytes, capture_output=True, env=env, timeout=120,
    )


def test_file_verb_utf8_stdin_survives_cp932_locale(tmp_path):
    root = tmp_path / "wiki_root"
    _init_wiki(root)

    proc = _run_cli(["file", str(root), "wiki/derived/utf8-test.md",
                     "utf8 contract"],
                    stdin_bytes=_JP_BODY.encode("utf-8"))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"written:" in proc.stdout

    page = root / "wiki" / "derived" / "utf8-test.md"
    assert page.is_file()
    page_bytes = page.read_bytes()
    assert _NONBMP_UTF8 in page_bytes, (
        "the non-BMP canary survives byte-exactly on disk, so stdin was decoded "
        "as strict UTF-8 rather than under the forced locale"
    )
    assert "鷗".encode("utf-8") in page_bytes


def test_resolve_root_nonascii_root_round_trips_under_cp932(tmp_path):
    root = tmp_path / f"wiki-{_NONBMP}root"
    _init_wiki(root)

    proc = _run_cli(["resolve-root", "--root", str(root)])
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    lines = proc.stdout.decode("utf-8").splitlines()
    out_root, scope = lines[0].strip(), lines[1].strip()
    assert _NONBMP in out_root, (
        "the stdout reconfigure wins over the forced locale, which cannot encode "
        "this character at all"
    )
    assert scope == "prompt"


def test_ingest_apply_utf8_manifest_survives_cp932_locale(tmp_path):
    root = tmp_path / "wiki_root"
    _init_wiki(root)
    src = root / "input.txt"
    src.write_text("third party content", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PKG_ROOT)
    env["PYTHONIOENCODING"] = "cp932"
    env["PYTHONUTF8"] = "0"
    begin = subprocess.run(
        [sys.executable, "-c",
         "import sys; from llmwiki.ingest.ingest_driver import main; "
         "sys.exit(main())",
         "begin", str(root), str(src), "--kind=fe_b"],
        capture_output=True, env=env, timeout=120,
    )
    assert begin.returncode == 0, begin.stderr.decode("utf-8", "replace")

    manifest = json.dumps(
        [{"rel_path": "wiki/utf8-apply.md", "content": _JP_BODY}],
        ensure_ascii=False,
    )
    proc = _run_cli(["ingest-apply", str(root), "fe_b"],
                    stdin_bytes=manifest.encode("utf-8"))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    page_bytes = (root / "wiki" / "utf8-apply.md").read_bytes()
    assert _NONBMP_UTF8 in page_bytes

    finish = subprocess.run(
        [sys.executable, "-c",
         "import sys; from llmwiki.ingest.ingest_driver import main; "
         "sys.exit(main())",
         "finish", str(root), "fail"],
        capture_output=True, env=env, timeout=120,
    )
    assert finish.returncode == 0, finish.stderr.decode("utf-8", "replace")
