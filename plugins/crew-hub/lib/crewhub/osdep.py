"""OS-specific process and file primitives. POSIX keeps the exact behaviour crew-hub always had; Windows gets
equivalents through ctypes (kernel32) and msvcrt. Nothing here imports ctypes, subprocess or msvcrt at module load:
the hook starts on every tool call, so those are imported only on the Windows code paths that need them.

Windows notes (verified on Windows 11 with Claude Code 2.1.317, docs/WINDOWS.md):
- Never call os.kill(pid, 0) on Windows: signal 0 is CTRL_C_EVENT there, so it sends Ctrl+C to the target's console
  process group instead of probing it. pid_alive() uses OpenProcess + WaitForSingleObject instead.
- The session registry's procStart is the process creation time as a FILETIME (100 ns ticks since 1601), which
  GetProcessTimes returns. proc_start() returns the same decimal string, so the registry check stays exact.
- Hooks run under Git Bash, so the hook's parent is bash.exe, not claude.exe: parent_chain() walks a Toolhelp
  snapshot instead of /proc.
- File modes (0600/0700) don't exist; the files live in the user profile, whose ACL already limits them to the user
  (and administrators). chmod helpers are no-ops there.
"""

import os
import sys

O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no CRT text-mode translation (CRLF, Ctrl+Z)

WINDOWS = os.name == "nt"

# ---- Windows API (lazy) --------------------------------------------------------------------------------------------
_K32 = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
ERROR_ACCESS_DENIED = 5
WAIT_TIMEOUT = 0x102
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = -1

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

FILE_APPEND_DATA = 0x0004
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read | write | delete: a rename/rotation by the daemon works while hooks append
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80

LOCK_OFFSET = 1 << 30  # Windows byte-range lock far past the pid text, so readers of the pidfile never hit it


def _k32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE)
        k.WriteFile.argtypes = (wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                ctypes.c_void_p)
        _K32 = k
    return _K32


def _bad_handle(h):
    return not h or h == INVALID_HANDLE or (isinstance(h, int) and h == (1 << (8 * _ptr_size())) - 1)


def _ptr_size():
    import ctypes

    return ctypes.sizeof(ctypes.c_void_p)


def _open_process(pid, extra=0):
    """(handle or None, access_denied)."""
    import ctypes

    k = _k32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | extra, False, int(pid))
    if not h:
        return None, ctypes.get_last_error() == ERROR_ACCESS_DENIED
    return h, False


def _win_alive(pid):
    try:
        h, denied = _open_process(pid, SYNCHRONIZE)
        if h is None:
            if denied:
                return True  # exists, but belongs to someone we can't query
            h, denied = _open_process(pid)
            if h is None:
                return denied
            k = _k32()
            try:
                import ctypes
                from ctypes import wintypes

                code = wintypes.DWORD()
                k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
                return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        k = _k32()
        try:
            return k.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k.CloseHandle(h)
    except Exception:
        return True  # same bias as POSIX: an odd error never ends a session


def _win_creation_time(pid):
    try:
        import ctypes
        from ctypes import wintypes

        h, _ = _open_process(pid)
        if h is None:
            return None
        k = _k32()
        try:
            c, e, kt, ut = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def _win_image(pid):
    """Lower-case executable basename of `pid` ("claude.exe"), or None."""
    try:
        import ctypes
        from ctypes import wintypes

        h, _ = _open_process(pid)
        if h is None:
            return None
        k = _k32()
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return None
            return os.path.basename(buf.value).lower() or None
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


_NT = None


def _nt():
    """(ntdll, PROCESS_BASIC_INFORMATION class), set up once."""
    global _NT
    if _NT is None:
        import ctypes
        from ctypes import wintypes

        class PBI(ctypes.Structure):
            _fields_ = [("ExitStatus", ctypes.c_long), ("PebBaseAddress", ctypes.c_void_p),
                        ("AffinityMask", ctypes.c_size_t), ("BasePriority", ctypes.c_long),
                        ("UniqueProcessId", ctypes.c_size_t), ("InheritedFromUniqueProcessId", ctypes.c_size_t)]

        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        ntdll.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                    ctypes.c_void_p)
        _NT = (ntdll, PBI)
    return _NT


def _win_name_parent(pid):
    """(lower-case exe name, parent pid) of one process on one handle: QueryFullProcessImageNameW +
    NtQueryInformationProcess(ProcessBasicInformation). Microseconds, where a full Toolhelp snapshot costs ~10 ms
    per hook run. (None, None) when it can't be read."""
    try:
        import ctypes
        from ctypes import wintypes

        h, _ = _open_process(pid)
        if h is None:
            return None, None
        k = _k32()
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            name = os.path.basename(buf.value).lower() if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) \
                else None
            ntdll, PBI = _nt()
            pbi = PBI()
            ok = ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) == 0
            return name or None, (int(pbi.InheritedFromUniqueProcessId) or None) if ok else None
        finally:
            k.CloseHandle(h)
    except Exception:
        return None, None


def _win_process_table():
    """{pid: (exe lower-case, ppid)} from one Toolhelp snapshot, or {}."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k = _k32()
        k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if _bad_handle(snap):
            return {}
        out = {}
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = (e.szExeFile.lower(), int(e.th32ParentProcessID))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return out
    except Exception:
        return {}


# ---- /proc (Linux) -------------------------------------------------------------------------------------------------
def _proc_stat_fields(pid):
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    lp, rp = data.find("("), data.rfind(")")
    if lp < 0 or rp < 0:
        return None
    return data[lp + 1 : rp], data[rp + 2 :].split()


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as f:
            return f.read()
    except Exception:
        return None


def has_proc():
    return os.path.isdir("/proc/self")


# ---- public: processes ---------------------------------------------------------------------------------------------
def pid_alive(pid, on_error=True):
    """True while `pid` runs (or can't be ruled out). POSIX: os.kill(pid, 0), as before; `on_error` is the answer
    for any other error (callers differed: state.py said alive, the hook's daemon check said not alive)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if WINDOWS:
        return _win_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return on_error


def proc_start(pid):
    """The process start token Claude Code writes as procStart: /proc field 22 on Linux, the creation FILETIME on
    Windows, as a decimal string. None when the process is gone or the OS has neither (macOS)."""
    if WINDOWS:
        t = _win_creation_time(pid)
        return None if t is None else str(t)
    st = _proc_stat_fields(pid)
    try:
        return st[1][19] if st else None
    except Exception:
        return None


def can_check_start():
    """True where proc_start() works (Linux /proc, Windows), so pid reuse can be ruled out."""
    return WINDOWS or has_proc()


def proc_name_ppid(pid):
    """(name, ppid) of a process, or None where it can't be read. Linux: comm; Windows: lower-case exe name."""
    if WINDOWS:
        row = _win_process_table().get(int(pid))
        return row
    st = _proc_stat_fields(pid)
    if not st:
        return None
    try:
        return st[0], int(st[1][1])
    except Exception:
        return None


POSIX_SHELLS = ("sh", "bash", "dash", "zsh", "ksh", "fish")
WIN_WRAPPERS = ("bash.exe", "sh.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "python3.exe",
                "py.exe", "env.exe")


def _is_claude_exe(name):
    return isinstance(name, str) and (name == "claude.exe" or name.startswith("claude"))


def claude_pid(ppid):
    """The claude process that ran a hook whose parent is `ppid`.

    POSIX: skip up to three `sh -c` wrappers via /proc (unchanged). Windows: hooks run under Git Bash (several
    bash.exe hops; the Store's python alias may add one), so walk up to six parents, one process at a time, until
    claude.exe; fall back to `ppid` when none is found."""
    if WINDOWS:
        pid = ppid
        for _ in range(6):
            name, parent = _win_name_parent(pid)
            if name is None:
                break
            if _is_claude_exe(name):
                return pid
            if name not in WIN_WRAPPERS or not parent or parent == pid:
                break
            pid = parent
        return ppid
    pid = ppid
    for _ in range(3):
        st = proc_name_ppid(pid)
        if not st or st[0] not in POSIX_SHELLS or st[1] <= 1:
            break
        pid = st[1]
    return pid


def is_claude_pid(pid):
    """True only when the OS confirms `pid` is a claude process (Linux cmdline, Windows image name)."""
    if WINDOWS:
        return _is_claude_exe(_win_image(pid))
    cl = _proc_cmdline(pid)
    return cl is not None and b"claude" in cl


def is_daemon_pid(pid):
    """Guard against pid reuse for crew-hubd's pidfile. Linux: cmdline names crew-hubd. Windows: a python image.
    Elsewhere (macOS) the pid alone has to do."""
    if WINDOWS:
        img = _win_image(pid)
        return img is None or img.startswith("python")  # None: can't query (other user), keep the old bias
    if has_proc():
        cl = _proc_cmdline(pid)
        return True if cl is None and os.path.exists("/proc/%d" % pid) else (cl is not None and b"crew-hubd" in cl)
    return True


def spawn_detached(argv, cwd=None):
    """Start a long-lived background process that outlives the hook and the claude session that ran it.

    POSIX: double fork + setsid, stdio to /dev/null (unchanged). Windows: DETACHED_PROCESS (no console),
    CREATE_NEW_PROCESS_GROUP (no Ctrl+C from the parent console) and CREATE_BREAKAWAY_FROM_JOB when the hook's job
    allows it, so closing the session doesn't take crew-hubd with it."""
    if WINDOWS:
        import subprocess

        base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
            try:
                subprocess.Popen(argv, cwd=cwd or os.path.expanduser("~"), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                 creationflags=flags)
                return True
            except OSError:
                continue
        return False
    pid = os.fork()
    if pid:
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass
        return True
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        if devnull > 2:
            os.close(devnull)
        os.chdir(cwd or "/")
        os.execv(argv[0], argv)
    except Exception:
        pass
    os._exit(0)


# ---- public: files -------------------------------------------------------------------------------------------------
def append_bytes(path, data):
    """Append `data` in one write that concurrent appenders can't interleave with.

    POSIX: one os.write on an O_APPEND fd (unchanged). Windows: the C runtime emulates O_APPEND with seek-then-write,
    which two hooks can race, so open with FILE_APPEND_DATA only: then the file system appends each WriteFile
    atomically. Share mode includes delete so crew-hubd can rotate the spool while a hook holds it."""
    if WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            k = _k32()
            h = k.CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_ALL, None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
            if _bad_handle(h):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                done = wintypes.DWORD(0)
                if not k.WriteFile(h, data, len(data), ctypes.byref(done), None) or done.value != len(data):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                k.CloseHandle(h)
            return
        except ImportError:
            pass
    fd = os.open(path, O_BINARY | os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def fchmod(fd, mode):
    """os.fchmod where file modes mean something (not Windows)."""
    if not WINDOWS and hasattr(os, "fchmod"):
        os.fchmod(fd, mode)


def tighten(path, mode):
    """chmod `path` to `mode` if group/other bits are set. No-op on Windows (the profile ACL protects it).
    Returns True when it changed something."""
    if WINDOWS:
        return False
    import stat

    if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        os.chmod(path, mode)
        return True
    return False


def lock_exclusive(fd):
    """Non-blocking exclusive lock held until the fd closes. False when another process holds it.

    POSIX: flock (unchanged). Windows: msvcrt byte-range lock on one byte at LOCK_OFFSET (past the pid text, so
    the hook can still read the pid)."""
    if WINDOWS:
        import msvcrt

        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fd):
    """Release lock_exclusive() early (Windows asks for an explicit unlock before close; POSIX: close does it)."""
    if WINDOWS:
        import msvcrt

        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.lseek(fd, pos, os.SEEK_SET)


PIPE_PREFIX = "\\\\.\\pipe\\"


def is_pipe_path(path):
    return WINDOWS and isinstance(path, str) and path.lower().startswith(PIPE_PREFIX)


def pipe_exists(path):
    """Whether a Windows named pipe exists, by listing \\\\.\\pipe\\ (stat/exists may open a client connection)."""
    name = path[len(PIPE_PREFIX):].lower()
    try:
        return name in (n.lower() for n in os.listdir(PIPE_PREFIX))
    except OSError:
        return False


def socket_present(path):
    """The inbox endpoint exists: a Unix socket file, or a Windows named pipe."""
    if is_pipe_path(path):
        return pipe_exists(path)
    return os.path.exists(path)


def python_for_daemon():
    """The interpreter to run crew-hubd with: this one."""
    return sys.executable
