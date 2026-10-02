"""Tests of the Claude limit-hit decision for the bottom plan bar (board/plans.py live_hits).

    python3 -m unittest discover -s tests
"""
import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402,F401
from board.plans import USAGE_EVERY, ClaudeUsage, live_hits  # noqa: E402

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



class UsageState(unittest.TestCase):
    """429 right after a restart: the previous server's query time is carried over so the API is not called again within a minute, and the last value is shown right away."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'sub', 'usage.json')

    def tearDown(self):
        self.dir.cleanup()

    def test_first_poll_without_state_is_immediate(self):
        u = ClaudeUsage(self.path)
        u.load()
        self.assertEqual(u.wait(now=1000.0), 0.0)

    def test_restart_waits_for_the_previous_poll_minute(self):
        a = ClaudeUsage(self.path)
        a.v, a.tried = {'as_of': 970.0, 'seven_day': {'percent': 12.0, 'resets_at': 2000.0}}, 970.0
        a.save()
        b = ClaudeUsage(self.path)                   # new server
        b.load()
        self.assertEqual(b.v['seven_day']['percent'], 12.0)
        self.assertAlmostEqual(b.wait(now=1000.0), 970.0 + USAGE_EVERY - 1000.0)
        self.assertEqual(b.wait(now=970.0 + USAGE_EVERY + 5), 0.0)

    def test_retry_after_is_respected(self):
        u = ClaudeUsage(self.path)
        u.tried, u.retry_after = 1000.0, 300.0
        self.assertAlmostEqual(u.wait(now=1000.0), 300.0)
        u.retry_after = 5.0                          # never called more often than once a minute
        self.assertAlmostEqual(u.wait(now=1000.0), USAGE_EVERY)

    def test_state_file_is_private_and_holds_usage_only(self):
        u = ClaudeUsage(self.path)
        u.v, u.tried = {'as_of': 1.0, 'five_hour': {'percent': 3.0, 'resets_at': 9.0}}, 1.0
        u.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path) as f:
            self.assertEqual(set(json.load(f)), {'v', 'tried'})
        self.assertFalse(os.path.exists(self.path + '.tmp'))

    def test_429_hidden_while_value_is_fresh(self):
        u = ClaudeUsage(self.path)
        u.v, u.error, u.code = {'as_of': 1000.0}, '조회 실패(HTTP 429)', 429
        self.assertIsNone(u.shown_error(now=1000.0 + 2 * USAGE_EVERY))          # a 429 on every other query: the value is 2 minutes old
        self.assertEqual(u.shown_error(now=1000.0 + 3 * USAGE_EVERY + 1), u.error)   # shown if no value has been received for a long time
        u.v = None
        self.assertEqual(u.shown_error(now=1000.0), u.error)                   # shown if there is no value to show
        u.v, u.error, u.code = {'as_of': 1000.0}, '조회 실패(HTTP 401)', 401
        self.assertEqual(u.shown_error(now=1001.0), u.error)                   # other failures are always shown

    def test_broken_state_is_ignored(self):
        os.makedirs(os.path.dirname(self.path))
        for body in ('not json', '[]', '{"v": 3, "tried": "x"}', '{"v": {"percent": 1}}'):
            with open(self.path, 'w') as f:
                f.write(body)
            u = ClaudeUsage(self.path)
            u.load()
            self.assertIsNone(u.v)
            self.assertIsNone(u.tried)


if __name__ == '__main__':
    unittest.main()
