"""Tests of release preparation (server side): no usage query and no credential file, path settings, port first and the start output, process check (no /proc), host names, solo and sources, fonts.
The tests read temporary folders only (the transcripts and credential files of the real HOME are never opened).

    python3 -m unittest discover -s tests
"""
import builtins
import contextlib
import http.client
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import cache_globals, isolated_env, listen_port, patched, read_output, server, start_patches, terminal_lang  # noqa: E402
from test_preserve import codex_entry  # noqa: E402
from test_stage1 import call  # noqa: E402
from test_stage2 import iso  # noqa: E402

from board import catalog, i18n, plans, procs, util  # noqa: E402
from board.codex_parse import cx_alive, cx_open  # noqa: E402

SID = ['%08d-0000-4000-8000-000000000000' % n for n in range(1, 99)]


def write(path, text='x', mode='w'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)
    return path


def touch(path, t):
    os.utime(path, (t, t))


class Home:
    """A temporary HOME: points the path globals at this folder (patched) and helps create Claude transcripts."""

    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')
        self.claude = os.path.join(self.home, '.claude')
        self.codex = os.path.join(self.home, '.codex')
        os.makedirs(os.path.join(self.claude, 'projects'))
        os.makedirs(os.path.join(self.codex, 'sessions'))
        self.links = server.LinkIndex()
        start_patches(case, HOME=self.home, CLAUDE_HOME=self.claude, PROJECTS=os.path.join(self.claude, 'projects'),
                      CLAUDE_JSON=(os.path.join(self.home, '.claude.json'),),
                      CODEX_HOME=self.codex, CODEX_SESSIONS=os.path.join(self.codex, 'sessions'),
                      CODEX_NAMES=os.path.join(self.codex, 'session_index.jsonl'),
                      DENY_FILES=(os.path.join(self.claude, '.credentials.json'), os.path.join(self.home, '.claude.json'),
                                  os.path.join(self.codex, 'auth.json')),
                      CODEX=server.CodexIndex(), LINKS=self.links, **cache_globals(self.home))
        reps = mock.patch.dict(catalog._REPS, clear=True)
        reps.start()
        case.addCleanup(reps.stop)

    def session(self, sid, proj='-w-app', cwd='/w/app', t=None, agents=0, title=''):
        """A Claude session transcript. With agents > 0, that many subagent metas are created. t = modification time."""
        d = os.path.join(self.claude, 'projects', proj)
        lines = [json.dumps({'type': 'user', 'timestamp': iso(t or time.time()), 'cwd': cwd, 'message': {'role': 'user', 'content': 'hi'}}, separators=(',', ':'))]
        if title:
            lines.append(json.dumps({'type': 'ai-title', 'aiTitle': title}, separators=(',', ':')))
        p = write(os.path.join(d, sid + '.jsonl'), '\n'.join(lines) + '\n')
        for n in range(agents):
            write(os.path.join(d, sid, 'subagents', 'agent-a%016x.meta.json' % n), '{"description":"d"}')
            a = write(os.path.join(d, sid, 'subagents', 'agent-a%016x.jsonl' % n), '{}\n')
            if t:
                touch(a, t)
        if t:
            touch(p, t)
        return p

    def pid_file(self, pid, sid, name='x'):
        write(os.path.join(self.claude, 'sessions', '%d.json' % pid), json.dumps({'pid': pid, 'sessionId': sid, 'name': name}))


class Run:
    """The running server.py process that live_server returns: out() and err() give the output so far; after it exits, out_text, err_text and code."""

    def __init__(self, p, out, err):
        self.p, self._out, self._err = p, out, err
        self.out_text = self.err_text = ''
        self.code = None

    @staticmethod
    def _read(f):
        return read_output(f)                          # not seek(0) and read(): that moves the position the server writes at (see compat.read_output)

    def out(self):
        return self._read(self._out)

    def err(self):
        return self._read(self._err)

    def port(self, timeout=60):
        """The port in the address line of the server's output, waiting for the line (it comes a moment after the words a test waits for when the machine is busy). When the server ends or
        the time is up without one, the test fails with everything the server printed."""
        end = time.time() + timeout
        while True:
            out = self.out()
            found = listen_port(out)
            if found is not None:
                return found
            code = self.p.poll()
            if code is not None or time.time() > end:
                raise AssertionError('no address line in the output of the server (%s):\n--- stdout:\n%s\n--- stderr:\n%s' % (
                    'it ended with code %s' % code if code is not None else 'none in %s s' % timeout, out, self.err()))
            time.sleep(0.05)


@contextlib.contextmanager
def live_server(args, home, extra_env=None, wait=('프로세스 판정',), timeout=20):
    """Starts server.py as a separate process, waits until the text in wait appears on stdout (or the process ends), hands over a Run, and stops it on exit."""
    env = isolated_env(home, AGENT_BULLPEN_LANG='ko')       # the terminal texts the tests wait for and read are Korean, whatever LANG the runner has
    env.update(extra_env or {})
    with tempfile.TemporaryFile('w+') as out, tempfile.TemporaryFile('w+') as err:
        run = Run(subprocess.Popen([sys.executable, server.__file__] + list(args), stdout=out, stderr=err, env=env, stdin=subprocess.DEVNULL), out, err)
        try:
            end = time.time() + timeout
            while time.time() < end and run.p.poll() is None and not all(w in run.out() for w in wait):
                time.sleep(0.05)
            yield run
        finally:
            if run.p.poll() is None:
                run.p.terminate()
            try:
                run.p.wait(10)
            except subprocess.TimeoutExpired:
                run.p.kill()
                run.p.wait()
            run.out_text, run.err_text, run.code = run.out(), run.err(), run.p.returncode


def run_server(args, home, extra_env=None, wait=('프로세스 판정',), timeout=20):
    """The result of starting live_server and stopping it right away: (stdout, stderr, exit code)."""
    with live_server(args, home, extra_env, wait, timeout) as run:
        pass
    return run.out_text, run.err_text, run.code


class ChildOutput(unittest.TestCase):
    """What a test reads of a server's output while the server runs: the file the server writes to is read without moving the position it writes at, and the address line is waited for."""
    PRINTS = 'import sys, time\nfor i in range(1500):\n    print("line %d" % i, flush=True)\n    if i % 25 == 0:\n        time.sleep(0.001)\n'

    def child(self, code, out, err):
        return subprocess.Popen([sys.executable, '-c', code], stdout=out, stderr=err, stdin=subprocess.DEVNULL)

    def test_a_reader_that_polls_hard_loses_no_line_of_the_child(self):
        """The old way (`seek(0)`, `read()`) moved the position the child writes at, and the next line it wrote landed on the text that was there."""
        with tempfile.TemporaryFile('w+') as out, tempfile.TemporaryFile('w+') as err:
            p = self.child(self.PRINTS, out, err)
            while p.poll() is None:
                read_output(out)
            p.wait()
            self.assertEqual(read_output(out).splitlines(), ['line %d' % i for i in range(1500)])

    def test_the_port_is_waited_for(self):
        with tempfile.TemporaryFile('w+') as out, tempfile.TemporaryFile('w+') as err:
            run = Run(self.child('import time\ntime.sleep(0.4)\nprint("현황판: http://localhost:8123/", flush=True)\ntime.sleep(5)', out, err), out, err)
            try:
                self.assertEqual(run.port(), 8123)
            finally:
                run.p.kill()
                run.p.wait()

    def test_a_server_that_ends_without_an_address_fails_the_test_with_its_output(self):
        with tempfile.TemporaryFile('w+') as out, tempfile.TemporaryFile('w+') as err:
            run = Run(self.child('import sys\nprint("starting")\nprint("boom", file=sys.stderr)\nsys.exit(3)', out, err), out, err)
            with self.assertRaises(AssertionError) as cm:
                run.port()
            msg = str(cm.exception)
            self.assertIn('ended with code 3', msg)
            self.assertIn('starting', msg)
            self.assertIn('boom', msg)

    def test_a_server_that_prints_no_address_in_time_fails_the_test_with_its_output(self):
        with tempfile.TemporaryFile('w+') as out, tempfile.TemporaryFile('w+') as err:
            run = Run(self.child('import time\nprint("starting", flush=True)\ntime.sleep(30)', out, err), out, err)
            try:
                with self.assertRaises(AssertionError) as cm:
                    run.port(timeout=1)
            finally:
                run.p.kill()
                run.p.wait()
            self.assertIn('none in 1 s', str(cm.exception))
            self.assertIn('starting', str(cm.exception))


class FakeServer:
    server_address = ('127.0.0.1', 8790)
    closed = False

    def serve_forever(self):
        pass

    def server_close(self):
        self.closed = True


@contextlib.contextmanager
def main_env(case, home, run_inline=True):
    """An environment that calls main() without sockets or threads: the servers are fakes, threads only record their target (it is called at once if run_inline), REG is a fake."""
    targets = []
    ready = threading.Event()
    ready.set()

    class FakeThread:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            targets.append(self.target)
            if run_inline and self.target is not server.LINKS.scan:
                self.target()

    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(server.threading, 'Thread', FakeThread))
        st.enter_context(mock.patch.object(server.LINKS, 'ready', ready))
        st.enter_context(mock.patch.object(server.LINKS, 'scan', lambda: None))
        st.enter_context(patched(REG=types.SimpleNamespace(get=lambda sid: None, loop=lambda: None)))
        st.enter_context(mock.patch.dict(server.main.__globals__, {'DEFAULT_SESSION': None, 'FIXED_DEFAULT': None}))
        st.enter_context(mock.patch.object(server, 'ALLOWED_HOSTS', set(server.ALLOWED_HOSTS)))
        st.enter_context(mock.patch.object(server, 'ALLOWED_SUFFIXES', set()))
        yield targets


def run_main(argv, servers=None, lang='ko', keep=False):
    """Calls main() and returns (stdout, stderr, SystemExit code). The terminal language is pinned with lang (AGENT_BULLPEN_LANG; None to let the environment decide).
    Restores the server.CLI_LANG and cli_t that main() changed (with keep=True they are left as they are: the caller reads the values after main() and restores them itself)."""
    out, err, code = io.StringIO(), io.StringIO(), None
    pin = mock.patch.dict(os.environ, {'AGENT_BULLPEN_LANG': lang} if lang else {}) if keep else terminal_lang(lang)
    with pin, mock.patch('sys.argv', ['server.py'] + argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(server, 'bind_servers', (lambda hosts, port: servers) if servers is not None else server.bind_servers):
        try:
            server.main()
        except SystemExit as e:
            code = e.code
    return out.getvalue(), err.getvalue(), code


# ---------- 1. no usage query: no credential file is opened and nothing goes out ----------
class NoUsageQuery(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        write(os.path.join(self.h.claude, '.credentials.json'), json.dumps({'claudeAiOauth': {'accessToken': 'FAKE', 'expiresAt': (time.time() + 3600) * 1000}}))
        write(os.path.join(self.h.home, '.claude.json'), json.dumps({
            'oauthAccount': {'userRateLimitTier': 'default_claude_max_5x', 'billingType': 'x'},
            'cachedUsageUtilization': {'fetchedAtMs': 1790000000000, 'utilization': {'five_hour': {'utilization': 12.0, 'resets_at': '2026-09-30T05:00:00Z'}}}}))
        plans._CL_CONF.update(key=None, v=None)
        self.addCleanup(plans._CL_CONF.update, key=None, v=None)

    def test_main_never_opens_credentials_or_calls_the_api(self):
        opened, real_open = [], builtins.open

        def spy(path, *a, **kw):
            opened.append(os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else path)
            return real_open(path, *a, **kw)
        with main_env(self, self.h, run_inline=True) as targets, mock.patch.object(builtins, 'open', spy), \
                mock.patch('urllib.request.urlopen', side_effect=AssertionError('urlopen 호출')):
            out, err, code = run_main(['--port', '0'], servers=[FakeServer()])
            plan = server.plan_status()['claude']
        self.assertIsNone(code)
        self.assertEqual([p for p in opened if p.endswith(('.credentials.json', 'usage.json'))], [])
        self.assertEqual(err, '')
        self.assertNotIn('사용량 조회', out)
        self.assertEqual(plan['source'], 'cache')
        self.assertEqual(plan['five_hour']['percent'], 12.0)           # the bottom bar value comes from the .claude.json cache
        self.assertEqual(set(plan) & {'usage_api', 'error', 'error_info'}, set())

    def test_the_old_flag_ends_with_a_pointer_to_the_status_line_command(self):
        for lang, words in (('ko', ('없어졌습니다', 'statusline.py --print-config', 'docs/configuration.md')),
                            ('en', ('no longer exists', 'statusline.py --print-config', 'docs/configuration.md'))):
            srv = FakeServer()
            with main_env(self, self.h, run_inline=False) as targets:
                out, err, code = run_main(['--port', '0', '--claude-usage-api'], servers=[srv], lang=lang)
            self.assertEqual(code, 2, lang)
            self.assertEqual(out, '')
            for w in words:
                self.assertIn(w, err, lang)
            self.assertEqual(targets, [])                              # nothing was started: no thread, no scan
            self.assertFalse(srv.closed)                               # and no port was opened for it either

    def test_the_old_flag_is_not_in_the_help(self):
        out, err, code = run_server(['--help'], self.h.home, wait=('zzz',))                  # it ends by itself: nothing to stop (stopping it after its first words races its exit)
        self.assertEqual(code, 0)
        self.assertNotIn('usage-api', out)
        self.assertIn('--token', out)

    def test_the_old_flag_end_to_end(self):
        out, err, code = run_server(['--port', '0', '--claude-usage-api'], self.h.home, wait=('zzz',), timeout=5)
        self.assertEqual((out, code), ('', 2))
        self.assertIn('statusline.py', err)
        self.assertFalse(os.path.exists(os.path.join(self.h.home, '.cache')))

    def test_no_claude_json_means_no_claude_entry(self):
        os.remove(os.path.join(self.h.home, '.claude.json'))
        self.assertIsNone(server.plan_status()['claude'])

    def test_claude_json_follows_resolved_candidates(self):
        # --claude-config-dir ~/.claude: if <folder>/.claude.json is missing, the one in the neighbouring folder is read
        own, beside = os.path.join(self.h.claude, '.claude.json'), os.path.join(self.h.home, '.claude.json')
        with patched(CLAUDE_JSON=(own, beside)):
            self.assertEqual(server.plan_status()['claude']['five_hour']['percent'], 12.0)
            write(own, json.dumps({'oauthAccount': {'userRateLimitTier': 'default_claude_pro'}}))
            self.assertEqual(server.plan_status()['claude']['plan'], 'Pro')     # the one inside the folder comes first


class NoCredentialOrNetworkCode(unittest.TestCase):
    """The sources of the server hold no code that opens a login file or talks to the outside: the credentials file is named only in the never-open list of
    board/util.py, and no module imports an HTTP client."""
    FILES = ['server.py', 'statusline.py'] + sorted(os.path.join('board', f) for f in os.listdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'board')) if f.endswith('.py'))

    def sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in self.FILES:
            with open(os.path.join(root, rel), encoding='utf-8') as f:
                yield rel, f.read()

    def test_the_credentials_file_is_named_only_in_the_never_open_list(self):
        named = {rel: text.count('.credentials.json') for rel, text in self.sources() if '.credentials.json' in text}
        self.assertEqual(named, {os.path.join('board', 'util.py'): 1})
        util_src = dict(self.sources())[os.path.join('board', 'util.py')]
        line = next(ln for ln in util_src.splitlines() if '.credentials.json' in ln)
        self.assertIn("'DENY_FILES'", line)

    def test_no_http_client_is_imported(self):
        banned = ('urllib.request', 'urlopen', 'http.client', 'HTTPSConnection', 'HTTPConnection', 'create_connection', 'import requests', 'import ssl', 'import smtplib')
        found = [(rel, word) for rel, text in self.sources() for word in banned if word in text]
        self.assertEqual(found, [])


# ---------- 2. path settings ----------
class ResolvePaths(unittest.TestCase):
    H = '/home/u'

    def r(self, flags=None, env=None):
        return util.resolve_paths(flags, env or {}, self.H)

    def test_defaults(self):
        r = self.r()
        self.assertEqual((r['CLAUDE_HOME'], r['PROJECTS'], r['CODEX_HOME'], r['CODEX_SESSIONS'], r['CODEX_NAMES']),
                         ('/home/u/.claude', '/home/u/.claude/projects', '/home/u/.codex', '/home/u/.codex/sessions', '/home/u/.codex/session_index.jsonl'))
        self.assertEqual(r['FROM'], {'claude': 'default', 'codex': 'default'})
        self.assertEqual(r['CLAUDE_JSON'], ('/home/u/.claude.json',))

    def test_priority_flag_over_env_over_default(self):
        env = {'CLAUDE_CONFIG_DIR': '/e/claude', 'CODEX_HOME': '/e/codex'}
        r = self.r(env=env)
        self.assertEqual((r['CLAUDE_HOME'], r['CODEX_HOME'], r['FROM']), ('/e/claude', '/e/codex', {'claude': 'env', 'codex': 'env'}))
        r = self.r({'claude': '/f/claude', 'codex': '/f/codex'}, env)
        self.assertEqual((r['CLAUDE_HOME'], r['CODEX_HOME'], r['FROM']), ('/f/claude', '/f/codex', {'claude': 'flag', 'codex': 'flag'}))
        r = self.r({'codex': '/f/codex'}, env)                          # only one is a flag: the other comes from the environment variable
        self.assertEqual((r['CLAUDE_HOME'], r['FROM']), ('/e/claude', {'claude': 'env', 'codex': 'flag'}))

    def test_empty_env_is_unset_and_paths_are_absolute(self):
        r = self.r(env={'CLAUDE_CONFIG_DIR': '', 'CODEX_HOME': ''})
        self.assertEqual(r['FROM'], {'claude': 'default', 'codex': 'default'})
        r = self.r({'claude': 'rel/dir/../claude/'})
        self.assertEqual(r['CLAUDE_HOME'], os.path.join(os.getcwd(), 'rel', 'claude'))

    def test_claude_json_rule(self):
        # default: ~/.claude.json
        self.assertEqual(self.r()['CLAUDE_JSON'], ('/home/u/.claude.json',))
        # CLAUDE_CONFIG_DIR: <folder>/.claude.json
        self.assertEqual(self.r(env={'CLAUDE_CONFIG_DIR': '/e/.claude'})['CLAUDE_JSON'], ('/e/.claude/.claude.json',))
        # flag: <folder>/.claude.json; if the folder is named .claude, the one in the neighbouring folder is the next candidate
        self.assertEqual(self.r({'claude': '/f/.claude'})['CLAUDE_JSON'], ('/f/.claude/.claude.json', '/f/.claude.json'))
        self.assertEqual(self.r({'claude': '/f/cfg'})['CLAUDE_JSON'], ('/f/cfg/.claude.json',))

    def test_deny_files_follow_the_resolution(self):
        d = self.r()['DENY_FILES']
        self.assertEqual(set(d), {'/home/u/.claude/.credentials.json', '/home/u/.claude.json', '/home/u/.claude/.claude.json', '/home/u/.codex/auth.json'})
        d = self.r({'claude': '/f/.claude', 'codex': '/f/codex'})['DENY_FILES']
        self.assertEqual(set(d), {'/f/.claude/.credentials.json', '/f/.claude/.claude.json', '/f/.claude.json', '/f/codex/auth.json'})
        d = self.r(env={'CLAUDE_CONFIG_DIR': '/e/cfg'})['DENY_FILES']          # a candidate that is not read (the neighbouring folder) is denied too
        self.assertEqual(set(d), {'/e/cfg/.credentials.json', '/e/cfg/.claude.json', '/e/.claude.json', '/home/u/.codex/auth.json'})

    def test_denied_file_and_realpath_follow_the_resolved_dirs(self):
        with tempfile.TemporaryDirectory() as t:
            t = os.path.realpath(t)
            cfg, codex, link = os.path.join(t, 'cfg'), os.path.join(t, 'real-codex'), os.path.join(t, 'codex-link')
            os.makedirs(cfg)
            os.makedirs(codex)
            os.symlink(codex, link)
            r = util.resolve_paths({'claude': cfg, 'codex': link}, {}, t)
            auth = write(os.path.join(codex, 'auth.json'))
            sess = write(os.path.join(codex, 'sessions', 'x.json'))
            with patched(CLAUDE_HOME=r['CLAUDE_HOME'], CODEX_HOME=r['CODEX_HOME'], DENY_FILES=r['DENY_FILES']):
                for name in ('.credentials.json', '.claude.json', 'settings.json', 'settings.local.json'):
                    self.assertTrue(server.denied_file(os.path.join(cfg, name)), name)
                self.assertFalse(server.denied_file(os.path.join(cfg, 'notes.md')))
                self.assertFalse(server.denied_file(os.path.join(t, 'settings.json')))
                self.assertTrue(server.denied_file(os.path.realpath(os.path.join(link, 'auth.json'))))   # a Codex folder given as a symlink is also compared by its real path
                self.assertTrue(server.denied_file(os.path.realpath(sess)))
                self.assertTrue(server.denied_file(os.path.realpath(auth)))
                self.assertFalse(server.denied_file(os.path.join(t, 'plain', 'a.md')))

    def test_scan_path_flags(self):
        f = server.scan_path_flags
        self.assertEqual(f(['--port', '1', '--claude-config-dir', '/a', '--codex-home=/b']), {'claude': '/a', 'codex': '/b'})
        self.assertEqual(f(['--claude-config-dir', '/a', '--claude-config-dir=/c']), {'claude': '/c'})        # the last value
        self.assertEqual(f(['--claude-config-dir']), {})                                                      # no value: argparse reports it
        self.assertEqual(f(['--', '--codex-home', '/b']), {})

    def run_py(self, code, env=None, argv=()):
        e = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
        e.update(PYTHONDONTWRITEBYTECODE='1', **(env or {}))
        r = subprocess.run([sys.executable, '-c', code] + list(argv), cwd=os.path.dirname(server.__file__), env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        return r.stdout.strip()

    def test_env_is_read_at_import(self):
        out = self.run_py('import server; print(server.CLAUDE_HOME, server.CODEX_SESSIONS, server.PATH_FROM)',
                          {'CLAUDE_CONFIG_DIR': '/e/claude', 'CODEX_HOME': '/e/codex'})
        self.assertEqual(out, "/e/claude /e/codex/sessions {'claude': 'env', 'codex': 'env'}")

    def test_import_does_not_read_argv(self):
        # argv is looked at first only when server.py is __main__ (PathsEndToEnd sees a real run). An import follows the environment variables only
        code = ('import runpy, sys\n'
                'sys.argv = ["server.py", "--claude-config-dir", "/f/claude", "--codex-home", "/f/codex", "--port", "0"]\n'
                'g = runpy.run_path("server.py", run_name="not_main")\n'
                'print(g["CLAUDE_HOME"], g["PATH_FROM"]["claude"])\n')
        out = self.run_py(code, {'CLAUDE_CONFIG_DIR': '/e/claude'})
        self.assertEqual(out, '/e/claude env')

    def test_main_refuses_flags_that_board_did_not_apply(self):
        # a harness that calls main() after an import: the flag was not applied to the paths, so it ends with an error
        for argv in (['--claude-config-dir', '/somewhere/else'], ['--codex-home', '/somewhere/else']):
            out, err, code = run_main(['--port', '0'] + argv)
            self.assertEqual(code, 2, argv)
            self.assertIn('어긋납니다', err)
            self.assertIn(argv[0], err)

    def test_main_accepts_flags_that_match(self):
        h = Home(self)
        with main_env(self, h), patched(PATH_FROM={'claude': 'flag', 'codex': 'default'}):
            out, err, code = run_main(['--port', '0', '--claude-config-dir', h.claude], servers=[FakeServer()])
        self.assertIsNone(code)
        self.assertIn('(--claude-config-dir)', out)


class PathsEndToEnd(unittest.TestCase):
    """Runs server.py directly in a separate process: flags, environment variables and defaults appear in the start output (the folders read, their source, the session count)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.root, 'home')
        os.makedirs(self.home)

    def make_claude(self, cfg, n_solo=2, n_agent=1):
        base = os.path.join(cfg, 'projects', '-w-app')
        for i in range(n_solo + n_agent):
            sid = SID[i]
            write(os.path.join(base, sid + '.jsonl'), json.dumps({'type': 'user', 'timestamp': iso(time.time()), 'cwd': '/w/app', 'message': {'content': 'hi'}}, separators=(',', ':')) + '\n')
            if i >= n_solo:
                write(os.path.join(base, sid, 'subagents', 'agent-a1.meta.json'), '{}')
                write(os.path.join(base, sid, 'subagents', 'agent-a1.jsonl'), '{}\n')

    def test_flag_beats_env_and_shows_the_source(self):
        flag_cfg, env_cfg = os.path.join(self.root, 'flag-claude'), os.path.join(self.root, 'env-claude')
        self.make_claude(flag_cfg, 2, 1)
        self.make_claude(env_cfg, 5, 0)
        out, err, code = run_server(['--port', '0', '--claude-config-dir', flag_cfg], self.home,
                                       {'CLAUDE_CONFIG_DIR': env_cfg, 'CODEX_HOME': os.path.join(self.root, 'env-codex')})
        self.assertEqual(code, -signal.SIGTERM)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('현황판: http://localhost:'), lines[0])
        claude = next(ln for ln in lines if 'Claude Code' in ln)
        self.assertIn(os.path.join(flag_cfg, 'projects'), claude)
        self.assertIn('세션 3 · 에이전트 있는 세션 1', claude)
        self.assertIn('(--claude-config-dir)', claude)
        codex = next(ln for ln in lines if 'Codex' in ln)
        self.assertIn('폴더 없음', codex)
        self.assertIn('(CODEX_HOME)', codex)
        self.assertTrue(any(ln.startswith('  프로세스 판정') for ln in lines))

    def test_defaults_under_home(self):
        self.make_claude(os.path.join(self.home, '.claude'), 1, 1)
        out, err, code = run_server(['--port', '0'], self.home)
        claude = next(ln for ln in out.splitlines() if 'Claude Code' in ln)
        self.assertIn('~/.claude/projects', claude)
        self.assertIn('세션 2 · 에이전트 있는 세션 1', claude)
        self.assertIn('(기본)', claude)


# ---------- 3. start-up order and start output ----------
class PortFirst(unittest.TestCase):
    def setUp(self):
        self.busy = socket.socket()
        self.busy.bind(('127.0.0.1', 0))
        self.busy.listen(1)
        self.addCleanup(self.busy.close)
        self.port = self.busy.getsockname()[1]

    def tripwires(self):
        boom = mock.Mock(side_effect=AssertionError('포트를 못 연 뒤에는 아무것도 시작하지 않는다'))
        return [mock.patch.object(server, 'scan_sessions', boom), mock.patch.object(server.LINKS, 'scan', boom),
                mock.patch.object(server.threading, 'Thread', boom),
                patched(REG=types.SimpleNamespace(get=boom, loop=boom))]

    def test_conflict_is_one_line_and_exit_1(self):
        with contextlib.ExitStack() as st:
            for t in self.tripwires():
                st.enter_context(t)
            out, err, code = run_main(['--port', str(self.port), '--host', '127.0.0.1'])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(err.count('\n'), 1, err)                                  # one line
        self.assertIn('127.0.0.1:%d' % self.port, err)                            # address and port
        self.assertIn('--port %d' % (self.port + 1), err)                         # the alternative
        self.assertIn('http://localhost:%d/' % self.port, err)

    def test_conflict_end_to_end(self):
        with tempfile.TemporaryDirectory() as home:
            out, err, code = run_server(['--port', str(self.port)], home)
            self.assertEqual(code, 1)
            self.assertEqual(out, '')
            self.assertEqual(err.count('\n'), 1)
            self.assertIn('--port', err)
            self.assertFalse(os.path.exists(os.path.join(home, '.cache')))            # nothing was started: no scan, no link record

    def test_second_host_failure_closes_the_first(self):
        opened = []
        real = server.BoardServer

        class Spy(real):
            def __init__(self, addr, handler):
                real.__init__(self, addr, handler)
                opened.append(self)
        with mock.patch.object(server, 'BoardServer', Spy), terminal_lang('ko'):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as err:
                server.bind_servers(['127.0.0.1', '100.127.255.254'], 0)                # a VPN address that this machine does not have
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('이 기기에 없는 주소', err.getvalue())
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].socket.fileno(), -1)                                # the one opened first was closed


class NoReverseDns(unittest.TestCase):
    """http.server's HTTPServer.server_bind calls socket.getfqdn(host) (a PTR lookup through the system resolver unless the host is loopback).
    This server does not use server_name, so it does not look it up."""

    def boom(self):
        return mock.patch.multiple(socket, getfqdn=mock.Mock(side_effect=AssertionError('getfqdn 호출')),
                                   gethostbyaddr=mock.Mock(side_effect=AssertionError('gethostbyaddr 호출')))

    def test_board_server_does_not_resolve(self):
        with self.boom():
            for cls, host in ((server.BoardServer, '127.0.0.1'), (server.HTTPServer6, '::1')):
                try:
                    srv = cls((host, 0), server.Handler)
                except OSError:
                    continue                                                    # a machine without IPv6
                self.addCleanup(srv.server_close)
                self.assertEqual((srv.server_name, srv.server_port), (host, srv.server_address[1]))

    def test_the_standard_server_would(self):
        # confirms that this test is meaningful: the standard class calls getfqdn (if a Python version change makes it stop calling, the test is skipped)
        with self.boom():
            try:
                srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            except AssertionError:
                return
            srv.server_close()
        self.skipTest('이 Python의 표준 서버는 getfqdn을 부르지 않는다')

    def test_all_servers_use_it(self):
        self.assertTrue(issubclass(server.HTTPServer6, server.BoardServer))
        with mock.patch.object(server, 'Handler', server.Handler), self.boom():
            srvs = server.bind_servers(['127.0.0.1'], 0)
        self.addCleanup(lambda: [x.server_close() for x in srvs])
        self.assertIsInstance(srvs[0], server.BoardServer)


class StartupOutput(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)

    def run_with(self, argv):
        with main_env(self, self.h) as targets:
            out, err, code = run_main(argv, servers=[FakeServer()])
            return out, err, code, (list(server.ALLOWED_HOSTS), sorted(server.ALLOWED_SUFFIXES))

    def test_loopback_has_no_warning(self):
        out, err, code, _ = self.run_with(['--port', '0'])
        self.assertEqual(err, '')
        self.assertTrue(out.startswith('현황판: http://localhost:8790/\n'))
        self.assertIn('프로세스 판정 %s' % {'proc': '/proc', 'ps': 'ps', 'none': '없음'}[procs.method()], out)

    def test_loopback_allow_host_has_no_warning(self):
        _, err, _, _ = self.run_with(['--port', '0', '--allow-host', 'localhost'])
        self.assertEqual(err, '')

    def test_non_loopback_allow_host_warns(self):
        _, err, _, (hosts, suffixes) = self.run_with(['--port', '0', '--allow-host', 'box.example.net', '--allow-host', '.tail1234.ts.net'])
        self.assertEqual(err.count('\n'), 4)                                         # a framed notice: bar, title, text, bar
        self.assertEqual(err.splitlines()[0], '!' * 72)
        self.assertIn('경고 — 인증 없음', err)
        self.assertIn('허용한 이름 box.example.net, .tail1234.ts.net', err)
        self.assertNotIn('열린 주소', err)
        self.assertIn('box.example.net', hosts)
        self.assertEqual(suffixes, ['.tail1234.ts.net'])

    def test_no_auth_on_a_non_loopback_host_warns_loudly(self):
        with main_env(self, self.h):
            out, err, code = run_main(['--port', '0', '--host', '127.0.0.1', '--host', '100.100.0.7', '--no-auth'], servers=[FakeServer(), FakeServer()])
        self.assertEqual(err.count('\n'), 4)
        self.assertIn('경고 — 인증 없음', err)
        self.assertIn('열린 주소 100.100.0.7', err)
        self.assertNotIn('허용한 이름', err)
        self.assertEqual(out.count('현황판: http://'), 2)
        self.assertNotIn('token', out)

    def test_counts_and_sources(self):
        self.h.session(SID[0], t=time.time() - 50, agents=1)
        self.h.session(SID[1], t=time.time() - 30)
        self.h.session(SID[2], proj='-w-solo', cwd='/w/solo', t=time.time() - 10)
        out, err, code, _ = self.run_with(['--port', '0'])
        claude = next(ln for ln in out.splitlines() if 'Claude Code' in ln)
        self.assertIn('세션 3 · 에이전트 있는 세션 1', claude)
        self.assertIn('~/.claude/projects', claude)
        codex = next(ln for ln in out.splitlines() if 'Codex' in ln)
        self.assertIn('단독 대화, 최근 7일: 0', codex)


# ---------- 4. process check ----------
PS_OUT = (b'    1 /sbin/launchd\n'
          b'  101 /usr/local/bin/claude --resume x\n'
          b'  102 node /opt/bin/codex exec do-the-thing 019a0001-0000-7000-8000-000000000000\n'
          b'  103 /Applications/Foo.app/Contents/MacOS/Foo -x\n'
          b'bad line\n')


@contextlib.contextmanager
def masked_proc(ps_out=PS_OUT):
    """A place without /proc (imitating macOS): PROC points at a missing folder, and the ps output is the given one (None means ps cannot be used)."""
    procs.reset()
    with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(procs, '_run_ps', lambda: ps_out), \
            mock.patch.object(procs, 'uid', lambda pid: os.geteuid()):                     # the owner `ps` would name: the user of the test (the real ps table is another machine's)
        try:
            yield
        finally:
            procs.reset()


class ProcsMasked(unittest.TestCase):
    def test_method(self):
        with masked_proc():
            self.assertIn(procs.method(), ('ps', 'none'))
            with mock.patch('shutil.which', lambda n: None):
                self.assertEqual(procs.method(), 'none')
            with mock.patch('shutil.which', lambda n: '/bin/ps'):
                self.assertEqual(procs.method(), 'ps')

    def test_cmdline_has_three_values(self):
        with masked_proc():
            self.assertIs(procs.cmdline_has(101, b'claude'), True)
            self.assertIs(procs.cmdline_has(103, b'claude'), False)        # a different command
            self.assertIs(procs.cmdline_has(999, b'claude'), False)        # no such process
            for bad in (None, 'x', 0, -1, True, 1.5):
                self.assertIs(procs.cmdline_has(bad, b'claude'), False)
        with masked_proc(None):
            self.assertIsNone(procs.cmdline_has(101, b'claude'))           # unknown if ps cannot be used either (not "ended")

    def test_never_raises_when_ps_is_missing(self):
        procs.reset()
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(procs, 'PS_ARGV', ('/nonexistent/ps',)):
            self.assertIsNone(procs.cmdline_has(101, b'claude'))
            pr = procs.codex_procs('/x/sessions')
            self.assertEqual((pr['known'], pr['fd_known'], pr['any'], pr['fds']), (False, False, False, set()))
        procs.reset()

    def test_ps_timeout_and_failure_are_unknown(self):
        procs.reset()
        for exc in (subprocess.TimeoutExpired('ps', 5), OSError('x')):
            with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch('subprocess.run', side_effect=exc):
                self.assertIsNone(procs.cmdline_has(101, b'claude'))
            procs.reset()
        bad = types.SimpleNamespace(stdout=b'', returncode=1)
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch('subprocess.run', return_value=bad):
            self.assertIsNone(procs.cmdline_has(101, b'claude'))
        procs.reset()

    def test_codex_procs_from_ps(self):
        with masked_proc():
            pr = procs.codex_procs('/h/.codex/sessions')
        self.assertEqual((pr['known'], pr['fd_known'], pr['any'], pr['fds']), (True, False, True, set()))
        self.assertEqual(len(pr['cmd']), 1)
        self.assertIn(b'019a0001', pr['cmd'][0])

    def test_ps_is_cached(self):
        calls = []
        procs.reset()
        with mock.patch.object(procs, 'PROC', '/nonexistent-proc'), mock.patch.object(procs, '_run_ps', lambda: calls.append(1) or PS_OUT):
            for _ in range(5):
                procs.cmdline_has(101, b'claude')
                procs.codex_procs('/s')
        self.assertEqual(len(calls), 1)
        procs.reset()

    def test_cx_alive_and_cx_open(self):
        e = dict(codex_entry(1), id='019a0001-0000-7000-8000-000000000000', origin='tui', open=True)
        other = dict(codex_entry(2), origin='tui', open=True)
        done = dict(codex_entry(3), origin='exec', open=False)
        with masked_proc():
            self.assertIs(cx_alive(e), True)                        # the id is on the command line
            self.assertIsNone(cx_alive(other))                     # codex is running but its open files are unknown → unknown
            self.assertIs(cx_alive(done), False)                    # a finished exec is ended
        with masked_proc(PS_OUT.replace(b'codex', b'xxxxx')):
            self.assertIs(cx_alive(other), False)                   # ps can also tell that there is no codex process
        with masked_proc(None):
            self.assertIsNone(cx_alive(other))                      # unknown if the list cannot be obtained
            self.assertIs(cx_alive(done), False)

    def test_codex_verdict_does_not_end_on_unknown(self):
        """The judgment the page uses (CodexLinker.verdict): a rollout whose process cannot be seen is `unknown` once it has been quiet, never `ended`."""
        s = server.Session.__new__(server.Session)
        s.agents = {}
        linker = server.CodexLinker(s)
        a = server.CodexAgent(codex_entry(5), {})
        a.spawn_ts, a.last_ts = 1000.0, 1000.0
        now = 1000.0 + 120                                                  # quiet for more than 60 seconds
        with masked_proc():                                                 # another codex is running and the open files are unknown
            self.assertEqual(linker.verdict(a, now).status, 'unknown')
            self.assertEqual(linker.verdict(a, 1000.0 + 30).status, 'running')     # still growing: no reason to doubt it
        with masked_proc(None):
            self.assertEqual(linker.verdict(a, now).status, 'unknown')
            self.assertEqual(linker.verdict(a, 1000.0 + 30).status, 'running')
        with masked_proc(PS_OUT.replace(b'codex', b'xxxxx')):               # it is certain that there is no codex at all
            v = linker.verdict(a, now)
            self.assertEqual((v.status, v.reason), ('ended', 'crash'))
        pr = {'ts': 10 ** 12, 'cmd': [], 'fds': set(), 'any': True}         # Linux (knows the fds too): ended if not open
        with patched(cx_procs=lambda: pr):
            self.assertEqual(linker.verdict(a, now).status, 'ended')


class ClaudeAliveMasked(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        self.h.pid_file(101, SID[0])                    # claude is running
        self.h.pid_file(103, SID[1])                    # that pid is a different command
        self.h.pid_file(999, SID[2])                    # no such pid
        write(os.path.join(self.h.claude, 'sessions', 'broken.json'), 'not json')
        write(os.path.join(self.h.claude, 'sessions', 'list.json'), '[]')

    def alive(self, sid):
        s = server.Session.__new__(server.Session)
        s.id = sid
        return s.alive()

    def test_ps(self):
        with masked_proc():
            self.assertEqual(server.claude_alive_ids(), {SID[0]})
            self.assertEqual(self.alive(SID[0])[:2], (True, 101))
            self.assertEqual(self.alive(SID[1]), (False, None, None))
            self.assertEqual(self.alive(SID[2]), (False, None, None))
            self.assertEqual(self.alive(SID[9]), (False, None, None))

    def test_no_way_to_know_is_unknown_not_ended(self):
        with masked_proc(None):
            self.assertEqual(self.alive(SID[0]), (None, 101, 'x'))
            self.assertEqual(server.claude_alive_ids(), {SID[0], SID[1], SID[2]})   # does not assume they ended
            self.assertEqual(self.alive(SID[9]), (False, None, None))                 # a session with no record stays as it is

    def test_agent_status_follows_the_three_values(self):
        s = server.Session.__new__(server.Session)
        s.codex = None
        s._cli_alive = set()
        a = server.Agent('a0123456789abcdef', {})
        a.spawn_ts = a.last_ts = 1000.0
        a.stopped_ts, a.notifications, a.handbacks, a.last_stop, a.origin, a.provider = None, [], [], None, 'task', 'claude'
        self.assertEqual(s.agent_status(a, False, 1010.0), 'ended')
        self.assertEqual(s.agent_status(a, True, 1010.0), 'running')
        self.assertEqual(s.agent_status(a, None, 1010.0), 'running')                  # unknown: judged by the growth of the transcript
        self.assertEqual(s.agent_status(a, None, 1000.0 + server.STALL_SEC + 1), 'stalled')

    def test_catalog_keeps_representative_when_unknown(self):
        # two agent sessions of the same project. The representative is the older SID[0] (pid 101): it changes to the recent one only when it is certain that there is no process
        self.h.session(SID[0], t=time.time() - 50, agents=1)
        self.h.session(SID[1], t=time.time() - 5, agents=1)
        for ps_out, rep in ((None, SID[0]),                                      # ps cannot be used either: unknown → keep the representative
                            (PS_OUT, SID[0]),                                    # pid 101 is alive as claude → keep
                            (b'    1 /sbin/launchd\n', SID[1])):                  # it is certain that pid 101 is gone → the recent session
            with masked_proc(ps_out):
                catalog._REPS['app'] = SID[0]
                ss, _ = catalog.scan_sessions()
                self.assertEqual(catalog.session_projects(ss)[0]['rep'], rep, ps_out)


@unittest.skipUnless(os.path.isfile('/proc/self/cmdline') and os.path.isfile('/bin/sleep'), 'Linux /proc 전용')
class ProcsLinux(unittest.TestCase):
    """The real /proc: finds the rollout opened by a process named codex (including a transcript path that goes through a symlink)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)

    def spawn(self, rollout):
        p = subprocess.Popen(['bash', '-c', 'exec 3< "$1"; exec -a codex-fake sleep 30', 'x', rollout], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        for _ in range(100):                                    # until the exec finishes and the name changes
            try:
                with open('/proc/%d/cmdline' % p.pid, 'rb') as f:
                    if f.read().startswith(b'codex-fake'):
                        return p
            except OSError:
                pass
            time.sleep(0.02)
        self.fail('프로세스가 준비되지 않았다')

    def test_open_rollout_and_symlinked_sessions_dir(self):
        real = os.path.join(self.root, 'real')
        rollout = write(os.path.join(real, 'sessions', '2026', '09', '30', 'rollout-a.jsonl'))
        link = os.path.join(self.root, 'link')
        os.symlink(real, link)
        self.spawn(rollout)
        procs.reset()
        for sessions in (os.path.join(real, 'sessions'), os.path.join(link, 'sessions')):
            pr = procs.codex_procs(sessions)
            self.assertEqual((pr['known'], pr['fd_known'], pr['any']), (True, True, True))
            self.assertIn(sessions + rollout[len(os.path.join(real, 'sessions')):], pr['fds'], sessions)   # matched by the transcript path name
            path = sessions + rollout[len(os.path.join(real, 'sessions')):]
            self.assertIs(cx_open(path, 'no-such-id', pr), True)
            self.assertIs(cx_open(os.path.join(sessions, 'other.jsonl'), 'no-such-id', pr), False)    # all fds are known, so "not open" is certain
            procs.reset()

    def test_real_cmdline_has(self):
        p = self.spawn(write(os.path.join(self.root, 'sessions', 'r.jsonl')))
        self.assertIs(procs.cmdline_has(p.pid, b'codex-fake'), True)
        self.assertIs(procs.cmdline_has(p.pid, b'claude'), False)
        p.kill()
        p.wait()
        self.assertIs(procs.cmdline_has(p.pid, b'codex-fake'), False)       # the process has ended


# ---------- 5. host names ----------
class AllowHost(unittest.TestCase):
    def test_arg(self):
        f = server._allow_host_arg
        self.assertEqual(f('My.Box'), 'my.box')
        self.assertEqual(f('.Example.NET'), '.example.net')
        self.assertEqual(f('*.example.net'), '.example.net')
        self.assertEqual(f('my.box:8790'), 'my.box')
        self.assertEqual(f('[fd7a:115c:a1e0::1]'), 'fd7a:115c:a1e0::1')
        self.assertEqual(f('.ts.net.'), '.ts.net')
        import argparse
        for bad in ('', '.', '..', 'a..b', '.a..b', 'a b', 'a/b', '*', 'x@y', '-x'):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                f(bad)

    def test_suffix(self):
        with mock.patch.object(server, 'ALLOWED_SUFFIXES', {'.tail1234.ts.net'}):
            for h in ('box.tail1234.ts.net', 'BOX.tail1234.ts.net:8790', 'a.b.tail1234.ts.net', 'box.tail1234.ts.net.'):
                self.assertTrue(server.host_allowed(h), h)
            for h in ('tail1234.ts.net', 'evil-tail1234.ts.net', 'box.tail1234.ts.net.evil.test', 'box.tail9999.ts.net', 'ts.net'):
                self.assertFalse(server.host_allowed(h), h)

    def test_ipv6_is_compared_as_an_address(self):
        # the same address written differently: it must pass even if the text given at open and the Host header (the URL parser turns it into the standard form) differ
        h = Home(self)
        with main_env(self, h):
            run_main(['--port', '0', '--host', 'fd7a:115c:a1e0:0:0:0:0:1'], servers=[FakeServer()])
            for host in ('[fd7a:115c:a1e0::1]:8790', '[fd7a:115c:a1e0:0:0:0:0:1]:8790', '[FD7A:115C:A1E0::1]', '[fd7a:115c:a1e0:0000::0001]:9'):
                self.assertTrue(server.host_allowed(host), host)
            for host in ('[fd7a:115c:a1e0::2]:8790', '[fd7a:115c:a1e0::1]x', 'fd7a:115c:a1e0::1', '[fd7a:115c:a1e0::10]'):
                self.assertFalse(server.host_allowed(host), host)
        with main_env(self, h):
            run_main(['--port', '0', '--host', 'fd7a:115c:a1e0::1'], servers=[FakeServer()])           # open in the compressed form and connect in the long form
            self.assertTrue(server.host_allowed('[fd7a:115c:a1e0:0:0:0:0:1]:8790'))

    def test_ipv6_names_are_normalized(self):
        self.assertEqual(server.host_name('[fd7a:115c:a1e0:0:0:0:0:1]:8790'), 'fd7a:115c:a1e0::1')
        self.assertEqual(server.host_name('[::1]'), '::1')
        self.assertEqual(server.host_name('[0:0:0:0:0:0:0:1]'), '::1')            # the long notation of loopback gives the same name
        self.assertEqual(server.host_name('127.0.0.1:80'), '127.0.0.1')
        self.assertEqual(server.host_name('Box.Example.NET.'), 'box.example.net')   # the name stays as it is (only the lowercasing and the trailing dot)
        self.assertEqual(server._allow_host_arg('[FD7A:115C:A1E0:0:0:0:0:1]'), 'fd7a:115c:a1e0::1')
        self.assertEqual(server._allow_host_arg('fd7a:115c:a1e0:0:0:0:0:1'), 'fd7a:115c:a1e0::1')

    def test_allow_host_ip_written_differently(self):
        h = Home(self)
        with main_env(self, h):
            run_main(['--port', '0', '--allow-host', '[fd7a:115c:a1e0:0:0:0:0:5]'], servers=[FakeServer()])
            self.assertTrue(server.host_allowed('[fd7a:115c:a1e0::5]:8790'))

    def test_no_builtin_suffix(self):
        self.assertEqual(server.ALLOWED_SUFFIXES, set())
        self.assertFalse(hasattr(server, 'NETBIRD_SUFFIX'))
        self.assertFalse(server.host_allowed('box.netbird.cloud'))

    def test_main_collects_names_and_suffixes(self):
        h = Home(self)
        with main_env(self, h):
            run_main(['--port', '0', '--allow-host', 'My.Box', '--allow-host', '.Example.net', '--host', '100.100.0.7'], servers=[FakeServer()])
            self.assertTrue(server.host_allowed('my.box:1'))
            self.assertTrue(server.host_allowed('100.100.0.7:8790'))
            self.assertTrue(server.host_allowed('x.example.net'))
            self.assertFalse(server.host_allowed('example.net'))

    def test_403_tells_how_to_fix(self):
        body = server.host_hint('box.tail1234.ts.net:8790')
        en, ko = body.splitlines()                                                  # English first
        self.assertIn('--allow-host box.tail1234.ts.net', ko)
        self.assertIn('--allow-host .tail1234.ts.net', ko)
        self.assertIn('--allow-host box.tail1234.ts.net', en)
        self.assertTrue(any('가' <= c <= '힣' for c in ko))
        self.assertFalse(any('가' <= c <= '힣' for c in en))
        # a weird Host is not echoed back as it is
        for weird in ('<script>alert(1)</script>', 'a b', None, '', 'x@y'):
            b = server.host_hint(weird)
            self.assertNotIn('<script>', b)
            self.assertEqual(len(b.splitlines()), 2)

    def test_403_over_http(self):
        srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
        c.request('GET', '/api/plans', headers={'Host': 'box.example.net:8790'})
        r = c.getresponse()
        body = r.read().decode()
        self.assertEqual(r.status, 403)
        self.assertTrue(r.getheader('Content-Type').startswith('text/plain'))
        self.assertIn('--allow-host box.example.net', body)
        self.assertEqual(len(body.splitlines()), 2)


class HelpAndWarnings(unittest.TestCase):
    def test_session_help_mentions_the_plain_chat_fallback(self):
        out = io.StringIO()
        with terminal_lang('ko'), mock.patch('sys.argv', ['server.py', '--help']), contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            server.main()
        text = ' '.join(out.getvalue().split())
        self.assertIn('--session SESSION', text)
        self.assertIn('없으면 가장 최근 일반 대화', text)                   # default session: solo if there is no orchestration
        desc = i18n.translator('ko')('cli.help.description')              # the description text (the dictionary's cli.help.description) says the same thing
        self.assertIn('일반 대화', desc)
        self.assertIn(' '.join(desc.split()), text)

    def test_no_syntax_or_deprecation_warnings(self):
        # make sure that what new Python (3.12+) reports as SyntaxWarning or DeprecationWarning (invalid escapes, deprecated time functions, etc.) does not end up in the CI log
        import glob
        import warnings
        root = os.path.dirname(os.path.abspath(server.__file__))
        files = [os.path.join(root, 'server.py')] + sorted(glob.glob(os.path.join(root, 'board', '*.py')) + glob.glob(os.path.join(root, 'tests', '*.py')))
        for f in files:
            with open(f, encoding='utf-8') as fh:
                src = fh.read()
            with warnings.catch_warnings():
                warnings.simplefilter('error')
                compile(src, f, 'exec')
            for word in ('utc' + 'fromtimestamp', 'datetime.utc' + 'now'):       # time functions deprecated in 3.12 (joined with + so that this file itself is not caught)
                self.assertNotIn(word, src, f)


class ShortPath(unittest.TestCase):
    def test_prefix_needs_a_separator(self):
        with patched(HOME='/home/u'):
            self.assertEqual(server.short_path('/home/u'), '~')
            self.assertEqual(server.short_path('/home/u/.claude'), '~/.claude')
            self.assertEqual(server.short_path('/home/u2/.claude'), '/home/u2/.claude')


# ---------- 6. solo and sources ----------
class SoloAndSources(unittest.TestCase):
    def setUp(self):
        self.h = Home(self)
        self.now = time.time()

    def by_id(self, ss):
        return {s['id']: s for s in ss}

    def test_solo_is_listed_after_the_rest_and_flagged(self):
        self.h.session(SID[0], t=self.now - 100, agents=2)
        self.h.session(SID[1], t=self.now - 10)                          # a more recent solo
        ss, counts = catalog.scan_sessions()
        self.assertEqual([s['id'] for s in ss], [SID[0], SID[1]])         # sessions with agents first, solo last
        d = self.by_id(ss)
        self.assertNotIn('solo', d[SID[0]])                                # the shape of existing entries is unchanged
        self.assertIs(d[SID[1]]['solo'], True)
        self.assertEqual((d[SID[1]]['agents'], d[SID[1]]['provider'], d[SID[1]]['proj'], d[SID[1]]['active']), (0, 'claude', 'app', 1))
        self.assertEqual(counts, {'claude': 2, 'agent_sessions': 1, 'codex': 0})

    def test_solo_cap_is_separate(self):
        for i in range(catalog.SOLO_LIST_MAX + 5):
            self.h.session(SID[i], proj='-w-solo%02d' % i, cwd='/w/solo%02d' % i, t=self.now - 1000 - i)
        for i in range(3):
            self.h.session(SID[60 + i], t=self.now - 100 - i, agents=1)
        ss, counts = catalog.scan_sessions()
        solos = [s for s in ss if s.get('solo')]
        self.assertEqual(len(solos), catalog.SOLO_LIST_MAX)
        self.assertEqual(solos[0]['id'], SID[0])                          # most recent first
        self.assertEqual(len([s for s in ss if not s.get('solo')]), 3)    # solo does not take the place of agent sessions
        self.assertEqual(counts['claude'], catalog.SOLO_LIST_MAX + 5 + 3) # the count is the total, regardless of the list limit
        self.assertEqual(counts['agent_sessions'], 3)

    def test_agent_cap_is_unchanged(self):
        for i in range(catalog.CL_LIST_MAX + 4):
            self.h.session(SID[i], t=self.now - 100 - i, agents=1)
        ss, counts = catalog.scan_sessions()
        self.assertEqual(len(ss), catalog.CL_LIST_MAX)
        self.assertEqual((counts['claude'], counts['agent_sessions']), (catalog.CL_LIST_MAX + 4,) * 2)

    def test_cli_child_is_not_solo(self):
        self.h.session(SID[0], t=self.now - 10)
        with mock.patch.object(self.h.links, 'cli_owner', lambda sid: {'sid': 'p'} if sid == SID[0] else None):
            ss, counts = catalog.scan_sessions()
        self.assertEqual((ss, counts['claude']), ([], 0))

    def test_codex_counts_and_sources(self):
        write(os.path.join(self.h.codex, 'sessions', '2026', '09', '30', 'rollout-2026-09-30T00-00-00-019a0001-0000-7000-8000-000000000000.jsonl'),
              json.dumps({'timestamp': iso(self.now), 'type': 'session_meta', 'payload': {'id': '019a0001-0000-7000-8000-000000000000', 'cwd': '/w/cx',
                                                                                         'timestamp': iso(self.now), 'originator': 'codex_cli_rs', 'source': 'cli'}}) + '\n')
        self.h.session(SID[0], t=self.now - 10)
        ss, counts = catalog.scan_sessions()
        self.assertEqual(counts['codex'], 1)
        src = catalog.sources(counts)
        self.assertEqual(src, [
            {'provider': 'claude', 'dir': '~/.claude', 'from': 'default', 'exists': True, 'sessions': 1, 'agent_sessions': 0},
            {'provider': 'codex', 'dir': '~/.codex', 'from': 'default', 'exists': True, 'sessions': 1, 'agent_sessions': None}])

    def test_sources_when_missing_and_from(self):
        import shutil
        shutil.rmtree(os.path.join(self.h.claude, 'projects'))
        shutil.rmtree(os.path.join(self.h.codex, 'sessions'))
        with patched(PATH_FROM={'claude': 'flag', 'codex': 'env'}):
            ss, counts = catalog.scan_sessions()
            src = catalog.sources(counts)
        self.assertEqual(ss, [])
        self.assertEqual([(s['provider'], s['from'], s['exists'], s['sessions']) for s in src], [('claude', 'flag', False, 0), ('codex', 'env', False, 0)])
        self.assertEqual(src[0]['dir'], '~/.claude')

    # --- representative and default session ---
    def test_rep_prefers_agent_session_even_if_solo_is_newer(self):
        self.h.session(SID[0], t=self.now - 500, agents=1)
        self.h.session(SID[1], t=self.now - 5)
        ss, _ = catalog.scan_sessions()
        with patched(claude_alive_ids=lambda: {SID[0], SID[1]}):
            projs = catalog.session_projects(ss)
        self.assertEqual([(p['name'], p['rep'], p['n']) for p in projs], [('app', SID[0], 1)])      # n and last count orchestration only
        self.assertAlmostEqual(projs[0]['last'], self.now - 500, delta=2)
        d = self.by_id(ss)
        self.assertEqual((d[SID[0]]['rep'], d[SID[1]]['rep']), (True, False))

    def test_rep_is_latest_solo_when_there_is_no_agent_session(self):
        self.h.session(SID[0], t=self.now - 500)
        self.h.session(SID[1], t=self.now - 50)
        self.h.session(SID[2], t=self.now - 300)
        ss, _ = catalog.scan_sessions()
        with patched(claude_alive_ids=lambda: {SID[0], SID[1], SID[2]}):
            projs = catalog.session_projects(ss)
        self.assertEqual([(p['rep'], p['n']) for p in projs], [(SID[1], 3)])

    def test_solo_rep_is_replaced_when_an_agent_session_appears(self):
        self.h.session(SID[0], t=self.now - 50)
        ss, _ = catalog.scan_sessions()
        with patched(claude_alive_ids=lambda: {SID[0], SID[1]}):
            self.assertEqual(catalog.session_projects(ss)[0]['rep'], SID[0])
            self.h.session(SID[1], t=self.now - 100, agents=1)
            ss, _ = catalog.scan_sessions()
            self.assertEqual(catalog.session_projects(ss)[0]['rep'], SID[1])

    def test_project_order_and_default_ignore_solo_only_projects(self):
        self.h.session(SID[0], proj='-w-orch', cwd='/w/orch', t=self.now - 900, agents=1)
        self.h.session(SID[1], proj='-w-chat', cwd='/w/chat', t=self.now - 5)           # more recent, but a project with solo only
        self.h.session(SID[2], proj='-w-old', cwd='/w/old', t=self.now - 2000, agents=1)
        ss, _ = catalog.scan_sessions()
        with patched(claude_alive_ids=lambda: {SID[0], SID[1], SID[2]}):
            projs = catalog.session_projects(ss)
        self.assertEqual([p['name'] for p in projs], ['chat', 'orch', 'old'])           # the order follows the latest activity of the representative candidate
        self.assertEqual(catalog.default_session(projs, ss), SID[0])                    # the default is the most recent among the orchestration projects

    def test_default_falls_back_to_latest_solo_then_none(self):
        self.h.session(SID[0], proj='-w-a', cwd='/w/a', t=self.now - 500)
        self.h.session(SID[1], proj='-w-b', cwd='/w/b', t=self.now - 50)
        ss, _ = catalog.scan_sessions()
        with patched(claude_alive_ids=lambda: {SID[0], SID[1]}):
            projs = catalog.session_projects(ss)
        self.assertEqual(catalog.default_session(projs, ss), SID[1])
        self.assertIsNone(catalog.default_session([], []))

    def test_codex_conversation_still_counts_as_orchestration(self):
        cx = {'id': '019a0001-0000-7000-8000-000000000000', 'project': 'cx', 'proj': 'cx', 'mtime': self.now - 600, 'agents': 0, 'agents_mtime': 0,
              'provider': 'codex', 'origin': 'tui', 'title': '', 'active': 0}
        solo = {'id': SID[1], 'project': 'chat', 'proj': 'chat', 'mtime': self.now - 5, 'agents': 0, 'agents_mtime': 0, 'provider': 'claude',
                'active': 1, 'solo': True, 'title': ''}
        projs = catalog.session_projects([cx, solo])
        self.assertEqual(catalog.default_session(projs, [cx, solo]), cx['id'])

    def test_api_sessions_and_default(self):
        self.h.session(SID[0], proj='-w-orch', cwd='/w/orch', t=self.now - 900, agents=1)
        self.h.session(SID[1], proj='-w-chat', cwd='/w/chat', t=self.now - 5)
        with patched(claude_alive_ids=lambda: {SID[0], SID[1]}), mock.patch.object(server, 'FIXED_DEFAULT', None):
            code, body = call('/api/sessions')
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {'default', 'sessions', 'projects', 'sources'})
        self.assertEqual(body['default'], SID[0])
        self.assertEqual([s['id'] for s in body['sessions'] if s.get('solo')], [SID[1]])
        self.assertEqual([s['provider'] for s in body['sources']], ['claude', 'codex'])
        self.assertEqual(body['sources'][0]['agent_sessions'], 1)

    def test_api_sessions_with_nothing(self):
        with mock.patch.object(server, 'FIXED_DEFAULT', None), mock.patch.object(server, 'DEFAULT_SESSION', None):
            code, body = call('/api/sessions')
        self.assertEqual((code, body['default'], body['sessions'], body['projects']), (200, None, [], []))
        self.assertEqual(body['sources'][0]['sessions'], 0)

    def test_fixed_default_wins(self):
        self.h.session(SID[0], t=self.now - 5)
        with mock.patch.object(server, 'FIXED_DEFAULT', SID[7]):
            self.assertEqual(call('/api/sessions')[1]['default'], SID[7])


# ---------- 7. fonts ----------
class Fonts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.static = os.path.join(os.path.realpath(self.tmp.name), 'static')
        self.bin = bytes(range(256)) * 20                              # bytes that break if read as text
        write(os.path.join(self.static, 'fonts', 'Galmuri11.woff2'), self.bin, 'wb')
        write(os.path.join(self.static, 'fonts', 'a.woff'), 'w', 'w')
        write(os.path.join(self.static, 'fonts', 'b.ttf'), 't', 'w')
        write(os.path.join(self.static, 'fonts', 'fonts.css'), '@font-face{}', 'w')
        write(os.path.join(self.static, 'fonts', 'OFL.txt'), 'license', 'w')
        write(os.path.join(self.static, 'fonts', 'evil.exe'), 'x', 'w')
        write(os.path.join(self.static, 'fonts', 'sub', 'inner.woff2'), 'x', 'w')
        write(os.path.join(self.static, 'secret.woff2'), 'secret', 'w')
        os.symlink(os.path.join(self.static, 'secret.woff2'), os.path.join(self.static, 'fonts', 'link.woff2'))
        os.mkfifo(os.path.join(self.static, 'fonts', 'fifo.ttf'))
        os.makedirs(os.path.join(self.static, 'fonts', 'dir.woff2'))
        start_patches(self, STATIC=self.static)
        self.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(self.srv.server_close)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def get(self, path, host=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', path, headers=dict({'Host': host or 'localhost:%d' % self.port}, **(headers or {})))
        r = c.getresponse()
        return r.status, r.getheader('Content-Type'), r.getheader('Cache-Control'), r.getheader('Content-Encoding'), r.read()

    def test_serves_each_type_as_bytes(self):
        st, ctype, cache, enc, body = self.get('/fonts/Galmuri11.woff2', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual((st, ctype, enc), (200, 'font/woff2', None))        # fonts are not compressed again
        self.assertEqual(body, self.bin)
        self.assertIn('max-age', cache)
        for name, ctype, body in (('a.woff', 'font/woff', b'w'), ('b.ttf', 'font/ttf', b't'), ('fonts.css', 'text/css; charset=utf-8', b'@font-face{}'),
                                  ('OFL.txt', 'text/plain; charset=utf-8', b'license')):
            st, got_type, _, _, got = self.get('/fonts/' + name)
            self.assertEqual((st, got_type, got), (200, ctype, body), name)

    def test_refuses_other_names_and_paths(self):
        for path in ('/fonts/', '/fonts', '/fonts/evil.exe', '/fonts/sub/inner.woff2', '/fonts/../secret.woff2', '/fonts/%2e%2e/secret.woff2',
                     '/fonts/..%2fsecret.woff2', '/fonts/%2e%2e%2fsecret.woff2', '/fonts/a..woff2', '/fonts/nope.woff2', '/fonts/Galmuri11.woff2/x',
                     '/fonts/Galmuri11.WOFF2', '/fonts/Galmuri11.woff2%00.txt', '/fonts/a%20b.woff', '/fonts/\\secret.woff2'):
            st = self.get(path)[0]
            self.assertEqual(st, 404, path)

    def test_only_regular_files(self):
        self.assertEqual(self.get('/fonts/link.woff2')[0], 404)              # symlink
        self.assertEqual(self.get('/fonts/fifo.ttf')[0], 404)                # FIFO (refused without blocking)
        self.assertEqual(self.get('/fonts/dir.woff2')[0], 404)               # folder

    def test_symlinked_fonts_dir_is_refused(self):
        import shutil
        shutil.move(os.path.join(self.static, 'fonts'), os.path.join(self.static, 'real-fonts'))
        os.symlink(os.path.join(self.static, 'real-fonts'), os.path.join(self.static, 'fonts'))
        self.assertEqual(self.get('/fonts/a.woff')[0], 404)

    def test_host_check_applies(self):
        self.assertEqual(self.get('/fonts/a.woff', host='evil.test')[0], 403)

    def test_font_re(self):
        for ok in ('Galmuri11.woff2', 'a-b_c.1.ttf', 'x.css', 'OFL.txt', 'a.woff'):
            self.assertTrue(server.FONT_RE.fullmatch(ok), ok)
        for bad in ('x.exe', 'x', '.woff2', 'a/b.woff2', 'a b.woff2', 'x.woff2\n', 'x.WOFF2', 'x.woff2x'):
            self.assertFalse(server.FONT_RE.fullmatch(bad), repr(bad))

    def test_shipped_fonts_dir_is_servable_if_present(self):
        """If the repo's static/fonts/ exists, it holds only names that can be served by this rule (the UI side puts them there: skipped if absent)."""
        d = os.path.join(os.path.dirname(os.path.abspath(server.__file__)), 'static', 'fonts')
        if not os.path.isdir(d):
            self.skipTest('static/fonts 없음')
        for name in os.listdir(d):
            self.assertTrue(server.FONT_RE.fullmatch(name), name)


if __name__ == '__main__':
    unittest.main()
