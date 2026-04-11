---
name: compact-document
description: Compact articles, papers, specs, runbooks, meeting transcripts, incident logs, policies, and contracts into dense structured representations with minimal information loss. Supports 7 document modes with automatic mode detection, configurable compaction axes (preservation, coverage, structure), and chunked processing for long documents. Use when the user says compact, summarize, condense, shorten, or compress a document, article, transcript, or text. Also use for conversation compaction, meeting notes extraction, or incident log summarization.
---

# Document Compaction

Multi-mode compaction framework that shortens, reorganizes, and deduplicates documents while minimizing loss of first-class information.

## Execution Model

Two paths depending on source length:

**Path A (no chunking):** Discovery -> render SubAgent -> final output
**Path B (chunking):** Discovery -> chunk split -> chunk-brief SubAgents (parallel) -> merge (main thread) -> render SubAgent -> final output

## SubAgent Delegation

Protocol: compact-document/subagent-protocol.md

Task types:
- chunk-brief: Extract structured brief from one source chunk (parallel, max 4)
- render: Produce final compacted output in mode-specific structure

Runtime variables:
- task_description: mode, axes, chunk text or merged state, source_name
- output_file_path: `{working_dir}/compact-output/{source_name}-brief-{chunk_id}.md` or `{working_dir}/compact-output/{source_name}-compacted.md`

## Discovery (Main Thread)

This is always performed by the main thread. Do not delegate Discovery.

### Step 1: Receive Input

Accept the source document. Identify the source_name from filename, title, or user label.

### Step 2: Determine Mode

**Mode registry:**

| mode | when to use |
|------|------------|
| research_paper | academic/technical papers with research question, method, results |
| spec_design_doc | specifications, architecture docs, design proposals |
| procedure_runbook | procedures, manuals, SOPs, runbooks, how-to guides |
| conversation_meeting | conversation logs, meeting transcripts, working sessions |
| debug_incident_log | debugging sessions, incident timelines, postmortems |
| policy_contract | policy documents, contracts, terms, rule-based formal text |
| general_article | articles, guides, roundups, walkthroughs, comparisons, travel articles |

**If user specifies a mode:** accept it as final. Do not reinterpret.

**If user does not specify:** inspect only lightweight signals (filename, title, headings, TOC, first 500-2000 tokens, structural markers). Propose one mode:

```
Suggested mode: [mode_id]
Confidence: [high|medium|low]
Reasons:
- [reason 1]
- [reason 2]
- [reason 3 if needed]
Fallback: [mode_id or "none"]

Reply with one of:
- Use suggested mode
- Use fallback mode
- Use this mode instead: [mode_id]
```

Confidence rules:
- high: title/headings/markers strongly indicate one mode; competing modes weak
- medium: one mode best fit but one fallback plausible
- low: signals sparse, conflicting, or mixed

**No-response fallback:** If user's next message says continue/proceed/compact without specifying a mode, adopt the suggested mode. If user names a different mode or states an unambiguous objective mapping to one mode, use that.

**Compound documents:** Choose one primary mode based on (1) user's stated goal, (2) dominant structure, (3) sections with most operational value. Treat secondary structures as subordinate. Do not switch to multi-mode unless user explicitly requests it.

### Step 3: Determine Article Subtype (general_article only)

If mode is general_article, determine article_subtype:
- **narrative_article**: mainly explanatory/argumentative; named items function as examples; reader takeaway is conceptual
- **guide_roundup_walkthrough**: organized around named items, destinations, tools, products, comparisons, recommendations, category blocks; item coverage is central

Default: if grouped named items or comparison blocks dominate, use guide_roundup_walkthrough; otherwise narrative_article.

### Step 4: Determine Compaction Axes

| axis | values | default |
|------|--------|---------|
| preservation | strict, standard | strict |
| coverage | compact, broad, exhaustive | broad |
| structure | preserve_major_structure, preserve_grouping_only, flatten_if_needed | preserve_major_structure |

**Escalate coverage to exhaustive** when source is primarily: guide, roundup, list of named items, travel article, comparison article, technical walkthrough, operational/procedural document.

Do not lower preservation unless user explicitly requests aggressive compression.

### Step 5: Determine Language

1. User explicitly specifies output language -> use it
2. Language field already provided -> use it as output language
3. Otherwise -> infer source language and use it

### Step 6: Decide Chunking

Decide chunking after mode and axes are confirmed.

**Thresholds:**
- ~6000 tokens or less: no chunking
- ~6000-16000 tokens: 2-4 chunks
- ~16000+ tokens: 4+ chunks as needed
- If chunk briefs would be longer than source: skip chunking

**Chunking rules:**
- Prefer natural boundaries over fixed-size windows
- Prefer fewer, larger chunks (target 2000-4000 tokens per chunk)
- Default overlap: 0. Use 100-200 tokens only when boundary context loss would materially damage quality
- When structure = preserve_major_structure, avoid splitting across major named sections
- When coverage = exhaustive, keep complete named item groups together

**Natural boundaries by mode:**
- research_paper: abstract, intro, method, results, discussion, appendix
- spec_design_doc: sections, components, interfaces, decision blocks
- procedure_runbook: prerequisites, preparation, step groups, rollback, validation
- conversation_meeting: topic shifts, turn ranges, decision points, work-state transitions
- debug_incident_log: symptoms, investigation phases, hypotheses, tests, fixes
- policy_contract: definitions, clauses, obligations, exceptions, durations
- general_article (narrative): headings, major topic blocks
- general_article (guide): grouped lists, recommendation groups, category blocks, item_groups

### Step 7: Create Context Handoff

Write a Context Handoff file to `{working_dir}/compact-output/context-handoff.md` containing:

```
source_name: [name]
mode: [mode_id]
article_subtype: [narrative_article|guide_roundup_walkthrough|none]
language: [output language]
preservation: [strict|standard]
coverage: [compact|broad|exhaustive]
structure: [preserve_major_structure|preserve_grouping_only|flatten_if_needed]
chunking: [yes|no]
user_goal: [if specified]
```

### Step 8: Route to Execution

**If chunking is off:**
1. Read render/prompt.md, resolve runtime variables
2. In task_description, include: "Chunking is off. Raw source follows:" then the full source text
3. Launch render SubAgent -> output file is the final result
4. Present result to user

**If chunking is on:**
1. Split source into chunks at natural boundaries decided in Step 6
2. For each chunk, read chunk-brief/prompt.md, resolve variables:
   - task_description: chunk_id, source_span, chunk_text
   - output_file_path: `{working_dir}/compact-output/{source_name}-brief-{chunk_id}.md`
3. Launch chunk-brief SubAgents in parallel (max 4)
4. Collect all chunk briefs
5. **Merge chunk briefs (main thread):** see Merge Procedure below
6. Read render/prompt.md, resolve variables:
   - task_description: "Chunking is on. Merged compacted state follows:" then the merged state
   - output_file_path: `{working_dir}/compact-output/{source_name}-compacted.md`
7. Launch render SubAgent -> output file is the final result
8. Present result to user

## Merge Procedure (Main Thread)

When merging chunk briefs into consolidated state, follow these rules:

1. Sort briefs by source order (chunk_id)
2. Identify repeated background; keep it once
3. Preserve updates and final values
4. Keep chronology only where it matters for decisions, causality, or state transitions
5. Preserve distinct first-class items per coverage level
6. For guide_roundup_walkthrough, preserve grouped item coverage before prose compression

Produce a merged_compacted_state with these fields:
- source_name, mode, article_subtype, preservation, coverage, structure
- section_backbone: major sections that should remain visible
- preserved_items: deduplicated first-class items
- grouped_coverage_blocks: grouped item/category/comparison sets
- facts_and_operations: deduplicated factual/procedural/operational content
- final_values: latest decisions, statuses, outcomes
- important_updates: meaningful changes across source
- open_items: remaining unknowns, risks, pending items

Conflict resolution: prefer latest explicit value. Note meaningful changes as "Updated from X to Y."

## Edge Cases

- **Source too short:** Do not chunk. Preserve with minimal restructuring. Skip unnecessary scaffolding.
- **Source outside registry** (poetry, fiction, raw data, code-only): use closest mode only if it preserves content faithfully; otherwise apply minimal-loss compaction and note in output.
- **Source nearly empty:** Do not fabricate structure; preserve what exists.
- **Mode recheck:** Do not re-classify during compaction unless strong contradictory evidence appears. If switching, switch once and re-plan chunking only if existing boundaries are clearly incompatible.

## Forbidden Behavior

- Do not write a detailed summary before mode confirmation when mode was unspecified
- Do not read the whole source only to generate a mode proposal
- Do not output long reasoning at the gateway stage
- Do not offer more than one fallback mode
- Do not delete first-class information merely for elegance

## Reference Files

For detailed compaction rules and mode output structures, SubAgents read:
- [shared-contract.md](references/shared-contract.md) - global compaction rules
- [mode-definitions.md](references/mode-definitions.md) - mode-specific output structures and detail rules
