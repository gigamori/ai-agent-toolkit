# Instruction-Writing Tips

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

## What belongs in the instruction set

Before tuning wording, decide whether a statement should be an instruction at
all. Two families behave differently under pressure:

- **Directive** — an obligation ("use at most three bullets", "never edit the
  source"). When it collides with the content, the model damages the content to
  comply. Two colliding directives have no principled resolution, so the
  conflict doesn't converge however many times the skill is re-run.
- **Natural framing** — a statement that changes how the situation is read
  ("this field records a user preference, not an execution detail"). Framing
  that doesn't apply is simply unused; it cannot collide.

Sort with one question: when this statement and the content disagree, which
should win? If the statement should win — because any content contradicting it
is wrong — make it a directive. Otherwise make it framing. Only statements that
pass keep the directive set small enough to hold.

Two things quietly grow that set:

- **Numeric caps.** "At most three items" collides with content resting on a
  five-point progression, and it's the content that gets cut. The repair is to
  remove the cap, not raise it — any number you write becomes the next
  collision. The same goes for an illustration from the requirements
  conversation ("seven would be fine") transcribed into the skill as a limit.
- **Restating a procedure that already lives elsewhere** — in another section, a
  reference file, or a script's own error message. The restatement grants no new
  capability and only raises compliance pressure. Link instead.

## Position before order

Ordering matters only once the block sits somewhere the agent still weights it.
Expect a serial-position pattern: the beginning gets primacy, the end gets
recency, and the middle is where instructions go to die. That is the real cost
behind the length ceiling (see `SKILL.md` § Keep SKILL.md Under 500 Lines) — it
is a dilution budget, not a style rule.

- Move a load-bearing block to a frontier *before* rearranging its insides.
  Reordering bullets inside a block buried mid-document rarely rescues it.
- Don't assume the last line wins. Authority channel, tool schema, and plain
  semantic relevance can each dominate recency.

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
- Introduce a term before it carries weight. A tool name, field, or literal
  first appearing inside the instruction that depends on it is a forward
  reference at the vocabulary level.
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
- That example sets the ceiling, not the floor. Examples carry salience, so the
  model treats yours as the target — one weaker than what it would have produced
  unaided is worse than no example at all.

## Salience: rarity, not intensity

`IMPORTANT` / `CRITICAL` / `NEVER` lose effect in any skill that already uses
them liberally — they saturate. What still works: a single rare, distinctive
anchor (e.g. a bracketed tag like `⟦GATE⟧`) placed once, at the end, bound
directly to a required action ("resolve this before generating output").
Reserve it for the one condition that must not be missed — using it more than
once per skill defeats the rarity that makes it work.

## Bracketing: both frontiers, different forms

When one requirement dominates the skill, place it at both ends and keep it out
of the middle. Don't repeat the same sentence — verbatim repetition reads as
boilerplate and loses the salience it was meant to buy. Change its form:

- Front: state the requirement as the decision frame ("`debugger` records the
  user's preference, not an execution detail").
- End: demand an act ("quote `debugger:human` or `debugger:llm` verbatim, or
  stop").

Reserve this for one requirement. Bracketing everything is just repetition.

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

## Reference files buy token economy, not isolation

Progressive disclosure (see `SKILL.md` § Progressive Disclosure) keeps tokens
out of the window until they're needed. It is easy to over-read that as
protecting a decision from material that shouldn't influence it. It doesn't,
unless all four hold:

- The decision can be stated without the file's contents.
- The read is genuinely on demand — anything expanded at render time (see
  `advanced-mode.md` § Dynamic Context Injection) buys no isolation.
- The noise you're worried about isn't already in the conversation. A file
  reference cannot evict what is already in context.
- The decision happens *before* the read. Afterwards the loaded content weighs
  the same as if you had inlined it.

For a genuinely fresh window, use `context: fork` (see `advanced-mode.md` §
Isolated Execution), which does not inherit conversation history. The trade-off
either way is retrieval omission: a reader given only a slice cannot notice what
the slice left out.

## Validating the wording

Every technique here shifts a probability. One clean run is not evidence the
wording holds.

- Test in a fresh process. Skill and agent definitions are not reliably
  hot-reloaded, so re-running inside the same session can be measuring the old
  file.
- Build a hostile context rather than a clean one: a long conversation
  saturated with the tokens the skill is supposed to ignore, then invoke it with
  the required argument missing.
- Score observable behavior, not the agent's account of itself. Did it stop? Did
  it ask? Did it quote the allowed value verbatim? Did it infer the answer from
  a cue you forbade?
- A handful of runs is a smoke test, not a result. If the requirement is
  load-bearing, the fix is the enforcement mechanism named under
  § Checkable-ization, not another round of rewording.
