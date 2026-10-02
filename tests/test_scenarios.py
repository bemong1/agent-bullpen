"""The scenario generator (tools/scenarios): axes -> synthetic HOME -> truth (oracle) -> what the board shows (observe) -> pass / miss / wrong.

The strict xfail list (tests/scenarios_xfail.json) is exactly the set of red cells of the board as it is today: a red cell that is not listed, a listed cell that
now passes, a listed cell whose value changed, a stale entry: each fails here. Fixing the board turns cells green; the fixer then regenerates the list with
`python3 -m tools.scenarios.run --write-xfail` and the diff shows which cells moved. The table of red cells comes from
`python3 -m tools.scenarios.run --md`, never from a test.

    python3 -m unittest discover -s tests
"""
import contextlib
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unicodedata
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402,F401  (puts the repo root first on sys.path)

from tools.scenarios import axes, build, observe, oracle, run  # noqa: E402
from tools.scenarios.axes import AXES, BUNDLES, Case, WAY  # noqa: E402
from tools.scenarios.observe import MISSING  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A guard against the generator growing without notice, not a speed test: about 15 s on a developer machine, roughly twice that on a
# shared CI runner. AGENT_BULLPEN_SCENARIO_BUDGET overrides it for a slower machine.
BUDGET_SEC = float(os.environ.get('AGENT_BULLPEN_SCENARIO_BUDGET') or 60.0)


class OracleIsIndependent(unittest.TestCase):
    def test_oracle_and_axes_never_import_board(self):
        code = ('import sys; import tools.scenarios.oracle, tools.scenarios.axes; '
                'bad = [m for m in sys.modules if m == "board" or m.startswith("board.")]; '
                'sys.exit("board imported: %s" % bad if bad else 0)')
        r = subprocess.run([sys.executable, '-c', code], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_facts_module_is_definitions_only(self):
        import ast
        with open(os.path.join(REPO, 'board', 'facts.py')) as f:
            tree = ast.parse(f.read())
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertEqual(funcs, [], 'board/facts.py defines shapes and enumerations only')
        imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, {'re', 'dataclasses', 'typing'})


class Grading(unittest.TestCase):
    """The pass / miss / wrong table of run.py."""

    def test_table(self):
        g = run.grade
        self.assertEqual(g('seat', None, MISSING), 'pass')          # nothing to show, nothing shown
        self.assertEqual(g('seat', None, None), 'pass')
        self.assertEqual(g('seat', None, 'B'), 'wrong')             # a false seat
        self.assertEqual(g('tree', 'x', MISSING), 'miss')
        self.assertEqual(g('tree', 'x', None), 'miss')
        self.assertEqual(g('tree', 'x', 'x'), 'pass')
        self.assertEqual(g('tree', 'x', 'y'), 'wrong')
        self.assertEqual(g('tree', None, 'y'), 'wrong')
        self.assertEqual(g('status', 'unknown', 'ended'), 'wrong')  # claiming what the records cannot tell
        self.assertEqual(g('status', 'unknown', 'unknown'), 'pass')
        self.assertEqual(g('status', 'interrupted', 'unknown'), 'miss')
        self.assertEqual(g('status', 'interrupted', 'ended'), 'wrong')
        self.assertEqual(g('node', None, MISSING), 'miss')           # None is "the main session": not emitting it is a miss
        self.assertEqual(g('node', None, None), 'pass')
        self.assertEqual(g('node', 'a' * 17, None), 'wrong')         # claimed the main session for a sub-agent's child
        self.assertEqual(g('node', 'a' * 17, MISSING), 'miss')
        self.assertEqual(g('rule_class', 'certain', 'guess'), 'miss')    # less sure than the records allow
        self.assertEqual(g('rule_class', 'guess', 'certain'), 'wrong')   # surer than they allow
        self.assertEqual(g('rule_class', 'none', 'guess'), 'wrong')
        self.assertEqual(g('rule_class', 'none', 'none'), 'pass')
        self.assertEqual(g('resets_at', 1000.0, 1000.4), 'pass')
        self.assertEqual(g('resets_at', 1000.0, 1100.0), 'wrong')
        self.assertEqual(g('reason', None, MISSING), 'pass')
        self.assertEqual(g('reason', 'limit', MISSING), 'miss')

    def test_a_set_of_places_is_graded_as_a_whole(self):
        g = run.grade
        one, two = frozenset(['u|1|B']), frozenset(['u|1|B', 'u|1|B_last'])
        self.assertEqual(g('placements', one, one), 'pass')
        self.assertEqual(g('placements', one, two), 'wrong')                # a seat the truth does not list is a false seat, whatever else is right
        self.assertEqual(g('placements', two, one), 'miss')                 # a listed seat that is not shown is a miss
        self.assertEqual(g('placements', one, frozenset(['u|1|C'])), 'wrong')
        self.assertEqual(g('placements', frozenset(), one), 'wrong')        # nothing to show, a seat shown
        self.assertEqual(g('placements', frozenset(), frozenset()), 'pass')
        self.assertEqual(g('placements', frozenset(), MISSING), 'pass')
        self.assertEqual(g('placements', one, MISSING), 'miss')
        self.assertEqual(g('units', frozenset(['a', 'b']), frozenset(['a', 'b', 'c'])), 'wrong')      # a debate the folder list should not have
        self.assertEqual(g('seat', 'B', ('B', 'B_last')), 'wrong')          # an agent that holds two seats has no one seat
        self.assertEqual(g('seat', None, ('B', 'C')), 'wrong')


class HonestGrading(unittest.TestCase):
    """A false place or a moved diagnostic cannot hide behind a right one: the board shows the child in two seats (the right one last), or says a diagnostic
    about the child as one about the orchestrator."""

    def deb_state(self, cells, folder='r1'):
        """The API state of one topic: `cells` = [(seat, state)] of the agent G, each in the round folder `folder` (the board gives the real path of the file)."""
        rows = [{'p': seat, 'cells': [{'agent': 'G', 'round': 1, 'state': state, 'readers': [], 'path': '/repo/docs/rev/%s/%s.md' % (folder, seat)}]} for seat, state in cells]
        return {'agents': [{'id': 'G', 'tag': 'B gate'}], 'debates': [{'topics': [{'dir': '/repo/docs/rev', 'rounds': [1], 'rows': rows}]}]}

    def graded(self, cells, folder='r1', **v):
        case = axes.normalize(Case('deb', dict({'role': 'writer', 'kind': 'sub', 'rpath': 'abs', 'fstate': 'written'}, **v)))
        b = types.SimpleNamespace(main_path='m', ids={'child': 'G'}, meta={'repo': '/repo'}, t0=0)
        obs = observe.Obs()
        observe.read_deb(b, obs, types.SimpleNamespace(sessions={'m': (None, self.deb_state(cells, folder))}))
        return {x.field: x.result for x in run.grade_case(case, b, obs, oracle.truth(case)) if x.role == 'child'}

    def test_the_right_seat_alone_passes(self):
        got = self.graded([('B', 'draft')])
        self.assertEqual({k: v for k, v in got.items() if v != 'pass'}, {})

    def test_a_false_seat_before_the_right_one_is_not_overwritten(self):
        got = self.graded([('B_last', 'done'), ('B', 'draft')])
        self.assertEqual(got['placements'], 'wrong')
        self.assertEqual(got['seat'], 'wrong')

    def test_a_false_seat_after_the_right_one_is_not_ignored_either(self):
        got = self.graded([('B', 'draft'), ('B_last', 'done')])
        self.assertEqual((got['placements'], got['seat']), ('wrong', 'wrong'))

    def test_a_seat_where_the_truth_has_none_is_wrong_in_every_position(self):
        for cells in ([('B', 'draft')], [('A', 'done'), ('B', 'draft')]):
            got = self.graded(cells, role='quoter', rpath='abs', fstate='none')
            self.assertEqual(got['placements'], 'wrong', cells)

    def test_the_file_of_a_seat_is_graded_with_the_spelling_of_its_round_folder(self):
        """`r1/B.md` and `r01/B.md` are two files: a board that names the one in the other folder is wrong, whichever seat, round and state it shows."""
        case = dict(rdir='both', fstate='none', kind='cli', life='running')
        got = self.graded([('B', 'writing')], **case)
        self.assertEqual({k: v for k, v in got.items() if v != 'pass' and not k.startswith('diag:')}, {})      # the right file, the right state
        got = self.graded([('B', 'writing')], folder='r01', **case)
        self.assertEqual(got['placements'], 'wrong')                                              # the same seat, round and state in the other folder
        self.assertEqual((got['unit'], got['round'], got['seat'], got['cell']), ('pass', 'pass', 'pass', 'pass'))     # the fields that never saw the folder
        got = self.graded([('B', 'draft'), ('B', 'draft')], **case)
        self.assertEqual(got['placements'], 'pass')                                               # one place twice is one place

    def test_every_round_folder_spelling_is_a_file_of_its_own(self):
        for rdir, folder in (('r1', 'r1'), ('r01', 'r01'), ('round1', 'round1')):
            right = self.graded([('B', 'draft')], folder=folder, rdir=rdir)
            self.assertEqual(right['placements'], 'pass', rdir)
            for other in {'r1', 'r01', 'round1'} - {folder}:
                self.assertEqual(self.graded([('B', 'draft')], folder=other, rdir=rdir)['placements'], 'wrong', (rdir, other))

    def test_a_diagnostic_about_the_child_said_about_the_orchestrator_is_wrong(self):
        case = axes.normalize(Case('sta', {'skind': 'cli', 'life': 'normal_end', 'flaw': 'torn'}))
        b = types.SimpleNamespace(ids={}, t0=0)
        obs = observe.Obs()
        obs.diag = {('torn_lines', 'orch')}
        got = {(x.role, x.field): x.result for x in run.grade_case(case, b, obs, oracle.truth(case)) if x.field.startswith('diag:')}
        self.assertEqual(got, {('child', 'diag:torn_lines'): 'miss', ('orch', 'diag:torn_lines'): 'wrong'})

    def test_the_small_values_of_a_diagnostic_are_compared(self):
        case = axes.normalize(Case('sta', {'skind': 'main', 'life': 'limit_auto', 'at': 'just_ended'}))
        b = types.SimpleNamespace(ids={}, t0=1000, T=lambda off: 1000 + off)
        T = oracle.truth(case)
        self.assertEqual(T.diag_params[('limit_group', 'orch')], {'members': 2, 'resets_at': ('T', 7200)})

        def graded(entries):
            obs = observe.Obs()
            obs.diag = {('limit_group', 'orch')}
            obs.diag_params = {('limit_group', 'orch'): entries}
            return {x.field: x.result for x in run.grade_case(case, b, obs, T) if x.field.startswith('diag:limit_group')}
        self.assertEqual(graded([{'members': 2, 'resets_at': 8200.0}]), {'diag:limit_group': 'pass', 'diag:limit_group.members': 'pass', 'diag:limit_group.resets_at': 'pass'})
        self.assertEqual(graded([{'members': 3, 'resets_at': 8200.0}])['diag:limit_group.members'], 'wrong')            # a group of three the scene does not have
        self.assertEqual(graded([{'members': 2, 'resets_at': 9999.0}])['diag:limit_group.resets_at'], 'wrong')
        self.assertEqual(graded([{'members': 2}])['diag:limit_group.resets_at'], 'miss')                                  # the entry does not say when
        self.assertEqual(graded([{'members': 2, 'resets_at': 8200.0}, {'members': 5, 'resets_at': 8200.0}])['diag:limit_group.members'], 'wrong')    # every entry has to be right

    def test_the_process_table_diagnostic_is_the_pages_and_is_graded_by_code(self):
        case = axes.normalize(Case('sta', {'skind': 'cli', 'life': 'crash', 'os': 'mac_nops'}))
        b = types.SimpleNamespace(ids={}, t0=0)
        T = oracle.truth(case)
        self.assertIn(('proc_unknown', 'child'), T.diag)
        for who in (('proc_unknown', 'child'), ('proc_unknown', 'orch')):
            obs = observe.Obs()
            obs.diag = {who}
            got = {x.field: x.result for x in run.grade_case(case, b, obs, T) if x.field == 'diag:proc_unknown'}
            self.assertEqual(got, {'diag:proc_unknown': 'pass'}, who)


class Axes(unittest.TestCase):
    def test_every_value_has_a_meaning_in_the_tables(self):
        self.assertEqual(set(WAY), set(AXES['way']))
        for bundle, names in BUNDLES.items():
            for a in names:
                self.assertIn(a, AXES, a)
                self.assertTrue(set(axes.BASE) >= {a}, a)

    def test_normalisation_is_idempotent(self):
        import random
        rnd = random.Random(7)
        for bundle, names in BUNDLES.items():
            for _ in range(1500):
                c = axes.normalize(Case(bundle, {n: rnd.choice(AXES[n]) for n in names}))
                self.assertEqual(axes.normalize(c).key(), c.key(), c.id)

    def test_case_id_round_trips(self):
        for bundle in BUNDLES:
            c = axes.normalize(Case(bundle, {}))
            self.assertEqual(Case.from_id(c.id).id, c.id)

    def test_ids_and_times_come_from_the_case_id(self):
        c = Case('aff', {})
        self.assertEqual(build.sid_of(c.id, 'orch'), build.sid_of(c.id, 'orch'))
        self.assertNotEqual(build.sid_of(c.id, 'orch'), build.sid_of(c.id, 'child'))
        self.assertNotEqual(build.sid_of(c.id, 'orch'), build.sid_of(Case('aff', {'way': 'bg'}).id, 'orch'))


class Selection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = run.select()

    def test_count_and_uniqueness(self):
        ids = [c.id for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(700 <= len(ids) <= 3000, len(ids))

    def test_the_cover_is_deterministic(self):
        names = run.PAIR_AXES['sta']
        first = [c.id for c in run.pairwise('sta', names)]
        self.assertEqual(first, [c.id for c in run.pairwise('sta', names)])

    def test_the_real_cases_are_there(self):
        names = {n for c in self.cases if c.real for n in c.real.split('/')}
        want = {'A%d' % i for i in range(1, 22)} | {'B%d' % i for i in range(1, 11)} | {'C%d' % i for i in range(1, 17)}
        self.assertEqual(names, want)

    def test_every_pair_of_values_inside_a_bundle_is_covered_or_impossible(self):
        import itertools
        import random
        for bundle in ('aff', 'deb', 'sta', 'cpl', 'room'):
            names = run.PAIR_AXES[bundle]
            covered = set()
            for c in (c for c in self.cases if c.bundle == bundle and c.twin_of is None):
                for a, b in itertools.combinations(names, 2):
                    covered.add((a, c.v[a], b, c.v[b]))
            rnd = random.Random(11)
            for a, b in itertools.combinations(names, 2):
                for x in AXES[a]:
                    for y in AXES[b]:
                        if (a, x, b, y) in covered:
                            continue
                        # not covered: it must be impossible, i.e. the normaliser folds the pair away in every completion we try
                        for _ in range(100):
                            row = {n: rnd.choice(AXES[n]) for n in names}
                            row[a], row[b] = x, y
                            probe = axes.normalize(Case(bundle, row))
                            self.assertFalse(probe.v[a] == x and probe.v[b] == y, 'a coverable pair is missing from the cover: %s %s' % (bundle, (a, x, b, y)))

    def test_every_positive_has_its_decoy_twins(self):
        """Each positive of the real cases and the pairwise cover has its twins in the list: the classic one (a lookalike replaces the launch) and, for a launch
        with a complete literal argument, the stranger twin (the launch stays and only the child is foreign). Positives that differ only in how the launch was
        written share one lookalike: the twin has no launch of its own."""
        ids = {c.id for c in self.cases}
        pos = [c for c in self.cases if c.core and c.twin_of is None and run.is_positive(c)]
        lacking = [c.id for c in pos for t in run.twins_of(c) if t.id not in ids]
        self.assertEqual(lacking, [])
        twins = [c for c in self.cases if c.twin_of]
        self.assertGreater(len(twins), 80)
        for t in twins:
            self.assertIn(t.twin_of, ids)
            self.assertNotEqual(t.id, t.twin_of)
        strangers = [c for c in twins if c.bundle == 'aff' and c.v['bait'] == 'foreign']
        self.assertGreater(len(strangers), 60)
        kept = 0
        for c in strangers:                                                       # the stranger twin keeps the evidence path of its positive
            pos_v = Case.from_id(c.twin_of).v
            if pos_v['way'] == c.v['way'] and (pos_v['via'], pos_v['src'], pos_v['form']) == ('arg', 'call', 'new'):
                kept += 1
                for axis in ('spawner', 'out', 'cwd', 'timing', 'seen', 'os'):
                    self.assertEqual(c.v[axis], pos_v[axis], (c.id, axis))
        self.assertGreater(kept, 20)


def _norm(text):
    text = unicodedata.normalize('NFC', text)
    for ch in '\\\'"`':
        text = text.replace(ch, '')
    return re.sub(r'\s+', ' ', text).strip()


def _coverage(prompt, blob):
    """Share of 16 evenly spread 32-char anchors of the prompt found in the blob."""
    p = _norm(prompt)
    if len(p) < 32:
        return 1.0 if p in blob else 0.0
    step = max(1, (len(p) - 32) // 15)
    anchors = [p[i:i + 32] for i in range(0, len(p) - 31, step)][:16]
    return sum(1 for a in anchors if a in blob) / float(len(anchors))


def _read_lines(path):
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()]


def _texts(path):
    """Everything the main session or a sub-agent wrote into its record that a launch could be read from: Bash commands, Write contents, Edit new strings."""
    out = []
    for d in _read_lines(path):
        if d.get('type') != 'assistant':
            continue
        for blk in d['message']['content']:
            if blk.get('type') == 'tool_use':
                i = blk['input']
                out.append(i.get('command') or i.get('content') or i.get('new_string') or '')
    return ' '.join(out)


class BuilderMatchesOracle(unittest.TestCase):
    """The oracle says which evidence exists; the files must really hold it (and nothing more). Built only, the board is not run."""

    def test_affiliation_evidence_is_in_the_files(self):
        root = tempfile.mkdtemp(prefix='scen-consist-')
        checked = 0
        try:
            for c in run.select():
                if c.bundle != 'aff' or c.v['target'] != 'cli' or c.v['bait'] != 'none' or c.v['form'] in ('nopersist', 'deleted', 'resume'):
                    continue
                v = c.v
                b = build.build_case(c, os.path.join(root, axes.digest(c.id, n=10)))
                ev = oracle.aff_evidence(v)
                child = _read_lines(b.paths['child'])
                sdk = [d for d in child if d.get('promptSource') == 'sdk']
                prompt = sdk[-1]['message']['content']
                launcher = b.paths['sub'] if v['spawner'] == 'sub' else (b.paths['mid'] if v['spawner'] == 'grand' else b.paths['orch'])
                if v['out'] == 'overwritten':
                    launcher = b.paths['orch2']                            # a peer session started the child
                blob = _norm(_texts(launcher))
                cov = _coverage(prompt, blob)
                label = '%s coverage %.2f' % (c.id, cov)
                if v['decoy'] == 'twin_text':
                    continue
                if v['src'] == 'absent' and v['decoy'] != 'same_n':
                    self.assertLess(cov, 0.4, label)
                elif v['author'] != 'self':
                    # the text is in the records of whoever wrote it, and only there (a child a person started has no launcher: the author is the only session to hold it)
                    if v['starter'] == 'call':
                        self.assertLess(cov, 0.4, label)
                    author = {'peer': 'author', 'main': 'orch', 'sub': 'writer'}[v['author']]
                    self.assertGreaterEqual(_coverage(prompt, _norm(_texts(b.paths[author]))), 0.4, label)
                    self.assertFalse(ev['content'] or ev['short'], label)
                else:
                    self.assertGreaterEqual(cov, 0.4, label)
                # the process snapshot: the environment names the tree only when the way keeps it
                for ph in b.phases:
                    for p in ph.procs:
                        if p['session'] and p['session'].get('sessionId') == b.ids['child']:
                            has_env = bool((p['env'] or {}).get('CLAUDE_CODE_SESSION_ID'))
                            self.assertEqual(has_env, WAY[v['way']]['env'] and v['starter'] == 'call', label)      # a child a person started has no Claude above it
                # the output file holds the child's id exactly when the oracle says it is a proof
                out = os.path.join(b.scratch, 'out.json')
                text = ''
                if os.path.exists(out):
                    with open(out) as fh:
                        text = fh.read()
                if ev['out']:
                    self.assertIn(b.ids['child'], text, label)
                elif v['out'] in ('reused', 'removed', 'none', 'log_only'):
                    self.assertNotIn(b.ids['child'], text, label)
                checked += 1
                shutil.rmtree(os.path.join(root, axes.digest(c.id, n=10)), ignore_errors=True)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertGreater(checked, 200)


class OracleDecisions(unittest.TestCase):
    """The decisions the oracle's truth follows, one assertion each. They are about the truth, not about the board."""

    def aff(self, **v):
        return oracle.truth(axes.normalize(Case('aff', v))).subjects['child']

    def test_a_competing_launch_with_another_literal_text_is_refuted(self):
        t = self.aff(decoy='concurrent', way='bg', via='stdin', src='absent', seen='ended_unseen')       # only the time rule can link this child
        self.assertEqual((t['tree'], t['rule_class']), ('@orch', 'guess'))
        self.assertNotIn('unlinked', t)
        t = self.aff(decoy='twin_text', way='bg', via='stdin', src='absent', seen='ended_unseen')       # the same literal text twice stays a tie
        self.assertEqual((t['tree'], t['unlinked']), (None, 'ambiguous'))

    def test_session_id_in_the_call_is_third_rank_evidence(self):
        t = self.aff(form='session_id', way='direct', via='subst', src='absent', seen='ended_unseen')
        self.assertEqual((t['tree'], t['rule_class']), ('@orch', 'certain'))
        t = self.aff(form='new', way='direct', via='subst', src='absent', seen='ended_unseen')
        self.assertEqual(t['rule_class'], 'guess')                                                         # the same launch without the id: only the time rule

    def test_a_crash_is_ended_for_a_child_with_a_notice_and_unknown_without_one(self):
        def state(skind):
            c = axes.normalize(Case('sta', {'skind': skind, 'life': 'crash', 'os': 'mac_nops'}))
            return oracle.truth(c).subjects['child']
        self.assertEqual((state('cli')['status'], state('cli')['reason']), ('ended', 'crash'))
        self.assertEqual((state('codex')['status'], state('codex')['reason']), ('unknown', None))

    def test_a_quiet_codex_thread_on_macos_may_be_stalled_or_unknown(self):
        def truth(skind, os_kind):
            return oracle.truth(axes.normalize(Case('sta', {'skind': skind, 'life': 'stalled_silent', 'os': os_kind})))
        self.assertEqual(truth('codex', 'linux').subjects['child']['status'], 'stalled')            # the open file ties the thread to its process
        self.assertEqual(truth('codex', 'mac').subjects['child']['status'], 'stalled')              # `ps` shows the process and the words it was started with ...
        self.assertEqual(truth('codex', 'mac').accepted[('child', 'status')], {'unknown'})          # ... a board that cannot tie it to the thread may say it does not know
        self.assertNotIn(('child', 'status'), truth('cli', 'mac').accepted)
        self.assertEqual(truth('codex', 'mac_nops').subjects['child']['status'], 'unknown')         # no `ps` at all: not a choice
        c = axes.normalize(Case('sta', {'skind': 'codex', 'life': 'stalled_silent', 'os': 'mac'}))
        b = types.SimpleNamespace(ids={}, t0=0)
        for shown, want in (('stalled', 'pass'), ('unknown', 'pass'), ('running', 'wrong'), ('ended', 'wrong')):
            obs = observe.Obs()
            obs.set('child', 'status', shown)
            got = {x.field: x.result for x in run.grade_case(c, b, obs, oracle.truth(c))}
            self.assertEqual(got['status'], want, shown)

    def test_format_drift_is_a_known_field_gone_inside_the_range_and_a_version_outside_it_says_nothing(self):
        for flaw, drift in (('old_format', False), ('future_version', False), ('field_gone', True), ('unknown_type', False), ('torn', False), ('none', False)):
            c = axes.normalize(Case('sta', {'skind': 'cli', 'life': 'normal_end', 'flaw': flaw}))
            self.assertEqual(('format_drift', 'child') in oracle.truth(c).diag, drift, flaw)

    def test_the_spelled_name_of_the_written_file_is_the_seat_when_names_collide(self):
        def seat(fstate):
            c = axes.normalize(Case('deb', {'kind': 'sub', 'nstyle': 'collide', 'rpath': 'var_ext', 'marker': 'own', 'fstate': fstate}))
            return oracle.truth(c).subjects['child']['seat']
        self.assertEqual(seat('written'), 'B_gate')
        self.assertEqual(seat('none'), 'B')                                                                # no write: the plain answer stays

    def test_limit_group_needs_two_members_with_the_same_reset(self):
        def diag(at):
            c = axes.normalize(Case('sta', {'skind': 'main', 'life': 'limit_auto', 'at': at}))
            return ('limit_group', 'orch') in oracle.truth(c).diag
        self.assertTrue(diag('just_ended'))                  # the orchestrator waits for the reset together with its agent
        self.assertTrue(diag('after_restart'))
        self.assertFalse(diag('after_resume'))               # after the automatic continue only the agent is left: one member

    def test_a_variable_set_outside_the_script_is_not_in_its_scope(self):
        c = axes.normalize(Case('aff', {'way': 'script', 'src': 'posarg', 'out': 'json_var', 'seen': 'ended_unseen'}))
        T = oracle.truth(c)
        self.assertEqual(T.subjects['child']['rule_class'], 'certain')                       # the instruction is in the call, so the content rule still names the parent
        self.assertIn(('path_unresolved', 'child'), T.diag)
        self.assertFalse(oracle.aff_evidence(c.v)['out'])
        c = axes.normalize(Case('aff', {'way': 'script', 'src': 'call', 'out': 'json_var', 'seen': 'ended_unseen'}))   # the script assigns it itself: in scope
        self.assertTrue(oracle.aff_evidence(c.v)['out'])

    def test_a_content_tie_is_reported_only_when_it_had_to_decide(self):
        def diag(**v):
            c = axes.normalize(Case('aff', dict({'decoy': 'twin_text', 'way': 'bg', 'via': 'subst', 'src': 'prior'}, **v)))
            return ('ambiguous_content', 'child') in oracle.truth(c).diag
        self.assertTrue(diag(seen='ended_unseen'))                                           # nothing else names the parent: the tie decides
        self.assertFalse(diag(seen='live'))                                                  # the process names it first
        self.assertFalse(diag(seen='ended_unseen', out='json_file'))                         # so does the output file

    def test_a_launch_with_no_child_behind_it_is_an_orphan_but_an_echo_or_a_stranger_process_is_not(self):
        for bait, orphan in (('cwd_mismatch', True), ('text_mismatch', True), ('too_old', True), ('foreign', True), ('echo_only', False), ('other_user', False), ('pid_reuse', False)):
            c = axes.normalize(Case('aff', {'bait': bait}))
            self.assertEqual(('orphan_launch', 'orch') in oracle.truth(c).diag, orphan, bait)

    def test_a_successful_write_settles_the_folder_of_an_ambiguous_path(self):
        def truth(fstate):
            c = axes.normalize(Case('deb', {'homonym': 'two', 'kind': 'cli', 'fstate': fstate}))
            return oracle.truth(c)
        T = truth('written')
        self.assertEqual((T.subjects['child']['unit'], T.subjects['child']['seat'], T.subjects['child']['role']), ('docs/rev', 'B', 'writer'))
        self.assertIn(('path_ambiguous', 'child'), T.diag)                                    # the instruction itself is still ambiguous
        T = truth('none')
        self.assertEqual((T.subjects['child']['unit'], T.subjects['child']['seat']), (None, None))
        self.assertIn(('path_ambiguous', 'child'), T.diag)

    def test_a_reader_needs_a_cell_to_be_a_reader_of(self):
        c = axes.normalize(Case('deb', {'structure': 'flat', 'role': 'reader', 'decl': 'no'}))
        self.assertEqual(oracle.truth(c).subjects['child']['role'], 'none')
        c = axes.normalize(Case('deb', {'structure': 'flat', 'role': 'reader', 'decl': 'yes'}))
        self.assertEqual(oracle.truth(c).subjects['child']['role'], 'reader')

    def test_a_tag_alone_is_no_seat(self):
        c = axes.normalize(Case('deb', {'role': 'tag_only'}))
        self.assertEqual(oracle.truth(c).subjects['child']['seat'], None)

    def test_reasons_of_resumed_and_torn_cells(self):
        import types
        from tools.scenarios.run import Cell, reason_of
        b = types.SimpleNamespace(ids={}, t0=0)

        def cell(bundle, field, result, ws, gs, **v):
            c = axes.normalize(Case(bundle, v))
            return Cell(c, 'child', field, result, ws, gs, b)
        self.assertEqual(reason_of(cell('aff', 'tree', 'wrong', '@orch', '@mid', form='resume', way='direct', seen='ended_unseen')), 'A-TREE-RESUME')
        self.assertEqual(reason_of(cell('sta', 'status', 'wrong', 'interrupted', 'ended', skind='cli', life='limit_exit', flaw='torn')), 'S-INTERRUPTED')
        self.assertEqual(reason_of(cell('sta', 'status', 'wrong', 'interrupted', 'failed', skind='sub', life='sub_limit_resume', flaw='torn')), 'S-FAILED')
        self.assertEqual(reason_of(cell('sta', 'status', 'wrong', 'done', 'ended', skind='cli', life='normal_end', flaw='torn')), 'S-TORN')
        self.assertEqual(reason_of(cell('cpl', 'status', 'miss', 'running', 'MISSING', spawner='grand')), 'C-GRAND-SCREEN')
        self.assertEqual(reason_of(cell('cpl', 'status', 'miss', 'running', 'MISSING', spawner='sub')), 'C-UNLINKED')


class LaterShapes(unittest.TestCase):
    """The shapes the first generator did not draw, found in real records: each is an axis value with its truth, and the builder writes what
    the value says (so a red cell is the board's, not the scene's)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-later-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, bundle, **v):
        c = axes.normalize(Case(bundle, v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def bash(self, path):
        return [(d['timestamp'], blk['input']['command'], blk['id']) for d in _read_lines(path) if d.get('type') == 'assistant'
                for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == 'Bash']

    # --- ids ---
    def test_a_later_axis_is_named_in_the_id_only_when_it_is_not_at_the_baseline(self):
        base = Case('aff', {}).id
        self.assertNotIn('author', base)
        self.assertNotIn('busy', base)
        self.assertNotIn('lang', Case('deb', {}).id)
        self.assertNotIn('os=', Case('deb', {}).id)
        c = axes.normalize(Case('aff', {'author': 'peer', 'busy': 'sh', 'seen': 'ended_unseen'}))
        self.assertIn('author=peer;busy=sh', c.id)
        self.assertEqual(Case.from_id(c.id).id, c.id)
        d = axes.normalize(Case('deb', {'kind': 'cli', 'os': 'mac_nops', 'life': 'stalled_silent', 'lang': 'ko'}))
        self.assertEqual(Case.from_id(d.id).key(), d.key())
        self.assertTrue(d.id.endswith('os=mac_nops;lang=ko'), d.id)

    def test_the_old_cover_still_has_its_ids(self):
        """Adding values and axes never renames a case that does not use them: the first 1038 ids of the generator are all still valid cases."""
        ids = {c.id for c in run.select()}
        c = axes.normalize(Case('aff', {'way': 'bg', 'out': 'json_file', 'seen': 'ended_unseen'}))
        self.assertIn(c.id, ids)
        self.assertEqual(axes.normalize(Case.from_id(c.id)).key(), c.key())

    # --- the instruction was written by somebody else ---
    def test_author_truth_the_launcher_is_the_session_that_launches_and_nobody_else(self):
        for author, spawner, node in (('peer', 'main', None), ('main', 'sub', '@sub'), ('sub', 'main', None)):
            c = axes.normalize(Case('aff', {'author': author, 'busy': 'py', 'seen': 'ended_unseen', 'way': 'direct'}))
            self.assertEqual(c.v['spawner'], spawner)
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child'], {'tree': '@orch', 'rule_class': 'guess', 'node': node}, author)                # no content proof, no process: the time rule only
            self.assertIn(('content_author_differs', 'child'), T.allowed)
            self.assertNotIn(('content_only', 'child'), T.diag)
            self.assertNotIn(('orphan_launch', 'orch'), T.diag)                                                                 # the launcher's call has its child
        c = axes.normalize(Case('aff', {'author': 'peer', 'seen': 'ended_unseen'}))
        self.assertIn(('child', 'tree', '@author'), oracle.truth(c).forbid)
        c = axes.normalize(Case('aff', {'author': 'peer', 'seen': 'live'}))
        self.assertEqual(oracle.truth(c).subjects['child']['rule_class'], 'certain')                                           # the process names the tree

    def test_author_scenes_put_the_text_with_the_author_and_a_call_that_launches_nothing(self):
        for author, busy in (('peer', 'py'), ('peer', 'sh'), ('peer', 'unittest'), ('main', 'child'), ('main', 'child_file'), ('sub', 'sh')):
            c, b = self.build('aff', author=author, busy=busy, seen='ended_unseen', way='bg', via='subst', src='prior')
            prompt = [d for d in _read_lines(b.paths['child']) if d.get('promptSource') == 'sdk'][0]['message']['content']
            writer = {'peer': 'author', 'main': 'orch', 'sub': 'writer'}[author]
            launcher = b.paths['sub'] if author == 'main' else b.paths['orch']
            self.assertGreaterEqual(_coverage(prompt, _norm(_texts(b.paths[writer]))), 0.4, c.id)
            self.assertLess(_coverage(prompt, _norm(_texts(launcher))), 0.4, c.id)
            busy_cmds = [cmd for _, cmd, _ in self.bash(b.paths[writer]) if 'cat >' not in cmd]
            self.assertEqual(len(busy_cmds), 1, (c.id, busy_cmds))
            self.assertEqual('claude -p' in busy_cmds[0], busy in ('child', 'child_file'), (c.id, busy_cmds))                  # only these authors launch anything
            if busy == 'child_file':
                self.assertIn('$(cat ', busy_cmds[0])                                                                           # ... reading its words from a file it wrote
            launch = [cmd for _, cmd, _ in self.bash(launcher) if 'claude -p' in cmd and '$(cat' in cmd]
            self.assertEqual(len(launch), 1, c.id)

    def test_an_author_that_conflicts_with_the_rest_of_the_scene_folds_back_to_the_launcher(self):
        for v in ({'bait': 'echo_only'}, {'form': 'resume'}, {'out': 'json_file'}, {'decoy': 'sibling'}, {'cwd': 'var'}, {'target': 'cx_exec'}):
            c = axes.normalize(Case('aff', dict({'author': 'peer'}, **v)))
            self.assertEqual((c.v['author'], c.v['busy']), ('self', 'py'), v)

    # --- an output file written over later ---
    def test_a_stale_output_path_names_the_peer_as_the_launcher(self):
        c, b = self.build('aff', out='overwritten', seen='ended_unseen')
        T = oracle.truth(c)
        self.assertEqual(T.subjects['child']['tree'], '@orch2')
        self.assertNotIn('node', T.subjects['child'])                                   # the child is on the peer's page, not on this one
        self.assertIn(('child', 'tree', '@orch'), T.forbid)
        self.assertIn(('content_only', 'child'), T.allowed)
        out_path = os.path.join(b.scratch, 'out.json')
        with open(out_path) as fh:
            self.assertIn(b.ids['child'], fh.read())                                       # the file does hold the child's id ...
        main_calls = [cmd for _, cmd, _ in self.bash(b.paths['orch'])]
        self.assertTrue(any('> ' + out_path in cmd for cmd in main_calls))                  # ... and the main session's call redirects there ...
        self.assertFalse(any(b.ids['child'] in cmd for cmd in main_calls))                  # ... but never names the child
        peer_calls = [cmd for _, cmd, _ in self.bash(b.paths['orch2'])]
        self.assertEqual(sum('--resume %s' % b.ids['child'] in cmd and '> ' + out_path in cmd for cmd in peer_calls), 1)       # the peer's later resume wrote it
        self.assertGreater(os.stat(out_path).st_mtime, b.T(200))

    # --- the folder of the launch ---
    def test_pushd_is_a_change_of_folder_and_a_variable_nothing_defines_is_not(self):
        c, b = self.build('aff', cwd='pushd', seen='ended_unseen')
        self.assertTrue(self.bash(b.paths['orch'])[-1][1].startswith('pushd '))
        self.assertEqual(oracle.truth(c).subjects['child'], oracle.truth(axes.normalize(Case('aff', {'cwd': 'cd', 'seen': 'ended_unseen'}))).subjects['child'])
        c, b = self.build('aff', cwd='var', seen='ended_unseen', via='subst', src='prior')
        calls = [cmd for _, cmd, _ in self.bash(b.paths['orch'])]
        self.assertTrue(any(cmd.startswith('cd "$WT" && claude') for cmd in calls))
        self.assertFalse(any('WT=' in cmd for cmd in calls))
        self.assertFalse(oracle.aff_evidence(c.v)['time'])                                  # the folder cannot be compared: no time rule
        self.assertEqual(oracle.truth(c).subjects['child']['rule_class'], 'certain')        # the text in the call still proves it

    def test_a_stranger_beside_a_launch_with_an_unknown_folder_is_in_another_project(self):
        c, b = self.build('aff', cwd='var', bait='foreign', seen='ended_unseen', via='subst', src='prior')
        self.assertEqual((c.v['via'], c.v['src']), ('subst', 'prior'))
        first = _read_lines(b.paths['child'])[0]
        self.assertTrue(first['cwd'].endswith('elsewhere'))
        self.assertEqual(oracle.truth(c).subjects['child'], {'tree': None, 'rule_class': 'none'})
        c = axes.normalize(Case('aff', {'cwd': 'var', 'bait': 'foreign', 'via': 'arg', 'src': 'call'}))
        self.assertEqual((c.v['via'], c.v['src']), ('arg', 'call'))                         # a complete literal argument stays what refutes a stranger

    # --- Codex lookalikes ---
    def test_codex_lookalikes_link_nothing(self):
        for bait in axes.CODEX_BAITS:
            c, b = self.build('aff', target='cx_exec', bait=bait, seen='ended_unseen')
            self.assertEqual(c.v['bait'], bait)
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child'], {'tree': None, 'rule_class': 'none'}, bait)
            self.assertIn(('child', 'tree', '@orch'), T.forbid)
            calls = [cmd for _, cmd, _ in self.bash(b.paths['orch'])]
            self.assertTrue(any('codex exec' in cmd for cmd in calls), bait)
        c, b = self.build('aff', target='cx_exec', bait='cwd_mismatch')
        self.assertTrue(any(' -C ' in cmd for _, cmd, _ in self.bash(b.paths['orch'])))
        c, b = self.build('aff', target='cx_exec', bait='echo_only')
        self.assertTrue(all(cmd.startswith('echo ') for _, cmd, _ in self.bash(b.paths['orch'])))
        for target in ('cx_tui', 'cx_desktop', 'cx_guardian'):
            self.assertEqual(axes.normalize(Case('aff', {'target': target, 'bait': 'text_mismatch'})).v['bait'], 'none')

    # --- a resumed run, stopped again ---
    def test_a_resumed_run_that_is_stopped_again_is_killed(self):
        for life in ('crash', 'limit_exit', 'api_529', 'time_limit_kill'):
            c, b = self.build('sta', skind='cli', life=life, at='resume_stopped')
            T = oracle.truth(c).subjects['child']
            self.assertEqual((T['status'], T['reason'], T['by']), ('killed', 'stopped', ('@orch', None)), life)
            main = _read_lines(b.paths['orch'])
            stops = [blk['input']['task_id'] for d in main if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == 'TaskStop']
            resume = [blk for d in main if d.get('type') == 'assistant' for blk in d['message']['content']
                      if blk.get('type') == 'tool_use' and blk['name'] == 'Bash' and '--resume' in blk['input']['command']]
            self.assertEqual(len(resume), 1, life)
            results = [d['toolUseResult'].get('backgroundTaskId') for d in main if isinstance(d.get('toolUseResult'), dict) and d['toolUseResult'].get('backgroundTaskId')]
            self.assertIn(stops[-1], results, life)                                                       # the TaskStop is of the resume call's task, not of the first run's
            self.assertEqual(stops[-1], [d['toolUseResult']['backgroundTaskId'] for d in main if isinstance(d.get('toolUseResult'), dict)
                                         and d['toolUseResult'].get('backgroundTaskId') and d['message']['content'][0].get('tool_use_id') == resume[0]['id']][0])
        self.assertEqual(axes.normalize(Case('sta', {'skind': 'sub', 'life': 'sub_limit_resume', 'at': 'resume_stopped'})).v['at'], 'after_resume')
        self.assertEqual(axes.normalize(Case('sta', {'skind': 'cli', 'life': 'normal_end', 'at': 'resume_stopped'})).v['at'], 'just_ended')

    def test_the_resume_call_says_what_the_resumed_run_is_told(self):
        """A resume call whose words are the first run's would be refuted as the launch of the second run: the call and the run's first line say the same."""
        for life in ('limit_exit', 'crash', 'kill_resume'):
            c, b = self.build('sta', skind='cli', life=life, at='after_resume')
            runs = [d['message']['content'] for d in _read_lines(b.paths['child']) if d.get('promptSource') == 'sdk']
            self.assertEqual(len(runs), 2)
            resume = [cmd for _, cmd, _ in self.bash(b.paths['orch']) if '--resume' in cmd][0]
            self.assertIn('"%s"' % runs[1], resume)
            self.assertNotIn(runs[0], resume)
            self.assertEqual(oracle.truth(c).subjects['child']['by'], ('@orch', None))

    # --- debate ---
    def test_the_list_of_debates_is_exactly_the_folders_on_disk(self):
        c = axes.normalize(Case('deb', {'structure': 'topics'}))
        self.assertEqual(oracle.truth(c).subjects['listing']['units'], {'docs/rev/t1', 'docs/rev/t2'})              # not 'docs/rev', the folder of the common brief
        self.assertEqual(oracle.truth(axes.normalize(Case('deb', {'homonym': 'two'}))).subjects['listing']['units'], {'docs/rev', 'docs/rev2'})
        c2, b = self.build('deb', structure='topics')
        self.assertTrue(os.path.exists(os.path.join(b.meta['root'], 'brief.md')))                                      # the common brief is there, one folder above the topics
        self.assertTrue(os.path.isdir(os.path.join(b.meta['repo'], '.git')))                                           # in a repository: the board walks from its top
        got = {'cells': frozenset(['docs/rev', 'docs/rev/t1', 'docs/rev/t2'])}
        self.assertEqual(run.grade('units', oracle.truth(c).subjects['listing']['units'], got['cells']), 'wrong')

    def test_a_tag_with_a_path_that_cannot_be_resolved_seats_nobody(self):
        c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'marker': 'none', 'fstate': 'none'}))
        T = oracle.truth(c)
        self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'))
        self.assertIn(('path_unresolved', 'child'), T.diag)
        c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'marker': 'own', 'fstate': 'none'}))
        self.assertEqual(oracle.truth(c).subjects['child']['seat'], 'B')                                              # its own marker does
        c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'marker': 'none', 'fstate': 'written', 'nstyle': 'numbered'}))
        self.assertEqual(oracle.truth(c).subjects['child']['seat'], 'opus1')                                          # so does a write of its own, under the name it wrote

    def test_a_reader_that_only_names_the_report_is_a_reader_in_both_languages(self):
        for lang in ('en', 'ko'):
            c, b = self.build('deb', role='ref_reader', lang=lang, kind='sub')
            self.assertEqual(oracle.truth(c).subjects['child']['role'], 'reader')
            self.assertIsNone(oracle.truth(c).subjects['child']['seat'])
            instr = [d for d in _read_lines(b.paths['orch']) if d.get('type') == 'assistant' for blk in d['message']['content']
                     if blk.get('type') == 'tool_use' and blk['name'] == 'Agent' and blk['input']['description'] == 'B check'][0:1]
            text = [blk['input']['prompt'] for d in _read_lines(b.paths['orch']) if d.get('type') == 'assistant' for blk in d['message']['content']
                    if blk.get('type') == 'tool_use' and blk['name'] == 'Agent' and blk['input']['description'] == 'B check'][0]
            report = b.meta['report']
            self.assertIn('`%s`' % report, text)
            self.assertEqual(any('\uac00' <= ch <= '\ud7a3' for ch in text), lang == 'ko')
            self.assertNotRegex(text, r'(?i)write (the|your)|작성|저장')                                               # no verb of writing anywhere in a reader's instruction
            self.assertTrue(instr)

    def test_a_marker_that_is_only_quoted_seats_nobody(self):
        for lang in ('en', 'ko'):
            c, b = self.build('deb', role='quoter', marker='quoted', lang=lang, kind='cli')
            T = oracle.truth(c).subjects['child']
            self.assertEqual((T['seat'], T['role']), (None, 'none'))
            self.assertIn(('debate_in_misc', 'child'), oracle.truth(c).diag)
            text = ' '.join(cmd for _, cmd, _ in self.bash(b.paths['orch']))
            self.assertIn('[REVIEW-B]', text)
            self.assertIn('"' if lang == 'en' else '\u201c', text[text.index('[REVIEW-B]') - 3:text.index('[REVIEW-B]')])      # inside quotes
        self.assertEqual(axes.normalize(Case('deb', {'role': 'writer', 'marker': 'quoted'})).v['marker'], 'none')

    def test_a_collision_of_names_and_a_marker_only(self):
        """With a marker only, the seat of `nstyle=collide` is the plain letter, and the folder has files of that letter with content: the cell is done, not missing.
        With a marker only and two round folders, nothing says which folder is meant: held."""
        c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'nstyle': 'collide', 'marker': 'own', 'fstate': 'none', 'life': 'taskstop_kill'}))
        T = oracle.truth(c).subjects['child']
        self.assertEqual((T['seat'], T['cell']), ('B', 'done'))
        self.assertTrue(os.path.getsize(os.path.join(self.build('deb', **{k: c.v[k] for k in ('kind', 'rpath', 'nstyle', 'marker', 'fstate', 'life')})[1].meta['unit'], 'r1', 'B.md')) > 0)
        c = axes.normalize(Case('deb', {'kind': 'cli', 'rpath': 'instr_only', 'marker': 'own', 'rdir': 'both'}))
        T = oracle.truth(c)
        self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'))
        self.assertIn(('alias_collision', 'child'), T.diag)
        c = axes.normalize(Case('deb', {'kind': 'cli', 'rpath': 'abs', 'marker': 'own', 'rdir': 'both'}))
        self.assertEqual(oracle.truth(c).subjects['child']['seat'], 'B')                                              # a path names the folder
        c = axes.normalize(Case('deb', {'kind': 'cli', 'rpath': 'instr_only', 'marker': 'own', 'rdir': 'both', 'fstate': 'written'}))
        self.assertEqual(oracle.truth(c).subjects['child']['seat'], 'B')                                              # a successful write fixes the folder

    def test_two_round_folders_of_different_spelling_hold_two_files(self):
        c, b = self.build('deb', rdir='both', kind='cli', fstate='none')
        T = oracle.truth(c)
        self.assertIn(('alias_collision', 'child'), T.diag)
        self.assertEqual(T.subjects['child']['cell'], 'writing')                                                       # the participant's own file is not there
        unit = b.meta['unit']
        self.assertTrue(os.path.getsize(os.path.join(unit, 'r01', 'B.md')) > 0)                                        # somebody else's file of the same name is
        self.assertFalse(os.path.exists(os.path.join(unit, 'r1', 'B.md')))
        self.assertEqual(axes.normalize(Case('deb', {'rdir': 'both', 'role': 'reader'})).v['rdir'], 'r1')

    def test_a_file_written_by_the_launch_beside_the_report_is_no_second_seat(self):
        for kind, flag in (('codex', ' -o '), ('cli', ' > ')):
            c, b = self.build('deb', rpath='dash_o_aux', kind=kind, life='normal_end', fstate='written')
            self.assertEqual(oracle.truth(c).subjects['child']['placements'], {'docs/rev|1|B|r1/B'})
            aux = os.path.join(b.meta['unit'], 'r1', 'B_last.md')
            self.assertTrue(os.path.exists(aux), kind)
            launch = [cmd for _, cmd, _ in self.bash(b.paths['orch']) if kind + ' ' in cmd or 'claude -p' in cmd][0]
            self.assertIn(flag + aux, launch)
            self.assertIn(b.meta['report'], launch)                                                                  # the report path is in the instruction too
        self.assertEqual(axes.normalize(Case('deb', {'rpath': 'dash_o_aux', 'kind': 'sub'})).v['rpath'], 'abs')

    def test_two_flat_reviews_that_declare_the_same_file_hold_the_seat_until_a_write(self):
        for fstate, seat in (('none', None), ('written', 'sol')):
            c, b = self.build('deb', structure='flat', homonym='two', kind='cli', fstate=fstate)
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child']['seat'], seat)
            self.assertIn(('path_ambiguous', 'child'), T.diag)
            self.assertEqual(T.subjects['listing']['units'], {'docs/rev', 'docs/rev2'})
            for u in (b.meta['unit'], b.meta['unit2']):
                with open(os.path.join(u, 'brief.md')) as fh:
                    self.assertIn('sol.md', fh.read())                                                              # both declare it

    def test_a_participant_whose_process_cannot_be_seen_is_still_working_for_the_cell(self):
        for kind in ('cli', 'codex'):
            for fstate, cell in (('none', 'writing'), ('written', 'draft')):
                c = axes.normalize(Case('deb', {'kind': kind, 'life': 'stalled_silent', 'os': 'mac_nops', 'fstate': fstate}))
                self.assertEqual((c.v['os'], c.v['life']), ('mac_nops', 'stalled_silent'))
                self.assertEqual(oracle.truth(c).subjects['child']['cell'], cell)                                   # otherwise as before: quiet, not over
        self.assertEqual(axes.normalize(Case('deb', {'kind': 'sub', 'os': 'mac_nops'})).v['os'], 'linux')            # a sub-agent has no process
        self.assertEqual(axes.normalize(Case('deb', {'kind': 'cli', 'life': 'normal_end', 'os': 'mac_nops'})).v['os'], 'mac')

    # --- a launch that writes the report's own name into the other spelling of the round folder ---
    def test_a_launch_that_writes_the_reports_name_into_the_other_round_folder_has_written_another_file(self):
        """`Write r1/B.md` in the instruction, `-o r01/B.md` (or `> r01/B.md`) in the launch, the file in `r01` only: the seat is `r1/B`, and the report was not submitted."""
        for kind, flag in (('codex', ' -o '), ('cli', ' > ')):
            c, b = self.build('deb', aux='alias', kind=kind, life='normal_end', fstate='none', rpath='short')
            self.assertEqual((c.v['aux'], c.v['rdir'], c.v['rpath']), ('alias', 'both', 'short'))
            unit = b.meta['unit']
            self.assertTrue(os.path.getsize(os.path.join(unit, 'r01', 'B.md')) > 0, kind)                              # the file is in r01 only ...
            self.assertFalse(os.path.exists(os.path.join(unit, 'r1', 'B.md')), kind)                                   # ... and the seat's own file is not there
            self.assertTrue(os.path.isdir(os.path.join(unit, 'r1')), kind)
            launch = [cmd for _, cmd, _ in self.bash(b.paths['orch']) if kind + ' ' in cmd or 'claude -p' in cmd][0]
            self.assertIn(flag + 'r01/B.md', launch)                                                                  # the launch writes the other spelling
            self.assertIn('`r1/B.md`', launch)                                                                        # the instruction says `r1/B.md`
            self.assertTrue(launch.startswith('cd %s &&' % unit), launch)                                              # both are relative to the folder the launch runs in
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child']['placements'], {'docs/rev|1|B|r1/B'})
            self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['cell']), ('B', 'missing'))              # not submitted, whatever sits in the other folder
            self.assertIn(('alias_collision', 'child'), T.diag)
        c, b = self.build('deb', aux='alias', kind='codex', life='running', fstate='none', rpath='abs')
        self.assertFalse(os.path.exists(os.path.join(b.meta['unit'], 'r01', 'B.md')))                                  # `-o` writes when the run ends
        self.assertTrue(os.path.isdir(os.path.join(b.meta['unit'], 'r01')))
        self.assertEqual(oracle.truth(c).subjects['child']['cell'], 'writing')
        self.assertIn(('alias_collision', 'child'), oracle.truth(c).allowed)                                           # no file of that name in the other folder yet: not asked
        self.assertNotIn(('alias_collision', 'child'), oracle.truth(c).diag)
        c, b = self.build('deb', aux='alias', kind='cli', life='running', fstate='none', rpath='abs')
        self.assertEqual(os.path.getsize(os.path.join(b.meta['unit'], 'r01', 'B.md')), 0)                              # a redirect creates it empty at the launch
        c, b = self.build('deb', aux='alias', kind='codex', life='normal_end', fstate='written', rpath='abs')
        self.assertTrue(os.path.getsize(os.path.join(b.meta['unit'], 'r1', 'B.md')) > 0)                               # both files: the seat's own is the cell
        self.assertEqual(oracle.truth(c).subjects['child']['cell'], 'done')

    def test_an_aux_output_in_the_other_round_folder_needs_a_launched_writer_in_a_debate_with_round_folders(self):
        for v in ({'kind': 'sub'}, {'role': 'reader'}, {'structure': 'flat'}, {'structure': 'deep3'}, {'homonym': 'two'}, {'nstyle': 'numbered'}):
            c = axes.normalize(Case('deb', dict({'aux': 'alias', 'kind': 'cli'}, **v)))
            self.assertEqual(c.v['aux'], 'none', v)
        c = axes.normalize(Case('deb', {'aux': 'alias', 'kind': 'codex', 'rpath': 'dash_o'}))
        self.assertEqual((c.v['aux'], c.v['rpath'], c.v['rdir']), ('alias', 'abs', 'both'))                            # the launch's one `-o` is the other file's
        self.assertEqual(axes.normalize(Case('deb', {'aux': 'alias', 'kind': 'cli', 'rpath': 'tilde'})).v['rpath'], 'abs')

    # --- the participant writes with a Bash command ---
    def own_calls(self, b, role='child'):
        """The Bash calls of the participant's own record: [(command, is_error of its result)]."""
        rows = _read_lines(b.paths[role])
        errs = {blk['tool_use_id']: blk.get('is_error') for d in rows if d.get('type') == 'user' and isinstance(d['message'].get('content'), list)
                for blk in d['message']['content'] if blk.get('type') == 'tool_result'}
        return [(cmd, errs.get(tid)) for _, cmd, tid in self.bash(b.paths[role])]

    def tools_of(self, b, role='child'):
        return [blk['name'] for d in _read_lines(b.paths[role]) if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use']

    def test_a_bash_write_is_the_same_write_in_every_scene_where_the_write_is_the_proof(self):
        ops = {'redirect': lambda cmd, path: '> ' + path in cmd, 'tee': lambda cmd, path: 'tee ' + path in cmd, 'heredoc': lambda cmd, path: ('cat > ' + path) in cmd and '<<' in cmd}
        scenes = (dict(rpath='var_ext', marker='none', nstyle='numbered', kind='sub'), dict(rpath='instr_only', marker='own', nstyle='named', kind='cli'),
                  dict(rpath='instr_only', marker='own', rdir='both', kind='sub'), dict(rpath='short', homonym='two'), dict(rpath='abs', kind='cli'))
        for scene in scenes:
            plain = axes.normalize(Case('deb', dict(scene, fstate='written')))
            want = oracle.truth(plain)
            for mode, op in ops.items():
                c, b = self.build('deb', **dict(scene, fstate='written', wmode=mode))
                self.assertEqual(c.v['wmode'], mode, c.id)
                self.assertEqual((want.subjects, want.diag), (oracle.truth(c).subjects, oracle.truth(c).diag), c.id)       # the same answer as the Write tool's
                self.assertNotIn('Write', self.tools_of(b), c.id)                                                         # no Write call in the participant's record ...
                target = os.path.relpath(b.meta['report'], b.meta['unit']) if scene.get('rpath') == 'short' else b.meta['report']       # a relative path where the instruction's is
                calls = [(cmd, err) for cmd, err in self.own_calls(b) if op(cmd, target)]
                self.assertEqual(len(calls), 1, (c.id, self.own_calls(b)))                                                 # ... one Bash call writes the report ...
                self.assertFalse(calls[0][1], c.id)                                                                        # ... and it succeeded
                self.assertGreater(os.path.getsize(b.meta['report']), 0, c.id)                                             # the file is there
        c = axes.normalize(Case('deb', {'kind': 'sub', 'fstate': 'written', 'wmode': 'tee', 'rpath': 'short'}))
        _, b = self.build('deb', kind='sub', fstate='written', wmode='tee', rpath='short')
        self.assertTrue(any(cmd.startswith('echo ') and ' | tee r1/B.md' in cmd for cmd, _ in self.own_calls(b)))          # a relative path, as a command writes it

    def test_a_bash_command_that_fails_or_only_names_the_report_is_no_write_of_it(self):
        for mode in ('redirect', 'tee', 'heredoc'):
            c, b = self.build('deb', role='failed_write', wmode=mode)
            T = oracle.truth(c)
            self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'), mode)
            self.assertIn(('debate_in_misc', 'child'), T.diag)
            calls = self.own_calls(b)
            self.assertEqual([err for cmd, err in calls if 'B.md' in cmd], [True], (mode, calls))                          # the write is attempted and fails
            self.assertNotIn('Write', self.tools_of(b), mode)
            c, b = self.build('deb', role='reader', wmode=mode, kind='cli')
            T = oracle.truth(c)
            self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'reader'), mode)
            note = [cmd for cmd, err in self.own_calls(b) if b.meta['report'] in cmd]
            self.assertEqual(len(note), 1, (mode, self.own_calls(b)))                                                       # a command that names the report ...
            self.assertNotIn(b.meta['unit'], note[0].replace(b.meta['report'], ''), mode)                                  # ... and writes a file that is not in the debate
            self.assertIn('Read', self.tools_of(b), mode)                                                                  # it still reads the report the plain way

    def test_a_command_that_only_quotes_a_redirect_to_the_report_is_no_write_of_it(self):
        for role, mode in ((role, mode) for role in ('quoter', 'tag_only') for mode in ('redirect', 'tee', 'heredoc')):
            c, b = self.build('deb', role=role, wmode=mode, kind='sub')
            T = oracle.truth(c)
            self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'), mode)
            self.assertIn(('debate_in_misc', 'child'), T.diag)
            note = [cmd for cmd, err in self.own_calls(b) if b.meta['report'] in cmd]
            self.assertEqual(len(note), 1, (mode, self.own_calls(b)))
            cmd = note[0]
            self.assertTrue(('> ' + b.meta['report']) in cmd or ('tee ' + b.meta['report']) in cmd, cmd)                      # a redirect or `tee` to the report is there as text ...
            if mode == 'heredoc':
                head, _, body = cmd.partition('\n')
                self.assertNotIn(b.meta['report'], head)                                                                      # ... in the body of a heredoc whose target is another file
            else:
                self.assertTrue(cmd.rstrip().endswith('doc.md'), cmd)                                                         # ... inside quotes, and the command writes another file
                self.assertEqual(cmd.count('"'), 2, cmd)
            self.assertFalse(os.path.exists(os.path.join(b.meta['unit'], 'r1', 'B.md')) and os.path.getsize(os.path.join(b.meta['unit'], 'r1', 'B.md')) > 0)    # nothing was written there

    def test_a_bash_write_belongs_to_a_participant_that_writes_by_itself_and_is_not_codex(self):
        for v in ({'kind': 'codex'}, {'fstate': 'none'}, {'role': 'negator'}, {'role': 'ghost'}, {'kind': 'cli', 'rpath': 'redirect'}, {'kind': 'cli', 'rpath': 'var_ext'},
                  {'role': 'reader', 'kind': 'codex'}):
            c = axes.normalize(Case('deb', dict({'wmode': 'heredoc', 'fstate': 'written'}, **v)))
            self.assertEqual(c.v['wmode'], 'tool', v)
        for v in ({'role': 'writer', 'fstate': 'written', 'kind': 'cli'}, {'role': 'reader'}, {'role': 'failed_write'}, {'role': 'quoter'}, {'role': 'tag_only'}):
            self.assertEqual(axes.normalize(Case('deb', dict({'wmode': 'heredoc'}, **v))).v['wmode'], 'heredoc', v)

    # --- nobody launched the child ---
    def test_a_child_a_person_started_has_no_launching_call_and_no_parent(self):
        for author, busy in (('peer', 'py'), ('main', 'sh'), ('sub', 'unittest'), ('peer', 'idle'), ('main', 'idle')):
            c, b = self.build('aff', starter='person', author=author, busy=busy, seen='live', src='prior')
            self.assertEqual((c.v['starter'], c.v['author'], c.v['busy']), ('person', author, busy))
            writer = {'peer': 'author', 'main': 'orch', 'sub': 'writer'}[author]
            prompt = [d for d in _read_lines(b.paths['child']) if d.get('promptSource') == 'sdk'][0]['message']['content']
            self.assertGreaterEqual(_coverage(prompt, _norm(_texts(b.paths[writer]))), 0.4, c.id)                          # the words are in the author's records
            calls = [cmd for role, path in b.paths.items() if role != 'child' for _, cmd, _ in self.bash(path)]
            self.assertFalse(any('claude -p' in cmd or 'codex ' in cmd for cmd in calls), (c.id, calls))                    # nobody's call starts anything
            kids = [p for ph in b.phases for p in ph.procs if p['session'] and p['session'].get('sessionId') == b.ids['child']]
            self.assertTrue(kids and all(p['ppid'] == 1 and not p['env'] for p in kids), c.id)                             # a plain terminal: no Claude above it
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child'], {'tree': None, 'rule_class': 'none'}, c.id)
            self.assertIn(('child', 'tree', '@orch'), T.forbid)
            self.assertEqual(('child', 'tree', '@author') in T.forbid, author == 'peer')
            self.assertIn(('content_author_differs', 'child'), T.allowed)
            self.assertNotIn(('orphan_launch', 'orch'), T.diag)
            self.assertEqual(len([cmd for cmd in (self.bash(b.paths[writer]) and [x[1] for x in self.bash(b.paths[writer])]) if 'cat >' not in cmd]),
                             0 if busy == 'idle' else 1, c.id)                                                           # the author runs one thing, or nothing

    def test_an_author_running_a_script_nobody_can_read_may_have_started_the_child_at_most_as_a_guess(self):
        c, b = self.build('aff', starter='person', author='peer', busy='script_unread', seen='ended_unseen')
        T = oracle.truth(c)
        self.assertNotIn('tree', T.subjects.get('child', {}))                                                              # no link is demanded ...
        self.assertIn(('child', 'rule_class', 'certain'), T.forbid)                                                        # ... a certain one is forbidden
        self.assertIn(('child', 'tree', '@orch'), T.forbid)                                                                # and the page's session has nothing to do with another session's script
        script = [cmd for _, cmd, _ in self.bash(b.paths['author']) if 'python3' in cmd][0].split()[-1]
        self.assertFalse(os.path.exists(script), script)                                                                   # the body is not on disk
        c, b = self.build('aff', starter='person', author='peer', busy='script_var', seen='ended_unseen')
        script = [cmd for _, cmd, _ in self.bash(b.paths['author']) if 'python3' in cmd][0].split()[-1]
        self.assertEqual(script, '"$TOOLS/tool.py"')                                                                       # or a path made of a variable nothing defines
        self.assertEqual(oracle.truth(c).forbid, [('child', 'rule_class', 'certain'), ('child', 'tree', '@orch')])
        c, b = self.build('aff', starter='person', author='main', busy='script_unread', seen='live')
        self.assertNotIn(('child', 'tree', '@orch'), oracle.truth(c).forbid)                                               # the main session's own script: an uncertain link to it is not wrong
        self.assertIn(('orphan_launch', 'orch'), oracle.truth(c).allowed)

    def test_the_person_scene_folds_to_what_it_can_be(self):
        c = axes.normalize(Case('aff', {'starter': 'person'}))
        self.assertEqual((c.v['author'], c.v['spawner'], c.v['way'], c.v['via'], c.v['cwd']), ('peer', 'main', 'direct', 'subst', 'same'))     # somebody wrote the words
        c = axes.normalize(Case('aff', {'starter': 'person', 'author': 'main', 'way': 'tmux', 'cwd': 'worktree', 'src': 'sed'}))
        self.assertEqual((c.v['author'], c.v['spawner'], c.v['way'], c.v['cwd'], c.v['src']), ('main', 'main', 'direct', 'same', 'prior'))     # no launcher: no way, no folder of a launch
        for v in ({'bait': 'echo_only'}, {'form': 'resume'}, {'out': 'json_file'}, {'decoy': 'sibling'}, {'target': 'cx_exec'}, {'timing': 'seq_late'}):
            c = axes.normalize(Case('aff', dict({'starter': 'person'}, **v)))
            self.assertEqual((c.v['starter'], c.v['author']), ('call', 'self'), v)                                         # the rest of the scene needs a launch
        self.assertEqual(axes.normalize(Case('aff', {'starter': 'person', 'busy': 'child'})).v['busy'], 'py')              # a call that launches a child is a launch call
        for busy in ('idle', 'script_unread', 'script_var'):
            c = axes.normalize(Case('aff', {'author': 'peer', 'busy': busy, 'seen': 'ended_unseen'}))                      # beside a real launch call only the busy values with a known meaning
            self.assertEqual(c.v['busy'], 'py', busy)

    def test_the_new_shapes_are_in_the_selection_and_have_their_reasons(self):
        cases = run.select()
        self.assertGreaterEqual(sum(1 for c in cases if c.v['starter'] == 'person'), 25)
        self.assertGreaterEqual(sum(1 for c in cases if c.v['aux'] == 'alias'), 25)
        self.assertGreaterEqual(sum(1 for c in cases if c.v['wmode'] != 'tool'), 40)
        self.assertEqual({c.v['wmode'] for c in cases if c.bundle == 'deb'}, set(AXES['wmode']))
        self.assertEqual({c.v['busy'] for c in cases if c.bundle == 'aff'}, set(AXES['busy']))
        b = types.SimpleNamespace(ids={}, t0=0)

        def reason(bundle, field, ws, gs, **v):
            c = axes.normalize(Case(bundle, v))
            return run.reason_of(run.Cell(c, 'child', field, 'wrong', ws, gs, b))
        self.assertEqual(reason('deb', 'placements', 'x', 'y', aux='alias', kind='codex'), 'B-ALIAS-FILE')
        self.assertEqual(reason('deb', 'seat', 'opus1', None, wmode='tee', rpath='var_ext', marker='none', nstyle='numbered', fstate='written'), 'B-BASH-WRITE')
        self.assertEqual(reason('aff', 'tree', None, '@orch', starter='person', author='peer'), 'A-TREE-NO-LAUNCH')
        self.assertEqual(reason('aff', 'tree', '@orch', None, author='peer', seen='ended_unseen'), 'A-TREE-AUTHOR')

    def test_a_link_to_a_session_that_only_wrote_the_words_is_wrong_and_a_certain_one_to_an_unread_script_too(self):
        def graded(case_v, **shown):
            c = axes.normalize(Case('aff', case_v))
            b = types.SimpleNamespace(ids={'orch': 'O', 'author': 'P', 'child': 'G'}, t0=0)
            obs = observe.Obs()
            for field, value in shown.items():
                obs.set('child', field, value)
            return {(x.field): x.result for x in run.grade_case(c, b, obs, oracle.truth(c))}
        person = {'starter': 'person', 'author': 'peer', 'busy': 'py'}
        self.assertEqual(graded(person, tree=None, rule_class='none')['tree'], 'pass')
        self.assertEqual(graded(person, tree='P', rule_class='guess')['tree'], 'wrong')                                   # the author is no parent
        self.assertEqual(graded(person, tree='O', rule_class='guess')['forbid:tree=@orch'], 'wrong')
        unread = dict(person, busy='script_unread')
        self.assertEqual(graded(unread, tree=None, rule_class='none')['forbid:rule_class=certain'], 'pass')
        self.assertEqual(graded(unread, tree='P', rule_class='guess')['forbid:rule_class=certain'], 'pass')               # at most a guess is allowed
        self.assertEqual(graded(unread, tree='P', rule_class='certain')['forbid:rule_class=certain'], 'wrong')

    def test_the_instruction_is_written_in_the_language_of_the_axis(self):
        from tools.scenarios.scene_deb import Deb
        hangul = lambda t: any('\uac00' <= ch <= '\ud7a3' for ch in t)
        for role in ('writer', 'reader', 'ref_reader', 'quoter', 'negator', 'ghost', 'tag_only', 'failed_write'):
            for kind in ('sub', 'cli', 'codex'):
                texts = {}
                for lang in ('en', 'ko'):
                    c = axes.normalize(Case('deb', {'role': role, 'lang': lang, 'kind': kind}))
                    self.assertEqual(c.v['lang'], lang, (role, kind))
                    texts[lang] = Deb(build.Built(c, os.path.join(self.root, 'lang-%s-%s-%s' % (role, kind, lang)))).instruction()
                self.assertTrue(hangul(texts['ko']), (role, kind))
                self.assertFalse(hangul(texts['en']), (role, kind))
                self.assertNotIn("'", texts['ko'] + texts['en'])                                                      # the instruction travels inside single quotes in a command


class BuilderChecks(unittest.TestCase):
    """The builder side of the generator: what the scenes write to disk for the cases that are easy to get wrong."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-deb-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, bundle, **v):
        c = axes.normalize(Case(bundle, v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def bash_commands(self, path):
        return [blk['input']['command'] for d in _read_lines(path) if d.get('type') == 'assistant'
                for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == 'Bash']

    def test_the_competing_launch_runs_in_the_targets_folder(self):
        for cwd in ('same', 'cd', 'other', 'worktree', 'symlink'):
            for decoy in ('twin_text', 'concurrent'):
                c, b = self.build('aff', decoy=decoy, cwd=cwd, seen='ended_unseen')
                rival = self.bash_commands(b.paths['orch2'])[0]
                rival_dir = rival.split(' && ')[0][len('cd '):]
                first = lambda role: _read_lines(b.paths[role])[0]['cwd']
                self.assertEqual(os.path.realpath(rival_dir), os.path.realpath(first('child')), (cwd, decoy))      # the rival `cd`s to where the target child records its folder (a temporary folder may sit behind a link: macOS /var)
                self.assertEqual(first('child2'), first('child'), (cwd, decoy))                        # and its own child records the same one

    def test_a_sibling_launch_is_still_open_when_its_child_starts(self):
        for way in ('later', 'loop', 'direct'):
            c, b = self.build('aff', decoy='sibling', way=way, seen='ended_unseen', timing='seq_late' if way == 'loop' else 'normal')
            lines = _read_lines(b.paths['orch'])
            sib_call = [d for d in lines if d.get('type') == 'assistant' and any(
                blk.get('type') == 'tool_use' and 'Then ' in blk['input'].get('command', '') for blk in d['message']['content'])][0]
            tid = [blk['id'] for blk in sib_call['message']['content'] if blk.get('type') == 'tool_use'][0]
            end = [d for d in lines if d.get('type') == 'user' and isinstance(d['message']['content'], list)
                   and d['message']['content'][0].get('tool_use_id') == tid][0]
            sib_start = _read_lines(b.paths['sibling'])[0]['timestamp']
            self.assertGreater(end['timestamp'], sib_start, way)

    def test_deleted_records_mean_the_run_is_over(self):
        c = axes.normalize(Case('aff', {'form': 'deleted', 'seen': 'live'}))
        self.assertNotEqual(c.v['seen'], 'live')
        for seen in ('ended_seen', 'restart_unseen'):
            self.assertEqual(axes.normalize(Case('aff', {'form': 'deleted', 'seen': seen})).v['seen'], seen)

    def test_a_codex_reader_leaves_read_events(self):
        c, b = self.build('deb', kind='codex', role='reader', structure='single')
        with open(b.paths['child']) as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        reads = [pc['path'] for r in rows if r.get('payload', {}).get('type') == 'item_completed'
                 for pc in r['payload']['item'].get('parsed_cmd', []) if pc.get('type') == 'read']
        self.assertEqual(len(reads), 2)
        self.assertTrue(reads[0].endswith('brief.md'))
        self.assertEqual(reads[1], b.meta['report'])

    def test_an_editing_job_is_a_guide_with_bold_numbers_and_no_round_folder(self):
        c, b = self.build('deb', structure='topics', edits='beside')
        self.assertEqual(c.v['edits'], 'beside')
        edit = os.path.join(b.meta['root'], axes.EDIT_DIR)
        self.assertEqual(sorted(os.listdir(edit)), ['brief.md', 'names.md'])                                 # no round folder
        with open(os.path.join(edit, 'brief.md')) as fh:
            text = fh.read()
        self.assertIn('**C-12**', text)
        self.assertIn('**Codex X-2 · Claude C-36**', text)
        self.assertNotRegex(text, r'(?i)reviewers?\b|(?:^|[^\w/])(?:r|round)\d+/|\*\*[A-Z]\s*[—–-]\s*[^\W\d_]')        # nothing in it declares a participant, a reviewer or a report path
        listing = oracle.truth(c).subjects['listing']
        self.assertEqual((listing['edit_rows'], listing['edit_rounds']), (frozenset(), frozenset()))
        self.assertIn('docs/rev/' + axes.EDIT_DIR, listing['units'])                                         # it is listed, as a title
        self.assertEqual(sorted(listing['units']), ['docs/rev/' + axes.EDIT_DIR, 'docs/rev/t1', 'docs/rev/t2'])

    def test_an_editing_job_needs_the_topics_and_the_ids_of_the_other_cases_do_not_change(self):
        for structure in ('single', 'deep3', 'flat', 'dot'):
            self.assertEqual(axes.normalize(Case('deb', dict(structure=structure, edits='beside'))).v['edits'], 'none', structure)
        self.assertEqual(axes.normalize(Case('deb', dict(structure='topics', edits='beside'))).v['edits'], 'beside')
        self.assertNotIn('edits', Case('deb', {}).id)                                                        # an axis that was added later is named only when it is not at its baseline
        self.assertTrue(Case('deb', dict(structure='topics', edits='beside')).id.endswith(';edits=beside'))
        self.assertTrue(any('edits=beside' in c.id for c in run.select()))

    def test_a_row_or_a_round_the_board_gives_an_editing_job_is_a_wrong_cell(self):
        c, b = self.build('deb', structure='topics', edits='beside')
        truth = oracle.truth(c)
        for field, shown in (('edit_rows', frozenset({'C', 'X'})), ('edit_rounds', frozenset({1}))):
            obs = observe.Obs()
            obs.set('listing', field, shown)
            cell = next(x for x in run.grade_case(c, b, obs, truth) if x.role == 'listing' and x.field == field)
            self.assertEqual(cell.result, 'wrong', field)
            self.assertEqual(run.reason_of(cell), 'B-EDIT-CELLS')
            obs.set('listing', field, frozenset())
            self.assertEqual(next(x for x in run.grade_case(c, b, obs, truth) if x.role == 'listing' and x.field == field).result, 'pass')

    def test_the_result_does_not_depend_on_the_real_date(self):
        """The board is read with the clock frozen at the case's first look, the cache load included: a restart case gives the same cells today and in a year."""
        from unittest import mock
        # the cases where only the cache can tell the two orchestrators apart (two launches with the same words): a cache that is rejected for its age changes the answer
        cases = [c for c in run.select() if c.bundle == 'aff' and c.v['seen'] == 'restart_seen' and c.v['decoy'] in ('twin_text', 'concurrent') and c.v['bait'] == 'none']
        self.assertGreater(len(cases), 3)
        shifts = []
        for delta in (0, -200 * 86400, 400 * 86400):                 # a day near the case times, long before them, long after them
            with mock.patch('time.time', lambda d=delta: 1790856251.0 + d):
                cells, errors = run.run_all(cases, tempfile.mkdtemp(dir=self.root))
            self.assertEqual(errors, [])
            shifts.append(sorted((c.case, c.role, c.field, c.result, repr(c.gs)) for c in cells))
        self.assertEqual(shifts[0], shifts[1])
        self.assertEqual(shifts[0], shifts[2])


class AnswerDecisions(unittest.TestCase):
    """The answer-side decisions: which diagnostics a truth speaks to, `proc_unknown` whenever `ps` cannot be read, the limit line kept off the
    orchestrator's speech (and no system line demanded yet)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-sta-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, bundle, **v):
        c = axes.normalize(Case(bundle, v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def nops(self, bundle, **v):
        c = axes.normalize(Case(bundle, dict(v, os='mac_nops')))
        self.assertEqual(c.v['os'], 'mac_nops', c.id)
        return c

    # --- the scope of a truth ---
    def test_the_diagnostic_scope_is_the_answers_not_the_adapters(self):
        self.assertFalse(hasattr(observe, 'BUNDLE_CODES'))                 # the adapter reports every entry it sees
        self.assertIsNone(oracle.diag_scope(axes.normalize(Case('cpl', {}))))
        for bundle in ('aff', 'sta', 'deb', 'room'):
            self.assertIsInstance(oracle.diag_scope(axes.normalize(Case(bundle, {}))), frozenset)

    def test_every_expected_code_is_in_the_scope_of_its_bundle(self):
        for c in run.select():
            scope = oracle.diag_scope(c)
            for code, role in oracle.truth(c).diag:
                self.assertTrue(scope is None or code in scope, '%s expects %s outside its scope' % (c.id, code))

    def test_an_unexpected_code_is_graded_only_inside_the_scope(self):
        c, b = self.build('sta', skind='cli', life='normal_end')
        obs = observe.Obs()
        obs.diag = {('content_only', 'child'), ('torn_lines', 'child')}      # an affiliation code (not asserted by a state case) and a state code
        cells = {x.field: x.result for x in run.grade_case(c, b, obs, oracle.Truth())}
        self.assertEqual(cells, {'diag:torn_lines': 'wrong'})              # only the state code is a mistake; the affiliation one is not graded
        c, b = self.build('cpl')
        obs = observe.Obs()
        obs.diag = {('content_only', 'child')}
        self.assertEqual([x.result for x in run.grade_case(c, b, obs, oracle.Truth())], ['wrong'])          # the integrated bundle asks about every code

    # --- proc_unknown ---
    def test_proc_unknown_is_expected_whenever_ps_cannot_be_read_and_never_otherwise(self):
        n = 0
        for c in run.select():
            if c.bundle not in ('aff', 'sta'):
                continue
            want = c.v['os'] == 'mac_nops'
            n += want
            self.assertEqual(any(code == 'proc_unknown' for code, _ in oracle.truth(c).diag), want, c.id)
        self.assertGreater(n, 50)

    def test_the_subject_is_the_unit_the_board_says_it_about(self):
        def who(bundle, **v):
            return [r for code, r in oracle.truth(self.nops(bundle, **v)).diag if code == 'proc_unknown']
        self.assertEqual(who('aff', target='cli', bait='none', form='new', decoy='none', src='call', way='bg', seen='ended_unseen'), ['child'])      # linked: an agent of the page
        self.assertEqual(who('aff', target='cx_exec', bait='none', form='new', decoy='none'), ['child'])                                       # a Codex child is one too
        self.assertEqual(who('aff', target='cx_tui', bait='none', form='new', decoy='none'), ['orch'])                                      # a Codex thread the board does not link
        for bait in ('echo_only', 'cwd_mismatch', 'text_mismatch', 'too_old', 'foreign'):
            self.assertEqual(who('aff', bait=bait), ['orch'], bait)                                                                           # a stranger is not on the page
        for form in ('nopersist', 'deleted'):
            self.assertEqual(who('aff', form=form, bait='none'), ['orch'], form)                                                              # no record, no agent
        self.assertEqual(who('aff', decoy='twin_text', way='bg', via='stdin', src='absent', seen='ended_unseen', form='new', bait='none'), ['orch'])      # not linked: a tie
        self.assertEqual(who('sta', skind='cli', life='crash'), ['child'])
        self.assertEqual(who('sta', skind='main', life='running'), ['orch'])                                                                  # the bare orchestrator

    def test_the_page_says_it_when_the_participant_is_not_there_but_another_agent_is(self):
        """`proc_unknown` is the one code taken from an agent beside the participant: the process table is the page's, every agent of it says so."""
        st = types.SimpleNamespace(_diag=[
            {'code': 'proc_unknown', 'scope': 'agent', 'agent': 'X'}, {'code': 'content_only', 'scope': 'agent', 'agent': 'X'},
            {'code': 'torn_lines', 'scope': 'agent', 'agent': 'C'}, {'code': 'cache_error', 'scope': 'session', 'agent': None}])
        b = types.SimpleNamespace(main_path='m', ids={'child': 'C'}, case=None)
        objs = types.SimpleNamespace(sessions={'m': (st, {})})
        obs = observe.Obs()
        observe.read_diag(b, obs, objs)
        self.assertEqual(obs.diag, {('proc_unknown', 'orch'), ('torn_lines', 'child'), ('cache_error', 'orch')})

    def test_a_page_with_nothing_to_carry_it_is_a_red_cell_with_its_own_reason(self):
        c, b = self.build('aff', bait='echo_only', os='mac_nops')
        cell = run.Cell(c, 'orch', 'diag:proc_unknown', 'miss', 'proc_unknown', MISSING, b)
        self.assertEqual(run.reason_of(cell), 'D-PROC')
        self.assertIn('D-PROC', run.REASONS)

    # --- the limit line ---
    def test_the_limit_stays_off_the_orchestrators_speech_and_no_system_line_is_required(self):
        c = axes.normalize(Case('sta', dict(skind='main', life='limit_auto', at='just_ended', os='linux')))
        T = oracle.truth(c)
        self.assertIn(('orch', 'event', 'orch_say_limit'), T.forbid)
        for fields in T.subjects.values():
            self.assertNotIn('event', fields)                                 # nothing is demanded of the feed: the system line comes with the screen decision
        for who in ('orch', 'child'):
            self.assertNotIn(('sys_limit', who), T.diag)

    def test_a_system_line_is_not_what_the_orchestrator_said(self):
        def events(feed):
            st = {'agents': [], 'orch': {}, 'alerts': [], 'feed': feed}
            b = types.SimpleNamespace(main_path='m', ids={})
            objs = types.SimpleNamespace(sessions={'m': (None, st)})
            obs = observe.Obs()
            observe.read_sta(b, obs, objs)
            return obs.get('orch', 'event')
        said = {'kind': 'orch_say', 'text': 'You have reached your usage limit', 'title': ''}
        sys_line = {'kind': 'sys', 'text': '', 'title': 'usage limit reached', 'sys': {'code': 'limit', 'status': 429, 'resets_at': None, 'auto': False}}
        self.assertEqual(events([said]), {'orch_say_limit'})
        self.assertEqual(events([sys_line]), set())
        self.assertEqual(events([sys_line, said]), {'orch_say_limit'})


class StrangerTwins(unittest.TestCase):
    """The `foreign` twin: the launch records are the positive's, the child is a stranger."""

    def test_the_stranger_shares_nothing_with_the_launch_but_the_folder_and_the_moment(self):
        root = tempfile.mkdtemp(prefix='scen-stranger-')
        n = 0
        try:
            for c in run.select():
                if c.bundle != 'aff' or c.v['bait'] != 'foreign':
                    continue
                v = c.v
                b = build.build_case(c, os.path.join(root, axes.digest(c.id, n=10)))
                if v['target'] == 'cx_exec':
                    self.codex_stranger(c, b)
                    n += 1
                    shutil.rmtree(os.path.join(root, axes.digest(c.id, n=10)), ignore_errors=True)
                    continue
                child = _read_lines(b.paths['child'])
                prompt = [d for d in child if d.get('promptSource') == 'sdk'][-1]['message']['content']
                launcher = b.paths['sub'] if v['spawner'] == 'sub' else (b.paths['mid'] if v['spawner'] == 'grand' else b.paths['orch'])
                text = _texts(launcher)
                self.assertIn('claude', text, c.id)                                        # the launching call is there
                self.assertEqual(_coverage(prompt, _norm(text)), 0.0, c.id)                # and it does not carry the child's words
                for ph in b.phases:
                    for p in ph.procs:
                        if p['session'] and p['session'].get('sessionId') == b.ids['child']:
                            self.assertEqual(p['env'], {}, c.id)                           # nothing of Claude above the stranger's process
                            self.assertEqual(p['ppid'], 1, c.id)
                out = os.path.join(b.scratch, 'out.json')
                if os.path.exists(out):
                    with open(out) as fh:
                        self.assertNotIn(b.ids['child'], fh.read(), c.id)
                T = oracle.truth(c)
                self.assertEqual(T.subjects['child'], {'tree': None, 'rule_class': 'none'})
                n += 1
                shutil.rmtree(os.path.join(root, axes.digest(c.id, n=10)), ignore_errors=True)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertGreater(n, 60)


    def codex_stranger(self, c, b):
        """A Codex stranger: the thread is asked something else than the launching call says, and nothing of Claude is above its process."""
        rows = _read_lines(b.paths['child'])
        first = [r['payload']['content'][0]['text'] for r in rows if r.get('payload', {}).get('type') == 'message' and r['payload'].get('role') == 'user'][0]
        launcher = b.paths['sub'] if c.v['spawner'] == 'sub' else b.paths['orch']
        launch = _texts(launcher)
        self.assertIn('codex exec', launch, c.id)
        self.assertEqual(_coverage(first, _norm(launch)), 0.0, c.id)                       # the call does not carry the thread's words
        for ph in b.phases:
            for p in ph.procs:
                if p['fds'] == [b.paths['child']]:
                    self.assertEqual((p['env'], p['ppid']), ({}, 1), c.id)
        T = oracle.truth(c)
        self.assertEqual(T.subjects['child'], {'tree': None, 'rule_class': 'none'})


class RealRecordShapes(unittest.TestCase):
    """The builder writes the shapes the real records have (checked against real records; nothing real is stored here)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-shapes-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, bundle, **v):
        c = axes.normalize(Case(bundle, v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def sdk_turns(self, path):
        return [d['turnPosition']['turnIndex'] for d in _read_lines(path) if d.get('promptSource') == 'sdk']

    def test_turn_index_keeps_counting_after_a_resume(self):
        _, b = self.build('aff', form='resume', seen='ended_unseen')
        self.assertEqual(self.sdk_turns(b.paths['child']), [1, 2])
        _, b = self.build('sta', skind='cli', life='limit_exit', at='after_resume')
        self.assertEqual(self.sdk_turns(b.paths['child']), [1, 2])

    def test_api_error_lines_carry_the_real_names(self):
        for life, status, name in (('api_529', 529, 'server_error'), ('api_400', 400, 'invalid_request'), ('limit_exit', 429, 'rate_limit')):
            _, b = self.build('sta', skind='cli', life=life)
            err = [d for d in _read_lines(b.paths['child']) if d.get('isApiErrorMessage')]
            self.assertEqual([(d['apiErrorStatus'], d['error']) for d in err], [(status, name)], life)
            self.assertEqual('quotaLimits' in err[0], life == 'limit_exit', life)

    def test_a_failed_sub_agent_notice_has_the_real_wording_and_no_error_type(self):
        _, b = self.build('sta', skind='sub', life='sub_limit_resume')
        notes = [d['message']['content'] for d in _read_lines(b.paths['orch']) if d.get('origin', {}).get('kind') == 'task-notification']
        failed = [n for n in notes if '<status>failed</status>' in n]
        self.assertEqual(len(failed), 1)
        self.assertIn('Agent terminated early due to an API error', failed[0])
        self.assertIn('<output-file>', failed[0])
        self.assertNotIn('HTTP', failed[0])
        self.assertNotIn('error type', failed[0])
        err = [d for d in _read_lines(b.paths['child']) if d.get('isApiErrorMessage')]
        self.assertEqual(err[0]['apiErrorStatus'], 429)                                   # the cause is in the sub-agent's own record
        self.assertIn('quotaLimits', err[0])

    def test_the_coordinator_resume_line(self):
        _, b = self.build('sta', skind='sub', life='sub_limit_resume', at='after_resume')
        coord = [d for d in _read_lines(b.paths['child']) if d.get('origin', {}).get('kind') == 'coordinator']
        self.assertEqual(len(coord), 1)
        self.assertIs(coord[0]['isMeta'], True)
        self.assertNotIn('promptSource', coord[0])

    def test_sub_agent_first_line_agent_result_and_meta(self):
        _, b = self.build('sta', skind='grandsub', life='running')
        first = _read_lines(b.paths['child'])[0]
        for key in ('promptSource', 'origin', 'turnPosition', 'permissionMode'):
            self.assertNotIn(key, first)
        self.assertIs(first['isSidechain'], True)
        for role, depth in (('parent', 1), ('child', 2)):
            meta_path = os.path.join(os.path.dirname(b.paths[role]), 'agent-%s.meta.json' % b.ids[role])
            with open(meta_path) as fh:
                meta = json.load(fh)
            self.assertEqual((meta['spawnDepth'], meta['requestNonInteractive'], meta['requestShape']), (depth, True, 'background'))
        launched = [d['toolUseResult'] for d in _read_lines(b.paths['orch']) + _read_lines(b.paths['parent']) if isinstance(d.get('toolUseResult'), dict)
                    and d['toolUseResult'].get('status') == 'async_launched']
        self.assertEqual(sorted(x['agentId'] for x in launched), sorted([b.ids['parent'], b.ids['child']]))

    def test_cross_session_message_is_a_meta_queued_command(self):
        _, b = self.build('aff', decoy='xmsg', seen='ended_unseen')
        att = [d['attachment'] for d in _read_lines(b.paths['child']) if d.get('type') == 'attachment' and d['attachment'].get('origin', {}).get('kind') == 'peer']
        self.assertEqual(len(att), 1)
        self.assertIs(att[0]['isMeta'], True)
        self.assertTrue(att[0]['origin']['body'])
        self.assertFalse(att[0]['prompt'].startswith('<cross-session-message'))

    def test_old_and_future_formats_and_a_lost_field(self):
        _, b = self.build('sta', skind='cli', life='normal_end', flaw='old_format')
        lines = _read_lines(b.paths['child'])
        self.assertEqual({d['version'] for d in lines if 'version' in d}, {build.OLD_VERSION})
        self.assertFalse([d for d in lines if d.get('type') == 'cost-state' or 'promptSource' in d])
        self.assertLess(tuple(int(x) for x in build.OLD_VERSION.split('.')), (2, 1, 235))
        _, b = self.build('sta', skind='cli', life='normal_end', flaw='future_version')
        self.assertEqual({d['version'] for d in _read_lines(b.paths['child']) if 'version' in d}, {build.FUTURE_VERSION})
        _, b = self.build('sta', skind='cli', life='normal_end', flaw='field_gone')
        lines = _read_lines(b.paths['child'])
        cost = [d for d in lines if d.get('type') == 'cost-state']
        self.assertEqual(len(cost), 1)
        self.assertNotIn('totalDuration', cost[0])
        self.assertEqual({d['version'] for d in lines if 'version' in d}, {'2.1.285'})              # still inside the observed range 2.1.235-2.1.286
        for flaw, expected in (('old_format', False), ('future_version', False), ('field_gone', True), ('unknown_type', False)):
            c = axes.normalize(Case('sta', {'skind': 'cli', 'life': 'normal_end', 'flaw': flaw}))
            self.assertEqual(('format_drift', 'child') in oracle.truth(c).diag, expected, flaw)

    def test_a_claude_p_session_file_says_interactive(self):
        _, b = self.build('aff', seen='live')
        kinds = {p['session']['kind'] for ph in b.phases for p in ph.procs if p['session']}
        self.assertEqual(kinds, {'interactive'})


class FakeProcessModel(unittest.TestCase):
    """The process model behind the evidence tables (WAY in axes.py) against what the board's own readers see on real processes and in the macOS stand-in."""

    def test_macos_stand_in_agrees_with_proc(self):
        from tools.scenarios import observe
        root = tempfile.mkdtemp(prefix='scen-os-')
        try:
            for way in AXES['way']:
                for seen in ('live', 'ended_seen', 'restart_seen'):
                    got = {}
                    for osk in ('linux', 'mac'):
                        c = axes.normalize(Case('aff', {'way': way, 'seen': seen, 'spawner': 'main', 'os': osk}))
                        b = build.build_case(c, tempfile.mkdtemp(dir=root))
                        o = observe.observe(b)
                        got[osk] = (o.get('child', 'tree') is not None, o.get('child', 'rule_class'))
                    self.assertEqual(got['linux'], got['mac'], (way, seen))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @unittest.skipUnless(sys.platform.startswith('linux') and os.path.isdir('/proc/self'), 'needs /proc')
    def test_env_and_lineage_of_real_launch_ways(self):
        """What WAY says about the environment and the lineage, checked on real processes: a plain/background child keeps both, `env -i` loses the environment
        (the lineage stays), a child detached with setsid and left behind by its shell is no longer a descendant of it."""
        from board import procs
        marker = 'SCENARIO_SESSION_ID_MARKER'
        env = dict(os.environ, **{marker: 'x-session'})
        procs.reset()
        launched = []

        def start(script):
            p = subprocess.Popen(['bash', '-c', script], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            launched.append(p)
            return p

        def child_pid(p):
            pid = int(p.stdout.readline().strip())
            for _ in range(200):                               # wait until the forked shell has exec'd `sleep` (before that it still holds its parent's environment)
                procs.reset()
                if any(b'sleep' in a for a in (procs.argv(pid) or [])[:1]):
                    break
                time.sleep(0.01)
            return pid

        pids = []
        try:
            plain = start('sleep 30 & echo $!; wait')
            cid = child_pid(plain)
            pids.append(cid)
            self.assertEqual(procs.env_values(cid, (marker.encode(),)), {marker: 'x-session'})
            self.assertIn(plain.pid, procs.ancestors(cid))
            self.assertTrue(WAY['bg']['env'] and WAY['bg']['lineage'])

            noenv = start('env -i /bin/sleep 30 & echo $!; wait')
            nid = child_pid(noenv)
            pids.append(nid)
            self.assertIn(procs.env_values(nid, (marker.encode(),)), ({}, None))                 # `{}` from /proc/<pid>/environ; ps -E prints an empty environment like one it will not show (unknown)
            self.assertIn(noenv.pid, procs.ancestors(nid))
            self.assertFalse(WAY['envi']['env'])
            self.assertTrue(WAY['envi']['lineage'])

            detached = start('setsid nohup sleep 30 >/dev/null 2>&1 & echo $!')
            did = child_pid(detached)
            pids.append(did)
            detached.wait(timeout=10)                              # the launching shell ends, the child stays
            time.sleep(0.2)
            procs.reset()
            self.assertEqual(procs.env_values(did, (marker.encode(),)), {marker: 'x-session'})
            self.assertNotIn(detached.pid, procs.ancestors(did))
            self.assertTrue(WAY['detach']['env'])
            self.assertFalse(WAY['detach']['lineage'])
        finally:
            for p in launched:
                p.kill()
                p.wait()
                p.stdout.close()
            for pid in pids:                                           # the background `sleep`s are not children of what was killed above
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            procs.reset()


class CaseFoldingFileSystem(unittest.TestCase):
    """A scene that needs B.md and b.md side by side cannot be built where the file system folds case (the macOS default). Such a case is left out and said so, never
    graded as a miss, and an xfail list is not written from a run that left cases out."""

    def collide_and_plain(self):
        cases = run.select()
        collide = [c for c in cases if build.needs_two_names_by_case(c)]
        plain = [c for c in cases if c.bundle == 'deb' and not build.needs_two_names_by_case(c)]
        self.assertTrue(collide and plain)
        return cases, collide, plain

    def test_the_collision_cases_are_left_out_only_on_a_folding_file_system(self):
        cases, collide, plain = self.collide_and_plain()
        want = {c.id for c in collide}
        with mock.patch.object(build, 'folds_case', lambda folder: True):
            self.assertEqual(run.skipped_here(cases), want)
            root = tempfile.mkdtemp(prefix='scen-fold-')
            self.addCleanup(shutil.rmtree, root, True)
            cells, errors = run.run_all(collide[:2] + plain[:1], root)
            self.assertEqual(errors, [])
            self.assertEqual({c.case for c in cells}, {plain[0].id})                      # the collision cases give no cell at all, the others still run
            self.assertIn('skipped on this machine: %d cases' % len(want), run.summary_text(cases, cells, skipped=want))
        with mock.patch.object(build, 'folds_case', lambda folder: False):
            self.assertEqual(run.skipped_here(cases), set())
            root = tempfile.mkdtemp(prefix='scen-nofold-')
            self.addCleanup(shutil.rmtree, root, True)
            cells, errors = run.run_all(collide[:1], root)
            self.assertEqual((errors, {c.case for c in cells}), ([], {collide[0].id}))

    def test_a_listed_case_that_was_left_out_is_not_stale(self):
        doc = {'cells': {'deb:gone': {'seat.cell': ['miss', 'done', 'missing', 'D-DEBATE']}}}
        self.assertEqual(run.compare_xfail([], doc, skipped={'deb:gone'}), [])
        self.assertEqual(len(run.compare_xfail([], doc)), 1)

    def test_the_probe(self):
        with tempfile.TemporaryDirectory(prefix='scen-probe-') as d:
            build._FOLDS.clear()
            self.addCleanup(build._FOLDS.clear)
            with mock.patch('os.path.exists', lambda p: True):                            # the other spelling of the probe file "exists": the file system folds case
                self.assertTrue(build.folds_case(d))
            build._FOLDS.clear()
            self.assertIsInstance(build.folds_case(d), bool)
            self.assertEqual(os.listdir(d), [])                                           # the probe file is gone
            self.assertFalse(build.folds_case(os.path.join(d, 'missing')))

    def test_an_xfail_list_is_not_written_from_a_run_that_left_cases_out(self):
        with mock.patch.object(run, 'skipped_here', lambda cases, root=None: {'deb:x'}), mock.patch.object(run, 'XFAIL', os.devnull):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run.main(['--write-xfail', '--cases', 'no-such-case*'])


class RoomScenes(unittest.TestCase):
    """The room bundle: a folder where agents of one orchestrator share a guide of any name and each hold a file of their own (or only message each other), the lookalikes
    that are no room, and the words of the instruction that are never evidence. The builder writes what each value says, so a red cell is the board's."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-room-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def case(self, **v):
        return axes.normalize(Case('room', v))

    def build(self, **v):
        c = self.case(**v)
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def kind(self, **v):
        return oracle.room_kind(self.case(**v).v)[0]

    def prompt(self, b, role):
        return _read_lines(b.paths[role])[0]['message']['content']

    def calls(self, path, name):
        return [blk['input'] for d in _read_lines(path) if d.get('type') == 'assistant' for blk in d['message']['content']
                if blk.get('type') == 'tool_use' and blk['name'] == name]

    # --- the rules ---
    def test_a_room_needs_a_shared_guide_and_a_file_of_each_participants_own(self):
        self.assertEqual(self.kind(), 'cells')
        # the first half only: everybody points at one guide, and the files are no per-participant files in its folder
        for guide in ('top_readme', 'top_claude', 'top_agents', 'docs_guide'):
            c = self.case(guide=guide)
            self.assertEqual(c.v['shape'], 'far', guide)                                    # a common document has no folder to write beside
            self.assertIsNone(oracle.room_kind(c.v)[0], guide)
        for shape in ('deep', 'far', 'same', 'scatter'):
            self.assertIsNone(self.kind(shape=shape), shape)
        # the second half only: a file of each beside the others, and no shared guide in the first instruction
        for guide in ('own', 'missing', 'late'):
            self.assertIsNone(self.kind(guide=guide), guide)
        self.assertIsNone(self.kind(people='1'))
        self.assertIsNone(self.kind(trees='two', people='2'))                               # one participant on each orchestrator's page
        self.assertEqual(self.kind(trees='two', people='3'), 'cells')                       # two of them on this page, the third is another orchestrator's

    def test_a_guide_of_any_name_and_a_file_beside_it_or_one_below_make_a_room(self):
        for guide, shape, people in itertools.product(('agenda', 'brief', 'readme', 'plan'), ('beside', 'below', 'mixed'), ('2', '3', '5')):
            self.assertEqual(self.kind(guide=guide, shape=shape, people=people), 'cells', (guide, shape, people))
            T = oracle.truth(self.case(guide=guide, shape=shape, people=people))
            self.assertEqual(sum(1 for r, f in T.subjects.items() if f.get('role') == 'writer'), int(people))

    def test_the_title_of_a_room_is_the_first_heading_of_its_guide(self):
        for lang in ('en', 'ko'):
            for guide in ('agenda', 'brief', 'readme', 'plan'):
                c, b = self.build(guide=guide, lang=lang)
                with open(os.path.join(b.meta['unit'], axes.ROOM_GUIDES[guide])) as fh:
                    heading = re.match(r'# (.+)', fh.read()).group(1)
                self.assertEqual(oracle.truth(c).subjects['listing']['titles'], frozenset(['docs/meeting|' + heading]), (guide, lang))

    def test_the_words_of_the_instruction_are_never_evidence(self):
        for guide, (shape, talk), lang in itertools.product(('agenda', 'brief', 'late'), (('beside', 'none'), ('below', 'none'), ('none', 'peer'), ('none', 'none'), ('r1', 'none'), ('far', 'none')), AXES['lang']):
            truths = [oracle.truth(self.case(word=w, lang=lang, shape=shape, talk=talk, guide=guide)).subjects for w in AXES['word']]
            self.assertTrue(all(t == truths[0] for t in truths), (guide, shape, talk, lang))
        # ... and the builder does write them: five instructions, five wordings
        for lang in AXES['lang']:
            texts = {w: self.prompt(self.build(word=w, lang=lang)[1], 'p1') for w in AXES['word']}
            self.assertEqual(len(set(texts.values())), 5, lang)
        self.assertIn('debate', self.prompt(self.build(word='debate')[1], 'p1'))
        self.assertIn('회의', self.prompt(self.build(word='meeting', lang='ko')[1], 'p1'))
        self.assertNotRegex(re.sub(r'`[^`]*`', '', self.prompt(self.build(word='none')[1], 'p1')), r'(?i)debate|meeting|agenda|sync')     # no noun at all (the paths are not words)

    def test_a_meeting_by_message_only_is_a_room_without_cells(self):
        c = self.case(shape='none', talk='peer')
        T = oracle.truth(c)
        self.assertEqual(T.subjects['listing']['units'], frozenset(['docs/meeting']))
        for role in ('p1', 'p2', 'p3'):
            self.assertEqual(T.subjects[role], dict(unit='docs/meeting', seat=None, cell=None, role='none', placements=frozenset()))
        for talk in ('none', 'orch'):                                                         # nobody tells the others anything: the orchestrator alone does, or nobody
            self.assertIsNone(self.kind(shape='none', talk=talk), talk)
        self.assertIsNone(self.kind(shape='none', talk='peer', guide='top_readme'))          # a common document and messages: still no room
        self.assertIsNone(self.kind(shape='none', talk='peer', guide='late'))
        for shape in ('same', 'far', 'deep', 'scatter'):                                      # messages beside files that are no per-participant files: those are not meetings by message only
            self.assertIsNone(self.kind(shape=shape, talk='peer'), shape)
        self.assertEqual(self.case(people='1', shape='none', talk='peer').v['talk'], 'none')

    def test_a_round_folder_makes_a_debate_whatever_the_guide_is_called(self):
        for guide in ('agenda', 'plan', 'readme', 'brief'):
            c = self.case(shape='r1', guide=guide, people='2')
            T = oracle.truth(c)
            self.assertEqual(oracle.room_kind(c.v)[0], 'debate')
            self.assertEqual(T.subjects['listing']['units'], frozenset(['docs/meeting']))
            self.assertNotIn('titles', T.subjects['listing'])                                 # the title of a folder that was a debate already is not asked
            self.assertEqual(T.subjects['p1']['placements'], frozenset(['docs/meeting|1|A|r1/A']))
        self.assertEqual(oracle.room_kind(self.case(shape='r1', people='1').v)[0], 'debate')   # one participant is enough for a debate
        self.assertEqual(self.case(shape='r1', guide='top_readme').v['shape'], 'far')          # a common document has no folder of its own to hold a round folder
        self.assertEqual(self.case(shape='r1', guide='own').v['guide'], 'agenda')              # a debate has the guide of its folder

    def test_the_seat_is_the_letter_a_marker_names_else_the_tag_else_the_stem_of_the_file(self):
        marks = ('bracket', 'dam', 'dam_paren', 'en_participant', 'en_as', 'en_seat', 'tag')
        for mark in marks:
            c = self.case(seatmark=mark, fname='prefix', shape='below')
            self.assertEqual(oracle.truth(c).subjects['p2']['seat'], 'B', mark)
            self.assertEqual(oracle.truth(c).subjects['p2']['placements'], frozenset(['docs/meeting|1|B|out/notes_B']), mark)       # the place is the file, the seat the letter
        for mark in ('none', 'quoted', 'negated', 'other'):                                  # a letter that is quoted, negated or means something else seats nobody
            for lang in AXES['lang']:
                c = self.case(seatmark=mark, fname='prefix', shape='below', lang=lang)
                self.assertEqual(oracle.truth(c).subjects['p2']['seat'], 'notes_B', (mark, lang))
        for mark in AXES['seatmark']:
            self.assertEqual(oracle.truth(self.case(seatmark=mark, fname='plain')).subjects['p2']['seat'], 'B', mark)           # the file is named by the letter: all agree
        self.assertEqual(oracle.truth(self.case(shape='mixed', people='5')).subjects['p2']['placements'], frozenset(['docs/meeting|1|notes_B|notes_B']))
        self.assertEqual(oracle.truth(self.case(shape='mixed', people='5')).subjects['p3']['placements'], frozenset(['docs/meeting|1|C|out/C']))

    def test_a_marker_needs_the_language_it_is_written_in(self):
        for mark in ('dam', 'dam_paren'):
            self.assertEqual(self.case(seatmark=mark, lang='en').v['lang'], 'ko')
        for mark in ('en_participant', 'en_as', 'en_seat'):
            self.assertEqual(self.case(seatmark=mark, lang='ko').v['lang'], 'en')

    def test_the_cells_follow_the_files_and_whether_the_participants_work_on(self):
        for proof, phase, want in (('told', 'working', 'writing'), ('told', 'done', 'missing'), ('wrote', 'working', 'draft'), ('both', 'done', 'done')):
            T = oracle.truth(self.case(proof=proof, phase=phase))
            self.assertEqual({T.subjects[r]['cell'] for r in ('p1', 'p2', 'p3')}, {want}, (proof, phase))

    def test_an_agent_of_another_orchestrator_is_not_on_the_page(self):
        c, b = self.build(trees='two', people='5')
        self.assertEqual(b.meta['page'], ['p1', 'p3', 'p5'])
        self.assertEqual(b.meta['strangers'], ['p2', 'p4'])
        T = oracle.truth(c)
        self.assertEqual({r for r in T.subjects if r != 'listing'}, {'p1', 'p3', 'p5'})
        other = os.path.join(os.path.dirname(b.paths['orch']), b.sid('orch2'), 'subagents')
        mine = os.path.join(os.path.dirname(b.paths['orch']), b.sid('orch'), 'subagents')
        self.assertEqual(len([f for f in os.listdir(other) if f.endswith('.jsonl')]), 2)
        self.assertEqual(len([f for f in os.listdir(mine) if f.endswith('.jsonl')]), 3)
        self.assertEqual(self.prompt(b, 'p1').split('. ')[0], 'You are a participant of the meeting')      # the same words, another tree

    # --- the builder ---
    def test_every_first_instruction_points_where_the_guide_axis_says(self):
        c, b = self.build()
        guide = os.path.join(b.meta['unit'], 'agenda.md')
        for role in ('p1', 'p2', 'p3'):
            self.assertIn('`%s`' % guide, self.prompt(b, role))
        self.assertTrue(os.path.isfile(guide))
        c, b = self.build(guide='own')
        self.assertEqual(sorted(f for f in os.listdir(b.meta['unit']) if f.startswith('agenda')), ['agenda_A.md', 'agenda_B.md', 'agenda_C.md'])
        for role, letter in zip(('p1', 'p2', 'p3'), 'ABC'):
            self.assertIn('agenda_%s.md' % letter, self.prompt(b, role))
        c, b = self.build(guide='missing')
        self.assertIn('agenda.md', self.prompt(b, 'p1'))
        self.assertFalse(os.path.exists(os.path.join(b.meta['unit'], 'agenda.md')))
        c, b = self.build(guide='late')
        self.assertTrue(os.path.isfile(os.path.join(b.meta['unit'], 'agenda.md')))
        for role in ('p1', 'p2', 'p3'):
            self.assertNotIn('agenda.md', self.prompt(b, role))                               # the first instruction names no guide ...
            later = [d['message']['content'] for d in _read_lines(b.paths[role]) if d.get('origin', {}).get('kind') == 'coordinator']
            self.assertEqual(len(later), 1)
            self.assertIn('agenda.md', later[0])                                              # ... a later message does
        for guide, rel in (('top_readme', 'README.md'), ('top_claude', 'CLAUDE.md'), ('top_agents', 'AGENTS.md'), ('docs_guide', 'docs/guide.md')):
            c, b = self.build(guide=guide)
            self.assertTrue(os.path.isfile(os.path.join(b.meta['repo'], rel)), guide)
            self.assertIn('`%s`' % os.path.join(b.meta['repo'], rel), self.prompt(b, 'p2'))
            self.assertFalse(os.path.exists(os.path.join(b.meta['repo'], 'docs', 'meeting', 'agenda.md')))

    def test_the_guide_never_declares_a_participant_a_reviewer_or_a_round(self):
        """A guide that did would be read by the rules of a debate (units.declares_rounds, declared_reports): it must say nothing but its title and its topics."""
        for guide, lang in itertools.product(('agenda', 'brief', 'readme', 'plan'), AXES['lang']):
            c, b = self.build(guide=guide, lang=lang)
            with open(os.path.join(b.meta['unit'], axes.ROOM_GUIDES[guide])) as fh:
                text = fh.read()
            self.assertNotRegex(text, r'(?i)reviewers?\b|(?:^|[^\w/])(?:r|round)\d+/|\*\*[A-Z]\s*[—–-]|\.md`')

    def test_files_are_on_disk_where_the_shape_says_and_when_a_participant_wrote_them(self):
        for shape, rel in (('beside', 'docs/meeting/{L}.md'), ('below', 'docs/meeting/out/{L}.md'), ('r1', 'docs/meeting/r1/{L}.md'), ('deep', 'docs/meeting/out/x/{L}.md'),
                           ('far', 'work/reports/{L}.md')):
            for proof in ('told', 'wrote', 'both'):
                c, b = self.build(shape=shape, proof=proof, people='2')
                for letter in 'AB':
                    path = os.path.join(b.meta['repo'], rel.format(L=letter))
                    self.assertEqual(os.path.isfile(path), proof != 'told', (shape, proof, letter))
                    told = path in self.prompt(b, 'p%d' % ('AB'.index(letter) + 1))
                    self.assertEqual(told, proof != 'wrote', (shape, proof))                    # the instruction names the file when it is told
                    wrote = [w['file_path'] for w in self.calls(b.paths['p%d' % ('AB'.index(letter) + 1)], 'Write')]
                    self.assertEqual(wrote, [path] if proof != 'told' else [], (shape, proof))
        c, b = self.build(shape='same', people='3')
        self.assertEqual(sorted(os.listdir(b.meta['unit'])), ['agenda.md', 'minutes.md'])
        c, b = self.build(shape='scatter', people='3')
        for letter in 'abc':
            self.assertTrue(os.path.isdir(os.path.join(b.meta['repo'], 'repos', 'svc_' + letter, '.git')))
            self.assertTrue(os.path.isfile(os.path.join(b.meta['repo'], 'repos', 'svc_' + letter, 'src', 'part.py')))
        self.assertEqual(sorted(os.listdir(b.meta['unit'])), ['agenda.md'])
        c, b = self.build(shape='none')
        self.assertEqual(sorted(os.listdir(b.meta['unit'])), ['agenda.md'])                   # nothing is written, nothing is told to be written
        self.assertEqual(self.calls(b.paths['p1'], 'Write'), [])

    def test_a_path_is_written_in_the_form_the_axis_says(self):
        for ref in ('abs', 'rel', 'tilde'):
            c, b = self.build(ref=ref, people='2')
            text = self.prompt(b, 'p1')
            want = {'abs': os.path.join(b.meta['repo'], 'docs/meeting/agenda.md'), 'rel': 'docs/meeting/agenda.md',
                    'tilde': '~' + os.path.join(b.meta['repo'], 'docs/meeting/agenda.md')[len(b.home):]}[ref]
            self.assertIn('`%s`' % want, text)
            self.assertIn(want.replace('agenda.md', 'A.md'), text)

    def test_messages_go_between_the_participants_or_from_the_orchestrator_only(self):
        c, b = self.build(shape='none', talk='peer', people='3')
        ids = {r: b.ids[r] for r in ('p1', 'p2', 'p3')}
        for role, to in (('p1', 'p2'), ('p2', 'p3'), ('p3', 'p1')):                          # a ring: each one tells the next
            self.assertEqual([m['to'] for m in self.calls(b.paths[role], 'SendMessage')], [ids[to]], role)
        self.assertEqual(self.calls(b.paths['orch'], 'SendMessage'), [])
        c, b = self.build(shape='none', talk='orch', people='3')
        self.assertEqual({m['to'] for m in self.calls(b.paths['orch'], 'SendMessage')}, {b.ids[r] for r in ('p1', 'p2', 'p3')})
        for role in ('p1', 'p2', 'p3'):
            self.assertEqual(self.calls(b.paths[role], 'SendMessage'), [], role)
        c, b = self.build(shape='none', talk='peer', people='2')
        self.assertEqual([m['to'] for m in self.calls(b.paths['p1'], 'SendMessage')], [b.ids['p2']])
        self.assertEqual([m['to'] for m in self.calls(b.paths['p2'], 'SendMessage')], [b.ids['p1']])
        c, b = self.build(shape='none', talk='peer', trees='two', people='3')                 # the ring stays inside the orchestrator: p1 and p3 on this page, p2 on the other
        self.assertEqual([m['to'] for m in self.calls(b.paths['p1'], 'SendMessage')], [b.ids['p3']])
        self.assertEqual([m['to'] for m in self.calls(b.paths['p3'], 'SendMessage')], [b.ids['p1']])

    NAIVE_MARK = re.compile(r'\[[A-Za-z0-9_]+-([A-E])\]|(?<![A-Za-z0-9])([A-E])\s*\([^)\n]{1,60}\)\s*담당|(?<![A-Za-z0-9])([A-E])\s*담당|participant ([A-E])\b|[Ss]eat ([A-E])\b|\bas ([A-E]) \(')

    def naive_letter(self, text):
        """The letter a reader that only matches the words finds in the part of an instruction a marker is looked for in (the first 300 characters), or None."""
        m = self.NAIVE_MARK.search(text[:300])
        return next(g for g in m.groups() if g) if m else None

    def test_a_marker_opens_the_instruction_and_a_lookalike_is_no_marker_for_a_reader_that_only_matches_words(self):
        want = {'bracket': r'^\[ROOM-B\] ', 'dam': r'^당신은 B 담당입니다', 'dam_paren': r'^당신은 B\(예산\) 담당입니다', 'en_participant': r'^You are participant B \(Budget\)',
                'en_as': r'^Work as B \(Budget\)', 'en_seat': r'^You hold seat B\.'}
        for mark, rx in want.items():
            text = self.prompt(self.build(seatmark=mark)[1], 'p2')
            self.assertRegex(text, rx, mark)
            self.assertEqual(self.naive_letter(text), 'B', mark)                                   # the participant's own letter, at the start
        for mark, lang in itertools.product(('quoted', 'negated', 'other'), AXES['lang']):
            c, b = self.build(seatmark=mark, lang=lang)
            for i, role in enumerate(('p1', 'p2', 'p3')):
                found = self.naive_letter(self.prompt(b, role))
                self.assertIsNotNone(found, (mark, lang, role))                                      # it does look like a marker to a reader that only matches the words ...
                if mark != 'other':
                    self.assertEqual(found, 'BCD'[i], (mark, lang, role))                            # ... of the next participant's letter, never the participant's own
        for mark in ('none', 'tag'):
            self.assertIsNone(self.naive_letter(self.prompt(self.build(seatmark=mark)[1], 'p1')), mark)
        for i, phrase in enumerate(('as C (not C++)', 'seat C', 'participant C of the user study')):
            self.assertIn(phrase, self.prompt(self.build(seatmark='other')[1], 'p%d' % (i + 1)))
        self.assertIn('C(언어) 담당', self.prompt(self.build(seatmark='other', lang='ko')[1], 'p1'))
        self.assertIn('C 담당자', self.prompt(self.build(seatmark='other', lang='ko')[1], 'p2'))
        c, b = self.build(seatmark='tag')                                                          # the tag is the first word of the description
        self.assertEqual(sorted(x['description'][:2] for x in self.calls(b.paths['orch'], 'Agent')), ['A ', 'B ', 'C '])

    def test_every_room_case_is_built_as_its_axes_say(self):
        """A sweep over every room case of the selection: who is on the page, what each first instruction points at (in the form the axis says), which files are on
        disk, and who sends a message to whom."""
        n = 0
        for c in run.select():
            if c.bundle != 'room':
                continue
            n += 1
            b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))
            v, repo = c.v, b.meta['repo']
            form = {'abs': lambda r: os.path.join(repo, r), 'rel': lambda r: r, 'tilde': lambda r: '~' + os.path.join(repo, r)[len(b.home):]}[v['ref']]
            page = [(int(r[1:]) - 1, r) for r in b.meta['page']]
            self.assertEqual(len(self.calls(b.paths['orch'], 'Agent')), len(page) + (v['copy'] == 'worktree'), c.id)          # a checkout with a copy of the bundle has another agent working in it
            self.assertEqual(page, [(i, 'p%d' % (i + 1)) for i in range(int(v['people'])) if axes.room_tree(v, i) == 1], c.id)
            for i, role in page:
                text = self.prompt(b, role)
                guide, on_disk = axes.room_guide(v, i)
                self.assertEqual('`%s`' % form(guide) in text if guide else False, guide is not None, c.id)
                if guide:
                    self.assertEqual(os.path.isfile(os.path.join(repo, guide)), on_disk, c.id)
                out, code = axes.room_out(v, i), axes.room_code(v, i)
                if out is not None:
                    self.assertEqual('`%s`' % form(out) in text, v['proof'] != 'wrote', c.id)       # the instruction names the file unless only the write shows it
                    wrote = [os.path.join(repo, out)] + ([os.path.join(repo, code)] if code else [])
                    scratch = [os.path.join(b.work, 'scratch', name % axes.SEAT_LETTERS[i]) for name in ('repro_%s.py', 'draft_%s.md')] if v['scratch'] == 'tmp' else []
                    self.assertEqual([w['file_path'] for w in self.calls(b.paths[role], 'Write')], (wrote if v['proof'] != 'told' else []) + scratch, c.id)
                    logs = [x['command'] for x in self.calls(b.paths[role], 'Bash') if '> ' in x['command']]
                    self.assertEqual(logs, ['make test > %s' % os.path.join(repo, 'logs', axes.SEAT_LETTERS[i] + '.txt')] if v['scratch'] == 'log' else [], c.id)
                    self.assertEqual(('Execute these instructions:\n\n```text\n' in text or '다음 지시를 따르세요:\n\n```text\n' in text), v['wrap'] == 'exec_fence', c.id)
                    self.assertEqual(('Implement your step in a source file' in text or '소스 파일에 구현하세요' in text), v['code'] == 'implied' or (bool(code) and v['proof'] == 'wrote'), c.id)
                    if code:
                        self.assertEqual('`%s`' % form(code) in text, v['proof'] != 'wrote', c.id)
                else:
                    self.assertEqual(self.calls(b.paths[role], 'Write'), [], c.id)
            for i in range(int(v['people'])):
                out, code = axes.room_out(v, i), axes.room_code(v, i)
                if out is not None:
                    self.assertEqual(os.path.isfile(os.path.join(repo, out)), v['proof'] != 'told', c.id)
                if code:
                    self.assertEqual(os.path.isfile(os.path.join(repo, code)), v['proof'] != 'told', c.id)
            sent = {role: len(self.calls(b.paths[role], 'SendMessage')) for _, role in page}
            same_tree = [i for i, _ in page]
            self.assertEqual(sum(sent.values()), len(same_tree) if v['talk'] == 'peer' and len(same_tree) > 1 else 0, c.id)
            self.assertEqual(len(self.calls(b.paths['orch'], 'SendMessage')), len(page) if v['talk'] == 'orch' else 0, c.id)
        self.assertGreater(n, 300)

    # --- the invariant that keeps the debate cases as they are ---
    def test_in_a_debate_case_no_other_agent_points_at_the_guide_of_the_participant(self):
        """The room rules need two agents of one orchestrator pointing at the same guide. Of the debate and coupling cases only the rival does (it writes the same
        file as the participant, so it has no file of its own): every other agent of those scenes is given a text that names no guide, and the room rules add nothing."""
        guide = re.compile(r'\b(?:brief|README|index|agenda|plan)\.md\b')
        n = 0
        for c in run.select():
            if c.bundle not in ('deb', 'cpl') or c.twin_of or int(axes.digest(c.id, n=4), 16) % 9:
                continue
            b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))
            texts = []
            for d in _read_lines(b.paths['orch']):
                if d.get('type') == 'assistant':
                    for blk in d['message']['content']:
                        if blk.get('type') == 'tool_use' and blk['name'] == 'Agent':
                            texts.append(blk['input'].get('prompt', ''))
                        elif blk.get('type') == 'tool_use' and blk['name'] == 'Bash' and ('claude -p' in blk['input'].get('command', '') or 'codex exec' in blk['input'].get('command', '')):
                            texts.append(blk['input']['command'])
            naming = [t for t in texts if guide.search(t)]
            self.assertLessEqual(len(naming), 2 if c.v['role'] == 'rival' else 1, c.id)
            n += 1
        self.assertGreater(n, 30)


def _when(stamp):
    """Seconds since the epoch of a record's ISO timestamp."""
    import calendar
    return calendar.timegm(time.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S')) + float('0' + stamp[19:-1])


class BundleScenes(unittest.TestCase):
    """The room's folder as one topic of a bundle (`bundle=root`): the folder above it holds the bundle's brief and, by `above`, a document that may close it; `copy` reaches the
    bundle's folder by a link the participants write their paths with, or copies it into a linked worktree of the repository where another agent works."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-bundle-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, **v):
        c = axes.normalize(Case('room', dict(bundle='root', proof='wrote', phase='done', **v)))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def calls(self, path, name):
        return [blk['input'] for d in _read_lines(path) if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == name]

    def test_the_new_axes_are_named_in_an_id_only_off_the_baseline(self):
        base = Case('room', {}).id
        for a in ('bundle', 'above', 'copy'):
            self.assertNotIn(a + '=', base)
            self.assertIn(a, axes.OPTIONAL['room'])
        self.assertIn('above=closing', Case('room', dict(bundle='root', above='closing')).id)

    def test_what_cannot_be_a_bundle_topic_is_folded_to_a_room_alone(self):
        for v in (dict(guide='top_readme'), dict(shape='far'), dict(shape='r1'), dict(people='1'), dict(trees='two'), dict(cite='fence')):
            c = axes.normalize(Case('room', dict(v, bundle='root', above='closing', copy='link')))
            self.assertEqual((c.v['bundle'], c.v['above'], c.v['copy']), ('none', 'none', 'none'), v)
        self.assertEqual(axes.normalize(Case('room', dict(above='closing', copy='worktree'))).v['above'], 'none')

    def test_the_bundle_has_a_brief_and_the_room_is_a_folder_of_it(self):
        c, b = self.build()
        repo = b.meta['repo']
        self.assertEqual(b.meta['unit'], os.path.join(repo, 'docs', 'records', 'bundle', 'meeting'))
        self.assertTrue(os.path.isfile(os.path.join(repo, 'docs', 'records', 'bundle', 'brief.md')))
        self.assertTrue(os.path.isfile(os.path.join(b.meta['unit'], 'agenda.md')))

    def test_the_documents_above_the_room(self):
        for above, name, after in (('none', None, None), ('closing', 'CLOSING.md', True), ('unnamed', 'CLOSING.md', True), ('early', 'CLOSING.md', False), ('plain', 'notes.md', True), ('open', 'CLOSING.md', True)):
            c, b = self.build(above=above)
            bundle = os.path.join(b.meta['repo'], 'docs', 'records', 'bundle')
            have = sorted(f for f in os.listdir(bundle) if f != 'brief.md' and os.path.isfile(os.path.join(bundle, f)))
            self.assertEqual(have, [name] if name else [], above)
            if name:
                mine = os.path.getmtime(os.path.join(b.meta['unit'], 'A.md'))
                self.assertEqual(os.path.getmtime(os.path.join(bundle, name)) > mine, after, above)
                with open(os.path.join(bundle, name)) as f:
                    said = f.read()
                self.assertEqual('meeting/' in said, above in ('closing', 'early', 'plain'), above)

    def test_the_link_is_what_the_participants_write_and_the_files_are_in_the_real_folder(self):
        c, b = self.build(copy='link')
        repo = b.meta['repo']
        link = os.path.join(repo, 'view')
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(repo, 'docs', 'records', 'bundle')))
        self.assertEqual(b.meta['unit'], os.path.join(link, 'meeting'))
        self.assertTrue(os.path.isfile(os.path.join(repo, 'docs', 'records', 'bundle', 'meeting', 'A.md')))
        for role in b.meta['page']:
            text = _read_lines(b.paths[role])[0]['message']['content']
            self.assertIn(os.path.join(link, 'meeting'), text)
            self.assertNotIn('docs/records/bundle', text)
            self.assertTrue(all(w['file_path'].startswith(link + os.sep) for w in self.calls(b.paths[role], 'Write')))

    def test_the_worktree_is_a_linked_checkout_with_a_copy_of_the_bundle_and_an_agent_in_it(self):
        c, b = self.build(copy='worktree', above='closing')
        repo, wt = b.meta['repo'], os.path.join(b.work, 'wt')
        def read(path):
            with open(path) as f:
                return f.read()
        git = read(os.path.join(wt, '.git')).split(':', 1)[1].strip()
        self.assertEqual(os.path.realpath(os.path.join(git, read(os.path.join(git, 'commondir')).strip())), os.path.realpath(os.path.join(repo, '.git')))
        for rel in ('brief.md', 'CLOSING.md', 'meeting/agenda.md', 'meeting/A.md'):
            one, two = os.path.join(repo, 'docs', 'records', 'bundle', rel), os.path.join(wt, 'docs', 'records', 'bundle', rel)
            self.assertEqual((read(one), os.path.getmtime(one)), (read(two), os.path.getmtime(two)), rel)
        agents = self.calls(b.paths['orch'], 'Agent')
        self.assertEqual(len(agents), len(b.meta['page']) + 1)
        first = _read_lines(b.paths['bystander'])[0]
        self.assertEqual(first['cwd'], wt)
        self.assertNotIn('meeting', first['message']['content'])                       # it points at no guide: it is no participant

    def test_the_truth_closes_the_room_only_when_the_conclusion_is_after_the_files_and_the_files_are_in_and_nobody_works(self):
        for above, phase, proof in itertools.product(AXES['above'], AXES['phase'], ('told', 'wrote')):
            c = axes.normalize(Case('room', dict(bundle='root', above=above, phase=phase, proof=proof)))
            got = oracle.truth(c).subjects['listing']['finals']
            want = above in ('closing', 'unnamed') and phase == 'done' and proof == 'wrote'
            self.assertEqual(got, frozenset(['docs/records/bundle/meeting|../CLOSING.md']) if want else frozenset(), (above, phase, proof))


class ReviewShapes(unittest.TestCase):
    """Shapes of real work that the first scenes did not draw, as axis values with a truth and a builder: runs that never overlap, parallel work on code beside a
    plan, an earlier instruction that is only shown (a fence, a block quote, a read-only review), messages that were never delivered, and an orchestrator page whose
    session ended in a way the old scenes never had (a `claude -p` run, local slash commands, a process that is gone). The builder writes what the value says, so a red
    cell is the board's."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-review-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def case(self, bundle='room', **v):
        return axes.normalize(Case(bundle, v))

    def build(self, bundle='room', **v):
        c = self.case(bundle, **v)
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def why(self, **v):
        kind, _, _, why = oracle.room_trace(self.case(**v).v)
        return kind, why

    def calls(self, path, name):
        return [blk['input'] for d in _read_lines(path) if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == name]

    def prompt(self, b, role):
        return _read_lines(b.paths[role])[0]['message']['content']

    # --- ids: nothing that does not use the new axes is renamed ---
    def test_the_new_axes_are_named_in_an_id_only_when_they_are_not_at_the_baseline(self):
        for bundle, names in (('room', ('rtime', 'code', 'cite', 'delivery')), ('deb', ('qform',)), ('sta', ('entry', 'tail', 'process'))):
            base = Case(bundle, {}).id
            for a in names:
                self.assertNotIn(a + '=', base)
                self.assertIn(a, axes.OPTIONAL[bundle])
        self.assertIn('rtime=sequential', self.case(rtime='sequential').id)
        self.assertIn('tail=commands', self.case('sta', skind='main', tail='commands').id)
        self.assertEqual(Case.from_id(self.case('sta', skind='main', entry='sdk', tail='end', process='gone').id).key(), self.case('sta', skind='main', entry='sdk', tail='end', process='gone').key())
        self.assertNotIn('entry', run.PAIR_AXES['sta'])

    # --- runs that never overlap ---
    def test_participants_that_run_one_after_the_other_are_no_room(self):
        for people, phase in itertools.product(('2', '3', '5'), AXES['phase']):
            self.assertEqual(self.why(rtime='overlap', people=people, phase=phase), ('cells', None), (people, phase))
            self.assertEqual(self.why(rtime='sequential', people=people, phase=phase), (None, 'timing'), (people, phase))
        T = oracle.truth(self.case(rtime='sequential', phase='done'))
        self.assertEqual(T.subjects['listing']['units'], frozenset())
        for role in ('p1', 'p2', 'p3'):
            self.assertEqual(T.subjects[role], dict(seat=None, cell=None, role='none', placements=frozenset(), unit=None))
        # it only means something where a room could be one; and runs that never coexist send no message
        self.assertEqual(self.case(rtime='sequential', shape='r1').v['rtime'], 'overlap')                  # a round folder is a debate whenever it ran
        self.assertEqual(self.case(rtime='sequential', shape='none', talk='peer').v['rtime'], 'overlap')
        self.assertEqual(self.case(rtime='sequential', talk='peer').v['talk'], 'none')
        self.assertEqual(self.case(rtime='sequential', talk='orch').v['talk'], 'orch')                        # the orchestrator may message each of them
        self.assertEqual(self.why(rtime='sequential', talk='orch'), (None, 'timing'))
        self.assertEqual(self.case(rtime='sequential', people='1').v['rtime'], 'overlap')
        self.assertEqual(self.case(rtime='sequential', guide='top_readme').v['rtime'], 'overlap')

    def spans(self, b):
        """[(start, end or inf)] of the participants of the page, from their own records: the first line, and the last one when it ends the turn."""
        out = []
        for role in b.meta['page']:
            rows = _read_lines(b.paths[role])
            last = [d for d in rows if d.get('type') == 'assistant'][-1]
            ended = last['message'].get('stop_reason') == 'end_turn'
            out.append((_when(rows[0]['timestamp']), _when(rows[-1]['timestamp']) if ended else float('inf')))
        return out

    def test_the_records_overlap_in_time_exactly_when_the_axis_says(self):
        for rtime, people, phase in itertools.product(AXES['rtime'], ('2', '3', '5'), AXES['phase']):
            c, b = self.build(rtime=rtime, people=people, phase=phase)
            spans = self.spans(b)
            overlap = max(s for s, _ in spans) < min(e for _, e in spans)
            self.assertEqual(overlap, rtime in ('overlap', 'quiet'), c.id)                              # one that has not ended is there until now
            if rtime == 'quiet':                                                                         # none has ended, though each one's records stop before the next one's begin
                rows = {role: _read_lines(b.paths[role]) for role in b.meta['page']}
                windows = [(_when(r[0]['timestamp']), _when(r[-1]['timestamp'])) for r in rows.values()]
                self.assertEqual([e for _, e in spans], [float('inf')] * int(people), c.id)
                for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
                    self.assertGreater(s2 - e1, 60, c.id)
            if rtime == 'sequential':                                                                    # each starts after the one before has finished, with room to spare
                for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
                    self.assertGreater(s2 - e1, 60, c.id)
                self.assertEqual([e == float('inf') for _, e in spans], [False] * (int(people) - 1) + [phase == 'working'], c.id)
            elif phase == 'done' and rtime != 'quiet':
                self.assertTrue(all(e != float('inf') for _, e in spans), c.id)

    # --- parallel work on code that reports beside a plan ---
    def test_participants_that_mostly_change_code_outside_the_folder_are_no_room(self):
        want = {('2', 'none'): 'cells', ('2', 'one'): 'cells', ('2', 'all'): None, ('3', 'one'): 'cells', ('3', 'majority'): None, ('3', 'all'): None,
                ('5', 'one'): 'cells', ('5', 'majority'): None, ('5', 'all'): None}
        for (people, code), kind in want.items():
            for shape in ('beside', 'below', 'mixed'):
                self.assertEqual(self.why(people=people, code=code, shape=shape), (kind, None if kind else 'code'), (people, code, shape))
        self.assertEqual(self.case(people='2', code='majority').v['code'], 'all')                           # two of two
        self.assertEqual({len(axes.room_coders(self.case(people=p, code='majority').v)) for p in ('3',)}, {2})
        self.assertEqual(len(axes.room_coders(self.case(people='5', code='majority').v)), 3)
        self.assertEqual(self.case(code='all', shape='far').v['code'], 'none')                              # code is a second file beside the notes in the folder
        self.assertEqual(self.case(code='all', guide='top_readme').v['code'], 'none')
        # a room that is one: the notes are still the file of each, code or not
        T = oracle.truth(self.case(people='3', code='one', shape='below'))
        self.assertEqual({T.subjects[r]['seat'] for r in ('p1', 'p2', 'p3')}, {'A', 'B', 'C'})
        T = oracle.truth(self.case(people='3', code='all', rtime='overlap'))
        self.assertEqual(T.subjects['listing']['units'], frozenset())
        for kind in oracle.room_trace(self.case(people='3', code='all').v)[:1]:
            self.assertIsNone(kind)

    def test_the_code_is_told_and_written_where_the_axis_says(self):
        for code, people, proof in itertools.product(('one', 'majority', 'all'), ('3', '5'), AXES['proof']):
            c, b = self.build(code=code, people=people, proof=proof)
            repo, v = b.meta['repo'], c.v
            coders = axes.room_coders(v)
            for i in range(int(people)):
                role, path = 'p%d' % (i + 1), os.path.join(repo, 'src', 'task_%s' % 'abcde'[i], 'part.py')
                told = path in self.prompt(b, role)
                written = [w['file_path'] for w in self.calls(b.paths[role], 'Write') if w['file_path'].endswith('part.py')]
                self.assertEqual(told, i in coders and proof != 'wrote', (c.id, role))
                self.assertEqual(written, [path] if (i in coders and proof != 'told') else [], (c.id, role))
                self.assertEqual(os.path.isfile(path), i in coders and proof != 'told', (c.id, role))
            self.assertEqual(len(coders), {'one': 1, 'majority': int(people) // 2 + 1, 'all': int(people)}[code])
            self.assertNotIn('src', os.listdir(b.meta['unit']))                                              # the code is outside the guide's folder

    # --- an earlier instruction that is only shown ---
    def test_an_instruction_that_is_only_quoted_or_reviewed_is_no_room_and_seats_nobody(self):
        for cite, shape, lang in itertools.product(AXES['cite'][1:], ('beside', 'below', 'mixed'), AXES['lang']):
            c = self.case(cite=cite, shape=shape, lang=lang)
            self.assertEqual(oracle.room_trace(c.v)[0::3], (None, 'cite'), c.id)
            T = oracle.truth(c)
            self.assertEqual(T.subjects['listing']['units'], frozenset(), c.id)
            for role in ('p1', 'p2', 'p3'):
                self.assertEqual(T.subjects[role], dict(seat=None, cell=None, role='none', placements=frozenset()), c.id)
        # a folder that already has a round folder is a debate on the list, and nobody in it is seated by words that were only quoted
        for cite in AXES['cite'][1:]:
            c = self.case(cite=cite, shape='r1')
            self.assertEqual(oracle.room_trace(c.v)[0::3], ('debate', 'cite'))
            T = oracle.truth(c)
            self.assertEqual(T.subjects['listing']['units'], frozenset(['docs/meeting']))
            self.assertEqual(T.subjects['p1'], dict(seat=None, cell=None, role='none', placements=frozenset()))
        # nothing is written and nothing but the earlier words names the files; the scene is the same wherever else the axes sit
        c = self.case(cite='fence', seatmark='bracket', proof='wrote', talk='peer', code='all', rtime='sequential', trees='two')
        self.assertEqual((c.v['proof'], c.v['seatmark'], c.v['talk'], c.v['code'], c.v['rtime'], c.v['trees']), ('told', 'none', 'none', 'none', 'overlap', 'one'))
        self.assertEqual(self.case(cite='fence', guide='top_readme').v['cite'], 'none')
        self.assertEqual(self.case(cite='fence', shape='far').v['cite'], 'none')
        self.assertEqual(self.case(cite='fence', people='1').v['cite'], 'none')

    def test_the_earlier_instruction_is_shown_in_the_form_the_axis_says(self):
        marks = {'inline': lambda t: re.search(r'review its wording; do not carry it out: "Read `[^`]+` and follow it\. Write your notes to `[^`]+`\."', t) and '```' not in t and '\n> ' not in t,
                 'fence': lambda t: re.search(r'```text\nRead `[^`]+` and follow it\.\nWrite your notes to `[^`]+`\.\n```', t),
                 'blockquote': lambda t: re.search(r'\n> Read `[^`]+` and follow it\.\n> Write your notes to `[^`]+`\.', t) and '```' not in t,
                 'readonly': lambda t: t.startswith('You are a participant of the meeting. This is a read-only review: do not create, change or run anything.') and '```' not in t and '\n> ' not in t}
        for cite, mark in marks.items():
            for shape in ('beside', 'r1'):
                c, b = self.build(cite=cite, shape=shape, people='2')
                for role, letter in (('p1', 'A'), ('p2', 'B')):
                    text = self.prompt(b, role)
                    self.assertTrue(mark(text), (cite, shape, text))
                    self.assertIn('`%s`' % os.path.join(b.meta['unit'], 'agenda.md'), text)
                    self.assertIn('%s.md`' % letter, text)                                                  # the file is named, and not written
                    self.assertEqual(self.calls(b.paths[role], 'Write'), [])
                    self.assertFalse(os.path.exists(os.path.join(b.meta['unit'], 'r1' if shape == 'r1' else '', letter + '.md')), (cite, shape))
                self.assertTrue(os.path.isfile(os.path.join(b.meta['unit'], 'agenda.md')))                  # the guide is there: only the words are somebody else's
            c, b = self.build(cite=cite, lang='ko', people='2')
            self.assertRegex(self.prompt(b, 'p1'), r'[가-힣]')

    def test_a_report_path_a_quoter_only_shows_seats_nobody_in_the_form_the_axis_says(self):
        for qform, kind, lang in itertools.product(AXES['qform'][1:], AXES['kind'], AXES['lang']):
            c, b = self.build('deb', role='quoter', qform=qform, kind=kind, lang=lang)
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child']['seat'], None, c.id)
            self.assertNotIn(('debate_in_misc', 'child'), T.diag, c.id)
            self.assertIn(('debate_in_misc', 'child'), T.allowed, c.id)
            if kind == 'sub':
                text = [x['prompt'] for x in self.calls(b.paths['orch'], 'Agent')][-1]
            else:
                text = [x['command'] for x in self.calls(b.paths['orch'], 'Bash') if 'claude -p' in x['command'] or 'codex exec' in x['command']][0]
            self.assertIn({'fence': '```text\n', 'blockquote': '\n> ', 'readonly': 'read-only review' if lang == 'en' else '읽기 전용 검토',
                           'sentence': 'Read-only quote of an earlier instruction.\nRead' if lang == 'en' else '이전 지침의 읽기 전용 인용입니다.\n`'}[qform], text, c.id)
            self.assertIn('r1/B.md', text, c.id)                                                           # the path is in the text, in every form
        c = self.case('deb', role='quoter', qform='fence', marker='quoted', wmode='redirect')
        self.assertEqual((c.v['marker'], c.v['wmode']), ('none', 'tool'))
        self.assertEqual(self.case('deb', role='writer', qform='fence').v['qform'], 'inline')               # only a quoter shows an earlier instruction
        self.assertIn(('debate_in_misc', 'child'), oracle.truth(self.case('deb', role='quoter')).diag)       # the one-line quote is as it was

    # --- messages that were never delivered ---
    def test_a_message_that_was_answered_with_an_error_makes_no_meeting(self):
        for people, guide in itertools.product(('2', '3', '5'), ('agenda', 'brief')):
            self.assertEqual(self.why(shape='none', talk='peer', people=people, guide=guide), ('members', None), (people, guide))
            self.assertEqual(self.why(shape='none', talk='peer', people=people, guide=guide, delivery='failed'), (None, 'delivery'), (people, guide))
            self.assertEqual(self.why(shape='beside', talk='peer', people=people, guide=guide, delivery='failed'), ('cells', None), (people, guide))     # files make a room by themselves
        self.assertEqual(self.case(delivery='failed').v['delivery'], 'ok')                                  # nobody to message
        self.assertEqual(self.case(delivery='failed', talk='orch').v['delivery'], 'ok')
        self.assertEqual(self.case(delivery='failed', talk='peer', people='1').v['delivery'], 'ok')
        T = oracle.truth(self.case(shape='none', talk='peer', delivery='failed'))
        self.assertEqual(T.subjects['listing']['units'], frozenset())
        self.assertEqual(T.subjects['p1'], dict(seat=None, cell=None, role='none', placements=frozenset(), unit=None))

    def test_the_failed_message_carries_the_error_flag_in_its_result(self):
        for delivery, flag in (('ok', False), ('failed', True)):
            c, b = self.build(shape='none', talk='peer', people='3', delivery=delivery)
            for role in ('p1', 'p2', 'p3'):
                rows = _read_lines(b.paths[role])
                ids = [blk['id'] for d in rows if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == 'SendMessage']
                results = [blk for d in rows if d.get('type') == 'user' and isinstance(d['message']['content'], list) for blk in d['message']['content']
                           if blk.get('type') == 'tool_result' and blk['tool_use_id'] in ids]
                self.assertEqual([r['is_error'] for r in results], [flag], (delivery, role))

    # --- the orchestrator page of a session ---
    def test_the_page_is_working_only_while_a_turn_goes_on_in_a_process_that_is_there(self):
        for entry, tail, process in itertools.product(AXES['entry'], AXES['tail'], AXES['process']):
            c = self.case('sta', skind='main', life='running', entry=entry, tail=tail, process=process)
            want = 'working' if (tail in ('mid', 'prompt', 'next') and process == 'there') else 'idle'
            self.assertEqual(oracle.truth(c).subjects['orch'], {'orch_state': want}, c.id)
        self.assertEqual(oracle.truth(self.case('sta', skind='main', life='running')).subjects['orch'], {'orch_state': 'working'})       # the baseline is as it was
        self.assertEqual(self.case('sta', skind='main', life='running').id, 'sta:skind=main;life=running;flaw=none;at=live;os=linux')
        # a `claude -p` run has no slash commands; only an orchestrator that no limit stopped has these shapes
        self.assertEqual(self.case('sta', skind='main', entry='sdk', tail='commands').v['tail'], 'end')
        for skind, life in (('cli', 'normal_end'), ('main', 'limit_exit'), ('sub', 'running')):
            v = self.case('sta', skind=skind, life=life, entry='sdk', tail='end', process='gone').v
            self.assertEqual((v['entry'], v['tail'], v['process']), ('cli', 'mid', 'there'), (skind, life))
        v = self.case('sta', skind='main', entry='sdk', tail='end', process='gone', flaw='multi_proc', os='mac_nops', at='after_restart').v
        self.assertEqual((v['flaw'], v['os'], v['at']), ('none', 'linux', 'live'))

    def test_the_builder_ends_the_session_the_way_the_axes_say(self):
        for entry, tail, process in itertools.product(AXES['entry'], AXES['tail'], AXES['process']):
            c, b = self.build('sta', skind='main', life='running', entry=entry, tail=tail, process=process)
            v = c.v
            rows = _read_lines(b.paths['orch'])
            conv = [d for d in rows if d.get('type') in ('user', 'assistant')]
            is_cmd = lambda d: isinstance(d['message']['content'], str) and d['message']['content'].startswith(('<local-command-', '<command-name>'))
            self.assertEqual({d.get('entrypoint') for d in conv}, {'sdk-cli' if v['entry'] == 'sdk' else 'cli'}, c.id)
            durations = [d for d in rows if d.get('subtype') == 'turn_duration']
            self.assertEqual(bool(durations), v['entry'] == 'cli' and v['tail'] in ('end', 'prompt', 'commands', 'next', 'asked'), c.id)          # a `claude -p` run has none
            self.assertEqual(bool([d for d in rows if d.get('type') == 'cost-state']), v['entry'] == 'sdk' and v['tail'] in ('end', 'prompt', 'asked'), c.id)
            last = conv[-1]
            if v['tail'] == 'mid':
                self.assertEqual((last['type'], last['message']['stop_reason']), ('assistant', 'tool_use'), c.id)
            elif v['tail'] in ('end', 'asked'):
                self.assertEqual((last['type'], last['message']['stop_reason']), ('assistant', 'end_turn'), c.id)
                self.assertEqual(last['message']['content'][0]['text'].endswith('?'), v['tail'] == 'asked', c.id)
            elif v['tail'] == 'next':                                                                                                     # the next prompt and its first call, within a second of the end
                self.assertEqual((last['type'], last['message']['stop_reason']), ('assistant', 'tool_use'), c.id)
                self.assertEqual((conv[-2]['type'], is_cmd(conv[-2]), conv[-3]['message']['stop_reason']), ('user', False, 'end_turn'), c.id)
                self.assertLess(_when(conv[-1]['timestamp']) - _when(conv[-3]['timestamp']), 1.0, c.id)
            elif v['tail'] == 'prompt':
                self.assertEqual(last['type'], 'user', c.id)
                self.assertFalse(is_cmd(last), c.id)
                self.assertEqual([d['message'].get('stop_reason') for d in conv if d['type'] == 'assistant'][-1], 'end_turn', c.id)      # nobody answered it
            elif v['tail'] == 'commands':
                self.assertEqual([is_cmd(d) for d in conv[-3:]], [True, True, True], c.id)
                self.assertEqual(conv[-4]['message']['stop_reason'], 'end_turn', c.id)                                                   # the turn had ended before them
            else:
                self.assertEqual([d['type'] for d in conv], ['user'] * len(conv), c.id)
                self.assertTrue(all(is_cmd(d) for d in conv), c.id)                                                                      # nothing but slash commands
            live = [p for ph in b.phases for p in ph.procs]
            self.assertEqual(len(live), 1 if v['process'] == 'there' else 0, c.id)
            if live:
                self.assertEqual('-p' in live[0]['argv'], v['entry'] == 'sdk', c.id)
                self.assertEqual(live[0]['session']['entrypoint'], 'sdk-cli' if v['entry'] == 'sdk' else 'cli', c.id)
            self.assertEqual(os.path.isfile(os.path.join(b.sess_dir, '100.json')), False, c.id)                                         # the session files are written when the board looks

    def test_every_new_shape_is_in_the_selection_with_its_reason(self):
        cases = run.select()
        self.assertEqual({c.v['rtime'] for c in cases if c.bundle == 'room'}, set(AXES['rtime']))
        self.assertEqual({c.v['code'] for c in cases if c.bundle == 'room'}, set(AXES['code']))
        self.assertEqual({c.v['cite'] for c in cases if c.bundle == 'room'}, set(AXES['cite']))
        self.assertEqual({c.v['delivery'] for c in cases if c.bundle == 'room'}, set(AXES['delivery']))
        self.assertEqual({c.v['wrap'] for c in cases if c.bundle == 'room'}, set(AXES['wrap']))
        self.assertEqual({c.v['scratch'] for c in cases if c.bundle == 'room'}, set(AXES['scratch']))
        self.assertEqual({c.v['qform'] for c in cases if c.bundle == 'deb'}, set(AXES['qform']))
        self.assertEqual({(c.v['entry'], c.v['tail'], c.v['process']) for c in cases if c.bundle == 'sta' and c.v['skind'] == 'main' and c.v['life'] == 'running'},
                         {(e, t, p) for e, t, p in itertools.product(AXES['entry'], AXES['tail'], AXES['process']) if not (e == 'sdk' and t in ('commands', 'commands_only'))})
        b = types.SimpleNamespace(ids={}, t0=0)

        def reason(bundle, subject, field, ws, gs, **v):
            c = axes.normalize(Case(bundle, v))
            return run.reason_of(run.Cell(c, subject, field, 'wrong', ws, gs, b))
        self.assertEqual(reason('room', 'p1', 'seat', None, 'A', rtime='sequential'), 'R-ROOM-TIMING')
        self.assertEqual(reason('room', 'p1', 'seat', None, 'A', code='all'), 'R-ROOM-CODE')
        self.assertEqual(reason('room', 'p1', 'seat', None, 'A', cite='fence'), 'R-ROOM-CITE')
        self.assertEqual(reason('room', 'p1', 'seat', None, 'A', cite='fence', shape='r1'), 'R-ROOM-CITE')
        self.assertEqual(reason('room', 'p1', 'unit', None, 'x', shape='none', talk='peer', delivery='failed'), 'R-ROOM-MSG-FAIL')
        self.assertEqual(reason('room', 'p1', 'seat', None, 'A', shape='none', talk='peer'), 'R-ROOM-MEMBERS')
        self.assertEqual(reason('deb', 'child', 'seat', None, 'B', role='quoter', qform='fence'), 'B-CITE')
        self.assertEqual(reason('deb', 'child', 'seat', None, 'B', role='quoter'), 'B-QUOTE')
        for shape, want in ((dict(process='gone', tail='mid'), 'S-ORCH-DEAD'), (dict(tail='commands'), 'S-ORCH-COMMANDS'), (dict(entry='sdk', tail='end'), 'S-ORCH-SDK-END'),
                            (dict(), 'S-ORCH-STATE')):
            self.assertEqual(reason('sta', 'orch', 'orch_state', 'idle', 'working', skind='main', life='running', **shape), want, shape)
        for r in ('R-ROOM-TIMING', 'R-ROOM-CODE', 'R-ROOM-CITE', 'R-ROOM-MSG-FAIL', 'B-CITE', 'S-ORCH-DEAD', 'S-ORCH-COMMANDS', 'S-ORCH-SDK-END'):
            self.assertIn(r, run.REASONS)


class StrictXfail(unittest.TestCase):
    """The red list of the board as it is today. See the module docstring for how to change it."""

    def test_red_cells_are_exactly_the_listed_ones(self):
        cases = run.select()
        t0 = time.time()
        cells, errors = run.run_all(cases)
        dt = time.time() - t0
        skipped = run.skipped_here(cases)
        sys.stderr.write('\n' + run.summary_text(cases, cells, errors, dt, skipped) + '\n')
        self.assertEqual(errors, [], 'the generator itself failed on a case')
        self.assertLess(dt, BUDGET_SEC, 'the scenario suite must stay under %d s' % BUDGET_SEC)
        with open(os.path.join(REPO, 'tests', 'scenarios_xfail.json')) as f:
            doc = json.load(f)
        problems = run.compare_xfail(cells, doc, skipped)
        self.assertEqual(problems[:20], [], '%d differences from tests/scenarios_xfail.json (first 20 shown); regenerate with --write-xfail after a deliberate change' % len(problems))
        # every red cell has a reason that names an owner
        for cid, cs in doc['cells'].items():
            for key, (result, want, got, reason) in cs.items():
                self.assertIn(reason, doc['reasons'], '%s %s' % (cid, key))
        modules = {os.path.splitext(n)[0] for n in os.listdir(os.path.join(REPO, 'board')) if n.endswith('.py')}
        for rid, r in doc['reasons'].items():
            self.assertTrue(r['module'] and set(r['module'].split('/')) <= modules, rid)           # each reason names the board modules to look at
            self.assertTrue(r['text'], rid)

    def test_the_run_is_deterministic(self):
        """Two runs in different folders give the same cells: ids, times and values come from the case id, never from the folder, the clock or the umask."""
        cases = run.select()[::7]
        with tempfile.TemporaryDirectory(prefix='scen-a-') as ra, tempfile.TemporaryDirectory(prefix='scen-b-') as rb:
            a, ea = run.run_all(cases, ra)
            b, eb = run.run_all(cases, rb)
        self.assertEqual((ea, eb), ([], []))
        key = lambda cells: sorted((c.case, c.role, c.field, c.result, repr(c.ws), repr(c.gs)) for c in cells)
        self.assertEqual(key(a), key(b))


if __name__ == '__main__':
    unittest.main()
