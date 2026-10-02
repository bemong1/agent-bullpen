"""Starts the real server.py in a separate process on a synthetic HOME (tools/synth_home.py) and checks: the session list (orchestration + solo), debate cells and the final (auto),
the Codex link, talk-card events (scope=agents), the plan cache, --live. In default mode it also watches, from inside the server process, that no credential file is opened and there are zero outside requests.
The tests read only temp folders (they never open the real HOME, ~/.claude or ~/.codex).

    python3 -m unittest discover -s tests
"""
import calendar
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import isolated_env  # noqa: E402  (first of the board imports: it pins HOME and the cache to a throwaway folder)
import synth_home  # noqa: E402

SERVER = os.path.join(ROOT, 'server.py')
FAKE_TOKEN = 'FAKE-TOKEN-must-never-be-read'
AGENT_KINDS = {'spawn', 'orch_msg', 'handback', 'peer', 'agent_msg', 'xread'}
USER_KINDS = {'user_say', 'orch_say', 'orch_ask', 'user_answer'}

# A watch that runs inside the server process: it writes to a file any open of the credential files or the old usage.json, any urlopen, and any connection or name lookup to a non-loopback host.
# With the watch on, it does the same as `python3 server.py <args>` (runs __main__ with runpy).
GUARD = r'''
import builtins, os, runpy, socket, sys, urllib.request
root, log, deny = sys.argv[1], sys.argv[2], [p for p in sys.argv[3].split(os.pathsep) if p]
sys.path.insert(0, root)
sink = open(log, 'a', buffering=1)
deny = {os.path.realpath(p) for p in deny}
def note(*a):
    sink.write(' '.join(map(str, a)) + '\n')
_open, _osopen = builtins.open, os.open
def watched(path):
    return isinstance(path, (str, bytes, os.PathLike)) and os.path.realpath(os.fsdecode(path)) in deny
def guarded_open(file, *a, **k):
    if watched(file):
        note('open', file)
    return _open(file, *a, **k)
def guarded_osopen(path, *a, **k):
    if watched(path):
        note('os.open', path)
    return _osopen(path, *a, **k)
builtins.open, os.open = guarded_open, guarded_osopen
def blocked(*a, **k):
    note('urlopen')
    raise OSError('blocked by test')
urllib.request.urlopen = blocked
LOCAL = (None, '', 'localhost', '127.0.0.1', '::1')
_connect = socket.socket.connect
def connect(self, addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    if host not in LOCAL:
        note('connect', host)
    return _connect(self, addr)
socket.socket.connect = connect
_gai = socket.getaddrinfo
def gai(host, *a, **k):
    if host not in LOCAL:
        note('getaddrinfo', host)
    return _gai(host, *a, **k)
socket.getaddrinfo = gai
sys.argv = [os.path.join(root, 'server.py')] + sys.argv[4:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def start_live(info):
    """synth_home.start_live, with the Popen objects of the fake processes kept next to their pids: (pids, procs). Pass procs to reap() once stop_live has ended the
    processes: a Popen object that is dropped while its child has not been waited for warns that the subprocess is still running."""
    procs, real = [], subprocess.Popen

    class Recorded(real):
        def __init__(self, *args, **kw):
            real.__init__(self, *args, **kw)
            procs.append(self)
    with mock.patch.object(synth_home.subprocess, 'Popen', Recorded):
        return synth_home.start_live(info), procs


def reap(procs):
    """Waits for the fake processes (stop_live has signalled them); one that is still there is killed."""
    for p in procs:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


class Board:
    """server.py started on a synthetic HOME. With guard=(log path, files that must not be opened) it runs under GUARD. On close it stops only this process."""

    def __init__(self, home, orch, guard=None, args=()):
        env = isolated_env(home, AGENT_BULLPEN_LANG='ko')      # the ready line below and the start output the tests read are Korean, whatever LANG the runner has
        cmd = [sys.executable, SERVER] if not guard else [sys.executable, '-c', GUARD, ROOT, guard[0], os.pathsep.join(guard[1])]
        self.out = tempfile.TemporaryFile('w+')
        self.p = subprocess.Popen(cmd + ['--port', '0'] + list(args), stdout=self.out, stderr=subprocess.STDOUT, env=env, cwd=ROOT, stdin=subprocess.DEVNULL)
        self.orch, self.port = orch, None
        end = time.time() + 60
        while time.time() < end and self.p.poll() is None and self.port is None:
            if '세션 %s 읽음' % orch in self.text():
                self.port = int(self.text().split('http://localhost:')[1].split('/')[0])
            else:
                time.sleep(0.05)
        if self.port is None:
            raise AssertionError('server did not come up:\n' + self.text())

    def text(self):
        self.out.seek(0)
        return self.out.read()

    def get(self, path, session=True, raw=False):
        if session and 'session=' not in path:
            path += ('&' if '?' in path else '?') + 'session=' + self.orch
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d%s' % (self.port, path), timeout=60) as r:
                body = r.read()
                return r.status, body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.out.close()


def read(path):
    with open(path) as f:
        return f.read()


def synth_home_codex_unknown(home):
    """codex_unknown_here for the synthetic HOME `home`."""
    return codex_unknown_here(os.path.join(home, '.codex', 'sessions'))


def codex_unknown_here(sessions):
    """Whether an unfinished Codex agent reads as `unknown` on this machine. Without /proc (macOS) ps cannot say which rollout a codex process holds open, so once any codex
    process runs (a fake one of --live, or a real one of the developer) the agent is neither running nor ended. With /proc, or with no codex process at all, it is not unknown."""
    sys.path.insert(0, ROOT)
    from board import procs
    procs.reset()
    try:
        return not procs.has_proc() and bool(procs.codex_procs(sessions)['any'])
    finally:
        procs.reset()


def tree_digest(root):
    """List of (relative path, content) for the files under a folder. Modification times are left out."""
    out = []
    for d, _, files in sorted(os.walk(root)):
        for f in sorted(files):
            p = os.path.join(d, f)
            with open(p, 'rb') as fh:
                out.append((os.path.relpath(p, root), fh.read()))
    return out


class SynthBoard(unittest.TestCase):
    """One server on a synthetic HOME in default mode is shared by all tests. Decoys shaped like credential files are planted."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.info = synth_home.build(os.path.join(cls.tmp.name, 'home'))
        h = cls.info['home']
        cls.decoys = {
            os.path.join(cls.info['claude'], '.credentials.json'): json.dumps({'claudeAiOauth': {'accessToken': FAKE_TOKEN, 'expiresAt': (time.time() + 3600) * 1000}}),
            os.path.join(cls.info['codex'], 'auth.json'): json.dumps({'OPENAI_API_KEY': FAKE_TOKEN}),
            os.path.join(h, '.cache', 'agent-bullpen', 'usage.json'): json.dumps({'v': {'as_of': 1.0, 'marker': FAKE_TOKEN}, 'tried': 1.0}),
        }
        for p, text in cls.decoys.items():
            synth_home.put(p, text)
            os.utime(p, (1000000000, 1000000000))
        cls.log = os.path.join(cls.tmp.name, 'guard.log')
        cls.board = Board(h, cls.info['orch'], guard=(cls.log, list(cls.decoys)))
        cls.state = cls.board.get('/api/state')[1]

    @classmethod
    def tearDownClass(cls):
        cls.board.close()
        cls.tmp.cleanup()

    def agents(self):
        return {a['tag']: a for a in self.state['agents']}

    # ---------- session list ----------
    def test_session_list_has_orchestration_and_solo(self):
        code, body = self.board.get('/api/sessions')
        self.assertEqual(code, 200)
        by_id = {s['id']: s for s in body['sessions']}
        orch, solo = by_id[self.info['orch']], by_id[self.info['solo']]
        self.assertEqual((orch['proj'], orch['agents'], orch.get('solo'), orch['title']), ('acme-app', 5, None, 'Config loader review (acme-app v0.8)'))
        self.assertEqual((solo['proj'], solo['agents'], solo['solo'], solo['title']), ('demo-notes', 0, True, 'Fix Sunday-only date parsing test'))
        self.assertEqual([s['id'] for s in body['sessions']], [self.info['orch'], self.info['solo']])      # solo goes last
        self.assertEqual(body['default'], self.info['orch'])
        self.assertEqual([(p['name'], p['rep']) for p in body['projects']], [('acme-app', self.info['orch']), ('demo-notes', self.info['solo'])])
        claude, codex = body['sources']
        self.assertEqual((claude['sessions'], claude['agent_sessions'], claude['from'], claude['exists']), (2, 1, 'default', True))
        self.assertEqual((codex['sessions'], codex['exists']), (0, True))       # a linked Codex is not listed separately

    def test_paths_shown_are_under_the_synthetic_home(self):
        body = self.board.get('/api/sessions')[1]
        self.assertEqual([s['dir'] for s in body['sources']], ['~/.claude', '~/.codex'])
        self.assertEqual(self.board.get('/api/state')[1]['debates'][0]['short'], '~/work/acme-app/docs/review')

    def test_solo_session_opens(self):
        code, st = self.board.get('/api/state?session=' + self.info['solo'])
        self.assertEqual((code, st['agents'], st['debates'], st['session']['title']), (200, [], [], 'Fix Sunday-only date parsing test'))
        kinds = [e['kind'] for e in st['feed']]
        self.assertEqual((kinds.count('user_say'), kinds.count('orch_say')), (2, 4))
        self.assertEqual(st['orch']['model'], 'claude-sonnet-5-5')

    # ---------- debate cells and final ----------
    def test_debate_cells_and_auto_final(self):
        debates = self.state['debates']
        self.assertEqual(len(debates), 1)
        d = debates[0]
        self.assertEqual((d['title'], d['name'], d['root']), ('Config loader review (acme-app v0.8)', 'review', self.info['review']))
        topics = {t['key']: t for t in d['topics']}
        self.assertEqual(sorted(topics), ['t1_env', 't2_retry'])
        t1, t2 = topics['t1_env'], topics['t2_retry']
        self.assertEqual((t1['title'], t1['rounds'], [r['p'] for r in t1['rows']]), ('T1 Env override naming', [1, 2], ['A', 'B', 'C']))
        self.assertEqual((t2['title'], t2['rounds'], [r['p'] for r in t2['rows']]), ('T2 Retry policy', [1, 2], ['A', 'B']))
        self.assertEqual([r['role'] for r in t1['rows']], ['Compatibility', 'Ergonomics', 'Cross-check'])
        # T1 ended with rulings.md: the table lists no final output, so auto_final finds it
        f1 = t1['final']
        self.assertEqual((f1['rel'], f1['auto'], f1['exists'], f1['path']), ('rulings.md', True, True, os.path.join(self.info['units']['t1_env'], 'rulings.md')))
        self.assertTrue(all(c['state'] == 'done' for r in t1['rows'] for c in r['cells']))
        self.assertTrue(all(c['lines'] > 0 and c['agent'] for r in t1['rows'] for c in r['cells']))
        # T2 is in progress: B has no round 2 yet (without --live there is no process, so it is "missing", not "writing")
        self.assertEqual((t2['final']['rel'], t2['final']['exists']), (None, False))
        states = {r['p']: [c['state'] for c in r['cells']] for r in t2['rows']}
        self.assertEqual(states, {'A': ['done', 'done'], 'B': ['done', 'missing']})
        self.assertEqual([x['name'] for x in t1['docs']], ['brief.md', 'rulings.md'])
        self.assertEqual([x['name'] for x in t2['docs']], ['brief.md'])
        self.assertEqual(d['finals'], [])

    def test_readers_follow_the_cross_reading(self):
        t1 = next(t for t in self.state['debates'][0]['topics'] if t['key'] == 't1_env')
        r1 = {r['p']: r['cells'][0]['readers'] for r in t1['rows']}
        self.assertEqual(r1, {'A': ['C', 'T1-B'], 'B': ['C', 'T1-A'], 'C': ['T1-A', 'T1-B']})

    def test_documents_open(self):
        path = os.path.join(self.info['units']['t1_env'], 'rulings.md')
        code, body = self.board.get('/api/file?path=' + urllib.parse.quote(path))
        self.assertEqual(code, 200)
        self.assertIn('Canonical form', body['text'])
        self.assertEqual(body['short'], '~/work/acme-app/docs/review/t1_env/rulings.md')

    # ---------- agents · Codex ----------
    def test_agents(self):
        ag = self.agents()
        self.assertEqual(sorted(ag), ['C', 'T1-A', 'T1-B', 'T2-A', 'T2-B'])
        self.assertEqual({t: a['status'] for t, a in ag.items()}, {'C': 'done', 'T1-A': 'done', 'T1-B': 'done', 'T2-A': 'done', 'T2-B': 'ended'})
        self.assertEqual({t: a['provider'] for t, a in ag.items()}, {'C': 'codex', 'T1-A': 'claude', 'T1-B': 'claude', 'T2-A': 'claude', 'T2-B': 'claude'})
        self.assertEqual((ag['T1-A']['model'], ag['T1-B']['model']), ('claude-opus-5-5', 'claude-sonnet-5-5'))
        self.assertTrue(all(a['tokens']['cost'] > 0 for a in ag.values()))
        self.assertEqual(self.state['orch']['state'], 'idle')                                               # its last turn is open, but no process holds the session: nothing is working
        self.assertEqual(self.state['alerts'], [])

    def test_codex_exec_is_linked_to_the_orchestrator(self):
        cx = self.agents()['C']
        self.assertEqual((cx['id'], cx['model'], cx['origin']), (self.info['codex_thread'], 'gpt-6.1-sol', 'exec'))
        code, d = self.board.get('/api/agent?id=' + cx['id'])
        self.assertEqual(code, 200)
        self.assertEqual((d['link']['rule'], d['link']['sid'], d['link']['prompt_ok'], d['link']['cwd_ok']), ('prompt', self.info['orch'], True, True))
        self.assertLess(0, d['link']['dt'])
        self.assertLess(d['link']['dt'], 30)
        unit = self.info['units']['t1_env']
        self.assertEqual([(t['status'], t['out'], t['out_state']) for t in d['turns']],
                         [('done', os.path.join(unit, 'r1', 'C.md'), 'confirmed'), ('done', os.path.join(unit, 'r2', 'C.md'), 'confirmed')])
        self.assertEqual([w['path'] for w in d['writes']], [os.path.join(unit, 'r1', 'C.md'), os.path.join(unit, 'r2', 'C.md')])
        self.assertEqual(sorted(r['short'] for r in d['reads']), ['~/work/acme-app/docs/review/t1_env/r1/A.md', '~/work/acme-app/docs/review/t1_env/r1/B.md'])
        self.assertFalse(d['partial'])

    def test_codex_rollout_is_not_a_separate_session(self):
        ids = [s['id'] for s in self.board.get('/api/sessions')[1]['sessions']]
        self.assertNotIn(self.info['codex_thread'], ids)

    def test_codex_limit_and_tokens(self):
        lim = self.state['codex_limit']
        self.assertEqual((lim['used_percent'], lim['window_minutes'], lim['plan_type'], lim['stale'], lim['reached']), (27.0, 10080, 'pro', False, False))
        self.assertGreater(self.agents()['C']['tokens']['cost'], 0)

    # ---------- talk cards ----------
    def test_agent_talk_events(self):
        code, body = self.board.get('/api/talk?scope=agents&limit=300')
        self.assertEqual((code, body['more']), (200, False))
        kinds = {}
        for e in body['items']:
            kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
        self.assertLessEqual(set(kinds), AGENT_KINDS)
        self.assertEqual(kinds['spawn'], 5)                                  # 4 from the Agent tool + 1 Codex launched with Bash
        self.assertGreaterEqual(kinds['handback'], 9)
        self.assertEqual(kinds['orch_msg'], 5)
        self.assertEqual(kinds['agent_msg'], 3)
        self.assertGreaterEqual(kinds['xread'], 6)
        ag = self.agents()
        spawns = {e['to'] for e in body['items'] if e['kind'] == 'spawn'}
        self.assertEqual(spawns, {a['id'] for a in ag.values()})              # the receiver was resolved to the agent id, not the tool_use_id
        msg = next(e for e in body['items'] if e['kind'] == 'agent_msg' and e['title'] == 'Heads-up on flat names')
        self.assertEqual((msg['from'], msg['peer']), (ag['T1-A']['id'], ag['T1-B']['id']))
        to_orch = next(e for e in body['items'] if e['kind'] == 'agent_msg' and e['from'] == ag['T2-B']['id'])
        self.assertIsNone(to_orch['peer'])
        self.assertTrue(any(e['kind'] == 'xread' and e['from'] == ag['C']['id'] for e in body['items']))   # a read of a report that Codex wrote
        self.assertTrue(any(e['kind'] == 'xread' and e['to'] == ag['C']['id'] for e in body['items']))     # a read done by Codex

    def test_user_talk_events(self):
        code, body = self.board.get('/api/talk?limit=100')
        self.assertEqual(code, 200)
        self.assertLessEqual({e['kind'] for e in body['items']}, USER_KINDS)
        self.assertEqual([e['kind'] for e in body['items']].count('user_say'), 2)
        ask = next(e for e in body['items'] if e['kind'] == 'orch_ask')
        self.assertIn('How many rounds', ask['text'])
        self.assertIn('Two rounds', next(e for e in body['items'] if e['kind'] == 'user_answer')['text'])

    def test_events_have_times_in_the_last_two_hours(self):
        now = time.time()
        for e in self.state['feed']:
            self.assertTrue(now - 125 * 60 < e['ts'] < now + 5, e)

    # ---------- plans ----------
    def test_plan_cache(self):
        code, p = self.board.get('/api/plans', session=False)
        self.assertEqual(code, 200)
        c = p['claude']
        self.assertEqual((c['plan'], c['source'], c['hits']), ('Max 5x', 'cache', {}))
        self.assertEqual(set(c) & {'usage_api', 'error', 'error_info'}, set())
        self.assertEqual((c['five_hour']['percent'], c['seven_day']['percent']), (34.0, 52.0))
        self.assertEqual([(x['name'], x['percent']) for x in c['scoped']], [('Sonnet', 18.0)])
        self.assertLess(abs(c['as_of'] - (self.info['now'] - 240)), 1)
        for w in (c['five_hour'], c['seven_day']):
            self.assertGreater(w['resets_at'], p['now'])
        self.assertEqual((p['codex']['used_percent'], p['codex']['plan_type']), (27.0, 'pro'))
        self.assertNotIn('stripe', json.dumps(p['codex']))
        self.assertNotIn('email', json.dumps(p))

    # ---------- default mode: no credential file is opened and nothing goes out ----------
    def test_default_mode_opens_no_credentials_and_makes_no_requests(self):
        for path in ('/api/sessions', '/api/plans', '/api/state', '/api/talk?scope=agents', '/api/timeline', '/api/agent?id=' + self.info['codex_thread']):
            self.assertEqual(self.board.get(path)[0], 200, path)
        for p in self.decoys:                                                  # a file shaped like a credential file is not opened through /api/file either
            self.assertEqual(self.board.get('/api/file?path=' + urllib.parse.quote(p))[0], 403, p)
        self.assertNotIn('사용량 조회', self.board.text())
        self.assertIn('프로세스 판정', self.board.text())
        log = read(self.log) if os.path.exists(self.log) else ''
        self.assertEqual(log, '', '감시에 걸린 것: ' + log)
        for p, text in self.decoys.items():                                    # the decoys are as they were (neither read nor written)
            self.assertEqual(read(p), text)
            self.assertEqual(os.stat(p).st_mtime, 1000000000)
        every = self.board.text() + json.dumps(self.state) + json.dumps(self.board.get('/api/plans', session=False)[1])
        self.assertNotIn(FAKE_TOKEN, every)

    def test_guard_would_catch_a_credential_read(self):
        """Check that the watch really triggers: in a separate process under the same watch (the first part of GUARD), open a decoy and try to go outside."""
        decoy = next(iter(self.decoys))
        probe = GUARD.split("sys.argv = [os.path.join(root, 'server.py')]")[0] + (
            "open(%r).read()\n"
            "try:\n    socket.create_connection(('example.invalid', 80), timeout=1)\nexcept OSError:\n    pass\n"
            "try:\n    urllib.request.urlopen('http://example.invalid/')\nexcept OSError:\n    pass\n" % decoy)
        log = os.path.join(self.tmp.name, 'probe.log')
        subprocess.run([sys.executable, '-c', probe, ROOT, log, decoy], stderr=subprocess.DEVNULL, timeout=30, check=True)
        text = read(log)
        for word in ('open ', 'getaddrinfo example.invalid', 'urlopen'):
            self.assertIn(word, text)


class LiveFlag(unittest.TestCase):
    """--live: with a fake process named `claude` and ~/.claude/sessions/<pid>.json present, a session reads as "working". --stop turns it off."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')
        self.addCleanup(synth_home.stop_live, self.home, True)
        self.pids = []

    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'synth_home.py'), self.home] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=60, universal_newlines=True)

    def test_live_sessions_read_as_working_and_stop_ends_them(self):
        r = self.cli('--live')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('--stop', r.stdout)                                      # it prints how to turn it off
        self.assertIn('kill ', r.stdout)
        sdir = os.path.join(self.home, '.claude', 'sessions')
        files = sorted(os.listdir(sdir))
        self.assertEqual(len(files), 2)
        for f in files:
            d = json.loads(read(os.path.join(sdir, f)))
            self.pids.append(d['pid'])
            self.assertEqual(f, '%d.json' % d['pid'])
            self.assertEqual(synth_home.fake_cmdline(d['pid']), 'claude %d' % synth_home.LIVE_SECONDS)
        board = Board(self.home, synth_home.ORCH)
        self.addCleanup(board.close)
        st = board.get('/api/state')[1]
        self.assertEqual((st['session']['alive'], st['session']['pid'] in self.pids, st['orch']['state']), (True, True, 'working'))
        ag = {a['tag']: a['status'] for a in st['agents']}
        self.assertEqual(ag, {'C': 'done', 'T1-A': 'done', 'T1-B': 'done', 'T2-A': 'done', 'T2-B': 'running'})
        t2 = next(t for t in st['debates'][0]['topics'] if t['key'] == 't2_retry')
        self.assertEqual({r['p']: [c['state'] for c in r['cells']] for r in t2['rows']}, {'A': ['done', 'done'], 'B': ['done', 'writing']})
        self.assertEqual(next(a for a in st['agents'] if a['tag'] == 'T2-B')['pending']['name'], 'Read')
        solo = board.get('/api/state?session=' + synth_home.SOLO)[1]
        self.assertTrue(solo['session']['alive'])
        r = self.cli('--stop')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('stopped 2', r.stdout)
        self.assertNotIn('left', r.stdout)
        self.assertEqual(os.listdir(sdir), [])
        deadline = time.time() + 10
        while time.time() < deadline and any(synth_home.fake_cmdline(p) == 'claude %d' % synth_home.LIVE_SECONDS for p in self.pids):
            time.sleep(0.1)
        self.assertFalse(any(synth_home.fake_cmdline(p) == 'claude %d' % synth_home.LIVE_SECONDS for p in self.pids))
        self.assertFalse(board.get('/api/state')[1]['session']['alive'])

    def test_stop_only_touches_what_this_tool_wrote(self):
        """--stop: deletes only `<pid>.json` files that carry the marker. Stops only processes whose command line is `claude <seconds>`. Other people's registration files stay."""
        synth_home.build(self.home)
        sdir = os.path.join(self.home, '.claude', 'sessions')
        sleepers = [subprocess.Popen(['sleep', '30']) for _ in range(3)]
        for sl in sleepers:
            self.addCleanup(sl.wait)
            self.addCleanup(sl.kill)
        real, reused, odd = sleepers
        put = lambda name, d: synth_home.put(os.path.join(sdir, name), json.dumps(d))
        put('%d.json' % real.pid, {'pid': real.pid, 'sessionId': synth_home.ORCH, 'name': 'a real session'})            # no marker = someone else's
        put('%d.json' % reused.pid, {'pid': reused.pid, 'sessionId': synth_home.ORCH, 'synthHome': True})              # ours, but another process has taken the pid
        put('notes.json', {'pid': odd.pid, 'synthHome': True})                                                         # the name is not `<pid>.json`
        put('%d.json' % (odd.pid + 1), {'pid': odd.pid, 'synthHome': True})                                            # the name and the pid differ
        self.assertEqual(synth_home.stop_live(self.home, quiet=True), [])
        self.assertEqual([sl.poll() for sl in sleepers], [None, None, None])                                           # a process that is not `claude <seconds>` gets no signal
        self.assertEqual(sorted(os.listdir(sdir)), sorted(['%d.json' % real.pid, 'notes.json', '%d.json' % (odd.pid + 1)]))
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'synth_home.py'), self.home, '--stop'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=60, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('stopped 0', r.stdout)
        self.assertIn('left 3 file(s) this tool did not write', r.stdout)


class StopGuards(unittest.TestCase):
    """--stop and writing do not touch the real HOME or a folder this tool did not make."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)

    def registration(self, home, mark=True, name='4242.json'):
        """A file that looks like a real Claude Code registration file (it even carries our marker: if the folder is not ours it still must not be deleted)."""
        path = synth_home.put(os.path.join(home, '.claude', 'sessions', name), json.dumps({'pid': 4242, 'sessionId': synth_home.ORCH, 'synthHome': True}))
        if mark:
            synth_home.put(os.path.join(home, synth_home.MARK), synth_home.MARK_TEXT)
        return path

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'synth_home.py')] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', **env), timeout=60, universal_newlines=True)

    def test_folder_without_our_marker(self):
        home = os.path.join(self.root, 'plain')
        reg = self.registration(home, mark=False)
        r = self.cli(home, '--stop')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('not a synthetic HOME', r.stdout)
        self.assertEqual(synth_home.stop_live(home, quiet=True), [])             # the library call does nothing either
        self.assertTrue(os.path.exists(reg))

    def test_marker_must_hold_our_text(self):
        home = os.path.join(self.root, 'other')
        reg = self.registration(home, mark=False)
        synth_home.put(os.path.join(home, synth_home.MARK), 'not ours\n')
        self.assertNotEqual(self.cli(home, '--stop').returncode, 0)
        self.assertEqual(synth_home.stop_live(home, quiet=True), [])
        self.assertTrue(os.path.exists(reg))
        with self.assertRaises(SystemExit):                                       # writing as well: someone else's folder with content in it
            synth_home.build(home)
        self.assertTrue(os.path.exists(reg))

    def test_real_home_even_with_a_marker(self):
        """In a process whose HOME is this folder, pointing at this folder means the real HOME: refused even with a marker."""
        home = os.path.join(self.root, 'realhome')
        reg = self.registration(home)
        r = self.cli(home, '--stop', HOME=home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('real HOME', r.stdout)
        self.assertTrue(os.path.exists(reg))
        with mock.patch.dict(os.environ, {'HOME': home}):
            self.assertEqual(synth_home.stop_live(home, quiet=True), [])
            self.assertTrue(os.path.exists(reg))
            with self.assertRaises(SystemExit):
                synth_home.build(home)

    def test_empty_real_home_is_not_written(self):
        home = os.path.join(self.root, 'emptyhome')
        os.makedirs(home)
        r = self.cli(home, HOME=home)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('real HOME', r.stdout)
        self.assertEqual(os.listdir(home), [])

    def test_symlinked_sessions_folder_is_not_followed(self):
        """Even if `.claude` inside our folder is a link to somewhere else (such as the real ~/.claude), what is inside it is not deleted."""
        elsewhere = os.path.join(self.root, 'elsewhere')
        reg = self.registration(elsewhere, mark=False)
        home = os.path.join(self.root, 'ours')
        os.makedirs(home)
        synth_home.put(os.path.join(home, synth_home.MARK), synth_home.MARK_TEXT)
        os.symlink(os.path.join(elsewhere, '.claude'), os.path.join(home, '.claude'))
        self.assertEqual(synth_home.stop_live(home, quiet=True), [])
        self.assertTrue(os.path.exists(reg))
        synth_home.build(home)                                                    # on a rebuild too, only the link is removed and what it points to stays
        self.assertTrue(os.path.exists(reg))
        self.assertFalse(os.path.islink(os.path.join(home, '.claude')))


class Generator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')

    def test_times_follow_now_and_the_run_is_repeatable(self):
        now = 1_800_000_000.0
        synth_home.build(self.home, now=now)
        first = tree_digest(self.home)
        stamps = []
        for d, _, files in os.walk(self.home):
            for f in files:
                if f.endswith('.jsonl') or f == 'session_index.jsonl':
                    stamps += re.findall(r'"(?:timestamp|updated_at)":"(\d{4}-\d\d-\d\dT[\d:.]+Z)"', read(os.path.join(d, f)))
        self.assertGreater(len(stamps), 150)
        secs = [calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')) for s in stamps]
        self.assertTrue(all(now - 125 * 60 <= t <= now + 1 for t in secs), (min(secs) - now, max(secs) - now))
        synth_home.build(self.home, now=now)                                    # again in the same folder: identical down to the bytes
        self.assertEqual(tree_digest(self.home), first)
        synth_home.build(self.home, now=now + 1000)                             # only the times shift
        self.assertNotEqual(tree_digest(self.home), first)
        strip = lambda names: [re.sub(r'rollout-[\dT-]+-', 'rollout-', n) for n, _ in names]
        self.assertEqual(strip(tree_digest(self.home)), strip(first))

    def test_no_real_name_or_path_in_any_file(self):
        synth_home.build(self.home)
        real = os.path.realpath(os.path.expanduser('~'))
        user = getpass.getuser()
        for name, data in tree_digest(self.home):
            if real not in ('/', '') and not self.home.startswith(real + os.sep):
                self.assertNotIn(real.encode(), data, name)
            if len(user) > 3 and '/%s/' % user not in self.home:
                self.assertNotIn(('/%s/' % user).encode(), data, name)
        blob = b''.join(d for _, d in tree_digest(self.home))
        for word in (b'@', b'password', b'secret'):
            self.assertNotIn(word, blob, word)
        claude_json = json.loads(read(os.path.join(self.home, '.claude.json')))
        self.assertEqual(sorted(claude_json['oauthAccount']), ['billingType', 'userRateLimitTier'])     # no account id or email fields
        for d in ('.claude', '.codex'):                                          # no credential file is created
            names = [f for _, _, fs in os.walk(os.path.join(self.home, d)) for f in fs]
            self.assertNotIn('.credentials.json', names)
            self.assertNotIn('auth.json', names)

    def test_every_jsonl_line_is_compact_json(self):
        synth_home.build(self.home)
        n = 0
        for d, _, files in os.walk(self.home):
            for f in files:
                if f.endswith('.jsonl'):
                    for ln in read(os.path.join(d, f)).splitlines():
                        self.assertEqual(json.dumps(json.loads(ln), separators=(',', ':'), ensure_ascii=False), ln)
                        n += 1
        self.assertGreater(n, 200)

    def test_refuses_to_write_into_a_folder_it_did_not_make(self):
        other = os.path.join(os.path.dirname(self.home), 'other')
        os.makedirs(other)
        with open(os.path.join(other, 'keep.txt'), 'w') as f:
            f.write('mine')
        with self.assertRaises(SystemExit):
            synth_home.build(other)
        self.assertEqual(os.listdir(other), ['keep.txt'])                       # nothing was deleted or written
        synth_home.build(self.home)                                             # a folder it made itself is written again
        with open(os.path.join(self.home, 'work', 'extra.txt'), 'w') as f:
            f.write('x')
        synth_home.build(self.home)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'work', 'extra.txt')))

    def test_refuses_the_real_home_and_odd_paths(self):
        with self.assertRaises(SystemExit):
            synth_home.build(os.path.expanduser('~'))
        for bad in ('/tmp/has space/x', '/tmp/.hidden/x', '/tmp/a:b'):
            with self.assertRaises(SystemExit):
                synth_home.check_folder(bad)

    def test_cli_prints_how_to_run_the_board(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'synth_home.py'), self.home], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=60, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('HOME=%s python3 server.py' % self.home, r.stdout)
        self.assertNotIn('kill', r.stdout)                                      # without --live no process is created
        self.assertFalse(os.path.exists(os.path.join(self.home, '.claude', 'sessions')))


class StoppedScene(unittest.TestCase):
    """--stopped: work that stopped and nests, on the small scene and on the busy one. The scene is what the screen checks (state_checks.js) and the pictures need: a draft that nobody is
    typing, a paused cell, a tool summary the board words itself, and nothing real in any file."""

    def build(self, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.info = synth_home.build(os.path.join(os.path.realpath(self.tmp.name), 'home'), **kw)
        board = Board(self.info['home'], self.info['orch'])
        self.addCleanup(board.close)
        return board, board.get('/api/state')[1]

    def check_scene(self, state, paused, held):
        agents = {a['tag']: a for a in state['agents'] if a['tag']}
        self.assertEqual((self.info['stopped']['paused'], self.info['stopped']['held']), (paused, held))
        self.assertEqual((agents[paused]['status'], agents[paused]['reason']), ('interrupted', 'api_error'))
        self.assertEqual((agents[held]['status'], agents[held]['reason']), ('interrupted', 'api_error'))
        cells = {c['agent']: c for d in state['debates'] for tp in d['topics'] for r in tp['rows'] for c in r['cells'] if c['state'] in ('paused', 'draft') and c['agent']}
        self.assertEqual(cells[agents[paused]['id']]['state'], 'paused')                  # stopped before any file: nothing to show
        self.assertEqual(cells[agents[held]['id']]['state'], 'draft')                      # its report is on disk, and nobody is writing it
        return agents

    def test_a_draft_nobody_is_typing_and_a_paused_cell_on_the_small_scene(self):
        _, state = self.build(stopped=True)
        self.check_scene(state, 'T2-B', 'T2-A')

    def test_the_busy_scene_has_both_too(self):
        _, state = self.build(busy=True, stopped=True)
        self.check_scene(state, 'T5-B', 'T5-C')

    def test_a_tool_summary_the_board_words_itself(self):
        _, state = self.build(stopped=True)
        mine = [a for a in state['agents'] if (a.get('last_tool') or {}).get('name') == 'SubagentHandback']
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]['last_tool']['text_i18n'], {'key': 'event.tool.handback', 'params': {}})

    def test_the_options_say_what_they_do(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'synth_home.py'), '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--stopped', r.stdout)
        self.assertNotRegex(r.stdout + read(os.path.join(ROOT, 'tools', 'synth_home.py')), r'--robust|robust_|Robust|robust PLAN')

    def test_nothing_real_in_any_file_and_the_scene_is_repeatable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.join(os.path.realpath(tmp.name), 'home')
        synth_home.build(home, now=1_800_000_000.0, stopped=True)
        first = tree_digest(home)
        synth_home.build(home, now=1_800_000_000.0, stopped=True)
        self.assertEqual(tree_digest(home), first)
        real, user = os.path.realpath(os.path.expanduser('~')), getpass.getuser()
        for name, data in first:
            if real not in ('/', '') and not home.startswith(real + os.sep):
                self.assertNotIn(real.encode(), data, name)
            if len(user) > 3 and '/%s/' % user not in home:
                self.assertNotIn(('/%s/' % user).encode(), data, name)


class BusyBoard(unittest.TestCase):
    """--busy (a big scene, for README screenshots): acme-robot, 8 debate topics (6 finished, 2 in progress), 33 agents (2 of them Codex). Without --live every unfinished agent is `ended`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.info = synth_home.build(os.path.join(cls.tmp.name, 'home'), busy=True)
        cls.board = Board(cls.info['home'], cls.info['orch'])
        cls.state = cls.board.get('/api/state')[1]

    @classmethod
    def tearDownClass(cls):
        cls.board.close()
        cls.tmp.cleanup()

    def topics(self):
        return {t['key']: t for t in self.state['debates'][0]['topics']}

    def codex_unknown(self):
        return codex_unknown_here(os.path.join(self.info['home'], '.codex', 'sessions'))

    def test_sessions(self):
        code, body = self.board.get('/api/sessions')
        by_id = {x['id']: x for x in body['sessions']}
        orch, solo = by_id[self.info['orch']], by_id[self.info['solo']]
        self.assertEqual((orch['proj'], orch['agents'], orch.get('solo'), orch['title']), ('acme-robot', 33, None, 'acme-robot v2 migration'))
        self.assertEqual((solo['proj'], solo['agents'], solo['solo']), ('demo-notes', 0, True))
        self.assertEqual(body['default'], self.info['orch'])
        self.assertEqual((body['sources'][0]['sessions'], body['sources'][0]['agent_sessions'], body['sources'][1]['sessions']), (2, 1, 0))     # both Codex agents are linked to the orchestrator

    def test_agent_counts_and_kinds(self):
        ag = self.state['agents']
        self.assertEqual(len(ag), 33)
        self.assertEqual({p: sum(a['provider'] == p for a in ag) for p in ('claude', 'codex')}, {'claude': 31, 'codex': 2})
        unknown = 2 if self.codex_unknown() else 0                                                         # the two Codex agents, on a machine that cannot see open files and has a codex process
        self.assertEqual(sum(a['status'] == 'done' for a in ag), 19)                                      # 6 finished topics x 3 + T5-C
        self.assertEqual(sum(a['status'] == 'ended' for a in ag), 14 - unknown)                            # there is no process, so the 14 unfinished agents are ended
        self.assertEqual(sum(a['status'] == 'unknown' for a in ag), unknown)
        workers = [a for a in ag if not a['units']]
        self.assertEqual(len(workers), 9)                                                                  # 9 desks in the "Other work" room
        self.assertEqual(sorted(re.sub(r'-\d+$', '', a['tag']) for a in workers), ['opus5.5'] + ['sol6.1'] + ['sonnet5.5'] * 7)
        self.assertEqual(sum(a['model'].startswith('claude-opus') for a in workers), 1)
        self.assertEqual(sum(a['provider'] == 'codex' for a in workers), 1)
        self.assertEqual(self.state['orch']['state'], 'idle')                                               # its last turn is open, but no process holds the session: nothing is working

    def test_topics_and_finals(self):
        t = self.topics()
        self.assertEqual(sorted(t), ['t1_naming', 't2_errors', 't3_retry', 't4_config', 't5_logging', 't6_tests', 't7_packages', 't8_release'])
        have = {k: (v['final']['exists'], v['final']['auto'], v['final']['rel']) for k, v in t.items()}
        self.assertEqual({k: v for k, v in have.items() if v[0]}, {
            't1_naming': (True, False, 'final/naming.md'), 't4_config': (True, False, 'final/config.md'), 't8_release': (True, False, 'final/release.md'),   # final output from the brief table
            't2_errors': (True, True, 'rulings.md'), 't6_tests': (True, True, 'rulings.md'), 't7_packages': (True, True, 'rulings.md')})                 # found automatically
        self.assertFalse(t['t3_retry']['final']['exists'] or t['t5_logging']['final']['exists'])
        self.assertEqual([f['name'] for f in self.state['debates'][0]['finals']], ['config.md', 'naming.md', 'release.md'])
        self.assertTrue(all(len(v['rows']) == 3 for v in t.values()))
        cells = lambda k: {r['p']: [c['state'] for c in r['cells']] for r in t[k]['rows']}
        self.assertEqual(cells('t1_naming'), {'A': ['done', 'done'], 'B': ['done', 'done'], 'C': ['done', 'done']})
        self.assertEqual(cells('t3_retry'), {'A': ['done', 'missing'], 'B': ['done', 'missing'], 'C': ['done', 'writing' if self.codex_unknown() else 'missing']})     # round 2 is not there yet (no --live, so "missing"; C is the Codex agent, whose process nobody can rule out)
        self.assertEqual(cells('t5_logging'), {'A': ['missing'], 'B': ['missing'], 'C': ['done']})            # only round 1

    def test_recent_topic_keeps_its_room_and_old_ones_do_not(self):
        """The room of a finished topic disappears 20 minutes after its last activity (counted with the past agents). T6 finished 12 minutes ago."""
        now = self.state['now']
        last = lambda k: max(c['mtime'] or 0 for r in self.topics()[k]['rows'] for c in r['cells'])
        self.assertLess(now - last('t6_tests'), 1200)
        for k in ('t1_naming', 't2_errors', 't4_config', 't7_packages', 't8_release'):
            self.assertGreater(now - last(k), 1200, k)

    def test_both_codex_threads_are_linked(self):
        cx = [a for a in self.state['agents'] if a['provider'] == 'codex']
        self.assertEqual(sorted(a['id'] for a in cx), sorted([synth_home.BUSY_CX_DEBATE, synth_home.BUSY_CX_WORKER]))
        d = self.board.get('/api/agent?id=' + synth_home.BUSY_CX_DEBATE)[1]
        unit = self.info['units']['t3_retry']
        self.assertEqual(d['link']['rule'], 'prompt')
        self.assertEqual([(t['status'] if t['end'] else 'open', t['out_state']) for t in
                          [dict(t, end=t['end']) for t in d['turns']]], [('done', 'confirmed'), ('open', 'planned')])
        self.assertEqual([t['out'] for t in d['turns']], [os.path.join(unit, 'r1', 'C.md'), os.path.join(unit, 'r2', 'C.md')])
        w = self.board.get('/api/agent?id=' + synth_home.BUSY_CX_WORKER)[1]
        self.assertEqual((w['link']['rule'], [t['out'] for t in w['turns']], [bool(t['end']) for t in w['turns']]), ('prompt', [None, None], [True, False]))

    def test_exactly_one_alert_from_a_report(self):
        al = self.state['alerts']
        self.assertEqual([(a['level'], a['id'].split(':')[0]) for a in al], [('check', 'hb')])
        self.assertIn('plan_route', al[0]['text'])
        self.assertEqual(al[0]['agent'], self.state['names'] and next(a['id'] for a in self.state['agents'] if a['tag'] == 'T7-B'))

    def test_agent_talk_is_busy(self):
        body = self.board.get('/api/talk?scope=agents&limit=300')[1]
        kinds = {}
        for e in body['items']:
            kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
        self.assertLessEqual(set(kinds), AGENT_KINDS)
        self.assertEqual(kinds['spawn'], 33)
        self.assertGreaterEqual(kinds['handback'], 40)
        self.assertGreaterEqual(kinds['xread'], 40)
        cx = synth_home.BUSY_CX_WORKER
        self.assertTrue(any(e['kind'] == 'handback' and e['from'] == cx for e in body['items']))          # ◆Codex's final report
        self.assertTrue(any(e['kind'] == 'orch_msg' and e['to'] == cx for e in body['items']))
        user = self.board.get('/api/talk?limit=100')[1]['items']
        self.assertEqual([e['kind'] for e in user].count('user_say'), 3)
        self.assertEqual(user[-1]['kind'], 'orch_say')
        self.assertNotIn('?', user[-1]['text'])

    def test_costs_and_plan(self):
        total = self.state['orch']['tokens']['cost'] + sum(a['tokens']['cost'] for a in self.state['agents'])
        self.assertTrue(10 < total < 100, total)
        c = self.board.get('/api/plans', session=False)[1]['claude']
        self.assertEqual((c['plan'], c['five_hour']['percent'], c['seven_day']['percent'], c['source']), ('Max 20x', 47.0, 61.0, 'cache'))

    def test_every_time_is_relative_to_now(self):
        stamps = re.findall(r'"(?:timestamp|updated_at)":"(\d{4}-\d\d-\d\dT[\d:.]+Z)"', ''.join(
            read(os.path.join(d, f)) for d, _, fs in os.walk(self.info['home']) for f in fs if f.endswith('.jsonl')))
        secs = [calendar.timegm(time.strptime(x[:19], '%Y-%m-%dT%H:%M:%S')) for x in stamps]
        self.assertGreater(len(secs), 900)
        self.assertTrue(self.info['now'] - 301 * 60 <= min(secs) and max(secs) <= self.info['now'] + 1)


class BusyLive(unittest.TestCase):
    """--busy --live: the 14 unfinished agents (9 workers + 3 in T3 + 2 in T5) read as working. A Codex agent is working only when a fake `codex` process has its rollout open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')
        self.addCleanup(synth_home.stop_live, self.home, True)

    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'synth_home.py'), self.home] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=60, universal_newlines=True)

    def test_working_and_stop_ends_every_fake_process(self):
        r = self.cli('--busy', '--live')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('`codex`', r.stdout)
        self.assertIn('--stop', r.stdout)
        live = json.loads(read(os.path.join(self.home, synth_home.LIVE_FILE)))
        pids = [q['pid'] for q in live['procs']] + [json.loads(read(os.path.join(self.home, '.claude', 'sessions', f)))['pid']
                                                    for f in os.listdir(os.path.join(self.home, '.claude', 'sessions'))]
        self.assertEqual((len(live['procs']), len(pids)), (2, 4))
        self.assertEqual(sorted(synth_home.fake_cmdline(p) for p in pids), ['claude %d' % synth_home.LIVE_SECONDS] * 2 + ['codex %d' % synth_home.LIVE_SECONDS] * 2)
        board = Board(self.home, synth_home.BUSY_ORCH)
        self.addCleanup(board.close)
        st = board.get('/api/state')[1]
        self.assertEqual((st['session']['alive'], st['orch']['state']), (True, 'working'))
        ag = st['agents']
        unknown = synth_home_codex_unknown(self.home)                    # no /proc (macOS): ps cannot say which rollout the fake codex process holds open
        self.assertEqual(sum(a['status'] == 'running' for a in ag), 14 - 2 * unknown)
        self.assertEqual(sum(a['status'] == 'done' for a in ag), 19)
        self.assertEqual(sum(a['status'] in ('stalled', 'ended', 'failed', 'killed') for a in ag), 0)
        self.assertEqual({a['status'] for a in ag if a['provider'] == 'codex'}, {'unknown' if unknown else 'running'})     # an open turn + a process that has the rollout open (/proc)
        self.assertEqual(sum(a['status'] == 'running' for a in ag if not a['units']), 9 - unknown)         # 9 agents in the "Other work" room, one of them Codex
        states = [c['state'] for t in st['debates'][0]['topics'] for r in t['rows'] for c in r['cells']]
        self.assertEqual((states.count('writing'), states.count('draft')), (5, 0))                          # T3 round 2 has 3 cells (Codex's is the cell planned with -o) + T5 round 1 has 2 cells
        self.assertEqual([a['level'] for a in st['alerts']], ['check'])
        r = self.cli('--stop')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('stopped 4', r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, synth_home.LIVE_FILE)))
        deadline = time.time() + 10
        while time.time() < deadline and any((synth_home.fake_cmdline(p) or '') in ('claude %d' % synth_home.LIVE_SECONDS, 'codex %d' % synth_home.LIVE_SECONDS) for p in pids):
            time.sleep(0.1)
        self.assertEqual([p for p in pids if (synth_home.fake_cmdline(p) or '') in ('claude %d' % synth_home.LIVE_SECONDS, 'codex %d' % synth_home.LIVE_SECONDS)], [])

    def test_stop_does_not_trust_a_live_file_it_did_not_write(self):
        synth_home.build(self.home, busy=True)
        sleeper = subprocess.Popen(['sleep', '30'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        lf = os.path.join(self.home, synth_home.LIVE_FILE)
        synth_home.put(lf, json.dumps({'procs': [{'pid': sleeper.pid, 'cmd': 'codex %d' % synth_home.LIVE_SECONDS}]}))      # no marker
        self.assertEqual(synth_home.stop_live(self.home, quiet=True), [])
        self.assertTrue(os.path.exists(lf))
        synth_home.put(lf, json.dumps({'synthHome': True, 'procs': [{'pid': sleeper.pid, 'cmd': 'codex %d' % synth_home.LIVE_SECONDS}]}))    # has the marker but the pid belongs to another process
        self.assertEqual(synth_home.stop_live(self.home, quiet=True), [])
        self.assertIsNone(sleeper.poll())
        self.assertFalse(os.path.exists(lf))


class BusyGenerator(unittest.TestCase):
    def test_default_scene_is_not_changed_by_the_busy_option_and_busy_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(os.path.realpath(tmp), 'home')
            synth_home.build(home, now=1_800_000_000.0)
            small = tree_digest(home)
            synth_home.build(home, now=1_800_000_000.0, busy=True)
            big = tree_digest(home)
            self.assertGreater(len(big), 4 * len(small))
            synth_home.build(home, now=1_800_000_000.0, busy=True)
            self.assertEqual(tree_digest(home), big)                                  # with the same now: identical down to the bytes
            synth_home.build(home, now=1_800_000_000.0)                               # a small scene over a big scene: no file of the big scene is left
            self.assertEqual(tree_digest(home), small)
            self.assertFalse(os.path.exists(os.path.join(home, 'work', 'acme-robot')))

    def test_busy_has_no_real_name_or_path_and_only_generic_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(os.path.realpath(tmp), 'home')
            synth_home.build(home, busy=True)
            real = os.path.realpath(os.path.expanduser('~'))
            user = getpass.getuser()
            blob = b''
            for name, data in tree_digest(home):
                blob += data
                if real not in ('/', '') and not home.startswith(real + os.sep):
                    self.assertNotIn(real.encode(), data, name)
                if len(user) > 3 and '/%s/' % user not in home:
                    self.assertNotIn(('/%s/' % user).encode(), data, name)
            for word in (b'@', b'password', b'secret', b'/Dev/', b'.claude/projects', b'netbird'):          # no address, no credential word, no folder of a real workspace or HOME
                self.assertNotIn(word, blob, word)
            names = set(re.findall(rb'work/([A-Za-z0-9_-]+)/', blob))
            self.assertEqual(names, {b'acme-robot', b'demo-notes'})


class LinkScene(unittest.TestCase):
    """--links: `claude -p` children next to the orchestrator's Bash calls (each reads its instruction from a file the records do not show, so the words cannot be told). Two are linked only by a guess (rule time, certain false), three could not be linked
    (unlinked: ambiguous, ended_before_seen, and no_matching_call while a fake process of the child is still running). The UI shows these (guess mark, "not linked" box)."""
    C = synth_home.LINK_CHILDREN

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.info = synth_home.build(os.path.join(cls.tmp.name, 'home'), busy=True, links=True)
        cls.pids, cls.procs = start_live(cls.info)
        cls.board = Board(cls.info['home'], cls.info['orch'])
        cls.state = cls.board.get('/api/state')[1]

    @classmethod
    def tearDownClass(cls):
        cls.board.close()
        synth_home.stop_live(cls.info['home'], True)
        reap(cls.procs)
        cls.tmp.cleanup()

    def test_children_are_linked_by_a_guess_and_everything_else_stays_certain(self):
        ag = self.state['agents']
        kids = [a for a in ag if a['origin'] == 'cli']
        self.assertEqual([(a['link']['rule'], a['link']['certain']) for a in kids], [('time', False)] * 2)
        self.assertEqual(sorted(a['id'][:8] for a in kids), [self.C[0][:8], self.C[1][:8]])
        self.assertEqual(len(ag), 35)                                                          # 33 of the busy scene + the two children
        self.assertTrue(all(a['link']['certain'] for a in ag if a['origin'] != 'cli'))

    def test_unlinked_has_the_three_reasons_newest_first(self):
        u = self.state['unlinked']
        self.assertEqual([(x['id'], x['reason']) for x in u], [(self.C[4], 'no_matching_call'), (self.C[3], 'ended_before_seen'), (self.C[2], 'ambiguous')])
        self.assertTrue(all(x['provider'] == 'claude' and x['cwd'] == '~/work/acme-robot' for x in u))
        self.assertEqual([x['started'] for x in u], sorted((x['started'] for x in u), reverse=True))
        self.assertTrue(all(0 < self.state['now'] - x['started'] < 600 for x in u))

    def test_the_judgment_is_the_same_once_the_later_stage_has_run(self):
        """The server answers its first /api/state before the later stage of the link index has run (it starts after that picture), and that stage must not change what the
        scene means: read in this process with the later stage inline, the children are linked by a guess or left unlinked for the same reasons."""
        sys.path.insert(0, os.path.join(ROOT, 'tests'))
        from compat import patched, server
        from board import link, procs
        home = self.info['home']
        idx = server.LinkIndex()                                                               # the later stage runs inside scan()
        with patched(HOME=home, CLAUDE_HOME=os.path.join(home, '.claude'), PROJECTS=os.path.join(home, '.claude', 'projects'), CODEX_HOME=os.path.join(home, '.codex'),
                     CODEX_SESSIONS=os.path.join(home, '.codex', 'sessions'), CODEX_NAMES=os.path.join(home, '.codex', 'session_index.jsonl'), CODEX=server.CodexIndex(), LINKS=idx):
            procs.reset()                                                                      # without /proc (macOS) the process table is cached for 3 seconds, and an earlier test may have left one that lacks the fake child
            self.addCleanup(procs.reset)
            idx.scan()
            self.assertTrue(idx.deep_ready.is_set())
            self.assertEqual({k: (o['sid'], o['rule'], link.certain(o['rule'])) for k, o in idx.cli_owners.items()},
                             {self.C[0]: (self.info['orch'], 'time', False), self.C[1]: (self.info['orch'], 'time', False)})
            self.assertEqual([(x['id'], x['reason']) for x in idx.unlinked_for(self.info['orch'])],
                             [(self.C[4], 'no_matching_call'), (self.C[3], 'ended_before_seen'), (self.C[2], 'ambiguous')])
        self.assertEqual([(x['id'], x['reason']) for x in self.state['unlinked']], [(self.C[4], 'no_matching_call'), (self.C[3], 'ended_before_seen'), (self.C[2], 'ambiguous')])

    def test_the_scene_is_only_there_with_the_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(os.path.realpath(tmp), 'home')
            info = synth_home.build(home, busy=True)
            self.assertNotIn('links', info)
            kids = [os.path.join(d, f) for d, _, fs in os.walk(home) for f in fs if f.startswith('c11d')]
            self.assertEqual(kids, [])


if __name__ == '__main__':
    unittest.main()
