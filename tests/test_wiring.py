"""The pure judgments (who launched a child, the debate units and seats, the run state) wired into the board (sessions, views, the HTTP layer, the diagnostics).

What is checked here is the wiring, not the judgments: a run that hit a limit shows as `interrupted/limit` with its reset time and raises one grouped notice, the
orchestrator that waits for the reset is `limit_wait`, a grand-child hangs under its launcher, `units` keeps its meaning while `work_units` carries the wider
one, a debate judgment is reused until something it read has changed, a file the agent failed to write seats nobody, the diagnostics carry codes and numbers and
never text, and the response leaves in one write. The generator (tools/scenarios) is the wide net: the cells of the coupling bundle, which only the wiring can
make green, are checked at the end.

    python3 -m unittest tests.test_wiring
"""
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
import test_runstate as rst  # noqa: E402  (small record builders)
import test_stage1 as st1  # noqa: E402
import test_stage2 as st2  # noqa: E402
from board import affil, catalog, diag, lineage, runstate as RS, sessions, views  # noqa: E402

T0 = st2.T0
SECRET = 'SECRET-INSTRUCTION-TEXT-do-not-leak'


def dump(d):
    return json.dumps(d, separators=(',', ':'))


def lines_of(*records):
    return [dump(r) for r in records]


def child_user(t, text='hi'):
    return dump({'type': 'user', 'timestamp': st2.iso(t), 'cwd': '/w', 'message': {'role': 'user', 'content': text}})


def err_line(t, resets, status=429):
    return dump(dict(rst.api_err(t, status=status, resets=resets), sessionId='x', cwd='/w'))


def say_line(t, text='done'):
    return dump({'type': 'assistant', 'timestamp': st2.iso(t), 'cwd': '/w', 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn',
                                                                                         'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 1, 'output_tokens': 1}}})


def cost_line(sid='x', total=1000):
    return dump({'type': 'cost-state', 'sessionId': sid, 'totalDuration': total})


# ---------------------------------------------------------------------------------------------------------------------
class OneWrite(unittest.TestCase):
    """The headers and the body go out in a single write: two writes make a body past 64 KB wait for the client's delayed ACK (~200 ms)."""

    def send(self, body, version='HTTP/1.1', **kw):
        h = server.Handler.__new__(server.Handler)
        h.wfile, h.headers, h.request_version, h.requestline, h.command = io.BytesIO(), {}, version, 'GET / %s' % version, 'GET'
        writes = []
        h.wfile.write = lambda b: writes.append(bytes(b)) or len(b)
        with mock.patch.object(server.Handler, 'log_request', lambda *a, **k: None):
            h._send(200, body, **kw)
        return writes

    def test_one_write_for_headers_and_body(self):
        for size in (10, 5000, 400000):
            body = {'x': 'y' * size}
            writes = self.send(body)
            self.assertEqual(len(writes), 1, size)
            head, _, payload = writes[0].partition(b'\r\n\r\n')
            self.assertTrue(head.startswith(b'HTTP/1.'), head[:20])
            self.assertEqual(json.loads(payload), body)
            self.assertIn(b'Content-Length: %d' % len(payload), head)

    def test_bytes_body_and_http09(self):
        writes = self.send(b'static bytes', ctype='text/plain; charset=utf-8')
        self.assertEqual((len(writes), writes[0].endswith(b'\r\n\r\nstatic bytes')), (1, True))
        writes = self.send(b'old client', version='HTTP/0.9')
        self.assertEqual(writes, [b'old client'])                                # an HTTP/0.9 request gets the body alone, as before


# ---------------------------------------------------------------------------------------------------------------------
class Fixture(st2.CliFixture):
    """A real Session over a fake HOME; the process table is the one the test says."""
    C1, C2, GC = '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444'

    def state(self, s, alive=True):
        with mock.patch.object(server.Session, 'alive', lambda self_: (alive, 123 if alive else None, 'n')), patched(claude_alive_ids=lambda: set()):
            return s.state()


class StatusWiring(Fixture):
    def test_a_limit_stopped_child_is_interrupted_with_its_reset_time(self):
        resets = T0 + 7200
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"')])
        self.write(self.C1, [child_user(T0 + 5), err_line(T0 + 9, resets)])
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        st = self.state(s)
        a = next(x for x in st['agents'] if x['id'] == self.C1)
        self.assertEqual((a['status'], a['reason'], a['resets_at']), ('interrupted', 'limit', resets))
        self.assertEqual((a['origin'], a['node'], a['parent'], a['by']), ('cli', None, None, [self.PARENT, None]))      # who handed its (one) run its instruction
        self.assertEqual((a['link']['rule_class'], a['link']['tree']), ('guess', self.PARENT))
        kinds = [x['id'].split(':')[0] for x in st['alerts']]
        self.assertEqual((kinds.count('limit'), kinds.count('fail')), (1, 0))             # a limit is one grouped notice, not a failure
        lim = next(x for x in st['alerts'] if x['id'].startswith('limit:'))
        self.assertEqual((lim['id'], lim['title_params']['agents'], lim['title_params']['at']), ('limit:%d' % resets, 1, resets))
        self.assertEqual(lim['title_i18n'], {'key': 'alert.limit.title', 'params': {'at': resets}})      # the key says which sentence; the page words the time in its own zone

    def test_a_content_guess_tells_why_it_is_one(self):
        c = server.Agent(self.C1, {})
        c.origin, c.cli = 'cli', {'rule': 'content_short', 'sid': self.PARENT}
        for label, dec, want in (('a short instruction', types.SimpleNamespace(incomplete=False, assumed=False), (False, False)),
                                 ('a comparison cut short', types.SimpleNamespace(incomplete=True, assumed=False), (True, False)),
                                 ('a launching script nobody could read', types.SimpleNamespace(incomplete=False, assumed=True), (False, True)),
                                 ('both', types.SimpleNamespace(incomplete=True, assumed=True), (True, True)),
                                 ('no decision of this run', None, (False, False))):
            with self.subTest(label), mock.patch.object(views.LINKS, 'decisions', {self.C1: dec} if dec else {}):
                link = views.link_of(None, c)
                self.assertEqual((link['rule'], link['rule_class'], link['incomplete'], link['assumed']), ('content_short', 'guess') + want)
        a = server.Agent('a0000000000000001', {})
        a.origin = 'subagent'
        self.assertNotIn('assumed', views.link_of(None, a))                                 # only a `claude -p` run has a decision to tell

    def test_two_agents_on_one_limit_are_one_notice(self):
        resets = T0 + 7200
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"', tid='t1'), st2.bash_line(T0 + 1, 'cd /w && claude -p "$Q"', tid='t2')])
        self.write(self.C1, [child_user(T0 + 5), err_line(T0 + 9, resets)])
        self.write(self.C2, [child_user(T0 + 6), err_line(T0 + 10, resets)])
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        st = self.state(s)
        self.assertEqual({a['status'] for a in st['agents']}, {'interrupted'})
        lim = [x for x in st['alerts'] if x['id'].startswith('limit:')]
        self.assertEqual((len(lim), lim[0]['title_params']['agents']), (1, 2))
        self.assertEqual(st['diag']['by_code'].get('limit_group'), 1)

    def test_a_child_that_ended_cleanly_and_one_closed_without_an_end(self):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"', tid='t1'), st2.bash_line(T0 + 1, 'cd /w && claude -p "$Q"', tid='t2')])
        self.write(self.C1, [child_user(T0 + 5), say_line(T0 + 9), cost_line()])
        self.write(self.C2, [child_user(T0 + 6), cost_line()])                           # the process ended (cost-state) in the middle of a turn
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        st = self.state(s)
        by = {a['id']: a for a in st['agents']}
        self.assertEqual(by[self.C1]['status'], 'done')
        self.assertEqual((by[self.C2]['status'], by[self.C2]['reason']), ('interrupted', 'exited'))      # nobody is blamed for it
        self.assertEqual([r['kind'] for r in by[self.C1]['runs']], ['first'])

    def test_the_orchestrator_waiting_for_a_limit_reset(self):
        resets = T0 + 3600
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': st2.iso(T0), 'cwd': '/w', 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': 'go'}}),
                                 err_line(T0 + 5, resets)])
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        st = self.state(s)
        self.assertEqual((st['orch']['state'], st['orch']['resets_at'], st['orch']['auto']), ('limit_wait', resets, False))
        self.assertFalse([e for e in st['feed'] if e['kind'] == 'orch_say'])             # the limit text is news, not something it said to the user
        self.assertEqual([x['level'] for x in st['alerts'] if x['id'].startswith('limit:')], ['check'])
        gone = self.state(s, alive=False)                                                 # the process is gone: it is not waiting for anything
        self.assertNotEqual(gone['orch']['state'], 'limit_wait')

    def test_without_a_process_table_nothing_is_said_about_a_process(self):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"')])
        self.write(self.C1, [child_user(T0 + 5), say_line(T0 + 9)])
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        with mock.patch('board.procs.table_known', lambda: False):
            st = self.state(s)
        a = next(x for x in st['agents'] if x['id'] == self.C1)
        self.assertEqual(a['status'], 'done')                                            # an end turn is an end turn
        self.assertIn('proc_unknown', st['diag']['by_code'])


class ResumedRun(Fixture):
    """A `claude -p` child that is resumed by a later call: that call's TaskStop reaches the judgment (the link names the call of every run)."""

    def make(self, stop_task='bgB'):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p hi', tid='tA'), st2.result_line(T0 + 10, tid='tA'),
                                 st2.bash_line(T0 + 100, 'cd /w && claude -p --resume %s again' % self.C1, tid='tB'),
                                 dump({'type': 'user', 'timestamp': st2.iso(T0 + 101), 'cwd': '/w', 'toolUseResult': {'backgroundTaskId': 'bgB'},
                                       'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tB', 'content': 'started'}]}}),
                                 dump({'type': 'assistant', 'timestamp': st2.iso(T0 + 130), 'cwd': '/w', 'message': {'role': 'assistant', 'content': [
                                     {'type': 'tool_use', 'id': 'tS', 'name': 'TaskStop', 'input': {'task_id': stop_task}}]}})])
        tool = dump({'type': 'assistant', 'timestamp': st2.iso(T0 + 104), 'cwd': '/w', 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': 'tc', 'name': 'Bash', 'input': {'command': 'sleep 600'}}], 'usage': {'input_tokens': 1, 'output_tokens': 1}}})
        self.write(self.C1, [child_user(T0 + 5), say_line(T0 + 9), cost_line(), child_user(T0 + 102, 'again'), tool])
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        return s

    def test_the_link_names_the_call_of_each_run(self):
        s = self.make()
        o = self.links.cli_owners[self.C1]
        self.assertEqual((o['calls'], o['calls_certain']), (['tA', 'tB'], [True, True]))
        self.assertEqual(s.launch_calls(s.agents[self.C1].cli), ['tA', 'tB'])

    def test_a_taskstop_on_the_resuming_call_stops_the_child(self):
        st = self.state(self.make())
        a = next(x for x in st['agents'] if x['id'] == self.C1)
        self.assertEqual((a['status'], a['reason']), ('killed', 'stopped'))                # only the resuming call's job was stopped, and it is the last run's
        self.assertEqual([r['kind'] for r in a['runs']], ['first', 'process'])
        self.assertEqual(a['by'], [self.PARENT, None])

    def test_a_taskstop_of_another_job_does_not(self):
        st = self.state(self.make(stop_task='bgOther'))
        a = next(x for x in st['agents'] if x['id'] == self.C1)
        self.assertNotEqual(a['status'], 'killed')

    def test_only_the_calls_the_evidence_names_count(self):
        pick = server.Session.launch_calls
        self.assertEqual(pick({'call': 'c1', 'calls': ['c1', 'c2', None], 'calls_certain': [True, True, False]}), ['c1', 'c2'])
        self.assertEqual(pick({'call': None, 'calls': ['p1'], 'calls_certain': [False]}), [])           # only paired by order: a stop found there could be another child's
        self.assertEqual(pick({'call': 'c1', 'calls': [None], 'calls_certain': [False]}), ['c1'])      # the call the evidence named for the first run
        self.assertEqual(pick({'call': 'c1'}), ['c1'])                                                  # a link without per-run calls
        self.assertEqual(pick({}), [])


class TailRestart(Fixture):
    def test_a_record_that_shrinks_is_counted_once(self):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"')])
        first = [child_user(T0 + 5), say_line(T0 + 9), cost_line(), child_user(T0 + 20, 'again'), say_line(T0 + 24), cost_line(2000)]
        path = self.write(self.C1, first)
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        a = s.agents[self.C1]
        self.assertEqual(len(a.runs.runs), 2)
        with open(path, 'w') as f:
            f.write('\n'.join(first[:3]) + '\n')                                         # rewritten shorter: the tail starts over from the beginning
        s.poll()
        self.assertEqual(len(a.runs.runs), 1)                                            # rebuilt from the lines, not continued on top of the old runs


class WrongShapedLine(Fixture):
    """A line whose fields have the wrong type is skipped and counted; it never stops the session from opening (it used to raise out of `poll`, and with it out of the
    server's start when the session was the default one)."""
    SHAPES = {
        'message is a string': {'type': 'assistant', 'timestamp': T0, 'cwd': '/w', 'message': 'plain string'},
        'timestamp is a number': {'type': 'assistant', 'timestamp': 12345, 'cwd': '/w', 'message': {'role': 'assistant', 'content': []}},
        'timestamp is a list': {'type': 'system', 'subtype': 'x', 'timestamp': [1], 'cwd': '/w'},
    }

    def lines(self, bad):
        bad = dict(bad, timestamp=st2.iso(T0) if bad['timestamp'] == T0 else bad['timestamp'])
        return [say_line(T0, 'first'), dump(bad), say_line(T0 + 3, 'second')]

    def test_poll_skips_the_line_and_counts_it(self):
        for n, (name, bad) in enumerate(self.SHAPES.items()):
            with self.subTest(name):
                sid = '55555555-5555-4555-8555-5555555555%02d' % n
                self.write(sid, self.lines(bad))
                s = server.Session(os.path.join(self.proj, sid + '.jsonl'))
                s.poll()
                self.assertGreaterEqual(s.parse_errors, 1)
                self.assertTrue(any(e['text'] == 'second' for e in s.feed), 'the line after it is read')

    def test_the_registry_opens_a_session_that_has_such_a_line(self):
        self.write(self.PARENT, self.lines(self.SHAPES['message is a string']))
        self.scan()                                                                      # the registry waits for the first link scan
        self.assertIsNotNone(catalog.Registry().get(self.PARENT))

    def test_a_subagent_record_with_such_a_line_is_skipped_too(self):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"')])
        self.write(self.C1, self.lines(self.SHAPES['message is a string']) + [say_line(T0 + 9)])
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        self.assertGreaterEqual(s.parse_errors, 1)
        self.assertIn(self.C1, s.agents)


class ChildTitleLater(Fixture):
    """A child that a remembered line (or a live environment) places is shown at the first screen, before the sub-agents' records are read; the call that started it is in one of them.
    When the link names the call, the child's title, its start and its spawn event (the title, the launcher) are made again from it."""
    WORDS = ('Draft the release notes from the settled rulings, grouped by package, and keep the migration notes for the end where the readers of the changelog will look for them '
             'first; mention every renamed option and every removed flag.')
    SUB = 'a' + 'b' * 16

    def make(self):
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': st2.iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        d = os.path.join(self.proj, self.PARENT, 'subagents')
        os.makedirs(d)
        with open(os.path.join(d, 'agent-%s.jsonl' % self.SUB), 'w') as f:
            f.write(st2.bash_line(T0 - 2, 'cd /w && claude -p "%s"' % self.WORDS, tid='toolu_sub', desc='Draft the release notes in a separate run') + '\n'
                    + st2.result_line(T0 + 30, tid='toolu_sub') + '\n')
        with open(os.path.join(d, 'agent-%s.meta.json' % self.SUB), 'w') as f:
            json.dump({'description': 'helper', 'agentType': 'general-purpose', 'toolUseId': 'toolu_x'}, f)
        self.write(self.C1, [child_user(T0, self.WORDS), say_line(T0 + 20)])
        path = os.path.join(os.path.realpath(self.tmp.name), 'cache', 'links.json')
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o700)
        with open(path, 'w') as fh:
            json.dump({'version': lineage.CACHE_VERSION, 'links': [{'child': self.C1, 'parent': self.PARENT, 'kind': 'cli', 'rule': 'env', 'seen': time.time() - 100, 'started': T0}]}, fh)
        os.chmod(path, 0o600)
        self.links.lineage.enable_cache(path)
        self.links.deep_inline = False                              # the server's way: the first scan is stage 1 alone
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        return s

    def test_the_title_and_the_spawn_event_are_made_again_when_the_call_is_known(self):
        s = self.make()
        o = self.links.cli_owners[self.C1]
        self.assertEqual((o['rule'], o['call'], o['bash_desc']), ('file', None, ''))               # stage 1: the remembered line places it, the sub-agent's call is not read yet
        a = s.agents[self.C1]
        spawn = next(e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.C1)
        self.assertEqual((a.description, a.title, spawn['title'], spawn['from'], spawn.get('title_i18n', {}).get('key')), ('claude -p', 'claude -p', 'Claude Code 실행', 'orch', 'event.spawn_cli.title'))
        with mock.patch('time.time', lambda: T0 + 600):
            self.links.scan_deep()
        o = self.links.cli_owners[self.C1]
        self.assertEqual((o['rule'], o['node'], o['call'], o['bash_desc']), ('file', self.SUB, 'toolu_sub', 'Draft the release notes in a separate run'))
        s.poll()
        a = s.agents[self.C1]
        self.assertEqual((a.description, a.title, a.spawn_ts), ('Draft the release notes in a separate run', 'Draft the release notes in a separate run', o['bash_ts']))
        spawn = next(e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.C1)
        self.assertEqual((spawn['title'], spawn['from'], 'title_i18n' in spawn, 'title_is_default' in spawn), ('Draft the release notes in a separate run', self.SUB, False, False))
        by = {x['id']: x for x in self.state(s)['agents']}
        self.assertEqual(by[self.C1]['title'], 'Draft the release notes in a separate run')

    def test_nothing_is_made_again_while_nothing_changed(self):
        s = self.make()
        with mock.patch('time.time', lambda: T0 + 600):
            self.links.scan_deep()
        s.poll()
        before = [dict(e) for e in s.feed]
        s.poll()
        self.assertEqual(s.feed, before)                                                          # nothing is made again while nothing changed


class GrandChild(Fixture):
    def test_a_grandchild_hangs_under_the_child_that_launched_it(self):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p hi')])
        self.write(self.C1, [child_user(T0 + 5, 'hi'), st2.bash_line(T0 + 6, 'cd /w && claude -p sub', tid='toolu_g'), say_line(T0 + 40)])
        self.write(self.GC, [child_user(T0 + 8, 'sub'), say_line(T0 + 12)])
        self.scan()
        self.assertEqual(self.links.cli_owners[self.GC]['sid'], self.C1)                  # the judgment: the tree of the grandchild is the child
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        s.poll()
        self.assertEqual(set(s.agents), {self.C1, self.GC})                               # the top page shows it
        self.assertEqual((s.launcher_of(s.agents[self.C1]), s.launcher_of(s.agents[self.GC])), ('orch', self.C1))
        st = self.state(s)
        by = {a['id']: a for a in st['agents']}
        self.assertEqual((by[self.C1]['parent'], by[self.GC]['parent'], by[self.GC]['node'], by[self.GC]['link']['tree']), (None, self.C1, None, self.C1))
        spawn = next(e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.GC)
        self.assertEqual(spawn['from'], self.C1)                                         # the event says who launched it; a direct child still says 'orch'
        self.assertEqual(next(e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.C1)['from'], 'orch')


# ---------------------------------------------------------------------------------------------------------------------
class OrchestratorStateWiring(Fixture):
    """What the page says about the orchestrator itself, read from the main record of a session and from its process."""

    def orch(self, n, records, alive=True):
        sid = '%08d-0000-4000-8000-000000000000' % n
        self.write(sid, [dump(r) for r in records])
        s = server.Session(os.path.join(self.proj, sid + '.jsonl'))
        s.poll()
        return self.state(s, alive)

    def test_a_print_session_that_ended_its_turn_is_not_working(self):
        records = (rst.sdk(1), rst.tool(2, 'c'), rst.result(3, 'c'), rst.say(5))              # entrypoint sdk-cli: the record has no turn_duration
        for n, alive in enumerate((True, False, None)):
            self.assertEqual(self.orch(n, records, alive)['orch']['state'], 'idle', alive)

    def test_a_session_of_local_commands_is_not_working(self):
        for n, (records, alive) in enumerate((((rst.typed(1), rst.say(5), rst.turn_end(5.5)) + tuple(rst.usage_command(100)), True),     # a /usage after a normal turn end
                                              (tuple(rst.usage_command(10)), True),                                                    # nothing but commands
                                              (tuple(rst.usage_command(10)) + (rst.cost(),), None))):                                  # and the process that wrote them has exited
            st = self.orch(10 + n, records, alive)
            self.assertEqual(st['orch']['state'], 'idle', n)
        self.assertEqual(st['orch']['last_ts'], None)                                          # a command and its output are no activity of the orchestrator

    def test_a_process_that_is_gone_is_not_working(self):
        records = (rst.sdk(1), rst.tool(2, 'c'))                                                # the last turn stopped in the middle (it was killed)
        self.assertEqual([self.orch(20 + n, records, alive)['orch']['state'] for n, alive in enumerate((True, False, None))], ['working', 'idle', 'working'])

    def test_an_exit_marker_ends_the_turn_even_when_the_process_cannot_be_seen(self):
        self.assertEqual(self.orch(30, (rst.sdk(1), rst.tool(2, 'c'), rst.result(3, 'c'), rst.cost()), None)['orch']['state'], 'idle')

    def test_a_turn_that_ends_in_a_question_asks_for_the_answer_only_while_the_session_is_there(self):
        for n, alive, want in ((40, True, ['decide']), (41, None, ['decide']), (42, False, [])):      # a session that is closed has nobody to answer: unknown is not closed
            st = self.orch(n, (rst.sdk(1), rst.say(5, 'Which of the two designs do you want?')), alive)
            self.assertEqual([a['level'] for a in st['alerts'] if a['id'].startswith('say:')], want, alive)
        for n, alive, want in ((43, True, ['decide']), (44, False, [])):                              # the same for a typed session that was closed
            st = self.orch(n, (rst.typed(1), rst.say(5, 'Which of the two designs do you want?'), rst.turn_end(5.5)), alive)
            self.assertEqual([a['level'] for a in st['alerts'] if a['id'].startswith('say:')], want, alive)

    def test_the_wait_for_the_next_instruction_is_only_for_a_session_that_is_there(self):
        for n, alive, want in ((45, True, ['info']), (46, None, ['info']), (47, False, [])):
            st = self.orch(n, (rst.sdk(1), rst.say(5, 'The work is done.')), alive)
            self.assertEqual([a['level'] for a in st['alerts'] if a['id'].startswith('turn:')], want, alive)

    def test_a_turn_that_started_right_after_the_end_is_not_waiting_for_anything(self):
        records = (rst.sdk(1), rst.say(100, 'Which one do you want?'), rst.sdk(100.2, 'The first one.', idx=2), rst.tool(100.4, 'c'))
        st = self.orch(48, records, True)
        self.assertEqual((st['orch']['state'], [a['id'] for a in st['alerts'] if a['id'].split(':')[0] in ('say', 'turn')]), ('working', []))

    def test_the_orchestrator_is_working_during_a_call_that_started_right_after_the_end_of_a_turn(self):
        records = (rst.sdk(1), rst.say(100), rst.sdk(100.2, 'Next.', idx=2), rst.tool(100.4, 'c'))
        self.assertEqual([self.orch(60 + n, records, alive)['orch']['state'] for n, alive in enumerate((True, None, False))], ['working', 'working', 'idle'])
        typed = (rst.typed(1), rst.say(100), rst.turn_end(100.05), rst.typed(100.2, 'Next.'), rst.tool(100.4, 'c'))
        self.assertEqual(self.orch(70, typed, True)['orch']['state'], 'working')

    def test_what_comes_after_the_end_of_a_turn_is_work(self):
        records = (rst.sdk(1), rst.say(5), rst.typed(100, 'And the second part.'))
        self.assertEqual(self.orch(50, records, True)['orch']['state'], 'working')


class CodexThreadState(unittest.TestCase):
    """A Codex conversation is its own orchestrator: working while a turn is open, unless the process that has it open is known to be gone."""

    def test_a_thread_whose_process_is_gone_is_not_working(self):
        import test_preserve as pre
        with tempfile.TemporaryDirectory() as tmp, patched(CODEX_NAMES=os.path.join(tmp, 'none.jsonl')):
            c = server.CodexSession(dict(pre.codex_entry(), path=os.path.join(tmp, 'rollout.jsonl'), origin='tui', guardian=False, first_user='hi'))
            c._feed_cx(pre.cx_rec('event_msg', 'task_started', {}, ts=pre.T + 1))
            got = []
            for alive in (True, False, None):
                with mock.patch.object(server.CodexSession, 'alive', lambda self_, a=alive: (a, None, 'n')):
                    got.append(views.state(c)['orch']['state'])
            self.assertEqual(got, ['working', 'idle', 'working'])                              # unknown is not gone
            c._feed_cx(pre.cx_rec('event_msg', 'task_complete', {}, ts=pre.T + 30))
            with mock.patch.object(server.CodexSession, 'alive', lambda self_: (True, None, 'n')):
                self.assertEqual(views.state(c)['orch']['state'], 'idle')
            c._feed_cx(pre.cx_rec('event_msg', 'task_started', {}, ts=pre.T + 30.3))                   # the next turn starts within a second of the end of this one
            with mock.patch.object(server.CodexSession, 'alive', lambda self_: (True, None, 'n')):
                self.assertEqual(views.state(c)['orch']['state'], 'working')
                self.assertTrue(c._codex_busy({}, pre.T + 3600))
            c._feed_cx(pre.cx_rec('event_msg', 'task_complete', {}, ts=pre.T + 60))
            with mock.patch.object(server.CodexSession, 'alive', lambda self_: (True, None, 'n')):
                self.assertEqual(views.state(c)['orch']['state'], 'idle')


# ---------------------------------------------------------------------------------------------------------------------
class DebateFixture(unittest.TestCase):
    """A debate folder on disk and sub-agents that write, read or fail to write its reports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(t, 'home')
        self.repo = os.path.join(self.home, 'work', 'repo')
        self.rev = os.path.join(self.repo, 'docs', 'rev')
        os.makedirs(os.path.join(self.repo, '.git'))
        os.makedirs(os.path.join(self.rev, 'r1'))
        with open(os.path.join(self.rev, 'brief.md'), 'w') as f:
            f.write('# Review\n\n**A — Compatibility**\n**B — Ergonomics**\n')
        with open(os.path.join(self.rev, 'r1', 'A.md'), 'w') as f:
            f.write('report A\n')
        self.proj = os.path.join(self.home, '.claude', 'projects', '-w')
        os.makedirs(self.proj)
        os.makedirs(os.path.join(self.home, '.codex', 'sessions'))
        self.sid = '55555555-5555-4555-8555-555555555555'
        start = patched(HOME=self.home, CLAUDE_HOME=os.path.join(self.home, '.claude'), PROJECTS=os.path.join(self.home, '.claude', 'projects'),
                        CODEX_HOME=os.path.join(self.home, '.codex'), CODEX_SESSIONS=os.path.join(self.home, '.codex', 'sessions'),
                        CODEX_NAMES=os.path.join(self.home, '.codex', 'session_index.jsonl'), CODEX=server.CodexIndex(), LINKS=server.LinkIndex())
        start.__enter__()
        self.addCleanup(start.__exit__, None, None, None)
        self.sub = os.path.join(self.proj, self.sid, 'subagents')
        os.makedirs(self.sub)
        self.calls = []

    def agent(self, n, desc, prompt, steps):
        """steps: [(kind, args)]: write ok/fail, read."""
        aid = 'a%016x' % n
        tu = 'toolu_%d' % n
        self.calls.append({'type': 'tool_use', 'id': tu, 'name': 'Agent', 'input': {'description': desc, 'prompt': prompt}})
        with open(os.path.join(self.sub, 'agent-%s.meta.json' % aid), 'w') as f:
            json.dump({'description': desc, 'toolUseId': tu}, f)
        rows = [dump({'type': 'user', 'timestamp': st2.iso(T0 + n), 'cwd': self.repo, 'message': {'role': 'user', 'content': prompt}})]
        for i, (kind, path) in enumerate(steps):
            t, cid = T0 + n + 1 + i, 'tu%d_%d' % (n, i)
            name = 'Read' if kind == 'read' else 'Write'
            inp = {'file_path': path} if kind == 'read' else {'file_path': path, 'content': 'x'}
            rows.append(dump({'type': 'assistant', 'timestamp': st2.iso(t), 'cwd': self.repo, 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use',
                                                                                                    'content': [{'type': 'tool_use', 'id': cid, 'name': name, 'input': inp}]}}))
            rows.append(dump({'type': 'user', 'timestamp': st2.iso(t + 0.5), 'cwd': self.repo, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': cid, 'content': 'err' if kind == 'fail' else 'ok', **({'is_error': True} if kind == 'fail' else {})}]}}))
        with open(os.path.join(self.sub, 'agent-%s.jsonl' % aid), 'w') as f:
            f.write('\n'.join(rows) + '\n')
        return aid

    def session(self, cwd=None):
        main = os.path.join(self.proj, self.sid + '.jsonl')
        with open(main, 'w') as f:
            f.write(dump({'type': 'assistant', 'timestamp': st2.iso(T0), 'cwd': cwd or self.repo, 'message': {'role': 'assistant', 'content': self.calls}}) + '\n')
        s = server.Session(main)
        s.poll()
        s.poll()
        return s

    def state(self, s):
        with patched(claude_alive_ids=lambda: set()), mock.patch.object(server.Session, 'alive', lambda self_: (False, None, None)):
            return s.state()


class UnitsAndSeats(DebateFixture):
    def test_units_is_where_it_sits_and_work_units_is_where_it_works(self):
        a = os.path.join(self.rev, 'r1', 'A.md')
        w = self.agent(1, 'A writer', 'Write your round 1 report to `%s`.' % a, [('write', a)])
        r = self.agent(2, 'X reader', 'Read the reports in `%s` and comment.' % os.path.join(self.rev, 'r1'), [('read', a)])
        f = self.agent(3, 'B failed', 'Do your review of the topic.', [('fail', os.path.join(self.rev, 'r1', 'B.md'))])      # no word on where to write: only its write, and it failed
        st = self.state(self.session())
        by = {x['id']: x for x in st['agents']}
        unit = self.rev
        self.assertEqual((by[w]['units'], by[w]['work_units']), ([unit], [unit]))
        self.assertEqual((by[r]['units'], by[r]['work_units']), ([], [unit]))              # a reader is on the work, not in the debate's seats
        self.assertEqual(by[f]['units'], [])                                              # a write that failed seats nobody
        cells = {c['agent']: c['state'] for d in st['debates'] for t in d['topics'] for row in t['rows'] for c in row['cells'] if c['agent']}
        self.assertIn(w, cells)
        self.assertNotIn(r, cells)
        self.assertNotIn(f, cells)

    def test_a_write_knows_whether_it_worked_only_after_its_result(self):
        a = server.Agent('a0123456789abcdef', {})
        use = {'type': 'assistant', 'timestamp': st2.iso(T0), 'message': {'content': [{'type': 'tool_use', 'id': 'w1', 'name': 'Write', 'input': {'file_path': '/x/r1/A.md'}}]}}
        a.feed(use)
        self.assertNotIn('ok', a.writes[0])
        a.feed({'type': 'user', 'timestamp': st2.iso(T0 + 1), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'w1', 'is_error': True, 'content': 'no'}]}})
        self.assertIs(a.writes[0]['ok'], False)
        use['message']['content'][0]['id'] = 'w2'
        a.feed(use)
        a.feed({'type': 'user', 'timestamp': st2.iso(T0 + 2), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'w2', 'content': 'fine'}]}})
        self.assertIs(a.writes[1]['ok'], True)

    def test_a_message_knows_whether_it_was_delivered_only_after_its_result(self):
        a = server.Agent('a0123456789abcdef', {})
        send = lambda cid, t: {'type': 'assistant', 'timestamp': st2.iso(t), 'message': {'content': [
            {'type': 'tool_use', 'id': cid, 'name': 'SendMessage', 'input': {'to': 'b', 'summary': 'hello', 'message': 'hi'}}]}}
        result = lambda cid, t, **kw: {'type': 'user', 'timestamp': st2.iso(t), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': cid, 'content': 'x', **kw}]}}
        a.feed(send('m1', T0))
        self.assertEqual((a.sent[0]['id'], 'ok' in a.sent[0]), ('m1', False))
        a.feed(result('m1', T0 + 1, is_error=True))
        self.assertIs(a.sent[0]['ok'], False)
        a.feed(send('m2', T0 + 2))
        a.feed(result('m2', T0 + 3))
        self.assertIs(a.sent[1]['ok'], True)
        a.feed(send('m3', T0 + 4))
        a.feed(result('other', T0 + 5, is_error=True))                                           # the result of some other call says nothing about it
        self.assertNotIn('ok', a.sent[2])

    def test_the_debate_judgment_is_made_again_when_the_result_of_a_message_arrives(self):
        s = server.Session.__new__(server.Session)
        a = server.Agent('a0123456789abcdef', {})
        s.agents = {a.id: a}
        a.feed({'type': 'assistant', 'timestamp': st2.iso(T0), 'message': {'content': [{'type': 'tool_use', 'id': 'm1', 'name': 'SendMessage', 'input': {'to': 'b', 'message': 'hi'}}]}})
        before = s._debate_key({a.id: 'running'})
        a.feed({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'm1', 'is_error': True, 'content': 'no'}]}})       # no time: nothing else about the agent moves
        self.assertNotEqual(s._debate_key({a.id: 'running'}), before)                           # so the key itself must carry the result

    def test_the_notice_of_a_background_task_is_no_agent_finishing(self):
        a = server.Agent('a0123456789abcdef', {})
        note = '<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n<summary>Background command done</summary>\n</task-notification>'
        for tid in ('b3k9x2m1q', 'a1234567890abcdef'):
            a._child_note(T0, note % tid, None)
        self.assertEqual([n['task'] for n in a.child_notes], ['a1234567890abcdef'])


class FileViewNeedsAWriteThatWorked(DebateFixture):
    """The page opens a file an agent wrote. A write counts when its result said it worked: a call that failed or has no result yet opens nothing. An entry kept at
    completion (a Codex file change, a report confirmed by its hash) has no call to wait for and counts as made."""

    def md(self, name):
        path = os.path.join(self.repo, name)
        with open(path, 'w') as f:
            f.write('notes\n')
        return path

    def use(self, a, cid, path, t, name='Write'):
        a.feed({'type': 'assistant', 'timestamp': st2.iso(t), 'message': {'content': [{'type': 'tool_use', 'id': cid, 'name': name, 'input': {'file_path': path}}]}})

    def done(self, a, cid, t, **kw):
        a.feed({'type': 'user', 'timestamp': st2.iso(t), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': cid, 'content': 'x', **kw}]}})

    def test_only_a_write_that_worked_opens_a_file(self):
        s = self.session()
        a = server.Agent('a0123456789abcdef', {})
        s.agents[a.id] = a
        failed, pending, worked, edited = (self.md(n) for n in ('failed.md', 'pending.md', 'worked.md', 'edited.md'))
        self.use(a, 'w1', failed, T0)
        self.done(a, 'w1', T0 + 1, is_error=True)
        self.use(a, 'w2', pending, T0 + 2)
        self.use(a, 'w3', worked, T0 + 3)
        self.done(a, 'w3', T0 + 4)
        self.use(a, 'w4', edited, T0 + 5, name='Edit')
        self.done(a, 'w4', T0 + 6)
        self.assertEqual([s.allowed_file(p) for p in (failed, pending)], [None, None])
        self.assertEqual([s.allowed_file(p) for p in (worked, edited)], [os.path.realpath(worked), os.path.realpath(edited)])
        self.done(a, 'w2', T0 + 7)                                                              # the result of the one in progress comes
        self.assertEqual(s.allowed_file(pending), os.path.realpath(pending))

    def test_a_record_made_at_completion_counts(self):
        s = self.session()
        a = server.Agent('a0123456789abcdef', {})
        s.agents[a.id] = a
        made = self.md('made.md')
        a.writes.append({'ts': T0, 'path': made})                                               # a Codex FileChange, or the -o file found by its hash: no call, no result to wait for
        self.assertEqual(s.allowed_file(made), os.path.realpath(made))


class ResumedSubAgent(DebateFixture):
    """A sub-agent that is sent back to work and ends again in the same words (a notice without usage twice): the second notice is the one that says it is done,
    and it is read from the launcher's Ledger, which keeps it (a notice is one only when it matches in words, usage or time)."""

    def notice(self, aid, t):
        return dump({'type': 'user', 'timestamp': st2.iso(t), 'cwd': self.repo, 'origin': {'kind': 'task-notification'}, 'message': {'role': 'user', 'content': (
            '<task-notification>\n<task-id>%s</task-id>\n<status>failed</status>\n<summary>Agent "A job" failed: stopped</summary>\n</task-notification>' % aid)}})

    def test_the_second_end_notice_counts(self):
        aid = self.agent(1, 'A job', 'do the work', [('read', os.path.join(self.rev, 'r1', 'A.md'))])
        with open(os.path.join(self.sub, 'agent-%s.jsonl' % aid), 'a') as f:                    # it works again later
            f.write(dump({'type': 'assistant', 'timestamp': st2.iso(T0 + 400), 'cwd': self.repo, 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'id': 'again', 'name': 'Read', 'input': {'file_path': os.path.join(self.rev, 'r1', 'A.md')}}]}}) + '\n')
            f.write(dump({'type': 'user', 'timestamp': st2.iso(T0 + 401), 'cwd': self.repo, 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'again', 'content': 'ok'}]}}) + '\n')
        s = self.session()
        with open(s.path, 'a') as f:
            f.write(self.notice(aid, T0 + 20) + '\n' + self.notice(aid, T0 + 405) + '\n')           # the same words, 385 s apart, no usage in either
        s.poll()
        self.assertEqual(len(s.ledger.agent_notes(aid)), 2)                                      # the Ledger keeps both (the page's own notice card still shows one: same words, no usage)
        a = next(x for x in self.state(s)['agents'] if x['id'] == aid)
        self.assertEqual(a['status'], 'failed')                                                  # the notice that came after its last record is the one that counts


class DebateCache(DebateFixture):
    def test_a_judgment_is_reused_until_something_it_read_changes(self):
        a = os.path.join(self.rev, 'r1', 'A.md')
        self.agent(1, 'A writer', 'Write your round 1 report to `%s`.' % a, [('write', a)])
        s = self.session()
        st = {aid: 'running' for aid in s.agents}
        first = s.judged(st)
        with mock.patch('glob.glob', side_effect=AssertionError('listed again')), mock.patch('os.listdir', side_effect=AssertionError('listed again')):
            self.assertIs(s.judged(st), first)                                           # nothing changed: only stats, no listing
        with open(os.path.join(self.rev, 'r1', 'B.md'), 'w') as f:                        # a file made by someone else, no record of any agent moved
            f.write('b\n')
        second = s.judged(st)
        self.assertIsNot(second, first)
        self.assertIn('B', {r['p'] for d in second.debates for t in d['topics'] for r in t['rows']})
        self.assertIsNot(s.judged({aid: 'done' for aid in s.agents}), second)            # a status moved
        done = {aid: 'done' for aid in s.agents}
        with mock.patch.object(sessions, 'DEBATE_TTL', -1):                              # a judgment is also made again after DEBATE_TTL seconds, whatever the stats say
            self.assertIsNot(s.judged(done), s.judged(done))

    def bash(self, aid, n, command, result=None, t=None):
        """One more Bash call in the record of the sub-agent `aid` (and its result, `result` being the call's is_error), at time `t`: a call made in the same instant as the
        agent's last record does not move the agent's last time, so the judgment has to see it by the call itself."""
        t, cid = T0 + 50 + n if t is None else t, 'bash%d' % n
        rows = [dump({'type': 'assistant', 'timestamp': st2.iso(t), 'cwd': self.repo, 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': cid, 'name': 'Bash', 'input': {'command': command}}]}})]
        if result is not None:
            rows.append(dump({'type': 'user', 'timestamp': st2.iso(t), 'cwd': self.repo, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': cid, 'content': 'err' if result else 'ok', **({'is_error': True} if result else {})}]}}))
        with open(os.path.join(self.sub, 'agent-%s.jsonl' % aid), 'a') as f:
            f.write('\n'.join(rows) + '\n')

    def test_a_debate_only_a_shell_write_names_is_judged_again_without_waiting(self):
        b = os.path.join(self.rev, 'r1', 'B.md')
        aid = self.agent(1, 'B writer', 'Do the review you were asked for.', [('read', os.path.join(self.home, 'notes.txt'))])     # its last record is at T0 + 2.5
        s = self.session()
        st = {aid: 'running'}
        first = s.judged(st)
        self.assertEqual(first.debates, [])                                                 # nothing names the debate yet
        last = s.agents[aid].last_ts
        with open(b, 'w') as f:
            f.write('report B\n')
        self.bash(aid, 1, "cat > %s <<'EOF'\nreport B\nEOF" % b, False, t=last)             # the only thing that names it is a shell write
        s.poll()
        self.assertEqual(s.agents[aid].last_ts, last)
        second = s.judged(st)                                                               # no DEBATE_TTL to wait for: the record grew, and no folder it had looked at moved
        self.assertIsNot(second, first)
        cells = {r['p']: r['cells'][0] for d in second.debates for t in d['topics'] for r in t['rows']}
        self.assertEqual(cells['B']['agent'], aid)

    def test_a_shell_write_that_fails_after_its_file_was_there_is_judged_again(self):
        b = os.path.join(self.rev, 'r1', 'B.md')
        aid = self.agent(1, 'B writer', 'Do the review you were asked for.', [('read', os.path.join(self.home, 'notes.txt'))])
        with open(b, 'w') as f:
            f.write('report B\n')
        s = self.session()
        last = s.agents[aid].last_ts
        self.bash(aid, 1, "cat > %s <<'EOF'\nreport B\nEOF" % b, t=last)                    # no result yet, the file is there
        s.poll()
        st = {aid: 'running'}
        first = s.judged(st)
        self.assertEqual(next(r for d in first.debates for t in d['topics'] for r in t['rows'] if r['p'] == 'B')['cells'][0]['agent'], aid)
        with open(os.path.join(self.sub, 'agent-%s.jsonl' % aid), 'a') as f:                # the call turns out to have failed, in the same instant
            f.write(dump({'type': 'user', 'timestamp': st2.iso(last), 'cwd': self.repo, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'bash1', 'content': 'err', 'is_error': True}]}}) + '\n')
        s.poll()
        self.assertEqual(s.agents[aid].last_ts, last)
        second = s.judged(st)
        self.assertIsNot(second, first)                                                      # the file and the folders on disk are the same: only the call's result moved
        row = next(r for d in second.debates for t in d['topics'] for r in t['rows'] if r['p'] == 'B')
        self.assertIsNone(row['cells'][0]['agent'])                                          # a write that failed seats nobody

    def test_the_diagnostics_of_the_judgment_follow_a_reused_one(self):
        a = os.path.join(self.rev, 'r1', 'A.md')
        self.agent(1, 'A writer', 'Write your round 1 report to `%s`.' % a, [('write', a)])
        s = self.session()
        st = {aid: 'running' for aid in s.agents}
        jd = s.judged(st)
        s.debate_diag = []
        self.assertIs(s.judged(st), jd)
        self.assertIs(s.debate_diag, jd.diag)

    def test_a_debate_no_record_names_is_found_by_the_walk(self):
        other = os.path.join(self.repo, 'docs', 'other')
        os.makedirs(os.path.join(other, 'r1'))
        with open(os.path.join(other, 'brief.md'), 'w') as f:
            f.write('# Other\n')
        a = os.path.join(self.rev, 'r1', 'A.md')
        self.agent(1, 'A writer', 'Write your round 1 report to `%s`.' % a, [('write', a)])
        s = self.session()
        before = {d['root'] for d in self.state(s)['debates']}
        self.assertNotIn(other, before)
        s.walk_repos()
        self.assertEqual((s.walked_units, s.walk_gen), (sorted([other, self.rev]), 1))
        self.assertIn(other, {d['root'] for d in self.state(s)['debates']})
        s.walk_repos()
        self.assertEqual(s.walk_gen, 1)                                                  # the same list: nothing to judge again


    def test_a_working_folder_that_is_no_repository_is_not_walked(self):
        plain = os.path.join(self.home, 'work', 'notes')
        os.makedirs(os.path.join(plain, 'docs', 'other', 'r1'))
        with open(os.path.join(plain, 'docs', 'other', 'brief.md'), 'w') as f:
            f.write('# Other\n')
        s = self.session(cwd=plain)
        self.assertEqual(s.cwd, plain)
        s.walk_repos()
        self.assertEqual(s.walked_units, [])                                             # a folder that is no repository top is not a place to look through
        s2 = self.session(cwd=os.path.join(self.repo, 'docs'))                           # inside a repository: its top is walked, the debate there is found
        s2.walk_repos()
        self.assertIn(self.rev, s2.walked_units)

    def test_the_background_walk_runs_when_the_server_turns_it_on(self):
        other = os.path.join(self.repo, 'docs', 'other')
        os.makedirs(os.path.join(other, 'r1'))
        a = os.path.join(self.rev, 'r1', 'A.md')
        self.agent(1, 'A writer', 'Write your round 1 report to `%s`.' % a, [('write', a)])
        s = self.session()
        s.poll()
        self.assertIsNone(s._walk_thread)                                                # off unless the server asked for it (tests, tools)
        later = sessions.LaterWork()
        later.open()
        with mock.patch.object(sessions, 'WALK_ENABLED', True), mock.patch.object(sessions, 'LATER', later):
            s._walk_at = -1e9
            s.poll()
            self.assertIsNotNone(s._walk_thread)
            s._walk_thread.join(10)
            s.poll()                                                                     # a walk is not started again within WALK_EVERY seconds
        self.assertIn(other, s.walked_units)
        self.assertEqual(s.walk_gen, 1)


class LaterGate(DebateFixture):
    """The second link pass and the walk of the repository folders wait for the first picture (or LATER_AFTER seconds): they must not compete with the first read of a session."""

    def gate(self, start=True):
        later = sessions.LaterWork()
        fake = types.SimpleNamespace(calls=0)
        if start:
            fake.start_deep = lambda: setattr(fake, 'calls', fake.calls + 1)
        return later, fake

    def test_the_second_pass_starts_once_when_the_gate_opens(self):
        later, fake = self.gate()
        later.armed = True
        with mock.patch.object(sessions, 'LINKS', fake):
            self.assertEqual(fake.calls, 0)
            later.open()
            later.open()
        self.assertEqual((fake.calls, later.is_open()), (1, True))

    def test_nothing_starts_unless_the_server_armed_it(self):
        later, fake = self.gate()
        with mock.patch.object(sessions, 'LINKS', fake):
            later.open()
        self.assertEqual((fake.calls, later.is_open()), (0, True))

    def test_an_index_without_a_second_pass_or_one_that_fails_does_not_matter(self):
        later, fake = self.gate(start=False)
        later.armed = True
        with mock.patch.object(sessions, 'LINKS', fake):
            later.open()
        later2, fake2 = self.gate()
        later2.armed = True
        fake2.start_deep = mock.Mock(side_effect=RuntimeError('boom'))
        with mock.patch.object(sessions, 'LINKS', fake2), mock.patch('builtins.print') as pr:
            later2.open()
        self.assertTrue(pr.called)
        self.assertTrue(later.is_open() and later2.is_open())

    def test_the_latest_start_opens_it_when_no_picture_is_ever_asked_for(self):
        later, fake = self.gate()
        fake.ready = threading.Event()
        fake.ready.set()
        with mock.patch.object(sessions, 'LINKS', fake):
            later.tick()                                                                 # not armed: nothing happens
            self.assertFalse(later.is_open())
            later.arm(0.05)
            later.tick()
            self.assertFalse(later.is_open())                                            # the wait has only begun
            time.sleep(0.1)
            later.tick()
            later.tick()
        self.assertEqual((later.is_open(), fake.calls), (True, 1))

    def test_the_latest_start_counts_from_the_first_link_scan(self):
        later, fake = self.gate()
        fake.ready = threading.Event()                                                   # the first scan of a big HOME can take longer than the wait
        with mock.patch.object(sessions, 'LINKS', fake):
            later.arm(0.05)
            later.tick()
            time.sleep(0.1)
            later.tick()
            self.assertFalse(later.is_open())
            fake.ready.set()
            later.tick()
            self.assertFalse(later.is_open())                                            # counted from now, not from the arming
            time.sleep(0.1)
            later.tick()
        self.assertEqual((later.is_open(), fake.calls), (True, 1))

    def test_the_registry_loop_keeps_the_latest_start(self):
        class Stop(Exception):
            pass
        later = mock.Mock()
        with mock.patch.object(catalog, 'LATER', later), mock.patch.object(catalog.LINKS, 'scan', lambda: None), \
                mock.patch.object(catalog.time, 'sleep', side_effect=Stop):
            with self.assertRaises(Stop):
                catalog.Registry().loop()
        later.tick.assert_called_once_with()

    def test_the_first_state_opens_it_and_a_later_one_does_not_start_it_again(self):
        later, fake = self.gate()
        later.armed = True
        s = self.session()
        with mock.patch.object(sessions, 'LATER', later), mock.patch.object(sessions.LINKS, 'start_deep', fake.start_deep, create=True):
            self.assertFalse(later.is_open())
            self.state(s)
            self.assertEqual((later.is_open(), fake.calls), (True, 1))
            self.state(s)
        self.assertEqual(fake.calls, 1)

    def test_the_registry_warm_up_opens_it_even_when_the_picture_fails(self):
        later, fake = self.gate()
        with mock.patch.object(sessions, 'LATER', later), mock.patch.object(catalog, 'LATER', later), \
                mock.patch.object(catalog.views, 'state', side_effect=RuntimeError('boom')), mock.patch('builtins.print'):
            catalog.Registry._warm(mock.Mock(id='x'))
        self.assertTrue(later.is_open())

    def test_the_walk_waits_for_the_gate(self):
        a = os.path.join(self.rev, 'r1', 'A.md')
        self.agent(1, 'A writer', 'Write your round 1 report to `%s`.' % a, [('write', a)])
        s = self.session()
        later = sessions.LaterWork()
        with mock.patch.object(sessions, 'WALK_ENABLED', True), mock.patch.object(sessions, 'LATER', later):
            s._walk_at = -1e9
            s.poll()
            self.assertIsNone(s._walk_thread)                                            # the first picture is not out yet
            later.open()
            s.poll()
            self.assertIsNotNone(s._walk_thread)
            s._walk_thread.join(10)


# ---------------------------------------------------------------------------------------------------------------------
class Diagnostics(Fixture):
    def make(self):
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"')])
        torn = dump({'type': 'assistant', 'timestamp': st2.iso(T0 + 7), 'cwd': '/w', 'sessionId': 'x', 'uuid': 'u1', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': SECRET}]}})
        self.write(self.C1, [child_user(T0 + 5, SECRET), torn[:30] + say_line(T0 + 9), cost_line()])      # half a record glued to the next one
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        return s

    def test_api_diag_has_codes_and_numbers_never_text(self):
        s = self.make()
        with mock.patch.object(server.Session, 'alive', lambda self_: (True, 1, 'n')), patched(claude_alive_ids=lambda: set()):
            code, body = st1.call('/api/diag?session=' + self.PARENT, s)
            _, state = st1.call('/api/state?session=' + self.PARENT, s)
        self.assertEqual(code, 200)
        self.assertEqual((body['session'], body['n'], body['capped']), (self.PARENT, len(body['items']), False))
        self.assertIn('torn_lines', {e['code'] for e in body['items']})
        for e in body['items']:
            self.assertEqual(set(e), {'code', 'level', 'scope', 'agent', 'unit', 'params'})
            self.assertIn(e['level'], ('warn', 'info'))
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn(SECRET, json.dumps(state['diag']))
        self.assertEqual(set(state['diag']), {'n', 'warn', 'info', 'by_code', 'capped'})   # /api/state carries counts, not entries
        self.assertEqual(state['diag']['n'], body['n'])

    def test_every_value_is_short_plain_data(self):
        s = self.make()
        self.state(s)
        for e in s._diag:
            for v in e['params'].values():
                for x in (v if isinstance(v, list) else [v]):
                    self.assertTrue(x is None or isinstance(x, (bool, int, float)) or (isinstance(x, str) and len(x) <= diag.VALUE_MAX), (e['code'], x))
        self.assertEqual(diag._clean({'a': 'x' * 500, 'b': {'c': 1}, 'c': [1, 2], 'd': object(), 'e': ['y' * 500]}), {'c': [1, 2]})

    def test_a_link_cache_that_cannot_be_trusted_is_said(self):
        s = self.make()
        cache = os.path.join(self.tmp.name, 'cache', 'links.json')
        os.makedirs(os.path.dirname(cache), mode=0o777)
        os.chmod(os.path.dirname(cache), 0o777)                                          # others can write to the folder: the cache is neither read nor written
        with mock.patch.object(diag.LINKS.lineage, 'cache', cache):
            entries = diag.collect(s, time.time(), {}, [])
        self.assertEqual([e['params'] for e in entries if e['code'] == 'cache_error'], [{'what': 'untrusted'}])
        os.chmod(os.path.dirname(cache), 0o700)
        with mock.patch.object(diag.LINKS.lineage, 'cache', cache):
            self.assertFalse([e for e in diag.collect(s, time.time(), {}, []) if e['code'] == 'cache_error'])

    def test_at_most_200_entries_per_session(self):
        s = self.make()
        many = [{'code': 'content_only', 'subject': self.C1, 'tree': self.PARENT, 'node': None} for _ in range(500)]
        with mock.patch.object(diag.LINKS, 'diags', many):
            entries = diag.collect(s, time.time(), {}, [])
        self.assertEqual(len(entries), diag.MAX_PER_SESSION)
        self.assertTrue(diag.counts(entries)['capped'])


# ---------------------------------------------------------------------------------------------------------------------
class RunBoundaryParity(unittest.TestCase):
    """affil.RunStarts is the temporary copy of the run boundary that link.py still uses; board/runstate.py is the rule. They must say the same runs until the copy goes."""
    SEQS = {
        'one run': [rst.sdk(10, 'first'), rst.say(12)],
        'resumed after cost-state': [rst.sdk(10, 'first', 1), rst.say(12), rst.cost(), rst.sdk(30, 'again', 2), rst.say(33), rst.cost(2000)],
        'turn index keeps rising in one process': [rst.sdk(10, 'first', 1), rst.say(12), rst.sdk(30, 'second', 2), rst.say(33)],
        'a notice and a peer line are no run': [rst.sdk(10, 'first', 1), rst.say(12),
                                                rst.line('user', 20, message={'role': 'user', 'content': '<task-notification>\n<task-id>b1</task-id>\n</task-notification>'}),
                                                rst.line('user', 21, isMeta=True, origin={'kind': 'peer'}, message={'role': 'user', 'content': 'hello from a peer'}), rst.say(22)],
        'a slash command': [rst.sdk(10, '<command-name>/init</command-name> text', 1), rst.say(12), rst.cost(), rst.sdk(40, 'next', 2)],
    }

    def test_same_boundaries(self):
        for name, seq in self.SEQS.items():
            rs, tr = affil.RunStarts(), RS.RunTracker('s1')
            for d in seq:
                rs.feed(json.dumps(d))
                tr.feed(d)
            self.assertEqual(len(rs.runs), len(tr.runs), name)                         # the same number of runs ...
            for mine, theirs in zip(rs.runs, tr.runs):
                if theirs.turns:                                                       # ... and the same instruction opens each (a slash command's expansion opens a run without a turn)
                    self.assertEqual(round(mine.ts), round(theirs.turns[0].ts), name)


# ---------------------------------------------------------------------------------------------------------------------
class GeneratorGuard(unittest.TestCase):
    """The cells that only the wiring can make green, from the scenario generator: the coupling bundle (a participant launched by a sub-agent or a child: its link,
    state, cell and seat) and the state bundle's status, reason, reset time and orchestrator state. A red cell here is a regression of the wiring."""

    def run_cells(self, glob, fields):
        from tools.scenarios import run
        import fnmatch
        cases = [c for c in run.select() if fnmatch.fnmatch(c.id, glob)]
        cells, errors = run.run_all(cases)
        self.assertEqual(errors, [])
        self.assertGreater(len(cases), 50)
        return [(c.key, c.result, c.ws, c.gs) for c in cells if c.field in fields and c.result != 'pass']

    def test_coupling_bundle_is_green(self):
        fields = {'tree', 'rule_class', 'node', 'status', 'reason', 'resets_at', 'unit', 'round', 'seat', 'role', 'cell'}
        red = self.run_cells('cpl:*', fields)
        self.assertEqual(red, [])

    def test_state_bundle_judgments_are_green(self):
        red = self.run_cells('sta:*', {'status', 'reason', 'resets_at', 'orch_state'})
        self.assertEqual(red, [])


if __name__ == '__main__':
    unittest.main()
