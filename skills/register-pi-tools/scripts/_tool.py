#!/usr/bin/env python3
# ---
# name: _tool
# description: Shared helper providing args() — frontmatter-driven JSON Schema validation, auto --help, and stdin/argv input
# usage: from _tool import args
# args:
#   type: object
#   properties: {}
# ---
"""Shared helper: read arguments for the calling script using its frontmatter
`args` (JSON Schema) as the single source of truth. Reads JSON on stdin or
`--key value` on argv, applies defaults, validates, and auto-generates `--help`.

Caller usage:

    from _tool import args

    def main():
        a = args()              # validated dict
        path = a["file_or_dir"]
        ...

PyYAML is required; the helper exits with an error if it is missing.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    # Idempotent: avoid double-wrapping when caller scripts also reconfigure.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

_PY_FRONTMATTER = re.compile(
    r"^# ---\n((?:#[^\n]*\n)*?)# ---",
    re.MULTILINE,
)


class _ValidationError(Exception):
    pass


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


def _parse_yaml(text: str) -> dict | None:
    if yaml is None:
        sys.stderr.write(
            "Error: PyYAML is required. Install it with `uv pip install pyyaml`.\n"
        )
        sys.exit(1)
    loaded = yaml.safe_load(text)
    return loaded if isinstance(loaded, dict) else None


def _extract_frontmatter(source: str) -> dict | None:
    m = _PY_FRONTMATTER.search(source)
    if not m:
        return None
    raw = _strip_comment_prefix(m.group(1))
    return _parse_yaml(raw)


def _caller_path() -> Path:
    me = Path(__file__).resolve()
    for frame in inspect.stack():
        try:
            f = Path(frame.filename).resolve()
        except (OSError, ValueError):
            continue
        if f != me and f.suffix == ".py":
            return f
    raise RuntimeError("cannot determine caller script path")


def _load_caller_frontmatter() -> dict:
    path = _caller_path()
    source = path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(source) or {}
    if "args" not in fm:
        fm["args"] = {"type": "object", "properties": {}}
    return fm


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if not isinstance(raw, str):
        return bool(raw)
    s = raw.lower()
    if s in ("true", "1", "yes", "y", "on"):
        return True
    if s in ("false", "0", "no", "n", "off"):
        return False
    raise _ValidationError(f"invalid boolean: {raw!r}")


def _coerce(prop_schema: dict, raw: str) -> Any:
    t = prop_schema.get("type", "string")
    if t == "string":
        return raw
    if t == "boolean":
        return _coerce_bool(raw)
    if t == "integer":
        try:
            return int(raw)
        except ValueError as e:
            raise _ValidationError(f"invalid integer: {raw!r}") from e
    if t == "number":
        try:
            return float(raw)
        except ValueError as e:
            raise _ValidationError(f"invalid number: {raw!r}") from e
    if t == "array":
        s = raw.strip()
        if s.startswith("["):
            return json.loads(s)
        return [x.strip() for x in s.split(",")] if s else []
    if t == "object":
        return json.loads(raw)
    return raw


def _is_bool_prop(properties: dict, key: str) -> bool:
    p = properties.get(key)
    return isinstance(p, dict) and p.get("type") == "boolean"


def _parse_argv(tokens: list[str], schema: dict) -> dict:
    properties: dict = schema.get("properties", {}) or {}
    result: dict[str, Any] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok.startswith("--"):
            raise _ValidationError(f"unexpected positional argument: {tok!r}")
        name = tok[2:]
        if "=" in name:
            name, value = name.split("=", 1)
            key = name.replace("-", "_")
            if key not in properties:
                raise _ValidationError(f"unknown argument: --{name}")
            result[key] = _coerce(properties[key], value)
            i += 1
            continue
        key = name.replace("-", "_")
        if key not in properties:
            raise _ValidationError(f"unknown argument: --{name}")
        if _is_bool_prop(properties, key):
            # peek next token: explicit value if it looks like a bool literal,
            # otherwise treat the flag presence as True.
            if i + 1 < n and tokens[i + 1].lower() in (
                "true",
                "false",
                "0",
                "1",
                "yes",
                "no",
                "on",
                "off",
            ):
                result[key] = _coerce_bool(tokens[i + 1])
                i += 2
            else:
                result[key] = True
                i += 1
            continue
        if i + 1 >= n:
            raise _ValidationError(f"--{name} requires a value")
        result[key] = _coerce(properties[key], tokens[i + 1])
        i += 2
    return result


def _apply_defaults(value: dict, schema: dict) -> None:
    if schema.get("type") not in (None, "object"):
        return
    properties: dict = schema.get("properties", {}) or {}
    for k, p in properties.items():
        if k not in value and isinstance(p, dict) and "default" in p:
            value[k] = p["default"]


def _validate(value: Any, schema: dict, path: str = "$") -> None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise _ValidationError(
                f"{path}: expected object, got {type(value).__name__}"
            )
        for req in schema.get("required", []) or []:
            if req not in value:
                raise _ValidationError(f"{path}: missing required field '{req}'")
        props = schema.get("properties", {}) or {}
        allow_extra = schema.get("additionalProperties", False)
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], f"{path}.{k}")
            elif not allow_extra:
                raise _ValidationError(f"{path}: unknown field '{k}'")
    elif t == "string":
        if not isinstance(value, str):
            raise _ValidationError(
                f"{path}: expected string, got {type(value).__name__}"
            )
        if "enum" in schema and value not in schema["enum"]:
            raise _ValidationError(
                f"{path}: value {value!r} not in enum {schema['enum']}"
            )
    elif t == "boolean":
        if not isinstance(value, bool):
            raise _ValidationError(
                f"{path}: expected boolean, got {type(value).__name__}"
            )
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _ValidationError(
                f"{path}: expected integer, got {type(value).__name__}"
            )
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _ValidationError(
                f"{path}: expected number, got {type(value).__name__}"
            )
    elif t == "array":
        if not isinstance(value, list):
            raise _ValidationError(
                f"{path}: expected array, got {type(value).__name__}"
            )
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{i}]")


def _format_help(fm: dict) -> str:
    name = fm.get("name") or ""
    desc = fm.get("description") or ""
    usage = fm.get("usage") or ""
    schema = fm.get("args") or {}
    properties: dict = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    lines: list[str] = []
    title = name
    if name and desc:
        title = f"{name} — {desc}"
    elif desc:
        title = desc
    if title:
        lines.append(title)
        lines.append("")
    if usage:
        lines.append("Usage:")
        lines.append(f"  {usage}")
        lines.append("")
    lines.append("Arguments (JSON via stdin or --key value via argv):")
    if not properties:
        lines.append("  (none)")
    else:
        for k, p in properties.items():
            if not isinstance(p, dict):
                p = {}
            t = p.get("type", "string")
            if k in required:
                tag = "required"
            elif "default" in p:
                tag = f"default: {json.dumps(p['default'], ensure_ascii=False)}"
            else:
                tag = "optional"
            flag = "--" + k.replace("_", "-")
            lines.append(f"  {flag}  ({t}, {tag})")
            if p.get("description"):
                lines.append(f"      {p['description']}")
    lines.append("")
    lines.append("Examples:")
    placeholder = (
        '{' + ", ".join(f'"{k}": ...' for k in list(properties.keys())[:2]) + '}'
        if properties
        else "{}"
    )
    if usage:
        script_path = _extract_script_from_usage(usage)
    else:
        script_path = "lib/src/<script>.py"
    lines.append(f"  echo '{placeholder}' | uv run python {script_path}")
    lines.append(f"  uv run python {script_path} --help")
    return "\n".join(lines)


def _extract_script_from_usage(usage: str) -> str:
    # usage example: "uv run python lib/src/extract_frontmatter.py <args>"
    m = re.search(r"(lib/src/\S+\.py)", usage)
    return m.group(1) if m else "lib/src/<script>.py"


def _emit_error_and_exit(fm: dict, message: str) -> "Any":
    sys.stderr.write(f"Error: {message}\n\n")
    sys.stderr.write(_format_help(fm))
    sys.stderr.write("\n")
    sys.exit(1)


def args() -> dict:
    """Return the validated argument dict for the calling script.

    Resolution order:
      1. If --help / -h is in argv, print help and exit 0.
      2. If stdin is not a TTY and contains data, parse it as JSON.
      3. If argv has --key value tokens, parse and merge (argv wins).
      4. Apply defaults from frontmatter.args.properties.*.default.
      5. Validate against frontmatter.args.

    On any validation error, print the error + auto-generated help to stderr
    and exit with status 1.
    """
    fm = _load_caller_frontmatter()
    schema: dict = fm.get("args", {}) or {}

    argv = sys.argv[1:]

    if any(t in ("--help", "-h") for t in argv):
        sys.stdout.write(_format_help(fm))
        sys.stdout.write("\n")
        sys.exit(0)

    raw: dict = {}

    if not sys.stdin.isatty():
        try:
            data = sys.stdin.read()
        except OSError:
            data = ""
        if data and data.strip():
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError as e:
                _emit_error_and_exit(fm, f"invalid JSON on stdin: {e}")
            if not isinstance(parsed, dict):
                _emit_error_and_exit(
                    fm,
                    f"stdin JSON must be an object, got {type(parsed).__name__}",
                )
            raw = parsed

    if argv:
        try:
            argv_args = _parse_argv(argv, schema)
        except _ValidationError as e:
            _emit_error_and_exit(fm, str(e))
        raw.update(argv_args)

    _apply_defaults(raw, schema)

    try:
        _validate(raw, schema)
    except _ValidationError as e:
        _emit_error_and_exit(fm, str(e))

    return raw


def main() -> None:
    # `_tool` itself can be invoked for sanity checks.
    a = args()
    json.dump(a, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
