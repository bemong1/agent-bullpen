"""Judged collision paths survive the diagnostic API, and report keys contain only path components."""
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, tag, wr  # noqa: E402
from board import diag  # noqa: E402


class JudgmentAPI(Fixture):
    def collect(self, rows):
        s = types.SimpleNamespace(id='11111111-2222-4333-8444-555555555555', provider='codex', agents={}, runs=None, debate_diag=rows)
        links = types.SimpleNamespace(diags=[], unlinked_for=lambda _sid: [], lineage=types.SimpleNamespace(write_error=None, cache=None))
        with mock.patch.object(diag, 'LINKS', links), mock.patch.object(diag, 'codex_drift', return_value=set()), mock.patch.object(diag.RS, 'group_diag', return_value=[]):
            return diag.collect(s, 500, {}, [])

    def test_long_collision_details_are_summarized_and_the_full_paths_reach_the_api(self):
        seat = 'review_' + 'x' * 70
        names = ('round1_%s.md' % seat, 'ROUND1_%s.MD' % seat, 'round1_b.md')
        paths = [self.file('talk/' + n, mtime=200 + i * 10) for i, n in enumerate(names)]
        authors = [agent(aid, writes=[wr(p, 200 + i * 10)]) for i, (aid, p) in enumerate(zip('ABC', paths))]
        result = self.page(*authors)
        (collision,) = [d for d in result.diag if d['code'] == 'alias_collision']
        self.assertGreater(len(collision['detail']), diag.VALUE_MAX)
        (entry,) = [d for d in self.collect(result.diag) if d['code'] == 'alias_collision']
        self.assertEqual(entry['params'], {'detail': collision['detail'][:diag.VALUE_MAX - 1] + '…', 'paths': sorted(paths[:2])})
        self.assertEqual(json.loads(json.dumps(entry)), entry)
        self.assertFalse(result.finals[self.p('talk')].confirmed)

    def test_report_keys_identify_each_physical_path_in_every_debate_shape(self):
        for kind in ('file', 'folder', 'room'):
            with self.subTest(kind=kind):
                unit = 'talk_' + kind
                names = ('round1_a.md', 'ROUND1_a.MD', 'round1_b.md') if kind == 'file' else ('r1/A.md', 'r1/A.MD', 'r1/B.md') if kind == 'folder' else ('A.md', 'A.MD', 'B.md')
                paths = [self.file(unit + '/' + n, mtime=200 + i * 10) for i, n in enumerate(names)]
                authors = [agent(aid, writes=[wr(p, 200 + i * 10)], tag=tag(self.p(unit)) if kind == 'room' else None)
                           for i, (aid, p) in enumerate(zip('ABC', paths))]
                result = self.page(*authors)
                reports = [p for d in result.debates for t in d['topics'] for r in t['rows'] for c in r['cells'] for p in c['reports']]
                self.assertEqual({p['path'] for p in reports}, set(paths))
                self.assertEqual(len({tuple(p['key']) for p in reports}), len(paths))
                self.assertTrue(all(os.path.join(*p['key']) == p['path'] for p in reports))
                self.assertTrue(all('\0' not in part for p in reports for part in p['key']))
                self.assertTrue(any('\0' in k[2] for k in result.cells))
                self.assertEqual(json.loads(json.dumps(reports)), reports)


if __name__ == '__main__':
    unittest.main()
