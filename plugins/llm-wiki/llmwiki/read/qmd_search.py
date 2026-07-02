# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""qmd search backend wrapper (read/ layer; optional-search-qmd.md S3).

Dependency-free subprocess shell-out to the external ``qmd`` CLI (Quick Markdown
Search). This module is part of the READ profile: its import closure is
``llmwiki.core.wiki_index`` + stdlib ONLY — never ``llmwiki.write`` /
``llmwiki.ingest`` (D-2 read-profile closure). qmd is optional and external; when
it is absent or the backend is not selected, the caller falls back to the
index-direct read path.

Design authority: optional-search-qmd.md (D-Q1..D-Q8, DD3), verified against the
installed qmd 2.5.3. Load-bearing invariants realized here:

  - **project-local isolation (D-Q3):** ``qmd init`` runs at the wiki-root BEFORE
    ``qmd collection add``, so qmd stores its index under ``<wiki-root>/.qmd/``
    instead of the global ``~/.cache/qmd`` registry. qmd resolves the index by
    walking UP from the process CWD, so EVERY shell-out runs with ``cwd=<root>``.
  - **wiki/-only collection (D-Q4):** the collection is the ``wiki/`` subtree
    (``qmd collection add <root>/wiki``, pattern fixed ``**/*.md``, no ``--mask``);
    the sibling ``raw/`` is never indexed (D16 untrusted-source isolation).
  - **B boundary = scan_pages post-filter (DD3):** every qmd hit is reconstructed
    to a wiki-root-relative path and kept ONLY if ``wiki_index.scan_pages`` also
    produces it. scan_pages stays the single page-ness authority — README / raw /
    any non-page ``.md`` are dropped here, not by a qmd ``ignore`` rule. The filter
    is fail-safe: a hit that cannot be reconstructed under the root is dropped, so
    a non-page can never be returned (recall may miss; correctness cannot break).
  - **D22 tier-by-path:** the tier is taken from the scan_pages entry (==
    ``wiki_index.tier_of(path)``), never from qmd, never from the LLM.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from llmwiki.core import wiki_index

_URI_PREFIX = "qmd://"
# Over-fetch k+margin from qmd so the scan_pages post-filter (DD3) can drop
# non-page hits (e.g. wiki/README.md) without starving the top-k result slots.
_OVERFETCH_MARGIN = 10
# Default ceiling for a single hybrid query. The FIRST query after install also
# downloads qmd's query-expansion / rerank models (~GB, R-Q3), so the ceiling is
# generous; front-load with /wiki-reindex to avoid paying it on a user query.
_QUERY_TIMEOUT = 300


class QmdError(RuntimeError):
    """A qmd shell-out failed hard (non-zero exit, timeout, unrunnable binary, or
    unparseable output). The caller degrades to the index-direct read path."""


def is_available(qmd_bin: str) -> bool:
    """D-Q8 term: the qmd binary resolves on PATH (or as an absolute path)."""
    return shutil.which(qmd_bin) is not None


def should_use(root, resolutions) -> bool:
    """D-Q8 activation predicate — use qmd IFF ALL hold:

        search_backend == "qmd"  AND  qmd_bin resolves  AND
        len(scan_pages(root)) > int(qmd_page_threshold)

    ``resolutions`` is the dict from ``config_resolver.resolve_all`` (axis ->
    Resolution). A missing/non-integer threshold degrades to False (safe: stay on
    the index-direct path). This predicate never spawns qmd.
    """
    if resolutions["search_backend"].value != "qmd":
        return False
    if not is_available(resolutions["qmd_bin"].value):
        return False
    try:
        threshold = int(resolutions["qmd_page_threshold"].value)
    except (KeyError, TypeError, ValueError):
        return False
    return len(wiki_index.scan_pages(root)) > threshold


def is_initialized(root) -> bool:
    """Whether the project-local qmd index already exists at ``<root>/.qmd/``.

    Used to detect FIRST lazy activation (D-Q6): when False, the query path runs
    the one-time ``ensure_collection`` (init + collection add + embed, ~GB models)
    inline; when True it goes straight to the fast ``update`` + ``query`` and lets
    ``/wiki-reindex`` (S5) own embedding refresh.
    """
    return (Path(root) / ".qmd").is_dir()


def _run(qmd_bin, args, root, timeout=None):
    """Run ``qmd <args>`` with ``cwd=<root>`` (D-Q3 project-local resolution).

    Resolves ``qmd_bin`` to its full path via ``shutil.which`` first: on Windows an
    npm-installed CLI is a ``qmd.CMD`` shim, and ``subprocess`` cannot launch it by
    the bare name ``qmd`` (WinError 2) — only by the resolved path. On POSIX this
    is a harmless identity resolution.

    Sets ``PWD`` in the child env to the wiki-root: qmd resolves its project-local
    index root from ``PWD`` (not just the process cwd), and on Windows the
    ``qmd.CMD`` shim does NOT receive subprocess's ``cwd=``. Without this, qmd
    writes ``.qmd/`` at the caller's shell PWD (leaking to the global registry
    instead of ``<wiki-root>/.qmd/``, breaking D-Q3). ``cwd=`` is also set for
    POSIX correctness.

    Returns the CompletedProcess; stdout carries data, stderr carries qmd's
    progress spinner / warnings (ignored here). Raises OSError if the binary
    cannot be executed and TimeoutExpired if it overruns ``timeout``.
    """
    exe = shutil.which(qmd_bin) or qmd_bin
    env = {**os.environ, "PWD": os.path.abspath(root)}
    return subprocess.run(
        [exe, *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        # qmd emits UTF-8 (incl. progress-spinner escapes); decode as UTF-8 and
        # never crash on stray bytes. Without this, text=True uses the locale
        # codec (e.g. cp932 on Japanese Windows) and raises UnicodeDecodeError in
        # subprocess's reader thread on qmd's output.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def ensure_collection(root, qmd_bin) -> None:
    """Bootstrap the project-local index (D-Q3/D-Q4), idempotent:

        qmd init                       -> creates <root>/.qmd/ (local, not global)
        qmd collection add <root>/wiki -> scopes the collection to the wiki/ subtree
        qmd embed                      -> vectors for the hybrid ``qmd query`` engine

    Idempotent by design: re-``init`` returns success and re-``collection add`` of
    the same path returns a non-zero "already exists" that is intentionally
    tolerated (verified on qmd 2.5.3). Only an UNRUNNABLE binary is fatal.
    """
    wiki_dir = str(Path(root) / wiki_index.WIKI_DIR)
    try:
        _run(qmd_bin, ["init"], root)
        _run(qmd_bin, ["collection", "add", wiki_dir], root)
        _run(qmd_bin, ["embed"], root)
    except OSError as e:  # binary missing / not executable
        raise QmdError(f"qmd is not runnable: {e}") from e


def update(root, qmd_bin) -> None:
    """Incremental re-index (D-Q6): pick up changed pages before a query. Best
    effort — a non-zero exit is left to the next query to surface."""
    try:
        _run(qmd_bin, ["update"], root)
    except OSError as e:
        raise QmdError(f"qmd is not runnable: {e}") from e


def _reconstruct_rel(file_field: str, root) -> "str | None":
    """qmd ``--json`` ``file`` -> wiki-root-relative POSIX path (D-Q5).

    qmd 2.5.3 returns an ABSOLUTE path under a ``qmd://`` scheme, e.g.
    ``qmd://<root>\\wiki/derived/foo.md`` (collection-root portion in the native
    separator, in-collection remainder with ``/``). Strip the scheme, then take
    the path relative to the wiki-root. Returns None if the hit is not under the
    root (defensive; such a hit is not a wiki page and is dropped by the caller).
    """
    if not file_field:
        return None
    raw = (file_field[len(_URI_PREFIX):]
           if file_field.startswith(_URI_PREFIX) else file_field)
    try:
        rel = Path(raw).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return None
    return rel.as_posix()


def _hits_to_pages(hits, root, k):
    """DD3 B-boundary: map qmd hits -> ``[(tier, rel_path)]``, keeping ONLY paths
    that ``scan_pages`` also produces (single page-ness authority), de-duped, in
    qmd's ranked order, trimmed to ``k``. The tier is taken from the scan_pages
    entry (== ``tier_of``), never from qmd.
    """
    pages = {pe.rel_path: pe.tier for pe in wiki_index.scan_pages(root)}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        rel = _reconstruct_rel(hit.get("file", ""), root)
        if rel is None or rel not in pages or rel in seen:
            continue
        seen.add(rel)
        out.append((pages[rel], rel))
        if len(out) >= k:
            break
    return out


def query(root, qmd_bin, q, k=10, timeout=_QUERY_TIMEOUT):
    """Hybrid qmd query (D-Q6) -> ranked ``[(tier, rel_path)]`` top-k wiki pages.

    Runs ``qmd query <q> --json -n <k+margin>`` with ``cwd=<root>``, over-fetching
    so the scan_pages post-filter (DD3) can drop non-page hits without starving the
    top-k. Every returned path is one ``scan_pages`` would also produce; ``raw/``
    and ``wiki/README.md`` never appear (they are not in the scan_pages set).

    Raises ``QmdError`` on a hard failure (unrunnable binary, timeout, non-zero
    exit, or unparseable stdout) so the caller can degrade to the index-direct
    path. A successful query with no page hits returns ``[]``.
    """
    over = k + _OVERFETCH_MARGIN
    try:
        cp = _run(qmd_bin, ["query", q, "--json", "-n", str(over)], root,
                  timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise QmdError(f"qmd query timed out after {timeout}s") from e
    except OSError as e:
        raise QmdError(f"qmd is not runnable: {e}") from e
    if cp.returncode != 0:
        raise QmdError(f"qmd query exited {cp.returncode}: "
                       f"{cp.stderr.strip()[:200]}")
    try:
        hits = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise QmdError(f"qmd query stdout was not JSON: {e}") from e
    if not isinstance(hits, list):
        raise QmdError("qmd query JSON payload was not a list")
    return _hits_to_pages(hits, root, k)
