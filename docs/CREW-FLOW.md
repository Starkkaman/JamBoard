# Crew Flow: zero-token progress tracking (scope added 2026-10-27, not built yet)

Isaac asked for "our own Claude Code plugin that makes it easier to track progress without wasting tokens".

## Problem
- Plans reach the hub via ~/.claude/crew-hub/plans/<session>.json. The orchestrator model rewrites that whole file on
  every change, which costs output tokens on each update.
- The rule that explains the file sits in the main CLAUDE.md, so every NAS session loads it, planning or not.
- savvy-flow is a third-party plugin (claude-kit). Our local edits (no Fable tier) are lost on every update.

## Proposal
A second plugin, `crew-flow`, in the JamBoard marketplace, next to `crew-hub`:
- An orchestrator skill `/crew-flow <task>`, modelled on savvy-flow: plan, delegate to tiered workers, adversarial review.
  Tiers Heavy / Careful / Medium / Light as agents. No Fable.
- **Tracking through actions the model takes anyway**, read by crew-hub hooks at zero extra cost:
  - The plan is Claude Code's built-in task list: TaskCreate with subject `[tier] title`, then TaskUpdate for status.
  - Each worker launch uses the task's title as the Agent `description`. crew-hubd joins agent ↔ task by title (and by
    an explicit `#<taskId>` suffix if present).
  - The phase is derived from events (tasks created → planning, agents running → delegate, agents done but tasks
    still open → review, all completed → finished), so the model doesn't report it.
  - The plan title is the session name, or the first task's prefix (`Project: …`).
- crew-hubd builds `session.plan` from the task list when there's no plan file. The plan file stays supported, so
  Jamfin and existing sessions keep working; the CLAUDE.md rule can then shrink to one line, or go.

## Open points to settle when building
- Exact join rule and how renamed or reused tasks behave.
- Whether workers report a one-line summary via SubagentStop (already captured as `last_assistant_message`), or whether
  the orchestrator's TaskUpdate description carries it.
- Migration: savvy-flow sessions keep using the plan file until they switch.
