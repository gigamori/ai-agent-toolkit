---
name: rule-inject:init
description: Initialize rule-inject hooks for the current workspace. Generates when_detection.json from the project's CLAUDE.md and merges absolute-path hook entries into .claude/settings.json. Required for Cursor; optional for Claude Code. Re-run whenever CLAUDE.md changes or MCP servers are added/removed.
---

# rule-inject init

Wire the rule-inject harness into the project in CWD. After running this, external rules declared via `<rules when="..." src="..."/>` in `CLAUDE.md` will be enforced (read-required) whenever a matching tool fires.

## Step 1: Locate the plugin's absolute path

Resolve in this order:

1. If the environment variable `$CLAUDE_PLUGIN_ROOT` is available, use it.
2. Otherwise, compute two levels up from this `SKILL.md` (i.e., `.../rule-inject/`).
3. If neither works, ask the user.

Refer to this value as `<PLUGIN>` below.

## Step 2: Detect MCP servers in CWD

Look in the following order; read `mcpServers` or `mcp_servers` from the first settings file that exists:

1. `<CWD>/.cursor/mcp.json`
2. `<CWD>/.vscode/mcp.json`
3. `<CWD>/.claude.json`
4. `~/.claude.json`

Store the extracted server names in `MCP_SERVERS: list[str]`. If none are found, use an empty list.

## Step 3: Update the mcp_servers map in tool_signatures.json (only if MCP was detected)

If MCP servers were found in Step 2:

1. Read `<PLUGIN>/hooks/tool_signatures.json`.
2. Present the detected server names to the user and ask which category each belongs to (`database` / other).
3. Update the `mcp_servers` key in the form `{"database": ["srv1", ...], ...}` based on the answers.
4. Write the file back. Do NOT touch the existing `signatures` array.

If the user replies "skip MCP classification," you may skip this step.

## Step 4: Generate when_detection.json

Run `<PLUGIN>/hooks/generate_when_detection.py` with CWD's `CLAUDE.md` as the source.

```bash
uv run python <PLUGIN>/hooks/generate_when_detection.py
```

- With no argument, it walks upward from CWD to find `CLAUDE.md`.
- To point to a specific file, pass its absolute path as the first argument.

Take note of the `Recommended PreToolUse matcher` line at the end of the output (used in Step 5). If no such line is emitted, use the static matcher `Bash|Write|Edit`.

## Step 5: Merge hooks into .claude/settings.json

1. Read `<CWD>/.claude/settings.json` (treat as new if missing or `{}`).
2. Read `<PLUGIN>/hooks/hooks.json` and substitute the literal string `${CLAUDE_PLUGIN_ROOT}` with `<PLUGIN>` (from Step 1) to produce the expanded JSON.
3. If Step 4 produced a `Recommended matcher`, replace the PreToolUse matcher (`Bash|Write|Edit`) in the expanded JSON with that value.
4. Merge into `settings.json.hooks` in CWD:
   - If an array for the same event (`PreToolUse` / `PostToolUse`) already exists → append each entry. Skip entries whose `command` is already present.
   - If the event name does not yet exist → add it.
5. Write the file back with 2-space indentation.

Notes:

- Keep the plugin-side `hooks/hooks.json` untouched (Claude Code continues to use the plugin form directly).
- Cursor operation depends on the absolute-path hooks now in `.claude/settings.json`.
- Cursor's hook stdin arrives with a UTF-8 BOM, but the plugin's hook scripts already handle both via `utf-8-sig` decode.

## Step 6: Report completion

Tell the user:

- The plugin's absolute path
- How many MCP servers were detected and whether the classification was applied
- The number of detection entries produced in `when_detection.json` (from `generate_when_detection.py` stdout)
- The PreToolUse matcher string written to `.claude/settings.json`
- **When to re-run**: whenever CWD's `CLAUDE.md` changes, or when MCP servers are added or removed.

## Error handling

- Step 4 — `CLAUDE.md` not found → ask the user to create `CLAUDE.md` and re-run init afterward.
- Step 5 — write-permission error → ask the user to create `.claude/settings.json` manually and re-run.
- `uv` not on PATH → ask whether to fall back to `python` before substituting.
