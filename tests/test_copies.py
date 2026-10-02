"""One folder shown once: the same real folder (a link into it), and the copies of one repository's folder in its worktrees, are one debate on the board; the one that
this session's agents work in stands for them. A bundle's folder that holds only an instruction and no seat is not a debate of its own when a debate encloses it.

    python3 -m unittest tests.test_copies
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402

from board import copies, debates  # noqa: E402

BRIEF = '# Release review\n\nReviewers and their result files are named in the round folders.\n'


def write(path, text='x\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class Family(unittest.TestCase):
    """A repository `main` and two linked worktrees `wt1` and `wt2` of it, each holding the same debate folder `docs/rev` (a round folder with two reports)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(os.path.realpath(self.tmp.name), 'dev')
        self.main = self.checkout('main')
        self.wt1 = self.checkout('wt1', of=self.main)
        self.wt2 = self.checkout('wt2', of=self.main)
        for top in (self.main, self.wt1, self.wt2):
            self.fill(top)
        self.n = 0

    def checkout(self, name, of=None):
        top = os.path.join(self.base, name)
        if of is None:
            os.makedirs(os.path.join(top, '.git'))
        else:
            gd = os.path.join(of, '.git', 'worktrees', name)
            write(os.path.join(gd, 'commondir'), '../..\n')
            write(os.path.join(top, '.git'), 'gitdir: %s\n' % gd)
        return top

    def fill(self, top, rel='docs/rev'):
        write(os.path.join(top, rel, 'brief.md'), BRIEF)
        for p in 'AB':
            write(os.path.join(top, rel, 'r1', p + '.md'), '# %s\n\nfindings\n' % p)

    def agent(self, text, cwd, reads=(), writes=(), last=200.0, ok=True, shell=(), shell_ok=True, ts=150.0):
        self.n += 1
        a = server.Agent('a%016d' % self.n, {})
        a.origin, a.spawn_ts, a.first_ts, a.last_ts, a.cwd = 'subagent', 100.0 + self.n, 100.0 + self.n, last, cwd
        a.spawn_prompt = text
        a.reads = {p: 120.0 for p in reads}
        for p in writes:
            a.writes.append({'ts': ts, 'path': p, 'ok': ok})
        for p in shell:
            a.shell_writes.append({'ts': ts, 'paths': [p], 'id': 'toolu_%s' % p, 'ok': shell_ok})
        return a

    def judge(self, agents=(), walked=None, status='done'):
        walked = [os.path.join(t, 'docs', 'rev') for t in (self.main, self.wt1, self.wt2)] if walked is None else walked
        s = types.SimpleNamespace(agents={a.id: a for a in agents}, _file_cache={}, _head_cache={}, cwd=self.main, walked_units=walked)
        return debates.judge(s, {a.id: status for a in agents})

    def seated(self, top, p='A', cwd=None):
        path = os.path.join(top, 'docs', 'rev', 'r1', p + '.md')
        return self.agent('Read `%s/docs/rev/brief.md`. Write your report to `%s`.' % (top, path), cwd or top, writes=[path])

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
        write(os.path.join(sub, '.git'), 'gitdir: ../main/.git/modules/sub\n')                    # a submodule has no commondir: its own folder is the common one
        self.assertEqual(copies.git_common_dir(sub), gd)

    def test_a_broken_or_missing_pointer_is_no_repository(self):
        odd = os.path.join(self.base, 'odd')
        write(os.path.join(odd, '.git'), 'nonsense\n')
        self.assertIsNone(copies.git_common_dir(odd))
        write(os.path.join(odd, '.git'), 'gitdir: %s/gone\n' % self.base)
        self.assertIsNone(copies.git_common_dir(odd))
        self.assertIsNone(copies.git_common_dir(os.path.join(self.base, 'nowhere')))

    def test_identity_is_the_repository_and_the_place_in_it(self):
        ids = {t: copies.identity(os.path.join(t, 'docs', 'rev')) for t in (self.main, self.wt1, self.wt2)}
        self.assertEqual(len(set(ids.values())), 1)
        self.assertNotEqual(copies.identity(os.path.join(self.main, 'docs', 'other')), ids[self.main])
        other = self.checkout('other')
        self.fill(other)
        self.assertNotEqual(copies.identity(os.path.join(other, 'docs', 'rev')), ids[self.main])           # another repository's folder of the same name is another folder

    def test_a_link_into_the_folder_has_the_identity_of_the_folder(self):
        alias = os.path.join(self.base, 'alias')
        os.symlink(os.path.join(self.main, 'docs', 'rev'), alias)
        self.assertEqual(copies.identity(alias), copies.identity(os.path.join(self.main, 'docs', 'rev')))

    def test_a_folder_in_no_repository_is_known_by_its_real_path(self):
        plain = os.path.join(self.base, 'plain', 'rev')
        os.makedirs(plain)
        alias = os.path.join(self.base, 'plain_alias')
        os.symlink(plain, alias)
        self.assertEqual(copies.identity(alias), copies.identity(plain))
        copy = os.path.join(self.base, 'plain_copy', 'rev')
        os.makedirs(copy)
        self.assertNotEqual(copies.identity(copy), copies.identity(plain))                          # a copy made by hand is a folder of its own


class Fold(Family):
    def test_copies_nobody_works_in_are_one_debate_the_main_checkout_stands_for(self):
        jd = self.judge()
        self.assertEqual(self.roots(jd), [os.path.join(self.main, 'docs', 'rev')])
        self.assertEqual(jd.debates[0]['copies'], 2)

    def test_the_copy_the_session_works_in_stands_for_the_others(self):
        a = self.seated(self.wt2)
        jd = self.judge([a])
        self.assertEqual(self.roots(jd), [os.path.join(self.wt2, 'docs', 'rev')])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([(r['p'], r['cells'][0]['state'], r['cells'][0]['agent']) for r in tp['rows']], [('A', 'done', a.id), ('B', 'done', None)])
        self.assertEqual(jd.debates[0]['copies'], 2)

    def test_a_copy_that_was_only_read_or_written_in_beats_the_main_checkout(self):
        reader = self.agent('Look around.', self.wt1, reads=[os.path.join(self.wt1, 'docs', 'rev', 'r1', 'B.md')])
        self.assertEqual(self.roots(self.judge([reader])), [os.path.join(self.wt1, 'docs', 'rev')])
        writer = self.agent('Look around.', self.wt2, writes=[os.path.join(self.wt2, 'docs', 'rev', 'notes.md')])
        self.assertEqual(self.roots(self.judge([reader, writer])), [os.path.join(self.wt2, 'docs', 'rev')])      # a write is more than a read

    def test_a_read_of_a_copy_that_was_folded_shows_on_the_cell_of_the_one_that_stands_for_it(self):
        a = self.seated(self.wt2)
        reader = self.agent('Read the others.', self.wt1, reads=[os.path.join(self.wt1, 'docs', 'rev', 'r1', 'A.md')])
        reader.auto_tag = 'rv'
        jd = self.judge([a, reader])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([c['readers'] for c in tp['rows'][0]['cells']], [['rv']])

    def test_a_write_that_failed_does_not_make_its_copy_the_one_that_stands_for_the_others(self):
        reader = self.agent('Look around.', self.wt2, reads=[os.path.join(self.wt2, 'docs', 'rev', 'r1', 'B.md')])
        failed = self.agent('Look around.', self.wt1, writes=[os.path.join(self.wt1, 'docs', 'rev', 'notes.md')], ok=False)
        self.assertEqual(self.roots(self.judge([reader, failed])), [os.path.join(self.wt2, 'docs', 'rev')])            # a failed write says nothing: the read decides
        pending = self.agent('Look around.', self.wt1, writes=[os.path.join(self.wt1, 'docs', 'rev', 'notes.md')], ok=None)
        pending.writes[0]['id'] = 'toolu_x'                                                                         # a call whose result is not known
        self.assertEqual(self.roots(self.judge([reader, pending])), [os.path.join(self.wt2, 'docs', 'rev')])
        made = self.agent('Look around.', self.wt1, writes=[os.path.join(self.wt1, 'docs', 'rev', 'notes.md')])
        self.assertEqual(self.roots(self.judge([reader, made])), [os.path.join(self.wt1, 'docs', 'rev')])             # a write that worked beats the read

    def test_a_shell_write_is_believed_only_when_its_call_worked(self):
        reader = self.agent('Look around.', self.wt2, reads=[os.path.join(self.wt2, 'docs', 'rev', 'r1', 'B.md')])
        note = os.path.join(self.wt1, 'docs', 'rev', 'notes.md')
        write(note)
        for shell_ok, root in ((False, self.wt2), (None, self.wt2), (True, self.wt1)):
            sh = self.agent('Look around.', self.wt1, shell=[note], shell_ok=shell_ok)
            self.assertEqual(self.roots(self.judge([reader, sh])), [os.path.join(root, 'docs', 'rev')], shell_ok)

    def test_a_read_of_another_report_with_other_content_is_not_a_read_of_the_one_that_stands_for_it(self):
        a = self.seated(self.wt2)
        write(os.path.join(self.wt2, 'docs', 'rev', 'r1', 'A.md'), '# A\n\nrevised in this worktree\n')              # the main checkout's A.md is another text
        reader = self.agent('Read the others.', self.main, reads=[os.path.join(self.main, 'docs', 'rev', 'r1', 'A.md')])
        reader.auto_tag = 'rv'
        same = self.agent('Read the others.', self.wt1, reads=[os.path.join(self.wt1, 'docs', 'rev', 'r1', 'B.md')])
        same.auto_tag = 'sm'
        jd = self.judge([a, reader, same])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual(jd.debates[0]['root'], os.path.join(self.wt2, 'docs', 'rev'))
        self.assertEqual([c['readers'] for r in tp['rows'] for c in r['cells']], [[], ['sm']])                        # B.md is the same text in both copies; A.md is not

    def test_a_read_of_the_real_file_shows_on_the_cell_that_is_spelled_with_the_link(self):
        link = os.path.join(self.base, 'view')
        os.symlink(os.path.join(self.main, 'docs'), link)
        path = os.path.join(link, 'rev', 'r1', 'A.md')
        a = self.agent('Read `%s/rev/brief.md`. Write your report to `%s`.' % (link, path), link, writes=[path])
        reader = self.agent('Read the others.', self.main, reads=[os.path.join(self.main, 'docs', 'rev', 'r1', 'A.md')])
        reader.auto_tag = 'rv'
        jd = self.judge([a, reader], walked=[os.path.join(self.main, 'docs', 'rev')])
        (tp,) = jd.debates[0]['topics']
        self.assertEqual(jd.debates[0]['root'], os.path.join(link, 'rev'))
        self.assertEqual([c['readers'] for c in tp['rows'][0]['cells']], [['rv']])                                    # the same file under another name: nothing to compare

    def test_two_copies_that_both_have_a_seat_stay_two_debates_and_the_third_is_folded(self):
        a, b = self.seated(self.wt1), self.seated(self.wt2, 'B')
        jd = self.judge([a, b])
        self.assertEqual(sorted(self.roots(jd)), sorted(os.path.join(t, 'docs', 'rev') for t in (self.wt1, self.wt2)))
        self.assertEqual(sorted(d['copies'] for d in jd.debates), [0, 1])             # the main checkout is folded into one of them; no cell is hidden

    def test_the_agents_of_a_folded_copy_work_in_the_debate_that_stands_for_it(self):
        a = self.seated(self.wt2)
        reader = self.agent('Look around.', self.wt1, reads=[os.path.join(self.wt1, 'docs', 'rev', 'r1', 'A.md')])
        jd = self.judge([a, reader])
        self.assertEqual({k: sorted(v) for k, v in jd.members.items()}[reader.id], [os.path.join(self.wt2, 'docs', 'rev')])

    def test_another_repository_and_a_folder_copied_by_hand_are_not_folded(self):
        other = self.checkout('other')
        self.fill(other)
        hand = os.path.join(self.base, 'hand')
        self.fill(hand)
        walked = [os.path.join(t, 'docs', 'rev') for t in (self.main, self.wt1, other, hand)]
        jd = self.judge(walked=walked)
        self.assertEqual(sorted(self.roots(jd)), sorted(os.path.join(t, 'docs', 'rev') for t in (self.main, other, hand)))
        self.assertEqual(sorted(d['copies'] for d in jd.debates), [0, 0, 1])

    def test_a_link_into_the_folder_is_the_folder_and_the_agents_own_spelling_is_kept(self):
        link = os.path.join(self.base, 'view')
        os.symlink(os.path.join(self.main, 'docs'), link)
        path = os.path.join(link, 'rev', 'r1', 'A.md')
        a = self.agent('Read `%s/rev/brief.md`. Write your report to `%s`.' % (link, path), link, writes=[path])
        jd = self.judge([a], walked=[os.path.join(self.main, 'docs', 'rev')])
        self.assertEqual(self.roots(jd), [os.path.join(link, 'rev')])                         # not folded toward the real path: the cells are keyed by what the agents wrote
        (tp,) = jd.debates[0]['topics']
        self.assertEqual([(r['p'], r['cells'][0]['agent']) for r in tp['rows']], [('A', a.id), ('B', None)])
        self.assertEqual(jd.debates[0]['copies'], 1)

    def test_every_other_debate_is_listed_once_too(self):
        for top in (self.main, self.wt1, self.wt2):
            self.fill(top, 'docs/two')
        walked = [os.path.join(t, 'docs', n) for t in (self.main, self.wt1, self.wt2) for n in ('rev', 'two')]
        jd = self.judge(walked=walked)
        self.assertEqual(sorted(self.roots(jd)), [os.path.join(self.main, 'docs', 'rev'), os.path.join(self.main, 'docs', 'two')])


class SameFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.realpath(self.tmp.name)

    def test_one_file_under_two_names_or_two_files_with_the_same_text(self):
        a, b, c = (write(os.path.join(self.base, n), '# A\n\nfindings\n') for n in ('a.md', 'b.md', 'c.md'))
        os.symlink(a, os.path.join(self.base, 'link.md'))
        self.assertTrue(copies.same_file(a, a))
        self.assertTrue(copies.same_file(a, os.path.join(self.base, 'link.md')))
        self.assertTrue(copies.same_file(a, b))
        write(c, '# A\n\nfindings, revised\n')
        self.assertFalse(copies.same_file(a, c))
        write(c, '# A\n\nfindingz\n')                                                    # the same size, another text
        self.assertFalse(copies.same_file(a, c))

    def test_a_file_that_is_not_there_or_not_a_regular_file_is_nobody_elses(self):
        a = write(os.path.join(self.base, 'a.md'))
        self.assertFalse(copies.same_file(a, os.path.join(self.base, 'gone.md')))
        self.assertFalse(copies.same_file(os.path.join(self.base, 'gone.md'), a))
        os.makedirs(os.path.join(self.base, 'dir'))
        self.assertFalse(copies.same_file(a, os.path.join(self.base, 'dir')))
        self.assertFalse(copies.same_file(os.path.join(self.base, 'dir'), os.path.join(self.base, 'dir')))     # two names for a folder are no report

    def test_a_secret_or_hidden_file_is_never_opened_to_be_compared(self):
        a = write(os.path.join(self.base, 'token_notes.md'), 'x\n')
        b = write(os.path.join(self.base, 'copy', 'token_notes.md'), 'x\n')
        self.assertFalse(copies.same_file(a, b))
        h1, h2 = write(os.path.join(self.base, '.h', 'a.md'), 'x\n'), write(os.path.join(self.base, 'o', '.h', 'a.md'), 'x\n')
        self.assertFalse(copies.same_file(h1, h2))

    def test_a_file_too_big_to_compare_is_not_taken_for_the_same(self):
        a, b = (write(os.path.join(self.base, n), 'x' * (copies.COMPARE_MAX + 1)) for n in ('a.md', 'b.md'))
        self.assertFalse(copies.same_file(a, b))
        self.assertTrue(copies.same_file(a, a))                                                  # the same file needs no reading


class GuideOnly(Family):
    """A folder that holds only an instruction (no seat, no cell, nobody declared) and sits inside the folder of a debate is part of that debate's records."""

    def setUp(self):
        super().setUp()
        self.rev = os.path.join(self.main, 'docs', 'rev')
        write(os.path.join(self.rev, 'step2', 'recheck', 'brief.md'), '# Recheck\n\nCompare the two.\n')
        write(os.path.join(self.rev, 'step2', 'recheck', 'diff.md'))

    def listed(self, walked=None, agents=()):
        walked = walked or [self.rev, os.path.join(self.rev, 'step2', 'recheck')]
        jd = self.judge(agents, walked=walked)
        return jd, sorted(self.roots(jd))

    def test_it_is_not_a_debate_of_its_own(self):
        _jd, roots = self.listed()
        self.assertEqual(roots, [self.rev])

    def test_but_a_guide_only_folder_alone_in_the_open_is_still_listed(self):
        lone = os.path.join(self.base, 'main', 'notes', 'idea')
        write(os.path.join(lone, 'brief.md'), '# An idea\n\nNothing declared.\n')
        _jd, roots = self.listed(walked=[lone])
        self.assertEqual(roots, [lone])

    def test_a_direct_child_topic_that_holds_only_an_instruction_stays_a_title(self):
        bundle = os.path.join(self.main, 'docs', 'bundle')
        write(os.path.join(bundle, 'brief.md'), '# The bundle\n')
        write(os.path.join(bundle, 't1', 'r1', 'A.md'))
        write(os.path.join(bundle, 't1', 'brief.md'), '# T1\n')
        write(os.path.join(bundle, 'later', 'brief.md'), '# Later\n\nNothing declared.\n')
        jd, roots = self.listed(walked=[bundle, os.path.join(bundle, 't1'), os.path.join(bundle, 'later')])
        self.assertEqual(roots, [bundle])
        self.assertEqual(sorted(t['key'] for t in jd.debates[0]['topics']), ['later', 't1'])

    def test_one_that_declares_its_reports_or_has_a_seat_or_a_round_folder_is_a_debate(self):
        decl = os.path.join(self.rev, 'step2', 'decl')
        write(os.path.join(decl, 'brief.md'), '# Reviews\n\nReviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n')
        rounds = os.path.join(self.rev, 'step2', 'rounds')
        write(os.path.join(rounds, 'r1', 'A.md'))
        write(os.path.join(rounds, 'brief.md'), '# Rounds\n')
        seat = os.path.join(self.rev, 'step2', 'recheck', 'sol.md')
        a = self.agent('Read `%s/brief.md`. Write your result to `%s`.' % (os.path.dirname(seat), seat), self.main, writes=[seat])
        write(os.path.join(os.path.dirname(seat), 'brief.md'), '# Recheck\n\nReviewers and their result files (declared): `sol.md` (reviewer sol).\n')
        _jd, roots = self.listed(walked=[self.rev, os.path.join(self.rev, 'step2', 'recheck'), decl, rounds], agents=[a])
        self.assertEqual(roots, sorted([self.rev, os.path.join(self.rev, 'step2', 'recheck'), decl, rounds]))

    def test_a_guide_only_folder_that_holds_only_guide_only_folders_goes_with_them(self):
        group = os.path.join(self.rev, 'step3')
        write(os.path.join(group, 'brief.md'), '# Pass 3\n\nNothing declared.\n')
        write(os.path.join(group, 'recheck', 'brief.md'), '# Recheck pass 3\n')
        jd, roots = self.listed(walked=[self.rev, group, os.path.join(group, 'recheck')])
        self.assertEqual(roots, [self.rev])

    def test_a_guide_that_describes_rounds_but_names_nobody_shows_nothing_either(self):
        planned = os.path.join(self.rev, 'step2', 'planned')
        write(os.path.join(planned, 'brief.md'), '# Planned\n\nThe reports will go to `r1/<id>.md` once the people are chosen.\n')
        _jd, roots = self.listed(walked=[self.rev, planned])
        self.assertEqual(roots, [self.rev])

    def test_it_is_still_a_debate_when_it_names_the_participants_it_waits_for(self):
        planned = os.path.join(self.rev, 'step2', 'planned')
        write(os.path.join(planned, 'brief.md'), '# Planned\n\n- **A — flow** reads the code\n- **B — tests** reads the tests\n\nReports go to `r1/A.md` and `r1/B.md`.\n')
        _jd, roots = self.listed(walked=[self.rev, planned])
        self.assertEqual(roots, sorted([self.rev, planned]))

    def test_a_title_with_a_title_below_it_and_no_debate_around_them_stays_one_entry(self):
        lone = os.path.join(self.base, 'main', 'notes')
        write(os.path.join(lone, 'brief.md'), '# Notes\n')
        write(os.path.join(lone, 'inner', 'brief.md'), '# Inner\n')
        _jd, roots = self.listed(walked=[lone, os.path.join(lone, 'inner')])
        self.assertEqual(len(roots), 1)

    def test_the_copies_of_it_are_gone_with_it(self):
        walked = []
        for t in (self.main, self.wt1, self.wt2):
            write(os.path.join(t, 'docs', 'rev', 'step2', 'recheck', 'brief.md'), '# Recheck\n')
            walked += [os.path.join(t, 'docs', 'rev'), os.path.join(t, 'docs', 'rev', 'step2', 'recheck')]
        _jd, roots = self.listed(walked=walked)
        self.assertEqual(roots, [self.rev])


if __name__ == '__main__':
    unittest.main()
