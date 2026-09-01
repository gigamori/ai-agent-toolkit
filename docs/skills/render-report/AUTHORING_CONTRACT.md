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

### Measured 2026-09-02

Model: the judge inherited the parent (Opus 5, 1M context). Six trials: five decks each carrying exactly one planted deviation, plus `tests/fixtures/deck_clean.md` unmodified as the control arm. Each deck was judged once, in a fresh context, given only the report, the deck's body slides and `deck-review.md`.

**Every planted deck passes `validate_deck.py` first.** That is what makes the measurement about G-B: the deterministic gate cannot see any of these five, so a detection is attributable to the judge alone.

| Class | Plant | Verdict | Class named | Slide named |
|---|---|---|---|---|
| `MODALITY_UP` | S12 `body`: 「棄却できない」→「棄却された」 | FAIL | correct | correct |
| `QUALIFIER_DROP` | S8 `evidence`: 「分母=前回接触ありの8,257案件。」を削除 | FAIL | correct | correct |
| `UNTRACEABLE` | S10 `body`: report にない一文を追加 | FAIL | correct | correct |
| `EMPHASIS_SWAP` | S2 `findings`: 主要な発見 1 と 2 を入替え | FAIL | correct | correct |
| `CONTRADICTION` | S5 `body`: 13,020行 → 12,443行（S21 と矛盾する実在トークン） | FAIL | correct | correct |
| — (control) | none | PASS | — | — |

5 of 5 detected. Every planted deck returned **exactly one** finding, so the 18 other body slides in each produced no false positive: 90 slide-level clean observations plus the control's own 19.

**What this does not establish.** One trial per class is not a rate. It shows each class is detectable and that the judge does not fail indiscriminately (the control passed); it puts no useful lower bound on the miss rate — a single success is consistent with a true rate anywhere above a few percent. Do not quote "5/5" as a detection rate. Re-measure with more trials per class before treating a `PASS` as assurance, and re-measure from scratch if the model, `deck-review.md`, or the class list changes.

The mutation strings are in the table above; the fixtures themselves are disposable and were built under `tmp/gb-baserate/` rather than tracked, since the ordinary change loop does not re-run them.

## End-to-end run — 2026-09-02

Executed on `tests/fixtures/report_clean.md` (342 lines, 12 claims) in an isolated worktree, with `[A]` and `[G-B]` as separately launched subagents so the two context boundaries the design depends on were real: the judge did not grade what it wrote, and it read only the report, the deck's body slides and `deck-review.md`.

| Stage | Result |
|---|---|
| `[A]` deck-author | 27 slides — 1 title, 1 exec-summary, 6 section, 12 finding, 4 evidence, 3 appendix |
| G-A `validate_deck.py` | OK, re-run independently of the agent's own claim |
| `[G-B]` deck-judge | PASS |
| `[H]` human approval | granted, with overflow explicitly waived for this run |
| `[R]` deck-renderer | 27 slides, 162 shapes, 4 charts, 664 words |
| G-R `check_pptx_text.py` | OK, re-run with this repository's checker rather than the renderer's own |
| G-R `officecli validate` | passed |
| G-R contrast | not applicable — all 27 slides enumerated, the only 4 explicit colour sites are chart series fills, no text-on-background pair |
| G-R overflow | 1 site (`S2` `exec-summary`/`findings`, ~458 chars against a ~424 budget); waived at `[H]`, nothing was shortened |
| Loop iterations consumed | **0** — no gate sent it back |
| PowerPoint open on real hardware | not done; `validate` does not prove it |

**27 slides against the 77** that a verbatim transcription of the same report produced. That ratio is the whole point of the second hop, and it is now measured rather than asserted.

The report markdown was untouched: the worktree's only new file was the deck-spec.

`[H]` was put to a human rather than self-approved: the gate exists so that no machine approves the deck it produced. The approval came with overflow waived, so the deck was rendered as authored.

`[R]` never read the report — its inputs were the deck-spec and `deck-archetypes.md` only. That hop2 produced no untraceable string is the evidence that the isolation held, and it was checked with this repository's `check_pptx_text.py`, not with the instrument the renderer wrote for itself.

### Measured gap: nothing checks the capacity guidance before rendering

`deck-archetypes.md` gives a per-slot character budget, but it is prose that `[A]` applies by hand and no gate reads. Measured over both decks:

| Deck | Slots over budget | Of measured | Worst |
|---|---|---|---|
| `[A]`'s E2E deck | 7 | 63 | `section/body` at 1.59× |
| the hand-authored control | 3 | 61 | `section/body` at 1.72× |

Two independent authors overshot, both concentrated in `section/body`. Overflow would surface only after an expensive render, as a G-R finding, one full loop iteration later.

The budget is arithmetic over the deck-spec and needs no pptx, so G-A could carry it. **Not built** — it is outside the design this change implemented, and is recorded here as the next candidate rather than added silently. If it is built, it belongs beside the other `validate_deck.py` predicates with its own control arm, and `deck-archetypes.md` becomes its data source rather than advice.
