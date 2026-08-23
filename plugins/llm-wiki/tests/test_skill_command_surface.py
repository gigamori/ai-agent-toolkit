import os
import re

import pytest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILLS_DIR = os.path.join(_PKG_ROOT, "skills")


def _load_skill_md(skill_dir):
    path = os.path.join(_SKILLS_DIR, skill_dir, "SKILL.md")
    assert os.path.isfile(path), f"{skill_dir}: SKILL.md missing at {path}"
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
    assert m, f"{skill_dir}: missing frontmatter block"
    return m.group(1), m.group(2)


def _name(frontmatter, skill_dir):
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            return line[len("name:"):].strip()
    pytest.fail(f"{skill_dir}: no name: line in frontmatter")


@pytest.mark.parametrize("skill_dir", [
    "wiki-ingest-docs", "wiki-file", "wiki-ingest-sessions",
])
def test_skill_dir_name_matches_frontmatter_name(skill_dir):
    frontmatter, _ = _load_skill_md(skill_dir)
    assert _name(frontmatter, skill_dir) == skill_dir


def test_old_wiki_ingest_skill_is_gone_no_alias():
    assert not os.path.exists(os.path.join(_SKILLS_DIR, "wiki-ingest")), (
        "skills/wiki-ingest/ must not exist: it was renamed to wiki-ingest-docs "
        "with no alias left behind"
    )


def test_ingest_docs_never_passes_kind():
    _, body = _load_skill_md("wiki-ingest-docs")
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("uv run", "  --kind", "${KIND")) and "--kind" in stripped:
            pytest.fail(
                f"/wiki-ingest-docs must not pass --kind, which would disarm the "
                f"fail-closed .jsonl gate; found: {stripped!r}")
    assert "${KIND:+--kind=" not in body, (
        "/wiki-ingest-docs must not pass --kind even conditionally")


def test_wiki_file_fixes_kind_and_cutoff():
    _, body = _load_skill_md("wiki-file")
    assert "--kind=fe_b_prime" in body, (
        "/wiki-file must pin --kind=fe_b_prime")
    assert "--cutoff=last-user" in body, (
        "/wiki-file must pin --cutoff=last-user: the invocation turn is an "
        "instruction, not payload")


def test_wiki_file_sources_the_running_session_id():
    _, body = _load_skill_md("wiki-file")
    assert "${CLAUDE_SESSION_ID}" in body, (
        "/wiki-file must capture the running session id via the skill-template "
        "substitution rather than asking the user for it")


def test_wiki_file_has_no_glob_dispatch():
    _, body = _load_skill_md("wiki-file")
    assert "ingest enumerate" not in body, (
        "/wiki-file must not call the enumerate verb (single sid, no glob)")


def test_wiki_file_narrowing_channel_is_drop_only():
    _, body = _load_skill_md("wiki-file")
    assert "project-batch" in body, (
        "/wiki-file's narrowing flow must extract turns with the code-owned "
        "project-batch verb")
    assert "DELETE whole entries" in body, (
        "/wiki-file must state the drop-only rule for the narrowing channel")


def test_wiki_file_narrowing_protects_the_cutoff_anchor():
    _, body = _load_skill_md("wiki-file")
    assert "NEVER delete anything from the LAST USER-ROLE entry onward" in body, (
        "/wiki-file's narrowing flow must forbid deleting the cutoff anchor, "
        "anchored on the last USER-ROLE entry rather than the file's last entry")
    assert "not code-enforced" in body or "NOT code-enforced" in body, (
        "the anchor rule must be marked as prompt-only, so a reader knows the "
        "driver will not catch this mistake")


@pytest.mark.parametrize("skill_dir", [
    "wiki-ingest-docs", "wiki-file", "wiki-ingest-sessions",
])
def test_write_skills_ask_before_applying_when_explicit(skill_dir):
    _, body = _load_skill_md(skill_dir)
    assert "AskUserQuestion" in body, (
        f"{skill_dir}: declares AskUserQuestion in allowed-tools but never tells "
        f"the orchestrator to USE it, so the explicit branch would do nothing")
    assert "write_mode" in body and "explicit" in body, (
        f"{skill_dir}: must branch on write_mode resolving to explicit")


@pytest.mark.parametrize("skill_dir", [
    "wiki-ingest-docs", "wiki-file", "wiki-ingest-sessions",
])
def test_write_skills_handle_the_unanswerable_confirmation(skill_dir):
    _, body = _load_skill_md(skill_dir)
    assert "non-interactive" in body.lower(), (
        f"{skill_dir}: must state what happens when the confirmation cannot be "
        f"answered (print mode / automation)")
    assert "write_mode=implicit" in body, (
        f"{skill_dir}: must name the deliberate implicit override as the "
        f"non-interactive escape hatch")


@pytest.mark.parametrize("skill_dir,loop_word", [
    ("wiki-ingest-docs", "sweep"),
    ("wiki-ingest-sessions", "sweep"),
])
def test_batch_skills_ask_once_at_the_batch_head(skill_dir, loop_word):
    _, body = _load_skill_md(skill_dir)
    assert loop_word in body.lower(), (
        f"{skill_dir}: the batch-head confirmation must describe the run as a "
        f"whole")
    assert "do NOT ask again" in body, (
        f"{skill_dir}: must forbid re-asking per item after the batch-head "
        f"confirmation")


def test_wiki_ingest_docs_prefixes_the_wiki_root_onto_enumerate_output():
    _, body = _load_skill_md("wiki-ingest-docs")
    assert '"$WIKI_ROOT/$rel_path"' in body, (
        "the docs loop must pass the wiki-root-prefixed path to begin, not the "
        "bare enumerate rel_path")
