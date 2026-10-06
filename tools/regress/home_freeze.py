#!/usr/bin/env python3
"""Freezes the HOME of a live board into a folder, so that two editions of the board can be run over the very same input, offline (home_replay.py), and compared (home_diff.py).

    python3.12 tools/regress/home_freeze.py --out <folder> [--port 8790] [--trace-code <board code folder>]... [--plan]

The live board is only ever asked with GET. Writes go to --out and nowhere else (not the repository, not HOME). Never opened or copied, by name: ~/.claude/.credentials.json, ~/.codex/auth.json, ~/.codex/*.sqlite*, .claude.json.
What is in the folder (CONTRACT 5.2):
  manifest.json      the moment T, the sessions (the live list), the size of every record at that moment, what was found unstable, the totals
  records/<path>     the records of the chosen sessions (main record, subagents/, .meta.json, the `claude -p` children linked to them, the Codex rollouts linked to them and their descendants), cut at their size at T
  root/<path>        the mirror of the rest of the disk the board looks at, under its ORIGINAL absolute path: the files whose bytes were wanted (.md, what a trace read) hold them, any other file is a sparse stand-in of the same size
  disk.json          for every path: kind, size, mtime_ns, ino, owner, link target, a folder's names; and "not there" for what was looked up and was absent (home_jail.py reads it)
  proc.json          the status the live board gave each agent, the environment tags of live processes, the orchestrator's own
  api_live/          the live /api/state of each session and the /api/agent of the agents that stand in a cell
  cache/agent-bullpen/links.json   the link cache
The folders of the records list only what was captured (the replay sees the world of the capture); every other folder lists everything it held.
`--trace-code` (repeatable): after the capture is made, the board edition in that folder is run in trace mode (home_replay.py --trace) and what it looked up that the capture lacks is added, until a run finds nothing new.
Memory and disk: a session is asked from the live board only while MemAvailable is above --min-free-mib; the capture stops (and says so) past --limit-bytes of copied bytes."""
import argparse
import datetime
import errno
import json
import os
import re
import shutil
import stat as _stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import home_jail  # noqa: E402

MANIFEST_VERSION = 1
LIMIT_BYTES = 2 << 30
MD_MAX = 32 << 20                 # a .md or a file a trace read is copied up to this size (bigger: a stand-in, said in the manifest and a miss in the replay)
LIST_STAT_MAX = 2000              # of a listed folder, this many children are looked at (the names are all kept)
LIST_MD_MAX = 200                 # and this many of its .md files are copied
TAG_NAMES = ('BULLPEN_ROOM', 'BULLPEN_SEAT')
ID_NAMES = ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID')
PATH_KEYS = ('root', 'dir', 'path', 'guide', 'table_path', 'brief', 'cwd', 'folder', 'file')
SAFE_NAME = re.compile(r'[A-Za-z0-9._-]+')
ROLLOUT_ID = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$')


class LimitExceeded(Exception):
    pass


# ---------- the live board: GET only ----------
class Live:
    def __init__(self, port, host='127.0.0.1', timeout=300):
        self.base, self.timeout = 'http://%s:%d' % (host, port), timeout

    def get(self, path):
        req = urllib.request.Request(self.base + path, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, None


def mem_available_mib():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


# ---------- what is on the live disk ----------
def observe(path):
    """The entry of a path as it is now (lstat; a link keeps its target)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {'t': 'absent', 'err': 'ENOENT'}
    except NotADirectoryError:
        return {'t': 'absent', 'err': 'ENOTDIR'}
    except PermissionError:
        return {'t': 'absent', 'err': 'EACCES'}
    except OSError as e:
        return {'t': 'absent', 'err': errno.errorcode.get(e.errno, 'ENOENT')}
    m = st.st_mode
    e = {'t': 'dir' if _stat.S_ISDIR(m) else 'file' if _stat.S_ISREG(m) else 'link' if _stat.S_ISLNK(m) else 'other', 'mode': m, 'ino': st.st_ino, 'dev': st.st_dev, 'nlink': st.st_nlink,
         'uid': st.st_uid, 'gid': st.st_gid, 'size': st.st_size, 'atime_ns': st.st_atime_ns, 'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns}
    if e['t'] == 'link':
        try:
            e['target'] = os.readlink(path)
        except OSError:
            e['target'] = ''
    if e['t'] == 'dir':
        e['names'] = None
    return e


def read_exact(src, dst, n):
    """Copies the first n bytes of src to dst (fewer if the file is shorter). Returns how many."""
    done = 0
    with open(src, 'rb') as f, open(dst, 'wb') as g:
        while done < n:
            chunk = f.read(min(1 << 20, n - done))
            if not chunk:
                break
            g.write(chunk)
            done += len(chunk)
    return done


class Capture:
    """The capture being built in a folder: `entries` (disk.json), the files, the byte count."""

    def __init__(self, out, limit=LIMIT_BYTES, max_file=MD_MAX):
        self.out, self.limit, self.max_file = os.path.abspath(out), limit, max_file
        self.entries = {}
        self.copied = 0
        self.unstable = []
        self.stand_ins_read = []                                 # a file a trace read whose bytes were too big to copy
        self.scoped = set()                                      # the folders that list only what was captured (the record folders)
        self.records = {}                                        # path -> {size, session, kind}
        self.changes = 0                                         # counts what a pass added (the trace loop stops when a run adds none)

    # --- entries ---
    def see(self, path):
        e = self.entries.get(path)
        if e is None:
            if home_jail.is_forbidden(path):
                return {'t': 'absent', 'err': 'EACCES'}
            e = self.entries[path] = observe(path)
            self.changes += 1
        return e

    def ensure(self, path, follow=True):
        """Makes the entries that a lookup of `path` needs (every component, every link and its target) and returns the final path, or None when the path leads nowhere."""
        comps, stack, i, hops = path.split('/'), [], 0, 0
        while i < len(comps):
            c = comps[i]
            i += 1
            if c in ('', '.'):
                continue
            if c == '..':
                if stack:
                    stack.pop()
                continue
            cand = '/' + '/'.join(stack + [c])
            e = self.see(cand)
            rest = [x for x in comps[i:] if x not in ('', '.')]
            if e['t'] == 'absent':
                return None
            if e['t'] == 'link' and (follow or rest):
                hops += 1
                if hops > home_jail.MAX_HOPS:
                    return None
                if e.get('target', '').startswith('/'):
                    stack = []
                comps, i = e.get('target', '').split('/') + rest, 0
                continue
            if e['t'] in ('dir', 'link'):
                stack.append(c)
                if not rest:
                    return cand
                continue
            return None if rest else cand
        self.see('/')
        return '/' + '/'.join(stack)

    def listing(self, path, with_children=True):
        """Keeps the names of a folder as they are now; looks at (and copies the .md of) a bounded number of its children."""
        final = self.ensure(path)
        if final is None or self.entries[final]['t'] != 'dir':
            return
        e = self.entries[final]
        if e.get('names') is None:
            try:
                e['names'] = sorted(n for n in os.listdir(final) if not home_jail.is_forbidden(n))
            except OSError:
                e['names'] = []
            self.changes += 1
        if with_children:
            md = 0
            for n in e['names'][:LIST_STAT_MAX]:
                child = final.rstrip('/') + '/' + n
                ce = self.see(child)
                if n.endswith('.md') and ce['t'] == 'file' and md < LIST_MD_MAX:
                    md += 1
                    self.content(child)

    # --- bytes ---
    def store_path(self, final, record=False):
        return ('records' if record else 'root') + final

    def content(self, path, exact=None, record=False):
        """Copies the bytes of a file (following links) into the mirror; `exact` cuts at that many bytes (a record, at its size at T). Returns the final path."""
        final = self.ensure(path)
        if final is None:
            return None
        e = self.entries[final]
        if e['t'] != 'file':
            return final
        store = self.store_path(final, record)
        if e.get('copied') and e.get('store') == store:
            return final
        if exact is None and e['size'] > self.max_file:
            e['copied'] = False                                       # too big to keep: the stand-in has the size, a read of it is a miss
            self.stand_in(final, e)
            self.stand_ins_read.append(final)
            return final
        if self.copied + (e['size'] if exact is None else exact) > self.limit:
            raise LimitExceeded('over %d bytes while copying %s' % (self.limit, final))
        dest = os.path.join(self.out, store)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        before, got = e, 0
        for attempt in range(3):
            before = observe(final)
            if before['t'] != 'file':
                return final                                          # gone while it was being copied
            n = exact if exact is not None else before['size']
            try:
                got = read_exact(final, dest, n)
            except OSError:
                return final
            after = observe(final)
            if exact is not None:                                     # a record only grows: its first n bytes are what they were as long as it is the same file
                stable = after['t'] == 'file' and after['ino'] == before['ino'] and after['size'] >= n and got == n
            else:
                stable = after['t'] == 'file' and (after['size'], after['mtime_ns']) == (before['size'], before['mtime_ns']) and got == n
            if stable:
                break
        else:
            self.unstable.append(final)                               # changed under the copy three times: reported, and the session is left out of the comparison
        if exact is None:
            e.update({k: before[k] for k in ('mtime_ns', 'atime_ns', 'ctime_ns', 'ino') if k in before})
            e['size'] = got
        e['store'], e['copied'] = store, True
        if final in self.stand_ins_read:
            self.stand_ins_read.remove(final)                         # it was too big once (or the limit was lower): it is kept now
        self.copied += got
        self.changes += 1
        return final

    def stand_in(self, final, e):
        """A sparse file of the same size: a stat is right, a read is a miss (the jail says so)."""
        dest = os.path.join(self.out, self.store_path(final))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.truncate(e['size'])
        e['store'] = self.store_path(final)

    def finish_stand_ins(self):
        for p, e in self.entries.items():
            if e['t'] == 'file' and not e.get('store'):
                e['copied'] = False
                self.stand_in(p, e)

    # --- the records ---
    def add_record(self, path, size, session, kind):
        """A record, cut at `size` (its size at T)."""
        final = self.ensure(path)
        if final is None or self.entries[final]['t'] != 'file':
            return False
        e = self.entries[final]
        e['size'] = size
        self.content(final, exact=size, record=True)
        self.records[final] = {'size': size, 'session': session, 'kind': kind}
        d = os.path.dirname(final)
        while d and d != '/':
            self.scoped.add(d)
            d = os.path.dirname(d)
        return True

    def scope_listings(self, roots):
        """The folders of the record space list what was captured: the entries that stand directly in them."""
        kids = {}
        for k, v in self.entries.items():
            if v['t'] != 'absent':
                kids.setdefault(os.path.dirname(k), set()).add(os.path.basename(k))
        for d in self.scoped:
            if not any(d == r or d.startswith(r.rstrip('/') + '/') for r in roots):
                continue
            e = self.entries.get(d)
            if e and e['t'] == 'dir':
                e['names'] = sorted(kids.get(d, ()))

    def mirror_dirs(self):
        for p, e in self.entries.items():
            if e['t'] == 'dir':
                os.makedirs(os.path.join(self.out, 'root') + p, exist_ok=True)

    def write_disk(self, roots=()):
        self.scope_listings(roots)
        self.finish_stand_ins()
        self.mirror_dirs()
        with open(os.path.join(self.out, 'disk.json'), 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'entries': self.entries}, f, sort_keys=True, ensure_ascii=False)


# ---------- choosing and finding ----------
def choose_sessions(listing, wanted):
    items = [s for s in listing.get('sessions') or [] if isinstance(s, dict) and s.get('id')]
    if wanted:
        by = {s['id']: s for s in items}
        return [by.get(w) or {'id': w, 'provider': 'claude', 'agents': 0} for w in wanted]
    seen, out = set(), []
    for s in items:
        if s.get('agents') and not s.get('solo') and s['id'] not in seen:
            seen.add(s['id'])
            out.append(s)
    return out


def claude_index(projects):
    """{session id: record path} of every Claude record in the projects folder (names only)."""
    out = {}
    try:
        for proj in os.listdir(projects):
            d = os.path.join(projects, proj)
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                if n.endswith('.jsonl') and SAFE_NAME.fullmatch(n):
                    out.setdefault(n[:-6], os.path.join(d, n))
    except OSError:
        pass
    return out


def codex_index(sessions):
    """({thread id: rollout path}, {thread id: parent thread id}) of the Codex rollouts: the id from the name, the parent from the first line."""
    paths, parents = {}, {}
    try:
        for y in os.listdir(sessions):
            for m in os.listdir(os.path.join(sessions, y)):
                for d in os.listdir(os.path.join(sessions, y, m)):
                    for n in os.listdir(os.path.join(sessions, y, m, d)):
                        mt = ROLLOUT_ID.search(n) if n.startswith('rollout-') else None
                        if mt:
                            paths[mt.group(1)] = os.path.join(sessions, y, m, d, n)
    except OSError:
        pass
    for tid, p in paths.items():
        try:
            with open(p, 'rb') as f:
                meta = json.loads(f.readline(4 << 20)).get('payload') or {}
            par = meta.get('parent_thread_id')
            if isinstance(par, str) and par:
                parents[tid] = par
        except (OSError, ValueError, AttributeError):
            pass
    return paths, parents


def record_files(main, kind):
    """[(path, kind)] of one Claude record and what stands under its folder (subagents)."""
    out = [(main, kind)]
    sub = main[:-6] + '/subagents'
    try:
        for n in sorted(os.listdir(sub)):
            if SAFE_NAME.fullmatch(n) and (n.endswith('.jsonl') or n.endswith('.meta.json')):
                out.append((os.path.join(sub, n), 'sub'))
    except OSError:
        pass
    return out


def closure(sid, state, c_index, x_paths, x_parents):
    """[(record path, kind)] of one session: its record, the records of the agents it shows (a `claude -p` child, a Codex thread), the descendants of those Codex threads."""
    ids = {sid} | {a.get('id') for a in (state or {}).get('agents') or [] if isinstance(a.get('id'), str)}
    out, seen = [], set()
    codex_ids = {i for i in ids if i in x_paths}
    grew = True
    while grew:
        grew = False
        for t, par in x_parents.items():
            if par in codex_ids and t not in codex_ids:
                codex_ids.add(t)
                grew = True
    for i in sorted(ids):
        if i in c_index and c_index[i] not in seen:
            for p, k in record_files(c_index[i], 'main' if i == sid else 'child'):
                if p not in seen:
                    seen.add(p)
                    out.append((p, k))
    for t in sorted(codex_ids):
        if x_paths[t] not in seen:
            seen.add(x_paths[t])
            out.append((x_paths[t], 'codex'))
    return out


def state_paths(state, home):
    """The absolute paths a state names (roots, folders, cells, finals, tables, guides)."""
    found = set()

    def walk(x, key=None):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and key in PATH_KEYS and 1 < len(x) < 4096 and '\n' not in x:
            if x.startswith('~/') or x == '~':
                x = home + x[1:]
            if x.startswith('/'):
                found.add(os.path.normpath(x))
    walk(state)
    return sorted(found)


def live_env(names=TAG_NAMES + ID_NAMES):
    """[{pid, env: {tag name: value}}] of the live claude/codex processes of this user that carry a tag (BULLPEN_ROOM / BULLPEN_SEAT); every other value is dropped as soon as it is read."""
    out, me = [], os.getuid()
    try:
        pids = [int(n) for n in os.listdir('/proc') if n.isdigit()]
    except OSError:
        return out
    for pid in sorted(pids):
        try:
            if os.stat('/proc/%d' % pid).st_uid != me:
                continue
            with open('/proc/%d/cmdline' % pid, 'rb') as f:
                cmd = f.read(4096)
            if b'claude' not in cmd and b'codex' not in cmd:
                continue
            with open('/proc/%d/environ' % pid, 'rb') as f:
                raw = f.read(1 << 20)
        except OSError:
            continue
        env = {}
        for item in raw.split(b'\0'):
            k, _, v = item.partition(b'=')
            if k.decode('ascii', 'replace') in names:
                env[k.decode('ascii')] = v.decode('utf-8', 'replace')
        del raw
        if any(n in env for n in TAG_NAMES):
            out.append({'pid': pid, 'env': {n: env[n] for n in TAG_NAMES + ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID') if n in env}})
    return out


def own_top_of(port):
    """The folder the live board runs from (the repository top above its server.py), from /proc: the process that listens on the port. None when it cannot be told."""
    try:
        sockets = set()
        for table in ('/proc/net/tcp', '/proc/net/tcp6'):
            try:
                with open(table) as f:
                    for line in list(f)[1:]:
                        p = line.split()
                        if p[3] == '0A' and int(p[1].split(':')[1], 16) == port:
                            sockets.add('socket:[%s]' % p[9])
            except OSError:
                pass
        for pid in (n for n in os.listdir('/proc') if n.isdigit()):
            try:
                if not any(os.readlink('/proc/%s/fd/%s' % (pid, fd)) in sockets for fd in os.listdir('/proc/%s/fd' % pid)):
                    continue
                with open('/proc/%s/cmdline' % pid, 'rb') as f:
                    words = [w.decode('utf-8', 'replace') for w in f.read().split(b'\0')]
                srv = next((w for w in words if w.endswith('server.py') or w.endswith('run_board.py')), None)
                if srv and not os.path.isabs(srv):
                    srv = os.path.join(os.readlink('/proc/%s/cwd' % pid), srv)
                if srv:
                    d = os.path.dirname(os.path.realpath(srv))
                    while d != '/' and not os.path.exists(os.path.join(d, '.git')):
                        d = os.path.dirname(d)
                    return d if d != '/' else None
            except OSError:
                continue
    except (OSError, ValueError, IndexError):
        pass
    return None


def inside(path, folder):
    return folder and (path == folder or path.startswith(folder.rstrip('/') + '/'))


def run_trace(python, code, cap, name, rounds_dir):
    """One trace run of a board edition over the capture folder; returns ({touched path: info}, jail_miss list)."""
    out = os.path.join(rounds_dir, name + '.json')
    touched = os.path.join(rounds_dir, name + '.touched.json')
    r = subprocess.run([python, '-I', os.path.join(HERE, 'home_replay.py'), '--trace', '--code', code, '--cap', cap, '--out', out, '--touched', touched, '--no-raw'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = r.stdout.decode('utf-8', 'replace')
    try:
        with open(touched, encoding='utf-8') as f:
            t = json.load(f)
        with open(out + '.jail_miss.json', encoding='utf-8') as f:
            miss = json.load(f)
    except (OSError, ValueError):
        raise SystemExit('trace run failed (%s):\n%s' % (name, log[-2000:]))
    return t, miss, log


def cell_agents(state):
    """The agents that stand in a cell of a live state (the ones /api/agent is read for)."""
    ids = set()
    for d in state.get('debates') or []:
        for t in d.get('topics') or []:
            for r in t.get('rows') or []:
                for c in r.get('cells') or []:
                    for k in ('agent', 'owner', 'writer'):
                        if isinstance(c.get(k), str):
                            ids.add(c[k])
                    ids.update(x for x in c.get('editors') or [] if isinstance(x, str))
    return ids


def load_capture(out, limit, max_file):
    """The Capture of a folder that home_freeze.py made before (to add a trace of another edition of the board), and its manifest."""
    with open(os.path.join(out, 'manifest.json'), encoding='utf-8') as f:
        manifest = json.load(f)
    cap = Capture(out, limit, max_file)
    with open(os.path.join(out, 'disk.json'), encoding='utf-8') as f:
        cap.entries = json.load(f)['entries']
    cap.records, cap.unstable, cap.copied = manifest.get('records') or {}, list(manifest.get('unstable') or ()), manifest.get('copied_bytes', 0)
    cap.stand_ins_read = [p for p in manifest.get('stand_ins_read') or () if not cap.entries.get(p, {}).get('copied')]
    for p in cap.records:
        d = os.path.dirname(p)
        while d and d != '/':
            cap.scoped.add(d)
            d = os.path.dirname(d)
    return cap, manifest


def trace_rounds(cap, out, roots, python, codedirs, max_rounds, save, rounds):
    """What each board edition looks up that the capture does not hold is added, until a run finds nothing new."""
    os.makedirs(os.path.join(out, 'trace'), exist_ok=True)
    for codedir in codedirs:
        name = os.path.basename(os.path.abspath(codedir)) or 'code'
        n0 = sum(1 for r in rounds if r['code'] == name)
        for rnd in range(n0, n0 + max_rounds):
            touched, miss, log = run_trace(python, os.path.abspath(codedir), out, '%s_%d' % (name, rnd), os.path.join(out, 'trace'))
            before = cap.changes
            for p, info in sorted(touched.items()):
                if home_jail.is_forbidden(p) or any(inside(p, r) for r in roots):
                    continue
                final = cap.ensure(p)
                if final is None:
                    continue
                if info.get('listed'):
                    cap.listing(final, with_children=False)
                if info.get('read'):
                    cap.content(final)
            grew = cap.changes - before
            rounds.append({'code': name, 'round': rnd, 'touched': len(touched), 'added': grew, 'jail_miss': sum(m['count'] for m in miss if m['kind'] != 'forbidden_stat')})
            print('  trace %s round %d: %d path(s) read from the live disk, %d added, jail_miss %d' % (name, rnd, len(touched), grew, rounds[-1]['jail_miss']), flush=True)
            cap.write_disk(roots)
            save({'trace_rounds': rounds})
            if not grew:
                break


def main(argv=None):
    ap = argparse.ArgumentParser(description='Freeze the HOME of a live board (GET only) into a folder.')
    ap.add_argument('--out', help='the folder to make (it must not exist or must be empty); NOT inside the repository or HOME\'s .claude / .codex')
    ap.add_argument('--extend', help='a capture made before: add the trace of --trace-code to it (nothing is read from the live board again)')
    ap.add_argument('--port', type=int, default=8790)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--session', action='append', help='a session id to freeze (repeatable; default: every listed session that has agents)')
    ap.add_argument('--home', default=os.path.expanduser('~'))
    ap.add_argument('--claude-home', help='default: <home>/.claude')
    ap.add_argument('--codex-home', help='default: <home>/.codex')
    ap.add_argument('--cache-home', help='where the live board keeps agent-bullpen/links.json (default: $XDG_CACHE_HOME, else <home>/.cache)')
    ap.add_argument('--board-top', help='the folder the live board runs from (default: found through the process that listens on the port)')
    ap.add_argument('--trace-code', action='append', default=[], help='a board code folder to trace over the capture (repeatable)')
    ap.add_argument('--python', default=sys.executable, help='the interpreter of the trace runs (3.11 or later)')
    ap.add_argument('--extra-paths', help='a file with one more absolute path per line to add')
    ap.add_argument('--limit-bytes', type=int, default=LIMIT_BYTES)
    ap.add_argument('--max-file-bytes', type=int, default=MD_MAX, help='a .md, or a file a trace read, is kept in full up to this size')
    ap.add_argument('--min-free-mib', type=int, default=2048)
    ap.add_argument('--max-rounds', type=int, default=6)
    ap.add_argument('--no-proc-env', action='store_true', help='do not read the environment tags (BULLPEN_ROOM / BULLPEN_SEAT) of live processes')
    ap.add_argument('--plan', action='store_true', help='only say what would be captured and how big it is')
    a = ap.parse_args(argv)
    home = os.path.abspath(a.home)
    claude_home = os.path.abspath(a.claude_home or os.path.join(home, '.claude'))
    codex_home = os.path.abspath(a.codex_home or os.path.join(home, '.codex'))
    projects, csessions = os.path.join(claude_home, 'projects'), os.path.join(codex_home, 'sessions')
    repo = os.path.realpath(os.path.join(HERE, '..', '..'))
    if a.extend:
        out = os.path.realpath(a.extend)
        if not a.trace_code:
            ap.error('--extend needs --trace-code')
        cap, manifest = load_capture(out, a.limit_bytes, a.max_file_bytes)
        roots = (os.path.join(manifest['claude_home'], 'projects'), os.path.join(manifest['codex_home'], 'sessions'))
        rounds = list(manifest.get('trace_rounds') or [])

        def save_ext(extra=None):
            manifest.update(copied_bytes=cap.copied, entries=len(cap.entries), unstable=sorted(set(cap.unstable)), stand_ins_read=sorted(set(cap.stand_ins_read)), **(extra or {}))
            with open(os.path.join(out, 'manifest.json'), 'w', encoding='utf-8') as f:
                json.dump(manifest, f, sort_keys=True, ensure_ascii=False, indent=1)
        try:
            trace_rounds(cap, out, roots, a.python, a.trace_code, a.max_rounds, save_ext, rounds)
        except LimitExceeded as e:
            save_ext({'aborted': str(e)})
            raise SystemExit('stopped: %s' % e)
        cap.write_disk(roots)
        save_ext({'trace_rounds': rounds})
        print('extended: %s (%.1f MiB copied, %d entries)' % (out, cap.copied / 1048576.0, len(cap.entries)), flush=True)
        return 0
    if not a.plan:
        if not a.out:
            ap.error('--out is required')
        out = os.path.realpath(a.out)
        if inside(out, repo) or inside(out, os.path.realpath(claude_home)) or inside(out, os.path.realpath(codex_home)) or out == os.path.realpath(home):
            ap.error('--out may not be inside the repository, the Claude or Codex folder, or be HOME itself')
        if os.path.exists(out) and os.listdir(out):
            ap.error('--out exists and is not empty')
    live = Live(a.port, a.host)
    T = time.time()
    code, listing = live.get('/api/sessions')
    if code != 200 or not isinstance(listing, dict):
        raise SystemExit('the live board does not answer /api/sessions (%s)' % code)
    chosen = choose_sessions(listing, a.session)
    print('%d session(s) chosen' % len(chosen), flush=True)
    c_index = claude_index(projects)
    x_paths, x_parents = codex_index(csessions)
    states, plan = {}, {}
    for s in chosen:
        free = mem_available_mib()
        if free is not None and free < a.min_free_mib:
            raise SystemExit('MemAvailable %d MiB is under --min-free-mib %d: not asking the live board for another session' % (free, a.min_free_mib))
        code, st = live.get('/api/state?session=' + urllib.parse.quote(s['id']))
        if code != 200 or not isinstance(st, dict):
            print('  %s: /api/state answered %s, left out' % (s['id'][:8], code), flush=True)
            continue
        states[s['id']] = st
        files = closure(s['id'], st, c_index, x_paths, x_parents)
        sizes = {}
        for p, k in files:
            try:
                sizes[p] = (os.stat(p).st_size, k)
            except OSError:
                pass
        plan[s['id']] = sizes
        print('  %s: %d agents, %d record file(s), %.1f MiB' % (s['id'][:8], len(st.get('agents') or []), len(sizes), sum(v[0] for v in sizes.values()) / 1048576.0), flush=True)
    total = sum(v[0] for sizes in plan.values() for v in sizes.values())
    print('records in all: %.1f MiB (limit %.1f MiB)' % (total / 1048576.0, a.limit_bytes / 1048576.0), flush=True)
    if a.plan:
        return 0 if total <= a.limit_bytes else 2
    if total > a.limit_bytes:
        raise SystemExit('the records alone are over --limit-bytes: nothing was copied')
    cap = Capture(out, a.limit_bytes, a.max_file_bytes)
    os.makedirs(out, exist_ok=True)
    board_top = a.board_top or own_top_of(a.port)
    sess_items = [s for s in chosen if s['id'] in states]
    # api_live and the agents of the cells
    for s in sess_items:
        sid = s['id']
        st = states[sid]
        d = os.path.join(out, 'api_live', sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'state.json'), 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        for aid in sorted(cell_agents(st)):
            if not SAFE_NAME.fullmatch(aid):
                continue
            code, det = live.get('/api/agent?id=%s&session=%s' % (urllib.parse.quote(aid), urllib.parse.quote(sid)))
            if code == 200 and det is not None:
                with open(os.path.join(d, 'agent_%s.json' % aid), 'w', encoding='utf-8') as f:
                    json.dump(det, f, ensure_ascii=False)
    roots = (projects, csessions)
    rounds = []
    manifest = {'version': MANIFEST_VERSION, 'T': T, 'created_utc': datetime.datetime.fromtimestamp(T, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'home': home, 'claude_home': claude_home,
                'codex_home': codex_home, 'board_top': board_top, 'port': a.port, 'python': '%d.%d.%d' % sys.version_info[:3], 'sessions': sess_items, 'limit_bytes': a.limit_bytes}

    def save(extra=None):
        manifest.update(records=cap.records, unstable=sorted(set(cap.unstable)), stand_ins_read=sorted(set(cap.stand_ins_read)), copied_bytes=cap.copied, entries=len(cap.entries), **(extra or {}))
        manifest['unstable_sessions'] = sorted({cap.records[p]['session'] for p in cap.unstable if p in cap.records})
        with open(os.path.join(out, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, sort_keys=True, ensure_ascii=False, indent=1)
    try:
        # the records, cut at their size now
        for sid, sizes in plan.items():
            for p in sizes:
                try:
                    size_now = os.stat(p).st_size
                except OSError:
                    continue
                cap.add_record(p, size_now, sid, sizes[p][1])
        # the folders and files the live states name
        for st in states.values():
            for p in state_paths(st, home):
                final = cap.ensure(p)
                if final is None:
                    continue
                if cap.entries[final]['t'] == 'dir':
                    cap.listing(final)
                elif final.endswith('.md'):
                    cap.content(final)
                d = os.path.dirname(final)
                for _ in range(3):
                    cap.listing(d, with_children=False)
                    d = os.path.dirname(d)
        if a.extra_paths:
            with open(a.extra_paths, encoding='utf-8') as f:
                for line in f:
                    p = line.strip()
                    if p.startswith('/'):
                        final = cap.ensure(p)
                        if final and cap.entries[final]['t'] == 'file':
                            cap.content(final)
                        elif final:
                            cap.listing(final)
        cache = os.path.join(a.cache_home or os.environ.get('XDG_CACHE_HOME') or os.path.join(home, '.cache'), 'agent-bullpen', 'links.json')
        if os.path.isfile(cache):
            dest = os.path.join(out, 'cache', 'agent-bullpen')
            os.makedirs(dest, exist_ok=True)
            shutil.copyfile(cache, os.path.join(dest, 'links.json'))
        proc = {'agents': {sid: {x['id']: {'status': x.get('status'), 'reason': x.get('reason'), 'resets_at': x.get('resets_at')} for x in st.get('agents') or [] if x.get('id')}
                           for sid, st in states.items()},
                'orch': {sid: {'state': (st.get('orch') or {}).get('state')} for sid, st in states.items()}, 'env': [] if a.no_proc_env else live_env()}
        with open(os.path.join(out, 'proc.json'), 'w', encoding='utf-8') as f:
            json.dump(proc, f, sort_keys=True, ensure_ascii=False)
        cap.write_disk(roots)
        save()
        trace_rounds(cap, out, roots, a.python, a.trace_code, a.max_rounds, save, rounds)
    except LimitExceeded as e:
        save({'aborted': str(e)})
        raise SystemExit('stopped: %s (the folder holds the part made so far, manifest.json says so)' % e)
    cap.write_disk(roots)
    save({'trace_rounds': rounds})
    print('frozen: %s (%.1f MiB copied, %d entries, %d unstable)' % (out, cap.copied / 1048576.0, len(cap.entries), len(set(cap.unstable))), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
