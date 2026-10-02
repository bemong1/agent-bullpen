"""Tests of how the server holds up against odd requests, odd files and odd shapes of valid JSON: the token in a log line, the file a request opens, a record folder
with a broken link in it, a list that follows the conversation and not the file time, the Host allow list of a wildcard without a token, and the cookie of a fixed token.
Every test reads a temporary folder only.

    python3 -m unittest discover -s tests
"""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
from test_access import TOKEN, request  # noqa: E402
from test_release import SID, FakeServer, Home, live_server, main_env, run_main, touch, write  # noqa: E402
from test_stage1 import FakeSession, call  # noqa: E402
from test_stage2 import iso  # noqa: E402

from board import access, catalog, codex_index, plans, util  # noqa: E402

ANY_SESSION = '&session=11111111-2222-4333-8444-555555555555'


def call_reg(path, reg, host='localhost:8790'):
    """Calls do_GET with `reg` as the registry of sessions and returns (code, body)."""
    sent = []
    h = server.Handler.__new__(server.Handler)
    h.path, h.headers = path, {'Host': host}
    h._send = lambda code, body, ctype=None: sent.append((code, body))
    with patched(REG=reg):
        h.do_GET()
    return sent[0]


class LogRedaction(unittest.TestCase):
    """The log of a first visit must not keep the token, however the key is written: the check decodes the key, so the redaction does too."""

    def test_a_key_written_with_escapes_loses_its_value(self):
        for line in ('"GET /?%74oken=SECRET HTTP/1.1" 302 -', '"GET /?a=1&%74%6F%6B%65%6E=SECRET&b=2 HTTP/1.1" 302 -', '"GET /?%54oken=SECRET HTTP/1.1" 401 -',
                     '"GET /x?q=1&to%6ben=SECRET HTTP/1.1" 302 -'):
            self.assertNotIn('SECRET', access.redact(line), line)
        self.assertEqual(access.redact('127.0.0.1 "GET /?%74oken=SECRET&x=1 HTTP/1.1" 302 -'), '127.0.0.1 "GET /?%74oken=…&x=1 HTTP/1.1" 302 -')

    def test_every_repeat_of_the_key_loses_its_value(self):
        self.assertEqual(access.redact('"GET /?token=A&session=s&%74oken=B&token=C HTTP/1.1" 302 -'), '"GET /?token=…&session=s&%74oken=…&token=… HTTP/1.1" 302 -')
        self.assertNotIn('SECRET', access.redact('"GET /?x=1;token=SECRET HTTP/1.1" 302 -'))

    def test_other_keys_are_left_alone(self):
        for line in ('"GET /api/state?session=s&tokens=1&mytoken=2&t=3 HTTP/1.1" 200 -', '"GET /api/state HTTP/1.1" 200 -', '"GET /?token HTTP/1.1" 401 -'):
            self.assertEqual(access.redact(line), line)

    def test_the_handler_logs_through_the_redaction(self):
        h = server.Handler.__new__(server.Handler)
        h.client_address = ('127.0.0.1', 5)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'AGENT_BULLPEN_LOG': '1'}), contextlib.redirect_stdout(out):
            h.log_message('"%s" %s %s', 'GET /?%74oken=SECRET HTTP/1.1', '302', '-')
        self.assertNotIn('SECRET', out.getvalue())
        self.assertIn('…', out.getvalue())

    def test_a_live_server_logs_no_token_for_an_escaped_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(os.path.realpath(tmp), 'home')
            os.makedirs(os.path.join(home, '.claude', 'projects'))
            with live_server(['--port', '0', '--token', TOKEN], home, extra_env={'AGENT_BULLPEN_LOG': '1'}) as run:
                self.assertEqual(request(run.port(), '/?%74oken=' + TOKEN)[0], 302)       # the check decodes the key, so this is a real login
        logged = [ln for ln in run.out_text.splitlines() if '"GET ' in ln]
        self.assertTrue(logged, run.out_text)
        self.assertFalse([ln for ln in logged if TOKEN in ln], logged)


class ApprovedFileOpen(unittest.TestCase):
    """/api/file opens the path that was approved. A link put in its place after the approval is not followed to another file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.approved = write(os.path.join(self.dir, 'approved.md'), 'approved text')
        self.other = write(os.path.join(self.dir, 'other.md'), 'OTHER SECRET TEXT')

    def get(self, path, before_open=None):
        class Approving(FakeSession):
            def allowed_file(self, p):
                if before_open:
                    before_open()
                return p
        return call('/api/file?path=' + quote(path) + ANY_SESSION, Approving())

    def test_an_approved_file_is_read(self):
        code, body = self.get(self.approved)
        self.assertEqual((code, body['text']), (200, 'approved text'))

    def test_a_link_in_place_of_the_file_is_not_followed(self):
        def swap():
            os.remove(self.approved)
            os.symlink(self.other, self.approved)
        code, body = self.get(self.approved, swap)
        self.assertNotIn('OTHER SECRET', json.dumps(body))
        self.assertEqual((code, body['error_code']), (404, 'open_failed'))

    def test_a_link_in_place_of_a_folder_is_not_followed(self):
        write(os.path.join(self.dir, 'one', 'a.md'), 'first')
        write(os.path.join(self.dir, 'two', 'a.md'), 'SECOND SECRET')

        def swap():
            os.rename(os.path.join(self.dir, 'one'), os.path.join(self.dir, 'one.old'))
            os.symlink(os.path.join(self.dir, 'two'), os.path.join(self.dir, 'one'))
        code, body = self.get(os.path.join(self.dir, 'one', 'a.md'), swap)
        self.assertNotIn('SECOND SECRET', json.dumps(body))
        self.assertEqual((code, body['error_code']), (404, 'open_failed'))

    def test_open_safe_with_an_approved_path_resolves_nothing(self):
        with util.open_safe(self.approved, approved=True) as f:
            self.assertEqual(f.read(), 'approved text')
        link = os.path.join(self.dir, 'link.md')
        os.symlink(self.other, link)
        os.makedirs(os.path.join(self.dir, 'sub'))
        for path in (link, os.path.join(self.dir, 'sub', '..', 'approved.md'), os.path.join(self.dir, '.', 'approved.md'), 'approved.md', self.dir + '//approved.md'):
            with self.assertRaises(OSError, msg=path):
                util.open_safe(path, approved=True)

    def test_an_approved_path_still_meets_the_deny_rules(self):
        auth = write(os.path.join(self.dir, 'credentials.md'), 'x')
        with self.assertRaises(util.Denied):
            util.open_safe(auth, approved=True)
        with patched(DENY_FILES=(self.approved,)):
            with self.assertRaises(util.Denied):
                util.open_safe(self.approved, approved=True)


class CodexIndexShapes(unittest.TestCase):
    """A rollout whose lines are valid JSON of an unexpected shape is left out. It does not stop the list, the other rollouts or the Claude sessions."""
    GOOD = '019a0001-0000-7000-8000-000000000000'

    def setUp(self):
        self.h = Home(self)
        self.n = 0

    def rollout(self, first, rest=()):
        self.n += 1
        name = 'rollout-2026-09-30T00-00-%02d-%s.jsonl' % (self.n, self.GOOD[:-1] + str(self.n % 10))
        return write(os.path.join(self.h.codex, 'sessions', '2026', '09', '30', name), '\n'.join([first] + list(rest)) + '\n')

    def meta(self, payload):
        return json.dumps({'timestamp': iso(time.time()), 'type': 'session_meta', 'payload': payload})

    def test_a_first_line_that_is_not_an_object_with_an_object_payload(self):
        for first in ('[]', 'null', '42', '"x"', '{}', '{"payload": []}', '{"payload": "x"}', '{"payload": null}', '{"payload": {"id": ["a"]}}', '{"payload": {"id": 5}}',
                      '{"payload": {"id": {"a": 1}}}'):
            self.rollout(first)
        good = self.rollout(self.meta({'id': self.GOOD, 'cwd': '/w/cx', 'timestamp': iso(time.time()), 'originator': 'codex_cli_rs', 'source': 'cli'}))
        idx = codex_index.CodexIndex()
        idx.refresh(force=True)
        self.assertEqual([e['id'] for e in idx.entries()], [self.GOOD])
        self.assertEqual(idx.get(self.GOOD)['path'], good)

    def test_fields_of_the_wrong_type_do_not_stop_the_list(self):
        self.rollout(self.meta({'id': self.GOOD, 'cwd': ['x'], 'timestamp': 5, 'parent_thread_id': ['p'], 'originator': ['o'], 'source': 'cli'}))
        idx = codex_index.CodexIndex()
        idx.refresh(force=True)
        for e in idx.entries():
            self.assertIsInstance(e['id'], str)
            self.assertIsInstance(e['cwd'], str)
            self.assertTrue(e['parent'] is None or isinstance(e['parent'], str))
        with patched(CODEX=idx):
            ss, counts = catalog.scan_sessions()
        self.assertEqual(counts['claude'], 0)

    def test_the_other_rollouts_and_the_claude_list_are_still_served(self):
        self.h.session(SID[0], t=time.time() - 10)
        self.rollout('[]')
        self.rollout(self.meta([]))
        self.rollout(self.meta({'id': self.GOOD, 'cwd': '/w/cx', 'timestamp': iso(time.time()), 'originator': 'codex_cli_rs', 'source': 'cli'}))
        idx = codex_index.CodexIndex()
        with patched(CODEX=idx):
            ss, counts = catalog.scan_sessions()
        self.assertEqual(sorted(s['id'] for s in ss), sorted([SID[0], self.GOOD]))
        self.assertEqual((counts['claude'], counts['codex']), (1, 1))

    def test_names_of_another_shape_are_skipped(self):
        self.rollout(self.meta({'id': self.GOOD, 'cwd': '/w/cx', 'originator': 'codex_cli_rs'}))
        write(os.path.join(self.h.codex, 'session_index.jsonl'), '\n'.join(['[]', '5', 'null', '{"id": ["x"], "thread_name": "n"}', '{"id": "a", "thread_name": 7}',
                                                                         json.dumps({'id': self.GOOD, 'thread_name': 'A name'})]) + '\n')
        idx = codex_index.CodexIndex()
        idx.refresh(force=True)
        self.assertEqual(idx.title(idx.get(self.GOOD)), 'A name')

    def test_a_file_that_is_not_read_is_not_read_again_until_it_changes(self):
        bad = self.rollout('[]')
        idx = codex_index.CodexIndex()
        with mock.patch.object(idx, '_new', wraps=idx._new) as new:
            idx.refresh(force=True)
            idx.refresh(force=True)
            self.assertEqual(new.call_count, 1)
            with open(bad, 'a') as f:
                f.write(self.meta({'id': self.GOOD}) + '\n')
            os.utime(bad, (time.time() + 5, time.time() + 5))
            idx.refresh(force=True)
            self.assertEqual(new.call_count, 2)

    def test_limits_of_another_shape_do_not_stop_the_plan(self):
        shapes = ({'primary': []}, {'primary': 'x'}, {'primary': {'used_percent': 5, 'resets_at': 'tomorrow'}},
                  {'primary': {'used_percent': 5, 'resets_at': 1e12}, 'secondary': []}, {'primary': {'used_percent': 5, 'resets_at': 5}, 'secondary': 'x', 'credits': []},
                  [], 'x')
        for n, rl in enumerate(shapes):
            self.rollout(self.meta({'id': self.GOOD[:-2] + '%02d' % n, 'cwd': '/w/cx', 'originator': 'codex_cli_rs'}),
                         [json.dumps({'timestamp': iso(time.time()), 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': rl}}, separators=(',', ':'))])
        idx = codex_index.CodexIndex()
        idx.refresh(force=True)
        self.assertTrue(idx.limit() is None or isinstance(idx.limit(), dict))
        with patched(CODEX=idx):
            self.assertIn('codex', plans.plan_status())


class ClaudeJsonShapes(unittest.TestCase):
    """~/.claude.json holds valid JSON of an unexpected shape: the plan bar and the usage read go on without it."""

    def setUp(self):
        self.h = Home(self)
        self.path = os.path.join(self.h.home, '.claude.json')
        cache = mock.patch.dict(plans._CL_CONF, {'key': None, 'v': None})
        cache.start()
        self.addCleanup(cache.stop)

    def plan(self, doc):
        write(self.path, doc if isinstance(doc, str) else json.dumps(doc))
        plans._CL_CONF.update(key=None, v=None)
        return plans.claude_plan()

    def test_a_document_that_is_not_an_object(self):
        for doc in ('[]', 'null', '42', '"x"', 'true'):
            self.assertIsNone(self.plan(doc), doc)
            self.assertEqual(plans.plan_status()['claude'], None, doc)

    def test_a_field_of_another_shape_is_left_out(self):
        for doc in ({'oauthAccount': []}, {'oauthAccount': {'userRateLimitTier': 5}}, {'cachedUsageUtilization': []}, {'cachedUsageUtilization': {'utilization': []}},
                    {'cachedUsageUtilization': {'utilization': {'five_hour': 3, 'seven_day': [], 'extra_usage': 'x', 'limits': 5}}},
                    {'cachedUsageUtilization': {'utilization': {'limits': [1, 'x', None, {'kind': 'weekly_scoped', 'scope': []}, {'kind': 'weekly_scoped', 'scope': {'model': 4}}]}}},
                    {'cachedUsageUtilization': {'fetchedAtMs': 'soon', 'utilization': {}}}, {'cachedUsageUtilization': {'fetchedAtMs': [1]}},
                    {'cachedUsageUtilization': {'utilization': {'five_hour': {'utilization': 3, 'resets_at': 1790000000}, 'limits': [{'kind': 'weekly_scoped', 'resets_at': [1]}]}}}):
            got = self.plan(doc)
            self.assertTrue(got is None or isinstance(got, dict), doc)

    def test_a_good_part_is_kept_beside_a_bad_one(self):
        got = self.plan({'oauthAccount': {'userRateLimitTier': 'default_claude_max_5x'}, 'cachedUsageUtilization': []})
        self.assertEqual(got['plan'], 'Max 5x')
        got = self.plan({'oauthAccount': 5, 'cachedUsageUtilization': {'fetchedAtMs': 1_800_000_000_000,
                                                                       'utilization': {'five_hour': {'utilization': 12, 'resets_at': '2099-01-01T00:00:00Z'}}}})
        self.assertEqual(got['plan'], '')
        self.assertEqual(got['five_hour']['percent'], 12)


class ScanKeepsGoing(unittest.TestCase):
    """One record that cannot be looked at does not take the list down: a link that leads nowhere, a link that loops, a folder with a record's name, a file that vanished, a file that cannot be read."""

    def setUp(self):
        self.h = Home(self)
        self.now = time.time()

    def folder(self, path):
        return os.path.dirname(path)

    def test_a_dangling_link_a_loop_and_a_folder_are_not_sessions(self):
        good = self.h.session(SID[0], t=self.now - 10)
        d = self.folder(good)
        os.symlink(os.path.join(d, 'nowhere.jsonl'), os.path.join(d, SID[1] + '.jsonl'))
        os.symlink(os.path.join(d, SID[2] + '.jsonl'), os.path.join(d, SID[2] + '.jsonl'))
        os.mkdir(os.path.join(d, SID[3] + '.jsonl'))
        ss, counts = catalog.scan_sessions()
        self.assertEqual([s['id'] for s in ss], [SID[0]])
        self.assertEqual((counts['claude'], counts['agent_sessions']), (1, 0))

    def test_a_subagent_record_that_cannot_be_looked_at_is_left_out(self):
        p = self.h.session(SID[0], t=self.now - 10, agents=2)
        sub = os.path.join(self.folder(p), SID[0], 'subagents')
        os.symlink(os.path.join(sub, 'gone.jsonl'), os.path.join(sub, 'agent-a0000000000000099.jsonl'))
        os.symlink(os.path.join(sub, 'agent-a0000000000000098.jsonl'), os.path.join(sub, 'agent-a0000000000000098.jsonl'))
        ss, _ = catalog.scan_sessions()
        self.assertEqual([(s['id'], s['agents']) for s in ss], [(SID[0], 2)])

    def test_a_file_that_vanishes_between_the_listing_and_the_stat(self):
        a = self.h.session(SID[0], t=self.now - 10)
        b = self.h.session(SID[1], t=self.now - 20)
        real = os.stat

        def stat(path, *args, **kw):
            if os.fspath(path) == a:
                raise FileNotFoundError(2, 'No such file or directory', a)
            return real(path, *args, **kw)
        with mock.patch.object(os, 'stat', stat):
            ss, counts = catalog.scan_sessions()
        self.assertEqual([s['id'] for s in ss], [SID[1]])
        self.assertEqual(counts['claude'], 1)
        self.assertTrue(os.path.exists(b))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read everything')
    def test_a_record_that_cannot_be_read_is_not_listed_and_opening_it_is_a_403(self):
        p = self.h.session(SID[0], t=self.now - 5)
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o600)
        self.h.session(SID[1], t=self.now - 100)
        ss, counts = catalog.scan_sessions()
        self.assertEqual(([s['id'] for s in ss], counts['claude']), ([SID[1]], 1))
        projs = catalog.session_projects(ss)
        self.assertEqual(catalog.default_session(projs, ss), SID[1])
        code, body = call_reg('/api/state?session=' + SID[0], catalog.Registry())
        self.assertEqual((code, body['error_code']), (403, 'unreadable'))

    def test_a_folder_with_a_records_name_is_not_a_session_to_open(self):
        d = os.path.join(self.h.claude, 'projects', '-w-app')
        os.makedirs(os.path.join(d, SID[0] + '.jsonl'))
        self.assertIsNone(catalog.Registry().get(SID[0]))


class RequestsThatFail(unittest.TestCase):
    """What the handler answers when opening a session fails, or when anything it does raises."""
    PATH = '/api/state?session=' + SID[0]

    def reg_raising(self, exc):
        def get(sid):
            raise exc
        return types.SimpleNamespace(get=get, loop=lambda: None)

    def test_a_session_that_cannot_be_opened_is_a_403_or_a_404_with_a_code(self):
        for exc, want in ((PermissionError(13, 'Permission denied'), 403), (FileNotFoundError(2, 'No such file'), 404), (OSError(5, 'Input/output error'), 404)):
            code, body = call_reg(self.PATH, self.reg_raising(exc))
            self.assertEqual((code, body['error_code'], body['session']), (want, 'unreadable', SID[0]), repr(exc))
            self.assertNotIn('Permission', json.dumps(body))                           # no system message, no path

    def test_an_unexpected_exception_is_a_500_json(self):
        s = FakeSession()
        s.state = lambda: {}['no such key']
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, body = call(self.PATH + '&secret=1', s)
        self.assertEqual((code, body), (500, {'error': 'internal error', 'error_code': 'internal_error'}))
        self.assertEqual(out.getvalue(), 'request error KeyError /api/state\n')            # the kind of failure and the path: no query, no text of the failure

    def test_the_connection_is_answered_not_dropped(self):
        srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        s = FakeSession()
        s.state = lambda: {}['no such key']
        err = io.StringIO()
        with patched(REG=types.SimpleNamespace(get=lambda sid: s)), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=15)
            c.request('GET', self.PATH, headers={'Host': 'localhost'})
            r = c.getresponse()
            body = json.loads(r.read())
            c.request('GET', '/api/i18n', headers={'Host': 'localhost'})         # the same connection goes on being served
            second = c.getresponse()
            second.read()
            c.close()
        self.assertEqual((r.status, body['error_code']), (500, 'internal_error'))
        self.assertEqual(second.status, 200)
        self.assertNotIn('Traceback', err.getvalue())

    def test_the_start_goes_on_when_the_default_session_cannot_be_opened(self):
        h = Home(self)
        h.session(SID[0], t=time.time() - 10)
        with main_env(self, h, run_inline=False), patched(REG=self.reg_raising(PermissionError(13, 'denied'))):
            out, err, code = run_main(['--port', '0'], servers=[FakeServer()], lang='en')
            self.assertEqual((code, err), (None, ''))
            out, err, code = run_main(['--port', '0', '--session', SID[0]], servers=[FakeServer()], lang='en')
            self.assertEqual(code, 2)
            self.assertIn(SID[0], err)


class RecencyFollowsTheConversation(unittest.TestCase):
    """Which session opens first, and the order of the project list, follow the last talk in the record, not the time the file was touched
    (a live Claude Code appends bookkeeping lines with no talk in them)."""

    def setUp(self):
        self.h = Home(self)
        self.now = time.time()

    def ledger(self, path, t):
        with open(path, 'a') as f:
            f.write('{"type":"artifact-autoreact-ledger","entries":[]}\n')
        touch(path, t)

    def pick(self, ss):
        with patched(claude_alive_ids=lambda: {s['id'] for s in ss}):
            projs = catalog.session_projects(ss)
        return projs, catalog.default_session(projs, ss)

    def test_a_bookkeeping_line_does_not_make_an_old_session_the_newest(self):
        old = self.h.session(SID[0], proj='-w-old', cwd='/w/old', t=self.now - 15 * 86400, agents=1)
        self.ledger(old, self.now - 40)
        self.h.session(SID[1], proj='-w-new', cwd='/w/new', t=self.now - 360, agents=1)
        ss, _ = catalog.scan_sessions()
        projs, default = self.pick(ss)
        self.assertEqual([p['name'] for p in projs], ['new', 'old'])
        self.assertEqual(default, SID[1])

    def test_the_item_carries_the_time_of_the_last_talk(self):
        p = self.h.session(SID[0], t=self.now - 3000)
        self.ledger(p, self.now - 5)
        item = catalog.scan_sessions()[0][0]
        self.assertAlmostEqual(item['mtime'], self.now - 3000, delta=2)
        self.assertEqual(item['active'], 0)                          # a bookkeeping line is not activity

    def test_the_last_talk_is_the_last_user_or_assistant_line(self):
        p = self.h.session(SID[0], t=self.now - 3000)
        with open(p, 'a') as f:
            f.write(json.dumps({'type': 'assistant', 'timestamp': iso(self.now - 90), 'message': {'role': 'assistant', 'content': []}}) + '\n')
            f.write('{"type":"last-prompt","lastPrompt":"x"}\n')
        self.ledger(p, self.now - 5)
        item = catalog.scan_sessions()[0][0]
        self.assertAlmostEqual(item['mtime'], self.now - 90, delta=2)
        self.assertEqual(item['active'], 1)

    def test_a_talk_line_with_a_timestamp_that_is_not_text_is_passed_over(self):
        p = self.h.session(SID[0], t=self.now - 500)
        with open(p, 'a') as f:
            for stamp in (5, [1], None, {'a': 1}):
                f.write(json.dumps({'type': 'assistant', 'timestamp': stamp, 'message': {'role': 'assistant', 'content': []}}) + '\n')
        item = catalog.scan_sessions()[0][0]
        self.assertAlmostEqual(item['mtime'], self.now - 500, delta=2)

    def test_without_any_talk_the_file_time_is_used(self):
        p = write(os.path.join(self.h.claude, 'projects', '-w-app', SID[0] + '.jsonl'), '{"type":"ai-title","aiTitle":"x"}\n')
        touch(p, self.now - 700)
        item = catalog.scan_sessions()[0][0]
        self.assertAlmostEqual(item['mtime'], self.now - 700, delta=2)

    def test_the_newest_subagent_record_counts_by_its_last_line(self):
        p = self.h.session(SID[0], t=self.now - 9 * 86400, agents=2)
        sub = os.path.join(os.path.dirname(p), SID[0], 'subagents')
        newest = os.path.join(sub, 'agent-a0000000000000001.jsonl')
        write(newest, json.dumps({'type': 'assistant', 'timestamp': iso(self.now - 200), 'message': {'role': 'assistant', 'content': []}}) + '\n')
        touch(newest, self.now - 3)
        item = catalog.scan_sessions()[0][0]
        self.assertAlmostEqual(item['agents_mtime'], self.now - 200, delta=2)
        self.assertEqual(item['active'], 1)                          # the file itself grew just now: that count is as before

    def test_a_long_last_line_does_not_hide_the_talk_before_it(self):
        p = self.h.session(SID[0], t=self.now - 1200)
        with open(p, 'a') as f:
            f.write('{"type":"attachment","blob":"' + 'x' * (1 << 20) + '"}\n')
        touch(p, self.now - 4)
        item = catalog.scan_sessions()[0][0]
        self.assertAlmostEqual(item['mtime'], self.now - 1200, delta=2)


class WildcardWithoutAToken(unittest.TestCase):
    """--host 0.0.0.0 --no-auth prints this machine's addresses; the server lets those names in, or it would refuse the very address it printed."""

    def setUp(self):
        self.h = Home(self)

    def start(self, *argv):
        with mock.patch.object(access, 'local_addresses', lambda: ['192.168.0.5', '100.64.0.2', '10.0.0.9', '10.0.0.10', '10.0.0.11']), \
                main_env(self, self.h, run_inline=False):
            out, err, code = run_main(['--port', '0'] + list(argv), servers=[FakeServer()], lang='en')
            return out, err, code, [server.host_allowed(h) for h in ('192.168.0.5:8790', '100.64.0.2', '10.0.0.11:9', '192.168.0.6', 'evil.test', '10.0.0.12')]

    def test_every_address_of_this_machine_is_let_in(self):
        out, err, code, allowed = self.start('--host', '0.0.0.0', '--no-auth')
        self.assertEqual(code, None)
        self.assertIn('Dashboard: http://192.168.0.5:8790/', out)
        self.assertEqual(allowed, [True, True, True, False, False, False])

    def test_the_same_for_the_ipv6_wildcard(self):
        self.assertEqual(self.start('--host', '::', '--no-auth')[3], [True, True, True, False, False, False])

    def test_a_listener_with_a_token_changes_nothing_else(self):
        self.assertEqual(self.start('--host', '127.0.0.1')[3], [False] * 6)
        self.assertEqual(self.start('--host', '192.168.0.5', '--no-auth')[3], [True, False, False, False, False, False])


class HostHintForAnAddress(unittest.TestCase):
    """The 403 text offers a suffix only for a name that has one."""

    def test_an_ip_address_gets_no_suffix_advice(self):
        for header, name in (('192.0.2.28:8790', '192.0.2.28'), ('100.64.0.153', '100.64.0.153'), ('[fd7a:115c:a1e0:0:0:0:0:1]:8790', 'fd7a:115c:a1e0::1'), ('10.0.0.9', '10.0.0.9')):
            hint = server.host_hint(header)
            self.assertIn('--allow-host ' + name, hint)
            self.assertNotRegex(hint.lower(), r'suffix|접미사')
            self.assertNotIn('--allow-host .', hint)
            self.assertEqual(len(hint.splitlines()), 2, hint)

    def test_a_name_with_a_suffix_still_gets_it(self):
        hint = server.host_hint('box.tail1234.ts.net:8790')
        self.assertIn('--allow-host box.tail1234.ts.net', hint)
        self.assertIn('--allow-host .tail1234.ts.net', hint)

    def test_the_text_of_an_odd_header_is_not_echoed(self):
        self.assertNotIn('evil', server.host_hint('evil@host'))


class CookieOfAFixedToken(unittest.TestCase):
    """The cookie of a start is good for that start only: a copied cookie dies when the server is started again, even with the same fixed token."""

    def test_a_cookie_does_not_outlive_its_start(self):
        first, second = access.Gate(TOKEN), access.Gate(TOKEN)
        cookie = first.cookie_header(8790).split(';')[0]
        self.assertEqual(first.judge(cookie, None, '', 8790), (access.OK, None))
        self.assertEqual(second.judge(cookie, None, '', 8790), (access.DENY, None))

    def test_the_token_still_logs_in_after_a_restart(self):
        second = access.Gate(TOKEN)
        self.assertEqual(second.judge(None, None, 'token=' + TOKEN, 8790)[0], access.LOGIN)
        self.assertEqual(second.judge(None, 'Bearer ' + TOKEN, '', 8790), (access.OK, None))
        cookie = second.cookie_header(8790).split(';')[0]
        self.assertEqual(second.judge(cookie, None, '', 8790), (access.OK, None))

    def test_the_cookie_is_the_same_within_one_start_and_keeps_its_shape(self):
        g = access.Gate(TOKEN)
        self.assertEqual(g.cookie_header(8790), g.cookie_header(8790))
        self.assertRegex(g.cookie_header(8790), r'agent_bullpen_8790=[0-9a-f]{64}; Path=/; HttpOnly; SameSite=Strict')
        self.assertNotIn(TOKEN, g.cookie_header(8790))
        self.assertNotEqual(access.Gate(TOKEN).cookie_header(8790), g.cookie_header(8790))


if __name__ == '__main__':
    unittest.main()
