"""A judgment is reused only while nothing it read has changed (CONTRACT J19, 4.5 `test_cache`).

The key of the cache is the state of the agents and the session (`facts_gen`, the statuses, the walk). That cannot know what the disk says, so the judgment also keeps every lookup it made of the disk with the
answer (`Catalog.reads`), and a judgment is reused only when asking them all again gives the same answers. Each test changes one thing on the disk while the facts stay the same (`facts_gen` and the key do not
move) and sees the judgment made again; the same facts and the same disk give the same judgment, and a judgment is the same object while nothing moved. What the page shows of a file or a folder is among
the lookups of the judgment too (O15), so that one list says whether the judgment is good.

    python3 -m unittest tests.test_cache
"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402

from board import units as U  # noqa: E402
from board.facts import Planned, WriteEvent  # noqa: E402


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.talk = os.path.join(self.root, 'talk')
        os.makedirs(os.path.join(self.talk, 'r1'))
        os.makedirs(os.path.join(self.talk, 'r2'))                              # a round that nobody has written in yet
        self.s = server.Session.__new__(server.Session)
        server.Session.__init__(self.s, '/nonexistent/x.jsonl')
        self.s.cwd = self.root
        self.a = self.agent('A')

    def agent(self, aid):
        a = server.Agent(aid + '-0000-4000-8000-000000000001', {'description': aid})
        a.origin, a.cwd = 'subagent', self.root
        a.spawn_ts = a.first_ts = 100.0
        a.last_ts = 300.0
        self.s.agents[a.id] = a
        return a

    def put(self, rel, text='x\n', mtime=200.0):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        os.utime(path, (mtime, mtime))
        return path

    def write(self, a, path, ts=200.0, **kw):
        a.ev.add_write(WriteEvent(a.id, path, ts, kw.pop('kind', 'create'), kw.pop('evidence', 'tool'), kw.pop('ok', True), run=1, **kw))

    def judged(self):
        return self.s.judged({a.id: 'done' for a in self.s.agents.values()})

    def key(self):
        return self.s._debate_key({a.id: 'done' for a in self.s.agents.values()})

    def cells(self, jd):
        return {(r['p'], c['round']): c for d in jd.debates for t in d['topics'] for r in t['rows'] for c in r['cells']}

    def test_nothing_changed_the_judgment_is_the_same_one(self):
        self.write(self.a, self.put('talk/r1/A.md'))
        first = self.judged()
        self.assertTrue(first.reads)
        self.assertIs(self.judged(), first)
        self.assertIs(self.judged(), first)
        self.assertFalse(U.Catalog.changed(first.reads))

    def test_a_folder_that_got_a_file_is_judged_again(self):
        self.write(self.a, self.put('talk/r1/A.md'))
        first, key = self.judged(), self.key()
        self.put('talk/r2/B.md', mtime=250.0)                                  # the empty round folder gets a file: the folder `talk` and the files the judgment stat'ed are the same
        self.assertEqual(self.key(), key)
        second = self.judged()
        self.assertIsNot(second, first)
        self.assertIn(('B', 2), self.cells(second))
        self.assertNotIn(('B', 2), self.cells(first))

    def test_a_cell_file_written_over_in_place_is_judged_again(self):
        path = self.put('talk/r1/A.md', 'first\n')
        self.write(self.a, path)
        first, key = self.judged(), self.key()
        self.assertEqual(self.cells(first)[('A', 1)]['lines'], 1)
        folder = os.stat(os.path.dirname(path)).st_mtime_ns
        with open(path, 'w') as f:                                             # the same file, in place: its time and size move, the time of the folder does not
            f.write('second\nthird\n')
        os.utime(path, (400.0, 400.0))
        os.utime(os.path.dirname(path), ns=(folder, folder))
        self.assertEqual(self.key(), key)
        second = self.judged()
        self.assertIsNot(second, first)

    def test_a_link_that_was_made_is_judged_again(self):
        path = self.put('talk/r1/A.md')
        alias = os.path.join(self.root, 'alias')
        self.write(self.a, os.path.join(alias, 'r1', 'A.md'))                  # the agent wrote through a name that is no folder yet
        first, key = self.judged(), self.key()
        self.assertEqual(first.debates, [])
        os.symlink(self.talk, alias)
        self.assertEqual(self.key(), key)
        second = self.judged()
        self.assertIsNot(second, first)
        self.assertEqual([d['root'] for d in second.debates], [self.talk])
        self.assertEqual(os.path.realpath(path), path)

    def test_the_file_of_a_checked_write_that_was_replaced_is_judged_again(self):
        path = self.put('talk/r1/A.md', 'first', mtime=250.0)
        a = self.a
        a.ev.add_planned(Planned(a.id, path, '-o', None, 1, 'command', 100.0))
        self.write(a, path, 300.0, kind='replace', evidence='planned', proof='sha', span=(100.0, 300.0), shas=(hashlib.sha1(b'first').hexdigest(),))
        first, key = self.judged(), self.key()
        self.assertEqual(self.cells(first)[('A', 1)]['agent'], a.id)
        self.assertEqual(self.cells(first)[('A', 1)]['state'], 'done')
        st = os.stat(path)
        with open(path, 'r+b') as f:                                           # other bytes of the same size
            f.write(b'other')
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(path).st_size, st.st_size)
        self.assertEqual(os.stat(path).st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(self.key(), key)
        self.assertIs(self.judged(), first)                                    # the limit (O15): the bytes are asked again only when the stat of the file moves, and this one did not
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))          # the time moved
        second = self.judged()
        self.assertIsNot(second, first)
        self.assertNotEqual(self.cells(second)[('A', 1)]['state'], 'done')     # the save is not the one the run was to make

    def test_the_same_facts_and_the_same_disk_give_the_same_judgment(self):
        self.write(self.a, self.put('talk/r1/A.md'))
        a, b = self.s.judged({self.a.id: 'done'}), None
        self.s.__dict__.pop('_jd_cache')
        b = self.s.judged({self.a.id: 'done'})
        self.assertIsNot(a, b)
        self.assertEqual(a.debates, b.debates)
        self.assertEqual((a.agent_units, a.members, a.diag, a.placed), (b.agent_units, b.members, b.diag, b.placed))
        self.assertEqual({k: vars(v) for k, v in a.cells.items()}, {k: vars(v) for k, v in b.cells.items()})
        self.assertEqual(a.reads, b.reads)

    def test_a_new_status_is_another_judgment(self):
        self.write(self.a, self.put('talk/r1/A.md'))
        first = self.s.judged({self.a.id: 'running'})
        self.assertIsNot(self.s.judged({self.a.id: 'done'}), first)
        self.assertIs(self.s.judged({self.a.id: 'running'}), first)


    # ----- what a judgment costs while it is reused (O15): the folders above a path are looked at once, what the page shows of a file is among the lookups, a request for one agent asks nothing,
    # and the facts are brought up to date once

    def test_the_real_path_the_catalog_finds_is_the_one_of_the_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            os.makedirs(os.path.join(root, 'a', 'b', 'c'))
            with open(os.path.join(root, 'a', 'f.txt'), 'w') as h:
                h.write('x')
            for name, target in (('ln', os.path.join(root, 'a', 'b')), ('a/rel', 'b/c'), ('a/chain', '../a/rel'), ('loop1', os.path.join(root, 'loop2')), ('loop2', os.path.join(root, 'loop1')),
                                 ('dangling', os.path.join(root, 'nowhere', 'zz'))):
                os.symlink(target, os.path.join(root, name))
            paths = ['', 'a', 'a/b/c', 'ln', 'ln/c', 'ln/c/x/y', 'a/rel', 'a/rel/q', 'a/chain', 'a/chain/z', 'loop1', 'loop1/x', 'dangling', 'dangling/x', 'a/f.txt/x', 'nope/x/y', 'a/b/../b/c', 'ln/../a']
            memo = {}
            for rel in paths + ['/', '/tmp', '//tmp/x', 'relative/x'] + paths[::-1]:
                path = os.path.join(root, rel) if rel not in ('/', '/tmp', '//tmp/x', 'relative/x') else rel
                self.assertEqual(U._real_via(path, memo), os.path.realpath(path), path)

    def test_the_folders_above_the_paths_are_looked_at_once_when_the_lookups_are_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            files = [os.path.join(root, 'd%d' % (i % 4), 'sub', 'f%d.md' % i) for i in range(40)]
            for f in files:
                os.makedirs(os.path.dirname(f), exist_ok=True)
                open(f, 'w').close()
            cat = U.Catalog()
            cat.begin()
            for f in files:
                cat.realpath(os.path.dirname(f))
                cat.realpath(f)
            reads = cat.reads()
            depth = len(root.split(os.sep))
            with mock.patch('os.lstat', wraps=os.lstat) as lstat:
                self.assertFalse(U.Catalog.changed(reads))
            names = depth + 4 * 2 + 40                                                  # every name of every distinct path once: the folders of the root, d0..d3, sub, the files
            self.assertLessEqual(lstat.call_count, names)
            self.assertLess(lstat.call_count * 3, 80 * (depth + 3))                     # (asking `realpath` for each would be 80 paths of depth + 2 or 3 names)

    def test_the_bytes_are_asked_again_only_when_the_stat_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'f.md')
            with open(path, 'w') as h:
                h.write('abc')
            cat = U.Catalog()
            cat.begin()
            cat.sha1(path)
            reads = cat.reads()
            with mock.patch.object(U, 'open_safe', side_effect=AssertionError('the bytes were read')):
                self.assertFalse(U.Catalog.changed(reads))
            with open(path, 'w') as h:
                h.write('abcd')
            self.assertTrue(U.Catalog.changed(reads))
            os.remove(path)
            self.assertTrue(U.Catalog.changed(reads))

    def test_what_the_page_shows_of_a_brief_is_made_again_when_the_brief_changes(self):
        brief = self.put('talk/brief.md', '# First title\n', mtime=100.0)
        self.write(self.a, self.put('talk/r1/A.md'))
        first = self.judged()
        self.assertEqual(first.debates[0]['title'], 'First title')
        self.assertIs(self.judged(), first)
        with open(brief, 'w') as h:
            h.write('# Second title\n')
        os.utime(brief, (400.0, 400.0))
        second = self.judged()
        self.assertIsNot(second, first)
        self.assertEqual(second.debates[0]['title'], 'Second title')

    def test_a_document_of_a_topic_that_was_added_is_listed_at_the_next_look(self):
        self.write(self.a, self.put('talk/r1/A.md'))
        first = self.judged()
        self.assertEqual(first.debates[0]['topics'][0]['docs'], [])
        self.put('talk/notes.md')
        second = self.judged()
        self.assertEqual([d['name'] for d in second.debates[0]['topics'][0]['docs']], ['notes.md'])

    def test_the_facts_are_brought_up_to_date_once_for_a_judgment(self):
        self.write(self.a, self.put('talk/r1/A.md'))
        calls = []
        real = self.s.refresh_facts
        self.s.refresh_facts = lambda statuses=None: (calls.append(1), real(statuses))[1]
        self.judged()
        self.assertEqual(len(calls), 1, 'a judgment that is made brings the facts up to date once')
        self.judged()
        self.assertEqual(len(calls), 2, 'one for the key of a judgment that is reused')

    def test_a_request_for_one_agent_reuses_the_last_judgment_and_asks_nothing(self):
        import types
        from board import views
        self.write(self.a, self.put('talk/r1/A.md'))
        statuses = {a.id: 'done' for a in self.s.agents.values()}
        self.s._verdicts = {i: types.SimpleNamespace(status=st) for i, st in statuses.items()}
        first = self.s.judged(statuses)
        self.assertIs(self.s.last_judged(statuses), first)
        self.assertIsNone(self.s.last_judged({self.a.id: 'running'}))
        with mock.patch.object(U.Catalog, 'changed', side_effect=AssertionError('the disk was looked at')), mock.patch.object(self.s, 'refresh_facts', side_effect=AssertionError('the facts were made')):
            d = views.agent_detail(self.s, self.a.id)
        self.assertEqual((d['placed'], d['launch']), (first.placed.get(self.a.id), first.launch.get(self.a.id)))
        self.s._verdicts[self.a.id] = types.SimpleNamespace(status='running')            # a state the last judgment was not made for: the judgment is asked for
        self.assertEqual(views.agent_detail(self.s, self.a.id)['id'], self.a.id)
        self.assertIsNot(self.s.last_judged({self.a.id: 'running'}), first)

class Lookups(unittest.TestCase):
    """The lookups the Catalog keeps and asks again."""

    def test_every_kind_of_lookup_is_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            os.makedirs(os.path.join(root, 'd', 'r1'))
            f = os.path.join(root, 'd', 'r1', 'A.md')
            with open(f, 'w') as h:
                h.write('x')
            os.symlink(f, os.path.join(root, 'l'))
            cat = U.Catalog()
            cat.begin()
            cat.stat(f), cat.lstat(os.path.join(root, 'l')), cat.listdir(os.path.join(root, 'd')), cat.realpath(os.path.join(root, 'l')), cat.readlink(os.path.join(root, 'l'))
            cat.sha1(f), cat.first_line(f), cat.stat(os.path.join(root, 'missing'))
            reads = cat.reads()
            self.assertEqual({op for (op, _arg), _ans in reads}, {'stat', 'lstat', 'list', 'real', 'link', 'sha', 'line'})
            self.assertFalse(U.Catalog.changed(reads))
            with open(os.path.join(root, 'missing'), 'w') as h:                 # a lookup that found nothing is remembered too
                h.write('y')
            self.assertTrue(U.Catalog.changed(reads))

    def test_a_lookup_is_made_once_in_a_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat = U.Catalog()
            cat.begin()
            with mock.patch.object(U, '_probe', wraps=U._probe) as probe:
                cat.listdir(tmp), cat.listdir(tmp), cat.stat(tmp), cat.stat(tmp)
                self.assertEqual(probe.call_count, 2)
            cat.begin()
            self.assertEqual(cat.reads(), ())

if __name__ == '__main__':
    unittest.main()
