# Security Constraints and Troubleshooting

## Security Constraints

### Frontmatter Restrictions

Frontmatter appears in the agent's system prompt. Malicious content could inject instructions.

Forbidden:
- XML angle brackets (`<` `>`) anywhere in frontmatter values
- Skills named with platform-reserved prefixes (e.g. "claude", "anthropic")
- Characters conflicting with YAML syntax in unquoted values:
  `:`, `#`, `[`, `]`, `{`, `}`, `,`, `&`, `*`, `!`, `|`, `>`, `'`, `"`, `%`
- Code execution constructs in YAML

Allowed:
- Any standard YAML types (strings, numbers, booleans, lists, objects)
- Custom metadata fields
- Long descriptions (up to 1024 characters)

Required — quote string values:
- **Wrap every string value in double quotes**, especially `description`. This neutralizes the YAML-conflicting characters listed above, including a `:` followed by a space or a `#` mid-value, and any indicator character (`# > | - ! & * ? : [ ] { } @ %`) at the *start* of a value.
- Do not place a raw `"` inside a double-quoted value; escape it as `\"` or omit it.
- Example: `description: "Processes X. Supported: A / B / C."` — the unquoted form `description: Processes X. Supported: A / B / C.` fails to parse at `Supported:`.
- After authoring, run `scripts/validate_frontmatter.py` (see SKILL.md Phase 4) to confirm the frontmatter parses.

### Folder Rules

- No `README.md` inside the skill folder (all documentation goes in SKILL.md or references/)
- Folder name must match the `name` field in frontmatter

## Troubleshooting

### Skill won't upload

Error: "Could not find SKILL.md in uploaded folder"
- Cause: File not named exactly `SKILL.md` (case-sensitive)
- Solution: Rename to `SKILL.md`. No variations accepted (`SKILL.MD`, `skill.md`, etc.)

Error: "Invalid frontmatter"
- Cause: YAML formatting issue
- Common mistakes:
  - Missing `---` delimiters
  - Unclosed quotes in description
  - XML angle brackets in values

Error: "Invalid skill name"
- Cause: Name has spaces, capitals, or underscores
- Solution: Use kebab-case only (`my-cool-skill`, not `My Cool Skill`)

### Skill doesn't trigger

Symptom: Skill never loads automatically

Fix checklist:
- Is the description too generic? ("Helps with projects" won't trigger)
- Does it include trigger phrases users would actually say?
- Does it mention relevant file types if applicable?

Debugging approach: Ask the agent "When would you use the [skill name] skill?" and adjust based on what's missing.

### Skill triggers too often

Symptom: Skill loads for unrelated queries

Solutions:
1. Add negative triggers: `"Do NOT use for simple data exploration (use data-viz skill instead)."`
2. Be more specific: `"Processes PDF legal documents for contract review"` instead of `"Processes documents"`
3. Clarify scope: `"Use specifically for online payment workflows, not for general financial queries."`

### Instructions not followed

Common causes:
1. Instructions too verbose — keep concise, use bullet points, move detail to references/
2. Instructions buried — put critical instructions at the top, use `## Important` headers
3. Ambiguous language — replace `"validate things properly"` with explicit checklist items
4. For critical validations, consider bundling a script rather than relying on language instructions

### Large context issues

Symptom: Skill seems slow or responses degraded

Solutions:
1. Move detailed docs to references/, keep SKILL.md under 500 lines / 5,000 words
2. Evaluate if too many skills are enabled simultaneously (20-50 threshold)
3. Use progressive disclosure — link to references instead of inline
