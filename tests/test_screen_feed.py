"""What the server hands the screen: the system lines, the limit alert, the diagnostics and the session list (the screen itself is checked by tools/regress/state_checks.js and office_checks.js).

- the system lines of the feed (kind `sys`): an API error line of the record, and the notices that follow a usage limit, become events with a dictionary key and numbers
  (never the line's own words) and are no longer anything the orchestrator said
- the grouped limit alert names its sentence by key (four of them), the time is a number
- the diagnostics of the orchestrator's own judgment join the list once
- the session list counts a grandchild, the first picture is worked out in the background right after a session is opened
- an agent's API error line is not something the agent said

    python3 -m unittest tests.test_screen_feed
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402
from test_server_i18n import EN, KO, SessionCase, human, say  # noqa: E402
import test_stage2 as st2  # noqa: E402
from board import catalog, diag, runstate as RS, views  # noqa: E402

T0 = st2.T0


def api_line(t, status, kind, resets=None, text='Synthetic API error line.'):
    d = {'type': 'assistant', 'timestamp': st2.iso(t), 'isApiErrorMessage': True, 'error': kind,
         'message': {'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence', 'content': [{'type': 'text', 'text': text}]}}
    if status:
        d['apiErrorStatus'] = status
    if resets:
        d['quotaLimits'] = {'status': 'rejected', 'resetsAt': resets, 'rateLimitType': 'five_hour'}
    return d


def notice(t, text):
    return {'type': 'system', 'subtype': 'informational', 'timestamp': st2.iso(t), 'content': text}


def sys_events(s):
    return [e for e in s.feed if e['kind'] == 'sys']


class SysLines(SessionCase):
    def test_a_limit_line_with_its_notice_is_one_system_line_that_continues_by_itself(self):
        s = self.claude()
        s._feed_main(human('go on'))
        s._feed_main(api_line(T0 + 10, 429, 'rate_limit', resets=int(T0 + 7200)))
        (e,) = sys_events(s)
        self.assertEqual((e['kind'], e['from'], e['to'], e['text']), ('sys', 'sys', 'user', ''))
        self.assertEqual(e['sys'], {'code': 'limit', 'status': 429, 'resets_at': T0 + 7200, 'auto': False})
        self.assertEqual(e['title_i18n'], {'key': 'event.sys.limit', 'params': {'at': T0 + 7200}})
        self.assertEqual(e['title_is_default'], True)
        s._feed_main(notice(T0 + 10.1, 'Usage limit reached · continuing automatically at 10:40am · esc or type to cancel'))
        self.assertEqual(len(sys_events(s)), 1)                                      # the notice amends the line before it
        self.assertEqual((e['sys']['auto'], e['title_i18n']['key'], e['title_i18n']['params']), (True, 'event.sys.limit.auto', {'at': T0 + 7200}))
        self.assertTrue(e['title'].startswith('사용 한도 도달 · ') and e['title'].endswith(' 자동 재개'), e['title'])      # the old Korean field says the same
        s._feed_main(notice(T0 + 7200, 'Usage limit reset · continuing automatically'))
        self.assertEqual([x['sys']['code'] for x in sys_events(s)], ['limit', 'reset'])
        self.assertEqual(sys_events(s)[1]['title_i18n']['key'], 'event.sys.reset')

    def test_a_notice_long_after_the_line_does_not_amend_it(self):
        s = self.claude()
        s._feed_main(api_line(T0, 429, 'rate_limit'))
        s._feed_main(notice(T0 + 600, 'Usage limit reached · continuing automatically at 10:40am'))
        (e,) = sys_events(s)
        self.assertEqual((e['sys']['auto'], e['title_i18n']['key']), (False, 'event.sys.limit.noTime'))      # no time is made up

    def test_the_other_errors(self):
        s = self.claude()
        for t, status, kind in ((T0, 529, 'server_error'), (T0 + 1, 400, 'invalid_request'), (T0 + 2, None, 'authentication_failed'), (T0 + 3, None, 'server_error')):
            s._feed_main(api_line(t, status, kind))
        got = [(e['sys']['code'], e['title_i18n']['key'], e['title_i18n']['params'], e['title']) for e in sys_events(s)]
        self.assertEqual(got, [('api_error', 'event.sys.api_error', {'status': 529}, 'API 오류 529 · 재시도 가능'),
                               ('rejected', 'event.sys.rejected', {'status': 400}, 'API 오류 400 · 요청 거부됨'),
                               ('auth', 'event.sys.auth', {}, '로그인 만료 · 다시 로그인 필요'),
                               ('api_error', 'event.sys.api_error.noStatus', {}, 'API 오류 · 재시도 가능')])

    def test_none_of_it_is_the_orchestrator_speaking(self):
        s = self.claude()
        s._feed_main(say('A real report.'))
        s._feed_main(api_line(T0, 429, 'rate_limit', resets=int(T0 + 60), text="You've hit your limit"))
        self.assertEqual([e['text'] for e in s.feed if e['kind'] == 'orch_say'], ['A real report.'])
        self.assertEqual(s.orch['last_say'], 'A real report.')
        self.assertFalse(any("hit your limit" in (e.get('text') or '') + (e.get('title') or '') for e in s.feed))      # the line's own words are nowhere in the feed

    def test_a_time_that_is_not_a_time_is_left_out(self):
        s = self.claude()
        s._feed_main(api_line(T0, 429, 'rate_limit', resets=10 ** 18))
        (e,) = sys_events(s)
        self.assertEqual(e['title_i18n']['key'], 'event.sys.limit.noTime')

    def test_every_key_exists_in_both_languages_and_the_korean_says_the_old_title(self):
        s = self.claude()
        at = T0 + 7200
        for code, status, when, auto in (('limit', 429, at, False), ('limit', 429, at, True), ('limit', 429, None, False), ('limit', 429, None, True), ('reset', None, None, False),
                                         ('api_error', 529, None, False), ('api_error', None, None, False), ('rejected', 400, None, False), ('rejected', None, None, False), ('auth', None, None, False)):
            key, params, ko = views.sys_line(code, status, when, auto)
            self.assertIn(key, EN, key)
            self.assertIn(key, KO, key)
            filled = KO[key]
            for k, v in params.items():
                filled = filled.replace('{%s}' % k, '%s' % v)
            if 'at' in params:                                                       # the dictionary words the time in the page; the old field has it as month/day hour:minute
                self.assertEqual(KO[key].replace('{at}', views._when(params['at'])), ko)
            else:
                self.assertEqual(filled, ko)


class LimitAlert(unittest.TestCase):
    def alert(self, resets, auto):
        s = server.Session('/nonexistent/aaaaaaaa-0000-4000-8000-000000000000.jsonl')
        s._verdicts = {}
        s._orch_verdict = RS.OrchVerdict('limit_wait', resets, auto)
        s.orch['last_ts'] = T0
        (x,) = [a for a in views.alerts(s, {}, T0 + 60) if a['id'].startswith('limit:')]
        return x

    def test_the_four_sentences_are_named_by_key(self):
        for resets, auto, key in ((T0 + 7200, False, 'alert.limit.title'), (T0 + 7200, True, 'alert.limit.auto.title'), (None, False, 'alert.limit.noTime.title'), (None, True, 'alert.limit.noTime.auto.title')):
            x = self.alert(resets, auto)
            self.assertEqual(x['title_i18n']['key'], key)
            self.assertEqual(x['title_i18n']['params'], {'at': resets} if resets else {})
            self.assertEqual(x['title_params'], {'at': resets, 'auto': auto, 'agents': 0, 'orch': True})
            self.assertIn(key, EN)
            self.assertIn(key, KO)
            self.assertEqual(x['level'], 'note' if auto else 'check')                     # a notice that needs nothing from the user is a note, not "your turn"
            self.assertIn('board.alert.level.' + x['level'], EN)
            self.assertIn('board.alert.level.' + x['level'], KO)
            expect = KO[key].replace('{at}', views._when(resets)) if resets else KO[key]
            self.assertEqual(x['title'], expect)                                       # the old Korean field and the Korean dictionary agree

    def test_an_unusable_time_is_no_time(self):
        x = self.alert(10 ** 18, False)
        self.assertEqual((x['title_i18n']['key'], x['title_i18n']['params']), ('alert.limit.noTime.title', {}))


class OrchDiag(unittest.TestCase):
    def test_the_orchestrators_process_diagnostic_joins_the_list_once_in_order(self):
        entries = [diag._entry('not_resumed', 'agent', 'a1'), diag._entry('content_only', 'agent', 'a2')]
        ov = RS.OrchVerdict('idle', diag=(('proc_unknown', {}),))
        out = views._with_orch_diag(entries, ov)
        self.assertEqual([(e['code'], e['scope']) for e in out], [('not_resumed', 'agent'), ('content_only', 'agent'), ('proc_unknown', 'orch')])
        self.assertEqual(out[-1], diag._entry('proc_unknown', 'orch', None))
        self.assertEqual(len(views._with_orch_diag(out, ov)), 3)                         # an entry of the orchestrator that is there already is not added again
        self.assertIs(views._with_orch_diag(entries, RS.OrchVerdict('idle')), entries)

    def test_the_cap_holds(self):
        entries = [diag._entry('not_resumed', 'agent', 'a%03d' % i) for i in range(diag.MAX_PER_SESSION)]
        out = views._with_orch_diag(entries, RS.OrchVerdict('idle', diag=(('proc_unknown', {}),)))
        self.assertEqual(len(out), diag.MAX_PER_SESSION)


class Catalog(unittest.TestCase):
    def owners(self, edges):
        return {c: {'sid': p} for c, p in edges}

    def test_a_grandchild_is_counted_with_its_launchers_tree(self):
        with mock.patch.object(catalog.LINKS, 'cli_owners', self.owners([('c1', 'top'), ('c2', 'top'), ('g1', 'c1'), ('gg1', 'g1'), ('x1', 'other')])):
            self.assertEqual(catalog.cli_totals(), {'top': 4, 'c1': 2, 'g1': 1, 'other': 1})

    def test_a_loop_and_the_depth_limit(self):
        with mock.patch.object(catalog.LINKS, 'cli_owners', self.owners([('a', 'a0'), ('b', 'a'), ('a0', 'b')])):
            self.assertEqual(catalog.cli_totals()['a0'], 2)                              # never counts itself, never counts one twice
        chain = [('n%d' % i, 'n%d' % (i - 1) if i else 'root') for i in range(12)]
        with mock.patch.object(catalog.LINKS, 'cli_owners', self.owners(chain)):
            self.assertEqual(catalog.cli_totals()['root'], catalog.MAX_NEST + 1)         # the page follows MAX_NEST levels beyond the first

    def test_the_session_list_says_the_number_the_page_will_show(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, 'projects', 'p')
            os.makedirs(proj)
            top = 'aaaaaaaa-0000-4000-8000-000000000001'
            with open(os.path.join(proj, top + '.jsonl'), 'w') as f:
                f.write('{"type":"user","timestamp":"%s","cwd":"/w","message":{"role":"user","content":"hi"}}\n' % st2.iso(time.time()))
            owners = self.owners([('c1', top), ('g1', 'c1')])
            with patched(PROJECTS=os.path.join(tmp, 'projects')), mock.patch.object(catalog.LINKS, 'cli_owners', owners):
                items = [x for x in catalog.list_sessions() if x['id'] == top]
            self.assertEqual([x['agents'] for x in items], [2])

    def test_a_session_is_worked_out_in_the_background_when_it_is_registered(self):
        calls = []
        s = mock.Mock(id='aaaaaaaa-0000-4000-8000-000000000002')
        with mock.patch.object(views, 'state', lambda x: calls.append(x)):
            catalog.Registry._warm(s)
        self.assertEqual(calls, [s])
        with mock.patch.object(views, 'state', side_effect=RuntimeError('boom')), mock.patch('builtins.print') as pr:
            catalog.Registry._warm(s)                                                    # a failure never reaches the caller
        self.assertTrue(pr.called)


class AgentApiError(unittest.TestCase):
    def test_the_line_of_an_api_error_is_not_what_the_agent_said(self):
        a = server.Agent('a%016x' % 7, {'description': 'T1-A research'})
        a.feed(api_line(T0, 429, 'rate_limit', resets=int(T0 + 60), text="You've hit your limit"))
        self.assertEqual((list(a.texts), list(a.activity)), ([], []))
        a.feed({'type': 'assistant', 'timestamp': st2.iso(T0 + 5), 'message': {'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': None,
                                                                              'content': [{'type': 'text', 'text': 'Real words.'}]}})
        self.assertEqual([x['text'] for x in a.texts], ['Real words.'])


class TalkKinds(unittest.TestCase):
    def test_the_user_card_carries_the_system_lines(self):
        self.assertIn('sys', server.TALK_KINDS['user'])
        self.assertNotIn('sys', server.TALK_KINDS['agents'])


if __name__ == '__main__':
    unittest.main()
