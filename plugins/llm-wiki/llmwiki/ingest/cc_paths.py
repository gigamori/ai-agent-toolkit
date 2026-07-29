"""CLAUDE_CONFIG_DIR-aware resolution of the CC session-log universes.

ONE resolution rule shared by every reader of the CC session logs in this
package — `ingest_driver`'s rglob-based sid / project-dir lookup and the DuckDB
views loaded by `cc_log_project` and `ingest_driver` — so the readers cannot
drift apart.

Semantics replicate what Claude Code itself does with the env value (verified
2026-07-28 on Windows with a headless `claude -p` against an isolated config
dir; see `_projects/harness-taskflow/project-notes/specs/claude-config-dir-support.md`
§1.1):

  * the value is LITERAL — no `~`-expansion and no env-var expansion. A relative
    value resolves against the process cwd (`os.path.abspath`), exactly as CC
    resolves it. Expanding `~` here would point at a different place than the
    one CC actually writes to.
  * only the built-in default `~/.claude` is expanduser'd.

Readers scan the UNION `[<env value>, ~/.claude]` with the env universe FIRST,
so first-wins lookups give the env universe priority while a missed env var, a
half-finished migration, or a VS Code extension host started with a different
environment still resolves.

SQL injection contract (see `cc_views.sql` and its canonical sibling
`skills/inspect-cc-log/scripts/views.sql`): the views keep a single literal
anchor glob so the file stays valid SQL that can be pasted into a DuckDB CLI as
is. `apply_cc_projects_glob` rewrites that one literal after the file is read
and BEFORE it is executed. With no env var set the text is returned unchanged
(byte-identical), so the default environment behaves exactly as before.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "CLAUDE_CONFIG_DIR"
_DEFAULT_CONFIG_DIR = "~/.claude"
_PROJECTS = "projects"
_GLOB_TAIL = "**/*.jsonl"

# The default-universe glob as it appears in the views, and the exact quoted
# literal `apply_cc_projects_glob` replaces. It occurs EXACTLY once in the SQL
# (the header comment mentions the same path WITHOUT quotes, so it is not a
# match) — `tests/test_cc_paths.py` asserts that.
CC_PROJECTS_DEFAULT_GLOB = "~/.claude/projects/**/*.jsonl"
CC_PROJECTS_GLOB_ANCHOR = f"'{CC_PROJECTS_DEFAULT_GLOB}'"


def _env_config_dir() -> str | None:
    """The `$CLAUDE_CONFIG_DIR` value as CC resolves it, or None when unset.

    Literal + `os.path.abspath` (cwd-relative), never expanduser'd — see the
    module docstring.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    return os.path.abspath(raw) if raw else None


def _default_projects_root() -> Path:
    return Path(os.path.expanduser(_DEFAULT_CONFIG_DIR)) / _PROJECTS


def _escape_glob_dir(dir_path: str) -> str:
    """Escape glob metacharacters in a literal directory path for DuckDB.

    DuckDB treats the WHOLE injected string as a glob, while `_has_logs` treats
    the root as a literal path (pathlib globs only the pattern part). A config
    dir containing `[` or `]` (legal in Windows dir names) therefore passed the
    existence filter but matched nothing in DuckDB, aborting CREATE VIEW (probed
    2026-07-29, duckdb 1.5.5). Wrapping each of `[]*?` in a character class
    (`[` -> `[[]`) makes DuckDB match it literally (probed); `*`/`?` can occur
    in POSIX dir names only.
    """
    return "".join("[" + ch + "]" if ch in "[]*?" else ch for ch in dir_path)


def cc_projects_roots() -> list[Path]:
    """The CC `projects` dirs to scan: `[$CLAUDE_CONFIG_DIR, ~/.claude]`, env first.

    Deduped with `os.path.normcase` (Windows case-insensitivity), so an env value
    pointing at the default dir yields a single root. Callers that take the first
    match get env-universe priority.
    """
    return [root for root, _glob in _cc_projects_universes()]


def _cc_projects_universes() -> list[tuple[Path, str]]:
    """`[(projects_root, duckdb_glob), ...]` in scan order (env first).

    The default universe keeps its `~/...` glob literal rather than an absolute
    path: DuckDB expands `~` itself (`USERPROFILE` on Windows, matching
    `os.path.expanduser` — probed 2026-07-29), and the views' contract is that no
    absolute path is baked in.
    """
    universes: list[tuple[Path, str]] = []
    env = _env_config_dir()
    if env:
        root = Path(env) / _PROJECTS
        universes.append((root, f"{_escape_glob_dir(root.as_posix())}/{_GLOB_TAIL}"))
    default = _default_projects_root()
    if not any(
        os.path.normcase(str(root)) == os.path.normcase(str(default))
        for root, _glob in universes
    ):
        universes.append((default, CC_PROJECTS_DEFAULT_GLOB))
    return universes


def _has_logs(projects_root: Path) -> bool:
    """True when at least one `*.jsonl` lives under `projects_root`.

    DuckDB raises `IOException: No files found that match the pattern` — at
    CREATE VIEW time, killing the whole view stack — for ANY glob in a glob list
    that matches nothing, and an existing-but-empty dir counts as nothing
    (probed 2026-07-29, duckdb 1.5.5). So an empty universe must be filtered out
    before it reaches the SQL. `**` matches zero directories in both pathlib and
    DuckDB (probed), which makes this check equivalent to what DuckDB will read.
    """
    try:
        return next(projects_root.glob(_GLOB_TAIL), None) is not None
    except OSError:
        return False


def apply_cc_projects_glob(sql: str) -> str:
    """Point the views' base glob at every CC log universe that has logs.

    With `$CLAUDE_CONFIG_DIR` unset the input is returned UNCHANGED (byte for
    byte). With it set, the single anchor literal becomes a DuckDB glob LIST of
    the universes that actually contain logs, env universe first. When neither
    universe has logs, the env glob alone is injected: the read still fails as it
    would today, but the error names the configured dir instead of misdirecting
    at `~/.claude`.
    """
    if _env_config_dir() is None:
        return sql
    universes = _cc_projects_universes()
    globs = [glob for root, glob in universes if _has_logs(root)]
    if not globs:
        globs = [universes[0][1]]
    listed = ", ".join("'" + glob.replace("'", "''") + "'" for glob in globs)
    return sql.replace(CC_PROJECTS_GLOB_ANCHOR, f"[{listed}]")


def read_cc_views_sql(path: Path) -> str:
    """Read a cc-views SQL file and apply the glob injection (the loader path).

    Every loader of `cc_views.sql` goes through this so the three call sites
    cannot diverge.
    """
    return apply_cc_projects_glob(path.read_text(encoding="utf-8"))
