# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Multi-scope wiki-root resolver (plan T1, §2-A, W-a/W-b).

Resolves the active wiki-root by EXISTENCE, top-down, in a fixed precedence:

    prompt > pj > workspace > cwd > child

This module RESOLVES ONLY. It never creates or generates a wiki — generation
is `/wiki-init` (plan T2, R-6). If nothing resolves, `resolve()` returns None;
the caller decides what to do (it must never silently fabricate a root).

Scopes (plan §2-A 1..5):
  1. prompt    — an explicit `prompt_root` (the `--root` override). Most
                 preferred; taken verbatim with scope "prompt" (no existence
                 gate — an explicit override is the caller's responsibility).
  2. pj        — taskflow project linkage (one-way llm-wiki -> taskflow, W-b).
                 With a `session_id` (Phase 1 P1) read the exact per-session
                 `_projects/_state/<session_id>.json` FIRST — the file taskflow
                 writes — so concurrent sessions on different pj resolve their
                 OWN wiki; absent / unreadable / no usable `project` -> skip pj
                 (fail-closed, 2026-08-19), NOT a fallback. The most recent
                 `_projects/_state/*.json` (mtime desc, 1 file) is read ONLY
                 when no `session_id` is given (legacy callers). Take its
                 `project` field (verified: the state file is
                 `{"project": "llm-wiki", ...}`, plan §1). Resolve project
                 roots via `$TASKFLOW_PROJECT_ROOTS` (`;`-separated; if unset,
                 fall back to `_projects/` in the workspace), mirroring
                 taskflow's progress SKILL Step 2. For the first
                 `<proot>/<project>/wiki/` whose `.llmwiki` exists -> scope "pj".
                 State-file read is OPTIONAL: missing file / no project / no
                 match -> skip pj cleanly (never error). Plan R-5/W-b.
  3. workspace — the convention path `<workspace-root>/_llm-wiki/` (Q2: `_`
                 prefix). If its `.llmwiki` exists -> scope "workspace".
  4. cwd       — if CWD has `.llmwiki` -> scope "cwd" (legacy / standalone repo
                 compatibility, plan §1 last bullet).
  5. child     — scan the IMMEDIATE children of CWD (depth 1, no recursion);
                 if EXACTLY one child directory has `.llmwiki` -> that child,
                 scope "child". Zero children with a marker, or two or more
                 (ambiguous — picking one silently could write to the wrong
                 wiki), fall through to none. Added 2026-08-08: opening the
                 PARENT of a wiki folder is a common accident (observed in the
                 pi-side bashReview-exemption E2E), and without this scope the
                 session goes dormant with no wiki even though the intent is
                 obvious when there is only one candidate.
  6. none      — None.

workspace-root rule (plan §2-A asks for a deterministic, documented rule):
    The workspace-root is the PARENT directory of the project-roots container.
    Concretely, it is the parent of the FIRST existing root obtained from
    `$TASKFLOW_PROJECT_ROOTS` (split on `;`); if that variable is unset (or no
    listed root exists), the container is `_projects/` under the CWD, whose
    parent is the CWD. This reuses the exact same project-root machinery the pj
    scope uses (no separate, ambiguous repo walk-up), so workspace and pj agree
    on where "the workspace" is. The base for the `_projects/` fallback and for
    `Path.cwd()` is supplied via the `cwd` parameter (defaults to the real CWD)
    so the behaviour is testable and deterministic.

The `.llmwiki` existence check is delegated to `marker.detect` (do NOT
re-implement marker parsing). Dependency-free; imports `marker` from
`llmwiki.core`.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from llmwiki.core import marker


STATE_GLOB = "_projects/_state/*.json"
PROJECTS_DIRNAME = "_projects"
WORKSPACE_WIKI_DIRNAME = "_llm-wiki"
PROJECT_WIKI_SUBDIR = "wiki"
TASKFLOW_PROJECT_ROOTS = "TASKFLOW_PROJECT_ROOTS"


@dataclass
class Resolution:
    """A resolved wiki-root and the scope it was resolved through."""

    root: Path
    scope: str  # one of "prompt" | "pj" | "workspace" | "cwd" | "child"


def _has_marker(path: Path) -> bool:
    """Existence check for a wiki-root, via marker.detect (no re-parse)."""
    return marker.detect(path) is not None


def _project_roots(cwd: Path) -> list[Path]:
    """The ordered list of project-root containers (progress SKILL Step 2).

    Split `$TASKFLOW_PROJECT_ROOTS` on `;` into directories; drop empties. If
    the variable is unset/empty, fall back to a single container: `_projects/`
    under `cwd`.
    """
    raw = os.environ.get(TASKFLOW_PROJECT_ROOTS)
    if raw:
        roots = [Path(tok) for tok in raw.split(";") if tok.strip()]
        if roots:
            return roots
    return [cwd / PROJECTS_DIRNAME]


def _workspace_root(cwd: Path) -> Path:
    """Deterministic workspace-root: parent of the project-roots container.

    Uses the FIRST existing project-root from `$TASKFLOW_PROJECT_ROOTS`; if none
    of the listed roots exist (or the variable is unset), the container is
    `_projects/` under `cwd`, whose parent is `cwd`.
    """
    roots = _project_roots(cwd)
    for root in roots:
        if root.is_dir():
            return root.parent
    # No listed root exists -> the container is `<cwd>/_projects`, parent = cwd.
    return roots[0].parent


def _read_state_project(state_file: Path) -> "str | None":
    """Read a single state file's `project` field, or None on any failure.

    Shared by the session-specific fast path and the mtime-latest fallback so
    both degrade identically (unreadable / bad JSON / missing project -> None).
    """
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    project = data.get("project")
    if not isinstance(project, str) or not project.strip():
        return None
    return project.strip()


def _latest_state_project(cwd: Path,
                          session_id: "str | None" = None,
                          explain: bool = False) -> "str | None":
    """The `project` field of the session's state file, else the most recent one.

    Session-aware (Phase 1 P1): when `session_id` is given, read ONLY the exact
    `_projects/_state/<session_id>.json` — this is the state file taskflow's
    `session_init.py` writes (`f'{session_id}.json'`). When that file is absent /
    unreadable / has no usable `project`, return None — the pj scope is skipped
    rather than falling back, because the mtime-latest scan would resolve
    whichever session wrote last, i.e. a DIFFERENT concurrent session's project.
    The legacy mtime-latest scan below runs ONLY when `session_id` is falsy.

    Mirrors progress SKILL Step 2 for the legacy (no-`session_id`) path: list
    `_projects/_state/*.json` by mtime descending and read the most recent. Any
    failure (no dir, no file, unreadable, bad JSON, missing/empty `project`)
    degrades to None so the pj scope is simply skipped — never an error (W-b).
    """
    state_dir = cwd / PROJECTS_DIRNAME / "_state"
    # Session-aware fast path: the exact per-session state file taskflow writes.
    if session_id:
        session_file = state_dir / f"{session_id}.json"
        if session_file.is_file():
            project = _read_state_project(session_file)
            if project is not None:
                return project
        # Fail-closed (2026-08-19): a sid WAS given but its own state file is
        # absent / unreadable / carries no usable `project`. Falling through to
        # the mtime-latest scan here would silently resolve a DIFFERENT
        # concurrent session's project — the exact cross-talk this fast path
        # exists to close, and the one `ingest_driver._active_project_for_sid`
        # refuses by design. Skip pj instead: `resolve()` continues to
        # workspace > cwd > child and finally the NO-WIKI sentinel. The scan
        # below stays reachable ONLY for a falsy `session_id` (the legacy
        # no-`--sid` callers), whose behavior is unchanged.
        if explain:
            print(
                f"pj-skip: sid given but {session_file.as_posix()} is "
                "absent/unreadable/has no project (mtime-latest fallback not used)",
                file=sys.stderr,
            )
        return None
    try:
        candidates = [p for p in state_dir.glob("*.json") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return _read_state_project(latest)


def _resolve_pj(cwd: Path,
                session_id: "str | None" = None,
                explain: bool = False) -> "Resolution | None":
    """pj scope: state-file `project` + project-roots -> `<proot>/<project>/wiki/`.

    Returns the first existing match, or None (skip pj) when there is no state
    file / no project / no matching wiki (degrade, W-b/R-5). `session_id` selects
    the per-session state file EXCLUSIVELY (Phase 1 P1 + fail-closed 2026-08-19);
    mtime-latest applies only when no `session_id` is given.
    """
    project = _latest_state_project(cwd, session_id, explain=explain)
    if project is None:
        return None
    for proot in _project_roots(cwd):
        candidate = proot / project / PROJECT_WIKI_SUBDIR
        if _has_marker(candidate):
            return Resolution(root=candidate, scope="pj")
    return None


def resolve(prompt_root: "str | None" = None,
            cwd: "str | Path | None" = None,
            session_id: "str | None" = None,
            explain: bool = False) -> "Resolution | None":
    """Resolve the active wiki-root by existence in precedence order.

    Order (plan §2-A + child 2026-08-08): prompt > pj > workspace > cwd >
    child; None if nothing matches (incl. the ambiguous multi-child case).

    Args:
        prompt_root: an explicit `--root` override (most preferred). Taken
            verbatim with scope "prompt"; not existence-gated.
        cwd: the base directory used for the cwd scope, the `_projects/`
            fallback, and the workspace-root computation. Defaults to the real
            current working directory. Exposed for deterministic testing.
        session_id: the CC session id (Phase 1 P1). When given, the pj scope
            reads `_projects/_state/<session_id>.json` EXCLUSIVELY so concurrent
            sessions on different pj resolve their OWN wiki; absent / unreadable /
            no usable `project` skips pj instead of falling back. The CLI threads
            this through its `resolve-root --sid S` flag (theme1 i:63); when
            `--sid` is omitted the session context is None and the legacy
            mtime-latest behavior applies.
        explain: when True, print a one-line reason on stderr if the pj scope
            is skipped because a GIVEN `session_id` had no usable state file.
            Set only by the `resolve-root` CLI verb; the hook and the viewer
            leave it False so no library-level stderr is produced on their
            per-turn path. Never affects the return value.
    """
    # 1) prompt — explicit override wins. Absolutize so a RELATIVE `--root` is
    #    stable regardless of the process CWD (the hook cwd and a command's shell
    #    cwd can diverge); init already does `Path(root).resolve()` (F3, review §5).
    if prompt_root is not None:
        return Resolution(root=Path(prompt_root).resolve(), scope="prompt")

    base = Path(cwd) if cwd is not None else Path.cwd()

    # 2) pj — taskflow project linkage (optional; degrades cleanly).
    pj = _resolve_pj(base, session_id, explain=explain)
    if pj is not None:
        return pj

    # 3) workspace — `<workspace-root>/_llm-wiki/`.
    workspace_wiki = _workspace_root(base) / WORKSPACE_WIKI_DIRNAME
    if _has_marker(workspace_wiki):
        return Resolution(root=workspace_wiki, scope="workspace")

    # 4) cwd — `<cwd>/.llmwiki`.
    if _has_marker(base):
        return Resolution(root=base, scope="cwd")

    # 5) child — exactly one immediate child with a marker (depth 1 only).
    #
    # Ambiguity is fail-closed ON PURPOSE: with two candidate wikis under the
    # cwd, silently picking either one hands every later WRITE (`file`,
    # `promote`, `ingest ...`) a root the user never chose. None keeps the
    # existing NO-WIKI behavior, and the caller-side guard tells the user to
    # open the wiki folder itself. Depth is 1 and never recursive — a recursive
    # scan would both be slow on big trees and widen the ambiguity surface.
    #
    # OSError (unreadable cwd, permission) skips the scope cleanly — same
    # "never error, just don't resolve" contract as the pj scope.
    try:
        candidates = [
            child for child in base.iterdir()
            if child.is_dir() and _has_marker(child)
        ]
    except OSError:
        candidates = []
    if len(candidates) == 1:
        return Resolution(root=candidates[0], scope="child")

    # 6) none.
    return None
