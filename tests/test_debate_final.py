"""Finding the final document for a topic whose brief table names no final deliverable (board/debates.py auto_final).

    python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402,F401
from board.debates import auto_final  # noqa: E402


class AutoFinal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.unit = os.path.join(os.path.realpath(self.tmp.name), 't5_readability')
        os.makedirs(self.unit)
        self.s = types.SimpleNamespace(_file_cache={})

    def put(self, name, t, text='x\n'):
        p = os.path.join(self.unit, name)
        with open(p, 'w') as f:
            f.write(text)
        os.utime(p, (t, t))

    def rows(self, *cells):
        return [{'cells': [{'state': st, 'mtime': mt} for st, mt in cells]}]

    def test_rulings_after_last_round(self):
        self.put('brief.md', 50)
        self.put('round2.md', 150)                 # round 2 instruction file (after round 1): not a conclusion
        self.put('rulings.md', 300)
        f, info = auto_final(self.s, self.unit, self.rows(('done', 100), ('done', 200)))
        self.assertEqual(f, 'rulings.md')
        self.assertEqual(info['mtime'], 300)

    def test_plan_counts_as_conclusion(self):          # a debate that ended with PLAN.md
        self.put('PLAN.md', 300)
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200)))[0], 'PLAN.md')

    def test_same_time_counts(self):                     # a folder where git clone or a copy gave every file the same time
        self.put('PLAN.md', 199.999)               # the copy order put it 1 ms before the report (200.000)
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200), ('done', 200)))[0], 'PLAN.md')

    def test_nothing_newer_than_reports(self):
        self.put('rulings.md', 150)                # the next round started after the conclusion
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 100), ('done', 200))), (None, None))

    def test_brief_and_round_docs_never_final(self):
        for name in ('brief.md', 'round3.md', 'round2_followup.md', 'r3_instructions.md'):
            self.put(name, 300)
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200))), (None, None))

    def test_round_in_progress(self):
        self.put('rulings.md', 300)
        for st in ('writing', 'draft', 'paused'):             # paused: a cut-off run may come back, the round is not over
            self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200), (st, None))), (None, None), st)

    def test_no_submitted_report(self):
        self.put('rulings.md', 300)
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('waiting', None), ('missing', None))), (None, None))

    def test_conclusion_name_beats_newer_note(self):
        self.put('rulings_v1.md', 250)
        self.put('rulings.md', 300)
        self.put('notes.md', 400)                  # more recent, but the name does not look like a conclusion
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200)))[0], 'rulings.md')

    def test_any_doc_when_no_conclusion_name(self):
        self.put('draft_CONVENTION.md', 300)
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200)))[0], 'draft_CONVENTION.md')

    def test_empty_and_subfolders_ignored(self):
        self.put('rulings.md', 300, text='')       # empty file
        os.makedirs(os.path.join(self.unit, 'work_A'))
        with open(os.path.join(self.unit, 'work_A', 'final.md'), 'w') as f:
            f.write('x')
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 200))), (None, None))


if __name__ == '__main__':
    unittest.main()


class RelativeRefsNeedRealFolder(unittest.TestCase):
    """A relative path in text (r1/C.md) is resolved only when the base folder is a real debate folder (has brief.md or r<N>/).
    This was the bug where Codex started at the top of the repo, with a prompt saying it reads r1/C.md, and that folder showed up as a fake debate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.topic = os.path.join(self.root, 'docs', 'release')
        os.makedirs(os.path.join(self.topic, 'r1'))
        with open(os.path.join(self.topic, 'brief.md'), 'w') as f:
            f.write('# release\n')
        self.s = server.Session(os.path.join(self.root, 'aaaaaaaa-0000-4000-8000-000000000000.jsonl'))

    def codex(self, cwd):
        a = server.Agent('a%016x' % 7, {'description': 'C 검토'})
        a.provider, a.cwd, a.spawn_ts = 'codex', cwd, 1000.0
        a.spawn_prompt = 'r1/A.md와 r1/B.md를 읽고 r1/C.md에 쓴다'
        self.s.agents[a.id] = a
        return a

    def units(self):
        out, _ = self.s.debates({})
        return {t['dir'] for d in out for t in d['topics']}

    def test_repo_top_is_not_a_debate(self):
        self.codex(self.root)                       # top of the repo: no brief.md and no r1/
        self.assertNotIn(self.root, self.units())

    def test_round_like_names_are_not_rounds(self):
        work = os.path.join(self.root, 'work_X')
        os.makedirs(os.path.join(work, 'r2_check'))        # work folder whose name merely starts with r
        with open(os.path.join(work, 'r1_run.log'), 'w') as f:
            f.write('x')
        self.codex(work)
        self.assertNotIn(work, self.units())

    def test_topic_folder_resolves(self):
        self.codex(self.topic)
        self.assertIn(self.topic, self.units())
