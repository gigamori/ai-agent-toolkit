import hashlib
from pathlib import Path

import pytest


def test_cc_views_byte_equivalence():
    repo_root = Path(__file__).resolve().parents[3]

    canonical = repo_root / "skills" / "inspect-cc-log" / "scripts" / "views.sql"
    vendored = repo_root / "plugins" / "llm-wiki" / "llmwiki" / "ingest" / "cc_views.sql"

    if not canonical.exists():
        pytest.skip(
            f"Canonical skills/inspect-cc-log/scripts/views.sql is CC-only and "
            f"absent in this harness: {canonical}"
        )

    assert canonical.exists(), f"Canonical file not found: {canonical}"
    assert vendored.exists(), f"Vendored file not found: {vendored}"

    canonical_bytes = canonical.read_bytes()
    vendored_bytes = vendored.read_bytes()

    canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
    vendored_hash = hashlib.sha256(vendored_bytes).hexdigest()

    assert canonical_hash == vendored_hash, (
        f"cc_views.sql byte mismatch:\n"
        f"  Canonical ({canonical}): {canonical_hash}\n"
        f"  Vendored  ({vendored}): {vendored_hash}\n"
        f"Please re-sync the vendored copy (verbatim duplicate, no edits)."
    )

    assert len(canonical_bytes) > 0, "Canonical file is empty"
    assert len(vendored_bytes) > 0, "Vendored file is empty"
