"""The scene the README pictures are taken from (tools/docs_scene.py): about ten agents working, a few resting, the stopped states in the agent list, the orchestrator working.
Starts the real server.py on a synthetic HOME with fake processes (--live) and reads /api/state.

    python3 -m unittest discover -s tests
"""
import getpass
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402,F401  (pins HOME and the cache to a throwaway folder before board is imported)
import docs_scene  # noqa: E402
import synth_home  # noqa: E402
from test_synth import Board, reap, start_live, synth_home_codex_unknown, tree_digest  # noqa: E402


class DocsScene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Every resource gets its cleanup as soon as it exists: a setUpClass that fails half way is not followed by tearDownClass, and the server and the fake processes would stay.
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.info = docs_scene.build(os.path.join(cls.tmp.name, 'home'))
        cls.home = cls.info['home']
        cls.pids, cls.procs = start_live(cls.info)
        cls.addClassCleanup(lambda: (synth_home.stop_live(cls.home, quiet=True), reap(cls.procs)))
        cls.board = Board(cls.home, cls.info['orch'])
        cls.addClassCleanup(cls.board.close)
        end = time.time() + 30                                  # the first seconds after the start show the `claude -p` runs a live process or an environment places, without the call that started them (the sub-agents' records are read after the first screen): wait for it
        cls.state, why = None, 'no answer'
        while True:
            try:
                code, body = cls.board.get('/api/state')
            except OSError as e:                                # a server that is busy may be slow to answer: the next look is another try
                code, body, why = None, None, 'no answer (%s: %s)' % (type(e).__name__, e)
            if code == 200:
                cls.state = body
                grand = {a['id']: a for a in body['agents']}.get(synth_home.STOPPED_KIDS['grand'])
                if grand and grand['title'] != 'claude -p':
                    break
            elif code is not None:
                why = 'HTTP %s: %r' % (code, body[:200])
            if time.time() > end:
                break
            time.sleep(0.5)
        if cls.state is None:
            raise AssertionError('/api/state gave no state in 30 s (%s); the server printed:\n%s' % (why, cls.board.text()))
        cls.unknown = synth_home_codex_unknown(cls.home)         # no /proc (macOS): the Codex seat of T3 cannot be told from a fake process

    def topics(self):
        return {t['dir'].rsplit('/', 1)[1][:2].upper(): t for t in self.state['debates'][0]['topics']}

    def test_orchestrator_works_and_ten_agents_work(self):
        st = self.state
        self.assertEqual((st['session']['alive'], st['orch']['state']), (True, 'working'))
        running = [a for a in st['agents'] if a['status'] == 'running']
        self.assertEqual(len(running), 10 - self.unknown)
        self.assertEqual(sum(1 for a in running if a['units']), 8 - self.unknown)             # eight in debate rooms: T3 (three, one is Codex), T6 (three), T7 (two)
        self.assertEqual(sum(1 for a in running if not a['units']), 2)                       # the release-notes run and the `claude -p` run it started
        self.assertEqual([a['level'] for a in st['alerts']], ['check'])                     # the limit one sub-agent hit; the orchestrator itself is not waiting on it

    def test_four_to_six_rest_and_the_rest_of_the_rooms_are_as_planned(self):
        t = self.topics()
        resting = [r['agents'][-1] for k in ('T4', 'T5') for r in t[k]['rows']]
        by_id = {a['id']: a for a in self.state['agents']}
        self.assertTrue(4 <= len(resting) <= 6, resting)
        self.assertEqual(sorted(by_id[i]['status'] for i in resting), ['done'] * 4 + ['interrupted'] * 2)
        cells = {k: [c['state'] for r in t[k]['rows'] for c in r['cells']] for k in t}
        self.assertEqual(cells['T3'].count('writing'), 3)
        self.assertEqual(cells['T6'], ['writing'] * 3)
        self.assertEqual(cells['T7'], ['writing'] * 2)
        self.assertEqual(cells['T5'].count('draft'), 2)
        self.assertTrue(t['T4']['final']['exists'] and not t['T5']['final']['exists'])
        self.assertEqual([len(t[k]['rows']) for k in ('T3', 'T6', 'T7')], [3, 3, 2])

    def test_the_stopped_states_and_the_launched_tree_are_there(self):
        ag = self.state['agents']
        self.assertTrue({'limit', 'time_limit', 'exited', 'api_error'} <= {a['reason'] for a in ag if a['status'] == 'interrupted'})
        by_id = {a['id']: a for a in ag}
        grand, great = by_id[synth_home.STOPPED_KIDS['grand']], by_id[synth_home.STOPPED_KIDS['great']]
        self.assertEqual((grand['status'], grand['title']), ('running', 'Draft the release notes in a separate run'))
        self.assertEqual(great['status'], 'done')
        self.assertEqual(great['parent'], grand['id'])
        self.assertEqual(by_id[grand['parent']]['title'], 'Release notes draft')

    def test_the_context_is_not_at_its_limit(self):
        t = self.state['orch']['tokens']
        self.assertTrue(0.25 < t['ctx'] / t['ctx_limit'] < 0.65, t)

    def test_nothing_real_in_any_file_and_the_scene_is_repeatable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.join(os.path.realpath(tmp.name), 'home')
        docs_scene.build(home, now=1_800_000_000.0)
        first = tree_digest(home)
        docs_scene.build(home, now=1_800_000_000.0)
        self.assertEqual(tree_digest(home), first)
        real, user = os.path.realpath(os.path.expanduser('~')), getpass.getuser()
        for name, data in first:
            if real not in ('/', '') and not home.startswith(real + os.sep):
                self.assertNotIn(real.encode(), data, name)
            if len(user) > 3 and '/%s/' % user not in home:
                self.assertNotIn(('/%s/' % user).encode(), data, name)


if __name__ == '__main__':
    unittest.main()
