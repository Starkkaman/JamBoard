"""Tail ~/.claude/crew-hub/spool.jsonl from a persisted (inode, offset), and rotate it once it is big and fully read.

Rotation race handling
----------------------
Hooks open the spool with O_APPEND|O_CREAT, write one line and close it, all within a few milliseconds (and Claude kills a
hook after its 5 s timeout). We never truncate the live file in place, because a hook that already holds an fd would then
write at the old end, leaving a hole of NUL bytes, or its line would be lost. Instead:

1. rename spool.jsonl -> spool.jsonl.old (atomic). Hooks that open the path afterwards create a fresh spool.jsonl.
   A hook that opened the old inode just before the rename still appends to spool.jsonl.old, which we keep draining.
2. Keep reading spool.jsonl.old to EOF on every poll for ROTATE_GRACE seconds (longer than the hook timeout, so no
   writer can still hold it), drain it one last time, then unlink it.
3. The new spool.jsonl is read from offset 0; its inode differs, so the persisted offset is reset automatically.

Lines from the old file may be applied slightly after early lines of the new file; the snapshot model tolerates that
because every record carries its own timestamp and events are idempotent folds.
"""

import os
import time

ROTATE_BYTES = 5 * 1024 * 1024
ROTATE_GRACE = 10.0
READ_CHUNK = 1024 * 1024


class _Tail(object):
    def __init__(self, path, inode=None, offset=0):
        self.path = path
        self.inode = inode
        self.offset = offset

    def read_lines(self):
        """Complete lines appended since the last call. A trailing partial line is left for the next call."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return []
        out = []
        try:
            st = os.fstat(fd)
            if self.inode != st.st_ino or st.st_size < self.offset:
                self.inode = st.st_ino
                self.offset = 0
            while True:
                os.lseek(fd, self.offset, os.SEEK_SET)
                data = os.read(fd, READ_CHUNK)
                if not data:
                    break
                end = data.rfind(b"\n")
                if end < 0:
                    if len(data) >= READ_CHUNK:
                        self.offset += len(data)  # absurd line with no newline: skip it
                        continue
                    break
                out.extend(data[: end + 1].splitlines())
                self.offset += end + 1
                if len(data) < READ_CHUNK:
                    break
        finally:
            os.close(fd)
        return out

    def size(self):
        try:
            return os.stat(self.path).st_size
        except OSError:
            return 0


class Spool(object):
    def __init__(self, path, position=None, rotate_bytes=ROTATE_BYTES, grace=ROTATE_GRACE):
        pos = position if isinstance(position, dict) else {}
        self.path = path
        self.old_path = path + ".old"
        self.tail = _Tail(path, pos.get("inode"), int(pos.get("offset") or 0))
        self.rotate_bytes = rotate_bytes
        self.grace = grace
        self.old = None
        self.old_since = 0.0
        try:
            old_ino = os.stat(self.old_path).st_ino
        except OSError:
            old_ino = None
        if old_ino is not None:
            # A rotation was in progress when we last stopped: resume draining from wherever we were in that inode.
            old_pos = pos.get("old") if isinstance(pos.get("old"), dict) else {}
            offset = 0
            if old_pos.get("inode") == old_ino:
                offset = int(old_pos.get("offset") or 0)
            elif pos.get("inode") == old_ino:  # stopped right after the rename, before saving
                offset = int(pos.get("offset") or 0)
                self.tail = _Tail(path, None, 0)
            self.old = _Tail(self.old_path, old_ino, offset)
            self.old_since = time.time()

    def position(self):
        pos = {"inode": self.tail.inode, "offset": self.tail.offset}
        if self.old is not None:
            pos["old"] = {"inode": self.old.inode, "offset": self.old.offset}
        return pos

    def poll(self):
        lines = []
        if self.old is not None:
            lines.extend(self.old.read_lines())
            if time.time() - self.old_since >= self.grace:
                lines.extend(self.old.read_lines())
                try:
                    os.unlink(self.old_path)
                except OSError:
                    pass
                self.old = None
        lines.extend(self.tail.read_lines())
        self._maybe_rotate()
        return lines

    def _maybe_rotate(self):
        if self.old is not None:
            return
        size = self.tail.size()
        if size < self.rotate_bytes or self.tail.offset < size:
            return
        try:
            os.rename(self.path, self.old_path)
        except OSError:
            return
        self.old = _Tail(self.old_path, self.tail.inode, self.tail.offset)
        self.old_since = time.time()
        self.tail = _Tail(self.path, None, 0)
