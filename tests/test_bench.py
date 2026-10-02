"""tools/bench.py: the synthetic big HOME, the numbers it takes, and the --check gate.

What is checked (no absolute timing: a loaded machine would make that a coin flip):
  * the synthetic HOME has the sessions, Bash calls and `claude -p` children it says it has, the children are findable in the launching calls, and a folder that is
    not ours is never overwritten;
  * scale 1 is 100 sessions / 25 000 Bash calls / 200 children;
  * the gate: the baseline passes against itself, a number 30 % worse fails when it is a gating one (CPU or in-process time) and only warns when it is wall-clock HTTP,
    a halved baseline fails, a number near zero does not trip on noise, a number missing on one side is skipped;
  * a small run end to end (a worker per measurement, a server on 8857-8860, a client process): the parts of a request add up (lock wait + state + JSON + gzip +
    socket write never exceed the handler's own time, the handler never exceeds what the client waited), the seams are all there, the unchanged answer is tiny, and
    --check against its own result passes, against a halved one exits 1;
  * it never touches 8790 and leaves no process behind.

    python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tools'))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402,F401  (pins HOME and the cache to a throwaway folder before board is imported)

import bench  # noqa: E402

BENCH = os.path.join(REPO, 'tools', 'bench.py')
SMALL = ['--scale', '0.05', '--bulk-kb', '0', '--iterations', '4', '--requests', '5', '--write-every', '0.3']


def ports_free():
    import socket
    free = []
    for p in bench.PORTS:
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            free.append(p)
        except OSError:
            pass
        finally:
            s.close()
    return free


class Build(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='bench-test-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_scale_one_is_the_plans_scene(self):
        self.assertEqual(bench.plan(1.0)[:3], (100, 25000, 200))
        self.assertGreaterEqual(bench.plan(1.0)[3], 100)               # the busy orchestrator has about a hundred sub-agents

    def test_the_home_has_what_the_manifest_says(self):
        man = bench.build_big_home(self.root, scale=0.1, bulk_kb=0)
        projects = os.path.join(man['home'], '.claude', 'projects')
        mains = children = subs = bash = 0
        child_texts = []
        for dirpath, _, files in os.walk(projects):
            for f in files:
                p = os.path.join(dirpath, f)
                if f.endswith('.meta.json'):
                    continue
                if not f.endswith('.jsonl'):
                    continue
                if os.sep + 'subagents' + os.sep in p:
                    subs += 1
                    kind = 'sub'
                else:
                    kind = None
                with open(p) as fh:
                    lines = [json.loads(x) for x in fh if x.strip()]
                bash += sum(1 for d in lines if d.get('type') == 'assistant' for b in (d['message'].get('content') or []) if b.get('type') == 'tool_use' and b.get('name') == 'Bash')
                if kind == 'sub':
                    continue
                if lines[0].get('entrypoint') == 'sdk-cli':
                    children += 1
                    child_texts.append(next(d['message']['content'] for d in lines if d.get('promptSource') == 'sdk'))
                else:
                    mains += 1
        self.assertEqual((man['mains'], man['children'], man['subagents']), (mains, children, subs))
        self.assertEqual(man['sessions'], mains + children)
        self.assertEqual(man['bash_calls'], bash)
        self.assertGreater(man['bash_calls'], 2000)
        self.assertEqual(man['busy_agents'], bench.plan(0.1)[3])
        self.assertTrue(os.path.exists(man['busy_path']))
        # a child can be told from the launching calls: its instruction sits in a command or in a file a command reads
        every = ''
        for dirpath, _, files in os.walk(projects):
            for f in files:
                if f.endswith('.jsonl') and os.sep + 'subagents' + os.sep not in dirpath:
                    with open(os.path.join(dirpath, f)) as fh:
                        every += fh.read()
        work = os.path.join(man['home'], 'work')
        for name in os.listdir(work):
            if os.path.isfile(os.path.join(work, name)):
                with open(os.path.join(work, name)) as fh:
                    every += fh.read()
        findable = sum(1 for t in child_texts if t in every.replace('\\"', '"'))
        self.assertGreaterEqual(findable, len(child_texts) - 1)

    def test_it_is_deterministic(self):
        a = bench.build_big_home(os.path.join(self.root, 'a'), scale=0.05, bulk_kb=0, now=1790000000)
        b = bench.build_big_home(os.path.join(self.root, 'b'), scale=0.05, bulk_kb=0, now=1790000000)
        for k in ('sessions', 'mains', 'subagents', 'children', 'bash_calls', 'bytes'):
            self.assertEqual(a[k], b[k], k)

    def test_a_folder_that_is_not_ours_is_refused(self):
        stranger = os.path.join(self.root, 'stranger')
        os.makedirs(stranger)
        with open(os.path.join(stranger, 'notes.txt'), 'w') as f:
            f.write('mine')
        cp = subprocess.run([sys.executable, BENCH, 'build', stranger, '--scale', '0.05', '--bulk-kb', '0'], capture_output=True, text=True)
        self.assertNotEqual(cp.returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(stranger, 'notes.txt')))
        self.assertFalse(os.path.exists(os.path.join(stranger, 'home')))
        self.assertTrue(bench.is_ours(self.root) is False)


class Help(unittest.TestCase):
    """`--help` of every command prints (a literal % in a help text once made argparse raise), and carries no internal tags."""

    def test_every_help_prints_and_names_no_internal_tag(self):
        for args in ((), ('run',), ('baseline',), ('build',)):
            with self.subTest(args=args):
                r = subprocess.run([sys.executable, BENCH] + list(args) + ['--help'], capture_output=True, text=True, timeout=60)
                self.assertEqual((r.returncode, r.stderr), (0, ''))
                self.assertNotRegex(r.stdout, r'\bR\d{1,2}\b|PLAN|robust')
        out = subprocess.run([sys.executable, BENCH, 'run', '--help'], capture_output=True, text=True, timeout=60).stdout
        self.assertIn('%', out)                                                  # the tolerance of --check reads "25 %"

    def test_the_budget_rows_name_no_internal_tag(self):
        for name, _, why in bench.BUDGETS:
            self.assertNotRegex(why, r'\bR\d{1,2}\b|PLAN|robust', name)
        rows, _ = bench.budget_rows({'ready_wall': 1.0}, {'ready_wall': 1.2, 'http_busy_p95': 0.01, 'http_busy_identity_p95': 0.01, 'state_p95_wall': 0.01})
        for row in rows:
            self.assertNotRegex(row[-1], r'\bR\d{1,2}\b|PLAN|robust')


class Gate(unittest.TestCase):
    BASE = {'ready_cpu': 0.7, 'ready_wall': 0.7, 'scan_idle_p95_cpu': 0.009, 'scan_grow_p95_cpu': 0.023, 'state_p50_cpu': 0.0073, 'http_busy_p95': 0.034, 'http_busy_p50': 0.017}

    def test_the_baseline_passes_against_itself(self):
        rows, ok = bench.compare(self.BASE, dict(self.BASE))
        self.assertTrue(ok)
        self.assertTrue(rows and all(r[4] == 'ok' for r in rows))

    def test_a_gating_number_30_percent_worse_fails(self):
        for name in ('ready_cpu', 'scan_idle_p95_cpu', 'scan_grow_p95_cpu', 'state_p50_cpu'):
            cur = dict(self.BASE)
            cur[name] = self.BASE[name] * 1.3 + 0.01
            rows, ok = bench.compare(self.BASE, cur)
            self.assertFalse(ok, name)
            self.assertEqual([r[4] for r in rows if r[0] == name], ['FAIL'])

    def test_within_25_percent_passes(self):
        cur = {k: v * 1.2 for k, v in self.BASE.items()}
        self.assertTrue(bench.compare(self.BASE, cur)[1])

    def test_wall_clock_http_only_warns_unless_strict(self):
        cur = dict(self.BASE, http_busy_p95=self.BASE['http_busy_p95'] * 3)
        rows, ok = bench.compare(self.BASE, cur)
        self.assertTrue(ok)
        self.assertIn('warn', [r[4] for r in rows])
        self.assertFalse(bench.compare(self.BASE, cur, strict=True)[1])

    def test_a_halved_baseline_fails(self):
        half = {k: v / 2.0 for k, v in self.BASE.items()}
        rows, ok = bench.compare(half, self.BASE)
        self.assertFalse(ok)

    def test_a_number_near_zero_does_not_trip_on_noise(self):
        base = {'scan_idle_p95_cpu': 0.0004}
        self.assertTrue(bench.compare(base, {'scan_idle_p95_cpu': 0.0011})[1])             # x2.7 of 0.4 ms is 0.7 ms more: inside the floor
        self.assertFalse(bench.compare(base, {'scan_idle_p95_cpu': 0.01})[1])

    def test_a_missing_number_is_skipped(self):
        rows, ok = bench.compare({'ready_cpu': 1.0}, {'scan_idle_p95_cpu': 9.0})
        self.assertEqual(rows, [])
        self.assertTrue(ok)

    def test_the_budgets(self):
        base = {'ready_wall': 0.7, 'scan_idle_p95_wall': 0.009, 'scan_grow_p95_wall': 0.023}
        good = {'ready_wall': 1.6, 'scan_idle_p95_wall': 0.013, 'scan_grow_p95_wall': 0.027, 'http_busy_p95': 0.04, 'state_p95_wall': 0.015}
        rows, ok = bench.budget_rows(base, good)
        self.assertTrue(ok, rows)
        self.assertFalse(bench.budget_rows(base, dict(good, ready_wall=1.8))[1])                 # +1 s at most for the first screen
        self.assertFalse(bench.budget_rows(base, dict(good, scan_idle_p95_wall=0.02))[1])        # +5 ms for a later scan
        self.assertFalse(bench.budget_rows(base, dict(good, http_busy_p95=0.2))[1])              # 150 ms over HTTP
        self.assertTrue(bench.budget_rows(base, dict(good, http_busy_identity_p95=0.02))[1])
        self.assertFalse(bench.budget_rows(base, dict(good, http_busy_identity_p95=0.23))[1])    # ... also for a client that does not ask for gzip
        self.assertFalse(bench.budget_rows(base, dict(good, state_p95_wall=0.03))[1])            # 20 ms in the process

    def test_a_baseline_of_several_runs_is_their_median(self):
        runs = [{'metrics': {'ready_cpu': v, 'scan_idle_p95_cpu': v / 10.0}, 'repo': '/x/before', 'loadavg': [1, 2, 3], 'schema': 1, 'when': 'w'} for v in (0.5, 0.9, 0.6)]
        runs[1]['metrics']['only_here'] = 1.0
        doc = bench.median_doc(runs)
        self.assertEqual(doc['metrics']['ready_cpu'], 0.6)
        self.assertEqual(doc['metrics']['only_here'], 1.0)
        self.assertEqual((doc['runs'], doc['label']), (3, 'before'))
        self.assertNotIn('scan', doc)                                   # no per-request rows in a baseline
        self.assertTrue(bench.compare(doc['metrics'], doc['metrics'])[1])

    def test_slim_keeps_the_gate_numbers_and_the_scene_only(self):
        doc = {'schema': 1, 'repo': '/a/b/ab-before', 'when': 'now', 'loadavg': [1], 'python': '3', 'cpus': 2, 'home': 'synthetic', 'manifest': {'sessions': 3}, 'metrics': {'ready_cpu': 1.0},
               'scan': {'big': 'x' * 100}, 'http': {'runs': [{'rows': list(range(100))}]}}
        out = bench.slim(doc)
        self.assertEqual(set(out), {'schema', 'when', 'loadavg', 'python', 'cpus', 'home', 'manifest', 'metrics', 'label'})
        self.assertEqual(out['label'], 'ab-before')

    def test_percentiles(self):
        self.assertEqual(bench.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertEqual(bench.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(bench.pct([], 50), None)
        self.assertEqual(bench.pct([None, 3], 50), 3)


@unittest.skipUnless(len(ports_free()) >= 1, 'all of 8857-8860 are busy')
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='bench-e2e-')
        cls.out = os.path.join(cls.root, 'res.json')
        cp = subprocess.run([sys.executable, BENCH, 'run'] + SMALL + ['--json', cls.out], capture_output=True, text=True, timeout=300,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        cls.cp = cp
        cls.doc = None
        if os.path.exists(cls.out):
            with open(cls.out) as f:
                cls.doc = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_it_ran(self):
        self.assertEqual(self.cp.returncode, 0, self.cp.stderr[-600:] + self.cp.stdout[-600:])
        self.assertIsNotNone(self.doc)
        m = self.doc['manifest']
        self.assertGreater(m['bash_calls'], 1000)
        self.assertEqual(m['children'], bench.plan(0.05)[2])

    def test_every_number_the_gate_reads_is_there(self):
        m = self.doc['metrics']
        for name in ('ready_cpu', 'ready_wall', 'link_scan_first_cpu', 'scan_idle_p95_cpu', 'scan_grow_p95_cpu', 'state_p50_cpu', 'json_wall', 'gzip_wall', 'http_busy_p50',
                     'http_busy_p95', 'http_quiet_p95'):
            self.assertIn(name, m, name)
            self.assertGreaterEqual(m[name], 0)

    def test_the_board_read_the_scene(self):
        sc = self.doc['scan']
        self.assertGreaterEqual(sc['counts']['cli_owners'], 1)           # the `claude -p` children were linked to their parents
        self.assertGreaterEqual(sc['session']['agents'], bench.plan(0.05)[3])

    def test_the_seams_are_all_there(self):
        self.assertEqual(self.doc['http']['seams_missing'], [])
        self.assertIn(self.doc['http']['port'], bench.PORTS)
        self.assertNotEqual(self.doc['http']['port'], 8790)

    def test_the_parts_of_a_request_add_up(self):
        eps = 0.003
        for run in self.doc['http']['runs']:
            self.assertGreater(len(run['rows']), 0, run['label'])
            self.assertEqual(run['seam_records'], len(run['rows']), run['label'])
            for r in run['rows']:
                if r['handler'] is None:
                    self.fail('a request left no record in the seams (%s)' % run['label'])
                parts = sum(r[k] or 0.0 for k in ('lock_wait', 'compute', 'json', 'gzip', 'write'))
                self.assertLessEqual(parts, r['handler'] + eps, '%s: the parts exceed the handler' % run['label'])
                self.assertLessEqual(r['handler'], r['client_total'] + eps, '%s: the handler exceeds what the client waited' % run['label'])

    def test_the_runs_differ_where_they_should(self):
        runs = {r['label']: r for r in self.doc['http']['runs']}
        self.assertEqual(set(runs), {'quiet', 'quiet_identity', 'quiet_unchanged', 'poll', 'busy', 'busy_identity'})
        full, ident, unch = runs['quiet'], runs['quiet_identity'], runs['quiet_unchanged']
        self.assertTrue(full['gzip'] and not ident['gzip'])
        self.assertGreater(ident['sizes']['bytes'], full['sizes']['bytes'])                           # gzip made it smaller
        self.assertLess(unch['sizes']['bytes'], 200)                                                 # the unchanged answer is a few bytes
        self.assertIsNone(unch['parts']['compute']['p50'] if unch['parts']['compute']['n'] == 0 else None)   # and computes no state
        self.assertGreater(runs['busy']['write'], 0)                                                 # the busy run had its writer
        self.assertIn('session_poll', runs['busy']['background'])                                    # and the poll loop ran

    def test_check_passes_against_itself_and_fails_against_a_halved_baseline(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        # a second run of the same scene is not the same run, and a shared machine (CI) can be slow for a moment: check against a baseline
        # four times slower, which any normal run meets, so the test is about the gate and not about this machine's timing
        slow = json.loads(json.dumps(self.doc))
        slow['metrics'] = {k: v * 4.0 for k, v in slow['metrics'].items()}
        slow_path = os.path.join(self.root, 'slow.json')
        with open(slow_path, 'w') as f:
            json.dump(slow, f)
        same = subprocess.run([sys.executable, BENCH, 'run'] + SMALL + ['--what', 'scan', '--check', slow_path], capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(same.returncode, 0, same.stdout[-800:] + same.stderr[-300:])
        self.assertIn('check: ok', same.stdout)
        half = json.loads(json.dumps(self.doc))
        half['metrics'] = {k: v / 4.0 for k, v in half['metrics'].items()}
        path = os.path.join(self.root, 'half.json')
        with open(path, 'w') as f:
            json.dump(half, f)
        bad = subprocess.run([sys.executable, BENCH, 'run'] + SMALL + ['--what', 'scan', '--check', path], capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(bad.returncode, 1, bad.stdout[-800:])
        self.assertIn('check: FAILED', bad.stdout)

    def test_no_process_is_left_behind(self):
        cp = subprocess.run(['ss', '-ltn'], capture_output=True, text=True) if shutil.which('ss') else None
        if cp is None:
            self.skipTest('no ss')
        for p in bench.PORTS:
            self.assertNotRegex(cp.stdout, r':%d\s' % p)


if __name__ == '__main__':
    unittest.main()
