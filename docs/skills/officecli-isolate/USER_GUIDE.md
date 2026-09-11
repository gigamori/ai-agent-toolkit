# officecli-isolate — User Guide

Create or edit a Word, Excel or PowerPoint file through officecli's own document skills, without the work showing up in your session. The skill launches a child session per file, tells it which officecli skill to load, hands it your inputs, and takes back a short fixed report. Screenshots, layout trial-and-error, CLI help output and batch payloads stay in the child.

**Invocation**: the skill runs only when you call it explicitly as `/officecli-isolate`; the agent never triggers it on its own.

## What you provide

| Input | When | What |
|-------|------|------|
| Format | always | `docx`, `xlsx` or `pptx` |
| Content | creating, replacing | `docx` / `pptx`: one markdown file. `xlsx`: one CSV per sheet |
| Original | updating, replacing | The existing file. It is never edited; the child works on a copy at the output path |
| Instruction document | updating (required); creating `xlsx` (optional layout); replacing `xlsx` / `pptx` (required scope) | A small markdown file; formats below |
| Output path | optional | Defaults to the content's or original's directory and stem (`-out` suffix for updates) |
| Genre | optional | The officecli skill to load. Defaults to the format's base skill |

## The three cases

The case is decided by which inputs exist, not by how you phrase the request.

| You give | Case | What the child does |
|----------|------|---------------------|
| content only | **create** | Builds a new file. `docx` / `xlsx` are transcribed by officecli's native markdown / CSV ingest; `pptx` is authored under the loaded deck skill and may be rewritten for the medium |
| original + instruction document | **update-partial** | Applies exactly the listed edits to a copy, resolving each location to a stable-id path, and returns those ids |
| original + content (+ scope for `xlsx` / `pptx`) | **replace** | Replaces the body (`docx`), the mapped sheets' data regions (`xlsx`) or a slide range (`pptx`) on a copy, keeping styles, headers and footers, formulas, charts, theme and masters |

Give a combination outside that table and the skill asks which case you mean rather than guessing.

## Genre

officecli ships one base skill per format and several scene layers on top of them. The child loads exactly one, and it never picks it from the content: you name it, or the base is used.

| Format | Default | Others you can name |
|--------|---------|---------------------|
| docx | `word` | `academic-paper`, `word-form` |
| pptx | `pptx` | `pitch-deck`, `morph-ppt`, `morph-ppt-3d` |
| xlsx | `excel` | `financial-model`, `data-dashboard` |

## Instruction documents

The full formats are in the skill's `references/instruction-formats.md`.

**Partial update** — one block per edit:

```markdown
## 1
location: find:DRAFT
value: FINAL
tracked: Alice
```

`location` is an officecli path (`/body/p[3]`), a stable-id path (`/slide[2]/shape[@id=550950021]`), or `find:<text>` that must match exactly once. `value` sets text; `prop: key=value` sets properties; `tracked` (docx) makes it a tracked change.

**xlsx layout** (optional when creating): sections `sheets`, `columns`, `freeze`, `autofilter`, `charts`, `print`, one line per item.

**Replace scope**: `xlsx` a `sheets` section mapping each CSV to a sheet; `pptx` a `slides` section with `range: 4-7` and `keep-layout: yes|no`. `docx` needs none.

## What comes back

The child's whole report is one block:

```
STATUS: done | blocked
OUTPUT: <path>
GATES: validate=ok issues=0 gate3=PASS
TOUCHED: <stable path> : <old → new>
BLOCKED: <edit> : <reason> | none
CONTACT_SHEET: <png path> | n/a
DECLARATION: <a few sentences on structure, what was kept, anything it could not keep intact>
```

Then the caller re-runs the child's own two gate commands on the saved file, `officecli validate` and `view issues`, and tells you whether they match what the block claims. Nothing else about the document is checked for you: what it says and how it looks are the loaded officecli skill's job, then yours.

For `pptx` you are asked to look at the contact sheet and judge readability. The caller does not open it; that judgement is yours, and your remarks go back to the child. The sheet is made by the skill's own script, which sizes the grid from the slide count so that no row is cut off; a deck too large for one sheet comes as `-1.png`, `-2.png`, ... in slide order.

## What it does not do

- Check the content itself. The child follows officecli's own skill for the format, which owns structure, wording and design, and runs that skill's delivery gates. Whether the result says what you meant is your call, on the file and the contact sheet.
- The caller never runs officecli's design or edit commands itself, never reads the child's screenshots, and never edits your content, original or instruction files. If the child reports an input as insufficient, you fix the input.
- One child handles one file. Two files are two launches.
