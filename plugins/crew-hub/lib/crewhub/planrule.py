"""The plan-file rule in the user-level ~/.claude/CLAUDE.md (crew-hub-pair --install-plan-rule, and after pairing).

The rule text ships as <plugin>/templates/plan-file-rule.md and lives in CLAUDE.md between the two marker comments
below. Installing is idempotent: the first marked block is replaced (later duplicates removed), or the block is appended
when there is none; everything outside the markers is kept byte for byte. The file is written via a temp file + rename
in the same directory (a symlinked CLAUDE.md is followed), keeping the old file's mode.
"""

import os

from . import paths

START = "<!-- crew-hub:plan-file -->"
END = "<!-- /crew-hub:plan-file -->"


class RuleError(Exception):
    """A message for the person (e.g. an unterminated marker block); nothing was written."""


def block():
    with open(paths.plan_rule_template(), "r", encoding="utf-8") as f:
        body = f.read().strip("\n")
    return "%s\n%s\n%s" % (START, body, END)


def merge(text, new_block):
    """`text` with exactly one marked block equal to `new_block`."""
    out = []
    rest = text
    placed = False
    while True:
        i = rest.find(START)
        if i < 0:
            out.append(rest)
            break
        j = rest.find(END, i + len(START))
        if j < 0:
            raise RuleError("%s has a %s line without %s; fix it by hand" % (paths.user_claude_md(), START, END))
        out.append(rest[:i])
        if not placed:  # later (duplicate) blocks are dropped
            out.append(new_block)
            placed = True
        rest = rest[j + len(END):]
    merged = "".join(out)
    if placed:
        return merged
    if not merged:
        return new_block + "\n"
    sep = "" if merged.endswith("\n\n") else ("\n" if merged.endswith("\n") else "\n\n")
    return merged + sep + new_block + "\n"


def install():
    """Install or refresh the block. Returns "added", "updated" or "unchanged"."""
    target = os.path.realpath(paths.user_claude_md())
    try:
        with open(target, "r", encoding="utf-8") as f:
            old = f.read()
        mode = os.stat(target).st_mode & 0o7777
    except FileNotFoundError:
        old = None
        mode = None
    new = merge(old or "", block())
    if old is not None and new == old:
        return "unchanged"
    d = os.path.dirname(target)
    if not os.path.isdir(d):
        os.makedirs(d, 0o700)
    tmp = os.path.join(d, ".CLAUDE.md.crew-hub.%d.tmp" % os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644 if mode is None else mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            f.write(new)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return "added" if old is None or START not in old else "updated"
