# crew-hub on Windows (plugin 0.6.0)

Native Windows support for the machine side (hooks, crew-hubd, crew-hub-pair). Nothing changes on the Home Assistant
side, in the snapshot shape (DESIGN.md §2), the command contract (COMMANDS.md) or the webhook (WEBHOOK.md). POSIX
behaviour is unchanged: every Windows path is behind `osdep.WINDOWS` (`lib/crewhub/osdep.py`).

Verified 2026-11-03 on Windows 11 Home 26300 (machine "Serenity"), Claude Code 2.1.317, Git for Windows 2.56, hooks
run with the Store's Python 3.11 (`python3`), tests with python.org 3.14.

## What Claude Code does on Windows (observed)

| Thing | Windows | Consequence |
|---|---|---|
| Hook commands | run by **Git Bash** (`/usr/bin/bash`, `MSYSTEM=UCRT64`); `${CLAUDE_PLUGIN_ROOT}` expands to a forward-slash path; the `#!/usr/bin/env python3` shebang works (also with a CRLF checkout) | hooks.json and bin/ are unchanged and shared with POSIX. Needs `python3` on Git Bash's PATH. |
| Hook parent | `bash.exe` (2–3 of them), then `claude.exe` | `osdep.claude_pid` walks up to 6 parents (image name + NtQueryInformationProcess) to `claude.exe`. |
| `procStart` in `sessions/<pid>.json` | process creation time as a FILETIME (e.g. `134381983571074905`) | `osdep.proc_start` = GetProcessTimes creation time: exact match, so pid reuse is still caught. |
| `messagingSocketPath` | named pipe `\\.\pipe\LOCAL\cc-msg-<32 hex>` | inbox send = open the pipe, write auth + frame, **close**. The session acts on the frame when the client closes (a pipe has no half-close) and never replies, so `refused` can't be detected (a bad token is silently dropped, verified). |
| key file | `<pid>.<sha256(LOWER-CASED pipe path)>.key`, fields `peerToken`, `procStartFt`, `pidDomain` | `inbox.key_path` tries the exact spelling, then lower case; `procStartFt` is checked like `procStart`. |
| `~/.claude.json` project keys | forward slashes: `C:/Serenity Projects` | `commands.is_trusted` also tries the forward-slash spelling of a preset path. |
| `claude --bg …` output | `backgrounded · <id> · <name>` (same as POSIX) | start works unchanged; `claude stop <id>` prints `stopped <id>`, exit 0. |
| bg sessions | run under `claude.exe --bg-pty-host \\.\pipe\cc-daemon-…` (ConPTY) | **interrupt** (`claude attach` in a pty + one Esc) is not ported: it is not offered in `controls` and the action answers `unsupported`. |

## Python on Windows: traps the port handles

- **`os.kill(pid, 0)` is Ctrl+C.** On Windows signal 0 is `CTRL_C_EVENT`: it doesn't probe, it interrupts the target's
  console group (it took down the first test run with KeyboardInterrupt). `osdep.pid_alive` uses OpenProcess +
  WaitForSingleObject. Never call `os.kill(pid, 0)` in this code base.
- **`os.kill(pid, SIGTERM)` is TerminateProcess**: no handler runs, so no offline status or state save. crew-hubd stops
  cleanly when `~/.claude/crew-hub/crew-hubd.stop` exists (checked every loop, Windows only); crew-hub-pair uses it for
  restarts. A killed hook can't record `unanswered`; the model's hold expiry covers it.
- **CRT text mode.** `os.open` without `O_BINARY` translates `\r\n` and treats 0x1A as EOF, which broke spool offsets.
  Every `os.open` passes `O_BINARY` (0 on POSIX).
- **`O_APPEND` is seek-then-write** in the C runtime, so concurrent appenders overwrite each other (seen in
  crew-hubd.log: main thread vs command worker). `osdep.append_bytes` opens with `FILE_APPEND_DATA` only (atomic
  appends) and share mode read|write|delete, so the daemon can still rotate the spool while a hook writes. Used for the
  spool and the daemon log.
- **Detached daemon.** `subprocess.Popen` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` (+
  `CREATE_BREAKAWAY_FROM_JOB` when allowed). Verified: crew-hubd keeps running after the session that spawned it ends.
  Its own children (`claude stop`, `claude --bg`) get `CREATE_NO_WINDOW`, or each would open a console window.
- **Pidfile lock**: msvcrt byte-range lock on one byte at offset 1 GiB (past the pid text, so the hook can still read
  the pid). An open file can't be deleted on Windows, so the daemon releases and closes it before unlinking.
- **Blocked webhook poll.** Winsock's `shutdown()` doesn't wake a `recv()` in another thread and `sock.close()` is
  deferred while http.client's makefile holds the socket; `Http.abort` detaches the socket and closes the handle.
- **File modes**: 0600/0700 don't exist. chmod/fchmod are skipped; `~/.claude/crew-hub` lives in the user profile,
  whose ACL limits it to the user and administrators.
- **`~`** is `USERPROFILE` (HOME is ignored by Python). Claude Code uses the same. Tests set both (`_util.home_env`,
  `set_home`), or they would write into the real profile.
- **Named pipe existence** is checked by listing `\\.\pipe\` (on older Pythons `os.path.exists` may open a client
  connection to the session).

## Hook cost

~125–135 ms per hook on this machine, of which ~85 ms is the Store Python's own start-up (`python3 -c pass`); the
parent walk is ~7 ms. Well inside the 5 s hook timeout.

## Install and pairing

See README.md, "Windows". In short: Git for Windows (Claude Code needs it anyway) and a `python3` that Git Bash finds
(`python3 --version` in Git Bash; the Microsoft Store Python provides `python3`, the python.org installer only
`python`/`py`), then `claude plugin install crew-hub@jamboard`, then `/crew-hub-setup`.

## Tests

`python -W error::ResourceWarning -m unittest discover -s tests` passes on Windows (197 tests, 5 skipped: the two pty
tests, the SIGTERM→unanswered hook test, the Unix-socket refusal test and the symlink test). Windows-only tests: send
over a real named pipe (`FakePipeInbox`, multiprocessing's AF_PIPE listener) and the lower-cased key file +
`procStartFt` convention.
