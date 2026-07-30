# Maintenance contract — mode-orchestrator

> Positioning: this is **maintenance context, not execution context** — not needed to *run* mode-orchestrator, needed to *maintain* it. `SKILL.md` is behaviorally accurate for execution; this file records the obligations an editor of the skill must satisfy that `SKILL.md` does not (and should not) state.

## Dual-harness integrity

The skill runs unmodified on both Claude Code and Pi: `SKILL.md` Step -1 probes the harness and reads `references/harness-cc.md` or `references/harness-pi.md` accordingly, so the two facets share one entry point but diverge in delegation method, time-bound mechanics, and the mechanical denial check (Claude Code only — Pi has no permission layer).

Because both facets live in one skill and Pi loads it via a symlink into `~/.pi/agent/skills/` (not a copy), there is no propagation step to remember — the change reaches Pi automatically. What is NOT automatic is correctness on both sides:

- Before landing a change to `SKILL.md`'s Execution steps, or to anything a harness reference file governs (delegation method, time-bound signaling, the denial check), read BOTH `references/harness-cc.md` and `references/harness-pi.md` and judge explicitly whether each still holds.
- A change scoped to one harness reference (e.g. a Claude Code watchdog fix) needs no edit to the other — but state that scoping decision, don't leave it implicit.
- Re-run `scripts/watchdog_test.sh` and `scripts/deny_scan_test.sh` after touching either script; both are self-contained (they build their own fake session logs) so this costs nothing to skip accidentally — don't.

## Relationship to xml-wf

mode-orchestrator and `skills/xml-wf/` are separate products that independently reimplement the same idea — running an isolated, mode-tagged turn per step — over different substrates (mode-orchestrator: one subagent call per todolist step; xml-wf: `wfrun`'s deterministic Python control flow over `<step>` elements). Neither is canonical for the other; there is no shared file and no propagation obligation between them.

When a change here alters step-execution semantics in a way that looks generally useful (e.g. how failures escalate to a debug turn, how a stale completion signal is discarded, how the harness/model resolution works), consider whether `skills/xml-wf/` would benefit from the same idea and note it for the xml-wf maintainer — a suggestion to evaluate, not an obligation to port.

## Mode prompt fragments

`modes/` is a snapshot of the `role-mode` plugin's canonical mode bodies (`plugins/role-mode/prompts/modes/`), synced manually and currently text-identical to canonical (EOL differs only). Its propagation obligations, sync procedure, and drift detection are owned by the `generate-sibling-handoff` skill's registry (`references/families.md`, `role-mode` family) — see that registry before touching `modes/`, and do not duplicate its content here.
