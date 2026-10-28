# Agent memory + an Obsidian vault: research and recommendation

2026-10-27 · question from Isaac: "an Obsidian server to help manage memory for agents — more insight, as efficient and
as strong as possible." Nothing was installed or changed for this note.

## TL;DR

Do not run an Obsidian *server*. Make a **plain Markdown vault** (`~/project/vault`, a git repo on Gitea) that agents
read with their normal file tools and Isaac opens in Obsidian. Shrink the always-loaded CLAUDE.md files to short
indexes that point into the vault. Let **crew-hubd write the agent-outcome log** into the vault with zero model tokens.
Add Obsidian Sync (headless on the NAS) for phone access only if Isaac wants it. Add an Obsidian MCP only for a specific
need that files can't cover.

## What we have today (measured 2026-10-27, tokens ≈ chars / 4)

| File | Size | When it loads |
|---|---|---|
| `~/project/CLAUDE.md` (root) | 19,146 chars ≈ **4.8K tokens** | every NAS session at launch, and again for each subagent that loads project instructions |
| auto-memory `MEMORY.md` + 3 topic files | 416 chars index (≈100 tokens); topics 3.4K chars | index every session; topics on demand |
| per-project CLAUDE.md (13 files) | 182,661 chars ≈ **45.7K tokens** in total | each one lazily, the first time a file in that folder is read |
| … largest: jamfin 52K (≈13K tok), retro-frontend 34K (≈8.5K), home-assistant 27K (≈6.7K), aero15 25K (≈6.3K), echo-show-kiosk 17.7K (≈4.4K) | | |

The root file mixes four kinds of content: (1) **rules** (ground rules, never print secrets, ask-before list),
(2) **environment facts** (hosts, IPs, paths, mounts, tools), (3) **runbooks/findings** (TV adb ports, audio device
codes, USB passthrough), and (4) **history/status** (dated "2026-10-xx: did X" lines for ~15 projects, retired
items such as the Crew Board, the removed AI-stack table). Only (1) and a one-line summary of (2) need to be in
every session. (3) and (4) are what the vault is for, and (4) is also what goes stale.

How Claude Code loads these (primary source: https://code.claude.com/docs/en/memory):
- CLAUDE.md files above the working directory load in full at launch. Subdirectory CLAUDE.md files "load on demand"
  once Claude reads a file in that folder. The docs recommend "under 200 lines per CLAUDE.md file. Longer files
  consume more context and reduce adherence."
- `@path` imports "help you organize a long file but don't reduce its context cost". So imports are **not** the fix.
  Path-scoped `.claude/rules/` (with `paths:` frontmatter) do load lazily.
- Auto memory loads "the first 200 lines of MEMORY.md, or the first 25KB". Topic files are read on demand. It is
  **machine-local**: "Files are not shared across machines", so the Macs never see the NAS memories.
- Anthropic's context-engineering guidance makes the same point: keep "lightweight identifiers (file paths …)" in
  context, load the content just in time, and use structured note-taking (a "NOTES.md file") for long-horizon memory.
  Recall falls off as the context grows ("context rot").
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

## Ways to put Obsidian behind agents (state in Oct 2026)

- **Obsidian desktop app as a server**: there is no official headless Obsidian. The only way to run it on a server is
  the Electron GUI in a container streamed to a browser (linuxserver/obsidian, Selkies). It runs privileged, with no
  auth by default and a passwordless-sudo terminal in the web UI. https://docs.linuxserver.io/images/docker-obsidian/
  It would also need a NAS app change by Isaac and space on The Holodeck (~19 GB free).
- **Local REST API plugin** (coddingtonbear, about 3.1k stars, active): runs *inside* a running Obsidian, HTTPS :27124,
  bearer API key. It now ships a **built-in MCP endpoint** (`/mcp/`, Streamable HTTP) with tools to list, read, write,
  append, patch, move and delete files, plus search, tags, commands and open-file. It warns that a heavy MCP result can
  crash the renderer and take the server down. https://github.com/coddingtonbear/obsidian-local-rest-api
- **mcp-obsidian** (MarkusPfundstein, about 4.5k stars): 7 tools (list, get, search, patch, append, delete) on top of
  that plugin. It needs Obsidian open, and it breaks with the 2.0 `mcp` SDK. https://github.com/MarkusPfundstein/mcp-obsidian
- **Obsidian CLI** (official, 1.12.0, Feb 2026): search, read, create, append, daily:*, backlinks, tasks. "Obsidian CLI
  requires the Obsidian app to be running." https://obsidian.md/help/cli · https://obsidian.md/changelog/2026-02-10-desktop-v1.12.0
- **Obsidian Headless** (official, `npm i -g obsidian-headless`, binary `ob`, Node 22+, since 2026-02-27): **sync
  only**, no API. It needs an Obsidian Sync subscription and uses the same E2E encryption. Running it as a service is
  documented (systemd/launchd). The NAS already has Node 24 in ~/.local/opt/node24. https://obsidian.md/changelog/2026-02-27-sync
  · https://mprokopov.pages.dev/posts/obsidian-headless-sync-systemd/
- **Filesystem MCP** (reference server): 13 generic file tools limited to allowed directories. Claude Code already
  has Read/Write/Edit/Grep, so it adds nothing here. https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
- **Sync between devices**:
  - git with obsidian-git: good on desktop. On mobile it is "highly unstable", with no SSH and crashes on clone/pull.
    https://github.com/Vinzent03/obsidian-git
  - Self-hosted LiveSync (CouchDB, S3 or WebRTC P2P, E2E): powerful, but it is another server to run, and it must not
    be mixed with other sync tools. https://github.com/vrtmrz/obsidian-livesync
  - SMB mount of the NAS folder: only works at home, and file-watching over SMB is unreliable for a live app (general experience, not verified here).

### MCP token cost, checked against the docs
Claude Code 2.1.x defers MCP tools by default: "Only tool names and server instructions load at session start". Full
schemas load through ToolSearch when needed. https://code.claude.com/docs/en/mcp (section "Scale with MCP tool search")
So an Obsidian MCP costs roughly **100–300 tokens per session** (names plus any server instructions, capped at 4,096
chars), plus one ToolSearch round-trip (~1–2K tokens of schemas) the first time it's used. The always-loaded cost
isn't the problem. These are:
- **Availability**: every Obsidian MCP needs a running desktop Obsidian, which the NAS container can't host.
- **Duplication**: its read/search/append do the same job as Read/Grep/Edit, which agents already use well.
- **Big results**: tool output is capped at 25K tokens by default, with a warning above 10K. A file read is as
  selective as `grep`/`offset` makes it.
Server instructions stay in context even with deferral (this session carries the home-assistant and rea instructions
on every turn). Keep that in mind before adding any server.

## Options compared

| Option | Setup effort | Tokens / session (always-on) | NAS + roaming Macs | Insight for Isaac | Main risks |
|---|---|---|---|---|---|
| **A. Today**: big CLAUDE.md + machine-local auto-memory | none | ≈4.9K root, plus 2–13K per project touched | NAS only; Macs have their own CLAUDE.md and memory | low: history buried in CLAUDE.md, no links, no browsing | stale lines, lower adherence, no cross-machine memory |
| **B. Plain Markdown vault + git (Gitea)**, slim index, on-demand notes | low (a folder, a repo, a few rules) | ≈1.5K root index + ≈0.1K MEMORY.md | NAS writes directly; Macs `git pull/push` (offline-first, syncs when home or through a remote) | high on desktop: Obsidian over the clone gives graph, backlinks, history and diffs | merge conflicts (avoided by append-only per-machine logs), secrets slipping in (pre-commit scan) |
| **C. B + Obsidian Headless Sync on the NAS** | low–medium (subscription, `ob` as a background job) | same as B | adds phone and anywhere access, E2E encrypted | **highest**: phone + Mac, live | paid; two sync paths on one folder (git for history, Sync for devices) must not fight; keep `.git/` out of Sync |
| **D. B + self-hosted LiveSync** | medium–high (CouchDB app on NAS, reverse proxy) | same as B | yes, including phone | high | another server to run; don't mix with other sync; CouchDB on the full Holodeck pool |
| **E. Obsidian desktop in a container + Local REST API MCP** | high (NAS app change, privileged GUI container) | ≈0.2–0.3K deferred, plus schemas on first use | NAS only, and only while the GUI app runs | high, but through a browser-streamed desktop | privileged container, no auth by default, Electron crashes, ~GBs of RAM/disk, NAS-admin dependency |
| **F. Mac Obsidian + REST API/MCP; NAS agents call it over the LAN** | medium | ≈0.2–0.3K | breaks whenever the Mac roams or sleeps | high | NAS agents depend on a laptop; API key handling |

## Recommendation: B now, C if the phone matters, MCP only for a named need

**Phase 1: plain vault, slim index (about a day of agent work, no new services)**
1. Create `~/project/vault/` as a git repo with a remote on Gitea (`starkkaman/vault`, private, deploy key with write
   for the NAS; the Macs use their own keys). Layout:
   - `INDEX.md`: at most 60 lines; one line per host, project and runbook, each with a link.
   - `hosts/` (nas, hisense-tv, ipad, echo-show, phones …): IPs, access, gotchas.
   - `runbooks/` (adb reconnect, USB serial, TV audio codes …).
   - `projects/<name>/status.md` (current state, with `updated:` and `status:` frontmatter),
     `projects/<name>/decisions.md` (append-only, dated ADR lines), `projects/<name>/history.md`.
   - `log/YYYY-MM-DD-<machine>.md`: generated, see below.
   - `vault/CLAUDE.md`: writing rules (frontmatter, one fact per note, no secrets, link instead of copy). It lazy-loads
     only when an agent touches the vault.
2. **Move, don't copy**: cut sections (3) and (4) out of the root CLAUDE.md into `hosts/`, `runbooks/` and
   `projects/*/history.md`. The root keeps the ground rules, a 10-line host summary and "where to look"
   (`vault/INDEX.md`, read it when the task touches a host or project). Target ≈6K chars. Do the same for the large
   project CLAUDE.md files: keep current state + repo rules (≤8K chars), move dated history to the vault.
3. Keep auto-memory for small, machine-local preferences (it's already lean). Put cross-machine facts in the vault.
4. Staleness rules (in `vault/CLAUDE.md`):
   - every status note carries `updated:`;
   - agents verify a fact older than ~14 days before acting on it, and fix or date-stamp it;
   - superseded facts move to `history.md` instead of piling up "Since …" qualifiers.
   Obsidian's core **Bases** view over the frontmatter gives Isaac a "stale notes" table without plugins (https://obsidian.md/help/bases).
5. Isaac on the Mac: open the cloned vault in Obsidian. obsidian-git (desktop) can auto-pull/commit, or he just reads.

**Phase 2: phone and anywhere access (only if Isaac wants it)**: Obsidian Sync with `ob sync --continuous` on the NAS
(Node 24 is already there; run it as a crew-hubd-style background job, since the container has no systemd). Phone and
Macs use the normal Obsidian Sync. Git stays as the **history** layer, committed only from the NAS, with `.git/`
excluded from Sync. Then the Macs no longer need git for the vault. Avoid git on the phone (obsidian-git warns against
mobile use). Alternative if Isaac won't pay: LiveSync (option D), at the cost of a CouchDB app.

**Phase 3: MCP, only when a concrete need appears** (for example Obsidian-only features like Bases queries or
`daily:append` from a Mac session). Then use the Local REST API's built-in MCP on a Mac that is running Obsidian,
scoped to that Mac's sessions, never as a NAS dependency. Revisit if Obsidian ships a headless API.

## Token budget: recommendation vs today

| | Today | After phase 1 |
|---|---|---|
| Always loaded per NAS session (root CLAUDE.md + MEMORY.md) | ≈4.9K | ≈1.6K (−3.3K) |
| Per savvy-flow plan with ~4 workers that load project instructions | ≈19.6K | ≈6.4K (−13K) |
| Touching jamfin/ (lazy CLAUDE.md) | ≈13K | ≈2K (−11K) |
| Touching retro-frontend / home-assistant / aero15 | ≈8.5K / 6.7K / 6.3K | ≈2K each |
| On-demand vault reads when relevant | 0 (it's all preloaded) | +0.5–4K (1–3 notes) only in tasks that need them |
| Obsidian MCP (if ever added, deferred) | – | +0.1–0.3K always, plus ~1–2K on first use |

Prompt caching makes a repeated prefix cheaper in money, but it still uses up the context window and, per the docs,
lowers adherence. Smaller is better on both counts. The phase-1 savings are an estimate (target sizes, chars/4).
Measure with `/context` before and after.

## Zero-token agent outcomes into the vault (Crew Hub / Crew Flow)

crew-hubd already folds the hook events and plan files without any model calls (docs/DESIGN.md). Extend it with an
optional **vault writer**. No LLM is involved, so this costs no tokens:
- **Per-machine daily log**: append to `vault/log/YYYY-MM-DD-<machine>.md`, one line per event that matters:
  - session started/ended (title, project folder from `cwd`);
  - plan finished: plan title, done/total, and each task's `title`, `tier`, `status` and `summary` from
    `~/.claude/crew-hub/plans/<id>.json` (the orchestrator already writes those summaries);
  - agent failed or stopped; permission request answered from HA.
  Per-machine files make every log append-only with one writer, so git never conflicts.
- **Project rollup**: when a plan's `cwd` is under `~/project/<p>/`, also append a one-line link
  (`[[log/2026-10-27-nas#plan-…]]`) to `projects/<p>/history.md` so the project page shows its agent history.
- **Commit**: crew-hubd commits `vault/log/` itself, at most every 15 min and on day rollover, as "crew-hub log".
  Pushing to Gitea is optional and can use the existing deploy-key pattern. Macs (webhook transport) write their own
  `-macbook`/`-macmini` files in their own clone.
- **Redaction**: reuse the DESIGN.md source rules (names, descriptions, statuses, timings, token counts; never tool
  inputs, prompts or briefs). Pass free-text plan summaries through the same secret-pattern filter before writing,
  because a summary is the one field a model wrote.
- Insight for Isaac: Obsidian's daily-notes and calendar view over `log/`, backlinks from each project page,
  and git history as an audit trail. The live view stays the HA "Crew" dashboard. The vault is the durable record.

## Fleet fit and hygiene

- **NAS sessions**: write the vault directly (`~/project/vault`). No mount or sync is needed.
- **Macs**: a git clone. Off-LAN they work offline and sync when home, unless Gitea is reached through the existing
  reverse proxy or a VPN (Isaac's call). In phase 2, Obsidian Sync covers this.
  - A Mac's `~/.claude/CLAUDE.md` can hold a one-line pointer to its clone.
  - Avoid an `@` import of the whole INDEX, because imports load in full.
- **GitHub as the remote** instead of Gitea works from anywhere. It is only acceptable if the vault really holds no
  secrets, and IPs and device names are still personal data. Prefer Gitea.
- **Conflicts**:
  - append-only, per-machine logs;
  - one owner per status note (the project's session);
  - `git pull --rebase` before writing;
  - never two sync systems writing the same files.
- **Secrets**:
  - the vault never holds credentials: MQTT/HA passwords, the ha-mcp `/private_…` path, command secrets, pairing
    codes, keys, adb pairing codes;
  - notes say *where* a secret lives (`~/.claude/crew-hub/secrets.json`, "ask Isaac"), never its value;
  - add a pre-commit secret scan (for example gitleaks) and a `.gitignore` for `.obsidian/workspace*`;
  - the same "never print config files" memory rule applies to vault notes.

## Open questions for Isaac

1. Is phone access worth an Obsidian Sync subscription (phase 2), or is the Mac enough?
2. Vault remote: Gitea (LAN only unless proxied) or a private GitHub repo?
3. Should the migration of the root CLAUDE.md into the vault be a savvy-flow plan (one task per section, with a
   review that nothing was lost)?
