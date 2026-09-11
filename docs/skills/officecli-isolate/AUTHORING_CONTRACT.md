# officecli-isolate — Authoring Contract

Maintenance obligations for `skills/officecli-isolate/`. Not read at runtime.

## What the skill is

A containment shell around officecli's own document skills. It owns two things and nothing else:

1. **Routing** — which of three prompt templates a job gets, decided by input shape (`SKILL.md` § Case selection), and which single officecli skill the child loads (`SKILL.md` § Genre resolution, resolved by the caller; the child never infers it).
2. **Containment** — one general-purpose child per artifact; the caller passes file paths and a resolved template, and takes back the return block only (`references/common.md`).

What the document should contain, how it should look, which commands build it and when it is good enough belong to the officecli skill the child loads (`officecli load_skill <name>`). Do not restate any of that here; when it changes, this skill is unaffected as long as the command names it cites still exist.

The caller's only check on the produced file is re-running the child's own two gate commands (`officecli validate`, `officecli view issues`) — cheap, and about the file's validity, not about its content.

## Files and their contract

| File | Read by | Must hold |
|------|---------|-----------|
| `SKILL.md` | caller | Inputs, case table, genre table, variable table, after-return procedure. Under 200 lines. No design guidance |
| `create/prompt.md`, `update-partial/prompt.md`, `replace/prompt.md` | child (as its entire prompt) | Section order `role → rules → context → tools → task → constraints`. Every `{variable}` in the `SKILL.md` table and no other. First rule: read `common.md`. Second rule: load exactly one officecli skill, which owns the document |
| `references/common.md` | child | Containment rules, officecli lifecycle, visual-audit delegation, the return block. The only definition of the return block |
| `references/instruction-formats.md` | caller (author) and child (reader) | Formats A (partial-update), B (xlsx layout), C (replace scope) — the only input formats this skill defines. A field not defined here is not read by anyone. State what a field means and what it produces, never how the child should drive the CLI |
| `scripts/contact_sheet.py` | child (pptx) | The only way a contact sheet is made. Reads slide count and aspect from the deck, fixes columns and a 1600 x 1920 viewport, pages with `--page` when the deck exceeds one sheet, checks the PNG header; exit 0 / 1 / 2 |

The three templates re-express the shared rules in their own `<rules>`; they do not inline `common.md`. Edit `common.md` and every template together when a shared rule changes.

## Return block

Defined once in `common.md`. `SKILL.md` § After return parses it; the user guides quote it. A change to a line name or its allowed values touches all four places in the same commit.

The block is the whole anti-pollution mechanism. A child that returns a narrative, an image path other than the contact sheet, or command output has broken the contract even when its file is correct. Keep the block's lines few and its values one line each: every line added to it is a second obligation competing with that one.

## Why the contact sheet is a script

Measured on officecli 1.0.148 (2026-09-10, html render path; PowerPoint was not installed):

- `view <pptx> screenshot --grid N` draws the grid inside a fixed viewport of 1600 x `--screenshot-height` (default 1200) and clips the rows that do not fit. A 5-slide deck under `--grid auto` became 2 columns x 3 rows with the third row cut at about a third of its height; the human judge could not read it, and the child that made it had reported the tiles as unclipped.
- `--grid auto` on a 20-slide deck rendered 12 tiles and left 8 slides out entirely.
- When either requested side exceeds 1920 px, the renderer scales both sides down by `1920 / max(width, height)`, which clips the other axis (1600 x 2400 came back as 1280 x 1920 with the right column cut).
- `--grid C --screenshot-height 1920` rendered every tile completely for 5 slides (C = 3) and 20 slides (C = 4); `--page a-b` combined with `--grid` restricts the sheet to those slides.

`contact_sheet.py` encodes exactly that: fewest columns of 3..6 whose rows fit 1920 px for the deck's own aspect ratio, `--page` splitting beyond that, and a PNG-header size check so a scaled render fails loudly. The child's own eyes are not a gate for this; the first E2E round proved they pass a clipped sheet.

This is the one piece of officecli behaviour the skill does encode, and the bar it cleared: the human could not do their part without it, the CLI's own defaults were the cause, and the fix is deterministic. Anything that does not clear all three belongs in the loaded skill, not here.

## Things that look like improvements and are not

- **A fidelity check over the child's output.** Shipped 2026-09-10 as `scripts/check_office_text.py` (source text against document text, with a `FAIL` relaunching the child) and removed 2026-09-11. It was not part of isolating officecli, and it cost more than it caught: three false failures in the first E2E round — a docx table folded to `[Table: N rows]` by `view text`, an xlsx display value `14.5%` compared against the raw `0.145`, chart-only numbers invisible to every view mode — made children degrade their own output to satisfy it, rewriting a native table as tab-aligned paragraphs and dropping a number format. Each repair then needed one more rule telling the child not to obey the check. Judging what the document says is the user's job, downstream of this skill.
- **Transcription or no-new-number rules in the templates.** Removed with it. They made the pptx child answer to a verbatim-style contract that the loaded deck skill does not have, which is the opposite of running that skill unchanged.
- **CLI recipes in the templates** — define Heading styles before a markdown import, create the sheet before `import`, re-apply a column format to filled cells, clone a slide before removing it. Each entered after an E2E run surfaced it, and each pins this skill to an officecli version. Checked 2026-09-11: the `word` skill never mentions `--type markdown` at all, so a recipe naming it was also steering the child away from that skill's own workflow. `officecli help` and the loaded skill are the child's sources.
- Letting the child infer the genre from the content. Decided against: the outcome varies per run and the caller knows the intent.
- Merging the three templates into one with format branches. Decided against: the child then reads two cases it is not running.
- Splitting templates per format (nine files). Decided against: the format-specific part is a table row; the design content lives in the officecli skill the child loads.
- `context: fork` on `SKILL.md`. Decided against: the fork's prompt is the skill body, which cannot select a template at runtime; inline routing plus an explicit delegation is also harness-portable.
- Reading `~/.claude/skills/officecli-*/SKILL.md` instead of `officecli load_skill`. Decided against: the two copies already differ; `load_skill` is version-matched to the installed CLI.

## Testing a change

- Frontmatter: `uv run --script <create-skill>/scripts/validate_frontmatter.py skills/officecli-isolate/SKILL.md` prints `OK`.
- Contact sheet: run `contact_sheet.py` on a 5-slide and a 20-slide deck (clone a slide with `officecli add <deck> / --from /slide[2]`); each printed sheet must be `1600x1920`, and a look at the PNG must show every tile with its bottom edge. A deck above one sheet's capacity must produce numbered sheets.
- End to end, one job per case (`create` docx, xlsx and pptx; `update-partial`; `replace`), driven by a subagent playing the caller inside an isolated worktree. Check that the caller's transcript holds no image, no `load_skill` output and no batch payload, and that every child reply is exactly the return block.
- Do not run officecli against tracked fixtures in the shared tree; outputs go under a gitignored directory.
