"""llmwiki — immutable engine for an LLM-maintained wiki.

Path-import package (no-install): consumers add the plugin root to ``sys.path``
at a single CLI/bin bootstrap point and ``import llmwiki``. No build step and no
``console_scripts`` launcher are required (see package-cli-architecture spec).

This top-level module exposes only ``__version__`` at the P1 skeleton stage.
The concrete public re-exports (``core`` / ``write`` / ``ingest`` / ``lint`` /
``view`` / ``init`` symbols) are populated as the modules are migrated into their
subpackages in later steps; re-exporting them here before the modules exist would
break ``import llmwiki``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
