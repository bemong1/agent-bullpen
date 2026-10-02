"""Tests of statusline.py, the status line command of Claude Code that keeps the plan usage for the dashboard.

    python3 -m unittest discover -s tests

The command is run as a separate process (the way Claude Code runs it) with a private HOME and cache folder, never the real ones.
"""
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'statusline.py')
sys.path.insert(0, ROOT)
import statusline  # noqa: E402

LIMITS = {'five_hour': {'used_percentage': 42.3, 'resets_at': 1790000000},
          'seven_day': {'used_percentage': 18, 'resets_at': '2026-10-08T03:00:00.000Z'}}


def slurp(path, **kw):
    with open(path, **kw) as f:
        return f.read()


def payload(rate_limits=LIMITS, **extra):
    d = {'session_id': 'DECOY-SESSION-0000', 'cwd': '/home/decoy/secret-project', 'transcript_path': '/home/decoy/.claude/projects/x/y.jsonl',
         'model': {'id': 'decoy-model', 'display_name': 'Decoy'}, 'workspace': {'current_dir': '/home/decoy/ws', 'project_dir': '/home/decoy/proj'},
         'cost': {'total_cost_usd': 12.34}, 'prompt': 'sk-ant-oat01-DECOYTOKEN', 'version': '9.9.9'}
    if rate_limits is not None:
        d['rate_limits'] = rate_limits
    d.update(extra)
    return json.dumps(d).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, 'home')
        self.cache = os.path.join(self.tmp.name, 'cache')
        os.makedirs(self.home)
        self.dir = os.path.join(self.cache, 'agent-bullpen')
        self.file = os.path.join(self.dir, 'statusline.json')

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        e = {'HOME': self.home, 'XDG_CACHE_HOME': self.cache, 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        e.update(extra)
        return e

    def run_cmd(self, data=b'', *args, timeout=30, **env):
        return subprocess.run([sys.executable, SCRIPT] + list(args), input=data, env=self.env(**env), capture_output=True, timeout=timeout)

    def saved(self):
        with open(self.file) as f:
            return json.load(f)


class Reading(Base):
    def test_keeps_only_the_rate_limits(self):
        r = self.run_cmd(payload())
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))
        d = self.saved()
        self.assertEqual(set(d), {'v', 'at', 'rate_limits'})
        self.assertEqual(d['v'], 1)
        self.assertLess(abs(d['at'] - time.time()), 30)
        self.assertEqual(d['rate_limits'], {'five_hour': {'used_percentage': 42.3, 'resets_at': 1790000000},
                                            'seven_day': {'used_percentage': 18.0, 'resets_at': '2026-10-08T03:00:00.000Z'}})

    def test_no_decoy_text_reaches_the_file(self):
        rl = {'five_hour': {'used_percentage': 12.5, 'resets_at': 1790000000, 'note': 'sk-ant-oat01-NESTED', 'cwd': '/home/decoy/nested'},
              'seven_day': {'used_percentage': 3, 'resets_at': 1790600000, 'model': {'id': 'decoy-model'}},
              'seven_day_opus': {'used_percentage': 1, 'resets_at': 1790600000},
              'note': 'sk-ant-oat01-TOP', 'path': '/home/decoy/rl',
              'spend_limit': {'used_percentage': 5, 'resets_at': '2026-11-01T00:00:00Z', 'used_usd': 1.5, 'limit_usd': 20, 'period': 'monthly',
                              'token': 'sk-ant-oat01-SPEND'}}
        self.run_cmd(payload(rl, extra_top={'cwd': '/home/decoy/x'}))
        raw = slurp(self.file)
        for bad in ('decoy', 'DECOY', 'sk-ant', '/home', 'opus', 'note', 'token', 'session', 'cost', 'prompt'):
            self.assertNotIn(bad, raw)
        self.assertEqual(self.saved()['rate_limits']['spend_limit'],
                         {'used_percentage': 5.0, 'resets_at': '2026-11-01T00:00:00Z', 'used_usd': 1.5, 'limit_usd': 20.0, 'period': 'monthly'})

    def test_text_in_a_value_is_dropped_with_its_window(self):
        rl = {'five_hour': {'used_percentage': 10, 'resets_at': 'sk-ant-oat01-LEAK'},
              'seven_day': {'used_percentage': 'sk-ant-oat01-LEAK', 'resets_at': 1790600000}}
        self.run_cmd(payload(rl))
        self.assertFalse(os.path.exists(self.file))
        rl = {'five_hour': {'used_percentage': 10, 'resets_at': '/home/decoy/2026-10-08'},
              'spend_limit': {'used_percentage': 5, 'resets_at': 1790600000, 'period': 'sk-ant-LEAK/../x', 'used_usd': '/home/decoy'}}
        self.run_cmd(payload(rl))
        self.assertNotIn('decoy', slurp(self.file) if os.path.exists(self.file) else '')
        self.assertEqual(self.saved()['rate_limits'], {'spend_limit': {'used_percentage': 5.0, 'resets_at': 1790600000}})

    def test_input_without_usable_rate_limits_writes_nothing(self):
        for data in (payload(None), payload({}), payload([]), payload('x'), payload(7), payload(None, other=1), payload({'five_hour': None}),
                     payload({'five_hour': []}), payload({'five_hour': {'used_percentage': 5}}), payload({'five_hour': {'resets_at': 1790000000}}),
                     b'not json at all', b'{"rate_limits": {"five_hour": ', b'', b'[]', b'null', b'42', b'"text"', b'\xff\xfe\x00bad', b'{' * 5000,
                     b'[' * 200000, b'{"a":' * 100000 + b'1' + b'}' * 100000):
            r = self.run_cmd(data)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''), data[:40])
            self.assertFalse(os.path.exists(self.file), data[:40])

    def test_odd_numbers_are_refused(self):
        bad = [True, False, -1, 1001, 'NaN', 'Infinity', '12', None, [], {}, 1e999]
        for p in bad:
            body = '{"rate_limits":{"five_hour":{"used_percentage":%s,"resets_at":1790000000}}}' % (json.dumps(p) if not isinstance(p, str) or p not in ('NaN', 'Infinity') else p)
            self.assertEqual(self.run_cmd(body.encode()).returncode, 0)
            self.assertFalse(os.path.exists(self.file), body)
        for r in (True, 0, -5, 5e12, 'x', '', '2026', [1], {}, 'NaN', 1e999):
            body = '{"rate_limits":{"five_hour":{"used_percentage":5,"resets_at":%s}}}' % (json.dumps(r) if not isinstance(r, float) else '1e999')
            self.run_cmd(body.encode())
            self.assertFalse(os.path.exists(self.file), body)

    def test_reset_time_forms_are_kept_as_given(self):
        for r in (1790000000, 1790000000123, 1790000000.5, '2026-10-08T03:00:00Z', '2026-10-08T03:00:00+09:00', '2026-10-08 03:00:00'):
            self.assertEqual(statusline.sanitize({'five_hour': {'used_percentage': 1, 'resets_at': r}}),
                             {'five_hour': {'used_percentage': 1.0, 'resets_at': r}}, r)

    def test_input_size_limit(self):
        base = {'rate_limits': LIMITS, 'pad': ''}
        n = len(json.dumps(base).encode())
        exact = json.dumps(dict(base, pad='x' * ((1 << 20) - n))).encode()
        self.assertEqual(len(exact), 1 << 20)
        self.run_cmd(exact)
        self.assertTrue(os.path.exists(self.file))                               # exactly the limit is read
        os.unlink(self.file)
        r = self.run_cmd(exact + b' ')
        self.assertEqual((r.returncode, r.stdout), (0, b''))
        self.assertFalse(os.path.exists(self.file))                              # one byte over is not looked at
        r = self.run_cmd(b'{"rate_limits":' + b' ' * (5 << 20) + b'{}}')
        self.assertEqual((r.returncode, r.stdout), (0, b''))

    def test_never_fails_on_any_stdin(self):
        r = subprocess.run([sys.executable, SCRIPT], stdin=subprocess.DEVNULL, env=self.env(), capture_output=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))
        r = subprocess.run('%s %s <&-' % (shlex.quote(sys.executable), shlex.quote(SCRIPT)), shell=True, env=self.env(), capture_output=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, b''))
        r = subprocess.run('%s %s --show < /dev/null >&-' % (shlex.quote(sys.executable), shlex.quote(SCRIPT)), shell=True, env=self.env(), capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0)

    def test_never_fails_when_the_cache_cannot_be_used(self):
        blocker = os.path.join(self.tmp.name, 'blocker')
        open(blocker, 'w').close()
        r = self.run_cmd(payload(), '--show', XDG_CACHE_HOME=blocker)              # a file where the folder should be
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'5h 42% \xc2\xb7 7d 18%\n', b''))
        r = self.run_cmd(payload(), XDG_CACHE_HOME='/proc/nope/x')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))

    def test_board_package_is_not_imported_and_subprocess_only_for_a_chain(self):
        code = ('import sys; sys.path.insert(0, %r); import statusline; sys.stdin = open(%r); statusline.main([]); '
                'print(sorted(m for m in sys.modules if m == "board" or m.startswith("board.") or m in ("server", "subprocess", "urllib.request", "argparse")))'
                % (ROOT, os.devnull))
        r = subprocess.run([sys.executable, '-c', code], env=self.env(), capture_output=True, timeout=30)
        self.assertEqual(r.stdout.strip(), b'[]', r.stderr)

    def test_show_prints_a_short_text_only_without_a_chain(self):
        r = self.run_cmd(payload(), '--show')
        self.assertEqual(r.stdout, '5h 42% · 7d 18%\n'.encode())
        r = self.run_cmd(payload(None), '--show')
        self.assertEqual(r.stdout, b'')
        r = self.run_cmd(payload(), '--show', '--chain', 'cat >/dev/null; printf mine')
        self.assertEqual(r.stdout, b'mine')
        r = self.run_cmd(payload({'seven_day': {'used_percentage': 99.6, 'resets_at': 1790000000}}), '--show')
        self.assertEqual(r.stdout, '7d 100%\n'.encode())

    def test_unknown_options_are_ignored(self):
        r = self.run_cmd(payload(), '--nope', '--chain', '--chain-timeout', 'x', '-q')
        self.assertEqual((r.returncode, r.stdout), (0, b''))
        self.assertTrue(os.path.exists(self.file))


class Files(Base):
    def test_folder_and_file_are_private(self):
        old = os.umask(0)
        try:
            self.run_cmd(payload())
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(os.listdir(self.dir), ['statusline.json'])                    # no temporary file is left

    def test_default_folder_is_under_home_cache(self):
        env = {'HOME': self.home, 'PATH': os.environ.get('PATH', '')}
        subprocess.run([sys.executable, SCRIPT], input=payload(), env=env, timeout=30)
        self.assertTrue(os.path.isfile(os.path.join(self.home, '.cache', 'agent-bullpen', 'statusline.json')))

    def test_a_folder_others_can_write_is_not_used(self):
        for mode in (0o777, 0o775, 0o722):
            os.makedirs(self.dir, exist_ok=True)
            os.chmod(self.dir, mode)
            r = self.run_cmd(payload())
            self.assertEqual(r.returncode, 0)
            self.assertEqual(os.listdir(self.dir), [], oct(mode))
            os.chmod(self.dir, 0o700)

    def test_a_file_others_can_write_is_not_replaced(self):
        os.makedirs(self.dir, mode=0o700)
        with open(self.file, 'w') as f:
            f.write('{"keep": 1}')
        os.chmod(self.file, 0o666)
        self.run_cmd(payload())
        self.assertEqual(slurp(self.file), '{"keep": 1}')

    def test_a_link_at_the_file_is_replaced_not_followed(self):
        os.makedirs(self.dir, mode=0o700)
        target = os.path.join(self.tmp.name, 'victim')
        with open(target, 'w') as f:
            f.write('untouched')
        os.chmod(target, 0o600)
        os.symlink(target, self.file)
        self.run_cmd(payload())
        self.assertEqual(slurp(target), 'untouched')
        self.assertFalse(os.path.islink(self.file))
        self.assertEqual(self.saved()['v'], 1)

    def test_an_old_temporary_name_that_is_a_link_is_not_followed(self):
        os.makedirs(self.dir, mode=0o700)
        target = os.path.join(self.tmp.name, 'victim')
        with open(target, 'w') as f:
            f.write('untouched')
        for pid in range(1, 4):
            os.symlink(target, '%s.%d.tmp' % (self.file, pid))
        self.run_cmd(payload())
        self.assertEqual(slurp(target), 'untouched')

    def test_unchanged_reading_is_not_written_again_within_the_gap(self):
        statusline.save(statusline.sanitize(LIMITS), now=1000.0, path=self.file)
        before = os.stat(self.file)
        self.assertFalse(statusline.save(statusline.sanitize(LIMITS), now=1000.0 + statusline.WRITE_GAP - 1, path=self.file))
        after = os.stat(self.file)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.saved()['at'], 1000.0)
        self.assertTrue(statusline.save(statusline.sanitize(LIMITS), now=1000.0 + statusline.WRITE_GAP, path=self.file))      # the gap has passed
        self.assertEqual(self.saved()['at'], 1030.0)
        changed = statusline.sanitize({'five_hour': {'used_percentage': 43, 'resets_at': 1790000000}})
        self.assertTrue(statusline.save(changed, now=1031.0, path=self.file))                                                 # a changed value is written at once
        self.assertEqual(self.saved()['rate_limits'], changed)
        self.assertTrue(statusline.save(changed, now=900.0, path=self.file))                                                  # a saved time in the future does not block
        self.assertFalse(statusline.save({}, now=2000.0, path=self.file))                                                     # nothing to keep: the last reading stays
        self.assertEqual(self.saved()['at'], 900.0)

    def test_throttle_across_processes(self):
        self.run_cmd(payload())
        a = os.stat(self.file)
        self.run_cmd(payload())
        b = os.stat(self.file)
        self.assertEqual((a.st_ino, a.st_mtime_ns), (b.st_ino, b.st_mtime_ns))

    def test_a_broken_or_odd_saved_file_is_just_replaced(self):
        os.makedirs(self.dir, mode=0o700)
        for body in (b'garbage', b'[]', b'{"at": "x", "rate_limits": %s}' % json.dumps(statusline.sanitize(LIMITS)).encode(), b'\xff\x00', b'[' * 100000):
            with open(self.file, 'wb') as f:
                f.write(body)
            os.chmod(self.file, 0o600)
            self.run_cmd(payload())
            self.assertEqual(self.saved()['rate_limits']['five_hour']['used_percentage'], 42.3)
        os.unlink(self.file)
        os.mkfifo(self.file)                              # a FIFO there must not hang the command
        r = self.run_cmd(payload(), timeout=20)
        self.assertEqual(r.returncode, 0)


class Chain(Base):
    def test_the_chained_command_gets_the_same_input_and_its_output_is_passed_on_as_is(self):
        data = payload()
        r = self.run_cmd(data, '--chain', 'cat')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, data, b''))
        self.assertTrue(os.path.exists(self.file))                    # the reading is kept too
        r = self.run_cmd(data, "--chain=printf 'no newline \\377\\376 \\303\\251'")
        self.assertEqual(r.stdout, b'no newline \xff\xfe \xc3\xa9')
        r = self.run_cmd(data, '--chain', 'python3 -c "import sys,json; d=json.load(sys.stdin); print(d[\'model\'][\'display_name\'])"')
        self.assertEqual(r.stdout, b'Decoy\n')

    def test_the_environment_variable_works_the_same_way(self):
        r = self.run_cmd(payload(), STATUSLINE_NOPE='1', AGENT_BULLPEN_STATUSLINE_CHAIN='cat >/dev/null; echo from-env')
        self.assertEqual(r.stdout, b'from-env\n')
        r = self.run_cmd(payload(), '--chain', 'echo from-flag', AGENT_BULLPEN_STATUSLINE_CHAIN='echo from-env')
        self.assertEqual(r.stdout, b'from-flag\n')                    # the option wins

    def test_a_failing_chain_still_passes_its_output_and_exits_zero(self):
        r = self.run_cmd(payload(), '--chain', 'echo partial; exit 3')
        self.assertEqual((r.returncode, r.stdout), (0, b'partial\n'))
        r = self.run_cmd(payload(), '--chain', 'no-such-command-xyz')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))
        r = self.run_cmd(payload(), '--chain', 'echo oops >&2; exit 1')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))      # its error text is not printed either
        r = self.run_cmd(payload(), '--chain', 'kill -9 $$')
        self.assertEqual((r.returncode, r.stdout), (0, b''))
        r = self.run_cmd(payload(), '--chain', '   ')
        self.assertEqual((r.returncode, r.stdout), (0, b''))

    def test_a_chain_that_never_ends_is_stopped_with_everything_it_started(self):
        pidfile = os.path.join(self.tmp.name, 'pid')
        t0 = time.time()
        r = self.run_cmd(payload(), '--chain', 'sleep 60 & echo $! > %s; echo early; wait' % shlex.quote(pidfile), '--chain-timeout', '0.5')
        self.assertLess(time.time() - t0, 10)
        self.assertEqual((r.returncode, r.stdout), (0, b''))                       # nothing is printed on a timeout
        self.assertTrue(os.path.exists(self.file))                                  # but the reading was kept before the command ran
        pid = int(slurp(pidfile))
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail('the process the chain started is still running')

    def test_a_chain_that_does_not_read_its_input_is_fine(self):
        r = self.run_cmd(payload() + b' ' * (3 << 20), '--chain', 'echo ignored-input')
        self.assertEqual((r.returncode, r.stdout), (0, b'ignored-input\n'))

    def test_an_oversized_input_still_goes_whole_to_the_chain_but_is_not_kept(self):
        data = payload() + b' ' * (3 << 20)
        r = self.run_cmd(data, '--chain', 'wc -c')
        self.assertEqual(r.stdout.strip(), str(len(data)).encode())
        self.assertFalse(os.path.exists(self.file))

    def test_the_chain_is_not_run_again_from_inside_itself(self):
        marker = os.path.join(self.tmp.name, 'runs')
        inner = '%s %s --chain %s' % (shlex.quote(sys.executable), shlex.quote(SCRIPT), shlex.quote('echo ran >> ' + marker))
        r = self.run_cmd(payload(), '--chain', 'cat >/dev/null; ' + inner + '; echo outer')
        self.assertEqual(r.stdout, b'outer\n')
        self.assertFalse(os.path.exists(marker))

    def test_output_is_capped(self):
        r = self.run_cmd(payload(), '--chain', 'head -c 3000000 /dev/zero | tr "\\0" x')
        self.assertEqual(len(r.stdout), statusline.MAX_OUT)

    def test_a_closed_stdout_does_not_fail(self):
        r = subprocess.run('%s %s --chain %s >&-' % (shlex.quote(sys.executable), shlex.quote(SCRIPT), shlex.quote('echo hi')), shell=True,
                           input=payload(), env=self.env(), capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0)


class PrintConfig(Base):
    def settings(self, body, home=None):
        home = home or os.path.join(self.tmp.name, 'claude')
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, 'settings.json')
        with open(path, 'w') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))
        return home, path

    def digest(self, path):
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest(), os.stat(path).st_mtime_ns, os.stat(path).st_ino

    def test_chains_the_current_status_line_and_does_not_touch_the_file(self):
        cur = {'model': 'x', 'hooks': {'Stop': []}, 'statusLine': {'type': 'command', 'command': "~/bin/mine.sh --flag 'a b'", 'padding': 2}}
        home, path = self.settings(cur)
        before = self.digest(path)
        r = self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home)
        self.assertEqual(self.digest(path), before)
        self.assertEqual(os.listdir(home), ['settings.json'])
        self.assertEqual((r.returncode, r.stderr.count(b'\n') > 0), (0, True))
        out = json.loads(r.stdout)
        self.assertEqual(list(out), ['statusLine'])
        sl = out['statusLine']
        self.assertEqual((sl['type'], sl['padding']), ('command', 2))
        self.assertEqual(shlex.split(sl['command']), ['python3', SCRIPT, '--chain', "~/bin/mine.sh --flag 'a b'"])
        self.assertNotIn('hooks', r.stdout.decode())                                   # only the status line, not the rest of the settings

    def test_the_piece_runs_and_still_draws_the_old_status_line(self):
        home, _ = self.settings({'statusLine': {'type': 'command', 'command': "cat >/dev/null; echo 'my line'"}})
        out = json.loads(self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home).stdout)
        r = subprocess.run(out['statusLine']['command'], shell=True, input=payload(), env=self.env(), capture_output=True, timeout=30)
        self.assertEqual(r.stdout, b'my line\n')
        self.assertTrue(os.path.exists(self.file))

    def test_without_a_status_line(self):
        for body in ({'model': 'x'}, {}, {'statusLine': None}):
            home, path = self.settings(body)
            before = self.digest(path)
            r = self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home)
            self.assertEqual(self.digest(path), before)
            sl = json.loads(r.stdout)['statusLine']
            self.assertEqual(shlex.split(sl['command']), ['python3', SCRIPT])
            self.assertEqual(sl['type'], 'command')

    def test_missing_or_broken_settings_file(self):
        home = os.path.join(self.tmp.name, 'nothing-here')
        r = self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home)
        self.assertEqual(json.loads(r.stdout)['statusLine']['type'], 'command')
        self.assertFalse(os.path.exists(home))                                          # not even the folder is created
        for body in ('{broken', '[]', 'null', '\xff'):
            home, path = self.settings(body)
            r = self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home)
            self.assertEqual((r.returncode, json.loads(r.stdout)['statusLine']['type']), (0, 'command'))
            self.assertEqual(slurp(path, encoding='utf-8'), body)

    def test_default_location_is_dot_claude_under_home(self):
        home, path = self.settings({'statusLine': {'type': 'command', 'command': 'echo hi'}}, home=os.path.join(self.home, '.claude'))
        r = self.run_cmd(b'', '--print-config')
        self.assertEqual(shlex.split(json.loads(r.stdout)['statusLine']['command'])[-1], 'echo hi')

    def test_already_set_up_is_not_wrapped_twice(self):
        cmd = 'python3 %s --chain %s' % (shlex.quote(SCRIPT), shlex.quote('echo hi'))
        home, path = self.settings({'statusLine': {'type': 'command', 'command': cmd}})
        r = self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home)
        self.assertEqual(json.loads(r.stdout)['statusLine']['command'], cmd)

    def test_another_statusline_py_is_chained_and_this_one_in_any_spelling_is_not_wrapped_twice(self):
        other = 'python3 ~/bin/statusline.py'
        home, _ = self.settings({'statusLine': {'type': 'command', 'command': other}})
        sl = json.loads(self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home).stdout)['statusLine']
        self.assertEqual(shlex.split(sl['command']), ['python3', SCRIPT, '--chain', other])
        link_dir = os.path.join(self.home, 'tools')
        os.makedirs(link_dir)
        os.symlink(SCRIPT, os.path.join(link_dir, 'statusline.py'))
        for spelled in ('python3 ~/tools/statusline.py --show', 'python3 "$HOME/tools/statusline.py"', 'python3 %s' % os.path.join(link_dir, 'statusline.py')):
            cmd = spelled
            home, _ = self.settings({'statusLine': {'type': 'command', 'command': cmd}})
            got = json.loads(self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home).stdout)['statusLine']['command']
            self.assertEqual(got, cmd, spelled)
        home, _ = self.settings({'statusLine': {'type': 'command', 'command': "echo 'unbalanced"}})
        got = json.loads(self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home).stdout)['statusLine']['command']
        self.assertEqual(shlex.split(got), ['python3', SCRIPT, '--chain', "echo 'unbalanced"])

    def test_a_status_line_of_another_type_is_not_chained(self):
        home, path = self.settings({'statusLine': {'type': 'static', 'text': 'x'}})
        r = self.run_cmd(b'', '--print-config', CLAUDE_CONFIG_DIR=home)
        self.assertEqual(shlex.split(json.loads(r.stdout)['statusLine']['command']), ['python3', SCRIPT])
        self.assertIn(b'replace', r.stderr)

    def test_does_not_read_stdin_or_write_the_reading(self):
        home, _ = self.settings({})
        r = subprocess.run([sys.executable, SCRIPT, '--print-config'], stdin=subprocess.PIPE, env=self.env(CLAUDE_CONFIG_DIR=home), capture_output=True, timeout=10, input=None)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.cache))

    def test_help_prints_the_usage(self):
        r = self.run_cmd(b'', '--help')
        self.assertEqual(r.returncode, 0)
        self.assertIn(b'--print-config', r.stdout)
        self.assertFalse(os.path.exists(self.cache))


if __name__ == '__main__':
    unittest.main()
