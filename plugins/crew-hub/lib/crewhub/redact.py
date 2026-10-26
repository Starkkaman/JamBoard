"""Allowlist redaction of a Claude Code hook payload into one small spool record.

Only fields listed here ever leave the hook. Prompts, commands, tool_input as a whole, tool_response bodies, file contents
and agent prompts are never copied. Keep this module import-light: it runs on every hook.
"""

import os

STEP_MAX = 200
SUBJECT_MAX = 120
MAX_TODOS = 30

FILE_TOOLS = ("Read", "Write", "Edit", "NotebookEdit", "MultiEdit")
AGENT_TOOLS = ("Agent", "Task")
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest")


def clip(value, limit):
    """First line of a string, stripped and cut to `limit` characters; None for anything else or empty."""
    if not isinstance(value, str):
        return None
    line = value.strip().split("\n", 1)[0].strip()
    if not line:
        return None
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line


def _str(value, limit=200):
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def _hostname(url):
    if not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlsplit  # lazy: only WebFetch pays for it

        host = urlsplit(url.strip()).hostname
    except Exception:
        return None
    return host[:120] if host else None


def _basename(path):
    if not isinstance(path, str) or not path:
        return None
    return os.path.basename(path.rstrip("/"))[:120] or None


def tool_fields(tool, tool_input):
    """Safe description of one tool call: a dict of a few short fields, never the raw input."""
    out = {}
    if not isinstance(tool, str):
        return out
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        d = clip(ti.get("description"), STEP_MAX)
        if d:
            out["desc"] = d
    elif tool in AGENT_TOOLS:
        d = clip(ti.get("description"), STEP_MAX)
        if d:
            out["desc"] = d
        st = _str(ti.get("subagent_type"), 80)
        if st:
            out["sat"] = st
    elif tool in FILE_TOOLS:
        b = _basename(ti.get("file_path") or ti.get("notebook_path"))
        if b:
            out["file"] = b
    elif tool == "WebFetch":
        h = _hostname(ti.get("url"))
        if h:
            out["host"] = h
    elif tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3:
            out["mcp"] = {"server": parts[1][:80], "tool": parts[2][:80]}
    # WebSearch, Grep, Glob and everything else: the tool name alone.
    return out


def _todos(ti):
    todos = ti.get("todos") if isinstance(ti, dict) else None
    if not isinstance(todos, list):
        return None
    out = []
    for t in todos[:MAX_TODOS]:
        if not isinstance(t, dict):
            continue
        out.append({"content": clip(t.get("content"), SUBJECT_MAX) or "", "status": _str(t.get("status"), 20) or "pending"})
    return out


def plan_path_for(file_path, plans_dir):
    """Return the resolved path when `file_path` is a .json file directly or indirectly under `plans_dir`, else None."""
    if not isinstance(file_path, str) or not file_path.endswith(".json"):
        return None
    try:
        real = os.path.realpath(file_path)
        root = os.path.realpath(plans_dir)
    except Exception:
        return None
    if real.startswith(root + os.sep) and real.endswith(".json"):
        return real
    return None


def redact(payload, ts, pid=None, plans_dir=None):
    """Build the spool record for one hook payload. Never raises for odd input shapes."""
    if not isinstance(payload, dict):
        payload = {}
    ev = _str(payload.get("hook_event_name"), 40) or "Unknown"
    rec = {"v": 1, "ts": ts, "ev": ev}
    sid = _str(payload.get("session_id"), 100)
    if sid:
        rec["sid"] = sid
    if pid:
        rec["pid"] = pid
    cwd = _basename(payload.get("cwd"))
    if cwd:
        rec["cwd"] = cwd
    tp = _str(payload.get("transcript_path"), 1024)
    if tp:
        rec["tp"] = tp  # local only: the daemon reads the transcript tail; never published
    for src, dst, lim in (("agent_id", "agent", 100), ("agent_type", "atype", 80), ("tool_use_id", "tuid", 100)):
        val = _str(payload.get(src), lim)
        if val:
            rec[dst] = val
    eff = payload.get("effort")
    if isinstance(eff, dict):
        lvl = _str(eff.get("level"), 20)
        if lvl:
            rec["eff"] = lvl

    tool = _str(payload.get("tool_name"), 200)
    if tool:
        rec["tool"] = tool
    tool_input = payload.get("tool_input")

    if ev in TOOL_EVENTS and tool:
        rec.update(tool_fields(tool, tool_input))
        if tool == "TodoWrite":
            todos = _todos(tool_input)
            if todos is not None:
                rec["todos"] = todos
        if ev == "PostToolUse":
            resp = payload.get("tool_response")
            if not isinstance(resp, dict):
                resp = {}
            if tool in AGENT_TOOLS:
                aid = _str(resp.get("agentId"), 100)
                if aid:
                    rec["aid"] = aid
                title = clip(resp.get("description"), STEP_MAX)
                if title:
                    rec["title"] = title
                model = _str(resp.get("resolvedModel"), 100)
                if model:
                    rec["model"] = model
                if isinstance(resp.get("isAsync"), bool):
                    rec["async"] = resp["isAsync"]
            elif tool == "TaskUpdate":
                tid = resp.get("taskId")
                if tid is None and isinstance(tool_input, dict):
                    tid = tool_input.get("taskId")
                if isinstance(tid, (str, int)):
                    rec["taskId"] = str(tid)[:40]
                sc = resp.get("statusChange")
                if isinstance(sc, dict):
                    rec["statusChange"] = {"from": _str(sc.get("from"), 20), "to": _str(sc.get("to"), 20)}
            elif tool in ("Write", "Edit") and plans_dir and isinstance(tool_input, dict):
                plan = plan_path_for(tool_input.get("file_path"), plans_dir)
                if plan:
                    rec["plan"] = plan

    elif ev in ("TaskCreated", "TaskCompleted"):
        tid = payload.get("task_id")
        if isinstance(tid, (str, int)):
            rec["task_id"] = str(tid)[:40]
        subj = clip(payload.get("task_subject"), SUBJECT_MAX)
        if subj:
            rec["subject"] = subj
    elif ev in ("Stop", "SubagentStop"):
        msg = clip(payload.get("last_assistant_message"), STEP_MAX)
        if msg:
            rec["msg"] = msg
    elif ev == "SessionStart":
        src = _str(payload.get("source"), 40)
        if src:
            rec["source"] = src
        model = payload.get("model")
        if isinstance(model, str):
            rec["model"] = model[:100]
    elif ev == "SessionEnd":
        reason = _str(payload.get("reason"), 40)
        if reason:
            rec["reason"] = reason
    elif ev == "Notification":
        nt = _str(payload.get("notification_type"), 60)
        if nt:
            rec["ntype"] = nt
        msg = clip(payload.get("message"), STEP_MAX)
        if msg:
            rec["msg"] = msg
    elif ev in ("PreCompact", "PostCompact"):
        trig = _str(payload.get("trigger"), 20)
        if trig:
            rec["trigger"] = trig
    return rec
