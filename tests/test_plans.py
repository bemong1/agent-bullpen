"""Tests of the Claude limit-hit decision for the bottom plan bar (board/plans.py live_hits).

    python3 -m unittest discover -s tests
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server  # noqa: E402,F401
from board import plans  # noqa: E402
from board.plans import STATUSLINE_LIVE, live_hits, statusline_usage  # noqa: E402

HIT = {'status': 'rejected', 'resets_at': 2000.0, 'ts': 1000.0}


class LiveHits(unittest.TestCase):
    def test_reset_pass_clears_hit(self):
        # weekly usage rate 12% read at 1500, after the hit (1000): cleared by a reset pass → the hit record is not returned
        v = {'as_of': 1500.0, 'seven_day': {'percent': 12.0, 'resets_at': 2000.0}}
        self.assertEqual(live_hits({'seven_day': HIT}, v), {})

    def test_hit_stays_while_usage_is_full(self):
        v = {'as_of': 1500.0, 'seven_day': {'percent': 100.0, 'resets_at': 2000.0}}
        self.assertEqual(live_hits({'seven_day': HIT}, v), {'seven_day': HIT})

    def test_older_reading_does_not_clear(self):
        # a usage rate read before the hit (a cache, etc.) cannot overturn the hit
        v = {'as_of': 900.0, 'seven_day': {'percent': 40.0, 'resets_at': 2000.0}}
        self.assertEqual(live_hits({'seven_day': HIT}, v), {'seven_day': HIT})

    def test_no_reading_keeps_hit(self):
        self.assertEqual(live_hits({'seven_day': HIT}, {'as_of': None}), {'seven_day': HIT})
        self.assertEqual(live_hits({'seven_day': HIT}, {'as_of': 1500.0, 'seven_day': None}), {'seven_day': HIT})

    def test_windows_are_separate(self):
        # the weekly hit stays even when the 5-hour window clears
        v = {'as_of': 1500.0, 'five_hour': {'percent': 5.0}, 'seven_day': {'percent': 100.0}}
        got = live_hits({'five_hour': HIT, 'seven_day': HIT}, v)
        self.assertEqual(set(got), {'seven_day'})

    def test_does_not_share_the_index_dicts(self):
        q = {'seven_day': dict(HIT)}
        live_hits(q, {})['seven_day']['status'] = 'x'
        self.assertEqual(q['seven_day']['status'], 'rejected')


NOW = 1_800_000_000.0
FUTURE5, FUTURE7 = NOW + 3600, NOW + 86400


def status_doc(at=NOW - 20, five=(42.5, FUTURE5), seven=(18.0, FUTURE7), **top):
    rl = {}
    if five:
        rl['five_hour'] = {'used_percentage': five[0], 'resets_at': five[1]}
    if seven:
        rl['seven_day'] = {'used_percentage': seven[0], 'resets_at': seven[1]}
    return dict({'v': 1, 'at': at, 'rate_limits': rl}, **top)


class StatuslineFile(unittest.TestCase):
    """What is read from the file statusline.py leaves, and what is not trusted."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.dir.name, 'agent-bullpen')
        os.makedirs(self.folder, mode=0o700)
        self.path = os.path.join(self.folder, 'statusline.json')

    def tearDown(self):
        self.dir.cleanup()

    def put(self, doc, mode=0o600):
        with open(self.path, 'w') as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        os.chmod(self.path, mode)

    def read(self, now=NOW):
        return statusline_usage(self.path, now=now)

    def test_a_good_file(self):
        self.put(status_doc())
        self.assertEqual(self.read(), {'five_hour': {'percent': 42.5, 'resets_at': FUTURE5}, 'seven_day': {'percent': 18.0, 'resets_at': FUTURE7}, 'as_of': NOW - 20})

    def test_reset_time_in_text_seconds_or_milliseconds(self):
        want = 1_800_003_600
        for r in (want, want * 1000, float(want), want * 1000.0, '2027-01-15T09:00:00Z', '2027-01-15T09:00:00.000Z', '2027-01-15T18:00:00+09:00'):
            self.put(status_doc(five=(10, r), seven=None))
            got = self.read()
            self.assertEqual(got['five_hour']['resets_at'], want, r)
            self.assertEqual(got['seven_day'], None)

    def test_a_window_that_has_reset_has_no_percent(self):
        self.put(status_doc(five=(88, NOW - 5)))
        got = self.read()
        self.assertEqual(got['five_hour'], {'percent': None, 'resets_at': NOW - 5})   # not shown as 88, and not guessed as 0
        self.assertEqual(got['seven_day']['percent'], 18.0)

    def test_nothing_usable_when_every_window_has_reset(self):
        self.put(status_doc(five=(88, NOW - 5), seven=(50, NOW - 1)))
        self.assertIsNone(self.read())
        self.put(status_doc(five=None, seven=None))
        self.assertIsNone(self.read())

    def test_percent_is_kept_in_range(self):
        self.put(status_doc(five=(130, FUTURE5), seven=(-4, FUTURE7)))
        got = self.read()
        self.assertEqual((got['five_hour']['percent'], got['seven_day']['percent']), (100.0, 0.0))

    def test_wrong_shapes_are_refused(self):
        bad = [None, [], 'x', 3, {}, {'v': 2, 'at': NOW - 1, 'rate_limits': status_doc()['rate_limits']}, {'v': 1, 'rate_limits': status_doc()['rate_limits']},
               {'v': 1, 'at': 'now', 'rate_limits': status_doc()['rate_limits']}, {'v': 1, 'at': True, 'rate_limits': status_doc()['rate_limits']},
               {'v': 1, 'at': NOW + 4000, 'rate_limits': status_doc()['rate_limits']},                 # from the future
               {'v': 1, 'at': NOW - 1, 'rate_limits': []}, {'v': 1, 'at': NOW - 1, 'rate_limits': 'x'}, {'v': 1, 'at': NOW - 1},
               status_doc(five=(True, FUTURE5), seven=None), status_doc(five=('9', FUTURE5), seven=None), status_doc(five=(5, 'x'), seven=None),
               status_doc(five=(5, True), seven=None), status_doc(five=(5, [FUTURE5]), seven=None), status_doc(five=(5, 0), seven=None),
               '{"v":1,"at":NaN,"rate_limits":{"five_hour":{"used_percentage":1,"resets_at":1900000000}}}',
               '{"v":1,"at":1,"rate_limits":{"five_hour":{"used_percentage":NaN,"resets_at":1900000000}}}',
               '{"v":1,"at":%s,"rate_limits":{"five_hour":{"used_percentage":1,"resets_at":Infinity}}}' % (NOW - 1)]
        for doc in bad:
            self.put(doc)
            self.assertIsNone(self.read(), doc)

    def test_broken_files(self):
        for body in ('not json', '', '{"v":1', '[' * 100000, '{"a":' * 100000, '\xff\xfe'):
            self.put(body)
            self.assertIsNone(self.read(), body[:20])
        with open(self.path, 'wb') as f:
            f.write(b'\xff\xfe\x00')
        self.assertIsNone(self.read())

    def test_too_big_a_file_is_refused(self):
        doc = status_doc(pad='x' * plans.STATUSLINE_MAX)
        self.put(doc)
        self.assertIsNone(self.read())

    def test_a_file_or_folder_others_can_write_is_not_trusted(self):
        self.put(status_doc(), mode=0o666)
        self.assertIsNone(self.read())
        self.put(status_doc(), mode=0o664)
        self.assertIsNone(self.read())
        self.put(status_doc(), mode=0o644)
        self.assertIsNotNone(self.read())                              # read by others is fine; only writing matters
        os.chmod(self.folder, 0o777)
        self.assertIsNone(self.read())
        os.chmod(self.folder, 0o770)
        self.assertIsNone(self.read())
        os.chmod(self.folder, 0o700)
        self.assertIsNotNone(self.read())

    def test_a_file_of_another_owner_is_not_trusted(self):
        self.put(status_doc())
        st = os.stat(self.path)
        with mock.patch.object(plans.lineage.os, 'geteuid', return_value=st.st_uid + 1):
            self.assertIsNone(self.read())

    def test_a_link_is_not_followed(self):
        real = os.path.join(self.dir.name, 'real.json')
        with open(real, 'w') as f:
            json.dump(status_doc(), f)
        os.chmod(real, 0o600)
        os.symlink(real, self.path)
        self.assertIsNone(self.read())

    def test_a_fifo_or_folder_is_not_read_and_does_not_block(self):
        os.mkfifo(self.path)
        self.assertIsNone(self.read())
        os.unlink(self.path)
        os.mkdir(self.path)
        self.assertIsNone(self.read())

    def test_absent_file(self):
        self.assertIsNone(self.read())
        self.assertIsNone(statusline_usage(os.path.join(self.dir.name, 'nowhere', 'statusline.json'), now=NOW))


class ClaudePlanSources(unittest.TestCase):
    """The order of the sources of the Claude usage: the status line file, then the ~/.claude.json cache."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.dir.name, 'agent-bullpen')
        os.makedirs(self.folder, mode=0o700)
        self.sl = os.path.join(self.folder, 'statusline.json')
        self.cj = os.path.join(self.dir.name, '.claude.json')
        self.write_cache(as_of=NOW - 3000, five=(30.0, FUTURE5), seven=(10.0, FUTURE7))
        stack = [patched(STATUSLINE_STATE=self.sl, CLAUDE_JSON=[self.cj]),
                 mock.patch.dict(plans._CL_CONF, {'key': None, 'v': None}), mock.patch.object(plans.LINKS, 'quota', {})]
        for m in stack:
            m.__enter__()
            self.addCleanup(m.__exit__, None, None, None)

    def tearDown(self):
        self.dir.cleanup()

    def write_cache(self, as_of, five, seven):
        util = {'five_hour': {'utilization': five[0], 'resets_at': '2027-01-15T09:00:00+00:00'}, 'seven_day': {'utilization': seven[0], 'resets_at': '2027-01-16T09:00:00+00:00'},
                'extra_usage': {'is_enabled': False, 'disabled_reason': 'out_of_credits'}}
        with open(self.cj, 'w') as f:
            json.dump({'oauthAccount': {'userRateLimitTier': 'default_claude_max_5x', 'billingType': 'stripe_subscription', 'emailAddress': 'secret@example.com'},
                       'cachedUsageUtilization': {'fetchedAtMs': as_of * 1000, 'utilization': util}}, f)
        plans._CL_CONF.update(key=None, v=None)

    def put_status(self, doc, mode=0o600):
        with open(self.sl, 'w') as f:
            json.dump(doc, f)
        os.chmod(self.sl, mode)

    def plan(self):
        return plans.claude_plan(now=NOW)

    def test_without_the_file_nothing_changes(self):
        got = self.plan()
        self.assertEqual((got['source'], got['five_hour']['percent'], got['as_of']), ('cache', 30.0, NOW - 3000))
        self.assertEqual(got['plan'], 'Max 5x')
        self.assertEqual(set(got) & {'usage_api', 'error', 'error_info'}, set())      # the fields of the removed account query are gone

    def test_the_status_line_comes_first(self):
        self.put_status(status_doc())
        got = self.plan()
        self.assertEqual((got['source'], got['as_of']), ('statusline', NOW - 20))
        self.assertEqual((got['five_hour'], got['seven_day']), ({'percent': 42.5, 'resets_at': FUTURE5}, {'percent': 18.0, 'resets_at': FUTURE7}))
        self.assertEqual(got['plan'], 'Max 5x')                         # the plan and the extra-usage state still come from ~/.claude.json
        self.assertEqual(got['extra']['reason'], 'out_of_credits')
        self.assertNotIn('secret@example.com', json.dumps(got))

    def test_the_status_line_wins_over_a_newer_cache_while_it_is_recent(self):
        self.put_status(status_doc(at=NOW - 100))
        self.write_cache(as_of=NOW - 10, five=(55.0, FUTURE5), seven=(25.0, FUTURE7))
        got = self.plan()
        self.assertEqual((got['source'], got['five_hour']['percent']), ('statusline', 42.5))

    def test_the_status_line_does_not_hold_back_a_newer_cache_once_it_is_old(self):
        self.put_status(status_doc(at=NOW - STATUSLINE_LIVE - 1))
        self.write_cache(as_of=NOW - 60, five=(61.0, FUTURE5), seven=(11.0, FUTURE7))                   # /usage was opened after the status line stopped
        got = self.plan()
        self.assertEqual((got['source'], got['five_hour']['percent'], got['as_of']), ('cache', 61.0, NOW - 60))

    def test_an_old_status_line_is_still_used_when_nothing_is_newer(self):
        self.put_status(status_doc(at=NOW - 5 * STATUSLINE_LIVE))
        got = self.plan()
        self.assertEqual((got['source'], got['as_of']), ('statusline', NOW - 5 * STATUSLINE_LIVE))
        self.write_cache(as_of=NOW - 6 * STATUSLINE_LIVE, five=(61.0, FUTURE5), seven=(11.0, FUTURE7))
        self.assertEqual(self.plan()['source'], 'statusline')

    def test_the_cache_is_used_when_there_is_no_usable_status_line(self):
        self.assertEqual(self.plan()['source'], 'cache')                       # no file
        self.put_status(status_doc(five=(80, NOW - 10), seven=(50, NOW - 10)))
        self.assertEqual(self.plan()['source'], 'cache')                       # every window has reset
        self.put_status(status_doc(), mode=0o666)
        self.assertEqual(self.plan()['source'], 'cache')                       # a file others can write
        self.put_status(status_doc(at=NOW + 10000))
        self.assertEqual(self.plan()['source'], 'cache')                       # from the future

    def test_a_window_that_has_reset_is_not_shown_as_a_percent(self):
        self.put_status(status_doc(five=(88, NOW - 5)))
        got = self.plan()
        self.assertEqual(got['source'], 'statusline')
        self.assertEqual(got['five_hour'], {'percent': None, 'resets_at': NOW - 5})
        self.assertEqual(got['seven_day']['percent'], 18.0)

    def test_the_status_line_reading_clears_an_old_limit_hit(self):
        with mock.patch.object(plans.LINKS, 'quota', {'five_hour': {'status': 'rejected', 'resets_at': NOW + 100, 'ts': NOW - 500}}):
            self.put_status(status_doc(at=NOW - 20, five=(3.0, FUTURE5)))
            self.assertEqual(self.plan()['hits'], {})                                  # read after the hit and under 100%
            self.put_status(status_doc(at=NOW - 20, five=(100.0, FUTURE5)))
            self.assertIn('five_hour', self.plan()['hits'])

    def test_it_works_without_the_cache_file(self):
        os.unlink(self.cj)
        self.assertIsNone(self.plan())
        self.put_status(status_doc())
        got = self.plan()
        self.assertEqual((got['source'], got['five_hour']['percent']), ('statusline', 42.5))
        self.assertNotIn('plan', got)

    def test_the_answer_of_the_page_carries_the_source_and_time(self):
        self.put_status(status_doc())
        stub = mock.Mock()
        stub.limit.return_value = None
        with mock.patch.object(plans, 'CODEX', stub), mock.patch.object(plans.time, 'time', return_value=NOW):
            got = json.loads(json.dumps(plans.plan_status()))
        self.assertEqual((got['claude']['source'], got['claude']['as_of']), ('statusline', NOW - 20))


if __name__ == '__main__':
    unittest.main()
