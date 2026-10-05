"""Server-side edge-case tests: nested shells and command-position detection, `claude -p` candidates, bulk reading of a big transcript, IPv6, opening documents (links), etc.

    python3 -m unittest discover -s tests
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server, start_patches, terminal_lang  # noqa: E402
from test_stage2 import T0, CliFixture, bash_line, child_lines, result_line  # noqa: E402

U = '019a0000-0000-7000-8000-000000000000'


def launches(cmd):
    return bool(server.LAUNCH_RE.search(server.shell_code(cmd)))


class F01NestedShell(unittest.TestCase):
    """Quotes and heredocs inside a nested shell (bash -c, sh -c, eval), and escaped characters, are not treated as executable code."""

    def test_inner_quote_is_text(self):
        self.assertFalse(launches("bash -c 'echo \"(codex exec resume %s fake)\"'" % U))
        self.assertFalse(launches('bash -c "echo \\"codex exec y\\""'))
        self.assertFalse(launches("eval 'echo \"; codex exec z\"'"))
        self.assertFalse(launches("sh -c 'echo \\'x\\'; printf \"%s\" \"; codex exec q\"'"))

    def test_inner_heredoc_is_text(self):
        self.assertFalse(launches("bash -c 'cat <<EOF\ncodex exec x\nEOF'"))
        self.assertFalse(launches("bash -c 'cat <<\"EOF\"\n; codex exec x\nEOF\n'"))

    def test_inner_comment_is_text(self):
        self.assertFalse(launches("bash -c 'true # ; codex exec x'"))

    def test_escaped_chars_are_text(self):
        self.assertFalse(launches('echo \\(codex exec resume %s fake\\)' % U))
        self.assertFalse(launches(r'echo x \; codex exec y'))

    def test_real_inner_commands_still_launch(self):
        self.assertTrue(launches("bash -c 'cd /a && codex exec x'"))
        self.assertTrue(launches('sh -c "cd /a && codex exec \\"$Q\\""'))
        self.assertTrue(launches("eval 'true; codex exec x'"))
        self.assertTrue(launches("bash -c 'echo hi; codex exec \"$Q\"'"))
        self.assertTrue(launches("cd /x && \\\ncodex exec go"))            # line continuation
        self.assertTrue(launches('echo "$(codex exec yes)"'))

    def test_cwd_inside_nested_shell(self):
        L = server.cx_launches('sh -c "true; cd /a && codex exec \\"$Q\\""', {}, '/w')
        self.assertEqual(L[0]['scwd'], '/a')

    def test_length_is_kept(self):
        for c in ("bash -c 'a \"b\" c'", 'sh -c "x \\"y\\" \\\\ z"', "eval 'cat <<E\nx\nE\n'", 'echo \\( \\" \\\\'):
            self.assertEqual(len(server.shell_code(c)), len(c), c)

    def test_unparsable_is_not_evidence(self):
        with mock.patch.dict(server.shell_code.__globals__, {'_shell_code': mock.Mock(side_effect=IndexError('boom'))}):
            code = server.shell_code('codex exec x\necho hi')
        self.assertEqual(code, 'x' * 12 + '\n' + 'x' * 7)          # everything is masked (length and newlines are kept)
        self.assertFalse(server.LAUNCH_RE.search(code))

    def test_parse_call_does_not_claim_fake_launch(self):
        c = server.cx_parse_call(1.0, 't', {'command': "bash -c 'echo \"(codex exec resume %s fake)\"'" % U}, '/w')
        self.assertIsNotNone(c)                    # it names a thread id, so the call is kept
        self.assertFalse(c['launch'])
        self.assertEqual(c['L'], [])


class F04RelativeCd(unittest.TestCase):
    """cd is followed in order, relative to the shell's working folder. Whitespace at the start of a line is also taken into account."""

    def test_relative_cd_then_relative_C(self):
        L = server.cx_launches('cd subdir && codex exec -C . hi', {}, '/work')
        self.assertEqual((L[0]['scwd'], L[0]['cwd']), ('/work/subdir', '/work/subdir'))

    def test_cd_chain(self):
        L = server.cx_launches('cd /a && cd b && cd ../c && codex exec x', {}, '/w')
        self.assertEqual((L[0]['scwd'], L[0]['cwd']), ('/a/c', '/a/c'))
        L = server.cx_launches('cd a; cd /z; cd y; codex exec x', {}, '/w')
        self.assertEqual(L[0]['scwd'], '/z/y')

    def test_indented_line_head(self):
        L = server.cx_launches('echo go\n  cd /new\n  codex exec x', {}, '/work')
        self.assertEqual(L[0]['scwd'], '/new')

    def test_unknown_stays_unknown(self):
        L = server.cx_launches('cd $X && codex exec x', {}, '/w')
        self.assertIsNone(L[0]['scwd'])
        L = server.cx_launches('cd - && codex exec x', {}, '/w')
        self.assertIsNone(L[0]['scwd'])
        L = server.cx_launches('cd sub && codex exec x', {}, None)          # a relative cd when the preceding working folder is unknown
        self.assertIsNone(L[0]['scwd'])

    def test_no_cd_keeps_base(self):
        L = server.cx_launches('codex exec x', {}, '/w')
        self.assertEqual(L[0]['scwd'], '/w')

    def test_home_cd(self):
        L = server.cx_launches('cd ~ && codex exec x', {}, '/w')
        self.assertEqual(L[0]['scwd'], os.path.expanduser('~'))

    def test_uuid_re_is_sid_re(self):
        self.assertIs(server.shell_code.__globals__['SID_RE'], server.SID_RE)
        self.assertNotIn('UUID_RE', server.shell_code.__globals__)


class F02CliCandidates(CliFixture):
    """`claude -p` candidates are looked for only in the code left after removing heredocs, quoted bodies and comments (everything in an executing position is a candidate)."""
    C1, C2 = '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333'

    def cands(self, cmd, cwd='/w'):
        f = {'pos': 0, 'sid': self.PARENT, 'calls': [], 'cli': []}
        self.links._cli_line(f, bash_line(T0, cmd, cwd).encode())
        return f['cli']

    def test_text_is_not_a_launch(self):
        self.assertEqual(self.cands("cat <<'EOF'\nclaude -p hello\nEOF"), [])
        self.assertEqual(self.cands('echo "claude -p x"'), [])
        self.assertEqual(self.cands("echo 'cd /a; claude -p x'"), [])
        self.assertEqual(self.cands('true # claude -p x'), [])
        self.assertEqual(self.cands("python3 - <<'PY'\nprint('claude -p x')\nPY"), [])

    def test_doc_and_real_together(self):
        got = self.cands("cat <<'EOF'\n`claude -p` 글\nEOF\ncd /real && claude -p go")
        self.assertEqual([c['cwd'] for c in got], ['/real'])
        got = self.cands("cd /a && claude -p one\ncat <<'EOF'\nclaude -p doc\nEOF\ncd /b && claude -p two")
        self.assertEqual([c['cwd'] for c in got], ['/a', '/b'])            # everything in an executing position is a candidate

    def test_unrelated_concurrent_session_is_not_adopted(self):
        cmd = "cat <<'EOF'\nclaude -p 설명\nEOF\ncd /real && claude -p hi"
        self.write(self.PARENT, [bash_line(T0, cmd, cwd='/other')])
        self.write(self.C2, child_lines(T0 + 3, cwd='/other'))            # an unrelated session whose folder and time match the document candidate
        self.write(self.C1, child_lines(T0 + 5, cwd='/real'))             # the real child
        own = self.scan()
        self.assertEqual(set(own), {self.C1})

    def test_only_doc_candidate_links_nothing(self):
        self.write(self.PARENT, [bash_line(T0, "cat <<'EOF'\nclaude -p 설명\nEOF", cwd='/w')])
        self.write(self.C1, child_lines(T0 + 3, cwd='/w'))
        self.assertEqual(self.scan(), {})

    def test_relative_cd_for_claude_p(self):
        got = self.cands('cd sub && claude -p go', cwd='/w')
        self.assertEqual(got[0]['cwd'], '/w/sub')


class F03BulkTurns(unittest.TestCase):
    """Trimming the kept turns to 200 is done after the events are emitted. Reading 205 turns at once (the initial read) or one turn at a time gives the same events.
    The events of every turn come out (the events of the first 5 turns are not cut off)."""
    TID = '019a0000-0000-7000-8000-0000000000cc'
    EXPECT = {'spawn': 1, 'orch_msg': 204, 'handback': 205, 'notify': 205}

    def turn_lines(self, k):
        t = 1790726400.0 + k * 10
        ts = lambda x: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(x))
        return ['{"timestamp":"%s","type":"event_msg","payload":{"type":"task_started"}}' % ts(t),
                '{"timestamp":"%s","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"go %d"}]}}' % (ts(t + 1), k),
                '{"timestamp":"%s","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"done %d"}}' % (ts(t + 5), k)]

    def setup(self, d):
        path = os.path.join(d, 'rollout-x-%s.jsonl' % self.TID)
        with open(path, 'w') as f:
            f.write('{"timestamp":"2026-09-30T00:00:00.000Z","type":"session_meta","payload":{"id":"%s","cwd":"/w"}}\n' % self.TID)
        e = {'id': self.TID, 'path': path, 'cwd': '/w', 'meta_ts': 1790726400.0, 'model': 'gpt-6.1-sol'}
        info = {'sid': 'sidX', 'rule': 'prompt', 'call': None, 'bash_ts': 1790726399.0, 'bash_desc': '', 'dt': 1.0, 'cwd': '/w',
                'cwd_ok': True, 'prompt_ok': True}
        sess = types.SimpleNamespace(id='sidX', agents={}, feed=[], launcher_of=lambda a: 'orch')
        sess._event = lambda ts, kind, frm, to, title, text='', agent=None, extra=None, **kw: sess.feed.append({'ts': ts, 'kind': kind, 'agent': agent, 'text': text})
        ready = threading.Event()
        ready.set()
        for p in (mock.patch.object(server.LINKS, 'ready', ready), mock.patch.object(server.LINKS, 'owners', {self.TID: info}),
                  mock.patch.object(server.CODEX, 'by_id', {self.TID: e})):
            p.start()
            self.addCleanup(p.stop)
        return path, sess, server.CodexLinker(sess)

    def kinds(self, sess):
        out = {}
        for e in sess.feed:
            out[e['kind']] = out.get(e['kind'], 0) + 1
        return out

    def write(self, path, ks):
        with open(path, 'a') as f:
            for k in ks:
                f.write('\n'.join(self.turn_lines(k)) + '\n')

    def test_bulk_205(self):
        with tempfile.TemporaryDirectory() as d:
            path, sess, lk = self.setup(d)
            self.write(path, range(205))
            lk.poll()                                               # the first poll reads all 205 turns at once
            a = lk.agents[self.TID]
            self.assertEqual(self.kinds(sess), self.EXPECT)
            self.assertEqual(len(a.turns), 200)                     # the turns kept for the details are still 200
            self.assertEqual((a.turns[0]['n'], a.turns[-1]['n'], a.turn_seq), (5, 204, 205))
            self.assertEqual(len(a.notifications), 205)
            self.assertEqual(sess.feed[0]['kind'], 'spawn')
            n = len(sess.feed)
            lk.poll()
            self.assertEqual(len(sess.feed), n)                     # unchanged when repeated

    def test_bulk_equals_incremental(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            p1, s1, l1 = self.setup(d1)
            self.write(p1, range(205))
            l1.poll()
            p2, s2, l2 = self.setup(d2)
            for k in range(205):
                self.write(p2, [k])
                l2.poll()
            self.assertEqual(self.kinds(s1), self.kinds(s2))
            self.assertEqual([(e['kind'], e['text']) for e in s1.feed], [(e['kind'], e['text']) for e in s2.feed])

    def test_bulk_then_more(self):
        with tempfile.TemporaryDirectory() as d:
            path, sess, lk = self.setup(d)
            self.write(path, range(150))
            lk.poll()
            self.write(path, range(150, 230))                       # then 80 more turns at once
            lk.poll()
            self.assertEqual(self.kinds(sess), {'spawn': 1, 'orch_msg': 229, 'handback': 230, 'notify': 230})
            self.assertEqual(len(lk.agents[self.TID].turns), 200)


class F05EntrySnapshot(unittest.TestCase):
    """The entries CodexIndex hands to a reader are copies, down to the inner mutable values (turns, thread_total, limit)."""

    def index(self):
        idx = server.CodexIndex()
        e = {'id': 'i1', 'parent': 'p0', 'turns': [{'start': 1, 'user': 'a'}], 'thread_total': {'input_tokens': 5},
             'limit': {'ts': 1, 'primary': {'used_percent': 3}, 'credits': {'balance': 1}}, 'first_user': 'x'}
        idx.files['/p'] = e
        idx.by_id['i1'] = e
        return idx, e

    def test_reader_changes_do_not_reach_the_index(self):
        for read in (lambda i: i.entries()[0], lambda i: i.get('i1'), lambda i: i.children('p0')[0]):
            idx, e = self.index()
            got = read(idx)
            got['turns'][0]['user'] = 'changed'
            got['thread_total']['input_tokens'] = 99
            got['limit']['primary']['used_percent'] = 50
            got['limit']['credits']['balance'] = 0
            self.assertEqual(e['turns'][0]['user'], 'a')
            self.assertEqual(e['thread_total'], {'input_tokens': 5})
            self.assertEqual(e['limit']['primary'], {'used_percent': 3})
            self.assertEqual(e['limit']['credits'], {'balance': 1})

    def test_scan_changes_do_not_reach_the_reader(self):
        idx, e = self.index()
        got = idx.entries()[0]
        e['turns'][0]['user'] = 'scan'
        e['turns'].append({'start': 2, 'user': None})
        e['thread_total']['input_tokens'] = 7
        e['limit']['primary']['used_percent'] = 9
        self.assertEqual((got['turns'], got['thread_total'], got['limit']['primary']),
                         ([{'start': 1, 'user': 'a'}], {'input_tokens': 5}, {'used_percent': 3}))

    def test_get_unknown_is_none_and_none_values_stay(self):
        idx, e = self.index()
        self.assertIsNone(idx.get('nope'))
        e['thread_total'] = None
        e['limit'] = None
        got = idx.entries()[0]
        self.assertIsNone(got['thread_total'])
        self.assertIsNone(got['limit'])


class F06CliOwnershipChanges(CliFixture):
    """When ownership of a claude -p child is cancelled or reassigned, it drops out of that session's agents (events already emitted and idx stay as they are; if it is owned again it is restored)."""
    C1 = '22222222-2222-4222-8222-222222222222'
    OTHER = '44444444-4444-4444-8444-444444444444'

    def open(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p hi')])
        self.write(self.C1, child_lines(T0 + 5))
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        return s

    def spawns(self, s):
        return [e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.C1]

    def test_cancel_and_restore(self):
        s = self.open()
        a = s.agents[self.C1]
        n_feed = len(s.feed)
        owners = dict(self.links.cli_owners)
        self.links.cli_owners = {}                                # ownership cancelled
        self.assertTrue(s.poll())
        self.assertNotIn(self.C1, s.agents)
        self.assertNotIn(self.C1, s.agent_tails)
        self.assertEqual(len(s.feed), n_feed)                     # events already emitted stay
        self.assertFalse(s.poll())                                # no change if nothing changed
        with patched(claude_alive_ids=lambda: set()):
            self.assertNotIn(self.C1, [x['id'] for x in s.state()['agents']])
        self.links.cli_owners = owners                            # owned again
        self.assertTrue(s.poll())
        self.assertIs(s.agents[self.C1], a)                       # the same agent
        self.assertEqual(len(self.spawns(s)), 1)                  # the spawn event is not emitted again
        self.assertEqual(len(s.feed), n_feed)

    def test_reassigned_to_another_parent(self):
        s = self.open()
        other = server.Session(os.path.join(self.proj, self.OTHER + '.jsonl'))
        self.links.cli_owners = {self.C1: dict(self.links.cli_owners[self.C1], sid=self.OTHER)}
        s.poll()
        other.poll()
        self.assertNotIn(self.C1, s.agents)
        self.assertIn(self.C1, other.agents)
        self.assertEqual(len(self.spawns(other)), 1)
        self.assertEqual(len(self.spawns(s)), 1)                  # the event emitted to the previous parent stays

    def test_unchanged_ownership_changes_nothing(self):
        s = self.open()
        a = s.agents[self.C1]
        n_feed = len(s.feed)
        s.poll()
        s.poll()
        self.assertIs(s.agents[self.C1], a)
        self.assertEqual(len(s.feed), n_feed)


class F07RegistryFailure(unittest.TestCase):
    """A session whose first read failed is not registered. The next request opens it again."""
    SID = '55555555-5555-4555-8555-555555555555'

    def test_failed_open_then_retry(self):
        reg = server.Registry()
        tries = []

        class Flaky:
            provider = 'claude'
            tail = types.SimpleNamespace(max_read=None, behind=lambda: False)
            codex = None

            def __init__(self, path):
                self.path = path

            def poll(self):
                tries.append(1)
                if len(tries) == 1:
                    raise OSError('boom')

        ready = threading.Event()
        ready.set()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        record = os.path.join(tmp.name, '%s.jsonl' % self.SID)           # a record that is there: only a regular file is opened as a session
        open(record, 'w').close()
        with patched(Session=Flaky), mock.patch.object(server.LINKS, 'ready', ready), \
                mock.patch('glob.glob', lambda pat: [record]):
            with self.assertRaises(OSError):
                reg.get(self.SID)
            self.assertEqual(reg.sessions, {})
            self.assertEqual(reg.loading, {})
            s = reg.get(self.SID)                                  # the next request opens it anew
            self.assertIs(reg.sessions[self.SID], s)
            self.assertIs(reg.get(self.SID), s)
        self.assertEqual(len(tries), 2)


class R17HostParsing(unittest.TestCase):
    """Only a numeric port is accepted after the colon in Host."""

    def test_names(self):
        for h, name in (('localhost', 'localhost'), ('LOCALHOST.:80', 'localhost'), ('127.0.0.1:8790', '127.0.0.1'),
                        ('[::1]:8790', '::1'), ('[::1]', '::1'), ('box.example.net:1', 'box.example.net')):
            self.assertEqual(server.host_name(h), name, h)

    def test_weird_shapes_are_empty(self):
        for h in ('127.0.0.1:80@evil.test', 'localhost@evil.test', 'localhost:abc', 'localhost:', 'localhost:80:90', '::1', '[::1',
                  'a b', 'localhost/x', 'localhost?x', '', None, ':80'):
            self.assertEqual(server.host_name(h), '', repr(h))
            self.assertFalse(server.host_allowed(h), repr(h))

    def test_still_allowed(self):
        for h in ('localhost:5555', '127.0.0.1:1', '[::1]:1', '127.0.0.1'):
            self.assertTrue(server.host_allowed(h), h)
        self.assertFalse(server.host_allowed('evil.test'))
        self.assertFalse(server.host_allowed('127.0.0.1.evil.test'))


class F09Ipv6Loopback(unittest.TestCase):
    """`--host ::1` opens an IPv6 server."""

    def test_server_class(self):
        import socket
        self.assertEqual(server.HTTPServer6.address_family, socket.AF_INET6)
        self.assertEqual(server.ThreadingHTTPServer.address_family, socket.AF_INET)

    def test_serves_on_ipv6_loopback(self):
        import http.client
        try:
            srv = server.HTTPServer6(('::1', 0), server.Handler)
        except OSError:
            self.skipTest('IPv6 loopback not available')
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        c = http.client.HTTPConnection('::1', port, timeout=10)
        c.request('GET', '/board.js', headers={'Host': '[::1]:%d' % port})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        c.request('GET', '/board.js', headers={'Host': 'evil.test'})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 403)


class R7MissingSession(unittest.TestCase):
    """A --session that is well formed but has no transcript ends with an error message."""

    def test_exit_with_message(self):
        import contextlib
        import io
        fake = types.SimpleNamespace(get=lambda sid: None, loop=lambda: None)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as home:                   # the folders read at start-up are an empty temporary folder (the real transcripts are not touched)
            ready = threading.Event()
            ready.set()
            with mock.patch('sys.argv', ['server.py', '--session', '66666666-6666-4666-8666-666666666666', '--port', '0']), \
                    mock.patch.object(server.LINKS, 'scan', lambda: None), mock.patch.object(server.LINKS, 'ready', ready), \
                    patched(REG=fake, CLAUDE_HOME=os.path.join(home, '.claude'), PROJECTS=os.path.join(home, '.claude', 'projects'),
                            CODEX_HOME=os.path.join(home, '.codex'), CODEX_SESSIONS=os.path.join(home, '.codex', 'sessions'),
                            CODEX_NAMES=os.path.join(home, '.codex', 'session_index.jsonl')), \
                    mock.patch.dict(server.main.__globals__, {'DEFAULT_SESSION': None, 'FIXED_DEFAULT': None}), \
                    terminal_lang('ko'), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    server.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('세션을 찾지 못했습니다', err.getvalue())


class R8SettingsDenied(unittest.TestCase):
    """settings*.json in the Claude settings folder is not opened through /api/file, even if an agent wrote it."""

    def test_denied(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            ch = os.path.join(home, '.claude')
            os.makedirs(os.path.join(ch, 'projects'))
            with patched(HOME=home, CLAUDE_HOME=ch, CODEX_HOME=os.path.join(home, '.codex'),
                         DENY_FILES=(os.path.join(ch, '.credentials.json'),)):
                for name in ('settings.json', 'settings.local.json', 'settings-x.json'):
                    self.assertTrue(server.denied_file(os.path.join(ch, name)), name)
                for path in (os.path.join(ch, 'projects', 'settings.json'), os.path.join(ch, 'notes.json'),
                             os.path.join(home, 'proj', 'settings.json'), os.path.join(ch, 'settings.md')):
                    self.assertFalse(server.denied_file(path), path)


class F10OpenNoFollow(unittest.TestCase):
    """Opens the checked path without following a link at any component."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, 'a', 'b'))
        os.makedirs(os.path.join(self.root, 'forbidden'))
        with open(os.path.join(self.root, 'a', 'b', 'c.md'), 'wb') as f:
            f.write('줄1\r\n줄2\n끝 \xff'.encode('utf-8')[:-1] + b'\xff')
        with open(os.path.join(self.root, 'forbidden', 'c.md'), 'w') as f:
            f.write('secret')

    def test_opens_like_open(self):
        real = os.path.join(self.root, 'a', 'b', 'c.md')
        with server.open_nofollow(real) as f:
            got = f.read()
            mtime = os.fstat(f.fileno()).st_mtime
        with open(real, encoding='utf-8', errors='replace') as f:
            self.assertEqual(got, f.read())              # newline translation and broken-byte handling are the same as open
        self.assertEqual(mtime, os.stat(real).st_mtime)

    def test_middle_directory_swapped_for_link(self):
        real = os.path.join(self.root, 'a', 'b', 'c.md')
        os.rename(os.path.join(self.root, 'a'), os.path.join(self.root, 'a_old'))
        os.symlink(os.path.join(self.root), os.path.join(self.root, 'a'))   # a -> root: a/b/c.md does not exist
        with self.assertRaises(OSError):
            server.open_nofollow(real)
        os.remove(os.path.join(self.root, 'a'))
        os.symlink(os.path.join(self.root, 'forbidden'), os.path.join(self.root, 'a'))
        with self.assertRaises(OSError):                 # the old way (O_NOFOLLOW on the last component only) would follow the link here
            server.open_nofollow(os.path.join(self.root, 'a', 'c.md'))

    def test_last_component_link_and_fifo_and_dir(self):
        os.symlink(os.path.join(self.root, 'forbidden', 'c.md'), os.path.join(self.root, 'a', 'link.md'))
        with self.assertRaises(OSError):
            server.open_nofollow(os.path.join(self.root, 'a', 'link.md'))
        os.mkfifo(os.path.join(self.root, 'a', 'fifo.md'))
        with self.assertRaises(OSError):                 # a FIFO is refused without blocking
            server.open_nofollow(os.path.join(self.root, 'a', 'fifo.md'))
        with self.assertRaises(OSError):
            server.open_nofollow(os.path.join(self.root, 'a'))
        with self.assertRaises(OSError):
            server.open_nofollow('relative/x.md')
        with self.assertRaises(OSError):
            server.open_nofollow(os.path.join(self.root, 'nope.md'))

    @unittest.skipUnless(os.path.isdir('/proc/self/fd'), '/proc이 없는 곳(macOS 등)에서는 열린 fd를 세지 않는다')
    def test_no_leaked_descriptors(self):
        real = os.path.join(self.root, 'a', 'b', 'c.md')
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(20):
            with server.open_nofollow(real):
                pass
            with self.assertRaises(OSError):
                server.open_nofollow(os.path.join(self.root, 'nope.md'))
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


def codex_owner(cmd):
    """The owner (None if there is none) found by treating cmd as a Bash call run by Claude session sidA and feeding the U thread to cx_link."""
    call = server.cx_parse_call(100.0, 'tu', {'command': cmd}, '/w')
    thread = {'id': U, 'origin': 'exec', 'guardian': False, 'meta_ts': 102.0, 'cwd': '/w',
              'first_user': '이 글은 그 명령의 지시문과 아무 관계가 없는 첫 사용자 메시지다. 길이는 충분히 길다.'}
    return server.cx_link([('sidA', call)] if call else [], [thread]).get(U)


class N01CommandPosition(CliFixture):
    """The inside of a quote after `eval` or a shell `-c` is shell code only when that word is at a **real command position**.
    A string used for output or as an argument, such as `echo eval '…'`, `printf … bash -c '…'` or `git commit -m "eval '…'"`, is plain text."""
    CODEX = 'true; codex exec resume %s fake' % U
    CLAUDE = 'true; claude -p fake'
    C1 = '22222222-2222-4222-8222-222222222222'
    NEG = ("echo eval '{}'", "printf '%s\\n' bash -c '{}'", 'git commit -m "eval \'{}\'"', 'echo "bash -c \'{}\'"',
           "grep -e '{}' file", "bash script.sh -c '{}'", "echo $(pwd) bash -c '{}'", "bash -c true '{}'", "echo hi; echo eval '{}'",
           "ssh host '{}'", "printf '%s' \"eval '{}'\"")
    POS = ("eval '{}'", "FOO=1 bash -c '{}'", "timeout 600 bash -c '{}'", "nohup bash -c '{}' &", "(eval \"{}\")",
           "env -i X=1 sh -c '{}'", "sudo -u me bash -c '{}'", "if true; then eval '{}'; fi", "/bin/bash -ec '{}'",
           "bash -lc '{}'", 'bash -lc "{}"', "zsh -c '{}'", "command bash -c '{}'", "time nice -n 5 dash -c '{}'",
           "timeout -s KILL 600 bash -c '{}'", "echo a | eval '{}'", "cd /w && eval '{}'", "bash --norc -c '{}'")

    def cands(self, cmd):
        f = {'pos': 0, 'sid': self.PARENT, 'calls': [], 'cli': []}
        self.links._cli_line(f, bash_line(T0, cmd).encode())
        return f['cli']

    def test_negatives_codex(self):
        for w in self.NEG:
            cmd = w.format(self.CODEX)
            self.assertFalse(launches(cmd), cmd)                      # no launch
            self.assertIsNone(codex_owner(cmd), cmd)                  # no owner link (rule id)
            call = server.cx_parse_call(1.0, 't', {'command': cmd}, '/w')
            self.assertTrue(call is None or (not call['launch'] and call['L'] == []), cmd)

    def test_negatives_claude(self):
        for w in self.NEG:
            cmd = w.format(self.CLAUDE)
            self.assertEqual(self.cands(cmd), [], cmd)                # no CLI candidate

    def test_negatives_claude_do_not_adopt_unrelated_session(self):
        for n, w in enumerate(self.NEG):
            cmd = w.format(self.CLAUDE)
            parent = '%08d-1111-4111-8111-111111111111' % n
            child = '%08d-2222-4222-8222-222222222222' % n
            self.write(parent, [bash_line(T0, cmd)])
            self.write(child, child_lines(T0 + 3))                    # an unrelated session started in the same folder 3 seconds later
        self.assertEqual(self.scan(), {})                             # no owner link

    def test_positives_codex(self):
        for w in self.POS:
            cmd = w.format('codex exec resume %s x' % U)
            self.assertTrue(launches(cmd), cmd)
            o = codex_owner(cmd)
            self.assertIsNotNone(o, cmd)                              # a real link is made
            self.assertEqual((o['sid'], o['rule']), ('sidA', 'id'), cmd)

    def test_positives_claude(self):
        for n, w in enumerate(self.POS):
            cmd = w.format('claude -p hi')                            # the letters are the child's first instruction (child_lines)
            self.assertEqual(len(self.cands(cmd)), 1, cmd)
            parent = '%08d-1111-4111-8111-111111111111' % n
            child = '%08d-2222-4222-8222-222222222222' % n
            self.write(parent, [bash_line(T0 + n * 1000, cmd), result_line(T0 + n * 1000 + 8)])    # the call ends: it cannot be the parent of another call's child
            self.write(child, child_lines(T0 + n * 1000 + 3))
        own = self.scan()
        self.assertEqual(len(own), len(self.POS))                     # all of them are linked
        self.assertEqual({o['sid'][:8] for o in own.values()}, {'%08d' % n for n in range(len(self.POS))})

    def test_bash_dash_lc_with_cd(self):
        cands = self.cands('bash -lc "cd /x && claude -p go"')
        self.assertEqual([c['cwd'] for c in cands], ['/x'])
        cands = self.cands('(cd /x && eval "claude -p go")')
        self.assertEqual([c['cwd'] for c in cands], ['/x'])

    def test_earlier_cases_still_blocked(self):
        self.assertFalse(launches("bash -c 'echo \"(codex exec resume %s fake)\"'" % U))   # nested shell
        self.assertFalse(launches('echo \\(codex exec resume %s fake\\)' % U))             # escaped characters
        self.assertFalse(launches("bash -c 'cat <<EOF\ncodex exec x\nEOF'"))
        self.assertEqual(self.cands("cat <<'EOF'\nclaude -p hello\nEOF"), [])              # heredoc

    def test_helper_code_arg(self):
        from importlib import import_module
        try:
            ca = import_module('board.link')._code_arg
        except ImportError:
            self.skipTest('한 파일 코드에는 없는 내부 함수')
        self.assertTrue(ca(['eval']))
        self.assertTrue(ca(['FOO=1', 'env', '-u', 'X', 'nohup', 'timeout', '-s', 'KILL', '10', 'bash', '-lc']))
        self.assertTrue(ca(['then', 'eval']))
        self.assertTrue(ca(['eval', '--']))
        self.assertFalse(ca(['eval', 'echo']))               # if it is not eval's first argument it is an argument of the preceding word
        self.assertFalse(ca([]))
        self.assertFalse(ca(['nohup']))
        self.assertFalse(ca(['bash']))
        self.assertFalse(ca(['bash', '-x']))
        self.assertFalse(ca(['bash', '-c', 'x']))                   # only the first operand is the command text
        self.assertFalse(ca(['echo', 'eval']))
        self.assertFalse(ca(['xbash', '-c']))


class R401ClosedOptionList(N01CommandPosition):
    """Options of a prefix command or shell open the inside only when they are all in a **closed list**. Lookup, help and unknown options do not open it.
    eval only when the quote is its first argument. Every negative has no launch, no CLI candidate and no owner link (Codex rule id, Claude child),
    and every positive really gets linked (this class also re-runs the negative and positive lists of the earlier command-position tests the same way)."""
    NEG = ("command -v bash -c '{}'", "command -V bash -c '{}'", "env --help bash -c '{}'", "bash --help -c '{}'",
           "timeout --help bash -c '{}'", "timeout --help '{}'", "env --version bash -c '{}'", "bash --version -c '{}'",
           "nice --help bash -c '{}'", "sudo -V bash -c '{}'", "sudo --help bash -c '{}'", "sudo -l bash -c '{}'",
           "command -x bash -c '{}'", "bash -v -c '{}'", "bash -O extglob -c '{}'", "timeout -v 5 bash -c '{}'",
           "env -S x bash -c '{}'", "timeout abc bash -c '{}'", "time -f %e bash -c '{}'", "exec -a x bash -c '{}'",
           "timeout 5 --help bash -c '{}'", "bash -c --help '{}'", "env -u bash -c '{}'", "nice -n bash -c '{}'",
           "sudo -u bash -c '{}'", "bash +x -c '{}'", "bash --rcfile f -c '{}'", "bash -o -c '{}'", "bash -lc -v '{}'",
           "eval echo '{}'", "eval printf '%s' '{}'", "eval true '{}'", "eval x '{}'", "eval -x '{}'")
    POS = ("timeout -k 5 600 bash -c '{}'", "env VAR=1 bash -c '{}'", "sudo -u x bash -lc '{}'", "bash -ec '{}'",
           "nice -n 5 bash -c '{}'", "env -u FOO -C /tmp bash -c '{}'", "timeout --kill-after=5 10s bash -c '{}'",
           "sudo -E -H -n -u me bash -c '{}'", "bash -o pipefail -c '{}'", "bash --login -c '{}'", "command -p bash -c '{}'",
           "time -p bash -c '{}'", "nice -5 bash -c '{}'", "setsid -f bash -c '{}'", "timeout 1.5 bash -c '{}'",
           "bash --noprofile --norc -c '{}'", "env -i bash -c '{}'", "timeout -s KILL 600 bash -c '{}'", "eval '{}'",
           "eval -- '{}'", "timeout $T bash -c '{}'", "env --unset=A --chdir=/w bash -xc '{}'", "sudo FOO=1 bash -c '{}'")

    def test_eval_uses_the_quote_as_the_command(self):
        U2 = U
        self.assertTrue(launches("eval 'codex exec resume %s x'" % U2))
        self.assertEqual([c['cwd'] for c in self.cands('eval "cd /x && claude -p go"')], ['/x'])   # the cd inside the quote too
        self.assertFalse(launches("eval echo 'codex exec resume %s x'" % U2))                     # it is echo …, so not a launch
        self.assertEqual(self.cands("eval echo 'claude -p go'"), [])
        self.assertIsNone(codex_owner("eval echo 'codex exec resume %s x'" % U2))

    def test_options_each_checked(self):
        from importlib import import_module
        try:
            ca = import_module('board.link')._code_arg
        except ImportError:
            self.skipTest('한 파일 코드에는 없는 내부 함수')
        for ok in (['command', '-p', 'bash', '-c'], ['timeout', '-k', '5', '600', 'bash', '-lc'], ['env', 'VAR=1', 'bash', '-c'],
                   ['sudo', '-u', 'x', 'bash', '-lc'], ['nice', '-n', '5', 'bash', '-c'], ['bash', '-ec']):
            self.assertTrue(ca(ok), ok)
        for no in (['command', '-v', 'bash', '-c'], ['command', '-V', 'bash', '-c'], ['env', '--help', 'bash', '-c'],
                   ['bash', '--help', '-c'], ['timeout', '--help'], ['timeout', '--help', 'bash', '-c'], ['timeout', 'x', 'bash', '-c'],
                   ['env', '-S', 'a', 'bash', '-c'], ['sudo', '-l', 'bash', '-c'], ['bash', '-v', '-c'], ['bash', '-O', 'x', '-c'],
                   ['nice', '-n', 'x', 'bash', '-c'], ['timeout', '-k', 'x', '5', 'bash', '-c']):
            self.assertFalse(ca(no), no)


class S0102OptionValues(R401ClosedOptionList):
    """The **value** of shell `-o` and `+o`, and the `=` form or attached value of short options, also go by a closed list. Negatives have no launch, CLI candidate or owner link
    and positives get linked (the negative and positive lists of the closed-list tests are inherited and extended)."""
    NEG = R401ClosedOptionList.NEG + (
        "bash -o noexec -c '{}'", "bash +o noexec -c '{}'", "sh -o noexec -c '{}'", "zsh -o noexec -c '{}'", "bash -o unknownopt -c '{}'",
        "bash -o errexit -o noexec -c '{}'", "bash -o allexport -c '{}'", "bash -o ignoreeof -c '{}'", "bash -o NOEXEC -c '{}'",
        "zsh -o posix -c '{}'", "sh -o posix -c '{}'", "dash -o errtrace -c '{}'", "sh -o pipefail -c '{}'", "bash -opipefail -c '{}'",
        "env -u=FOO bash -c '{}'", "timeout -k=5 10 bash -c '{}'", "timeout -s=KILL 10 bash -c '{}'", "nice -n=5 bash -c '{}'",
        "env -C=/tmp bash -c '{}'", "sudo -u=root bash -c '{}'", "env -uFOO bash -c '{}'", "timeout -k5 10 bash -c '{}'",
        "timeout -sKILL 10 bash -c '{}'", "nice -n5 bash -c '{}'", "env -C/tmp bash -c '{}'", "sudo -uroot bash -c '{}'",
        "env -u -x bash -c '{}'", "sudo -u -E bash -c '{}'", "timeout 5 FOO=1 bash -c '{}'", "nohup FOO=1 bash -c '{}'",
        "command FOO=1 bash -c '{}'", "exec FOO=1 bash -c '{}'", "timeout 5 then bash -c '{}'", "nohup ! bash -c '{}'")
    POS = R401ClosedOptionList.POS + (
        "bash -o errexit -c '{}'", "bash -o nounset -c '{}'", "bash -o xtrace -c '{}'", "bash -o verbose -c '{}'", "bash -o noclobber -c '{}'",
        "bash -o posix -c '{}'", "bash -o errtrace -c '{}'", "bash -o functrace -c '{}'", "bash +o pipefail -c '{}'",
        "bash -o errexit -o nounset -c '{}'", "zsh -o pipefail -c '{}'", "sh -o errexit -c '{}'", "dash -o nounset -c '{}'",
        "ksh -o pipefail -c '{}'", "bash -o pipefail -lc '{}'", "env --unset=FOO bash -c '{}'", "env --chdir=/w bash -c '{}'",
        "timeout --kill-after=5 --signal=KILL 10 bash -c '{}'", "nice --adjustment=5 bash -c '{}'", "timeout -k 5 -s KILL 10 bash -c '{}'",
        "env -u FOO bash -c '{}'", "env -C /w bash -c '{}'", "env -u FOO -u BAR A=1 bash -c '{}'", "sudo -u x bash -lc '{}'",
        "FOO=1 nohup bash -c '{}'", "FOO=1 timeout 5 bash -c '{}'", "then FOO=1 bash -c '{}'", "env A=1 B=2 bash -c '{}'", "sudo A=1 bash -c '{}'",
        "nice -n -5 bash -c '{}'", "timeout 5 >/dev/null bash -c '{}'")

    def test_shell_option_values(self):
        from importlib import import_module
        try:
            ca = import_module('board.link')._code_arg
        except ImportError:
            self.skipTest('한 파일 코드에는 없는 내부 함수')
        for v in ('pipefail', 'errexit', 'nounset', 'xtrace', 'verbose', 'noclobber', 'posix', 'errtrace', 'functrace'):
            self.assertTrue(ca(['bash', '-o', v, '-c']), v)
            self.assertTrue(ca(['bash', '+o', v, '-c']), v)
        for v in ('noexec', 'allexport', 'emacs', 'vi', 'ignoreeof', 'NOEXEC', 'nounset;', ''):
            self.assertFalse(ca(['bash', '-o', v, '-c']), v)
            self.assertFalse(ca(['bash', '+o', v, '-c']), v)
        self.assertFalse(ca(['bash', '-o']))
        self.assertFalse(ca(['bash', '-c', '-o', 'noexec']))                      # what follows -c is the slot for the command text (the next quote is not opened)

    def test_short_option_values_are_spaced(self):
        from importlib import import_module
        try:
            ca = import_module('board.link')._code_arg
        except ImportError:
            self.skipTest('한 파일 코드에는 없는 내부 함수')
        for ok in (['env', '-u', 'FOO', 'bash', '-c'], ['timeout', '-k', '5', '10', 'bash', '-c'], ['timeout', '-s', 'KILL', '10', 'bash', '-c'],
                   ['nice', '-n', '5', 'bash', '-c'], ['env', '--unset=FOO', 'bash', '-c'], ['timeout', '--signal=KILL', '10', 'bash', '-c']):
            self.assertTrue(ca(ok), ok)
        for no in (['env', '-u=FOO', 'bash', '-c'], ['timeout', '-k=5', '10', 'bash', '-c'], ['timeout', '-s=KILL', '10', 'bash', '-c'],
                   ['nice', '-n=5', 'bash', '-c'], ['env', '-uFOO', 'bash', '-c'], ['timeout', '-k5', '10', 'bash', '-c'],
                   ['nice', '-n5', 'bash', '-c'], ['env', '--ignore-environment=1', 'bash', '-c'], ['timeout', '--foreground=1', '10', 'bash', '-c']):
            self.assertFalse(ca(no), no)


class S03S04ThenDo(S0102OptionValues):
    """Long shell options are per shell (bash only), `timeout -s` takes known signals only, and `then` and `do` are reserved words only at a command position.
    Negatives have no launch, CLI candidate or owner link and positives get linked (the lists of the earlier tests are inherited and extended)."""
    NEG = S0102OptionValues.NEG + (
        "dash --noprofile -c '{}'", "sh --norc -c '{}'", "sh --login -c '{}'", "dash --login -c '{}'", "zsh --login -c '{}'", "ksh --norc -c '{}'",
        "dash --norc --noprofile -c '{}'", "bash --posix -c '{}'", "bash --noediting -c '{}'", "bash --login --unknown -c '{}'",
        "timeout -s NOTASIGNAL 10 bash -c '{}'", "timeout -s 99 10 bash -c '{}'", "timeout -s 65 10 bash -c '{}'", "timeout -s 32 10 bash -c '{}'",
        "timeout -s HUP=1 10 bash -c '{}'", "timeout -s kill 10 bash -c '{}'", "timeout -s SIG 10 bash -c '{}'", "timeout -s SIGNOTHING 10 bash -c '{}'",
        "timeout --signal=NOTASIGNAL 10 bash -c '{}'", "timeout --signal=12x 10 bash -c '{}'", "timeout -s 1x 10 bash -c '{}'")
    POS = S0102OptionValues.POS + (
        "bash --login --noprofile -lc '{}'", "bash --noprofile --norc --login -c '{}'",
        "timeout -s KILL 10 bash -c '{}'", "timeout -s SIGKILL 10 bash -c '{}'", "timeout -s 9 10 bash -c '{}'", "timeout -s 0 10 bash -c '{}'",
        "timeout -s 31 10 bash -c '{}'", "timeout -s TERM 10 bash -c '{}'", "timeout -s SIGTERM 10 bash -c '{}'", "timeout --signal=SIGINT 10 bash -c '{}'",
        "timeout -s USR1 10 bash -c '{}'", "timeout -s IOT 10 bash -c '{}'", "timeout -k 5 --signal=HUP 10 bash -c '{}'")
    # Output commands with `then` and `do` (and other reserved words) in an **argument position**: not a command boundary
    TEXT_NEG = ("echo then {}", "echo do {}", "printf '%s\\n' then {}", "echo x then {}", "echo do; echo then {}", "echo then do {}",
                "grep do {}", "echo a && echo do {}", "echo a | grep then {}", "echo else {}", "echo 'x' do {}")
    # `then` and `do` at a command position (including chained reserved words)
    TEXT_POS = ("if true; then {}; fi", "for x in a; do {}; done", "n=0; while [ $n -lt 1 ]; do {}; n=1; done", "if true\nthen\n  {}\nfi",
                "for x in a\ndo {}\ndone", "if true; then if true; then {}; fi; fi", "while false; do true; done; for y in b; do {}; done",
                "x=1; if [ $x ]; then nohup {} & fi", "true; do {}", "a=1\n then {}", "for i in 1 2; do timeout 5 {}; done")

    def test_then_do_only_at_command_position_codex(self):
        for t in self.TEXT_NEG:
            cmd = t.format('codex exec resume %s fake' % U)
            self.assertFalse(launches(cmd), cmd)
            self.assertIsNone(codex_owner(cmd), cmd)
        for t in self.TEXT_POS:
            cmd = t.format('codex exec resume %s x' % U)
            self.assertTrue(launches(cmd), cmd)
            self.assertIsNotNone(codex_owner(cmd), cmd)

    def test_then_do_only_at_command_position_claude(self):
        for t in self.TEXT_NEG:
            cmd = t.format('claude -p fake')
            self.assertEqual(self.cands(cmd), [], cmd)
        for n, t in enumerate(self.TEXT_NEG):                          # an unrelated session in the same folder 3 seconds later is not attached as a child
            parent = '%08d-3333-4333-8333-333333333333' % n
            child = '%08d-4444-4444-8444-444444444444' % n
            self.write(parent, [bash_line(T0 + n * 1000, t.format('claude -p fake'))])
            self.write(child, child_lines(T0 + n * 1000 + 3))
        self.assertEqual(self.scan(), {})
        for n, t in enumerate(self.TEXT_POS):
            self.assertEqual(len(self.cands(t.format('claude -p go'))), 1, t)


class RealShellSoundness(unittest.TestCase):
    """Checked with a real shell: a command we judge to open (launch) must **really run** (missing one because we did not open it is fine). The inner command is swapped for a harmless `printf` and run,
    and the output shows whether it ran. Commands this machine lacks (sudo, zsh, ksh, setsid -f) and fragments (then …) are left out, and so is a template this machine cannot run
    (`can_run`: macOS has no `timeout` and has BSD env and nice), one by one: the rest is still checked."""
    SKIP = ('sudo', 'zsh', 'ksh', 'setsid', 'then ', 'ssh ', 'eval -x', '>/dev/null')
    TOOLS = ('timeout', 'env', 'nice', 'nohup', 'dash')
    GNU_TOOLS = ('timeout', 'env', 'nice')
    # options the BSD tools may not take: a long option, `env -C`, and `nice -5` / `nice -n -5` (BSD nice refuses a negative value without privilege and then does not run the command)
    GNU_OPTION = re.compile(r'\b(?:env|nice|timeout)\b[^;&|]*?\s(?:--\w|-C\b|-\d)')
    _gnu = {}

    def real_runs(self, cmd):
        import subprocess
        r = subprocess.run(['bash', '-c', cmd], env=dict(os.environ, T='10'), stdin=subprocess.DEVNULL, capture_output=True, timeout=15, text=True)
        return 'RAN' in r.stdout

    @classmethod
    def is_gnu(cls, tool):
        """Whether `tool` is the GNU one: it answers --version (the BSD env, nice and timeout do not)."""
        import subprocess
        if tool not in cls._gnu:
            try:
                cls._gnu[tool] = subprocess.run([tool, '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                cls._gnu[tool] = False
        return cls._gnu[tool]

    def can_run(self, t):
        """Whether this machine can really run the template: it has every tool the template names, and the GNU env, nice and timeout when the template gives them options only those take."""
        import shutil
        used = [w for w in self.TOOLS if re.search(r'\b%s\b' % w, t)]
        if any(not shutil.which(w) for w in used):
            return False
        return not self.GNU_OPTION.search(t) or all(self.is_gnu(w) for w in used if w in self.GNU_TOOLS)

    def templates(self):
        return [t.replace('/w ', '/tmp ') for t in S03S04ThenDo.NEG + S03S04ThenDo.POS if not any(w in t for w in self.SKIP)]

    def test_opened_implies_really_runs(self):
        import shutil
        if not shutil.which('bash'):
            self.skipTest('bash 없음')
        bad, checked = [], 0
        for t in self.templates():
            if not self.can_run(t):
                continue
            checked += 1
            if launches(t.format('codex exec resume %s x' % U)) and not self.real_runs(t.format('printf RAN')):
                bad.append(t)
        full = all(shutil.which(w) for w in self.TOOLS) and all(self.is_gnu(w) for w in self.GNU_TOOLS)
        self.assertGreater(checked, 80 if full else 40)                  # most templates need none of the tools a machine may lack
        self.assertEqual(bad, [], '안을 열었지만 실제로는 실행되지 않는 명령')

    def test_a_machine_without_timeout_and_with_bsd_tools_still_checks_most_templates(self):
        """What macOS has: no `timeout`, no GNU option in env and nice. Every template that names `timeout` or gives those tools a GNU option is left out, none else."""
        with mock.patch('shutil.which', lambda w: None if w == 'timeout' else '/usr/bin/' + w), mock.patch.object(self, 'is_gnu', lambda w: False):
            run = [t for t in self.templates() if self.can_run(t)]
        left = [t for t in self.templates() if t not in run]
        self.assertTrue(all(re.search(r'\btimeout\b', t) or self.GNU_OPTION.search(t) for t in left), left)
        self.assertGreater(len(run), 40)
        self.assertTrue(any('nice -n 5 ' in t for t in run) and any('env -u FOO ' in t for t in run))                    # the POSIX forms are still checked
        self.assertFalse(any(re.search(r'\btimeout\b', t) or '--unset=' in t or 'env -C' in t or 'nice -5' in t for t in run))
        with mock.patch('shutil.which', lambda w: '/usr/bin/' + w), mock.patch.object(self, 'is_gnu', lambda w: True):     # all GNU: nothing is left out
            self.assertEqual([t for t in self.templates() if not self.can_run(t)], [])

    def test_text_forms(self):
        import shutil
        if not shutil.which('bash'):
            self.skipTest('bash 없음')
        for t in S03S04ThenDo.TEXT_POS:
            self.assertTrue(launches(t.format('codex exec resume %s x' % U)), t)          # if it was opened
            if '\n then' in t or t.startswith('true; do'):
                continue                                                                   # fragments (syntax errors) are not asked whether they ran
            if not self.can_run(t):
                continue                                                                   # needs a tool this machine lacks (`timeout`): the parser side above still holds
            self.assertTrue(self.real_runs(t.format('printf RAN')) or 'nohup' in t, t)      # it really runs
        for t in S03S04ThenDo.TEXT_NEG:
            self.assertFalse(launches(t.format('codex exec resume %s x' % U)), t)


class N01ScanPerformance(unittest.TestCase):
    """LinkIndex.scan looks for the strings in the whole chunk instead of in every line (same lines, same order). Masking inside double quotes works per span."""

    def test_hit_lines_equals_per_line_filter(self):
        import random
        from importlib import import_module
        try:
            hit = import_module('board.link')._hit_lines
        except ImportError:
            self.skipTest('한 파일 코드에는 없는 내부 함수')
        rnd = random.Random(5)
        toks = [b'"Bash"', b'"quotaLimits"', b'abc', b'x' * 50, b'', b'"Bash" "quotaLimits"', b'{"a":1}', b'\xff\xfe']
        for _ in range(500):
            lines = [b' '.join(rnd.choice(toks) for _ in range(rnd.randint(0, 4))) for _ in range(rnd.randint(0, 30))]
            body = b'\n'.join(lines) + b'\n'
            want = [r for r in body.split(b'\n') if b'"quotaLimits"' in r or b'"Bash"' in r]
            self.assertEqual(hit(body), want)

    def test_scan_keeps_line_order_and_quota(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            proj = os.path.join(home, '.claude', 'projects', '-w')
            os.makedirs(proj)
            os.makedirs(os.path.join(home, '.codex', 'sessions'))
            lines = [json.dumps({'type': 'user', 'message': {'content': 'noise'}}),
                     json.dumps({'quotaLimits': {'rateLimitType': 'five_hour', 'status': 'rejected', 'resetsAt': 1}, 'timestamp': '2026-09-30T00:00:00.000Z'}),
                     bash_line(T0, 'cd /a && claude -p one', tid='t1'), json.dumps({'type': 'user', 'message': {'content': 'noise'}}),
                     bash_line(T0 + 1, 'codex exec -o a.md "x"', tid='t2'), bash_line(T0 + 2, 'claude -p two', tid='t3')]
            with open(os.path.join(proj, '11111111-1111-4111-8111-111111111111.jsonl'), 'w') as f:
                f.write('\n'.join(lines) + '\n')
            idx = server.LinkIndex()
            with patched(PROJECTS=os.path.join(home, '.claude', 'projects'), CODEX_SESSIONS=os.path.join(home, '.codex', 'sessions'),
                         CODEX_NAMES=os.path.join(home, '.codex', 'names.jsonl'), CODEX=server.CodexIndex()):
                idx.scan()
            f = next(iter(idx.files.values()))
            self.assertEqual([c['id'] for c in f['cli']], ['t1', 't3'])      # in line order
            self.assertEqual([c['id'] for c in f['calls']], ['t2'])
            self.assertEqual(idx.quota['five_hour']['status'], 'rejected')

    def test_report_prefilters_give_the_same_matches(self):
        import random
        from importlib import import_module
        try:
            deb = import_module('board.debates')
        except ImportError:
            self.skipTest('한 파일 코드에는 없는 내부 함수')
        rnd = random.Random(7)
        toks = ['/home/u/proj/t1/r1/A.md', 'r2/B.md', '`/x/r10/C.md`', '~/d/r3/D.md', 'x/r1/y.md', '…/t2/r2/A.md', ' ', '\n', '쓴다', 'r1', '/r1/', 'a.md',
                '/proj/docs/notes/opus.md', '(/tmp/x/r1/B.md)', '.md', 'r9/', '/r', 'rr1/A.md', '-r1/A.md']
        for _ in range(3000):
            t = ''.join(rnd.choice(toks) for _ in range(rnd.randint(0, 8)))
            self.assertEqual([m.group(0) for m in deb.report_refs(t)], [m.group(0) for m in server.REPORT_RE.finditer(t)], t)
            self.assertEqual([m.group(0) for m in deb.rel_report_refs(t)], [m.group(0) for m in server.REL_REPORT_RE.finditer(t)], t)

    def test_dquote_mask_is_length_preserving(self):
        for c in ('echo "aaa $(true; codex exec x) bbb" ccc', 'echo "a\\"b" \\"x', 'echo "unterminated', 'echo "a`b c`d"',
                  'echo "$HOME $1 $" x', 'echo "' + 'y' * 5000 + '"'):
            code = server.shell_code(c)
            self.assertEqual(len(code), len(c))
        self.assertTrue(server.LAUNCH_RE.search(server.shell_code('echo "a $(codex exec x) b"')))
        self.assertFalse(server.LAUNCH_RE.search(server.shell_code('echo "a ; codex exec x b"')))


if __name__ == '__main__':
    unittest.main()
