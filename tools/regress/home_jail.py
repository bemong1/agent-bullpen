# The jail of the frozen-HOME tools (see home_freeze.py): inside one process, every disk lookup of an absolute path is answered from the capture's mirror, as if the disk were what it was
# at the moment of the capture, and a lookup the capture does not know is refused and counted (`jail_miss`) instead of reaching the live disk.
#
# What it wraps: os.stat/lstat/listdir/scandir/readlink/access/open/fstat/close, builtins.open and io.open (os.path.exists/isdir/getmtime, glob, realpath and the pathlib of 3.11+ reach the disk through these),
# the calls that write (outside the output folders: PermissionError), and the calls that start a process (refused).
# What it never does: change a byte. The answers of stat/lstat/scandir are made from `disk.json` (kind, size, mtime_ns, ino, owner, link target), not from the mirror files (which carry the time of the copy);
# a symbolic link of the capture is not a link of the mirror but a line of `disk.json`, followed here in the ORIGINAL path space (an absolute target that is read as it is would reach the live disk).
#
# Two modes. `replay`: a path that is not in the capture is a FileNotFoundError and a line in `misses`. `trace`: used by home_freeze.py to find out what a program looks up: the records and what the capture already holds are
# answered from the mirror, anything else is passed to the live disk and noted in `touched` (the freezer then adds it to the capture and runs again).
#
# Layout of the capture (the caller builds it, home_freeze.py): disk.json {"entries": {original absolute path: entry}}; the bytes of a file at `<cap>/<entry.store>`.
# entry: {t: file|dir|link|absent|other, mode, ino, dev, nlink, uid, gid, size, atime_ns, mtime_ns, ctime_ns, target (link), names (dir: the listing, or null = not listed), store, copied (False = sparse stand-in: a read is a miss), err (absent)}
import builtins
import errno
import fnmatch
import io
import json
import os
import posixpath
import stat as _stat
import subprocess
import sys
import threading

FORBIDDEN_GLOBS = ('.credentials.json', 'auth.json', '*.sqlite*', '.claude.json')     # never opened, never copied, whatever the folder (the account's secrets)
MAX_HOPS = 40
WRITE_FUNCS = ('mkdir', 'rmdir', 'remove', 'unlink', 'rename', 'replace', 'symlink', 'link', 'utime', 'chmod', 'chown', 'truncate', 'mkfifo')


def is_forbidden(path):
    base = posixpath.basename(os.fspath(path) if not isinstance(path, bytes) else os.fsdecode(path)).lower()
    return any(fnmatch.fnmatchcase(base, g.lower()) for g in FORBIDDEN_GLOBS)


def under(path, root):
    """Whether `path` is `root` or inside it (whole components)."""
    root = root.rstrip('/') or '/'
    return path == root or root == '/' or path.startswith(root + '/')


def make_stat(e):
    """The os.stat_result of an entry: the same number the real call returned at the capture (floats made as the C code does: seconds + nanoseconds * 1e-9)."""
    at, mt, ct = (int(e.get(k) or 0) for k in ('atime_ns', 'mtime_ns', 'ctime_ns'))

    def fl(ns):
        sec, nsec = divmod(ns, 10 ** 9)
        return sec + nsec * 1e-9
    tup = (e.get('mode', 0), e.get('ino', 0), e.get('dev', 0), e.get('nlink', 1), e.get('uid', 0), e.get('gid', 0), e.get('size', 0), at // 10 ** 9, mt // 10 ** 9, ct // 10 ** 9)
    return os.stat_result(tup, {'st_atime': fl(at), 'st_mtime': fl(mt), 'st_ctime': fl(ct), 'st_atime_ns': at, 'st_mtime_ns': mt, 'st_ctime_ns': ct,
                                'st_blksize': 4096, 'st_blocks': (int(e.get('size', 0)) + 511) // 512, 'st_rdev': 0})


class DirEntryProxy:
    """What os.scandir hands out inside the jail: the name and the original path, and every question about the entry answered from the capture (a real DirEntry would ask the live disk
    without passing through the wrapped os.stat)."""
    __slots__ = ('name', 'path', '_jail', '_abs')

    def __init__(self, jail, name, path, absolute):
        self.name, self.path, self._jail, self._abs = name, path, jail, absolute

    def __fspath__(self):
        return self.path

    def __repr__(self):
        return '<DirEntryProxy %r>' % (self.name,)

    def inode(self):
        return self._jail.lstat(self._abs).st_ino

    def stat(self, *, follow_symlinks=True):
        return self._jail.stat(self._abs, follow_symlinks=follow_symlinks)

    def _kind(self, test, follow):
        try:
            return test(self.stat(follow_symlinks=follow).st_mode)
        except FileNotFoundError:
            return False

    def is_dir(self, *, follow_symlinks=True):
        return self._kind(_stat.S_ISDIR, follow_symlinks)

    def is_file(self, *, follow_symlinks=True):
        return self._kind(_stat.S_ISREG, follow_symlinks)

    def is_symlink(self):
        return self._kind(_stat.S_ISLNK, False)

    def is_junction(self):
        return False


class ScandirIter:
    def __init__(self, entries):
        self._it = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self._it = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Jail:
    """`entries` is disk.json's mapping. `cap` is the capture folder (where `store` is read from). `passthrough`: folders that are the real disk (the board's code, the interpreter, the capture itself, the output), `writable`: where a write is allowed.
    `record_roots`: folders whose content is exactly the capture (a record of a session that was not captured is not looked for on the live disk, in trace mode too)."""

    def __init__(self, entries, cap, mode='replay', passthrough=(), writable=(), record_roots=()):
        assert mode in ('replay', 'trace')
        self.entries, self.cap, self.mode = entries, cap, mode
        self.passthrough = tuple(os.path.normpath(p) for p in passthrough if p)
        self.writable = tuple(os.path.normpath(p) for p in writable if p)
        self.record_roots = tuple(os.path.normpath(p) for p in record_roots if p)
        self.misses = {}                        # (kind, path, func) -> count
        self.touched = {}                       # trace mode: original absolute path -> {'funcs': set, 'read': bool, 'listed': bool}
        self.lock = threading.RLock()
        self.real = {}
        self.fdmap = {}                         # fd -> (entry, (st_dev, st_ino) of the mirror file the fd is on)
        self.dirfds = {}                        # fd of a folder opened through the jail -> its ORIGINAL path (a name given with dir_fd=fd is taken from there)
        self.installed = False
        self.cwd = os.getcwd()

    # ---------- notes ----------
    def miss(self, kind, path, func):
        with self.lock:
            k = (kind, path, func)
            self.misses[k] = self.misses.get(k, 0) + 1

    def touch(self, path, func, read=False, listed=False):
        with self.lock:
            t = self.touched.setdefault(path, {'funcs': set(), 'read': False, 'listed': False})
            t['funcs'].add(func)
            t['read'] = t['read'] or read
            t['listed'] = t['listed'] or listed

    def miss_list(self):
        return [{'kind': k, 'path': p, 'func': f, 'count': n} for (k, p, f), n in sorted(self.misses.items())]

    def miss_total(self):
        """The number of misses that count against the replay (a refused look at a forbidden name is listed, not counted)."""
        return sum(n for (k, p, f), n in self.misses.items() if k != 'forbidden_stat')

    # ---------- path space ----------
    def absolute(self, path):
        p = os.fspath(path)
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        return p if p.startswith('/') else posixpath.join(self.cwd, p)

    def rooted(self, path, dir_fd):
        """The path a call with dir_fd means: a relative name is taken from the original path of that folder (the board opens a file by walking down a folder at a time with dir_fd).
        None when dir_fd is a folder the jail did not open (a real descriptor: the call goes to the real function as it was)."""
        if dir_fd is None or isinstance(path, int):
            return path
        p = os.fspath(path)
        if (p.startswith(b'/') if isinstance(p, bytes) else p.startswith('/')):
            return path
        base = self.dirfds.get(dir_fd)
        if base is None:
            return None
        return posixpath.join(os.fsencode(base), p) if isinstance(p, bytes) else posixpath.join(base, p)

    def is_pass(self, p):
        """Whether a path is the real disk: under a pass-through folder, and not a path the capture holds (what the capture has is its answer even below a folder that is let through)."""
        n = posixpath.normpath(p)
        return n not in self.entries and any(under(n, r) for r in self.passthrough + self.writable)

    def is_writable(self, p):
        n = posixpath.normpath(p)
        return any(under(n, r) for r in self.writable)

    def in_records(self, p):
        return any(under(p, r) for r in self.record_roots)

    def walk(self, path, follow=True):
        """(status, final path, entry): status 'ok' (the entry is that of `final`), 'err' (`entry` is the errno to raise), 'miss' (the capture does not say; `final` is the path as far as it was resolved plus the rest),
        'live' (trace mode: a place the live disk answers). Components are taken in the original path space: `..` goes up from where the links led, a link is read from its entry."""
        comps = self.absolute(path).split('/')
        stack, i, hops = [], 0, 0
        while i < len(comps):
            c = comps[i]
            i += 1
            if c in ('', '.'):
                continue
            if c == '..':
                if stack:
                    stack.pop()
                continue
            cand = '/' + '/'.join(stack + [c])
            e = self.entries.get(cand)
            rest = [x for x in comps[i:] if x not in ('', '.')]
            last = not rest
            if e is None:
                parent = self.entries.get('/' + '/'.join(stack)) if stack else {'t': 'dir', 'names': None}
                if parent is not None and parent.get('t') == 'dir' and parent.get('names') is not None and c not in parent['names']:
                    return 'err', cand, errno.ENOENT          # the listing of the folder at the capture does not have it
                if self.mode == 'trace' and not self.in_records(cand) and not self.is_pass(cand):
                    return 'live', '/' + '/'.join(stack + [c] + rest), None
                return 'miss', '/' + '/'.join(stack + [c] + rest), None
            t = e.get('t')
            if t == 'absent':
                return 'err', cand, errno.ENOTDIR if e.get('err') == 'ENOTDIR' else errno.ENOENT
            if t == 'link' and (follow or not last):
                hops += 1
                if hops > MAX_HOPS:
                    return 'err', cand, errno.ELOOP
                target = e.get('target') or ''
                if target.startswith('/'):
                    stack = []
                comps = target.split('/') + rest
                i = 0
                continue
            if t == 'dir' or t == 'link':
                stack.append(c)
                if last:
                    return 'ok', cand, e
                continue
            if rest:
                return 'err', cand, errno.ENOTDIR
            return 'ok', cand, e
        final = '/' + '/'.join(stack)
        e = self.entries.get(final) or ({'t': 'dir', 'mode': 0o40755, 'names': None, 'size': 4096, 'ino': 2, 'mtime_ns': 0} if not stack else None)
        if e is None:
            return 'miss', final, None
        return 'ok', final, e

    def resolve(self, path, func, follow=True):
        """(final path, entry) of a path in the captured world, or the exception to raise. Notes the miss; in trace mode (and only there) a place of the live disk returns (None, None) after noting it."""
        a = self.absolute(path)
        if is_forbidden(a):
            self.miss('forbidden_stat', a, func)               # a look at the name (the board resolves its deny list this way) is refused and noted, but is not a miss: nothing is opened
            raise PermissionError(errno.EACCES, 'forbidden by the jail', a)
        status, final, e = self.walk(a, follow)
        if status == 'ok':
            if is_forbidden(final):
                self.miss('forbidden_stat', final, func)
                raise PermissionError(errno.EACCES, 'forbidden by the jail', final)
            return final, e
        if status == 'err':
            raise OSError(e, os.strerror(e), a)
        if status == 'live':
            self.touch(a, func)
            return None, None
        self.miss('lookup', a, func)
        raise FileNotFoundError(errno.ENOENT, 'not in the capture', a)

    # ---------- the answers ----------
    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if isinstance(path, int):
            return self.fstat(path)
        orig = path
        path = self.rooted(path, dir_fd)
        if path is None:
            return self.real['stat'](orig, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if self.is_pass(self.absolute(path)):
            return self.real['stat'](path, follow_symlinks=follow_symlinks)
        final, e = self.resolve(path, 'stat' if follow_symlinks else 'lstat', follow_symlinks)
        if final is None:
            return self.real['stat'](path, follow_symlinks=follow_symlinks)
        return make_stat(e)

    def lstat(self, path, *, dir_fd=None):
        return self.stat(path, dir_fd=dir_fd, follow_symlinks=False)

    def listing(self, path, func='listdir'):
        """(the names of a folder of the capture, its path), or None when the live disk answers (trace mode)."""
        a = self.absolute(path)
        final, e = self.resolve(a, func)
        if final is None:
            self.touch(a, func, listed=True)
            return None
        if e.get('t') != 'dir':
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), a)
        if e.get('names') is None:
            if self.mode == 'trace' and not self.in_records(final):
                self.touch(final, func, listed=True)             # a folder the capture knows but never listed: the live disk lists it, and the freezer will keep the names
                return None
            self.miss('lookup', final, func)
            raise FileNotFoundError(errno.ENOENT, 'folder not listed in the capture', a)
        return [n for n in e['names'] if not is_forbidden(n)], final

    def listdir(self, path=None):
        if isinstance(path, int):
            if path not in self.dirfds:
                return self.real['listdir'](path)
            path = self.dirfds[path]
        p = '.' if path is None else path
        b = isinstance(p, bytes)
        a = self.absolute(p)
        if self.is_pass(a):
            return self.real['listdir'](path) if path is not None else self.real['listdir']()
        got = self.listing(a)
        if got is None:
            return self.real['listdir'](path) if path is not None else self.real['listdir']()
        names = got[0]
        return [os.fsencode(n) for n in names] if b else list(names)

    def scandir(self, path=None):
        if isinstance(path, int):
            if path not in self.dirfds:
                return self.real['scandir'](path)
            path = self.dirfds[path]
        p = '.' if path is None else path
        b = isinstance(p, bytes)
        a = self.absolute(p)
        if self.is_pass(a):
            return self.real['scandir'](path) if path is not None else self.real['scandir']()
        got = self.listing(a, 'scandir')
        base = os.fsdecode(p) if b else p
        if got is None:
            names = self.real['listdir'](a)
            final = a
        else:
            names, final = got
        out = []
        for n in names:
            child = posixpath.join(final, n)
            shown = posixpath.join(base, n)
            out.append(DirEntryProxy(self, os.fsencode(n) if b else n, os.fsencode(shown) if b else shown, child))
        return ScandirIter(out)

    def readlink(self, path, *, dir_fd=None):
        orig = path
        path = self.rooted(path, dir_fd)
        if path is None:
            return self.real['readlink'](orig, dir_fd=dir_fd)
        a = self.absolute(path)
        if self.is_pass(a):
            return self.real['readlink'](path)
        final, e = self.resolve(a, 'readlink', follow=False)
        if final is None:
            return self.real['readlink'](path)
        if e.get('t') != 'link':
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), a)
        t = e.get('target') or ''
        return os.fsencode(t) if isinstance(path, bytes) else t

    def access(self, path, mode, *, dir_fd=None, effective_ids=False, follow_symlinks=True):
        orig = path
        path = self.rooted(path, dir_fd)
        if path is None:
            return self.real['access'](orig, mode, dir_fd=dir_fd, effective_ids=effective_ids, follow_symlinks=follow_symlinks)
        a = self.absolute(path)
        if self.is_pass(a):
            return self.real['access'](path, mode, effective_ids=effective_ids, follow_symlinks=follow_symlinks)
        try:
            final, e = self.resolve(a, 'access', follow_symlinks)
        except OSError:
            return False
        if final is None:
            return self.real['access'](path, mode, effective_ids=effective_ids, follow_symlinks=follow_symlinks)
        if mode == os.F_OK:
            return True
        m, uid, gid = e.get('mode', 0), self.real['geteuid'](), self.real['getegid']()
        shift = 6 if e.get('uid') == uid else 3 if e.get('gid') == gid else 0
        bits = (m >> shift) & 7
        want = (4 if mode & os.R_OK else 0) | (2 if mode & os.W_OK else 0) | (1 if mode & os.X_OK else 0)
        return uid == 0 or (bits & want) == want

    # ---------- opening ----------
    @staticmethod
    def writes_flags(flags):
        return bool(flags & os.O_ACCMODE) or bool(flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))

    def deny_write(self, path, func):
        a = self.absolute(path)
        self.miss('write', a, func)
        raise PermissionError(errno.EACCES, 'writes outside the output folders are refused by the jail', a)

    def mirror_of(self, a, func, flags=os.O_RDONLY):
        """The real path of the bytes behind a path of the captured world (or None: the live disk answers, trace mode)."""
        final, e = self.resolve(a, func, follow=not (flags & getattr(os, 'O_NOFOLLOW', 0)))
        if final is None:
            self.touch(a, func, read=True)
            return None, None
        t = e.get('t')
        if t == 'link':
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), a)
        if t == 'absent':
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), a)
        if t == 'file' and not e.get('copied', True):
            if self.mode == 'trace' and not self.in_records(final):
                self.touch(final, func, read=True)           # known by its stat only: this read is what makes the freezer keep its bytes; the live file answers now
                return None, None
            self.miss('sparse_read', final, func)           # a read of a file whose bytes were not copied: the zeros of the stand-in are not what the program read
        store = e.get('store') or ('root' + final)
        return os.path.join(self.cap, store), e

    def os_open(self, path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, int):
            return self.real['open'](path, flags, mode, dir_fd=dir_fd)
        orig = path
        path = self.rooted(path, dir_fd)
        if path is None:
            return self.real['open'](orig, flags, mode, dir_fd=dir_fd)
        a = self.absolute(path)
        if is_forbidden(a):
            self.miss('forbidden_open', a, 'open')
            raise PermissionError(errno.EACCES, 'forbidden by the jail', a)
        if self.writes_flags(flags):
            if not self.is_writable(a):
                self.deny_write(a, 'open(write)')
            return self.real['open'](path, flags, mode)
        if self.is_pass(a):
            return self.real['open'](path, flags, mode)
        real, e = self.mirror_of(a, 'open', flags)
        if real is None:
            fd = self.real['open'](path, flags, mode)
            if flags & getattr(os, 'O_DIRECTORY', 0):
                with self.lock:
                    self.dirfds[fd] = posixpath.normpath(a)           # trace mode: a folder of the live disk; the names walked down from it are looked up (and noted) by their full path
            return fd
        fd = self.real['open'](real, flags & ~getattr(os, 'O_NOFOLLOW', 0), mode)
        self._register(fd, e)
        if e.get('t') == 'dir':
            with self.lock:
                self.dirfds[fd] = self.walk(a)[1]
        return fd

    def _register(self, fd, e):
        try:
            st = self.real['fstat'](fd)
            with self.lock:
                self.fdmap[fd] = (e, (st.st_dev, st.st_ino))
        except OSError:
            pass

    def fstat(self, fd):
        got = self.fdmap.get(fd) if isinstance(fd, int) else None
        real = self.real['fstat'](fd)
        if got and (real.st_dev, real.st_ino) == got[1]:
            return make_stat(got[0])
        if got:
            with self.lock:
                self.fdmap.pop(fd, None)                           # the number went to another file
        return real

    def os_close(self, fd):
        with self.lock:
            self.fdmap.pop(fd, None)
            self.dirfds.pop(fd, None)
        return self.real['close'](fd)

    def py_open(self, file, mode='r', *args, **kw):
        if isinstance(file, int):
            return self.real['pyopen'](file, mode, *args, **kw)
        a = self.absolute(file)
        if is_forbidden(a):
            self.miss('forbidden_open', a, 'open')
            raise PermissionError(errno.EACCES, 'forbidden by the jail', a)
        if any(c in mode for c in 'wax+'):
            if not self.is_writable(a):
                self.deny_write(a, 'open(write)')
            return self.real['pyopen'](file, mode, *args, **kw)
        if self.is_pass(a):
            return self.real['pyopen'](file, mode, *args, **kw)
        real, e = self.mirror_of(a, 'open')
        if real is None:
            return self.real['pyopen'](file, mode, *args, **kw)
        f = self.real['pyopen'](real, mode, *args, **kw)
        try:
            self._register(f.fileno(), e)
        except (OSError, ValueError, AttributeError):
            pass
        return f

    # ---------- writes and processes ----------
    def guard_write(self, name):
        real = self.real[name]

        def call(*args, **kw):
            paths = [a for a in args[:2] if isinstance(a, (str, bytes, os.PathLike)) and not isinstance(a, int)]
            if name in ('symlink', 'link'):
                paths = paths[1:]                                  # the new name is what is written; the old one is only named
            for p in paths:
                if not self.is_writable(self.absolute(p)):
                    self.deny_write(p, name)
            return real(*args, **kw)
        call.__name__ = name
        return call

    def deny_process(self, func):
        def call(*args, **kw):
            argv = args[0] if args else kw.get('args') or kw.get('command') or ''
            first = argv.split()[0] if isinstance(argv, (str, bytes)) and argv.split() else argv[0] if argv and not isinstance(argv, (str, bytes)) else ''
            self.miss('subprocess', os.path.basename(os.fsdecode(first)) if isinstance(first, (str, bytes)) else '', func)     # the name of the program only, never its arguments
            raise PermissionError(errno.EACCES, 'a process may not be started inside the jail', func)
        call.__name__ = func
        return call

    # ---------- install ----------
    def install(self):
        if self.installed:
            return
        r = self.real
        for n in ('stat', 'lstat', 'listdir', 'scandir', 'readlink', 'access', 'open', 'fstat', 'close', 'geteuid', 'getegid') + WRITE_FUNCS:
            if hasattr(os, n):
                r[n] = getattr(os, n)
        r['pyopen'] = builtins.open
        r['popen_class'] = subprocess.Popen
        r['proc'] = {n: getattr(os, n) for n in ('system', 'popen', 'execv', 'execve', 'execvp', 'execvpe', 'posix_spawn', 'posix_spawnp', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe') if hasattr(os, n)}
        os.stat, os.lstat, os.listdir, os.scandir, os.readlink, os.access = self.stat, self.lstat, self.listdir, self.scandir, self.readlink, self.access
        os.open, os.fstat, os.close = self.os_open, self.fstat, self.os_close
        builtins.open = io.open = self.py_open
        for n in WRITE_FUNCS:
            if n in r:
                setattr(os, n, self.guard_write(n))
        for n in r['proc']:
            setattr(os, n, self.deny_process('os.' + n))
        jail = self

        class _NoPopen(r['popen_class']):
            def __init__(self, *a, **k):
                jail.deny_process('subprocess.Popen')(*a, **k)
        subprocess.Popen = _NoPopen
        self.installed = True

    def uninstall(self):
        if not self.installed:
            return
        r = self.real
        for n in ('stat', 'lstat', 'listdir', 'scandir', 'readlink', 'access', 'open', 'fstat', 'close') + WRITE_FUNCS:
            if n in r:
                setattr(os, n, r[n])
        builtins.open = io.open = r['pyopen']
        for n, f in r['proc'].items():
            setattr(os, n, f)
        subprocess.Popen = r['popen_class']
        self.installed = False

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *a):
        self.uninstall()


def load_entries(cap):
    with open(os.path.join(cap, 'disk.json'), encoding='utf-8') as f:
        return json.load(f)['entries']


CODE_PARTS = ('board', 'static', 'server.py', 'statusline.py')      # what of a board code folder is code. The rest of the folder (docs, tests, a debate folder a session worked in) may be judgment data: the capture's answer


def jail_for(cap, mode, code_dir, out_dirs, extra_pass=(), record_roots=()):
    """The jail of a capture folder for one run: pass-through = the code of the board (CODE_PARTS of its folder), the interpreter's libraries, the capture folder itself (the jail reads the mirror),
    the output folders (also the only places a write is allowed)."""
    passthrough = [os.path.join(code_dir, n) for n in CODE_PARTS] + [cap, sys.prefix, sys.base_prefix, sys.exec_prefix] + list(out_dirs) + list(extra_pass)
    return Jail(load_entries(cap), cap, mode, passthrough, out_dirs, record_roots)
