"""What the server adds for the English edition and the terminal texts in the dictionary (cli.*).

- the new API fields (events, alerts, plans, token models, JSON errors) and that the old fields keep their bytes: the old Korean `title`/`text`/`error` are checked
  against literals, and every title_i18n / text_i18n / error_info is checked to say the same as the old field once the Korean dictionary words it
- the terminal in English and Korean: the start output, the warnings, the bind failures, `--help --lang en|ko`, the 403 body (English first), process method codes
- every cli.* alert.* event.* plan.* key the code names exists, and every key of those areas is used

    python3 -m unittest discover -s tests
"""
import contextlib
import datetime
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server, terminal_lang  # noqa: E402
import test_stage1 as st1  # noqa: E402
import test_stage2 as st2  # noqa: E402
from test_i18n import LocalesDir, pack  # noqa: E402
from test_preserve import T, TS, codex_entry, cx_rec, tool_use  # noqa: E402
from test_release import FakeServer, Home, live_server, main_env, run_main, write  # noqa: E402

from board import i18n, plans, procs, tokens, util, views  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZONES = ('cli', 'alert', 'event')


def load_messages(code):
    with open(os.path.join(ROOT, 'static', 'locales', code + '.json'), encoding='utf-8') as f:
        return json.load(f)['messages']


KO, EN = load_messages('ko'), load_messages('en')


def ko(key, **params):
    """The Korean dictionary's words for key: what the old Korean field of the same thing must say, byte for byte."""
    return i18n.fill(KO[key], params)


def en(key, **params):
    return i18n.fill(EN[key], params)


def said(i18n_field):
    """The Korean wording of a title_i18n / text_i18n {key, params}."""
    return ko(i18n_field['key'], **i18n_field['params'])


HEAD = ['ts', 'kind', 'from', 'to', 'title', 'text', 'agent']       # the first keys of every event, in this order, as before


class SessionCase(unittest.TestCase):
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


def say(text, mid='m1'):
    return {'type': 'assistant', 'timestamp': TS, 'message': {'id': mid, 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': text}]}}


def human(text):
    return {'type': 'user', 'timestamp': TS, 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': text}}


def peer(body):
    return {'type': 'user', 'timestamp': TS, 'origin': {'kind': 'peer', 'from': 'a1', 'body': body}, 'message': {'role': 'user', 'content': 'x'}}


class EventFields(SessionCase):
    """Events: title_i18n {key, params} and title_is_default only on a title the server wrote; the old title/text unchanged; the new keys come after the old ones."""

    def check(self, e, title, key=None, default=True):
        self.assertEqual((e['title'], e.get('title_i18n', {}).get('key'), e.get('title_is_default')), (title, key, default if key else None), e)
        self.assertEqual(list(e)[:len(HEAD)], HEAD)
        if key:
            self.assertEqual(e['title_i18n'], {'key': key, 'params': {}})
            self.assertEqual(said(e['title_i18n']), e['title'])                      # the Korean dictionary says what the old field says
            self.assertLess(list(e).index('title_i18n'), list(e).index('title_is_default'))
            self.assertGreaterEqual(list(e).index('title_i18n'), len(HEAD))        # after every old field
            self.assertIsInstance(e['title_is_default'], bool)
        else:
            self.assertNotIn('title_i18n', e)
            self.assertNotIn('title_is_default', e)

    def test_claude_main_stream(self):
        s = self.claude()
        s._feed_main(human('please do it'))
        s._feed_main(say('hello there'))
        s._feed_main(tool_use('Agent', {'description': 'T1-A research', 'prompt': 'p'}, tid='tu_a'))
        s._feed_main(tool_use('SendMessage', {'to': 'a1', 'summary': 'a summary', 'message': 'm'}, tid='tu_b'))
        s._feed_main(tool_use('SendMessage', {'to': 'a1', 'message': 'no summary here'}, tid='tu_c'))
        s._feed_main(tool_use('TaskStop', {'task_id': 'a0123456789abcdef'}, tid='tu_d'))
        s._feed_main(peer('Subagent hand-back\nThe report follows:\n  final text'))
        s._feed_main(peer('hello from a peer'))
        by = {}
        for e in s.feed:
            by.setdefault((e['kind'], e['title']), e)
        self.check(by[('user_say', '사용자 지시')], '사용자 지시', 'event.user_say.title')
        self.check(by[('orch_say', '사용자에게 보고')], '사용자에게 보고', 'event.orch_say.title')
        self.check(by[('spawn', 'T1-A research')], 'T1-A research')                    # data, not the server's words
        self.check(by[('orch_msg', 'a summary')], 'a summary')
        self.check(by[('orch_msg', '메시지')], '메시지', 'event.message.title')          # the stand-in title: the page tells it by title_is_default
        self.check(by[('stop', '에이전트 중지')], '에이전트 중지', 'event.stop.title')
        self.check(by[('handback', '최종 보고')], '최종 보고', 'event.handback.title')
        self.check(by[('peer', '메시지')], '메시지', 'event.message.title')
        self.assertEqual(by[('handback', '최종 보고')]['text'], 'final text')                # old text unchanged
        self.assertTrue(all('text_i18n' not in e for e in s.feed))                       # no event body is written by the server

    def test_choice_question_and_answer(self):
        s = self.claude()
        qs = [{'header': 'Pick', 'question': 'Which?', 'options': [{'label': 'A', 'description': 'a'}, {'label': 'B'}]}, {'question': 'No header?', 'options': []}]
        s._feed_main(tool_use('AskUserQuestion', {'questions': qs}, tid='tuq'))
        ask = s.feed[-1]
        self.check(ask, '선택지 질문', 'event.orch_ask.title')
        self.assertEqual(ask['text'], '**Pick** Which?\n- A — a\n- B — \n\n**질문** No header?')       # old text unchanged (the default header is still baked in)
        self.assertEqual(ask['questions'], [
            {'header': 'Pick', 'question': 'Which?', 'options': [{'label': 'A', 'description': 'a'}, {'label': 'B', 'description': ''}]},
            {'header': None, 'question': 'No header?', 'options': []}])                        # a missing header is null: the page words the default itself
        self.assertEqual(ko('event.ask.header'), '질문')
        s._feed_main({'type': 'user', 'timestamp': TS, 'toolUseResult': {'answers': {'Which?': 'A'}},
                      'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tuq', 'content': 'x'}]}})
        self.check(s.feed[-1], '선택', 'event.user_answer.title')
        self.assertEqual(s.feed[-1]['text'], '**Pick** A')

    def test_codex_orch_ask_has_no_descriptions(self):
        c = self.codex()
        c._feed_cx(cx_rec('response_item', 'message', {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'thinking aloud'}]}))
        self.check(c.feed[-1], '사용자에게 보고', 'event.orch_say.title')
        args = json.dumps({'questions': [{'title': 'Pick one', 'options': ['x', {'label': 'y'}]}, 'junk']})
        c._feed_cx(cx_rec('response_item', 'function_call', {'name': 'request_user_input_async', 'arguments': args, 'call_id': 'c1'}))
        ask = c.feed[-1]
        self.check(ask, '선택지 질문', 'event.orch_ask.title')
        self.assertEqual(ask['text'], '**질문** Pick one\n- x\n- y')
        self.assertEqual(ask['questions'], [{'header': None, 'question': 'Pick one', 'options': [{'label': 'x', 'description': None}, {'label': 'y', 'description': None}]}])

    def test_notify_status_codes(self):
        s = self.claude()
        for n, status in enumerate(('completed', 'failed', 'killed', 'stopped')):
            s._notification(T + n, '<task-id>a%016x</task-id><status>%s</status><summary>s%d</summary>' % (n, status, n))
        got = [(e['title'], e['status'], e.get('title_i18n', {}).get('key'), e.get('title_is_default')) for e in s.feed if e['kind'] == 'notify']
        self.assertEqual(got, [('작업 끝', 'done', 'event.notify.done', False), ('실패', 'failed', 'event.notify.failed', False),
                               ('중지됨', 'killed', 'event.notify.killed', False), ('stopped', 'stopped', None, None)])
        for e in s.feed:
            self.assertEqual(list(e)[:len(HEAD)], HEAD)
            self.assertEqual(e['title_i18n']['params'] if 'title_i18n' in e else {}, {})
            if 'title_i18n' in e:
                self.assertEqual(said(e['title_i18n']), e['title'])
        # the agent's notification record keeps its own (older) status words
        a = server.CodexAgent(codex_entry(), {})
        for n, status in enumerate(('done', 'failed', 'killed')):
            a.turns.append({'n': n, 'start': T + n * 10, 'end': T + n * 10 + 5, 'status': status, 'error': None, 'sha': None, 'msg': 'final %d' % n, 'user': 'u%d' % n,
                            'call': None, 'bash_ts': None, 'out': None, 'out_state': None, 'mapped': True})
        c = self.claude()
        c.codex._derive(a)
        notes = [(e['title'], e['status'], e['title_i18n']['key']) for e in c.feed if e['kind'] == 'notify']
        self.assertEqual(notes, [('작업 끝', 'done', 'event.notify.done'), ('실패', 'failed', 'event.notify.failed'), ('중지됨', 'killed', 'event.notify.killed')])
        self.assertEqual([e['title_i18n']['key'] for e in c.feed if e['kind'] == 'handback'], ['event.handback.title'] * 1)   # only the finished turn has a report

    def test_agent_message_without_a_summary(self):
        s = self.claude()
        a = server.Agent('a%016x' % 1, {'description': 'T1-A'})
        a.sent = [{'ts': T, 'to': 'orch', 'summary': '', 'text': 'body one'}, {'ts': T + 1, 'to': 'orch', 'summary': 'S2', 'text': 'body two'}]
        s.agents[a.id] = a
        s._agent_events()
        self.check(s.feed[0], '메시지', 'event.message.title')
        self.check(s.feed[1], 'S2')
        self.assertEqual(s.feed[0]['peer'], None)                                          # the old extra field is still there

    def test_claude_p_child_without_a_description(self):
        class Case(st2.CliFixture):
            C1 = '22222222-2222-4222-8222-222222222222'

            def runTest(self):
                pass
        for desc, title, key in (('', 'Claude Code 실행', 'event.spawn_cli.title'), ('Launch child', 'Launch child', None)):
            case = Case()
            case.setUp()
            try:
                case.write(case.PARENT, [st2.bash_line(st2.T0, 'cd /w && claude -p hi', desc=desc)])
                case.write(case.C1, st2.child_lines(st2.T0 + 5))
                case.scan()
                s = server.Session(os.path.join(case.proj, case.PARENT + '.jsonl'))
                s.poll()
                (sp,) = [e for e in s.feed if e['kind'] == 'spawn']
                self.assertEqual((sp['title'], sp.get('title_i18n', {}).get('key')), (title, key))
                if key:
                    self.assertEqual((sp['title_is_default'], said(sp['title_i18n'])), (True, sp['title']))
            finally:
                case.doCleanups()


class AlertFields(SessionCase):
    """Alerts: title_i18n for every alert, text_i18n only where the server wrote the text (the Codex limit); old title/text unchanged."""

    def alerts(self, s, statuses=None, now=T + 100):
        with mock.patch.object(views, 'CODEX', types.SimpleNamespace(limit=lambda: None)):
            return views.alerts(s, statuses or {}, now)

    def check(self, a, title, key, params, text=None):
        self.assertEqual((a['title'], a['title_i18n']), (title, {'key': key, 'params': params}), a)
        self.assertEqual(said(a['title_i18n']), a['title'])
        self.assertEqual(list(a)[:6], ['id', 'level', 'title', 'text', 'ts', 'agent'])
        self.assertNotIn('text_i18n', a)
        if text is not None:
            self.assertEqual(a['text'], text)

    def test_choice_question_say_and_turn(self):
        s = self.claude()
        s._feed_main(tool_use('AskUserQuestion', {'questions': [{'question': 'Which?', 'options': [{'label': 'A'}, {'label': 'B'}]}]}, tid='tuq'))
        s.orch['last_ts'] = T
        (a,) = self.alerts(s)
        self.check(a, '오케스트레이터가 선택지를 묻고 있습니다', 'alert.ask.title', {}, 'Which? — A / B')
        s = self.claude()
        s._feed_main(say('Shall I go ahead?'))
        s.orch['last_ts'] = T
        (a,) = self.alerts(s)
        self.check(a, '오케스트레이터가 답을 기다립니다', 'alert.say.title', {}, 'Shall I go ahead?')
        s = self.claude()
        s._feed_main(say('All done.'))
        s.orch['last_ts'] = T
        (a,) = self.alerts(s)
        self.check(a, '진행 중인 에이전트가 없고 오케스트레이터가 다음 지시를 기다립니다', 'alert.turn.title', {}, 'All done.')
        self.assertEqual(a['level'], 'info')

    def agents_session(self, **kw):
        s = self.claude()
        a = server.Agent('a%016x' % 1, {'description': 'T1-A research'})
        a.tag, a.last_ts = 'T1-A', T
        a.activity.append({'ts': T, 'kind': 'tool', 'name': 'Bash', 'text': 'running tests', 'cat': 'bash'})
        a.notifications.append({'ts': T, 'status': 'failed', 'summary': 'it broke', 'tokens': None, 'tools': None, 'duration_ms': None})
        s.agents[a.id] = a
        return s, a

    def test_stall_failed_and_stopped(self):
        s, a = self.agents_session()
        s.orch['last_ts'] = T
        stalled = self.alerts(s, {a.id: 'stalled'}, now=T + 7 * 60 + 5)
        (x,) = stalled
        self.check(x, 'T1-A — 7분째 활동 없음', 'alert.stall.title', {'name': 'T1-A', 'minutes': 7}, 'running tests')
        self.assertIsInstance(x['title_i18n']['params']['minutes'], int)
        (x,) = self.alerts(s, {a.id: 'failed'}, now=T + 60)
        self.check(x, 'T1-A — 실패', 'alert.fail.failed.title', {'name': 'T1-A'}, 'it broke')
        (x,) = self.alerts(s, {a.id: 'killed'}, now=T + 60)
        self.check(x, 'T1-A — 중지됨', 'alert.fail.killed.title', {'name': 'T1-A'}, 'it broke')

    def test_report_with_a_decision_item(self):
        s, a = self.agents_session()
        s.orch['last_ts'] = T
        s.feed.append({'ts': T, 'kind': 'handback', 'from': a.id, 'to': 'orch', 'title': '최종 보고', 'text': 'done\n- user decision: pick A or B', 'agent': a.id})
        (x,) = self.alerts(s, {a.id: 'done'})
        self.check(x, 'T1-A 보고에 사용자 결정·승인 항목', 'alert.hb.title', {'name': 'T1-A'}, 'user decision: pick A or B')

    def test_codex_weekly_limit(self):
        s = self.claude()
        s.orch['last_ts'] = T
        s._codex_busy = lambda statuses, now: True
        as_of, resets = 1790726400.0, 1791000000.0
        stamp = lambda ts: datetime.datetime.fromtimestamp(ts).strftime('%m/%d %H:%M')
        for lim, title, key, params in (
                ({'used_percent': 93.4, 'reached': False}, '◆ Codex 주간 한도 93% — 남은 Codex 작업이 멈출 수 있음', 'alert.cxlimit.percent.title', {'percent': 93}),
                ({'used_percent': 100.0, 'reached': True}, '◆ Codex 주간 한도 도달 — 남은 Codex 작업이 멈출 수 있음', 'alert.cxlimit.reached.title', {})):
            full = dict(lim, stale=False, as_of=as_of, resets_at=resets)
            with mock.patch.object(views, 'CODEX', types.SimpleNamespace(limit=lambda: full)):
                (x,) = views.alerts(s, {}, T + 100)
            old_text = '계정 전체 값 · 기록 %s · 재설정 %s' % (stamp(as_of), stamp(resets))
            self.assertEqual((x['title'], x['text'], x['id'], x['ts']), (title, old_text, 'cxlimit:%s' % resets, as_of))
            self.assertEqual(x['title_i18n'], {'key': key, 'params': params})
            self.assertEqual(said(x['title_i18n']), title)
            self.assertEqual(x['text_i18n'], {'key': 'alert.cxlimit.text', 'params': {'as_of': as_of, 'resets_at': resets}})
            self.assertEqual(ko('alert.cxlimit.text', as_of=stamp(as_of), resets_at=stamp(resets)), old_text)      # the page formats the epoch seconds
            self.assertEqual(list(x)[:6], ['id', 'level', 'title', 'text', 'ts', 'agent'])
        full = dict(used_percent=95.0, reached=False, stale=False, as_of=None, resets_at=None)               # unknown dates: null, shown as '-'
        with mock.patch.object(views, 'CODEX', types.SimpleNamespace(limit=lambda: full)):
            (x,) = views.alerts(s, {}, T + 100)
        self.assertEqual((x['text'], x['text_i18n']['params']), ('계정 전체 값 · 기록 - · 재설정 -', {'as_of': None, 'resets_at': None}))


class TokenModels(unittest.TestCase):
    """Token models: model_costs [{model, advisor, cost}] beside the old `models` (which keeps the ' 조언' suffix key)."""

    def test_advisor_structure(self):
        tm = server.TokenMeter()
        tm.add({'id': 'm1', 'model': 'claude-opus-5-5', 'usage': {'iterations': [
            {'type': 'message', 'input_tokens': 5, 'output_tokens': 7},
            {'type': 'advisor_message', 'model': 'claude-opus-5-5', 'input_tokens': 100000, 'output_tokens': 20000}]}})
        tm.add({'id': 'm2', 'model': 'claude-sonnet-5-5', 'usage': {'input_tokens': 1000, 'output_tokens': 500}})
        d = tm.as_dict()
        self.assertEqual(set(d['models']), {'opus-5-5', 'opus-5-5 조언', 'sonnet-5-5'})                       # the old keys, suffix included
        self.assertEqual([(m['model'], m['advisor']) for m in d['model_costs']], [(k[:-3] if k.endswith(' 조언') else k, k.endswith(' 조언')) for k in d['models']])
        self.assertEqual({(m['model'], m['advisor']): m['cost'] for m in d['model_costs']},
                         {('opus-5-5', False): d['models']['opus-5-5'], ('opus-5-5', True): d['models']['opus-5-5 조언'], ('sonnet-5-5', False): d['models']['sonnet-5-5']})
        self.assertTrue(all(isinstance(m['advisor'], bool) for m in d['model_costs']))
        self.assertEqual(list(d).index('model_costs'), list(d).index('models') + 1)

    def test_empty_and_codex(self):
        self.assertEqual(server.TokenMeter().as_dict()['model_costs'], [])
        tm = server.TokenMeter()
        tm.add_codex('r1', {'input_tokens': 1000, 'output_tokens': 100}, 'gpt-6.1-sol')
        d = tm.as_dict()
        self.assertEqual([(m['model'], m['advisor']) for m in d['model_costs']], [(k, False) for k in d['models']])


class ToolTexts(unittest.TestCase):
    """The words the server itself puts in a tool's summary come with a dictionary key (the page words them in its own language); the old Korean field keeps its bytes."""

    def feed(self, name, inp):
        a = server.Agent('a%016x' % 1, {'description': 'T1-A research'})
        a.feed(tool_use(name, inp, tid='t1'))
        return a

    def test_the_handback_summary_has_a_key_and_the_old_text_agrees_with_the_korean_dictionary(self):
        a = self.feed('SubagentHandback', {})
        entry = next(x for x in a.activity if x['kind'] == 'tool')
        self.assertEqual(entry['text'], '최종 보고 전달')
        self.assertEqual(entry['text_i18n'], {'key': 'event.tool.handback', 'params': {}})
        self.assertEqual(said(entry['text_i18n']), entry['text'])
        self.assertIn('event.tool.handback', EN)
        self.assertFalse(re.search('[가-힣]', EN['event.tool.handback']))
        self.assertEqual(a.pending['t1']['text_i18n'], entry['text_i18n'])

    def test_a_tool_whose_summary_is_the_agents_own_data_has_no_key(self):
        a = self.feed('Bash', {'command': 'ls', 'description': 'List files'})
        entry = next(x for x in a.activity if x['kind'] == 'tool')
        self.assertEqual(entry['text'], 'List files')
        self.assertNotIn('text_i18n', entry)
        self.assertNotIn('text_i18n', a.pending['t1'])


class ErrorCodes(unittest.TestCase):
    """JSON errors: error_code after the old fields, which keep their bytes."""
    S = '&session=11111111-2222-4333-8444-555555555555'

    def test_every_json_error(self):
        sess = st1.FakeSession()
        cases = [('/api/event?idx=99' + self.S, 404, {'error': 'no event'}, 'no_event'),
                 ('/api/event?idx=x' + self.S, 400, {'error': 'bad parameter'}, 'bad_parameter'),
                 ('/api/nothing?x=1' + self.S, 404, {'error': 'not found'}, 'not_found'),
                 ('/nothing', 404, {'error': 'not found'}, 'not_found'),
                 ('/fonts/..%2Fx.woff2', 404, {'error': 'not found'}, 'not_found'),
                 ('/locales/zz.json', 404, {'error': 'not found'}, 'not_found'),
                 ('/api/file?path=/x/y.md' + self.S, 403, {'error': 'not allowed'}, 'not_allowed')]
        sess.agent_detail = lambda aid: None
        sess.allowed_file = lambda p: None
        cases.append(('/api/agent?id=zz' + self.S, 404, {'error': 'no agent'}, 'no_agent'))
        for path, code, old, error_code in cases:
            got_code, body = st1.call(path, sess)
            self.assertEqual((got_code, body), (code, dict(old, error_code=error_code)), path)
            self.assertEqual(list(body), list(old) + ['error_code'], path)               # the old fields first, in their order
        with mock.patch.object(server, 'REG', types.SimpleNamespace(get=lambda sid: None)):
            h = server.Handler.__new__(server.Handler)
            sent = []
            h.path, h.headers = '/api/state?session=abc', {'Host': 'localhost:8790'}
            h._send = lambda c, b, ctype=None: sent.append((c, b))
            h.do_GET()
        self.assertEqual(sent, [(404, {'error': 'session not found', 'session': 'abc', 'error_code': 'session_not_found'})])

    def test_too_large_and_open_failed(self):
        with tempfile.TemporaryDirectory() as d:
            big = os.path.join(d, 'big.md')
            with open(big, 'w') as f:
                f.write('x' * (server.FILE_MAX + 1))
            folder = os.path.join(d, 'folder.md')
            os.mkdir(folder)
            s = st1.FakeSession()
            s.allowed_file = lambda p: os.path.realpath(p)
            code, body = st1.call('/api/file?path=' + big + self.S, s)
            self.assertEqual((code, body), (413, {'error': 'too large', 'limit': server.FILE_MAX, 'error_code': 'too_large'}))
            self.assertEqual(list(body), ['error', 'limit', 'error_code'])
            code, body = st1.call('/api/file?path=' + folder + self.S, s)
            self.assertEqual((code, body), (404, {'error': '일반 파일이 아닙니다', 'error_code': 'open_failed'}))     # the OS-level text is kept as it was

    def test_over_http_a_session_error_has_the_code_and_a_good_answer_does_not(self):
        srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
        c.request('GET', '/api/state?session=nope', headers={'Host': 'localhost:8790'})
        r = c.getresponse()
        self.assertEqual((r.status, json.loads(r.read())), (404, {'error': 'session not found', 'session': 'nope', 'error_code': 'session_not_found'}))
        c.request('GET', '/api/i18n', headers={'Host': 'localhost:8790'})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertNotIn('error_code', json.loads(r.read()))


class TerminalTexts(unittest.TestCase):
    """The start output, the warnings and the failures in both languages (the Korean words are the old ones: tests/test_release.py checks them as they ran before)."""

    def srcs(self, **kw):
        base = [{'provider': 'claude', 'dir': '/h/.claude', 'exists': True, 'sessions': 3, 'agent_sessions': 1, 'from': 'default'},
                {'provider': 'codex', 'dir': '/h/.codex', 'exists': True, 'sessions': 2, 'from': 'env'}]
        return base

    def test_start_lines(self):
        row = lambda name, where, n, label: '  %-13s %-28s %s  (%s)' % (name, where, n, label)
        for lang, want in (
                ('ko', [row('Claude Code', '/h/.claude/projects', '세션 3 · 에이전트 있는 세션 1', '기본'), row('◆ Codex', '/h/.codex/sessions', '단독 대화, 최근 7일: 2', 'CODEX_HOME'),
                        '  프로세스 판정 /proc']),
                ('en', [row('Claude Code', '/h/.claude/projects', 'sessions 3 · with agents 1', 'default'), row('◆ Codex', '/h/.codex/sessions', 'standalone sessions, last 7 days: 2', 'CODEX_HOME'),
                        '  Process check /proc'])):
            with terminal_lang(lang), mock.patch.object(procs, 'method', lambda: 'proc'):
                self.assertEqual(server.startup_lines(self.srcs()), want, lang)

    def test_method_variants_and_a_missing_folder(self):
        srcs = self.srcs()
        srcs[1].update(exists=False)
        want = {'ko': {'none': '없음(세션 상태는 모름으로 보임)', 'ps': 'ps(Codex가 연 파일은 모름)', 'proc': '/proc', 'no': '폴더 없음'},
                'en': {'none': 'none (session state shows as unknown)', 'ps': 'ps (cannot see files Codex has open)', 'proc': '/proc', 'no': 'folder not found'}}
        for lang in ('ko', 'en'):
            for method in ('proc', 'ps', 'none'):
                with terminal_lang(lang), mock.patch.object(procs, 'method', lambda m=method: m):
                    lines = server.startup_lines(srcs)
                self.assertIn(want[lang]['no'], lines[1])
                self.assertTrue(lines[2].endswith(want[lang][method]), lines[2])

    def test_process_method_is_a_code(self):
        self.assertIn(procs.method(), ('proc', 'ps', 'none'))
        for want, has_proc, ps in (('proc', True, None), ('ps', False, '/bin/ps'), ('none', False, None)):
            with mock.patch.object(procs, 'has_proc', lambda v=has_proc: v), mock.patch('shutil.which', lambda n, v=ps: v):
                self.assertEqual(procs.method(), want)
                self.assertIn('cli.start.method.' + want, KO)
                self.assertIn('cli.start.method.' + want, EN)

    def test_bind_failures(self):
        import errno
        want = {'ko': {errno.EADDRINUSE: '현황판을 열지 못했습니다: 127.0.0.1:8790 — 포트를 이미 쓰고 있습니다. 이미 띄운 현황판이면 http://localhost:8790/ 을 여세요. 다른 포트로 띄우려면 --port 8791 처럼 주세요.',
                       errno.EACCES: '현황판을 열지 못했습니다: 127.0.0.1:8790 — 권한이 없습니다(1024 미만 포트는 관리자만 열 수 있습니다). --port 8790 처럼 큰 포트를 주세요.',
                       errno.EADDRNOTAVAIL: '현황판을 열지 못했습니다: 127.0.0.1:8790 — 이 기기에 없는 주소입니다(VPN이 꺼져 있으면 그 주소도 없습니다). --host 값을 확인하세요.',
                       errno.EIO: '현황판을 열지 못했습니다: 127.0.0.1:8790 — Input/output error. 다른 포트는 --port 8791 처럼 주세요.',
                       'name': '현황판을 열지 못했습니다: 127.0.0.1:8790 — 호스트 이름을 찾을 수 없습니다. --host 값을 확인하세요.'},
                'en': {errno.EADDRINUSE: "Couldn't start the dashboard on 127.0.0.1:8790 — the port is already in use. If it is a dashboard you already started, open http://localhost:8790/. For another port, pass --port 8791.",
                       errno.EACCES: "Couldn't start the dashboard on 127.0.0.1:8790 — permission denied (ports below 1024 need administrator rights). Pass a higher port such as --port 8790.",
                       errno.EADDRNOTAVAIL: "Couldn't start the dashboard on 127.0.0.1:8790 — this machine has no such address (a VPN address disappears while the VPN is down). Check --host.",
                       errno.EIO: "Couldn't start the dashboard on 127.0.0.1:8790 — Input/output error. For another port, pass --port 8791.",
                       'name': "Couldn't start the dashboard on 127.0.0.1:8790 — the host name cannot be resolved. Check --host."}}
        for lang in ('ko', 'en'):
            for no, text in want[lang].items():
                err = io.StringIO()
                failure = socket.gaierror(-2, 'Name or service not known') if no == 'name' else OSError(no, os.strerror(no))
                with terminal_lang(lang), contextlib.redirect_stderr(err), mock.patch.object(server, 'BoardServer', side_effect=failure), \
                        self.assertRaises(SystemExit) as cm:
                    server.bind_servers(['127.0.0.1'], 8790)
                self.assertEqual((cm.exception.code, err.getvalue()), (1, text + '\n'), (lang, no))

    def test_argument_errors(self):
        import argparse
        host = {'ko': "열 수 없는 주소입니다: 'a b' (127.0.0.1, 0.0.0.0, :: 같은 IP 주소나 호스트 이름을 포트 없이 주세요)",
                'en': "Cannot listen on this address: 'a b' (give an IP address such as 127.0.0.1, 0.0.0.0 or ::, or a host name, with no port)"}
        other = {'ko': ("이름이 아닙니다: 'a b' (예: my.box, .example.net)", "UUID 모양이 아닙니다: 'zz'", '빈 경로입니다'),
                 'en': ("Not a host name: 'a b' (e.g. my.box, .example.net)", "Not a UUID: 'zz'", 'Empty path')}
        for lang in ('ko', 'en'):
            with terminal_lang(lang):
                for fn, arg, text in ((server._host_arg, 'a b', host[lang]), (server._allow_host_arg, 'a b', other[lang][0]),
                                      (server._session_arg, 'zz', other[lang][1]), (server._dir_arg, ' ', other[lang][2])):
                    with self.assertRaises(argparse.ArgumentTypeError) as cm:
                        fn(arg)
                    self.assertEqual(str(cm.exception), text, lang)

    def test_skipped_line_log(self):
        for lang, want in (('ko', '줄 건너뜀 sid 1.5 ValueError\n'), ('en', 'skipped a line: sid 1.5 ValueError\n')):
            out = io.StringIO()
            with terminal_lang(lang), contextlib.redirect_stdout(out):
                util.line_error('sid', {'timestamp': 1.5}, ValueError('secret content'))
            self.assertEqual(out.getvalue(), want)                       # the content of the line is never printed
        out = io.StringIO()
        with terminal_lang('ko'), contextlib.redirect_stdout(out):
            util.line_error('sid', None, KeyError())
        self.assertEqual(out.getvalue(), '줄 건너뜀 sid None KeyError\n')

    def test_a_language_in_the_environment_reaches_the_modules_below_the_server(self):
        with terminal_lang('en'):
            self.assertEqual((server.CLI_LANG, i18n.CLI_LANG), ('en', 'en'))
        with terminal_lang('ko'):
            self.assertEqual((server.CLI_LANG, i18n.CLI_LANG), ('ko', 'ko'))
        self.assertEqual(i18n.cli_t('cli.arg.dir'), 'Empty path' if i18n.CLI_LANG == 'en' else '빈 경로입니다')


class StartOutputInEnglish(unittest.TestCase):
    """main() in the English terminal: the language order (--lang, AGENT_BULLPEN_LANG, LC_ALL, LC_MESSAGES, LANG, English) and the words."""

    def setUp(self):
        self.h = Home(self)

    def run_with(self, argv, lang='en'):
        with main_env(self, self.h, run_inline=False):                  # the usage thread must not run (its loop never ends)
            return run_main(argv, servers=[FakeServer()], lang=lang)

    def test_loopback(self):
        out, err, code = self.run_with(['--port', '0'])
        self.assertEqual((err, code), ('', None))
        lines = out.splitlines()
        self.assertEqual(lines[0], 'Dashboard: http://localhost:8790/')
        self.assertTrue(lines[1].startswith('  Claude Code   '), lines[1])
        self.assertIn('sessions 0 · with agents 0', lines[1])                  # the empty HOME has the folders but no session
        self.assertIn('standalone sessions, last 7 days: 0', lines[2])
        self.assertTrue(re.fullmatch(r'  Process check (/proc|ps \(cannot see files Codex has open\)|none \(session state shows as unknown\))', lines[-1]), lines[-1])
        self.assertFalse(any('가' <= c <= '힣' for c in out), out)

    def test_counts_and_sources(self):
        self.h.session('11111111-0000-4000-8000-000000000000', t=time.time() - 50, agents=1)
        self.h.session('22222222-0000-4000-8000-000000000000', t=time.time() - 30)
        out, _, _ = self.run_with(['--port', '0'])
        claude = next(ln for ln in out.splitlines() if 'Claude Code' in ln)
        self.assertIn('sessions 2 · with agents 1', claude)
        self.assertIn('(default)', claude)
        self.assertIn('standalone sessions, last 7 days: 0', next(ln for ln in out.splitlines() if 'Codex' in ln))

    def test_the_unprotected_warning_and_the_token_lines(self):
        with main_env(self, self.h, run_inline=False):
            out, err, code = run_main(['--port', '0', '--host', '127.0.0.1', '--host', '100.100.0.7', '--allow-host', 'box.example.net', '--no-auth'],
                                      servers=[FakeServer(), FakeServer()], lang='en')
        bar, title, warn, bar2 = err.splitlines()
        self.assertEqual((bar, title, bar2), ('!' * 72, 'WARNING — NO AUTHENTICATION', '!' * 72))
        self.assertTrue(warn.startswith('This server has no login and no token. Every device that can reach the addresses below can read your Claude and Codex conversations'), warn)
        self.assertTrue(warn.endswith('(access control is up to your VPN and firewall): listening on 100.100.0.7 · allowed names box.example.net'), warn)
        self.assertEqual(out.count('Dashboard: http://'), 2)
        with main_env(self, self.h, run_inline=False):
            out, err, code = run_main(['--port', '0', '--host', '100.100.0.7'], servers=[FakeServer()], lang='en')
        lines = out.splitlines()
        self.assertEqual(err, '')
        self.assertEqual(lines[0], 'Dashboard — an access token is needed. Open one of these:')
        self.assertTrue(re.fullmatch(r'  http://100\.100\.0\.7:8790/\?token=[A-Za-z0-9_-]{43}', lines[1]), lines[1])
        self.assertTrue(lines[2].startswith('The browser keeps the token in a cookie after the first visit'), lines[2])

    def test_the_order_of_the_choice(self):
        for env, argv, want in (({'LANG': 'C'}, [], 'en'), ({'LANG': 'ko_KR.UTF-8'}, [], 'ko'), ({'LANG': 'ko_KR.UTF-8'}, ['--lang', 'en'], 'en'),
                                ({'AGENT_BULLPEN_LANG': 'en', 'LC_ALL': 'ko_KR.UTF-8'}, [], 'en'), ({'LC_ALL': 'C', 'LANG': 'ko_KR.UTF-8'}, [], 'en')):
            with mock.patch.dict(os.environ, env, clear=True), main_env(self, self.h):
                out, _, _ = run_main(['--port', '0'] + argv, servers=[FakeServer()], lang=None)
            self.assertTrue(out.startswith({'en': 'Dashboard: ', 'ko': '현황판: '}[want]), (env, argv, out[:30]))

    def test_session_not_found_and_path_flags_in_english(self):
        with main_env(self, self.h), patched(PATH_FROM={'claude': 'default', 'codex': 'default'}):
            out, err, code = run_main(['--port', '0', '--claude-config-dir', '/somewhere/else'], lang='en')
        self.assertEqual(code, 2)
        self.assertIn('--claude-config-dir /somewhere/else does not match the path the server is using (', err)
        self.assertIn('Path flags only apply when you run python3 server.py directly', err)
        with terminal_lang('en'):
            self.assertEqual(server.cli_t('cli.arg.session_not_found', session='abc'), 'Session not found: abc')

    def test_broken_dictionary_warning_in_both_languages(self):
        for lang, want in (('en', 'warning: static/locales/fr.json was not registered: '), ('ko', '경고: 사전 파일을 등록하지 못했습니다(static/locales/fr.json): ')):
            with self.subTest(lang=lang):
                loc = LocalesDir(self)
                loc.put('en.json', pack('en', 'English', {'cli.warn.dictionary': EN['cli.warn.dictionary']}))
                loc.put('ko.json', pack('ko', '한국어', {'cli.warn.dictionary': KO['cli.warn.dictionary']}))
                loc.put('fr.json', '{broken')
                with main_env(self, self.h):
                    out, err, code = run_main(['--port', '0'], servers=[FakeServer()], lang=lang)
                self.assertEqual(err.count(want), 1, err)


class HelpAndHostBody(unittest.TestCase):
    """`--help --lang en|ko` in a real process, and the 403 body: English first, the same whatever the terminal language."""

    def help(self, argv, env=None):
        e = {k: v for k, v in os.environ.items() if k not in ('LANG', 'LC_ALL', 'LC_MESSAGES', 'AGENT_BULLPEN_LANG', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
        e.update(env or {}, PYTHONDONTWRITEBYTECODE='1', HOME=tempfile.gettempdir(), COLUMNS='100')
        r = subprocess.run([sys.executable, server.__file__] + argv, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual((r.returncode, r.stderr), (0, ''))
        return r.stdout

    def test_help_in_each_language(self):
        ko_text = self.help(['--help', '--lang', 'ko'], {'LANG': 'C'})
        en_text = self.help(['--lang', 'en', '--help'], {'LANG': 'ko_KR.UTF-8'})
        self.assertIn('Claude Code·Codex 멀티 에이전트 현황판', ko_text)
        self.assertIn('지원: en, ko', ko_text)
        self.assertIn('a live dashboard for the multi-agent sessions of Claude Code and Codex', en_text)
        self.assertIn('Supported: en, ko', en_text)
        self.assertFalse(any('가' <= c <= '힣' for c in en_text), en_text)
        self.assertNotIn('Supported:', ko_text)
        for text in (ko_text, en_text):
            for flag in ('--session SESSION', '--host HOST', '--allow-host NAME', '--claude-config-dir DIR', '--codex-home DIR', '--lang CODE', '--token VALUE', '--no-auth', '--version'):
                self.assertIn(flag, text)
            self.assertNotIn('usage-api', text)
            self.assertNotIn('netbird.cloud', text.lower())
        self.assertEqual(self.help(['--help'], {'LANG': 'ko_KR.UTF-8'}), ko_text)                     # the environment picks the same
        self.assertEqual(self.help(['--help'], {'LANG': 'C'}), self.help(['--help', '--lang', 'en']))
        self.assertEqual(self.help(['--help'], {'AGENT_BULLPEN_LANG': 'ko', 'LANG': 'C'}), ko_text)
        self.assertEqual(self.help(['--help', '--lang', 'xx'], {'LANG': 'ko'}), self.help(['--help', '--lang', 'en']))      # an unknown language is English

    def test_the_link_cache_help_names_everything_the_file_keeps(self):
        en_text = ' '.join(self.help(['--lang', 'en', '--help']).split())
        ko_text = ' '.join(self.help(['--lang', 'ko', '--help']).split())
        for word in ('process lineage', 'environment', 'an output file', 'a long matching instruction', 'links.json'):
            self.assertIn(word, en_text)
        for word in ('프로세스 계보', '환경변수', '출력 파일', '긴 지시문 일치', 'links.json'):
            self.assertIn(word, ko_text)
        with open(os.path.join(ROOT, 'docs', 'configuration.md'), encoding='utf-8') as f:
            (row,) = [ln for ln in f if ln.startswith('| `XDG_CACHE_HOME`')]
        self.assertIn('links.json', row)                                                          # the folder holds the link record as well as the status line reading
        self.assertIn('statusline.json', row)

    def test_descriptions_and_help_have_no_percent_trouble(self):
        for lang in ('ko', 'en'):
            for key in KO:
                if key.startswith('cli.help.'):
                    self.assertNotIn('%(', i18n.translator(lang)(key, langs='en', sec=60))

    def test_403_body_is_english_first_then_korean_and_ignores_the_terminal_language(self):
        want = ('Host not allowed: box.tail1234.ts.net. Restart the server with --allow-host box.tail1234.ts.net (or a suffix such as --allow-host .tail1234.ts.net).\n'
                '허용하지 않은 접속 이름입니다: box.tail1234.ts.net. 서버를 --allow-host box.tail1234.ts.net 와 함께 다시 시작하세요(이름이 여럿이면 --allow-host .tail1234.ts.net 처럼 .접미사로).\n')
        for lang in ('en', 'ko'):
            with terminal_lang(lang):
                self.assertEqual(server.host_hint('box.tail1234.ts.net:8790'), want, lang)

    def test_403_placeholders_per_language_for_a_strange_name(self):
        for header in ('<script>alert(1)</script>', 'a b', None, '', 'x@y'):
            en_line, ko_line = server.host_hint(header).splitlines()
            self.assertEqual(en_line, 'Host not allowed: <name>. Restart the server with --allow-host <name> (or a suffix such as --allow-host .<suffix>).')
            self.assertEqual(ko_line, '허용하지 않은 접속 이름입니다: <이름>. 서버를 --allow-host <이름> 와 함께 다시 시작하세요(이름이 여럿이면 --allow-host .<접미사> 처럼 .접미사로).')
        short = server.host_hint('localhost:1').splitlines()                                 # a one-label name has no suffix to suggest
        self.assertIn('--allow-host .<suffix>', short[0])
        self.assertIn('--allow-host .<접미사>', short[1])

    def test_403_without_a_korean_dictionary_is_one_english_line(self):
        loc = LocalesDir(self)
        loc.put('en.json', pack('en', 'English', {'cli.host.denied': EN['cli.host.denied'], 'cli.host.name_unknown': EN['cli.host.name_unknown'],
                                                  'cli.host.suffix_unknown': EN['cli.host.suffix_unknown']}))
        self.assertEqual(len(server.host_hint('box.example.net').splitlines()), 1)

    def test_403_over_http_from_a_live_english_terminal(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, '.claude', 'projects'))
            with live_server(['--port', '0', '--lang', 'en'], home, wait=('Process check',)) as run:
                port = run.port()
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                c.request('GET', '/api/plans', headers={'Host': 'box.example.net:%d' % port})
                r = c.getresponse()
                body = r.read().decode()
                self.assertEqual(r.status, 403)
                self.assertTrue(body.startswith('Host not allowed: box.example.net.'), body)
                self.assertEqual(len(body.splitlines()), 2)
            self.assertIn('Dashboard: http://localhost:', run.out_text)
            self.assertEqual(run.err_text, '')


class EnglishTerminalEndToEnd(unittest.TestCase):
    """A separate server process in an English terminal (the language order from a real environment) next to the Korean one that the other tests pin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')
        write(os.path.join(self.home, '.claude', 'projects', '-w', '11111111-0000-4000-8000-000000000000.jsonl'),
              json.dumps({'type': 'user', 'timestamp': '2026-09-30T00:00:00Z', 'cwd': '/w', 'message': {'content': 'hi'}}) + '\n')

    def test_lang_c_gives_english_and_ko_gives_korean(self):
        for env, first, status in (({'AGENT_BULLPEN_LANG': 'auto', 'LANG': 'C', 'LC_ALL': 'C'}, 'Dashboard: http://localhost:', '  Process check '),
                                   ({'AGENT_BULLPEN_LANG': 'ko'}, '현황판: http://localhost:', '  프로세스 판정 ')):
            with live_server(['--port', '0'], self.home, extra_env=env, wait=(status.strip(),)) as run:      # the ready line differs by language
                pass
            lines = run.out_text.splitlines()
            self.assertTrue(lines[0].startswith(first), lines[0])
            self.assertTrue(any(ln.startswith(status) for ln in lines), lines)
            self.assertEqual(run.err_text, '')


class KeysAreUsed(unittest.TestCase):
    """Every cli.* alert.* event.* key that the code names exists (in both languages), and every key of those areas is used by the code."""

    def named(self):
        names, files = set(), [os.path.join(ROOT, 'server.py')] + [os.path.join(ROOT, 'board', f) for f in sorted(os.listdir(os.path.join(ROOT, 'board'))) if f.endswith('.py')]
        for f in files:
            with open(f, encoding='utf-8') as fh:
                src = fh.read()
            for m in re.finditer(r"""['"]((?:cli|alert|event)\.[A-Za-z_.%]+)['"]""", src):
                names.add(m.group(1))
        return names

    def test_named_keys_exist_in_both_languages(self):
        dynamic = {'cli.start.method.': ('proc', 'ps', 'none'), 'alert.fail.%s.title': ('failed', 'killed')}
        for name in sorted(self.named()):
            if name == 'event.notify.':                       # a prefix test in the event code, not a key
                continue
            keys = [name + v for v in dynamic[name]] if name.endswith('.') else [name % v for v in dynamic[name]] if '%' in name else [name]
            for k in keys:
                self.assertIn(k, EN, k)
                self.assertIn(k, KO, k)

    def test_every_key_of_the_areas_is_used(self):
        used = set()
        for name in self.named():
            if name.endswith('.'):
                used.update(k for k in EN if k.startswith(name))
            elif '%' in name:
                used.update(name % v for v in ('failed', 'killed'))
            else:
                used.add(name)
        page_only = {'event.ask.header'}              # worded by the page: the default header of a question whose `header` is null
        for k in EN:
            if k.split('.')[0] in ZONES and not k.endswith('._') and k not in page_only:
                self.assertIn(k, used, 'unused key ' + k)

    def test_the_areas_have_the_same_keys_in_both_languages_and_no_hangul_in_english(self):
        for zone in ZONES:
            ek, kk = [k for k in EN if k.startswith(zone + '.')], [k for k in KO if k.startswith(zone + '.')]
            self.assertEqual(ek, kk, zone)
        for k, v in EN.items():
            if k.split('.')[0] in ZONES:
                self.assertFalse(re.search('[가-힣]', v if isinstance(v, str) else ''.join(v.values())), k)


if __name__ == '__main__':
    unittest.main()
