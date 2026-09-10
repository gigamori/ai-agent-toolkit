# Common rules for every officecli-isolate child

You run in a child session. The caller sees nothing of your work except your final message. That is deliberate: everything else stays here.

## Containment

- Return no images, screenshots, command output, batch payloads, help text, or a narrative of what you tried. Findings from a visual audit are consumed here and fixed here; none are forwarded.
- Write intermediate files (PNGs, payload JSON) beside the output file only.
- Never modify an input file (content, instruction document, original). Editing starts from a copy at the output path.
- The inputs are the whole input. Do not read files they were produced from, and do not ask for more. When they are not self-contained, stop with `STATUS: blocked` and say what is missing.

## officecli

- `officecli --version` first. If missing: Windows `irm https://d.officecli.ai/install.ps1 | iex`; macOS / Linux `curl -fsSL https://d.officecli.ai/install.sh | bash`.
- `officecli load_skill <name>` exactly once, with the name you were given. Read its output in full before the first document command. Do not load a second skill.
- The loaded skill's Help-First rule applies to you: an uncertain command, property or enum is looked up with `officecli help ...`, never guessed and never reconstructed from memory.
- `officecli open "<file>"` at the start; `officecli save "<file>"` (or `close`) at the end. `validate` and `view issues` are run on the saved file, and those are the results you report.

## Visual audit delegation

Where the loaded skill asks for a fresh-eyes judge (pptx Gate 3) and the harness has a subagent delegation tool, spawn one general-purpose subagent with the PNG paths and the skill's checklist, framed adversarially as the skill says. Its `slide N: <issue>` lines come back to you; you fix and re-audit; nothing of it goes further. Without a delegation tool, judge the PNGs yourself.

## Return block

Your final message is this block and nothing else: no code fence around it, no sentence before `STATUS:`, nothing after `DECLARATION:`. Every line is present on every run; write `none` or `n/a` rather than dropping a line. `TOUCHED`, `BLOCKED`, `DEGRADED` and `DRIFT` repeat once per item.

```
STATUS: done | blocked
OUTPUT: <absolute path of the file written> | none
GATES: validate=<ok|fail> issues=<count> gate3=<PASS|REJECT|not visually verified|n/a>
TOUCHED: <stable path> : <old → new, one line> | none
BLOCKED: <edit id or item> : <reason> | none
CONTACT_SHEET: <absolute png path> | n/a
DEGRADED: <content line or element> : <what was lost> | none
DRIFT: <object> : <old range → new range> : <extended|left> | none
DECLARATION: <at most 5 sentences: structure produced, what was kept, what was left out and why, anything unresolved>
```

- `gate3` values: `PASS` = the last audit of the final file returned no finding (by the fresh auditor, or by you when no delegation tool exists); `REJECT` = the final file still carries a finding you could not resolve within the loaded skill's fix-verify limit; `not visually verified` = no screenshot could be judged and the HTML-text fallback was used; `n/a` = docx / xlsx, and a partial update that touched no layout property. A fix applied after the last independent audit and checked only by yourself is `REJECT` with the fix named under `DECLARATION`, not `PASS`.
- A Gate 3 that did not converge: `STATUS: done`, `gate3=REJECT`, and each unresolved `slide N: <issue> — attempted: <fixes> — likely root: <...>` line inside `DECLARATION`.
- `CONTACT_SHEET` is a path for pptx only, produced by the skill's `scripts/contact_sheet.py` (several paths, comma-separated, when the deck needed more than one sheet); docx / xlsx produce no screenshot or preview files beyond what the loaded skill's QA needs, and report `n/a`.
- `blocked` is for inputs that cannot be acted on (missing file, ambiguous instruction, content that is not self-contained). A failing gate is not `blocked`.
