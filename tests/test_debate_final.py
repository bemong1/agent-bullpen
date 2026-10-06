"""The final of a debate (CONTRACT J15, J16): the one document that closes it, found by structure alone.

A topic, a room or the root of a bundle has a confirmed final only when exactly one document competes (a document written, or tried, in the folder after the last report was made) and it was written by a sure
tool or shell write that is not older than the last write attempt on a report; no cell is open (not submitted, held, or an empty file), no round folder is empty, nobody tied to it may still be working, and no
record that may hold a later write was left unread. The documents are only put in an order (`units.final_sort_key`); the text of one is never read. The input is the events of the agents (`judge_support`), the
Judgement of `units.assign` (`jd.finals`, `jd.closable`), and for what the page gets `debates.judge` of a stand-in session.

    python3 -m unittest tests.test_debate_final
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from judge_support import Fixture, agent, plan, wr  # noqa: E402

from board import units as U  # noqa: E402


class Topic(Fixture):
    """A topic folder `t5_readability` that holds two reports, r1/A.md (made at 100) and r1/B.md (at 200), by the agents A and B who are done."""

    def setUp(self):
        super().setUp()
        self.unit = self.p('t5_readability')
        self.pa = self.file('t5_readability/r1/A.md', '# A\n\nfindings\n', 100)
        self.pb = self.file('t5_readability/r1/B.md', '# B\n\nfindings\n', 200)
        self.a = agent('A', writes=[wr(self.pa, 100)])
        self.b = agent('B', writes=[wr(self.pb, 200)])

    def put(self, name, ts, text='x\n'):
        """A document in the folder of the topic with the time `ts` on the disk. Returns its path. Whether somebody wrote it is told by an event."""
        return self.file('t5_readability/' + name, text, ts)

    def final(self, *agents, orch=(), **kw):
        """(the Judgement, the Final of the topic) of the two reports (or of `agents`) and the write events of the orchestrator `orch`."""
        jd = self.assign(*(agents or (self.a, self.b)), orch=orch, **kw)
        return jd, jd.finals[self.unit]


class AutoFinal(Topic):
    def test_rulings_after_last_round(self):
        brief, round2, rulings = self.put('brief.md', 50), self.put('round2.md', 150), self.put('rulings.md', 300)       # a guide and the instruction of round 2 are not conclusions
        jd, f = self.final(orch=[wr(brief, 50), wr(round2, 150), wr(rulings, 300)])
        self.assertEqual((f.confirmed, f.path, f.by, f.why, f.candidates, f.scope), (True, rulings, 'orch', [], [rulings], 'topic'))
        self.assertEqual(f.last_report, 200)                              # the last write attempt on a report: the final is not older than it
        self.assertTrue(jd.closable[self.unit])

    def test_the_page_gets_the_final_of_the_topic(self):
        rulings = self.put('rulings.md', 300, 'one\ntwo\n')
        jd = self.page(self.a, self.b, orch=[wr(rulings, 300)])
        (topic,) = jd.debates[0]['topics']
        final = topic['final']
        self.assertEqual((final['confirmed'], final['exists'], final['rel'], final['by'], final['mtime'], final['lines'], final['why'], final['scope']), (True, True, 'rulings.md', 'orch', 300, 2, [], 'topic'))
        self.assertEqual([(c['rel'], c['lines']) for c in final['candidates']], [('rulings.md', 2)])
        self.assertTrue(topic['closable'])
        self.assertTrue(jd.finals[self.unit].confirmed)

    def test_plan_counts_as_conclusion(self):          # a debate that ended with PLAN.md, written by one of its agents
        plan_md = self.put('PLAN.md', 300)
        jd, f = self.final(self.a, agent('B', writes=[wr(self.pb, 200), wr(plan_md, 300)]))
        self.assertEqual((f.confirmed, f.path, f.by), (True, plan_md, 'B'))

    def test_same_time_counts(self):                     # a folder where git clone or a copy gave every file the same time
        plan_md = self.put('PLAN.md', 199.999)         # the copy order put it 1 ms before the report (200.000)
        self.assertEqual(self.final(orch=[wr(plan_md, 199.999)])[1].path, plan_md)
        old = self.put('PLAN.md', 197.0)                # more than SAME_TIME before the last report: it is from before them
        f = self.final(orch=[wr(old, 197.0)])[1]
        self.assertEqual((f.confirmed, f.why, f.candidates), (False, ['none'], []))

    def test_nothing_newer_than_reports(self):
        rulings = self.put('rulings.md', 150)                # the next round started after the conclusion
        f = self.final(orch=[wr(rulings, 150)])[1]
        self.assertEqual((f.confirmed, f.path, f.why, f.candidates), (False, None, ['none'], []))
        rulings = self.put('rulings.md', 300)               # now it is after them, and final; then the next round: a report written after it takes the final back
        self.assertTrue(self.final(orch=[wr(rulings, 300)])[1].confirmed)
        again = self.file('t5_readability/r2/A.md', 'x\n', 400)
        f = self.final(agent('A', writes=[wr(self.pa, 100), wr(again, 400)]), self.b, orch=[wr(rulings, 300)])[1]
        self.assertEqual((f.confirmed, f.why), (False, ['none']))

    def test_brief_and_round_docs_never_final(self):
        names = ('brief.md', 'README.md', 'index.md', 'round3.md', 'round2_followup.md', 'r3_instructions.md')
        orch = [wr(self.put(name, 300), 300) for name in names]
        f = self.final(orch=orch)[1]
        self.assertEqual((f.confirmed, f.why, f.candidates), (False, ['none'], []))       # they are no candidates and no rivals
        rulings = self.put('rulings.md', 310)
        f = self.final(orch=orch + [wr(rulings, 310)])[1]
        self.assertEqual((f.confirmed, f.path), (True, rulings))           # and they do not make "several"

    def test_round_in_progress(self):
        os.unlink(self.pb)
        rulings = self.put('rulings.md', 300)
        for st, status in (('writing', 'running'), ('paused', 'interrupted')):         # paused: a cut-off run may come back, the round is not over
            jd, f = self.final(self.a, agent('B', planned=[plan(self.pb)], status=status), orch=[wr(rulings, 300)])
            self.assertEqual(self.cell(jd, 't5_readability', 'r1', 'B').state, st, st)
            self.assertEqual((f.confirmed, 'open_cell' in f.why), (False, True), st)
            self.assertFalse(jd.closable[self.unit], st)
        self.put('r1/B.md', 200)
        jd, f = self.final(self.a, agent('B', writes=[wr(self.pb, 200)], status='running'), orch=[wr(rulings, 300)])
        self.assertEqual(self.cell(jd, 't5_readability', 'r1', 'B').state, 'draft')    # a working agent has a draft
        self.assertEqual((f.confirmed, 'open_cell' in f.why, 'live_participant' in f.why), (False, True, True))

    def test_no_submitted_report(self):
        os.unlink(self.pb)
        rulings = self.put('rulings.md', 300)
        # a report that was asked for and never came (its agent is over): a cell that is missing, not submitted
        jd, f = self.final(self.a, agent('B', planned=[plan(self.pb)]), orch=[wr(rulings, 300)])
        self.assertEqual(self.cell(jd, 't5_readability', 'r1', 'B').state, 'missing')
        self.assertEqual((f.confirmed, f.why), (False, ['open_cell']))
        # a seat two agents were asked to fill at once: nobody holds it, it waits
        x = self.p('t5_readability', 'r1', 'X.md')
        jd, f = self.final(self.a, agent('C', planned=[plan(x)], status='running'), agent('D', planned=[plan(x)], status='running'), orch=[wr(rulings, 300)])
        self.assertEqual(self.cell(jd, 't5_readability', 'r1', 'X').state, 'waiting')
        self.assertIn('open_cell', f.why)
        self.assertFalse(f.confirmed)
        # nothing was submitted at all: there is no report to conclude
        jd, f = self.final(agent('A', planned=[plan(self.pa)]), agent('B', planned=[plan(self.pb)]), orch=[wr(rulings, 300)])
        a_cell = self.cell(jd, 't5_readability', 'r1', 'A')
        self.assertEqual((a_cell.state, a_cell.previous), ('missing', True))        # the file of A.md on the disk is from before: it is not this run's report
        self.assertEqual((f.confirmed, f.why), (False, ['no_report']))

    def test_conclusion_name_beats_newer_note(self):
        v1, rulings, notes = self.put('rulings_v1.md', 250), self.put('rulings.md', 300), self.put('notes.md', 400)      # notes.md is more recent, but the name does not look like a conclusion
        f = self.final(orch=[wr(v1, 250), wr(rulings, 300), wr(notes, 400)])[1]
        self.assertEqual((f.confirmed, f.why), (False, ['several']))     # three documents were written after the reports: nothing is picked (J15)
        self.assertEqual(f.candidates, [rulings, v1, notes])               # the order of the list: a conclusion name first, then the latest, then the name
        # documents that came after the reports with no record of anybody (a copy, a script) compete all the same: nothing says which of them is the conclusion (principle U, O14)
        f = self.final(orch=[wr(rulings, 300)])[1]
        self.assertEqual((f.confirmed, f.path, f.why, f.candidates), (False, None, ['several'], [rulings, v1, notes]))
        # ... and the ones that are older than the reports are no candidates at all
        self.put('rulings_v1.md', 50)
        self.put('notes.md', 60)
        f = self.final(orch=[wr(rulings, 300)])[1]
        self.assertEqual((f.confirmed, f.path, f.candidates), (True, rulings, [rulings]))

    def test_the_names_of_a_conclusion_come_first(self):
        # only an order: the names a candidate list puts first (and the names it does not)
        for name in ('FINAL.md', 'summary.md', '결론.md', 'CLOSING.md', 'closure_notes.md', '종결.md', 'rulings.md', 'PLAN.md', 'decision_v2.md'):
            self.assertFalse(U.final_sort_key(name, 100)[0], name)
        for name in ('notes.md', 'draft_CONVENTION.md', 'log.md', 'README.md'):
            self.assertTrue(U.final_sort_key(name, 100)[0], name)
        names = ['notes.md', 'rulings_v1.md', 'draft_CONVENTION.md', 'rulings.md', 'PLAN.md']
        mtime = {'notes.md': 400, 'rulings_v1.md': 250, 'draft_CONVENTION.md': 350, 'rulings.md': 300, 'PLAN.md': 300}
        self.assertEqual(sorted(names, key=lambda n: U.final_sort_key(n, mtime[n])), ['PLAN.md', 'rulings.md', 'rulings_v1.md', 'notes.md', 'draft_CONVENTION.md'])

    def test_any_doc_when_no_conclusion_name(self):
        doc = self.put('draft_CONVENTION.md', 300)
        f = self.final(orch=[wr(doc, 300)])[1]
        self.assertEqual((f.confirmed, f.path), (True, doc))

    def test_a_document_nobody_wrote_is_no_final(self):
        rulings = self.put('rulings.md', 300)               # on the disk, after the reports, and no write of it in any record: the disk alone makes no final
        f = self.final()[1]
        self.assertEqual((f.confirmed, f.path, f.why, f.candidates), (False, None, ['none'], [rulings]))

    def test_empty_and_subfolders_ignored(self):
        empty = self.put('rulings.md', 300, text='')       # empty file
        deeper = self.file('t5_readability/work_A/final.md', 'x', 300)
        f = self.final(orch=[wr(empty, 300), wr(deeper, 300)])[1]
        self.assertEqual((f.confirmed, f.why, f.candidates), (False, ['none'], []))


class BundleFinal(Fixture):
    """A bundle `review` of two topics, each with two reports by agents who are done (step1: 100 and 110, step2: 200 and 210), a guide in the folder of the bundle, and a document the orchestrator writes there
    (D5): the final of the bundle. Its steps can be closed with it. The page (`debates.judge`) gives the final of the bundle as `final` of the debate and `closable` of each topic."""

    def setUp(self):
        super().setUp()
        self.bundle = self.p('review')
        self.file('review/brief.md', '# The review\n\nEvery step is a folder below.\n', 50)
        self.topics = [self.p('review', n) for n in ('step1', 'step2')]
        self.agents = []
        for n, t in (('step1', 100), ('step2', 200)):
            self.file('review/%s/brief.md' % n, '# Step\n', 50)
            for i, p in enumerate('AB'):
                path = self.file('review/%s/r1/%s.md' % (n, p), '# %s\n\nfindings\n' % p, t + 10 * i)
                self.agents.append(agent('%s%s' % (n, p), writes=[wr(path, t + 10 * i)]))

    def doc(self, rel, ts, text='closed\n'):
        """A document below the bundle with the time `ts`; the orchestrator's sure write of it."""
        return self.file('review/' + rel, text, ts), wr(self.p('review', rel), ts)

    def judge(self, *docs, agents=None, **kw):
        """(the debate as the page gets it, the Judged) of the reports and the documents (path, event) written by the orchestrator."""
        jd = self.page(*(self.agents if agents is None else agents), orch=[w for _p, w in docs], walked=self.topics, **kw)
        (d,) = jd.debates
        self.assertEqual(d['root'], self.bundle)
        return d, jd

    def closable(self, d):
        return {t['key']: t['closable'] for t in d['topics']}

    def test_a_document_after_every_report_closes_the_bundle_and_every_step(self):      # C54, through the page
        d, jd = self.judge(self.doc('CLOSING.md', 300))
        self.assertEqual((d['final']['confirmed'], d['final']['rel'], d['final']['by'], d['final']['scope'], d['final']['why']), (True, 'CLOSING.md', 'orch', 'bundle', []))
        self.assertEqual(self.closable(d), {'step1': True, 'step2': True})
        self.assertEqual({t['key']: t['final']['confirmed'] for t in d['topics']}, {'step1': False, 'step2': False})     # a step has no conclusion of its own

    def test_the_name_is_no_condition_but_a_guide_and_the_round_documents_are_never_final(self):
        docs = [self.doc(n, 300) for n in ('README.md', 'index.md', 'round2.md', 'r3_instructions.md')]
        d, _jd = self.judge(*docs)
        self.assertEqual((d['final']['confirmed'], d['final']['why'], d['final']['candidates']), (False, ['none'], []))
        d, _jd = self.judge(*docs, self.doc('notes.md', 310))                   # any other name competes; with no conclusion name it is still the only one
        self.assertEqual((d['final']['confirmed'], d['final']['rel']), (True, 'notes.md'))

    def test_a_document_that_came_before_the_last_report_closes_nothing(self):
        d, _jd = self.judge(self.doc('CLOSING.md', 150))                       # the last report is at 210
        self.assertEqual((d['final']['confirmed'], d['final']['why']), (False, ['none']))
        self.assertEqual(self.closable(d), {'step1': False, 'step2': False})
        closing = self.doc('CLOSING.md', 300)                                # and one report written after it takes it back
        later = self.file('review/step2/r1/B.md', 'revised\n', 350)
        d, _jd = self.judge(closing, agents=[*self.agents[:3], agent('step2B', writes=[wr(self.p('review/step2/r1/B.md'), 210), wr(later, 350, kind='update')])])
        self.assertEqual((d['final']['confirmed'], d['final']['why']), (False, ['none']))
        self.assertEqual(self.closable(d), {'step1': False, 'step2': False})

    def test_a_copy_that_gave_every_file_nearly_the_same_time_counts(self):
        d, _jd = self.judge(self.doc('CLOSING.md', 209.5))                       # the last report is at 210
        self.assertEqual((d['final']['confirmed'], d['final']['rel']), (True, 'CLOSING.md'))

    def test_a_step_that_is_not_submitted_holds_the_bundle_and_every_step(self):
        path = self.p('review', 'step2', 'r1', 'B.md')
        os.unlink(path)
        open_b = agent('step2B', planned=[plan(path)], status='running')        # asked to save it, not yet saved
        d, jd = self.judge(self.doc('CLOSING.md', 300), agents=[*self.agents[:3], open_b])
        self.assertEqual((d['final']['confirmed'], 'open_cell' in d['final']['why']), (False, True))
        self.assertEqual(self.closable(d), {'step1': False, 'step2': False})

    def test_somebody_who_is_still_working_holds_the_bundle(self):
        editor = agent('E', status='running', writes=[wr(self.p('review', 'step1', 'r1', 'A.md'), 150, kind='update')])
        d, jd = self.judge(self.doc('CLOSING.md', 300), agents=[*self.agents, editor])
        self.assertEqual((d['final']['confirmed'], d['final']['why']), (False, ['live_participant']))
        self.assertEqual(self.closable(d), {'step1': False, 'step2': False})
        done = agent('E', status='done', writes=editor.writes)                   # it is over: the bundle is final again
        self.assertTrue(self.judge(self.doc('CLOSING.md', 300), agents=[*self.agents, done])[0]['final']['confirmed'])

    def test_two_documents_after_the_reports_are_two_competitors(self):
        d, _jd = self.judge(self.doc('CLOSING.md', 300), self.doc('summary.md', 310))
        self.assertEqual((d['final']['confirmed'], d['final']['why']), (False, ['several']))
        self.assertEqual([c['rel'] for c in d['final']['candidates']], ['summary.md', 'CLOSING.md'])     # the latest of the two that look like a conclusion first

    def test_a_document_in_the_final_folder_of_the_bundle_counts(self):
        d, _jd = self.judge(self.doc('final/report.md', 300))
        self.assertEqual((d['final']['confirmed'], d['final']['rel']), (True, 'final/report.md'))
        self.assertEqual(self.closable(d), {'step1': True, 'step2': True})

    def test_an_empty_document_is_no_final(self):
        d, _jd = self.judge(self.doc('CLOSING.md', 300, text=''))
        self.assertEqual((d['final']['confirmed'], d['final']['why'], d['final']['candidates']), (False, ['none'], []))

    def test_a_conclusion_in_the_folder_of_one_step_closes_that_step_only(self):
        d, jd = self.judge(self.doc('step1/CLOSING.md', 300))
        self.assertEqual({t['key']: (t['final']['confirmed'], t['final']['rel']) for t in d['topics']}, {'step1': (True, 'CLOSING.md'), 'step2': (False, None)})
        self.assertEqual(self.closable(d), {'step1': True, 'step2': False})
        self.assertEqual((d['final']['confirmed'], d['final']['why']), (False, ['none']))      # nothing in the folder of the bundle

    def test_a_document_above_a_topic_that_is_not_below_a_guide_closes_nothing(self):
        # a topic deeper in a folder tree: the folder between it and the root has no guide, so the topic is a root of its own and no document above it is its final
        self.file('deep/brief.md', '# deep\n', 50)
        path = self.file('deep/part/step2/r1/A.md', 'a\n', 200)
        agents = [agent('Z', writes=[wr(path, 200)])]
        above, further = self.file('deep/part/CLOSING.md', 'closed\n', 300), self.file('deep/CLOSING.md', 'closed\n', 400)
        jd = self.assign(*agents, orch=[wr(above, 300), wr(further, 400)])
        unit = self.p('deep', 'part', 'step2')
        self.assertEqual((jd.finals[unit].confirmed, jd.finals[unit].why, jd.finals[unit].candidates), (False, ['none'], []))
        self.assertFalse(jd.closable[unit])
        self.file('deep/part/brief.md', '# part\n', 50)                      # a guide in that folder makes it the root of the bundle: its documents are the bundle's
        jd = self.assign(*agents, orch=[wr(above, 300), wr(further, 400)])
        self.assertEqual((jd.finals[self.p('deep', 'part')].confirmed, jd.finals[self.p('deep', 'part')].path), (True, above))
        self.assertTrue(jd.closable[unit])

    def test_a_topic_that_is_the_root_has_nothing_above_it(self):
        single = self.p('alone')
        path = self.file('alone/r1/A.md', 'a\n', 200)
        closing = self.file('alone/CLOSING.md', 'closed\n', 300)
        jd = self.page(agent('Z', writes=[wr(path, 200)]), orch=[wr(closing, 300)])
        (d,) = jd.debates
        self.assertEqual((d['root'], d['final']), (single, None))               # no bundle: the topic is its own root and the document is its own final
        self.assertEqual((d['topics'][0]['final']['confirmed'], d['topics'][0]['final']['rel'], d['topics'][0]['final']['scope']), (True, 'CLOSING.md', 'topic'))


if __name__ == '__main__':
    unittest.main()
