# Maintenance contract — xml-wf

> Positioning: this is **maintenance context, not execution context** — not needed to *run* xml-wf, needed to *maintain* it. `SKILL.md` and `references/spec.md` are behaviorally accurate for execution; this file records the obligations an editor of the skill must satisfy that those files do not (and should not) state.

## Multi-facet integrity

xml-wf executes steps through three interchangeable facets, selected by mode/backend: **Run (batch)** (`--backend cc`, `references/run-cc.md`), **Run (batch, Pi)** (`--backend pi`, `references/run-pi.md`, driven by `scripts/wfrun/pi_cli.py`), and **LLM orchestration** (`--run-llm`, `references/run-llm.md`, which itself branches into layer A / claude CLI and layer B / subagent facility, working on any harness including Pi with no subagent tool).

The `pi` backend is not a thin mirror of `cc`: it refuses some attributes outright (`schema=`, for one, because it cannot enforce structured output) and rewrites others. `references/run-pi.md` enumerates every such divergence and is the source of truth for all of them — read it there, and when a divergence changes, change it there.

Because Pi loads this skill via a symlink into `~/.pi/agent/skills/` (not a copy), a change reaches Pi automatically — there is no propagation step to remember. What is NOT automatic is correctness on the Pi side:

- Before landing a change to `scripts/wfrun/executor.py` (or anything the control-flow spec governs — error handling, retry, `<replan>`, variable resolution), judge explicitly whether `scripts/wfrun/pi_cli.py` and `references/run-pi.md` still hold, and update both if the pi backend's behavior or its documented rewrites now differ.
- Run the full test suite from the skill's `scripts/` directory (`uv run python -m unittest discover -s tests`) after touching `executor.py`, `pi_cli.py`, or anything under `scripts/wfrun/modes/` — the suite includes `test_pi_cli.py`, `test_run_backend.py`, and `test_run_inherit_model.py`, which are the only mechanical check that the pi facet did not silently regress.
- A change scoped to one facet only (e.g. a `run-llm` layer-B delivery fix) needs no edit to the others — but state that scoping decision, don't leave it implicit.
- After editing `scripts/wfrun/modes/*.md` or the prompt assembly, sample the prompt layer (`uv run python evals/prompt_smoke.py`, also from `scripts/`) and compare compliance rates before/after — that layer is probabilistic and outside the unit tests.
- Injected guardrail text lives in `scripts/wfrun/guardrails.py` and nowhere else, including the conditionally injected `VALUE_LINE_RULE` and the `VALUE_LINE_PREFIX` / `VALUE_LINE_PLACEHOLDER` tokens its extractor compares against: `stepio` holds the *condition* and the parsing, never a string literal of the prompt. This is what keeps the A/B control cheap — a **wording** change is measured by restoring `guardrails.py` alone in a worktree, and only a change to the injection **condition** needs `stepio.py` in the before-condition too. Wording that migrates into `stepio.py` silently escapes that control.

## Relationship to mode-orchestrator

xml-wf and `skills/mode-orchestrator/` are separate products that independently reimplement the same idea — running an isolated, mode-tagged turn per step — over different substrates (xml-wf: `wfrun`'s deterministic Python control flow over `<step>` elements; mode-orchestrator: one subagent call per todolist step). Neither is canonical for the other; there is no shared file, and — with the one exception below — no propagation obligation between them.

When a change here alters step-execution semantics in a way that looks generally useful (e.g. how a stale completion signal is discarded, how the harness/model resolution works, retry/escalation policy), consider whether `skills/mode-orchestrator/` would benefit from the same idea and note it for its maintainer — a suggestion to evaluate, not an obligation to port.

## Decision contract (aligned with mode-orchestrator — the one propagation obligation)

The demand-driven decision channel (`DECISION:` requests, `references/spec.md` "Decision requests") shares a **deliberately aligned user-facing contract** with mode-orchestrator's decision loop: the decider vocabulary (`human`/`llm`, default `human`), the cap semantics (llm rulings only — human answers never consume it), and the escalation grounds (irreversible / outward-facing / goal-changing → human). The alignment exists because users move between the two skills, and defaults that disagree tax every unattended run's post-mortem.

The implementations are independent (xml-wf enforces the contract in `wfrun` code; mode-orchestrator enforces it as prompt contract in `SKILL.md`) and some divergences are **deliberate** — e.g. xml-wf's run-llm orchestrator never reads a request body while mode-orchestrator permits a scoped read of its `## Decision request` section; xml-wf has continuation forms (a)/(b), mode-orchestrator has amend-plan. Do not "fix" those toward each other.

When editing either side's decider vocabulary, defaults, cap semantics, or escalation grounds, the `generate-sibling-handoff` registry (`references/families.md`, **decision-contract** family) owns the propagation trigger and the consumer list — consult it before landing the change, and do not duplicate its content here. Precedent: the cap-scope alignment landed as P3 (`9168a52`).

## Model choice for evals

`scripts/evals/` harnesses call a real CLI and cost money, so a `--model` default there is a maintained choice, not a placeholder. Take it from the **measurement layer** list in `references/build.md` (§ Model selection), which is canonical because `mode-orchestrator` reads the same list — do not restate the names here. When a default moves, say so at the call site: a rate measured under a different variant is not a continuation of the old figure. Precedent: `adjudicator_smoke.py` defaulted to `google/gemini-3.1-flash-lite` until 2026-08-17, and the 40/40 ruling rate `build.md` cites belongs to that variant, not to the current default.

## Mode prompt fragments

`scripts/wfrun/modes/` is an INDEPENDENT derivation of the `role-mode` plugin's canonical mode bodies (`plugins/role-mode/prompts/modes/`), not a copy: xml-wf's own 4-axis/3-axis framework headers (`Mode / Rules / Task / Role`, `[BLOCKED: rules <id>]` literal, guardrail text) exist because a `<step>` carries `<rules>` and `<task>` that canonical does not model. Only the **mode filename set** (identity — which modes exist) tracks canonical; bodies and headers are maintained here independently and must never be synced by filename match. Propagation obligations and drift detection are owned by the `generate-sibling-handoff` skill's registry (`references/families.md`, `role-mode` family) — see that registry before touching `scripts/wfrun/modes/`, and do not duplicate its content here.
