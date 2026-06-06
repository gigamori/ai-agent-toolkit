# Project Routing Task — moved

The authoritative project-router spec now lives in the subagent definition
body: **`agents/project-router.md`** (the body below its frontmatter).

The subagent loads that body as its system prompt automatically, so the main
agent no longer inlines this file — it passes only the JSON context block to
`subagent_type: project-router`.

Edit `agents/project-router.md` to change router behavior (including the
`Step 2b` autosave-detection conditions). This file is kept only as a redirect
for older references.
