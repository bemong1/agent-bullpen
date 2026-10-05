"""Runs the board code in this process on a case's synthetic HOME (no server) and reads what it shows, in the shape of the oracle's truth.

A field the board does not emit today (node, by, a reason, a resets_at, diagnostics ...) is MISSING. A change of the board's output shape must update this adapter in
the same change: the place to look is the `agent_api` and `state_api` functions, which read the API dicts by key.

Nothing here reads a real HOME: every global that names a folder is patched to the case's folders, `time.time` is frozen per phase, the process table is a fake
/proc (Linux) or a fake `ps` (macOS) and the links cache lives inside the case.
"""

import contextlib
import json
import os
import subprocess
import sys
import types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (REPO, os.path.join(REPO, 'tests')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from compat import patched, server  # noqa: E402  (tests/compat.py: patches a global in every board module that holds it)
from test_proclink import FakeProc  # noqa: E402  (the fake /proc of the lineage tests)

from board import link, procs, views  # noqa: E402



class _Missing:
    """The board does not emit this field at all."""

    def __repr__(self):
        return 'MISSING'

    def __bool__(self):
        return False


MISSING = _Missing()


class Obs:
    """What the board shows for one case: values by (role, field), diagnostics, alerts, events."""

    def __init__(self):
        self.f = {}
        self.diag = set()
        self.diag_params = {}            # (code, role) -> [params of each entry the board shows]
        self.alerts = []                 # alert kinds: 'turn', 'fail', 'stall', 'hb', 'ask', 'say'
        self.events = set()              # (role, kind) of feed events worth checking
        self.notes = []

    def set(self, role, field, value):
        self.f[(role, field)] = value

    def get(self, role, field):
        return self.f.get((role, field), MISSING)


# ---------------------------------------------------------------------------------------------------------------------
# fake process tables
# ---------------------------------------------------------------------------------------------------------------------
def _uid_of(spec):
    u = spec.get('uid')
    return os.geteuid() + 1 if u == 'other' else (os.geteuid() if u is None else u)


def write_sessions(b, procs_list):
    """~/.claude/sessions/<pid>.json of the processes of this phase (the folder is rewritten every phase)."""
    if os.path.isdir(b.sess_dir):
        for f in os.listdir(b.sess_dir):
            os.unlink(os.path.join(b.sess_dir, f))
    os.makedirs(b.sess_dir, exist_ok=True)
    for p in procs_list:
        if p.get('session'):
            with open(os.path.join(b.sess_dir, '%d.json' % p['pid']), 'w') as f:
                json.dump(p['session'], f)


def linux_proc(root, procs_list):
    fp = FakeProc(root)
    for p in procs_list:
        fp.add(p['pid'], p['ppid'], p['argv'], uid=_uid_of(p), start=p['start'], fds=p['fds'])
        if p['env'] is not None:
            items = ['HOME=/home/u', 'LANG=C'] + ['%s=%s' % kv for kv in p['env'].items()]
            with open(os.path.join(root, str(p['pid']), 'environ'), 'wb') as f:
                f.write(b'\0'.join(i.encode() for i in items) + b'\0')
    return root


def ps_stub(procs_list, usable=True):
    """A `subprocess` stand-in for board.procs on macOS: the three `ps` calls the board makes, answered from the process list."""

    def run(argv, **kw):
        if not usable:
            raise OSError('ps is not available')
        a = tuple(argv)
        cmd = {p['pid']: ' '.join(p['argv']) for p in procs_list}
        if a == procs.PS_ARGV:
            out = ''.join('%d %s\n' % (pid, c) for pid, c in cmd.items())
        elif a == procs.PS_PPID_ARGV:
            out = ''.join('%d %d\n' % (p['pid'], p['ppid']) for p in procs_list)
        elif a[:-1] == procs.PS_ENV_ARGV:
            spec = next((p for p in procs_list if str(p['pid']) == a[-1]), None)
            if spec is None or spec['env'] is None:
                return types.SimpleNamespace(returncode=1, stdout=b'')
            out = '%s HOME=/home/u %s\n' % (cmd[spec['pid']], ' '.join('%s=%s' % kv for kv in spec['env'].items()))
        else:
            return types.SimpleNamespace(returncode=1, stdout=b'')
        return types.SimpleNamespace(returncode=0, stdout=out.encode())

    return types.SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError, TimeoutExpired=subprocess.TimeoutExpired, PIPE=subprocess.PIPE,
                                 DEVNULL=subprocess.DEVNULL, CalledProcessError=subprocess.CalledProcessError)


# ---------------------------------------------------------------------------------------------------------------------
@contextlib.contextmanager
def frozen(now):
    with mock.patch('time.time', lambda: now):
        yield


def observe(b):
    """The case, observed: scan the phases in order (a new boot number means a fresh board process), then read the final state."""
    case = b.case
    obs = Obs()
    boot = None
    stack = None
    objs = None
    try:
        for i, ph in enumerate(b.phases):
            if boot != ph.boot:
                if stack is not None:
                    stack.close()
                boot = ph.boot
                links, index = server.LinkIndex(), server.CodexIndex()
                with frozen(ph.now):                       # the cache reads the real date (a record older than a day is dropped): at the case's own time
                    links.lineage.enable_cache(os.path.join(b.cache, 'links.json'))
                stack = contextlib.ExitStack()
                stack.enter_context(patched(HOME=b.home, CLAUDE_HOME=b.claude, PROJECTS=b.projects, CODEX_HOME=b.codex,
                                            CODEX_SESSIONS=os.path.join(b.codex, 'sessions'), CODEX_NAMES=os.path.join(b.codex, 'session_index.jsonl'),
                                            CODEX=index, LINKS=links))
                stack.enter_context(mock.patch.dict(os.environ, {'HOME': b.home}))
                objs = types.SimpleNamespace(links=links, index=index, sessions={}, pages={})
            os_kind = case.v.get('os', 'linux')
            write_sessions(b, ph.procs)
            with contextlib.ExitStack() as st:
                if os_kind == 'linux':
                    root = linux_proc(os.path.join(b.root, 'proc%d' % i), ph.procs)
                    st.enter_context(mock.patch.object(procs, 'PROC', root))
                else:
                    st.enter_context(mock.patch.object(procs, 'PROC', os.path.join(b.root, 'no-proc')))
                    st.enter_context(mock.patch.object(procs, 'subprocess', ps_stub(ph.procs, usable=(os_kind == 'mac'))))
                st.enter_context(frozen(ph.now))
                procs.reset()
                if ph.hook:
                    ph.hook()                              # the records that arrive between two looks
                objs.index.refresh(force=True)
                objs.links.scan()
                if case.bundle == 'cxo':
                    step_page(b, objs)                    # the server opens a page once and keeps polling it: what comes between two looks (a late record) reaches a page that is open already
                if i == len(b.phases) - 1:
                    read_final(b, obs, objs)
        return obs
    finally:
        if stack is not None:
            stack.close()
        procs.reset()


def read_final(b, obs, objs):
    """Fills `obs` from the board's state after the last phase (the clock is still frozen at that phase's time)."""
    bundle = b.case.bundle
    if bundle in ('aff', 'cpl'):
        read_aff(b, obs, objs)
    if bundle in ('deb', 'cpl'):
        read_deb(b, obs, objs)
    if bundle == 'room':
        read_room(b, obs, objs)
    if bundle in ('sta', 'cpl'):
        read_sta(b, obs, objs)
    if bundle == 'cxo':
        read_cxo(b, obs, objs)
    if bundle == 'rer':
        read_rer(b, obs, objs)
    read_diag(b, obs, objs)


def read_diag(b, obs, objs):
    """The diagnostics the board shows for the page (/api/diag: the entries of the state pass) as (code, role): `child` for the participant under test and for the
    debate folders (an entry about no agent), `orch` for the orchestrator and the session. An entry about another agent is not asked about here.
    Every code is reported with the small values of its entry; which of them a case's truth speaks to is the answer's decision (oracle.diag_scope), not the adapter's."""
    if not b.main_path:
        return
    s, st = open_state(b, objs)
    G = b.ids.get('child')
    rer_roles = {b.ids[r]: r for r in ('a1', 'a2', 'b1', 'b2') if r in b.ids} if getattr(b.case, 'bundle', None) == 'rer' else {}
    for e in s._diag or ():
        who = None
        if e['scope'] == 'agent' and e['agent'] == G:
            who = 'child'
        elif e['scope'] == 'agent' and e['agent'] in rer_roles:
            who = rer_roles[e['agent']]                      # a rerun: the diagnostics of the runs of each participant
        elif e['scope'] == 'agent' and e['code'] == 'proc_unknown':
            who = 'orch'                                    # every agent says it (the process table is one for the page): beside the participant, it is the page that says it
        elif e['scope'] == 'unit':
            who = 'child'
        elif e['scope'] in ('orch', 'session'):
            who = 'orch'
        if who is not None:
            obs.diag.add((e['code'], who))
            obs.diag_params.setdefault((e['code'], who), []).append(dict(e.get('params') or {}))


# ---------------------------------------------------------------------------------------------------------------------
# shared readers
# ---------------------------------------------------------------------------------------------------------------------
def page_of(b, objs):
    """The page of the top orchestrator, made when it can be (its record is there, and its thread is listed), then kept: (Session or None)."""
    key = b.main_path
    s = objs.pages.get(key)
    if s is None:
        if b.meta.get('page') == 'codex':
            e = objs.index.get(b.ids['top'])
            s = server.CodexSession(e) if e is not None else None      # the page of a Codex thread: the thread itself is the orchestrator
        elif os.path.isfile(key):
            s = server.Session(key)
        if s is not None:
            objs.pages[key] = s
    return s


def step_page(b, objs):
    """One look of the server at the page of this phase: it is opened at the first look that can and polled at every later one, and its state is drawn (the page is drawn every few seconds)."""
    s = page_of(b, objs) if b.main_path else None
    if s is not None:
        s.poll()
        views.state(s)


def open_state(b, objs, path=None):
    """The API state of the main session (opened once per observation, or kept from the earlier looks of a case that is looked at in steps): (Session, state dict)."""
    key = path or b.main_path
    if key in objs.sessions:
        return objs.sessions[key]
    s = page_of(b, objs) if key == b.main_path and key in objs.pages or b.case.bundle == 'cxo' else None
    if s is None:
        if b.meta.get('page') == 'codex':
            s = server.CodexSession(objs.index.get(b.ids['top']))      # the page of a Codex thread: the thread itself is the orchestrator
        else:
            s = server.Session(key)
    s.poll()
    s.poll()
    s.walk_repos()                    # the background walk of the repository for debate folders no record names, done once (the server does it a few seconds after opening)
    st = views.state(s)
    objs.sessions[key] = (s, st)
    return s, st


def agent_api(st, aid):
    """The agent dict of the API state (None if the board does not list it)."""
    return next((a for a in st['agents'] if a['id'] == aid), None)


def link_rule_class(rule):
    return 'certain' if link.certain(rule) else 'guess'


# ---------------------------------------------------------------------------------------------------------------------
# affiliation
# ---------------------------------------------------------------------------------------------------------------------
SESSION_ROLES_SKIP = ('orch', 'orch2', 'orch_new', 'mid', 'child', 'forkbase')


def read_aff(b, obs, objs):
    v = b.case.v
    links = objs.links
    G = b.ids.get('child')
    if G is None:
        return
    own = links.cli_owners.get(G) if v.get('target', 'cli') == 'cli' or b.case.bundle == 'cpl' else links.owners.get(G)
    obs.set('child', 'tree', own['sid'] if own else None)
    obs.set('child', 'rule_class', 'none' if not own else link_rule_class(own['rule']))
    node = by = MISSING
    if own and b.main_path:
        # whoever the tree is (the main session, or the `claude -p` child that launched a grand-child), the screen is the top orchestrator's: the child
        # is read there, under its launcher. The board does not list a grand-child on that screen yet, so its node and `by` stay MISSING until it does.
        _, st = open_state(b, objs)
        a = agent_api(st, G)
        if a is not None:
            node = a['node'] if 'node' in a else MISSING
            by = (tuple(a['by']) if a['by'] else None) if 'by' in a else MISSING       # the API gives [tree, node]; the truth is a pair
            if isinstance(a.get('link'), dict) and 'rule_class' in a['link']:
                obs.set('child', 'rule_class', a['link']['rule_class'])
    obs.set('child', 'node', node)
    obs.set('child', 'by', by)
    reason = None
    for sid, items in links.unlinked_map.items():
        for it in items:
            if it['id'] == G:
                reason = it['reason']
    obs.set('child', 'unlinked', reason)
    for role, sid in b.ids.items():
        if role in SESSION_ROLES_SKIP or role == 'sub' or not isinstance(sid, str) or sid.startswith('a') and len(sid) == 17:
            continue
        o = links.cli_owners.get(sid)
        obs.set(role, 'tree', o['sid'] if o else None)


def _one(values):
    """The one value every place agrees on, None for no place, or the sorted tuple of the different ones (a scalar truth never equals it)."""
    seen = sorted(set(values), key=str)
    return seen[0] if len(seen) == 1 else (None if not seen else tuple(seen))


def file_below(path, unit_dir):
    """The file a cell stands for, as the path below its debate folder without the extension (`r01/B`): the spelling of the round folder is part of it."""
    if not path:
        return None
    return os.path.splitext(os.path.relpath(path, unit_dir))[0]


def deb_view(st, rel, G):
    """What the board's debate list says about the agent G: (its API dict or None, its cells as (unit, round, seat, state, file), whether somebody reads one of its cells,
    the debate folders listed). The board may show an agent in more than one place; every one of them counts."""
    placed = []
    readers = False
    names = set()
    a = agent_api(st, G) if G else None
    if a is not None:
        names.add(a.get('tag') or a['id'][:6])
    listed = []
    for d in st['debates']:
        for t in d['topics']:
            listed.append(rel(t['dir']))
            for row in t['rows']:
                for cell in row['cells']:
                    if G and cell['agent'] == G:
                        placed.append((rel(t['dir']), cell['round'] if len(t['rounds']) else None, row['p'], cell['state'], file_below(cell.get('path'), t['dir'])))
                    if names & set(cell.get('readers') or ()):
                        readers = True
    return a, placed, readers, listed


def set_places(obs, role, a, placed, readers, rel):
    """The fields of a participant: where it sits (unit, round, seat, cell, role, placements)."""
    unit = _one(p[0] for p in placed)
    if not placed and a is not None and a.get('work_units'):       # a reader, or a held agent: the debate it works in (`units` is where it sits)
        unit = rel(sorted(a['work_units'])[0])
    obs.set(role, 'unit', unit)
    obs.set(role, 'round', _one(p[1] for p in placed))
    obs.set(role, 'seat', _one(p[2] for p in placed))
    obs.set(role, 'cell', _one(p[3] for p in placed))
    obs.set(role, 'role', 'writer' if placed else ('reader' if readers else 'none'))
    obs.set(role, 'placements', frozenset('%s|%s|%s|%s' % (p[0], p[1], p[2], p[4]) for p in placed))


def read_deb(b, obs, objs):
    if not b.main_path:
        return
    s, st = open_state(b, objs)
    repo = b.meta['repo']
    rel = lambda p: os.path.relpath(p, repo) if p else None
    G = b.ids.get('child')
    a, placed, readers, listed = deb_view(st, rel, G)
    if G:
        set_places(obs, 'child', a, placed, readers, rel)
    obs.set('listing', 'unit', next(iter(listed), None))
    obs.set('listing', 'units', frozenset(listed))
    if b.meta.get('edit'):                          # the rows and rounds the board gives the editing job beside the topics (none when it is not listed at all)
        edit = [t for d in st['debates'] for t in d['topics'] if rel(t['dir']) == rel(b.meta['edit'])]
        obs.set('listing', 'edit_rows', frozenset(row['p'] for t in edit for row in t['rows']))
        obs.set('listing', 'edit_rounds', frozenset(r for t in edit for r in t['rounds']))


def read_room(b, obs, objs):
    """The room scene: every participant of the page where it sits, the folders the board lists and the title each is given."""
    if not b.main_path:
        return
    s, st = open_state(b, objs)
    repo = b.meta['repo']
    rel = lambda p: os.path.relpath(p, repo) if p else None
    for role in b.meta['page']:
        a, placed, readers, _ = deb_view(st, rel, b.ids.get(role))
        set_places(obs, role, a, placed, readers, rel)
    obs.set('listing', 'units', frozenset(rel(t['dir']) for d in st['debates'] for t in d['topics']))
    obs.set('listing', 'titles', frozenset('%s|%s' % (rel(t['dir']), t['title']) for d in st['debates'] for t in d['topics']))
    obs.set('listing', 'finals', frozenset('%s|%s' % (rel(t['dir']), t['final']['rel']) for d in st['debates'] for t in d['topics']
                                           if t['final']['exists'] and t['final']['auto'] and (t['final']['rel'] or '').startswith('../')))      # a topic closed by the conclusion of its bundle
    guide = b.meta.get('guide')
    obs.set('listing', 'guide_opens', frozenset([rel(guide)]) if guide and s.allowed_file(guide) else frozenset())      # the document view opens the guide of a room the page shows


def read_sta(b, obs, objs):
    if not b.main_path:
        return
    s, st = open_state(b, objs)
    ids = {a['id'] for a in st['agents']}
    o = st['orch']
    obs.set('orch', 'orch_state', o.get('state', MISSING))
    obs.set('orch', 'resets_at', o.get('resets_at', MISSING))
    kinds, say_limit, stray = set(), False, False
    for al in st['alerts']:
        kinds.add(al['id'].split(':')[0])
    obs.set('orch', 'alert', kinds)
    for ev in st['feed']:
        if ev['kind'] == 'orch_say' and 'limit' in (ev.get('text') or '').lower():
            say_limit = True
        if ev['kind'] == 'notify' and ev.get('from') not in ids:
            stray = True
    obs.set('orch', 'event', {k for k, on in (('orch_say_limit', say_limit), ('notify_stray', stray)) if on})
    G = b.ids.get('child')
    if G is not None:
        a = agent_api(st, G)
        if a is None:
            for f in ('status', 'reason', 'resets_at'):
                obs.set('child', f, MISSING)
            obs.set('child', 'alert', set())
        else:
            obs.set('child', 'status', a.get('status', MISSING))
            obs.set('child', 'reason', a.get('reason', MISSING))
            obs.set('child', 'resets_at', a.get('resets_at', MISSING))
            obs.set('child', 'by', (tuple(a['by']) if a['by'] else None) if 'by' in a else MISSING)
            obs.set('child', 'alert', {al['id'].split(':')[0] for al in st['alerts'] if al.get('agent') == G})


# ---------------------------------------------------------------------------------------------------------------------
# Codex orchestrator
# ---------------------------------------------------------------------------------------------------------------------
EVENT_KINDS = ('spawn', 'orch_msg', 'agent_msg', 'handback')           # the kinds of event a sub-agent's story is made of (others, such as the orchestrator's own words, are not about it)


def owner_info(links, G):
    """What the board says about who started the run or thread G: {tree, node, certain} or None. The board's own graph (`owner_of`) when it has one, else the older
    tables of the link index (`cli_owners` for a `claude -p` run, `owners` for a Codex thread). `tree` is the launching session or root thread."""
    of = getattr(links, 'owner_of', None)
    if of is not None:
        o = of(G)
        return None if o is None else {'tree': o.get('parent'), 'node': o['node'] if 'node' in o else MISSING, 'certain': bool(o.get('certain'))}
    o = links.cli_owners.get(G) or links.owners.get(G)
    if not o:
        return None
    return {'tree': o['sid'], 'node': o['node'] if 'node' in o else MISSING, 'certain': link.certain(o['rule'])}


def read_cxo(b, obs, objs):
    """The page of the top orchestrator (a Claude session or a Codex thread) and the link index, read for every run and thread of the scene that the truth speaks about."""
    v = b.case.v
    s, st = open_state(b, objs)
    links, index = objs.links, objs.index
    repo = b.meta['repo']
    rel = lambda p: os.path.relpath(p, repo) if p else None
    page_of = getattr(links, 'page_of', None)
    for role in ('child', 'mid', 'host', 'guardian'):
        G = b.ids.get(role)
        if G is None or not isinstance(G, str):
            continue
        a = agent_api(st, G)
        obs.set(role, 'listed', 'yes' if a is not None else 'none')
        if role == 'guardian':
            continue
        o = owner_info(links, G)
        obs.set(role, 'tree', o['tree'] if o else None)
        obs.set(role, 'rule_class', 'none' if not o else ('certain' if o['certain'] else 'guess'))
        obs.set(role, 'node', o['node'] if o else MISSING)
        obs.set(role, 'page', page_of(G) if page_of is not None else MISSING)
        if a is None:
            for f in ('parent', 'status', 'label'):
                obs.set(role, f, MISSING)
        else:
            obs.set(role, 'parent', a['parent'] if 'parent' in a else MISSING)
            obs.set(role, 'status', a.get('status', MISSING))
            obs.set(role, 'label', a.get('tag') or a.get('title') or MISSING)
    G = b.ids.get('child')
    if v['subj'] == 'cx_sub' and G:
        mine = [ev for ev in st['feed'] if G in (ev.get('agent'), ev.get('to'), ev.get('from'))]
        obs.set('child', 'events', frozenset(ev['kind'] for ev in mine if ev['kind'] in EVENT_KINDS))
        obs.set('child', 'spawn_text', next((ev.get('text') for ev in mine if ev['kind'] == 'spawn'), MISSING))
        e = index.get(G)
        obs.set('child', 'first_user', e['first_user'] if e else MISSING)
    obs.set('orch', 'guardian_calls', (st['orch'].get('tokens') or {}).get('guardian', {}).get('calls', 0))
    if v['topic'] != 'none' and G:
        a, placed, readers, listed = deb_view(st, rel, G)
        set_places(obs, 'child', a, placed, readers, rel)
        obs.set('listing', 'units', frozenset(listed))


# ---------------------------------------------------------------------------------------------------------------------
# a rerun
# ---------------------------------------------------------------------------------------------------------------------
def read_rer(b, obs, objs):
    """The two runs of each participant: the tree, the call (the Bash call the link names), the start and the title the page gives it, and the seat it holds in the debate list."""
    s, st = open_state(b, objs)
    links = objs.links
    repo = b.meta['repo']
    rel = lambda p: os.path.relpath(p, repo) if p else None
    for role in ('a1', 'a2', 'b1', 'b2'):
        G = b.ids.get(role)
        if G is None:
            continue
        o = links.cli_owners.get(G) or links.owners.get(G)
        obs.set(role, 'tree', o['sid'] if o else None)
        obs.set(role, 'call', (o['call'] if 'call' in o else MISSING) if o else None)
        a, placed, readers, listed = deb_view(st, rel, G)
        obs.set(role, 'start', a['spawn_ts'] if a is not None and a.get('spawn_ts') else MISSING)
        obs.set(role, 'title', a['title'] if a is not None else MISSING)
        set_places(obs, role, a, placed, readers, rel)
        obs.set('listing', 'units', frozenset(listed))
