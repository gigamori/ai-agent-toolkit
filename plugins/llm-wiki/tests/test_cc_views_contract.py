"""
Contract test: byte equivalence of vendored cc_views.sql copy with canonical.

Ensures that the cc_views.sql file in the plugin (vendored copy) remains
byte-identical to the canonical source in skills/inspect-cc-log/scripts/views.sql.
This guards against drift and verifies the build-time vendor contract.
"""

import hashlib
from pathlib import Path


def test_cc_views_byte_equivalence():
    """
    Verify that the vendored cc_views.sql in the plugin is identical to the
    canonical source in the skills/inspect-cc-log folder.

    The canonical source is the single source of truth.
    The vendored copy must be byte-identical (no edits, reordering, or formatting changes).
    """
    # Resolve paths relative to the repository root.
    # __file__ = plugins/llm-wiki/tests/test_cc_views_contract.py
    #   parents[0]=tests  parents[1]=llm-wiki  parents[2]=plugins  parents[3]=repo root
    repo_root = Path(__file__).resolve().parents[3]  # ai-agent-toolkit/

    canonical = repo_root / "skills" / "inspect-cc-log" / "scripts" / "views.sql"
    vendored = repo_root / "plugins" / "llm-wiki" / "llmwiki" / "ingest" / "cc_views.sql"

    # Both files must exist
    assert canonical.exists(), f"Canonical file not found: {canonical}"
    assert vendored.exists(), f"Vendored file not found: {vendored}"

    # Read both files as binary to ensure exact byte-for-byte comparison
    canonical_bytes = canonical.read_bytes()
    vendored_bytes = vendored.read_bytes()

    # Compute hashes for debugging
    canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
    vendored_hash = hashlib.sha256(vendored_bytes).hexdigest()

    # Assert exact byte equivalence
    assert canonical_hash == vendored_hash, (
        f"cc_views.sql byte mismatch:\n"
        f"  Canonical ({canonical}): {canonical_hash}\n"
        f"  Vendored  ({vendored}): {vendored_hash}\n"
        f"Please re-sync the vendored copy (verbatim duplicate, no edits)."
    )

    # Also verify the content is not empty
    assert len(canonical_bytes) > 0, "Canonical file is empty"
    assert len(vendored_bytes) > 0, "Vendored file is empty"
