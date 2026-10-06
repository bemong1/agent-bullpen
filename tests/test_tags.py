"""The room tag of an agent (CONTRACT 2.4): `BULLPEN_ROOM` and `BULLPEN_SEAT`, read from the environment of its live process or from the command that launched it.

  - the names, apart from the ones the links of who started whom read (`lineage.TAG_NAMES`)
  - Linux `/proc/<pid>/environ` (values as they are) and macOS `ps -E` (a value must have the shape of its name; one with a blank in it is not read)
  - the command: `VAR=value cmd`, `export VAR=value`, a relative path from the shell, no `$`, one value
  - environ before the command; an ended agent by the command it was launched with last; an inherited value is no tag
  - the room is a folder that exists (realpath) that no debate cannot be in; the seat is `name` or `rN/name`

    python3 -m unittest tests.test_tags
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402

from board import lineage, procs, runstate as RS, units as U  # noqa: E402
from board.facts import Tag  # noqa: E402

T0 = 1790000000.0
NAMES = (b'BULLPEN_ROOM', b'BULLPEN_SEAT')


def wait_for_exec(p):
    """Until the child is the program it was started as (before that it still has the environment of the process that made it)."""
    deadline = time.time() + 10
    while time.time() < deadline and procs.has_proc():
        try:
            with open('/proc/%d/cmdline' % p.pid, 'rb') as f:
                if f.read().startswith(sys.executable.encode()):
                    return
        except OSError:
            pass
        time.sleep(0.02)


class Names(unittest.TestCase):
    def test_the_names_are_apart_from_the_ones_of_the_links(self):
        self.assertEqual(lineage.TAG_NAMES, NAMES)
        self.assertEqual(lineage.ENV_NAMES, (b'CLAUDE_CODE_SESSION_ID', b'CLAUDE_PID', b'CODEX_THREAD_ID', b'CODEX_SESSION_ID'))
        self.assertFalse(set(lineage.ENV_NAMES) & set(lineage.TAG_NAMES))


class EnvironOfAProcess(unittest.TestCase):
    """The environment of a live process: only the values of the names that were asked for come out."""

    def spawn(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith('BULLPEN_')}
        base.update(env)
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], env=base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.terminate(), p.wait()))
        wait_for_exec(p)
        return p

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_linux_environ_gives_the_values_as_they_are(self):
        p = self.spawn(BULLPEN_ROOM='/some where/with blanks', BULLPEN_SEAT='r1/A', SECRET_TOKEN='do-not-read')
        got = procs.env_values(p.pid, lineage.TAG_NAMES)
        self.assertEqual(got, {'BULLPEN_ROOM': '/some where/with blanks', 'BULLPEN_SEAT': 'r1/A'})      # NUL separated: a blank is only a blank

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_a_name_that_is_not_there_is_not_in_the_answer(self):
        p = self.spawn(BULLPEN_SEAT='A')
        self.assertEqual(procs.env_values(p.pid, lineage.TAG_NAMES), {'BULLPEN_SEAT': 'A'})

    @unittest.skipUnless(sys.platform == 'darwin', 'reads the environment with `ps -E`')
    def test_macos_reads_the_environment_of_a_real_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            room = os.path.realpath(tmp)
            p = self.spawn(BULLPEN_ROOM=room, BULLPEN_SEAT='r1/A', SECRET_TOKEN='do-not-read')
            got = None
            for _ in range(50):                                                          # `ps` may not show the environment of a process that has just started
                got = procs.env_values(p.pid, lineage.TAG_NAMES)
                if got:
                    break
                time.sleep(0.1)
                procs._ENV.clear()
            self.assertEqual(got, {'BULLPEN_ROOM': room, 'BULLPEN_SEAT': 'r1/A'})


class PsShapes(unittest.TestCase):
    """What `ps -E` gives is blank separated, so a value counts only when it has the shape of its name."""

    def pick(self, raw):
        return procs._pick_env(raw, lineage.TAG_NAMES, False)

    def test_a_path_without_a_blank_and_a_seat(self):
        self.assertEqual(self.pick(b'claude -p x BULLPEN_ROOM=/w/talk BULLPEN_SEAT=r1/A HOME=/h'), {'BULLPEN_ROOM': '/w/talk', 'BULLPEN_SEAT': 'r1/A'})
        self.assertEqual(self.pick(b'x BULLPEN_SEAT=A BULLPEN_ROOM=/a/b X=1'), {'BULLPEN_ROOM': '/a/b', 'BULLPEN_SEAT': 'A'})
        self.assertEqual(self.pick(b'x BULLPEN_ROOM=/w/talk'), {'BULLPEN_ROOM': '/w/talk'})
        self.assertEqual(self.pick(b'x BULLPEN_SEAT=round2/B'), {'BULLPEN_SEAT': 'round2/B'})

    def test_a_path_with_a_blank_cannot_be_read(self):
        self.assertEqual(self.pick(b'x BULLPEN_ROOM=/w/my talk BULLPEN_SEAT=r1/A HOME=/h'), {'BULLPEN_SEAT': 'r1/A'})

    def test_the_shape_of_the_name(self):
        for raw in (b'x BULLPEN_ROOM=talk', b'x BULLPEN_ROOM=', b'x BULLPEN_SEAT=a/b/c', b'x BULLPEN_SEAT=r1/', b'x BULLPEN_SEAT=x/y'):
            self.assertEqual(self.pick(raw), {}, raw)

    def test_two_values_are_not_known(self):
        self.assertEqual(self.pick(b'x BULLPEN_ROOM=/a BULLPEN_ROOM=/b BULLPEN_SEAT=A'), {'BULLPEN_SEAT': 'A'})

    def test_the_numbers_and_ids_of_the_links_are_as_they_were(self):
        sid = '11111111-1111-4111-8111-111111111111'
        self.assertEqual(procs._pick_env(b'x CLAUDE_CODE_SESSION_ID=%s CLAUDE_PID=123 CODEX_THREAD_ID=a/b' % sid.encode(), lineage.ENV_NAMES, False),
                         {'CLAUDE_CODE_SESSION_ID': sid, 'CLAUDE_PID': '123'})

    def test_linux_has_no_such_limits(self):
        self.assertEqual(procs._pick_env(b'BULLPEN_ROOM=/w/my talk\0BULLPEN_SEAT=anything at all\0X=1', lineage.TAG_NAMES, True), {'BULLPEN_ROOM': '/w/my talk', 'BULLPEN_SEAT': 'anything at all'})


class TagBase(unittest.TestCase):
    CHILD = '22222222-2222-4222-8222-222222222222'
    SID = '11111111-1111-4111-8111-111111111111'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.work = os.path.join(self.root, 'work')
        self.talk = os.path.join(self.work, 'talk')
        os.makedirs(self.talk)
        self.s = server.Session.__new__(server.Session)
        server.Session.__init__(self.s, '/nonexistent/%s.jsonl' % self.SID)
        self.s.id = self.SID
        self.s.cwd = self.work
        self.procs = []

    def agent(self, command=None, calls=('tc1',), cwd=None, live=None, aid=None, prompt=None):
        a = server.Agent(aid or self.CHILD, {'description': 'child'})
        a.origin = 'cli'
        a.cwd = self.work
        a.cli = {'sid': self.SID, 'node': None, 'rule': 'out', 'call': calls[-1], 'bash_ts': T0, 'calls': list(calls), 'calls_certain': [True] * len(calls)}
        if prompt is not None:
            a.talk.append({'ts': T0 + 1, 'kind': 'in', 'text': prompt})                          # the instruction the child began with
        self.s.agents[a.id] = a
        if command is not None:
            self.s.spawn_cmds[calls[-1]] = (command, cwd or self.work)
        if live is not None:
            self.s._cli_procs[a.id] = RS.Proc(True, tuple(live))
        return a

    def spawn(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith('BULLPEN_')}
        base.update(env)
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], env=base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.terminate(), p.wait()))
        wait_for_exec(p)
        return p

    def tag(self, a, status='running'):
        self.s.refresh_facts({a.id: status})
        return a.room_tag


class FromTheCommand(TagBase):
    def test_a_variable_in_front_of_the_command(self):
        a = self.agent('BULLPEN_ROOM=%s BULLPEN_SEAT=r1/A claude -p "x"' % self.talk)
        self.assertEqual(self.tag(a, 'done'), Tag(self.talk, 'r1/A', 'command', a.run))

    def test_export_before_the_command(self):
        a = self.agent('export BULLPEN_ROOM=%s; export BULLPEN_SEAT=A\nclaude -p "x"' % self.talk)
        self.assertEqual(self.tag(a, 'done'), Tag(self.talk, 'A', 'command', None))

    def test_a_room_alone(self):
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk)
        self.assertEqual(self.tag(a, 'done'), Tag(self.talk, None, 'command', None))

    def test_a_relative_room_counts_from_the_folder_of_the_shell(self):
        a = self.agent('cd work && BULLPEN_ROOM=talk claude -p x', cwd=self.root)
        self.assertEqual(self.tag(a, 'done').room, self.talk)
        a = self.agent('BULLPEN_ROOM=work/talk claude -p x', cwd=self.root)
        self.assertEqual(self.tag(a, 'done').room, self.talk)

    def test_a_value_that_is_not_one_literal_is_not_given(self):
        for cmd in ('BULLPEN_ROOM=$WHERE claude -p x', 'BULLPEN_ROOM=%s claude -p a; BULLPEN_ROOM=%s/other claude -p b' % (self.talk, self.talk), 'echo BULLPEN_ROOM=%s' % self.talk + '; claude -p x'):
            a = self.agent(cmd)
            self.assertIsNone(self.tag(a, 'done'), cmd)

    def test_a_command_in_a_heredoc_or_in_quotes_gives_nothing(self):
        a = self.agent("cat > notes.md <<'EOF'\nBULLPEN_ROOM=%s\nEOF\nclaude -p x" % self.talk)
        self.assertIsNone(self.tag(a, 'done'))

    def test_the_last_command_that_launched_it(self):
        self.s.spawn_cmds['tc1'] = ('BULLPEN_ROOM=%s BULLPEN_SEAT=r1/A claude -p x' % self.talk, self.work)
        self.s.spawn_cmds['tc2'] = ('BULLPEN_ROOM=%s BULLPEN_SEAT=r2/A claude -p --resume x' % self.talk, self.work)
        a = self.agent(None, calls=('tc1', 'tc2'))
        self.assertEqual(self.tag(a, 'done').seat, 'r2/A')                                      # resumed with another seat: that one

    def test_a_resume_that_names_no_tag_has_none(self):
        self.s.spawn_cmds['tc1'] = ('BULLPEN_ROOM=%s claude -p x' % self.talk, self.work)
        a = self.agent(None, calls=('tc1', 'tc2'))                                              # the last call has no tag in it (so it is not kept)
        self.assertIsNone(self.tag(a, 'done'))

    def test_the_run_of_the_tag_is_the_last_run(self):
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk)
        a.feed({'type': 'user', 'timestamp': '2026-10-04T00:00:00.000Z', 'message': {'content': 'x'}})          # (the child began with the instruction the command gave it)
        self.assertEqual(self.tag(a, 'done').run, 1)


class OneCallTwoChildren(TagBase):
    """One Bash call that starts two children: the variables of one of them are not the tag of the other (a false participant is worse than a missed one). What is read is the piece of the
    command that started that child; when the pieces cannot be told apart and differ, nobody gets a tag."""
    S, P = '33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444'

    def two(self, command, s_prompt='review the ledger', p_prompt='review the invoices', calls=('tc1',)):
        s = self.agent(command, calls=calls, aid=self.S, prompt=s_prompt)
        p = self.agent(None, calls=calls, aid=self.P, prompt=p_prompt)
        self.s.refresh_facts({self.S: 'done', self.P: 'done'})
        return s.room_tag, p.room_tag

    def test_the_variables_of_one_piece_belong_to_its_child(self):
        """The case the scenarios found (`ctr launch=call tag=seat`): the seat of S was put on P as well."""
        cmd = "cd %s && (BULLPEN_ROOM=%s BULLPEN_SEAT=r1/S claude -p --model m 'review the ledger' & claude -p --model m 'review the invoices' & wait)" % (self.work, self.talk)
        s, p = self.two(cmd)
        self.assertEqual(s, Tag(self.talk, 'r1/S', 'command', None))
        self.assertIsNone(p)

    def test_the_same_when_the_other_piece_comes_first(self):
        cmd = "cd %s && (claude -p 'review the invoices' & BULLPEN_ROOM=%s BULLPEN_SEAT=r1/S claude -p 'review the ledger' & wait)" % (self.work, self.talk)
        s, p = self.two(cmd)
        self.assertEqual((s, p), (Tag(self.talk, 'r1/S', 'command', None), None))

    def test_two_seats_for_two_children(self):
        cmd = "export BULLPEN_ROOM=%s; BULLPEN_SEAT=r1/A claude -p 'review the ledger'; BULLPEN_SEAT=r1/B claude -p 'review the invoices'" % self.talk
        s, p = self.two(cmd)
        self.assertEqual((s.seat, p.seat, s.room, p.room), ('r1/A', 'r1/B', self.talk, self.talk))

    def test_pieces_that_cannot_be_told_apart_and_differ_give_nobody_a_tag(self):
        cmd = "cd %s && (BULLPEN_ROOM=%s BULLPEN_SEAT=r1/S claude -p 'review the ledger' & claude -p 'review the invoices' & wait)" % (self.work, self.talk)
        self.assertEqual(self.two(cmd, 'something else entirely', 'and this is something else again'), (None, None))      # the children began with other words than the pieces say
        cmd = "(BULLPEN_ROOM=%s BULLPEN_SEAT=r1/A claude -p 'same words' & BULLPEN_ROOM=%s BULLPEN_SEAT=r1/B claude -p 'same words' & wait)" % (self.talk, self.talk)
        self.assertEqual(self.two(cmd, 'same words', 'same words', calls=('tc2',)), (None, None))

    def test_pieces_that_give_the_same_need_no_telling_apart(self):
        cmd = "export BULLPEN_ROOM=%s; claude -p \"$ONE\" & claude -p \"$TWO\" & wait" % self.talk             # (their words are no literals: either may be either child)
        s, p = self.two(cmd, 'unrelated', 'also unrelated')
        self.assertEqual((s, p), (Tag(self.talk, None, 'command', None), Tag(self.talk, None, 'command', None)))

    def test_a_command_that_starts_agents_like_this_one_and_none_of_them_is_it_starts_nobody_with_a_tag(self):
        """O12 (review 2): the pieces say `one` and `two`, the children began with other words: they were started by something that is not in the text."""
        cmd = "export BULLPEN_ROOM=%s; claude -p 'one' & claude -p 'two' & wait" % self.talk
        self.assertEqual(self.two(cmd, 'unrelated', 'also unrelated'), (None, None))
        s, p = self.two(cmd, 'one', 'two', calls=('tc5',))
        self.assertEqual((s, p), (Tag(self.talk, None, 'command', None), Tag(self.talk, None, 'command', None)))

    def test_a_variable_in_a_subshell_does_not_reach_what_is_outside_it(self):
        cmd = "(export BULLPEN_ROOM=%s; claude -p 'review the ledger'); claude -p 'review the invoices'" % self.talk
        s, p = self.two(cmd)
        self.assertEqual((s, p), (Tag(self.talk, None, 'command', None), None))

    def test_a_piece_with_a_variable_that_is_no_literal_gives_no_value(self):
        cmd = "BULLPEN_ROOM=$WHERE claude -p 'review the ledger'"
        self.assertEqual(self.two(cmd), (None, None))

    def test_env_and_prefix_words(self):
        cmd = "env BULLPEN_ROOM=%s nohup claude -p 'review the ledger'" % self.talk
        s, _ = self.two(cmd)
        self.assertEqual(s.room, self.talk)

    def test_a_loop_over_one_piece_gives_every_child_of_it_the_same(self):
        cmd = "for i in 1 2; do BULLPEN_ROOM=%s claude -p \"review $i\"; done" % self.talk
        s, p = self.two(cmd)
        self.assertEqual((s.room, p.room), (self.talk, self.talk))

    def test_a_launch_that_is_not_in_the_text_gets_what_the_text_gives_at_all(self):
        cmd = "BULLPEN_ROOM=%s ./start-both.sh" % self.talk
        s, p = self.two(cmd)
        self.assertEqual((s.room, p.room), (self.talk, self.talk))
        cmd = "BULLPEN_ROOM=%s ./a.sh; BULLPEN_ROOM=%s/other ./b.sh" % (self.talk, self.talk)
        self.assertEqual(self.two(cmd, calls=('tc9',)), (None, None))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_the_environment_of_a_live_child_is_its_own_whatever_the_command_says(self):
        """A live process says what it has itself; what the other piece gave is not looked at. A value that equals the orchestrator's is inherited unless the child's own piece gave it."""
        s_proc = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='r1/S')
        p_proc = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='r1/S')                             # P got the same variables from the shell of the orchestrator
        orch = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='r1/S')
        self.s.alive = lambda: (True, orch.pid, 'orch')
        cmd = "(BULLPEN_ROOM=%s BULLPEN_SEAT=r1/S claude -p 'review the ledger' & claude -p 'review the invoices' & wait)" % self.talk
        s = self.agent(cmd, aid=self.S, prompt='review the ledger', live=[s_proc.pid])
        p = self.agent(None, aid=self.P, prompt='review the invoices', live=[p_proc.pid])
        self.s.refresh_facts({self.S: 'running', self.P: 'running'})
        self.assertEqual(s.room_tag, Tag(self.talk, 'r1/S', 'environ', None))
        self.assertIsNone(p.room_tag)


class Fresh(object):
    def fresh(self, command, prompt='go', **kw):
        """A child of a call of its own (the session keeps what it read of a call: one text per call id)."""
        self.n = getattr(self, 'n', 0) + 1
        return self.agent(command, prompt=prompt, calls=('tc%d' % self.n,), **kw)

    def room_of(self, command, prompt='go', **kw):
        tag = self.tag(self.fresh(command, prompt, **kw), 'done')
        return tag.room if tag else None


class DeliveredValues(Fresh, TagBase):
    """O12 (CONTRACT 2.4): a tag is what the command gave the child: a variable in front of the command (`VAR=x cmd`, `env VAR=x cmd`) or one the shell had exported before it (`export VAR=x`) in the
    same shell, in a place that is sure to have run. A shell variable that is not exported does not reach a child; `unset`, `env -u` and `env -i` take a value away; a subshell and a branch that
    may not have run do not give one to what is outside them."""

    def test_what_is_given_to_the_child(self):
        for cmd in ('BULLPEN_ROOM=%(r)s claude -p go', 'export BULLPEN_ROOM=%(r)s; claude -p go', 'export BULLPEN_ROOM=%(r)s\nclaude -p go', 'env BULLPEN_ROOM=%(r)s claude -p go',
                    'declare -x BULLPEN_ROOM=%(r)s; claude -p go', 'BULLPEN_ROOM=%(r)s; export BULLPEN_ROOM; claude -p go', 'export BULLPEN_ROOM=/nowhere; BULLPEN_ROOM=%(r)s; claude -p go',
                    'export BULLPEN_ROOM=%(r)s && claude -p go', 'cd /w && export BULLPEN_ROOM=%(r)s && claude -p go', 'export BULLPEN_ROOM=%(r)s; (claude -p go)',
                    'export BULLPEN_ROOM=%(r)s; nohup claude -p go &', 'env -u OTHER BULLPEN_ROOM=%(r)s claude -p go', 'BULLPEN_ROOM=%(r)s env -u OTHER claude -p go',
                    'BULLPEN_ROOM=%(r)s timeout -s KILL 60 claude -p go', 'export BULLPEN_ROOM=%(r)s; env -i claude -p other; claude -p go',
                    'for i in 1; do export BULLPEN_ROOM=%(r)s; done; claude -p go'):                 # (a loop over a list runs its body, and what it exports stays in the shell)
            with self.subTest(cmd):
                self.assertEqual(self.room_of(cmd % {'r': self.talk}), self.talk)

    def test_a_shell_variable_that_is_not_exported_does_not_reach_the_child(self):
        for cmd in ('BULLPEN_ROOM=%(r)s; claude -p go', 'BULLPEN_ROOM=%(r)s\nclaude -p go', 'BULLPEN_ROOM=%(r)s && claude -p go', 'declare BULLPEN_ROOM=%(r)s; claude -p go',
                    'readonly BULLPEN_ROOM=%(r)s; claude -p go', 'local BULLPEN_ROOM=%(r)s; claude -p go', 'BULLPEN_ROOM=%(r)s; BULLPEN_SEAT=r1/A; claude -p go'):
            with self.subTest(cmd):
                self.assertIsNone(self.room_of(cmd % {'r': self.talk}))
        self.assertIsNone(self.tag(self.fresh('BULLPEN_SEAT=r1/A; claude -p go'), 'done'))

    def test_what_is_taken_away_is_not_given(self):
        for cmd in ('export BULLPEN_ROOM=%(r)s; unset BULLPEN_ROOM; claude -p go', 'export BULLPEN_ROOM=%(r)s; export -n BULLPEN_ROOM; claude -p go',
                    'export BULLPEN_ROOM=%(r)s; env -u BULLPEN_ROOM claude -p go', 'export BULLPEN_ROOM=%(r)s; env -i claude -p go', 'BULLPEN_ROOM=%(r)s env -u BULLPEN_ROOM claude -p go',
                    'BULLPEN_ROOM=%(r)s env -i claude -p go', 'export BULLPEN_ROOM=%(r)s; env --unset=BULLPEN_ROOM claude -p go', 'export BULLPEN_ROOM=%(r)s; (unset BULLPEN_ROOM; claude -p go)',
                    'export BULLPEN_ROOM=%(r)s; BULLPEN_ROOM= claude -p go'):
            with self.subTest(cmd):
                self.assertIsNone(self.room_of(cmd % {'r': self.talk}))

    def test_a_value_given_to_what_is_not_the_child_does_not_reach_it(self):
        for cmd in ('(export BULLPEN_ROOM=%(r)s); claude -p go', '(export BULLPEN_ROOM=%(r)s; true) && claude -p go', 'true || export BULLPEN_ROOM=%(r)s; claude -p go',
                    'test -d x && export BULLPEN_ROOM=%(r)s; claude -p go', 'if [ -d x ]; then export BULLPEN_ROOM=%(r)s; fi; claude -p go',
                    'f() { export BULLPEN_ROOM=%(r)s; }; claude -p go', 'echo export BULLPEN_ROOM=%(r)s; claude -p go', 'exit 0; export BULLPEN_ROOM=%(r)s; claude -p go'):
            with self.subTest(cmd):
                self.assertIsNone(self.room_of(cmd % {'r': self.talk}))

    def test_pieces_told_by_the_instruction_when_it_is_one(self):
        """Another piece with an instruction that is no literal (`$P`, a file on stdin) may be this child: it is not told from the piece that says the same words. The tag is then what both give, or none."""
        both = 'export BULLPEN_ROOM=%s; ' % self.talk
        seat = lambda cmd, prompt: (lambda t: t.seat if t else None)(self.tag(self.fresh(both + cmd, prompt), 'done'))
        self.assertIsNone(seat('P=go; BULLPEN_SEAT=r1/A claude -p "$P" & BULLPEN_SEAT=r1/B claude -p go & wait', 'go'))            # A's words are a variable: it may be this child
        self.assertIsNone(seat('echo go | BULLPEN_SEAT=r1/A claude -p > a.md & BULLPEN_SEAT=r1/B claude -p go > b.md & wait', 'go'))
        self.assertIsNone(seat('BULLPEN_SEAT=r1/A claude -p < a.txt & BULLPEN_SEAT=r1/B claude -p "Write r1/B.md" & wait', 'Write r1/B.md'))
        self.assertEqual(seat('BULLPEN_SEAT=r1/A claude -p "$(cat a.txt)" & BULLPEN_SEAT=r1/B claude -p --model opus "review" & wait', 'opus'), 'r1/A')     # B says `review`: it is not this child (`opus` is the value of an option)
        self.assertEqual(seat('BULLPEN_SEAT=r1/A claude -p "$(cat a.txt)" & BULLPEN_SEAT=r1/B claude -p "review" & wait', 'review'), None)
        self.assertEqual(seat('BULLPEN_SEAT=r1/A claude -p "review a" & BULLPEN_SEAT=r1/B claude -p "review b" & wait', 'review b'), 'r1/B')
        self.assertIsNone(seat('BULLPEN_SEAT=r1/A claude -p "go" & ./start_b.sh & wait', 'brief for b'))                       # the piece says `go`: another launch started this child

    def test_the_text_after_the_launch_does_not_matter(self):
        a = self.fresh('export BULLPEN_ROOM=%s; claude -p go; unset BULLPEN_ROOM; claude -p other' % self.talk)
        self.assertEqual(self.tag(a, 'done').room, self.talk)


class ScriptLaunch(Fresh, TagBase):
    """O12 (review 2): what a script that the text does not show is started with is what every command that could have started it gives it, the way a piece is given its variables: the ones the
    shell exported before it in the same shell and nothing taken away since, and what stands in front of the command. When the text cannot say which command it was, nothing."""

    def setUp(self):
        TagBase.setUp(self)
        self.other = os.path.join(self.work, 'other')
        os.makedirs(self.other)

    def test_what_a_script_is_given(self):
        for cmd, want in (('export BULLPEN_ROOM=%(r)s; ./launch.sh', 'r'),
                          ('export BULLPEN_ROOM=%(o)s; BULLPEN_ROOM=%(r)s; ./launch.sh', 'r'),                       # exported, then given another value: the script has the other
                          ('export BULLPEN_ROOM=%(r)s; BULLPEN_ROOM=%(o)s; ./launch.sh', 'o'),
                          ('BULLPEN_ROOM=%(r)s ./launch.sh', 'r'), ('env BULLPEN_ROOM=%(r)s ./launch.sh', 'r'),
                          ('(export BULLPEN_ROOM=%(r)s; ./launch.sh)', 'r'),
                          ('export BULLPEN_ROOM=%(r)s; ./a.sh; ./b.sh', 'r'),
                          ('export BULLPEN_ROOM=%(r)s; BULLPEN_ROOM=%(o)s true; ./launch.sh', 'r'),                  # what is put in front of `true` reaches `true`
                          ('if false; then export BULLPEN_ROOM=%(r)s; fi; ./launch.sh', None),                       # may not have run
                          ('test -d x && export BULLPEN_ROOM=%(r)s; ./launch.sh', None),
                          ('(export BULLPEN_ROOM=%(r)s); ./launch.sh', None),                                        # in a subshell the script is not in
                          ('export BULLPEN_ROOM=%(r)s; unset BULLPEN_ROOM; ./launch.sh', None),
                          ('export BULLPEN_ROOM=%(r)s; env -u BULLPEN_ROOM ./launch.sh', None),
                          ('export BULLPEN_ROOM=%(r)s; env -i ./launch.sh', None),
                          ('BULLPEN_ROOM=%(r)s; ./launch.sh', None),                                                 # not exported
                          ('BULLPEN_ROOM=%(r)s true; ./launch.sh', None),                                            # given to `true`
                          ('BULLPEN_ROOM=%(r)s ./a.sh; ./b.sh', None),                                               # one of the two gets it: which one started the child is not in the text
                          ('BULLPEN_ROOM=%(r)s ./a.sh; BULLPEN_ROOM=%(o)s ./b.sh', None)):
            with self.subTest(cmd):
                room = self.room_of(cmd % {'r': self.talk, 'o': self.other})
                self.assertEqual(room, {'r': self.talk, 'o': self.other, None: None}[want])

    def test_the_script_that_the_real_shell_starts_has_what_the_model_says(self):
        """The model against bash itself, for the shapes that matter (the script prints what it was given)."""
        import subprocess
        for cmd, want in (('export V=a; V=b; sh -c \'printf "%s" "$V"\'', 'b'), ('if false; then export V=a; fi; sh -c \'printf "%s" "$V"\'', ''),
                          ('V=a; sh -c \'printf "%s" "$V"\'', ''), ('export V=a; env -u V sh -c \'printf "%s" "$V"\'', ''), ('(export V=a); sh -c \'printf "%s" "$V"\'', '')):
            got = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != 'V'}).stdout
            self.assertEqual(got, want, cmd)


class ResumedChildren(TagBase):
    """O12 (review 2): the command that resumed a child is read for the run it began: the id it resumes tells which child a piece is, and its instruction is compared with the first instruction of that run,
    not of the child. A piece when there are pieces and none is the agent: nobody is given what the others have."""
    A, B = 'aaaaaaaa-0000-4000-8000-aaaaaaaaaaaa', 'bbbbbbbb-0000-4000-8000-bbbbbbbbbbbb'

    def child(self, aid, first, resume, calls=('tc1', 'tr1')):
        a = self.agent(None, calls=calls, aid=aid)
        a.feed({'type': 'user', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(T0 + 2)), 'message': {'content': first}})
        a.feed({'type': 'cost-state', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(T0 + 25)), 'totalDuration': 1000})
        a.feed({'type': 'user', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(T0 + 50)), 'message': {'content': resume}})
        return a

    def rooms(self, template, instruction='round 2'):
        r2 = os.path.join(self.work, 'talk2')
        os.makedirs(r2, exist_ok=True)
        self.s._tag_cmds.clear()                                                                       # (one text per call id is kept)
        self.s.spawn_cmds['tr1'] = (template.format(R=self.talk, R2=r2, A=self.A, B=self.B), self.work)
        a, b = self.child(self.A, 'brief for A', instruction), self.child(self.B, 'brief for B', instruction)
        self.s.refresh_facts({a.id: 'done', b.id: 'done'})
        show = lambda t: None if t is None else ('R' if t.room == self.talk else 'R2' if t.room == r2 else t.room, t.seat)
        return show(a.room_tag), show(b.room_tag)

    def test_the_run_that_a_resume_began_is_what_its_instruction_is_compared_with(self):
        self.assertEqual(self.child(self.A, 'brief for A', 'round 2').runs.runs[1].epoch, 2)           # (the second run, and its instruction as the board has it)
        self.assertEqual(self.s._run_instruction(self.child(self.A, 'brief for A', 'round 2'), 'tr1'), 'round 2')
        self.assertEqual(self.s._run_instruction(self.child(self.A, 'brief for A', 'round 2'), 'tc1'), 'brief for A')

    def test_a_piece_that_resumes_another_child_is_not_this_one(self):
        for template, want in (('(export BULLPEN_ROOM={R}; claude -p -r {A} "round 2") & claude -p -r {B} "round 2" & wait', (('R', None), None)),
                               ('(export BULLPEN_ROOM={R}; claude -p -r {A} "round 2"); claude -p -r {B} "round 2"', (('R', None), None)),
                               ('(export BULLPEN_ROOM={R}; claude -p --resume {A} "round 2") & claude -p --resume {B} "round 2" & wait', (('R', None), None)),
                               ('export BULLPEN_ROOM={R}; claude -p -r {A} "round 2" & env -u BULLPEN_ROOM claude -p -r {B} "round 2" & wait', (('R', None), None)),
                               ('claude -p -r {A} "round 2" & BULLPEN_ROOM={R2} claude -p -r {B} "round 2" & wait', (None, ('R2', None))),
                               ('BULLPEN_ROOM={R} true; claude -p -r {A} "round 2"; claude -p -r {B} "round 2"', (None, None)),
                               ('export BULLPEN_ROOM={R2}; BULLPEN_SEAT=r2/B claude -p -r {B} "round 2"', (None, ('R2', 'r2/B'))),
                               ('if [ -d {R} ]; then export BULLPEN_ROOM={R}; fi; claude -p -r {A} "round 2"; claude -p -r {B} "round 2"', (None, None))):
            with self.subTest(template):
                self.assertEqual(self.rooms(template), want)

    def test_a_resume_with_no_id_is_told_by_the_instruction_of_its_run(self):
        self.assertEqual(self.rooms('BULLPEN_ROOM={R} claude -p -c "round 2"'), (('R', None), ('R', None)))        # (nothing tells the children apart: the same for both)
        self.assertEqual(self.rooms('BULLPEN_ROOM={R} claude -p -c "something else"'), (None, None))             # the pieces say another instruction than either child's run began with


class InheritedFromTheLauncher(TagBase):
    """O12 ②: a value the child has because the one that launched it had it (and not because its own command gave it) is no tag: not the orchestrator's alone, the launcher's as well. A resume that gives
    no tag drops what the environment said of the run before."""
    G = '55555555-5555-4555-8555-555555555555'

    def orch(self, env=None):
        proc = self.spawn(**(env or {}))
        self.s.alive = lambda: (True, proc.pid, 'orch')

    def test_a_grandchild_has_the_tag_of_the_child_that_launched_it_only_by_inheritance(self):
        c1 = self.agent('BULLPEN_ROOM=%s BULLPEN_SEAT=r1/A claude -p x' % self.talk, calls=('tc1',), live=[self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='r1/A').pid])
        grand = self.agent(None, calls=('tg1',), aid=self.G, live=[self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='r1/A').pid])
        grand.cli = dict(grand.cli, sid=c1.id, node=c1.id)                                         # started from a call in the record of the child
        self.orch()
        self.s.refresh_facts({c1.id: 'running', self.G: 'running'})
        self.assertEqual(c1.room_tag, Tag(self.talk, 'r1/A', 'environ', None))
        self.assertIsNone(grand.room_tag)
        self.assertEqual(grand.tag_env, (None, None))

    def test_the_grandchild_that_ended_does_not_get_it_back_from_the_cache(self):
        c1 = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, calls=('tc1',), live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        grand = self.agent(None, calls=('tg1',), aid=self.G, live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        grand.cli = dict(grand.cli, sid=c1.id, node=c1.id)
        self.orch()
        self.s.refresh_facts({c1.id: 'running', self.G: 'running'})
        self.s._cli_procs[grand.id] = RS.Proc(False)
        self.s.refresh_facts({c1.id: 'done', self.G: 'done'})
        self.assertIsNone(grand.room_tag)

    def test_a_grandchild_whose_own_command_gives_the_tag_has_it(self):
        c1 = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, calls=('tc1',), live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        grand = self.agent('BULLPEN_ROOM=%s claude -p y' % self.talk, calls=('tg1',), aid=self.G, live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        grand.cli = dict(grand.cli, sid=c1.id, node=c1.id)
        self.orch()
        self.s.refresh_facts({c1.id: 'running', self.G: 'running'})
        self.assertEqual(grand.room_tag, Tag(self.talk, None, 'environ', None))

    def test_a_launcher_that_nobody_saw_leaves_nothing_to_tell_the_value_from(self):
        """The child that launched it ended before its environment was looked at and named no tag in its own command: what the grandchild has may be what it passed on. Not a tag."""
        c1 = self.agent(None, calls=('tc1',))                                                        # (no process, no environment kept, no command text)
        grand = self.agent(None, calls=('tg1',), aid=self.G, live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        grand.cli = dict(grand.cli, sid=c1.id, node=c1.id)
        self.orch()
        self.s.refresh_facts({c1.id: 'done', self.G: 'running'})
        self.assertIsNone(c1.room_tag)
        self.assertIsNone(grand.room_tag)
        self.assertEqual(grand.tag_env, (None, None))
        absent = self.agent(None, calls=('tg2',), aid='66666666-6666-4666-8666-666666666666', live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        absent.cli = dict(absent.cli, sid=self.SID, node='77777777-7777-4777-8777-777777777777')      # started by an agent the page has not got
        self.s.refresh_facts({absent.id: 'running'})
        self.assertIsNone(absent.room_tag)

    def test_a_resume_that_gives_no_tag_drops_what_the_run_before_had(self):
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, calls=('tc1',), live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        self.orch()
        self.assertEqual(self.tag(a).room, self.talk)
        self.assertEqual(a.tag_env, (self.talk, None))
        self.s._cli_procs[a.id] = RS.Proc(False)                                                    # it ended
        a.cli = dict(a.cli, call='tc2', calls=['tc1', 'tc2'], calls_certain=[True, True])           # resumed by a call that names no tag (its text is not kept)
        self.assertIsNone(self.tag(a, 'done'))
        self.assertFalse(any(a.tag_env or ()))                                                       # (nothing is kept of the run before)
        self.assertIsNone(self.tag(a, 'done'))

    def test_the_same_run_that_ended_keeps_what_it_had(self):
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, calls=('tc1',), live=[self.spawn(BULLPEN_ROOM=self.talk).pid])
        self.orch()
        self.assertEqual(self.tag(a).room, self.talk)
        self.s._cli_procs[a.id] = RS.Proc(False)
        self.s.spawn_cmds.pop('tc1')                                                                # (the text of the command is gone: only what the environment said is left)
        self.assertEqual(self.tag(a, 'done'), Tag(self.talk, None, 'environ', None))


class CodexExecPieces(TagBase):
    """The same for `codex exec`: the piece that started the thread, told by what the thread began with."""

    def thread(self, tid, prompt):
        from test_codex_page import exec_agent
        a = exec_agent(tid, link={'sid': self.SID, 'node': None, 'rule': 'prompt', 'call': 'tx1', 'parent_kind': 'claude'}, user=prompt)
        a.turns[0]['call'] = 'tx1'
        self.s.agents[a.id] = a
        return a

    def test_two_threads_of_one_call(self):
        self.s.spawn_cmds['tx1'] = ("cd %s && (BULLPEN_ROOM=%s BULLPEN_SEAT=r1/S codex exec -o o.md 'review the ledger please' & codex exec 'review the invoices please' & wait)" % (self.work, self.talk), self.work)
        s = self.thread('X1', 'review the ledger please')
        p = self.thread('X2', 'review the invoices please')
        self.s.refresh_facts({'X1': 'done', 'X2': 'done'})
        self.assertEqual(s.room_tag, Tag(self.talk, 'r1/S', 'command', s.run))
        self.assertIsNone(p.room_tag)


class FromTheEnvironment(TagBase):
    def orch(self, **env):
        """An orchestrator whose process can be read, with these variables in its environment."""
        p = self.spawn(**env)
        self.s.alive = lambda: (True, p.pid, 'orch')
        return p

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_a_live_process_says_it_itself(self):
        p = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='r1/B')
        self.orch()
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % os.path.join(self.work, 'other'), live=[p.pid])
        os.makedirs(os.path.join(self.work, 'other'))
        self.assertEqual(self.tag(a), Tag(self.talk, 'r1/B', 'environ', None))                  # the environment, not the command

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_processes_that_differ_know_nothing(self):
        p, q = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A'), self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='B')
        self.orch()
        a = self.agent(live=[p.pid, q.pid])
        self.assertEqual(self.tag(a), Tag(self.talk, None, 'environ', None))                    # the room they agree on, no seat

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_the_agent_that_ended_keeps_what_its_environment_said(self):
        p = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A')
        self.orch()
        a = self.agent(live=[p.pid])
        self.assertEqual(self.tag(a).seat, 'A')
        self.s._cli_procs[a.id] = RS.Proc(False)                                                 # the process is gone, and the command did not name a tag
        self.assertEqual(self.tag(a, 'done'), Tag(self.talk, 'A', 'environ', None))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_an_inherited_value_is_no_tag(self):
        """The orchestrator was started with the variable set in its shell: every child has it, nobody set it for the child."""
        child = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A')
        orch = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A')
        self.s.alive = lambda: (True, orch.pid, 'orch')
        a = self.agent('claude -p x', live=[child.pid])
        self.assertIsNone(self.tag(a))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_the_command_that_gives_the_value_again_makes_it_a_tag(self):
        child = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A')
        orch = self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A')
        self.s.alive = lambda: (True, orch.pid, 'orch')
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, live=[child.pid])
        self.assertEqual(self.tag(a), Tag(self.talk, None, 'environ', None))                     # the room was given with the command, the seat only inherited

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_when_the_environment_of_the_orchestrator_cannot_be_read_a_value_only_the_environment_gives_is_inherited(self):
        """Two children that got the variable from the shell of the orchestrator must not become a room of two: with nothing to tell the value from an inherited one it is not a tag. A value the
        command gives is one (the same command that gives the room does not give the seat: only the room is kept)."""
        children = [self.spawn(BULLPEN_ROOM=self.talk, BULLPEN_SEAT='A') for _ in range(2)]
        self.s.alive = lambda: (False, None, None)                                                  # no process of the orchestrator to read
        plain = self.agent('claude -p x', live=[children[0].pid])
        self.assertIsNone(self.tag(plain))
        self.s.agents.clear()
        given = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, calls=('tc9',), live=[children[1].pid])
        self.assertEqual(self.tag(given), Tag(self.talk, None, 'environ', None))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'reads /proc/<pid>/environ')
    def test_a_value_the_orchestrator_does_not_have_is_a_tag(self):
        child = self.spawn(BULLPEN_ROOM=self.talk)
        orch = self.spawn()
        self.s.alive = lambda: (True, orch.pid, 'orch')
        a = self.agent('claude -p x', live=[child.pid])
        self.assertEqual(self.tag(a).room, self.talk)

    def test_a_process_that_cannot_be_read_falls_back_on_the_command(self):
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % self.talk, live=[2 ** 22 + 12345])           # (a process that is not there)
        self.assertEqual(self.tag(a), Tag(self.talk, None, 'command', None))


class TheCheck(TagBase):
    def test_a_room_that_is_not_a_folder(self):
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % os.path.join(self.work, 'missing'))
        self.assertIsNone(self.tag(a, 'done'))
        os.makedirs(os.path.join(self.work, 'missing'))
        self.assertIsNotNone(self.tag(a, 'done'))                                               # the folder is made later: the tag is there from then on

    def test_a_file_is_not_a_room(self):
        path = os.path.join(self.work, 'a.txt')
        open(path, 'w').close()
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % path)
        self.assertIsNone(self.tag(a, 'done'))

    def test_the_room_is_the_realpath(self):
        link = os.path.join(self.root, 'alias')
        os.symlink(self.talk, link)
        a = self.agent('BULLPEN_ROOM=%s claude -p x' % link)
        self.assertEqual(self.tag(a, 'done').room, self.talk)

    def test_a_folder_that_is_too_broad_or_one_the_tools_keep(self):
        keep = U.STATE_DIRS[0].rstrip(os.sep)
        os.makedirs(os.path.join(keep, 'plans'), exist_ok=True)
        for room in ('/', os.path.dirname(U.HOME) if os.path.dirname(U.HOME) != U.HOME else '/', U.HOME, keep, os.path.join(keep, 'plans')):
            a = self.agent('BULLPEN_ROOM=%s claude -p x' % room)
            self.assertIsNone(self.tag(a, 'done'), room)

    def test_the_seat_is_a_name_or_a_round_and_a_name(self):
        for i, (seat, want) in enumerate((('A', 'A'), ('r1/A', 'r1/A'), ('round2/B', 'round2/B'), ('r1/A.md', 'r1/A'), ('A.md', 'A'), ('a/b/c', None), ('', None), ('r1/', None))):
            a = self.agent('BULLPEN_ROOM=%s BULLPEN_SEAT=%s claude -p x' % (self.talk, "'%s'" % seat if seat else "''"), calls=('call%d' % i,))
            tag = self.tag(a, 'done')
            self.assertEqual(tag.seat if tag else 'no tag', want, seat)                          # a seat that is not a seat leaves the room

    def test_a_seat_without_a_room_is_no_tag(self):
        a = self.agent('BULLPEN_SEAT=A claude -p x')
        self.assertIsNone(self.tag(a, 'done'))

    def test_what_has_no_process_of_its_own_has_no_tag(self):
        a = server.Agent('a%016x' % 1, {'description': 'sub', 'toolUseId': 'toolu_1'})
        self.s.agents[a.id] = a
        self.s.spawn_cmds['toolu_1'] = ('BULLPEN_ROOM=%s claude -p x' % self.talk, self.work)
        self.assertIsNone(self.tag(a, 'done'))


if __name__ == '__main__':
    unittest.main()
