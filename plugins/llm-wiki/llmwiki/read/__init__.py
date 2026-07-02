"""llmwiki.read — the read profile (D-2): question-answering read paths.

Houses ``query`` (index-direct enumeration) and ``qmd_search`` (optional external
qmd full-text backend, shell-out). Both are dependency-free and their import
closure is ``llmwiki.core`` + stdlib ONLY — never ``llmwiki.write`` /
``llmwiki.ingest`` (the read-profile import-closure invariant, D-2). qmd is an
external CLI reached by subprocess, so it adds no Python dependency to this layer.
"""
