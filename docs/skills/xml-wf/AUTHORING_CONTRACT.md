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

## Relationship to mode-orchestrator

xml-wf and `skills/mode-orchestrator/` are separate products that independently reimplement the same idea — running an isolated, mode-tagged turn per step — over different substrates (xml-wf: `wfrun`'s deterministic Python control flow over `<step>` elements; mode-orchestrator: one subagent call per todolist step). Neither is canonical for the other; there is no shared file and no propagation obligation between them.

When a change here alters step-execution semantics in a way that looks generally useful (e.g. how a stale completion signal is discarded, how the harness/model resolution works, retry/escalation policy), consider whether `skills/mode-orchestrator/` would benefit from the same idea and note it for its maintainer — a suggestion to evaluate, not an obligation to port.

## Mode prompt fragments

`scripts/wfrun/modes/` is an INDEPENDENT derivation of the `role-mode` plugin's canonical mode bodies (`plugins/role-mode/prompts/modes/`), not a copy: xml-wf's own 4-axis/3-axis framework headers (`Mode / Rules / Task / Role`, `[BLOCKED: rules <id>]` literal, guardrail text) exist because a `<step>` carries `<rules>` and `<task>` that canonical does not model. Only the **mode filename set** (identity — which modes exist) tracks canonical; bodies and headers are maintained here independently and must never be synced by filename match. Propagation obligations and drift detection are owned by the `generate-sibling-handoff` skill's registry (`references/families.md`, `role-mode` family) — see that registry before touching `scripts/wfrun/modes/`, and do not duplicate its content here.
