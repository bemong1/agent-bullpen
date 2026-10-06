"""The jail of the frozen-HOME tools (tools/regress/home_jail.py): inside one process every lookup of the disk is answered from a capture, never from the live disk.

Each test makes a small live folder, freezes it with the capture builder of home_freeze.py, then CHANGES the live folder (other bytes, other sizes, other times, new files), and runs code inside the jail in a child process:
what it sees must be the capture, and what the capture does not hold must be refused and counted (`jail_miss`), not answered by the live disk. Only temporary folders are read; no board code is imported.

    python3 -m unittest tests.test_home_jail -q
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, 'tools', 'regress')
sys.path.insert(0, TOOLS)
import home_freeze  # noqa: E402

DRIVER = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, %(tools)r)
    import home_jail
    spec = json.load(open(sys.argv[1]))
    if spec.get('code'):
        jail = home_jail.jail_for(spec['cap'], 'replay', spec['code'], spec['writable'], extra_pass=spec.get('pass') or (), record_roots=spec.get('records') or ())
    else:
        jail = home_jail.Jail(home_jail.load_entries(spec['cap']), spec['cap'], 'replay', [spec['cap'], sys.prefix, sys.base_prefix] + list(spec.get('pass') or ()), spec['writable'], spec.get('records') or ())
    ns = {'os': os, 'sys': sys, 'jail': jail, 'result': None, 'json': json}
    code = compile(spec['body'], '<body>', 'exec')
    jail.install()
    try:
        exec(code, ns)
    finally:
        jail.uninstall()
    print(json.dumps({'result': ns['result'], 'misses': jail.miss_list(), 'total': jail.miss_total()}, default=str))
''')


class Fixture:
    """A live folder (`live`), the capture made from it (`cap`), a folder for the output of the run (`out`), and a way to run code inside the jail."""

    def __init__(self, case):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='home-jail-'))
        case.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live, self.cap, self.out, self.work = (os.path.join(self.tmp, n) for n in ('live', 'cap', 'out', 'work'))
        for d in (self.live, self.cap, self.out, self.work):
            os.makedirs(d)
        self.builder = home_freeze.Capture(self.cap)
        self.driver = os.path.join(self.work, 'driver.py')
        with open(self.driver, 'w') as f:
            f.write(DRIVER % {'tools': TOOLS})

    def put(self, rel, data, mtime=None):
        p = os.path.join(self.live, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        if mtime is not None:
            os.utime(p, ns=(mtime, mtime))
        return p

    def link(self, rel, target):
        p = os.path.join(self.live, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        os.symlink(target, p)
        return p

    def freeze(self, *paths, content=True):
        for p in paths:
            final = self.builder.ensure(p)
            if content and final and self.builder.entries[final]['t'] == 'file':
                self.builder.content(final)
        self.builder.write_disk(())

    def run(self, body, **spec):
        spec.update(cap=self.cap, writable=spec.get('writable', [self.out]), body=textwrap.dedent(body))
        sp = os.path.join(self.work, 'spec.json')
        with open(sp, 'w') as f:
            json.dump(spec, f)
        r = subprocess.run([sys.executable, '-I', self.driver, sp], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={'PYTHONDONTWRITEBYTECODE': '1', 'PATH': os.environ.get('PATH', '')})
        if r.returncode:
            raise AssertionError('the jailed run failed:\n' + r.stderr.decode('utf-8', 'replace')[-3000:])
        return json.loads(r.stdout.decode().strip().splitlines()[-1])


class LinkInMirror(unittest.TestCase):
    """A symbolic link of the capture is read in the original path space: open, stat, realpath and readlink give what the capture holds; the live target is not read (Sol's synthetic reproduction:
    an absolute link handed to open() read the live bytes)."""

    def setUp(self):
        self.fx = fx = Fixture(self)
        self.real = fx.put('data/real.txt', 'captured bytes', mtime=1_700_000_000_111_222_333)
        self.abs = fx.link('data/abs', self.real)
        self.rel = fx.link('data/rel', 'real.txt')
        self.dirlink = fx.link('dirlink', os.path.join(fx.live, 'data'))
        self.loop_a = fx.link('loop_a', os.path.join(fx.live, 'loop_b'))
        self.loop_b = fx.link('loop_b', os.path.join(fx.live, 'loop_a'))
        fx.freeze(self.abs, self.rel, os.path.join(self.dirlink, 'real.txt'), self.loop_a, self.loop_b)
        self.fx.put('data/real.txt', 'LIVE BYTES THAT MUST NOT BE READ, and longer', mtime=1_800_000_000_000_000_000)          # the live file moves on after the capture

    def test_open_stat_realpath_readlink_follow_the_capture(self):
        body = '''
            import stat
            r = {}
            r['abs'] = open(%(abs)r).read()
            r['rel'] = open(%(rel)r).read()
            r['dirlink'] = open(%(dirlink)r + '/real.txt').read()
            st = os.stat(%(abs)r)
            r['size'], r['mtime_ns'] = st.st_size, st.st_mtime_ns
            r['lstat_is_link'] = stat.S_ISLNK(os.lstat(%(abs)r).st_mode)
            r['realpath'] = os.path.realpath(%(abs)r)
            r['realpath_dir'] = os.path.realpath(%(dirlink)r + '/real.txt')
            r['readlink'] = os.readlink(%(abs)r)
            r['islink'] = os.path.islink(%(rel)r)
            r['exists'] = os.path.exists(%(abs)r)
            fd = os.open(%(abs)r, os.O_RDONLY)
            r['fstat_mtime_ns'] = os.fstat(fd).st_mtime_ns
            r['fd_read'] = os.read(fd, 100).decode()
            os.close(fd)
            result = r
        ''' % {'abs': self.abs, 'rel': self.rel, 'dirlink': self.dirlink}
        out = self.fx.run(body)
        r = out['result']
        self.assertEqual((r['abs'], r['rel'], r['dirlink']), ('captured bytes',) * 3)
        self.assertEqual((r['size'], r['mtime_ns']), (len('captured bytes'), 1_700_000_000_111_222_333))      # the capture's stat, not the live file's
        self.assertEqual(r['fstat_mtime_ns'], 1_700_000_000_111_222_333)                                         # fstat of an fd opened through the jail: the original too
        self.assertEqual(r['fd_read'], 'captured bytes')
        self.assertTrue(r['lstat_is_link'] and r['islink'] and r['exists'])
        self.assertEqual(r['realpath'], self.real)                                                              # the original path space, not a path in the mirror
        self.assertEqual(r['realpath_dir'], self.real)
        self.assertEqual(r['readlink'], self.real)
        self.assertEqual(out['total'], 0)

    def test_a_loop_and_nofollow(self):
        body = '''
            r = {}
            try:
                open(%(loop)r)
            except OSError as e:
                r['loop'] = e.errno
            try:
                os.open(%(abs)r, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as e:
                r['nofollow'] = e.errno
            result = r
        ''' % {'loop': self.loop_a, 'abs': self.abs}
        import errno
        r = self.fx.run(body)['result']
        self.assertEqual(r['loop'], errno.ELOOP)
        self.assertEqual(r['nofollow'], errno.ELOOP)


class DirEntryProxy(unittest.TestCase):
    """os.scandir hands out proxies: the name and path are in the original space, and stat(), is_file(), is_dir(), is_symlink() and inode() are the capture's answers. A real DirEntry
    would ask the live disk without passing through the wrapped os.stat (Sol's synthetic reproduction)."""

    def setUp(self):
        self.fx = fx = Fixture(self)
        self.dir = os.path.join(fx.live, 'list')
        fx.put('list/a.md', 'ten bytes!', mtime=1_700_000_000_000_000_007)
        fx.put('list/b.txt', 'b')
        os.makedirs(os.path.join(self.dir, 'sub'))
        fx.link('list/l', os.path.join(self.dir, 'a.md'))
        fx.builder.listing(self.dir)
        fx.builder.ensure(os.path.join(self.dir, 'l'))
        fx.builder.entries[self.dir]['names'].append('ghost')                                                  # listed, but nothing was captured of it
        fx.builder.write_disk(())
        self.a_ino = os.stat(os.path.join(self.dir, 'a.md')).st_ino
        fx.put('list/a.md', 'now a much longer live file, same name', mtime=1_900_000_000_000_000_000)           # the live disk moves on
        fx.put('list/c.md', 'a file that appeared after the capture')

    def test_entries_are_proxies_with_the_captured_answers(self):
        body = '''
            r = {}
            for e in sorted(os.scandir(%(dir)r), key=lambda e: e.name):
                if e.name == 'ghost':
                    continue
                r[e.name] = [type(e).__name__, e.path, e.is_file(), e.is_dir(), e.is_symlink(), e.is_file(follow_symlinks=False), e.stat().st_size, e.stat().st_mtime_ns, e.inode()]
            result = r
        ''' % {'dir': self.dir}
        out = self.fx.run(body)
        r = out['result']
        self.assertEqual(sorted(r), ['a.md', 'b.txt', 'l', 'sub'])                                              # c.md is not there: the listing is the capture's
        a = r['a.md']
        self.assertEqual(a[0], 'DirEntryProxy')
        self.assertEqual(a[1], os.path.join(self.dir, 'a.md'))
        self.assertEqual(a[2:5], [True, False, False])
        self.assertEqual((a[6], a[7], a[8]), (10, 1_700_000_000_000_000_007, self.a_ino))
        self.assertEqual(r['sub'][2:5], [False, True, False])
        link = r['l']                                                                                            # a link to a file: is_file follows it, is_symlink says what it is
        self.assertEqual((link[2], link[4], link[5]), (True, True, False))
        self.assertEqual(link[6], 10)

    def test_a_child_the_capture_does_not_have_goes_through_the_jail(self):
        body = '''
            r = {}
            for e in os.scandir(%(dir)r):
                if e.name == 'ghost':
                    try:
                        e.stat()
                    except FileNotFoundError:
                        r['ghost'] = 'refused'
            result = r
        ''' % {'dir': self.dir}
        out = self.fx.run(body)
        self.assertEqual(out['result'], {'ghost': 'refused'})
        self.assertEqual([(m['kind'], m['func'], os.path.basename(m['path'])) for m in out['misses']], [('lookup', 'stat', 'ghost')])

    def test_glob_and_walk_see_the_capture(self):
        body = '''
            import glob
            result = {'glob': sorted(os.path.basename(p) for p in glob.glob(%(dir)r + '/*.md')),
                      'walk': sorted(n for _, ds, fs in os.walk(%(dir)r) for n in ds + fs if n != 'ghost'),
                      'listdir': sorted(os.listdir(%(dir)r))}
        ''' % {'dir': self.dir}
        r = self.fx.run(body)['result']
        self.assertEqual(r['glob'], ['a.md'])
        self.assertNotIn('c.md', r['listdir'])
        self.assertEqual(r['walk'], ['a.md', 'b.txt', 'l', 'sub'])


class Miss(unittest.TestCase):
    """What the capture does not know is refused, whichever function asks, and counted: a FileNotFoundError and a line (path, function) in jail_miss. The live disk is never the answer."""

    def setUp(self):
        self.fx = fx = Fixture(self)
        fx.put('known/a.txt', 'a')
        fx.freeze(os.path.join(fx.live, 'known', 'a.txt'))
        self.unknown = fx.put('elsewhere/x.txt', 'live only')                                                    # exists on the disk, not in the capture
        self.unknown_dir = os.path.dirname(self.unknown)

    def test_every_wrapped_function_refuses(self):
        body = '''
            import glob, io
            r = {}
            def attempt(name, fn):
                try:
                    r[name] = ['returned', fn()]
                except FileNotFoundError:
                    r[name] = 'FileNotFoundError'
            p, d = %(p)r, %(d)r
            attempt('stat', lambda: os.stat(p))
            attempt('lstat', lambda: os.lstat(p))
            attempt('listdir', lambda: os.listdir(d))
            attempt('scandir', lambda: list(os.scandir(d)))
            attempt('readlink', lambda: os.readlink(p))
            attempt('open', lambda: open(p))
            attempt('io.open', lambda: io.open(p))
            attempt('os.open', lambda: os.open(p, os.O_RDONLY))
            attempt('getmtime', lambda: os.path.getmtime(p))
            r['exists'] = os.path.exists(p)
            r['isdir'] = os.path.isdir(d)
            r['access'] = os.access(p, os.R_OK)
            r['glob'] = glob.glob(d + '/*')
            result = r
        ''' % {'p': self.unknown, 'd': self.unknown_dir}
        out = self.fx.run(body)
        r = out['result']
        for name in ('stat', 'lstat', 'listdir', 'scandir', 'readlink', 'open', 'io.open', 'os.open', 'getmtime'):
            self.assertEqual(r[name], 'FileNotFoundError', name)
        self.assertEqual((r['exists'], r['isdir'], r['access'], r['glob']), (False, False, False, []))
        funcs = {m['func'] for m in out['misses'] if m['kind'] == 'lookup'}
        self.assertTrue({'stat', 'lstat', 'listdir', 'scandir', 'readlink', 'open', 'access'} <= funcs, funcs)
        self.assertTrue(all(m['path'] in (self.unknown, self.unknown_dir) for m in out['misses'] if m['kind'] == 'lookup'))
        self.assertGreater(out['total'], 0)

    def test_a_name_the_listing_lacks_is_not_a_miss(self):
        fx = self.fx
        fx.builder.listing(os.path.join(fx.live, 'known'))
        fx.builder.write_disk(())
        fx.put('known/new_after_capture.txt', 'live only')
        body = '''
            result = {'exists': os.path.exists(%(p)r), 'listing': os.listdir(%(d)r)}
        ''' % {'p': os.path.join(fx.live, 'known', 'new_after_capture.txt'), 'd': os.path.join(fx.live, 'known')}
        out = self.fx.run(body)
        self.assertEqual(out['result'], {'exists': False, 'listing': ['a.txt']})
        self.assertEqual(out['total'], 0)                                                                         # the capture says it was not there: that is an answer

    def test_a_relative_path_is_taken_from_the_working_folder(self):
        out = self.fx.run('''
            try:
                open('no/such/relative.txt')
            except FileNotFoundError:
                result = 'refused'
        ''')
        self.assertEqual(out['result'], 'refused')
        self.assertEqual(out['misses'][0]['path'], os.path.join(os.getcwd(), 'no/such/relative.txt'))

    def test_a_process_may_not_be_started(self):
        out = self.fx.run('''
            import subprocess
            r = {}
            for name, fn in (('run', lambda: subprocess.run(['echo', 'secret-argument'])), ('popen', lambda: subprocess.Popen('true', shell=True)),
                             ('check_output', lambda: subprocess.check_output(['ps'])), ('system', lambda: os.system('true --private-argument'))):
                try:
                    fn()
                    r[name] = 'started'
                except PermissionError:
                    r[name] = 'refused'
            result = r
        ''')
        self.assertEqual(out['result'], {'run': 'refused', 'popen': 'refused', 'check_output': 'refused', 'system': 'refused'})
        kinds = {(m['kind'], m['path']) for m in out['misses']}
        self.assertEqual(kinds, {('subprocess', 'echo'), ('subprocess', 'true'), ('subprocess', 'ps')})                 # the program only, never its arguments
        self.assertNotIn('secret-argument', json.dumps(out['misses']))

    def test_the_account_files_are_refused_by_name(self):
        fx = self.fx
        creds = fx.put('home/.claude/.credentials.json', '{"token": "SECRET"}')
        auth = fx.put('home/.codex/auth.json', '{"token": "SECRET"}')
        db = fx.put('home/.codex/state.sqlite-wal', 'SECRET')
        for p in (creds, auth, db):
            fx.builder.ensure(p)
            self.assertNotIn(p, fx.builder.entries)                                                                # the freezer does not even look at them
            self.assertIsNone(fx.builder.content(p))
        fx.builder.write_disk(())
        out = fx.run('''
            r = {}
            for name, p in (('creds', %r), ('auth', %r), ('db', %r)):
                try:
                    open(p)
                    r[name] = 'opened'
                except PermissionError:
                    r[name] = 'refused'
                try:
                    os.stat(p)
                except PermissionError:
                    r[name] += '+stat refused'
            result = r
        ''' % (creds, auth, db))
        self.assertEqual(set(out['result'].values()), {'refused+stat refused'})
        kinds = {m['kind'] for m in out['misses']}
        self.assertEqual(kinds, {'forbidden_open', 'forbidden_stat'})
        self.assertEqual(out['total'], 3)                                                                           # the three opens count; a look at a name is listed but is not a miss
        for dirpath, _, names in os.walk(fx.cap):
            for n in names:
                with open(os.path.join(dirpath, n), 'rb') as f:
                    self.assertNotIn(b'SECRET', f.read())


class PassThroughData(unittest.TestCase):
    """What the jail lets through to the live disk is the code (the board folders, the interpreter's libraries), nothing else. A judgment datum that happens to lie in the code folder (a session that
    works in the board's own repository) is the capture's answer, a refusal when the capture lacks it, never the live bytes."""

    def setUp(self):
        self.fx = fx = Fixture(self)
        self.code = os.path.join(fx.tmp, 'boardcode')
        os.makedirs(os.path.join(self.code, 'board'))
        with open(os.path.join(self.code, 'board', 'mod.py'), 'w') as f:
            f.write('CODE = 1\n')
        self.doc = fx.put('../boardcode/docs/review/x.md', 'captured document')
        fx.freeze(self.doc)
        fx.builder.listing(os.path.dirname(self.doc))
        fx.builder.write_disk(())
        fx.put('../boardcode/docs/review/x.md', 'LIVE document, edited after the capture')
        self.live_only = fx.put('../boardcode/docs/review/y.md', 'live only')

    def test_a_datum_in_the_code_folder_is_the_captures(self):
        body = '''
            r = {'code': open(%(code)r + '/board/mod.py').read(), 'doc': open(%(doc)r).read(), 'listing': sorted(os.listdir(os.path.dirname(%(doc)r)))}
            try:
                open(%(live)r)
            except FileNotFoundError:
                r['live_only'] = 'refused'
            try:
                os.stat(%(code)r + '/docs/review/never-captured.md')
            except FileNotFoundError:
                r['never'] = 'refused'
            result = r
        ''' % {'code': self.code, 'doc': self.doc, 'live': self.live_only}
        out = self.fx.run(body, code=self.code)
        r = out['result']
        self.assertEqual(r['code'], 'CODE = 1\n')                                                                   # the board's own files are read from the disk
        self.assertEqual(r['doc'], 'captured document')
        self.assertEqual(r['listing'], ['x.md'])                                                                    # not y.md: the live listing is not the answer
        self.assertEqual((r['live_only'], r['never']), ('refused', 'refused'))
        self.assertEqual({os.path.basename(m['path']) for m in out['misses'] if m['kind'] == 'lookup'}, set())       # the listing of the capture says they are not there: an answer, not a miss

    def test_a_path_the_capture_has_no_word_on_is_a_miss_even_below_the_code_folder(self):
        fx = self.fx
        other = os.path.join(self.code, 'docs', 'unlisted')
        os.makedirs(other)
        with open(os.path.join(other, 'z.md'), 'w') as f:
            f.write('live')
        out = fx.run('''
            try:
                open(%r)
            except FileNotFoundError:
                result = 'refused'
        ''' % os.path.join(other, 'z.md'), code=self.code)
        self.assertEqual(out['result'], 'refused')
        self.assertEqual(out['total'], 1)


class WriteBoundary(unittest.TestCase):
    """A write is allowed only in the output folders (the run's own folder and the fresh copy of the link cache in it): anywhere else, the mirror and the live HOME included, it is a PermissionError and a line in jail_miss."""

    def setUp(self):
        self.fx = fx = Fixture(self)
        self.live_file = fx.put('home/.cache/agent-bullpen/links.json', '{"v": "live"}')
        fx.freeze(self.live_file)
        self.cache_copy = os.path.join(fx.out, 'cache')
        os.makedirs(os.path.join(self.cache_copy, 'agent-bullpen'))

    def test_writes_inside_the_output_work(self):
        body = '''
            import tempfile
            p = %(cache)r + '/agent-bullpen/links.json'
            with open(p, 'w') as f:
                f.write('{"v": "new"}')
            os.makedirs(%(out)r + '/deeper/still', exist_ok=True)
            tmp = %(out)r + '/links.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, b'x')
            os.close(fd)
            os.replace(tmp, %(out)r + '/deeper/links.json')
            os.unlink(%(out)r + '/deeper/links.json')
            result = [open(p).read(), os.path.isdir(%(out)r + '/deeper/still')]
        ''' % {'cache': self.cache_copy, 'out': self.fx.out}
        out = self.fx.run(body)
        self.assertEqual(out['result'], ['{"v": "new"}', True])
        self.assertEqual(out['total'], 0)

    def test_writes_anywhere_else_are_refused(self):
        mirror = os.path.join(self.fx.cap, 'root', self.live_file.lstrip('/'))
        self.assertTrue(os.path.isfile(mirror))
        body = '''
            r = {}
            def attempt(name, fn):
                try:
                    fn()
                    r[name] = 'done'
                except PermissionError:
                    r[name] = 'refused'
            def w(path, mode='w'):
                with open(path, mode) as f:
                    f.write('X')
            attempt('live', lambda: w(%(live)r))
            attempt('append', lambda: w(%(live)r, 'a'))
            attempt('mirror', lambda: w(%(mirror)r))
            attempt('os.open', lambda: os.open(%(live)r, os.O_WRONLY))
            attempt('creat', lambda: os.open(%(new)r, os.O_WRONLY | os.O_CREAT))
            attempt('mkdir', lambda: os.makedirs(%(newdir)r))
            attempt('unlink', lambda: os.unlink(%(live)r))
            attempt('replace', lambda: os.replace(%(live)r, %(out)r + '/stolen'))
            attempt('rename_in', lambda: os.rename(%(out)r + '/nothing', %(live)r))
            attempt('chmod', lambda: os.chmod(%(live)r, 0o600))
            attempt('utime', lambda: os.utime(%(live)r, (1, 1)))
            attempt('symlink', lambda: os.symlink('x', %(live)r + '.lnk'))
            result = r
        ''' % {'live': self.live_file, 'mirror': mirror, 'new': self.live_file + '.new', 'newdir': os.path.join(self.fx.live, 'newdir'), 'out': self.fx.out}
        out = self.fx.run(body)
        self.assertEqual(set(out['result'].values()), {'refused'}, out['result'])
        self.assertEqual({m['kind'] for m in out['misses']}, {'write'})
        # nothing changed: the live file, the mirror, and no new file anywhere
        for p in (self.live_file, mirror):
            with open(p) as f:
                self.assertEqual(f.read(), '{"v": "live"}')
        self.assertFalse(os.path.exists(self.live_file + '.new') or os.path.exists(os.path.join(self.fx.live, 'newdir')) or os.path.exists(os.path.join(self.fx.out, 'stolen')))
        self.assertEqual(out['total'], len(out['result']))


class DirFd(unittest.TestCase):
    """A file opened by walking down a folder at a time with dir_fd (the board's open_safe does it) is the capture's: the names are taken from the original path of the folder, not from the working folder
    (found on a real capture: every file opened that way was a miss, and read as nothing)."""

    def setUp(self):
        self.fx = fx = Fixture(self)
        self.f = fx.put('deep/er/a.md', 'captured text\nsecond line\n', mtime=1_700_000_000_000_000_042)
        self.link = fx.link('deep/er/l.md', self.f)
        fx.builder.listing(os.path.dirname(self.f))
        fx.freeze(self.f, self.link)
        fx.put('deep/er/a.md', 'LIVE bytes')

    def test_walking_down_with_dir_fd_reads_the_capture(self):
        parts = [p for p in self.f.split('/') if p]
        body = """
            parts = %r
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            for p in parts[:-1]:
                nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nfd
            r = {'names': sorted(os.listdir(fd)), 'stat_mtime': os.stat(parts[-1], dir_fd=fd).st_mtime_ns, 'access': os.access(parts[-1], os.R_OK, dir_fd=fd)}
            ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            r['text'] = os.read(ffd, 100).decode()
            r['fstat_mtime'] = os.fstat(ffd).st_mtime_ns
            try:
                os.open('l.md', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                r['nofollow_on_a_link'] = e.errno
            try:
                os.open('nothing.md', os.O_RDONLY, dir_fd=fd)
            except FileNotFoundError:
                r['absent'] = 'refused'
            os.close(ffd)
            os.close(fd)
            result = r
        """ % (parts,)
        out = self.fx.run(body)
        r = out['result']
        self.assertEqual(r['text'], 'captured text\nsecond line\n')
        self.assertEqual((r['stat_mtime'], r['fstat_mtime']), (1_700_000_000_000_000_042,) * 2)
        self.assertEqual(r['names'], ['a.md', 'l.md'])
        self.assertTrue(r['access'])
        import errno
        self.assertEqual(r['nofollow_on_a_link'], errno.ELOOP)
        self.assertEqual(r['absent'], 'refused')
        self.assertEqual(out['total'], 0)

    def test_a_real_descriptor_is_not_taken_for_a_captured_folder(self):
        out = self.fx.run("""
            fd = os.open(%r, os.O_RDONLY | os.O_DIRECTORY)
            result = sorted(os.listdir(fd))
            os.close(fd)
        """ % self.fx.out, writable=[self.fx.out])
        self.assertEqual(out['result'], [])
        self.assertEqual(out['total'], 0)


class Stat(unittest.TestCase):
    """The numbers of a stat are the capture's, to the nanosecond, and the float times are the ones the real call gives."""

    def test_stat_result_is_made_from_the_entry(self):
        fx = Fixture(self)
        p = fx.put('f.txt', 'abc', mtime=1_700_000_123_456_789_012)
        real = os.stat(p)
        fx.freeze(p)
        out = fx.run('''
            st = os.stat(%r)
            result = [st.st_mode, st.st_ino, st.st_dev, st.st_size, st.st_uid, st.st_gid, st.st_mtime_ns, st.st_mtime, st.st_nlink]
        ''' % p)
        self.assertEqual(out['result'], [real.st_mode, real.st_ino, real.st_dev, real.st_size, real.st_uid, real.st_gid, real.st_mtime_ns, real.st_mtime, real.st_nlink])
        self.assertTrue(stat.S_ISREG(out['result'][0]))


class Modes(unittest.TestCase):
    """The trace mode (home_freeze.py uses it to learn what a program looks up): the capture answers what it holds, the live disk answers the rest and every such lookup is noted. The record folders are the
    capture's only, in trace mode too."""

    def test_trace_passes_unknown_paths_and_notes_them(self):
        fx = Fixture(self)
        known = fx.put('known.txt', 'captured')
        fx.freeze(known)
        live_only = fx.put('other/live.txt', 'live only')
        records = os.path.join(fx.tmp, 'records')
        os.makedirs(records)
        with open(os.path.join(records, 'sess.jsonl'), 'w') as f:
            f.write('{}')
        fx.put('known.txt', 'changed')
        body_run = textwrap.dedent('''
            import json, os, sys
            sys.path.insert(0, %r)
            import home_jail
            jail = home_jail.Jail(home_jail.load_entries(%r), %r, 'trace', [%r], [], [%r])
            with jail:
                r = [open(%r).read(), open(%r).read()]
                try:
                    open(%r)
                except FileNotFoundError:
                    r.append('record refused')
            print(json.dumps({'r': r, 'touched': sorted(jail.touched), 'miss': jail.miss_list()}))
        ''') % (TOOLS, fx.cap, fx.cap, fx.cap, records, known, live_only, os.path.join(records, 'sess.jsonl'))
        p = os.path.join(fx.work, 'trace.py')
        with open(p, 'w') as f:
            f.write(body_run)
        r = subprocess.run([sys.executable, '-I', p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        out = json.loads(r.stdout.decode())
        self.assertEqual(out['r'], ['captured', 'live only', 'record refused'])
        self.assertIn(live_only, out['touched'])
        self.assertNotIn(known, out['touched'])
        self.assertEqual([m['path'] for m in out['miss']], [os.path.join(records, 'sess.jsonl')])


if __name__ == '__main__':
    unittest.main()
