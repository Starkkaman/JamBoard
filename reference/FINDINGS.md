# Findings from the Jamfin session (2026-10-26), for the Crew Hub project

## What already exists
- Crew Board artifact (the look to copy): https://claude.ai/artifact/SpbMfJSCGoAKGAZsNwtjDc (v4).
  Source: reference/crew-board-v4.html. Jamfin's "Cut Paper" night palette:
  - ink #141210, slab #1f1c19, paper #ece2c9, dim #9a8f7c, ochre #e5a31b (running), red #d43a1c (blocked/failed), teal #3f9aa5 (done).
  - Fonts: Londrina Solid (display, uppercase) and Jost (body).
  - Layout: a plan strip (title, red progress bar, phase chip, queued tasks), then one block per session (status chip,
    summary, model, cost, context bar), with agent cards inside. Each card shows a live current step (pulsing dot),
    elapsed time, step count and the last 3 steps.
- Data model the board used (artifact db), still useful as a schema:
  - flow{title, phase, done, total, finished, sessionId}
  - agents[{title, tier, status queued|running|done|failed, order, startedAt, agentId, endedAt, summary}]

## Dead ends (verified, don't retry)
- Artifact pages can't call the Claude Code Remote connector: list_sessions fails with `blocked_by_policy` for the page.
- Headless `claude -p` (tested with model claude-haiku-5-5) has no ArtifactData/Artifact tools, so a script can't push to an artifact.
  That test cost ~$0.005, using ~42k cache-read tokens per call.
- The savvy-progress mod (TUI) doesn't render over Remote Control (disabled on the NAS).

## Data sources that work
- Claude Code hooks (zero tokens) in every session on a machine. Verify the exact payload fields on Claude Code 2.1.295.
- Session event stream (seen via the Claude Code Remote MCP from inside a session; system events):
  - `task_started` {task_id, task_type (local_bash, …), description, owned_by_subagent, parent_task_id = the subagent's id}
  - `task_notification` {task_id, status completed|failed, summary}
  - Subagent steps carry plain-English descriptions, e.g. "Sort HDR10 by play count and read first row".
  - `init` events are large (~25 KB each, with tool lists); skip them.
- Session list (Claude Code Remote list_sessions):
  - title, session_status, status_bucket (WORKING/BLOCKED/FAILED/REVIEW_READY/COMPLETED), task_summary,
    post_turn_summary{status_detail, recent_action}, connection_status
  - external_metadata{model, usage.cost_usd, context_usage{used_tokens, max_tokens}}, updated_at

## Environment
- NAS container: user node (uid 1000), TrueNAS 192.168.10.157. No docker socket, no host SSH, no NAS admin.
- Home Assistant: 192.168.5.166:8123, Mosquitto 192.168.5.166:1883, ha-mcp add-on (secret path; never copy it).
  Ask before HA changes; the "Home Assistant Mr Manager" session owns HA work.
- Main CLAUDE.md: /home/node/project/CLAUDE.md
