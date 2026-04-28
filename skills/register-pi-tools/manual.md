# register-pi-tools — User Manual

User-facing manual for the `register-pi-tools` skill: a workflow that converts Python scripts into Anthropic API tools through frontmatter `args` (JSON Schema) and a `tools.yaml` registry. The LLM-facing spec lives in `SKILL.md` next to this file. (Japanese version: `manual_ja.md`.)

## How it fits together

```
[ <input_dir>/*.py source scripts ]
       ↓ frontmatter `args:` (JSON Schema) is the single source of truth
       ↓
[ build_tools_yaml.py ]   ← in this skill's scripts/ folder
       ↓ walks every .py, aggregates each frontmatter into one entry
       ↓
[ <output_path> = ~/.pi/agent/tools.yaml ]   ← build artifact, do not hand-edit
       ↓
[ Dispatcher (dispatch_tools.py / pi extension / ...) ]
       ↓ load_tools → to_anthropic_tools → request.tools
       ↓ on tool_use, spawn entry.command + pipe JSON to stdin
[ Anthropic API ]
```

## Three-step setup

### 1. Author or migrate scripts

Write Python scripts as you normally would. The argument-acquisition layer must follow:

- declare the schema in a YAML frontmatter `args:` block
- use `from _tool import args`
- do not use argparse

The detailed per-script spec is in `SKILL.md` § "Per-script migration loop".

To migrate existing argparse scripts, **invoke this skill**:

```
User: "Use register-pi-tools to migrate <directory>"
LLM  → skill auto-triggers → confirms input_dir / output_path → migrates each .py
```

### 2. Build the registry

Once every script under the target directory is migrated, generate `tools.yaml`:

```bash
uv run --with pyyaml python ~/.claude/skills/register-pi-tools/scripts/build_tools_yaml.py \
  --input-dir <directory containing scripts> \
  --output-path ~/.pi/agent/tools.yaml
```

Arguments:

| Key | Required | Default | Meaning |
|---|---|---|---|
| `--input-dir` | ✓ | — | Directory to walk recursively. `_*.py` and `ignore-old/` are auto-excluded |
| `--output-path` | — | `~/.pi/agent/tools.yaml` | Output yaml file path (tilde expansion supported) |

Output entry format:

```yaml
- name: extract_frontmatter
  description: Extract frontmatter from files
  input_schema:
    type: object
    required: [file_or_dir]
    properties:
      file_or_dir: {type: string, description: ...}
      ...
  command: uv run python C:/home/doc/prompts/lib/src/extract_frontmatter.py
```

`tools.yaml` is a **build artifact**. Never edit it by hand — it is overwritten on the next build.

### 3. Call tools through a dispatcher

#### From Python

```python
import sys
sys.path.insert(0, "/path/to/lib/src")  # directory containing dispatch_tools.py
from dispatch_tools import load_tools, to_anthropic_tools, dispatch

entries = load_tools("~/.pi/agent/tools.yaml")
tools_for_api = to_anthropic_tools(entries)  # ready for request.tools

# When a tool_use reply arrives:
result = dispatch("extract_frontmatter", {"file_or_dir": "lib/src/_tool.py", "json": True}, entries)
print(result)  # str (UTF-8 stdout)
```

#### CLI smoke test (run_tool.py)

```bash
uv run python /path/to/lib/src/run_tool.py \
  --tool extract_frontmatter \
  --input '{"file_or_dir":"lib/src/_tool.py","json":true}'
```

Arguments:

| Key | Required | Default | Meaning |
|---|---|---|---|
| `--tool` | ✓ | — | The `name` registered in tools.yaml |
| `--input` | — | `"{}"` | JSON string fed to the tool's stdin |
| `--tools-yaml` | — | `lib/tools.yaml` | tools.yaml path (override as needed) |
| `--timeout` | — | none | Execution timeout in seconds |

#### Through pi (future)

A thin TypeScript extension that loads `tools.yaml` and calls `pi.registerTool({...})` is planned. No pi-mono PR is required — the public extension API is sufficient.

#### Claude Code compatibility (not direct; MCP server only)

`tools.yaml` cannot be loaded directly into Claude Code. Claude Code's tool registry is internal — neither slash commands, skills, nor hooks expose an API to inject entries into the Anthropic API `request.tools` array. The `UserPromptSubmit` hook can only add text context, not structured tool definitions.

The supported route is to wrap `tools.yaml` in an **MCP server** (Model Context Protocol). The server exposes each entry via `ListTools` / `CallTool`, and Claude Code (or any MCP-aware client) auto-registers them once configured in `settings.json` `mcpServers`. The dispatch logic (load yaml, spawn `entry.command`, pipe JSON to stdin) is the same as the Python `dispatch_tools.py` and the planned pi extension — only the surface protocol differs. This is a separate implementation track from the pi extension; sharing a thin core library across both is feasible.

## Files in this skill

| Path | Role |
|---|---|
| `~/.claude/skills/register-pi-tools/SKILL.md` | LLM-facing spec (auto-trigger) |
| `~/.claude/skills/register-pi-tools/manual.md` | This file (English, human-facing) |
| `~/.claude/skills/register-pi-tools/manual_ja.md` | Japanese version of this manual |
| `~/.claude/skills/register-pi-tools/scripts/build_tools_yaml.py` | Registry builder |
| `~/.claude/skills/register-pi-tools/scripts/_tool.py` | Bundled runtime helper |
| `~/.pi/agent/tools.yaml` | Generated registry (build artifact) |

The dispatcher (`dispatch_tools.py` / `run_tool.py`) lives in the consuming project (e.g. `lib/src/`). This skill is responsible for migration and the registry build only.

## Troubleshooting

### `Error: $: missing required field 'XXX'`

A field declared in `args.required` was not supplied. Pass it via the stdin JSON object or `--xxx value` on the command line.

### `Error: $: unknown field 'YYY'`

A key was passed (via stdin JSON or argv) that is not declared in `args.properties`. Check for typos. To intentionally allow extra keys, add `additionalProperties: true` to the schema.

### `invalid tool name 'foo bar' in /path/to/script.py`

The frontmatter `name` does not match `^[a-zA-Z0-9_-]{1,64}$` (Anthropic API constraint). Use only alphanumerics, hyphens, and underscores.

### `lost sys.stderr` / `I/O operation on closed file`

The script wraps Windows stdout with `io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")`. `_tool` already reconfigures the streams; double-wrapping the same buffer fails on shutdown. Replace it with `sys.stdout.reconfigure(encoding="utf-8")`.

### YAML parse error: `mapping values are not allowed here`

A `description` value contains `:` or `{` `}` while written in flow style. Wrap the value in double quotes or rewrite in block style.

### `tools.yaml` is stale

Re-run `build_tools_yaml.py`. Never hand-edit — the next build overwrites it.

## Dependencies

- Python 3.10+
- `pyyaml` (use `uv run --with pyyaml` ad-hoc, or `uv pip install pyyaml` to persist in a venv)
- Each migrated script has its own dependencies listed in its frontmatter `usage` line

## Related docs

- LLM-facing spec for this skill: `SKILL.md` next to this file
- pi-mono extension API: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
