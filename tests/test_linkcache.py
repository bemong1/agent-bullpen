"""Tests for the link record (a file), link certainty (link {rule, certain}) and missed candidates (unlinked).

The link record keeps only links found by a certain rule (lineage, environment variable) in `$XDG_CACHE_HOME/agent-bullpen/links.json` (just session id, rule and times), and the server reads it at start.
The tests use only temporary folders: the real cache is not touched (the Lineage cache is turned on by server.main(); in tests and the harness it is None).

    python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import urllib.request
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
from test_stage2 import T0, bash_line, child_lines, dump, iso  # noqa: E402
from test_proclink import C, D, P, Q, TID, TID2, UID, Fixture  # noqa: E402,F401
from test_detached import SECRETS, set_env  # noqa: E402
from test_release import live_server  # noqa: E402

from board import lineage, link, procs, views  # noqa: E402

NOW = time.time()
MAX_PAIRS, MAX_DAYS, MAX_UNLINKED = 2000, 90, 20          # the approved values as they are (if a constant in the code changes, this test tells)


def row(child=C, parent=P, kind='cli', rule='env', seen=None, started=T0, **kw):
    r = {'child': child, 'parent': parent, 'kind': kind, 'rule': rule, 'seen': NOW - 100 if seen is None else seen, 'started': started}
    r.update(kw)
    return r


def put_cache(path, rows, version=1, mode=0o600, dir_mode=0o700):
    os.makedirs(os.path.dirname(path), mode=dir_mode, exist_ok=True)
    os.chmod(os.path.dirname(path), dir_mode)
    with open(path, 'w') as f:
        json.dump({'version': version, 'links': rows}, f)
    os.chmod(path, mode)



def old_link(link):
    """The two keys `link` always had (rule, certain): the API only adds keys (rule_class, ...) and never changes these."""
    return {k: link[k] for k in ('rule', 'certain')}


class CacheFile(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.cache = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')

    def restart(self):
        """As if the server were restarted: a new LinkIndex reads the same file (the processes and session files are already gone)."""
        links = server.LinkIndex()
        links.lineage.enable_cache(self.cache)
        return links

    def live_tree(self):
        """One detached child that links through the environment and one Codex exec."""
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, C)
        set_env(self.proc, 103, P, 100)
        self.proc.add(104, 1, ['codex', 'exec', 'x'], fds=[self.rollout()])
        set_env(self.proc, 104, P, 100)
        self.parent()
        self.child()

    def end_all(self):
        for pid in (100, 103, 104):
            self.proc.remove(pid)
        for f in os.listdir(self.sess_dir):
            os.unlink(os.path.join(self.sess_dir, f))

    # ---- reading ----
    def test_defaults_do_not_touch_any_file(self):
        self.assertIsNone(lineage.Lineage().cache)
        self.assertIsNone(server.LinkIndex().lineage.cache)
        self.assertIsNone(server.LINKS.lineage.cache)                           # the test process does not call main()
        self.live_tree()
        self.scan()
        self.assertFalse(os.path.exists(os.path.join(self.t, 'xdg')))

    def test_loaded_entries_are_certain_and_marked_file(self):
        put_cache(self.cache, [row(), row(TID, P, 'codex', 'proc', started=None)])
        links = self.restart()
        self.parent()
        self.child()
        self.rollout()
        self.index.refresh(force=True)
        links.scan()
        o = links.cli_owners[C]
        self.assertEqual((o['sid'], o['rule'], o['call']), (P, 'file', None))
        self.assertEqual(o['bash_ts'], T0)                                       # the stored start time
        t = links.owners[TID]
        self.assertEqual((t['sid'], t['rule']), (P, 'file'))
        self.assertTrue(link.certain(o['rule']) and link.certain(t['rule']))

    def test_an_older_files_content_links_do_not_come_back_and_the_file_is_written_again_as_the_current_version(self):
        """A `content` link of a version 2 file may rest on words that only travelled as text (a relay that typed an instruction into a terminal); it is read again
        from the records, which no longer count such words. The other rules stay, and the file is rewritten once, as version 3, without the dropped link."""
        E = '55555555-5555-4555-8555-555555555555'
        put_cache(self.cache, [row(C, P, rule='content', node='a' + '0' * 16), row(D, P, rule='out'), row(E, P, rule='env')], version=2)
        links = server.LinkIndex()
        links.lineage.enable_cache(self.cache)
        self.assertEqual(sorted(links.lineage.saved), [D])                                  # the `out` link is kept, as a record of the judgment (never evidence)
        self.assertEqual(sorted(links.lineage.cli), [E])
        self.assertNotIn(C, links.lineage.saved)
        self.assertNotIn(C, links.lineage.cli)
        links.lineage.scan({P, C, D, E}, self.index.get)                                   # the first scan writes it again
        with open(self.cache) as f:
            d = json.load(f)
        self.assertEqual(d['version'], 5)
        self.assertEqual(sorted(r['child'] for r in d['links']), sorted([D, E]))
        again = server.LinkIndex()
        again.lineage.enable_cache(self.cache)
        self.assertEqual(sorted(again.lineage.saved) + sorted(again.lineage.cli), sorted([D, E]))
        self.assertEqual(again.lineage._saved, again.lineage._rows_text())                  # a current file is not written again at the next scan

    def test_a_content_link_of_a_current_file_is_read(self):
        put_cache(self.cache, [row(C, P, rule='content')], version=lineage.CACHE_VERSION)
        links = server.LinkIndex()
        links.lineage.enable_cache(self.cache)
        self.assertEqual(links.lineage.saved[C]['orig'], 'content')
        put_cache(self.cache, [row(C, P, rule='content'), row(D, P, rule='out')], version=3)         # a version 3 file: its `content` links may rest on a plain argument or a script that only says the words
        links = server.LinkIndex()
        links.lineage.enable_cache(self.cache)
        self.assertEqual(sorted(links.lineage.saved), [D])

    def test_broken_and_untrusted_files_are_ignored_silently(self):
        for text in ('{not json', '[]', '"x"', '', '{"version": 2, "links": []}', '{"version": 1, "links": "x"}', '{"version": 1}'):
            os.makedirs(os.path.dirname(self.cache), exist_ok=True)
            with open(self.cache, 'w') as f:
                f.write(text)
            os.chmod(self.cache, 0o600)
            self.assertEqual(lineage.read_cache(self.cache), [], text)
            self.assertEqual(self.restart().lineage.cli, {})
        self.assertEqual(lineage.read_cache(os.path.join(self.t, 'nope', 'links.json')), [])
        put_cache(self.cache, [row()])
        self.assertEqual(len(lineage.read_cache(self.cache)), 1)                 # control
        os.chmod(os.path.dirname(self.cache), 0o777)                             # a file in a folder other users can write to is not trusted
        self.assertEqual(lineage.read_cache(self.cache), [])
        put_cache(self.cache, [row()], mode=0o666)
        self.assertEqual(lineage.read_cache(self.cache), [])
        put_cache(self.cache, [row()])
        real = self.cache + '.real'
        os.rename(self.cache, real)
        os.symlink(real, self.cache)                                             # a symlink is not followed
        self.assertEqual(lineage.read_cache(self.cache), [])
        os.unlink(self.cache)
        os.rename(real, self.cache)
        with open(self.cache, 'w') as f:
            f.write('{"version":1,"links":[' + ('{"child":"x"},' * 200000) + '{}]}')   # a file over the size limit
        os.chmod(self.cache, 0o600)
        self.assertEqual(lineage.read_cache(self.cache), [])

    def test_bad_rows_are_dropped_one_by_one(self):
        def cid(i):
            return '7%07d-0000-4000-8000-000000000000' % i
        good = row(cid(0))
        bad = [row(cid(1), parent='x'), row(cid(2), parent='../../etc'), row(P, P), row(child='x'), row(cid(3), kind='exec'), row(cid(4), rule='time'),
               row(cid(5), rule='file'), row(cid(6), seen='yesterday'), row(cid(7), seen=True), row(cid(8), seen=float('nan')),
               row(cid(9), seen=NOW - 91 * 86400), row(cid(10), seen=NOW + 10 * 86400), 'junk', None, 5, {'child': cid(11)}, row(cid(12), kind=None)]
        odd = [row(cid(13), started='x'), row(cid(0), parent=Q)]                        # a bad started leaves only the time missing; a second row for the same child is dropped
        put_cache(self.cache, [good] + bad + odd)
        got = lineage.read_cache(self.cache)
        self.assertEqual([(r['child'], r['parent'], r['started']) for r in got], [(cid(0), P, T0), (cid(13), P, None)])
        self.assertTrue(all(set(r) == {'child', 'parent', 'kind', 'rule', 'seen', 'started'} for r in got))

    def test_limits_are_the_approved_ones(self):
        self.assertEqual((lineage.CACHE_MAX, lineage.CACHE_DAYS, link.UNLINKED_MAX), (MAX_PAIRS, MAX_DAYS, MAX_UNLINKED))
        self.assertEqual(lineage.CACHE_RULES, ('proc', 'env', 'out', 'content'))       # version 2 adds the links that rest on an output file or a long instruction
        self.assertEqual((lineage.CACHE_VERSION, lineage.CONTENT_FROM), (5, 5))        # version 5: the `content` links of an older file are not read

    def test_size_and_age_limits_on_read(self):
        many = [row(child='%08d-0000-4000-8000-000000000000' % i, seen=NOW - 1000 - i) for i in range(MAX_PAIRS + 50)]
        put_cache(self.cache, many)
        got = lineage.read_cache(self.cache)
        self.assertEqual(len(got), MAX_PAIRS)
        self.assertEqual(got[0]['child'], '%08d-0000-4000-8000-000000000000' % 0)    # the entry first seen latest comes first
        put_cache(self.cache, [row(seen=NOW - MAX_DAYS * 86400 - 5), row(child=D, seen=NOW - MAX_DAYS * 86400 + 60)])
        self.assertEqual([r['child'] for r in lineage.read_cache(self.cache, now=NOW)], [D])        # NOW is when this module was imported; a slow run would age the 60 s margin out

    # ---- writing ----
    def test_written_when_a_certain_link_is_found(self):
        self.live_tree()
        self.scan()
        self.assertFalse(os.path.exists(self.cache))                              # the cache is not enabled
        self.links.lineage.enable_cache(self.cache)
        self.assertFalse(os.path.exists(self.cache))                              # nothing to read and nothing changed
        self.scan()
        self.assertEqual(self.links.cli_owners[C]['rule'], 'env')
        self.assertTrue(os.path.isfile(self.cache))
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.cache)).st_mode), 0o700)
        self.assertEqual(os.listdir(os.path.dirname(self.cache)), ['links.json'])  # no temporary file is left behind
        with open(self.cache) as f:
            d = json.load(f)
        self.assertEqual(d['version'], 5)
        by = {(r['kind'], r['child']): r for r in d['links']}
        self.assertEqual(set(by), {('cli', C), ('codex', TID)})
        self.assertEqual((by[('cli', C)]['parent'], by[('cli', C)]['rule'], by[('cli', C)]['started']), (P, 'env', T0 + 1))
        self.assertIsNone(by[('codex', TID)]['started'])
        for r in d['links']:
            self.assertEqual(set(r), {'child', 'parent', 'kind', 'rule', 'seen', 'started'})
            self.assertAlmostEqual(r['seen'], time.time(), delta=60)

    def test_only_ids_rules_and_times_are_written(self):
        """No paths, prompts or environment variable values are in the file."""
        self.live_tree()
        self.links.lineage.enable_cache(self.cache)
        self.write(C, [dump({'type': 'user', 'timestamp': iso(T0 + 2), 'cwd': '/w/private-project-dir', 'message': {'role': 'user', 'content': 'SECRET-PROMPT-TEXT'}})])
        self.scan()
        with open(self.cache) as f:
            text = f.read()
        for needle in SECRETS + ('/w', 'private-project-dir', 'SECRET-PROMPT-TEXT', self.t, 'claude', 'codex exec', 'HOME', 'api', 'token'):
            self.assertNotIn(needle, text, needle)
        strings = re.findall(r'"([^"]*)"', text)
        for sv in strings:
            self.assertTrue(re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|version|links|child|parent|kind|rule|seen|started|cli|codex|proc|env', sv), sv)

    def test_estimated_rules_are_not_written(self):
        """Links made by command parsing plus time (the "time" rule) and details filled in from the call are not stored."""
        self.links.lineage.enable_cache(self.cache)
        self.parent(P, [bash_line(T0, 'claude -p "$Q"', '/w', tid='toolu_p')])      # no literal instruction: only the command, the time and the folder
        self.child()
        self.assertEqual(self.scan().cli_owners[C]['rule'], 'time')
        self.assertFalse(os.path.exists(self.cache))

    def test_survives_a_restart_and_is_not_forgotten_when_processes_end(self):
        self.links.lineage.enable_cache(self.cache)
        self.live_tree()
        self.scan()
        self.end_all()
        second = self.restart()
        procs.reset()
        self.index.refresh(force=True)
        second.scan()
        o = second.cli_owners[C]
        self.assertEqual((o['sid'], o['rule']), (P, 'file'))
        self.assertEqual((second.owners[TID]['sid'], second.owners[TID]['rule']), (P, 'file'))
        self.assertEqual(second.cli_owned_by(P), {C: o})
        second.scan()                                                              # the same after scanning again
        self.assertEqual(second.cli_owners[C]['rule'], 'file')

    def test_a_restart_that_sees_the_process_again_upgrades_the_rule_but_keeps_first_seen(self):
        self.links.lineage.enable_cache(self.cache)
        self.live_tree()
        self.scan()
        with open(self.cache) as f:
            first = {(r['kind'], r['child']): r for r in json.load(f)['links']}
        time.sleep(0.02)
        second = self.restart()
        procs.reset()
        self.index.refresh(force=True)
        second.scan()
        self.assertEqual((second.cli_owners[C]['rule'], second.owners[TID]['rule']), ('env', 'env'))      # this time it was seen directly
        with open(self.cache) as f:
            again = {(r['kind'], r['child']): r for r in json.load(f)['links']}
        self.assertEqual({k: v['seen'] for k, v in again.items()}, {k: v['seen'] for k, v in first.items()})
        self.assertEqual({k: v['rule'] for k, v in again.items()}, {k: v['rule'] for k, v in first.items()})

    def test_entries_of_vanished_sessions_are_dropped_at_start(self):
        put_cache(self.cache, [row(), row(D, Q), row(TID, P, 'codex', 'proc', started=None), row(TID2, P, 'codex', 'env', started=None)])
        self.parent()
        self.child()                                                             # only C and P have transcripts (not Q or D)
        self.rollout()
        second = self.restart()
        self.assertEqual(set(second.lineage.cli), {C, D})                        # at read time, all of them
        self.index.refresh(force=True)
        second.scan()
        self.assertEqual(set(second.lineage.cli), {C})                           # the first scan checks them against the transcripts
        self.assertEqual(set(second.lineage.cx), {TID})                          # TID2 has no rollout
        with open(self.cache) as f:
            rows = json.load(f)['links']
        self.assertEqual({(r['kind'], r['child']) for r in rows}, {('cli', C), ('codex', TID)})

    def test_nothing_is_dropped_while_no_record_folder_is_visible(self):
        put_cache(self.cache, [row()])
        second = self.restart()
        shutil.rmtree(self.proj)
        os.makedirs(self.proj)                                                    # no transcripts at all (the folder is not visible yet)
        second.scan()
        self.assertIn(C, second.lineage.cli)
        with open(self.cache) as f:
            self.assertEqual(len(json.load(f)['links']), 1)

    def test_size_and_age_limits_on_write(self):
        lin = lineage.Lineage(self.cache)
        for i in range(MAX_PAIRS + 30):
            lin.cli['%08d-0000-4000-8000-000000000000' % i] = {'sid': P, 'ts': None, 'cwd': None, 'rule': 'env', 'orig': 'env', 'seen': NOW - i}
        lin.cli[D] = {'sid': P, 'ts': None, 'cwd': None, 'rule': 'env', 'orig': 'env', 'seen': NOW - MAX_DAYS * 86400 - 1}
        lin._save()
        with open(self.cache) as f:
            rows = json.load(f)['links']
        self.assertEqual(len(rows), MAX_PAIRS)
        self.assertNotIn(D, {r['child'] for r in rows})
        self.assertEqual(len(lin.cli), MAX_PAIRS)                          # memory holds the same set too

    def test_write_failure_is_quiet_and_retried_later(self):
        lin = lineage.Lineage(self.cache)
        lin.cli[C] = {'sid': P, 'ts': None, 'cwd': None, 'rule': 'env', 'orig': 'env', 'seen': NOW}
        with mock.patch.object(lineage, 'write_cache', side_effect=PermissionError('secret path /x')):
            lin._save()
        self.assertEqual(lin.write_error, 'PermissionError')                       # only the exception name (the content and path are not kept)
        self.assertFalse(os.path.exists(self.cache))
        lin._save()                                                                # not written again before the retry time
        self.assertFalse(os.path.exists(self.cache))
        lin._retry = 0
        lin._save()
        self.assertTrue(os.path.exists(self.cache))
        self.assertIsNone(lin.write_error)

    def test_unwritable_folder_does_not_raise(self):
        os.makedirs(os.path.dirname(self.cache))
        os.chmod(os.path.dirname(self.cache), 0o500)
        self.addCleanup(os.chmod, os.path.dirname(self.cache), 0o700)
        self.links.lineage.enable_cache(self.cache)
        self.live_tree()
        self.scan()                                                                # linking keeps working even when writing fails
        self.assertIn(C, self.links.cli_owners)

    def test_default_path_follows_xdg_cache_home(self):
        code = 'import sys; sys.path.insert(0, %r); import board.lineage as l; print(l.LINK_CACHE)' % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = {k: v for k, v in os.environ.items() if k not in ('XDG_CACHE_HOME', 'HOME')}
        out = subprocess.check_output([sys.executable, '-c', code], env=dict(base, HOME='/h', PYTHONDONTWRITEBYTECODE='1')).decode().strip()
        self.assertEqual(out, '/h/.cache/agent-bullpen/links.json')
        out = subprocess.check_output([sys.executable, '-c', code], env=dict(base, HOME='/h', XDG_CACHE_HOME='/x/y', PYTHONDONTWRITEBYTECODE='1')).decode().strip()
        self.assertEqual(out, '/x/y/agent-bullpen/links.json')


class CacheEndToEnd(unittest.TestCase):
    """A separate server process: on by default and read at start; with --no-link-cache it neither reads nor writes. HOME is a temporary folder."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(os.path.realpath(self.tmp.name), 'home')
        proj = os.path.join(self.home, '.claude', 'projects', '-w')
        os.makedirs(proj)
        for sid, t in ((P, T0 - 100), (C, T0 + 2)):
            with open(os.path.join(proj, sid + '.jsonl'), 'w') as f:
                f.write('\n'.join(child_lines(t, '/w')) + '\n')
        self.cache = os.path.join(self.home, '.cache', 'agent-bullpen', 'links.json')
        put_cache(self.cache, [row(seen=time.time() - 5)])

    def get(self, run, path):
        with urllib.request.urlopen('http://127.0.0.1:%d%s' % (run.port(), path), timeout=20) as r:
            return json.load(r)

    def test_default_reads_the_file_and_no_link_cache_ignores_it(self):
        with live_server(['--port', '0'], self.home) as run:
            st = self.get(run, '/api/state?session=' + P)
            sessions = self.get(run, '/api/sessions')
        child = [a for a in st['agents'] if a['id'] == C]
        self.assertEqual(len(child), 1)
        self.assertEqual(old_link(child[0]['link']), {'rule': 'file', 'certain': True})
        self.assertEqual(child[0]['origin'], 'cli')
        self.assertNotIn(C, [s['id'] for s in sessions['sessions']])              # shown only as a child agent
        self.assertEqual(run.err_text, '')
        with open(self.cache) as f:                                                 # it only read (nothing changed) and did not write again
            self.assertEqual([r['child'] for r in json.load(f)['links']], [C])
        with live_server(['--port', '0', '--no-link-cache'], self.home) as run:
            st = self.get(run, '/api/state?session=' + P)
            sessions = self.get(run, '/api/sessions')
        self.assertEqual([a for a in st['agents'] if a['id'] == C], [])
        self.assertIn(C, [s['id'] for s in sessions['sessions']])
        self.assertEqual(run.err_text, '')

    def test_no_link_cache_never_creates_the_folder(self):
        os.unlink(self.cache)
        os.rmdir(os.path.dirname(self.cache))
        with live_server(['--port', '0', '--no-link-cache'], self.home):
            pass
        self.assertFalse(os.path.exists(os.path.dirname(self.cache)))
        with live_server(['--port', '0'], self.home):                              # with no certain link there is nothing to create
            pass
        self.assertFalse(os.path.exists(self.cache))

    def test_help_lists_the_option(self):
        out = subprocess.run([sys.executable, server.__file__, '--help'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=dict(os.environ, AGENT_BULLPEN_LANG='en', PYTHONDONTWRITEBYTECODE='1'), timeout=60).stdout.decode()
        self.assertIn('--no-link-cache', out)


# =====================================================================================================================
class Brief(unittest.TestCase):
    def test_rules(self):
        ns = types.SimpleNamespace
        for rule, want in (('proc', True), ('env', True), ('file', True), ('prompt', True), ('id', True), ('time', False), ('session', False), (None, False), ('weird', False)):
            self.assertEqual(link.link_brief(ns(provider='codex', origin='exec', link={'rule': rule}, cli=None)), {'rule': rule, 'certain': want}, rule)
        for rule, want in (('proc', True), ('env', True), ('file', True), ('time', False)):
            self.assertEqual(link.link_brief(ns(provider='claude', origin='cli', link=None, cli={'rule': rule})), {'rule': rule, 'certain': want}, rule)
        self.assertEqual(link.link_brief(ns(provider='claude', origin='cli', link=None, cli={})), {'rule': 'time', 'certain': False})   # an old entry without a rule is an estimate
        self.assertEqual(link.link_brief(ns(provider='claude', origin='subagent', link=None, cli=None)), {'rule': 'subagent', 'certain': True})
        self.assertEqual(link.link_brief(ns(provider='codex', origin='exec', link=None, cli=None)), {'rule': None, 'certain': False})


class Api(Fixture):
    def state(self, sid=P):
        s = server.Session(os.path.join(self.proj, sid + '.jsonl'))
        s.poll()
        return s, views.state(s)

    def test_agents_carry_rule_and_certainty_and_old_fields_stay(self):
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, C)
        set_env(self.proc, 103, P, 100)                                             # environment: certain
        self.write(D, child_lines(T0 + 5, '/w'))
        self.parent(P, [bash_line(T0 + 4, 'claude -p "$Q"', '/w', tid='toolu_t')])   # time rule: estimated (no literal instruction to compare)
        self.proc.add(104, 1, ['codex', 'exec', 'x'], fds=[self.rollout()])
        set_env(self.proc, 104, P, 100)                                             # Codex environment: certain
        self.child()
        self.scan()
        s, st = self.state()
        by = {a['id']: a for a in st['agents']}
        self.assertEqual(old_link(by[C]['link']), {'rule': 'env', 'certain': True})
        self.assertEqual(old_link(by[D]['link']), {'rule': 'time', 'certain': False})
        self.assertEqual(old_link(by[TID]['link']), {'rule': 'env', 'certain': True})
        for a in st['agents']:
            self.assertLessEqual({'rule', 'certain'}, set(a['link']))                      # the old keys stay; new ones are only added (rule_class, ...)
            self.assertEqual(a['link']['rule_class'], 'certain' if a['link']['certain'] else 'guess')
            for old in ('id', 'status', 'provider', 'origin', 'tokens', 'spawn_ts'):
                self.assertIn(old, a)
        self.assertEqual(s.agent_detail(TID)['link']['certain'], True)
        self.assertEqual(s.agent_detail(TID)['link']['rule'], 'env')
        for k in ('sid', 'call', 'bash_ts', 'bash_desc', 'dt', 'cwd', 'cwd_ok', 'prompt_ok'):
            self.assertIn(k, s.agent_detail(TID)['link'])                           # the old keys stay as they are

    def test_subagents_are_certain(self):
        d = os.path.join(self.proj, P, 'subagents')
        os.makedirs(d)
        self.parent()
        aid = 'a1' + '0' * 15
        with open(os.path.join(d, 'agent-%s.meta.json' % aid), 'w') as f:
            json.dump({'description': 'sub', 'agentType': 'general-purpose', 'toolUseId': 'toolu_1'}, f)
        with open(os.path.join(d, 'agent-%s.jsonl' % aid), 'w') as f:
            f.write(dump({'type': 'user', 'timestamp': iso(T0), 'message': {'role': 'user', 'content': 'go'}}) + '\n')
        self.scan()
        _, st = self.state()
        self.assertEqual([old_link(a['link']) for a in st['agents']], [{'rule': 'subagent', 'certain': True}])

    def test_file_rule_and_same_parent_call_keep_the_certain_rule(self):
        self.parent(P, [bash_line(T0, 'claude -p hi', '/w', tid='toolu_p', desc='launch')])      # the literal argument is the child's instruction
        self.child()
        links = server.LinkIndex()
        links.lineage.cli[C] = {'sid': P, 'ts': T0, 'cwd': None, 'rule': 'file', 'orig': 'env', 'seen': NOW}
        with mock.patch.object(lineage.Lineage, 'scan', lambda *a, **k: False):
            links.scan()
        o = links.cli_owners[C]
        self.assertEqual((o['rule'], o['call'], o['bash_desc']), ('file', 'toolu_p', 'launch'))     # even when filled in from the call info, the rule stays on the certain side

    def test_state_has_unlinked_list(self):
        self.parent()
        self.scan()
        _, st = self.state()
        self.assertEqual(st['unlinked'], [])


# =====================================================================================================================
def sdk(sid, t, cwd='/w', entry='sdk-cli'):
    return [dump({'type': 'user', 'timestamp': iso(t), 'cwd': cwd, 'entrypoint': entry, 'message': {'role': 'user', 'content': 'hi'}}),
            dump({'type': 'assistant', 'timestamp': iso(t + 4), 'cwd': cwd, 'entrypoint': entry,
                  'message': {'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}], 'usage': {}}})]


class Unlinked(Fixture):
    def kid(self, sid, t, cwd='/w', entry='sdk-cli'):
        self.write(sid, sdk(sid, t, cwd, entry))
        return sid

    def got(self, sid=P):
        self.scan()
        return self.links.unlinked_for(sid)

    def ids(self, sid=P):
        return {x['id']: x['reason'] for x in self.got(sid)}

    def test_ambiguous_when_a_matching_call_could_not_take_it(self):
        self.parent(P, [bash_line(T0, 'cd /w && claude -p hi', '/w', tid='toolu_p')])
        self.kid(C, T0 + 2)
        self.kid(D, T0 + 3)                                                           # one call, two sessions in the same folder at the same time
        got = self.got()
        self.assertEqual(len(got), 1)
        x = got[0]
        self.assertEqual(set(x), {'id', 'provider', 'started', 'cwd', 'reason'})
        self.assertEqual((x['id'], x['provider'], x['reason'], x['started']), (D, 'claude', 'ambiguous', T0 + 3))
        self.assertEqual(self.links.cli_owners[C]['sid'], P)                          # the one that came first got linked

    def test_cwd_is_shortened_like_everywhere_else(self):
        self.parent(P, [bash_line(T0, 'claude -p hi', self.home + '/proj', tid='toolu_p')])
        self.kid(C, T0 + 2, self.home + '/proj')
        self.kid(D, T0 + 3, self.home + '/proj')
        self.assertEqual(self.got()[0]['cwd'], '~/proj')

    def test_no_matching_call_for_a_live_process_and_ended_before_seen_otherwise(self):
        self.parent(P, [bash_line(T0, 'ssh remote ./start-agents.sh', '/w', tid='toolu_p')])      # a launch that cannot be read
        self.kid(C, T0 + 3)
        self.assertEqual(self.ids(), {C: 'ended_before_seen'})                          # the process was never seen
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, C)                                                      # alive, but with neither environment nor lineage
        self.assertEqual(self.ids(), {C: 'no_matching_call'})

    def test_codex_threads(self):
        self.parent(P, [bash_line(T0, 'cd /w && codex exec -m x go', '/w', tid='toolu_p')])
        self.parent(Q, [bash_line(T0 + 1, 'cd /w && codex exec -m y go', '/w', tid='toolu_q')])   # two sessions launched Codex at the same time: the session rule cannot decide either
        self.rollout(TID, t=T0 + 3)
        self.rollout(TID2, t=T0 + 4)
        for sid in (P, Q):
            got = self.got(sid)
            self.assertEqual({x['id']: (x['provider'], x['reason']) for x in got}, {TID: ('codex', 'ambiguous'), TID2: ('codex', 'ambiguous')})
        self.assertEqual(self.links.owners, {})

    def test_codex_without_launch_call_uses_liveness(self):
        self.parent(P, [bash_line(T0, 'make run-agents', '/w', tid='toolu_p')])
        path = self.rollout(TID, t=T0 + 3)
        self.assertEqual(self.ids(), {TID: 'ended_before_seen'})
        self.proc.add(104, 1, ['codex', 'exec', 'x'], fds=[path])                     # open, but could not be linked anywhere
        self.assertEqual(self.ids(), {TID: 'no_matching_call'})

    def test_not_candidates(self):
        self.parent(P, [bash_line(T0, 'ssh remote go', '/w', tid='toolu_p')])
        self.kid('50000001-1111-4111-8111-111111111111', T0 + 3, entry='cli')                        # an interactive session the user opened directly
        self.kid('50000002-1111-4111-8111-111111111111', T0 + 3, cwd='/elsewhere')                  # a different project
        self.kid('50000003-1111-4111-8111-111111111111', T0 + 3 + link.CLI_WINDOW + 5)              # outside the window
        self.kid('50000004-1111-4111-8111-111111111111', T0 - 10)                                    # before the call
        self.kid('50000005-1111-4111-8111-111111111111', T0 + 3, cwd='/w/sub')                      # inside the same project (a candidate)
        self.rollout(TID, origin='codex-tui', t=T0 + 3)                                              # interactive Codex
        self.rollout(TID2, parent='x-parent', t=T0 + 3)                                              # guardian
        self.assertEqual(set(self.ids()), {'50000005-1111-4111-8111-111111111111'})

    def test_linked_ones_and_the_parent_itself_are_not_listed(self):
        self.parent(P, [bash_line(T0, 'claude -p hi', '/w', tid='toolu_p')])
        self.kid(C, T0 + 2)
        self.assertEqual(self.got(), [])                                              # it was linked
        self.assertEqual(self.links.unlinked_for(C), [])
        self.assertEqual(self.links.unlinked_for('no-such-session'), [])

    def test_launch_in_another_folder_belongs_to_someone_else(self):
        self.parent(P, [bash_line(T0, 'cd /other && claude -p one', '/w', tid='toolu_p')])        # P launched it in /other
        self.kid(C, T0 + 2, cwd='/w')                                                  # this session started in /w
        self.assertEqual(self.got(), [])

    def test_old_candidates_are_not_searched(self):
        old = time.time() - (link.UNLINKED_DAYS + 2) * 86400
        self.parent(P, [bash_line(old, 'ssh remote go', '/w', tid='toolu_p')])
        self.kid(C, old + 3)
        self.assertEqual(self.got(), [])
        new = time.time() - 3600
        self.parent(P, [bash_line(new, 'ssh remote go', '/w', tid='toolu_n')])
        self.kid(D, new + 3)
        self.assertEqual(set(self.ids()), {D})

    def test_cap_and_newest_first(self):
        n = MAX_UNLINKED + 6
        now = time.time() - 7200
        self.parent(P, [bash_line(now + i * 100, 'ssh remote go', '/w', tid='toolu_%d' % i) for i in range(n)])
        for i in range(n):
            self.kid('5%07d-1111-4111-8111-111111111111' % i, now + i * 100 + 3)
        got = self.got()
        self.assertEqual(len(got), MAX_UNLINKED)
        self.assertEqual([x['started'] for x in got], sorted((x['started'] for x in got), reverse=True))
        self.assertEqual(got[0]['id'], '5%07d-1111-4111-8111-111111111111' % (n - 1))

    def test_one_candidate_can_belong_to_two_busy_sessions(self):
        self.parent(P, [bash_line(T0, 'ssh remote go', '/w', tid='toolu_p')])
        self.parent(Q, [bash_line(T0 + 1, 'make', '/w', tid='toolu_q')])
        self.kid(C, T0 + 3)
        self.assertEqual((set(self.ids(P)), set(self.ids(Q))), ({C}, {C}))             # it is not hidden

    def test_state_and_codex_session(self):
        self.parent(P, [bash_line(T0, 'ssh remote go', '/w', tid='toolu_p')])
        self.kid(C, T0 + 3)
        self.scan()
        s = server.Session(os.path.join(self.proj, P + '.jsonl'))
        s.poll()
        st = views.state(s)
        self.assertEqual([(x['id'], x['reason']) for x in st['unlinked']], [(C, 'ended_before_seen')])
        self.assertEqual(json.loads(json.dumps(st['unlinked'])), st['unlinked'])
        cs = types.SimpleNamespace(provider='codex', id='x')
        self.assertEqual(server.Session.unlinked(cs), [])

    def test_a_failure_in_the_scan_does_not_stop_linking(self):
        self.parent(P, [bash_line(T0, 'claude -p hi', '/w', tid='toolu_p')])
        self.kid(C, T0 + 2)
        with mock.patch.object(link.LinkIndex, '_unlinked', side_effect=RuntimeError('boom')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.scan().cli_owners[C]['sid'], P)
        self.assertEqual(out.getvalue().strip(), 'unlinked scan error RuntimeError')          # only the name is printed


if __name__ == '__main__':
    unittest.main()
