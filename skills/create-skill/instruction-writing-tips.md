# Instruction-Writing Tips (Ordering, Concreteness, Salience)

Read this when writing the wording *inside* a skill's instructions — the SKILL.md
body, a `prompt.md` template, or a `subagent-protocol.md` gate — where an agent
must reliably follow a directive rather than merely be aware of it.

## Scope

These effects operate mainly within a single role/channel — the tokens an agent
reads as its own prompt. They are weaker than the permission hierarchy
(system > developer > user > tool), tool schemas, and validators, which override
wording. Use the wording techniques below to make correct behavior *more likely*;
use schema/hook/validator enforcement (see `advanced-mode.md` § Hooks) to make it
*certain*.

## Ordering: conditioner before trigger

Autoregressive generation conditions each token on everything before it and
cannot rewind. Consequences for how you order a section:

- Put natural/contextual framing (what a field *means*, what it's for) early —
  it conditions interpretation without committing to an action yet.
- Put the concrete, checkable instruction (the actual trigger for behavior)
  last — recency plus specificity make it actionable.
- Don't make an early section depend on something defined later (a forward
  reference) — it forces the model to resolve a dependency across a gap it
  can't reliably revisit.
- This is why `subagent-protocol.md`'s section order (`role` → `rules` →
  `context` → `task` → `constraints`) works: role/rules establish criteria and
  boundaries *before* the model commits to executing `task`. Keep new prompt
  templates in that order for the same reason, not just for consistency.

## Concreteness in reusable templates

Abstract instructions ("verify appropriately") are satisfied loosely and
violated with a post-hoc justification. Concrete instructions ("quote
`debugger:human` or `debugger:llm` verbatim, or stop") leave no room to drift.

- Trade-off: concrete instructions overfit to one context; abstract ones
  under-bind.
- For templates reused across task types (like `prompt.md`): hardcode values
  that never change across invocations. For values that vary per invocation,
  instruct the model to "make this concrete at generation time" *and* seed one
  worked example — a template that only says "be specific" tends to get filled
  in vaguely once it's copied a few times.

## Salience: rarity, not intensity

`IMPORTANT` / `CRITICAL` / `NEVER` lose effect in any skill that already uses
them liberally — they saturate. What still works: a single rare, distinctive
anchor (e.g. a bracketed tag like `⟦GATE⟧`) placed once, at the end, bound
directly to a required action ("resolve this before generating output").
Reserve it for the one condition that must not be missed — using it more than
once per skill defeats the rarity that makes it work.

## Checkable-ization: make judgment verifiable

Replace judgment calls with verifiable acts, and replace branches with
unconditional steps:

- "Confirm X is correct" (a judgment call the model can confabulate) → "quote X
  verbatim from the source; if you cannot, stop and ask" (a verifiable act).
- "If unsure, ask" (a branch the model can route around) → "the first line of
  output must be the quoted value" (an unconditional step).

**Caveat**: a citation/quote requirement raises observability — you can see
whether it happened — but it is not by itself an enforcement gate. A model can
generate a plausible-looking quote that doesn't match the source. Only a
programmatic check (schema validation, exact-match against the source or
invocation, a tool-call constraint, or a hook — see `advanced-mode.md` §
Hooks) actually blocks incorrect behavior. Use wording to raise the odds; use
one of those mechanisms wherever the failure would be costly.
