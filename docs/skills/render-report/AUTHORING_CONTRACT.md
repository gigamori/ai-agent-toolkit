# Maintenance contract — render-report

> Positioning: this is **maintenance context, not execution context** — not needed to *run* render-report, needed to *maintain* it. `SKILL.md` and the files under `references/` are behaviourally accurate for execution; this file records the obligations an editor of the skill must satisfy that they do not (and should not) state.

## What the skill is

render-report is a **containment shell around `officecli-pptx`**, plus fidelity gates. It does not carry deck-design knowledge of its own. The quality of a deck is owned by `~/.claude/skills/officecli-pptx/SKILL.md`; render-report owns two things the deck skill does not: keeping the layout trial-and-error out of the caller's context, and checking afterwards that the deck still says what the report said.

This is a reversal of the earlier design, which put a **deck-spec** document between the report and the pptx and had render-report supply the layout rules. That design was measured against a control that delegated the whole job to `officecli-pptx` with no render-report documents at all, on the same 342-line report: the control produced the better deck, and the deck-spec pipeline got worse as more layout rules were added to it. Only containment kept the control from being adoptable, and containment is a subagent boundary rather than a document. The old pipeline is in git history; do not reintroduce it piecemeal.

## The one contract and the gates

| Contract | Definition | Deterministic gate |
|---|---|---|
| report markdown | `references/report-md-contract.md` | `scripts/validate_contract.py` |

There is no second contract. The deck is not a document with a schema any more, so the gates below check the *rendered pptx* against the report rather than checking an intermediate against a spec.

| Gate | Kind | What it does |
|---|---|---|
| `scripts/check_pptx_text.py` | deterministic | **Judges** two things: every in-scope `F-n` appears on some slide, and each `F-n` shares a slide with its confidence mark. **Lists** a third: every numeric token on the deck that does not occur in the report. The listing never sets the exit code |
| `officecli validate` / `view issues` | deterministic | Schema, overflow, structure. Re-run by the caller rather than trusted from `[D]`'s report |
| `scripts/check_contrast.py` | deterministic | WCAG ratio at sites where a colour was set explicitly |
| `references/deck-review.md` (`[J]`) | LLM, fresh context | Five distortion classes over `officecli view <pptx> text` |
| contact sheet (`[H]`) | human | Readability, which no gate above judges |

`check_pptx_text.py` imports `validate_contract.py` (`claim_blocks`, `CONFIDENCE_MARKS`) by path. A rename or signature change in the older script breaks the newer one, and nothing but the test suite will say so. Run the suite after touching either.

## Editing rules

- **Do not restate in a document what a gate already enforces.** `deck-building.md` is what `[D]` reads, and its content budget is the control's own prompt plus containment. Two constraints that gates already check — "do not strengthen a claim" (`[J]`) and "put `F-n` on the same slide as its mark" (`check_pptx_text.py`) — were written into it as standalone rules, then removed on 2026-09-02. The requirement that a gate checks still reaches `[D]` through the section that tells it to *run* that gate; a second copy phrased as a rule adds compliance pressure without adding capability. **The effect of that removal is unmeasured** — see "What is not measured".
- **`範囲` (scope) narrows the input, never the output.** It selects which part of the report the deck is built from. It must never be used to specify a slide count. A slide-count instruction makes the resulting page count an artefact of the instruction rather than an observation, which is how one earlier experiment was invalidated.
- **The pptx route is one hop.** Any statement of Hard Rule 1 must name the route it applies to: `docx`/`xlsx` are verbatim transcription, `pptx` is authored by `[D]`. A blanket "wording is never rewritten" is false for pptx and is the sentence a maintainer will reach for by reflex.
- **Numbers are listed, not judged.** Do not "fix" `check_pptx_text.py` by adding a rule that exempts short integers, digits-only lines, or dates. The decision and its evidence are below.
- **`[J]` runs in a fresh context and sees no images.** If it is ever given the contact sheet, it stops being a fidelity judge and starts being a second layout reviewer, which `[H]` already is.
- **Gate 3 inside `[D]` is judged by a different subagent.** `officecli-pptx` says the builder is biased toward "looks fine"; measured on this skill's own earlier run, self-judgement produced 4 findings where an independent judge produced 59 on the same deck.

## Blind spots — what the gates do not see

A gate's coverage claim is only as good as its stated denominator.

| Gate | Does not see |
|---|---|
| `check_pptx_text.py` numeric listing | Nothing about correctness: it is a listing, not a verdict. On the control deck all 15 entries were decorative — divider counters `01`–`04`, the cover date, and two axis reference ticks (`1.0`, `1.00`) — and zero were fabricated data. It also cannot see a substituted value whose digits occur elsewhere in the report, a correct number under a wrong label, or a swapped denominator |
| `check_pptx_text.py` chart data | Chart categories and values do **not** appear in `officecli view <pptx> text`; they are read separately from `officecli query <pptx> chart --json`. A category string containing a comma is mis-split, because that JSON returns categories comma-joined. A native chart's **data labels round** (`33.6` for `33.632`) and the rounded form is not what the query returns, so a rounded label that does not occur in the report is invisible to this gate and to the visual auditor alike |
| `check_pptx_text.py` coverage | That the slide carrying `F-n` says anything true about `F-n` |
| `[J]` | Anything that is not one of the five distortion classes. In particular, a deck that moves the report's `検証状態` (test statistics, robustness variants) into its body prose and drops the `解釈` (the causal reading) is **not** a distortion, so `[J]` passes it. A deck can be faithful and still fail to communicate; that is `[H]`'s to catch, and `[H]` is the only gate that catches it |
| `[J]` | Defects that are already in the report. A deck that faithfully carries a report's own contradiction passes every gate here. On 2026-09-02 three independent role-played reviewers reading only two claim blocks each found the same contradiction between a claim's heading and its own `限界` line, plus an over-strong causal verb in an `解釈`. `validate_contract.py` requires a heading to be an assertion but does not check it against the block's own limits. **Upstream defects are out of this skill's scope and no gate here will report them** |
| `check_contrast.py` | Sites where no colour was set explicitly (theme defaults). It applies to text on *any* background, white included — a run on 2026-09-02 skipped 31 white-background pairs and missed three sites at ≈2.9:1 that the deck builder later reported |
| `officecli view issues --type low_contrast` | Not adopted as a replacement; see the measurement in `docs/dev/test-constraints.md` |

The measurement unit for the deterministic rows is the control arm in `scripts/tests/test_check_pptx_text.py`. Regions with no control arm carry no assurance from this table.

## Decisions

- **Numeric provenance is a listing, not a verdict** (user ruling, 2026-09-02). Run against the control deck, the judging version produced 13 failures: 12 decorative section counters and one genuine derived value absent from the report (`88.30%` = 100 − 11.70). The derived value is exactly what the constraint exists for; the counters are false positives, and no predicate available to the script separates them — a counter is not wrong content, so it fails the test for a rule ("whatever the content, the content is the side that is wrong"). Rather than add a threshold, the script lists and `[J]` decides.
- **Charts may be built from any number in the report** (settled 2026-09-02 by adopting v3). The earlier Hard Rule 5 admitted only tables carrying a chart annotation. The control deck charted a five-point series from a claim's `根拠`, which has no annotation, and that slide was the quality target. Annotations remain the author's explicit request for a figure and still govern `docx`.
- **`[D]` is one role.** The earlier split of authoring from layout was dropped with the deck-spec. Its stated risk — that a single agent doing both would degrade the writing — is the one real cost of the change, and it remains at one observation.
- **The `officecli batch` code renderer is still not built.** `[D]` writes the deck through officecli calls of its own choosing.

## Measurements

### Equivalence with the control is stipulated, not measured

The adoption decision on 2026-09-02 was a ruling that the shell is "equivalent to the control", taken deliberately without the run that would separate the remaining difference. Recorded so no later reader mistakes it for a measurement.

| | control (12 claims) | control, narrowed | v3 shell, narrowed |
|---|---|---|---|
| documents read | officecli-pptx only | officecli-pptx only | + `deck-building.md` |
| scope | 12 claims | F-6, F-7 | F-6, F-7 |
| model | Opus 5 | Opus 5 | Fable 5.1 |
| tool calls / tokens / minutes | — | 116 / 320k / 39 | 37 / 219k / 25 |
| slides | 24 | 13 | 8 |
| body prose drawn from | — | `解釈` | `検証状態` |

Containment held in every run (no images returned to the caller). Structure — splitting one claim across slides, and giving the crude figure its own heading — appeared in the narrowed control too, so it is an effect of narrowing the scope and not of these documents. The prose difference is **confounded with model and budget** and is not attributable to the documents. Narrowing scope lowers the total slide count but raises slides per claim (2.0 → 6.5 in the control), because the framing slides are a floor.

### What is not measured

- **`[J]`'s detection rate for this skill.** The earlier judge was measured at 5 of 5 planted deviations with a clean control, but that measurement was taken with a different model, a different `deck-review.md` and a different input (a deck-spec, not rendered text). Its own recorded re-measurement condition names all three. **It does not transfer.** The current judge has one trial, with nothing planted: no evidence of detection at all. Do not quote a `PASS` as assurance.
- **The effect of removing the two restated rules from `deck-building.md`.** Removed on the reasoning above, not on a measurement. Nothing was lost from the gates, so the change cannot reduce coverage; whether it improves the prose is open.
- **PowerPoint on real hardware.** `validate` does not prove a deck opens.
- **Which model runs `[D]` in production.** The two runs above used different models and neither is designated the representative one.

### Run on 2026-09-02 (the adopted shell, scope F-6/F-7)

| Stage | Result |
|---|---|
| `[D]` | 8 slides; Gates 1/2/2b pass; Gate 3 with a fresh auditor, 3 rounds, **not converged** (residual sub-threshold spacing and CJK line-breaking items) |
| `check_pptx_text.py --scope F-6,F-7` | `OK`, listing empty |
| `officecli validate` / `view issues` | pass / 0 |
| contrast | 73 pairs checked, 1 fail, fixed; then 12 more sites fixed after the builder reported the white-background gap |
| `[J]` | `PASS`, no findings |
| `[H]` | The prose was rejected by the human reader as a list of numbers — the gap `[J]` cannot see, above |
