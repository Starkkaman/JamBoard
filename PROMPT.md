# Crew Hub: starting prompt

You own a new project, **Crew Hub**: a Home Assistant hub that shows live progress for every Claude Code agent I run,
lets me talk to those agents and start new projects from Home Assistant, and ships as a Claude Code plugin so any
machine can join with little setup.

Start by reading `/home/node/project/CLAUDE.md` (home-lab context and ground rules), `/home/node/project/crew-hub/reference/FINDINGS.md`
(what's already verified and what's a dead end) and `/home/node/project/crew-hub/reference/crew-board-v4.html` (the look to match).
Work in `/home/node/project/crew-hub/`: make it a git repo with its own short CLAUDE.md, and keep the main CLAUDE.md's
Crew Hub entry up to date.

## Goals
1. **Dashboard that looks nearly exactly like the Crew Board** (https://claude.ai/artifact/SpbMfJSCGoAKGAZsNwtjDc), inside
   Home Assistant and usable in the HA mobile app away from home. It needs:
   - Jamfin's Cut Paper night palette and fonts, a plan strip with a red progress bar and phase chip, and queued tasks.
   - One block per session: status chip, summary, model, cost, context bar.
   - Agent cards: a live current step with a pulsing dot, elapsed time, step count, the last 3 steps, and progress bars.
   - It must update within seconds, so work out how to get live updates.
   - Probably a custom Lovelace card (JS/TS, served as a dashboard resource) fed by HA entities or events. Choose and justify.
2. **A hub for all my agents:**
   - Every Claude Code session on the TrueNAS box (Remote Control sessions in the `claude` app container) and its subagents.
   - Easy to add sessions from other machines (Mac, Linux boxes). Recommend the transport (HA webhook through my HA
     remote URL, MQTT, or something else) with security and offline buffering in mind.
   - Tasks that haven't started yet (plans and todo lists) must show too.
3. **Interaction from the dashboard:**
   - Send a message to a running session or agent.
   - Answer a session that's waiting on me.
   - Stop or interrupt one.
   - Start a new project/session (pick machine, folder and model; type the first prompt).
   - Find out what's actually possible to deliver into a running Claude Code session from outside, and verify it before building on it.
     Candidates: a per-machine bridge using the `claude` CLI's background agents (`claude agents`, `attach`, `logs`,
     `respawn`, `stop`), or a small relay session that uses the Claude Code Remote MCP tools
     (send_message, create_session, interrupt_session, list_sessions).
   - Every action needs HA authentication. No unauthenticated command endpoints, and no arbitrary shell commands from HA.
     Use an allowlist of actions, with a confirm step for destructive ones.
4. **A Claude Code plugin** (hooks + a setup skill/command) so a new machine joins with roughly one install and one config step:
   - hooks that publish session, agent and subagent start/step/finish and plan/todo events to HA, at zero tokens
   - the bridge for goal 3
   - a machine id and per-machine credentials
   - packaged as a plugin marketplace repo I can add with `/plugin marketplace add`
   - Ask me where to host it (GitHub Starkkaman or my Gitea).

## Constraints
- **Token efficiency is the point:**
  - Reporting must cost the agents zero tokens (hooks, not tool calls, and no extra brief text).
  - The dashboard must not need any model calls to stay current.
- **Home Assistant:**
  - Work with the **"Home Assistant Mr Manager"** session (SendMessage) for anything inside HA. It knows my HA setup, the
    Astrion remote/RosCard dashboards and the HA conventions.
  - HA changes need my OK. Batch them into one clear proposal before applying.
  - Never copy the ha-mcp secret path, MQTT passwords or webhook IDs into git or chat.
- **Jamfin integration:** the **Jamfin session ("project-4c")** runs a savvy-flow orchestrator and wants to show its plans and
  agent cards on the hub. Agree an interface with it early, e.g. which event or service it calls when a plan starts or a
  task is accepted. It will also tell you what it has learned about session event shapes.
- **Verify the fields first:** check hook payload fields (subagent ids, descriptions, todo/task events) on Claude Code 2.1.295 with
  a real test session before designing around them. Write down what's verified vs assumed.
- **My ground rules:** ask before NAS app changes, credentials, or anything destructive.
- **Live view of your own work:** keep the existing Crew Board artifact updated with your own work (ArtifactData,
  collection `agents`, doc `status/flow`) until the HA hub can show itself.

## First deliverable (before building)
A short design proposal for me covering:
- the architecture diagram: hooks → transport → HA → card, and HA → bridge → sessions
- the data model, and what's verified vs needs testing
- the plugin's shape and the setup flow for a new machine
- the security model
- the exact HA changes Mr Manager would make

Then build it in small, verifiable steps.
