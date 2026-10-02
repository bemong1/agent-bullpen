"""Tests for linking child sessions by process lineage (board/lineage.py, procs.py) and for reading the script files that Bash runs (link.bash_scripts).

They use only a fake /proc (procs.PROC), a fake ~/.claude/sessions and temporary folders. There must be no false links:
unrelated processes, another user's session files, pid reuse (procStart), another pid namespace, sessions without a transcript, cycles, nested scripts.

    python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server, start_patches  # noqa: E402
from test_stage2 import T0, bash_line, child_lines, dump, iso  # noqa: E402,F401  (import functions only: importing a TestCase would run it twice)

from board import link, lineage, procs  # noqa: E402

P = '11111111-1111-4111-8111-111111111111'       # parent Claude session
Q = '22222222-2222-4222-8222-222222222222'       # another Claude session
C = '33333333-3333-4333-8333-333333333333'       # claude -p child session
D = '44444444-4444-4444-8444-444444444444'
TID = '019a0001-0000-7000-8000-00000000000a'     # Codex thread
TID2 = '019a0001-0000-7000-8000-00000000000b'
NS = 'pid:[4026531836]'
UID = os.geteuid()


class FakeProc:
    """A fake /proc to give to procs.PROC. For each process it creates cmdline, status (PPid, Uid), stat (starttime) and fd/."""

    def __init__(self, root, ns=NS):
        self.root = root
        os.makedirs(os.path.join(root, 'self', 'ns'))
        with open(os.path.join(root, 'self', 'cmdline'), 'wb') as f:
            f.write(b'python\0')
        if ns:
            os.symlink(ns, os.path.join(root, 'self', 'ns', 'pid'))

    def add(self, pid, ppid, argv, uid=UID, start=None, fds=(), comm='x'):
        d = os.path.join(self.root, str(pid))
        os.makedirs(os.path.join(d, 'fd'))
        with open(os.path.join(d, 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in argv) + b'\0')
        with open(os.path.join(d, 'status'), 'w') as f:
            f.write('Name:\t%s\nPPid:\t%d\nUid:\t%d\t%d\t%d\t%d\n' % (comm, ppid, uid, uid, uid, uid))
        with open(os.path.join(d, 'stat'), 'w') as f:       # comm may contain spaces or parentheses, so counting starts after the last `)`
            f.write('%d (%s) S %d %s %d\n' % (pid, comm + ') (x', ppid, ' '.join(['0'] * 17), start if start is not None else pid * 10))
        for i, target in enumerate(fds):
            os.symlink(target, os.path.join(d, 'fd', str(3 + i)))

    def remove(self, pid):
        shutil.rmtree(os.path.join(self.root, str(pid)))


class Fixture(unittest.TestCase):
    """A fake HOME + a fake /proc + a new LinkIndex and CodexIndex."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.t = t
        self.home = os.path.join(t, 'home')
        self.claude = os.path.join(self.home, '.claude')
        self.proj = os.path.join(self.claude, 'projects', '-w')
        self.sess_dir = os.path.join(self.claude, 'sessions')
        self.codex = os.path.join(self.home, '.codex')
        for d in (self.proj, self.sess_dir, os.path.join(self.codex, 'sessions')):
            os.makedirs(d)
        self.proc = FakeProc(os.path.join(t, 'proc'))
        self.links = server.LinkIndex()
        self.index = server.CodexIndex()
        start_patches(self, HOME=self.home, CLAUDE_HOME=self.claude, PROJECTS=os.path.join(self.claude, 'projects'),
                      CODEX_HOME=self.codex, CODEX_SESSIONS=os.path.join(self.codex, 'sessions'),
                      CODEX_NAMES=os.path.join(self.codex, 'session_index.jsonl'), CODEX=self.index, LINKS=self.links,
                      PROC=self.proc.root)
        procs.reset()
        self.addCleanup(procs.reset)
        link._SCRIPTS.clear()

    # ---- transcripts ----
    def write(self, sid, lines):
        p = os.path.join(self.proj, sid + '.jsonl')
        with open(p, 'a') as f:
            f.write('\n'.join(lines) + '\n')
        return p

    def parent(self, sid=P, lines=None):
        return self.write(sid, lines or [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])

    def child(self, sid=C, t=T0 + 2, cwd='/w'):
        return self.write(sid, child_lines(t, cwd))

    def session_file(self, pid, sid, start=None, entrypoint='sdk-cli', proc_start='auto', domain='linux:abc:' + NS, name=None, cwd='/w'):
        d = {'pid': pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': int((T0 + 1) * 1000), 'kind': 'interactive', 'entrypoint': entrypoint}
        if proc_start == 'auto':
            proc_start = str(start if start is not None else pid * 10)
        if proc_start is not None:
            d['procStart'] = proc_start
        if domain is not None:
            d['pidDomain'] = domain
        with open(os.path.join(self.sess_dir, name or '%d.json' % pid), 'w') as f:
            json.dump(d, f)

    def tree(self, with_child=True, **kw):
        """P (100, claude) → bash -c (101) → bash script (102) → claude -p (103, session C)."""
        self.proc.add(100, 1, ['claude', '--dangerously-skip-permissions'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.add(101, 100, ['/bin/bash', '-c', 'source snapshot.sh && eval "bash /tmp/x/run.sh"'])
        self.proc.add(102, 101, ['/bin/bash', '/tmp/x/run.sh'])
        if with_child:
            self.proc.add(103, 102, kw.get('argv', ['claude', '-p', 'hello', '--model', 'opus']))
            self.session_file(103, C, entrypoint=kw.get('entrypoint', 'sdk-cli'))

    def rollout(self, tid=TID, origin='codex_exec', cwd='/w', t=T0 + 2, user='fix the thing in the build', parent=None):
        d = os.path.join(self.codex, 'sessions', '2026', '10', '01')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'rollout-2026-10-01T00-00-00-%s.jsonl' % tid)
        meta = {'id': tid, 'originator': origin, 'cwd': cwd, 'timestamp': iso(t)}
        if parent:
            meta['parent_thread_id'] = parent
        lines = [dump({'timestamp': iso(t), 'type': 'session_meta', 'payload': meta}),
                 dump({'timestamp': iso(t + 1), 'type': 'event_msg', 'payload': {'type': 'task_started'}}),
                 dump({'timestamp': iso(t + 1), 'type': 'response_item',
                       'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': user}]}})]
        with open(p, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return p

    def codex_proc(self, pid, ppid, path, argv=None):
        self.proc.add(pid, ppid, argv or ['codex', 'exec', '-C', '/w', 'do it'], fds=[path])

    def scan(self):
        procs.reset()
        self.index.refresh(force=True)
        self.links.scan()
        return self.links


# =====================================================================================================================
class ProcsPrimitives(unittest.TestCase):
    def test_real_proc_basics(self):
        procs.reset()
        me = os.getpid()
        self.assertEqual(procs.ppid(me), os.getppid())
        self.assertIn(os.getppid(), procs.ancestors(me))
        self.assertEqual(procs.uid(me), os.geteuid())
        self.assertTrue(procs.argv(me)[0])
        if procs.has_proc():
            with open('/proc/self/stat', 'rb') as f:
                raw = f.read()
            self.assertEqual(procs.starttime(me), int(raw[raw.rindex(b')') + 2:].split()[19]))
            self.assertTrue(procs.pid_ns().startswith('pid:['))

    def test_never_raises(self):
        for bad in (None, -1, 0, True, 'x', 1.5, 10 ** 9, 2 ** 70):
            self.assertIsNone(procs.ppid(bad))
            self.assertEqual(procs.ancestors(bad), [])
            self.assertIsNone(procs.starttime(bad))
            self.assertIsNone(procs.uid(bad))
            self.assertIsNone(procs.argv(bad))

    def test_fake_proc_fields_and_chain(self):
        with tempfile.TemporaryDirectory() as d:
            fp = FakeProc(os.path.join(d, 'p'))
            fp.add(10, 1, ['claude'], start=777, comm='a b')
            fp.add(11, 10, ['bash', '-c', 'x'], uid=UID + 5)
            fp.add(12, 11, ['sleep', '9'])
            with mock.patch.object(procs, 'PROC', fp.root):
                procs.reset()
                self.assertEqual(procs.ppid(12), 11)
                self.assertEqual(procs.ancestors(12), [11, 10])           # stops at 1
                self.assertEqual(procs.starttime(10), 777)                 # even if comm has spaces or parentheses
                self.assertEqual(procs.uid(11), UID + 5)
                self.assertEqual(procs.argv(11), [b'bash', b'-c', b'x'])
                self.assertEqual(procs.pid_ns(), NS)
                self.assertIsNone(procs.ppid(99))                          # a process that does not exist
                self.assertEqual(procs.ancestors(99), [])

    def test_ancestor_cycle_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            fp = FakeProc(os.path.join(d, 'p'))
            fp.add(20, 21, ['a'])
            fp.add(21, 20, ['b'])
            for i in range(30, 130):
                fp.add(i, i + 1, ['c'])
            with mock.patch.object(procs, 'PROC', fp.root):
                self.assertEqual(procs.ancestors(20), [21])                # stops at the cycle
                self.assertEqual(len(procs.ancestors(30, limit=5)), 5)

    def test_ps_fallback_when_no_proc(self):
        calls = []

        def run(argv, **kw):
            calls.append(tuple(argv))
            if tuple(argv) == procs.PS_UID_ARGV:
                return types.SimpleNamespace(returncode=0, stdout=b'  500     0\n  501   501\n 502   501\nbad line\n  503 x\n')
            return types.SimpleNamespace(returncode=0, stdout=b'  500   1\n  501 500\n 502 501\nbad line\n')
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            procs.reset()
            self.assertEqual(procs.ppid(502), 501)
            self.assertEqual(procs.ancestors(502), [501, 500])
            self.assertIsNone(procs.ppid(999))
            self.assertEqual(len(calls), 1)                                # the table is not fetched again for 3 seconds
            self.assertIsNone(procs.starttime(502))                        # without /proc it cannot be compared
            self.assertEqual((procs.uid(502), procs.uid(500), procs.uid(501)), (501, 0, 501))     # the owner comes from its own ps table
            self.assertEqual((procs.uid(999), procs.uid(503)), (None, None))                     # a missing process; a row that is not two numbers
            self.assertEqual([c for c in calls if c == procs.PS_UID_ARGV], [procs.PS_UID_ARGV])  # fetched once for all of them
            self.assertIsNone(procs.codex_pids('/s'))                      # open files are unknown
        for exc in (OSError('x'), subprocess.TimeoutExpired('ps', 1)):
            procs.reset()
            with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', side_effect=exc):
                self.assertIsNone(procs.ppid(502))
                self.assertIsNone(procs.uid(502))
                self.assertEqual(procs.ancestors(502), [])
        procs.reset()
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), \
                mock.patch.object(subprocess, 'run', return_value=types.SimpleNamespace(returncode=1, stdout=b'')):
            self.assertIsNone(procs.ppid(502))
            self.assertIsNone(procs.uid(502))
        procs.reset()

    def test_real_ps_without_proc(self):
        """The ps way against the real `ps` of this machine (procps on Linux, BSD on macOS): the tables for the parent and the owner read back what the system says."""
        import shutil
        if not shutil.which('ps'):
            self.skipTest('no ps')
        me = os.getpid()
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'):
            procs.reset()
            self.assertFalse(procs.has_proc())
            self.assertEqual(procs.ppid(me), os.getppid())
            self.assertEqual(procs.uid(me), os.geteuid())
            self.assertIn(os.getppid(), procs.ancestors(me))
            self.assertIsNone(procs.uid(2 ** 22 + 12345))                                     # no such process
        procs.reset()

    def test_codex_pids_per_process(self):
        with tempfile.TemporaryDirectory() as d:
            sessions = os.path.join(d, 'sessions')
            os.makedirs(sessions)
            roll = os.path.join(sessions, 'rollout-x-%s.jsonl' % TID)
            open(roll, 'w').close()
            fp = FakeProc(os.path.join(d, 'p'))
            fp.add(40, 1, ['/opt/bin/codex', 'exec', 'x'], fds=[roll, os.path.join(d, 'other')])
            fp.add(41, 1, ['codex-view', roll], fds=[roll])                # a different tool with a similar name
            fp.add(42, 1, ['node', '/x/bin/codex.js', 'exec'], fds=[roll])
            fp.add(43, 1, ['vim', 'notes'])
            with mock.patch.object(procs, 'PROC', fp.root):
                procs.reset()
                got = {i['pid']: i['fds'] for i in procs.codex_pids(sessions)}
                self.assertEqual(got, {40: {roll}, 42: {roll}})
                self.assertEqual(procs._proc_scan(sessions)[1], {roll})   # the old shape (merged open files) stays as it is
                self.assertEqual(len(procs._proc_scan(sessions)[0]), 3)    # `codex-view` also counts as a process named codex under the old check
        procs.reset()


# =====================================================================================================================
class LineageCli(Fixture):
    def linked(self):
        return self.scan().cli_owners

    def test_child_linked_through_script_chain(self):
        self.tree()
        self.parent()
        self.child()
        o = self.linked()[C]
        self.assertEqual(o['sid'], P)
        self.assertIsNone(o['call'])
        self.assertEqual(o['bash_desc'], '')
        self.assertEqual(o['bash_ts'], T0 + 1)                                # startedAt of the session file
        self.assertIsNone(o['dt'])
        self.assertEqual(o['cwd'], '/w')
        self.assertEqual(self.links.cli_owned_by(P), {C: o})
        self.assertEqual(self.links.cli_count(P), 1)

    def test_remembered_after_exit_and_not_written(self):
        self.tree()
        self.parent()
        self.child()
        self.assertIn(C, self.linked())
        for pid in (100, 101, 102, 103):
            self.proc.remove(pid)
        for f in os.listdir(self.sess_dir):
            os.unlink(os.path.join(self.sess_dir, f))
        before = sorted(os.listdir(self.sess_dir)), sorted(os.listdir(self.home))
        self.assertEqual(self.linked()[C]['sid'], P)                          # remembered after the process ends, as long as the server is up
        self.assertEqual((sorted(os.listdir(self.sess_dir)), sorted(os.listdir(self.home))), before)
        self.assertEqual(sorted(os.listdir(self.home)), ['.claude', '.codex'])  # no file is written

    def test_no_claude_ancestor(self):
        self.tree()
        self.proc.remove(101)
        self.proc.add(101, 1, ['/bin/bash', '-c', 'x'])                      # the chain does not reach claude (100)
        self.parent()
        self.child()
        self.assertNotIn(C, self.linked())

    def test_unrelated_process_not_linked(self):
        self.tree(with_child=False)
        self.proc.add(300, 1, ['claude', '-p', 'x'])                         # a claude -p outside the lineage
        self.session_file(300, C)
        self.parent()
        self.child()
        self.assertNotIn(C, self.linked())

    def test_nearest_claude_ancestor_wins(self):
        self.tree(with_child=False)
        self.proc.add(110, 102, ['claude', '-p', 'sub'])                     # Q under P, and C under Q
        self.session_file(110, Q)
        self.proc.add(111, 110, ['/bin/bash', '/tmp/y.sh'])
        self.proc.add(112, 111, ['claude', '-p', 'subsub'])
        self.session_file(112, C)
        self.parent()
        self.parent(Q)
        self.child()
        got = self.linked()
        self.assertEqual(got[Q]['sid'], P)
        self.assertEqual(got[C]['sid'], Q)

    def test_pid_reuse_is_not_linked(self):
        self.parent()
        self.child()
        self.tree()
        self.session_file(100, P, entrypoint='cli', proc_start='999999')      # a different process with the same pid
        self.assertNotIn(C, self.linked())
        self.session_file(100, P, entrypoint='cli')
        self.session_file(103, C, proc_start='999999')                        # the child side is the reused one
        self.assertNotIn(C, self.linked())
        self.session_file(103, C)
        self.assertEqual(self.linked()[C]['sid'], P)                          # putting it back links it again (the check above was the cause)

    def test_other_users_are_not_linked(self):
        self.parent()
        self.child()
        self.tree()
        self.proc.remove(100)
        self.proc.add(100, 1, ['claude', '--dangerously-skip-permissions'], uid=UID + 1)   # a process of a different user than the file owner
        self.assertNotIn(C, self.linked())
        self.proc.remove(100)
        self.proc.add(100, 1, ['claude'])
        self.proc.remove(103)
        self.proc.add(103, 102, ['claude', '-p', 'x'], uid=UID + 1)
        self.assertNotIn(C, self.linked())

    def test_other_pid_namespace(self):
        self.parent()
        self.child()
        self.tree()
        self.session_file(100, P, entrypoint='cli', domain='linux:abc:pid:[1234]')
        self.assertNotIn(C, self.linked())
        self.session_file(100, P, entrypoint='cli', domain=None)               # with no marker there is no comparison
        self.assertEqual(self.linked()[C]['sid'], P)

    def test_non_claude_process_with_session_file(self):
        self.parent()
        self.child()
        self.tree()
        self.proc.remove(100)
        self.proc.add(100, 1, ['python3', 'server.py'])                       # an unrelated process that only shares the pid
        self.assertNotIn(C, self.linked())

    def test_interactive_child_is_not_a_sub_agent(self):
        self.parent()
        self.child()
        self.tree(argv=['claude', '--resume'], entrypoint='cli')
        self.assertNotIn(C, self.linked())
        self.proc.remove(103)
        self.proc.add(103, 102, ['claude', '--print', 'x'])                   # --print is the same as -p
        self.assertEqual(self.linked()[C]['sid'], P)

    def test_session_opened_by_user_is_not_taken(self):
        self.parent()
        self.child()
        self.tree()
        self.proc.add(200, 1, ['claude', '--resume', C])                      # the user opened the same session separately
        self.session_file(200, C, entrypoint='cli')
        self.assertNotIn(C, self.linked())
        self.proc.remove(200)
        os.unlink(os.path.join(self.sess_dir, '200.json'))
        self.assertEqual(self.linked()[C]['sid'], P)
        self.proc.add(200, 1, ['claude', '--resume', C])                      # if the user opens it after the link was made, the link is released
        self.session_file(200, C, entrypoint='cli')
        self.assertNotIn(C, self.linked())

    def test_records_are_required(self):
        self.tree()
        self.parent()
        self.assertNotIn(C, self.linked())                                    # no child transcript: nowhere to show it
        os.unlink(os.path.join(self.proj, P + '.jsonl'))
        self.child()
        self.assertNotIn(C, self.linked())                                    # no parent transcript: the child only disappears from the list
        self.parent()
        self.assertEqual(self.linked()[C]['sid'], P)

    def test_same_session_is_not_its_own_child(self):
        self.tree()
        self.parent()
        self.session_file(103, P)                                             # claude -p --resume <the parent itself>
        self.assertEqual(self.linked(), {})

    def test_no_cycles(self):
        self.tree(with_child=False)
        self.proc.add(110, 102, ['claude', '-p', 'x'])                        # P → Q
        self.session_file(110, Q)
        self.proc.add(111, 110, ['claude', '-p', 'y'])                        # Q → P (resume): a cycle
        self.session_file(111, P)
        self.parent()
        self.parent(Q)
        got = self.linked()
        self.assertEqual(got[Q]['sid'], P)
        self.assertNotIn(P, got)

    def test_session_file_hygiene(self):
        self.parent()
        self.child()
        self.tree()
        os.unlink(os.path.join(self.sess_dir, '103.json'))
        for kind in ('name', 'pid', 'sid', 'json', 'big', 'link'):
            if kind == 'name':
                self.session_file(103, C, name='copy-of-103.json')
            elif kind == 'pid':
                self.session_file(104, C, name='103.json')                    # the pid inside the file differs from the file name
            elif kind == 'sid':
                self.session_file(103, '../../etc/x')
            elif kind == 'json':
                with open(os.path.join(self.sess_dir, '103.json'), 'w') as f:
                    f.write('{not json')
            elif kind == 'big':
                with open(os.path.join(self.sess_dir, '103.json'), 'w') as f:
                    f.write(json.dumps({'pid': 103, 'sessionId': C, 'pad': 'x' * (lineage.SESSION_FILE_MAX + 5)}))
            elif kind == 'link':
                self.session_file(103, C, name='real103.json')
                os.symlink(os.path.join(self.sess_dir, 'real103.json'), os.path.join(self.sess_dir, '103.json'))
            self.assertNotIn(C, self.linked(), kind)
            for f in os.listdir(self.sess_dir):
                if f not in ('100.json',):
                    os.unlink(os.path.join(self.sess_dir, f))
        self.session_file(103, C)
        self.assertEqual(self.linked()[C]['sid'], P)

    def test_lineage_beats_time_rule(self):
        """A `claude -p` call in another session Q would take C by time and cwd, but if the lineage says P, it is P. The released call takes the next candidate."""
        self.parent()
        self.parent(Q, [bash_line(T0, 'claude -p hi', '/w', tid='toolu_q')])      # the literal argument is the child's instruction: a different one would veto the call
        self.child()
        self.assertEqual(self.linked()[C]['sid'], Q)                          # control: with no lineage, the time rule
        self.assertEqual(self.links.cli_owners[C]['call'], 'toolu_q')
        self.child(D, T0 + 5)
        self.tree()
        got = self.linked()
        self.assertEqual(got[C]['sid'], P)
        self.assertIsNone(got[C]['call'])
        self.assertEqual(got[D]['sid'], Q)                                    # Q's call lets go of C and takes D
        self.assertEqual(got[D]['call'], 'toolu_q')

    def test_same_parent_call_fills_details(self):
        self.parent(P, [bash_line(T0, 'cd /w && bash /tmp/x/run.sh', '/w', tid='toolu_p', desc='launch')])
        self.child()
        self.tree()
        with mock.patch.object(link, 'bash_scripts', return_value=[{'path': '/tmp/x/run.sh', 'text': 'claude -p hi', 'code': 'claude -p hi',
                                                                    'cwd': '/w', 'env': {}}]):
            o = self.linked()[C]
        self.assertEqual((o['sid'], o['call'], o['bash_desc']), (P, 'toolu_p', 'launch'))   # with the same parent, the call info fills in the details

    def test_ps_platform_without_proc(self):
        """A place without /proc (imitating macOS): links by the ps table alone. Start time and owner cannot be compared."""
        self.parent()
        self.child()
        self.tree()
        table = {100: ('claude --dangerously-skip-permissions', 1), 101: ('/bin/bash -c x', 100), 102: ('/bin/bash /tmp/x/run.sh', 101),
                 103: ('claude -p hello', 102)}
        cmds = ''.join('%d %s\n' % (p, c) for p, (c, _) in table.items()).encode()
        ppids = ''.join('%d %d\n' % (p, pp) for p, (_, pp) in table.items()).encode()

        uids = {}                                                          # pid -> owner the fake ps names (ours unless set)

        def run(argv, **kw):
            if tuple(argv) == procs.PS_UID_ARGV:
                return types.SimpleNamespace(returncode=0, stdout=''.join('%d %d\n' % (p, uids.get(p, os.geteuid())) for p in table).encode())
            return types.SimpleNamespace(returncode=0, stdout=ppids if tuple(argv) == procs.PS_PPID_ARGV else cmds)
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            self.assertEqual(self.linked()[C]['sid'], P)
        self.links.lineage = lineage.Lineage()
        uids[100] = os.geteuid() + 1                                       # the live claude of the session file is another user's: its file is not trusted
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', run):
            self.assertNotIn(C, self.linked())
        self.links.lineage = lineage.Lineage()
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', side_effect=OSError):
            self.assertNotIn(C, self.linked())                                # if ps cannot be used either, unknown: no link and no exception


# =====================================================================================================================
class LineageCodex(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.tree(with_child=False)
        self.parent()

    def owners(self):
        return self.scan().owners

    def test_exec_thread_linked(self):
        path = self.rollout()
        self.codex_proc(104, 102, path)
        o = self.owners()[TID]
        self.assertEqual((o['sid'], o['rule']), (P, 'proc'))
        self.assertEqual((o['call'], o['bash_ts'], o['bash_desc'], o['dt']), (None, None, '', None))
        self.assertFalse(o['cwd_ok'] or o['prompt_ok'])
        self.assertEqual(self.links.owned_by(P), {TID: o})

    def test_session_gets_codex_agent_and_cli_agent(self):
        self.child()
        self.session_file(103, C)
        self.proc.add(103, 102, ['claude', '-p', 'x'])
        path = self.rollout()
        self.codex_proc(104, 102, path)
        self.scan()
        s = server.Session(os.path.join(self.proj, P + '.jsonl'))
        s.poll()
        self.assertIn(C, s.agents)
        self.assertEqual(s.agents[C].origin, 'cli')
        a = s.agents[TID]
        self.assertEqual(a.link['rule'], 'proc')
        self.assertEqual(a.spawn_ts, T0 + 2)                                  # the Bash call is unknown, so the time the thread started
        self.assertIsNone(s.state() and None)

    def test_not_exec_or_guardian(self):
        for kw in ({'origin': 'codex-tui'}, {'origin': 'Codex Desktop'}, {'parent': 'x-parent'}):
            path = self.rollout(**kw)
            self.codex_proc(104, 102, path)
            self.assertNotIn(TID, self.owners(), kw)
            self.proc.remove(104)
            os.unlink(path)

    def test_lookalike_process_ignored(self):
        path = self.rollout()
        self.codex_proc(104, 102, path, argv=['codex-view', path])
        self.assertNotIn(TID, self.owners())

    def test_no_claude_ancestor(self):
        path = self.rollout()
        self.codex_proc(104, 1, path)                                         # no parent (a daemon)
        self.proc.add(105, 1, ['/bin/bash', 'x'])
        self.codex_proc(106, 105, path)
        self.assertNotIn(TID, self.owners())

    def test_other_users_claude_ancestor(self):
        path = self.rollout()
        self.codex_proc(104, 102, path)
        self.proc.remove(100)
        self.proc.add(100, 1, ['claude'], uid=UID + 1)
        self.assertNotIn(TID, self.owners())

    def test_rollout_outside_sessions_folder(self):
        other = os.path.join(self.t, 'elsewhere')
        os.makedirs(other)
        p = os.path.join(other, 'rollout-2026-10-01T00-00-00-%s.jsonl' % TID)
        open(p, 'w').close()
        self.rollout()
        self.codex_proc(104, 102, p)
        self.assertNotIn(TID, self.owners())

    def test_remembered_after_exit(self):
        path = self.rollout()
        self.codex_proc(104, 102, path)
        self.assertIn(TID, self.owners())
        for pid in (100, 101, 102, 104):
            self.proc.remove(pid)
        self.assertEqual(self.owners()[TID]['sid'], P)

    def test_unknown_without_proc(self):
        path = self.rollout()
        self.codex_proc(104, 102, path)
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(subprocess, 'run', side_effect=OSError):
            self.assertNotIn(TID, self.owners())
            self.assertIsNone(procs.codex_pids(os.path.join(self.codex, 'sessions')))

    PROMPT = 'Investigate the failing build and write a short report about what you found, thanks'

    def call_in(self, sid, tid='toolu_c', cmd=None):
        self.parent(sid, [bash_line(T0, cmd or 'cd /w && codex exec -m x "%s"' % self.PROMPT, '/w', tid=tid, desc='launch')])

    def test_lineage_beats_prompt_rule_of_other_session(self):
        self.parent(Q)
        path = self.rollout(user=self.PROMPT)
        self.call_in(Q)
        self.assertEqual(self.owners()[TID]['rule'], 'prompt')                # control: with no lineage, the prompt rule (Q)
        self.assertEqual(self.owners()[TID]['sid'], Q)
        self.codex_proc(104, 102, path)
        o = self.owners()[TID]
        self.assertEqual((o['sid'], o['rule']), (P, 'proc'))

    def test_same_session_call_fills_details(self):
        path = self.rollout(user=self.PROMPT)
        self.call_in(P)
        self.codex_proc(104, 102, path)
        o = self.owners()[TID]
        self.assertEqual((o['sid'], o['rule'], o['call'], o['bash_desc']), (P, 'prompt', 'toolu_c', 'launch'))

    def test_pinned_thread_leaves_time_rule_candidates(self):
        """A thread settled by the lineage drops out of the time-rule candidates of the other threads: the one call is left with one candidate instead of two, so it links."""
        self.parent(Q, [bash_line(T0, 'cd /w && codex exec -m x "$Q"', '/w', tid='toolu_c', desc='launch')])
        a = self.rollout(TID, user='one')
        self.rollout(TID2, t=T0 + 3, user='two')
        got = self.owners()
        self.assertEqual({k: (v['sid'], v['rule']) for k, v in got.items()}, {TID: (Q, 'session')})   # control: two candidates, so the time rule cannot decide and the session rule applies; one call starts one thread, the earlier
        self.codex_proc(104, 102, a)
        got = self.owners()
        self.assertEqual((got[TID]['sid'], got[TID]['rule']), (P, 'proc'))
        self.assertEqual((got[TID2]['sid'], got[TID2]['rule']), (Q, 'time'))   # once the thread taken by the lineage is removed, one candidate is left


# =====================================================================================================================
SCRIPT = '#!/bin/bash\ncd "$HOME/proj/$1" || exit 1\nclaude -p "go" --model "$2"\n'


class Scripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.realpath(self.tmp.name)
        self.run_sh = self.put('run.sh', 'echo hi\nclaude -p "x"\n')
        link._SCRIPTS.clear()

    def put(self, name, text, mode=0o755):
        p = os.path.join(self.d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def got(self, cmd, cwd=None):
        return link.bash_scripts(cmd, cwd or self.d)

    def test_run_forms(self):
        p, d = self.run_sh, self.d
        cmds = ['bash %s' % p, 'sh %s' % p, 'zsh %s' % p, 'dash %s' % p, 'bash -e %s' % p, 'bash -o pipefail %s' % p, 'bash -eu %s a b' % p,
                'source %s' % p, '. %s' % p, p, 'nohup bash %s > /dev/null 2>&1 &' % p, 'timeout 600 bash %s' % p, 'FOO=1 %s' % p,
                'cd %s && ./run.sh' % d, 'cd %s; bash run.sh' % d, '(cd %s && ./run.sh)' % d, 'bash -lc "cd %s && ./run.sh"' % d,
                'if %s; then echo ok; fi' % p, 'x=$(bash %s)' % p, 'echo a | bash "%s"' % p, "bash '%s'" % p, 'time %s' % p,
                'ls\ncd %s\nbash run.sh' % d, '/bin/bash %s' % p, 'env A=1 bash %s' % p]
        for c in cmds:
            r = self.got(c, '/nonexistent' if 'cd ' in c else d)
            self.assertEqual([x['path'] for x in r], [p], c)

    def test_relative_to_call_cwd(self):
        r = self.got('bash run.sh', self.d)
        self.assertEqual((r[0]['path'], r[0]['cwd']), (self.run_sh, self.d))   # the script starts in the call's working folder
        self.assertEqual(self.got('cd %s && ./run.sh' % self.d, '/')[0]['cwd'], self.d)
        self.assertEqual(self.got('bash run.sh', '/'), [])                     # the same name in another folder is not it
        self.assertEqual(link.bash_scripts('bash run.sh', None), [])           # without the working folder a relative path cannot be resolved

    def test_not_a_run(self):
        p, d = self.run_sh, self.d
        for c in ('echo "bash %s"' % p, "echo 'source %s'" % p, '# bash %s' % p, 'cat <<EOF\nbash %s\nEOF' % p, "cat <<'EOF'\n%s\nEOF" % p,
                  "bash -c 'echo hi'", 'ls %s' % p, 'cat %s' % p, 'bash --weird %s' % p, 'bash -x -c %s' % p, 'bash $S', 'bash "$D/run.sh"',
                  '(cd /tmp && true); ./run.sh', 'ssh host %s' % p, 'python3 %s' % p, 'bash %s/missing.sh' % d, 'bash -n', 'bash', '',
                  'bash -o noexec %s' % p, 'git commit -m "bash %s"' % p, 'echo bash\\ %s' % p):
            self.assertEqual(self.got(c, '/nonexistent'), [], c)
        self.assertEqual(link.bash_scripts(None, d), [])
        self.assertEqual(link.bash_scripts(['bash', p], d), [])

    def test_only_scripts_that_mention_claude_or_codex(self):
        quiet = self.put('quiet.sh', 'echo hello\nmake all\n')
        self.assertEqual(self.got('bash %s' % quiet), [])
        self.assertEqual(len(self.got('bash %s' % self.put('c.sh', 'codex exec "x"\n'))), 1)

    def test_depth_one_only(self):
        inner = self.put('inner.sh', 'claude -p "x"\n')
        outer = self.put('outer.sh', 'echo claude\nbash %s\nsource %s\n' % (inner, inner))
        r = self.got('bash %s' % outer)
        self.assertEqual([x['path'] for x in r], [outer])                      # only the outer script is read
        links = server.LinkIndex()
        f = {'pos': 0, 'sid': P, 'calls': [], 'cli': []}
        links._cli_line(f, bash_line(T0, 'bash %s' % outer, self.d).encode())
        self.assertEqual(f['cli'], [])                                         # the claude -p in the inner script is not looked at
        f = {'pos': 0, 'sid': P, 'calls': [], 'cli': []}
        links._cli_line(f, bash_line(T0, 'bash %s' % inner, self.d).encode())
        self.assertEqual(len(f['cli']), 1)

    def test_heredoc_in_script_is_not_a_launch(self):
        s = self.put('doc.sh', 'cat <<EOF\nclaude -p "not a launch"\nEOF\n# claude -p x\necho "claude -p y"\n')
        links = server.LinkIndex()
        f = {'pos': 0, 'sid': P, 'calls': [], 'cli': []}
        links._cli_line(f, bash_line(T0, 'bash %s' % s, self.d).encode())
        self.assertEqual(f['cli'], [])

    def test_shebang(self):
        py = self.put('tool.py', '#!/usr/bin/env python3\nprint("claude -p x")\nclaude -p y\n')
        self.assertEqual(self.got(py), [])                                     # a Python file run by path is not a shell script
        self.assertEqual(len(self.got('bash %s' % py)), 1)                     # if a shell was told to read it, it is shell text
        self.assertEqual(len(self.got(self.put('e.sh', '#!/usr/bin/env bash\nclaude -p x\n'))), 1)
        self.assertEqual(len(self.got(self.put('n.sh', 'claude -p x\n'))), 1)  # with no shebang line it is shell

    def test_unreadable_files_are_skipped_silently(self):
        big = self.put('big.sh', 'claude -p x\n' + '#' * link.SCRIPT_MAX)
        binary = self.put('bin.sh', 'claude -p x\n\0\1\2')
        secret = self.put('my_secret_run.sh', 'claude -p x\n')
        hidden = self.put('.config/run.sh', 'claude -p x\n')
        dot = self.put('.hidden.sh', 'claude -p x\n')
        os.symlink(hidden, os.path.join(self.d, 'via-link.sh'))                # a symlink to a file in a hidden folder
        fifo = os.path.join(self.d, 'pipe.sh')
        os.mkfifo(fifo)
        for p in (big, binary, secret, hidden, dot, os.path.join(self.d, 'via-link.sh'), fifo, self.d, '/dev/null', '/proc/self/cmdline'):
            self.assertEqual(self.got('bash %s' % p), [], p)
        self.assertEqual(len(self.got('bash %s' % self.put('ok.sh', 'claude -p x\n' + '#' * 1000))), 1)
        edge = self.put('edge.sh', 'claude -p x\n' + '#' * (link.SCRIPT_MAX - len('claude -p x\n')))      # exactly the limit
        self.assertEqual(len(self.got('bash %s' % edge)), 1)

    def test_changed_file_is_read_again(self):
        self.assertEqual(len(self.got('bash %s' % self.run_sh)), 1)
        self.put('run.sh', 'echo none\n')
        self.assertEqual(self.got('bash %s' % self.run_sh), [])
        self.put('run.sh', 'claude -p "a much longer line now"\n')
        self.assertIn('longer', self.got('bash %s' % self.run_sh)[0]['text'])

    def test_script_env_resolves_home_args_and_assignments(self):
        s = self.put('launch.sh', 'R=$HOME/base\nt=$1; m=$2\nQ="$R/$t/x"\ncd "$HOME/proj/$t" || exit 1\nclaude -p "go" --model "$m"\n')
        with mock.patch.dict(os.environ, {'HOME': '/home/u'}):
            r = self.got('bash %s build opus' % s)[0]
            self.assertEqual(r['env'], {'HOME': '/home/u', '1': 'build', '2': 'opus', 'R': '/home/u/base', 't': 'build', 'm': 'opus',
                                        'Q': '/home/u/base/build/x'})
            links = server.LinkIndex()
            f = {'pos': 0, 'sid': P, 'calls': [], 'cli': []}
            links._cli_line(f, bash_line(T0, 'bash %s build opus' % s, '/elsewhere').encode())
            self.assertEqual([c['cwd'] for c in f['cli']], ['/home/u/proj/build'])
            f = {'pos': 0, 'sid': P, 'calls': [], 'cli': []}
            links._cli_line(f, bash_line(T0, 'bash %s "$X" opus' % s, '/elsewhere').encode())
            self.assertEqual([c['cwd'] for c in f['cli']], [None])             # an argument that cannot be resolved: unknown working folder (it is not taken to match any folder)

    def test_commands_outside_scripts_do_not_get_home_expansion(self):
        """`$HOME` is not expanded in command text (only inside scripts), so that existing links do not change."""
        c = link.cx_parse_call(T0, 'u', {'command': 'codex exec -C "$HOME/x" "p"'}, '/w')
        self.assertIsNone(c['L'][0]['cwd'])

    def test_codex_in_script_r1_and_out_path(self):
        prompt = 'Please investigate the failing build and then write a summary of everything you found today'
        s = self.put('cx.sh', 'R=%s/runs\nt=$1\ncodex exec -C "$HOME/w/$t" -o "$R/$t/last.md" "%s"\necho done\n' % (self.d, prompt))
        with mock.patch.dict(os.environ, {'HOME': '/home/u'}):
            c = link.cx_parse_call(T0, 'tu', {'command': 'bash %s job1' % s, 'description': 'run job'}, '/elsewhere')
        self.assertTrue(c['launch'])
        L = c['L'][0]
        self.assertEqual((L['cwd'], L['resume'], L['literal'] >= link.CX_PROMPT_MIN), ('/home/u/w/job1', None, True))
        thread = {'id': TID, 'origin': 'exec', 'guardian': False, 'meta_ts': T0 + 2, 'first_user': prompt, 'cwd': '/home/u/w/job1'}
        o = link.cx_link([(P, c)], [thread])[TID]
        self.assertEqual((o['sid'], o['rule'], o['call'], o['cwd_ok'], o['prompt_ok']), (P, 'prompt', 'tu', True, True))
        thread2 = dict(thread, id=TID2, cwd='/somewhere/else')                 # a different folder does not link
        self.assertNotIn(TID2, link.cx_link([(P, c)], [thread2]))
        lk = server.CodexLinker(types.SimpleNamespace(id=P, agents={}))
        self.assertEqual(lk._resolve_out(c, L, prompt), os.path.join(self.d, 'runs', 'job1', 'last.md'))   # $R and $t in -o become the script's values

    def test_codex_in_script_time_and_session_rules(self):
        s = self.put('cx2.sh', 'cd /w\ncodex exec "$1"\n')
        c = link.cx_parse_call(T0, 'tu', {'command': 'bash %s do-it' % s}, '/x')
        self.assertTrue(c['launch'])
        thread = {'id': TID, 'origin': 'exec', 'guardian': False, 'meta_ts': T0 + 2, 'first_user': 'unrelated', 'cwd': '/w'}
        self.assertEqual(link.cx_link([(P, c)], [thread])[TID]['rule'], 'time')
        other = dict(thread, cwd='/not-w')
        self.assertEqual(link.cx_link([(P, c)], [other]), {})                  # the cd in the script differs, so it does not link
        late = dict(thread, meta_ts=T0 + 400)
        self.assertEqual(link.cx_link([(P, c)], [late]), {})

    def test_id_in_script_counts_for_launch_calls_only(self):
        s = self.put('res.sh', 'codex exec resume %s "more"\n' % TID)
        c = link.cx_parse_call(T0, 'tu', {'command': 'bash %s' % s}, '/w')
        self.assertEqual(c['resumes'], {TID})
        s2 = self.put('note.sh', 'echo claude %s\n' % TID)                      # the id in a script that has no codex launch is not counted
        self.assertIsNone(link.cx_parse_call(T0, 'tu', {'command': 'bash %s' % s2}, '/w'))

    def test_an_id_in_a_path_is_not_an_id_the_call_names(self):
        """Only `resume <id>` after `codex exec` names a thread. A path with an id in it (a folder named like one, another thread's rollout file) does not."""
        s = self.put(TID2 + '/run.sh', 'codex exec "go"\n')                       # the script sits in a folder named like a thread id
        c = link.cx_parse_call(T0, 'tu', {'command': 'bash %s' % s}, '/w')
        self.assertEqual((c['launch'], c['resumes']), (True, set()))
        c = link.cx_parse_call(T0, 'tu', {'command': 'codex exec "go" > /h/.codex/sessions/rollout-2026-10-01-%s.jsonl' % TID}, '/w')
        self.assertEqual(c['resumes'], set())
        c = link.cx_parse_call(T0, 'tu', {'command': 'codex exec resume %s "go"' % TID}, '/w')
        self.assertEqual(c['resumes'], {TID})
        for cmd in ("bash -c 'codex exec resume %s x'" % TID, 'timeout 9 codex exec -m m resume %s "more"' % TID, 'cd /w && codex exec resume "%s" go' % TID):
            self.assertEqual(link.cx_parse_call(T0, 'tu', {'command': cmd}, '/w')['resumes'], {TID}, cmd)
        for cmd in ('codex exec "please resume %s"' % TID, "echo 'codex exec resume %s'" % TID, 'codex exec go; echo resume %s' % TID, 'codex exec resume go %s' % TID):
            self.assertEqual(link.cx_parse_call(T0, 'tu', {'command': cmd}, '/w')['resumes'], set(), cmd)
        stranger = {'id': TID2, 'origin': 'exec', 'guardian': False, 'meta_ts': T0 + 200, 'first_user': 'x', 'cwd': '/other'}
        named_in_path = link.cx_parse_call(T0, 'tu', {'command': 'codex exec "go"; tail /h/.codex/sessions/rollout-2026-10-01-%s.jsonl' % TID2}, '/w')
        self.assertEqual(link.cx_link([(P, named_in_path)], [stranger]), {})                    # it claimed the thread by the id in the path before

    def test_the_temporary_folder_may_have_ids_in_its_path(self):
        """The tests keep their files in a temporary folder; one under a session's scratch folder has a UUID in its path and nothing may read it as an id."""
        d = tempfile.mkdtemp(prefix=TID + '-')
        try:
            s = os.path.join(d, 'run.sh')
            with open(s, 'w') as fh:
                fh.write('codex exec "go"\n')
            os.chmod(s, 0o755)
            self.assertEqual(link.cx_parse_call(T0, 'tu', {'command': 'bash %s' % s}, '/w')['resumes'], set())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_plain_commands_unchanged(self):
        self.assertIsNone(link.cx_parse_call(1.0, 'u', {'command': 'ls -la'}, '/w'))
        self.assertIsNone(link.cx_parse_call(1.0, 'u', {'command': 'echo codex'}, '/w'))
        self.assertEqual(link.bash_scripts('ls -la', '/w'), [])
        c = link.cx_parse_call(1.0, 'u', {'command': 'codex exec "hello"', 'description': 'run'}, '/w')
        self.assertEqual((c['launch'], len(c['L']), c['assigns']), (True, 1, {}))

    def test_runs_limit(self):
        cmd = '\n'.join('bash %s' % self.run_sh for _ in range(30))
        self.assertEqual(len(self.got(cmd)), link.SCRIPT_RUNS_MAX)


class ScriptScan(Fixture):
    """From the transcript: a call left with only a name (`bash run.sh`) links to the claude -p / codex exec inside the script."""

    def setUp(self):
        Fixture.setUp(self)
        self.sd = os.path.join(self.t, 'scratch')
        os.makedirs(self.sd)

    def script(self, name, text):
        p = os.path.join(self.sd, name)
        with open(p, 'w') as f:
            f.write(text)
        return p

    def test_claude_p_through_script(self):
        s = self.script('go.sh', 'cd "$HOME/proj/$1" || exit 1\nclaude -p hi\n')
        os.makedirs(os.path.join(self.home, 'proj', 'task1'))
        self.parent(P, [bash_line(T0, 'bash %s task1' % s, '/w', tid='toolu_s', desc='run task')])
        self.write(C, child_lines(T0 + 3, os.path.join(self.home, 'proj', 'task1')))
        self.write(D, child_lines(T0 + 3, os.path.join(self.home, 'proj', 'other')))
        with mock.patch.dict(os.environ, {'HOME': self.home}):
            got = self.scan().cli_owners
        self.assertEqual(set(got), {C})                                        # only the one whose folder matches through the cd in the script
        self.assertEqual((got[C]['sid'], got[C]['call'], got[C]['bash_desc']), (P, 'toolu_s', 'run task'))

    def test_line_without_claude_or_codex_words(self):
        s = self.script('x.sh', 'sleep 1; claude -p hi\n')
        self.parent(P, [bash_line(T0, 'cd /w && bash %s' % s, '/w', tid='toolu_s')])      # the line has neither claude nor codex
        self.child()
        self.assertEqual(self.scan().cli_owners[C]['sid'], P)

    def test_codex_through_script_session_rule(self):
        s = self.script('cx.sh', 'cd /w\nfor q in a b; do codex exec "[Q-$q] $(cat q.txt)"; done\n')
        self.parent(P, [bash_line(T0, '%s' % s, '/w', tid='toolu_s')])
        os.chmod(s, 0o755)
        self.rollout(TID, user='[Q-a] x')
        self.rollout(TID2, t=T0 + 3, user='[Q-b] y')
        o = self.scan().owners
        self.assertEqual({k: v['sid'] for k, v in o.items()}, {TID: P, TID2: P})
        self.assertEqual(o[TID]['rule'], 'session')

    def test_script_that_is_not_there_is_silent(self):
        self.parent(P, [bash_line(T0, 'bash %s/gone.sh' % self.sd, '/w')])
        self.child()
        self.assertEqual(self.scan().cli_owners, {})

    def test_noise_does_not_link(self):
        """A call that only mentions a script name (echo, heredoc) is not linked."""
        s = self.script('go.sh', 'claude -p hi\n')
        self.parent(P, [bash_line(T0, 'echo "bash %s"' % s, '/w'), bash_line(T0, 'cat <<EOF\nbash %s\nEOF' % s, '/w', tid='toolu_2')])
        self.child()
        self.assertEqual(self.scan().cli_owners, {})


class CodexExecGuesses(unittest.TestCase):
    """The estimates for a `codex exec` thread (the time rule and the session rule) have the same refutations as the ones for `claude -p`: a call that
    gave a complete literal instruction is not the parent of a thread with other first words; the thread runs in the folder `-C` names; a call that is not a
    loop starts one thread."""
    LONG = 'fix the flaky retry test in the payments module and report back'
    OTHER = 'write a haiku about rust'

    def call(self, cmd, ts=T0, cwd='/w', tid='toolu_c', sid=P):
        return sid, link.cx_parse_call(ts, tid, {'command': cmd, 'description': 'd'}, cwd)

    @staticmethod
    def thread(tid, ts, first, cwd='/w'):
        return {'id': tid, 'origin': 'exec', 'guardian': False, 'meta_ts': ts, 'first_user': first, 'cwd': cwd}

    def linked(self, calls, threads):
        return {t: o['rule'] for t, o in link.cx_link(calls, threads).items()}

    def test_a_stranger_thread_is_not_taken_by_a_call_with_a_complete_literal(self):
        c = self.call('codex exec "%s"' % self.LONG)
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 1, self.LONG), self.thread(TID2, T0 + 5, self.OTHER)]), {TID: 'prompt'})
        self.assertEqual(self.linked([c], [self.thread(TID2, T0 + 5, self.OTHER)]), {})

    def test_the_same_with_a_short_literal(self):
        c = self.call('codex exec "fix lint"')
        self.assertEqual(self.linked([c], [self.thread(TID2, T0 + 5, self.OTHER)]), {})
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 5, 'fix lint')]), {TID: 'time'})
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 5, '  Fix  "lint" ')]), {})                  # other words (case counts), not just other blanks
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 5, '  fix   lint\n')]), {TID: 'time'})

    def test_the_folder_option_still_decides(self):
        c = self.call('codex exec -C /other "fix lint"')
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 5, 'fix lint', '/w')]), {})
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 5, 'fix lint', '/other')]), {TID: 'time'})

    def test_an_instruction_the_call_does_not_give_refutes_nothing(self):
        for cmd in ('codex exec -', 'codex exec', 'codex exec "$Q"', 'codex exec "$(cat p.md)"'):
            c = self.call(cmd)
            self.assertEqual(self.linked([c], [self.thread(TID, T0 + 5, self.OTHER)]), {TID: 'time'}, cmd)

    def test_a_call_that_is_not_a_loop_starts_one_thread(self):
        c = self.call('codex exec "$Q"')
        self.assertEqual(self.linked([c], [self.thread(TID, T0 + 1, 'a'), self.thread(TID2, T0 + 9, 'b')]), {TID: 'session'})
        loop = self.call('for q in a b c; do codex exec "$q"; done')
        self.assertEqual(self.linked([loop], [self.thread(TID, T0 + 1, 'a'), self.thread(TID2, T0 + 9, 'b')]), {TID: 'session', TID2: 'session'})

    def test_a_thread_already_accounted_to_the_call_by_its_words_leaves_no_room_for_a_second(self):
        c = self.call('codex exec "%s"' % self.LONG)
        both = [self.thread(TID, T0 + 1, self.LONG), self.thread(TID2, T0 + 5, self.LONG)]
        self.assertEqual(self.linked([c], both), {TID: 'prompt', TID2: 'prompt'})                            # the same words twice: both say so (the old rule, unchanged)


    def test_a_call_that_is_still_running_can_have_started_a_thread_later_than_the_window(self):
        """`sleep 90 && codex exec "<words>"`: the thread starts 92 s after the call began, while the call is still running. The words decide (the instruction
        rule); the guesses by time keep their 30 s window."""
        words = 'summarise the failing tests of the lagoon module and list every suspect file'
        sid, c = self.call('sleep 90 && codex exec "%s"' % words)
        late = self.thread(TID, T0 + 92, words)
        self.assertEqual(self.linked([(sid, c)], [late]), {})                                               # nothing says the call was still running: the old window
        c['span_end'] = None                                                                                 # still running (link.py fills this in from the call's span)
        self.assertEqual(self.linked([(sid, c)], [late]), {TID: 'prompt'})
        c['span_end'] = T0 + 100
        self.assertEqual(self.linked([(sid, c)], [late]), {TID: 'prompt'})
        c['span_end'] = T0 + 60                                                                              # the call had ended long before the thread began
        self.assertEqual(self.linked([(sid, c)], [late]), {})
        c['span_end'] = None
        self.assertEqual(self.linked([(sid, c)], [self.thread(TID, T0 + 92, self.OTHER)]), {})              # other words: no
        self.assertEqual(self.linked([(sid, c)], [self.thread(TID, T0 + 92 + 7200, words)]), {})            # a call open for hours is not taken for ever
        sid, c = self.call('sleep 90 && codex exec "$Q"')
        c['span_end'] = None
        self.assertEqual(self.linked([(sid, c)], [self.thread(TID, T0 + 92, 'x')]), {})                     # without the words the time rule keeps its window


class CodexTurnCall(unittest.TestCase):
    """CodexLinker._turn_call falls back to "the call that resumed this thread": a thread id in a path of a call (a folder, another rollout file) is not a resume."""

    def linker(self, **call_kw):
        lk = server.CodexLinker(types.SimpleNamespace(id=P, agents={}))
        c = link.cx_parse_call(T0, 'toolu_c', {'command': call_kw.pop('cmd'), 'description': 'd'}, '/w')
        lk.order = [c]
        return lk, c

    def test_a_resume_is_found_and_an_id_in_a_path_is_not(self):
        t = {'user': 'something else', 'start': T0 + 5}
        lk, c = self.linker(cmd='codex exec resume %s "more"' % TID)
        self.assertEqual(lk._turn_call(TID, 1, t), (c, None))
        lk, c = self.linker(cmd='codex exec "go" > /h/.codex/sessions/rollout-2026-10-01-%s.jsonl' % TID)
        self.assertIn(TID, c['ids'])                                                              # the loose reading still sees it ...
        self.assertEqual(lk._turn_call(TID, 1, t), (None, None))                                 # ... the turn does not take it for the call that started it


if __name__ == '__main__':
    unittest.main()
