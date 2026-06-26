# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""config / precedence resolver (D3/D4/D5).

Reads SCHEMA.md YAML frontmatter `config`, applies the 3-tier precedence
`prompt-explicit > wiki-local config > built-in default` (D4), resolves each axis
independently (D3), honors override_scope operation|session (D5), and emits the
one-line resolved-value declaration before a write op (D5 / design §3 step 4).

No PyYAML dependency: the frontmatter `config:` block is a flat key: value map, so
a minimal line parser is sufficient and keeps the script dependency-free (mirrors
the SCHEMA.md note that the plugin parses this frontmatter itself).

I/O contract:
    load_config(schema_path) -> dict[str, str]
      in : path to SCHEMA.md
      out: flat dict of config axis -> wiki-local value (only keys present in the
           frontmatter `config:` block; empty values dropped so default applies)

    resolve(axis, *, prompt_value=None, wiki_config=None) -> Resolution
      in : axis name, optional prompt-explicit value, the loaded wiki config
      out: Resolution { axis, value, source }   source in {prompt, wiki, default}
           - applies D3 (independent per axis) + D4 (3-tier precedence)
           - empty/None at a tier falls through to the next

    resolve_all(prompt_values, wiki_config) -> dict[axis, Resolution]

    write_mode_skips_confirmation(resolutions) -> bool   # D5 (implicit = loud)
    autocommit_forced(resolutions) -> bool               # floor: implicit -> auto

    declare(resolution) -> str
      out: the one-line "[wiki] <axis> = <value> (<source>)" declaration string
           emitted before any write-bearing op (D5).

    override_persists(resolutions) -> bool                # D5 sticky vs operation

Built-in defaults (design §3): the AXES table below. These are the authoritative
fallback when both prompt and wiki-local are empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# Built-in defaults (design §3 / SCHEMA.md frontmatter comments). Authoritative.
DEFAULTS: dict[str, str] = {
    "activation_scope": "scoped",
    "read_grounding": "implicit",
    "write_mode": "explicit",
    "write_autocommit": "auto",
    "override_scope": "operation",
    "apply_fanout_k": "10",
    "max_count": "100",
    "max_bytes": "10485760",
}

AXES = tuple(DEFAULTS.keys())


@dataclass
class Resolution:
    axis: str
    value: str
    source: str   # "prompt" | "wiki" | "default"


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# A config line: 2-space indent, key, colon, value (value may be empty / have a
# trailing `# comment`). Only the flat `config:` block is parsed.
_KV_RE = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")


def _strip_comment(val: str) -> str:
    # Remove a trailing `# ...` comment not inside quotes (values here are simple).
    hash_idx = val.find("#")
    if hash_idx != -1:
        val = val[:hash_idx]
    return val.strip().strip("'\"").strip()


def load_config(schema_path: "str | Path") -> dict[str, str]:
    """Parse the `config:` block from SCHEMA.md frontmatter into a flat dict.

    Only the immediate `config:` children (2-space indent) are read. Empty values
    are dropped (so the default tier applies). `doc_type_profiles:` and any other
    top-level frontmatter block are ignored.
    """
    text = Path(schema_path).read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.search(text)
    if not m:
        return {}
    fm = m.group(1).splitlines()
    out: dict[str, str] = {}
    in_config = False
    for line in fm:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*:\s*$", line):
            in_config = line.strip() == "config:"
            continue
        if not in_config:
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key, raw_val = kv.group(1), kv.group(2)
        val = _strip_comment(raw_val)
        if val:
            out[key] = val
    return out


def resolve(axis: str, *, prompt_value: "str | None" = None,
            wiki_config: "dict[str, str] | None" = None) -> Resolution:
    """Resolve one axis via D4 precedence, independently (D3)."""
    wiki_config = wiki_config or {}
    if prompt_value is not None and str(prompt_value).strip():
        return Resolution(axis, str(prompt_value).strip(), "prompt")
    wv = wiki_config.get(axis)
    if wv is not None and str(wv).strip():
        return Resolution(axis, str(wv).strip(), "wiki")
    return Resolution(axis, DEFAULTS.get(axis, ""), "default")


def resolve_all(prompt_values: "dict[str, str] | None",
                wiki_config: "dict[str, str] | None") -> dict[str, Resolution]:
    prompt_values = prompt_values or {}
    return {
        axis: resolve(axis, prompt_value=prompt_values.get(axis),
                      wiki_config=wiki_config)
        for axis in AXES
    }


def write_mode_skips_confirmation(resolutions: dict[str, Resolution]) -> bool:
    """D5: write_mode=implicit skips the per-apply confirmation (loud-announced)."""
    return resolutions["write_mode"].value == "implicit"


def autocommit_forced(resolutions: dict[str, Resolution]) -> bool:
    """Floor (SCHEMA.md): write_mode=implicit forces autocommit true."""
    return resolutions["write_mode"].value == "implicit"


def override_persists(resolutions: dict[str, Resolution]) -> bool:
    """D5: prompt override is operation-scoped by default; sticky only if
    override_scope resolved to `session`."""
    return resolutions["override_scope"].value == "session"


class ConfigInconsistency(Exception):
    """Raised when a resolved axis dict violates a config consistency invariant."""


def check_consistency(resolutions: dict[str, Resolution]) -> None:
    """Validate the resolved-axis invariant `apply_fanout_k <= max_count` (D-c).

    Pure and dependency-free: reads only the already-resolved values. The driver
    (T2) calls this before locking; a violation raises ConfigInconsistency so the
    error surfaces before any side effect. Both axes are int-valued strings; a
    non-integer value is itself an inconsistency.
    """
    raw_k = resolutions["apply_fanout_k"].value
    raw_max = resolutions["max_count"].value
    try:
        k = int(raw_k)
        max_count = int(raw_max)
    except (TypeError, ValueError) as exc:
        raise ConfigInconsistency(
            f"apply_fanout_k ({raw_k!r}) and max_count ({raw_max!r}) "
            f"must be integers"
        ) from exc
    if k > max_count:
        raise ConfigInconsistency(
            f"apply_fanout_k ({k}) must be <= max_count ({max_count})"
        )


def declare(resolution: Resolution) -> str:
    """One-line resolved-value declaration emitted before a write op (D5)."""
    return f"[wiki] {resolution.axis} = {resolution.value} ({resolution.source})"


def declare_all(resolutions: dict[str, Resolution]) -> str:
    return "\n".join(declare(resolutions[a]) for a in AXES)
