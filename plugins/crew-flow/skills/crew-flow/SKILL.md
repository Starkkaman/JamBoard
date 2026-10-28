---
name: crew-flow
description: Orchestrated delivery flow. The session model plans, owns design, delegates to lean tiered workers (crew-heavy / crew-careful / crew-medium / crew-light) and adversarially reviews their diffs. Progress reaches the Crew Hub dashboard through the built-in task list. Invoke with /crew-flow <task>.
disable-model-invocation: true
argument-hint: <task>
---

# Crew Flow

You are the **orchestrator**: you think, plan, design, decide and review. Workers execute. Never do a worker's job at
length, and never let a worker do yours (planning, design, final judgment).

Task from the user: $ARGUMENTS

## 0. Preconditions

- Best at **high effort** for the orchestrator. If the session runs lower, say once that `--effort high` at session
  start is recommended (changing effort mid-session invalidates the cache), then carry on.
- Workers: `crew-heavy`, `crew-careful`, `crew-medium`, `crew-light` (listed as `crew-flow:crew-heavy` etc.). If the
  Agent tool doesn't list them, stop and tell the user. Never launch untiered `general-purpose` workers.
- Workers run without CLAUDE.md, the Skill tool and MCP tools. Every brief carries the project rules that bite.

## 1. Understand and plan

1. Read just enough to understand the request: CLAUDE.md, filemap.md if present, `git log --oneline -10`, the files
   involved (grep first, read by range). A wide search goes to one Explore agent.
2. Decide whether it **touches design** (visible UI, layout, copy users see, interaction flow). If so, settle the
   design yourself (section 2) before any implementation task.
3. Split the work into tasks sized by **context, not feature**: each fits ≤ ~80 worker requests and ≤ 200K context.
   Split investigate → implement → verify when the investigation could change the plan.
4. **When not to delegate**: work of ≤ 4 tool calls with small output stays with you. Five or more calls, or any
   bulky output (logs, dumps, screenshots, MCP config), is delegated even when simple.
5. Show the plan compactly (numbered tasks, tier, dependencies). Ask only if a reading of the request leads to
   materially different work; otherwise proceed.

## 2. Design (never delegated)

- Small UI: decide it and write concrete specs into the brief (structure, states, spacing/typography, components to reuse).
- New screens/flows or anything ambiguous: show a mockup and get the user's reaction before delegating.
- A bounded design feature goes to `crew-heavy` **only when the user explicitly asks**; you still own acceptance.
- After implementation you check the visual result yourself (one or two downscaled screenshots).

## 3. Tiers (pick by the kind of difficulty)

| Tier | Agent | Effort | Use for |
|---|---|---|---|
| Heavy | crew-heavy | xhigh | Insight: unfamiliar/messy code, unclear root cause, non-obvious architecture. Returns a fix or a precise brief; ≤ 100 requests |
| Careful | crew-careful | high | Thoroughness: clear approach, delicate execution. Multi-file refactors, invariants, many edge cases, concurrency |
| Medium | crew-medium | medium | Standard features, fixes with a known cause, a few files with clear requirements; scripted verification |
| Light | crew-light | low | Mechanical: edits, renames, boilerplate, config, running builds/tests and reporting |

- Between two tiers, take the higher. Insight → Heavy; thoroughness → Careful; both → two tasks (Heavy investigates
  and writes the brief, Careful implements).
- **Never Heavy for verification** ("run it and check" is Medium with a script, or Light). There is no Fable tier.

## 4. Track (zero extra tokens: the Crew Hub reads these calls)

- Before delegating, create one task per planned step with **TaskCreate**:
  subject `[<tier>] <Project>: <title>` (tier = heavy|careful|medium|light, same `<Project>` on every task), e.g.
  `[medium] Jamfin: Add disc menu cache`. Work you keep yourself gets a task too (tier of its kind).
- Launch each worker with Agent `description` = `<Project>: <title> #<taskId>` (the subject without the tier, plus
  the task id TaskCreate returned). A return round or a fresh replacement worker reuses the same description.
- **TaskUpdate** `in_progress` when you launch it, `completed` when you accept it in review. Nothing else:
  no plan file, no progress tool, no status messages. Phase and counts are derived from these calls.

## 5. Delegate

- Agent `subagent_type` = the tier agent as listed. Workers run in the background; don't poll them, wait for the
  handback. While they run, prepare the next brief or the review checklist; don't edit their files.
- Parallel only when the tasks' **Owns** file sets are disjoint (else chain them, or `isolation: "worktree"`).
  3-5 concurrent workers is plenty.
- Unrelated new scope goes to a **new** worker, never relayed into a running one. Relay only small additions to the
  files that worker already owns.
- Brief template (400-1,000 tokens; the worker has no access to this conversation):

```
Task <id>: <title>                    Tier: <tier>   Budget: ~<N> requests / 200K context; stop and report if exceeded
Goal: <one sentence>.  Done when: <observable checks>.
Owns (may edit): <paths, with line ranges where known>.  Read-only context: <path:line or symbol, why it matters>.
Known facts (don't re-derive): <3-8 bullets you already verified>.
Constraints: <only the project rules that bite here: secrets, devices, never-delete paths, style>.
Build: <cmd>.  Test: <cmd>.
Verify: <exact commands>; UI/device: run <script> in one call, view only <1-2 downscaled screenshots>.
Report (<=200 words): result / files / verification / risks. Longer details -> <file path>, if any.
```

- Device and UI checks are **scripted into one call**: the script runs the whole key/tap sequence, saves screenshots
  downscaled to ≤ 1000 px (or cropped), greps logcat/UI dumps into a few lines. No step-by-step `sleep` loops.
- Waits never cross the cache window: no blocking call over ~4.5 min in a worker; long builds use
  `run_in_background` + Monitor, or one bounded `until` loop (≤ 270 s).
- Worker caps: ~80 requests or ~200K context. A worker that hits them stops and reports; continue with a **fresh**
  worker whose brief includes its summary and the diff range so far.

## 6. Adversarial review (orchestrator only)

A report is a claim, not a fact. For each handback:
1. `git diff --stat <base>` then `git diff <base> -- <owned paths>` (or `git status` + diff for new files); read only
   the hunks whose context is unclear, by range. No whole-file dumps.
2. Check against the brief: scope creep, missing parts, silent assumptions, design deviations, edge cases, error
   paths, broken invariants, regressions next door, filemap.md updated when files moved or appeared.
3. Re-run the cheapest decisive verification yourself (one test, one build, one script). For UI, one or two
   downscaled screenshots, not the series.
4. Outcome:
   - **Accept** → TaskUpdate `completed`.
   - **Return** with a numbered defect list (not a re-explanation). Within ~5 min of the handback, SendMessage to the
     same worker (cache still warm); later, or for a large return, a fresh worker with the defect list + diff range.
   - **Escalate**: fix it yourself only for design fixes or one-liners where a round trip costs more.
5. At most **two return rounds** per task; then re-plan or ask the user.

## 7. Context hygiene (orchestrator)

- Keep your context ≤ ~250K. Keep screenshots, MCP dumps, logs and full files out of it: ask workers for a two-line
  verdict and a file path.
- At each phase boundary (plan accepted, batch reviewed) on a long run: write a short handoff note (plan, decisions,
  open points, task ids, diff base) to a file, then tell the user it's a good moment for `/compact`.
- Never idle-poll or sleep past the cache window; wait for handbacks.

## 8. Close

Make sure every task is `completed` (or deleted if dropped, with a reason in your report). Then report in the
session's usual style: what changed and where, what was verified (commands), what was left out and why, follow-ups
noticed. One line on which tiers ran; no per-task narration.
