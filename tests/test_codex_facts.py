"""Tests of what the Codex index states about each thread: its kind and parents (root, native sub-agent, guardian review, internal), the history a sub-agent copies from its
parent, the commands a thread ran (CommandExecution), the collaboration events (SubAgentActivity, agent_message) and the description of a tool call that has no literal.
The records are made up: only their shape follows what Codex writes. Every test reads a temporary folder only.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import start_patches  # noqa: E402

from board import codex_facts as F, codex_index, codex_parse, fingerprint  # noqa: E402

ROOT, SUB1, SUB2, SUB3, GUARD, ODD = ('aaaaaaaa-0000-4000-8000-00000000000%d' % i for i in range(1, 7))
T0 = 1791100000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def line(t, typ, payload, ordinal=None):
    """One record line as Codex writes it: compact JSON, the keys in this order."""
    d = {'timestamp': iso(t)}
    if ordinal is not None:
        d['ordinal'] = ordinal
    d['type'] = typ
    d['payload'] = payload
    return json.dumps(d, separators=(',', ':'), ensure_ascii=False)


def numbered(items):
    """Lines from (time, type, payload) triples, each with its ordinal (the line number minus one)."""
    return [line(t, typ, p, i) for i, (t, typ, p) in enumerate(items)]


def root_meta(tid=ROOT, source='exec', originator='codex_exec', **kw):
    m = {'id': tid, 'timestamp': iso(T0), 'cwd': '/work/demo', 'originator': originator, 'source': source}
    m.update(kw)
    return m


def sub_meta(tid, parent=ROOT, path='/root/s1', nick='Nova', depth=1, start=7, **kw):
    spawn = {'parent_thread_id': parent, 'depth': depth, 'agent_path': path, 'agent_nickname': nick, 'agent_role': None}
    m = {'session_id': parent, 'id': tid, 'forked_from_id': parent, 'parent_thread_id': parent, 'timestamp': iso(T0), 'cwd': '/work/demo', 'originator': 'codex-tui',
         'source': {'subagent': {'thread_spawn': spawn}}, 'thread_source': 'subagent', 'agent_nickname': nick, 'agent_path': path, 'subagent_history_start_ordinal': start}
    m.update(kw)
    return m


def guardian_meta(tid=GUARD, parent=ROOT):
    return {'id': tid, 'timestamp': iso(T0), 'cwd': '/work/demo', 'originator': 'codex_exec', 'parent_thread_id': parent,
            'source': {'subagent': {'other': 'guardian'}}, 'thread_source': 'guardian_review'}


def item_payload(item, tid=ROOT):
    return {'type': 'item_completed', 'thread_id': tid, 'turn_id': 'turn-1', 'item': item, 'started_at_ms': 1, 'completed_at_ms': 2}


SAME = object()


def cmd_item(cmd='echo hi', item_id='call-1', pid='101', command=SAME, cwd='file:///work/demo', status='completed', code=0, secs=0, nanos=0, out='hi\n'):
    """A CommandExecution item with the keys in the order Codex writes them."""
    return {'type': 'CommandExecution', 'id': item_id, 'process_id': pid, 'command': ['/bin/bash', '-lc', cmd] if command is SAME else command, 'cwd': cwd,
            'parsed_cmd': [{'type': 'unknown', 'cmd': cmd}], 'source': 'unified_exec_startup', 'status': status, 'stdout': out, 'stderr': '', 'aggregated_output': out,
            'exit_code': code, 'duration': {'secs': secs, 'nanos': nanos}, 'formatted_output': out}


def activity(kind, item_id, agent=SUB1, path='/root/s1'):
    return item_payload({'type': 'SubAgentActivity', 'id': item_id, 'kind': kind, 'agent_thread_id': agent, 'agent_path': path})


def message(author, recipient, *texts, encrypted=False, mid='amsg_1', turn=None):
    content = [{'type': 'input_text', 'text': t} for t in texts]
    if encrypted:
        content.append({'type': 'encrypted_content', 'encrypted_content': 'gAAAAAB' + 'x' * 20})
    p = {'type': 'agent_message', 'id': mid, 'author': author, 'recipient': recipient, 'content': content}
    if turn:
        p['internal_chat_message_metadata_passthrough'] = {'turn_id': turn, 'create_time': 1.5}
    return p


def exec_call(call_id):
    """The call of an exec cell (call_id comes before name)."""
    return {'type': 'custom_tool_call', 'id': 'ctc_' + call_id, 'status': 'completed', 'call_id': call_id, 'name': 'exec', 'input': 'text(await tools.exec_command({cmd: "sleep 5"}))'}


def wait_call(call_id, cell):
    return {'type': 'function_call', 'id': 'fc_' + call_id, 'name': 'wait', 'arguments': json.dumps({'cell_id': str(cell), 'max_tokens': 2500, 'yield_time_ms': 1000}, separators=(',', ':')), 'call_id': call_id}


def output(call_id, *parts, kind='custom_tool_call_output'):
    """The output of a call: a plain text, or a list of parts (the usual shape of a finished cell)."""
    out = parts[0] if len(parts) == 1 and parts[0].startswith('Script running') else [{'type': 'input_text', 'text': t} for t in parts]
    return {'type': kind, 'id': 'out_' + call_id, 'call_id': call_id, 'output': out}


def yielded(sid):
    """The result of an exec_command that went on in the background."""
    return json.dumps({'chunk_id': 'abc123', 'wall_time_seconds': 1.0, 'session_id': sid, 'original_token_count': 0, 'output': ''}, separators=(',', ':'))


def user_message(text):
    return {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}


def parse(raw, ts=T0 + 5):
    """parse_cmd_exec on a record line the way the index calls it: after the item prefix check."""
    raw = raw.encode() if isinstance(raw, str) else raw
    im = F.CX_ITEM_RE.search(raw, 0, F.CX_ITEM_HEAD)
    return F.parse_cmd_exec(raw, im.start() if im else 0, ROOT, ts)


class Rollouts(unittest.TestCase):
    """A throwaway rollout folder and an index that reads it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, 'sessions', '2026', '10', '04')
        os.makedirs(self.dir)
        start_patches(self, CODEX_SESSIONS=os.path.join(self.tmp.name, 'sessions'), CODEX_NAMES=os.path.join(self.tmp.name, 'names.jsonl'))
        self.idx = codex_index.CodexIndex()
        self.n = 0

    def path(self, tid):
        return os.path.join(self.dir, 'rollout-2026-10-04T00-00-00-%s.jsonl' % tid)

    def write(self, tid, lines, mode='w'):
        with open(self.path(tid), mode, encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    def append(self, tid, lines):
        self.write(tid, lines, 'a')

    def put(self, tid, meta, rest=(), ordinals=True):
        """A rollout from its meta and the (time, type, payload) triples after it."""
        items = [(T0, 'session_meta', meta)] + list(rest)
        self.write(tid, numbered(items) if ordinals else [line(t, typ, p) for t, typ, p in items])

    def refresh(self):
        self.idx.refresh(force=True)
        return self.idx

    def entry(self, tid):
        return self.refresh().get(tid)


class KindOfThread(Rollouts):
    def test_kind_by_the_shape_of_the_first_line(self):
        parent_other = '00000000-0000-4000-8000-000000000999'
        sub = sub_meta(SUB1)
        wrong_parent = sub_meta(SUB1, parent_thread_id=parent_other)       # the top-level parent is another thread than the one in thread_spawn
        no_start = sub_meta(SUB1)
        del no_start['subagent_history_start_ordinal']
        table = [
            ('a root of a tui', root_meta(source='cli', originator='codex-tui'), 'root', False),
            ('a root of an exec', root_meta(source='exec'), 'root', False),
            ('a root of an editor', root_meta(source='vscode', originator='Codex Desktop'), 'root', False),
            ('a root with no source at all', {'id': ROOT, 'cwd': '/w'}, 'root', False),
            ('a native sub-agent', sub, 'sub', False),
            ('a sub-agent whose parents disagree', wrong_parent, 'internal', True),
            ('a sub-agent whose history end is not given', no_start, 'internal', True),
            ('a guardian review (source and thread_source)', guardian_meta(), 'guardian', False),
            ('a guardian by source.subagent.other alone', {'id': GUARD, 'parent_thread_id': ROOT, 'source': {'subagent': {'other': 'guardian'}}}, 'guardian', False),
            ('a guardian by thread_source alone', {'id': GUARD, 'parent_thread_id': ROOT, 'source': 'exec', 'thread_source': 'guardian_review'}, 'guardian', False),
            ('a dict source of no known kind', {'id': ODD, 'source': {'something': {}}}, 'internal', True),
            ('a subagent source of no known kind', {'id': ODD, 'parent_thread_id': ROOT, 'source': {'subagent': {'other': 'review'}}}, 'internal', True),
            ('a plain source with a parent', {'id': ODD, 'parent_thread_id': ROOT, 'source': 'exec'}, 'internal', True),
            ('a parent of another type', {'id': ODD, 'parent_thread_id': 7, 'source': 'exec'}, 'internal', True),
            ('a spawn with no parent at the top', {'id': ODD, 'source': {'subagent': {'thread_spawn': {'parent_thread_id': ROOT}}}, 'subagent_history_start_ordinal': 3}, 'internal', True),
        ]
        for what, meta, kind, drift in table:
            with self.subTest(what):
                c = F.classify(meta)
                self.assertEqual((c['kind'], c['drift']), (kind, drift))
                self.assertEqual(c['depth'] == 0, kind == 'root')

    def test_the_index_entry_of_each_kind(self):
        self.put(ROOT, root_meta(source='cli', originator='codex-tui'))
        self.put(SUB1, sub_meta(SUB1, start=9, nick='Nova', depth=1))
        self.put(GUARD, guardian_meta())
        self.put(ODD, {'id': ODD, 'cwd': '/w', 'source': {'something': {}}})
        idx = self.refresh()
        r, s, g, o = (idx.get(t) for t in (ROOT, SUB1, GUARD, ODD))
        self.assertEqual([(e['kind'], e['guardian'], e['drift']) for e in (r, s, g, o)],
                         [('root', False, False), ('sub', True, False), ('guardian', True, False), ('internal', True, True)])
        self.assertEqual((s['parent'], s['agent_path'], s['nick'], s['depth'], s['prefix_ord']), (ROOT, '/root/s1', 'Nova', 1, 9))
        self.assertEqual((g['parent'], g['agent_path'], g['prefix_ord']), (ROOT, None, 0))
        self.assertEqual((r['parent'], r['agent_path'], r['nick'], r['depth'], r['prefix_ord']), (None, None, None, 0, 0))
        self.assertEqual({e['id'] for e in idx.children(ROOT)}, {SUB1, GUARD})

    def test_the_depth_of_a_sub_agent(self):
        self.assertEqual(F.classify(sub_meta(SUB1, depth=2, path='/root/a/b'))['depth'], 2)
        spawn_without_depth = sub_meta(SUB1, path='/root/a/b/c')
        del spawn_without_depth['source']['subagent']['thread_spawn']['depth']
        self.assertEqual(F.classify(spawn_without_depth)['depth'], 3)             # from the path
        spawn_without_path = sub_meta(SUB1, path=None, agent_path=None)
        del spawn_without_path['source']['subagent']['thread_spawn']['depth']
        c = F.classify(spawn_without_path)
        self.assertEqual((c['kind'], c['depth'], c['agent_path']), ('sub', 1, None))   # a child is at least one step down

    def test_root_of(self):
        a, b, c = SUB1, SUB2, SUB3
        self.put(ROOT, root_meta())
        self.put(a, sub_meta(a, parent=ROOT, path='/root/a', depth=1))
        self.put(b, sub_meta(b, parent=a, path='/root/a/b', depth=2))
        self.put(GUARD, guardian_meta(parent=b))
        self.put(c, sub_meta(c, parent='00000000-0000-4000-8000-000000000999', path='/root/c'))   # its parent has no rollout
        idx = self.refresh()
        self.assertEqual([idx.root_of(t) for t in (ROOT, a, b, GUARD)], [ROOT] * 4)
        self.assertIsNone(idx.root_of(c))
        self.assertIsNone(idx.root_of('00000000-0000-4000-8000-000000000998'))

    def test_root_of_stops_at_a_cycle_and_at_depth_eight(self):
        x, y = SUB1, SUB2
        self.put(x, sub_meta(x, parent=y, path='/root/x'))
        self.put(y, sub_meta(y, parent=x, path='/root/y'))
        self.assertIsNone(self.refresh().root_of(x))
        chain = ['bbbbbbbb-0000-4000-8000-0000000000%02d' % i for i in range(11)]
        self.put(chain[0], root_meta(chain[0]))
        for i in range(1, 11):
            self.put(chain[i], sub_meta(chain[i], parent=chain[i - 1], path='/root/' + '/'.join('n' * j for j in range(1, i + 1))))
        idx = self.refresh()
        self.assertEqual(idx.root_of(chain[8]), chain[0])           # eight steps up is allowed
        self.assertIsNone(idx.root_of(chain[9]))
        self.assertIsNone(idx.root_of(chain[10]))

    def test_root_of_does_not_pass_an_internal_thread(self):
        self.put(ROOT, root_meta())
        self.put(ODD, {'id': ODD, 'parent_thread_id': ROOT, 'source': 'exec'})                # internal
        self.put(SUB1, sub_meta(SUB1, parent=ODD, path='/root/a'))
        idx = self.refresh()
        self.assertIsNone(idx.root_of(ODD))
        self.assertIsNone(idx.root_of(SUB1))


class CopiedHistory(Rollouts):
    START = 7           # the last ordinal of the history copied from the parent

    def fork(self):
        """The lines a sub-agent's rollout starts with (ordinals 1..START): the parent's own record as it was when the sub-agent was made."""
        return [
            (T0 + 1, 'session_meta', root_meta(ROOT, source='cli', originator='codex-tui')),
            (T0 + 1, 'event_msg', {'type': 'task_started', 'turn_id': 'parent-turn'}),
            (T0 + 1, 'response_item', user_message('The parent was asked to do the whole job.')),
            (T0 + 1, 'turn_context', {'model': 'parent-model'}),
            (T0 + 1, 'event_msg', item_payload(cmd_item('echo parent', 'p-call'))),
            (T0 + 1, 'event_msg', activity('started', 'p-spawn', agent=SUB2, path='/root/other')),
            (T0 + 1, 'response_item', message('/root', '/root/other', 'a message of the parent to somebody else')),
        ]

    def own(self):
        return [
            (T0 + 2, 'event_msg', {'type': 'task_started', 'turn_id': 'own-turn'}),
            (T0 + 2, 'response_item', message('/root', '/root/s1', 'Review the module and report.', encrypted=True)),
            (T0 + 3, 'turn_context', {'model': 'child-model'}),
            (T0 + 4, 'event_msg', item_payload(cmd_item('echo child', 'c-call', secs=1), SUB1)),
            (T0 + 4, 'token_usage_record', {'thread_token_usage': {'input_tokens': 5, 'output_tokens': 2}}),
            (T0 + 5, 'event_msg', {'type': 'task_complete', 'turn_id': 'own-turn'}),
        ]

    def test_the_copied_lines_are_not_the_threads_own(self):
        self.put(SUB1, sub_meta(SUB1, start=self.START), self.fork() + self.own())
        e = self.entry(SUB1)
        self.assertEqual([t['user'] for t in e['turns']], [None])                 # the instruction is encrypted: not known
        self.assertEqual(e['turns'][0]['start'], T0 + 2)             # its own first turn, not the parent's turn at the copy time
        self.assertIsNone(e['first_user'])                           # and the note that comes with it is not an instruction
        self.assertEqual((e['model'], e['calls'], e['open']), ('child-model', 1, False))
        self.assertEqual(e['thread_total'], {'input_tokens': 5, 'output_tokens': 2})
        self.assertEqual([c['item_id'] for c in self.idx.cmds(SUB1)], ['c-call'])
        self.assertEqual([(c['kind'], c['author'], c['recipient'], c['text'], c['encrypted']) for c in self.idx.collab(SUB1)],
                         [('message', '/root', '/root/s1', 'Review the module and report.', True)])

    def test_a_rollout_that_is_only_the_copy(self):
        self.put(SUB1, sub_meta(SUB1, start=self.START), self.fork())
        e = self.entry(SUB1)
        self.assertEqual((e['open'], e['turns'], e['first_user'], e['model'], e['calls']), (False, [], None, '', 0))
        self.assertEqual((self.idx.cmds(SUB1), self.idx.collab(SUB1)), ((), ()))
        self.assertEqual(e['last_ts'], T0 + 1)                       # the time of the copy is still when the thread was made

    def test_a_sub_agent_of_a_desktop_or_exec_origin_has_turns_too(self):
        for origin in ('codex_exec', 'Codex Desktop', 'codex-tui'):
            self.put(SUB1, sub_meta(SUB1, start=self.START, originator=origin), self.fork() + self.own())
            self.assertEqual(len(self.entry(SUB1)['turns']), 1, origin)

    def test_a_record_without_ordinals_is_cut_by_the_line_number(self):
        self.put(SUB1, sub_meta(SUB1, start=self.START), self.fork() + self.own(), ordinals=False)
        e = self.entry(SUB1)
        self.assertEqual([c['item_id'] for c in self.idx.cmds(SUB1)], ['c-call'])
        self.assertEqual((len(e['turns']), len(self.idx.collab(SUB1))), (1, 1))

    def test_the_copy_may_end_between_two_reads(self):
        lines = numbered([(T0, 'session_meta', sub_meta(SUB1, start=self.START))] + self.fork() + self.own())
        self.write(SUB1, lines[:5])                                     # the copy is still being written
        e = self.entry(SUB1)
        self.assertEqual((e['open'], e['turns'], e['model']), (False, [], ''))
        self.append(SUB1, lines[5:10])                                  # the rest of the copy and the first own lines
        e = self.entry(SUB1)
        self.assertEqual((len(e['turns']), e['open'], len(self.idx.collab(SUB1))), (1, True, 1))
        self.append(SUB1, lines[10:])
        e = self.entry(SUB1)
        self.assertEqual((len(e['turns']), e['open'], e['model']), (1, False, 'child-model'))
        self.assertEqual([c['item_id'] for c in self.idx.cmds(SUB1)], ['c-call'])
        self.assertEqual(len(self.idx.collab(SUB1)), 1)

    def test_the_copy_may_end_between_two_reads_in_a_record_without_ordinals(self):
        lines = [line(t, typ, p) for t, typ, p in [(T0, 'session_meta', sub_meta(SUB1, start=self.START))] + self.fork() + self.own()]
        self.write(SUB1, lines[:5])                                     # the line number has to survive the reads: no line is counted twice
        self.assertEqual(self.entry(SUB1)['turns'], [])
        self.append(SUB1, lines[5:10])
        self.assertEqual((len(self.entry(SUB1)['turns']), len(self.idx.collab(SUB1))), (1, 1))     # the message of the copy (the 7th line) is not the thread's
        self.append(SUB1, lines[10:])
        e = self.entry(SUB1)
        self.assertEqual(([c['item_id'] for c in self.idx.cmds(SUB1)], e['model'], len(self.idx.collab(SUB1))), (['c-call'], 'child-model', 1))

    def test_the_instruction_of_a_sub_agent_is_never_taken_from_a_message(self):
        """The first message to a sub-agent is its instruction and Codex encrypts it; a plain one is no better: what is readable is the note that comes with it."""
        plain = [x if x[2].get('type') != 'agent_message' else (x[0], x[1], message('/root', '/root/s1', 'Review the module and report.')) for x in self.own()]
        plain.insert(1, (T0 + 2, 'response_item', message('/root/s2', '/root/s3', 'a note between two others')))
        plain.append((T0 + 6, 'response_item', message('/root', '/root/s1', 'A second instruction.')))
        self.put(SUB1, sub_meta(SUB1, start=self.START), self.fork() + plain)
        e = self.entry(SUB1)
        self.assertEqual((e['first_user'], e['turns'][0]['user']), (None, None))
        got = self.idx.collab(SUB1)
        self.assertEqual((len(got), got[1]['text'], got[1]['encrypted']), (3, 'Review the module and report.', False))        # the events are all there, with their text
        self.idx = codex_index.CodexIndex()
        self.put(SUB2, sub_meta(SUB2, path='/root/s2', start=self.START), self.fork() + self.own())
        e = self.entry(SUB2)
        self.assertEqual((e['first_user'], e['turns'][0]['user']), (None, None))

    def test_the_title_of_a_sub_agent_is_its_name(self):
        self.put(SUB1, sub_meta(SUB1, path='/root/team/worker', nick='Nova', start=self.START), self.fork() + self.own())
        self.put(SUB2, sub_meta(SUB2, path=None, nick='Rigel', agent_path=None, start=self.START), self.fork() + self.own())
        self.put(SUB3, sub_meta(SUB3, path='/root/s3', nick='Vega', start=self.START), self.fork() + [(T0 + 2, 'event_msg', {'type': 'task_started'}),
                                                                                                        (T0 + 2, 'response_item', user_message('Plain instruction.'))])
        idx = self.refresh()
        self.assertEqual([idx.title(idx.get(t)) for t in (SUB1, SUB2, SUB3)], ['worker', 'Rigel', 'Plain instruction.'])
        write_names = os.path.join(self.tmp.name, 'names.jsonl')
        with open(write_names, 'w') as f:
            f.write(json.dumps({'id': SUB1, 'thread_name': 'A name given by the user'}) + '\n')
        self.assertEqual(idx.title(idx.get(SUB1)), 'A name given by the user')

    def test_an_instruction_by_a_user_message_is_the_fallback(self):
        own = [x for x in self.own() if x[2].get('type') != 'agent_message']
        own.insert(1, (T0 + 2, 'response_item', user_message('Instruction in a user message.')))        # the way an exec thread is told
        self.put(SUB1, sub_meta(SUB1, start=self.START), self.fork() + own)
        self.assertEqual(self.entry(SUB1)['first_user'], 'Instruction in a user message.')

    def test_turns_of_an_exec_root_keep_their_instruction_as_they_did(self):
        steps = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 1, 'response_item', user_message('Do it.')), (T0 + 2, 'event_msg', {'type': 'task_complete'})]
        self.put(ROOT, root_meta(source='exec'), steps)
        self.assertEqual(self.entry(ROOT)['turns'], [{'start': T0 + 1, 'end': T0 + 2, 'user': 'Do it.'}])
        self.put(GUARD, dict(guardian_meta(), originator='codex_exec'), steps)
        self.assertEqual(self.entry(GUARD)['turns'], [])


class TurnSpans(Rollouts):
    """The turns of every root (TUI, desktop, exec) and sub-agent as spans, for the one who asks whether a time falls in a turn of the thread. No text for the ones that did not have it."""

    STEPS = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 1, 'response_item', user_message('A long instruction ' + 'x' * 500)), (T0 + 5, 'event_msg', {'type': 'task_complete'}),
             (T0 + 10, 'event_msg', {'type': 'task_started'}), (T0 + 12, 'event_msg', {'type': 'turn_aborted'}), (T0 + 20, 'event_msg', {'type': 'task_started'})]

    def test_every_kind_of_root_has_its_spans(self):
        for tid, meta in ((ROOT, root_meta(source='cli', originator='codex-tui')), (SUB2, root_meta(SUB2, source='vscode', originator='Codex Desktop')), (SUB3, root_meta(SUB3, source='exec'))):
            self.put(tid, meta, self.STEPS)
            e = self.entry(tid)
            self.assertEqual([(t['start'], t['end']) for t in e['turns']], [(T0 + 1, T0 + 5), (T0 + 10, T0 + 12), (T0 + 20, None)], tid)
            self.assertTrue(e['open'])

    def test_no_text_is_kept_for_a_root_that_was_not_read_for_its_instruction(self):
        self.put(ROOT, root_meta(source='cli', originator='codex-tui'), self.STEPS)
        self.put(SUB3, root_meta(SUB3, source='exec'), self.STEPS)
        self.refresh()
        self.assertEqual([t['user'] for t in self.idx.get(ROOT)['turns']], [None, None, None])
        self.assertTrue(self.idx.get(ROOT)['first_user'].startswith('A long instruction'))                # what the list shows as its title is as it was
        self.assertTrue(self.idx.get(SUB3)['turns'][0]['user'].startswith('A long instruction'))

    def test_a_turn_that_never_ended_ends_when_the_next_one_begins(self):
        self.put(ROOT, root_meta(source='cli', originator='codex-tui'), [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 4, 'event_msg', {'type': 'task_started'})])
        self.assertEqual([(t['start'], t['end']) for t in self.entry(ROOT)['turns']], [(T0 + 1, T0 + 4), (T0 + 4, None)])

    def test_a_sub_agent_has_its_own_turns_only(self):
        fork = [(T0 + 1, 'session_meta', root_meta()), (T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 1, 'event_msg', {'type': 'task_complete'})]
        own = [(T0 + 2, 'event_msg', {'type': 'task_started'}), (T0 + 3, 'event_msg', {'type': 'task_complete'})]
        self.put(SUB1, sub_meta(SUB1, start=len(fork)), fork + own)
        self.assertEqual([(t['start'], t['end']) for t in self.entry(SUB1)['turns']], [(T0 + 2, T0 + 3)])

    def test_only_the_newest_are_kept(self):
        n = codex_index.CX_TURNS_KEEP + 30
        steps = [(T0 + 2 * i + k, 'event_msg', {'type': 'task_started' if k == 0 else 'task_complete'}) for i in range(n) for k in (0, 1)]
        self.put(ROOT, root_meta(source='cli', originator='codex-tui'), steps)
        turns = self.entry(ROOT)['turns']
        self.assertEqual((len(turns), turns[0]['start'], turns[-1]['end']), (codex_index.CX_TURNS_KEEP, T0 + 60, T0 + 2 * (n - 1) + 1))

    def test_a_snapshot_of_the_spans_is_a_copy(self):
        self.put(ROOT, root_meta(source='cli', originator='codex-tui'), self.STEPS)
        e = self.entry(ROOT)
        e['turns'][0]['end'] = 0
        e['turns'].clear()
        self.assertEqual([t['end'] for t in self.idx.get(ROOT)['turns']], [T0 + 5, T0 + 12, None])

    def test_an_entry_made_by_hand_without_turns_can_be_read(self):
        idx = codex_index.CodexIndex()
        idx.by_id[ROOT] = {'id': ROOT, 'guardian': False, 'path': '/nowhere', 'thread_total': None}
        self.assertEqual(idx.get(ROOT)['id'], ROOT)

    def test_the_tail_of_a_big_rollout_gives_the_spans_it_holds_without_repeats(self):
        with mock.patch.object(codex_index, 'CX_BIG', 60000), mock.patch.object(codex_index, 'CX_TAIL', 20000):
            steps = []
            for i in range(120):
                steps += [(T0 + 10 * i, 'event_msg', {'type': 'task_started'}), (T0 + 10 * i + 1, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))),
                          (T0 + 10 * i + 5, 'event_msg', {'type': 'task_complete'})]
            self.put(ROOT, root_meta(source='cli', originator='codex-tui'), steps)
            first = self.entry(ROOT)
            self.assertTrue(first['partial'])
            spans = [(t['start'], t['end']) for t in first['turns']]
            self.assertTrue(0 < len(spans) < 120)
            self.assertEqual(spans[-1], (T0 + 1190, T0 + 1195))
            self.assertTrue(all(t['user'] is None for t in first['turns']))
            self.append(ROOT, [line(T0 + 1300, 'event_msg', {'type': 'task_started'}, 400)])
            after = [(t['start'], t['end']) for t in self.entry(ROOT)['turns']]
            self.assertEqual(len(after), len(set(after)))                                              # read again from the tail: not added to
            self.assertEqual(after[-1], (T0 + 1300, None))

    def test_an_end_whose_start_is_before_the_tail_is_a_span_without_a_start(self):
        with mock.patch.object(codex_index, 'CX_BIG', 60000), mock.patch.object(codex_index, 'CX_TAIL', 20000):
            steps = [(T0, 'event_msg', {'type': 'task_started'})] + [(T0 + 1 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(300)]
            steps.append((T0 + 400, 'event_msg', {'type': 'task_complete'}))
            self.put(ROOT, root_meta(source='cli', originator='codex-tui'), steps)
            e = self.entry(ROOT)
        self.assertTrue(e['partial'])
        self.assertEqual([(t['start'], t['end']) for t in e['turns']], [(None, T0 + 400)])


class Commands(Rollouts):
    def run_of(self, *items, meta=None):
        """A rollout of one root with these items, read by an index of its own (a file rewritten within the clock's tick would keep its size and time)."""
        self.idx = codex_index.CodexIndex()
        self.put(ROOT, meta or root_meta(), [(T0 + 1 + i, 'event_msg', item_payload(it)) for i, it in enumerate(items)])
        return self.entry(ROOT), self.idx.cmds(ROOT)

    def test_a_command_with_everything(self):
        e, cmds = self.run_of(cmd_item('ls -la /tmp', 'c1', '4242', cwd='file:///work/demo', secs=2, nanos=500000000))
        self.assertEqual(cmds, ({'thread': ROOT, 'item_id': 'c1', 'process_id': '4242', 'cmd': 'ls -la /tmp', 'cwd': '/work/demo', 'start': T0 + 1 - 2.5, 'end': T0 + 1,
                                 'status': 'completed', 'exit_code': 0, 'offset': cmds[0]['offset']},))
        self.assertEqual((e['cmds_skipped'], e['cmds_big'], e['cmds_evicted']), (0, 0, 0))

    def test_a_failed_command(self):
        _, cmds = self.run_of(cmd_item('false', status='failed', code=1))
        self.assertEqual((cmds[0]['status'], cmds[0]['exit_code']), ('failed', 1))

    def test_the_shape_of_command(self):
        table = [
            (['/bin/bash', '-lc', 'echo a b'], 'echo a b'), (['/bin/bash', '-c', 'echo a'], 'echo a'), (['sh', '-c', 'true'], 'true'), (['/usr/bin/zsh', '-lc', 'x'], 'x'),
            (['/bin/bash'], None), (['/bin/bash', '-lc'], None), (['/bin/bash', '-ic', 'echo a'], None), (['/bin/bash', '-l', '-c', 'echo a'], None),
            (['python3', '-c', 'print(1)'], None), (['git', 'status'], None), (['/bin/bash', '-lc', ['x']], None), ([], None), ('echo a', None), ({'a': 1}, None), (None, None),
        ]
        for command, cmd in table:
            with self.subTest(command):
                _, cmds = self.run_of(cmd_item(command=command, cmd='x'))
                self.assertEqual([(c['cmd'], c['status']) for c in cmds], [(cmd, 'completed')])        # the run is kept even when its text is not

    def test_the_folder(self):
        table = [('file:///work/demo', '/work/demo'), ('file:///work/a%20b', '/work/a b'), ('file://localhost/work/demo', '/work/demo'), ('/work/plain', '/work/plain'),
                 ('file://other-host/work', None), ('relative/dir', None), ('', None), (None, None), (['/work'], None)]
        for cwd, want in table:
            with self.subTest(cwd):
                _, cmds = self.run_of(cmd_item(cwd=cwd))
                self.assertEqual(cmds[0]['cwd'], want)

    def test_other_states_and_odd_values(self):
        e, cmds = self.run_of(cmd_item('a', 'c1', status='in_progress'), cmd_item('b', 'c2', status='declined'), cmd_item('c', 'c3', code=None), cmd_item('d', 'c4', code=True),
                              cmd_item('e', 'c5', pid=None), cmd_item('f', 'c6', pid=17))
        self.assertEqual([c['item_id'] for c in cmds], ['c3', 'c4', 'c5', 'c6'])
        self.assertEqual(e['cmds_skipped'], 2)
        self.assertEqual([c['exit_code'] for c in cmds], [None, None, 0, 0])                         # null stays None; a bool is no exit code
        self.assertEqual([c['process_id'] for c in cmds], ['101', '101', None, '17'])

    def test_other_items_are_not_commands(self):
        _, cmds = self.run_of({'type': 'FileChange', 'id': 'f1', 'changes': {}, 'status': 'completed'}, {'type': 'Reasoning', 'id': 'r1'}, {'type': 'AgentMessage', 'id': 'm1'},
                              {'type': 'UserMessage', 'id': 'u1', 'content': []}, {'type': 'CollabAgentToolCall', 'id': 'w1', 'tool': 'wait'}, cmd_item('kept', 'c1'))
        self.assertEqual([c['item_id'] for c in cmds], ['c1'])

    def test_the_text_of_an_output_is_not_the_key(self):
        trap = 'before "exit_code":99,"duration":{"secs":9,"nanos":9} after "source":"x","status":"failed"'
        raw = line(T0, 'event_msg', item_payload(cmd_item('echo trap', code=0, secs=3, out=trap)), 5)
        self.assertIn('\\"exit_code\\":99', raw)                                       # inside a string every quote is escaped
        c, _ = parse(raw, ts=T0)
        self.assertEqual((c['exit_code'], c['status'], c['start']), (0, 'completed', T0 - 3))

    def test_a_line_over_the_decode_limit_is_read_from_its_front(self):
        huge = 'x' * (codex_parse.CX_LINE_MAX + 100)
        raw = line(T0, 'event_msg', item_payload(cmd_item('make all', 'big1', '77', cwd='file:///work/demo', status='failed', code=2, secs=7, out=huge)), 9)
        self.assertGreater(len(raw), codex_parse.CX_LINE_MAX)
        c, big = parse(raw, ts=T0 + 10)
        self.assertEqual((c['cmd'], c['cwd'], c['status'], c['item_id'], c['process_id'], big), ('make all', '/work/demo', 'failed', 'big1', '77', False))
        self.assertEqual((c['exit_code'], c['start'], c['end']), (2, T0 + 3, T0 + 10))             # found behind the output because it is where Codex puts it

    def test_a_big_line_whose_back_part_cannot_be_read_has_no_exit_code_and_no_duration(self):
        huge = 'x' * (codex_parse.CX_LINE_MAX + 100)
        item = cmd_item('make all', out=huge)
        item['duration'] = {'nanos': 5, 'secs': 7}                                                  # another order than the one that is read
        c, _ = parse(line(T0, 'event_msg', item_payload(item), 9), ts=T0 + 10)
        self.assertEqual((c['cmd'], c['exit_code'], c['start'], c['end']), ('make all', None, T0 + 10, T0 + 10))
        item = cmd_item('make all', out=huge)
        del item['exit_code'], item['duration']
        c, _ = parse(line(T0, 'event_msg', item_payload(item), 9), ts=T0 + 10)
        self.assertEqual((c['status'], c['exit_code'], c['start']), ('completed', None, T0 + 10))

    def test_a_big_line_whose_front_cannot_be_read_is_dropped_and_counted(self):
        huge = 'x' * (codex_parse.CX_LINE_MAX + 100)
        item = cmd_item('make all', out=huge)
        item = {'type': item.pop('type'), 'command': item.pop('command'), **item}                   # the command before the id
        self.assertEqual(parse(line(T0, 'event_msg', item_payload(item), 9)), (None, False))
        e, cmds = self.run_of(item, cmd_item('after', 'c9'))
        self.assertEqual(([c['item_id'] for c in cmds], e['cmds_skipped']), (['c9'], 1))

    def test_the_slow_way_reads_a_small_line_the_front_cannot(self):
        item = cmd_item('echo reordered', 'c7', '12', secs=1, nanos=250000000, code=3, status='failed')
        reordered = {'type': item.pop('type'), 'command': item.pop('command'), **item}
        spaced = json.dumps({'timestamp': iso(T0), 'type': 'event_msg', 'payload': item_payload(cmd_item('echo spaced', 'c8', '13', status='failed', code=3, secs=1, nanos=250000000))})
        for what, raw, want in (('reordered', line(T0, 'event_msg', item_payload(reordered), 9), 'echo reordered'), ('spaced', spaced, 'echo spaced')):
            with self.subTest(what):
                c, _ = F.parse_cmd_exec(raw.encode(), 0, ROOT, T0 + 2)
                self.assertEqual((c['cmd'], c['status'], c['exit_code'], c['start'], c['process_id']), (want, 'failed', 3, T0 + 2 - 1.25, c['process_id']))
                self.assertIn(c['process_id'], ('12', '13'))

    def test_the_two_ways_of_reading_agree(self):
        samples = [cmd_item('echo "quoted" \\ back é中\n second line', 'c1', '5', secs=0, nanos=1), cmd_item(command=['/bin/bash', '-c', 'x\ty'], cwd='file:///a%20b'),
                   cmd_item('false', status='failed', code=127, secs=40000, nanos=999999999), cmd_item('echo', code=None)]
        for it in samples:
            fast = line(T0, 'event_msg', item_payload(it), 3)
            slow = json.dumps(json.loads(fast))                                                      # the same record with the usual spaces
            a, b = parse(fast, ts=T0 + 1), F.parse_cmd_exec(slow.encode(), 0, ROOT, T0 + 1)
            self.assertEqual(a, b)
            self.assertIsNotNone(a[0])

    def test_a_half_written_line_is_waited_for_and_a_broken_one_is_counted(self):
        whole = line(T0 + 1, 'event_msg', item_payload(cmd_item('whole', 'w1')), 4)
        self.put(ROOT, root_meta())
        with open(self.path(ROOT), 'a', encoding='utf-8') as f:
            f.write(whole[:len(whole) // 2])                    # no line end yet: the writer is in the middle of the record
        e = self.entry(ROOT)
        self.assertEqual((e['cmds_skipped'], self.idx.cmds(ROOT)), (0, ()))
        with open(self.path(ROOT), 'a', encoding='utf-8') as f:
            f.write(whole[len(whole) // 2:] + '\n')
        e = self.entry(ROOT)
        self.assertEqual(([c['item_id'] for c in self.idx.cmds(ROOT)], e['cmds_skipped']), (['w1'], 0))
        self.append(ROOT, [whole[:len(whole) // 2]])            # a line that ends there for good is broken
        e = self.entry(ROOT)
        self.assertEqual(([c['item_id'] for c in self.idx.cmds(ROOT)], e['cmds_skipped']), (['w1'], 1))

    def test_the_newest_five_thousand_are_kept(self):
        n = F.CX_CMDS_KEEP + 120
        self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(n)])
        e = self.entry(ROOT)
        cmds = self.idx.cmds(ROOT)
        self.assertEqual((len(cmds), cmds[0]['item_id'], cmds[-1]['item_id'], e['cmds_evicted']), (F.CX_CMDS_KEEP, 'c120', 'c%d' % (n - 1), 120))
        self.assertEqual([c['end'] for c in cmds], sorted(c['end'] for c in cmds))              # oldest first

    def test_a_command_text_over_the_limit_is_not_kept(self):
        e, cmds = self.run_of(cmd_item('z' * (F.CX_CMD_MAX + 1), 'c1'), cmd_item('z' * F.CX_CMD_MAX, 'c2'))
        self.assertEqual([(c['item_id'], c['cmd'] is None, c['status']) for c in cmds], [('c1', True, 'completed'), ('c2', False, 'completed')])
        self.assertEqual((e['cmds_big'], e['cmds_skipped']), (1, 0))

    def test_a_big_rollout_keeps_what_its_tail_holds_without_repeats(self):
        with mock.patch.object(codex_index, 'CX_BIG', 60000), mock.patch.object(codex_index, 'CX_TAIL', 20000):
            items = [(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(300)]
            self.put(ROOT, root_meta(), items)
            e = self.entry(ROOT)
            self.assertTrue(e['partial'])
            ids = [c['item_id'] for c in self.idx.cmds(ROOT)]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(0 < len(ids) < 300 and ids[-1] == 'c299')
            for round_ in range(3):                                                             # it grows and is read again: the same lines are not added twice
                self.append(ROOT, numbered([(T0 + 1000 + round_, 'event_msg', item_payload(cmd_item('more', 'm%d' % round_)))])[:1])
                self.refresh()
                ids = [c['item_id'] for c in self.idx.cmds(ROOT)]
                self.assertEqual(len(ids), len(set(ids)), round_)
                self.assertEqual(ids[-1], 'm%d' % round_)
            self.assertEqual(self.idx.get(ROOT)['cmds_evicted'], 0)

    def test_only_roots_and_sub_agents_have_facts(self):
        self.put(GUARD, guardian_meta(), [(T0 + 1, 'event_msg', item_payload(cmd_item('echo g'))), (T0 + 2, 'event_msg', activity('started', 'a1'))])
        self.put(ODD, {'id': ODD, 'source': {'something': {}}}, [(T0 + 1, 'event_msg', item_payload(cmd_item('echo o')))])
        self.refresh()
        self.assertEqual([(self.idx.cmds(t), self.idx.collab(t)) for t in (GUARD, ODD, 'unknown')], [((), ())] * 3)


class Collaboration(Rollouts):
    def test_the_three_activities_of_a_sub_agent(self):
        steps = [(T0 + 1, 'event_msg', activity('started', 'call-spawn', SUB1, '/root/s1')), (T0 + 5, 'event_msg', activity('completed', 'subagent-completed-turn-9', SUB1, '/root/s1')),
                 (T0 + 6, 'event_msg', activity('interrupted', 'call-stop', SUB1, '/root/s1'))]
        self.put(ROOT, root_meta(), steps)
        self.refresh()
        self.assertEqual(self.idx.collab(ROOT), tuple(
            {'thread': ROOT, 'kind': k, 'ts': T0 + dt, 'call_id': cid, 'agent_thread_id': SUB1, 'agent_path': '/root/s1', 'author': None, 'recipient': None, 'text': None,
             'msg_id': None, 'turn_id': None, 'sub_turn_id': turn, 'encrypted': False}
            for k, dt, cid, turn in (('started', 1, 'call-spawn', None), ('completed', 5, 'subagent-completed-turn-9', 'turn-9'), ('interrupted', 6, 'call-stop', None))))

    def test_odd_activities_are_dropped(self):
        steps = [(T0 + 1, 'event_msg', item_payload({'type': 'SubAgentActivity', 'id': 'x1', 'kind': 'resumed', 'agent_thread_id': SUB1})),
                 (T0 + 2, 'event_msg', item_payload({'type': 'SubAgentActivity', 'id': 'x2'})),
                 (T0 + 3, 'event_msg', item_payload({'type': 'SubAgentActivity', 'id': 7, 'kind': 'started', 'agent_thread_id': ['x'], 'agent_path': '/root/s1'})),
                 (T0 + 4, 'event_msg', item_payload({'type': 'CollabAgentToolCall', 'id': 'w1', 'tool': 'wait', 'receiver_thread_ids': [SUB1]}))]
        self.put(ROOT, root_meta(), steps)
        self.refresh()
        self.assertEqual([(c['kind'], c['call_id'], c['agent_thread_id'], c['agent_path']) for c in self.idx.collab(ROOT)], [('started', None, None, '/root/s1')])

    def test_a_message_between_agents(self):
        steps = [(T0 + 1, 'response_item', message('/root/s1', '/root', 'First part.', 'Second part.', encrypted=True)),
                 (T0 + 2, 'response_item', message('/root/s1', '/root/s2', 'Only text.')),
                 (T0 + 3, 'response_item', message('/root/s2', '/root', encrypted=True)),
                 (T0 + 4, 'response_item', {'type': 'agent_message', 'author': '/root/s2', 'recipient': '/root', 'content': 'not a list'}),
                 (T0 + 5, 'response_item', {'type': 'agent_message', 'author': 5, 'recipient': None, 'content': [{'type': 'input_text', 'text': 4}, 'x', {'type': 'text', 'text': 'no'}]})]
        self.put(ROOT, root_meta(), steps)
        self.refresh()
        got = self.idx.collab(ROOT)
        self.assertTrue(all(c['kind'] == 'message' and c['thread'] == ROOT and c['call_id'] is None and c['agent_thread_id'] is None for c in got))
        self.assertEqual([(c['ts'], c['author'], c['recipient'], c['text']) for c in got],
                         [(T0 + 1, '/root/s1', '/root', 'First part.\nSecond part.'), (T0 + 2, '/root/s1', '/root/s2', 'Only text.'), (T0 + 3, '/root/s2', '/root', None),
                          (T0 + 4, '/root/s2', '/root', None), (T0 + 5, None, None, None)])

    def test_the_text_of_a_message_is_cut_to_four_kib_of_memory(self):
        """CPython keeps 1, 2 or 4 bytes a character by the widest one: the cut is by what the text takes (as board/fingerprint.py counts), not by its characters."""
        for char, width in (('a', 1), ('\u00e9', 1), ('\u4e2d', 2), ('\U0001F600', 4)):
            text = char * 5000
            c, whole = F.parse_message(message('/root', '/root/s1', text), ROOT, T0)
            self.assertEqual(whole, text)
            self.assertEqual(c['text'], text[:len(c['text'])], char)
            self.assertLessEqual(fingerprint.nbytes(c['text']), F.CX_MSG_MAX, char)
            self.assertGreater(fingerprint.nbytes(c['text']), F.CX_MSG_MAX - 64, char)                  # as much as fits
        c, _ = F.parse_message(message('/root', '/root/s1', 'a' * 4096 + 'b'), ROOT, T0)
        self.assertEqual(c['text'], 'a' * 4096)
        c, _ = F.parse_message(message('/root', '/root/s1', 'short \u4e2d'), ROOT, T0)
        self.assertEqual(c['text'], 'short \u4e2d')                                      # under the limit: whole

    def test_a_message_line_over_the_decode_limit_is_not_read(self):
        steps = [(T0 + 1, 'response_item', message('/root/s1', '/root', 'x' * (codex_parse.CX_LINE_MAX + 10))), (T0 + 2, 'response_item', message('/root/s1', '/root', 'short'))]
        self.put(ROOT, root_meta(), steps)
        self.refresh()
        self.assertEqual([c['text'] for c in self.idx.collab(ROOT)], ['short'])

    def test_the_events_are_in_file_order_and_the_list_is_bounded(self):
        n = F.CX_COLLAB_KEEP + 5
        self.put(ROOT, root_meta(), [(T0 + i, 'response_item', message('/root/s1', '/root', 'm%d' % i)) for i in range(n)])
        self.refresh()
        got = self.idx.collab(ROOT)
        self.assertEqual((len(got), got[0]['text'], got[-1]['text']), (F.CX_COLLAB_KEEP, 'm5', 'm%d' % (n - 1)))


class Handback(Rollouts):
    """What the parent's record says about a sub-agent that finished, in the order it is written: the facts the screen's hand-back rule needs."""

    def test_the_new_fields_of_a_message(self):
        self.put(ROOT, root_meta(), [(T0 + 1, 'response_item', message('/root/s1', '/root', 'Report.', mid='amsg_77', turn='turn-parent')),
                                     (T0 + 2, 'response_item', message('/root', '/root/s1', 'note', encrypted=True, mid='amsg_78', turn='turn-child'))])
        self.refresh()
        a, b = self.idx.collab(ROOT)
        self.assertEqual((a['msg_id'], a['turn_id'], a['encrypted'], a['sub_turn_id']), ('amsg_77', 'turn-parent', False, None))
        self.assertEqual((b['msg_id'], b['turn_id'], b['encrypted'], b['text']), ('amsg_78', 'turn-child', True, 'note'))

    def test_the_turn_of_a_sub_agent_is_taken_from_the_completed_activity(self):
        self.assertEqual(F.parse_activity(line(T0, 'event_msg', activity('completed', 'subagent-completed-0192-abc')).encode(), ROOT, T0)['sub_turn_id'], '0192-abc')
        for kind, cid in (('completed', 'something-else'), ('completed', 'subagent-completed-'), ('started', 'subagent-completed-x'), ('interrupted', 'subagent-completed-x')):
            self.assertIsNone(F.parse_activity(line(T0, 'event_msg', activity(kind, cid)).encode(), ROOT, T0)['sub_turn_id'], (kind, cid))

    def test_a_message_without_the_metadata_has_no_turn(self):
        for meta in (None, 'text', [], {'turn_id': 5}, {}):
            p = message('/root/s1', '/root', 'x')
            if meta is not None:
                p['internal_chat_message_metadata_passthrough'] = meta
            self.assertIsNone(F.parse_message(p, ROOT, T0)[0]['turn_id'])

    def test_the_events_come_in_the_order_they_are_written(self):
        """Completed first, the sub-agent's message after it: what a hand-back is made of. A message before the completion is a report on the way."""
        steps = [(T0 + 1, 'event_msg', activity('started', 'call-1', SUB1, '/root/s1')),
                 (T0 + 2, 'response_item', message('/root/s1', '/root', 'A question on the way.', mid='amsg_1')),
                 (T0 + 3, 'event_msg', activity('completed', 'subagent-completed-turn-5', SUB1, '/root/s1')),
                 (T0 + 3, 'response_item', message('/root/s1', '/root', 'The final report.', mid='amsg_2', turn='turn-parent')),
                 (T0 + 4, 'event_msg', activity('started', 'call-2', SUB2, '/root/s2')),
                 (T0 + 5, 'event_msg', activity('interrupted', 'call-3', SUB1, '/root/s1'))]
        self.put(ROOT, root_meta(), steps)
        self.refresh()
        got = [(c['kind'], c['agent_path'] or c['author'], c['msg_id'] or c['call_id']) for c in self.idx.collab(ROOT)]
        self.assertEqual(got, [('started', '/root/s1', 'call-1'), ('message', '/root/s1', 'amsg_1'), ('completed', '/root/s1', 'subagent-completed-turn-5'),
                               ('message', '/root/s1', 'amsg_2'), ('started', '/root/s2', 'call-2'), ('interrupted', '/root/s1', 'call-3')])


class OffsetsAndText(Rollouts):
    def test_every_command_knows_where_its_line_is(self):
        self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(5)])
        self.refresh()
        extra = [line(T0 + 10 + i, 'event_msg', item_payload(cmd_item('more %d' % i, 'm%d' % i)), 20 + i) for i in range(3)]
        self.append(ROOT, extra)                                                           # read on from where it stopped
        self.refresh()
        cmds = self.idx.cmds(ROOT)
        self.assertEqual(len(cmds), 8)
        with open(self.path(ROOT), 'rb') as f:
            raw = f.read()
        for c in cmds:
            self.assertTrue(raw[c['offset']:].startswith(b'{"timestamp"'), c['item_id'])
            self.assertIn(b'"id":"%s"' % c['item_id'].encode(), raw[c['offset']:c['offset'] + 400], c['item_id'])

    def test_the_offsets_of_a_big_rollout_read_from_its_end(self):
        with mock.patch.object(codex_index, 'CX_BIG', 60000), mock.patch.object(codex_index, 'CX_TAIL', 20000):
            self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(300)])
            self.refresh()
            with open(self.path(ROOT), 'rb') as f:
                raw = f.read()
            cmds = self.idx.cmds(ROOT)
            self.assertTrue(cmds)
            for c in cmds:
                self.assertIn(b'"id":"%s"' % c['item_id'].encode(), raw[c['offset']:c['offset'] + 400], c['item_id'])

    def commands(self, count, size, **kw):
        self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item(('%03d' % i) + 'x' * (size - 3), 'c%d' % i))) for i in range(count)])
        return self.entry(ROOT), self.idx.cmds(ROOT)

    def test_past_the_budget_of_a_thread_the_oldest_text_goes_and_the_command_stays(self):
        with mock.patch.object(F, 'CX_TEXT_THREAD', 1000):
            e, cmds = self.commands(8, 300)
        self.assertEqual(len(cmds), 8)
        self.assertEqual([c['cmd'] is not None for c in cmds], [False] * 5 + [True] * 3)           # 3 x 300 fit in 1000
        self.assertEqual([(c['item_id'], c['status'], c['cwd']) for c in cmds[:2]], [('c0', 'completed', '/work/demo'), ('c1', 'completed', '/work/demo')])
        self.assertEqual(sum(len(c['cmd'] or '') for c in cmds), 900)
        g = [x for x in self.idx.gaps(ROOT) if x[2] == 'budget']
        self.assertEqual(g, [(cmds[0]['start'], cmds[4]['end'], 'budget')])

    def test_a_text_that_was_dropped_can_be_read_again(self):
        with mock.patch.object(F, 'CX_TEXT_THREAD', 1000):
            _, cmds = self.commands(8, 300)
        self.assertEqual(self.idx.cmd_text(ROOT, 'c0'), '000' + 'x' * 297)
        self.assertEqual(self.idx.cmd_text(ROOT, 'c0'), '000' + 'x' * 297)                    # from the cache the second time
        self.assertEqual(self.idx.cmd_text(ROOT, 'c7'), '007' + 'x' * 297)                   # still held
        self.assertIsNone(self.idx.cmd_text(ROOT, 'no-such-item'))
        self.assertIsNone(self.idx.cmd_text('no-such-thread', 'c0'))
        self.assertIsNone(cmds[0]['cmd'])                                                       # reading it again does not put it back
        self.assertFalse([g for g in self.idx.gaps(ROOT) if g[2] == 'reread'])

    def test_a_text_that_cannot_be_read_again_is_a_gap(self):
        with mock.patch.object(F, 'CX_TEXT_THREAD', 1000):
            _, cmds = self.commands(8, 300)
        v = self.idx.version
        self.write(ROOT, ['{"timestamp":"x"}'] * 3)                                             # the file is not what it was
        self.assertIsNone(self.idx.cmd_text(ROOT, 'c1'))
        self.assertGreater(self.idx.version, v)
        self.assertIn((cmds[1]['start'], cmds[1]['end'], 'reread'), self.idx.gaps(ROOT))
        self.assertIsNone(self.idx.cmd_text(ROOT, 'c1'))                                        # a text that was not dropped has nothing to read again

    def test_a_command_that_never_had_a_text_is_not_read_again(self):
        self.put(ROOT, root_meta(), [(T0 + 1, 'event_msg', item_payload(cmd_item(command=['git', 'status'], item_id='g1'))),
                                     (T0 + 2, 'event_msg', item_payload(cmd_item('z' * (F.CX_CMD_MAX + 1), 'g2')))])
        self.refresh()
        self.assertEqual((self.idx.cmd_text(ROOT, 'g1'), self.idx.cmd_text(ROOT, 'g2')), (None, None))
        self.assertEqual(sorted(g[2] for g in self.idx.gaps(ROOT)), ['argv', 'big'])           # both are gaps over their run: neither text is known

    def test_the_budget_over_all_threads_takes_the_oldest_text_first(self):
        a, b = ROOT, SUB3
        self.put(a, root_meta(a), [(T0 + 10 * i, 'event_msg', item_payload(cmd_item('a%d' % i + 'x' * 297, 'a%d' % i))) for i in range(4)])
        self.put(b, root_meta(b), [(T0 + 10 * i + 5, 'event_msg', item_payload(cmd_item('b%d' % i + 'x' * 297, 'b%d' % i))) for i in range(4)])
        with mock.patch.object(codex_index, 'CX_TEXT_TOTAL', 1000):
            self.refresh()
        cmds = sorted(self.idx.cmds(a) + self.idx.cmds(b), key=lambda c: c['end'])
        self.assertEqual([(c['item_id'], c['cmd'] is not None) for c in cmds],
                         [('a0', False), ('b0', False), ('a1', False), ('b1', False), ('a2', False), ('b2', True), ('a3', True), ('b3', True)])

    def test_a_message_text_is_dropped_before_a_newer_command_text(self):
        steps = [(T0 + 1, 'response_item', message('/root/s1', '/root', 'm' * 500)), (T0 + 2, 'event_msg', item_payload(cmd_item('c' * 500, 'c1'))),
                 (T0 + 3, 'response_item', message('/root/s1', '/root', 'n' * 500))]
        with mock.patch.object(F, 'CX_TEXT_THREAD', 1000):
            self.put(ROOT, root_meta(), steps)
            self.refresh()
        got = self.idx.collab(ROOT)
        self.assertEqual((got[0]['text'], got[1]['text'] is not None, self.idx.cmds(ROOT)[0]['cmd'] is not None), (None, True, True))
        self.assertEqual((got[0]['author'], got[0]['encrypted']), ('/root/s1', False))


class Gaps(Rollouts):
    """Where the commands of a thread may be missing, as the record shows it."""

    def lines(self, steps, **kw):
        self.put(ROOT, root_meta(**kw), steps)
        self.refresh()
        return self.idx.gaps(ROOT)

    def test_a_turn_that_is_open(self):
        g = self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'event_msg', item_payload(cmd_item('x')))])
        self.assertEqual(g, ((T0 + 1, None, 'turn'),))

    def test_no_gap_when_every_turn_has_ended_and_everything_has_been_written(self):
        self.assertEqual(self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'event_msg', item_payload(cmd_item('x'))),
                                     (T0 + 3, 'event_msg', {'type': 'task_complete'})]), ())
        self.assertEqual(self.idx.gaps('no-such-thread'), ())

    def test_a_command_that_runs_in_the_background_and_ends_in_the_turn(self):
        pty = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
               (T0 + 3, 'response_item', output('call-a', 'Script completed\nWall time 1.0 seconds\nOutput:\n', yielded(4242)))]
        self.assertEqual(self.lines(pty), ((T0 + 1, None, 'turn'),))                              # a turn is open: that covers it
        arrived = pty + [(T0 + 20, 'event_msg', item_payload(cmd_item('sleep 9', 'c1', '4242', secs=17))), (T0 + 21, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(arrived), ())                                                  # its record came: nothing is missing now

    def test_the_commands_that_run_now_in_an_open_turn_are_told_apart_from_the_turn_being_open(self):
        """`gaps` says the open turn may hold a command that is missing; `running` says which ones are known to run: an exec call with no output yet, a cell that answered "running"."""
        g = self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'event_msg', item_payload(cmd_item('x')))])
        self.assertEqual((g, self.idx.running(ROOT)), (((T0 + 1, None, 'turn'),), ()))                       # nothing known to run
        g = self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 5, 'response_item', exec_call('call-a'))])
        self.assertEqual((g, self.idx.running(ROOT)), (((T0 + 1, None, 'turn'),), (T0 + 5,)))                # the call has no output yet
        g = self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 5, 'response_item', exec_call('call-a')),
                        (T0 + 6, 'response_item', output('call-a', 'Script completed', 'ok'))])
        self.assertEqual(self.idx.running(ROOT), ())                                                         # its output came
        g = self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 5, 'response_item', exec_call('call-b')),
                        (T0 + 6, 'response_item', output('call-b', 'Script running with cell ID 7'))])
        self.assertEqual(self.idx.running(ROOT), (T0 + 5,))                                                  # a cell that runs on
        g = self.lines([(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 5, 'response_item', exec_call('call-c')), (T0 + 9, 'event_msg', {'type': 'task_complete'})])
        self.assertEqual(self.idx.running(ROOT), ())                                                         # no turn is open: the gaps say what is left
        self.assertEqual(self.idx.running('no-such-thread'), ())

    def test_a_command_whose_record_never_came(self):
        pty = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
               (T0 + 3, 'response_item', output('call-a', 'Script completed\nWall time 1.0 seconds\nOutput:\n', yielded(4242))),
               (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(pty), ((T0 + 2, T0 + 9, 'open'),))                            # from the cell that started it to the end of the turn

    def test_the_record_of_another_process_does_not_settle_it(self):
        steps = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
                 (T0 + 3, 'response_item', output('call-a', yielded(4242))), (T0 + 4, 'event_msg', item_payload(cmd_item('other', 'c1', '777'))),
                 (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(steps), ((T0 + 2, T0 + 9, 'open'),))

    def test_a_record_that_comes_before_the_output_that_told_it_was_running(self):
        steps = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
                 (T0 + 3, 'event_msg', item_payload(cmd_item('quick', 'c1', '4242'))), (T0 + 4, 'response_item', output('call-a', yielded(4242))),
                 (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(steps), ())

    def test_a_cell_that_runs_on_until_a_wait_sees_it_end(self):
        head = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
                (T0 + 3, 'response_item', output('call-a', 'Script running with cell ID 16\nWall time 1.0 seconds\nOutput:\n')),
                (T0 + 5, 'response_item', wait_call('call-w', 16))]
        ended = head + [(T0 + 7, 'response_item', output('call-w', 'Script completed\nWall time 1.0 seconds\nOutput:\n', kind='function_call_output')),
                        (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(ended), ())
        running = head + [(T0 + 7, 'response_item', output('call-w', 'Script running with cell ID 16\nWall time 1.0 seconds\nOutput:\n', kind='function_call_output')),
                          (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.idx = codex_index.CodexIndex()
        self.assertEqual(self.lines(running), ((T0 + 2, T0 + 9, 'open'),))                         # the start is the cell's, not the wait's

    def test_a_wait_that_ends_the_cell_and_leaves_a_process_running(self):
        steps = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
                 (T0 + 3, 'response_item', output('call-a', 'Script running with cell ID 3\nWall time 1.0 seconds\nOutput:\n')),
                 (T0 + 5, 'response_item', wait_call('call-w', 3)),
                 (T0 + 7, 'response_item', output('call-w', 'Script completed\nWall time 4.0 seconds\nOutput:\n', yielded(555), kind='function_call_output')),
                 (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(steps), ((T0 + 2, T0 + 9, 'open'),))

    def test_a_session_id_that_a_command_printed_is_not_a_running_command(self):
        printed = json.dumps({'chunk_id': 'z1', 'wall_time_seconds': 0.1, 'exit_code': 0, 'output': json.dumps({'session_id': 42, 'wall_time_seconds': 1})}, separators=(',', ':'))
        steps = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
                 (T0 + 3, 'response_item', output('call-a', 'Script completed\nWall time 1.0 seconds\nOutput:\n', printed, '{"session_id": 43}')),
                 (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(steps), ())

    def test_an_output_of_a_failed_cell_or_without_a_running_command_opens_nothing(self):
        steps = [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'response_item', exec_call('call-a')),
                 (T0 + 3, 'response_item', output('call-a', 'Script failed\nWall time 0.0 seconds\nOutput:\n', 'Script error:\nboom')),
                 (T0 + 4, 'response_item', exec_call('call-b')),
                 (T0 + 5, 'response_item', output('call-b', 'Script completed\nWall time 1.0 seconds\nOutput:\n', json.dumps({'chunk_id': 'z', 'exit_code': 0, 'output': 'ok'}))),
                 (T0 + 6, 'response_item', {'type': 'function_call_output', 'id': 'o', 'call_id': 'call-unknown', 'output': 'plain'}),
                 (T0 + 9, 'event_msg', {'type': 'task_complete'})]
        self.assertEqual(self.lines(steps), ())

    def test_a_thread_without_a_turn_marker_shows_its_open_runs(self):
        steps = [(T0 + 2, 'response_item', exec_call('call-a')), (T0 + 3, 'response_item', output('call-a', yielded(9)))]
        self.assertEqual(self.lines(steps), ((T0 + 2, None, 'open'),))

    def test_a_command_text_over_the_limit_is_a_gap_over_its_run(self):
        g = self.lines([(T0 + 5, 'event_msg', item_payload(cmd_item('z' * (F.CX_CMD_MAX + 1), 'c1', secs=3)))])
        self.assertEqual(g, ((T0 + 2, T0 + 5, 'big'),))

    def test_a_line_that_cannot_be_read_is_a_gap_over_its_run_or_from_the_start_of_the_thread(self):
        huge = 'x' * (codex_parse.CX_LINE_MAX + 100)
        item = cmd_item('make', out=huge, secs=2)
        item = {'type': item.pop('type'), 'command': item.pop('command'), **item}
        self.assertEqual(self.lines([(T0 + 5, 'event_msg', item_payload(item))]), ((T0 + 3, T0 + 5, 'unreadable'),))         # the duration is behind the output
        del item['duration']
        self.idx = codex_index.CodexIndex()
        self.assertEqual(self.lines([(T0 + 5, 'event_msg', item_payload(item))]), ((T0, T0 + 5, 'unreadable'),))            # it ran sometime since the thread began

    def test_a_big_rollout_does_not_know_what_came_before_its_tail(self):
        with mock.patch.object(codex_index, 'CX_BIG', 60000), mock.patch.object(codex_index, 'CX_TAIL', 20000):
            g = self.lines([(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(300)])
        self.assertEqual(len(g), 1)
        self.assertEqual((g[0][0], g[0][2]), (-float('inf'), 'partial'))
        self.assertTrue(T0 < g[0][1] < T0 + 300)                                                    # up to the first line that was read

    def test_commands_pushed_out_by_the_count_are_a_gap(self):
        n = F.CX_CMDS_KEEP + 20
        g = self.lines([(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(n)])
        self.assertEqual(g, ((T0 + 0, T0 + 19, 'evicted'),))

    def test_what_a_sub_agent_copied_is_not_its_gap_and_a_guardian_has_none(self):
        fork = [(T0 + 1, 'session_meta', root_meta()), (T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 1, 'response_item', exec_call('p')), (T0 + 1, 'response_item', output('p', yielded(5)))]
        own = [(T0 + 2, 'event_msg', {'type': 'task_started'}), (T0 + 3, 'event_msg', {'type': 'task_complete'})]
        self.put(SUB1, sub_meta(SUB1, start=len(fork)), fork + own)
        self.put(GUARD, guardian_meta(), [(T0 + 1, 'event_msg', {'type': 'task_started'})])
        self.refresh()
        self.assertEqual((self.idx.gaps(SUB1), self.idx.gaps(GUARD)), ((), ()))                     # the copied turn and its run are the parent's

    def test_the_gaps_are_sorted_tuples_of_three(self):
        self.put(ROOT, root_meta(), [(T0 + 9, 'event_msg', item_payload(cmd_item('z' * (F.CX_CMD_MAX + 1), 'c1', secs=1))), (T0 + 1, 'event_msg', {'type': 'task_started'})])
        g = self.refresh().gaps(ROOT)
        self.assertIsInstance(g, tuple)
        self.assertTrue(all(isinstance(x, tuple) and len(x) == 3 for x in g))
        self.assertEqual(list(g), sorted(g, key=lambda x: x[0]))
        self.assertEqual([x[2] for x in g], ['turn', 'big'] if g[0][0] < g[1][0] else ['big', 'turn'])

    def test_too_many_settled_gaps_are_folded_into_one(self):
        f = F.ThreadFacts()
        for i in range(F.CX_GAPS_MAX + 40):
            f.add_gap(float(i), float(i) + 0.5, 'big')
        self.assertLessEqual(len(f.gaps), F.CX_GAPS_MAX)
        self.assertEqual((f.gaps[0][0], max(g[1] for g in f.gaps), f.gaps[0][2]), (0.0, F.CX_GAPS_MAX + 39.5, 'many'))


class DifferentKeyOrder(Rollouts):
    """A record Codex writes with its keys in another order than usual is still read; one that is recognised and cannot be read is a gap and a drift, never nothing."""

    def front(self, item, first=('id', 'type')):
        """The item with the keys in `first` put first."""
        return {**{k: item[k] for k in first}, **{k: v for k, v in item.items() if k not in first}}

    def read(self, *items, raw=()):
        self.idx = codex_index.CodexIndex()
        self.put(ROOT, root_meta(), [(T0 + 1 + i, 'event_msg', it if isinstance(it, dict) and it.get('type') == 'item_completed' else item_payload(it)) for i, it in enumerate(items)])
        if raw:
            self.append(ROOT, list(raw))
        e = self.entry(ROOT)
        return e, self.idx.cmds(ROOT), self.idx.gaps(ROOT)

    def test_an_item_with_its_id_before_its_type_is_read(self):
        e, cmds, gaps = self.read(self.front(cmd_item('ls -la', 'c1', '7', secs=2, code=3, status='failed')))
        self.assertEqual([(c['item_id'], c['cmd'], c['cwd'], c['status'], c['exit_code'], c['process_id'], c['start']) for c in cmds],
                         [('c1', 'ls -la', '/work/demo', 'failed', 3, '7', T0 + 1 - 2)])
        self.assertEqual((gaps, e['cmds_skipped'], e['cmds_drift'], e['drift']), ((), 0, 0, False))

    def test_other_orders_of_the_item_the_payload_and_the_line(self):
        item = cmd_item('echo reordered', 'c2')
        payload_first_thread = {'thread_id': ROOT, 'turn_id': 't', 'type': 'item_completed', 'item': item}
        line_type_first = json.dumps({'type': 'event_msg', 'timestamp': iso(T0 + 3), 'payload': item_payload(item)}, separators=(',', ':'))
        e, cmds, gaps = self.read(self.front(cmd_item('one', 'c1'), first=('command', 'id', 'type')), payload_first_thread, raw=[line_type_first])
        self.assertEqual([(c['item_id'], c['cmd']) for c in cmds], [('c1', 'one'), ('c2', 'echo reordered'), ('c2', 'echo reordered')])
        self.assertEqual((gaps, e['cmds_skipped'], e['cmds_drift']), ((), 0, 0))
        self.assertEqual(cmds[2]['end'], T0 + 3)                                                      # the time comes from the line, wherever the key is

    def test_an_activity_with_its_id_before_its_type_is_read(self):
        item = self.front({'type': 'SubAgentActivity', 'id': 'call-1', 'kind': 'started', 'agent_thread_id': SUB1, 'agent_path': '/root/s1'})
        self.put(ROOT, root_meta(), [(T0 + 1, 'event_msg', item_payload(item))])
        self.refresh()
        self.assertEqual([(c['kind'], c['call_id'], c['agent_thread_id']) for c in self.idx.collab(ROOT)], [('started', 'call-1', SUB1)])

    def test_a_recognised_command_that_cannot_be_read_is_a_gap_and_a_drift(self):
        odd_status = self.front(cmd_item('x', 'c1', status='weird', secs=3))
        no_id = self.front(cmd_item('x', 'c2', secs=1))
        del no_id['id']
        e, cmds, gaps = self.read(odd_status, no_id)
        self.assertEqual((cmds, e['cmds_skipped'], e['cmds_drift'], e['drift']), ((), 2, 2, True))
        self.assertEqual(gaps, ((T0 + 1 - 3, T0 + 1, 'unreadable'), (T0 + 2 - 1, T0 + 2, 'unreadable')))     # the run, as far as the line says when it was
        self.assertEqual(e['kind'], 'root')                                                                # the thread is still a root: only what it said was not understood

    def test_a_run_whose_time_is_not_in_the_line_is_a_gap_from_the_start_of_the_thread(self):
        no_duration = self.front(cmd_item('x', 'c1', status='weird'))
        del no_duration['duration']
        e, cmds, gaps = self.read(no_duration)
        self.assertEqual((cmds, e['cmds_drift'], gaps), ((), 1, ((T0, T0 + 1, 'unreadable'),)))

    def test_a_line_that_only_mentions_the_name_is_not_a_command(self):
        other = [{'type': 'AgentMessage', 'id': 'm1', 'content': [{'type': 'output_text', 'text': 'the "CommandExecution" item and "SubAgentActivity" are written'}]},
                 {'type': 'Extension', 'kind': 'CommandExecution', 'id': 'x1'}, {'type': 'FileChange', 'id': 'f1', 'changes': {'/a/CommandExecution': {}}, 'status': 'completed'}]
        e, cmds, gaps = self.read(*other)
        self.assertEqual((cmds, gaps, e['cmds_skipped'], e['cmds_drift'], e['drift']), ((), (), 0, 0, False))
        self.assertEqual(self.idx.collab(ROOT), ())

    def test_a_command_that_is_not_a_shell_command_is_a_gap_over_its_run(self):
        e, cmds, gaps = self.read(cmd_item(command=['git', 'status'], item_id='g1', secs=4))
        self.assertEqual(([(c['item_id'], c['cmd']) for c in cmds], gaps), ([('g1', None)], ((T0 + 1 - 4, T0 + 1, 'argv'),)))
        self.assertEqual((e['cmds_skipped'], e['cmds_drift']), (0, 0))                                      # a shape that is understood: not a drift

    def test_the_counts_start_over_with_a_big_rollout_read_again(self):
        with mock.patch.object(codex_index, 'CX_BIG', 60000), mock.patch.object(codex_index, 'CX_TAIL', 20000):
            odd = [(T0 + i, 'event_msg', item_payload(self.front(cmd_item('echo %d' % i, 'c%d' % i, status='weird')))) for i in range(300)]
            self.put(ROOT, root_meta(), odd)
            first = self.entry(ROOT)['cmds_drift']
            self.append(ROOT, [line(T0 + 999, 'event_msg', item_payload(self.front(cmd_item('more', 'm', status='weird'))), 400)])
            self.assertEqual(self.entry(ROOT)['cmds_drift'], first)       # the tail holds as many lines as before: the same count, not a running total
            self.assertTrue(0 < first < 300)


class Rewritten(Rollouts):
    """A rollout that gets smaller or is written again starts its facts over, and says so with a new generation."""

    def put_cmds(self, texts, tid=ROOT, **meta):
        self.put(tid, root_meta(tid, **meta), [(T0 + i, 'event_msg', item_payload(cmd_item(t, 'c%d' % i))) for i, t in enumerate(texts)])

    def test_every_entry_has_a_generation(self):
        self.put_cmds(['a'])
        e = self.entry(ROOT)
        self.assertIsInstance(e['gen'], int)
        gen = e['gen']
        self.append(ROOT, [line(T0 + 9, 'event_msg', item_payload(cmd_item('b', 'c9')), 9)])
        self.assertEqual(self.entry(ROOT)['gen'], gen)                       # growing is not a new generation

    def test_a_smaller_file_starts_over(self):
        self.put_cmds(['one', 'two', 'three'])
        before = self.entry(ROOT)
        self.assertEqual(len(self.idx.cmds(ROOT)), 3)
        self.put(ROOT, root_meta(), [(T0 + 1, 'event_msg', {'type': 'task_started'}), (T0 + 2, 'event_msg', {'type': 'task_complete'})])
        now = self.entry(ROOT)
        self.assertEqual((now['gen'], self.idx.cmds(ROOT), self.idx.gaps(ROOT)), (before['gen'] + 1, (), ()))

    def test_a_file_written_again_with_another_first_line_starts_over(self):
        self.put_cmds(['one'], cwd='/work/a')
        gen = self.entry(ROOT)['gen']
        self.put_cmds(['one', 'two', 'three', 'four'], cwd='/work/b')              # bigger, and the first line is not the same
        e = self.entry(ROOT)
        self.assertEqual((e['cwd'], e['gen'], len(self.idx.cmds(ROOT))), ('/work/b', gen + 1, 4))

    def test_a_path_that_holds_another_thread_now_has_no_trace_of_the_first(self):
        self.put_cmds(['one'])
        self.assertEqual(len(self.idx.cmds(ROOT)) if self.entry(ROOT) else 0, 1)
        lines = numbered([(T0, 'session_meta', root_meta(SUB3))] + [(T0 + i, 'event_msg', item_payload(cmd_item('other %d' % i, 'x%d' % i, ))) for i in range(1, 5)])
        self.write(ROOT, lines)                                                      # the same path, another thread's record, bigger
        idx = self.refresh()
        self.assertEqual(([e['id'] for e in idx.entries()], idx.get(ROOT)), ([SUB3], None))
        self.assertEqual((len(idx.cmds(SUB3)), idx.cmds(ROOT)), (4, ()))

    def test_a_rewrite_of_what_was_already_read_starts_over_even_when_the_file_is_bigger(self):
        self.put_cmds(['one', 'two', 'three'])
        gen = self.entry(ROOT)['gen']
        self.put_cmds(['ONE', 'TWO', 'THREE', 'four'])          # the same first line, the lines already read are other lines, and there are more of them
        e = self.entry(ROOT)
        self.assertEqual((e['gen'], [c['cmd'] for c in self.idx.cmds(ROOT)]), (gen + 1, ['ONE', 'TWO', 'THREE', 'four']))

    def test_the_generation_goes_on_when_a_file_goes_and_comes_back(self):
        self.put_cmds(['one'])
        gen = self.entry(ROOT)['gen']
        os.remove(self.path(ROOT))
        self.assertIsNone(self.refresh().get(ROOT))
        self.put_cmds(['one'])
        self.assertGreater(self.entry(ROOT)['gen'], gen)

    def test_a_text_read_again_is_not_taken_from_an_earlier_generation(self):
        with mock.patch.object(F, 'CX_TEXT_THREAD', 1000):
            self.put_cmds([('%03d' % i) + 'a' * 297 for i in range(8)])
            self.refresh()
            self.assertEqual(self.idx.cmd_text(ROOT, 'c0'), '000' + 'a' * 297)           # now in the cache
            self.put_cmds([('%03d' % i) + 'b' * 297 for i in range(9)])                  # the same item ids, other texts, one more line
            self.refresh()
            self.assertIsNone(self.idx.cmd_text(ROOT, 'c8') and None)
            self.assertEqual(self.idx.cmd_text(ROOT, 'c0'), '000' + 'b' * 297)
            self.assertEqual([k[1] for k in self.idx._texts], [self.idx.get(ROOT)['gen']])


class PartialSubAgent(Rollouts):
    """A big sub-agent rollout is read from its end: the copied lines that are in that end are still the parent's."""

    START = 40

    def build(self, ordinals=True):
        meta = sub_meta(SUB1, start=self.START)
        fork = [(T0 + 1, 'event_msg', item_payload(cmd_item('copied-%d' % i, 'p%d' % i))) for i in range(self.START)]
        own = [(T0 + 5 + i, 'event_msg', item_payload(cmd_item('own-%d' % i, 'o%d' % i), SUB1)) for i in range(3)]
        self.put(SUB1, meta, fork + own, ordinals=ordinals)

    def test_the_copied_lines_in_the_tail_are_not_its_own(self):
        with mock.patch.object(codex_index, 'CX_BIG', 8000), mock.patch.object(codex_index, 'CX_TAIL', 7000):
            self.build()
            e = self.entry(SUB1)
            self.assertTrue(e['partial'])
            ids = [c['item_id'] for c in self.idx.cmds(SUB1)]
        self.assertEqual(ids, ['o0', 'o1', 'o2'])

    def test_lines_without_an_ordinal_are_not_known_to_be_its_own(self):
        with mock.patch.object(codex_index, 'CX_BIG', 8000), mock.patch.object(codex_index, 'CX_TAIL', 7000):
            self.build(ordinals=False)
            self.assertTrue(self.entry(SUB1)['partial'])
            self.assertEqual(self.idx.cmds(SUB1), ())

    def test_a_big_root_is_read_as_before(self):
        with mock.patch.object(codex_index, 'CX_BIG', 8000), mock.patch.object(codex_index, 'CX_TAIL', 7000):
            self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item('r%d' % i, 'r%d' % i))) for i in range(60)], ordinals=False)
            self.assertTrue(self.entry(ROOT)['partial'])
            self.assertTrue(self.idx.cmds(ROOT))


class BudgetInMemoryBytes(Rollouts):
    """The budgets count what a text takes in memory, as board/fingerprint.py does, not its characters."""

    def held(self, f):
        return sum(sys.getsizeof(c['cmd']) - sys.getsizeof('') for c in f.texted) + sum(sys.getsizeof(c['text']) - sys.getsizeof('') for c in f.ctexted)

    def cmd(self, i, text):
        return {'thread': ROOT, 'item_id': 'c%d' % i, 'process_id': '1', 'cmd': text, 'cwd': '/w', 'start': float(i), 'end': float(i) + 0.5, 'status': 'completed', 'exit_code': 0, 'offset': 0}

    def test_the_sum_is_what_the_texts_hold(self):
        f = F.ThreadFacts()
        emoji = '\U0001F600'
        with mock.patch.object(F, 'CX_TEXT_THREAD', 200000):
            for i in range(40):
                f.add_cmd(self.cmd(i, chr(0x1F600 + i % 20) * 10000 + str(i)))               # 40 KB of memory each
        self.assertEqual(f.nbytes, self.held(f))
        self.assertLessEqual(f.nbytes, 200000)
        self.assertTrue(0 < len(f.texted) < 40)
        self.assertEqual(emoji, '\U0001F600')

    def test_what_is_dropped_is_counted_as_it_was_added(self):
        f = F.ThreadFacts()
        with mock.patch.object(F, 'CX_TEXT_THREAD', 1 << 30):
            for i in range(10):
                f.add_cmd(self.cmd(i, ('é中' if i % 2 else '\U0001F600') * (3000 + i)))     # 1, 2 and 4 bytes a character
            before = f.nbytes
            freed = f.strip_oldest()
        self.assertEqual(before - freed, f.nbytes)
        self.assertEqual(f.nbytes, self.held(f))
        f.trim(0)
        self.assertEqual((f.nbytes, self.held(f)), (0, 0))

    def test_a_command_text_over_the_limit_is_by_its_memory(self):
        wide = '\U0001F600' * (F.CX_CMD_MAX // 4 + 1)                                            # fewer characters than the limit, more bytes
        self.assertLess(len(wide), F.CX_CMD_MAX)
        self.put(ROOT, root_meta(), [(T0 + 1, 'event_msg', item_payload(cmd_item(wide, 'c1'))), (T0 + 2, 'event_msg', item_payload(cmd_item('\U0001F600' * (F.CX_CMD_MAX // 4 - 16), 'c2')))])
        e = self.entry(ROOT)
        self.assertEqual([(c['item_id'], c['cmd'] is None) for c in self.idx.cmds(ROOT)], [('c1', True), ('c2', False)])
        self.assertEqual(e['cmds_big'], 1)

    def test_a_message_is_cut_by_its_memory(self):
        wide = '\U0001F600' * 5000
        c, whole = F.parse_message(message('/root', '/root/s1', wide), ROOT, T0)
        self.assertEqual(whole, wide)
        self.assertLessEqual(sys.getsizeof(c['text']) - sys.getsizeof(''), F.CX_MSG_MAX)
        self.assertEqual(c['text'], wide[:len(c['text'])])
        self.assertGreater(len(c['text']), 1000)
        ascii_text, _ = F.parse_message(message('/root', '/root/s1', 'a' * 5000), ROOT, T0)
        self.assertEqual(len(ascii_text['text']), F.CX_MSG_MAX)

    def test_the_text_read_again_is_kept_by_its_memory(self):
        wide = '\U0001F600' * 2000                                                              # 8 KB in memory
        with mock.patch.object(F, 'CX_TEXT_THREAD', 20000), mock.patch.object(codex_index, 'CX_TEXT_CACHE_BYTES', 20000):
            self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item(wide + str(i), 'c%d' % i))) for i in range(8)])
            self.refresh()
            for i in range(4):
                self.assertEqual(self.idx.cmd_text(ROOT, 'c%d' % i), wide + str(i))
            self.assertLessEqual(self.idx._texts_size, 20000)
            self.assertEqual(self.idx._texts_size, sum(sys.getsizeof(t) - sys.getsizeof('') for t in self.idx._texts.values()))
            self.assertLessEqual(len(self.idx._texts), 2)

    def test_the_budget_over_all_threads_counts_memory_too(self):
        a, b = ROOT, SUB3
        wide = '\U0001F600' * 2000
        for tid in (a, b):
            self.put(tid, root_meta(tid), [(T0 + 10 * i + (5 if tid == b else 0), 'event_msg', item_payload(cmd_item(wide + str(i), '%s%d' % (tid[-1], i)))) for i in range(4)])
        with mock.patch.object(codex_index, 'CX_TEXT_TOTAL', 30000):
            self.refresh()
        held = sum(self.held(e['_facts']) for e in self.idx.files.values())
        self.assertLessEqual(held, 30000)
        self.assertEqual(held, sum(e['_facts'].nbytes for e in self.idx.files.values()))


class Readers(Rollouts):
    def test_a_snapshot_does_not_carry_the_scan_state_and_the_lists_are_a_copy(self):
        self.put(ROOT, root_meta(), [(T0 + 1, 'event_msg', item_payload(cmd_item('one', 'c1')))])
        e = self.entry(ROOT)
        for name in ('_facts', '_fork', '_nline', '_turn_text'):
            self.assertNotIn(name, e)
        before = self.idx.cmds(ROOT)
        self.assertIsInstance(before, tuple)
        self.append(ROOT, [line(T0 + 2, 'event_msg', item_payload(cmd_item('two', 'c2')), 2)])
        self.refresh()
        self.assertEqual((len(before), len(self.idx.cmds(ROOT))), (1, 2))
        self.assertEqual(len(self.idx.entries()), 1)
        self.assertEqual(self.idx.cmds('no-such-thread'), ())

    def test_the_version_rises_when_a_fact_arrives(self):
        self.put(ROOT, root_meta())
        v = self.refresh().version
        self.refresh()
        self.assertEqual(self.idx.version, v)
        self.append(ROOT, [line(T0 + 1, 'event_msg', item_payload(cmd_item('x')), 1)])
        self.refresh()
        self.assertGreater(self.idx.version, v)

    def test_a_rollout_that_is_rewritten_smaller_starts_its_facts_over(self):
        self.put(ROOT, root_meta(), [(T0 + i, 'event_msg', item_payload(cmd_item('echo %d' % i, 'c%d' % i))) for i in range(10)])
        self.assertEqual(len(self.entry(ROOT) and self.idx.cmds(ROOT)), 10)
        self.put(ROOT, root_meta(), [(T0, 'event_msg', item_payload(cmd_item('echo new', 'n1')))])
        self.refresh()
        self.assertEqual([c['item_id'] for c in self.idx.cmds(ROOT)], ['n1'])

    def test_the_line_decoder_gives_the_ordinal(self):
        self.assertEqual(codex_parse.cx_decode(line(T0, 'event_msg', {'type': 'task_started'}, 12).encode())['ord'], 12)
        self.assertIsNone(codex_parse.cx_decode(line(T0, 'event_msg', {'type': 'task_started'}).encode())['ord'])
        big = line(T0, 'response_item', {'type': 'message', 'content': 'a' * (codex_parse.CX_LINE_MAX + 10)}, 4).encode()
        self.assertEqual(codex_parse.cx_decode(big)['ord'], 4)
        self.assertIsNone(codex_parse.cx_decode(b'{"timestamp":"' + iso(T0).encode() + b'","ordinal":"x","type":"event_msg","payload":{"type":"a"}}')['ord'])


class ToolDescription(unittest.TestCase):
    """What a Codex tool call shows as its description: a literal the call gives, else nothing; never a line of the JS."""

    def test_a_cell_with_only_write_stdin_shows_the_tool_and_the_session(self):
        js = 'text(await tools.write_stdin({session_id: 4321, chars: "", yield_time_ms: 1000, max_output_tokens: 4000}))'
        self.assertEqual(codex_parse.codex_tool(js), ('write_stdin', 'session 4321'))
        self.assertEqual(codex_parse.codex_tool('const r = await tools.write_stdin({session_id: "77", chars: "secret\\n"});')[1], 'session 77')

    def test_what_is_typed_into_a_process_is_not_shown(self):
        name, text = codex_parse.codex_tool('await tools.write_stdin({session_id: 5, chars: "rm -rf build\\n"})')
        self.assertNotIn('rm', text)
        self.assertEqual((name, text), ('write_stdin', 'session 5'))

    def test_a_cell_with_no_literal_shows_the_name_only(self):
        for js, name in (('const sid = pick();\ntext(await tools.write_stdin({session_id: sid, chars: ""}))', 'write_stdin'),
                         ('const c = build();\nawait tools.exec_command({cmd: c, workdir: w})', 'exec_command'),
                         ('await tools.view_image({path: p})', 'view_image'), ('await tools.web__run({q: query})', 'web__run'),
                         ('const secret = "x";\nconsole.log(secret)', 'exec'), ('', 'exec'), (None, 'exec')):
            with self.subTest(js):
                self.assertEqual(codex_parse.codex_tool(js), (name, ''))

    def test_the_literals_are_still_shown(self):
        self.assertEqual(codex_parse.codex_tool('await tools.exec_command({cmd: "ls -la"})'), ('exec_command', 'ls -la'))
        js = 'await tools.write_stdin({session_id: 9, chars: ""});\nawait tools.exec_command({cmd: `git status`})'
        self.assertEqual(codex_parse.codex_tool(js), ('exec_command', 'git status'))          # the command of a cell with both
        self.assertEqual(codex_parse.codex_call('custom_tool_call', {'input': 'tools.write_stdin({session_id: 3})'}), ('write_stdin', 'session 3'))


if __name__ == '__main__':
    unittest.main()
