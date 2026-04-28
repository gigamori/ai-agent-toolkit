---
name: register-pi-tools
description: Migrates Python scripts under a directory to use frontmatter args (JSON Schema) and the _tool.args() runtime, then builds a tools.yaml registry that pi (or any compatible LLM caller) can load to register each script as an Anthropic API tool. Use when the user asks to register Python scripts as pi tools, migrate argparse to _tool, add frontmatter args to scripts, or build/refresh tools.yaml.
---

# Register Pi Tools

Convert existing Python scripts under a chosen directory so each one carries a YAML frontmatter `args` block (JSON Schema) and uses `from _tool import args` for argument parsing. After migrating all files, run the bundled build script to emit a `tools.yaml` registry that downstream LLM-callers (pi extension, MCP server, or custom) consume to construct the Anthropic API `tools` array.

Do not read `manual.md` / `manual_ja.md` directly — they are user-facing only.

## Inputs to confirm before doing anything

Always confirm both with the user before starting work:

- `input_dir`: directory whose `*.py` files are migrated and registered (recurses; `_*.py` and `ignore-old/` are skipped). No default — ask if not given.
- `output_path`: target file for the registry. Default: `~/.pi/agent/tools.yaml`. Confirm before writing.

## Per-script migration loop

For each `*.py` under `input_dir` (excluding `_*` private modules and `ignore-old/`):

### Step 1 — Survey

Read the file and identify the current argument-acquisition style:
- Standard `argparse.ArgumentParser` + `parser.parse_args()`
- Hand-rolled `sys.argv` parsing
- No arguments at all

For each argument, extract: name (argparse `dest` or existing variable), type (`string` / `boolean` / `integer` / `number` / `array` / `object`), required vs optional, default value, one-line description.

### Step 2 — Add or update the frontmatter

Place this block immediately after the shebang. The `args` block is the single source of truth for the argument schema; `_tool.args()` consumes it at runtime, and the registry uses it as the Anthropic `input_schema`.

    # ---
    # name: <filename without extension>
    # description: <one-line summary, max ~60 chars>
    # usage: uv run python <relpath> [args...]
    # args:
    #   type: object
    #   required: [<keys that must be present>]
    #   properties:
    #     <key>:
    #       type: string | boolean | integer | number | array | object
    #       default: <optional default>
    #       description: <one line>
    # ---

A no-args script must still declare an empty block:

    # args:
    #   type: object
    #   properties: {}

Constraints:
- `name` must match `^[a-zA-Z0-9_-]{1,64}$` (Anthropic API constraint; the build halts on violation).
- YAML pitfall: when a `description` value contains `: ` or `{ }`, wrap it in double quotes or use block style. Inline flow-mapping `{type: ..., description: ...}` with such characters fails parsing.

### Step 3 — Replace argparse with `_tool.args()`

Drop `import argparse` and add:

    from _tool import args

Replace the parser block with:

    a = args()
    foo = a["required_field"]
    bar = a.get("optional_field", default_value)

For large scripts with many `args.foo` references, use a `SimpleNamespace` shim instead of rewriting every reference:

    from types import SimpleNamespace
    from _tool import args as _read_args

    def main():
        args = SimpleNamespace(**_read_args())
        # existing args.foo references keep working

Translate argparse details into the schema:
- `nargs="+"` → `type: array, items: {type: string}` (required if no default)
- `action="store_true"` → `type: boolean, default: false`
- Custom `dest=` (e.g. `dest="as_json"`) → keep the schema key in its natural form (`json`) and rebind locally: `as_json = a["json"]`
- Short flags (`-o/--output`) → keep only the long form; `_tool` does not parse short flags

If the script does Windows stdout/stderr UTF-8 setup, use `sys.stdout.reconfigure(encoding="utf-8")` rather than wrapping `sys.stdout.buffer` with a fresh `TextIOWrapper`. `_tool` already reconfigures the streams; a second wrapper around the same underlying buffer causes `lost sys.stderr` on shutdown.

### Step 4 — Verify (all three checks must pass)

Always invoke through `uv` and resolve every third-party import the script makes by passing `--with <pkg>` (one per dependency) or `--with-requirements <file>`. Never skip verification because of `ModuleNotFoundError`; missing imports indicate the dispatch command is incomplete and must be fixed before the registry is built.

    uv run --with pyyaml --with <dep1> --with <dep2> python <relpath> --help          # exit 0, help derived from frontmatter
    echo '{}' | uv run --with pyyaml --with <dep1> python <relpath>                    # required missing → exit 1; else normal run

Discovery loop for missing deps:

1. Run `--help`. If it fails with `ModuleNotFoundError: <pkg>`, add `--with <pkg>` and rerun.
2. Repeat until `--help` exits 0.
3. Persist the resolved set into frontmatter `command` (see Optional frontmatter fields) so the registry inherits the runnable command — otherwise downstream LLM-callers will hit the same `ModuleNotFoundError` at dispatch time.

Then run one happy-path business-logic check using a representative input to confirm migrated behavior matches the original.

## Building the registry

After every script under `input_dir` has been migrated and verified, build the registry:

    uv run --with pyyaml python ~/.claude/skills/register-pi-tools/scripts/build_tools_yaml.py \
      --input-dir <input_dir> --output-path <output_path>

Confirm:
- The build prints `Wrote N tools to <path> (skipped M)`.
- No `name` regex violation halted the build.
- `output_path` exists and parses as YAML.

Each entry has the shape:

    - name: <frontmatter.name>
      description: <frontmatter.description or "">
      input_schema: <frontmatter.args>      # verbatim JSON Schema
      command: <frontmatter.command or "uv run python <abs_posix_path>">

## Anthropic tool object mapping

When a downstream LLM caller loads `tools.yaml` and constructs `request.tools`:

    request.tools.entry = {
      name:        entry.name,
      description: entry.description,
      input_schema: entry.input_schema,
    }
    # entry.command is held by the dispatcher and not sent to the API

Reason: Anthropic restricts `name` to `^[a-zA-Z0-9_-]{1,64}$`. Spaces / slashes / dots in the command string cannot live in `name`, so `command` is a separate field.

On `tool_use` reply, the dispatcher looks up the entry by name, spawns `entry.command` via shell, pipes `tool_use.input` (JSON) to stdin, and returns stdout as `tool_result.content`.

## Optional frontmatter fields

- `command`: override the execution string (default is `uv run python <abs_posix_path>`). Use this to bake `uv --with` flags for every third-party import the script needs, so the registry stays self-contained and dispatch never fails on `ModuleNotFoundError`. Example: `command: uv run --with pillow --with pyyaml python /abs/path/to/compress_images.py`. Prefer `--with` over `pip install` into a shared venv — it keeps each tool's deps isolated and reproducible across machines.
- `enabled: false`: exclude this script from the registry (useful for WIP / sample files).

## Reference: argparse → frontmatter args mapping

| argparse | frontmatter args |
|---|---|
| `add_argument("foo")` (positional, required) | `required: [foo]`, `properties.foo.type: string` |
| `add_argument("--flag", action="store_true")` | `properties.flag: {type: boolean, default: false}` |
| `add_argument("--n", type=int, default=0)` | `properties.n: {type: integer, default: 0}` |
| `add_argument("files", nargs="+")` | `properties.files: {type: array, items: {type: string}}` plus `required: [files]` |
| `add_argument("--mode", choices=["a","b"])` | `properties.mode: {type: string, enum: [a, b]}` |
| `add_argument("path", nargs="?", default="d")` | `properties.path: {type: string, default: d}` (do not add to `required`) |

`_tool` handles kebab→snake conversion for argv (`--file-or-dir` → `file_or_dir`) and rejects schema-外 keys in strict mode. The script only needs to declare the schema correctly.

## Bundled scripts

- `scripts/build_tools_yaml.py`: walks `input_dir`, extracts each frontmatter, validates `name`, writes the registry to `output_path`. Self-contained (depends only on `scripts/_tool.py` and `pyyaml`).
- `scripts/_tool.py`: bundled copy of the runtime helper that `build_tools_yaml.py` itself depends on. Treated as the authoritative copy; sync downstream copies (e.g. in a project's `lib/src/_tool.py`) from this file when divergence appears.
