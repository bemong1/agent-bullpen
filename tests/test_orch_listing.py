"""The orchestrator's own successful writes name where the list of debates looks, and nothing else: no seat, no room, no member.

  - `agents.shell_mkdirs`: the folders a shell command makes
  - `units.listing_hint`: which write points at which folder
  - `units.written_debate`: which folders the list takes (the disk decides, not the name: a guide and an exact round folder, nothing a text declares)
  - `units.assign(hints=)`: the same cells, rooms and places with and without hints; only the list, the order and the time differ
  - the session: the successful writes of a Claude record (Write, Edit, MultiEdit, Bash) and of a Codex rollout (FileChange, CommandExecution that exited 0)

    python3 -m unittest tests.test_orch_listing
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
from judge_support import Fixture, agent, hint, launch, wr  # noqa: E402
from test_codex_page import line  # noqa: E402  (a function: no TestCase is imported twice)

from board import agents as AG, facts as F, units as U, views  # noqa: E402


def write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


# a brief that says who writes which result file: no folder is a debate for what its brief says (the flat review is gone), only for a guide and exact round folders
FLAT_BRIEF = '# Review\n\nReviewers and their result files: `sol.md` (reviewer sol), `opus.md` (reviewer opus).\n'
ONE_DECLARED = '# Review\n\nReviewers and their result files: `sol.md` (reviewer sol).\n'
ROUNDS_BRIEF = '# Debate\n\n**A — flow**: reads the code. **B — gate**: reads the tests.\nReports go to r1/A.md and r1/B.md.\n'


# What an orchestrator's call may hold that ends with 0 and did none of what it says of the folder of somebody else (`%s`): a skipped group, a function never called, a loop over nothing,
# a command after an `exit`, and a markdown write in those places.
NOT_RUN_BEFORE_THE_LAST = ('true || { true; mkdir -p %s/r1; }', 'f() { true; mkdir -p %s/r1; }', 'for x in; do true; mkdir -p %s/r1; done', 'exit 0; mkdir -p %s/r1', 'true || ( true; mkdir -p %s/r1 )',
                           'true || { true; echo hi > %s/brief.md; }', 'false && { true; cat > %s/brief.md <<EOF\n# x\nEOF\n}', '[ -d D ] || { true; mkdir -p %s/r1; }')


class ShellMkdirs(unittest.TestCase):
    def test_what_it_takes(self):
        for cmd, cwd, want in (('mkdir -p talk/r1', '/w', ['/w/talk/r1']), ('cd x && mkdir r1', '/w', ['/w/x/r1']), ('D=/a; mkdir -p "$D/r1"', '/w', ['/a/r1']),
                               ('mkdir -m 700 r1', '/w', ['/w/r1']), ('mkdir -- r1', '/w', ['/w/r1']), ('mkdir -p --mode=755 /abs/r1 && ls', '/w', ['/abs/r1']),
                               ('mkdir a b', '/w', ['/w/a', '/w/b']), ('sudo mkdir -p r1', '/w', ['/w/r1']), ('mkdir -pv talk/r1', '/w', ['/w/talk/r1'])):
            self.assertEqual(AG.shell_mkdirs(cmd, cwd), want, cmd)

    def test_what_it_does_not_take(self):
        for cmd in ('mkdir -p t/{r1,r2}', 'echo mkdir -p r1', 'printf "mkdir r1"', 'cat <<EOF\nmkdir -p r1\nEOF', 'mkdir -p $UNKNOWN/r1', 'mkdir r*', 'ls r1', 'python3 -c "import os; os.makedirs(\'r1\')"'):
            self.assertEqual(AG.shell_mkdirs(cmd, '/w'), [], cmd)
        self.assertEqual(AG.shell_mkdirs('mkdir r1', ''), [])                         # a relative path with no known folder


# Commands that end with 0 and have not run their last command (`%s`): it is in a group, a function or a loop that did not run, after a command that left the shell, or on a branch that was not taken.
NOT_RUN = (
    'true || { true; %s; }', 'false && { true; %s; }', '[ -d D ] && { true; %s; }', 'true || ( true; %s )', 'true || { true; { true; %s; }; }', '{ true; } || { true; %s; }',
    '[ -d D ] || ( true; true; %s )', 'f() { true; %s; }', 'f () { true; %s; }; f', 'function f { true; %s; }', 'function f() { true; %s; }', 'f()\n{\n  true\n  %s\n}', 'f()\n\n( true; %s )',
    'for x in; do true; %s; done', 'for x in $NONE; do true; %s; done', 'for x in "$@"; do true; %s; done', 'for x; do true; %s; done', 'true || for x in a b; do true; %s; done',
    'select x in a b; do true; %s; done', 'while [ -d D ]; do true; %s; done', 'until [ -d D ]; do true; %s; done', 'if [ -d D ]; then true; %s; fi',
    'if [ -d D ]; then true; else true; %s; fi', 'if [ -d D ]; then { true; %s; }; fi', 'true || if true; then true; %s; fi', 'case $x in a) true; %s ;; esac',
    'exit 0; %s', 'exit 0\n%s', 'return 0; %s', 'exec true; %s', 'true && exit 0; %s', '{ exit 0; }; %s', 'if [ -d D ]; then exit 0; fi; %s', '[ -d D ] || exit 0; %s', 'command exit 0; %s',
    'FOO=1 exit 0; %s', 'f() { exit 0; }; f; %s', 'for x in a b; do break; true; %s; done', 'for x in a b; do continue; true; %s; done', 'echo hi && ( cd /x; exit 0 ) && %s',
)
# ... and commands that did run it: a group, a subshell, a loop over words, a function that is only defined, and an `exit` that comes after it.
RAN = (
    '{ true; %s; }', '( true; %s )', 'true; { true; %s; }', 'true || { true; }; %s', 'true && { true; } ; %s', 'for t in a b; do true; %s; done', 'f() { true; }; %s', 'f() { true; }\n%s', 'f()\n{\n  true\n}\n%s',
    'function f { true; }; %s', 'if [ -d D ]; then true; fi; %s', 'if [ -d D ]; then true; else true; fi; %s', 'while false; do true; done; %s', 'case $x in a) true ;; esac; %s',
    '%s; exit 0', '%s\nexit 0', 'echo $((1+2)); %s', 'a=(1 2); %s', 'diff <(a) <(b); %s', 'echo $(true; true); %s', '( exit 0 ); %s', 'for x in a b; do true; break; done; %s', 'true; (true; true; %s)',
    'if true; then true; fi; { true; %s; }', 'for x in a b; do true; done; { true; %s; }', 'f() { return 0; }; f; %s',
)
# the last command of a template, the way a hint reads it, and what it gives for the folder `%s`
READ = (('mkdir -p %s/r1', lambda cmd: AG.shell_mkdirs(cmd, '/w'), '/w/%s/r1'),
        ('echo hi > %s/brief.md', lambda cmd: AG.shell_writes(cmd, '/w', True), '/w/%s/brief.md'))


class MayNotHaveRun(unittest.TestCase):
    """A shell command whose write or mkdir the shell may not have run is no hint: `true || mkdir -p D/r1` ends with 0 and made nothing, and D may be somebody else's debate."""

    def test_mkdir_in_a_place_that_is_sure_to_run(self):
        for cmd, want in (('mkdir -p x/r1', ['/w/x/r1']), ('cd x && mkdir r1', ['/w/x/r1']), ('mkdir -p a/r1; mkdir -p b/r1', ['/w/a/r1', '/w/b/r1']),
                          ('mkdir -p a/r1 && mkdir -p b/r1', ['/w/a/r1', '/w/b/r1']), ('mkdir -p x/r1 || true', ['/w/x/r1']), ('true || ls; mkdir -p x/r1', ['/w/x/r1']),
                          ('false\nmkdir -p x/r1', ['/w/x/r1']), ('mkdir -p x/r1 &', ['/w/x/r1']), ('for t in a b; do mkdir -p $t/r1; done', []),                  # (a command after a keyword is not read at all)
                          ('pushd d && mkdir -p x/r1', ['/w/d/x/r1'])):
            self.assertEqual(AG.shell_mkdirs(cmd, '/w'), want, cmd)

    def test_mkdir_that_may_not_have_run(self):
        for cmd in ('true || mkdir -p D/r1', '[ -d D ] || mkdir -p D/r1', 'test -d D && mkdir -p D/r1', 'echo hi && mkdir -p D/r1', 'ls D || cd x && mkdir -p D/r1',
                    'false && true; true || mkdir -p D/r1', 'if [ -d D ]; then mkdir -p D/r1; fi', 'if [ -d D ]; then true; else mkdir -p D/r1; fi',
                    'while [ -d D ]; do mkdir -p D/r1; done', 'case $x in a) mkdir -p D/r1 ;; esac', 'mkdir --help D/r1', 'mkdir --version D/r1', 'mkdir -p --help D/r1'):
            self.assertEqual(AG.shell_mkdirs(cmd, '/w'), [], cmd)

    def test_the_same_rule_for_markdown_writes_of_a_hint_and_not_for_the_writes_of_an_agent(self):
        sure = lambda cmd: AG.shell_writes(cmd, '/w', True)
        self.assertEqual(sure("cat > D/brief.md <<'EOF'\n# x\nEOF"), ['/w/D/brief.md'])
        self.assertEqual(sure('mkdir -p D && echo hi > D/brief.md'), ['/w/D/brief.md'])
        self.assertEqual(sure("cd D && cat > brief.md <<'EOF'\n# x\nEOF"), ['/w/D/brief.md'])
        for cmd in ('true || echo hi > D/brief.md', 'test -d D && echo hi > D/brief.md', 'if [ -d D ]; then echo hi > D/brief.md; fi', 'true || tee D/brief.md', 'tee --help D/brief.md',
                    'tee --version D/brief.md'):
            self.assertEqual(sure(cmd), [], cmd)
            if '--' not in cmd:
                self.assertEqual(AG.shell_writes(cmd, '/w'), ['/w/D/brief.md'], cmd)             # an agent's own writes are read as they always were

    def test_a_command_in_a_group_or_a_loop_that_did_not_run_is_no_hint(self):
        for template in NOT_RUN:
            for last, read, _path in READ:
                cmd = template % (last % 'D')
                self.assertEqual(read(cmd), [], cmd)

    def test_a_command_that_ran_is_a_hint_after_a_group_a_function_a_loop_or_before_an_exit(self):
        for template in RAN:
            for last, read, path in READ:
                cmd = template % (last % 'x')
                self.assertEqual(read(cmd), [path % 'x'], cmd)

    def test_the_writes_of_an_agent_are_read_whatever_the_shape(self):
        for template in NOT_RUN:
            cmd = template % 'echo hi > D/brief.md'
            self.assertEqual(AG.shell_writes(cmd, '/w'), ['/w/D/brief.md'], cmd)

    def test_a_shape_that_cannot_be_read_leaves_what_follows_out(self):
        for cmd in ('echo }; mkdir -p D/r1', 'echo {; mkdir -p D/r1', 'time { true; }; mkdir -p D/r1', 'for ((i=0; i<2; i++)); do true; mkdir -p D/r1; done', 'fi; mkdir -p D/r1', 'done; mkdir -p D/r1',
                    'true; ); mkdir -p D/r1', '} ; mkdir -p D/r1', 'if true; then true; done; mkdir -p D/r1', 'f() true; mkdir -p D/r1', '{ true; ); mkdir -p D/r1'):
            self.assertEqual(AG.shell_mkdirs(cmd, '/w'), [], cmd)

    def test_the_commands_of_a_pipe_and_a_list_with_semicolons_are_all_sure(self):
        self.assertEqual(AG.shell_mkdirs('ls | head; mkdir -p a/r1; ls && mkdir -p b/r1', '/w'), ['/w/a/r1'])


@unittest.skipUnless(shutil.which('bash'), 'needs bash to run the commands')
class AgainstBash(unittest.TestCase):
    """The folders a hint takes are the folders bash made: made-up commands (lists, groups, subshells, functions, loops, `if`, `case`, `exit`, `break`) are run in a folder of their own, and
    where the call ended with 0 every folder that `shell_mkdirs` names is there. (It may leave out folders that were made: a hint is a miss before it is a false link.)"""

    def command(self, rng, depth=0, loop=False, func=False, counter=None):
        counter = counter if counter is not None else [0]
        sub = lambda loop=loop, func=func: self.command(rng, depth + 1, loop, func, counter)

        def atom():
            r = rng.random()
            if r < 0.4:
                counter[0] += 1
                return 'mkdir -p d%d' % counter[0]
            return rng.choice(['true', 'false', 'true', 'exit 0', 'exit 3', '[ -d d1 ]', 'cd .', 'echo hi | cat'] + (['return 0'] if func else []) + (['break'] if loop else []))

        def item():
            if depth > 2 or rng.random() < 0.55:
                return atom()
            k = rng.randrange(10)
            return ('{ %s; }' % sub(), '( %s )' % sub(), 'if %s; then %s; fi' % (sub(), sub()), 'if %s; then %s; else %s; fi' % (sub(), sub(), sub()), 'for x in a b; do %s; done' % sub(True),
                    'for x in; do %s; done' % sub(True), 'while false; do %s; done' % sub(True), 'until false; do %s; break; done' % sub(True),
                    'f() { %s; }; %s' % (sub(False, True), rng.choice(['f', 'true', 'f && true'])), 'case %s in a) %s ;; b) %s ;; esac' % (rng.choice('ab'), sub(), sub()))[k]
        out = [item()]
        for _ in range(rng.randint(0, 2)):
            out += [rng.choice([' ; ', ' && ', ' || ', '\n']), item()]
        return ''.join(out)

    def test_what_it_names_was_made(self):
        rng, checked, hits = random.Random(11), 0, 0
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(300):
                cmd = self.command(rng)
                work = os.path.join(os.path.realpath(tmp), 'w%d' % i)
                os.makedirs(work)
                try:
                    done = subprocess.run(['bash', '-c', cmd], cwd=work, capture_output=True, timeout=5)
                except subprocess.TimeoutExpired:
                    continue
                if done.returncode:
                    continue
                checked += 1
                named = set(AG.shell_mkdirs(cmd, work))
                made = {os.path.join(work, n) for n in os.listdir(work)}
                self.assertLessEqual(named, made, cmd)
                hits += len(named)
        self.assertGreater(checked, 100)
        self.assertGreater(hits, 50)                                                                 # it is not a test of a reader that names nothing


class ListingHint(unittest.TestCase):
    def test_the_table(self):
        for path, is_dir, want in (('/d/talk/brief.md', False, '/d/talk'), ('/d/talk/README.md', False, '/d/talk'), ('/d/talk/index.md', False, '/d/talk'),
                                   ('/d/talk/r1/A.md', False, '/d/talk'), ('/d/talk/round2/B_flow.md', False, '/d/talk'), ('/d/talk/r1', True, '/d/talk'), ('/d/talk/r01', True, '/d/talk'),
                                   ('/d/talk/round3', True, '/d/talk'), ('/d/talk/x/../brief.md', False, '/d/talk')):
            self.assertEqual(U.listing_hint(path, is_dir), want, path)
        for path, is_dir in (('/d/PLAN.md', False), ('/d/notes.md', False), ('/d/talk/CLOSING.md', False), ('/d/talk/r1/notes.txt', False), ('/d/talk/r1/sub/A.md', False), ('/d/other', True),
                             ('/d/talk/r1x', True), ('/d/talk/brief.md', True), ('talk/brief.md', False), ('', False), (None, False), ('/d/talk/r1', False)):
            self.assertIsNone(U.listing_hint(path, is_dir), path)


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.cat = U.Catalog()
        self.cat.begin()

    def ok(self, folder):
        self.cat.begin()
        u = self.cat.unit_at(folder)
        return bool(u) and U.written_debate(u)

    def folder(self, name, guide, text, rounds=('r1',)):
        d = os.path.join(self.root, name)
        if guide:
            write(os.path.join(d, guide), text)
        for r in rounds:
            os.makedirs(os.path.join(d, r))
        os.makedirs(d, exist_ok=True)
        return d

    def test_what_is_listed(self):
        self.assertTrue(self.ok(self.folder('a', 'brief.md', ROUNDS_BRIEF)))                         # a guide and an exact round folder
        self.assertTrue(self.ok(self.folder('b', 'README.md', '# Notes on the debate', ('round1',))))   # a README is a guide beside a round folder, away from the top of a repository
        self.assertTrue(self.ok(self.folder('c', 'brief.md', FLAT_BRIEF, ('r1',))))                    # what the brief says of result files is not looked at: the round folder is
        self.assertTrue(self.ok(self.folder('d', 'index.md', 'x', ('r01', 'r2'))))

    def test_what_is_not(self):
        self.assertFalse(self.ok(self.folder('e', 'brief.md', ROUNDS_BRIEF, ())))                      # a brief alone, whatever it says of rounds
        self.assertFalse(self.ok(self.folder('f', 'brief.md', ONE_DECLARED, ())))                      # one declared file
        self.assertFalse(self.ok(self.folder('f2', 'brief.md', FLAT_BRIEF, ())))                       # two declared files: a guide and no round folder is no debate (the flat branch is gone)
        self.assertFalse(self.ok(self.folder('g', 'README.md', '# a note folder', ())))                # a README with no round
        self.assertFalse(self.ok(self.folder('h', None, '', ('r1',))))                                  # `mkdir r1` and no guide
        d = self.folder('later', 'brief.md', ROUNDS_BRIEF)
        import shutil
        shutil.rmtree(d)
        self.assertFalse(self.ok(d))                                                                   # it was deleted after the write

    def test_the_top_of_a_repository_and_its_docs_with_a_readme(self):
        top = self.folder('proj', 'README.md', '# the project', ('r1',))
        os.makedirs(os.path.join(top, '.git'))
        self.assertFalse(self.ok(top))
        docs = self.folder('proj/docs', 'README.md', '# docs', ('r1',))
        self.assertFalse(self.ok(docs))
        self.assertTrue(self.ok(self.folder('proj/docs/review', 'README.md', '# review', ('r1',))))     # a folder of its own below them is not the document of the repository
        top_b = self.folder('proj2', 'brief.md', ROUNDS_BRIEF, ('r1',))
        os.makedirs(os.path.join(top_b, '.git'))
        self.assertTrue(self.ok(top_b))                                                                # only a README or an index is the project's own document

    def test_the_folders_of_the_tools_and_of_this_program(self):
        state = self.folder('state/.claude/plans', 'brief.md', ROUNDS_BRIEF)
        with mock.patch.object(U, 'STATE_DIRS', (os.path.join(self.root, 'state', '.claude') + os.sep,)):
            self.assertFalse(self.ok(state))
        own = self.folder('checkout/tests/fixture', 'brief.md', ROUNDS_BRIEF)
        with mock.patch.object(U, '_OWN_TOP', [os.path.join(self.root, 'checkout')]):
            self.assertFalse(self.ok(own))
            self.assertTrue(self.ok(self.folder('elsewhere', 'brief.md', ROUNDS_BRIEF)))
        broad = self.folder('broad', 'brief.md', ROUNDS_BRIEF)
        with mock.patch.object(U, 'too_broad', lambda p: p == broad):
            self.assertFalse(self.ok(broad))

    def test_this_checkout_is_its_own_repository_when_it_has_a_git_folder(self):
        top = U.own_top()
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(top, here if os.path.exists(os.path.join(here, '.git')) else None)


class AssignWithHints(Fixture):
    """A hint lists a folder and does nothing else: the cells, the places an agent is tied to and the rooms are the same with and without it."""

    def setUp(self):
        super().setUp()
        self.sat = self.p('sat')
        self.file('sat/brief.md', ROUNDS_BRIEF)
        self.dirs('sat/r1')
        self.talk = self.p('talk')
        self.file('talk/brief.md', ROUNDS_BRIEF)
        self.dirs('talk/r1')
        self.flat = self.p('flat')
        self.file('flat/brief.md', FLAT_BRIEF)                                                          # a brief that names result files and has no round folder
        self.notes = self.p('notes')
        self.file('notes/brief.md', ROUNDS_BRIEF)                                                       # a brief alone
        self.b = agent('b', writes=[wr(self.file('sat/r1/B.md', 'x\n', 120.0), 120.0)], status='running', start=100.0, last=150.0)

    def run_assign(self, hints, **kw):
        return self.assign(self.b, hints=hints, **kw)

    def test_only_the_list_and_the_time_differ(self):
        plain = self.run_assign(None)
        hinted = self.run_assign({self.talk: hint(500.0), self.flat: hint(400.0), self.notes: hint(300.0), self.p('talk', 'nope'): hint(1.0), self.sat: hint(90.0)})
        for name in ('cells', 'placed', 'rooms', 'agent_units', 'worked', 'bound', 'assignments', 'diag', 'folded'):
            self.assertEqual(getattr(plain, name), getattr(hinted, name), name)
        self.assertEqual((plain.listed, plain.hinted), ({self.sat: 'cell'}, set()))
        self.assertEqual(hinted.listed, {self.sat: 'cell', self.talk: 'hint'})                           # the brief that stands alone, the one with no round folder and the folder that is not one stay out
        self.assertEqual(hinted.hinted, {self.talk})                                                     # only the one that is on the list for the hint alone
        self.assertEqual({p: hinted.unit_ts[p] for p in (self.talk,)}, {self.talk: 500.0})
        self.assertEqual(hinted.unit_ts[self.sat], plain.unit_ts[self.sat])                               # the later of the two times: the agent started at 100, the write was at 90
        self.assertEqual(self.run_assign({self.sat: hint(900.0)}).unit_ts[self.sat], 900.0)
        self.assertEqual(hinted.worked, {'b': {self.sat}})
        self.assertEqual((plain.order, plain.current), ([self.sat], self.sat))
        self.assertEqual((hinted.order, hinted.current), ([self.sat, self.talk], self.sat))               # a folder only the orchestrator wrote in is behind the one with an agent in it

    def test_the_call_of_a_hint_places_an_agent_launched_by_it_and_seats_nobody(self):
        c = agent('c', launch=launch('m1', 't5'), status='running', start=200.0, last=250.0)           # launched by the call that wrote the brief of talk, and has written nothing
        plain = self.assign(self.b, c, hints={self.talk: hint(150.0)})
        called = self.assign(self.b, c, hints={self.talk: hint(150.0, calls=('t5',))})
        self.assertEqual(plain.placed, {})
        self.assertEqual({k: (p.unit, p.why, p.sure) for k, p in called.placed.items()}, {'c': (self.talk, 'launch_call', False)})        # a thought place, never a sure one
        for name in ('cells', 'rooms', 'agent_units', 'assignments', 'diag'):                            # no seat, no room, no cell
            self.assertEqual(getattr(plain, name), getattr(called, name), name)
        self.assertNotIn('c', called.agent_units)

    def test_the_orchestrator_has_no_seat_and_no_room(self):
        mark = self.file('talk/r1/A.md', 'x\n', 450.0)
        hinted = self.assign(hints={self.talk: hint(500.0)}, orch=[wr(mark, 450.0)])
        self.assertEqual(hinted.rooms, {})
        self.assertEqual(hinted.assignments, [])
        self.assertNotIn(self.talk, {u for us in hinted.worked.values() for u in us})
        self.assertNotIn(self.talk, {u for us in hinted.agent_units.values() for u in us})
        (cell,) = [c for c in hinted.cells.values() if c.unit == self.talk]
        self.assertEqual((cell.owner, cell.agent, cell.editors), (None, None, ['orch']))                  # what it wrote is a file of the cell: it is an editor of it, never its owner
        self.assertEqual((hinted.listed, hinted.current), ({self.talk: 'hint'}, self.talk))              # with no debate that has an agent, the one the orchestrator made is the current one


# ---------------------------------------------------------------------------------------------------------------------
# the sessions
# ---------------------------------------------------------------------------------------------------------------------
def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(t))


def agent_that_wrote(session, work, t0, debate, rnd='r1', name='B'):
    """An agent of the page that wrote `<debate>/<rnd>/<name>.md` with a Write tool whose result said it worked (the collector makes the sure write event of it): a debate with an agent in it. It
    is started well before the orchestrator's later writes, and the file is on the disk."""
    aid = 'a%016x' % 7
    path = write(os.path.join(debate, rnd, name + '.md'), 'x\n')
    a = server.Agent(aid, {'description': 'T1-B review'})
    a.feed({'type': 'user', 'timestamp': iso(t0 + 2), 'cwd': work, 'message': {'role': 'user', 'content': 'Review the code.'}})
    a.feed({'type': 'assistant', 'timestamp': iso(t0 + 3), 'cwd': work, 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_w1', 'name': 'Write', 'input': {'file_path': path, 'content': 'x'}}]}})
    a.feed({'type': 'user', 'timestamp': iso(t0 + 4), 'cwd': work, 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_w1', 'content': 'x'}]}})
    session.agents[aid] = a
    return aid


class ClaudeRecord(unittest.TestCase):
    """The main record of a Claude orchestrator: a write counts when its result says it worked."""
    SID = 'aaaaaaaa-0000-4000-8000-0000000000a1'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.proj = os.path.join(self.root, 'claude', 'projects', 'p')
        os.makedirs(self.proj)
        self.work = os.path.join(self.root, 'work')
        self.t0 = float(int(time.time()) - 300)
        self.n = 0
        self.links = server.LinkIndex()
        self.links.ready.set()
        self.stack = patched(HOME=self.root, CLAUDE_HOME=os.path.join(self.root, 'claude'), PROJECTS=os.path.join(self.root, 'claude', 'projects'), LINKS=self.links)
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.path = os.path.join(self.proj, self.SID + '.jsonl')
        self.rows([{'type': 'user', 'timestamp': iso(self.t0), 'cwd': self.work, 'sessionId': self.SID, 'message': {'role': 'user', 'content': 'start the debate'}, 'origin': {'kind': 'human'}}])
        self.session = server.Session(self.path)

    def rows(self, rows):
        with open(self.path, 'a') as f:
            f.write('\n'.join(json.dumps(r) for r in rows) + '\n')

    def call(self, name, inp, error=None, mid=None):
        """One tool call and its result (error True: the result says it failed; None: no result yet). `mid` is the id of the message that holds the call: calls of one message are one group."""
        self.n += 1
        tid = 'toolu_%d' % self.n
        ts = self.t0 + self.n * 5
        rows = [{'type': 'assistant', 'timestamp': iso(ts), 'cwd': self.work, 'sessionId': self.SID,
                 'message': dict({'role': 'assistant', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}, **({'id': mid} if mid else {}))}]
        if error is not None:
            rows.append({'type': 'user', 'timestamp': iso(ts + 1), 'cwd': self.work, 'sessionId': self.SID,
                         'message': {'role': 'user', 'content': [dict({'type': 'tool_result', 'tool_use_id': tid, 'content': 'x'}, **({'is_error': True} if error else {}))]}})
        self.rows(rows)
        self.session.poll()
        return ts

    def hints(self):
        return sorted(self.session.orch_hints)

    def test_a_write_that_worked_points_at_its_folder(self):
        talk = os.path.join(self.root, 'talk')
        ts = self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': 'x'}, error=False)
        self.assertEqual(self.session.orch_hints, {talk: ts})
        for name, path in (('Edit', os.path.join(self.root, 'e', 'r1', 'A.md')), ('MultiEdit', os.path.join(self.root, 'm', 'README.md'))):
            self.call(name, {'file_path': path}, error=False)
        self.assertEqual(self.hints(), sorted([talk, os.path.join(self.root, 'e'), os.path.join(self.root, 'm')]))

    def test_a_hint_says_which_call_and_which_message_wrote_there(self):
        talk = os.path.join(self.root, 'talk')
        ts = self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': 'x'}, error=False, mid='msg_1')
        group = ('claude', self.SID, None, 'msg_1')
        self.assertEqual(self.session.orch_hint_facts, {talk: F.Hint(ts, frozenset({'toolu_1'}), frozenset({group}))})
        ts2 = self.call('Write', {'file_path': os.path.join(talk, 'r1', 'A.md'), 'content': 'x'}, error=False, mid='msg_2')
        self.assertEqual(self.session.orch_hint_facts, {talk: F.Hint(ts2, frozenset({'toolu_1', 'toolu_2'}), frozenset({group, ('claude', self.SID, None, 'msg_2')}))})      # the time is the last write
        self.call('Write', {'file_path': os.path.join(talk, 'r2', 'A.md'), 'content': 'x'}, error=True, mid='msg_3')                                            # a failure is no call of the hint
        self.call('Bash', {'command': 'mkdir -p other/r1'}, error=False)                                                                                       # a record with no message id has a call and no group
        self.assertEqual(self.session.orch_hint_facts[os.path.join(self.work, 'other')].calls, frozenset({'toolu_4'}))
        self.assertEqual(self.session.orch_hint_facts[os.path.join(self.work, 'other')].groups, frozenset())
        self.assertEqual(self.session.orch_hint_facts[talk].calls, frozenset({'toolu_1', 'toolu_2'}))

    def test_a_write_that_failed_or_has_no_result_points_nowhere(self):
        self.call('Write', {'file_path': os.path.join(self.root, 'f', 'brief.md'), 'content': 'x'}, error=True)
        self.call('Write', {'file_path': os.path.join(self.root, 'g', 'brief.md'), 'content': 'x'}, error=None)
        self.call('Write', {'file_path': os.path.join(self.root, 'h', 'PLAN.md'), 'content': 'x'}, error=False)       # a plan: no hint
        self.assertEqual(self.hints(), [])
        self.assertEqual(self.session.orch_hints_gen, 0)

    def test_bash_writes_and_makes_folders(self):
        talk = os.path.join(self.work, 'talk')
        self.call('Bash', {'command': 'mkdir -p talk/r1'}, error=False)
        self.call('Bash', {'command': "cat > talk/brief.md <<'EOF'\n# Debate\nEOF"}, error=False)
        self.assertEqual(self.hints(), [talk])
        self.call('Bash', {'command': 'mkdir -p other/r1'}, error=True)                                  # it failed
        self.call('Bash', {'command': 'echo "mkdir -p words/r1" && printf "talk2/brief.md"'}, error=False)    # only words
        self.call('Bash', {'command': 'mkdir -p fan/{r1,r2}'}, error=False)                              # a brace list is not read
        self.assertEqual(self.hints(), [talk])

    def test_a_mkdir_that_did_not_run_does_not_list_the_debate_of_somebody_else(self):
        theirs = os.path.join(self.root, 'theirs')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        for cmd in ('true || mkdir -p %s/r1' % theirs, 'mkdir --help %s/r1' % theirs, '[ -d %s ] || mkdir -p %s/r1' % (theirs, theirs), 'if [ -d %s ]; then mkdir -p %s/r1; fi' % (theirs, theirs)):
            self.call('Bash', {'command': cmd}, error=False)
        self.assertEqual(self.hints(), [])
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [])
        self.call('Bash', {'command': 'mkdir -p %s/r1' % theirs}, error=False)                           # run for real, it is the orchestrator's write
        self.assertEqual(self.hints(), [theirs])

    def test_a_group_a_function_or_a_loop_that_did_not_run_does_not_list_the_debate_of_somebody_else(self):
        theirs = os.path.join(self.root, 'theirs')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        for cmd in NOT_RUN_BEFORE_THE_LAST:
            self.call('Bash', {'command': cmd % theirs}, error=False)                                  # each of them ends with 0
        self.assertEqual(self.hints(), [])
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [])
        self.call('Bash', {'command': '{ true; mkdir -p %s/r1; }' % theirs}, error=False)               # the same group run for real is the orchestrator's write
        self.assertEqual(self.hints(), [theirs])

    def test_a_late_result_of_an_old_write_does_not_push_out_a_newer_one(self):
        from board import sessions
        with mock.patch.object(sessions, 'ORCH_HINTS_MAX', 3):
            old_ts = self.call('Write', {'file_path': os.path.join(self.root, 'old', 'brief.md'), 'content': 'x'}, error=None)               # no result yet
            old_id = 'toolu_%d' % self.n
            for name in ('n1', 'n2', 'n3'):
                self.call('Write', {'file_path': os.path.join(self.root, name, 'brief.md'), 'content': 'x'}, error=False)
            self.rows([{'type': 'user', 'timestamp': iso(old_ts + 600), 'cwd': self.work, 'sessionId': self.SID,
                        'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': old_id, 'content': 'x'}]}}])
            self.session.poll()
        self.assertEqual(self.hints(), sorted(os.path.join(self.root, n) for n in ('n1', 'n2', 'n3')))      # the one written longest ago is the one let go, whenever its result came

    def test_a_folder_is_listed_once_the_disk_says_it_is_a_debate(self):
        talk = os.path.join(self.root, 'talk')
        write(os.path.join(talk, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(talk, 'r1'))
        self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': ROUNDS_BRIEF}, error=False)
        st = views.state(self.session)
        self.assertEqual([t['dir'] for d in st['debates'] for t in d['topics']], [talk])
        self.assertEqual(st['agents'], [])                                                                # the orchestrator sits nowhere
        # a brief alone is no debate, even written by the orchestrator
        alone = os.path.join(self.root, 'alone')
        write(os.path.join(alone, 'brief.md'), ROUNDS_BRIEF)
        self.call('Write', {'file_path': os.path.join(alone, 'brief.md'), 'content': ROUNDS_BRIEF}, error=False)
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [talk])

    def with_an_agent_in(self, debate):
        """An agent of the page that wrote r1/B.md of `debate` (a debate with an agent in it), started well before the orchestrator's later writes."""
        return agent_that_wrote(self.session, self.work, self.t0, debate)

    def test_a_debate_only_the_orchestrator_wrote_never_takes_the_place_of_one_with_agents(self):
        theirs = os.path.join(self.root, 'migration')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        aid = self.with_an_agent_in(theirs)
        scratch = os.path.join(self.root, 'scratch', 'trial')
        write(os.path.join(scratch, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(scratch, 'r1'))
        self.call('Write', {'file_path': os.path.join(scratch, 'brief.md'), 'content': ROUNDS_BRIEF}, error=False)       # later than the agent started
        debates = views.state(self.session)['debates']
        self.assertEqual([(d['root'], d['current']) for d in debates], [(theirs, True), (scratch, False)])
        self.assertEqual([(t['dir'], [c['agent'] for r in t['rows'] for c in r['cells'] if c['agent']]) for d in debates for t in d['topics']], [(theirs, [aid]), (scratch, [])])
        # with no debate that has an agent, the one the orchestrator made is the current one
        self.session.agents.clear()
        self.assertEqual([(d['root'], d['current']) for d in views.state(self.session)['debates']], [(scratch, True)])

    def test_a_debate_the_walk_found_with_no_agent_in_it_does_not_take_the_place_of_one_with_agents_either(self):
        theirs = os.path.join(self.root, 'migration')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        aid = self.with_an_agent_in(theirs)
        old = os.path.join(self.root, 'scratch', 'old-review')                                              # on the list by the walk of the repository: nothing is hinted
        write(os.path.join(old, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(old, 'r1'))
        self.session.walked_units = [old]
        order = lambda: [(d['root'], d['current']) for d in views.state(self.session)['debates']]
        self.assertEqual(order(), [(theirs, True), (old, False)])
        self.call('Write', {'file_path': os.path.join(old, 'brief.md'), 'content': ROUNDS_BRIEF}, error=False)       # the orchestrator writes there, later than the agent started
        self.assertEqual(order(), [(theirs, True), (old, False)])
        self.assertEqual([(t['dir'], [c['agent'] for r in t['rows'] for c in r['cells'] if c['agent']]) for d in views.state(self.session)['debates'] for t in d['topics']], [(theirs, [aid]), (old, [])])
        # a debate that is on the list for both reasons, the walk and the write, is behind the one that is on it for the walk only
        hint = os.path.join(self.root, 'scratch', 'trial')
        write(os.path.join(hint, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(hint, 'r1'))
        self.call('Write', {'file_path': os.path.join(hint, 'brief.md'), 'content': ROUNDS_BRIEF}, error=False)
        self.assertEqual(order(), [(theirs, True), (old, False), (hint, False)])
        # with no debate that has an agent, the order is what it was: the walk's before the hint's, the later write first among those of the same kind
        self.session.agents.clear()
        self.assertEqual(order(), [(old, True), (hint, False)])

    def test_the_group_of_a_debate_the_orchestrator_wrote_shows_only_what_the_gate_let_through(self):
        grp = os.path.join(self.root, 'scratch', 'grp')
        write(os.path.join(grp, 'brief.md'), '# Notes of the work\n\nThree topics below.\n')
        write(os.path.join(grp, 't1', 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(grp, 't1', 'r1'))
        write(os.path.join(grp, 't2', 'brief.md'), ROUNDS_BRIEF)                                           # nobody wrote it, and it is a brief alone
        write(os.path.join(grp, 't3', 'brief.md'), ROUNDS_BRIEF)
        self.call('Write', {'file_path': os.path.join(grp, 't1', 'brief.md'), 'content': ROUNDS_BRIEF}, error=False)
        debates = views.state(self.session)['debates']
        self.assertEqual([(d['root'], [t['dir'] for t in d['topics']]) for d in debates], [(grp, [os.path.join(grp, 't1')])])

    def test_a_new_hint_makes_the_judgment_again(self):
        talk = os.path.join(self.root, 'talk')
        write(os.path.join(talk, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(talk, 'r1'))
        views.state(self.session)
        before = self.session._debate_key({})
        self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': 'x'}, error=False)
        self.assertNotEqual(self.session._debate_key({}), before)
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [talk])

    def test_the_most_recent_folders_are_kept_and_the_limit_is_said(self):
        from board import sessions
        with mock.patch.object(sessions, 'ORCH_HINTS_MAX', 3):
            for i in range(5):
                self.call('Write', {'file_path': os.path.join(self.root, 'd%d' % i, 'brief.md'), 'content': 'x'}, error=False)
        self.assertEqual(self.hints(), sorted(os.path.join(self.root, 'd%d' % i) for i in (2, 3, 4)))
        self.assertTrue(self.session.orch_hints_dropped)
        st = views.state(self.session)
        self.assertEqual([(x['code'], x['scope'], x['params']) for x in self.session._diag if x['code'] == 'listing_capped'], [('listing_capped', 'session', {'detail': 'writes'})])
        self.assertEqual(st['diag']['by_code'].get('listing_capped'), 1)

    def test_the_time_is_the_last_write(self):
        talk = os.path.join(self.root, 'talk')
        self.call('Write', {'file_path': os.path.join(talk, 'brief.md'), 'content': 'x'}, error=False)
        ts = self.call('Write', {'file_path': os.path.join(talk, 'r1', 'A.md'), 'content': 'x'}, error=False)
        self.assertEqual(self.session.orch_hints, {talk: ts})


class CodexRecord(unittest.TestCase):
    """The rollout of a Codex orchestrator: a FileChange that completed (an added or changed file) and a command that exited 0."""
    R = '019c0000-0000-7000-8000-00000000000a'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.work = os.path.join(self.root, 'work')
        os.makedirs(self.work)
        self.t0 = float(int(time.time()) - 300)
        day = os.path.join(self.root, '.codex', 'sessions', '2026', '10', '04')
        os.makedirs(day)
        self.path = os.path.join(day, 'rollout-2026-10-04T00-00-00-%s.jsonl' % self.R)
        meta = {'id': self.R, 'session_id': self.R, 'timestamp': iso(self.t0), 'cwd': self.work, 'originator': 'codex-tui', 'source': 'cli'}
        self.lines = [line(self.t0, 'session_meta', meta, 0), line(self.t0 + 1, 'event_msg', {'type': 'task_started'}, 1)]
        self.n = 1
        self.flush()
        self.links, self.index = server.LinkIndex(), server.CodexIndex()
        self.links.ready.set()
        self.stack = patched(HOME=self.root, CLAUDE_HOME=os.path.join(self.root, '.claude'), PROJECTS=os.path.join(self.root, '.claude', 'projects'),
                             CODEX_HOME=os.path.join(self.root, '.codex'), CODEX_SESSIONS=os.path.join(self.root, '.codex', 'sessions'),
                             CODEX_NAMES=os.path.join(self.root, '.codex', 'session_index.jsonl'), CODEX=self.index, LINKS=self.links)
        self.stack.__enter__()
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.index.refresh(force=True)
        self.session = server.CodexSession(self.index.get(self.R))
        self.session.poll()

    def flush(self):
        with open(self.path, 'wb') as f:
            f.write(b'\n'.join(self.lines) + b'\n')

    def item(self, item):
        self.n += 1
        self.lines.append(line(self.t0 + 10 + self.n, 'event_msg', {'type': 'item_completed', 'thread_id': self.R, 'turn_id': 'turn-1', 'item': item}, self.n))
        self.flush()
        self.index.refresh(force=True)
        self.session.poll()

    def file_change(self, changes, status='completed'):
        self.item({'type': 'FileChange', 'id': 'fc_%d' % self.n, 'changes': changes, 'status': status, 'stdout': '', 'stderr': ''})

    def command(self, cmd, exit_code=0, status='completed'):
        self.item({'type': 'CommandExecution', 'id': 'item_%d' % self.n, 'process_id': str(5000 + self.n), 'command': ['/bin/bash', '-lc', cmd], 'cwd': 'file://' + self.work,
                   'parsed_cmd': [{'type': 'unknown', 'cmd': cmd}], 'source': 'unified_exec_startup', 'status': status, 'stdout': '', 'stderr': '', 'aggregated_output': '',
                   'exit_code': exit_code, 'duration': {'secs': 0, 'nanos': 1000000}, 'formatted_output': ''})

    def hints(self):
        return sorted(self.session.orch_hints)

    def test_a_file_change_that_completed(self):
        talk = os.path.join(self.root, 'talk')
        self.file_change({os.path.join(talk, 'brief.md'): {'type': 'add', 'content': 'x'}, os.path.join(self.root, 'notes', 'PLAN.md'): {'type': 'add', 'content': 'x'}})
        self.assertEqual(self.hints(), [talk])
        self.file_change({os.path.join(talk, 'r1', 'A.md'): {'type': 'update', 'unified_diff': '@@'}})
        self.assertEqual(self.hints(), [talk])

    def test_a_hint_says_which_call_wrote_there(self):
        talk = os.path.join(self.root, 'talk')
        self.file_change({os.path.join(talk, 'brief.md'): {'type': 'add', 'content': 'x'}})
        self.command('mkdir -p talk/r1')
        got = self.session.orch_hint_facts
        self.assertEqual(got[talk].calls, frozenset({'fc_1'}))
        self.assertEqual(got[talk].groups, frozenset({('codex', self.R, None, 'fc_1')}))                  # a call of its own thread is its own group
        self.assertEqual((got[os.path.join(self.work, 'talk')].calls, got[os.path.join(self.work, 'talk')].groups),
                         (frozenset({'item_2'}), frozenset({('codex', self.R, None, 'item_2')})))

    def test_a_delete_a_move_and_a_change_that_did_not_complete(self):
        self.file_change({os.path.join(self.root, 'gone', 'brief.md'): {'type': 'delete'}})
        self.file_change({os.path.join(self.root, 'half', 'brief.md'): {'type': 'add', 'content': 'x'}}, status='failed')
        self.assertEqual(self.hints(), [])
        moved = os.path.join(self.root, 'moved')
        self.file_change({os.path.join(self.root, 'old', 'brief.md'): {'type': 'update', 'move_path': os.path.join(moved, 'brief.md')}})
        self.assertEqual(self.hints(), [moved])                                                           # the folder it is in now

    def test_a_command_that_exited_zero_and_one_that_did_not(self):
        talk = os.path.join(self.work, 'talk')
        self.command('mkdir -p talk/r1')
        self.assertEqual(self.hints(), [talk])
        self.command('mkdir -p other/r1', exit_code=1)
        self.command('mkdir -p again/r1', status='failed')
        self.command('echo "mkdir -p words/r1"')
        self.assertEqual(self.hints(), [talk])
        self.command("cat > talk2/brief.md <<'EOF'\n# x\nEOF")
        self.assertEqual(self.hints(), sorted([talk, os.path.join(self.work, 'talk2')]))

    def test_a_command_that_ran_nothing_does_not_list_the_debate_of_somebody_else(self):
        theirs = os.path.join(self.root, 'theirs')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        for cmd in ('true || mkdir -p %s/r1' % theirs, 'mkdir --help %s/r1' % theirs, 'test -d %s && mkdir -p %s/r1' % (theirs, theirs)):
            self.command(cmd)                                                                          # each of them exits 0
        self.assertEqual(self.hints(), [])
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [])
        self.command('mkdir -p %s/r1' % theirs)
        self.assertEqual(self.hints(), [theirs])

    def test_a_group_a_function_or_a_loop_that_did_not_run_does_not_list_the_debate_of_somebody_else(self):
        theirs = os.path.join(self.root, 'theirs')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        for cmd in NOT_RUN_BEFORE_THE_LAST:
            self.command(cmd % theirs)                                                                 # each of them exits 0
        self.assertEqual(self.hints(), [])
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [])
        self.command('{ true; mkdir -p %s/r1; }' % theirs)                                              # the same group run for real is the orchestrator's write
        self.assertEqual(self.hints(), [theirs])

    def test_a_debate_the_walk_found_with_no_agent_in_it_does_not_take_the_place_of_one_with_agents_either(self):
        theirs = os.path.join(self.root, 'migration')
        write(os.path.join(theirs, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(theirs, 'r1'))
        agent_that_wrote(self.session, self.work, self.t0, theirs)
        old = os.path.join(self.root, 'scratch', 'old-review')
        write(os.path.join(old, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(old, 'r1'))
        self.session.walked_units = [old]
        self.file_change({os.path.join(old, 'brief.md'): {'type': 'update', 'unified_diff': '@@'}})
        self.assertEqual([(d['root'], d['current']) for d in views.state(self.session)['debates']], [(theirs, True), (old, False)])

    def test_the_latest_sixty_four_are_kept_whatever_the_order_the_record_is_read_in(self):
        # the record is read at once: its file changes first, its commands after. 65 old mkdirs, then 64 newer file changes
        for i in range(65):
            self.n += 1
            self.lines.append(line(self.t0 + 10 + self.n, 'event_msg', {'type': 'item_completed', 'thread_id': self.R, 'turn_id': 'turn-1', 'item': {
                'type': 'CommandExecution', 'id': 'item_%d' % self.n, 'process_id': str(7000 + self.n), 'command': ['/bin/bash', '-lc', 'mkdir -p old%d/r1' % i],
                'cwd': 'file://' + self.work, 'parsed_cmd': [], 'source': 'unified_exec_startup', 'status': 'completed', 'stdout': '', 'stderr': '', 'aggregated_output': '',
                'exit_code': 0, 'duration': {'secs': 0, 'nanos': 1000000}, 'formatted_output': ''}}, self.n))
        for i in range(64):
            self.n += 1
            self.lines.append(line(self.t0 + 10 + self.n, 'event_msg', {'type': 'item_completed', 'thread_id': self.R, 'turn_id': 'turn-1', 'item': {
                'type': 'FileChange', 'id': 'fc_%d' % self.n, 'changes': {os.path.join(self.root, 'new%d' % i, 'brief.md'): {'type': 'add', 'content': 'x'}}, 'status': 'completed',
                'stdout': '', 'stderr': ''}}, self.n))
        self.flush()
        self.index.refresh(force=True)
        fresh = server.CodexSession(self.index.get(self.R))
        fresh.poll()
        fresh.poll()
        self.assertEqual(sorted(fresh.orch_hints), sorted(os.path.join(self.root, 'new%d' % i) for i in range(64)))
        self.assertTrue(fresh.orch_hints_dropped)

    def test_a_file_change_over_a_megabyte_is_read(self):
        """The item of a file change or of a command is read in full however long its line is (up to 64 MB): what it wrote counts."""
        talk = os.path.join(self.root, 'talk')
        self.file_change({os.path.join(talk, 'brief.md'): {'type': 'add', 'content': 'x' * (1 << 20 + 1)}})
        self.assertEqual(self.hints(), [talk])

    def test_a_line_over_the_limit_is_not_read(self):
        from board import codex_parse
        talk = os.path.join(self.root, 'talk')
        with mock.patch.object(codex_parse, 'CX_ITEM_MAX', 3 << 20):                                       # (the stand-in of 64 MB)
            self.file_change({os.path.join(talk, 'brief.md'): {'type': 'add', 'content': 'x' * (4 << 20)}})
        self.assertEqual(self.hints(), [])

    def test_the_folder_is_listed_when_it_is_a_debate(self):
        talk = os.path.join(self.root, 'talk')
        write(os.path.join(talk, 'brief.md'), ROUNDS_BRIEF)
        os.makedirs(os.path.join(talk, 'r1'))
        self.file_change({os.path.join(talk, 'brief.md'): {'type': 'add', 'content': ROUNDS_BRIEF}})
        self.assertEqual([t['dir'] for d in views.state(self.session)['debates'] for t in d['topics']], [talk])


if __name__ == '__main__':
    unittest.main()
