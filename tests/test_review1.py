"""The first review of 0.3.0 (O14): the judgment cases the two reviews reproduced, each one first as a failing test.

    python3 -m unittest tests.test_review1
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, hint, launch, page_session, plan, tag, wr  # noqa: E402

from board import debates, units as U  # noqa: E402
from compat import server  # noqa: E402


class Boundary(Fixture):
    """A1: J3 ② holds the seat for a write that may have made the file first, and the edge of "first" is the same for a save that is sure and one that is not."""

    def owners(self, other, ts):
        y = self.file('u/r1/A.md', 'y\n', mtime=200.2)
        jd = self.assign(agent('Y', writes=[wr(y, 200.0)]), agent('X', writes=[other(y, ts)]))
        return self.cell(jd, 'u', 'r1', 'A').owner, sorted(d['code'] for d in jd.diag)

    def test_an_unsure_attempt_one_second_after_the_first_save_holds_the_seat_as_a_sure_one_does(self):
        unsure = lambda y, ts: wr(y, ts, ok=None, evidence='shell', proof='window', span=(ts - 2.0, ts + 0.5))       # noqa: E731
        failed = lambda y, ts: wr(y, ts, ok=False)                                                                    # noqa: E731
        sure = lambda y, ts: wr(y, ts)                                                                                # noqa: E731
        for name, other in (('unsure', unsure), ('failed', failed), ('sure', sure)):
            with self.subTest(name, ts='200.999'):
                self.assertEqual(self.owners(other, 200.999)[0], None)
            with self.subTest(name, ts='201.000'):
                self.assertEqual(self.owners(other, 201.0)[0], None, 'the edge is inside')
            with self.subTest(name, ts='201.001'):
                self.assertEqual(self.owners(other, 201.001)[0], 'Y')
        self.assertEqual(self.owners(unsure, 201.0)[1], ['seat_tie_held', 'seat_tie_held'])


class Editors(Fixture):
    """A4: the editors of a cell that was taken over twice (J4, J5): the owner just before first, the others as they first wrote."""

    def test_the_owner_before_comes_first_and_the_rest_by_their_first_write(self):
        p = self.file('talk/r1/A.md', 'x\n', mtime=240.5)
        a = agent('A', writes=[wr(p, 200)], start=100, last=210)
        b = agent('B', writes=[wr(p, 220)], planned=[plan(p, ts=219)], start=219, last=230)
        c = agent('C', writes=[wr(p, 240)], planned=[plan(p, ts=239)], start=239, last=250)
        cell = self.cell(self.assign(a, b, c), 'talk', 'r1', 'A')
        self.assertEqual((cell.owner, cell.editors), ('C', ['B', 'A']))


class RootPlacement(Fixture):
    """A3: what the orchestrator did in the root of a bundle is a placement of its own: a hint of a topic does not narrow it to the topics it does not name."""

    def setUp(self):
        super().setUp()
        self.file('b/brief.md', '# b\n')
        for t in ('t1', 't2', 't3'):
            self.dirs('b/%s/r1' % t)
        self.p1 = self.file('b/t1/r1/P.md', 'p\n', mtime=200.5)
        self.fin = self.file('b/t1/ruling.md', 'r\n', mtime=300.5)
        self.P = agent('P', writes=[wr(self.p1, 200)], status='done', start=100, last=250)
        self.X = agent('X', launch=launch(group='mX', call='tX'), status='running', start=100, last=400)

    def judge(self, **hints):
        hs = {self.p(k.replace('_', '/')): v for k, v in hints.items()}
        return self.assign(self.P, self.X, orch=[wr(self.fin, 300)], hints=hs)

    def test_a_hint_of_the_root_places_the_agent_at_the_root_whatever_the_topics_say(self):
        t1 = self.p('b/t1')
        for name, hs in (('root alone', {'b': hint(50, calls=('o1',), groups=('mX',))}),
                         ('and a topic with a call that is not the agent\'s', {'b': hint(50, calls=('o1',), groups=('mX',)), 'b_t1': hint(60, calls=('o2',), groups=('mY',)), 'b_t2': hint(60, calls=('o3',), groups=('mZ',))})):
            with self.subTest(name):
                jd = self.judge(**hs)
                pl = jd.placed['X']
                self.assertEqual((pl.unit, pl.topic, pl.why), (self.p('b'), None, 'launch_call'))
                self.assertFalse(jd.finals[t1].confirmed)
                self.assertIn('live_participant', jd.finals[t1].why)
                self.assertFalse(jd.closable[t1])

    def test_a_hint_of_a_topic_and_one_of_the_root_both_count_and_the_root_one_holds_every_topic(self):
        jd = self.judge(b=hint(50, calls=('o1',), groups=('mX',)), b_t3=hint(60, calls=('tX',)))
        pl = jd.placed['X']
        self.assertEqual((pl.unit, pl.topic), (self.p('b'), None))
        self.assertFalse(jd.closable[self.p('b/t1')])

    def test_a_hint_of_one_topic_alone_places_the_agent_at_that_topic(self):
        jd = self.judge(b=hint(50, calls=('o1',)), b_t3=hint(60, calls=('tX',)))
        pl = jd.placed['X']
        self.assertEqual((pl.unit, pl.topic), (self.p('b'), self.p('b/t3')))
        self.assertTrue(jd.finals[self.p('b/t1')].confirmed)                        # the other topic is not held back by an agent that is placed at one topic only


class MissingStatus(Fixture):
    """L4: a state nobody gave is not a state that says it ended."""

    def test_an_agent_with_no_state_is_one_that_may_still_work(self):
        a = self.file('v/r1/A.md', 'a\n', mtime=200.5)
        fin = self.file('v/ruling.md', 'r\n', mtime=300.5)
        out = {}
        for st in ('unknown', None):
            jd = self.assign(agent('A', writes=[wr(a, 200)], status=st, start=100, last=250), orch=[wr(fin, 300)])
            out[st] = (self.cell(jd, 'v', 'r1', 'A').state, jd.finals[self.p('v')].confirmed, jd.finals[self.p('v')].why, jd.closable[self.p('v')])
        self.assertEqual(out[None], out['unknown'])
        self.assertEqual(out[None][:2], ('draft', False))
        self.assertFalse(out[None][3])

    def test_an_agent_with_no_state_does_not_hand_its_seat_over(self):
        f = self.file('v/r1/A.md', 'a\n', mtime=200.5)
        a = agent('A', writes=[wr(f, 200)], status=None, start=100, last=150)
        b = agent('B', writes=[wr(f, 260)], planned=[plan(f, ts=250)], start=250, last=300)
        jd = self.assign(a, b)
        self.assertEqual(self.cell(jd, 'v', 'r1', 'A').owner, 'A')                  # B is an editor: nothing says A is over

    def test_the_page_shows_a_placed_agent_with_no_state_as_live(self):
        self.file('b/brief.md', '# b\n')
        self.dirs('b/t1/r1', 'b/t2/r1')
        p = self.file('b/t1/r1/P.md', 'p\n', mtime=200.5)
        q = self.file('b/t2/r1/Q.md', 'q\n', mtime=200.5)
        hs = {self.p('b'): hint(50, groups=('mX',))}
        s, statuses = page_session(agent('P', writes=[wr(p, 200)]), agent('Q', writes=[wr(q, 200)]), agent('X', launch=launch(group='mX'), status='running'), hints=hs)
        def rows():
            return [r for d in debates.judge(s, statuses).debates for r in d['placed']]
        for st in ('running', None):
            statuses['X'] = st
            self.assertTrue(rows() and all(r['live'] for r in rows()), st)
        del statuses['X']                                                           # (a table with a hole in it)
        self.assertTrue(rows() and all(r['live'] for r in rows()))
        statuses['X'] = 'done'
        self.assertTrue(rows() and not any(r['live'] for r in rows()))


class Resumed(Fixture):
    """J6 as D19 says: a save of an earlier run stays a submission until the run now asks for the file again."""

    def test_resuming_an_agent_does_not_make_its_earlier_report_a_draft(self):
        a = self.file('w/r1/A.md', 'a\n', mtime=200.5)
        self.dirs('w/r2')
        A = agent('A', writes=[wr(a, 200, run=1)], status='running', start=100, last=900, run=2, run_start=800)
        c = self.cell(self.assign(A), 'w', 'r1', 'A')
        self.assertEqual((c.state, c.previous, c.owner), ('done', False, 'A'))

    def test_the_run_that_writes_the_file_again_works_on_it(self):
        a = self.file('w/r1/A.md', 'a\n', mtime=900.5)
        A = agent('A', writes=[wr(a, 200, run=1), wr(a, 900, run=2)], status='running', start=100, last=950, run=2, run_start=800)
        self.assertEqual(self.cell(self.assign(A), 'w', 'r1', 'A').state, 'draft')

    def test_a_request_of_the_run_that_is_not_met_yet_is_not_a_submission(self):
        a = self.file('w/r1/A.md', 'a\n', mtime=200.5)
        A = agent('A', writes=[wr(a, 200, run=1)], planned=[plan(a, ts=810, run=2)], status='running', start=100, last=950, run=2, run_start=800)
        c = self.cell(self.assign(A), 'w', 'r1', 'A')
        self.assertEqual((c.state, c.previous), ('writing', True))

    def test_a_run_nobody_knows_holds_the_cell(self):
        a = self.file('w/r1/A.md', 'a\n', mtime=200.5)
        A = agent('A', writes=[wr(a, 200, run=None)], status='running', start=100, last=950, run=2, run_start=800)
        self.assertEqual(self.cell(self.assign(A), 'w', 'r1', 'A').state, 'draft')

    def test_an_earlier_request_that_was_met_and_nothing_since_is_a_submission(self):
        a = self.file('w/r1/A.md', 'a\n', mtime=200.5)
        A = agent('A', writes=[wr(a, 200, run=1)], planned=[plan(a, ts=150, run=1)], status='running', start=100, last=950, run=2, run_start=800)
        c = self.cell(self.assign(A), 'w', 'r1', 'A')
        self.assertEqual((c.state, c.previous), ('done', False))


class Rivals(Fixture):
    """J15: a document that appeared or changed after the last sure write on a cell with no event of anybody is a competitor of the final (principle U), and a file of a member is no document of the room."""

    def test_a_document_that_came_with_no_record_competes_with_the_final(self):
        a = self.file('t/r1/A.md', 'a\n', mtime=200.5)
        fin = self.file('t/ruling.md', 'r\n', mtime=300.5)
        self.file('t/final_by_python.md', 'other\n', mtime=310.5)
        jd = self.assign(agent('A', writes=[wr(a, 200)], status='done', start=100, last=250), orch=[wr(fin, 300)])
        f = jd.finals[self.p('t')]
        self.assertFalse(f.confirmed)
        self.assertEqual(f.why, ['several'])
        self.assertEqual(f.path, None)

    def test_a_document_with_no_record_that_is_older_than_the_last_save_is_no_competitor(self):
        a = self.file('t/r1/A.md', 'a\n', mtime=200.5)
        fin = self.file('t/ruling.md', 'r\n', mtime=300.5)
        self.file('t/older.md', 'other\n', mtime=100.5)
        f = self.assign(agent('A', writes=[wr(a, 200)], status='done', start=100, last=250), orch=[wr(fin, 300)]).finals[self.p('t')]
        self.assertEqual((f.confirmed, os.path.basename(f.path)), (True, 'ruling.md'))

    def test_a_document_with_no_record_alone_is_no_final(self):
        a = self.file('t/r1/A.md', 'a\n', mtime=200.5)
        self.file('t/by_python.md', 'other\n', mtime=310.5)
        f = self.assign(agent('A', writes=[wr(a, 200)], status='done', start=100, last=250)).finals[self.p('t')]
        self.assertEqual((f.confirmed, f.path, f.why), (False, None, ['none']))

    def test_a_document_of_a_bundle_that_came_with_no_record_competes_too(self):
        self.file('b/brief.md', '# b\n')
        a = self.file('b/t1/r1/A.md', 'a\n', mtime=200.5)
        b = self.file('b/t2/r1/B.md', 'b\n', mtime=200.5)
        fin = self.file('b/final/ruling.md', 'r\n', mtime=300.5)
        self.file('b/final/other.md', 'o\n', mtime=310.5)
        jd = self.assign(agent('A', writes=[wr(a, 200)]), agent('B', writes=[wr(b, 200)]), orch=[wr(fin, 300)])
        self.assertIn('several', jd.finals[self.p('b')].why)

    def room(self):
        room = self.p('room')
        os.makedirs(room)
        a = self.file('room/A.md', 'a\n', mtime=200.5)
        b = self.file('room/B.md', 'b\n', mtime=210.5)
        return room, a, b

    def test_a_second_file_of_a_member_is_not_the_final_of_its_room(self):
        room, a, b = self.room()
        n = self.file('room/A_notes.md', 'n\n', mtime=300.5)
        A = agent('A', writes=[wr(a, 200), wr(n, 300)], tag=tag(room), status='done', start=100, last=350)
        B = agent('B', writes=[wr(b, 210)], tag=tag(room), status='done', start=100, last=250)
        jd = self.assign(A, B)
        self.assertEqual(jd.rooms[room].kind, 'cells')
        f = jd.finals[room]
        self.assertEqual((f.confirmed, f.path), (False, None))
        self.assertNotIn(n, f.candidates)
        self.assertFalse(jd.closable[room])

    def test_a_file_in_the_room_that_the_orchestrator_wrote_is_a_document_of_it(self):
        room, a, b = self.room()
        fin = self.file('room/ruling.md', 'r\n', mtime=400.5)
        A = agent('A', writes=[wr(a, 200)], tag=tag(room), status='done', start=100, last=350)
        B = agent('B', writes=[wr(b, 210)], tag=tag(room), status='done', start=100, last=250)
        f = self.assign(A, B, orch=[wr(fin, 400)]).finals[room]
        self.assertEqual((f.confirmed, os.path.basename(f.path or '')), (True, 'ruling.md'))

    def test_a_file_of_a_member_that_the_orchestrator_touched_too_stays_a_candidate(self):
        room, a, b = self.room()
        n = self.file('room/A_notes.md', 'n\n', mtime=320.5)
        A = agent('A', writes=[wr(a, 200), wr(n, 300)], tag=tag(room), status='done', start=100, last=350)
        B = agent('B', writes=[wr(b, 210)], tag=tag(room), status='done', start=100, last=250)
        f = self.assign(A, B, orch=[wr(n, 320, kind='replace')]).finals[room]
        self.assertIn(n, f.candidates)


class MemberFiles(Fixture):
    """O14a: a file a member of a room made is no candidate of the room, but it competes when it may be the later word."""

    def judged(self, a_extra=(), orch=(), b_extra=()):
        room = self.p('room')
        a = self.file('room/A.md', 'a\n', mtime=200.5)
        b = self.file('room/B.md', 'b\n', mtime=210.5)
        A = agent('A', writes=[wr(a, 200), *a_extra], tag=tag(room), status='done', start=100, last=450)
        B = agent('B', writes=[wr(b, 210), *b_extra], tag=tag(room), status='done', start=100, last=450)
        return room, self.assign(A, B, orch=list(orch))

    def test_a_member_file_written_after_the_final_competes_with_it(self):
        s = self.file('room/summary.md', 's\n', mtime=400.5)
        t = self.file('room/A_take.md', 't\n', mtime=410.5)
        room, jd = self.judged([wr(t, 410)], [wr(s, 400)])
        f = jd.finals[room]
        self.assertEqual((f.confirmed, f.why, f.candidates), (False, ['several'], [s]))                 # no candidate of the room, and a rival all the same

    def test_a_member_file_that_changed_after_its_last_record_competes(self):
        s = self.file('room/summary.md', 's\n', mtime=400.5)
        t = self.file('room/A_take.md', 't2\n', mtime=420.5)
        room, jd = self.judged([wr(t, 300)], [wr(s, 400)])
        self.assertEqual((jd.finals[room].confirmed, jd.finals[room].why), (False, ['several']))

    def test_a_member_report_that_finished_before_the_final_does_not(self):
        s = self.file('room/summary.md', 's\n', mtime=400.5)
        n = self.file('room/A_notes.md', 'n\n', mtime=300.5)
        room, jd = self.judged([wr(n, 300)], [wr(s, 400)])
        f = jd.finals[room]
        self.assertEqual((f.confirmed, f.path, f.candidates), (True, s, [s]))

    def test_a_member_file_alone_is_no_final(self):
        n = self.file('room/A_notes.md', 'n\n', mtime=300.5)
        room, jd = self.judged([wr(n, 300)])
        self.assertEqual((jd.finals[room].confirmed, jd.finals[room].why), (False, ['none']))

    def test_a_failed_try_at_a_file_of_nobody_is_a_rival_as_before(self):
        s = self.file('room/summary.md', 's\n', mtime=400.5)
        t = self.file('room/A_take.md', 't\n', mtime=410.5)
        room, jd = self.judged([wr(t, 410, ok=False)], [wr(s, 400)])
        self.assertEqual((jd.finals[room].confirmed, jd.finals[room].why), (False, ['several']))

    def test_a_member_file_just_before_the_final_does_not_compete_by_the_rule_of_O14a(self):
        # the rule is "not before the final's write less SAME_TIME": 5 s before is before. (probe_o14 shape 'A final.md + orch notes.md' reads as several to the review: a question for the contract)
        fn = self.file('room/final.md', 'f\n', mtime=400.5)
        nt = self.file('room/notes.md', 'n\n', mtime=405.5)
        room, jd = self.judged([wr(fn, 400)], [wr(nt, 405)])
        self.assertEqual((jd.finals[room].confirmed, os.path.basename(jd.finals[room].path or '')), (True, 'notes.md'))


class Names(unittest.TestCase):
    """Opus P2-5: a message goes to whom its `to` names by id, and not by what a description or a letter of it says."""

    def agents(self):
        out = {}
        for i, d in (('a0000000000000001', 'A reviewer'), ('a0000000000000002', 'B writer'), ('a0000000000000003', 'opus-1 Budget')):
            out[i] = server.Agent(i, {'description': d})
        out['a0000000000000001'].auto_tag = 'opus5.5'
        return types.SimpleNamespace(agents=out)

    def test_only_an_id_names_an_agent(self):
        names = debates.peer_names(self.agents())
        self.assertEqual(names, {'a0000000000000001': 'a0000000000000001', 'a0000000000000002': 'a0000000000000002', 'a0000000000000003': 'a0000000000000003'})

    def test_a_message_to_a_role_letter_or_a_description_went_to_nobody(self):
        s = self.agents()
        sender = s.agents['a0000000000000001']
        names = debates.peer_names(s)
        for to in ('A', 'B', 'a reviewer', 'B writer', 'opus5.5', 'opus-1', 'A0000000000000002'.upper()):
            sender.sent = [{'ts': 1, 'to': to, 'ok': True}]
            got = debates.facts_of(sender, '', names, 'done').sent_to
            self.assertEqual(got, ('a0000000000000002',) if to.lower() == 'a0000000000000002' else (), to)


if __name__ == '__main__':
    unittest.main()
