"""The call that launched an agent, as a key (CONTRACT 2.3): launched together = the same `gkey()`; a key that is not known is None, and None is in no group with anything.

  - Claude Agent tool: the message.id of the line that holds the call (parallel calls are lines of their own with one message.id), the node when a sub-agent started it
  - `claude -p` from Bash, `codex exec` from Bash: the message.id of the Bash line (`Span.msg_id`, the group of the call of the Codex linker)
  - `codex exec` from the shell of a Codex thread: the exec call around the command, else the command itself
  - a native Codex sub-agent: its own group, whatever the turn
  - no key: a link that was only guessed, a call that is not known

    python3 -m unittest tests.test_launch_keys
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server, start_patches  # noqa: E402
from test_codex_page import FakeSession, exec_agent, line  # noqa: E402  (functions and a plain class: no TestCase is imported twice)

from board import codex_facts as CF, sessions as SE  # noqa: E402
from board.facts import LaunchKey, Span  # noqa: E402
from board.link import certain as link_certain  # noqa: E402

T0 = 1790000000.0


def dump(d):
    return json.dumps(d, separators=(',', ':'))


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.%03dZ' % int((t % 1) * 1000)


class Fixture(unittest.TestCase):
    """A fake HOME (~/.claude/projects/<proj>/<sid>.jsonl) with a LinkIndex and an empty CodexIndex of its own."""
    PARENT = '11111111-1111-4111-8111-111111111111'
    C1, C2, C3 = '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.home = os.path.join(t, 'home')
        self.proj = os.path.join(self.home, '.claude', 'projects', '-w')
        os.makedirs(self.proj)
        os.makedirs(os.path.join(self.home, '.codex', 'sessions'))
        self.links = server.LinkIndex()
        start_patches(self, HOME=self.home, CLAUDE_HOME=os.path.join(self.home, '.claude'), PROJECTS=os.path.join(self.home, '.claude', 'projects'),
                      CODEX_HOME=os.path.join(self.home, '.codex'), CODEX_SESSIONS=os.path.join(self.home, '.codex', 'sessions'),
                      CODEX_NAMES=os.path.join(self.home, '.codex', 'session_index.jsonl'), CODEX=server.CodexIndex(), LINKS=self.links)

    def write(self, sid, lines):
        p = os.path.join(self.proj, sid + '.jsonl')
        with open(p, 'a') as f:
            f.write('\n'.join(lines) + '\n')
        return p

    def tool(self, t, name, inp, tid, mid, cwd='/w'):
        message = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}
        if mid:
            message['id'] = mid
        return dump({'type': 'assistant', 'timestamp': iso(t), 'cwd': cwd, 'message': message})

    def child(self, t, text='hi', cwd='/w'):
        return [dump({'type': 'user', 'timestamp': iso(t), 'cwd': cwd, 'message': {'role': 'user', 'content': text}}),
                dump({'type': 'assistant', 'timestamp': iso(t + 4), 'cwd': cwd, 'message': {'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'hello'}],
                                                                                          'usage': {'input_tokens': 10, 'output_tokens': 5}}})]

    def session(self):
        self.links.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        return s


LONG_A = 'Review the currency handling of the ledger package carefully and report in plain words what is wrong with it today.'
LONG_B = 'Check the rounding of every invoice total in the billing package and write down each case where the cents do not add up.'


class AgentTool(Fixture):
    def agent(self, s, aid):
        return s.agents[aid]

    def setup_subagents(self, mids):
        """The main record has one Agent call for every id of `mids` (a message id or None), made at T0, T0 + 1, ...; a sub-agent of each."""
        lines = [self.tool(T0 + i, 'Agent', {'description': 'job %d' % i, 'prompt': 'go'}, 'toolu_%d' % i, mid) for i, mid in enumerate(mids)]
        self.write(self.PARENT, lines)
        folder = os.path.join(self.proj, self.PARENT, 'subagents')
        os.makedirs(folder)
        for i in range(len(mids)):
            aid = 'a%016x' % (i + 1)
            with open(os.path.join(folder, 'agent-%s.meta.json' % aid), 'w') as f:
                json.dump({'description': 'job %d' % i, 'agentType': 'general', 'toolUseId': 'toolu_%d' % i}, f)
            with open(os.path.join(folder, 'agent-%s.jsonl' % aid), 'w') as f:
                f.write(dump({'type': 'user', 'timestamp': iso(T0 + 10), 'message': {'role': 'user', 'content': 'go'}}) + '\n')

    def test_parallel_calls_of_one_message_are_launched_together(self):
        self.setup_subagents(['msg_1', 'msg_1', 'msg_2'])
        s = self.session()
        s.refresh_facts({})
        keys = [s.agents['a%016x' % (i + 1)].launch for i in range(3)]
        self.assertEqual(keys[0], LaunchKey('claude', self.PARENT, None, 'msg_1', 'toolu_0'))
        self.assertEqual(keys[1].gkey(), keys[0].gkey())
        self.assertNotEqual(keys[2].gkey(), keys[0].gkey())
        self.assertEqual((keys[2].group, keys[2].call), ('msg_2', 'toolu_2'))
        self.assertEqual(len({k.call for k in keys}), 3)

    def test_a_line_without_a_message_id_gives_no_key(self):
        self.setup_subagents([None, 'msg_1'])
        s = self.session()
        s.refresh_facts({})
        self.assertIsNone(s.agents['a%016x' % 1].launch)
        self.assertIsNotNone(s.agents['a%016x' % 2].launch)

    def test_an_agent_without_a_call_has_none(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, '/nonexistent/%s.jsonl' % self.PARENT)
        a = server.Agent('a%016x' % 7, {'description': 'x'})                                  # no toolUseId in the meta
        s.agents[a.id] = a
        s.refresh_facts({})
        self.assertIsNone(a.launch)

    def test_a_sub_agent_that_a_sub_agent_started(self):
        self.setup_subagents(['msg_1'])
        parent = 'a%016x' % 1
        grandchild = 'a%016x' % 9
        folder = os.path.join(self.proj, self.PARENT, 'subagents')
        with open(os.path.join(folder, 'agent-%s.jsonl' % parent), 'a') as f:
            f.write(self.tool(T0 + 12, 'Agent', {'description': 'inner', 'prompt': 'go'}, 'toolu_inner', 'msg_in', cwd='/w') + '\n')
        with open(os.path.join(folder, 'agent-%s.meta.json' % grandchild), 'w') as f:
            json.dump({'description': 'inner', 'agentType': 'general', 'toolUseId': 'toolu_inner', 'parentAgentId': parent}, f)
        with open(os.path.join(folder, 'agent-%s.jsonl' % grandchild), 'w') as f:
            f.write(dump({'type': 'user', 'timestamp': iso(T0 + 13), 'message': {'role': 'user', 'content': 'go'}}) + '\n')
        s = self.session()
        s.poll()
        s.refresh_facts({})
        self.assertEqual(s.agents[grandchild].launch, LaunchKey('claude', self.PARENT, parent, 'msg_in', 'toolu_inner'))
        self.assertEqual(s.agents[parent].launch.node, None)                                     # the one that the main record started: no node


class CliChildren(Fixture):
    def test_claude_p_children_of_one_message_are_launched_together(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_A, 'description': 'one'}, 't1', 'msg_X'),
                                 self.tool(T0 + 1, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_B, 'description': 'two'}, 't2', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        self.write(self.C2, self.child(T0 + 6, LONG_B))
        s = self.session()
        s.refresh_facts({})
        a, b = s.agents[self.C1], s.agents[self.C2]
        self.assertEqual(a.cli['call'], 't1')
        self.assertEqual(a.launch, LaunchKey('claude', self.PARENT, None, 'msg_X', 't1'))
        self.assertEqual(b.launch, LaunchKey('claude', self.PARENT, None, 'msg_X', 't2'))
        self.assertEqual(a.launch.gkey(), b.launch.gkey())

    def test_another_message_is_another_group(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_A}, 't1', 'msg_X'),
                                 self.tool(T0 + 30, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_B}, 't2', 'msg_Y')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        self.write(self.C2, self.child(T0 + 36, LONG_B))
        s = self.session()
        s.refresh_facts({})
        self.assertNotEqual(s.agents[self.C1].launch.gkey(), s.agents[self.C2].launch.gkey())

    def test_a_link_that_was_only_guessed_gives_no_key(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p hi'}, 't1', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, 'hi'))
        s = self.session()
        s.refresh_facts({})
        a = s.agents[self.C1]
        self.assertFalse(link_certain(a.cli['rule']))
        self.assertIsNone(a.launch)

    def test_the_call_that_is_not_known(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_A}, 't1', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        s = self.session()
        a = s.agents[self.C1]
        a.cli = dict(a.cli, call=None)
        s.refresh_facts({})
        self.assertIsNone(a.launch)

    def test_a_call_that_was_only_counted_by_order(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_A}, 't1', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        s = self.session()
        a = s.agents[self.C1]
        a.cli = dict(a.cli, calls=['t1'], calls_certain=[False])
        s.refresh_facts({})
        self.assertIsNone(a.launch)

    def test_a_call_whose_line_has_no_message_id(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_A}, 't1', None)])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        s = self.session()
        s.refresh_facts({})
        self.assertIsNone(s.agents[self.C1].launch)

    def test_the_span_carries_the_message_id(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && claude -p "%s"' % LONG_A}, 't1', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        self.session()
        span = self.links.decisions[self.C1].call.span
        self.assertEqual((span.call_id, span.msg_id), ('t1', 'msg_X'))
        self.assertIsNone(Span('s', None, 'c', 1.0, None).msg_id)                                  # the default: not known


class CodexExecFromBash(Fixture):
    """A `codex exec` thread started by a Bash call of a Claude record: the group of its launch is the message of that line."""

    def test_the_linker_keeps_the_message_of_the_call(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, os.path.join(self.proj, self.PARENT + '.jsonl'))
        d = json.loads(self.tool(T0, 'Bash', {'command': 'cd /w && codex exec -o /w/r1/B.md "%s"' % LONG_A}, 'tb1', 'msg_Z'))
        b = d['message']['content'][0]
        s.codex.note_bash(d, T0, b)
        c = s.codex.calls['tb1']
        self.assertEqual((c['grp'], c['prov']), ('msg_Z', 'claude'))

    def test_the_key_of_the_thread(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.id = self.PARENT
        for tid, mid in (('tb1', 'msg_Z'), ('tb2', 'msg_Z'), ('tb3', 'msg_W')):
            d = json.loads(self.tool(T0, 'Bash', {'command': 'cd /w && codex exec "%s"' % LONG_A}, tid, mid))
            s.codex.note_bash(d, T0, d['message']['content'][0])
        keys = []
        for i, tid in enumerate(('tb1', 'tb2', 'tb3')):
            a = exec_agent('X%d' % i, link={'sid': self.PARENT, 'node': None, 'rule': 'prompt', 'call': tid, 'parent_kind': 'claude'})
            s.agents[a.id] = a
            keys.append(a)
        s.refresh_facts({})
        k = [a.launch for a in keys]
        self.assertEqual(k[0], LaunchKey('claude', self.PARENT, None, 'msg_Z', 'tb1'))
        self.assertEqual(k[0].gkey(), k[1].gkey())
        self.assertNotEqual(k[0].gkey(), k[2].gkey())

    def test_a_thread_linked_by_a_guess_or_without_the_call_has_none(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, os.path.join(self.proj, self.PARENT + '.jsonl'))
        d = json.loads(self.tool(T0, 'Bash', {'command': 'cd /w && codex exec "%s"' % LONG_A}, 'tb1', 'msg_Z'))
        s.codex.note_bash(d, T0, d['message']['content'][0])
        for link in ({'sid': self.PARENT, 'node': None, 'rule': 'time', 'call': 'tb1'}, {'sid': self.PARENT, 'node': None, 'rule': 'prompt', 'call': None},
                     {'sid': self.PARENT, 'node': None, 'rule': 'proc', 'call': 'unknown-call'}, {'sid': self.PARENT, 'node': None, 'rule': 'cache'}):
            a = exec_agent('Y', link=link)
            s.agents['Y'] = a
            s.refresh_facts({})
            self.assertIsNone(a.launch, link)


class AssignmentsInFront(Fixture):
    """The words `NAME=value` in front of a command are assignments of the shell: what follows them is still the command position (`BULLPEN_ROOM=x claude -p …`), as it is after `env NAME=value`.
    A word that only travels (`echo`, `tmux send-keys`, a heredoc, an argument) stays no launch."""

    YES = ("BULLPEN_ROOM=/x claude -p 'a'", "cd /w && (BULLPEN_ROOM=/x BULLPEN_SEAT=r1/S claude -p m 'a' & claude -p m 'b' & wait)", "A=1 B=2 nohup claude -p x",
           "for i in 1 2; do BULLPEN_SEAT=r1/$i claude -p x; done", 'X="a b" claude -p x', "if true; then A=1 claude -p x; fi", "ls\nA=1 claude -p x", "echo hi; A=1 timeout 5 claude --print x")
    CODEX_YES = ("BULLPEN_ROOM=/x codex exec -o o.md 'x'", "cd /w && BULLPEN_ROOM=/x BULLPEN_SEAT=r1/B codex exec 'x'", "A=1 nohup codex exec 'x'", "(A=1 codex exec 'x' &)")
    NO = ("echo BULLPEN_ROOM=/x claude -p 'a'", "BULLPEN_ROOM=/x echo claude -p a", "FOO=1 tmux send-keys 'claude -p x'", "X=1; echo claude -p y", "cat <<EOF\nA=1 claude -p x\nEOF",
          "grep A=1 claude -p x", "A=1 claude mcp list", "A=1 claude --version", "A=1 pgrep claude -p", "A=1 codex exec --help", "echo A=1 codex exec x", "A=1 echo codex exec x")

    def test_what_is_a_launch(self):
        from board import link as L
        for c in self.YES:
            self.assertTrue(L.CLAUDE_LAUNCH_RE.search(L.shell_code(c)), c)
            self.assertEqual(len(L.launch_facts(c, L.shell_code(c), '/w', None, 'claude')) >= 1, True, c)
        for c in self.CODEX_YES:
            self.assertTrue(L.LAUNCH_RE.search(L.shell_code(c)), c)
        for c in self.NO:
            code = L.shell_code(c)
            self.assertFalse(L.CLAUDE_LAUNCH_RE.search(code) or L.LAUNCH_RE.search(code), c)

    def test_the_same_launch_with_and_without_the_assignment_has_the_same_facts(self):
        from board import link as L
        plain, given = "cd /w && claude -p --model m 'review it' > talk/r1/A.md", "cd /w && BULLPEN_ROOM=/x BULLPEN_SEAT=r1/A claude -p --model m 'review it' > talk/r1/A.md"
        a, b = (L.launch_facts(c, L.shell_code(c), '/', None, 'claude') for c in (plain, given))
        self.assertEqual(len(a), 1)
        self.assertEqual([(d['cwd'], d['n'], d['arg'], d['resume'], d['persist'], [(r.op, r.path_resolved) for r in d['redirects']]) for d in a],
                         [(d['cwd'], d['n'], d['arg'], d['resume'], d['persist'], [(r.op, r.path_resolved) for r in d['redirects']]) for d in b])
        self.assertEqual(b[0]['redirects'][0].path_resolved, '/w/talk/r1/A.md')

    def test_a_codex_call_with_assignments(self):
        c = server.cx_parse_call(T0, 'tx1', {'command': 'cd /w && BULLPEN_ROOM=/x BULLPEN_SEAT=r1/B codex exec -o talk/r1/B.md "%s"' % LONG_A}, '/w')
        self.assertTrue(c['launch'])
        (L,) = c['L']
        self.assertEqual((L['out'], L['scwd'], L['literal'] > 40), ('talk/r1/B.md', '/w', True))

    def test_the_child_of_a_launch_after_assignments_is_linked(self):
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': 'cd /w && BULLPEN_ROOM=/x claude -p "%s"' % LONG_A, 'description': 'one'}, 't1', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        s = self.session()
        s.refresh_facts({})
        a = s.agents[self.C1]
        self.assertEqual((a.cli['call'], a.cli['rule']), ('t1', 'content'))
        self.assertEqual(a.launch, LaunchKey('claude', self.PARENT, None, 'msg_X', 't1'))

    def test_two_children_of_one_call_that_gives_one_of_them_a_tag_are_both_linked(self):
        cmd = "cd /w && (BULLPEN_ROOM=/x BULLPEN_SEAT=r1/S claude -p '%s' & claude -p '%s' & wait)" % (LONG_A, LONG_B)
        self.write(self.PARENT, [self.tool(T0, 'Bash', {'command': cmd, 'description': 'both'}, 't1', 'msg_X')])
        self.write(self.C1, self.child(T0 + 5, LONG_A))
        self.write(self.C2, self.child(T0 + 6, LONG_B))
        s = self.session()
        self.assertEqual(sorted(s.agents), sorted([self.C1, self.C2]))
        s.refresh_facts({})
        self.assertEqual(s.agents[self.C1].launch.gkey(), s.agents[self.C2].launch.gkey())


class CodexShell(unittest.TestCase):
    """A command of a Codex thread that launches `codex exec`: commands of one exec call are launched together; a command that is in no exec call is a group of its own."""
    R = '019c0000-0000-7000-8000-00000000000a'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.work = os.path.join(self.root, 'work')
        os.makedirs(self.work)
        day = os.path.join(self.root, '.codex', 'sessions', '2026', '10', '04')
        os.makedirs(day)
        self.path = os.path.join(day, 'rollout-2026-10-04T00-00-00-%s.jsonl' % self.R)
        meta = {'id': self.R, 'session_id': self.R, 'timestamp': iso(T0), 'cwd': self.work, 'originator': 'codex-tui', 'source': 'cli'}
        self.lines = [line(T0, 'session_meta', meta, 0), line(T0 + 1, 'event_msg', {'type': 'task_started'}, 1)]
        self.n = 1
        self.flush()
        self.links, self.index = server.LinkIndex(), server.CodexIndex()
        self.links.ready.set()
        stack = patched(HOME=self.root, CLAUDE_HOME=os.path.join(self.root, '.claude'), PROJECTS=os.path.join(self.root, '.claude', 'projects'), CODEX_HOME=os.path.join(self.root, '.codex'),
                        CODEX_SESSIONS=os.path.join(self.root, '.codex', 'sessions'), CODEX_NAMES=os.path.join(self.root, '.codex', 'session_index.jsonl'), CODEX=self.index, LINKS=self.links)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        self.index.refresh(force=True)
        self.session = server.CodexSession(self.index.get(self.R))
        self.session.poll()

    def flush(self):
        with open(self.path, 'wb') as f:
            f.write(b'\n'.join(self.lines) + b'\n')

    def add(self, typ, pt, payload):
        self.n += 1
        self.lines.append(line(T0 + 10 + self.n, typ, dict([('type', pt)] + list(payload.items())), self.n))        # (the keys in the order Codex writes them: the readers look at the front of a line)

    def settle(self):
        self.flush()
        self.index.refresh(force=True)
        self.session.poll()

    def command(self, iid, cmd='codex exec "%s"' % LONG_A):
        item = dict([('type', 'CommandExecution'), ('id', iid), ('process_id', '5%s' % self.n), ('command', ['/bin/bash', '-lc', cmd]), ('cwd', 'file://' + self.work),
                     ('parsed_cmd', [{'type': 'unknown', 'cmd': cmd}]), ('source', 'unified_exec_startup'), ('status', 'completed'), ('stdout', ''), ('stderr', ''), ('aggregated_output', ''),
                     ('exit_code', 0), ('duration', {'secs': 0, 'nanos': 1000000}), ('formatted_output', '')])
        self.add('event_msg', 'item_completed', {'thread_id': self.R, 'turn_id': 'u', 'item': item})

    def test_the_commands_of_one_exec_call_are_one_group(self):
        self.add('response_item', 'custom_tool_call', {'call_id': 'call_1', 'name': 'exec', 'input': 'text'})
        self.command('item_1')
        self.command('item_2')
        self.add('response_item', 'custom_tool_call_output', {'call_id': 'call_1', 'output': 'x'})
        self.command('item_3')                                                                        # no exec call open
        self.settle()
        calls = self.session.codex.calls
        self.assertEqual({k: (c['grp'], c['prov']) for k, c in calls.items()}, {'item_1': ('call_1', 'codex'), 'item_2': ('call_1', 'codex'), 'item_3': ('item_3', 'codex')})

    def test_two_open_exec_calls_say_nothing(self):
        self.add('response_item', 'custom_tool_call', {'call_id': 'call_1', 'name': 'exec', 'input': 'text'})
        self.add('response_item', 'custom_tool_call', {'call_id': 'call_2', 'name': 'exec', 'input': 'text'})
        self.command('item_1')
        self.settle()
        self.assertEqual(self.session.codex.calls['item_1']['grp'], 'item_1')

    def test_the_index_keeps_the_exec_call_of_a_command(self):
        self.add('response_item', 'custom_tool_call', {'call_id': 'call_1', 'name': 'exec', 'input': 'text'})
        self.command('item_1')
        self.settle()
        (c,) = self.index.cmds(self.R)
        self.assertEqual(c['exec'][0], 'call_1')

    def test_the_hint_of_a_command_of_the_orchestrator_knows_its_group(self):
        self.add('response_item', 'custom_tool_call', {'call_id': 'call_1', 'name': 'exec', 'input': 'text'})
        self.command('item_1', 'mkdir -p talk/r1')
        self.settle()
        talk = os.path.join(self.work, 'talk')
        self.assertEqual(self.session.orch_hint_facts[talk].calls, frozenset(('item_1',)))
        self.assertEqual(self.session.orch_hint_facts[talk].groups, frozenset((('codex', self.R, None, 'call_1'),)))


class NativeSubAgent(unittest.TestCase):
    def sub(self):
        a = SE.CodexAgent({'id': 'S1', 'path': '/s', 'cwd': '/w', 'meta_ts': 1.0, 'model': '', 'kind': 'sub', 'agent_path': '/root/s1', 'prefix_ord': 1}, {})
        a.runtime = ('ROOT', '/root.jsonl')
        return a

    def session(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, '/nonexistent/x.jsonl')
        return s

    def test_one_call_one_group(self):
        s = self.session()
        a, b = self.sub(), self.sub()
        b.id = 'S2'
        a.launch_src, b.launch_src = ('PARENT', 'call_1', T0), ('PARENT', 'call_2', T0)
        s.agents['S1'], s.agents['S2'] = a, b
        s.refresh_facts({})
        self.assertEqual(a.launch, LaunchKey('codex', 'ROOT', 'PARENT', '-:call_1', 'call_1'))
        self.assertNotEqual(a.launch.gkey(), b.launch.gkey())                                       # the same turn and the same parent: still each its own

    def test_the_turn_of_the_parent_is_in_the_group(self):
        fake = types.SimpleNamespace(get=lambda tid: {'turns': [{'start': 1.0, 'end': 50.0, 'user': None}, {'start': 60.0, 'end': None, 'user': None}]})
        with patched(CODEX=fake):
            self.assertEqual(SE.Session._turn_of('P', 30.0), '0')
            self.assertEqual(SE.Session._turn_of('P', 70.0), '1')
            self.assertEqual(SE.Session._turn_of('P', 55.0), '-')

    def test_a_sub_agent_whose_spawn_is_not_known_has_none(self):
        s = self.session()
        a = self.sub()
        s.agents['S1'] = a
        s.refresh_facts({})
        self.assertIsNone(a.launch)

    def test_the_started_event_of_the_parent_sets_where_it_came_from(self):
        fs = FakeSession(sid='R', provider='codex')
        lk = server.CodexLinker(fs)
        a = self.sub()
        a.sub_name = 's1'
        lk.agents['S1'] = a
        fs.agents['S1'] = a
        started = CF.collab('R', 'started', T0 + 2, call_id='call_7', agent_thread_id='S1', agent_path='/root/s1')
        fake = types.SimpleNamespace(collab=lambda tid: (started,) if tid == 'R' else (), get=lambda tid: None, title=lambda e: '')
        with patched(CODEX=fake):
            lk._collab({'S1': {}})
        self.assertEqual(a.launch_src, ('R', 'call_7', T0 + 2))


if __name__ == '__main__':
    unittest.main()
