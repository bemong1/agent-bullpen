"""Tests for detached children (linked through environment variables), several sessions launched from one loop, and the status of grandchild agents.

Environment variables: a detached `claude -p` or `codex` has init (1) as its parent, so it cannot be found through its ancestors, but its environ holds the id of the session that launched it (CLAUDE_CODE_SESSION_ID) and its pid (CLAUDE_PID).
Because environ holds secrets, the tests also check that only those two values are read and that nothing is kept.

    python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
from test_stage2 import T0, bash_line, child_lines, dump, iso  # noqa: E402
from test_proclink import C, D, P, Q, TID, TID2, UID, Fixture  # noqa: E402,F401  (Fixture has no tests)

from board import link, lineage, procs  # noqa: E402

SECRETS = ('sk-ant-SECRETVALUE123', 'ghp_SECRETTOKEN456', 'oauth-SECRET789')
E = 'CLAUDE_CODE_SESSION_ID'


def set_env(fp, pid, sid=None, cpid=None, extra=True):
    """Fake /proc/<pid>/environ: the session id and pid, plus other values that hold secrets."""
    items = ['HOME=/home/u', 'ANTHROPIC_API_KEY=%s' % SECRETS[0], 'GITHUB_TOKEN=%s' % SECRETS[1]] if extra else []
    if sid is not None:
        items.append('%s=%s' % (E, sid))
    if cpid is not None:
        items.append('CLAUDE_PID=%s' % cpid)
    items.append('CLAUDE_CODE_OAUTH_TOKEN=%s' % SECRETS[2])
    with open(os.path.join(fp.root, str(pid), 'environ'), 'wb') as f:
        f.write(b'\0'.join(i.encode() for i in items) + b'\0')


class EnvPrimitives(unittest.TestCase):
    def test_real_process_environ_only_requested_keys(self):
        env = {'PATH': os.environ.get('PATH', ''), 'SECRET_TOKEN': SECRETS[0], E: P, 'CLAUDE_PID': '123', 'XCLAUDE_PID': '9', 'EMPTY': ''}
        p = subprocess.Popen([sys.executable, '-c', 'import sys, time; print("ready", flush=True); time.sleep(30)'], env=env, stdout=subprocess.PIPE)
        try:
            p.stdout.readline()                                                  # read after exec has finished and the environment is in place
            got = procs.env_values(p.pid, (E.encode(), b'CLAUDE_PID', b'NOT_THERE'))
            self.assertEqual(got, {E: P, 'CLAUDE_PID': '123'})                   # only the requested names, not a name that is the same apart from a prefix (XCLAUDE_PID)
            self.assertNotIn(SECRETS[0], repr(got))
        finally:
            p.kill()
            p.wait()
            p.stdout.close()
        self.assertIsNone(procs.env_values(p.pid, (E.encode(),)))                # a finished process is unknown

    def test_unreadable_is_unknown(self):
        for bad in (None, -1, 0, True, 'x', 10 ** 9):
            self.assertIsNone(procs.env_values(bad, (b'A',)))
        self.assertIsNone(procs.env_values(1, (b'A',)))                          # init's environ cannot be read (another user)

    def test_pick_env_anchoring(self):
        self.assertEqual(procs._pick_env(b'A=1\0CLAUDE_PID=7\0XCLAUDE_PID=8\0', (b'CLAUDE_PID',), True), {'CLAUDE_PID': '7'})
        self.assertEqual(procs._pick_env(b'CLAUDE_PID=7\0', (b'CLAUDE_PID',), True), {'CLAUDE_PID': '7'})
        self.assertEqual(procs._pick_env(b'A=B_CLAUDE_PID=7\0', (b'CLAUDE_PID',), True), {})
        self.assertEqual(procs._pick_env(b' FOO=bar CLAUDE_PID=9 X_CLAUDE_PID=1', (b'CLAUDE_PID',), False), {'CLAUDE_PID': '9'})

    def test_pick_env_from_ps_needs_a_value_of_the_right_shape_and_one_value_only(self):
        """`ps -E` separates the variables by blanks, so a value with blanks in it can hold ` NAME=value` text: only an id or a number that ends at a blank counts, and
        a name that stands twice with different values is unknown."""
        e, other = E.encode(), Q.encode()
        pick = lambda raw, *names: procs._pick_env(raw, names, False)
        self.assertEqual(pick(b'HOME=/h %s=%s X=1' % (e, other), e), {E: Q})
        self.assertEqual(pick(b'NOTE=see %s=%s here %s=%s' % (e, other, e, P.encode()), e), {})           # the same name, two values: unknown
        self.assertEqual(pick(b'NOTE=see %s=%s here %s=%s' % (e, other, e, other), e), {E: Q})            # twice, the same value
        self.assertEqual(pick(b'HOME=/h %s=not-an-id' % e, e), {})
        self.assertEqual(pick(b'HOME=/h %s=%s,more' % (e, other), e), {})                                  # the value must end at a blank
        self.assertEqual(pick(b'HOME=/h CLAUDE_PID=12x', b'CLAUDE_PID'), {})
        self.assertEqual(pick(b'HOME=/h CLAUDE_PID=1234 %s=%s' % (e, other), b'CLAUDE_PID', e), {'CLAUDE_PID': '1234', E: Q})
        self.assertEqual(procs._pick_env(b'A=1\0CLAUDE_PID=7\0CLAUDE_PID=8\0', (b'CLAUDE_PID',), True), {})
        self.assertEqual(procs._pick_env(b'A=1\0CLAUDE_PID=7\0CLAUDE_PID=7\0', (b'CLAUDE_PID',), True), {'CLAUDE_PID': '7'})

    def test_ps_fallback(self):
        cmd = b'claude -p fix CLAUDE_CODE_SESSION_ID=%s' % P.encode()           # the same text inside the arguments is not environment
        calls = []

        def run(argv, **kw):
            calls.append(tuple(argv))
            if tuple(argv[:-1]) == procs.PS_ENV_ARGV:
                return types.SimpleNamespace(returncode=0, stdout=cmd + b' HOME=/h SECRET=%s CLAUDE_PID=100 %s=%s\n' % (SECRETS[0].encode(), E.encode(), Q.encode()))
            return types.SimpleNamespace(returncode=0, stdout=b'  77 %s\n' % cmd)
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            procs.reset()
            got = procs.env_values(77, (E.encode(), b'CLAUDE_PID'))
            self.assertEqual(got, {E: Q, 'CLAUDE_PID': '100'})                   # the later environment (Q), not the text in the earlier arguments (P)
            n = len(calls)
            self.assertEqual(procs.env_values(77, (E.encode(), b'CLAUDE_PID')), got)
            self.assertEqual(len(calls), n)                                      # 3-second cache
            self.assertNotIn(SECRETS[0], repr(procs._ENV))                       # values that were not requested are not in the cache either
            self.assertIsNone(procs.env_values(78, (E.encode(),)))               # a process that is not in the ps table
        # unknown (None, never `{}` = "read, and the name is absent") if the environment is not printed (another user's process: macOS ps shows only the command), if the part before it
        # differs so it cannot be split off, or if ps fails
        for out in (cmd, cmd + b' ', b'something else entirely CLAUDE_PID=1', None):
            def run2(argv, out=out, **kw):
                if tuple(argv[:-1]) == procs.PS_ENV_ARGV:
                    if out is None:
                        raise OSError('no ps')
                    return types.SimpleNamespace(returncode=0, stdout=out + b'\n')
                return types.SimpleNamespace(returncode=0, stdout=b'  77 %s\n' % cmd)
            with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run2):
                procs.reset()
                self.assertIsNone(procs.env_values(77, (E.encode(),)), out)
        procs.reset()

    def test_ps_environment_without_the_name_is_empty_but_known(self):
        """The environment was printed and the name is not in it: `{}`, unlike the environment of another user's process, which ps does not print (None)."""
        cmd = b'claude -p fix'

        def run(argv, **kw):
            if tuple(argv[:-1]) == procs.PS_ENV_ARGV:
                return types.SimpleNamespace(returncode=0, stdout=cmd + b' HOME=/h PATH=/bin\n')
            return types.SimpleNamespace(returncode=0, stdout=b'  77 %s\n' % cmd)
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            procs.reset()
            self.assertEqual(procs.env_values(77, (E.encode(),)), {})
        procs.reset()


class EnvLineage(Fixture):
    """A detached child: the ancestor chain is cut at init (1) and only the environment points to the parent."""

    def detached(self, sid=P, cpid=100, argv=None, entrypoint='sdk-cli', **kw):
        self.proc.add(100, 1, ['claude', '--dangerously-skip-permissions'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.add(103, 1, argv or ['claude', '-p', 'hello', '--model', 'opus'])          # setsid nohup … & : the parent is init
        self.session_file(103, C, entrypoint=entrypoint)
        set_env(self.proc, 103, sid, cpid, **kw)

    def owners_cli(self):
        return self.scan().cli_owners

    def test_detached_child_linked_by_environment(self):
        self.detached()
        self.parent()
        self.child()
        o = self.owners_cli()[C]
        self.assertEqual((o['sid'], o['call'], o['bash_desc']), (P, None, ''))
        self.assertEqual(o['bash_ts'], T0 + 1)
        self.assertEqual(self.links.cli_count(P), 1)

    def test_without_environment_a_detached_child_is_not_linked(self):
        self.detached()
        os.unlink(os.path.join(self.proc.root, '103', 'environ'))                # cannot be read (another user, permissions)
        self.parent()
        self.child()
        self.assertNotIn(C, self.owners_cli())
        set_env(self.proc, 103, None, None)                                      # the environment was read but there is no session id
        self.assertNotIn(C, self.owners_cli())

    def test_own_session_id_is_ignored(self):
        self.detached(sid=C)                                                     # `claude -p --resume <itself>`: its own session
        self.parent()
        self.child()
        self.assertNotIn(C, self.owners_cli())

    def test_bad_values_are_ignored(self):
        self.detached()
        self.parent()
        self.child()
        self.assertEqual(self.owners_cli()[C]['sid'], P)                         # for comparison
        self.links.lineage = lineage.Lineage()
        for sid in ('not-a-uuid', '../../x', '', P + 'x', P[:-1] + 'G', ' ' + P, '%s %s' % (P, Q)):
            set_env(self.proc, 103, sid, 100)
            self.links.lineage = lineage.Lineage()
            self.links.cli_owners = {}
            self.assertNotIn(C, self.owners_cli(), repr(sid))

    def test_claude_pid_cross_check(self):
        self.parent()
        self.parent(Q)
        self.child()
        self.detached()
        self.session_file(100, Q, entrypoint='cli')                              # the process of CLAUDE_PID is now a different session (the session was switched)
        self.assertNotIn(C, self.owners_cli())
        self.session_file(100, P, entrypoint='cli')                              # if they match, the link holds
        self.assertEqual(self.owners_cli()[C]['sid'], P)

    def test_parent_process_already_gone(self):
        self.detached(cpid=424242)                                               # there is no process for CLAUDE_PID (the parent ended first): only the session id is trusted
        self.parent()
        self.child()
        self.assertEqual(self.owners_cli()[C]['sid'], P)

    def test_claude_pid_missing_still_links(self):
        self.detached(cpid=None)
        self.parent()
        self.child()
        self.assertEqual(self.owners_cli()[C]['sid'], P)

    def test_environment_needs_records_of_both(self):
        self.detached()
        self.child()
        self.assertNotIn(C, self.owners_cli())                                   # no parent transcript
        os.unlink(os.path.join(self.proj, C + '.jsonl'))
        self.parent()
        self.assertNotIn(C, self.owners_cli())                                   # no child transcript

    def test_environment_beats_ancestors_and_falls_back(self):
        """The ancestor chain points to Q but the environment says P: the environment wins. If the environment's session has no transcript, fall back to the ancestors."""
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.add(110, 1, ['claude', '--resume'])
        self.session_file(110, Q, entrypoint='cli')
        self.proc.add(111, 110, ['/bin/bash', 'x.sh'])
        self.proc.add(112, 111, ['claude', '-p', 'x'])
        self.session_file(112, C)
        set_env(self.proc, 112, P, 100)
        self.parent()
        self.parent(Q)
        self.child()
        self.assertEqual(self.owners_cli()[C]['sid'], P)
        os.unlink(os.path.join(self.proj, P + '.jsonl'))
        self.links.lineage = lineage.Lineage()
        self.assertEqual(self.owners_cli()[C]['sid'], Q)

    def test_environment_beats_time_rule(self):
        self.parent(Q, [bash_line(T0, 'claude -p hi', '/w', tid='toolu_q')])      # the literal argument equals the child's instruction (a different one would veto the call)
        self.parent()
        self.child()
        self.assertEqual(self.owners_cli()[C]['sid'], Q)                         # for comparison: without the environment the time rule applies
        self.detached()
        self.assertEqual(self.owners_cli()[C]['sid'], P)

    def test_interactive_process_with_environment_is_not_a_sub_agent(self):
        self.detached(argv=['claude', '--resume'], entrypoint='cli')              # an interactive claude the user opened in something like tmux
        self.parent()
        self.child()
        self.assertNotIn(C, self.owners_cli())

    def test_remembered_and_not_written(self):
        self.detached()
        self.parent()
        self.child()
        self.assertIn(C, self.owners_cli())
        for pid in (100, 103):
            self.proc.remove(pid)
        for f in os.listdir(self.sess_dir):
            os.unlink(os.path.join(self.sess_dir, f))
        self.assertEqual(self.owners_cli()[C]['sid'], P)
        self.assertEqual(sorted(os.listdir(self.home)), ['.claude', '.codex'])

    def test_detached_codex_linked_by_environment(self):
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.parent()
        path = self.rollout()
        self.codex_proc(104, 1, path)                                            # a codex exec detached with setsid
        set_env(self.proc, 104, P, 100)
        o = self.scan().owners[TID]
        self.assertEqual((o['sid'], o['rule'], o['call']), (P, 'env', None))
        for kw in ({'origin': 'codex-tui'}, {'parent': 'x-parent'}):             # interactive and guardian ones are not linked even when there is an environment
            self.proc.remove(104)
            os.unlink(path)
            path = self.rollout(**kw)
            self.codex_proc(104, 1, path)
            set_env(self.proc, 104, P, 100)
            self.assertNotIn(TID, self.scan().owners, kw)

    def test_detached_codex_without_or_with_bad_environment(self):
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.parent()
        path = self.rollout()
        self.codex_proc(104, 1, path)
        self.assertNotIn(TID, self.scan().owners)                                # the environment could not be read
        set_env(self.proc, 104, Q, 100)                                          # CLAUDE_PID (100) is now P but the id is Q: mismatch
        self.parent(Q)
        self.assertNotIn(TID, self.scan().owners)

    def test_codex_environment_beats_prompt_rule(self):
        prompt = 'Investigate the failing build and write a short report about what you found, thanks'
        self.parent(Q, [bash_line(T0, 'cd /w && codex exec -m x "%s"' % prompt, '/w', tid='toolu_c')])
        self.parent()
        path = self.rollout(user=prompt)
        self.assertEqual(self.scan().owners[TID]['sid'], Q)                      # for comparison
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.codex_proc(104, 1, path)
        set_env(self.proc, 104, P, 100)
        self.assertEqual(self.scan().owners[TID]['sid'], P)

    def test_secrets_are_never_kept_or_printed(self):
        self.detached()
        self.proc.add(104, 1, ['codex', 'exec', 'x'], fds=[self.rollout()])
        set_env(self.proc, 104, P, 100)
        self.parent()
        self.child()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            links = self.scan()
            links.scan()
        seen = [out.getvalue(), err.getvalue(), repr(vars(links.lineage)), repr(links.cli_owners), repr(links.owners), repr(procs._ENV),
                repr(procs._PIDS), repr(procs._CODEX), repr(vars(links.lineage.__class__))]
        self.assertIn(C, links.cli_owners)
        for secret in SECRETS:
            for text in seen:
                self.assertNotIn(secret, text)
        got = procs.env_values(103, lineage.ENV_NAMES)
        self.assertEqual(sorted(got), ['CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID'])
        self.assertTrue(all(s not in repr(got) for s in SECRETS))
        # the only file read is environ and the bytes read are used only to look for the two names: no secret value is left anywhere in the module globals
        for mod in (procs, lineage, link):
            for k, v in vars(mod).items():
                if isinstance(v, (dict, list, set, tuple, str, bytes)):
                    for secret in SECRETS:
                        self.assertNotIn(secret, repr(v), '%s.%s' % (mod.__name__, k))

    def test_ps_platform_environment(self):
        """Where there is no /proc: link through the environment in the `ps -E` output (after the command line). If no environment is printed, do not link."""
        self.parent()
        self.child()
        cmd = 'claude -p hello'
        table = {100: ('claude --dangerously-skip-permissions', 1), 103: (cmd, 1)}
        env_out = {100: None, 103: '%s HOME=/h API_KEY=%s %s=%s CLAUDE_PID=100' % (cmd, SECRETS[0], E, P)}
        self.session_file(100, P, entrypoint='cli', proc_start=None, domain=None)
        self.session_file(103, C, proc_start=None, domain=None)

        def run(argv, **kw):
            argv = tuple(argv)
            if argv[:-1] == procs.PS_ENV_ARGV:
                o = env_out[int(argv[-1])]
                if o is None:
                    return types.SimpleNamespace(returncode=1, stdout=b'')
                return types.SimpleNamespace(returncode=0, stdout=o.encode() + b'\n')
            if argv == procs.PS_PPID_ARGV:
                return types.SimpleNamespace(returncode=0, stdout=''.join('%d %d\n' % (p, pp) for p, (_, pp) in table.items()).encode())
            return types.SimpleNamespace(returncode=0, stdout=''.join('%d %s\n' % (p, c) for p, (c, _) in table.items()).encode())
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            self.assertEqual(self.scan().cli_owners[C]['sid'], P)
        self.links.lineage = lineage.Lineage()
        env_out[103] = cmd                                                       # output with no environment attached (permissions)
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            self.assertNotIn(C, self.scan().cli_owners)


# =====================================================================================================================
class LoopFactor(unittest.TestCase):
    def factors(self, cmd, **kw):
        code = link.shell_code(cmd)
        return [link.loop_factor(cmd, code, m.end()) for m in link.CLAUDE_LAUNCH_RE.finditer(code)]

    def test_counts(self):
        for cmd, want in [('claude -p x', [1]),
                          ('for x in a b c; do claude -p "$x" & done', [3]),
                          ('for x in a b c\ndo\n  claude -p $x &\ndone\nclaude -p after', [3, 1]),
                          ('for i in {1..5}; do claude -p x; done', [5]),
                          ('for i in {5..1}; do claude -p x; done', [5]),
                          ('for i in {a,b,c,d}; do claude -p x; done', [4]),
                          ('for i in $(ls); do claude -p x; done', [link.LOOP_UNKNOWN]),
                          ('for i in $LIST; do claude -p x; done', [link.LOOP_UNKNOWN]),
                          ('for i in *.md; do claude -p x; done', [link.LOOP_UNKNOWN]),
                          ('for i; do claude -p x; done', [link.LOOP_UNKNOWN]),
                          ('while read q; do claude -p "$q" & done < f', [link.LOOP_UNKNOWN]),
                          ('until false; do claude -p x; done', [link.LOOP_UNKNOWN]),
                          ('for a in 1 2; do for b in x y z; do claude -p q & done; done', [6]),
                          ('for a in 1 2; do echo hi; done; claude -p z', [1]),
                          ('for a in 1 2; do (cd /w && claude -p "$a") & done', [2]),
                          ('for a in 1 2; do if true; then claude -p x; fi; done', [2]),
                          ('for a in x y; do echo $a; done\nfor b in 1 2 3; do claude -p $b; done', [3]),
                          ('echo "for x in a b; do claude -p y; done"', []),
                          ('for a in 1 2 3 4 5 6 7 8 9 10; do for b in 1 2 3 4 5 6 7 8 9 10; do claude -p x; done; done', [link.LOOP_MAX])]:
            self.assertEqual(self.factors(cmd), want, cmd)

    def test_loop_words_in_other_places_are_not_loops(self):
        for cmd in ('echo for x in a b c; claude -p x', 'echo done; claude -p x', 'ls done do for\nclaude -p x', "echo 'for a in 1 2; do' && claude -p x"):
            self.assertEqual(self.factors(cmd), [1], cmd)

    def test_never_raises_and_is_bounded(self):
        for cmd in ('for', 'do', 'done done done claude -p x', 'for x in ; do claude -p x; done', 'for x in "unterminated; do claude -p x; done', 'while'):
            for n in self.factors(cmd):
                self.assertTrue(1 <= n <= link.LOOP_MAX, cmd)


class LoopLinks(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.kids = []

    def kid(self, n, t, cwd='/w', text='x'):
        """A child record whose first instruction is `text`: a call whose literal argument (or literal loop list) does not contain it is not its parent."""
        sid = '5%07d-1111-4111-8111-111111111111' % n
        self.write(sid, [dump({'type': 'user', 'timestamp': iso(t), 'cwd': cwd, 'message': {'role': 'user', 'content': text}})])
        self.kids.append(sid)
        return sid

    def owners_of(self, parent):
        return {c for c, o in self.scan().cli_owners.items() if o['sid'] == parent}

    def call(self, cmd, sid=P, t=T0, cwd='/w'):
        self.parent(sid, [bash_line(t, cmd, cwd, tid='toolu_l', desc='batch')])

    def test_known_loop_takes_that_many(self):
        self.call('for x in a b c; do claude -p "$x" & done')
        ks = [self.kid(i, T0 + 2 + i, text='abcab'[i]) for i in range(5)]       # every child is one of the listed words, so only the count limits them
        got = self.owners_of(P)
        self.assertEqual(got, set(ks[:3]))                                       # iteration count 3: up to the earliest three, not the fourth or fifth
        o = self.links.cli_owners[ks[0]]
        self.assertEqual((o['call'], o['bash_desc'], o['cwd']), ('toolu_l', 'batch', '/w'))

    def test_unknown_loop_is_bounded(self):
        self.call('while read q; do claude -p "$q" & done < qs.txt')
        ks = [self.kid(i, T0 + 1 + i * 0.1) for i in range(link.LOOP_UNKNOWN + 4)]
        self.assertEqual(self.owners_of(P), set(ks[:link.LOOP_UNKNOWN]))

    def test_unknown_count_and_unknown_folder_takes_one(self):
        """If neither the iteration count nor the working folder is known, several children are not linked by time alone. If the count is known, up to that many are linked even when the folder is not."""
        self.call('while read q; do (cd "$q" && claude -p x) & done < qs.txt')
        ks = [self.kid(i, T0 + 2 + i, cwd='/w/d%d' % i) for i in range(4)]                  # inside the folder the session was in: the folder is not known, the time rule may guess
        self.assertEqual(self.owners_of(P), {ks[0]})

    def test_an_unknown_folder_does_not_take_a_child_in_another_project(self):
        """`cd "$q" && claude -p x` with a folder that cannot be worked out: a child that runs outside the folder the session was in (another project) is not guessed."""
        self.call('for q in a b c; do (cd "$q" && claude -p x) & done')
        [self.kid(i, T0 + 2 + i, cwd='/d%d' % i) for i in range(3)]
        self.assertEqual(self.owners_of(P), set())

    def test_known_count_without_known_folder(self):
        self.call('for q in a b c; do (cd "$P/$q" && claude -p x) & done')                      # $P is set by nothing in the command: the folders are not known
        ks = [self.kid(i, T0 + 2 + i, cwd='/w/d%d' % i) for i in range(5)]
        self.assertEqual(self.owners_of(P), set(ks[:3]))

    def test_a_loop_over_a_literal_list_knows_its_folders(self):
        """`cd "$q"` with `q` from a literal list: the child has to be in one of the listed folders; the others are strangers."""
        self.call('for q in a b c; do (cd "$q" && claude -p x) & done')
        ks = [self.kid(i, T0 + 2 + i, cwd='/w/%s' % name) for i, name in enumerate(('a', 'b', 'zz', 'c'))]
        self.assertEqual(self.owners_of(P), {ks[0], ks[1], ks[3]})

    def test_plain_call_still_takes_one(self):
        self.call('claude -p x')
        ks = [self.kid(i, T0 + 2 + i) for i in range(3)]
        self.assertEqual(self.owners_of(P), {ks[0]})

    def test_launch_after_a_loop_takes_one(self):
        self.call('for a in 1 2; do echo hi; done; claude -p x')               # a launch outside the loop
        ks = [self.kid(i, T0 + 2 + i) for i in range(3)]
        self.assertEqual(self.owners_of(P), {ks[0]})

    def test_false_links_are_not_made(self):
        """Even in the same folder, a session outside the range (time, folder) or already assigned to another parent is not taken."""
        self.call('for x in a b c; do claude -p "$x" & done')
        inside = [self.kid(i, T0 + 2 + i, text='ab'[i]) for i in range(2)]
        before = self.kid(10, T0 - 5, text='a')                                  # a session earlier than the call
        other_dir = self.kid(12, T0 + 3, cwd='/elsewhere', text='c')             # a different folder
        stranger = self.kid(13, T0 + 3, text='zzz')                              # the loop lists its words: a child that is none of them is not its child
        self.assertEqual(self.owners_of(P), set(inside))
        for s in (before, other_dir, stranger):
            self.assertNotIn(s, self.links.cli_owners)

    def test_pinned_to_another_parent_is_not_taken(self):
        self.call('for x in a b c; do claude -p "$x" & done')
        ks = [self.kid(i, T0 + 2 + i, text='abc'[i]) for i in range(3)]
        self.parent(Q)
        self.proc.add(110, 1, ['claude'])
        self.session_file(110, Q, entrypoint='cli')
        self.proc.add(111, 1, ['claude', '-p', 'x'])
        self.session_file(111, ks[1])
        set_env(self.proc, 111, Q, 110)                                          # the middle session says its environment is Q
        got = self.scan().cli_owners
        self.assertEqual({k: got[k]['sid'] for k in got}, {ks[0]: P, ks[1]: Q, ks[2]: P})

    def test_a_loop_does_not_take_the_calling_session_itself(self):
        self.call('for x in a b; do claude -p "$x" & done')
        self.assertEqual(self.owners_of(P), set())                               # its own transcript is not a candidate

    def test_loop_inside_a_script_file(self):
        sd = os.path.join(self.t, 'scratch')
        os.makedirs(sd)
        s = os.path.join(sd, 'batch.sh')
        with open(s, 'w') as f:
            f.write('cd /w\nfor t in x y z; do\n  claude -p "$t" < /dev/null > out.$t 2>&1 &\ndone\nwait\n')
        self.call('bash %s' % s)
        ks = [self.kid(i, T0 + 2 + i, text='xyzx'[i]) for i in range(4)]
        self.assertEqual(self.owners_of(P), set(ks[:3]))

    def test_two_loop_calls_do_not_share_a_child(self):
        self.parent(P, [bash_line(T0, 'for x in a b; do claude -p "$x" & done', '/w', tid='toolu_1'),
                        bash_line(T0 + 1, 'for x in c d; do claude -p "$x" & done', '/w', tid='toolu_2')])
        ks = [self.kid(i, T0 + 2 + i, text='abcda'[i]) for i in range(5)]
        got = self.scan().cli_owners
        self.assertEqual(sorted(got[k]['call'] for k in ks if k in got), ['toolu_1', 'toolu_1', 'toolu_2', 'toolu_2'])
        self.assertNotIn(ks[4], got)                                             # a third child of the first loop (its words a, b) has no share left


# =====================================================================================================================
def notification(task, status='completed', tool_use='toolu_g', summary='Agent "x" finished', usage=True, t=T0 + 50):
    a = {'type': 'queued_command', 'commandMode': 'task-notification', 'timestamp': iso(t),
         'prompt': '<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>%s</status>\n<summary>%s</summary>\n<result>%s</result>\n</task-notification>'
                   % (task, tool_use, status, summary, 'long result ' * 500)}
    if usage:
        a['usage'] = {'totalTokens': 234187, 'toolUses': 36, 'durationMs': 218972}
    return dump({'type': 'attachment', 'timestamp': iso(t), 'attachment': a})


class Grandchild(unittest.TestCase):
    """Agent G (depth 2) launched by subagent A1 (a1…): the completion notification is in A1's transcript, not in the main transcript."""
    A1 = 'a1' + '0' * 15
    G = 'a2' + '0' * 15
    H = 'a3' + '0' * 15
    X = 'a4' + '0' * 15
    SID = '66666666-6666-4666-8666-666666666666'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(os.path.realpath(self.tmp.name), self.SID)
        os.makedirs(os.path.join(self.d, 'subagents'))
        self.main = self.d + '.jsonl'
        self.lines(self.main, [dump({'type': 'user', 'timestamp': iso(T0), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}}),
                               dump({'type': 'assistant', 'timestamp': iso(T0 + 1), 'cwd': '/w',
                                     'message': {'content': [{'type': 'tool_use', 'id': 'toolu_a1', 'name': 'Agent', 'input': {'description': 'A1 work', 'prompt': 'p'}}]}})])

    def lines(self, path, lines):
        with open(path, 'a') as f:
            f.write('\n'.join(lines) + '\n')

    def agent(self, aid, parent=None, tu=None, depth=None, lines=None, desc=None):
        meta = {'description': desc or 'agent ' + aid[:2], 'agentType': 'general-purpose', 'toolUseId': tu or 'toolu_' + aid[:2]}
        if parent:
            meta.update(parentAgentId=parent, spawnDepth=depth or 2)
        with open(os.path.join(self.d, 'subagents', 'agent-%s.meta.json' % aid), 'w') as f:
            json.dump(meta, f)
        self.lines(os.path.join(self.d, 'subagents', 'agent-%s.jsonl' % aid),
                   lines or [dump({'type': 'user', 'timestamp': iso(T0 + 2), 'message': {'role': 'user', 'content': 'do it'}}),
                             dump({'type': 'assistant', 'timestamp': iso(T0 + 3), 'message': {'content': [{'type': 'text', 'text': 'working'}], 'usage': {}}})])

    def session(self):
        s = server.Session(self.main)
        self.poll(s)
        return s

    def poll(self, s):
        s.poll()
        s._attach_main_side()                                                     # what views.state does before it reads the status

    def status(self, s, aid, now=T0 + 60, alive=True):
        return s.agent_status(s.agents[aid], alive, now)

    def notify_events(self, s, aid):
        return [e for e in s.feed if e['kind'] == 'notify' and e.get('agent') == aid]

    def test_notification_in_parent_record_marks_done(self):
        self.agent(self.A1)
        self.agent(self.G, self.A1, 'toolu_g')
        self.lines(os.path.join(self.d, 'subagents', 'agent-%s.jsonl' % self.A1), [notification(self.G)])
        s = self.session()
        self.assertEqual(self.status(s, self.G, now=T0 + 3600 * 5), 'done')       # not "working" even after several hours
        n = s.agents[self.G].notifications
        self.assertEqual([(x['status'], x['tokens'], x['tools'], x['duration_ms']) for x in n], [('completed', 234187, 36, 218972)])
        self.assertEqual(len(self.notify_events(s, self.G)), 1)
        self.assertEqual(self.notify_events(s, self.G)[0]['status'], 'done')
        self.poll(s)
        self.poll(s)
        self.assertEqual(len(s.agents[self.G].notifications), 1)                  # read again, still only once
        self.assertEqual(len(self.notify_events(s, self.G)), 1)
        self.assertEqual(self.status(s, self.A1), 'running')                      # the parent is unchanged

    def test_failed_and_killed(self):
        self.agent(self.A1)
        self.agent(self.G, self.A1)
        self.agent(self.H, self.A1, 'toolu_h')
        self.lines(os.path.join(self.d, 'subagents', 'agent-%s.jsonl' % self.A1),
                   [notification(self.G, 'failed', summary='boom'), notification(self.H, 'killed', 'toolu_h', 'stopped')])
        s = self.session()
        self.assertEqual((self.status(s, self.G), self.status(s, self.H)), ('failed', 'killed'))

    def test_parent_finished_without_notification_is_ended(self):
        self.agent(self.A1)
        self.agent(self.G, self.A1)
        self.assertEqual(self.status(self.session(), self.G), 'running')           # for comparison: the parent is still working
        self.lines(self.main, [dump({'type': 'user', 'timestamp': iso(T0 + 30), 'message': {'role': 'user', 'content':
                    '<task-notification><task-id>%s</task-id><status>completed</status><summary>A1 done</summary></task-notification>' % self.A1}})])
        s = self.session()
        self.assertEqual(self.status(s, self.A1), 'done')
        self.assertEqual(self.status(s, self.G), 'ended')                          # the parent has finished but there is no notification
        self.assertEqual(s.agents[self.G].notifications, [])

    def test_parent_ended_because_the_session_process_is_gone(self):
        self.agent(self.A1)
        self.agent(self.G, self.A1)
        s = self.session()
        self.assertEqual(self.status(s, self.G, alive=False), 'ended')

    def test_notification_beats_parent_state_and_unknown_parent_is_not_ended(self):
        self.agent(self.G, self.A1)                                               # the meta of parent A1 does not exist yet
        s = self.session()
        self.assertEqual(self.status(s, self.G), 'running')

    def test_notification_before_the_agent_is_known(self):
        self.agent(self.A1)
        self.lines(os.path.join(self.d, 'subagents', 'agent-%s.jsonl' % self.A1), [notification(self.G)])
        s = self.session()
        self.assertNotIn(self.G, s.agents)
        self.agent(self.G, self.A1)
        self.poll(s)
        self.assertEqual(self.status(s, self.G), 'done')
        self.assertEqual(len(s.agents[self.G].notifications), 1)

    def test_third_level(self):
        self.agent(self.A1)
        self.agent(self.G, self.A1)
        self.agent(self.H, self.G, 'toolu_h', depth=3)
        self.lines(os.path.join(self.d, 'subagents', 'agent-%s.jsonl' % self.G), [notification(self.H, tool_use='toolu_h')])
        s = self.session()
        self.assertEqual(self.status(s, self.H), 'done')
        self.assertEqual(self.status(s, self.G), 'running')

    def test_parent_cycle_does_not_recurse(self):
        self.agent(self.G, self.H)
        self.agent(self.H, self.G)
        s = self.session()
        for aid in (self.G, self.H):
            self.assertIn(self.status(s, aid), ('running', 'stalled', 'ended'))

    def test_foreground_agent_result(self):
        """The result of an Agent tool call is the completion itself. The confirmation that it started in the background (async_launched) is not a completion."""
        calls = [{'type': 'tool_use', 'id': 'toolu_%s' % n, 'name': 'Agent', 'input': {'description': n, 'prompt': 'p'}} for n in ('g', 'h', 'x')]
        self.agent(self.A1, lines=[
            dump({'type': 'user', 'timestamp': iso(T0 + 2), 'message': {'role': 'user', 'content': 'go'}}),
            dump({'type': 'assistant', 'timestamp': iso(T0 + 3), 'message': {'content': calls, 'usage': {}}}),
            dump({'type': 'user', 'timestamp': iso(T0 + 4), 'toolUseResult': {'isAsync': True, 'status': 'async_launched', 'agentId': self.X},
                  'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_x', 'content': 'Async agent launched'}]}})])
        self.agent(self.G, self.A1, 'toolu_g')
        self.agent(self.H, self.A1, 'toolu_h')
        self.agent(self.X, self.A1, 'toolu_x')
        s = self.session()
        self.assertEqual(self.status(s, self.X), 'running')                       # only the confirmation that it started
        self.lines(os.path.join(self.d, 'subagents', 'agent-%s.jsonl' % self.A1), [
            dump({'type': 'user', 'timestamp': iso(T0 + 40), 'toolUseResult': {'status': 'completed', 'agentId': self.G},
                  'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_g', 'content': 'result text'}]}}),
            dump({'type': 'user', 'timestamp': iso(T0 + 41),
                  'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_h', 'is_error': True, 'content': 'Error: nope'}]}})])
        self.poll(s)
        self.assertEqual((self.status(s, self.G), self.status(s, self.H), self.status(s, self.X)), ('done', 'failed', 'running'))
        self.poll(s)
        self.assertEqual((len(self.notify_events(s, self.G)), len(self.notify_events(s, self.H)), len(self.notify_events(s, self.X))), (1, 1, 0))

    def test_unknown_result_shape_is_not_a_completion(self):
        self.agent(self.A1, lines=[
            dump({'type': 'assistant', 'timestamp': iso(T0 + 3), 'message': {'content': [
                {'type': 'tool_use', 'id': 'toolu_g', 'name': 'Agent', 'input': {'description': 'sub', 'prompt': 'p'}}], 'usage': {}}}),
            dump({'type': 'user', 'timestamp': iso(T0 + 4), 'toolUseResult': {'status': 'weird'},
                  'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_g', 'content': 'Fork started'}]}})])
        self.agent(self.G, self.A1, 'toolu_g')
        s = self.session()
        self.assertEqual(self.status(s, self.G), 'running')
        self.assertEqual(s.agents[self.G].notifications, [])

    def test_direct_children_and_main_record_unchanged(self):
        self.agent(self.A1)
        self.lines(self.main, [dump({'type': 'user', 'timestamp': iso(T0 + 30), 'message': {'role': 'user', 'content':
                    '<task-notification><task-id>%s</task-id><status>completed</status><summary>A1 done</summary></task-notification>' % self.A1}})])
        s = self.session()
        self.assertEqual(self.status(s, self.A1), 'done')
        self.assertIsNone(s.agents[self.A1].parent_agent)
        self.assertEqual(len(self.notify_events(s, self.A1)), 1)


if __name__ == '__main__':
    unittest.main()
