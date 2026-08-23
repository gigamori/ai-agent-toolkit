# llm-wiki test knowledge gate (v0.1.4)

Two mechanical rules over the plugin's test suites, the command that enforces them, and the
scope decisions taken when they were introduced.

The reason they exist: a comment or a docstring in a test file is prose no run ever shows, and an
agent that holds only this checkout reads it as fact. A claim it cannot check from here is worse than no
claim at all. So facts about the world outside the codebase move to
[`test-constraints.md`](test-constraints.md), intent behind a counter-intuitive assertion moves
into the assertion message where a failure prints it, and everything else goes.

## The rules

- **R1** No comments and no docstrings in a test file, except the directive allowlist. In a
  runner's own configuration R1 still applies, and one comment form passes: the pointer template.
- **R2** No unreachable references, applied to every line — comments, docstrings, test names and
  assertion messages alike. R2 is what stops a reference from being smuggled into the relocation
  targets when a comment's content moves into a test name.

Directive allowlist, enumerated by grep over the tree rather than from memory:

| Directive | Where it appears |
|---|---|
| `# noqa: F401` | the dispatch-only import in `test_single_authority.py` |
| `#!/usr/bin/env ...` | none today; kept so an executable test does not trip the gate |
| `# type: ignore[...]` | none today; kept for stub classes standing in for stdlib objects |
| `# /// script` … `# ///` (PEP 723 block) | none today; kept for a test that declares inline dependencies |

A docstring is a comment for this gate's purposes and has no exemption. A runner prints neither,
so an agent reads both as unverifiable assertion and neither becomes visible when it is wrong; a
test function's name is already its description. No file under `tests/` holds a docstring,
`conftest.py` included.

### The pointer, in a runner's configuration

`conftest.py` asserts no contract of its own, so it is not a test — but the same agent reads it,
and a constraint written there misleads it exactly as one in a test would. R1 reaches it. One
comment form passes, a fixed template rather than a judgement call:

```
See <path>.md, "<heading>".
```

The path must exist; the gate resolves it against the plugin root and against the repository root.
The heading is never checked — checking it would turn a document edit into a build failure, which
is what Not built, deliberately rules out. Nothing else passes: no run instructions, no rationale,
no second sentence. A docstring in such a file is judged by the same template and fails it.

The pointer is permitted by R1 and matched by R2's citation pattern, so the two rules collide on
the only comment the policy allows here. An accepted pointer line is exempt from that one R2
pattern; every other R2 pattern still applies to it, and a pointer whose target is missing is not
accepted and so is not exempt.

`tests/conftest.py` carries no pointer today. Its former docstring narrated the three lines below
it, so it was not a constraint with a home to point at; and no pointer path could be written
there anyway, because the file is a byte-identical copy in the Pi package, where neither the
plugin-root nor the repository-root spelling of this document resolves.

The docstring half of R1 is not a grep — a triple-quoted string could equally be a fixture
assigned to a name. The gate reads the positions Python defines (a module's first statement, and
the first statement in a def or class body) through `ast`, and leaves every other string literal
alone.

A `#` inside a triple-quoted fixture string is content under test — a markdown heading in a
`SCHEMA.md` fixture, a command body in a generated driver script — never a comment. The gate
reads comments through Python's tokenizer, so it never confuses the two.

## Running it

```bash
uv run --no-project python plugins/llm-wiki/scripts/lint_test_knowledge.py
```

Exit `0` = gate holds, `1` = violation, `2` = the gate scanned zero files (a coverage failure,
not a clean tree). Run it before the suite; this repo has no CI and no repo-level runner, so the
gate is one more check run by the same hand that runs the tests:

```bash
uv run --no-project python plugins/llm-wiki/scripts/lint_test_knowledge.py && uv run pytest -q
```

(the `pytest` invocation runs from `plugins/llm-wiki/`, which owns the dev dependency group).

Every run prints the number of files it scanned, what it excluded and why, which files are held to
the pointer template, the remaining allowlist count, and what its patterns cannot see.

## Coverage

The gate unit is `plugins/llm-wiki`. It walks `tests/` and `scripts/tests/` recursively, so a new
file or a new subdirectory in either is picked up without touching the gate; `scripts/tests/`
holds no test file today and is scanned anyway so it cannot become an unchecked pocket. Other
packages in this repository (`plugins/taskflow/tests/`, which has
[its own gate](../../taskflow/docs/test-gate.md), `plugins/role-mode/tests/`,
`skills/*/scripts/tests/`, the top-level `tests/`) are **outside this gate** and carry no
assurance from it.

`conftest.py` is a runner configuration: R1 reaches it under the pointer template above, and R2
applies to it in full. The gate names it on every run.

The gate's own blind spots, printed on every run: a citation phrased without one of its citation
verbs and without a section sign; a bare gitignored path such as
`_projects/<p>/project-notes/x.md`, which a fixture spells exactly as a citation would; a spec
item referred to as `item<N>`, a spelling ordinary prose also uses; a reference assembled at
runtime from parts. Its counts are therefore lower bounds for R2 and exact for R1.

## Scope decisions — migration record, 2026-08-23

Recorded because an undocumented scope call is the failure the scope rule exists to prevent.

- **In scope:** every `.py` under `tests/` — 45 test files plus `conftest.py`. These assert
  contracts and are re-run whenever the code changes. That they are run by hand rather than by CI
  does not matter; there is no "automated" condition in the rule.
- **`conftest.py` reversed, 2026-08-24.** It was first ruled out of R1 as a configuration file
  that asserts nothing. The policy now reaches a runner's configuration under the pointer
  template, so R1 applies. Its docstring was judged not to be K1 — the filter asks whether an
  author rewriting the file from scratch would rediscover the fact, and a three-line `sys.path`
  insert makes it self-evident — so there was nothing to relocate and nothing to point at. The
  docstring was deleted and the file now carries no prose at all. It also named `plugins/llm-wiki/`
  as the inserted root, which is false in the Pi copy of the same bytes.
- **Out of scope:** nothing was excluded on judgement. The suite contains no probe, race harness,
  or one-off measurement script; `__pycache__` is a build artifact and is skipped mechanically.
- **Static source-text tests were kept, all of them.** `test_agents_tool_contract.py`,
  `test_single_authority.py`, `test_skill_command_surface.py`, `test_contract_public_api.py` and
  `test_cc_views_contract.py` verify source text rather than behaviour, which is the pattern that
  marks a test for deletion. Each one enforces a cross-cutting invariant over many files — the
  subagent tool grants, the single definition site of a symbol across the package, the user-facing
  command surface, the frozen public signatures, and the byte-equality of a vendored copy — rather
  than standing in for a behavioural test of one unit. They are lints wearing a test's clothes, and
  each is the last standing check for its class. They stay, and the ordinary `pytest` run is where
  they run.
- **One known-gap test was kept and re-labelled.**
  `test_boilerplate_does_not_eat_user_bullet_lines_is_known_gap` documents a gap in the code, not
  an inadequacy of the test: the assertion itself is exact. Its comment said the behaviour was
  pinned rather than endorsed, and that a change adding an end-of-injection terminator must fail
  here and be re-judged; that sentence moved into the assertion message, where a failure prints it.
- **No test was deleted.** 574 test functions before, 574 after; 645 cases pass, unchanged. No
  surviving test needed more than one assertion-message line to carry its intent.
- **Language.** Test names, assertion messages and the constraints document are English, matching
  the rest of the package. Japanese strings that remain are fixture data — real transcript text the
  projector is asserted against — not prose.

What the migration moved, measured over those 46 files: 1,379 comment lines to 1 (the single
allowlisted directive), and 949 docstring lines to 0. Facts about the world outside the codebase went
to [`test-constraints.md`](test-constraints.md) with the date each was
first recorded, taken from git history rather than from the comment that carried it.

No allowlist file exists. The migration completed in one pass, so there were no violations left to
freeze; the gate still reads `tests/.knowledge-allowlist` if one is ever added and prints the count
either way.

## Not built, deliberately

- No id scheme linking a test to an entry in the constraints document.
- No dangling-id or orphan-entry check that fails the suite when that document drifts.

Both would convert documentation staleness into a build failure, which is heavier than the comment
maintenance they replace. The constraints document is allowed to rot: whoever next reads a dead
entry deletes it.
