#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests for generate_kanban.py's multi-workspace port resolution.

Regression guard for the kanban port-conflict fix
(_projects/harness-taskflow/tasks/0_todo/2026-07-16_kanban-multi-workspace-port-conflict.md):
each workspace must derive a stable port from its `_projects` roots via
`hashlib` (not the PYTHONHASHSEED-salted builtin `hash()`), and
`resolve_workspace_port` must never mistake one workspace's server for
another's, even when their derived ports collide.

stdlib only (network calls are monkeypatched out). Run with:
  uv run python plugins/taskflow/tests/test_kanban_port.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Import the module under test from scripts/ (sibling of tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_kanban as gk  # noqa: E402

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


def test_workspace_key_order_and_case_independent() -> None:
    a = [Path("C:/Work/Alpha"), Path("C:/Work/Beta")]
    b = [Path("c:/work/beta"), Path("c:/work/alpha")]
    check(gk._workspace_key(a) == gk._workspace_key(b),
          "_workspace_key: order- and case-independent for the same roots")

    c = [Path("C:/Work/Gamma")]
    check(gk._workspace_key(a) != gk._workspace_key(c),
          "_workspace_key: differs for different roots")


def test_derive_port_deterministic_and_in_span() -> None:
    key = gk._workspace_key([Path("C:/Work/Alpha")])
    p1 = gk._derive_port(key)
    p2 = gk._derive_port(key)
    check(p1 == p2, "_derive_port: repeated calls with the same key agree (in-process)")
    check(gk.PORT_BASE <= p1 < gk.PORT_BASE + gk.PORT_SPAN,
          f"_derive_port: result {p1} within [PORT_BASE, PORT_BASE+PORT_SPAN)")

    # hashlib.sha1, not the builtin hash() (which is salted per-process via
    # PYTHONHASHSEED) — assert against a fixed, hand-computed value so a
    # future switch back to hash() would break this test.
    import hashlib
    expected = gk.PORT_BASE + (int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % gk.PORT_SPAN)
    check(p1 == expected, "_derive_port: matches hashlib.sha1-based computation")


def test_resolve_all_free_binds_at_derived_start() -> None:
    roots = [Path("C:/Work/Alpha")]
    start = gk._derive_port(gk._workspace_key(roots))
    gk.port_status = lambda port, timeout=2.0: ("free", None)
    port, state, info = gk.resolve_workspace_port(roots)
    check((port, state) == (start, "free"),
          "resolve_workspace_port: all free -> binds at the derived start port (single probe)")


def test_resolve_finds_own_server_at_start() -> None:
    roots = [Path("C:/Work/Alpha")]
    key = gk._workspace_key(roots)
    start = gk._derive_port(key)
    gk.port_status = (
        lambda port, timeout=2.0:
        ("ours", {"app": "taskflow-kanban", "pid": 111, "key": key}) if port == start
        else ("free", None)
    )
    port, state, info = gk.resolve_workspace_port(roots)
    check((port, state, info.get("pid")) == (start, "ours", 111),
          "resolve_workspace_port: finds our own server already at the derived start")


def test_resolve_skips_hash_collision_from_other_workspace() -> None:
    """A different workspace's server occupying our derived port must NOT be
    mistaken for ours — the whole point of the workspace-key check."""
    roots = [Path("C:/Work/Alpha")]
    key = gk._workspace_key(roots)
    other_key = gk._workspace_key([Path("C:/Work/Beta")])
    start = gk._derive_port(key)

    def probe(port, timeout=2.0):
        if port == start:
            return ("ours", {"app": "taskflow-kanban", "pid": 222, "key": other_key})
        return ("free", None)

    gk.port_status = probe
    port, state, info = gk.resolve_workspace_port(roots)
    check(state == "free" and port != start,
          f"resolve_workspace_port: hash collision at {start} (other workspace) -> "
          f"falls through to a different free port, got ({port}, {state})")


def test_resolve_finds_own_server_displaced_by_collision() -> None:
    """Our own server, previously started under a collision and bound to a
    fallback slot, must still be found on a later invocation."""
    roots = [Path("C:/Work/Alpha")]
    key = gk._workspace_key(roots)
    other_key = gk._workspace_key([Path("C:/Work/Beta")])
    start = gk._derive_port(key)
    displaced = gk.PORT_BASE + ((start - gk.PORT_BASE + 5) % gk.PORT_SPAN)

    def probe(port, timeout=2.0):
        if port == start:
            return ("ours", {"app": "taskflow-kanban", "pid": 444, "key": other_key})
        if port == displaced:
            return ("ours", {"app": "taskflow-kanban", "pid": 555, "key": key})
        return ("foreign", None)

    gk.port_status = probe
    port, state, info = gk.resolve_workspace_port(roots)
    check((port, state, info.get("pid")) == (displaced, "ours", 555),
          "resolve_workspace_port: finds our own server displaced elsewhere in the span")


def test_port_state_path_isolates_different_keys_sharing_root() -> None:
    """Two workspaces that share roots[0] (distinct secondary roots, hence
    distinct keys) must not collide on the same persisted-port file."""
    shared_root = Path("C:/Work/Shared")
    key_a = gk._workspace_key([shared_root, Path("C:/Work/SecondaryA")])
    key_b = gk._workspace_key([shared_root, Path("C:/Work/SecondaryB")])
    path_a = gk._port_state_path([shared_root], key_a)
    path_b = gk._port_state_path([shared_root], key_b)
    check(path_a != path_b, "_port_state_path: distinct keys sharing roots[0] get distinct files")
    check(path_a.parent == shared_root / "_state", "_port_state_path: lives under roots[0]/_state")


def test_persist_read_clear_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kanban-port-test-"))
    try:
        roots = [tmp / "_projects"]
        roots[0].mkdir()
        key = gk._workspace_key(roots)

        check(gk._read_persisted_port(roots, key) is None,
              "_read_persisted_port: no record yet -> None")

        gk._persist_port(roots, key, 17400, 555)
        check(gk._read_persisted_port(roots, key) == 17400,
              "_read_persisted_port: round-trips a persisted port")

        other_key = gk._workspace_key([tmp / "_projects-other"])
        check(gk._read_persisted_port(roots, other_key) is None,
              "_read_persisted_port: a record for a different key is never trusted")

        gk._clear_persisted_port(roots, key, 99999)  # wrong port -> must NOT clear
        check(gk._read_persisted_port(roots, key) == 17400,
              "_clear_persisted_port: no-ops when the port doesn't match the record")

        gk._clear_persisted_port(roots, key, 17400)
        check(gk._read_persisted_port(roots, key) is None,
              "_clear_persisted_port: removes the record when key+port match")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_finds_displaced_server_after_collision_occupant_leaves() -> None:
    """F1 regression: a server bound to a fallback slot (hash collision at
    the derived start) must still be discoverable once the collision's
    occupant is gone — re-deriving `start` and finding it merely "free" must
    NOT be treated as "our server isn't running anywhere"."""
    tmp = Path(tempfile.mkdtemp(prefix="kanban-port-test-"))
    try:
        roots = [tmp / "_projects"]
        roots[0].mkdir()
        key = gk._workspace_key(roots)
        other_key = gk._workspace_key([tmp / "_projects-other"])
        start = gk._derive_port(key)

        # 1. Collision at start (a different workspace is "ours" there) ->
        #    bind falls through to the first free slot.
        gk.port_status = (
            lambda port, timeout=2.0:
            ("ours", {"app": "taskflow-kanban", "pid": 1, "key": other_key}) if port == start
            else ("free", None)
        )
        bound_port, state, info = gk.resolve_workspace_port(roots)
        check(state == "free" and bound_port != start,
              f"F1 setup: collision at start -> bound elsewhere, got ({bound_port}, {state})")

        # 2. Simulate main() persisting the port after a successful bind.
        gk._persist_port(roots, key, bound_port, 777)

        # 3. The collision's occupant is gone; `start` now probes free too.
        #    Only `bound_port` answers, as ours.
        def probe_after(port, timeout=2.0):
            if port == bound_port:
                return ("ours", {"app": "taskflow-kanban", "pid": 777, "key": key})
            return ("free", None)
        gk.port_status = probe_after

        port2, state2, info2 = gk.resolve_workspace_port(roots)
        check((port2, state2) == (bound_port, "ours"),
              f"F1 fix: displaced server found via persisted record after collision clears, "
              f"got ({port2}, {state2}) expected ({bound_port}, 'ours')")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_stale_persisted_record_self_heals() -> None:
    """A persisted port whose server is gone (crash / unclean kill, no
    `--stop` to clear the record) must not block finding a fresh port."""
    tmp = Path(tempfile.mkdtemp(prefix="kanban-port-test-"))
    try:
        roots = [tmp / "_projects"]
        roots[0].mkdir()
        key = gk._workspace_key(roots)
        start = gk._derive_port(key)

        gk._persist_port(roots, key, start + 1 if start + 1 < gk.PORT_BASE + gk.PORT_SPAN else start - 1, 999)
        gk.port_status = lambda port, timeout=2.0: ("free", None)  # nothing is actually running
        port, state, info = gk.resolve_workspace_port(roots)
        check(state == "free", f"stale persisted record falls through to a free port, got {state}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_avoids_double_probe_when_persisted_equals_start() -> None:
    """Common repeat-invocation case (no collision ever happened): the
    persisted port equals the derived start, so it must be probed only
    once, not twice."""
    tmp = Path(tempfile.mkdtemp(prefix="kanban-port-test-"))
    try:
        roots = [tmp / "_projects"]
        roots[0].mkdir()
        key = gk._workspace_key(roots)
        start = gk._derive_port(key)
        gk._persist_port(roots, key, start, 42)

        calls = []

        def counting_probe(port, timeout=2.0):
            calls.append(port)
            return ("ours", {"app": "taskflow-kanban", "pid": 42, "key": key})

        gk.port_status = counting_probe
        port, state, info = gk.resolve_workspace_port(roots)
        check((port, state) == (start, "ours"), "resolve finds ours at persisted==start")
        check(len(calls) == 1, f"probes exactly once when persisted == start, got {len(calls)} calls: {calls}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_full_span_occupied() -> None:
    roots = [Path("C:/Work/Alpha")]
    start = gk._derive_port(gk._workspace_key(roots))
    gk.port_status = lambda port, timeout=2.0: ("foreign", None)
    port, state, info = gk.resolve_workspace_port(roots)
    check(state == "full", f"resolve_workspace_port: whole span occupied by foreign services -> full, got {state}")


def test_resolve_explicit_port_foreign() -> None:
    roots = [Path("C:/Work/Alpha")]
    gk.port_status = lambda port, timeout=2.0: ("foreign", None) if port == 19999 else ("free", None)
    port, state, info = gk.resolve_workspace_port(roots, explicit_port=19999)
    check((port, state) == (19999, "foreign"), "resolve_workspace_port: --port on an occupied foreign port reports foreign")


def test_resolve_explicit_port_ours() -> None:
    roots = [Path("C:/Work/Alpha")]
    key = gk._workspace_key(roots)
    gk.port_status = (
        lambda port, timeout=2.0:
        ("ours", {"app": "taskflow-kanban", "pid": 333, "key": key}) if port == 19999
        else ("free", None)
    )
    port, state, info = gk.resolve_workspace_port(roots, explicit_port=19999)
    check((port, state) == (19999, "ours"), "resolve_workspace_port: --port matching our own key reports ours")


def main() -> int:
    real_port_status = gk.port_status
    try:
        test_workspace_key_order_and_case_independent()
        test_derive_port_deterministic_and_in_span()
        test_resolve_all_free_binds_at_derived_start()
        test_resolve_finds_own_server_at_start()
        test_resolve_skips_hash_collision_from_other_workspace()
        test_resolve_finds_own_server_displaced_by_collision()
        test_port_state_path_isolates_different_keys_sharing_root()
        test_persist_read_clear_roundtrip()
        test_resolve_finds_displaced_server_after_collision_occupant_leaves()
        test_resolve_stale_persisted_record_self_heals()
        test_resolve_avoids_double_probe_when_persisted_equals_start()
        test_resolve_full_span_occupied()
        test_resolve_explicit_port_foreign()
        test_resolve_explicit_port_ours()
    finally:
        gk.port_status = real_port_status

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
