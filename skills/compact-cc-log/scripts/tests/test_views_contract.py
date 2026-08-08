"""
Contract test: byte equivalence of vendored views.sql with the canonical
inspect-cc-log source.

Ensures scripts/views.sql here stays byte-identical to
skills/inspect-cc-log/scripts/views.sql. Same pattern as
plugins/llm-wiki/tests/test_cc_views_contract.py.
"""

import hashlib
from pathlib import Path


def test_views_sql_byte_equivalence():
    # __file__ = skills/compact-cc-log/scripts/tests/test_views_contract.py
    #   parents[0]=tests parents[1]=scripts parents[2]=compact-cc-log parents[3]=skills parents[4]=repo root
    repo_root = Path(__file__).resolve().parents[4]

    canonical = repo_root / "skills" / "inspect-cc-log" / "scripts" / "views.sql"
    vendored = repo_root / "skills" / "compact-cc-log" / "scripts" / "views.sql"

    assert canonical.exists(), f"Canonical file not found: {canonical}"
    assert vendored.exists(), f"Vendored file not found: {vendored}"

    canonical_bytes = canonical.read_bytes()
    vendored_bytes = vendored.read_bytes()

    canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
    vendored_hash = hashlib.sha256(vendored_bytes).hexdigest()

    assert canonical_hash == vendored_hash, (
        f"views.sql byte mismatch:\n"
        f"  Canonical ({canonical}): {canonical_hash}\n"
        f"  Vendored  ({vendored}): {vendored_hash}\n"
        f"Please re-sync the vendored copy (verbatim duplicate, no edits)."
    )

    assert len(canonical_bytes) > 0, "Canonical file is empty"
    assert len(vendored_bytes) > 0, "Vendored file is empty"
