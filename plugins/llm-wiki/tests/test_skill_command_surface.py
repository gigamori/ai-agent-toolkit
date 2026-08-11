"""Static contract test for the llm-wiki SKILL command surface (D1-D4, D11, D13).

The command surface is named by its OBJECT, not by pipeline weight: a document
(`/wiki-ingest-docs`), the running conversation (`/wiki-file`), or other
sessions' logs (`/wiki-ingest-sessions`). These are user-visible names and the
old `/wiki-ingest` was renamed with NO alias, so drift here is a broken command
for every user — pin it.

The `--kind` assertions are the D11 safety point, not cosmetics: `/wiki-ingest-docs`
must NOT pass `--kind`, because omitting it leaves `--kind=auto`, which is what
keeps `begin`'s fail-closed `.jsonl` gate REACHABLE on the docs sweep (the text
extension allowlist includes `.jsonl`, so the sweep can enumerate a session log).
An explicit `--kind=fe_b` resolves to the same origin as auto, so hardcoding it
would buy nothing except disarming that gate.
"""
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


# --------------------------------------------------------------------------- #
# D1/D3: the three write-bearing entry points exist under their object names,
# and the pre-rename `/wiki-ingest` is gone with no alias left behind.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("skill_dir", [
    "wiki-ingest-docs", "wiki-file", "wiki-ingest-sessions",
])
def test_skill_dir_name_matches_frontmatter_name(skill_dir):
    frontmatter, _ = _load_skill_md(skill_dir)
    assert _name(frontmatter, skill_dir) == skill_dir


def test_old_wiki_ingest_skill_is_gone_no_alias():
    """D1 takes a hard cut: the rename keeps no alias directory."""
    assert not os.path.exists(os.path.join(_SKILLS_DIR, "wiki-ingest")), (
        "skills/wiki-ingest/ must not exist — D1 renamed it to wiki-ingest-docs "
        "with no alias"
    )


# --------------------------------------------------------------------------- #
# D11: the docs sweep must leave --kind=auto so the .jsonl gate stays reachable.
# --------------------------------------------------------------------------- #
def test_ingest_docs_never_passes_kind():
    _, body = _load_skill_md("wiki-ingest-docs")
    # Any `--kind=` in a command position would disarm the gate; the only
    # permitted mentions are prose explaining why the flag is NOT passed.
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("uv run", "  --kind", "${KIND")) and "--kind" in stripped:
            pytest.fail(
                f"/wiki-ingest-docs must not pass --kind (D11); found: {stripped!r}")
    assert "${KIND:+--kind=" not in body, (
        "/wiki-ingest-docs must not pass --kind even conditionally (D11)")


# --------------------------------------------------------------------------- #
# D3/D13: /wiki-file fixes source, kind and cutoff; it takes no path argument.
# --------------------------------------------------------------------------- #
def test_wiki_file_fixes_kind_and_cutoff():
    _, body = _load_skill_md("wiki-file")
    assert "--kind=fe_b_prime" in body, (
        "/wiki-file must pin --kind=fe_b_prime (D3)")
    assert "--cutoff=last-user" in body, (
        "/wiki-file must pin --cutoff=last-user (D13) — the invocation turn is "
        "an instruction, not payload")


def test_wiki_file_sources_the_running_session_id():
    _, body = _load_skill_md("wiki-file")
    assert "${CLAUDE_SESSION_ID}" in body, (
        "/wiki-file must capture the running session id via the skill-template "
        "substitution rather than asking the user for it (D3)")


def test_wiki_file_has_no_glob_dispatch():
    """D3: fixing the source to one sid drops the glob-vs-file dispatch and the
    `enumerate` call entirely — that is why this SKILL is SHORTER."""
    _, body = _load_skill_md("wiki-file")
    assert "ingest enumerate" not in body, (
        "/wiki-file must not call the enumerate verb (single sid, no glob)")


def test_wiki_file_narrowing_channel_is_drop_only():
    """D14: the narrowing flow must state the drop-only rule the driver enforces
    by hash re-verification — a reader who edits text instead of deleting an
    entry gets a refused ingest."""
    _, body = _load_skill_md("wiki-file")
    assert "project-batch" in body, (
        "/wiki-file's narrowing flow must extract turns with the code-owned "
        "project-batch verb")
    assert "DELETE whole entries" in body, (
        "/wiki-file must state the drop-only rule for the narrowing channel (D14)")


def test_wiki_file_narrowing_protects_the_cutoff_anchor():
    """The one narrowing rule that is NOT code-enforced must be stated loudly.

    `--cutoff=last-user` anchors on the last user-role turn; in FLOW B that is
    the invocation entry the LLM was just handed. Deleting it re-anchors the
    cutoff onto the user's last real turn, silently dropping the content the run
    exists to file. The driver cannot detect this (a trimmed turn list is a
    legitimate shape), so the SKILL text is the only guard.

    The rule must be phrased from the last USER-ROLE entry, not from the end of
    the file: assistant narration is flushed mid-turn, so the literal last
    entries are normally `assistant` records that came after the invocation, and
    a "keep the last entry" rule would guard the wrong record.
    """
    _, body = _load_skill_md("wiki-file")
    assert "NEVER delete anything from the LAST USER-ROLE entry onward" in body, (
        "/wiki-file's narrowing flow must forbid deleting the cutoff anchor, "
        "anchored on the last USER-ROLE entry rather than the file's last entry")
    assert "not code-enforced" in body or "NOT code-enforced" in body, (
        "the anchor rule must be marked as prompt-only, so a reader knows the "
        "driver will not catch this mistake")


# --------------------------------------------------------------------------- #
# D5 pre-apply confirmation: the E2E found that NO write-bearing skill actually
# implemented it. The contract assumed one existed on three independent sites —
# `templates/SCHEMA.md` ("write_mode controls only whether a confirmation is
# shown before applying"), core-design D5, and `config_resolver`'s
# `write_mode_skips_confirmation()` helper — and `wiki-query` documents itself as
# EXEMPT from "the write_mode pre-apply confirmation", an exemption that is
# meaningless if the path never existed. Every skill nevertheless carried only
# the implicit half ("if implicit, announce that confirmation is skipped"), so
# the explicit branch silently did nothing.
#
# That is exactly the failure class a static test catches and a runtime test does
# not: the defect was an ABSENT instruction, invisible to any run that did not
# think to look for a question that never came.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("skill_dir", [
    "wiki-ingest-docs", "wiki-file", "wiki-ingest-sessions",
])
def test_write_skills_ask_before_applying_when_explicit(skill_dir):
    _, body = _load_skill_md(skill_dir)
    assert "AskUserQuestion" in body, (
        f"{skill_dir}: declares AskUserQuestion in allowed-tools but never tells "
        f"the orchestrator to USE it — this is the D5 gap the E2E surfaced")
    assert "write_mode" in body and "explicit" in body, (
        f"{skill_dir}: must branch on write_mode resolving to explicit")


@pytest.mark.parametrize("skill_dir", [
    "wiki-ingest-docs", "wiki-file", "wiki-ingest-sessions",
])
def test_write_skills_handle_the_unanswerable_confirmation(skill_dir):
    """A confirmation that cannot be answered must not strand the transaction.

    Print-mode / automated callers cannot reply to AskUserQuestion. Since the
    lock is held across the stages, a skill that simply waits reproduces the
    stranded-transaction class this project has already been bitten by. Each
    skill must name the non-interactive path and its escape (a deliberate
    `write_mode=implicit`).
    """
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
    """Batch loops ask ONCE, not per item (F-1).

    A per-item confirmation would fire N times on a glob sweep or a
    workspace-scope session set, which makes the gate unusable and trains the
    user to approve blindly. The question is hoisted to the batch head, where it
    gates the INPUT SET; the write allowlist and the per-item transaction remain
    the gates over the output.
    """
    _, body = _load_skill_md(skill_dir)
    assert loop_word in body.lower(), (
        f"{skill_dir}: the batch-head confirmation must describe the run as a "
        f"whole")
    assert "do NOT ask again" in body, (
        f"{skill_dir}: must forbid re-asking per item after the batch-head "
        f"confirmation")


def test_wiki_ingest_docs_prefixes_the_wiki_root_onto_enumerate_output():
    """`enumerate` returns wiki-root-relative paths; `begin` resolves against CWD.

    Both contracts are individually correct, so neither was changed. What was
    wrong is the loop instruction that fed one straight into the other: measured
    from a CWD other than the wiki root, `begin` fails with
    `source not readable (missing/dir/permission): <rel_path>`. The SKILL must
    prefix the resolved root.
    """
    _, body = _load_skill_md("wiki-ingest-docs")
    assert '"$WIKI_ROOT/$rel_path"' in body, (
        "the docs loop must pass the wiki-root-prefixed path to begin, not the "
        "bare enumerate rel_path")
