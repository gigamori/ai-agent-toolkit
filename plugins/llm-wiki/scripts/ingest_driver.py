# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""ingest_driver — the deterministic ingest CLI (plan C1, §3 contract).

Owns ALL code steps + transaction state for an ingest. It does NOT author page
content and does NOT dispatch subagents (that stays in the orchestrator prompt).
State between `begin` and `finish`/`abort` is carried on disk via the
`.llmwiki.txn` sidecar — ZERO LLM-threaded transaction state (plan R-d / T2
completion criterion).

Verbs (the four transaction verbs below + the read-only `enumerate` helper,
plan §2 A-1; the four transaction verbs honor plan R-f's verb budget <=4):

  begin <root> <source> [--kind=auto|fe_b|fe_b_prime] [--write_mode=..]
        [--apply_fanout_k=..] [--doc_type=..] [--external=..]
    marker.detect -> config_resolver.resolve_all + declare_all
    -> config_resolver.check_consistency (raises ConfigInconsistency) BEFORE
       locking (plan §3 / D-c: violation surfaces before any side effect)
    -> transaction.checkpoint THEN transaction.acquire_lock (checkpoint-before-
       lock, plan §3)
    -> front-end (FE-B: frontends.fe_b ; FE-B': extract_cc_log.extract_markdown
       then frontends.fe_b_prime). The FE runs redaction/secret-scan + content-
       hash dedup itself.
    -> write the raw artifact (unless dedup no-op) + write the sidecar
    -> print JSON {declaration[], redacted_body, origin, doc_type, max_count,
       max_bytes, apply_fanout_k, dedup_noop, redaction_flags[]}.
    If dedup_noop, the caller skips stages and calls finish(fail) to roll back
    the just-written raw.

  plan-fanout <root> <stage1_proposal_path_or_json>
    touched <= k -> one cluster; touched > k -> ceil(touched/k) clusters each
    <= k. Print {clusters: [[rel_path,...], ...]}.

  finish <root> <outcome:success|fail>
    reconstruct lock handle + checkpoint from the sidecar -> join (confirm
    expected pages on disk) -> wiki_index.regenerate -> wiki_log.append
    (FE-dispatched prefix) -> single transaction.commit (success) OR
    transaction.rollback (fail) -> transaction.release_lock (always) -> delete
    the sidecar. Print {committed_sha} or {rolled_back_to}.

  abort <root>
    rollback + release_lock + delete sidecar (manual recovery, D-g). Safe when a
    sidecar exists; no-op-with-message if none.

  enumerate <root> <glob>   (read-only helper, plan §2 A-1 / G-a/G-b/G-e/G-d)
    Expand the glob in Python (`Path.glob`, no shell expansion; OS-independent +
    deterministic via sorting). Force-exclude wiki-internal paths (G-b: raw/,
    wiki/, .git/, SCHEMA.md, .llmwiki[.lock/.txn], log.md, index.md); files only.
    A directory-only argument (`./docs/`) is sugar for `<dir>/**/*` restricted to
    a text-type extension allowlist (G-e). `**` recurses (G-d). Zero matches is
    an explicit error. No lock / checkpoint / write — pure enumeration. Print
    {files: [rel_path,...], excluded: <count>, pattern: <effective glob>}.

Sidecar `.llmwiki.txn` (JSON, beside `.llmwiki.lock`):
  {checkpoint_head, stashed, origin, doc_type, max_count, max_bytes,
   apply_fanout_k, fe_hash, pid}

NOTE: the FE-B' extractor module is `extract_cc_log`.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

import marker
import config_resolver
import transaction
import frontends
import wiki_index
import wiki_log
import extract_cc_log


SIDECAR_NAME = ".llmwiki.txn"

# Front-end origins (log prefix dispatch keys). FE-B = 3rd-party source,
# FE-B' = cc-log jsonl transcript (design §4).
ORIGIN_FE_B = "fe_b"
ORIGIN_FE_B_PRIME = "fe_b_prime"


class DriverError(Exception):
    """A driver-level usage / state error (surfaced to the CLI as exit 2)."""


# --------------------------------------------------------------------------- #
# sidecar (on-disk transaction state — replaces LLM-threaded state)
# --------------------------------------------------------------------------- #
def _sidecar_path(wiki_root: "str | Path") -> Path:
    return Path(wiki_root) / SIDECAR_NAME


def _write_sidecar(wiki_root: "str | Path", state: dict) -> None:
    _sidecar_path(wiki_root).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_sidecar(wiki_root: "str | Path") -> "dict | None":
    path = _sidecar_path(wiki_root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _delete_sidecar(wiki_root: "str | Path") -> None:
    try:
        _sidecar_path(wiki_root).unlink()
    except FileNotFoundError:
        pass


def _checkpoint_from_sidecar(state: dict) -> transaction.Checkpoint:
    return transaction.Checkpoint(head=state.get("checkpoint_head"),
                                  stashed=bool(state.get("stashed")))


def _lock_handle(wiki_root: "str | Path") -> transaction.LockHandle:
    return transaction.LockHandle(path=Path(wiki_root) / transaction.LOCK_NAME)


# --------------------------------------------------------------------------- #
# verb: begin
# --------------------------------------------------------------------------- #
def _resolve_kind(kind: str) -> str:
    """Resolve the --kind selector to a front-end origin.

    `auto` defaults to FE-B (3rd-party source) — FE-B' is opt-in for cc-log
    jsonl since its extractor pass is jsonl-specific (design §4 / FE-B').
    """
    if kind in ("auto", "", None, "fe_b"):
        return ORIGIN_FE_B
    if kind == "fe_b_prime":
        return ORIGIN_FE_B_PRIME
    raise DriverError(f"unknown --kind: {kind!r} (auto|fe_b|fe_b_prime)")


def begin(wiki_root: str, source: str, *, kind: str = "auto",
          write_mode: "str | None" = None,
          apply_fanout_k: "str | None" = None,
          doc_type: "str | None" = None,
          external: "str | None" = None) -> dict:
    root = Path(wiki_root)

    # 1) marker.detect — the directory must be a wiki root (D8).
    mk = marker.detect(root)
    if mk is None:
        raise DriverError(f"no .llmwiki marker at {wiki_root} (not a wiki root)")

    # 2) config_resolver.resolve_all + declare_all.
    wiki_config = config_resolver.load_config(mk.schema_path)
    prompt_values: dict[str, str] = {}
    if write_mode:
        prompt_values["write_mode"] = write_mode
    if apply_fanout_k:
        prompt_values["apply_fanout_k"] = apply_fanout_k
    resolutions = config_resolver.resolve_all(prompt_values, wiki_config)
    declaration_block = config_resolver.declare_all(resolutions)
    declaration = declaration_block.splitlines() if declaration_block else []
    # The resolved-value declaration is announced before any write op (D5).
    print(declaration_block, file=sys.stderr)

    # 3) validate the consistency invariant BEFORE locking (D-c). A violation
    #    raises ConfigInconsistency before any side effect (no checkpoint, no
    #    lock, no FE) so begin aborts cleanly with the tree untouched.
    config_resolver.check_consistency(resolutions)

    origin = _resolve_kind(kind)

    # 3b) read the source NOW, before checkpoint. checkpoint() stashes untracked
    #     hand-edits to satisfy the clean-tree precondition (R8); if the source
    #     path happens to live inside the (untracked) wiki root, that stash would
    #     remove it from under the front-end. Reading it as pure input here (no
    #     side effect) keeps the checkpoint/stash semantics intact while ensuring
    #     the front-end sees the content. FE-B reads text+ext; FE-B' extracts the
    #     jsonl transcript to markdown.
    if origin == ORIGIN_FE_B:
        fe_b_content = Path(source).read_text(encoding="utf-8")
        fe_b_ext = Path(source).suffix.lstrip(".") or "txt"
    else:  # FE-B'
        fe_b_prime_markdown = extract_cc_log.extract_markdown(source)

    # 4) checkpoint THEN acquire_lock (checkpoint-before-lock, plan §3).
    cp = transaction.checkpoint(root)
    try:
        handle = transaction.acquire_lock(root)
    except transaction.LockHeld:
        # Checkpoint may have stashed hand-edits; restore them before failing so
        # begin leaves no orphan stash when it could not acquire the lock.
        if cp.stashed:
            transaction.rollback(root, cp)
        raise

    try:
        # 5) run the matching front-end (the source was already read in 3b; the
        #    front-end assembly is pure: redaction/secret-scan + content-hash
        #    dedup, no source read). frontends.py owns redact-before-hash (D16/D18).
        if origin == ORIGIN_FE_B:
            fe = frontends.fe_b(root, fe_b_content, fe_b_ext,
                                external_locator=external)
        else:  # FE-B': fe_b_prime over the already-extracted markdown.
            fe = frontends.fe_b_prime(root, fe_b_prime_markdown)

        # doc_type: FE-B' floor fixes transcript; FE-B takes the prompt value or
        # the FE-provided frontmatter doc_type if any.
        resolved_doc_type = (
            fe.frontmatter.get("doc_type")
            or doc_type
            or ("transcript" if origin == ORIGIN_FE_B_PRIME else "")
        )

        max_count = int(resolutions["max_count"].value)
        max_bytes = int(resolutions["max_bytes"].value)
        k = int(resolutions["apply_fanout_k"].value)

        # 6) write the raw artifact (FE does not write). On dedup no-op the raw
        #    already exists, so skip the write; the caller will finish(fail).
        if not fe.exists:
            raw_path = root / Path(fe.rel_path)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(fe.body, encoding="utf-8")

        # 7) write the sidecar (on-disk transaction state).
        _write_sidecar(root, {
            "checkpoint_head": cp.head,
            "stashed": cp.stashed,
            "origin": origin,
            "doc_type": resolved_doc_type,
            "max_count": max_count,
            "max_bytes": max_bytes,
            "apply_fanout_k": k,
            "fe_hash": fe.hash,
            "pid": handle_pid(handle),
        })
    except Exception:
        # Any failure after locking: roll back to the checkpoint (removes the
        # just-written raw + restores stash) and release the lock so begin does
        # not strand the wiki. The sidecar (if written) is removed too.
        transaction.rollback(root, cp)
        transaction.release_lock(handle)
        _delete_sidecar(root)
        raise

    # 8) print the JSON contract (plan §3).
    out = {
        "declaration": declaration,
        "redacted_body": fe.body,
        "origin": origin,
        "doc_type": resolved_doc_type,
        "max_count": max_count,
        "max_bytes": max_bytes,
        "apply_fanout_k": k,
        "dedup_noop": fe.exists,
        "redaction_flags": [asdict(f) for f in fe.redaction_flags],
    }
    return out


def handle_pid(handle: transaction.LockHandle) -> "int | None":
    """Read the pid the lock file recorded (acquire_lock writes os.getpid())."""
    try:
        return int(handle.path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# verb: plan-fanout
# --------------------------------------------------------------------------- #
def _load_touched(stage1_proposal: str) -> list[str]:
    """Accept either a path to a JSON file or an inline JSON string.

    The proposal carries the Stage1 touched-page set. Accepted shapes:
      - {"touched": [rel_path, ...]}
      - [rel_path, ...]
    """
    text = stage1_proposal
    p = Path(stage1_proposal)
    if p.is_file():
        text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriverError(f"stage1 proposal is neither a file nor JSON: {exc}") from exc
    if isinstance(data, dict):
        touched = data.get("touched", [])
    elif isinstance(data, list):
        touched = data
    else:
        raise DriverError("stage1 proposal JSON must be a list or {touched: [...]}")
    if not all(isinstance(t, str) for t in touched):
        raise DriverError("touched entries must be rel_path strings")
    return list(touched)


def plan_fanout(wiki_root: str, stage1_proposal: str) -> dict:
    """touched <= k -> one cluster; touched > k -> ceil(touched/k) clusters,

    each cluster <= k (plan §3 / F6 cluster split = code, D23)."""
    root = Path(wiki_root)
    state = _read_sidecar(root)
    if state is None:
        raise DriverError("no .llmwiki.txn sidecar; call begin first")
    k = int(state["apply_fanout_k"])
    if k <= 0:
        raise DriverError(f"apply_fanout_k must be positive, got {k}")
    touched = _load_touched(stage1_proposal)
    n = len(touched)
    if n <= k:
        clusters = [touched] if touched else []
    else:
        num = math.ceil(n / k)
        clusters = [touched[i * k:(i + 1) * k] for i in range(num)]
    return {"clusters": clusters}


# --------------------------------------------------------------------------- #
# verb: finish
# --------------------------------------------------------------------------- #
def _log_header_for_origin(origin: str) -> tuple[str, str]:
    if origin == ORIGIN_FE_B:
        return wiki_log.header_for_fe_b()
    if origin == ORIGIN_FE_B_PRIME:
        return wiki_log.header_for_fe_b_prime()
    raise DriverError(f"unknown origin in sidecar: {origin!r}")


def finish(wiki_root: str, outcome: str, *,
           expected_pages: "list[str] | None" = None,
           title: "str | None" = None) -> dict:
    if outcome not in ("success", "fail"):
        raise DriverError(f"outcome must be success|fail, got {outcome!r}")
    root = Path(wiki_root)

    # reconstruct lock handle + checkpoint from the sidecar (zero LLM-threaded
    # state — D21 single transaction, D23 central join).
    state = _read_sidecar(root)
    if state is None:
        raise DriverError("no .llmwiki.txn sidecar; nothing to finish")
    cp = _checkpoint_from_sidecar(state)
    handle = _lock_handle(root)

    try:
        if outcome == "success":
            # Any failure on the success path (missing join, regenerate, log
            # append, or commit) must roll back to the checkpoint before the
            # finally releases the lock + deletes the sidecar — otherwise a
            # partial index/log/raw + a stale lock/sidecar would strand with no
            # recovery handle. This makes finish honour its invariant: exactly one
            # of commit / rollback before release_lock (wiki-ingest.md §finish),
            # symmetric with begin's post-lock except.
            try:
                # join: confirm the expected pages are on disk (D23 central join).
                missing = [p for p in (expected_pages or [])
                           if not (root / Path(p)).exists()]
                if missing:
                    raise DriverError(f"expected pages missing on disk: {missing}")
                # central index/log regenerate + append (inside the single tx).
                wiki_index.regenerate(root)
                op, tag = _log_header_for_origin(state["origin"])
                wiki_log.append(root / "log.md", op, tag,
                                title or f"ingest {state.get('fe_hash', '')[:12]}")
                sha = transaction.commit(root, f"ingest: {title or state.get('fe_hash','')[:12]}")
                result = {"committed_sha": sha}
            except Exception:
                transaction.rollback(root, cp)
                raise
        else:  # fail
            transaction.rollback(root, cp)
            result = {"rolled_back_to": cp.head}
    finally:
        # release_lock always; delete sidecar on every terminal path (R-a).
        transaction.release_lock(handle)
        _delete_sidecar(root)
    return result


# --------------------------------------------------------------------------- #
# verb: abort
# --------------------------------------------------------------------------- #
def abort(wiki_root: str) -> dict:
    """Manual recovery (D-g): rollback + release_lock + delete sidecar.

    Safe to call when a sidecar exists; no-op-with-message if none.
    """
    root = Path(wiki_root)
    state = _read_sidecar(root)
    if state is None:
        return {"aborted": False, "message": "no .llmwiki.txn sidecar; nothing to abort"}
    cp = _checkpoint_from_sidecar(state)
    handle = _lock_handle(root)
    try:
        transaction.rollback(root, cp)
    finally:
        transaction.release_lock(handle)
        _delete_sidecar(root)
    return {"aborted": True, "rolled_back_to": cp.head}


# --------------------------------------------------------------------------- #
# verb: enumerate  (read-only glob expansion; plan §2 A-1, G-a/G-b/G-e/G-d)
# --------------------------------------------------------------------------- #
# Force-excluded wiki-internal paths (G-b): self-ingestion guard. Any candidate
# whose POSIX relative path is, or lives under, one of these is dropped.
_EXCLUDED_DIRS = ("raw", "wiki", ".git")
_EXCLUDED_FILES = (
    "SCHEMA.md", ".llmwiki", ".llmwiki.lock", ".llmwiki.txn",
    "log.md", "index.md",
)

# Text-type default extension allowlist (G-e). A directory-only argument
# (e.g. `./docs/`) is sugar for `<dir>/**/*` restricted to these extensions;
# non-text (images etc.) is never picked. Fixed, documented allowlist.
_TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".text", ".json", ".jsonl",
})


def _is_excluded_internal(rel_posix: str) -> bool:
    """True if a wiki-relative POSIX path is a force-excluded internal path (G-b)."""
    parts = rel_posix.split("/")
    # Drop anything under an excluded directory (raw/, wiki/, .git/) at any depth.
    if any(seg in _EXCLUDED_DIRS for seg in parts):
        return True
    # Drop excluded files matched anywhere in the tree (e.g. nested SCHEMA.md).
    if parts[-1] in _EXCLUDED_FILES:
        return True
    return False


def enumerate_files(wiki_root: str, glob: str) -> dict:
    """Expand a glob in Python (G-a), drop internal paths (G-b), files only.

    - `glob` is expanded with `pathlib.Path.glob` relative to `wiki_root`
      (NO shell expansion; OS-independent + deterministic via sorting).
    - A directory-only argument (trailing `/`, or an existing directory with no
      glob metacharacters) is sugar for `<dir>/**/*` restricted to the text-type
      extension allowlist (G-e).
    - An explicit glob with its own extension is honored as-is; the G-b internal
      exclusions still apply.
    - `**` recursion works (G-d). Zero matches is an explicit error (G-d).

    Read-only: no lock, no checkpoint, no writes (the per-file transaction stays
    in begin->finish; TA2 wires the loop).

    Output: {files: [<rel path>...], excluded: <count>, pattern: <effective glob>}.
    """
    root = Path(wiki_root)

    # Directory-only sugar (G-e): a trailing slash, or a metacharacter-free token
    # that resolves to an existing directory, becomes `<dir>/**/*` + allowlist.
    glob_meta = any(c in glob for c in "*?[")
    is_dir_only = glob.endswith(("/", "\\")) or (
        not glob_meta and (root / glob).is_dir()
    )
    if is_dir_only:
        dir_token = glob.rstrip("/\\")
        effective_pattern = f"{dir_token}/**/*" if dir_token else "**/*"
        restrict_to_text = True
    else:
        effective_pattern = glob
        restrict_to_text = False

    matches = sorted(root.glob(effective_pattern))

    files: list[str] = []
    excluded = 0
    for path in matches:
        if not path.is_file():          # files only (drop directory entries)
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            # A match outside the root (e.g. via `..`/absolute glob) is not a
            # wiki-relative path; treat as excluded rather than emit it.
            excluded += 1
            continue
        rel_posix = rel.as_posix()
        if _is_excluded_internal(rel_posix):
            excluded += 1
            continue
        if restrict_to_text and path.suffix.lower() not in _TEXT_EXTENSIONS:
            excluded += 1
            continue
        files.append(rel_posix)

    if not files:
        raise DriverError(
            f"glob matched zero files: {glob!r} "
            f"(effective pattern {effective_pattern!r}, {excluded} excluded)"
        )

    return {"files": files, "excluded": excluded, "pattern": effective_pattern}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_opts(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    for tok in argv:
        if tok.startswith("--"):
            key, _, val = tok[2:].partition("=")
            opts[key] = val
        else:
            positional.append(tok)
    return positional, opts


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: ingest_driver.py "
              "<begin|plan-fanout|finish|abort|enumerate> ...",
              file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    pos, opts = _parse_opts(rest)
    try:
        if verb == "begin":
            if len(pos) < 2:
                raise DriverError("begin requires <root> <source>")
            result = begin(
                pos[0], pos[1],
                kind=opts.get("kind", "auto"),
                write_mode=opts.get("write_mode"),
                apply_fanout_k=opts.get("apply_fanout_k"),
                doc_type=opts.get("doc_type"),
                external=opts.get("external"),
            )
        elif verb == "plan-fanout":
            if len(pos) < 2:
                raise DriverError("plan-fanout requires <root> <stage1_proposal_path_or_json>")
            result = plan_fanout(pos[0], pos[1])
        elif verb == "finish":
            if len(pos) < 2:
                raise DriverError("finish requires <root> <outcome:success|fail>")
            expected = opts.get("expected_pages")
            expected_pages = expected.split(",") if expected else None
            result = finish(pos[0], pos[1],
                            expected_pages=expected_pages,
                            title=opts.get("title"))
        elif verb == "abort":
            if len(pos) < 1:
                raise DriverError("abort requires <root>")
            result = abort(pos[0])
        elif verb == "enumerate":
            if len(pos) < 2:
                raise DriverError("enumerate requires <root> <glob>")
            result = enumerate_files(pos[0], pos[1])
        else:
            print(f"unknown verb: {verb!r} "
                  "(begin|plan-fanout|finish|abort|enumerate)",
                  file=sys.stderr)
            return 2
    except config_resolver.ConfigInconsistency as e:
        print(f"config-inconsistency: {e}", file=sys.stderr)
        return 2
    except extract_cc_log.ExtractError as e:
        print(f"extract: {e}", file=sys.stderr)
        return 3
    except (DriverError, transaction.LockHeld, transaction.GitError,
            transaction.NotARepoRoot) as e:
        print(str(e), file=sys.stderr)
        return 2

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
