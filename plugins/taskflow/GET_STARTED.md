# Get started with taskflow

Start project work with `pj:<project>`, and Claude catches up on that project's active work before answering. taskflow keeps the work, decisions, and next steps across chats, so your growing list of Claude Code sessions does not become your to-do list.

## Sound familiar?

- I split a large piece of work into several chats, then lose track of which chat has the latest progress.
- I know something is still in progress, but I cannot quickly tell what remains.
- I remember a decision or research result, but it is buried in an old chat.
- I hesitate to start a new chat because I will have to explain the work again.

You do not have to maintain a separate record for this. taskflow keeps the project work organized while you talk to Claude normally.

## Your first action

When you begin work, name the project at the start of your prompt:

```
pj:customer-portal refresh the login screen
```

Use the same project name whenever you return in a new chat. In the same chat, you can continue without repeating `pj:`. If you cannot remember a project name, type `pj:?`.

## From unfinished work to a clear next move

Imagine you are improving a customer portal over several chats.

1. In the first chat, type `pj:customer-portal refresh the login screen`. Claude starts with the project's in-progress work and continues with you.
2. Work with Claude normally. As work advances, task progress and the list of what remains stay current. When the chat ends, that session's work is recorded with its task.
3. In a later chat, type `pj:customer-portal /progress audit`. taskflow classifies every task by its remaining work, so you can see which work is still pending and which may be ready to finish.
4. Continue the task in the chat you choose. Claude has the project and active-task context, so you do not need to reconstruct the work from old chats.
5. When the work is truly complete, say `/progress approve login-screen`. Claude asks for your OK before marking it Done.

## See your work at a glance

Run `/kanban` to open a local board of every project and task. View work by status or by project, filter the board, and open a task's session history when you need to find the chat behind it.

## You stay in control

- taskflow never guesses your project. If you have not named one and none is already set in the chat, it stays out of the way.
- A task moves to Done only with your explicit approval.
- When you ask to create a project or save notes, Claude confirms before making the change.

## Start today

taskflow works with Claude Code. Install it once:

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install taskflow@ai-agent-toolkit
```

There is nothing else to configure. The first time you use `pj:<project>` in a workspace, taskflow creates its project area. For complete instructions, see the [user guide](USER_GUIDE.md).
