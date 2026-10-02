"""Tests of the language base: dictionary registration and reading, the terminal language choice, substitution, /api/i18n and /locales, --lang and
--help --lang, and the browser-side JS checks (when node is there). They read temporary folders only (no real HOME transcript or credential file is opened).

    python3 -m unittest discover -s tests
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server, terminal_lang  # noqa: E402
from test_release import FakeServer, Home, main_env, run_main, write  # noqa: E402

from board import i18n  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = os.path.join(ROOT, 'static', 'locales')


def pack(code, name, messages=None, **extra):
    d = {'meta': {'code': code, 'name': name, 'locale': code}, 'messages': messages or {'a': 'A'}, 'formats': {}, 'limits': {}}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


class LocalesDir:
    """A test static/locales/: i18n.LOCALES_DIR points at this folder. Each case writes the files it needs."""

    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(os.path.realpath(self.tmp.name), 'locales')
        os.makedirs(self.dir)
        p = mock.patch.object(i18n, 'LOCALES_DIR', self.dir)
        p.start()
        case.addCleanup(p.stop)
        case.addCleanup(i18n._cache.clear)

    def put(self, name, text):
        return write(os.path.join(self.dir, name), text)


class Registry(unittest.TestCase):
    def setUp(self):
        self.loc = LocalesDir(self)

    def test_registers_only_well_formed_regular_json_files(self):
        loc = self.loc
        loc.put('en.json', pack('en', 'English'))
        loc.put('ko.json', pack('ko', '한국어'))
        loc.put('pt-BR.json', pack('pt-BR', 'Português (Brasil)'))
        loc.put('fr.json', '{not json')                                       # broken JSON
        loc.put('de.json', pack('xx', 'Deutsch'))                              # meta.code differs from the file name
        loc.put('it.json', json.dumps({'meta': {'code': 'it', 'name': 'Italiano'}}))   # no messages
        loc.put('es.json', json.dumps({'meta': {'code': 'es', 'name': 'Español'}, 'messages': {'a': 5}}))   # a value that is not text
        loc.put('nl.json', json.dumps({'meta': {'code': 'nl', 'name': ''}, 'messages': {}}))               # no name
        loc.put('_limits.json', pack('_limits', 'x'))                          # not a language file
        loc.put('.hidden.json', pack('en', 'x'))
        loc.put('README.txt', 'x')
        loc.put('ZH.json', pack('ZH', 'x'))                                    # an upper-case name is not registered (not EN.json: on a file system that folds case, as macOS does by default, that is en.json)
        loc.put('ko.json.bak', pack('ko', 'x'))
        os.makedirs(os.path.join(loc.dir, 'sub.json'))                         # a folder
        loc.put('real.json', pack('en', 'x'))                                  # a link to a real file
        os.symlink(os.path.join(loc.dir, 'real.json'), os.path.join(loc.dir, 'sv.json'))
        os.mkfifo(os.path.join(loc.dir, 'da.json'))                            # a FIFO: refused without blocking
        self.assertEqual(i18n.languages(), [{'code': 'en', 'name': 'English'}, {'code': 'ko', 'name': '한국어'}, {'code': 'pt-BR', 'name': 'Português (Brasil)'}])
        probs = i18n.problems()
        self.assertEqual(sorted(probs), ['da.json', 'de.json', 'es.json', 'fr.json', 'it.json', 'nl.json', 'sub.json', 'sv.json'])
        self.assertIn('not a regular file', probs['sv.json'])
        self.assertIn('meta.code', probs['de.json'])
        self.assertIn('messages', probs['it.json'])
        with open(os.path.join(loc.dir, 'ko.json'), 'rb') as f:
            self.assertEqual(i18n.registry()['ko']['data'], f.read())                          # the file's bytes as they are

    def test_english_comes_first_and_is_the_default(self):
        self.loc.put('ko.json', pack('ko', '한국어'))
        self.loc.put('en.json', pack('en', 'English'))
        self.loc.put('ar.json', pack('ar', 'العربية'))
        self.assertEqual([x['code'] for x in i18n.languages()], ['en', 'ar', 'ko'])
        self.assertEqual(i18n.default_code(), 'en')

    def test_default_without_english(self):
        self.assertEqual(i18n.default_code(), 'en')                            # English even with no dictionary at all (telling the user is the page's job)
        self.loc.put('ko.json', pack('ko', '한국어'))
        self.assertEqual(i18n.default_code(), 'ko')

    def test_missing_folder_is_empty(self):
        shutil.rmtree(self.loc.dir)
        self.assertEqual((i18n.registry(), i18n.languages(), i18n.problems()), ({}, [], {}))

    def test_a_changed_file_is_read_again_and_a_broken_one_drops_out(self):
        self.loc.put('ko.json', pack('ko', '한국어', {'a': 'one'}))
        self.assertEqual(i18n.registry()['ko']['pack']['messages'], {'a': 'one'})
        self.loc.put('ko.json', pack('ko', '한국어', {'a': 'two', 'b': 'x'}))     # the size changed, so it is read again
        self.assertEqual(i18n.registry()['ko']['pack']['messages'], {'a': 'two', 'b': 'x'})
        self.loc.put('ko.json', '{broken')
        self.assertNotIn('ko', i18n.registry())
        self.assertIn('ko.json', i18n.problems())

    def test_size_limit(self):
        self.loc.put('ko.json', pack('ko', '한국어', {'a': 'x' * (i18n.FILE_MAX + 10)}))
        self.assertNotIn('ko', i18n.registry())
        self.assertIn('too large', i18n.problems()['ko.json'])

    def test_file_re(self):
        for ok in ('en.json', 'ko.json', 'pt-BR.json', 'zh-Hant-TW.json', 'ast.json'):
            self.assertTrue(i18n.FILE_RE.fullmatch(ok), ok)
        for bad in ('_en.json', 'e.json', 'english.json', 'EN.json', 'en.JSON', 'en.json\n', 'en.json.bak', 'en-.json', '../en.json', 'a/en.json', 'en .json', 'en_US.json'):
            self.assertFalse(i18n.FILE_RE.fullmatch(bad), repr(bad))


class ShippedDictionaries(unittest.TestCase):
    def test_en_and_ko_are_registered_and_parallel(self):
        reg = i18n.registry()
        self.assertEqual(sorted(reg), ['en', 'ko'])
        self.assertEqual(i18n.problems(), {})
        en, ko = (reg[c]['pack'] for c in ('en', 'ko'))
        self.assertEqual(list(en), ['meta', 'messages', 'formats', 'limits'])
        self.assertEqual(list(ko), list(en))
        # The same keys in the same order are checked for the common sections and the section anchors only (the full check is tools/regress/i18n_check.py)
        base = lambda d: [k for k in d['messages'] if k.split('.')[0] in ('common', 'status', 'kind', 'time', 'unit') or k.endswith('._')]    # noqa: E731
        self.assertEqual(base(ko), base(en))
        self.assertEqual(len([k for k in base(en) if k.endswith('._')]), 13)
        self.assertEqual(i18n.languages(), [{'code': 'en', 'name': 'English'}, {'code': 'ko', 'name': '한국어'}])


class CliLang(unittest.TestCase):
    """The terminal language: --lang, AGENT_BULLPEN_LANG, LC_ALL, LC_MESSAGES, LANG, then English. The first value that is set wins; C, POSIX and unsupported languages give English."""

    def test_table(self):
        for flag, env, want in [
            (None, {}, 'en'),
            (None, {'LANG': 'C'}, 'en'),
            (None, {'LANG': 'C.UTF-8'}, 'en'),
            (None, {'LANG': 'POSIX'}, 'en'),
            (None, {'LANG': 'ko_KR.UTF-8'}, 'ko'),
            (None, {'LANG': 'ko_KR.eucKR'}, 'ko'),
            (None, {'LANG': 'ko'}, 'ko'),
            (None, {'LANG': 'KO_kr'}, 'ko'),
            (None, {'LANG': 'ko_KR.UTF-8@euro'}, 'ko'),
            (None, {'LANG': 'en_US.UTF-8'}, 'en'),
            (None, {'LANG': 'fr_FR.UTF-8'}, 'en'),                                  # unsupported
            (None, {'LC_ALL': 'en_US.UTF-8', 'LANG': 'ko_KR.UTF-8'}, 'en'),          # LC_ALL beats LANG
            (None, {'LC_MESSAGES': 'ko_KR.UTF-8', 'LANG': 'en_US.UTF-8'}, 'ko'),
            (None, {'LC_ALL': 'ko', 'LC_MESSAGES': 'en', 'LANG': 'en'}, 'ko'),
            (None, {'LC_ALL': 'C', 'LANG': 'ko_KR.UTF-8'}, 'en'),                    # POSIX: LC_ALL=C wins (it does not fall through)
            (None, {'LC_ALL': 'fr_FR', 'LANG': 'ko_KR.UTF-8'}, 'en'),
            (None, {'LC_ALL': '', 'LANG': 'ko_KR.UTF-8'}, 'ko'),                     # an empty value is not set
            (None, {'LC_ALL': '  ', 'LC_MESSAGES': '', 'LANG': 'ko'}, 'ko'),
            (None, {'AGENT_BULLPEN_LANG': 'ko', 'LANG': 'C'}, 'ko'),                 # how tests and tools pin the language
            (None, {'AGENT_BULLPEN_LANG': 'en', 'LC_ALL': 'ko_KR.UTF-8'}, 'en'),
            (None, {'AGENT_BULLPEN_LANG': 'auto', 'LANG': 'ko_KR.UTF-8'}, 'ko'),
            (None, {'AGENT_BULLPEN_LANG': 'xx', 'LANG': 'ko'}, 'en'),
            ('en', {'LANG': 'ko_KR.UTF-8'}, 'en'),                                   # --lang beats the environment
            ('ko', {'LANG': 'C'}, 'ko'),
            ('ko', {'AGENT_BULLPEN_LANG': 'en'}, 'ko'),
            ('KO', {}, 'ko'),
            ('ko_KR.UTF-8', {}, 'ko'),
            ('auto', {'LANG': 'ko_KR.UTF-8'}, 'ko'),                                 # auto = look at the environment
            ('', {'LANG': 'ko'}, 'ko'),
            ('xx', {'LANG': 'ko'}, 'en'),                                           # naming an unknown language gives English (no fall-through to the environment)
            ('auto', {}, 'en'),
        ]:
            with self.subTest(flag=flag, env=env):
                self.assertEqual(i18n.cli_lang(flag, env), want)

    def test_reads_the_process_environment_by_default(self):
        with mock.patch.dict(os.environ, {'LANG': 'ko_KR.UTF-8'}, clear=True):
            self.assertEqual(i18n.cli_lang(), 'ko')
        with mock.patch.dict(os.environ, {'LANG': 'C'}, clear=True):
            self.assertEqual(i18n.cli_lang(), 'en')

    def test_new_languages_need_no_code(self):
        loc = LocalesDir(self)
        loc.put('en.json', pack('en', 'English'))
        loc.put('ja.json', pack('ja', '日本語'))
        loc.put('pt-BR.json', pack('pt-BR', 'Português'))
        self.assertEqual(i18n.cli_lang(None, {'LANG': 'ja_JP.UTF-8'}), 'ja')
        self.assertEqual(i18n.cli_lang(None, {'LANG': 'pt_BR.UTF-8'}), 'pt-BR')
        self.assertEqual(i18n.cli_lang(None, {'LANG': 'pt_PT.UTF-8'}), 'en')       # pt-PT does not shrink to pt-BR
        self.assertEqual(i18n.cli_lang(None, {'LANG': 'ko_KR.UTF-8'}), 'en')        # this dictionary has no ko

    def test_no_dictionaries_at_all(self):
        LocalesDir(self)
        self.assertEqual(i18n.cli_lang('ko', {'LANG': 'ko'}), 'en')

    def test_match(self):
        sup = ['en', 'zh', 'zh-TW']
        for tag, want in [('zh-TW', 'zh-TW'), ('zh_tw', 'zh-TW'), ('zh-Hant-TW', 'zh'), ('zh-CN', 'zh'), ('EN-us', 'en'), ('ko', None), ('', None), ('C', None), (None, None), (5, None), ('-', None)]:
            self.assertEqual(i18n.match(tag, sup), want, tag)


class Fill(unittest.TestCase):
    def test_substitution(self):
        self.assertEqual(i18n.fill('{n}s ago', {'n': 5}), '5s ago')
        self.assertEqual(i18n.fill('{a} and {a} {b}', {'a': 'x'}), 'x and x {b}')       # a name without a value stays
        self.assertEqual(i18n.fill('{a}', {'a': 0}), '0')
        self.assertEqual(i18n.fill('{a}', {'a': None}), '{a}')
        self.assertEqual(i18n.fill('no params {} {1} {a b}', {'a': 'x'}), 'no params {} {1} {a b}')
        self.assertEqual(i18n.fill('a {name} b', {'name': '$& {name} \\1'}), 'a $& {name} \\1 b')      # values go in as plain text
        self.assertEqual(i18n.fill('{v:number} calls', {'v': 1234567}), '1,234,567 calls')
        self.assertEqual(i18n.fill('{v:number}', {'v': 'x'}), 'x')
        self.assertEqual(i18n.fill('{v:nope}', {'v': 'x'}), 'x')                         # an unknown formatter gives the value only

    def test_subject_particle_matches_the_browser_rule(self):
        # The same rule as gaI in static/i18n.js; the same table is in tools/regress/i18n_checks.js
        for word, want in [('서버', '서버가'), ('토큰', '토큰이'), ('T1-A', 'T1-A가'), ('T1', 'T1이'), ('T3', 'T3이'), ('T8', 'T8이'), ('orch-l', 'orch-l이'),
                           ('x-r', 'x-r이'), ('x-q', 'x-q가'), ('', '가'), ('값', '값이')]:
            self.assertEqual(i18n.fill('{w:subject}', {'w': word}), want, word)


class Translator(unittest.TestCase):
    def test_lookup_order_and_plural_other(self):
        loc = LocalesDir(self)
        loc.put('en.json', pack('en', 'English', {'a': 'A en', 'only.en': 'E {n}', 'p': {'one': '{n} item', 'other': '{n} items'}}))
        loc.put('ko.json', pack('ko', '한국어', {'a': 'A ko', 'p': {'other': '{n}개'}}))
        ko, en = i18n.translator('ko'), i18n.translator('en')
        self.assertEqual((ko('a'), en('a')), ('A ko', 'A en'))
        self.assertEqual(ko('only.en', n=3), 'E 3')                                     # missing in the chosen language: English
        self.assertEqual(ko('nowhere'), '[nowhere]')
        self.assertEqual((ko('p', n=1), en('p', n=1)), ('1개', '1 items'))               # the terminal has no plural algorithm: other
        self.assertEqual(i18n.translator('xx')('a'), 'A en')                              # an unknown language gives English

    def test_shipped_common_zone(self):
        t = i18n.translator('ko')
        self.assertEqual((t('status.running'), t('time.ago.hourMin', h=2, m=5)), ('작업 중', '2시간 5분 전'))
        self.assertEqual(i18n.translator('en')('time.ago.hourMin', h=2, m=5), '2h 5m ago')


class Http(unittest.TestCase):
    """/api/i18n and /locales/<file>: answered without a session, and only registered JSON file names are served."""

    def setUp(self):
        self.loc = LocalesDir(self)
        self.en, self.ko = pack('en', 'English', {'a': 'A'}), pack('ko', '한국어', {'a': '가'})
        self.loc.put('en.json', self.en)
        self.loc.put('ko.json', self.ko)
        self.loc.put('fr.json', '{broken')
        self.loc.put('_limits.json', pack('_limits', 'x'))
        self.loc.put('.hidden.json', pack('en', 'x'))
        os.makedirs(os.path.join(self.loc.dir, 'sub'))
        write(os.path.join(self.loc.dir, 'sub', 'ko.json'), self.ko)
        write(os.path.join(os.path.dirname(self.loc.dir), 'secret.json'), '{"secret": 1}')
        os.symlink(os.path.join(os.path.dirname(self.loc.dir), 'secret.json'), os.path.join(self.loc.dir, 'link.json'))
        os.mkfifo(os.path.join(self.loc.dir, 'fifo.json'))
        h = Home(self)                                                                     # a server with no session at all
        with mock.patch.object(server, 'DEFAULT_SESSION', None):
            self.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            self.addCleanup(self.srv.server_close)
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
            self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.assertIsNotNone(h)

    def get(self, path, host=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', path, headers=dict({'Host': host or 'localhost:%d' % self.port}, **(headers or {})))
        r = c.getresponse()
        return r.status, r.getheader('Content-Type'), r.getheader('Cache-Control'), r.read()

    def test_api_i18n_answers_without_any_session(self):
        st, ctype, cache, body = self.get('/api/i18n')
        self.assertEqual((st, ctype, cache), (200, 'application/json; charset=utf-8', 'no-store'))
        self.assertEqual(json.loads(body), {'default': 'en', 'languages': [{'code': 'en', 'name': 'English'}, {'code': 'ko', 'name': '한국어'}]})
        # the same server answers 404 to an API that needs a session: proof that this one is answered before the session check
        self.assertEqual(self.get('/api/state')[0], 404)
        self.assertEqual(self.get('/api/i18n?session=deadbeef')[0], 200)

    def test_api_i18n_follows_the_folder(self):
        self.loc.put('ja.json', pack('ja', '日本語'))
        langs = json.loads(self.get('/api/i18n')[3])['languages']
        self.assertEqual([x['code'] for x in langs], ['en', 'ja', 'ko'])                  # a file dropped in is seen without a restart (the folder is rescanned on every request)

    def test_locale_files_are_served_as_they_are(self):
        for name, text in (('en.json', self.en), ('ko.json', self.ko)):
            st, ctype, cache, body = self.get('/locales/' + name)
            self.assertEqual((st, ctype, cache), (200, 'application/json; charset=utf-8', 'no-store'), name)
            self.assertEqual(body, text.encode('utf-8'))
        self.assertEqual(self.get('/locales/ko.json?x=1')[0], 200)                          # the query is ignored

    def test_only_registered_names(self):
        for path in ('/locales/', '/locales', '/locales/xx.json', '/locales/fr.json', '/locales/_limits.json', '/locales/.hidden.json',
                     '/locales/sub/ko.json', '/locales/sub', '/locales/ko.json/x', '/locales/ko.json/', '/locales/KO.json', '/locales/ko.JSON', '/locales/ko',
                     '/locales/ko.json.bak', '/locales/ko.json%00', '/locales/ko.json%00.png', '/locales/..', '/locales/../en.json', '/locales/./ko.json',
                     '/locales/%2e%2e/secret.json', '/locales/..%2fsecret.json', '/locales/%2e%2e%2fsecret.json', '/locales/..\\secret.json',
                     '/locales//ko.json', '/locales/ko.json%20', '/locales/link.json', '/locales/fifo.json', '/locales/secret.json', '/locales/%6bo.json'):
            st = self.get(path)[0]
            self.assertEqual(st, 404, path)

    def test_host_check_applies(self):
        self.assertEqual(self.get('/api/i18n', host='evil.test')[0], 403)
        self.assertEqual(self.get('/locales/ko.json', host='evil.test')[0], 403)

    def test_i18n_script_is_served(self):
        st, ctype, _, body = self.get('/i18n.js')
        self.assertEqual((st, ctype), (200, 'text/javascript; charset=utf-8'))
        self.assertIn(b'const I18N', body)

    def test_gzip_for_larger_bodies(self):
        self.loc.put('ko.json', pack('ko', '한국어', {'a': '가' * 3000}))
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        c.request('GET', '/locales/ko.json', headers={'Host': 'localhost:%d' % self.port, 'Accept-Encoding': 'gzip'})
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader('Content-Encoding')), (200, 'gzip'))


class ServerArgs(unittest.TestCase):
    def run_main_keeping_globals(self, argv, **kw):
        with terminal_lang(None):                        # the environment decides (no pin); the module settings are put back after the run
            out = run_main(argv, lang=None, keep=True, **kw)
            return out + (server.CLI_LANG,)

    def test_scan_flag(self):
        f = server.scan_flag
        self.assertEqual(f(['--lang', 'ko'], '--lang'), 'ko')
        self.assertEqual(f(['--lang=ko'], '--lang'), 'ko')
        self.assertEqual(f(['--lang', 'ko', '--lang=en'], '--lang'), 'en')              # the last one
        self.assertIsNone(f(['--lang'], '--lang'))                                       # argparse reports a missing value
        self.assertIsNone(f(['--', '--lang', 'ko'], '--lang'))                           # nothing after -- is read
        self.assertIsNone(f(['--language', 'ko', '--port', '1'], '--lang'))
        self.assertEqual(f(['--help', '--lang', 'ko'], '--lang'), 'ko')                  # a --lang after --help is read too

    def test_help_with_lang(self):
        for argv, env, want in [(['--help', '--lang', 'ko'], {'LANG': 'C'}, 'ko'), (['--lang', 'ko', '--help'], {'LANG': 'C'}, 'ko'), (['--lang=en', '--help'], {'LANG': 'ko_KR.UTF-8'}, 'en'),
                                (['--help'], {'LANG': 'ko_KR.UTF-8'}, 'ko'), (['--help'], {'LANG': 'C'}, 'en'), (['--help'], {'AGENT_BULLPEN_LANG': 'ko', 'LANG': 'C'}, 'ko')]:
            with self.subTest(argv=argv, env=env), mock.patch.dict(os.environ, env, clear=True):
                out = io.StringIO()
                with mock.patch('sys.argv', ['server.py'] + argv), contextlib.redirect_stdout(out), terminal_lang(None):
                    with self.assertRaises(SystemExit) as cm:
                        server.main()
                    lang = server.CLI_LANG
                self.assertEqual((cm.exception.code, lang), (0, want))
                text = ' '.join(out.getvalue().split())
                self.assertIn('--lang CODE', text)
                self.assertIn({'ko': '지원: en, ko', 'en': 'Supported: en, ko'}[want], text)           # the help is in the terminal language
                self.assertIn('--session SESSION', text)
                self.assertNotIn({'ko': 'Supported:', 'en': '지원:'}[want], text)

    def test_lang_option_selects_the_terminal_language(self):
        h = Home(self)
        for argv, env, want in [(['--lang', 'ko'], {'LANG': 'C'}, 'ko'), (['--lang', 'en'], {'LANG': 'ko_KR.UTF-8'}, 'en'), ([], {'LANG': 'ko_KR.UTF-8'}, 'ko'), ([], {'LANG': 'C'}, 'en'),
                                (['--lang', 'auto'], {'LC_ALL': 'ko'}, 'ko'), (['--lang', 'xx'], {'LANG': 'ko'}, 'en'), (['--lang=KO'], {}, 'ko')]:
            with self.subTest(argv=argv, env=env), mock.patch.dict(os.environ, env, clear=True), main_env(self, h):
                out, err, code, lang = self.run_main_keeping_globals(['--port', '0'] + argv, servers=[FakeServer()])
                self.assertEqual(lang, want)
                self.assertNotIn('warning', err)

    def test_cli_t_follows_the_language(self):
        h = Home(self)
        with mock.patch.dict(os.environ, {'LANG': 'C'}, clear=True), main_env(self, h):
            with terminal_lang(None):
                run_main(['--port', '0', '--lang', 'ko'], servers=[FakeServer()], lang=None, keep=True)
                self.assertEqual(server.cli_t('status.running'), '작업 중')
                self.assertEqual(i18n.cli_t('status.running'), '작업 중')                  # the modules below the server see the same language
                run_main(['--port', '0', '--lang', 'en'], servers=[FakeServer()], lang=None, keep=True)
                self.assertEqual(server.cli_t('status.running'), 'Working')
                self.assertEqual(i18n.cli_t('status.running'), 'Working')

    def test_a_broken_dictionary_is_reported_once_at_start(self):
        h = Home(self)
        loc = LocalesDir(self)
        loc.put('en.json', pack('en', 'English', {'cli.warn.dictionary': 'warning: static/locales/{name} was not registered: {why}'}))
        loc.put('fr.json', '{broken')
        with mock.patch.dict(os.environ, {}, clear=True), main_env(self, h):
            out, err, code, lang = self.run_main_keeping_globals(['--port', '0'], servers=[FakeServer()])
        self.assertEqual(err.count('warning: static/locales/fr.json was not registered'), 1, err)

    def test_the_real_help_end_to_end(self):
        """A separate process: --help ends (exit 0) under LANG=C and ko_KR, and --lang ko too, in the language the terminal language order gives."""
        for argv, env in [(['--help'], {'LANG': 'C'}), (['--help', '--lang', 'ko'], {'LANG': 'C'}), (['--lang', 'en', '--help'], {'LANG': 'ko_KR.UTF-8'}),
                          (['--help'], {'LANG': 'ko_KR.UTF-8', 'AGENT_BULLPEN_LANG': 'en'}), (['--help', '--lang', 'xx'], {})]:
            with self.subTest(argv=argv, env=env):
                e = {k: v for k, v in os.environ.items() if k not in ('LANG', 'LC_ALL', 'LC_MESSAGES', 'AGENT_BULLPEN_LANG', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
                e.update(env, PYTHONDONTWRITEBYTECODE='1', HOME=tempfile.gettempdir())
                r = subprocess.run([sys.executable, server.__file__] + argv, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60, stdin=subprocess.DEVNULL)
                self.assertEqual((r.returncode, r.stderr), (0, ''))
                self.assertIn('--lang CODE', r.stdout)

    def test_missing_option_value_is_the_argparse_error(self):
        e = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HOME=tempfile.gettempdir())
        r = subprocess.run([sys.executable, server.__file__, '--lang'], env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2)
        self.assertIn('--lang: expected one argument', r.stderr)                          # argparse's own sentences stay English


def load_checker():
    import importlib.util
    spec = importlib.util.spec_from_file_location('i18n_check', os.path.join(ROOT, 'tools', 'regress', 'i18n_check.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DictionaryChecker(unittest.TestCase):
    """tools/regress/i18n_check.py: it passes on the shipped files and reports each kind of mistake."""

    def setUp(self):
        self.chk = load_checker()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.static = os.path.join(self.tmp.name, 'static')
        self.copy_p0_dictionaries()
        self.write_js('x.js', "const a = t('status.running'); // Hangul in a comment is fine: 주석\n")

    def copy_p0_dictionaries(self):
        """The shipped dictionaries cut down to the common sections and every section anchor, so these tests do not depend on what the page sections hold."""
        shutil.rmtree(os.path.join(self.static, 'locales'), ignore_errors=True)
        os.makedirs(os.path.join(self.static, 'locales'))
        for code in ('en', 'ko'):
            with open(os.path.join(SHIPPED, code + '.json'), encoding='utf-8') as f:
                d = json.load(f)
            d['messages'] = {k: v for k, v in d['messages'].items() if k.split('.')[0] in ('common', 'status', 'kind', 'time', 'unit') or k.endswith('._')}
            d['limits'] = {k: v for k, v in d['limits'].items() if k in d['messages']}
            with open(os.path.join(self.static, 'locales', code + '.json'), 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=1)

    def write_js(self, name, text):
        write(os.path.join(self.static, name), text)

    def edit(self, code, fn):
        path = os.path.join(self.static, 'locales', code + '.json')
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        fn(d)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

    def findings(self):
        out = []
        packs = self.chk.check_dictionaries(self.static, out)
        self.chk.check_static(self.static, packs, out)
        return out

    def groups(self):
        return sorted({g for g, _, _ in self.findings()})

    def test_clean_on_the_shipped_files(self):
        self.assertEqual(self.findings(), [])                                       # the copy made in setUp (the common sections only, no pages)
        import re
        out = []
        static = os.path.join(ROOT, 'static')
        self.chk.check_static(static, self.chk.check_dictionaries(static, out), out)
        # The pages' hard-coded Korean is not checked here: only the syntax and the common sections must be clean.
        mine = [x for x in out if x[0] == 'syntax' or (x[0] not in ('static-hangul', 'limits-note') and re.search(r'"(common|status|kind|time|unit)\.', x[2]))]
        self.assertEqual(mine, [])

    def test_each_mistake_is_reported(self):
        def drop_ko_key(d):
            del d['messages']['time.now']
        def en_hangul(d):
            d['messages']['common.close'] = 'Close 닫기'
        def en_term(d):
            d['messages']['common.close'] = 'Close the drawer'
        def en_board(d):
            d['messages']['common.close'] = 'Show the board'
        def ko_param(d):
            d['messages']['time.ago.min'] = '{count}분 전'
        def no_other(d):
            d['messages']['unit.agent'] = {'one': '{count} agent'}
        def too_long(d):
            d['messages']['common.you'] = 'Somebody else'
        def moved_key(d):
            d['messages'] = {k: v for k, v in d['messages'].items() if k != 'status.done'}
            d['messages']['common.extra.late'] = 'x'                                   # a common key after the last section
        def bad_zone_order(d):
            m = d['messages']
            d['messages'] = {k: m[k] for k in sorted(m, key=lambda k: (k.split('.')[0] != 'unit', ))}
        for code, fn, group in [('ko', drop_ko_key, 'keys'), ('en', en_hangul, 'hangul'), ('en', en_term, 'terms'), ('en', en_board, 'terms'), ('ko', ko_param, 'params'),
                                ('en', no_other, 'plural'), ('en', too_long, 'limits'), ('en', moved_key, 'zones'), ('en', bad_zone_order, 'zones')]:
            with self.subTest(fn=fn.__name__):
                self.copy_p0_dictionaries()
                self.edit(code, fn)
                self.assertIn(group, self.groups())

    def test_whiteboard_and_dashboard_are_not_the_bare_word_board(self):
        self.edit('en', lambda d: d['messages'].update({'common.close': 'Whiteboard on the dashboard'}))
        self.assertNotIn('terms', self.groups())

    def test_other_languages_only_note_the_limit(self):
        self.edit('ko', lambda d: d['messages'].update({'common.you': '아주아주아주아주아주 긴 이름'}))
        self.assertEqual(self.groups(), ['limits-note'])

    def test_json_syntax_and_duplicate_keys(self):
        path = os.path.join(self.static, 'locales', 'ko.json')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        write(path, text.replace('"common.you": "사용자",', '"common.you": "사용자",\n    "common.you": "또",'))
        self.assertIn(('syntax', 'ko.json', 'duplicate key "common.you"'), self.findings())
        write(path, '{broken')
        self.assertIn('syntax', self.groups())

    def test_references(self):
        self.write_js('x.js', "t('status.running'); t('no.such.key'); I18N.t('nor.this'); x.t('a.b'); t('status.' + a.status); t('nope.' + a.k); // t('commented.out')\n")
        refs = [w + ' ' + m for g, w, m in self.findings() if g == 'refs']
        self.assertEqual(refs, ['x.js:1 unknown key "no.such.key"', 'x.js:1 unknown key "nor.this"', 'x.js:1 no key starts with "nope."'])
        self.write_js('p.html', '<b data-i18n="common.close"></b><b data-i18n-title="common.nope"></b>\n')
        self.assertIn('p.html:1 unknown key "common.nope"', [w + ' ' + m for g, w, m in self.findings() if g == 'refs'])

    def test_hangul_outside_comments_in_js_html_css(self):
        self.write_js('a.js', "// 주석\n/* 블록\n주석 */\nconst a = '한글';\nconst b = `x ${ '값' } y`;\nconst c = /조언$/;   // i18n-ok\nconst d = 'http://x/' + 1; // 끝\nconst e = 5 / 2; // 나누기\nconst f = `${ a /* 주석 */ }`;\n")
        self.write_js('b.html', '<!-- 주석 --><p title="제목">본문</p><script>const x = 1; // 주석\nconst y = "문자열";</script><style>/* 주석 */ .a:after { content: "내용"; }</style>\n')
        self.write_js('c.css', '/* 주석 */\n.a { content: "내용"; } /* 주석 */\n')
        rows = {}
        for g, w, m in self.findings():
            if g == 'static-hangul':
                rows.setdefault(w.split(':')[0], []).append(int(w.split(':')[1]))
        self.assertEqual(rows, {'a.js': [4, 5], 'b.html': [1, 2], 'c.css': [2]})


class BrowserSide(unittest.TestCase):
    """static/i18n.js: the language order (address / stored / browser / unsupported / storage exception), lookup, plurals, dates, links, the selector. Skipped when node is missing."""

    def test_i18n_checks_js(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        r = subprocess.run([node, os.path.join(ROOT, 'tools', 'regress', 'i18n_checks.js'), os.path.join(ROOT, 'static')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        fails = [line for line in r.stdout.splitlines() if line.startswith(('FAIL', 'HARNESS'))]
        self.assertEqual((r.returncode, fails), (0, []), r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn('ALL PASS', r.stdout)


if __name__ == '__main__':
    unittest.main()
