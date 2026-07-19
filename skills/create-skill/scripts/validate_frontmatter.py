# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Validate a SKILL.md frontmatter block.

Usage:
    uv run --script scripts/validate_frontmatter.py path/to/SKILL.md

Prints "OK" and exits 0 when the frontmatter parses as YAML and satisfies the
name/description constraints. Otherwise prints each problem and exits 1.

The most common failure this catches: an unquoted `description` whose value
contains a ": " (colon + space) or "#", which YAML reads as a mapping
separator/comment and rejects. Wrapping string values in double quotes fixes it.
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        "pyyaml is not installed. Run via `uv run --script` (the PEP 723 header "
        "declares the dependency), or `pip install pyyaml`. If neither is "
        "available, manually confirm every frontmatter string value is "
        "double-quoted."
    )

NAME_RE = re.compile(r"^[a-z0-9-]+$")
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)", re.DOTALL)


def extract_frontmatter(text):
    """Return the raw YAML frontmatter block, or None if absent/unterminated."""
    m = FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def find_angle_brackets(value, path="frontmatter"):
    """Yield the paths of frontmatter keys/values containing '<' or '>'."""
    if isinstance(value, str):
        if "<" in value or ">" in value:
            yield path
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and ("<" in k or ">" in k):
                yield f"{path}.{k} (key)"
            yield from find_angle_brackets(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from find_angle_brackets(v, f"{path}[{i}]")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: validate_frontmatter.py path/to/SKILL.md")

    path = Path(sys.argv[1])
    if not path.is_file():
        sys.exit(f"not a file: {path}")

    text = path.read_text(encoding="utf-8")

    raw = extract_frontmatter(text)
    if raw is None:
        sys.exit(
            "frontmatter not found: file must start with a '---' line and close "
            "with a '---' line"
        )

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        print("FAIL: frontmatter is not valid YAML")
        print(e)
        print()
        print(
            "Hint: wrap string values (especially `description`) in double "
            "quotes. A ': ' or '#' inside an unquoted value, or an indicator "
            "character at the start of a value, breaks YAML parsing."
        )
        sys.exit(1)

    if not isinstance(data, dict):
        sys.exit("FAIL: frontmatter did not parse to a mapping")

    errors = []

    name = data.get("name")
    if name is None:
        errors.append("missing required field: name")
    elif not isinstance(name, str):
        errors.append("name must be a string")
    else:
        if not NAME_RE.match(name):
            errors.append(f"name must be kebab-case [a-z0-9-]: {name!r}")
        if len(name) > 64:
            errors.append(f"name exceeds 64 chars: {len(name)}")
        folder = path.parent.name
        if name != folder:
            errors.append(f"name {name!r} must match folder name {folder!r}")

    desc = data.get("description")
    if desc is None:
        errors.append("missing required field: description")
    elif not isinstance(desc, str):
        errors.append("description must be a string")
    else:
        if not desc.strip():
            errors.append("description must not be empty")
        if len(desc) > 1024:
            errors.append(f"description exceeds 1024 chars: {len(desc)}")

    for loc in find_angle_brackets(data):
        errors.append(f"angle brackets '<' or '>' not allowed in frontmatter: {loc}")

    if errors:
        print("FAIL: frontmatter constraints violated")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("OK")


if __name__ == "__main__":
    main()
