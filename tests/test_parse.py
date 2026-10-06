"""Tests that pin down the basic behaviour of transcript parsing (how a codex exec launch is read, link rules, tokens, etc.).

    python3 -m unittest discover -s tests
    AB_SRC=<other copy folder> python3 -m unittest discover -s tests     # the same tests on another copy (code from before a fix, etc.)
"""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
from board import runstate, util  # noqa: E402


class Launch(unittest.TestCase):
    def test_launch_fields(self):
        L = server.cx_launches('cd /x && codex exec -o out.md "$Q" 2>&1', {}, '/w')
        self.assertEqual(len(L), 1)
        self.assertEqual(L[0]['out'], 'out.md')
        self.assertEqual(L[0]['scwd'], '/x')
        self.assertEqual(L[0]['cwd'], '/x')
        self.assertIsNone(L[0]['resume'])
        self.assertEqual(L[0]['names'], [('v0', 'Q')])     # the '2' of 2>&1 does not slip in as an argument

    def test_resume(self):
        L = server.cx_launches('codex exec resume 019a0000-0000-7000-8000-000000000000 "go on"', {}, '/w')
        self.assertEqual(L[0]['resume'], '019a0000-0000-7000-8000-000000000000')
        self.assertEqual(L[0]['scwd'], '/w')

    def test_cd_option(self):
        L = server.cx_launches('codex exec -C /abs/dir "do it"', {}, '/w')
        self.assertEqual(L[0]['cwd'], '/abs/dir')
        self.assertEqual(L[0]['scwd'], '/w')

    def test_launch_re_not(self):
        for c in ('pgrep codex', 'codex exec --help', 'echo "codex exec"'):
            self.assertFalse(server.LAUNCH_RE.search(c), c)

    def test_launch_re_yes(self):
        for c in ('timeout 600 codex exec "x"', 'y=$(codex exec "x")', 'nohup codex exec "x" &',
                  'for i in 1 2; do codex exec "x"; done', 'cd /a && codex exec "x"'):
            self.assertTrue(server.LAUNCH_RE.search(c), c)

    def test_parse_call_filters(self):
        self.assertIsNone(server.cx_parse_call(1.0, 'u1', {'command': 'ls -la'}, '/w'))
        self.assertIsNone(server.cx_parse_call(1.0, 'u1', {'command': 'echo codex'}, '/w'))
        c = server.cx_parse_call(1.0, 'u1', {'command': 'codex exec "hello"', 'description': 'run'}, '/w')
        self.assertTrue(c['launch'])
        self.assertEqual(c['desc'], 'run')
        self.assertEqual(c['cwd'], '/w')
        uid = '019a0000-0000-7000-8000-000000000000'
        c = server.cx_parse_call(1.0, 'u2', {'command': 'tail -f %s.log # codex' % uid}, '/w')
        self.assertFalse(c['launch'])
        self.assertEqual(c['ids'], {uid})

    def test_prompt_rx(self):
        rx, names, literal = server.cx_prompt_rx('Review $DIR and write a report with at least forty characters')
        self.assertEqual(names, [('v0', 'DIR')])
        self.assertEqual(literal, len('Review  and write a report with at least forty characters'))
        self.assertTrue(rx.fullmatch('Review anything at all here and write a report with at least forty characters'))
        self.assertFalse(rx.fullmatch('Something else'))


class LinkRules(unittest.TestCase):
    def _thread(self, tid, meta_ts, first_user, cwd='/w'):
        return {'id': tid, 'origin': 'exec', 'guardian': False, 'meta_ts': meta_ts, 'first_user': first_user, 'cwd': cwd}

    def test_prompt_rule(self):
        prompt = 'Please review the design document thoroughly and write your findings to a report file'
        call = server.cx_parse_call(100.0, 'tu1', {'command': 'cd /w && codex exec "%s"' % prompt}, '/w')
        owners = server.cx_link([('sidA', call)], [self._thread('t1', 102.0, prompt)])
        self.assertEqual(owners['t1']['sid'], 'sidA')
        self.assertEqual(owners['t1']['rule'], 'prompt')

    def test_two_sessions_no_link(self):
        prompt = 'Please review the design document thoroughly and write your findings to a report file'
        cmd = 'cd /w && codex exec "%s"' % prompt
        a = server.cx_parse_call(100.0, 'tuA', {'command': cmd}, '/w')
        b = server.cx_parse_call(101.0, 'tuB', {'command': cmd}, '/w')
        owners = server.cx_link([('sidA', a), ('sidB', b)], [self._thread('t1', 102.0, prompt)])
        self.assertNotIn('t1', owners)

    def test_window(self):
        prompt = 'Please review the design document thoroughly and write your findings to a report file'
        call = server.cx_parse_call(100.0, 'tu1', {'command': 'codex exec "%s"' % prompt}, '/w')
        owners = server.cx_link([('sidA', call)], [self._thread('t1', 100.0 + server.CX_WINDOW + 1, prompt)])
        self.assertNotIn('t1', owners)


class CodexTool(unittest.TestCase):
    def test_exec_command(self):
        self.assertEqual(server.codex_tool('tools.exec_command({cmd: "ls -la"})'), ('exec_command', 'ls -la'))

    def test_apply_patch_wins(self):
        js = 'await tools.exec_command({cmd:"ls"}); await tools.web__run({q:"z"}); await tools.apply_patch(`*** Add File: /x/y.md\n+hi`)'
        self.assertEqual(server.codex_tool(js)[0], 'apply_patch')

    def test_plain(self):
        self.assertEqual(server.codex_tool('console.log(1)'), ('exec', ''))      # no tool and no literal: the name only, never a line of the JS


class Tokens(unittest.TestCase):
    def test_streaming_replace(self):
        tm = server.TokenMeter()
        for out in (10, 50):
            tm.add({'id': 'm1', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 1, 'output_tokens': out}})
        self.assertEqual(tm.t['output'], 50)
        self.assertEqual(tm.t['calls'], 1)

    def test_advisor_only_in_adv(self):
        tm = server.TokenMeter()
        tm.add({'id': 'm1', 'model': 'claude-opus-5-5', 'usage': {'iterations': [
            {'type': 'message', 'input_tokens': 5, 'output_tokens': 7},
            {'type': 'advisor_message', 'model': 'claude-opus-5-5', 'input_tokens': 100, 'output_tokens': 20}]}})
        tm.add({'id': 'm2', 'model': 'claude-opus-5-5', 'usage': {'iterations': [
            {'type': 'advisor_message', 'model': 'claude-opus-5-5', 'input_tokens': 100, 'output_tokens': 20}]}})
        self.assertEqual((tm.t['input'], tm.t['output'], tm.t['calls']), (5, 7, 1))
        self.assertEqual((tm.t['adv_input'], tm.t['adv_output'], tm.t['adv_calls']), (200, 40, 2))

    def test_ctx_limit(self):
        tm = server.TokenMeter()
        tm.model('claude-opus-5-5[1m]')
        self.assertEqual(tm.ctx_limit, 1000000)
        tm = server.TokenMeter()
        tm.add({'id': 'a', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 250000, 'output_tokens': 1}})
        self.assertEqual(tm.ctx_limit, 1000000)
        tm = server.TokenMeter()
        tm.fixed_limit = True
        tm.add({'id': 'a', 'model': 'claude-opus-5-5', 'usage': {'input_tokens': 250000, 'output_tokens': 1}})
        self.assertEqual(tm.ctx_limit, 200000)

    def test_call_cost(self):
        k = server.call_cost({'input_tokens': 1000000, 'cache_creation': {'ephemeral_5m_input_tokens': 1000000,
                                                                          'ephemeral_1h_input_tokens': 1000000}}, 'claude-opus-5-5')
        self.assertAlmostEqual(k['cost_input'], 4.0)
        self.assertAlmostEqual(k['cost_write'], 5.0 + 8.0)          # the 5-minute and 1-hour cache writes are counted separately
        self.assertAlmostEqual(server.call_cost({'input_tokens': 1000000, 'speed': 'fast'}, 'claude-opus-5-5')['cost_input'], 8.0)
        self.assertAlmostEqual(server.call_cost({'input_tokens': 1000000, 'inference_geo': 'us'}, 'claude-opus-5-5')['cost_input'], 4.4)
        self.assertAlmostEqual(server.call_cost({'cache_creation_input_tokens': 1000000}, 'claude-opus-5-5')['cost_write'], 5.0)
        self.assertIsNone(server.call_cost({'input_tokens': 5}, 'some-unknown-model'))

    def test_model_short(self):
        self.assertEqual(server.claude_model_short('claude-opus-5-5[1m]'), 'opus5.5')
        self.assertEqual(server.claude_model_short('claude-haiku-4-5-20251001'), 'haiku4.5')
        self.assertEqual(server.cx_model_short('gpt-6.1-sol'), 'sol6.1')
        self.assertEqual(server.cx_model_short('gpt-6-astra'), 'astra6')
        self.assertEqual(server.cx_model_short(''), 'codex')


class Helpers(unittest.TestCase):
    def test_brief_table(self):
        s = server.Session.__new__(server.Session)
        t = s._brief_table('| 주제 | 폴더 | 선행 | 최종 산출물 |\n|---|---|---|---|\n'
                           '| X 주제 | `t1_x/` | — | `final/x.md` |\n| Y | `t2_y/` | t1 | `final/y.md` |\n')
        self.assertEqual(t, {'t1_x': {'name': 'X 주제', 'deps': '', 'final': 'final/x.md'},
                             't2_y': {'name': 'Y', 'deps': 't1', 'final': 'final/y.md'}})

    def test_cx_decode_big_line(self):
        raw = (b'{"timestamp":"2026-09-30T00:00:00Z","type":"response_item","payload":{"type":"message","content":"'
               + b'a' * (server.CX_LINE_MAX + 10) + b'"}}')
        r = server.cx_decode(raw)
        self.assertIsNone(r['p'])
        self.assertEqual((r['type'], r['pt']), ('response_item', 'message'))
        self.assertTrue(r['head'].startswith(b'{"timestamp"'))
        self.assertIsNone(server.cx_decode(b'x' * (server.CX_LINE_MAX + 10)))
        self.assertIsNone(server.cx_decode(b'   '))
        self.assertIsNone(server.cx_decode(b'{not json'))

    def test_tail_partial_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'a.jsonl')
            with open(p, 'wb') as f:
                f.write(b'{"a":1}\n{"b":')
            t = server.Tail(p)
            self.assertEqual(t.read(), [b'{"a":1}'])
            with open(p, 'ab') as f:
                f.write(b'2}\n')
            self.assertEqual(t.read(), [b'{"b":2}'])
            self.assertEqual(t.read(), [])

    def test_tail_start_skips_partial(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'a.jsonl')
            with open(p, 'wb') as f:
                f.write(b'{"aaaa":1}\n{"b":2}\n')
            t = server.Tail(p, start=3)
            self.assertEqual(t.read(), [b'{"b":2}'])

    def test_tail_max_read_behind(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'a.jsonl')
            with open(p, 'wb') as f:
                f.write(b'{"a":1}\n' * 10)
            t = server.Tail(p, max_read=16)
            self.assertTrue(t.behind())
            n = 0
            for _ in range(20):
                n += len(t.read())
                if not t.behind():
                    break
            self.assertEqual(n, 10)

    def test_tail_truncated_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'a.jsonl')
            with open(p, 'wb') as f:
                f.write(b'{"a":1}\n{"a":2}\n')
            t = server.Tail(p)
            self.assertEqual(len(t.read()), 2)
            with open(p, 'wb') as f:
                f.write(b'{"c":3}\n')
            self.assertEqual(t.read(), [b'{"c":3}'])

    def test_text_helpers(self):
        self.assertEqual(server.trunc('abcdef', 4), 'abc…')
        self.assertEqual(server.trunc('abc', 4), 'abc')
        self.assertEqual(server.as_text([{'type': 'text', 'text': 'a'}, {'type': 'image'}, {'type': 'text', 'text': 'b'}]), 'a\nb')
        self.assertEqual(server.strip_reminders('hi <system-reminder>x\ny</system-reminder> there'), 'hi  there')
        self.assertEqual(server.tool_category('Read'), 'read')
        self.assertEqual(server.tool_category('apply_patch'), 'write')
        self.assertEqual(server.tool_category('exec_command'), 'bash')
        self.assertEqual(server.tool_category('SendMessage'), 'msg')
        self.assertEqual(server.tool_brief('Bash', {'command': 'ls\nx', 'description': ''}), 'ls')
        self.assertAlmostEqual(server.parse_ts('2026-09-30T00:00:00Z'), 1790726400.0)
        self.assertIsNone(server.parse_ts('nope'))


class SharedMemory(unittest.TestCase):
    """/dev/shm holds ordinary files (a review kept in shared memory); the rest of /dev, /proc and /sys is kernel views and devices."""
    SHM = '/dev/shm/review/r1/A.md'

    def metadata(self, mode, only=SHM):
        """os.lstat that answers `mode` for one path and the real answer for the others (nothing is made in /dev/shm)."""
        real = os.lstat

        def lstat(path, *a, **k):
            if str(path).startswith(only):
                return os.stat_result((mode | 0o644, 1, 1, 1, 0, 0, 10, 0, 100, 0))
            return real(path, *a, **k)
        return mock.patch('os.lstat', lstat)

    def test_an_ordinary_file_in_shared_memory_goes_through_the_usual_checks(self):
        with self.metadata(stat.S_IFREG):
            self.assertFalse(util.denied_file(self.SHM))
            self.assertFalse(util.denied_file(self.SHM, fresh=False))
            self.assertIsNotNone(util.stat_plain(self.SHM))
            self.assertIsNotNone(util.stat_regular(self.SHM))
            with self.assertRaises(OSError) as cm:                              # past the checks: it is the (missing) file that fails to open
                util.open_safe(self.SHM)
            self.assertNotIsInstance(cm.exception, util.Denied)

    def test_the_usual_secret_and_hidden_rules_still_apply_there(self):
        for path, why in (('/dev/shm/review/r1/token.md', 'hidden'), ('/dev/shm/.review/r1/A.md', 'hidden'), ('/dev/shm/review/credentials.md', 'hidden')):
            with self.metadata(stat.S_IFREG, only='/dev/shm/'):
                self.assertFalse(util.denied_file(path), path)
                self.assertIsNone(util.stat_plain(path), path)
                self.assertIsNotNone(util.stat_regular(path), path)             # a cell only looks at metadata: the name rules are for what is opened or searched
                with self.assertRaises(util.Denied) as cm:
                    util.open_safe(path)
                self.assertEqual(cm.exception.why, why, path)

    def test_a_device_or_anything_that_is_not_a_regular_file_is_still_refused(self):
        for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFDIR):
            with self.metadata(mode):
                self.assertTrue(util.denied_file(self.SHM), mode)
                self.assertIsNone(util.stat_regular(self.SHM), mode)
                with self.assertRaises(util.Denied):
                    util.open_safe(self.SHM)

    def test_the_folder_itself_and_a_file_that_is_not_there_are_still_refused(self):
        for path in ('/dev/shm', '/dev/shm/notes.md', '/dev/shm/review/r1/gone.md'):
            self.assertTrue(util.denied_file(path), path)
        for path in ('/dev/null', '/dev/stdin', '/dev/zero', '/dev/shmx/a.md', '/dev/mem', '/proc/self/environ', '/sys/kernel/uevent_seqnum', '/dev'):
            self.assertTrue(util.denied_file(path), path)

    def test_a_link_to_a_device_in_shared_memory_stays_refused(self):
        """The check is on the realpath, so a link in /dev/shm to /dev/null is /dev/null."""
        real = os.path.realpath
        with mock.patch('os.path.realpath', lambda p, *a, **k: '/dev/null' if p == self.SHM else real(p, *a, **k)):
            self.assertTrue(util.denied_file(os.path.realpath(self.SHM)))
            with self.assertRaises(util.Denied):
                util.open_safe(self.SHM)


class AgentFeed(unittest.TestCase):
    def _agent(self):
        return server.Agent('a0123456789abcdef', {'description': 'T1-A 조사'})

    def test_tag_key(self):
        a = self._agent()
        self.assertEqual((a.tag, a.title, a.key), ('T1-A', '조사', 'a'))

    def test_pending_and_write(self):
        a = self._agent()
        ts = '2026-09-30T00:00:00Z'
        a.feed({'type': 'assistant', 'timestamp': ts, 'message': {'model': 'claude-opus-5-5', 'id': 'm1', 'content': [
            {'type': 'tool_use', 'id': 'tu1', 'name': 'Write', 'input': {'file_path': '/x/r1/A.md'}}]}})
        self.assertIn('tu1', a.pending)
        self.assertEqual(a.writes[0]['path'], '/x/r1/A.md')
        self.assertEqual(a.tool_count, 1)
        a.feed({'type': 'user', 'timestamp': ts, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tu1'}]}})
        self.assertNotIn('tu1', a.pending)

    TS = '2026-09-30T00:00:00Z'

    def bash(self, a, cmd, call='tb1', cwd='/w', error=None):
        """One Bash call of an agent, and its result when `error` is True or False (None: no result yet)."""
        a.feed({'type': 'assistant', 'timestamp': self.TS, 'cwd': cwd, 'message': {'id': 'm' + call, 'content': [
            {'type': 'tool_use', 'id': call, 'name': 'Bash', 'input': {'command': cmd}}]}})
        if error is not None:
            a.feed({'type': 'user', 'timestamp': self.TS, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': call, 'is_error': error}]}})

    SHELL_WRITES = [
        ("cat > /x/r1/B.md <<'EOF'\nfindings > not.md\nEOF", '/w', ['/x/r1/B.md']),
        ('cat > /x/r1/B.md <<EOF\nline\nEOF', '/w', ['/x/r1/B.md']),
        ('echo done >> /x/r1/B.md', '/w', ['/x/r1/B.md']),
        ('cat /x/r1/A.md | tee /x/r1/B.md', '/w', ['/x/r1/B.md']),
        ('tee -a /x/r1/B.md < /tmp/in.txt', '/w', ['/x/r1/B.md']),
        ('echo hi > /x/r1/B.md 2>&1', '/w', ['/x/r1/B.md']),
        ('echo hi &> /x/r1/B.md', '/w', ['/x/r1/B.md']),
        ('echo a > /x/r1/A.md; echo b > /x/r1/B.md', '/w', ['/x/r1/A.md', '/x/r1/B.md']),
        ('mkdir -p /x/r1 && cat > /x/r1/B.md <<EOF\nx\nEOF', '/w', ['/x/r1/B.md']),
        ('cat > "/x/r1/B C.md" <<EOF\nx\nEOF', '/w', ['/x/r1/B C.md']),
        ('cat > r1/B.md <<EOF\nx\nEOF', '/x', ['/x/r1/B.md']),
        ('cd /x && cat > r1/B.md <<EOF\nx\nEOF', '/w', ['/x/r1/B.md']),
        ('(cd /x; cat > r1/B.md <<EOF\nx\nEOF\n); cat > r1/C.md <<EOF\nx\nEOF', '/w', ['/x/r1/B.md', '/w/r1/C.md']),
        ('OUT=/x; echo hi > $OUT/r1/B.md', '/w', ['/x/r1/B.md']),
        ('bash -c \'cat > /x/r1/B.md <<EOF\nx\nEOF\'', '/w', ['/x/r1/B.md']),
        ('echo hi > ~/r1/B.md', '/w', [os.path.expanduser('~/r1/B.md')]),
        # not a write of a report
        ('foo 2> /x/r1/B.md', '/w', []),                                              # standard error
        ('echo "x > /x/r1/B.md"', '/w', []),                                          # text in quotes
        ("echo 'tee /x/r1/B.md'", '/w', []),
        ('cat <<EOF\n> /x/r1/B.md\nEOF', '/w', []),                                    # a heredoc body
        ('# cat > /x/r1/B.md', '/w', []),                                             # a comment
        ('echo hi > /x/r1/B.log', '/w', []),                                          # not markdown
        ('cat /x/r1/B.md > /dev/null', '/w', []),
        ('cat /x/r1/B.md', '/w', []),
        ('echo hi > $OUT/r1/B.md', '/w', []),                                         # a variable nothing fixes
        ('echo hi > $(pwd)/r1/B.md', '/w', []),
        ('cat > r1/B.md <<EOF\nx\nEOF', '', []),                                      # a relative path and no known folder
        ('cd "$D" && cat > r1/B.md <<EOF\nx\nEOF', '/w', []),                         # a folder nothing fixes
        ('grep x r1/B.md | wc -l', '/w', []),
        ('cat a.md b.md >&2', '/w', []),
        ('cat a.md >&2 /x/r1/B.md', '/w', []),                                        # a copy of standard output, not a file
    ]

    def test_a_shell_write_of_a_markdown_file_is_noted_apart_from_the_tool_writes(self):
        for cmd, cwd, want in self.SHELL_WRITES:
            a = self._agent()
            self.bash(a, cmd, cwd=cwd, error=False)
            got = [p for w in a.shell_writes for p in w['paths']]
            self.assertEqual(got, want, cmd)
            self.assertEqual(a.writes, [], cmd)                                       # the page's list of written files is the tools' alone

    def test_the_result_of_the_call_settles_a_shell_write(self):
        a = self._agent()
        self.bash(a, 'cat > /x/r1/A.md <<EOF\nx\nEOF', call='t1')
        self.bash(a, 'cat > /x/r1/B.md <<EOF\nx\nEOF', call='t2', error=False)
        self.bash(a, 'cat > /x/r1/C.md <<EOF\nx\nEOF', call='t3', error=True)
        self.assertEqual([(w['paths'], w.get('ok')) for w in a.shell_writes], [(['/x/r1/A.md'], None), (['/x/r1/B.md'], True), (['/x/r1/C.md'], False)])

    def test_a_call_with_no_markdown_redirect_is_not_a_shell_write(self):
        a = self._agent()
        for cmd in ('ls -la', 'echo hi > /tmp/x.log', 'cat /x/r1/B.md', 'tee /tmp/x.log'):
            self.bash(a, cmd, error=False)
        self.assertEqual(a.shell_writes, [])                       # the page's list of shell writes is markdown alone, as it was
        self.assertEqual(a.write_events, [])                       # a file in a scratch folder is no work, and a read is no write
        self.assertEqual([(r.path, r.via) for r in a.read_events], [('/x/r1/B.md', 'shell')])      # (what `cat` reads is a read, and the command window says so)
        self.assertEqual([w.reads for w in a.windows], [(), (), ('/x/r1/B.md',), ()])

    def test_pending_text_len(self):
        a = self._agent()
        for n in (1, 159, 160, 161, 239, 240, 241, 300):
            a.pending.clear()
            a.feed({'type': 'assistant', 'timestamp': '2026-09-30T00:00:00Z', 'message': {'id': 'm%d' % n, 'content': [
                {'type': 'tool_use', 'id': 'tu', 'name': 'Bash', 'input': {'command': 'x', 'description': 'd' * n}}]}})
            self.assertEqual(a.pending['tu']['text'], server.trunc('d' * n, 160))
            self.assertEqual(a.activity[-1]['text'], server.trunc('d' * n, 240))


class Status(unittest.TestCase):
    def _sess(self):
        s = server.Session.__new__(server.Session)
        return s

    def test_stall_boundary(self):
        s = self._sess()
        a = server.Agent('a0123456789abcdef', {})
        a.last_ts = 1000.0
        self.assertEqual(s.agent_status(a, True, 1000.0 + server.STALL_SEC), 'running')
        self.assertEqual(s.agent_status(a, True, 1000.0 + server.STALL_SEC + 1), 'stalled')
        self.assertEqual(s.agent_status(a, False, 1000.0 + 5), 'ended')

    def test_waiting_tool_keeps_running(self):
        s = self._sess()
        a = server.Agent('a0123456789abcdef', {})
        a.last_ts = 1000.0
        a.pending['t'] = {'ts': 1000.0, 'name': 'Bash', 'text': 'docker run'}
        self.assertEqual(s.agent_status(a, True, 1000.0 + server.STALL_SEC + 100), 'running')
        self.assertEqual(s.agent_status(a, True, 1000.0 + server.TOOL_STALL_SEC + 1), 'stalled')

    def test_notification_done(self):
        """The end notice is read from the launcher's record (the Ledger of the session), as the board gets it: the task-notification line of the main record."""
        s = self._sess()
        s.ledger = runstate.Ledger()
        a = server.Agent('a0123456789abcdef', {})
        a.last_ts = 1000.0
        self.assertEqual(s.agent_status(a, True, 1100.0), 'running')
        s.ledger.feed({'type': 'user', 'timestamp': '1970-01-01T00:16:41.000Z', 'origin': {'kind': 'task-notification'}, 'message': {'role': 'user', 'content': (
            '<task-notification>\n<task-id>a0123456789abcdef</task-id>\n<status>completed</status>\n<summary>Agent "x" completed</summary>\n</task-notification>')}})
        self.assertEqual(s.agent_status(a, True, 1100.0), 'done')


if __name__ == '__main__':
    unittest.main()
