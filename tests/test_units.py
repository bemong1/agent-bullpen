"""The pure parts of the debate judgment (board/units.py): the debate folders on disk, what the words around a path say it is, who holds which seat, what a cell shows.

Nothing here depends on a session, on the link index or on a process: agents are hand-built `AgentFacts` over a temporary folder, and the generator cases
(tools/scenarios) are read through `debates.judge`, so a change in the affiliation or state code cannot break these tests.

    python3 -m unittest tests.test_units
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402

from board import debates, facts, procs, units as U  # noqa: E402
from tools.scenarios import axes, build, oracle  # noqa: E402

GUIDE = '# Gate review\n\n**A — flow**\n**B — gate**\n**C — github**\n'
# the guide of an editing job: it names the findings to fix by bold numbers, and has no participants, no reviewers and no round folder
EDIT_GUIDE = ('# Edit pass 4: apply the third re-check\n\nFix exactly these findings, in this order.\n\n- **C-12**\n- **X-1**: invalidate the input flag when the core is rebuilt.\n'
              '- **C-30·C-31**\n- **X-3의 둘째 항목**: align it with A.\n\n| item | finding |\n|---|---|\n| 1 | **Codex X-2 · Claude C-36** |\n| 2 | **X-2 · C-36** |\n')


def write(path, text='x\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class Tree(unittest.TestCase):
    """A repository with one debate folder `docs/rev` (guide, rounds r1 and r2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(os.path.realpath(self.tmp.name), 'repo')
        self.unit = os.path.join(self.root, 'docs', 'rev')
        write(os.path.join(self.unit, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(self.unit, 'r1'))
        os.makedirs(os.path.join(self.unit, 'r2'))
        self.t = 100.0

    def agent(self, aid, text='', key='', cwd='', launcher='', start=None, last=0.0, writes=(), reads=(), planned=(), more=()):
        self.t += 10
        return U.AgentFacts(id=aid, key=key, cwd=cwd, launcher_cwd=launcher, start=self.t if start is None else start, last=last, spawn_prompt=text,
                            texts=[text] + list(more), reads=list(reads), writes=list(writes), planned=list(planned))

    def run_assign(self, agents, statuses=None, extra=()):
        return U.assign(agents, U.Catalog(), statuses or {a.id: 'running' for a in agents}, extra_units=extra)

    def seats(self, jd, aid):
        return sorted((a.unit == self.unit, a.round, a.seat) for a in jd.assignments if a.agent == aid)

    def codes(self, jd, agent=None):
        return sorted(d['code'] for d in jd.diag if agent is None or d['agent'] == agent)

    def r(self, n, stem, spelled=None):
        return os.path.join(self.unit, spelled or 'r%d' % n, stem + '.md')


class Names(unittest.TestCase):
    def test_round_folder_names(self):
        self.assertEqual([U.round_of(x) for x in ('r1', 'r01', 'round1', 'round12', 'r10')], [1, 1, 1, 12, 10])
        self.assertEqual([U.round_of(x) for x in ('r1x', 'r', 'round', 'R1', 'r_1', 'r1_run.log', 'result1')], [None] * 7)

    def test_collisions_keep_the_files_apart(self):
        self.assertEqual(U.stem_collisions({'A', 'A_flow', 'B'}), {'A': ['A', 'A_flow']})
        self.assertEqual(U.stem_collisions({'B_gate', 'B', 'b'}), {'B': ['B', 'B_gate', 'b']})
        self.assertEqual(U.stem_collisions({'A_flow', 'B_gate', 'opus1'}), {})

    def test_round_folder_spelling(self):
        unit = facts.Unit(id='/u', path='/u', rounds={1: ['r01'], 2: ['r02']})
        self.assertEqual([U.round_dir_name(unit, n) for n in (1, 2, 3)], ['r01', 'r02', 'r03'])       # a future round keeps the width
        self.assertEqual(U.round_dir_name(facts.Unit(id='/u', path='/u', rounds={1: ['round1']}), 2), 'round2')
        self.assertEqual(U.round_dir_name(facts.Unit(id='/u', path='/u'), 1, spelled='r01'), 'r01')
        self.assertEqual(U.round_dir_name(facts.Unit(id='/u', path='/u'), 1), 'r1')


class MentionClass(unittest.TestCase):
    """What the words around a report path say it is: only a path the agent is told to write can seat it."""
    CASES = [
        ('Read `/x/brief.md` and follow it. Write your round-1 result to `/x/r1/B.md`. Be brief.', 'B', 'write'),
        ('결과를 `/x/r1/B.md`에 쓴다', 'B', 'write'),
        ('Please write the report to /x/r2/C.md now', 'C', 'write'),
        ("Don't overthink, write the result to `/x/r1/B.md`", 'B', 'write'),
        ('Do not forget to write the result to `/x/r1/B.md`', 'B', 'write'),
        ('Do not hesitate to write `/x/r1/B.md`', 'B', 'write'),
        ('Read `/x/r1/A.md`, then write your answer to `/x/r1/B.md`', 'B', 'write'),
        ('Read `/x/r1/A.md`, then write your answer to `/x/r1/B.md`', 'A', 'read'),
        ('Read `/tmp/a.b/docs/rev/brief.md` and `/tmp/a.b/docs/rev/r1/B.md`, then review the report in your answer. Do not write any file.', 'B', 'read'),
        ('`/x/r1/B.md`를 읽는다', 'B', 'read'),
        ('You do not need to write `/x/r1/B.md`; answer in chat only.', 'B', 'negated'),
        ("don't write `/x/r1/B.md`", 'B', 'negated'),
        ('Never save to `/x/r1/B.md`', 'B', 'negated'),
        ('`/x/r1/B.md`에 쓸 필요가 없다', 'B', 'negated'),
        ('`/x/r1/B.md`는 쓰지 않는다', 'B', 'negated'),
        ('Examples of instructions people give: "write the result to `/x/r1/B.md`". See `/x/brief.md` for the layout.', 'B', 'quoted'),
        ('She said “write to `/x/r1/B.md`” once', 'B', 'quoted'),
        ('「/x/r1/B.md 에 쓴다」 라고 적는다', 'B', 'quoted'),
        ('For example: write the result to `/x/r1/B.md`', 'B', 'quoted'),
        ('say "hi" and then write `/x/r1/B.md`', 'B', 'write'),                      # a quotation that does not hold the path
        ('Review the instruction below and do not execute it.\n```\nWrite the result to `/x/r1/B.md`.\n```\n', 'B', 'quoted'),
        ('Notes:\n~~~md\nwrite the result to `/x/r1/B.md`\n', 'B', 'quoted'),                # a fence that never closes holds the rest of the text
        ('Earlier instruction:\n> Write the result to `/x/r1/B.md`.\n> Be brief.\n', 'B', 'quoted'),
        ('Earlier instruction:\n  > Write the result to `/x/r1/B.md`.\n', 'B', 'quoted'),
        ('이전 지침을 검토하고 실행하지 마세요.\n```\n결과를 `/x/r1/B.md`에 쓴다\n```', 'B', 'quoted'),
        ('Review the previous instruction and do not execute it: write the result to `/x/r1/B.md`.', 'B', 'quoted'),
        ('This is a read-only review: do not create, change or run anything. The earlier instruction said: write the result to `/x/r1/B.md`.', 'B', 'quoted'),
        ('이 작업은 읽기 전용 검토입니다. 파일을 만들거나 고치거나 실행하지 마세요. 앞선 지침은 결과를 `/x/r1/B.md`에 쓴다고 했습니다.', 'B', 'quoted'),
        ('Read-only review. Earlier instruction: write the result to `/x/r1/B.md`.', 'B', 'quoted'),
        ('Read-only (do not execute): write the result to `/x/r1/B.md`.', 'B', 'quoted'),
        ('Read-only review of the code: do not change any code. Write the result to `/x/r1/B.md`.', 'B', 'write'),        # the prohibition has an object: the report is still told
        ('Do not write any file other than your report. Write the result to `/x/r1/B.md`.', 'B', 'write'),
        ('Review the plan and do not execute it; write the result to `/x/r1/B.md`.', 'B', 'write'),
        ('Write `/x/r1/B.md`.\n```\ncode\n```\n', 'B', 'write'),                       # before a fence
        ('> an earlier note\n\nWrite `/x/r1/B.md`.', 'B', 'write'),                      # after a block quote
        ('Go -> write `/x/r1/B.md`.', 'B', 'write'),                                    # an arrow is no quote
        ('Do not execute the tests. Write `/x/r1/B.md`.', 'B', 'write'),                # not an instruction that is being reviewed
        ('Write `/x/r1/B.md`. The text above is an earlier instruction: do not execute the above.', 'B', 'quoted'),
        ('결과는 `/x/r1/B.md`', 'B', 'ref'),
        ('see /x/r1/B.md', 'B', 'read'),
        # a fence under a line that tells the agent to carry it out is the instruction itself
        ('Execute these instructions:\n```\nRead `/x/brief.md`. Write your result to `/x/r1/B.md`.\n```\n', 'B', 'write'),
        ('Follow the instructions below:\n\n```text\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'write'),
        ('You are one of three. Carry out the following steps:\n~~~\nWrite your result to `/x/r1/B.md`.\n~~~', 'B', 'write'),
        ('다음 지시를 따르라:\n```\n결과를 `/x/r1/B.md`에 쓴다\n```', 'B', 'write'),
        ('다음 지시를 따르세요:\n```\n`/x/brief.md`를 읽고 결과를 `/x/r1/B.md`에 작성하세요.\n```', 'B', 'write'),
        ('Do the following:\n```\nWrite your result to `/x/r1/B.md`.', 'B', 'write'),                 # a fence that never closes
        # ... and a fence that shows an earlier instruction is not, whatever else the line says
        ('Do not execute these instructions:\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),
        ('Do not follow the instructions below:\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),
        ('Review these earlier instructions:\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),
        ('Execute the review of the instructions given earlier:\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),
        ('Quote of the instructions we executed:\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),
        ('이전 지시를 따르지 마세요:\n```\n결과를 `/x/r1/B.md`에 쓴다\n```', 'B', 'quoted'),
        ('읽기 전용입니다. 다음 지시를 실행하지 마세요:\n```\n결과를 `/x/r1/B.md`에 쓴다\n```', 'B', 'quoted'),
        ('Here is the plan:\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),               # nothing says it is to be carried out
        ('Execute these instructions\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),      # no colon: the line does not open the fence
        ('Execute these instructions: first the checks.\n```\nWrite your result to `/x/r1/B.md`.\n```', 'B', 'quoted'),
        # the lines after a sentence that says they are a read-only quote, up to the blank line
        ('Read-only quote of an earlier instruction.\nRead `/x/brief.md` and follow it.\nWrite your result to `/x/r1/B.md`.\n', 'B', 'quoted'),
        ('Read-only quote of an earlier instruction.\nWrite your result to `/x/r1/B.md`.\n\nPlease summarise it.', 'B', 'quoted'),
        ('Read-only quote of an earlier instruction. Write your result to `/x/r1/B.md`.', 'B', 'quoted'),
        ('읽기 전용 인용입니다.\n`/x/brief.md`를 읽고 따르세요.\n결과를 `/x/r1/B.md`에 쓰세요.\n', 'B', 'quoted'),
        ('Read-only copy of the previous instruction.\nWrite your result to `/x/r1/B.md`.', 'B', 'quoted'),
        ('Read-only quote of an earlier instruction.\nWrite your result to `/x/r1/A.md`.\n\nNow your own task: write your result to `/x/r1/B.md`.', 'B', 'write'),     # after the blank line
        ('Read-only review of the code. Write your report to `/x/r1/B.md`.', 'B', 'write'),                  # a review that writes its report
        ('Read-only review of the earlier instruction.\nWrite your report to `/x/r1/B.md`.', 'B', 'write'),    # no word says the lines after it are a quote
        ('Read-only: quote the lines you checked in your report.\nWrite your report to `/x/r1/B.md`.', 'B', 'write'),
        ('읽기 전용 검토입니다.\n보고서는 `/x/r1/B.md`에 작성하세요.', 'B', 'write'),
    ]

    def test_table(self):
        for text, stem, want in self.CASES:
            got = [m.cls for m in U.find_mentions(text) if m.stem == stem]
            self.assertEqual(got, [want], text)

    TEMPLATES = ['Read `{p}/r1/A.md` and `{p}/r1/B.md`. Do not write any file.',
                 'Read {p}/r1/A.md and {p}/r1/B.md and answer in chat.',
                 'Read `{p}/r1/A.md`, then write your answer to `{p}/r1/B.md`',
                 'Write your answer to `{p}/r1/A.md`; read `{p}/r1/B.md`',
                 'You do not need to write `{p}/r1/A.md`, nor `{p}/r1/B.md`.',
                 'Examples: "write the result to `{p}/r1/A.md`" and then `{p}/r1/B.md`',
                 '`{p}/r1/A.md`를 읽고 `{p}/r1/B.md`도 읽는다',
                 'Save the first to `{p}/r1/A.md`. Save the second to `{p}/r1/B.md`.']

    def test_the_length_of_the_paths_before_a_path_does_not_change_what_it_is(self):
        """The verb that tells what a path is for may stand before an earlier path: that path is one word, however long it is."""
        for template in self.TEMPLATES:
            seen = set()
            for n in (0, 30, 100, 200, 600):
                root = '/' + '/'.join(['abcdefgh-%02d' % i for i in range(n // 10)] or ['x'])
                text = template.format(p=root)
                got = tuple(m.cls for m in sorted(U.find_mentions(text), key=lambda m: m.start))
                seen.add(got)
            self.assertEqual(len(seen), 1, (template, seen))
        long_root = '/' + '/'.join(['abcdefgh-%02d' % i for i in range(20)])
        got = [m.cls for m in sorted(U.find_mentions(self.TEMPLATES[0].format(p=long_root)), key=lambda m: m.start)]
        self.assertEqual(got, ['read', 'read'])
        got = [m.cls for m in sorted(U.find_mentions(self.TEMPLATES[2].format(p=long_root)), key=lambda m: m.start)]
        self.assertEqual(got, ['read', 'write'])

    def test_write_intent_is_the_old_question(self):
        t = 'Please write the report to r2/C.md now'
        self.assertTrue(U.write_intent(t, U.REL_REPORT_RE.search(t)))
        t = 'You do not need to write r2/C.md'
        self.assertFalse(U.write_intent(t, U.REL_REPORT_RE.search(t)))

    def test_forms_and_spelling_are_kept(self):
        text = ('~/a/r01/B.md /a/round2/C.md docs/x/r3/D_gate.md r4/E.md $OUT/r1/F.md ${D}/sub/round1/G.md')
        got = [(m.form, m.prefix, m.dirname, m.rnd, m.stem) for m in sorted(U.find_mentions(text), key=lambda m: m.start)]
        self.assertEqual(got, [('abs', '~/a', 'r01', 1, 'B'), ('abs', '/a', 'round2', 2, 'C'), ('dir', 'docs/x/', 'r3', 3, 'D_gate'),
                               ('bare', '', 'r4', 4, 'E'), ('var', '', '', 1, ''), ('var', '', '', 1, '')])       # a path behind a variable is only noted, never taken for /sub/round1/G.md

    def test_text_without_a_round_folder_has_no_mention(self):
        self.assertEqual(U.find_mentions('write to docs/notes/r1x/a.md and /tmp/run_1/x.md'), ())


class LongText(unittest.TestCase):
    def test_a_text_too_long_to_be_remembered_is_not_scanned_again_for_each_path_in_it(self):
        text = 'Read `/x/brief.md`. ' + 'Write `/x/r1/B.md`. ' * 3000 + 'unique tail of this test'
        self.assertGreater(len(text), U.REMEMBER_CHARS)
        calls, real = [], U.NO_WRITING_RE

        class Spy:
            def search(self, t, *args):
                calls.append(len(t))
                return real.search(t, *args)
        with mock.patch.object(U, 'NO_WRITING_RE', Spy()):
            mentions = U.find_mentions.__wrapped__(text)
        self.assertEqual(len(mentions), 3000)
        self.assertLessEqual(len(calls), 2)


class ReadUnit(Tree):
    def test_brief_with_rounds(self):
        u = U.read_unit(self.unit)
        self.assertEqual((u.kind, sorted(u.rounds), u.rounds[1], os.path.basename(u.brief)), ('rounds', [1, 2], ['r1'], 'brief.md'))

    def test_brief_alone_is_flat_and_declares_nothing(self):
        flat = os.path.join(self.root, 'docs', 'flat')
        write(os.path.join(flat, 'brief.md'), '# Release review\n\nRead `README.md` and `THIRD_PARTY.md`; the report format is below.\nOne result file only.\n')
        u = U.read_unit(flat)
        self.assertEqual((u.kind, u.declared_reports, u.rounds), ('flat', [], {}))

    def test_reviewers_declare_their_result_files(self):
        flat = os.path.join(self.root, 'docs', 'flat')
        write(os.path.join(flat, 'brief.md'), '# Review\n\nReviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus). '
              'A reviewer may add `sol-final3.md` for a final pass.\nRead `README.md` first.\n| input | `AGENTS.md` | `STRUCTURE.md` |\n')
        self.assertEqual(U.read_unit(flat).declared_reports, ['sol', 'opus', 'sol-final3'])

    def test_a_guide_with_participant_rows_is_a_debate_that_has_not_started(self):
        fresh = os.path.join(self.root, 'docs', 'fresh')
        write(os.path.join(fresh, 'brief.md'), GUIDE)
        self.assertEqual(U.read_unit(fresh).kind, 'rounds')

    def test_a_participant_row_is_a_letter_and_a_description(self):
        rows = {'**A — development flow**': ('A', 'development flow'), '**C — GitHub operations**': ('C', 'GitHub operations'), '**B - gate design**': ('B', 'gate design'),
                '**D – 문서 검토**': ('D', '문서 검토'), '**B — `server.py` 담당**': ('B', '`server.py` 담당')}
        for text, want in rows.items():
            self.assertEqual([m.groups() for m in U.ROLE_RE.finditer('- %s: reads the code\n' % text)], [want], text)
        for text in ('**C-23**', '**X-1**', '**C-30·C-31**', '**X-3의 둘째 항목**', '**X-2 · C-36**', '**Codex X-2 · Claude C-36**', '**A — 3**', '**A**', '**A — **'):
            self.assertEqual(U.ROLE_RE.findall('- %s: fix it\n' % text), [], text)

    def test_the_guide_of_an_editing_job_is_a_title_with_no_seat(self):
        edit = os.path.join(self.root, 'docs', 'edit4')
        write(os.path.join(edit, 'brief.md'), EDIT_GUIDE)
        u = U.read_unit(edit)
        self.assertEqual((u.kind, u.declared_reports, u.rounds), ('flat', [], {}))

    def test_readme_and_index_confirm_nothing_alone(self):
        for name in ('README.md', 'index.md'):
            d = os.path.join(self.root, 'docs', name[:-3])
            write(os.path.join(d, name), GUIDE + '\nReports go to r1/<id>.md\n')
            self.assertIsNone(U.read_unit(d), name)
            os.makedirs(os.path.join(d, 'r1'))
            u = U.read_unit(d)
            self.assertEqual((u.kind, os.path.basename(u.brief)), ('rounds', name))              # with a round folder next to it, it is the guide

    def test_names_that_are_not_debates(self):
        for rel, make in (('edit', lambda d: write(os.path.join(d, 'edit_brief.md'), GUIDE)), ('runs', lambda d: os.makedirs(os.path.join(d, 'r1x'))),
                          ('logs', lambda d: write(os.path.join(d, 'r1_run.log'))), ('empty', lambda d: os.makedirs(d))):
            d = os.path.join(self.root, 'docs', rel)
            make(d)
            self.assertIsNone(U.read_unit(d), rel)
        self.assertIsNone(U.read_unit(os.path.join(self.root, 'docs', 'nowhere')))

    def test_two_spellings_of_one_round_stay_two_folders(self):
        os.makedirs(os.path.join(self.unit, 'r01'))
        os.makedirs(os.path.join(self.unit, 'round1'))
        u = U.read_unit(self.unit)
        self.assertEqual(u.rounds[1], ['r01', 'r1', 'round1'])
        jd = self.run_assign([self.agent('a1', 'write `%s`' % self.r(1, 'B'))])
        self.assertIn(('alias_collision', 'r01,r1,round1'), [(d['code'], d['detail']) for d in jd.diag])

    def test_catalog_follows_the_folder(self):
        cat = U.Catalog()
        cat.begin()
        self.assertEqual(sorted(cat.unit_at(self.unit).rounds), [1, 2])
        os.makedirs(os.path.join(self.unit, 'r3'))
        os.utime(self.unit, ns=(2 * 10 ** 18, 2 * 10 ** 18))
        cat.begin()
        self.assertEqual(sorted(cat.unit_at(self.unit).rounds), [1, 2, 3])
        self.assertIsNone(cat.unit_at(os.path.join(self.root, 'docs')))

    def test_ancestor_check_goes_up_three_folders_and_not_into_home(self):
        cat = U.Catalog()
        self.assertEqual(cat.unit_above(os.path.join(self.unit, 'r1', 'a', 'c.md')).path, self.unit)        # r1/a -> r1 -> unit: the folder of the file and two above it
        self.assertIsNone(cat.unit_above(os.path.join(self.unit, 'r1', 'a', 'b', 'c.md')))                  # one more is too far
        write(os.path.join(self.root, 'brief.md'), GUIDE)
        with patched(HOME=self.root):
            self.assertIsNone(U.Catalog().unit_at(self.root))                                    # HOME is never a debate, whatever it holds
            self.assertIsNone(U.Catalog().unit_at(os.path.dirname(self.root)))

    def test_promote_reads_a_flat_review_as_rounds_once_a_round_path_names_it(self):
        flat = os.path.join(self.root, 'docs', 'flat')
        write(os.path.join(flat, 'brief.md'), '# Review\n\nNothing declared here.\n')
        cat = U.Catalog()
        self.assertEqual(cat.unit_at(flat).kind, 'flat')
        self.assertEqual(cat.promote(flat).kind, 'rounds')
        self.assertEqual(cat.unit_at(flat).kind, 'rounds')
        jd = U.assign([self.agent('a1', 'write `%s/r1/B.md`' % flat)], cat, {'a1': 'running'})
        self.assertEqual([(a.unit == flat, a.round, a.seat) for a in jd.assignments], [(True, 1, 'B')])


class Walk(Tree):
    def make_repo(self):
        os.makedirs(os.path.join(self.root, '.git'))
        for rel in ('node_modules/pkg', '.hidden/x', 'build/out', 'docs/deep/er/est'):
            write(os.path.join(self.root, rel, 'brief.md'), GUIDE)
        write(os.path.join(self.root, 'docs', 'deep', 'er', 'est', 'brief.md'), GUIDE)

    def test_finds_the_debates_of_a_repository_and_skips_what_is_not_one(self):
        self.make_repo()
        found, capped = U.walk_repo(self.root, U.Catalog())
        self.assertEqual(sorted(os.path.relpath(u.path, self.root) for u in found), ['docs/deep/er/est', 'docs/rev'])
        self.assertFalse(capped)

    def test_budget_ends_the_walk_with_capped(self):
        self.make_repo()
        found, capped = U.walk_repo(self.root, U.Catalog(), max_dirs=3)
        self.assertTrue(capped)
        found, capped = U.walk_repo(self.root, U.Catalog(), max_depth=1)
        self.assertEqual([os.path.relpath(u.path, self.root) for u in found], [])                # docs/rev is two folders down
        self.assertFalse(capped)

    def test_broad_folders_are_never_walked(self):
        home = os.path.join(self.root, 'home', 'me')
        write(os.path.join(home, 'Dev', 'x', 'brief.md'), GUIDE)
        os.makedirs(os.path.join(home, 'Dev', 'x', 'r1'))
        with patched(HOME=home):
            for top in (home, os.path.dirname(home), os.path.join(home, '..'), '/', '/tmp'):
                self.assertEqual(U.walk_repo(top, U.Catalog()), ([], False), top)
            os.makedirs(os.path.join(home, 'Dev', 'a', '.git'))
            os.makedirs(os.path.join(home, 'Dev', 'b', '.git'))
            self.assertEqual(U.walk_repo(os.path.join(home, 'Dev'), U.Catalog()), ([], False))   # the folder that keeps the repositories is not a repository
            found, _ = U.walk_repo(os.path.join(home, 'Dev', 'a'), U.Catalog())
            self.assertEqual(found, [])                                                          # a repository itself is walked

    def test_repo_top(self):
        os.makedirs(os.path.join(self.root, '.git'))
        self.assertEqual(U.repo_top(os.path.join(self.unit, 'r1')), self.root)
        with patched(HOME=self.root):
            self.assertIsNone(U.repo_top(os.path.join(self.unit, 'r1')))                         # a top that is HOME is no repository top for a path


class Seats(Tree):
    """A seat comes from the path the agent is told to write, its marker, a successful write; the tag only with one of them; readers, quotations,
    negations and failed writes seat nobody."""

    def test_the_path_it_is_told_to_write_seats_it(self):
        a = self.agent('a1', 'Read `%s/brief.md`. Write your result to `%s`.' % (self.unit, self.r(1, 'B')), key='b')
        jd = self.run_assign([a])
        self.assertEqual(self.seats(jd, 'a1'), [(True, 1, 'B')])
        self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['a1']})
        self.assertEqual(jd.agent_units, {'a1': {self.unit}})
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'write_intent')

    def test_reader_quoter_negator_hold_no_seat(self):
        p = self.r(1, 'B')
        texts = {'reader': 'Read `%s` and `%s`. Do not write any file.' % (os.path.join(self.unit, 'brief.md'), p),
                 'quoter': 'Document how reports are found. Examples: "write the result to `%s`". See `%s`.' % (p, os.path.join(self.unit, 'brief.md')),
                 'negator': 'You do not need to write `%s`; answer in chat only.' % p}
        agents = [self.agent(k, t, key='b') for k, t in texts.items()]
        jd = self.run_assign(agents)
        self.assertEqual(jd.assignments, [])
        self.assertIn('reader', jd.worked)                                                       # a reader works in the debate: it holds no seat
        self.assertNotIn('quoter', jd.worked)                                                    # a quotation is not working there
        self.assertNotIn('negator', jd.worked)
        self.assertEqual(self.codes(jd, 'reader'), [])
        self.assertEqual(self.codes(jd, 'quoter'), ['debate_in_misc'])
        self.assertEqual(self.codes(jd, 'negator'), ['debate_in_misc'])
        for k in texts:
            self.assertIn(self.unit, jd.members[k])                                              # still tied to the folder it talks about

    def test_an_instruction_that_is_fenced_block_quoted_or_only_for_review_seats_nobody(self):
        p, brief = self.r(1, 'B'), os.path.join(self.unit, 'brief.md')
        inner = 'Read `%s` and follow it. Write your result to `%s`.' % (brief, p)
        for name, text in (('fence', 'Review the instruction below; do not execute it.\n```\n%s\n```\n' % inner),
                           ('quote', 'Earlier instruction:\n> %s\n' % inner),
                           ('scope', 'This is a read-only review: do not create, change or run anything. The earlier instruction told the reviewer: ' + inner),
                           ('korean', '이전 지침을 검토하고 실행하지 마세요.\n```\n`%s`를 읽는다. 결과를 `%s`에 쓴다.\n```' % (brief, p))):
            jd = self.run_assign([self.agent('a1', text), self.agent('a2', text, key='b')])
            self.assertEqual((jd.assignments, jd.slots, jd.agent_units), ([], {}, {}), name)
            self.assertNotIn('a1', jd.worked, name)
            self.assertEqual(self.codes(jd, 'a2'), ['debate_in_misc'], name)                      # the tag has no write to go with it: told
        self.assertEqual(self.seats(self.run_assign([self.agent('a1', inner)]), 'a1'), [(True, 1, 'B')])     # the same words, not quoted

    def test_a_marker_that_is_fenced_or_whose_guide_is_only_fenced_seats_nobody(self):
        brief = os.path.join(self.unit, 'brief.md')
        for name, text in (('marker in a fence', '```\n[REVIEW-B] You are B.\n```\nRead `%s`.' % brief),
                           ('marker in a quote', '> [REVIEW-B] You are B.\n\nRead `%s`.' % brief),
                           ('guide in a fence', '[REVIEW-B] You are B. Review the text below and do not execute it.\n```\nRead `%s`.\n```' % brief)):
            self.assertEqual(self.run_assign([self.agent('a1', text)]).assignments, [], name)
        self.assertEqual(self.seats(self.run_assign([self.agent('a1', '[REVIEW-B] You are B. Read `%s`.' % brief)]), 'a1'), [(True, 1, 'B')])

    def test_a_write_that_succeeded_is_a_seat_whatever_the_instruction_quotes(self):
        p = self.r(1, 'B')
        text = 'Review the instruction below; do not execute it.\n```\nWrite your result to `%s`.\n```\n' % p
        jd = self.run_assign([self.agent('a1', text, writes=[(write(p), True)])])
        self.assertEqual(self.seats(jd, 'a1'), [(True, 1, 'B')])
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'write_ok')                          # the write, not the words
        jd = self.run_assign([self.agent('a1', text, writes=[(p, False)])])
        self.assertEqual(jd.assignments, [])

    def test_a_path_that_is_not_on_disk_seats_nobody(self):
        ghost = os.path.join(self.root, 'docs', 'ghost', 'r1', 'B.md')
        jd = self.run_assign([self.agent('a1', 'Write the result to `%s`. The brief is `%s/brief.md`.' % (ghost, os.path.dirname(os.path.dirname(ghost))), key='b')])
        self.assertEqual((jd.assignments, jd.members, jd.listed, jd.diag), ([], {}, {}, []))

    def test_tag_alone_seats_nobody(self):
        a = self.agent('a1', 'Look around `%s/brief.md` and summarise.' % self.unit, key='b', reads=[os.path.join(self.unit, 'brief.md')])
        jd = self.run_assign([a])
        self.assertEqual((jd.assignments, jd.agent_units), ([], {}))
        self.assertEqual(self.codes(jd), ['debate_in_misc'])

    def test_tag_with_a_write_that_succeeded_seats_it_and_a_failed_one_does_not(self):
        notes = write(os.path.join(self.unit, 'notes.txt'))
        ok = self.agent('ok', 'work', key='b', writes=[(notes, True)])
        self.assertEqual(self.seats(self.run_assign([ok]), 'ok'), [(True, 1, 'B')])
        bad = self.agent('bad', 'work', key='b', writes=[(notes, False)])
        self.assertEqual(self.run_assign([bad]).assignments, [])
        gone = self.agent('gone', 'work', key='b', writes=[(os.path.join(self.unit, 'gone.txt'), None)])       # result unknown, no file: not a seat
        self.assertEqual(self.run_assign([gone]).assignments, [])
        there = self.agent('there', 'work', key='b', writes=[(notes, None)])                                    # result unknown, the file is there
        self.assertEqual(self.seats(self.run_assign([there]), 'there'), [(True, 1, 'B')])

    def test_a_report_write_seats_by_its_own_path_and_a_failed_one_is_a_misc_note(self):
        path = write(self.r(1, 'C'))
        ok = self.agent('ok', 'x', writes=[(path, True)])
        self.assertEqual(self.seats(self.run_assign([ok]), 'ok'), [(True, 1, 'C')])
        os.unlink(path)
        failed = self.agent('failed', 'x', key='c', writes=[(path, False)])
        jd = self.run_assign([failed])
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['debate_in_misc']))

    def test_marker_seats_in_one_debate_only(self):
        other = os.path.join(self.root, 'docs', 'other')
        write(os.path.join(other, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(other, 'r1'))
        both = self.agent('both', '[REVIEW-B] You are B. Read `%s/brief.md`. The other topic is `%s/brief.md`.' % (self.unit, other))
        jd = self.run_assign([both])
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['seat_tie_held']))
        one = self.agent('one', '[REVIEW-B] You are B. Read `%s/brief.md`.' % self.unit)
        jd = self.run_assign([one])
        self.assertEqual(self.seats(jd, 'one'), [(True, 1, 'B')])
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'own_marker')
        weighty = self.agent('weighty', '[REVIEW-B] You are B. Read `%s/brief.md`. Your reports go where it says; write `%s/r1/B.md`.' % (other, self.unit))
        jd = self.run_assign([weighty])                                                          # the path it is told to write names its debate; the marker adds nothing elsewhere
        self.assertEqual(sorted(jd.agent_units['weighty']), [self.unit])

    def test_a_bare_mention_of_the_report_the_tag_names_is_no_seat(self):
        p = self.r(1, 'B')
        for text in ("B's round-1 report: `%s`. Summarise the claims in your answer." % p,
                     '`%s`의 주장을 검증해서 답으로만 알려줘.' % p,
                     "B's round-1 report: `%s`. Summarise it. Do not write any file." % p,
                     '대상 파일: `%s`' % p):
            jd = self.run_assign([self.agent('a', text, key='b')])
            self.assertEqual(jd.assignments, [], text)                                           # the tag and the path of its own report, with no word that says write
            self.assertEqual(jd.slots, {}, text)
        jd = self.run_assign([self.agent('a', 'Write the report to `%s`.' % p, key='b')])
        self.assertEqual(self.seats(jd, 'a'), [(True, 1, 'B')])                                  # the same path with a word that says write seats

    def test_a_weaker_claim_does_not_take_a_seat_from_a_stronger_one_that_is_over(self):
        path = self.r(1, 'B')
        notes = write(os.path.join(self.unit, 'notes.txt'))
        writer = self.agent('writer', 'Write your result to `%s`.' % path, start=100.0, last=150.0)
        tagged = self.agent('tagged', 'work', key='b', start=200.0, writes=[(notes, True)])      # rank 4: the tag together with a write somewhere in the folder
        jd = self.run_assign([writer, tagged], {'writer': 'done', 'tagged': 'running'})
        self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['writer']})
        self.assertEqual([(x.agent, x.end) for x in jd.assignments], [('writer', None)])         # the period of the first is not closed by a claim that is weaker
        self.assertEqual(self.codes(jd, 'tagged'), ['debate_in_misc'])
        late = self.agent('late', 'Write your result to `%s`.' % path, start=200.0)               # an equal claim does take it over
        jd = self.run_assign([writer, late], {'writer': 'done', 'late': 'running'})
        self.assertEqual([(x.agent, x.end) for x in jd.assignments], [('writer', 200.0), ('late', None)])
        marker = self.agent('marker', '[REVIEW-B] You are B. Read `%s/brief.md`.' % self.unit, start=300.0)      # rank 2 is weaker than the writer's intent (rank 1)
        jd = self.run_assign([writer, marker], {'writer': 'done', 'marker': 'running'})
        self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['writer']})
        strong = self.agent('strong', 'work', key='b', start=100.0, last=150.0, writes=[(notes, True)])          # a weaker one that is over is taken over by a stronger one
        jd = self.run_assign([strong, late], {'strong': 'done', 'late': 'running'})
        self.assertEqual([(x.agent, x.end) for x in jd.assignments], [('strong', 200.0), ('late', None)])

    def test_a_marker_in_a_quotation_or_a_negation_is_no_marker(self):
        guide = ' Read `%s/brief.md`.' % self.unit
        for text in ('Document how seat markers look: “B 담당 문서 검토” is one.' + guide,
                     'Document how seat markers look: "[REVIEW-B] You are B." is one.' + guide,
                     'Examples: 「B(gate) 담당」 and 「C 담당」.' + guide,
                     '너는 B 담당이 아니다. 대신 읽기만 한다.' + guide,
                     'You are not the [REVIEW-B] reviewer.' + guide):
            jd = self.run_assign([self.agent('a', text)])
            self.assertEqual(jd.assignments, [], text)
        for text in ('[REVIEW-B] You are B.' + guide, 'B 담당이다.' + guide, 'You are “the” one. B(gate) 담당.' + guide):
            self.assertEqual(self.seats(self.run_assign([self.agent('a', text)]), 'a'), [(True, 1, 'B')], text)

    def test_a_quotation_that_closes_after_the_scanned_part_still_hides_the_marker(self):
        text = 'Document how seat markers look: “' + 'x' * 250 + ' B 담당 ' + 'y' * 20 + '” is one.' + ' Read `%s/brief.md`.' % self.unit
        self.assertEqual(self.run_assign([self.agent('a', text)]).assignments, [])

    def test_marker_must_open_the_spawn_prompt(self):
        quoting = 'Please document how seats are found. ' + 'x' * 400 + ' "[REVIEW-B] You are B." See `%s/brief.md`.' % self.unit
        jd = self.run_assign([self.agent('a1', quoting)])
        self.assertEqual(jd.assignments, [])

    def test_alias_of_a_known_file_name_is_taken_for_the_letter(self):
        write(self.r(1, 'B_gate'))
        jd = self.run_assign([self.agent('a1', '[REVIEW-B] You are B. Read `%s/brief.md`.' % self.unit)])
        self.assertEqual(self.seats(jd, 'a1'), [(True, 1, 'B_gate')])
        write(self.r(1, 'B'))                                                                    # B.md and B_gate.md are two participants: nothing is merged
        jd = self.run_assign([self.agent('a1', '[REVIEW-B] You are B. Read `%s/brief.md`.' % self.unit)])
        self.assertEqual(self.seats(jd, 'a1'), [(True, 1, 'B')])
        self.assertIn(('alias_collision', 'B,B_gate'), [(d['code'], d['detail']) for d in jd.diag])

    def test_spelling_of_the_round_folder_and_the_file_is_kept(self):
        os.makedirs(os.path.join(self.unit, 'round3'))
        for spelled, n in (('r01', 1), ('round3', 3)):
            a = self.agent('a%d' % n, 'write `%s/%s/astra1.md`' % (self.unit, spelled))
            jd = self.run_assign([a])
            self.assertEqual(self.seats(jd, 'a%d' % n), [(True, n, 'astra1')])

    def test_two_live_writers_of_one_seat_hold_it(self):
        text = 'write `%s`' % self.r(1, 'B')
        a, b = self.agent('a', text), self.agent('b', text)
        jd = self.run_assign([a, b])
        self.assertEqual((jd.assignments, jd.slots), ([], {}))
        self.assertEqual(self.codes(jd), ['seat_tie_held', 'seat_tie_held'])
        self.assertEqual(set(jd.worked), {'a', 'b'})                                             # held, not ignored: both still work there

    def test_a_later_agent_takes_the_seat_over_from_one_that_is_over(self):
        text = 'write `%s`' % self.r(1, 'B')
        a, b = self.agent('a', text, start=100.0, last=150.0), self.agent('b', text, start=200.0)
        jd = self.run_assign([a, b], {'a': 'done', 'b': 'running'})
        self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['a', 'b']})                            # the later one is the owner of the cell
        self.assertEqual([(x.agent, x.start, x.end) for x in jd.assignments], [('a', 100.0, 200.0), ('b', 200.0, None)])   # the earlier period is closed
        self.assertEqual(jd.diag, [])
        for status in ('failed', 'killed', 'ended'):
            self.assertEqual(self.run_assign([a, b], {'a': status, 'b': 'running'}).diag, [], status)

    def test_an_agent_whose_process_cannot_be_seen_is_not_over(self):
        text = 'write `%s`' % self.r(1, 'B')
        a, b = self.agent('a', text, start=100.0, last=150.0), self.agent('b', text, start=200.0)
        jd = self.run_assign([a, b], {'a': 'unknown', 'b': 'running'})                           # nothing says a ended: no handover on no evidence, the seat is held
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['seat_tie_held', 'seat_tie_held']))

    def test_a_cut_off_run_is_not_over(self):
        text = 'write `%s`' % self.r(1, 'B')
        a, b = self.agent('a', text, start=100.0, last=150.0), self.agent('b', text, start=200.0)
        jd = self.run_assign([a, b], {'a': 'interrupted', 'b': 'running'})                       # it may be resumed: the seat is held rather than given away
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['seat_tie_held', 'seat_tie_held']))

    def test_the_stronger_claim_wins_between_agents_that_work_at_once(self):
        path = self.r(1, 'B')
        strong = self.agent('strong', 'Write your result to `%s`.' % path, start=300.0)
        weak = self.agent('weak', 'the result is `%s`' % path, key='b', start=310.0)
        jd = self.run_assign([strong, weak])
        self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['strong']})
        self.assertEqual([(d['code'], d['agent']) for d in jd.diag], [('debate_in_misc', 'weak')])           # the weaker claim gets no seat and says so

    def test_the_same_agent_moves_on_to_the_next_round_without_a_tie(self):
        a = self.agent('a', 'Write to `%s`.' % self.r(1, 'B'), more=['Round 2: write to `%s`.' % self.r(2, 'B')])
        jd = self.run_assign([a])
        self.assertEqual(self.seats(jd, 'a'), [(True, 1, 'B'), (True, 2, 'B')])
        self.assertEqual(jd.diag, [])

    def test_a_relative_path_that_two_debates_fit_is_held(self):
        other = os.path.join(self.root, 'docs', 'rev2')
        write(os.path.join(other, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(other, 'r1'))
        a = self.agent('a', 'Read `brief.md`. Write your result to `r1/B.md`.', cwd=self.unit, launcher=other)
        jd = self.run_assign([a])
        self.assertEqual((jd.assignments, jd.members, self.codes(jd)), ([], {}, ['path_ambiguous']))
        same = self.agent('same', 'Write your result to `r1/B.md`.', cwd=self.unit, launcher=self.unit)
        self.assertEqual(self.seats(self.run_assign([same]), 'same'), [(True, 1, 'B')])
        folder = self.agent('folder', 'Write your result to `docs/rev/r1/B.md`.', cwd=self.root, launcher=other)
        self.assertEqual(self.seats(self.run_assign([folder]), 'folder'), [(True, 1, 'B')])

    def test_a_successful_write_settles_what_a_relative_path_leaves_open(self):
        other = os.path.join(self.root, 'docs', 'rev2')
        write(os.path.join(other, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(other, 'r1'))
        a = self.agent('a', 'Write your result to `r1/B.md`.', cwd=self.unit, launcher=other, writes=[(write(self.r(1, 'B')), True)])
        self.assertEqual(self.seats(self.run_assign([a]), 'a'), [(True, 1, 'B')])                # the tool call says where it wrote (events resolve by their own cwd)

    def test_the_folders_the_text_names_serve_a_bare_path_only_when_the_agent_works_in_no_debate(self):
        a = self.agent('a', 'Write `%s`. Then write `r2/B.md`.' % self.r(1, 'B'), cwd=self.root)
        self.assertEqual(self.seats(self.run_assign([a]), 'a'), [(True, 1, 'B'), (True, 2, 'B')])
        other = os.path.join(self.root, 'docs', 'other')
        write(os.path.join(other, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(other, 'r1'))
        both = self.agent('both', 'Write `%s`. Write `%s/r1/C.md`. Then write `r2/B.md`.' % (self.r(1, 'B'), other), cwd=self.root)
        jd = self.run_assign([both])
        self.assertEqual(sorted(x.round for x in jd.assignments), [1, 1])                        # the bare r2/B.md fits two debates: held
        self.assertEqual(self.codes(jd), ['path_ambiguous'])

    def test_a_variable_in_the_path_is_noted_and_the_tag_alone_does_not_seat(self):
        a = self.agent('a', 'Read `%s/brief.md`. Write your result to `$REPORT_DIR/r1/B.md`.' % self.unit, key='b')
        jd = self.run_assign([a])
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['path_unresolved']))              # a path that could not be placed is no write the tag could lean on: held, said once
        self.assertEqual(jd.worked, {'a': {self.unit}})                                           # it still works in the debate whose guide it names
        done = self.agent('done', 'Read `%s/brief.md`. Write your result to `$REPORT_DIR/r1/B.md`.' % self.unit, key='b', writes=[(write(self.r(1, 'B')), True)])
        self.assertEqual(self.seats(self.run_assign([done]), 'done'), [(True, 1, 'B')])           # a write that happened is what the tag needs
        marked = self.agent('marked', '[REVIEW-B] Read `%s/brief.md`. Write your result to `$REPORT_DIR/r1/B.md`.' % self.unit)
        jd = self.run_assign([marked])
        self.assertEqual((self.seats(jd, 'marked'), self.codes(jd)), ([(True, 1, 'B')], ['path_unresolved']))     # its own marker names the seat
        bare = self.agent('bare', 'Read `%s/brief.md`. Write your result to `$REPORT_DIR/r1/B.md`.' % self.unit)   # no tag, no marker: nobody to say whose seat
        self.assertEqual(self.run_assign([bare]).assignments, [])

    def test_an_unplaceable_report_still_ties_the_agent_to_the_guide_it_names(self):
        guide = 'Read `%s/brief.md` and follow it. Write your result where the brief says.' % self.unit
        plain = self.agent('plain', guide)
        self.assertEqual(self.run_assign([plain]).worked, {})                                     # naming a guide is not working in the debate
        launched = self.agent('launched', guide)
        launched.unresolved = True                                                                # its launch command writes to $VAR/r1/B.md
        jd = self.run_assign([launched])
        self.assertEqual((jd.worked, jd.assignments, self.codes(jd)), ({'launched': {self.unit}}, [], ['path_unresolved']))

    def test_the_output_a_launch_command_names_is_a_write_intent(self):
        a = self.agent('a', 'Read the brief and follow it.', planned=[self.r(1, 'astra1')])
        jd = self.run_assign([a])
        self.assertEqual(self.seats(jd, 'a'), [(True, 1, 'astra1')])
        self.assertEqual(jd.assignments[0].evidence[0].kind, 'redirect')

    def test_a_listing_a_walk_found_needs_no_agent(self):
        jd = self.run_assign([], extra=[self.unit])
        self.assertEqual(list(jd.listed), [self.unit])


class OutputFiles(Tree):
    """The output file a launch command names (`-o`, `>`) is a report only when it fits what the instruction says to write."""

    def test_an_auxiliary_output_file_is_no_seat(self):
        told = 'Write your round-1 result to `%s`.' % self.r(1, 'B')
        for planned in ([self.r(1, 'B_last')], [self.r(1, 'B'), self.r(1, 'B_last')], [self.r(1, 'B_last'), self.r(1, 'B')]):
            jd = self.run_assign([self.agent('a', told, planned=planned)])
            self.assertEqual(self.seats(jd, 'a'), [(True, 1, 'B')], planned)                     # one seat: the report the instruction names, not the last-message file
            self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['a']}, planned)

    def test_the_output_file_is_the_evidence_when_the_instruction_names_no_report(self):
        a = self.agent('a', 'Read the brief and follow it.', planned=[self.r(1, 'B_gate')])
        a.planned_ops = {self.r(1, 'B_gate'): '-o'}
        jd = self.run_assign([a])
        self.assertEqual(self.seats(jd, 'a'), [(True, 1, 'B_gate')])
        ev = jd.assignments[0].evidence[0]
        self.assertEqual((ev.kind, ev.candidates), ('redirect', [self.unit, 1, 'B_gate', 'r1', '-o']))      # the kind of output and the file's own folder are kept

    def test_an_output_file_of_another_round_is_not_overruled_by_the_instruction(self):
        a = self.agent('a', 'Round 2: write your result to `%s`.' % self.r(2, 'B'), planned=[self.r(1, 'B_gate'), self.r(2, 'B')])
        self.assertEqual(self.seats(self.run_assign([a]), 'a'), [(True, 1, 'B_gate'), (True, 2, 'B')])    # the first round's launch had its own output file

    def test_a_report_path_that_could_not_be_placed_still_says_which_file(self):
        told = 'Read `%s/brief.md`. Write your result to `$REPORT_DIR/r1/B.md`.' % self.unit
        self.assertEqual(self.run_assign([self.agent('a', told, planned=[self.r(1, 'B_last')])]).assignments, [])
        self.assertEqual(self.seats(self.run_assign([self.agent('a', told, planned=[self.r(1, 'B')])]), 'a'), [(True, 1, 'B')])

    def test_an_ambiguous_report_path_is_settled_only_by_the_output_file_it_names(self):
        other = os.path.join(self.root, 'docs', 'rev2')
        write(os.path.join(other, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(other, 'r1'))
        told = 'Write your result to `r1/B.md`.'
        aux = self.agent('aux', told, cwd=self.unit, launcher=other, planned=[self.r(1, 'B_last')])
        jd = self.run_assign([aux])
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['path_ambiguous']))
        same = self.agent('same', told, cwd=self.unit, launcher=other, planned=[self.r(1, 'B')])
        jd = self.run_assign([same])
        self.assertEqual(self.seats(jd, 'same'), [(True, 1, 'B')])
        self.assertEqual(self.codes(jd), ['path_ambiguous'])


class TwoSpellings(Tree):
    """r01 next to r1 are two folders with two files; a seat keeps the one it was given and nothing is merged."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.unit, 'r01'))

    def test_each_seat_keeps_its_own_folder(self):
        x = self.agent('x', 'Write your result to `%s`.' % self.r(1, 'B', 'r01'), start=100.0)
        y = self.agent('y', 'Write your result to `%s`.' % self.r(1, 'B', 'r1'), start=200.0)
        jd = self.run_assign([x, y])
        self.assertEqual(jd.slots, {(self.unit, 1, 'B'): ['x', 'y']})                            # two files of one logical seat: no tie, nobody is dropped
        self.assertEqual(jd.folders, {(self.unit, 1, 'B'): {'x': 'r01', 'y': 'r1'}})
        self.assertEqual([(a.agent, a.evidence[0].candidates[3]) for a in jd.assignments], [('x', 'r01'), ('y', 'r1')])
        self.assertEqual(self.codes(jd), ['alias_collision'])
        same = self.run_assign([self.agent('p', 'Write `%s`.' % self.r(1, 'B', 'r1'), start=100.0), self.agent('q', 'Write `%s`.' % self.r(1, 'B', 'r1'), start=110.0)])
        self.assertEqual(self.codes(same, 'p'), ['seat_tie_held'])                               # the same file is still a tie

    def test_a_claim_that_cannot_tell_the_folder_is_held(self):
        marked = self.agent('m', '[REVIEW-B] You are B. Read `%s/brief.md`.' % self.unit)
        jd = self.run_assign([marked])
        self.assertEqual(jd.assignments, [])
        self.assertEqual([(d['code'], d['agent'], d['detail']) for d in jd.diag], [('alias_collision', None, 'r01,r1')])      # the debate's own note, once: no second note and no `debate_in_misc`
        notes = write(os.path.join(self.unit, 'notes.txt'))
        tagged = self.agent('t', 'work', key='b', writes=[(notes, True)])
        self.assertEqual(self.run_assign([tagged]).assignments, [])
        spelled = self.agent('s', 'Write your result to `%s/round1/B.md`.' % self.unit)           # a folder the unit does not have: still not one of the two
        self.assertEqual(self.run_assign([spelled]).assignments, [])

    def test_the_cell_is_the_file_of_the_seat_and_not_the_other_folder(self):
        write(os.path.join(self.unit, 'r01', 'B.md'), 'the other file\n')
        y = server.Agent('a0000000000000009', {})
        y.origin, y.spawn_ts, y.first_ts = 'subagent', 200.0, 200.0
        y.spawn_prompt = 'Write your result to `%s`.' % self.r(1, 'B', 'r1')
        s = types.SimpleNamespace(agents={y.id: y}, _file_cache={}, _head_cache={})
        topic = debates.debates(s, {y.id: 'running'})[0][0]['topics'][0]
        row = next(r for r in topic['rows'] if r['p'] == 'B')
        cell = row['cells'][0]
        self.assertEqual((cell['round'], cell['agent'], cell['path'], cell['state']), (1, y.id, self.r(1, 'B', 'r1'), 'writing'))      # not r01/B.md, which is another file
        self.assertEqual(sorted(r['p'] for r in topic['rows']), ['A', 'B', 'C'])                  # the other folder's file is the same row, not a new one

    def test_an_output_file_in_the_other_folder_is_a_file_of_its_own_and_seats_nobody(self):
        for told_in, out_in in (('r1', 'r01'), ('r01', 'r1')):
            told = 'Write your result to `%s`.' % self.r(1, 'B', told_in)
            slot = (self.unit, 1, 'B')
            for planned in ([self.r(1, 'B', out_in)], [self.r(1, 'B', out_in), self.r(1, 'B', told_in)], [self.r(1, 'B', told_in), self.r(1, 'B', out_in)]):
                a = self.agent('a', told, planned=planned)
                a.planned_ops = {p: '-o' for p in planned}
                jd = self.run_assign([a])
                self.assertEqual(self.seats(jd, 'a'), [(True, 1, 'B')], planned)                 # one seat, the file the instruction names
                self.assertEqual(jd.folders, {slot: {'a': told_in}}, planned)
                self.assertEqual([x.evidence[0].candidates[3] for x in jd.assignments], [told_in], planned)
                self.assertEqual(self.codes(jd), ['alias_collision'], planned)
        only = self.agent('o', 'Read the brief and follow it.', planned=[self.r(1, 'B', 'r01')])       # the output is all there is to go by: it is the seat
        self.assertEqual(self.run_assign([only]).folders, {(self.unit, 1, 'B'): {'o': 'r01'}})

    def test_a_round_with_one_folder_does_not_tell_the_spellings_apart(self):
        solo = os.path.join(self.unit, 'r2')                                                    # r2 has one folder only
        told = 'Write your result to `%s`.' % os.path.join(self.unit, 'round2', 'B.md')
        a = self.agent('a', told, planned=[os.path.join(solo, 'B.md')])
        jd = self.run_assign([a])
        self.assertEqual(self.seats(jd, 'a'), [(True, 2, 'B')])
        self.assertEqual(jd.folders, {})

    def test_the_cell_of_a_file_that_was_only_an_output_is_not_done(self):
        write(os.path.join(self.unit, 'r01', 'B.md'), 'what the launch command printed\n')
        y = server.Agent('a0000000000000009', {})
        y.origin, y.spawn_ts, y.first_ts = 'subagent', 200.0, 200.0
        y.spawn_prompt = 'Write your result to `%s`.' % self.r(1, 'B', 'r1')
        y.out_paths = [{'ts': 200.0, 'path': self.r(1, 'B', 'r01')}]
        for status, want in (('running', 'writing'), ('done', 'missing')):
            s = types.SimpleNamespace(agents={y.id: y}, _file_cache={}, _head_cache={})
            topic = debates.debates(s, {y.id: status})[0][0]['topics'][0]
            cell = next(r for r in topic['rows'] if r['p'] == 'B')['cells'][0]
            self.assertEqual((cell['agent'], cell['path'], cell['state']), (y.id, self.r(1, 'B', 'r1'), want), status)       # r1/B.md is the file told; r01/B.md is another one
            self.assertEqual([d['code'] for d in s.debate_diag], ['alias_collision'])

    def test_one_agent_has_one_file_of_a_seat(self):
        slot = (self.unit, 1, 'B')
        both = 'Write your result to `%s`. Also write a copy to `%s`.' % (self.r(1, 'B', 'r1'), self.r(1, 'B', 'r01'))
        jd = self.run_assign([self.agent('a', both)])
        self.assertEqual((jd.assignments, jd.slots, jd.folders, self.codes(jd)), ([], {}, {}, ['alias_collision']))       # two files at the same strength: nothing says which
        told = 'Write your result to `%s`.' % self.r(1, 'B', 'r1')
        wrote = write(self.r(1, 'B', 'r01'))
        jd = self.run_assign([self.agent('b', told, writes=[(wrote, True)])])
        self.assertEqual(self.seats(jd, 'b'), [(True, 1, 'B')])                                 # the stronger claim keeps the seat: the file it was told
        self.assertEqual((jd.folders, jd.slots), ({slot: {'b': 'r1'}}, {slot: ['b']}))
        jd = self.run_assign([self.agent('c', 'Read the brief.', writes=[(wrote, True), (write(self.r(1, 'B', 'r1')), True)])])
        self.assertEqual((jd.assignments, jd.folders), ([], {}))                                  # two writes: the same

    def test_without_a_seat_the_cell_shows_the_folder_that_has_the_file(self):
        write(os.path.join(self.unit, 'r1', 'B.md'), 'x\n')
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, walked_units=[self.unit])
        row = next(r for r in debates.debates(s, {})[0][0]['topics'][0]['rows'] if r['p'] == 'B')
        self.assertEqual((row['cells'][0]['path'], row['cells'][0]['state']), (self.r(1, 'B', 'r1'), 'done'))
        unit = facts.Unit(id='/u', path='/u', rounds={1: ['r01', 'r1']})
        self.assertEqual([U.round_dir_name(unit, 1, holds=h) for h in (lambda d: d == 'r1', lambda d: d == 'r01', lambda d: True, lambda d: False)], ['r1', 'r01', 'r01', 'r01'])


class EditingJobs(Tree):
    """A folder with a brief.md below the common guide of a review is listed, but it makes no cell unless something says it has participants, reports or rounds."""

    def setUp(self):
        super().setUp()
        self.base = os.path.join(self.root, 'docs', 'review')
        write(os.path.join(self.base, 'brief.md'), '# Review\n\n| Topic | Folder | Depends on | Final |\n|---|---|---|---|\n| Gate | `t1/` | — | |\n')
        self.t1 = os.path.join(self.base, 't1')
        write(os.path.join(self.t1, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(self.t1, 'r1'))
        write(os.path.join(self.t1, 'r1', 'A.md'), '# A\n\nfinding\n')

    def topics(self, *folders):
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, walked_units=[self.t1] + [os.path.join(self.base, f) for f in folders])
        ds = debates.debates(s, {})[0]
        self.assertEqual([d['root'] for d in ds], [self.base])
        return {t['key']: t for t in ds[0]['topics']}

    def test_an_editing_job_beside_the_topics_is_a_title_only(self):
        for name in ('edit4', 'edit5'):
            write(os.path.join(self.base, name, 'brief.md'), EDIT_GUIDE)
            os.makedirs(os.path.join(self.base, name, 'edits'))
        tops = self.topics('edit4', 'edit5')
        self.assertEqual(sorted(tops), ['edit4', 'edit5', 't1'])
        for name in ('edit4', 'edit5'):
            self.assertEqual((tops[name]['kind'], tops[name]['rows'], tops[name]['rounds']), ('flat', [], []), name)
        self.assertEqual(([r['p'] for r in tops['t1']['rows']], tops['t1']['rounds']), (['A', 'B', 'C'], [1]))   # the topic that has a round folder is as it was

    def test_a_guide_that_names_a_round_path_but_declares_nobody_makes_no_cell(self):
        write(os.path.join(self.base, 'later', 'brief.md'), '# Later\n\nThe reports will go to `r1/<id>.md` once the people are chosen.\n')
        topic = self.topics('later')['later']
        self.assertEqual((topic['kind'], topic['rows'], topic['rounds']), ('rounds', [], []))

    def test_declared_participants_wait_for_a_round_that_has_not_started(self):
        write(os.path.join(self.base, 'fresh', 'brief.md'), GUIDE)
        topic = self.topics('fresh')['fresh']
        self.assertEqual((topic['kind'], topic['rounds']), ('rounds', [1]))
        self.assertEqual([(r['p'], [(c['round'], c['state']) for c in r['cells']]) for r in topic['rows']], [('A', [(1, 'waiting')]), ('B', [(1, 'waiting')]), ('C', [(1, 'waiting')])])


class Flat(Tree):
    """A review with no round folder has seats only for the reviewers and result files its guide declares."""

    def setUp(self):
        super().setUp()
        self.flat = os.path.join(self.root, 'docs', 'review')
        write(os.path.join(self.flat, 'brief.md'), '# Release review\n\nReviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n')
        self.bare = os.path.join(self.root, 'docs', 'bare')
        write(os.path.join(self.bare, 'brief.md'), '# Release review\n\nReview the notes once and report what you find.\n')

    def test_a_declared_file_is_a_seat_with_no_round(self):
        a = self.agent('a', 'Read `%s/brief.md`. Write your findings to `%s/sol.md`.' % (self.flat, self.flat))
        jd = self.run_assign([a])
        self.assertEqual([(x.unit == self.flat, x.round, x.seat) for x in jd.assignments], [(True, None, 'sol')])
        self.assertEqual(jd.slots, {(self.flat, None, 'sol'): ['a']})
        self.assertEqual(jd.diag, [])

    def test_a_file_the_guide_does_not_declare_is_no_seat(self):
        a = self.agent('a', 'Read `%s/brief.md`. Write your findings to `%s/notes.md`.' % (self.flat, self.flat))
        self.assertEqual(self.run_assign([a]).assignments, [])

    def test_nothing_declared_is_a_title_and_a_diagnostic_and_only_where_someone_writes(self):
        writer = self.agent('w', 'Read `%s/brief.md`. Write your findings to `%s/sol.md`.' % (self.bare, self.bare))
        reader = self.agent('r', 'Read `%s/brief.md` and `%s/sol.md`.' % (self.bare, self.bare))
        jd = self.run_assign([writer, reader])
        self.assertEqual((jd.assignments, self.codes(jd)), ([], ['declaration_missing']))
        self.assertEqual(list(jd.listed), [self.bare])
        self.assertEqual(sorted(jd.worked), ['r', 'w'])
        jd = self.run_assign([reader])
        self.assertEqual((self.codes(jd), list(jd.listed)), ([], []))                            # a reader alone: nothing to flag, and not listed

    def test_a_bare_path_resolves_against_the_working_folder_of_the_agent(self):
        a = self.agent('a', 'Read `brief.md`. Write your findings to `opus.md`.', cwd=self.flat)
        self.assertEqual([(x.unit == self.flat, x.seat) for x in self.run_assign([a]).assignments], [(True, 'opus')])

    def two_reviews(self, second='`sol.md` (reviewer sol)'):
        other = os.path.join(self.root, 'docs', 'review2')
        write(os.path.join(other, 'brief.md'), '# Second review\n\nReviewers and their result files (declared): %s.\n' % second)
        return other

    def test_a_file_name_that_two_reviews_declare_is_held(self):
        other = self.two_reviews()
        a = self.agent('a', 'Write your findings to sol.md.', cwd=self.flat, launcher=other)       # where it works and where it was started both declare sol.md
        jd = self.run_assign([a])
        self.assertEqual((jd.assignments, jd.slots, self.codes(jd)), ([], {}, ['path_ambiguous']))
        self.assertNotIn('a', jd.members)                                                        # tied to neither: a guess to one of them is worse than none
        self.assertEqual(jd.listed, {})
        same = self.agent('same', 'Write your findings to sol.md.', cwd=self.flat, launcher=self.flat)
        self.assertEqual([(x.unit == self.flat, x.seat) for x in self.run_assign([same]).assignments], [(True, 'sol')])
        folder = self.agent('folder', 'Read docs/review/brief.md. Write your findings to docs/review/sol.md.', cwd=self.root, launcher=other)
        self.assertEqual([(x.unit == self.flat, x.seat) for x in self.run_assign([folder]).assignments], [(True, 'sol')])

    def test_only_the_review_that_declares_the_file_takes_it(self):
        other = self.two_reviews('`opus.md` (reviewer opus)')
        a = self.agent('a', 'Write your findings to sol.md.', cwd=self.flat, launcher=other)
        jd = self.run_assign([a])
        self.assertEqual([(x.unit == self.flat, x.seat) for x in jd.assignments], [(True, 'sol')])
        self.assertEqual(self.codes(jd), [])

    def test_a_write_that_succeeded_settles_what_the_file_name_leaves_open(self):
        other = self.two_reviews()
        a = self.agent('a', 'Write your findings to sol.md.', cwd=self.flat, launcher=other, writes=[(write(os.path.join(other, 'sol.md')), True)])
        jd = self.run_assign([a])
        self.assertEqual([(x.unit == other, x.round, x.seat) for x in jd.assignments], [(True, None, 'sol')])      # the tool call names its own folder
        self.assertEqual(self.codes(jd), ['path_ambiguous'])                                      # the instruction stays ambiguous

    def test_an_ambiguous_quotation_still_hides_the_seat_of_both(self):
        other = self.two_reviews()
        a = self.agent('a', 'Examples: "write your findings to sol.md".', cwd=self.flat, launcher=other)
        self.assertEqual(self.run_assign([a]).assignments, [])

    def test_a_quoted_or_negated_result_file_is_no_seat(self):
        for text in ('Examples: "write your findings to `%s/sol.md`".' % self.flat, 'You do not need to write `%s/sol.md`.' % self.flat):
            a = self.agent('a', 'Read `%s/brief.md`. %s' % (self.flat, text))
            self.assertEqual(self.run_assign([a]).assignments, [], text)


class Cells(unittest.TestCase):
    """Only an interrupted run gets a state of its own (paused without a file, a draft with one); the rest reads the file as before. An agent whose process cannot be seen
    (`unknown`, a platform without a process table, quiet for a minute) is working as far as anyone knows: it is never shown as done or missing."""
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


# ---------------------------------------------------------------------------------------------------------------------
# the generator cases, read through debates.judge (sub-agents only: no link index, no process table)
# ---------------------------------------------------------------------------------------------------------------------
class Generated(unittest.TestCase):
    """Cases of tools/scenarios (the oracle's truth) judged by debates.judge on the synthetic HOME: unit, round, seat and the debate diagnostics must be the truth's."""
    DEB = [
        dict(role='writer', rpath='abs'), dict(role='writer', rpath='tilde', structure='topics'), dict(role='writer', rpath='folder', structure='deep3'),
        dict(role='writer', rpath='short'), dict(role='writer', rpath='dotdot', structure='readme'), dict(role='writer', rpath='abs', rdir='r01'),
        dict(role='writer', rpath='abs', rdir='round1', nstyle='numbered'), dict(role='writer', rpath='abs', nstyle='named', fstate='written'),
        dict(role='writer', rpath='abs', nstyle='lower'), dict(role='writer', rpath='abs', nstyle='collide', fstate='written'),
        dict(role='writer', rpath='var_ext', marker='own'), dict(role='writer', rpath='instr_only', marker='own', nstyle='named', fstate='written'),
        dict(role='writer', rpath='var_ext', marker='none'), dict(role='writer', rpath='abs', structure='dot'),
        dict(role='reader', rpath='abs'), dict(role='reader', rpath='tilde', structure='topics'), dict(role='quoter', rpath='abs'), dict(role='quoter', rpath='folder', nstyle='named'),
        dict(role='negator', rpath='abs'), dict(role='negator', rpath='tilde', structure='dot'), dict(role='failed_write'), dict(role='tag_only'), dict(role='ghost', rpath='abs'),
        dict(role='rival'), dict(role='writer', rpath='abs', structure='flat', decl='yes'), dict(role='writer', rpath='folder', structure='flat', decl='no'),
        dict(role='reader', rpath='abs', structure='flat', decl='no'),
        dict(role='ref_reader', rpath='abs'), dict(role='ref_reader', rpath='tilde', structure='topics'), dict(role='ref_reader', rpath='abs', lang='ko'),
        dict(role='quoter', rpath='abs', marker='quoted'), dict(role='quoter', rpath='abs', marker='quoted', lang='ko'),
        dict(role='writer', rpath='abs', rdir='both'), dict(role='writer', rpath='abs', rdir='both', nstyle='named'),
        dict(role='writer', rpath='short', structure='flat', homonym='two'), dict(role='writer', rpath='abs', lang='ko'),
    ]

    @classmethod
    def cases(cls):
        out = []
        for spec in cls.DEB:
            case = axes.normalize(axes.Case('deb', dict(spec, kind='sub')))
            if case.v['kind'] == 'sub' and case.id not in [c.id for c in out]:
                out.append(case)
        return out

    def judge(self, case):
        """(Judged, the child's agent id, repo) of a case: the sessions of the case are read in this process, the clock is the case's last phase."""
        root = tempfile.mkdtemp(prefix='units-gen-')
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        b = build.build_case(case, root)
        ph = b.phases[-1]
        links, index = server.LinkIndex(), server.CodexIndex()
        with patched(HOME=b.home, CLAUDE_HOME=b.claude, PROJECTS=b.projects, CODEX_HOME=b.codex, CODEX_SESSIONS=os.path.join(b.codex, 'sessions'),
                     CODEX_NAMES=os.path.join(b.codex, 'session_index.jsonl'), CODEX=index, LINKS=links), \
                mock.patch.dict(os.environ, {'HOME': b.home}), mock.patch.object(procs, 'PROC', os.path.join(b.root, 'no-proc')), mock.patch('time.time', lambda: ph.now):
            procs.reset()
            s = server.Session(b.main_path)
            s.poll()
            s.poll()
            jd = debates.judge(s, {aid: 'running' for aid in s.agents})
        return jd, b.ids.get('child'), b.meta['repo']

    def test_the_cases_have_the_truth_the_oracle_computes(self):
        cases = self.cases()
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            truth = oracle.truth(case).subjects.get('child', {})
            jd, child, repo = self.judge(case)
            rel = lambda p: os.path.relpath(p, repo)                                          # noqa: E731
            mine = [a for a in jd.assignments if a.agent == child]
            where = sorted(rel(p) for p in jd.agent_units.get(child, ()))
            if 'unit' in truth and truth['unit'] is not None:
                self.assertIn(truth['unit'], where, case.id)
            elif 'unit' in truth:
                self.assertEqual(where, [], case.id)
            seat = truth.get('seat')
            if seat is None:
                self.assertEqual(mine, [], case.id)
            else:
                self.assertEqual([(rel(a.unit), a.round, a.seat) for a in mine], [(truth['unit'], truth.get('round'), seat)], case.id)
            want = {c for c, _ in oracle.truth(case).diag if c in facts.DIAG_DEBATE}
            got = {d['code'] for d in jd.diag if d['agent'] in (child, None)}
            self.assertEqual(got, want, case.id)


class DebatesView(unittest.TestCase):
    """What the board shows of the judgment: the cells of a seat follow the status of its agent, a flat review has no round."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.unit = os.path.join(os.path.realpath(self.tmp.name), 'repo', 'rev')
        write(os.path.join(self.unit, 'brief.md'), GUIDE)
        os.makedirs(os.path.join(self.unit, 'round1'))

    def build(self, status, text=None, flat=None):
        a = server.Agent('a0000000000000001', {'description': 'B check'})
        a.origin, a.spawn_ts, a.first_ts = 'subagent', 100.0, 100.0
        a.spawn_prompt = text or 'Write your result to `%s/round1/B_gate.md`.' % self.unit
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        return debates.debates(s, {a.id: status}), a

    def cell(self, out, rows=None):
        topic = out[0][0]['topics'][0]
        return {r['p']: r['cells'][0] for r in topic['rows']}

    def test_interrupted_agent_is_paused_without_a_file_and_drafting_with_one(self):
        out, a = self.build('interrupted')
        cell = self.cell(out)['B_gate']
        self.assertEqual((cell['state'], cell['agent'], cell['path']), ('paused', a.id, os.path.join(self.unit, 'round1', 'B_gate.md')))      # the spelling is kept
        write(os.path.join(self.unit, 'round1', 'B_gate.md'), 'one\ntwo\n')
        out, a = self.build('interrupted')
        self.assertEqual(self.cell(out)['B_gate']['state'], 'draft')

    def test_the_other_states_read_the_file_as_before(self):
        for status, nofile, withfile in (('running', 'writing', 'draft'), ('stalled', 'writing', 'draft'), ('done', 'missing', 'done'), ('ended', 'missing', 'done'),
                                        ('failed', 'missing', 'done'), ('killed', 'missing', 'done'), ('unknown', 'writing', 'draft')):
            path = os.path.join(self.unit, 'round1', 'B_gate.md')
            if os.path.exists(path):
                os.unlink(path)
            out, _a = self.build(status)
            self.assertEqual(self.cell(out)['B_gate']['state'], nofile, status)
            write(path, 'x\n')
            out, _a = self.build(status)
            self.assertEqual(self.cell(out)['B_gate']['state'], withfile, status)

    def shell_agent(self, command, error, text=None, key=''):
        """A sub-agent told of its report by a noun only ("Report: <path>"), which then runs one Bash call; `error` is the call's result (None: no result yet)."""
        a = server.Agent('a0000000000000001', {'description': '%s check' % key} if key else {})
        a.origin, a.spawn_ts, a.first_ts = 'subagent', 100.0, 100.0
        a.spawn_prompt = text or 'Read %s/brief.md. Report: %s/round1/B_gate.md' % (self.unit, self.unit)
        a.feed({'type': 'assistant', 'timestamp': '1970-01-01T00:02:00Z', 'cwd': self.unit, 'message': {'id': 'm1', 'content': [
            {'type': 'tool_use', 'id': 'tb1', 'name': 'Bash', 'input': {'command': command}}]}})
        if error is not None:
            a.feed({'type': 'user', 'timestamp': '1970-01-01T00:02:10Z', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tb1', 'is_error': error}]}})
        return a

    def test_a_shell_write_of_the_report_that_succeeded_is_a_write_that_succeeded(self):
        report = os.path.join(self.unit, 'round1', 'B_gate.md')
        heredoc = "cat > %s <<'EOF'\nfindings\nEOF" % report
        for command, error, made, seat in ((heredoc, False, True, True),                       # it ran and the file is there
                                           (heredoc, False, False, False),                      # it said so but there is no file: the redirect never ran
                                           (heredoc, True, True, False),                        # the call failed
                                           (heredoc, None, False, False),                       # no result yet and no file
                                           (heredoc, None, True, True),                         # no result yet, the file is already there
                                           ('echo hi > %s/notes.log' % self.unit, False, True, False),
                                           ('cat %s' % report, False, True, False)):             # a read
            if os.path.exists(report):
                os.unlink(report)
            if made:
                write(report, 'findings\n')
            a = self.shell_agent(command, error)
            s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
            topic = debates.debates(s, {a.id: 'done'})[0][0]['topics'][0]
            cells = {r['p']: r['cells'][0] for r in topic['rows']}
            got = (cells['B_gate']['agent'], cells['B_gate']['state']) if 'B_gate' in cells else None          # no seat and no file: not even a row
            self.assertEqual(got, (a.id, 'done') if seat else ((None, 'done') if made else None), (command[:20], error, made))
            self.assertEqual(a.writes, [])

    def test_a_debate_only_a_shell_write_names_is_listed_with_that_cell(self):
        report = write(os.path.join(self.unit, 'round1', 'B_gate.md'), 'findings\n')
        a = self.shell_agent("cat > %s <<'EOF'\nfindings\nEOF" % report, False, text='Do the review you were asked for.')
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        debates_out = debates.debates(s, {a.id: 'done'})[0]
        cell = {r['p']: r['cells'][0] for d in debates_out for t in d['topics'] for r in t['rows']}['B_gate']
        self.assertEqual((cell['agent'], cell['state']), (a.id, 'done'))
        s = types.SimpleNamespace(agents={a.id: self.shell_agent('cat %s' % report, False, text='Do the review you were asked for.')}, _file_cache={}, _head_cache={})
        self.assertEqual(debates.debates(s, {a.id: 'done'})[0], [])                           # a read names no debate

    def test_a_shell_write_of_another_file_in_the_debate_is_the_tags_write(self):
        notes = os.path.join(self.unit, 'notes.md')
        a = self.shell_agent("cat > %s <<EOF\nx\nEOF" % notes, False, key='B')
        write(notes)
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        cell = next(r for r in debates.debates(s, {a.id: 'running'})[0][0]['topics'][0]['rows'] if r['p'] == 'B')['cells'][0]
        self.assertEqual((cell['agent'], cell['round']), (a.id, 1))                           # as a Write to a file there would be

    def test_a_finished_writer_keeps_its_done_cell_when_a_weaker_claim_comes_later(self):
        path = os.path.join(self.unit, 'round1', 'B_gate.md')
        write(path, 'report\n')
        writer = server.Agent('a0000000000000011', {})
        writer.origin, writer.spawn_ts, writer.first_ts, writer.last_ts = 'subagent', 100.0, 100.0, 150.0
        writer.spawn_prompt = 'Write your result to `%s`.' % path
        later = server.Agent('a0000000000000012', {'description': 'B check'})
        later.origin, later.spawn_ts, later.first_ts = 'subagent', 200.0, 200.0
        later.spawn_prompt = 'Check the gate.'
        later.writes.append({'ts': 210.0, 'path': write(os.path.join(self.unit, 'notes.txt')), 'ok': True})       # the tag and a write somewhere in the folder: rank 4
        s = types.SimpleNamespace(agents={writer.id: writer, later.id: later}, _file_cache={}, _head_cache={})
        topic = debates.debates(s, {writer.id: 'done', later.id: 'running'})[0][0]['topics'][0]
        cell = next(r for r in topic['rows'] if r['p'] == 'B_gate')['cells'][0]
        self.assertEqual((cell['agent'], cell['state']), (writer.id, 'done'))                     # the weaker claim did not take the cell and turn it into a draft

    def test_a_flat_review_shows_cells_without_a_round(self):
        flat = os.path.join(os.path.dirname(self.unit), 'review')
        write(os.path.join(flat, 'brief.md'), '# Review\n\nReviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus).\n')
        write(os.path.join(flat, 'opus.md'), 'done\n')
        out, a = self.build('running', text='Read `%s/brief.md`. Write your findings to `%s/sol.md`.' % (flat, flat))
        topic = out[0][0]['topics'][0]
        self.assertEqual((topic['kind'], topic['rounds'], topic['dir']), ('flat', [], flat))
        cells = {r['p']: r['cells'] for r in topic['rows']}
        self.assertEqual(sorted(cells), ['opus', 'sol'])
        self.assertEqual([(c['round'], c['state'], c['agent']) for c in cells['sol']], [(None, 'writing', a.id)])
        self.assertEqual([(c['round'], c['state'], c['agent']) for c in cells['opus']], [(None, 'done', None)])
        self.assertEqual(topic['final'], {'path': None, 'rel': None, 'exists': False, 'auto': False, 'mtime': None, 'lines': 0})   # a result file is not the conclusion

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
        write(os.path.join(parent, 'brief.md'), root_text)
        topics = []
        for name in ('t1_schema', 't2_data'):
            t = os.path.join(parent, name)
            write(os.path.join(t, 'brief.md'), '# %s\n\n%s' % (name, GUIDE))
            os.makedirs(os.path.join(t, 'round1'), exist_ok=True)
            topics.append(t)
        return parent, topics

    def test_a_common_guide_that_describes_rounds_is_the_root_and_not_an_empty_topic(self):
        for text in ('# Migration\n\nEvery topic keeps its reports in `r1/<id>.md` and `r2/<id>.md`.\n',
                     '# Migration\n\n**A — flow**\n**B — data**\n\n| topic | folder |\n|---|---|\n| Schema | `t1_schema/` |\n'):
            parent, topics = self.grouped(text)
            self.assertEqual(U.read_unit(parent).kind, 'rounds')                                 # the guide describes a round layout: it reads as a debate of its own
            s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, walked_units=[parent] + topics)
            out = debates.debates(s, {})[0]
            self.assertEqual([(d['root'], [t['dir'] for t in d['topics']]) for d in out], [(parent, topics)], text)      # one card, its topics are the folders below it
            self.assertEqual(sum(1 for d in out for t in d['topics'] if t['dir'] == parent), 0)

    def test_the_common_guide_stays_a_root_when_an_agent_works_in_one_topic(self):
        parent, topics = self.grouped('# Migration\n\nEvery topic keeps its reports in `r1/<id>.md`.\n')
        a = server.Agent('a0000000000000008', {})
        a.origin, a.spawn_ts, a.first_ts = 'subagent', 100.0, 100.0
        a.spawn_prompt = 'Write your result to `%s/round1/A.md`.' % topics[0]
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

    def test_the_text_functions_are_remembered_up_to_a_size(self):
        U.find_mentions.cache_clear()
        short, long_ = 'write `/x/r1/B.md`', 'x' * (U.REMEMBER_CHARS + 1) + ' write `/x/r1/B.md`'
        for text in (short, short, long_, long_):
            self.assertEqual([(m.stem, m.cls) for m in U.find_mentions(text)], [('B', 'write')])
        self.assertEqual((U.find_mentions.cache_info().hits, U.find_mentions.cache_info().currsize), (1, 1))        # only the short one is kept

    def test_the_diagnostics_are_left_on_the_session(self):
        a = server.Agent('a0000000000000002', {})
        a.spawn_ts, a.first_ts = 100.0, 100.0
        a.spawn_prompt = 'You do not need to write `%s/round1/B.md`; read `%s/brief.md`.' % (self.unit, self.unit)
        s = types.SimpleNamespace(agents={a.id: a}, _file_cache={}, _head_cache={})
        debates.debates(s, {a.id: 'running'})
        self.assertEqual([(d['code'], d['agent']) for d in s.debate_diag], [('debate_in_misc', a.id)])
        for d in s.debate_diag:
            self.assertEqual(sorted(d), ['agent', 'code', 'detail', 'unit'])                  # ids, folders and short codes: no text of a record


if __name__ == '__main__':
    unittest.main()
