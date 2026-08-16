# Maintenance contract — mode-orchestrator

> Positioning: this is **maintenance context, not execution context** — not needed to *run* mode-orchestrator, needed to *maintain* it. `SKILL.md` is behaviorally accurate for execution; this file records the obligations an editor of the skill must satisfy that `SKILL.md` does not (and should not) state.

## Dual-harness integrity

The skill runs unmodified on both Claude Code and Pi: `SKILL.md` Step -1 probes the harness and reads `references/harness-cc.md` or `references/harness-pi.md` accordingly, so the two facets share one entry point but diverge in delegation method, time-bound mechanics, and the mechanical denial check (Claude Code only — Pi has no permission layer).

Because both facets live in one skill and Pi loads it via a symlink into `~/.pi/agent/skills/` (not a copy), there is no propagation step to remember — the change reaches Pi automatically. What is NOT automatic is correctness on both sides:

- Before landing a change to `SKILL.md`'s Execution steps, or to anything a harness reference file governs (delegation method, time-bound signaling, the denial check), read BOTH `references/harness-cc.md` and `references/harness-pi.md` and judge explicitly whether each still holds.
- A change scoped to one harness reference (e.g. a Claude Code watchdog fix) needs no edit to the other — but state that scoping decision, don't leave it implicit.
- Re-run `scripts/watchdog_test.sh`, `scripts/deny_scan_test.sh` and `scripts/pi_reply_test.sh` after touching any of those scripts; all three are self-contained (they build their own fake session logs, or read committed fixtures) so this costs nothing to skip accidentally — don't. `pi_reply_test.sh` additionally guards a Pi-side contract: its `fixtures/*.jsonl` are carved from a real `pi -p --mode json` stream, so if a pi version bump changes that stream's shape, re-carve the fixtures from a fresh capture rather than editing them to match the extractor.

## Relationship to xml-wf

mode-orchestrator and `skills/xml-wf/` are separate products that independently reimplement the same idea — running an isolated, mode-tagged turn per step — over different substrates (mode-orchestrator: one subagent call per todolist step; xml-wf: `wfrun`'s deterministic Python control flow over `<step>` elements). Neither is canonical for the other; there is no shared file, and — with the one exception below — no propagation obligation between them.

When a change here alters step-execution semantics in a way that looks generally useful (e.g. how failures escalate to a debug turn, how a stale completion signal is discarded, how the harness/model resolution works), consider whether `skills/xml-wf/` would benefit from the same idea and note it for the xml-wf maintainer — a suggestion to evaluate, not an obligation to port.

## Decision contract (aligned with xml-wf — the one propagation obligation)

The decision loop (`needs-decision`, `SKILL.md` step 7) shares a **deliberately aligned user-facing contract** with xml-wf's `DECISION:` channel: the decider vocabulary (`--decider human|llm`, default `human`), the cap semantics (inserted llm-decider turns only — human decisions never consume it), and the escalation grounds (irreversible / outward-facing / goal-changing → human). The alignment exists because users move between the two skills, and defaults that disagree tax every unattended run's post-mortem.

The implementations are independent (this skill enforces the contract as prompt contract in `SKILL.md`; xml-wf enforces it in `wfrun` code) and some divergences are **deliberate** — e.g. this skill permits a scoped read of a turn's `## Decision request` section while xml-wf's run-llm orchestrator never reads a request body; this skill has amend-plan, xml-wf has continuation forms (a)/(b). Do not "fix" those toward each other.

When editing either side's decider vocabulary, defaults, cap semantics, or escalation grounds, the `generate-sibling-handoff` registry (`references/families.md`, **decision-contract** family) owns the propagation trigger and the consumer list — consult it before landing the change, and do not duplicate its content here. Precedent: the cap-scope alignment landed as P3 (`9168a52`).

## Mode prompt fragments

`modes/` is a snapshot of the `role-mode` plugin's canonical mode bodies (`plugins/role-mode/prompts/modes/`), synced manually and currently text-identical to canonical (EOL differs only). Its propagation obligations, sync procedure, and drift detection are owned by the `generate-sibling-handoff` skill's registry (`references/families.md`, `role-mode` family) — see that registry before touching `modes/`, and do not duplicate its content here.

## Measuring a prompt change on the Pi facet — a git worktree is not the lever

A change to `modes/` or to `SKILL.md`'s injected text can only be judged by measurement (prompt-layer compliance is probabilistic). The obvious way to build the before-arm — check the old commit out into a worktree and run there — **does not work on the Pi facet, and fails silently**.

Pi resolves skills from exactly two places: `<agentDir>/skills` and `<cwd>/.pi/skills` (`packages/coding-agent/src/core/skills.ts`, `userSkillsDir` / `projectSkillsDir`; the user copy wins a name collision). A repository's own `skills/` directory is not one of them. In this environment the agent-dir entry for this skill is a **symlink to the main checkout**, so every run injects whatever the main checkout currently holds no matter which commit the worktree is pinned to.

Measured 2026-08-16: an arm pinned to the pre-change commit injected the **post**-change wording — 124 verbatim occurrences in the delegation `raw/*.jsonl`, zero occurrences of the wording that worktree actually contained. The arm returned the same zero-contamination count as the after-arm, from a lever that was never pulled.

Two obligations follow:

- **Switch the agent dir, not the working tree.** Point `PI_CODING_AGENT_DIR` (`config.ts`, `ENV_AGENT_DIR`) at a shadow directory that symlinks every entry of the real agent dir except the one skill under test, which points at the target worktree. Symlink rather than copy so no credential file (`auth.json`) is duplicated into a temporary location.
- **Verify the injected text after the run, from the run's own artifacts.** Grep the delegation `raw/*.jsonl` for the wording verbatim and report the per-arm counts. `drift_check.py` cannot serve here: it compares copies *inside the repository* and says nothing about what a running agent loaded. A zero that has not passed this gate is inconclusive, not a pass.

This also explains a class of confound in the recorded Pi-facet runs: when a commit lands mid-session, the runs before and after it were executed against different skill text even though nothing about the harness changed. Runs are not a single skill version unless the agent dir was pinned.
