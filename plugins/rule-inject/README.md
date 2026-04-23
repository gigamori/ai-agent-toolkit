# rule-inject

A Claude Code plugin that enforces reading of external rule files declared via `<rules when="..." src="..."/>` tags in `CLAUDE.md`. It deterministically blocks tool calls through the `PreToolUse` hook's `permissionDecision: "deny"` when required rule files are unread, rather than relying on the AI's self-discipline.

Works with both Claude Code and Cursor.

[日本語版 README はこちら](README_ja.md)

> Currently covers only the `PreToolUse` timing. The name `rule-inject` anticipates future support for rule injection at other timings (e.g., `UserPromptSubmit`).

## What it solves

Even when `CLAUDE.md` instructs the agent to "read `lib/prompts/development/python_uv.md` before running Python commands," the AI may execute the command without reading it. rule-inject:

1. Parses `CLAUDE.md` and extracts every `<rules when="..." src="..."/>` declaration
2. When the AI invokes a tool (`Bash` / `Write` / `Edit` / MCP), checks whether the `when` condition matches
3. If it matches and the `src` file is unread, blocks the tool call and responds with "please Read the following"
4. Once the AI reads it, the entry is removed from the pending list and subsequent calls are not blocked

## Installation (Claude Code)

### Via the plugin marketplace (recommended)

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install rule-inject@ai-agent-toolkit
```

### Local (development / testing)

```bash
claude --plugin-dir ./plugins/rule-inject
```

## Setup

Once the plugin is enabled, initialize it from the working directory:

```
/rule-inject:init
```

This will:

- Parse the nearest `CLAUDE.md` from CWD and generate `hooks/when_detection.json`
- Detect MCP servers in CWD (`.cursor/mcp.json` / `.vscode/mcp.json` / `.claude.json`) and prompt for their classification
- Merge hook entries — with the plugin's absolute path expanded — into `.claude/settings.json`

**When to re-run**:

- When CWD's `CLAUDE.md` changes
- When MCP servers are added or removed
- When the plugin is reinstalled at a different path (the cached path changes)

### Using with Cursor (manual setup)

Cursor does not recognize the Claude Code plugin structure, but it does auto-load `.claude/skills/` and `.claude/settings.json`. Perform the following one-time setup:

#### Prerequisites

- `uv` must be on the `PATH`
- Cursor's Third Party Hooks must be enabled
- On Windows, administrator rights or Developer Mode ON (required for creating symlinks)

#### Steps

1. **Create a symlink to the skill**

   `<plugin>` is the plugin's absolute path (e.g. `C:\Users\<user>\.claude\plugins\rule-inject`). From the workspace CWD:

   bash (WSL / Git Bash):

   ```bash
   mkdir -p .claude/skills
   ln -s <plugin>/skills/init .claude/skills/init
   ```

   PowerShell (administrator):

   ```powershell
   New-Item -ItemType Directory -Force .claude\skills | Out-Null
   New-Item -ItemType SymbolicLink -Path .claude\skills\init -Target <plugin>\skills\init
   ```

   cmd (administrator):

   ```cmd
   mkdir .claude\skills 2>nul
   mklink /D .claude\skills\init <plugin>\skills\init
   ```

2. **Emit the hook settings**

   From a Cursor session:

   ```
   initialize rule-inject
   ```

   The init skill is auto-detected and writes hook settings — with the plugin's absolute path expanded — into `.claude/settings.json`. Cursor's Third Party Hooks reads this `.claude/settings.json`.

3. **Verify**

   - `.claude/skills/init/SKILL.md` is reachable via the symlink
   - `.claude/settings.json` contains `hooks.PreToolUse` and `hooks.PostToolUse`, and the `command` is expanded to the plugin's absolute path

#### Known Cursor differences (found in Phase 0 validation)

The rule-inject hook scripts absorb these differences:

| Difference | Cursor | Claude Code | How it is handled |
|---|---|---|---|
| hook stdin encoding | with UTF-8 BOM | without UTF-8 BOM | scripts decode as `utf-8-sig` |
| Shell tool_name | `"Shell"` | `"Bash"` | `TOOL_NAME_ALIASES = {"Shell": "Bash"}` |
| hook_event_name | `"preToolUse"` (camelCase) | `"PreToolUse"` (PascalCase) | no runtime impact (matcher layer auto-maps) |
| deny via exit code 2 + stderr | **not supported** | supported | the hook always emits JSON with `permissionDecision: "deny"` |

#### Cursor constraints

- If the plugin is reinstalled at a different path, re-run "initialize rule-inject" to refresh the absolute path in `.claude/settings.json`
- Because symlinks are used, updates to `SKILL.md` inside the plugin propagate automatically
- Do NOT put Cursor-native tool names (`edit_file` / `delete_file` / `grep`, etc.) into matchers. Use the Claude-side names (`Write` / `Edit` / `Grep`) only — Cursor auto-maps them.

## Usage

### Declaring rules in CLAUDE.md

Declare external rules in the project root's `CLAUDE.md`:

```xml
<rules priority="2" when="running python-related commands or executing python scripts" id="python_uv" src="lib/prompts/development/python_uv.md" />
<rules priority="2" when="performing git operations" id="git" src="lib/prompts/development/git.md" />
```

- `when`: trigger keywords (natural language allowed)
- `src`: path to the file that must be read (workspace-relative)
- `id`: optional; used as a fallback trigger when `when` is omitted

`/rule-inject:init` parses this `CLAUDE.md`, cross-references the keyword-signature table in `tool_signatures.json`, and produces `when_detection.json`.

### Example: a rule that fires

Given `CLAUDE.md` with `when="running python-related commands"` → `src="lib/prompts/development/python_uv.md"`:

```
AI: runs `uv run python script.py`
→ PreToolUse hook fires
→ python_uv.md is detected as unread
→ tool call is blocked
→ AI receives: "BLOCKED: please Read the following file(s) with the Read tool and retry: lib/prompts/development/python_uv.md"
AI: Reads python_uv.md
→ PostToolUse hook removes it from the pending list
AI: retries `uv run python script.py`
→ pending list is empty → call proceeds
```

### Examples that do NOT fire

- A `<rules>` tag without both `when` and `id` — no trigger
- A `when` phrase that has no corresponding signature in `tool_signatures.json` — `/rule-inject:init` emits a WARN
- Any tool other than `Bash` / `Write` / `Edit` / MCP (e.g. `Read` itself, `Grep`) — filtered out by the matcher

### Clearing pending manually

Normally the pending list is cleared when the required files are read. To force-clear:

```bash
rm $TMPDIR/claude_rules_pending_<session_id>.txt
```

(The `<session_id>` appears in the deny reason that the AI surfaces.)

## How it works

### End-to-end flow

```
AI: invokes a tool (Bash / Write / Edit / MCP)
  │
  ├─ [PreToolUse hook: pre_tool_check_rules.py]
  │     ├─ reads session_id / tool_name / tool_input from stdin (BOM-tolerant)
  │     ├─ applies tool_name alias (Shell → Bash)
  │     ├─ walks upward from CWD to find CLAUDE.md
  │     ├─ extracts <rules when/src> tags
  │     ├─ matches against when_detection.json — adds src to pending when trigger/tool/field/pattern all match
  │     ├─ removes any src already in the reads log
  │     └─ if pending is non-empty, emits JSON permissionDecision=deny
  │
  ├─ AI: Reads the required src files as instructed
  │
  ├─ [PostToolUse hook: post_track_reads.py]
  │     └─ on a successful Read, removes file_path from the reads log and pending list
  │
  └─ AI: retries the original tool call → passes if pending is empty
```

### Hook scripts

- `hooks/pre_tool_check_rules.py` — PreToolUse, deny decision
- `hooks/post_track_reads.py` — PostToolUse, Read tracking
- `hooks/generate_when_detection.py` — builds `when_detection.json` from `CLAUDE.md` + `tool_signatures.json` (invoked by the init skill)
- `hooks/tool_signatures.json` — keyword → tool/field/pattern lookup table
- `hooks/when_detection.json` — project-specific detection rules produced by init

### State files

`tempfile.gettempdir()/claude_rules_pending_<session_id>.txt` — the unread `src` entries for that session
`tempfile.gettempdir()/claude_reads_<session_id>.txt` — absolute paths that have been Read in that session (append-only)

State is per-session, so concurrent sessions do not interfere with each other.

## Roadmap

| Phase | Work | Status |
|---|---|---|
| 0 | Validate Cursor deny semantics | ✅ Done (confirmed `permissionDecision: "deny"` is honored; identified stdin schema differences) |
| 1 | Plugin scaffold | ✅ Done |
| 2 | UTF-8 BOM handling + Shell→Bash alias | ✅ Done |
| 3 | init skill implementation | ✅ Done |
| 4 | README detailing | ✅ this README |

Low-priority TODO:

- Confirm actual `tool_name` for Cursor `Edit` / MCP invocations (unobserved in Phase 0 C-2)
- Expand keywords in `tool_signatures.json` (currently only covers python / git / SQL basics)
- Support rule injection at timings other than `PreToolUse` (e.g., `UserPromptSubmit`)

## License

[MIT](../../LICENSE)
