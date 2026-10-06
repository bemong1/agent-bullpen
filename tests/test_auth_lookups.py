"""The auth files are refused by name, and the board does not look them up again and again (O4): the four files of the deny list (the credentials, the two places of `.claude.json`, the Codex `auth.json`)
get no stat, lstat or readlink from the checks of a judgment, the way that opens content or the way that only looks at metadata, beyond one look each, the first time, to learn whether one is a link
(what it found is kept while the folders that hold them are as they were). The folders that hold them are looked at, and a name that is a link is followed.

    python3 -m unittest tests.test_auth_lookups
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched  # noqa: E402

from board import debates, units as U, util  # noqa: E402


class Home(unittest.TestCase):
    """A synthetic HOME with the four auth files, and a spy on every lookup of the system that takes a path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        self.claude, self.codex = os.path.join(self.home, '.claude'), os.path.join(self.home, '.codex')
        self.doc_dir = os.path.join(self.home, 'work', 'talk', 'r1')
        for d in (self.claude, self.codex, self.doc_dir):
            os.makedirs(d)
        self.auth = [self.put(self.claude, '.credentials.json'), self.put(self.home, '.claude.json'), self.put(self.codex, 'auth.json')]
        self.missing = os.path.join(self.claude, '.claude.json')                 # a candidate that is not there is not looked up either
        self.doc = self.put(self.doc_dir, 'A.md', 'a\nb\n')
        self.patch = patched(HOME=self.home, CLAUDE_HOME=self.claude, CODEX_HOME=self.codex, DENY_FILES=(*self.auth, self.missing))
        self.patch.__enter__()
        self.addCleanup(self.patch.__exit__, None, None, None)
        util._DENY_ROOTS['e'] = None
        util._AUTH_LINKS['e'] = None
        util._REAL_DIRS.clear()
        self.addCleanup(lambda: (util._DENY_ROOTS.update(e=None), util._AUTH_LINKS.update(e=None), util._REAL_DIRS.clear()))
        self.old()

    def put(self, folder, name, text='{}\n'):
        path = os.path.join(folder, name)
        os.makedirs(folder, exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def old(self):
        """Every folder as old as a folder that nobody touched for a while (the state of a folder that changed a moment ago is not trusted, and these tests are about the ones that did not)."""
        past = time.time() - 3600
        for d in (self.home, self.claude, self.codex, self.doc_dir, os.path.dirname(self.doc_dir)):
            if os.path.isdir(d):
                os.utime(d, (past, past))

    def watched(self):
        """The lookups of the system on a path of the deny list: the context manager gives the list they are written to."""
        names = {os.path.normpath(p) for p in (*self.auth, self.missing)}
        seen = []
        real = {name: getattr(os, name) for name in ('stat', 'lstat', 'readlink', 'open', 'access')}

        def spy(name):
            def look(path, *a, **k):
                if isinstance(path, (str, bytes, os.PathLike)) and os.path.normpath(os.fsdecode(path)) in names:
                    seen.append((name, os.fsdecode(path)))
                return real[name](path, *a, **k)
            return look
        for name in real:
            mock.patch.object(os, name, spy(name)).start()
        self.addCleanup(mock.patch.stopall)
        return seen


class Lookups(Home):
    def looked_up(self, seen):
        """{auth file: how many lookups}."""
        out = {}
        for _name, path in seen:
            out[path] = out.get(path, 0) + 1
        return out

    def test_the_checks_of_a_document_look_up_an_auth_file_once_and_then_never(self):
        seen = self.watched()
        for _ in range(20):
            with util.open_safe(self.doc, binary=True) as f:                 # the way that opens content: the deny check is made fresh each time
                f.read()
            self.assertEqual(util.stat_regular(self.doc)[0], self.doc)         # the way that only looks at metadata
            self.assertEqual(util.stat_plain(self.doc)[0], self.doc)
            self.assertFalse(util.denied_file(self.doc))
            self.assertFalse(util.denied_file(self.doc, fresh=False))
        self.assertEqual({n: c for n, c in self.looked_up(seen).items() if c > 1}, {})                  # (0.2.1 and the first 0.3.0: once for each check, 20 opens = 80 and more)
        seen.clear()
        for _ in range(20):
            with util.open_safe(self.doc, binary=True) as f:
                f.read()
            util.stat_regular(self.doc)
        self.assertEqual(seen, [])

    def test_what_the_judgment_asks_of_a_document_does_not_look_up_an_auth_file(self):
        seen = self.watched()
        cat = U.Catalog()
        cat.begin()
        self.assertTrue(cat.sha1(self.doc))                                  # the sha opens the file (the way that opens content)
        cat.stat(self.doc), cat.listdir(self.doc_dir), cat.realpath(self.doc)
        reads = cat.reads()
        seen.clear()
        for _ in range(20):
            self.assertFalse(U.Catalog.changed(reads))
        s = type('S', (), {'agents': {}, '_file_cache': {}, '_head_cache': {}, 'cwd': self.home, 'walked_units': [os.path.dirname(self.doc_dir)]})()
        for _ in range(3):
            debates.judge(s, {})                                             # the page of a debate: its cells are counted, its brief is read
        self.assertEqual(debates.file_info(s, self.doc)['lines'], 2)
        self.assertEqual(seen, [])                                           # (the sha, a judgment and a page of it: after the first look, none)

    def test_a_folder_that_is_not_there_holds_no_link_and_costs_no_lookup_at_all(self):
        os.remove(self.auth[2])
        os.rmdir(self.codex)                                                 # no Codex folder (or one the capture does not list): nothing in it to look up
        util._DENY_ROOTS['e'] = None
        util._AUTH_LINKS['e'] = None
        self.old()
        seen = self.watched()
        for _ in range(5):
            with util.open_safe(self.doc, binary=True) as f:
                f.read()
        self.assertEqual([x for x in seen if x[1] == self.auth[2]], [])      # nothing in a folder that is not there is looked up
        self.assertLessEqual(max(self.looked_up(seen).values(), default=0), 1)
        self.assertTrue(util.denied_file(self.auth[2]))                      # still refused by its name

    def test_the_auth_files_are_still_refused(self):
        for path in self.auth + [self.missing]:
            self.assertTrue(util.denied_file(path), path)
            self.assertTrue(util.denied_file(path, fresh=False), path)
            self.assertIsNone(util.stat_regular(path), path)
            with self.assertRaises(util.Denied):
                util.open_safe(path)
        self.assertTrue(util.denied_file(os.path.join(self.claude, 'settings.json')))
        self.assertTrue(util.denied_file(os.path.join(self.codex, 'sessions', 'x.json')))

    def test_a_link_to_an_auth_file_and_an_auth_file_that_is_a_link_are_refused_too(self):
        link = os.path.join(self.doc_dir, 'B.md')
        os.symlink(self.auth[0], link)                                       # a document that leads to the credentials
        self.assertIsNone(util.stat_regular(link))
        with self.assertRaises(util.Denied):
            util.open_safe(link, strict=False)
        target = self.put(self.home, 'elsewhere.md', 'x\n')
        os.remove(self.auth[2])
        os.symlink(target, self.auth[2])                                     # the Codex auth file is itself a link to another file: that file is the secret
        self.old()
        self.assertTrue(util.denied_file(os.path.realpath(target)))
        with self.assertRaises(util.Denied):
            util.open_safe(target, strict=False)

    def test_an_auth_link_is_followed_again_at_each_check_and_the_others_are_not_looked_up(self):
        target = self.put(self.home, 'elsewhere.md', 'x\n')
        os.remove(self.auth[2])
        os.symlink(target, self.auth[2])
        self.old()
        seen = self.watched()
        for _ in range(20):
            self.assertTrue(util.denied_file(os.path.realpath(target)))
        link = [x for x in seen if x[1] == self.auth[2]]
        self.assertGreaterEqual(len(link), 20)                                           # the link: at every check (O19)
        self.assertLessEqual(len(link), 20 * 3)
        self.assertLessEqual(max(self.looked_up([x for x in seen if x[1] != self.auth[2]]).values(), default=0), 1)     # the others: once each, on the first check
        seen.clear()
        for _ in range(20):
            self.assertTrue(util.denied_file(os.path.realpath(target)))
        self.assertEqual([x for x in seen if x[1] != self.auth[2]], [])                  # and then not at all

    def test_an_auth_link_that_is_repointed_is_refused_at_its_new_target_with_no_change_in_its_folder(self):
        talk = os.path.join(self.home, 'work', 'talk')
        x, y = self.put(talk, 'x.json', '{"k": 1}\n'), self.put(talk, 'y.json', '{"k": 2}\n')
        os.remove(self.auth[2])
        os.symlink(x, self.auth[2])                                                      # ~/.codex/auth.json -> work/talk/x.json
        self.old()
        self.assertEqual((util.denied_file(x), util.denied_file(y)), (True, False))
        os.symlink(y, self.auth[2] + '.new')
        os.replace(self.auth[2] + '.new', self.auth[2])                                  # repointed (and the folder time put back, as if nothing moved)
        self.old()
        self.assertEqual((util.denied_file(x), util.denied_file(y)), (False, True))
        with self.assertRaises(util.Denied):
            util.open_safe(y, strict=False)

    def test_a_link_in_the_middle_of_the_way_of_an_auth_link_is_repointed_and_the_new_target_is_refused(self):
        store = os.path.join(self.home, 'store')
        a, b = self.put(os.path.join(store, 'a'), 'cx.json', '{"t": "a"}\n'), self.put(os.path.join(store, 'b'), 'cx.json', '{"t": "b"}\n')
        os.symlink(os.path.join(store, 'a'), os.path.join(store, 'cur'))                 # store/cur -> store/a
        os.remove(self.auth[2])
        os.symlink(os.path.join(store, 'cur', 'cx.json'), self.auth[2])                  # ~/.codex/auth.json -> store/cur/cx.json
        for d in (store, os.path.join(store, 'a'), os.path.join(store, 'b')):
            os.utime(d, (time.time() - 3600,) * 2)
        self.old()
        self.assertEqual((util.denied_file(a), util.denied_file(b)), (True, False))
        os.symlink(os.path.join(store, 'b'), os.path.join(store, 'cur.new'))
        os.replace(os.path.join(store, 'cur.new'), os.path.join(store, 'cur'))           # store/cur -> store/b: ~/.codex does not change
        self.old()
        self.assertEqual((util.denied_file(a), util.denied_file(b)), (False, True))
        with self.assertRaises(util.Denied):
            util.open_safe(b, strict=False)

    def test_a_file_that_becomes_an_auth_link_is_refused_too(self):
        target = self.put(self.home, 'elsewhere.md', 'x\n')
        self.assertFalse(util.denied_file(target))                                       # (the names are regular files now)
        os.remove(self.auth[2])
        os.symlink(target, self.auth[2])
        self.old()
        self.assertTrue(util.denied_file(target))                                        # a made entry moves its folder: the name is asked again

if __name__ == '__main__':
    unittest.main()
