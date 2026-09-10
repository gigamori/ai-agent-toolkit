---
name: officecli-isolate
disable-model-invocation: true
description: "Run officecli's official document skills (word / pptx / excel and their scene layers such as pitch-deck, academic-paper, financial-model, data-dashboard) inside a child session so that layout trial-and-error, screenshots and CLI output never enter the caller's context. Three cases, picked by input shape: create a new docx / xlsx / pptx from markdown or CSV, partially update an existing file from an instruction document, or replace the body / data region / a slide range while keeping the file's styles, formulas, theme and masters. The child loads exactly one officecli skill named by the caller, runs that skill's Delivery Gates, and returns a fixed block (paths, gate results, touched element ids, contact-sheet path); the caller re-checks fidelity with a script. Use when an Office document must be created or edited through officecli without polluting the current session, or when the user mentions officecli-isolate, child session for officecli, or context-isolated document generation."
---

# Isolate officecli

Every officecli document job runs in a **child session**. The caller (this session) resolves inputs, picks one prompt template, launches one child per artifact, and checks what comes back. Design rules, CLI recipes and QA gates are not restated here: the child loads them with `officecli load_skill <name>`.

## Inputs

| Input | Required | Content |
|-------|----------|---------|
| Format | yes | `docx` / `xlsx` / `pptx` |
| Content | create, replace | `docx` / `pptx`: a markdown file. `xlsx`: one CSV per sheet |
| Original | update-partial, replace | The existing Office file. Never edited in place; the child copies it |
| Instruction document | update-partial (required), create `xlsx` (optional), replace `xlsx` / `pptx` (required) | Markdown, formats in [instruction-formats.md](references/instruction-formats.md) |
| Output path | no | Default: same directory and stem as the content or original, suffix `-out` for updates |
| Genre | no | An officecli skill name. Default: the format's base skill (below) |

## Case selection

Decide from the shape of the inputs, not from wording:

| Original given | Content given | Instruction given | Case |
|---|---|---|---|
| no | yes | xlsx: optional | `create` |
| yes | no | yes | `update-partial` |
| yes | yes | xlsx / pptx: yes | `replace` |

Any other combination (original + content + a partial-update instruction, no original and no content, ...) is ambiguous. Ask which case is meant; do not pick one.

## Genre resolution

The child never infers the genre. Resolve it here and pass one name.

| Format | Base (default) | Scene layers the caller may name instead |
|--------|----------------|------------------------------------------|
| docx | `word` | `academic-paper`, `word-form` |
| pptx | `pptx` | `pitch-deck`, `morph-ppt`, `morph-ppt-3d` |
| xlsx | `excel` | `financial-model`, `data-dashboard` |

The user's explicit genre wins. Without one, use the base. A name outside this table: confirm it exists with `officecli load_skill` (the bare command lists every installed name) before passing it; unknown → ask.

## Launch

1. `officecli --version`. If missing, install (Windows: `irm https://d.officecli.ai/install.ps1 | iex`; macOS / Linux: `curl -fsSL https://d.officecli.ai/install.sh | bash`) and re-check.
2. Read the case's template: `create/prompt.md`, `update-partial/prompt.md` or `replace/prompt.md` (this skill's directory).
3. Resolve every `{...}` variable in it:

   | Variable | Value |
   |----------|-------|
   | `{format}` | `docx` / `xlsx` / `pptx` |
   | `{genre}` | the resolved officecli skill name |
   | `{content_path}` | absolute path of the markdown / CSV (`none` for update-partial; several CSVs: comma-separated) |
   | `{original_path}` | absolute path of the existing file (`none` for create) |
   | `{instruction_path}` | absolute path of the instruction document, or `none` |
   | `{output_path}` | absolute path of the file to write |
   | `{findings}` | `none` on the first attempt; on a relaunch, the violation lines from step 3 of "After return" |
   | `{skill_dir}` | absolute path of this skill's directory |

4. Launch **one general-purpose subagent per artifact** with the resolved template as its entire prompt. No model override. Pass nothing else: no conversation excerpts, no upstream files. The template tells the child to read [common.md](references/common.md) and the instruction formats itself.
5. The child's final message is the only thing that returns. Do not read the child's intermediate files (screenshots, batch payloads, help output).

## After return

1. The final message must be the return block defined in [common.md](references/common.md) (`STATUS:` first, `DECLARATION:` last, every line present; a code fence around the block is tolerated). A malformed block: relaunch once with `{findings}` = `return block incomplete: <missing lines>`. Still malformed → report to the user and stop.
2. `STATUS: blocked` → report the reason to the user; nothing else to check.
3. Re-check what the child reported, cheaply, on the saved file:

   ```bash
   officecli validate "<output>"
   officecli view "<output>" issues
   ```

   Then the fidelity script (this is the check the child's self-report cannot replace). `${CLAUDE_SKILL_DIR}` is this skill's directory, the same path as `{skill_dir}`:

   | Case | Command |
   |------|---------|
   | create / replace, docx / xlsx | `uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_office_text.py "<output>" --source "<content>" --mode verbatim` |
   | create, pptx | `uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_office_text.py "<output>" --source "<content>" --mode rewrite` |
   | replace, pptx | `uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_office_text.py "<output>" --source "<content>" --mode rewrite --slides <range>` (the `range:` of the scope document) |
   | update-partial | `uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_office_text.py "<output>" --instruction "<instruction>"` |

   Several CSVs: repeat `--source` per file. `xlsx` replace: `--source` per mapped CSV. The script reads native table cells, chart data and raw cell values, so a value the text view hides still counts; a `FAIL` names a value that is absent from the file.
4. `FAIL` → relaunch the same case once with the violation lines as `{findings}`. A second `FAIL` → report the lines to the user and stop.
5. `pptx`: hand the `CONTACT_SHEET` path to the user and ask them to judge readability. Do not open the image yourself. When `GATES` carries `gate3=REJECT`, hand over the unresolved `slide N: ...` lines from `DECLARATION` with it; the user decides whether to accept the deck or send those lines back. Remarks go back to the child as `{findings}` (one more launch; the re-run passes step 3 again).

## Report to the user

- The return block, verbatim
- `validate` / `view issues` results from step 3
- The script's verdict and its listings (`rewrite` mode lists headings not found and numbers not in the source; they are for the reader, not a verdict)

## What the caller never does

- Run officecli design or edit commands itself for this job. All document work is the child's.
- Read screenshots, PNGs, batch payloads or `load_skill` output. One image in this context is what this skill exists to prevent.
- Launch one child for two artifacts, or stack two genres on one artifact.
- Edit the content, original or instruction files. If the child reports them insufficient, the user fixes them.
