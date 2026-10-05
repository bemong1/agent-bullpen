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
from .facts import ANY_NODE_RE
from .util import CLAUDE_HOME, CODEX_SESSIONS, HOME, SID_RE

SESSION_FILE_MAX = 64 << 10                 # read cap for one session file
SESSION_FILE_RE = re.compile(r'(\d+)\.json')
ROLLOUT_ID_RE = re.compile(r'(%s)\.jsonl' % SID_RE.pattern)       # rollout-<time>-<thread id>.jsonl
ENV_NAMES = (b'CLAUDE_CODE_SESSION_ID', b'CLAUDE_PID', b'CODEX_THREAD_ID', b'CODEX_SESSION_ID')           # only these four are read from environ

# record of the links found by firm rules (so they are not forgotten across a restart). It holds only session ids, rule and time (no path, instruction, fingerprint or environment
# variable value). Version 2 adds the links that rest on an output file (`out`) or a long instruction (`content`), with the sub-agent id when the node is one; version 1 files are still read.
# Version 3 is the first whose `content` links come from a reader that does not take words that only travel as text (`tmux send-keys …`, `echo`) for a launch; version 4 the first whose
# `content` links also leave out the plain arguments of an ordinary program (`curl --data claude -p …`) and the words a script file only prints or types; version 6 the first whose
# `content` links also leave out a python file that only mentions the tool (a relay that types it into a terminal): the `content` links of an older file are not read (the records give
# them again, rightly), the other rules of an older file are kept.
LINK_CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache'), 'agent-bullpen', 'links.json')
# Version 5 adds `parent_kind` (`claude` or `codex`: whose id the parent is; a row without it, of an older file, is a Claude parent) and the node of an environment link.
CACHE_VERSION = 6
CACHE_READ_VERSIONS = (1, 2, 3, 4, 5, 6)
CONTENT_FROM = 6                  # the first file version whose `content` links are read
PARENT_KINDS = ('claude', 'codex')
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


def env_parent(pid, own, sess, env=None):
    """The parent Claude session id that the environment of process pid points to, else None (environment unreadable or absent, its own session, or inconsistent with `CLAUDE_PID`).
    own: that process's own session id (None if none). env: the values already read (procs.env_values)."""
    env = procs.env_values(pid, ENV_NAMES) if env is None else env
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


def _kind_of(e):
    return e.get('kind', 'guardian' if e.get('guardian') else 'root') if e else None


def codex_env(env, own, codex_get, codex_root):
    """What the environment of a process says about the Codex thread whose shell started it: None (nothing: no `CODEX_THREAD_ID`, or the process's own thread), {'bad': True} (something that
    cannot be trusted: a name that is no thread id, a review thread, the child's own tree), {'unknown': thread id} (a thread id nothing is known of: the index does not have it yet, or its
    chain does not end in a root yet: the process was started by a shell whose commands nobody can read, which is not the same as nothing having started it), {'conflict': [(root, node),
    (session, None)]} (`CODEX_SESSION_ID` is not the root of `CODEX_THREAD_ID`: the two names point to different trees, which is a conflict, not an absence), or {'root', 'node'}:
    `CODEX_THREAD_ID` is the thread that ran the command, so the node is that thread when it is a sub-agent, None when it is the root."""
    t = env.get('CODEX_THREAD_ID') if env else None
    if not t:
        return None
    if t == own:
        return None
    if not SID_RE.fullmatch(t):
        return {'bad': True}
    e = codex_get(t)
    kind = _kind_of(e)
    if kind == 'guardian':
        return {'bad': True}
    root = codex_root(t) if kind in ('root', 'sub') else None
    if root is None:
        return {'unknown': t}
    if root == own:
        return {'bad': True}
    node = t if _kind_of(e) == 'sub' else None
    s = env.get('CODEX_SESSION_ID')
    if s and s != root:
        return {'conflict': [(root, node), (s, None)]}
    return {'root': root, 'node': node}


def nearest_agent(pid, sess, cxp):
    """The nearest of pid's ancestors that is an agent: a Claude with a valid session file ('claude', its record) or a codex ('codex', the one root its open rollouts belong to, None when
    they belong to several roots, one of them is not known, or its open files cannot be seen). None when there is none (the process was started detached: its parent is init)."""
    for a in procs.ancestors(pid):
        if a in sess:
            return 'claude', sess[a]
        if a in cxp:
            return 'codex', cxp[a]
        if procs.is_codex_argv(procs.argv(a)):
            return 'codex', None                                         # a codex whose open files cannot be seen (no /proc: macOS) or that has no rollout open: an agent whose root is not known
    return None


def codex_agents(sessions, codex_root):
    """{pid: root} of the live codex processes that have a rollout open: the root thread all their open rollouts belong to, None when they belong to several (an app-server: it
    holds the threads of many sessions), when the root of any one of them cannot be told (it is not known yet: nothing says it is not another tree), or when the process is in
    another pid namespace (its pids mean nothing here)."""
    out, ns = {}, procs.pid_ns()
    for it in procs.codex_pids(sessions) or []:
        tids = [t for t in (rollout_thread(p) for p in it['fds']) if t]
        if not tids:
            continue
        other = procs.pid_ns(it['pid'])
        roots = {codex_root(t) for t in tids}
        out[it['pid']] = next(iter(roots)) if len(roots) == 1 and None not in roots and not (ns and other and other != ns) else None      # a rollout whose root is not known holds the tree
    return out


_UNREAD = object()


def _dedupe(pairs):
    """The (tree, node) pairs without a tree twice (the first one stays)."""
    out, seen = [], set()
    for t, n in pairs:
        if t not in seen:
            seen.add(t)
            out.append((t, n))
    return out


def parent_claim(pid, own, sess, sids, cxp, codex_get, codex_root, env=_UNREAD):
    """The parent the live process `pid` points to: {tree, node, rule, exact, pk} (`pk`: whose tree it is, `claude` or `codex`; `exact`: the claim names the node too: a Codex
    environment), {'pending': {claude, codex}} when the two providers' environments name different parents and nothing decides between them yet, {'conflict': [(tree, node)]}
    when the evidence of one provider contradicts itself (`CODEX_SESSION_ID` is not the root of `CODEX_THREAD_ID`, or the environment and the codex process above name different
    roots): the trees are a tie, which holds the child; {'unknown': (thread id,)} when the environment names a Codex thread nothing is known of (the child is looked at again at every
    scan, and no content match makes it certain meanwhile); or None.
    The nearest agent among the ancestors decides whose environment counts: the other provider's variables are left over from an agent further up. When the process was
    started detached (no agent among its ancestors) both environments are read and, when both are there, the ownership chain decides (Lineage.resolve)."""
    env = procs.env_values(pid, ENV_NAMES) if env is _UNREAD else env
    cl = env_parent(pid, own, sess, env)
    cx = codex_env(env, own, codex_get, codex_root)
    agent = nearest_agent(pid, sess, cxp)
    if (agent and agent[0] == 'claude') or (agent is None and cx is None):
        anc = agent[1]['sid'] if agent else None
        for sid, rule in ((cl, 'env'), (anc, 'proc')):
            if sid and sid != own and sid in sids:
                return {'tree': sid, 'node': None, 'rule': rule, 'exact': False, 'pk': 'claude'}
        return None
    claim = {'tree': cx['root'], 'node': cx['node'], 'rule': 'env', 'exact': True, 'pk': 'codex'} if cx and 'root' in cx else None
    if agent:                                                       # a codex is the nearest agent
        root = agent[1]
        if cx and 'unknown' in cx:
            return {'unknown': (cx['unknown'],)}
        if cx and 'conflict' in cx:
            return {'conflict': _dedupe(cx['conflict'] + ([(root, None)] if root else []))}
        if claim:
            return claim if root in (None, claim['tree']) else {'conflict': [(root, None), (claim['tree'], claim['node'])]}
        if cx is None and root and codex_get(root):
            return {'tree': root, 'node': None, 'rule': 'proc', 'exact': False, 'pk': 'codex'}
        return None
    if cx.get('bad'):
        return None                                                 # an environment that cannot be trusted next to a Claude one: nothing is claimed
    if 'unknown' in cx:
        return {'unknown': (cx['unknown'],)}
    if 'conflict' in cx:
        return {'conflict': _dedupe(cx['conflict'] + ([(cl, None)] if (cl and cl != own and cl in sids) else []))}
    if not (cl and cl != own and cl in sids):
        return claim
    return {'pending': {'claude': cl, 'codex': (cx['root'], cx['node'])}}


def _held(got):
    """What a child that is not placed yet keeps (Lineage.pending) of a parent_claim that holds it: the two providers' parents, the trees that tie, or the threads nothing is known of."""
    return got.get('pending') or ({'conflict': got['conflict']} if 'conflict' in got else {'unknown': got['unknown']})


def codex_free(pid, env, sess, cxp):
    """Whether the environment of process `pid` (read: `env`) holds no name of Codex AND says why no Codex shell can be behind it: the environment names a live Claude session by both
    `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID` (a shell that threw the names of Codex away, `env -i`, `sudo`, `ssh localhost`, `tmux new-window`, still carries Claude's), or the
    ancestors can be followed to the end, a live Claude is among them and no codex is. None when the environment was not read."""
    if env is None:
        return None
    if 'CODEX_THREAD_ID' in env or 'CODEX_SESSION_ID' in env:
        return False
    sid, cpid = env.get('CLAUDE_CODE_SESSION_ID'), env.get('CLAUDE_PID')
    if sid and SID_RE.fullmatch(sid) and cpid and cpid.isdigit():
        rec = sess.get(int(cpid))
        if rec and rec['sid'] == sid:
            return True
    ancestors, complete = procs.chain(pid)
    # the ancestors reach a live Claude and no codex is among them (a process started detached has init for its parent: that says nothing about what ran the shell before it)
    return complete and any(a in sess for a in ancestors) and not any(a in cxp or procs.is_codex_argv(procs.argv(a)) for a in ancestors)


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


def read_cache(path, now=None, info=None):
    """The entries of the cache file [{child, parent, kind, rule, seen, started}]. [] (quietly) if it is absent, broken or untrusted. The shape of every entry is checked:
    a UUID-shaped session id, kind (cli|codex), rule (CACHE_RULES), first-seen time within CACHE_DAYS, an optional sub-agent id, and the pair count up to CACHE_MAX.
    A `content` entry of a file older than CONTENT_FROM is left out. `info` (a dict) is given the file's `version` when the file could be read."""
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
        version = d.get('version') if isinstance(d, dict) else None
        rows = d.get('links') if version in CACHE_READ_VERSIONS else None
        if info is not None and rows is not None:
            info['version'] = version
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
        if rule == 'content' and version < CONTENT_FROM:
            continue
        pk = r.get('parent_kind', 'claude')
        if pk not in PARENT_KINDS:
            continue
        if (kind, c) in seen_keys:
            continue
        seen_keys.add((kind, c))
        node = r.get('node')
        row_ = {'child': c, 'parent': p, 'kind': kind, 'rule': rule, 'seen': float(seen), 'started': float(started) if num(started) else None, 'parent_kind': pk}
        if isinstance(node, str) and ANY_NODE_RE.fullmatch(node):
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
    """Links decided by lineage and environment variables: cli {child Claude session id: {sid (parent), ts, cwd, rule, orig, seen, pk, node, exact}}, cx {Codex thread id: parent id} and
    cx_info {thread id: {rule, orig, seen, ts, pk, node, exact}}. The parent is a Claude session id or a Codex root thread id (`pk`: `claude` or `codex`); `node` and `exact` come only from
    the environment of a Codex shell (the thread that ran the command). rule is how it was learned this time (proc|env, file if only read from the file), orig is the rule found first
    (what is kept in the file). Not erased while the server is up.
    If cache (a path) is given, every change is kept in the file and enable_cache reads it at start. Tests and the harness have cache None, so they do not touch files."""

    def __init__(self, cache=None):
        self.saved = {}                   # child session id -> {tree, node, orig, seen, ts, pk}: links the affiliation judgment found firm (output file, long instruction); never evidence by itself
        self.cli = {}
        self.cx = {}
        self.cx_info = {}
        self.codex_free = {}              # child id -> True when its process's environment was read and nothing can have come from a Codex shell (see codex_free()), False when it was read and might; not kept when the process was not read
        self.pending = {}                 # child id -> {kind (cli|cx), claude, codex: (root, node), ts, cwd}: both providers' environments name a parent and the chain does not say yet which is the direct one (or `conflict`: [(tree, node)], or `unknown`: (thread id,) in place of both)
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
        info = {}
        for r in read_cache(path, info=info):
            pk, node = r['parent_kind'], r.get('node')
            if r['kind'] == 'cli' and r['rule'] in SAVED_RULES:
                self.saved.setdefault(r['child'], {'tree': r['parent'], 'node': node, 'orig': r['rule'], 'seen': r['seen'], 'ts': r['started'], 'pk': pk})
            elif r['kind'] == 'cli':
                self.cli.setdefault(r['child'], {'sid': r['parent'], 'ts': r['started'], 'cwd': None, 'rule': 'file', 'orig': r['rule'], 'seen': r['seen'], 'pk': pk, 'node': node,
                                                 'exact': pk == 'codex' and r['rule'] == 'env'})
            else:
                if r['child'] not in self.cx:
                    self.cx[r['child']] = r['parent']
                    self.cx_info[r['child']] = {'rule': 'file', 'orig': r['rule'], 'seen': r['seen'], 'ts': r['started'], 'pk': pk, 'node': node, 'exact': pk == 'codex' and r['rule'] == 'env'}
        self._loaded = bool(self.cli or self.cx or self.saved)
        stale = 'version' in info and info['version'] != CACHE_VERSION          # a file of an older version is written again, once, as the current one
        self._saved = None if had or stale else self._rows_text()

    def remember(self, child, tree, node, rule, started, now=None, pk='claude'):
        """Keeps a link the affiliation judgment found firm: rule `out` (the launching call's output file holds the child's id) or `content` (a long instruction found in
        the launcher's text). Only ids, the rule and times are kept, never the instruction, a path or a fingerprint. A link seen before is not rewritten.
        It is not evidence in later judgments: it only fills in when the records and processes give none (rule `cache`)."""
        if rule not in SAVED_RULES or child == tree or not (SID_RE.fullmatch(child) and SID_RE.fullmatch(tree)):
            return False
        cur = self.saved.get(child)
        if cur and cur['tree'] == tree and cur['node'] == node:
            return False
        self.saved[child] = {'tree': tree, 'node': node, 'orig': rule, 'seen': (cur or {}).get('seen') or (time.time() if now is None else now),
                             'ts': started if isinstance(started, (int, float)) else None, 'pk': pk}
        self.version += 1
        self._save()
        return True

    def _rows(self, now=None):
        now = time.time() if now is None else now
        rows = [dict({'child': c, 'parent': v['sid'], 'kind': 'cli', 'rule': v['orig'], 'seen': v['seen'], 'started': v['ts'], 'parent_kind': v.get('pk', 'claude')},
                     **({'node': v['node']} if v.get('node') else {})) for c, v in self.cli.items()]
        rows += [dict({'child': t, 'parent': p, 'kind': 'codex', 'rule': self.cx_info[t]['orig'], 'seen': self.cx_info[t]['seen'], 'started': self.cx_info[t]['ts'],
                       'parent_kind': self.cx_info[t].get('pk', 'claude')}, **({'node': self.cx_info[t]['node']} if self.cx_info[t].get('node') else {}))
                 for t, p in self.cx.items()]
        rows += [dict({'child': c, 'parent': v['tree'], 'kind': 'cli', 'rule': v['orig'], 'seen': v['seen'], 'started': v['ts'], 'parent_kind': v.get('pk', 'claude')},
                      **({'node': v['node']} if v['node'] else {})) for c, v in self.saved.items() if c not in self.cli]
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

    @staticmethod
    def _parent_has_record(parent, pk, sids, codex_get):
        """Whether the parent of a remembered link still has a record: a Claude session, or a Codex root thread."""
        if pk == 'codex':
            e = codex_get(parent)
            return bool(e) and _kind_of(e) == 'root'
        return parent in sids

    def _check_loaded(self, sids, codex_get):
        """Matches the entries read from the file against the current records, once at first: a session or thread whose parent or child record has disappeared is dropped."""
        if not self._loaded or not sids:
            return False                                   # if the records folder has not been seen yet (empty), nothing is erased
        self._loaded, dropped = False, False
        ok = lambda parent, pk: self._parent_has_record(parent, pk, sids, codex_get)
        for c in [c for c, v in self.cli.items() if v['rule'] == 'file' and (c not in sids or not ok(v['sid'], v.get('pk', 'claude')))]:
            del self.cli[c]
            dropped = True
        for t in [t for t in self.cx if self.cx_info[t]['rule'] == 'file' and (not ok(self.cx[t], self.cx_info[t].get('pk', 'claude')) or not codex_get(t))]:
            del self.cx[t]
            del self.cx_info[t]
            dropped = True
        for c in [c for c, v in self.saved.items() if c not in sids or not ok(v['tree'], v.get('pk', 'claude'))]:
            del self.saved[c]
            dropped = True
        return dropped

    def _cycle(self, child, parent):
        x = parent
        for _ in range(64):
            if x == child:
                return True
            x = (self.cli.get(x) or {}).get('sid') or self.cx.get(x)
            if not x:
                return False
        return True

    def scan(self, sids, codex_get, codex_root=None):
        """Scans once. sids: set of Claude session ids that have a record, codex_get(thread id) → Codex list entry (None if none), codex_root(thread id) → the root thread above it
        (None if unknown; by default it follows the entries' `parent`). True if something changed."""
        if codex_root is None:
            def codex_root(tid):
                cur = tid
                for _ in range(9):
                    e = codex_get(cur)
                    if not e or _kind_of(e) == 'internal':
                        return None
                    if _kind_of(e) == 'root':
                        return cur
                    cur = e.get('parent')
                return None
        sess = read_sessions()
        self.live_sids = {s['sid'] for s in sess.values()}
        direct = {s['sid'] for s in sess.values() if not s['print']}      # sessions the user opened directly
        cxp = codex_agents(CODEX_SESSIONS, codex_root)                    # the live codex processes that have a rollout open, and the root each belongs to
        live = {}                                                         # child session id -> {parent id: (that process, claim)}
        pending = {}
        for pid, s in sess.items():
            if not s['print'] or s['sid'] in direct:
                continue
            env = procs.env_values(pid, ENV_NAMES) if s['sid'] in sids else None
            self._note_env(s['sid'], codex_free(pid, env, sess, cxp))
            got = parent_claim(pid, s['sid'], sess, sids, cxp, codex_get, codex_root, env) if s['sid'] in sids else None
            if got and ('pending' in got or 'conflict' in got or 'unknown' in got):
                pending[s['sid']] = dict(_held(got), kind='cli', ts=s['ts'], cwd=s['cwd'])
            elif got:
                live.setdefault(s['sid'], {})[got['tree']] = (s, got)
        changed = self._check_loaded(sids, codex_get)
        now = time.time()
        for csid in [c for c in self.cli if c in direct]:                 # now opened directly by the user: not a child agent
            del self.cli[csid]
            changed = True
        for csid, parents in live.items():
            cur = self.cli.get(csid)
            if cur and cur['sid'] in parents:
                s, got = parents[cur['sid']]
                same = (cur['rule'], cur['orig'], cur.get('node'), cur.get('exact')) == (got['rule'], got['rule'], got['node'], got['exact'])
                if not same:                                               # something known only from the file was seen directly this time (or by a firmer rule)
                    cur.update(rule=got['rule'], orig=got['rule'], node=got['node'], exact=got['exact'], pk=got['pk'])
                    changed = True
                continue
            psid, (s, got) = max(parents.items(), key=lambda kv: (kv[1][0]['ts'] or 0, kv[1][0]['pid']))
            if not self._cycle(csid, psid):
                self.cli[csid] = {'sid': psid, 'ts': s['ts'], 'cwd': s['cwd'], 'rule': got['rule'], 'orig': got['rule'], 'seen': now, 'pk': got['pk'], 'node': got['node'], 'exact': got['exact']}
                changed = True
        livecx, self.live_threads = {}, set()
        for it in procs.codex_pids(CODEX_SESSIONS) or []:
            got, tried = None, False
            for path in it['fds']:
                tid = rollout_thread(path)
                if tid:
                    self.live_threads.add(tid)
                e = codex_get(tid) if tid else None
                # only exec threads started by an agent (the same targets as cx_link): not a TUI or desktop the user opened, nor a sub-agent or review thread
                if e and e['origin'] == 'exec' and not e['guardian'] and os.path.normpath(e['path']) == os.path.normpath(path):
                    if not tried:
                        env = procs.env_values(it['pid'], ENV_NAMES)
                        tried, got = True, parent_claim(it['pid'], None, sess, sids, cxp, codex_get, codex_root, env)
                    self._note_env(tid, codex_free(it['pid'], env, sess, cxp))
                    if got and ('pending' in got or 'conflict' in got or 'unknown' in got):
                        pending[tid] = dict(_held(got), kind='cx', ts=None, cwd=None)
                    elif got and got['tree'] != tid and (got['pk'] == 'claude' or codex_get(got['tree'])):
                        livecx.setdefault(tid, {})[got['tree']] = got
        for tid, parents in livecx.items():
            cur = self.cx.get(tid)
            if cur in parents:
                info, got = self.cx_info[tid], parents[cur]
                if (info['rule'], info['orig'], info.get('node'), info.get('exact')) != (got['rule'], got['rule'], got['node'], got['exact']):
                    info.update(rule=got['rule'], orig=got['rule'], node=got['node'], exact=got['exact'], pk=got['pk'])
                    changed = True
                continue
            psid = sorted(parents)[0]
            got = parents[psid]
            if not self._cycle(tid, psid):
                self.cx[tid] = psid
                self.cx_info[tid] = {'rule': got['rule'], 'orig': got['rule'], 'seen': now, 'ts': None, 'pk': got['pk'], 'node': got['node'], 'exact': got['exact']}
                changed = True
        # a child that a live process or a decision already placed is not pending; a placement that is only a remembered line (rule `file`) does not count against what the process says now
        self.pending = {c: p for c, p in pending.items() if not ((c in self.cli and self.cli[c]['rule'] != 'file') or (c in self.cx and self.cx_info[c]['rule'] != 'file'))}
        if changed:
            self.version += 1
        self._save()
        return changed

    def _note_env(self, child, free):
        """Remembers, for a child whose process was read, whether nothing can have come from a Codex shell (codex_free). A process that could not be read leaves what is known."""
        if free is not None:
            self.codex_free[child] = free

    def resolve(self, up):
        """Decides the children whose two providers' environments name different parents, with the ownership chain: `up(id)` is the ids above one (through certain links only: the
        links of the children being decided here are not among them). The Codex thread is the direct parent when the Claude session is above its root; the Claude session when
        that root is above it; neither, or both (a cycle): the child is held, no link. True when a child got its parent."""
        changed, now = False, time.time()
        for child, p in list(self.pending.items()):
            if 'claude' not in p:
                continue                                              # a conflict inside one provider: nothing to tell apart with a chain
            x, (root, node) = p['claude'], p['codex']
            below_x, below_t = x in up(root), root in up(x)             # the Codex thread hangs below the session / the session hangs below the Codex root
            if below_x == below_t:
                continue
            tree, node, pk, exact = (root, node, 'codex', True) if below_x else (x, None, 'claude', False)
            if self._cycle(child, tree):
                continue
            if p['kind'] == 'cli':
                self.cli[child] = {'sid': tree, 'ts': p['ts'], 'cwd': p['cwd'], 'rule': 'env', 'orig': 'env', 'seen': now, 'pk': pk, 'node': node, 'exact': exact}
            else:
                self.cx[child] = tree
                self.cx_info[child] = {'rule': 'env', 'orig': 'env', 'seen': now, 'ts': None, 'pk': pk, 'node': node, 'exact': exact}
            del self.pending[child]
            changed = True
        if changed:
            self.version += 1
            self._save()
        return changed
