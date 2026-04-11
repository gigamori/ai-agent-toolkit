# Guide for Creating SubAgent Delegation Skills

Process guide for /create-skill when creating skills that involve SubAgent delegation.

## Applicability

Apply this guide when any of the following conditions are met:

- The workflow can be decomposed into multiple independent subtasks
- Tasks are large enough to exhaust the main thread's context
- Parallel execution would improve efficiency

## Reference Files

| File | Purpose |
|------|---------|
| `subagent-protocol.md` | Protocol (B). Copy to skill_dir |
| `prompt.md` | Prompt Template stub (C). Copy and fill in per task type |

## Creation Process

### Step 1: Understand the Workflow

Confirm the following with the user:

- What is the purpose of this skill?
- What are the inputs (data files, documents, code, etc.)?
- What is the expected final output?
- What is the general flow of the workflow?

### Step 2: Task Decomposition

Decompose the workflow into task types. A task type = a unit of work handled by a single SubAgent invocation.

Present to the user and obtain agreement:

```
Proposed task types:
1. {task-type-a}: {what it does}
2. {task-type-b}: {what it does}
3. (Integration): {if cross-validation / final output is needed}

Dependencies:
- {task-type-a} and {task-type-b} are independent (can run in parallel)
- Integration runs after all tasks complete
```

### Step 3: Design Each Task Type

Determine the following for each task type through dialogue with the user.

#### 3a. role

- What kind of specialist is this task type's SubAgent?
- What is it responsible for and what is out of scope?

#### 3b. rules

- What are the quality criteria (accuracy, completeness, reproducibility, etc.)?
- What is prohibited (no guessing, no data modification, etc.)?
- What are the output format constraints?

#### 3c. procedure

- Step-by-step instructions
- Is each step specific and verifiable?
- Are verification/confirmation steps included?
- Are tool usage points clearly identified?

#### 3d. tools

- Tools, commands, and APIs to use
- How to invoke them, where to find documentation
- Omit if not needed

#### 3e. task_description schema

- What information items should be included in each subtask description?
- Examples: issue name, target variables, analysis perspective, etc.

### Step 4: Runtime Design

Determine the following:

- Information to include in Context Handoff (agreements, term definitions, task decomposition results, etc.)
- Naming convention for output_file_path
- Whether Integration is needed and how to execute it
    - Cross-validation perspectives
    - Final output format
    - Execute on main thread or delegate to SubAgent

### Step 5: File Generation

Generate in the following order:

1. Create skill_dir
2. Copy `subagent-protocol.md` to skill_dir
3. Create a directory for each task type
4. Copy `prompt.md` to each directory
5. Fill in domain-specific content from Step 3 into each prompt.md
6. If delegating Integration to a SubAgent, create an Integration directory as well
7. Generate SKILL.md (see template below)

### Step 6: Verification

- Does each prompt.md follow the structural conventions in subagent-protocol.md?
- Are runtime variables `{context_handoff_path}`, `{task_description}`, `{output_file_path}` correctly placed?
- Does a `<constraints>` section exist in every prompt.md?
- Does SKILL.md list all task types and naming conventions?

## SKILL.md Template

```markdown
---
name: {skill-name}
description: {description}
---

# {Skill Name}

## Overview
{Purpose and target of the skill}

## Input
{Types and formats of input}

## SubAgent Delegation
Protocol: {skill_dir}/subagent-protocol.md

Task types:
- {task-type-a}: {overview}
- {task-type-b}: {overview}

Runtime variables:
- Information to include in task_description: {list items}
- Naming convention for output_file_path: {convention}

## Discovery
{What the main thread does first: requirements confirmation, task decomposition, Context Handoff creation}

## Final Output
{Format and destination of the final deliverable}
```
