"""The frozen-HOME tools (tools/regress/home_freeze.py, home_replay.py, home_diff.py; the jail itself is tested in test_home_jail.py), on a synthetic HOME.

 * Capture: the builder (records cut at their size at the moment, stand-ins, listings, the account files never touched), the choice of what is copied, the limits.
 * Replay: a synthetic HOME is served by a real board, frozen (GET only), replayed twice (the projections are the same bytes, no jail_miss), compared with what the live board said (the fidelity gate),
   and nothing outside the output was written. This one needs an interpreter of 3.11 or later: set HOME_TOOLS_PYTHON to one when the test run is older.
 * Diff: the lines that decide the release, with 0.3.0 projections written by hand in the field names of CONTRACT 2.7 (there is no 0.3.0 judgment yet): the cells the tools wrote keep their owner,
   no new confirmed falsehood, the reasons of what is lost, the expected losses, the two readings of D1.

    python3 -m unittest tests.test_home_tools -q
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'regress'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import isolated_env, listen_port, read_output  # noqa: E402
import home_diff  # noqa: E402
import home_freeze  # noqa: E402
import home_replay  # noqa: E402
import synth_home  # noqa: E402

SERVER = os.path.join(ROOT, 'server.py')


def newer_python():
    """An interpreter of 3.11 or later (the replay needs it): this one, HOME_TOOLS_PYTHON, or python3.12 / python3.11 on the PATH."""
    if sys.version_info >= (3, 11):
        return sys.executable
    for cand in (os.environ.get('HOME_TOOLS_PYTHON'), shutil.which('python3.12'), shutil.which('python3.11')):
        if cand and os.path.isfile(cand):
            return cand
    return None


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def tree_hash(folder):
    """{relative path: sha1} of every file under a folder (to see that nothing was written)."""
    out = {}
    for dirpath, dirs, names in os.walk(folder):
        for n in names:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                out[os.path.relpath(p, folder)] = 'link:' + os.readlink(p)
            else:
                with open(p, 'rb') as f:
                    out[os.path.relpath(p, folder)] = hashlib.sha1(f.read()).hexdigest()
    return out


# ---------- the capture builder ----------
class CaptureBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='home-freeze-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live, self.out = os.path.join(self.tmp, 'live'), os.path.join(self.tmp, 'out')
        os.makedirs(self.live)

    def put(self, rel, data):
        p = os.path.join(self.live, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return p

    def test_a_record_is_cut_at_its_size_at_the_moment(self):
        rec = self.put('projects/p/sid.jsonl', '{"a":1}\n{"b":2}\n')
        size = os.path.getsize(rec)
        with open(rec, 'a') as f:
            f.write('{"appended after the capture":true}\n')
        cap = home_freeze.Capture(self.out)
        self.assertTrue(cap.add_record(rec, size, 'sid', 'main'))
        cap.write_disk((os.path.join(self.live, 'projects'),))
        with open(os.path.join(self.out, 'records' + rec), 'rb') as f:
            self.assertEqual(f.read(), b'{"a":1}\n{"b":2}\n')
        e = read_json(os.path.join(self.out, 'disk.json'))['entries']
        self.assertEqual(e[rec]['size'], size)                                                        # a stat of the replay says what the board saw
        self.assertEqual(e[rec]['store'], 'records' + rec)
        self.assertEqual(cap.records[rec], {'size': size, 'session': 'sid', 'kind': 'main'})

    def test_the_record_folders_list_only_what_was_captured(self):
        a = self.put('projects/p/a.jsonl', 'x')
        self.put('projects/p/not-captured.jsonl', 'y')
        self.put('projects/other/b.jsonl', 'z')
        cap = home_freeze.Capture(self.out)
        cap.add_record(a, 1, 'a', 'main')
        cap.write_disk((os.path.join(self.live, 'projects'),))
        e = read_json(os.path.join(self.out, 'disk.json'))['entries']
        self.assertEqual(e[os.path.join(self.live, 'projects')]['names'], ['p'])
        self.assertEqual(e[os.path.join(self.live, 'projects', 'p')]['names'], ['a.jsonl'])
        self.assertNotIn(os.path.join(self.live, 'projects', 'other'), e)

    def test_other_files_are_stand_ins_unless_their_bytes_were_wanted(self):
        doc = self.put('work/r1/A.md', 'report text')
        big = self.put('work/data.bin', b'\0' * 100000)
        small = self.put('work/note.txt', 'note')
        cap = home_freeze.Capture(self.out)
        for p in (doc, big, small):
            cap.ensure(p)
        cap.content(doc)
        cap.write_disk(())
        e = read_json(os.path.join(self.out, 'disk.json'))['entries']
        self.assertTrue(e[doc]['copied'])
        self.assertEqual(read_text(os.path.join(self.out, e[doc]['store'])), 'report text')
        for p in (big, small):
            self.assertFalse(e[p]['copied'])                                                           # a read of it is a miss in the replay
            self.assertEqual(os.path.getsize(os.path.join(self.out, e[p]['store'])), e[p]['size'])    # the stat is right (sparse: it costs no disk)
        self.assertLess(cap.copied, 1000)

    def test_a_file_too_big_to_keep_is_a_stand_in_and_is_said(self):
        big = self.put('work/big.md', b'x' * 5000)
        cap = home_freeze.Capture(self.out, max_file=1000)
        cap.content(big)
        self.assertEqual(cap.stand_ins_read, [big])
        self.assertFalse(cap.entries[big]['copied'])

    def test_the_limit_stops_the_copy(self):
        rec = self.put('projects/p/sid.jsonl', 'x' * 5000)
        cap = home_freeze.Capture(self.out, limit=1000)
        with self.assertRaises(home_freeze.LimitExceeded):
            cap.add_record(rec, 5000, 'sid', 'main')

    def test_absent_paths_are_remembered_as_absent(self):
        cap = home_freeze.Capture(self.out)
        p = os.path.join(self.live, 'nothing', 'here.md')
        self.assertIsNone(cap.ensure(p))
        self.assertEqual(cap.entries[os.path.join(self.live, 'nothing')]['t'], 'absent')

    def test_links_are_followed_to_their_target_and_kept_as_links(self):
        real = self.put('data/real.md', 'x')
        os.symlink(real, os.path.join(self.live, 'abs'))
        os.symlink('data', os.path.join(self.live, 'rel'))
        cap = home_freeze.Capture(self.out)
        self.assertEqual(cap.ensure(os.path.join(self.live, 'abs')), real)
        self.assertEqual(cap.ensure(os.path.join(self.live, 'rel', 'real.md')), real)
        self.assertEqual(cap.entries[os.path.join(self.live, 'abs')]['t'], 'link')
        self.assertEqual(cap.entries[os.path.join(self.live, 'abs')]['target'], real)
        self.assertEqual(cap.entries[os.path.join(self.live, 'rel')]['target'], 'data')

    def test_the_root_and_a_listing_of_it_do_not_break_the_walk(self):
        cap = home_freeze.Capture(self.out)
        self.assertEqual(cap.ensure('/'), '/')
        self.assertEqual(cap.ensure('/tmp/..'), '/')
        cap.listing('/', with_children=False)                                                          # a trace may have listed it
        self.assertIsNotNone(cap.entries['/']['names'])

    def test_the_account_files_are_never_looked_at(self):
        for rel in ('.claude/.credentials.json', '.codex/auth.json', '.codex/state.sqlite', '.claude.json'):
            p = self.put(rel, 'SECRET')
            cap = home_freeze.Capture(self.out)
            self.assertIsNone(cap.content(p))
            cap.listing(os.path.dirname(p))
        cap.write_disk(())
        for dirpath, _, names in os.walk(self.out):
            for n in names:
                with open(os.path.join(dirpath, n), 'rb') as f:
                    self.assertNotIn(b'SECRET', f.read())
        names = read_json(os.path.join(self.out, 'disk.json'))['entries']
        self.assertTrue(all(os.path.basename(k) not in ('.credentials.json', 'auth.json', 'state.sqlite', '.claude.json') for k in names))

    def test_the_closure_follows_children_and_codex_descendants(self):
        projects, sessions = os.path.join(self.live, 'projects'), os.path.join(self.live, 'sessions')
        sid, child = 'a' * 8 + '-0000-4000-8000-000000000001', 'b' * 8 + '-0000-4000-8000-000000000002'
        main = self.put('projects/p/%s.jsonl' % sid, '{}\n')
        self.put('projects/p/%s/subagents/agent-x.jsonl' % sid, '{}\n')
        self.put('projects/p/%s/subagents/agent-x.meta.json' % sid, '{}')
        kid = self.put('projects/p/%s.jsonl' % child, '{}\n')
        t_root, t_sub, t_guard, t_other = ('0000000%d-0000-4000-8000-00000000000%d' % (i, i) for i in range(1, 5))
        for tid, parent in ((t_root, None), (t_sub, t_root), (t_guard, t_sub), (t_other, None)):
            meta = {'payload': {'id': tid}}
            if parent:
                meta['payload']['parent_thread_id'] = parent
            self.put('sessions/2026/10/07/rollout-2026-10-07T00-00-00-%s.jsonl' % tid, json.dumps(meta) + '\n')
        c_index = home_freeze.claude_index(projects)
        x_paths, x_parents = home_freeze.codex_index(sessions)
        state = {'agents': [{'id': child}, {'id': t_root}, {'id': 'agent-not-a-record'}]}
        files = home_freeze.closure(sid, state, c_index, x_paths, x_parents)
        paths = {p for p, _ in files}
        self.assertIn(main, paths)
        self.assertIn(kid, paths)
        self.assertEqual(sum(1 for p in paths if p.endswith('subagents/agent-x.jsonl') or p.endswith('subagents/agent-x.meta.json')), 2)
        codex = {os.path.basename(p) for p, k in files if k == 'codex'}
        self.assertEqual(codex, {'rollout-2026-10-07T00-00-00-%s.jsonl' % t for t in (t_root, t_sub, t_guard)})      # the descendants come along, the stranger does not

    def test_out_may_not_be_inside_the_repository_or_the_claude_folder(self):
        for bad in (os.path.join(ROOT, 'capture-here'), os.path.join(self.live, '.claude', 'cap')):
            os.makedirs(os.path.join(self.live, '.claude'), exist_ok=True)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    home_freeze.main(['--out', bad, '--home', self.live, '--port', '9'])
            self.assertFalse(os.path.exists(os.path.join(ROOT, 'capture-here')))


class LinkCacheCopy(unittest.TestCase):
    def test_the_copy_is_private_like_the_live_boards_own(self):
        """The board reads a link cache only when the folder and the file are the user's and nobody else can write them (lineage._cache_trusted). A capture made with a umask of 002 holds a
        group-writable one: the run's copy must not be, or every remembered link is silently left out (found on a real capture: a third of the agents were missing)."""
        tmp = tempfile.mkdtemp(prefix='home-cache-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = os.path.join(tmp, 'cap-cache')
        os.makedirs(os.path.join(src, 'agent-bullpen'))
        f = os.path.join(src, 'agent-bullpen', 'links.json')
        with open(f, 'w') as g:
            g.write('{"version": 6, "links": []}')
        os.chmod(os.path.join(src, 'agent-bullpen'), 0o775)
        os.chmod(f, 0o664)
        dst = os.path.join(tmp, 'run-cache')
        home_replay.fresh_cache(src, dst)
        for p in (dst, os.path.join(dst, 'agent-bullpen'), os.path.join(dst, 'agent-bullpen', 'links.json')):
            self.assertEqual(os.stat(p).st_mode & 0o077, 0, p)
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o664)                                                        # the capture itself is not touched
        home_replay.fresh_cache(os.path.join(tmp, 'no-cache-in-the-capture'), os.path.join(tmp, 'run-cache-2'))      # a capture with no cache gets an empty private folder
        self.assertEqual(os.listdir(os.path.join(tmp, 'run-cache-2')), [])


# ---------- the projection ----------
STATE_021 = {
    'debates': [{'root': '/w/review', 'current': True, 'topics': [
        {'dir': '/w/review/t1', 'key': 't1', 'room': 'cells', 'final': {'path': '/w/review/t1/rulings.md', 'exists': True},
         'rows': [{'p': 'A', 'cells': [{'path': '/w/review/t1/r1/A.md', 'agent': 'ag-a', 'state': 'done', 'planned': False}, {'path': '/w/review/t1/r2/A.md', 'agent': 'ag-a', 'state': 'waiting', 'planned': True}]},
                  {'p': 'B', 'cells': [{'path': '/w/review/t1/r1/B.md', 'agent': None, 'state': 'done'}]}]},
        {'dir': '/w/review/t2', 'key': 't2', 'final': {'path': None, 'exists': False}, 'rows': []}], 'finals': []}],
    'agents': [{'id': 'ag-a', 'status': 'done', 'tag': 'A', 'units': ['/w/review/t1'], 'work_units': ['/w/review/t1', '/w/review/t2']}], 'diag': {'n': 2, 'by_code': {'x': 2}}}
STATE_030 = {
    'debates': [{'root': '/w/review', 'current': True, 'sure': True, 'final': {'confirmed': True, 'exists': True, 'path': '/w/review/final.md', 'why': []}, 'topics': [
        {'dir': '/w/review/t1', 'key': 't1', 'room': 'cells', 'room_sure': False, 'final': {'confirmed': False, 'exists': False, 'path': None, 'why': ['open_cell'], 'by': None},
         'rows': [{'p': 'A', 'cells': [{'path': '/w/review/t1/r1/A.md', 'agent': 'ag-a', 'owner': 'ag-a', 'editors': [], 'evidence': 'tool', 'hint': None, 'state': 'done', 'previous': False}]}]}]}],
    'agents': [{'id': 'ag-a', 'status': 'done', 'tag': 'A', 'room_tag': {'room': '~/w/review', 'seat': 'A'}, 'placed': None, 'units': ['/w/review/t1'], 'work_units': ['/w/review/t1']}], 'diag': {'n': 0, 'by_code': {}}}


class Projection(unittest.TestCase):
    def test_the_projection_of_a_0_2_1_state(self):
        p = home_replay.project_state(STATE_021, {'ag-a': {'writes': [{'path': '/w/review/t1/r1/A.md'}], 'tag': 'A', 'room_tag': {'room': '~/w/review', 'seat': 'A'}, 'junk': 1}})
        self.assertEqual(p['roots'], ['/w/review'])
        self.assertEqual(p['current'], ['/w/review'])
        self.assertEqual(p['rooms'], {'/w/review|/w/review/t1': {'kind': 'cells', 'sure': True, 'members': ['A', 'B']}})
        self.assertEqual(p['cells']['/w/review|/w/review/t1|r1|A'], {'path': '/w/review/t1/r1/A.md', 'agent': 'ag-a', 'state': 'done', 'planned': False})
        self.assertEqual(p['cells']['/w/review|/w/review/t1|r1|B']['agent'], None)
        self.assertEqual(p['finals']['/w/review|/w/review/t1'], {'path': '/w/review/t1/rulings.md', 'exists': True})
        self.assertEqual(p['finals']['/w/review|/w/review/t2'], {'path': None, 'exists': False})
        self.assertEqual(p['agents']['ag-a']['work_units'], ['/w/review/t1', '/w/review/t2'])
        self.assertEqual(p['agents']['ag-a']['detail'], {'writes': [{'path': '/w/review/t1/r1/A.md'}], 'room_tag': {'room': '~/w/review', 'seat': 'A'}})        # only the named detail fields: the room tag (O16), not the letter `tag`
        self.assertIsNone(p['agents']['ag-a']['room_tag'])                                                              # 0.2.1 has no room tag
        self.assertEqual(p['diag_n'], 2)

    def test_the_projection_of_a_0_3_0_state_uses_the_2_7_names(self):
        p = home_replay.project_state(STATE_030)
        cell = p['cells']['/w/review|/w/review/t1|r1|A']
        self.assertEqual((cell['owner'], cell['agent'], cell['editors'], cell['evidence'], cell['hint'], cell['previous']), ('ag-a', 'ag-a', [], 'tool', None, False))
        self.assertEqual(p['rooms']['/w/review|/w/review/t1'], {'kind': 'cells', 'sure': False, 'members': ['A']})
        self.assertEqual(p['finals']['/w/review|/w/review/t1'], {'path': None, 'exists': False, 'confirmed': False, 'why': ['open_cell'], 'by': None})
        self.assertEqual(p['root_finals']['/w/review'], {'path': '/w/review/final.md', 'exists': True, 'confirmed': True, 'why': []})
        self.assertEqual(p['sure'], {'/w/review': True})
        self.assertEqual(p['agents']['ag-a']['room_tag'], {'room': '~/w/review', 'seat': 'A'})                          # the tag of the room, not the letter the screen names the agent by
        self.assertNotIn('tag', p['agents']['ag-a'])


# ---------- the whole way on a synthetic HOME ----------
class FreezeAndReplay(unittest.TestCase):
    """A board on a synthetic HOME, frozen over HTTP (GET only), replayed twice with the code of this repository."""

    @classmethod
    def setUpClass(cls):
        cls.py = newer_python()
        if cls.py is None:
            raise unittest.SkipTest('the replay needs Python 3.11 or later (set HOME_TOOLS_PYTHON)')
        cls.tmp = tempfile.mkdtemp(prefix='home-e2e-')
        cls.home = os.path.join(cls.tmp, 'home')
        cls.info = synth_home.build(cls.home)
        cls.live_before = tree_hash(cls.info['home'])
        cls.log = tempfile.TemporaryFile('w+')
        cls.proc = subprocess.Popen([sys.executable, SERVER, '--port', '0'], stdout=cls.log, stderr=subprocess.STDOUT, env=isolated_env(cls.info['home']), cwd=ROOT, stdin=subprocess.DEVNULL)
        end, cls.port = time.time() + 60, None
        while time.time() < end and cls.port is None and cls.proc.poll() is None:
            cls.port = listen_port(read_output(cls.log))
            time.sleep(0.05)
        if cls.port is None:
            cls.tearDownClass()
            raise AssertionError('the board did not come up')
        for _ in range(100):                                                                                           # the session is read at start; wait until it is listed
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/api/sessions' % cls.port, timeout=30) as r:
                    if any(s['id'] == cls.info['orch'] for s in json.load(r)['sessions']):
                        break
            except OSError:
                pass
            time.sleep(0.1)
        cls.cap = os.path.join(cls.tmp, 'cap')
        argv = ['--out', cls.cap, '--port', str(cls.port), '--home', cls.info['home'], '--python', cls.py, '--trace-code', ROOT, '--board-top', ROOT, '--cache-home', os.path.join(cls.info['home'], '.cache'), '--no-proc-env']
        with contextlib.redirect_stdout(io.StringIO()) as cls.freeze_out:
            cls.freeze_code = home_freeze.main(argv)

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, 'proc', None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if getattr(cls, 'log', None) is not None:
            cls.log.close()
        shutil.rmtree(getattr(cls, 'tmp', ''), ignore_errors=True)

    def replay(self, name):
        out = os.path.join(self.tmp, name)
        r = subprocess.run([self.py, '-I', os.path.join(ROOT, 'tools', 'regress', 'home_replay.py'), '--code', ROOT, '--cap', self.cap, '--out', out], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(r.returncode, 0, r.stdout.decode('utf-8', 'replace')[-3000:])
        return out

    def test_the_capture_holds_what_was_asked(self):
        self.assertEqual(self.freeze_code, 0)
        manifest = read_json(os.path.join(self.cap, 'manifest.json'))
        self.assertEqual([s['id'] for s in manifest['sessions']], [self.info['orch']])
        self.assertEqual(manifest['unstable'], [])
        self.assertTrue(manifest['trace_rounds'] and manifest['trace_rounds'][-1]['added'] == 0)                       # the last trace run found nothing new
        self.assertEqual(manifest['trace_rounds'][-1]['jail_miss'], 0)
        for name in ('disk.json', 'proc.json', 'manifest.json'):
            self.assertTrue(os.path.isfile(os.path.join(self.cap, name)))
        self.assertTrue(os.path.isfile(os.path.join(self.cap, 'api_live', self.info['orch'], 'state.json')))
        recs = manifest['records']
        self.assertTrue(any(p.endswith(self.info['orch'] + '.jsonl') for p in recs))
        self.assertTrue(any('/subagents/' in p for p in recs))
        for p, r in recs.items():                                                                                      # every record is in the mirror at exactly its size
            self.assertEqual(os.path.getsize(os.path.join(self.cap, 'records' + p)), r['size'])
        for dirpath, _, names in os.walk(self.cap):                                                                    # no account file anywhere
            for n in names:
                self.assertNotIn(n, ('.credentials.json', 'auth.json', '.claude.json'))

    def test_the_live_home_was_not_written(self):
        # the board itself may write its link cache; the synthetic HOME's records, folders and files the freeze and replays read must be as they were
        after = tree_hash(self.info['home'])
        changed = {k for k in set(after) | set(self.live_before) if after.get(k) != self.live_before.get(k)}
        self.assertEqual({k for k in changed if not k.startswith('.cache/')}, set())

    def test_two_replays_are_the_same_bytes_and_miss_nothing(self):
        a, b = self.replay('r1.json'), self.replay('r2.json')
        with open(a, 'rb') as f, open(b, 'rb') as g:
            self.assertEqual(f.read(), g.read())
        self.assertEqual([m for m in read_json(a + '.jail_miss.json') if m['kind'] != 'forbidden_stat'], [])
        res = read_json(a)
        self.assertEqual(res['jail_miss_count'], 0)
        row = res['sessions'][self.info['orch']]
        self.assertIsNone(row['error'])
        self.assertTrue(row['projection']['cells'])
        perf = read_json(a + '.perf.json')[self.info['orch']]                                                            # the times are apart from the projection
        self.assertEqual(sorted(perf), ['details_agents', 'details_sec', 'open_sec', 'state_cold_sec', 'state_warm_sec', 'walk_sec'])
        self.assertGreater(perf['details_agents'], 0)                                                                    # /api/agent was asked for the agents, and that time is not in the state's

    def test_details_none_times_the_judgment_alone(self):
        out = os.path.join(self.tmp, 'nodetails.json')
        r = subprocess.run([self.py, '-I', os.path.join(ROOT, 'tools', 'regress', 'home_replay.py'), '--code', ROOT, '--cap', self.cap, '--out', out, '--details', 'none', '--no-raw'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(r.returncode, 0, r.stdout.decode('utf-8', 'replace')[-2000:])
        row = read_json(out)['sessions'][self.info['orch']]
        self.assertTrue(row['projection']['cells'])
        self.assertTrue(all('detail' not in a for a in row['projection']['agents'].values()))                              # no /api/agent at all
        self.assertEqual(read_json(out + '.perf.json')[self.info['orch']]['details_agents'], 0)

    def test_the_replay_matches_the_live_board_and_the_gate_says_so(self):
        a = self.replay('gate.json')
        r = subprocess.run([self.py, '-I', os.path.join(ROOT, 'tools', 'regress', 'home_diff.py'), '--cap', self.cap, '--a', a, '--fidelity', '--report-dir', self.tmp], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout.decode())
        res = read_json(os.path.join(self.tmp, 'fidelity.json'))
        self.assertEqual((res['differing'], res['unexplained']), ([], []))
        self.assertGreater(res['total'], 10)
        self.assertTrue(res['passed'])

    def test_extending_a_capture_adds_a_round_and_nothing_else_when_nothing_is_new(self):
        before_disk = tree_hash(self.cap)
        before = read_json(os.path.join(self.cap, 'manifest.json'))
        with contextlib.redirect_stdout(io.StringIO()):
            code = home_freeze.main(['--extend', self.cap, '--python', self.py, '--trace-code', ROOT])
        self.assertEqual(code, 0)
        after = read_json(os.path.join(self.cap, 'manifest.json'))
        self.assertEqual(len(after['trace_rounds']), len(before['trace_rounds']) + 1)
        self.assertEqual((after['trace_rounds'][-1]['added'], after['trace_rounds'][-1]['jail_miss']), (0, 0))
        changed = {k for k, v in tree_hash(self.cap).items() if before_disk.get(k) != v and not k.startswith('trace/') and k != 'manifest.json'}
        self.assertEqual(changed, set())                                                                               # the records, the mirror and disk.json are as they were

    def test_the_replay_writes_only_its_output(self):
        before = tree_hash(self.cap)
        out = self.replay('writes.json')
        self.assertEqual(tree_hash(self.cap), before)                                                                  # the capture (mirror and caches) is not changed
        left = [n for n in os.listdir(self.tmp) if n.startswith('writes.json')]
        self.assertEqual(sorted(left), ['writes.json', 'writes.json.jail_miss.json', 'writes.json.perf.json', 'writes.json.raw'])      # and the run's own folder is gone


# ---------- the lines that decide the release ----------
def proj(cells=None, rooms=None, finals=None, roots=None, agents=None, root_finals=None, sure=None, diag_codes=None, diag_n=0, diag_items=None):
    return {'roots': roots if roots is not None else ['R'], 'current': [], 'rooms': rooms or {}, 'cells': cells or {}, 'finals': finals or {}, 'root_finals': root_finals or {},
            'sure': sure or {}, 'agents': agents or {}, 'diag_n': diag_n, 'diag_codes': diag_codes or {}, 'diag_items': diag_items or []}


def a_cell(agent, path='/w/t/r1/X.md', state='done', planned=False):
    return {'path': path, 'agent': agent, 'state': state, 'planned': planned}


def b_cell(owner, agent=None, path='/w/t/r1/X.md', state='done', editors=(), hint=None, evidence='tool', previous=False):
    return {'path': path, 'owner': owner, 'agent': agent or owner, 'editors': list(editors), 'evidence': evidence, 'hint': hint, 'state': state, 'previous': previous}


def events(*paths, ts=100, evidence='tool', ok=True, sure=True):
    return {'detail': {'write_events': [{'ts': ts + i, 'path': p, 'evidence': evidence, 'ok': ok, 'sure': sure, 'kind': 'create', 'proof': 'exit'} for i, p in enumerate(paths)]}}


KEY = 'R|T|r1|X'


class ToolWrites(unittest.TestCase):
    """Line 1: a cell whose agent has a successful tool write on its path keeps that owner in 0.3.0; else only editor_first, listed."""

    def sessions(self, a_cells, b_cells, b_agents):
        A, B = {'s1': proj(cells=a_cells)}, {'s1': proj(cells=b_cells, agents=b_agents)}
        return home_diff.check_tool_writes(A, B, ['s1'])

    def test_the_same_owner_passes(self):
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell('x')}, {'x': events('/w/t/r1/X.md')})
        self.assertEqual((r['baseline'], r['same'], r['violations'], r['editor_first']), (1, 1, [], []))

    def test_a_cell_without_a_tool_write_is_not_in_the_baseline(self):
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell(None, agent='x', evidence=None)}, {'x': events('/other.md')})
        self.assertEqual((r['baseline'], r['violations']), (0, []))
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell(None, agent='x')}, {'x': events('/w/t/r1/X.md', evidence='shell')})        # a shell write is not W1
        self.assertEqual(r['baseline'], 0)
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell(None)}, {'x': events('/w/t/r1/X.md', ok=False)})                                # a failed one neither
        self.assertEqual(r['baseline'], 0)

    def test_another_owner_is_allowed_only_as_editor_first(self):
        # 0.2.1 gave the cell to x, who wrote it at 200; y wrote it first (at 100): editor_first
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell('y', editors=['x'])}, {'x': events('/w/t/r1/X.md', ts=200), 'y': events('/w/t/r1/X.md', ts=100)})
        self.assertEqual((len(r['editor_first']), r['violations']), (1, []))
        self.assertEqual((r['editor_first'][0]['a_agent'], r['editor_first'][0]['b_owner']), ('x', 'y'))
        # y wrote it later: not the first writer, so it is a violation
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell('y')}, {'x': events('/w/t/r1/X.md', ts=100), 'y': events('/w/t/r1/X.md', ts=200)})
        self.assertEqual((r['editor_first'], len(r['violations'])), ([], 1))

    def test_flat_review_is_the_accepted_exception_and_not_a_failure(self):
        """O16: the cells of a folder of files with no round folder that 0.3.0 does not list are counted apart (PLAN 4 accepted that loss); a lost cell of a topic 0.3.0 does list is still a violation."""
        flat = 'R|T9||f'
        a = {'s': proj(cells={flat: a_cell('x', path='/w/t9/f.md'), KEY: a_cell('x')})}
        ev = events('/w/t9/f.md', '/w/t/r1/X.md')['detail']['write_events']
        b = {'s': proj(cells={KEY: b_cell(None, agent='x', evidence=None, state='previous')}, agents={'x': {'detail': {'write_events': ev}}})}
        r = home_diff.check_tool_writes(a, b, ['s'])
        self.assertEqual([x['cell'] for x in r['accepted']], [flat])
        self.assertEqual([x['reason'] for x in r['accepted']], ['flat_review'])
        self.assertEqual([x['cell'] for x in r['violations']], [KEY])                                                     # the other cell is a topic 0.3.0 lists: a violation
        res = home_diff.compare(a, b)
        self.assertEqual((res['line1']['baseline'], len(res['line1']['accepted']), len(res['line1']['violations'])), (2, 1, 1))
        only = home_diff.compare({'s': proj(cells={flat: a_cell('x', path='/w/t9/f.md')})}, {'s': proj(agents={'x': {'detail': {'write_events': ev[:1]}}})})
        self.assertTrue(only['passed']['line1'])                                                                           # nothing but the accepted exception: not a failure
        self.assertEqual(len(only['line1']['accepted']), 1)
        self.assertIn('accepted flat_review (O16) 1, violations 0', home_diff.render_report(only))
        self.assertIn('accepted (O16)', home_diff.render_report(only))

    def test_no_owner_or_no_cell_is_a_violation(self):
        r = self.sessions({KEY: a_cell('x')}, {KEY: b_cell(None, agent='x')}, {'x': events('/w/t/r1/X.md')})
        self.assertEqual(len(r['violations']), 1)
        r = self.sessions({KEY: a_cell('x')}, {}, {'x': events('/w/t/r1/X.md')})
        self.assertEqual(len(r['violations']), 1)
        self.assertEqual(r['violations'][0]['b_owner'], None)


class NewConfirmed(unittest.TestCase):
    """Line 2: every confirmed item of 0.3.0 that 0.2.1 had not is in truth.json as true; false or unmarked stops the release."""

    def setUp(self):
        self.A = {'aaaa11110': proj(cells={KEY: a_cell('x')}, rooms={'R|T1': {'kind': 'cells', 'sure': True, 'members': ['x']}}, finals={'R|T1': {'exists': False, 'path': None}})}
        self.B = {'aaaa11110': proj(cells={KEY: b_cell('x'), 'R|T|r2|Q-late': b_cell('Q-late', path='/w/t/r2/Q-late.md'), 'R|T|r1|C': b_cell(None, agent='x')},
                                    rooms={'R|T1': {'kind': 'cells', 'sure': True, 'members': ['x']}, 'R|T9': {'kind': 'members', 'sure': True, 'members': ['p']}, 'R|T8': {'kind': 'members', 'sure': False, 'members': []}},
                                    finals={'R|T1': {'exists': True, 'confirmed': True, 'path': '/w/t1/final.md', 'why': []}, 'R|T2': {'exists': False, 'confirmed': False, 'path': None, 'why': ['no_report']}},
                                    roots=['R', 'R2'], sure={'R2': True})}

    def test_only_what_is_new_and_confirmed_is_listed(self):
        items = home_diff.new_confirmed(self.A, self.B, ['aaaa11110'])
        self.assertEqual(sorted((i['kind'], i['key']) for i in items), [('cell', 'R|T|r2|Q-late'), ('final', 'R|T1'), ('room', 'R|T9'), ('root', 'R2')])
        # the same owner as 0.2.1, an unowned cell, and an estimated room are not new confirmed items

    def test_truth_marks_decide(self):
        items = home_diff.new_confirmed(self.A, self.B, ['aaaa11110'])
        truth = {'items': [{'session': 'aaaa1111', 'kind': 'cell', 'key': '*|r2|Q-late', 'owner': 'Q-late', 'verdict': 'true', 'why': 'W2: the file was written by the shell of Q-late'},
                           {'session': 'aaaa1111', 'kind': 'room', 'key': 'R|T9', 'verdict': 'false'},
                           {'session': 'other', 'kind': 'final', 'key': 'R|T1', 'verdict': 'true'}]}
        home_diff.mark(items, truth)
        by = {i['key']: i['verdict'] for i in items}
        self.assertEqual(by, {'R|T|r2|Q-late': 'true', 'R|T9': 'false', 'R|T1': None, 'R2': None})                         # a mark of another session does not count
        self.assertEqual(next(i['why'] for i in items if i['key'] == 'R|T|r2|Q-late'), 'W2: the file was written by the shell of Q-late')

    def test_the_release_is_stopped_by_a_false_or_an_unmarked_item(self):
        truth = {'items': [{'session': 'aaaa', 'kind': k, 'key': '*', 'verdict': 'true'} for k in ('cell', 'room', 'final', 'root')]}
        res = home_diff.compare(self.A, self.B, truth)
        self.assertTrue(res['passed']['line2'])
        self.assertEqual(res['line2'], {'total': 4, 'true': 4, 'false': [], 'unmarked': []})
        truth['items'][1]['verdict'] = 'false'
        self.assertFalse(home_diff.compare(self.A, self.B, truth)['passed']['line2'])
        self.assertFalse(home_diff.compare(self.A, self.B, None)['passed']['line2'])                                        # no truth.json: everything is unmarked


class LostReasons(unittest.TestCase):
    """Line 3: what 0.2.1 had and 0.3.0 has not, by reason (the first rule that holds)."""

    def lost(self, a, b):
        return {(i['kind'], i['key']): i['reason'] for i in home_diff.lost_items({'s': a}, {'s': b}, ['s'])}

    def test_cells(self):
        a = proj(cells={'R|T|r1|P': a_cell('x', planned=True, state='waiting'), 'R|T|r1|Q': a_cell('x', state='waiting'), 'R|T|r1|H': a_cell('x'), 'R|T|r1|E': a_cell('x'), 'R|T|r1|U': a_cell('x'),
                        'R|T|r1|G': a_cell(None, state='done'), 'R|T|r1|K': a_cell('x'), 'R|T|r1|W': a_cell('x'), 'R|T|r1|Z': a_cell('x', state='done', path='/w/t/r1/Z.md'),
                        'R|T|r1|N': a_cell(None, state='waiting'), 'R|T|r1|V': a_cell('x', path='/w/t/r1/V.md'), 'R|T|r1|L': a_cell('x', path='/w/t/r1/L.md'), 'R|T|r1|D': a_cell('x', path='/w/t/r1/D.md')})
        b = proj(cells={'R|T|r1|H': b_cell(None, agent='x', hint={'kind': 'window', 'agent': 'x'}, evidence=None, state='previous'),
                        'R|T|r1|E': b_cell(None, agent='x', editors=['x'], evidence=None, state='previous'),
                        'R|T|r1|U': b_cell(None, agent='x', path='/w/t/r1/U.md', evidence=None, state='previous'),
                        'R|T|r1|G': b_cell(None, agent=None, evidence=None, state='previous'),
                        'R|T|r1|K': b_cell('y', editors=['x']),
                        'R|T|r1|W': b_cell(None, agent='x', evidence='planned', state='missing'),
                        'R|T|r1|Z': b_cell(None, agent='x', evidence=None, state='previous'),
                        'R|T|r1|V': b_cell(None, agent='x', path='/w/t/r1/V.md', evidence=None, state='previous'),
                        'R|T|r1|L': b_cell(None, agent='x', path='/w/t/r1/L.md', evidence=None, state='previous'),
                        'R|T|r1|D': b_cell(None, agent='x', path='/w/t/r1/D.md', evidence=None, state='previous')},
                 agents={'x': {'detail': {'write_events': events('/w/t/r1/U.md', evidence='shell', sure=False)['detail']['write_events'] + events('/w/t/r1/V.md')['detail']['write_events']
                                          + events('/w/t/r1/L.md')['detail']['write_events']}}}, diag_items=[{'code': 'history_lost', 'agent': 'y', 'unit': 'T', 'detail': '1/L'}, {'code': 'seat_tie_held', 'agent': 'x', 'unit': 'T', 'detail': '1/D'}])
        b['agents']['x']['detail']['write_events'] += [dict(events('/w/t/r1/D.md')['detail']['write_events'][0])]
        got = self.lost(a, b)
        self.assertEqual(got[('cell', 'R|T|r1|P')], 'planned_text')
        self.assertEqual(got[('cell', 'R|T|r1|Q')], 'role_row')                                                           # a cell of an agent, waiting, that 0.3.0 does not have
        self.assertEqual(got[('cell', 'R|T|r1|N')], 'role_row')                                                           # and one with no agent: the line of a sentence
        self.assertEqual(got[('cell', 'R|T|r1|H')], 'w3_hint_only')
        self.assertEqual(got[('cell', 'R|T|r1|E')], 'edit_only')
        self.assertEqual(got[('cell', 'R|T|r1|U')], 'unchecked_save')
        self.assertEqual(got[('cell', 'R|T|r1|G')], 'previous_gray')
        self.assertEqual(got[('cell', 'R|T|r1|K')], 'editor_first')
        self.assertEqual(got[('cell', 'R|T|r1|W')], 'demand_unmet')
        self.assertEqual(got[('cell', 'R|T|r1|Z')], 'no_write_event')                                                     # the agent has no write on it in 0.3.0
        self.assertEqual(got[('cell', 'R|T|r1|V')], 'other')                                                              # a sure write, nobody owns it, no diagnostic says why: said one by one
        self.assertEqual(got[('cell', 'R|T|r1|L')], 'history_lost')                                                       # the diagnostic names this cell (round 1, file L) of the topic
        self.assertEqual(got[('cell', 'R|T|r1|D')], 'held_unsure')

    def test_a_diagnostic_holds_only_the_cell_it_names(self):
        a = proj(cells={'R|T|r1|A': a_cell('x'), 'R|T|r2|A': a_cell('x', path='/w/t/r2/A.md'), 'R|T9|r1|A': a_cell('x', path='/w/t9/r1/A.md')})
        ev = events('/w/t/r1/X.md', '/w/t/r2/A.md', '/w/t9/r1/A.md')['detail']['write_events']
        ev[0]['path'] = '/w/t/r1/X.md'
        b = proj(cells={'R|T|r1|A': b_cell(None, agent='x', evidence=None), 'R|T|r2|A': b_cell(None, agent='x', path='/w/t/r2/A.md', evidence=None), 'R|T9|r1|A': b_cell(None, agent='x', path='/w/t9/r1/A.md', evidence=None)},
                 agents={'x': {'detail': {'write_events': ev}}}, diag_items=[{'code': 'history_lost', 'agent': 'y', 'unit': 'T', 'detail': '2/A'}, {'code': 'history_lost', 'agent': 'y', 'unit': 'T', 'detail': '-/A'}])
        got = self.lost(a, b)
        self.assertEqual(got[('cell', 'R|T|r2|A')], 'history_lost')                                                       # round 2 of T
        self.assertEqual(got[('cell', 'R|T|r1|A')], 'other')                                                              # round 1 is not named: a sure write, no owner, no reason
        self.assertEqual(got[('cell', 'R|T9|r1|A')], 'other')                                                             # another topic: not held by the item of T

    def test_rooms_finals_and_units(self):
        a = proj(rooms={'R|T1': {'kind': 'cells', 'sure': True, 'members': []}, 'R|T2': {'kind': 'cells', 'sure': True, 'members': []}, 'R|T7': {'kind': 'cells', 'sure': True, 'members': []}},
                 cells={'R|T7||f': a_cell('x', path='/w/t7/f.md')},
                 finals={'R|T1': {'exists': True, 'path': '/f1.md'}, 'R|T2': {'exists': True, 'path': '/f2.md'}, 'R|T3': {'exists': True, 'path': '/f3.md'}, 'R|T4': {'exists': True, 'path': '/f4.md'},
                         'R|T5': {'exists': True, 'path': '/f5.md'}, 'R|T6': {'exists': True, 'path': '/f6.md'}, 'R|T7': {'exists': True, 'path': '/f7.md'}},
                 agents={'x': {'work_units': ['/u1', '/u2', 'T7'], 'units': ['/u1']}}, roots=['R', 'Gone', 'T7'])
        b = proj(rooms={'R|T1': {'kind': 'cells', 'sure': False, 'members': []}},
                 finals={'R|T1': {'exists': False, 'confirmed': False, 'path': None, 'why': ['estimated_room']}, 'R|T2': {'exists': False, 'confirmed': False, 'path': None, 'why': ['history_lost']},
                         'R|T3': {'exists': False, 'confirmed': False, 'path': None, 'why': ['open_cell']}, 'R|T4': {'exists': False, 'confirmed': False, 'path': None, 'why': [], 'by': 'orch'},
                         'R|T5': {'exists': True, 'confirmed': True, 'path': '/other.md', 'why': []}},
                 agents={'x': {'work_units': ['/u1'], 'units': []}})
        got = self.lost(a, b)
        self.assertEqual(got[('room', 'R|T1')], 'estimated_room_not_current')
        self.assertEqual(got[('room', 'R|T2')], 'other')                                                                  # 0.2.1 had it as a room and nothing says why 0.3.0 has none
        self.assertEqual(got[('room', 'R|T7')], 'flat_review')                                                            # a folder of files with no round folder that 0.3.0 does not list
        self.assertEqual(got[('final', 'R|T7')], 'flat_review')
        self.assertEqual(got[('root', 'T7')], 'flat_review')
        self.assertEqual(got[('work_unit', 'x|T7')], 'flat_review')
        self.assertEqual(got[('final', 'R|T1')], 'estimated_room_final')
        self.assertEqual(got[('final', 'R|T2')], 'history_lost')
        self.assertEqual(got[('final', 'R|T3')], 'final_unconfirmed')
        self.assertEqual(got[('final', 'R|T4')], 'orch_final_reading')
        self.assertEqual(got[('final', 'R|T5')], 'final_other_file')                                                      # confirmed, but another file
        self.assertEqual(got[('work_unit', 'x|/u2')], 'work_units_text')                                                  # a unit 0.2.1 knew by sentences only
        self.assertEqual(got[('unit', 'x|/u1')], 'seat_text')                                                             # seated with no cell of its own
        self.assertEqual(got[('root', 'Gone')], 'other')
        items = home_diff.lost_items({'s': a}, {'s': b}, ['s'])
        self.assertEqual(next(i['detail'] for i in items if i['key'] == 'R|T3'), 'open_cell')

    def test_a_unit_is_lost_for_the_reason_its_cells_were(self):
        a = proj(cells={'R|T|r1|A': a_cell('x'), 'R|T|r1|B': a_cell('x', path='/w/t/r1/B.md'), 'R|T|r1|C': a_cell('x', path='/w/t/r1/C.md')}, agents={'x': {'work_units': ['T'], 'units': ['T']}})
        b = proj(cells={'R|T|r1|A': b_cell(None, agent='x', evidence=None, hint={'kind': 'window', 'agent': 'x'}), 'R|T|r1|B': b_cell(None, agent='x', path='/w/t/r1/B.md', evidence=None, hint={'kind': 'window', 'agent': 'x'}),
                        'R|T|r1|C': b_cell(None, agent='x', path='/w/t/r1/C.md', editors=['x'], evidence=None)}, agents={'x': {'work_units': ['T'], 'units': []}})
        items = home_diff.lost_items({'s': a}, {'s': b}, ['s'])
        unit = next(i for i in items if i['kind'] == 'unit')
        self.assertEqual(unit['reason'], 'w3_hint_only')                                                                  # two cells lost to a hint, one to an edit: the most of them
        self.assertIn('w3_hint_only 2', unit['detail'])
        # all its cells kept but the unit is not held: nothing explains it
        b2 = proj(cells={k: b_cell('x', path=v['path']) for k, v in a['cells'].items()}, agents={'x': {'work_units': ['T'], 'units': []}})
        self.assertEqual({(i['kind'], i['reason']) for i in home_diff.lost_items({'s': a}, {'s': b2}, ['s'])}, {('unit', 'other')})

    def test_a_cell_under_another_root_is_compared_there_not_lost(self):
        a = proj(cells={'R|T|r1|A': a_cell('x')}, agents={'x': {}}, roots=[])
        b = proj(cells={'T|T|r1|A': b_cell('x')}, agents={'x': {}}, roots=['T'])                                          # 0.3.0 lists the topic as a root of its own
        self.assertEqual(self.lost(a, b), {})
        res = home_diff.compare({'s': a}, {'s': b})
        self.assertEqual(res['regrouped'], [{'session': 's', 'kind': 'cell', 'a': 'R|T|r1|A', 'b': 'T|T|r1|A'}])
        self.assertEqual([i['key'] for i in res['new_confirmed'] if i['kind'] == 'cell'], [])                                                    # nor a new confirmed owner
        two = proj(cells={'T|T|r1|A': b_cell('x'), 'U|T|r1|A': b_cell('y', path='/w/t/r1/A.md')}, roots=['T', 'U'])      # two places of that name: not guessed
        self.assertEqual(self.lost(a, two), {('cell', 'R|T|r1|A'): 'other'})

    def test_what_is_kept_is_not_lost(self):
        a = proj(cells={KEY: a_cell('x')}, finals={'R|T': {'exists': True, 'path': '/f.md'}})
        b = proj(cells={KEY: b_cell('x')}, finals={'R|T': {'exists': True, 'confirmed': True, 'path': '/f.md', 'why': []}})
        self.assertEqual(self.lost(a, b), {})

    def test_a_violation_of_line_one_carries_the_reason(self):
        ev = events('/w/t/r1/X.md')['detail']['write_events']
        A = {'s': proj(cells={KEY: a_cell('x')})}
        B = {'s': proj(cells={KEY: b_cell(None, agent='x', evidence=None, state='previous')}, agents={'x': {'detail': {'write_events': ev}}}, diag_items=[{'code': 'history_lost', 'agent': 'y', 'unit': 'T', 'detail': '1/X'}])}
        r = home_diff.check_tool_writes(A, B, ['s'])
        self.assertEqual([(v['reason'], v['b_owner']) for v in r['violations']], [('history_lost', None)])


class SamePlace(unittest.TestCase):
    """0.2.1 names a folder by the link it was reached through and 0.3.0 by the folder itself (found on a real HOME: ~110 cells of one folder reported as a violation of line 1 and 113 as new
    confirmed). Both are written as the real path the capture gives (the links of disk.json followed in the original path space, the live disk never asked) before they are compared."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='home-place-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        live, self.cap = os.path.join(self.tmp, 'live'), os.path.join(self.tmp, 'cap')
        self.real = os.path.join(live, 'store', 'records', 'review')
        os.makedirs(os.path.join(self.real, 't1', 'r1'))
        with open(os.path.join(self.real, 't1', 'r1', 'A.md'), 'w') as f:
            f.write('report')
        os.makedirs(os.path.join(live, 'work'))
        self.alias = os.path.join(live, 'work', 'review')                                  # a relative link, like ~/work/review -> ../store/records/review
        os.symlink('../store/records/review', self.alias)
        os.symlink('r1/A.md', os.path.join(self.real, 't1', 'latest.md'))                 # a file that is itself a link
        cap = home_freeze.Capture(self.cap)
        for p in (self.alias + '/t1/r1/A.md', self.real + '/t1/latest.md', self.real + '/t1/r1'):
            cap.ensure(p)
        cap.write_disk(())
        os.makedirs(self.cap, exist_ok=True)
        self.res = home_diff.Resolver(self.cap)

    def test_the_real_path_of_a_folder_and_of_a_file(self):
        self.assertEqual(self.res.folder(self.alias), self.real)
        self.assertEqual(self.res.folder(self.alias + '/t1/r1'), self.real + '/t1/r1')
        self.assertEqual(self.res.file(self.alias + '/t1/r1/A.md'), self.real + '/t1/r1/A.md')
        self.assertEqual(self.res.file(self.real + '/t1/latest.md'), self.real + '/t1/latest.md')                       # a link that is the file itself stays what it is
        self.assertEqual(self.res.folder('/nowhere/in/the/capture/../x'), '/nowhere/in/the/x')                          # not known: as it was written (normalized)
        self.assertEqual(self.res.folder('relative'), 'relative')

    def test_both_editions_are_compared_in_one_name(self):
        via, real = self.alias, self.real
        a = proj(roots=[via], cells={'%s|%s/t1|r1|A' % (via, via): a_cell('x', path=via + '/t1/r1/A.md')}, finals={'%s|%s/t1' % (via, via): {'exists': True, 'path': via + '/t1/final.md'}},
                 rooms={'%s|%s/t1' % (via, via): {'kind': 'cells', 'sure': True, 'members': ['A']}}, agents={'x': {'units': [via + '/t1'], 'work_units': [via + '/t1']}})
        ev = [{'ts': 1, 'path': real + '/t1/r1/A.md', 'evidence': 'tool', 'ok': True, 'sure': True, 'kind': 'create'}]
        b = proj(roots=[real], cells={'%s|%s/t1|r1|A' % (real, real): b_cell('x', path=real + '/t1/r1/A.md')}, finals={'%s|%s/t1' % (real, real): {'exists': False, 'confirmed': False, 'path': None, 'why': ['open_cell']}},
                 rooms={'%s|%s/t1' % (real, real): {'kind': 'cells', 'sure': True, 'members': ['A']}}, agents={'x': {'units': [real + '/t1'], 'work_units': [real + '/t1'], 'detail': {'write_events': ev}}})
        raw = home_diff.compare({'s': a}, {'s': b})
        self.assertEqual((raw['line1']['baseline'], raw['line1']['same']), (0, 0))                                         # under two names: no write of the agent is found on the 0.2.1 cell's path, and the cell is "new"
        self.assertGreater(len(raw['new_confirmed']), 0)
        A, ca = home_diff.normalize_all({'s': a}, self.res)
        B, cb = home_diff.normalize_all({'s': b}, self.res)
        self.assertEqual((ca, cb), ([], []))
        res = home_diff.compare(A, B)
        self.assertEqual((len(res['line1']['violations']), res['line1']['same'], res['line1']['baseline']), (0, 1, 1))
        self.assertEqual(res['new_confirmed'], [])                                                                         # the same cell, the same owner
        self.assertEqual([(i['kind'], i['reason']) for i in res['lost']], [('final', 'final_unconfirmed')])               # and what is really lost is what is left
        self.assertEqual(A['s']['agents']['x']['units'], [real + '/t1'])
        self.assertEqual(B['s']['agents']['x']['detail']['write_events'][0]['path'], real + '/t1/r1/A.md')

    def test_a_final_named_through_a_link_is_the_file_it_leads_to(self):
        """0.2.1 names the final by the link in the folder of finals, 0.3.0 by the file the link leads to: one file, nothing lost and nothing new."""
        real = self.real
        a = proj(roots=[real], finals={'%s|%s/t1' % (real, real): {'exists': True, 'path': real + '/t1/latest.md'}})
        b = proj(roots=[real], finals={'%s|%s/t1' % (real, real): {'exists': True, 'confirmed': True, 'path': real + '/t1/r1/A.md', 'why': []}})
        A, _ = home_diff.normalize_all({'s': a}, self.res)
        B, _ = home_diff.normalize_all({'s': b}, self.res)
        self.assertNotEqual(A['s']['finals'].popitem()[1]['path'], B['s']['finals']['%s|%s/t1' % (real, real)]['path'])       # the paths differ ...
        A, _ = home_diff.normalize_all({'s': a}, self.res)
        res = home_diff.compare(A, B)
        self.assertEqual((res['lost'], res['new_confirmed']), ([], []))                                                      # ... the file does not
        other = proj(roots=[real], finals={'%s|%s/t1' % (real, real): {'exists': True, 'confirmed': True, 'path': real + '/t1/other.md', 'why': []}})
        O, _ = home_diff.normalize_all({'s': other}, self.res)
        self.assertEqual([(i['kind'], i['reason']) for i in home_diff.compare(A, O)['lost']], [('final', 'final_other_file')])

    def test_two_names_of_one_key_are_merged_and_said(self):
        via, real = self.alias, self.real
        p = proj(roots=[via, real], cells={'%s|%s/t1|r1|A' % (via, via): a_cell('x', path=via + '/t1/r1/A.md'), '%s|%s/t1|r1|A' % (real, real): a_cell('y', path=real + '/t1/r1/A.md')})
        out, col = home_diff.normalize_all({'s': p}, self.res)
        self.assertEqual(out['s']['roots'], [real])
        self.assertEqual(len(out['s']['cells']), 1)
        self.assertEqual([(c['kind'], c['session']) for c in col], [('cell', 's')])                                          # the first (by the old key) is kept and the merge is reported

    def test_the_command_writes_real_paths_unless_told_not_to(self):
        via, real = self.alias, self.real
        for name, v in (('a.json', proj(roots=[via], cells={'%s|%s/t1|r1|A' % (via, via): a_cell('x', path=via + '/t1/r1/A.md')})),
                        ('b.json', proj(roots=[real], cells={'%s|%s/t1|r1|A' % (real, real): b_cell('x', path=real + '/t1/r1/A.md')}, agents={'x': {'detail': {'write_events': [{'ts': 1, 'path': real + '/t1/r1/A.md', 'evidence': 'tool', 'ok': True, 'sure': True, 'kind': 'create'}]}}}))):
            with open(os.path.join(self.tmp, name), 'w') as f:
                json.dump({'sessions': {'aaaa11110': {'error': None, 'projection': v}}}, f)
        for flag, want in (([], 'baseline 1, same 1, editor_first 0 (to confirm one by one), accepted flat_review (O16) 0, violations 0'), (['--raw-paths'], 'baseline 0')):         # raw: the write is on a path the 0.2.1 cell does not have
            with contextlib.redirect_stdout(io.StringIO()):
                home_diff.main(['--cap', self.cap, '--a', os.path.join(self.tmp, 'a.json'), '--b', os.path.join(self.tmp, 'b.json'), '--report-dir', self.tmp] + flag)
            with open(os.path.join(self.tmp, 'report.md')) as f:
                self.assertIn(want, f.read())


class Expected(unittest.TestCase):
    def test_actual_counts_against_the_expected_ones(self):
        lost = [{'session': 'aaaa11110', 'reason': 'role_row'}] * 22 + [{'session': 'aaaa11110', 'reason': 'w3_hint_only'}] * 3 + [{'session': 'bbbb22220', 'reason': 'role_row'}] * 12 + [{'session': 'bbbb22220', 'reason': 'other'}]
        expect = {'items': [{'session': 'aaaa1111', 'reason': 'role_row', 'count': 22}, {'session': 'aaaa1111', 'reason': 'w3_hint_only', 'count': 4}, {'session': 'bbbb2222', 'reason': 'role_row', 'count': 12}]}
        r = home_diff.expected_check(lost, expect, ['aaaa11110', 'bbbb22220'])
        self.assertEqual([(x['reason'], x['expected'], x['actual'], x['ok']) for x in r['rows']], [('role_row', 22, 22, True), ('w3_hint_only', 4, 3, False), ('role_row', 12, 12, True)])
        self.assertEqual(r['unexpected'], [{'session': 'bbbb22220', 'reason': 'other', 'count': 1}])                       # a loss nobody wrote down beforehand
        flat = [{'session': 'aaaa11110', 'reason': 'flat_review', 'kind': 'cell', 'key': 'R|T9|r1|A'}, {'session': 'aaaa11110', 'reason': 'flat_review', 'kind': 'cell', 'key': 'R|T9|r1|B'},
                {'session': 'aaaa11110', 'reason': 'flat_review', 'kind': 'work_unit', 'key': 'x|T9'}, {'session': 'aaaa11110', 'reason': 'flat_review', 'kind': 'root', 'key': 'T8'}]
        by = home_diff.expected_check(flat, {'items': [{'session': 'aaaa1111', 'reason': 'flat_review', 'count': 2, 'count_by': 'topic'}, {'session': 'aaaa1111', 'reason': 'flat_review', 'count': 4}]}, ['aaaa11110'])
        self.assertEqual([(x['count_by'], x['actual'], x['ok']) for x in by['rows']], [('topic', 2, True), ('item', 4, True)])                      # two topics, four items
        gone = home_diff.expected_check(lost, {'items': [{'session': 'cccc3333', 'reason': 'role_row', 'count': 5}]}, ['aaaa11110'])
        self.assertEqual([(x['ok'], x['actual']) for x in gone['rows']], [(None, 0)])                                       # a session left out of the capture is not a failure


class Finals(unittest.TestCase):
    def test_the_two_readings_of_D1(self):
        fin = lambda n: {'finals': {'R|T%d' % i: {'exists': True, 'confirmed': True, 'path': '/f%d' % i} for i in range(n)}, 'root_finals': {'R': {'exists': True, 'confirmed': True, 'path': '/r'}}}     # noqa: E731
        A, B, N = {'s': proj()}, {'s': dict(proj(), **fin(5))}, {'s': dict(proj(), **fin(2))}
        r = home_diff.compare(A, B, None, None, N)
        self.assertEqual((r['line5']['with_orch'], r['line5']['without_orch'], r['line5']['in_0_2_1']), (6, 3, 0))
        self.assertIsNone(home_diff.compare(A, B)['line5']['without_orch'])
        old = {'s': dict(proj(), finals={'a': {'exists': True, 'path': '/a'}, 'b': {'exists': False, 'path': None}}, root_finals={'r': ['/x'], 'q': {'exists': True, 'path': '/q'}})}                  # 0.2.1: exists
        self.assertEqual(home_diff.confirmed_finals(old), 2)


class Fidelity(unittest.TestCase):
    def test_ratio_and_explanations(self):
        live = {'s': proj(cells={'R|T|r1|%d' % i: a_cell('x') for i in range(100)}, roots=['R'])}
        same = home_diff.fidelity(live, live)
        self.assertEqual((same['total'], same['differing'], same['passed']), (101, [], True))
        replay = {'s': proj(cells=dict(live['s']['cells']), roots=['R'])}
        replay['s']['cells']['R|T|r1|3'] = a_cell('y')
        one = home_diff.fidelity(replay, live)                                                                              # 1 of 101 differs: not over 1%, but unexplained
        self.assertEqual((len(one['differing']), one['ratio'] <= 0.01, one['passed']), (1, True, False))
        explained = home_diff.fidelity(replay, live, {'s|cell|R|T|r1|3': 'the agent was renamed by the board between the two reads'})
        self.assertTrue(explained['passed'])
        replay['s']['cells']['R|T|r1|4'] = a_cell('y')
        two = home_diff.fidelity(replay, live, {'cell|R|T|r1|3': 'x', 'cell|R|T|r1|4': 'y'})                                 # explained, but over 1%
        self.assertEqual((len(two['differing']), two['passed']), (2, False))

    def test_an_item_missing_on_one_side_differs(self):
        live = {'s': proj(cells={KEY: a_cell('x')})}
        replay = {'s': proj(cells={})}
        r = home_diff.fidelity(replay, live)
        self.assertEqual([(d['item'], d['in_live'], d['in_replay']) for d in r['differing']], [('cell|' + KEY, True, False)])


class Perf(unittest.TestCase):
    def test_the_requests_for_details_are_a_column_of_their_own(self):
        ra = {'sessions': {'s': {'state_cold_sec': 1.0, 'state_warm_sec': 0.5, 'details_sec': 3.0, 'details_agents': 30}}}
        rb = {'sessions': {'s': {'state_cold_sec': 2.0, 'state_warm_sec': 0.25}}}                                           # a run made with --details none
        rows = home_diff.perf_rows(ra, rb)
        self.assertEqual((rows[0]['a_cold'], rows[0]['b_warm'], rows[0]['a_details'], rows[0]['a_details_agents'], rows[0]['b_details']), (1.0, 0.25, 3.0, 30, None))
        res = home_diff.compare({'s': proj()}, {'s': proj()}, ra=ra, rb=rb)
        text = home_diff.render_report(res)
        self.assertIn('timed apart', text)
        self.assertIn('3.000 (30 agents)', text)


class Report(unittest.TestCase):
    def test_a_report_and_the_two_json_files_are_written_into_the_capture_folder(self):
        tmp = tempfile.mkdtemp(prefix='home-diff-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cap = os.path.join(tmp, 'cap')
        os.makedirs(cap)
        with open(os.path.join(cap, 'disk.json'), 'w') as f:
            json.dump({'version': 1, 'entries': {}}, f)
        A = {'sessions': {'aaaa11110': {'error': None, 'projection': proj(cells={KEY: a_cell('x'), 'R|T|r1|Q': a_cell('x', state='waiting')}), 'state_cold_sec': 1.5, 'state_warm_sec': 0.1}}}
        B = {'sessions': {'aaaa11110': {'error': None, 'projection': proj(cells={KEY: b_cell('x'), 'R|T|r2|Q-late': b_cell('Q-late')}, agents={'x': events('/w/t/r1/X.md')}), 'state_cold_sec': 2.5, 'state_warm_sec': 0.2}}}
        for name, d in (('a.json', A), ('b.json', B), ('truth.json', {'items': []})):
            with open(os.path.join(tmp, name), 'w') as f:
                json.dump(d, f)
        with contextlib.redirect_stdout(io.StringIO()):
            code = home_diff.main(['--cap', cap, '--a', os.path.join(tmp, 'a.json'), '--b', os.path.join(tmp, 'b.json'), '--truth', os.path.join(tmp, 'truth.json')])
        self.assertEqual(code, 1)                                                                                          # the one new confirmed item is unmarked
        report = read_text(os.path.join(cap, 'report.md'))
        self.assertIn('unmarked (stops the release)', report)
        self.assertIn('role_row', report)
        self.assertEqual([i['key'] for i in read_json(os.path.join(cap, 'new_confirmed.json'))], ['R|T|r2|Q-late'])
        self.assertEqual([(i['key'], i['reason']) for i in read_json(os.path.join(cap, 'lost.json'))], [('R|T|r1|Q', 'role_row')])


if __name__ == '__main__':
    unittest.main()
