"""One place for process judgments: alive (True) / absent (False) / unknown (None). Never raises.

On Linux it uses /proc; elsewhere (macOS etc.) `ps -axww -o pid=,command=` (cached for 3 seconds) stands in. If neither is usable, everything is unknown.
"Unknown" is not treated as "ended": when it is None, the caller decides by whether the record is still growing.
The command line is used only for the judgment and is never sent out through the API (another process's arguments may hold secrets).
Lineage (ppid, start time, owner, the rollout each codex process has open) is also only read here: no signals are sent and no exceptions are raised (None when unknown)."""

import os
import re
import shutil
import subprocess
import threading
import time

PROC = '/proc'                    # hidden in tests (to imitate macOS)
PS_ARGV = ('ps', '-axww', '-o', 'pid=,command=')
PS_PPID_ARGV = ('ps', '-axww', '-o', 'pid=,ppid=')     # parent pid table when there is no /proc (everything at once, cached for 3 seconds)
PS_UID_ARGV = ('ps', '-axww', '-o', 'pid=,uid=')       # owner (effective user id) table when there is no /proc, the same way
PS_ENV_ARGV = ('ps', '-E', '-ww', '-o', 'command=', '-p')   # command line with the environment attached when there is no /proc (macOS; only the same user's processes). The pid is appended at the end
ENV_READ_MAX = 1 << 20                                   # cap on how much of environ is read
CACHE_SEC = 3

_lock = threading.Lock()
_PS = {'ts': 0.0, 'v': None}      # ps result {pid: command line (bytes)}. None if it could not be had (that is not asked again for 3 seconds either)
_CODEX = {'ts': 0.0, 'v': None}
_PPID = {'ts': 0.0, 'v': None}    # ps result {pid: ppid}
_UID = {'ts': 0.0, 'v': None}     # ps result {pid: uid}
_PIDS = {'ts': 0.0, 'v': None, 'k': None}     # per codex process {pid, argv, fds}
_ENV = {}                         # the ps way: {(pid, names): (time, values)}: holds only the values of the requested names (no other part of the environment is kept anywhere)


def reset():
    """Empties the caches (for tests)."""
    with _lock:
        _PS.update(ts=0.0, v=None)
        _CODEX.update(ts=0.0, v=None)
        _PPID.update(ts=0.0, v=None)
        _UID.update(ts=0.0, v=None)
        _PIDS.update(ts=0.0, v=None, k=None)
        _ENV.clear()


def has_proc():
    return os.path.isfile(os.path.join(PROC, 'self', 'cmdline'))


def method():
    """The detection method as a code: 'proc' | 'ps' | 'none' (neither is usable: every judgment is unknown). The start output words it from the dictionary (cli.start.method.<code>)."""
    if has_proc():
        return 'proc'
    return 'ps' if shutil.which('ps') else 'none'


def table_known():
    """Whether the process table can be read at all: /proc, or a working `ps`. False means every judgment about a process is "cannot tell" (the page says
    "process status unknown"), not "there is none"."""
    return has_proc() or _ps_table() is not None


def _run_ps():
    """ps output (bytes). None if it could not be had."""
    try:
        r = subprocess.run(PS_ARGV, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def _ps_table():
    """{pid: command line}. None if ps is unusable."""
    with _lock:
        if time.time() - _PS['ts'] < CACHE_SEC:
            return _PS['v']
        out, table = _run_ps(), None
        if out is not None:
            table = {}
            for line in out.splitlines():
                head, _, cmd = line.strip().partition(b' ')
                if head.isdigit():
                    table[int(head)] = cmd.strip()
        _PS.update(ts=time.time(), v=table)
        return table


def cmdline_has(pid, needle):
    """Whether the command line of process pid contains needle (bytes). True / False (no such process, or another command) / None (cannot tell)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if has_proc():
        try:
            with open('%s/%d/cmdline' % (PROC, pid), 'rb') as fh:
                return needle in fh.read()
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return False
        except OSError:
            return None
    table = _ps_table()
    if table is None:
        return None
    cmd = table.get(pid)
    return False if cmd is None else needle in cmd


def _is_codex(argv):
    return any(b'codex' in os.path.basename(a) for a in argv[:2])


def _proc_codex(sessions):
    """Scanning /proc: for each codex process {pid, argv, fds (set of open rollout paths)}. None if the list cannot be read."""
    try:
        pids = os.listdir(PROC)
    except OSError:
        return None
    real = os.path.realpath(sessions)
    out = []
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open('%s/%s/cmdline' % (PROC, pid), 'rb') as fh:
                argv = fh.read().split(b'\0')
        except OSError:
            continue
        if not _is_codex(argv):
            continue
        fds = set()
        try:
            for fd in os.listdir('%s/%s/fd' % (PROC, pid)):
                try:
                    link = os.readlink('%s/%s/fd/%s' % (PROC, pid, fd))
                except OSError:
                    continue
                if link.startswith(sessions + os.sep):
                    fds.add(link)
                elif link.startswith(real + os.sep):       # an fd link is the resolved real path: match even if a link sits in the record path
                    fds.add(sessions + link[len(real):])
        except OSError:
            pass
        out.append({'pid': int(pid), 'argv': argv, 'fds': fds})
    return out


def _proc_scan(sessions):
    """Scanning /proc: (codex command lines, open rollout paths). None if the list cannot be read."""
    items = _proc_codex(sessions)
    if items is None:
        return None
    return [b' '.join(i['argv']) for i in items], set().union(*(i['fds'] for i in items))


def codex_procs(sessions):
    """Command lines of codex processes and open rollout paths (cached for 3 seconds).
    known: whether the process list was obtained (if not, every judgment is unknown) / fd_known: whether open files can be known too (ps cannot)."""
    with _lock:
        c = _CODEX['v']
        if c is not None and time.time() - _CODEX['ts'] < CACHE_SEC and c['sessions'] == sessions:
            return c
    if has_proc():
        scan = _proc_scan(sessions)
        v = dict(cmd=scan[0], fds=scan[1], known=True, fd_known=True) if scan else dict(cmd=[], fds=set(), known=False, fd_known=False)
    else:
        table = _ps_table()
        cmds = [] if table is None else [c for c in table.values() if _is_codex(c.split(None, 2))]
        v = dict(cmd=cmds, fds=set(), known=table is not None, fd_known=False)
    v.update(any=bool(v['cmd']), sessions=sessions, ts=time.time())
    with _lock:
        _CODEX.update(ts=v['ts'], v=v)
    return v


# ---------- lineage: parent · start time · owner · command line · the rollout each process has open (read only, no exceptions, None when unknown) ----------
def _ok_pid(pid):
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _status_field(pid, key):
    """The values (a list of bytes) on the line `key:` (bytes, colon included) of /proc/<pid>/status. None for a missing process, an unreadable file or a missing line."""
    try:
        with open('%s/%d/status' % (PROC, pid), 'rb') as fh:
            for line in fh:
                if line.startswith(key):
                    return line[len(key):].split()
    except OSError:
        return None
    return None


def _ps_pairs(argv, cache):
    """{pid: number} from a two-column `ps` listing (pid, then ppid or uid), cached for 3 seconds in `cache`. None if ps is unusable (that is not asked again for 3 seconds either)."""
    with _lock:
        if time.time() - cache['ts'] < CACHE_SEC:
            return cache['v']
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        out = r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        out = None
    table = None
    if out is not None:
        table = {}
        for line in out.splitlines():
            f = line.split()
            if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
                table[int(f[0])] = int(f[1])
    with _lock:
        cache.update(ts=time.time(), v=table)
    return table


def _ps_ppids():
    """{pid: ppid}. None if ps is unusable."""
    return _ps_pairs(PS_PPID_ARGV, _PPID)


def ppid(pid):
    """Parent pid. On Linux the PPid of /proc/<pid>/status, elsewhere the `ps -o pid=,ppid=` table (cached for 3 seconds). None for a missing process or unknown."""
    if not _ok_pid(pid):
        return None
    if has_proc():
        f = _status_field(pid, b'PPid:')
        try:
            return int(f[0]) if f else None
        except ValueError:
            return None
    table = _ps_ppids()
    return None if table is None else table.get(pid)


def ancestors(pid, limit=64):
    """List of pids in the order pid's parent, that parent's parent, … (stops at 1 or below, a cycle, or a break)."""
    out, seen = [], {pid}
    p = ppid(pid)
    while p and p > 1 and p not in seen and len(out) < limit:
        out.append(p)
        seen.add(p)
        p = ppid(p)
    return out


def starttime(pid):
    """starttime in /proc/<pid>/stat (clock ticks since boot, the 22nd value). The same as the value Claude Code writes to procStart in the session file.
    Without /proc (macOS) there is nothing to compare, so None."""
    if not _ok_pid(pid) or not has_proc():
        return None
    try:
        with open('%s/%d/stat' % (PROC, pid), 'rb') as fh:
            raw = fh.read()
        return int(raw[raw.rindex(b')') + 2:].split()[19])      # comm may contain spaces or parentheses, so count from after the last `)`
    except (OSError, ValueError, IndexError):
        return None


def uid(pid):
    """Effective user id of the process: the second value of Uid in /proc/<pid>/status, elsewhere the `ps -o pid=,uid=` table (cached for 3 seconds). None for a missing process or unknown."""
    if not _ok_pid(pid):
        return None
    if has_proc():
        f = _status_field(pid, b'Uid:')
        try:
            return int(f[1]) if f and len(f) > 1 else None
        except ValueError:
            return None
    table = _ps_pairs(PS_UID_ARGV, _UID)
    return None if table is None else table.get(pid)


def argv(pid):
    """The command-line words of pid (a list of bytes). Without /proc, an approximation that splits the ps output on whitespace. None for a missing process or unknown."""
    if not _ok_pid(pid):
        return None
    if has_proc():
        try:
            with open('%s/%d/cmdline' % (PROC, pid), 'rb') as fh:
                raw = fh.read()
        except OSError:
            return None
        return raw.rstrip(b'\0').split(b'\0') if raw else None
    table = _ps_table()
    cmd = None if table is None else table.get(pid)
    return cmd.split() if cmd else None


def pid_ns():
    """Marker of this process's pid namespace (`pid:[4026531836]`). None if it cannot be read."""
    try:
        return os.readlink('%s/self/ns/pid' % PROC)
    except OSError:
        return None


def _is_codex_bin(argv_):
    """Whether it is the codex executable itself (`codex`, `codex.js`, or that file opened as the first argument by a launcher such as node). Not another tool such as `codex-view`."""
    return any(os.path.basename(a) in (b'codex', b'codex.js') for a in argv_[:2])


def codex_pids(sessions):
    """For each live codex process {pid, argv, fds (set of open rollout paths)} (cached for 3 seconds). None (unknown) without /proc or when the list cannot be read.
    ps does not know open files, so without /proc it is unknown."""
    with _lock:
        c = _PIDS
        if c['k'] == sessions and time.time() - c['ts'] < CACHE_SEC:
            return c['v']
    items = None
    if has_proc():
        scan = _proc_codex(sessions)
        if scan is not None:
            items = [i for i in scan if _is_codex_bin(i['argv'])]
    with _lock:
        _PIDS.update(ts=time.time(), v=items, k=sessions)
    return items


def _run_env_ps(pid):
    """The command line (one line of bytes) of pid with the environment attached, by `ps -E`. None if it could not be had."""
    try:
        r = subprocess.run(PS_ENV_ARGV + (str(pid),), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def env_values(pid, names):
    """Of the environment variables of process pid, only the values of names (bytes names), as {name (str): value (str)}. None (unknown) if it cannot be read (no such process, another user, or ps does not give the environment).
    If it was read but the name is absent, it is a dict missing only that name. environ holds secrets such as tokens, so values outside the requested names are dropped as soon as they are read
    and kept nowhere: not in the cache, the logs, or the returned value. On Linux /proc/<pid>/environ, elsewhere `ps -E` (for a process whose argv is known, only the part after it is taken as the environment)."""
    if not _ok_pid(pid):
        return None
    names = tuple(names)
    if has_proc():
        try:
            with open('%s/%d/environ' % (PROC, pid), 'rb') as fh:
                raw = fh.read(ENV_READ_MAX)
        except OSError:
            return None
        return _pick_env(raw, names, True)
    key = (pid, names)
    with _lock:
        c = _ENV.get(key)
        if c and time.time() - c[0] < CACHE_SEC:
            return c[1]
    table = _ps_table()
    cmd = None if table is None else table.get(pid)
    out = _run_env_ps(pid) if cmd else None
    got = None
    if out is not None and out.startswith(cmd):                 # the environment is appended after the command line. If the front is not the same, the two cannot be told apart
        rest = out[len(cmd):]
        if rest.strip():                                        # nothing appended: ps leaves out another user's environment (an empty one looks the same), so unknown, not "absent"
            got = _pick_env(rest, names, False)
    with _lock:
        if len(_ENV) > 256:
            _ENV.clear()
        _ENV[key] = (time.time(), got)
    return got


_ENV_VALUE = re.compile(rb'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9]{1,12}')      # the values we ask for: a session id or a pid


def _pick_env(raw, names, nul):
    """From raw (the environment blob), picks only `name=value` for each of names. nul: whether items are separated by NUL (environ) or by whitespace (ps).
    A name that stands twice with different values is unknown (left out). With ps the items are only blank-separated, so a value that holds blanks can hold
    text that looks like ` NAME=value`: a value counts only when it is an id or a number that ends at a blank or at the end."""
    out = {}
    before, value = (b'\0', rb'([^\0]*)') if nul else (rb'\s', rb'(\S*)')
    for n in names:
        found = {m.group(1) for m in re.finditer(rb'(?:\A|' + before + rb')' + re.escape(n) + b'=' + value, raw)}
        if len(found) != 1:
            continue                                                 # not there, or two different values
        v = next(iter(found))
        if not nul and not _ENV_VALUE.fullmatch(v):
            continue
        out[n.decode('ascii', 'replace')] = v.decode('utf-8', 'replace')
    return out
