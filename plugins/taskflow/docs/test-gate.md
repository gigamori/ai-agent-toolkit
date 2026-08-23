# taskflow test knowledge gate (v0.2.8)

Two mechanical rules over `plugins/taskflow/tests/`, the command that enforces them, and the
scope decisions taken when they were introduced.

The reason they exist: a comment in a test file is prose no run ever shows, and an agent that
holds only this checkout reads it as fact. A claim it cannot check from here is worse than no
claim at all. So facts about the world outside the codebase move to
[`test-constraints.md`](test-constraints.md), intent behind a counter-intuitive assertion moves
into the assertion message where a failure prints it, and everything else goes.

## The rules

- **R1** No comments in a test file, except the directive allowlist.
- **R2** No unreachable references, applied to every line — comments, test names, assertion
  messages and docstrings alike. R2 is what stops a reference from being smuggled into the
  relocation targets when a comment's content moves into a test name.

Directive allowlist, enumerated by grep over the tree rather than from memory:

| Directive | Where it appears |
|---|---|
| `#!/usr/bin/env python3`, `#!/usr/bin/env bash` | first line of every executable test |
| `# noqa: E402` | the `sys.path` insert before importing the module under test |
| `# type: ignore[...]` | stub classes standing in for stdlib objects |
| `# /// script` … `# ///` (PEP 723 block) | tests that declare inline dependencies |

Docstrings are not comments and are exempt from R1, because banning them fights Python's own
convention. Their text is still subject to R2 and to deletion as narration: what may remain is a
single sentence carrying intent that will not fit the test name.

Heredoc bodies in the `.sh` suites are fixture data, not source. A markdown `# heading` inside
one is content under test and is never treated as a comment.

## Running it

```bash
uv run --no-project python plugins/taskflow/scripts/lint_test_knowledge.py
```

Exit `0` = gate holds, `1` = violation, `2` = the gate scanned zero files (a coverage failure,
not a clean tree). Run it before the suites; this repo has no CI and no repo-level runner, so the
gate is one more check run by the same hand that runs the tests.

Every run prints the number of files it scanned, what it excluded and why, the remaining
allowlist count, and what its patterns cannot see. It also invokes the sandbox-guard ratchet
(`tests/test_sandbox_guard_ratchet.py`), which is a lint over many files rather than a
behavioural test of one unit — it stays where it is and runs from here.

## Coverage

The gate unit is `plugins/taskflow`. It walks `tests/` recursively, so a new file or a new
subdirectory is picked up without touching the gate. Other packages in this repository
(`plugins/llm-wiki/tests/`, `plugins/role-mode/tests/`, `skills/*/scripts/tests/`, the top-level
`tests/`) are **outside this gate** and carry no assurance from it.

The gate's own blind spots, printed on every run: a citation phrased without one of its citation
verbs and without a section sign; a bare gitignored path such as
`_projects/<p>/project-notes/x.md`, which a fixture spells exactly as a citation would; a
reference assembled at runtime from parts. Its counts are therefore lower bounds for R2 and exact
for R1.

## Scope decisions — migration record, 2026-08-23

Recorded because an undocumented scope call is the failure the scope rule exists to prevent.

- **In scope:** every `.py` and `.sh` directly under `tests/` — 55 files. These assert contracts
  and are re-run whenever the code changes. That they are run by hand rather than by CI does not
  matter; there is no "automated" condition in the rule.
- **Out of scope:** `tests/race/lockedrebuild/` (`race.py`, `setup.py`) — a race harness built to
  answer one question, which the ordinary change loop does not run. `tests/fixtures/` holds
  fixture data, not test code. Both are named in the gate's output so a reader sees they were
  decided, not missed. Folding either into the normal loop puts it in scope that day.
- **`capture_paths.sh`** is a sourced helper with no shebang and no invocation, but it lives among
  the suites and is read alongside them, so it stays in scope. It carried a header block; that
  header is gone like every other.
- **`test_sandbox_guard_ratchet.py`** declares that it is a presence check on source text and
  proves nothing about execution. That is the pattern that marks a test for deletion, but this one
  enforces a cross-cutting invariant over many files rather than standing in for a behavioural
  test of one unit — a lint wearing a test's clothes — and it is the last standing check for the
  state-directory hazard class. It is kept, and the gate runs it. Its detector limits moved from a
  header comment into its printed report, where they are on the execution path.
- **No test was deleted.** The self-declared-inadequacy sweep returned only non-vacuity controls
  (arms that prove another arm discriminates) and one runtime note reporting that a specific
  control is vacuous while pointing at the arm that is not. Neither is a test declaring its own
  inadequacy. No surviving test needed more than one assertion-message line to carry its intent.
- **Language.** Test names, assertion messages and the constraints document are English. `打止め`
  stays: it is the project's own term for the give-up record, used in
  `hooks/session_progress_capture.py` and in `architecture.md`, not stray prose.

What the migration moved, measured over those 55 files: 1,903 comment lines to 144 (all of them
allowlisted directives), and 1,273 docstring lines to 42. Facts about the world outside the
codebase went to [`test-constraints.md`](test-constraints.md) with the date each was first
recorded, taken from git history rather than from the comment that carried it.

No allowlist file exists. The migration completed in one pass, so there were no violations left to
freeze; the gate still reads `tests/.knowledge-allowlist` if one is ever added and prints the
count either way.

## Not built, deliberately

- No id scheme linking a test to an entry in the constraints document.
- No dangling-id or orphan-entry check that fails the suite when that document drifts.

Both would convert documentation staleness into a build failure, which is heavier than the comment
maintenance they replace. The constraints document is allowed to rot: whoever next reads a dead
entry deletes it.
