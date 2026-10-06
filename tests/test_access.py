"""Tests of the access token: the check on non-loopback addresses (401, cookie, redirect, Bearer), the start output, --token / --no-auth, --host with any address or name,
and --version. The unit tests call board.access directly; the others start the real server.py (a separate process, ephemeral port, temporary HOME).

    python3 -m unittest discover -s tests
"""
import hmac
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import isolated_env, server  # noqa: E402
from test_release import FakeServer, Home, live_server, main_env, run_main, run_server  # noqa: E402

import board  # noqa: E402
from board import access  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = 'k3Xv9QpL2mZr8TnYb5Wc7HdJ4sAe6UgF'          # a fixed token for the tests that start a server with --token
COOKIE_RE = re.compile(r'agent_bullpen_(\d+)=([0-9a-f]{64}); Path=/; HttpOnly; SameSite=Strict')


class Unit(unittest.TestCase):
    def test_new_tokens_are_long_random_and_fit_an_address(self):
        a, b = access.new_token(), access.new_token()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 40)
        self.assertTrue(access.token_ok(a))

    def test_token_ok(self):
        self.assertTrue(access.token_ok(TOKEN))
        for bad in ('', 'short', 'a' * 15, 'a b' * 8, 'tok/en' * 4, 'tok?en=' * 3, 'ü' * 20, None):
            self.assertFalse(access.token_ok(bad), bad)
        self.assertTrue(access.token_ok('a' * 16))

    def test_loopback_means_localhost_127_and_1_only(self):
        for name in ('127.0.0.1', '127.5.5.5', '::1', '[::1]', 'localhost', 'LOCALHOST', 'localhost.', '::ffff:127.0.0.1'):
            self.assertTrue(access.is_loopback(name), name)
        for name in ('0.0.0.0', '::', '192.168.0.5', '10.0.0.1', '100.100.0.7', 'fd7a:115c:a1e0::1', 'my.box', 'localhost.evil.test', '::ffff:10.0.0.1', '', None):
            self.assertFalse(access.is_loopback(name), name)

    def test_shown_addresses(self):
        self.assertEqual(access.shown_addresses('127.0.0.1'), ['localhost'])
        self.assertEqual(access.shown_addresses('::1'), ['[::1]'])
        self.assertEqual(access.shown_addresses('192.168.0.5'), ['192.168.0.5'])
        self.assertEqual(access.shown_addresses('my.box'), ['my.box'])
        with mock.patch.object(access, 'local_addresses', lambda: ['192.168.0.5', '100.64.0.2', '10.0.0.9', '10.0.0.10', '10.0.0.11']):
            self.assertEqual(access.shown_addresses('0.0.0.0'), ['localhost', '192.168.0.5', '100.64.0.2', '10.0.0.9', '10.0.0.10'])
            self.assertEqual(access.shown_addresses('::')[0], 'localhost')

    def test_local_addresses_are_other_devices_addresses_and_never_raise(self):
        got = access.local_addresses()
        for a in got:
            ip = __import__('ipaddress').ip_address(a)
            self.assertEqual(ip.version, 4)
            self.assertFalse(ip.is_loopback or ip.is_link_local or ip.is_unspecified, a)
        boom = mock.Mock(side_effect=OSError('no network here'))
        with mock.patch.object(socket, 'socket', boom), mock.patch.object(socket, 'getaddrinfo', boom), mock.patch.object(socket, 'if_nameindex', boom):
            self.assertEqual(access.local_addresses(), [])

    def test_a_cookie_that_differs_in_its_last_character_is_denied_whatever_the_salt(self):
        for salt in (bytes([0]) * 16, bytes([29]) * 16):                             # the cookie of the first does not end in `0`, of the second it does
            with mock.patch.object(access.secrets, 'token_bytes', return_value=salt):
                g = access.Gate(TOKEN)
            name, _, value = g.cookie_header(8790).split(';')[0].partition('=')
            self.assertEqual(value[-1] == '0', salt[0] == 29, 'the pinned salt no longer gives the cookie the test is about')
            other = value[:-1] + ('1' if value[-1] == '0' else '0')
            self.assertEqual(g.judge('%s=%s' % (name, other), None, '', 8790), (access.DENY, None), value)
            self.assertEqual(g.judge('%s=%s' % (name, value), None, '', 8790), (access.OK, None), value)

    def test_judge(self):
        g = access.Gate(TOKEN)
        cookie = g.cookie_header(8790).split(';')[0]
        name, _, value = cookie.partition('=')
        self.assertEqual(name, 'agent_bullpen_8790')
        self.assertNotIn(TOKEN, cookie)                                              # the cookie holds a value derived from the token, never the token
        self.assertEqual(g.judge(None, None, '', 8790), (access.DENY, None))
        self.assertEqual(g.judge(cookie, None, '', 8790), (access.OK, None))
        self.assertEqual(g.judge('x=1; ' + cookie + '; y=2', None, '', 8790), (access.OK, None))
        self.assertEqual(g.judge(cookie, None, '', 8791), (access.DENY, None))       # a cookie of another port's server
        self.assertEqual(g.judge('agent_bullpen_8790=' + value[:-1] + ('1' if value[-1] == '0' else '0'), None, '', 8790), (access.DENY, None))       # a last character that differs, whatever the salt gave
        self.assertEqual(g.judge(None, 'Bearer ' + TOKEN, '', 8790), (access.OK, None))
        self.assertEqual(g.judge(None, 'bearer   ' + TOKEN + ' ', '', 8790), (access.OK, None))
        for bad in ('Bearer ' + TOKEN[:-1], 'Basic ' + TOKEN, TOKEN, 'Bearer', 'Bearer ' + value):
            self.assertEqual(g.judge(None, bad, '', 8790), (access.DENY, None), bad)
        self.assertEqual(g.judge(None, None, 'token=' + TOKEN, 8790), (access.LOGIN, []))
        self.assertEqual(g.judge(None, None, 'session=a&token=%s&demo=all&session=b' % TOKEN, 8790), (access.LOGIN, [('session', 'a'), ('demo', 'all'), ('session', 'b')]))
        self.assertEqual(g.judge(None, None, 'token=wrong', 8790), (access.DENY, None))
        self.assertEqual(g.judge(cookie, None, 'token=wrong', 8790), (access.OK, None))      # a stale token in the address does not lock out a valid cookie
        self.assertEqual(g.judge(None, None, 'token=wrong&token=' + TOKEN, 8790)[0], access.LOGIN)

    def test_tokens_and_cookies_are_compared_in_constant_time(self):
        g = access.Gate(TOKEN)
        cookie = g.cookie_header(8790).split(';')[0]
        calls = []
        real = hmac.compare_digest

        def spy(a, b):
            calls.append((type(a), type(b)))
            return real(a, b)
        with mock.patch.object(access.hmac, 'compare_digest', spy):
            for args in ((None, None, 'token=' + TOKEN), (cookie, None, ''), (None, 'Bearer ' + TOKEN, '')):
                del calls[:]
                g.judge(args[0], args[1], args[2], 8790)
                self.assertTrue(calls, args)
                self.assertTrue(all(c == (bytes, bytes) for c in calls), calls)
        with open(os.path.join(ROOT, 'board', 'access.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertNotRegex(src, r'[^=!]==\s*(self\._token|self\._cookie|token)\b')       # no plain comparison of a secret

    def test_the_location_never_leaves_this_host(self):
        self.assertEqual(access.clean_location('/game', [('demo', 'all'), ('a', 'b c')]), '/game?demo=all&a=b+c')
        self.assertEqual(access.clean_location('/', []), '/')
        for path in ('//evil.test/x', '/\\evil.test', 'evil.test', ''):
            self.assertEqual(access.clean_location(path, []), '/', path)

    def test_the_denied_page_is_short_has_both_languages_and_repeats_nothing(self):
        page = access.denied_page().decode()
        self.assertIn('<h1>Access token required</h1>', page)
        self.assertIn('<h1>접속 토큰이 필요합니다</h1>', page)
        self.assertIn('?token=', page)
        self.assertLess(len(page), 2000)

    def test_log_lines_lose_the_token(self):
        self.assertEqual(access.redact('127.0.0.1 "GET /?token=abc123&x=1 HTTP/1.1" 302 -'), '127.0.0.1 "GET /?token=…&x=1 HTTP/1.1" 302 -')
        self.assertEqual(access.redact('"GET /game?session=s&token=abc HTTP/1.1" 200 -'), '"GET /game?session=s&token=… HTTP/1.1" 200 -')
        self.assertEqual(access.redact('"GET /api/state HTTP/1.1" 200 -'), '"GET /api/state HTTP/1.1" 200 -')


class StartOutput(unittest.TestCase):
    """main() with fake servers: which listeners get a gate, and what is printed."""

    def setUp(self):
        self.h = Home(self)

    def run_with(self, argv, n=1, lang='en', env=None):
        servers = [FakeServer() for _ in range(n)]
        with main_env(self, self.h, run_inline=False), mock.patch.dict(os.environ, env or {}):
            out, err, code = run_main(['--port', '0'] + argv, servers=servers, lang=lang)
        return out, err, code, servers

    def token_of(self, out):
        found = set(re.findall(r'\?token=([A-Za-z0-9_.~-]+)', out))
        self.assertEqual(len(found), 1, out)
        return found.pop()

    def test_loopback_is_as_before(self):
        out, err, code, (srv,) = self.run_with([])
        self.assertEqual((err, code, srv.gate), ('', None, None))
        self.assertTrue(out.startswith('Dashboard: http://localhost:8790/\n'))
        self.assertNotIn('token', out)

    def test_a_wildcard_gets_a_random_token_and_the_addresses_of_this_machine(self):
        with mock.patch.object(access, 'local_addresses', lambda: ['192.168.0.5', '100.64.0.2']):
            out, err, code, (srv,) = self.run_with(['--host', '0.0.0.0'])
        self.assertEqual((err, code), ('', None))
        self.assertIsInstance(srv.gate, access.Gate)
        tok = self.token_of(out)
        self.assertEqual(len(tok), 43)
        lines = out.splitlines()
        self.assertEqual(lines[0], 'Dashboard — an access token is needed. Open one of these:')
        self.assertEqual(lines[1:4], ['  http://localhost:8790/?token=' + tok, '  http://192.168.0.5:8790/?token=' + tok, '  http://100.64.0.2:8790/?token=' + tok])
        self.assertIn('cookie', lines[4])
        self.assertTrue(srv.gate.judge(None, 'Bearer ' + tok, '', 8790)[0] == access.OK)

    def test_every_start_makes_a_new_token(self):
        a = self.token_of(self.run_with(['--host', '192.168.0.5'])[0])
        b = self.token_of(self.run_with(['--host', '192.168.0.5'])[0])
        self.assertNotEqual(a, b)

    def test_ipv6_wildcard_a_lan_address_and_a_name_are_guarded_too(self):
        for host, shown in (('::', 'localhost'), ('192.168.0.5', '192.168.0.5'), ('fd7a:115c:a1e0::1', '[fd7a:115c:a1e0::1]'), ('my.box', 'my.box'), ('[::]', 'localhost')):
            with mock.patch.object(access, 'local_addresses', lambda: []):
                out, err, code, (srv,) = self.run_with(['--host', host])
            self.assertEqual(err, '', host)
            self.assertIsNotNone(srv.gate, host)
            self.assertIn('  http://%s:8790/?token=' % shown, out, host)

    def test_a_mix_guards_only_the_listeners_that_are_not_loopback(self):
        out, err, code, (a, b) = self.run_with(['--host', '127.0.0.1', '--host', '100.100.0.7'], n=2)
        self.assertEqual(err, '')
        self.assertIsNone(a.gate)
        self.assertIsNotNone(b.gate)
        self.assertIn('Dashboard: http://localhost:8790/', out)
        self.assertIn('  http://100.100.0.7:8790/?token=', out)

    def test_a_fixed_token_comes_from_the_option_or_the_environment_and_guards_loopback_too(self):
        out, err, code, (srv,) = self.run_with(['--token', TOKEN])
        self.assertEqual((err, code, self.token_of(out)), ('', None, TOKEN))
        self.assertIsNotNone(srv.gate)
        out, err, code, (srv,) = self.run_with(['--host', '0.0.0.0'], env={'AGENT_BULLPEN_TOKEN': TOKEN})
        self.assertEqual(self.token_of(out), TOKEN)
        out, err, code, (srv,) = self.run_with(['--token', 'a' * 20], env={'AGENT_BULLPEN_TOKEN': TOKEN})
        self.assertEqual(self.token_of(out), 'a' * 20)                                    # the option wins over the environment

    def test_no_auth_switches_the_check_off_and_warns_loudly(self):
        out, err, code, (srv,) = self.run_with(['--host', '0.0.0.0', '--no-auth'])
        self.assertIsNone(srv.gate)
        self.assertNotIn('token', out)
        lines = err.splitlines()
        self.assertEqual((len(lines), lines[0], lines[-1]), (4, '!' * 72, '!' * 72))
        self.assertIn('WARNING — NO AUTHENTICATION', lines[1])
        self.assertIn('listening on 0.0.0.0', lines[2])
        out, err, code, (srv,) = self.run_with(['--no-auth'], env={'AGENT_BULLPEN_TOKEN': TOKEN})        # on loopback it is a no-op, and it beats the environment
        self.assertEqual((err, srv.gate), ('', None))

    def test_the_rules_keep_the_old_host_names_when_there_is_no_check(self):
        with main_env(self, self.h, run_inline=False):
            run_main(['--port', '0', '--host', '192.168.0.5', '--no-auth', '--allow-host', 'box.example.net'], servers=[FakeServer()])
            self.assertTrue(server.host_allowed('192.168.0.5:8790'))
            self.assertTrue(server.host_allowed('box.example.net'))
            self.assertFalse(server.host_allowed('192.168.0.6'))

    def test_bad_options_end_with_one_line(self):
        for argv, env, word in ((['--token', 'short'], None, '16'), (['--token', TOKEN, '--no-auth'], None, '--no-auth and --token'), ([], {'AGENT_BULLPEN_TOKEN': 'short'}, 'AGENT_BULLPEN_TOKEN'),
                                (['--host', 'a b'], None, 'Cannot listen on this address'), (['--host', '0.0.0.0:8790'], None, 'Cannot listen on this address')):
            out, err, code, _ = self.run_with(argv, env=env)
            self.assertEqual((code, out), (2, ''), argv)
            self.assertIn(word, err, argv)

    def test_the_korean_texts(self):
        out, err, code, _ = self.run_with(['--host', '192.168.0.5'], lang='ko')
        self.assertIn('접속 토큰이 필요합니다', out)
        self.assertIn('쿠키', out)


def request(port, path='/', host=None, headers=None, address='127.0.0.1'):
    c = http.client.HTTPConnection(address, port, timeout=15)
    h = {'Host': host or 'localhost:%d' % port}
    h.update(headers or {})
    c.request('GET', path, headers=h)
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), body)
    c.close()
    return out


class LiveServer(unittest.TestCase):
    """A real server process on 0.0.0.0, and on loopback for the cases that stay as they were."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')
        os.makedirs(os.path.join(self.home, '.claude', 'projects'))

    def serve(self, *args, **kw):
        return live_server(['--port', '0'] + list(args), self.home, **kw)

    def login(self, port, token):
        status, headers, body = request(port, '/?token=' + token)
        self.assertEqual(status, 302)
        m = COOKIE_RE.fullmatch(headers['set-cookie'])
        self.assertTrue(m, headers['set-cookie'])
        return '%s=%s' % ('agent_bullpen_' + m.group(1), m.group(2))

    def test_the_wildcard_starts_and_the_first_visit_asks_for_the_token(self):
        with self.serve('--host', '0.0.0.0', '--token', TOKEN) as run:
            port = run.port()
            status, headers, body = request(port, '/')
            self.assertEqual(status, 401)
            self.assertTrue(headers['content-type'].startswith('text/html'))
            self.assertIn('Bearer', headers['www-authenticate'])
            text = body.decode()
            self.assertIn('Access token required', text)
            self.assertIn('접속 토큰이 필요합니다', text)
            self.assertNotIn(TOKEN, text)
            for path in ('/game', '/board.js', '/fonts/x.woff2', '/locales/en.json', '/api/i18n', '/api/plans', '/api/sessions', '/api/state?session=nope', '/nothing', '//x'):
                self.assertEqual(request(port, path)[0], 401, path)
            status, headers, body = request(port, '/api/plans')
            self.assertEqual((status, json.loads(body)), (401, {'error': 'unauthorized', 'error_code': 'unauthorized'}))
            self.assertEqual(request(port, '/api/plans', headers={'Authorization': 'Bearer ' + TOKEN[:-1] + 'x'})[0], 401)
            self.assertEqual(request(port, '/?token=' + TOKEN[:-1] + 'x')[0], 401)
            self.assertNotIn('set-cookie', request(port, '/?token=wrong')[1])

    def test_any_host_name_is_accepted_when_the_token_is_right(self):
        with self.serve('--host', '0.0.0.0', '--token', TOKEN) as run:
            port = run.port()
            for host in ('evil.example', '192.168.0.9:%d' % port, 'my.box'):
                self.assertEqual(request(port, '/api/plans', host=host)[0], 401, host)          # not a 403 about the name: the token is what is asked for
                self.assertEqual(request(port, '/api/plans', host=host, headers={'Authorization': 'Bearer ' + TOKEN})[0], 200, host)
            self.assertEqual(request(port, '/api/plans', host=None, headers={'Authorization': 'Bearer ' + TOKEN, 'Host': ''})[0], 200)

    def test_the_token_in_the_address_sets_a_cookie_and_goes_back_without_it(self):
        with self.serve('--host', '0.0.0.0', '--token', TOKEN) as run:
            port = run.port()
            status, headers, body = request(port, '/?token=' + TOKEN)
            self.assertEqual((status, headers['location'], body), (302, '/', b''))
            m = COOKIE_RE.fullmatch(headers['set-cookie'])
            self.assertEqual(int(m.group(1)), port)
            self.assertNotEqual(m.group(2), TOKEN)
            self.assertNotIn(TOKEN, json.dumps(headers))                                  # no header repeats the token
            status, headers, body = request(port, '/game?demo=all&session=abc&token=%s&x=1' % TOKEN)
            self.assertEqual((status, headers['location']), (302, '/game?demo=all&session=abc&x=1'))
            status, headers, body = request(port, '//evil.example/?token=' + TOKEN)
            self.assertEqual((status, headers['location'].startswith('//')), (302, False))
            status, headers, body = request(port, '/api/plans?token=' + TOKEN)
            self.assertEqual((status, headers['location']), (302, '/api/plans'))

    def test_with_the_cookie_every_page_and_api_works_and_a_reload_works(self):
        with self.serve('--host', '0.0.0.0', '--token', TOKEN) as run:
            port = run.port()
            cookie = self.login(port, TOKEN)
            for _ in range(2):                                                            # the second round is a reload
                for path, ctype in (('/', 'text/html'), ('/game', 'text/html'), ('/board.js', 'text/javascript'), ('/api/i18n', 'application/json'), ('/api/plans', 'application/json'),
                                    ('/api/sessions', 'application/json')):
                    status, headers, body = request(port, path, headers={'Cookie': cookie})
                    self.assertEqual(status, 200, path)
                    self.assertTrue(headers['content-type'].startswith(ctype), path)
                    self.assertNotIn(TOKEN.encode(), body)
            self.assertEqual(request(port, '/', headers={'Cookie': cookie[:-1] + ('0' if cookie[-1] != '0' else '1')})[0], 401)
            self.assertEqual(request(port, '/', headers={'Cookie': cookie.replace('agent_bullpen_%d' % port, 'agent_bullpen_%d' % (port + 1))})[0], 401)
            self.assertEqual(request(port, '/', headers={'Authorization': 'Bearer ' + TOKEN})[0], 200)

    def test_a_random_token_is_in_the_output_only_where_the_user_reads_it(self):
        with self.serve('--host', '0.0.0.0', extra_env={'AGENT_BULLPEN_LOG': '1'}) as run:
            port = run.port()
            tok = re.search(r'\?token=([A-Za-z0-9_-]+)', run.out()).group(1)
            self.assertEqual(len(tok), 43)
            cookie = self.login(port, tok)
            self.assertEqual(request(port, '/api/plans', headers={'Cookie': cookie})[0], 200)
            self.assertEqual(request(port, '/api/plans', headers={'Authorization': 'Bearer ' + tok})[0], 200)
            before = run.out()
        out, err = run.out_text, run.err_text
        urls = [ln for ln in out.splitlines() if ln.startswith('  http://') and '?token=' in ln]
        self.assertTrue(urls)
        self.assertEqual(out.count(tok), len(urls))                                       # only in the printed addresses
        logged = [ln for ln in out.splitlines() if ' "GET ' in ln]
        self.assertTrue(any('/?token=…' in ln for ln in logged), logged)                  # the request line of the first visit is in the log, without the value
        self.assertNotIn(tok, '\n'.join(logged))
        self.assertNotIn(tok, err)
        self.assertEqual(err, '')

    def test_another_address_of_this_machine_is_refused_without_the_token(self):
        others = access.local_addresses()
        if not others:
            self.skipTest('this machine has no address besides loopback')
        with self.serve('--host', '0.0.0.0', '--token', TOKEN) as run:
            port = run.port()
            self.assertIn('http://%s:%d/?token=%s' % (others[0], port, TOKEN), run.out())
            for address in others[:3]:
                try:
                    status, headers, body = request(port, '/', host='%s:%d' % (address, port), address=address)
                except OSError:
                    continue                                                                # an address of this machine that a firewall or the network setup does not let back in
                self.assertEqual(status, 401, address)
                self.assertIn(b'Access token required', body)
                status, headers, body = request(port, '/api/state', host='%s:%d' % (address, port), address=address)
                self.assertEqual(status, 401, address)
                status, headers, body = request(port, '/?token=' + TOKEN, host='%s:%d' % (address, port), address=address)
                self.assertEqual((status, headers['location']), (302, '/'), address)
                cookie = headers['set-cookie'].split(';')[0]
                self.assertEqual(request(port, '/', host='%s:%d' % (address, port), address=address, headers={'Cookie': cookie})[0], 200)

    def test_the_ipv6_wildcard_starts_and_answers_on_both_families(self):
        if not socket.has_ipv6:
            self.skipTest('no IPv6')
        with self.serve('--host', '::', '--token', TOKEN, wait=('프로세스 판정',)) as run:
            if run.p.poll() is not None:
                self.skipTest('cannot listen on :: here: ' + run.err())
            port = run.port()
            self.assertEqual(request(port, '/', address='127.0.0.1')[0], 401)
            try:
                c = http.client.HTTPConnection('::1', port, timeout=10)
                c.request('GET', '/', headers={'Host': 'localhost'})
                self.assertEqual(c.getresponse().status, 401)
            except OSError:
                self.skipTest('::1 is not reachable here')

    def test_loopback_stays_as_it_was(self):
        with self.serve() as run:
            port = run.port()
            self.assertNotIn('token', run.out())
            status, headers, body = request(port, '/')
            self.assertEqual(status, 200)
            self.assertNotIn('set-cookie', headers)
            self.assertEqual(request(port, '/?token=anything')[0], 200)                  # no check here: the parameter means nothing
            self.assertEqual(request(port, '/api/plans', host='evil.example')[0], 403)   # the Host allow list is what guards loopback
            self.assertEqual(request(port, '/api/plans', host='localhost:1')[0], 200)
        self.assertEqual(run.err_text, '')

    def test_a_fixed_token_guards_loopback_too(self):
        with self.serve('--token', TOKEN) as run:
            port = run.port()
            self.assertEqual(request(port, '/api/plans')[0], 401)
            self.assertEqual(request(port, '/api/plans', headers={'Authorization': 'Bearer ' + TOKEN})[0], 200)
            self.assertIn('?token=' + TOKEN, run.out())
        with self.serve(extra_env={'AGENT_BULLPEN_TOKEN': TOKEN}) as run:
            self.assertEqual(request(run.port(), '/api/plans')[0], 401)
        with self.serve('--no-auth', extra_env={'AGENT_BULLPEN_TOKEN': TOKEN}) as run:
            self.assertEqual(request(run.port(), '/api/plans')[0], 200)

    def test_no_auth_on_a_wildcard_keeps_the_old_host_rule_and_shouts(self):
        with self.serve('--host', '0.0.0.0', '--no-auth') as run:
            port = run.port()
            self.assertEqual(request(port, '/api/plans')[0], 200)
            self.assertEqual(request(port, '/api/plans', host='192.168.0.9:%d' % port)[0], 403)
        self.assertIn('!' * 72, run.err_text)
        self.assertIn('경고 — 인증 없음', run.err_text)
        self.assertNotIn('token', run.out_text)
        with self.serve('--host', '0.0.0.0', '--no-auth', '--allow-host', '192.168.0.9') as run:
            self.assertEqual(request(run.port(), '/api/plans', host='192.168.0.9:1')[0], 200)

    def test_host_names_work_and_one_that_does_not_resolve_fails_cleanly(self):
        with self.serve('--host', 'localhost') as run:
            self.assertEqual(request(run.port(), '/api/plans')[0], 200)
            self.assertNotIn('token', run.out())
        out, err, code = run_server(['--port', '0', '--host', 'no-such-host.invalid'], self.home, wait=('zzz',), timeout=20)
        self.assertEqual((out, code), ('', 1))
        self.assertEqual(err.count('\n'), 1)
        self.assertIn('no-such-host.invalid:', err)
        self.assertIn('--host', err)


class Version(unittest.TestCase):
    def test_server_prints_the_version(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'server.py'), '--version'], capture_output=True, text=True, timeout=30, env=isolated_env(tempfile.gettempdir()))
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr), (0, 'Agent Bullpen 0.2.1', ''))
        self.assertEqual(board.__version__, '0.2.1')

    def test_the_changelog_names_the_same_version(self):
        with open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8') as f:
            heads = re.findall(r'^## (\S+)', f.read(), re.M)
        released = [h for h in heads if h != 'Unreleased']          # what is done since the last release has its own heading above it
        self.assertEqual(released[0], board.__version__)
        self.assertEqual([i for i, h in enumerate(heads) if h == 'Unreleased'], [0] if 'Unreleased' in heads else [])      # ... and only at the top


if __name__ == '__main__':
    unittest.main()


class SlowNameLookup(unittest.TestCase):
    """The host-name fallback of local_addresses() must not hold the start-up output back (macOS can wait on DNS for a .local name)."""

    def test_a_slow_lookup_is_given_up_after_a_second(self):
        def slow(*a, **k):
            time.sleep(5)
            return [(socket.AF_INET, 0, 0, '', ('192.0.2.9', 0))]
        with mock.patch.object(socket, 'if_nameindex', lambda: []), mock.patch.object(socket, 'getaddrinfo', slow):
            t0 = time.time()
            self.assertEqual(access.local_addresses(), [])
            self.assertLess(time.time() - t0, 2.5)

    def test_a_quick_lookup_is_used(self):
        with mock.patch.object(socket, 'if_nameindex', lambda: []), \
                mock.patch.object(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, 0, 0, '', ('192.0.2.9', 0))]):
            self.assertEqual(access.local_addresses(), ['192.0.2.9'])
