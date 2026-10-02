"""Server-side test of the agent talk card: `/api/talk?scope=` and the talk events of `claude -p` child sessions.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
import test_stage1 as st1  # noqa: E402
import test_stage2 as st2  # noqa: E402

T0 = st2.T0
USER_KINDS = ('user_say', 'orch_say', 'orch_ask', 'user_answer')
AGENT_KINDS = ('spawn', 'orch_msg', 'handback', 'peer', 'agent_msg', 'xread')
KIND_SEQ = ['user_say', 'spawn', 'orch_say', 'orch_msg', 'notify', 'handback', 'peer', 'stop', 'agent_msg', 'xread', 'orch_ask',
            'user_answer', 'agent_msg']


class TalkSession:
    """A session with only what the Handler calls: a feed that mixes user talk and agent talk, and one agent."""
    S = '&session=11111111-2222-4333-8444-555555555555'

    def __init__(self):
        self.version = 1
        self.lock = threading.RLock()
        self.feed = [{'kind': k, 'ts': float(i), 'text': 't%d' % i, 'from': 'x', 'to': 'y', 'agent': None} for i, k in enumerate(KIND_SEQ)]
        self.feed[1].update({'from': 'orch', 'to': 'toolu_1', 'tool_use_id': 'toolu_1', 'model': 'opus'})     # spawn launched by the Agent tool: the recipient is still the tool_use_id
        self.feed[9].update({'author': 'a1', 'path': '/d/r1/A.md', 'text': ''})
        self.agents = {'a1': types.SimpleNamespace(id='a1', tool_use_id='toolu_1', provider='claude'),
                       'cx': types.SimpleNamespace(id='cx', tool_use_id=None, provider='codex')}

    def state(self):
        return {'state': 1}


def talk(query='', s=None):
    code, body = st1.call('/api/talk?%s%s' % (query, TalkSession.S), s or TalkSession())
    return code, body


class Scope(unittest.TestCase):
    def test_default_is_user_talk(self):
        want = [i for i, k in enumerate(KIND_SEQ) if k in USER_KINDS]
        for q in ('', 'scope=user', 'scope='):                       # an empty value is the same as none
            code, body = talk(q)
            self.assertEqual((code, [e['idx'] for e in body['items']], body['more']), (200, want, False), q)
        self.assertEqual(want, [0, 2, 10, 11])

    def test_default_items_are_unchanged(self):
        """The items of the default response are the feed events with only idx and full_len added (same as before scope was added)."""
        _, body = talk()
        for e in body['items']:
            src = TalkSession().feed[e['idx']]
            self.assertEqual(e, dict(src, idx=e['idx'], full_len=len(src['text'])))

    def test_agents_scope_kinds(self):
        code, body = talk('scope=agents')
        want = [i for i, k in enumerate(KIND_SEQ) if k in AGENT_KINDS]
        self.assertEqual((code, [e['idx'] for e in body['items']], body['more']), (200, want, False))
        self.assertEqual({e['kind'] for e in body['items']}, set(AGENT_KINDS))         # no notify, stop or user talk
        self.assertEqual([e['kind'] for e in body['items']], [KIND_SEQ[i] for i in want])

    def test_agents_scope_keeps_extra_fields(self):
        _, body = talk('scope=agents')
        xr = next(e for e in body['items'] if e['kind'] == 'xread')
        self.assertEqual((xr['author'], xr['path'], xr['text'], xr['full_len']), ('a1', '/d/r1/A.md', '', 0))

    def test_spawn_target_resolved_like_state(self):
        _, body = talk('scope=agents')
        sp = next(e for e in body['items'] if e['kind'] == 'spawn')
        self.assertEqual((sp['agent'], sp['to'], sp['tool_use_id'], sp['model']), ('a1', 'a1', 'toolu_1', 'opus'))
        s = TalkSession()
        s.agents = {}                                                # if the agent is not found yet, tool_use_id stays as it is (same as state())
        _, body = talk('scope=agents', s)
        sp = next(e for e in body['items'] if e['kind'] == 'spawn')
        self.assertEqual((sp['agent'], sp['to']), (None, 'toolu_1'))
        self.assertEqual(s.feed[1]['to'], 'toolu_1')                 # the feed itself is not modified

    def test_paging_and_truncation_same_as_user_scope(self):
        s = TalkSession()
        s.feed[3]['text'] = 'x' * 2500
        _, body = talk('scope=agents&limit=3', s)
        self.assertEqual(([e['idx'] for e in body['items']], body['more']), ([8, 9, 12], True))
        _, body = talk('scope=agents&limit=3&before=8', s)
        self.assertEqual(([e['idx'] for e in body['items']], body['more']), ([3, 5, 6], True))
        big = next(e for e in body['items'] if e['idx'] == 3)
        self.assertEqual((big['full_len'], len(big['text'])), (2500, len(server.trunc('x' * 2500, 2000))))
        _, body = talk('scope=agents&limit=300&before=3', s)
        self.assertEqual(([e['idx'] for e in body['items']], body['more']), ([1], False))

    def test_bad_scope_400(self):
        for q in ('scope=x', 'scope=Agents', 'scope=agent', 'scope=all', 'scope=agents,user', 'scope=%20', 'scope=0'):
            self.assertEqual(talk(q), (400, {'error': 'bad parameter', 'error_code': 'bad_parameter'}), q)
        self.assertEqual(talk('scope=agents&limit=0')[0], 400)       # the other argument checks are unchanged
        self.assertEqual(talk('scope=agents&limit=301')[0], 400)

    def test_scope_only_for_talk(self):
        code, _ = st1.call('/api/state?scope=zzz' + TalkSession.S, TalkSession())
        self.assertEqual(code, 200)


# ---------- claude -p child sessions ----------

def user_line(t, text, cwd='/w', uuid=None, **extra):
    d = {'type': 'user', 'timestamp': st2.iso(t), 'cwd': cwd, 'message': {'role': 'user', 'content': text}}
    if uuid:
        d['uuid'] = uuid
    d.update(extra)
    return st2.dump(d)


def result_line(t, cwd='/w'):
    return st2.dump({'type': 'user', 'timestamp': st2.iso(t), 'cwd': cwd, 'toolUseResult': {'stdout': 'x'},
                     'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'ok'}]}})


def asst_line(t, blocks, stop, msg_id=None, cwd='/w'):
    m = {'model': 'claude-sonnet-5-5', 'stop_reason': stop, 'content': blocks, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
    if msg_id:
        m['id'] = msg_id
    return st2.dump({'type': 'assistant', 'timestamp': st2.iso(t), 'cwd': cwd, 'message': m})


def text(s):
    return {'type': 'text', 'text': s}


class CliTalk(st2.CliFixture):
    C1 = '22222222-2222-4222-8222-222222222222'

    def open(self, lines):
        # the instruction is a variable, as in a real launch script: the call's own letters then neither prove nor refute anything about the child's first line
        self.write(self.PARENT, [st2.bash_line(T0, 'cd /w && claude -p "$Q"')])
        self.child_path = self.write(self.C1, lines)
        self.scan()
        s = server.Session(os.path.join(self.proj, self.PARENT + '.jsonl'))
        s.poll()
        return s

    def evs(self, s, *kinds):
        return [e for e in s.feed if e.get('agent') == self.C1 and (not kinds or e['kind'] in kinds)]

    def brief(self, s, *kinds):
        return [(e['kind'], e['from'], e['to'], e['title'], e['text']) for e in self.evs(s, *kinds)]

    def test_first_prompt_is_spawn_body(self):
        s = self.open(st2.child_lines(T0 + 5))
        sp = self.evs(s, 'spawn')
        self.assertEqual(len(sp), 1)
        self.assertEqual((sp[0]['text'], sp[0]['title'], sp[0]['from'], sp[0]['to'], sp[0]['ts']), ('hi', 'Launch child', 'orch', self.C1, T0))
        self.assertEqual(self.evs(s, 'orch_msg'), [])

    def test_spawn_keeps_its_idx_when_prompt_arrives_later(self):
        """A child transcript can show up before the first prompt: the spawn event stays as it is and only its body is filled in (the event number does not change, and it does not appear twice)."""
        s = self.open([st2.dump({'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': st2.iso(T0 + 5), 'cwd': '/w'})])
        idx = [i for i, e in enumerate(s.feed) if e['kind'] == 'spawn']
        self.assertEqual((len(idx), s.feed[idx[0]]['text']), (1, ''))
        n = len(s.feed)
        self.write(self.C1, [user_line(T0 + 6, 'do the thing')])
        s.poll()
        self.assertEqual([i for i, e in enumerate(s.feed) if e['kind'] == 'spawn'], idx)
        self.assertEqual((s.feed[idx[0]]['text'], len(s.feed)), ('do the thing', n))

    def test_later_prompts_are_orch_msg(self):
        s = self.open(st2.child_lines(T0 + 5))
        self.write(self.C1, [user_line(T0 + 60, 'second\nline'), asst_line(T0 + 64, [text('done 2')], 'end_turn', 'm2'),
                             user_line(T0 + 90, 'third')])
        s.poll()
        self.assertEqual(self.brief(s, 'orch_msg'), [('orch_msg', 'orch', self.C1, '메시지', 'second\nline'),
                                                     ('orch_msg', 'orch', self.C1, '메시지', 'third')])
        self.assertEqual([e['ts'] for e in self.evs(s, 'orch_msg')], [T0 + 60, T0 + 90])
        self.assertEqual(self.evs(s, 'spawn')[0]['text'], 'hi')

    def test_not_prompts(self):
        """Tool results, isMeta (image notes), notifications, empty text, and text holding only a system-reminder are not prompts. A system-reminder inside a prompt is stripped."""
        s = self.open(st2.child_lines(T0 + 5))
        self.write(self.C1, [result_line(T0 + 10), user_line(T0 + 11, '[Image: original 10x10]', isMeta=True),
                             user_line(T0 + 12, '<task-notification>\n<task-id>b1</task-id>\n</task-notification>'),
                             user_line(T0 + 13, '   '), user_line(T0 + 14, '<system-reminder>be nice</system-reminder>'),
                             user_line(T0 + 15, 'real<system-reminder>be nice</system-reminder> ask'),
                             st2.dump({'type': 'user', 'timestamp': st2.iso(T0 + 16), 'cwd': '/w', 'message': {'role': 'user', 'content': [
                                 {'type': 'text', 'text': 'block one'}, {'type': 'image'}, {'type': 'text', 'text': 'block two'}]}})])
        s.poll()
        self.assertEqual([e['text'] for e in self.evs(s, 'orch_msg')], ['real ask', 'block one\nblock two'])

    def test_end_turn_is_handback(self):
        s = self.open(st2.child_lines(T0 + 5))
        hb = self.evs(s, 'handback')
        self.assertEqual([(e['from'], e['to'], e['title'], e['text'], e['ts']) for e in hb], [(self.C1, 'orch', '최종 보고', 'hello', T0 + 9)])

    def test_handback_only_for_end_turn_text(self):
        """Text said while calling a tool, a transcript entry holding only thinking, and stop_sequence (a limit notice etc.) are not a report. A thinking entry and a text entry that arrive separately still count as one."""
        s = self.open([user_line(T0 + 5, 'go'),
                       asst_line(T0 + 6, [text('let me look')], 'tool_use', 'm1'),
                       asst_line(T0 + 7, [{'type': 'tool_use', 'id': 'tu1', 'name': 'Bash', 'input': {'command': 'ls'}}], 'tool_use', 'm1'),
                       result_line(T0 + 8),
                       asst_line(T0 + 9, [{'type': 'thinking', 'thinking': ''}], 'end_turn', 'm2'),
                       asst_line(T0 + 9.1, [text('first'), text('  final report  ')], 'end_turn', 'm2'),
                       asst_line(T0 + 10, [text("You've hit your weekly limit")], 'stop_sequence', 'm3'),
                       asst_line(T0 + 11, [{'type': 'thinking', 'thinking': ''}], 'end_turn', 'm4')])
        self.assertEqual([e['text'] for e in self.evs(s, 'handback')], ['final report'])

    def test_each_turn_has_its_handback(self):
        s = self.open([user_line(T0 + 5, 'go'), asst_line(T0 + 6, [text('r1')], 'end_turn', 'm1'),
                       user_line(T0 + 30, 'more'), asst_line(T0 + 31, [text('r2')], 'end_turn', 'm2')])
        self.assertEqual([(e['kind'], e['text']) for e in self.evs(s, 'spawn', 'orch_msg', 'handback')],
                         [('spawn', 'go'), ('handback', 'r1'), ('orch_msg', 'more'), ('handback', 'r2')])

    def test_same_text_twice_is_two_events(self):
        s = self.open([user_line(T0 + 5, 'go', uuid='u1'), asst_line(T0 + 6, [text('ok')], 'end_turn', 'm1'),
                       user_line(T0 + 30, 'go', uuid='u2'), asst_line(T0 + 31, [text('ok')], 'end_turn', 'm2')])
        self.assertEqual([e['kind'] for e in self.evs(s, 'orch_msg', 'handback')], ['handback', 'orch_msg', 'handback'])

    def test_no_duplicates_on_repoll_truncate_and_reown(self):
        lines = [user_line(T0 + 5, 'go', uuid='u1'), asst_line(T0 + 6, [text('r1')], 'end_turn', 'm1'),
                 user_line(T0 + 30, 'more', uuid='u2'), asst_line(T0 + 31, [text('r2')], 'end_turn', 'm2')]
        s = self.open(lines)
        want = self.brief(s)
        for _ in range(3):
            s.poll()
        self.assertEqual(self.brief(s), want)
        with open(self.child_path, 'w') as f:                        # if the transcript shrinks and is then rewritten with the same content, Tail reads it again from the start
            f.write('\n'.join(lines) + '\n' + user_line(T0 + 40, 'x' * 20000, uuid='u3') + '\n')
        os.utime(self.child_path, (T0, T0))
        s.poll()
        self.assertEqual(self.brief(s)[:len(want)], want)
        self.assertEqual(len(self.brief(s)), len(want) + 1)
        n = len(s.feed)
        os.truncate(self.child_path, 10)
        s.poll()                                                     # only half a line: no new event
        self.assertEqual(len(s.feed), n)
        with open(self.child_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        s.poll()
        self.assertEqual(len(s.feed), n)
        # losing ownership (the link becomes ambiguous) and getting it back leaves the events unchanged
        keep = dict(self.links.cli_owners)
        self.links.cli_owners = {}
        s.poll()
        self.assertNotIn(self.C1, s.agents)
        self.links.cli_owners = keep
        s.poll()
        self.assertIn(self.C1, s.agents)
        self.assertEqual(len(s.feed), n)

    def test_events_in_time_order_and_state_fields(self):
        s = self.open([user_line(T0 + 5, 'go'), asst_line(T0 + 6, [text('r1')], 'end_turn', 'm1'), user_line(T0 + 30, 'more')])
        self.assertEqual([e['ts'] for e in s.feed], sorted(e['ts'] for e in s.feed))
        with patched(claude_alive_ids=lambda: set()):
            st = s.state()
        ag = next(a for a in st['agents'] if a['id'] == self.C1)
        self.assertEqual((ag['handbacks'], ag['received']), (1, 2))            # 1 report, prompts (1 from the transcript + 1 from the events)
        sp = next(e for e in st['feed'] if e['kind'] == 'spawn')
        self.assertEqual((sp['text'], sp['full_len'], sp['agent'], sp['to']), ('go', 2, self.C1, self.C1))
        d = s.agent_detail(self.C1)
        self.assertEqual(([h['text'] for h in d['handbacks']], [m['text'] for m in d['orch_msgs']], d['spawn_prompt']), (['r1'], ['more'], 'go'))

    def test_talk_api_for_child(self):
        s = self.open([user_line(T0 + 5, 'go'), asst_line(T0 + 6, [text('r1')], 'end_turn', 'm1'), user_line(T0 + 30, 'more')])
        code, body = st1.call('/api/talk?scope=agents&session=' + self.PARENT, s)
        self.assertEqual(code, 200)
        self.assertEqual([(e['kind'], e['from'], e['to'], e['text']) for e in body['items']],
                         [('spawn', 'orch', self.C1, 'go'), ('handback', self.C1, 'orch', 'r1'), ('orch_msg', 'orch', self.C1, 'more')])
        code, body = st1.call('/api/talk?session=' + self.PARENT, s)                   # the default response has no child-session talk
        self.assertEqual((code, body['items']), (200, []))
        code, ev = st1.call('/api/event?idx=%d&session=%s' % (next(i for i, e in enumerate(s.feed) if e['kind'] == 'spawn'), self.PARENT), s)
        self.assertEqual((code, ev['text']), (200, 'go'))

    def test_other_origins_unchanged(self):
        """Claude subagents (Agent tool) and Codex stay as they are now: the prompts and end_turn text in a subagent transcript make no events."""
        a = server.Agent('a0123456789abcdef', {})
        for line in (user_line(T0, 'spawn prompt'), user_line(T0 + 1, 'The coordinator sent a message while you were working: hi'),
                     asst_line(T0 + 2, [text('all done')], 'end_turn', 'm1')):
            a.feed(json.loads(line))
        self.assertEqual((a.talk, a.origin, a.spawn_prompt, [m['text'] for m in a.received]), ([], 'subagent', 'spawn prompt', ['hi']))
        ca = server.CodexAgent({'id': 'x', 'meta_ts': 1, 'path': '/x', 'cwd': '/w', 'model': 'm'}, {})
        self.assertEqual((ca.origin, ca.talk), ('exec', []))

    def test_subagent_session_makes_no_cli_events(self):
        proj = self.proj
        parent = os.path.join(proj, self.PARENT + '.jsonl')
        aid = 'a0123456789abcdef'
        sub = os.path.join(proj, self.PARENT, 'subagents')
        os.makedirs(sub)
        with open(parent, 'w') as f:
            f.write(st2.dump({'type': 'assistant', 'timestamp': st2.iso(T0), 'cwd': '/w', 'message': {'content': [
                {'type': 'tool_use', 'id': 'toolu_9', 'name': 'Agent', 'input': {'description': 'T1-A job', 'prompt': 'P'}}]}}) + '\n')
        with open(os.path.join(sub, 'agent-%s.meta.json' % aid), 'w') as f:
            json.dump({'description': 'T1-A job', 'toolUseId': 'toolu_9'}, f)
        with open(os.path.join(sub, 'agent-%s.jsonl' % aid), 'w') as f:
            f.write('\n'.join([user_line(T0 + 1, 'P'), user_line(T0 + 2, 'follow up'),
                               asst_line(T0 + 3, [text('done')], 'end_turn', 'm1')]) + '\n')
        s = server.Session(parent)
        s.poll()
        self.assertEqual([e['kind'] for e in s.feed], ['spawn'])          # same as before: just the one Agent call in the main transcript
        self.assertEqual(s.feed[0]['text'], 'P')
        self.assertEqual(s.agents[aid].origin, 'subagent')


if __name__ == '__main__':
    unittest.main()
