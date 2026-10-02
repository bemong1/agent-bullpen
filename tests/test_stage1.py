"""Tests of bug and security fixes: session id check, limits on viewing documents (/api/file), numeric arguments, Host check, debate roots, concurrent requests, etc.

    python3 -m unittest discover -s tests
"""
import argparse
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server, start_patches, terminal_lang  # noqa: E402


class Item1SessionId(unittest.TestCase):
    """A session id must be UUID-shaped. A value with path or glob characters looks up no file and gives None."""

    def test_get_rejects(self):
        for bad in ('../x', '*', '', None, '../../../../etc/passwd', '/abs/path', '0000000-0000', 'A' * 36,
                    '11111111-2222-4333-8444-555555555555\n', '11111111-2222-4333-8444-555555555555/../x'):
            self.assertIsNone(server.Registry().get(bad), repr(bad))

    def test_sid_re(self):
        self.assertTrue(server.SID_RE.fullmatch('11111111-2222-4333-8444-555555555555'))
        self.assertTrue(server.SID_RE.fullmatch('019a0000-1111-7222-8333-444444444444'))
        self.assertFalse(server.SID_RE.fullmatch('11111111-2222-4333-8444-555555555555\n'))
        self.assertFalse(server.SID_RE.fullmatch('*'))


class Item2AllowedFile(unittest.TestCase):
    """/api/file. Refusing credential files and ~/.codex/ comes first; for debate roots there are three rules (dot folder, HOME ancestor, real debate folder)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(t, 'home')
        for d in ('.claude', '.codex', 'debate/r1', 'debate/.hidden', 'plain'):
            os.makedirs(os.path.join(self.home, d))
        self.w = self._write
        for rel in ('.claude/.credentials.json', '.claude.json', '.codex/auth.json', '.codex/x.json', 'debate/brief.md',
                    'debate/a.json', 'debate/r1/A.md', 'debate/.hidden/a.json', 'plain/a.json', 'top.md'):
            self.w(rel)
        os.symlink(os.path.join(self.home, '.codex/auth.json'), os.path.join(self.home, 'debate/link.md'))
        start_patches(self, HOME=self.home, CLAUDE_HOME=os.path.join(self.home, '.claude'),
                      CODEX_HOME=os.path.join(self.home, '.codex'),
                      DENY_FILES=(os.path.join(self.home, '.claude/.credentials.json'), os.path.join(self.home, '.claude.json'),
                                  os.path.join(self.home, '.codex/auth.json')))

    def _write(self, rel, txt='x'):
        with open(os.path.join(self.home, rel), 'w') as f:
            f.write(txt)

    def sess(self, roots, writes=()):
        s = server.Session.__new__(server.Session)
        s.lock = threading.RLock()
        a = server.Agent('a0123456789abcdef', {})
        a.writes = [{'ts': 1.0, 'path': os.path.join(self.home, w)} for w in writes]
        s.agents = {a.id: a}
        s.debates = lambda statuses: ([{'root': os.path.join(self.home, r)} for r in roots], {})
        return s

    def ok(self, s, rel):
        return bool(s.allowed_file(os.path.join(self.home, rel)))

    def test_root_is_home_denied(self):
        s = self.sess(['.'])
        self.assertFalse(self.ok(s, 'top.md'))
        self.assertFalse(self.ok(s, '.claude.json'))

    def test_root_is_home_ancestor_denied(self):
        s = self.sess(['..'])
        self.assertFalse(self.ok(s, 'top.md'))

    def test_real_debate_root(self):
        s = self.sess(['debate'])
        self.assertTrue(self.ok(s, 'debate/a.json'))
        self.assertTrue(self.ok(s, 'debate/r1/A.md'))
        self.assertFalse(self.ok(s, 'debate/.hidden/a.json'))         # under a dot folder
        self.assertFalse(self.ok(s, 'debate/a.exe'))                  # extension
        self.assertFalse(self.ok(s, 'top.md'))                        # outside the root

    def test_fake_root_denied(self):
        s = self.sess(['plain'])                                      # no brief.md and no r*/
        self.assertFalse(self.ok(s, 'plain/a.json'))

    def test_deny_list_before_everything(self):
        s = self.sess(['debate', '.codex', '.claude'], writes=['.claude/.credentials.json', '.codex/auth.json', '.codex/x.json'])
        for rel in ('.claude/.credentials.json', '.claude.json', '.codex/auth.json', '.codex/x.json'):
            self.assertFalse(self.ok(s, rel), rel)

    def test_symlink_to_auth_denied(self):
        s = self.sess(['debate'])
        self.assertFalse(self.ok(s, 'debate/link.md'))                # the name is .md, the real file is auth.json

    def test_agent_write_allowed(self):
        s = self.sess([], writes=['top.md', '.claude/memo.md'])
        self.assertTrue(self.ok(s, 'top.md'))
        self.w('.claude/memo.md', 'm')
        self.assertFalse(self.ok(s, '.claude/memo.md'))               # under a dot folder is not opened even if an agent wrote it

    def test_agent_written_config_denied(self):
        # Config files written by an agent (they may hold tokens): dot paths and secret names are refused regardless of extension or who wrote them
        rels = ['.docker/config.json', '.config/gh/hosts.yml', 'proj/.mcp.json', 'proj/secrets.yaml',
                'proj/credentials.json', 'proj/token.json', 'proj/api_key.txt', 'proj/kubeconfig.yaml', 'proj/auth.json',
                'proj/secret_handling.md', 'proj/credentials.md', 'proj/secrets.md', 'proj/Password-Policy.md', 'proj/token_notes.md']
        ok = ['proj/report.md', 'proj/notes.txt', 'proj/data.json']
        s = self.sess([], writes=rels + ok)
        for rel in rels:
            self.assertFalse(self.ok(s, rel), rel)
        for rel in ok:
            self.assertTrue(self.ok(s, rel), rel)

    def test_secret_names_apply_to_markdown_in_a_debate_root(self):
        # The same holds for documents in a debate folder with no write record: reports and the final document open, and sensitive names are refused even as .md
        for rel in ('debate/credentials.md', 'debate/secrets.md', 'debate/r1/api_token.md'):
            self.w(rel)
        s = self.sess(['debate'])
        for rel in ('debate/r1/A.md', 'debate/brief.md'):
            self.assertTrue(self.ok(s, rel), rel)
        for rel in ('debate/credentials.md', 'debate/secrets.md', 'debate/r1/api_token.md'):
            self.assertFalse(self.ok(s, rel), rel)

    def test_returns_real_path(self):
        os.symlink(os.path.join(self.home, 'debate'), os.path.join(self.home, 'dlink'))
        s = self.sess(['debate'])
        self.assertEqual(s.allowed_file(os.path.join(self.home, 'dlink/a.json')), os.path.join(self.home, 'debate/a.json'))


class FakeSession:
    """A session with only what Handler calls (do_GET is called directly, without a socket)."""
    def __init__(self):
        self.version = 7
        self.lock = threading.RLock()
        self.feed = [{'kind': k, 'ts': float(i), 'text': 't%d' % i} for i, k in
                     enumerate(['user_say', 'orch_say', 'spawn', 'user_say', 'orch_say', 'orch_ask', 'user_answer', 'user_say', 'orch_say', 'orch_say'])]

    def state(self):
        return {'state': 1}

    def timeline(self, since):
        return {'since': since}


class FakeReg:
    def __init__(self, s):
        self.s = s

    def get(self, sid):
        return self.s if sid else None


def call(path, s=None, host='localhost:8790'):
    """Calls do_GET and returns (code, body)."""
    sent = []
    h = server.Handler.__new__(server.Handler)
    h.path = path
    h.headers = {'Host': host}
    h._send = lambda code, body, ctype=None: sent.append((code, body))
    with mock.patch.object(server, 'REG', FakeReg(s or FakeSession())):
        h.do_GET()
    return sent[0]


class Item4NumericArgs(unittest.TestCase):
    """Numeric arguments. A bad value gives 400 JSON (it used to drop the connection without a response). limit 1–300, idx 0 or more."""
    S = '&session=11111111-2222-4333-8444-555555555555'

    def test_bad_values_400(self):
        for q in ('/api/state?v=7&t=abc', '/api/state?t=nan', '/api/state?t=inf', '/api/timeline?since=abc', '/api/timeline?since=nan',
                  '/api/talk?before=abc', '/api/talk?limit=abc', '/api/talk?limit=0', '/api/talk?limit=-3', '/api/talk?before=1.5',
                  '/api/event?idx=abc', '/api/event?idx=-1', '/api/event?idx=', '/api/event?idx=1.5', '/api/event?idx=' + '9' * 5000):
            code, body = call(q + self.S)
            self.assertEqual((code, body), (400, {'error': 'bad parameter', 'error_code': 'bad_parameter'}), q)

    def test_state(self):
        self.assertEqual(call('/api/state?v=7&t=%f%s' % (server.time.time(), self.S)), (200, {'unchanged': True, 'version': 7}))
        self.assertEqual(call('/api/state?v=6&t=%f%s' % (server.time.time(), self.S)), (200, {'state': 1}))
        self.assertEqual(call('/api/state?v=7&t=1.5' + self.S), (200, {'state': 1}))     # an old t
        self.assertEqual(call('/api/state?x=1' + self.S), (200, {'state': 1}))

    def test_timeline(self):
        self.assertEqual(call('/api/timeline?since=123.5' + self.S), (200, {'since': 123.5}))
        code, body = call('/api/timeline?x=1' + self.S)
        self.assertEqual(code, 200)
        self.assertAlmostEqual(body['since'], server.time.time() - 3600, delta=5)

    def test_talk(self):
        code, body = call('/api/talk?limit=2' + self.S)
        self.assertEqual((code, [e['idx'] for e in body['items']], body['more']), (200, [8, 9], True))
        code, body = call('/api/talk?limit=2&before=8' + self.S)
        self.assertEqual([e['idx'] for e in body['items']], [6, 7])
        code, body = call('/api/talk?limit=300' + self.S)                 # 1–300 is the allowed range
        self.assertEqual((code, len(body['items']), body['more']), (200, 9, False))
        self.assertEqual(call('/api/talk?limit=301' + self.S)[0], 400)    # outside the range is 400 (it is not clamped to 300)
        self.assertEqual(call('/api/talk?limit=500' + self.S)[0], 400)
        code, body = call('/api/talk?' + self.S[1:])
        self.assertEqual(len(body['items']), 9)

    def test_event(self):
        self.assertEqual(call('/api/event?idx=2' + self.S)[1]['kind'], 'spawn')
        self.assertEqual(call('/api/event?idx=99' + self.S), (404, {'error': 'no event', 'error_code': 'no_event'}))

    def test_unrelated_param_ignored(self):
        self.assertEqual(call('/api/state?limit=abc&idx=zz' + self.S), (200, {'state': 1}))


class Item5HostArg(unittest.TestCase):
    """--host takes any IP address (loopback, wildcard, LAN, VPN, IPv6) or a host name, and nothing else. It is tested through the function, without opening a socket."""

    def test_allowed(self):
        for h in ('127.0.0.1', '127.5.5.5', '::1', 'localhost', '100.100.0.7', '100.64.0.0', '100.127.255.255', 'fd7a:115c:a1e0::1',
                  '0.0.0.0', '::', '192.168.0.5', '10.0.0.1', '192.0.2.28', '2001:db8::1', 'fe80::1', 'my.box', 'box.example.net', 'host-1', '::ffff:10.0.0.1'):
            self.assertEqual(server._host_arg(h), h)

    def test_brackets_and_spaces_are_taken_off(self):
        self.assertEqual(server._host_arg('[::]'), '::')
        self.assertEqual(server._host_arg(' [fd7a:115c:a1e0::1] '), 'fd7a:115c:a1e0::1')
        self.assertEqual(server._host_arg(' 0.0.0.0 '), '0.0.0.0')

    def test_refused(self):
        for h in ('', ' ', '*', '0.0.0.0:8790', 'host:80', 'http://host', 'host/path', 'a b', '-bad', 'bad-', 'a..b', '[::1', '::1]', 'ho$t'):
            with self.assertRaises(argparse.ArgumentTypeError, msg=repr(h)):
                server._host_arg(h)

    def test_help_has_no_fixed_vpn_name(self):
        import io
        out = io.StringIO()
        with terminal_lang('ko'), mock.patch('sys.argv', ['server.py', '--help']), mock.patch('sys.stdout', out), self.assertRaises(SystemExit):
            server.main()
        self.assertIn('--allow-host', out.getvalue())
        self.assertNotIn('netbird.cloud', out.getvalue().lower())          # no built-in suffix is described


class Item6HostHeader(unittest.TestCase):
    """The Host header is compared by name only (the port is ignored). Only loopback names, the address the server was opened on and --allow-host pass; everything else gets 403 (no built-in suffix)."""
    S = '?session=11111111-2222-4333-8444-555555555555'

    def test_names(self):
        for h in ('localhost', 'localhost:8790', 'localhost:12345', '127.0.0.1:8790', '127.0.0.1', '[::1]:8790', '[::1]', 'LOCALHOST:8790',
                  'localhost.:8790'):
            self.assertTrue(server.host_allowed(h), h)

    def test_refused(self):
        for h in ('', None, 'evil.test', 'evil.test:8790', '127.0.0.1.evil.test', 'localhost.evil.test', 'box.example.net', 'box.netbird.cloud',
                  'box.tail1234.ts.net', '100.100.0.7', '::1', '[::1', '0.0.0.0:8790', 'localhost@evil.test'):
            self.assertFalse(server.host_allowed(h), repr(h))

    def test_added_names(self):
        with mock.patch.object(server, 'ALLOWED_HOSTS', server.ALLOWED_HOSTS | {'100.100.0.7', 'my.box'}):
            self.assertTrue(server.host_allowed('100.100.0.7:8790'))
            self.assertTrue(server.host_allowed('my.box:9999'))
        self.assertFalse(server.host_allowed('my.box'))

    def test_handler_403(self):
        code, body = call('/api/state' + self.S, host='evil.test:8790')
        self.assertEqual(code, 403)
        self.assertIn(b'--allow-host evil.test', body)                    # how to fix it: an English line first, then the line in the board language
        self.assertEqual(len(body.decode().splitlines()), 2)
        self.assertEqual(call('/', host='evil.test')[0], 403)
        self.assertEqual(call('/api/state' + self.S, host='localhost:5555')[0], 200)
        self.assertEqual(call('/api/state' + self.S, host=None)[0], 403)


class Item3ReportRe(unittest.TestCase):
    """REPORT_RE does not start in the middle of a path ('/t2_error' in '…/t2_error/r2/A.md')."""

    def root(self, text):
        m = server.REPORT_RE.search(text)
        return m.group(1) if m else None

    def test_ellipsis_and_dots(self):
        self.assertIsNone(self.root('x (`.../t2_error/r2/A.md`)'))
        self.assertIsNone(self.root('x …/t2_error/r2/A.md'))
        self.assertIsNone(self.root('x ../t2_error/r2/A.md'))

    def test_ok(self):
        self.assertEqual(self.root('see `~/Dev/x/r1/A.md` now'), '~/Dev/x')
        self.assertEqual(self.root('(/tmp/x/r1/B.md)'), '/tmp/x')
        self.assertEqual(self.root('/home/u/p/review/t1/r1/A.md'), '/home/u/p/review/t1')
        self.assertEqual(self.root('"~/r1/A.md"'), '~')
        self.assertEqual(self.root('경로: /home/u/d/r2/C.md에 쓴다'), '/home/u/d')
        self.assertEqual(self.root('/a/r1/X.md'), '/a')

    def test_relative_none(self):
        self.assertIsNone(self.root('review/t2/r1/A.md'))
        self.assertIsNone(self.root('foo/bar/r1/A.md'))

    def test_all_matches(self):
        t = '/a/b/r1/A.md and /c/d/r2/B.md, also ~/e/r1/C.md'
        self.assertEqual([m.group(1) for m in server.REPORT_RE.finditer(t)], ['/a/b', '/c/d', '~/e'])


class DebateBase(unittest.TestCase):
    """Common set-up of the debate table tests: two debates (t9, t8) in a temporary folder, and a Session built without a file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.unit = os.path.join(self.root, 't9')
        self.unit2 = os.path.join(self.root, 't8')
        for u in (self.unit, self.unit2):
            os.makedirs(os.path.join(u, 'r1'))
            for name in ('brief.md', 'r1/A.md', 'r1/B.md'):
                with open(os.path.join(u, name), 'w') as f:
                    f.write('# %s\nx\n' % os.path.basename(u))

    def sess(self, agents):
        s = server.Session.__new__(server.Session)
        s.lock = threading.RLock()
        s._file_cache, s._head_cache = {}, {}
        s.agents = {a.id: a for a in agents}
        return s

    def agent(self, aid, tag, prompt, received=()):
        a = server.Agent(aid, {'description': '%s worker' % tag})
        a.spawn_prompt = prompt
        a.spawn_ts = 100.0
        a.received = [{'ts': 200.0 + i, 'text': t} for i, t in enumerate(received)]
        return a

    def cells(self, s, status, row):
        d, _ = s.debates({aid: status for aid in s.agents})
        top = next(t for dd in d for t in dd['topics'] if t['dir'] == self.unit)
        r = next(x for x in top['rows'] if x['p'] == row)
        return top['rounds'], [(c['round'], c['state'], c['agent']) for c in r['cells']]


class Item20RelativeReports(DebateBase):
    """A relative report path (`r2/A.md`) in a Claude agent's instruction is resolved against that folder only when the agent already belongs to exactly one debate folder."""

    def test_resolved_when_one_unit(self):
        a = self.agent('aA', 'T9-A', '결과를 `%s/r1/A.md`에 쓴다' % self.unit, ['R2 시작: `r1/B.md`를 읽고 `r2/A.md`를 쓴다'])
        s = self.sess([a])
        rounds, cells = self.cells(s, 'running', 'A')
        self.assertEqual(rounds, [1, 2])
        self.assertEqual(cells, [(1, 'done', 'aA'), (2, 'writing', 'aA')])        # the round being written now is 2

    def test_not_resolved_without_absolute_ref(self):
        a = self.agent('aA', 'T9-A', 'r1/A.md를 쓴다', ['R2: r2/A.md를 쓴다'])          # there is no folder it already belongs to
        self.assertEqual(self.sess([a]).debates({'aA': 'running'})[0], [])

    def test_not_resolved_with_two_units(self):
        a = self.agent('aA', 'T9-A', '`%s/r1/A.md`에 쓴다. `%s/r1/A.md`에 쓴다.' % (self.unit, self.unit2), ['R2: r2/A.md를 쓴다'])
        s = self.sess([a])
        rounds, cells = self.cells(s, 'running', 'A')
        self.assertEqual(rounds, [1])
        self.assertEqual(cells, [(1, 'draft', 'aA')])                             # no round 2 cell appears

    def test_other_tag_not_own(self):
        a = self.agent('aB', 'T9-B', '`%s/r1/B.md`에 쓴다' % self.unit, ['R2: r2/A.md를 읽는다'])   # someone else's report (A) is not this agent's cell
        s = self.sess([a])
        rounds, cells = self.cells(s, 'running', 'B')
        self.assertEqual(cells, [(1, 'draft', 'aB')])
        self.assertEqual(rounds, [1])


class Item8Registry(unittest.TestCase):
    """Concurrent requests for the same id get the one session whose first read has finished. Catching up also looks at linked Codex tails. entries() is a copy."""
    SID = '11111111-2222-4333-8444-555555555555'

    def test_concurrent_get_opens_once(self):
        reg = server.Registry()
        opened = []

        def slow_open(sid):
            opened.append(sid)
            time.sleep(0.3)
            obj = types.SimpleNamespace(provider='claude')
            with reg.lock:
                reg.sessions[sid] = obj
            return obj
        reg._open = slow_open
        got = []
        ths = [threading.Thread(target=lambda: got.append(reg.get(self.SID))) for _ in range(8)]
        for t in ths:
            t.start()
            time.sleep(0.02)
        for t in ths:
            t.join()
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(got), 8)
        self.assertTrue(all(g is got[0] for g in got))
        self.assertEqual(reg.loading, {})

    def test_failed_open_releases_waiters(self):
        reg = server.Registry()
        calls = []

        def bad_open(sid):
            calls.append(sid)
            time.sleep(0.1)
            raise OSError('boom')
        reg._open = bad_open
        errs = []

        def run():
            try:
                reg.get(self.SID)
            except OSError:
                errs.append(1)
        ths = [threading.Thread(target=run) for _ in range(3)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(10)
        self.assertEqual(reg.loading, {})
        self.assertEqual(len(errs), len(calls))            # the waiting requests try to open it again, and all of them finish

    def test_open_does_not_register_if_first_poll_raises(self):   # if the first read raises, the session is not registered
        reg = server.Registry()
        order = []

        class Boom:
            provider = 'claude'
            tail = types.SimpleNamespace(max_read=None, behind=lambda: False)
            codex = None

            def __init__(self, path):
                self.path = path

            def poll(self):
                order.append(('poll', self.SID_NOT_REGISTERED_YET()))
                raise OSError('boom')

            def SID_NOT_REGISTERED_YET(self):
                return Item8Registry.SID in reg.sessions
        ready = threading.Event()
        ready.set()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        record = os.path.join(tmp.name, '%s.jsonl' % self.SID)           # a record that is there: only a regular file is opened as a session
        open(record, 'w').close()
        with patched(Session=Boom), mock.patch.object(server.LINKS, 'ready', ready), \
                mock.patch('glob.glob', lambda pat: [record]):
            with self.assertRaises(OSError):
                reg.get(self.SID)
        self.assertEqual(order, [('poll', False)])                   # not yet registered while it is being read
        self.assertNotIn(self.SID, reg.sessions)                     # if the first read fails it is not registered (an incomplete session is not cached)
        self.assertEqual(reg.loading, {})

    def _fake(self, main_behind=(), codex_behind=()):
        class Tail:
            def __init__(self, seq, max_read=None):
                self.seq, self.max_read = list(seq), max_read

            def behind(self):
                return bool(self.seq and self.seq[0])

            def step(self):
                if self.seq:
                    self.seq.pop(0)

        class S:
            pass
        s = S()
        s.lock = threading.RLock()
        s.feed = [{'ts': 5.0, 'i': 'a'}, {'ts': 1.0, 'i': 'b'}]
        s.tail = Tail(main_behind, max_read=16 if main_behind else None)
        s.codex = S()
        s.codex.tails = {'t%d' % i: Tail(seq) for i, seq in enumerate(codex_behind)}
        s.polls = 0

        def poll():
            s.polls += 1
            s.tail.step()
            for t in (s.codex.tails.values() if s.codex else ()):
                t.step()
        s.poll = poll
        return s

    def test_catch_up_covers_codex_tails(self):
        s = self._fake(codex_behind=[[True, True, False]])
        server.Registry._catch_up(s)
        self.assertEqual(s.polls, 2)
        self.assertEqual([e['i'] for e in s.feed], ['b', 'a'])          # events appended afterwards are slotted back in time order

    def test_catch_up_main_tail_and_limit(self):
        s = self._fake(main_behind=[True] * 20)
        server.Registry._catch_up(s)
        self.assertEqual(s.polls, 8)

    def test_catch_up_nothing_behind(self):
        s = self._fake()
        server.Registry._catch_up(s)
        self.assertEqual(s.polls, 0)
        self.assertEqual([e['i'] for e in s.feed], ['a', 'b'])          # left untouched

    def test_catch_up_codex_session_without_linker(self):
        s = self._fake(main_behind=[True, False])
        s.codex = None
        server.Registry._catch_up(s)
        self.assertEqual(s.polls, 1)

    def test_entries_are_copies(self):
        idx = server.CodexIndex()
        idx.files['p'] = {'id': 'x', 'turns': [], 'model': 'm'}
        e = idx.entries()[0]
        e['id'], e['model'] = 'changed', 'other'
        self.assertEqual((idx.files['p']['id'], idx.files['p']['model']), ('x', 'm'))


class Item9Launches(unittest.TestCase):
    """`codex exec` inside quotes, in a heredoc or in a comment is not a launch. A relative -C becomes an absolute path based on the shell's working folder."""
    UID = '019a0000-0000-7000-8000-000000000000'

    def launch(self, cmd):
        c = server.cx_parse_call(1.0, 'u', {'command': cmd}, '/w')
        return bool(c and c['launch'])

    def test_not_launch(self):
        for cmd in ("printf '%s' '(codex exec resume " + self.UID + " fake)'",
                    'echo "x; codex exec y"', "echo 'codex exec a'", 'echo "run codex exec now" && ls',
                    "cat <<'EOF'\ncodex exec \"hi\"\nEOF", "python3 - <<EOF\nprint('codex exec x')\nEOF",
                    "cat >> notes.md <<'EOF'\n- `codex exec` 는 새 디렉터리에서\nEOF\nls",
                    "cat <<-EOF\n\tcodex exec x\n\tEOF\nls",
                    "# codex exec x\nls", "ls # then codex exec x",
                    "codex exec --help", "pgrep codex"):
            self.assertFalse(self.launch(cmd), cmd)

    def test_launch(self):
        # The double-quote and single-quote cases of the test above: only a codex exec at a command position (after ; & | ( ) is a launch
        for cmd in ('codex exec "x"', 'y="$(codex exec "x")"', 'y=`codex exec x`', "bash -c 'cd /x; codex exec \"x\"'",
                    'sh -c "cd /x; codex exec x"', 'nohup codex exec x &', 'cd /a && codex exec "x"',
                    'cat <<EOF\nfoo\nEOF\ncodex exec "real"', 'for i in 1 2; do codex exec "$Q"; done',
                    "x=$(cat <<'EOF'\nq\nEOF\n); codex exec \"$x\"", "# don't stop\ncodex exec \"real\"",
                    "echo 'a'; codex exec \"b\"", 'timeout 10 codex exec x', "eval 'cd /x; codex exec x'",
                    '(cd /a; codex exec "x")', 'echo hi && (codex exec x) | tee out'):
            self.assertTrue(self.launch(cmd), cmd)

    def test_launch_list(self):
        L = server.cx_launches('echo \'codex exec a\'; codex exec "b"; cat <<EOF\ncodex exec c\nEOF', {}, '/w')
        self.assertEqual(len(L), 1)

    def test_shell_code_length(self):
        for cmd in ("a 'b c' \"d $(e 'f') g\" `h` <<EOF\nx\nEOF\ny", "echo 'unterminated", 'echo "unterminated $(x', "cat <<EOF\nno end",
                    "\\'x\\'", '$((1+2)) "$(( 3 ))"', "a <<< 'here string' # c", "<<", "<<'", ""):
            self.assertEqual(len(server.shell_code(cmd)), len(cmd), cmd)

    def test_shell_code_never_raises(self):
        import random
        rnd = random.Random(1)
        for _ in range(3000):
            cmd = ''.join(rnd.choice(["'", '"', '`', '$(', ')', '(', '<<', 'EOF', '\n', ' ', '#', '\\', 'x', '-', 'codex exec ', ';']) for _ in range(rnd.randint(0, 25)))
            self.assertEqual(len(server.shell_code(cmd)), len(cmd))

    def test_relative_cd_option(self):
        f = lambda cmd, base='/w': server.cx_launches(cmd, {}, base)[0]
        self.assertEqual(f('cd /x && codex exec -C . "p"')['cwd'], '/x')
        self.assertEqual(f('cd /x && codex exec -C sub "p"')['cwd'], '/x/sub')
        self.assertEqual(f('cd /x/y && codex exec -C ../z "p"')['cwd'], '/x/z')
        self.assertEqual(f('codex exec -C . "p"')['cwd'], '/w')
        self.assertEqual(f('codex exec --cd sub/dir "p"')['cwd'], '/w/sub/dir')
        self.assertEqual(f('codex exec -C /abs/dir "p"')['cwd'], '/abs/dir')
        self.assertEqual(f('codex exec -C "$PWD/x" "p"')['cwd'], '/w/x')
        self.assertEqual(f('codex exec -C sub "p"', base=None)['cwd'], 'sub')          # if it cannot be resolved it stays as before
        self.assertEqual(f('cd /x && codex exec -C . "p"')['scwd'], '/x')

    def test_cd_inside_quotes_ignored(self):
        L = server.cx_launches("echo 'cd /nope'; codex exec \"p\"", {}, '/w')
        self.assertEqual(L[0]['scwd'], '/w')

    def test_link_with_relative_cd_option(self):
        prompt = 'Please review the design document thoroughly and write your findings to a report file'
        call = server.cx_parse_call(100.0, 'tu1', {'command': 'cd /x && codex exec -C . "%s"' % prompt}, '/w')
        t = {'id': 't1', 'origin': 'exec', 'guardian': False, 'meta_ts': 102.0, 'first_user': prompt, 'cwd': '/x'}
        o = server.cx_link([('sidA', call)], [t])
        self.assertEqual((o['t1']['sid'], o['t1']['rule'], o['t1']['cwd_ok']), ('sidA', 'prompt', True))

    def test_heredoc_call_does_not_link(self):
        cmd = "cat >> notes.md <<'EOF'\nrun: codex exec \"Please review the design document thoroughly and write your findings\"\nEOF"
        call = server.cx_parse_call(100.0, 'tu1', {'command': cmd}, '/w')
        self.assertIsNone(call)


class Item10Ownership(unittest.TestCase):
    """When ownership changes, CodexLinker drops the threads it no longer owns (events and idx stay). A cached standalone Codex session is returned after the ownership check."""
    TID = '019a0000-0000-7000-8000-0000000000aa'
    X = '11111111-2222-4333-8444-555555555555'

    def rollout(self, d):
        path = os.path.join(d, 'rollout-2026-09-30T00-00-00-%s.jsonl' % self.TID)
        lines = [
            '{"timestamp":"2026-09-30T00:00:00.000Z","type":"session_meta","payload":{"id":"%s","cwd":"/w"}}' % self.TID,
            '{"timestamp":"2026-09-30T00:00:01.000Z","type":"event_msg","payload":{"type":"task_started"}}',
            '{"timestamp":"2026-09-30T00:00:01.500Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}',
            '{"timestamp":"2026-09-30T00:00:09.000Z","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"done"}}']
        with open(path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return path

    def test_linker_drops_and_restores(self):
        with tempfile.TemporaryDirectory() as d:
            e = {'id': self.TID, 'path': self.rollout(d), 'cwd': '/w', 'meta_ts': 1790726400.0, 'model': 'gpt-6.1-sol'}
            info = {'sid': self.X, 'rule': 'prompt', 'call': 'tu1', 'bash_ts': 1790726399.0, 'bash_desc': '', 'dt': 1.0, 'cwd': '/w',
                    'cwd_ok': True, 'prompt_ok': True}
            sess = types.SimpleNamespace(id=self.X, agents={}, feed=[])
            sess._event = lambda ts, kind, frm, to, title, text='', agent=None, extra=None, **kw: sess.feed.append({'ts': ts, 'kind': kind, 'agent': agent})
            ready = threading.Event()
            ready.set()
            owners = {self.TID: info}
            with mock.patch.object(server.LINKS, 'ready', ready), mock.patch.object(server.LINKS, 'owners', owners), \
                    mock.patch.object(server.CODEX, 'by_id', {self.TID: e}):
                lk = server.CodexLinker(sess)
                self.assertTrue(lk.poll())
                a = lk.agents[self.TID]
                self.assertIs(sess.agents[self.TID], a)
                n_feed = len(sess.feed)
                self.assertGreater(n_feed, 0)
                owners.clear()                                  # linked -> ambiguous
                self.assertTrue(lk.poll())
                self.assertNotIn(self.TID, lk.agents)
                self.assertNotIn(self.TID, sess.agents)
                self.assertNotIn(self.TID, lk.tails)
                self.assertEqual(len(sess.feed), n_feed)        # events already emitted are not removed
                self.assertFalse(lk.poll())                     # no change if nothing changed
                owners[self.TID] = info                         # owned again
                self.assertTrue(lk.poll())
                self.assertIs(sess.agents[self.TID], a)         # the same agent is revived
                self.assertEqual(len(sess.feed), n_feed)        # events are not duplicated

    def test_cached_standalone_redirects_when_owned(self):
        reg = server.Registry()
        cx = types.SimpleNamespace(provider='codex', id=self.TID)
        owner_sess = types.SimpleNamespace(provider='claude', id=self.X)
        reg.sessions = {self.TID: cx, self.X: owner_sess}
        with mock.patch.object(server.LINKS, 'owners', {}):
            self.assertIs(reg.get(self.TID), cx)                # with no owner it stays as is
        with mock.patch.object(server.LINKS, 'owners', {self.TID: {'sid': self.X}}):
            self.assertIs(reg.get(self.TID), owner_sess)        # linked later
            self.assertIs(reg.get(self.X), owner_sess)


class Item11TurnSeq(unittest.TestCase):
    """Codex turns get a monotonically increasing number, used for the event dedup key and the first-turn check. Even when they grow past 200, the events of new turns keep coming out."""
    TID = '019a0000-0000-7000-8000-0000000000bb'

    def turn_lines(self, k):
        t = 1790726400.0 + k * 10
        ts = lambda x: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(x))
        return ['{"timestamp":"%s","type":"event_msg","payload":{"type":"task_started"}}' % ts(t),
                '{"timestamp":"%s","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"go %d"}]}}' % (ts(t + 1), k),
                '{"timestamp":"%s","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"done %d"}}' % (ts(t + 5), k)]

    def setup_linker(self, d):
        path = os.path.join(d, 'rollout-x-%s.jsonl' % self.TID)
        with open(path, 'w') as f:
            f.write('{"timestamp":"2026-09-30T00:00:00.000Z","type":"session_meta","payload":{"id":"%s","cwd":"/w"}}\n' % self.TID)
        e = {'id': self.TID, 'path': path, 'cwd': '/w', 'meta_ts': 1790726400.0, 'model': 'gpt-6.1-sol'}
        info = {'sid': 'sidX', 'rule': 'prompt', 'call': None, 'bash_ts': 1790726399.0, 'bash_desc': '', 'dt': 1.0, 'cwd': '/w', 'cwd_ok': True, 'prompt_ok': True}
        sess = types.SimpleNamespace(id='sidX', agents={}, feed=[])
        sess._event = lambda ts, kind, frm, to, title, text='', agent=None, extra=None, **kw: sess.feed.append({'ts': ts, 'kind': kind, 'agent': agent, 'text': text})
        ready = threading.Event()
        ready.set()
        patches = [mock.patch.object(server.LINKS, 'ready', ready), mock.patch.object(server.LINKS, 'owners', {self.TID: info}),
                   mock.patch.object(server.CODEX, 'by_id', {self.TID: e})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return path, sess, server.CodexLinker(sess)

    def kinds(self, sess):
        out = {}
        for e in sess.feed:
            out[e['kind']] = out.get(e['kind'], 0) + 1
        return out

    def test_205_turns_grow_live(self):
        with tempfile.TemporaryDirectory() as d:
            path, sess, lk = self.setup_linker(d)
            for k in range(205):
                with open(path, 'a') as f:
                    f.write('\n'.join(self.turn_lines(k)) + '\n')
                lk.poll()
            a = lk.agents[self.TID]
            self.assertEqual(len(a.turns), 200)                       # the kept list is 200
            self.assertEqual((a.turns[0]['n'], a.turns[-1]['n'], a.turn_seq), (5, 204, 205))
            self.assertEqual(self.kinds(sess), {'spawn': 1, 'orch_msg': 204, 'handback': 205, 'notify': 205})
            self.assertEqual(len(a.notifications), 205)
            n = len(sess.feed)
            lk.poll()
            self.assertEqual(len(sess.feed), n)                       # no duplicates when the same poll is repeated
            self.assertEqual(sess.feed[-1]['text'], 'done 204')       # even the final report of the last turn came out

    def test_public_turns_keys_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path, sess, lk = self.setup_linker(d)
            with open(path, 'a') as f:
                f.write('\n'.join(self.turn_lines(0)) + '\n')
            lk.poll()
            s = server.Session.__new__(server.Session)
            s.lock = threading.RLock()
            s.agents = sess.agents
            s._name_agents = lambda: None
            s._attach_main_side = lambda: None
            det = s.agent_detail(self.TID)
            self.assertEqual(set(det['turns'][0]), {'start', 'end', 'status', 'error', 'bash_ts', 'out', 'out_state'})


class Item12Determinism(DebateBase):
    """The cell's assignee is the last in (launch time, id) order, the writer is the agent with the earliest write time. The order in which agents were found does not matter."""

    def make(self, order, spawn=(100.0, 200.0), writes=None):
        a1 = self.agent('aA1', 'T9-A', '`%s/r1/A.md`에 쓴다' % self.unit)
        a2 = self.agent('aA2', 'T9-A', '`%s/r1/A.md`에 쓴다' % self.unit)
        a1.spawn_ts, a2.spawn_ts = spawn
        for a, ts in zip((a1, a2), writes or ()):
            a.writes = [{'ts': ts, 'path': os.path.join(self.unit, 'r1', 'A.md')}]
        by = {'aA1': a1, 'aA2': a2}
        return self.sess([by[i] for i in order])

    def cell(self, s, status='done'):
        d, _ = s.debates({aid: status for aid in s.agents})
        top = next(t for dd in d for t in dd['topics'] if t['dir'] == self.unit)
        return next(x for x in top['rows'] if x['p'] == 'A')['cells'][0]

    def test_assignee_is_latest_spawn(self):
        for order in (('aA1', 'aA2'), ('aA2', 'aA1')):
            c = self.cell(self.make(order))
            self.assertEqual(c['agent'], 'aA2', order)
            self.assertEqual(c['state'], 'done')

    def test_assignee_status_follows(self):
        s = self.make(('aA2', 'aA1'))
        d, _ = s.debates({'aA1': 'running', 'aA2': 'ended'})           # aA2, launched later, is the assignee, so the cell is not a draft even while aA1 runs
        top = next(t for dd in d for t in dd['topics'] if t['dir'] == self.unit)
        self.assertEqual(next(x for x in top['rows'] if x['p'] == 'A')['cells'][0]['state'], 'done')

    def test_assignee_tie_uses_id(self):
        for order in (('aA1', 'aA2'), ('aA2', 'aA1')):
            self.assertEqual(self.cell(self.make(order, spawn=(150.0, 150.0)))['agent'], 'aA2')

    def test_writer_is_earliest(self):
        for order in (('aA1', 'aA2'), ('aA2', 'aA1')):
            self.assertEqual(self.cell(self.make(order, writes=(300.0, 250.0)))['writer'], 'aA2', order)
            self.assertEqual(self.cell(self.make(order, writes=(250.0, 300.0)))['writer'], 'aA1', order)

    def test_writer_tie_uses_id(self):
        for order in (('aA1', 'aA2'), ('aA2', 'aA1')):
            self.assertEqual(self.cell(self.make(order, writes=(300.0, 300.0)))['writer'], 'aA1', order)

    def test_no_writer(self):
        self.assertIsNone(self.cell(self.make(('aA1', 'aA2')))['writer'])

    def test_xread_author_earliest_writer(self):
        path = os.path.join(self.unit, 'r1', 'A.md')
        for order in (('aA1', 'aA2', 'aB'), ('aB', 'aA2', 'aA1')):
            a1 = self.agent('aA1', 'T9-A', 'x')
            a2 = self.agent('aA2', 'T9-A', 'x')
            b = self.agent('aB', 'T9-B', 'x')
            a1.writes = [{'ts': 300.0, 'path': path}]
            a2.writes = [{'ts': 250.0, 'path': path}]
            b.read_log = [(400.0, path)]
            by = {'aA1': a1, 'aA2': a2, 'aB': b}
            s = self.sess([by[i] for i in order])
            s.feed, s._derived = [], {}
            s._agent_events()
            xr = [e for e in s.feed if e['kind'] == 'xread']
            self.assertEqual([e['author'] for e in xr], ['aA2'], order)

    def test_writer_table(self):
        s = self.make(('aA2', 'aA1'), writes=(300.0, 250.0))
        self.assertEqual(server.writer_table(s), {os.path.join(self.unit, 'r1', 'A.md'): 'aA2'})


class Item13UserSay(unittest.TestCase):
    """A duplicate user instruction is one with identical full text and a time difference within 120 seconds (the double record by the queued_command and human lines). The first-200-characters rule is dropped."""

    def sess(self):
        s = server.Session.__new__(server.Session)
        s.feed, s.user_seen, s.pending_q, s.first_ts, s.cwd, s.slug, s.title, s.codex = [], {}, {}, None, '', '', '', None
        s.orch = {'state': 'idle', 'last_action': '', 'last_action_ts': None, 'last_say': '', 'last_say_ts': None,
                  'last_ts': None, 'pending_bg': 0, 'model': '', 'effort': ''}
        s.orch_tokens = server.TokenMeter()
        return s

    def says(self, s):
        return [(e['ts'], e['text']) for e in s.feed if e['kind'] == 'user_say']

    def test_repeat_after_window_kept(self):
        s = self.sess()
        s._user_say(1000.0, '계속해')
        s._user_say(1000.0 + 119, '계속해')                  # counted as a double record
        s._user_say(1000.0 + 121, '계속해')                  # 121 s after the first: entered separately
        s._user_say(1000.0 + 700, '계속해')
        self.assertEqual([t for t, _ in self.says(s)], [1000.0, 1121.0, 1700.0])

    def test_window_is_from_last_kept(self):
        s = self.sess()
        for dt in (0, 100, 200, 300):                        # they keep coming at 100 s intervals: 100, within 120 of 0, is filtered out, and 200 is a new instruction
            s._user_say(1000.0 + dt, '진행해')
        self.assertEqual([t for t, _ in self.says(s)], [1000.0, 1200.0])

    def test_full_text_not_prefix(self):
        s = self.sess()
        head = 'x' * 200
        s._user_say(1000.0, head + ' A')
        s._user_say(1010.0, head + ' B')                     # a long instruction that matches only in the first 200 characters is a different instruction
        s._user_say(1020.0, head + ' A')                     # identical in full and within 120 s: a duplicate
        self.assertEqual([x for _, x in self.says(s)], [head + ' A', head + ' B'])

    def test_whitespace_and_reminders(self):
        s = self.sess()
        s._user_say(1000.0, '  go  <system-reminder>x</system-reminder>')
        s._user_say(1005.0, 'go')
        s._user_say(1006.0, '<x>tag</x>')                      # text starting with '<' is not an instruction (as before)
        self.assertEqual(len(self.says(s)), 1)

    def test_unknown_time_is_duplicate(self):
        s = self.sess()
        s._user_say(1000.0, 'a')
        s._user_say(None, 'a')
        s._user_say(None, 'b')
        s._user_say(None, 'b')
        self.assertEqual([x for _, x in self.says(s)], ['a', 'b'])

    def test_queued_and_human_pair_in_feed_main(self):
        s = self.sess()
        s._feed_main({'type': 'attachment', 'timestamp': '2026-09-30T00:00:10.000Z',
                      'attachment': {'type': 'queued_command', 'commandMode': 'prompt', 'prompt': '해줘', 'origin': {'kind': 'human'},
                                     'timestamp': '2026-09-30T00:00:00.000Z'}})
        s._feed_main({'type': 'user', 'timestamp': '2026-09-30T00:01:00.000Z', 'origin': {'kind': 'human'}, 'message': {'content': '해줘'}})
        self.assertEqual(len(self.says(s)), 1)               # a double record of the same input (60 s apart)
        s._feed_main({'type': 'user', 'timestamp': '2026-09-30T00:30:00.000Z', 'origin': {'kind': 'human'}, 'message': {'content': '해줘'}})
        self.assertEqual(len(self.says(s)), 2)               # entered again 30 minutes later

    def test_codex_session_same_rule(self):
        s = server.CodexSession.__new__(server.CodexSession)
        s.feed, s.user_seen = [], {}
        s._user_say(1000.0, '<env>keep</env>', raw=True)         # Codex does not filter out text starting with '<' (the leading part is already stripped)
        s._user_say(1050.0, '<env>keep</env>', raw=True)
        s._user_say(1500.0, '<env>keep</env>', raw=True)
        self.assertEqual([t for t, _ in self.says(s)], [1000.0, 1500.0])


if __name__ == '__main__':
    unittest.main()


class FileSizeCap(unittest.TestCase):
    """/api/file: a file over 2 MiB gets 413 (its content is not read); below that it is served as before."""
    S = '&session=11111111-2222-4333-8444-555555555555'

    def test_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            small, big = os.path.join(root, 'small.md'), os.path.join(root, 'big.md')
            with open(small, 'w') as f:
                f.write('# ok\n')
            with open(big, 'w') as f:
                f.write('x' * (server.FILE_MAX + 1))
            s = FakeSession()
            s.allowed_file = lambda p: os.path.realpath(p)
            code, body = call('/api/file?path=' + small + self.S, s)
            self.assertEqual((code, body['text']), (200, '# ok\n'))
            code, body = call('/api/file?path=' + big + self.S, s)
            self.assertEqual((code, body), (413, {'error': 'too large', 'limit': server.FILE_MAX, 'error_code': 'too_large'}))
