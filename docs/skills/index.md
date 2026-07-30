# Skill docs index

Non-runtime documentation for the standalone skills under [`skills/`](../../skills/).

These files are **never read during execution** — a skill's LLM-facing execution
context is its own `SKILL.md` (plus any `references/` inside the skill dir).
What lives here is everything a human needs *around* the skill: how to use it,
and the contracts to honour when maintaining it.

Only skills whose docs would not fit in `SKILL.md` have a directory here; the
rest are documented by their `SKILL.md` and the row in the repo
[README](../../README.md#skills).

| Skill | Docs | Audience |
|---|---|---|
| [compact-document](compact-document/) | [AUTHORING_CONTRACT.md](compact-document/AUTHORING_CONTRACT.md) | Maintainers — the shared design contract the skill's three prompt templates must jointly satisfy |
| [mode-orchestrator](mode-orchestrator/) | [index.md](mode-orchestrator/index.md) → user guide (EN/JA), workflow-spec authoring guide | End users + spec authors |
| [register-pi-tools](register-pi-tools/) | [USER_GUIDE.md](register-pi-tools/USER_GUIDE.md) / [USER_GUIDE_ja.md](register-pi-tools/USER_GUIDE_ja.md) | End users |
| [xml-wf](xml-wf/) | [README.md](xml-wf/README.md) / [README_ja.md](xml-wf/README_ja.md) (reference), [USER_GUIDE.md](xml-wf/USER_GUIDE.md) / [USER_GUIDE_ja.md](xml-wf/USER_GUIDE_ja.md) | Reference readers + end users |

## Where a doc belongs

- **Runtime** (read while the skill executes) → inside the skill dir:
  `skills/<skill>/SKILL.md`, `skills/<skill>/references/`.
- **Non-runtime** (user guides, authoring contracts, design rationale) → here:
  `docs/skills/<skill>/`.
- **Plugin-owned skills** are not indexed here — a plugin ships as its whole
  directory, so its docs live under `plugins/<plugin>/` (see each plugin's own
  `README.md`).

Classify by whether execution reads the file, not by how often it is grepped.
