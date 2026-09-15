# Project Routing Task — moved

The authoritative project-router spec now lives in the subagent definition
body: **`agents/project-router.md`** (the body below its frontmatter).

The subagent loads that body as its system prompt automatically, so the main
agent no longer inlines this file — it passes only the JSON context block to
`subagent_type: taskflow:project-router`.

Edit `agents/project-router.md` to change router behavior. This file is kept
only as a redirect for older references.
