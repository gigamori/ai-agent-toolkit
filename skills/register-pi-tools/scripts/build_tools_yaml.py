#!/usr/bin/env python3
# ---
# name: build_tools_yaml
# description: Walk input_dir for Python scripts with frontmatter args and emit a tools.yaml registry
# usage: uv run --with pyyaml python build_tools_yaml.py --input-dir DIR [--output-path PATH]
# args:
#   type: object
#   required: [input_dir]
#   properties:
#     input_dir:
#       type: string
#       description: Directory to scan recursively for *.py files
#     output_path:
#       type: string
#       default: ~/.pi/agent/tools.yaml
#       description: Output yaml path (tilde expansion supported)
# ---
"""Aggregate Python frontmatter under `input_dir` into a tools.yaml registry at `output_path`.

Each entry carries the mapping info for an Anthropic API tool object:
  - name: frontmatter.name
  - description: frontmatter.description (or "")
  - input_schema: frontmatter.args (verbatim JSON Schema)
  - command: frontmatter.command, or `uv run python <abs_posix_path>` by default

Skip rules:
  - Files whose name starts with `_` (private modules)
  - Anything under `ignore-old/`
  - Frontmatter with `enabled: false`
  - Missing `name` or `args` (logged as SKIP on stderr; processing continues)

`name` must match `^[a-zA-Z0-9_-]{1,64}$` (Anthropic API constraint). Violations raise ValueError and halt the build.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

import yaml  # type: ignore

from _tool import args  # noqa: E402

# Anthropic tool name: ^[a-zA-Z0-9_-]{1,64}$
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_PY_FRONTMATTER = re.compile(
    r"^# ---\n((?:#[^\n]*\n)*?)# ---",
    re.MULTILINE,
)


def _strip_comment_prefix(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith("# "):
            out.append(line[2:])
        elif line == "#":
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def _extract_frontmatter(source: str) -> dict | None:
    m = _PY_FRONTMATTER.search(source)
    if not m:
        return None
    raw = _strip_comment_prefix(m.group(1))
    loaded = yaml.safe_load(raw)
    return loaded if isinstance(loaded, dict) else None


def _iter_scripts(src_dir: Path):
    for p in sorted(src_dir.rglob("*.py")):
        if p.name.startswith("_"):
            continue
        rel = p.relative_to(src_dir).as_posix()
        if rel.startswith("ignore-old/") or "/ignore-old/" in rel:
            continue
        yield p


def _default_command(script_path: Path) -> str:
    # Use absolute POSIX path so the entry is location-independent.
    return f"uv run python {script_path.resolve().as_posix()}"


def _build_entry(script_path: Path) -> dict | None:
    source = script_path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(source)
    if not isinstance(fm, dict):
        sys.stderr.write(f"SKIP {script_path.as_posix()}: no frontmatter\n")
        return None
    if fm.get("enabled") is False:
        return None
    name = fm.get("name")
    schema = fm.get("args")
    if not name or not isinstance(schema, dict):
        sys.stderr.write(f"SKIP {script_path.as_posix()}: no name/args\n")
        return None
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid tool name {name!r} in {script_path.as_posix()} "
            f"(must match ^[a-zA-Z0-9_-]{{1,64}}$)"
        )
    desc = fm.get("description", "") or ""
    command = fm.get("command") or _default_command(script_path)
    return {
        "name": name,
        "description": desc,
        "input_schema": schema,
        "command": command,
    }


def _resolve_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def main() -> None:
    a = args()
    src_dir = _resolve_path(a["input_dir"])
    output_path = _resolve_path(a.get("output_path", "~/.pi/agent/tools.yaml"))

    if not src_dir.is_dir():
        sys.stderr.write(f"Error: input_dir is not a directory: {src_dir.as_posix()}\n")
        sys.exit(1)

    entries: list[dict] = []
    skipped = 0
    for script in _iter_scripts(src_dir):
        try:
            entry = _build_entry(script)
        except ValueError as e:
            sys.stderr.write(f"Error: {e}\n")
            sys.exit(1)
        if entry is None:
            skipped += 1
            continue
        entries.append(entry)

    entries.sort(key=lambda d: d["name"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(
            entries,
            f,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )

    sys.stdout.write(
        f"Wrote {len(entries)} tools to {output_path.as_posix()} (skipped {skipped})\n"
    )


if __name__ == "__main__":
    main()
