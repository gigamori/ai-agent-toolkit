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
- **R3** Every `.sh` suite under `tests/` (`capture_paths.sh` excluded — it is a sourced snippet
  with no `PASS`/`FAIL` counter of its own) reaches an `exit 1` when its own `$FAIL` counter is
  non-zero: either inside the function its `trap ... EXIT` installs, or, for the one suite with no
  trap, as a tail guard. Enforced by `tests/test_sh_fail_propagation.py`, run as one more suite
  rather than wired into `lint_test_knowledge.py`'s own invocation — a class lint (a) over all 23
  in-scope files in one pass, plus a RED/GREEN control (b) that mirrors `hooks/` and `prompts/`
  into a temp tree, copies `test_guidelines_reminder_mode.sh` in at matching depth, and asserts the
  unperturbed copy exits 0 while a one-character perturbation to one of its own assertions makes it
  exit 1. R3's own detector limits, printed in its report: the class lint (a) is a source-text
  guard — it cannot see a trailer present but unreachable (dead code after an earlier `exit`), a
  suite that never increments `$FAIL` and so legitimately exits 0, or a suite whose failures are
  reported through a differently-named counter; it is a lower bound on violations. The RED/GREEN
  control (b) covers exactly the one file it mirrors — the other 22 in-scope suites are covered by
  the class lint (a) only, and that is a stated limit, not an implied one.

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

## Router role-division gate additions — scope calls and uncovered configurations

Six gates added or extended over the four retired router invariants and the new
progress-digest hook, each demonstrated with a recorded RED before being counted as a
gate. Scope calls, and what each cannot see:

- **`test_router_pointer_contract.py`** gained a whole-file (not apply-template-only)
  absence check for the retired fields/markers and a presence check for the
  `[tasks-rel:` pointer. It is a text match over `agents/project-router.md` alone: it
  cannot see whether a router run actually emits the pointer, whether the pointer names
  the right tasks, or whether the main agent honours it.
- **`test_session_init_bootstrap.py`** gained four fixture-driven arms (A-D) plus one
  literal anchor (`INLINE_BUDGET_U16 == 10000`) tying the budget constant to the pinned
  harness version. Not covered: checkout-path length (a longer checkout raises every
  measured total; the fixtures use a temp path, not the real one), row lengths beyond
  the fixtures' own (the Task column is an uncapped H1; arm B exercises the guard, not
  a bound), the `notes_notice` layer's unbounded exception-message arm (`str(e)`, no
  cap), the manifest reminder variant (untested; safe by monotonicity, it is smaller
  than the full variant the arms use), and any `inject_every_turn: true` project (a
  supported configuration where the digest is never built, because that branch is
  always the heavy-turn branch). Arm A was under-sized against the row-cap decision's
  own worst arm until a later review of it found: its fixture carried the digest and the full
  guidelines reminder only, omitting the project-rules manifest block, the qmd-degrade
  notice, and a notes-push block sized to the worst known case, so it measured 7,855 u16
  (headroom 2,145) against the plan's declared worst-arm margin of 370 — the canary the
  `IN_PROGRESS_ROWS_MAX = 5` decision is staked on fired roughly 1,775 u16 late. Arm A now
  seeds a three-heading `rules.md` (manifest variant, `project_rules_indexed` pre-set so
  the turn stays ordinary), an 8-row notes-push fixture sized to the survey's synthetic
  maximum, and a nonexistent `TASKFLOW_QMD_BIN` against a `project-notes/.qmd/` directory
  to force the degrade notice — the three blocks the worst arm counts. Measured after the
  resize: total 9,638 u16, headroom 362, against the plan's worst-arm figure of
  9,630/370 (the ~8 u16 spread is the fixture's own project name and temp `state_file=`
  path, both checkout-local).
- **`test_guidelines_reminder_mode.sh`** is unchanged; its existing RED/GREEN control
  in `test_sh_fail_propagation.py` already exercises the class this router change
  touches. Reproducing that control by hand once (`ROUTER:|RESPONSE` to
  `ROUTEQ:|RESPONSE`) surfaced a reproduction hazard worth recording: a naive
  whole-line `sed` substitution perturbs BOTH occurrences of the pattern (one per
  reminder file's own grep line) and the gate stays vacuously green, because both
  extractions then agree on the same (wrong) value. The suite's own Python control
  perturbs only the first occurrence in the file (`str.replace(old, new, 1)`); a
  hand reproduction has to match that exactly or it proves nothing.
- **`test_freshsession_mechanisms.sh`** gained two discovery arms and one negative
  assertion on the existing non-discovery arm. A whole-payload substring match for
  `discovery=1` is unsound in both directions here: the reminder's own `ROUTER:` line
  and `project_routing.md`'s own prose both contain the literal `discovery=1` /
  `project-notes (index rows):` as English description of the mechanism, not as the
  field or block itself, on turns where those files are injected. Every discovery
  assertion in this suite is scoped to the decoded header line (everything before the
  first real blank line in the JSON-decoded `additionalContext`), and the
  notes-push-absence assertion is scoped to the render footer literal
  (`project-notes matched:`), which is safe on THIS fixture only because the one prose
  occurrence of that literal (`prompts/notes_guidelines.md`) reaches the payload solely
  via `inject_guidelines_full`, false here because `guidelines_loaded` is pre-seeded
  true; a fresh-session discovery fixture would need the same care the `discovery=1`
  checks already take. The assertion discriminates even on a fixture whose single
  project-notes row matches nothing: `notes_push.py::render` (:545-548) emits the
  `HEADER` and the `M of N (K shown, S strong)` footer unconditionally whenever
  `project-notes/index.md` exists, `M`/`K`/`S` simply reading `0` when nothing matched.
  Not covered: whether the model actually invokes the router on a discovery turn
  (prompt-level, unmeasured here).
- **`test_carry_only_backstop.sh`** gained four arms over the exec-bind carry's
  interaction with the `[tasks-rel:]` pointer and `EXEC_PARSE_WINDOW`. Two of the four
  are self-contained vacuity controls (a bare pointer binds nothing; a bare carry does
  bind), so the pair is what demonstrates the co-existence and displacement arms are
  discriminating rather than trivially true. Not covered: how often a real assistant
  response places a long `[tasks-rel:]` line ahead of its `[tasks:]` line in production
  (prompt-level, unmeasured here).
- **`test_progress_session_header_contract.py`** (widened while landing this change to scan the
  `[Progress Session]` f-string's full continuation span, not just its first physical
  line) matches the pattern `(\w+)=\{` against that span to name
  the emitter's fields. `discovery=1` reaches the header through the separate
  `{discovery_field}` variable, whose content is a string literal rather than a
  `key={value}` token, so `emitted_fields()` can never name `discovery`. Nothing fails
  today because no document quotes `discovery=<...>` inside the `[Progress Session]`
  pattern; the day one does, the gate reports it as stale rather than catching the
  actual omission.
- **`test_progress_view_cap.py`** gained a byte-identity check between
  `hooks/progress_digest.py` and `scripts/view_progress.py`'s `TABLE_BEGIN`,
  `TABLE_END`, `SECTION_RE.pattern` and `SEPARATOR_RE.pattern`. It compares exactly two
  of the progress-table parsing's four copies: `check_progress.py::parse_progress_table_rows`
  and `rebuild_progress.py::render_section` build the same shape inline (a literal
  `re.match` and f-string rows, respectively) and are not comparable by symbol. Nothing
  enforces those two against the other two; a `LOCKSTEP` comment is the only remaining
  link.

## Not built, deliberately

- No id scheme linking a test to an entry in the constraints document.
- No dangling-id or orphan-entry check that fails the suite when that document drifts.

Both would convert documentation staleness into a build failure, which is heavier than the comment
maintenance they replace. The constraints document is allowed to rot: whoever next reads a dead
entry deletes it.
