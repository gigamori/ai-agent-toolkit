# Developer docs index

Repository-level developer documentation — the material that belongs to no single
skill or plugin. Nothing here is read during execution.

| Doc | What it is |
|---|---|
| [test-gate.md](test-gate.md) | The test-knowledge gate over `skills/`: its two rules, how to run it, its coverage boundary, and the migration record |
| [test-constraints.md](test-constraints.md) | Facts about the world outside this repository that the `skills/` test suites depend on — host platform, third-party CLIs, measured model behaviour |

Per-skill non-runtime docs live under [`docs/skills/`](../skills/index.md); a
plugin's docs ship inside `plugins/<plugin>/`.
