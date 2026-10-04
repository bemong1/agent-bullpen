"""Affiliation: who launched a `claude -p` child session. board/affil.py (the judgment, pure), the facts board/link.py gathers for it
(launch arguments, the redirect reader, calls with their ends, the run starts of a child), and the link cache.

Three layers:
  1. the judgment on hand-made facts (ranks, ties held, tree / node / call / by kept apart, the refutations);
  2. the readers: `launch_facts` (one redirect reader for the output proof and for the report path), `can_launch`, `RunStarts`;
  3. the generator cases (tools/scenarios): every `aff` case is built into a synthetic HOME, read by LinkIndex, and compared with the oracle on tree, node,
     rule class, `by`, `unlinked` and the diagnostics this layer owns. The board and the oracle agree on every case.

    python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402,F401  (puts the repo root first on sys.path)

from board import affil, fingerprint as fp, lineage, link  # noqa: E402
from board.facts import Redirect, Span  # noqa: E402
from tools.scenarios import axes, build, observe, oracle, run  # noqa: E402

T0 = 1790000000.0
A, B, C = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333'
KID = '44444444-4444-4444-8444-444444444444'
SUB1, SUB2 = 'a' + '1' * 16, 'a' + '2' * 16
TEXT = ('Please review the amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow nectar orchard prairie quartz ridge summit '
        'tundra umber valley willow xenon yarrow zephyr and report in plain words.')
OTHER = ('Summarise the failing tests of the lagoon module, list every suspect file, and propose the smallest change that makes the build green again today.')


# ---------------------------------------------------------------------------------------------------------------------
# hand-made facts
# ---------------------------------------------------------------------------------------------------------------------
def call(tree, node, start, end=None, cid=None, launches=(), lits=(), id_args=(), desc='', launching=None, assumed=()):
    sp = Span(owner_tree=tree, owner_node=node, call_id=cid or 'toolu_%s_%s' % (node or 'main', int(start)), start=start, end=end, launchy=True, redirects=[])
    return affil.Call(sp, list(launches), frozenset(lits), desc, None, frozenset(id_args), launching, **({'assumed': assumed} if assumed else {}))


def owner(tree, node=None, calls=(), text='', first=T0 - 5000, last=T0 + 5000, complete=True):
    n = fp.normalize(text)
    o = affil.Owner(tree, node, first, last, pool=lambda lo, hi: (n, complete), text_of=lambda c: n)
    for c in calls:
        o.add(c)
    return o


def launch(arg=None, cwd='/w', n=1, loop_args=(), persist=True, resume=None, session_id=None, redirects=(), reopens=False, src_text=None):
    return affil.Launch(cwd, n, fp.normalize(arg) if arg is not None else None, [fp.normalize(a) for a in loop_args], resume, session_id, persist, True,
                        list(redirects), reopens, src_text=src_text)


def child(text=TEXT, t0=T0 + 2, cwd='/w', sid=KID, extra_runs=()):
    runs = [affil.Instr(t0, 1, text)] + [affil.Instr(ts, i + 2, tx) for i, (ts, tx) in enumerate(extra_runs)]
    return affil.ChildFacts(sid, t0, cwd, 'sdk-cli', runs)


class Out:
    """The output-file proof of a child: files_with(sid) and writers(path)."""

    def __init__(self, files, writers):
        self.f, self.w = files, writers

    def files_with(self, sid):
        return self.f.get(sid, [])

    def writers(self, path):
        return self.w.get(path, [])


def writer_call(tree, node, start, path, op='>'):
    c = call(tree, node, start, end=start + 5)
    r = Redirect(1, op, path, path, [])
    c.span.redirects.append(r)
    return c, r


class Ranks(unittest.TestCase):
    def test_content_alone_is_firm_and_says_so(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, launches=[launch(None)])], text=TEXT)])
        self.assertEqual((d.tree, d.node, d.rule, d.rank, d.certain), (A, None, 'content', 3, True))
        self.assertIn('content_only', [c for c, _ in d.diags])
        self.assertEqual(d.by, [(A, None)])

    def test_node_comes_from_the_text_of_the_sub_agent_never_from_the_environment(self):
        """The text sits in the sub-agent's record: tree = the main session, node = that sub-agent."""
        owners = [owner(A, None, [], text='nothing'), owner(A, SUB1, [call(A, SUB1, T0 - 1, launches=[launch(None)])], text=TEXT)]
        d = affil.decide(child(), owners)
        self.assertEqual((d.tree, d.node, d.rank), (A, SUB1, 3))
        self.assertTrue(d.relation.certain['node'])

    def test_environment_names_the_tree_only(self):
        owners = [owner(A, None, [], text='x'), owner(A, SUB1, [], text='x')]
        d = affil.decide(child(), owners, pins=[{'kind': 'env', 'tree': A, 'ts': T0 + 2}])
        self.assertEqual((d.tree, d.rule, d.rank, d.certain), (A, 'env', 2, True))
        self.assertIsNone(d.node)                                              # None = the main session: the node was not decided ...
        self.assertFalse(d.relation.certain['node'])
        self.assertIn('node_unresolved', [c for c, _ in d.diags])             # ... and a sub-agent was alive, so it is said

    def test_environment_alone_is_not_a_node_problem_without_sub_agents(self):
        d = affil.decide(child(), [owner(A, None, [], text='x')], pins=[{'kind': 'proc', 'tree': A, 'ts': None}])
        self.assertEqual((d.tree, d.node, d.relation.certain['node']), (A, None, True))
        self.assertNotIn('node_unresolved', [c for c, _ in d.diags])

    def test_output_file_beats_the_process_and_the_conflict_is_reported(self):
        """The parent switched sessions (/resume). The output file names the old session, the process the new one."""
        wc, wr = writer_call(A, None, T0 - 1, '/o/out.json')
        d = affil.decide(child(), [owner(A, None, [wc], text='x'), owner(B, None, [], text='x')], pins=[{'kind': 'proc', 'tree': B, 'ts': None}],
                         out=Out({KID: ['/o/out.json']}, {'/o/out.json': [(wc, wr)]}))
        self.assertEqual((d.tree, d.rule, d.rank), (A, 'out', 1))
        self.assertEqual(d.call.span.call_id, wc.span.call_id)
        self.assertIn('evidence_conflict', [c for c, _ in d.diags])

    def test_several_writers_of_one_file_give_the_call_only_to_the_last_truncating_one(self):
        w1, r1 = writer_call(A, None, T0 - 100, '/o/f.json')
        w2, r2 = writer_call(A, None, T0 - 1, '/o/f.json')
        d = affil.decide(child(), [owner(A, None, [w1, w2])], out=Out({KID: ['/o/f.json']}, {'/o/f.json': [(w1, r1), (w2, r2)]}))
        self.assertEqual(d.call.span.call_id, w2.span.call_id)
        wa, ra = writer_call(A, None, T0 - 100, '/o/g.json', '>>')
        wb, rb = writer_call(A, None, T0 - 1, '/o/g.json', '>>')
        d = affil.decide(child(), [owner(A, None, [wa, wb])], out=Out({KID: ['/o/g.json']}, {'/o/g.json': [(wa, ra), (wb, rb)]}))
        self.assertEqual((d.tree, d.call), (A, None))                          # appended by several calls: the owner is clear, the call is not

    def test_output_file_of_two_owners_is_a_tie(self):
        wa, ra = writer_call(A, None, T0 - 2, '/o/f.json', '>>')
        wb, rb = writer_call(B, None, T0 - 1, '/o/f.json', '>>')
        d = affil.decide(child(), [owner(A, None, [wa]), owner(B, None, [wb])], out=Out({KID: ['/o/f.json']}, {'/o/f.json': [(wa, ra), (wb, rb)]}))
        self.assertEqual((d.tree, d.held), (None, 'ambiguous'))
        self.assertEqual(set(d.held_trees), {A, B})

    def test_an_old_output_file_proves_nothing(self):
        wc, wr = writer_call(A, None, T0 - 3 * 3600, '/o/out.json')
        d = affil.decide(child(), [owner(A, None, [wc], text='x')], out=Out({KID: ['/o/out.json']}, {'/o/out.json': [(wc, wr)]}))
        self.assertIsNone(d.tree)


class Ties(unittest.TestCase):
    """A tie at the best rank holds the field; the judgment never falls through to a lower rank to break it."""

    def two_owners(self):
        both = [launch(TEXT)]
        return [owner(A, None, [call(A, None, T0, launches=both)], text=TEXT), owner(B, None, [call(B, None, T0 + 0.5, launches=both)], text=TEXT)]

    def test_same_text_in_two_trees_is_held_and_time_does_not_break_it(self):
        d = affil.decide(child(), self.two_owners())
        self.assertEqual((d.tree, d.held), (None, 'ambiguous'))
        self.assertIn('ambiguous_content', [c for c, _ in d.diags])
        self.assertEqual(set(d.held_trees), {A, B})

    def test_a_competitor_in_the_childs_folder_ties_but_one_in_another_folder_is_refuted(self):
        """A regression case: the same words launched twice. In one folder they tie (held); a competitor in another folder is not a candidate,
        so the child is not a tie there - the generator builds the first shape, with the competing launch in the target's folder."""
        same = [owner(A, None, [call(A, None, T0, launches=[launch(TEXT, cwd='/w/sub')])], text=TEXT), owner(B, None, [call(B, None, T0 + 0.5, launches=[launch(TEXT, cwd='/w/sub')])], text=TEXT)]
        d = affil.decide(child(cwd='/w/sub'), same)
        self.assertEqual((d.tree, d.held), (None, 'ambiguous'))
        other = [same[0], owner(B, None, [call(B, None, T0 + 0.5, launches=[launch(TEXT, cwd='/w')])], text=TEXT)]
        d = affil.decide(child(cwd='/w/sub'), other)
        self.assertEqual((d.tree, d.rank), (A, 3))

    def test_a_tie_between_two_sub_agents_of_one_tree_keeps_the_tree(self):
        owners = [owner(A, SUB1, [call(A, SUB1, T0, launches=[launch(None)])], text=TEXT), owner(A, SUB2, [call(A, SUB2, T0, launches=[launch(None)])], text=TEXT)]
        d = affil.decide(child(), owners)
        self.assertEqual((d.tree, d.node, d.held), (A, None, None))
        self.assertTrue(d.relation.certain['tree'])
        self.assertFalse(d.relation.certain['node'])
        self.assertIn('node_unresolved', [c for c, _ in d.diags])

    def test_one_clearly_ahead_is_not_a_tie(self):
        owners = self.two_owners()
        owners[1] = owner(B, None, [call(B, None, T0 + 0.5, launches=[launch(OTHER)])], text=OTHER)
        d = affil.decide(child(), owners)
        self.assertEqual((d.tree, d.rank), (A, 3))

    def test_an_id_written_in_the_call_decides_a_tie_of_texts(self):
        owners = self.two_owners()
        owners[0].calls[0].id_args = frozenset([KID])
        d = affil.decide(child(), owners)
        self.assertEqual((d.tree, d.rule), (A, 'content'))                     # the id is stored under the content rule, kind `resume`
        self.assertEqual(d.kind, 'resume')

    def test_the_childs_own_id_in_a_running_call_beats_a_text_that_fits_another_owner(self):
        """`--resume <id>` names the child itself; the same words in another owner's text (a twin that launched them too) do not."""
        mine = owner(A, None, [call(A, None, T0, launches=[launch(None)], id_args=[KID])], text='read through $(cat file)')
        twin = owner(B, None, [call(B, None, T0 + 0.5, launches=[launch(TEXT)])], text=TEXT)
        d = affil.decide(child(), [mine, twin])
        self.assertEqual((d.tree, d.kind, d.held), (A, 'resume', None))
        other = owner(B, None, [call(B, None, T0 + 0.5, launches=[launch(None)], id_args=[KID])], text='x')
        self.assertEqual((affil.decide(child(), [mine, other]).tree, affil.decide(child(), [mine, other]).held), (None, 'ambiguous'))      # two owners with the id: held

    def test_the_command_reader_and_the_text_agree_with_one_owner_only(self):
        """Two launches in one folder at the same moment: the instruction names one of them (the other is refuted: its literal prompt differs from the child's first instruction)."""
        owners = [owner(A, None, [call(A, None, T0, launches=[launch(TEXT)])], text=TEXT), owner(B, None, [call(B, None, T0 + 0.5, launches=[launch(OTHER)])], text=OTHER)]
        d = affil.decide(child(), owners)
        self.assertEqual((d.tree, d.held), (A, None))


class Refutations(unittest.TestCase):
    """A launch that cannot be the parent is not a candidate at any rank."""

    def one(self, ln, text=TEXT, cwd='/w', **kw):
        return affil.decide(child(text, cwd=cwd), [owner(A, None, [call(A, None, T0, launches=[ln], **kw)], text=text)])

    def test_literal_argument_that_differs_vetoes_even_with_the_text_in_the_record(self):
        self.assertIsNone(self.one(launch(OTHER)).tree)
        self.assertEqual(self.one(launch(TEXT)).tree, A)

    def test_quoting_differences_do_not_veto(self):
        self.assertEqual(self.one(launch(TEXT.replace(' ', '  '))).tree, A)

    def test_other_folder_vetoes_even_with_the_same_text(self):
        self.assertIsNone(self.one(launch(TEXT, cwd='/elsewhere')).tree)
        self.assertEqual(self.one(launch(TEXT, cwd=None)).tree, A)             # a folder that is not known refutes nothing

    def test_no_session_persistence_cannot_be_the_parent_of_a_record(self):
        self.assertIsNone(self.one(launch(TEXT, persist=False)).tree)

    def test_loop_over_a_literal_list_only_produces_the_listed_words(self):
        self.assertEqual(self.one(launch(None, loop_args=['a', TEXT])).tree, A)
        self.assertIsNone(self.one(launch(None, loop_args=['a', 'b'])).tree)

    def test_a_call_the_reader_could_not_read_is_not_refuted(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, lits=['x'], launching=True)], text=TEXT)])        # names claude, no launch placed
        self.assertEqual((d.tree, d.rank), (A, 3))

    def test_not_running_at_the_start_is_not_a_launcher(self):
        o = lambda end: owner(A, None, [call(A, None, T0 - 100, end=end, launches=[launch(TEXT)])], text=TEXT)
        self.assertEqual(affil.decide(child(), [o(T0 - 8)]).tree, A)           # ended 10 s before the child's first line: still the launcher (the grace)
        self.assertIsNone(affil.decide(child(), [o(T0 - 20)]).tree)
        self.assertEqual(affil.decide(child(), [o(None)]).tree, A)             # an end that was never seen: running
        c = call(A, None, T0 + 30, launches=[launch(TEXT)])
        self.assertIsNone(affil.decide(child(), [owner(A, None, [c], text=TEXT)]).tree)   # started after the child

    def test_the_child_is_never_its_own_launcher(self):
        d = affil.decide(child(sid=A), [owner(A, None, [call(A, None, T0, launches=[launch(TEXT)])], text=TEXT)])
        self.assertIsNone(d.tree)

    def test_text_that_does_not_fit_gives_no_content_link(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0)], text=OTHER)])
        self.assertIsNone(d.tree)


class Lower(unittest.TestCase):
    def test_short_instruction_equal_to_a_literal_is_a_guess(self):
        d = affil.decide(child('/init'), [owner(A, None, [call(A, None, T0, lits=['/init'], launches=[launch('/init')])])])
        self.assertEqual((d.tree, d.rule, d.rank, d.certain), (A, 'content_short', 4, False))

    def test_short_instruction_in_a_loop_list(self):
        d = affil.decide(child('/init'), [owner(A, None, [call(A, None, T0, launches=[launch(None, loop_args=['/other', '/init'])])])])
        self.assertEqual((d.tree, d.rule), (A, 'content_short'))

    def test_short_instruction_of_two_owners_is_held(self):
        owners = [owner(A, None, [call(A, None, T0, lits=['/init'], launching=('claude',))]), owner(B, None, [call(B, None, T0, lits=['/init'], launching=('claude',))])]
        self.assertEqual(affil.decide(child('/init'), owners).held, 'ambiguous')

    def test_time_rule_needs_one_call_in_the_same_folder(self):
        ln = launch(None)
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, launches=[ln])], text='x')])
        self.assertEqual((d.tree, d.rule, d.rank, d.certain), (A, 'time', 5, False))
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, launches=[ln])], text='x'), owner(B, None, [call(B, None, T0, launches=[launch(None)])], text='x')])
        self.assertEqual((d.tree, d.held), (None, 'ambiguous'))
        d = affil.decide(child(cwd='/link'), [owner(A, None, [call(A, None, T0, launches=[launch(None, cwd='/real')])], text='x')])
        self.assertIsNone(d.tree)                                              # a guess compares folder texts, not realpaths

    def test_a_certain_rule_is_never_made_of_a_guess(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, launches=[launch(None)])], text='x')], pins=[{'kind': 'env', 'tree': B, 'ts': None}])
        self.assertEqual((d.tree, d.rule), (B, 'env'))                         # the process (rank 2) before the time rule (rank 5)

    def test_incomplete_text_never_makes_a_firm_link(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, launches=[launch(None)])], text=TEXT, complete=False)])
        self.assertEqual((d.tree, d.certain, d.incomplete), (A, False, True))
        self.assertIn('fingerprint_incomplete', [c for c, _ in d.diags])

    def test_remembered_link_only_when_nothing_else_speaks(self):
        d = affil.decide(child(), [owner(A, None, [], text='x')], saved={'tree': A, 'node': SUB1})
        self.assertEqual((d.tree, d.node, d.rule, d.certain), (A, SUB1, 'cache', True))
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0, launches=[launch(TEXT)])], text=TEXT)], saved={'tree': B, 'node': None})
        self.assertEqual((d.tree, d.rule), (A, 'content'))

    def test_content_flag_off_is_the_quick_first_judgment(self):
        o = [owner(A, None, [call(A, None, T0, launches=[launch(None)])], text=TEXT)]
        self.assertEqual(affil.decide(child(), o, content=False).rule, 'time')
        self.assertEqual(affil.decide(child(), o, content=True).rule, 'content')


class RunsAndBy(unittest.TestCase):
    """A resumed run says who resumed it; the parent stays the first launcher."""

    def setUp(self):
        self.first = call(A, None, T0 - 10, end=T0 + 20, launches=[launch(TEXT)])
        self.resume = call(A, SUB1, T0 + 3600 - 5, end=T0 + 3700, launches=[launch(OTHER)], id_args=[KID])
        self.owners = [owner(A, None, [self.first], text=TEXT), owner(A, SUB1, [self.resume], text=OTHER)]

    def test_by_of_a_resume_and_tree_of_the_first_run(self):
        d = affil.decide(child(extra_runs=[(T0 + 3600, OTHER)]), self.owners)
        self.assertEqual((d.tree, d.node), (A, None))
        self.assertEqual(d.by, [(A, None), (A, SUB1)])

    def test_environment_of_the_resumed_process_is_by_only(self):
        c = child(extra_runs=[(T0 + 3600, OTHER)])
        d = affil.decide(c, [owner(A, None, [], text='x'), owner(B, None, [], text='x')], pins=[{'kind': 'env', 'tree': B, 'ts': T0 + 3600}])
        self.assertIsNone(d.tree)                                              # the tree is decided by the first run: nobody launched it that we can see
        self.assertEqual(d.by, [None, (B, None)])

    def test_process_of_the_first_run_is_tree_evidence(self):
        c = child(extra_runs=[(T0 + 3600, OTHER)])
        d = affil.decide(c, [owner(A, None, [], text='x')], pins=[{'kind': 'env', 'tree': A, 'ts': T0 + 2}])
        self.assertEqual((d.tree, d.rule), (A, 'env'))

    def test_a_pin_without_a_start_time_belongs_to_the_latest_run(self):
        c = child(extra_runs=[(T0 + 3600, OTHER)])
        d = affil.decide(c, [owner(A, None, [], text='x')], pins=[{'kind': 'file', 'tree': A, 'ts': None}])
        self.assertIsNone(d.tree)
        self.assertEqual(d.by[-1], (A, None))

    def test_a_run_nobody_launched_has_no_by(self):
        d = affil.decide(child(extra_runs=[(T0 + 9000, OTHER)]), self.owners)
        self.assertEqual(d.by[1], None)


class Claims(unittest.TestCase):
    """Which launching calls a child's runs are accounted to (`Decision.claims`); a launch no child is accounted to is an orphan."""

    def ids(self, d):
        return [c.span.call_id for c in d.claims]

    def test_the_known_call_is_the_only_one_claimed(self):
        a = call(A, None, T0, launches=[launch(TEXT)], cid='ca')
        d = affil.decide(child(), [owner(A, None, [a], text=TEXT)])
        self.assertEqual((d.call is a, self.ids(d)), (True, ['ca']))

    def test_when_the_call_is_not_known_every_candidate_of_the_tree_is_claimed(self):
        a, b = call(A, None, T0 - 1, launches=[launch(TEXT)], cid='ca'), call(A, None, T0, launches=[launch(TEXT)], cid='cb')
        d = affil.decide(child(), [owner(A, None, [a, b], text=TEXT)])
        self.assertEqual((d.tree, d.call, sorted(self.ids(d))), (A, None, ['ca', 'cb']))            # over-counting only hides an orphan

    def test_a_stranger_every_launch_of_which_is_refuted_claims_nothing(self):
        c = call(A, None, T0, launches=[launch(OTHER)], cid='ca')
        self.assertEqual(affil.decide(child(), [owner(A, None, [c], text=TEXT)]).claims, [])
        c = call(A, None, T0, launches=[launch(TEXT, cwd='/elsewhere')], cid='cb')
        self.assertEqual(affil.decide(child(), [owner(A, None, [c], text=TEXT)]).claims, [])

    def test_a_tie_claims_every_call_it_is_held_between(self):
        a, b = call(A, None, T0, launches=[launch(TEXT)], cid='ca'), call(B, None, T0, launches=[launch(TEXT)], cid='cb')
        d = affil.decide(child(), [owner(A, None, [a], text=TEXT), owner(B, None, [b], text=TEXT)])
        self.assertEqual(d.held, 'ambiguous')
        self.assertEqual(sorted(self.ids(d)), ['ca', 'cb'])

    def test_a_run_that_cannot_be_placed_accounts_for_read_calls_only(self):
        """The launch folder is a symlink to the child's folder: not refuted (realpath), but no guess either (folder texts differ) - the run stays unplaced."""
        with tempfile.TemporaryDirectory() as d_:
            real = os.path.join(d_, 'real')
            os.makedirs(real)
            os.symlink(real, os.path.join(d_, 'lnk'))
            read = call(A, None, T0, launches=[launch(None, cwd=os.path.join(d_, 'lnk'))], cid='read')
            weak = call(B, None, T0, launches=[affil.Launch(None, 1, None, (), None, None, True, False, [])], cid='weak')
            d = affil.decide(child('/init', cwd=real), [owner(A, None, [read], text='x'), owner(B, None, [weak], text='x')])
        self.assertEqual((d.tree, d.held), (None, None))
        self.assertEqual(self.ids(d), ['read'])                                     # a launch the reader could not read is not accounted for by a child it cannot place

    def test_a_resumed_run_claims_the_call_that_was_running_then(self):
        first = call(A, None, T0 - 10, end=T0 + 20, launches=[launch(TEXT)], cid='first')
        resume = call(A, None, T0 + 3600 - 5, end=T0 + 3700, launches=[launch(OTHER)], id_args=[KID], cid='resume')
        d = affil.decide(child(extra_runs=[(T0 + 3600, OTHER)]), [owner(A, None, [first, resume], text=TEXT + OTHER)])
        self.assertEqual(sorted(self.ids(d)), ['first', 'resume'])

    def test_the_output_file_of_the_first_launch_does_not_name_the_call_of_a_later_run(self):
        w1, r1 = writer_call(A, None, T0 - 1, '/o/f.json')
        later = call(A, None, T0 + 3600 - 5, end=T0 + 3700, launches=[launch(OTHER)], cid='resume')
        out = Out({KID: ['/o/f.json']}, {'/o/f.json': [(w1, r1)]})
        d = affil.decide(child(extra_runs=[(T0 + 3600, OTHER)]), [owner(A, None, [w1, later], text=TEXT)], out=out)
        self.assertEqual(d.rule, 'out')
        self.assertEqual(d.by[1], (A, None))                                     # the later run is placed by what ran then, not by the old file
        self.assertIn('resume', [c.span.call_id for c in d.claims])


class PerRun(unittest.TestCase):
    """`Decision.calls` / `run_claims` / `run_ts`: what the evidence says about the call of each run (a resumed run has its own)."""

    def test_each_run_has_its_own_call_and_start(self):
        first = call(A, None, T0 - 10, end=T0 + 20, launches=[launch(TEXT)], cid='first')
        resume = call(A, None, T0 + 3600 - 5, end=T0 + 3700, launches=[launch(OTHER)], id_args=[KID], cid='resume')
        d = affil.decide(child(extra_runs=[(T0 + 3600, OTHER)]), [owner(A, None, [first, resume], text=TEXT + OTHER)])
        self.assertEqual([c.span.call_id if c else None for c in d.calls], ['first', 'resume'])
        self.assertEqual([[c.span.call_id for c in cl] for cl in d.run_claims], [['first'], ['resume']])
        self.assertEqual(d.run_ts, [T0 + 2, T0 + 3600])

    def test_a_run_the_evidence_does_not_name_has_candidates_and_no_call(self):
        a, b = call(A, None, T0 - 1, launches=[launch(TEXT)], cid='ca'), call(A, None, T0, launches=[launch(TEXT)], cid='cb')
        d = affil.decide(child(), [owner(A, None, [a, b], text=TEXT)])
        self.assertEqual(d.calls, [None])
        self.assertEqual(sorted(c.span.call_id for c in d.run_claims[0]), ['ca', 'cb'])
        self.assertEqual(sorted(c.span.call_id for c in d.claims), ['ca', 'cb'])             # `claims` stays the union of the runs'

    def test_a_child_without_an_instruction_line_is_one_run_at_its_start_time(self):
        a = call(A, None, T0, launches=[launch(None)], cid='ca')
        d = affil.decide(affil.ChildFacts(KID, T0 + 2, '/w', 'sdk-cli', []), [owner(A, None, [a], text='x')])
        self.assertEqual(d.run_ts, [T0 + 2])
        self.assertEqual(len(d.calls), 1)


class Matching(unittest.TestCase):
    """Runs are matched to the launches that could have started them (affil.assign_launches): a launch is given at most its count of runs, the
    earliest run takes the earliest running call, and a launch no run was given to is the one that has no child."""

    def one(self, key, ts, cands, named=None):
        return (key, ts, named, cands)

    def id_of(self, asg, key):
        c = asg.call_of[key]
        return c.span.call_id if c else None

    def test_a_run_goes_to_the_call_running_at_its_start_and_not_to_one_that_ended_within_the_grace(self):
        old = call(A, None, T0 - 22, end=T0 - 8, launches=[launch(TEXT)], cid='old')
        new = call(A, None, T0 - 1, launches=[launch(TEXT)], cid='new')
        asg = affil.assign_launches([self.one('r', T0, [old, new])])
        self.assertEqual(self.id_of(asg, 'r'), 'new')

    def test_the_earlier_run_takes_the_earlier_call_and_each_call_gets_its_count(self):
        a, b = call(A, None, T0 - 5, launches=[launch(TEXT)], cid='a'), call(A, None, T0 - 4, launches=[launch(TEXT)], cid='b')
        asg = affil.assign_launches([self.one('r2', T0 + 1, [b, a]), self.one('r1', T0, [b, a])])
        self.assertEqual((self.id_of(asg, 'r1'), self.id_of(asg, 'r2')), ('a', 'b'))
        self.assertEqual((asg.filled(a), asg.filled(b)), (1, 1))

    def test_a_later_run_moves_an_earlier_one_so_that_every_run_has_a_launch(self):
        a, b = call(A, None, T0 - 5, launches=[launch(TEXT)], cid='a'), call(A, None, T0 - 4, launches=[launch(TEXT)], cid='b')
        asg = affil.assign_launches([self.one('r1', T0, [a, b]), self.one('r2', T0 + 1, [a])])
        self.assertEqual((self.id_of(asg, 'r1'), self.id_of(asg, 'r2')), ('b', 'a'))

    def test_a_loop_call_is_given_as_many_runs_as_its_count(self):
        loop = call(A, None, T0 - 5, launches=[launch(None, n=3)], cid='loop')
        asg = affil.assign_launches([self.one(i, T0 + i, [loop]) for i in range(5)])
        self.assertEqual(asg.filled(loop), 3)                                       # the other two fit nowhere: nothing to count, nothing to say
        self.assertEqual([self.id_of(asg, i) for i in range(5)], ['loop'] * 3 + [None, None])

    def test_the_call_the_evidence_names_is_kept_even_beyond_its_count(self):
        a = call(A, None, T0 - 5, launches=[launch(TEXT)], cid='a')
        asg = affil.assign_launches([self.one('r1', T0, [a], named=a), self.one('r2', T0 + 1, [a], named=a)])
        self.assertEqual((asg.filled(a), self.id_of(asg, 'r2'), sorted(asg.named, key=str)), (2, 'a', ['r1', 'r2']))

    def test_a_named_run_takes_its_place_before_the_others_are_matched(self):
        a, b = call(A, None, T0 - 5, launches=[launch(TEXT)], cid='a'), call(A, None, T0 - 4, launches=[launch(TEXT)], cid='b')
        asg = affil.assign_launches([self.one('r1', T0, [a, b]), self.one('r2', T0 + 1, [a], named=a)])
        self.assertEqual((self.id_of(asg, 'r1'), self.id_of(asg, 'r2')), ('b', 'a'))

    def test_launches_the_reader_understood_come_before_the_ones_it_could_not_place(self):
        weak = call(B, None, T0 - 5, launches=[affil.Launch(None, 1, None, (), None, None, True, False, [])], cid='weak')
        read = call(A, None, T0 - 1, launches=[launch(None)], cid='read')
        asg = affil.assign_launches([self.one('r', T0, [weak, read])])
        self.assertEqual((self.id_of(asg, 'r'), asg.filled(weak)), ('read', 0))
        asg = affil.assign_launches([self.one('r', T0, [weak])])                  # with nothing else to fit, a child it cannot place is given to it
        self.assertEqual(self.id_of(asg, 'r'), 'weak')

    def test_a_run_that_fits_two_launches_leaves_one_childless_and_either_may_be_it(self):
        a, b = call(A, None, T0 - 1, launches=[launch(TEXT)], cid='a'), call(B, None, T0 - 1, launches=[launch(TEXT)], cid='b')
        asg = affil.assign_launches([self.one('twin', T0, [a, b])])
        left = b if asg.filled(a) else a
        self.assertEqual({c.span.call_id for c in asg.reach(left)}, {'a', 'b'})

    def test_a_launch_no_run_can_move_to_is_surely_childless(self):
        a, b = call(A, None, T0 - 1, launches=[launch(TEXT)], cid='a'), call(B, None, T0 - 1, launches=[launch(TEXT)], cid='b')
        asg = affil.assign_launches([self.one('r', T0, [a])])
        self.assertEqual((asg.filled(a), [c.span.call_id for c in asg.reach(a)]), (1, ['a']))
        self.assertEqual([c.span.call_id for c in asg.reach(b)], ['b'])

    def test_a_launch_that_asked_for_no_record_takes_no_run(self):
        a = call(A, None, T0 - 1, launches=[launch(TEXT, persist=False)], cid='a')
        asg = affil.assign_launches([self.one('r', T0, [a])])
        self.assertIsNone(asg.call_of['r'])


class Orders(unittest.TestCase):
    def test_settle_prefers_the_best_rank_and_holds_ties(self):
        s = affil.settle([(5, 'time', A, None, None), (3, 'content', B, None, None)], [])
        self.assertEqual((s['tree'], s['rank']), (B, 3))
        s = affil.settle([(3, 'content', A, None, None), (3, 'content', B, None, None)], [])
        self.assertTrue(s['held'])
        s = affil.settle([(2, 'env', A, None, None)], [(3, 'content', [(B, None), (C, None)])])
        self.assertEqual((s['tree'], s['held']), (A, False))

    def test_the_node_is_taken_inside_the_decided_tree_only(self):
        s = affil.settle([(2, 'env', A, None, None), (3, 'content', B, SUB1, None)], [])
        self.assertEqual((s['tree'], s['node'], s['node_known']), (A, None, False))


# ---------------------------------------------------------------------------------------------------------------------
# the readers
# ---------------------------------------------------------------------------------------------------------------------
def facts(cmd, cwd='/w', env=None, tool='claude'):
    return link.launch_facts(cmd, link.shell_code(cmd), cwd, env, tool)


def reds(cmd, **kw):
    return [(r.fd, r.op, r.path_resolved, tuple(r.unresolved_vars)) for L in facts(cmd, **kw) for r in L['redirects']]


class RedirectReader(unittest.TestCase):
    """One reader for the redirections of a launching command: the output proof and the report path both use it."""

    def test_fd_order(self):
        self.assertEqual(reds('claude -p x > /o/a.json'), [(1, '>', '/o/a.json', ())])
        self.assertEqual(reds('claude -p x > /o/a.json 2>&1'), [(1, '>', '/o/a.json', ()), (2, '2>&1', '/o/a.json', ())])
        self.assertEqual(reds('claude -p x 2>&1 > /o/a.json'), [(2, '2>&1', None, ()), (1, '>', '/o/a.json', ())])      # fd 2 still on the terminal
        self.assertEqual(reds('claude -p x > /o/a.json 2> /o/e.log'), [(1, '>', '/o/a.json', ()), (2, '2>', '/o/e.log', ())])
        self.assertEqual(reds('claude -p x &> /o/both.log'), [(1, '>', '/o/both.log', ()), (2, '2>', '/o/both.log', ())])
        self.assertEqual(reds('claude -p x >> /o/log.txt'), [(1, '>>', '/o/log.txt', ())])

    def test_not_redirections(self):
        self.assertEqual(reds('claude -p "a > b" '), [])                         # inside a quote
        self.assertEqual(reds('claude -p x | tee /o/a.json'), [])                 # a pipe is not a redirect
        self.assertEqual(reds('claude -p x < in.txt'), [])
        self.assertEqual(reds('claude -p x; echo > /o/other'), [])                # another command

    def test_substitution_inside_the_instruction_does_not_cut_the_command(self):
        self.assertEqual(reds('claude -p "$(cat /p/a.md)" > /o/a.json 2>&1'), [(1, '>', '/o/a.json', ()), (2, '2>&1', '/o/a.json', ())])

    def test_variables_only_from_the_commands_own_scope(self):
        self.assertEqual(reds('OUT=/tmp/o/x.json; claude -p x > "$OUT"'), [(1, '>', '/tmp/o/x.json', ())])
        self.assertEqual(reds('claude -p x > "$SPD/out.json"'), [(1, '>', None, ('SPD',))])                 # not defined here: never searched for
        self.assertEqual(reds('claude -p x > "$(mktemp)"'), [(1, '>', None, ('$(…)',))])
        self.assertEqual(reds('claude -p x > ${R}/o.json', env={'R': '/scr'}), [(1, '>', '/scr/o.json', ())])    # a script's own variable
        self.assertEqual(reds('for t in a b; do claude -p x > /r/$t.json; done'), [(1, '>', '/r/a.json', ()), (1, '>', '/r/b.json', ())])
        self.assertEqual(reds('for t in $(ls); do claude -p x > /r/$t.json; done'), [(1, '>', None, ('t',))])  # a list that is not literal: unresolved

    def test_relative_paths_follow_the_shell_folder(self):
        self.assertEqual(reds('cd /x && claude -p y > out.json'), [(1, '>', '/x/out.json', ())])
        self.assertEqual(reds('claude -p y > out.json', cwd=None), [(1, '>', None, ('cwd',))])

    def test_codex_output_option(self):
        got = [(r.op, r.path_resolved) for L in facts('codex exec -o /o/last.md "x"', tool='codex') for r in L['redirects']]
        self.assertEqual(got, [('-o', '/o/last.md')])
        got = [(r.op, r.path_resolved) for L in facts('codex exec --output-last-message=/o/l.md "x" > /o/log 2>&1', tool='codex') for r in L['redirects']]
        self.assertIn(('>', '/o/log'), got)


class LaunchArguments(unittest.TestCase):
    def one(self, cmd, **kw):
        got = facts(cmd, **kw)
        self.assertEqual(len(got), 1, cmd)
        return got[0]

    def test_literal_argument_is_normalised(self):
        self.assertEqual(self.one('claude -p --model m "Please  \\"review\\" it"')['arg'], 'Please review it')
        self.assertIsNone(self.one('claude -p "$(cat p.md)"')['arg'])
        self.assertIsNone(self.one('claude -p "text $X"')['arg'])
        self.assertIsNone(self.one("claude -p <<'EOF'\ntext\nEOF")['arg'])        # the instruction comes in on stdin
        self.assertIsNone(self.one('claude -p a b')['arg'])                       # two words: which one is the instruction is not known

    def test_known_options_take_their_values(self):
        self.assertEqual(self.one('claude -p --model claude-sonnet-5-5 --effort high --output-format json "go on"')['arg'], 'go on')
        self.assertEqual(self.one('claude --model m -p "go on" --verbose')['arg'], 'go on')

    def test_loop_variable(self):
        L = self.one('for x in "first one" second; do claude -p "$x"; done')
        self.assertEqual((L['arg'], L['loop_args'], L['n']), (None, ('first one', 'second'), 2))
        L = self.one('for x in $(ls); do claude -p "$x"; done')
        self.assertEqual((L['arg'], L['loop_args']), (None, ()))

    def test_positional_arguments_of_a_script(self):
        L = self.one('cd "$1"\nclaude -p --model "$2" "$3"\n', env={'1': '/w', '2': 'm', '3': 'the words'})
        self.assertEqual(L['arg'], 'the words')

    def test_resume_session_id_and_persistence(self):
        L = self.one('claude -p --resume %s "go"' % A)
        self.assertEqual((L['resume'], L['arg']), (A, 'go'))
        L = self.one('claude -p --resume "go"')
        self.assertEqual((L['resume'], L['arg']), (None, 'go'))                   # --resume takes a value only when it is a session id
        self.assertEqual(self.one('claude -p --session-id %s "go"' % B)['session_id'], B)
        self.assertFalse(self.one('claude -p --no-session-persistence "go"')['persist'])
        self.assertTrue(self.one('claude -p "go"')['persist'])

    def test_env_dash_i_is_a_launch(self):
        self.assertEqual(len(facts('env -i PATH="$PATH" HOME="$HOME" claude -p "go"')), 1)
        self.assertEqual(len(facts('env -u FOO claude -p "go"')), 1)
        self.assertEqual(facts('echo claude -p "go"'), [])

    def test_working_folder_and_loop_count(self):
        L = self.one('cd /x && claude -p go')
        self.assertEqual((L['cwd'], L['n']), ('/x', 1))
        self.assertEqual(self.one('for a in 1 2 3; do claude -p go; done')['n'], 3)


class FolderFromOwnAssignments(unittest.TestCase):
    """`T=~/proj; cd $T/x && claude -p go`: the folder is known when the command itself says what the variable is (as the script reader and the Codex reader
    already did); a variable nothing in the command sets stays unknown."""

    def test_a_variable_the_command_assigns_is_followed_into_the_cd(self):
        self.assertEqual(facts('T=/t/proj; cd $T/x && claude -p go')[0]['cwd'], '/t/proj/x')
        self.assertEqual(facts('T=/t/proj\n(cd "$T/x" && claude -p go)')[0]['cwd'], '/t/proj/x')
        self.assertEqual(facts('T=~/proj; cd ${T}/x && claude -p go')[0]['cwd'], os.path.expanduser('~/proj/x'))

    def test_what_the_command_does_not_set_or_sets_from_another_command_stays_unknown(self):
        self.assertIsNone(facts('cd "$WT" && claude -p go')[0]['cwd'])
        self.assertIsNone(facts('T=$(mktemp -d); cd $T && claude -p go')[0]['cwd'])
        self.assertIsNone(facts('for d in a b; do (cd $T/$d && claude -p go); done')[0]['cwd'])

    def test_home_in_a_command_is_still_not_expanded(self):
        self.assertIsNone(facts('cd "$HOME/x" && claude -p go')[0]['cwd'])

    def test_a_loop_over_a_literal_list_gives_the_few_folders_the_launch_can_be_in(self):
        L = facts('T=/t/p; for d in a b c; do (cd $T/$d && claude -p go); done')[0]
        self.assertEqual((L['cwd'], L['cwds']), (None, ('/t/p/a', '/t/p/b', '/t/p/c')))
        self.assertEqual(facts('for d in a b; do (cd /t/p/$d && claude -p go); done')[0]['cwds'], ('/t/p/a', '/t/p/b'))
        self.assertEqual(facts('for d in a; do (cd /t/p/$d && claude -p go); done')[0]['cwd'], '/t/p/a')                # one value: the folder
        self.assertEqual(facts('cd /t/p && claude -p go')[0]['cwds'], ())

    def test_folders_that_are_not_all_known_give_none(self):
        for cmd in ('for d in a b; do (cd $T/$d && claude -p go); done', 'for d in a b; do (cd /t/p/$d/$E && claude -p go); done',
                    'for d in a $(ls); do (cd /t/p/$d && claude -p go); done', 'for d in *; do (cd /t/p/$d && claude -p go); done'):
            self.assertEqual(facts(cmd)[0]['cwds'], (), cmd)

    def test_a_launch_that_can_be_in_one_of_a_few_folders_fits_a_child_in_one_of_them_only(self):
        ln = launch(None, cwd=None)
        ln.cwds = ('/t/p/a', '/t/p/b')
        o = owner(A, None, [call(A, None, T0, launches=[ln])], text='nothing of it')
        for cwd, linked in (('/t/p/a', True), ('/t/p/b', True), ('/t/p/c', False), ('/w', False)):
            d = affil.decide(child(cwd=cwd), [o])
            self.assertEqual((d.tree, d.rule), (A, 'time') if linked else (None, None), cwd)


class InstructionRead(unittest.TestCase):
    """`claude -p "$(cat P)"` / `claude -p < P`: when the board knows what P held (the launcher wrote it with the Write tool), that launch can only have started a
    child whose instruction is in it (the veto of a launch for an instruction that is not a literal argument). What P held never makes a launch the parent: it only rules out."""
    OTHER_WORDS = fp.normalize(OTHER)

    def owners(self, text_of_file):
        author = owner(A, None, [call(A, None, T0 - 5, launches=[launch(None, src_text=lambda: text_of_file)])], text=TEXT)
        sub = owner(A, SUB1, [call(A, SUB1, T0, launches=[launch(None)])], text='nothing of it')
        return [author, sub]

    def test_a_launch_that_read_another_text_is_not_the_parent(self):
        d = affil.decide(child(), self.owners(self.OTHER_WORDS))
        self.assertEqual((d.tree, d.node, d.rule, d.certain), (A, SUB1, 'time', False))                # the sub-agent's launch, as a guess; the author's text proves nothing
        self.assertNotIn('content_only', [c for c, _ in d.diags])

    def test_the_same_when_the_launcher_is_another_session(self):
        author = owner(A, None, [call(A, None, T0 - 5, launches=[launch(None, src_text=lambda: self.OTHER_WORDS)])], text=TEXT)
        launcher = owner(B, None, [call(B, None, T0, launches=[launch(None)])], text='nothing of it')
        d = affil.decide(child(), [author, launcher])
        self.assertEqual((d.tree, d.rule, d.certain), (B, 'time', False))
        plain = owner(A, None, [call(A, None, T0 - 5, launches=[launch(None)])], text=TEXT)                  # an author whose launch reads a variable's words: still its content
        self.assertEqual(affil.decide(child(), [plain, launcher]).tree, A)

    def test_a_file_that_holds_the_instruction_or_is_not_known_changes_nothing(self):
        for held in (fp.normalize(TEXT), TEXT + ' and more', None):
            d = affil.decide(child(), self.owners(held))
            self.assertEqual((d.tree, d.rule), (A, 'content'), held)                                   # as before (the node is held: two nodes run launches that read files)

    def test_the_text_is_read_only_when_a_judgment_needs_it(self):
        asked = []
        o = owner(A, None, [call(A, None, T0, launches=[launch(TEXT, src_text=lambda: asked.append(1))])], text=TEXT)
        affil.decide(child(), [o])
        self.assertEqual(asked, [])                                                                    # a literal argument decides first, the file is not asked


class InstructionSource(unittest.TestCase):
    def src(self, cmd, **kw):
        return facts(cmd, **kw)[0]['src']

    def test_the_file_a_launch_reads_its_instruction_from(self):
        self.assertEqual(self.src('claude -p "$(cat /w/p.md)"'), '/w/p.md')
        self.assertEqual(self.src("claude -p --model m \"$(cat -- '/w/my p.md')\" > /o/out.json"), '/w/my p.md')
        self.assertEqual(self.src('cd /x && claude -p "$(cat p.md)"'), '/x/p.md')
        self.assertEqual(self.src('claude -p < /w/p.md'), '/w/p.md')
        self.assertEqual(self.src('P=/w/p.md; claude -p "$(cat $P)"'), '/w/p.md')

    def test_what_is_not_one_file_is_none(self):
        for cmd in ('claude -p "go"', 'claude -p "go $(cat /w/p.md)"', 'claude -p "$(cat $F)"', 'claude -p "$(cat a.md b.md)"', 'cat /w/p.md | claude -p',
                    'claude -p <<EOF\ngo\nEOF', 'claude -p "$(cat /w/p.md | head)"', 'claude -p "$X"'):
            self.assertIsNone(self.src(cmd), cmd)


class LongCommands(unittest.TestCase):
    """The first screen reads every Bash command of every record: one command of 200 KB with 20 000 quotes must not take seconds."""

    def test_thousands_of_quotes_in_one_line_are_read_in_a_blink(self):
        cmd = 'printf %s ' + ' '.join('"item number %d"' % i for i in range(20000)) + " && bash -c 'claude -p hi'"
        t = time.perf_counter()
        code = link.shell_code(cmd)
        took = time.perf_counter() - t
        self.assertEqual(len(code), len(cmd))
        self.assertLess(took, 2.0)
        self.assertIn('claude -p hi', code)                                       # the shell text of the last `bash -c` is still seen as code
        self.assertNotIn('item number', code)                                     # the quoted data is masked

    def test_a_shell_c_after_a_very_long_prefix_is_text_not_a_shell(self):
        """The boundary of the span: the assignments before `bash -c` are read as long as they are within CODE_ARG_SPAN characters of their simple command."""
        word = 'AA=1 '
        within = 'env ' + word * ((link.CODE_ARG_SPAN - 40) // len(word)) + "bash -c 'claude -p hi'"
        beyond = 'env ' + word * ((link.CODE_ARG_SPAN + 40) // len(word)) + "bash -c 'claude -p hi'"
        self.assertIn('claude -p hi', link.shell_code(within))
        self.assertNotIn('claude -p hi', link.shell_code(beyond))                  # masked as text: a missed launch, never a false one
        self.assertIn('claude -p hi', link.shell_code("env A=1 B=2 bash -c 'claude -p hi'"))


class DirectoryStack(unittest.TestCase):
    """`pushd X` moves the shell to X like `cd X`; `popd` goes back to a folder the board does not follow."""

    def test_pushd_is_a_cd(self):
        self.assertEqual(facts('pushd /x/y >/dev/null && claude -p go')[0]['cwd'], '/x/y')
        self.assertEqual(facts('pushd sub && claude -p go', cwd='/w')[0]['cwd'], '/w/sub')
        self.assertEqual(facts('cd /a; pushd b; claude -p go')[0]['cwd'], '/a/b')
        self.assertEqual(facts('(pushd /x && claude -p go); claude -p again', cwd='/w')[1]['cwd'], '/w')           # a subshell's pushd does not reach out of it

    def test_popd_and_pushd_without_a_folder_leave_it_unknown(self):
        self.assertIsNone(facts('pushd /x && popd && claude -p go')[0]['cwd'])
        self.assertIsNone(facts('pushd && claude -p go')[0]['cwd'])
        self.assertIsNone(facts('pushd +1 && claude -p go')[0]['cwd'])
        self.assertIsNone(facts('pushd -n /x && claude -p go')[0]['cwd'])
        self.assertIsNone(facts('pushd $WT && claude -p go')[0]['cwd'])

    def test_the_folder_of_a_pushd_launch_is_compared_with_the_child(self):
        """The wrong folder used to refute the real parent: `pushd X && claude -p ...` ran in X."""
        o = owner(A, None, [call(A, None, T0, launches=[launch(TEXT, cwd=facts('pushd /x && claude -p go')[0]['cwd'])])], text=TEXT)
        self.assertEqual(affil.decide(child(cwd='/x'), [o]).tree, A)


class OnlyPrinting(unittest.TestCase):
    def test_commands_that_only_print_are_not_launching_calls(self):
        for cmd in ("echo 'Run: claude -p \"x\"'", "printf '%s' 'claude -p x'", 'grep -c claude notes.txt', "git commit -m 'claude -p x'",
                    "cat > p.txt <<'EOF'\nclaude -p hello\nEOF", 'echo hi > out.txt'):
            self.assertFalse(link.can_launch(link._masked(cmd)), cmd)

    def test_commands_that_could_run_something_are(self):
        for cmd in ('claude -p x', "tmux new-session -d -s w 'claude -p x'", 'bash run.sh a', 'python3 launch.py', "printf '%s\\n' x | xargs -I{} claude -p {}",
                    'sleep 90 && cd /w && claude -p x', 'cd /x\nenv -i A=1 claude -p y', "cat > /s/run.sh <<'EOF'\nclaude -p hi\nEOF\nbash /s/run.sh"):
            self.assertTrue(link.can_launch(link._masked(cmd)), cmd)


class RunStartLines(unittest.TestCase):
    """The start instruction of every run of a child (runstate's run boundary, with the words of each instruction added)."""

    def feed(self, lines):
        rs = affil.RunStarts()
        for ln in lines:
            rs.feed(json.dumps(ln))
        return rs.runs

    @staticmethod
    def user(ts, text, **kw):
        d = {'type': 'user', 'timestamp': '2026-10-01T00:00:%06.3fZ' % ts, 'message': {'role': 'user', 'content': text}}
        d.update(kw)
        return d

    def sdk(self, ts, text, idx):
        return self.user(ts, text, promptSource='sdk', turnPosition={'promptIndex': 0, 'turnIndex': idx})

    def test_resume_is_a_new_run_after_cost_state_even_when_the_turn_index_keeps_rising(self):
        runs = self.feed([self.sdk(1, 'first', 1), {'type': 'cost-state', 'sessionId': A}, self.sdk(30, 'second', 2)])
        self.assertEqual([r.head for r in runs], ['first', 'second'])

    def test_a_turn_index_written_again_is_the_same_turn(self):
        runs = self.feed([self.sdk(1, 'first', 1), self.sdk(30, 'second', 1)])
        self.assertEqual([r.head for r in runs], ['first'])                         # the boundary is runstate's: only a cost-state ends a run

    def test_more_turns_of_one_process_are_not_new_runs(self):
        runs = self.feed([self.sdk(1, 'first', 1), self.sdk(5, 'again', 2), self.sdk(9, 'and again', 3)])
        self.assertEqual([r.head for r in runs], ['first'])

    def test_not_starts(self):
        runs = self.feed([self.user(1, '<task-notification>x</task-notification>', promptSource='system'), self.user(2, 'meta', isMeta=True, promptSource='sdk'),
                          {'type': 'queue-operation', 'operation': 'enqueue', 'content': 'q'},
                          self.user(4, '<cross-session-message from="x">hi</cross-session-message>'),
                          {'type': 'user', 'timestamp': '2026-10-01T00:00:05.000Z', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'x'}]}},
                          self.sdk(6, 'real', 1), self.user(7, 'typed later', promptSource='typed')])
        self.assertEqual([r.head for r in runs], ['real'])                           # the first instruction line opens the run; a later one in the same process does not

    def test_older_records_have_no_prompt_source(self):
        runs = self.feed([self.user(1, 'older style'), self.user(2, 'a second typed line')])
        self.assertEqual([r.head for r in runs], ['older style'])

    def test_a_slash_command_expansion_is_an_unknown_instruction(self):
        runs = self.feed([self.sdk(1, '<command-name>/init</command-name>\n<command-message>init is running</command-message>', 1)])
        self.assertEqual((len(runs), runs[0].head, runs[0].short), (1, '', None))
        self.assertTrue(runs[0].same_as('anything'))                                # it vetoes nothing

    def test_an_older_records_slash_command_opens_a_run_of_unknown_words(self):
        runs = self.feed([self.user(1, '<command-name>/init</command-name>\n<command-message>init is running</command-message>'), self.user(2, '<command-message>init is running</command-message>')])
        from board.util import parse_ts
        self.assertEqual([(r.head, r.ts) for r in runs], [('', parse_ts('2026-10-01T00:00:01.000Z'))])      # no marker, no turn: the run starts at the command line, the words are unknown

    def test_a_run_opened_by_a_line_that_is_no_instruction_is_given_its_words_when_they_come(self):
        rs = affil.RunStarts()
        ret = [rs.feed(json.dumps(ln)) for ln in (self.user(1, 'x', isMeta=True), self.sdk(30, 'the words', 1))]
        self.assertEqual(ret, [True, True])                                         # both lines are news to link.py: it judges the child again
        self.assertEqual([(r.head, round(r.ts) - round(rs.runs[0].ts)) for r in rs.runs], [('the words', 0)])
        self.assertEqual(rs.runs[0].ts, rs.tracker.runs[0].turns[0].ts)             # the instruction line's time, not the opening line's

    def test_long_instruction_is_cut_at_the_head(self):
        runs = self.feed([self.sdk(1, 'w ' * 40000, 1)])
        self.assertTrue(runs[0].truncated)
        self.assertLessEqual(len(runs[0].head), fp.HEAD_CHARS)


# ---------------------------------------------------------------------------------------------------------------------
# the generator cases
# ---------------------------------------------------------------------------------------------------------------------
OWNED_DIAG = ('content_only', 'ambiguous_content', 'node_unresolved', 'path_unresolved', 'evidence_conflict', 'orphan_launch')


def run_case(case):
    """Builds one case and reads it with LinkIndex; the (case, built, links) triple."""
    root = tempfile.mkdtemp(prefix='affil-')
    try:
        b = build.build_case(case, os.path.join(root, 'c'))
        cap = {}
        # observe freezes the clock per phase, but reads the link cache (links.json, rows older than a day or newer than 90 days are refused) before the first
        # phase: freeze it there too, so that the result does not depend on today's date
        with mock.patch.object(observe, 'read_final', lambda b_, obs, objs: cap.setdefault('links', objs.links)), mock.patch('time.time', lambda: b.phases[0].now):
            observe.observe(b)
        return b, cap['links']
    finally:
        shutil.rmtree(root, ignore_errors=True)


def compare(case):
    """The fields on which LinkIndex differs from the oracle for one case: {field: (got, want)}."""
    b, L = run_case(case)
    T = oracle.truth(case)
    sub = T.subjects.get('child', {})
    G = b.ids.get('child')
    o = L.cli_owners.get(G)
    bad = {}
    if 'tree' in sub:
        want = run.resolve(sub['tree'], b)
        got = o['sid'] if o else None
        if got != want:
            bad['tree'] = (got, want)
    if 'rule_class' in sub:
        got = 'none' if not o else ('certain' if link.certain(o['rule']) else 'guess')
        if got != sub['rule_class']:
            bad['rule_class'] = (got, sub['rule_class'])
    if 'node' in sub and o is not None:
        want = run.resolve(sub['node'], b)
        if o['node'] != want:
            bad['node'] = (o['node'], want)
    if 'by' in sub:
        want = run.resolve(sub['by'], b)
        got = tuple(o['by'][-1]) if o and o['by'] and o['by'][-1] else None
        if want is not None and got != tuple(want):
            bad['by'] = (got, want)
    if 'unlinked' in sub:
        got = next((it['reason'] for items in L.unlinked_map.values() for it in items if it['id'] == G), None)
        if got != sub['unlinked']:
            bad['unlinked'] = (got, sub['unlinked'])
    want_d = {c for c, _ in T.diag if c in OWNED_DIAG}
    allowed = {c for c, _ in T.allowed}                                 # said or not, they are not graded here (as run.grade_case)
    got_d = {d['code'] for d in L.diags if d['code'] in OWNED_DIAG and (d['subject'] == G or d['code'] == 'orphan_launch')}
    for code in (want_d - got_d) | (got_d - want_d - allowed):
        bad['diag:' + code] = (code in got_d, code in want_d)
    return bad


class GeneratorCases(unittest.TestCase):
    """Every `claude -p` affiliation case of the generator: tree, node, rule class, `by`, `unlinked` and the owned diagnostics equal the oracle's,
    with no exception."""

    @classmethod
    def setUpClass(cls):
        cls.cases = [c for c in run.select() if c.bundle == 'aff' and c.v['target'] == 'cli']

    def test_there_are_cases(self):
        self.assertGreater(len(self.cases), 300)

    def test_all_cases_match_the_oracle(self):
        differ = [(c.id, bad) for c, bad in ((c, compare(c)) for c in self.cases) if bad]
        self.assertEqual(differ[:5], [], '%d cases differ from the oracle' % len(differ))

    def test_no_case_links_what_the_oracle_calls_a_stranger(self):
        """The decoy twins (bait, foreign) are negatives: the board never links them (a false link is worse than a missed one)."""
        twins = [c for c in self.cases if c.v['bait'] != 'none']
        self.assertGreater(len(twins), 100)
        wrong = []
        for c in twins:
            b, L = run_case(c)
            if L.cli_owners.get(b.ids.get('child')):
                wrong.append(c.id)
        self.assertEqual(wrong, [])

    def test_the_real_cases_are_all_right(self):
        for name, c in axes.real_cases():
            if c.bundle != 'aff' or c.v['target'] != 'cli':
                continue
            bad = compare(c)
            self.assertEqual(bad, {}, name)


class Sensitivity(unittest.TestCase):
    """Each rule is load-bearing: with it switched off, the case that needs it is linked wrongly (or not at all)."""

    def case(self, **kw):
        base = dict(target='cli', spawner='main', way='direct', via='arg', src='call', form='new', cwd='same', out='none', timing='normal', seen='ended_unseen',
                    decoy='none', bait='none', os='linux')
        base.update(kw)
        return axes.normalize(axes.Case('aff', base))

    def linked(self, case):
        b, L = run_case(case)
        o = L.cli_owners.get(b.ids['child'])
        return o['sid'] if o else None

    def test_baseline_links_and_baits_do_not(self):
        self.assertIsNotNone(self.linked(self.case()))
        for bait in ('text_mismatch', 'cwd_mismatch', 'echo_only', 'too_old'):
            self.assertIsNone(self.linked(self.case(bait=bait)), bait)

    def test_without_the_refutation_a_different_literal_argument_links(self):
        with mock.patch.object(affil, 'launch_ok', lambda L, ch, r: True):
            self.assertIsNotNone(self.linked(self.case(bait='text_mismatch')))
            self.assertIsNotNone(self.linked(self.case(bait='cwd_mismatch')))

    def test_without_the_position_filter_an_echo_is_a_launch(self):
        with mock.patch.object(link, 'can_launch', lambda code: True), mock.patch.object(link, 'exec_parts', lambda text, depth=0: ((text,), (), ())):
            self.assertIsNotNone(self.linked(self.case(bait='echo_only')))                  # every word counts where it stands: the quoted text of an echo too

    def test_without_the_running_requirement_an_old_call_links(self):
        with mock.patch.object(affil, 'span_running', lambda span, t: True):
            self.assertIsNotNone(self.linked(self.case(bait='too_old')))

    def test_a_loop_list_is_a_closed_set(self):
        c = self.case(way='loop', decoy='short', bait='foreign')
        self.assertIsNone(self.linked(c))
        with mock.patch.object(affil, 'launch_ok', lambda L, ch, r: True):
            self.assertIsNotNone(self.linked(c))

    def test_a_slow_sequential_launch_is_found_without_a_window(self):
        """A1: the second child of one blocking call starts 121 s after the call began (the old 60 s window lost it)."""
        c = self.case(way='loop', timing='seq_late', decoy='short', cwd='other')
        self.assertIsNotNone(self.linked(c))

    def test_the_environment_names_the_tree_but_not_the_node(self):
        c = self.case(spawner='sub', way='pysub', src='absent', via='arg', seen='live')              # only the process and its environment say anything
        b, L = run_case(c)
        o = L.cli_owners[b.ids['child']]
        self.assertEqual((o['sid'], o['node']), (b.ids['orch'], None))
        self.assertIn('node_unresolved', [d['code'] for d in L.diags if d['subject'] == b.ids['child']])


class CodexFromSubAgent(unittest.TestCase):
    def test_codex_exec_of_a_sub_agent_gets_its_tree_and_node(self):
        c = axes.normalize(axes.Case('aff', dict(target='cx_exec', spawner='sub', seen='ended_unseen')))
        b, L = run_case(c)
        o = L.owners[b.ids['child']]
        self.assertEqual((o['sid'], o['node'], link.certain(o['rule'])), (b.ids['orch'], b.ids['sub'], True))

    def test_a_thread_placed_by_its_environment_gets_the_node_of_the_call_that_started_it(self):
        c = axes.normalize(axes.Case('aff', dict(target='cx_exec', spawner='sub', seen='live', way='later')))
        b, L = run_case(c)
        o = L.owners[b.ids['child']]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['call'] is not None), (b.ids['orch'], b.ids['sub'], 'prompt', True))   # a call that waited 90 s before it ran codex: it is still running, so the words find it

    TID = '019a0000-0000-7000-8000-000000000001'

    def thread(self, ts=T0 + 3, cwd='/w', first='x'):
        return {'id': self.TID, 'origin': 'exec', 'guardian': False, 'meta_ts': ts, 'cwd': cwd, 'first_user': first}

    def calls(self, *specs, text=TEXT):
        out = []
        for i, (tree, node, ts, cwd) in enumerate(specs):
            c = link.cx_parse_call(ts, 'toolu_%d' % i, {'command': 'cd %s && codex exec -m m "%s"' % (cwd, text), 'description': 'launch'}, cwd)
            c['node'] = node
            out.append((tree, c))
        return out

    def test_calls_that_agree_on_the_node_give_it_and_a_single_call_is_named(self):
        got = link.cx_link(self.calls((A, SUB1, T0, '/w')), [self.thread(first=TEXT)], {self.TID: A})[self.TID]
        self.assertEqual((got['node'], got['call'], got['bash_ts'], got['dt']), (SUB1, 'toolu_0', T0, 3.0))
        self.assertEqual(got['rule'], 'prompt')                                      # the instruction rule had it already: nothing changes

    def test_the_lookup_only_adds_the_node_to_a_thread_the_rules_could_not_place(self):
        got = link.cx_link(self.calls((A, SUB1, T0, '/w'), text='$Q'), [self.thread(first='other words')], {self.TID: A}, {self.TID: 'env'})[self.TID]
        self.assertEqual((got['rule'], got['node'], got['call']), ('env', SUB1, 'toolu_0'))

    def test_a_call_that_gave_other_words_is_not_looked_at_either(self):
        """The same refutation for the lookup: the call's complete literal instruction is not the thread's first words, so the call did not start it (no node, no call)."""
        got = link.cx_link(self.calls((A, SUB1, T0, '/w')), [self.thread(first='other words')], {self.TID: A}, {self.TID: 'env'})[self.TID]
        self.assertEqual((got['rule'], got.get('node'), got['call']), ('env', None, None))

    def test_calls_of_two_nodes_give_no_node(self):
        got = link.cx_link(self.calls((A, SUB1, T0, '/w'), (A, SUB2, T0 + 1, '/w'), text='$Q'), [self.thread(first='other words')], {self.TID: A}, {self.TID: 'env'})[self.TID]
        self.assertEqual((got.get('node'), got['call']), (None, None))

    def test_the_prompt_decides_between_calls_of_two_nodes(self):
        a = self.calls((A, SUB1, T0, '/w'), text='$Q')
        b = self.calls((A, SUB2, T0 + 1, '/w'), text='$Q')
        b[0][1]['L'][0]['literal'] = 0                                              # this one gives no words to compare
        got = link.cx_link(a + b, [self.thread(first='other words')], {self.TID: A}, {self.TID: 'env'})[self.TID]
        self.assertEqual(got.get('node'), None)
        a[0][1]['L'][0]['rx'] = re.compile('other words')
        a[0][1]['L'][0]['literal'] = 99
        got = link.cx_link(a + b, [self.thread(first='other words')], {self.TID: A}, {self.TID: 'env'})[self.TID]
        self.assertEqual(got['node'], SUB1)

    def test_a_call_of_another_session_or_folder_is_not_looked_at(self):
        got = link.cx_link(self.calls((B, SUB1, T0, '/w'), (A, SUB2, T0, '/elsewhere'), text='$Q'), [self.thread(first='other words')], {self.TID: A}, {self.TID: 'env'})[self.TID]
        self.assertEqual((got.get('node'), got['call']), (None, None))


# ---------------------------------------------------------------------------------------------------------------------
# calls, their ends and the missing 60 s window (LinkIndex on small records)
# ---------------------------------------------------------------------------------------------------------------------
from test_stage2 import CliFixture, bash_line, child_lines, dump, iso  # noqa: E402


def result_line(t, tid, bg=None):
    tur = {'stdout': 'ok', 'stderr': '', 'interrupted': False}
    if bg:
        tur['backgroundTaskId'] = bg
    return dump({'type': 'user', 'timestamp': iso(t), 'toolUseResult': tur,
                 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': 'ok'}]}})


def notification_line(t, tid, bg, shape='attachment'):
    body = '<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>completed</status>\n<summary>Background command "x" completed (exit code 0)</summary>\n</task-notification>' % (bg, tid)
    if shape == 'attachment':
        return dump({'type': 'attachment', 'timestamp': iso(t), 'attachment': {'type': 'queued_command', 'prompt': body, 'commandMode': 'task-notification'}})
    return dump({'type': 'user', 'timestamp': iso(t), 'promptSource': 'system', 'message': {'role': 'user', 'content': body}})


class SpanEnds(CliFixture):
    def span(self, tid='toolu_1', sid=None):
        self.links.scan()
        f = next(f for f in self.links.files.values() if f['sid'] == (sid or self.PARENT))
        return f['spans'][tid].span

    def test_foreground_call_ends_with_its_result(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"'), result_line(T0 + 7, 'toolu_1')])
        s = self.span()
        self.assertEqual((s.end, s.end_status, s.bg_id), (T0 + 7, 'result', None))

    def test_background_call_ends_with_its_notification_in_either_shape(self):
        for shape in ('attachment', 'user'):
            with self.subTest(shape):
                self.setUp()
                self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"'), result_line(T0 + 0.4, 'toolu_1', bg='b1'), notification_line(T0 + 33, 'toolu_1', 'b1', shape)])
                s = self.span()
                self.assertEqual((s.end, s.end_status, s.bg_id), (T0 + 33, 'notification', 'b1'))
                self.assertIn('exit code 0', s.end_reason)

    def test_background_call_without_a_notification_stays_open(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"'), result_line(T0 + 0.4, 'toolu_1', bg='b1')])
        s = self.span()
        self.assertEqual((s.end, s.bg_id), (None, 'b1'))

    def test_a_call_without_any_result_is_running(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"')])
        self.assertIsNone(self.span().end)

    def test_a_result_belongs_to_its_own_call(self):
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"', tid='toolu_1'), bash_line(T0 + 1, 'ls ./x.py', tid='toolu_2'), result_line(T0 + 2, 'toolu_2'),
                                 result_line(T0 + 9, 'toolu_1')])
        self.assertEqual(self.span('toolu_1').end, T0 + 9)
        self.assertEqual(self.span('toolu_2').end, T0 + 2)

    def test_no_window_a_slow_child_of_a_long_call_is_linked_and_a_late_one_after_the_call_is_not(self):
        """A1: sequential launches inside one call start 100+ s after the call began (the old 60 s window lost them); after the call ended nothing is linked."""
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"'), result_line(T0 + 200, 'toolu_1')])
        slow = '55555555-5555-4555-8555-555555555555'
        late = '66666666-6666-4666-8666-666666666666'
        self.write(slow, child_lines(T0 + 121))
        self.write(late, child_lines(T0 + 260))
        got = self.scan()
        self.assertEqual(set(got), {slow})
        self.assertEqual((got[slow]['sid'], got[slow]['rule'], got[slow]['call'], got[slow]['dt']), (self.PARENT, 'time', 'toolu_1', 121.0))

    def test_a_sub_agent_record_is_read_and_names_its_node(self):
        agent = 'a' + 'b' * 16
        d = os.path.join(self.proj, self.PARENT, 'subagents')
        os.makedirs(d)
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 50), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        with open(os.path.join(d, 'agent-%s.jsonl' % agent), 'w') as f:
            f.write(bash_line(T0, 'cd /w && claude -p "$Q"') + '\n' + result_line(T0 + 30, 'toolu_1') + '\n')
        with open(os.path.join(d, 'agent-%s.meta.json' % agent), 'w') as f:
            json.dump({'description': 'helper', 'agentType': 'general-purpose', 'toolUseId': 'toolu_x'}, f)
        kid = '55555555-5555-4555-8555-555555555555'
        self.write(kid, child_lines(T0 + 3))
        o = self.scan()[kid]
        self.assertEqual((o['sid'], o['node'], o['rule']), (self.PARENT, agent, 'time'))

    def test_a_window_bigger_than_one_owner_may_hold_is_incomplete_and_never_firm(self):
        """An owner's text window is at most 2 MiB; when it was cut the content comparison is marked incomplete and makes no certain link."""
        words = ('amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow nectar orchard prairie quartz ridge summit tundra umber '
                 'valley willow xenon yarrow zephyr alloy beacon cobalt dune estuary flint garnet hollow iris jasper knoll lantern marble nickel opal pebble').split()
        instr = ' '.join(words[:30]) + ' and report in plain words.'
        filler = [bash_line(T0 - 1000 + i, "cat > /tmp/f%d <<'EOF'\n%s\nEOF" % (i, (' '.join(words) + ' ') * 800), tid='toolu_f%d' % i) for i in range(12)]
        self.write(self.PARENT, filler + [bash_line(T0, 'cd /w && claude -p "$(cat /tmp/p.txt)"', tid='toolu_1'), bash_line(T0 - 2, "cat > /tmp/p.txt <<'EOF'\n%s\nEOF" % instr, tid='toolu_p')])
        kid = '55555555-5555-4555-8555-555555555555'
        self.write(kid, [dump({'type': 'user', 'timestamp': iso(T0 + 3), 'cwd': '/w', 'message': {'role': 'user', 'content': instr}})])
        o = self.scan()[kid]
        self.assertEqual(o['rule'], 'content_short')                                   # demoted to a guess: the comparison left text out
        self.assertFalse(link.certain(o['rule']))
        self.assertIn('fingerprint_incomplete', [d['code'] for d in self.links.diags if d['subject'] == kid])
        self.assertLessEqual(self.links.tcache.size, fp.CACHE_BYTES)

    def test_the_server_instance_returns_after_stage_one_and_finishes_in_the_background(self):
        self.links.deep_inline = False
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "$Q"'), result_line(T0 + 30, 'toolu_1')])
        kid = '55555555-5555-4555-8555-555555555555'
        self.write(kid, child_lines(T0 + 3))
        self.links.scan()
        self.assertTrue(self.links.ready.is_set())
        self.assertEqual(self.links.cli_owners[kid]['sid'], self.PARENT)                 # what stage 1 knows is already published
        self.assertFalse(self.links.deep_ready.is_set())                                 # stage 2 waits until it is asked for (LinkIndex.start_deep)
        self.assertTrue(self.links.start_deep())
        self.assertTrue(self.links.deep_ready.wait(10))
        self.links.scan()
        self.assertEqual(self.links.timing.keys(), {'stage1', 'stage2'})                 # later scans run both stages in one call


class OrphanLaunch(CliFixture):
    """`orphan_launch` counts the launches no child is accounted to: only what can be checked (a loop's known count, a call that has ended or has run a while)."""

    def count(self, now, tree=None):
        with mock.patch('time.time', lambda: now):
            self.scan()
        return sum(d.get('n', 1) for d in self.links.diags if d['code'] == 'orphan_launch' and d['tree'] == (tree or self.PARENT))

    def parent(self, *lines):
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})] + list(lines))

    def test_a_launch_with_its_child_is_not_an_orphan(self):
        self.parent(bash_line(T0, 'cd /w && claude -p hi'), result_line(T0 + 9, 'toolu_1'))
        self.write('55555555-5555-4555-8555-555555555555', child_lines(T0 + 2))
        self.assertEqual(self.count(T0 + 600), 0)

    def test_an_ended_launch_without_a_child_counts_after_the_grace(self):
        self.parent(bash_line(T0, 'cd /w && claude -p hi'), result_line(T0 + 9, 'toolu_1'))
        self.assertEqual(self.count(T0 + 9 + link.ORPHAN_GRACE - 1), 0)             # a child may still be starting
        self.assertEqual(self.count(T0 + 9 + link.ORPHAN_GRACE + 1), 1)

    def test_a_running_call_counts_one_launch_only_when_it_has_no_child_at_all(self):
        self.parent(bash_line(T0, 'cd /w && claude -p hi'))
        self.assertEqual(self.count(T0 + link.ORPHAN_OPEN - 1), 0)
        self.assertEqual(self.count(T0 + link.ORPHAN_OPEN + 1), 1)
        self.write('55555555-5555-4555-8555-555555555555', child_lines(T0 + 2))
        self.assertEqual(self.count(T0 + 600), 0)

    def test_a_sequential_loop_in_progress_is_not_guessed_at(self):
        """Three launches in one running call, one child so far: the other two have simply not started yet."""
        self.parent(bash_line(T0, 'cd /w && for x in a b c; do claude -p "$x"; done'))
        self.write('55555555-5555-4555-8555-555555555555', [dump({'type': 'user', 'timestamp': iso(T0 + 2), 'cwd': '/w', 'message': {'role': 'user', 'content': 'a'}})])
        self.assertEqual(self.count(T0 + 120), 0)

    def test_a_finished_loop_counts_the_launches_that_left_no_child(self):
        self.parent(bash_line(T0, 'cd /w && for x in a b c; do claude -p "$x"; done'), result_line(T0 + 50, 'toolu_1'))
        self.write('55555555-5555-4555-8555-555555555555', [dump({'type': 'user', 'timestamp': iso(T0 + 2), 'cwd': '/w', 'message': {'role': 'user', 'content': 'a'}})])
        self.assertEqual(self.count(T0 + 200), 2)

    def test_no_session_persistence_counts_at_once(self):
        self.parent(bash_line(T0, 'cd /w && claude -p --no-session-persistence "hi"'))
        self.assertEqual(self.count(T0 + 1), 1)

    def test_a_stranger_does_not_account_for_the_launch_it_is_refuted_by(self):
        """A child with other words, started in the window of a launch with a literal argument: the launch is still childless (the old count of started records said no)."""
        self.parent(bash_line(T0, 'cd /w && claude -p "words of the launch"'), result_line(T0 + 9, 'toolu_1'))
        self.write('55555555-5555-4555-8555-555555555555', [dump({'type': 'user', 'timestamp': iso(T0 + 2), 'cwd': '/w', 'message': {'role': 'user', 'content': 'other words'}})])
        self.assertEqual(self.count(T0 + 600), 1)
        self.assertNotIn('55555555-5555-4555-8555-555555555555', self.links.cli_owners)

    def test_launches_that_only_print_or_quote_are_not_counted_and_unread_ones_are(self):
        for i, cmd in enumerate(("echo 'run: claude -p \"x\"'", "python3 - <<'EOF'\nprint('claude -p x')\nEOF", "git commit -m 'claude -p x'")):
            self.parent(bash_line(T0 + i, cmd, tid='toolu_%d' % i), result_line(T0 + i + 1, 'toolu_%d' % i))
        self.assertEqual(self.count(T0 + 600), 0)
        self.parent(bash_line(T0 + 10, "tmux new-session -d -s w 'claude -p \"x\"'", tid='toolu_t'), result_line(T0 + 11, 'toolu_t'))
        self.assertEqual(self.count(T0 + 600), 1)

    def test_a_child_two_sessions_could_have_launched_accounts_for_one_launch_and_both_say_so(self):
        """Two sessions launched the same words; one child: held between them. One of the two launches has no child and the records do not say which."""
        other = '66666666-6666-4666-8666-666666666666'
        for sid in (self.PARENT, other):
            self.write(sid, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}}),
                             bash_line(T0, 'cd /w && claude -p "%s"' % TEXT), result_line(T0 + 9, 'toolu_1')])
        self.write('55555555-5555-4555-8555-555555555555', child_lines(T0 + 2, text=TEXT))
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        got = {d['tree']: d for d in self.links.diags if d['code'] == 'orphan_launch'}
        self.assertEqual(set(got), {self.PARENT, other})
        self.assertTrue(all(d['n'] == 1 and d['ambiguous'] for d in got.values()))
        self.assertNotIn('55555555-5555-4555-8555-555555555555', self.links.cli_owners)             # held, not linked

    def test_a_launch_only_one_child_can_belong_to_is_surely_the_childless_one(self):
        """The same two launches, but the child is in the folder of one of them only: the other launch is the orphan, said without a doubt."""
        other = '66666666-6666-4666-8666-666666666666'
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}}),
                                 bash_line(T0, 'cd /w && claude -p "%s"' % TEXT), result_line(T0 + 9, 'toolu_1')])
        self.write(other, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/z', 'message': {'role': 'user', 'content': 'go'}}),
                           bash_line(T0, 'cd /z && claude -p "%s"' % TEXT, cwd='/z'), result_line(T0 + 9, 'toolu_1')])
        self.write('55555555-5555-4555-8555-555555555555', child_lines(T0 + 2, text=TEXT))
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        got = {d['tree']: d for d in self.links.diags if d['code'] == 'orphan_launch'}
        self.assertEqual(set(got), {other})
        self.assertNotIn('ambiguous', got[other])

    def test_the_diagnostic_names_the_launching_tree_and_node_only(self):
        self.parent(bash_line(T0, 'cd /w && claude -p --no-session-persistence "hi"'))
        with mock.patch('time.time', lambda: T0 + 5):
            self.scan()
        d = next(d for d in self.links.diags if d['code'] == 'orphan_launch')
        self.assertEqual(set(d), {'code', 'subject', 'tree', 'node', 'n'})


class PickedCalls(CliFixture):
    """`cli_owners[child]['calls']`: the call of every run (the evidence's, else the one the launches count to), `calls_certain` which of them the evidence
    named, and `bash_ts` / `bash_desc` / `dt` from the first run's call."""

    def parent(self, *lines):
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})] + list(lines))

    def test_two_calls_with_the_same_words_give_each_child_its_own(self):
        """The second call ran the very command of the first one a little later; the first call had ended only 8 s before the second child started."""
        a, b = '55555555-5555-4555-8555-555555555555', '77777777-7777-4777-8777-777777777777'
        cmd = 'cd /w && claude -p "%s"' % TEXT
        self.parent(bash_line(T0, cmd, tid='toolu_1'), result_line(T0 + 14, 'toolu_1'), bash_line(T0 + 21.2, cmd, tid='toolu_2'), result_line(T0 + 35, 'toolu_2'))
        self.write(a, child_lines(T0 + 1, text=TEXT))
        self.write(b, child_lines(T0 + 22, text=TEXT))
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        oa, ob = self.links.cli_owners[a], self.links.cli_owners[b]
        self.assertEqual((oa['calls'], ob['calls']), (['toolu_1'], ['toolu_2']))
        self.assertEqual((oa['call'], ob['call'], ob['calls_certain']), ('toolu_1', None, [False]))         # `call` is what the evidence names (a tie holds it between two that fit)
        self.assertEqual((ob['bash_ts'], round(ob['dt'], 1)), (T0 + 21.2, 0.8))
        self.assertEqual((oa['bash_ts'], ob['bash_desc']), (T0, 'Launch child'))

    def test_a_resumed_run_has_the_call_that_resumed_it(self):
        kid = '55555555-5555-4555-8555-555555555555'
        self.parent(bash_line(T0, 'cd /w && claude -p "%s"' % TEXT, tid='toolu_1'), result_line(T0 + 9, 'toolu_1'),
                    bash_line(T0 + 3600, 'cd /w && claude -p --resume %s "and now the second part of the work"' % kid, tid='toolu_2'), result_line(T0 + 3609, 'toolu_2'))
        self.write(kid, child_lines(T0 + 2, text=TEXT) + [dump({'type': 'cost-state', 'timestamp': iso(T0 + 8), 'sessionId': kid, 'totalDuration': 6000})]
                   + [dump({'type': 'user', 'timestamp': iso(T0 + 3602), 'cwd': '/w', 'promptSource': 'sdk', 'turnPosition': {'promptIndex': 0, 'turnIndex': 2},
                            'message': {'role': 'user', 'content': 'and now the second part of the work'}})])
        with mock.patch('time.time', lambda: T0 + 7200):
            self.scan()
        o = self.links.cli_owners[kid]
        self.assertEqual((o['calls'], o['call']), (['toolu_1', 'toolu_2'], 'toolu_1'))
        self.assertEqual(o['calls_certain'], [True, True])
        self.assertEqual(len(o['by']), 2)

    def test_a_resumed_slash_command_run_far_into_the_record_is_a_run_start(self):
        """A slash command's line has the turn position and no `promptSource`; it opens a run of unknown words even after the first lines of the record."""
        kid = '55555555-5555-4555-8555-555555555555'
        filler = [dump({'type': 'assistant', 'timestamp': iso(T0 + 3 + i * 0.01), 'cwd': '/w', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'x'}]}})
                  for i in range(80)]
        self.write(kid, child_lines(T0 + 2, text=TEXT) + filler + [dump({'type': 'cost-state', 'timestamp': iso(T0 + 8), 'sessionId': kid, 'totalDuration': 6000}),
                   dump({'type': 'user', 'timestamp': iso(T0 + 3600), 'cwd': '/w', 'turnPosition': {'promptIndex': 0, 'turnIndex': 2},
                         'message': {'role': 'user', 'content': '<command-message>init is running</command-message>\n<command-name>/init</command-name>'}})])
        self.parent()
        with mock.patch('time.time', lambda: T0 + 7200):
            self.scan()
        f = next(f for f in self.links.files.values() if f['sid'] == kid)
        self.assertEqual([(round(r.ts - T0), r.head) for r in f['rs'].runs], [(2, fp.normalize(TEXT)[:fp.HEAD_CHARS]), (3600, '')])

    def test_a_child_with_no_call_in_the_records_has_no_call_and_the_pin_time(self):
        """Linked by its process only: no call to name (calls [None]), `bash_ts` falls back to the process start."""
        kid = '55555555-5555-4555-8555-555555555555'
        self.parent()
        self.write(kid, child_lines(T0 + 2, text=TEXT))
        self.links.lineage.cli[kid] = {'sid': self.PARENT, 'rule': 'proc', 'ts': T0 + 1.5, 'cwd': '/w'}
        with mock.patch('time.time', lambda: T0 + 20):
            self.scan()
        o = self.links.cli_owners[kid]
        self.assertEqual((o['calls'], o['calls_certain'], o['call'], o['bash_ts']), ([None], [False], None, T0 + 1.5))


# ---------------------------------------------------------------------------------------------------------------------
# the link cache
# ---------------------------------------------------------------------------------------------------------------------
class LinkCacheVersion2(unittest.TestCase):
    def test_firm_content_and_out_links_are_kept_as_ids_rules_and_times_only(self):
        c = Sensitivity('test_baseline_links_and_baits_do_not').case(out='json_file', way='bg', seen='live')
        root = tempfile.mkdtemp(prefix='affil-cache-')
        try:
            b = build.build_case(c, os.path.join(root, 'c'))
            cache = os.path.join(b.cache, 'links.json')
            observe.observe(b)                                                  # enable_cache(links.json) is part of the observation
            self.assertTrue(os.path.isfile(cache))
            with open(cache) as fh:
                text = fh.read()
            d = json.loads(text)
            self.assertEqual(d['version'], 4)
            rows = [r for r in d['links'] if r['kind'] == 'cli']
            self.assertTrue(rows)
            for r in rows:
                self.assertLessEqual(set(r), {'child', 'parent', 'kind', 'rule', 'seen', 'started', 'node'})
                self.assertIn(r['rule'], ('proc', 'env', 'out', 'content'))
            # nothing of the instruction, a path or the environment is in the file
            self.assertNotIn(TEXT[:20], text)
            for needle in ('Please review', os.path.join(b.home), 'CLAUDE_PID', 'session_id', '.json"'):
                self.assertNotIn(needle, text, needle)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_rows_of_older_versions_are_still_read_but_not_their_content_links_and_unknown_rules_are_not(self):
        """Version 3 is the first whose `content` links come from a reader that does not count words that only travel as text: an older file's `content` rows are not read."""
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            p = os.path.join(d, 'links.json')
            now = 1790000000.0
            D = KID2
            rows = [{'child': B, 'parent': A, 'kind': 'cli', 'rule': 'env', 'seen': now, 'started': now},
                    {'child': D, 'parent': A, 'kind': 'cli', 'rule': 'out', 'seen': now, 'started': now}]
            extra = [{'child': C, 'parent': A, 'kind': 'cli', 'rule': 'content', 'seen': now, 'started': now, 'node': SUB1},
                     {'child': KID, 'parent': A, 'kind': 'cli', 'rule': 'time', 'seen': now, 'started': now}]
            for version, want in ((1, [B, D]), (2, [B, D]), (3, [B, D]), (4, [B, D, C])):
                with open(p, 'w') as fh:
                    json.dump({'version': version, 'links': rows + extra}, fh)
                os.chmod(p, 0o600)
                info = {}
                got = lineage.read_cache(p, now=now, info=info)
                self.assertEqual([r['child'] for r in got], want, version)
                self.assertEqual(info, {'version': version})
            self.assertEqual(got[2].get('node'), SUB1)
            with open(p, 'w') as fh:
                json.dump({'version': 5, 'links': rows}, fh)
            os.chmod(p, 0o600)
            info = {}
            self.assertEqual(lineage.read_cache(p, now=now, info=info), [])
            self.assertEqual(info, {})                                                   # a version this one does not know is not read at all

    def test_a_remembered_link_survives_a_restart_without_the_records_evidence(self):
        ln = lineage.Lineage()
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            p = os.path.join(d, 'links.json')
            ln.enable_cache(p)
            self.assertTrue(ln.remember(KID, A, SUB1, 'content', T0, now=T0))
            self.assertFalse(ln.remember(KID, A, SUB1, 'content', T0, now=T0 + 5))   # a link seen before is not rewritten
            self.assertFalse(ln.remember(KID, A, None, 'time', T0))                    # a guess is never kept
            again = lineage.Lineage()
            again.enable_cache(p)
            self.assertEqual((again.saved[KID]['tree'], again.saved[KID]['node'], again.saved[KID]['orig']), (A, SUB1, 'content'))
            self.assertNotIn(KID, again.cli)                                           # it is not a pin: it never counts as rank 2 evidence by itself

# ---------------------------------------------------------------------------------------------------------------------
# output generations, launching calls, node held, the time rule's folder, byte limits
# ---------------------------------------------------------------------------------------------------------------------
OUT_PATH = '/o/out.json'


def out_writer(tree, start, op='>', node=None, **kw):
    """A launching call that redirects the launch's output to OUT_PATH (the redirect belongs to the launch, as link.py builds it)."""
    r = Redirect(1, op, OUT_PATH, OUT_PATH, [])
    c = call(tree, node, start, end=start + 5, launches=[launch(redirects=[r], **kw)])
    c.span.redirects.append(r)
    return c, r


class OutputFileGenerations(unittest.TestCase):
    """An output file proves the launch that wrote what it holds now. A call that came later and overwrote it, or took the session up again into it, wrote
    that content; the launch that started the first run is not shown by it."""

    def out(self, *writers):
        return Out({KID: [OUT_PATH]}, {OUT_PATH: list(writers)})

    def test_a_later_call_that_overwrote_the_file_wrote_what_it_holds(self):
        wa, ra = out_writer(A, T0 - 10)
        wc, rc = out_writer(C, T0 + 90, resume=KID)
        owners = [owner(A, None, [wa], text='x'), owner(B, None, [call(B, None, T0, launches=[launch(None)])], text=TEXT), owner(C, None, [wc], text='x')]
        d = affil.decide(child(), owners, out=self.out((wa, ra), (wc, rc)))
        self.assertEqual((d.tree, d.rule, d.certain), (B, 'content', True))              # what the records say about B; A's file proves nothing about run 1

    def test_without_another_explanation_the_old_file_is_no_certain_link(self):
        wa, ra = out_writer(A, T0 - 10)
        wc, rc = out_writer(C, T0 + 90, resume=KID)
        d = affil.decide(child(), [owner(A, None, [wa], text='x'), owner(C, None, [wc], text='x')], out=self.out((wa, ra), (wc, rc)))
        self.assertNotEqual(d.rule, 'out')
        self.assertFalse(d.certain)

    def test_a_later_append_by_a_launch_that_starts_new_sessions_does_not_undo_the_proof(self):
        wa, ra = out_writer(A, T0 - 10)
        wd, rd = out_writer(C, T0 + 90, '>>')
        d = affil.decide(child(), [owner(A, None, [wa], text='x'), owner(C, None, [wd], text='x')], out=self.out((wa, ra), (wd, rd)))
        self.assertEqual((d.tree, d.rule), (A, 'out'))

    def test_a_later_append_that_can_take_a_session_up_again_is_not_the_first_launcher_either(self):
        for kw in ({'resume': KID}, {'session_id': KID}, {'reopens': True}):
            with self.subTest(kw):
                wa, ra = out_writer(A, T0 - 10)
                wd, rd = out_writer(C, T0 + 90, '>>', **kw)
                d = affil.decide(child(), [owner(A, None, [wa], text='x'), owner(C, None, [wd], text='x')], out=self.out((wa, ra), (wd, rd)))
                self.assertNotEqual(d.rule, 'out')

    def test_the_proof_is_not_a_way_around_the_literal_argument_veto(self):
        """The call's literal instruction is another one: it cannot have started this child, whatever the file says."""
        wa, ra = out_writer(A, T0 - 1, arg=OTHER)
        d = affil.decide(child(), [owner(A, None, [wa], text='x')], out=self.out((wa, ra)))
        self.assertIsNone(d.tree)
        wb, rb = out_writer(A, T0 - 1, cwd='/elsewhere')
        self.assertIsNone(affil.decide(child(), [owner(A, None, [wb], text='x')], out=self.out((wb, rb))).tree)
        wc, rc = out_writer(A, T0 - 1, arg=TEXT)
        self.assertEqual(affil.decide(child(), [owner(A, None, [wc], text='x')], out=self.out((wc, rc))).rule, 'out')

    def test_the_run_that_the_later_call_resumed_is_proved_by_that_call(self):
        wa, ra = out_writer(A, T0 - 10)
        wc, rc = out_writer(C, T0 + 90, resume=KID, arg='go on and finish the work')
        c = child(extra_runs=[(T0 + 100, 'go on and finish the work')])
        d = affil.decide(c, [owner(A, None, [wa], text='x'), owner(C, None, [wc], text='x')], out=self.out((wa, ra), (wc, rc)))
        self.assertEqual(d.by[1], (C, None))
        self.assertNotEqual(d.rule, 'out')                                                # run 1's proof is not run 0's


class LaunchingCalls(unittest.TestCase):
    """Rank 3 (content) is for an owner that was running a call able to start a session. A call that only runs something else (a server, a test run, a script
    that was read and starts nothing) shows who wrote the words, not who started the child."""

    def test_the_author_with_a_plain_call_is_not_the_launcher(self):
        author = owner(A, None, [call(A, None, T0 - 30)], text=TEXT)                      # `python3 server.py`: a span, no launch
        launcher = owner(B, None, [call(B, None, T0, launches=[launch(None)])], text='read through $(cat file)')
        d = affil.decide(child(), [author, launcher])
        self.assertEqual((d.tree, d.rule, d.certain), (B, 'time', False))
        self.assertNotIn('content_only', [c for c, _ in d.diags])

    def test_alone_such_a_match_links_nothing_and_says_who_wrote_the_words(self):
        """The words of the instruction are in the record of a session that ran nothing able to start a child: that session wrote them, nobody says it launched."""
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30)], text=TEXT)])
        self.assertEqual((d.tree, d.rule, d.rank, d.kind, d.by, d.held), (None, None, None, None, [None], None))
        self.assertEqual(d.diags, [('content_author_differs', {'other': A})])
        self.assertEqual((d.claims, d.call), ([], None))

    def test_an_author_with_no_call_at_all_links_nothing_either(self):
        d = affil.decide(child(), [owner(A, None, [], text=TEXT)])
        self.assertIsNone(d.tree)

    def test_the_author_alone_is_said_even_when_the_comparison_was_cut(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30)], text=TEXT, complete=False)])
        self.assertEqual((d.tree, d.incomplete), (None, True))
        self.assertEqual(sorted(c for c, _ in d.diags), ['content_author_differs', 'fingerprint_incomplete'])

    def test_a_sub_agent_that_wrote_the_words_and_ran_nothing_links_nothing(self):
        d = affil.decide(child(), [owner(A, None, [], text='nothing'), owner(A, SUB1, [call(A, SUB1, T0 - 30)], text=TEXT)])
        self.assertIsNone(d.tree)
        self.assertEqual(d.diags, [('content_author_differs', {'other': A})])             # the tree, as for every diagnostic of this code

    def test_the_author_is_said_when_the_launcher_is_found_by_another_rule(self):
        author = owner(A, None, [call(A, None, T0 - 30)], text=TEXT)
        launcher = owner(B, None, [call(B, None, T0, launches=[launch(None)])], text='read through $(cat file)')
        d = affil.decide(child(), [author, launcher])
        self.assertEqual((d.tree, d.rule), (B, 'time'))
        self.assertIn(('content_author_differs', {'other': A}), d.diags)

    def test_the_author_of_a_later_run_is_said_too(self):
        first = call(A, None, T0 - 10, end=T0 + 20, launches=[launch(TEXT)])
        plain = call(B, None, T0 + 3590)
        d = affil.decide(child(extra_runs=[(T0 + 3600, OTHER)]), [owner(A, None, [first], text=TEXT), owner(B, None, [plain], text=OTHER)])
        self.assertEqual((d.tree, d.by), (A, [(A, None), None]))
        self.assertEqual(d.diags, [('content_author_differs', {'other': B})])

    def test_an_owner_with_a_launch_among_its_calls_is_the_content_rule_itself(self):
        d = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30), call(A, None, T0 - 20, launches=[launch(None)])], text=TEXT)])
        self.assertEqual((d.tree, d.rule, d.certain), (A, 'content', True))
        self.assertNotIn('content_author_differs', [c for c, _ in d.diags])

    def test_a_call_that_may_launch_keeps_the_content_rule(self):
        for kw in ({'launches': [launch(None)]}, {'launching': True}):                        # a read launch; a call the reader could not place
            with self.subTest(kw):
                d = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30, **kw)], text=TEXT)])
                self.assertEqual((d.tree, d.rule, d.certain), (A, 'content', True))

    def test_a_call_whose_script_nobody_could_read_gives_a_guess_not_a_firm_link(self):
        """`python3 gone.py`: nothing says it starts a session, nothing says it does not. The words in the author's record are then at most a guess (rank 4)."""
        for kw in ({'assumed': ('claude', 'codex')}, {'assumed': ('claude',)}):
            with self.subTest(kw):
                d = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30, **kw)], text=TEXT)])
                self.assertEqual((d.tree, d.rule, d.rank, d.certain), (A, 'content_short', 4, False))
                self.assertEqual((d.assumed, d.incomplete), (True, False))                 # the page can say why: the call is what is not known, the comparison was whole
                self.assertNotIn('fingerprint_incomplete', [c for c, _ in d.diags])
                self.assertNotIn('content_only', [c for c, _ in d.diags])

    def test_the_other_guesses_are_not_marked_as_assumed(self):
        cut = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30, launches=[launch(None)])], text=TEXT, complete=False)])
        self.assertEqual((cut.rule, cut.incomplete, cut.assumed), ('content_short', True, False))
        short = affil.decide(child('/init'), [owner(A, None, [call(A, None, T0, lits=['/init'], launches=[launch('/init')])])])
        self.assertEqual((short.rule, short.assumed), ('content_short', False))
        firm = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30, launches=[launch(None)])], text=TEXT)])
        self.assertEqual((firm.rule, firm.assumed), ('content', False))
        unread_and_cut = affil.decide(child(), [owner(A, None, [call(A, None, T0 - 30, assumed=('claude',))], text=TEXT, complete=False)])
        self.assertEqual((unread_and_cut.rule, unread_and_cut.incomplete, unread_and_cut.assumed), ('content_short', True, True))     # both reasons are true

    def test_a_script_nobody_could_read_is_still_a_launcher_that_counts(self):
        c = call(A, None, T0 - 30, assumed=('claude', 'codex'))
        self.assertTrue(c.starts('claude'))
        self.assertTrue(c.starts('codex'))
        self.assertFalse(c.starts('claude', read=True))
        self.assertTrue(c.launching)

    def test_what_was_read_is_firm(self):
        read = {'a read launch': call(A, None, T0, launches=[launch(None)]), 'a command or script that names it': call(A, None, T0, launching=('claude',)),
                'a tool named, another assumed': call(A, None, T0, launching=('claude',), assumed=('codex',))}
        for label, c in read.items():
            with self.subTest(label):
                self.assertTrue(c.starts('claude', read=True))
        self.assertFalse(call(A, None, T0, launching=('codex',), assumed=('claude',)).starts('claude', read=True))
        self.assertFalse(call(A, None, T0).starts('claude', read=True))

    def test_a_read_launch_among_unread_calls_keeps_the_content_rule(self):
        calls = [call(A, None, T0 - 40, assumed=('claude', 'codex')), call(A, None, T0 - 30, launches=[launch(None)])]
        d = affil.decide(child(), [owner(A, None, calls, text=TEXT)])
        self.assertEqual((d.tree, d.rule, d.certain), (A, 'content', True))

    def test_an_unread_script_of_the_author_is_a_guess_that_still_outranks_the_time_rule(self):
        """The words come from a session that ran a script nobody could read (it may well have launched them); another session ran a read launch that took its
        instruction from a file. Rank 4 before rank 5, as a guess either way."""
        author = owner(A, None, [call(A, None, T0 - 30, assumed=('claude', 'codex'))], text=TEXT)
        launcher = owner(B, None, [call(B, None, T0, launches=[launch(None)])], text='read through $(cat file)')
        d = affil.decide(child(), [author, launcher])
        self.assertEqual((d.tree, d.rule, d.rank, d.certain), (A, 'content_short', 4, False))

    def test_the_default_of_a_call_is_to_launch_when_it_has_launches(self):
        self.assertTrue(call(A, None, T0, launches=[launch(None)]).launching)
        self.assertFalse(call(A, None, T0).launching)
        self.assertTrue(call(A, None, T0, launching=True).launching)


class NodeHeld(unittest.TestCase):
    """The words sit in the record of the main session and a sub-agent of the same session was running the launch that reads them from a file: the text shows
    who wrote the words, so the tree is sure and the node is not."""

    def two_nodes(self, main_launch, sub_launch):
        main = owner(A, None, [call(A, None, T0 - 100, launches=[main_launch])], text=TEXT)
        sub = owner(A, SUB1, [call(A, SUB1, T0, launches=[sub_launch])], text='nothing of it')
        return [main, sub]

    def test_indirect_launches_of_both_nodes_hold_the_node(self):
        d = affil.decide(child(), self.two_nodes(launch(None), launch(None)))
        self.assertEqual((d.tree, d.rule, d.certain), (A, 'content', True))
        self.assertIsNone(d.node)
        self.assertFalse(d.relation.certain['node'])
        self.assertIsNone(d.call)
        self.assertIn('node_unresolved', [c for c, _ in d.diags])

    def test_a_literal_argument_that_is_the_instruction_is_not_indirect(self):
        d = affil.decide(child(), self.two_nodes(launch(TEXT), launch(None)))
        self.assertEqual((d.tree, d.node, d.relation.certain['node']), (A, None, True))
        self.assertNotIn('node_unresolved', [c for c, _ in d.diags])

    def test_a_sibling_launch_that_cannot_fit_the_child_does_not_hold_it(self):
        for other in (launch(OTHER), launch(None, cwd='/elsewhere'), launch(None, persist=False)):
            d = affil.decide(child(), self.two_nodes(launch(None), other))
            self.assertEqual((d.tree, d.node, d.relation.certain['node']), (A, None, True))

    def test_a_sibling_without_a_running_launch_does_not_hold_it(self):
        main = owner(A, None, [call(A, None, T0 - 100, launches=[launch(None)])], text=TEXT)
        sub = owner(A, SUB1, [], text='nothing of it')
        d = affil.decide(child(), [main, sub])
        self.assertEqual((d.tree, d.node), (A, None))
        self.assertNotIn('node_unresolved', [c for c, _ in d.diags] if sub.alive_at(T0) is False else [])

    def test_the_sub_agent_that_wrote_the_words_and_launched_them_literally_keeps_its_node(self):
        main = owner(A, None, [call(A, None, T0 - 100, launches=[launch(None)])], text='nothing of it')
        sub = owner(A, SUB1, [call(A, SUB1, T0, launches=[launch(TEXT)])], text=TEXT)
        d = affil.decide(child(), [main, sub])
        self.assertEqual((d.tree, d.node, d.relation.certain['node']), (A, SUB1, True))


class TimeRuleFolder(unittest.TestCase):
    """Rank 5 (time) is a guess about one launch in the child's folder: a launch whose folder could not be worked out (`cd "$WT" && claude -p ...`) fits a
    child only when the child runs inside the folder the session itself was in."""

    def unknown_folder(self, session_cwd='/w'):
        c = call(A, None, T0, launches=[launch(None, cwd=None)])
        c.span.cwd = session_cwd
        return owner(A, None, [c], text='nothing of it')

    def test_a_child_in_another_project_is_not_taken(self):
        self.assertIsNone(affil.decide(child(cwd='/other'), [self.unknown_folder()]).tree)
        self.assertIsNone(affil.decide(child(cwd='/wx'), [self.unknown_folder()]).tree)                     # a longer name is not "inside"

    def test_a_child_in_or_under_the_sessions_folder_is_a_guess(self):
        for cwd in ('/w', '/w/sub/deeper'):
            d = affil.decide(child(cwd=cwd), [self.unknown_folder()])
            self.assertEqual((d.tree, d.rule, d.certain), (A, 'time', False), cwd)

    def test_nothing_known_about_either_folder_is_no_guess(self):
        o = self.unknown_folder(session_cwd=None)
        self.assertIsNone(affil.decide(child(cwd='/w'), [o]).tree)

    def test_a_known_launch_folder_is_compared_as_before(self):
        o = owner(A, None, [call(A, None, T0, launches=[launch(None, cwd='/w')])], text='nothing of it')
        self.assertEqual(affil.decide(child(cwd='/w'), [o]).rule, 'time')
        self.assertIsNone(affil.decide(child(cwd='/w/sub'), [o]).tree)


class ByteLimits(unittest.TestCase):
    """The limits count what the texts hold in memory: a 16 KiB head of emoji is 64 KiB of characters' worth of bytes."""

    def test_the_head_of_an_instruction_is_cut_by_bytes_held(self):
        for ch in ('a ', '한 ', '\U0001f600 ', 'é '):
            i = affil.Instr(T0, 1, ch * 30000)
            self.assertTrue(i.truncated, ch)
            self.assertLessEqual(fp.nbytes(i.head), fp.HEAD_BYTES, ch)
            self.assertGreater(fp.nbytes(i.head), fp.HEAD_BYTES // 2, ch)

    def test_a_short_instruction_of_wide_characters_is_not_cut(self):
        i = affil.Instr(T0, 1, '지시문 한 줄')
        self.assertFalse(i.truncated)
        self.assertEqual(i.head, '지시문 한 줄')

    def test_an_argument_cut_at_the_cap_is_a_prefix_not_a_different_text(self):
        long_text = fp.normalize('한 ' * 30000)
        i = affil.Instr(T0, 1, long_text)
        arg = fp.clip(long_text, fp.HEAD_BYTES)
        self.assertTrue(i.same_as(arg))
        self.assertFalse(i.same_as(fp.normalize('another instruction')))


# ---------------------------------------------------------------------------------------------------------------------
# records that are broken, unreadable or huge; the later stage starts on request; the lock
# ---------------------------------------------------------------------------------------------------------------------
def tool_line(t, name, inp, tid, cwd='/w'):
    return dump({'type': 'assistant', 'timestamp': iso(t), 'cwd': cwd,
                 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}})


KID2 = '55555555-5555-4555-8555-555555555555'
KID3 = '66666666-6666-4666-8666-666666666666'


class BrokenRecords(CliFixture):
    def parent(self, *lines):
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 900), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})] + list(lines))

    def test_line_texts_ignores_the_shapes_it_does_not_know(self):
        base = {'type': 'assistant', 'timestamp': iso(T0)}
        for blocks in ([{'type': 'tool_use', 'name': 'Write', 'input': 'not-a-dict'}], [{'type': 'tool_use', 'name': 'Bash', 'input': ['x']}],
                       [{'type': 'tool_use', 'name': 'MultiEdit', 'input': {'edits': 'nope'}}], [{'type': 'tool_use', 'name': 'MultiEdit', 'input': {'edits': [3, None]}}],
                       [{'type': 'tool_use', 'name': 'Edit', 'input': {'new_string': 5}}], 'not-a-list', None):
            self.assertEqual(link.line_texts(json.dumps(dict(base, message={'role': 'assistant', 'content': blocks})).encode()), [], blocks)
        self.assertEqual(link.line_texts(json.dumps(dict(base, message='a string')).encode()), [])
        self.assertEqual(link.line_texts(b'[1, 2]'), [])
        ok = {'type': 'tool_use', 'name': 'Write', 'input': {'file_path': '/x', 'content': 'a  b'}}
        self.assertEqual(link.line_texts(json.dumps(dict(base, message={'role': 'assistant', 'content': ['junk', ok]})).encode()), ['a b'])

    def test_a_broken_write_line_does_not_stop_the_later_stage(self):
        """A `Write` whose input is not an object, inside the window the fingerprint reads: the pass used to die on it and never finish."""
        self.parent(tool_line(T0 - 100, 'Write', 'not-a-dict', 'tw1'), tool_line(T0 - 99, 'Edit', ['x'], 'tw2'), bash_line(T0, 'cd /w && claude -p "%s"' % TEXT, tid='toolu_1'),
                    result_line(T0 + 60, 'toolu_1'))
        self.write(KID, child_lines(T0 + 2, text=TEXT))
        self.links.deep_inline = False
        self.links.scan()
        errors = []
        with mock.patch('threading.excepthook', lambda a: errors.append(a.exc_type.__name__)):
            self.assertTrue(self.links.start_deep())
            self.assertTrue(self.links.deep_ready.wait(10))
        self.assertEqual(errors, [])
        self.assertEqual(self.links.cli_owners[KID]['sid'], self.PARENT)

    def test_a_child_whose_judgment_fails_is_skipped_and_said(self):
        self.parent(bash_line(T0, 'cd /w && claude -p "%s"' % TEXT, tid='toolu_1'), result_line(T0 + 60, 'toolu_1'),
                    bash_line(T0 + 100, 'cd /w && claude -p "%s"' % OTHER, tid='toolu_2'), result_line(T0 + 160, 'toolu_2'))
        self.write(KID, child_lines(T0 + 2, text=TEXT))
        self.write(KID2, child_lines(T0 + 102, text=OTHER))
        real = affil.decide

        def decide(cf, *a, **kw):
            if cf.sid == KID:
                raise RuntimeError('boom')
            return real(cf, *a, **kw)
        out = io.StringIO()
        with mock.patch.object(affil, 'decide', decide), contextlib.redirect_stdout(out):
            self.scan()
        self.assertNotIn(KID, self.links.cli_owners)
        self.assertEqual(self.links.cli_owners[KID2]['sid'], self.PARENT)                    # the other child is judged as usual
        self.assertIn(KID, out.getvalue())                                                   # the log names the session, never a line or its text
        self.assertNotIn('boom', out.getvalue())
        self.assertIn(('parse_errors', KID), [(d['code'], d['subject']) for d in self.links.diags])

    @unittest.skipIf(os.geteuid() == 0, 'root can read any file')
    def test_a_record_that_cannot_be_opened_is_skipped(self):
        self.parent(bash_line(T0, 'cd /w && claude -p "%s"' % TEXT, tid='toolu_1'), result_line(T0 + 60, 'toolu_1'))
        self.write(KID, child_lines(T0 + 2, text=TEXT))
        locked = self.write(KID3, [dump({'type': 'user', 'timestamp': iso(T0), 'cwd': '/z', 'message': {'role': 'user', 'content': 'x'}})])
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o600)
        self.scan()
        self.assertEqual(self.links.cli_owners[KID]['sid'], self.PARENT)
        self.scan()
        mine = [d for d in self.links.diags if d['code'] == 'parse_errors' and d['subject'] == KID3]
        self.assertEqual(len(mine), 1)                                                       # said once per scan, not more
        os.chmod(locked, 0o600)
        self.scan()
        self.assertEqual([d for d in self.links.diags if d['code'] == 'parse_errors'], [])   # readable again: nothing to say

    def test_a_line_without_an_end_is_dropped_once_and_not_read_again(self):
        with mock.patch.object(link, 'LINE_MAX', 1 << 20), mock.patch.object(link, 'CHUNK', 256 << 10):
            p = self.write(KID3, [dump({'type': 'user', 'timestamp': iso(T0 - 50), 'cwd': '/z', 'message': {'role': 'user', 'content': 'x'}})])
            with open(p, 'ab') as fh:
                fh.write(b'{"type":"user","message":{"content":"' + b'x' * (3 << 20))                       # 3 MiB and no newline
            self.links.scan()
            f = next(f for f in self.links.files.values() if f['sid'] == KID3)
            self.assertEqual(f['pos'], os.path.getsize(p))                                      # the position moved past the long line
            with open(p, 'ab') as fh:
                fh.write(b'y' * 1000)                                                           # it goes on: still the same line
            self.links.scan()
            self.assertEqual(f['pos'], os.path.getsize(p))
            with open(p, 'ab') as fh:
                fh.write(b'\n' + bash_line(T0, 'cd /z && claude -p hi', cwd='/z', tid='toolu_9').encode() + b'\n')
            self.links.scan()
            self.assertIn('toolu_9', f['spans'])                                                # the line after it is read
            self.assertEqual(f['pos'], os.path.getsize(p))


class RunAfterACutTool(CliFixture):
    """A `claude -p` process killed while a tool ran leaves a tool call with no result; the `--resume` that follows is a new run, found from the lines right before
    its instruction (the assistant line that asked for the tool), without reading every line of the child's record."""

    def sdk(self, t, text, idx):
        return dump({'type': 'user', 'timestamp': iso(t), 'cwd': '/w', 'promptSource': 'sdk', 'turnPosition': {'promptIndex': 0, 'turnIndex': idx},
                     'message': {'role': 'user', 'content': text}})

    def asked_for_a_tool(self, t, tid='toolu_k'):
        return dump({'type': 'assistant', 'timestamp': iso(t), 'cwd': '/w', 'message': {'role': 'assistant', 'stop_reason': 'tool_use',
                     'content': [{'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': 'sleep 600'}}]}})

    def runs(self, lines):
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}}),
                                 bash_line(T0, 'cd /w && claude -p "%s"' % TEXT, tid='toolu_1')])
        self.write(KID, lines)
        self.scan()
        f = next(f for f in self.links.files.values() if f['sid'] == KID)
        return f['rs'].runs

    def test_a_resume_after_a_tool_that_never_returned_is_a_new_run(self):
        runs = self.runs([self.sdk(T0 + 2, TEXT, 1), self.asked_for_a_tool(T0 + 5), self.sdk(T0 + 900, 'carry on with the review please', 2)])
        self.assertEqual([r.head for r in runs], [fp.normalize(TEXT), 'carry on with the review please'])

    def test_the_lines_before_it_are_found_across_a_chunk_boundary(self):
        lines = [self.sdk(T0 + 2, TEXT, 1), self.asked_for_a_tool(T0 + 5), self.sdk(T0 + 900, 'carry on with the review please', 2)]
        with mock.patch.object(link, 'CHUNK', len(lines[0]) + len(lines[1]) + 2):                 # the second chunk begins with the resume line
            runs = self.runs(lines)
        self.assertEqual(len(runs), 2)

    def test_a_clean_end_before_the_next_prompt_is_not_a_new_run(self):
        done = dump({'type': 'assistant', 'timestamp': iso(T0 + 5), 'cwd': '/w', 'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}]}})
        runs = self.runs([self.sdk(T0 + 2, TEXT, 1), done, self.sdk(T0 + 900, 'one more question', 2)])
        self.assertEqual(len(runs), 1)                                                         # a process takes its next prompt after it has ended the turn

    def test_lines_that_say_nothing_are_not_read(self):
        """Only the few lines before an instruction are decoded: a record full of tool calls is not."""
        filler = [self.asked_for_a_tool(T0 + 3 + i / 100.0, 'toolu_f%d' % i) for i in range(300)]
        calls = []
        real = affil.RunStarts.feed_context
        with mock.patch.object(affil.RunStarts, 'feed_context', lambda self_, raw: (calls.append(1), real(self_, raw))[1]):
            self.runs([self.sdk(T0 + 2, TEXT, 1)] + filler + [self.sdk(T0 + 900, 'carry on with the review please', 2)])
        self.assertLessEqual(len(calls), 6)


class LaterStage(CliFixture):
    def setUp(self):
        super().setUp()
        self.links.deep_inline = False
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p "%s"' % TEXT, tid='toolu_1'), result_line(T0 + 60, 'toolu_1')])
        self.write(KID, child_lines(T0 + 2, text=TEXT))

    def threads(self):
        return [t for t in threading.enumerate() if t.name == 'link-deep']

    def test_scan_never_starts_the_later_stage_on_its_own(self):
        self.links.scan()
        self.links.scan()
        time.sleep(0.2)
        self.assertEqual((self.threads(), self.links.deep_ready.is_set(), self.links.ready.is_set()), ([], False, True))
        self.assertEqual(self.links.cli_owners[KID]['rule'], 'time')                           # what the first stage knows is shown meanwhile

    def test_start_deep_starts_it_once(self):
        self.links.scan()
        gate = threading.Event()
        real = self.links.scan_deep
        started = []

        def slow():
            started.append(1)
            gate.wait(10)
            real()
        with mock.patch.object(self.links, 'scan_deep', slow):
            self.assertTrue(self.links.start_deep())
            self.assertFalse(self.links.start_deep())                                          # asked again while it runs
            self.assertFalse(self.links.start_deep())
            gate.set()
            self.assertTrue(self.links.deep_ready.wait(10))
        self.assertEqual(len(started), 1)
        self.assertFalse(self.links.start_deep())                                              # asked again when it is done
        self.assertEqual(self.links.cli_owners[KID]['rule'], 'content')

    def test_start_deep_does_nothing_for_an_index_that_runs_it_inline(self):
        self.links.deep_inline = True
        self.assertFalse(self.links.start_deep())
        self.assertEqual(self.threads(), [])

    def test_children_that_start_before_the_later_stage_are_still_linked(self):
        self.links.scan()
        self.write(self.PARENT, [bash_line(T0 + 200, 'cd /w && claude -p "%s"' % OTHER, tid='toolu_2'), result_line(T0 + 230, 'toolu_2')])
        self.write(KID2, child_lines(T0 + 202, text=OTHER))
        self.links.scan()
        self.assertEqual(self.links.cli_owners[KID2]['sid'], self.PARENT)

    def test_a_failing_later_stage_is_logged_and_the_scans_go_on(self):
        self.links.scan()
        out = io.StringIO()
        with mock.patch.object(self.links, '_stage2', side_effect=RuntimeError('boom')), contextlib.redirect_stdout(out):
            self.assertTrue(self.links.start_deep())
            self.links._deep_thread.join(10)
            self.links.scan()
        self.assertTrue(self.links.deep_ready.is_set())                                       # later scans run the stage themselves (and log when it fails again)
        self.assertIn('RuntimeError', out.getvalue())
        self.assertNotIn('boom', out.getvalue())

    def test_the_judgment_does_not_hold_the_lock_that_requests_wait_for(self):
        """cli_owned_by / owned_by take LINKS.lock: the final judgment must not keep it while it works."""
        self.links.deep_inline = True
        seen = []
        real = affil.decide

        def decide(*a, **kw):
            got = []
            t = threading.Thread(target=lambda: (got.append(self.links.lock.acquire(timeout=2)), self.links.lock.release() if got[0] else None))
            t.start()
            t.join(5)
            seen.append(got[0] if got else None)
            return real(*a, **kw)
        with mock.patch.object(affil, 'decide', decide):
            self.links.scan()
        self.assertTrue(seen)
        self.assertTrue(all(x is True for x in seen), seen)


class LaunchKinds(unittest.TestCase):
    """A call that starts Codex cannot have started a `claude -p` child (and the other way round): the "running call" that rank 3 and rank 4 need is one that
    can start a session of the child's kind, and a call that its own launch refutes is not one."""
    PREFIX = 'Summarize the open TODOs in docs/'
    SECOND = PREFIX + ' (second try)'

    def owner_with(self, *calls, text=None):
        return owner(A, None, list(calls), text=text if text is not None else self.PREFIX)

    def test_open_codex_calls_do_not_make_the_owner_a_claude_launcher(self):
        """The scene that made a false certain link: the instruction shares its first 32 characters with a `claude -p` call that its words refute, and the session has
        `codex exec` calls whose end was never seen."""
        refuted = call(A, None, T0 - 8, end=T0 - 7, launches=[launch(self.PREFIX)])
        codex = call(A, None, T0 - 900, launching=('codex',))
        d = affil.decide(child(self.SECOND), [self.owner_with(codex, refuted)])
        self.assertFalse(d.certain)
        self.assertNotEqual(d.rule, 'content')
        # control: an open call that can start claude makes the same text a certain content link again
        d = affil.decide(child(self.SECOND), [self.owner_with(call(A, None, T0 - 900, launching=('claude',)), refuted)])
        self.assertEqual((d.tree, d.rule, d.certain), (A, 'content', True))

    def test_the_kind_of_a_call(self):
        claude, codex, both, none = (call(A, None, T0, launching=x) for x in (('claude',), ('codex',), True, False))
        self.assertEqual([c.starts('claude') for c in (claude, codex, both, none)], [True, False, True, False])
        self.assertEqual([c.starts('codex') for c in (claude, codex, both, none)], [False, True, True, False])
        self.assertTrue(call(A, None, T0, launches=[launch(None)]).starts('claude'))                  # a `claude -p` the reader found
        self.assertFalse(call(A, None, T0, launches=[launch(None)]).starts('codex'))
        probed = affil.Call(Span(A, None, 't', T0, None), [], frozenset(), '', None, frozenset(), None, lambda: (('codex',), ()))
        self.assertEqual((probed.starts('claude'), probed.starts('codex')), (False, True))
        guessed = affil.Call(Span(A, None, 't', T0, None), [], frozenset(), '', None, frozenset(), None, lambda: ((), ('claude', 'codex')))
        self.assertEqual([guessed.starts(t) for t in ('claude', 'codex')], [True, True])
        self.assertEqual([guessed.starts(t, read=True) for t in ('claude', 'codex')], [False, False])

    def test_a_short_literal_of_a_codex_call_is_not_the_parent_of_a_claude_child(self):
        c = child('go', t0=T0 + 2)
        codex = owner(A, None, [call(A, None, T0, lits=['go'], launching=('codex',))], text='x')
        self.assertIsNone(affil.decide(c, [codex]).tree)
        claude = owner(A, None, [call(A, None, T0, lits=['go'], launching=('claude',))], text='x')
        self.assertEqual((affil.decide(c, [claude]).tree, affil.decide(c, [claude]).rule), (A, 'content_short'))


class LaunchKindsInRecords(CliFixture):
    """The same, read from records: what `launch_tools` says about a command, and the old synthetic scene read by LinkIndex."""

    def tools(self, cmd, cwd='/w'):
        return link.launch_tools(cmd, cwd)

    def assumed(self, cmd, cwd='/w'):
        return link.launch_kinds(cmd, cwd)[1]

    def test_the_tools_a_command_can_start(self):
        self.assertEqual(self.tools('claude -p "x"'), {'claude'})
        self.assertEqual(self.tools('cd /w && codex exec resume 019a7d22-5d3f-7b42-9a26-3c7dae1f2b02 "x" &'), {'codex'})
        self.assertEqual(self.tools("tmux new-session -d 'claude -p x' && codex exec y"), {'claude', 'codex'})
        self.assertEqual(self.tools('bash /nowhere/run.sh'), {'claude', 'codex'})                  # a script nobody can read may start either
        self.assertEqual(self.assumed('bash /nowhere/run.sh'), {'claude', 'codex'})          # ... which is only assumed
        self.assertEqual(self.assumed('claude -p x; bash /nowhere/run.sh'), {'codex'})        # a tool the command names is not assumed
        self.assertEqual(self.assumed('cd /w && codex exec "x" &'), set())
        self.assertEqual(self.assumed('python3 $DIR/run.py'), {'claude', 'codex'})
        self.assertEqual(self.assumed('python3 -m unittest tests/test_big.py'), set())
        self.assertEqual(self.tools('python3 -m unittest tests/test_big.py'), set())
        self.assertEqual(self.tools("echo 'codex exec x'"), set())                                  # printing is not starting

    def test_a_script_is_read_for_the_tool_it_names(self):
        for text, want in (('codex exec "go"\n', {'codex'}), ('claude -p go\n', {'claude'}), ('make all\n', set()), ('claude -p a; codex exec b\n', {'claude', 'codex'})):
            path = os.path.join(os.path.realpath(self.tmp.name), 'run.sh')
            with open(path, 'w') as fh:
                fh.write(text)
            link._SCRIPTS.clear()
            self.assertEqual(self.tools('bash %s' % path), want, text)

    def test_open_codex_calls_of_the_session_do_not_link_a_claude_child_for_sure(self):
        """The old synthetic scene: two `codex exec resume … &` calls that were never seen to end, `claude -p "Summarize …"`, and a child that is that plus a few words."""
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 1000), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}}),
                                 bash_line(T0 - 900, 'cd /w && codex exec resume 019a7d22-5d3f-7b42-9a26-3c7dae1f2b02 "Second pass on the failing combinations" &', tid='toolu_c1'),
                                 bash_line(T0 - 145, 'cd /w && codex exec resume 019a7d22-5d3f-7b42-9a26-3c7dae1f2b03 "Second pass on the other combinations" &', tid='toolu_c2'),
                                 bash_line(T0 - 8, 'cd /w && claude -p "Summarize the open TODOs in docs/"', tid='toolu_p'), result_line(T0 - 7, 'toolu_p')])
        self.write(KID, child_lines(T0 + 2, text='Summarize the open TODOs in docs/ (second try)'))
        with mock.patch('time.time', lambda: T0 + 60):
            self.scan()
        self.assertIsNone(self.links.cli_owners.get(KID))                                                   # no claude launch ran: the words alone link nothing
        self.assertIn(('content_author_differs', self.PARENT), [(d['code'], d.get('other')) for d in self.links.diags if d['subject'] == KID])

    def test_a_codex_thread_is_not_linked_by_a_call_that_only_starts_claude(self):
        c = link.cx_parse_call(T0, 'toolu_1', {'command': 'claude -p "fix the flaky retry test in the payments module and report back"'}, '/w')
        self.assertIsNone(c)                                                                          # not a call the Codex rules look at
        stranger = {'id': '019a0001-0000-7000-8000-00000000000a', 'origin': 'exec', 'guardian': False, 'meta_ts': T0 + 2, 'first_user': 'x', 'cwd': '/w'}
        self.assertEqual(link.cx_link([], [stranger]), {})


class WordsThatOnlyTravel(CliFixture):
    """WP0: words that only travel as text (typed into a terminal, printed) are no launch of the call that carries them. That call is nobody's launcher, not even
    as a guess, and it is no rival of the session that really ran `claude -p`."""
    RELAY = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    LAUNCHER = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'

    SHAPES = {
        'send-keys': "tmux send-keys -t w -l '%s' && tmux send-keys -t w Enter",
        'send-keys, chained': "cd /w && tmux send-keys -t w:0.1 -l '%s'; sleep 1; tmux send-keys -t w:0.1 Enter",
        'send, remote': "ssh box \"tmux send-keys -t w '%s' Enter\"",
        'python -c': "python3 -c \"import subprocess; subprocess.run(['tmux', 'send-keys', '-t', 'w', '-l', '%s'])\"",
        'echo': "echo 'Run: %s'",
        'printf': "printf '%%s\\n' \"%s\"",
        'screen': "screen -S w -X stuff '%s'",
        'paste-buffer': "tmux set-buffer -b x '%s' && tmux paste-buffer -b x -t w",
    }

    def relay(self, shape, sid=None):
        cmd = self.SHAPES[shape] % ('claude -p "%s"' % TEXT)
        self.write(sid or self.RELAY, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}}),
                                       bash_line(T0, cmd, tid='toolu_relay'), result_line(T0 + 0.5, 'toolu_relay')])
        return cmd

    def launcher(self):
        self.write(self.LAUNCHER, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'run it'}}),
                                   bash_line(T0 + 1, 'cd /w && claude -p --model m "%s"' % TEXT, tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])

    def run_scan(self):
        self.write(KID, child_lines(T0 + 2, text=TEXT))
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        return self.links.cli_owners.get(KID)

    def test_a_relay_is_nobodys_launcher(self):
        for shape in self.SHAPES:
            with self.subTest(shape):
                self.setUp()
                self.relay(shape)
                self.assertIsNone(self.run_scan(), shape)
                self.assertEqual([c for c in self.links.decisions[KID].claims], [], shape)           # not even a call the child could be accounted to

    def test_a_relay_and_the_real_launcher_are_no_tie(self):
        for shape in self.SHAPES:
            with self.subTest(shape):
                self.setUp()
                self.relay(shape)
                self.launcher()
                o = self.run_scan()
                self.assertEqual((o['sid'], o['rule'], o['certain']), (self.LAUNCHER, 'content', True), shape)

    def test_a_short_literal_is_not_linked_to_a_relay_either(self):
        self.write(self.RELAY, [bash_line(T0, "tmux send-keys -t w -l 'claude -p \"/init\"' && tmux send-keys -t w Enter", tid='toolu_relay'), result_line(T0 + 0.5, 'toolu_relay')])
        self.write(KID, child_lines(T0 + 2, text='/init'))
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        self.assertIsNone(self.links.cli_owners.get(KID))

    def test_the_commands_that_run_what_they_are_given_still_link(self):
        for cmd in ('tmux new-session -d -s w \'claude -p "%s"\'', 'tmux -L sock new-window -d \'cd /w && claude -p "%s"\'', 'ssh box \'claude -p "%s"\'',
                    'printf "%%s\\n" go | xargs -I{} claude -p "%s"', 'bash -c \'claude -p "%s"\'', 'docker exec c claude -p "%s"', 'env -i PATH=$PATH claude -p "%s"'):
            with self.subTest(cmd):
                self.setUp()
                self.write(self.RELAY, [bash_line(T0, cmd % TEXT, tid='toolu_t'), result_line(T0 + 5, 'toolu_t')])
                o = self.run_scan()
                self.assertIsNotNone(o, cmd)
                self.assertEqual((o['sid'], o['certain']), (self.RELAY, True), cmd)

    def test_code_given_to_python_may_start_a_session_and_is_a_guess_at_most(self):
        self.write(self.RELAY, [bash_line(T0, 'python3 -c "import subprocess; subprocess.run([\'claude\', \'-p\', \'%s\'])"' % TEXT, tid='toolu_py'), result_line(T0 + 5, 'toolu_py')])
        o = self.run_scan()
        self.assertIsNotNone(o)
        self.assertEqual((o['sid'], o['certain']), (self.RELAY, False))                                       # assumed: at most the guess of rank 4
        self.assertEqual(link.launch_kinds('python3 -c "subprocess.run([\'claude\', \'-p\'])"', '/w'), (frozenset(), frozenset({'claude'})))


    def cache(self, version, rule='content'):
        d = os.path.join(os.path.realpath(self.tmp.name), 'cache')
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        path = os.path.join(d, 'links.json')
        with open(path, 'w') as fh:
            json.dump({'version': version, 'links': [{'child': KID, 'parent': self.RELAY, 'kind': 'cli', 'rule': rule, 'seen': time.time() - 100, 'started': T0 + 2}]}, fh)
        os.chmod(path, 0o600)
        self.links.lineage.enable_cache(path)

    def test_a_content_link_a_relay_got_in_an_older_file_does_not_come_back(self):
        self.write(self.RELAY, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}})])
        self.cache(2)
        self.assertIsNone(self.run_scan())                                                       # nothing in the records says it: the old link is gone, not a fallback
        self.assertNotIn(KID, self.links.lineage.saved)

    def test_the_same_link_in_a_current_file_is_a_fallback(self):
        self.write(self.RELAY, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}})])
        self.cache(lineage.CACHE_VERSION)
        o = self.run_scan()
        self.assertEqual((o['sid'], o['rule']), (self.RELAY, 'cache'))

    def test_the_content_link_of_a_version_3_file_does_not_come_back_either(self):
        """Version 4: the plain arguments of an ordinary program and the words a script only prints no longer count as a launch, so what a version 3 file remembers of the kind is read again."""
        self.write(self.RELAY, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}})])
        self.cache(3)
        self.assertIsNone(self.run_scan())
        self.assertNotIn(KID, self.links.lineage.saved)

    def test_the_other_rules_of_an_older_file_are_kept(self):
        self.write(self.RELAY, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}})])
        self.cache(2, rule='out')
        o = self.run_scan()
        self.assertEqual((o['sid'], o['rule']), (self.RELAY, 'cache'))


class PlainArgumentsAreNoLaunch(CliFixture):
    """The words `claude -p` among the plain arguments of an ordinary program (`curl --data claude -p --data "<instruction>"`) are text the program is given: no launch, no link,
    no rival of the session that really ran it. Only a command position counts, and the command line a program that runs its arguments is given (read once, as one command)."""
    RELAY = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    LAUNCHER = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
    SHAPES = (
        'curl --data claude -p --data "%s" https://example.invalid/hook',
        'curl -d claude -d -p -d "%s" https://example.invalid/hook',
        'mytool --notify --tool claude -p --text "%s"',
        'wget --post-data="%s" https://example.invalid/x claude -p',
        'ls claude -p "%s"',
        'FOO=1 mytool claude -p "%s"',
        'flock /tmp/l.lock mytool claude -p "%s"',
        # the wrappers' own arguments: the command line they run is read once, as the command it is
        "tmux list-panes -F '#{pane_id}' | xargs -I{} tmux send-keys -t {} 'claude -p \"%s\"' Enter",
        "ssh localhost tmux send-keys -t w \"'claude -p \\\"%s\\\"'\" Enter",
        "ssh box echo 'claude -p \"%s\"'",
        "kubectl exec pod -- tmux send-keys -t w 'claude -p \"%s\"' Enter",
        "docker exec box tmux send-keys -t w 'claude -p \"%s\"' Enter",
        "watch -n 60 tmux send-keys -t w 'claude -p \"%s\"' Enter",
        "xargs -n1 echo 'claude -p \"%s\"' < list.txt",
        "tmux new-session -d -s w \\; send-keys -t w 'claude -p \"%s\"' Enter",
        "find . -name '*.md' | xargs grep -n 'claude -p \"%s\"'",
    )

    def relay(self, shape):
        self.write(self.RELAY, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}}),
                                bash_line(T0, shape % TEXT, tid='toolu_relay'), result_line(T0 + 0.5, 'toolu_relay')])

    def test_the_words_are_no_launch_for_any_of_the_readers(self):
        for shape in self.SHAPES:
            cmd = shape % TEXT
            with self.subTest(shape):
                self.assertEqual(link.launch_kinds(cmd, '/w'), (frozenset(), frozenset()), shape)
                self.assertFalse(link._weak_launch(cmd), shape)
                self.assertFalse(link._launchy_call(cmd), shape)

    def test_such_a_call_links_nothing_and_is_no_rival(self):
        for shape in self.SHAPES:
            with self.subTest(shape):
                self.setUp()
                self.relay(shape)
                self.write(KID, child_lines(T0 + 2, text=TEXT))
                with mock.patch('time.time', lambda: T0 + 600):
                    self.scan()
                self.assertIsNone(self.links.cli_owners.get(KID), shape)
                self.assertEqual(self.links.lineage.saved, {}, shape)                         # and nothing is remembered
                self.write(self.LAUNCHER, [bash_line(T0 + 1, 'cd /w && claude -p --model m "%s"' % TEXT, tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])
                with mock.patch('time.time', lambda: T0 + 700):
                    self.scan()
                o = self.links.cli_owners[KID]
                self.assertEqual((o['sid'], o['rule'], o['certain']), (self.LAUNCHER, 'content', True), shape)      # the one that ran it is alone

    def test_the_command_position_and_the_command_line_a_wrapper_runs_still_count(self):
        for cmd in ('claude -p "x"', 'cd /w && nohup claude -p "x" &', 'timeout 600 claude -p "x"', 'FOO=1 env -i PATH=$PATH claude -p "x"', 'sudo -u me claude -p "x"',
                    "tmux new-session -d 'claude -p x'", "tmux new-session -d -s w 'claude -p x'", 'tmux new-session -d -s w claude -p x', "tmux new-window -n claude 'claude -p x'",
                    "tmux -L s new-window -c /w -e A=1 -n n 'cd /w && claude -p x'", "tmux split-window -h -c /w claude -p x",
                    'ssh box claude -p x', 'ssh -p 22 -i key box "cd /w && claude -p x"', 'xargs -I{} claude -p "{}"', 'xargs claude -p < prompts.txt', 'xargs -n1 -P4 claude -p',
                    'docker exec c claude -p x', 'docker run --rm -e A=1 --name n img claude -p x', 'kubectl exec -n ns pod -c main -- claude -p x',
                    "bash -c 'claude -p x'", 'echo go | xargs -n1 claude -p', 'watch -n 5 claude -p x', 'script -qc "claude -p x" /dev/null', 'su -c "claude -p x" me',
                    'screen -dmS n claude -p x', 'parallel claude -p {} ::: a b'):
            with self.subTest(cmd):
                self.assertTrue(link._weak_launch(cmd) or link.launch_kinds(cmd, '/w')[0] == {'claude'}, cmd)
                self.assertEqual(link.launch_kinds(cmd, '/w')[0], frozenset(('claude',)), cmd)

    def test_a_script_or_a_python_file_next_to_the_words_is_still_a_candidate_call(self):
        self.assertTrue(link._launchy_call('python3 tool.py claude -p'))
        self.assertTrue(link._launchy_call('./run.sh claude'))
        self.assertTrue(link._launchy_call('mytool run.sh --note claude'))
        self.assertFalse(link._launchy_call('curl --data "claude -p" https://example.invalid/'))


class ScriptFilesAreReadLikeCommands(CliFixture):
    """A script file's text is read as a command is: a script that only says `claude` (printed, typed into a terminal, an argument of a program) runs no tool."""
    RELAY = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

    def script(self, text):
        path = os.path.join(os.path.realpath(self.tmp.name), 'relay.sh')
        with open(path, 'w') as fh:
            fh.write(text)
        link._SCRIPTS.clear()
        link._SCRIPT_TOOLS.clear()
        return path

    def test_a_script_that_types_the_instruction_into_a_terminal_is_no_launch(self):
        path = self.script('#!/bin/bash\ntmux send-keys -t w "claude -p \\"$1\\"" Enter\n')
        cmd = 'bash %s "%s"' % (path, TEXT)
        self.assertEqual(link.launch_kinds(cmd, '/w'), (frozenset(), frozenset()))
        self.assertEqual(link.bash_scripts(cmd, '/w'), [])
        self.write(self.PARENT, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}}),
                                 bash_line(T0 - 3, cmd, tid='toolu_relay'), result_line(T0 - 2.5, 'toolu_relay')])
        self.write(KID, child_lines(T0 + 2, text=TEXT))
        with mock.patch('time.time', lambda: T0 + 600):
            self.scan()
        self.assertIsNone(self.links.cli_owners.get(KID))

    def test_what_a_script_runs_counts_and_what_it_prints_does_not(self):
        for text, want in (('claude -p "$1"\n', ({'claude'}, set())), ('cd /w\nnohup claude -p "$1" &\n', ({'claude'}, set())), ('codex exec "$1"\n', ({'codex'}, set())),
                           ("tmux new-session -d 'claude -p x'\n", ({'claude'}, set())), ('ssh box claude -p x\n', ({'claude'}, set())),
                           ('/usr/local/bin/claude -p "$1"\n', ({'claude'}, set())), ('python3 -c "import os; os.system(\'claude -p x\')"\n', (set(), {'claude'})),
                           ('echo claude\n', (set(), set())), ('# claude -p x\nmake\n', (set(), set())), ('grep claude notes.md\n', (set(), set())),
                           ('ssh box tmux send-keys -t w "claude -p x" Enter\n', (set(), set())), ('curl --data claude -p http://example.invalid/\n', (set(), set())),
                           ("cat <<'EOF'\nclaude -p x\nEOF\n", (set(), set()))):
            with self.subTest(text):
                got, may = link.script_tools(text)
                self.assertEqual((set(got), set(may)), want, text)


class WhereAWordRuns(unittest.TestCase):
    """exec_regions: the pieces of a command where the words claude / codex run, and where they only may."""

    def words(self, cmd):
        run, maybe = link.exec_regions(cmd)
        return link._tools_named('\n'.join(run)), link._tools_named('\n'.join(maybe))

    def test_where_it_runs(self):
        none, claude = frozenset(), frozenset({'claude'})
        for cmd, want in (('claude -p x', (claude, none)),
                          ('cd /w && nohup claude -p x &', (claude, none)),
                          ('timeout 600 claude -p x', (claude, none)),
                          ('FOO=1 sudo -u me claude -p x', (claude, none)),
                          ("tmux new-session -d 'claude -p x'", (claude, none)),
                          ("tmux -L s -f /x/conf split-window 'claude -p x'", (claude, none)),
                          ("tmux send-keys -t w 'claude -p x' Enter", (none, none)),
                          ('tmux send-keys -t w claude -p x Enter', (none, none)),
                          ("tmux send -t w 'claude -p x'", (none, none)),
                          ("tmux paste-buffer -t w; tmux set-buffer 'claude -p x'", (none, none)),
                          ("screen -dmS w claude -p x", (claude, none)),
                          ("screen -S w -p 0 -X stuff 'claude -p x'", (none, none)),
                          ("echo 'claude -p x'", (none, none)),
                          ('echo claude -p x', (none, none)),
                          ("printf '%s' 'claude -p x' | tmux load-buffer -", (none, none)),
                          ("ssh h 'claude -p x'", (claude, none)),
                          ("ssh h \"echo 'claude -p x'\"", (none, none)),
                          ('xargs -n1 claude -p', (claude, none)),
                          ("docker exec c sh -c 'claude -p x'", (claude, none)),
                          ('docker logs claude', (none, none)),
                          ("python3 -c \"os.system('claude -p x')\"", (none, claude)),
                          ("python3 -c \"os.system('tmux send-keys claude -p x')\"", (none, none)),
                          ("node -e \"exec('claude -p x')\"", (none, claude)),
                          ('script -qc "claude -p x" /dev/null', (claude, none)),
                          ('script -q out.txt', (none, none)),
                          ("grep 'claude -p' notes.md", (none, none)),
                          ('git commit -m "claude -p"; python3 run.py', (none, none)),
                          ('bash run.sh; echo "codex exec y"', (none, none)),
                          ('codex exec "y"', (frozenset({'codex'}), none)),
                          ('echo "$(claude -p x)"', (claude, none)),
                          ("echo 'unbalanced claude -p", (none, none))):
            self.assertEqual(self.words(cmd), want, cmd)

    def test_a_weak_launch_is_only_where_it_runs(self):
        for cmd, want in (("tmux new-session -d -s w 'claude -p \"go\"'", True), ("tmux send-keys -t w 'claude -p \"go\"' Enter", False),
                          ("echo claude -p go", False), ("printf '%s\\n' go | xargs claude -p", True), ("ssh h 'claude -p go'", True),
                          ("python3 -c \"os.system('claude -p go')\"", False), ("python3 - <<'EOF'\nclaude -p go\nEOF", False),
                          ("screen -S w -X stuff 'claude -p go'", False)):
            self.assertEqual(link._weak_launch(cmd), want, cmd)

    def test_a_span_is_made_only_for_a_call_that_can_run_the_words_or_a_script(self):
        for cmd, want in (("tmux send-keys -t w 'claude -p go.sh' Enter", False), ("tmux new-session -d 'claude -p go'", True), ('bash run.sh', True),
                          ('python3 tool.py claude', True), ("echo 'claude -p go'", False), ('python3 -c "os.system(\'claude -p go\')"', True)):
            self.assertEqual(link._launchy_call(cmd), want, cmd)


class FolderScenes(CliFixture):
    """The scene builders of the tests below: real folders and files (a script is read from disk), written as records and read by LinkIndex."""
    X, Y = CliFixture.PARENT, '77777777-7777-4777-8777-777777777777'

    def setUp(self):
        super().setUp()
        root = os.path.join(os.path.realpath(self.tmp.name), 'work')
        self.wx, self.wy = os.path.join(root, 'x'), os.path.join(root, 'y')
        os.makedirs(self.wx)
        os.makedirs(self.wy)

    def put(self, name, text, folder=None):
        path = os.path.join(folder or self.wx, name)
        with open(path, 'w') as fh:
            fh.write(text)
        return path

    def author(self, call, sid=None):
        """X wrote the instruction into a file and runs `call` (nothing came back from it yet)."""
        self.write(sid or self.X, [dump({'type': 'user', 'timestamp': iso(T0 - 900), 'cwd': self.wx, 'message': {'role': 'user', 'content': 'draft'}}),
                                   tool_line(T0 - 800, 'Write', {'file_path': os.path.join(self.wx, 'prompt.md'), 'content': TEXT}, 'tw1', self.wx),
                                   bash_line(T0 - 300, call, cwd=self.wx, tid='toolu_x')])

    def launcher(self):
        """Y reads the instruction through $(cat ...): its record does not hold the words."""
        self.write(self.Y, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': self.wy, 'message': {'role': 'user', 'content': 'run the review'}}),
                            bash_line(T0, 'claude -p "$(cat %s)"' % os.path.join(self.wx, 'prompt.md'), cwd=self.wy, tid='toolu_y'), result_line(T0 + 40, 'toolu_y')])

    def child(self):
        self.write(KID, child_lines(T0 + 2, cwd=self.wy, text=TEXT))
        with mock.patch('time.time', lambda: T0 + 600):
            self.links.scan()
        return self.links.cli_owners.get(KID)



class RealFolders(FolderScenes):
    def test_the_author_with_a_server_running_is_not_taken_for_the_launcher(self):
        self.put('server.py', 'import http.server\nprint("serving")\n')
        self.author('python3 %s --port 9000' % os.path.join(self.wx, 'server.py'))
        self.launcher()
        o = self.child()
        self.assertEqual((o['sid'], o['rule'], o['certain']), (self.Y, 'time', False))                  # the session that ran the launch, as a guess
        self.assertNotIn(('content_only', KID), [(d['code'], d['subject']) for d in self.links.diags])

    def diags_of_child(self):
        return [(d['code'], d.get('other')) for d in self.links.diags if d['subject'] == KID]

    def test_the_author_alone_is_no_parent_and_is_said(self):
        """Nothing in the records says the author launched the child: not linked, not even as a guess. The author is named in a diagnostic."""
        self.put('server.py', 'print("serving")\n')
        self.author('python3 %s' % os.path.join(self.wx, 'server.py'))
        self.assertIsNone(self.child())
        self.assertEqual(self.diags_of_child(), [('content_author_differs', self.X)])
        self.assertEqual(self.links.unlinked_for(self.Y), [])                                              # nobody else is a candidate either

    def test_a_test_run_and_a_read_shell_script_that_start_nothing_are_no_launch_either(self):
        for call in ('python3 -m unittest tests/test_big.py', 'bash {x}/build.sh', 'cd {x} && ./build.sh'):
            with self.subTest(call):
                self.assertIsNone(self.run_scene({'build.sh': '#!/bin/sh\nmake all\n'}, call))
                self.assertEqual(self.diags_of_child(), [('content_author_differs', self.X)])

    def test_a_script_that_only_says_or_types_the_words_runs_nothing(self):
        """A script whose text holds `claude` where nothing runs it (an echo, text typed into a terminal, an argument of a program that does not run it) is no launcher."""
        for text in ('echo claude is great\n', 'tmux send-keys -t w "claude -p \\"$1\\"" Enter\n', 'grep -n claude notes.md\ncurl --data claude -p http://example.invalid/\n',
                     'ssh box echo "claude -p x"\n'):
            with self.subTest(text):
                self.assertIsNone(self.run_scene({'plain.sh': text}, 'bash {x}/plain.sh'))
                self.assertEqual(link.launch_kinds('bash %s/plain.sh' % self.wx, self.wx), (frozenset(), frozenset()))
                self.assertEqual(link.bash_scripts('bash %s/plain.sh' % self.wx, self.wx), [])

    def run_scene(self, files, call):
        """A fresh scene: the files are put in the author's folder, the author runs `call` ({x} is that folder), the child is read. -> its owner entry or None."""
        self.setUp()
        for name, text in files.items():
            self.put(name, text)
        self.author(call.replace('{x}', self.wx))
        return self.child()

    def test_a_script_that_names_claude_may_launch_and_the_words_are_firm(self):
        for files, call, label in (({'launch.py': 'import subprocess\nsubprocess.run(["claude", "-p", open("prompt.md").read()])\n'}, 'python3 {x}/launch.py', 'a python file that names claude'),
                                   ({'wrap.sh': 'tmux new-session -d \'claude -p "something"\'\n'}, 'bash {x}/wrap.sh', 'a shell script that runs it through a wrapper'),
                                   ({}, 'tmux new-session -d \'claude -p "something"\'', 'a claude launch it could not place')):
            with self.subTest(label):
                o = self.run_scene(files, call)
                self.assertEqual((o['sid'], o['rule'], o['certain']), (self.X, 'content', True))

    def test_a_script_nobody_could_read_gives_a_guess_at_most(self):
        """A script that is not there, a path that is a variable, a file past the size limit, a binary one: nothing says it starts a session and nothing says
        it does not. The words in the author's record are a guess (rank 4), never a firm link."""
        big, binary = '# a long comment\n' * (link.SCRIPT_MAX // 16 + 10), 'echo hi\n\0\0'
        for files, call, label in (({}, 'bash {x}/gone.sh', 'a script that is not there'), ({}, 'python3 {x}/gone.py', 'a python file that is not there'),
                                   ({}, 'python3 $DIR/run.py', 'a path that is a variable'), ({'big.py': big}, 'python3 {x}/big.py', 'a python file past the size limit'),
                                   ({'bin.sh': binary}, 'bash {x}/bin.sh', 'a binary file')):
            with self.subTest(label):
                link._SCRIPTS.clear()
                o = self.run_scene(files, call)
                self.assertEqual((o['sid'], o['rule'], o['certain']), (self.X, 'content_short', False))
                self.assertNotIn('fingerprint_incomplete', [c for c, _ in self.diags_of_child()])      # the comparison itself was whole
                self.assertEqual((self.links.decisions[KID].assumed, self.links.decisions[KID].incomplete), (True, False))

    def test_a_script_nobody_could_read_is_not_held_to_one_child(self):
        """How many children a script starts is not known: two children whose instructions the author wrote and who ran one unreadable script are both guessed to be
        its, not one of them held back as the cap of a single launch (as before the change, when both were firm)."""
        kid2 = '66666666-6666-4666-8666-666666666666'
        self.write(self.X, [dump({'type': 'user', 'timestamp': iso(T0 - 900), 'cwd': self.wx, 'message': {'role': 'user', 'content': 'draft'}}),
                            tool_line(T0 - 800, 'Write', {'file_path': os.path.join(self.wx, 'one.md'), 'content': TEXT}, 'tw1', self.wx),
                            tool_line(T0 - 790, 'Write', {'file_path': os.path.join(self.wx, 'two.md'), 'content': OTHER}, 'tw2', self.wx),
                            bash_line(T0 - 300, 'python3 %s' % os.path.join(self.wx, 'gone.py'), cwd=self.wx, tid='toolu_x')])
        self.write(KID, child_lines(T0 + 2, cwd=self.wy, text=TEXT))
        self.write(kid2, child_lines(T0 + 3, cwd=self.wy, text=OTHER))
        with mock.patch('time.time', lambda: T0 + 600):
            self.links.scan()
        got = {k: (o['sid'], o['rule'], o['certain']) for k, o in self.links.cli_owners.items()}
        self.assertEqual(got, {KID: (self.X, 'content_short', False), kid2: (self.X, 'content_short', False)})

    def test_a_codex_exec_is_no_launcher_of_a_claude_child(self):
        self.author('codex exec "something"')
        self.assertIsNone(self.child())
        self.assertEqual(self.diags_of_child(), [('content_author_differs', self.X)])

    def test_a_sub_agents_bare_python_call_is_asked_about_when_it_matters(self):
        """The sub-agent wrote the words and ran a python file: it shows who wrote them, not who launched (the file is read only now, from the record)."""
        self.put('server.py', 'print("serving")\n')
        sub = 'a' + '3' * 16
        os.makedirs(os.path.join(self.proj, self.X, 'subagents'))
        self.write(self.X, [dump({'type': 'user', 'timestamp': iso(T0 - 900), 'cwd': self.wx, 'message': {'role': 'user', 'content': 'orchestrate'}})])
        with open(os.path.join(self.proj, self.X, 'subagents', 'agent-%s.jsonl' % sub), 'w') as fh:
            fh.write('\n'.join([tool_line(T0 - 800, 'Write', {'file_path': os.path.join(self.wx, 'prompt.md'), 'content': TEXT}, 'tw1', self.wx),
                                 bash_line(T0 - 300, 'python3 %s' % os.path.join(self.wx, 'server.py'), cwd=self.wx, tid='toolu_x')]) + '\n')
        self.assertIsNone(self.child())
        self.assertEqual(self.diags_of_child(), [('content_author_differs', self.X)])


class OutputFileOfAnotherSession(FolderScenes):
    """The whole scene of an output file that was written over: A redirected another launch to the path, B started the child, C resumed the child into the
    same path much later. The file names the child, and it is C's output."""

    def test_the_first_writer_of_the_path_is_not_the_launcher(self):
        out = os.path.join(self.wy, 'out.json')
        a, b, c = '88888888-8888-4888-8888-888888888888', '99999999-9999-4999-8999-999999999999', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        self.write(a, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': self.wy, 'message': {'role': 'user', 'content': 'go'}}),
                       bash_line(T0 - 12, 'cd %s && claude -p "another task entirely" > %s' % (self.wy, out), cwd=self.wy, tid='toolu_a'), result_line(T0 - 5, 'toolu_a')])
        self.write(b, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': self.wy, 'message': {'role': 'user', 'content': 'go'}}),
                       tool_line(T0 - 50, 'Write', {'file_path': os.path.join(self.wy, 'p.md'), 'content': TEXT}, 'tw1', self.wy),
                       bash_line(T0, 'cd %s && claude -p "$(cat p.md)"' % self.wy, cwd=self.wy, tid='toolu_b'), result_line(T0 + 40, 'toolu_b')])
        self.write(c, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': self.wy, 'message': {'role': 'user', 'content': 'go'}}),
                       bash_line(T0 + 90, 'cd %s && claude -p --resume %s "go on" > %s' % (self.wy, KID, out), cwd=self.wy, tid='toolu_c'), result_line(T0 + 95, 'toolu_c')])
        with open(out, 'w') as fh:
            json.dump({'type': 'result', 'session_id': KID}, fh)
        o = self.child()
        self.assertEqual((o['sid'], o['rule']), (b, 'content'))                                          # B's call carries the words; A's output path is not the proof

    def test_without_the_overwrite_the_same_file_proves_the_writer(self):
        out = os.path.join(self.wy, 'out.json')
        self.write(self.Y, [dump({'type': 'user', 'timestamp': iso(T0 - 100), 'cwd': self.wy, 'message': {'role': 'user', 'content': 'go'}}),
                            bash_line(T0, 'cd %s && claude -p "$(cat p.md)" > %s' % (self.wy, out), cwd=self.wy, tid='toolu_y'), result_line(T0 + 40, 'toolu_y')])
        with open(out, 'w') as fh:
            json.dump({'type': 'result', 'session_id': KID}, fh)
        o = self.child()
        self.assertEqual((o['sid'], o['rule']), (self.Y, 'out'))


class WideTexts(CliFixture):
    """The limits count bytes held: a window of Korean text that is half the characters of an ASCII one is as big in memory."""

    def test_a_window_of_wide_text_is_cut_by_bytes_and_makes_no_certain_link(self):
        instr = ' '.join(['지시문 %d번째 낱말과 설명이 이어지는 긴 문장' % i for i in range(12)])
        filler = [bash_line(T0 - 1000 + i, "cat > /tmp/f%d <<'EOF'\n%s\nEOF" % (i, '가나다라마바사 ' * 16000), tid='toolu_f%d' % i) for i in range(12)]      # 12 x 112 000 characters
        self.write(self.PARENT, filler + [bash_line(T0 - 2, "cat > /tmp/p.txt <<'EOF'\n%s\nEOF" % instr, tid='toolu_p'), bash_line(T0, 'cd /w && claude -p "$(cat /tmp/p.txt)"', tid='toolu_1')])
        self.write(KID, [dump({'type': 'user', 'timestamp': iso(T0 + 3), 'cwd': '/w', 'message': {'role': 'user', 'content': instr}})])
        self.assertLess(sum(len(b) for b in [('가나다라마바사 ' * 16000)] * 12), 3 * fp.OWNER_BYTES)     # fewer characters than the limit's number of bytes ...
        o = self.scan()[KID]
        self.assertEqual(o['rule'], 'content_short')                                                     # ... but more bytes than the limit
        self.assertIn('fingerprint_incomplete', [d['code'] for d in self.links.diags if d['subject'] == KID])
        self.assertLessEqual(self.links.tcache.size, fp.CACHE_BYTES)

    def test_a_cut_line_makes_the_window_incomplete(self):
        instr = ' '.join(['instruction %d with enough words to be told apart from the rest of the text' % i for i in range(8)])
        big = tool_line(T0 - 50, 'Write', {'file_path': '/w/big.txt', 'content': instr + ' ' + 'filler words ' * 30000}, 'tw1')       # about 400 KB: cut at the line limit
        self.write(self.PARENT, [big, bash_line(T0, 'cd /w && claude -p "$(cat /w/big.txt)"', tid='toolu_1')])
        self.write(KID, [dump({'type': 'user', 'timestamp': iso(T0 + 3), 'cwd': '/w', 'message': {'role': 'user', 'content': instr}})])
        o = self.scan()[KID]
        self.assertEqual(o['rule'], 'content_short')
        self.assertIn('fingerprint_incomplete', [d['code'] for d in self.links.diags if d['subject'] == KID])
        self.assertEqual(link.line_texts(big.encode())[0][-1], fp.CUT)


class OutputFilesAreReadSafely(CliFixture):
    def test_a_file_the_policy_refuses_is_never_opened(self):
        """The output file named by a record goes through the document policy (util.stat_plain / open_safe): a path it refuses is neither read nor trusted."""
        out = os.path.join(os.path.realpath(self.tmp.name), 'o.json')
        with open(out, 'w') as fh:
            fh.write('{"session_id": "%s"}' % KID)
        self.write(self.PARENT, [bash_line(T0, 'cd /w && claude -p hi > %s' % out, tid='toolu_1'), result_line(T0 + 40, 'toolu_1')])
        self.write(KID, child_lines(T0 + 2, text='hi'))
        opened = []
        real = link.open_safe
        with mock.patch.object(link, 'stat_plain', lambda path: None), mock.patch.object(link, 'open_safe', lambda *a, **kw: (opened.append(a), real(*a, **kw))[1]):
            owners = self.scan()
        self.assertNotEqual((owners.get(KID) or {}).get('rule'), 'out')                                  # refused by the policy: no proof
        self.assertEqual(opened, [])
        self.links.out.seen.clear()
        self.links.out.file_sids.clear()
        self.links.out.sid_files.clear()
        self.assertEqual(self.scan()[KID]['rule'], 'out')                                                # the same file through the policy: read and trusted


if __name__ == '__main__':
    unittest.main()
