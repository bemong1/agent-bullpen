"""tools/regress/api_snap.py: API_SNAP_ALLOW accepts exactly the new fields of the language edition and the status and link work, and nothing else.

An old answer (A) and a new answer (B) are compared on synthetic data: the new fields in their places do not count, but a changed old field, a field that is
new in another place, a removed field, a changed value or length, and the old Korean fields are still differences. `compare` runs on a fake `get` (no server).

    python3 -m unittest discover -s tests
"""
import contextlib
import copy
import io
import json
import os
import sys
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'regress'))
import api_snap  # noqa: E402


def event(kind='orch_say', **extra):
    return dict({'ts': 5.0, 'kind': kind, 'from': 'orch', 'to': 'user', 'title': '사용자에게 보고', 'text': 'hello', 'agent': None, 'idx': 3}, **extra)


NEW_EVENT = {'title_i18n': {'key': 'event.orch_say.title', 'params': {}}, 'title_is_default': True}


def alert(**extra):
    return dict({'id': 'say:1', 'level': 'decide', 'title': '오케스트레이터가 답을 기다립니다', 'text': 'Shall I go ahead?', 'ts': 1.0, 'agent': None}, **extra)


def strip(old, new, endpoint='api/state'):
    used = Counter()
    return api_snap.strip_new(old, new, endpoint, dropped=used), used


class Allowed(unittest.TestCase):
    def assert_accepted(self, old, new, endpoint='api/state', rule=None):
        out, used = strip(old, new, endpoint)
        self.assertEqual(api_snap.norm(out), api_snap.norm(old), (out, old))
        self.assertTrue(used if rule is None else used[rule], used)

    def test_event_fields_in_the_state_feed_the_talk_items_and_one_event(self):
        old, new = event(), event(**NEW_EVENT)
        self.assert_accepted({'feed': [old, old]}, {'feed': [new, new]}, rule='event title_i18n / title_is_default / questions')
        self.assert_accepted({'items': [old], 'more': False}, {'items': [new], 'more': False}, 'api/talk')
        self.assert_accepted(old, new, 'api/event')
        ask = event('orch_ask', questions=[{'header': None, 'question': 'Which?', 'options': []}])
        self.assert_accepted(event('orch_ask'), dict(ask, **NEW_EVENT))

    def test_notify_status_only_on_a_notify_event(self):
        self.assert_accepted(event('notify'), event('notify', status='done'), rule='notify event status')
        out, used = strip(event('orch_say'), event('orch_say', status='done'))
        self.assertIn('status', out)                                                # another kind of event: the new key is a difference
        self.assertFalse(used)

    def test_agent_status_collision_stays_a_difference(self):
        old = {'agents': [{'id': 'a1', 'title': 'T1', 'tokens': {'models': {}}}]}
        new = {'agents': [{'id': 'a1', 'title': 'T1', 'tokens': {'models': {}, 'model_costs': []}, 'status': 'running'}]}   # status on an agent is an old field
        out, used = strip(old, new)
        self.assertEqual(out['agents'][0].get('status'), 'running')
        self.assertEqual(list(used), ['tokens model_costs'])
        self.assertNotEqual(api_snap.norm(out), api_snap.norm(old))

    def test_alert_fields(self):
        extra = {'title_i18n': {'key': 'alert.say.title', 'params': {}}, 'text_i18n': {'key': 'alert.cxlimit.text', 'params': {'as_of': None, 'resets_at': None}}}
        self.assert_accepted({'alerts': [alert()]}, {'alerts': [alert(**extra)]}, rule='alert title_i18n / text_i18n')

    def test_tokens_model_costs_in_the_orchestrator_and_in_agents(self):
        old = {'orch': {'tokens': {'models': {'m': 1}}}, 'agents': [{'tokens': {'models': {'m 조언': 2}}}]}
        new = {'orch': {'tokens': {'models': {'m': 1}, 'model_costs': [{'model': 'm', 'advisor': False, 'cost': 1}]}},
               'agents': [{'tokens': {'models': {'m 조언': 2}, 'model_costs': [{'model': 'm', 'advisor': True, 'cost': 2}]}}]}
        self.assert_accepted(old, new, rule='tokens model_costs')

    def test_plans_error_info_only_at_claude_of_api_plans(self):
        old, new = {'claude': {'error': None}, 'codex': None}, {'claude': {'error': None, 'error_info': None}, 'codex': None}
        self.assert_accepted(old, new, 'api/plans', rule='plans claude.error_info')
        out, used = strip({'claude': {'error': None}}, {'claude': {'error': None, 'error_info': None}}, 'api/state')       # the same shape in another answer
        self.assertIn('error_info', out['claude'])
        out, used = strip({'x': {'error': None}}, {'x': {'error': None, 'error_info': None}}, 'api/plans')                  # another place of api/plans
        self.assertIn('error_info', out['x'])

    def test_error_code_only_at_the_top_of_an_error_body(self):
        self.assert_accepted({'error': 'no event'}, {'error': 'no event', 'error_code': 'no_event'}, 'api/event', rule='JSON error error_code')
        out, used = strip({'a': {'error': 'x'}}, {'a': {'error': 'x', 'error_code': 'c'}})
        self.assertIn('error_code', out['a'])
        out, used = strip({'ok': 1}, {'ok': 1, 'error_code': 'c'})                                                         # not an error body
        self.assertIn('error_code', out)


def agent_dict(**extra):
    return dict({'id': 'a1', 'title': 'T1', 'status': 'done', 'tokens': {'models': {}}, 'link': {'rule': 'subagent', 'certain': True}}, **extra)


NEW_AGENT = {'reason': None, 'resets_at': None, 'node': None, 'by': None, 'parent': None, 'runs': [], 'work_units': []}


class StatusFields(unittest.TestCase):
    """What the status and link work adds to /api/state: accepted where it is added, a difference anywhere else."""

    def test_agent_fields_and_the_link_keys(self):
        old = {'agents': [agent_dict()], 'orch': {'state': 'idle', 'tokens': {}}, 'session': {}}
        new = {'agents': [dict(agent_dict(), **NEW_AGENT, link={'rule': 'subagent', 'certain': True, 'rule_class': 'certain', 'incomplete': False, 'assumed': False, 'tree': 's'})],
               'orch': {'state': 'idle', 'tokens': {}, 'resets_at': None, 'auto': False}, 'session': {}, 'diag': {'n': 0, 'warn': 0, 'info': 0, 'by_code': {}, 'capped': False}}
        out, used = strip(old, new)
        self.assertEqual(api_snap.norm(out), api_snap.norm(old))
        self.assertEqual(set(used), {'agent reason / resets_at / node / by / parent / runs / work_units', 'agent link rule_class / incomplete / assumed / tree', 'orch resets_at / auto (limit_wait)', 'state diag'})

    def test_the_dictionary_key_of_a_tool_summary(self):
        tool = {'ts': 5.0, 'kind': 'tool', 'name': 'SubagentHandback', 'text': '최종 보고 전달', 'cat': 'msg'}
        key = {'text_i18n': {'key': 'event.tool.handback', 'params': {}}}
        old = {'agents': [dict(agent_dict(), current=tool, last_tool=tool, pending={'ts': 5.0, 'name': 'SubagentHandback', 'text': '최종 보고 전달'})]}
        new = {'agents': [dict(agent_dict(), current=dict(tool, **key), last_tool=dict(tool, **key), pending={'ts': 5.0, 'name': 'SubagentHandback', 'text': '최종 보고 전달', **key})]}
        out, used = strip(old, new)
        self.assertEqual(api_snap.norm(out), api_snap.norm(old))
        self.assertEqual(set(used), {'tool text_i18n (activity, last_tool, current, pending)'})
        out, _ = strip({'a': {'x': 1}}, {'a': {'x': 1, **key}})                                   # the same name on something that is no tool entry stays a difference
        self.assertNotEqual(api_snap.norm(out), api_snap.norm({'a': {'x': 1}}))
        changed = dict(tool, text='another text', **key)                                         # a changed old field is still a difference
        out, _ = strip({'agents': [dict(agent_dict(), current=tool)]}, {'agents': [dict(agent_dict(), current=changed)]})
        self.assertNotEqual(api_snap.norm(out), api_snap.norm({'agents': [dict(agent_dict(), current=tool)]}))

    def test_the_limit_alert_params(self):
        extra = {'title_i18n': {'key': 'alert.limit.title', 'params': {'at': 5}}, 'title_params': {'at': 5, 'auto': False, 'agents': 1, 'orch': False}}
        self.assertTrue(strip({'alerts': [alert()]}, {'alerts': [alert(**extra)]})[1])
        out, _ = strip({'alerts': [alert()]}, {'alerts': [alert(**extra)]})
        self.assertEqual(api_snap.norm(out), api_snap.norm({'alerts': [alert()]}))

    def test_the_same_names_in_other_places_are_differences(self):
        for old, new in (({'x': {'tokens': {}, 'status': 'a'}}, {'x': {'tokens': {}, 'status': 'a', 'reason': None}}),                      # not an element of agents[]
                         ({'agents': [{'id': 'a'}]}, {'agents': [{'id': 'a', 'parent': None}]}),                                           # an agent without tokens/status is not the state's
                         ({'a': {'rule': 'r', 'certain': True}}, {'a': {'rule': 'r', 'certain': True, 'tree': 's'}}),                       # not a link
                         ({'orch': {'state': 'idle'}}, {'orch': {'state': 'idle', 'auto': True}}),                                          # an orch without tokens
                         ({'a': {'b': 1}}, {'a': {'b': 1}, 'diag': {}})):                                                                  # not the top of a state answer
            out, _ = strip(old, new)
            self.assertNotEqual(api_snap.norm(out), api_snap.norm(old), new)

    def test_a_new_value_of_an_old_field_and_a_system_line_stay_differences(self):
        old = {'agents': [agent_dict(status='ended')], 'orch': {'state': 'idle', 'tokens': {}}, 'session': {}}
        new = {'agents': [dict(agent_dict(status='interrupted'), **NEW_AGENT)], 'orch': {'state': 'idle', 'tokens': {}}, 'session': {}}
        out, _ = strip(old, new)
        self.assertNotEqual(api_snap.norm(out), api_snap.norm(old))                              # status ended -> interrupted is the intended change, and a DIFF
        out, _ = strip({'feed': [event()]}, {'feed': [event(), event('sys', title='사용 한도 도달', sys={'code': 'limit'})]})
        self.assertNotEqual(api_snap.norm(out), api_snap.norm({'feed': [event()]}))                # a system line makes the feed longer


class StillDifferences(unittest.TestCase):
    def differs(self, old, new, endpoint='api/state'):
        out, _ = strip(old, new, endpoint)
        return api_snap.norm(out) != api_snap.norm(old)

    def test_a_changed_old_field_with_the_new_fields_added(self):
        self.assertTrue(self.differs({'feed': [event()]}, {'feed': [event(title='사용자 지시', **NEW_EVENT)]}))
        self.assertTrue(self.differs({'feed': [event()]}, {'feed': [event(text='bye', **NEW_EVENT)]}))
        self.assertTrue(self.differs({'alerts': [alert()]}, {'alerts': [alert(text='changed', title_i18n={'key': 'k', 'params': {}})]}))

    def test_a_new_field_that_the_table_does_not_name(self):
        self.assertTrue(self.differs({'feed': [event()]}, {'feed': [event(note='x', **NEW_EVENT)]}))
        self.assertTrue(self.differs({'a': 1}, {'a': 1, 'title_i18n': {}}))                    # not an event, not an alert
        self.assertTrue(self.differs({'orch': {'tokens': {'models': {}}}}, {'orch': {'tokens': {'models': {}, 'advisor': 1}}}))

    def test_an_old_field_that_went_missing(self):
        self.assertTrue(self.differs({'feed': [event()]}, {'feed': [{k: v for k, v in event(**NEW_EVENT).items() if k != 'agent'}]}))

    def test_a_key_the_old_answer_has_is_never_ignored(self):
        old = event(**NEW_EVENT)                                                                # the old side already has title_i18n: a changed value is a difference
        new = event(title_i18n={'key': 'other', 'params': {}}, title_is_default=True)
        self.assertTrue(self.differs({'feed': [old]}, {'feed': [new]}))

    def test_another_length_or_a_dropped_list(self):
        self.assertTrue(self.differs({'feed': [event()]}, {'feed': [event(**NEW_EVENT), event(**NEW_EVENT)]}))
        self.assertTrue(self.differs({'feed': [event(), event()]}, {'feed': [event(**NEW_EVENT)]}))
        self.assertTrue(self.differs({'feed': [event()]}, {'feed': None}))

    def test_nothing_is_accepted_without_the_table(self):
        out = api_snap.strip_new(event(), event(**NEW_EVENT), 'api/state', allow=[])
        self.assertEqual(sorted(set(out) - set(event())), ['title_i18n', 'title_is_default'])

    def test_the_input_is_not_changed(self):
        old, new = {'feed': [event()]}, {'feed': [event(**NEW_EVENT)]}
        before = copy.deepcopy(new)
        api_snap.strip_new(old, new, 'api/state')
        self.assertEqual(new, before)


class Compare(unittest.TestCase):
    """compare(): the sandwich A, B, A, the status codes, the notes it prints."""

    def run_compare(self, answers, path='api/state?session=s', **kw):
        calls = []

        def get(port, p):
            calls.append((port, p))
            r = answers[port]
            r = r.pop(0) if isinstance(r, list) else r
            return r

        out = io.StringIO()
        with mock.patch.object(api_snap, 'get', get), contextlib.redirect_stdout(out):
            ok = api_snap.compare('A', 'B', path, wait=0, **kw)
        return ok, out.getvalue(), calls

    def test_the_new_fields_are_accepted_and_counted(self):
        used = Counter()
        ok, out, calls = self.run_compare({'A': (200, {'feed': [event()], 'now': 1}), 'B': (200, {'feed': [event(**NEW_EVENT)], 'now': 2})}, dropped=used)
        self.assertTrue(ok)
        self.assertTrue(out.startswith('ok '), out)
        self.assertEqual(used['event title_i18n / title_is_default / questions'], 2)
        self.assertEqual([c[0] for c in calls], ['A', 'B', 'A'])

    def test_a_status_code_that_differs(self):
        ok, out, _ = self.run_compare({'A': (200, {'x': 1}), 'B': (404, {'x': 1})}, tries=1)
        self.assertFalse(ok)
        self.assertTrue(out.startswith('DIFF '), out)

    def test_a_difference_names_the_first_place_after_the_accepted_fields_are_gone(self):
        ok, out, _ = self.run_compare({'A': (200, {'feed': [event()]}), 'B': (200, {'feed': [event(text='bye', **NEW_EVENT)]})}, tries=1)
        self.assertFalse(ok)
        self.assertIn('first diff at /feed[0]/text', out)

    def test_data_that_moves_between_the_two_reads_of_A_is_matched_by_either(self):
        a1, a2, b = (200, {'v': 1}), (200, {'v': 2}), (200, {'v': 2})
        ok, out, _ = self.run_compare({'A': [a1, a2], 'B': [b]}, tries=1)
        self.assertTrue(ok, out)

    def test_the_error_body_of_a_403(self):
        ok, out, _ = self.run_compare({'A': (403, {'error': 'denied'}), 'B': (403, {'error': 'denied', 'error_code': 'not_allowed'})}, path='api/file?path=/etc/hostname')
        self.assertTrue(ok, out)


class Script(unittest.TestCase):
    def test_the_table_names_every_field_that_the_api_adds(self):
        keys = {k for _, _, ks in api_snap.API_SNAP_ALLOW for k in ks}
        self.assertEqual(keys, {'title_i18n', 'title_is_default', 'questions', 'status', 'text_i18n', 'title_params', 'model_costs', 'error_info', 'error_code',      # the language work (+ the limit alert's params)
                                'reason', 'resets_at', 'node', 'by', 'parent', 'runs', 'work_units', 'rule_class', 'incomplete', 'assumed', 'tree', 'auto', 'diag'})         # the status and link work

    def test_raw_mode_accepts_nothing_and_the_default_mode_accepts_the_table(self):
        answers = {'A': (200, {'feed': [event()]}), 'B': (200, {'feed': [event(**NEW_EVENT)]})}      # every path answers like this (api/sessions, api/plans)
        with mock.patch.object(api_snap, 'get', lambda port, p: answers[port]), mock.patch.object(api_snap.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(api_snap.main(['--raw', 'A', 'B']), 1)
        self.assertIn('FAILED 2', out.getvalue())
        self.assertNotIn('accepted new fields', out.getvalue())
        with mock.patch.object(api_snap, 'get', lambda port, p: answers[port]), mock.patch.object(api_snap.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(api_snap.main(['A', 'B']), 0)
        self.assertIn('accepted new fields: event title_i18n / title_is_default / questions x4', out.getvalue())
        self.assertIn('ALL OK', out.getvalue())

if __name__ == '__main__':
    unittest.main()
