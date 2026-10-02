"""Links child sessions through the process lineage: a live `claude -p` child session, and a rollout opened by a `codex` process, are walked up through their parents
and attached to the session of the nearest Claude ancestor that has `~/.claude/sessions/<pid>.json`. This is firmer evidence than the command-string, time and cwd rules, so it wins when it conflicts with them.

When Bash runs a script file that starts a child session (`bash run.sh` → `claude -p …`), the record keeps only the script name, but the process tree
still has the ancestors. A link once made is remembered after the process ends: in memory while the server is up, and firm links also in the link cache
(`links.json`, session ids only; off with `--no-link-cache`), which is read back at the next start.

A detached child (`setsid nohup claude -p … &`) has its parent changed to init (1) and cannot be found through the ancestors. But the environment of a process that the Bash tool started
still carries the session id (`CLAUDE_CODE_SESSION_ID`) and pid (`CLAUDE_PID`) of the Claude that started it. This is the firmest evidence, ahead of the ancestors.
environ holds secrets, so only those two values are taken out and nothing else is kept in storage, logs or responses (procs.env_values).

Conditions that prevent a false link (all read-only):
- The session file is named `<pid>.json` and that pid must be a claude process now. On Linux `procStart` must equal the start time in /proc (pid reuse),
  the process owner must equal the file owner (another user's file), and the pid namespace in `pidDomain` must equal this server's.
- Both the parent and the child session must have a record (jsonl) (otherwise there is nowhere to show it on the page).
- If a process that the user opened directly for the same session (not `-p`) is alive separately, it is not linked (that session is the one the user is watching).
- A cycle (B is a child of A and A is a child of B) is not made.
- If the session id in the environment is the process's own session, it is ignored; and if the session of the live Claude process that the environment's `CLAUDE_PID` points to differs from that id (the session was switched),
  it is not used. If the `CLAUDE_PID` process is already gone (the parent ended first), only the session id is trusted."""

import glob
import json
import os
import re
import stat
import time

from . import procs
from .facts import NODE_ID_RE
from .util import CLAUDE_HOME, CODEX_SESSIONS, HOME, SID_RE

SESSION_FILE_MAX = 64 << 10                 # read cap for one session file
SESSION_FILE_RE = re.compile(r'(\d+)\.json')
ROLLOUT_ID_RE = re.compile(r'(%s)\.jsonl' % SID_RE.pattern)       # rollout-<time>-<thread id>.jsonl
ENV_NAMES = (b'CLAUDE_CODE_SESSION_ID', b'CLAUDE_PID')           # only these two are read from environ

# record of the links found by firm rules (so they are not forgotten across a restart). It holds only session ids, rule and time (no path, instruction, fingerprint or environment
# variable value). Version 2 adds the links that rest on an output file (`out`) or a long instruction (`content`), with the sub-agent id when the node is one; version 1 files are still read.
LINK_CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache'), 'agent-bullpen', 'links.json')
CACHE_VERSION = 2
CACHE_READ_VERSIONS = (1, 2)
CACHE_MAX = 2000                  # cap on the number of pairs (the oldest are dropped first)
CACHE_DAYS = 90                   # entries first seen longer ago than this are cleaned up
CACHE_FILE_MAX = 2 << 20          # cap on the file read
CACHE_RETRY_SEC = 60              # retry after this long if a write fails
CACHE_RULES = ('proc', 'env', 'out', 'content')     # rules that are kept: lineage, environment variable, output file, long instruction (the short-instruction and time rules are guesses, so they are not kept)
SAVED_RULES = ('out', 'content')  # of those, the ones decided by the affiliation judgment (board/affil.py) and handed in through remember(); proc and env are seen by this module itself


def _read_json(path):
    """The JSON dict of a regular file (not a link or FIFO) and the file's owner. None if it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > SESSION_FILE_MAX:
            return None
        with os.fdopen(fd, 'rb') as fh:
            fd = -1
            d = json.loads(fh.read(SESSION_FILE_MAX + 1))
    except (OSError, ValueError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    return (d, st.st_uid) if isinstance(d, dict) else None


def holds(pid, d, owner, ns):
    """Whether the claude process of that pid now is the one that wrote this session file: True, False (no such process, another command, a reused pid, another user,
    another pid namespace) or None (the process is there but its start time or owner cannot be read; nothing says it is the same one). The one check both the links
    (`_mine`) and the page's state (sessions.alive, session_procs) make."""
    r = procs.cmdline_has(pid, b'claude')
    if r is not True:
        return r                                      # False: gone or another command; None: the table cannot be read
    if not procs.has_proc():
        u = procs.uid(pid)                            # ps names the owner but not the start time
        return u is None or u == owner                # an owner that cannot be told: trust only the judgment above (a live claude)
    st, uid = procs.starttime(pid), procs.uid(pid)
    if st is None or uid is None:
        return None
    if uid != owner:
        return False
    ps = d.get('procStart')
    if isinstance(ps, (str, int)) and not isinstance(ps, bool) and str(ps).isdigit() and int(ps) != st:
        return False                                  # a different process with the same pid
    dom = d.get('pidDomain')
    if ns and isinstance(dom, str) and 'pid:[' in dom and not dom.endswith(ns):
        return False                                  # a pid from another pid namespace (a container etc.) is not this server's pid
    return True


def _mine(pid, d, owner, ns):
    """Whether this session file was really written by the claude process of that pid now (not a reused pid, another user, or another pid namespace). What cannot be
    told is not a link."""
    return holds(pid, d, owner, ns) is True


def session_files():
    """[(pid, d, owner)] of ~/.claude/sessions/<pid>.json: the regular files that name their own pid, with the owner of the file. Others are skipped."""
    out = []
    for f in glob.glob(os.path.join(CLAUDE_HOME, 'sessions', '*.json')):
        m = SESSION_FILE_RE.fullmatch(os.path.basename(f))
        got = _read_json(f) if m else None
        if got and got[0].get('pid') == int(m.group(1)):
            out.append((int(m.group(1)), got[0], got[1]))
    return out


def claims():
    """[(pid, d, alive)] of the session files, alive being `holds` (True / False / None). The page's state reads the process of a session through this."""
    ns = procs.pid_ns()
    return [(pid, d, holds(pid, d, owner, ns)) for pid, d, owner in session_files()]


def claude_alive_ids():
    """Ids of the Claude sessions that cannot be called finished: a process that holds the session file, or one that cannot be told apart from it (`holds` is not False).
    A nonexistent process, another command's, a reused pid, another user's or another pid namespace's is left out."""
    return {d.get('sessionId') for _, d, alive in claims() if alive is not False}


def read_sessions():
    """{pid: {pid, sid, cwd, ts, print}}: of sessions/<pid>.json, the ones that really belong to that process now (`_mine`).
    print: whether the process was started by `claude -p` (or the SDK). ts: process start time (seconds)."""
    out, ns = {}, procs.pid_ns()
    for pid, d, owner in session_files():
        sid = d.get('sessionId')
        if not isinstance(sid, str) or not SID_RE.fullmatch(sid) or not _mine(pid, d, owner, ns):
            continue
        at = d.get('startedAt')
        out[pid] = {'pid': pid, 'sid': sid, 'cwd': d.get('cwd') if isinstance(d.get('cwd'), str) else None,
                    'ts': at / 1000 if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 else None,
                    'print': d.get('entrypoint') == 'sdk-cli' or any(a in (b'-p', b'--print') for a in (procs.argv(pid) or [])[1:])}
    return out


def nearest(pid, sessions):
    """The nearest of pid's ancestors that has a valid session file (None if none)."""
    for a in procs.ancestors(pid):
        if a in sessions:
            return sessions[a]
    return None


def env_parent(pid, own, sess):
    """The parent Claude session id that the environment of process pid points to, else None (environment unreadable or absent, its own session, or inconsistent with `CLAUDE_PID`).
    own: that process's own session id (None if none)."""
    env = procs.env_values(pid, ENV_NAMES)
    if not env:
        return None
    sid, cpid = env.get('CLAUDE_CODE_SESSION_ID'), env.get('CLAUDE_PID')
    if not sid or not SID_RE.fullmatch(sid) or sid == own:
        return None
    if cpid and cpid.isdigit() and int(cpid) != pid:
        rec = sess.get(int(cpid))
        if rec and rec['sid'] != sid:
            return None
    return sid


def parent_session(pid, own, sess, sids):
    """The parent Claude session of pid: (session id, rule). The session the environment points to (rule 'env', the firmest) → otherwise the session of the nearest valid Claude ancestor ('proc').
    Only sessions that have a record. (None, None) if none."""
    anc = nearest(pid, sess)
    for sid, rule in ((env_parent(pid, own, sess), 'env'), (anc and anc['sid'], 'proc')):
        if sid and sid != own and sid in sids:
            return sid, rule
    return None, None


def rollout_thread(path):
    """rollout path → thread id (the UUID at the end of the file name). None otherwise."""
    m = ROLLOUT_ID_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def _cache_trusted(path):
    """Whether the cache folder and file to read or write are mine (links are not picked up from another user's folder): the owner is me and no other user can write to it."""
    uid = os.geteuid() if hasattr(os, 'geteuid') else None
    for p in (os.path.dirname(path), path):
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        if uid is not None and (st.st_uid != uid or st.st_mode & 0o022):
            return False
    return True


def read_cache(path, now=None):
    """The entries of the cache file [{child, parent, kind, rule, seen, started}]. [] (quietly) if it is absent, broken or untrusted. The shape of every entry is checked:
    a UUID-shaped session id, kind (cli|codex), rule (CACHE_RULES), first-seen time within CACHE_DAYS, an optional sub-agent id, and the pair count up to CACHE_MAX."""
    now = time.time() if now is None else now
    try:
        if not _cache_trusted(path):
            return []
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > CACHE_FILE_MAX:
            return []
        with os.fdopen(fd, 'rb') as fh:
            fd = -1
            d = json.loads(fh.read(CACHE_FILE_MAX + 1))
        rows = d.get('links') if isinstance(d, dict) and d.get('version') in CACHE_READ_VERSIONS else None
    except (OSError, ValueError):
        return []
    finally:
        if fd >= 0:
            os.close(fd)
    out, seen_keys = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        c, p, kind, rule, seen, started = (r.get(k) for k in ('child', 'parent', 'kind', 'rule', 'seen', 'started'))
        num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and x == x
        if not (isinstance(c, str) and isinstance(p, str) and SID_RE.fullmatch(c) and SID_RE.fullmatch(p) and c != p):
            continue
        if kind not in ('cli', 'codex') or rule not in CACHE_RULES or not num(seen) or not (now - CACHE_DAYS * 86400 <= seen <= now + 86400):
            continue
        if (kind, c) in seen_keys:
            continue
        seen_keys.add((kind, c))
        node = r.get('node')
        row_ = {'child': c, 'parent': p, 'kind': kind, 'rule': rule, 'seen': float(seen), 'started': float(started) if num(started) else None}
        if isinstance(node, str) and NODE_ID_RE.fullmatch(node):
            row_['node'] = node                       # only a link whose node is a sub-agent carries it
        out.append(row_)
    out.sort(key=lambda r: -r['seen'])
    return out[:CACHE_MAX]


def write_cache(path, rows):
    """Writes rows by replacement (folder 0700, file 0600, written to a temp file and os.replace). The caller handles OSError."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if not _cache_trusted(path):
        raise PermissionError('cache folder is not ours')
    tmp = '%s.%d.tmp' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'version': CACHE_VERSION, 'links': rows}, fh, separators=(',', ':'))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Lineage:
    """Links decided by lineage and environment variables: cli {child Claude session id: {sid (parent), ts, cwd, rule, orig, seen}}, cx {Codex thread id: parent Claude session id} and cx_info {thread id: {rule, orig, seen}}.
    rule is how it was learned this time (proc|env, file if only read from the file), orig is the rule found first (what is kept in the file). Not erased while the server is up.
    If cache (a path) is given, every change is kept in the file and enable_cache reads it at start. Tests and the harness have cache None, so they do not touch files."""

    def __init__(self, cache=None):
        self.saved = {}                   # child session id -> {tree, node, orig, seen, ts}: links the affiliation judgment found firm (output file, long instruction); never evidence by itself
        self.cli = {}
        self.cx = {}
        self.cx_info = {}
        self.version = 0
        self.cache = None
        self.live_sids = set()            # session ids of the Claude processes that were alive in the last scan (for unlinked's `ended_before_seen` judgment)
        self.live_threads = set()         # thread ids of the rollouts that codex processes had open in the last scan
        self.write_error = None           # exception name of the last write failure (not its content)
        self._saved = None
        self._retry = 0.0
        self._loaded = False              # whether there are entries read from the file that must be matched against the records in the first scan
        if cache:
            self.enable_cache(cache)

    # ---- keeping in a file ----
    def enable_cache(self, path):
        """Keeps the links in path, and reads what is in the file now and uses it with the same rule (rule 'file', firm). Once, at start."""
        self.cache = path
        had = bool(self.cli or self.cx or self.saved)      # if something was already found before turning this on, it differs from the file, so it must be written once at first
        for r in read_cache(path):
            if r['kind'] == 'cli' and r['rule'] in SAVED_RULES:
                self.saved.setdefault(r['child'], {'tree': r['parent'], 'node': r.get('node'), 'orig': r['rule'], 'seen': r['seen'], 'ts': r['started']})
            elif r['kind'] == 'cli':
                self.cli.setdefault(r['child'], {'sid': r['parent'], 'ts': r['started'], 'cwd': None, 'rule': 'file', 'orig': r['rule'], 'seen': r['seen']})
            else:
                if r['child'] not in self.cx:
                    self.cx[r['child']] = r['parent']
                    self.cx_info[r['child']] = {'rule': 'file', 'orig': r['rule'], 'seen': r['seen'], 'ts': r['started']}
        self._loaded = bool(self.cli or self.cx or self.saved)
        self._saved = None if had else self._rows_text()

    def remember(self, child, tree, node, rule, started, now=None):
        """Keeps a link the affiliation judgment found firm: rule `out` (the launching call's output file holds the child's id) or `content` (a long instruction found in
        the launcher's text). Only ids, the rule and times are kept, never the instruction, a path or a fingerprint. A link seen before is not rewritten.
        It is not evidence in later judgments: it only fills in when the records and processes give none (rule `cache`)."""
        if rule not in SAVED_RULES or child == tree or not (SID_RE.fullmatch(child) and SID_RE.fullmatch(tree)):
            return False
        cur = self.saved.get(child)
        if cur and cur['tree'] == tree and cur['node'] == node:
            return False
        self.saved[child] = {'tree': tree, 'node': node, 'orig': rule, 'seen': (cur or {}).get('seen') or (time.time() if now is None else now),
                             'ts': started if isinstance(started, (int, float)) else None}
        self.version += 1
        self._save()
        return True

    def _rows(self, now=None):
        now = time.time() if now is None else now
        rows = [{'child': c, 'parent': v['sid'], 'kind': 'cli', 'rule': v['orig'], 'seen': v['seen'], 'started': v['ts']} for c, v in self.cli.items()]
        rows += [{'child': t, 'parent': p, 'kind': 'codex', 'rule': self.cx_info[t]['orig'], 'seen': self.cx_info[t]['seen'], 'started': self.cx_info[t]['ts']}
                 for t, p in self.cx.items()]
        rows += [{'child': c, 'parent': v['tree'], 'kind': 'cli', 'rule': v['orig'], 'seen': v['seen'], 'started': v['ts'], **({'node': v['node']} if v['node'] else {})}
                 for c, v in self.saved.items() if c not in self.cli]
        rows = [r for r in rows if r['seen'] >= now - CACHE_DAYS * 86400]
        rows.sort(key=lambda r: (-r['seen'], r['child']))
        return rows[:CACHE_MAX]

    def _rows_text(self):
        return json.dumps(self._rows(), sort_keys=True)

    def _prune(self):
        """Entries over the cap or age are dropped from memory too (the same set as the file)."""
        keep = {(r['kind'], r['child']) for r in self._rows()}
        for c in [c for c in self.cli if ('cli', c) not in keep]:
            del self.cli[c]
        for t in [t for t in self.cx if ('codex', t) not in keep]:
            del self.cx[t]
            self.cx_info.pop(t, None)
        for c in [c for c in self.saved if ('cli', c) not in keep]:
            del self.saved[c]

    def _save(self):
        if not self.cache:
            return
        self._prune()
        text = self._rows_text()
        if text == self._saved or time.time() < self._retry:
            return
        try:
            write_cache(self.cache, self._rows())
            self._saved, self.write_error = text, None
        except OSError as e:
            self.write_error = type(e).__name__
            self._retry = time.time() + CACHE_RETRY_SEC

    def _check_loaded(self, sids, codex_get):
        """Matches the entries read from the file against the current records, once at first: a session or thread whose parent or child record has disappeared is dropped."""
        if not self._loaded or not sids:
            return False                                   # if the records folder has not been seen yet (empty), nothing is erased
        self._loaded, dropped = False, False
        for c in [c for c, v in self.cli.items() if v['rule'] == 'file' and (c not in sids or v['sid'] not in sids)]:
            del self.cli[c]
            dropped = True
        for t in [t for t in self.cx if self.cx_info[t]['rule'] == 'file' and (self.cx[t] not in sids or not codex_get(t))]:
            del self.cx[t]
            del self.cx_info[t]
            dropped = True
        for c in [c for c, v in self.saved.items() if c not in sids or v['tree'] not in sids]:
            del self.saved[c]
            dropped = True
        return dropped

    def _cycle(self, child, parent):
        x = parent
        for _ in range(64):
            if x == child:
                return True
            x = (self.cli.get(x) or {}).get('sid')
            if not x:
                return False
        return True

    def scan(self, sids, codex_get):
        """Scans once. sids: set of Claude session ids that have a record, codex_get(thread id) → Codex list entry (None if none). True if something changed."""
        sess = read_sessions()
        self.live_sids = {s['sid'] for s in sess.values()}
        direct = {s['sid'] for s in sess.values() if not s['print']}      # sessions the user opened directly
        live = {}                                                         # child session id -> {parent session id: (that process, rule)}
        for pid, s in sess.items():
            if not s['print'] or s['sid'] in direct:
                continue
            psid, rule = parent_session(pid, s['sid'], sess, sids) if s['sid'] in sids else (None, None)
            if psid:
                live.setdefault(s['sid'], {})[psid] = (s, rule)
        changed = self._check_loaded(sids, codex_get)
        now = time.time()
        for csid in [c for c in self.cli if c in direct]:                 # now opened directly by the user: not a child agent
            del self.cli[csid]
            changed = True
        for csid, parents in live.items():
            cur = self.cli.get(csid)
            if cur and cur['sid'] in parents:
                s, rule = parents[cur['sid']]
                if cur['rule'] != rule or cur['orig'] != rule:             # something known only from the file was seen directly this time (or by a firmer rule)
                    cur['rule'], cur['orig'] = rule, rule
                    changed = True
                continue
            psid, (s, rule) = max(parents.items(), key=lambda kv: (kv[1][0]['ts'] or 0, kv[1][0]['pid']))
            if not self._cycle(csid, psid):
                self.cli[csid] = {'sid': psid, 'ts': s['ts'], 'cwd': s['cwd'], 'rule': rule, 'orig': rule, 'seen': now}
                changed = True
        livecx, self.live_threads = {}, set()
        for it in procs.codex_pids(CODEX_SESSIONS) or []:
            psid, rule = parent_session(it['pid'], None, sess, sids)
            for path in it['fds']:
                tid = rollout_thread(path)
                if tid:
                    self.live_threads.add(tid)
                e = codex_get(tid) if tid and psid else None
                # only exec threads started by Claude (the same targets as cx_link): not a TUI or desktop the user opened, nor a guardian child thread
                if e and e['origin'] == 'exec' and not e['guardian'] and os.path.normpath(e['path']) == os.path.normpath(path):
                    livecx.setdefault(tid, {})[psid] = rule
        for tid, parents in livecx.items():
            cur = self.cx.get(tid)
            if cur in parents:
                info = self.cx_info[tid]
                if info['rule'] != parents[cur] or info['orig'] != parents[cur]:
                    info['rule'], info['orig'] = parents[cur], parents[cur]
                    changed = True
                continue
            psid = sorted(parents)[0]
            self.cx[tid] = psid
            self.cx_info[tid] = {'rule': parents[psid], 'orig': parents[psid], 'seen': now, 'ts': None}
            changed = True
        if changed:
            self.version += 1
        self._save()
        return changed
