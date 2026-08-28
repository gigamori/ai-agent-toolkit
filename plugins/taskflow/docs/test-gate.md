# taskflow test knowledge gate (v0.2.9)

Two mechanical rules over `plugins/taskflow/tests/`, the command that enforces them, and the
scope decisions taken when they were introduced.

The reason they exist: prose in a test file is prose no run ever shows, and an agent that holds
only this checkout reads it as fact. A claim it cannot check from here is worse than no claim at
all. So facts about the world outside the codebase move to
[`test-constraints.md`](test-constraints.md), intent behind a counter-intuitive assertion moves
into the assertion message where a failure prints it, and everything else goes.

## The rules

- **R1** No comments and no docstrings in a test file, except the directive allowlist.
- **R2** No unreachable references, applied to every line — comments, docstrings, test names and
  assertion messages alike. R2 is what stops a reference from being smuggled into the relocation
  targets when a comment's content moves into a test name.

Directive allowlist, enumerated by grep over the tree rather than from memory:

| Directive | Where it appears |
|---|---|
| `#!/usr/bin/env python3`, `#!/usr/bin/env bash` | first line of every executable test |
| `# noqa: E402` | the `sys.path` insert before importing the module under test |
| `# type: ignore[...]` | stub classes standing in for stdlib objects |
| `# /// script` … `# ///` (PEP 723 block) | tests that declare inline dependencies |

R1 has nothing else. A docstring is a comment for this policy's purposes: a runner prints neither,
so an agent reads both as unverifiable assertion, and neither becomes visible when it is wrong.
The language convention that reserves docstrings for API prose does not survive contact with a
test function, whose name is already its description. A test configuration file (`conftest.py`,
`vitest.config.*`) is not an exemption either — Scope simply never makes it a test file, since it
asserts no contract. None exist under `tests/` today; a constraint written in one would still be
K1 and would still leave for the constraints document.

Heredoc bodies in the `.sh` suites are fixture data, not source. A markdown `# heading` inside one
is content under test and is never treated as a comment.

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
behavioural test of one unit — it stays where it is and runs from here, and prints its own
detector limits at the end of its report.

R1's comment half and all of R2 are grep-level. **The docstring half is not.** A pattern over
triple-quoted strings cannot tell a docstring from a fixture string assigned to a name, so the
gate resolves docstrings at the positions the language defines — a module's first statement, and
the first statement in a `def` or `class` body — and leaves every other string alone. Getting that
wrong permissively leaves docstrings unchecked; getting it wrong strictly flags fixtures and the
gate gets switched off. Both directions were measured: fixture strings at module scope and inside
a function body are not flagged, and a real docstring is.

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

## Scope decisions — migration record

Recorded because an undocumented scope call is the failure the scope rule exists to prevent.
Migrated 2026-08-23; extended to docstrings 2026-08-24 when the policy dropped that exemption.

- **Ownership — this tree is canonical, not a mirror.** Settled before any edit, from the
  consumer registry rather than from the impression that the directory looks locally owned. Both
  taskflow consumers are `Type: P` (parallel implementation), and `drift_check.py` reports
  `SKIP [P] — parallel implementation — no shared files to compare` for each. A parallel
  implementation is the opposite of a mirrored tree: two harnesses, each with its own tests, no
  shared files, and divergence is expected. Each side migrates its own suite. Nothing here was
  copied from elsewhere, so nothing here had to be left to another repository to migrate.
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
  inadequacy. Every rescued K2 sentence fit one assertion message, so no test became a deletion
  candidate under that clause either.
- **Language.** Test names, assertion messages and the constraints document are English. `打止め`
  stays: it is the project's own term for the give-up record, used in
  `hooks/session_progress_capture.py` and in `architecture.md`, not stray prose.

What the migration moved, measured over those 55 files: 1,903 comment lines to 144 (all of them
allowlisted directives), and 1,273 docstring lines across 100 docstrings to zero. Facts about the
world outside the codebase went to [`test-constraints.md`](test-constraints.md) with the date each
was first recorded, taken from git history rather than from the comment that carried it.

The gate is measured non-vacuous in both halves: 2,499 violations against the pre-migration tree
versus 0 against the current one, and 8 docstring detections against the pre-amendment versions of
the five files that still had them.

No allowlist file exists. The migration completed in one pass, so there were no violations left to
freeze; the gate still reads `tests/.knowledge-allowlist` if one is ever added and prints the
count either way.

## Not built, deliberately

- No id scheme linking a test to an entry in the constraints document.
- No dangling-id or orphan-entry check that fails the suite when that document drifts.

Both would convert documentation staleness into a build failure, which is heavier than the comment
maintenance they replace. The constraints document is allowed to rot: whoever next reads a dead
entry deletes it.
