"""One folder shown once: the same real folder (a link into it), and the copies of one repository's folder in its worktrees (CONTRACT J17).

Nothing is moved from one copy to another: what was read in a copy, and who is placed there, stays with that copy. A copy of a folder that this session has nothing to do with (no cell written or asked
for in it, no room tag or hint of the orchestrator pointing at it; a read is none) is not shown when a copy with something of this session is; where no copy has, all are shown (the walk found them).
A copy that has something stays, whatever else is a copy of it. A folder reached through a link is the folder of its real path. A folder that holds only an instruction (a guide and a round
folder with no report) and sits inside a debate that shows something is part of its records.

    python3 -m unittest tests.test_copies
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, hint, plan, rd, tag, wr  # noqa: E402

from board import copies, units as U  # noqa: E402

BRIEF = '# Release review\n\nReviewers and their result files are named in the round folders.\n'


class Family(Fixture):
    """A repository `main` and two linked worktrees `wt1` and `wt2` of it, each holding the same debate folder `docs/rev` (a guide and a round folder with two reports, A.md and B.md)."""

    def setUp(self):
        super().setUp()
        self.base = self.p('dev')
        self.main = self.checkout('main')
        self.wt1 = self.checkout('wt1', of=self.main)
        self.wt2 = self.checkout('wt2', of=self.main)
        for top in (self.main, self.wt1, self.wt2):
            self.fill(top)

    def put(self, path, text='x\n'):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def checkout(self, name, of=None):
        top = os.path.join(self.base, name)
        if of is None:
            os.makedirs(os.path.join(top, '.git'))
        else:
            gd = os.path.join(of, '.git', 'worktrees', name)
            self.put(os.path.join(gd, 'commondir'), '../..\n')
            self.put(os.path.join(top, '.git'), 'gitdir: %s\n' % gd)
        return top

    def fill(self, top, rel='docs/rev'):
        self.put(os.path.join(top, rel, 'brief.md'), BRIEF)
        for p in 'AB':
            self.put(os.path.join(top, rel, 'r1', p + '.md'), '# %s\n\nfindings\n' % p)

    def rev(self, top):
        return os.path.join(top, 'docs', 'rev')

    def report(self, top, p='A', rnd='r1'):
        return os.path.join(top, 'docs', 'rev', rnd, p + '.md')

    def agent(self, aid, cwd, reads=(), writes=(), **kw):
        """An agent that works in `cwd`: it wrote `writes` (write events) and read the paths `reads`."""
        return agent(aid, writes=writes, reads=[rd(p, 120.0) for p in reads], cwd=cwd, **kw)

    def judge(self, agents=(), walked=None):
        walked = [self.rev(t) for t in (self.main, self.wt1, self.wt2)] if walked is None else walked
        return self.page(*agents, walked=walked, cwd=self.main)

    def seated(self, top, p='A', cwd=None, aid=None):
        """An agent that wrote the report `p` of the copy in `top` with a tool (a sure write)."""
        return self.agent(aid or 'seat-' + p, cwd or top, writes=[wr(self.report(top, p), 150)])

    def roots(self, jd):
        return [d['root'] for d in jd.debates]


class GitIdentity(Family):
    def test_the_common_folder_of_a_main_checkout_a_linked_worktree_and_a_submodule(self):
        self.assertEqual(copies.git_common_dir(self.main), os.path.join(self.main, '.git'))
        self.assertEqual(copies.git_common_dir(self.wt1), os.path.join(self.main, '.git'))
        self.assertEqual(copies.git_common_dir(self.wt2), os.path.join(self.main, '.git'))
        sub = os.path.join(self.base, 'sub')
        gd = os.path.join(self.main, '.git', 'modules', 'sub')
        os.makedirs(gd)
        self.put(os.path.join(sub, '.git'), 'gitdir: ../main/.git/modules/sub\n')                  # a submodule has no commondir: its own folder is the common one
        self.assertEqual(copies.git_common_dir(sub), gd)

    def test_a_broken_or_missing_pointer_is_no_repository(self):
        odd = os.path.join(self.base, 'odd')
        self.put(os.path.join(odd, '.git'), 'nonsense\n')
        self.assertIsNone(copies.git_common_dir(odd))
        self.put(os.path.join(odd, '.git'), 'gitdir: %s/gone\n' % self.base)
        self.assertIsNone(copies.git_common_dir(odd))
        self.assertIsNone(copies.git_common_dir(os.path.join(self.base, 'nowhere')))

    def test_identity_is_the_repository_and_the_place_in_it(self):
        ids = {t: copies.identity(self.rev(t)) for t in (self.main, self.wt1, self.wt2)}
        self.assertEqual(len(set(ids.values())), 1)
        self.assertNotEqual(copies.identity(os.path.join(self.main, 'docs', 'other')), ids[self.main])
        other = self.checkout('other')
        self.fill(other)
        self.assertNotEqual(copies.identity(self.rev(other)), ids[self.main])           # another repository's folder of the same name is another folder

    def test_a_link_into_the_folder_has_the_identity_of_the_folder(self):
        alias = os.path.join(self.base, 'alias')
        os.symlink(self.rev(self.main), alias)
        self.assertEqual(copies.identity(alias), copies.identity(self.rev(self.main)))

    def test_a_folder_in_no_repository_is_known_by_its_real_path(self):
        plain = os.path.join(self.base, 'plain', 'rev')
        os.makedirs(plain)
        alias = os.path.join(self.base, 'plain_alias')
        os.symlink(plain, alias)
        self.assertEqual(copies.identity(alias), copies.identity(plain))
        copy = os.path.join(self.base, 'plain_copy', 'rev')
        os.makedirs(copy)
        self.assertNotEqual(copies.identity(copy), copies.identity(plain))                          # a copy made by hand is a folder of its own

    def test_the_judgment_asks_the_disk_through_a_catalog_and_gets_the_same_answers(self):
        cat, memo = U.Catalog(), {}
        cat.begin()
        for t in (self.main, self.wt1, self.wt2):
            self.assertEqual(copies.identity(self.rev(t), memo, cat), copies.identity(self.rev(t)))
        self.assertEqual(copies.git_common_dir(self.wt1, cat), os.path.join(self.main, '.git'))
        self.assertTrue(copies.is_main_checkout(self.rev(self.main), cat))
        self.assertFalse(copies.is_main_checkout(self.rev(self.wt1), cat))
        self.assertEqual({op for (op, _arg), _ans in cat.reads()} - {'stat', 'lstat', 'real', 'line'}, set())      # nothing but the door of the Catalog


class Fold(Family):
    def test_copies_nobody_works_in_are_one_debate_the_main_checkout_stands_for(self):
        # O10: where no copy has anything of this session in it (the walk found them all) one stands for them, the main checkout first; nothing is moved
        jd = self.judge()
        self.assertEqual(self.roots(jd), [self.rev(self.main)])
        self.assertEqual([d['copies'] for d in jd.debates], [2])
        no_main = self.judge(walked=[self.rev(t) for t in (self.wt2, self.wt1)])
        self.assertEqual(self.roots(no_main), [self.rev(self.wt1)])                              # no main checkout among them: the shortest path, then the name (wt1 and wt2 are alike)

    def test_the_copy_the_session_works_in_stands_for_the_others(self):
        a = self.seated(self.wt2)
        jd = self.judge([a])
        self.assertEqual(self.roots(jd), [self.rev(self.wt2)])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([(r['p'], r['cells'][0]['state'], r['cells'][0]['agent']) for r in tp['rows']], [('A', 'done', a.id), ('B', 'previous', None)])      # B.md is on the disk from before
        self.assertEqual(jd.debates[0]['copies'], 2)

    def test_a_copy_that_was_only_read_or_written_in_is_not_kept_beside_one_with_a_cell(self):
        # J17 turned this over (a read or a write used to make its copy stand for the others): a read is no evidence, and a write that is no cell (notes.md) is none either; only a cell that was
        # written (sure) or asked for is
        reader = self.agent('rd1', self.wt1, reads=[self.report(self.wt1, 'B')])
        writer = self.agent('wr1', self.main, writes=[wr(os.path.join(self.rev(self.main), 'notes.md'), 150)])
        self.assertEqual(self.roots(self.judge([reader, writer])), [self.rev(self.main)])                         # no copy has any evidence: the main checkout stands alone (O10), not the one that was read
        author = self.agent('wr2', self.wt2, writes=[wr(self.report(self.wt2, 'B'), 150)])
        self.assertEqual(self.roots(self.judge([reader, writer, author])), [self.rev(self.wt2)])                 # the one with a cell written in it stands alone: the read copy and the written one are hidden

    def test_a_read_of_a_copy_that_was_folded_is_not_moved_to_the_cell_of_the_one_that_stands_for_it(self):
        a = self.seated(self.wt2)
        reader = self.agent('rv', self.wt1, reads=[self.report(self.wt1, 'A')])
        near = self.agent('nr', self.wt2, reads=[self.report(self.wt2, 'A')])
        jd = self.judge([a, reader, near])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual(self.roots(jd), [self.rev(self.wt2)])
        self.assertEqual([c['readers'] for c in tp['rows'][0]['cells']], [['nr']])                             # the read in the copy that is shown shows; the one in the hidden copy is not carried over

    def test_a_write_that_failed_does_not_make_its_copy_the_one_that_stands_for_the_others(self):
        reader = self.agent('rd1', self.wt2, reads=[self.report(self.wt2, 'B')])
        author = self.agent('au1', self.wt2, writes=[wr(self.report(self.wt2, 'A'), 150)])                     # the copy this session works in
        path = self.report(self.wt1, 'B')
        failed = self.agent('f1', self.wt1, writes=[wr(path, 150, ok=False)])
        self.assertEqual(self.roots(self.judge([reader, author, failed])), [self.rev(self.wt2)])               # a failed write says nothing: its copy is hidden like the main checkout
        pending = self.agent('p1', self.wt1, writes=[wr(path, 150, ok=None)])                                  # a call whose result is not known
        self.assertEqual(self.roots(self.judge([reader, author, pending])), [self.rev(self.wt2)])
        made = self.agent('m1', self.wt1, writes=[wr(path, 150)])
        self.assertEqual(sorted(self.roots(self.judge([reader, author, made]))), sorted(self.rev(t) for t in (self.wt1, self.wt2)))      # a write that worked is a copy this session is in too

    def test_a_shell_write_is_believed_only_when_its_call_worked(self):
        author = self.agent('au1', self.wt2, writes=[wr(self.report(self.wt2, 'A'), 150)])                     # the copy this session works in
        note = self.put(self.report(self.wt1, 'C'))
        for shell_ok, tops in ((False, [self.wt2]), (None, [self.wt2]), (True, [self.wt1, self.wt2])):
            sh = self.agent('sh1', self.wt1, writes=[wr(note, 150, evidence='shell', proof='exit', ok=shell_ok)])
            self.assertEqual(sorted(self.roots(self.judge([author, sh]))), sorted(self.rev(t) for t in tops), shell_ok)

    def test_a_read_of_another_report_with_other_content_is_not_a_read_of_the_one_that_stands_for_it(self):
        # nothing is compared and nothing is moved any more (`copies.same_file` is gone): a read in a hidden copy shows on no cell, whether the text there is the same or not
        a = self.seated(self.wt2)
        self.put(self.report(self.wt2, 'A'), '# A\n\nrevised in this worktree\n')                              # the main checkout's A.md is another text
        reader = self.agent('rv', self.main, reads=[self.report(self.main, 'A')])
        same = self.agent('sm', self.wt1, reads=[self.report(self.wt1, 'B')])
        jd = self.judge([a, reader, same])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual(jd.debates[0]['root'], self.rev(self.wt2))
        self.assertEqual([c['readers'] for r in tp['rows'] for c in r['cells']], [[], []])

    def test_a_read_of_the_real_file_shows_on_the_cell_of_the_folder_reached_through_the_link(self):
        link = os.path.join(self.base, 'view')
        os.symlink(os.path.join(self.main, 'docs'), link)
        path = os.path.join(link, 'rev', 'r1', 'A.md')
        a = self.agent('a1', link, writes=[wr(path, 150)])
        reader = self.agent('rv', self.main, reads=[self.report(self.main, 'A')])
        jd = self.judge([a, reader], walked=[self.rev(self.main)])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual(jd.debates[0]['root'], self.rev(self.main))                                            # the real path is the folder
        self.assertEqual([c['readers'] for c in tp['rows'][0]['cells']], [['rv']])
        self.assertEqual(tp['rows'][0]['cells'][0]['agent'], a.id)                                              # the write through the link is the write of that cell

    def test_a_read_through_the_link_shows_on_the_cell_of_the_real_file(self):
        # CONTRACT J17: the read paths are brought to their real path before the judgment, as the write events are. `debates._debate` matches the readers of a cell by the path as the agent spelled it
        # (`a.reads`), so a read spelled with the link does not show on the cell (whose path is the real one). A known miss: it comes out of the expected failures when the readers are matched by real path.
        link = os.path.join(self.base, 'view')
        os.symlink(os.path.join(self.main, 'docs'), link)
        a = self.agent('a1', self.main, writes=[wr(self.report(self.main, 'A'), 150)])
        reader = self.agent('rv', self.main, reads=[os.path.join(link, 'rev', 'r1', 'A.md')])
        jd = self.judge([a, reader], walked=[self.rev(self.main)])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([c['readers'] for c in tp['rows'][0]['cells']], [['rv']])

    def test_two_copies_that_both_have_a_seat_stay_two_debates_and_the_third_is_folded(self):
        a, b = self.seated(self.wt1), self.seated(self.wt2, 'B')
        jd = self.judge([a, b])
        self.assertEqual(sorted(self.roots(jd)), sorted(self.rev(t) for t in (self.wt1, self.wt2)))
        self.assertEqual(sorted(d['copies'] for d in jd.debates), [0, 1])             # the main checkout is hidden behind one of them; no cell is hidden

    def test_the_agents_of_a_folded_copy_do_not_move_to_the_debate_that_stands_for_it(self):
        a = self.seated(self.wt2)
        reader = self.agent('rd1', self.wt1, reads=[self.report(self.wt1, 'A'), os.path.join(self.rev(self.wt1), 'brief.md')])
        near = self.agent('nr', self.wt2, reads=[os.path.join(self.rev(self.wt2), 'brief.md')])
        jd = self.judge([a, reader, near])
        self.assertEqual(self.roots(jd), [self.rev(self.wt2)])
        self.assertEqual({k: sorted(v) for k, v in jd.members.items() if k != a.id}, {'nr': [self.rev(self.wt2)]})      # the reader of the hidden copy is placed nowhere; the one who read the guide of the shown copy is

    def test_another_repository_and_a_folder_copied_by_hand_are_not_folded(self):
        other = self.checkout('other')
        self.fill(other)
        hand = os.path.join(self.base, 'hand')
        self.fill(hand)
        walked = [self.rev(t) for t in (self.main, self.wt1, other, hand)]
        a = self.seated(self.wt1)                                                           # the session works in the worktree: the main checkout is a copy of it, the others are not
        jd = self.judge([a], walked=walked)
        self.assertEqual(sorted(self.roots(jd)), sorted(self.rev(t) for t in (self.wt1, other, hand)))
        self.assertEqual(sorted(d['copies'] for d in jd.debates), [0, 0, 1])

    def test_a_link_into_the_folder_is_the_folder_and_the_real_path_is_what_is_listed(self):
        link = os.path.join(self.base, 'view')
        os.symlink(os.path.join(self.main, 'docs'), link)
        path = os.path.join(link, 'rev', 'r1', 'A.md')
        a = self.agent('a1', link, writes=[wr(path, 150)])
        jd = self.judge([a], walked=[self.rev(self.main)])
        self.assertEqual(self.roots(jd), [self.rev(self.main)])                              # J17: the events are brought to their real path first, the cells are keyed by it (the old rule kept the spelling)
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([(r['p'], r['cells'][0]['agent']) for r in tp['rows']], [('A', a.id), ('B', None)])
        self.assertEqual(jd.debates[0]['copies'], 0)                                         # one folder, not a copy of itself

    def test_the_same_real_path_is_one_folder(self):                                         # C57
        link = os.path.join(self.base, 'view')
        os.symlink(os.path.join(self.main, 'docs'), link)
        a = self.agent('a1', link, writes=[wr(os.path.join(link, 'rev', 'r1', 'A.md'), 150)])
        b = self.agent('b1', self.main, writes=[wr(self.report(self.main, 'B'), 160)])
        jd = self.judge([a, b], walked=[os.path.join(link, 'rev'), self.rev(self.main)])
        self.assertEqual(self.roots(jd), [self.rev(self.main)])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([(r['p'], r['cells'][0]['owner']) for r in tp['rows']], [('A', 'a1'), ('B', 'b1')])

    def test_a_copy_that_an_asked_report_a_room_tag_or_a_hint_points_at_stays(self):
        asked = self.agent('as1', self.wt1, planned=[plan(self.report(self.wt1, 'C'))])
        self.assertEqual(self.roots(self.judge([asked])), [self.rev(self.wt1)])                   # a report it was asked to save
        tagged = self.agent('tg1', self.wt2, tag=tag(self.rev(self.wt2)))
        self.assertEqual(self.roots(self.judge([tagged])), [self.rev(self.wt2)])                  # a room tag
        pointed = self.page(walked=[self.rev(t) for t in (self.main, self.wt1, self.wt2)], cwd=self.main, hints={self.rev(self.wt1): hint(100.0, calls=('t1',))})
        self.assertEqual(self.roots(pointed), [self.rev(self.wt1)])                               # the orchestrator wrote its guide and round folders there
        both = self.judge([asked, tagged])
        self.assertEqual(sorted(self.roots(both)), sorted(self.rev(t) for t in (self.wt1, self.wt2)))   # each of them stays: nothing is folded into the other

    def test_every_other_debate_is_listed_once_too(self):
        for top in (self.main, self.wt1, self.wt2):
            self.fill(top, 'docs/two')
        walked = [os.path.join(t, 'docs', n) for t in (self.main, self.wt1, self.wt2) for n in ('rev', 'two')]
        a = self.agent('a1', self.main, writes=[wr(self.report(self.main), 150)])
        b = self.agent('b1', self.main, writes=[wr(os.path.join(self.main, 'docs', 'two', 'r1', 'A.md'), 150)])
        jd = self.judge([a, b], walked=walked)
        self.assertEqual(sorted(self.roots(jd)), [os.path.join(self.main, 'docs', 'rev'), os.path.join(self.main, 'docs', 'two')])
        self.assertEqual(sorted(self.roots(self.judge(walked=walked))), [os.path.join(self.main, 'docs', 'rev'), os.path.join(self.main, 'docs', 'two')])      # none has anything of this session: one of each (O10)


class GuideOnly(Family):
    """A folder that holds only an instruction (a guide and a round folder with no report: no seat, no cell) and sits inside the folder of a debate is part of that debate's records. A guide with no round folder is
    no debate folder at all (CONTRACT J9, C25): no unit, so no entry."""

    def setUp(self):
        super().setUp()
        self.rev_main = self.rev(self.main)
        self.guide_only(os.path.join(self.rev_main, 'step2', 'recheck'), '# Recheck\n\nCompare the two.\n')
        self.put(os.path.join(self.rev_main, 'step2', 'recheck', 'diff.md'))

    def guide_only(self, folder, text='# Notes\n'):
        """A folder with a guide and an empty round folder."""
        self.put(os.path.join(folder, 'brief.md'), text)
        os.makedirs(os.path.join(folder, 'r1'), exist_ok=True)
        return folder

    def listed(self, walked=None, agents=()):
        walked = walked or [self.rev_main, os.path.join(self.rev_main, 'step2', 'recheck')]
        jd = self.judge(agents, walked=walked)
        return jd, sorted(self.roots(jd))

    def test_it_is_not_a_debate_of_its_own(self):
        _jd, roots = self.listed()
        self.assertEqual(roots, [self.rev_main])

    def test_but_a_guide_only_folder_alone_in_the_open_is_still_listed(self):
        lone = self.guide_only(os.path.join(self.base, 'main', 'notes', 'idea'), '# An idea\n\nNothing declared.\n')
        _jd, roots = self.listed(walked=[lone])
        self.assertEqual(roots, [lone])

    def test_a_guide_with_no_round_folder_is_no_debate_folder(self):
        bare = self.put(os.path.join(self.base, 'main', 'notes', 'bare', 'brief.md'), '# Bare\n\nNothing declared.\n')
        _jd, roots = self.listed(walked=[os.path.dirname(bare)])
        self.assertEqual(roots, [])

    def test_a_direct_child_topic_that_holds_only_an_instruction_stays_a_title(self):
        bundle = os.path.join(self.main, 'docs', 'bundle')
        self.put(os.path.join(bundle, 'brief.md'), '# The bundle\n')
        self.put(os.path.join(bundle, 't1', 'r1', 'A.md'))
        self.put(os.path.join(bundle, 't1', 'brief.md'), '# T1\n')
        self.guide_only(os.path.join(bundle, 'later'), '# Later\n\nNothing declared.\n')
        jd, roots = self.listed(walked=[os.path.join(bundle, 't1'), os.path.join(bundle, 'later')])
        self.assertEqual(roots, [bundle])
        self.assertEqual(sorted(t['key'] for t in jd.debates[0]['topics']), ['later', 't1'])

    def test_one_that_has_a_seat_or_a_report_is_a_debate_and_one_that_only_declares_them_is_not(self):
        decl = self.guide_only(os.path.join(self.rev_main, 'step2', 'decl'), '# Reviews\n\nReviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n')
        rounds = self.guide_only(os.path.join(self.rev_main, 'step2', 'rounds'), '# Rounds\n')
        self.put(os.path.join(rounds, 'r1', 'A.md'))                                   # a report on the disk
        seat = os.path.join(self.rev_main, 'step2', 'recheck', 'r1', 'sol.md')
        a = self.agent('a1', self.main, planned=[plan(seat)])                          # a report it was asked to save
        _jd, roots = self.listed(walked=[self.rev_main, os.path.join(self.rev_main, 'step2', 'recheck'), decl, rounds], agents=[a])
        self.assertEqual(roots, sorted([self.rev_main, os.path.join(self.rev_main, 'step2', 'recheck'), rounds]))      # the declared names are a sentence: decl shows nothing

    def test_a_guide_only_folder_that_holds_only_guide_only_folders_goes_with_them(self):
        group = os.path.join(self.rev_main, 'step3')
        self.put(os.path.join(group, 'brief.md'), '# Pass 3\n\nNothing declared.\n')
        self.guide_only(os.path.join(group, 'recheck'), '# Recheck pass 3\n')
        jd, roots = self.listed(walked=[self.rev_main, os.path.join(group, 'recheck')])
        self.assertEqual(roots, [self.rev_main])

    def test_a_guide_that_describes_rounds_but_names_nobody_shows_nothing_either(self):
        planned = self.guide_only(os.path.join(self.rev_main, 'step2', 'planned'), '# Planned\n\nThe reports will go to `r1/<id>.md` once the people are chosen.\n')
        _jd, roots = self.listed(walked=[self.rev_main, planned])
        self.assertEqual(roots, [self.rev_main])

    def test_it_is_not_a_debate_when_it_only_names_the_participants_it_waits_for(self):
        # the old rule listed it for the participants a guide names; a sentence makes no cell now (J2), so there is nothing to show
        planned = self.guide_only(os.path.join(self.rev_main, 'step2', 'planned'), '# Planned\n\n- **A — flow** reads the code\n- **B — tests** reads the tests\n\nReports go to `r1/A.md` and `r1/B.md`.\n')
        _jd, roots = self.listed(walked=[self.rev_main, planned])
        self.assertEqual(roots, [self.rev_main])

    def test_a_title_with_a_title_below_it_and_no_debate_around_them_stays_one_entry(self):
        lone = os.path.join(self.base, 'main', 'notes')
        self.put(os.path.join(lone, 'brief.md'), '# Notes\n')
        self.guide_only(os.path.join(lone, 'inner'), '# Inner\n')
        _jd, roots = self.listed(walked=[os.path.join(lone, 'inner')])
        self.assertEqual(len(roots), 1)

    def test_the_copies_of_it_are_gone_with_it(self):
        walked = []
        for t in (self.main, self.wt1, self.wt2):
            self.guide_only(os.path.join(t, 'docs', 'rev', 'step2', 'recheck'), '# Recheck\n')
            walked += [os.path.join(t, 'docs', 'rev'), os.path.join(t, 'docs', 'rev', 'step2', 'recheck')]
        _jd, roots = self.listed(walked=walked)
        self.assertEqual(roots, [self.rev(self.main)])                                              # nothing of this session in any: the main checkout stands for them (O10), and none of the guide-only folders is listed
        a = self.seated(self.main)
        _jd, roots = self.listed(walked=walked, agents=[a])
        self.assertIn(self.rev_main, roots)                                                         # the copy with a seat stays, with the guide-only folder inside it part of its records
        self.assertNotIn(os.path.join(self.rev_main, 'step2', 'recheck'), roots)
        self.assertEqual({os.path.join(self.rev(t)) for t in (self.wt1, self.wt2)} & set(roots), set())      # the copies of the debate are hidden (what a hidden copy holds is a question: see the report)


if __name__ == '__main__':
    unittest.main()
