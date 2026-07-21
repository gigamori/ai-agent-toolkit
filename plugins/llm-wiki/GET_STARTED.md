# Help Claude give better answers with llm-wiki

Give Claude the documents and notes that matter, then ask questions in plain language when you need an answer. Your one new habit is simple: add material worth remembering to your wiki.

## Sound familiar?

- I open a new chat and have to explain the same background again.
- I know a decision exists, but I cannot remember whether it was in a note, a document, or an earlier chat.
- I want a clear answer from the material we already have, not another search through old files.

llm-wiki turns those scattered materials into pages Claude can use when answering you.

## Your first action

Add one document you regularly need Claude to understand:

```
/wiki-ingest ./docs/customer-plan.md
```

For example, add the customer plan before asking, *“What did we agree to do for this customer?”* Claude answers from the wiki and shows the page path for each point, so you can check where the answer came from.

## From first document to a useful answer

Imagine you want Claude to stay up to date on an important customer plan.

1. Create a wiki once with `/wiki-init`; it asks where to put it.
2. Add the plan with `/wiki-ingest ./docs/customer-plan.md`.
3. Review the proposed page updates and approve them when they look right. Nothing is saved until you do.
4. Continue your work. Later, ask Claude what the plan says or what was decided.
5. Claude reads the relevant pages and answers with page-path citations.
6. If an answer is worth keeping, say *“file that as a page.”* Claude keeps it as a clearly labelled conclusion until you choose to promote it.
7. The next time you need the answer, ask again instead of rebuilding the context from old chats.

## See what Claude knows

Run:

```
/wiki-view
```

A local viewer opens at `http://127.0.0.1:17330/`. You can click through linked pages and see whether each is an established source page or Claude's clearly labelled conclusion. Your original source material is not shown in the viewer.

## You stay in control

- Asking a question only reads your wiki; it does not change anything.
- Before an ordinary import writes pages, you review and approve it.
- If an import fails partway through, its changes are rolled back.

## Get started

Install the plugin in Claude Code:

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install llm-wiki@ai-agent-toolkit
```

Make sure [`uv`](https://docs.astral.sh/uv/) is on your PATH, then run:

```
/wiki-init
```

Add one document you want Claude to understand. For complete instructions, see the [User Guide](USER_GUIDE.md).
