"""Tests of the activity timeline the server sends (board/views.py `timeline`, the `/api/timeline` arguments of server.py): the windows up to 12 hours are sent as they always were (golden:
a copy of the earlier function is the reference), a longer range is binned on the server, `until` ends a range before now, and a range that is not one is a 400.
Made-up sessions only.

    python3 -m unittest discover -s tests
"""
import json
import os
import random
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
from test_stage1 import FakeReg, FakeSession, call  # noqa: E402

from board import views  # noqa: E402
from board.util import short_path  # noqa: E402

NOW = 1_800_000_000.0
KINDS = ('read', 'write', 'bash', 'web', 'msg', 'recv', 'error', 'other')
ORCH = ('orch_msg', 'spawn', 'user_say', 'orch_say', 'handback', 'orch_ask', 'user_answer')


def old_timeline(s, since):
    """The function as it was before ranges: the reference of the golden tests."""
    with s.lock:
        lanes = []
        for a in s.agents.values():
            ticks = [[ts, c] for ts, c in a.ticks if ts and ts >= since]
            if ticks or (a.last_ts or 0) >= since:
                lanes.append({'id': a.id, 'ticks': ticks, 'spawn_ts': a.spawn_ts or a.first_ts,
                              'writes': [[w['ts'], short_path(w['path'])] for w in a.writes if w['ts'] and w['ts'] >= since],
                              'handbacks': [h['ts'] for h in a.handbacks if h['ts'] and h['ts'] >= since],
                              'received': [m['ts'] for m in a.received if m['ts'] and m['ts'] >= since]})
        orch = [[ev['ts'], ev['kind']] for ev in s.feed if ev['ts'] and ev['ts'] >= since and
                ev['kind'] in ('orch_msg', 'spawn', 'user_say', 'orch_say', 'handback', 'orch_ask', 'user_answer')]
        return {'since': since, 'now': 0, 'lanes': lanes, 'orch': orch}


class Agent:
    def __init__(self, aid, spawn, last, ticks=(), writes=(), handbacks=(), received=()):
        self.id, self.spawn_ts, self.first_ts, self.last_ts = aid, spawn, spawn, last
        self.ticks = list(ticks)
        self.writes = [{'ts': ts, 'path': p} for ts, p in writes]
        self.handbacks = [{'ts': ts, 'text': 'x'} for ts in handbacks]
        self.received = [{'ts': ts, 'text': 'x'} for ts in received]


class Sess:
    def __init__(self, agents, feed=()):
        self.lock = threading.RLock()
        self.agents = {a.id: a for a in agents}
        self.feed = list(feed)


def ev(ts, kind):
    return {'ts': ts, 'kind': kind}


def busy(seed=7, n_agents=6, span=3 * 86400):
    """Agents that work in bursts over `span` seconds before NOW, with every kind of mark."""
    rnd = random.Random(seed)
    agents = []
    for i in range(n_agents):
        spawn = NOW - rnd.uniform(0, span)
        ticks, ts = [], spawn
        for _ in range(rnd.randint(40, 400)):
            ts += rnd.expovariate(1 / 90)
            if ts > NOW:
                break
            ticks.append((ts, rnd.choice(KINDS)))
        writes = [(t, '/work/talk/r%d/%s.md' % (rnd.randint(1, 3), 'abc'[rnd.randint(0, 2)])) for t, _ in rnd.sample(ticks, min(3, len(ticks)))] + \
                 [(t, '/work/src/file%d.py' % j) for j, (t, _) in enumerate(rnd.sample(ticks, min(5, len(ticks))))]
        recent = NOW - rnd.uniform(60, 36000)                                     # and a burst in the last ten hours
        ticks += [(recent + k * 30.0, rnd.choice(KINDS)) for k in range(30)]
        agents.append(Agent('a%03d' % i, spawn, ticks[-1][0] if ticks else spawn, ticks, writes, [t for t, _ in ticks[-2:]], [t for t, _ in ticks[:2]]))
    feed = [ev(NOW - rnd.uniform(0, span), rnd.choice(ORCH + ('notify', 'sys'))) for _ in range(200)] + [ev(NOW - 600 * k, rnd.choice(ORCH)) for k in range(1, 30)]
    return Sess(agents, sorted(feed, key=lambda e: e['ts']))


class Golden(unittest.TestCase):
    """The windows up to 12 hours: the same answer as before, key for key."""

    def timeline(self, s, since, **kw):
        with mock.patch.object(views.time, 'time', return_value=NOW):
            return views.timeline(s, since, **kw)

    def test_the_four_windows_are_the_same_as_the_earlier_function(self):
        s = busy()
        for secs in (1800, 7200, 43200, 43200 + 5, 3 * 3600, 600, 5):
            with self.subTest(secs):
                got = self.timeline(s, NOW - secs)
                want = old_timeline(s, NOW - secs)
                self.assertEqual(set(got), {'since', 'now', 'lanes', 'orch'})                  # no key is added to a window that is sent whole
                self.assertEqual(dict(got, now=0), want)
                self.assertTrue(got['lanes'] and got['orch'] if secs >= 7200 else True)

    def test_a_window_that_starts_in_the_future_or_a_session_with_nothing_in_it(self):
        s = busy()
        self.assertEqual(dict(self.timeline(s, NOW + 100), now=0), old_timeline(s, NOW + 100))
        empty = Sess([])
        self.assertEqual(self.timeline(empty, NOW - 7200), {'since': NOW - 7200, 'now': NOW, 'lanes': [], 'orch': []})

    def test_the_answer_is_json(self):
        json.dumps(self.timeline(busy(), NOW - 7 * 86400))
        json.dumps(self.timeline(busy(), NOW - 7200, until=NOW - 3600))

    def test_a_window_up_to_the_limit_is_whole_and_one_second_more_is_binned(self):
        s = busy()
        a = self.timeline(s, NOW - views.TL_BIN_SPAN)
        self.assertNotIn('binned', a)
        b = self.timeline(s, NOW - views.TL_BIN_SPAN - 1)
        self.assertTrue(b['binned'])
        self.assertAlmostEqual(b['bin'], (views.TL_BIN_SPAN + 1) / views.TL_BINS)


class Binned(unittest.TestCase):
    def timeline(self, s, since, **kw):
        with mock.patch.object(views.time, 'time', return_value=NOW):
            return views.timeline(s, since, **kw)

    def test_one_tick_of_each_kind_in_each_column(self):
        since, span = NOW - 7 * 86400, 7 * 86400
        size = span / views.TL_BINS
        s = busy(span=span)
        got = self.timeline(s, since)
        self.assertTrue(got['binned'])
        self.assertAlmostEqual(got['bin'], size)
        self.assertEqual(sorted(l['id'] for l in got['lanes']), sorted(s.agents))
        for lane in got['lanes']:
            a = s.agents[lane['id']]
            cells = {}
            for ts, c in a.ticks:
                if ts >= since:
                    cells.setdefault((int((ts - since) // size), c), []).append(ts)
            self.assertEqual(sorted((int((t[0] - since) // size), t[1]) for t in lane['ticks']), sorted(cells))             # a tick for each (column, kind) that had one
            for t in lane['ticks']:
                k = (int((t[0] - since) // size), t[1])
                self.assertEqual(t[0], min(cells[k]))                                                                       # the first of them
                self.assertEqual(t[2] if len(t) == 3 else 1, len(cells[k]))                                                 # and how many it stands for, when more than one
            self.assertEqual(sum(t[2] if len(t) == 3 else 1 for t in lane['ticks']), sum(1 for ts, _ in a.ticks if ts >= since))
            self.assertEqual([t[0] for t in lane['ticks']], sorted(t[0] for t in lane['ticks']))
            self.assertLessEqual(len(lane['ticks']), views.TL_BINS * len(KINDS))

    def test_a_count_is_only_given_when_it_is_more_than_one(self):
        since = NOW - 3 * 86400
        a = Agent('a1', since + 10, since + 5000, [(since + 100, 'bash'), (since + 101, 'bash'), (since + 102, 'read'), (since + 200000, 'bash')])
        lane = self.timeline(Sess([a]), since)['lanes'][0]
        self.assertEqual(lane['ticks'], [[since + 100, 'bash', 2], [since + 102, 'read'], [since + 200000, 'bash']])

    def test_only_the_saved_reports_of_the_writes_and_one_in_a_column(self):
        since = NOW - 3 * 86400
        a = Agent('a1', since, since + 9000, [(since + 50, 'write')], writes=[(since + 10, '/w/talk/r1/A.md'), (since + 11, '/w/talk/r1/A.md'), (since + 12, '/w/talk/r1/B.md'),
                                                                              (since + 13, '/w/src/main.py'), (since + 14, '/w/notes.md'), (since + 100000, '/w/talk/r2/A.md')])
        lane = self.timeline(Sess([a]), since)['lanes'][0]
        self.assertEqual(lane['writes'], [[since + 10, short_path('/w/talk/r1/A.md')], [since + 12, short_path('/w/talk/r1/B.md')], [since + 100000, short_path('/w/talk/r2/A.md')]])

    def test_hand_backs_and_received_messages_one_in_a_column(self):
        since = NOW - 3 * 86400
        a = Agent('a1', since, since + 9000, [(since + 50, 'write')], handbacks=[since + 100, since + 101, since + 90000], received=[since + 5, since + 6, since + 7])
        lane = self.timeline(Sess([a]), since)['lanes'][0]
        self.assertEqual((lane['handbacks'], lane['received']), ([since + 100, since + 90000], [since + 5]))

    def test_the_orchestrator_marks_one_of_each_kind_in_a_column(self):
        since = NOW - 3 * 86400
        feed = [ev(since + 100, 'spawn'), ev(since + 101, 'spawn'), ev(since + 102, 'user_say'), ev(since + 103, 'notify'), ev(since + 200000, 'spawn')]
        got = self.timeline(Sess([], feed), since)['orch']
        self.assertEqual(got, [[since + 100, 'spawn', 2], [since + 102, 'user_say'], [since + 200000, 'spawn']])

    def test_a_lane_with_no_tick_in_the_range_is_there_while_its_agent_is(self):
        since = NOW - 3 * 86400
        idle = Agent('idle', since - 5000, since + 60, [(since - 4000, 'read')])
        gone = Agent('gone', since - 5000, since - 10, [(since - 4000, 'read')])
        ids = [lane['id'] for lane in self.timeline(Sess([idle, gone]), since)['lanes']]
        self.assertEqual(ids, ['idle'])

    def test_the_size_is_a_page_not_the_history(self):
        s = busy(n_agents=40, span=7 * 86400)
        for a in s.agents.values():
            a.ticks += [(NOW - 7 * 86400 + i * 100.0, KINDS[i % 8]) for i in range(6000)]       # 40 lanes that have been busy all week, in every kind
        got = self.timeline(s, NOW - 7 * 86400)
        self.assertLessEqual(sum(len(lane['ticks']) for lane in got['lanes']), views.TL_MARKS_MAX)
        self.assertGreater(got['bin'], 7 * 86400 / views.TL_BINS)                                   # the columns got wider to keep within it
        self.assertLess(len(json.dumps(got)), 1_000_000)

    def test_a_session_within_the_limit_keeps_its_columns(self):
        got = self.timeline(busy(), NOW - 7 * 86400)
        self.assertAlmostEqual(got['bin'], 7 * 86400 / views.TL_BINS)


class OrderOfTicks(unittest.TestCase):
    """The ticks of an agent are looked at between the two times by bisection when they are in time order; a list that is not (a tick with no time, one out of order) is looked at whole."""

    def timeline(self, s, since, **kw):
        with mock.patch.object(views.time, 'time', return_value=NOW):
            return views.timeline(s, since, **kw)

    def lane(self, ticks, since=None, **kw):
        a = Agent('a1', NOW - 10 * 86400, NOW - 50, ticks)
        s = Sess([a])
        return [t[:2] for t in self.timeline(s, since or NOW - 7 * 86400, **kw)['lanes'][0]['ticks']], s

    def test_a_tick_with_no_time_does_not_stop_a_long_range(self):
        ticks = [(NOW - 500, 'read'), (None, 'bash'), (NOW - 100, 'write')]
        got, s = self.lane(ticks)
        self.assertEqual(got, [[NOW - 500, 'read'], [NOW - 100, 'write']])
        s.agents['a1'].ticks.append((NOW - 60, 'web'))                         # the next scan goes on from where the order was last looked at
        s.agents['a1'].ticks.append((0, 'other'))
        got = [t[:2] for t in self.timeline(s, NOW - 7 * 86400)['lanes'][0]['ticks']]
        self.assertEqual(got, [[NOW - 500, 'read'], [NOW - 100, 'write'], [NOW - 60, 'web']])

    def test_a_tick_with_no_time_in_the_first_place_or_the_last(self):
        for ticks in ([(None, 'bash'), (NOW - 500, 'read')], [(NOW - 500, 'read'), (None, 'bash')], [(None, 'bash')]):
            got, _ = self.lane(ticks)
            self.assertEqual(got, [[ts, c] for ts, c in ticks if ts], ticks)

    def test_ticks_out_of_order_are_all_found(self):
        ticks = [(NOW - 100, 'read'), (NOW - 500, 'bash'), (NOW - 300, 'write'), (NOW - 8 * 86400, 'web')]
        got, s = self.lane(ticks)
        self.assertEqual(sorted(got), sorted([[NOW - 100, 'read'], [NOW - 500, 'bash'], [NOW - 300, 'write']]))
        got, _ = self.lane(ticks, since=NOW - 400, until=NOW - 150)
        self.assertEqual(got, [[NOW - 300, 'write']])

    def test_a_list_in_order_is_cut_at_both_ends_and_kept_up_to_date(self):
        ticks = [(NOW - 9 * 86400 + i * 3600, 'read') for i in range(0, 216)]                # one an hour for nine days
        a = Agent('a1', NOW - 10 * 86400, NOW - 50, ticks)
        s = Sess([a])
        first = self.timeline(s, NOW - 3 * 86400, until=NOW - 2 * 86400)['lanes'][0]['ticks']
        want = [t for t in ticks if NOW - 3 * 86400 <= t[0] <= NOW - 2 * 86400]
        self.assertEqual(sorted(t[0] for t in first), sorted(t[0] for t in want))
        a.ticks.append((NOW - 2 * 86400 - 10, 'bash'))                                       # later in the list, so out of order: it is found
        again = self.timeline(s, NOW - 3 * 86400, until=NOW - 2 * 86400)['lanes'][0]['ticks']
        self.assertIn(NOW - 2 * 86400 - 10, [t[0] for t in again])
        a.ticks[:] = a.ticks[:10]                                                            # a list that got shorter is looked at again
        self.assertEqual([t[0] for t in self.timeline(s, NOW - 10 * 86400)['lanes'][0]['ticks']], [t[0] for t in a.ticks])


class RankedAndOutsideTheLock(unittest.TestCase):
    """The rows of a range are the ones that worked in it, and the binning is done once, outside the session's lock."""

    def timeline(self, s, since, **kw):
        with mock.patch.object(views.time, 'time', return_value=NOW):
            return views.timeline(s, since, **kw)

    def test_the_rows_of_a_past_range_are_the_agents_that_worked_in_it(self):
        since, until = NOW - 3 * 86400, NOW - 2 * 86400
        idle = [Agent('idle%02d' % i, since - 86400, NOW - 10 - i, [(NOW - 5, 'read')]) for i in range(70)]                  # alive through the range, nothing in it
        busy_ = [Agent('work%02d' % i, since + 1000, since + 5000 + i, [(since + 2000 + k, 'bash') for k in range(0, 4000, 20)]) for i in range(30)]
        got = self.timeline(Sess(idle + busy_), since, until=until)
        ids = [lane['id'] for lane in got['lanes']]
        self.assertEqual((len(ids), got['lanes_total']), (views.TL_ROWS, 100))
        self.assertEqual(sorted(i for i in ids if i.startswith('work')), ['work%02d' % i for i in range(30)])
        self.assertEqual(ids[:30], ['work%02d' % i for i in range(29, -1, -1)])                       # the ones with a tick in the range first, the latest tick first
        self.assertTrue(all(lane['ticks'] for lane in got['lanes'][:30]))

    def test_an_agent_with_no_tick_in_the_range_comes_after_the_ones_with_ticks_by_its_last_activity(self):
        since, until = NOW - 3 * 86400, NOW - 2 * 86400
        a = [Agent('quiet-new', since - 10, NOW - 5, []), Agent('quiet-old', since - 10, since + 10, []), Agent('works', since + 100, since + 200, [(since + 150, 'bash')])]
        got = self.timeline(Sess(a), since, until=until)
        self.assertEqual([lane['id'] for lane in got['lanes']], ['works', 'quiet-new', 'quiet-old'])

    def test_a_list_that_is_not_in_order_is_ranked_by_the_latest_tick_in_the_range(self):
        since, until = NOW - 3 * 86400, NOW - 2 * 86400
        a = Agent('late', since - 10, since + 9000, [(since + 5000, 'bash'), (since + 100, 'read'), (NOW - 5, 'read')])        # out of order
        b = Agent('early', since - 10, since + 9000, [(since + 1000, 'bash')])
        got = self.timeline(Sess([b, a]), since, until=until)
        self.assertEqual([lane['id'] for lane in got['lanes']], ['late', 'early'])

    def test_the_lanes_come_in_the_order_of_the_work_in_the_range_whatever_the_marks_show(self):
        """A binned tick stands at the first time of its column: one agent that worked at +100 and +500 s of a column (its tick says +100) and sixty that worked at +201...+260 s. The
        order the lanes come in is by the work itself (the agent that worked last first), so a page that keeps the first 60 of what it is given keeps the right ones."""
        since = NOW - 7 * 86400
        a = Agent('aaa', since - 100, since + 700, [(since + 100, 'bash'), (since + 500, 'bash')])
        others = [Agent('o%02d' % i, since - 100, since + 700, [(since + 201 + i, 'bash')]) for i in range(60)]
        for kw in ({}, {'all_lanes': True}):
            got = self.timeline(Sess(others + [a]), since, **kw)
            ids = [lane['id'] for lane in got['lanes']]
            self.assertEqual(ids[0], 'aaa', kw)                                                          # the latest work in the range comes first
            self.assertEqual(ids[1:3], ['o59', 'o58'], kw)
            self.assertEqual(len(ids), views.TL_ROWS if not kw else 61)
            self.assertEqual(got['lanes'][0]['ticks'], [[since + 100, 'bash', 2]])                       # and its tick is the first of its column: it cannot say who worked last
            self.assertEqual(got['lanes_total'], 61)

    def test_the_binning_is_done_outside_the_lock_and_once_for_each_lane(self):
        class Lock:
            def __init__(self):
                self.depth, self.lock = 0, threading.RLock()

            def __enter__(self):
                self.lock.acquire()
                self.depth += 1

            def __exit__(self, *a):
                self.depth -= 1
                self.lock.release()
        s = busy(n_agents=12, span=7 * 86400)
        for a in s.agents.values():
            a.ticks += [(NOW - 7 * 86400 + i * 100.0, KINDS[i % 8]) for i in range(6000)]       # more ticks than a page may hold: the columns get wider
        s.lock = Lock()
        seen = []
        real = views._cells
        with mock.patch.object(views, '_cells', lambda *a, **k: seen.append(s.lock.depth) or real(*a, **k)):
            got = self.timeline(s, NOW - 7 * 86400)
        self.assertGreater(got['bin'], 7 * 86400 / views.TL_BINS)
        self.assertEqual((len(seen), set(seen)), (12, {0}))                                    # a pass for each lane, none while the lock is held
        self.assertLessEqual(sum(len(lane['ticks']) for lane in got['lanes']), views.TL_MARKS_MAX)

    def test_widening_the_columns_gives_what_binning_at_that_width_gives(self):
        rnd = random.Random(5)
        ticks = sorted((NOW - 5 * 86400 + rnd.uniform(0, 5 * 86400), rnd.choice(KINDS)) for _ in range(3000))
        since, hi, size = NOW - 5 * 86400, NOW, 5 * 86400 / views.TL_BINS
        for f in (2, 3, 7):
            merged = views._marks(views._merge(views._cells(ticks, since, hi, size), f))
            direct = views._marks(views._cells(ticks, since, hi, size * f))
            self.assertEqual(merged, direct, f)

    def test_the_feed_and_the_lists_are_copied_in_the_lock_and_read_after_it(self):
        s = busy(n_agents=3, span=3 * 86400)
        got = self.timeline(s, NOW - 3 * 86400)
        self.assertTrue(got['orch'] and all(lane['writes'] is not None for lane in got['lanes']))


class Rows(unittest.TestCase):
    """A long range holds the most recently active lanes, and all of them when asked."""

    def timeline(self, s, since, **kw):
        with mock.patch.object(views.time, 'time', return_value=NOW):
            return views.timeline(s, since, **kw)

    def crowd(self, n=90):
        since = NOW - 3 * 86400
        agents = [Agent('c%03d' % i, since + 1000, since + 2000 + i * 100, [(since + 1100, 'bash'), (since + 1200, 'read')]) for i in range(n)]
        return Sess(agents), since

    def test_the_most_recently_active_rows_and_how_many_there_are(self):
        s, since = self.crowd()
        got = self.timeline(s, since)
        self.assertEqual((len(got['lanes']), got['lanes_total']), (views.TL_ROWS, 90))
        self.assertEqual([l['id'] for l in got['lanes']], ['c%03d' % i for i in range(89, 89 - views.TL_ROWS, -1)])        # the newest first
        self.assertTrue(all(l['ticks'] for l in got['lanes']))

    def test_all_of_them_when_asked(self):
        s, since = self.crowd()
        got = self.timeline(s, since, all_lanes=True)
        self.assertEqual((len(got['lanes']), got['lanes_total']), (90, 90))

    def test_a_long_range_with_few_lanes_holds_them_all(self):
        s, since = self.crowd(7)
        got = self.timeline(s, since)
        self.assertEqual((len(got['lanes']), got['lanes_total']), (7, 7))

    def test_the_total_counts_the_lanes_at_work_in_the_range(self):
        s, since = self.crowd(10)
        s.agents['old'] = Agent('old', since - 9000, since - 100, [(since - 500, 'bash')])
        s.agents['later'] = Agent('later', since + 90000, since + 95000, [(since + 91000, 'bash')])
        got = self.timeline(s, since, until=since + 50000)
        self.assertEqual((got['lanes_total'], sorted(l['id'] for l in got['lanes'])), (10, ['c%03d' % i for i in range(10)]))

    def test_a_window_that_is_sent_whole_is_not_cut(self):
        s, since = self.crowd(90)
        got = self.timeline(s, NOW - 3600 * 12)
        self.assertNotIn('lanes_total', got)
        s2 = Sess([Agent('x%03d' % i, NOW - 5000, NOW - 100 - i, [(NOW - 4000, 'read')]) for i in range(90)])
        got = self.timeline(s2, NOW - 3600 * 12)
        self.assertEqual(len(got['lanes']), 90)

    def test_a_heavy_session_is_answered_in_a_blink_and_within_a_megabyte(self):
        rnd = random.Random(3)
        agents = []
        for i in range(300):
            start = NOW - rnd.uniform(0, 7 * 86400)
            ts, ticks = start, []
            for _ in range(400):
                ts += rnd.expovariate(1 / 60)
                ticks.append((ts, rnd.choice(KINDS)))
            agents.append(Agent('h%03d' % i, start, ticks[-1][0], ticks))
        s = Sess(agents)
        got = self.timeline(s, NOW - 7 * 86400)
        self.assertEqual(len(got['lanes']), views.TL_ROWS)
        self.assertLess(len(json.dumps(got)), 1_000_000)
        every = self.timeline(s, NOW - 7 * 86400, all_lanes=True)
        self.assertEqual(len(every['lanes']), 300)
        self.assertLess(len(json.dumps(every)), 1_000_000)


class Until(unittest.TestCase):
    def timeline(self, s, since, until=None):
        with mock.patch.object(views.time, 'time', return_value=NOW):
            return views.timeline(s, since, until)

    def test_what_is_after_it_is_left_out(self):
        s = Sess([Agent('a1', NOW - 5000, NOW - 100, [(NOW - 4000, 'read'), (NOW - 3000, 'bash'), (NOW - 2000, 'write'), (NOW - 1000, 'web')],
                        writes=[(NOW - 3500, '/w/talk/r1/A.md'), (NOW - 500, '/w/talk/r2/A.md')], handbacks=[NOW - 2500, NOW - 200], received=[NOW - 4500, NOW - 300])],
                 [ev(NOW - 3900, 'spawn'), ev(NOW - 1500, 'orch_msg'), ev(NOW - 50, 'orch_say')])
        got = self.timeline(s, NOW - 6000, NOW - 2200)
        lane = got['lanes'][0]
        self.assertEqual(got['until'], NOW - 2200)
        self.assertEqual(lane['ticks'], [[NOW - 4000, 'read'], [NOW - 3000, 'bash']])
        self.assertEqual((lane['writes'], lane['handbacks'], lane['received']), ([[NOW - 3500, short_path('/w/talk/r1/A.md')]], [NOW - 2500], [NOW - 4500]))
        self.assertEqual(got['orch'], [[NOW - 3900, 'spawn']])
        self.assertNotIn('binned', got)

    def test_a_lane_of_an_agent_that_began_after_it_is_left_out(self):
        s = Sess([Agent('early', NOW - 9000, NOW - 8000, [(NOW - 8500, 'read')]), Agent('late', NOW - 1000, NOW - 10, [(NOW - 500, 'read')])])
        self.assertEqual([lane['id'] for lane in self.timeline(s, NOW - 10000, NOW - 3000)['lanes']], ['early'])
        self.assertEqual([lane['id'] for lane in self.timeline(s, NOW - 10000)['lanes']], ['early', 'late'])

    def test_a_long_range_that_ends_in_the_past_is_binned_by_its_own_length(self):
        since, until = NOW - 20 * 86400, NOW - 10 * 86400
        a = Agent('a1', since + 100, until - 100, [(since + 100, 'bash'), (until - 100, 'bash'), (until + 5, 'bash'), (since - 5, 'bash')])
        got = self.timeline(Sess([a]), since, until)
        self.assertTrue(got['binned'])
        self.assertAlmostEqual(got['bin'], 10 * 86400 / views.TL_BINS)
        self.assertEqual([t[0] for t in got['lanes'][0]['ticks']], [since + 100, until - 100])
        short = self.timeline(Sess([a]), until - 3600, until)
        self.assertNotIn('binned', short)

    def test_the_default_is_now(self):
        a = Agent('a1', NOW - 100, NOW, [(NOW - 50, 'bash'), (NOW + 50, 'bash')])
        self.assertEqual(self.timeline(Sess([a]), NOW - 1000)['lanes'][0]['ticks'], [[NOW - 50, 'bash'], [NOW + 50, 'bash']])      # nothing is left out of a window that has no end


class Arguments(unittest.TestCase):
    """The arguments of /api/timeline: since and until are numbers, until is after since and at most 31 days away, else 400 with a JSON error."""
    S = '&session=11111111-2222-4333-8444-555555555555'

    def test_a_range_that_is_not_one_is_a_400_with_its_own_code(self):
        t = server.time.time()
        for q in ('since=%f&until=%f' % (t, t - 5), 'since=%f&until=%f' % (t, t), 'since=%f&until=%f' % (t - 32 * 86400, t), 'since=%f&until=%f' % (t - 31 * 86400 - 1, t),
                  'until=%f&since=-1e300' % t, 'since=1&until=1e300'):
            code, body = call('/api/timeline?' + q + self.S)
            self.assertEqual((code, body), (400, {'error': 'bad range', 'error_code': 'bad_range'}), q)

    def test_an_until_that_is_not_a_number_is_a_bad_parameter(self):
        for q in ('until=abc', 'until=nan', 'until=inf', 'until=-inf', 'until=1,5', 'until=0x10', 'since=nan&until=5', 'since=abc&until=5'):
            code, body = call('/api/timeline?' + q + self.S)
            self.assertEqual((code, body), (400, {'error': 'bad parameter', 'error_code': 'bad_parameter'}), q)

    def test_an_empty_until_is_none(self):
        self.assertEqual(call('/api/timeline?since=123.5&until=' + self.S), (200, {'since': 123.5}))              # parse_qs drops an empty value

    def test_without_until_the_answer_is_the_sessions_as_it_was(self):
        self.assertEqual(call('/api/timeline?since=123.5' + self.S), (200, {'since': 123.5}))
        self.assertEqual(call('/api/timeline?since=1' + self.S), (200, {'since': 1.0}))                           # no limit on a range without until: all of a session

    def test_a_good_range_is_given_to_the_timeline(self):
        t = float(int(server.time.time()))
        seen = []
        with mock.patch.object(server.views, 'timeline', lambda s, since, until, everyone: seen.append((since, until, everyone)) or {'ok': 1}):
            self.assertEqual(call('/api/timeline?since=%f&until=%f' % (t - 3600, t - 60) + self.S), (200, {'ok': 1}))
            self.assertEqual(call('/api/timeline?since=%f&until=%f' % (t - 31 * 86400, t) + self.S), (200, {'ok': 1}))      # exactly the longest
            self.assertEqual(call('/api/timeline?until=%f' % t + self.S), (200, {'ok': 1}))                                  # since is an hour before now
        self.assertEqual(seen[0], (t - 3600, t - 60, False))
        self.assertEqual(seen[1], (t - 31 * 86400, t, False))
        self.assertAlmostEqual(seen[2][0], server.time.time() - 3600, delta=5)        # since defaults to an hour before now

    def test_all_is_asked_for_with_all_1(self):
        t = float(int(server.time.time()))
        seen = []
        with mock.patch.object(server.views, 'timeline', lambda s, since, until, everyone: seen.append((since, until, everyone)) or {'ok': 1}):
            for q, want in (('&all=1', True), ('&all=true', True), ('&all=0', False), ('&all=yes', False), ('&all=', False), ('', False)):
                seen.clear()
                self.assertEqual(call('/api/timeline?since=%f&until=%f' % (t - 3 * 86400, t) + q + self.S)[0], 200)
                self.assertEqual(seen, [(t - 3 * 86400, t, want)], q)
            seen.clear()
            self.assertEqual(call('/api/timeline?since=%f&all=1' % (t - 3 * 86400) + self.S), (200, {'ok': 1}))           # without until too
            self.assertEqual(seen, [(t - 3 * 86400, None, True)])
            seen.clear()
            self.assertEqual(call('/api/timeline?since=%f&all=0' % (t - 3 * 86400) + self.S), (200, {'since': t - 3 * 86400}))     # no until, not all: the session's own
            self.assertEqual(seen, [])

    def test_through_a_real_view(self):
        s = Sess([Agent('a1', NOW - 5000, NOW - 100, [(NOW - 4000, 'read'), (NOW - 1000, 'bash')])], [ev(NOW - 900, 'spawn')])
        s.id = '11111111-2222-4333-8444-555555555555'
        s.timeline = lambda since: views.timeline(s, since)
        with mock.patch.object(views.time, 'time', return_value=NOW):
            code, body = call('/api/timeline?since=%f&until=%f' % (NOW - 6000, NOW - 2000) + self.S, s)
        self.assertEqual(code, 200)
        self.assertEqual((body['until'], [t[0] for t in body['lanes'][0]['ticks']], body['orch']), (NOW - 2000, [NOW - 4000], []))


if __name__ == '__main__':
    unittest.main()
