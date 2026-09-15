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
  - **typed query, no rerank (D-Q6):** the query is sent as a typed query
    DOCUMENT (a ``vec:`` line plus one ``lex:`` line per term), not as bare
    text, and with ``--no-rerank``. Together these skip both of qmd's
    model-backed stages — expansion is never invoked for a typed document, and
    the reranker is off — while the ``lex:`` half preserves the freshness that
    ``qmd update`` provides. See ``_query_doc``.
  - **launched past the Windows shim:** because that document is multi-line and
    cmd.exe truncates a command line at a newline, an npm ``.CMD`` shim is
    unwrapped to its JS entry and run under node. See ``_launcher``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from llmwiki.core import wiki_index

_URI_PREFIX = "qmd://"
# Over-fetch k+margin from qmd so the scan_pages post-filter (DD3) can drop
# non-page hits (e.g. wiki/README.md) without starving the top-k result slots.
_OVERFETCH_MARGIN = 10
# A `vec:` line rejects two things on qmd 2.5.3 and exits 1: an unbalanced `"`,
# and a `-` at line start or after whitespace (parsed as an unsupported
# negation). Both are ordinary in a phrased question, so they are stripped.
_VEC_NEGATION_RE = re.compile(r'(^|\s)-(?=[\w"])')
# Terms for the `lex:` half. ASCII runs of >=3 chars (hyphen/underscore kept),
# and CJK runs of >=2 chars after splitting on hiragana, which is inflection
# rather than content. Capped so one long question cannot become a huge argv.
_LEX_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
_CJK_RUN_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+")
_HIRAGANA_RE = re.compile(r"[぀-ゟ]+")
_LEX_TERMS_MAX = 12
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "with", "this", "that", "from", "into", "your",
    "you", "all", "can", "has", "have", "had", "was", "were", "been", "being",
    "but", "not", "out", "get", "set", "its", "will", "may", "one", "two",
    "more", "than", "then", "when", "what", "how", "why", "who", "which",
    "such", "run", "use", "add", "does", "did", "where", "about",
})
# npm installs a Node CLI on Windows as a `.CMD` shim, and cmd.exe TRUNCATES the
# command line at the first newline inside any argument — silently, with exit 0.
# A typed query document is multi-line, so through the shim every line after the
# first is lost AND so is every flag after it (measured: `--json` dropped, the
# reranker ran anyway). The shim's last line names the JS entry it would run, so
# it is parsed out and launched under node directly. Matches the last
# `"%dp0%\…"` token, which is that entry (an earlier one names node.exe itself).
_SHIM_SUFFIXES = (".cmd", ".bat")
_SHIM_TARGET_RE = re.compile(r'"%dp0%\\([^"]+)"')
# Default ceiling for a single query. The typed document plus
# --no-rerank skips BOTH model-backed cost sources (query expansion, reranker),
# leaving only the embedding-model load that every query pays — qmd loads it per
# process and never caches the query embedding. A query that has not answered
# inside this window is stuck, not slow. The one-time `qmd embed` is deliberately
# NOT bounded by this; see ``ensure_collection``.
_QUERY_TIMEOUT = 30


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


def _launcher(qmd_bin):
    """``qmd_bin`` -> the argv prefix that runs it, bypassing a Windows shim.

    Resolves ``qmd_bin`` via ``shutil.which``: on Windows an npm-installed CLI is
    a ``qmd.CMD`` shim and ``subprocess`` cannot launch it by the bare name
    ``qmd`` (WinError 2) — only by the resolved path. On POSIX this is a harmless
    identity resolution and the resolved path is returned as-is.

    A shim is then unwrapped to ``[node, <its JS entry>]``, because cmd.exe
    truncates the command line at a newline and the query is a multi-line typed
    document (see ``_SHIM_TARGET_RE``). Node is picked the way the shim itself
    picks it: a ``node.exe`` sitting beside the shim wins, else PATH. Anything
    unexpected — unreadable shim, unrecognized shape, missing target — falls back
    to the resolved path, which still runs single-line commands correctly.
    """
    exe = shutil.which(qmd_bin) or qmd_bin
    if not exe.lower().endswith(_SHIM_SUFFIXES):
        return [exe]
    try:
        shim = Path(exe).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [exe]
    targets = _SHIM_TARGET_RE.findall(shim)
    if not targets:
        return [exe]
    here = Path(exe).parent
    entry = here / targets[-1].replace("\\", os.sep)
    if not entry.exists():
        return [exe]
    local_node = here / "node.exe"
    node = str(local_node) if local_node.exists() else (shutil.which("node") or "node")
    return [node, str(entry)]


def _run(qmd_bin, args, root, timeout=None):
    """Run ``qmd <args>`` with ``cwd=<root>`` (D-Q3 project-local resolution).

    The binary is located by ``_launcher``, which also unwraps a Windows npm
    shim so that multi-line arguments survive.

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
    env = {**os.environ, "PWD": os.path.abspath(root)}
    return subprocess.run(
        [*_launcher(qmd_bin), *args],
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
    tolerated (verified on qmd 2.5.3).

    ``embed`` is the exception: its exit status IS checked. Unlike the other two it
    has no benign non-zero, and a failed embed leaves the index with no vectors
    while ``query`` keeps exiting 0 on the BM25 half — so tolerating it reports a
    built index that cannot answer. Observed on an AMD Radeon 780M, where the
    embedding pass aborts with 0xC0000409 unless ``GGML_VK_DISABLE_COOPMAT=1`` is
    in the environment.

    **Blocks with NO ceiling, by choice.** ``qmd embed`` is one-time index
    construction whose cost scales with the wiki; a timeout here would abort the
    build partway instead of making it faster, and qmd activates only above
    ``qmd_page_threshold`` pages. On the ``cli.py`` search path this runs inline
    when ``.qmd/`` is absent, so the FIRST query on a never-reindexed wiki can
    block for many minutes. ``/wiki-reindex`` (S5) exists to pay it out of band;
    ``_QUERY_TIMEOUT`` bounds the query only, never this.
    """
    wiki_dir = str(Path(root) / wiki_index.WIKI_DIR)
    try:
        _run(qmd_bin, ["init"], root)
        _run(qmd_bin, ["collection", "add", wiki_dir], root)
        cp = _run(qmd_bin, ["embed"], root)
    except OSError as e:  # binary missing / not executable
        raise QmdError(f"qmd is not runnable: {e}") from e
    if cp.returncode != 0:
        raise QmdError(f"qmd embed exited {cp.returncode}: "
                       f"{cp.stderr.strip()[:200]}")


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


def _lex_terms(q: str) -> list:
    """The terms of ``q``, one per returned item — never joined into one line.

    qmd ANDs the terms *inside* a single ``lex:`` line, so a whole question on
    one line matches almost nothing; separate lines combine by RRF instead. CJK
    is split on hiragana and matched as an exact substring (qmd indexes CJK
    character-by-character for FTS and has no word segmentation), so the runs
    between inflections are the units worth sending.
    """
    out = []
    for word in _LEX_WORD_RE.findall(q):
        term = word.lower().strip("-_")
        if len(term) >= 3 and term not in _STOPWORDS and term not in out:
            out.append(term)
    for run in _CJK_RUN_RE.findall(q):
        for frag in _HIRAGANA_RE.split(run):
            if len(frag) >= 2 and frag not in out:
                out.append(frag)
    return [t.replace('"', "") for t in out][:_LEX_TERMS_MAX]


def _query_doc(q: str) -> str:
    """A phrased query -> a typed qmd query document (``vec:`` + ``lex:`` lines).

    Two properties make this the shape to send, and BOTH are load-bearing:

      - **typed at all:** a typed document enters ``structuredSearch``, which
        never invokes query expansion. Bare text invokes it unless BM25 happens
        to be confident — a threshold, not a guarantee, and it fails exactly
        where BM25 is weakest.
      - **a lexical half:** ``qmd update`` (run before every query, D-Q6) indexes
        a new page lexically but does NOT embed it — qmd prints "Run 'qmd embed'"
        and leaves vectors to ``/wiki-reindex``. A ``vec:``-only document could
        therefore not see a page ingested since the last embed. The ``lex:``
        lines are what keep D-Q6's within-query freshness true.

    Each typed line is collapsed to one line, and the characters qmd rejects in
    a ``vec:`` line are stripped.
    """
    vec = " ".join(_VEC_NEGATION_RE.sub(r"\1", q.replace('"', " ")).split())
    return "\n".join(["vec: " + vec] + ["lex: " + t for t in _lex_terms(q)])


def query(root, qmd_bin, q, k=10, timeout=_QUERY_TIMEOUT):
    """qmd query (D-Q6) -> ranked ``[(tier, rel_path)]`` top-k wiki pages.

    Runs ``qmd query 'vec: <q>' --no-rerank --json -n <k+margin>`` with
    ``cwd=<root>``, over-fetching so the scan_pages post-filter (DD3) can drop
    non-page hits without starving the top-k. Every returned path is one
    ``scan_pages`` would also produce; ``raw/`` and ``wiki/README.md`` never
    appear (they are not in the scan_pages set).

    Raises ``QmdError`` on a hard failure (unrunnable binary, timeout, non-zero
    exit, or unparseable stdout) so the caller can degrade to the index-direct
    path. A successful query with no page hits returns ``[]``.
    """
    over = k + _OVERFETCH_MARGIN
    try:
        cp = _run(qmd_bin,
                  ["query", _query_doc(q), "--no-rerank", "--json", "-n", str(over)],
                  root, timeout=timeout)
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
