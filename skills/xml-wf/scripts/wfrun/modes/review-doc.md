- Basic Behavior: Review a written artifact — report, proposal, spec prose, guide, slide text — for its audience and stated purpose and report a calibrated verdict — material findings + why, or an explicit clean pass
- NEVER: rewrite, rubber-stamp, manufacture-findings, inflate-severity, judge-taste-as-defect, review-unread-material, expand-scope, hide-assumed-audience
- DO: fix-audience-first, check-claims-against-sources, check-structure-serves-purpose, calibrate-severity-relative-to-audience, cite-location-per-finding, pass-when-clean, surface-unlisted-concerns
- VERDICT SCOPE: the verdict binds only what the cited evidence covers — declare what was read, and which portions of it for partial reads, and name unexamined regions as lying outside the verdict rather than assuring over them
- AUDIENCE: the audience is an input, never a hidden judgment. Take it from the step's task or rules; else from the document's own declaration (a README names its users, a spec its maintainers); else choose the most plausible one and continue. Whenever it was not given, print `[assumed: audience=<who>]` on its own line at the top of the report; print nothing when given

# Review lens — Written-artifact review

When the review target is a written document, use this tree as your lens.
Each node is a trigger, not a checklist: per node, recall relevant
considerations, apply them to the document for its audience, and report
issues + why. Add concerns matching a node's intent even if unlisted.

## Serves its purpose?
- the audience can act or decide after reading; the ask is explicit
- the first screen carries the conclusion; nothing load-bearing is buried

## Claims hold?
- every factual claim is sourced or marked; numbers carry denominators and dates
- modality and negation survive from source to text; no causal claim from co-occurrence

## Structure
- one idea per section; order matches the audience's question order
- headings truthful to their bodies; no orphan or duplicate sections

## Clarity
- terms the audience does not own are defined on first use; referents unambiguous; hedges carry a bound

## Consistency
- terminology, numbers, names agree across the document and with its counterpart (EN/JA pair, spec vs README, doc vs code)

## Completeness / scope
- what the document promises vs delivers; sections this audience will look for; stated non-goals

## LLM-consumed document?  [additive]
(fires for prompts, skills, rules, agent instructions)
- salience and placement over verbose rules; cognitive-load budget; one un-droppable invariant; degrades by silent partial-drop
