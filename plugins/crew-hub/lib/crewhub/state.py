"""State model: folds spool records into the per-machine snapshot of DESIGN.md §2.

Times in the snapshot are epoch milliseconds (ints), like ~/.claude/sessions/*.json. Keys starting with "_" are
internal (pid, transcript path, file signatures) and are never published.
"""

import json
import os
import time

MAX_AGENTS = 12
TRAIL = 3
MAX_TASKS = 50
ENDED_TTL_MS = 3600 * 1000
STALE_MS = 24 * 3600 * 1000  # no event for a day and no way to check the pid: treat as ended
SNAPSHOT_MAX = 40000
TRANSCRIPT_TAIL = 128 * 1024
PLAN_MAX_BYTES = 256 * 1024

WORKING_EVENTS = ("UserPromptSubmit", "PreToolUse", "SubagentStart")


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
        self.ctx_max = ctx_max or (lambda model: 1000000 if model and ("opus" in model or "fable" in model) else 200000)
        self.changed = True

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
                "project": None,
                "status": "idle",
                "waiting": None,
                "summary": None,
                "model": None,
                "effort": None,
                "ctx": {"used": None, "max": None},
                "costUsd": None,
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
        if s["status"] != status:
            s["status"] = status
            s["_statusAt"] = at
        if status != "waiting":
            s["waiting"] = None

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
            "steps": 0,
            "current": None,
            "trail": [],
        }
        s["agents"].append(a)
        if len(s["agents"]) > MAX_AGENTS:
            finished = [x for x in s["agents"] if x["status"] != "running" and x is not a]
            victim = min(finished or [x for x in s["agents"] if x is not a], key=lambda x: x.get("startedAt") or 0)
            s["agents"].remove(victim)
        return a

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

        if ev in WORKING_EVENTS:
            self._set_status(s, "working", at)

        if ev == "SessionStart":
            if rec.get("model"):
                s["model"] = rec["model"]
            if s["status"] != "working":
                self._set_status(s, "idle", at)
        elif ev == "SubagentStart" and aid:
            a = self._agent(s, aid, at)
            a["type"] = rec.get("atype") or a["type"]
            a["status"] = "running"
        elif ev == "PreToolUse":
            if aid:
                a = self._agent(s, aid, at)
                if rec.get("atype") and not a["type"]:
                    a["type"] = rec["atype"]
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
                    if to == "deleted":
                        s["tasks"] = [t for t in s["tasks"] if t["id"] != rec["taskId"]]
                    elif to:
                        self._task(s, rec["taskId"])["status"] = to
                elif tool == "TodoWrite" and isinstance(rec.get("todos"), list):
                    self._todos(s, rec["todos"])
                if rec.get("plan"):
                    self._load_plan(rec["plan"], plan_loader, plans_dir)
        elif ev == "SubagentStop" and aid:
            a = self._agent(s, aid, at)
            a["status"] = "done"
            a["endedAt"] = at
            if rec.get("atype") and not a["type"]:
                a["type"] = rec["atype"]
        elif ev == "TaskCreated" and rec.get("task_id"):
            self._task(s, rec["task_id"], rec.get("subject"))
        elif ev == "TaskCompleted" and rec.get("task_id"):
            self._task(s, rec["task_id"], rec.get("subject"))["status"] = "completed"
        elif ev == "PermissionRequest":
            self._set_status(s, "waiting", at)
            s["waiting"] = {"kind": "permission", "tool": tool, "text": step_text(rec) if tool else None}
        elif ev == "Notification":
            kind = self._notification_kind(rec)
            if kind:
                self._set_status(s, "waiting", at)
                prev = s.get("waiting") or {}
                s["waiting"] = {"kind": kind, "tool": prev.get("tool"), "text": rec.get("msg") or prev.get("text")}
        elif ev == "Stop" and not aid:
            self._set_status(s, "idle", at)
            if rec.get("msg"):
                s["summary"] = rec["msg"]
        elif ev == "StopFailure" and not aid:
            self._set_status(s, "idle", at)
        elif ev == "SessionEnd":
            self._set_status(s, "ended", at)
            s["_endedAt"] = at
            for a in s["agents"]:
                if a["status"] == "running":
                    a["status"] = "done"
                    a["endedAt"] = at

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
        s["plan"] = plan
        s["_planSig"] = list(sig) if sig else None
        self.changed = True

    # ---- enrichment ---------------------------------------------------------------------------------------------
    def enrich(self, sessions_dir=None, plans_dir=None, now=None):
        now = now or now_ms()
        if sessions_dir:
            self._enrich_sessions_dir(sessions_dir)
        for s in self.sessions.values():
            if s["status"] != "ended":
                self._enrich_transcript(s)
                self._check_alive(s, now)
            if plans_dir:
                self._refresh_plan(s, plans_dir)
        self.prune(now)

    def _enrich_sessions_dir(self, sessions_dir):
        try:
            names = os.listdir(sessions_dir)
        except OSError:
            return
        best = {}
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
            key = (alive, d.get("updatedAt") or 0)
            if sid not in best or key > best[sid][0]:
                best[sid] = (key, d)
        for sid, ((alive, _), d) in best.items():
            s = self.sessions[sid]
            before = (s.get("name"), s.get("ccr"), s["status"], s.get("_pid"))
            if isinstance(d.get("name"), str):
                s["name"] = d["name"][:200]
            if isinstance(d.get("bridgeSessionId"), str):
                s["ccr"] = d["bridgeSessionId"][:100]
            if alive:
                s["_pid"] = d["pid"]
                s["_pidSrc"] = "sessions"
            fstat = d.get("status")
            fat = d.get("statusUpdatedAt") or d.get("updatedAt") or 0
            if alive and isinstance(fat, (int, float)) and fat > (s.get("_statusAt") or 0) + 1000:
                if fstat == "idle" and s["status"] == "working":
                    self._set_status(s, "idle", int(fat))
                elif fstat == "busy" and s["status"] == "idle":
                    self._set_status(s, "working", int(fat))
            if before != (s.get("name"), s.get("ccr"), s["status"], s.get("_pid")):
                self.changed = True

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
            self.changed = True

    def prune(self, now=None):
        now = now or now_ms()
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if s["status"] == "ended" and now - (s.get("_endedAt") or s.get("updatedAt") or 0) > ENDED_TTL_MS:
                del self.sessions[sid]
                self.changed = True

    # ---- snapshot -----------------------------------------------------------------------------------------------
    @staticmethod
    def _public(s):
        out = {k: v for k, v in s.items() if not k.startswith("_")}
        out["agents"] = [dict(a, trail=list(a.get("trail") or [])) for a in s["agents"]]
        out["tasks"] = [dict(t) for t in s["tasks"]]
        return out

    def snapshot(self, machine):
        order = sorted(
            self.sessions.values(), key=lambda s: (s["status"] == "ended", -(s.get("updatedAt") or 0))
        )
        sessions = [self._public(s) for s in order]
        snap = {"machine": machine, "sessions": sessions}
        return cap_snapshot(snap)


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
