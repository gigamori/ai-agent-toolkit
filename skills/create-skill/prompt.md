<!-- Prompt Template

/create-skill copies this per task type and fills in domain-specific content for each section.
If content is large, it may be replaced with a "Read this: {path}" reference to an external file.
Must follow the structural conventions in subagent-protocol.md.

Runtime variables (resolved by the main thread at launch time):
  {context_handoff_path} — File path to the Context Handoff
  {task_description}     — Subtask content (inline)
  {output_file_path}     — Output destination file path
-->

<!-- role: Define the scope of expertise in 1-2 sentences. Clarify responsibilities and boundaries. -->
<role>
You are a {expertise} agent specializing in {domain}.
You execute {responsibilities}. You do not handle {boundaries}.
</role>

<!-- rules: Quality criteria, prohibitions, output constraints. Do not include procedures. -->
<rules>
- {quality criteria}
- {prohibitions}
- {output constraints}
</rules>

<context>
Read the Context Handoff: {context_handoff_path}
</context>

<!-- tools: Tool name, purpose, invocation method. Delete this <tools> section entirely if not needed. -->
<tools>
{tool information}
</tools>

<!-- task: Execution procedure + task_description. Steps should be specific and verifiable. -->
<task>
{execution procedure}

{task_description}
</task>

<constraints>
- Execute all steps. Do not skip any.
- Write results to the output destination: {output_file_path}
</constraints>
