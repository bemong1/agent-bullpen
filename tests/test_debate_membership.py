"""Who belongs to a debate, and who holds a seat, is decided by what an agent did, never by what it was told or how a report path was spelled.

A debate folder is a Unit: it has an exact round folder (r1, r01, round1). A seat (a cell) is a file `<folder>/<round folder>/<name>.md` that a sure write made, that a run was asked to save (a launch
command's `-o` or redirect, the seat of a room tag), or that is on disk from before. An agent that only reads the folder, edits a report that is not its own, or has a room tag and no seat gets no cell.
The input is the events of the agents (`judge_support.agent(...)`), the page is `debates.judge` of a stand-in session.

    python3 -m unittest tests.test_debate_membership
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture as Disk, agent, page_session, plan, rd, tag, wr  # noqa: E402
from compat import server  # noqa: E402

from board import debates  # noqa: E402

BRIEF = ('# review — branch policy\n\n'
         '| topic | folder | deps | final |\n|---|---|---|---|\n| Branch policy | `review_branch_policy/` | — | |\n\n'
         '**A — development flow**\n**B — gate design**\n**C — GitHub operations**\n')
REL = 'docs/records/review_branch_policy'


class Fixture(Disk):
    """A repository with the debate folder `docs/records/review_branch_policy`: a guide (its text names three roles: a sentence, no seat) and the round folders r1 and r2, still empty."""

    def setUp(self):
        super().setUp()
        self.repo = self.p('repo')
        self.unit = os.path.join(self.repo, *REL.split('/'))
        self.brief = self.file('repo/%s/brief.md' % REL, BRIEF)
        self.dirs('repo/%s/r1' % REL, 'repo/%s/r2' % REL)

    def report(self, stem, rnd=1):
        """The path of a report file of the debate (the file itself is not made)."""
        return os.path.join(self.unit, 'r%d' % rnd, stem + '.md')

    def agent(self, aid, **kw):
        kw.setdefault('status', 'running')
        kw.setdefault('cwd', self.repo)
        return agent(aid, **kw)

    def rows(self, jd):
        self.assertEqual(len(jd.debates), 1, [d['root'] for d in jd.debates])
        d = jd.debates[0]
        self.assertEqual(d['root'], self.unit)
        return {r['p']: r for r in d['topics'][0]['rows']}


class ReportedCase(Fixture):
    """review: Claude claude -p (A) and two Codex exec (B, C), cwd = repo root, each told to save its report (a redirect, two `-o`), nothing written yet, names A_flow / B_gate / C_github."""

    def setUp(self):
        super().setUp()
        self.a = self.agent('a1b2c3d4-0000-4000-8000-000000000000', origin='cli', planned=[plan(self.report('A_flow'), op='>')])
        self.b = self.agent('019a0065-0000-7000-8000-000000000000', provider='codex', origin='exec', planned=[plan(self.report('B_gate'))])
        self.c = self.agent('019a0066-0000-7000-8000-000000000000', provider='codex', origin='exec', planned=[plan(self.report('C_github'))], reads=[rd(self.brief, 1010.0, 'codex')])

    def test_all_three_sit_in_the_room_before_any_file_exists(self):
        jd = self.page(self.a, self.b, self.c)
        rows = self.rows(jd)
        self.assertEqual(sorted(rows), ['A_flow', 'B_gate', 'C_github'])    # file names as the row names, no stray A/B/C rows
        for stem, ag in (('A_flow', self.a), ('B_gate', self.b), ('C_github', self.c)):
            cell = rows[stem]['cells'][0]
            self.assertEqual((cell['round'], cell['agent'], cell['state'], cell['planned']), (1, ag.id, 'writing', True), stem)
            self.assertEqual(jd.members[ag.id], {self.unit})                 # shown in the room, not as "other work"
            self.assertNotIn(ag.id, jd.agent_units)                         # nobody owns a cell yet: a request is no ownership

    def test_the_role_rows_of_the_brief_make_no_row_and_no_role(self):
        # replaces the old "roles follow the leading letter": the brief names three roles in a sentence, and the judgment reads no sentence
        rows = self.rows(self.page(self.a))
        self.assertEqual(sorted(rows), ['A_flow'])
        self.assertEqual([r['role'] for r in rows.values()], [''])

    def test_cells_follow_the_files(self):
        pa = self.file('repo/%s/r1/A_flow.md' % REL, 'a\nb\n', 200)
        a = self.agent('a1b2c3d4-0000-4000-8000-000000000000', origin='cli', status='done', planned=[plan(pa, op='>')], writes=[wr(pa, 200)])
        b, c = (self.agent(x.id, provider='codex', origin='exec', status='done', planned=x.planned) for x in (self.b, self.c))
        rows = self.rows(self.page(a, b, c))
        self.assertEqual((rows['A_flow']['cells'][0]['state'], rows['A_flow']['cells'][0]['lines']), ('done', 2))
        self.assertEqual(rows['B_gate']['cells'][0]['state'], 'missing')    # its agent finished but wrote nothing

    def test_claude_and_codex_mix_in_one_debate(self):
        jd = self.page(self.a, self.b, self.c)
        self.assertEqual({x.provider for x in (self.a, self.b, self.c)}, {'claude', 'codex'})
        self.assertEqual(len(jd.debates), 1)
        self.assertEqual(sum(len(r['agents']) for r in jd.debates[0]['topics'][0]['rows']), 3)

    def test_cwd_can_come_from_the_link_entry(self):
        # the working folder of a claude -p child is known from the link; it is a fact of the agent (tops of repositories, the work outside a room), no longer the base of a path in a sentence
        s, st = page_session(self.a)
        a = s.agents[self.a.id]
        a.cwd = ''
        a.cli = {'cwd': self.repo}
        self.assertEqual(debates.agent_cwd(a), self.repo)
        self.assertEqual(debates.facts_of(a).cwd, self.repo)
        self.assertEqual(self.rows(debates.judge(s, st))['A_flow']['cells'][0]['agent'], self.a.id)

    def test_without_a_cwd_nothing_is_guessed(self):
        s, st = page_session(self.a)
        a = s.agents[self.a.id]
        a.cwd, a.cli = '', None
        self.assertEqual(debates.agent_cwd(a), '')
        a.cwd = 'repo'                                                       # a folder that is not absolute is no folder
        self.assertEqual(debates.agent_cwd(a), '')
        self.assertEqual(debates.facts_of(a).cwd, '')
        self.assertEqual(self.rows(debates.judge(s, st))['A_flow']['cells'][0]['agent'], self.a.id)     # the seat is made by the event and its absolute path: no cwd is needed


class NamesAreFiles(Fixture):
    """The name of a row is the name of a file. Two files that begin with the same letter are two rows (the letter of a name is no alias), and a name is any characters."""

    def test_bare_and_named_files_stay_separate(self):
        old, new = self.file('repo/%s/r1/A.md' % REL, 'old\n', 150), self.file('repo/%s/r1/A_flow.md' % REL, 'new\n', 160)
        rows = self.rows(self.page(self.agent('a1', status='done', writes=[wr(new, 160)])))
        self.assertEqual(sorted(rows), ['A', 'A_flow'])
        self.assertEqual(rows['A_flow']['cells'][0]['agent'], 'a1')
        self.assertEqual((rows['A']['cells'][0]['agent'], rows['A']['cells'][0]['state']), (None, 'previous'))      # the older file is nobody's: a grey cell, not A_flow's
        self.assertEqual([rows['A']['role'], rows['A_flow']['role']], ['', ''])

    def test_two_named_files_of_one_letter_stay_separate(self):
        self.file('repo/%s/r1/A_flow.md' % REL, '1\n', 150)
        other = self.file('repo/%s/r2/A_other.md' % REL, '2\n', 160)
        rows = self.rows(self.page(self.agent('a1', status='done', writes=[wr(other, 160)])))
        self.assertEqual(sorted(rows), ['A_flow', 'A_other'])

    def test_a_name_is_any_characters(self):
        path = self.file('repo/%s/r1/검토 2.md' % REL, 'x\n', 160)
        rows = self.rows(self.page(self.agent('a1', status='done', writes=[wr(path, 160)])))
        self.assertEqual(sorted(rows), ['검토 2'])
        self.assertEqual(rows['검토 2']['cells'][0]['agent'], 'a1')


class SeatsOfATag(Fixture):
    """A room tag (BULLPEN_ROOM, BULLPEN_SEAT) is a fact: the room and a seat as `rN/name`. With no seat it puts the agent in the debate and makes no cell; the seat of a round makes a cell that waits for the report."""

    def test_a_tag_without_a_seat_puts_the_agent_in_the_debate_and_makes_no_cell(self):
        a = self.agent('a1', tag=tag(self.unit), reads=[rd(self.brief, 1.0)])
        jd = self.page(a)
        self.assertEqual(jd.debates[0]['root'], self.unit)                  # the tag lists the debate
        self.assertEqual(self.rows(jd), {})
        self.assertEqual(jd.members['a1'], {self.unit})
        self.assertNotIn('a1', jd.agent_units)
        self.assertEqual([(p['agent'], p['why'], p['sure']) for p in jd.debates[0]['topics'][0]['placed']], [('a1', 'tag', True)])

    def test_the_seat_of_a_round_makes_a_cell_that_waits_for_the_report(self):
        rows = self.rows(self.page(self.agent('a1', tag=tag(self.unit, 'r1/B_gate'))))
        self.assertEqual([(c['round'], c['agent'], c['state']) for c in rows['B_gate']['cells']], [(1, 'a1', 'writing')])

    def test_a_seat_without_a_round_is_a_line_and_not_a_cell(self):
        jd = self.page(self.agent('a1', tag=tag(self.unit, 'B_gate')))
        self.assertEqual(self.rows(jd), {})
        self.assertEqual(jd.members['a1'], {self.unit})

    def test_a_write_in_the_folder_that_is_no_report_makes_no_seat(self):
        # the old rule seated a tagged agent on any write inside the folder; a file that is not `<round folder>/<name>.md` is no cell
        notes = self.file('repo/%s/notes.md' % REL, 'n\n', 150)
        other = self.file('repo/%s/work/notes.txt' % REL, 'n\n', 150)
        jd = self.page(self.agent('a1', tag=tag(self.unit), writes=[wr(notes, 150), wr(other, 160)]))
        self.assertEqual(self.rows(jd), {})
        self.assertNotIn('a1', jd.agent_units)

    def test_a_write_that_failed_or_left_no_file_gets_no_seat(self):
        # the structural half of the old tag tests: a result that says it failed, and one that is not known with nothing on disk, make no cell (C02)
        ok = self.file('repo/%s/r1/A_flow.md' % REL, 'a\n', 150)
        a = self.agent('a1', status='done', writes=[wr(ok, 150)])
        b = self.agent('b1', status='done', writes=[wr(self.report('B_gate'), 150, ok=False)])
        c = self.agent('c1', status='done', writes=[wr(self.report('C_github'), 150, ok=None)])
        jd = self.page(a, b, c)
        self.assertEqual(sorted(self.rows(jd)), ['A_flow'])
        self.assertTrue({'b1', 'c1'}.isdisjoint(jd.agent_units))
        self.assertTrue({'b1', 'c1'}.isdisjoint(jd.members))


class NoFalseDebates(Fixture):
    def test_report_like_text_in_an_unrelated_repo(self):
        # the structure that looked like a debate in another repository: a request for a report where there is no round folder on disk, a write that failed, one whose result is not known, a read
        other = self.p('other')
        notes = os.path.join(other, 'docs', 'notes')
        self.dirs('other/docs/notes')
        a = agent('b0000000000000001', status='running', cwd=other, planned=[plan(os.path.join(notes, 'r1', 'x.md'), op='>')], reads=[rd(os.path.join(notes, 'brief.md'))],
                  writes=[wr(os.path.join(notes, 'r1', 'x.md'), 160, ok=False)])
        c = agent('b0000000000000009', provider='codex', origin='exec', status='running', cwd=other, planned=[plan(os.path.join(notes, 'r2', 'y.md'))],
                  writes=[wr(os.path.join(notes, 'r2', 'y.md'), 160, ok=None)])
        jd = self.page(a, c)
        self.assertEqual(jd.debates, [])
        self.assertEqual(dict(jd.members), {})
        self.assertEqual(dict(jd.agent_units), {})

    def test_folder_without_brief_or_exact_round_folder_is_not_a_debate(self):
        # r1x is not r<N>, r2_check and r1_run are not rounds; a folder with only a guide, or with a report-like file and no round folder, is no debate (what a sure write or a read of them does not change)
        a = self.file('other/docs/plain/r1x/A.md', 'x\n', 150)
        self.file('other/docs/plain/r1_run.log', 'x\n', 150)
        b = self.file('other/docs/plain2/A.md', 'x\n', 150)
        self.file('other/docs/plain2/brief.md', '# plain\n', 150)
        c = self.file('other/work_X/r2_check/z.md', 'x\n', 150)
        guide = self.file('other/docs/guide/brief.md', '# guide\n', 150)
        jd = self.page(agent('b0000000000000002', writes=[wr(a, 150), wr(b, 151), wr(c, 152)], reads=[rd(guide), rd(b)], cwd=self.p('other')))
        self.assertEqual(jd.debates, [])
        self.assertEqual(dict(jd.members), {})

    def test_marker_text_without_a_folder_seats_nobody(self):
        # a seat marker with no usable folder: a room tag with a seat on a folder that is not there, and a tag on a folder that holds no debate (one agent alone makes no room)
        a = agent('b0000000000000004', status='running', cwd=self.repo, tag=tag(self.p('nowhere'), 'r1/C'))
        b = agent('b0000000000000014', status='running', cwd=self.repo, tag=tag(self.p('other-place'), 'C'))
        self.dirs('other-place')
        c = agent('b0000000000000024', status='running', cwd=self.repo, tag=tag(self.p(), 'r1/C'))      # a folder above the repository: it holds no debate itself
        jd = self.page(a, b, c)
        self.assertEqual(jd.debates, [])
        self.assertEqual(dict(jd.members), {})

    def test_reader_only_reviewer_is_not_a_participant(self):
        author = self.agent('b0000000000000005', status='done', writes=[wr(self.file('repo/%s/r1/A_flow.md' % REL, 'report\n', 150), 150)])
        rev = self.agent('b0000000000000006', status='done', reads=[rd(self.brief, 1.0), rd(self.report('A_flow'), 2.0)])
        jd = self.page(author, rev)
        self.assertEqual(jd.members[rev.id], {self.unit})                    # it works in the debate (it read the guide), with no seat
        self.assertEqual(set(jd.agent_units), {author.id})
        rows = self.rows(jd)
        self.assertEqual(sorted(rows), ['A_flow'])                           # no row of its own, no row from the role lines of the guide
        for r in rows.values():
            self.assertNotIn(rev.id, r['agents'])
        self.assertTrue(rows['A_flow']['cells'][0]['readers'])               # it still shows as a cross-review "read"
        self.assertEqual((jd.placed[rev.id]['why'], jd.placed[rev.id]['sure']), ('guide_read', False))       # a guess, and told as one

    def test_reviewer_with_a_tag_and_only_reads(self):
        author = self.agent('b0000000000000007', status='done', writes=[wr(self.file('repo/%s/r1/A_flow.md' % REL, 'report\n', 150), 150)])
        rev = self.agent('b0000000000000008', status='running', tag=tag(self.unit), reads=[rd(self.brief, 1.0)])
        jd = self.page(author, rev)
        self.assertNotIn(rev.id, jd.agent_units)                             # a tag puts it in the room: it never gives a cell
        self.assertEqual(sorted(self.rows(jd)), ['A_flow'])
        self.assertEqual((jd.placed[rev.id]['why'], jd.placed[rev.id]['sure']), ('tag', True))

    def test_an_editor_of_a_report_is_not_its_seat(self):
        path = self.file('repo/%s/r1/A_flow.md' % REL, 'report\n', 300)
        author = self.agent('b0000000000000015', status='done', writes=[wr(path, 200)])
        editor = self.agent('b0000000000000016', status='done', writes=[wr(path, 300, kind='update')])
        jd = self.page(author, editor)
        cell = self.rows(jd)['A_flow']['cells'][0]
        self.assertEqual((cell['owner'], cell['agent'], cell['editors']), (author.id, author.id, [editor.id]))
        self.assertEqual(set(jd.agent_units), {author.id})


class NextRound(Fixture):
    """review round 2: the same session (a Codex turn that is resumed, a Claude agent that is sent a message) is asked to save its r2 report after it wrote r1. The new request (the `-o` of the new turn,
    the seat `r2/name` of the new run) or its write seats it in r2; the r1 report stays a report of r1."""

    def wrote_r1(self, stem, **kw):
        return self.agent(kw.pop('aid'), status='running', run=2, run_start=400.0, writes=[wr(self.file('repo/%s/r1/%s.md' % (REL, stem), 'r1\n', 200), 200, run=1)], **kw)

    def test_codex_resume_moves_to_round_two(self):
        b = self.wrote_r1('B_gate', aid='019a0065-0000-7000-8000-000000000000', provider='codex', origin='exec', planned=[plan(self.report('B_gate', 2), ts=400.0, run=2)])
        rows = self.rows(self.page(b))
        r1, r2 = rows['B_gate']['cells']
        self.assertEqual((r1['state'], r2['state'], r2['agent']), ('done', 'writing', b.id))
        self.assertEqual(sorted(rows), ['B_gate'])                           # the r1 report it re-reads is not a second seat

    def test_claude_next_round_message(self):
        a = self.wrote_r1('A_flow', aid='d0000000000000001', origin='cli', tag=tag(self.unit, 'r2/A_flow', run=2))
        r1, r2 = self.rows(self.page(a))['A_flow']['cells']
        self.assertEqual((r1['state'], r2['state'], r2['agent']), ('done', 'writing', a.id))
        b = self.wrote_r1('A_flow', aid='d0000000000000002', origin='cli')       # no seat, but its record shows the r2 report written: the round it works in is r2 then
        path = self.file('repo/%s/r2/A_flow.md' % REL, 'r2\n', 450)
        b = self.agent(b.id, origin='cli', run=2, run_start=400.0, writes=[*b.writes, wr(path, 450, run=2)])
        r1, r2 = self.rows(self.page(b))['A_flow']['cells']
        self.assertEqual((r1['state'], r2['state'], r2['agent']), ('done', 'draft', b.id))

    def test_prefix_outside_a_debate_folder_is_ignored(self):
        # the next round named in a folder that is no debate (no round folder on disk) is no seat and no debate; it stays on r1
        b = self.wrote_r1('B_gate', aid='019a0065-0000-7000-8000-000000000000', provider='codex', origin='exec', planned=[plan(self.p('repo', 'docs', 'notes', 'r2', 'B_gate.md'), ts=400.0, run=2)])
        jd = self.page(b)
        cells = self.rows(jd)['B_gate']['cells']
        self.assertEqual([(c['round'], c['state']) for c in cells], [(1, 'done')])     # still on r1, which its run 1 saved and the run 2 asks nothing of (D19); no seat in a folder that is not a debate


class CwdRecording(unittest.TestCase):
    def test_first_cwd_wins(self):
        a = server.Agent('c0000000000000001', {})
        a.feed({'type': 'user', 'timestamp': '2026-10-01T00:00:00.000Z', 'cwd': '/repo', 'message': {'content': 'hi'}})
        a.feed({'type': 'assistant', 'timestamp': '2026-10-01T00:00:01.000Z', 'cwd': '/repo/sub', 'message': {'content': []}})
        self.assertEqual(a.cwd, '/repo')

    def test_codex_agent_keeps_its_entry_cwd(self):
        e = {'id': '019a0001-0000-7000-8000-000000000000', 'meta_ts': 1.0, 'path': '/x.jsonl', 'cwd': '/repo', 'model': 'm'}
        self.assertEqual(server.CodexAgent(e, {}).cwd, '/repo')


if __name__ == '__main__':
    unittest.main()
