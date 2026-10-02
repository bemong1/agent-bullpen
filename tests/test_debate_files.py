"""The three ways debate state handles files:
- The way that opens content (reading brief, counting lines, -o sha1, /api/file): right before opening, the deny decision is redone without any cache. Only regular files are opened (FIFOs, devices and folders are refused), and the amount read has a cap.
- The way that only looks at metadata (debate cells, the final in the brief table): it sees only existence, size and time. The cell stands even for a dot path or secret name (only the line count is 0). A link to a credential file, and anything that is not a regular file, counts as a missing file.
- The way that searches a folder for candidates (auto_final candidates, docs and finals lists): dot paths and secret names count as missing files too.

    python3 -m unittest discover -s tests
"""
import builtins
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import patched, server, start_patches  # noqa: E402

from board import debates, util  # noqa: E402


def write(path, text='x\n', mode='w'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)
    return path


def stamp(path, t):
    os.utime(path, (t, t))


def run_with_timeout(fn, seconds=5):
    """Runs fn() in another thread and returns (finished, result). If it blocks while opening a file, it returns unfinished (the thread is a daemon)."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(seconds)
    return (not t.is_alive()), (box[0] if box else None)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.claude = os.path.join(self.root, 'cfg')
        self.cred = write(os.path.join(self.claude, '.credentials.json'), '{"claudeAiOauth": {"accessToken": "FAKE"}}\nline2\nline3\n')
        self.unit = os.path.join(self.root, 'proj', 't1')
        os.makedirs(os.path.join(self.unit, 'r1'))
        start_patches(self, CLAUDE_HOME=self.claude, DENY_FILES=(self.cred,), CODEX_HOME=os.path.join(self.root, 'codex'))
        self.s = types.SimpleNamespace(_file_cache={}, _head_cache={})
        self.addCleanup(self.unblock_all)
        self.fifos = []

    def fifo(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.mkfifo(path)
        self.fifos.append(path)
        return path

    def unblock_all(self):
        for p in self.fifos:                  # release it by opening for both reading and writing so no blocked thread is left behind (O_RDWR does not block on a FIFO)
            try:
                os.close(os.open(p, os.O_RDWR | os.O_NONBLOCK))
            except OSError:
                pass

    def rows(self, mtime):
        return [{'cells': [{'state': 'done', 'mtime': mtime}]}]


class StatPlain(Fixture):
    def test_policy(self):
        ok = write(os.path.join(self.unit, 'r1', 'A.md'))
        self.assertEqual(util.stat_plain(ok)[0], ok)
        fifo = self.fifo(os.path.join(self.unit, 'f.md'))
        link = os.path.join(self.unit, 'link.md')
        os.symlink(self.cred, link)
        for bad in (fifo, link, self.cred, os.path.join(self.unit, 'r1'), os.path.join(self.unit, 'nope.md'),
                    write(os.path.join(self.unit, 'credentials.md')), write(os.path.join(self.unit, 'api_token.md')),
                    write(os.path.join(self.root, '.hidden', 'r1', 'A.md'))):
            self.assertIsNone(util.stat_plain(bad), bad)

    def test_link_to_ordinary_file_still_works(self):
        real = write(os.path.join(self.root, 'elsewhere', 'plan.md'))
        link = os.path.join(self.unit, 'plan.md')
        os.symlink(real, link)
        self.assertEqual(util.stat_plain(link)[0], real)


class StatRegular(Fixture):
    """The way that looks at metadata only, without opening: dot paths and secret names pass; links to credential files, FIFOs and folders count as missing files."""

    def test_policy(self):
        for ok in (write(os.path.join(self.root, '.wt', 'work', 'r1', 'A.md')), write(os.path.join(self.unit, 'token_budget.md')),
                   write(os.path.join(self.unit, 'credentials.md'))):
            self.assertEqual(util.stat_regular(ok)[0], ok, ok)
            self.assertIsNone(util.stat_plain(ok), ok)                         # the way that searches a folder is stricter
        link = os.path.join(self.unit, 'link.md')
        os.symlink(self.cred, link)
        for bad in (link, self.cred, self.fifo(os.path.join(self.unit, 'f.md')), os.path.join(self.unit, 'r1'), os.path.join(self.unit, 'nope.md')):
            self.assertIsNone(util.stat_regular(bad), bad)


class FileInfoAndHead(Fixture):
    def test_credentials_link_is_nothing_and_never_opened(self):
        link = os.path.join(self.unit, 'rulings.md')
        os.symlink(self.cred, link)
        opened, real_open, real_os_open = [], builtins.open, os.open

        def spy_open(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        def spy_os_open(path, *a, **kw):
            opened.append(str(path))
            return real_os_open(path, *a, **kw)
        with mock.patch.object(builtins, 'open', spy_open), mock.patch.object(os, 'open', spy_os_open):
            self.assertIsNone(debates.file_info(self.s, link))
            self.assertIsNone(debates.file_info(self.s, link, strict=True))
            self.assertEqual(debates.read_head(self.s, link), '')
            stamp(write(os.path.join(self.unit, 'r1', 'A.md')), 100)
            self.assertEqual(debates.auto_final(self.s, self.unit, self.rows(100)), (None, None))
        self.assertEqual([p for p in opened if '.credentials.json' in p], [])
        self.assertNotIn(link, self.s._file_cache)

    def test_hidden_and_secret_names_keep_the_cell_but_not_the_lines(self):
        # a cell (report) looks at metadata only: even with a dot path or secret name the file exists and only the line count is 0. Only when searching a folder (strict) is it a missing file
        for p in (os.path.join(self.root, '.hid', 'r1', 'A.md'), os.path.join(self.unit, 'r1', 'secrets.md'),
                  os.path.join(self.unit, 'r1', 'token_budget.md')):
            write(p, 'a\nb\n')
            info = debates.file_info(self.s, p)
            self.assertEqual((info['exists'], info['size'], info['lines']), (True, 4, 0), p)
            self.assertIsNone(debates.file_info(self.s, p, strict=True), p)
        ok = write(os.path.join(self.unit, 'r1', 'A.md'), 'a\nb\nc\n')
        self.assertEqual(debates.file_info(self.s, ok)['lines'], 3)
        self.assertEqual(debates.file_info(self.s, ok, strict=True)['lines'], 3)

    def test_head_ignores_name_rules_but_not_credentials(self):
        # brief.md is a fixed name, so it is read even on a dot path (otherwise the table and title of a debate under a dot folder would not show up). A link to a credential file is not read
        p = write(os.path.join(self.root, '.wt', 'work', 'brief.md'), '# 제목\n')
        self.assertEqual(debates.read_head(self.s, p), '# 제목\n')
        link = os.path.join(self.unit, 'brief.md')
        os.symlink(self.cred, link)
        self.assertEqual(debates.read_head(self.s, link), '')

    def test_fifo_does_not_block(self):
        fifo = self.fifo(os.path.join(self.unit, 'r1', 'A.md'))
        for fn in (lambda: debates.file_info(self.s, fifo), lambda: debates.read_head(self.s, fifo)):
            done, res = run_with_timeout(fn)
            self.assertTrue(done, '일반 파일이 아닌 것을 열려고 막혔다')
            self.assertIn(res, (None, ''))

    def test_directory_is_nothing(self):
        self.assertIsNone(debates.file_info(self.s, os.path.join(self.unit, 'r1')))

    def test_line_count_is_capped_at_file_max(self):
        line = b'x' * 99 + b'\n'
        n = (util.FILE_MAX // 100) + 2000
        p = os.path.join(self.unit, 'r1', 'big.md')
        write(p, line * n, 'wb')
        info = debates.file_info(self.s, p)
        self.assertEqual(info['size'], n * 100)                       # size is the real size
        self.assertEqual(info['lines'], util.FILE_MAX // 100)        # line count only covers the first FILE_MAX bytes
        self.assertLess(info['lines'], n)

    def test_bounded_read(self):
        p = os.path.join(self.unit, 'r1', 'big.md')
        write(p, b'y' * (util.FILE_MAX + 100000), 'wb')
        read_sizes = []
        real = util.open_nofollow

        class Spy:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

            def read(self, n=-1):
                b = self.f.read(n)
                read_sizes.append((n, len(b)))
                return b

        def spy(path, binary=False):
            return Spy(real(path, binary))
        with patched(open_nofollow=spy):
            debates.file_info(self.s, p)
        self.assertTrue(read_sizes and all(n > 0 for n, _ in read_sizes))      # there is no read() without a size
        self.assertLessEqual(sum(got for _, got in read_sizes), util.FILE_MAX)

    def test_head_is_capped(self):
        p = write(os.path.join(self.unit, 'brief.md'), 'z' * (debates.HEAD_CHARS + 500))
        self.assertEqual(len(debates.read_head(self.s, p)), debates.HEAD_CHARS)

    def test_info_is_cached_until_mtime_or_size_changes(self):
        p = write(os.path.join(self.unit, 'r1', 'A.md'), 'a\n')
        with mock.patch.object(debates, 'count_lines', wraps=debates.count_lines) as c:
            debates.file_info(self.s, p)
            debates.file_info(self.s, p)
            self.assertEqual(c.call_count, 1)
            write(p, 'a\nb\n')
            self.assertEqual(debates.file_info(self.s, p)['lines'], 2)
            self.assertEqual(c.call_count, 2)


class AutoFinalCandidates(Fixture):
    def setUp(self):
        super().setUp()
        stamp(write(os.path.join(self.unit, 'r1', 'A.md')), 100)

    def test_only_the_winner_is_read(self):
        for name, t in (('notes.md', 150), ('rulings.md', 160), ('todo.md', 170), ('misc.md', 180)):
            stamp(write(os.path.join(self.unit, name), 'a\nb\n'), t)
        with mock.patch.object(debates, 'count_lines', wraps=debates.count_lines) as c:
            f, info = debates.auto_final(self.s, self.unit, self.rows(100))
        self.assertEqual((f, info['lines']), ('rulings.md', 2))        # a conclusion-like name comes first
        self.assertEqual(c.call_count, 1)                              # the candidate was chosen by stat, and the lines of only one file are counted

    def test_fifo_candidate_is_skipped_without_blocking(self):
        stamp(write(os.path.join(self.unit, 'summary.md'), 'ok\n'), 150)
        self.fifo(os.path.join(self.unit, 'rulings.md'))
        done, res = run_with_timeout(lambda: debates.auto_final(self.s, self.unit, self.rows(100)))
        self.assertTrue(done, 'FIFO 후보에서 막혔다')
        self.assertEqual(res[0], 'summary.md')

    def test_credentials_link_candidate_is_skipped(self):
        stamp(write(os.path.join(self.unit, 'summary.md'), 'ok\n'), 150)
        link = os.path.join(self.unit, 'rulings.md')
        os.symlink(self.cred, link)
        os.utime(link, (160, 160), follow_symlinks=False)
        f, info = debates.auto_final(self.s, self.unit, self.rows(100))
        self.assertEqual(f, 'summary.md')

    def test_secret_named_markdown_is_not_a_candidate(self):
        stamp(write(os.path.join(self.unit, 'secrets.md'), 'x\n'), 150)
        stamp(write(os.path.join(self.unit, 'credentials_final.md'), 'x\n'), 160)
        self.assertEqual(debates.auto_final(self.s, self.unit, self.rows(100)), (None, None))


class DebateView(Fixture):
    """The same policy applies to the debate shape _debate produces (a document that cannot be opened is not put in the docs list, the cells or the final)."""

    def build(self):
        write(os.path.join(self.unit, 'brief.md'), '# T1\n')
        write(os.path.join(self.unit, 'r1', 'A.md'), 'a\nb\n')
        write(os.path.join(self.unit, 'notes.md'), 'n\n')
        write(os.path.join(self.unit, 'credentials.md'), 'c\n')
        self.fifo(os.path.join(self.unit, 'pipe.md'))
        os.makedirs(os.path.join(self.unit, 'dir.md'))
        os.symlink(self.cred, os.path.join(self.unit, 'cred_link.md'))
        s = types.SimpleNamespace(_file_cache={}, _head_cache={}, agents={})
        return debates._debate(s, self.unit, [self.unit], {}, {}, {}, {}, {}, {})

    def test_docs_list_only_openable_documents(self):
        done, d = run_with_timeout(self.build)
        self.assertTrue(done, '토론 상태 계산이 막혔다')
        t = d['topics'][0]
        self.assertEqual([x['name'] for x in t['docs']], ['brief.md', 'notes.md'])
        self.assertEqual(t['rows'][0]['cells'][0]['state'], 'done')
        self.assertEqual(t['rows'][0]['cells'][0]['lines'], 2)                  # an ordinary report is counted as is

    def test_final_dir_lists_regular_files_only(self):
        fin = os.path.join(self.unit, 'final')
        write(os.path.join(fin, 'plan.md'), '1\n2\n3\n')
        os.makedirs(os.path.join(fin, 'rulings'))
        self.fifo(os.path.join(fin, 'pipe.md'))
        os.symlink(self.cred, os.path.join(fin, 'cred.md'))
        s = types.SimpleNamespace(_file_cache={}, _head_cache={}, agents={})
        done, d = run_with_timeout(lambda: debates._debate(s, self.unit, [self.unit], {}, {}, {}, {}, {}, {}))
        self.assertTrue(done)
        self.assertEqual([(f['name'], f['lines']) for f in d['finals']], [('plan.md', 3)])


class FileEndpoint(Fixture):
    """The document view (/api/file) does not open a FIFO and answers right away."""

    def test_fifo_is_refused_quickly(self):
        from test_stage1 import call
        import threading as th
        write(os.path.join(self.unit, 'brief.md'), '# T1\n')
        fifo = self.fifo(os.path.join(self.unit, 'r1', 'pipe.md'))
        s = server.Session.__new__(server.Session)
        s.lock = th.RLock()
        s.agents = {}
        s.debates = lambda statuses: ([{'root': self.unit}], {})
        with patched(HOME=self.root):
            done, res = run_with_timeout(lambda: call('/api/file?path=%s&session=11111111-2222-4333-8444-555555555555' % fifo, s=s))
        self.assertTrue(done, 'FIFO 열기에서 막혔다')
        self.assertEqual(res[0], 404)


class DenyCacheRace(Fixture):
    """The short cache of realpaths for the deny decision (for the non-opening way only) does not leak into the way that opens content.
    If a credentials link is warmed into the cache and then repointed at another file within the TTL, the new target is denied but the stale cache does not know."""

    def setUp(self):
        super().setUp()
        os.remove(self.cred)
        self.old = write(os.path.join(self.claude, 'old.json'), '{}\n')
        os.symlink(self.old, self.cred)                                   # .credentials.json -> old.json
        self.victim = write(os.path.join(self.unit, 'brief.md'), '# 비밀 제목\nsecond\n')
        self.warm = write(os.path.join(self.unit, 'r1', 'A.md'), 'a\n')
        start_patches(self, DENY_ROOTS_TTL=3600.0)                        # so the cache does not expire by itself while the test runs
        util._DENY_ROOTS['e'] = None
        util._REAL_DIRS.clear()
        self.addCleanup(lambda: (util._DENY_ROOTS.update(e=None), util._REAL_DIRS.clear()))
        self.assertIsNotNone(util.stat_regular(self.warm))                # warm the cache: realpath of the deny list = old.json
        os.remove(self.cred)
        os.symlink(self.victim, self.cred)                                # within the TTL it is repointed: .credentials.json -> brief.md

    def test_premise_cache_is_stale_but_fresh_check_denies(self):
        real = os.path.realpath(self.victim)
        self.assertFalse(util.denied_file(real, fresh=False))             # a stale cache does not know (used only on the non-opening way)
        self.assertTrue(util.denied_file(real))                           # a fresh check denies it

    def test_every_opening_path_checks_fresh(self):
        opened, real_nofollow = [], util.open_nofollow

        def spy(real, binary=False):
            opened.append(real)
            return real_nofollow(real, binary)
        with patched(open_nofollow=spy):
            self.assertEqual(debates.read_head(self.s, self.victim), '')                   # reading brief
            self.assertIsNone(debates.count_lines(self.victim))                            # counting lines: denied
            self.assertIsNone(debates.file_info(self.s, self.victim))                      # cell and final: becomes a missing file at the line-count step
            self.assertIsNone(debates.file_info(self.s, self.victim, strict=True))
            with self.assertRaises(util.Denied):
                util.open_safe(self.victim)                                                # common entry point
            self.assertIsNone(server.CodexLinker(server.Session.__new__(server.Session))._sha_file(self.victim))   # -o sha1
        self.assertNotIn(os.path.realpath(self.victim), opened)                            # the target file was never opened

    def test_file_endpoint_checks_fresh(self):
        from test_stage1 import call
        s = server.Session.__new__(server.Session)
        s.lock = threading.RLock()
        s.agents = {}
        s.debates = lambda statuses: ([{'root': self.unit}], {})
        with patched(HOME=self.root):
            code, body = call('/api/file?path=%s&session=11111111-2222-4333-8444-555555555555' % self.victim, s=s)
        self.assertEqual(code, 403)
        self.assertNotIn('비밀 제목', str(body))

    def test_denied_between_check_and_open_is_403(self):
        # no content is sent even when the target becomes denied after allowed_file let it through and right before it is opened
        from test_stage1 import call
        s = types.SimpleNamespace(allowed_file=lambda path: os.path.realpath(self.victim), version=1, lock=threading.RLock())
        code, body = call('/api/file?path=%s&session=11111111-2222-4333-8444-555555555555' % self.victim, s=s)
        self.assertEqual((code, body), (403, {'error': 'not allowed', 'error_code': 'not_allowed'}))


class DotPathDebate(Fixture):
    """A debate cell and the final in the brief table look at metadata only: a report under a dot folder (e.g. a project under ~/.config, or .wt/work joined by a link)
    or with token/secret in its name still gets its cell and its final shows. The side that opens content (/api/file) and the lists found by searching folders stay strict."""
    TABLE = '# 토론\n\n| 주제 | 폴더 | 선행 | 최종 산출물 |\n|---|---|---|---|\n| T1 | `t1/` | — | `final/rulings.md` |\n| T2 | `t2/` | — | `final/token_budget.md` |\n'

    def build(self, base):
        root = os.path.join(base, 'work')
        write(os.path.join(root, 'brief.md'), self.TABLE)
        for t in ('t1', 't2'):
            write(os.path.join(root, t, 'brief.md'), '# %s\n' % t)
            for who in ('A', 'B', 'C'):
                write(os.path.join(root, t, 'r1', who + '.md'), 'x\ny\n')
            write(os.path.join(root, t, 'notes.md'), 'n\n')
        write(os.path.join(root, 'final', 'rulings.md'), '1\n2\n3\n')
        write(os.path.join(root, 'final', 'token_budget.md'), '1\n')
        return root

    def debate(self, root):
        s = types.SimpleNamespace(_file_cache={}, _head_cache={}, agents={})
        return debates._debate(s, root, [os.path.join(root, 't1'), os.path.join(root, 't2')], {}, {}, {}, {}, {}, {})

    def check_cells_and_finals(self, d, lines):
        t1, t2 = d['topics']
        for t in (t1, t2):
            self.assertEqual([c['state'] for r in t['rows'] for c in r['cells']], ['done'] * 3, t['key'])      # all 6 cells are done
        self.assertEqual([c['lines'] for r in t1['rows'] for c in r['cells']], [lines] * 3)
        self.assertEqual((t1['final']['rel'], t1['final']['exists'], t1['final']['lines']), ('final/rulings.md', True, 3 if lines else 0))
        self.assertEqual((t2['final']['rel'], t2['final']['exists'], t2['final']['lines']), ('final/token_budget.md', True, 0))   # token in the name

    def test_dot_folder_through_a_link(self):
        # the case where work in a synthetic HOME is moved to .wt/work and joined by a link (the realpath contains a dot folder)
        real = self.build(os.path.join(self.root, '.wt'))
        link = os.path.join(self.root, 'work')
        os.symlink(real, link)
        d = self.debate(link)
        self.check_cells_and_finals(d, lines=0)                       # dot path, so only the line count is 0
        self.assertEqual([x['name'] for x in d['topics'][0]['docs']], [])        # the lists found by searching folders are strict
        self.assertEqual(d['finals'], [])

    def test_dot_folder_directly(self):
        d = self.debate(self.build(os.path.join(self.root, '.config', 'proj')))
        self.check_cells_and_finals(d, lines=0)

    def test_secret_named_report_in_a_normal_folder(self):
        d = self.debate(self.build(os.path.join(self.root, 'proj')))
        self.check_cells_and_finals(d, lines=2)                       # a report on a normal path is counted down to its lines
        t1, t2 = d['topics']
        self.assertEqual([x['name'] for x in t1['docs']], ['brief.md', 'notes.md'])
        self.assertEqual(sorted(f['name'] for f in d['finals']), ['rulings.md'])       # token_budget.md is not listed

    def test_file_endpoint_stays_strict(self):
        from test_stage1 import call
        root = self.build(os.path.join(self.root, '.wt'))
        s = server.Session.__new__(server.Session)
        s.lock = threading.RLock()
        s.agents = {}
        s.debates = lambda statuses: ([{'root': root}], {})
        sid = '&session=11111111-2222-4333-8444-555555555555'
        with patched(HOME=self.root):
            for rel in ('t1/r1/A.md', 'final/rulings.md', 'final/token_budget.md'):
                self.assertEqual(call('/api/file?path=%s%s' % (os.path.join(root, rel), sid), s=s)[0], 403, rel)
        root = self.build(os.path.join(self.root, 'proj'))
        s.debates = lambda statuses: ([{'root': root}], {})
        with patched(HOME=self.root):
            self.assertEqual(call('/api/file?path=%s%s' % (os.path.join(root, 't1/r1/A.md'), sid), s=s)[0], 200)
            self.assertEqual(call('/api/file?path=%s%s' % (os.path.join(root, 'final/token_budget.md'), sid), s=s)[0], 403)

    def test_credentials_link_report_is_still_nothing(self):
        root = self.build(os.path.join(self.root, 'proj'))
        os.remove(os.path.join(root, 't1', 'r1', 'B.md'))
        os.symlink(self.cred, os.path.join(root, 't1', 'r1', 'B.md'))
        t1 = self.debate(root)['topics'][0]
        self.assertEqual([c['state'] for r in t1['rows'] for c in r['cells']], ['done', 'waiting', 'done'])    # a cell that is a link to a credential file is a missing file


class LinkedGuides(Fixture):
    """The loose way (reading brief, line count of report cells, final in the brief table) also applies the dot-path and secret-name rules to the target when the file is joined by a link.
    A link cannot pull the content of a hidden or secret file into a debate title or line count. A regular file in place is read even under a dot folder."""
    SECRET = '# FAKE_PRIVATE_TOKEN\nline2\nline3\nline4\nline5\n'

    def setUp(self):
        super().setUp()
        self.wt = os.path.join(self.root, 'repo', '.claude', 'worktrees', 'wt1')
        self.deb = os.path.join(self.wt, 'debate')
        os.makedirs(os.path.join(self.deb, 'r1'))
        self.secret = write(os.path.join(self.root, 'home', '.config', 'secrets.md'), self.SECRET)
        self.hidden = write(os.path.join(self.root, 'home', '.docker', 'config.json'), '# FAKE_HIDDEN\n{}\n')
        self.token = write(os.path.join(self.root, 'home', 'docs', 'tokens.md'), '# FAKE_TOKENS\n')
        self.notes = write(os.path.join(self.root, 'home', 'docs', 'notes.md'), '# 평범한 제목\n1\n2\n')

    def head(self, target):
        link = os.path.join(self.deb, 'brief.md')
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link)
        return debates.read_head(types.SimpleNamespace(_head_cache={}), link)

    def test_brief_link_to_hidden_or_secret_target_is_not_read(self):
        for target in (self.secret, self.hidden, self.token):
            self.assertEqual(self.head(target), '', target)
        chain = os.path.join(self.root, 'home', 'step.md')                       # a link to a link
        os.symlink(self.secret, chain)
        self.assertEqual(self.head(chain), '')

    def test_title_does_not_carry_the_target(self):
        write(os.path.join(self.deb, 'r1', 'A.md'), 'a\n')
        os.symlink(self.secret, os.path.join(self.deb, 'brief.md'))
        s = types.SimpleNamespace(_file_cache={}, _head_cache={}, agents={})
        d = debates._debate(s, self.deb, [self.deb], {}, {}, {}, {}, {}, {})
        self.assertNotIn('FAKE_PRIVATE_TOKEN', repr(d))                           # no text of the target anywhere in the title, roles or table
        self.assertEqual(d['title'], 'debate')                                    # it could not be read, so the folder name

    def test_brief_link_to_an_ordinary_document_still_reads(self):
        self.assertEqual(self.head(self.notes), '# 평범한 제목\n1\n2\n')

    def test_in_place_brief_in_a_hidden_project_still_reads(self):
        for base in (self.deb, os.path.join(self.root, '.config', 'proj', 'debate')):
            p = write(os.path.join(base, 'brief.md'), '# 제자리\n')
            self.assertEqual(debates.read_head(types.SimpleNamespace(_head_cache={}), p), '# 제자리\n', base)

    def test_directory_link_alone_reaches_only_the_same_file_name(self):
        # an in-place brief.md with only a folder link in its path is read (a debate joined to a dot folder like .wt/work). A folder link reaches only the file of the same name
        real = os.path.join(self.root, '.wt', 'work')
        write(os.path.join(real, 'brief.md'), '# 링크로 이은 폴더\n')
        link = os.path.join(self.root, 'work')
        os.symlink(real, link)
        self.assertEqual(debates.read_head(types.SimpleNamespace(_head_cache={}), os.path.join(link, 'brief.md')), '# 링크로 이은 폴더\n')

    def test_open_safe_rules(self):
        link = os.path.join(self.deb, 'brief.md')
        os.symlink(self.secret, link)
        with self.assertRaises(util.Denied) as cm:
            util.open_safe(link, strict=False)
        self.assertEqual(cm.exception.why, 'hidden')
        with util.open_safe(self.secret, strict=False) as f:                       # a hidden file in place (not a link) opens on the loose way
            self.assertIn('FAKE_PRIVATE_TOKEN', f.read())
        with self.assertRaises(util.Denied):                                       # the strict way refuses even in place
            util.open_safe(self.secret)

    def test_report_link_lines_do_not_count_the_target(self):
        os.makedirs(os.path.join(self.deb, 'r1'), exist_ok=True)
        os.symlink(self.secret, os.path.join(self.deb, 'r1', 'B.md'))             # a hidden/secret target of 5 lines
        os.symlink(self.notes, os.path.join(self.deb, 'r1', 'C.md'))              # an ordinary target of 3 lines
        write(os.path.join(self.deb, 'r1', 'A.md'), 'a\nb\n')
        s = types.SimpleNamespace(_file_cache={}, _head_cache={}, agents={})
        d = debates._debate(s, self.deb, [self.deb], {}, {}, {}, {}, {}, {})
        cells = {r['p']: r['cells'][0] for r in d['topics'][0]['rows']}
        self.assertEqual((cells['A']['state'], cells['A']['lines']), ('done', 2))
        self.assertEqual((cells['B']['state'], cells['B']['lines']), ('done', 0))   # the cell stands (metadata) but the target's lines are not counted
        self.assertEqual((cells['C']['state'], cells['C']['lines']), ('done', 3))

    def test_table_final_link_lines(self):
        write(os.path.join(self.deb, 'brief.md'), '| 주제 | 폴더 | 선행 | 최종 산출물 |\n|---|---|---|---|\n| T | `t1/` | — | `final/rulings.md` |\n')
        t1 = os.path.join(self.deb, 't1')
        write(os.path.join(t1, 'r1', 'A.md'), 'a\n')
        os.makedirs(os.path.join(self.deb, 'final'))
        os.symlink(self.secret, os.path.join(self.deb, 'final', 'rulings.md'))
        s = types.SimpleNamespace(_file_cache={}, _head_cache={}, agents={})
        d = debates._debate(s, self.deb, [t1], {}, {}, {}, {}, {}, {})
        f = d['topics'][0]['final']
        self.assertEqual((f['rel'], f['exists'], f['lines']), ('final/rulings.md', True, 0))


class WorktreeRule(Fixture):
    """Claude Code --worktree works in <repository>/.claude/worktrees/<name>/. The dot-path rule does not treat just those three components as hidden."""

    def hidden(self, *parts):
        return util.hidden_or_secret(os.path.join(self.root, *parts))

    def test_only_the_three_components(self):
        self.assertFalse(self.hidden('repo', '.claude', 'worktrees', 'wt1', 'docs', 'r1', 'A.md'))
        self.assertFalse(self.hidden('repo', '.claude', 'worktrees', 'wt1', 'notes.md'))
        for parts in (('repo', '.claude', 'worktrees', 'wt1', '.env'),                   # any other dot component below that stays hidden
                      ('repo', '.claude', 'worktrees', 'wt1', '.git', 'config'),
                      ('repo', '.claude', 'worktrees', 'wt1', '.config', 'gh', 'hosts.yml'),
                      ('repo', '.claude', 'worktrees', 'wt1', '.mcp.json'),
                      ('repo', '.claude', 'worktrees', 'wt1', 'docs', 'credentials.md'),     # a secret name stays too
                      ('repo', '.claude', 'worktrees', 'wt1', 'token.json'),
                      ('repo', '.claude', 'worktrees', '.wt', 'a.md'),                       # a name starting with a dot
                      ('repo', '.claude', 'worktrees', 'notes.md'),                          # a file directly under worktrees (not a working folder)
                      ('repo', '.claude', 'settings.json'),                                  # elsewhere inside the repository's .claude
                      ('repo', '.claude', 'projects', 'x', 'a.md'),
                      ('.config', 'repo', '.claude', 'worktrees', 'wt1', 'a.md'),            # another dot component earlier in the path
                      ('repo', '.claude', 'worktrees', 'wt1', '.claude', 'worktrees', 'wt2', 'a.md')):    # a second .claude
            self.assertTrue(self.hidden(*parts), parts)

    def test_config_dir_itself_is_not_exempt(self):
        # does not apply to the Claude config folder (~/.claude): even with worktrees under it, it is still refused
        cfg = os.path.join(self.root, 'home', '.claude')
        with patched(CLAUDE_HOME=cfg):
            self.assertTrue(util.hidden_or_secret(os.path.join(cfg, 'worktrees', 'wt1', 'a.md')))
            self.assertFalse(util.hidden_or_secret(os.path.join(self.root, 'home', 'repo', '.claude', 'worktrees', 'wt1', 'a.md')))
        with patched(CLAUDE_HOME=os.path.join(self.root, 'repo', '.claude')):             # if the config folder is the repository's .claude, it is refused there too
            self.assertTrue(util.hidden_or_secret(os.path.join(self.root, 'repo', '.claude', 'worktrees', 'wt1', 'a.md')))

    def test_document_view_in_a_worktree_debate(self):
        from test_stage1 import call
        root = os.path.join(self.root, 'repo', '.claude', 'worktrees', 'wt1', 'debate')
        write(os.path.join(root, 'brief.md'), '# T\n')
        write(os.path.join(root, 'r1', 'A.md'), 'report\n')
        write(os.path.join(root, '.env'), 'KEY=1\n')
        write(os.path.join(root, '.git', 'config'), '[core]\n')
        write(os.path.join(root, 'credentials.md'), 'c\n')
        s = server.Session.__new__(server.Session)
        s.lock = threading.RLock()
        s.agents = {}
        s.debates = lambda statuses: ([{'root': root}], {})
        sid = '&session=11111111-2222-4333-8444-555555555555'
        with patched(HOME=self.root):
            self.assertEqual(call('/api/file?path=%s%s' % (os.path.join(root, 'r1', 'A.md'), sid), s=s)[0], 200)
            self.assertEqual(call('/api/file?path=%s%s' % (os.path.join(root, 'brief.md'), sid), s=s)[0], 200)
            for rel in ('.env', '.git/config', 'credentials.md'):
                self.assertEqual(call('/api/file?path=%s%s' % (os.path.join(root, rel), sid), s=s)[0], 403, rel)

    def test_agent_written_file_in_a_worktree(self):
        # a file an agent wrote (outside the debate folder) also opens when it is under a worktree. Dot components, secret names and credential files are still refused
        from test_stage1 import call
        wt = os.path.join(self.root, 'repo', '.claude', 'worktrees', 'wt1')
        ok = write(os.path.join(wt, 'notes.md'), 'n\n')
        bad = [write(os.path.join(wt, '.env.md'), 'k\n'), write(os.path.join(wt, 'secrets.md'), 's\n'), write(os.path.join(wt, '.git', 'x.md'), 'g\n')]
        s = server.Session.__new__(server.Session)
        s.lock = threading.RLock()
        a = server.Agent('a0123456789abcdef', {})
        a.writes = [{'ts': 1.0, 'path': p} for p in [ok] + bad]
        s.agents = {a.id: a}
        s.debates = lambda statuses: ([], {})
        sid = '&session=11111111-2222-4333-8444-555555555555'
        with patched(HOME=self.root):
            self.assertEqual(call('/api/file?path=%s%s' % (ok, sid), s=s)[0], 200)
            for p in bad:
                self.assertEqual(call('/api/file?path=%s%s' % (p, sid), s=s)[0], 403, p)


class ShaFile(Fixture):
    """The sha1 of the Codex -o file is opened under the same policy too."""

    def linker(self):
        return server.CodexLinker(server.Session.__new__(server.Session))

    def test_regular_file(self):
        import hashlib
        p = write(os.path.join(self.unit, 'r1', 'a.md'), 'hello\n')
        self.assertEqual(self.linker()._sha_file(p), hashlib.sha1(b'hello\n').hexdigest())

    def test_refused(self):
        link = os.path.join(self.unit, 'out.md')
        os.symlink(self.cred, link)
        fifo = self.fifo(os.path.join(self.unit, 'fifo.md'))
        big = write(os.path.join(self.unit, 'big.md'), b'z' * (util.FILE_MAX + 1), 'wb')
        lk = self.linker()
        self.assertIsNone(lk._sha_file(link))
        self.assertIsNone(lk._sha_file(big))
        done, res = run_with_timeout(lambda: lk._sha_file(fifo))
        self.assertTrue(done, 'FIFO에서 막혔다')
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
