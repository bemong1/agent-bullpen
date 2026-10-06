"""Rooms: agents of one orchestrator that work together in a folder (a meeting, an agenda, a plan ...) are shown like a debate. A room is found by structure alone (CONTRACT J13, J14, J15):
the records say which agents were launched together (`launch(group)`), which of them carry the same room tag (`tag(room, seat)`), the files each of them wrote with a sure write (`writes=`), what
they read (`reads=`) and whom they sent a message to (`sent_to=`); the disk says which folder it is. No sentence is read: what an agent was told cannot even be given.

  - a sure room: two or more agents with the same room tag on a folder that has no round folder
  - an unsure room (an estimate: never the current debate, never final): agents launched together that each wrote a file of their own in a folder or one below it; or, with no markdown written,
    the same document read in one folder and a message that went through (a room of participants only)
  - what is no room: one agent, a file all of them write, a place everybody reads (the top of a repository, its `docs`), a folder that is a debate or holds debates, the output of a launch command,
    most of them changing the work outside the folder, agents not launched together (one after the other)

Hand-built facts over a temporary repository, judged by `units.assign` (and, for what the board shows, by `debates.judge`); the generator's `room` bundle (tools/scenarios) and the contract cases
C28 and C39-C47 cover the same rules over the axis values.

    python3 -m unittest tests.test_rooms
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, launch, plan, rd, tag, wr  # noqa: E402
from compat import server  # noqa: E402

from board import debates, units as U  # noqa: E402

GUIDE = '# Weekly sync\n\nTopics:\n\n- schedule\n- budget\n'


class Room(Fixture):
    """A repository (`.git` at its top) with the room folder `docs/meeting` and its guide `brief.md`."""

    def setUp(self):
        super().setUp()
        self.top = self.p('repo')
        os.makedirs(os.path.join(self.top, '.git'))
        self.folder = self.p('repo', 'docs', 'meeting')
        self.guide = self.file('repo/docs/meeting/brief.md', GUIDE, 50.0)

    def doc(self, rel, text='x\n', mtime=200.0):
        """A file below the room folder, on disk."""
        return self.file('repo/docs/meeting/' + rel, text, mtime)

    def member(self, aid, rel=None, group='m1', at=200.0, **kw):
        """An agent launched with the others of `group` that wrote the file `rel` of its own (default `<aid>.md`) below the room folder with a Write tool at `at`."""
        path = self.doc(rel or aid + '.md', mtime=at)
        kw.setdefault('status', 'running')
        return agent(aid, writes=[wr(path, at)], launch=launch(group, 't' + aid), **kw)

    def pair(self, one='A.md', two='B.md', below=False, **kw):
        base = 'out/' if below else ''
        return self.member('A', base + one, **kw), self.member('B', base + two, at=201.0, **kw)

    def tagged(self, aid, seat=None, own=True, **kw):
        """An agent with the room tag of the folder (and its seat) that wrote its own file `<seat or aid>.md` when `own`."""
        kw.setdefault('status', 'running')
        writes = [wr(self.doc((seat or aid) + '.md'), 200.0)] if own else []
        return agent(aid, writes=writes, tag=tag(self.folder, seat), **kw)

    def shown(self, *agents, **kw):
        """What the board shows (debates.judge) of the facts of the agents."""
        sf = self.sf(*agents, **kw)
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, cwd=self.top)
        with mock.patch.object(debates, 'session_facts', lambda s_, statuses, fresh=False: sf):
            return debates.judge(s, {a.id: a.status for a in agents})

    @staticmethod
    def topics(jd):
        return [t for d in jd.debates for t in d['topics']]

    def assertNoRoom(self, jd):
        self.assertEqual(jd.rooms, {})
        self.assertNotIn('room', jd.listed.values())
        return jd

    def states(self, jd):
        return {c.stem: c.state for c in jd.cells.values() if c.unit == self.folder}


class Cells(Room):
    def test_two_participants_with_a_file_each_beside_the_guide_are_a_room(self):
        a, b = self.pair()
        jd = self.assign(a, b)
        room = jd.rooms[self.folder]
        self.assertEqual((room.kind, room.sure, room.why, room.members, room.guide), ('cells', False, 'launch', ['A', 'B'], self.guide))
        self.assertEqual(room.files, {'A': self.p('repo/docs/meeting/A.md'), 'B': self.p('repo/docs/meeting/B.md')})
        self.assertEqual({k: (c.owner, c.agent, c.evidence, c.state) for k, c in jd.cells.items()},
                         {(self.folder, '-', 'A'): ('A', 'A', 'tool', 'draft'), (self.folder, '-', 'B'): ('B', 'B', 'tool', 'draft')})
        self.assertEqual(jd.agent_units, {'A': {self.folder}, 'B': {self.folder}})
        self.assertEqual((jd.listed, jd.roots), ({self.folder: 'room'}, {self.folder: [self.folder]}))

    def test_the_board_gets_the_room_as_a_topic(self):
        a, b = self.pair()
        j = self.shown(a, b)
        (tp,) = self.topics(j)
        self.assertEqual((tp['dir'], tp['title'], tp['room'], tp['kind'], tp['rounds']), (self.folder, 'Weekly sync', 'cells', 'rounds', [1]))
        self.assertEqual((tp['guide'], tp['room_sure'], tp['room_why']), (self.guide, False, 'launch'))
        self.assertEqual([(r['p'], r['agents'], [(c['round'], c['state'], c['path'], c['owner']) for c in r['cells']]) for r in tp['rows']],
                         [('A', ['A'], [(1, 'draft', self.p('repo/docs/meeting/A.md'), 'A')]), ('B', ['B'], [(1, 'draft', self.p('repo/docs/meeting/B.md'), 'B')])])
        self.assertEqual({k: sorted(v) for k, v in j.agent_units.items()}, {'A': [self.folder], 'B': [self.folder]})

    def test_a_room_needs_no_guide(self):
        os.unlink(self.guide)
        a, b = self.pair()
        jd = self.assign(a, b)
        self.assertEqual((jd.rooms[self.folder].kind, jd.rooms[self.folder].guide), ('cells', None))
        (tp,) = self.topics(self.shown(a, b))
        self.assertEqual((tp['room'], tp['guide'], tp['title']), ('cells', None, 'meeting'))

    def test_files_one_folder_below_the_guide_and_a_mixed_room(self):
        a, b = self.pair(below=True)
        jd = self.assign(a, b)
        (room,) = jd.rooms.values()
        self.assertEqual((room.folder, room.members), (self.p('repo/docs/meeting/out'), ['A', 'B']))      # the folder that holds the files themselves
        c = self.member('C', 'notes_C.md', at=202.0)
        self.assertEqual(self.assign(a, b, c).rooms, {})                                                  # files in two folders make no room (the orchestrator's answer O1)

    def test_the_file_is_what_the_participant_wrote(self):
        a = agent('A', writes=[wr(self.doc('notes.md'), 150.0)], launch=launch('m1', 't1'), status='done')
        b = agent('B', writes=[wr(self.doc('y.md'), 150.0)], launch=launch('m1', 't2'), status='done')
        jd = self.assign(a, b)
        self.assertEqual(self.states(jd), {'notes': 'done', 'y': 'done'})
        self.assertEqual({r['p']: r['cells'][0]['state'] for r in self.topics(self.shown(a, b))[0]['rows']}, {'notes': 'done', 'y': 'done'})

    def test_the_seat_is_the_tag_then_the_file(self):
        a = agent('A', writes=[wr(self.doc('B.md'), 150.0)], tag=tag(self.folder, 'B'), status='running')
        b = agent('B', tag=tag(self.folder, 'C'), status='running', start=101.0)                      # nothing written yet: its seat names the file
        c = agent('C', writes=[wr(self.doc('notes_D.md'), 150.0)], tag=tag(self.folder), status='running', start=102.0)        # no seat: the file it wrote
        jd = self.assign(a, b, c)
        room = jd.rooms[self.folder]
        self.assertEqual((room.sure, room.why, room.members), (True, 'tag', ['A', 'B', 'C']))
        self.assertEqual({k[2]: (c.owner, c.agent, c.evidence, c.state) for k, c in jd.cells.items()},
                         {'B': ('A', 'A', 'tool', 'draft'), 'C': (None, 'B', 'tag', 'writing'), 'notes_D': ('C', 'C', 'tool', 'draft')})

    def test_a_room_of_a_tag_shows_the_file_of_even_one_member(self):
        jd = self.assign(self.tagged('A'), self.tagged('B', own=False))
        room = jd.rooms[self.folder]
        self.assertEqual((room.kind, room.sure, sorted(room.files)), ('cells', True, ['A']))
        self.assertEqual(self.states(jd), {'A': 'draft'})
        self.assertNoRoom(self.assign(self.member('A'), agent('B', launch=launch('m1', 'tB'))))          # the launch alone makes a room only for the files of two of them

    def test_a_room_that_is_working_has_a_draft_and_a_finished_one_has_a_done_cell(self):
        for status, want in (('running', {'A': 'draft', 'B': 'writing'}), ('done', {'A': 'done', 'B': 'missing'})):
            a, b = self.tagged('A', 'A', status=status), self.tagged('B', 'B', own=False, status=status)       # B holds its seat and has written nothing
            jd = self.assign(a, b)
            self.assertEqual(self.states(jd), want, status)
            self.assertEqual({r['p']: r['cells'][0]['state'] for r in self.topics(self.shown(a, b))[0]['rows']}, want, status)

    def test_no_participant_file_and_no_guide_is_taken_for_the_conclusion(self):
        a, b = self.tagged('A', status='done'), self.tagged('B', status='done')
        jd = self.assign(a, b)
        f = jd.finals[self.folder]
        self.assertEqual((f.confirmed, f.path, f.candidates, f.why), (False, None, [], ['none']))        # the files of the participants and the guide are no conclusion
        minutes = self.doc('minutes.md', 'we decided\n', 300.0)
        jd = self.assign(a, b, orch=[wr(minutes, 300.0)])
        f = jd.finals[self.folder]
        self.assertEqual((f.confirmed, f.path, f.by, f.why), (True, minutes, 'orch', []))                # a document of its own written after them is
        (tp,) = self.topics(self.shown(a, b, orch=[wr(minutes, 300.0)]))
        self.assertEqual((tp['final']['exists'], tp['final']['rel']), (True, 'minutes.md'))

    def test_only_a_brief_a_readme_or_an_index_is_the_guide(self):
        os.unlink(self.guide)
        for name in ('brief.md', 'README.md', 'index.md'):
            guide = self.doc(name, '# Title of %s\n' % name, 50.0)
            a, b = self.pair()
            jd = self.assign(a, b)
            self.assertEqual(jd.rooms[self.folder].guide, guide, name)
            (tp,) = self.topics(self.shown(a, b))
            self.assertEqual((tp['title'], tp['room'], tp['guide']), ('Title of %s' % name, 'cells', guide), name)
            os.unlink(guide)
        for name in ('agenda.md', 'plan.md', 'whatever-this-is.md'):                                      # a name settles nothing: no guide, and no title
            other = self.doc(name, '# Title of %s\n' % name, 50.0)
            a, b = self.pair()
            self.assertIsNone(self.assign(a, b).rooms[self.folder].guide, name)
            (tp,) = self.topics(self.shown(a, b))
            self.assertEqual((tp['title'], tp['guide']), ('meeting', None), name)
            os.unlink(other)

    def test_an_agent_that_was_not_launched_with_them_is_not_one_of_them(self):
        a, b = self.pair()
        for launched in (launch('m2', 't9'), None):                                                    # another call, or none that is known
            c = agent('C', writes=[wr(self.doc('C.md'), 202.0)], launch=launched, status='running')
            jd = self.assign(a, b, c)
            self.assertEqual(jd.rooms[self.folder].members, ['A', 'B'])
            self.assertEqual(sorted(k[2] for k in jd.cells), ['A', 'B'])
            (tp,) = self.topics(self.shown(a, b, c))
            self.assertEqual([r['p'] for r in tp['rows']], ['A', 'B'])

    def test_two_files_of_one_name_keep_two_seats_by_the_folder_they_are_in(self):
        a = agent('A', writes=[wr(self.doc('left/report.md'), 200.0)], tag=tag(self.folder), status='running')
        b = agent('B', writes=[wr(self.doc('right/report.md'), 201.0)], tag=tag(self.folder), status='running')
        jd = self.assign(a, b)
        self.assertEqual({k[2]: c.owner for k, c in jd.cells.items()}, {'left/report': 'A', 'right/report': 'B'})
        (tp,) = self.topics(self.shown(a, b))
        self.assertEqual({r['p']: (r['agents'], os.path.relpath(r['cells'][0]['path'], self.folder)) for r in tp['rows']},
                         {'left/report': (['A'], 'left/report.md'), 'right/report': (['B'], 'right/report.md')})

    def test_a_room_inside_a_folder_with_a_brief_is_listed_once(self):
        self.file('repo/docs/brief.md', '# The whole review\n', 50.0)
        a, b = self.pair()
        jd = self.assign(a, b)
        self.assertEqual(list(jd.rooms), [self.folder])
        self.assertEqual([(d['root'], [t['dir'] for t in d['topics']]) for d in self.shown(a, b).debates], [(self.folder, [self.folder])])

    def test_an_estimated_room_is_never_the_current_debate_and_a_tag_makes_a_sure_one(self):
        a, b = self.pair()
        jd = self.assign(a, b)
        self.assertEqual((jd.order, jd.current), ([self.folder], None))
        self.assertEqual([d['current'] for d in self.shown(a, b).debates], [False])
        jd = self.assign(self.tagged('A'), self.tagged('B'))
        self.assertEqual((jd.rooms[self.folder].sure, jd.current), (True, self.folder))
        self.assertEqual([(d['current'], d['sure']) for d in self.shown(self.tagged('A'), self.tagged('B')).debates], [(True, True)])


class Closed(Room):
    """The final of a room (J15): the one document written in the room's folder after all the files, by a sure write, that nothing competes with. Only a room that a tag made is closed: a room that
    the launch makes is an estimate, and an estimate confirms nothing."""

    def done(self, how='tag', status='done'):
        if how == 'tag':
            return self.tagged('A', status=status), self.tagged('B', status=status)
        return self.pair(status=status)

    def final(self, agents, *orch, jd=None):
        jd = self.assign(*agents, orch=list(orch))
        return jd.finals[self.folder], jd

    def closing(self, name='CLOSING.md', ts=300.0, folder=None, mtime=None):
        path = self.file(os.path.join(folder or 'repo/docs/meeting', name), 'Everything is in; the folder is closed.\n', ts if mtime is None else mtime)
        return wr(path, ts), path

    def test_a_room_with_every_file_in_and_a_closing_document_after_it_is_closed(self):
        ev, path = self.closing()
        f, jd = self.final(self.done(), ev)
        self.assertEqual((f.confirmed, f.path, f.by, f.why, f.candidates, jd.closable), (True, path, 'orch', [], [path], {self.folder: True}))
        self.assertEqual(sorted(self.states(jd).values()), ['done', 'done'])
        (tp,) = self.topics(self.shown(*self.done(), orch=[ev]))
        self.assertEqual((tp['final']['exists'], tp['final']['rel'], tp['final']['by'], tp['closable']), (True, 'CLOSING.md', 'orch', True))

    def test_a_document_from_before_the_files_or_with_no_document_closes_nothing(self):
        ev, path = self.closing(ts=150.0)                                                              # the files were written at 200 and 201
        f, jd = self.final(self.done(), ev)
        self.assertEqual((f.confirmed, f.path, f.candidates, f.why, jd.closable), (False, None, [], ['none'], {self.folder: False}))
        f, jd = self.final(self.done())
        self.assertEqual((f.confirmed, f.why, f.candidates), (False, ['none'], []))

    def test_the_name_of_the_document_does_not_decide(self):
        for name in ('notes.md', 'CLOSING.md', 'ruling.md', 'x.md'):
            ev, path = self.closing(name)
            f, jd = self.final(self.done(), ev)
            self.assertEqual((f.confirmed, f.path), (True, path), name)                                  # the name only puts the candidates in order, and here there is one
            os.remove(path)                                                                              # (a document that is left behind with no record would compete with the next one)

    def test_a_document_written_before_a_later_change_of_the_files_is_withdrawn(self):
        ev, path = self.closing()
        a, b = self.done()
        late = wr(self.doc('A.md', mtime=400.0), 400.0, kind='update')                                   # a participant changed its file after the conclusion
        f, jd = self.final((agent('A', writes=[*a.writes, late], tag=a.tag, status='done'), b), ev)
        self.assertEqual((f.confirmed, f.why), (False, ['none']))

    def test_two_documents_after_the_files_are_no_final(self):
        ev1, p1 = self.closing('CLOSING.md', 300.0)
        ev2, p2 = self.closing('notes.md', 301.0)
        f, jd = self.final(self.done(), ev1, ev2)
        self.assertEqual((f.confirmed, f.why, sorted(f.candidates)), (False, ['several'], sorted([p1, p2])))

    def test_a_room_still_working_is_not_closed(self):
        ev, path = self.closing()
        f, jd = self.final(self.done(status='running'), ev)
        self.assertIn('live_participant', f.why)
        self.assertEqual((f.confirmed, jd.closable), (False, {self.folder: False}))

    def test_a_document_above_the_room_is_no_final_of_it(self):
        ev, path = self.closing('CLOSING.md', folder='repo/docs')                                       # in the folder around the room, as a bundle's conclusion stands
        f, jd = self.final(self.done(), ev)
        self.assertEqual((f.confirmed, f.candidates, f.why), (False, [], ['none']))

    def test_a_room_that_the_launch_made_is_never_closed(self):
        ev, path = self.closing()
        f, jd = self.final(self.done('launch'), ev)
        self.assertEqual((f.confirmed, f.why, f.candidates, jd.closable), (False, ['estimated_room'], [path], {self.folder: False}))


class NotARoom(Room):
    def test_a_document_one_of_the_agents_wrote_is_a_result_not_a_guide(self):
        report = self.doc('phase.md', '# Phase report\n', 150.0)
        first = agent('first', writes=[wr(report, 150.0)], launch=launch('m0', 't0'), status='done', last=160.0)          # launched on its own, before them
        a, b = self.pair()
        jd = self.assign(first, a, b)
        self.assertEqual(jd.rooms[self.folder].members, ['A', 'B'])                                      # it is no one of them, and its report is no file of theirs
        self.assertNotIn('phase', {k[2] for k in jd.cells})
        self.assertNoRoom(self.assign(first, a))                                                         # one launched with it, with a file of its own: no room of two
        c = agent('C', planned=[plan(report)], launch=launch('m1', 'tC'), status='running')               # told to write it, not yet there
        jd = self.assign(a, b, c)
        self.assertEqual((jd.rooms[self.folder].members, sorted(k[2] for k in jd.cells)), (['A', 'B'], ['A', 'B']))
        self.assertNoRoom(self.assign(a, c))                                                             # what is only asked for is no file of its own

    def test_one_agent(self):
        self.assertNoRoom(self.assign(self.member('A')))
        self.assertNoRoom(self.assign(self.tagged('A')))                                                 # one tag is no room
        other = self.p('repo', 'docs', 'other')
        os.makedirs(other)
        self.assertNoRoom(self.assign(self.tagged('A'), agent('B', writes=[wr(self.file('repo/docs/other/B.md'), 200.0)], tag=tag(other))))      # two tags on two folders

    def test_the_documents_of_a_repository_everybody_reads(self):
        for folder in (self.top, self.p('repo', 'docs')):
            rel = os.path.relpath(folder, self.root)
            a = agent('A', writes=[wr(self.file(rel + '/A.md', mtime=200.0), 200.0)], launch=launch('m1', 't1'))
            b = agent('B', writes=[wr(self.file(rel + '/B.md', mtime=201.0), 201.0)], launch=launch('m1', 't2'))
            self.assertNoRoom(self.assign(a, b))                                                         # even with the files beside it
            guide = self.file(rel + '/README.md', '# Doc\n', 50.0)
            a = agent('A', reads=[rd(guide)], launch=launch('m1', 't1'), sent_to=('B',))
            b = agent('B', reads=[rd(guide)], launch=launch('m1', 't2'))
            self.assertNoRoom(self.assign(a, b))                                                         # nor with a talk about what they read
        self.file('repo/notes/README.md', '# Doc\n', 50.0)                                              # (the same, in a folder of the repository that nobody reads: a room)
        a = agent('A', writes=[wr(self.file('repo/notes/A.md', mtime=200.0), 200.0)], launch=launch('m1', 't1'))
        b = agent('B', writes=[wr(self.file('repo/notes/B.md', mtime=201.0), 201.0)], launch=launch('m1', 't2'))
        self.assertEqual(list(self.assign(a, b).rooms), [self.p('repo', 'notes')])

    def test_one_file_that_all_of_them_write(self):
        mins = self.doc('minutes.md')
        self.assertNoRoom(self.assign(*(agent(i, planned=[plan(mins)], launch=launch('m1', 't' + i)) for i in 'AB')))                # told to write it, not yet there
        both = [agent(i, writes=[wr(mins, 150.0 + k)], launch=launch('m1', 't' + i), status='done') for k, i in enumerate('AB')]
        self.assertNoRoom(self.assign(*both))
        own = [agent(a.id, writes=[*a.writes, wr(self.doc('%s.md' % a.id), 160.0)], launch=a.launch, status='done') for a in both]
        jd = self.assign(*own)                                                                           # with a file of its own each, they are a room, and minutes.md is no seat of it
        self.assertEqual((jd.rooms[self.folder].members, sorted(k[2] for k in jd.cells)), (['A', 'B'], ['A', 'B']))

    def test_files_outside_the_room(self):
        code = [agent(i, writes=[wr(self.file('repo/svc_%s/src/part.py' % i), 190.0)], launch=launch('m1', 't' + i), status='done') for i in 'ab']
        self.assertNoRoom(self.assign(*code))                                                            # only markdown is a file of a room
        far = [agent(i, writes=[wr(self.doc('%s/x/%s.md' % (d, i)), 190.0)], launch=launch('m1', 't' + i)) for i, d in (('A', 'out'), ('B', 'other'))]
        self.assertNoRoom(self.assign(*far))                                                             # two folders below, in two places: no folder holds both
        mine = [agent(i, writes=[wr(self.doc('%s.md' % i, mtime=190.0), 190.0, evidence='planned', proof='sha', span=(100.0, 300.0), text='x\n')], launch=launch('m1', 't' + i), status='done')
                for i in 'AB']
        self.assertNoRoom(self.assign(*mine))                                                            # what a launch command saved is no file of its own

    def test_a_guide_of_each_ones_own_that_is_missing_or_that_a_later_message_names(self):
        a = agent('A', reads=[rd(self.file('repo/docs/meeting/agenda_A.md', '# A\n', 50.0))], launch=launch('m1', 't1'), sent_to=('B',))
        b = agent('B', reads=[rd(self.file('repo/docs/meeting/agenda_B.md', '# B\n', 50.0))], launch=launch('m1', 't2'), sent_to=('A',))
        self.assertNoRoom(self.assign(a, b))                                                             # not the same document
        a = agent('A', reads=[rd(self.guide)], launch=launch('m1', 't1'), sent_to=('B',))
        b = agent('B', launch=launch('m1', 't2'), sent_to=('A',))
        self.assertNoRoom(self.assign(a, b))                                                             # one of them read none

    def test_readers_alone_are_no_room(self):
        a = agent('A', reads=[rd(self.guide)], launch=launch('m1', 't1'))
        b = agent('B', reads=[rd(self.guide)], launch=launch('m1', 't2'))
        self.assertNoRoom(self.assign(a, b))                                                             # they read the same document and nobody said anything to anybody
        a = agent('A', reads=[rd(self.guide)], launch=launch('m1', 't1'), sent_to=('B',))
        self.assertEqual(self.assign(a, b).rooms[self.folder].kind, 'members')                           # ... and with a message that went through they are the room of participants

    def test_a_folder_that_is_a_debate_already_keeps_its_rules(self):
        self.dirs('repo/docs/meeting/r1')
        a, b = self.pair()
        jd = self.assign(a, b)
        self.assertNoRoom(jd)                                                                            # `A.md` beside the guide is no report of a round
        self.assertEqual((jd.listed, jd.cells), ({}, {}))
        a = self.member('A', 'r1/A.md')
        b = self.member('B', 'r1/B.md', at=201.0)
        jd = self.assertNoRoom(self.assign(a, b))                                                        # reports in a round folder are cells of a debate
        self.assertEqual((jd.listed, sorted(jd.cells)), ({self.folder: 'cell'}, [(self.folder, 'r1', 'A'), (self.folder, 'r1', 'B')]))
        (tp,) = self.topics(self.shown(a, b))
        self.assertEqual((tp.get('room'), [(r['p'], c['round']) for r in tp['rows'] for c in r['cells']]), (None, [('A', 1), ('B', 1)]))
        jd = self.assertNoRoom(self.assign(agent('A', tag=tag(self.folder)), agent('B', tag=tag(self.folder))))      # the room tag of a folder with a round is for the debate
        self.assertEqual(jd.listed, {self.folder: 'tag'})

    def test_a_write_that_is_not_a_sure_whole_file_is_no_file_of_its_own(self):
        a = self.member('A')
        b_file = self.doc('B.md', mtime=100.0)                                                           # on disk from before
        sure = wr(b_file, 201.0)
        self.assertEqual(list(self.assign(a, agent('B', writes=[sure], launch=launch('m1', 'tB'))).rooms), [self.folder])                      # (the control: a Write that worked)
        for name, ev in (('failed', wr(b_file, 201.0, ok=False)), ('result unknown', wr(b_file, 201.0, ok=None)),
                         ('edit of a file that was there', wr(b_file, 201.0, kind='update')), ('append', wr(b_file, 201.0, kind='append')),
                         ('shell, save not seen in its window', wr(b_file, 201.0, evidence='shell', proof='window', span=(150.0, 152.0))),     # the file is from time 100
                         ('shell, that did not decide its exit status and failed', wr(b_file, 201.0, evidence='shell', ok=None, proof='window', span=(200.0, 202.0)))):
            b = agent('B', writes=[ev], launch=launch('m1', 'tB'))
            self.assertNoRoom(self.assign(a, b))
        saved = self.doc('B.md', mtime=151.0)
        b = agent('B', writes=[wr(saved, 201.0, evidence='shell', proof='window', span=(150.0, 152.0))], launch=launch('m1', 'tB'))
        self.assertEqual(list(self.assign(a, b).rooms), [self.folder])                                   # a masked place counts when the file was saved in the time of the command

    def test_a_flat_review_that_declares_its_result_files_keeps_its_rules(self):
        """A brief that says who writes which result file is only text: it makes no flat debate (the kind is gone), and no room of two agents that nobody launched together (C45 limit)."""
        self.file('repo/docs/review/brief.md', '# Review\n\nReviewers and their result files: `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n', 50.0)
        sol, opus = self.file('repo/docs/review/sol.md', mtime=200.0), self.file('repo/docs/review/opus.md', mtime=210.0)
        jd = self.assign(agent('sol', writes=[wr(sol, 200.0)], launch=None, status='done'), agent('opus', writes=[wr(opus, 210.0)], launch=None, status='done'))
        self.assertEqual((jd.rooms, jd.listed, jd.cells), ({}, {}, {}))                                  # nothing is listed
        jd = self.assign(agent('sol', writes=[wr(sol, 200.0)], launch=launch('m1', 't1'), status='done'), agent('opus', writes=[wr(opus, 210.0)], launch=launch('m1', 't2'), status='done'))
        (room,) = jd.rooms.values()                                                                      # launched together they are a room, an estimate: never the current debate
        self.assertEqual((room.folder, room.sure, room.members, jd.current), (self.p('repo/docs/review'), False, ['opus', 'sol'], None))

    def test_a_folder_that_holds_debates_is_no_room(self):
        self.dirs('repo/runs/x/r1')
        pair = [agent(i, writes=[wr(self.file('repo/runs/%s.md' % i, mtime=200.0), 200.0)], launch=launch('m1', 't' + i)) for i in 'PQ']
        self.assertNoRoom(self.assign(*pair))                                                            # a bundle's folder is the folder of its topics
        os.rmdir(self.p('repo/runs/x/r1'))
        self.assertEqual(list(self.assign(*pair).rooms), [self.p('repo/runs')])                           # ... and the same two, with no debate below, are a room

    def test_messages_alone_are_not_a_meeting_when_the_orchestrator_sends_them_or_a_participant_has_a_file(self):
        reader = lambda i, **kw: agent(i, reads=[rd(self.guide)], launch=launch('m1', 't' + i), **kw)       # noqa: E731
        self.assertNoRoom(self.assign(reader('A'), reader('B')))                                          # nobody told anybody anything (what the orchestrator says is no message between them)
        self.assertNoRoom(self.assign(reader('A', sent_to=('someone-else',)), reader('B')))               # a message to nobody of theirs
        elsewhere = self.file('repo/work/reports/B.md', 'x\n', 200.0)
        a, b = reader('A', sent_to=('B',)), reader('B', writes=[wr(elsewhere, 200.0)])
        self.assertNoRoom(self.assign(a, b))                                                             # one of them wrote a file of its own: the meeting is not by message only
        c = reader('B', writes=[wr(self.p('scratch.txt'), 200.0)])
        self.assertEqual(self.assign(a, c).rooms[self.folder].kind, 'members')                           # (a file that is not markdown is no file of its own)


class Delivered(Room):
    """A message that failed to go (its tool result was an error) is no talk: nothing was said to the other participant."""

    def meeting(self, *oks):
        """Two agents of one launch that read the guide, and the messages they sent (`oks`: True, False, None, 'absent' for a record with no result, 'none' for no message)."""
        a, b = (server.Agent('a%016d' % i, {'description': d}) for i, d in ((1, 'B Budget'), (2, 'C Risk')))
        for x in (a, b):
            x.spawn_ts = x.first_ts = 100.0
            x.last_ts = 200.0
            x.cwd = self.top
            x.launch = launch('m1', 't' + x.id)
            x.ev.add_read(rd(self.guide))
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'my view', **({} if oks[0] == 'absent' else {'ok': oks[0]})}]
        b.sent = [] if oks[1] == 'none' else [{'ts': 161.0, 'to': a.id, 'summary': '', 'text': 'mine', **({} if oks[1] == 'absent' else {'ok': oks[1]})}]
        return a, b

    def judge(self, a, b):
        s = types.SimpleNamespace(agents={a.id: a, b.id: b}, _file_cache={}, _head_cache={}, cwd=self.top)
        return debates.judge(s, {a.id: 'running', b.id: 'running'})

    def test_a_failed_message_is_no_meeting_and_the_others_are(self):
        for oks in ((False, 'none'), (False, False)):
            jd = self.judge(*self.meeting(*oks))
            self.assertEqual([t for t in self.topics(jd) if t.get('room')], [], oks)
        for oks in ((True, 'none'), ('absent', 'none'), (None, 'none'), (False, True), (False, 'absent')):
            (tp,) = self.topics(self.judge(*self.meeting(*oks)))
            self.assertEqual(tp['room'], 'members', oks)

    def test_what_the_judgment_is_given_is_the_messages_that_went_through(self):
        a, b = self.meeting(False, True)
        s = types.SimpleNamespace(agents={a.id: a, b.id: b}, _file_cache={}, _head_cache={}, cwd=self.top)
        sf = debates.session_facts(s, {})
        self.assertEqual({f.id: f.sent_to for f in sf.agents}, {a.id: (), b.id: (a.id,)})


class Together(Room):
    """A room is where the agents that were launched together work in the room's folder: agents launched one after the other, or changing code elsewhere, are not in a meeting. There is no rule
    of time: the records of when they ran say nothing of whether they were launched together (CONTRACT J14: no 60 seconds, no `_present_together`)."""

    def code(self, name):
        return self.p('repo', 'src', name, 'part.py')

    def reporter(self, name, code=None, group='m1', report=True, **kw):
        """An agent of `group` that wrote its report one folder below the guide; `code` ('tool', 'shell', 'planned') is how it also wrote the code of its part elsewhere in the repository."""
        writes = []
        if code:
            ev = dict(tool={}, shell=dict(evidence='shell', proof='exit'), planned=dict(evidence='planned', proof='sha', span=(100.0, 300.0), text='x\n'))
            writes.append(wr(self.file('repo/src/%s/part.py' % name, mtime=190.0), 190.0, **ev[code]))
        if report:
            writes.append(wr(self.doc('impl/%s.md' % name), 200.0))
        kw.setdefault('status', 'done')
        return agent(name, writes=writes, launch=launch(group, 't' + name), **kw)

    def test_agents_that_only_write_their_reports_below_the_guide_are_a_room(self):
        jd = self.assign(*(self.reporter(n) for n in 'xyz'))
        (room,) = jd.rooms.values()
        self.assertEqual((room.folder, room.members, sorted(k[2] for k in jd.cells)), (self.p('repo/docs/meeting/impl'), ['x', 'y', 'z'], ['x', 'y', 'z']))

    def test_agents_that_change_code_elsewhere_and_report_beside_the_plan_are_no_room(self):
        self.assertNoRoom(self.assign(*(self.reporter(n, 'tool') for n in 'xyz')))                       # wrote the code with a tool, and the report
        self.assertNoRoom(self.assign(*(self.reporter(n, 'shell') for n in 'xyz')))                      # ... with a shell command that decides its own exit status
        self.assertNoRoom(self.assign(*(agent(n, planned=[plan(self.doc('impl/%s.md' % n))], launch=launch('m1', 't' + n)) for n in 'xyz')))        # asked to report: nothing is written yet
        self.assertEqual(len(self.assign(*(self.reporter(n, 'planned') for n in 'xyz')).rooms), 1)       # the output of a launch command is no change of the work

    def test_the_majority_decides_one_that_edits_elsewhere_does_not_end_the_room(self):
        jd = self.assign(self.reporter('x'), self.reporter('y'), self.reporter('z', 'tool'))
        self.assertEqual(sorted(r.members for r in jd.rooms.values()), [['x', 'y', 'z']])                # one of three
        self.assertNoRoom(self.assign(self.reporter('x'), self.reporter('y', 'tool'), self.reporter('z', 'tool')))        # two of three
        jd = self.assign(self.reporter('x'), self.reporter('y', 'tool'))                                 # one of two is half, not more than half
        self.assertEqual(sorted(r.members for r in jd.rooms.values()), [['x', 'y']])

    def test_agents_that_run_one_after_the_other_are_no_room(self):
        a = self.member('A', start=100.0, last=150.0, status='done')
        later = lambda **kw: self.member('B', group='m2', at=301.0, start=300.0, last=360.0, **kw)         # noqa: E731  (launched by another call, later)
        self.assertNoRoom(self.assign(a, later(status='done')))
        self.assertNoRoom(self.assign(a, later(status='running')))                                        # still working: no more time for the first
        self.assertNoRoom(self.assign(a, self.member('B', group='m2', at=151.0, start=149.0, last=360.0, status='running')))      # even when it starts before the first one is over
        b = self.member('B', group='m1', at=301.0, start=300.0, last=360.0, status='done')                # the same two, launched together (and one after the other: it is no matter)
        self.assertEqual(list(self.assign(a, b).rooms), [self.folder])
        unknown = lambda x: agent(x.id, writes=x.writes, launch=None, status='done')                       # noqa: E731  (a launch that is not known is no group, and two of them are not one)
        self.assertNoRoom(self.assign(a, unknown(b)))
        self.assertNoRoom(self.assign(unknown(a), unknown(b)))

    def test_a_running_agent_is_there_until_now_and_one_that_cannot_be_seen_is_there_until_its_last_record(self):
        a = self.member('A', start=100.0, last=150.0)
        b = self.member('B', at=301.0, start=300.0, last=360.0)                                          # started after the first one's last record: launched together, so in the room
        for sa, sb in (('running', 'running'), ('unknown', 'running'), ('done', 'done'), ('done', 'unknown'), ('stalled', 'interrupted')):
            jd = self.assign(agent('A', writes=a.writes, launch=a.launch, status=sa, start=100.0, last=150.0), agent('B', writes=b.writes, launch=b.launch, status=sb, start=300.0, last=360.0))
            self.assertEqual(len(jd.rooms), 1, (sa, sb))                                                 # whatever the state, whenever they ran: nothing in the room is told by time

    def test_an_agent_whose_time_the_records_do_not_give_is_in_no_room(self):
        a, b = self.pair()
        unplaced = agent('B', writes=b.writes, launch=None, status='done')                               # what cannot be told is its launch: it is in no room
        self.assertNoRoom(self.assign(a, unplaced))
        for kw in (dict(start=0.0), dict(last=0.0)):                                                      # no start, no end: the time is no input of a room
            blank = agent('A', writes=a.writes, launch=a.launch, status='done', **kw)
            self.assertEqual(self.assign(blank, b).rooms[self.folder].members, ['A', 'B'], kw)

    def test_the_most_that_were_there_at_one_time_are_the_room(self):
        a, b = self.pair(start=100.0, last=200.0, status='done')
        late = self.member('C', 'C.md', group='m2', at=901.0, start=900.0, last=950.0, status='done')   # launched by a call of its own an hour later
        self.assertEqual(self.assign(a, b, late).rooms[self.folder].members, ['A', 'B'])
        same = self.member('C', 'C.md', group='m1', at=901.0, start=900.0, last=950.0, status='done')    # launched with them, however late it came
        self.assertEqual(self.assign(a, b, same).rooms[self.folder].members, ['A', 'B', 'C'])


class Scratch(Room):
    """Only a file of the work that an agent changes outside the guide's folder is a change of the work: what a launch command saved (a redirect, a log), a shell command that cannot be told to have
    worked, and the files an agent keeps in a folder for scratch work outside the repository, are not. A room of reviewers stays one when most of them do that."""

    def scratch(self, *parts):
        return self.p('scratch', *parts)

    def reviewers(self, names='ABC', extra=None, tops=False, cwd=''):
        """Each is launched with the others, and writes its notes beside the guide (the Write tool); `extra(name)` is the other writes of it."""
        out = []
        for name in names:
            writes = [wr(self.doc(name + '.md'), 200.0)] + (extra(name) if extra else [])
            out.append(agent(name, writes=writes, launch=launch('m1', 't' + name), status='running', cwd=cwd))
        return out

    def rooms(self, agents, **kw):
        return list(self.assign(*agents, **kw).rooms.values())

    def test_what_a_launch_command_saves_is_no_change_of_the_work(self):
        def log(name):
            out = []
            for ext, proof in (('txt', 'window'), ('out', 'sha')):
                path = self.file('repo/logs/%s.%s' % (name, ext), 'x\n', 150.0)
                out.append(wr(path, 160.0, evidence='planned', proof=proof, span=(100.0, 160.0), text='x\n'))
            return out
        for names in ('AB', 'ABC'):
            (room,) = self.rooms(self.reviewers(names, log))
            self.assertEqual(room.members, list(names))

    def test_files_a_shell_command_wrote_are_no_change_of_the_work_either(self):
        def shell(name):            # a shell command whose result the record does not give (`ok` None, a place where a later command can hide a failure)
            return [wr(self.file('repo/docs/other/%s.md' % name, mtime=151.0), 151.0, evidence='shell', ok=None, proof='window', span=(150.0, 152.0))]
        (room,) = self.rooms(self.reviewers('ABC', shell))
        self.assertEqual(room.members, ['A', 'B', 'C'])

    def test_a_shell_write_that_is_sure_is_a_change_of_the_work(self):
        def shell(name):
            return [wr(self.file('repo/src/%s.py' % name, mtime=151.0), 151.0, evidence='shell', proof='exit')]
        self.assertEqual(self.rooms(self.reviewers('ABC', shell)), [])
        self.assertEqual(len(self.rooms(self.reviewers('ABC', lambda n: shell(n) if n == 'A' else []))), 1)

    def test_a_file_in_a_folder_for_scratch_work_outside_the_repository_is_no_change_of_the_work(self):
        def scratch(name):
            return [wr(self.file('scratch/%s' % rel, mtime=252.0), 252.0) for rel in ('repro_%s.py' % name, 'draft_%s.md' % name)]       # after the notes: the first file of its own is what stands for it
        for names in ('AB', 'ABC'):
            (room,) = self.rooms(self.reviewers(names, scratch))
            self.assertEqual((room.folder, room.members), (self.folder, list(names)))
        agents = self.reviewers('ABC', scratch)
        for a in agents:
            a.status = 'done'
        self.assertEqual(len(self.rooms(agents)), 1)

    def test_a_copy_the_instruction_tells_to_keep_in_a_scratch_folder_is_no_change_of_the_work(self):
        agents = []
        for name in 'AB':
            copy_ = self.scratch(name + '.md')
            agents.append(agent(name, writes=[wr(self.doc(name + '.md'), 200.0)], planned=[plan(copy_)], launch=launch('m1', 't' + name), status='running'))      # asked to keep a copy there
        (room,) = self.rooms(agents)
        self.assertEqual(room.members, ['A', 'B'])
        agents = [agent(a.id, writes=[*a.writes, wr(self.file('scratch/%s.md' % a.id, mtime=205.0), 205.0)], planned=a.planned, launch=a.launch, status='running') for a in agents]      # and kept it
        (room,) = self.rooms(agents)
        self.assertEqual(room.members, ['A', 'B'])

    def test_a_file_of_the_work_in_the_repository_still_is(self):
        def work(name):
            return [wr(self.file('repo/src/%s/part.py' % name, mtime=152.0), 152.0)]
        self.assertEqual(self.rooms(self.reviewers('AB', work)), [])
        self.assertEqual(len(self.rooms(self.reviewers('ABC', lambda n: work(n) if n == 'A' else []))), 1)       # one of three is not most of them

    def test_a_file_in_the_checkout_the_agent_works_in_is_the_work_whichever_repository_holds_the_guide(self):
        other = self.p('checkout')
        os.makedirs(os.path.join(other, '.git'))
        def code(name):
            return [wr(self.file('checkout/src/%s.py' % name, mtime=152.0), 152.0)]
        tops = lambda cwd: (U.repo_top(cwd),) if cwd else ()                                              # noqa: E731
        self.assertEqual(self.rooms(self.reviewers('AB', code, cwd=other), tops_of=tops), [])
        self.assertEqual(len(self.rooms(self.reviewers('AB', code), tops_of=tops)), 1)                    # an agent that does not work in that checkout has changed nothing of its work

    def test_when_the_guide_is_in_no_repository_the_files_below_it_are_still_the_work_whatever_repository_the_agent_started_in(self):
        agents = []
        for name in 'ABC':
            own = self.file('plain/plan/%s.md' % name, mtime=200.0)
            probe = self.file('plain/plan/verify/probes/%s.py' % name, mtime=190.0)
            agents.append(agent(name, writes=[wr(own, 200.0), wr(probe, 190.0)], launch=launch('m1', 't' + name), status='running', cwd=self.top))      # it was started in a repository elsewhere
        tops = lambda cwd: (U.repo_top(cwd),) if cwd else ()                                              # noqa: E731
        with mock.patch.object(U, 'SCRATCH_DIRS', (self.scratch() + os.sep,)):
            self.assertEqual(self.rooms(agents, tops_of=tops), [])                                        # probes beside a plan: parallel work

    def test_without_a_repository_a_scratch_folder_is_still_left_out(self):
        def agents(extra):
            out = []
            for name in 'AB':
                own = self.file('plain/plan/%s.md' % name, mtime=200.0)
                out.append(agent(name, writes=[wr(own, 200.0)] + extra(name), launch=launch('m1', 't' + name), status='running', cwd=self.p('plain')))
            return out
        with mock.patch.object(U, 'SCRATCH_DIRS', (self.scratch() + os.sep,)):
            self.assertEqual(len(self.rooms(agents(lambda n: [wr(self.file('scratch/repro_%s.py' % n, mtime=152.0), 152.0)]))), 1)
            self.assertEqual(self.rooms(agents(lambda n: [wr(self.file('elsewhere/src/%s.py' % n, mtime=153.0), 153.0)])), [])        # a file of the project beside the folder is one


class Members(Room):
    """A room of participants only (D6): agents launched together that wrote no markdown, read the same document of one folder and talked to each other (a message that went through). Nothing is
    written, so the room has no cell and no round, and nobody holds a seat."""

    def talk(self):
        a, b = (server.Agent('a%016d' % i, {'description': d}) for i, d in ((1, 'B Budget'), (2, 'C Risk')))
        for x in (a, b):
            x.spawn_ts = x.first_ts = 100.0
            x.last_ts = 200.0
            x.cwd = self.top
            x.launch = launch('m1', 't' + x.id)
            x.ev.add_read(rd(self.guide))
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'my view'}]
        b.sent = [{'ts': 161.0, 'to': a.id, 'summary': '', 'text': 'mine'}]
        return a, b

    def judge(self, a, b):
        s = types.SimpleNamespace(agents={a.id: a, b.id: b}, _file_cache={}, _head_cache={}, cwd=self.top)
        return debates.judge(s, {a.id: 'running', b.id: 'running'})

    def test_a_meeting_by_message_is_a_room_of_participants_only(self):
        a, b = self.talk()
        jd = self.judge(a, b)
        (tp,) = self.topics(jd)
        self.assertEqual((tp['dir'], tp['title'], tp['room'], tp['rounds'], tp['guide'], tp['room_sure']), (self.folder, 'Weekly sync', 'members', [], self.guide, False))
        self.assertEqual([(r['agents'], r['cells']) for r in tp['rows']], [([a.id], []), ([b.id], [])])
        self.assertEqual(jd.assignments, [])                                  # nobody holds a seat
        self.assertEqual(list(jd.cells), [])
        self.assertFalse(tp['final']['confirmed'])

    def test_one_message_one_way_is_enough_and_only_the_id_of_the_other_names_it(self):
        a, b = self.talk()
        b.sent = []
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'x'}]                 # by its id
        (tp,) = self.topics(self.judge(a, b))
        self.assertEqual(tp['room'], 'members')
        for to in ('C Risk', 'c', 'opus5.5'):                                             # by its description, its role letter or its model name: nobody that is known (O14)
            a.sent = [{'ts': 160.0, 'to': to, 'summary': '', 'text': 'x'}]
            self.assertEqual([t for t in self.topics(self.judge(a, b)) if t.get('room')], [], to)
        a.sent = [{'ts': 160.0, 'to': a.id, 'summary': '', 'text': 'x'}]                 # to itself: to nobody
        self.assertEqual([t for t in self.topics(self.judge(a, b)) if t.get('room')], [])

    def test_the_same_document_and_two_launches_are_needed(self):
        a, b = self.talk()
        b.launch = launch('m2', 'tb')                                                      # launched apart
        self.assertEqual([t for t in self.topics(self.judge(a, b)) if t.get('room')], [])
        b.launch = launch('m1', 'tb')
        b.ev.reads.clear()
        b.ev.add_read(rd(self.file('repo/docs/meeting/other.md', '# Other\n', 50.0)))       # another document
        self.assertEqual([t for t in self.topics(self.judge(a, b)) if t.get('room')], [])

    def test_the_judgment_is_made_again_when_a_message_is_sent(self):
        a, b = self.talk()
        a.sent = b.sent = []
        s = types.SimpleNamespace(agents={a.id: a, b.id: b}, _file_cache={}, _head_cache={}, cwd=self.top)
        before = server.Session._debate_key(s, {a.id: 'running', b.id: 'running'})
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'x'}]
        after = server.Session._debate_key(s, {a.id: 'running', b.id: 'running'})
        self.assertNotEqual(before, after)


class Viewer(Room):
    """The document view opens what the room on the screen holds (views.allowed_file reads the debates the page shows: the room's folder is its root)."""

    def session(self, shown):
        s = server.Session.__new__(server.Session)
        s.lock = __import__('threading').RLock()
        s.agents = {}
        s.debates = lambda statuses: (shown.debates, {})
        return s

    def test_the_guide_and_the_documents_of_a_room_open_in_the_document_view_and_nothing_else(self):
        a, b = self.pair()
        self.file('repo/docs/other.md', 'x\n', 200.0)
        s = self.session(self.shown(a, b))
        self.assertEqual(s.allowed_file(self.guide), os.path.realpath(self.guide))
        self.assertEqual(s.allowed_file(self.p('repo/docs/meeting/A.md')), os.path.realpath(self.p('repo/docs/meeting/A.md')))
        self.assertIsNone(s.allowed_file(self.p('repo/docs/other.md')))                                # beside the room's folder, not in it


class ViewerOfTheRoomOnScreen(Room):
    """The document view opens what the room on the screen holds: the room is the one the page shows, judged with the statuses the page was built with."""

    def agent_of(self, aid, group, start, last, own=None, status='running'):
        """A real agent of a session (its events as the collector keeps them), launched by the call of `group`."""
        a = server.Agent('a%016d' % aid, {})
        a.spawn_ts = a.first_ts = start
        a.last_ts = last
        a.cwd = self.top
        a.launch = launch(group, 't%d' % aid)
        if own:
            a.ev.add_write(wr(own, start + 5.0, agent=a.id))
        return a

    def session(self, agents, statuses):
        s = server.Session.__new__(server.Session)
        s.lock = __import__('threading').RLock()
        s.agents = {a.id: a for a in agents}
        s._file_cache, s._head_cache, s.cwd = {}, {}, self.top
        s._verdicts = {aid: types.SimpleNamespace(status=st) for aid, st in statuses.items()}
        s.refresh_facts = lambda statuses: None                        # the facts (launch keys, tags) are given here, not made again from records: that is the collection's (S1) test
        return s

    def test_a_room_of_two_that_work_in_turns_while_both_are_running_opens_its_guide(self):
        a = self.agent_of(1, 'm1', 100.0, 110.0, self.doc('A.md'))
        b = self.agent_of(2, 'm1', 200.0, 210.0, self.doc('B.md'))
        for st in ('running', 'done'):                                                                  # no rule of time: the room is there when they are over too
            s = self.session([a, b], {a.id: st, b.id: st})
            shown = [t for d in s.debates({a.id: st, b.id: st})[0] for t in d['topics']]
            self.assertEqual([t.get('room') for t in shown], ['cells'], st)
            self.assertEqual(s.allowed_file(self.guide), os.path.realpath(self.guide), st)
            self.assertEqual(s.allowed_file(self.p('repo/docs/meeting/A.md')), os.path.realpath(self.p('repo/docs/meeting/A.md')), st)
            self.assertIsNone(s.allowed_file(self.p('repo/docs/other.md')), st)
        b = self.agent_of(2, 'm2', 200.0, 210.0, self.doc('B.md'))                                      # launched by another call: they never were together, and nothing opens
        s = self.session([a, b], {a.id: 'running', b.id: 'running'})
        self.assertIsNone(s.allowed_file(self.guide))

    def test_the_view_does_not_make_up_a_state_for_the_page(self):
        a = self.agent_of(1, 'm1', 100.0, 110.0, self.doc('A.md'))
        b = self.agent_of(2, 'm1', 200.0, 210.0, self.doc('B.md'))
        s = self.session([a, b], {a.id: 'running', b.id: 'running'})
        seen = []
        real = server.Session.debates
        s.debates = lambda statuses: seen.append(dict(statuses)) or real(s, statuses)
        s.allowed_file(self.guide)
        self.assertEqual(seen, [{a.id: 'running', b.id: 'running'}])


if __name__ == '__main__':
    unittest.main()
