"""Local session registry (~/.claude/sessions/*.json) and the peer inbox socket (docs/evidence/bridge-tests.md §a).

Lookup rule (verified): the registry entry must belong to a live process (/proc/<pid>/stat field 22 == procStart), and
the key file ~/.claude/sessions/<pid>.<sha256(messagingSocketPath)>.key must carry the same procStart. Stale files from
reused pids exist, so both checks are mandatory. The peer token is read only to authenticate; it is never logged.

Windows (verified with Claude Code 2.1.317, docs/WINDOWS.md): procStart is the process creation FILETIME; the
messaging endpoint is a named pipe (\\\\.\\pipe\\LOCAL\\cc-msg-<hex>); the key file hashes the LOWER-CASED pipe path
and carries procStartFt instead of procStart. The session reads the frame when the client closes its end (a pipe has
no half-close), and closes without a reply either way, so "refused" can't be told apart from success there.
"""

import hashlib
import json
import os

from . import osdep

KIND_MAP = {"bg": "bg", "daemon-worker": "bg", "print": "print", "interactive": "interactive"}
FRAME_MAX = 64 * 1024


def proc_start(pid):
    """The process start token as Claude Code writes procStart (osdep.proc_start), or None (dead, or no way to
    read it, e.g. macOS)."""
    try:
        return osdep.proc_start(int(pid))
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


def _key_file(sessions_dir, pid, sock):
    digest = hashlib.sha256(sock.encode("utf-8")).hexdigest()
    return os.path.join(sessions_dir, "%d.%s.key" % (pid, digest))


def key_path(sessions_dir, entry):
    """~/.claude/sessions/<pid>.<sha256(socket path)>.key. Windows hashes the lower-cased pipe path, so both
    spellings are tried there (exact first)."""
    sock = entry.get("messagingSocketPath")
    if not isinstance(sock, str) or not sock:
        return None
    kp = _key_file(sessions_dir, entry["pid"], sock)
    if osdep.WINDOWS and not os.path.exists(kp):
        low = _key_file(sessions_dir, entry["pid"], sock.lower())
        if os.path.exists(low):
            return low
    return kp


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
    for field in ("procStart", "procStartFt"):  # procStartFt: Windows (FILETIME, same value as the registry's)
        if k.get(field) is not None and str(k.get(field)) != str(entry.get("procStart")):
            return None
    return k["peerToken"]


def inbox_ready(sessions_dir, entry):
    """Cheap check for the snapshot: live entry, socket file (Windows: named pipe) present, key file matching."""
    if not entry_alive(entry):
        return False
    sock = entry.get("messagingSocketPath")
    if not isinstance(sock, str) or not osdep.socket_present(sock):
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
    if osdep.is_pipe_path(sock_path):
        _send_pipe(sock_path, data)
        return
    import socket

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


def _send_pipe(path, data):
    """Windows: open the session's named pipe as a client, write auth + frame, close. Closing is the end-of-stream
    the session waits for (verified: nothing is delivered until then, and it never replies)."""
    if not osdep.pipe_exists(path):
        raise InboxError("no_inbox")
    try:
        f = open(path, "r+b", buffering=0)
    except OSError:
        raise InboxError("send_failed")  # gone, or every pipe instance busy
    try:
        view = memoryview(data)
        while view:
            n = f.write(view)
            if not n:
                raise InboxError("send_failed")
            view = view[n:]
    except OSError:
        raise InboxError("send_failed")
    finally:
        try:
            f.close()
        except OSError:
            pass


def user_frame(text, urgent):
    return {
        "type": "user",
        "priority": "now" if urgent else "next",
        "from": "crew-hub",
        "message": {"role": "user", "content": text},
    }
