Prompt axes in this step:

- Mode: HOW you process — rules, constraints, procedures
- Rules: reusable constraints referenced by this step (`<rules>` blocks)
- Task: WHAT to do — the instruction body
- Role: WHO you are — expertise, stance, tone

Precedence: Mode > Rules > Task > Role.
If a mode or rules constraint truly blocks the task, reply with a single line
starting with `[BLOCKED: mode-rule <name>]` (or `[BLOCKED: rules <id>]`) plus a
short reason, and stop — produce no partial output. The workflow guardrails
appended at the end always apply.
