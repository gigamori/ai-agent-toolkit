# officecli-isolate — docs

Non-runtime documentation for the `officecli-isolate` skill (`skills/officecli-isolate/`).
Nothing here is read while the skill executes; the execution context is the skill's
`SKILL.md`, its three `*/prompt.md` templates and `references/`.

- **[USER_GUIDE.md](USER_GUIDE.md)** — what it does, the three cases and how they are
  picked, genre, the instruction documents, what comes back. Japanese:
  [USER_GUIDE_ja.md](USER_GUIDE_ja.md).
- **[AUTHORING_CONTRACT.md](AUTHORING_CONTRACT.md)** — maintenance obligations: why the
  skill is a containment shell around officecli's own skills, what the three prompt
  templates must jointly satisfy, the return block, what the fidelity script sees and
  cannot see, and how to test a change.
