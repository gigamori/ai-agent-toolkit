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

Verbs (the four transaction verbs below + the read-only `enumerate`,
`session-plan` and `project-batch` helpers, plan §2 A-1; the four transaction
verbs honor plan R-f's verb budget <=4 — the read-only helpers are OUTSIDE that
budget, opening no transaction):

  begin <root> <source> [--kind=auto|fe_b|fe_b_prime] [--write_mode=..]
        [--apply_fanout_k=..] [--doc_type=..] [--external=..] [--turns=<path>]
    marker.detect -> config_resolver.resolve_all + declare_all
    -> config_resolver.check_consistency (raises ConfigInconsistency) BEFORE
       locking (plan §3 / D-c: violation surfaces before any side effect)
    -> transaction.acquire_lock THEN transaction.checkpoint (lock-first, so only
       the lock holder ever creates the fixed-path journal dir; F1 fix)
    -> front-end (FE-B: frontends.fe_b ; FE-B': cc_log_project.project_owned
       then frontends.fe_b_prime). The FE runs redaction/secret-scan + content-
       hash dedup itself.
    -> write the raw artifact (unless dedup no-op) + write the sidecar
    -> print JSON {declaration[], redacted_body, origin, doc_type, max_count,
       max_bytes, apply_fanout_k, dedup_noop, redaction_flags[], ledger_skipped}.
    If dedup_noop, the raw already existed so it was NOT written; the caller
    still skips the stages and calls finish(fail) to release the lock and
    discard the journal (the raw rollback is a harmless no-op).
    `--turns=<path>` (FE-B' Path B, R1/F-H1): use the pre-extracted turns at
    <path> (from `project-batch`) instead of re-scanning the corpus — begin then
    runs only the cheap per-sid projection half (project_from_turns). Path A
    omits it (begin extracts the one sid itself via project_owned).

  plan-fanout <root> <stage1_proposal_path_or_json>
    touched <= k -> one cluster; touched > k -> ceil(touched/k) clusters each
    <= k. touched > max_count -> DriverError (F2 ingest-grain budget / human gate,
    since the per-worker WriteSession budget would otherwise be multiplied by the
    cluster count). Print {clusters: [[rel_path,...], ...]}.

  finish <root> <outcome:success|fail>
    reconstruct lock handle + checkpoint from the sidecar -> join (confirm
    expected pages on disk) -> wiki_index.regenerate -> wiki_log.append
    (FE-dispatched prefix) -> transaction.commit (discard journal, success) OR
    transaction.rollback (replay journal, fail) -> transaction.release_lock
    (always) -> delete the sidecar. Print {committed:true} or {rolled_back:true}.

  abort <root>
    rollback + release_lock + delete sidecar (manual recovery, D-g). Safe when a
    sidecar exists; no-op-with-message if none.

  enumerate <root> <glob>   (read-only helper, plan §2 A-1 / G-a/G-b/G-e/G-d)
    Expand the glob in Python (`Path.glob`, no shell expansion; OS-independent +
    deterministic via sorting). Force-exclude wiki-internal paths (G-b: raw/,
    wiki/, .git/, .qmd/, SCHEMA.md, .llmwiki[.lock/.txn], log.md, index.md,
    .cc-turn-ledger.jsonl); files only. (F7: the turn ledger is a driver state
    file — excluding it prevents enumerate from self-ingesting it.)
    A directory-only argument (`./docs/`) is sugar for `<dir>/**/*` restricted to
    a text-type extension allowlist (G-e). `**` recurses (G-d). Zero matches is
    an explicit error. No lock / checkpoint / write — pure enumeration. Print
    {files: [rel_path,...], excluded: <count>, pattern: <effective glob>}.

  session-plan <root> [--pj <name>]   (read-only Path B resolver, T6 / F1-b/F2-B)
    Resolve the SET of cc-log session ids for a Path B project ingest and return
    them ORDERED BY session-start ts ASCENDING — and nothing else. It does NOT
    partition ownership / emit an owned-turn manifest (F1-b/F2-B: ownership is
    decided per-begin by the ledger diff); it does NOT open a transaction (read
    only, outside the verb budget).
      - --pj <name> given: filter `_projects/_state/*.json` (the taskflow state
        dir under the process CWD, exactly as wiki_root_resolver locates it) by
        `project == <name>`; the matching state files' filename STEMS are the
        sids (state file is `<sid>.json`, `{"project": ...}`).
      - --pj omitted: resolve the CC project dir from the RUNNING session's own
        log location as ground truth (U3) — find `~/.claude/projects/*/<current-
        sid>.jsonl` (current-sid = $CLAUDE_SESSION_ID) and take its PARENT dir
        (CC-internal-encoding independent), then the sids are that dir's
        `*.jsonl` stems. SECONDARY fallback only: reverse-generate the slug from
        the CWD (path separators + the drive colon -> `-`).
    ts to sort by = each sid's earliest record timestamp (`cc_session.started` =
    min(ts) in the vendored views) — the authoritative in-log event clock, not
    file mtime (mtime drifts on copy/resume/fork/checkout). Zero matches is an
    explicit error (fail-closed, like `enumerate`). Print {sids: [sid,...],
    scope: "pj"|"cwd", pattern: <state-glob or project-dir>}.

  project-batch <root> <sid> [<sid>...]   (read-only Path B scan-collapse; R1/F-H1)
    Extract the turns for ALL given sids in ONE corpus scan
    (cc_log_project.extract_turns_batch) so Path B does not re-scan
    ~/.claude/projects once per begin (N sids -> N scans). Writes each sid's
    extracted turns (boilerplate-stripped, F5-hash-carrying) to a per-sid JSON
    file under a fresh temp dir OUTSIDE the wiki root (never journaled / never
    enumerated), and prints {out_dir, turns: {sid: <path>}, scanned}. The Path B
    loop passes each begin `--turns=<path>` so begin runs only the cheap per-sid
    half (project_from_turns) — the ledger read-after-write (F3) stays sequential.
    Opens NO transaction (outside the R-f verb budget). The loop owns temp cleanup.

Sidecar `.llmwiki.txn` (JSON, beside `.llmwiki.lock`):
  {journal_dir, origin, doc_type, max_count, max_bytes,
   apply_fanout_k, fe_hash, pid, lock_token, pending_ledger_entries}

`pending_ledger_entries` (S8-b / T4) carries the novel turn-content-hash
entries from `begin` to `finish` on disk. `finish(success)` journals the turn
ledger (`.cc-turn-ledger.jsonl`) then appends them inside the single
transaction; on rollback the journal reverts the append (F3). The T2 projector
populates this list; the current FE paths emit none, so `begin` writes it empty.

`lock_token` (DEC-R1=D) records the ownership token `begin` wrote into the lock
file, so `finish` / `abort` refuse to operate on a transaction they do not own
(token mismatch) — defense-in-depth atop the residue-shape reclaim guard.

NOTE: the FE-B' projector module is `cc_log_project` (fork-aware; projects a
single sid out of the vendored DuckDB views, dedups + ledger-diffs the turns).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from llmwiki.core import marker
from llmwiki.core import config_resolver
from llmwiki.write import transaction
from llmwiki.ingest import frontends
from llmwiki.core import wiki_index
from llmwiki.core import wiki_log
from llmwiki.ingest import cc_log_project
from llmwiki.ingest import ledger


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
    return transaction.Checkpoint(journal_dir=state.get("journal_dir", ""))


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
          external: "str | None" = None,
          turns: "str | None" = None) -> dict:
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

    # 3b) read the source as pure input (no side effect). FE-B reads text+ext;
    #     FE-B' extracts the jsonl transcript to markdown. A non-UTF-8 (binary)
    #     source is a clean per-file DriverError (R6) — so a glob loop counts it
    #     as a failure and continues (G-f) instead of dying with a traceback.
    if origin == ORIGIN_FE_B:
        try:
            fe_b_content = Path(source).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DriverError(
                f"source is not UTF-8 text (binary?): {source}") from exc
        fe_b_ext = Path(source).suffix.lstrip(".") or "txt"
    else:  # FE-B'
        # Normalize the source to a session id. Path A surface: `source` is a CC
        # session jsonl path whose filename stem IS the sid (empirically
        # `<sid>.jsonl`, 100%). Path B's session-plan sid (T6) reuses this branch
        # by passing the sid as the source (its stem is the sid too, so the same
        # derivation holds). The projector (T2) projects that sid (+ agent
        # children) out of the vendored DuckDB views, strips boilerplate,
        # length-independent exact-dedups, diffs the turn ledger (T4) and renders
        # FE-B'-compatible markdown of the NOVEL turns.
        sid = Path(source).stem
        if turns is not None:
            # Path B scan-collapse (R1/F-H1): the turns were already extracted by
            # the read-only `project-batch` verb in ONE corpus scan before the
            # loop; `--turns` is the path to this sid's turn-JSON. begin does NOT
            # re-scan — it only runs the cheap per-sid half (dedup + ledger diff +
            # markdown) so the ledger read-after-write (F3) stays sequential.
            turns_path = Path(turns)
            try:
                extracted = json.loads(turns_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DriverError(
                    f"--turns file unreadable or not JSON: {turns}") from exc
            # Guard the sid<->turns pairing: the turn-JSON's sid must match the
            # source's sid (a mismatched pairing would silently mis-attribute a
            # session's turns). The batch file records its sid under "sid"; each
            # turn's provenance also carries the owning sid on the first entry.
            turns_sid = extracted.get("sid") if isinstance(extracted, dict) else None
            turn_list = (extracted.get("turns") if isinstance(extracted, dict)
                         else extracted)
            if turns_sid is not None and turns_sid != sid:
                raise DriverError(
                    f"--turns sid mismatch: file is for {turns_sid!r} but source "
                    f"resolves to {sid!r}")
            if not isinstance(turn_list, list):
                raise DriverError("--turns JSON must be a list or {sid, turns:[...]}")
            proj = cc_log_project.project_from_turns(
                root, sid, turn_list, ledger=ledger)
        else:
            proj = cc_log_project.project_owned(root, sid, ledger=ledger)

    # 4) acquire_lock THEN checkpoint (lock-first). Only the lock holder ever
    #    creates/touches the fixed-path journal dir, so a second ingest that fails
    #    to lock never races on it (F1 fix; the old checkpoint-before-lock order
    #    existed only for the now-removed git stash). LockHeld propagates before
    #    any journal is created — nothing to clean up.
    handle = transaction.acquire_lock(root)
    try:
        cp = transaction.checkpoint(root)
    except Exception:
        transaction.release_lock(handle)
        raise

    try:
        # 5) run the matching front-end (the source was already read in 3b; the
        #    front-end assembly is pure: redaction/secret-scan + content-hash
        #    dedup, no source read). frontends.py owns redact-before-hash (D16/D18).
        if origin == ORIGIN_FE_B:
            fe = frontends.fe_b(root, fe_b_content, fe_b_ext,
                                external_locator=external)
        else:  # FE-B': fe_b_prime over the projected novel-turn markdown.
            fe = frontends.fe_b_prime(root, proj.markdown)

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
        #    Journal the create BEFORE writing so a failed finish/abort removes the
        #    orphan raw (required for D18 dedup correctness).
        if not fe.exists:
            transaction.journal_before_write(root, [fe.rel_path])
            raw_path = root / Path(fe.rel_path)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(fe.body, encoding="utf-8")

        # 7) write the sidecar (on-disk transaction state). `lock_token` records
        #    the ownership token so finish/abort can refuse a foreign txn (DEC-R1=D).
        #    `pending_ledger_entries` carries the novel turn-content-hash entries
        #    (S8-b / T4) from begin to finish on-disk (ZERO LLM-threaded state);
        #    finish(success) journals + appends them to the turn ledger. The FE-B'
        #    projector (cc_log_project.project_owned) populates this at projection
        #    time (it diffs each projected turn's hash against
        #    ledger.read_seen_hashes and emits the novel LedgerEntry list); the
        #    FE-B path has no projection, so it emits none.
        _write_sidecar(root, {
            "journal_dir": cp.journal_dir,
            "origin": origin,
            "doc_type": resolved_doc_type,
            "max_count": max_count,
            "max_bytes": max_bytes,
            "apply_fanout_k": k,
            "fe_hash": fe.hash,
            "pid": handle_pid(handle),
            "lock_token": handle.token,
            # FE-B' carries the projector's novel turn-content-hash entries;
            # FE-B has no projection so it emits none.
            "pending_ledger_entries": (
                proj.novel_entries if origin == ORIGIN_FE_B_PRIME else []
            ),
        })
    except Exception:
        # Any failure after locking: replay the journal (removes the just-written
        # raw, if any) and release the lock so begin does not strand the wiki. The
        # sidecar (if written) is removed too.
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
        # FE-B' per-run ledger-skipped TURN count (F6): how many projected turns
        # were dropped because a prior ingest already owns them (turn-content-hash
        # ledger diff). Surfaced so the Path B loop (wiki-ingest-project.md) can sum
        # it across sids and report it — an incremental re-run must not look like a
        # silent no-op (RS-d). Gated to FE-B' exactly like pending_ledger_entries;
        # FE-B has no projection so it is 0.
        "ledger_skipped": (
            proj.ledger_skipped if origin == ORIGIN_FE_B_PRIME else 0
        ),
    }
    return out


def handle_pid(handle: transaction.LockHandle) -> "int | None":
    """Read the pid the lock file recorded (acquire_lock writes {pid, token})."""
    try:
        data = json.loads(handle.path.read_text(encoding="utf-8"))
        return int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
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

    each cluster <= k (plan §3 / F6 cluster split = code, D23). Total touched is
    first gated against max_count (F2 ingest-grain budget) -> human gate."""
    root = Path(wiki_root)
    state = _read_sidecar(root)
    if state is None:
        raise DriverError("no .llmwiki.txn sidecar; call begin first")
    k = int(state["apply_fanout_k"])
    if k <= 0:
        raise DriverError(f"apply_fanout_k must be positive, got {k}")
    touched = _load_touched(stage1_proposal)
    n = len(touched)
    # F2: max_count is the per-apply-worker WriteSession budget (write_tool.py);
    # fanout would otherwise multiply it (ceil(n/k) workers x max_count each), so
    # the D19 "budget overflow -> human gate" never fires at the INGEST grain.
    # Gate the whole touched set here — the one place the total n is known before
    # any Stage2 write — so a runaway Stage1 proposal is escalated, not silently
    # fanned out. (k <= max_count holds by check_consistency, so a legal single
    # cluster never trips this.)
    max_count = int(state["max_count"])
    if n > max_count:
        raise DriverError(
            f"budget overflow: touched pages ({n}) > max_count ({max_count}); "
            f"escalate to the human gate")
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
    # state — single transaction, D23 central join).
    state = _read_sidecar(root)
    if state is None:
        raise DriverError("no .llmwiki.txn sidecar; nothing to finish")
    # DEC-R1=D ownership check: the on-disk lock must be the one begin acquired.
    # When BOTH tokens exist and disagree, this residue belongs to a DIFFERENT
    # ingest -> refuse WITHOUT touching its lock/journal/sidecar (do not
    # commit/rollback/release someone else's transaction).
    expected_token = state.get("lock_token")
    actual_token = transaction.read_lock_token(root)
    if (expected_token is not None and actual_token is not None
            and expected_token != actual_token):
        raise DriverError(
            "lock ownership mismatch: .llmwiki.lock is held by a different "
            "ingest; refusing to finish (recover the owning transaction via `abort`)")
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
                # Journal index.md/log.md AND the turn ledger before the central
                # writers mutate them (raw + pages were already journaled by
                # begin / ingest-apply). The ledger is a DRIVER-written state file
                # exactly like index.md/log.md — same journal-before-write path,
                # so rollback restores its pre-append backup (F3: on fail the
                # novel entries are NOT appended). The Stage2 allowlist write tool
                # is never involved.
                pending = [ledger.LedgerEntry(**e)
                           for e in state.get("pending_ledger_entries", [])]
                journal_targets = ["index.md", "log.md"]
                if pending:
                    journal_targets.append(ledger.LEDGER_NAME)
                transaction.journal_before_write(root, journal_targets)
                wiki_index.regenerate(root)
                op, tag = _log_header_for_origin(state["origin"])
                wiki_log.append(root / "log.md", op, tag,
                                title or f"ingest {state.get('fe_hash', '')[:12]}")
                # Append the novel turn-content-hash entries (S8-b) LAST, still
                # inside the same transaction, so a failure above reaches the
                # except -> rollback and the ledger append is never committed.
                ledger.append_entries(root, pending)
                transaction.commit(root, f"ingest: {title or state.get('fe_hash','')[:12]}")
                result = {"committed": True}
            except Exception:
                transaction.rollback(root, cp)
                raise
        else:  # fail
            transaction.rollback(root, cp)
            result = {"rolled_back": True}
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

    Sidecar-INDEPENDENT (F1): `begin` writes the sidecar LAST (after the lock, the
    journal dir and the raw artifact), so a hard crash in that window leaves a held
    lock + a journal (+ orphan raw) but NO sidecar. Keying recovery on the sidecar
    alone would strand that lock forever and leave the orphan raw to poison D18
    dedup. So abort recovers from ANY of {sidecar, journal dir, lock file}: it
    replays the journal (fixed path under the root — removes the orphan raw) and
    releases the lock regardless of whether the sidecar was ever written. Truly
    nothing present => no-op-with-message. Idempotent (rollback is a no-op replay).
    """
    root = Path(wiki_root)
    state = _read_sidecar(root)
    journal_present = (root / transaction.JOURNAL_DIR).is_dir()
    lock_present = (root / transaction.LOCK_NAME).is_file()
    if state is None and not journal_present and not lock_present:
        return {"aborted": False, "message": "no .llmwiki.txn sidecar; nothing to abort"}
    # DEC-R1=D ownership check: refuse only when BOTH a sidecar token AND a live
    # lock token exist and disagree (this residue is a DIFFERENT ingest). When
    # the sidecar is absent (F1 crash shape) there is no token to compare, so
    # sidecar-independent recovery from {journal, lock} still proceeds.
    if state is not None:
        expected_token = state.get("lock_token")
        actual_token = transaction.read_lock_token(root)
        if (expected_token is not None and actual_token is not None
                and expected_token != actual_token):
            return {"aborted": False,
                    "message": "lock ownership mismatch; residue belongs to a "
                               "different ingest — not aborting"}
    # journal_dir is the fixed path under the root; the sidecar copy is only a hint.
    cp = (_checkpoint_from_sidecar(state) if state is not None
          else transaction.Checkpoint(journal_dir=str(root / transaction.JOURNAL_DIR)))
    handle = _lock_handle(root)
    try:
        transaction.rollback(root, cp)
    finally:
        transaction.release_lock(handle)
        _delete_sidecar(root)
    return {"aborted": True, "recovered_without_sidecar": state is None}


# --------------------------------------------------------------------------- #
# verb: enumerate  (read-only glob expansion; plan §2 A-1, G-a/G-b/G-e/G-d)
# --------------------------------------------------------------------------- #
# Force-excluded wiki-internal paths (G-b): self-ingestion guard. Any candidate
# whose POSIX relative path is, or lives under, one of these is dropped.
_EXCLUDED_DIRS = ("raw", "wiki", ".git", ".llmwiki.txn.d", ".llmwiki.toggle.d", ".qmd")
_EXCLUDED_FILES = (
    "SCHEMA.md", ".llmwiki", ".llmwiki.lock", ".llmwiki.txn",
    "log.md", "index.md", ".cc-turn-ledger.jsonl",
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
# verb: session-plan  (read-only Path B session-set resolver; T6 / F1-b / F2-B)
# --------------------------------------------------------------------------- #
# The taskflow state dir is a WORKSPACE concept (keyed off the process CWD),
# NOT under the wiki `<root>`: it lives at `<cwd>/_projects/_state/*.json`, the
# exact location wiki_root_resolver._latest_state_project reads (each file is
# `<sid>.json` with a `{"project": ...}` body). We locate it the same way.
_STATE_SUBPATH = ("_projects", "_state")

# The CC session-log root (U3 ground truth). `~` is expanded at runtime; no
# absolute path is baked in (repo secret rule). Mirrors the vendored views'
# `~/.claude/projects/**/*.jsonl` glob and revert_cc_log_extract's default.
_CC_PROJECTS_DIR = "~/.claude/projects"

# The env var that exposes the RUNNING session's id to a runtime script.
# Verified against real source: skills/revert/scripts/revert_cc_log_extract.py
# (`os.environ.get("CLAUDE_SESSION_ID")`) and skills/create-skill/advanced-mode.md
# (`${CLAUDE_SESSION_ID}` = "Current session ID"). NOT guessed.
_CURRENT_SID_ENV = "CLAUDE_SESSION_ID"


def _state_dir(cwd: "Path | None" = None) -> Path:
    """The taskflow state dir `<cwd>/_projects/_state` (process CWD, not wiki root).

    Mirrors wiki_root_resolver._latest_state_project's location rule so pj
    resolution agrees with the rest of the toolkit on where the workspace is.
    """
    base = cwd if cwd is not None else Path.cwd()
    return base.joinpath(*_STATE_SUBPATH)


def _sids_for_pj(project: str, cwd: "Path | None" = None) -> list[str]:
    """The sids whose state file's `project` field == <project>.

    Each `_projects/_state/<sid>.json` is `{"project": ..., ...}`; the filename
    STEM is the sid (verified: state file is written as `<session_id>.json` by
    taskflow session_init.py). A malformed / unreadable state file is skipped
    (never fatal — a foreign or half-written file must not sink the resolve).
    """
    state_dir = _state_dir(cwd)
    sids: list[str] = []
    try:
        candidates = sorted(p for p in state_dir.glob("*.json") if p.is_file())
    except OSError:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("project") == project:
            sids.append(path.stem)
    return sids


def _cc_project_dir_from_running_session() -> "Path | None":
    """U3 PRIMARY: the CC project dir of the RUNNING session, as ground truth.

    Find `~/.claude/projects/*/<current-sid>.jsonl` (current-sid =
    $CLAUDE_SESSION_ID) and return its PARENT dir — the CC-internal-encoding of
    the dir name is irrelevant because we locate the dir by the session file that
    lives in it, never by reconstructing the slug. Returns None if the env var is
    unset or no such jsonl exists (the caller then tries the fallback).
    """
    sid = os.environ.get(_CURRENT_SID_ENV)
    if not sid:
        return None
    root = Path(os.path.expanduser(_CC_PROJECTS_DIR))
    if not root.is_dir():
        return None
    matches = sorted(root.rglob(f"{sid}.jsonl"))
    if not matches:
        return None
    return matches[0].parent


def _cc_project_dir_from_cwd(cwd: "Path | None" = None) -> "Path | None":
    """U3 SECONDARY fallback: reverse-generate the CC dir slug from the CWD.

    CC encodes a project dir as the absolute cwd with path separators AND the
    drive colon replaced by `-`. This is a best-effort fallback used ONLY when
    the running session's own log cannot be located (primary path). Returns the
    dir Path if it exists under `~/.claude/projects`, else None.
    """
    base = (cwd if cwd is not None else Path.cwd()).resolve()
    # Replace every path separator and the drive colon with `-` (e.g.
    # `C:\a\b` / `/home/a/b` -> `C--a-b` / `-home-a-b`).
    slug = re.sub(r"[\\/:]", "-", str(base))
    root = Path(os.path.expanduser(_CC_PROJECTS_DIR))
    candidate = root / slug
    return candidate if candidate.is_dir() else None


def _sids_in_project_dir(project_dir: Path) -> list[str]:
    """The session sids in a CC project dir = the stems of its session `*.jsonl`.

    A session log's filename IS `<sid>.jsonl` (100%, per the task doc). Agent
    child logs are named `agent-*.jsonl` (they carry the parent sid INSIDE the
    JSON, not in the filename) — excluded here so only real sessions are planned;
    the projector folds each session's agent children in by the shared sid.
    """
    sids: list[str] = []
    for path in sorted(project_dir.glob("*.jsonl")):
        stem = path.stem
        if stem.startswith("agent-") or stem == "journal":
            continue
        sids.append(stem)
    return sids


def _order_sids_by_started_ts(sids: list[str]) -> list[str]:
    """Order sids by each session's earliest record ts ASCENDING (F2-B).

    The sort key is `cc_session.started` (= min(ts) over the session's records)
    from the vendored DuckDB views — the authoritative in-log event clock, which
    (unlike file mtime) is stable across copy/resume/fork/checkout and is what
    the whole view stack + revert already order by. A sid with no `started` row
    (e.g. a log DuckDB could not parse) sorts LAST but is still returned (never
    dropped) so the plan stays complete; ties fall back to the sid string for a
    deterministic order.
    """
    if not sids:
        return []
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - declared in the PEP723 header
        raise DriverError(f"duckdb unavailable for session-plan ordering: {exc}") from exc
    try:
        con = duckdb.connect()
        # Reuse the projector's vendored views (single source of truth for the
        # cc-log DuckDB schema — cc_log_project._VIEWS_SQL points at cc_views.sql).
        con.execute(cc_log_project._VIEWS_SQL.read_text(encoding="utf-8"))
        placeholders = ",".join("?" for _ in sids)
        rows = con.execute(
            f"SELECT session_id, started FROM cc_session "
            f"WHERE session_id IN ({placeholders})",
            sids,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - surface as a driver error
        raise DriverError(f"session-plan ts ordering failed: {exc}") from exc
    started: dict[str, object] = {sid: ts for sid, ts in rows}
    # None-started sids sort last (True > False); then by ts, then by sid.
    return sorted(
        sids,
        key=lambda s: (started.get(s) is None, started.get(s), s),
    )


def session_plan(wiki_root: str, *, pj: "str | None" = None) -> dict:
    """Resolve the Path B session-id SET, ordered by session-start ts ascending.

    Read-only (no lock / checkpoint / write / transaction). Ownership is NOT
    partitioned here (F1-b/F2-B): each begin decides ownership dynamically via
    the ledger diff. `wiki_root` is accepted for surface parity with the other
    verbs and to confirm it is a wiki root, but the state dir and CC log dir are
    resolved off the process CWD / $HOME (not under the wiki root).

    Resolution:
      - pj given  -> `_projects/_state/*.json` filtered by `project == pj`;
                     scope "pj".
      - pj omitted-> the running session's CC project dir (U3 primary), else the
                     cwd-reverse-generated dir (U3 secondary); scope "cwd".
    Zero matches -> DriverError (fail-closed, like enumerate).

    Output: {sids: [sid,...], scope: "pj"|"cwd", pattern: <state-glob|dir>}.
    """
    root = Path(wiki_root)
    mk = marker.detect(root)
    if mk is None:
        raise DriverError(f"no .llmwiki marker at {wiki_root} (not a wiki root)")

    if pj:
        sids = _sids_for_pj(pj)
        scope = "pj"
        pattern = str(_state_dir()) + os.sep + "*.json"
        if not sids:
            raise DriverError(
                f"session-plan matched zero sessions for --pj {pj!r} "
                f"(no _projects/_state/*.json with project=={pj!r})")
    else:
        project_dir = _cc_project_dir_from_running_session()
        source = "running-session"
        if project_dir is None:
            project_dir = _cc_project_dir_from_cwd()
            source = "cwd-fallback"
        if project_dir is None:
            raise DriverError(
                "session-plan could not resolve the CC project dir: "
                f"${_CURRENT_SID_ENV} did not locate a session jsonl under "
                f"{_CC_PROJECTS_DIR}, and the cwd-reverse-generated slug dir "
                "does not exist (fail-closed)")
        sids = _sids_in_project_dir(project_dir)
        scope = "cwd"
        pattern = str(project_dir)
        if not sids:
            raise DriverError(
                f"session-plan matched zero sessions in CC project dir "
                f"{project_dir} (resolved via {source})")

    ordered = _order_sids_by_started_ts(sids)
    return {"sids": ordered, "scope": scope, "pattern": pattern}


# --------------------------------------------------------------------------- #
# verb: project-batch  (read-only Path B scan-collapse; R1 / F-H1)
# --------------------------------------------------------------------------- #
# Path B must not re-scan the whole ~/.claude/projects corpus once per begin
# (N sids -> N full scans). This read-only verb runs the EXPENSIVE projection
# (one corpus scan for ALL sids via cc_log_project.extract_turns_batch), writes
# each sid's extracted turns to a per-sid JSON file under a fresh temp dir, and
# returns the {sid: path} map. The Path B loop then passes each begin
# `--turns=<path>` so begin runs only the cheap per-sid half (dedup + ledger diff
# + markdown), keeping the ledger read-after-write (F3) sequential. Opens NO
# transaction (outside the R-f verb budget), exactly like enumerate/session-plan.
_BATCH_TURNS_PREFIX = "llmwiki-turns-"


def project_batch(wiki_root: str, sids: "list[str]") -> dict:
    """Extract turns for ALL sids in one scan; write per-sid JSON; return the map.

    Read-only (no lock / checkpoint / write to the wiki / transaction). The turn
    files are written OUTSIDE the wiki root (a temp dir), so they are never
    journaled and never enumerated; the Path B loop owns their cleanup.

    Output: {out_dir: <temp dir>, turns: {sid: <per-sid turn-json path>},
             scanned: <sid count>}.
    """
    root = Path(wiki_root)
    mk = marker.detect(root)
    if mk is None:
        raise DriverError(f"no .llmwiki marker at {wiki_root} (not a wiki root)")
    if not sids:
        raise DriverError("project-batch requires at least one sid")

    # One corpus scan for all sids (the expensive half; R1). ledger is the hash
    # single source of truth — the extracted turns carry their F5 hash so begin's
    # project_from_turns agrees on the dedup/ledger key without re-hashing.
    extracted = cc_log_project.extract_turns_batch(sids, ledger=ledger)

    out_dir = Path(tempfile.mkdtemp(prefix=_BATCH_TURNS_PREFIX))
    turns_map: dict[str, str] = {}
    for sid in sids:
        turn_list = extracted.get(sid, [])
        out_path = out_dir / f"{sid}.json"
        # Record the owning sid alongside the turns so begin's --turns path can
        # guard the sid<->file pairing (fail-closed on mismatch).
        out_path.write_text(
            json.dumps({"sid": sid, "turns": turn_list}, ensure_ascii=False),
            encoding="utf-8",
        )
        turns_map[sid] = str(out_path)
    return {"out_dir": str(out_dir), "turns": turns_map, "scanned": len(sids)}


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
              "<begin|plan-fanout|finish|abort|enumerate|session-plan|"
              "project-batch> ...",
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
                turns=opts.get("turns"),
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
        elif verb == "session-plan":
            if len(pos) < 1:
                raise DriverError("session-plan requires <root> [--pj <name>]")
            # Accept both `--pj=name` and the spec's space form `--pj name`.
            # In the space form the parser leaves `--pj` as an empty-value opt and
            # the name lands as the next positional (pos[1]); use it as the pj name.
            pj_val = opts.get("pj")
            if "pj" in opts and not pj_val and len(pos) >= 2:
                pj_val = pos[1]
            result = session_plan(pos[0], pj=pj_val or None)
        elif verb == "project-batch":
            if len(pos) < 2:
                raise DriverError("project-batch requires <root> <sid> [<sid>...]")
            result = project_batch(pos[0], pos[1:])
        else:
            print(f"unknown verb: {verb!r} "
                  "(begin|plan-fanout|finish|abort|enumerate|session-plan|"
                  "project-batch)",
                  file=sys.stderr)
            return 2
    except config_resolver.ConfigInconsistency as e:
        print(f"config-inconsistency: {e}", file=sys.stderr)
        return 2
    except cc_log_project.ProjectionError as e:
        print(f"extract: {e}", file=sys.stderr)
        return 3
    except transaction.StaleJournal as e:
        print(f"stale-journal: {e}", file=sys.stderr)
        return 2
    except (DriverError, transaction.LockHeld) as e:
        print(str(e), file=sys.stderr)
        return 2

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
