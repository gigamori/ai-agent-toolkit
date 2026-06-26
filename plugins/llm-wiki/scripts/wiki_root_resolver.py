# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Multi-scope wiki-root resolver (plan T1, §2-A, W-a/W-b).

Resolves the active wiki-root by EXISTENCE, top-down, in a fixed precedence:

    prompt > pj > workspace > cwd

This module RESOLVES ONLY. It never creates or generates a wiki — generation
is `/wiki-init` (plan T2, R-6). If nothing resolves, `resolve()` returns None;
the caller decides what to do (it must never silently fabricate a root).

Scopes (plan §2-A 1..5):
  1. prompt    — an explicit `prompt_root` (the `--root` override). Most
                 preferred; taken verbatim with scope "prompt" (no existence
                 gate — an explicit override is the caller's responsibility).
  2. pj        — taskflow project linkage (one-way llm-wiki -> taskflow, W-b).
                 Read the most recent `_projects/_state/*.json` (mtime desc,
                 1 file) and take its `project` field (verified: the state file
                 is `{"project": "llm-wiki", ...}`, plan §1). Resolve project
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
  5. none      — None.

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
re-implement marker parsing). Dependency-free; imports the sibling `marker`
module from `scripts/`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import marker


STATE_GLOB = "_projects/_state/*.json"
PROJECTS_DIRNAME = "_projects"
WORKSPACE_WIKI_DIRNAME = "_llm-wiki"
PROJECT_WIKI_SUBDIR = "wiki"
TASKFLOW_PROJECT_ROOTS = "TASKFLOW_PROJECT_ROOTS"


@dataclass
class Resolution:
    """A resolved wiki-root and the scope it was resolved through."""

    root: Path
    scope: str  # one of "prompt" | "pj" | "workspace" | "cwd"


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


def _latest_state_project(cwd: Path) -> "str | None":
    """The `project` field of the most recent state file, or None (optional).

    Mirrors progress SKILL Step 2: list `_projects/_state/*.json` by mtime
    descending and read the most recent. Any failure (no dir, no file, unreadable,
    bad JSON, missing/empty `project`) degrades to None so the pj scope is
    simply skipped — never an error (W-b).
    """
    state_dir = cwd / PROJECTS_DIRNAME / "_state"
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
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    project = data.get("project")
    if not isinstance(project, str) or not project.strip():
        return None
    return project.strip()


def _resolve_pj(cwd: Path) -> "Resolution | None":
    """pj scope: state-file `project` + project-roots -> `<proot>/<project>/wiki/`.

    Returns the first existing match, or None (skip pj) when there is no state
    file / no project / no matching wiki (degrade, W-b/R-5).
    """
    project = _latest_state_project(cwd)
    if project is None:
        return None
    for proot in _project_roots(cwd):
        candidate = proot / project / PROJECT_WIKI_SUBDIR
        if _has_marker(candidate):
            return Resolution(root=candidate, scope="pj")
    return None


def resolve(prompt_root: "str | None" = None,
            cwd: "str | Path | None" = None) -> "Resolution | None":
    """Resolve the active wiki-root by existence in precedence order.

    Order (plan §2-A): prompt > pj > workspace > cwd; None if nothing matches.

    Args:
        prompt_root: an explicit `--root` override (most preferred). Taken
            verbatim with scope "prompt"; not existence-gated.
        cwd: the base directory used for the cwd scope, the `_projects/`
            fallback, and the workspace-root computation. Defaults to the real
            current working directory. Exposed for deterministic testing.
    """
    # 1) prompt — explicit override wins. Absolutize so a RELATIVE `--root` is
    #    stable regardless of the process CWD (the hook cwd and a command's shell
    #    cwd can diverge); init already does `Path(root).resolve()` (F3, review §5).
    if prompt_root is not None:
        return Resolution(root=Path(prompt_root).resolve(), scope="prompt")

    base = Path(cwd) if cwd is not None else Path.cwd()

    # 2) pj — taskflow project linkage (optional; degrades cleanly).
    pj = _resolve_pj(base)
    if pj is not None:
        return pj

    # 3) workspace — `<workspace-root>/_llm-wiki/`.
    workspace_wiki = _workspace_root(base) / WORKSPACE_WIKI_DIRNAME
    if _has_marker(workspace_wiki):
        return Resolution(root=workspace_wiki, scope="workspace")

    # 4) cwd — `<cwd>/.llmwiki`.
    if _has_marker(base):
        return Resolution(root=base, scope="cwd")

    # 5) none.
    return None
