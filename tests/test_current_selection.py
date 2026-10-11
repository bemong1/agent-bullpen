"""J10: open placements in orchestrator-made debates can select the default before any report exists."""
import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, hint, launch, rd, wr  # noqa: E402


class CurrentSelection(Fixture):
    def setUp(self):
        super().setUp()
        path = self.file('old/r1/A.md', mtime=220)
        self.old = agent('A', writes=[wr(path, 220)], start=200)
        self.file('talk/brief.md', mtime=250)
        self.dirs('talk/r1', 'talk/r2')
        self.hints = {self.p('talk'): hint(250)}

    def reader(self, **kw):
        return agent('B', reads=[rd(self.p('talk/brief.md'), 310)], status='running', start=300, **kw)

    def test_open_statuses_promote_a_guide_reader_without_confirming_any_seat_or_final(self):
        for status in ('running', 'stalled', 'unknown', None):
            with self.subTest(status=status):
                reader = dataclasses.replace(self.reader(), status=status)
                jd = self.assign(self.old, reader, hints=self.hints)
                self.assertEqual(jd.current, self.p('talk'))
                self.assertEqual(jd.placed['B'].why, 'guide_read')
                self.assertFalse(jd.placed['B'].sure)
                self.assertEqual(jd.bound, {self.p('old'): {'A'}})
                self.assertFalse(any(c.unit == self.p('talk') for c in jd.cells.values()))
                self.assertNotIn('B', jd.agent_units)
                self.assertFalse(jd.finals[self.p('talk')].confirmed)
                self.assertFalse(jd.closable[self.p('talk')])
                page = self.page(self.old, reader, hints=self.hints)
                self.assertEqual([d['root'] for d in page.debates if d['current']], [self.p('talk')])
                self.assertEqual(page.members['B'], {self.p('talk')})
                self.assertEqual(page.debates[0]['topics'][0]['placed'][0]['agent'], 'B')

    def test_a_launch_placement_competes_without_reading_the_guide(self):
        called = agent('B', launch=launch(call='start'), status='running', start=300)
        jd = self.assign(self.old, called, hints={self.p('talk'): hint(250, calls=('start',))})
        self.assertEqual((jd.current, jd.placed['B'].why), (self.p('talk'), 'launch_call'))
        self.assertEqual(jd.bound, {self.p('old'): {'A'}})

    def test_a_finished_or_interrupted_placement_does_not_promote_the_debate(self):
        for status in ('done', 'ended', 'interrupted', 'error', 'limit_wait'):
            with self.subTest(status=status):
                jd = self.assign(self.old, dataclasses.replace(self.reader(), status=status), hints=self.hints)
                self.assertEqual(jd.current, self.p('old'))

    def test_a_guide_read_without_an_orchestrator_hint_does_not_promote_a_walked_debate(self):
        jd = self.assign(self.old, self.reader(), walked=[self.p('talk')])
        self.assertEqual(jd.placed['B'].why, 'guide_read')
        self.assertEqual(jd.current, self.p('old'))

    def test_hint_and_open_agent_without_a_placement_do_not_promote_the_debate(self):
        jd = self.assign(self.old, agent('B', status='running', start=300), hints=self.hints)
        self.assertNotIn('B', jd.placed)
        self.assertEqual(jd.current, self.p('old'))

    def test_multiple_candidates_use_the_latest_agent_start_instead_of_hint_time(self):
        self.file('new/brief.md', mtime=100)
        self.dirs('new/r1')
        c = agent('C', reads=[rd(self.p('new/brief.md'))], status='running', start=400)
        hints = {self.p('talk'): hint(900), self.p('new'): hint(100)}
        jd = self.assign(self.old, self.reader(), c, hints=hints)
        self.assertEqual(jd.order, [self.p('new'), self.p('talk'), self.p('old')])
        self.assertEqual(jd.current, self.p('new'))
        newer_bound = dataclasses.replace(self.old, start=500)
        self.assertEqual(self.assign(newer_bound, self.reader(), c, hints=hints).current, self.p('old'))

    def test_a_peer_alone_does_not_supply_the_new_selection_evidence(self):
        author = agent('C', writes=[wr(self.file('talk/r1/C.md', mtime=120), 120)], start=100, launch=launch())
        peer = agent('B', launch=launch(call='peer'), status='running', start=500)
        jd = self.assign(self.old, author, peer, hints={self.p('talk'): hint(90)})
        self.assertEqual(jd.placed['B'].whys, ['launch_peer'])
        self.assertEqual(jd.current, self.p('old'))

    def test_split_and_vetoed_placements_do_not_promote_a_debate(self):
        self.file('other/brief.md', mtime=250)
        self.dirs('other/r1')
        split = dataclasses.replace(self.reader(), reads=self.reader().reads + agent('B', reads=[rd(self.p('other/brief.md'))]).reads)
        jd = self.assign(self.old, split, hints={**self.hints, self.p('other'): hint(250)})
        self.assertNotIn('B', jd.placed)
        self.assertEqual(jd.current, self.p('old'))
        self.dirs('.git')
        vetoed = dataclasses.replace(self.reader(), writes=agent('B', writes=[wr(self.file('outside.md', mtime=320), 320)]).writes)
        jd = self.assign(self.old, vetoed, hints=self.hints)
        self.assertNotIn('B', jd.placed)
        self.assertEqual(jd.current, self.p('old'))

    def test_a_hint_on_another_topic_does_not_promote_the_read_topic(self):
        self.file('bundle/brief.md', mtime=250)
        self.file('bundle/made/brief.md', mtime=250)
        self.file('bundle/read/brief.md', mtime=250)
        self.dirs('bundle/made/r1', 'bundle/read/r1')
        reader = agent('B', reads=[rd(self.p('bundle/read/brief.md'))], status='running', start=300)
        jd = self.assign(self.old, reader, hints={self.p('bundle/made'): hint(250)}, walked=[self.p('bundle/read')])
        self.assertEqual(jd.placed['B'].topic, self.p('bundle/read'))
        self.assertEqual(jd.current, self.p('old'))

    def bundle(self):
        self.file('bundle/brief.md', mtime=90)
        for topic in ('made', 'read'):
            self.file('bundle/%s/brief.md' % topic, mtime=90)
            self.dirs('bundle/%s/r1' % topic)
        return agent('C', writes=[wr(self.file('bundle/made/r1/C.md', mtime=120), 120)], start=100, last=150, launch=launch(call='author'))

    def test_a_peer_in_a_hinted_topic_cannot_borrow_a_read_of_another_topic(self):
        author = self.bundle()
        reader = agent('B', reads=[rd(self.p('bundle/read/brief.md'), 310)], status='running', start=300, launch=launch(call='reader'))
        hints, walked = {self.p('bundle/made'): hint(90)}, [self.p('bundle/read')]
        plain = self.assign(self.old, author, hints=hints, walked=walked)
        jd = self.assign(self.old, author, reader, hints=hints, walked=walked)
        self.assertEqual(jd.placed['B'].whys, ['launch_peer', 'guide_read'])
        self.assertIsNone(jd.placed['B'].topic)
        self.assertEqual(jd.current, self.p('old'))
        self.assertEqual(jd.placed['B'].whys_by_unit, {self.p('bundle/made'): ['launch_peer'], self.p('bundle/read'): ['guide_read']})
        for field in ('cells', 'bound', 'agent_units', 'assignments', 'closable'):
            self.assertEqual(getattr(jd, field), getattr(plain, field))
        page = self.page(self.old, author, reader, hints=hints, walked=walked)
        self.assertEqual([d['root'] for d in page.debates if d['current']], [self.p('old')])

    def test_reads_match_the_hint_of_their_exact_topic_or_bundle_root(self):
        author = self.bundle()
        for read_scope, hint_scope, promote in (('bundle', 'bundle', True), ('bundle', 'bundle/made', False),
                                               ('bundle/read', 'bundle/made', False), ('bundle/read', 'bundle', False),
                                               ('bundle/made', 'bundle/made', True)):
            with self.subTest(read_scope=read_scope, hint_scope=hint_scope):
                reader = agent('B', reads=[rd(self.p(read_scope + '/brief.md'), 310)], status='running', start=300, launch=launch(call='reader'))
                jd = self.assign(self.old, author, reader, hints={self.p(hint_scope): hint(90)}, walked=[self.p('bundle/read')])
                self.assertEqual(jd.current, self.p('bundle' if promote else 'old'))
                self.assertIn('guide_read', jd.placed['B'].whys_by_unit[self.p(read_scope)])

    def test_a_launch_in_the_bundle_root_needs_an_explicit_root_guide_read(self):
        author = self.bundle()
        called = agent('B', launch=launch(call='reader'), status='running', start=300)
        hints = {self.p('bundle'): hint(90, calls=('reader',))}
        jd = self.assign(self.old, author, called, hints=hints, walked=[self.p('bundle/read')])
        self.assertIn('launch_call', jd.placed['B'].whys)
        self.assertEqual(jd.current, self.p('old'))


if __name__ == '__main__':
    unittest.main()
