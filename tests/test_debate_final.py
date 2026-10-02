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
from board import debates  # noqa: E402
from board.debates import auto_final, closing_above, names_topic  # noqa: E402


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


class ClosingAbove(unittest.TestCase):
    """A topic with no conclusion of its own is closed by a conclusion document in the folder above it (the root of its bundle): written after the topic's last activity, and
    either naming the topic's folder or written after everything the bundle did."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(os.path.realpath(self.tmp.name), 'review')
        self.unit = os.path.join(self.root, 'step2')
        os.makedirs(self.unit)
        self.s = types.SimpleNamespace(_file_cache={}, _head_cache={})

    def put(self, name, t, text='The step2/ folder is closed.\n'):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)
        os.utime(p, (t, t))
        return p

    def rows(self, *cells):
        return [{'cells': [{'state': st, 'mtime': mt} for st, mt in cells]}]

    def find(self, rows=None, active=0.0, bundle=0.0, unit=None, root=None, topics=()):
        more = {'topics': topics} if topics else {}
        return closing_above(self.s, unit or self.unit, root or self.root, rows or self.rows(('done', 100), ('done', 200)), active, bundle, **more)

    def test_a_closing_document_that_names_the_topic_and_came_later_closes_it(self):
        self.put('CLOSING.md', 300)
        rel, info = self.find()
        self.assertEqual((rel, info['mtime'], info['exists'] if 'exists' in info else True), ('../CLOSING.md', 300, True))

    def test_the_names_of_a_conclusion_are_the_ones_auto_final_reads_and_closing_too(self):
        for name in ('FINAL.md', 'summary.md', '결론.md', 'CLOSING.md', 'closure_notes.md', '종결.md', 'rulings.md', 'PLAN.md'):
            p = self.put(name, 300)
            self.assertEqual(self.find()[0], '../' + name, name)
            os.unlink(p)

    def test_a_document_that_is_no_conclusion_closes_nothing(self):
        for name in ('notes.md', 'brief.md', 'README.md', 'round2.md', 'r3_instructions.md', 'log.md'):
            self.put(name, 300)
        self.assertEqual(self.find(), (None, None))

    def test_a_document_from_before_the_last_activity_closes_nothing(self):
        self.put('CLOSING.md', 150)
        self.assertEqual(self.find(), (None, None))                                 # the last report is at 200
        self.put('CLOSING.md', 300)
        self.assertEqual(self.find(active=350)[0], None)                              # an agent of the topic went on working after it (its record, not a file)
        self.assertEqual(self.find(active=250)[0], '../CLOSING.md')

    def test_a_copy_that_gave_every_file_nearly_the_same_time_counts(self):
        self.put('CLOSING.md', 199.5)
        self.assertEqual(self.find()[0], '../CLOSING.md')

    def test_a_document_that_does_not_name_the_topic_closes_it_only_when_nothing_in_the_bundle_came_after_it(self):
        self.put('CLOSING.md', 300, text='Everything is closed.\n')
        self.assertEqual(self.find(bundle=200)[0], '../CLOSING.md')                 # the bundle's last activity is before it: it is the bundle's closing
        self.assertEqual(self.find(bundle=400), (None, None))                         # another topic worked on after it: it speaks for nobody it does not name
        self.put('CLOSING.md', 300, text='The step2/ folder is closed.\n')
        self.assertEqual(self.find(bundle=400)[0], '../CLOSING.md')                 # it names this one

    def test_a_document_that_names_another_topic_speaks_for_that_topic_only(self):
        step1 = os.path.join(self.root, 'step1')
        os.makedirs(step1)
        topics = [step1, self.unit]
        self.put('CLOSING.md', 300, text='Only the step1/ folder is closed; the other topics are not done.\n')
        self.assertEqual(self.find(bundle=200, topics=topics), (None, None))                  # it limits itself to step1, so it closes no other topic
        self.assertEqual(self.find(bundle=200, topics=topics, unit=step1, rows=self.rows(('done', 100)))[0], '../CLOSING.md')
        self.put('CLOSING.md', 300, text='The step1/ folder is closed.\n')
        self.assertEqual(self.find(bundle=200, topics=topics), (None, None))                  # a document that names one topic and is silent on this one
        self.put('CLOSING.md', 300, text='The step1/ and step2/ folders are closed.\n')
        self.assertEqual(self.find(bundle=200, topics=topics)[0], '../CLOSING.md')            # it names this one too

    def test_a_document_that_names_nobody_still_closes_the_bundle_when_it_says_nothing_is_open(self):
        step1 = os.path.join(self.root, 'step1')
        os.makedirs(step1)
        for text in ('Everything is closed.\n', 'Everything is in; nothing is left open.\n', '모든 주제를 종결했다.\n'):
            self.put('CLOSING.md', 300, text=text)
            self.assertEqual(self.find(bundle=200, topics=[step1, self.unit])[0], '../CLOSING.md', text)

    def test_a_document_that_names_nobody_but_says_something_is_open_closes_nothing(self):
        step1 = os.path.join(self.root, 'step1')
        os.makedirs(step1)
        for text in ('Part of it is closed; the rest is not done yet.\n', 'Closed for now. Remaining: the other topics.\n', '일부만 종결했고 나머지는 미완료이다.\n', '아직 남은 주제가 있다.\n',
                     'The other topics are still open.\n', 'Pending: everything else.\n'):
            self.put('CLOSING.md', 300, text=text)
            self.assertEqual(self.find(bundle=200, topics=[step1, self.unit]), (None, None), text)

    def test_a_plain_word_names_a_topic_only_as_a_path(self):
        self.assertTrue(names_topic('The step2/ folder.', self.unit, self.root))
        self.assertTrue(names_topic('Folders: step2, step3.', self.unit, self.root))             # a name with a digit is not a word of the prose
        self.assertTrue(names_topic('See `review/step2`.', self.unit, self.root))
        self.assertFalse(names_topic('The step20 and xstep2 folders and step2.md.', self.unit, self.root))
        recheck = os.path.join(self.root, 'recheck')
        self.assertFalse(names_topic('We recheck everything before closing.', recheck, self.root))
        self.assertTrue(names_topic('Records: `recheck/`, `cleanup/`.', recheck, self.root))
        self.assertTrue(names_topic('Records: recheck/ and cleanup/.', recheck, self.root))
        self.assertTrue(names_topic('in "recheck" only', recheck, self.root))
        self.assertTrue(names_topic('see %s for the details' % recheck, recheck, self.root))
        self.assertFalse(names_topic('', self.unit, self.root))

    def test_a_topic_still_open_or_with_nothing_submitted_is_not_closed(self):
        self.put('CLOSING.md', 300)
        for st in ('writing', 'draft', 'paused'):
            self.assertEqual(self.find(self.rows(('done', 200), (st, None))), (None, None), st)
        self.assertEqual(self.find(self.rows(('waiting', None), ('missing', None))), (None, None))

    def test_an_empty_hidden_or_secret_document_is_none(self):
        self.put('CLOSING.md', 300, text='')
        self.put('.final.md', 300)
        self.put('token_summary.md', 300)
        self.assertEqual(self.find(), (None, None))

    def test_the_topic_that_is_the_root_has_nothing_above_it(self):
        self.put('CLOSING.md', 300)
        self.assertEqual(self.find(unit=self.root), (None, None))

    def test_a_document_that_names_nobody_closes_only_from_the_bundles_own_folder(self):
        deep = os.path.join(self.root, 'part', 'step2')
        os.makedirs(deep)
        self.put('part/CLOSING.md', 300, text='Everything is closed.\n')
        self.assertEqual(self.find(unit=deep, bundle=200), (None, None))                 # a folder between the topic and the root is no bundle
        self.put('CLOSING.md', 300, text='Everything is closed.\n')
        self.assertEqual(self.find(unit=deep, bundle=200)[0], '../../CLOSING.md')

    def test_a_topic_deeper_in_the_bundle_looks_in_every_folder_above_it_up_to_the_root(self):
        deep = os.path.join(self.root, 'part', 'step2')
        os.makedirs(deep)
        self.put('part/FINAL.md', 300, text='The step2/ folder is closed.\n')
        self.assertEqual(self.find(unit=deep)[0], '../FINAL.md')
        self.put('CLOSING.md', 400, text='The step2/ folder is closed.\n')
        self.assertEqual(self.find(unit=deep)[0], '../../CLOSING.md')                  # the closest is not the only one: the later one is the closing

    def test_the_conclusion_of_the_topic_itself_comes_first(self):
        # closing_above is asked only for a topic that has none of its own (debates._debate); a name that is a conclusion in the topic's folder is auto_final's
        self.put('step2/rulings.md', 300)
        self.assertEqual(auto_final(self.s, self.unit, self.rows(('done', 100), ('done', 200)))[0], 'rulings.md')


class ClosingInBundle(unittest.TestCase):
    """The whole judgment: a bundle of two topics, each with two reports, and a closing document in the bundle's folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(os.path.realpath(self.tmp.name), 'review')
        self.topics = [os.path.join(self.root, n) for n in ('step1', 'step2')]
        self.put('brief.md', 0, '# The review\n\nEvery step is a folder below.\n')
        for unit, t in zip(self.topics, (100, 200)):
            self.put(os.path.join(os.path.basename(unit), 'brief.md'), 0, '# Step\n\nTwo reviewers.\n')
            for p in 'AB':
                self.put(os.path.join(os.path.basename(unit), 'r1', p + '.md'), t, '# %s\n\nfindings\n' % p)

    def put(self, name, t, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        if t:
            os.utime(path, (t, t))

    def finals(self):
        s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, cwd=self.root, walked_units=self.topics)
        (d,) = debates.judge(s, {}).debates
        self.assertEqual(d['root'], self.root)
        return {os.path.basename(t['dir']): (t['final']['exists'], t['final']['rel']) for t in d['topics']}

    def test_a_document_that_closes_one_step_and_leaves_the_others_open_closes_only_that_one(self):
        self.put('CLOSING.md', 300, 'Only the step1/ folder is closed; every other topic is not done.\n')
        self.assertEqual(self.finals(), {'step1': (True, '../CLOSING.md'), 'step2': (False, None)})

    def test_a_document_that_closes_the_whole_bundle_closes_every_step(self):
        self.put('CLOSING.md', 300, 'Everything is in; nothing is left open.\n')
        self.assertEqual(self.finals(), {'step1': (True, '../CLOSING.md'), 'step2': (True, '../CLOSING.md')})

    def test_a_document_that_names_both_closes_both(self):
        self.put('CLOSING.md', 300, 'The step1/ and step2/ folders are closed.\n')
        self.assertEqual(self.finals(), {'step1': (True, '../CLOSING.md'), 'step2': (True, '../CLOSING.md')})


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
