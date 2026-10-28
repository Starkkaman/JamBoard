"""State model: folds spool records into the per-machine snapshot of DESIGN.md §2.

Times in the snapshot are epoch milliseconds (ints), like ~/.claude/sessions/*.json. Keys starting with "_" are
internal (pid, transcript path, file signatures) and are never published.

Sessions without hooks still show up through their plan file (~/.claude/crew-hub/plans/<session id>.json, see
poll_plans): such a session carries "_fromPlan" until its first hook event, lives exactly as long as the plan file, and
takes its status from ~/.claude/sessions/*.json while that process is alive (idle otherwise).

Agent lifecycle (0.4.0): running -> done (SubagentStop, SessionEnd) | stopped. An agent is "stopped" when its session
restarts while it runs (SessionStart with source startup/resume/clear/unknown, not compact; or the registry entry
moving to a new process) with stopReason "session restarted", when the session stops it with TaskStop ("stopped by
session"), or when it made no step for AGENT_IDLE_MS while its session is idle or ended ("inactive"). A step revives a
"session restarted"/"inactive" agent; SubagentStart revives any agent. Finished agents (done/failed/stopped) disappear
FINISHED_TTL_MS after endedAt and at most MAX_FINISHED of them are kept per session; untitled agents without steps are
dropped once finished. Plan task statuses are never touched (the plan file is the orchestrator's truth).

session.name is human (display_name): registry name when nameSource is user/auto, else the newest non-derived former
name, else the plan title's prefix before ":", else the cwd basename plus the local name. session.localName keeps the
registry name (e.g. "project-aa").

Waiting (0.4.1) means "a prompt is open right now". Sources: open requests `_reqs` (PermissionRequest records with a
requestId), a request-less prompt `_note` (Notification permission/question, or a PermissionRequest from an older
crew-hook), and the live registry entry (~/.claude/sessions/<pid>.json status "waiting" + waitingFor). Rules:
- Live registry says waiting: waiting (text from the newest request, else the note, else waitingFor).
- Live registry says busy/idle/shell: a request stays only while crew-hook still holds it and the registry status didn't
  change after the request (REQ_RESOLVED_SLACK_MS), or for REG_LAG_MS after it arrived (registry write lag); a note
  only for REG_LAG_MS. Everything else is dropped.
- No live registry entry: any progress event of the session (agents included) ends the waiting, except for a request
  crew-hook still holds (only its own clears apply then); "unanswered" or a hold that ran out drops a request; requests
  expire after NOREG_MAX_MS, notes after NOTE_MAX_MS.
- hubCanAnswer is true only while crew-hook holds the request (hold not over, "answered"/"unanswered" not seen).
- idle_prompt notifications ("waiting for your input") mean idle, not waiting.

Plans (0.5.0): session.plan carries "source": "file" (the plan file, always wins) or "tasks" (derived from the built-in
task list when there is no plan file; see derive_plan). A derived plan needs at least 2 tasks or one tier prefix.

Tokens (0.5.0): session.tokens {in, out, cacheRead, cacheWrite, cacheHit, turns} sums the main-thread assistant usage
of the session transcript (subagents excluded; turns = distinct assistant messages, i.e. API calls); agent.tokens
{in, out, cacheRead, cacheWrite} sums the agent's own transcript (<session transcript without .jsonl>/subagents/
agent-<id>.jsonl, or the SubagentStop agent_transcript_path): every 15 s while it runs, once more after it ends.
Transcripts are read incrementally from a remembered (inode, offset); usage repeated per content block is counted once
per message id. Transcript paths are local only ("_" keys).
"""

import json
import os
import re
import time

from . import inbox

MAX_AGENTS = 12
MAX_FINISHED = 6
FINISHED = ("done", "failed", "stopped")
AGENT_IDLE_MS = 10 * 60 * 1000
FINISHED_TTL_MS = 30 * 60 * 1000
STOP_RESTARTED = "session restarted"
STOP_INACTIVE = "inactive"
STOP_KILLED = "stopped by session"  # TaskStop (or its aliases) on the agent's id
STOP_TOOLS = ("TaskStop", "KillShell", "KillBash")
NO_RESTART_SOURCES = ("compact",)  # SessionStart after compaction: the process and its subagents live on
HUMAN_NAME_SOURCES = ("user", "auto")
DERIVED_RE = re.compile(r"^(.+)-[0-9a-f]{2,4}$")  # Claude Code's generated local names: "<cwd basename>-<hex>"
TRAIL = 3
MAX_TASKS = 50
ENDED_TTL_MS = 3600 * 1000
STALE_MS = 24 * 3600 * 1000  # no event for a day and no way to check the pid: treat as ended
SNAPSHOT_MAX = 40000
TRANSCRIPT_TAIL = 128 * 1024
TOKEN_FILE_CHUNK = 4 * 1024 * 1024  # most bytes read from one transcript per enrich pass
TOKEN_PASS_BUDGET = 16 * 1024 * 1024  # most bytes read from all transcripts per enrich pass
TOKEN_IDS = 16  # recent message ids remembered for de-duplicating repeated usage
AGENT_TOKENS_EVERY_MS = 15 * 1000
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TIERS = ("heavy", "careful", "medium", "light")
TIER_RE = re.compile(r"^\[(heavy|careful|medium|light)\]\s*", re.I)
TASK_REF_RE = re.compile(r"\s*#([A-Za-z0-9_.-]{1,40})\s*$")
PROJECT_RE = re.compile(r"^([^:\[\]#\s][^:\[\]#]{0,39}?)\s*:\s+(\S.*)$")
PLAN_MAX_BYTES = 256 * 1024

WORKING_EVENTS = ("UserPromptSubmit", "PreToolUse", "SubagentStart")
REGISTRY_STATUS = {"busy": "working", "idle": "idle"}
SID_MAX = 100
MAX_REQS = 8
HOLD_DEFAULT_S = 55
REQ_GRACE_MS = 2 * 1000  # a held request older than its hold + this can no longer be answered (hook gone)
REQ_RESOLVED_SLACK_MS = 1000  # registry busy/idle this much after a request means it was answered elsewhere
REG_LAG_MS = 3 * 1000  # the registry may not show "waiting" yet this long after a prompt opened
NOTE_MAX_MS = 60 * 1000  # a request-less prompt without a registry entry
NOREG_MAX_MS = 10 * 60 * 1000  # safety expiry for requests of sessions without a registry entry
CLEAR_ALL_EVENTS = ("UserPromptSubmit", "SessionEnd")
CLEAR_TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure", "PermissionDenied")
# Without a registry entry, any of these (from the session or its agents) ends a waiting state that no hold backs.
PROGRESS_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied", "UserPromptSubmit",
                   "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "Stop", "StopFailure",
                   "SessionEnd")
# Main-thread events that prove a prompt was left: a cached registry "waiting" older than them is stale until re-read.
RESOLVE_EVENTS = ("PostToolUse", "PostToolUseFailure", "PermissionDenied", "UserPromptSubmit", "Stop", "StopFailure",
                  "answered")


def now_ms():
    return int(time.time() * 1000)


def _ms(ts):
    try:
        return int(float(ts) * 1000)
    except Exception:
        return now_ms()


def step_text(rec):
    """Plain-English step for a redacted tool record (never more than what redact.py kept)."""
    tool = rec.get("tool") or "tool"
    if rec.get("desc"):
        return rec["desc"]
    if rec.get("file"):
        return "%s %s" % (tool, rec["file"])
    if rec.get("host"):
        return "%s %s" % (tool, rec["host"])
    mcp = rec.get("mcp")
    if isinstance(mcp, dict):
        return "%s: %s" % (mcp.get("server"), mcp.get("tool"))
    return tool


def _str(v, limit):
    return v[:limit] if isinstance(v, str) else None


def validate_plan(data):
    """Return a clean plan dict, or None when `data` isn't a plan object."""
    if not isinstance(data, dict):
        return None

    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    plan = {
        "title": _str(data.get("title"), 200) or "",
        "phase": _str(data.get("phase"), 120),
        "done": num(data.get("done")),
        "total": num(data.get("total")),
        "finished": bool(data.get("finished")),
        "updatedAt": data.get("updatedAt") if isinstance(data.get("updatedAt"), (int, float, str)) else None,
        "source": "file",
        "tasks": [],
    }
    if isinstance(data.get("ccrSessionId"), str):
        plan["ccrSessionId"] = data["ccrSessionId"][:100]
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        for t in tasks[:60]:
            if not isinstance(t, dict):
                continue
            status = t.get("status")
            plan["tasks"].append(
                {
                    "id": str(t.get("id"))[:60] if t.get("id") is not None else None,
                    "title": _str(t.get("title"), 200) or "",
                    "tier": _str(t.get("tier"), 40),
                    "status": status if status in ("queued", "running", "done", "failed") else "queued",
                    "order": num(t.get("order")),
                    "agentId": _str(t.get("agentId"), 100),
                    "summary": _str(t.get("summary"), 300),
                }
            )
    return plan


def read_plan_file(path):
    """(plan or None, signature). A missing or invalid file means no plan."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    sig = (st.st_mtime_ns, st.st_size)
    if st.st_size > PLAN_MAX_BYTES:
        return None, sig
    try:
        with open(path, "r", encoding="utf-8") as f:
            return validate_plan(json.load(f)), sig
    except Exception:
        return None, sig


def read_transcript_usage(path):
    """(model, context tokens) from the newest main-thread assistant message in the last 128 KB, or (None, None)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - TRANSCRIPT_TAIL)
            f.seek(start)
            data = f.read()
    except OSError:
        return None, None
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # first line is probably cut
    for raw in reversed(lines):
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        usage = msg.get("usage")
        if not isinstance(usage, dict) or model == "<synthetic>":
            continue
        used = 0
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            v = usage.get(k)
            if isinstance(v, int):
                used += v
        return (model if isinstance(model, str) else None), used
    return None, None


def _usage_vals(u):
    return [u[k] if isinstance(u.get(k), int) and u[k] > 0 else 0 for k in USAGE_KEYS]


def read_usage(path, tok, budget=TOKEN_FILE_CHUNK, main_only=False):
    """Continue summing assistant usage in the transcript at `path` from the reader state `tok` (None: from the start).

    Returns (state, bytes consumed); state is None when the file can't be read. state = {ino, off, t: [in, out,
    cacheRead, cacheWrite, turns], ids: [[message id, [in, out, cacheRead, cacheWrite]], ...]}. A replaced or shrunk
    file starts over. Only complete lines are consumed; at most `budget` bytes are read per call. Claude Code writes one
    line per content block with the same message id and usage, so a repeated id only adds what grew."""
    try:
        f = open(path, "rb")
    except OSError:
        return None, 0
    with f:
        try:
            st = os.fstat(f.fileno())
        except OSError:
            return None, 0
        if not isinstance(tok, dict) or tok.get("ino") != st.st_ino or not _num(tok.get("off")) \
                or st.st_size < tok["off"]:
            tok = {"ino": st.st_ino, "off": 0, "t": [0, 0, 0, 0, 0], "ids": []}
        off = int(tok["off"])
        if st.st_size <= off or budget <= 0:
            return tok, 0
        f.seek(off)
        data = f.read(min(budget, st.st_size - off))
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) >= budget:  # one line longer than the budget (no usage line is): skip it
            tok["off"] = off + len(data)
            return tok, len(data)
        return tok, 0  # a line still being written
    chunk = data[: end + 1]
    tok["off"] = off + len(chunk)
    tot = tok["t"]
    ids = tok["ids"]
    for raw in chunk.split(b"\n"):
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant" or (main_only and d.get("isSidechain")):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict) or msg.get("model") == "<synthetic>" or not isinstance(msg.get("usage"), dict):
            continue
        vals = _usage_vals(msg["usage"])
        mid = msg.get("id") if isinstance(msg.get("id"), str) else d.get("uuid")
        prev = None
        for e in ids:
            if e[0] == mid and mid is not None:
                prev = e
                break
        if prev is None:
            for i in range(4):
                tot[i] += vals[i]
            tot[4] += 1
            ids.append([mid, vals])
            del ids[:-TOKEN_IDS]
        else:
            for i in range(4):
                if vals[i] > prev[1][i]:
                    tot[i] += vals[i] - prev[1][i]
                    prev[1][i] = vals[i]
    return tok, len(chunk)


def session_tokens(tok):
    """Public session.tokens from a read_usage state."""
    i, o, cr, cw, turns = tok["t"]
    den = i + cr + cw
    return {"in": i, "out": o, "cacheRead": cr, "cacheWrite": cw,
            "cacheHit": round(cr / den, 3) if den else None, "turns": turns}


def agent_tokens(tok):
    i, o, cr, cw = tok["t"][:4]
    return {"in": i, "out": o, "cacheRead": cr, "cacheWrite": cw}


def agent_transcript(session_tp, aid):
    """<session transcript minus .jsonl>/subagents/agent-<aid>.jsonl (Claude Code 2.1.3xx layout), or None."""
    if not isinstance(session_tp, str) or not session_tp.endswith(".jsonl") or not isinstance(aid, str) \
            or not AGENT_ID_RE.match(aid):
        return None
    return os.path.join(session_tp[: -len(".jsonl")], "subagents", "agent-%s.jsonl" % aid)


# ---- plans derived from the built-in task list (Crew Flow, docs/CREW-FLOW.md) ------------------------------------------
def _norm(text):
    return " ".join(text.split()).casefold() if isinstance(text, str) else ""


def parse_subject(subject):
    """(tier, project, title, bare) for a task subject or agent description.

    "[tier] " (heavy|careful|medium|light, any case) and "Project: " prefixes may come in either order. `title` is the
    text without the tier prefix (project kept, "Project: title"), `bare` the title without the project prefix."""
    text = " ".join(subject.split()) if isinstance(subject, str) else ""
    tier = project = None
    m = TIER_RE.match(text)
    if m:
        tier = m.group(1).lower()
        text = text[m.end():]
    bare = text
    m = PROJECT_RE.match(text)
    if m:
        project, bare = m.group(1).strip(), m.group(2)
        m2 = TIER_RE.match(bare) if tier is None else None
        if m2:
            tier = m2.group(1).lower()
            bare = bare[m2.end():]
        text = "%s: %s" % (project, bare)
    return tier, project, text, bare


def _title_keys(title, bare):
    keys = {_norm(title), _norm(bare)}
    keys.discard("")
    return keys


def _key_matches(agent_key, task_keys):
    """Exact (case-insensitive) match; a task subject clipped with "…" matches an agent title it is a prefix of."""
    for k in task_keys:
        if agent_key == k or (k.endswith("\u2026") and len(k) > 10 and agent_key.startswith(k[:-1].rstrip())):
            return True
    return False


def _agent_start(a):
    v = a.get("startedAt")
    return v if _num(v) else 0


def join_agents(tasks, agents):
    """{task id: [agents]} — an explicit "#<taskId>" at the end of the agent title, else the first task whose title
    (tier prefix stripped, with or without its project prefix) equals the agent's title case-insensitively, preferring
    one not completed and not already held by a running agent."""
    keys = {}
    for t in tasks:
        _, _, title, bare = parse_subject(t.get("subject"))
        keys[t["id"]] = _title_keys(title, bare)
    joined = {}
    for a in sorted(agents, key=_agent_start):
        title = a.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        tid = None
        m = TASK_REF_RE.search(title)
        if m:
            if m.group(1) in keys:
                tid = m.group(1)
            title = title[: m.start()]
        if tid is None:
            _, _, atitle, _ = parse_subject(title)
            akey = _norm(atitle)
            if not akey:
                continue
            cands = [t for t in tasks if _key_matches(akey, keys[t["id"]])]
            if not cands:
                continue
            free = [t for t in cands if t.get("status") != "completed"
                    and not any(x.get("status") == "running" for x in joined.get(t["id"], []))]
            tid = (free or cands)[0]["id"]
        joined.setdefault(tid, []).append(a)
    return joined


def derive_plan(s, name=None):
    """session.plan built from the session's built-in task list (s["tasks"] from TaskCreated/TaskUpdate), or None.

    Active only with at least 2 tasks or a tier prefix on any task. Status: completed -> done; a joined agent running ->
    running; the newest joined agent failed/stopped (and the task not completed) -> failed; in_progress -> running;
    else queued. Phase: all completed -> finished; a joined agent running -> delegate; joined agents all finished ->
    review; no joined agent yet -> plan. Title: "<Project>: <n> tasks" when every task has the same project prefix,
    else `name`."""
    if s.get("_todos"):
        return None  # a TodoWrite list, not the built-in task list
    tasks = [t for t in s.get("tasks") or [] if isinstance(t, dict) and t.get("id") is not None]
    if not tasks:
        return None
    parsed = [parse_subject(t.get("subject")) for t in tasks]
    if len(tasks) < 2 and not any(p[0] for p in parsed):
        return None
    projects = {p[1] for p in parsed}
    common = projects.pop() if len(projects) == 1 else None
    joined = join_agents(tasks, [a for a in s.get("agents") or [] if isinstance(a, dict)])
    out = []
    done = 0
    any_running = False
    updated = s.get("_tasksAt") if _num(s.get("_tasksAt")) else 0
    for i, (t, (tier, _, title, bare)) in enumerate(zip(tasks, parsed)):
        ags = joined.get(t["id"], [])
        running = any(a.get("status") == "running" for a in ags)
        newest = max(ags, key=_agent_start) if ags else None
        any_running = any_running or running
        for a in ags:
            for k in ("startedAt", "endedAt"):
                if _num(a.get(k)) and a[k] > updated:
                    updated = a[k]
        st = t.get("status")
        if st == "completed":
            status = "done"
            done += 1
        elif running:
            status = "running"
        elif newest is not None and newest.get("status") in ("failed", "stopped"):
            status = "failed"
        elif st == "in_progress":
            status = "running"
        else:
            status = "queued"
        out.append({"id": str(t["id"])[:60], "title": (bare if common else title)[:200] or str(t["id"]), "tier": tier,
                    "status": status, "order": i + 1, "agentId": newest.get("id") if newest else None,
                    "summary": None})
    if done == len(tasks):
        phase = "finished"
    elif any_running:
        phase = "delegate"
    elif joined:
        phase = "review"
    else:
        phase = "plan"
    if common:
        title = "%s: %d tasks" % (common, len(tasks))
    else:
        title = name if isinstance(name, str) and name.strip() else "Tasks"
    return {"title": title[:200], "phase": phase, "done": done, "total": len(tasks), "finished": phase == "finished",
            "updatedAt": updated or None, "source": "tasks", "tasks": out}


def find_registry(sessions_dir, sid):
    """(alive, entry) for the best ~/.claude/sessions/*.json entry of `sid` (live pid first, then newest), or None."""
    try:
        names = os.listdir(sessions_dir)
    except (OSError, TypeError):
        return None
    best = None
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, n), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("sessionId") != sid:
            continue
        pid = d.get("pid")
        alive = isinstance(pid, int) and pid > 0 and pid_alive(pid)
        key = (alive, d.get("updatedAt") or 0)
        if best is None or key > best[0]:
            best = (key, d)
    return (best[0][0], best[1]) if best else None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _basename(path):
    if not isinstance(path, str) or not path.strip():
        return None
    b = os.path.basename(path.rstrip("/"))
    return b[:200] or None


def is_derived_name(name, cwd_base=None):
    """True for names Claude Code generated itself ("project-aa"): "<cwd basename>-<2-4 hex>"."""
    if not isinstance(name, str):
        return False
    m = DERIVED_RE.match(name.strip())
    if not m:
        return False
    return cwd_base is None or m.group(1) == cwd_base


def _human(name, source, cwd_base):
    """A name worth showing: set by the user or Claude's auto-title, or (no source given) not a generated one."""
    if not isinstance(name, str) or not name.strip():
        return False
    if isinstance(source, str):
        return source in HUMAN_NAME_SOURCES
    return not is_derived_name(name, cwd_base)


def registry_names(best, entries):
    """{name, source, former, cwd} from the chosen registry entry `best` and every entry of the same session id.

    `former` is the newest human name among the entries' formerNames ({name, until, ...}) and the user/auto names of
    the other (older) entries."""
    cands = []
    for d in entries:
        cwd = _basename(d.get("cwd"))
        fn = d.get("formerNames")
        if isinstance(fn, list):
            for e in fn[:50]:
                if isinstance(e, dict) and _human(e.get("name"), e.get("nameSource"), cwd):
                    until = e.get("until")
                    cands.append((until if isinstance(until, (int, float)) else 0, e["name"].strip()[:200]))
        if d is not best and isinstance(d.get("nameSource"), str) and _human(d.get("name"), d.get("nameSource"), cwd):
            upd = d.get("updatedAt")
            cands.append((upd if isinstance(upd, (int, float)) else 0, d["name"].strip()[:200]))
    name = best.get("name")
    src = best.get("nameSource")
    return {
        "name": name.strip()[:200] if isinstance(name, str) and name.strip() else None,
        "source": src[:20] if isinstance(src, str) else None,
        "former": max(cands)[1] if cands else None,
        "cwd": _basename(best.get("cwd")),
    }


def display_name(s):
    """(name, localName) for a session dict; see the module doc for the order."""
    reg = s.get("_reg") or {}
    local = reg.get("name")
    cwd = reg.get("cwd") or (s.get("project") if isinstance(s.get("project"), str) else None)
    if local and _human(local, reg.get("source"), cwd):
        return local, local
    if reg.get("former"):
        return reg["former"], local
    plan = s.get("plan")
    title = plan.get("title") if isinstance(plan, dict) else None
    if isinstance(plan, dict) and plan.get("source") == "tasks" and ": " not in (title or ""):
        title = None  # a derived plan without a project prefix is titled after the session itself
    if isinstance(title, str) and title.strip():
        head = title.split(":", 1)[0].strip()
        return (head or title.strip())[:200], local
    if cwd and cwd != "project":
        return ("%s (%s)" % (cwd, local) if local else cwd), local
    return (local or cwd or None), local


def _proc_gone(pid, proc_start):
    """True when the process (pid, procStart) no longer runs; without /proc only the pid is checked."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.path.isdir("/proc/self"):
        return not inbox.entry_alive({"pid": pid, "procStart": proc_start})
    return not pid_alive(pid)


def _ended_key(a):
    """When a finished agent ended (startedAt for old entries without endedAt)."""
    v = a.get("endedAt") or a.get("startedAt") or 0
    return v if isinstance(v, (int, float)) else 0


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _had_hold(r):
    """True when crew-hook held this request for a hub answer (older state files: guessed from holdS/hubCanAnswer)."""
    if "_hold" in r:
        return r["_hold"] is True
    return _num(r.get("_holdS")) or r.get("hubCanAnswer") is True


def _holding(r, now):
    """True while crew-hook still polls for an answer to `r` (answers are still possible)."""
    if r.get("_holdOver") or not _had_hold(r):
        return False
    if "_hold" not in r and r.get("hubCanAnswer") is False:
        return False  # pre-0.4.1 state: unanswered or expired
    hold = r["_holdS"] if _num(r.get("_holdS")) else HOLD_DEFAULT_S
    return now <= (r.get("since") or 0) + hold * 1000 + REQ_GRACE_MS


def _reg_waiting(s):
    """The live registry entry says waiting, and no main-thread event has left a prompt since it was read."""
    return s.get("_regStatus") == "waiting" and (s.get("_resolvedAt") or 0) <= (s.get("_regRead") or 0)


def _req_kept(s, r, now):
    """Whether an open request still counts (see the module doc)."""
    reg = s.get("_regStatus")
    if reg == "waiting":
        return True
    since = r.get("since") or 0
    if reg is not None:
        at = s.get("_regAt")
        if _num(at) and at > since + REQ_RESOLVED_SLACK_MS:
            return False  # Claude Code left the prompt after it opened (answered elsewhere, denied, turn cut)
        return now - since < REG_LAG_MS or _holding(r, now)
    if r.get("_holdOver") or (_had_hold(r) and not _holding(r, now)):
        return False
    return now - since <= NOREG_MAX_MS


def _note_kept(s, note, now):
    reg = s.get("_regStatus")
    if reg == "waiting":
        return True
    age = now - (note.get("_since") or 0)
    return age < (REG_LAG_MS if reg is not None else NOTE_MAX_MS)


def _wait_kind(waiting_for):
    """Registry waitingFor ("permission prompt" / "input needed") -> waiting kind."""
    t = (waiting_for or "").lower()
    if "permission" in t:
        return "permission"
    if "input" in t or "question" in t:
        return "question"
    return "input"


def _confirmed_claude_pid(pid):
    """True only when /proc shows `pid` is a claude process (hook ppid may be a short-lived shell)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return b"claude" in f.read()
    except Exception:
        return False


class Model(object):
    def __init__(self, ctx_max=None):
        self.sessions = {}
        self._token_budget = TOKEN_PASS_BUDGET
        self.ctx_max = ctx_max or (lambda model: 1000000 if model and ("opus" in model or "fable" in model) else 200000)
        self.changed = True
        self._bad_plans = {}  # sid -> signature of a plan file that didn't parse (not re-read until it changes)

    # ---- persistence -------------------------------------------------------------------------------------------
    def to_dict(self):
        return {"sessions": self.sessions}

    def load_dict(self, data):
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            self.sessions = {k: v for k, v in data["sessions"].items() if isinstance(v, dict)}
            self.changed = True

    # ---- folding ----------------------------------------------------------------------------------------------
    def _session(self, sid, at):
        s = self.sessions.get(sid)
        if s is None:
            s = {
                "id": sid,
                "ccr": None,
                "name": None,
                "localName": None,
                "project": None,
                "status": "idle",
                "waiting": None,
                "summary": None,
                "model": None,
                "effort": None,
                "ctx": {"used": None, "max": None},
                "costUsd": None,
                "tokens": None,
                "startedAt": at,
                "updatedAt": at,
                "tasks": [],
                "plan": None,
                "agents": [],
                "_statusAt": at,
            }
            self.sessions[sid] = s
        return s

    def _set_status(self, s, status, at):
        """Set the session's base status; an open prompt (_pick_waiting) turns anything but "ended" into waiting."""
        if status == "ended":
            s.pop("_reqs", None)
            s.pop("_note", None)
            w = None
        else:
            w = self._pick_waiting(s, at)
        if w is not None:
            status = "waiting"
        elif status == "waiting":
            status = "working"  # nothing open backs it
        if s["status"] != status:
            s["status"] = status
            s["_statusAt"] = at
        if s.get("waiting") != w:
            s["waiting"] = w
            self.changed = True

    # ---- permission prompts and questions (requestId from crew-hook) --------------------------------------------
    @staticmethod
    def _pick_waiting(s, now):
        """The public waiting object for what is open right now, or None (pure; see the module doc)."""
        reqs = [r for r in s.get("_reqs") or [] if _req_kept(s, r, now)]
        if reqs:
            w = _public_req(reqs[-1])
            w["hubCanAnswer"] = _holding(reqs[-1], now)
            return w
        note = s.get("_note")
        if isinstance(note, dict) and _note_kept(s, note, now):
            return _public_req(note)
        if _reg_waiting(s):
            wf = s.get("_regWaitingFor")
            return {"kind": _wait_kind(wf), "tool": None, "text": wf or None}
        return None

    def _prune_waiting(self, s, now):
        """Drop requests and the note that no longer count (never while a cached registry "waiting" is stale)."""
        if s.get("_regStatus") == "waiting" and not _reg_waiting(s):
            return
        reqs = s.get("_reqs") or []
        keep = [r for r in reqs if _req_kept(s, r, now)]
        if len(keep) != len(reqs):
            if keep:
                s["_reqs"] = keep
            else:
                s.pop("_reqs", None)
            self.changed = True
        note = s.get("_note")
        if note is not None and not (isinstance(note, dict) and _note_kept(s, note, now)):
            s.pop("_note", None)
            self.changed = True

    def _refresh_waiting(self, s, now, fallback):
        """Prune, then re-derive status/waiting; a waiting state that ended falls back to `fallback`."""
        if s["status"] == "ended":
            return
        self._prune_waiting(s, now)
        base = s["status"] if s["status"] != "waiting" else fallback
        before = (s["status"], s.get("waiting"))
        self._set_status(s, base, now)
        if before != (s["status"], s.get("waiting")):
            self.changed = True

    def _fold_requests(self, s, ev, rec, at):
        """Track open requests in s["_reqs"] (oldest first; the newest one is session.waiting) and the request-less
        prompt s["_note"]."""
        reqs = s.get("_reqs") or []
        aid = rec.get("agent")
        rid = rec.get("rid")
        note = s.get("_note")
        tool = rec.get("tool")
        if ev in RESOLVE_EVENTS and not aid:
            s["_resolvedAt"] = at
        if ev == "PermissionRequest" and isinstance(rid, str):
            hold = rec.get("hold") is True
            w = {
                "kind": "question" if tool == "AskUserQuestion" else "permission",
                "requestId": rid,
                "tool": tool,
                "text": step_text(rec) if tool else None,
                "since": at,
                "hubCanAnswer": hold,
                "_hold": hold,
            }
            if isinstance(rec.get("questions"), list):
                w["questions"] = rec["questions"]
            if aid:
                w["_agent"] = aid
            if _num(rec.get("holdS")):
                w["_holdS"] = rec["holdS"]
            reqs = [r for r in reqs if r.get("requestId") != rid] + [w]
            reqs = reqs[-MAX_REQS:]
            note = None
        elif ev == "PermissionRequest":
            # older crew-hook without request ids: a request-less prompt
            note = {"kind": "permission", "tool": tool, "text": step_text(rec) if tool else None, "_since": at}
        elif ev == "Notification":
            kind = self._notification_kind(rec)
            if kind in ("permission", "question") and not [r for r in reqs if _req_kept(s, r, at)]:
                prev = note if isinstance(note, dict) else {}
                note = {"kind": kind, "tool": prev.get("tool"), "text": rec.get("msg") or prev.get("text"),
                        "_since": prev.get("_since") or at}
        elif ev == "answered" and isinstance(rid, str):
            reqs = [r for r in reqs if r.get("requestId") != rid]
        elif ev == "unanswered" and isinstance(rid, str):
            # The hold is over: HA can't answer any more. Kept only while the registry says waiting (_req_kept).
            for r in reqs:
                if r.get("requestId") == rid:
                    r["hubCanAnswer"] = False
                    r["_holdOver"] = True
        elif ev in CLEAR_ALL_EVENTS or (ev in ("Stop", "StopFailure") and not aid):
            reqs = []
            note = None
        elif ev == "SubagentStop" and aid:
            reqs = [r for r in reqs if r.get("_agent") != aid]
        elif ev in CLEAR_TOOL_EVENTS:
            for i, r in enumerate(reqs):
                if r.get("tool") == tool and r.get("_agent") == aid:
                    del reqs[i]
                    break
        if ev in PROGRESS_EVENTS and s.get("_regStatus") is None:
            # No registry to ask: progress means the prompt is gone, unless crew-hook still holds it.
            reqs = [r for r in reqs if _holding(r, at)]
            note = None
        if reqs:
            s["_reqs"] = reqs
        else:
            s.pop("_reqs", None)
        if note:
            s["_note"] = note
        else:
            s.pop("_note", None)
        # the event handler in apply() may set a more precise status afterwards
        self._refresh_waiting(s, at, "working")

    def waiting_state(self, sid, rid):
        """("ok", kind) when `rid` is an open request of `sid` that crew-hook is holding, else ("not_waiting", None)."""
        s = self.sessions.get(sid)
        now = now_ms()
        for r in (s.get("_reqs") or []) if s else []:
            if r.get("requestId") == rid:
                if _holding(r, now) and _req_kept(s, r, now):
                    return "ok", r.get("kind")
                break
        return "not_waiting", None

    def _agent(self, s, aid, at):
        for a in s["agents"]:
            if a["id"] == aid:
                return a
        a = {
            "id": aid,
            "type": None,
            "title": None,
            "model": None,
            "status": "running",
            "startedAt": at,
            "endedAt": None,
            "stopReason": None,
            "steps": 0,
            "current": None,
            "trail": [],
            "tokens": None,
        }
        s["agents"].append(a)
        if len(s["agents"]) > MAX_AGENTS:
            # running agents are never dropped by count; the oldest finished one goes
            finished = [x for x in s["agents"] if x["status"] != "running" and x is not a]
            if finished:
                s["agents"].remove(min(finished, key=_ended_key))
        return a

    @staticmethod
    def _revive(a):
        """A step from a stopped agent proves it still runs (not after an explicit TaskStop: late events may race it)."""
        if a["status"] == "stopped" and a.get("stopReason") != STOP_KILLED:
            a["status"] = "running"
            a["endedAt"] = None
            a["stopReason"] = None

    def _stop_running(self, s, reason, at):
        """Mark every running agent of `s` stopped (the plan's task statuses stay as the plan file says)."""
        hit = False
        for a in s["agents"]:
            if a["status"] == "running":
                a["status"] = "stopped"
                a["endedAt"] = at
                a["stopReason"] = reason
                hit = True
        if hit:
            self.changed = True
        return hit

    def _sweep_agents(self, s, now):
        """Running agents without a step for AGENT_IDLE_MS while the session is idle or ended -> stopped (inactive)."""
        reg = s.get("_regStatus")
        quiet = s["status"] == "ended" or reg == "idle" or (reg is None and s["status"] == "idle")
        if not quiet:
            return False
        hit = False
        for a in s["agents"]:
            if a["status"] != "running":
                continue
            cur = a.get("current")
            last = max(a.get("startedAt") or 0, (cur.get("at") or 0) if isinstance(cur, dict) else 0)
            if now - last > AGENT_IDLE_MS:
                a["status"] = "stopped"
                a["endedAt"] = now
                a["stopReason"] = STOP_INACTIVE
                hit = True
        if hit:
            self.changed = True
        return hit

    def _prune_agents(self, s, now):
        """Drop finished untitled no-step agents, finished agents FINISHED_TTL_MS after endedAt, and all but the
        MAX_FINISHED newest finished ones. Running agents are never dropped here."""
        keep = set()
        finished = []
        for a in s["agents"]:
            if a["status"] == "running":
                keep.add(id(a))
                continue
            if not a.get("title") and not a.get("steps"):
                continue
            if now - _ended_key(a) > FINISHED_TTL_MS:
                continue
            finished.append(a)
        keep.update(id(a) for a in sorted(finished, key=_ended_key, reverse=True)[:MAX_FINISHED])
        out = [a for a in s["agents"] if id(a) in keep]
        if len(out) != len(s["agents"]):
            s["agents"] = out
            self.changed = True

    def _refresh_name(self, s):
        name, local = display_name(s)
        if (s.get("name"), s.get("localName")) != (name, local):
            s["name"] = name
            s["localName"] = local
            self.changed = True
            return True
        return False

    def _task(self, s, tid, subject=None):
        for t in s["tasks"]:
            if t["id"] == tid:
                if subject:
                    t["subject"] = subject
                return t
        t = {"id": tid, "subject": subject or "", "status": "pending"}
        s["tasks"].append(t)
        if len(s["tasks"]) > MAX_TASKS:
            done = [x for x in s["tasks"] if x["status"] == "completed"]
            s["tasks"].remove(done[0] if done else s["tasks"][0])
        return t

    def apply(self, rec, plan_loader=read_plan_file, plans_dir=None):
        if not isinstance(rec, dict):
            return
        sid = rec.get("sid")
        ev = rec.get("ev")
        if not isinstance(sid, str) or not sid or not isinstance(ev, str):
            return
        at = _ms(rec.get("ts"))
        s = self._session(sid, at)
        self.changed = True
        s.pop("_fromPlan", None)  # it has hooks after all: normal lifecycle from now on
        if at > (s.get("updatedAt") or 0):
            s["updatedAt"] = at
        if rec.get("cwd") and not rec.get("agent"):
            s["project"] = rec["cwd"]
        if rec.get("tp") and not rec.get("agent"):
            s["_tp"] = rec["tp"]
        if rec.get("pid") and not s.get("_pidSrc"):
            s["_hookPid"] = rec["pid"]
        if rec.get("eff") and not rec.get("agent"):
            s["effort"] = rec["eff"]
        if s["status"] == "ended" and ev not in ("SessionEnd",):
            s.pop("_endedAt", None)
            self._set_status(s, "idle", at)

        tool = rec.get("tool")
        aid = rec.get("agent")
        self._fold_requests(s, ev, rec, at)

        if ev in WORKING_EVENTS:
            self._set_status(s, "working", at)

        if ev == "SessionStart":
            # startup/resume/clear (or unknown): whatever subagents were running didn't survive; compaction keeps them
            if rec.get("source") not in NO_RESTART_SOURCES:
                self._stop_running(s, STOP_RESTARTED, at)
            if rec.get("model"):
                s["model"] = rec["model"]
            if s["status"] != "working":
                self._set_status(s, "idle", at)
        elif ev == "SubagentStart" and aid:
            a = self._agent(s, aid, at)
            a["type"] = rec.get("atype") or a["type"]
            a["status"] = "running"
            a["endedAt"] = None
            a["stopReason"] = None
        elif ev == "PreToolUse":
            if aid:
                a = self._agent(s, aid, at)
                if rec.get("atype") and not a["type"]:
                    a["type"] = rec["atype"]
                self._revive(a)
                if a["current"]:
                    a["trail"] = (a["trail"] + [a["current"]])[-TRAIL:]
                a["current"] = {"text": step_text(rec), "at": at}
                a["steps"] += 1
            if tool == "TodoWrite" and isinstance(rec.get("todos"), list):
                self._todos(s, rec["todos"])
        elif ev in ("PostToolUse", "PostToolUseFailure"):
            if s["status"] == "waiting":
                self._set_status(s, "working", at)
            if ev == "PostToolUse":
                if tool in ("Agent", "Task") and rec.get("aid"):
                    a = self._agent(s, rec["aid"], at)
                    if rec.get("title"):
                        a["title"] = rec["title"]
                    if rec.get("model"):
                        a["model"] = rec["model"]
                    if rec.get("sat") and not a["type"]:
                        a["type"] = rec["sat"]
                elif tool == "TaskUpdate" and rec.get("taskId"):
                    sc = rec.get("statusChange") or {}
                    to = sc.get("to")
                    s["_tasksAt"] = at
                    if to == "deleted":
                        s["tasks"] = [t for t in s["tasks"] if t["id"] != rec["taskId"]]
                    elif to:
                        self._task(s, rec["taskId"], rec.get("subject"))["status"] = to
                    elif rec.get("subject"):
                        self._task(s, rec["taskId"], rec["subject"])
                elif tool == "TodoWrite" and isinstance(rec.get("todos"), list):
                    self._todos(s, rec["todos"])
                elif tool in STOP_TOOLS and isinstance(rec.get("stopTask"), str):
                    for a in s["agents"]:
                        if a["id"] == rec["stopTask"] and a["status"] in ("running", "stopped"):
                            a["status"] = "stopped"
                            a["endedAt"] = at
                            a["stopReason"] = STOP_KILLED
                if rec.get("plan"):
                    self._load_plan(rec["plan"], plan_loader, plans_dir)
        elif ev == "SubagentStop" and aid:
            a = self._agent(s, aid, at)
            a["status"] = "done"
            a["endedAt"] = at
            a["stopReason"] = None
            if rec.get("atype") and not a["type"]:
                a["type"] = rec["atype"]
            if isinstance(rec.get("atp"), str) and rec["atp"].endswith(".jsonl"):
                a["_atp"] = rec["atp"]  # local only: token sum at stop
        elif ev == "TaskCreated" and rec.get("task_id"):
            if s.pop("_todos", None):
                s["tasks"] = []  # the built-in task list replaces an older TodoWrite list
            s["_tasksAt"] = at
            self._task(s, rec["task_id"], rec.get("subject"))
        elif ev == "TaskCompleted" and rec.get("task_id"):
            s["_tasksAt"] = at
            self._task(s, rec["task_id"], rec.get("subject"))["status"] = "completed"
        elif ev == "Notification":
            # permission/question prompts were folded in _fold_requests; "waiting for your input" means idle
            if self._notification_kind(rec) == "input" and s["status"] == "working":
                self._set_status(s, "idle", at)
        elif ev == "Stop" and not aid:
            self._set_status(s, "idle", at)
            if rec.get("msg"):
                s["summary"] = rec["msg"]
        elif ev == "StopFailure" and not aid:
            self._set_status(s, "idle", at)
        elif ev == "PermissionDenied":
            if s["status"] == "waiting":
                self._set_status(s, "working", at)
        elif ev == "SessionEnd":
            self._set_status(s, "ended", at)
            s["_endedAt"] = at
            for a in s["agents"]:
                if a["status"] == "running":
                    a["status"] = "done"
                    a["endedAt"] = at
        self._sync_plan(s)
        self._refresh_name(s)

    @staticmethod
    def _notification_kind(rec):
        nt = (rec.get("ntype") or "").lower()
        msg = (rec.get("msg") or "").lower()
        if "permission" in nt or (not nt and "permission" in msg):
            return "permission"
        if "elicitation" in nt or "question" in nt:
            return "question"
        if "idle" in nt or (not nt and "waiting for your input" in msg):
            return "input"
        return None

    def _todos(self, s, todos):
        s["_todos"] = True
        s["tasks"] = [
            {"id": str(i + 1), "subject": t.get("content") or "", "status": t.get("status") or "pending"}
            for i, t in enumerate(todos[:MAX_TASKS])
        ]

    def _load_plan(self, path, plan_loader=read_plan_file, plans_dir=None):
        """The plan file's name is the local session id it belongs to (not necessarily the writer's session)."""
        if plans_dir:
            root = os.path.realpath(plans_dir)
            if not os.path.realpath(path).startswith(root + os.sep):
                return
        name = os.path.basename(path)
        if not name.endswith(".json"):
            return
        sid = name[: -len(".json")]
        plan, sig = plan_loader(path)
        s = self.sessions.get(sid)
        if s is None:
            if plan is None:
                return
            s = self._session(sid, now_ms())
            s["_fromPlan"] = True
        s["plan"] = plan
        s["_planSig"] = list(sig) if sig else None
        self._sync_plan(s)
        self._refresh_name(s)
        self.changed = True

    def _sync_plan(self, s):
        """Keep the plan file's plan (source "file"); without one, derive the plan from the task list (source
        "tasks", or None). Returns True when session.plan changed."""
        p = s.get("plan")
        if isinstance(p, dict) and (p.get("source") == "file" or (p.get("source") is None and s.get("_planSig"))):
            p["source"] = "file"  # state.json from before 0.5.0
            return False
        name = display_name(dict(s, plan=None))[0]
        d = derive_plan(s, name)
        if d != p:
            s["plan"] = d
            self.changed = True
            return True
        return False

    # ---- tokens -----------------------------------------------------------------------------------------------
    def _read_tokens(self, path, tok, main_only):
        budget = min(TOKEN_FILE_CHUNK, self._token_budget)
        new, used = read_usage(path, tok, budget, main_only)
        self._token_budget -= used
        return new, used

    def _enrich_tokens(self, s, now):
        """Session totals from the session transcript, agent totals from the agents' transcripts (module doc)."""
        tp = s.get("_tp")
        if tp and self._token_budget > 0:
            tok, _ = self._read_tokens(tp, s.get("_tok"), True)
            if tok is not None:
                s["_tok"] = tok
                pub = session_tokens(tok)
                if s.get("tokens") != pub:
                    s["tokens"] = pub
                    self.changed = True
        for a in s.get("agents") or []:
            running = a.get("status") == "running"
            if running:
                if now - (a.get("_tokAt") or 0) < AGENT_TOKENS_EVERY_MS:
                    continue
            elif a.get("_tokEnd") == a.get("endedAt") and "_tokEnd" in a:
                continue  # final sum done for this end
            if self._token_budget <= 0:
                return
            path = a.get("_atp") or agent_transcript(tp, a.get("id"))
            if not path:
                continue
            a["_tokAt"] = now
            tok, _ = self._read_tokens(path, a.get("_tok"), False)
            if tok is None:
                if not running and now - (_ended_key(a) or now) > AGENT_TOKENS_EVERY_MS:
                    a["_tokEnd"] = a.get("endedAt")  # no transcript to read: give up
                continue
            a["_tok"] = tok
            pub = agent_tokens(tok)
            if a.get("tokens") != pub:
                a["tokens"] = pub
                self.changed = True
            if not running:
                try:
                    complete = tok["off"] >= os.stat(path).st_size
                except OSError:
                    complete = True
                if complete:
                    a["_tokEnd"] = a.get("endedAt")

    # ---- enrichment ---------------------------------------------------------------------------------------------
    def enrich(self, sessions_dir=None, plans_dir=None, now=None):
        now = now or now_ms()
        self._token_budget = TOKEN_PASS_BUDGET
        if sessions_dir:
            self._enrich_sessions_dir(sessions_dir, now)
        for s in sorted(self.sessions.values(), key=lambda x: -(x.get("updatedAt") or 0)):
            if s["status"] != "ended":
                self._enrich_transcript(s)
                self._check_alive(s, now)
            if s["status"] != "ended":
                reg = s.get("_regStatus")
                self._refresh_waiting(s, now, REGISTRY_STATUS.get(reg, "working") if reg is not None else "idle")
            self._sweep_agents(s, now)
            self._enrich_tokens(s, now)
            self._sync_plan(s)
            self._refresh_name(s)
        if plans_dir:
            self.poll_plans(plans_dir, sessions_dir, now)
        self.prune(now)

    def _enrich_sessions_dir(self, sessions_dir, now=None):
        now = now or now_ms()
        try:
            names = os.listdir(sessions_dir)
        except OSError:
            return
        entries = {}  # sid -> [(alive, entry)]
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(sessions_dir, n), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            sid = d.get("sessionId")
            if sid not in self.sessions:
                continue
            pid = d.get("pid")
            alive = isinstance(pid, int) and pid > 0 and pid_alive(pid)
            entries.setdefault(sid, []).append((alive, d))
        for sid, s in self.sessions.items():
            lst = entries.get(sid) or []
            if s.get("_fromPlan"):
                lst = [x for x in lst if x[0]]  # plan-only sessions only follow a live registry entry
            if not lst:
                self._drop_registry(s)
                continue
            alive, d = max(lst, key=lambda x: (x[0], x[1].get("updatedAt") or 0))
            before = (s.get("ccr"), s["status"], s.get("_pid"), s.get("kind"), s.get("_inbox"),
                      len(s.get("_reqs") or []))
            s["kind"] = inbox.entry_kind(d)
            s["_inbox"] = bool(alive and inbox.inbox_ready(sessions_dir, d))
            s["_reg"] = registry_names(d, [x[1] for x in lst])
            if isinstance(d.get("bridgeSessionId"), str):
                s["ccr"] = d["bridgeSessionId"][:100]
            if alive:
                s["_pid"] = d["pid"]
                s["_pidSrc"] = "sessions"
                proc = [d["pid"], d.get("procStart")]
                prev = s.get("_regProc")
                if isinstance(prev, list) and len(prev) == 2 and prev != proc and _proc_gone(prev[0], prev[1]):
                    # the session moved to a new process (restart/respawn): its old subagents are gone
                    self._stop_running(s, STOP_RESTARTED, now)
                s["_regProc"] = proc
                self._take_registry(s, d, now)
            else:
                self._drop_registry(s)
            fstat = d.get("status")
            fat = d.get("statusUpdatedAt") or d.get("updatedAt") or 0
            if alive:
                # registry-driven waiting: set by "waiting", cleared by busy/idle unless a hold still backs it
                self._refresh_waiting(s, now, REGISTRY_STATUS.get(fstat, "working"))
            if alive and isinstance(fat, (int, float)) and fat > (s.get("_statusAt") or 0) + 1000:
                if fstat == "idle" and s["status"] == "working":
                    self._set_status(s, "idle", int(fat))
                elif fstat == "busy" and s["status"] == "idle":
                    self._set_status(s, "working", int(fat))
            self._refresh_name(s)
            if before != (s.get("ccr"), s["status"], s.get("_pid"), s.get("kind"), s.get("_inbox"),
                          len(s.get("_reqs") or [])):
                self.changed = True

    @staticmethod
    def _take_registry(s, d, now):
        """Remember a live registry entry's status for the waiting rules (_regStatus/_regAt/_regWaitingFor/_regRead)."""
        st = d.get("status")
        s["_regStatus"] = st[:20] if isinstance(st, str) else None
        if s["_regStatus"] is None:
            s.pop("_regStatus", None)
        at = d.get("statusUpdatedAt") or d.get("updatedAt")
        s["_regAt"] = int(at) if _num(at) else None
        wf = d.get("waitingFor")
        s["_regWaitingFor"] = wf.strip()[:60] if isinstance(wf, str) and wf.strip() else None
        s["_regRead"] = now

    @staticmethod
    def _drop_registry(s):
        for k in ("_regStatus", "_regAt", "_regWaitingFor", "_regRead"):
            s.pop(k, None)

    def _enrich_transcript(self, s):
        tp = s.get("_tp")
        if not tp:
            return
        try:
            st = os.stat(tp)
        except OSError:
            return
        sig = [st.st_mtime_ns, st.st_size]
        if s.get("_tpSig") == sig:
            return
        s["_tpSig"] = sig
        model, used = read_transcript_usage(tp)
        if model and model != s.get("model"):
            s["model"] = model
            self.changed = True
        if used is not None:
            mx = self.ctx_max(s.get("model"))
            if s["ctx"] != {"used": used, "max": mx}:
                s["ctx"] = {"used": used, "max": mx}
                self.changed = True

    def _check_alive(self, s, now):
        if s.get("_fromPlan"):
            # Lifetime follows the plan file (poll_plans); a dead process just makes it idle again.
            pid = s.get("_pid")
            if pid and not pid_alive(pid):
                s.pop("_pid", None)
                s.pop("_pidSrc", None)
                self._drop_registry(s)
                self._set_status(s, "idle", now)
                self._refresh_name(s)
                self.changed = True
            return
        pid = s.get("_pid")
        if not pid and s.get("_hookPid"):
            hp = s["_hookPid"]
            if _confirmed_claude_pid(hp):
                s["_pid"] = pid = hp
                s["_pidSrc"] = "hook"
        if pid:
            if not pid_alive(pid):
                self._set_status(s, "ended", now)
                s["_endedAt"] = now
                self.changed = True
        elif now - (s.get("updatedAt") or 0) > STALE_MS:
            self._set_status(s, "ended", now)
            s["_endedAt"] = now
            self.changed = True

    def _refresh_plan(self, s, plans_dir):
        path = os.path.join(plans_dir, s["id"] + ".json")
        try:
            st = os.stat(path)
            sig = [st.st_mtime_ns, st.st_size]
        except OSError:
            sig = None
        if sig != s.get("_planSig"):
            plan, rsig = read_plan_file(path)
            s["plan"] = plan
            s["_planSig"] = list(rsig) if rsig else None
            self._sync_plan(s)
            self.changed = True

    def poll_plans(self, plans_dir, sessions_dir=None, now=None):
        """Cheap pass over the plans dir (listdir + stat; files are read only when their signature changed).

        Known sessions get their plan refreshed; a plan for an unknown session id creates a plan-only session; a
        plan-only session whose file is gone is removed."""
        now = now or now_ms()
        try:
            names = os.listdir(plans_dir)
        except (OSError, TypeError):
            names = []
        present = {}
        for n in names:
            if n.endswith(".json") and 0 < len(n) - 5 <= SID_MAX and not n.startswith("."):
                present[n[:-5]] = os.path.join(plans_dir, n)
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if s.get("_fromPlan") and sid not in present:
                del self.sessions[sid]
                self.changed = True
                continue
            before = s.get("_planSig")
            self._refresh_plan(s, plans_dir)
            if s.get("_fromPlan") and s.get("_planSig") != before:
                s["updatedAt"] = max(s.get("updatedAt") or 0, now)
            if s.get("_planSig") != before:
                self._refresh_name(s)
        for sid in list(self._bad_plans):
            if sid not in present or sid in self.sessions:
                del self._bad_plans[sid]
        for sid, path in present.items():
            if sid in self.sessions:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig = [st.st_mtime_ns, st.st_size]
            if self._bad_plans.get(sid) == sig:
                continue
            plan, rsig = read_plan_file(path)
            if plan is None:
                self._bad_plans[sid] = sig
                continue
            self._session_from_plan(sid, plan, rsig, sessions_dir, now)

    def _session_from_plan(self, sid, plan, sig, sessions_dir, now):
        s = self._session(sid, now)
        s["_fromPlan"] = True
        s["plan"] = plan
        s["_planSig"] = list(sig) if sig else None
        s["ccr"] = plan.get("ccrSessionId")
        reg = find_registry(sessions_dir, sid) if sessions_dir else None
        if reg and reg[0]:
            d = reg[1]
            s["_pid"] = d["pid"]
            s["_pidSrc"] = "sessions"
            s["_reg"] = registry_names(d, [d])
            self._take_registry(s, d, now)
            s["_regProc"] = [d["pid"], d.get("procStart")]
            if isinstance(d.get("bridgeSessionId"), str):
                s["ccr"] = d["bridgeSessionId"][:100]
            s["status"] = REGISTRY_STATUS.get(d.get("status"), "idle")
            fat = d.get("statusUpdatedAt") or d.get("updatedAt")
            s["_statusAt"] = int(fat) if isinstance(fat, (int, float)) else now
            self._refresh_waiting(s, now, s["status"])  # a registry "waiting" shows right away
        self._sync_plan(s)
        self._refresh_name(s)
        self.changed = True
        return s

    def prune(self, now=None):
        now = now or now_ms()
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if s["status"] == "ended" and now - (s.get("_endedAt") or s.get("updatedAt") or 0) > ENDED_TTL_MS:
                del self.sessions[sid]
                self.changed = True
                continue
            self._prune_agents(s, now)

    # ---- snapshot -----------------------------------------------------------------------------------------------
    @staticmethod
    def _controls(s, commands_active):
        """The command actions HA may offer for this session (docs/COMMANDS.md)."""
        if not commands_active or s["status"] == "ended":
            return []
        out = []
        if s.get("_inbox"):
            out.append("send")
        w = s.get("waiting")
        if isinstance(w, dict) and w.get("requestId") and w.get("hubCanAnswer") is True:
            out.append("answer")
        if s.get("kind") == "bg" and s.get("_pid"):
            out += ["interrupt", "stop"]
        return out

    @classmethod
    def _public(cls, s, commands_active=False):
        out = {k: v for k, v in s.items() if not k.startswith("_")}
        out["agents"] = [dict({k: v for k, v in a.items() if not k.startswith("_")}, trail=list(a.get("trail") or []),
                              stopReason=a.get("stopReason"), tokens=_copy(a.get("tokens")))
                         for a in s["agents"]]
        out.setdefault("localName", None)
        out["tokens"] = _copy(s.get("tokens"))
        if isinstance(out.get("plan"), dict):
            out["plan"] = dict(out["plan"], tasks=[dict(t) for t in out["plan"].get("tasks") or []])
        out["tasks"] = [dict(t) for t in s["tasks"]]
        out["kind"] = s.get("kind") or "interactive"
        out["controls"] = cls._controls(s, commands_active)
        if isinstance(out.get("waiting"), dict):
            out["waiting"] = _public_req(out["waiting"])
        return out

    def snapshot(self, machine, commands_active=False):
        order = sorted(
            self.sessions.values(), key=lambda s: (s["status"] == "ended", -(s.get("updatedAt") or 0))
        )
        sessions = [self._public(s, commands_active) for s in order]
        snap = {"machine": machine, "sessions": sessions}
        return cap_snapshot(snap)


def _copy(d):
    return dict(d) if isinstance(d, dict) else None


def _public_req(w):
    out = {k: v for k, v in w.items() if not k.startswith("_")}
    if isinstance(out.get("questions"), list):
        out["questions"] = [dict(q, options=list(q.get("options") or [])) for q in out["questions"] if isinstance(q, dict)]
    return out


def _size(snap):
    return len(json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def cap_snapshot(snap, limit=SNAPSHOT_MAX):
    """Shrink the snapshot in stages until it fits: ended sessions, trails, finished agents, task lists, sessions."""
    if _size(snap) <= limit:
        return snap
    sessions = snap["sessions"]
    # 1. ended sessions, oldest first (they are at the end of the list)
    while _size(snap) > limit and sessions and sessions[-1]["status"] == "ended":
        sessions.pop()
    # 2. trails
    if _size(snap) > limit:
        for s in sessions:
            for a in s["agents"]:
                a["trail"] = []
    # 3. finished agents, oldest first
    while _size(snap) > limit:
        cands = [(a.get("startedAt") or 0, s, a) for s in sessions for a in s["agents"] if a["status"] != "running"]
        if not cands:
            break
        _, s, a = min(cands, key=lambda c: c[0])
        s["agents"].remove(a)
    # 4. long task lists
    if _size(snap) > limit:
        for s in sessions:
            s["tasks"] = [t for t in s["tasks"] if t["status"] != "completed"][:20]
            if s.get("plan"):
                s["plan"]["tasks"] = s["plan"]["tasks"][:30]
    # 5. whole sessions, least recently updated last
    while _size(snap) > limit and len(sessions) > 1:
        sessions.pop()
    return snap
