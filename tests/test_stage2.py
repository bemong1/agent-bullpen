"""Tests of `claude -p` sessions launched through Bash (= subagents) and of the static file list.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server, start_patches  # noqa: E402
from board.link import certain as link_certain  # noqa: E402

T0 = 1790000000.0


def dump(d):
    return json.dumps(d, separators=(',', ':'))   # no spaces, like a real transcript (LinkIndex and _head first filter on the raw text)


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.%03dZ' % int((t % 1) * 1000)


def bash_line(t, cmd, cwd='/w', tid='toolu_1', desc='Launch child'):
    return dump({'type': 'assistant', 'timestamp': iso(t), 'cwd': cwd,
                       'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tid, 'name': 'Bash',
                                                                     'input': {'command': cmd, 'description': desc}}]}})


def result_line(t, tid='toolu_1', cwd='/w'):
    """The foreground result of a Bash call: the moment the launched process is over for the parent (it bounds the call, so a later session is not its child)."""
    return dump({'type': 'user', 'timestamp': iso(t), 'cwd': cwd, 'toolUseResult': {'stdout': 'ok', 'stderr': '', 'interrupted': False},
                 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': 'ok'}]}})


def child_lines(t, cwd='/w', stop='end_turn', model='claude-sonnet-5-5', text='hi'):
    return [dump({'type': 'user', 'timestamp': iso(t), 'cwd': cwd, 'message': {'role': 'user', 'content': text}}),
            dump({'type': 'assistant', 'timestamp': iso(t + 4), 'cwd': cwd,
                        'message': {'model': model, 'stop_reason': stop, 'content': [{'type': 'text', 'text': 'hello'}],
                                    'usage': {'input_tokens': 10, 'output_tokens': 5}}})]


class CliFixture(unittest.TestCase):
    """A fake HOME (~/.claude/projects/<proj>/<sid>.jsonl), a new LinkIndex and an empty CodexIndex."""
    PARENT = '11111111-1111-4111-8111-111111111111'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(t, 'home')
        self.proj = os.path.join(self.home, '.claude', 'projects', '-w')
        os.makedirs(self.proj)
        os.makedirs(os.path.join(self.home, '.codex', 'sessions'))
        self.links = server.LinkIndex()
        start_patches(self, HOME=self.home, CLAUDE_HOME=os.path.join(self.home, '.claude'),
                      PROJECTS=os.path.join(self.home, '.claude', 'projects'),
                      CODEX_HOME=os.path.join(self.home, '.codex'),
                      CODEX_SESSIONS=os.path.join(self.home, '.codex', 'sessions'),
                      CODEX_NAMES=os.path.join(self.home, '.codex', 'session_index.jsonl'),
                      CODEX=server.CodexIndex(), LINKS=self.links)

    def write(self, sid, lines):
        p = os.path.join(self.proj, sid + '.jsonl')
        with open(p, 'a') as f:
            f.write('\n'.join(lines) + '\n')
        return p

    def scan(self):
        self.links.scan()
        return self.links.cli_owners


class StaticFiles(unittest.TestCase):
    def test_new_files_listed(self):
        for path, (name, ctype) in {'/board.css': ('board.css', 'text/css; charset=utf-8'),
                                    '/board.js': ('board.js', 'text/javascript; charset=utf-8'),
                                    '/common.js': ('common.js', 'text/javascript; charset=utf-8'),
                                    '/game-art.js': ('game-art.js', 'text/javascript; charset=utf-8'),
                                    '/game-demo.js': ('game-demo.js', 'text/javascript; charset=utf-8')}.items():
            self.assertEqual(server.STATIC_FILES[path], (name, ctype), path)
        for path in ('/', '/index.html', '/game', '/game.js'):
            self.assertIn(path, server.STATIC_FILES)

    def test_missing_file_is_404(self):
        sent = []
        h = server.Handler.__new__(server.Handler)
        h.headers = {'Host': 'localhost:8790'}
        h.path = '/board.js'
        h._send = lambda code, body, ctype=None: sent.append((code, body))
        with tempfile.TemporaryDirectory() as empty, patched(STATIC=empty):
            h.do_GET()
        self.assertEqual(sent, [(404, {'error': 'not found', 'error_code': 'not_found'})])


class Item21LaunchRe(unittest.TestCase):
    def test_yes(self):
        for c in ('claude -p "x"', 'cd /a && claude --print "x"', 'timeout 600 claude -p x', 'nohup claude -p x &',
                  'for i in 1 2; do claude -p "$i"; done', 'env A=1 B=2 claude -p x', 'claude --model m --effort high -p x',
                  'y=$(claude -p x)', 'cd /a\nclaude -p x'):
            self.assertTrue(server.CLAUDE_LAUNCH_RE.search(c), c)

    def test_no(self):
        for c in ('claude', 'claude --version', 'pgrep claude', 'echo claude -p x', 'ls | grep claude -p', 'codex exec "x"',
                  'claude mcp list', 'ps aux | grep -- -p'):
            self.assertFalse(server.CLAUDE_LAUNCH_RE.search(c), c)


class Item21CliLine(CliFixture):
    def lines(self, cmd, cwd='/w', desc='d'):
        f = {'pos': 0, 'sid': self.PARENT, 'calls': [], 'cli': []}
        self.links._cli_line(f, bash_line(T0, cmd, cwd, desc=desc).encode())
        return f['cli']

    def test_cwd_from_cd_or_line(self):
        self.assertEqual(self.lines('claude -p hi')[0]['cwd'], '/w')
        self.assertEqual(self.lines('cd /x && claude -p hi')[0]['cwd'], '/x')
        self.assertEqual(self.lines('cd /a && cd /b && claude -p hi')[0]['cwd'], '/b')
        self.assertEqual(self.lines('cd ~/q; claude -p hi')[0]['cwd'], os.path.expanduser('~/q'))

    def test_cd_at_line_head(self):
        self.assertEqual(self.lines('echo start\ncd /x\nclaude -p hi')[0]['cwd'], '/x')
        self.assertEqual(self.lines('cd /x\n\ncd /y\nclaude --print hi')[0]['cwd'], '/y')

    def test_every_launch_is_a_candidate(self):
        got = self.lines('cd /a && claude -p one\ncd /b && claude -p two')
        self.assertEqual([c['cwd'] for c in got], ['/a', '/b'])
        self.assertEqual({c['id'] for c in got}, {'toolu_1'})
        self.assertEqual(got[0]['desc'], 'd')

    def test_not_a_launch(self):
        self.assertEqual(self.lines('claude --version'), [])
        f = {'pos': 0, 'sid': self.PARENT, 'calls': [], 'cli': []}
        self.links._cli_line(f, dump({'type': 'user', 'timestamp': iso(T0), 'message': {'content': 'claude -p'}}).encode())
        self.assertEqual(f['cli'], [])


class Item21CodexCd(unittest.TestCase):
    """The shell working folder of a Codex run also honours a cd at the head of a line (in a real transcript the ownership stays as it is; only the scwd of that one call changes)."""

    def test_cd_at_line_head(self):
        L = server.cx_launches('echo a\ncd /x\ncodex exec "do it"', {}, '/w')
        self.assertEqual((L[0]['scwd'], L[0]['cwd']), ('/x', '/x'))
        L = server.cx_launches('cd /x && codex exec "do it"', {}, '/w')
        self.assertEqual(L[0]['scwd'], '/x')
        L = server.cx_launches('echo "\ncd /x\n"; codex exec "do it"', {}, '/w')
        self.assertEqual(L[0]['scwd'], '/w')          # a cd inside quotes is not executed


class Item21Link(CliFixture):
    C1, C2 = '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333'

    def test_window_and_cwd(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p hi')])
        self.write(self.C1, child_lines(T0 + 5))
        own = self.scan()
        self.assertEqual(set(own), {self.C1})
        self.assertEqual(own[self.C1]['sid'], self.PARENT)
        self.assertEqual(own[self.C1]['dt'], 5.0)
        self.assertEqual(own[self.C1]['bash_desc'], 'Launch child')
        self.assertEqual(self.links.cli_owned_by(self.PARENT).keys(), {self.C1})
        self.assertEqual(self.links.cli_count(self.PARENT), 1)
        self.assertEqual(self.links.cli_owner(self.C1)['sid'], self.PARENT)
        self.assertIsNone(self.links.cli_owner(self.PARENT))

    def test_after_the_call_ended_or_other_cwd(self):
        """There is no 60 s window: a call is the parent of a child while that call is running (until its result, plus 10 s). A child that starts after
        the result came, or in another working folder than the call's, is not its child."""
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p hi'), result_line(T0 + 2)])
        self.write(self.C1, child_lines(T0 + 61))                # long after the result (a window of 60 s is not what decides it)
        self.write(self.C2, child_lines(T0 + 5, cwd='/other'))   # different working folder
        self.assertEqual(self.scan(), {})

    def test_slow_child_of_a_running_call_is_linked(self):
        """A call whose result has not come is still running, so a child that started a minute or two later is its child (it was beyond the old 60 s window)."""
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p hi')])
        self.write(self.C1, child_lines(T0 + 121))
        own = self.scan()
        self.assertEqual(set(own), {self.C1})
        self.assertEqual(own[self.C1]['call'], 'toolu_1')

    def test_before_call_is_not_a_child(self):
        self.write(self.PARENT, [bash_line(T0, 'claude -p hi')])
        self.write(self.C1, child_lines(T0 - 3))
        self.assertEqual(self.scan(), {})

    def test_one_child_per_call(self):
        """Each call has its own running period, so each child goes to the one call that was running when it started."""
        self.write(self.PARENT, [bash_line(T0, 'claude -p hi', tid='t1'), result_line(T0 + 5, tid='t1'),
                                 bash_line(T0 + 20, 'claude -p hi', tid='t2'), result_line(T0 + 25, tid='t2')])
        self.write(self.C1, child_lines(T0 + 4))
        self.write(self.C2, child_lines(T0 + 22))
        own = self.scan()
        self.assertEqual({k: v['call'] for k, v in own.items()}, {self.C1: 't1', self.C2: 't2'})

    def test_two_calls_that_both_fit_are_held(self):
        """Two calls running at the same time that both fit the same child: the call is held, not given to the earlier call by start order.
        The tree is the same for both calls, so the child is still the session's, as a guess."""
        self.write(self.PARENT, [bash_line(T0, 'claude -p hi', tid='t1'), bash_line(T0 + 1, 'claude -p hi', tid='t2')])
        self.write(self.C1, child_lines(T0 + 4))
        self.write(self.C2, child_lines(T0 + 6))
        own = self.scan()
        self.assertEqual({k: (v['sid'], v['call'], link_certain(v['rule'])) for k, v in own.items()},
                         {self.C1: (self.PARENT, None, False), self.C2: (self.PARENT, None, False)})

    def test_no_cwd_constraint_when_call_cwd_unknown(self):
        self.write(self.PARENT, [bash_line(T0, 'claude -p hi', cwd=None)])
        self.write(self.C1, child_lines(T0 + 2, cwd='/anything'))
        self.assertEqual(set(self.scan()), {self.C1})

    def test_no_calls_clears(self):
        self.write(self.C1, child_lines(T0))
        self.assertEqual(self.scan(), {})


class Item21Session(CliFixture):
    C1 = '22222222-2222-4222-8222-222222222222'

    def open(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p hi')])
        self.child_path = self.write(self.C1, child_lines(T0 + 5))
        self.scan()
        return server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))

    def test_poll_adds_child_agent(self):
        s = self.open()
        s.poll()
        a = s.agents[self.C1]
        self.assertEqual((a.origin, a.spawn_ts, a.description), ('cli', T0, 'Launch child'))
        self.assertEqual(a.cli['sid'], self.PARENT)
        self.assertEqual(s.agent_tails[self.C1].path, self.child_path)
        self.assertEqual(a.model, 'claude-sonnet-5-5')
        spawns = [e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.C1]
        self.assertEqual(len(spawns), 1)
        self.assertEqual((spawns[0]['from'], spawns[0]['title'], spawns[0]['ts']), ('orch', 'Launch child', T0))
        s.poll()
        self.assertEqual(len([e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.C1]), 1)

    def test_state_lists_child(self):
        s = self.open()
        s.poll()
        with patched(claude_alive_ids=lambda: set()):
            st = s.state()
        ag = {a['id']: a for a in st['agents']}
        self.assertEqual((ag[self.C1]['origin'], ag[self.C1]['status'], ag[self.C1]['provider']), ('cli', 'done', 'claude'))

    def test_status_follows_process(self):
        s = self.open()
        s.poll()
        a = s.agents[self.C1]
        now = a.last_ts + 5
        self.assertEqual(s.agent_status(a, False, now), 'done')           # the process has ended, last stop reason end_turn
        s._cli_alive = {self.C1}
        self.assertEqual(s.agent_status(a, False, now), 'running')        # still alive after the end of a turn (even if the parent session has ended)
        self.assertEqual(s.agent_status(a, False, a.last_ts + server.STALL_SEC + 1), 'stalled')
        self.write(self.C1, [dump({'type': 'assistant', 'timestamp': iso(T0 + 30), 'cwd': '/w', 'message': {
            'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'tu9', 'name': 'Bash', 'input': {'command': 'ls'}}],
            'usage': {'input_tokens': 1, 'output_tokens': 1}}})])
        s.poll()
        s._cli_alive = set()
        self.assertEqual(s.agent_status(a, False, a.last_ts + 5), 'ended')          # the process is gone in the middle of a turn: it crashed, it did not finish
        s._cli_alive = {self.C1}
        self.assertEqual(s.agent_status(a, False, a.last_ts + server.STALL_SEC + 1), 'running')          # a call that waits for its result is not a stall
        self.assertEqual(s.agent_status(a, False, a.last_ts + server.TOOL_STALL_SEC + 1), 'stalled')

    def test_list_sessions_hides_child_and_counts(self):
        self.open()
        with patched(claude_alive_ids=lambda: set()):
            out = server.list_sessions()
        ids = {x['id']: x for x in out}
        self.assertNotIn(self.C1, ids)
        self.assertEqual(ids[self.PARENT]['agents'], 1)

    def test_child_with_no_file_is_skipped(self):
        s = self.open()
        os.remove(self.child_path)
        s.poll()
        self.assertNotIn(self.C1, s.agents)


class Stage2Helpers(unittest.TestCase):
    """Helper functions shared by Claude and Codex (model_numbers, pending_or_stall, etc.). The visible results are checked by test_preserve.py."""

    def test_model_numbers(self):
        ag = [types.SimpleNamespace(id=i, m=m) for i, m in enumerate(['a', 'b', 'a', 'a', 'b'])]
        self.assertEqual(server.model_numbers(ag, lambda x: x.m), {0: 'a', 1: 'b', 2: 'a-2', 3: 'a-3', 4: 'b-2'})
        self.assertEqual(server.model_numbers([], lambda x: x.m), {})

    def test_pending_or_stall(self):
        a = types.SimpleNamespace(pending={})
        self.assertEqual(server.pending_or_stall(a, 100.0, 100.0 + server.STALL_SEC), 'running')
        self.assertEqual(server.pending_or_stall(a, 100.0, 100.0 + server.STALL_SEC + 1), 'stalled')
        a.pending = {'x': {'ts': 100.0}, 'y': {'ts': None}}
        self.assertEqual(server.pending_or_stall(a, 100.0, 100.0 + server.TOOL_STALL_SEC - 1), 'running')
        self.assertEqual(server.pending_or_stall(a, 100.0, 100.0 + server.TOOL_STALL_SEC), 'stalled')

    def test_codex_call(self):
        self.assertEqual(server.codex_call('custom_tool_call', {'input': 'tools.exec_command({cmd: "ls"})'}), ('exec_command', 'ls'))
        self.assertEqual(server.codex_call('function_call', {'name': 'shell', 'arguments': 'x' * 300}), ('shell', server.trunc('x' * 300, 240)))
        self.assertEqual(server.codex_call('function_call', {}), ('function', ''))

    def test_status_label(self):
        self.assertEqual({k: server.STATUS_LABEL.get(k, k) for k in ('completed', 'done', 'failed', 'killed', 'stopped')},
                         {'completed': '작업 끝', 'done': '작업 끝', 'failed': '실패', 'killed': '중지됨', 'stopped': 'stopped'})

    def test_orch_tool_truncates_to_160(self):
        s = server.Session.__new__(server.Session)
        s.orch = {}
        s._orch_tool(5.0, 'Bash', 'x' * 400)
        self.assertEqual(s.orch, {'last_action': 'Bash: ' + server.trunc('x' * 400, 160), 'last_action_ts': 5.0})

    def test_user_say_raw_keeps_angle_text(self):
        s = server.Session.__new__(server.Session)
        s.feed, s.user_seen = [], {}
        s._user_say(1.0, '<x>hi</x>')
        s._user_say(2.0, '<x>hi</x>', raw=True)
        self.assertEqual([e['text'] for e in s.feed], ['<x>hi</x>'])

    def test_read_head_cache_follows_mtime_and_size(self):
        s = server.Session.__new__(server.Session)
        s._head_cache = {}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'brief.md')
            self.assertEqual(server.read_head(s, p), '')
            with open(p, 'w') as f:
                f.write('# A\n')
            self.assertEqual(server.read_head(s, p), '# A\n')
            with mock.patch('builtins.open', side_effect=AssertionError('read again')):
                self.assertEqual(server.read_head(s, p), '# A\n')            # the same (mtime, size) is not read again
            with open(p, 'w') as f:
                f.write('# BB\n')
            self.assertEqual(server.read_head(s, p), '# BB\n')
            os.remove(p)
            self.assertEqual(server.read_head(s, p), '')
            self.assertNotIn(p, s._head_cache)


if __name__ == '__main__':
    unittest.main()
