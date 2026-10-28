# Orchestration efficiency on the NAS: measurements and Crew Flow rules

Measured 2026-10-27 from local Claude Code transcripts, to turn "as efficient as possible while as strong as
possible" into concrete rules for Crew Flow (see `../CREW-FLOW.md`). Aggregates only; no prompts or tool I/O.

## Method and data

- **Sessions** (orchestrator transcript + all `subagents/agent-*.jsonl`): Jamfin (9724ad2b), NervOS (a36f6eb3),
  Home Assistant Mr Manager (d9b15887), Crew Hub (a6de6a12). 4 orchestrators, 119 workers, 10,587 API requests,
  2026-10-25..28 (Mr Manager also has 4 workers from 10-22). The analysis agent itself is excluded. Mr Manager and
  Crew Hub were still running, so their numbers are a snapshot.
- **Parsing**: assistant entries deduplicated by `message.id`; tokens from `message.usage`; tool calls and results
  joined by id. Every request was `claude-opus-5-5`. Orchestrators ran with the 1 h cache TTL and workers with 5 min.
- **Prices** (claude-api skill, cached 2026-10-06), Opus 5.5 per MTok: input $4, output $20, cache read $0.20,
  cache write $5 (5 min) / $8 (1 h). These are **list-price equivalents**. The user's plan bills differently, so use
  the dollar figures for ranking and ratios, not as an invoice.
- **Gap in the data**: 5,745 worker requests (65 %) come from background-agent transcripts that store only the
  start-of-stream usage (output about 8 tokens, no `stop_reason`). Their input and cache counts are complete. Their
  output was imputed as the mean of complete worker requests at the same effort: low 490, medium 779, high 1,106,
  xhigh 1,224 tokens.
- **"Carried" cost** = a content's tokens × (write price + number of later requests × cache-read price). This is
  what a tool result or report costs over the rest of its context's life. It ignores compaction (only 3
  compactions happened, all in orchestrators).
- **Scripts**: `/tmp/claude-1000/-home-node-project/a6de6a12-6acf-59c4-ac42-7c458d21a068/scratchpad/efficiency/`
  (`analyze_lib.py`, `analyze.py`, `analyze2..7.py`, `simulate_split.py`, `simulate_orch.py`; outputs in `out*.txt`).

## Measured numbers

| Session | Orch req | Orch ctx median | Orch $ | Workers | Worker req | Worker $ | Worker share | Cache hit |
|---|---|---|---|---|---|---|---|---|
| Jamfin | 538 | 522K | 88.6 | 33 | 4,077 | 364.2 | 80 % | 98.9 % |
| NervOS | 167 | 289K | 15.0 | 36 | 2,888 | 264.6 | 95 % | 98.5 % |
| HA Mr Manager | 757 | 512K | 145.1 | 32 | 1,137 | 83.2 | 36 % | 97.3 % |
| Crew Hub | 221 | 307K | 26.7 | 18 | 802 | 56.4 | 68 % | 97.6 % |
| **Total** | 1,683 | avg 457K | **275** | 119 | 8,904 | **768** | **74 %** | 98.5 % |

Total ≈ **$1,044**. Split by kind: worker cache reads $435, worker cache writes $159, worker output $175 (partly
imputed), orchestrator cache reads $151, orchestrator writes $97, orchestrator output $27. Uncached input is
under $0.10. Caching works (98.5 % hit rate). What drives cost is **how big contexts get and how many requests
re-read them**.

| Metric | Value |
|---|---|
| Worker cost per request | mean $0.086; average context 248K tokens |
| Orchestrator cost per request | median $0.08-0.13; average context 457K tokens |
| Worker first-request context | median **59.5K** tokens, of which the brief is ~1K |
| Root CLAUDE.md in every worker | 4.4K tokens (7.5 % of the first request); now 19,086 chars ≈ 4.8K tokens |
| Skill listing / deferred MCP tool names per worker | 4.5K / 3.8K tokens (463 names: rea 207, ReVa 86, HA 77, Gmail/Zoom/Drive/Calendar/Docs 82) |
| Nested project CLAUDE.md loads in workers | 89 loads, 350K tokens (jamfin/CLAUDE.md alone is about 12.5K tokens) |
| Requests per worker | median 55, p90 169, max 301; peak context median 209K, p90 467K, max 753K |
| Worker requests at >300K context | 2,697 (30 %), costing **$314** (41 % of worker spend) |
| Tool calls per worker request | 1.16 on average; only 12.6 % of requests made more than one call |
| Requests until a worker's first edit | median 21 ($1.78); total **$191** (25 % of worker spend); 81 % of the files viewed were not named in the brief |
| Worker screenshots (image Reads) | 924 (Jamfin 662), about 2.1-2.8K tokens each |
| Worker wait/poll Bash steps (`sleep`, `until`) | 777 (Jamfin 629, median `sleep 2` between device steps) |
| Worker file viewing via Bash `cat`/`sed`/`head` | 1,419 calls, 1.8K tokens each; text `Read` only 188 calls |
| Briefs | 103 launches, median 863 tokens, p90 1,367, max 1,852 (not oversized) |
| Worker reports back | median 1,158 tokens; 104 of 120 exceed the 300-word cap |
| Orchestrator review per handback | median 3 requests ($0.32), $83 in total across 169 handbacks |
| Orchestrator review reads | 31 `git diff --stat`, 3 full diffs, 12 text-file reads, 62 Bash file views, 87 screenshots |
| Orchestrator idle cache rebuilds | 23 rebuilds after gaps of 61+ minutes (1 h TTL expired), **$71** |
| Worker cache expiries | 14 rebuilds after waits of about 10 minutes (5 min TTL), $21 |
| Plan-file and board writes | 76 plan writes (74 heredoc, 2 Write) + 21 ArtifactData + 2 savvy-progress: about 19K output tokens, **≈ $0.95 in total** |
| Per tier: $/request, output/request, average context | heavy 0.103 / 1,222 / 319K; careful 0.083 / 1,120 / 219K; medium 0.062 / 830 / 138K; light 0.048 / 490 / 92K; untiered general-purpose 0.088 / 871 / 264K |
| Orchestrator effort | Jamfin, Mr Manager and Crew Hub ran at **medium**; NervOS at high |
| Quality: return rounds | 20 return messages to 15 of 119 workers, so **≈ 87 % accepted first time**; returns listed 2-19 fix points |
| Mid-run scope relays | 34 messages to 19 running workers (user feedback or scope added) |
| Parallelism | max 3-5 concurrent workers; 3 files edited by two overlapping workers (Jamfin) |

## Top waste sources (ranked by $, categories overlap)

1. **Long-lived worker contexts: $314** for requests above 300K context. Tasks run 100-300 requests; scope gets
   relayed into running workers; returns go to the same worker. Cost grows roughly with the square of task length.
2. **Device and UI loops step by step: ≈ $180-200.** Screenshot reads carry $30-40 plus $67 for the turns that read
   them. Wait/poll steps cost $73 (+$2 carried, $0.094 each), and adb steps $17. Mostly Jamfin TV testing: one
   key press, `sleep 2` and a screencap per turn.
3. **Re-orientation before the first edit: $191.** A median of 21 requests, mostly viewing files that the brief
   didn't name.
4. **Fat orchestrator contexts: ≈ $150 cache reads + $71 idle rebuilds.** The average is 457K tokens. Hands-on work
   in the orchestrator is the main cause: Mr Manager made 399 HA MCP calls inline (221K tokens, $24 carried plus
   ≈ $50 of turns). 87 screenshots also sit in orchestrator contexts.
5. **Whole-file views via Bash: ≈ $57 carried + $103 of turns.** These are `cat`/`sed` of entire files, plus mostly
   one tool call per turn.
6. **Static worker start-up: ≈ $126.** The 58K-token preamble is written once and re-read on every request. About
   25K of it is removable (CLAUDE.md, skill listing, unused tool schemas, MCP names).
7. **Cache expiry during waits: $21.** Workers blocked 10 minutes on builds or sleeps; the 5 min TTL expired.
8. **Long reports: about $6-8 carried.** Small in dollars, but it inflates every later orchestrator turn.
9. **Plan-file rewrites: about $1.** Negligible. Crew Flow's TaskCreate design is worth it for robustness and for
   removing the rule from CLAUDE.md, not for tokens.

Not observed as waste: oversized briefs, relaunching the same task, and identical re-reads inside one worker (0).

## Rules for the Crew Flow skill and agents

Savings are estimates against this window's $1,044 and overlap. Taken together, expect roughly 30-40 % less
spend with the same or better review depth.

1. **Size tasks by context, not by feature.** A worker targets ≤ 80 requests and ≤ 200K context. Split work as
   investigate → implement → verify. Unrelated scope goes to a **new** worker, not a relay into a running one;
   relay only small additions to the files the worker already owns. Set `autoCompactWindow` (agent frontmatter,
   verified; tokens, about 60K-1M) to ~250K as a safety net.
   *Evidence*: 41 % of worker spend came from requests at >300K. Simulated cap at 80 requests: −$151 in reads,
   +$21 in fresh starts. *Saving*: **$70-130 (7-12 %)** after ~$1 of re-orientation per split.
2. **The brief pre-locates the work (template below).** It names the files the worker owns, the read-only context
   as `path:line` or symbol, 3-8 facts the orchestrator already established ("don't re-derive"), the exact
   build/test commands, a request budget and the definition of done.
   *Evidence*: 81 % of files viewed before the first edit were unnamed; orientation cost $191. Briefs are only
   ~900 tokens, so there is room. *Saving*: **≈ $60 (6 %)** if orientation drops by a third.
3. **Device and UI verification is scripted, not stepped.** One Bash script runs the whole key sequence, saves
   screenshots downscaled to ≤ 1000 px or cropped to the region, and greps logcat or a UI dump into a few text
   lines. The worker then views only the 1-2 images that decide pass or fail. Reusable scripts live in the repo
   (for example `tools/tv-check.sh`).
   *Evidence*: 924 screenshots and 777 `sleep`-between-steps turns, ≈ $180-200. *Saving*: **≈ $100 (10 %)**.
4. **Read discipline plus batched calls in workers.** Find with `grep -n` first, then `Read` with offset/limit.
   Never `cat` a file over ~150 lines. Put independent reads, greps and status checks in **one** turn as parallel
   calls.
   *Evidence*: 1,419 whole-file Bash views at 1.8K tokens; 87 % of turns carry one call; each turn costs ~$0.086.
   *Saving*: **$50-100 (5-10 %)**, i.e. 10-15 % fewer worker requests plus smaller results.
5. **Lean worker context (verified in Claude Code 2.1.311).**
   - Agent frontmatter: `omitClaudeMd: true` ("runs without the user, project and local CLAUDE.md … when spawned as
     a subagent"; plugin agents parse it too).
   - An explicit `tools:` list, e.g. `Bash, Read, Edit, Write, ToolSearch, TaskStop, Monitor`. Add `WebFetch` and
     `WebSearch` only in a research variant, and the HA MCP server only in an HA variant. Workers used only Bash,
     Read, Write, Edit, ToolSearch, Web*, HA MCP, TaskStop and Monitor. Artifact, the remote-session tools,
     Gmail/Zoom/Drive and ReVa had ≤ 3 uses or none at all.
   - Leaving `Skill` out also drops the 4.5K skill listing. The code returns no listing when the Skill tool is
     absent. `disallowedTools` accepts MCP server-level specs (`mcp__rea`, `mcp__ReVa__*`).
   - Put 6-10 lines of ground rules in the agent body instead of the 4.8K CLAUDE.md: secrets, never destructive
     actions without the orchestrator, ask before device flashing, where builds live.
   - Not usable for this: `--setting-sources` and `--bare` exist but are session-level CLI flags. They don't apply
     to Agent-tool workers, and `--bare` needs an API key instead of OAuth.
   - *Evidence*: first request 59.5K, of which ~4.4K is CLAUDE.md, 4.5K skills, 3.8K MCP names, and an estimated
     10-15K unused tool schemas. A built-in Explore agent with `omitClaudeMd` and a narrow tool set started at 30K (on 10-11, with fewer
     MCP servers installed, so only a rough reference).
   - *Saving*: **≈ $45-60 (5 %)**, including $9 of nested project CLAUDE.md loads (re-measure; nested-load behaviour
     under `omitClaudeMd` is unconfirmed). Check the first-request size on the dashboard after the change.
6. **Keep the orchestrator lean.** Target ≤ 250K context: at each plan boundary, write a handoff (plan, decisions,
   open points) and `/compact` or start a fresh session. Keep screenshots, MCP dumps and full files out of the
   orchestrator; ask the worker for a two-line verdict and a file path.
   *Evidence*: average 457K per orchestrator request; 23 rebuilds after idle gaps ($71). Simulated 250K cap: −$75
   reads, −$31 rebuilds. *Saving*: **$80-100 (8-10 %)**.
7. **When NOT to delegate.** A delegation has a fixed cost of about $0.65: worker start-up write ≈ $0.30, brief
   ≈ $0.02, orchestrator review ≈ $0.32 (median 3 turns). Work that needs ≤ 4 tool calls with small outputs stays
   in the orchestrator. Five or more calls, or any bulky output (dashboards, logs, images, MCP config dumps), gets
   delegated even when it is "simple", because each orchestrator turn costs $0.08-0.13 against $0.03-0.06 for a
   young worker.
   *Evidence*: 399 inline HA MCP calls in Mr Manager. *Saving*: **≈ $30-50** in that kind of session.
8. **Review = diff first, then targeted reads.**
   - Run `git diff --stat <base>..HEAD`, then `git diff <base>..HEAD -- <owned paths>`, and read only the hunks
     whose context is unclear.
   - Re-run the cheapest decisive check yourself.
   - For UI work, look at one or two downscaled screenshots, not the whole series.
   - *Evidence*: reviews today leaned on reports and screenshots: 3 full diffs and 12 file reads for 119 tasks,
     against 87 screenshots. A typical task diff is a few thousand tokens, ≈ $0.05-0.10 including carry.
   - *Effect*: **stronger review at about the same cost**. This rule buys quality, not savings.
9. **Tier and effort guidance (with evidence).** Effort is not the main cost driver: xhigh writes ~47 % more output
   per request than medium, but output is only ~23 % of worker spend; context length dominates.
   - Pick the tier by the kind of difficulty, then cap the task (rule 1).
   - Heavy is for investigation and unclear root causes. It returns a brief or a fix and stays ≤ 100 requests. It
     is never for "run it and check": one verification task at xhigh took 229 requests ($13). Verification is
     Medium with a script (rule 3), or Light.
   - Never launch untiered `general-purpose` workers: 49 of them cost $385, the largest bucket, with no tier
     choice.
   - Run the **orchestrator at high effort**. 3 of 4 ran at medium, the Opus 5.5 default. Orchestrator output was
     2.6 % of spend, so high costs about 1-2 % more and strengthens planning and review. Set it at session start,
     because changing effort mid-session invalidates the cache.
10. **Waits never cross the cache TTL.** No single blocking call longer than ~4.5 min in a worker (5 min TTL);
    orchestrators don't idle-poll. For long builds or tests, use `run_in_background` plus Monitor, or one bounded
    `until` loop (timeout ≤ 270 s). Never chain short sleeps across turns.
    *Evidence*: 14 worker rebuilds after ~10 min waits ($21); 777 wait turns at $0.094. *Saving*: **≈ $20-40**.
11. **Report cap: ≤ 200 words.** Result, files (one line each), verification command and outcome, open questions.
    Longer details go into a file (e.g. `.crew/reports/<task>.md`) that the orchestrator reads only on need.
    *Evidence*: median 1,158 tokens, and 104 of 120 reports broke the 300-word cap. *Saving*: **$5-8**, plus
    smaller orchestrator contexts (helps rule 6).
12. **Returns and relays.**
    - Return to the same worker only while its cache is warm (≤ 5 min after handback) or for small fixes.
      Resuming a cold worker rewrites its whole context, about $1-2 at 300K.
    - Larger returns go to a fresh worker with the defect list and the diff range.
    - Cap at two returns per task (savvy-flow keeps this; observed 87 % first-time acceptance).
    - *Evidence*: 2 of the 14 worker rebuilds followed a handback.
13. **Parallelism with ownership.** Run tasks in parallel only when their "Owns" file sets are disjoint; otherwise
    chain them or use `isolation: worktree`. 3-5 concurrent workers is enough here. Don't poll workers; wait for
    their handbacks.
    *Evidence*: max concurrency 3-5; 3 overlapping-file edits in Jamfin.
14. **Tracking through actions the model takes anyway** (CREW-FLOW proposal). Use TaskCreate/TaskUpdate on state
    changes only, with the Agent description equal to the task title. Then drop the plan-file rule from the root
    CLAUDE.md: it costs every NAS session and worker part of 4.8K tokens per request.
    *Evidence*: today's plan writing costs only ≈ $1, so the value is robustness and the shorter CLAUDE.md, not
    output tokens.

### Brief template (target 400-1,000 tokens)

```
Task <id>: <title>                    Tier: <tier>   Budget: ~<N> requests, stop and report if exceeded
Goal: <one sentence>.  Done when: <observable checks>.
Owns (may edit): <paths>.  Read-only context: <path:line / symbol, why it matters>.
Known facts (don't re-derive): <3-8 bullets the orchestrator already verified>.
Constraints: <only the project rules that bite here>; build: <cmd>; test: <cmd>.
Verify: <exact commands>; UI: run <script>, view only <1-2 screenshots>.
Report (<=200 words): result / files / verification / open questions. Details -> .crew/reports/<id>.md
```

## What to show on the dashboard (zero tokens: crew-hubd reads `transcript_path`)

- **Per agent card**:
  - current context (last request's input + cache write + cache read) with amber at 200K and red at 300K;
  - requests so far against the brief's budget;
  - cumulative list-price $;
  - cache hit %;
  - first-request size (start-up overhead);
  - counts of screenshots, wait turns and whole-file views;
  - tier and effort;
  - return rounds.
- **Orchestrator**:
  - context gauge (amber 250K, red 400K; "compact or hand off" hint);
  - minutes since the last request against the TTL (1 h orchestrator, 5 min workers) as a "cache warm / cold" dot;
  - review cost per handback.
- **Per plan**:
  - orchestrator vs worker $ split;
  - $ per accepted task;
  - first-time acceptance rate;
  - top 3 agents by $.
- **Data caveat for the hub**: background-agent transcripts may hold only start-of-stream usage. Use input and
  cache fields as exact, and show output as "≈" (estimated from effort) unless a `stop_reason` entry exists.

## Caveats

- **Dollar figures are list-price equivalents**, and worker output is partly imputed (see Method).
- **Categories overlap.** A screenshot turn can also be a wait turn or part of orientation, so don't add the
  waste rows together.
- **The quality proxies are coarse.** "Returned" means the orchestrator sent fixes; defects that review missed are
  invisible here.
- **The tool-schema size of ~10-15K is an estimate** (chars/4 of the definitions a worker sees). Measure the
  first-request context before and after rule 5.
