# Maintenance contract — render-report

> Positioning: this is **maintenance context, not execution context** — not needed to *run* render-report, needed to *maintain* it. `SKILL.md` and the files under `references/` are behaviourally accurate for execution; this file records the obligations an editor of the skill must satisfy that they do not (and should not) state.

## The two contracts and who enforces them

The skill owns two structure contracts and four gates. Changing either contract without moving its gate leaves an assurance that nobody produces.

| Contract | Definition | Deterministic gate | LLM gate |
|---|---|---|---|
| report markdown | `references/report-md-contract.md` | `scripts/validate_contract.py` | — |
| deck-spec | `references/deck-md-contract.md` | `scripts/validate_deck.py` (G-A) | `references/deck-review.md` (G-B) |

`scripts/check_pptx_text.py` (G-R hop2) enforces neither contract: it enforces that the rendered pptx says only what the deck-spec says.

**`validate_deck.py` imports `validate_contract.py`** — `ACTION_VERB_HINTS`, `parse_frontmatter`, `claim_blocks` and `section_subheadings`. A rename or signature change in the older script breaks the newer one, and nothing but its test suite will say so. Run the suite after touching either.

## Editing rules

- **The pptx path is 2 hop; docx and xlsx are 1 hop.** Any statement of Hard Rule 1 must name the hop it applies to. A blanket "wording is never rewritten" is now false for hop1, and it is the sentence a maintainer will reach for by reflex.
- **The archetype set is closed at 6.** Adding one means editing `deck-md-contract.md`, `deck-archetypes.md` (its region table), `deck-authoring.md` (its per-archetype table), `validate_deck.py` (`ARCHETYPES`, `REQUIRED_SLOTS`, `OPTIONAL_SLOTS`, and `BODY_ARCHETYPES` if the new one carries claims), and the mapping table in `references/format-mapping.md`. Five files; there is no single source.
- **The layout-label set is closed at 3** (`主要な発見`, `主な限界`, `根拠`). It is stated in `deck-archetypes.md` and duplicated as `LAYOUT_LABELS` in `check_pptx_text.py`, because the checker cannot parse a prose table at runtime. Changing one without the other makes hop2 either reject a legitimate deck or admit an invented word. This duplication is deliberate; do not "fix" it by having the script read the markdown.
- **`deck-review.md`'s exclusion list is load-bearing.** It tells the judge not to flag what a deterministic gate already covers (`検証状態`, `付録`, numeric provenance). Removing a deterministic check without adding its subject back to the judge's scope leaves that subject unchecked by anything.
- **Deviation classes are fixed at 5.** The base-rate measurement is per class; adding a class invalidates the recorded rates for none of the others but leaves the new one unmeasured, which must be stated rather than assumed.

## Blind spots — what the gates do not see

Recorded because a gate's coverage claim is only as good as its stated denominator. Each row was produced by a control arm in `scripts/tests/`, not by reading the code.

| Gate | Predicate | Does not see |
|---|---|---|
| G-A structure | archetype / slot / trace resolution | whether the content is right |
| G-A numeric provenance | every numeric token in deck content ∈ the report's token set | a correct number under a wrong label; a swapped denominator; a value recombined from another row; a claim carrying no digits at all; **any substituted value whose digits occur anywhere else in the report** (`17列` → `18列` passes because `18` occurs elsewhere) |
| G-A numeric provenance | — | tokens that are not quantities: `F-1` yields `1`, `2008-05-05` yields `2008`/`05`. Both sides use the same tokenizer, so this is consistent, never a FAIL — and never a catch either |
| G-A source table | header equality selects the table, then full row/column equality | a source table with a **column dropped** stops matching any report header and degrades to an authored table. Under a chart annotation the chart check still catches it; **a source table with no chart above it degrades silently** |
| G-A source table | — | choosing the wrong table for the slide |
| G-A authored table | numeric provenance only | every string cell (left to G-B), and any recombination of real numbers |
| G-A chart | annotation must match the report's after whitespace normalization; next table must be a source table | how the chart actually renders |
| G-A coverage | every `[F-n]` / `エグゼクティブサマリ` / `限界と注意事項` has ≥1 body slide | a slide that carries the trace but says something else. `分析対象データ` and `データの基本的特徴` are deliberately **not** in the coverage set |
| G-B | five deviation classes over body slides | `title` / `evidence` / `appendix` slides; visual emphasis; numeric correctness; probabilistic misses |
| G-R | every rendered string ∈ deck-spec strings ∪ 3 labels ∪ slide number | correct text placed on the **wrong slide**; text positioned off-canvas; anything the renderer omitted entirely (the check is containment, not equality) |
| G-R | — | a slide-number line that coincides with the slide's own number is admitted unconditionally |

The measurement unit for every count above is the control arm in `scripts/tests/test_validate_deck.py` and `test_check_pptx_text.py`. Regions with no control arm carry no assurance from this table.

## Contract decisions taken during implementation

- **A `title` slide carries no `trace:`.** The design document said trace was required on every slide while its own skeleton showed `## S1 title` with none and its value list had no title-shaped value. Settled in favour of the skeleton: the title slide's provenance is already fixed by frontmatter `title` / `source`. `validate_deck.py` rejects a `trace:` on a title slide, so the two readings cannot both pass.
- **`officecli view issues --type low_contrast` is not adopted.** It was evaluated as a replacement for `check_contrast.py` and returned `count: 0` on a deck measuring 1.22:1. The measurement is in [`docs/dev/test-constraints.md`](../../dev/test-constraints.md). Re-test before adopting it on a later officecli.
- **The `officecli batch` code renderer (deck-spec → batch JSON with archetype geometry and a text-fit estimate) is not built.** `[R]` stays an LLM transcription hop. The decision was deferred until the cost and overflow rate of the LLM path are measured on a real deck; if it is built, the hop2 wording check moves from a post-render officecli call into a unit test over the payload.

## Base-rate measurement for G-B

An LLM judge's `PASS` is not evidence until its detection rate has been measured per deviation class. Until that measurement exists:

- Do not treat a `PASS` as assurance over the classes in `deck-review.md`.
- Do not report a clean run as proof; report it as one trial.

The measurement is one deck-spec fixture per class, each carrying a single planted deviation, judged once, with the rate recorded here alongside the model and date. **Not yet run** — see the repository's own rules on probabilistic verification before running it: a zero-hit result is inconclusive without a control arm at a non-zero rate.

## What has not been run end to end

The full sequence `[A] → G-A → [G-B] → human → [R] → G-R` on a real report has **not** been executed. What exists is each piece verified in isolation: `validate_deck.py` and `check_pptx_text.py` against their control arms, and a hand-authored deck-spec of a 342-line report passing G-A.

Running it for real needs three separately launched subagents; doing all three roles in one context is not evidence, because two of the design's guarantees are context boundaries — the judge must not grade what it wrote, and the renderer must not have read the report. When it is run, record here: the slide count against the 77 slides a verbatim transcription of the same report produced, the number of loop iterations consumed, and whether G-R found overflow. Those three numbers are also the input to the deferred decision on building a deterministic renderer.

## Test suite

```bash
cd skills/render-report/scripts && uv run --no-project python -m unittest discover -s tests
```

The clean control arm is a hand-authored deck-spec of a real 342-line report (`tests/fixtures/deck_clean.md` against `report_clean.md`). It was written **before `validate_deck.py` existed**, so a passing clean run means the predicates admit a real deck rather than that the deck was fitted to the code. It was written **after `deck-md-contract.md`**, so it cannot expose a hole in the contract itself — which is why it passed on the first run. Preserve that ordering when adding a predicate: extend the clean fixture only when the contract genuinely changed, never to make a new check go green.

Neither blind spot in the table above came from the clean pass. Both came from mutations: the column-dropped source table and the numeric substitution whose digits occur elsewhere. Mutation controls, not the clean arm, are what find holes here.

`tests/fixtures/view_text_clean.txt` is a hand-written `officecli view text` rendering of that deck-spec. It is not generated by `check_pptx_text.py`; generating it from the code under test would make every containment assertion vacuous.

These files are inside the `skills/` test-knowledge gate ([`docs/dev/test-gate.md`](../../dev/test-gate.md)): no comments, no docstrings, no unreachable references. Run the gate after editing them.
