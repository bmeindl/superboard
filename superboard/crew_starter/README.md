# Your first crew task

The writer owns a release summary. The checker reads the source independently and tells the writer which claims it supports. You get the checked summary on the card where you asked for it. Each role runs through your configured agent CLI and uses your provider plan.

## Try the release desk

1. Start Superboard in the workspace where you installed Crew. In **My to-dos**, paste the following into the field for a new card. Send it with **⌘+Enter** on macOS or **Ctrl+Enter** on Linux.

   > @writer Draft release notes from crew/examples/release-1.md. Include a source check before you finish.

2. Open the card when the reply arrives. Compare the summary with the [first release’s source](examples/release-1.md). The submitted draft says everyone can export a PDF. The release record limits it to invited workspace administrators. The title fix, however, is available to everyone. A checked summary should get both facts right. The writer can send the checker a brief, answer a question and request a revision. The board runs the exchange for you.

3. If the checker caught a mistake, ask on the same card:

   > Follow crew/rituals/retrospective.md using this release. Show me the claim that changed and its source. If an instruction change would have caught it, apply one small edit to your writing method and show me what you changed.

   Inspect the edit in the [writer’s instructions](roles/writer/ROLE.md) or the [checker’s instructions](roles/checker/ROLE.md), wherever the miss happened. If the checker missed the beta restriction, use that miss as the example instead.

4. Start a fresh card with a different release. This makes the saved role instructions available without giving the writer the earlier conversation.

   > @writer Draft release notes from crew/examples/release-2.md. Quote your initial availability claim in the brief to the checker, then return the checked summary and tell me what changed.

   Compare the initial claim and final summary with the [second release’s source](examples/release-2.md). Use the [effect check](rituals/effect-check.md) to record whether the edit helped on this example. The new note should distinguish title search for everyone from attachment search for opted-in workspace owners, with its file limits. If the evidence is missing, ask the writer for it on the original card.

## Give a role your own work

Choose a small project folder inside your workspace. On a new card, start your first message with `@writer ` and give the path, the people who will read the result and the evidence you want checked. Follow-up messages on that card stay with the owner.

To add a specialist, copy the [blank blueprint](blueprints/ROLE.md) to `crew/roles/<role-id>/ROLE.md`. Fill in the job, where to find inputs, what the role may do, what it should return and one task you can use to try it. Add the role to [crew.json](crew.json) following the existing entries, and list it as a collaborator for each owner that can delegate to it. Try a new card addressed to `@<role-id>` and inspect its first result.

Roles share the workspace and use the host permissions of the runner. Give them material you are comfortable making available to that runner. Keep a source change and a personal preference distinct. For instance, “available only to beta users” belongs with the release evidence. “Put availability in the first sentence” is a preference you can record with the [preference ritual](rituals/preferences.md). The rituals run when you ask.
