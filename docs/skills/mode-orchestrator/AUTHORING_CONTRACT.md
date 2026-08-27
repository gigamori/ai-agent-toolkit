# Maintenance contract — mode-orchestrator

> Positioning: this is **maintenance context, not execution context** — not needed to *run* mode-orchestrator, needed to *maintain* it. `SKILL.md` is behaviorally accurate for execution; this file records the obligations an editor of the skill must satisfy that `SKILL.md` does not (and should not) state.

## Dual-harness integrity

The skill runs unmodified on both Claude Code and Pi: `SKILL.md` Step -1 probes the harness and reads `references/harness-cc.md` or `references/harness-pi.md` accordingly, so the two facets share one entry point but diverge in delegation method, time-bound mechanics, and the mechanical denial check (Claude Code only — Pi has no permission layer).

Because both facets live in one skill and Pi loads it via a symlink into `~/.pi/agent/skills/` (not a copy), there is no propagation step to remember — the change reaches Pi automatically. What is NOT automatic is correctness on both sides:

- Before landing a change to `SKILL.md`'s Execution steps, `references/execution-profiles.md`, or anything a harness reference file governs (delegation method, model override, time-bound signaling, the denial check), read BOTH `references/harness-cc.md` and `references/harness-pi.md` and judge explicitly whether each still holds.
- A change scoped to one harness reference (e.g. a Claude Code watchdog fix) needs no edit to the other — but state that scoping decision, don't leave it implicit.
- Re-run `scripts/watchdog_test.sh`, `scripts/deny_scan_test.sh` and `scripts/pi_reply_test.sh` after touching any of those scripts; all three are self-contained (they build their own fake session logs, or read committed fixtures) so this costs nothing to skip accidentally — don't. `pi_reply_test.sh` additionally guards a Pi-side contract: its `fixtures/*.jsonl` are carved from a real `pi -p --mode json` stream, so if a pi version bump changes that stream's shape, re-carve the fixtures from a fresh capture rather than editing them to match the extractor.
- Run `scripts/execution_profiles_test.sh` after changing it or `references/execution-profiles.md`. It validates the profile's **shape**, not its content — bound models are freely editable — via one-edge mutation controls on: the title line, blank-line positions, the exact header, the exact divider, exactly three effort rows in order `basic`/`pro`/`ultra`, exactly four columns, each CC/Pi model cell being a single bare token (no comma, no whitespace), the absence of a `provider`/`vendor`/`thinking`/`candidate` field, and the no-fallback rule being present and terminal. **Abandoned, not replaced**: a CC/Pi cell swap and a wrong-but-well-formed model name in a cell are both unpinnable now that cell values are freely bound; no mechanical successor exists, and that class is covered by review only.
- For adaptive routing changes, run the external evidence checker's `--self-test` from its evidence tree before accepting E2E evidence. Its registered immutable real-child artifacts and one-edge mutations cover adaptive/legacy selection, profile capture, planned/actual override separation, keyed command/raw/session/child correlation, dynamic lineage, amendment graphs, and provider identity. The tracked runtime does not load this checker; do not replace the registry with a temporary script.

### Re-carving `scripts/fixtures/` — scrub the captured machine paths

A fresh `pi -p --mode json` capture is a recording of a real session on a real machine, so it carries that machine's absolute paths: the session record's `cwd`, and the `path` argument of every file-reading tool call. Those are machine-local absolute paths, which must not enter a tracked file — the rule lives in the machine-global instructions file (`git_universal` block), not in this repository, and its sanctioned substitute is a generic `/path/to/...`. So the re-carve instructed above has a scrub step, and it is not optional:

- **Scrub by value substitution, keeping every key and nesting level.** The fidelity claim recorded in [`docs/dev/test-constraints.md`](../../dev/test-constraints.md) is about the measured *shape* — which keys exist and how they nest — not about literal values, so replacing a value leaves it intact. The fixtures already carry two worked precedents: `"cwd": "/path/to/workspace"` on line 1 of all five, and `"path": "/path/to/agent/skills/mode-orchestrator/SKILL.md"` on line 2 of the four that have a tool call.
- **Scrub every line, not just the first.** Line 2 was missed the first time round and shipped the capturing user's real home directory for months, while line 1 was already clean — a scrub that stops at the session record looks finished and is not.
- **Do the substitution in an editor or a script, never in a shell one-liner.** Inside the JSON the separators are backslash-doubled, and a quoting layer collapses `\\` to `\` silently, so a `sed`/`grep` pass can search for the wrong string and report the fixtures clean when they are not (measured 2026-08-23).
- **Verify with the repo-wide lint, not by eye.** `uv run --no-project python tests/test_tracked_path_hygiene.py` from the checkout root reads every tracked file in Python and flags any path naming this machine. Eyeballing does not work here: each leak sits inside a single multi-kilobyte JSON line.

## Test knowledge gate

`scripts/watchdog_test.sh`, `scripts/deny_scan_test.sh` and `scripts/pi_reply_test.sh` carry
no comments — they are read by agents that hold only this checkout, and prose no run ever
prints reads to them as fact. The two rules, the directive allowlist and the migration
record live in [`docs/dev/test-gate.md`](../../dev/test-gate.md); do not restate them here.

What an editor of this skill has to do:

- Run `uv run --no-project python tests/lint_test_knowledge.py` from the checkout root
  before the suites. Exit 2 means it scanned nothing, which is a coverage failure and not a
  clean tree.
- A fact about the world outside this repository — the shape of a captured pi stream, how
  Claude Code serializes a permission denial — goes to
  [`docs/dev/test-constraints.md`](../../dev/test-constraints.md) with the date it was
  observed, never into the suite. That document is its only home.
- What a case asserts belongs in the name it is registered under (`expect "…"`,
  `check "…"`), which the run prints; the watchdog suite's threshold cases are the worked
  example.
- `deny_scan.sh`, `watchdog.sh` and `pi_reply.js` are runtime scripts, not test files, and
  the gate does not read them.

## Relationship to xml-wf

mode-orchestrator and `skills/xml-wf/` are separate products that independently reimplement the same idea — running an isolated, mode-tagged turn per step — over different substrates (mode-orchestrator: one subagent call per todolist step; xml-wf: `wfrun`'s deterministic Python control flow over `<step>` elements). Neither is canonical for the other; there is no shared file, and — with the exceptions below — no propagation obligation between them.

When a change here alters step-execution semantics in a way that looks generally useful (e.g. how failures escalate to a debug turn, how a stale completion signal is discarded, how the harness/model resolution works), consider whether `skills/xml-wf/` would benefit from the same idea and note it for the xml-wf maintainer — a suggestion to evaluate, not an obligation to port.

## Decision contract (aligned with xml-wf)

The decision loop (`needs-decision`, `SKILL.md` step 7) shares a **deliberately aligned user-facing contract** with xml-wf's `DECISION:` channel: the decider vocabulary (`--decider human|llm`, default `human`), the cap semantics (inserted llm-decider turns only — human decisions never consume it), and the escalation grounds (irreversible / outward-facing / goal-changing → human). The alignment exists because users move between the two skills, and defaults that disagree tax every unattended run's post-mortem.

The implementations are independent (this skill enforces the contract as prompt contract in `SKILL.md`; xml-wf enforces it in `wfrun` code) and some divergences are **deliberate** — e.g. this skill permits a scoped read of a turn's `## Decision request` section while xml-wf's run-llm orchestrator never reads a request body; this skill has amend-plan, xml-wf has continuation forms (a)/(b). Do not "fix" those toward each other.

When editing either side's decider vocabulary, defaults, cap semantics, or escalation grounds, the `generate-sibling-handoff` registry (`references/families.md`, **decision-contract** family) owns the propagation trigger and the consumer list — consult it before landing the change, and do not duplicate its content here. Precedent: the cap-scope alignment landed as P3 (`9168a52`).

## Model-tier contract (aligned with xml-wf)

`skills/xml-wf/references/build.md` § Model selection declares its tier vocabulary (`basic`/`pro`/`ultra`) canonical for both this skill and xml-wf, so the three tier NAMES must not drift between them — the concrete model each tier BINDS to is a deliberate exception: bindings are independent per skill (`execution-profiles.md` here, `model_map.json` there) and are expected to differ.

The implementations are independent (this skill's `execution-profiles.md` is prompt-read only, enforced by its own shape gate, `scripts/execution_profiles_test.sh`; xml-wf enforces the vocabulary in code — `modelmap.py`'s `CANONICAL_MODELS`, `lint.py`'s `model-not-canonical`/`model-legacy-name` checks). Both sides carry the renamed `basic`/`pro`/`ultra` vocabulary as of 2026-08-28.

When editing the tier vocabulary, the approved-candidates-per-layer lists, or the measured floors, the `generate-sibling-handoff` registry (`references/families.md`, **model-tier** family) owns the propagation trigger and the consumer list — consult it before landing the change, and do not duplicate its content here.

## Mode prompt fragments

`modes/` is a snapshot of the `role-mode` plugin's canonical mode bodies (`plugins/role-mode/prompts/modes/`), synced manually and currently text-identical to canonical (EOL differs only). Its propagation obligations, sync procedure, and drift detection are owned by the `generate-sibling-handoff` skill's registry (`references/families.md`, `role-mode` family) — see that registry before touching `modes/`, and do not duplicate its content here.

## Measuring a prompt change on the Pi facet — a git worktree is not the lever

A change to `modes/` or to `SKILL.md`'s injected text can only be judged by measurement (prompt-layer compliance is probabilistic). The obvious way to build the before-arm — check the old commit out into a worktree and run there — **does not work on the Pi facet, and fails silently**.

Pi resolves skills from exactly two places: `<agentDir>/skills` and `<cwd>/.pi/skills` (`packages/coding-agent/src/core/skills.ts`, `userSkillsDir` / `projectSkillsDir`; the user copy wins a name collision). A repository's own `skills/` directory is not one of them. In this environment the agent-dir entry for this skill is a **symlink to the main checkout**, so every run injects whatever the main checkout currently holds no matter which commit the worktree is pinned to.

Measured 2026-08-16: an arm pinned to the pre-change commit injected the **post**-change wording — 124 verbatim occurrences in the delegation `raw/*.jsonl`, zero occurrences of the wording that worktree actually contained. The arm returned the same zero-contamination count as the after-arm, from a lever that was never pulled.

The obligations that follow:

- **Pick the arm's orchestrator model from the approved layer list, and never from the other layer.** The candidates live in xml-wf's `references/build.md` (§ Model selection) — canonical there so the two skills cannot drift; do not restate the names here. For this facet the consequence is specific: `gemini-3.5-flash-lite` is a **measurement-layer** candidate and is **not** admissible as the orchestrator driving a run. Measured 2026-08-17, an arm on it set no P2 timeout on any of its 3 delegations and dropped `MODE_ORCH_DEPTH`, `--no-skills`, the `raw/` capture, and the extractor call. An orchestrator that cannot follow `harness-pi.md` measures itself, not the prompt change under test — and its zeros are indistinguishable from a real regression.
- **Switch the agent dir, not the working tree.** Point `PI_CODING_AGENT_DIR` (`config.ts`, `ENV_AGENT_DIR`) at a shadow directory that symlinks every entry of the real agent dir except the one skill under test, which points at the target worktree. Symlink rather than copy so no credential file (`auth.json`) is duplicated into a temporary location.
- **The shadow dir now reaches the extractor too.** `harness-pi.md`'s P0 resolves `pi_reply.js` through `PI_CODING_AGENT_DIR` rather than a hard-coded `~/.pi/agent`, so an arm pointed at a shadow dir runs that arm's extractor. Before 2026-08-17 it did not: the lever moved the skill text while the extractor stayed pinned to the real agent dir, which is a silent cross-arm leak of exactly the kind this section exists to catch.
- **Verify the injected text after the run, from the run's own artifacts.** Grep the delegation `raw/*.jsonl` for the wording verbatim and report the per-arm counts. `drift_check.py` cannot serve here: it compares copies *inside the repository* and says nothing about what a running agent loaded. A zero that has not passed this gate is inconclusive, not a pass.

This also explains a class of confound in the recorded Pi-facet runs: when a commit lands mid-session, the runs before and after it were executed against different skill text even though nothing about the harness changed. Runs are not a single skill version unless the agent dir was pinned.
