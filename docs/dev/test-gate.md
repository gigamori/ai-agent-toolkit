# Test knowledge gate — the standalone skills

Two mechanical rules over the test files under `skills/`, the command that enforces them,
and the scope decisions taken when they were introduced.

The reason they exist: prose in a test file is prose no run ever shows, and an agent that
holds only this checkout reads it as fact. A claim it cannot check from here is worse than
no claim at all. So facts about the world outside the codebase move to
[`test-constraints.md`](test-constraints.md), the intent behind a counter-intuitive
assertion moves into the assertion message where a failure prints it, and everything else
goes.

## The rules

- **R1** No comments and no docstrings in a test file, except the directive allowlist.
- **R2** No unreachable references, applied to every line — comments, docstrings, test
  names and assertion messages alike. R2 is what stops a reference from being smuggled
  into the relocation targets when a comment's content moves into a test name.

Directive allowlist, enumerated by grep over the tree rather than from memory:

| Directive | Where it appears |
|---|---|
| `#!/usr/bin/env bash` | first line of every executable `.sh` suite |
| `# noqa: E402` | the `sys.path` insert before importing the module under test |
| `# /// script` … `# ///` (PEP 723 block) | recognised, currently unused under `skills/` |

`# type: ignore[...]`, `# pragma` and `# ruff:` are recognised by the gate for the same
reason and do not occur today.

R1 has nothing else. A docstring is a comment for this policy's purposes: a runner prints
neither, so an agent reads both as unverifiable assertion, and neither becomes visible when
it is wrong. The language convention that reserves docstrings for API prose does not
survive contact with a test function, whose name is already its description.

Heredoc bodies in the `.sh` suites are fixture data, not source, and are left alone.

## Running it

```bash
uv run --no-project python tests/lint_test_knowledge.py
```

Exit `0` = gate holds, `1` = violation, `2` = the gate scanned zero files (a coverage
failure, not a clean tree). Run it before the suites; this repository has no CI and no
repo-level runner, so the gate is one more check run by the same hand that runs the tests:

```bash
cd skills/xml-wf/scripts && uv run --no-project python -m unittest discover -s tests
bash skills/mode-orchestrator/scripts/watchdog_test.sh
bash skills/mode-orchestrator/scripts/deny_scan_test.sh
bash skills/mode-orchestrator/scripts/pi_reply_test.sh
bash skills/mode-orchestrator/scripts/execution_profiles_test.sh
```

`execution_profiles_test.sh` is the profile gate for
`references/execution-profiles.md`. It validates the exact full profile contract
and runs one-edge mutation controls for table shape, mappings, unsupported
fields, and the no-fallback rule.

Adaptive routing also has an external evidence checker with a registered
`--self-test`. It is outside this tracked test gate because it reads immutable
real-child artifacts rather than runtime-distributed files. Before accepting an
adaptive routing change, run that registry from its evidence tree; temporary
probes do not replace its real artifacts or one-edge mutations.

Every run prints the number of files scanned, what it excluded and why, the remaining
allowlist count, and what its patterns cannot see.

R1's comment half and all of R2 are grep-level. **The docstring half is not.** A pattern
over triple-quoted strings cannot tell a docstring from a fixture string assigned to a
name, so the gate resolves docstrings at the positions the language defines — a module's
first statement, and the first statement in a `def` or `class` body — and leaves every
other string alone.

## Coverage

The gate unit is `skills/`. It walks the tree and takes every file named `test_*.py`,
`*_test.py`, `test_*.sh` or `*_test.sh`, so a new skill or a new suite inside one is picked
up without touching the gate — that is the invariant the unit was chosen for. 26 files are
in scope today.

Outside this gate, and carrying no assurance from it: `plugins/llm-wiki/tests/` and
`plugins/taskflow/tests/`, which have their own gates and their own constraints documents;
`plugins/role-mode/`; and `tests/test_tracked_path_hygiene.py` at the repository root,
which is a repo-wide lint rather than a skill's suite.

The gate's own blind spots, printed on every run: a citation phrased without one of its
citation verbs; a document name written bare, which a fixture spells exactly as a citation
would; a reference assembled at runtime from parts. Its counts are therefore lower bounds
for R2 and exact for R1.

## Scope decisions — migration record

Recorded because an undocumented scope call is the failure the scope rule exists to
prevent. Migrated 2026-08-24, covering the two skills of the `ai-workflow` project.

- **Ownership — both trees are canonical, not mirrors.** Settled before any edit, from the
  consumer registry rather than from the impression that a directory looks locally owned.
  The registry's only in-repo copies for these two skills are `modes/*.md` (a V-copy in
  `skills/mode-orchestrator/modes/` and an identity-follow snapshot in
  `skills/xml-wf/scripts/wfrun/modes/`); no test tree is a copy of anything. The skills are
  symlinked into the Claude Code and Pi skill directories, but that is one file reached
  from two harnesses, not a second copy that can drift, so nothing here had to be left to
  another repository to migrate.
- **In scope:** `skills/xml-wf/scripts/tests/*.py` (13 files) and
  `skills/mode-orchestrator/scripts/*_test.sh` (4 files). These assert contracts and are
  re-run whenever the code changes. That they are run by hand rather than by CI does not
  matter; there is no "automated" condition in the rule.
- **Out of scope, decided rather than missed:** `skills/xml-wf/scripts/evals/`
  (`prompt_smoke.py`, `adjudicator_smoke.py`) — opt-in sampling harnesses that call a real
  CLI and cost money, which the ordinary change loop does not run. They are instruments
  built to answer one question: what compliance rate a prompt achieves. Folding either into
  the normal loop puts it in scope that day. The gate names the exclusion on every run.
- **Not tests at all:** `deny_scan.sh`, `watchdog.sh` and `pi_reply.js` are runtime scripts
  that happen to sit beside their suites; `scripts/fixtures/` is fixture data.
- **No test was deleted.** The self-declared-inadequacy sweep returned only the four
  source-text guards in `test_cli_encoding.py`, which assert that the four launcher call
  sites still pass `encoding=`/`errors=`. They are a lint wearing a test's clothes — a
  cross-cutting invariant over call sites rather than a stand-in for a behavioural test of
  one unit — and they are the last standing check for a defect class this repository
  actively tracks (the `sweep-defect-class` skill ships an auditor for it). They stay, and
  their justification moved from a docstring into the assertion message. Every other
  rescued intent fit one assertion-message line, so no test became a deletion candidate.
- **Language.** Test names, assertion messages and the constraints document are English.

What the migration moved, measured over those 16 files: 541 comment lines and 136
docstrings to zero, and 97 unreachable references to zero. Facts about the world outside
the codebase went to [`test-constraints.md`](test-constraints.md) with the date each was
observed, taken from git history where the comment that carried it had none.

The gate is measured non-vacuous in all three halves: 774 violations against the
pre-migration versions of those files (541 R1 comments, 136 R1 docstrings, 97 R2
references) versus 0 against the current ones.

`tests/.knowledge-allowlist` freezes the eight test files of the other standalone skills —
`compact-cc-log`, `inspect-cc-log`, `inspect-pi-log`, `register-pi-tools`, `revert` and two
local-only skills under `skills/.private/`. They were in scope for the gate from day one
but outside this migration; 130 violations are frozen there. Migration is done when that
file is deleted, not when it stops growing.

## Not built, deliberately

- No id scheme linking a test to an entry in the constraints document.
- No dangling-id or orphan-entry check that fails the suite when that document drifts.

Both would convert documentation staleness into a build failure, which is heavier than the
comment maintenance they replace. The constraints document is allowed to rot: whoever next
reads a dead entry deletes it.
