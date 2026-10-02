"""Behaviour-preservation tests (helpers shared by Claude and Codex, the debate tables). They look only at results visible from outside (agent and session fields, events, status), not at function names.

    python3 -m unittest discover -s tests
    AB_SRC=<other copy folder> python3 -m unittest tests.test_preserve      # the same tests against another copy (e.g. the code before a fix)
"""
import os
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402

TS = '2026-09-30T00:00:00Z'
T = 1790726400.0


def claude_agent(n=1):
    return server.Agent('a%016x' % n, {'description': 'T1-A 조사'})


def tool_use(name, inp, tid='tu1', mid='m1'):
    return {'type': 'assistant', 'timestamp': TS, 'message': {'id': mid, 'model': 'claude-opus-5-5', 'content': [
        {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]}}


def codex_entry(n=1, model='gpt-6.1-sol'):
    return {'id': '019a%04d-0000-7000-8000-000000000000' % n, 'meta_ts': T, 'path': '/nonexistent/rollout-%d.jsonl' % n,
            'cwd': '/w', 'model': model}


def cx_rec(typ, pt, p, ts=T + 10):
    return {'ts': ts, 'type': typ, 'pt': pt, 'p': p}


class ToolAndSayRecording(unittest.TestCase):
    """2-2a: recording of tool calls and utterances (tool counts, ticks, activity, pending calls)."""

    def test_claude_tool_fields_all_lengths(self):
        for n in range(0, 301):
            a = claude_agent()
            a.feed(tool_use('Bash', {'command': 'x', 'description': 'd' * n}))
            brief = 'd' * n if n else 'x'
            self.assertEqual(a.pending['tu1']['text'], server.trunc(brief, 160), n)
            self.assertEqual(a.activity[-1], {'ts': T, 'kind': 'tool', 'name': 'Bash', 'text': server.trunc(brief, 240), 'cat': 'bash'}, n)
            self.assertEqual((a.tool_count, dict(a.tool_counts), a.ticks), (1, {'Bash': 1}, [(T, 'bash')]), n)

    def test_claude_tool_without_id_has_no_pending(self):
        a = claude_agent()
        a.feed({'type': 'assistant', 'timestamp': TS, 'message': {'id': 'm', 'content': [
            {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': '/x/y.md'}}]}})
        self.assertEqual(a.pending, {})
        self.assertEqual((a.tool_count, a.ticks, a.reads), (1, [(T, 'read')], {'/x/y.md': T}))

    def test_claude_text_block(self):
        a = claude_agent()
        long = 'é' * 450
        a.feed({'type': 'assistant', 'timestamp': TS, 'message': {'id': 'm', 'content': [
            {'type': 'text', 'text': '  ' + long + '\n'}, {'type': 'text', 'text': '   '}]}})
        self.assertEqual(list(a.texts), [{'ts': T, 'text': long}])
        self.assertEqual(list(a.activity), [{'ts': T, 'kind': 'text', 'name': '', 'text': server.trunc(long, 400), 'cat': 'other'}])
        self.assertEqual((a.tool_count, a.ticks), (0, []))

    def test_codex_tool_and_text(self):
        a = server.CodexAgent(codex_entry(), {})
        a.feed_cx(cx_rec('response_item', 'custom_tool_call', {'input': 'await tools.exec_command({cmd: "ls -la"})', 'call_id': 'c1'}))
        a.feed_cx(cx_rec('response_item', 'function_call', {'name': 'shell', 'arguments': 'z' * 300, 'call_id': 'c2'}))
        a.feed_cx(cx_rec('response_item', 'custom_tool_call', {'input': 'console.log(1)'}))
        a.feed_cx(cx_rec('response_item', 'message', {'role': 'assistant', 'content': [{'type': 'output_text', 'text': '  done.  '}]}))
        self.assertEqual(a.pending, {'c1': {'ts': T + 10, 'name': 'exec_command', 'text': 'ls -la'},
                                     'c2': {'ts': T + 10, 'name': 'shell', 'text': server.trunc('z' * 240, 160)}})
        acts = list(a.activity)
        self.assertEqual([(x['kind'], x['name'], x['cat']) for x in acts],
                         [('tool', 'exec_command', 'bash'), ('tool', 'shell', 'other'), ('tool', 'exec', 'other'), ('text', '', 'other')])
        self.assertEqual(acts[1]['text'], server.trunc('z' * 300, 240))
        self.assertEqual(acts[2]['text'], 'console.log(1)')
        self.assertEqual(acts[3]['text'], 'done.')
        self.assertEqual(list(a.texts), [{'ts': T + 10, 'text': 'done.'}])
        self.assertEqual((a.tool_count, dict(a.tool_counts), [c for _, c in a.ticks]),
                         (3, {'exec_command': 1, 'shell': 1, 'exec': 1}, ['bash', 'other', 'other']))
        a.feed_cx(cx_rec('response_item', 'custom_tool_call_output', {'call_id': 'c1'}))
        self.assertNotIn('c1', a.pending)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cm = patched(CODEX_NAMES=os.path.join(self.tmp.name, 'none.jsonl'))
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def claude(self):
        return server.Session(os.path.join(self.tmp.name, 'aaaaaaaa-0000-4000-8000-000000000000.jsonl'))

    def codex(self):
        e = dict(codex_entry(), path=os.path.join(self.tmp.name, 'rollout.jsonl'), origin='tui', guardian=False, first_user='hi')
        return server.CodexSession(e)

    # 2-2b
    def test_orch_last_action(self):
        s = self.claude()
        s._feed_main(tool_use('Bash', {'command': 'ls', 'description': 'd' * 200}))
        self.assertEqual(s.orch['last_action'], 'Bash: ' + server.trunc('d' * 200, 160))
        self.assertEqual(s.orch['last_action_ts'], T)
        c = self.codex()
        c._feed_cx(cx_rec('response_item', 'custom_tool_call', {'input': 'tools.exec_command({cmd: "%s"})' % ('q' * 300)}))
        self.assertEqual(c.orch['last_action'], 'exec_command: ' + server.trunc('q' * 240, 160))
        self.assertEqual(c.orch['last_action_ts'], T + 10)
        c._feed_cx(cx_rec('response_item', 'function_call', {'name': 'shell', 'arguments': '{"a":1}'}, ts=T + 20))
        self.assertEqual((c.orch['last_action'], c.orch['last_action_ts']), ('shell: {"a":1}', T + 20))

    # 2-2d
    def test_codex_usage_record(self):
        u1 = {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 5}
        u2 = {'input_tokens': 200, 'cached_input_tokens': 50, 'output_tokens': 7, 'reasoning_output_tokens': 2}
        a = server.CodexAgent(codex_entry(model=''), {})                  # the model is not known yet
        c = self.codex()
        c.orch['model'] = ''
        first = {'usage': u1, 'response_id': 'x1', 'thread_token_usage': {'input_tokens': 100}}
        for feed in (a.feed_cx, c._feed_cx):
            feed(cx_rec('token_usage_record', None, first))
            feed(cx_rec('token_usage_record', None, {'usage': u2}))         # without a response id: 'r<number of responses counted so far>'
        for holder, meter in ((a, a.tokens), (c, c.orch_tokens)):
            self.assertEqual([r for r, _ in holder.nomodel], ['x1', 'r1'])
            self.assertEqual((holder.seen['input_tokens'], holder.seen['cached_input_tokens'], holder.seen['output_tokens'],
                              holder.seen['reasoning_output_tokens']), (300, 90, 12, 2))
            self.assertEqual(holder.thread_total, {'input_tokens': 100})      # the second record has no thread_token_usage: the earlier value is kept
            self.assertEqual((meter.t['calls'], meter.ctx, meter.t['cost']), (2, 200, 0))   # without the model no cost can be priced
            self.assertEqual(meter.t['unpriced'], 2)
        a.feed_cx(cx_rec('turn_context', None, {'model': 'gpt-6.1-sol'}))          # once the model is known they are counted again
        self.assertEqual(a.nomodel, [])
        self.assertGreater(a.tokens.t['cost'], 0)
        self.assertEqual(a.tokens.t['calls'], 2)

    # 2-2e
    def test_user_say(self):
        s = self.claude()
        human = lambda text, ts: {'type': 'user', 'timestamp': ts, 'origin': {'kind': 'human'}, 'message': {'content': text}}
        s._feed_main(human('<local-command>x</local-command>', TS))
        s._feed_main(human('<system-reminder>r</system-reminder>', TS))
        s._feed_main(human('진행해 <system-reminder>r</system-reminder>', TS))
        self.assertEqual([(e['kind'], e['text']) for e in s.feed], [('user_say', '진행해')])
        c = self.codex()
        c._feed_cx(cx_rec('response_item', 'message', {'role': 'user', 'content': [{'type': 'input_text', 'text': '<custom>hi'}]}))
        c._feed_cx(cx_rec('response_item', 'message', {'role': 'user', 'content': [{'type': 'input_text', 'text': '<custom>hi'}]}, ts=T + 20))
        c._feed_cx(cx_rec('response_item', 'message', {'role': 'user', 'content': [{'type': 'input_text', 'text': '<custom>hi'}]}, ts=T + 400))
        self.assertEqual([(e['kind'], e['text'], e['ts']) for e in c.feed], [('user_say', '<custom>hi', T + 10), ('user_say', '<custom>hi', T + 400)])

    # 2-2f
    def test_stall_tail_differs(self):
        s = self.claude()
        a = claude_agent()
        a.spawn_ts, a.last_ts = T, None
        self.assertEqual(s.agent_status(a, True, T + server.STALL_SEC - 1), 'running')      # Claude: without last_ts, spawn_ts is the base
        self.assertEqual(s.agent_status(a, True, T + server.STALL_SEC + 1), 'stalled')
        b = server.CodexAgent(codex_entry(), {})
        b.spawn_ts, b.last_ts = T, None
        linker = server.CodexLinker(s)
        procs = {'ts': 10 ** 12, 'cmd': [b'codex ' + b.id.encode()], 'fds': set(), 'any': True}
        with patched(cx_procs=lambda: procs):
            self.assertEqual(linker.status(b, True, T + 100), 'stalled')                     # Codex: without last_ts, 0 is the base
            b.last_ts = T + 90
            self.assertEqual(linker.status(b, True, T + 100), 'running')
            b.pending['x'] = {'ts': T, 'name': 'sh', 'text': ''}
            b.last_ts = T
            self.assertEqual(linker.status(b, True, T + server.STALL_SEC + 100), 'running')
            self.assertEqual(linker.status(b, True, T + server.TOOL_STALL_SEC + 1), 'stalled')

    # 2-2g
    def test_model_numbers(self):
        s = self.claude()
        mk = lambda n, model, spawn, tag='': (lambda a: (setattr(a, 'model', model), setattr(a, 'spawn_ts', spawn), setattr(a, 'tag', tag), a)[-1])(claude_agent(n))
        agents = [mk(1, 'claude-opus-5-5', 30), mk(2, 'claude-opus-5-5', 10), mk(3, 'claude-sonnet-5-5', 20),
                  mk(4, 'claude-opus-5-5', 20, tag='A'), mk(5, 'claude-opus-5-5[1m]', 40)]
        s.agents = {a.id: a for a in agents}
        s._name_agents()
        self.assertEqual({a.id[-1]: a.auto_tag for a in agents}, {'1': 'opus5.5-2', '2': 'opus5.5', '3': 'sonnet5.5', '4': '', '5': 'opus5.5-3'})

    def test_codex_tags(self):
        s = self.claude()
        linker = s.codex
        ags = []
        for n, (model, meta, rtag) in enumerate([('gpt-6.1-sol', 30, ''), ('gpt-6.1-sol', 10, ''), ('gpt-6-astra', 20, ''),
                                                 ('gpt-6.1-sol', 5, 'B'), ('gpt-6.1-sol', 40, '')], 1):
            a = server.CodexAgent(codex_entry(n, model), {})
            a.meta_ts, a.report_tag = meta, rtag
            linker.agents[a.id] = a
            linker.tails[a.id] = server.Tail(a.path)
            ags.append(a)
        info = {a.id: {'sid': s.id} for a in ags}
        fake = types.SimpleNamespace(ready=threading.Event(), owned_by=lambda sid: info)
        fake.ready.set()
        with patched(LINKS=fake):
            self.assertTrue(linker.poll())                                                   # the names were set for the first time
            self.assertEqual({a.meta_ts: a.tag for a in ags}, {30: 'sol6.1-2', 10: 'sol6.1', 20: 'astra6', 5: 'B', 40: 'sol6.1-3'})
            self.assertFalse(linker.poll())                                                  # unchanged: no change reported
            ags[1].model = 'gpt-6-astra'                                                     # when a model changes, the names are set again and the poll reports changed
            self.assertTrue(linker.poll())
            self.assertEqual({a.meta_ts: a.tag for a in ags}, {30: 'sol6.1', 10: 'astra6', 20: 'astra6-2', 5: 'B', 40: 'sol6.1-2'})

    # 2-2h
    def test_notification_labels(self):
        s = self.claude()
        for n, status in enumerate(('completed', 'failed', 'killed', 'stopped')):
            aid = 'a%016x' % n
            s._notification(T + n, '<task-id>%s</task-id><status>%s</status><summary>s%d</summary>' % (aid, status, n))
        self.assertEqual([(e['kind'], e['title'], e['text']) for e in s.feed],
                         [('notify', '작업 끝', 's0'), ('notify', '실패', 's1'), ('notify', '중지됨', 's2'), ('notify', 'stopped', 's3')])

    def test_codex_turn_labels(self):
        s = self.claude()
        a = server.CodexAgent(codex_entry(), {})
        for n, status in enumerate(('done', 'failed', 'killed')):
            a.turns.append({'n': n, 'start': T + n * 10, 'end': T + n * 10 + 5, 'status': status, 'error': 'boom' if status == 'failed' else None,
                            'sha': None, 'msg': 'final %d' % n, 'user': 'u%d' % n, 'call': None, 'bash_ts': None, 'out': None,
                            'out_state': None, 'mapped': True})
        s.codex._derive(a)
        notes = [(e['title'], e['text']) for e in s.feed if e['kind'] == 'notify']
        self.assertEqual(notes, [('작업 끝', 'final 0'), ('실패', 'boom'), ('중지됨', 'final 2')])
        self.assertEqual([n['status'] for n in a.notifications], ['completed', 'failed', 'killed'])


class DebateTables(unittest.TestCase):
    """2-3: writer and reader tables, the round being written now, head cache."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        for d in ('t1/r1', 't1/r2'):
            os.makedirs(os.path.join(self.root, d))
        with open(os.path.join(self.root, 'brief.md'), 'w') as f:
            f.write('# 제목 A\n\n| 주제 | 폴더 | 선행 | 최종 산출물 |\n|---|---|---|---|\n| 하나 | `t1/` | — | `final/x.md` |\n')
        with open(os.path.join(self.root, 't1', 'brief.md'), 'w') as f:
            f.write('# 주제 하나 — 주제 지침\n**A — 조사**\n**B — 검토**\n')
        for rel in ('t1/r1/A.md', 't1/r1/B.md'):
            with open(os.path.join(self.root, rel), 'w') as f:
                f.write('x\n')
        self.s = server.Session(os.path.join(self.root, 'aaaaaaaa-0000-4000-8000-000000000000.jsonl'))

    def agent(self, n, tag, spawn, prompt, writes=(), reads=()):
        a = server.Agent('a%016x' % n, {'description': '%s 역할' % tag})
        a.spawn_prompt, a.spawn_ts = prompt, spawn
        a.writes = [{'ts': spawn + i, 'path': p} for i, p in enumerate(writes)]
        a.reads = {p: spawn + 5 for p in reads}
        self.s.agents[a.id] = a
        return a

    def test_cells(self):
        t1 = os.path.join(self.root, 't1')
        a = self.agent(1, 'A', T, '%s/r1/A.md 를 쓴다' % t1, writes=[t1 + '/r1/A.md'], reads=[t1 + '/r1/B.md'])
        b = self.agent(2, 'B', T + 1, '%s/r1/B.md 를 쓴다' % t1, writes=[t1 + '/r1/B.md'], reads=[t1 + '/r1/A.md', t1 + '/r1/B.md'])
        a.last_ts = b.last_ts = T + 10
        out, units = self.s.debates({a.id: 'running', b.id: 'done'})
        topic = out[0]['topics'][0]
        cells = {(r['p'], c['round']): c for r in topic['rows'] for c in r['cells']}
        ca, cb = cells[('A', 1)], cells[('B', 1)]
        self.assertEqual((ca['state'], ca['writer'], ca['agent'], ca['readers']), ('draft', a.id, a.id, ['B']))
        self.assertEqual((cb['state'], cb['writer'], cb['agent'], cb['readers']), ('done', b.id, b.id, ['A']))   # the writer is not a reader
        self.assertEqual(sorted(units[a.id]), [t1])

    def test_repeated_calls_same_result_and_head_follows_file(self):
        t1 = os.path.join(self.root, 't1')
        self.agent(1, 'A', T, '%s/r1/A.md 를 쓴다' % t1, writes=[t1 + '/r1/A.md'])
        first = self.s.debates({})
        self.assertEqual(first[0][0]['title'], '제목 A')
        self.assertEqual(self.s.debates({}), first)
        with open(os.path.join(self.root, 'brief.md'), 'w') as f:                      # a changed brief shows up on the next call
            f.write('# 제목 B\n')
        os.utime(os.path.join(self.root, 'brief.md'), (T, T + 99))
        self.assertEqual(self.s.debates({})[0][0]['title'], '제목 B')
        os.remove(os.path.join(self.root, 'brief.md'))                                  # when it is gone, the topic folder becomes the root
        self.assertEqual(self.s.debates({})[0][0]['title'], '주제 하나 — 주제 지침')
        os.remove(os.path.join(t1, 'brief.md'))                                         # when that is gone too, the folder name is used
        self.assertEqual(self.s.debates({})[0][0]['title'], 't1')

    def test_allowed_file_does_not_leak_round_state(self):
        t1 = os.path.join(self.root, 't1')
        a = self.agent(1, 'A', T, '%s/r1/A.md 를 쓴다' % t1, writes=[t1 + '/r1/A.md'])
        a.last_ts = T + 10
        running = self.s.debates({a.id: 'running'})[0][0]['topics'][0]['rows'][0]['cells'][0]['state']
        self.s.allowed_file(os.path.join(t1, 'r1', 'A.md'))                              # calls debates with a fake status ('done')
        again = self.s.debates({a.id: 'running'})[0][0]['topics'][0]['rows'][0]['cells'][0]['state']
        self.assertEqual((running, again), ('draft', 'draft'))


if __name__ == '__main__':
    unittest.main()
