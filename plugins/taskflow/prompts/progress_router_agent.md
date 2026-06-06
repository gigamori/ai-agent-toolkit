# Progress Router Task — moved

The authoritative progress-router spec now lives in the subagent definition
body: **`agents/progress-router.md`** (the body below its frontmatter).

The subagent loads that body as its system prompt automatically, so the
`/progress` skill no longer inlines this file — it passes only the JSON
context block to `subagent_type: taskflow:progress-router`.

Edit `agents/progress-router.md` to change router behavior. This file is kept
only as a redirect for older references.
