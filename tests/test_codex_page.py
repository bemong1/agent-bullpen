"""The page of a Codex orchestrator: its team (native sub-agents, `claude -p` and `codex exec` runs) read from the one graph of who started whom, the parent's copied history
left out of a sub-agent, the tokens of the approval review and of the sub-agents, the state of a sub-agent, the events of the collaboration, the redirect of a linked id to its page.

The scene is the synthetic Codex orchestrator of tools/synth_home.py (--codex-orch), read in this process with a fake /proc; the smaller pieces are made by hand.

    python3 -m unittest tests.test_codex_page
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
from compat import patched, server  # noqa: E402
from test_proclink import NS, FakeProc  # noqa: E402
import synth_home  # noqa: E402

from board import agents as AG, catalog, diag, procs, runstate as RS, views  # noqa: E402
from board.codex_parse import cx_decode  # noqa: E402


def line(ts, typ, payload, ordinal=None):
    d = {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(ts)), 'type': typ, 'payload': payload}
    if ordinal is not None:
        d = {'timestamp': d['timestamp'], 'ordinal': ordinal, 'type': typ, 'payload': payload}
    return json.dumps(d, separators=(',', ':')).encode()


class ForkLines(unittest.TestCase):
    """The front of a sub-agent's rollout is its parent's history: nothing of it is read as the sub-agent's own."""

    def rows(self, lines, prefix=2, partial=False):
        fork = AG.ForkSkip(prefix, partial)
        return [r['pt'] for r in AG.cx_rows(lines, fork)]

    def test_lines_up_to_the_ordinal_of_the_copy_are_left_out(self):
        lines = [line(1, 'session_meta', {'id': 'x'}, 0), line(2, 'event_msg', {'type': 'task_started'}, 1), line(3, 'response_item', {'type': 'message', 'role': 'user'}, 2),
                 line(4, 'event_msg', {'type': 'task_started'}, 3), line(5, 'response_item', {'type': 'function_call'}, 4)]
        self.assertEqual(self.rows(lines), ['task_started', 'function_call'])

    def test_a_line_without_an_ordinal_counts_by_its_place_in_the_file(self):
        lines = [line(1, 'session_meta', {'id': 'x'}), line(2, 'event_msg', {'type': 'task_started'}), line(3, 'response_item', {'type': 'message'}),
                 line(4, 'event_msg', {'type': 'task_started'}), line(5, 'response_item', {'type': 'function_call'})]
        self.assertEqual(self.rows(lines), ['task_started', 'function_call'])
        self.assertEqual(self.rows([b'', b'not json'] + lines[:1], prefix=3), [])                 # a line that cannot be read still has its place

    def test_the_first_line_behind_the_copy_ends_it_for_good(self):
        lines = [line(1, 'session_meta', {'id': 'x'}, 0), line(2, 'event_msg', {'type': 'task_started'}, 5), line(3, 'event_msg', {'type': 'task_complete'}, 1)]
        self.assertEqual(self.rows(lines, prefix=2), ['task_started', 'task_complete'])             # an odd ordinal after the end of the copy is not a second copy

    def test_a_thread_with_no_copy_skips_nothing(self):
        lines = [line(1, 'event_msg', {'type': 'task_started'}, 0), line(2, 'event_msg', {'type': 'task_complete'}, 1)]
        self.assertEqual([r['pt'] for r in AG.cx_rows(lines, AG.ForkSkip(None))], ['task_started', 'task_complete'])
        self.assertEqual([r['pt'] for r in AG.cx_rows(lines, AG.ForkSkip(None, partial=True))], ['task_started', 'task_complete'])      # a root thread has no copy, tail or not
        self.assertEqual(self.rows(lines, prefix=0), ['task_started', 'task_complete'])

    def test_the_tail_of_a_big_sub_agent_is_told_line_by_line_by_its_own_ordinals(self):
        # the copied part may still be in the tail (nothing says where the tail began): ordinals up to the copy's end are the parent's, the first line behind it is the sub-agent's own
        lines = [line(1, 'event_msg', {'type': 'task_started'}, 90), line(2, 'response_item', {'type': 'message', 'role': 'user', 'content': 'the parent instruction'}, 91),
                 line(3, 'response_item', {'type': 'function_call'}, 93), line(4, 'event_msg', {'type': 'task_started'}, 101), line(5, 'response_item', {'type': 'function_call'}, 102)]
        self.assertEqual(self.rows(lines, prefix=100, partial=True), ['task_started', 'function_call'])
        a = AG.CodexAgent({'id': 'sub', 'path': '/x', 'cwd': '/w', 'meta_ts': 1.0, 'model': '', 'kind': 'sub', 'prefix_ord': 100}, {})
        a.fork = AG.ForkSkip(100, partial=True)
        for r in AG.cx_rows(lines, a.fork):
            a.feed_cx(r)
        self.assertIsNone(a.spawn_prompt)                                                     # the parent's instruction did not become the sub-agent's
        self.assertEqual((len(a.turns), a.tool_count), (1, 1))                                # its own turn and its own call only

    def test_a_line_of_a_big_tail_with_no_ordinal_is_not_known_to_be_the_sub_agents_own(self):
        lines = [line(1, 'event_msg', {'type': 'task_started'}), line(2, 'response_item', {'type': 'function_call'}, 200)]
        self.assertEqual(self.rows(lines, prefix=100, partial=True), ['function_call'])

    def test_reading_again_from_the_start_skips_the_copy_again(self):
        lines = [line(1, 'session_meta', {'id': 'x'}, 0), line(2, 'event_msg', {'type': 'task_started'}, 1), line(3, 'event_msg', {'type': 'task_complete'}, 2)]
        fork = AG.ForkSkip(1)
        self.assertEqual([r['pt'] for r in AG.cx_rows(lines, fork)], ['task_complete'])
        fork.restart()
        self.assertEqual([r['pt'] for r in AG.cx_rows(lines, fork)], ['task_complete'])

    def test_a_line_over_the_decode_limit_still_has_its_ordinal(self):
        big = b'{"timestamp":"2026-10-04T00:00:00.000Z","ordinal":1,"type":"response_item","payload":{"type":"message","content":"' + b'x' * (1 << 20) + b'"}}'
        self.assertEqual(cx_decode(big)['ord'], 1)
        self.assertEqual(self.rows([line(1, 'session_meta', {'id': 'x'}, 0), big, line(3, 'event_msg', {'type': 'task_started'}, 2)], prefix=1), ['task_started'])

    def test_a_sub_agent_agent_takes_none_of_the_copy_as_its_turn_activity_or_instruction(self):
        e = {'id': 'sub', 'path': '/x', 'cwd': '/w', 'meta_ts': 1.0, 'model': '', 'kind': 'sub', 'prefix_ord': 3, 'agent_path': '/root/s1', 'nick': 'Atlas'}
        a = AG.CodexAgent(e, {})
        self.assertEqual((a.origin, a.provider), ('subagent', 'codex'))
        lines = [line(1, 'session_meta', {'id': 'sub'}, 0), line(2, 'event_msg', {'type': 'task_started', 'model_context_window': 9}, 1),
                 line(3, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'the parent instruction'}]}, 2),
                 line(4, 'response_item', {'type': 'custom_tool_call', 'call_id': 'c1', 'name': 'exec', 'input': 'await tools.exec_command({cmd: "ls"})'}, 3),
                 line(5, 'event_msg', {'type': 'task_started'}, 4), line(6, 'event_msg', {'type': 'task_complete', 'last_agent_message': 'done'}, 5)]
        for r in AG.cx_rows(lines, a.fork):
            a.feed_cx(r)
        self.assertEqual(len(a.turns), 1)                                                     # its own turn only
        self.assertEqual((a.turns[0]['status'], a.turns[0]['user']), ('done', None))
        self.assertIsNone(a.spawn_prompt)                                                     # the parent's instruction is no instruction of its own
        self.assertEqual(a.tool_count, 0)
        self.assertEqual(len(a.runs.runs), 1)

    def test_a_thread_that_is_no_sub_agent_has_no_copy(self):
        a = AG.CodexAgent({'id': 'x', 'path': '/x', 'cwd': '/w', 'meta_ts': 1.0, 'model': '', 'kind': 'root', 'prefix_ord': 7}, {})
        self.assertEqual((a.origin, a.fork.over), ('exec', True))


class GuardianTokens(unittest.TestCase):
    def test_only_a_guardian_thread_comes_into_the_approval_review_tokens(self):
        kids = [{'id': 'g', 'kind': 'guardian', 'thread_total': {'input_tokens': 100, 'output_tokens': 10}, 'model': '', 'calls': 3},
                {'id': 's', 'kind': 'sub', 'thread_total': {'input_tokens': 5000, 'output_tokens': 500}, 'model': 'gpt-6.1-sol', 'calls': 9}]
        from board.tokens import TokenMeter
        m = TokenMeter()
        with mock.patch.object(AG.CODEX, 'children', lambda parent: kids):
            AG.cx_sync_guardians(m, 'p')
        t = m.as_dict()
        self.assertEqual((t['guardian']['calls'], t['guardian']['input'], t['guardian']['output']), (3, 100, 10))
        self.assertEqual(t['calls'], 3)                                                       # the sub-agent's 9 calls are its own card's


class SubAgentState(unittest.TestCase):
    """The judgment of a native sub-agent thread of Codex (runstate kind `cxsub`): the process is the runtime of its root thread."""
    T = 1000.0

    def judge(self, steps, alive, cuts=(), now=None):
        t = RS.CodexTracker('s')
        for ts, pt, p in steps:
            t.feed_cx(ts, 'event_msg', pt, p)
        f = RS.facts_of(t, 'cxsub', spawn_ts=self.T)
        f.cuts = list(cuts)
        return RS.judge(f, RS.Proc(alive), self.T + 30 if now is None else now)

    def test_a_turn_it_ended_is_done_whether_or_not_its_parent_is_there(self):
        for alive in (True, False, None):
            v = self.judge([(self.T, 'task_started', {}), (self.T + 5, 'task_complete', {'last_agent_message': 'x'})], alive)
            self.assertEqual((v.status, v.reason), ('done', None), alive)

    def test_an_interrupt_after_the_end_changes_nothing_and_one_during_the_work_makes_it_interrupted(self):
        ended = [(self.T, 'task_started', {}), (self.T + 5, 'task_complete', {})]
        self.assertEqual(self.judge(ended, True, cuts=[self.T + 8]).status, 'done')
        working = [(self.T, 'task_started', {})]
        self.assertEqual(self.judge(working, True, cuts=[self.T + 8]).status, 'interrupted')
        self.assertEqual(self.judge(working, True, cuts=[self.T - 100]).status, 'running')         # an interrupt of an earlier turn
        self.assertEqual(self.judge([(self.T, 'task_started', {}), (self.T + 3, 'turn_aborted', {'reason': 'interrupted'})], True).status, 'interrupted')

    def test_an_open_turn_is_working_while_the_runtime_is_there_and_ended_when_it_is_not(self):
        working = [(self.T, 'task_started', {})]
        self.assertEqual(self.judge(working, True).status, 'running')
        self.assertEqual(self.judge(working, False).status, 'ended')
        self.assertEqual(self.judge(working, None, now=self.T + 3600).status, 'unknown')            # no process view and a long quiet
        self.assertEqual(self.judge(working, True, now=self.T + 3600).status, 'stalled')

    def test_the_error_of_its_own_turn_is_classified_like_any_agents(self):
        v = self.judge([(self.T, 'task_started', {}), (self.T + 2, 'task_complete', {'error': {'codex_error_info': 'server_overloaded', 'message': 'busy'}})], True)
        self.assertEqual(v.status, 'interrupted')


# ---------------------------------------------------------------------------------------------------------------------
# the scene
# ---------------------------------------------------------------------------------------------------------------------
class Scene(unittest.TestCase):
    """The synthetic Codex orchestrator read in this process: a fake /proc with the codex process of the root (it holds the root rollout open) and the helper `claude -p`."""
    BUILD = {'codex_orch': True}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.home = os.path.join(os.path.realpath(cls.tmp.name), 'home')
        cls.info = synth_home.build(cls.home, now=time.time(), **cls.BUILD)
        cls.co = cls.info['codex_orch']
        cls.ids = cls.co['ids']
        cls.proc = FakeProc(os.path.join(os.path.realpath(cls.tmp.name), 'proc'))
        cls.proc.add(500, 1, ['codex', '--no-alt-screen'], fds=[cls.co['paths']['root']])
        for sid, cwd, start, env in cls.co['live']:                                   # the helper: a live `claude -p` whose environment names the Codex thread that ran its shell
            os.makedirs(os.path.join(cls.home, '.claude', 'sessions'), exist_ok=True)
            cls.proc.add(600, 1, ['claude', '-p', '--model', 'x'], start=6000)
            with open(os.path.join(cls.proc.root, '600', 'environ'), 'wb') as f:
                f.write(b'\0'.join(('%s=%s' % kv).encode() for kv in env.items()) + b'\0')
            with open(os.path.join(cls.home, '.claude', 'sessions', '600.json'), 'w') as f:
                json.dump({'pid': 600, 'sessionId': sid, 'cwd': cwd, 'startedAt': int(start * 1000), 'kind': 'interactive', 'entrypoint': 'sdk-cli', 'procStart': '6000',
                           'pidDomain': 'linux:abc:' + NS}, f)
        cls.links, cls.index = server.LinkIndex(), server.CodexIndex()
        cls.stack = patched(HOME=cls.home, CLAUDE_HOME=os.path.join(cls.home, '.claude'), PROJECTS=os.path.join(cls.home, '.claude', 'projects'),
                            CODEX_HOME=os.path.join(cls.home, '.codex'), CODEX_SESSIONS=os.path.join(cls.home, '.codex', 'sessions'),
                            CODEX_NAMES=os.path.join(cls.home, '.codex', 'session_index.jsonl'), CODEX=cls.index, LINKS=cls.links, PROC=cls.proc.root)
        cls.stack.__enter__()
        cls.addClassCleanup(cls.stack.__exit__, None, None, None)
        procs.reset()
        cls.addClassCleanup(procs.reset)
        cls.index.refresh(force=True)
        cls.links.scan()
        cls.page = server.CodexSession(cls.index.get(cls.co['root']))
        cls.page.poll()
        cls.page.poll()
        cls.st = views.state(cls.page)
        cls.by_id = {a['id']: a for a in cls.st['agents']}

    def agent(self, role):
        return self.by_id[self.ids[role] if role in self.ids else self.co['claude'][role]]


class TeamOfACodexPage(Scene):
    def test_the_team_is_two_sub_agents_two_claude_runs_and_an_exec_thread_and_no_guardian(self):
        kinds = sorted((a['origin'], a['provider']) for a in self.st['agents'])
        self.assertEqual(kinds, [('cli', 'claude'), ('cli', 'claude'), ('exec', 'codex'), ('subagent', 'codex'), ('subagent', 'codex')])
        self.assertNotIn(self.ids['guardian'], self.by_id)
        self.assertEqual({a['parent'] for a in self.st['agents']}, {None})                  # every one was started by the orchestrator's own shell or call
        self.assertEqual((self.st['session']['id'], self.st['orch']['provider']), (self.co['root'], 'codex'))

    def test_a_sub_agent_is_named_by_the_end_of_its_path_and_its_nickname_and_its_instruction_is_unknown(self):
        s1 = self.agent('s1')
        self.assertEqual((s1['tag'], s1['title'], s1['status']), ('s1', 'Atlas', 'done'))
        self.assertEqual((s1['link']['rule'], s1['link']['rule_class']), ('subagent', 'certain'))
        self.assertEqual(self.agent('s2')['status'], 'running')                                 # its turn is open and the runtime holds the root rollout
        self.assertIsNone(self.page.agents[self.ids['s1']].spawn_prompt)
        e = self.index.get(self.ids['s1'])
        self.assertIsNone(e['first_user'])

    def test_the_tokens_of_a_sub_agent_are_its_own_and_the_approval_review_keeps_only_its_threads(self):
        g = self.st['orch']['tokens']['guardian']
        self.assertEqual(g['calls'], 3)
        for r in ('s1', 's2'):
            t, total = self.agent(r)['tokens'], self.index.get(self.ids[r])['thread_total']
            self.assertEqual((t['input'] + t['cache_read'], t['output']), (total['input_tokens'], total['output_tokens']))       # what its own record says it used
        orch, root = self.st['orch']['tokens'], self.index.get(self.co['root'])
        self.assertEqual(orch['calls'], root['calls'] + 3)                                  # the root's calls and the approval review's; the sub-agents' calls are on their cards
        self.assertEqual(orch['input'] + orch['cache_read'], root['thread_total']['input_tokens'] + self.index.get(self.ids['guardian'])['thread_total']['input_tokens'])

    def test_the_debate_of_the_folder_has_its_two_participants_seated(self):
        talk = [t for d in self.st['debates'] for t in d['topics'] if t['dir'].endswith('/talk')]
        self.assertEqual(len(talk), 1)
        seats = {r['p']: [c['agent'] for c in r['cells']] for r in talk[0]['rows']}
        self.assertEqual(sorted(seats), ['A', 'B'])
        self.assertEqual(seats['A'], [self.co['claude']['reviewer']])
        self.assertEqual(seats['B'], [self.ids['reviewer_b']])

    def test_the_conversation_cards_follow_the_records_of_the_parent(self):
        feed = [e for e in self.st['feed'] if e['kind'] in ('spawn', 'orch_msg', 'agent_msg', 'handback') and e['agent'] in (self.ids['s1'], self.ids['s2'])]
        kinds = [(e['kind'], e['agent']) for e in feed]
        self.assertEqual(kinds.count(('spawn', self.ids['s1'])), 1)
        self.assertEqual(kinds.count(('spawn', self.ids['s2'])), 1)
        spawn = next(e for e in feed if e['kind'] == 'spawn')
        self.assertEqual((spawn['from'], spawn['to']), ('orch', spawn['agent']))
        self.assertEqual(spawn['text_i18n'], {'key': 'event.encrypted.text', 'params': {}})        # the instruction is ciphertext: the card says so
        for e in feed:
            self.assertNotIn('/root/', e['text'])                                                  # the plain note that comes with the ciphertext (it names a path) is never shown
        mid = [e for e in feed if e['kind'] == 'agent_msg']
        self.assertEqual([(e['agent'], e['to']) for e in mid], [(self.ids['s1'], 'orch')])        # the report in the middle of the work
        hand = [e for e in feed if e['kind'] == 'handback']
        self.assertEqual([(e['agent'], e['to']) for e in hand], [(self.ids['s1'], 'orch')])       # the first message after `completed`
        self.assertGreater(hand[0]['ts'], mid[0]['ts'])
        self.assertFalse([e for e in feed if e['kind'] == 'orch_msg'])                           # the first message to a sub-agent is the spawn itself

    def test_no_javascript_of_a_tool_call_is_shown_as_what_anyone_does(self):
        words = ('tools.exec_command', 'await ', 'const ', 'text(r.output)')
        shown = [self.st['orch']['last_action'], json.dumps([a['current'] for a in self.st['agents']]), json.dumps([a['last_tool'] for a in self.st['agents']])]
        for text in shown:
            for w in words:
                self.assertNotIn(w, text)
        self.assertTrue(self.st['orch']['last_action'])

    def test_polling_again_adds_no_second_card(self):
        before = len(self.page.feed)
        for _ in range(3):
            self.page.poll()
        self.assertEqual(len(self.page.feed), before)

    def test_the_list_counts_the_team_and_the_sub_agent_ids_open_the_page_of_the_root(self):
        self.assertEqual(catalog.team_totals()[self.co['root']], 5)
        item = next(x for x in catalog.list_sessions() if x['id'] == self.co['root'])
        self.assertEqual(item['agents'], 5)
        reg = catalog.Registry()
        self.assertEqual(reg.get(self.ids['s1']).id, self.co['root'])                                 # a sub-agent is a card of its root's page
        self.assertEqual(reg.get(self.ids['guardian']).id, self.co['root'])                        # so is the approval review
        self.assertEqual(reg.get(self.ids['reviewer_b']).id, self.co['root'])                      # and a `codex exec` run the shell started

    def test_the_page_shows_no_format_drift_for_records_it_can_read(self):
        self.assertNotIn('format_drift', self.st['diag']['by_code'])


class ChildSessionAgents(unittest.TestCase):
    """C14: a `claude -p` child of the page is a Claude session with a folder of its own; the sub-agents it started with its Agent tool hang under it on the page of the top."""
    TOP = 'aaaaaaaa-0000-4000-8000-000000000001'
    CHILD = 'bbbbbbbb-0000-4000-8000-000000000002'
    SUB = 'a%016x' % 0x1234
    T0 = time.time() - 600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.proj = os.path.join(t, 'claude', 'projects', 'p')
        os.makedirs(self.proj)
        iso = lambda x: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(x))
        self.iso = iso
        user = lambda sid, ts, text, **kw: json.dumps(dict({'type': 'user', 'timestamp': iso(ts), 'cwd': '/w', 'sessionId': sid, 'message': {'role': 'user', 'content': text}}, **kw))
        self.write(self.TOP, [user(self.TOP, self.T0, 'go', origin={'kind': 'human'})])
        self.child_lines = [user(self.CHILD, self.T0 + 5, 'Review the notes and use a helper.'),
                            json.dumps({'type': 'assistant', 'timestamp': iso(self.T0 + 6), 'cwd': '/w', 'sessionId': self.CHILD,
                                        'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'tool_use', 'id': 'toolu_h', 'name': 'Agent',
                                                                                                                      'input': {'description': 'T1-A look', 'prompt': 'look at it'}}]}})]
        self.write(self.CHILD, self.child_lines)
        folder = os.path.join(self.proj, self.CHILD, 'subagents')
        os.makedirs(folder)
        with open(os.path.join(folder, 'agent-%s.meta.json' % self.SUB), 'w') as f:
            json.dump({'agentType': 'general-purpose', 'description': 'T1-A look', 'toolUseId': 'toolu_h'}, f)
        with open(os.path.join(folder, 'agent-%s.jsonl' % self.SUB), 'w') as f:
            f.write(user(self.CHILD, self.T0 + 7, 'look at it') + '\n')
        self.links = server.LinkIndex()
        self.links.ready.set()
        self.links.cli_owners = {self.CHILD: {'sid': self.TOP, 'node': None, 'rule': 'env', 'certain': True, 'bash_ts': self.T0 + 4, 'bash_desc': '', 'call': None}}
        self.stack = patched(HOME=t, CLAUDE_HOME=os.path.join(t, 'claude'), PROJECTS=os.path.join(t, 'claude', 'projects'), LINKS=self.links)
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)

    def write(self, sid, lines):
        with open(os.path.join(self.proj, sid + '.jsonl'), 'a') as f:
            f.write('\n'.join(lines) + '\n')

    def state(self):
        s = server.Session(os.path.join(self.proj, self.TOP + '.jsonl'))
        s.poll()
        s.poll()
        return s, views.state(s)

    def test_the_sub_agent_of_a_child_hangs_under_the_child(self):
        s, st = self.state()
        by = {a['id']: a for a in st['agents']}
        self.assertEqual(set(by), {self.CHILD, self.SUB})
        self.assertEqual((by[self.CHILD]['origin'], by[self.CHILD]['parent']), ('cli', None))
        self.assertEqual((by[self.SUB]['origin'], by[self.SUB]['parent']), ('subagent', self.CHILD))
        spawn = [e for e in st['feed'] if e['kind'] == 'spawn' and e['agent'] == self.SUB]
        self.assertEqual([(e['from'], e['to']) for e in spawn], [(self.CHILD, self.SUB)])
        self.assertEqual(spawn[0]['text'], 'look at it')
        s.poll()
        self.assertEqual(len([e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.SUB]), 1)

    def test_its_life_is_the_childs_and_its_notice_is_in_the_childs_record(self):
        s, st = self.state()
        sub = next(a for a in st['agents'] if a['id'] == self.SUB)
        self.assertEqual(sub['status'], 'ended')                                          # the child's process is gone and no notice came
        notice = '<task-notification>\n<task-id>%s</task-id>\n<status>completed</status>\n<summary>looked</summary>\n</task-notification>' % self.SUB
        self.write(self.CHILD, [json.dumps({'type': 'user', 'timestamp': self.iso(self.T0 + 30), 'cwd': '/w', 'sessionId': self.CHILD, 'message': {'role': 'user', 'content': notice}})])
        s.poll()
        st = views.state(s)
        sub = next(a for a in st['agents'] if a['id'] == self.SUB)
        self.assertEqual(sub['status'], 'done')
        self.assertEqual(sub['notification']['summary'], 'looked')
        self.assertEqual(len([e for e in st['feed'] if e['kind'] == 'notify' and e['agent'] == self.SUB]), 1)

    def test_the_list_counts_the_sub_agent_of_the_child(self):
        item = next(x for x in catalog.list_sessions() if x['id'] == self.TOP)
        self.assertEqual(item['agents'], 2)

    def test_what_a_child_started_goes_when_the_child_is_no_longer_owned_and_comes_back_with_it(self):
        s, st = self.state()
        owners = self.links.cli_owners
        self.links.cli_owners = {}
        s.poll()
        self.assertEqual(set(s.agents), set())
        self.links.cli_owners = owners
        s.poll()
        self.assertEqual(set(s.agents), {self.CHILD, self.SUB})
        self.assertEqual(len([e for e in s.feed if e['kind'] == 'spawn' and e['agent'] == self.SUB]), 1)          # not a second card

    def test_a_codex_exec_thread_of_a_claude_session_still_hangs_under_the_orchestrator(self):
        e = {'id': 'x', 'path': '/x', 'cwd': '/w', 'meta_ts': 1.0, 'model': '', 'kind': 'root'}
        page = server.Session.__new__(server.Session)
        page.id, page.agents = self.TOP, {}
        a = AG.CodexAgent(e, {'sid': self.TOP, 'node': 'a1234', 'parent_kind': 'claude'})
        self.assertEqual(server.Session.launcher_of(page, a), 'orch')                      # as before: the node is a Claude sub-agent, which this page never hung it under
        a = AG.CodexAgent(e, {'sid': 'other-run', 'node': None, 'parent_kind': 'claude'})
        page.agents = {'other-run': object()}
        self.assertEqual(server.Session.launcher_of(page, a), 'other-run')                 # started by another run of the page: it hangs under that run


class FakeSession:
    """Just enough of a page for a Codex linker: its id, its agents, the feed, `launcher_of`."""

    def __init__(self, sid='R', provider='codex', up=None):
        self.id, self.provider, self.agents, self.feed, self.up = sid, provider, {}, [], up or {}

    def _event(self, ts, kind, frm, to, title, text='', agent=None, extra=None, title_key=None, questions=None, text_key=None):
        ev = {'ts': ts, 'kind': kind, 'from': frm, 'to': to, 'title': title, 'text': text, 'agent': agent}
        ev.update(extra or {})
        self.feed.append(ev)
        return ev

    def launcher_of(self, a):
        return self.up.get(a.id, 'orch')


INSTRUCTION = 'Review the currency handling of the ledger package carefully and report in plain words what is wrong.'


def exec_agent(tid='X', link=None, user=INSTRUCTION, ended=True, t0=1000.0):
    a = AG.CodexAgent({'id': tid, 'path': '/x', 'cwd': '/w', 'meta_ts': t0, 'model': '', 'kind': 'root'}, link or {})
    a.link = link or {}
    for r in (cx_decode(line(t0, 'event_msg', {'type': 'task_started'})),
              cx_decode(line(t0 + 0.5, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': user}]}))):
        a.feed_cx(r)
    if ended:
        a.feed_cx(cx_decode(line(t0 + 20, 'event_msg', {'type': 'task_complete', 'last_agent_message': 'done'})))
    return a


def codex_call(call_id, tree, node, ts=998.0, out='/w/talk/r1/B.md', text=INSTRUCTION):
    c = server.cx_parse_call(ts, call_id, {'command': 'cd /w && codex exec -o %s "%s"' % (out, text)}, '/w')
    c['tree'], c['node'] = tree, node
    return c


class CallOfAThread(unittest.TestCase):
    """The page takes the call of a Codex thread (its report path, its name) from what the link index decided: the node it named and the call it held are not chosen again."""

    def linker(self, calls, up=None):
        lk = server.CodexLinker(FakeSession(up=up))
        for c in calls:
            lk.calls[c['id']] = c
            lk.order.append(c)
        return lk

    def turn(self, a, i=0):
        return a.turns[i]

    def test_the_node_the_link_named_is_not_replaced_by_a_call_of_another_node_with_the_same_words(self):
        other = codex_call('call-s2', 'R', 'S2')                                  # the only call with this instruction is a sub-agent S2's; the environment said S1
        lk = self.linker([other])
        a = exec_agent(link={'sid': 'R', 'node': 'S1', 'call': None})
        self.assertEqual(lk._turn_call('X', 0, self.turn(a), a.link, '/w'), (None, None))
        self.assertEqual(lk._turn_call('X', 0, self.turn(a))[0]['id'], 'call-s2')            # (what the page did when nothing decided: the words found S2's call)
        a.link = {'sid': 'R', 'node': None, 'call': 'call-s2'}                    # a call named for another node is not taken either
        self.assertEqual(lk._turn_call('X', 0, self.turn(a), a.link, '/w'), (None, None))
        a.link = {'sid': 'R', 'node': 'S2', 'call': 'call-s2'}
        c, L = lk._turn_call('X', 0, self.turn(a), a.link, '/w')
        self.assertEqual((c['id'], L['out']), ('call-s2', '/w/talk/r1/B.md'))

    def test_a_call_the_link_index_held_is_not_picked_by_time(self):
        lk = self.linker([codex_call('call-a', 'R', None), codex_call('call-b', 'R', None, ts=999.0)])
        a = exec_agent(link={'sid': 'R', 'node': None, 'call': None})              # two calls say the same words: the index held the call
        self.assertEqual(lk._turn_call('X', 0, self.turn(a), a.link, '/w'), (None, None))
        a.link = {'sid': 'R', 'node': None, 'call': 'call-a'}
        self.assertEqual(lk._turn_call('X', 0, self.turn(a), a.link, '/w')[0]['id'], 'call-a')

    def test_a_resumed_turn_needs_the_owner_the_folder_and_exactly_one_call(self):
        def resume(call_id, tree, node, cwd='/w', ts=1019.0):
            c = server.cx_parse_call(ts, call_id, {'command': 'cd %s && codex exec resume X "%s"' % (cwd, INSTRUCTION)}, cwd)
            c['tree'], c['node'] = tree, node
            return c
        a = exec_agent(ended=False)
        a.feed_cx(cx_decode(line(1021.0, 'event_msg', {'type': 'task_started'})))
        a.feed_cx(cx_decode(line(1021.5, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': INSTRUCTION}]})))
        link = {'sid': 'R', 'node': None, 'call': 'call-first'}
        t = a.turns[-1]
        self.assertEqual(self.linker([resume('r1', 'R', None)])._turn_call('X', 1, t, link, '/w')[0]['id'], 'r1')
        self.assertEqual(self.linker([resume('r1', 'R', 'S2')])._turn_call('X', 1, t, link, '/w'), (None, None))          # another node's call
        self.assertEqual(self.linker([resume('r1', 'Q', None)])._turn_call('X', 1, t, link, '/w'), (None, None))          # another tree's
        self.assertEqual(self.linker([resume('r1', 'R', None, cwd='/elsewhere')])._turn_call('X', 1, t, link, '/w'), (None, None))     # another folder
        self.assertEqual(self.linker([resume('r1', 'R', None), resume('r2', 'R', None, ts=1020.0)])._turn_call('X', 1, t, link, '/w'), (None, None))   # a tie

    def test_a_command_that_comes_late_finds_its_turn_and_tells_the_card_made_before_it(self):
        lk = self.linker([])
        a = exec_agent(link={'sid': 'R', 'node': None, 'call': 'call-x'})
        lk.agents['X'] = lk.s.agents['X'] = a
        self.assertTrue(lk._derive(a))
        first = [e for e in lk.s.feed if e['kind'] == 'spawn']
        self.assertEqual(len(first), 1)
        self.assertIsNone(a.turns[0]['call'])
        self.assertFalse(a.out_paths)
        self.assertTrue(first[0]['title'].startswith('Review the currency'))                  # no report name yet: the first words
        lk.calls['call-x'] = codex_call('call-x', 'R', None)                       # the CommandExecution record is written when the command ends
        lk.order.append(lk.calls['call-x'])
        lk._derive(a)
        self.assertEqual((a.turns[0]['call'], [o['path'] for o in a.out_paths], a.report_tag), ('call-x', ['/w/talk/r1/B.md'], 'B'))
        spawns = [e for e in lk.s.feed if e['kind'] == 'spawn']
        self.assertEqual((len(spawns), spawns[0]['title'], spawns[0]['tool_use_id']), (1, 'B.md', 'call-x'))        # the same card, now with the name of the report
        n = len(lk.s.feed)
        lk._derive(a)
        self.assertEqual(len(lk.s.feed), n)

    def test_the_events_of_a_run_are_between_it_and_the_one_that_started_it(self):
        lk = server.CodexLinker(FakeSession(up={'X': 'K'}))
        a = exec_agent(link={'sid': 'K', 'node': None, 'call': None})
        lk.agents['X'] = a
        lk._derive(a)
        self.assertEqual([(e['kind'], e['from'], e['to']) for e in lk.s.feed], [('spawn', 'K', 'X'), ('handback', 'X', 'K'), ('notify', 'X', 'K')])


class FinalReportOfASubAgent(unittest.TestCase):
    """The first message of a sub-agent after its `completed` is its final report: the same whether the record was read at once or a message at a time."""
    ROOT = 'R'

    def collab(self, *rows):
        out = []
        for i, (kind, extra) in enumerate(rows):
            d = {'thread': 'R', 'kind': kind, 'ts': 100.0 + i, 'call_id': None, 'agent_thread_id': 'S1', 'agent_path': '/root/s1', 'sub_turn_id': None, 'author': None, 'recipient': None,
                 'text': None, 'msg_id': None, 'turn_id': None, 'encrypted': False}
            d.update(extra)
            out.append(d)
        return out

    def msg(self, mid, text):
        return ('message', {'author': '/root/s1', 'recipient': '/root', 'msg_id': mid, 'text': text, 'agent_thread_id': None, 'agent_path': None})

    def run_through(self, steps):
        s = FakeSession(self.ROOT)
        lk = server.CodexLinker(s)
        a = AG.CodexAgent({'id': 'S1', 'path': '/s', 'cwd': '/w', 'meta_ts': 1.0, 'model': '', 'kind': 'sub', 'agent_path': '/root/s1', 'prefix_ord': 1}, {})
        a.runtime, a.sub_name = ('R', '/r'), 's1'
        lk.agents['S1'] = s.agents['S1'] = a
        for rows in steps:
            with mock.patch.object(server.CODEX, 'collab', lambda tid, rows=rows: tuple(rows) if tid == 'R' else ()):
                lk._collab({'S1': {}})
        return [(e['kind'], e['text']) for e in s.feed if e['kind'] in ('handback', 'agent_msg')]

    def test_a_later_message_is_no_second_final_report(self):
        base = [('started', {}), ('completed', {})]
        one = self.collab(*base, self.msg('m1', 'the report'), self.msg('m2', 'one more thing'))
        read_at_once = self.run_through([one])
        self.assertEqual(read_at_once, [('handback', 'the report'), ('agent_msg', 'one more thing')])
        step_by_step = self.run_through([one[:3], one])
        self.assertEqual(step_by_step, read_at_once)
        again = self.run_through([one[:3], one[:3], one, one])
        self.assertEqual(again, read_at_once)

    def test_a_restart_of_the_agent_gives_it_a_final_report_again(self):
        rows = self.collab(('started', {}), ('completed', {}), self.msg('m1', 'first report'), ('started', {}), ('completed', {}), self.msg('m2', 'second report'))
        self.assertEqual(self.run_through([rows[:3], rows]), [('handback', 'first report'), ('handback', 'second report')])
        self.assertEqual(self.run_through([rows]), [('handback', 'first report'), ('handback', 'second report')])


class ExecOfADescendantClaude(unittest.TestCase):
    """Codex thread R -> `claude -p` K -> `codex exec -o .../B.md` X: the call of X is in K's record, and the events are between K and X, not the orchestrator and X."""
    R, K, X = '019b0000-0000-7000-8000-00000000000a', 'bbbbbbbb-0000-4000-8000-0000000000bb', '019b0000-0000-7000-8000-00000000000c'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.home = t
        self.t0 = time.time() - 300
        iso = lambda x: time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(x))
        day = os.path.join(t, '.codex', 'sessions', '2026', '10', '04')
        os.makedirs(day)

        def rollout(tid, lines):
            with open(os.path.join(day, 'rollout-2026-10-04T00-00-00-%s.jsonl' % tid), 'wb') as f:
                f.write(b'\n'.join(lines) + b'\n')
        meta = lambda tid, ts, origin, source: line(ts, 'session_meta', {'id': tid, 'session_id': tid, 'timestamp': iso(ts), 'cwd': '/w', 'originator': origin, 'source': source})
        rollout(self.R, [meta(self.R, self.t0, 'codex-tui', 'cli'), line(self.t0 + 1, 'event_msg', {'type': 'task_started'})])
        rollout(self.X, [meta(self.X, self.t0 + 22, 'codex_exec', 'exec'), line(self.t0 + 22, 'event_msg', {'type': 'task_started'}),
                         line(self.t0 + 22.5, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': INSTRUCTION}]}),
                         line(self.t0 + 40, 'event_msg', {'type': 'task_complete', 'last_agent_message': 'Currency is kept in minor units.'})])
        proj = os.path.join(t, '.claude', 'projects', 'p')
        os.makedirs(proj)
        rows = [{'type': 'user', 'timestamp': iso(self.t0 + 5), 'cwd': '/w', 'sessionId': self.K, 'message': {'role': 'user', 'content': 'Ask a Codex reviewer.'}},
                {'type': 'assistant', 'timestamp': iso(self.t0 + 20), 'cwd': '/w', 'sessionId': self.K,
                 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'tool_use', 'id': 'toolu_b', 'name': 'Bash',
                                                                                               'input': {'command': 'cd /w && codex exec -o /w/talk/r1/B.md "%s"' % INSTRUCTION}}]}},
                {'type': 'user', 'timestamp': iso(self.t0 + 41), 'cwd': '/w', 'sessionId': self.K,
                 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_b', 'content': 'ok'}]}}]
        with open(os.path.join(proj, self.K + '.jsonl'), 'w') as f:
            f.write('\n'.join(json.dumps(r) for r in rows) + '\n')
        self.links, self.index = server.LinkIndex(), server.CodexIndex()
        self.links.ready.set()
        self.links.cli_owners = {self.K: {'sid': self.R, 'node': None, 'rule': 'env', 'certain': True, 'bash_ts': self.t0 + 4, 'bash_desc': '', 'call': None}}
        self.links.owners = {self.X: {'sid': self.K, 'node': None, 'rule': 'prompt', 'call': 'toolu_b', 'bash_ts': self.t0 + 20, 'bash_desc': '', 'dt': 2.0, 'cwd': '/w',
                                      'cwd_ok': True, 'prompt_ok': True, 'parent_kind': 'claude'}}
        self.stack = patched(HOME=t, CLAUDE_HOME=os.path.join(t, '.claude'), PROJECTS=os.path.join(t, '.claude', 'projects'), CODEX_HOME=os.path.join(t, '.codex'),
                             CODEX_SESSIONS=os.path.join(t, '.codex', 'sessions'), CODEX_NAMES=os.path.join(t, '.codex', 'session_index.jsonl'), CODEX=self.index, LINKS=self.links)
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.index.refresh(force=True)
        self.page = server.CodexSession(self.index.get(self.R))
        self.page.poll()
        self.page.poll()

    def test_the_call_the_report_and_the_seat_of_the_exec_come_from_the_record_of_the_claude_that_started_it(self):
        x = self.page.agents[self.X]
        self.assertEqual(x.turns[0]['call'], 'toolu_b')
        self.assertEqual([o['path'] for o in x.out_paths], ['/w/talk/r1/B.md'])
        self.assertEqual(x.tag, 'B')
        self.assertEqual(self.page.launcher_of(x), self.K)

    def test_its_events_are_between_the_claude_that_started_it_and_it(self):
        mine = [(e['kind'], e['from'], e['to']) for e in self.page.feed if e.get('agent') == self.X]
        self.assertEqual(mine, [('spawn', self.K, self.X), ('handback', self.X, self.K), ('notify', self.X, self.K)])
        self.assertEqual(next(e for e in self.page.feed if e['kind'] == 'spawn' and e['agent'] == self.X)['title'], 'B.md')


class PageOpenedBeforeTheRecordGrows(Scene):
    """The server opens a page once and polls it: what the record gets later reaches a page that is open already, and gives what one read of the whole record gives."""

    def test_a_message_that_comes_after_the_final_report_is_a_message_not_a_second_report(self):
        root = self.co['paths']['root']
        with open(root) as f:
            n = len(f.read().splitlines())
        path = '/root/s1'
        late = {'type': 'agent_message', 'id': 'msg-late', 'author': path, 'recipient': '/root', 'content': [{'type': 'input_text', 'text': 'One more thing about rounding.'}],
                'internal_chat_message_metadata_passthrough': {'turn_id': 'turn-x'}}
        with open(root, 'ab') as f:
            f.write(line(time.time(), 'response_item', late, n) + b'\n')
        self.index.refresh(force=True)
        self.links.scan()
        self.page.poll()
        s1 = self.ids['s1']
        mine = [(e['kind'], e['text']) for e in self.page.feed if e['kind'] in ('handback', 'agent_msg') and e.get('agent') == s1]
        self.assertEqual([k for k, _ in mine].count('handback'), 1)
        self.assertEqual(mine[-1], ('agent_msg', 'One more thing about rounding.'))
        fresh = server.CodexSession(self.index.get(self.co['root']))                        # the same record read at once
        fresh.poll()
        once = [(e['kind'], e['text']) for e in fresh.feed if e['kind'] in ('handback', 'agent_msg') and e.get('agent') == s1]
        self.assertEqual(sorted(mine), sorted(once))


class FormatDrift(unittest.TestCase):
    def test_a_thread_of_an_unknown_shape_and_unread_commands_are_format_drift_of_the_page(self):
        page = types_namespace(id='root', provider='codex', agents={})
        entries = {'root': {'id': 'root', 'cmds_skipped': 2}}
        kids = {'root': [{'id': 'odd', 'kind': 'internal', 'drift': True}, {'id': 'g', 'kind': 'guardian', 'drift': False}]}
        with mock.patch.object(diag.CODEX, 'get', lambda tid: entries.get(tid)), mock.patch.object(diag.CODEX, 'children', lambda tid: kids.get(tid, [])):
            self.assertEqual(diag.codex_drift(page), {'invalid:CommandExecution', 'invalid:thread_source'})
        page = types_namespace(id='root', provider='codex', agents={})
        with mock.patch.object(diag.CODEX, 'get', lambda tid: {'id': tid, 'cmds_skipped': 0}), mock.patch.object(diag.CODEX, 'children', lambda tid: []):
            self.assertEqual(diag.codex_drift(page), set())
        page = types_namespace(id='c', provider='claude', agents={})
        self.assertEqual(diag.codex_drift(page), set())                                              # a Claude page with no Codex thread says nothing


def types_namespace(**kw):
    import types
    return types.SimpleNamespace(**kw)


if __name__ == '__main__':
    unittest.main()
