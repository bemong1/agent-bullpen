"""Edges of the judgment that the contract cases do not reach: what an adversarial reading of J1–J20 found, kept as tests.

    python3 -m unittest tests.test_judgment_edges
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, hint, launch, plan, tag, win, wr  # noqa: E402

from board import debates, units as U  # noqa: E402


class Names(Fixture):
    def test_a_name_the_disk_cannot_hold_does_not_stop_the_judgment(self):
        bad = self.p('a\x00b', 'talk', 'r1', 'A.md')
        good = self.file('talk/r1/B.md', mtime=200.0)
        jd = self.assign(agent('A', writes=[wr(bad, 100.0)], planned=[plan(bad)], tag=tag(self.p('a\x00b', 'c'))), agent('B', writes=[wr(good, 200.0)]),
                         hints={self.p('a\x00b', 'c'): hint()}, walked=[self.p('a\x00b', 'c')])
        self.assertEqual(self.cell(jd, 'talk', 'r1', 'B').owner, 'B')

    def test_the_name_of_a_report_may_have_any_letters(self):
        self.assertEqual(U.listing_hint(self.p('talk', 'r1', '검토.md')), self.p('talk'))
        self.assertEqual(U.report_of_path(self.p('talk', 'r1', '검토 1.md'))[1:], (1, '검토 1', 'r1'))

    def test_a_write_through_a_link_in_the_folder_of_a_room_is_the_same_folder(self):
        os.makedirs(self.p('real', 'room'))
        os.symlink(self.p('real'), self.p('alias'))
        files = {n: self.file('real/room/%s.md' % n, mtime=200.0) for n in 'xy'}
        logs = {n: self.p('alias', 'room', '%s.log' % n) for n in 'xy'}
        for n in 'xy':
            self.file('real/room/%s.log' % n, mtime=201.0)
        agents = [agent(n, writes=[wr(files[n], 200.0), wr(logs[n], 201.0, evidence='shell', proof='exit', kind='replace')], launch=launch('m1', 't' + n)) for n in 'xy']
        (room,) = self.assign(*agents).rooms.values()                                       # the logs are inside the room by their real path, not work done outside it
        self.assertEqual(room.members, ['x', 'y'])
        self.assertTrue(room.folder.startswith(self.p('real')))


class Rooms(Fixture):
    def test_the_room_is_the_folder_that_holds_the_files_and_not_the_one_above_it(self):
        agents = [agent(n, writes=[wr(self.file('notes/work/%s.md' % n, mtime=200.0), 200.0)], launch=launch('m1', 't' + n)) for n in 'AB']
        (room,) = self.assign(*agents).rooms.values()                                       # no repository, nothing above to stop at: the folder of the files is the room
        self.assertEqual((room.folder, room.members), (self.p('notes', 'work'), ['A', 'B']))

    def test_a_folder_that_is_no_room_does_not_make_the_one_above_it_a_room(self):
        os.makedirs(self.p('proj', '.git'))
        agents = [agent(n, writes=[wr(self.file('proj/%s.md' % n, mtime=200.0), 200.0)], launch=launch('m1', 't' + n)) for n in 'AB']
        self.assertEqual(self.assign(*agents).rooms, {})                                    # the top of a repository is a place everybody reads, and so is no room, nor is its parent


class Sure(Fixture):
    def test_a_proof_nobody_knows_is_not_sure(self):
        f = self.file('talk/r1/A.md', mtime=200.0)
        jd = self.assign(agent('A', writes=[wr(f, 200.0, proof='guess')]), agent('B', writes=[wr(self.file('talk/r1/B.md', mtime=210.0), 210.0)]))
        self.assertIsNone(self.cell(jd, 'talk', 'r1', 'A').owner)
        self.assertEqual(self.cell(jd, 'talk', 'r1', 'B').owner, 'B')

    def test_the_save_of_a_launch_command_in_a_dot_folder_is_checked_like_any_other(self):
        f = self.file('.work/talk/r1/B.md', 'out-B', mtime=250.0)
        a = agent('B', writes=[wr(f, 300.0, kind='replace', evidence='planned', proof='sha', span=(200.0, 300.0), text='out-B')], planned=[plan(f, ts=200.0)])
        self.assertEqual(self.cell(self.assign(a), '.work/talk', 'r1', 'B').owner, 'B')

    def test_the_evidence_of_a_requester_that_has_a_launch_output_and_a_seat_is_planned(self):
        self.dirs('talk/r1')
        a = agent('A', planned=[plan(self.p('talk/r1/A.md'), ts=100.0)], tag=tag(self.p('talk'), 'r1/A'), status='running', run_start=50.0)
        self.assertEqual(self.cell(self.assign(a), 'talk', 'r1', 'A').evidence, 'planned')
        b = agent('B', planned=[plan(self.p('talk/r1/B.md'), ts=100.0)], tag=tag(self.p('talk'), 'r1/B'), status='running', run_start=150.0)
        self.assertEqual(self.cell(self.assign(b), 'talk', 'r1', 'B').evidence, 'planned')


class Hints(Fixture):
    def test_a_window_let_go_near_the_time_of_a_coarse_file_makes_the_hint_unknown(self):
        f = self.file('talk/r1/B.md', mtime=100.0)                                          # whole seconds: the time is in [100, 101)
        a = agent('B', windows=[win(100.15, 100.2)], windows_dropped=(100.5, 100.6))
        self.assertIsNone(self.cell(self.assign(a, agent('A', writes=[wr(self.file('talk/r1/A.md', mtime=200.0), 200.0)])), 'talk', 'r1', 'B').hint)
        a = agent('B', windows=[win(100.15, 100.2)])
        self.assertEqual(self.cell(self.assign(a, agent('A', writes=[wr(self.file('talk/r1/A.md', mtime=200.0), 200.0)])), 'talk', 'r1', 'B').hint, {'kind': 'window', 'agent': 'B'})
        self.assertTrue(f)


class Requests(Fixture):
    def test_a_request_for_a_round_that_is_not_made_yet_still_holds_the_final_back(self):
        self.dirs('talk/r1')
        a = agent('A', writes=[wr(self.file('talk/r1/A.md', mtime=200.0), 200.0)])
        b = agent('B', writes=[wr(self.file('talk/r1/B.md', mtime=210.0), 210.0)])
        j = agent('J', writes=[wr(self.file('talk/ruling.md', mtime=300.0), 300.0)])
        p = agent('P', planned=[plan(self.p('talk/r2/P.md'), ts=250.0)], status='running')
        jd = self.assign(a, b, j, p)
        final = jd.finals[self.p('talk')]
        self.assertFalse(final.confirmed)
        self.assertIn('open_cell', final.why)
        self.assertFalse(jd.closable[self.p('talk')])
        self.assertEqual(self.cell(jd, 'talk', 'r2', 'P').agent, 'P')


class Page(Fixture):
    def test_a_cell_of_a_round_that_is_not_on_disk_shows_on_the_page(self):
        self.dirs('talk/r1')
        a = agent('A', writes=[wr(self.file('talk/r1/A.md', mtime=200.0), 200.0)])
        b = agent('B', tag=tag(self.p('talk'), 'r2/B'), status='running')
        r = self.page(a, b)
        cells = {(row['p'], c['round']): c for d in r.debates for t in d['topics'] for row in t['rows'] for c in row['cells']}
        self.assertEqual((cells[('B', 2)]['agent'], cells[('B', 2)]['evidence'], cells[('B', 2)]['state']), ('B', 'tag', 'writing'))

    def test_the_page_has_a_current_debate_when_the_first_of_the_list_is_left_out_of_it(self):
        a = agent('A', writes=[wr(self.file('p/r1/A.md', mtime=200.0), 200.0)])
        self.dirs('p/sub/r1')
        t = agent('T', tag=tag(self.p('p', 'sub')), status='running', start=500.0)
        r = self.page(a, t)
        self.assertTrue(r.debates)
        self.assertEqual([d['root'] for d in r.debates if d['current']], [r.debates[0]['root']])

    def test_a_placement_on_the_root_of_one_topic_shows_at_that_topic(self):
        self.file('rev/brief.md', '# R\n', mtime=90.0)
        a = agent('A', writes=[wr(self.file('rev/t1/r1/A.md', mtime=200.0), 200.0)])
        c = agent('C', tag=tag(self.p('rev')), status='running')
        r = self.page(a, c)
        (d,) = r.debates
        self.assertEqual(d['placed'], [])
        self.assertEqual([x['agent'] for x in d['topics'][0]['placed']], ['C'])

    def test_a_judgment_without_statuses_is_made_as_if_nobody_were_known(self):
        self.file('rev/brief.md', '# R\n', mtime=90.0)
        a = agent('A', writes=[wr(self.file('rev/t1/r1/A.md', mtime=200.0), 200.0)])
        c = agent('C', tag=tag(self.p('rev')), status='running')
        r = debates.judge(*(lambda s, _st: (s, None))(*__import__('judge_support').page_session(a, c)))
        self.assertTrue(r.debates)


if __name__ == '__main__':
    unittest.main()
