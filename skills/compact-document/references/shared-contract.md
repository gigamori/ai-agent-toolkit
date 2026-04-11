# Shared Compaction Contract

## Global Rules

- Optimize for loss-minimizing compaction, not abstract summarization
- Preserve information needed for faithful reconstruction, continued work, decision recovery, procedure execution, or item coverage
- Prefer shortening, restructuring, and deduplicating over deleting first-class information
- Omit inapplicable sections rather than padding
- Never invent missing information; use "Unknown" / "Not specified"
- Output in the language specified by the language field; fall back to source language

## First-Class Information

Items that must be preserved under default settings:
- Named items presented as main items
- Major section-level topics or topic blocks
- Explicit decisions, outcomes, recommendations
- Ordered steps, branch conditions, rollback logic, validation checks
- Key entities, tools, places, files, roles, actors, systems
- Configuration values, commands, identifiers, model names, paths, versions (when operationally relevant)
- Comparisons, alternatives, author/speaker judgments (when they affect interpretation)

## Preservation Policy

**strict**: preserve first-class information as default; shorten/group/deduplicate, do not omit unless user accepts stronger compression
**standard**: preserve first-class information as default; allow compression of secondary explanation and repeated framing when first-class information remains recoverable; do not omit concrete names, values, commands, items, or branch conditions

Rule: compress descriptions before deleting items. Remove repetition before removing distinct content.

## Coverage Policy

**compact**: keep only central first-class items (user must clearly want strong compression)
**broad**: keep all major first-class items; compress descriptions aggressively before omitting
**exhaustive**: keep all first-class items; preserve named lists, grouped recommendations, item coverage, comparison sets

Default: broad for ordinary compaction; exhaustive for guides, roundups, comparisons, travel articles, technical walkthroughs, operational documents.

## Structure Preservation

- Keep major section structure when it carries meaning
- Do not flatten multi-part articles, grouped lists, staged explanations, itineraries, comparison blocks, or grouped recommendations into generic point summaries
- flatten_if_needed: flatten only after preserving first-class items and major grouping relationships

## General Article Subtype Rules

**narrative_article**: preserve argument/explanatory flow; keep examples but do not let them overwhelm conceptual structure
**guide_roundup_walkthrough**: preserve item coverage, grouped categories, itinerary blocks, comparison sets, recommendation groups, practical selection criteria; compress descriptions before omitting any first-class listed item

## Compound Source Rules

Remain inside the selected primary mode. Summarize secondary-mode material only to support the primary-mode objective. Do not split into multiple independent mode outputs unless explicitly requested.

## Length Control

- Dense but not bloated
- Structured compression over narrative repetition
- Quote only when exact wording is operationally important
- Shorten descriptions before dropping coverage

## Chunk Rules (when chunking is on)

1. Process chunks in source order
2. Preserve major updates across chunks
3. Prefer most recent explicit value on conflicts
4. Note meaningful changes as "Updated from X to Y"
5. Create short structured chunk briefs, not freeform mini-essays
6. Preserve first-class items at chunk level before higher-level merge

## Merge Rules

1. Deduplicate repeated descriptions, not distinct items
2. Keep latest explicit value on conflicts
3. Preserve mode-critical information over general background
4. Keep chronology only where it explains decisions, causality, or state transitions
5. Preserve all first-class items required by coverage level
6. Produce one coherent compacted backbone, not a pile of chunk summaries

## Edge Cases

- Source too short for meaningful compaction: preserve with minimal restructuring
- Chunk briefs collectively too verbose: reduce brief detail first; bypass chunking if still insufficient
- Source outside intended registry: prefer minimal-loss preservation over forced categorization
