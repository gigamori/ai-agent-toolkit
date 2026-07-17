"""Tests: UserPromptSubmit marker-inject hook (D8, design §6 F2).

Covers: marker present -> wiki-active additionalContext emitted; dormant -> empty
exit (no output). The hook is run as a subprocess with hook JSON on stdin.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1] / "hooks" / "wiki_marker_inject.py"
)

# hooks/ is a CC-only asset (pi maps activation to TS and does not copy hooks/).
# Skip the whole module when the hook file is absent so this passes in CC and
# skips harmlessly in a pi-style harness. Keeps the copy bit-for-bit.
if not _HOOK.exists():
    pytest.skip(
        "hooks/wiki_marker_inject.py is CC-only; not present in this harness",
        allow_module_level=True,
    )


def _run(cwd: Path, prompt: str = "hello", session_id: str = "", env=None):
    payload = {"cwd": str(cwd), "prompt": prompt}
    if session_id:
        payload["session_id"] = session_id
    # encoding="utf-8" is REQUIRED on Windows: the hook writes UTF-8 and the
    # default cp932 decode raises on any non-ASCII byte (e.g. resolved paths).
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", timeout=30, env=env,
    )


def _ctx(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _make_wiki(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")


def _write_state(cwd: Path, project: str, name: str):
    state_dir = cwd / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / name).write_text(json.dumps({"project": project}), encoding="utf-8")


def test_emits_wiki_active_when_marker_present(tmp_path):
    (tmp_path / ".llmwiki").write_text("version: 1\nschema: SCHEMA.md\n", encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0
    ctx = _ctx(r)
    assert "wiki-active" in ctx
    assert json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_empty_exit_when_dormant(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


# --------------------------------------------------------------------------- #
# Phase 1 P2/P3 — `wiki:on|off` toggle + injection extensions
# --------------------------------------------------------------------------- #
def test_on_emits_wiki_on_leading_line_directive(tmp_path):
    _make_wiki(tmp_path)
    ctx = _ctx(_run(tmp_path, session_id="sid-a"))
    assert "[wiki:on]" in ctx
    assert "wiki-active" in ctx


def test_off_suppresses_injection_and_emits_off_notice(tmp_path):
    _make_wiki(tmp_path)
    ctx = _ctx(_run(tmp_path, prompt="please wiki:off now", session_id="sid-a"))
    assert "[wiki:off]" in ctx
    assert "wiki-active" not in ctx
    assert "active wiki:" not in ctx  # discovery line fully suppressed while off


def test_toggle_is_sticky_and_reversible(tmp_path):
    _make_wiki(tmp_path)
    # off -> stays off next turn (no marker) -> on restores
    _run(tmp_path, prompt="wiki:off", session_id="sid-a")
    ctx_still_off = _ctx(_run(tmp_path, prompt="hello", session_id="sid-a"))
    assert "[wiki:off]" in ctx_still_off
    ctx_back_on = _ctx(_run(tmp_path, prompt="wiki:on", session_id="sid-a"))
    assert "wiki-active" in ctx_back_on
    assert "[wiki:on]" in ctx_back_on


def test_new_session_starts_on_despite_other_session_off(tmp_path):
    _make_wiki(tmp_path)
    _run(tmp_path, prompt="wiki:off", session_id="sid-a")
    ctx = _ctx(_run(tmp_path, prompt="hello", session_id="sid-b"))
    assert "wiki-active" in ctx  # sid-b is unaffected by sid-a's off


def test_toggle_ignored_when_unresolved(tmp_path):
    # No wiki anywhere -> `wiki:off` is ignored and nothing is emitted.
    r = _run(tmp_path, prompt="wiki:off", session_id="sid-a")
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_pj_scope_injects_coexistence_guide(tmp_path, monkeypatch):
    proot = tmp_path / "roots"
    _make_wiki(proot / "myproj" / "wiki")
    _write_state(tmp_path, "myproj", name="sid-a.json")
    env = dict(os.environ, TASKFLOW_PROJECT_ROOTS=str(proot))
    r = _run(tmp_path, prompt="hello", session_id="sid-a", env=env)
    ctx = _ctx(r)
    assert "[wiki<->taskflow]" in ctx  # pj-scope coexistence guide present


def test_cwd_scope_has_no_coexistence_guide(tmp_path):
    # cwd scope resolves but must NOT carry the pj-only coexistence guide.
    _make_wiki(tmp_path)
    ctx = _ctx(_run(tmp_path, session_id="sid-a"))
    assert "[wiki<->taskflow]" not in ctx
    assert "wiki-active" in ctx


# --------------------------------------------------------------------------- #
# Step 3 — filing-marker contract pin (B) + wiki:off fail-visible (C)
# --------------------------------------------------------------------------- #
def test_marker_present_emits_file_tag_fea_and_slug_variants(tmp_path):
    # Part B(a): with a filing marker present (wiki-active), the emitted context
    # carries the `[llm-wiki:file]` tag + the FE-A path text, in BOTH the
    # slug-present and slug-absent variants (hook emit block).
    _make_wiki(tmp_path)
    # slug-present variant
    ctx_slug = _ctx(_run(tmp_path, prompt="answer this llm-wiki:file=mypage", session_id="sid-a"))
    assert "[llm-wiki:file]" in ctx_slug
    assert "FE-A path (wiki-query SKILL Step 3)" in ctx_slug
    assert "Target page name is `mypage` -> `wiki/derived/mypage.md`" in ctx_slug
    # slug-absent variant
    ctx_noslug = _ctx(_run(tmp_path, prompt="answer this llm-wiki:file", session_id="sid-b"))
    assert "[llm-wiki:file]" in ctx_noslug
    assert "FE-A path (wiki-query SKILL Step 3)" in ctx_noslug
    assert "No slug given: generate the page name from the answer content" in ctx_noslug


def test_skill_md_references_file_marker_tag():
    # Part B(b): the wiki-query SKILL references the same `[llm-wiki:file]` tag
    # literal that the hook emits (contract pinned across hook <-> skill).
    skill_md = _HOOK.resolve().parents[1] / "skills" / "wiki-query" / "SKILL.md"
    assert "[llm-wiki:file]" in skill_md.read_text(encoding="utf-8")


def test_off_plus_marker_reports_dropped(tmp_path):
    # Part C: a filing marker sent while wiki is OFF must not be silently
    # dropped — the off notice surfaces the DROP with re-send guidance.
    _make_wiki(tmp_path)
    ctx = _ctx(_run(tmp_path, prompt="wiki:off please file this llm-wiki:file", session_id="sid-a"))
    assert "[wiki:off]" in ctx
    assert "DROPPED because wiki is OFF" in ctx
    assert "Re-send `wiki:on`" in ctx
