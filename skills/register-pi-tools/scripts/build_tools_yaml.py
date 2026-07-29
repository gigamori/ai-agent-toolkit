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
#       description: Output yaml path (tilde expansion supported). Omit it to write to <agent-dir>/tools.yaml, where <agent-dir> is $PI_CODING_AGENT_DIR when set and ~/.pi/agent otherwise.
# ---
"""Aggregate Python frontmatter under `input_dir` into a tools.yaml registry at `output_path`.

`output_path` defaults to the registry pi itself reads: `<agent-dir>/tools.yaml`,
where `<agent-dir>` honours `$PI_CODING_AGENT_DIR` (see `_default_output_path`).
The frontmatter deliberately declares NO `default:` for it, because a static
default would be injected by `_tool.args()` and shadow that resolution.

Each entry carries the mapping info for an Anthropic API tool object:
  - name: frontmatter.name
  - description: frontmatter.description (or "")
  - input_schema: frontmatter.args (verbatim JSON Schema)
  - command: frontmatter.command if set, otherwise an auto-built
    `uv run [--with <pkg> ...] python <abs_posix_path>` derived from the
    script's top-level imports plus frontmatter.extra_with.

Skip rules:
  - Files whose name starts with `_` (private modules)
  - Anything under `ignore-old/`
  - Frontmatter with `enabled: false`
  - Missing `name` or `args` (logged as SKIP on stderr; processing continues)

`name` must match `^[a-zA-Z0-9_-]{1,64}$` (Anthropic API constraint). Violations raise ValueError and halt the build.
"""

from __future__ import annotations

import ast
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

# Common import-name → pip-package-name divergences. Anything not listed is
# assumed to share the same name on PyPI. Override per-script via frontmatter
# `command:` (full replace) or `extra_with: [pkg]` (append).
_IMPORT_TO_PIP = {
    "yaml": "pyyaml",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
    "magic": "python-magic",
    "jwt": "pyjwt",
    "skimage": "scikit-image",
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome",
    "serial": "pyserial",
    "win32api": "pywin32",
    "win32com": "pywin32",
    "win32con": "pywin32",
    "pythoncom": "pywin32",
}

_STDLIB: set[str] = set(getattr(sys, "stdlib_module_names", set()))


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


def _collect_top_imports(source: str) -> list[str]:
    """Return top-level module names referenced by `import` / `from X import`.

    Relative imports (`from . import x`) are skipped. AST is used so syntactic
    edge cases inside docstrings or strings cannot leak.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    mods.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if (node.level or 0) > 0:
                continue
            if node.module:
                mods.append(node.module.split(".")[0])
    return mods


def _is_local_module(mod: str, script: Path, root: Path) -> bool:
    """True if `mod` resolves to a sibling file/package between script.parent and root."""
    cur = script.parent
    while True:
        if (cur / f"{mod}.py").is_file() or (cur / mod / "__init__.py").is_file():
            return True
        if cur == root:
            break
        if root not in cur.parents:
            break
        cur = cur.parent
    return False


def _resolve_with_pkgs(
    script: Path,
    root: Path,
    source: str,
    extra: list[str],
) -> list[str]:
    pkgs: set[str] = set()
    for mod in _collect_top_imports(source):
        if not mod:
            continue
        if mod.startswith("_"):
            # __future__, _tool, and other private/local-style names
            continue
        if mod in _STDLIB:
            continue
        if _is_local_module(mod, script, root):
            continue
        pkgs.add(_IMPORT_TO_PIP.get(mod, mod))
    pkgs.update(extra)
    return sorted(pkgs)


def _default_command(script_path: Path, with_pkgs: list[str]) -> str:
    # Use absolute POSIX path so the entry is location-independent.
    flags = "".join(f" --with {p}" for p in with_pkgs)
    return f"uv run{flags} python {script_path.resolve().as_posix()}"


def _build_entry(script_path: Path, root: Path) -> dict | None:
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

    extra_with_raw = fm.get("extra_with") or []
    if not isinstance(extra_with_raw, list) or not all(
        isinstance(x, str) for x in extra_with_raw
    ):
        raise ValueError(
            f"extra_with must be a list of strings in {script_path.as_posix()}"
        )

    explicit_command = fm.get("command")
    if explicit_command:
        command = explicit_command
    else:
        with_pkgs = _resolve_with_pkgs(script_path, root, source, list(extra_with_raw))
        command = _default_command(script_path, with_pkgs)
    return {
        "name": name,
        "description": desc,
        "input_schema": schema,
        "command": command,
    }


def _resolve_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


# --- PI_CODING_AGENT_DIR support ---------------------------------------------
# tools.yaml is a config file pi reads from ONE global path, so this resolves a
# single agent dir (no union of candidates — a union would read a file pi
# ignores). Duplicated on purpose: this skill script is self-contained. The
# sibling reader implementation is `skills/inspect-pi-log/scripts/query.py`.
# Design: `pi/_projects/pi-extensions-dev/project-notes/specs/agent-dir-env-support-design.md`.
_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"


def _expand_tilde(value: str) -> str:
    """Expand `~` the way pi's `expandTildePath` does, and no further.

    `normalizePath` (`pi/packages/coding-agent/src/utils/paths.ts`) expands a
    bare `~`, a `~/`-prefixed path and -- on Windows only -- a `~\\`-prefixed
    one, deliberately leaving the `~user` form alone. `os.path.expanduser` is
    too eager here: it would expand `~user` to a directory pi never writes to.
    """
    home = os.path.expanduser("~")
    if value == "~":
        return home
    if value.startswith("~/") or (os.name == "nt" and value.startswith("~\\")):
        return os.path.join(home, value[2:])
    return value


def _default_output_path() -> Path:
    """`<agent-dir>/tools.yaml` — the registry pi loads, per `getAgentDir()`.

    Mirrors `pi/packages/coding-agent/src/config.ts`: `$PI_CODING_AGENT_DIR`
    when it is set and non-blank (tilde-expanded, unlike Claude Code's
    `CLAUDE_CONFIG_DIR`), else `~/.pi/agent`. Without this, setting the var
    moves pi's registry while this script keeps writing to the home default —
    a build that reports success and is never read.
    """
    raw = os.environ.get(_AGENT_DIR_ENV, "").strip()
    agent_dir = _expand_tilde(raw) if raw else os.path.join(
        os.path.expanduser("~"), ".pi", "agent"
    )
    return Path(agent_dir, "tools.yaml").resolve()


def main() -> None:
    a = args()
    src_dir = _resolve_path(a["input_dir"])
    # An explicit value keeps its original handling; only the default is env-aware.
    requested_output = a.get("output_path")
    output_path = (
        _resolve_path(requested_output) if requested_output else _default_output_path()
    )

    if not src_dir.is_dir():
        sys.stderr.write(f"Error: input_dir is not a directory: {src_dir.as_posix()}\n")
        sys.exit(1)

    entries: list[dict] = []
    skipped = 0
    for script in _iter_scripts(src_dir):
        try:
            entry = _build_entry(script, src_dir)
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
