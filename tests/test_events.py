"""The write, read and command events the collectors keep for the debate judgment (CONTRACT 2.1-2.2, J1): one test for every line of the table of what writes, and the rules of keeping them.

  - `agents.shell_writes(places=True)` and the deciding / masked places of a command (`ShellPlace`)
  - Claude `Write`, `Edit`, `MultiEdit`, `NotebookEdit`; Codex `FileChange`; Bash and `CommandExecution` (`ClaudeTools`, `CodexItems`, `ClaudeBash`, `BackgroundBash`)
  - the files read, the command windows and the exec cell around a Codex command (`Reads`, `Windows`)
  - what is kept and what is not (`Retention`, `PartialCodex`), the orchestrator's own events (`OrchEvents`)
  - the planned outputs of Codex `-o` and of `claude -p >` and their events (`PlannedCodex`, `PlannedCli`, `PlannedNoFileRead`)
  - `facts_gen` (`FactsGen`)

    python3 -m unittest tests.test_events
"""
import contextlib
import dataclasses
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
from test_codex_page import FakeSession, exec_agent, line  # noqa: E402  (functions and a plain class: no TestCase is imported twice)

from board import agents as AG, units as U  # noqa: E402
from board.codex_parse import cx_decode, cx_start_offset  # noqa: E402
from board.facts import CHECKED, PROOFS, Planned, WRITE_KINDS, WriteEvent  # noqa: E402

T0 = 1790000000.0


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))


def places(cmd, cwd='/w'):
    """[(path, decides, kind)] the command writes in a place that is sure to have run, files of every kind."""
    return [(w.path, w.decides, w.kind) for w in AG.shell_writes(cmd, cwd, True, places=True, md_only=False)]


class ShellPlace(unittest.TestCase):
    """Where in a command a write stands: a deciding place is one where an exit status 0 of the whole command says that it was run and worked; a masked place is one where a later command
    can hide a failure (CONTRACT 2.2 W2)."""

    def test_the_examples_of_the_contract(self):
        for cmd, want in (("mkdir -p r1 && cat > r1/A.md <<'EOF'\nx\nEOF\n", [('/w/r1/A.md', True, 'replace')]),
                          ("printf hi | tee r1/A.md", [('/w/r1/A.md', True, 'replace')]),
                          ("cat > r1/A.md <<'EOF'\nx\nEOF\nls r1", [('/w/r1/A.md', False, 'replace')]),
                          ("set -C; printf new > X; true", [('/w/X', False, 'replace')]),
                          ("{ a; b > X; }", [('/w/X', True, 'replace')]),
                          ("( cd d && cat > X )", [('/w/d/X', True, 'replace')]),
                          ("echo hi | tee X | wc -l", [('/w/X', False, 'replace')]),
                          ("a > X && b", [('/w/X', True, 'replace')]),
                          ("a > X || b", [('/w/X', False, 'replace')]),
                          ("! cat > X", [('/w/X', False, 'replace')]),
                          ("if true; then cat > X; fi", [])):                  # not sure to have run: no event
            self.assertEqual(places(cmd), want, cmd)

    def test_more_places(self):
        for cmd, decides in (('cat > X <<-EOF\n\tx\n\tEOF\n', True),                 # the line that ends a heredoc is no command that comes after
                             ('cat > X <<EOF\n\nEOF\n', True),
                             ('cat <<EOF > X\nbody\nEOF\n', True),
                             ('cat > X &', False),                                 # in the background: its status says nothing
                             ('cat > X; true', False),
                             ('cat > X\ntrue', False),
                             ('for t in a b; do cat > X; done', False),            # in a loop, whatever the words
                             ('echo $(cat > X)', None),                            # in a substitution: not even read
                             ('{ a > X; } && b', True),
                             ('{ a > X; } || b', False),
                             ('{ a > X; } | b', False),
                             ('mkdir a && mkdir b && cat > X', True),
                             ('mkdir a && cat > X && b', True),
                             ('cat > X && b || c', False),
                             ('cd /w2 && a > X', True)):
            got = places(cmd)
            if decides is None:
                self.assertEqual([d for _, d, _ in got], [False] if got else [], cmd)
            else:
                self.assertEqual([d for _, d, _ in got], [decides], cmd)

    def test_what_runs_in_another_process_or_may_end_the_shell_early_is_a_masked_place(self):
        """P2-4: `bash -c "…"` has the status of what is in it; the shell that reads this command only knows that `bash` ended. A `cd` that is not joined by `&&` may have failed, and the file is where
        the shell was then. A trap on the exit can turn any status into 0 (P3), a process substitution and a coproc run on their own."""
        for cmd, want in (('bash -c "printf x > A.md; true"', [('/w/A.md', False, 'replace')]),
                          ('bash -c "printf x > A.md"', [('/w/A.md', False, 'replace')]),
                          ("sh -c 'cd d && printf x > A.md'", [('/w/d/A.md', False, 'replace')]),
                          ('cd /d/r2; printf x > A.md', [('/d/r2/A.md', False, 'replace')]),
                          ('cd /d/r2\nprintf x > A.md', [('/d/r2/A.md', False, 'replace')]),
                          ('if cd /d/r2; then printf x > A.md; fi', []),
                          ('cd /d/r2 && printf x > A.md', [('/d/r2/A.md', True, 'replace')]),
                          ('cd /d && cd r2 && printf x > A.md', [('/d/r2/A.md', True, 'replace')]),
                          ('mkdir -p /d/r2 && cd /d/r2 && printf x > A.md', [('/d/r2/A.md', True, 'replace')]),
                          ('cd /d/r2; printf x > /w/A.md', [('/w/A.md', True, 'replace')]),          # the file is where it is whatever `cd` did
                          ('cd /d/r2; printf x > ~/A.md', [(os.path.expanduser('~/A.md'), True, 'replace')]),
                          ('(cd /d/r2; printf x > A.md)', [('/d/r2/A.md', False, 'replace')]),
                          ('trap "exit 0" EXIT; printf x > A.md', [('/w/A.md', False, 'replace')]),
                          ('printf x > A.md && trap "exit 0" EXIT', [('/w/A.md', False, 'replace')]),
                          ('trap "true" ERR; printf x > A.md', [('/w/A.md', False, 'replace')]),
                          ('trap - EXIT; printf x > A.md', [('/w/A.md', True, 'replace')]),
                          ('coproc printf x > A.md', [('/w/A.md', False, 'replace')]),
                          ('cat <(echo y > A.md)', []),
                          ('tee >(cat > A.md) < in', []),
                          ('echo y > >(cat > A.md)', [])):
            with self.subTest(cmd):
                self.assertEqual(places(cmd), want)

    def test_how_a_command_writes(self):
        for cmd, kind in (('echo x > A.md', 'replace'), ('echo x &> A.md', 'replace'), ('echo x >> A.md', 'append'), ('tee A.md', 'replace'), ('tee -a A.md', 'append'),
                          ('tee --append A.md', 'append'), ('tee -ia A.md', 'append'), ('tee -i A.md', 'replace')):
            self.assertEqual([k for _, _, k in places(cmd)], [kind], cmd)

    def test_files_of_every_kind_but_not_the_ones_that_are_no_work(self):
        self.assertEqual([p for p, _, _ in places('echo x > src/a.py')], ['/w/src/a.py'])
        self.assertEqual(places('echo x > /dev/null'), [])
        self.assertEqual(places('echo x > /tmp/scratch.txt'), [])                  # a scratch folder: not work
        self.assertEqual([p for p, _, _ in places('echo x > /tmp/scratch.md')], ['/tmp/scratch.md'])        # markdown is a candidate anywhere
        self.assertEqual(places('echo x > ~/.claude/notes.txt'), [])

    def test_a_repository_in_a_scratch_folder_is_work(self):
        """A checkout under /tmp: `make test > logs/x.txt` is a change of the work (the judgment's J14 counts it); the same file next to it, in no repository, is scratch."""
        with tempfile.TemporaryDirectory() as tmp:
            top = os.path.realpath(os.path.join(tmp, 'proj'))
            os.makedirs(os.path.join(top, '.git'))
            os.makedirs(os.path.join(top, 'logs'))
            os.makedirs(os.path.join(tmp, 'elsewhere'))
            self.assertTrue(top.startswith(tuple(U.SCRATCH_DIRS)))                          # (the test is only a test when the checkout is in a scratch folder)
            self.assertEqual([p for p, _, _ in places('make test > logs/x.txt', top)], [os.path.join(top, 'logs', 'x.txt')])
            self.assertEqual([p for p, _, _ in places('make test > %s/logs/new/y.txt' % top, '/w')], [os.path.join(top, 'logs', 'new', 'y.txt')])      # (the folder is not there yet)
            self.assertEqual(places('make test > logs/x.txt', os.path.join(tmp, 'elsewhere')), [])
            self.assertEqual(places('make test > %s/elsewhere/z.txt' % tmp, top), [])
        keep = os.path.join(U.STATE_DIRS[0], 'repo')                                         # what the tools keep of themselves is no work, repository or not
        os.makedirs(os.path.join(keep, '.git'), exist_ok=True)
        self.assertEqual(places('echo x > %s/notes.txt' % keep), [])
        self.assertEqual([p for p, _, _ in places('echo x > %s/notes.md' % keep)], [os.path.join(keep, 'notes.md')])

    def test_without_places_the_old_answer_stays(self):
        self.assertEqual(AG.shell_writes('cat > r1/A.md <<EOF\nx\nEOF', '/w'), ['/w/r1/A.md'])
        self.assertEqual(AG.shell_writes('echo hi > x.txt', '/w'), [])                   # markdown only, as it was
        self.assertEqual(AG.shell_writes('true || echo hi > x.md', '/w', True), [])

    def test_a_masked_place_is_really_one_set_C_and_true(self):
        """`set -C; printf new > X; true` ends with 0 and the file is as it was (the redirect failed on a file that is there): the status cannot be taken for the write (N06)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'X.md')
            with open(target, 'w') as f:
                f.write('old')
            cmd = 'set -C; printf new > %s; true' % target
            done = subprocess.run(['bash', '-c', cmd], stderr=subprocess.DEVNULL)
            with open(target) as f:
                self.assertEqual((done.returncode, f.read()), (0, 'old'))
            got = places(cmd, tmp)
            self.assertEqual(got, [(target, False, 'replace')])
            # the same command in a record: the event is `window`, which only the judgment's check can make sure
            a = server.Agent('a0123456789abcdef', {})
            a.feed({'type': 'assistant', 'timestamp': iso(T0), 'cwd': tmp, 'message': {'id': 'm1', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': cmd}}]}})
            a.feed({'type': 'user', 'timestamp': iso(T0 + 1), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}})
            (ev,) = a.write_events
            self.assertEqual((ev.proof, ev.ok, ev.span, ev.evidence), ('window', True, (T0, T0 + 1), 'shell'))
            self.assertIn(ev.proof, CHECKED)


class LaunchRedirects(unittest.TestCase):
    """O8: what the shell writes of the output of a child (`claude -p … > X`, `codex exec … > X`) is the child's report (its requirement and event), not a write of the one that started it.
    A redirect or a `tee` of any other command of the same text is a write as before."""

    def paths(self, cmd, **kw):
        return [(w.path, w.decides) for w in AG.shell_writes(cmd, '/w', True, places=True, md_only=False, skip_launch=True, **kw)]

    def test_the_redirect_of_a_launch_is_no_write(self):
        for cmd in ('claude -p "x" > r1/A.md', 'claude --print x >> r1/A.md', 'codex exec -o o.md "x" > r1/A.md', 'BULLPEN_ROOM=/w/talk claude -p "x" > r1/A.md',
                    'env A=1 nohup claude -p x > r1/A.md', 'A=1 timeout 5 claude -p x > r1/A.md', '(claude -p x > r1/A.md & wait)', 'cd /w && { A=1 claude -p x > r1/A.md; }',
                    'for i in 1; do B=2 claude -p x > r1/A.md; done', 'claude -p x > r1/A.md 2>&1', 'claude -p x &> r1/A.md'):
            self.assertEqual(self.paths(cmd), [], cmd)

    def test_the_redirect_of_any_other_piece_is_a_write(self):
        self.assertEqual(self.paths('echo x > r1/B.md; claude -p y > r1/A.md'), [('/w/r1/B.md', False)])
        self.assertEqual(self.paths('claude -p y > r1/A.md; cat > r1/B.md <<EOF\nx\nEOF'), [('/w/r1/B.md', True)])
        self.assertEqual(self.paths('echo hi | tee r1/A.md'), [('/w/r1/A.md', True)])
        self.assertEqual(self.paths('(A=1 claude -p x > r1/A.md & echo done > r1/B.md; wait)'), [('/w/r1/B.md', False)])
        self.assertEqual(self.paths('claude mcp list > r1/A.md'), [('/w/r1/A.md', True)])                    # not `claude -p`
        self.assertEqual(self.paths('claude --version > r1/A.md'), [('/w/r1/A.md', True)])
        self.assertEqual(self.paths('echo claude -p x > r1/A.md'), [('/w/r1/A.md', True)])                   # `echo` is the command: the words are its arguments

    def test_a_tee_that_takes_the_output_of_a_child_is_no_write_either(self):
        """O8b: `claude -p … | tee X` writes the output of the child into X: it is the child's report. Only a `tee` in the same pipeline after the launch."""
        for cmd in ('claude -p y | tee r1/A.md', 'codex exec -o o.md y | tee r1/A.md', 'A=1 nohup claude -p y | tee -a r1/A.md', 'claude -p y | sed s/a/b/ | tee r1/A.md', '{ claude -p y; } | tee r1/A.md',
                    '( A=1 claude -p y ) | tee r1/A.md', 'cd /w && claude -p y |& tee r1/A.md', 'claude -p y | { cat; } | tee r1/A.md'):
            self.assertEqual(self.paths(cmd), [], cmd)
        self.assertEqual(self.paths('claude -p y; echo z | tee r1/A.md'), [('/w/r1/A.md', True)])            # not the same pipeline
        self.assertEqual(self.paths('claude -p y && echo z | tee r1/A.md'), [])                               # (not sure to have run after a command that decides something: no event, launch or not)
        self.assertEqual(self.paths('claude -p y > r1/A.md; ls | tee r1/B.md'), [('/w/r1/B.md', True)])
        self.assertEqual(self.paths('echo z | tee r1/A.md | claude -p y'), [('/w/r1/A.md', False)])          # before the launch: it is the other command's own file
        self.assertEqual(self.paths('echo z | tee r1/A.md | wc -l'), [('/w/r1/A.md', False)])
        self.assertEqual(self.paths('claude -p y | tee r1/A.md; echo z | tee r1/B.md'), [('/w/r1/B.md', True)])
        self.assertEqual(self.paths('claude -p y | tee r1/A.md > r1/C.md'), [])                               # the redirect of the `tee` takes the output of the child as well (O11)
        self.assertEqual([w.path for w in AG.shell_writes('claude -p y | tee r1/A.md', '/w', True, places=True, md_only=False)], ['/w/r1/A.md'])        # the old way of asking stays

    def test_the_old_answer_is_the_old_answer(self):
        cmd = 'claude -p "x" > r1/A.md'
        self.assertEqual(AG.shell_writes(cmd, '/w', True), ['/w/r1/A.md'])                                  # (the page's hints read it this way)
        self.assertEqual([w.path for w in AG.shell_writes(cmd, '/w', True, places=True, md_only=False)], ['/w/r1/A.md'])

    def test_an_agent_that_launches_a_child_has_no_event_for_its_output(self):
        for origin in ('subagent', 'cli'):
            a = server.Agent('a0123456789abcdef', {'description': 'mid'})
            a.origin = origin
            a.feed({'type': 'assistant', 'timestamp': iso(T0), 'cwd': '/w', 'message': {'id': 'm1', 'content': [
                {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': "cd /w && claude -p 'x' > talk/r1/C.md; echo done > talk/r1/D.md"}}]}})
            a.feed({'type': 'user', 'timestamp': iso(T0 + 2), 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}})
            self.assertEqual([e.path for e in a.write_events], ['/w/talk/r1/D.md'], origin)
            self.assertEqual([(w.call, w.ok) for w in a.windows], [('t1', True)], origin)                      # the command is a window all the same

    def test_the_orchestrator_that_launches_a_child_has_no_event_for_its_output_either(self):
        log = AG.EventLog()
        calls = AG.ClaudeCalls('orch', log, lambda: None)
        calls.use(T0, {'id': 't1'}, 'Bash', {'command': "cd /w && (claude -p 'x' > talk/r1/A.md & codex exec -o /w/talk/r1/B.md 'y' > talk/r1/B2.md & wait)"}, '/w')
        calls.use(T0 + 5, {'id': 't2'}, 'Bash', {'command': "cat > talk/r1/ruling.md <<'EOF'\nx\nEOF"}, '/w')
        self.assertEqual([e.path for e in log.events()], ['/w/talk/r1/ruling.md'])

    def test_a_codex_shell_too(self):
        a = CodexBase('setUp')
        a.setUp()
        a.turn(T0)
        a.command(T0 + 5, 'cd /w && claude -p "x" > r1/A.md', secs=2, iid='c1')
        a.command(T0 + 9, 'cat > r1/B.md <<EOF\nx\nEOF', secs=1, iid='c2')
        self.assertEqual([e.path for e in a.a.write_events], ['/w/r1/B.md'])
        self.assertEqual([w.call for w in a.a.windows], ['c1', 'c2'])

    def test_the_child_keeps_its_requirement_and_its_event(self):
        """The same call: the launcher has no event, the child has the planned output and, at its end, the event."""
        t = CliSession('test_the_run_of_the_child')
        t.setUp()
        t.s.orch_calls.use(T0 + 1, {'id': 'tc1'}, 'Bash', {'command': "cd /w && claude -p 'x' > talk/r1/C.md"}, '/w')
        t.a.redirects = [t.Redirect(fd=1, op='>', path_raw='talk/r1/C.md', path_resolved='/w/talk/r1/C.md')]
        t.a.talk.append({'ts': T0 + 99, 'kind': 'end', 'text': 'the final answer'})
        t.s.refresh_facts({t.CHILD: 'done'})
        self.assertEqual(t.s.orch_events, [])
        self.assertEqual([p.path for p in t.a.planned], ['/w/talk/r1/C.md'])
        self.assertEqual([(e.path, e.evidence, e.proof) for e in t.a.write_events], [('/w/talk/r1/C.md', 'planned', 'content')])


class LaunchShapes(unittest.TestCase):
    """O11: the shapes a launch comes in. The piece that starts `claude -p` or `codex exec` is told the same way here (`starts_launch`, so that its redirect is no write of the one that ran the
    command) and in the reader of the link (`launch_facts`, which gives the child the redirect as its requirement): a shape one of them reads and the other does not is a file written by the wrong
    one."""

    LAUNCHES = (
        ('claude -p x > r1/A.md', 'claude'),
        ("claude -p 'Read brief.md.\nWrite the report.' > r1/A.md", 'claude'),                      # the quote holds a line break
        ('claude -p "line one\nline two" --model m > r1/A.md', 'claude'),
        ('env -u CLAUDECODE claude -p x > r1/A.md', 'claude'),                                    # the shape of a Claude that starts a Claude
        ('env -u A -u B C=1 claude -p x > r1/A.md', 'claude'),
        ('env --unset=A claude -p x > r1/A.md', 'claude'),
        ('env -i claude -p x > r1/A.md', 'claude'),
        ('env -C /w claude -p x > r1/A.md', 'claude'),
        ('timeout -s KILL 600 claude -p x > r1/A.md', 'claude'),
        ('timeout -k 5 -s TERM 60 claude -p x > r1/A.md', 'claude'),
        ('timeout --kill-after=5 --signal=TERM 60 claude -p x > r1/A.md', 'claude'),
        ('timeout --foreground 60 claude -p x > r1/A.md', 'claude'),
        ('nice -n 10 claude -p x > r1/A.md', 'claude'),
        ('nice -10 claude -p x > r1/A.md', 'claude'),
        ('ionice -c 3 claude -p x > r1/A.md', 'claude'),
        ('stdbuf -oL claude -p x > r1/A.md', 'claude'),
        ('stdbuf -o L -e0 claude -p x > r1/A.md', 'claude'),
        ('setsid claude -p x > r1/A.md', 'claude'),
        ('setsid -f claude -p x > r1/A.md', 'claude'),
        ('sudo -u me claude -p x > r1/A.md', 'claude'),
        ('time -p claude -p x > r1/A.md', 'claude'),
        ('nohup nice -n 5 timeout -s INT 9 env -u A claude -p x > r1/A.md', 'claude'),
        ('bash -c "claude -p go > r1/A.md"', 'claude'),
        ("sh -c 'cd /w && claude -p go > r1/A.md; true'", 'claude'),
        ('bash -lc "env -u A claude -p go > r1/A.md"', 'claude'),
        ('claude -p go | cat > r1/A.md', 'claude'),                                               # what the next stage writes is the output of the child
        ('claude -p go | sed s/a/b/ > r1/A.md', 'claude'),
        ('claude -p go 2>&1 | grep -v x > r1/A.md', 'claude'),
        ('( claude -p go ) > r1/A.md', 'claude'),                                                 # a redirect of the group is the output of what is in it
        ('{ claude -p go; } > r1/A.md', 'claude'),
        ('( cd /w; claude -p go ) >> r1/A.md', 'claude'),
        ('bash -c "claude -p go" > r1/A.md', 'claude'),
        ('codex e "go" > r1/A.md', 'codex'),
        ('codex exec go > r1/A.md', 'codex'),
        ('codex -c model=x exec go > r1/A.md', 'codex'),
        ('codex --config model=x --search exec go > r1/A.md', 'codex'),
        ('codex -a never -s read-only exec go > r1/A.md', 'codex'),
        ('env -u A codex e -o o.md go > r1/A.md', 'codex'),
        ('timeout -s KILL 600 codex exec go > r1/A.md', 'codex'),
    )

    def paths(self, cmd, **kw):
        return [(w.path, w.decides) for w in AG.shell_writes(cmd, '/w', True, places=True, md_only=False, skip_launch=True, **kw)]

    def test_what_a_launch_redirects_is_no_write_of_the_one_that_ran_it(self):
        for cmd, _tool in self.LAUNCHES:
            with self.subTest(cmd):
                self.assertEqual(self.paths(cmd), [])

    def test_the_reader_of_the_link_finds_the_same_launches(self):
        from board import link as L
        for cmd, tool in self.LAUNCHES:
            with self.subTest(cmd):
                facts = L.launch_facts(cmd, L.shell_code(cmd), '/w', None, tool)
                self.assertEqual(len(facts), 1)
                own = not re.search(r'\| |\) >|\} >|bash -c "claude -p go" >', cmd)                    # the redirect is one of the command that starts the child, not of the pipe or the group around it
                self.assertEqual([(r.op, r.path_resolved) for r in facts[0]['redirects'] if r.op in ('>', '>>')], [('>>' if '>>' in cmd else '>', '/w/r1/A.md')] if own else [])

    def test_a_quoted_instruction_over_several_lines_is_the_argument_of_the_launch(self):
        from board import link as L
        cmd = "cd /w && claude -p 'Read brief.md.\nWrite the report.\n' --model m > r1/B.md\necho done"
        (d,) = L.launch_facts(cmd, L.shell_code(cmd), '/w', None, 'claude')
        self.assertEqual((d['arg'], [(r.op, r.path_resolved) for r in d['redirects']]), ('Read brief.md. Write the report.', [('>', '/w/r1/B.md')]))                  # (the instruction as the link keeps it: the quoting and the line breaks folded)
        self.assertEqual(self.paths(cmd), [])
        self.assertEqual(self.paths(cmd + ' > r1/C.md'), [('/w/r1/C.md', True)])                  # (the command after the launch is a command of its own)

    def test_a_prefix_that_cannot_be_read_is_not_a_confirmed_write(self):
        """`env -S`, a prefix command with an option nobody listed: when the words after it say `claude -p` or `codex exec` the command may be a launch, and a launch's redirect is no write."""
        for cmd in ('env -S "A=1" claude -p x > r1/A.md', 'sudo --new-option claude -p x > r1/A.md', 'timeout --brand-new 5 claude -p x > r1/A.md', 'nice --what 3 codex exec x > r1/A.md',
                    "env -S 'claude -p x' > r1/A.md", "xargs -I{} claude -p {} > r1/A.md", "npx tool claude -p x > r1/A.md", "ssh host 'claude -p x' > r1/A.md"):
            with self.subTest(cmd):
                self.assertEqual(self.paths(cmd), [])

    def test_the_redirect_of_a_command_that_only_stands_in_such_a_shape_is_a_write(self):
        for cmd, want in (('env -u A echo hi > r1/A.md', [('/w/r1/A.md', True)]),
                          ('timeout -s KILL 5 tee r1/A.md', [('/w/r1/A.md', True)]),
                          ('nice -n 5 cat > r1/A.md <<EOF\nx\nEOF', [('/w/r1/A.md', True)]),
                          ('env A=1 claude --version > r1/A.md', [('/w/r1/A.md', True)]),
                          ('echo claude -p x | cat > r1/A.md', [('/w/r1/A.md', True)]),                # `echo` is the command: no child
                          ('claude -p x > r1/A.md; cat > r1/B.md <<EOF\nx\nEOF', [('/w/r1/B.md', True)]),
                          ('( claude -p x > r1/A.md ); echo y > r1/B.md', [('/w/r1/B.md', True)]),
                          ('( echo y ) > r1/B.md', [('/w/r1/B.md', True)]),                           # the group holds no launch
                          ('bash -c "echo y" > r1/B.md', [('/w/r1/B.md', True)]),
                          ("claude -p 'a\nb'; echo y > r1/B.md", [('/w/r1/B.md', True)])):
            with self.subTest(cmd):
                self.assertEqual(self.paths(cmd), want)

    def test_a_brace_that_is_an_argument_starts_nothing(self):
        """Review 2 (N1): `{` opens a group only where a command can begin. `echo { codex exec resume ID hi > r1/A.md` prints; the launch reader took it for a launch of the thread ID (a false link, rule `id`)."""
        from board import link as L
        for cmd in ('echo { codex exec resume 019c0000-0000-7000-8000-000000000001 hi > r1/A.md', 'echo { claude -p x > r1/A.md', 'echo x { claude -p y } > r1/A.md', 'printf "%s" { codex exec go > r1/A.md',
                    'codex -m e login > r1/A.md', 'codex --sandbox e login > r1/A.md', 'codex -p e login > r1/A.md', 'codex -c e=1 login > r1/A.md', 'codex e-mail > r1/A.md', 'codex login > r1/A.md'):
            with self.subTest(cmd):
                code = L.shell_code(cmd)
                self.assertEqual([len(L.launch_facts(cmd, code, '/w', None, tool)) for tool in ('claude', 'codex')], [0, 0])
                self.assertFalse(L.LAUNCH_RE.search(code) or L.CLAUDE_LAUNCH_RE.search(code))
                self.assertEqual(self.paths(cmd), [] if ' { ' in cmd else [('/w/r1/A.md', True)])        # (the redirect is a write of the one that ran it; a brace that is a word of a command makes the rest of the text not sure)
        for cmd, tool in (('{ claude -p go; } > r1/A.md', 'claude'), ('do { claude -p x; }', 'claude'), ('a && { codex exec go; }', 'codex'), ('{ { codex e go; }; }', 'codex'), ('codex -m e exec go', 'codex'),
                          ('(cd /w; { claude -p go; })', 'claude'), ('x=1; { claude -p go; }', 'claude')):
            with self.subTest(cmd):
                self.assertEqual(len(L.launch_facts(cmd, L.shell_code(cmd), '/w', None, tool)), 1)

    def test_a_shell_c_with_arguments_and_a_redirect_is_the_redirect_of_the_shell(self):
        """Review 2 (Opus P2-6): `bash -c '…' _ arg > X`: the words after the text are the shell's own and so is the redirect, the output of the child that the text starts."""
        from board import link as L
        for cmd in ('bash -c \'claude -p "$1"\' _ go > r1/A.md', 'sh -c "claude -p \\"$0\\"" go > r1/A.md', 'bash -c "claude -p x" _ a b > r1/A.md', 'bash -lc \'env -u A claude -p "$1"\' _ go >> r1/A.md',
                    'bash -c \'codex exec "$1"\' _ go > r1/A.md'):
            with self.subTest(cmd):
                self.assertEqual(self.paths(cmd), [])
                self.assertEqual(len(L.launch_facts(cmd, L.shell_code(cmd), '/w', None, 'codex' if 'codex' in cmd else 'claude')), 1)
        self.assertEqual(self.paths('bash -c \'echo "$1"\' _ hi > r1/B.md'), [('/w/r1/B.md', True)])
        self.assertEqual(self.paths("bash -c 'echo hi' ; echo y > r1/B.md"), [('/w/r1/B.md', True)])

    def test_an_inner_command_that_is_not_a_launch_is_a_masked_place(self):
        self.assertEqual(self.paths('bash -c "echo y > r1/B.md"'), [('/w/r1/B.md', False)])


class ClaudeBase(unittest.TestCase):
    def setUp(self):
        self.a = server.Agent('a0123456789abcdef', {'description': 'T1-A study'})

    def use(self, name, inp, ts, call, cwd='/w', mid='m1', extra=None):
        d = {'type': 'assistant', 'timestamp': iso(ts), 'cwd': cwd, 'message': {'id': mid, 'content': [{'type': 'tool_use', 'id': call, 'name': name, 'input': inp}]}}
        d.update(extra or {})
        self.a.feed(d)

    def result(self, call, ts, error=False, tur=None):
        d = {'type': 'user', 'timestamp': iso(ts), 'message': {'content': [dict({'type': 'tool_result', 'tool_use_id': call}, **({'is_error': True} if error else {}))]}}
        if tur is not None:
            d['toolUseResult'] = tur
        self.a.feed(d)

    def notice(self, ts, task, status='completed', summary='Background command completed', call=None):
        text = '<task-notification>\n<task-id>%s</task-id>\n%s<status>%s</status>\n<summary>%s</summary>\n</task-notification>' % (
            task, '<tool-use-id>%s</tool-use-id>\n' % call if call else '', status, summary)
        self.a.feed({'type': 'attachment', 'timestamp': iso(ts), 'attachment': {'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': text, 'timestamp': iso(ts)}})


class ClaudeTools(ClaudeBase):
    """`Write`, `Edit`, `MultiEdit`, `NotebookEdit`: an event when the call is made (the result is not known: ok None), made sure by the result."""

    def test_write_by_the_kind_its_result_gives(self):
        for i, (tur, kind) in enumerate((({'type': 'create', 'filePath': '/x/r1/A.md'}, 'create'), ({'type': 'update'}, 'replace'), (None, 'unknown'), ({}, 'unknown'))):
            call = 't%d' % i
            self.use('Write', {'file_path': '/x/r1/N%d.md' % i, 'content': 'x'}, T0 + i, call)
            self.assertEqual([(e.kind, e.ok, e.evidence, e.proof) for e in self.a.write_events][-1], ('unknown', None, 'tool', 'tool'))
            self.result(call, T0 + i + 0.5, tur=tur)
            ev = self.a.write_events[-1]
            self.assertEqual((ev.path, ev.kind, ev.ok, ev.call, ev.agent), ('/x/r1/N%d.md' % i, kind, True, call, self.a.id))

    def test_edit_multiedit_notebookedit_update_a_file(self):
        self.use('Edit', {'file_path': '/x/r1/A.md'}, T0, 'e1')
        self.use('MultiEdit', {'file_path': '/x/r1/B.md', 'edits': []}, T0 + 1, 'e2')
        self.use('NotebookEdit', {'notebook_path': '/x/n.ipynb'}, T0 + 2, 'e3')
        for call in ('e1', 'e2', 'e3'):
            self.result(call, T0 + 3)
        self.assertEqual([(e.path, e.kind, e.ok) for e in self.a.write_events], [('/x/r1/A.md', 'update', True), ('/x/r1/B.md', 'update', True), ('/x/n.ipynb', 'update', True)])

    def test_a_failed_call_is_a_failed_event(self):
        self.use('Write', {'file_path': '/x/r1/A.md'}, T0, 't1')
        self.result('t1', T0 + 1, error=True)
        self.assertEqual([(e.ok, e.kind) for e in self.a.write_events], [(False, 'unknown')])

    def test_what_is_no_write_event(self):
        self.use('Write', {'file_path': 'relative/A.md'}, T0, 't1')               # a path that is not absolute
        self.use('Write', {}, T0 + 1, 't2')
        self.use('Read', {'file_path': '/x/r1/A.md'}, T0 + 2, 't3')
        self.assertEqual(self.a.write_events, [])

    def test_the_run_of_the_event(self):
        self.a.feed({'type': 'user', 'timestamp': iso(T0 - 5), 'message': {'content': 'start'}})
        self.use('Write', {'file_path': '/x/r1/A.md'}, T0, 't1')
        self.assertEqual(self.a.write_events[0].run, 1)

    def test_the_old_lists_are_as_they_were(self):
        self.use('Write', {'file_path': '/x/r1/A.md'}, T0, 't1')
        self.use('MultiEdit', {'file_path': '/x/r1/B.md'}, T0 + 1, 't2')
        self.assertEqual([w['path'] for w in self.a.writes], ['/x/r1/A.md'])           # `writes` is the page's list: Write and Edit as before


class ClaudeBash(ClaudeBase):
    def test_a_deciding_place_has_the_result_of_the_command(self):
        for call, error, ok in (('b1', False, True), ('b2', True, False)):
            self.use('Bash', {'command': 'mkdir -p r1 && cat > r1/%s.md <<EOF\nx\nEOF' % call}, T0, call, mid='m' + call)
            self.assertEqual(self.a.write_events[-1].ok, None)
            self.result(call, T0 + 2, error=error)
            ev = self.a.write_events[-1]
            self.assertEqual((ev.path, ev.kind, ev.ok, ev.proof, ev.evidence, ev.span), ('/w/r1/%s.md' % call, 'replace', ok, 'exit', 'shell', None))

    def test_a_masked_place_gets_its_window_and_is_never_a_failure(self):
        self.use('Bash', {'command': 'cat > r1/A.md <<EOF\nx\nEOF\nls r1'}, T0, 'b1')
        self.use('Bash', {'command': 'cat > r1/B.md <<EOF\nx\nEOF\nls r1'}, T0 + 10, 'b2')
        self.result('b1', T0 + 2)
        self.result('b2', T0 + 12, error=True)
        a, b = self.a.write_events
        self.assertEqual((a.ok, a.proof, a.span), (True, 'window', (T0, T0 + 2)))
        self.assertEqual((b.ok, b.proof, b.span), (None, 'window', (T0 + 10, T0 + 12)))         # the command failed, but what failed is not told: not "failed"

    def test_append_and_every_path_of_a_command(self):
        self.use('Bash', {'command': 'echo a > A.md; echo b >> B.md; echo c | tee C.md'}, T0, 'b1')
        self.assertEqual([(e.path, e.kind) for e in self.a.write_events], [('/w/A.md', 'replace'), ('/w/B.md', 'append'), ('/w/C.md', 'replace')])
        self.assertTrue(all(e.call == 'b1' and e.ts == T0 for e in self.a.write_events))

    def test_a_command_that_may_not_have_run_has_no_event_but_has_its_window(self):
        self.use('Bash', {'command': 'true || echo hi > A.md'}, T0, 'b1')
        self.assertEqual(self.a.write_events, [])
        self.assertEqual([(w.call, w.t0, w.t1, w.ok) for w in self.a.windows], [('b1', T0, None, None)])

    def test_the_result_closes_the_window(self):
        self.use('Bash', {'command': 'ls'}, T0, 'b1')
        self.assertIsNone(self.a.windows[-1].t1)
        self.result('b1', T0 + 3)
        self.assertEqual([(w.t0, w.t1, w.ok) for w in self.a.windows], [(T0, T0 + 3, True)])
        self.use('Bash', {'command': 'false'}, T0 + 5, 'b2')
        self.result('b2', T0 + 6, error=True)
        self.assertEqual([(w.t1, w.ok) for w in self.a.windows][-1], (T0 + 6, False))


class BackgroundBash(ClaudeBase):
    """A command that went to the background has the result of the call "started", not "worked": its window and its events stay open until the completion notice."""

    def start(self, cmd='cat > r1/A.md <<EOF\nx\nEOF', task='bg1'):
        self.use('Bash', {'command': cmd, 'run_in_background': True}, T0, 'b1')
        self.result('b1', T0 + 1, tur={'backgroundTaskId': task})

    def test_nothing_is_decided_before_the_notice(self):
        self.start()
        self.assertEqual([(e.ok, e.proof) for e in self.a.write_events], [(None, 'exit')])
        self.assertEqual([(w.t1, w.ok) for w in self.a.windows], [(None, None)])

    def test_a_completed_notice_makes_it_worked(self):
        self.start()
        self.notice(T0 + 30, 'bg1')
        self.assertEqual([(e.ok, e.proof) for e in self.a.write_events], [(True, 'exit')])
        self.assertEqual([(w.t1, w.ok) for w in self.a.windows], [(T0 + 30, True)])

    def test_a_failed_notice_or_a_nonzero_exit_makes_it_failed(self):
        self.start()
        self.notice(T0 + 30, 'bg1', status='failed', summary='Background command failed with exit code 1')
        self.assertEqual([e.ok for e in self.a.write_events], [False])
        self.setUp()
        self.start()
        self.notice(T0 + 30, 'bg1', status='completed', summary='Background command completed (exit code 2)')
        self.assertEqual([e.ok for e in self.a.write_events], [False])

    def test_a_masked_place_gets_the_window_of_the_notice(self):
        self.start('cat > r1/A.md <<EOF\nx\nEOF\nls r1')
        self.notice(T0 + 30, 'bg1')
        (ev,) = self.a.write_events
        self.assertEqual((ev.ok, ev.proof, ev.span), (True, 'window', (T0, T0 + 30)))

    def test_a_notice_by_the_call_id_alone(self):
        self.start()
        self.notice(T0 + 30, 'other-task', call='b1')
        self.assertEqual([e.ok for e in self.a.write_events], [True])

    def test_a_notice_that_came_before_the_id_is_taken_too(self):
        self.use('Bash', {'command': 'cat > r1/A.md <<EOF\nx\nEOF', 'run_in_background': True}, T0, 'b1')
        self.notice(T0 + 0.5, 'bg1')
        self.result('b1', T0 + 1, tur={'backgroundTaskId': 'bg1'})
        self.assertEqual([e.ok for e in self.a.write_events], [True])

    def test_killed_is_not_worked(self):
        self.start()
        self.notice(T0 + 30, 'bg1', status='killed', summary='Background command stopped')
        self.assertEqual([e.ok for e in self.a.write_events], [False])


class CodexBase(unittest.TestCase):
    def setUp(self):
        self.a = server.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})

    def feed(self, ts, typ, pt, p):
        self.a.feed_cx({'ts': ts, 'type': typ, 'pt': pt, 'p': p, 'ord': None})

    def turn(self, ts):
        self.feed(ts, 'event_msg', 'task_started', {'type': 'task_started'})

    def item(self, ts, item):
        self.feed(ts, 'event_msg', 'item_completed', {'type': 'item_completed', 'thread_id': 'X', 'turn_id': 'u', 'item': item})

    def command(self, ts, cmd, code=0, status='completed', secs=0, nanos=0, iid='c1', cwd='file:///w', parsed=None):
        self.item(ts, {'type': 'CommandExecution', 'id': iid, 'process_id': '9', 'command': ['/bin/bash', '-lc', cmd], 'cwd': cwd, 'parsed_cmd': parsed or [{'type': 'unknown', 'cmd': cmd}],
                       'source': 'unified_exec_startup', 'status': status, 'exit_code': code, 'duration': {'secs': secs, 'nanos': nanos}})

    def call(self, ts, call_id='k1', name='exec'):
        self.feed(ts, 'response_item', 'custom_tool_call', {'type': 'custom_tool_call', 'name': name, 'call_id': call_id, 'input': 'text'})

    def output(self, ts, call_id='k1'):
        self.feed(ts, 'response_item', 'custom_tool_call_output', {'type': 'custom_tool_call_output', 'call_id': call_id, 'output': 'x'})


class CodexItems(CodexBase):
    def test_a_file_change_that_completed(self):
        self.turn(T0 + 1)
        self.item(T0 + 5, {'type': 'FileChange', 'id': 'fc1', 'status': 'completed', 'changes': {'/x/r1/A.md': {'type': 'add', 'content': 'x'}, '/x/r1/B.md': {'type': 'update', 'unified_diff': '@@'}}})
        self.assertEqual([(e.path, e.kind, e.ok, e.proof, e.evidence, e.call, e.ts, e.run) for e in self.a.write_events],
                         [('/x/r1/A.md', 'create', True, 'tool', 'tool', 'fc1', T0 + 5, 0), ('/x/r1/B.md', 'update', True, 'tool', 'tool', 'fc1', T0 + 5, 0)])

    def test_a_move_is_a_new_file_and_a_delete_is_none(self):
        self.item(T0 + 5, {'type': 'FileChange', 'id': 'fc1', 'status': 'completed', 'changes': {'/x/old.md': {'type': 'update', 'move_path': '/x/new.md'}, '/x/gone.md': {'type': 'delete'}}})
        self.assertEqual([(e.path, e.kind) for e in self.a.write_events], [('/x/new.md', 'create')])

    def test_a_change_that_did_not_complete_is_no_event(self):
        for status in ('failed', 'in_progress', None):
            self.item(T0 + 5, {'type': 'FileChange', 'id': 'fc1', 'status': status, 'changes': {'/x/r1/A.md': {'type': 'add'}}})
        self.assertEqual(self.a.write_events, [])

    def test_a_relative_path_counts_from_the_folder_of_the_thread(self):
        self.item(T0 + 5, {'type': 'FileChange', 'id': 'fc1', 'status': 'completed', 'changes': {'r1/A.md': {'type': 'add'}}})
        self.assertEqual([e.path for e in self.a.write_events], ['/w/r1/A.md'])

    def test_a_command_that_exited_zero_or_not(self):
        self.command(T0 + 5, 'mkdir -p r1 && cat > r1/A.md <<EOF\nx\nEOF', code=0, secs=2, iid='c1')
        self.command(T0 + 9, 'cat > r1/B.md <<EOF\nx\nEOF', code=1, status='failed', iid='c2')
        self.command(T0 + 12, 'cat > r1/C.md <<EOF\nx\nEOF', code=None, iid='c3')
        a, b, c = self.a.write_events
        self.assertEqual((a.path, a.ok, a.proof, a.span, a.ts, a.call), ('/w/r1/A.md', True, 'exit', None, T0 + 5, 'c1'))        # the time of the item, as a FileChange's is (J15 reads the last write: later is safer)
        self.assertEqual((b.path, b.ok, b.proof), ('/w/r1/B.md', False, 'exit'))
        self.assertEqual((c.path, c.ok, c.proof), ('/w/r1/C.md', None, 'exit'))

    def test_a_masked_place_has_the_window_of_the_command(self):
        self.command(T0 + 10, 'cat > r1/A.md <<EOF\nx\nEOF\nls r1', code=0, secs=2)
        self.command(T0 + 20, 'cat > r1/B.md <<EOF\nx\nEOF\nls r1', code=1, status='failed')
        a, b = self.a.write_events
        self.assertEqual((a.ok, a.proof, a.span), (True, 'window', (T0 + 8, T0 + 10)))
        self.assertEqual((b.ok, b.proof), (None, 'window'))

    def test_a_command_with_no_shell_text_writes_nothing(self):
        self.item(T0 + 5, {'type': 'CommandExecution', 'id': 'c1', 'process_id': '9', 'command': ['git', 'status'], 'cwd': 'file:///w', 'status': 'completed', 'exit_code': 0,
                           'duration': {'secs': 0, 'nanos': 1}})
        self.assertEqual(self.a.write_events, [])
        self.assertEqual(len(self.a.windows), 1)

    def test_the_old_lists_are_as_they_were(self):
        self.item(T0 + 5, {'type': 'FileChange', 'id': 'fc1', 'status': 'failed', 'changes': {'/x/r1/A.md': {'type': 'add'}}})
        self.assertEqual([w['path'] for w in self.a.writes], ['/x/r1/A.md'])          # (the page's list took every change then, and still does)


class Reads(ClaudeBase):
    def test_the_read_tool_and_what_cat_reads(self):
        self.use('Read', {'file_path': '/x/r1/A.md'}, T0, 'r1')
        self.use('Bash', {'command': 'cat r1/B.md | head -n 3; sed -n 1,5p r1/C.md; sed -i s/a/b/ r1/D.md'}, T0 + 1, 'b1')
        self.assertEqual([(r.path, r.via, r.ts) for r in self.a.read_events], [('/x/r1/A.md', 'tool', T0), ('/w/r1/B.md', 'shell', T0 + 1), ('/w/r1/C.md', 'shell', T0 + 1)])
        self.assertEqual(self.a.windows[0].reads, ('/w/r1/B.md', '/w/r1/C.md'))

    def test_a_call_with_no_reading_word_is_not_read_at_all(self):
        with mock.patch('board.link.shell_code', side_effect=AssertionError('read')):
            for cmd in ('ls channel moreover', 'git log --oneline', 'echo cat_food', 'python3 sedan.py'):
                self.assertEqual(AG.shell_reads(cmd, '/w'), [], cmd)             # `nl`, `more`, `sed` and `cat` are inside words

    def test_what_codex_has_read(self):
        a = server.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})
        item = {'type': 'CommandExecution', 'id': 'c1', 'process_id': '9', 'command': ['/bin/bash', '-lc', 'sed -n 1,5p r1/A.md'], 'cwd': 'file:///w', 'status': 'completed', 'exit_code': 0,
                'duration': {'secs': 0, 'nanos': 1}, 'parsed_cmd': [{'type': 'read', 'cmd': 'sed -n 1,5p r1/A.md', 'name': 'A.md', 'path': 'r1/A.md'}]}
        a.feed_cx({'ts': T0 + 5, 'type': 'event_msg', 'pt': 'item_completed', 'p': {'type': 'item_completed', 'item': item}, 'ord': None})
        self.assertEqual([(r.path, r.via) for r in a.read_events], [('/w/r1/A.md', 'codex')])
        self.assertEqual(a.windows[0].reads, ('/w/r1/A.md',))


class Windows(ClaudeBase):
    def test_a_command_is_a_window_whatever_its_result(self):
        self.use('Bash', {'command': 'a'}, T0, 'b1')
        self.use('Bash', {'command': 'b'}, T0 + 5, 'b2')
        self.use('Bash', {'command': 'c'}, T0 + 9, 'b3')
        self.result('b1', T0 + 2)
        self.result('b2', T0 + 6, error=True)
        self.assertEqual([(w.call, w.t0, w.t1, w.ok) for w in self.a.windows], [('b1', T0, T0 + 2, True), ('b2', T0 + 5, T0 + 6, False), ('b3', T0 + 9, None, None)])

    def test_the_windows_that_are_let_go_are_remembered_by_their_span(self):
        with mock.patch.object(AG, 'WINDOWS_KEEP', 3):
            a = server.Agent('a0123456789abcdef', {})
            a.ev.windows_keep = 3
            self.a = a
            for i in range(5):
                self.use('Bash', {'command': 'ls'}, T0 + 10 * i, 'b%d' % i)
                if i != 0:
                    self.result('b%d' % i, T0 + 10 * i + 2)
            self.assertEqual([w.call for w in a.windows], ['b2', 'b3', 'b4'])
            self.assertEqual(a.windows_dropped, (T0, float('inf')))                       # b0 never ended: its span has no end

    def test_a_dropped_window_that_had_ended(self):
        a = server.Agent('a0123456789abcdef', {})
        a.ev.windows_keep = 2
        self.a = a
        for i in range(4):
            self.use('Bash', {'command': 'ls'}, T0 + 10 * i, 'b%d' % i)
            self.result('b%d' % i, T0 + 10 * i + 2)
        self.assertEqual(a.windows_dropped, (T0, T0 + 12))

    def test_a_command_inside_an_exec_cell_starts_with_the_cell(self):
        a = CodexBase('setUp')
        a.setUp()
        a.turn(T0)
        a.call(T0 + 1, 'k1')
        a.command(T0 + 20, 'ls', secs=2, iid='c1')
        a.output(T0 + 21, 'k1')
        a.command(T0 + 30, 'ls', secs=1, iid='c2')                                          # no exec call open: its own start
        self.assertEqual([(w.call, w.t0, w.t1) for w in a.a.windows], [('c1', T0 + 1, T0 + 20), ('c2', T0 + 29, T0 + 30)])

    def test_two_open_exec_calls_say_nothing(self):
        a = CodexBase('setUp')
        a.setUp()
        a.turn(T0)
        a.call(T0 + 1, 'k1')
        a.call(T0 + 2, 'k2')
        a.command(T0 + 20, 'ls', secs=2, iid='c1')
        self.assertEqual([(w.t0, w.t1) for w in a.a.windows], [(T0 + 18, T0 + 20)])
        a.output(T0 + 21, 'k1')
        a.command(T0 + 30, 'ls', secs=1, iid='c2')                                           # one is left
        self.assertEqual(a.a.windows[-1].t0, T0 + 2)

    def test_a_new_turn_forgets_the_calls_of_the_one_before(self):
        a = CodexBase('setUp')
        a.setUp()
        a.turn(T0)
        a.call(T0 + 1, 'k1')
        a.turn(T0 + 50)
        a.command(T0 + 60, 'ls', secs=1)
        self.assertEqual(a.a.windows[0].t0, T0 + 59)


class Retention(unittest.TestCase):
    """What is kept: a markdown event never gives way to the others; the others are the newest OTHER_WRITES_KEEP; too many markdown events are not kept and the rest of the time is `lost`."""

    def test_a_markdown_event_outlives_six_hundred_shell_writes_to_code(self):
        a = server.Agent('a0123456789abcdef', {})
        a.feed({'type': 'assistant', 'timestamp': iso(T0), 'cwd': '/w', 'message': {'id': 'm0', 'content': [{'type': 'tool_use', 'id': 't0', 'name': 'Write', 'input': {'file_path': '/w/r1/A.md'}}]}})
        for i in range(600):
            a.feed({'type': 'assistant', 'timestamp': iso(T0 + 1 + i), 'cwd': '/w', 'message': {'id': 'm%d' % (i + 1), 'content': [
                {'type': 'tool_use', 'id': 's%d' % i, 'name': 'Bash', 'input': {'command': 'echo %d > /w/src/f%d.py' % (i, i)}}]}})
        events = a.write_events
        self.assertEqual(len([e for e in events if e.path.endswith('.md')]), 1)
        others = [e for e in events if not e.path.endswith('.md')]
        self.assertEqual(len(others), 500)
        self.assertEqual(others[0].path, '/w/src/f100.py')                                   # the newest 500
        self.assertEqual(a.writes_dropped, (T0 + 1, T0 + 100))                             # the span of the first hundred that were let go
        self.assertIsNone(a.lost)

    def test_tool_writes_to_other_files_are_kept_to_the_same_number(self):
        a = server.Agent('a0123456789abcdef', {})
        for i in range(502):
            a.feed({'type': 'assistant', 'timestamp': iso(T0 + i), 'cwd': '/w', 'message': {'id': 'm%d' % i, 'content': [
                {'type': 'tool_use', 'id': 't%d' % i, 'name': 'Edit', 'input': {'file_path': '/w/src/f%d.py' % i}}]}})
        self.assertEqual(len(a.write_events), 500)
        self.assertEqual(a.writes_dropped, (T0, T0 + 1))

    def test_too_many_markdown_events_are_not_kept_and_the_rest_is_lost(self):
        with mock.patch.object(AG, 'MD_EVENTS_MAX', 3):
            a = server.Agent('a0123456789abcdef', {})
            for i in range(5):
                a.feed({'type': 'assistant', 'timestamp': iso(T0 + i), 'cwd': '/w', 'message': {'id': 'm%d' % i, 'content': [
                    {'type': 'tool_use', 'id': 't%d' % i, 'name': 'Write', 'input': {'file_path': '/w/r1/F%d.md' % i}}]}})
            self.assertEqual([e.path for e in a.write_events], ['/w/r1/F0.md', '/w/r1/F1.md', '/w/r1/F2.md'])
            self.assertEqual(a.lost, (T0 + 3, float('inf')))                               # from the first one that was not kept

    def test_the_events_come_in_the_order_of_their_time(self):
        a = server.Agent('a0123456789abcdef', {})
        a.ev.add_write(WriteEvent('x', '/w/b.md', T0 + 5, 'create', 'tool', True))
        a.ev.add_write(WriteEvent('x', '/w/a.md', T0 + 1, 'create', 'tool', True))
        a.ev.add_write(WriteEvent('x', '/w/c.py', T0 + 3, 'create', 'tool', True))
        self.assertEqual([e.path for e in a.write_events], ['/w/a.md', '/w/c.py', '/w/b.md'])

    def test_the_kinds_and_proofs_are_the_enumerations_of_the_contract(self):
        self.assertEqual(WRITE_KINDS, ('create', 'replace', 'update', 'append', 'unknown'))
        self.assertEqual(PROOFS, ('tool', 'exit', 'window', 'sha', 'content'))
        a = server.Agent('a0123456789abcdef', {})
        a.feed({'type': 'assistant', 'timestamp': iso(T0), 'cwd': '/w', 'message': {'id': 'm', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'Bash', 'input': {'command': 'echo > x.md'}}]}})
        self.assertTrue(all(e.kind in WRITE_KINDS and e.proof in PROOFS for e in a.write_events))


class PartialCodex(unittest.TestCase):
    """A rollout over 64 MB is read from its end: what was before is a span `lost`."""

    def test_the_front_of_a_big_rollout_was_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'rollout.jsonl')
            with open(path, 'wb') as f:
                meta = line(T0, 'session_meta', {'id': 'X', 'cwd': '/w'}, 0)
                f.write(meta + b'\n')
                f.truncate(70 << 20)                                                        # a sparse file: the front is holes
                f.seek(0, 2)
                f.write(b'\n' + line(T0 + 500, 'event_msg', {'type': 'task_started'}, 5) + b'\n')
            size = os.path.getsize(path)
            start = cx_start_offset(path, size)
            self.assertGreater(start, 0)
            a = AG.CodexAgent({'id': 'X', 'path': path, 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})
            a.partial = start
            a.spawn_ts = T0 - 3
            rows = [cx_decode(line(T0 + 500, 'event_msg', {'type': 'task_started'}, 5)), cx_decode(line(T0 + 510, 'event_msg', {'type': 'task_complete', 'last_agent_message': ''}, 6))]
            self.assertIsNone(a.lost)
            for r in rows:
                a.feed_cx(r)
            self.assertEqual(a.lost, (T0 - 3, T0 + 500))                                    # from the start of the thread to the first line that was read

    def test_a_rollout_read_whole_has_nothing_lost(self):
        a = AG.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})
        a.feed_cx(cx_decode(line(T0 + 1, 'event_msg', {'type': 'task_started'}, 1)))
        self.assertIsNone(a.lost)

    def test_a_line_that_cannot_be_read_even_as_a_write_is_a_hole(self):
        from board import codex_parse
        a = AG.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})
        with mock.patch.object(codex_parse, 'CX_ITEM_MAX', 3 << 20):                          # (the stand-in of 64 MB)
            r = cx_decode(BigLines.line(T0 + 7, BigLines.command('cat > r1/A.md <<EOF\nx\nEOF', 4 << 20)))
        self.assertIsNone(r['p'])
        a.feed_cx(r)
        self.assertEqual(a.lost, (T0 + 7, T0 + 7))
        other = BigLines.line(T0 + 9, {'type': 'Reasoning', 'text': 'x' * (2 << 20)})
        a.feed_cx(cx_decode(other))
        self.assertEqual(a.lost, (T0 + 7, T0 + 7))                                          # another kind of long line is no hole of ours


class BigLines(unittest.TestCase):
    """O9: the item of a command or of a file change is read in full however long its line is (a command's output can be megabytes) up to 64 MB; only a longer one is a hole. What is
    not such an item is not decoded over 1 MB, as before."""

    @staticmethod
    def command(cmd, size, code=0):
        return {'type': 'CommandExecution', 'id': 'c1', 'process_id': '9', 'command': ['/bin/bash', '-lc', cmd], 'cwd': 'file:///w', 'parsed_cmd': [{'type': 'unknown', 'cmd': cmd}],
                'source': 'unified_exec_startup', 'status': 'completed', 'aggregated_output': 'x' * size, 'exit_code': code, 'duration': {'secs': 1, 'nanos': 0}}

    @staticmethod
    def line(ts, item):
        return json.dumps({'timestamp': iso(ts), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 'X', 'item': item}}, separators=(',', ':')).encode()

    def agent(self):
        return AG.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})

    def test_a_command_of_two_megabytes_is_a_write(self):
        raw = self.line(T0 + 7, self.command('cat > r1/A.md <<EOF\nx\nEOF', 2 << 20))
        self.assertGreater(len(raw), 2 << 20)
        r = cx_decode(raw)
        self.assertIsNotNone(r['p'])
        self.assertEqual((r['pt'], r['type'], r['ts']), ('item_completed', 'event_msg', T0 + 7))
        a = self.agent()
        a.feed_cx(r)
        self.assertEqual([(e.path, e.ok, e.call) for e in a.write_events], [('/w/r1/A.md', True, 'c1')])
        self.assertEqual([(w.call, w.ok) for w in a.windows], [('c1', True)])
        self.assertIsNone(a.lost)

    def test_a_file_change_of_two_megabytes_is_a_write(self):
        raw = self.line(T0 + 7, {'type': 'FileChange', 'id': 'f1', 'changes': {'/w/r1/A.md': {'type': 'add', 'content': 'y' * (2 << 20)}}, 'status': 'completed'})
        a = self.agent()
        a.feed_cx(cx_decode(raw))
        self.assertEqual([(e.path, e.kind) for e in a.write_events], [('/w/r1/A.md', 'create')])
        self.assertIsNone(a.lost)

    def test_the_line_of_a_command_that_failed_or_read_a_file(self):
        item = self.command('sed -n 1,5p r1/A.md', 2 << 20, code=2)
        item['status'] = 'failed'
        item['parsed_cmd'] = [{'type': 'read', 'cmd': 'sed -n 1,5p r1/A.md', 'name': 'A.md', 'path': 'r1/A.md'}]
        a = self.agent()
        a.feed_cx(cx_decode(self.line(T0 + 7, item)))
        self.assertEqual([(w.ok, w.reads) for w in a.windows], [(False, ('/w/r1/A.md',))])

    def test_what_is_no_such_item_is_not_decoded_over_a_megabyte(self):
        for item in ({'type': 'Reasoning', 'text': 'x' * (2 << 20)}, {'type': 'AgentMessage', 'text': 'x' * (2 << 20)}):
            r = cx_decode(self.line(T0 + 7, item))
            self.assertEqual((r['p'], r['pt']), (None, 'item_completed'))
        r = cx_decode(json.dumps({'timestamp': iso(T0), 'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'k', 'output': 'x' * (2 << 20)}}, separators=(',', ':')).encode())
        self.assertIsNone(r['p'])

    def test_a_small_line_is_as_it_was(self):
        r = cx_decode(self.line(T0 + 7, self.command('ls', 10)))
        self.assertEqual((r['p']['item']['id'], r['ord']), ('c1', None))
        self.assertEqual(cx_decode(b'\n'), None)
        self.assertIsNone(cx_decode(b'{"not json'))

    def test_the_stand_ins_of_the_limits(self):
        """A 70 MB line and a 2 MB one, with the limits made small: 1 MB -> 4 KB, 64 MB -> 64 KB."""
        from board import codex_parse
        small, mid, huge = self.line(T0 + 1, self.command('ls', 100)), self.line(T0 + 2, self.command('cat > r1/B.md <<EOF\nx\nEOF', 20 << 10)), self.line(T0 + 3, self.command('cat > r1/C.md <<EOF\nx\nEOF', 200 << 10))
        with mock.patch.object(codex_parse, 'CX_LINE_MAX', 4 << 10), mock.patch.object(codex_parse, 'CX_ITEM_MAX', 64 << 10):
            got = [cx_decode(x) for x in (small, mid, huge)]
        self.assertEqual([r['p'] is not None for r in got], [True, True, False])
        a = self.agent()
        for r in got:
            a.feed_cx(r)
        self.assertEqual([e.path for e in a.write_events], ['/w/r1/B.md'])
        self.assertEqual(a.lost, (T0 + 3, T0 + 3))                                          # only the one over the limit

    def test_the_head_of_a_line_says_whether_it_is_such_an_item(self):
        from board.codex_parse import cx_write_item
        self.assertTrue(cx_write_item(self.line(T0, self.command('ls', 1))))
        self.assertTrue(cx_write_item(self.line(T0, {'type': 'FileChange', 'id': 'f', 'changes': {}})))
        self.assertFalse(cx_write_item(self.line(T0, {'type': 'Reasoning', 'text': 'CommandExecution'})))
        self.assertFalse(cx_write_item(b'{"timestamp":"2026-01-01T00:00:00.000Z","type":"event_msg","payload":{"type":"task_started"}}'))
        self.assertFalse(cx_write_item(b'garbage'))


class MergeFront(unittest.TestCase):
    """`EventLog.merge_front`: an earlier part of the record, made on its own, comes before what is kept."""

    @staticmethod
    def w(path, ts):
        return WriteEvent('x', path, ts, 'create', 'tool', True)

    def test_markdown_events_are_all_kept_in_the_order_of_their_time(self):
        a, front = AG.EventLog(), AG.EventLog()
        a.add_write(self.w('/w/c.md', T0 + 30))
        front.add_write(self.w('/w/a.md', T0 + 10))
        front.add_write(self.w('/w/b.md', T0 + 20))
        g = a.gen
        a.merge_front(front)
        self.assertEqual([e.path for e in a.events()], ['/w/a.md', '/w/b.md', '/w/c.md'])
        self.assertGreater(a.gen, g)

    def test_over_the_limit_the_rest_is_lost(self):
        with mock.patch.object(AG, 'MD_EVENTS_MAX', 2):
            a, front = AG.EventLog(), AG.EventLog()
            a.add_write(self.w('/w/c.md', T0 + 30))
            for i in range(3):
                front.add_write(self.w('/w/f%d.md' % i, T0 + 10 + i))
            a.merge_front(front)
            self.assertEqual(len(a.events()), 2)
            self.assertEqual(a.lost, (T0 + 11, float('inf')))

    def test_the_others_are_the_newest_of_both(self):
        a, front = AG.EventLog(), AG.EventLog()
        for i in range(400):
            a.add_write(self.w('/w/src/n%d.py' % i, T0 + 1000 + i))
            front.add_write(self.w('/w/src/o%d.py' % i, T0 + i))
        a.merge_front(front)
        got = a.events()
        self.assertEqual(len(got), 500)
        self.assertEqual((got[0].path, got[-1].path), ('/w/src/o300.py', '/w/src/n399.py'))                 # the newest 500: the last 100 of the front and all of what was kept
        self.assertEqual(a.writes_dropped, (T0, T0 + 299))

    def test_windows_reads_and_what_the_front_could_not_keep(self):
        a, front = AG.EventLog(3), AG.EventLog(2)
        for i in range(2):
            a.add_window(AG.CmdWindow('x', 'k%d' % i, T0 + 100 + i, T0 + 101 + i, True))
        for i in range(4):
            front.add_window(AG.CmdWindow('x', 'f%d' % i, T0 + i, T0 + 0.5 + i, True))                       # (the log of the front keeps two: f0 and f1 are let go there)
        front.add_read(AG.ReadEvent('x', '/w/r.md', T0 + 1, 'codex'))
        front.widen_lost(T0 + 9, T0 + 9)
        a.merge_front(front)
        self.assertEqual([w.call for w in a.windows], ['f3', 'k0', 'k1'])
        self.assertEqual(a.windows_dropped, (T0, T0 + 2.5))                                                    # what the front let go and what is let go now
        self.assertEqual([r.path for r in a.reads], ['/w/r.md'])
        self.assertEqual(a.lost, (T0 + 9, T0 + 9))

    def test_the_part_before_the_read_is_apart_from_the_other_parts(self):
        a = AG.EventLog()
        a.set_front_lost(T0, T0 + 50)
        a.widen_lost(T0 + 70, T0 + 70)
        self.assertEqual(a.lost, (T0, T0 + 70))
        g = a.gen
        a.clear_front_lost()
        self.assertEqual(a.lost, (T0 + 70, T0 + 70))
        self.assertGreater(a.gen, g)
        g = a.gen
        a.clear_front_lost()
        self.assertEqual(a.gen, g)                                                                             # nothing to clear: nothing moved
        a.reset_record()
        self.assertIsNone(a.lost)


class FrontOfBigRollouts(unittest.TestCase):
    """O9 (2): a rollout over 64 MB is read from its end; its front is read once in the background for what writes (the item of a command or of a file change), and until it is done that part is `lost`."""

    ITEMS = [(T0 + 10, 'cmd', 'cat > r1/A.md <<EOF\nx\nEOF'), (T0 + 20, 'fc', '/w/r1/B.md'), (T0 + 30, 'cmd', 'echo x > src/z.py'), (T0 + 40, 'cmd', 'ls')]

    def setUp(self):
        from board import codex_parse
        self.codex_parse = codex_parse
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'rollout-x.jsonl')
        self.build()

    def item_line(self, ts, kind, what, n):
        if kind == 'fc':
            item = {'type': 'FileChange', 'id': 'f%d' % n, 'changes': {what: {'type': 'add', 'content': 'y' * 200}}, 'status': 'completed'}
        else:
            item = BigLines.command(what, 50)
            item['id'] = 'c%d' % n
        return line(ts, 'event_msg', {'type': 'item_completed', 'thread_id': 'X', 'item': item}, n)

    def build(self, hole=66 << 20):
        front = [line(T0, 'session_meta', {'id': 'X', 'cwd': '/w'}, 0), line(T0 + 1, 'event_msg', {'type': 'task_started'}, 1)]
        n = 2
        for ts, kind, what in self.ITEMS[:2]:
            front.append(self.item_line(ts, kind, what, n))
            n += 1
        front.append(line(T0 + 25, 'response_item', {'type': 'function_call_output', 'call_id': 'k', 'output': 'o' * (3 << 20)}, n))        # a long line that is no item
        n += 1
        for ts, kind, what in self.ITEMS[2:]:
            front.append(self.item_line(ts, kind, what, n))
            n += 1
        with open(self.path, 'wb') as f:
            f.write(b'\n'.join(front) + b'\n')
            self.front_end = f.tell()
            f.truncate(self.front_end + hole)                                                      # a sparse file: the middle is holes
            f.seek(0, 2)
            f.write(b'\n' + line(T0 + 500, 'event_msg', {'type': 'task_started'}, 5000) + b'\n')
            f.write(self.item_line(T0 + 510, 'cmd', 'cat > r1/T.md <<EOF\nx\nEOF', 5001) + b'\n')
        self.size = os.path.getsize(self.path)
        self.partial = self.codex_parse.cx_start_offset(self.path, self.size)

    def agent(self, kind='root', prefix=None, tid='X'):
        e = {'id': tid, 'path': self.path, 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': kind}
        if prefix is not None:
            e['prefix_ord'] = prefix
        a = AG.CodexAgent(e, {})
        a.partial = self.partial
        a.fork = AG.ForkSkip(prefix if kind == 'sub' else None, a.partial)
        a.spawn_ts = T0 - 3
        return a

    def tail(self, a):
        """What the read from `partial` on gives (the last lines)."""
        with open(self.path, 'rb') as f:
            f.seek(self.size - (1 << 14))
            for raw in f.read().split(b'\n')[1:]:
                r = cx_decode(raw)
                if r:
                    a.feed_cx(r)

    def test_the_place_the_read_begins_is_inside_the_hole(self):
        self.assertGreater(self.partial, self.front_end)
        self.assertLess(self.partial, self.size - (60 << 20))

    def test_the_front_is_lost_until_the_scan_is_done_and_then_it_is_not(self):
        a = self.agent()
        self.tail(a)
        self.assertEqual(a.lost, (T0 - 3, T0 + 500))
        self.assertEqual([e.path for e in a.write_events], ['/w/r1/T.md'])
        gen = a.facts_gen
        t = a.front_scan(wait=True)
        self.assertIsNotNone(t)
        self.assertEqual(a.front.state, 'done')
        self.assertIsNone(a.lost)                                                                           # the front was read: nothing is lost
        self.assertGreater(a.facts_gen, gen)
        self.assertEqual([(os.path.basename(e.path), e.run, e.ok) for e in a.write_events], [('A.md', None, True), ('B.md', None, True), ('z.py', None, True), ('T.md', 0, True)])
        self.assertEqual(sorted(w.call for w in a.windows), ['c2', 'c5', 'c5001', 'c6'])                    # the commands (the file change has no window, the long line that was no item nothing)
        self.assertEqual(a.front.stats['items'], 4)

    def test_it_does_not_run_twice_and_another_agent_of_the_file_does_not_read_it_again(self):
        a = self.agent()
        self.tail(a)
        self.assertIsNotNone(a.front_scan(wait=True))
        self.assertIsNone(a.front_scan(wait=True))                                                          # begun for this file already
        b = self.agent()
        self.tail(b)
        with mock.patch('board.codex_scan.scan', side_effect=AssertionError('read again')):
            self.assertIsNone(b.front_scan(wait=True))
        self.assertEqual((b.front.state, b.lost), ('done', None))
        self.assertEqual([e.path for e in b.write_events], [e.path for e in a.write_events])

    def test_what_a_scan_in_the_background_does_before_it_ends(self):
        """Before the end the front is lost and `facts_gen` has not moved; the events come in one step."""
        from board import codex_scan
        gate, started = threading.Event(), threading.Event()
        real = codex_scan.scan

        def slow(*args, **kw):
            started.set()
            gate.wait(10)
            return real(*args, **kw)
        a = self.agent()
        self.tail(a)
        with mock.patch.object(codex_scan, 'scan', slow):
            t = a.front_scan()
            self.assertTrue(started.wait(10))
            gen, events = a.facts_gen, list(a.write_events)
            self.assertEqual((a.front.state, a.lost), ('running', (T0 - 3, T0 + 500)))
            self.assertEqual((a.facts_gen, list(a.write_events)), (gen, events))
            self.assertIsNone(a.front_scan())                                                               # not begun twice while it runs
            gate.set()
            t.join(10)
        self.assertEqual((a.front.state, a.lost), ('done', None))
        self.assertGreater(a.facts_gen, gen)
        self.assertEqual(len(a.write_events), 4)

    def test_a_scan_that_fails_leaves_the_front_lost(self):
        a = self.agent()
        self.tail(a)
        with mock.patch('board.codex_scan.scan', side_effect=OSError('gone')), contextlib.redirect_stdout(io.StringIO()):
            a.front_scan(wait=True)
        self.assertEqual((a.front.state, a.lost), ('failed', (T0 - 3, T0 + 500)))
        self.assertEqual(len(a.write_events), 1)

    def test_a_file_that_was_written_over_hands_nothing_over(self):
        from board import codex_scan
        real = codex_scan.scan

        def over(*args, **kw):
            got = real(*args, **kw)
            os.rename(self.path, self.path + '.old')                                                        # another file under the same name
            with open(self.path, 'wb') as f:
                f.write(b'x' * (self.partial + 10))
            return got
        a = self.agent()
        self.tail(a)
        with mock.patch.object(codex_scan, 'scan', over), contextlib.redirect_stdout(io.StringIO()):
            a.front_scan(wait=True)
        self.assertEqual((a.front.state, a.lost), ('failed', (T0 - 3, T0 + 500)))
        self.assertEqual(len(a.write_events), 1)

    def rewrite_front(self):
        """The same file (the same inode and size), its front written again with another file name in the first item."""
        with open(self.path, 'r+b') as f:
            data = f.read(self.front_end)
            f.seek(0)
            f.write(data.replace(b'r1/A.md', b'r1/Z.md'))

    def test_the_result_of_a_file_that_was_written_over_in_place_is_not_used_again(self):
        """O9 cache: what is remembered of a front is good for the file it was made from; the file's identity is not enough (the same inode, the same size)."""
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        self.assertIn('A.md', [os.path.basename(e.path) for e in a.write_events])
        self.rewrite_front()
        b = self.agent()
        self.tail(b)
        b.front_scan(wait=True)
        self.assertEqual(b.front.state, 'done')
        self.assertEqual(sorted(os.path.basename(e.path) for e in b.write_events if e.run is None), ['B.md', 'Z.md', 'z.py'])

    def test_a_file_written_over_in_place_while_it_is_scanned_hands_nothing_over(self):
        from board import codex_scan
        real = codex_scan.scan

        def over(*args, **kw):
            got = real(*args, **kw)
            self.rewrite_front()
            return got
        a = self.agent()
        self.tail(a)
        with mock.patch.object(codex_scan, 'scan', over), contextlib.redirect_stdout(io.StringIO()):
            a.front_scan(wait=True)
        self.assertEqual((a.front.state, a.lost), ('failed', (T0 - 3, T0 + 500)))
        self.assertEqual(len(a.write_events), 1)
        b = self.agent()                                                                                   # (and nothing was remembered of it)
        self.tail(b)
        b.front_scan(wait=True)
        self.assertEqual(sorted(os.path.basename(e.path) for e in b.write_events if e.run is None), ['B.md', 'Z.md', 'z.py'])

    def test_the_events_of_a_remembered_front_belong_to_the_agent_that_has_them(self):
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        b = self.agent(tid='Y')
        self.tail(b)
        with mock.patch('board.codex_scan.scan', side_effect=AssertionError('read again')):
            b.front_scan(wait=True)
        got = [e for e in b.write_events if e.run is None]
        self.assertEqual(len(got), 3)
        self.assertEqual({e.agent for e in got}, {'Y'})
        self.assertEqual({w.agent for w in b.windows if w.call != 'c5001'}, {'Y'})
        self.assertEqual({e.agent for e in a.write_events}, {'X'})                                         # (the first owner's stay its own)

    def test_an_item_that_cannot_be_read_is_a_hole_in_its_time(self):
        """O9 / P3: a line that is the item of a write by its head and is not JSON (or has no time) is not dropped without a word: its time is `lost` (the judgment holds a first author back)."""
        with open(self.path, 'r+b') as f:
            data = f.read(self.front_end)
            f.seek(0)
            f.write(data.replace(b'/w/r1/B.md', b'/w/r1/B.md\x00'))                                       # a control character inside a string: not JSON (same length is not needed: the item is last in its line)
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        self.assertEqual(a.front.state, 'done')
        self.assertEqual(a.front.stats['bad'], 1)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events if e.run is None), ['A.md', 'z.py'])
        self.assertEqual(a.lost, (T0 + 10, T0 + 25))                                                        # between the line before it (the item at +10) and the line after it (the output at +25)

    def stamp_out(self, ts):
        """The item with the time `ts` loses its time (same length: the file keeps its places)."""
        with open(self.path, 'r+b') as f:
            data = f.read(self.front_end)
            f.seek(0)
            stamp = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(ts)).encode()
            f.write(data.replace(b'"timestamp":"' + stamp + b'"', b'"timestamp":"' + b'?' * len(stamp) + b'"'))

    def test_an_item_with_no_time_is_a_hole_between_the_lines_around_it(self):
        """No `(0, inf)`: the time of an item that cannot be read and has none is between the line before it and the line after it."""
        self.stamp_out(T0 + 30)
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events if e.run is None), ['A.md', 'B.md'])
        self.assertEqual(a.lost, (T0 + 25, T0 + 40))

    def test_an_item_with_no_time_at_the_end_of_the_front_is_a_hole_from_the_item_before_and_at_the_start_from_the_item_after(self):
        self.stamp_out(T0 + 40)
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        self.assertEqual(a.lost, (T0 + 30, T0 + 30))
        self.setUp()
        self.stamp_out(T0 + 10)
        b = self.agent()
        self.tail(b)
        b.front_scan(wait=True)
        self.assertEqual(b.lost, (T0 + 1, T0 + 20))

    def rewrite_middle(self):
        """A place that is in neither of the two samples (the head and the end of the front): the item of `z.py` is written over by one of the same length (the time of the file changes with it)."""
        with open(self.path, 'r+b') as f:
            data = f.read(self.front_end)
            self.assertGreater(data.index(b'src/z.py'), 64 << 10)
            f.seek(0)
            f.write(data.replace(b'src/z.py', b'src/y.py'))

    def test_a_file_written_over_between_the_samples_is_scanned_again(self):
        """Review 2 (N5, CONTRACT O18): the sampled bytes were the same, so the old events were handed over and `lost` was let go. The same size with another time is a file that was written over."""
        from board import codex_scan
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        self.assertIn('z.py', [os.path.basename(e.path) for e in a.write_events])
        before = codex_scan.file_version(self.path, self.partial)
        self.rewrite_middle()
        self.assertEqual(codex_scan.file_version(self.path, self.partial), before)                      # (the samples cannot tell)
        b = self.agent(tid='Y')
        self.tail(b)
        b.front_scan(wait=True)
        self.assertEqual((b.front.state, b.lost), ('done', None))
        self.assertEqual(sorted(os.path.basename(e.path) for e in b.write_events if e.run is None), ['A.md', 'B.md', 'y.py'])

    def test_a_file_written_over_between_the_samples_while_it_is_scanned_hands_nothing_over(self):
        from board import codex_scan
        real = codex_scan.scan

        def over(*args, **kw):
            got = real(*args, **kw)
            self.rewrite_middle()
            return got
        a = self.agent()
        self.tail(a)
        with mock.patch.object(codex_scan, 'scan', over), contextlib.redirect_stdout(io.StringIO()):
            a.front_scan(wait=True)
        self.assertEqual((a.front.state, a.lost), ('failed', (T0 - 3, T0 + 500)))
        self.assertEqual(len(a.write_events), 1)

    def test_a_file_that_only_grew_is_not_scanned_again_and_one_that_was_touched_or_made_shorter_is(self):
        from board import codex_scan
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        with open(self.path, 'ab') as f:                                                                   # lines added to the end
            f.write(b'\n' + line(T0 + 900, 'event_msg', {'type': 'task_started'}, 5100) + b'\n')
        b = self.agent(tid='Y')
        self.tail(b)
        with mock.patch('board.codex_scan.scan', side_effect=AssertionError('read again')):
            self.assertIsNone(b.front_scan(wait=True))
        self.assertEqual((b.front.state, b.lost), ('done', None))
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))                         # the same size and another time: written over
        c = self.agent(tid='Z')
        self.tail(c)
        with mock.patch('board.codex_scan.scan', wraps=codex_scan.scan) as scan:
            c.front_scan(wait=True)
        self.assertEqual((scan.call_count, c.front.state), (1, 'done'))                                   # (it was read again)

    def test_a_process_that_begins_again_has_nothing_remembered(self):
        from board import codex_scan
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        codex_scan._DONE.clear()                                                                           # (the memory of the process: another process has none)
        b = self.agent(tid='Y')
        self.tail(b)
        self.assertEqual((b.front.state, b.lost), (None, (T0 - 3, T0 + 500)))                              # until it has read the front it is lost
        with mock.patch('board.codex_scan.scan', wraps=codex_scan.scan) as scan:
            b.front_scan(wait=True)
        self.assertEqual((scan.call_count, b.front.state, b.lost), (1, 'done', None))

    def test_a_scan_that_was_done_before_the_first_line_came_leaves_no_front_lost(self):
        """Review 2 (Opus P3): the front was marked lost when the first line of the tail was read, and a scan that had already ended did not clear it again."""
        a = self.agent()
        a.front_scan(wait=True)
        self.assertEqual(a.front.state, 'done')
        self.tail(a)
        self.assertIsNone(a.lost)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events if e.run is None), ['A.md', 'B.md', 'z.py'])

    def test_a_read_again_from_the_start_scans_again(self):
        a = self.agent()
        self.tail(a)
        a.front_scan(wait=True)
        a.reset_runs()
        self.assertEqual((a.front.state, len(a.write_events), a.lost), (None, 0, None))
        self.tail(a)
        with mock.patch('board.codex_scan.scan', side_effect=AssertionError('the result of the file is still remembered')):
            a.front_scan(wait=True)
        self.assertEqual((a.front.state, a.lost), ('done', None))

    def test_an_item_too_long_even_for_the_scan_stays_a_hole(self):
        with mock.patch.object(self.codex_parse, 'CX_ITEM_MAX', 100):                                    # (the stand-in of 64 MB: every item of the front is longer than that)
            a = self.agent()
            self.tail(a)
            a.front_scan(wait=True)
        self.assertEqual(a.front.state, 'done')
        self.assertEqual(a.lost, (T0 + 10, T0 + 40))                                                         # a point for each of the four, one span for all of them
        self.assertEqual(len(a.write_events), 1)

    def test_a_sub_agent_does_not_take_the_history_copied_from_its_parent(self):
        a = self.agent(kind='sub', prefix=3)                  # lines 0 to 3 are the parent's: the session meta, the turn start, the first two items
        self.tail(a)
        a.front_scan(wait=True)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events if e.run is None), ['z.py'])

    def test_a_file_that_is_no_longer_there_is_a_failure_and_a_small_file_has_no_front(self):
        a = self.agent()
        a.path = os.path.join(self.tmp.name, 'missing.jsonl')
        self.assertIsNone(a.front_scan(wait=True))
        self.assertIsNone(a.front.state)
        a = self.agent()
        a.partial = 0
        self.assertIsNone(a.front_scan(wait=True))

    def test_the_orchestrator_thread_of_the_page_too(self):
        e = {'id': 'X', 'path': self.path, 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root', 'prefix_ord': 0, 'first_user': None}
        s = server.CodexSession(e)
        s.partial = self.partial
        s.first_ts = T0 - 3
        with open(self.path, 'rb') as f:
            data = f.read()[-(1 << 14):]
        for raw in data.split(b'\n')[1:]:
            r = cx_decode(raw)
            if r:
                s._feed_cx(r)
        self.assertEqual(s.orch_lost, (T0 - 3, T0 + 500))
        s.front_scan(wait=True)
        self.assertIsNone(s.orch_lost)
        self.assertEqual(sorted(os.path.basename(x.path) for x in s.orch_events), ['A.md', 'B.md', 'T.md', 'z.py'])
        self.assertEqual({x.agent for x in s.orch_events}, {'orch'})

    def test_the_page_starts_it_only_when_the_server_has_turned_the_background_on_and_the_first_picture_is_out(self):
        from board import sessions
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, '/nonexistent/x.jsonl')
        a = self.agent()
        s.agents[a.id] = a
        calls = []
        a.front_scan = lambda wait=False: calls.append(1)
        with mock.patch.object(sessions, 'WALK_ENABLED', False), mock.patch.object(sessions.LATER, 'is_open', lambda: True):
            s._front_later()
        self.assertEqual(calls, [])                                                                          # (off: tests and tools read only what they ask for)
        with mock.patch.object(sessions, 'WALK_ENABLED', True), mock.patch.object(sessions.LATER, 'is_open', lambda: False):
            s._front_later()
        self.assertEqual(calls, [])
        with mock.patch.object(sessions, 'WALK_ENABLED', True), mock.patch.object(sessions.LATER, 'is_open', lambda: True):
            s._front_later()
            a.partial = 0
            s._front_later()                                                                                 # nothing to read for a rollout that was read from its start
        self.assertEqual(calls, [1])


class BrokenLines(unittest.TestCase):
    """CONTRACT O17: a write line that cannot be read (it is no JSON, and its head says it is a command or a file change) is a hole in the time of the record, between the line before it and the line
    after it, unless it is the last line of a process that died writing it: the line after it is a process that begins (a `session_meta`, or a line whose ordinal does not go on from its own) or there
    is none. Read from the end (`cx_rows`) and in the scan of the front of a big rollout (`codex_scan.scan`) alike; no `(0, inf)`."""

    @staticmethod
    def item(ts, n, path='r1/A.md', size=10):
        it = dict(BigLines.command('cat > %s <<EOF\nx\nEOF' % path, size), id='c%d' % n)
        return line(ts, 'event_msg', {'type': 'item_completed', 'thread_id': 'X', 'item': it}, n)

    def broken(self, ts=T0 + 7, n=3):
        """A command item cut short at 1,026 bytes, as a writer that died leaves it (the real line was such a one)."""
        cut = self.item(ts, n, 'r1/C.md', 2000)[:1026]
        self.assertGreater(len(self.item(ts, n, 'r1/C.md', 2000)), 1026)
        return cut

    def head(self):
        return [line(T0, 'session_meta', {'id': 'X', 'cwd': '/w'}, 0), line(T0 + 1, 'event_msg', {'type': 'task_started'}, 1), self.item(T0 + 5, 2)]

    def after(self, kind):
        """The lines after the broken one (its ordinal is 3)."""
        if kind == 'restart':               # a process that took the file up again counts from the lines it could read: the ordinal of the broken line is used again
            return [line(T0 + 3600, 'event_msg', {'type': 'thread_settings_applied'}, 3), line(T0 + 3601, 'event_msg', {'type': 'task_started'}, 4), self.item(T0 + 3605, 5, 'r1/B.md')]
        if kind == 'meta':
            return [line(T0 + 3600, 'session_meta', {'id': 'X', 'cwd': '/w'}, 4), self.item(T0 + 3605, 5, 'r1/B.md')]
        if kind == 'goes_on':               # the same process: the next ordinal
            return [line(T0 + 8, 'event_msg', {'type': 'token_count'}, 4), self.item(T0 + 9, 5, 'r1/B.md')]
        return []

    def agent(self):
        return AG.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})

    def read(self, lines, a=None, fork=None):
        a = a or self.agent()
        for r in AG.cx_rows(lines, a.fork if fork is None else fork):
            a.feed_cx(r)
        return a

    def scan(self, lines, limit=None):
        from board import codex_scan
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, 'r.jsonl')
        with open(path, 'wb') as f:
            f.write(b'\n'.join(lines) + b'\n')
        log, stats = codex_scan.scan(path, limit if limit is not None else os.path.getsize(path), 'X', '/w', 2000)
        return log

    def both(self, lines, want, limit=None):
        a = self.read(lines)
        self.assertEqual(a.lost, want, 'read from the end')
        self.assertEqual(self.scan(lines, limit).lost, want, 'the scan of the front')

    def test_the_last_line_of_a_process_that_died_leaves_no_hole(self):
        """The real shape of the frozen HOME (its line 787): a command item cut short, then a process that counts from the ordinal of the broken line again."""
        lines = self.head() + [self.broken()] + self.after('restart')
        a = self.read(lines)
        self.assertIsNone(a.lost)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events), ['A.md', 'B.md'])
        self.both(lines, None)
        from board import debates
        self.assertIsNone(debates.facts_of(a).lost)                    # what the judgment reads: nothing is held for it

    def test_a_process_that_begins_with_its_meta_line_ends_the_one_before(self):
        self.both(self.head() + [self.broken()] + self.after('meta'), None)

    def test_a_broken_last_line_of_the_file_leaves_no_hole(self):
        self.both(self.head() + [self.broken()], None)

    def test_a_broken_write_line_in_the_middle_of_a_process_is_a_hole_between_the_lines_around_it(self):
        self.both(self.head() + [self.broken()] + self.after('goes_on'), (T0 + 5, T0 + 8))

    def test_a_broken_line_that_has_no_time_is_a_hole_all_the_same_and_never_from_zero(self):
        bad = self.broken()
        stamp = bad[bad.index(b'"timestamp":"') + 13:bad.index(b'"ordinal"') - 3]
        bad = bad.replace(stamp, b'?' * len(stamp), 1)
        self.both(self.head() + [bad] + self.after('goes_on'), (T0 + 5, T0 + 8))

    def test_two_broken_lines_in_a_row(self):
        lines = self.head() + [self.broken(), self.broken(T0 + 8, 4)] + [self.item(T0 + 9, 5, 'r1/B.md')]
        self.both(lines, (T0 + 5, T0 + 9))

    def test_the_next_physical_line_settles_a_broken_write_even_when_it_is_broken_itself(self):
        """Review 2 (N4): the line that follows is looked at, readable or not. The scan did, the read from the end skipped it and judged by the one after: a later author was the owner in one and held in the other."""
        junk = line(T0 + 8, 'event_msg', {'type': 'token_count', 'info': {'x': 'y' * 2000}}, 4)[:600]
        self.both(self.head() + [self.broken(), junk] + self.after('meta'), (T0 + 5, T0 + 8))
        junk_restart = line(T0 + 3600, 'event_msg', {'type': 'thread_settings_applied', 'x': 'y' * 2000}, 3)[:600]       # a restart whose first line is itself cut short
        self.both(self.head() + [self.broken(), junk_restart] + self.after('restart')[1:], None)
        self.both(self.head() + [self.broken(), b'', b''] + self.after('meta'), (T0 + 5, T0 + 7))                  # (a blank line is a line: it says nothing of a process that began)

    def test_the_same_lines_give_the_same_hole_in_both_paths(self):
        for first in (self.broken(),):
            for kind in ('restart', 'meta', 'goes_on', 'none'):
                with self.subTest(kind):
                    lines = self.head() + [first] + self.after(kind)
                    self.assertEqual(self.read(lines).lost, self.scan(lines).lost)

    def test_a_line_cut_after_a_megabyte_is_judged_like_any_other(self):
        """Review 2 (Opus P3): a write line longer than the limit of the reader that was cut short was a point of `lost` whatever followed it; it follows the same rule now."""
        item = self.item(T0 + 7, 3, 'r1/C.md', 3 << 20)
        cut = item[:(3 << 19)]
        self.assertGreater(len(cut), 1 << 20)
        for kind, want in (('restart', None), ('meta', None), ('none', None), ('goes_on', (T0 + 5, T0 + 8))):
            lines = self.head() + [cut] + self.after(kind)
            self.both(lines, want)
        whole = self.head() + [item]                                     # (a long line that is not cut is read)
        a = self.read(whole)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events), ['A.md', 'C.md'])
        self.assertIsNone(a.lost)

    def test_a_line_cut_before_the_kind_of_its_item_could_be_read_may_be_a_write(self):
        full = self.item(T0 + 7, 3, 'r1/C.md', 2000)
        for at in (full.index(b'"item"') + 8, full.index(b'"item"') - 5, full.index(b'"thread_id"') + 4):
            bad = full[:at]
            for kind, want in (('restart', None), ('goes_on', (T0 + 5, T0 + 8)), ('none', None)):
                with self.subTest(at=at, kind=kind):
                    self.both(self.head() + [bad] + self.after(kind), want)
        other = line(T0 + 7, 'event_msg', {'type': 'item_completed', 'thread_id': 'X', 'item': {'type': 'Reasoning', 'id': 'r', 'text': 'z' * 2000}}, 3)[:700]
        self.both(self.head() + [other] + self.after('goes_on'), None)                                  # an item of a kind that is no write: no hole

    def test_a_write_that_is_json_but_has_no_time_is_a_hole_in_both_paths(self):
        """A write with no readable time cannot be put in the record: it is as unread as one that is cut short, in the read from the end as in the scan."""
        full = self.item(T0 + 7, 3, 'r1/C.md')
        stamp = full[full.index(b'"timestamp":"') + 13:full.index(b'"ordinal"') - 3]
        bad = full.replace(stamp, b'?' * len(stamp), 1)
        json.loads(bad)                                                  # (it is JSON)
        self.both(self.head() + [bad] + self.after('goes_on'), (T0 + 5, T0 + 8))
        self.both(self.head() + [bad] + self.after('restart'), None)

    def test_a_line_that_is_json_and_no_write_is_none_in_both_paths_even_when_its_head_looks_cut(self):
        odd = json.dumps({'timestamp': '2026-01-01T00:00:07.000Z', 'ordinal': 3, 'type': 'event_msg', 'payload': {'x': 1, 'type': 'item_completed'}}, separators=(',', ':')).encode()
        self.both(self.head() + [odd] + self.after('goes_on'), None)

    def test_a_line_that_is_no_write_and_is_no_json_is_no_hole(self):
        junk = line(T0 + 6, 'event_msg', {'type': 'token_count', 'info': {'x': 'y' * 2000}}, 3)[:600]
        self.both(self.head() + [junk] + self.after('goes_on'), None)
        self.both(self.head() + [b'{"timestamp": not json'] + self.after('goes_on'), None)

    def test_what_waits_for_the_next_line_waits_over_a_read(self):
        a = self.read(self.head() + [self.broken()])                  # (the read ended with it: nothing says yet)
        self.assertIsNone(a.lost)
        self.read(self.after('goes_on'), a)
        self.assertEqual(a.lost, (T0 + 5, T0 + 8))
        b = self.read(self.head() + [self.broken()])
        self.read(self.after('restart'), b)
        self.assertIsNone(b.lost)

    def test_a_read_again_from_the_start_forgets_what_waited(self):
        a = self.read(self.head() + [self.broken()])
        a.fork.restart()
        self.read(self.after('goes_on'), a)
        self.assertIsNone(a.lost)                                       # (those lines are the first of a new read: nothing broken before them)

    def test_the_history_copied_from_a_parent_has_no_hole_of_the_sub_agents(self):
        lines = self.head() + [self.broken()] + self.after('goes_on')
        a = self.agent()
        a.fork = AG.ForkSkip(3)                                          # the lines up to the ordinal 3 are the parent's
        self.read(lines, a)
        self.assertIsNone(a.lost)
        b = self.agent()
        b.fork = AG.ForkSkip(2)                                          # the broken line is the sub-agent's own
        self.read(lines, b)
        self.assertEqual(b.lost, (T0 + 7, T0 + 8))                        # (the line before it is the parent's: from its own time)

    def test_the_scan_looks_at_the_line_after_the_last_one_it_reads(self):
        """The front ends in the line that holds the place the later read begins at: the broken line may be that one, and the line after it is not in the scan."""
        for kind, want in (('goes_on', (T0 + 5, T0 + 8)), ('restart', None), ('meta', None)):
            lines = self.head() + [self.broken()] + self.after(kind)
            limit = len(b'\n'.join(self.head())) + 1 + 100                 # a place inside the broken line
            self.assertEqual(self.scan(lines, limit).lost, want, kind)


class HeredocFilter(unittest.TestCase):
    """Review 2 (Opus P2-1): taking the bodies of heredocs out of a text before it is read is only a way to make it cheap: it must change nothing of what is read. A `<<EOF` in a comment, in a
    quoted text or in a prompt that teaches the shell used to take out the lines up to the next `EOF`, and with them a write of the command (the link reads the whole text: the two read it differently)."""

    CASES = {
        'comment': "# write my cell (cat <<EOF)\ncat > r1/A.md <<EOF\nx\nEOF\nclaude -p 'review' > r1/B.md",
        'comment_plain_write': "# see cat <<EOF usage\nprintf x > r1/A.md\ncat > n.txt <<EOF\ny\nEOF",
        'prompt_mentions': "P=\"$(cat <<'PROMPT'\nUse cat <<'EOF' to write.\nPROMPT\n)\"\nprintf x > r1/B.md\ncat > r1/C.md <<'EOF'\nz\nEOF\nclaude -p \"$P\" > r1/D.md",
        'quoted_arg': "echo 'use <<EOF here'\nprintf x > r1/A.md\ncat > n.txt <<EOF\ny\nEOF",
        'quote_open_above': "claude -p 'review\nthis <<EOF\nmore' > r1/B.md\nprintf x > r1/A.md\ncat > n.txt <<EOF\ny\nEOF",
        'no_later_eof': "# cat <<EOF\nprintf x > r1/A.md",
        'control': "cat > r1/A.md <<EOF\nx\nEOF\nprintf y > r1/B.md",
        'two_on_a_line': "cat <<A <<B > r1/A.md\na\nA\nb\nB\nprintf y > r1/B.md",
        'dash_and_tab': "cat > r1/A.md <<-EOF\n\tx\n\tEOF\nprintf y > r1/B.md",
    }

    def read(self, text):
        AG._PARSED.clear()
        AG._LAST_OUTSIDE[0] = None
        pieces, setters, others = AG.launch_pieces(text)
        return (AG.shell_writes(text, '/w', True, places=True, md_only=False, skip_launch=True), AG.shell_writes(text, '/w', True), AG.shell_reads(text, '/w', md_only=False),
                AG.shell_mkdirs(text, '/w'), [{k: v for k, v in p.items() if k != 'at'} for p in pieces], setters, [{k: v for k, v in o.items() if k != 'n'} for o in others])

    def unfiltered(self, text):
        with mock.patch.object(AG, 'outside_heredocs', lambda t: t):
            return self.read(text)

    def test_a_text_is_read_the_same_with_and_without_the_filter(self):
        for name, text in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(self.read(text), self.unfiltered(text))

    def test_a_write_the_filter_used_to_take_out(self):
        got = [(e.path.replace('/w/', ''), e.proof) for e in AG.shell_events('L', 100.0, 1, 'c1', self.CASES['comment'], '/w', ok=True)]
        self.assertEqual(got, [('r1/A.md', 'window')])
        got = [(e.path.replace('/w/', ''), e.proof) for e in AG.shell_events('L', 100.0, 1, 'c1', self.CASES['comment_plain_write'], '/w', ok=True)]
        self.assertEqual(got, [('r1/A.md', 'window'), ('n.txt', 'exit')])

    def test_a_real_heredoc_is_still_taken_out(self):
        """(The filter is for the cost of a text with a long body.)"""
        text = 'cat > r1/A.md <<EOF\n' + 'a line > r1/Z.md\n' * 500 + 'EOF\nprintf y > r1/B.md'
        got = AG.outside_heredocs(text)
        self.assertEqual(got, 'cat > r1/A.md <<EOF\nEOF\nprintf y > r1/B.md')
        self.assertEqual(AG.outside_heredocs('# only a comment <<EOF\ncat > f <<EOF\nx\nEOF'), '# only a comment <<EOF\ncat > f <<EOF\nEOF')

    def test_random_texts(self):
        import random
        rnd = random.Random(7)
        pool = ['# note (cat <<EOF)', 'cat > r1/A.md <<EOF', 'a line > r1/Z.md', 'EOF', 'printf x > r1/B.md', "claude -p 'review' > r1/C.md", "echo 'use <<EOF here'", 'P="$(cat <<\'PROMPT\'', 'PROMPT',
                ')"', 'cat <<<x', 'echo $((1<<n))', "tee r1/D.md <<'EOF'", 'cat r1/E.md', "# it's a comment", 'x="a', 'b"', 'cat > r1/F.md <<-EOF', '\tEOF', 'cd d && cat > r1/G.md <<EOF',
                'bash -c "cat <<EOF > r1/H.md"', "echo don't >> r1/I.md", 'wait', 'cat > r1/J.md <<EOF\nbody > r1/Y.md\nEOF', "cat > r1/K.md <<'END'\n# not > a comment\nEND", 'tee r1/L.md <<EOF\nx\nEOF']
        changed = 0
        for _ in range(400):
            text = '\n'.join(rnd.choice(pool) for _ in range(rnd.randint(2, 9)))
            changed += AG.outside_heredocs(text) != text
            self.assertEqual(self.read(text), self.unfiltered(text), text)
        self.assertGreater(changed, 60)                                          # (the filter did something in the texts it was tried on)


class FrontLines(unittest.TestCase):
    """`codex_scan.candidates`: the lines that write, whatever the size of the blocks, and nothing else is kept."""

    def lines(self, raws, limit=10 ** 12, prefix=None, block=None):
        from board import codex_scan
        stats = {}
        path = os.path.join(tempfile.mkdtemp(), 'r.jsonl')
        self.addCleanup(lambda: __import__('shutil').rmtree(os.path.dirname(path), ignore_errors=True))
        with open(path, 'wb') as f:
            f.write(b'\n'.join(raws) + b'\n')
        with mock.patch.object(codex_scan, 'BLOCK', block or codex_scan.BLOCK), open(path, 'rb') as f:
            return list(codex_scan.candidates(f, limit, prefix, stats)), stats

    def raws(self):
        item = lambda ts, i, kind='CommandExecution': BigLines.line(T0 + ts, dict(BigLines.command('cat > r1/A.md <<EOF\nx\nEOF', 30), id=i) if kind == 'CommandExecution' else {'type': kind, 'id': i, 'changes': {}})
        return [line(T0, 'session_meta', {'id': 'X'}, 0), item(1, 'a'), line(T0 + 2, 'event_msg', {'type': 'token_count'}, 2), item(3, 'b', 'FileChange'), b'', item(4, 'c', 'Reasoning'),
                line(T0 + 5, 'response_item', {'type': 'function_call_output', 'call_id': 'k', 'output': 'o' * 5000}, 5), item(6, 'd')]

    def test_every_block_size_gives_the_same_lines(self):
        want = None
        for block in (None, 4096, 1000, 257, 64, 7):
            got, stats = self.lines(self.raws(), block=block)
            ids = [json.loads(x)['payload']['item']['id'] for x in got]
            self.assertEqual(ids, ['a', 'b', 'd'], block)
            want = want or stats['lines']
            self.assertEqual(stats['lines'], want, block)

    def test_the_line_that_holds_the_limit_is_the_last_one(self):
        raws = self.raws()
        limit = len(raws[0]) + 1 + len(raws[1]) + 1 + 5                                                      # inside the third line
        got, stats = self.lines(raws, limit=limit, block=100)
        self.assertEqual([json.loads(x)['payload']['item']['id'] for x in got], ['a'])
        self.assertEqual(stats['lines'], 3)
        got, _ = self.lines(raws, limit=len(raws[0]) + 1 + len(raws[1]), block=100)                          # the newline that ends line 2 is at limit - 0: the line that holds it is the last
        self.assertEqual([json.loads(x)['payload']['item']['id'] for x in got], ['a'])

    def test_a_long_item_is_not_given_and_says_when_it_was(self):
        from board import codex_parse
        raws = self.raws() + [BigLines.line(T0 + 9, BigLines.command('ls', 5000))]
        with mock.patch.object(codex_parse, 'CX_ITEM_MAX', 3000):
            got, stats = self.lines(raws, block=500)
        self.assertEqual([json.loads(x)['payload']['item']['id'] for x in got], ['a', 'b', 'd'])
        self.assertEqual(stats['big'], [T0 + 9])

    def test_the_copied_history_of_a_sub_agent_is_left_out(self):
        got, _ = self.lines(self.raws(), prefix=1, block=300)                                                # lines 0 and 1 (the ordinals) are the parent's
        self.assertEqual([json.loads(x)['payload']['item']['id'] for x in got], ['b', 'd'])
        plain = [line(T0 + i, 'event_msg', {'type': 'item_completed', 'item': BigLines.command('cat > r1/A.md <<EOF\nx\nEOF', 5) | {'id': 'i%d' % i}} if False else
                 {'type': 'item_completed', 'item': dict(BigLines.command('cat > r1/A.md <<EOF\nx\nEOF', 5), id='i%d' % i)}) for i in range(5)]
        got, _ = self.lines(plain, prefix=2, block=300)                                                      # no ordinal: the place of the line in the file is the number
        self.assertEqual([json.loads(x)['payload']['item']['id'] for x in got], ['i3', 'i4'])

    def test_a_giant_line_that_is_no_item_is_skipped_as_it_streams_by(self):
        giant = line(T0 + 1, 'response_item', {'type': 'function_call_output', 'call_id': 'k', 'output': 'o' * (3 << 20)}, 1)
        got, stats = self.lines([self.raws()[1], giant, self.raws()[7]], block=1 << 20)
        self.assertEqual(len(got), 2)
        self.assertEqual(stats['lines'], 3)


class FrontMemory(unittest.TestCase):
    """The scan of a big file does not grow memory with the size of the file: a block, and the line when it is an item."""

    def test_a_file_of_three_hundred_megabytes_with_a_giant_line_in_the_middle(self):
        import tracemalloc
        from board import codex_scan
        head = line(T0, 'session_meta', {'id': 'X', 'cwd': '/w'}, 0)
        cmd = BigLines.line(T0 + 1, BigLines.command('cat > r1/A.md <<EOF\nx\nEOF', 10))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'big.jsonl')
            with open(path, 'wb') as f:
                f.write(head + b'\n' + cmd + b'\n')
                f.truncate(300 << 20)                                                                       # sparse: the middle is one giant line of zeros
                f.seek(0, 2)
                f.write(b'\n' + cmd + b'\n')
            size = os.path.getsize(path)
            tracemalloc.start()
            try:
                log, stats = codex_scan.scan(path, size - (64 << 20), 'X', '/w', 2000)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.assertEqual((stats['items'], len(log.events())), (1, 1))
        self.assertLess(peak, 40 << 20)                                                                     # (the block is 8 MB)


class PlannedCodex(unittest.TestCase):
    """The `-o` file of a Codex turn: a requirement from the call; when the turn has ended, one event with the sha1 of its last message and the window from the call to the end."""

    def agent(self, out='/w/talk/r1/B.md', status='done', msg='the report', src=None, ended=True):
        a = exec_agent('X', ended=False)
        t = a.turns[0]
        t.update(call='call1', bash_ts=T0 - 2 if src != 'argv' else None, out=out, out_state='planned')
        if src:
            t['out_src'] = src
        if ended:
            a.feed_cx(cx_decode(line(1020.0, 'event_msg', {'type': 'task_complete', 'last_agent_message': msg})))
            if status != 'done':
                a.turns[0]['status'] = status
        return a

    def test_a_turn_that_ended_well(self):
        a = self.agent()
        self.assertTrue(server.CodexLinker._plan(a))
        (p,) = a.planned
        self.assertEqual((p.agent, p.path, p.op, p.call, p.run, p.src, p.ts), ('X', '/w/talk/r1/B.md', '-o', 'call1', 0, 'command', T0 - 2))
        (ev,) = a.write_events
        sha = a.turns[0]['sha']
        self.assertEqual((ev.path, ev.kind, ev.evidence, ev.ok, ev.proof, ev.shas, ev.span, ev.ts, ev.call, ev.run),
                         ('/w/talk/r1/B.md', 'replace', 'planned', True, 'sha', (sha,), (T0 - 2, 1020.0), 1020.0, 'call1', 0))
        self.assertFalse(server.CodexLinker._plan(a))                                       # nothing changed: nothing made again

    def test_a_turn_that_failed_or_has_no_message(self):
        a = self.agent(status='failed')
        server.CodexLinker._plan(a)
        self.assertEqual([e.ok for e in a.write_events], [False])
        a = self.agent(msg='')
        server.CodexLinker._plan(a)
        (ev,) = a.write_events
        self.assertEqual((ev.proof, ev.shas), ('window', ()))                               # the message is not known: only the window

    def test_a_turn_that_has_not_ended_has_the_requirement_alone(self):
        a = self.agent(ended=False)
        server.CodexLinker._plan(a)
        self.assertEqual(len(a.planned), 1)
        self.assertEqual(a.write_events, [])

    def test_a_path_only_the_command_line_of_a_process_gave_is_no_event(self):
        a = self.agent(src='argv')
        server.CodexLinker._plan(a)
        (p,) = a.planned
        self.assertEqual((p.src, p.ts), ('argv', 1000.0))
        self.assertEqual(a.write_events, [])

    def test_a_plan_that_is_given_up_goes_with_its_event(self):
        a = self.agent()
        server.CodexLinker._plan(a)
        a.turns[0]['out'] = a.turns[0]['out_state'] = None                                  # `_revoke_argv`
        self.assertTrue(server.CodexLinker._plan(a))
        self.assertEqual((a.planned, a.write_events), ([], []))

    def test_the_path_that_changed_makes_the_event_again(self):
        a = self.agent()
        server.CodexLinker._plan(a)
        a.turns[0]['out'] = '/w/talk/r1/C.md'
        server.CodexLinker._plan(a)
        self.assertEqual([e.path for e in a.write_events], ['/w/talk/r1/C.md'])
        self.assertEqual([p.path for p in a.planned], ['/w/talk/r1/C.md'])


class PlannedNoFileRead(PlannedCodex):
    """The collector never opens the file an output names: the events are the same with no file, with another file and with a file that is gone."""

    def test_the_events_do_not_depend_on_the_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'r1', 'B.md')
            seen = []
            for state in ('absent', 'same', 'other'):
                if state != 'absent':
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, 'w') as f:
                        f.write('the report' if state == 'same' else 'something else')
                a = self.agent(out=out)
                real_open = open
                with mock.patch('builtins.open', side_effect=lambda p, *x, **k: (_ for _ in ()).throw(AssertionError('open %s' % p)) if str(p) == out else real_open(p, *x, **k)):
                    server.CodexLinker._plan(a)
                seen.append([dataclasses.astuple(e) for e in a.write_events])
            self.assertEqual(seen[0], seen[1])
            self.assertEqual(seen[0], seen[2])
            self.assertEqual(len(seen[0]), 1)

    def test_the_old_sha_check_still_works_for_the_page(self):
        """`out_state` and the old `writes` of the page are made as they were (the judgment does not read them)."""
        a = self.agent()
        lk = server.CodexLinker(FakeSession())
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'B.md')
            with open(out, 'w') as f:
                f.write('the report')
            a.turns[0]['out'] = out
            lk.agents['X'] = a
            lk._derive(a)
            self.assertEqual(a.turns[0]['out_state'], 'confirmed')
            self.assertEqual([w['path'] for w in a.writes], [out])
            self.assertEqual([(e.path, e.ok) for e in a.write_events], [(out, True)])
            self.assertEqual(len(a.write_events), 1)


class CandidateFiles(unittest.TestCase):
    """O13 (D8): a `-o` file that the command does not name (a path no scope of the call can work out) is not found by looking around the folders the instruction speaks of: the judgment is made
    of what the commands say, not of the words of an instruction. A file found that way (its sha1 is the sha1 of the last message) is shown on the page (`out_state`, `out_src` 'cand') and is no
    requirement, no event, no name."""

    def thread(self, tmp, user, files=('r1/B.md',), msg='the report'):
        talk = os.path.join(tmp, 'talk')
        for f in files:
            os.makedirs(os.path.dirname(os.path.join(talk, f)), exist_ok=True)
            with open(os.path.join(talk, f), 'w') as fh:
                fh.write(msg)
            os.utime(os.path.join(talk, f), (1020.5, 1020.5))                                 # (written when the turn ended)
        a = exec_agent('X', user=user % {'talk': talk}, ended=False)
        a.feed_cx(cx_decode(line(1020.0, 'event_msg', {'type': 'task_complete', 'last_agent_message': msg})))
        lk = server.CodexLinker(FakeSession())
        lk.agents['X'] = a
        return a, lk, talk

    def test_a_file_found_around_the_instruction_is_shown_and_no_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, lk, talk = self.thread(tmp, 'Read %(talk)s/brief.md and write your report')
            lk._derive(a)
            t = a.turns[0]
            self.assertEqual((t['out'], t['out_state'], t.get('out_src')), (os.path.join(talk, 'r1', 'B.md'), 'confirmed', 'cand'))
            self.assertEqual(([p.path for p in a.planned], a.write_events), ([], []))
            self.assertEqual((a.out_paths, a.report_tag), ([], ''))
            self.assertFalse(lk._plan(a))                                                      # (and it stays so)
            self.assertEqual(a.planned, [])

    def test_the_words_of_the_instruction_change_nothing_of_what_the_judgment_reads(self):
        seen = []
        for user in ('Read %(talk)s/brief.md and write your report', 'Look at the brief at %(talk)s/brief.md', 'no path here at all'):
            with tempfile.TemporaryDirectory() as tmp:
                a, lk, talk = self.thread(tmp, user)
                lk._derive(a)
                seen.append(([dataclasses.astuple(p) for p in a.planned], a.write_events, a.out_paths, a.report_tag))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0], seen[2])
        self.assertEqual(seen[0], ([], [], [], ''))

    def test_the_path_the_command_names_is_a_plan_whatever_was_found_around(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, lk, talk = self.thread(tmp, 'Read %(talk)s/brief.md and write your report')
            t = a.turns[0]
            t.update(call='call1', bash_ts=1000.0, out=os.path.join(talk, 'r1', 'B.md'), out_state='planned')
            lk._derive(a)
            self.assertEqual((t['out_state'], t.get('out_src')), ('confirmed', None))
            self.assertEqual([p.path for p in a.planned], [os.path.join(talk, 'r1', 'B.md')])
            self.assertEqual(len(a.write_events), 1)

    def test_a_command_that_comes_late_takes_the_place_of_what_was_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, lk, talk = self.thread(tmp, 'Read %(talk)s/brief.md and write your report')
            lk._derive(a)
            t = a.turns[0]
            self.assertEqual(t.get('out_src'), 'cand')
            t.update(call='call1', bash_ts=1000.0, out=os.path.join(talk, 'r1', 'C.md'), out_src=None, out_state='planned')       # what `_derive` does when the call of the turn is found
            lk._plan(a)
            self.assertEqual([p.path for p in a.planned], [os.path.join(talk, 'r1', 'C.md')])


class CliSession(unittest.TestCase):
    """`claude -p ... > file`: the requirement from the launching command and, when the child has ended, the event of the redirect."""
    SID = 'bbbbbbbb-0000-4000-8000-0000000000b1'
    CHILD = 'cccccccc-0000-4000-8000-0000000000c1'

    def setUp(self):
        from board.facts import Redirect
        self.Redirect = Redirect
        self.s = server.Session.__new__(server.Session)
        server.Session.__init__(self.s, '/nonexistent/%s.jsonl' % self.SID)
        self.s.cwd = '/w'
        a = server.Agent(self.CHILD, {'description': 'child'})
        a.origin = 'cli'
        a.cli = {'sid': self.SID, 'node': None, 'rule': 'out', 'call': 'tc1', 'bash_ts': T0 + 1, 'calls': ['tc1'], 'calls_certain': [True]}
        a.last_ts = T0 + 100
        self.a = a
        self.s.agents[self.CHILD] = a

    def with_end(self, text='the final answer', ok=True, op='>', status='done'):
        a = self.a
        a.redirects = [self.Redirect(fd=1, op=op, path_raw='r1/C.md', path_resolved='/w/talk/r1/C.md')]
        if text is not None:
            a.talk.append({'ts': T0 + 99, 'kind': 'end', 'text': text})
        self.s.orch_log.add_window(AG.CmdWindow('orch', 'tc1', T0 + 1, T0 + 101 if ok is not None else None, ok))
        self.s.refresh_facts({self.CHILD: status})

    def test_the_requirement_and_the_event_of_a_child_that_ended(self):
        self.with_end()
        (p,) = self.a.planned
        self.assertEqual((p.path, p.op, p.call, p.run, p.src, p.ts), ('/w/talk/r1/C.md', '>', 'tc1', 1, 'command', T0 + 1))
        (ev,) = self.a.write_events
        import hashlib
        self.assertEqual((ev.path, ev.kind, ev.evidence, ev.ok, ev.proof, ev.span, ev.ts, ev.call, ev.run), ('/w/talk/r1/C.md', 'replace', 'planned', True, 'content', (T0 + 1, T0 + 100), T0 + 100, 'tc1', 1))
        self.assertEqual(ev.shas, (hashlib.sha1(b'the final answer').hexdigest(), hashlib.sha1(b'the final answer\n').hexdigest()))

    def test_without_the_text_it_is_a_window(self):
        self.with_end(text=None)
        (ev,) = self.a.write_events
        self.assertEqual((ev.proof, ev.shas), ('window', ()))

    def test_an_append_is_no_authoring_event(self):
        self.with_end(op='>>')
        (ev,) = self.a.write_events
        self.assertEqual((ev.kind, ev.proof, ev.shas), ('append', 'window', ()))

    def test_the_launching_call_that_failed_or_is_not_known_to_have_worked(self):
        self.with_end(ok=False)
        self.assertEqual([e.ok for e in self.a.write_events], [False])
        self.setUp()
        self.with_end(ok=None)
        self.assertEqual([e.ok for e in self.a.write_events], [None])

    def test_a_child_that_is_running_has_the_requirement_alone(self):
        self.with_end(status='running')
        self.assertEqual((len(self.a.planned), self.a.write_events), (1, []))

    def test_the_event_is_made_again_when_the_launching_call_ends_later(self):
        self.with_end(ok=None)
        self.assertEqual([e.ok for e in self.a.write_events], [None])
        self.s.orch_log.close_window('tc1', T0 + 102, True)
        self.s.refresh_facts({self.CHILD: 'done'})
        self.assertEqual([e.ok for e in self.a.write_events], [True])
        gen = self.a.facts_gen
        self.s.refresh_facts({self.CHILD: 'done'})
        self.assertEqual(self.a.facts_gen, gen)                                             # nothing changed: nothing made again

    def test_the_run_of_the_child(self):
        self.with_end()
        self.assertEqual((self.a.run, self.a.run_start), (None, 0.0))                       # no run in the record yet
        self.a.feed({'type': 'user', 'timestamp': iso(T0 + 2), 'message': {'content': 'start'}})
        self.s.refresh_facts({self.CHILD: 'done'})
        self.assertEqual((self.a.run, self.a.run_start), (1, T0 + 2))


class ResumedChild(unittest.TestCase):
    """P2-4: a `claude -p` child that was resumed has a launching command for every run (`--resume`): each run has its own requirement (`Planned`) and its own event, made of that run's call, its
    time, its end and its last text. The first run's requirement and time are not mixed with the last run's end and text."""

    SID, CHILD, setUp, with_end = CliSession.SID, CliSession.CHILD, CliSession.setUp, CliSession.with_end

    def two_runs(self):
        a = self.a
        a.cli = dict(a.cli, call='tc1', calls=['tc1', 'tc2'], calls_certain=[True, True])
        a.feed({'type': 'user', 'timestamp': iso(T0 + 2), 'message': {'content': 'start'}})
        a.talk.append({'ts': T0 + 20, 'kind': 'end', 'text': 'first report'})
        a.feed({'type': 'cost-state', 'timestamp': iso(T0 + 25), 'totalDuration': 1000})
        a.feed({'type': 'user', 'timestamp': iso(T0 + 50), 'message': {'content': 'again'}})
        a.talk.append({'ts': T0 + 80, 'kind': 'end', 'text': 'second report'})
        a.redirects = [self.Redirect(fd=1, op='>', path_raw='r1/C.md', path_resolved='/w/talk/r1/C.md')]
        a.run_redirects = [a.redirects, [self.Redirect(fd=1, op='>', path_raw='r2/C.md', path_resolved='/w/talk/r2/C.md')]]
        self.s.orch_log.add_window(AG.CmdWindow('orch', 'tc1', T0 + 1, T0 + 30, True))
        self.s.orch_log.add_window(AG.CmdWindow('orch', 'tc2', T0 + 45, T0 + 95, True))

    def test_every_run_has_its_own_requirement(self):
        self.two_runs()
        self.s.refresh_facts({self.CHILD: 'running'})
        self.assertEqual([(p.path, p.op, p.call, p.run, p.ts) for p in self.a.planned],
                         [('/w/talk/r1/C.md', '>', 'tc1', 1, T0 + 1), ('/w/talk/r2/C.md', '>', 'tc2', 2, T0 + 45)])

    def test_and_its_own_event_made_of_its_own_end_and_text(self):
        import hashlib
        self.two_runs()
        self.s.refresh_facts({self.CHILD: 'done'})
        sha = lambda t: (hashlib.sha1(t.encode()).hexdigest(), hashlib.sha1((t + '\n').encode()).hexdigest())
        got = sorted((e.path, e.ts, e.call, e.run, e.proof, e.span, e.ok, e.shas) for e in self.a.write_events)
        self.assertEqual(got, [('/w/talk/r1/C.md', T0 + 20, 'tc1', 1, 'content', (T0 + 1, T0 + 20), True, sha('first report')),
                               ('/w/talk/r2/C.md', T0 + 100, 'tc2', 2, 'content', (T0 + 45, T0 + 100), True, sha('second report'))])

    def test_a_run_that_has_not_ended_has_the_requirement_alone(self):
        self.two_runs()
        self.s.refresh_facts({self.CHILD: 'running'})
        self.assertEqual([(e.path, e.run) for e in self.a.write_events], [('/w/talk/r1/C.md', 1)])        # (the first run ended: a later one began)

    def test_a_run_whose_call_was_only_counted_by_order_has_no_plan(self):
        self.two_runs()
        self.a.cli = dict(self.a.cli, calls_certain=[True, False])
        self.s.refresh_facts({self.CHILD: 'done'})
        self.assertEqual([p.run for p in self.a.planned], [1])
        self.assertEqual([e.run for e in self.a.write_events], [1])

    def test_nothing_is_made_twice(self):
        self.two_runs()
        self.s.refresh_facts({self.CHILD: 'done'})
        gen = self.a.facts_gen
        self.s.refresh_facts({self.CHILD: 'done'})
        self.assertEqual(self.a.facts_gen, gen)

    def test_the_redirects_of_each_run_come_from_the_launches_of_its_own_call(self):
        """`Session._redirects_by_run`: the launches of the call the evidence names for run k that can be that run's (the command reader's veto), one list for each run."""
        import types
        from board.facts import Redirect

        def launch(path):
            return types.SimpleNamespace(reader=True, redirects=[Redirect(1, '>', path, '/w/' + path, [])])
        run = lambda: types.SimpleNamespace()
        dec = types.SimpleNamespace(call=types.SimpleNamespace(launches=[launch('r1/C.md')]), child=types.SimpleNamespace(runs=[run(), run(), run()]),
                                    calls=[types.SimpleNamespace(launches=[launch('r1/C.md')]), types.SimpleNamespace(launches=[launch('r2/C.md')]), None])
        with mock.patch.dict(server.LINKS.decisions, {self.CHILD: dec}), mock.patch('board.affil.launch_ok', lambda L, child, run: True):
            self.assertEqual([[r.path_resolved for r in reds] for reds in self.s._redirects_by_run(self.CHILD)], [['/w/r1/C.md'], ['/w/r2/C.md'], []])
            self.assertEqual([r.path_resolved for r in self.s._redirects_of(self.CHILD)], ['/w/r1/C.md'])           # (the first run's, as it was)


class LiveOutPath(unittest.TestCase):
    """The `-o` of a live `codex exec` process: an absolute path as it is, a relative one from the folder its thread was started in, unless the command moves the folder."""

    def out(self, *words, cwd='/w'):
        from board import lineage
        return lineage.exec_out(['codex', 'exec'] + list(words), cwd)

    def test_absolute_and_relative(self):
        self.assertEqual(self.out('-o', '/w/talk/r1/B.md', 'go'), '/w/talk/r1/B.md')
        self.assertEqual(self.out('-o', 'talk/r1/B.md', 'go'), '/w/talk/r1/B.md')
        self.assertEqual(self.out('--output-last-message=talk/../talk/r1/B.md', 'go'), '/w/talk/r1/B.md')
        self.assertEqual(self.out('--output-last-message', './B.md', 'go', cwd='/w/talk'), '/w/talk/B.md')

    def test_without_the_folder_a_relative_path_is_not_known(self):
        from board import lineage
        self.assertIsNone(lineage.exec_out(['codex', 'exec', '-o', 'B.md', 'go']))
        self.assertIsNone(self.out('-o', 'B.md', 'go', cwd=None))
        self.assertIsNone(self.out('-o', 'B.md', 'go', cwd='relative/folder'))
        self.assertEqual(lineage.exec_out(['codex', 'exec', '-o', '/abs/B.md', 'go']), '/abs/B.md')

    def test_a_command_that_moves_the_folder_does_not_say_where_a_relative_path_is_from(self):
        self.assertIsNone(self.out('-C', '/other', '-o', 'B.md', 'go'))
        self.assertIsNone(self.out('--cd', 'sub', '-o', 'B.md', 'go'))
        self.assertIsNone(self.out('--cd=sub', '-o', 'B.md', 'go'))
        self.assertEqual(self.out('-C', '/other', '-o', '/abs/B.md', 'go'), '/abs/B.md')                       # an absolute one does not need it

    def test_what_was_not_read_before_is_not_read_now(self):
        self.assertIsNone(self.out('-o', 'a.md', '-o', 'b.md', 'go'))                                          # two of them
        self.assertIsNone(self.out('go', '--', '-o', 'B.md'))                                                  # after `--`
        self.assertIsNone(self.out('-o'))


class OpenExecCalls(unittest.TestCase):
    """The exec calls of a Codex thread that have not ended, with the text of their command (the record of a command is written when it ends)."""

    JS = 'const r = await tools.exec_command({cmd: %s, workdir: %s, yield_time_ms: 1000});\ntext(r.output);'

    def js(self, cmd, where='/w'):
        return self.JS % (json.dumps(cmd), json.dumps(where))

    def test_the_command_of_an_exec_call(self):
        from board.codex_parse import codex_exec_command
        self.assertEqual(codex_exec_command(self.js('cd /w && claude -p "x" > a.md')), ('cd /w && claude -p "x" > a.md', '/w'))
        self.assertEqual(codex_exec_command("tools.exec_command({cmd: 'ls -la'})"), ('ls -la', None))
        self.assertIsNone(codex_exec_command("tools.exec_command({cmd: 'ls'}); tools.exec_command({cmd: 'pwd'})"))      # two: nothing says which one a process is of
        self.assertIsNone(codex_exec_command('tools.apply_patch(x)'))
        self.assertIsNone(codex_exec_command('tools.exec_command({cmd: `echo ${x}`})'))                                 # not a literal
        self.assertIsNone(codex_exec_command(None))

    def test_open_calls_come_and_go(self):
        o = AG.OpenExecs()
        o.add('k1', T0, self.js('claude -p x > a.md'))
        o.add('k2', T0 + 1, 'tools.apply_patch(x)')
        self.assertEqual(o.commands(), [('k1', T0, 'claude -p x > a.md', '/w')])
        self.assertIsNone(o.only())                                                                                 # two are open
        o.output('k2')
        self.assertEqual(o.only(), T0)
        o.output('k1', [{'type': 'input_text', 'text': 'Script completed'}])
        self.assertEqual((o.commands(), o.only()), ([], None))

    def test_a_cell_that_went_on_in_the_background_keeps_its_command_until_the_command_ends(self):
        o = AG.OpenExecs()
        o.add('k1', T0, self.js('claude -p x > a.md'))
        o.output('k1', [{'type': 'input_text', 'text': 'Script running with cell ID 7'}, {'type': 'input_text', 'text': ''}])
        self.assertEqual(o.commands(), [('k1', T0, 'claude -p x > a.md', '/w')])
        self.assertIsNone(o.only())                                                                                 # it is no exec call that is open: nothing runs inside it any more
        o.finished('something else')
        self.assertEqual(len(o.commands()), 1)
        o.finished('claude -p x > a.md')
        self.assertEqual(o.commands(), [])

    def test_a_turn_forgets_them(self):
        o = AG.OpenExecs()
        o.add('k1', T0, self.js('ls'))
        o.clear()
        self.assertEqual(o.commands(), [])

    def test_the_thread_keeps_them_from_its_rollout(self):
        a = CodexBase('setUp')
        a.setUp()
        a.turn(T0)
        a.feed(T0 + 1, 'response_item', 'custom_tool_call', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'k1', 'input': self.js('claude -p x > a.md')})
        self.assertEqual(a.a._cx_execs.commands(), [('k1', T0 + 1, 'claude -p x > a.md', '/w')])
        a.output(T0 + 9, 'k1')
        self.assertEqual(a.a._cx_execs.commands(), [])
        a.call(T0 + 10, 'k2')                                                                                         # (no command text in it)
        a.turn(T0 + 20)
        self.assertEqual(a.a._cx_execs.commands(), [])

    def test_the_orchestrator_thread_too_and_the_end_of_the_command_forgets_a_running_cell(self):
        e = {'id': '019c0000-0000-7000-8000-00000000000a', 'path': '/nonexistent/x.jsonl', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root', 'prefix_ord': 0, 'first_user': None}
        s = server.CodexSession(e)
        row = lambda ts, typ, pt, p: s._feed_cx({'ts': ts, 'type': typ, 'pt': pt, 'ord': None, 'p': dict(p, type=pt)})
        row(T0 + 1, 'response_item', 'custom_tool_call', {'name': 'exec', 'call_id': 'k1', 'input': self.js('claude -p x > a.md')})
        self.assertEqual([c[0] for c in s._cx_execs.commands()], ['k1'])
        row(T0 + 2, 'response_item', 'custom_tool_call_output', {'call_id': 'k1', 'output': [{'type': 'input_text', 'text': 'Script running with cell ID 7'}]})
        self.assertEqual([c[0] for c in s._cx_execs.commands()], ['k1'])
        row(T0 + 30, 'event_msg', 'item_completed', {'item': {'type': 'CommandExecution', 'id': 'c1', 'process_id': '9', 'command': ['/bin/bash', '-lc', 'claude -p x > a.md'], 'cwd': 'file:///w',
                                                               'status': 'completed', 'exit_code': 0, 'duration': {'secs': 28, 'nanos': 0}}})
        self.assertEqual(s._cx_execs.commands(), [])


class PlannedLive(unittest.TestCase):
    """A `claude -p` child that a Codex shell started and that is still running: the command is written when it ends, but the exec call it runs in is there, with its text. The redirect of
    the one command that can be the child's is the requirement of the child while it works (J7)."""
    R = '019c0000-0000-7000-8000-00000000000a'
    CHILD = '22222222-2222-4222-8222-222222222222'
    PROMPT = 'Review the currency handling of the ledger package carefully and report in plain words what is wrong.'

    def setUp(self):
        e = {'id': self.R, 'path': '/nonexistent/x.jsonl', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root', 'prefix_ord': 0, 'first_user': None}
        self.s = server.CodexSession(e)

    def call(self, call_id, ts, cmd, where='/w'):
        js = OpenExecCalls.JS % (json.dumps(cmd), json.dumps(where))
        self.s._feed_cx({'ts': ts, 'type': 'response_item', 'pt': 'custom_tool_call', 'ord': None, 'p': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': call_id, 'input': js}})

    def child(self, prompt=None, cwd='/w', first=T0 + 5, tree=None):
        a = server.Agent(self.CHILD, {'description': 'child'})
        a.origin, a.cwd, a.first_ts = 'cli', cwd, first
        a.cli = {'sid': tree or self.R, 'node': None, 'rule': 'env', 'call': None, 'calls': [None], 'calls_certain': [False], 'bash_ts': T0, 'parent_kind': 'codex'}
        a.talk.append({'ts': first, 'kind': 'in', 'text': self.PROMPT if prompt is None else prompt})
        self.s.agents[a.id] = a
        return a

    def plan(self, a, status='running'):
        self.s.refresh_facts({a.id: status})
        return [(p.path, p.op, p.call, p.run, p.src, p.ts) for p in a.planned]

    LAUNCH = 'cd /w && claude -p --model m "%s" > talk/r1/A.md'

    def test_the_redirect_of_the_one_command(self):
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        a = self.child()
        self.assertEqual(self.plan(a), [('/w/talk/r1/A.md', '>', 'k1', 1, 'command', T0 + 1)])
        self.assertEqual(a.write_events, [])                                                                     # (it has not ended: only the requirement)

    def test_append_and_the_folder_of_the_call(self):
        self.call('k1', T0 + 1, 'claude -p "%s" >> talk/r1/A.md' % self.PROMPT, where='/w')
        self.assertEqual(self.plan(self.child()), [('/w/talk/r1/A.md', '>>', 'k1', 1, 'command', T0 + 1)])

    def test_two_commands_that_can_be_the_childs_say_nothing(self):
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        self.call('k2', T0 + 2, 'cd /w && claude -p --model m "%s" > talk/r1/B.md' % self.PROMPT)
        self.assertEqual(self.plan(self.child()), [])

    def test_a_command_with_other_words_or_another_folder_is_not_the_childs(self):
        self.call('k1', T0 + 1, self.LAUNCH % 'Something else entirely that this child was never told to do at all.')
        self.assertEqual(self.plan(self.child()), [])
        self.s._cx_execs.clear()
        self.call('k2', T0 + 1, self.LAUNCH % self.PROMPT)
        self.assertEqual(self.plan(self.child(cwd='/elsewhere')), [])
        self.assertEqual(self.plan(self.child(cwd='/w')), [('/w/talk/r1/A.md', '>', 'k2', 1, 'command', T0 + 1)])

    def test_a_command_called_after_the_child_began_did_not_start_it(self):
        self.call('k1', T0 + 9, self.LAUNCH % self.PROMPT)
        self.assertEqual(self.plan(self.child(first=T0 + 5)), [])

    def test_a_loop_of_launches_is_not_one_child(self):
        self.call('k1', T0 + 1, 'cd /w && for i in 1 2 3; do claude -p "%s" > talk/r1/A.md; done' % self.PROMPT)
        self.assertEqual(self.plan(self.child()), [])

    def test_a_command_without_a_redirect_or_without_text(self):
        self.call('k1', T0 + 1, 'cd /w && claude -p "%s"' % self.PROMPT)
        self.assertEqual(self.plan(self.child()), [])
        self.s._cx_execs.add('k2', T0 + 1, 'tools.exec_command({cmd: `echo ${x}`})')
        self.assertEqual(self.plan(self.child()), [])

    def test_the_launcher_must_be_the_thread_that_ran_the_command(self):
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        self.assertEqual(self.plan(self.child(tree='019c0000-0000-7000-8000-0000000000ff')), [])

    def test_the_call_that_is_known_is_not_looked_for_among_the_open_ones(self):
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        a = self.child()
        a.cli = dict(a.cli, call='item_9', calls=['item_9'], calls_certain=[True])
        self.assertEqual(self.plan(a), [])                                                                       # its redirects are the record's (`_redirects_of`): none here

    def test_a_cell_that_went_on_in_the_background_still_gives_it(self):
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        self.s._feed_cx({'ts': T0 + 2, 'type': 'response_item', 'pt': 'custom_tool_call_output', 'ord': None, 'p': {'type': 'custom_tool_call_output', 'call_id': 'k1',
                                                                                                                  'output': [{'type': 'input_text', 'text': 'Script running with cell ID 7'}]}})
        self.assertEqual(self.plan(self.child()), [('/w/talk/r1/A.md', '>', 'k1', 1, 'command', T0 + 1)])

    def test_when_the_command_has_ended_its_record_gives_it_and_nothing_is_made_twice(self):
        from board.facts import Redirect
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        a = self.child()
        self.plan(a)
        self.s._feed_cx({'ts': T0 + 9, 'type': 'response_item', 'pt': 'custom_tool_call_output', 'ord': None, 'p': {'type': 'custom_tool_call_output', 'call_id': 'k1', 'output': 'x'}})
        a.cli = dict(a.cli, call='item_1', calls=['item_1'], calls_certain=[True], bash_ts=T0 + 1)
        a.redirects = [Redirect(1, '>', 'talk/r1/A.md', '/w/talk/r1/A.md', [])]
        self.assertEqual(self.plan(a), [('/w/talk/r1/A.md', '>', 'item_1', 1, 'command', T0 + 1)])
        self.assertEqual(len(a.planned), 1)

    def test_the_instruction_is_compared_the_way_the_link_compares_it(self):
        """P2-5: the quotes, a double blank and a line break in the command do not make it another instruction (the same normalising on both sides)."""
        prompt = 'Review the "currency" handling of the ledger package carefully\nand report in plain words what is wrong.'
        for said in ('Review the "currency" handling of the ledger package carefully  and report in plain words what is wrong.',
                     "Review the 'currency' handling of the ledger package carefully\n\nand report in plain words what is wrong.\n"):
            self.s._cx_execs.clear()
            self.call('k1', T0 + 1, "cd /w && claude -p --model m '%s' > talk/r1/A.md" % said.replace("'", "'\\''"))
            self.assertEqual(self.plan(self.child(prompt=prompt)), [('/w/talk/r1/A.md', '>', 'k1', 1, 'command', T0 + 1)], said)
        self.s._cx_execs.clear()
        self.call('k1', T0 + 1, "cd /w && claude -p --model m 'Review the other thing' > talk/r1/A.md")
        self.assertEqual(self.plan(self.child(prompt=prompt)), [])

    def test_the_event_comes_when_the_child_has_ended(self):
        self.call('k1', T0 + 1, self.LAUNCH % self.PROMPT)
        a = self.child()
        a.last_ts = T0 + 60
        a.talk.append({'ts': T0 + 59, 'kind': 'end', 'text': 'the report'})
        self.s.refresh_facts({a.id: 'done'})
        (ev,) = a.write_events
        self.assertEqual((ev.path, ev.evidence, ev.proof, ev.ok, ev.span, ev.call, ev.run), ('/w/talk/r1/A.md', 'planned', 'content', None, (T0 + 1, T0 + 60), 'k1', 1))


class HintWords(unittest.TestCase):
    """P3: the words that let a shell command point the list of debates at a folder are looked for in the command, not in the body of a heredoc it writes."""

    def test_the_words_of_a_heredoc_body_are_no_hint(self):
        S = server.Session
        for body in ('Notes for r1', '1회차 메모', 'see README.md and round2', 'index.md'):
            self.assertEqual(S._shell_hints('cat > notes.md <<EOF\n%s\nEOF' % body, '/w/talk/r1'), [], body)
            self.assertEqual(S._shell_hints("cat > notes.md <<'EOF'\n%s\nEOF" % body, '/w/talk/r1'), [], body)

    def test_the_words_of_the_command_are_a_hint_still(self):
        S = server.Session
        self.assertEqual(S._shell_hints('cat > r1/notes.md <<EOF\n1회차\nEOF', '/w/talk'), [('/w/talk/r1/notes.md', False)])
        self.assertEqual(S._shell_hints("cat > notes.md <<'EOF'\nNotes for r1\nEOF\nmkdir r2", '/w/talk'), [('/w/talk/notes.md', False), ('/w/talk/r2', True)])
        self.assertEqual(S._shell_hints('cat > brief.md <<EOF\nx\nEOF', '/w/talk'), [('/w/talk/brief.md', False)])
        self.assertEqual(S._shell_hints('echo "Notes for r1" > notes.md', '/w/talk/r1'), [('/w/talk/r1/notes.md', False)])        # (quotes are read as they were)


class ParsedOnce(unittest.TestCase):
    """O15: a Bash command is read once for everything the collector wants of it (what it writes, what it reads, what it launches), and a command that cannot say anything of files is not read at all."""

    def counted(self, names):
        """A context in which the calls of the functions `names` (of board.agents and board.link) are counted."""
        from board import link as L
        counts = {n: 0 for n in names}
        stack = contextlib.ExitStack()
        for n in names:
            mod = AG if hasattr(AG, n) and n not in ('shell_code', 'literal_env', '_classify') else L
            real = getattr(mod, n)

            def wrap(*a, _n=n, _real=real, **kw):
                counts[_n] += 1
                return _real(*a, **kw)
            stack.enter_context(mock.patch.object(mod, n, wrap))
        return counts, stack

    def use(self, cmd):
        log = AG.EventLog()
        AG.ClaudeCalls('a', log, lambda: 1).use(T0, {'id': 't1'}, 'Bash', {'command': cmd}, '/w')
        return log

    def test_one_reading_for_the_writes_the_reads_and_the_launches(self):
        cmd = 'cd /w && OUT=r1/B.md; cat r1/A.md > $OUT 2>&1; BULLPEN_ROOM=/w/t claude -p x > r1/C.md; echo done'
        counts, stack = self.counted(['_commands_of', 'shell_code', 'literal_env'])
        with stack:
            log = self.use(cmd)
            AG.launch_pieces(cmd)                                                                          # (what the tag of the child reads later)
        self.assertEqual(counts, {'_commands_of': 1, 'shell_code': 1, 'literal_env': 1})                   # (the variables of the text are worked out once, and only when a path names one)
        self.assertEqual([e.path for e in log.events()], ['/w/r1/B.md'])
        self.assertEqual([r.path for r in log.reads], ['/w/r1/A.md'])
        counts, stack = self.counted(['literal_env'])
        with stack:
            self.use('cd /w && cat r1/A.md > r1/B.md')
        self.assertEqual(counts, {'literal_env': 0})

    def test_what_is_read_for_one_is_the_same_as_what_is_read_alone(self):
        for cmd in ('cd /w && cat r1/A.md > r1/B.md 2>&1; claude -p x > r1/C.md', 'echo hi | tee r1/A.md; head -n 3 r1/B.md', "bash -c 'cat r1/A.md > r1/B.md'", 'ls > /dev/null 2>&1'):
            first = [(w.path, w.decides, w.kind) for w in AG.shell_writes(cmd, '/w', True, places=True, md_only=False, skip_launch=True)], AG.shell_reads(cmd, '/w'), AG.shell_mkdirs(cmd, '/w')
            second = [(w.path, w.decides, w.kind) for w in AG.shell_writes(cmd, '/w', True, places=True, md_only=False, skip_launch=True)], AG.shell_reads(cmd, '/w'), AG.shell_mkdirs(cmd, '/w')
            self.assertEqual(first, second, cmd)

    def test_a_bash_call_of_an_agent_is_read_for_its_writes_once(self):
        """Review 2 (N6, Opus P2-4): the events and the page's list of the files a call wrote were read from the text one after the other, twice for each call. The list comes from the events now."""
        a = server.Agent('a0123456789abcdef', {'description': 'T1-A study'})
        with mock.patch.object(AG, 'shell_writes', wraps=AG.shell_writes) as spy:
            a.feed({'type': 'assistant', 'timestamp': iso(T0), 'cwd': '/w', 'message': {'id': 'm1', 'content': [
                {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': "cat > r1/A.md <<'EOF'\nx\nEOF\necho y > r1/B.md; echo z > src/c.py"}}]}})
        self.assertEqual(spy.call_count, 1)
        self.assertEqual([w['paths'] for w in a.shell_writes], [['/w/r1/A.md', '/w/r1/B.md']])               # (the markdown files, as before)
        self.assertEqual(sorted(os.path.basename(e.path) for e in a.write_events), ['A.md', 'B.md', 'c.py'])

    def test_a_command_that_says_nothing_of_files_is_not_read(self):
        counts, stack = self.counted(['_commands_of', 'shell_code'])
        with stack:
            for cmd in ('ls -la 2>&1', 'make test > /dev/null 2>&1', 'git status &> /dev/null', 'echo done', 'python3 x.py 2>/dev/null >/dev/null', 'sleep 1 >&2', 'echo hi 1>&2',
                        'pytest -q 2>&1 | wc -l', 'git diff --stat 2>&1 >/dev/null', 'cd /w && make 2>&1'):
                self.use(cmd)
        self.assertEqual(counts, {'_commands_of': 0, 'shell_code': 0})

    def test_a_command_that_writes_to_a_file_is_read_even_with_the_noise(self):
        events = self.use('make test > /dev/null 2>&1; echo hi > r1/A.md').events()
        self.assertEqual([e.path for e in events], ['/w/r1/A.md'])
        self.assertEqual([e.path for e in self.use('pytest 2>&1 | tee r1/log.txt').events()], ['/w/r1/log.txt'])

    def test_the_words_of_a_command_are_not_taken_apart_when_they_say_nothing(self):
        cmd = 'echo a; ls -la; cat r1/A.md; grep x y'
        counts, stack = self.counted(['_classify'])
        with stack:
            self.assertEqual(AG.shell_reads(cmd, '/w'), ['/w/r1/A.md'])
        self.assertEqual(counts['_classify'], 1)                                                           # only the command whose first word is one of the readers
        counts, stack = self.counted(['_classify'])
        with stack:
            AG.shell_writes('echo a; ls -la > /dev/null; echo b > r1/B.md; grep x y', '/w', True, places=True, md_only=False, skip_launch=True)
        self.assertEqual(counts['_classify'], 1)                                                           # only the one that has a redirect that is a file


class OrchEvents(unittest.TestCase):
    """The orchestrator's own writes and commands: events and windows of 'orch', and the calls and groups behind the hints."""
    SID = 'dddddddd-0000-4000-8000-0000000000d1'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.proj = os.path.join(self.root, 'claude', 'projects', 'p')
        os.makedirs(self.proj)
        self.work = os.path.join(self.root, 'work')
        self.n = 0
        self.links = server.LinkIndex()
        self.links.ready.set()
        stack = patched(HOME=self.root, CLAUDE_HOME=os.path.join(self.root, 'claude'), PROJECTS=os.path.join(self.root, 'claude', 'projects'), LINKS=self.links)
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        self.path = os.path.join(self.proj, self.SID + '.jsonl')
        self.rows([{'type': 'user', 'timestamp': iso(T0), 'cwd': self.work, 'sessionId': self.SID, 'message': {'role': 'user', 'content': 'start'}, 'origin': {'kind': 'human'}}])
        self.session = server.Session(self.path)

    def rows(self, rows):
        with open(self.path, 'a') as f:
            f.write('\n'.join(json.dumps(r) for r in rows) + '\n')

    def call(self, name, inp, error=None, mid=None, tur=None):
        self.n += 1
        tid, ts = 'toolu_%d' % self.n, T0 + self.n * 5
        rows = [{'type': 'assistant', 'timestamp': iso(ts), 'cwd': self.work, 'sessionId': self.SID,
                 'message': {'id': mid or 'msg_%d' % self.n, 'role': 'assistant', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}}]
        if error is not None:
            r = {'type': 'user', 'timestamp': iso(ts + 1), 'cwd': self.work, 'sessionId': self.SID,
                 'message': {'role': 'user', 'content': [dict({'type': 'tool_result', 'tool_use_id': tid, 'content': 'x'}, **({'is_error': True} if error else {}))]}}
            if tur:
                r['toolUseResult'] = tur
            rows.append(r)
        self.rows(rows)
        self.session.poll()
        return tid, ts

    def test_every_write_of_the_orchestrator_is_an_event_of_orch(self):
        talk = os.path.join(self.root, 'talk')
        self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': 'x'}, error=False, tur={'type': 'create'})
        self.call('Edit', {'file_path': os.path.join(talk, 'r1', 'A.md')}, error=False)
        self.call('Bash', {'command': "cat > talk/FINAL.md <<'EOF'\nx\nEOF"}, error=False)
        self.call('Write', {'file_path': os.path.join(self.root, 'notes', 'PLAN.md'), 'content': 'x'}, error=True)
        got = [(os.path.basename(e.path), e.kind, e.ok, e.proof, e.agent) for e in self.session.orch_events]
        self.assertEqual(got, [('brief.md', 'create', True, 'tool', 'orch'), ('A.md', 'update', True, 'tool', 'orch'), ('FINAL.md', 'replace', True, 'exit', 'orch'),
                               ('PLAN.md', 'unknown', False, 'tool', 'orch')])         # (a plan is no hint, but it is an event: the judgment decides)

    def test_every_bash_call_is_a_window_of_orch(self):
        self.call('Bash', {'command': 'cat talk/r1/A.md'}, error=False)
        self.call('Bash', {'command': 'false'}, error=True)
        self.call('Bash', {'command': 'sleep 99'})
        ws = list(self.session.orch_windows)
        self.assertEqual([(w.agent, w.ok, w.t1 is None) for w in ws], [('orch', True, False), ('orch', False, False), ('orch', None, True)])
        self.assertEqual(ws[0].reads, (os.path.join(self.work, 'talk', 'r1', 'A.md'),))

    def test_a_hint_knows_the_call_and_the_message_of_the_write(self):
        talk = os.path.join(self.root, 'talk')
        tid1, _ = self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': 'x'}, error=False, mid='msg_A')
        tid2, _ = self.call('Bash', {'command': 'mkdir -p %s/r1' % talk}, error=False, mid='msg_A')
        tid3, _ = self.call('Bash', {'command': 'mkdir -p %s/r2' % talk}, error=False, mid='msg_B')
        h = self.session.orch_hint_facts[talk]
        self.assertEqual(h.ts, self.session.orch_hints[talk])                             # the old value is the same time
        self.assertEqual(h.calls, frozenset((tid1, tid2, tid3)))
        self.assertEqual(h.groups, frozenset((('claude', self.SID, None, 'msg_A'), ('claude', self.SID, None, 'msg_B'))))
        self.assertEqual(set(self.session.orch_hint_facts), set(self.session.orch_hints))

    def test_a_write_that_failed_is_no_hint_and_no_hint_fact(self):
        self.call('Write', {'file_path': os.path.join(self.root, 'f', 'brief.md'), 'content': 'x'}, error=True)
        self.assertEqual((self.session.orch_hints, self.session.orch_hint_facts), ({}, {}))

    def test_the_hints_let_go_are_let_go_of_both(self):
        from board import sessions
        with mock.patch.object(sessions, 'ORCH_HINTS_MAX', 2):
            for name in ('n1', 'n2', 'n3'):
                self.call('Write', {'file_path': os.path.join(self.root, name, 'brief.md'), 'content': 'x'}, error=False)
        self.assertEqual(sorted(self.session.orch_hint_facts), sorted(self.session.orch_hints))
        self.assertEqual(len(self.session.orch_hint_facts), 2)

    def test_an_agent_tool_call_keeps_the_message_it_was_made_in(self):
        tid, _ = self.call('Agent', {'description': 'A study', 'prompt': 'go'}, mid='msg_P')
        self.assertEqual(self.session.spawns[tid]['msg'], 'msg_P')

    def test_the_restart_of_the_record_makes_the_events_again_once(self):
        self.call('Write', {'file_path': os.path.join(self.root, 'talk', 'brief.md'), 'content': 'x'}, error=False)
        before = list(self.session.orch_events)
        self.session.tail.pos = 10 ** 9                                                      # as if the file shrank: it is read again from its start
        with open(self.path, 'rb') as f:
            data = f.read()
        with open(self.path, 'wb') as f:
            f.write(data)
        self.session.poll()
        self.assertEqual(list(self.session.orch_events), before)


class Malformed(ClaudeBase):
    """A line that is not what it should be makes no event and does not stop what the page reads from it (the old lists)."""

    def test_a_line_with_no_time(self):
        self.a.feed({'type': 'assistant', 'cwd': '/w', 'message': {'id': 'm1', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Write', 'input': {'file_path': '/x/r1/A.md'}},
                                                                                      {'type': 'tool_use', 'id': 't2', 'name': 'Bash', 'input': {'command': 'echo x > /x/B.md'}}]}})
        self.assertEqual((self.a.write_events, list(self.a.windows)), ([], []))
        self.assertEqual([w['path'] for w in self.a.writes], ['/x/r1/A.md'])
        self.assertEqual([w['paths'] for w in self.a.shell_writes], [['/x/B.md']])

    def test_a_codex_item_with_no_time(self):
        a = CodexBase('setUp')
        a.setUp()
        a.item(None, {'type': 'FileChange', 'id': 'f1', 'status': 'completed', 'changes': {'/x/A.md': {'type': 'add'}}})
        self.assertEqual(a.a.write_events, [])

    def test_a_command_that_is_not_a_command(self):
        log = AG.EventLog()
        calls = AG.ClaudeCalls('x', log, lambda: None)
        for i, cmd in enumerate((None, 5, ['ls'], {'a': 1})):
            calls.use(T0, {'id': 't%d' % i}, 'Bash', {'command': cmd}, '/w')                  # (the old code of the page does not take these either)
        calls.use(T0, {'id': 'w'}, 'Write', {'file_path': ['x']}, '/w')
        self.assertEqual((log.events(), list(log.windows)), ([], []))


class RunNumbers(ClaudeBase):
    """Every event, requirement and tag has the number of the run it is in (3rd contract): a Claude epoch, a Codex turn n; None where it is not known."""

    def second_run(self):
        self.a.feed({'type': 'user', 'timestamp': iso(T0 - 5), 'message': {'content': 'start'}})
        self.use('Write', {'file_path': '/x/r1/A.md'}, T0, 'w1')
        self.use('Bash', {'command': 'echo x > /w/A.md'}, T0 + 1, 'b1')
        self.a.feed({'type': 'cost-state', 'timestamp': iso(T0 + 10), 'totalDuration': 1000})                  # the process ended: the next instruction is the next run
        self.a.feed({'type': 'user', 'timestamp': iso(T0 + 50), 'message': {'content': 'again'}})
        self.use('Write', {'file_path': '/x/r1/B.md'}, T0 + 60, 'w2')
        self.use('Bash', {'command': 'echo x > /w/B.md'}, T0 + 61, 'b2')

    def test_claude_events_have_the_epoch_of_the_run_they_were_made_in(self):
        self.second_run()
        self.assertEqual([(os.path.basename(e.path), e.run) for e in self.a.write_events], [('A.md', 1), ('A.md', 1), ('B.md', 2), ('B.md', 2)])

    def test_a_result_that_comes_in_a_later_run_keeps_the_run_of_its_call(self):
        self.a.feed({'type': 'user', 'timestamp': iso(T0 - 5), 'message': {'content': 'start'}})
        self.use('Write', {'file_path': '/x/r1/A.md'}, T0, 'w1')
        self.a.feed({'type': 'cost-state', 'timestamp': iso(T0 + 10), 'totalDuration': 1000})
        self.a.feed({'type': 'user', 'timestamp': iso(T0 + 50), 'message': {'content': 'again'}})
        self.result('w1', T0 + 51, tur={'type': 'create'})
        self.assertEqual([e.run for e in self.a.write_events], [1])

    def test_where_the_run_is_not_known_it_is_none(self):
        log = AG.EventLog()
        calls = AG.ClaudeCalls('orch', log, lambda: None)                                    # (the orchestrator: its run is not read by the judgment)
        calls.use(T0, {'id': 'w1'}, 'Write', {'file_path': '/x/r1/A.md'}, '/w')
        self.assertEqual([e.run for e in log.events()], [None])

    def test_codex_items_have_the_number_of_their_turn(self):
        a = CodexBase('setUp')
        a.setUp()
        a.turn(T0)
        a.item(T0 + 5, {'type': 'FileChange', 'id': 'f1', 'status': 'completed', 'changes': {'/x/A.md': {'type': 'add'}}})
        a.command(T0 + 6, 'echo x > /w/A.md')
        a.turn(T0 + 100)
        a.item(T0 + 105, {'type': 'FileChange', 'id': 'f2', 'status': 'completed', 'changes': {'/x/B.md': {'type': 'add'}}})
        a.command(T0 + 106, 'echo x > /w/B.md', iid='c2')
        self.assertEqual([(os.path.basename(e.path), e.run) for e in a.a.write_events], [('A.md', 0), ('A.md', 0), ('B.md', 1), ('B.md', 1)])

    def test_the_planned_output_and_its_event_have_the_turn(self):
        a = PlannedCodex('test_a_turn_that_ended_well').agent()
        server.CodexLinker._plan(a)
        self.assertEqual([p.run for p in a.planned], [0])
        self.assertEqual([e.run for e in a.write_events], [0])

    def test_the_planned_output_of_a_cli_child_has_the_epoch_of_its_run(self):
        t = CliSession('test_the_run_of_the_child')
        t.setUp()
        t.a.feed({'type': 'user', 'timestamp': iso(T0 + 2), 'message': {'content': 'start'}})
        t.with_end()
        self.assertEqual([p.run for p in t.a.planned], [1])
        self.assertEqual([e.run for e in t.a.write_events], [1])

    def test_the_tag_has_the_run_the_value_was_read_for(self):
        from board.facts import Tag
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, '/nonexistent/x.jsonl')
        with tempfile.TemporaryDirectory() as tmp:
            room = os.path.realpath(tmp)
            a = server.Agent('c' * 8 + '-0000-4000-8000-000000000001', {'description': 'child'})
            a.origin, a.cwd = 'cli', room
            a.cli = {'sid': 'x', 'node': None, 'rule': 'out', 'call': 'tc1', 'bash_ts': T0, 'calls': ['tc1'], 'calls_certain': [True]}
            s.agents[a.id] = a
            s.spawn_cmds['tc1'] = ('BULLPEN_ROOM=%s claude -p go' % room, room)
            a.feed({'type': 'user', 'timestamp': iso(T0), 'message': {'content': 'go'}})
            a.feed({'type': 'cost-state', 'timestamp': iso(T0 + 5), 'totalDuration': 1000})
            a.feed({'type': 'user', 'timestamp': iso(T0 + 9), 'message': {'content': 'again'}})
            s.refresh_facts({a.id: 'running'})
            self.assertEqual((a.run, a.run_start, a.room_tag), (2, T0 + 9, Tag(room, None, 'command', 2)))


class LostHull(unittest.TestCase):
    """`lost` is one span: the front of a big rollout that was not read and the markdown events past the limit are covered together, from the earlier start to the end of time."""

    def agent(self):
        return AG.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})

    def md_write(self, a, i):
        a.item = None
        a.feed_cx({'ts': T0 + 600 + i, 'type': 'event_msg', 'pt': 'item_completed', 'ord': None,
                   'p': {'type': 'item_completed', 'item': {'type': 'FileChange', 'id': 'f%d' % i, 'status': 'completed', 'changes': {'/w/r1/F%d.md' % i: {'type': 'add'}}}}})

    def test_the_front_and_then_too_many_markdown_events(self):
        with mock.patch.object(AG, 'MD_EVENTS_MAX', 2):
            a = self.agent()
            a.partial = 10
            a.spawn_ts = T0 + 100
            a.feed_cx(cx_decode(line(T0 + 500, 'event_msg', {'type': 'task_started'}, 5)))
            self.assertEqual(a.lost, (T0 + 100, T0 + 500))
            for i in range(3):
                self.md_write(a, i)
            self.assertEqual(len(a.write_events), 2)
            self.assertEqual(a.lost, (T0 + 100, float('inf')))                               # the earlier start, no end

    def test_too_many_markdown_events_and_then_the_front(self):
        with mock.patch.object(AG, 'MD_EVENTS_MAX', 2):
            a = self.agent()
            a.ev.widen_lost(T0 + 700, float('inf'))                                          # (as if the limit had been passed first)
            a.partial = 10
            a.spawn_ts = T0 + 100
            a.feed_cx(cx_decode(line(T0 + 500, 'event_msg', {'type': 'task_started'}, 5)))
            self.assertEqual(a.lost, (T0 + 100, float('inf')))

    def test_a_front_that_starts_after_the_overflow_starts(self):
        with mock.patch.object(AG, 'MD_EVENTS_MAX', 1):
            a = self.agent()
            self.md_write(a, 0)
            self.md_write(a, 1)                                                              # the first one that is not kept: T0 + 601
            self.assertEqual(a.lost, (T0 + 601, float('inf')))
            a.ev.widen_lost(T0 + 50, T0 + 80)                                                # a front that came from earlier: the hull
            self.assertEqual(a.lost, (T0 + 50, float('inf')))


class Locked(unittest.TestCase):
    """The collector changes the lists in place while the judgment copies them under `ev.lock` (`debates.facts_of`): every way of changing them takes that lock."""

    def blocked(self, fn):
        """Whether `fn` waits while another thread holds the lock of the log, and goes on when it is let go."""
        done = threading.Event()
        log = AG.EventLog()

        def run():
            fn(log)
            done.set()
        log.lock.acquire()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        waited = not done.wait(0.15)
        log.lock.release()
        t.join(5)
        return waited and done.is_set()

    def test_every_change_waits_for_the_lock(self):
        planned = Planned('x', '/w/o.md', '-o', 'c', 1, 'command', T0)
        window = AG.CmdWindow('x', 'b1', T0, None, None)
        event = WriteEvent('x', '/w/a.md', T0, 'create', 'tool', True)
        for label, fn in (('add_planned', lambda log: log.add_planned(planned)), ('drop_planned', lambda log: log.drop_planned(lambda p: False)),
                          ('add_read', lambda log: log.add_read(AG.ReadEvent('x', '/w/a.md', T0, 'tool'))), ('add_window', lambda log: log.add_window(window)),
                          ('close_window', lambda log: log.close_window('b1', T0 + 1, True)), ('add_write', lambda log: log.add_write(event)),
                          ('replace_write', lambda log: log.replace_write(event, event)), ('remove_write', lambda log: log.remove_write(event)),
                          ('widen_lost', lambda log: log.widen_lost(T0, T0 + 1)), ('reset_record', lambda log: log.reset_record()), ('bump', lambda log: log.bump()),
                          ('events', lambda log: log.events())):
            self.assertTrue(self.blocked(fn), label)

    def test_a_fact_set_on_an_agent_takes_the_lock_of_its_log(self):
        a = server.Agent('a0123456789abcdef', {})
        done = threading.Event()
        a.ev.lock.acquire()
        t = threading.Thread(target=lambda: (a.set_fact('run', 4), done.set()), daemon=True)
        t.start()
        self.assertFalse(done.wait(0.15))
        a.ev.lock.release()
        t.join(5)
        self.assertEqual(a.run, 4)

    def test_a_reader_that_copies_under_the_lock_never_meets_a_list_that_is_being_changed(self):
        log = AG.EventLog()
        stop, errors = threading.Event(), []

        def write():
            i = 0
            while not stop.is_set():
                i += 1
                log.add_read(AG.ReadEvent('x', '/w/%d.md' % i, T0 + i, 'tool'))
                log.add_planned(Planned('x', '/w/%d.md' % (i % 50), '-o', 'c', i % 7, 'command', T0))
                if i % 3 == 0:
                    log.drop_planned(lambda p: p.run != i % 7)
                log.add_window(AG.CmdWindow('x', 'b%d' % i, T0 + i, None, None))
                log.close_window('b%d' % i, T0 + i + 1, True)

        t = threading.Thread(target=write, daemon=True)
        t.start()
        try:
            for _ in range(2000):
                try:
                    with log.lock:
                        tuple(log.reads), tuple(log.planned), tuple(log.windows), tuple(log.events())
                except RuntimeError as e:                                                  # "changed size during iteration"
                    errors.append(e)
        finally:
            stop.set()
            t.join(5)
        self.assertEqual(errors, [])


class RecordRead(ClaudeBase):
    """A record that is read again from its start gives the same events once, not twice; what did not come from its lines (the planned outputs) stays."""

    def feed_all(self):
        self.use('Write', {'file_path': '/x/r1/A.md'}, T0, 'w1')
        self.result('w1', T0 + 1, tur={'type': 'create'})
        self.use('Bash', {'command': 'cat r1/B.md'}, T0 + 2, 'b1')
        self.result('b1', T0 + 3)

    def test_a_claude_record_read_again(self):
        self.feed_all()
        before = (list(self.a.write_events), list(self.a.windows), list(self.a.read_events))
        self.a.reset_runs()
        self.assertEqual((self.a.write_events, list(self.a.windows), self.a.read_events), ([], [], []))
        self.feed_all()
        self.assertEqual((list(self.a.write_events), list(self.a.windows), list(self.a.read_events)), before)

    def test_the_planned_outputs_and_their_events_stay(self):
        a = PlannedCodex('test_a_turn_that_ended_well').agent()
        server.CodexLinker._plan(a)
        events = list(a.write_events)
        a.reset_runs()
        self.assertEqual((list(a.write_events), len(a.planned)), (events, 1))
        self.assertFalse(server.CodexLinker._plan(a))                                       # and they are not made a second time

    def test_what_was_lost_is_found_out_again(self):
        a = AG.CodexAgent({'id': 'X', 'path': '/x', 'cwd': '/w', 'meta_ts': T0, 'model': '', 'kind': 'root'}, {})
        a.partial = 10
        a.feed_cx(cx_decode(line(T0 + 5, 'event_msg', {'type': 'task_started'}, 1)))
        self.assertEqual(a.lost, (T0, T0 + 5))
        a.reset_runs()
        self.assertIsNone(a.lost)
        a.feed_cx(cx_decode(line(T0 + 5, 'event_msg', {'type': 'task_started'}, 1)))
        self.assertEqual(a.lost, (T0, T0 + 5))


class DebateKey(unittest.TestCase):
    """The key a judgment is kept under: the generation of the collectors' facts and what the judgment reads of an agent that is no event; none of the counts of the old lists."""

    def setUp(self):
        self.s = server.Session.__new__(server.Session)
        server.Session.__init__(self.s, '/nonexistent/x.jsonl')
        self.a = server.Agent('a0123456789abcdef', {'description': 'T1-A study'})
        self.s.agents[self.a.id] = self.a
        self.st = {self.a.id: 'running'}

    def key(self):
        return self.s._debate_key(self.st)

    def test_the_old_counts_are_not_in_it(self):
        k = self.key()
        self.a.writes.append({'ts': T0, 'path': '/w/a.md', 'id': 'x', 'ok': True})
        self.a.shell_writes.append({'ts': T0, 'paths': ['/w/a.md'], 'id': 'y', 'ok': None})
        self.a.reads['/w/b.md'] = T0
        self.a.received.append({'ts': T0, 'text': 'x'})
        self.a.out_paths.append({'ts': T0, 'path': '/w/o.md'})
        self.a.redirects.append(None)
        self.assertEqual(self.key(), k)                                                       # (what they say is in the events, and the events move `facts_gen`)

    def test_what_moves_it(self):
        moves = []

        def moved(label, fn):
            k = self.key()
            fn()
            moves.append((label, self.key() != k))

        moved('event', lambda: self.a.ev.add_write(WriteEvent(self.a.id, '/w/a.md', T0, 'create', 'tool', True)))
        moved('window', lambda: self.a.ev.add_window(AG.CmdWindow(self.a.id, 'b1', T0, None, None)))
        moved('end of the window', lambda: self.a.ev.close_window('b1', T0 + 1, True))
        moved('read', lambda: self.a.ev.add_read(AG.ReadEvent(self.a.id, '/w/b.md', T0, 'tool')))
        moved('hint', lambda: self.s._note_orch_write('/w/talk/brief.md', T0, False, 'c1', ('claude', 's', None, 'm1')))
        moved('last heard of', lambda: setattr(self.a, 'last_ts', T0 + 5))
        moved('started', lambda: setattr(self.a, 'spawn_ts', T0 - 5))
        moved('name', lambda: self.a.describe('T1-B study'))
        moved('folder', lambda: setattr(self.a, 'cwd', '/w2'))
        moved('message that failed', lambda: self.a.sent.append({'ts': T0, 'to': 'x', 'ok': False}))
        moved('message', lambda: self.a.sent.append({'ts': T0, 'to': 'x', 'ok': None}))
        moved('state', lambda: self.st.update({self.a.id: 'done'}))
        moved('walk', lambda: setattr(self.s, 'walk_gen', 3))
        moved('agent', lambda: self.s.agents.update({'a1': server.Agent('a1', {})}))
        self.assertEqual([label for label, up in moves if not up], [])

    def test_a_message_whose_result_is_known_to_have_worked_does_not_move_it(self):
        self.a.sent.append({'ts': T0, 'to': 'x', 'ok': None})
        k = self.key()
        self.a.sent[0]['ok'] = True                                                           # the judgment counts a message that did not fail as sent, as it did before the result
        self.assertEqual(self.key(), k)


class FactsGen(ClaudeBase):
    """`facts_gen` goes up when something the judgment reads changes, and stays when nothing does (J19)."""

    def gen(self):
        return self.a.facts_gen

    def test_nothing_changed_nothing_moves(self):
        self.use('Bash', {'command': 'cat > a.md <<EOF\nx\nEOF'}, T0, 'b1')
        g = self.gen()
        self.a.write_events, self.a.windows, self.a.read_events, self.a.planned, self.a.lost                    # reading moves nothing
        self.assertEqual(self.gen(), g)

    def test_what_moves_it(self):
        steps = []

        def moved(label, fn):
            g = self.gen()
            fn()
            steps.append((label, self.gen() > g))

        moved('call', lambda: self.use('Bash', {'command': 'cat > a.md <<EOF\nx\nEOF'}, T0, 'b1'))
        moved('end of the window', lambda: self.result('b1', T0 + 3))
        moved('write', lambda: self.use('Write', {'file_path': '/x/r1/A.md'}, T0 + 4, 'w1'))
        moved('result of the write', lambda: self.result('w1', T0 + 5, tur={'type': 'create'}))
        moved('read', lambda: self.use('Read', {'file_path': '/x/r1/A.md'}, T0 + 6, 'r1'))
        moved('planned', lambda: self.a.ev.add_planned(Planned('x', '/w/o.md', '-o', 'c', 1, 'command', T0)))
        moved('launch', lambda: self.a.set_fact('launch', __import__('board.facts', fromlist=['LaunchKey']).LaunchKey('claude', 's', None, 'g', 'c')))
        moved('tag', lambda: self.a.set_fact('room_tag', __import__('board.facts', fromlist=['Tag']).Tag('/r', None, 'environ', 1)))
        moved('run', lambda: self.a.set_fact('run', 2))
        moved('run start', lambda: self.a.set_fact('run_start', T0 + 9))
        moved('lost', lambda: self.a.ev.widen_lost(T0, T0 + 1))
        self.assertEqual([label for label, up in steps if not up], [])

    def test_the_end_of_a_window_that_leaves_the_count_the_same(self):
        self.use('Bash', {'command': 'ls'}, T0, 'b1')
        before = (len(self.a.windows), sum(1 for w in self.a.windows if w.t1 is None), self.gen())
        self.result('b1', T0 + 1)
        after = (len(self.a.windows), sum(1 for w in self.a.windows if w.t1 is None), self.gen())
        self.assertEqual(before[0], after[0])
        self.assertGreater(after[2], before[2])
        g = self.gen()
        self.a.ev.close_window('b1', T0 + 9, True)                                            # a window that is not open: nothing changed
        self.assertEqual(self.gen(), g)

    def test_a_value_that_is_the_same_does_not_move_it(self):
        self.a.set_fact('run', 3)
        g = self.gen()
        self.a.set_fact('run', 3)
        self.assertEqual(self.gen(), g)

    def test_windows_let_go(self):
        self.a.ev.windows_keep = 2
        for i in range(2):
            self.use('Bash', {'command': 'ls'}, T0 + i, 'b%d' % i)
        g = self.gen()
        self.use('Bash', {'command': 'ls'}, T0 + 5, 'b9')
        self.assertGreater(self.gen(), g)
        self.assertIsNotNone(self.a.windows_dropped)

    def test_the_session_side(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, '/nonexistent/x.jsonl')
        g = s.facts_gen
        s._note_orch_write('/w/talk/brief.md', T0, False, 'c1', ('claude', 's', None, 'm1'))
        self.assertGreater(s.facts_gen, g)
        g = s.facts_gen
        s._note_orch_write('/w/talk/brief.md', T0, False, 'c1', ('claude', 's', None, 'm1'))      # the same write again: nothing new
        self.assertEqual(s.facts_gen, g)
        s._note_orch_write('/w/talk/brief.md', T0, False, 'c2', ('claude', 's', None, 'm1'))      # another call, the same time: the hint changed
        self.assertGreater(s.facts_gen, g)

    def test_the_key_of_a_judgment_follows_it(self):
        s = server.Session.__new__(server.Session)
        server.Session.__init__(s, '/nonexistent/x.jsonl')
        a = server.Agent('a0123456789abcdef', {})
        s.agents[a.id] = a
        k1 = s._debate_key({a.id: 'running'})
        self.assertEqual(k1, s._debate_key({a.id: 'running'}))
        a.ev.add_window(AG.CmdWindow(a.id, 'b1', T0, None, None))
        k2 = s._debate_key({a.id: 'running'})
        self.assertNotEqual(k1, k2)
        a.ev.close_window('b1', T0 + 1, True)                                                   # the end of a window: the counts are the same, the key is not
        self.assertNotEqual(k2, s._debate_key({a.id: 'running'}))


if __name__ == '__main__':
    unittest.main()
