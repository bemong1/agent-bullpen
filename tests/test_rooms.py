"""Rooms: agents of one orchestrator that work together in a folder (a meeting, an agenda, a plan ...) are shown like a debate, found by the structure of the records
and the disk alone: a guide that two or more of them are pointed at by their first instruction, and a file of its own each (or messages only). Hand-built agents over a
temporary repository; the generator's `room` bundle (tools/scenarios) covers the same rules over the axis values.

    python3 -m unittest tests.test_rooms
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402

from board import debates, units as U  # noqa: E402

GUIDE = '# Weekly sync\n\nTopics:\n\n- schedule\n- budget\n'


def write(path, text='x\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class Repo(unittest.TestCase):
    """A repository (`.git` at its top) with the room folder `docs/meeting` and its guide `agenda.md`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.top = os.path.join(os.path.realpath(self.tmp.name), 'repo')
        os.makedirs(os.path.join(self.top, '.git'))
        self.folder = os.path.join(self.top, 'docs', 'meeting')
        self.guide = write(os.path.join(self.folder, 'agenda.md'), GUIDE)
        self.n = 0

    def agent(self, text, desc='', writes=(), sent=(), start=None, last=None):
        self.n += 1
        a = server.Agent('a%016d' % self.n, {'description': desc} if desc else {})
        a.origin, a.spawn_ts, a.first_ts, a.last_ts, a.cwd = 'subagent', start or 100.0 + self.n, start or 100.0 + self.n, last or 200.0, self.top
        a.spawn_prompt = text
        for p in writes:
            a.writes.append({'ts': 150.0, 'path': p, 'ok': True})
        a.sent = [{'ts': 160.0, 'to': to, 'summary': '', 'text': 'x'} for to in sent]
        return a

    def judge(self, agents, status='running'):
        """`status` is one status for all of them, or {agent: status}."""
        s = types.SimpleNamespace(agents={a.id: a for a in agents}, _file_cache={}, _head_cache={}, cwd=self.top)
        return debates.judge(s, status if isinstance(status, dict) else {a.id: status for a in agents})

    def assertNoRoom(self, agents, status='running'):
        jd = self.judge(agents, status)
        self.assertEqual([t for t in self.topics(jd) if t.get('room')], [])
        self.assertEqual([x for x in jd.diag if x['code'] == 'debate_in_misc'], [])
        return jd

    def topics(self, jd):
        return [t for d in jd.debates for t in d['topics']]

    def ask(self, text, name, guide=None, own=None):
        """A first instruction: read the guide, write `name` (an absolute path) as the own file."""
        return 'Read `%s` and follow it. %s' % (guide or self.guide, ('Write your notes to `%s`.' % own) if own else '')

    def pair(self, one='A.md', two='B.md', below=False, **kw):
        base = os.path.join(self.folder, 'out') if below else self.folder
        a = self.agent(self.ask('', 'a', own=os.path.join(base, one)), **kw)
        b = self.agent(self.ask('', 'b', own=os.path.join(base, two)), **kw)
        return a, b


class Cells(Repo):
    def test_two_participants_with_a_file_each_beside_the_guide_are_a_room(self):
        a, b = self.pair()
        jd = self.judge([a, b])
        (tp,) = self.topics(jd)
        self.assertEqual((tp['dir'], tp['title'], tp['room'], tp['kind'], tp['rounds']), (self.folder, 'Weekly sync', 'cells', 'rounds', [1]))
        self.assertEqual(tp['guide'], self.guide)
        self.assertEqual([(r['p'], r['agents'], [(c['round'], c['state'], c['path']) for c in r['cells']]) for r in tp['rows']],
                         [('A', [a.id], [(1, 'writing', os.path.join(self.folder, 'A.md'))]), ('B', [b.id], [(1, 'writing', os.path.join(self.folder, 'B.md'))])])
        self.assertEqual({k: sorted(v) for k, v in jd.agent_units.items()}, {a.id: [self.folder], b.id: [self.folder]})

    def test_files_one_folder_below_the_guide_and_a_mixed_room(self):
        a, b = self.pair(below=True)
        c = self.agent(self.ask('', 'c', own=os.path.join(self.folder, 'notes_C.md')))
        (tp,) = self.topics(self.judge([a, b, c]))
        self.assertEqual({r['p']: os.path.relpath(r['cells'][0]['path'], self.folder) for r in tp['rows']}, {'A': 'out/A.md', 'B': 'out/B.md', 'notes_C': 'notes_C.md'})

    def test_the_file_is_what_the_participant_wrote_when_the_instruction_names_none(self):
        a = self.agent('Read `%s`. Save your notes in a file.' % self.guide, writes=[os.path.join(self.folder, 'x', 'notes.md')])
        b = self.agent('Read `%s`. Save your notes in a file.' % self.guide, writes=[os.path.join(self.folder, 'y.md')])
        write(os.path.join(self.folder, 'x', 'notes.md'))
        write(os.path.join(self.folder, 'y.md'))
        (tp,) = self.topics(self.judge([a, b], 'done'))
        self.assertEqual({r['p']: r['cells'][0]['state'] for r in tp['rows']}, {'notes': 'done', 'y': 'done'})

    def test_the_seat_is_the_marker_then_the_tag_then_the_file(self):
        a = self.agent('You are participant B (Budget). ' + self.ask('', 'a', own=os.path.join(self.folder, 'notes_B.md')))
        b = self.agent(self.ask('', 'b', own=os.path.join(self.folder, 'notes_C.md')), desc='C Risk notes')
        c = self.agent(self.ask('', 'c', own=os.path.join(self.folder, 'notes_D.md')))
        (tp,) = self.topics(self.judge([a, b, c]))
        self.assertEqual({r['p']: os.path.basename(r['cells'][0]['path']) for r in tp['rows']}, {'B': 'notes_B.md', 'C': 'notes_C.md', 'notes_D': 'notes_D.md'})

    def test_a_room_that_is_working_has_a_draft_and_a_finished_one_has_a_done_cell(self):
        a, b = self.pair()
        write(os.path.join(self.folder, 'A.md'), 'one\ntwo\n')
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual({r['p']: r['cells'][0]['state'] for r in tp['rows']}, {'A': 'draft', 'B': 'writing'})
        (tp,) = self.topics(self.judge([a, b], 'done'))
        self.assertEqual({r['p']: r['cells'][0]['state'] for r in tp['rows']}, {'A': 'done', 'B': 'missing'})

    def test_no_participant_file_and_no_guide_is_taken_for_the_conclusion(self):
        a, b = self.pair()
        write(os.path.join(self.folder, 'A.md'), 'a\n')
        write(os.path.join(self.folder, 'B.md'), 'b\n')
        (tp,) = self.topics(self.judge([a, b], 'done'))
        self.assertEqual((tp['final']['exists'], tp['final']['rel']), (False, None))
        write(os.path.join(self.folder, 'minutes.md'), 'we decided\n')
        os.utime(os.path.join(self.folder, 'minutes.md'), (9e9, 9e9))
        (tp,) = self.topics(self.judge([a, b], 'done'))
        self.assertEqual(tp['final']['rel'], 'minutes.md')                      # a document of its own is

    def test_a_guide_of_any_name_in_the_folder_makes_the_title(self):
        os.unlink(self.guide)
        for name in ('brief.md', 'README.md', 'plan.md', 'whatever-this-is.md'):
            guide = write(os.path.join(self.folder, name), '# Title of %s\n' % name)
            a = self.agent(self.ask('', 'a', guide=guide, own=os.path.join(self.folder, 'A.md')))
            b = self.agent(self.ask('', 'b', guide=guide, own=os.path.join(self.folder, 'B.md')))
            (tp,) = self.topics(self.judge([a, b]))
            self.assertEqual((tp['title'], tp['room']), ('Title of %s' % name, 'cells'), name)
            os.unlink(guide)

    def test_a_participant_that_points_at_no_guide_is_not_one_of_them(self):
        a, b = self.pair()
        c = self.agent('Write your notes to `%s`.' % os.path.join(self.folder, 'C.md'))
        (tp,) = self.topics(self.judge([a, b, c]))
        self.assertEqual([r['p'] for r in tp['rows']], ['A', 'B'])


    def test_two_files_of_one_name_keep_two_seats_by_the_folder_they_are_in(self):
        a = self.agent(self.ask('', 'a', own=os.path.join(self.folder, 'left', 'report.md')))
        b = self.agent(self.ask('', 'b', own=os.path.join(self.folder, 'right', 'report.md')))
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual({r['p']: (r['agents'], os.path.relpath(r['cells'][0]['path'], self.folder)) for r in tp['rows']},
                         {'left/report': ([a.id], 'left/report.md'), 'right/report': ([b.id], 'right/report.md')})

    def test_a_room_inside_a_folder_with_a_brief_is_one_of_its_topics(self):
        write(os.path.join(self.top, 'docs', 'brief.md'), '# The whole review\n')
        a, b = self.pair()
        jd = self.judge([a, b])
        self.assertEqual([(d['root'], [t['dir'] for t in d['topics']]) for d in jd.debates], [(os.path.join(self.top, 'docs'), [self.folder])])


class Closed(Repo):
    """A room that is a topic of a bundle (the folder above it has a brief.md): the bundle's conclusion document closes it."""

    def setUp(self):
        super().setUp()
        self.bundle = os.path.join(self.top, 'work', 'bundle')
        write(os.path.join(self.bundle, 'brief.md'), '# The whole review\n\nEvery pass is a folder below.\n')
        self.folder = os.path.join(self.bundle, 'step2')
        self.guide = write(os.path.join(self.folder, 'change_plan.md'), '# Edit pass\n\nEach edits a part.\n')

    def done(self, name='CLOSING.md', mtime=9e9, text='Everything is in; the step2/ folder is closed.\n'):
        a, b = self.pair(one='edits/A.md', two='edits/B.md', start=100.0, last=200.0)
        for p in ('A', 'B'):
            write(os.path.join(self.folder, 'edits', p + '.md'), '# %s\n\nedited\n' % p)
        path = write(os.path.join(self.bundle, name), text) if name else None
        if path:
            os.utime(path, (mtime, mtime))
        return a, b

    def topic(self, agents, status='done'):
        (d,) = self.judge(agents, status).debates
        self.assertEqual(d['root'], self.bundle)
        (tp,) = d['topics']
        return tp

    def test_a_room_with_every_file_in_and_a_closing_document_after_it_is_closed(self):
        tp = self.topic(self.done())
        self.assertEqual((tp['room'], [c['state'] for r in tp['rows'] for c in r['cells']]), ('cells', ['done', 'done']))
        self.assertEqual((tp['final']['exists'], tp['final']['auto'], tp['final']['rel'], tp['final']['path']),
                         (True, True, '../CLOSING.md', os.path.join(self.bundle, 'CLOSING.md')))

    def test_a_document_from_before_the_files_or_with_no_conclusion_name_closes_nothing(self):
        self.assertFalse(self.topic(self.done(mtime=1.0))['final']['exists'])
        self.assertFalse(self.topic(self.done(name='notes.md'))['final']['exists'])
        self.assertFalse(self.topic(self.done(name=None))['final']['exists'])

    def test_a_room_still_working_is_not_closed(self):
        a, b = self.done()
        self.assertFalse(self.topic([a, b], 'running')['final']['exists'])

    def test_the_conclusion_a_table_names_is_the_final_whatever_stands_above(self):
        self.done()
        write(os.path.join(self.bundle, 'brief.md'), '# The whole review\n\n| 주제 | 폴더 | 선행 | 최종 산출물 |\n|---|---|---|---|\n| Edit | `step2/` | — | `final/edit.md` |\n')
        tp = self.topic(self.done())
        self.assertEqual((tp['final']['rel'], tp['final']['exists'], tp['final']['auto']), ('final/edit.md', False, False))


class NotARoom(Repo):
    def test_a_document_one_of_the_agents_wrote_is_a_result_not_a_guide(self):
        report = write(os.path.join(self.folder, 'phase.md'), '# Phase report\n')
        first = self.agent('Write your report to `%s`.' % report, writes=[report])
        a = self.agent('Read `%s`. Write your notes to `%s`.' % (report, os.path.join(self.folder, 'A.md')))
        b = self.agent('Read `%s`. Write your notes to `%s`.' % (report, os.path.join(self.folder, 'B.md')))
        self.assertNoRoom([first, a, b])
        self.assertNoRoom([a, b, self.agent('Write your report to `%s`.' % report)])             # told to write it, not yet there

    def test_one_agent(self):
        self.assertNoRoom([self.agent(self.ask('', 'a', own=os.path.join(self.folder, 'A.md')))])

    def test_the_documents_of_a_repository_everybody_reads(self):
        for name in ('README.md', 'CLAUDE.md', 'AGENTS.md', 'docs/guide.md'):
            guide = write(os.path.join(self.top, name), '# Doc\n')
            top, docs = os.path.dirname(guide), os.path.dirname(guide)
            a = self.agent('Read `%s`. Write your notes to `%s`.' % (guide, os.path.join(docs, 'A.md')))
            b = self.agent('Read `%s`. Write your notes to `%s`.' % (guide, os.path.join(docs, 'B.md')))
            self.assertNoRoom([a, b])                                       # even with the files beside it
            self.assertNoRoom([self.agent('Read `%s`.' % guide, sent=['x']), self.agent('Read `%s`.' % guide)])

    def test_one_file_that_all_of_them_write(self):
        mins = os.path.join(self.folder, 'minutes.md')
        self.assertNoRoom([self.agent(self.ask('', 'a', own=mins)), self.agent(self.ask('', 'b', own=mins))])
        write(mins)
        self.assertNoRoom([self.agent(self.ask('', 'a'), writes=[mins]), self.agent(self.ask('', 'b'), writes=[mins])], 'done')

    def test_files_outside_the_room(self):
        for sub in ('out/x/%s.md', '../elsewhere/%s.md'):
            a = self.agent(self.ask('', 'a', own=os.path.normpath(os.path.join(self.folder, sub % 'A'))))
            b = self.agent(self.ask('', 'b', own=os.path.normpath(os.path.join(self.folder, sub % 'B'))))
            self.assertNoRoom([a, b])
        code = [self.agent('Read `%s`. Implement your part in `%s`.' % (self.guide, os.path.join(self.top, 'svc_%s' % x, 'src', 'part.py')),
                           writes=[write(os.path.join(self.top, 'svc_%s' % x, 'src', 'part.py'))]) for x in 'ab']
        self.assertNoRoom(code, 'done')

    def test_a_guide_of_each_ones_own_that_is_missing_or_that_a_later_message_names(self):
        own = [self.agent('Read `%s`. Write your notes to `%s`.' % (write(os.path.join(self.folder, 'agenda_%s.md' % x)), os.path.join(self.folder, '%s.md' % x))) for x in 'AB']
        self.assertNoRoom(own)
        gone = os.path.join(self.folder, 'gone.md')
        self.assertNoRoom([self.agent('Read `%s`. Write your notes to `%s`.' % (gone, os.path.join(self.folder, '%s.md' % x))) for x in 'AB'])
        late = []
        for x in 'AB':
            a = self.agent('Write your notes to `%s`.' % os.path.join(self.folder, '%s.md' % x))
            a.received.append({'ts': 120.0, 'text': 'Read `%s` and follow it.' % self.guide})
            late.append(a)
        self.assertNoRoom(late)

    def test_do_not_forget_to_read_the_guide_is_still_a_guide(self):
        a, b = self.pair()
        a.spawn_prompt = 'Do not forget to read `%s`. Write your notes to `%s`.' % (self.guide, os.path.join(self.folder, 'A.md'))
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual(tp['room'], 'cells')

    def test_a_guide_that_is_only_quoted_or_negated_or_to_be_written(self):
        for text in ('Example of a first line: "Read `%s`." It is only an example.', 'Do not read `%s`. Do not write a file.', 'Write the agenda to `%s`.'):
            a = self.agent(text % self.guide, sent=['a%016d' % 2])
            b = self.agent(text % self.guide, sent=['a%016d' % 1])
            self.n = 0
            self.assertNoRoom([a, b])

    def test_a_folder_that_is_a_debate_already_keeps_its_rules(self):
        os.makedirs(os.path.join(self.folder, 'r1'))
        a, b = self.pair()
        self.assertNoRoom([a, b])                                            # `A.md` beside the guide is no report of a round, and the folder is not listed: as before
        os.unlink(self.guide)
        guide = write(os.path.join(self.folder, 'brief.md'), GUIDE)
        a = self.agent(self.ask('', 'a', guide=guide, own=os.path.join(self.folder, 'r1', 'A.md')))
        b = self.agent(self.ask('', 'b', guide=guide, own=os.path.join(self.folder, 'r1', 'B.md')))
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual((tp.get('room'), [(r['p'], c['round']) for r in tp['rows'] for c in r['cells']]), (None, [('A', 1), ('B', 1)]))

    def test_a_flat_review_that_declares_its_result_files_keeps_its_rules(self):
        flat = os.path.join(self.top, 'docs', 'review')
        guide = write(os.path.join(flat, 'brief.md'), '# Review\n\nReviewers and their result files: `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n')
        a = self.agent('Read `%s`. Write your findings to `%s`.' % (guide, os.path.join(flat, 'sol.md')))
        b = self.agent('Read `%s`. Write your findings to `%s`.' % (guide, os.path.join(flat, 'opus.md')))
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual((tp['kind'], tp.get('room'), sorted(r['p'] for r in tp['rows'])), ('flat', None, ['opus', 'sol']))

    def test_messages_alone_are_not_a_meeting_when_the_orchestrator_sends_them_or_a_participant_has_a_file(self):
        a, b = self.agent('Read `%s`. Answer in your reply only.' % self.guide), self.agent('Read `%s`. Answer in your reply only.' % self.guide)
        a.orch_msgs.append({'ts': 130.0, 'summary': '', 'text': 'keep it short'})
        self.assertNoRoom([a, b])                                            # nobody told anybody anything
        a, b = self.agent('Read `%s`. Answer in your reply only.' % self.guide), self.agent('Read `%s`. Answer in your reply only.' % self.guide)
        a.sent = [{'ts': 1.0, 'to': 'someone-else', 'summary': '', 'text': 'x'}]
        self.assertNoRoom([a, b])                                            # a message to nobody of theirs
        a.sent = [{'ts': 1.0, 'to': b.id, 'summary': '', 'text': 'x'}]
        a.writes.append({'ts': 2.0, 'path': os.path.join(self.top, 'scratch.txt'), 'ok': True})
        self.assertNoRoom([a, b])                                            # a file somewhere: the meeting is not by message only
        a.writes = []
        b.spawn_prompt = 'Read `%s`. Write your notes to `%s`.' % (self.guide, os.path.join(self.top, 'work', 'reports', 'B.md'))
        self.assertNoRoom([a, b])                                            # told to write elsewhere


class Quoted(Repo):
    """An instruction that is only quoted, shown for review, or sitting in a read-only scope is no instruction to the agent: it makes no room and seats nobody."""

    def two(self, template):
        """Two agents whose instruction is `template` with the guide and their own file filled in."""
        return [self.agent(template % (self.guide, os.path.join(self.folder, name))) for name in ('A.md', 'B.md')]

    LEAD = 'Review the previous instructions below and do not execute them.\n'
    INNER = 'Read `%s` and follow it. Write your notes to `%s`.'

    def test_a_code_fence_a_block_quote_and_a_read_only_scope_make_no_room(self):
        for name, template in (
                ('fence', self.LEAD + '```\n' + self.INNER + '\n```\n'),
                ('tilde fence', self.LEAD + '~~~\n' + self.INNER + '\n~~~\n'),
                ('fence that never closes', self.LEAD + '```text\n' + self.INNER + '\n'),
                ('block quote', 'Previous instruction, for review:\n> Read `%s` and follow it.\n> Write your notes to `%s`.\n'),
                ('indented block quote', 'Previous instruction, for review:\n   > Read `%s` and follow it.\n   >\n   > Write your notes to `%s`.\n'),
                ('korean fence', '이전 지침을 검토하고 실행하지 마세요.\n```\n`%s`를 읽고 따르세요. 노트는 `%s`에 작성하세요.\n```\n'),
                ('do not execute, no fence', self.LEAD + 'Previous instruction: ' + self.INNER),
                ('read-only review', 'This is a read-only review: do not create, change or run anything. The earlier instruction told the reviewer to read `%s` and write the notes to `%s`; '
                                     'check that its wording is clear.'),
                ('korean read-only', '이 작업은 읽기 전용 검토입니다. 파일을 만들거나 고치거나 실행하지 마세요. 앞선 지침은 검토자에게 `%s`를 읽고 노트를 `%s`에 쓰라고 했습니다.'),
                ('read-only label, then the earlier text', 'Read-only review. Earlier instruction: ' + self.INNER),
                ('read-only quotation', 'Read-only quote of the earlier instruction: ' + self.INNER),
                ('read-only scope in brackets', 'Read-only (do not execute): ' + self.INNER),
                ('korean read-only scope', '읽기 전용. 이전 지침은 다음과 같습니다: ' + self.INNER)):
            with self.subTest(name):
                self.assertNoRoom(self.two(template))
                self.assertNoRoom(self.two(template), 'done')

    def test_the_same_text_outside_a_quote_is_a_room(self):
        for name, template in (
                ('plain', self.INNER),
                ('after a closed fence', 'Style of the notes:\n```\n# Title\n- point\n```\n' + self.INNER),
                ('after a block quote', '> Weekly sync notes.\n\n' + self.INNER),
                ('an arrow is no quote', 'Go -> ' + self.INNER),
                ('a fence on one line is code in a line', 'Use ```x``` as the style. ' + self.INNER),
                ('do not execute something else', 'Do not execute the tests. ' + self.INNER),
                ('do not execute what is only reviewed', 'Review the plan and do not execute it; ' + self.INNER),
                ('a read-only review of the code that still writes notes', 'Read-only review of the code: do not change any code. ' + self.INNER),
                ('do not write other files', 'Do not write any file other than your own. ' + self.INNER)):
            with self.subTest(name):
                (tp,) = self.topics(self.judge(self.two(template)))
                self.assertEqual(tp['room'], 'cells')

    def test_a_participant_whose_real_instruction_follows_the_quote_is_one_of_them(self):
        first = self.two(self.LEAD + '```\n' + self.INNER + '\n```\n')
        a, b = self.pair()
        self.assertNoRoom(first + [a])                                          # one real participant is no room
        (tp,) = self.topics(self.judge(first + [a, b]))
        self.assertEqual([r['p'] for r in tp['rows']], ['A', 'B'])
        self.assertEqual(sorted(x for r in tp['rows'] for x in r['agents']), sorted([a.id, b.id]))

    def test_a_quoted_guide_does_not_seat_a_marker_either(self):
        for text in ('```\n[ROOM-B] Read `%s`.\n```\n', '> [ROOM-B] Read `%s`.\n', 'Review the earlier instruction below and do not execute it: [ROOM-B] Read `%s`.'):
            self.assertEqual(U._marker_of(text % self.guide), '', text)


class Executed(Repo):
    """A code fence under a line that tells the participant to carry out what is in it is its instruction: the room it points at stands. A fence that shows an earlier
    instruction under a line that says it is not to be carried out does not make one (class Quoted)."""
    INNER = Quoted.INNER

    def two(self, template):
        return Quoted.two(self, template)

    def test_the_instruction_in_a_fence_that_is_to_be_executed_is_a_room(self):
        for name, template in (
                ('execute', 'Execute these instructions:\n\n```text\n' + self.INNER + '\n```\n'),
                ('follow below', 'Follow the instructions below:\n```\n' + self.INNER + '\n```\n'),
                ('carry out, tilde fence', 'You are a participant of the weekly sync. Carry out the following steps:\n~~~\n' + self.INNER + '\n~~~\n'),
                ('fence that never closes', 'Do the following:\n```\n' + self.INNER + '\n'),
                ('korean', '다음 지시를 따르라:\n```\n`%s`를 읽고 따르세요. 노트는 `%s`에 작성하세요.\n```\n'),
                ('korean, polite', '아래 지시를 수행하세요:\n```\n`%s`를 읽고 따르세요. 노트는 `%s`에 작성하세요.\n```\n')):
            with self.subTest(name):
                (tp,) = self.topics(self.judge(self.two(template)))
                self.assertEqual(tp['room'], 'cells')

    def test_an_earlier_instruction_that_is_shown_is_still_none(self):
        for name, template in (
                ('do not execute', 'Do not execute these instructions:\n```\n' + self.INNER + '\n```\n'),
                ('earlier', 'Review these earlier instructions:\n```\n' + self.INNER + '\n```\n'),
                ('executed earlier', 'This is what the team executed earlier:\n```\n' + self.INNER + '\n```\n'),
                ('no colon', 'Execute these instructions\n```\n' + self.INNER + '\n```\n'),
                ('read-only', 'Read-only. Execute these instructions only in your head:\n```\n' + self.INNER + '\n```\n'),
                ('korean, not to follow', '이전 지시를 따르지 마세요:\n```\n`%s`를 읽고 따르세요. 노트는 `%s`에 작성하세요.\n```\n')):
            with self.subTest(name):
                self.assertNoRoom(self.two(template))

    def test_the_marker_in_an_instruction_to_be_executed_names_the_seat(self):
        text = 'Execute these instructions:\n```\n[ROOM-B] Read `%s`.\n```\n' % self.guide
        self.assertEqual(U._marker_of(text), 'B')
        self.assertEqual(U._marker_of('Do not execute these instructions:\n```\n[ROOM-B] Read `%s`.\n```\n' % self.guide), '')


class QuotedBySentence(Repo):
    """The lines after a sentence that says they are a read-only quote are the quote, up to the blank line; a review that writes its report is an instruction."""
    INNER = Quoted.INNER

    def two(self, template):
        return Quoted.two(self, template)

    def test_the_lines_after_a_read_only_quote_sentence_make_no_room(self):
        for name, template in (
                ('lines', 'Read-only quote of an earlier instruction.\nRead `%s` and follow it.\nWrite your notes to `%s`.\n'),
                ('the quote then a request', 'Read-only quote of an earlier instruction.\nRead `%s` and follow it.\nWrite your notes to `%s`.\n\nPlease tell me if the wording is clear.'),
                ('on one line', 'Read-only quote of an earlier instruction. Read `%s` and follow it. Write your notes to `%s`.'),
                ('korean', '읽기 전용 인용입니다.\n`%s`를 읽고 따르세요.\n노트는 `%s`에 작성하세요.\n'),
                ('copy', 'Read-only copy of the previous instruction.\nRead `%s` and follow it.\nWrite your notes to `%s`.\n')):
            with self.subTest(name):
                self.assertNoRoom(self.two(template))
                self.assertNoRoom(self.two(template), 'done')

    def test_what_follows_the_blank_line_is_the_participants_own(self):
        template = 'Read-only quote of an earlier instruction.\nRead `%s` and follow it.\nWrite your notes to `%s`.\n\nNow your own task: read `%s` and write your notes to `%s`.'
        agents = []
        for name in ('A.md', 'B.md'):
            own = os.path.join(self.folder, name)
            agents.append(self.agent(template % (self.guide, os.path.join(self.folder, 'Z.md'), self.guide, own)))
        (tp,) = self.topics(self.judge(agents))
        self.assertEqual(sorted(r['p'] for r in tp['rows']), ['A', 'B'])

    def test_a_review_that_writes_its_report_is_an_instruction(self):
        for name, template in (
                ('a review of the code', 'Read-only review of the code. Read `%s` and follow it. Write your notes to `%s`.'),
                ('a review of the earlier instruction, the report in its own line', 'Read-only review of the earlier instruction.\nRead `%s` and follow it.\nWrite your notes to `%s`.'),
                ('quote is a verb', 'Read-only: quote the lines you checked in your notes.\nRead `%s` and follow it.\nWrite your notes to `%s`.'),
                ('korean review', '읽기 전용 검토입니다.\n`%s`를 읽고 따르세요.\n노트는 `%s`에 작성하세요.\n')):
            with self.subTest(name):
                (tp,) = self.topics(self.judge(self.two(template)))
                self.assertEqual(tp['room'], 'cells')


class Delivered(Repo):
    """A message that failed to go (its tool result was an error) is no talk: nothing was said to the other participant."""

    def meeting(self, *oks):
        a = self.agent('Read `%s`. Settle the open items with the other participants by message; write no file.' % self.guide, desc='B Budget')
        b = self.agent('Read `%s`. Settle the open items with the other participants by message; write no file.' % self.guide, desc='C Risk')
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'my view', **({} if oks[0] == 'absent' else {'ok': oks[0]})}]
        b.sent = [] if oks[1] == 'none' else [{'ts': 161.0, 'to': a.id, 'summary': '', 'text': 'mine', **({} if oks[1] == 'absent' else {'ok': oks[1]})}]
        return a, b

    def test_a_failed_message_is_no_meeting_and_the_others_are(self):
        self.assertNoRoom(self.meeting(False, 'none'))
        self.assertNoRoom(self.meeting(False, False))
        for oks in ((True, 'none'), ('absent', 'none'), (None, 'none'), (False, True), (False, 'absent')):
            (tp,) = self.topics(self.judge(list(self.meeting(*oks))))
            self.assertEqual(tp['room'], 'members', oks)


class Together(Repo):
    """A room is where the participants work at the same time and in the room's folder: agents run one after the other, or editing code elsewhere, are not in a meeting."""

    def report(self, name):
        return os.path.join(self.folder, 'impl', name + '.md')

    def code(self, name):
        return os.path.join(self.top, 'src', name, 'part.py')

    def implementers(self, told=True, wrote=True, report=True, names='xyz', **kw):
        """One agent each: reads the guide, changes the code of its own part and writes its report (a file one folder below the guide)."""
        out = []
        for name in names:
            text = 'Read `%s` and follow it.' % self.guide
            if told:
                text += ' Implement your part in `%s`.' % self.code(name) + (' Write a short report to `%s`.' % self.report(name) if report else '')
            writes = ([self.code(name)] + ([self.report(name)] if report else [])) if wrote else []
            for p in writes:
                write(p)
            out.append(self.agent(text, writes=writes, **kw))
        return out

    def reporter(self, name, code=False):
        """An agent that reads the guide and writes its report one folder below it (and, with `code`, also the code of its part elsewhere)."""
        paths = ([self.code(name)] if code else []) + [self.report(name)]
        for p in paths:
            write(p)
        return self.agent('Read `%s` and follow it. Write a short report to `%s`.' % (self.guide, self.report(name)), writes=paths)

    def test_agents_that_only_write_their_reports_below_the_guide_are_a_room(self):
        (tp,) = self.topics(self.judge([self.reporter(n) for n in 'xyz'], 'done'))
        self.assertEqual((tp['room'], sorted(r['p'] for r in tp['rows'])), ('cells', ['x', 'y', 'z']))

    def test_agents_that_change_code_elsewhere_and_report_beside_the_plan_are_no_room(self):
        self.assertNoRoom(self.implementers(told=True, wrote=True), 'done')                 # told and wrote
        self.assertNoRoom(self.implementers(told=True, wrote=False), 'running')             # told: nothing is written yet
        self.assertNoRoom(self.implementers(told=False, wrote=True), 'done')                # the instruction names no path: what they wrote says it
        self.assertNoRoom(self.implementers(told=True, wrote=False, report=False), 'running')

    def test_the_majority_decides_one_that_edits_elsewhere_does_not_end_the_room(self):
        (tp,) = self.topics(self.judge([self.reporter('x'), self.reporter('y'), self.reporter('z', code=True)], 'done'))
        self.assertEqual(sorted(r['p'] for r in tp['rows']), ['x', 'y', 'z'])                # one of three
        self.assertNoRoom([self.reporter('x'), self.reporter('y', code=True), self.reporter('z', code=True)], 'done')        # two of three
        (tp,) = self.topics(self.judge([self.reporter('x'), self.reporter('y', code=True)], 'done'))                         # one of two is half, not more than half
        self.assertEqual(sorted(r['p'] for r in tp['rows']), ['x', 'y'])

    def test_agents_that_run_one_after_the_other_are_no_room(self):
        a, b = self.pair(start=100.0, last=150.0)
        b.spawn_ts = b.first_ts = 300.0
        b.last_ts = 360.0
        self.assertNoRoom([a, b], 'done')
        self.assertNoRoom([a, b], {a.id: 'done', b.id: 'running'})                           # the second one still working does not give the first more time
        b.spawn_ts = b.first_ts = 149.0                                                       # starts before the first one is over: they were there together
        (tp,) = self.topics(self.judge([a, b], 'done'))
        self.assertEqual(tp['room'], 'cells')
        b.spawn_ts = b.first_ts = 150.0                                                       # starts the moment the first ended: not together (the latest start < the earliest end)
        self.assertNoRoom([a, b], 'done')

    def test_a_running_agent_is_there_until_now_and_one_that_cannot_be_seen_is_there_until_its_last_record(self):
        a, b = self.pair(start=100.0, last=150.0)
        b.spawn_ts = b.first_ts = 300.0
        self.assertNoRoom([a, b], {a.id: 'unknown', b.id: 'running'})                        # `unknown`: its last record is when it was last known to be there
        a.last_ts = 500.0
        (tp,) = self.topics(self.judge([a, b], {a.id: 'running', b.id: 'running'}))
        self.assertEqual(tp['room'], 'cells')

    def test_an_agent_whose_time_the_records_do_not_give_is_in_no_room(self):
        a, b = self.pair()
        a.spawn_ts = a.first_ts = 0                                                          # no start
        self.assertNoRoom([a, b], 'done')
        a, b = self.pair()
        a.last_ts = 0                                                                        # no end, and not running: it cannot be placed
        self.assertNoRoom([a, b], 'done')
        (tp,) = self.topics(self.judge([a, b], 'running'))                                   # a running one is there until now whatever its last record says
        self.assertEqual(tp['room'], 'cells')

    def test_the_most_that_were_there_at_one_time_are_the_room(self):
        a, b = self.pair(start=100.0, last=200.0)
        late = self.agent(self.ask('', 'c', own=os.path.join(self.folder, 'C.md')), start=900.0, last=950.0)
        (tp,) = self.topics(self.judge([a, b, late], 'done'))
        self.assertEqual(sorted(r['p'] for r in tp['rows']), ['A', 'B'])                     # the one that came an hour later is in no room with them


class Scratch(Repo):
    """Only a file of the work that an agent changes outside the guide's folder is a change of the work: what a shell command saved (a redirect, a log), and the files an
    agent keeps in a folder for scratch work outside the repository, are not. A room of reviewers stays one when most of them do that."""

    def scratch(self, *parts):
        return os.path.join(os.path.dirname(self.top), 'scratch', *parts)

    def reviewers(self, names='ABC', extra=None):
        """Each reads the guide and writes its notes beside it (the Write tool); `extra(name, agent)` adds what else it does."""
        out = []
        for name in names:
            own = os.path.join(self.folder, name + '.md')
            write(own)
            a = self.agent('Read `%s` and follow it. Write your notes to `%s`.' % (self.guide, own), writes=[own])
            if extra:
                extra(name, a)
            out.append(a)
        return out

    def rooms(self, agents, status='running'):
        return [t for t in self.topics(self.judge(agents, status)) if t.get('room')]

    def test_what_a_launch_command_saves_is_no_change_of_the_work(self):
        def log(name, a):
            a.redirects = [{'fd': 1, 'op': '>', 'path_resolved': os.path.join(self.top, 'logs', name + '.txt')}]
            a.out_paths = [{'ts': 100.0, 'path': os.path.join(self.top, 'logs', name + '.out')}]
        for names in ('AB', 'ABC'):
            (tp,) = self.rooms(self.reviewers(names, log))
            self.assertEqual(sorted(r['p'] for r in tp['rows']), list(names))

    def test_files_a_shell_command_wrote_are_no_change_of_the_work_either(self):
        def shell(name, a):
            a.shell_writes = [{'ts': 151.0, 'paths': [os.path.join(self.top, 'docs', 'other', name + '.md')], 'id': 'x', 'ok': None}]
        for p in ('A', 'B', 'C'):
            write(os.path.join(self.top, 'docs', 'other', p + '.md'))
        (tp,) = self.rooms(self.reviewers('ABC', shell))
        self.assertEqual(sorted(r['p'] for r in tp['rows']), ['A', 'B', 'C'])

    def test_a_file_in_a_folder_for_scratch_work_outside_the_repository_is_no_change_of_the_work(self):
        def scratch(name, a):
            for p in (self.scratch('repro_%s.py' % name), self.scratch('draft_%s.md' % name)):
                write(p)
                a.writes.append({'ts': 152.0, 'path': p, 'ok': True})
        for names in ('AB', 'ABC'):
            (tp,) = self.rooms(self.reviewers(names, scratch))
            self.assertEqual(sorted(r['p'] for r in tp['rows']), list(names))
        self.assertEqual(len(self.rooms(self.reviewers('ABC', scratch), 'done')), 1)

    def test_a_copy_the_instruction_tells_to_keep_in_a_scratch_folder_is_no_change_of_the_work(self):
        agents = []
        for name in 'AB':
            own = os.path.join(self.folder, name + '.md')
            agents.append(self.agent('Read `%s` and follow it. Write your notes to `%s`. Keep a scratch copy in `%s`.' % (self.guide, own, self.scratch(name + '.md'))))
        (tp,) = self.rooms(agents)
        self.assertEqual(sorted(r['p'] for r in tp['rows']), ['A', 'B'])

    def test_a_file_of_the_work_in_the_repository_still_is(self):
        def work(name, a):
            p = os.path.join(self.top, 'src', name, 'part.py')
            write(p)
            a.writes.append({'ts': 152.0, 'path': p, 'ok': True})
        self.assertEqual(self.rooms(self.reviewers('AB', work)), [])
        self.assertEqual(len(self.rooms(self.reviewers('ABC', lambda n, a: work(n, a) if n == 'A' else None))), 1)         # one of three is not most of them

    def test_a_file_in_the_checkout_the_agent_works_in_is_the_work_whichever_repository_holds_the_guide(self):
        other = os.path.join(os.path.dirname(self.top), 'checkout')
        os.makedirs(os.path.join(other, '.git'))
        agents = []
        for name in 'AB':
            own = os.path.join(self.folder, name + '.md')
            code = os.path.join(other, 'src', name + '.py')
            write(code)
            a = self.agent('Read `%s` and follow it. Write your notes to `%s`.' % (self.guide, own), writes=[own, code])
            a.cwd = other
            agents.append(a)
        self.assertEqual(self.rooms(agents), [])

    def test_when_the_guide_is_in_no_repository_the_files_below_it_are_still_the_work_whatever_repository_the_agent_started_in(self):
        bare = os.path.join(os.path.dirname(self.top), 'plain')
        guide = write(os.path.join(bare, 'plan', 'agenda.md'), GUIDE)
        agents = []
        for name in 'ABC':
            own = os.path.join(bare, 'plan', name + '.md')
            probe = os.path.join(bare, 'plan', 'verify', 'probes', name + '.py')
            a = self.agent('Read `%s` and follow it. Write your notes to `%s`.' % (guide, own), writes=[own, probe])
            a.cwd = self.top                                                              # it was started in a repository elsewhere
            agents.append(a)
        s = types.SimpleNamespace(agents={a.id: a for a in agents}, _file_cache={}, _head_cache={}, cwd=self.top)
        with mock.patch.object(U, 'SCRATCH_DIRS', (os.path.join(os.path.dirname(self.top), 'scratch') + os.sep,)):          # the temporary folder of the machine is here
            jd = debates.judge(s, {a.id: 'running' for a in agents})
        self.assertEqual([t for d in jd.debates for t in d['topics'] if t.get('room')], [])          # probes beside a plan: parallel work

    def test_without_a_repository_a_scratch_folder_is_still_left_out(self):
        bare = os.path.join(os.path.dirname(self.top), 'plain')
        guide = write(os.path.join(bare, 'plan', 'agenda.md'), GUIDE)
        agents = []
        for name in 'AB':
            own = os.path.join(bare, 'plan', name + '.md')
            a = self.agent('Read `%s` and follow it. Write your notes to `%s`.' % (guide, own), writes=[own])
            a.cwd = bare
            agents.append(a)
        s = types.SimpleNamespace(agents={a.id: a for a in agents}, _file_cache={}, _head_cache={}, cwd=bare)
        rooms = lambda: [t for d in debates.judge(s, {a.id: 'running' for a in agents}).debates for t in d['topics'] if t.get('room')]
        with mock.patch.object(U, 'SCRATCH_DIRS', (os.path.join(os.path.dirname(self.top), 'scratch') + os.sep,)):          # the temporary folder of the machine is here
            for a in agents:
                a.writes.append({'ts': 152.0, 'path': self.scratch('repro_%s.py' % a.id), 'ok': True})
            self.assertEqual(len(rooms()), 1)
            for a in agents:                                                                  # a file of the project beside the folder is one
                a.writes.append({'ts': 153.0, 'path': os.path.join(bare, 'src', a.id + '.py'), 'ok': True})
            self.assertEqual(rooms(), [])


class Members(Repo):
    def talk(self):
        a = self.agent('Read `%s`. Settle the open items with the other participants by message; write no file.' % self.guide, desc='B Budget')
        b = self.agent('Read `%s`. Settle the open items with the other participants by message; write no file.' % self.guide, desc='C Risk')
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'my view'}]
        b.sent = [{'ts': 161.0, 'to': a.id, 'summary': '', 'text': 'mine'}]
        return a, b

    def test_a_meeting_by_message_is_a_room_of_participants_only(self):
        a, b = self.talk()
        jd = self.judge([a, b])
        (tp,) = self.topics(jd)
        self.assertEqual((tp['dir'], tp['title'], tp['room'], tp['rounds']), (self.folder, 'Weekly sync', 'members', []))
        self.assertEqual([(r['agents'], r['cells']) for r in tp['rows']], [([a.id], []), ([b.id], [])])
        self.assertEqual(jd.assignments, [])                                  # nobody holds a seat, whatever their tags say
        self.assertEqual({k: sorted(v) for k, v in jd.agent_units.items()}, {a.id: [self.folder], b.id: [self.folder]})
        self.assertEqual([x for x in jd.diag if x['code'] == 'debate_in_misc'], [])

    def test_one_message_one_way_is_enough_and_the_names_of_the_others_can_be_used(self):
        a, b = self.talk()
        b.sent = []
        a.sent = [{'ts': 160.0, 'to': 'C Risk', 'summary': '', 'text': 'x'}]            # by its description
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual(tp['room'], 'members')
        a.sent = [{'ts': 160.0, 'to': 'c', 'summary': '', 'text': 'x'}]                  # by its tag
        (tp,) = self.topics(self.judge([a, b]))
        self.assertEqual(tp['room'], 'members')

    def test_the_judgment_is_made_again_when_a_message_is_sent(self):
        a, b = self.talk()
        a.sent = b.sent = []
        s = types.SimpleNamespace(agents={a.id: a, b.id: b}, _file_cache={}, _head_cache={}, cwd=self.top)
        before = server.Session._debate_key(s, {a.id: 'running', b.id: 'running'})
        a.sent = [{'ts': 160.0, 'to': b.id, 'summary': '', 'text': 'x'}]
        after = server.Session._debate_key(s, {a.id: 'running', b.id: 'running'})
        self.assertNotEqual(before, after)


class Viewer(Repo):
    def test_the_guide_and_the_documents_of_a_room_open_in_the_document_view_and_nothing_else(self):
        a, b = self.pair()
        write(os.path.join(self.folder, 'A.md'))
        write(os.path.join(self.top, 'docs', 'other.md'))
        s = server.Session.__new__(server.Session)
        s.lock = __import__('threading').RLock()
        s.agents = {a.id: a, b.id: b}
        s.debates = lambda statuses: (debates.judge(types.SimpleNamespace(agents=s.agents, _file_cache={}, _head_cache={}, cwd=self.top), statuses).debates, {})
        self.assertEqual(s.allowed_file(self.guide), os.path.realpath(self.guide))
        self.assertEqual(s.allowed_file(os.path.join(self.folder, 'A.md')), os.path.realpath(os.path.join(self.folder, 'A.md')))
        self.assertIsNone(s.allowed_file(os.path.join(self.top, 'docs', 'other.md')))                # beside the room's folder, not in it


class ViewerOfTheRoomOnScreen(Repo):
    """The document view opens what the room on the screen holds: the room is the one the page shows, judged with the statuses the page was built with."""

    def session(self, agents, statuses):
        s = server.Session.__new__(server.Session)
        s.lock = __import__('threading').RLock()
        s.agents = {a.id: a for a in agents}
        s._file_cache, s._head_cache, s.cwd = {}, {}, self.top
        s._verdicts = {aid: types.SimpleNamespace(status=st) for aid, st in statuses.items()}
        return s

    def test_a_room_of_two_that_work_in_turns_while_both_are_running_opens_its_guide(self):
        a = self.agent(self.ask('', 'a', own=os.path.join(self.folder, 'A.md')), start=100.0, last=110.0)
        b = self.agent(self.ask('', 'b', own=os.path.join(self.folder, 'B.md')), start=200.0, last=210.0)
        write(os.path.join(self.folder, 'A.md'))
        s = self.session([a, b], {a.id: 'running', b.id: 'running'})
        shown = [t for d in s.debates({a.id: 'running', b.id: 'running'})[0] for t in d['topics']]
        self.assertEqual([t.get('room') for t in shown], ['cells'])                        # the page has the room: running agents are together until now
        self.assertEqual(s.allowed_file(self.guide), os.path.realpath(self.guide))
        self.assertEqual(s.allowed_file(os.path.join(self.folder, 'A.md')), os.path.realpath(os.path.join(self.folder, 'A.md')))
        self.assertIsNone(s.allowed_file(os.path.join(self.top, 'docs', 'other.md')))
        s = self.session([a, b], {a.id: 'done', b.id: 'done'})                              # when both have finished they never met: no room on the page, nothing opens
        self.assertIsNone(s.allowed_file(self.guide))

    def test_the_view_does_not_make_up_a_state_for_the_page(self):
        a = self.agent(self.ask('', 'a', own=os.path.join(self.folder, 'A.md')), start=100.0, last=110.0)
        b = self.agent(self.ask('', 'b', own=os.path.join(self.folder, 'B.md')), start=200.0, last=210.0)
        s = self.session([a, b], {a.id: 'running', b.id: 'running'})
        seen = []
        real = server.Session.debates
        s.debates = lambda statuses: seen.append(dict(statuses)) or real(s, statuses)
        s.allowed_file(self.guide)
        self.assertEqual(seen, [{a.id: 'running', b.id: 'running'}])


class Markers(unittest.TestCase):
    def test_the_sentence_that_addresses_the_participant_is_the_marker(self):
        for text, want in (('You are participant B (Budget). Read x.', 'B'), ('Work as C (Risk). Read x.', 'C'), ('You hold seat D. Read x.', 'D'),
                           ('Hello. You are participant E', 'E'), ('당신은 B 담당입니다.', 'B'), ('당신은 B(예산) 담당입니다.', 'B'), ('[ROOM-B] hi', 'B')):
            self.assertEqual(U._marker_of(text), want, text)

    def test_the_same_words_with_another_meaning_are_not(self):
        for text in ('Compile the sample as C (not C++) before anything else.', 'Book seat C on the train.', 'Note that participant C of the user study asked for a dark theme.',
                     'You are not participant C.', 'Example of a first line: "You are participant B (Budget)." That is only an example.', 'C(언어) 담당 팀이 만든 샘플을 확인하세요.',
                     'C 담당자에게 문의 내용을 전달하세요.', 'Tell the user you are participant B', 'Work as C++ developers do.'):
            self.assertEqual(U._marker_of(text), '', text)


if __name__ == '__main__':
    unittest.main()
