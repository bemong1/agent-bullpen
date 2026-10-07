"""The contract cases (CONTRACT 3.2): synthetic cases, with the truth, run through the judgment.

Each case is a plain description of what a session did (events, windows, launch keys, room tags) and of the disk (folders, files with their times and bytes, links, worktrees). The test builds the
disk in a temporary folder, makes the SessionFacts of the description and compares units.assign with what the case says is true: the list of debates and the current one, the cells (owner, agent, editors,
evidence, state, previous, hint), where agents are placed, the rooms, the finals and whether a debate can be closed, and the diagnostics. The steps of `then` are cumulative: step k adds to step k - 1
and judges again. A key that a case does not give is not compared.

The cases are `tests/data/contract_cases.json`; CONTRACT_CASES names another file (the cases of the notes while the repository's copy is not there).

    python3 -m unittest tests.test_contract_cases
"""
import contextlib
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from board import facts as F, units as U  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.environ.get('CONTRACT_CASES') or os.path.join(HERE, 'data', 'contract_cases.json')
SLOTS = ('spawn_prompt', 'received', 'orch_msgs', 'codex_turn_user', 'cli_talk', 'bash_description', 'orch_say', 'brief_body', 'brief_table', 'role_lines', 'report_body+last_message', 'final_body')


def load_cases():
    """The cases, or an empty table when the file is not there (the tests of the cases are then skipped: the file comes with the oracle's copy of it)."""
    if not os.path.exists(CASES_FILE):
        return {'version': 4, 'cases': []}
    with open(CASES_FILE, encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------------------------------------------------
# a case -> a disk and the facts of a session
# ---------------------------------------------------------------------------------------------------------------------
def apply_step(case, add):
    """The case after a step of `then`: agents added, fields of agents set, writes and planned outputs appended, files and folders added or changed."""
    c = copy.deepcopy(case)
    c['agents'] += copy.deepcopy(add.get('agents', []))
    by = {a['id']: a for a in c['agents']}
    for aid, kv in add.get('set_agent', {}).items():
        by[aid].update(copy.deepcopy(kv))
    for aid, ws in add.get('agent_writes', {}).items():
        by[aid]['writes'] += copy.deepcopy(ws)
    for aid, ps in add.get('agent_planned', {}).items():
        by[aid]['planned'] += copy.deepcopy(ps)
    c['disk']['files'].update(copy.deepcopy(add.get('files', {})))
    c['disk']['dirs'] += [d for d in add.get('dirs', []) if d not in c['disk']['dirs']]
    return c


def body_of(meta):
    return meta['body'].encode('utf-8') if meta.get('body') is not None else b'x' * meta['size']


def build_disk(root, disk, bodies=None):
    """The folders, files (with their bytes and times), links and worktrees of a case under `root`. `bodies` {path: bytes} replaces the bytes of a file."""
    for d in disk.get('dirs', ()):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for rel, meta in disk.get('files', {}).items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = (bodies or {}).get(rel)
        with open(path, 'wb') as f:
            f.write(data if data is not None else body_of(meta))
        ns = int(round(meta['mtime'] * 1e9))
        if meta.get('coarse'):
            ns = (ns // 10 ** 9) * 10 ** 9
        os.utime(path, ns=(ns, ns))
    for alias, target in disk.get('links', {}).items():
        os.makedirs(os.path.dirname(os.path.join(root, alias)) or root, exist_ok=True)
        os.symlink(os.path.join(root, target), os.path.join(root, alias))
    for top, common in disk.get('git', {}).items():
        gd = os.path.join(root, common, 'worktrees', top.replace('/', '_'))
        os.makedirs(gd, exist_ok=True)
        with open(os.path.join(gd, 'commondir'), 'w') as f:
            f.write('../..\n')
        with open(os.path.join(root, top, '.git'), 'w') as f:
            f.write('gitdir: %s\n' % gd)


def gkey(root_group, launch=None):
    """The group of a hint: a plain name 'm1' is ('claude', 'S', None, 'm1')."""
    return root_group if isinstance(root_group, tuple) else ('claude', 'S', None, root_group)


def write_event(root, who, w):
    return F.WriteEvent(agent=who, path=os.path.join(root, w['path']), ts=w['ts'], kind=w['kind'], evidence=w['evidence'], ok=w['ok'], call=w.get('call'), run=w.get('run'),
                        proof=w.get('proof', 'tool'), span=tuple(w['span']) if w.get('span') else None,
                        shas=(hashlib.sha1(w['out'].encode('utf-8')).hexdigest(),) if w.get('out') is not None else ())


def window(root, who, w):
    return F.CmdWindow(agent=who, call=w['call'], t0=w['t0'], t1=w['t1'], ok=w['ok'], reads=tuple(os.path.join(root, r) for r in w.get('reads', ())))


def agent_facts(root, a):
    launch = a.get('launch')
    tag = a.get('tag')
    return U.AgentFacts(
        id=a['id'], provider=a['provider'], origin=a['origin'], start=a['start'], first=a['first'], last=a['last'], status=a['status'],
        launch=F.LaunchKey(launch['provider'], launch['tree'], launch['node'], launch['group'], launch['call']) if launch else None,
        tag=F.Tag(os.path.join(root, tag['room']), tag['seat'], tag['source'], tag.get('run')) if tag else None,
        writes=tuple(write_event(root, a['id'], w) for w in a['writes']),
        reads=tuple(F.ReadEvent(a['id'], os.path.join(root, r['path']), r['ts'], r['via']) for r in a['reads']),
        planned=tuple(F.Planned(a['id'], os.path.join(root, p['path']), p['op'], p.get('call'), p.get('run'), p.get('src', 'command'), p.get('ts', 0.0)) for p in a['planned']),
        windows=tuple(window(root, a['id'], w) for w in a['windows']), windows_dropped=tuple(a['windows_capped']) if a.get('windows_capped') else None,
        writes_dropped=tuple(a['writes_dropped']) if a.get('writes_dropped') else None, lost=tuple(a['lost']) if a.get('lost') else None,
        run=a.get('run'), run_start=a.get('run_start', 0.0), sent_to=tuple(a.get('sent_to', ())))


def session_facts(root, case):
    o = case['orch']
    hints = {os.path.join(root, f): F.Hint(h['ts'], frozenset(h['calls']), frozenset(gkey(g) for g in h['groups'])) for f, h in o.get('hints', {}).items()}
    return U.SessionFacts(
        agents=[agent_facts(root, a) for a in case['agents']], orch_writes=tuple(write_event(root, 'orch', w) for w in o['writes']),
        orch_windows=tuple(window(root, 'orch', w) for w in o['windows']), orch_windows_dropped=tuple(o['windows_capped']) if o.get('windows_capped') else None,
        orch_lost=tuple(o['lost']) if o.get('lost') else None, hints=hints, walked=tuple(os.path.join(root, w) for w in case.get('walked', ())))


# ---------------------------------------------------------------------------------------------------------------------
# what the judgment gave -> what a case says
# ---------------------------------------------------------------------------------------------------------------------
class Shown:
    """A Judgement written the way a case writes the truth: paths relative to the root of the case."""

    def __init__(self, root, jd, agents):
        self.root, self.jd = root, jd
        self.rel = lambda p: None if p is None else (os.path.relpath(p, root) if p.startswith(root) else p)
        self.agents = agents

    def cells(self):
        return {'%s|%s|%s' % (self.rel(u), rd, st): {'owner': c.owner, 'agent': c.agent, 'editors': list(c.editors), 'evidence': c.evidence, 'state': c.state, 'previous': c.previous, 'hint': c.hint}
                for (u, rd, st), c in self.jd.cells.items()}

    def placed(self):
        return {a: ({'unit': self.rel(p.unit), 'topic': self.rel(p.topic), 'why': p.why, 'whys': list(p.whys), 'sure': p.sure} if (p := self.jd.placed.get(a)) else None) for a in self.agents}

    def rooms(self):
        return {self.rel(f): {'kind': r.kind, 'sure': r.sure, 'why': r.why, 'members': list(r.members)} for f, r in self.jd.rooms.items()}

    def final(self, key):
        f = self.jd.finals[os.path.join(self.root, key)]
        return {'confirmed': f.confirmed, 'path': self.rel(f.path), 'by': f.by, 'why': list(f.why), 'candidates': [self.rel(p) for p in f.candidates]}

    def diag(self):
        return sorted(({'code': d['code'], 'agent': d['agent'], 'unit': self.rel(d['unit']), 'detail': d['detail']} for d in self.jd.diag),
                      key=lambda d: (d['code'], d['unit'] or '', d['detail'] or '', d['agent'] or ''))

    def compare(self, expect):
        """[(what, got, want)] of what differs from the truth of a step."""
        out = []

        def check(what, got, want):
            if got != want:
                out.append((what, got, want))
        jd = self.jd
        if 'listed' in expect:
            check('listed', sorted(self.rel(r) for r in jd.listed), sorted(expect['listed']))
        if 'hinted' in expect:
            check('hinted', sorted(self.rel(r) for r in jd.hinted), sorted(expect['hinted']))
        if 'current' in expect:
            check('current', self.rel(jd.current), expect['current'])
        if 'cells' in expect:
            check('cells', self.cells(), expect['cells'])
        if 'placed' in expect:
            check('placed', self.placed(), expect['placed'])
        if 'rooms' in expect:
            check('rooms', self.rooms(), expect['rooms'])
        for key, want in expect.get('finals', {}).items():
            check('final ' + key, self.final(key), want)
        for key, want in expect.get('closable', {}).items():
            check('closable ' + key, jd.closable.get(os.path.join(self.root, key)), want)
        if 'diag' in expect:
            check('diag', self.diag(), sorted(expect['diag'], key=lambda d: (d['code'], d['unit'] or '', d['detail'] or '', d['agent'] or '')))
        return out


@contextlib.contextmanager
def broad_root(root):
    """The world of a case: nothing in it is a scratch file."""
    broad = U.too_broad
    with mock.patch.object(U, 'SCRATCH_DIRS', ()), mock.patch.object(U, 'too_broad', lambda p: broad(p) or p in (os.path.join(root, 'tmp'), os.path.join(root, 'var/tmp'))):
        yield


def judge_case(case, bodies=None, keep=False):
    """Builds the disk of a case in a new folder and judges it: (the folder, the Shown of the Judgement). The caller removes the folder."""
    root = os.path.realpath(tempfile.mkdtemp(prefix='contract-'))
    try:
        build_disk(root, case['disk'], bodies)
        sf = session_facts(root, case)
        cat = U.Catalog()
        with broad_root(root):
            jd = U.assign(sf, cat)
        return root, Shown(root, jd, [a['id'] for a in case['agents']]), cat
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def steps(case):
    """[(case at that step, the truth of it)] for the case and each step of `then`, the steps accumulating."""
    cur, out = copy.deepcopy(case), [(copy.deepcopy(case), case['expect'])]
    for t in case['then']:
        cur = apply_step(cur, t['add'])
        out.append((copy.deepcopy(cur), t['expect']))
    return out


class ContractCases(unittest.TestCase):
    pass


def _make(case):
    def test(self):
        for i, (cur, expect) in enumerate(steps(case)):
            root, shown, _cat = judge_case(cur)
            try:
                diffs = shown.compare(expect)
            finally:
                shutil.rmtree(root, ignore_errors=True)
            self.assertEqual(diffs, [], '%s step %d (%s)' % (case['id'], i, case['title']))
    return test


for _case in load_cases()['cases']:
    setattr(ContractCases, 'test_' + _case['id'], _make(_case))


class Shape(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(CASES_FILE), 'no contract cases file (tests/data/contract_cases.json or CONTRACT_CASES)')
    def test_the_file_has_the_cases_of_the_contract(self):
        blob = load_cases()
        self.assertEqual(blob['version'], 4)
        self.assertEqual(len(blob['cases']), 127)
        self.assertEqual(len({c['id'] for c in blob['cases']}), 127)


if __name__ == '__main__':
    unittest.main()
