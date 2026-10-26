"""Local session registry (~/.claude/sessions/*.json) and the peer inbox socket (docs/evidence/bridge-tests.md §a).

Lookup rule (verified): the registry entry must belong to a live process (/proc/<pid>/stat field 22 == procStart), and
the key file ~/.claude/sessions/<pid>.<sha256(messagingSocketPath)>.key must carry the same procStart. Stale files from
reused pids exist, so both checks are mandatory. The peer token is read only to authenticate; it is never logged.
"""

import hashlib
import json
import os
import socket

KIND_MAP = {"bg": "bg", "daemon-worker": "bg", "print": "print", "interactive": "interactive"}
FRAME_MAX = 64 * 1024


def proc_start(pid):
    """Field 22 (starttime) of /proc/<pid>/stat as a string, or None (dead, or no /proc)."""
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as f:
            data = f.read().decode("utf-8", "replace")
        return data[data.rindex(")") + 2 :].split()[19]
    except Exception:
        return None


def entry_kind(entry):
    """bg | interactive | print, from the registry `kind` (bg also when a jobId is present)."""
    k = entry.get("kind") if isinstance(entry, dict) else None
    if isinstance(k, str) and k in KIND_MAP:
        return KIND_MAP[k]
    if isinstance(entry, dict) and isinstance(entry.get("jobId"), str) and entry.get("jobId"):
        return "bg"
    return "interactive"


def entry_alive(entry):
    """True when the entry's pid is running and is the same process (procStart matches)."""
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    live = proc_start(pid)
    if live is None:
        return False
    ps = entry.get("procStart")
    return ps is None or str(ps) == live


def read_entries(sessions_dir):
    out = []
    try:
        names = os.listdir(sessions_dir)
    except (OSError, TypeError):
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, n), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def find_live(sessions_dir, session_id):
    """The live registry entry whose sessionId is `session_id`, or None."""
    if not isinstance(session_id, str) or not session_id:
        return None
    for d in read_entries(sessions_dir):
        if d.get("sessionId") == session_id and entry_alive(d):
            return d
    return None


def key_path(sessions_dir, entry):
    sock = entry.get("messagingSocketPath")
    if not isinstance(sock, str) or not sock:
        return None
    digest = hashlib.sha256(sock.encode("utf-8")).hexdigest()
    return os.path.join(sessions_dir, "%d.%s.key" % (entry["pid"], digest))


def peer_token(sessions_dir, entry):
    """The inbox token for a live entry, or None when the key file is missing or belongs to another process."""
    kp = key_path(sessions_dir, entry)
    if not kp:
        return None
    try:
        with open(kp, "r", encoding="utf-8") as f:
            k = json.load(f)
    except Exception:
        return None
    if not isinstance(k, dict) or not isinstance(k.get("peerToken"), str) or not k["peerToken"]:
        return None
    if k.get("procStart") is not None and str(k.get("procStart")) != str(entry.get("procStart")):
        return None
    return k["peerToken"]


def inbox_ready(sessions_dir, entry):
    """Cheap check for the snapshot: live entry, socket file present, key file present and matching."""
    if not entry_alive(entry):
        return False
    sock = entry.get("messagingSocketPath")
    if not isinstance(sock, str) or not os.path.exists(sock):
        return False
    return peer_token(sessions_dir, entry) is not None


class InboxError(Exception):
    pass


def send_frame(sessions_dir, entry, frame, timeout=5.0):
    """Authenticate on the session's inbox socket and deliver one frame. Raises InboxError (code in args[0])."""
    if not entry_alive(entry):
        raise InboxError("not_live")
    sock_path = entry.get("messagingSocketPath")
    if not isinstance(sock_path, str) or not sock_path:
        raise InboxError("no_inbox")
    token = peer_token(sessions_dir, entry)
    if token is None:
        raise InboxError("no_inbox")
    data = (json.dumps({"type": "auth", "token": token}) + "\n" + json.dumps(frame, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    if len(data) > FRAME_MAX:
        raise InboxError("too_long")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        try:
            s.connect(sock_path)
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
        except OSError:
            raise InboxError("send_failed")
        # Success = the session closes without a reply (verified). A reply means it refused the frame.
        try:
            reply = s.recv(4096)
        except socket.timeout:
            reply = b""
        except OSError:
            reply = b""
        if reply.strip():
            raise InboxError("refused")
    finally:
        s.close()


def user_frame(text, urgent):
    return {
        "type": "user",
        "priority": "now" if urgent else "next",
        "from": "crew-hub",
        "message": {"role": "user", "content": text},
    }
