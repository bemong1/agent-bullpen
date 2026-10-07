"""The scenario generator (tools/scenarios): axes -> synthetic HOME -> truth (oracle) -> what the board shows (observe) -> pass / miss / wrong.

The strict xfail list (tests/scenarios_xfail.json) is exactly the set of red cells of the board as it is today against the truth of the 0.3.0 contract (tools/scenarios/contract.py,
whose own check is the table tests/data/contract_cases.json): a red cell that is not listed, a listed cell that now passes, a listed cell whose value changed, a stale entry: each
fails here. Each red cell carries the line of the contract it is about (J1, J5, J11 ...), so fixing the board turns cells green line by line; the fixer then regenerates the list with
`python3 -m tools.scenarios.run --write-xfail` and the diff shows which cells moved. The table of red cells comes from `python3 -m tools.scenarios.run --md`, never from a test.

    python3 -m unittest discover -s tests
"""
import contextlib
import copy
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

from tools.scenarios import axes, build, contract, cx_record, observe, oracle, plan, run, scene_cxo  # noqa: E402
from tools.scenarios.axes import AXES, BUNDLES, Case, WAY  # noqa: E402
from tools.scenarios.observe import MISSING  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A guard against the generator growing without notice, not a speed test: about 15 s on a developer machine, roughly twice that on a
# shared CI runner. AGENT_BULLPEN_SCENARIO_BUDGET overrides it for a slower machine.
BUDGET_SEC = float(os.environ.get('AGENT_BULLPEN_SCENARIO_BUDGET') or 60.0)


class CodexRecordShapes(unittest.TestCase):
    """The Codex rollout writer (tools/scenarios/cx_record.py) writes the shapes the real 0.153-0.160 records have (checked against real records; nothing real is stored here)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-cx-')
        cid = 'case'
        cls.cid = cid
        top_id, sub_id, g_id = (cx_record.tid_of(cid, r) for r in ('top', 'sub', 'guard'))
        cls.top = top = cx_record.Rollout(cx_record.rollout_path(cls.root, top_id, 1790000000), top_id, '/w/repo', cid, origin='codex-tui')
        top.meta(1790000000)
        top.task_started(1790000000.5, 'turn-top')
        top.user(1790000001, 'Do the work.', 'turn-top')
        top.shell(1790000002, 'echo hi', '/w/repo', 'call-1', 'turn-top', end=1790000003, out_at=1790000003.05, pid=77)
        top.shell(1790000004, 'sleep 99', '/w/repo', 'call-2', 'turn-top', end=None, out_at=1790000005, running=True)
        top.spawn(1790000010, 'call-spawn', 'turn-top', 's1', sub_id, '/root/s1')
        top.spawn(1790000011, 'call-bad', 'turn-top', 'S 1', sub_id, '/root/s1', ok=False)
        cls.sub = sub = cx_record.Rollout(cx_record.rollout_path(cls.root, sub_id, 1790000010.06), sub_id, '/w/repo', cid, kind='sub', parent=top_id, agent_path='/root/s1', nick='Atlas',
                                          depth=1, root_id=top_id)
        sub.fork(1790000010.06, top, 'turn-top', 'Do the work.', 1790000000)
        sub.task_started(1790000010.07, 'turn-sub')
        sub.agent_message(1790000010.2, '/root', '/root/s1', 'Message from /root: your task is in the attached content.', cipher=True, turn='turn-sub')
        sub.usage(1790000012, 'turn-sub', 100, 10)
        sub.complete(1790000020, 'turn-sub', 'Done.')
        top.sub_completed(1790000020.005, 'turn-top', 'turn-sub', sub_id, '/root/s1')
        top.agent_message(1790000020.01, '/root/s1', '/root', 'Done.', msg_id='m1', turn='turn-top')
        top.interrupt(1790000030, 'call-int', 'turn-top', sub_id, '/root/s1')
        cls.guard = g = cx_record.Rollout(cx_record.rollout_path(cls.root, g_id, 1790000005), g_id, '/w/repo', cid, kind='guardian', parent=top_id, root_id=top_id)
        g.meta(1790000005)
        for r in (top, sub, g):
            r.save()
        cls.lines = {k: _read_lines(r.path) for k, r in (('top', top), ('sub', sub), ('guard', g))}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def items(self, which, kind):
        return [d['payload']['item'] for d in self.lines[which] if d['type'] == 'event_msg' and d['payload'].get('type') == 'item_completed' and d['payload']['item']['type'] == kind]

    def test_every_line_has_the_key_order_and_ordinal_of_the_real_ones(self):
        from board import codex_parse
        for which, rows in self.lines.items():
            for i, d in enumerate(rows):
                self.assertEqual(list(d)[:3], ['timestamp', 'ordinal', 'type'], (which, i))
                self.assertEqual(d['ordinal'], i)
        for which in ('top', 'sub'):
            with open({'top': self.top, 'sub': self.sub}[which].path, 'rb') as f:
                for raw in f:
                    self.assertTrue(codex_parse.CX_HEAD_RE.match(raw), raw[:80])      # the board reads lines by their head

    def test_roots_have_a_string_source_and_a_sub_agent_names_its_parent_in_three_places(self):
        m = self.lines['top'][0]['payload']
        self.assertEqual((m['source'], m['thread_source'], m['originator']), ('cli', 'user', 'codex-tui'))
        self.assertNotIn('parent_thread_id', m)
        s = self.lines['sub'][0]['payload']
        spawn = s['source']['subagent']['thread_spawn']
        self.assertEqual((spawn['parent_thread_id'], spawn['depth'], spawn['agent_path']), (self.top.tid, 1, '/root/s1'))
        self.assertEqual((s['parent_thread_id'], s['thread_source'], s['agent_path'], s['agent_nickname']), (self.top.tid, 'subagent', '/root/s1', 'Atlas'))
        self.assertEqual(s['session_id'], self.top.tid)                                       # the session is the root's, the id the sub-agent's own
        self.assertEqual(s['id'], self.sub.tid)
        g = self.lines['guard'][0]['payload']
        self.assertEqual((g['source'], g['thread_source'], g['parent_thread_id']), ({'subagent': {'other': 'guardian'}}, 'guardian_review', self.top.tid))

    def test_a_sub_agents_rollout_starts_with_the_front_part_of_its_parent(self):
        rows = self.lines['sub']
        n = rows[0]['payload']['subagent_history_start_ordinal']
        self.assertEqual(n, cx_record.FORK_LINES)
        front, own = rows[1:n + 1], rows[n + 1:]
        self.assertEqual([d['ordinal'] for d in front], list(range(1, n + 1)))
        self.assertEqual(len({d['timestamp'] for d in front + rows[:1]}), 1)                  # every line of it is stamped with the moment of the spawn
        self.assertEqual(front[0]['type'], 'session_meta')
        self.assertEqual(front[0]['payload']['id'], self.top.tid)                             # the parent's own meta, not a second meta of the sub-agent
        self.assertEqual(front[1]['payload']['type'], 'task_started')
        self.assertEqual(front[1]['payload']['turn_id'], 'turn-top')
        users = [d for d in front if d['type'] == 'response_item' and d['payload'].get('role') == 'user']
        self.assertEqual([d['payload']['content'][0]['text'] for d in users], ['Do the work.'])
        self.assertTrue(users[0]['metadata']['inherited_user_message'])
        self.assertFalse([d for d in front if 'token' in d['type'] or d['payload'].get('type') == 'token_count'])       # no token count in it
        self.assertEqual((own[0]['ordinal'], own[0]['payload']['type'], own[0]['payload']['turn_id']), (n + 1, 'task_started', 'turn-sub'))
        self.assertFalse([d for d in own if d['type'] == 'response_item' and d['payload'].get('role') == 'user'])    # nothing of the sub-agent's own says `user`

    def test_the_first_message_from_the_parent_is_a_notice_beside_ciphertext(self):
        msgs = [d['payload'] for d in self.lines['sub'] if d['type'] == 'response_item' and d['payload'].get('type') == 'agent_message']
        first = msgs[0]
        self.assertEqual((first['author'], first['recipient']), ('/root', '/root/s1'))
        self.assertEqual([p['type'] for p in first['content']], ['input_text', 'encrypted_content'])
        self.assertLess(len(first['content'][0]['text']), 80)
        self.assertIn('/root', first['content'][0]['text'])
        self.assertTrue(first['content'][1]['encrypted_content'].startswith('gAAAAA'))

    def test_a_command_is_told_once_when_its_process_ends(self):
        done = self.items('top', 'CommandExecution')
        self.assertEqual(len(done), 1)                                                       # `sleep 99` is still running: no such line yet
        c = done[0]
        self.assertEqual(c['command'], ['/bin/bash', '-lc', 'echo hi'])
        self.assertEqual((c['cwd'], c['process_id'], c['status'], c['exit_code']), ('file:///w/repo', '77', 'completed', 0))
        self.assertEqual(set(c['duration']), {'secs', 'nanos'})
        calls = [d['payload'] for d in self.lines['top'] if d['type'] == 'response_item' and d['payload'].get('type') == 'custom_tool_call']
        self.assertEqual([x['name'] for x in calls], ['exec', 'exec'])
        self.assertIn('tools.exec_command(', calls[0]['input'])                              # the JavaScript, which nothing may read as the command
        outs = [d['payload']['output'][0]['text'] for d in self.lines['top'] if d['type'] == 'response_item' and d['payload'].get('type') == 'custom_tool_call_output']
        self.assertEqual(outs, ['Script completed', 'Script running with cell ID 7'])

    def test_sub_agent_activity_follows_the_spawn_call_and_the_end_of_the_sub_agent(self):
        acts = self.items('top', 'SubAgentActivity')
        self.assertEqual([(a['kind'], a['agent_path']) for a in acts], [('started', '/root/s1'), ('completed', '/root/s1'), ('interrupted', '/root/s1')])     # the failed spawn has none
        self.assertEqual(acts[0]['id'], 'call-spawn')                                        # the id of the call
        self.assertEqual(acts[1]['id'], 'subagent-completed-turn-sub')
        self.assertEqual(acts[2]['id'], 'call-int')
        calls = [d['payload'] for d in self.lines['top'] if d['type'] == 'response_item' and d['payload'].get('type') == 'function_call' and d['payload']['name'] == 'spawn_agent']
        self.assertEqual({c['namespace'] for c in calls}, {'collaboration'})
        self.assertEqual(json.loads(calls[0]['arguments']), {'task_name': 's1', 'message': cx_record.CIPHER})
        order = [(d['type'], d['payload'].get('type'), (d['payload'].get('item') or {}).get('kind')) for d in self.lines['top']]
        done = order.index(('event_msg', 'item_completed', 'completed'))
        self.assertEqual(order[done + 1][:2], ('response_item', 'agent_message'))               # `completed` first, the sub-agent's message after it

    def test_ids_are_made_from_the_case_and_the_role(self):
        self.assertEqual(cx_record.tid_of('a', 'top'), cx_record.tid_of('a', 'top'))
        self.assertNotEqual(cx_record.tid_of('a', 'top'), cx_record.tid_of('a', 'sub'))
        self.assertNotEqual(cx_record.tid_of('a', 'top'), cx_record.tid_of('b', 'top'))
        self.assertRegex(cx_record.tid_of('a', 'top'), r'^019a[0-9a-f]{4}-[0-9a-f]{4}-7[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$')


class OracleIsIndependent(unittest.TestCase):
    def test_oracle_and_axes_never_import_board(self):
        """The independence of the oracle: contract.py, oracle.py, plan.py and axes.py (what the truth is made of) load without the board. observe.py, the adapter that reads what the board
        shows, is the one file of tools/scenarios that does import it (CONTRACT O3)."""
        code = ('import sys; import tools.scenarios.oracle, tools.scenarios.axes, tools.scenarios.contract, tools.scenarios.plan; '
                'bad = [m for m in sys.modules if m == "board" or m.startswith("board.")]; '
                'sys.exit("board imported: %s" % bad if bad else 0)')
        r = subprocess.run([sys.executable, '-c', code], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_facts_module_is_definitions_only(self):
        import ast
        with open(os.path.join(REPO, 'board', 'facts.py')) as f:
            tree = ast.parse(f.read())
        allowed = {id(f) for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == 'LaunchKey' for f in c.body if isinstance(f, ast.FunctionDef) and f.name == 'gkey'}
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(n) not in allowed]
        self.assertEqual(funcs, [], 'board/facts.py defines shapes and enumerations only (and the one method LaunchKey.gkey, CONTRACT 2.1)')
        imported = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, {'re', 'dataclasses', 'typing'})


class ContractCases(unittest.TestCase):
    """`contract.truth` is the oracle of the 0.3.0 contract (CONTRACT.md J1-J20): the table of cases (tests/data/contract_cases.json, 3.1) is its own check, and the same function gives
    the truth of the structure axes of the scenario generator. The unit tests of the judgment read the same table, so the two readings of the contract cannot drift apart unseen."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, 'tests', 'data', 'contract_cases.json'), encoding='utf-8') as f:
            cls.table = json.load(f)

    def test_the_constants_are_the_tables(self):
        self.assertEqual(contract.CONSTS, self.table['consts'])

    def test_file_round_collision_truth_is_identical_across_hash_seeds(self):
        script = 'import json, sys; from tools.scenarios.contract import truth; print(json.dumps(truth(json.load(sys.stdin)), sort_keys=True))'
        for cid in ('C128', 'C129', 'C130', 'C135', 'C137', 'C138', 'C139', 'C140', 'C141'):
            c = next(c for c in self.table['cases'] if c['id'] == cid)
            results = []
            for seed in ('0', '1', '2', '7', '42'):
                with self.subTest(case=cid, seed=seed):
                    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE='1')
                    proc = subprocess.run([sys.executable, '-c', script], input=json.dumps(c), text=True, capture_output=True, cwd=REPO, env=env, check=True)
                    got = json.loads(proc.stdout)
                    results.append(proc.stdout)
                    self.assertEqual(contract.compare(got, c['expect']), [])
            with self.subTest(case=cid):
                self.assertEqual(len(set(results)), 1)

    def test_file_rounds_exclude_macos_scratch_roots_and_aliases(self):
        source = next(c for c in self.table['cases'] if c['id'] == 'C103')
        for folder, scratch, alias in (('private/tmp', None, None),
                                       ('private/var/folders/synthetic/T', ('private/var/folders/synthetic/T/',), None),
                                       ('private/tmp', ('tmp/',), 'tmp')):
            with self.subTest(folder=folder, alias=alias):
                c = copy.deepcopy(source)
                relocate = lambda p: folder + p[len('talk'):]  # noqa: E731
                c['disk']['files'] = {relocate(p): meta for p, meta in c['disk']['files'].items()}
                c['disk']['dirs'] = [folder]
                if alias:
                    c['disk']['links'] = {alias: folder}
                for a in c['agents']:
                    a['launch'] = None
                    for w in a['writes']:
                        w['path'] = (alias or folder) + w['path'][len('talk'):]
                with mock.patch.object(contract, 'SCRATCH_DIRS', scratch or contract.SCRATCH_DIRS):
                    self.assertEqual(contract.truth(c)['listed'], [])

    def test_every_case_and_every_step_is_reproduced(self):
        bad, compared = [], 0
        for c in self.table['cases']:
            for k, inp, want in contract.steps(c):
                compared += len(want)
                bad += ['%s step %d: %s' % (c['id'], k, key) for key, _g, _w in contract.compare(contract.truth(inp), want)]
        self.assertEqual(bad, [], 'the oracle and the table differ: a difference is a question for the contract, the table is not edited here')
        self.assertGreater(compared, 200)

    def differences(self, judge):
        """The ids of the cases (and steps) a judge that is not the contract's reproduces wrongly."""
        bad = set()
        for c in self.table['cases']:
            for k, inp, want in contract.steps(c):
                try:
                    got = judge(copy.deepcopy(inp)).truth()                             # (a judge may take things out of its input)
                except Exception:                                               # noqa: BLE001  (a rule taken out may break another: that is a difference as well)
                    bad.add('%s/%d' % (c['id'], k))
                    continue
                if contract.compare(got, want):
                    bad.add('%s/%d' % (c['id'], k))
        return bad

    def test_the_table_catches_a_change_of_rule(self):
        """The table is only a check if it notices a rule that is not the contract's. Each mutation stands for a row of the table at the end of CONTRACT 4.5 (the rule put back to an
        earlier or a looser shape) and the cases that row names must be among the ones that differ."""
        class NoStartBoundary(contract.Judge):
            def fresh(self, w):                                                   # a save just before the start of the run counts (the 2nd revision's slack)
                meta = self.files.get(w['path'])
                return bool(meta and w.get('span') and w['span'][0] - self.EPS <= meta['mtime'] <= w['span'][1] + self.GRACE and (w['out'] is None or meta.get('body') == w['out']))

        class NoSaveCheck(contract.Judge):
            def confirmed(self, w):                                               # a checked event is confirmed without looking at the disk
                return w['ok'] is True

        class FailureHoldsNothing(contract.Judge):
            def unsure_attempt(self, w):                                          # a failed attempt is "did not write"
                return w['kind'] in contract.AUTHORING and not w['confirmed'] and w['ok'] is not False

        class NoUnsureHold(contract.Judge):
            def unsure_attempt(self, w):                                          # an earlier attempt that is not confirmed holds nothing
                return False

        class LastAttemptIsLastConfirmed(contract.Judge):
            def last_attempt(self, paths):                                        # La = Lc: only a confirmed write takes a conclusion back
                return self.last_confirmed(paths)

        class DiskRemovesRivals(contract.Judge):
            def rivals(self, kind, scope, paths, lc):                             # a rival that is gone from the disk is no rival (rivals within C)
                return [p for p in super().rivals(kind, scope, paths, lc) if p in self.files and self.files[p]['size'] > 0]

        class LaunchWindowIsRival(contract.Judge):
            def window_hint(self, p):                                             # O2 not applied: the window of the call that launched the agent is a rival of its own
                meta = self.files.get(p)
                if not meta:
                    return None
                m = meta['mtime']
                hi = m + 1 if meta.get('coarse') else m
                cands = []
                for a in self.agents + [dict(id='orch', windows=self.orch['windows'], windows_capped=None)]:
                    for w in a['windows']:
                        t1 = w['t1'] if w['t1'] is not None else float('inf')
                        if w['t0'] <= hi + self.EPS and t1 >= m - self.EPS and p not in [self.real(r) for r in w['reads']]:
                            cands.append((a['id'], w))
                return {'kind': 'window', 'agent': cands[0][0]} if len(cands) == 1 and cands[0][0] != 'orch' and cands[0][1]['ok'] is True else None

        class RoomInTheFirstFolderByPath(contract.Judge):
            def _rooms(self):                                                     # O1 not applied: "F or one below", the first F by path
                rooms, taken = {}, set()
                groups = {}
                for a in self.agents:
                    if a['launch']:
                        groups.setdefault(contract.gkey(a['launch']), []).append(a)
                for g, ms in sorted(groups.items(), key=lambda kv: str(kv[0])):
                    own = {a['id']: self.own_files(a) for a in ms}
                    cand = sorted({x for ps in own.values() for p in ps for x in (contract.parent(p), contract.parent(contract.parent(p)))})
                    for f in cand:
                        mine = [a for a in ms if a['id'] not in taken and any(contract.in_reach(p, f) for p in own[a['id']])]
                        if len(mine) >= 2 and not (self.is_common(f) or self.round_dirs_of.get(f) or self.unit_below(f) or f in rooms):
                            files = {a['id']: next(p for p in own[a['id']] if contract.in_reach(p, f)) for a in mine}
                            rooms[f] = dict(kind='cells', sure=False, why='launch', members=sorted(files), files=sorted(files.values()))
                            taken |= set(files)
                return rooms, {f: r['files'] for f, r in rooms.items()}

        rows = (('O1 방의 폴더 = 구성원 자기 .md가 직접 든 폴더(옛: 경로순 첫 F)', RoomInTheFirstFolderByPath, {'C85', 'C86'}),
                ('O2 힌트: 띄운 호출의 창은 경쟁이 아님', LaunchWindowIsRival, {'C84'}),
                ('3차 J1 F 시작 쪽 −WIN_EPS', NoStartBoundary, {'C73'}),
                ('J1 F 없이 CHECKED 사건 확정', NoSaveCheck, {'C15', 'C61', 'C62', 'C63', 'C64/2', 'C73', 'C74', 'C75', 'C76', 'C80'}),
                ('4차 J3 ② 실패는 보류 안 함', FailureHoldsNothing, {'C83'}),
                ('J3 ② 앞선 미확인 시도 보류 없음', NoUnsureHold, {'C62', 'C83'}),
                ('3차 J15 L = 확정 사건만(La = Lc)', LastAttemptIsLastConfirmed, {'C74', 'C76', 'C81'}),
                ('4차 J15 디스크가 경쟁 후보를 뺌', DiskRemovesRivals, {'C79'}))
        for row, judge, must in rows:
            got = {x for x in self.differences(judge)}
            got |= {x.split('/')[0] for x in got}                                  # a case named without a step is any of its steps
            self.assertTrue(must <= got, '%s: the table should catch it in %s, it caught %s' % (row, sorted(must), sorted(got)))
        self.assertEqual(self.differences(contract.Judge), set())

    def test_the_table_has_unique_ids_and_only_the_keys_of_the_format(self):
        ids = [c['id'] for c in self.table['cases']]
        self.assertEqual(len(ids), len(set(ids)))
        known = {'listed', 'hinted', 'current', 'cells', 'paths', 'placed', 'rooms', 'finals', 'closable', 'diag'}
        for c in self.table['cases']:
            self.assertLessEqual(set(c['expect']), known, c['id'])
            for t in c['then']:
                self.assertLessEqual(set(t['expect']), known, c['id'])

    def test_the_table_holds_nothing_personal(self):
        text = json.dumps(self.table, ensure_ascii=False)
        self.assertNotRegex(text, r'/home/|/Users/|@[A-Za-z0-9-]+\.[a-z]|\b\d{1,3}(?:\.\d{1,3}){3}\b|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-')

    def test_the_truth_is_pure(self):
        c = next(x for x in self.table['cases'] if x['id'] == 'C71')
        before = json.dumps(c, sort_keys=True)
        one = contract.truth(c)
        self.assertEqual(json.dumps(c, sort_keys=True), before)
        self.assertEqual(json.dumps(contract.truth(c), sort_keys=True), json.dumps(one, sort_keys=True))
        for _k, inp, _w in contract.steps(c):
            contract.truth(inp)
        self.assertEqual(json.dumps(c, sort_keys=True), before)

    def test_the_truth_follows_the_structure_and_nothing_else(self):
        """A conclusion needs a write of the orchestrator's or an agent's that proves it; take it away and the final is unconfirmed, give it back as a copy (no event) and it still is."""
        c = copy.deepcopy(next(x for x in self.table['cases'] if x['id'] == 'C48'))
        self.assertTrue(contract.truth(c)['finals']['talk']['confirmed'])
        c['agents'] = [dict(a, writes=[w for w in a['writes'] if not w['path'].endswith('ruling.md')]) for a in c['agents']]
        t = contract.truth(c)['finals']['talk']
        self.assertFalse(t['confirmed'])
        self.assertEqual(t['why'], ['none'])
        self.assertEqual(t['candidates'], ['talk/ruling.md'])             # the file is still a candidate: the disk shows it, no event proves it

    def test_a_failed_write_makes_no_cell_and_an_unknown_one_makes_no_owner(self):
        t = contract.truth(next(x for x in self.table['cases'] if x['id'] == 'C02'))
        self.assertEqual((t['listed'], t['cells']), ([], {}))
        t = contract.truth(next(x for x in self.table['cases'] if x['id'] == 'C03'))
        cell, = t['cells'].values()
        self.assertEqual((cell['owner'], cell['state'], cell['previous']), (None, 'previous', True))


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
        case = dict(rdir='both', fstate='written', kind='sub', life='running')
        got = self.graded([('B', 'draft')], **case)
        self.assertEqual({k: v for k, v in got.items() if v != 'pass' and not k.startswith('diag:')}, {})      # the right file, the right state
        got = self.graded([('B', 'draft')], folder='r01', **case)
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
        self.assertTrue(700 <= len(ids) <= 3300, len(ids))

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
        for bundle in ('aff', 'deb', 'sta', 'cpl', 'room', 'cxo'):
            names = run.PAIR_AXES[bundle]
            covered = set()
            for c in (c for c in self.cases if c.bundle == bundle and c.twin_of is None):
                for a, b in itertools.combinations(names, 2):
                    covered.add((a, c.v[a], b, c.v[b]))
            rnd = random.Random(11)
            for a, b in itertools.combinations(names, 2):
                for x in run.pair_values(a):
                    for y in run.pair_values(b):
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


class NoiseAxes(unittest.TestCase):
    """The axes that only change words (CONTRACT 3.3, layer L3) are noise: a case that differs from another in them alone holds the same facts and has the same truth. The oracle
    never reads them (it reads plan.py's facts, which have no word in them), and BuilderMatchesPlan shows that the records hold exactly those facts."""

    DEB = {'lang': ('en', 'ko'), 'marker': ('none', 'own', 'cross', 'quoted'), 'decl': ('yes', 'no'), 'qform': AXES['qform'], 'rpath': ('abs', 'tilde', 'short', 'folder', 'dotdot', 'instr_only')}
    ROOM = {'word': AXES['word'], 'lang': AXES['lang'], 'seatmark': AXES['seatmark'], 'ref': AXES['ref'], 'wrap': AXES['wrap']}

    @staticmethod
    def variants(c, noise):
        for axis, values in noise.items():
            if c.v[axis] not in values:
                continue                                                     # (a path form that is a launch command's output, say, is structure for this case)
            for value in values:
                n = axes.normalize(Case(c.bundle, dict(c.v, **{axis: value})))
                if n.key() != c.key() and all(n.v[a] == c.v[a] for a in c.axes if a not in noise):
                    yield n

    def facts(self, c):
        if c.bundle == 'room':
            return plan.room_scene(c.v).F.case()
        v = dict(c.v)
        return plan.deb_scene(v, oracle.life_state(v['kind'], v['life'], 'just_ended', v['os'] == 'mac_nops')[0]).F.case()

    def test_the_words_change_neither_the_facts_nor_the_truth(self):
        compared = 0
        for c in run.select():
            if c.bundle not in ('deb', 'room') or c.twin_of or int(axes.digest(c.id, n=4), 16) % 3:
                continue
            base = (self.facts(c), oracle.truth(c))
            for n in self.variants(c, self.DEB if c.bundle == 'deb' else self.ROOM):
                self.assertEqual(self.facts(n), base[0], (c.id, n.id))
                t = oracle.truth(n)
                shown = lambda truth: ({r: {k: v for k, v in f.items() if k != 'titles'} for r, f in truth.subjects.items()}, truth.diag)      # noqa: E731  (a title is displayed text: the language shows)
                self.assertEqual(shown(t), shown(base[1]), (c.id, n.id))
                compared += 1
        self.assertGreater(compared, 400)

    def test_the_words_change_nothing_the_board_shows_either(self):
        """The same, read from the board (review P2-6): two cases that differ in words alone leave the board with the same answer in every cell it is asked about (the titles it
        prints are text and are not compared)."""
        root = tempfile.mkdtemp(prefix='scen-noise-')

        def reading(c):
            return {(x.role, x.field): x.gs for x in run.run_case(c, root) if x.field != 'titles'}
        try:
            compared, bad = 0, []
            for c in run.select():
                if c.bundle not in ('deb', 'room') or c.twin_of or int(axes.digest(c.id, n=4), 16) % 12:
                    continue
                base = reading(c)
                for n in self.variants(c, self.DEB if c.bundle == 'deb' else self.ROOM):
                    got = reading(n)
                    compared += 1
                    if got != base:
                        bad.append((c.id, n.id, sorted(k for k in set(base) | set(got) if base.get(k) != got.get(k))))
            self.assertGreater(compared, 500)
            self.assertEqual(bad[:3], [], '%d of %d pairs differ on the board' % (len(bad), compared))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_structure_axis_does_change_them(self):
        """The control: the axes that are not words change the facts (so the test above is not vacuous)."""
        base = axes.normalize(Case('deb', dict(role='writer', kind='sub', fstate='written', life='normal_end')))
        changed = 0
        for axis, value in (('wmode', 'redirect'), ('role', 'failed_write'), ('fstate', 'none'), ('life', 'running'), ('kind', 'cli'), ('rdir', 'both')):
            n = axes.normalize(Case('deb', dict(base.v, **{axis: value})))
            changed += self.facts(n) != self.facts(base)
        self.assertEqual(changed, 6)
        room = axes.normalize(Case('room', {}))
        for axis, value in (('launch', 'far'), ('proof', 'told'), ('shape', 'same'), ('rtime', 'sequential'), ('delivery', 'failed'), ('scratch', 'log')):
            n = axes.normalize(Case('room', dict(room.v, **{axis: value})))
            self.assertNotEqual(oracle.truth(n).subjects, oracle.truth(room).subjects, axis) if axis != 'delivery' else None


class BuilderMatchesPlan(unittest.TestCase):
    """The plan (tools/scenarios/plan.py) says, from the axis values, what the records of a debate, room or Codex-orchestrator scene hold: who wrote which file by which tool, who read
    which, which launch command names which output, who was launched together, which files are on disk. The builder writes the records; they must hold exactly that, since the
    truth is the contract's judgment of the plan. Built only, the board is not run."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-plan-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    @staticmethod
    def claude_calls(path):
        """[(tool name, input, is_error or None)] of a Claude record, in order."""
        rows = _read_lines(path)
        errors = {blk['tool_use_id']: blk.get('is_error') for d in rows if d.get('type') == 'user' and isinstance(d['message'].get('content'), list)
                  for blk in d['message']['content'] if blk.get('type') == 'tool_result'}
        return [(blk['name'], blk['input'], errors.get(blk['id'])) for d in rows if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use']

    @staticmethod
    def codex_items(path):
        return [d['payload']['item'] for d in _read_lines(path) if d.get('type') == 'event_msg' and d['payload'].get('type') == 'item_completed']

    def check(self, c, b, scene, top, scratch):
        """The records of the case `c` (built as `b`) against the facts of `scene`; `top` is what the facts' paths hang from, `scratch` the folder `tmp/scratch` of the facts stands for."""
        facts = scene.F
        where = lambda rel: os.path.join(scratch, rel[len('tmp/scratch/'):]) if rel.startswith('tmp/scratch/') else os.path.join(top, rel)      # noqa: E731
        for aid, a in facts.agents.items():
            if aid not in b.paths:
                continue
            rec = b.paths[aid]
            tool_writes = sorted(where(w['path']) for w in a['writes'] if w['evidence'] == 'tool')
            shell_writes = [w for w in a['writes'] if w['evidence'] == 'shell']
            if a['provider'] == 'codex':
                items = self.codex_items(rec)
                got = sorted(p for it in items if it['type'] == 'FileChange' and it.get('status') == 'completed' for p in it['changes'])
                self.assertEqual(got, tool_writes, (c.id, aid, 'a patch'))
                reads = sorted(where(r['path']) for r in a['reads'])
                seen = sorted(p['path'] for it in items if it['type'] == 'CommandExecution' for p in it.get('parsed_cmd', []) if p.get('type') == 'read')
                self.assertEqual(seen, reads, (c.id, aid, 'reads'))
                continue
            calls = self.claude_calls(rec)
            self.assertEqual(sorted(i['file_path'] for n, i, _ in calls if n in ('Write', 'Edit', 'MultiEdit')), tool_writes, (c.id, aid, 'Write'))
            self.assertEqual(sorted(i['file_path'] for n, i, _ in calls if n == 'Read' and not i['file_path'].endswith('notes.md')),
                             sorted(where(r['path']) for r in a['reads'] if r['via'] == 'tool'), (c.id, aid, 'Read'))
            for w in shell_writes:
                name = os.path.basename(w['path'])
                hit = [e for n, i, e in calls if n == 'Bash' and name in i['command']]
                self.assertTrue(hit, (c.id, aid, 'a command that writes', w['path']))
                self.assertEqual(bool(hit[0]), w['ok'] is False, (c.id, aid, w['path']))                       # the command failed exactly when the facts say it did
            fails = [w for w in a['writes'] if w['ok'] is False and w['evidence'] in ('tool', 'shell')]
            if c.bundle == 'ctr':                                                                # every command is a window
                self.assertEqual(sum(1 for n, i, _ in calls if n == 'Bash'), len(a['windows']), (c.id, aid, 'command windows'))
            self.assertEqual(sum(1 for n, i, e in calls if e and n in ('Write', 'Bash')), len(fails), (c.id, aid, 'failed writes'))
        # the launch commands name the outputs the facts say they do (the commands of the orchestrator's own record)
        if c.bundle in ('deb', 'cpl') and b.main_path.endswith('.jsonl'):
            launch = [i['command'] for rec in b.paths.values() if rec.endswith('.jsonl') and 'rollout' not in rec for n, i, _ in self.claude_calls(rec)
                      if n == 'Bash' and ('claude -p' in i['command'] or 'codex exec' in i['command'])]
            for aid, a in facts.agents.items():
                for q in a['planned']:
                    if not q['path'].startswith('tmp/'):
                        self.assertTrue(any(os.path.basename(q['path']) in cmd for cmd in launch), (c.id, aid, q['path'], launch))
        # the files of the repository
        on_disk = set()
        for dirpath, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d != '.git']
            on_disk |= {os.path.relpath(os.path.join(dirpath, f), top) for f in files}
        want = {rel for rel in facts.files if not rel.startswith('tmp/')}
        extra = {f for f in on_disk - want if f != 'notes.md'}
        self.assertEqual((sorted(want - on_disk), sorted(extra)), ([], []), c.id)
        for rel, meta in facts.files.items():
            path = where(rel)
            if os.path.isfile(path):
                self.assertAlmostEqual(os.path.getmtime(path), b.T(meta['mtime']), delta=0.01, msg=(c.id, rel))
                self.assertEqual(os.path.getsize(path) > 0, meta['size'] > 0, (c.id, rel))
                if 'body' in meta:
                    with open(path) as f:
                        self.assertEqual(f.read(), meta['body'], (c.id, rel))

    def test_the_debate_scenes(self):
        n = 0
        for c in run.select():
            if c.bundle not in ('deb', 'cpl') or int(axes.digest(c.id, n=4), 16) % 4:
                continue
            b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))
            v = dict(c.v) if c.bundle == 'deb' else dict(c.v, kind='cli', role='writer', nstyle='plain', structure='single', rdir='r1', homonym='none', decl='yes')
            status = oracle.life_state(v['kind'], v['life'], 'just_ended', v['os'] == 'mac_nops')[0]
            scene = plan.deb_scene(v, status)
            self.check(c, b, scene, b.meta['repo'], b.scratch)
            shutil.rmtree(os.path.join(self.root, axes.digest(c.id, n=10)), ignore_errors=True)
            n += 1
        self.assertGreater(n, 100)

    def test_the_room_scenes(self):
        n = 0
        for c in run.select():
            if c.bundle != 'room' or int(axes.digest(c.id, n=4), 16) % 4:
                continue
            b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))
            scene = plan.room_scene(c.v)
            self.check(c, b, scene, b.work, os.path.join(b.work, 'scratch'))
            # launched together = the Agent calls are lines of one message; each in a message of its own, or in a line with no message id
            msgs = [d['message'].get('id') for d in _read_lines(b.paths['orch']) if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == 'Agent'
                    and blk['input']['description'] != 'look around']
            groups = [scene.F.agents['p%d' % (i + 1)]['launch'] and scene.F.agents['p%d' % (i + 1)]['launch']['group'] for i in scene.page]
            self.assertEqual(len(msgs), len(groups), c.id)
            for (m1, g1), (m2, g2) in itertools.combinations(zip(msgs, groups), 2):
                self.assertEqual((m1 is not None and m1 == m2), (g1 is not None and g1 == g2), (c.id, m1, m2, g1, g2))
            shutil.rmtree(os.path.join(self.root, axes.digest(c.id, n=10)), ignore_errors=True)
            n += 1
        self.assertGreater(n, 150)

    def test_the_contract_scenes(self):
        n = 0
        for c in run.select():
            if c.bundle != 'ctr':
                continue
            b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))
            scene = plan.ctr_scene(c.v)
            self.check(c, b, scene, b.meta['repo'], b.scratch)
            orch_cmds = sum(1 for nm, i, _ in self.claude_calls(b.paths['orch']) if nm == 'Bash')
            self.assertEqual(orch_cmds, len(scene.F.orch['windows']), (c.id, 'the commands of the orchestrator'))
            msgs = {}
            for d in _read_lines(b.paths['orch']):
                if d.get('type') == 'assistant':
                    for blk in d['message']['content']:
                        if blk.get('type') == 'tool_use' and blk['name'] in ('Agent', 'Bash') and ('launch' in blk['input'].get('description', '') or blk['name'] == 'Agent'):
                            desc = blk['input'].get('description', '')
                            role = desc.split()[0] if blk['name'] == 'Agent' else 'S'
                            msgs[role] = d['message'].get('id')
            if c.v['launch'] == 'call':
                msgs['P'] = msgs['S']                                                                     # one Bash call starts both
            ids = [r for r in scene.F.agents if r in msgs]
            for ra, rb in itertools.combinations(ids, 2):
                ga, gb = scene.F.agents[ra]['launch'], scene.F.agents[rb]['launch']
                same_plan = bool(ga and gb and ga['group'] == gb['group'])
                same_records = msgs[ra] is not None and msgs[ra] == msgs[rb]
                self.assertEqual(same_records, same_plan, (c.id, ra, rb, msgs[ra], msgs[rb]))
            shutil.rmtree(os.path.join(self.root, axes.digest(c.id, n=10)), ignore_errors=True)
            n += 1
        self.assertGreater(n, 100)

    def test_the_codex_orchestrator_debate_scenes(self):
        n = 0
        for c in run.select():
            if c.bundle != 'cxo' or c.v['topic'] == 'none' or oracle.cxo_truth(c).subjects.get('child', {}).get('listed') == 'none':
                continue
            b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))
            self.check(c, b, plan.cxo_talk_scene(c.v), b.meta['repo'], b.scratch)
            shutil.rmtree(os.path.join(self.root, axes.digest(c.id, n=10)), ignore_errors=True)
            n += 1
        self.assertGreater(n, 10)


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
        self.assertEqual(seat('written'), 'B_gate')                                                       # the file it wrote, under the name it wrote it
        self.assertIsNone(seat('none'))                                                                   # no write: nothing seats it, whatever files of its letter are there

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

    def test_a_write_names_the_folder_whatever_the_instruction_fits(self):
        """Two debates fit the relative path of the instruction; the write that was made names its folder, and no write names none (the instruction is not read)."""
        def truth(fstate):
            c = axes.normalize(Case('deb', {'homonym': 'two', 'kind': 'cli', 'fstate': fstate}))
            return oracle.truth(c)
        T = truth('written')
        self.assertEqual((T.subjects['child']['unit'], T.subjects['child']['seat'], T.subjects['child']['role']), ('docs/rev', 'B', 'writer'))
        self.assertEqual(T.diag, [])                                                          # `path_ambiguous` is gone: no instruction is read
        T = truth('none')
        self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'))
        self.assertEqual(T.diag, [])

    def test_a_reader_is_somebody_who_read_a_cell(self):
        """The read of a file that is somebody's cell makes a reader; a flat review has no cell, so its report is only a file."""
        c = axes.normalize(Case('deb', {'structure': 'flat', 'role': 'reader'}))
        self.assertEqual(oracle.truth(c).subjects['child']['role'], 'none')
        c = axes.normalize(Case('deb', {'structure': 'single', 'role': 'reader'}))
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
        self.assertEqual(reason_of(cell('cpl', 'seat', 'miss', 'B', None, spawner='grand', rpath='redirect', life='normal_end')), 'J1-LAUNCH-REDIRECT')      # the launcher's own redirect owns the file
        self.assertEqual(reason_of(cell('cpl', 'seat', 'miss', 'B', None, spawner='sub', rpath='var', life='normal_end')), 'J1-LAUNCH-REDIRECT')


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

    def test_a_path_nobody_can_resolve_and_a_marker_seat_nobody_and_a_write_of_its_own_does(self):
        for marker in ('none', 'own'):
            c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'marker': marker, 'fstate': 'none'}))
            T = oracle.truth(c)
            self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'), marker)        # no instruction is read, so no marker seats anybody either
            self.assertEqual(T.diag, [], marker)                                                                       # `path_unresolved` is gone
        c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'marker': 'none', 'fstate': 'written', 'nstyle': 'numbered'}))
        self.assertEqual(oracle.truth(c).subjects['child']['seat'], 'opus1')                                          # a write of its own does, under the name it wrote

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
            self.assertEqual(oracle.truth(c).diag, [])
            text = ' '.join(cmd for _, cmd, _ in self.bash(b.paths['orch']))
            self.assertIn('[REVIEW-B]', text)
            self.assertIn('"' if lang == 'en' else '\u201c', text[text.index('[REVIEW-B]') - 3:text.index('[REVIEW-B]')])      # inside quotes
        self.assertEqual(axes.normalize(Case('deb', {'role': 'writer', 'marker': 'quoted'})).v['marker'], 'none')

    def test_a_collision_of_names_and_a_marker_only(self):
        """A marker seats nobody, and files of the participant's letter that others wrote are files, not its cell: only a write of its own is. Two round folders of different spelling
        say so (`alias_collision`), and the write names the folder."""
        c = axes.normalize(Case('deb', {'kind': 'sub', 'rpath': 'var_ext', 'nstyle': 'collide', 'marker': 'own', 'fstate': 'none', 'life': 'taskstop_kill'}))
        T = oracle.truth(c).subjects['child']
        self.assertEqual((T['seat'], T['cell'], T['placements']), (None, None, frozenset()))
        self.assertTrue(os.path.getsize(os.path.join(self.build('deb', **{k: c.v[k] for k in ('kind', 'rpath', 'nstyle', 'marker', 'fstate', 'life')})[1].meta['unit'], 'r1', 'B.md')) > 0)
        c = axes.normalize(Case('deb', {'kind': 'cli', 'rpath': 'instr_only', 'marker': 'own', 'rdir': 'both'}))
        T = oracle.truth(c)
        self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['role']), (None, 'none'))
        self.assertIn(('alias_collision', 'child'), T.diag)
        c = axes.normalize(Case('deb', {'kind': 'cli', 'rpath': 'instr_only', 'marker': 'own', 'rdir': 'both', 'fstate': 'written'}))
        T = oracle.truth(c)
        self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['placements']), ('B', frozenset(['docs/rev|1|B|r1/B'])))        # the write fixes the folder
        self.assertIn(('alias_collision', 'child'), T.diag)

    def test_two_round_folders_of_different_spelling_hold_two_files(self):
        c, b = self.build('deb', rdir='both', kind='cli', fstate='none')
        T = oracle.truth(c)
        self.assertIn(('alias_collision', 'child'), T.diag)
        self.assertIsNone(T.subjects['child']['cell'])                                                                 # it was told a path and wrote nothing, and no launch names a file
        unit = b.meta['unit']
        self.assertTrue(os.path.getsize(os.path.join(unit, 'r01', 'B.md')) > 0)                                        # somebody else's file of the same name is
        self.assertFalse(os.path.exists(os.path.join(unit, 'r1', 'B.md')))
        self.assertEqual(axes.normalize(Case('deb', {'rdir': 'both', 'role': 'reader'})).v['rdir'], 'r1')

    def test_a_file_the_launch_writes_beside_the_report_is_a_cell_of_its_own(self):
        for kind, flag in (('codex', ' -o '), ('cli', ' > ')):
            c, b = self.build('deb', rpath='dash_o_aux', kind=kind, life='normal_end', fstate='written')
            self.assertEqual(oracle.truth(c).subjects['child']['placements'], {'docs/rev|1|B|r1/B', 'docs/rev|1|B_last|r1/B_last'})       # the run asked for it: its cell, beside the one it wrote
            aux = os.path.join(b.meta['unit'], 'r1', 'B_last.md')
            self.assertTrue(os.path.exists(aux), kind)
            launch = [cmd for _, cmd, _ in self.bash(b.paths['orch']) if kind + ' ' in cmd or 'claude -p' in cmd][0]
            self.assertIn(flag + aux, launch)
            self.assertIn(b.meta['report'], launch)                                                                  # the report path is in the instruction too
        self.assertEqual(axes.normalize(Case('deb', {'rpath': 'dash_o_aux', 'kind': 'sub'})).v['rpath'], 'abs')

    def test_two_flat_reviews_that_declare_the_same_file_are_no_debate_and_hold_no_seat(self):
        for fstate in ('none', 'written'):
            c, b = self.build('deb', structure='flat', homonym='two', kind='cli', fstate=fstate)
            T = oracle.truth(c)
            self.assertIsNone(T.subjects['child']['seat'])                                                          # a folder with a guide and files beside it has no round folder: it is no debate
            self.assertEqual(T.diag, [])
            self.assertEqual(T.subjects['listing']['units'], frozenset())
            for u in (b.meta['unit'], b.meta['unit2']):
                with open(os.path.join(u, 'brief.md')) as fh:
                    self.assertIn('sol.md', fh.read())                                                              # both declare it

    def test_a_participant_whose_process_cannot_be_seen_is_still_working_for_the_cell(self):
        for kind in ('cli', 'codex'):
            for fstate, cell in (('none', None), ('written', 'draft')):
                c = axes.normalize(Case('deb', {'kind': kind, 'life': 'stalled_silent', 'os': 'mac_nops', 'fstate': fstate}))
                self.assertEqual((c.v['os'], c.v['life']), ('mac_nops', 'stalled_silent'))
                self.assertEqual(oracle.truth(c).subjects['child']['cell'], cell)                                   # a quiet participant that wrote is at work (`unknown` is open), one that wrote nothing has no cell
        self.assertEqual(axes.normalize(Case('deb', {'kind': 'sub', 'os': 'mac_nops'})).v['os'], 'linux')            # a sub-agent has no process
        self.assertEqual(axes.normalize(Case('deb', {'kind': 'cli', 'life': 'normal_end', 'os': 'mac_nops'})).v['os'], 'mac')

    # --- a launch that writes the report's own name into the other spelling of the round folder ---
    def test_a_launch_that_writes_the_reports_name_into_the_other_round_folder_has_written_another_file(self):
        """`Write r1/B.md` in the instruction, `-o r01/B.md` (or `> r01/B.md`) in the launch, the file in `r01` only: the instruction is not read, the launch's output is the cell."""
        for kind, flag in (('codex', ' -o '), ('cli', ' > ')):
            c, b = self.build('deb', aux='alias', kind=kind, life='normal_end', fstate='none', rpath='short')
            self.assertEqual((c.v['aux'], c.v['rdir'], c.v['rpath']), ('alias', 'both', 'short'))
            unit = b.meta['unit']
            self.assertTrue(os.path.getsize(os.path.join(unit, 'r01', 'B.md')) > 0, kind)                              # the file is in r01 only ...
            self.assertFalse(os.path.exists(os.path.join(unit, 'r1', 'B.md')), kind)                                   # ... and the file the instruction names is not there
            self.assertTrue(os.path.isdir(os.path.join(unit, 'r1')), kind)
            launch = [cmd for _, cmd, _ in self.bash(b.paths['orch']) if kind + ' ' in cmd or 'claude -p' in cmd][0]
            self.assertIn(flag + 'r01/B.md', launch)                                                                  # the launch writes the other spelling
            self.assertIn('`r1/B.md`', launch)                                                                        # the instruction says `r1/B.md`
            self.assertTrue(launch.startswith('cd %s &&' % unit), launch)                                              # both are relative to the folder the launch runs in
            T = oracle.truth(c)
            self.assertEqual(T.subjects['child']['placements'], {'docs/rev|1|B|r01/B'})                                # the cell is the file the launch was told to fill
            self.assertEqual((T.subjects['child']['seat'], T.subjects['child']['cell']), ('B', 'done'))               # the run ended well and the file holds its last message
            self.assertIn(('alias_collision', 'child'), T.diag)                                                       # two round folders of one round
        c, b = self.build('deb', aux='alias', kind='codex', life='running', fstate='none', rpath='abs')
        self.assertFalse(os.path.exists(os.path.join(b.meta['unit'], 'r01', 'B.md')))                                  # `-o` writes when the run ends
        self.assertTrue(os.path.isdir(os.path.join(b.meta['unit'], 'r01')))
        self.assertEqual(oracle.truth(c).subjects['child']['cell'], 'writing')
        self.assertIn(('alias_collision', 'child'), oracle.truth(c).diag)                                              # the folders say it, whatever the files
        c, b = self.build('deb', aux='alias', kind='cli', life='running', fstate='none', rpath='abs')
        self.assertEqual(os.path.getsize(os.path.join(b.meta['unit'], 'r01', 'B.md')), 0)                              # a redirect creates it empty at the launch
        c, b = self.build('deb', aux='alias', kind='codex', life='normal_end', fstate='written', rpath='abs')
        self.assertTrue(os.path.getsize(os.path.join(b.meta['unit'], 'r1', 'B.md')) > 0)                               # both files
        self.assertEqual(oracle.truth(c).subjects['child']['placements'], {'docs/rev|1|B|r1/B', 'docs/rev|1|B|r01/B'})   # both are its cells: it wrote one and the launch filled the other

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
            self.assertEqual(T.diag, [], mode)
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
            self.assertEqual(T.diag, [], mode)
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

        def reason(bundle, field, ws, gs, result='wrong', **v):
            c = axes.normalize(Case(bundle, v))
            return run.reason_of(run.Cell(c, 'child', field, result, ws, gs, b))
        self.assertEqual(reason('deb', 'placed', 'docs/rev|-|guide_read', 'MISSING', 'miss', role='reader'), 'J11-PLACED')
        self.assertEqual(reason('deb', 'unit', 'docs/rev', None, 'miss', role='reader'), 'J11-PLACED')
        self.assertIsNone(reason('deb', 'seat', None, 'B', role='quoter', qform='fence'))                      # the words of an instruction seat nobody: a red cell of that kind is nobody's reason yet
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
        self.assertNotIn('docs/rev/' + axes.EDIT_DIR, listing['units'])                                      # a guide with no round folder is no debate: it is not listed at all
        self.assertEqual(sorted(listing['units']), ['docs/rev/t1', 'docs/rev/t2'])

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
            self.assertEqual(run.reason_of(cell), 'J9-LISTING')
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

    def rooms(self, **v):
        """{folder: kind} of the rooms the truth says the scene has."""
        return dict(r.split('|') for r in oracle.truth(self.case(**v)).subjects['listing']['rooms'])

    def kind(self, **v):
        return self.rooms(**v).get('docs/meeting')

    def prompt(self, b, role):
        return _read_lines(b.paths[role])[0]['message']['content']

    def calls(self, path, name):
        return [blk['input'] for d in _read_lines(path) if d.get('type') == 'assistant' for blk in d['message']['content']
                if blk.get('type') == 'tool_use' and blk['name'] == name]

    # --- the rules (CONTRACT J14): participants launched together, a file of their own each in one folder; nothing a guide or an instruction says ---
    def test_a_room_needs_participants_launched_together_and_a_file_of_each_ones_own(self):
        self.assertEqual(self.rooms(), {'docs/meeting': 'cells'})
        for v in ({'launch': 'far'}, {'launch': 'none'}, {'rtime': 'sequential'}, {'rtime': 'quiet'}):                  # started by messages of their own, or by calls nobody can tell: no group
            self.assertEqual(self.rooms(**v), {}, v)
        self.assertEqual(self.rooms(proof='told'), {})                                                                # nothing was written
        for shape in ('same', 'scatter'):                                                                             # one file all of them write, code in other repositories: no file of their own
            self.assertEqual(self.rooms(shape=shape), {}, shape)
        self.assertEqual(self.rooms(people='1'), {})
        self.assertEqual(self.rooms(trees='two', people='2'), {})                                                      # one participant on each orchestrator's page
        self.assertEqual(self.rooms(trees='two', people='3'), {'docs/meeting': 'cells'})                              # two of them on this page, the third is another orchestrator's
        for guide in ('agenda', 'brief', 'readme', 'plan', 'own', 'missing', 'late', 'top_readme', 'docs_guide'):         # the guide is not read: whatever it is, or is not, the files make the room
            self.assertEqual(self.rooms(guide=guide, shape='beside') if guide not in ('top_readme', 'docs_guide') else self.rooms(guide='agenda', shape='beside'),
                             {'docs/meeting': 'cells'}, guide)
        # lookalikes the contract accepts as rooms (a known limit): files of their own in a common output folder, or two folders below the guide
        self.assertEqual(self.rooms(shape='far'), {'work/reports': 'cells'})                                           # (O1: the folder that holds their files directly)
        self.assertEqual(self.rooms(shape='deep'), {'docs/meeting/out/x': 'cells'})
        self.assertEqual(self.rooms(shape='below'), {'docs/meeting/out': 'cells'})
        self.assertEqual(self.rooms(shape='mixed'), {})                                                                 # their files are in different folders (O1: all the same, or no room)
        self.assertEqual(self.rooms(code='all'), {})                                                                   # most of them change code outside the folder
        self.assertEqual(self.rooms(scratch='log'), {})                                                                # a command's log in the repository is such a change too (a known limit)
        self.assertEqual(self.rooms(scratch='tmp'), {'docs/meeting': 'cells'})                                         # files kept outside the repository are no change of the work
        self.assertEqual(self.rooms(copy='worktree', bundle='root'), {'docs/records/bundle/meeting': 'cells'})        # (the copy in a worktree is no second room)

    def test_a_room_is_a_room_whatever_the_guide_is_called_and_each_participant_sits_in_its_file(self):
        folder = {'beside': 'docs/meeting', 'below': 'docs/meeting/out'}                                               # the folder that holds their files directly (O1)
        for guide, shape, people in itertools.product(('agenda', 'brief', 'readme', 'plan'), ('beside', 'below'), ('2', '3', '5')):
            T = oracle.truth(self.case(guide=guide, shape=shape, people=people))
            self.assertEqual(dict(r.split('|') for r in T.subjects['listing']['rooms']), {folder[shape]: 'cells'}, (guide, shape, people))
            self.assertEqual(sum(1 for r, f in T.subjects.items() if f.get('role') == 'writer'), int(people))
            self.assertEqual(T.subjects['listing']['rooms_sure'], frozenset(['%s|False|launch' % folder[shape]]))          # launched together: estimated
        for guide, people in itertools.product(('agenda', 'brief'), ('2', '3', '5')):                                     # each one's own way (`A.md`, `notes_B.md`, `out/C.md` ...)
            T = oracle.truth(self.case(guide=guide, shape='mixed', people=people))
            same_folder = people == '2'                                                                                    # the first two are beside the guide; the third is in `out/`: different folders, no room
            self.assertEqual(T.subjects['listing']['rooms'], frozenset(['docs/meeting|cells']) if same_folder else frozenset(), (guide, people))
            self.assertEqual(sum(1 for r, f in T.subjects.items() if f.get('role') == 'writer'), 2 if same_folder else 0)

    def test_the_title_of_a_room_is_the_first_heading_of_a_guide_the_contract_knows(self):
        for lang in ('en', 'ko'):
            for guide in ('brief', 'readme'):
                c, b = self.build(guide=guide, lang=lang)
                with open(os.path.join(b.meta['unit'], axes.ROOM_GUIDES[guide])) as fh:
                    heading = re.match(r'# (.+)', fh.read()).group(1)
                self.assertEqual(oracle.truth(c).subjects['listing']['titles'], frozenset(['docs/meeting|' + heading]), (guide, lang))
        for guide in ('agenda', 'plan'):                                                                               # a guide with another name is not asked for its title
            self.assertNotIn('titles', oracle.truth(self.case(guide=guide)).subjects['listing'], guide)

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
        self.assertEqual(T.subjects['listing']['rooms'], frozenset(['docs/meeting|members']))
        for role in ('p1', 'p2', 'p3'):
            self.assertEqual(T.subjects[role], dict(seat=None, cell=None, role='none', placements=frozenset(), placed=None))
        for talk in ('none', 'orch'):                                                         # nobody tells the others anything: the orchestrator alone does, or nobody
            self.assertEqual(self.rooms(shape='none', talk=talk), {}, talk)
        self.assertEqual(self.rooms(shape='none', talk='peer', guide='top_readme'), {})        # a document everybody reads at the top of a repository: no room
        self.assertEqual(self.rooms(shape='none', talk='peer', guide='late'), {'docs/meeting': 'members'})            # what they read is what counts, not what the first instruction says
        self.assertEqual(self.rooms(shape='none', talk='peer', guide='own'), {})              # each reads a guide of its own: no one file all of them read
        self.assertEqual(self.rooms(shape='none', talk='peer', delivery='failed'), {})        # messages that came to nothing are no talk
        for shape in ('same', 'beside'):                                                      # a file all of them write, or a file each: somebody wrote a `.md`, so it is no meeting by message only
            self.assertNotIn('members', self.rooms(shape=shape, talk='peer').values(), shape)
        self.assertEqual(self.case(people='1', shape='none', talk='peer').v['talk'], 'none')

    def test_a_round_folder_makes_a_debate_not_a_room(self):
        for guide in ('agenda', 'plan', 'readme', 'brief'):
            c = self.case(shape='r1', guide=guide, people='2')
            T = oracle.truth(c)
            self.assertEqual(T.subjects['listing']['rooms'], frozenset(), guide)
            self.assertEqual(T.subjects['listing']['units'], frozenset(['docs/meeting']))
            self.assertNotIn('titles', T.subjects['listing'])                                 # the title of a folder that was a debate already is not asked
            self.assertEqual(T.subjects['p1']['placements'], frozenset(['docs/meeting|1|A|r1/A']))
        self.assertEqual(self.rooms(shape='r1', people='1'), {})
        self.assertEqual(self.case(shape='r1', guide='top_readme').v['shape'], 'far')          # a common document has no folder of its own to hold a round folder
        self.assertEqual(self.case(shape='r1', guide='own').v['guide'], 'agenda')              # a debate has the guide of its folder

    def test_the_seat_is_the_stem_of_the_file_and_a_marker_changes_nothing(self):
        for mark in AXES['seatmark']:
            c = self.case(seatmark=mark, fname='prefix', shape='below')
            self.assertEqual(oracle.truth(c).subjects['p2']['seat'], 'notes_B', mark)
            self.assertEqual(oracle.truth(c).subjects['p2']['placements'], frozenset(['docs/meeting/out|1|notes_B|notes_B']), mark)       # the place is the file, the seat its name
            self.assertEqual(oracle.truth(self.case(seatmark=mark, fname='plain')).subjects['p2']['seat'], 'B', mark)
        self.assertEqual(oracle.truth(self.case(shape='mixed', people='5')).subjects['p2']['placements'], frozenset())                  # files in different folders: no room, no cell

    def test_a_marker_needs_the_language_it_is_written_in(self):
        for mark in ('dam', 'dam_paren'):
            self.assertEqual(self.case(seatmark=mark, lang='en').v['lang'], 'ko')
        for mark in ('en_participant', 'en_as', 'en_seat'):
            self.assertEqual(self.case(seatmark=mark, lang='ko').v['lang'], 'en')

    def test_the_cells_follow_the_writes_and_whether_the_participants_work_on(self):
        for proof, phase, want in (('told', 'working', None), ('told', 'done', None), ('wrote', 'working', 'draft'), ('both', 'done', 'done')):          # what is only told is no cell
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


class CodexOrchestratorScenes(unittest.TestCase):
    """The `cxo` bundle: the scenes are built as the axes say (the files and the process table hold exactly the evidence the oracle counts), and the oracle decides as its rules say."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-cxo-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, **v):
        c = axes.normalize(Case('cxo', v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def arrive(self, b):
        """The records that come between the looks are written."""
        for ph in b.phases:
            if ph.hook:
                ph.hook()

    def commands(self, path):
        return [d['payload']['item'] for d in _read_lines(path) if d['type'] == 'event_msg' and d['payload'].get('type') == 'item_completed'
                and d['payload']['item']['type'] == 'CommandExecution']

    def launcher_path(self, b):
        return b.paths['host'] if b.case.v['host'] == 'sub' and 'host' in b.paths else b.paths['top']

    def kid(self, b, phase=-1):
        """The process of the child in the last look, or None."""
        want = b.paths.get('child')
        for p in b.phases[phase].procs:
            if p['session'] and p['session']['sessionId'] == b.ids.get('child') or (want and want in p['fds'] and p['argv'][:2] == ['codex', 'exec']):
                return p
        return None

    def test_the_id_names_only_the_axes_that_are_off_the_baseline_among_the_late_ones(self):
        c = Case('cxo', {})
        self.assertEqual(c.id, 'cxo:top=cx_tui;chain=one;subj=cl;host=main;env=codex;how=fg;look=live')
        self.assertEqual(Case('cxo', {'lure': 'relay'}).id, c.id + ';lure=relay')
        self.assertEqual(Case('cxo', {'rec': 'late', 'edge': 'guess'}).id, c.id + ';rec=late;edge=guess')
        for cid in (c.id, Case('cxo', {'chain': 'cl>cx>cl', 'top': 'claude'}).id):
            self.assertEqual(Case.from_id(cid).id, cid)

    def test_the_chain_names_the_top_and_the_subject_and_impossible_combinations_fold(self):
        n = lambda **v: axes.normalize(Case('cxo', v)).v                         # noqa: E731
        self.assertEqual((n(chain='cl>cx>cl')['top'], n(chain='cl>cx>cl')['subj']), ('claude', 'cl'))
        self.assertEqual(n(chain='cx>cl>sub', top='claude')['top'], 'cx_tui')
        self.assertEqual(n(chain='cx>cl>sub')['subj'], 'cl_sub')
        self.assertEqual(n(chain='cx>cx')['subj'], 'cx')
        self.assertEqual(n(env='both')['env'], 'codex')                         # both providers' names only below a Claude session
        self.assertEqual(n(chain='cl>cx>cl', env='both')['env'], 'both')
        self.assertEqual(n(how='bg', look='ended')['look'], 'live')              # a child that died with the call leaves nothing to look at later
        self.assertEqual(n(rec='lost')['rec'], 'end')                           # a foreground call that is still running has no record yet anyway
        self.assertEqual(n(rec='late', env='codex')['rec'], 'end')              # a late record matters only where nothing else linked the child before it
        self.assertEqual(n(rec='late', env='none', look='ended')['rec'], 'late')
        self.assertEqual(n(subj='cx_sub', substate='parent_gone')['look'], 'ended')
        self.assertEqual(n(subj='cx_sub', substate='done', how='detach', env='none')['how'], 'fg')
        self.assertEqual(n(lure='user_script')['env'], 'none')
        self.assertEqual(n(lure='relay', top='cx_exec')['top'], 'cx_tui')       # the instruction goes into a terminal
        self.assertEqual(n(lure='gap')['look'], 'ended')
        self.assertEqual(n(top='claude')['guard'], 'none')
        self.assertEqual(n(edge='guess')['edge'], 'sure')

    def test_the_command_of_the_call_is_in_the_launchers_record_exactly_when_the_axes_say(self):
        n = 0
        for c in (c for c in run.select() if c.bundle == 'cxo' and c.v['subj'] in ('cl', 'cx') and c.v['chain'] == 'one' and c.v['lure'] in ('none', 'twin_orch', 'twin_out') + axes.CXO_RELAYS and c.v['env'] != 'stale'):
            _, b = self.build(**c.v)
            self.arrive(b)
            v = c.v
            cmds = [x for x in self.commands(self.launcher_path(b)) if b.ids['child_text'] in x['command'][2]]
            self.assertEqual(len(cmds), 1 if axes.cxo_record(v) or v['how'] == 'bg' and v['rec'] == 'end' else 0, c.id)
            for x in cmds:
                self.assertEqual(x['command'][:2], ['/bin/bash', '-lc'], c.id)
                self.assertEqual(len(x['command']), 3, c.id)
                self.assertTrue(x['cwd'].startswith('file://'), c.id)
                self.assertIsInstance(x['process_id'], str, c.id)
                self.assertIn('claude -p' if v['subj'] == 'cl' else 'codex exec', x['command'][2], c.id)
                self.assertEqual(x['command'][2].endswith(' &'), v['how'] != 'fg', c.id)
                self.assertEqual('setsid' in x['command'][2], v['how'] == 'detach', c.id)
            n += 1
        self.assertGreater(n, 60)

    def test_a_foreground_call_that_is_still_running_has_no_record_and_an_ampersand_call_has_one_at_once(self):
        _, b = self.build(how='fg', look='live')
        calls = [d for d in _read_lines(b.paths['top']) if d['type'] == 'response_item' and d['payload'].get('type') == 'custom_tool_call']
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.commands(b.paths['top']), [])
        outs = [d for d in _read_lines(b.paths['top']) if d['payload'].get('type') == 'custom_tool_call_output']
        self.assertEqual(outs, [])                                                # the cell has not returned: the child is running
        _, b = self.build(how='detach', look='live')
        (x,) = self.commands(b.paths['top'])
        self.assertLess(x['duration']['secs'], 1)
        self.assertTrue(self.kid(b))                                               # the child is alive and there is a record already

    def test_the_environment_and_the_lineage_of_the_child_are_what_the_axes_say(self):
        for how, env in itertools.product(('fg', 'detach'), ('codex', 'none')):
            _, b = self.build(how=how, env=env, look='live')
            k = self.kid(b)
            self.assertEqual(('CODEX_THREAD_ID' in (k['env'] or {}), 'CODEX_SESSION_ID' in (k['env'] or {})), (env == 'codex',) * 2, (how, env))
            if env == 'codex':
                self.assertEqual((k['env']['CODEX_THREAD_ID'], k['env']['CODEX_SESSION_ID']), (b.ids['top'], b.ids['top']))
            self.assertEqual(k['ppid'] == 1, how == 'detach')                    # `setsid` re-parents it
            self.assertNotIn('CLAUDE_CODE_SESSION_ID', k['env'] or {})
        _, b = self.build(how='fg', env='codex', host='sub', look='live')
        k = self.kid(b)
        self.assertEqual((k['env']['CODEX_THREAD_ID'], k['env']['CODEX_SESSION_ID']), (b.ids['host'], b.ids['top']))      # the sub-agent's shell: its own id, the root's session
        _, b = self.build(look='ended')
        self.assertIsNone(self.kid(b))

    def test_both_providers_names_are_in_the_grandchild_only_below_a_claude_session(self):
        _, b = self.build(chain='cl>cx>cl', env='both', how='detach', look='live')
        k = self.kid(b)
        self.assertEqual(k['env']['CLAUDE_CODE_SESSION_ID'], b.ids['top'])
        self.assertEqual((k['env']['CODEX_THREAD_ID'], k['env']['CODEX_SESSION_ID']), (b.ids['mid'], b.ids['mid']))   # the codex exec run is a root of its own
        self.assertEqual(k['ppid'], 1)
        _, b = self.build(chain='cl>cx>cl', env='codex', how='fg', look='live')
        self.assertNotIn('CLAUDE_CODE_SESSION_ID', self.kid(b)['env'])
        m = [p for p in b.phases[0].procs if p['argv'][:2] == ['codex', 'exec']]
        self.assertEqual([p['env']['CLAUDE_CODE_SESSION_ID'] for p in m], [b.ids['top']])      # the codex exec run itself was started by the Claude session

    def test_an_ampersand_call_leaves_no_run_and_no_record_of_one(self):
        for subj in ('cl', 'cx'):
            _, b = self.build(subj=subj, how='bg')
            self.assertNotIn('child', b.paths, subj)
            (x,) = self.commands(b.paths['top'])
            self.assertIn('nohup', x['command'][2])
            self.assertNotIn('setsid', x['command'][2])
            self.assertEqual([p for p in b.phases[0].procs if p['argv'][:1] in (['claude'],) or p['argv'][:2] == ['codex', 'exec']], [])

    def test_a_relay_is_a_call_of_another_session_that_only_types_the_command(self):
        for lure in ('relay', 'relay_py'):
            c, b = self.build(lure=lure, look='ended', how='detach')
            R = [d for d in _read_lines(b.paths['relayer']) if d['type'] == 'assistant']
            cmd = R[0]['message']['content'][0]['input']['command']
            self.assertIn('send-keys', cmd)
            self.assertTrue(cmd.startswith('tmux') if lure == 'relay' else cmd.startswith('python3 -c'))
            self.assertIn(b.ids['child_text'], cmd)                                   # the whole command line, with the child's words, is in it
            self.assertIn('claude -p', cmd)
            user = [d for d in _read_lines(b.paths['top']) if d['type'] == 'response_item' and d['payload'].get('role') == 'user']
            self.assertIn(b.ids['child_text'], user[0]['payload']['content'][0]['text'])        # the terminal got the same words
            self.assertLess(user[0]['timestamp'], iso_of(b.T(0)))                                # the words reach the terminal before the call that carries them out
            relay_t, child_t = (d['timestamp'] for d in (R[0], _read_lines(b.paths['child'])[0]))
            self.assertLess(relay_t, child_t)
            self.assertLess(abs(build_time(child_t) - build_time(relay_t)), 10)        # the call is close enough to the child to be taken for its launch by a reader that does not know better
            self.assertTrue(os.path.dirname(b.paths['relayer']) == os.path.dirname(b.paths['child']))      # the same repository: the folder refutes nothing

    def test_two_orchestrators_of_one_folder_start_a_child_each_with_the_same_words(self):
        _, b = self.build(lure='twin_orch', look='ended')
        child, twin = (_read_lines(b.paths[r])[0]['message']['content'] for r in ('child', 'twin'))
        self.assertEqual(child, twin)
        C = [d for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
        self.assertIn(child, C[0]['message']['content'][0]['input']['command'])
        _, b = self.build(lure='twin_orch', look='live', how='fg', env='codex')
        kids = [p for p in b.phases[0].procs if p['argv'][:2] == ['claude', '-p']]
        self.assertEqual(len(kids), 2)
        self.assertEqual({'CODEX_THREAD_ID' in (p['env'] or {}) for p in kids}, {True, False})      # one names Codex, one names the Claude session

    def test_an_output_file_that_names_the_child_decides_between_two_launches_with_the_same_words(self):
        for how in ('fg', 'detach'):
            c, b = self.build(lure='twin_out', look='ended', how=how, env='none')
            (x,) = [x for x in self.commands(b.paths['top']) if b.ids['child_text'] in x['command'][2]]
            out = os.path.join(b.scratch, 'out_x.json')
            self.assertIn('--output-format json', x['command'][2])
            self.assertIn('> %s' % out, x['command'][2])
            with open(out) as f:
                self.assertEqual(json.load(f)['session_id'], b.ids['child'])                # the file holds the session id of the child that was started by this call
            with open(os.path.join(b.scratch, 'out_c.json')) as f:
                self.assertEqual(json.load(f)['session_id'], b.ids['twin'])                 # and the other launch's file the id of its own
            t = oracle.truth(c).subjects['child']
            self.assertEqual((t['tree'], t['rule_class']), ('@top', 'certain'), how)       # it ranks above the words, so nothing is held
        c, b = self.build(lure='twin_out', look='live', how='detach', env='none')
        with open(os.path.join(b.scratch, 'out_x.json')) as f:
            self.assertEqual(f.read(), '')                                                  # the run is not over: the file is empty
        self.assertEqual(oracle.truth(c).subjects['child']['tree'], None)                   # nothing names the child but the words, which tie
        self.assertEqual(self.truth(lure='twin_out', look='live', how='detach', env='codex').subjects['child']['tree'], '@top')    # the environment

    def test_a_script_runs_codex_exec_with_no_orchestrator_behind_it(self):
        _, b = self.build(lure='user_script', look='live')
        k = self.kid(b)
        self.assertEqual(k['env'], {})
        self.assertNotIn(k['ppid'], (300, 301))
        calls = [x['command'][2] for x in self.commands(b.paths['top'])]
        self.assertTrue(all(b.ids['child_text'] not in c for c in calls))        # no call of the orchestrator carries the words of that thread
        self.assertEqual(len(calls), 1)                                          # but it did run another `codex exec` in the same folder just before
        self.assertIn('codex exec', calls[0])

    def test_a_command_nobody_can_read_yet_is_an_exec_cell_that_returned_running(self):
        _, b = self.build(lure='gap')
        rows = _read_lines(b.paths['top'])
        outs = [d['payload']['output'][0]['text'] for d in rows if d['payload'].get('type') == 'custom_tool_call_output']
        self.assertEqual(outs, ['Script running with cell ID 7'])
        self.assertEqual(self.commands(b.paths['top']), [])
        self.assertFalse([d for d in rows if d['payload'].get('type') == 'task_complete'])      # and its turn is still open
        C = [d for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
        self.assertIn(b.ids['child_text'], C[0]['message']['content'][0]['input']['command'])

    def test_the_middle_run_is_linked_by_a_guess_when_the_call_reads_its_words_from_a_file(self):
        _, b = self.build(chain='cl>cx>cl', env='both', how='detach', look='live', edge='guess')
        O = [d for d in _read_lines(b.paths['top']) if d['type'] == 'assistant']
        launch = [x['message']['content'][0]['input']['command'] for x in O if 'codex exec' in x['message']['content'][0]['input'].get('command', '')]
        self.assertEqual(len(launch), 1)
        self.assertIn('$(cat', launch[0])
        self.assertNotIn(b.ids['mid_text'], launch[0])
        written = [x for x in O if x['message']['content'][0]['name'] == 'Write']
        self.assertEqual(written, [])                                              # no record shows the file being written
        with open(re.search(r'\$\(cat ([^)]+)\)', launch[0]).group(1)) as f:
            self.assertEqual(f.read(), b.ids['mid_text'])
        self.assertEqual([p for p in b.phases[0].procs if p['argv'][:2] == ['codex', 'exec']], [])      # the middle run is gone: only the child lives, below init
        _, b = self.build(chain='cl>cx>cl', env='both', how='detach', look='live')
        launch = [x['message']['content'][0]['input']['command'] for x in [d for d in _read_lines(b.paths['top']) if d['type'] == 'assistant']
                  if 'codex exec' in x['message']['content'][0]['input'].get('command', '')]
        self.assertIn(b.ids['mid_text'], launch[0])

    def test_a_record_that_comes_late_is_written_between_the_two_looks(self):
        c, b = self.build(rec='late', env='none', look='ended')
        self.assertEqual(len(b.phases), 2)
        self.assertEqual([p['argv'][:2] for p in b.phases[0].procs], [['codex', '-m']])      # the first look: the child runs where the board cannot see it
        child_path = b.paths['child']
        self.assertEqual(self.commands(b.paths['top']), [])
        self.assertFalse(_cost_state(child_path))                                  # the child is not over in the first look's files
        self.assertGreater(b.phases[1].now - b.phases[0].now, 120)
        self.arrive(b)
        (x,) = self.commands(b.paths['top'])
        self.assertIn(b.ids['child_text'], x['command'][2])
        self.assertGreater(x['duration']['secs'], 100)                           # a run of two minutes: the record is written at its end
        self.assertTrue(_cost_state(child_path))
        self.assertEqual(len(b.phases[0].procs), 1)

    def test_a_native_sub_agent_has_the_front_part_of_its_parent_and_its_own_turn(self):
        c, b = self.build(subj='cx_sub', substate='done')
        rows = _read_lines(b.paths['child'])
        n = rows[0]['payload']['subagent_history_start_ordinal']
        self.assertEqual(rows[0]['payload']['source']['subagent']['thread_spawn']['parent_thread_id'], b.ids['top'])
        users = [d for d in rows[1:n + 1] if d['type'] == 'response_item' and d['payload'].get('role') == 'user']
        self.assertEqual([u['payload']['content'][0]['text'] for u in users], [b.ids['root_text']])      # the parent's instruction is in the front part, not the sub-agent's
        own = rows[n + 1:]
        self.assertFalse([d for d in own if d['type'] == 'response_item' and d['payload'].get('role') == 'user'])
        first = next(d['payload'] for d in own if d['payload'].get('type') == 'agent_message')
        self.assertEqual([p['type'] for p in first['content']], ['input_text', 'encrypted_content'])
        self.assertEqual(first['content'][0]['text'], b.ids['notice'])
        self.assertNotIn(b.ids['root_text'], first['content'][0]['text'])
        self.assertEqual(c.v['top'], 'cx_tui')
        # a sub-agent of a sub-agent: the front part carries the first sub-agent's view, and its parent is the first sub-agent
        c, b = self.build(subj='cx_sub', host='sub', substate='done')
        rows = _read_lines(b.paths['child'])
        self.assertEqual(rows[0]['payload']['parent_thread_id'], b.ids['host'])
        self.assertEqual(rows[0]['payload']['source']['subagent']['thread_spawn']['depth'], 2)
        self.assertEqual(rows[0]['payload']['agent_path'], '/root/s1/ss')
        self.assertEqual(rows[0]['payload']['session_id'], b.ids['top'])

    def test_how_a_sub_agent_ends_is_what_the_substate_says(self):
        def ends(**v):
            c, b = self.build(subj='cx_sub', **v)
            parent = _read_lines(b.paths['host'] if c.v['host'] == 'sub' else b.paths['top'])
            sub = _read_lines(b.paths['child'])
            acts = [(d['payload']['item']['kind']) for d in parent if d['type'] == 'event_msg' and d['payload'].get('type') == 'item_completed'
                    and d['payload']['item']['type'] == 'SubAgentActivity']
            own = [d['payload'].get('type') for d in sub if d['type'] == 'event_msg']
            return acts, own, b
        acts, own, b = ends(substate='running')
        self.assertEqual((acts, 'task_complete' in own, 'turn_aborted' in own), (['started'], False, False))
        self.assertEqual([p['argv'][0] for p in b.phases[0].procs], ['codex'])
        acts, own, _ = ends(substate='done')
        self.assertEqual((acts, 'task_complete' in own), (['started', 'completed'], True))
        acts, own, _ = ends(substate='int_after')
        self.assertEqual((acts, 'task_complete' in own), (['started', 'completed', 'interrupted'], True))      # the interrupt comes after it finished
        acts, own, _ = ends(substate='int_mid')
        self.assertEqual((acts, 'task_complete' in own, 'turn_aborted' in own), (['started', 'interrupted'], False, True))
        acts, own, b = ends(substate='parent_gone')
        self.assertEqual((acts, 'task_complete' in own), (['started'], False))
        self.assertEqual(b.phases[0].procs, [])                                  # its parent's process is gone, its own turn still open

    def test_the_parent_records_completed_first_and_the_message_after_it_and_a_report_in_the_middle_is_a_message_too(self):
        _, b = self.build(subj='cx_sub', substate='done')
        rows = _read_lines(b.paths['top'])
        kinds = [(d['payload'].get('item') or {}).get('kind') if d['type'] == 'event_msg' else d['payload'].get('type') for d in rows if d['type'] in ('event_msg', 'response_item')]
        i = kinds.index('completed')
        self.assertEqual(kinds[i + 1], 'agent_message')
        msgs = [d['payload'] for d in rows if d['payload'].get('type') == 'agent_message']
        self.assertEqual(len(msgs), 2)                                           # the report of the middle of the work, and the last message
        self.assertTrue(all(m['author'] == '/root/s1' and m['recipient'] == '/root' for m in msgs))
        sub_msgs = [d['payload'] for d in _read_lines(b.paths['child']) if d['payload'].get('type') == 'agent_message']
        self.assertEqual({m['id'] for m in sub_msgs[1:]}, {m['id'] for m in msgs})  # the same messages are in the sub-agent's rollout, with the same ids
        self.assertTrue(all(m['internal_chat_message_metadata_passthrough']['turn_id'] for m in msgs))

    def test_a_guardian_is_a_thread_beside_the_team_with_calls_of_its_own(self):
        _, b = self.build(subj='cx_sub', guard='one', substate='done')
        g = _read_lines(b.paths.get('guardian') or [p for p in glob_rollouts(b) if b.ids['guardian'] in p][0])
        self.assertEqual(g[0]['payload']['source'], {'subagent': {'other': 'guardian'}})
        self.assertEqual(g[0]['payload']['thread_source'], 'guardian_review')
        self.assertEqual(sum(1 for d in g if d['type'] == 'token_usage_record'), oracle.CXO_GUARD_CALLS)
        sub = _read_lines(b.paths['child'])
        self.assertGreater(sum(1 for d in sub if d['type'] == 'token_usage_record'), 0)      # the sub-agent has calls too: they are not the guardian's

    def test_a_participant_of_a_debate_folder_is_told_its_file_or_writes_it_with_the_launch(self):
        _, b = self.build(topic='talk', subj='cl', look='ended')
        self.assertTrue(os.path.isfile(os.path.join(b.work, 'repo', 'talk', 'brief.md')))
        self.assertIn(os.path.join(b.work, 'repo', 'talk', 'r1', 'A.md'), _read_lines(b.paths['child'])[0]['message']['content'])
        self.assertTrue(os.path.isfile(os.path.join(b.work, 'repo', 'talk', 'r1', 'A.md')))
        _, b = self.build(topic='talk', subj='cx', look='ended')
        (x,) = self.commands(b.paths['top'])
        self.assertIn(' -o %s' % os.path.join(b.work, 'repo', 'talk', 'r1', 'B.md'), x['command'][2])
        self.assertTrue(os.path.isfile(os.path.join(b.work, 'repo', 'talk', 'r1', 'B.md')))
        _, b = self.build(topic='talk', subj='cl', look='live')
        self.assertTrue(os.path.isdir(os.path.join(b.work, 'repo', 'talk', 'r1')))                          # the round folder is made before the participants run ...
        self.assertFalse(os.path.exists(os.path.join(b.work, 'repo', 'talk', 'r1', 'A.md')))                # ... and the report is not there until it is written

    def test_a_codex_exec_child_has_the_dash_o_of_its_command_in_its_own_arguments(self):
        W = lambda b, *p: os.path.join(b.work, 'repo', *p)                            # noqa: E731
        for topic, flag in (('talk', lambda b: W(b, 'talk', 'r1', 'B.md')), ('talk_rel', lambda b: os.path.join('talk', 'r1', 'B.md')), ('talk_aux', lambda b: W(b, 'talk', 'r1', 'B_last.md'))):
            _, b = self.build(topic=topic, subj='cx', look='live')
            argv = self.kid(b)['argv']
            self.assertEqual(argv[argv.index('-o'):argv.index('-o') + 2], ['-o', flag(b)], topic)           # the process has what the shell made of the call
            self.assertTrue(argv[-1].startswith(b.ids['child_text']), topic)                                 # the instruction is the last argument
            self.assertEqual('r1/B.md' in argv[-1], topic == 'talk_aux', topic)                               # only `talk_aux` is told the path
            _, b = self.build(topic=topic, subj='cx', look='ended')
            (x,) = self.commands(b.paths['top'])
            self.assertIn(' -o %s ' % flag(b), x['command'][2], topic)
        for subj in ('cl', 'cx'):
            _, b = self.build(subj=subj, look='live')
            self.assertNotIn('-o', (self.kid(b) or {'argv': []})['argv'])               # no report to tell: no `-o`

    def test_a_relative_dash_o_names_a_file_below_the_folder_the_call_started_in(self):
        _, b = self.build(topic='talk_rel', subj='cx', look='ended')
        self.assertTrue(os.path.isfile(os.path.join(b.work, 'repo', 'talk', 'r1', 'B.md')))
        _, b = self.build(topic='talk_rel', subj='cx', look='live')
        self.assertNotIn('r1', self.kid(b)['argv'][-1])                                       # the instruction has no path either
        t = self.truth(topic='talk_rel', subj='cx', look='live').subjects['child']
        self.assertEqual((t['unit'], t['seat'], t['cell']), ('talk', 'B', 'writing'))                    # the command's `cd` tells the folder: the output is the cell of the run asked for it

    def test_the_instruction_names_the_report_and_dash_o_is_another_file_of_the_round(self):
        W = os.path.join
        _, b = self.build(topic='talk_aux', subj='cx', look='ended')
        folder = W(b.work, 'repo', 'talk', 'r1')
        (x,) = self.commands(b.paths['top'])
        self.assertIn('Write your report to %s.' % W(folder, 'B.md'), x['command'][2])
        self.assertIn(' -o %s ' % W(folder, 'B_last.md'), x['command'][2])
        changes = [d['payload']['item'] for d in _read_lines(b.paths['child']) if d['type'] == 'event_msg' and d['payload'].get('type') == 'item_completed' and d['payload']['item']['type'] == 'FileChange']
        self.assertEqual([list(c['changes']) for c in changes], [[W(folder, 'B.md')]])          # the run writes the report with a patch
        self.assertEqual(sorted(os.listdir(folder)), ['B.md', 'B_last.md'])
        t = self.truth(topic='talk_aux', subj='cx', look='live')
        self.assertEqual(t.subjects['child']['placements'], {'talk|1|B_last|r1/B_last'})    # the run is asked for `-o`'s file (its cell); the report it was only told is nothing yet
        self.assertEqual(self.truth(topic='talk_aux', subj='cx', look='ended').subjects['child']['placements'], {'talk|1|B|r1/B', 'talk|1|B_last|r1/B_last'})     # written by a patch, and `-o`
        _, b = self.build(topic='talk_aux', subj='cx', look='live')
        argv = self.kid(b)['argv']
        self.assertEqual(argv[argv.index('-o') + 1], W(b.work, 'repo', 'talk', 'r1', 'B_last.md'))

    def test_a_codex_shell_redirects_the_output_of_a_claude_run_to_the_report_and_the_instruction_has_no_path(self):
        for how in ('fg', 'detach'):
            _, b = self.build(topic='talk_redir', subj='cl', look='ended', how=how)
            (x,) = [c for c in self.commands(b.paths['top']) if 'claude -p' in c['command'][2]]
            self.assertIn(' > talk/r1/A.md', x['command'][2], how)
            first = _read_lines(b.paths['child'])[0]['message']['content']
            self.assertNotIn('r1/', first)
            self.assertNotIn('Write', ' '.join(d['message']['content'][-1].get('name', '') for d in _read_lines(b.paths['child']) if d['type'] == 'assistant' and isinstance(d['message']['content'][-1], dict)))
            path = os.path.join(b.work, 'repo', 'talk', 'r1', 'A.md')
            with open(path) as f:
                self.assertIn('Findings of A', f.read(), how)                                 # the shell, not the run, wrote it
        _, b = self.build(topic='talk_redir', subj='cl', look='live', how='fg')
        self.assertEqual(os.path.getsize(os.path.join(b.work, 'repo', 'talk', 'r1', 'A.md')), 0)     # created at the launch, filled when the run ends
        t = self.truth(topic='talk_redir', subj='cl', look='live').subjects['child']
        self.assertEqual((t['unit'], t['seat'], t['cell']), ('talk', 'A', 'writing'))

    def test_the_new_kinds_of_participant_fold_to_the_ones_that_can_exist(self):
        n = lambda **v: axes.normalize(Case('cxo', v)).v                                   # noqa: E731
        self.assertEqual(n(topic='talk_rel', subj='cl')['topic'], 'talk')                   # `-o` is an option of `codex exec`
        self.assertEqual(n(topic='talk_aux', subj='cl')['topic'], 'talk')
        self.assertEqual(n(topic='talk_redir', subj='cx')['topic'], 'talk')                 # the redirect is of a `claude -p` run's output
        for topic, subj in (('talk_rel', 'cx'), ('talk_aux', 'cx'), ('talk_redir', 'cl')):
            self.assertEqual(n(topic=topic, subj=subj)['topic'], topic)
            self.assertEqual(n(topic=topic, subj=subj, how='bg')['topic'], 'none')           # a `&` call leaves no child
            self.assertEqual(n(topic=topic, subj=subj, host='sub')['topic'], 'none')
        self.assertEqual(n(topic='talk', os='mac')['os'], 'mac')
        for off in (dict(look='ended'), dict(how='detach'), dict(env='none'), dict(topic='none'), dict(lure='relay'), dict(chain='cx>cx')):
            self.assertEqual(n(**dict(dict(topic='talk', os='mac'), **off))['os'], 'linux', off)       # macOS only where a running participant's arguments would have told its report

    def test_macos_has_no_arguments_to_read_so_the_record_of_the_call_is_all_there_is_for_a_running_participant(self):
        for subj in ('cl', 'cx'):
            _, b = self.build(topic='talk', subj=subj, look='live', os='mac')
            self.assertEqual(b.case.v['os'], 'mac')
            t = oracle.truth(b.case).subjects['child']
            if subj == 'cx':
                self.assertEqual((t['unit'], t['seat'], t['cell']), ('talk', 'B', 'writing'))             # the launch command's `-o` is in the record of the call: the truth is the same as on Linux
            else:
                self.assertEqual((t['seat'], t['placements']), (None, frozenset()))                       # a run only told its path has no cell yet
        ids = {c.id for c in run.select()}
        for subj, top in itertools.product(('cl', 'cx'), ('cx_tui', 'cx_exec')):
            self.assertIn(axes.normalize(Case('cxo', dict(topic='talk', subj=subj, how='fg', look='live', top=top, env='codex', os='mac'))).id, ids)

    def test_the_new_participants_are_in_the_selection_and_the_reason_says_where_the_seat_stays_open(self):
        ids = {c.id for c in run.select()}
        for topic, subj in (('talk_rel', 'cx'), ('talk_aux', 'cx'), ('talk_redir', 'cl')):
            for how, look, top in itertools.product(('fg', 'detach'), AXES['look'], ('cx_tui', 'cx_exec')):
                self.assertIn(axes.normalize(Case('cxo', dict(topic=topic, subj=subj, how=how, look=look, top=top))).id, ids)
        with open(os.path.join(REPO, 'tests', 'scenarios_xfail.json')) as f:
            doc = json.load(f)
        for cid, cells in doc['cells'].items():
            if cid.startswith('cxo:') and 'topic=' in cid and 'topic=none' not in cid:
                fields = {k.split('.')[1] for k in cells if k.startswith('child.')}
                if fields & {'unit', 'round', 'seat', 'cell', 'role', 'placements'}:
                    self.assertLessEqual({r[3] for k, r in cells.items() if k.split('.')[1] in ('unit', 'round', 'seat', 'cell', 'role', 'placements')}, {'J7-LIVE-OUT', 'J7-LIVE-REDIRECT', 'X-MAC-LIVE'}, cid)

    def test_a_claude_sub_agent_of_a_claude_run_below_a_codex_page(self):
        _, b = self.build(chain='cx>cl>sub', look='live')
        meta = glob_files(os.path.dirname(b.paths['mid']), 'agent-*.meta.json')
        self.assertEqual(len(meta), 1)
        self.assertTrue(b.ids['child'].startswith('a') and len(b.ids['child']) == 17)
        mid = _read_lines(b.paths['mid'])
        self.assertTrue([d for d in mid if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == 'Agent'])

    def test_a_call_that_ends_with_ampersand_leaves_no_run_in_any_chain(self):
        for chain in ('one', 'cl>cx>cl', 'cx>cl>sub', 'cx>cx'):
            v = axes.normalize(Case('cxo', dict(chain=chain, how='bg', look='live'))).v
            if chain == 'one':
                self.assertEqual((v['how'], v['look']), ('bg', 'live'), chain)       # no child is made: an orphan launch
            else:
                self.assertNotEqual(v['how'], 'bg', chain)                           # the Codex shell's `&` call takes the child down with the call: there is nothing to put in a chain
        _, b = self.build(chain='cl>cx>cl', env='codex', how='bg', look='live')
        self.assertEqual(self.truth(chain='cl>cx>cl', env='codex', how='bg', look='live').subjects['child']['status'], 'running')   # the case that is left is the detached one
        self.assertIsNotNone(self.kid(b))
        for c in run.select():
            if c.bundle == 'cxo' and c.v['chain'] != 'one':
                self.assertNotEqual(c.v['how'], 'bg', c.id)

    def test_the_new_values_fold_to_what_can_be(self):
        n = lambda **v: axes.normalize(Case('cxo', v)).v                         # noqa: E731
        s = n(env='stale', how='fg', look='ended', host='sub', top='cx_exec')
        self.assertEqual((s['env'], s['how'], s['look'], s['host'], s['subj'], s['chain']), ('stale', 'detach', 'live', 'main', 'cl', 'one'))
        self.assertEqual(n(env='stale', chain='cx>cx')['env'], 'codex')           # the names of an old thread need a child another session started
        self.assertEqual(n(env='stale', subj='cx')['env'], 'codex')
        self.assertEqual(n(env='stale', lure='relay')['env'], 'codex')
        self.assertEqual(n(os='mac')['os'], 'linux')                             # the process table of macOS matters where both providers' names are in a grandchild
        self.assertEqual(n(os='mac_nops', chain='cl>cx>cl', env='both', look='live')['os'], 'mac')
        self.assertEqual(n(os='mac', chain='cl>cx>cl', env='both', look='ended')['os'], 'linux')
        self.assertEqual(n(os='mac', chain='cl>cx>cl', env='codex', look='live')['os'], 'linux')
        g = n(lure='gap', env='none', look='ended', how='fg', rec='lost')
        self.assertEqual((g['look'], g['how'], g['env']), ('live', 'detach', 'none'))         # a child that is still running, started by `tmux new-window`
        g = n(lure='gap', env='both', look='live')
        self.assertEqual((g['look'], g['how'], g['env']), ('ended', 'fg', 'codex'))           # the older gap case
        self.assertEqual((n(lure='launch_tmux')['env'], n(lure='launch_tmux')['how']), ('none', 'detach'))
        self.assertEqual((n(lure='launch_xargs')['env'], n(lure='launch_xargs')['how']), ('codex', 'fg'))
        for lure in axes.CXO_RELAYS:
            r = n(lure=lure, top='cx_exec', how='bg', subj='cx', host='sub')
            self.assertEqual((r['top'], r['subj'], r['host'], r['how']), ('cx_tui', 'cl', 'main', 'detach'), lure)
        self.assertEqual(n(chain='cx>cl>sub', env='stale')['env'], 'codex')
        w = n(lure='stale_turn', env='none', how='fg', look='ended', host='sub', top='cx_exec')
        self.assertEqual((w['env'], w['how'], w['look'], w['host'], w['top']), ('stale', 'detach', 'live', 'main', 'cx_exec'))
        for lure, how in (('pin_unknown', 'fg'), ('pin_stale_claude', 'detach')):
            p = n(lure=lure, env='codex', how='bg', look='ended', host='sub')
            self.assertEqual((p['env'], p['how'], p['look'], p['host'], p['subj']), ('none', how, 'live', 'main', 'cl'), lure)
        self.assertEqual((n(lure='launch_pyfile')['env'], n(lure='launch_pyfile')['how']), ('codex', 'fg'))
        for c in run.select():
            if c.bundle == 'cxo' and c.core and c.twin_of is None:
                self.assertNotEqual(c.v['env'], 'stale')                              # a value that came later is not in the pairwise cover: the older cover is as it was
        self.assertEqual(run.pair_values('env'), ['codex', 'both', 'none'])

    def test_every_way_of_passing_the_words_on_is_a_call_of_a_session_that_starts_nothing(self):
        for lure in ('relay_pyfile', 'relay_script', 'relay_xargs', 'relay_ssh', 'relay_kube', 'relay_curl'):
            c, b = self.build(lure=lure, look='ended', how='detach', env='none')
            calls = [d['message']['content'][0]['input']['command'] for d in _read_lines(b.paths['relayer']) if d['type'] == 'assistant'
                     and d['message']['content'][0]['name'] == 'Bash']
            cmd = calls[-1]
            self.assertIn(b.ids['child_text'], cmd, lure)                                 # the words are in the call, whole
            self.assertNotIn('nohup', cmd)
            self.assertNotIn('setsid', cmd)
            if lure == 'relay_script':
                self.assertTrue(cmd.startswith('bash %s "' % os.path.join(b.scratch, 'relay.sh')))
                self.assertEqual(len(calls), 2)                                           # the script is written by an earlier call, so its body can be read
                self.assertIn('cat > ', calls[0])
                with open(os.path.join(b.scratch, 'relay.sh')) as f:
                    body = f.read()
                self.assertIn('tmux send-keys -t w "claude -p', body)
                self.assertNotIn(b.ids['child_text'], body)                               # the script holds `$1`, not the words
            elif lure == 'relay_pyfile':
                self.assertTrue(cmd.startswith('python3 %s "' % os.path.join(b.scratch, 'relay.py')))
                self.assertEqual(len(calls), 2)                                           # the file is written by an earlier call, so its source can be read
                with open(os.path.join(b.scratch, 'relay.py')) as f:
                    body = f.read()
                self.assertIn('"send-keys"', body)
                self.assertIn('sys.argv[1]', body)
                self.assertNotIn(b.ids['child_text'], body)                               # the file holds the argument, not the words
            elif lure == 'relay_xargs':
                self.assertIn("xargs -I{} tmux send-keys -t {} 'claude -p \"%s\"' Enter" % b.ids['child_text'], cmd)
            elif lure == 'relay_ssh':
                self.assertTrue(cmd.startswith("ssh h echo 'claude -p \""))
            elif lure == 'relay_kube':
                self.assertTrue(cmd.startswith("kubectl exec pod -- tmux send-keys -t w 'claude -p \""))
            else:
                self.assertTrue(cmd.startswith('curl --data claude -p --data "'))
                self.assertTrue(cmd.endswith('https://example.invalid'))
            self.assertEqual(self.commands(b.paths['top'])[0]['command'][2].count('claude -p'), 1)    # the real launch is the Codex thread's own, after the words arrived
            t = oracle.truth(c).subjects['child']
            self.assertEqual(t['tree'], '@top', lure)
            self.assertIn(('child', 'tree', '@relayer'), oracle.truth(c).forbid, lure)
            self.assertEqual(oracle.truth(axes.normalize(Case('cxo', dict(lure=lure, look='ended', how='detach', env='none', rec='lost')))).subjects['child']['tree'], None)

    def test_a_wrapper_that_is_a_launch_is_the_sessions_own_launch(self):
        for lure, look in (('launch_tmux', 'ended'), ('launch_tmux', 'live'), ('launch_xargs', 'live'), ('launch_xargs', 'ended')):
            c, b = self.build(lure=lure, look=look)
            cmds = [d['message']['content'][0]['input']['command'] for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
            self.assertEqual(len(cmds), 1, lure)
            if lure == 'launch_tmux':
                self.assertIn("tmux new-session -d -s w 'claude -p --model", cmds[0])
            else:
                self.assertIn('| xargs -I{} claude -p --model', cmds[0])
                self.assertIn('"{}"', cmds[0])
            cmd = cmds[0]
            self.assertIn(b.ids['child_text'], cmd)
            top = _read_lines(b.paths['top'])
            self.assertTrue([d for d in top if d['payload'].get('type') == 'task_complete'])      # the Codex thread of the page is quiet: no command of it is still to come
            t = oracle.truth(c).subjects['child']
            self.assertEqual((t['tree'], t['rule_class'], t['page'], t['listed']), ('@orchc', 'certain', '@orchc', 'none'))
            self.assertIn(('child', 'tree', '@top'), oracle.truth(c).forbid)
            k = self.kid(b) if look == 'live' else None
            if look == 'live':
                self.assertEqual('CLAUDE_CODE_SESSION_ID' in k['env'], lure == 'launch_xargs')      # the tmux server's child has no names, the xargs child has the session's

    def test_stale_names_come_from_a_codex_thread_whose_turn_ended_hours_before_the_child(self):
        c, b = self.build(env='stale')
        k = self.kid(b)
        self.assertEqual((k['env']['CODEX_THREAD_ID'], k['env']['CODEX_SESSION_ID']), (b.ids['top'], b.ids['top']))
        self.assertEqual(k['ppid'], 1)                                                    # the tmux server's child
        top = _read_lines(b.paths['top'])
        done = [d for d in top if d['payload'].get('type') == 'task_complete']
        self.assertEqual(len(done), 1)
        self.assertGreater(b.T(0) - build_time(done[0]['timestamp']), 7000)                # the turn ended two hours before
        cmds = [d['message']['content'][0]['input']['command'] for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
        self.assertIn("tmux new-window -t w 'claude -p --model", cmds[0])
        self.assertIn(b.ids['child_text'], cmds[0])
        self.assertEqual(self.commands(b.paths['top']), [])                                # nothing of the old thread started the child
        t = oracle.truth(c)
        self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['rule_class']), ('@orchc', 'certain'))
        self.assertIn(('child', 'tree', '@top'), t.forbid)

    def test_a_child_nobody_names_beside_a_codex_command_without_a_record_and_a_running_claude_call(self):
        c, b = self.build(lure='gap', env='none')
        self.assertEqual(c.v['look'], 'live')
        rows = _read_lines(b.paths['top'])
        outs = [d['payload']['output'][0]['text'] for d in rows if d['payload'].get('type') == 'custom_tool_call_output']
        self.assertEqual(outs, ['Script running with cell ID 7'])
        self.assertEqual(self.commands(b.paths['top']), [])                                # the command that started the child has no record yet
        call = [d['payload']['input'] for d in rows if d['payload'].get('type') == 'custom_tool_call'][0]
        self.assertIn('tmux new-window -t w', call)
        self.assertIn(b.ids['child_text'].replace('"', '\\"'), call.replace('\\\\', '\\'))
        k = self.kid(b)
        self.assertEqual((k['env'], k['ppid']), ({}, 1))                                   # no names of Codex, none of Claude, nobody above it
        claude = [d for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
        self.assertIn(b.ids['child_text'], claude[0]['message']['content'][0]['input']['command'])
        self.assertEqual(len([d for d in _read_lines(b.paths['orchc']) if d['type'] == 'user' and d.get('toolUseResult')]), 0)      # the Claude call is still running
        t = oracle.truth(c)
        self.assertEqual(t.subjects.get('child'), None)
        self.assertEqual(t.forbid, [('child', 'rule_class', 'certain')])
        older = oracle.truth(axes.normalize(Case('cxo', dict(lure='gap')))).subjects['child']
        self.assertEqual((older['tree'], older['rule_class']), ('@orchc', 'guess'))        # the other gap case is as it was

    def test_on_macos_the_process_table_cannot_tell_a_codex_process_and_the_top_claude_session_is_never_the_certain_parent(self):
        c, b = self.build(chain='cl>cx>cl', env='both', how='fg', look='live', os='mac')
        self.assertEqual(c.v['os'], 'mac')
        k = self.kid(b)
        self.assertEqual(k['env']['CLAUDE_CODE_SESSION_ID'], b.ids['top'])
        self.assertEqual([p['fds'] for p in b.phases[0].procs if p['argv'][:2] == ['codex', 'exec']], [[b.paths['mid']]])      # the table has them; the observer's `ps` stand-in does not show them
        t = oracle.truth(c)
        self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['rule_class']), ('@mid', 'certain'))
        self.assertEqual(t.accepted[('child', 'tree')], {None})                           # held is as honest as the codex exec run
        self.assertEqual(t.accepted[('child', 'rule_class')], {'none'})
        self.assertIn(('child', 'tree', '@top'), t.forbid)
        self.assertEqual(t.subjects['mid']['tree'], '@top')
        lin = oracle.truth(axes.normalize(Case('cxo', dict(chain='cl>cx>cl', env='both', how='fg', look='live'))))
        self.assertEqual(lin.accepted, {})                                                # on Linux the lineage settles it: no other answer is as good

    def test_a_blind_thread_in_the_same_tree_does_not_take_the_node_of_a_launch_its_record_shows(self):
        c, b = self.build(host='sub', env='none', how='detach', look='ended')
        top = _read_lines(b.paths['top'])
        self.assertFalse([d for d in top if d['payload'].get('type') == 'task_complete'])     # the root's turn is still going: a thread whose commands the board cannot know yet
        launch = [x for x in self.commands(b.paths['host']) if b.ids['child_text'] in x['command'][2]]
        self.assertEqual(len(launch), 1)                                                   # the sub-agent's own record shows the launch
        t = oracle.truth(c).subjects['child']
        self.assertEqual((t['tree'], t['node'], t['rule_class']), ('@top', '@host', 'certain'))      # decision K2: the node is certain

    def test_a_python_file_that_only_types_the_words_is_a_relay_and_one_that_runs_claude_is_a_launch(self):
        c, b = self.build(lure='relay_pyfile', look='ended', how='detach', env='none')
        self.assertIn('relay_pyfile', axes.CXO_RELAYS)
        with open(os.path.join(b.scratch, 'relay.py')) as f:
            body = f.read()
        self.assertIn('subprocess.run(["tmux", "send-keys", "-t", "w", "claude -p \\"%s\\"" % sys.argv[1], "Enter"])', body)
        self.assertNotIn(b.ids['child_text'], body)                                       # the file holds `sys.argv[1]`, not the words
        calls = [d['message']['content'][0]['input']['command'] for d in _read_lines(b.paths['relayer']) if d['type'] == 'assistant']
        self.assertEqual(len(calls), 2)
        self.assertIn('cat > ', calls[0])                                                 # an earlier call writes the file, so its body can be read
        self.assertEqual(calls[1], 'python3 %s "%s"' % (os.path.join(b.scratch, 'relay.py'), b.ids['child_text']))
        t = oracle.truth(c)
        self.assertEqual(t.subjects['child']['tree'], '@top')
        self.assertIn(('child', 'tree', '@relayer'), t.forbid)
        c, b = self.build(lure='launch_pyfile', look='ended')
        with open(os.path.join(b.scratch, 'launch.py')) as f:
            body = f.read()
        self.assertIn('subprocess.run(["claude", "-p", "--model", ', body)               # it really runs claude
        self.assertTrue(b.ids['child_text'] in [d['message']['content'][0]['input']['command'] for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant'][-1])
        t = oracle.truth(c).subjects['child']
        self.assertEqual((t['tree'], t['rule_class']), ('@orchc', 'certain'))

    def test_the_names_of_a_thread_the_index_does_not_have_leave_a_claude_call_with_the_same_words_uncertain(self):
        c, b = self.build(lure='pin_unknown')
        k = self.kid(b)
        ghost = k['env']['CODEX_THREAD_ID']
        self.assertEqual(k['env']['CODEX_SESSION_ID'], ghost)
        self.assertEqual(k['ppid'], 1)
        self.assertNotIn('CLAUDE_CODE_SESSION_ID', k['env'])                              # only the unknown thread's names, nobody above it
        self.assertNotIn(ghost, ' '.join(glob_rollouts(b)))                               # no rollout of that thread
        self.assertNotIn(ghost, (b.ids['top'], b.ids.get('mid'), b.ids.get('host')))
        calls = [d for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
        self.assertIn(b.ids['child_text'], calls[0]['message']['content'][0]['input']['command'])
        self.assertEqual([d for d in _read_lines(b.paths['orchc']) if d.get('toolUseResult')], [])      # the Claude call is still running
        self.assertTrue([d for d in _read_lines(b.paths['top']) if d['payload'].get('type') == 'task_complete'])      # no turn of the page's thread is open: nothing else to blame
        t = oracle.truth(c)
        self.assertEqual((t.subjects.get('child'), t.forbid), (None, [('child', 'rule_class', 'certain')]))

    def test_the_names_a_tmux_server_kept_from_a_claude_session_do_not_make_it_the_parent(self):
        c, b = self.build(lure='pin_stale_claude')
        k = self.kid(b)
        p = [q for q in b.phases[0].procs if q['session'] and q['session']['sessionId'] == b.ids['orchc']][0]
        self.assertEqual((k['env']['CLAUDE_CODE_SESSION_ID'], k['env']['CLAUDE_PID']), (b.ids['orchc'], str(p['pid'])))     # the pin is valid: the process is alive and holds that session
        self.assertEqual(k['ppid'], 1)
        pr = [d for d in _read_lines(b.paths['orchc']) if d['type'] == 'assistant']
        calls = [d for d in pr if d['message']['content'][0].get('name') == 'Bash']
        self.assertEqual(len(calls), 1)
        self.assertLess(build_time(calls[0]['timestamp']), b.T(0) - 7000)                    # its only call ended hours before the child started: nothing is running
        self.assertEqual([x['command'][2] for x in self.commands(b.paths['top']) if "tmux new-window -t w 'claude -p" in x['command'][2]].__len__(), 1)    # the record of the Codex thread shows the launch
        t = oracle.truth(c).subjects['child']
        self.assertEqual((t['tree'], t['rule_class'], t['page']), ('@top', 'certain', '@top'))
        self.assertIn(('child', 'tree', '@orchc'), oracle.truth(c).forbid)
        self.assertIn(('evidence_conflict', 'child'), oracle.truth(c).diag)                # the stale names are dropped with a diagnostic

    def test_the_names_of_a_codex_thread_from_an_earlier_turn_say_nothing_for_the_turn_that_is_going_on(self):
        c, b = self.build(lure='stale_turn')
        self.assertEqual(c.v['env'], 'stale')
        rows = _read_lines(b.paths['top'])
        starts = [d for d in rows if d['payload'].get('type') == 'task_started']
        done = [d for d in rows if d['payload'].get('type') == 'task_complete']
        self.assertEqual((len(starts), len(done)), (2, 1))                                 # the earlier turn is over, the one that is going on is open
        cmds = self.commands(b.paths['top'])
        self.assertEqual([x['command'][2] for x in cmds], ['tmux new-session -d -s w', 'ls', 'ls'])      # the server in the earlier turn, only `ls` now
        calls = [d for d in rows if d['payload'].get('type') == 'custom_tool_call']
        self.assertEqual(len(calls), len(cmds))                                            # every command has its record: no gap
        # the commands of the turn that is going on are all over before the child starts: each call has its output and its CommandExecution, every one of them before t0
        t0 = b.T(scene_cxo.CHILD_START)
        outs = {d['payload']['call_id'] for d in rows if d['payload'].get('type') == 'custom_tool_call_output'}
        self.assertEqual({d['payload']['call_id'] for d in calls}, outs)
        for d in calls:
            self.assertLess(build_time(d['timestamp']), t0 - 50, d['payload']['call_id'])
        for d in rows:
            if d['payload'].get('type') in ('custom_tool_call_output', 'item_completed'):
                self.assertLess(build_time(d['timestamp']), t0 - 50)
        self.assertEqual(len([d for d in rows if (d['payload'].get('item') or {}).get('type') == 'CommandExecution']), len(calls))
        self.assertEqual([d for d in rows if d['payload'].get('type') == 'custom_tool_call_output' and 'running' in d['payload']['output'][0]['text']], [])      # nothing is still running
        second = [x for x in cmds if x['command'][2] == 'ls']
        self.assertTrue(all(build_time(d['timestamp']) > b.T(-300) for d in rows if d['payload'].get('item', {}).get('command', [None, None, ''])[2] == 'ls'))
        self.assertEqual(len(second), 2)
        k = self.kid(b)
        self.assertEqual((k['env']['CODEX_THREAD_ID'], k['env']['CODEX_SESSION_ID']), (b.ids['top'], b.ids['top']))
        t = oracle.truth(c).subjects['child']
        self.assertEqual((t['tree'], t['rule_class']), ('@orchc', 'certain'))
        self.assertIn(('child', 'tree', '@top'), oracle.truth(c).forbid)

    def test_the_whole_selection_of_the_bundle_builds_and_each_case_has_its_scene(self):
        n = 0
        for c in (c for c in run.select() if c.bundle == 'cxo'):
            _, b = self.build(**c.v)
            self.assertTrue(b.main_path and os.path.isfile(b.main_path), c.id)
            self.assertIn(b.meta['page'], ('claude', 'codex'), c.id)
            self.assertEqual(b.meta['page'] == 'claude', c.v['top'] == 'claude', c.id)
            self.assertTrue(b.phases, c.id)
            n += 1
        self.assertGreater(n, 200)

    # ---- the oracle ----
    def truth(self, **v):
        return oracle.truth(axes.normalize(Case('cxo', v)))

    def test_the_environment_and_the_lineage_decide_between_two_launches_with_the_same_words(self):
        t = self.truth(lure='twin_orch', how='fg', look='live', env='none')              # the lineage names the Codex orchestrator
        self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['rule_class']), ('@top', 'certain'))
        t = self.truth(lure='twin_orch', how='detach', look='live', env='codex')         # the environment does
        self.assertEqual(t.subjects['child']['tree'], '@top')
        for v in (dict(how='detach', look='live', env='none'), dict(how='fg', look='ended', env='codex'), dict(how='detach', look='ended', env='none')):
            t = self.truth(lure='twin_orch', **v)                                        # only the words: a tie, held
            self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['rule_class']), (None, 'none'), v)
            self.assertIn(('child', 'tree', '@top'), t.forbid)
        self.assertIn(('child', 'tree', '@orchc'), self.truth(lure='twin_orch', look='ended').forbid)

    def test_a_relay_and_a_script_link_nothing_to_the_session_or_the_script(self):
        for lure in ('relay', 'relay_py'):
            t = self.truth(lure=lure, how='detach', look='ended')
            self.assertEqual(t.subjects['child']['tree'], '@top')                       # the record of the call is the proof; the relayer is never the parent
            self.assertIn(('child', 'tree', '@relayer'), t.forbid)
            t = self.truth(lure=lure, how='detach', look='ended', rec='lost')
            self.assertEqual(t.subjects['child']['tree'], None)
            self.assertIn(('child', 'tree', '@relayer'), t.forbid)
        t = self.truth(lure='user_script', look='live')
        self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['page']), (None, '@child'))
        self.assertIn(('orphan_launch', 'orch'), t.allowed)                          # the orchestrator's own `codex exec` of the same folder has no child behind it: not asked

    def test_the_node_comes_from_the_environment_and_the_record_and_not_from_the_lineage(self):
        v = dict(host='sub', how='fg', look='live')
        self.assertEqual(self.truth(env='codex', **v).subjects['child']['node'], '@host')       # the one place an environment names a node
        t = self.truth(env='none', **v).subjects['child']                                       # the lineage alone: the tree, certain, and no node
        self.assertEqual((t['tree'], t['node'], t['rule_class']), ('@top', None, 'certain'))
        self.assertNotIn('parent', t)
        self.assertEqual(self.truth(env='none', host='sub', how='fg', look='ended').subjects['child']['node'], '@host')     # the record names it
        self.assertEqual(self.truth(host='main').subjects['child']['node'], None)

    def test_a_child_with_no_evidence_stands_alone_and_a_record_that_never_comes_is_none(self):
        t = self.truth(how='detach', look='ended', env='none', rec='lost').subjects['child']
        self.assertEqual((t['tree'], t['page'], t['listed']), (None, '@child', 'none'))
        t = self.truth(how='detach', look='live', env='none', rec='lost').subjects['child']
        self.assertEqual(t['tree'], None)                                              # setsid took it from the lineage, no environment, no record
        t = self.truth(how='detach', look='live', env='codex', rec='lost').subjects['child']
        self.assertEqual(t['tree'], '@top')
        t = self.truth(how='fg', look='live', env='none').subjects['child']
        self.assertEqual((t['tree'], t['status']), ('@top', 'running'))
        t = self.truth(rec='late', env='none', look='ended').subjects['child']
        self.assertEqual((t['tree'], t['status'], t['rule_class']), ('@top', 'done', 'certain'))      # in the end

    def test_an_ampersand_call_is_an_orphan_launch_and_has_no_card(self):
        t = self.truth(how='bg')
        self.assertEqual(t.diag, [('orphan_launch', 'orch')])
        self.assertEqual(t.diag_params[('orphan_launch', 'orch')], {'n': 1})
        self.assertNotIn('child', t.subjects)

    def test_both_providers_names_never_make_the_top_claude_session_the_parent_of_the_grandchild(self):
        for env in ('codex', 'both', 'none'):
            t = self.truth(chain='cl>cx>cl', env=env, how='fg', look='live')
            self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['parent']), ('@mid', '@mid'), env)
            self.assertIn(('child', 'tree', '@top'), t.forbid, env)
            self.assertEqual(t.subjects['mid']['tree'], '@top')
        t = self.truth(chain='cl>cx>cl', env='both', how='detach', look='live', edge='guess')
        self.assertEqual((t.subjects['child']['tree'], t.subjects['child']['listed']), (None, 'none'))
        self.assertEqual(t.subjects['mid']['rule_class'], 'guess')
        self.assertIn(('evidence_conflict', 'child'), t.diag)
        self.assertEqual({r for _, r, _ in t.forbid}, {'tree'})

    def test_a_guess_for_the_other_launcher_when_a_codex_thread_has_a_command_nobody_can_read(self):
        t = self.truth(lure='gap').subjects['child']
        self.assertEqual((t['tree'], t['rule_class'], t['listed'], t['page']), ('@orchc', 'guess', 'none', '@orchc'))

    def test_the_state_of_a_sub_agent_follows_its_record_and_its_parents_process(self):
        want = {'running': 'running', 'done': 'done', 'int_mid': 'interrupted', 'int_after': 'done', 'parent_gone': 'ended'}
        for sc, st in want.items():
            self.assertEqual(self.truth(subj='cx_sub', substate=sc).subjects['child']['status'], st, sc)
        t = self.truth(subj='cx_sub', host='sub', substate='parent_gone').subjects
        self.assertEqual((t['child']['status'], t['host']['status']), ('ended', 'ended'))
        t = self.truth(subj='cx_sub', host='sub', substate='int_mid').subjects
        self.assertEqual((t['child']['status'], t['host']['status']), ('interrupted', 'done'))

    def test_a_sub_agents_instruction_is_not_known_and_its_card_is_named_by_the_end_of_its_path(self):
        t = self.truth(subj='cx_sub', host='sub', substate='done')
        self.assertEqual((t.subjects['child']['label'], t.subjects['host']['label']), ('ss', 's1'))
        self.assertIsNone(t.subjects['child']['first_user'])
        self.assertEqual({(f, v) for _, f, v in t.forbid}, {('spawn_text', '@root_text'), ('spawn_text', '@notice')})
        self.assertEqual(t.subjects['child']['tree'], '@host')                         # a sub-agent below a sub-agent: its parent is that sub-agent; its page is the top
        self.assertEqual((t.subjects['child']['page'], t.subjects['child']['parent']), ('@top', '@host'))
        self.assertEqual(self.truth(subj='cx_sub', substate='done').subjects['child']['events'], {'spawn', 'agent_msg', 'handback'})
        self.assertEqual(self.truth(subj='cx_sub', substate='int_mid').subjects['child']['events'], {'spawn', 'agent_msg'})

    def test_the_guardian_has_no_card_and_only_its_own_calls_are_the_approval_review(self):
        t = self.truth(guard='one', subj='cx_sub', substate='done')
        self.assertEqual(t.subjects['guardian'], {'listed': 'none'})
        self.assertEqual(t.subjects['orch']['guardian_calls'], oracle.CXO_GUARD_CALLS)
        self.assertEqual(self.truth(subj='cx_sub').subjects['orch']['guardian_calls'], 0)      # a sub-agent's calls are its own card's

    def test_the_debate_seat_of_a_participant_follows_its_life_and_needs_a_link(self):
        t = self.truth(topic='talk', subj='cl', look='live').subjects['child']
        self.assertEqual((t['seat'], t['placements']), (None, frozenset()))                  # a run only told its path has written nothing yet
        t = self.truth(topic='talk', subj='cl', look='ended').subjects['child']
        self.assertEqual((t['unit'], t['round'], t['seat'], t['cell']), ('talk', 1, 'A', 'done'))       # it wrote the report with its Write tool
        t = self.truth(topic='talk', subj='cx', look='ended').subjects['child']
        self.assertEqual((t['seat'], t['cell']), ('B', 'done'))                              # `-o` of its command, and the run ended well
        t = self.truth(topic='talk_redir', subj='cl', look='ended', how='detach').subjects['child']
        self.assertEqual((t['seat'], t['cell']), ('A', 'done'))                              # the shell's redirect: the run ended well and the file holds its last message
        t = self.truth(topic='talk_redir', subj='cl', look='live').subjects['child']
        self.assertEqual((t['seat'], t['cell']), ('A', 'writing'))                          # asked for from the launch, not saved yet
        t = self.truth(topic='talk', subj='cl', how='detach', look='ended', env='none', rec='lost').subjects['child']
        self.assertNotIn('seat', t)                                                     # a run nothing links has no place in the page's debates

    def test_the_observer_reads_the_ownership_graph_when_the_board_has_one_and_the_older_tables_when_it_has_not(self):
        links = types.SimpleNamespace(owner_of=lambda g: {'parent': 'T', 'node': 'N', 'kind': 'cli', 'rule': 'env', 'certain': True} if g == 'x' else None)
        self.assertEqual(observe.owner_info(links, 'x'), {'tree': 'T', 'node': 'N', 'certain': True})
        self.assertIsNone(observe.owner_info(links, 'y'))
        links = types.SimpleNamespace(owner_of=lambda g: {'parent': 'T', 'node': None, 'kind': 'sub', 'rule': 'subagent', 'certain': False})
        self.assertEqual(observe.owner_info(links, 'x'), {'tree': 'T', 'node': None, 'certain': False})
        old = types.SimpleNamespace(cli_owners={'c': {'sid': 'S', 'rule': 'content', 'node': 'a1'}}, owners={'t': {'sid': 'S2', 'rule': 'time'}})
        self.assertEqual(observe.owner_info(old, 'c'), {'tree': 'S', 'node': 'a1', 'certain': True})
        got = observe.owner_info(old, 't')
        self.assertEqual((got['tree'], got['node'], got['certain']), ('S2', MISSING, False))
        self.assertIsNone(observe.owner_info(old, 'z'))

    def test_the_screen_parent_is_a_claim_when_it_is_none(self):
        self.assertEqual(run.grade('parent', None, None), 'pass')
        self.assertEqual(run.grade('parent', None, MISSING), 'miss')                 # not emitting it is a miss: None says "under the orchestrator"
        self.assertEqual(run.grade('parent', 'x', None), 'wrong')
        self.assertEqual(run.grade('parent', None, 'x'), 'wrong')
        self.assertEqual(run.grade('parent', 'x', MISSING), 'miss')


def iso_of(t):
    return build.iso(t)


def build_time(stamp):
    import calendar
    return calendar.timegm(time.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S')) + float(stamp[19:-1] or 0)


def _cost_state(path):
    return [d for d in _read_lines(path) if d.get('type') == 'cost-state']


def glob_rollouts(b):
    out = []
    for d, _, fs in os.walk(os.path.join(b.codex, 'sessions')):
        out += [os.path.join(d, f) for f in fs if f.startswith('rollout-')]
    return out


def glob_files(folder, pattern):
    import fnmatch
    out = []
    for d, _, fs in os.walk(folder):
        out += [os.path.join(d, f) for f in fs if fnmatch.fnmatch(f, pattern)]
    return out


class RerunScenes(unittest.TestCase):
    """The `rer` bundle: the same script is run again after the participants stopped, with the same command text; new sessions, the same instruction, the same output file."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-rer-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, **v):
        c = axes.normalize(Case('rer', v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def bash_calls(self, b):
        return [d['message']['content'][-1] for d in _read_lines(b.paths['orch']) if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == 'Bash']

    def test_the_id_and_the_selection(self):
        self.assertEqual(Case('rer', {}).id, 'rer:parts=ab;stop=cost')
        ids = {c.id for c in run.select() if c.bundle == 'rer'}
        self.assertEqual(ids, {'rer:parts=%s;stop=%s' % (p, s) for p in AXES['parts'] for s in AXES['stop']})
        for cid in ids:
            self.assertEqual(Case.from_id(cid).id, cid)

    def test_the_same_command_text_starts_the_participants_twice_with_one_instruction(self):
        c, b = self.build(parts='ab')
        calls = self.bash_calls(b)
        self.assertEqual(len(calls), 4)
        cmds = [x['input']['command'] for x in calls]
        self.assertEqual(cmds[0], cmds[2])                                                 # the same command text, called again after the brief was fixed
        self.assertEqual(cmds[1], cmds[3])
        self.assertTrue(cmds[0].endswith('$R/run_claude.sh mpd-A opus') and cmds[0].startswith('R='))
        self.assertTrue(cmds[1].endswith('$R/run_codex.sh mpd-B sol'))
        self.assertEqual([x['input']['description'] for x in calls], ['Start participant A', 'Start participant B', 'Start participant A again after the brief fix',
                                                                       'Start participant B again after the brief fix'])
        self.assertTrue(all(x['input'].get('run_in_background') for x in calls))
        first, second = _read_lines(b.paths['a1'])[0], _read_lines(b.paths['a2'])[0]
        self.assertEqual(first['message']['content'], second['message']['content'])        # the same instruction
        self.assertNotEqual(first['sessionId'], second['sessionId'])                        # a new session
        self.assertNotIn('forkedFrom', second)
        self.assertNotIn('--resume', ' '.join(cmds))
        t1 = [d['payload']['content'][0]['text'] for d in _read_lines(b.paths['b1']) if d['payload'].get('role') == 'user']
        t2 = [d['payload']['content'][0]['text'] for d in _read_lines(b.paths['b2']) if d['payload'].get('role') == 'user']
        self.assertEqual(t1, t2)
        self.assertIn(os.path.join(b.work, 'repo', 'docs', 'rev', 'r1', 'A.md'), first['message']['content'])
        self.assertNotEqual(b.ids['a1'], b.ids['a2'])
        self.assertNotEqual(b.ids['b1'], b.ids['b2'])
        self.assertEqual(len({b.ids['call_a1'], b.ids['call_a2'], b.ids['call_b1'], b.ids['call_b2']}), 4)

    def test_the_orchestrator_wrote_the_scripts_and_the_instruction_files_and_the_output_file_names_the_new_run(self):
        c, b = self.build(parts='ab')
        writes = [d['message']['content'][-1]['input'] for d in _read_lines(b.paths['orch']) if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == 'Write']
        names = sorted(os.path.basename(w['file_path']) for w in writes)
        self.assertEqual(names, ['mpd-A.txt', 'mpd-B.txt', 'run_claude.sh', 'run_codex.sh'])
        script = next(w for w in writes if w['file_path'].endswith('run_claude.sh'))
        self.assertIn('claude -p --model "$2" --output-format json "$(cat ', script['content'])
        self.assertIn('/out/$1.json', script['content'])                                    # every run redirects its result to a file named after the participant
        out = os.path.join(b.scratch, 'mpd', 'out', 'mpd-A.json')
        with open(out) as f:
            self.assertEqual(json.load(f)['session_id'], b.ids['a2'])                        # overwritten by the second run: the first run's id is gone
        edits = [d for d in _read_lines(b.paths['orch']) if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == 'Edit']
        self.assertEqual(len(edits), 1)                                                     # the brief was fixed between the runs
        t_edit = build_time(edits[0]['timestamp'])
        calls = [build_time(d['timestamp']) for d in _read_lines(b.paths['orch']) if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == 'Bash']
        self.assertEqual([c_ < t_edit for c_ in calls], [True, True, False, False])
        for x in ('A', 'B'):
            self.assertTrue(os.path.isfile(os.path.join(b.work, 'repo', 'docs', 'rev', 'r1', x + '.md')))      # the second runs wrote their reports

    def test_the_first_runs_stop_without_a_report_in_the_way_the_axis_says(self):
        c, b = self.build(parts='ab', stop='cost')
        self.assertEqual(len([d for d in _read_lines(b.paths['a1']) if d.get('type') == 'cost-state']), 1)
        self.assertFalse([d for d in _read_lines(b.paths['a1']) if d.get('type') == 'assistant' and d['message'].get('stop_reason') == 'end_turn'])     # no end of turn
        self.assertEqual([d['payload']['type'] for d in _read_lines(b.paths['b1']) if d['payload'].get('type') == 'turn_aborted'], ['turn_aborted'])
        c, b = self.build(parts='ab', stop='cut')
        self.assertEqual([d for d in _read_lines(b.paths['a1']) if d.get('type') == 'cost-state'], [])               # the record just stops
        self.assertEqual([d for d in _read_lines(b.paths['b1']) if d['payload'].get('type') in ('turn_aborted', 'task_complete')], [])
        for x in ('a1', 'b1'):
            rows = _read_lines(b.paths[x])
            self.assertLess(build_time(rows[-1]['timestamp']) - build_time(rows[0]['timestamp']), 130)              # about two minutes

    def test_only_the_chosen_participants_are_run(self):
        _, b = self.build(parts='a')
        self.assertEqual({r for r in b.ids if r in ('a1', 'a2', 'b1', 'b2')}, {'a1', 'a2'})
        self.assertEqual(len(self.bash_calls(b)), 2)
        _, b = self.build(parts='b')
        self.assertEqual({r for r in b.ids if r in ('a1', 'a2', 'b1', 'b2')}, {'b1', 'b2'})
        self.assertEqual(len(self.bash_calls(b)), 2)

    def test_the_truth_gives_the_seat_to_the_new_run_and_the_call_of_the_old_one_is_the_first_command(self):
        for parts in AXES['parts']:
            t = oracle.truth(axes.normalize(Case('rer', {'parts': parts})))
            for x in ('a', 'b'):
                if x not in parts:
                    self.assertNotIn(x + '2', t.subjects)
                    continue
                new, old = t.subjects[x + '2'], t.subjects[x + '1']
                self.assertEqual((new['call'], new['seat'], new['unit'], new['round'], new['cell']), ('@call_%s2' % x, x.upper(), 'docs/rev', 1, 'done'), (parts, x))
                self.assertEqual(new['placements'], oracle.place('docs/rev', 1, x.upper(), 'r1'))
                self.assertEqual((old['call'], old['seat'], old['placements']), ('@call_%s1' % x, None, frozenset()))
                self.assertEqual(t.accepted[(x + '1', 'call')], {None})                       # none is as honest as the first command; the second command is wrong
            self.assertEqual(t.diag, [])                                                       # no `seat_tie_held`
        t = oracle.truth(axes.normalize(Case('rer', {})))
        self.assertEqual(t.subjects['a2']['title'], '@title')
        self.assertEqual(t.subjects['a2']['start'], ('T', 330.0))
        self.assertEqual(t.subjects['b2']['start'], ('T', 331.0))
        self.assertEqual(run.grade('call', '@x', '@y'), 'wrong')

    def test_a_seat_that_is_held_is_a_diagnostic_the_truth_does_not_expect(self):
        c = axes.normalize(Case('rer', {}))
        t = oracle.truth(c)
        self.assertIn('seat_tie_held', oracle.diag_scope(c))                                  # graded: an entry the truth does not expect is wrong
        self.assertNotIn(('seat_tie_held', 'a1'), t.diag)


class ContractScenes(unittest.TestCase):
    """The contract scene (`ctr`): a debate folder, the participant under test `S`, a peer `P`, what comes later and the orchestrator's conclusion, varied by the structure axes
    `launch`, `wmethod`, `overlap`, `read`, `tag`, `later`, `final`, `life`. The truth is the contract's judgment of the plan; the builder writes what the plan says."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-ctr-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def case(self, **v):
        return axes.normalize(Case('ctr', v))

    def truth(self, **v):
        return oracle.truth(self.case(**v)).subjects

    def build(self, **v):
        c = self.case(**v)
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def test_the_axes_fold_to_what_can_be(self):
        self.assertEqual(self.case(launch='call').v['launch'], 'call')                         # one call starts both: the peer is a `claude -p` run as well
        self.assertEqual(self.case(overlap='orch', wmethod='python', launch='call').v['overlap'], 'orch')      # (O2: the launch call's window is no rival, so these combinations mean something)
        self.assertEqual(self.case(life='crash').v['life'], 'running')
        self.assertEqual(self.case(overlap='agent', wmethod='write').v['overlap'], 'none')      # only a file nobody was seen to write has a window to be told from others'
        self.assertEqual(self.case(overlap='orch', wmethod='python', tag='room').v['overlap'], 'orch')
        c = self.case(overlap='agent', wmethod='python', launch='far')
        self.assertEqual((c.v['overlap'], c.v['launch']), ('agent', 'msg'))                    # the other agent has to be at work
        self.assertEqual(self.case(later='none', final='early').v['final'], 'late')             # nothing comes later: after everything
        self.assertEqual(self.case(later='group', final='early').v['final'], 'early')
        self.assertEqual(Case.from_id(self.case(wmethod='python', overlap='orch').id).key(), self.case(wmethod='python', overlap='orch').key())

    def test_how_the_participant_saves_its_report_decides_who_owns_it(self):
        for wmethod in ('write', 'redirect', 'window'):                                         # a tool, a command in a deciding place, a command in a masked place saved inside its window
            t = self.truth(wmethod=wmethod)['S']
            self.assertEqual((t['seat'], t['cell'], t['placements']), ('S', 'done', frozenset(['talk|1|S|r1/S'])), wmethod)
        t = self.truth(wmethod='edit')['S']
        self.assertEqual((t['seat'], t['placements'], t['edits']), (None, frozenset(), frozenset(['talk|1|S|r1/S'])))     # an Edit of a file that was there: an editor, no owner
        t = self.truth(wmethod='stale')['S']
        self.assertEqual((t['seat'], t['placements'], t['edits']), (None, frozenset(), frozenset()))                      # `set -C; ...; true`: the old file stays, the save is not in the window
        t = self.truth(wmethod='python')['S']
        self.assertEqual((t['seat'], t['placements'], t['hint']), (None, frozenset(), 'S'))                               # nobody wrote it, one window could have
        for v in (dict(overlap='agent'), dict(overlap='orch'), dict(tag='room', overlap='orch'), dict(launch='call', overlap='agent')):     # another agent's window, any other of the orchestrator's: no one guess
            self.assertIsNone(self.truth(wmethod='python', **v)['S']['hint'], v)
        # O2: the call that launched the participant covers its whole run and is no rival of its own window: a `claude -p` run gets the hint too, unless the launch is not known
        for v in (dict(tag='room'), dict(launch='call'), dict(tag='seat', launch='far')):
            self.assertEqual(self.truth(wmethod='python', life='normal_end', **v)['S']['hint'], 'S', v)
        self.assertIsNone(self.truth(wmethod='python', life='normal_end', tag='room', launch='none')['S']['hint'])        # a call with no key cannot be told to be the launch
        self.assertIsNone(self.truth(wmethod='python', life='running', tag='room')['S']['hint'])                         # still at work: its own look-around call is open as well
        self.assertIsNone(self.truth(wmethod='write')['S']['hint'])
        self.assertEqual(self.truth(wmethod='write', life='running')['S']['cell'], 'draft')     # at work with a file: a draft
        self.assertEqual(self.truth(wmethod='write', life='normal_end')['S']['cell'], 'done')

    def test_who_is_placed_beside_the_others_and_why(self):
        for read in ('tool', 'cat'):
            for launch in ('msg', 'call'):                                                                                  # one message, or one call, started them both
                self.assertEqual(self.truth(wmethod='none', launch=launch, read=read)['S']['placed'], 'talk|-|launch_peer')     # started with a participant of the debate
            self.assertEqual(self.truth(wmethod='none', launch='far', read=read)['S']['placed'], 'talk|-|guide_read')       # only the guide it read (a command's read counts as well)
        for launch in ('far', 'none'):
            self.assertIsNone(self.truth(wmethod='none', launch=launch, read='none')['S']['placed'], launch)               # a call nobody can tell is no group
        self.assertEqual(self.truth(wmethod='none', tag='room', launch='far', read='none', life='running')['S']['placed'], 'talk|-|tag')
        t = self.truth(wmethod='none', tag='seat', launch='far', read='none', life='running')['S']
        self.assertEqual((t['seat'], t['cell']), ('S', 'writing'))                              # a tag with a seat asks for the cell: it is the participant's before it writes
        self.assertEqual(self.truth(wmethod='none', tag='seat', launch='far', read='none', life='normal_end')['S']['cell'], 'missing')

    def test_what_comes_later_and_the_conclusion(self):
        t = self.truth(later='group', final='early')
        self.assertEqual(t['Q1']['placements'], frozenset(['talk|2|Q1|r2/Q1']))
        self.assertEqual(t['listing']['finals'], frozenset())                                   # the conclusion came before round 2 was written
        self.assertEqual(self.truth(later='group', final='late')['listing']['finals'], frozenset(['talk|ruling.md']))
        self.assertEqual(self.truth(later='group', final='late')['listing']['closable'], frozenset(['talk']))
        self.assertEqual(self.truth(later='group', final='late', life='running')['listing']['finals'], frozenset())        # a participant is still at work
        t = self.truth(later='editors', final='early')
        self.assertEqual((t['E1']['edits'], t['E2']['edits']), (frozenset(['talk|1|P|r1/P']), frozenset(['talk|1|S|r1/S'])))
        self.assertEqual((t['E1']['seat'], t['listing']['finals']), (None, frozenset()))        # fixes after the conclusion take it back
        self.assertEqual(self.truth(later='editors', final='late')['listing']['finals'], frozenset(['talk|ruling.md']))
        self.assertEqual(self.truth(later='alone', final='late')['Q']['cell'], 'done')
        self.assertEqual(self.truth(final='none')['listing']['finals'], frozenset())

    def test_the_name_of_the_conclusion_changes_the_sort_of_the_candidates_and_nothing_else(self):
        """CONTRACT 3.3, L3: `ruling.md` -> `판정문.md` -> `notes.md` (a name that says a conclusion, one in the other language, one that does not): `confirmed`, the path's role and `why`
        are the same, and so is whether the debate can be closed; only the order of the candidates may differ."""
        for later, life in itertools.product(('none', 'group', 'editors'), ('running', 'normal_end')):
            for final in ('early', 'late'):
                seen = []
                for cname in AXES['cname']:
                    c = self.case(final=final, later=later, life=life, cname=cname)
                    ct = contract.truth(plan.ctr_scene(c.v).F.case())
                    f = ct['finals'][plan.CTR_UNIT]
                    name = plan.CTR_RULING_NAME[cname]
                    self.assertEqual(f['path'] in (None, plan.CTR_UNIT + '/' + name), True, (c.id, f))
                    seen.append((f['confirmed'], f['path'] is None, f['by'], f['why'], ct['closable'][plan.CTR_UNIT],
                                 [p.rsplit('/', 1)[-1] == name for p in f['candidates']]))
                self.assertEqual(len({str(x) for x in seen}), 1, (later, life, final, seen))
        # the order of the candidates is the one thing a name changes: with two documents the name that says a conclusion comes first, whatever else is the same
        facts = plan.ctr_scene(self.case(final='late', later='none', cname='plain').v).F
        facts.file(plan.CTR_UNIT + '/judgement.md', 120.0)
        facts.write('orch', plan.CTR_UNIT + '/judgement.md', 120.0, orch=True)
        got = contract.truth(facts.case())['finals'][plan.CTR_UNIT]
        self.assertEqual(got['candidates'], [plan.CTR_UNIT + '/judgement.md', plan.CTR_UNIT + '/notes.md'])      # neither name says a conclusion: the latest first ...
        facts = plan.ctr_scene(self.case(final='late', later='none', cname='plain').v).F
        facts.file(plan.CTR_UNIT + '/ruling.md', 119.0)
        facts.write('orch', plan.CTR_UNIT + '/ruling.md', 119.0, orch=True)
        got = contract.truth(facts.case())['finals'][plan.CTR_UNIT]
        self.assertEqual((got['candidates'][0], got['confirmed'], got['why']), (plan.CTR_UNIT + '/ruling.md', False, ['several']))     # ... `ruling.md` does: first, though nothing is confirmed

    def test_the_scene_is_built_as_the_axes_say(self):
        c, b = self.build(wmethod='window', tag='seat', read='cat', launch='far', life='running')
        self.assertTrue(b.paths['S'].endswith('.jsonl') and 'subagents' not in b.paths['S'])               # a `claude -p` run carries a tag, a sub-agent cannot
        calls = BuilderMatchesPlan.claude_calls(b.paths['S'])
        cmds = [i['command'] for n, i, _ in calls if n == 'Bash']
        self.assertEqual(cmds[0], 'cat %s' % os.path.join(b.meta['unit'], 'brief.md'))                    # `cat` of the guide ...
        write = [x for x in cmds if x.startswith('cat > ')][0]                                            # ... and the masked write
        self.assertIn('\nls ', write)                                                                     # a later command: the exit status says nothing about the write
        launch = [i['command'] for n, i, _ in BuilderMatchesPlan.claude_calls(b.paths['orch']) if n == 'Bash'][0]
        self.assertIn('BULLPEN_ROOM=%s BULLPEN_SEAT=r1/S claude -p' % b.meta['unit'], launch)               # the names are in the command (an ended run) ...
        kid = [p for ph in b.phases for p in ph.procs if p['session'] and p['session'].get('sessionId') == b.ids['S']][0]
        self.assertEqual((kid['env']['BULLPEN_ROOM'], kid['env']['BULLPEN_SEAT']), (b.meta['unit'], 'r1/S'))   # ... and in the environment of a live one
        c, b = self.build(wmethod='python', overlap='orch', read='none')
        self.assertIn('subagents', b.paths['S'])
        self.assertEqual([x['command'] for n, x, _ in BuilderMatchesPlan.claude_calls(b.paths['orch']) if n == 'Bash'], ['git status'])
        c, b = self.build(wmethod='stale')
        self.assertIn('set -C; printf new > ', [x['command'] for n, x, _ in BuilderMatchesPlan.claude_calls(b.paths['S']) if n == 'Bash'][0])
        self.assertEqual(os.path.getmtime(b.meta['report']), b.T(plan.GUIDE_T + 6))                           # the old file stays old
        c, b = self.build(launch='none')
        self.assertTrue(all('id' not in d['message'] for d in _read_lines(b.paths['orch']) if d.get('type') == 'assistant' and any(x.get('name') == 'Agent' for x in d['message']['content'])))
        c, b = self.build(later='group', final='early')
        self.assertTrue(os.path.isdir(os.path.join(b.meta['unit'], 'r2')))
        self.assertTrue(os.path.isfile(os.path.join(b.meta['unit'], 'ruling.md')))
        c, b = self.build(later='none')
        self.assertFalse(os.path.exists(os.path.join(b.meta['unit'], 'r2')))                               # an empty next round would block every conclusion

    def test_the_words_are_the_same_whatever_the_axes_say(self):
        texts = set()
        for v in (dict(), dict(wmethod='python', read='cat'), dict(tag='seat', launch='none'), dict(later='group', final='late', life='running')):
            c, b = self.build(**v)
            first = [d for d in _read_lines(b.paths['S']) if d.get('type') == 'user'][0]['message']['content']
            texts.add((first[:18], first[-26:]))                                                              # the frame of the sentence, whatever its words
        self.assertEqual(texts, {('Please review the ', 'and report in plain words.')})

    def test_the_selection_has_every_value_of_every_axis_and_the_reasons_name_the_contract(self):
        cases = [c for c in run.select() if c.bundle == 'ctr']
        self.assertGreater(len(cases), 120)
        for name in BUNDLES['ctr']:
            self.assertEqual({c.v[name] for c in cases}, {'running', 'normal_end'} if name == 'life' else set(AXES[name]), name)
        b = types.SimpleNamespace(ids={}, t0=0)

        def reason(field, ws, gs, role='S', result='wrong', **v):
            c = axes.normalize(Case('ctr', v))
            return run.reason_of(run.Cell(c, role, field, result, ws, gs, b))
        self.assertEqual(reason('seat', 'S', None, 'S', 'miss', launch='call', tag='seat', wmethod='python'), 'J13-TAG-SEAT')       # the seat of one run is read for both runs of one call
        self.assertEqual(reason('diag:seat_tie_held', None, 'seat_tie_held', 'P', launch='call', tag='seat', wmethod='python'), 'J13-TAG-SEAT')
        self.assertEqual(reason('placed', 'talk|-|tag', 'MISSING', 'S', 'miss', launch='call', tag='room', life='normal_end'), 'J13-TAG-LINK')      # an ended run with a tag prefix is not linked
        self.assertEqual(reason('placed', 'talk|-|tag', 'MISSING', 'S', 'miss', launch='call', tag='room', life='running'), 'J11-PLACED')
        self.assertEqual(reason('placed', 'talk|-|guide_read', 'MISSING', 'S', 'miss', launch='msg', tag='none', wmethod='none'), 'J11-PLACED')
        self.assertEqual(reason('unit', 'talk', None, 'S', 'miss', launch='msg', tag='none', wmethod='python'), 'J11-PLACED')
        self.assertEqual(reason('units', 'x', 'y', 'listing', final='late'), 'J9-LISTING')
        self.assertIsNone(reason('finals', 'x', 'y', 'listing', final='late'))                                   # a final the board does not confirm (or does) against J15 has no reason of its own now


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

    def test_a_conclusion_beside_the_room_closes_nothing(self):
        """A room is no topic of the bundle (it has no round folder) and the document beside it is written by no record: whatever it is called and whenever it came, no final is
        confirmed (J15 reads the documents beside the cells and the writes that prove them)."""
        for above, phase, proof in itertools.product(AXES['above'], AXES['phase'], ('told', 'wrote')):
            c = axes.normalize(Case('room', dict(bundle='root', above=above, phase=phase, proof=proof)))
            self.assertEqual(oracle.truth(c).subjects['listing']['finals'], frozenset(), (above, phase, proof))


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

    def rooms(self, **v):
        """{folder: kind} of the rooms the truth says the scene has."""
        return dict(r.split('|') for r in oracle.truth(self.case(**v)).subjects['listing']['rooms'])

    def calls(self, path, name):
        return [blk['input'] for d in _read_lines(path) if d.get('type') == 'assistant' for blk in d['message']['content'] if blk.get('type') == 'tool_use' and blk['name'] == name]

    def prompt(self, b, role):
        return _read_lines(b.paths[role])[0]['message']['content']

    # --- ids: nothing that does not use the new axes is renamed ---
    def test_the_new_axes_are_named_in_an_id_only_when_they_are_not_at_the_baseline(self):
        for bundle, names in (('room', ('rtime', 'code', 'cite', 'delivery', 'launch')), ('deb', ('qform',)), ('sta', ('entry', 'tail', 'process'))):
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
        """Started by calls of their own (a launch group is the only thing that ties them), they are no room, however their runs lay in time: the contract has no time rule."""
        for people, phase in itertools.product(('2', '3', '5'), AXES['phase']):
            self.assertEqual(self.rooms(rtime='overlap', people=people, phase=phase), {'docs/meeting': 'cells'}, (people, phase))
            self.assertEqual(self.rooms(rtime='sequential', people=people, phase=phase), {}, (people, phase))
            self.assertEqual(self.rooms(rtime='quiet', people=people, phase=phase), {}, (people, phase))               # every one still running: no more a room for that
        T = oracle.truth(self.case(rtime='sequential', phase='done'))
        self.assertEqual(T.subjects['listing']['units'], frozenset())
        for role in ('p1', 'p2', 'p3'):
            self.assertEqual(T.subjects[role], dict(seat=None, cell=None, role='none', placements=frozenset(), placed=None))
        # it only means something where a room could be one; and runs that never coexist send no message
        self.assertEqual(self.case(rtime='sequential', shape='r1').v['rtime'], 'overlap')                  # a round folder is a debate whenever it ran
        self.assertEqual(self.case(rtime='sequential', shape='none', talk='peer').v['rtime'], 'overlap')
        self.assertEqual(self.case(rtime='sequential', talk='peer').v['talk'], 'none')
        self.assertEqual(self.case(rtime='sequential', talk='orch').v['talk'], 'orch')                        # the orchestrator may message each of them
        self.assertEqual(self.rooms(rtime='sequential', talk='orch'), {})
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
        want = {('2', 'none'): True, ('2', 'one'): True, ('2', 'all'): False, ('3', 'one'): True, ('3', 'majority'): False, ('3', 'all'): False,
                ('5', 'one'): True, ('5', 'majority'): False, ('5', 'all'): False}
        for (people, code), room in want.items():
            for shape in ('beside', 'below'):
                self.assertEqual(bool(self.rooms(people=people, code=code, shape=shape)), room, (people, code, shape))
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
        self.assertEqual(self.rooms(people='3', code='all'), {})

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
        """Nothing is written (the scene is the words of an earlier instruction), so there is no room and no cell, in every form of the quote: the words are not read in any case."""
        for cite, shape, lang in itertools.product(AXES['cite'][1:], ('beside', 'below', 'mixed'), AXES['lang']):
            c = self.case(cite=cite, shape=shape, lang=lang)
            self.assertEqual(self.rooms(cite=cite, shape=shape, lang=lang), {}, c.id)
            T = oracle.truth(c)
            self.assertEqual(T.subjects['listing']['units'], frozenset(), c.id)
            for role in ('p1', 'p2', 'p3'):
                self.assertEqual(T.subjects[role], dict(seat=None, cell=None, role='none', placements=frozenset(), placed=None), c.id)
        # a folder that already has a round folder is a debate on the list (the walk finds it), and nobody in it sits anywhere: nothing was written
        for cite in AXES['cite'][1:]:
            c = self.case(cite=cite, shape='r1')
            T = oracle.truth(c)
            self.assertEqual(T.subjects['listing']['units'], frozenset(['docs/meeting']))
            self.assertEqual(T.subjects['p1'], dict(seat=None, cell=None, role='none', placements=frozenset(), placed=None))
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
            self.assertEqual(T.diag, [], c.id)
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
        self.assertEqual(oracle.truth(self.case('deb', role='quoter')).diag, [])                              # the one-line quote is no different: the words are not read

    # --- messages that were never delivered ---
    def test_a_message_that_was_answered_with_an_error_makes_no_meeting(self):
        for people, guide in itertools.product(('2', '3', '5'), ('agenda', 'brief')):
            self.assertEqual(self.rooms(shape='none', talk='peer', people=people, guide=guide), {'docs/meeting': 'members'}, (people, guide))
            self.assertEqual(self.rooms(shape='none', talk='peer', people=people, guide=guide, delivery='failed'), {}, (people, guide))
            self.assertEqual(self.rooms(shape='beside', talk='peer', people=people, guide=guide, delivery='failed'), {'docs/meeting': 'cells'}, (people, guide))     # files make a room by themselves
        self.assertEqual(self.case(delivery='failed').v['delivery'], 'ok')                                  # nobody to message
        self.assertEqual(self.case(delivery='failed', talk='orch').v['delivery'], 'ok')
        self.assertEqual(self.case(delivery='failed', talk='peer', people='1').v['delivery'], 'ok')
        T = oracle.truth(self.case(shape='none', talk='peer', delivery='failed'))
        self.assertEqual(T.subjects['listing']['units'], frozenset())
        self.assertEqual(T.subjects['p1'], dict(seat=None, cell=None, role='none', placements=frozenset(), placed=None))

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

        def reason(bundle_, subject, field, ws, gs, result='wrong', **v):
            c = axes.normalize(Case(bundle_, v))
            return run.reason_of(run.Cell(c, subject, field, result, ws, gs, b))
        self.assertEqual(reason('room', 'p1', 'placed', 'x', frozenset(), 'miss'), 'J11-PLACED')
        self.assertEqual(reason('room', 'listing', 'units', frozenset(['docs/meeting']), frozenset()), 'J9-LISTING')
        self.assertIsNone(reason('room', 'p1', 'seat', None, 'A', rtime='sequential'))                         # a room that is none for the contract has no reason of its own now: it is not red
        self.assertIsNone(reason('deb', 'child', 'seat', None, 'B', role='quoter', qform='fence'))
        for shape, want in ((dict(process='gone', tail='mid'), 'S-ORCH-DEAD'), (dict(tail='commands'), 'S-ORCH-COMMANDS'), (dict(entry='sdk', tail='end'), 'S-ORCH-SDK-END'),
                            (dict(), 'S-ORCH-STATE')):
            self.assertEqual(reason('sta', 'orch', 'orch_state', 'idle', 'working', skind='main', life='running', **shape), want, shape)
        for r in ('J9-LISTING', 'J11-PLACED', 'J13-TAG-LINK', 'J13-TAG-SEAT', 'J7-LIVE-OUT', 'J7-LIVE-REDIRECT', 'S-ORCH-DEAD', 'S-ORCH-COMMANDS', 'S-ORCH-SDK-END'):
            self.assertIn(r, run.REASONS)


class OrchestratorWriteScenes(unittest.TestCase):
    """The `owr` bundle: the orchestrator makes a folder that may be a debate (a tool, a patch, a command, only a `mkdir`, an attempt that failed, words that only name it); the
    scenes hold exactly that in the records and on disk, and the oracle lists the folder as its rule says."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='scen-owr-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, **v):
        c = axes.normalize(Case('owr', v))
        return c, build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=10)))

    def truth(self, **v):
        return oracle.truth(axes.normalize(Case('owr', v)))

    def calls(self, path, name):
        """The tool calls of a Claude record, with whether the result came back as an error: [(input, is_error)]."""
        lines = _read_lines(path)
        results = {c['tool_use_id']: c['is_error'] for d in lines if d['type'] == 'user' and isinstance(d['message']['content'], list)
                   for c in d['message']['content'] if c.get('type') == 'tool_result'}
        return [(d['message']['content'][-1]['input'], results.get(d['message']['content'][-1]['id'])) for d in lines
                if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == name]

    def items(self, path, kind):
        return [d['payload']['item'] for d in _read_lines(path) if d['type'] == 'event_msg' and d['payload'].get('type') == 'item_completed' and d['payload']['item']['type'] == kind]

    def unit(self, b):
        return os.path.join(b.home, axes.OWR_SITE_REL[b.case.v['dsite']])

    def test_the_id_and_the_folds(self):
        self.assertEqual(Case('owr', {}).id, 'owr:top=claude;ow=tool;dshape=brief_r1;dsite=plain;kid=none;copy=none')
        n = lambda **v: axes.normalize(Case('owr', v)).v                                 # noqa: E731
        self.assertEqual(n(top='cx_tui', ow='tool')['ow'], 'patch')                       # a patch is Codex's tool, the Write tool Claude's
        self.assertEqual(n(top='cx_exec', ow='tool')['ow'], 'patch')
        self.assertEqual(n(top='claude', ow='patch')['ow'], 'tool')
        self.assertEqual(n(copy='worktree', dsite='plain')['copy'], 'none')               # the copy is of a folder of the repository
        self.assertEqual(n(copy='worktree', dsite='repo')['copy'], 'worktree')
        self.assertEqual(n(copy='link')['copy'], 'none')
        self.assertEqual(n(kid='seated', dshape='brief_only')['kid'], 'none')             # a participant is told a report in a round folder
        for site in ('state', 'top', 'docs'):
            self.assertEqual(n(kid='seated', dsite=site)['kid'], 'none', site)
        self.assertEqual(n(kid='seated', dsite='scratch', dshape='readme_r1')['kid'], 'seated')
        self.assertEqual(n(kid='unlinked', dsite='state')['kid'], 'none')
        for c in (c for c in run.select() if c.bundle == 'owr'):
            self.assertEqual(Case.from_id(c.id).id, c.id)
            self.assertEqual(axes.normalize(c).id, c.id)

    def test_the_selection_has_every_write_of_every_shape_in_a_plain_and_a_scratch_folder_and_a_twin_of_each_listing_by_a_write(self):
        cases = [c for c in run.select() if c.bundle == 'owr']
        ids = {c.id for c in cases}
        self.assertTrue(120 <= len(ids) <= 200, len(ids))
        seen = {(c.v['ow'], c.v['dshape'], c.v['dsite']) for c in cases}
        for site in ('plain', 'scratch'):
            self.assertEqual({(o, sh) for o, sh, st in seen if st == site} >= set(itertools.product(AXES['ow'], AXES['dshape'])), True, site)      # the whole product
        for axis, values in (('top', run.OWR_TOPS), ('ow', AXES['ow']), ('dshape', AXES['dshape']), ('dsite', AXES['dsite']), ('kid', AXES['kid']), ('copy', ('none', 'worktree'))):
            self.assertEqual({c.v[axis] for c in cases}, set(values), axis)                  # every value of every axis is there
        for c in cases:
            v = c.v
            if axes.owr_listed(v) and not axes.owr_walked(v) and v['kid'] != 'seated':
                t = run.owr_twin_of(c)
                self.assertIn(t.id, ids, c.id)                                             # a case that lists a folder by a write has a twin where the write failed or was only words
                self.assertIn(t.v['ow'], ('failed', 'words'))
                self.assertEqual({k: x for k, x in t.v.items() if k != 'ow'}, {k: x for k, x in v.items() if k != 'ow'})
                self.assertFalse(axes.owr_listed(t.v) and not axes.owr_walked(t.v))
            else:
                self.assertIsNone(run.owr_twin_of(c))
        self.assertEqual([c.id for c in run.owr_shapes()], [c.id for c in run.owr_shapes()])       # deterministic

    def test_a_claude_orchestrator_writes_with_its_tool_a_command_or_words(self):
        for shape in ('brief_r1', 'declared2'):
            _, b = self.build(top='claude', ow='tool', dshape=shape)
            ((inp, err),) = self.calls(b.paths['orch'], 'Write')
            self.assertEqual((inp['file_path'], err), (os.path.join(self.unit(b), axes.OWR_GUIDE[shape]), False))
            self.assertEqual(self.calls(b.paths['orch'], 'Bash'), [])
        _, b = self.build(top='claude', ow='failed', dshape='brief_r1')
        ((inp, err),) = self.calls(b.paths['orch'], 'Write')
        self.assertTrue(err)                                                                 # the Write came back as an error
        self.assertTrue(all(e for _, e in self.calls(b.paths['orch'], 'Bash')))              # and so did the `mkdir`
        _, b = self.build(top='claude', ow='redirect', dshape='brief_r1')
        ((inp, err),) = self.calls(b.paths['orch'], 'Bash')
        self.assertIn("mkdir -p %s && cat > %s <<'EOF'" % (os.path.join(self.unit(b), 'r1'), os.path.join(self.unit(b), 'brief.md')), inp['command'])
        self.assertFalse(err)
        _, b = self.build(top='claude', ow='redirect', dshape='declared2')
        ((inp, _),) = self.calls(b.paths['orch'], 'Bash')
        self.assertNotIn('mkdir', inp['command'])                                            # no round folder to make
        _, b = self.build(top='claude', ow='mkdir_only', dshape='readme_r1')
        ((inp, _),) = self.calls(b.paths['orch'], 'Bash')
        self.assertEqual(inp['command'], 'mkdir -p %s' % os.path.join(self.unit(b), 'r1'))
        _, b = self.build(top='claude', ow='mkdir_only', dshape='brief_only')
        ((inp, _),) = self.calls(b.paths['orch'], 'Bash')
        self.assertEqual(inp['command'], 'mkdir -p %s' % self.unit(b))                       # the folder itself, no round folder
        _, b = self.build(top='claude', ow='words', dshape='brief_r1')
        ((inp, err),) = self.calls(b.paths['orch'], 'Bash')
        self.assertTrue(inp['command'].startswith('echo "mkdir -p '))
        self.assertEqual((self.calls(b.paths['orch'], 'Write'), err), ([], False))

    def test_a_codex_orchestrator_writes_with_a_patch_a_command_or_words(self):
        for top in ('cx_tui', 'cx_exec'):
            _, b = self.build(top=top, ow='patch', dshape='brief_r1')
            (fc,) = self.items(b.paths['top'], 'FileChange')
            self.assertEqual(list(fc['changes']), [os.path.join(self.unit(b), 'brief.md')])
            self.assertEqual((fc['status'], self.items(b.paths['top'], 'CommandExecution')), ('completed', []))
            _, b = self.build(top=top, ow='failed', dshape='brief_r1')
            self.assertEqual(self.items(b.paths['top'], 'FileChange'), [])                   # a patch that failed leaves no FileChange
            cmds = self.items(b.paths['top'], 'CommandExecution')
            self.assertEqual({(c['status'], c['exit_code']) for c in cmds}, {('failed', 1)})
            _, b = self.build(top=top, ow='redirect', dshape='readme_r1')
            (c,) = self.items(b.paths['top'], 'CommandExecution')
            self.assertEqual((c['status'], c['exit_code']), ('completed', 0))
            self.assertIn("cat > %s <<'EOF'" % os.path.join(self.unit(b), 'README.md'), c['command'][2])
            _, b = self.build(top=top, ow='mkdir_only', dshape='declared2')
            (c,) = self.items(b.paths['top'], 'CommandExecution')
            self.assertEqual(c['command'][2], 'mkdir -p %s' % os.path.join(self.unit(b), 'r1'))     # a brief that declares two results gets its round folder
            _, b = self.build(top=top, ow='words', dshape='brief_r1')
            (c,) = self.items(b.paths['top'], 'CommandExecution')
            self.assertTrue(c['command'][2].startswith('echo "mkdir -p '))
            self.assertEqual(self.items(b.paths['top'], 'FileChange'), [])

    def test_the_folder_is_on_disk_as_the_shape_says_whoever_made_it(self):
        want = {'brief_r1': ('brief.md', True), 'readme_r1': ('README.md', True), 'brief_only': ('brief.md', False), 'declared2': ('brief.md', False),
                'declared1': ('brief.md', False), 'notes': ('README.md', False)}
        for shape, (guide, rounds) in want.items():
            for ow in ('failed', 'words', 'tool'):
                _, b = self.build(ow=ow, dshape=shape)
                folder = self.unit(b)
                self.assertTrue(os.path.isfile(os.path.join(folder, guide)), (shape, ow))
                self.assertEqual(os.path.isdir(os.path.join(folder, 'r1')), rounds, (shape, ow))
        _, b = self.build(ow='mkdir_only', dshape='declared2')
        self.assertTrue(os.path.isdir(os.path.join(self.unit(b), 'r1')))                     # the orchestrator made it
        _, b = self.build(ow='mkdir_only', dshape='declared1')
        self.assertFalse(os.path.isdir(os.path.join(self.unit(b), 'r1')))
        with open(os.path.join(self.build(dshape='declared2')[1].home, axes.OWR_SITE_REL['plain'], 'brief.md')) as f:
            self.assertEqual(len(re.findall(r'`\w+\.md` \(reviewer', f.read())), 2)
        for site, rel in axes.OWR_SITE_REL.items():
            _, b = self.build(dsite=site)
            self.assertTrue(os.path.isfile(os.path.join(b.home, rel, 'brief.md')), site)
        _, b = self.build(dsite='state')
        self.assertTrue(self.unit(b).startswith(b.claude + os.sep))                         # under the agent's own state folder
        for site in ('plain', 'scratch', 'state'):
            _, b = self.build(dsite=site)
            top = self.unit(b)
            while top != b.home:                                                             # no repository above a folder that is no part of one
                self.assertFalse(os.path.exists(os.path.join(top, '.git')), site)
                top = os.path.dirname(top)

    def test_the_run_beside_the_folder(self):
        for top in ('claude', 'cx_tui'):
            c, b = self.build(top=top, ow='words', kid='seated')
            rpath = os.path.join(self.unit(b), 'r1', 'A.md')
            self.assertTrue(b.ids['kid_text'].endswith('Write your report to %s.' % rpath))
            first = _read_lines(b.paths['kid'])[0]['message']['content']
            self.assertEqual(first, b.ids['kid_text'])                                      # the run was told where to write
            writes = [d['message']['content'][-1]['input']['file_path'] for d in _read_lines(b.paths['kid']) if d['type'] == 'assistant' and d['message']['content'][-1].get('name') == 'Write']
            self.assertEqual(writes, [rpath])
            if top == 'claude':
                launch = [i['command'] for i, _ in self.calls(b.paths['orch'], 'Bash') if 'claude -p' in i['command']]
            else:
                launch = [x['command'][2] for x in self.items(b.paths['top'], 'CommandExecution') if 'claude -p' in x['command'][2]]
            self.assertEqual(len(launch), 1, top)
            self.assertIn(b.ids['kid_text'], launch[0])                                     # the launching call carries the words: a certain link
            self.assertFalse(launch[0].rstrip().endswith('&'))
            _, b = self.build(top=top, ow='words', kid='died')
            self.assertNotIn('kid', b.paths)                                                # a call that ends with `&` leaves no run
            launch = [i['command'] for i, _ in self.calls(b.paths['orch'], 'Bash') if 'claude -p' in i['command']] if top == 'claude' else \
                [x['command'][2] for x in self.items(b.paths['top'], 'CommandExecution') if 'claude -p' in x['command'][2]]
            self.assertTrue(launch and launch[0].rstrip().endswith('&'), top)
            _, b = self.build(top=top, ow='words', kid='unlinked')
            self.assertEqual(_read_lines(b.paths['kid'])[0]['message']['content'].split(' Write your')[0], b.ids['kid_text'])
            bash = [i['command'] for i, _ in self.calls(b.paths['orch'], 'Bash')] if top == 'claude' else [x['command'][2] for x in self.items(b.paths['top'], 'CommandExecution')]
            self.assertFalse([x for x in bash if b.ids['kid_text'] in x], top)             # nobody started it: no call has its words
            self.assertTrue(os.path.isfile(os.path.join(self.unit(b), 'r1', 'B.md')))
        _, b = self.build(ow='words', kid='unlinked', dshape='declared2')
        self.assertFalse(os.path.exists(os.path.join(self.unit(b), 'r1')))                  # nothing to write a report into

    def test_a_copy_in_a_linked_worktree_where_another_agent_works(self):
        for top in ('claude', 'cx_tui', 'cx_exec'):
            _, b = self.build(top=top, ow='words', dsite='repo', copy='worktree')
            wt = os.path.join(b.work, 'wt')
            with open(os.path.join(wt, '.git')) as f:
                self.assertTrue(f.read().startswith('gitdir: %s' % os.path.join(b.work, 'repo', '.git', 'worktrees')))
            for name in ('brief.md',):
                with open(os.path.join(wt, 'docs', 'talk', name)) as f, open(os.path.join(self.unit(b), name)) as g:
                    self.assertEqual(f.read(), g.read())
            self.assertTrue(os.path.isdir(os.path.join(wt, 'docs', 'talk', 'r1')))
            self.assertEqual('work/wt/docs/talk', oracle.OWR_COPY_REL)
            if top == 'claude':
                cwds = {d['cwd'] for p in glob_files(os.path.dirname(b.paths['orch']), 'agent-*.jsonl') for d in _read_lines(p)}
            else:
                cwds = {d['payload'].get('cwd') for p in glob_rollouts(b) for d in _read_lines(p) if d['type'] == 'session_meta'} - {None}
            self.assertIn(wt, cwds, top)                                                      # another agent of the page works in the worktree
            accepted = oracle.truth(axes.normalize(Case('owr', dict(top=top, dsite='repo', copy='worktree')))).accepted[('listing', 'units')]
            self.assertEqual(accepted, {frozenset(['work/wt/docs/talk'])})                   # the folder of the page or its copy: one debate
            nothing = oracle.truth(axes.normalize(Case('owr', dict(top=top, dsite='repo', copy='worktree', ow='failed')))).subjects['listing']['units']
            self.assertEqual(nothing, frozenset(['work/repo/docs/talk']))      # no write and nobody in either: one stands for them, the main checkout's (J17, O10)

    def test_the_truth_lists_a_folder_by_the_orchestrators_write_and_the_disk_and_by_nothing_else(self):
        def expected(ow, shape, site, kid):
            rounded = shape in ('brief_r1', 'readme_r1') or (ow == 'mkdir_only' and shape in ('brief_r1', 'readme_r1', 'declared2'))      # a debate is a folder with a round folder (J9)
            walked = site in ('repo', 'top', 'docs') and rounded                              # a walk of the repository lists what is a debate: a lone guide, declared result files or notes are none
            by_write = ow in ('tool', 'patch', 'redirect', 'mkdir_only') and rounded and site != 'state'
            return walked or kid == 'seated' or by_write
        n = 0
        for ow, shape, site, kid in itertools.product(AXES['ow'], AXES['dshape'], AXES['dsite'], AXES['kid']):
            for top in ('claude', 'cx_tui'):
                c = axes.normalize(Case('owr', dict(top=top, ow=ow, dshape=shape, dsite=site, kid=kid)))
                v = c.v
                units = oracle.truth(c).subjects['listing']['units']
                want = frozenset([axes.OWR_SITE_REL[site]]) if expected(v['ow'], shape, site, v['kid']) else frozenset()
                self.assertEqual(units, want, c.id)
                n += 1
        self.assertGreater(n, 800)
        self.assertEqual(self.truth(ow='mkdir_only', dshape='brief_only').subjects['listing']['units'], frozenset())       # `mkdir` of the folder itself names nothing
        self.assertEqual(self.truth(ow='tool', dshape='brief_only').subjects['listing']['units'], frozenset())           # a brief.md alone is a title, not a debate
        self.assertEqual(self.truth(ow='tool', dshape='declared1').subjects['listing']['units'], frozenset())
        self.assertEqual(self.truth(ow='tool', dshape='notes').subjects['listing']['units'], frozenset())
        self.assertEqual(self.truth(ow='tool', dshape='declared2').subjects['listing']['units'], frozenset())             # a brief.md that declares result files is no debate (J9, V2)
        self.assertEqual(self.truth(ow='mkdir_only', dshape='declared2').subjects['listing']['units'], frozenset(['work/plain/talk']))      # until the round folder is made
        self.assertEqual(self.truth(ow='failed', dshape='brief_r1').subjects['listing']['units'], frozenset())
        self.assertEqual(self.truth(ow='tool', dshape='brief_r1', dsite='state').subjects['listing']['units'], frozenset())
        self.assertEqual(self.truth(ow='failed', dshape='brief_only', dsite='repo').subjects['listing']['units'], frozenset())                   # a lone brief.md is no unit: the walk does not list it
        self.assertEqual(self.truth(ow='failed', dshape='brief_r1', dsite='repo').subjects['listing']['units'], frozenset(['work/repo/docs/talk']))      # the walk, whatever the orchestrator did
        self.assertEqual(self.truth(ow='words', dshape='brief_r1', dsite='top').subjects['listing']['units'], frozenset(['work/repo']))
        self.assertEqual(self.truth(ow='words', dshape='readme_r1', dsite='docs').subjects['listing']['units'], frozenset(['work/repo/docs']))

    def test_the_orchestrator_never_sits_and_the_seat_of_a_participant_does_not_depend_on_the_hint(self):
        for ow, shape, site, kid, top in itertools.product(AXES['ow'], AXES['dshape'], AXES['dsite'], AXES['kid'], ('claude', 'cx_exec')):
            c = axes.normalize(Case('owr', dict(top=top, ow=ow, dshape=shape, dsite=site, kid=kid)))
            t = oracle.truth(c).subjects
            self.assertEqual(t['orch']['rooms'], frozenset(), c.id)                         # no room: a write names a folder, no person
            self.assertEqual(t['orch']['cells'], frozenset(['%s|1|A|kid' % axes.OWR_SITE_REL[site]]) if c.v['kid'] == 'seated' else frozenset(), c.id)    # no cell is the orchestrator's
        seated = [{k: x for k, x in oracle.truth(axes.normalize(Case('owr', dict(top='claude', ow=ow, dshape='brief_r1', kid='seated')))).subjects['kid'].items()} for ow in ('tool', 'redirect', 'mkdir_only', 'failed', 'words')]
        self.assertTrue(all(x == seated[0] for x in seated))
        self.assertEqual((seated[0]['unit'], seated[0]['round'], seated[0]['seat'], seated[0]['role'], seated[0]['cell']), ('work/plain/talk', 1, 'A', 'writer', 'done'))
        self.assertEqual(seated[0]['placements'], {'work/plain/talk|1|A|r1/A'})
        for kid in ('died', 'none'):
            self.assertNotIn('kid', self.truth(kid=kid).subjects)
        self.assertEqual(self.truth(kid='unlinked').subjects['kid'], {'seat': None, 'role': 'none', 'placements': frozenset()})      # a record nobody started seats nobody, the folder listed or not

    def test_the_board_is_read_with_the_state_folders_of_the_case(self):
        seen = []
        real = observe.read_owr

        def spy(b, obs, objs):
            from board import units
            seen.append((units.STATE_DIRS, b.home))
            return real(b, obs, objs)
        c = axes.normalize(Case('owr', dict(dsite='state')))
        with tempfile.TemporaryDirectory(prefix='scen-owr-state-') as root, mock.patch.object(observe, 'read_owr', spy):
            run.run_case(c, root)
        ((dirs, home),) = seen
        self.assertEqual(dirs, (os.path.join(home, '.claude') + os.sep, os.path.join(home, '.codex') + os.sep))
        from board import units
        self.assertNotIn(home, ''.join(units.STATE_DIRS))                                    # and they are put back

    def test_a_folder_the_orchestrator_names_is_read_from_the_pages_debate_list(self):
        c = axes.normalize(Case('owr', dict(ow='words', dshape='brief_r1', dsite='repo', kid='seated')))
        with tempfile.TemporaryDirectory(prefix='scen-owr-read-') as root:
            cells = run.run_case(c, root)
        self.assertEqual({(x.role, x.field): x.result for x in cells},
                         {('listing', 'units'): 'pass', ('orch', 'rooms'): 'pass', ('orch', 'cells'): 'pass', ('kid', 'unit'): 'pass', ('kid', 'round'): 'pass', ('kid', 'seat'): 'pass',
                          ('kid', 'role'): 'pass', ('kid', 'placements'): 'pass', ('kid', 'cell'): 'pass'})
        got = {(x.role, x.field): x.gs for x in cells}
        self.assertEqual(got[('orch', 'cells')], ['work/repo/docs/talk|1|A|kid'])
        self.assertEqual(got[('listing', 'units')], ['work/repo/docs/talk'])

    def test_no_folder_of_the_orchestrators_writes_is_red(self):
        """The page lists a folder by its round folder (J9), whatever the orchestrator wrote and whatever a guide says it declares: all the cells of this bundle are green."""
        with open(os.path.join(REPO, 'tests', 'scenarios_xfail.json')) as f:
            doc = json.load(f)
        self.assertEqual([cid for cid in doc['cells'] if cid.startswith('owr:')], [])

    def test_the_diagnostics_asked_for_are_a_capped_list_only(self):
        self.assertEqual(oracle.diag_scope(axes.normalize(Case('owr', {}))), frozenset(['listing_capped']))


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
