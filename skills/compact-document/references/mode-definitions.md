# Mode Definitions

Each mode defines: output structure (numbered sections), priority order (same sequence), and mode-specific detail rules. The render agent uses this to produce final output.

---

## research_paper

**Output structure:**
1. Paper identity
2. Research question and thesis
3. Method
4. Data and experimental setup
5. Results
6. Comparisons and baselines
7. Limitations and assumptions
8. Practical implications
9. Open questions

**Detail rules:** Include datasets, benchmark names, task definitions, metrics with units, ablations, key equations (only if needed), architecture descriptions (if central). Do not over-copy background/literature review.
**Conflict rule:** Prefer more precise claims from results/discussion over broad intro/abstract framing.

---

## spec_design_doc

**Output structure:**
1. Document identity
2. Purpose and scope
3. System overview
4. Components and interfaces
5. Data flow or control flow
6. Key design decisions
7. Constraints and non-goals
8. Risks and open issues
9. Implementation implications

**Detail rules:** Include APIs, schemas, function signatures (if central), protocols, state transitions, configuration rules, versioning, migration considerations. Prefer signatures and behavioral rules over full code blocks.
**Conflict rule:** Mark final selected design and rejected alternatives clearly.

---

## procedure_runbook

**Output structure:**
1. Procedure identity
2. Purpose
3. Preconditions
4. Required inputs, tools, permissions, and dependencies
5. Ordered procedure
6. Branches and decision points
7. Errors, rollback, and recovery
8. Validation and completion criteria
9. Safety notes and constraints

**Detail rules:** Include exact commands (when operationally necessary), environment assumptions, required permissions, step order/dependencies, expected outputs, rollback steps, verification steps. Omit argumentative narrative unless it affects execution. Keep sequence integrity; do not merge steps that would hide dependency or order.

---

## conversation_meeting

**Output structure:**
1. Session identity
2. Initial request or objective
3. Intent evolution
4. Key decisions
5. Work performed
6. Evidence, files, or artifacts mentioned
7. Problems, disagreements, or corrections
8. Outstanding items
9. Current state and immediate next step

**Detail rules:** Include decision-changing instructions, final decisions, files touched, important commands/config/short code signatures. Include failed approaches only when they explain the final direction. Do not dump all messages verbatim; quote only decision-critical phrasing.
A claim enters 4 only with explicit affirmative tokens from the deciding speaker. Mere absence of objection keeps the claim in 3 or 8, not 4.
**Current state:** Describe work state immediately before compaction. Keep concrete and operational.

---

## debug_incident_log

**Output structure:**
1. Incident identity
2. Problem statement
3. Symptoms and impact
4. Timeline of investigation
5. Hypotheses and tests
6. Root cause or current best explanation
7. Fixes, mitigations, and verification
8. Remaining risks and unknowns
9. Recommended next action

**Detail rules:** Include timestamps (if useful), summarized logs/errors/stack traces, environment conditions, reproduction clues, essential commands/config changes, what was ruled out, resolution status.
**Precision:** Distinguish observed fact vs hypothesis vs confirmed cause vs workaround vs permanent fix.
**Status:** Final section reflects current operational state; say explicitly if issue remains active.

---

## policy_contract

**Output structure:**
1. Document identity
2. Scope and covered parties
3. Key definitions
4. Obligations
5. Permissions and prohibitions
6. Conditions, exceptions, and carve-outs
7. Deadlines, durations, and termination logic
8. Risks, ambiguities, and points needing review
9. Practical implications

**Detail rules:** Include defined terms, clause dependencies, exception structures, notice periods, renewal/termination conditions, liability/responsibility allocation. Use cautious language.
**Safety:** State ambiguity explicitly instead of resolving by inference.

---

## general_article

### Subtype: narrative_article

**Output structure:**
1. Article identity
2. Main subject and purpose
3. Major explanatory or argumentative blocks
4. First-class items and named entities
5. Practical details, comparisons, or examples
6. Author judgments, caveats, or takeaways

**Rendering:** Section 3 emphasizes explanatory/argumentative blocks. Section 4 may group named items under the blocks where discussed.

### Subtype: guide_roundup_walkthrough

**Output structure:**
1. Article identity
2. Main subject and purpose
3. Category, region, itinerary, comparison, or grouping blocks
4. First-class items within each block
5. Practical details, comparison criteria, or setup notes
6. Author judgments, caveats, or takeaways

**Rendering:** Section 3 preserves the article's grouping logic. Section 4 explicitly retains first-class listed items within those groupings. Keep grouped items under original grouping rather than flattening into one undifferentiated list.

**Common detail rules (both subtypes):** Preserve named tools/places/products/venues/destinations/entities presented as main items, grouped recommendation sets, itinerary/comparison/category blocks, practical details when core to the article's teaching, author evaluations when they affect interpretation. Compress descriptions before deleting first-class items. If coverage is exhaustive, preserve all first-class listed items.
