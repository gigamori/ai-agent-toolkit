# Instruction document formats

Three small markdown formats. The caller writes them; the child reads them. A field the format does not define is ignored, so do not rely on prose outside the fields.

## A. Partial-update instructions (`update-partial`)

One `## <id>` block per edit. `<id>` is any short token; it is echoed in `TOUCHED` / `BLOCKED`.

| Field | Required | Content |
|-------|----------|---------|
| `location` | yes | Where. One of the three forms below |
| `value` | one of `value` / `prop` | New text for the element. Multi-line: a fenced block on the following lines |
| `prop` | one of `value` / `prop` | `key=value`, one per line, for property changes (`fill=FF0000`, `bold=true`, `width=3cm`) |
| `tracked` | no | docx only. Author name; the edit becomes a tracked change (`revision.author`) |

Location forms:

| Form | Example | Resolution by the child |
|------|---------|-------------------------|
| officecli path | `/body/p[3]`, `/slide[2]/shape[1]`, `/Sheet1/B4` | `officecli get <path> --json`; the child records the stable-id form before editing |
| stable-id path | `/body/p[@paraId=1A2B3C4D]`, `/slide[2]/shape[@id=550950021]`, `/slide[2]/shape[@name=Title 1]` | Used as is |
| `find:` text | `find:Q3 revenue` | `officecli query` / `set --find`; the match must be unique, otherwise the edit is `BLOCKED` |

Example:

```markdown
## 1
location: /body/p[@paraId=1A2B3C4D]
value: Revenue grew 25% year over year.

## 2
location: find:DRAFT
value: FINAL
tracked: Alice

## 3
location: /slide[4]/shape[@name=Title 1]
prop: color=1F3864
prop: bold=true
```

## B. xlsx layout instruction (`create` xlsx, optional)

Sections are headings; each holds one line per item. Omitted sections take officecli's defaults.

```markdown
## sheets
data.csv -> /Data header=yes start=A1
summary.csv -> /Summary header=yes start=B2

## columns
/Data/A width=12 numFmt=yyyy-mm-dd
/Data/C width=14 numFmt=#,##0
/Data/D numFmt=0.0%

## freeze
/Data A2

## autofilter
/Data A1:D1

## charts
/Summary type=bar data=/Summary/A1:B6 anchor=D2,10,6 title=Revenue by region

## print
/Data orientation=landscape fit=width
```

`## sheets` is the only section the child needs; one CSV is one sheet. Without the section, one CSV named `x.csv` becomes sheet `/x` with a header row.

`header=yes` is officecli's `import --header`, which also sets an autofilter on the header row and freezes it. `## autofilter` and `## freeze` override that; without them, the import's own autofilter and freeze stay.

A `## columns` number format reaches both the column style and the cells the data already occupies, so the whole column displays alike.

## C. Replace scope (`replace`)

- `docx`: no instruction document. The whole body is replaced by the content markdown.
- `xlsx`: a `## sheets` section in the form of B, mapping each CSV to the sheet (and start cell) whose data region it replaces. Sheets not listed are kept untouched.
- `pptx`:

  ```markdown
  ## slides
  range: 4-7
  keep-layout: yes
  ```

  `range` is 1-based and inclusive; the content markdown replaces exactly those slides, and the new slides are inserted at the same position. `keep-layout: yes` tells the child to reuse the removed slides' layouts and theme; `no` lets it pick layouts from the deck's masters.
