"""Session list and opening: the list of Claude sessions and Codex conversations (list_sessions), a summary of the folders read (sources), picking each project's representative, and the Registry that opens a session just once (singleton REG)."""

import collections
import glob
import json
import os
import re
import stat
import threading
import time

from .lineage import claude_alive_ids
from .util import CLAUDE_HOME, CODEX_HOME, CODEX_SESSIONS, PATH_FROM, PROJECTS, SID_RE, parse_records, parse_ts, short_path
from .codex_index import CODEX
from .link import LINKS
from .sessions import LATER, CodexSession, Session
from . import views


CX_LIST_DAYS = 7
CL_LIST_MAX = 30           # Claude sessions that have agents: the most recent 30
SOLO_LIST_MAX = 30         # Claude sessions without agents (solo) get their own most recent 30: however many there are, they do not push out the agent-session list


_CL_META = {}      # Claude session record path -> (size, {cwd, title})
_CL_LAST = {}      # Claude record path -> (size, time of its last user or assistant line, or None)
TALK_TAIL = (512 << 10, 4 << 20)       # how much of the end of a record is looked through for the last talk: the small window first, the large one only if it found none
_TALK_RE = re.compile(rb'"type"\s*:\s*"(?:user|assistant)"')


def claude_meta(path):
    """A Claude session's working folder (the first cwd in the head) and title (the last ai-title in the last 512 KB). Re-read only when the size changes."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {'cwd': '', 'title': ''}
    c = _CL_META.get(path)
    if c and c[0] == size:
        return c[1]
    m = dict(c[1]) if c else {'cwd': '', 'title': ''}
    try:
        with open(path, 'rb') as f:
            if not m['cwd']:
                k = re.search(rb'"cwd":"([^"]+)"', f.read(1 << 20))
                m['cwd'] = k.group(1).decode('utf-8', 'replace') if k else ''
            f.seek(max(0, size - (512 << 10)))
            ts = re.findall(rb'"type":"ai-title","aiTitle":"((?:[^"\\]|\\.)*)"', f.read())
            if ts:
                m['title'] = json.loads(b'"' + ts[-1] + b'"')
    except (OSError, ValueError):
        pass
    _CL_META[path] = (size, m)
    return m


def _talk_time(lines):
    """The time of the last user or assistant record among the lines of a record (read from the end), or None."""
    for raw in reversed(lines):
        if not _TALK_RE.search(raw):
            continue
        for rec in reversed(parse_records(raw)[0]):
            stamp = rec.get('timestamp') if isinstance(rec, dict) and rec.get('type') in ('user', 'assistant') else None
            if isinstance(stamp, str) and parse_ts(stamp):
                return parse_ts(stamp)
    return None


def last_talk(path):
    """The time of the last user or assistant line of a record (what the conversation did last), or None when the end of the file has none. A live Claude Code also appends
    lines with no talk in them (bookkeeping, a ledger), which move the file time but are not activity of the session. Re-read only when the size changes."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    c = _CL_LAST.get(path)
    if c and c[0] == size:
        return c[1]
    found = None
    try:
        with open(path, 'rb') as f:
            for span in TALK_TAIL:
                f.seek(max(0, size - span))
                lines = f.read().split(b'\n')
                found = _talk_time(lines[1:] if size > span else lines)       # the first line of a window that starts inside the file is cut
                if found or size <= span:
                    break
    except (OSError, ValueError):
        return None
    _CL_LAST[path] = (size, found)
    return found


def team_totals():
    """{session or thread id: how many agents its page shows below its orchestrator}: everything started from it, directly or through others (the descendants of the one graph of who
    started whom: `claude -p` children, native sub-agent threads and `codex exec` threads of Codex, down as many levels as the graph follows), and the sub-agents (Agent tool) that
    the `claude -p` children among them started (the page of the top orchestrator shows them under their child, and counts them). Only an id that started something has an entry."""
    graph = LINKS.edges()[1]
    folders = {os.path.basename(p)[:-6]: p[:-6] for p in list(LINKS.files)}          # the folder of each session the link index has read: no search through the projects
    hosted = {}
    for tid, o in graph.items():
        if o['kind'] == 'cli':
            n = len(glob.glob(os.path.join(folders[tid] if tid in folders else os.path.join(PROJECTS, '*', tid), 'subagents', 'agent-*.meta.json')))
            if n:
                hosted[tid] = n
    out = {}
    for page in {o['parent'] for o in graph.values()}:
        team = LINKS.descendants(page)
        out[page] = len(team) + sum(hosted.get(t, 0) for t in team)
    return out


def _regular(path):
    """os.stat of a regular file that can be read, else None (a link that leads nowhere or loops, a folder, a file that went away or cannot be read)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) and os.access(path, os.R_OK) else None


def _is_file(path):
    """A regular file (a link that leads to one counts); a folder with a record's name, a FIFO or a link that leads nowhere does not."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _session_item(p, sid, team, now):
    """The list item of one Claude record, or None when the record is not a session to list. `mtime` and `agents_mtime` are the file times here (the upper bound of the
    time of the last talk); refine() puts the time of the talk in their place."""
    st = _regular(p)
    if st is None:
        return None
    sub = os.path.join(p[:-6], 'subagents')
    n = len(glob.glob(os.path.join(sub, 'agent-*.meta.json')))
    subs = [(f, t.st_mtime) for f in glob.glob(os.path.join(sub, 'agent-*.jsonl')) for t in [_regular(f)] if t]
    agents = n + team.get(sid, 0)
    if not agents and not st.st_size:
        return None                  # an empty record file (a session with no conversation) is not counted as solo
    newest = max(subs, key=lambda x: x[1], default=None)
    item = {'id': sid, 'project': os.path.basename(os.path.dirname(p)), 'mtime': st.st_mtime,
            'agents': agents, 'provider': 'claude', 'agents_mtime': newest[1] if newest else 0,
            'active': sum(1 for _, t in subs if now - t < 120), '_path': p, '_newest': newest[0] if newest else None}
    if not agents:
        item['solo'] = True
    return item


def _refine(item, now):
    """Replaces the file times of an item by the time of the last talk: the main record's, and the newest subagent record's (a time never later than its file time).
    A record whose end has no talk keeps its file time. The activity of a solo session is that time too; a session with agents counts the subagent records that grew."""
    talk = last_talk(item['_path'])
    if talk:
        item['mtime'] = min(item['mtime'], talk)
    if item['_newest']:
        sub = last_talk(item['_newest'])
        if sub:
            item['agents_mtime'] = min(item['agents_mtime'], sub)
    if item.get('solo'):
        item['active'] = 1 if now - item['mtime'] < 120 else 0
    return item


def _newest(items, n, now):
    """The n items whose last talk is the latest, most recent first. The file time is never earlier than the talk, so the items are taken in the order of their file times
    and the rest of the list is not looked at once the next file time cannot beat the n-th talk found."""
    key = lambda s: max(s['mtime'], s['agents_mtime'])
    items.sort(key=lambda s: -key(s))
    best = []
    for s in items:
        if len(best) >= n and key(s) <= key(best[n - 1]):
            break
        best.append(_refine(s, now))
        best.sort(key=lambda x: -key(x))
    return best[:n]


def scan_sessions():
    """(session list, counts). 30 Claude sessions that have agents + Codex of the last 7 days (counted apart) + the most recent 30 Claude sessions without agents (solo).
    Linked execs and guardians are not in the list. Each item carries proj (working folder name), title, active (number of agents whose record grew within the last 2 minutes), and last.
    A solo item carries solo=True and comes at the very end of the list (the order before it is the same as when there was no solo).
    What is recent is decided by the last talk in the record (the last user or assistant line; for agents, the newest subagent record), not by the file time. A record that cannot be
    looked at (it went away, cannot be read, is a link that leads nowhere or a folder) is left out; it does not stop the list.
    counts: claude (all sessions), agent_sessions (those with agents), codex (those of the last 7 days that made the list)."""
    out, solo, now = [], [], time.time()
    team = team_totals()
    for p in glob.glob(os.path.join(PROJECTS, '*', '*.jsonl')):
        sid = os.path.basename(p)[:-6]
        if LINKS.cli_owner(sid):
            continue                 # a child session started with claude -p is seen only as an agent of its parent session
        try:
            item = _session_item(p, sid, team, now)
        except OSError:
            continue
        if item:
            (solo if item.get('solo') else out).append(item)
    counts = {'claude': len(out) + len(solo), 'agent_sessions': len(out)}
    out, solo = _newest(out, CL_LIST_MAX, now), _newest(solo, SOLO_LIST_MAX, now)       # cut before reading the meta (claude_meta reads the first 1 MB and the last 512 KB of each file)
    for s in out + solo:
        m = claude_meta(s.pop('_path'))
        s.pop('_newest')
        s['proj'] = os.path.basename(m['cwd'].rstrip('/')) or s['project']
        s['title'] = m['title']
    CODEX.refresh()
    cx = []
    for e in CODEX.entries():
        if e['guardian'] or LINKS.owner_of(e['id']) or now - (e['mtime'] or 0) > CX_LIST_DAYS * 86400:
            continue
        proj = os.path.basename(e['cwd'].rstrip('/')) or e['cwd']
        cx.append({'id': e['id'], 'project': proj, 'proj': proj, 'mtime': e['mtime'], 'agents': team.get(e['id'], 0), 'agents_mtime': 0,
                   'provider': 'codex', 'origin': e['origin'], 'title': CODEX.title(e),
                   'active': 1 if now - (e['mtime'] or 0) < 120 else 0})
    cx.sort(key=lambda s: -s['mtime'])
    counts['codex'] = len(cx)
    return out + cx + solo, counts


def list_sessions():
    return scan_sessions()[0]


def sources(counts):
    """The sources of /api/sessions and of the start output: for each of Claude and Codex, the config folder (dir), the origin (from), whether the record folder exists (exists), and the session count.
    exists says whether the record folder under the config folder (projects for Claude, sessions for Codex) exists. Codex's agent_sessions is null."""
    return [{'provider': 'claude', 'dir': short_path(CLAUDE_HOME), 'from': PATH_FROM['claude'], 'exists': os.path.isdir(PROJECTS),
             'sessions': counts['claude'], 'agent_sessions': counts['agent_sessions']},
            {'provider': 'codex', 'dir': short_path(CODEX_HOME), 'from': PATH_FROM['codex'], 'exists': os.path.isdir(CODEX_SESSIONS),
             'sessions': counts['codex'], 'agent_sessions': None}]


_REPS = {}         # project -> representative session id (kept until it ends)
REP_IDLE_SEC = 1800


def session_projects(sessions):
    """One representative per project. The representative prefers a Claude session with agents or a standalone Codex conversation (orchestration),
    and for a project with no such session, the most recent solo session (a Claude session without agents) is the representative.
    The representative is the session with the most agents moving right now (the most recent if tied). Once chosen, it is not changed until that session ends or
    30 minutes pass with no movement (so the screen does not jump from the representative changing every few seconds).
    A project's n, last and order are counted from the candidates for the representative (only the orchestration ones if there are any): even if solo is added, the values of an orchestration project stay the same."""
    now, alive = time.time(), claude_alive_ids()
    last = lambda s: max(s['mtime'] or 0, s['agents_mtime'] or 0)
    groups = collections.OrderedDict()
    for s in sorted(sessions, key=lambda x: -last(x)):
        s['last'] = last(s)
        groups.setdefault(s['proj'], []).append(s)
    out = []
    for proj, lst in groups.items():
        pool = [s for s in lst if not s.get('solo')] or lst
        cur = next((s for s in lst if s['id'] == _REPS.get(proj)), None)
        ended = cur is None or now - cur['last'] > REP_IDLE_SEC or (cur['provider'] == 'claude' and cur['id'] not in alive) \
            or (cur.get('solo') and pool is not lst)          # solo is the representative but an orchestration has appeared
        if ended:
            cur = max(pool, key=lambda s: (s['active'], s['last']))
            _REPS[proj] = cur['id']
        for s in lst:
            s['rep'] = s is cur
        out.append({'name': proj, 'rep': cur['id'], 'n': len(pool), 'last': pool[0]['last']})
    out.sort(key=lambda p: -p['last'])                        # stable sort: if equal, the order of first appearance
    return out


def default_session(projs, sessions):
    """Where to open when the address has no session: the representative of the most recent project among those whose representative is an orchestration. If none, the representative (solo) of the most recent project."""
    solo = {s['id'] for s in sessions if s.get('solo')}
    return next((p['rep'] for p in projs if p['rep'] not in solo), projs[0]['rep'] if projs else None)


class Registry:
    def __init__(self):
        self.sessions = {}
        self.loading = {}          # sid -> Event: a session being read for the first time. If the same id is requested at the same time, wait until it ends
        self.lock = threading.Lock()

    def get(self, sid):
        """A Claude session first, else a Codex thread. If it is an exec thread that Claude started (linked), opens that Claude session.
        A session is registered only after its first read (including the catch-up) **succeeded** (on failure it is not registered). A request for the same id that comes in during the read waits and then receives the same session."""
        if not sid or not SID_RE.fullmatch(sid):
            return None                       # the value becomes a glob path as it is, so only a UUID shape passes
        with self.lock:
            s = self.sessions.get(sid)
            if not s:
                ev = self.loading.get(sid)
                mine = ev is None
                if mine:
                    ev = self.loading[sid] = threading.Event()
        if s:
            top = LINKS.page_of(sid)
            return self.get(top) if top != sid else s      # what was opened on its own and then linked to a session or thread is a card of that one's page
        if not mine:
            return self.get(sid) if ev.wait(120) else None    # when it ends, it has been registered (or handed over to another session)
        try:
            return self._open(sid)
        finally:
            with self.lock:
                self.loading.pop(sid, None)
            ev.set()

    def _open(self, sid):
        paths = [p for p in glob.glob(os.path.join(PROJECTS, '*', sid + '.jsonl')) if _is_file(p)]
        LINKS.ready.wait(30)                  # the first link scan must end so that Codex events fall within the first ordering
        top = LINKS.page_of(sid)              # a run or thread that something started is a card of the page of the top orchestrator (R8b)
        if top != sid:
            return self.get(top)
        if not paths:
            CODEX.refresh()
            e = CODEX.get(sid)
            if not e:
                return None
            if e['guardian'] and e['parent'] and e['parent'] != sid:      # not a root: a guardian review thread (or a sub-agent the board cannot place) is shown on the page of its parent
                return self.get(e['parent'])
        s = Session(paths[0]) if paths else CodexSession(e)
        s.poll()                              # the first read and catch-up must succeed before registering. On an exception it is not registered and is handed to the requester
        self._catch_up(s)                     # (the waiting requests are released by the finally in get, and wake up to try opening again)
        with self.lock:
            self.sessions[sid] = s
        threading.Thread(target=self._warm, args=(s,), daemon=True, name='warm-' + sid[:8]).start()
        return s

    @staticmethod
    def _warm(s):
        """Works out the first picture in the background right after the session is registered: the first state() reads the disk for debates and instructions (about 0.7 s on a large
        session), so the browser that asks next finds it done. A failure only means the first request pays as before."""
        try:
            views.state(s)
        except Exception as e:   # noqa: BLE001 — a warm-up must never matter
            print('warm-up error', s.id, type(e).__name__, e, flush=True)
        finally:
            LATER.open()                      # the first picture is out (or will cost what it costs): what waited for it may start

    @staticmethod
    def _catch_up(s):
        """Reads the last 64 MB of a big rollout (the Claude session's own standalone Codex record, or a linked Codex rollout) in several max_read steps.
        It is not yet registered, so the event numbers (idx) are not public; events appended afterwards are slotted in once more in time order."""
        def behind():
            if s.tail.max_read and s.tail.behind():
                return True
            cx = getattr(s, 'codex', None)
            return bool(cx and any(t.behind() for t in list(cx.tails.values())))
        polls = 0
        while polls < 8 and behind():
            s.poll()
            polls += 1
        if polls:
            with s.lock:
                s.feed.sort(key=lambda e: e['ts'] or 0)

    def loop(self):
        while True:
            LATER.tick()                      # the latest start of what waits for the first picture
            try:
                LINKS.scan()
            except Exception as e:   # noqa: BLE001 — the board must stay up
                print('link scan error', type(e).__name__, e, flush=True)
            for s in list(self.sessions.values()):
                try:
                    s.poll()
                except Exception as e:   # noqa: BLE001 — the board must stay up
                    print('poll error', s.id, e)
            time.sleep(2)


REG = Registry()
