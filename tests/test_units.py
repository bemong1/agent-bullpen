"""The pure parts of the debate judgment (board/units.py): the debate folders on disk, who holds which cell of them, what a cell shows.

The judgment reads no sentence. A folder is a debate when it has an exact round folder; a cell is a file `<unit>/<round folder>/<name>.md`, and what stands in it is decided by what agents DID:
the write events the collectors kept (`wr`), the files a launch command or a room tag asked them to save (`plan`, `tag`), their reads, the commands they ran. Nothing here depends on a
session, on the link index or on a process: agents are hand-built `AgentFacts` over a temporary folder (tests/judge_support.py), and the views of the board (`debates.debates`) are built on
agents whose events are given directly. What the instruction of an agent said cannot be given at all: that is the point. The 83 cases of the contract are in tests/test_contract_cases.py.

    python3 -m unittest tests.test_units
"""
import dataclasses
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
from judge_support import Fixture, agent, plan, rd, wr  # noqa: E402

from board import debates, units as U  # noqa: E402

GUIDE = '# Gate review\n\n**A — flow**\n**B — gate**\n**C — github**\n'      # what a guide says is never read: a role row makes no seat, no cell, no unit


def view_agent(aid, writes=(), planned=(), reads=(), start=100.0, last=0.0):
    """A real session agent (what the board builds its view from) with the events given: no record to read, nothing it was told."""
    a = server.Agent(aid, {})
    a.origin, a.spawn_ts, a.first_ts, a.last_ts, a.run, a.run_start = 'subagent', start, start, last, 1, start
    for w in writes:
        a.ev.add_write(dataclasses.replace(w, agent=aid))
    for q in planned:
        a.ev.add_planned(dataclasses.replace(q, agent=aid))
    for r in reads:
        a.ev.add_read(dataclasses.replace(r, agent=aid))
    return a


class Tree(Fixture):
    """A repository with one debate folder `docs/rev` (guide, rounds r1 and r2)."""

    def setUp(self):
        super().setUp()
        self.repo = self.p('repo')
        self.unit = self.p('repo', 'docs', 'rev')
        self.file('repo/docs/rev/brief.md', GUIDE)
        self.dirs('repo/docs/rev/r1', 'repo/docs/rev/r2')

    def r(self, n, stem, spelled=None):
        return os.path.join(self.unit, spelled or 'r%d' % n, stem + '.md')

    def report(self, n, stem, spelled=None, text='x\n', mtime=None):
        """A report file of the unit on disk (its path)."""
        return self.file(os.path.relpath(self.r(n, stem, spelled), self.root), text, mtime)

    def codes(self, jd, agent=None):
        return sorted(d['code'] for d in jd.diag if agent is None or d['agent'] == agent)

    def cell(self, jd, rdir, stem, unit=None):
        return jd.cells.get((unit or self.unit, rdir, stem))

    def owned(self, jd, aid):
        """(round, name) of the cells an agent owns, in this unit."""
        return sorted((c.round, c.stem) for c in jd.cells.values() if c.unit == self.unit and c.owner == aid)

    def periods(self, jd, unit=None):
        return [(x.agent, x.start, x.end) for x in jd.assignments if x.unit == (unit or self.unit)]


class Names(unittest.TestCase):
    def test_round_folder_names(self):
        self.assertEqual([U.round_of(x) for x in ('r1', 'r01', 'round1', 'round12', 'r10')], [1, 1, 1, 12, 10])
        self.assertEqual([U.round_of(x) for x in ('r1x', 'r', 'round', 'R1', 'r_1', 'r1_run.log', 'result1')], [None] * 7)


class FileRounds(Fixture):
    def test_round_folder_reports_keep_the_original_unit_and_empty_round(self):
        x = self.file('talk/r1/r1_a.md', mtime=200)
        y = self.file('talk/r1/r1_b.md', mtime=210)
        final = self.file('talk/r1/summary.md', mtime=300)
        self.dirs('talk/r2')
        jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)]), orch=[wr(final, 300)])
        self.assertEqual(set(jd.listed), {self.p('talk')})
        self.assertEqual({(rd, st) for u, rd, st in jd.cells}, {('r1', 'r1_a'), ('r1', 'r1_b'), ('r1', 'summary')})
        self.assertFalse(jd.finals[self.p('talk')].confirmed)
        self.assertEqual(jd.finals[self.p('talk')].why, ['none', 'empty_round'])

    def test_file_round_names_keep_the_bundle_and_live_participant(self):
        self.file('research/brief.md', mtime=50)
        report = self.file('research/t1/r1/A.md', mtime=150)
        x = self.file('research/round1_x.md', mtime=200)
        y = self.file('research/round1_y.md', mtime=210)
        final = self.file('research/decision.md', mtime=300)
        jd = self.assign(agent('C', writes=[wr(report, 150)], status='running'),
                         agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)]), orch=[wr(final, 300)])
        self.assertEqual(jd.roots, {self.p('research'): [self.p('research/t1')]})
        self.assertFalse(jd.finals[self.p('research')].confirmed)
        self.assertEqual(jd.finals[self.p('research')].why, ['open_cell', 'live_participant'])

    def test_nested_file_rounds_register_the_child_before_the_parent(self):
        self.file('research/brief.md', mtime=50)
        ws = [self.file('research/' + sub + 'round1_' + seat + '.md', mtime=ts)
              for sub in ('', 't1/') for seat, ts in (('x', 200), ('y', 210))]
        cat = U.Catalog()
        jd = self.assign(agent('A', writes=[wr(ws[0], 200), wr(ws[2], 220)]),
                         agent('B', writes=[wr(ws[1], 210), wr(ws[3], 230)]), catalog=cat)
        self.assertIsNone(cat.unit_at(self.p('research')))
        self.assertEqual(jd.roots, {self.p('research'): [self.p('research/t1')]})
        self.assertEqual({u for u, rd, st in jd.cells}, {self.p('research/t1')})

    def test_macos_scratch_roots_are_excluded_but_subfolders_can_qualify(self):
        for rel in ('private/tmp', 'private/var/folders/synthetic/T'):
            with self.subTest(root=rel):
                x, y = [self.file(rel + '/round1_' + s + '.md', mtime=200 + i * 10) for i, s in enumerate('xy')]
                sub_x, sub_y = [self.file(rel + '/talk/round1_' + s + '.md', mtime=200 + i * 10) for i, s in enumerate('xy')]
                with mock.patch.object(U, 'SCRATCH_DIRS', (self.p(rel) + os.sep,)):
                    self.assertEqual(self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)])).listed, {})
                    jd = self.assign(agent('A', writes=[wr(sub_x, 200)]), agent('B', writes=[wr(sub_y, 210)]))
                    self.assertEqual(set(jd.listed), {self.p(rel, 'talk')})

    def test_scratch_root_aliases_are_checked_by_realpath(self):
        x = self.file('private/tmp/round1_x.md', mtime=200)
        y = self.file('private/tmp/round1_y.md', mtime=210)
        os.symlink(self.p('private/tmp'), self.p('tmp'))
        with mock.patch.object(U, 'SCRATCH_DIRS', (self.p('tmp') + os.sep,)):
            jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)]))
        self.assertEqual(jd.listed, {})

    def test_updates_and_appends_cannot_qualify_a_second_author(self):
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.md', mtime=210)
        for kind in ('update', 'append'):
            with self.subTest(kind=kind):
                jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210, kind=kind)]))
                self.assertEqual(jd.listed, {})

    def test_disk_only_seats_cannot_qualify_the_folder(self):
        x = self.file('talk/round1_x.md', mtime=200)
        self.file('talk/round1_old.md', mtime=50)
        jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(x, 210)]))
        self.assertEqual(jd.listed, {})

    def test_two_authors_in_separate_one_seat_rounds_do_not_qualify(self):
        x = self.file('talk/r1_auth.md', mtime=200)
        y = self.file('talk/r2_billing.md', mtime=210)
        jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)]))
        self.assertEqual(jd.listed, {})

    def test_repository_top_and_docs_are_excluded(self):
        self.dirs('repo/.git')
        for folder in ('repo', 'repo/docs', 'repo/doc'):
            with self.subTest(folder=folder):
                x, y = [self.file(folder + '/round1_' + s + '.md', mtime=200 + i * 10) for i, s in enumerate('xy')]
                jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)]))
                self.assertEqual(jd.listed, {})

    def test_case_only_seat_names_do_not_qualify(self):
        x = self.file('talk/round1_a.md', mtime=200)
        y = self.file('talk/Round1_A.md', mtime=210)
        jd = self.assign(agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)]))
        self.assertEqual(jd.listed, {})

    def test_authoring_kinds_qualify_and_seats_are_lowercase(self):
        for kind in ('create', 'replace', 'unknown'):
            with self.subTest(kind=kind):
                x = self.file('talk/round1_X.md', mtime=200)
                y = self.file('talk/round1_Y.md', mtime=210)
                jd = self.assign(agent('A', writes=[wr(x, 200, kind=kind)]), agent('B', writes=[wr(y, 210, kind=kind)]))
                self.assertEqual({st: (c.owner, c.path) for (u, rd, st), c in jd.cells.items()}, {'x': ('A', x), 'y': ('B', y)})

    def test_an_unmet_file_round_request_holds_the_final(self):
        from judge_support import tag
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.md', mtime=210)
        final = self.file('talk/decision.md', mtime=300)
        future = self.p('talk', 'round2_x.md')
        b = agent('B', writes=[wr(y, 210)])
        for source in ('planned', 'tag'):
            with self.subTest(source=source):
                a = agent('A', writes=[wr(x, 200)], run=2, run_start=250,
                          planned=[plan(future, ts=250, run=2)] if source == 'planned' else (),
                          tag=tag(self.p('talk'), 'round2_x.md', run=2) if source == 'tag' else None)
                jd = self.assign(a, b, orch=[wr(final, 300)])
                c = jd.cells[(self.p('talk'), 'round2', 'x')]
                self.assertEqual((c.path, c.owner, c.agent, c.state, c.evidence), (future, None, 'A', 'missing', source))
                self.assertFalse(jd.finals[self.p('talk')].confirmed)
                self.assertEqual(jd.finals[self.p('talk')].why, ['open_cell'])

    def test_qualification_does_not_leak_between_sessions_on_one_catalog(self):
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.md', mtime=210)
        cat = U.Catalog()
        a, b = agent('A', writes=[wr(x, 200)]), agent('B', writes=[wr(y, 210)])
        first = self.assign(a, b, catalog=cat)
        self.assertEqual(set(first.listed), {self.p('talk')})
        self.assertEqual(self.assign(a, catalog=cat).listed, {})

    def test_a_masked_shell_write_must_pass_the_save_check_to_qualify(self):
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/r1_y.md', mtime=210)
        a = agent('A', writes=[wr(x, 200)])
        b = agent('B', writes=[wr(y, 210, evidence='shell', proof='window', span=(209, 211))])
        self.assertEqual(set(self.assign(a, b).listed), {self.p('talk')})
        self.file('talk/r1_y.md', mtime=250)
        self.assertEqual(self.assign(a, b).listed, {})

    def test_file_rounds_keep_owner_ties_and_current_round_states(self):
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.md', mtime=210)
        later = self.file('talk/round2_y.md', mtime=300)
        a = agent('A', writes=[wr(x, 200)])
        b = agent('B', writes=[wr(x, 200.5), wr(y, 210), wr(later, 300)], status='running')
        jd = self.assign(a, b)
        self.assertIsNone(jd.cells[(self.p('talk'), 'round1', 'x')].owner)
        self.assertEqual(jd.cells[(self.p('talk'), 'round1', 'y')].state, 'done')
        self.assertEqual(jd.cells[(self.p('talk'), 'round2', 'y')].state, 'draft')
        self.assertIn('open_cell', jd.finals[self.p('talk')].why)


class FileRoundCollisions(Fixture):
    def collision(self, names, running=True):
        paths = [self.file('talk/' + name, mtime=200 + i * 10) for i, name in enumerate(names)]
        final = self.file('talk/summary.md', mtime=300)
        authors = [agent(aid, writes=[wr(path, 200 + i * 10)], status='running' if running and i == 0 else 'done')
                   for i, (aid, path) in enumerate(zip('ABC', paths))]
        return paths, authors, final

    def test_case_and_extension_collisions_preserve_all_paths_and_hold_the_seat(self):
        for names in (('round1_A.md', 'round1_a.md', 'round1_b.md'),
                      ('round1_a.MD', 'round1_a.md', 'round1_b.md')):
            with self.subTest(names=names):
                paths, authors, final = self.collision(names)
                jd = self.assign(*authors, orch=[wr(final, 300)])
                by_path = {c.path: c for c in jd.cells.values()}
                self.assertEqual(set(by_path), set(paths))
                for path in paths[:2]:
                    self.assertEqual((by_path[path].stem, by_path[path].owner, by_path[path].agent), ('a', None, None))
                self.assertEqual(by_path[paths[2]].owner, 'C')
                self.assertTrue(any(d['code'] in ('alias_collision', 'seat_tie_held') for d in jd.diag))
                self.assertEqual(jd.finals[self.p('talk')].why, ['open_cell', 'live_participant'])
                self.assertFalse(jd.closable[self.p('talk')])
                self.assertEqual(jd.worked, {a: {self.p('talk')} for a in 'ABC'})
            for path in paths:
                os.unlink(path)

    def test_prefix_aliases_hold_the_seat_after_every_author_finishes(self):
        paths, authors, final = self.collision(('r1_a.md', 'round1_a.md', 'round1_b.md'), running=False)
        jd = self.assign(*authors, orch=[wr(final, 300)])
        self.assertEqual({c.path for c in jd.cells.values()}, set(paths))
        self.assertTrue(all(c.owner is None for c in jd.cells.values() if c.stem == 'a'))
        self.assertEqual(jd.finals[self.p('talk')].why, ['open_cell'])
        self.assertFalse(jd.closable[self.p('talk')])

    def test_colliding_reports_do_not_crash_the_debate_table(self):
        paths, authors, final = self.collision(('round1_A.md', 'round1_a.md', 'round1_b.md'))
        result = self.page(*authors, orch=[wr(final, 300)])
        topic = result.debates[0]['topics'][0]
        row = next(r for r in topic['rows'] if r['p'] == 'a')
        self.assertIsNone(row['cells'][0]['owner'])
        self.assertIsNone(row['cells'][0]['agent'])
        self.assertTrue(set(paths) <= {d['path'] for d in topic['docs']})
        self.assertEqual(topic['final']['why'], ['open_cell', 'live_participant'])

    def test_uppercase_final_competes_with_lowercase_and_can_close_alone(self):
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.MD', mtime=210)
        upper = self.file('talk/final_A.MD', mtime=300)
        lower = self.file('talk/final_B.md', mtime=310)
        a, b = agent('A', writes=[wr(x, 200), wr(upper, 300)]), agent('B', writes=[wr(y, 210), wr(lower, 310)])
        jd = self.assign(a, b)
        self.assertEqual(jd.finals[self.p('talk')].why, ['several'])
        self.assertEqual(set(jd.finals[self.p('talk')].candidates), {upper, lower})
        os.unlink(lower)
        jd = self.assign(a, agent('B', writes=[wr(y, 210)]))
        self.assertEqual((jd.finals[self.p('talk')].confirmed, jd.finals[self.p('talk')].path), (True, upper))

    def test_an_uppercase_failed_attempt_still_competes_when_the_file_is_absent(self):
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.md', mtime=210)
        lower = self.file('talk/final_B.md', mtime=310)
        jd = self.assign(agent('A', writes=[wr(x, 200), wr(self.p('talk/final_A.MD'), 300, ok=False)]),
                         agent('B', writes=[wr(y, 210), wr(lower, 310)]))
        self.assertEqual(jd.finals[self.p('talk')].why, ['several'])

    def test_extension_collisions_in_round_folders_preserve_both_reports(self):
        paths = [self.file('talk/r1/' + name, mtime=200 + i * 10) for i, name in enumerate(('A.MD', 'A.md', 'B.md'))]
        final = self.file('talk/summary.md', mtime=300)
        jd = self.assign(*(agent(a, writes=[wr(p, 200 + i * 10)]) for i, (a, p) in enumerate(zip('ABC', paths))), orch=[wr(final, 300)])
        self.assertEqual({c.path for c in jd.cells.values()}, set(paths))
        self.assertTrue(all(c.owner is None for c in jd.cells.values() if c.stem == 'A'))
        self.assertEqual(jd.finals[self.p('talk')].why, ['open_cell'])

    def test_uppercase_tagged_report_uses_the_named_file(self):
        from judge_support import tag
        x = self.file('talk/round1_x.md', mtime=200)
        y = self.file('talk/round1_y.md', mtime=210)
        later = self.file('talk/round2_x.MD', mtime=220)
        final = self.file('talk/summary.md', mtime=300)
        a = agent('A', writes=[wr(x, 200), wr(later, 220)], tag=tag(self.p('talk'), 'round2_x.MD', run=1))
        jd = self.assign(a, agent('B', writes=[wr(y, 210)]), orch=[wr(final, 300)])
        self.assertEqual({c.path for c in jd.cells.values()}, {x, y, later})
        self.assertEqual(jd.cells[(self.p('talk'), 'round2', 'x')].owner, 'A')
        self.assertTrue(jd.finals[self.p('talk')].confirmed)

    def test_extension_collisions_in_a_tagged_room_preserve_both_reports(self):
        from judge_support import tag
        paths = [self.file('talk/' + name, mtime=200 + i * 10) for i, name in enumerate(('A.MD', 'A.md', 'B.md'))]
        final = self.file('talk/summary.md', mtime=300)
        jd = self.assign(*(agent(a, writes=[wr(p, 200 + i * 10)], tag=tag(self.p('talk')))
                           for i, (a, p) in enumerate(zip('ABC', paths))), orch=[wr(final, 300)])
        self.assertEqual({c.path for c in jd.cells.values()}, set(paths))
        self.assertTrue(all(c.owner is None for c in jd.cells.values() if c.stem == 'A'))
        self.assertEqual(jd.finals[self.p('talk')].why, ['open_cell'])


class ReadUnit(Tree):
    """A debate folder is read from the disk: it has an exact round folder. Its guide is the brief next to it; nothing in a text of it counts."""

    def test_brief_with_rounds(self):
        u = U.Catalog().unit_at(self.unit)
        self.assertEqual((sorted(u.rounds), u.rounds[1], os.path.basename(u.brief)), ([1, 2], ['r1'], 'brief.md'))

    def test_brief_alone_is_no_unit(self):
        """A guide, whatever it says (reviewers and their files, participant rows, the findings of an editing job, a path of a round), is a unit only with a round folder next to it."""
        guides = {'flat': '# Release review\n\nRead `README.md` and `THIRD_PARTY.md`; the report format is below.\nOne result file only.\n',
                  'declared': '# Review\n\nReviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n',
                  'fresh': GUIDE,
                  'edit4': '# Edit pass 4\n\nFix exactly these findings, in this order.\n\n- **C-12**\n- **X-1**: invalidate the flag.\n\n| item | finding |\n|---|---|\n| 1 | **X-2** |\n',
                  'later': '# Later\n\nThe reports will go to `r1/<id>.md` once the people are chosen.\n'}
        for name, text in guides.items():
            self.file('docs/%s/brief.md' % name, text)
            self.assertIsNone(U.Catalog().unit_at(self.p('docs', name)), name)
        self.dirs('docs/fresh/r1')                                       # the round folder is the whole difference
        self.assertEqual(sorted(U.Catalog().unit_at(self.p('docs', 'fresh')).rounds), [1])

    def test_a_guide_with_participant_rows_and_no_round_folder_is_no_debate(self):
        fresh = self.p('docs', 'fresh')
        self.file('docs/fresh/brief.md', GUIDE)
        jd = self.assign(walked=[fresh])
        self.assertEqual((jd.cells, jd.listed, jd.roots), ({}, {}, {}))          # no row of it makes a cell, a unit or a place on the list

    def test_readme_and_index_confirm_nothing_alone(self):
        for name in ('README.md', 'index.md'):
            d = self.p('docs', name[:-3])
            self.file('docs/%s/%s' % (name[:-3], name), GUIDE + '\nReports go to r1/<id>.md\n')
            self.assertIsNone(U.Catalog().unit_at(d), name)
            self.dirs('docs/%s/r1' % name[:-3])
            u = U.Catalog().unit_at(d)
            self.assertEqual(os.path.basename(u.brief), name)                  # with a round folder next to it, it is the guide

    def test_names_that_are_not_debates(self):
        for rel, make in (('edit', lambda d: self.file(d + '/edit_brief.md', GUIDE)), ('runs', lambda d: self.dirs(d + '/r1x')), ('logs', lambda d: self.file(d + '/r1_run.log')),
                          ('empty', lambda d: self.dirs(d)), ('plain', lambda d: self.file(d + '/r1'))):                  # (a file named r1 is no round folder)
            make('docs/%s' % rel)
            self.assertIsNone(U.Catalog().unit_at(self.p('docs', rel)), rel)
        self.assertIsNone(U.Catalog().unit_at(self.p('docs', 'nowhere')))

    def test_the_topics_of_a_shared_guide_are_the_folders_with_a_round_folder(self):
        base = self.p('docs', 'review')
        self.file('docs/review/brief.md', '# Review\n\n| Topic | Folder |\n|---|---|\n| Gate | `t1/` |\n')
        self.file('docs/review/t1/brief.md', GUIDE)
        self.dirs('docs/review/t1/r1')
        self.file('docs/review/edit4/brief.md', GUIDE)                  # an editing job: a guide and a folder of edits, no round folder
        self.dirs('docs/review/edit4/edits')
        self.file('docs/review/notes/x.md')
        cat = U.Catalog()
        self.assertEqual([os.path.basename(u.path) for u in cat.children(base)], ['t1'])
        self.assertIsNone(cat.unit_at(base))                             # the shared guide holds no round folder: it is no unit, and the root of the topics below it

    def test_two_spellings_of_one_round_stay_two_folders(self):
        self.dirs('repo/docs/rev/r01', 'repo/docs/rev/round1')
        u = U.Catalog().unit_at(self.unit)
        self.assertEqual(u.rounds[1], ['r01', 'r1', 'round1'])
        jd = self.assign(agent('a1', writes=[wr(self.report(1, 'B'), 210)], status='running'))
        self.assertIn(('alias_collision', 'r01,r1,round1'), [(d['code'], d['detail']) for d in jd.diag])

    def test_catalog_follows_the_folder(self):
        cat = U.Catalog()
        cat.begin()
        self.assertEqual(sorted(cat.unit_at(self.unit).rounds), [1, 2])
        os.makedirs(os.path.join(self.unit, 'r3'))
        cat.begin()
        self.assertEqual(sorted(cat.unit_at(self.unit).rounds), [1, 2, 3])
        self.assertIsNone(cat.unit_at(os.path.join(self.repo, 'docs')))

    def test_home_is_never_a_debate_and_neither_is_what_holds_it(self):
        self.dirs('repo/r1')
        self.file('repo/brief.md', GUIDE)
        with patched(HOME=self.repo):
            self.assertTrue(U.too_broad(self.repo))
            self.assertTrue(U.too_broad(os.path.dirname(self.repo)))
            self.assertIsNone(U.Catalog().unit_at(self.repo))                                    # HOME is never a debate, whatever it holds
            self.assertIsNone(U.Catalog().unit_at(os.path.dirname(self.repo)))
            self.assertIsNotNone(U.Catalog().unit_at(self.unit))                                 # a debate below it is
        self.assertTrue(all(U.too_broad(x) for x in ('/', '/tmp', '/var/tmp')))
        self.assertFalse(U.too_broad(self.unit))

    def test_a_flat_folder_is_a_debate_once_a_round_folder_is_made_in_it(self):
        flat = self.p('docs', 'flat')
        self.file('docs/flat/brief.md', '# Review\n\nNothing declared here.\n')
        self.assertIsNone(U.Catalog().unit_at(flat))
        path = self.file('docs/flat/r1/B.md')                                            # an agent wrote a report in a round folder of it
        jd = self.assign(agent('a1', writes=[wr(path, 210)], status='running'))
        self.assertEqual(jd.listed, {flat: 'cell'})
        self.assertEqual([(c.round, c.stem, c.owner) for c in jd.cells.values()], [(1, 'B', 'a1')])


class Walk(Tree):
    def make_repo(self):
        os.makedirs(os.path.join(self.repo, '.git'))
        for rel in ('node_modules/pkg', '.hidden/x', 'build/out', 'docs/deep/er/est'):
            self.file('repo/%s/brief.md' % rel, GUIDE)
            self.dirs('repo/%s/r1' % rel)                                # each of them would be a debate if it were walked

    def test_finds_the_debates_of_a_repository_and_skips_what_is_not_one(self):
        self.make_repo()
        self.file('repo/docs/guide_only/brief.md', GUIDE)                  # a guide with no round folder is no debate
        found, capped = U.walk_repo(self.repo, U.Catalog())
        self.assertEqual(sorted(os.path.relpath(u.path, self.repo) for u in found), ['docs/deep/er/est', 'docs/rev'])
        self.assertFalse(capped)

    def test_budget_ends_the_walk_with_capped(self):
        self.make_repo()
        found, capped = U.walk_repo(self.repo, U.Catalog(), max_dirs=3)
        self.assertTrue(capped)
        found, capped = U.walk_repo(self.repo, U.Catalog(), max_depth=1)
        self.assertEqual([os.path.relpath(u.path, self.repo) for u in found], [])                # docs/rev is two folders down
        self.assertFalse(capped)

    def test_broad_folders_are_never_walked(self):
        home = os.path.join(self.root, 'home', 'me')
        self.file('home/me/Dev/x/brief.md', GUIDE)
        self.dirs('home/me/Dev/x/r1')
        with patched(HOME=home):
            for top in (home, os.path.dirname(home), os.path.join(home, '..'), '/', '/tmp'):
                self.assertEqual(U.walk_repo(top, U.Catalog()), ([], False), top)
            os.makedirs(os.path.join(home, 'Dev', 'a', '.git'))
            os.makedirs(os.path.join(home, 'Dev', 'b', '.git'))
            self.assertEqual(U.walk_repo(os.path.join(home, 'Dev'), U.Catalog()), ([], False))   # the folder that keeps the repositories is not a repository
            found, _ = U.walk_repo(os.path.join(home, 'Dev', 'a'), U.Catalog())
            self.assertEqual(found, [])                                                          # a repository itself is walked

    def test_repo_top(self):
        os.makedirs(os.path.join(self.repo, '.git'))
        self.assertEqual(U.repo_top(os.path.join(self.unit, 'r1')), self.repo)
        with patched(HOME=self.repo):
            self.assertIsNone(U.repo_top(os.path.join(self.unit, 'r1')))                         # a top that is HOME is no repository top for a path


class Seats(Tree):
    """A seat comes from what an agent did: the first sure write that made the whole file owns the cell, the others are editors; a reader, an agent whose write failed or whose result is not
    known, or one that only had a file to save and did not, holds none. Two sure writes within a second hold the cell, nobody is made the owner by guessing."""

    def pair(self, a_status, a_last=150.0, a_write=140.0, b_write=250.0, b_asked=True, b_status='running'):
        """a wrote r1/B.md and is over (or not); b, started later, was asked to save the same file (`planned`) and wrote it."""
        p = self.report(1, 'B')
        a = agent('a', writes=[wr(p, a_write)], status=a_status, start=100.0, last=a_last)
        b = agent('b', writes=[wr(p, b_write)], planned=[plan(p, ts=200.0)] if b_asked else [], status=b_status, start=200.0, last=300.0)
        return a, b

    def test_a_sure_write_seats_the_writer(self):
        p = self.report(1, 'B')
        jd = self.assign(agent('a1', writes=[wr(p, 210)], status='running'))
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.agent, c.evidence, c.editors, c.round, c.state, c.previous), ('a1', 'a1', 'tool', [], 1, 'draft', False))
        self.assertEqual(self.owned(jd, 'a1'), [(1, 'B')])
        self.assertEqual(jd.agent_units, {'a1': {self.unit}})
        self.assertEqual(jd.listed, {self.unit: 'cell'})
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'tool')                    # the write, not the words
        self.assertEqual(jd.diag, [])

    def test_reader_quoter_negator_hold_no_seat(self):
        p = self.report(1, 'B')
        reader = agent('reader', reads=[rd(p), rd(os.path.join(self.unit, 'brief.md'))], status='running')
        quoter = agent('quoter', status='running')                                  # said things about the file and did nothing to it: it has no event
        negator = agent('negator', writes=[wr(p, 210, ok=False)], status='running')   # the write it was asked for failed
        jd = self.assign(reader, quoter, negator, walked=[self.unit])
        self.assertEqual(jd.assignments, [])
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.agent, c.state, c.previous), (None, None, 'previous', True))   # nobody wrote the file in this session
        self.assertEqual(jd.worked, {'reader': {self.unit}})                         # a reader works in the debate it reads the guide of: it holds no seat
        self.assertEqual((jd.placed['reader'].why, jd.placed['reader'].sure), ('guide_read', False))
        self.assertEqual(self.codes(jd), [])
        self.assertEqual(jd.agent_units, {})

    def test_files_with_alike_names_are_cells_of_their_own(self):
        names = ('A', 'A_flow', 'a', '검토')
        agents = [agent('w%d' % i, writes=[wr(self.report(1, n), 210 + 5 * i)], status='running') for i, n in enumerate(names)]
        jd = self.assign(*agents)
        self.assertEqual({c.stem: c.owner for c in jd.cells.values()}, {'A': 'w0', 'A_flow': 'w1', 'a': 'w2', '검토': 'w3'})      # nothing is merged, whatever the names have in common
        self.assertEqual(jd.diag, [])

    def test_a_write_that_succeeded_is_a_seat_whatever_the_instruction_quotes(self):
        """Only the result of the write decides (the instruction is no input): a write that worked seats, a failed one and one whose result is not known do not, whatever stands on the disk."""
        p = self.report(1, 'B')
        jd = self.assign(agent('a1', writes=[wr(p, 210)]))
        self.assertEqual(self.owned(jd, 'a1'), [(1, 'B')])
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'tool')
        for ok in (False, None):
            jd = self.assign(agent('a1', writes=[wr(p, 210, ok=ok)]), walked=[self.unit])
            c = self.cell(jd, 'r1', 'B')
            self.assertEqual((jd.assignments, jd.agent_units, c.owner, c.state), ([], {}, None, 'previous'), ok)

    def test_a_path_that_is_not_on_disk_seats_nobody(self):
        ghost = self.p('docs', 'ghost', 'r1', 'B.md')                                # no such folder: not a debate
        a = agent('a1', writes=[wr(ghost, 210)], reads=[rd(self.p('docs', 'ghost', 'brief.md'))], planned=[plan(ghost)], status='running')
        jd = self.assign(a)
        self.assertEqual((jd.cells, jd.worked, jd.listed, jd.diag), ({}, {}, {}, []))
        self.dirs('docs/ghost/r1')                                                      # the round folder is the whole difference: with it the same events make the cell
        jd = self.assign(a)
        self.assertEqual([(c.stem, c.owner, c.state) for c in jd.cells.values()], [('B', 'a1', 'writing')])

    def test_a_report_write_seats_by_its_own_path_and_a_failed_one_is_a_misc_note(self):
        """A write seats by the path it made; a failed one seats nobody and leaves no note of any kind (the diagnostic for a stray tag is gone)."""
        path = self.report(1, 'C')
        jd = self.assign(agent('ok', writes=[wr(path, 210)]))
        self.assertEqual(self.owned(jd, 'ok'), [(1, 'C')])
        os.unlink(path)
        jd = self.assign(agent('failed', writes=[wr(path, 210, ok=False)]))
        self.assertEqual((jd.cells, jd.assignments, jd.listed, self.codes(jd)), ({}, [], {}, []))

    def test_a_cell_nobody_wrote_but_that_is_on_disk_is_previous_and_one_with_nothing_waits(self):
        self.report(1, 'B')
        jd = self.assign(walked=[self.unit])
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.agent, c.state, c.previous), (None, None, 'previous', True))
        self.assertEqual(self.cell(jd, 'r1', 'C'), None)                                  # no file, no write, no request: no cell

    def test_spelling_of_the_round_folder_and_the_file_is_kept(self):
        spell = self.p('docs', 'spell')
        self.dirs('docs/spell/r01', 'docs/spell/round3')
        for spelled, n in (('r01', 1), ('round3', 3)):
            a = agent('a%d' % n, writes=[wr(os.path.join(spell, spelled, 'astra1.md'), 210)])
            jd = self.assign(a)
            c = jd.cells[(spell, spelled, 'astra1')]
            self.assertEqual((c.round, c.rdir, c.stem, c.owner), (n, spelled, 'astra1', 'a%d' % n))
            self.assertEqual(self.periods(jd, spell), [('a%d' % n, 100.0, None)])
            self.assertEqual(jd.diag, [])

    def test_two_live_writers_of_one_seat_hold_it(self):
        p = self.report(1, 'B')
        a, b = agent('a', writes=[wr(p, 200.0)], status='running'), agent('b', writes=[wr(p, 200.5)], status='running')
        jd = self.assign(a, b)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.agent, c.editors, jd.assignments), (None, None, ['a', 'b'], []))
        self.assertEqual(self.codes(jd), ['seat_tie_held', 'seat_tie_held'])
        self.assertEqual({d['detail'] for d in jd.diag}, {'1/B'})
        self.assertEqual(set(jd.worked), {'a', 'b'})                                     # held, not ignored: both still work there
        b = agent('b', writes=[wr(p, 202.0)], status='running')                          # two seconds apart the first one is the author and the second edits
        jd = self.assign(a, b)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.editors, jd.diag), ('a', ['b'], []))

    def test_an_attempt_that_failed_before_the_first_sure_write_holds_the_cell(self):
        """A write that failed may have made the file all the same (a shell that failed after its redirect): the first sure write of another agent is then not known to be the first."""
        p = self.report(1, 'B')
        for failed in (wr(p, 200, evidence='shell', proof='exit', ok=False), wr(p, 200, ok=False)):
            jd = self.assign(agent('a', writes=[failed], status='done'), agent('b', writes=[wr(p, 250)], status='running', start=200.0))
            c = self.cell(jd, 'r1', 'B')
            self.assertEqual((c.owner, c.agent, c.editors), (None, None, ['b']))
            self.assertEqual([(d['code'], d['agent'], d['detail']) for d in jd.diag], [('seat_tie_held', 'a', '1/B'), ('seat_tie_held', 'b', '1/B')])
        own = agent('b', writes=[wr(p, 200, ok=False), wr(p, 250)], status='running')                    # its own failed attempt before its own write holds nothing
        later = agent('a', writes=[wr(p, 300, ok=False)], status='done')                                  # a failed attempt after the first sure write does not either
        jd = self.assign(own, later)
        self.assertEqual((self.cell(jd, 'r1', 'B').owner, jd.diag), ('b', []))

    def test_an_edit_of_a_file_nobody_wrote_in_this_session_makes_an_editor_and_no_owner(self):
        p = self.report(1, 'B', mtime=50.0)
        for kind in ('update', 'append'):
            jd = self.assign(agent('e', writes=[wr(p, 210, kind=kind)], status='running'))
            c = self.cell(jd, 'r1', 'B')
            self.assertEqual((c.owner, c.agent, c.editors, c.state, c.previous), (None, None, ['e'], 'previous', True), kind)     # the file is from before
            self.assertEqual((jd.assignments, jd.agent_units, jd.worked, jd.listed), ([], {}, {'e': {self.unit}}, {self.unit: 'cell'}), kind)

    def test_a_later_agent_takes_the_seat_over_from_one_that_is_over(self):
        for status in ('done', 'failed', 'killed', 'ended'):
            a, b = self.pair(status)
            jd = self.assign(a, b)
            c = self.cell(jd, 'r1', 'B')
            self.assertEqual((c.owner, c.agent, c.editors), ('b', 'b', ['a']), status)                    # the later one is the owner of the cell, the first is its first editor
            self.assertEqual(self.periods(jd), [('a', 100.0, 200.0), ('b', 200.0, None)], status)         # the earlier period is closed
            self.assertEqual(jd.diag, [], status)
            self.assertEqual(jd.agent_units, {'b': {self.unit}}, status)

    def test_a_later_agent_that_was_not_asked_for_the_file_only_edits_it(self):
        a, b = self.pair('done', b_asked=False)
        jd = self.assign(a, b)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.editors, self.periods(jd), jd.diag), ('a', ['b'], [('a', 100.0, None)], []))       # no structural link: a re-run that nothing ties to the file edits it

    def test_an_agent_whose_process_cannot_be_seen_is_not_over(self):
        a, b = self.pair('unknown')                                                      # nothing says a ended: no handover on no evidence, b only edits
        jd = self.assign(a, b)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.editors, self.periods(jd), jd.diag), ('a', ['b'], [('a', 100.0, None)], []))

    def test_a_cut_off_run_whose_last_record_is_before_the_new_one_gives_the_seat_over(self):
        for reason in ('interrupted', 'done', 'failed'):                                  # the same script run again after the last record of the first: a new session
            a, b = self.pair(reason)
            jd = self.assign(a, b)
            self.assertEqual(self.cell(jd, 'r1', 'B').owner, 'b', reason)
            self.assertEqual(self.periods(jd), [('a', 100.0, 200.0), ('b', 200.0, None)], reason)
            self.assertEqual(jd.diag, [], reason)

    def test_a_cut_off_run_that_works_after_the_new_one_started_holds_the_seat_with_it(self):
        """A run that was resumed and works when the new one starts is not over: it keeps the seat (the new one edits). Only two writes within a second hold the cell."""
        a, b = self.pair('interrupted', a_last=250.0)                                   # a was resumed and is still at work when b starts: they overlap
        jd = self.assign(a, b)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.editors, self.periods(jd), jd.diag), ('a', ['b'], [('a', 100.0, None)], []))
        for working in ('running', 'stalled', 'unknown'):                                # a run that is working, or whose process cannot be seen, is never over
            a, b = self.pair(working)
            self.assertEqual(self.cell(self.assign(a, b), 'r1', 'B').owner, 'a', working)
        a, b = self.pair('interrupted', a_last=250.0, a_write=249.5)                    # the two writes are a half second apart: nothing says who came first
        jd = self.assign(a, b)
        self.assertEqual((self.cell(jd, 'r1', 'B').owner, self.codes(jd)), (None, ['seat_tie_held', 'seat_tie_held']))

    def test_an_agent_that_was_only_asked_for_the_file_does_not_take_the_seat_from_one_that_wrote_it(self):
        path = self.report(1, 'B')
        told = agent('a', writes=[wr(path, 300)], status='done', start=100.0, last=300.0)             # resumed after b began, and wrote
        other = agent('b', planned=[plan(path, ts=200.0)], status='running', start=200.0)               # b was asked for the file and has not saved it
        jd = self.assign(told, other)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.agent, c.editors, c.state), ('a', 'a', [], 'done'))
        self.assertEqual(self.periods(jd), [('a', 100.0, None)])
        self.assertEqual(jd.worked, {'a': {self.unit}, 'b': {self.unit}})                # b is tied to the debate by its request, it owns nothing
        self.assertEqual(jd.diag, [])
        # ... and a first period that a takeover closed does not come back by itself: the later run that saved the file keeps the seat
        again = agent('a', writes=[wr(path, 140)], status='interrupted', start=100.0, last=150.0)
        new = agent('b', writes=[wr(path, 250)], planned=[plan(path, ts=200.0)], status='done', start=200.0)
        jd = self.assign(again, new)
        self.assertEqual(self.periods(jd), [('a', 100.0, 200.0), ('b', 200.0, None)])

    def test_the_first_sure_author_wins_between_agents_that_work_at_once(self):
        path = self.report(1, 'B')
        strong = agent('strong', writes=[wr(path, 300)], status='running', start=300.0)
        weak = agent('weak', writes=[wr(path, 310, kind='update')], status='running', start=310.0)
        jd = self.assign(strong, weak)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.owner, c.editors, jd.diag), ('strong', ['weak'], []))      # the one that came first, not the one that says more: the other is an editor
        asked = agent('asked', planned=[plan(path, ts=310.0)], status='running', start=310.0)
        jd = self.assign(strong, asked)
        self.assertEqual((self.cell(jd, 'r1', 'B').agent, jd.diag), ('strong', []))   # an agent that was only asked for the file does not get the cell

    def test_the_same_agent_moves_on_to_the_next_round_without_a_tie(self):
        a = agent('a', writes=[wr(self.r(1, 'B'), 210), wr(self.r(2, 'B'), 260)], status='running')
        jd = self.assign(a)
        self.assertEqual(self.owned(jd, 'a'), [(1, 'B'), (2, 'B')])
        self.assertEqual(jd.diag, [])

    def test_the_output_a_launch_command_names_is_a_planned_cell(self):
        p = self.r(1, 'astra1')
        jd = self.assign(agent('a', planned=[plan(p, ts=100.0)], status='running'))
        c = self.cell(jd, 'r1', 'astra1')                                                 # the file to save is the cell of the one that was asked for it: nothing is saved yet
        self.assertEqual((c.agent, c.owner, c.evidence, c.state, c.previous), ('a', None, 'planned', 'writing', False))
        self.assertEqual((jd.listed, jd.worked, jd.assignments), ({self.unit: 'cell'}, {'a': {self.unit}}, []))
        # the run ends and the file is what it saved: a sure write of the run (F), the cell is its own
        text = 'the report\n'
        self.report(1, 'astra1', text=text, mtime=250.0)
        saved = wr(p, 300, evidence='planned', proof='sha', span=(100.0, 300.0), text=text)
        jd = self.assign(agent('a', planned=[plan(p, ts=100.0)], writes=[saved], status='done'))
        c = self.cell(jd, 'r1', 'astra1')
        self.assertEqual((c.owner, c.evidence, c.state), ('a', 'planned', 'done'))
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'planned')
        # the file that stands there is not what the run saved (older than the run, or other bytes): not a submission, the file is the previous one
        for mtime, other in ((50.0, text), (250.0, 'from another run\n')):
            self.report(1, 'astra1', text=other, mtime=mtime)
            jd = self.assign(agent('a', planned=[plan(p, ts=100.0)], writes=[saved], status='done'))
            c = self.cell(jd, 'r1', 'astra1')
            self.assertEqual((c.owner, c.agent, c.state, c.previous), (None, 'a', 'missing', True), mtime)

    def test_a_listing_a_walk_found_needs_no_agent(self):
        self.report(1, 'B')
        jd = self.assign(walked=[self.unit])
        self.assertEqual(jd.listed, {self.unit: 'walk'})
        self.assertEqual(self.cell(jd, 'r1', 'B').state, 'previous')


class OutputFiles(Tree):
    """The output file a launch command names (`-o`, `>`) is a request to save that file: a cell when it is `<round folder>/<name>.md` of a debate, nothing otherwise. Nothing is compared with
    what the agent was told: nobody tells the judgment."""

    def test_the_output_file_is_a_cell_and_the_save_of_the_run_is_the_write_that_owns_it(self):
        p = self.r(1, 'B_gate')
        a = agent('a', planned=[plan(p, op='-o', ts=100.0)], status='running')
        jd = self.assign(a)
        c = self.cell(jd, 'r1', 'B_gate')
        self.assertEqual((c.agent, c.owner, c.evidence, c.state), ('a', None, 'planned', 'writing'))
        text = 'findings\n'
        self.report(1, 'B_gate', text=text, mtime=250.0)
        a = agent('a', planned=[plan(p, op='-o', ts=100.0)], writes=[wr(p, 300, evidence='planned', proof='sha', span=(100.0, 300.0), text=text)], status='done')
        jd = self.assign(a)
        self.assertEqual(self.owned(jd, 'a'), [(1, 'B_gate')])
        ev = jd.assignments[0].evidence[0]
        self.assertEqual((ev.kind, ev.candidates), ('planned', [self.unit, 1, 'B_gate', 'r1']))      # the kind of output and the file's own folder are kept

    def test_an_output_file_outside_a_round_folder_is_no_cell(self):
        """The last-message file of a run, kept beside the reports or in a folder of runs, is the file of no seat: a cell is a file in a round folder."""
        last = self.file('repo/docs/rev/runs/B_last.md', 'last message\n', mtime=250.0)
        beside = self.file('repo/docs/rev/B_last.md', 'last message\n', mtime=250.0)
        for p in (last, beside):
            a = agent('a', planned=[plan(p, ts=100.0)], writes=[wr(p, 300, evidence='planned', proof='sha', span=(100.0, 300.0), text='last message\n')], status='done')
            jd = self.assign(a)
            self.assertEqual((jd.cells, jd.listed, jd.assignments, jd.worked, jd.diag), ({}, {}, [], {}, []), p)

    def test_the_outputs_of_two_rounds_are_two_cells(self):
        a = agent('a', planned=[plan(self.r(1, 'B_gate'), ts=100.0), plan(self.r(2, 'B'), ts=200.0)], status='running')
        jd = self.assign(a)
        self.assertEqual(sorted((c.round, c.stem, c.agent, c.state) for c in jd.cells.values()), [(1, 'B_gate', 'a', 'writing'), (2, 'B', 'a', 'writing')])


class TwoSpellings(Tree):
    """r01 next to r1 are two folders with two files; a cell is a file, so nothing is merged and each keeps its own folder. The collision is noted once on the debate."""

    def setUp(self):
        super().setUp()
        self.dirs('repo/docs/rev/r01')

    def test_each_seat_keeps_its_own_folder(self):
        x = agent('x', writes=[wr(self.r(1, 'B', 'r01'), 210)], status='running', start=100.0)
        y = agent('y', writes=[wr(self.r(1, 'B', 'r1'), 220)], status='running', start=200.0)
        jd = self.assign(x, y)
        self.assertEqual({(rd_, c.stem): (c.owner, c.round) for (u, rd_, _s), c in jd.cells.items()}, {('r01', 'B'): ('x', 1), ('r1', 'B'): ('y', 1)})       # two files: no tie
        self.assertEqual([(a.agent, a.evidence[0].candidates[3]) for a in jd.assignments], [('x', 'r01'), ('y', 'r1')])
        self.assertEqual(self.codes(jd), ['alias_collision'])
        same = self.assign(agent('p', writes=[wr(self.r(1, 'B', 'r1'), 210)], status='running'), agent('q', writes=[wr(self.r(1, 'B', 'r1'), 210.5)], status='running'))
        self.assertEqual(self.codes(same, 'p'), ['seat_tie_held'])                           # the same file is still a tie

    def test_the_cell_is_the_file_of_the_seat_and_not_the_other_folder(self):
        self.file('repo/docs/rev/r01/B.md', 'the other file\n')
        y = agent('y', planned=[plan(self.r(1, 'B', 'r1'), ts=200.0)], status='running', start=200.0)
        jd = self.assign(y)
        c = self.cell(jd, 'r1', 'B')
        self.assertEqual((c.agent, c.state, c.previous), ('y', 'writing', False))        # not r01/B.md, which is another file
        other = self.cell(jd, 'r01', 'B')
        self.assertEqual((other.agent, other.state, other.previous), (None, 'previous', True))

    def test_the_row_of_the_seat_shows_the_file_of_the_seat_and_not_the_other_folder(self):
        self.file('repo/docs/rev/r01/B.md', 'the other file\n')
        a = view_agent('a0000000000000009', planned=[plan(self.r(1, 'B', 'r1'), ts=200.0)], start=200.0)
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        topic = debates.debates(s, {a.id: 'running'})[0][0]['topics'][0]
        row = next(r for r in topic['rows'] if r['p'] == 'B')
        cell = row['cells'][0]
        self.assertEqual((cell['round'], cell['agent'], cell['path'], cell['state']), (1, a.id, self.r(1, 'B', 'r1'), 'writing'))      # not r01/B.md, which is another file
        self.assertEqual(sorted(r['p'] for r in topic['rows']), ['B'])                    # the other folder's file is the same row, not a new one

    def test_an_output_file_in_the_other_folder_is_a_file_of_its_own_and_seats_nobody(self):
        for out_in, other in (('r1', 'r01'), ('r01', 'r1')):
            p = self.r(1, 'B', out_in)
            a = agent('a', planned=[plan(p, ts=100.0)], status='running')
            jd = self.assign(a)
            self.assertEqual({(rd_, c.stem): c.agent for (u, rd_, _s), c in jd.cells.items()}, {(out_in, 'B'): 'a'}, out_in)       # the file it was asked for, not its twin in the other folder
            self.assertEqual((jd.assignments, self.cell(jd, other, 'B')), ([], None), out_in)                                       # an output seats nobody, and makes no cell of the twin
            self.assertEqual(self.codes(jd), ['alias_collision'], out_in)
            text = 'x\n'                                                                    # the run saved it: the owner of that file, and only of that one
            self.report(1, 'B', out_in, text=text, mtime=250.0)
            saved = agent('a', planned=[plan(p, ts=100.0)], writes=[wr(p, 300, evidence='planned', proof='sha', span=(100.0, 300.0), text=text)], status='done')
            jd = self.assign(saved)
            self.assertEqual({(rd_, c.stem): c.owner for (u, rd_, _s), c in jd.cells.items()}, {(out_in, 'B'): 'a'}, out_in)
            os.unlink(self.r(1, 'B', out_in))

    def test_a_round_with_one_folder_does_not_tell_the_spellings_apart(self):
        a = agent('a', planned=[plan(os.path.join(self.unit, 'r2', 'B.md'), ts=100.0)], status='running')       # r2 has one folder only
        jd = self.assign(a)
        c = self.cell(jd, 'r2', 'B')
        self.assertEqual((c.round, c.rdir, c.agent), (2, 'r2', 'a'))
        self.assertEqual([(d['code'], d['detail']) for d in jd.diag], [('alias_collision', 'r01,r1')])        # round 1 has two spellings, round 2 has not

    def test_the_cell_of_a_file_that_was_only_an_output_is_not_done(self):
        self.file('repo/docs/rev/r01/B.md', 'what the launch command printed\n')
        a = agent('a', planned=[plan(self.r(1, 'B', 'r1'), ts=200.0)], start=200.0)
        for status, want in (('running', 'writing'), ('done', 'missing')):
            jd = self.assign(dataclasses.replace(a, status=status))
            c = self.cell(jd, 'r1', 'B')
            self.assertEqual((c.agent, c.path, c.state, c.previous), ('a', self.r(1, 'B', 'r1'), want, False), status)      # r1/B.md is the file asked for; r01/B.md is another one
            self.assertEqual(self.cell(jd, 'r01', 'B').state, 'previous', status)
            self.assertEqual(self.codes(jd), ['alias_collision'], status)

    def test_the_row_of_a_file_that_was_only_an_output_is_not_done(self):
        self.file('repo/docs/rev/r01/B.md', 'what the launch command printed\n')
        a = view_agent('a0000000000000009', planned=[plan(self.r(1, 'B', 'r1'), ts=200.0)], start=200.0)
        for status, want in (('running', 'writing'), ('done', 'missing')):
            s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
            topic = debates.debates(s, {a.id: status})[0][0]['topics'][0]
            cell = next(r for r in topic['rows'] if r['p'] == 'B')['cells'][0]
            self.assertEqual((cell['agent'], cell['path'], cell['state']), (a.id, self.r(1, 'B', 'r1'), want), status)       # r1/B.md is the file asked for; r01/B.md is another one
            self.assertEqual([d['code'] for d in s.debate_diag], ['alias_collision'])

    def test_one_agent_has_one_file_of_a_seat(self):
        """Two files of one name in two spellings are two cells: an agent that wrote both owns both (nothing says they are one seat); one that wrote one owns that one, the other is previous."""
        both = agent('a', writes=[wr(self.report(1, 'B', 'r1'), 210), wr(self.report(1, 'B', 'r01'), 220)], status='running')
        jd = self.assign(both)
        self.assertEqual({(rd_, c.stem): c.owner for (u, rd_, _s), c in jd.cells.items()}, {('r1', 'B'): 'a', ('r01', 'B'): 'a'})
        self.assertEqual(self.codes(jd), ['alias_collision'])
        one = agent('b', writes=[wr(self.r(1, 'B', 'r01'), 230)], status='running')
        jd = self.assign(one)
        self.assertEqual({(rd_, c.stem): (c.owner, c.state) for (u, rd_, _s), c in jd.cells.items()}, {('r01', 'B'): ('b', 'draft'), ('r1', 'B'): (None, 'previous')})
        self.assertEqual(self.owned(jd, 'b'), [(1, 'B')])

    def test_without_a_seat_the_cell_shows_the_folder_that_has_the_file(self):
        self.file('repo/docs/rev/r1/B.md', 'x\n')
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, walked_units=[self.unit])
        row = next(r for r in debates.debates(s, {})[0][0]['topics'][0]['rows'] if r['p'] == 'B')
        self.assertEqual((row['cells'][0]['path'], row['cells'][0]['state']), (self.r(1, 'B', 'r1'), 'previous'))         # nobody wrote it in this session: it is the file from before


class Cells(unittest.TestCase):
    """Only an interrupted run gets a state of its own (paused without a file, a draft with one); the rest reads the file as before. An agent whose process cannot be seen
    (`unknown`, a platform without a process table, quiet for a minute) is working as far as anyone knows: it is never shown as done or missing. (`previous` is the judgment's,
    for a file nobody wrote in this run: it is not a state of this table.)"""
    TABLE = [
        # (status, file, working here, has agent) -> state
        (('running', False, True, True), 'writing'), (('running', True, True, True), 'draft'),
        (('stalled', False, True, True), 'writing'), (('stalled', True, True, True), 'draft'),
        (('interrupted', False, True, True), 'paused'), (('interrupted', True, True, True), 'draft'),
        (('done', True, True, True), 'done'), (('done', False, True, True), 'missing'),
        (('ended', True, True, True), 'done'), (('ended', False, True, True), 'missing'),
        (('failed', True, True, True), 'done'), (('killed', False, True, True), 'missing'),
        (('unknown', False, True, True), 'writing'), (('unknown', True, True, True), 'draft'),               # its process cannot be seen: nothing says it ended
        (('unknown', True, False, True), 'done'), (('unknown', False, False, True), 'missing'),             # but not for a round it has moved on from
        (('running', True, False, True), 'done'), (('running', False, False, True), 'missing'),     # it works on another round now
        (('interrupted', False, False, True), 'missing'), (('interrupted', True, False, True), 'done'),
        ((None, False, False, False), 'waiting'), ((None, True, False, False), 'done'),
    ]

    def test_table(self):
        for args, want in self.TABLE:
            self.assertEqual(U.cell_state(*args), want, args)


class DebatesView(Fixture):
    """What the board shows of the judgment: the cells of a seat follow the status of its agent and what it did, a cell nobody wrote is the previous file."""

    def setUp(self):
        super().setUp()
        self.unit = self.p('repo', 'rev')
        self.file('repo/rev/brief.md', GUIDE)
        self.dirs('repo/rev/round1')
        self.report = self.p('repo', 'rev', 'round1', 'B_gate.md')

    def build(self, status, writes=(), planned=(), reads=(), **kw):
        a = view_agent('a0000000000000001', writes=writes, planned=planned, reads=reads, **kw)
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        return debates.debates(s, {a.id: status}), a

    def cell(self, out):
        topic = out[0][0]['topics'][0]
        return {r['p']: r['cells'][0] for r in topic['rows']}

    def test_interrupted_agent_is_paused_without_a_file_and_drafting_with_one(self):
        out, a = self.build('interrupted', planned=[plan(self.report, ts=100.0)])                        # asked for the file, and has not saved it
        cell = self.cell(out)['B_gate']
        self.assertEqual((cell['state'], cell['agent'], cell['path'], cell['previous']), ('paused', a.id, self.report, False))      # the spelling is kept
        self.file('repo/rev/round1/B_gate.md', 'one\ntwo\n')                                              # a file from before: it does not make this run's cell a draft
        out, a = self.build('interrupted', planned=[plan(self.report, ts=100.0)])
        cell = self.cell(out)['B_gate']
        self.assertEqual((cell['state'], cell['previous']), ('paused', True))
        out, a = self.build('interrupted', planned=[plan(self.report, ts=100.0)], writes=[wr(self.report, 150.0)])    # it saved it: a draft
        cell = self.cell(out)['B_gate']
        self.assertEqual((cell['state'], cell['owner'], cell['previous']), ('draft', a.id, False))

    def test_the_other_states_read_the_file_as_before(self):
        for status, nofile, withfile in (('running', 'writing', 'draft'), ('stalled', 'writing', 'draft'), ('done', 'missing', 'done'), ('ended', 'missing', 'done'),
                                        ('failed', 'missing', 'done'), ('killed', 'missing', 'done'), ('unknown', 'writing', 'draft')):
            if os.path.exists(self.report):
                os.unlink(self.report)
            out, _a = self.build(status, writes=[wr(self.report, 150.0)])                                  # a sure write that made it; the file is gone
            self.assertEqual(self.cell(out)['B_gate']['state'], nofile, status)
            self.file('repo/rev/round1/B_gate.md', 'x\n')
            out, _a = self.build(status, writes=[wr(self.report, 150.0)])
            self.assertEqual(self.cell(out)['B_gate']['state'], withfile, status)

    def test_a_shell_write_of_the_report_that_succeeded_is_a_write_that_succeeded(self):
        """What makes a shell write sure (J1): a command whose own exit status decides it and says ok; one whose later part can hide a failure only when the file was saved while it ran."""
        shell = dict(evidence='shell', proof='exit')
        masked = dict(evidence='shell', proof='window', span=(120.0, 130.0))
        rows = (  # (the write event, the time of the file (None: no file), the cell: (owner, state) or None for no cell, no row)
            (dict(shell, ok=True), 125.0, ('a', 'done')),                      # it ran and the file is there
            (dict(shell, ok=True), None, ('a', 'missing')),                    # the exit status said it worked: the file was written, and it is gone now
            (dict(shell, ok=False), 125.0, (None, 'previous')),                # the call failed: the file there is not its work
            (dict(shell, ok=None), 125.0, (None, 'previous')),                 # no result yet, the file is already there: the file does not say it was this call
            (dict(shell, ok=None), None, None),                                # no result yet and no file
            (dict(masked, ok=True), 125.0, ('a', 'done')),                     # a command that may hide a failure: the file was saved while it ran
            (dict(masked, ok=True), 90.0, (None, 'previous')),                 # ... the file is older than the command
            (dict(masked, ok=True), 130.5, (None, 'previous')),                # ... the file is later than the command and a short grace
            (dict(masked, ok=None), 125.0, (None, 'previous')))                # ... the command did not report that it worked
        for event, mtime, want in rows:
            if os.path.exists(self.report):
                os.unlink(self.report)
            if mtime is not None:
                self.file('repo/rev/round1/B_gate.md', 'findings\n', mtime=mtime)
            a = view_agent('a', writes=[wr(self.report, 135.0, **event)])
            s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={}, walked_units=[self.unit])      # (listed by a walk: a write that is not sure lists nothing)
            out = debates.debates(s, {a.id: 'done'})[0]
            cells = {r['p']: r['cells'][0] for d in out for t in d['topics'] for r in t['rows']}
            got = (cells['B_gate']['owner'], cells['B_gate']['state']) if 'B_gate' in cells else None
            self.assertEqual(got, want, (event, mtime))
        other = view_agent('a', writes=[wr(os.path.join(self.unit, 'notes.log'), 135.0, **shell)])        # a shell write of another file: no cell of it
        s = types.SimpleNamespace(agents={other.id: other}, _file_cache={}, _head_cache={})
        self.assertEqual(debates.debates(s, {other.id: 'done'})[0], [])
        reader = view_agent('a', reads=[rd(self.report)])                                                   # a read: no seat
        self.file('repo/rev/round1/B_gate.md', 'findings\n')
        s = types.SimpleNamespace(agents={reader.id: reader}, _file_cache={}, _head_cache={})
        self.assertEqual(debates.debates(s, {reader.id: 'done'})[0], [])

    def test_the_cell_of_a_request_shows_who_was_asked_and_that_nothing_is_saved(self):
        out, a = self.build('running', planned=[plan(self.report, ts=100.0)])
        cell = self.cell(out)['B_gate']
        self.assertEqual((cell['agent'], cell['owner'], cell['editors'], cell['evidence'], cell['planned'], cell['previous'], cell['rdir'], cell['state']),
                         (a.id, None, [], 'planned', True, False, 'round1', 'writing'))
        self.assertEqual(out[1], {})                                                      # it owns nothing yet: no debate is its own

    def test_a_debate_only_a_shell_write_names_is_listed_with_that_cell(self):
        report = self.file('repo/rev/round1/B_gate.md', 'findings\n')
        a = view_agent('a0000000000000001', writes=[wr(report, 135.0, evidence='shell', proof='exit')])
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        debates_out = debates.debates(s, {a.id: 'done'})[0]
        cell = {r['p']: r['cells'][0] for d in debates_out for t in d['topics'] for r in t['rows']}['B_gate']
        self.assertEqual((cell['agent'], cell['state'], cell['evidence']), (a.id, 'done', 'shell'))
        a = view_agent('a0000000000000001', reads=[rd(report)])
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        self.assertEqual(debates.debates(s, {a.id: 'done'})[0], [])                           # a read names no debate

    def test_a_finished_writer_keeps_its_done_cell_when_a_weaker_claim_comes_later(self):
        path = self.file('repo/rev/round1/B_gate.md', 'report\n')
        writer = view_agent('a0000000000000011', writes=[wr(path, 120.0)], start=100.0, last=150.0)
        later = view_agent('a0000000000000012', writes=[wr(path, 210.0, kind='update')], start=200.0)       # a later agent that edits the file (nothing asked it to save it) is an editor
        s = types.SimpleNamespace(agents={writer.id: writer, later.id: later}, _file_cache={}, _head_cache={})
        topic = debates.debates(s, {writer.id: 'done', later.id: 'running'})[0][0]['topics'][0]
        cell = next(r for r in topic['rows'] if r['p'] == 'B_gate')['cells'][0]
        self.assertEqual((cell['agent'], cell['state'], cell['editors']), (writer.id, 'done', [later.id]))      # the later one did not take the cell and turn it into a draft

    def test_a_debate_on_disk_that_no_session_names_is_listed_from_the_walk(self):
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={})
        self.assertEqual(debates.debates(s, {}), ([], {}))
        s.walked_units, s.walk_capped = [self.unit, os.path.join(self.unit, 'nowhere')], True
        out = debates.debates(s, {})
        self.assertEqual([(d['root'], d['last_ts'], [t['dir'] for t in d['topics']]) for d in out[0]], [(self.unit, 0, [self.unit])])
        self.assertEqual([d['code'] for d in s.debate_diag], ['listing_capped'])

    def grouped(self, root_text):
        """A common guide folder `migration` with two topic folders; the guide says `root_text`."""
        base = os.path.dirname(self.unit)
        parent = os.path.join(base, 'migration')
        self.file(os.path.relpath(os.path.join(parent, 'brief.md'), self.root), root_text)
        topics = []
        for name in ('t1_schema', 't2_data'):
            t = os.path.join(parent, name)
            self.file(os.path.relpath(os.path.join(t, 'brief.md'), self.root), '# %s\n\n%s' % (name, GUIDE))
            self.dirs(os.path.relpath(os.path.join(t, 'round1'), self.root))
            topics.append(t)
        return parent, topics

    def test_a_common_guide_that_describes_rounds_is_the_root_and_not_an_empty_topic(self):
        for text in ('# Migration\n\nEvery topic keeps its reports in `r1/<id>.md` and `r2/<id>.md`.\n',
                     '# Migration\n\n**A — flow**\n**B — data**\n\n| topic | folder |\n|---|---|\n| Schema | `t1_schema/` |\n',
                     '# Migration\n\nNothing about rounds here.\n'):
            parent, topics = self.grouped(text)
            self.assertIsNone(U.Catalog().unit_at(parent))                                       # no round folder: no unit, whatever it says; its guide makes it the root
            s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, walked_units=[parent] + topics)
            out = debates.debates(s, {})[0]
            self.assertEqual([(d['root'], [t['dir'] for t in d['topics']]) for d in out], [(parent, topics)], text)      # one card, its topics are the folders below it
            self.assertEqual(sum(1 for d in out for t in d['topics'] if t['dir'] == parent), 0)

    def test_the_common_guide_stays_a_root_when_an_agent_works_in_one_topic(self):
        parent, topics = self.grouped('# Migration\n\nEvery topic keeps its reports in `r1/<id>.md`.\n')
        path = self.file(os.path.relpath(os.path.join(topics[0], 'round1', 'A.md'), self.root))
        a = view_agent('a0000000000000008', writes=[wr(path, 120.0)])
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={}, walked_units=[parent] + topics)
        out, seated = debates.debates(s, {a.id: 'running'})
        self.assertEqual([(d['root'], [t['dir'] for t in d['topics']]) for d in out], [(parent, topics)])
        self.assertEqual(seated[a.id], {topics[0]})

    def test_a_guide_folder_with_a_round_folder_or_a_seat_is_a_topic_itself(self):
        parent, topics = self.grouped('# Migration\n\nReports go to `r1/<id>.md`.\n')
        os.makedirs(os.path.join(parent, 'r1'))                                                  # it holds a round itself: a debate with topics below it
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, walked_units=[parent] + topics)
        out = debates.debates(s, {})[0]
        self.assertIn(parent, [t['dir'] for d in out for t in d['topics']])

    def test_the_diagnostics_are_left_on_the_session(self):
        path = self.file('repo/rev/round1/B.md')
        a, b = view_agent('a0000000000000002', writes=[wr(path, 120.0)]), view_agent('a0000000000000003', writes=[wr(path, 120.5)])
        s = types.SimpleNamespace(agents={a.id: a, b.id: b}, _file_cache={}, _head_cache={})
        debates.debates(s, {a.id: 'running', b.id: 'running'})
        self.assertEqual([(d['code'], d['agent']) for d in s.debate_diag], [('seat_tie_held', a.id), ('seat_tie_held', b.id)])
        for d in s.debate_diag:
            self.assertEqual(sorted(d), ['agent', 'code', 'detail', 'unit'])                  # ids, folders and short codes: no text of a record


if __name__ == '__main__':
    unittest.main()
