#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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


def setup_zeroed_state_dir(state_dir: Path, session_ids: list[str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    for sid in session_ids:
        state_file = state_dir / f"{sid}.json"
        state = {
            "session_id": sid,
            "project": "test-project",
            "origin": "pi",
            "created": "2026-08-25T00:00:00+09:00",
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)


def test_ac1_zeroed_two_sessions_separated() -> None:
    print("--- Zeroed 2 session separation ---")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        state_dir = tmp / "_state"
        session_ids = [
            "00000000-0000-7000-8000-0058a66e62c5",
            "00000000-0000-7000-8000-0ba8ecb268d0",
        ]
        setup_zeroed_state_dir(state_dir, session_ids)

        prefix_index, tail_index = gk.build_uuid_index(state_dir)

        check(len(tail_index) == 2, f"zeroed sessions with different tails must have 2 unique tail_index entries: {list(tail_index.keys())}")
        check("0058a66e62c5" in tail_index, "first session's tail-12 is indexed")
        check("0ba8ecb268d0" in tail_index, "second session's tail-12 is indexed")

        check("00000000" in prefix_index, "first-8 prefix 00000000 exists for zeroed sessions")
        check(len(prefix_index["00000000"]) == 2, "prefix_index has 2 entries for 00000000 because zeroed sessions share same first-8")

        entry1 = gk.resolve_tag("0058a66e62c5", prefix_index, tail_index)
        entry2 = gk.resolve_tag("0ba8ecb268d0", prefix_index, tail_index)

        check(entry1 is not None, "first tail-12 tag resolves to a StateEntry")
        check(entry2 is not None, "second tail-12 tag resolves to a StateEntry")
        if entry1 and entry2:
            check(entry1.uuid != entry2.uuid, "two zeroed sessions with different tails resolve to different UUIDs")
            check(entry1.uuid == session_ids[0], f"entry1 UUID is {session_ids[0]}")
            check(entry2.uuid == session_ids[1], f"entry2 UUID is {session_ids[1]}")


def test_ac3_ambiguous_legacy_tag_no_resolution() -> None:
    print("--- Ambiguous legacy tag non-resolution ---")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        state_dir = tmp / "_state"
        session_ids = [
            "12345678-0000-0000-0000-000000000001",
            "12345678-0000-0000-0000-000000000002",
            "87654321-0000-0000-0000-000000000003",
        ]
        setup_zeroed_state_dir(state_dir, session_ids)

        prefix_index, tail_index = gk.build_uuid_index(state_dir)

        ambiguous_result = gk.resolve_tag("12345678", prefix_index, tail_index)
        check(ambiguous_result is None, "legacy 8-char tag with 2 candidates returns None to avoid ambiguous attribution")

        unique_result = gk.resolve_tag("87654321", prefix_index, tail_index)
        check(unique_result is not None, "legacy 8-char tag with 1 candidate resolves to a StateEntry")
        if unique_result:
            check(unique_result.uuid == session_ids[2], f"unique result UUID is {session_ids[2]}")


def test_ac3_ambiguous_tail12_tag_no_resolution() -> None:
    print("--- Ambiguous tail-12 tag non-resolution (birthday collision) ---")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        state_dir = tmp / "_state"
        session_ids = [
            "00000000-0000-0000-0000-000000001234",
            "11111111-1111-1111-1111-000000001234",
            "22222222-2222-2222-2222-000000005678",
        ]
        setup_zeroed_state_dir(state_dir, session_ids)

        prefix_index, tail_index = gk.build_uuid_index(state_dir)

        ambiguous_result = gk.resolve_tag("000000001234", prefix_index, tail_index)
        check(ambiguous_result is None, "tail-12 tag with 2 candidates (birthday collision) returns None to avoid ambiguous attribution")

        unique_result = gk.resolve_tag("000000005678", prefix_index, tail_index)
        check(unique_result is not None, "tail-12 tag with 1 candidate resolves to a StateEntry")
        if unique_result:
            check(unique_result.uuid == session_ids[2], f"unique result UUID is {session_ids[2]}")


def test_ac3_nonexistent_tag_no_resolution() -> None:
    print("--- Non-existent tag returns None ---")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        state_dir = tmp / "_state"
        session_ids = ["12345678-0000-0000-0000-000000000001"]
        setup_zeroed_state_dir(state_dir, session_ids)

        prefix_index, tail_index = gk.build_uuid_index(state_dir)

        result_legacy = gk.resolve_tag("deadbeef", prefix_index, tail_index)
        check(result_legacy is None, "legacy 8-char tag that does not match any session returns None")

        result_tail12 = gk.resolve_tag("deadbeefcafe", prefix_index, tail_index)
        check(result_tail12 is None, "tail-12 tag that does not match any session returns None")

        result_invalid = gk.resolve_tag("too_short", prefix_index, tail_index)
        check(result_invalid is None, "tag with invalid length (not 8 or 12) returns None")


def test_iter_entries_deduplication() -> None:
    print("--- iter_entries deduplication ---")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        state_dir = tmp / "_state"
        session_ids = ["12345678-0000-0000-0000-000000000001"]
        setup_zeroed_state_dir(state_dir, session_ids)

        prefix_index, tail_index = gk.build_uuid_index(state_dir)

        entries = gk.iter_entries(prefix_index, tail_index)
        check(len(entries) == 1, f"iter_entries must return each UUID exactly once, got {len(entries)}")
        check(entries[0].uuid == session_ids[0], f"entry UUID is {session_ids[0]}")


def main() -> int:
    print("=== session id tail-12 tag collision tests ===")
    print()

    repo_root = Path(__file__).resolve().parent.parent.parent
    if (repo_root / "_projects" / "_state").exists():
        print("FATAL: Running in shared tree with existing _state directory")
        print("  This test requires isolation in a worktree or temp directory")
        return 1

    test_ac1_zeroed_two_sessions_separated()
    test_ac3_ambiguous_legacy_tag_no_resolution()
    test_ac3_ambiguous_tail12_tag_no_resolution()
    test_ac3_nonexistent_tag_no_resolution()
    test_iter_entries_deduplication()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
