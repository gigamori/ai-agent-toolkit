# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""apply_finish — the compound `apply-finish` verb (spec E3 / F1 / F2).

E3 collapses the per-cluster `ingest-apply` calls + the closing `finish` into ONE
driver process (per-sid driver processes 3+k -> 3, k-independent), and closes the
ordinal<->page-set correspondence in code (F2) instead of leaving the manifest
order to the LLM.

Shared module placement (spec §リスク "共有関数化の import 循環 ... 括り出し先は
`llmwiki/ingest/` 配下に限定"): it lives beside `ingest_driver` under
`llmwiki/ingest/` and REUSES the already-verified transaction primitives rather
than duplicating them —
  - the apply body reuses `write_tool.WriteSession` (the D19/D20 allowlist gate —
    the ONE UN-DROPPABLE INVARIANT; apply-finish runs in the driver/CLI, NOT in a
    Stage LLM worker, so this does not weaken the gate);
  - the closing join/index/log/ledger/commit reuses `ingest_driver.finish`
    verbatim (single central commit, D23), and the cluster-drop guard (C2) is
    satisfied by writing the same per-ordinal receipts `ingest-apply` writes.
Import-cycle avoidance: this module imports `ingest_driver` at top; `ingest_driver`
imports THIS module only lazily inside its `apply-finish` verb branch (never at
module top), so there is no load-time cycle. `cli.py` likewise imports it
branch-locally.

Contract (spec E3 / F1):
  synopsis: `apply-finish <root> <origin> --manifest <path>... [--title=<t>]`
  - `--manifest` order == cluster ordinal; each `<path>` is a JSON file holding a
    manifest `[{rel_path, content}, ...]` (same shape `ingest-apply` reads on STDIN).
  - F2 pre-apply checks (BEFORE any write): (i) `len(--manifest)` ==
    `len(planned_clusters)`; (ii) each manifest[ordinal]'s rel_path set ⊆
    `planned_clusters[ordinal]`. A mismatch -> internal finish FAIL (rollback).
  - all manifests apply OK -> internal finish SUCCESS (central join / index
    regenerate / log append / ledger append / commit).
  - any manifest REJECTED (write_tool gate) -> internal finish FAIL (rollback);
    partial-write semantics: manifest[i] REJECTED rolls back manifest[0..i-1]'s
    journaled page writes too (transaction.rollback replays the shared journal).
  - stdout success: `{"clusters":[{"ordinal":N,"written":[rel_path,...]},...],
    "committed":true}`.
  - stdout failure: `{"rolled_back":true}`, stderr `REJECTED <gate> <reason>`,
    non-zero exit.
  - `--title` threads into `finish`'s log title (kept identical to the granular
    `finish --title` path).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from llmwiki.ingest import ingest_driver
from llmwiki.write import transaction
from llmwiki.write.write_tool import WriteSession, WriteRejected


# Usage exit code (sysexits.h EX_USAGE), byte-identical to cli.py's EX_USAGE.
EX_USAGE = 64


class ApplyFinishRejected(Exception):
    """A rejected apply-finish that has already rolled the transaction back.

    Carries the `gate`/`reason` for the stderr `REJECTED <gate> <reason>` line
    (F1). Raised only AFTER `ingest_driver.finish(..., "fail")` has rolled the
    transaction back + released the lock + deleted the sidecar, so the caller
    only has to render the `{"rolled_back": true}` stdout + the non-zero exit.
    """

    def __init__(self, gate: str, reason: str):
        super().__init__(f"{gate} {reason}")
        self.gate = gate
        self.reason = reason


def _ws_origin(fe_origin: str) -> str:
    """Map the front-end origin to the WriteSession tier (trust by location, D20).

    Byte-identical to `cli.py:_ingest_apply` (`ws_origin`): projection origins
    (fe_b_prime cc-log / fe_pi_log pi-log) carry UNTRUSTED transcript content and
    map to "derived" (wiki/derived/ only); fe_b (explicit 3rd-party source file)
    maps to "source".
    """
    return "derived" if fe_origin in ("fe_b_prime", "fe_pi_log") else "source"


def _load_manifest(path: str) -> list:
    """Read + validate a manifest JSON file: a list of {rel_path, content} dicts.

    Raises DriverUsageError (a malformed-input protocol violation, not a
    normal-data sentinel) on an unreadable / non-JSON / wrong-shaped file so
    the caller rolls the transaction back and reports (never a raw traceback).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ingest_driver.DriverUsageError(
            f"manifest unreadable or not JSON: {path} ({exc})") from exc
    if not isinstance(data, list):
        raise ingest_driver.DriverUsageError(
            f"manifest must be a list of {{rel_path, content}}: {path}")
    for entry in data:
        if (not isinstance(entry, dict) or "rel_path" not in entry
                or "content" not in entry):
            raise ingest_driver.DriverUsageError(
                f"manifest entry must be {{rel_path, content}}: {path}")
    return data


def apply_finish(wiki_root: str, fe_origin: str, manifest_paths: "list[str]",
                 *, title: "str | None" = None) -> dict:
    """Apply every manifest in ordinal order, then finish(success) (spec E3).

    Returns the success contract dict on success; raises `ApplyFinishRejected`
    (transaction already rolled back) on a write rejection or an F2 mismatch;
    raises `ingest_driver.DriverError` on a pre-flight setup error (no sidecar /
    no open transaction / foreign lock) WITHOUT touching the transaction.
    """
    root = Path(wiki_root)

    # Pre-flight 1: the transaction must exist and be ours. `begin` wrote the
    # sidecar; `plan-fanout` added planned_clusters.
    state = ingest_driver._read_sidecar(root)
    if state is None:
        raise ingest_driver.DriverError(
            "no .llmwiki.txn sidecar; run `begin` first")
    # F3: refuse to write unjournaled — the journal dir must already exist (the
    # driver `begin` created it). Same fail-closed guard as `ingest-apply`.
    if not (root / transaction.JOURNAL_DIR).is_dir():
        raise ingest_driver.DriverError(
            "REFUSED no-journal: apply-finish requires an open transaction "
            "(run `begin` first)")
    # DEC-R1=D ownership check BEFORE any write (mirrors `finish`): if the on-disk
    # lock belongs to a DIFFERENT ingest, refuse WITHOUT rolling back or writing
    # into its transaction (a foreign txn must be recovered via `abort`). Checked
    # here — not only at the closing finish — so a foreign lock never lets us
    # write page files into someone else's transaction first.
    expected_token = state.get("lock_token")
    actual_token = transaction.read_lock_token(root)
    if (expected_token is not None and actual_token is not None
            and expected_token != actual_token):
        raise ingest_driver.DriverError(
            "lock ownership mismatch: .llmwiki.lock is held by a different "
            "ingest; refusing apply-finish (recover the owning transaction via "
            "`abort`)")

    planned = state.get("planned_clusters")
    if planned is None:
        raise ingest_driver.DriverError(
            "no planned_clusters in sidecar; run `plan-fanout` before apply-finish")

    # F2 (i): manifest count must equal the planned cluster count. A mismatch is
    # a fail (rollback) BEFORE applying — the order-as-ordinal contract cannot
    # hold if the counts differ.
    if len(manifest_paths) != len(planned):
        ingest_driver.finish(str(root), "fail")
        raise ApplyFinishRejected(
            "manifest_count",
            f"--manifest count {len(manifest_paths)} != planned clusters "
            f"{len(planned)}")

    # Load + F2 (ii): each manifest[ordinal]'s rel_path set ⊆ planned[ordinal].
    # (Compare on POSIX-normalized paths — write_tool normalizes `\` to `/` and
    # the planned set is authored as POSIX rel_paths.) Any load / subset failure
    # is a fail (rollback) BEFORE applying.
    manifests: list[list] = []
    for ordinal, mpath in enumerate(manifest_paths):
        try:
            entries = _load_manifest(mpath)
        except ingest_driver.DriverError as exc:
            ingest_driver.finish(str(root), "fail")
            raise ApplyFinishRejected("manifest", f"manifest[{ordinal}] {exc}")
        manifest_rels = {e["rel_path"].replace("\\", "/") for e in entries}
        planned_rels = {p.replace("\\", "/") for p in planned[ordinal]}
        extra = manifest_rels - planned_rels
        if extra:
            ingest_driver.finish(str(root), "fail")
            raise ApplyFinishRejected(
                "cluster_pageset",
                f"manifest[{ordinal}] rel_path(s) not in planned cluster "
                f"{ordinal}: {sorted(extra)}")
        manifests.append(entries)

    # Apply every manifest in order via the D19/D20 allowlist gate. Budget comes
    # from the driver-owned sidecar (never LLM-threaded), exactly like
    # `ingest-apply`. Each cluster records a C2 dispatch receipt so the closing
    # finish's cluster-drop guard (expected_pages omitted) is satisfied.
    ws_origin = _ws_origin(fe_origin)
    max_count = int(state["max_count"])
    max_bytes = int(state["max_bytes"])
    clusters_written: list[dict] = []
    try:
        for ordinal, entries in enumerate(manifests):
            sess = WriteSession(root, max_count=max_count, max_bytes=max_bytes,
                                origin=ws_origin)
            for entry in entries:
                sess.add(entry["rel_path"], entry["content"])
            written = sess.commit()   # journals each target BEFORE writing (F1)
            clusters_written.append({"ordinal": ordinal, "written": written})
            # C2 receipt (same keys `ingest-apply` writes): read-modify-write the
            # sidecar so finish (expected_pages omitted) proves every planned
            # ordinal was dispatched.
            state.setdefault("applied_clusters", []).append(ordinal)
            state.setdefault("applied_written", []).extend(written)
            ingest_driver._write_sidecar(root, state)
    except WriteRejected as exc:
        # partial-write semantics (F1): manifest[0..i-1] writes were journaled by
        # their WriteSession.commit, so finish(fail) -> transaction.rollback
        # replays the shared journal and removes them (and begin's raw) too.
        ingest_driver.finish(str(root), "fail")
        raise ApplyFinishRejected(exc.gate, exc.reason) from exc

    # All manifests applied -> internal finish SUCCESS: central join (cluster
    # receipts now cover every planned ordinal) + index regenerate + log append +
    # ledger append + commit, all inside the single transaction (D23). `--title`
    # threads through to the log title exactly as the granular `finish --title`.
    ingest_driver.finish(str(root), "success", title=title)
    return {"clusters": clusters_written, "committed": True}


def run_apply_finish_cli(argv: "list[str]") -> int:
    """CLI adapter shared by `ingest_driver.main` and `cli.py` (verb budget).

    Parses `<root> <origin> --manifest <path>... [--title=<t>]` (both `--flag val`
    and `--flag=val` forms; `--manifest` repeats, order preserved = ordinal),
    calls `apply_finish`, and renders the E3/F1 stdout/stderr contract. Returns
    the process exit code. stdio is already reconfigured to UTF-8 by each caller's
    `main` before dispatch.
    """
    positional: list[str] = []
    manifests: list[str] = []
    title: "str | None" = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--manifest":
            if i + 1 < len(argv):
                manifests.append(argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--manifest="):
            manifests.append(a[len("--manifest="):])
            i += 1
            continue
        if a == "--title":
            title = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a.startswith("--title="):
            title = a[len("--title="):]
            i += 1
            continue
        positional.append(a)
        i += 1

    if len(positional) < 2 or not manifests:
        print("usage: apply-finish <root> <origin> --manifest <path>... "
              "[--title=<t>]", file=sys.stderr)
        return EX_USAGE

    root, fe_origin = positional[0], positional[1]
    try:
        result = apply_finish(root, fe_origin, manifests, title=title)
    except ApplyFinishRejected as e:
        # Failure contract (F1): stderr REJECTED line + stdout rolled_back + rc!=0.
        print(f"REJECTED {e.gate} {e.reason}", file=sys.stderr)
        print(json.dumps({"rolled_back": True}, ensure_ascii=False))
        return 1
    except ingest_driver.DriverUsageError as e:
        # Usage / protocol error (2026-07-16 F2): the transaction was NOT
        # touched, so there is no rolled_back to report.
        print(str(e), file=sys.stderr)
        return EX_USAGE
    except ingest_driver.DriverOpError as e:
        # Operational (runtime/environment/verification) error (2026-07-16 F2):
        # the transaction was NOT touched, so there is no rolled_back to report.
        print(str(e), file=sys.stderr)
        return 3
    except ingest_driver.DriverError as e:
        # Pre-flight setup error (no sidecar / no journal / foreign lock): the
        # transaction was NOT touched, so there is no rolled_back to report.
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0
