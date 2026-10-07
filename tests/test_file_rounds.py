"""A recorded Codex team with file rounds, read in process and shown as a debate table."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tools.scenarios import axes, build, observe  # noqa: E402
from tools.scenarios.scene_cxo import Cxo  # noqa: E402
from board import agents as AG, facts as F  # noqa: E402


def fixture(root, missing=False, uppercase=False):
    b = build.Built(axes.normalize(axes.Case('cxo', {})), root)
    scene = Cxo(b)
    top = scene.root('top', 'codex-tui', b.T(0), 'Review the options.')
    unit = os.path.join(scene.W, 'file-rounds')
    for i, seat in enumerate('abc'):
        sub = scene.sub(seat, top, b.T(10 + i * 5), 'review_' + seat, 'Reviewer', 1, 'Review the options.', b.T(0))
        turn = scene.turn_of(sub.tid)
        for n in (1, 2):
            if missing and seat == 'b' and n == 2:
                continue
            path = os.path.join(unit, ('ROUND%d_%s.MD' if uppercase else 'round%d_%s.md') % (n, seat))
            at = b.T(30 + n * 10 + i)
            sub.shell(at, "printf report > '%s'" % path, scene.W, 'write-%s-%d' % (seat, n), turn, end=at + 0.5, out_at=at + 0.6)
            build.put(path, 'report', at + 0.2)
        path = os.path.join(unit, 'final_%s.md' % seat)
        at = b.T(70 + i)
        sub.shell(at, "printf final > '%s'" % path, scene.W, 'final-' + seat, turn, end=at + 0.5, out_at=at + 0.6)
        build.put(path, 'final', at + 0.2)
        sub.complete(b.T(80 + i), turn, 'Finished.')
    top.complete(b.T(90), scene.turn_of(top.tid), 'Finished.')
    for r in scene.rolls:
        r.save()
    b.main_path = top.path
    b.meta.update(page='codex', repo=scene.W, unit=unit)
    b.phases.append(build.Phase(b.T(100), []))
    return b


def snapshot(b):
    got = {}

    def capture(b_, obs, objs):
        s, state = observe.open_state(b_, objs)
        topic = next(t for d in state['debates'] for t in d['topics'] if t['dir'] == b_.meta['unit'])
        paths = [c['path'] for r in topic['rows'] for c in r['cells'] if c['state'] == 'done'] + [c['path'] for c in topic['final']['candidates']]
        got.update(state=state, launches={aid: a.launch for aid, a in s.agents.items()}, allowed={p: s.allowed_file(p) for p in paths})
    with mock.patch.object(observe, 'read_final', capture):
        observe.observe(b)
    return got


class RecordedFileRounds(unittest.TestCase):
    def test_uppercase_file_rounds_are_collected_and_keep_their_write_history(self):
        with tempfile.TemporaryDirectory() as root:
            b = fixture(root, uppercase=True)
            got = snapshot(b)
            state = got['state']
            tp = next(t for d in state['debates'] for t in d['topics'] if t['dir'] == b.meta['unit'])
            self.assertEqual(tp['rounds'], [1, 2])
            self.assertEqual([r['p'] for r in tp['rows']], list('abc'))
            self.assertTrue(all(c['owner'] == b.ids[r['p']] and c['evidence'] == 'shell' for r in tp['rows'] for c in r['cells']))
            self.assertTrue(all(p == real for p, real in got['allowed'].items()))
        events = AG.EventLog()
        report = F.WriteEvent('A', '/tmp/review/ROUND1_a.MD', 100, 'create', 'shell', True, proof='exit')
        events.add_write(report)
        for i in range(AG.OTHER_WRITES_KEEP + 1):
            events.add_write(F.WriteEvent('A', '/work/file%d.py' % i, 200 + i, 'create', 'shell', True, proof='exit'))
        self.assertIn(report, events.events())

    def test_three_native_writers_make_two_rounds_with_their_own_seats(self):
        with tempfile.TemporaryDirectory() as root:
            b = fixture(root)
            got = snapshot(b)
            state = got['state']
            d = next(d for d in state['debates'] if d['root'] == b.meta['unit'])
            self.assertTrue(d['current'])
            tp = d['topics'][0]
            self.assertNotIn('room', tp)
            self.assertEqual(tp['rounds'], [1, 2])
            self.assertEqual([r['p'] for r in tp['rows']], list('abc'))
            self.assertEqual(len({lk.gkey() for lk in got['launches'].values()}), 1)
            self.assertEqual(len({lk.call for lk in got['launches'].values()}), 3)
            for row in tp['rows']:
                for c in row['cells']:
                    self.assertEqual(c['owner'], b.ids[row['p']])
                    self.assertEqual(c['state'], 'done')
                    self.assertEqual(c['evidence'], 'shell')
                    self.assertEqual(c['path'], os.path.join(b.meta['unit'], 'round%d_%s.md' % (c['round'], row['p'])))
            self.assertFalse(tp['final']['confirmed'])
            self.assertEqual(tp['final']['why'], ['several'])
            self.assertEqual(len(got['allowed']), 9)
            self.assertTrue(all(p == real for p, real in got['allowed'].items()))

    def test_a_missing_cell_keeps_a_file_round_path_in_the_table(self):
        with tempfile.TemporaryDirectory() as root:
            b = fixture(root, missing=True)
            state = snapshot(b)['state']
            tp = next(t for d in state['debates'] for t in d['topics'] if t['dir'] == b.meta['unit'])
            cell = next(r for r in tp['rows'] if r['p'] == 'b')['cells'][1]
            self.assertEqual(cell['state'], 'waiting')
            self.assertIsNone(cell['owner'])
            self.assertEqual(cell['path'], os.path.join(b.meta['unit'], 'round2_b.md'))


if __name__ == '__main__':
    unittest.main()
