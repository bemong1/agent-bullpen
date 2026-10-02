"""The file policy (board/util.py) and the diagnostics' parameters (board/diag.py).

- a file under /proc, /sys or /dev is never opened or listed, whichever way the path reaches it (a redirect `> /proc/<pid>/environ` written in a record)
- private key names are secret names; ordinary document names that only look alike stay readable
- a diagnostic carries only the parameters its code is known to have, in the shape that code gives them: a text that came out of a record never gets through

    python3 -m unittest tests.test_state_policy
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402,F401  (puts the repo root first on sys.path)

from board import diag, util  # noqa: E402
from board.facts import DIAG_CODES  # noqa: E402


def write(path, text='x\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class VirtualFiles(unittest.TestCase):
    """The kernel's views and the devices are not documents. Their files look regular to stat (a /proc file is S_ISREG), so the folder is what refuses them."""
    ENVIRON = '/proc/%d/environ' % os.getpid()

    @unittest.skipUnless(os.path.exists('/proc/self/environ'), 'no /proc here')
    def test_the_environment_of_a_process_is_not_opened_or_listed(self):
        for path in (self.ENVIRON, '/proc/self/environ', '/proc/self/cmdline', '/proc/self/status', '/proc/cpuinfo'):
            real = os.path.realpath(path)
            self.assertTrue(util.denied_file(real), path)
            self.assertTrue(util.denied_file(real, fresh=False), path)
            self.assertIsNone(util.stat_plain(path), path)
            self.assertIsNone(util.stat_regular(path), path)
            with self.assertRaises(util.Denied) as cm:
                util.open_safe(path, binary=True)
            self.assertEqual(cm.exception.why, 'denied')
            with self.assertRaises(util.Denied):
                util.open_safe(path, binary=True, strict=False)

    def test_the_three_roots_and_what_is_below_them(self):
        for path in ('/proc', '/proc/1/environ', '/sys', '/sys/kernel/uevent_seqnum', '/dev', '/dev/null', '/dev/shm/notes.md', '/dev/stdin'):
            self.assertTrue(util.denied_file(path), path)
        for path in ('/proc2/x.md', '/sysroot/a.md', '/devices/a.md', '/tmp/dev/a.md', '/home/u/proc/a.md', '/home/u/sys', '/a/proc'):
            self.assertFalse(util.denied_file(path), path)

    @unittest.skipUnless(os.path.exists('/proc/self/environ'), 'no /proc here')
    def test_a_link_into_the_proc_folder_is_refused_too(self):
        root = tempfile.mkdtemp(prefix='statepol-')
        try:
            link = os.path.join(root, 'env.md')
            os.symlink(self.ENVIRON, link)
            folder_link = os.path.join(root, 'p')
            os.symlink('/proc/self', folder_link)
            for path in (link, os.path.join(folder_link, 'environ')):
                self.assertIsNone(util.stat_plain(path), path)
                self.assertIsNone(util.stat_regular(path), path)
                with self.assertRaises(util.Denied):
                    util.open_safe(path, binary=True)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_an_ordinary_document_still_opens(self):
        root = tempfile.mkdtemp(prefix='statepol-')
        try:
            doc = write(os.path.join(root, 'r1', 'A.md'), 'findings\n')
            real = os.path.realpath(doc)
            self.assertFalse(util.denied_file(real))
            self.assertEqual(util.stat_plain(doc)[0], real)
            with util.open_safe(doc) as f:
                self.assertEqual(f.read(), 'findings\n')
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @unittest.skipUnless(os.path.exists('/proc/self/environ'), 'no /proc here')
    def test_the_output_index_does_not_read_it(self):
        """A launch whose output goes to `> /proc/<pid>/environ` is read for session ids by link.OutIndex (an output file is where a child's id may be). The environment
        of a process is not output: a read that got through would hand out whatever id-shaped text a variable holds."""
        from board.link import OutIndex
        sid = '99999999-8888-4777-8666-555555555555'
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], env={'SYNTH_JSON': '{"session_id": "%s"}' % sid, 'PATH': os.environ.get('PATH', '')})
        try:
            path = '/proc/%d/environ' % child.pid
            for _ in range(100):                                                   # the environment is there once the child has exec'd
                if os.path.exists(path) and open(path, 'rb').read():
                    break
                time.sleep(0.02)
            self.assertIn(sid.encode(), open(path, 'rb').read(), 'the control: the id-shaped text is in that environment')
            self.assertEqual(OutIndex._read(path), frozenset())
        finally:
            child.kill()
            child.wait()


class PrivateKeyNames(unittest.TestCase):
    SECRET = ('id_rsa', 'id_rsa.pub', 'id_rsa_work', 'id_dsa', 'id_ecdsa', 'id_ecdsa_sk', 'id_ed25519', 'id_ed25519.pub', 'ID_RSA', 'server.pem', 'SERVER.PEM',
              'fullchain.pem', 'tls.key', 'signing.KEY', 'cert.p12', 'cert.pfx', 'putty.ppk', 'store.jks', 'my.keystore')
    PLAIN = ('README.md', 'brief.md', 'A.md', 'B_gate.md', 'report.md', 'monkey.md', 'donkey.md', 'survey.md', 'keynote.md', 'pemberton.md', 'keys.md', 'key.md',
             'pem.md', 'rsa_notes.md', 'idea_rsa.md', 'idea.md', 'p12-notes.md', 'jks.md', 'ssh-guide.md', 'monkey.key.md', 'tls.key.md', 'server.pem.md', 'notes.txt')

    def test_private_key_names_are_secret_names(self):
        for name in self.SECRET:
            self.assertTrue(util.hidden_or_secret('/work/proj/' + name), name)

    def test_names_that_only_look_alike_stay_readable(self):
        for name in self.PLAIN:
            self.assertFalse(util.hidden_or_secret('/work/proj/' + name), name)

    def test_the_policy_holds_at_every_entrance(self):
        root = tempfile.mkdtemp(prefix='statepol-')
        try:
            for name in self.SECRET:
                path = write(os.path.join(root, 'unit', name), 'not a document\n')
                self.assertIsNone(util.stat_plain(path), name)
                with self.assertRaises(util.Denied) as cm:
                    util.open_safe(path)
                self.assertEqual(cm.exception.why, 'hidden', name)
            for name in self.PLAIN:
                path = write(os.path.join(root, 'unit', name), 'findings\n')
                self.assertEqual(util.stat_plain(path)[0], os.path.realpath(path), name)
                with util.open_safe(path) as f:
                    self.assertEqual(f.read(), 'findings\n', name)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_names_that_were_secret_before_still_are(self):
        for name in ('credentials.md', 'secrets.md', 'api_token.md', 'password.txt', 'kubeconfig', 'auth.json', 'my_api-key.md'):
            self.assertTrue(util.hidden_or_secret('/work/proj/' + name), name)


class DiagParams(unittest.TestCase):
    """`diag._entry` keeps a code's own parameters, in the shape the code gives them, and nothing else."""
    SID = '11111111-2222-4333-8444-555555555555'
    AID = 'a0123456789abcdef'

    def params(self, code, **kw):
        return diag._entry(code, 'agent', 'a1', **kw)['params']

    def test_the_parameters_each_producer_gives_are_kept(self):
        for code, kw in (('limit_group', {'resets_at': 1790007200.0, 'members': 3}), ('limit_group', {'resets_at': None, 'members': 2}),
                         ('not_resumed', {'reason': 'limit'}), ('not_resumed', {'reason': 'api_error'}),
                         ('torn_lines', {'recovered': 2, 'lost': 1}), ('stray_notice', {'count': 1}), ('multi_process', {'pids': 2}),
                         ('invisible_child', {'n': 1}), ('parse_errors', {'n': 4}),
                         ('format_drift', {'what': ['marker_missing:cost-state.totalDuration', 'out_of_range:2.1.290', 'invalid:version'], 'version': '2.1.290'}),
                         ('format_drift', {'what': ['marker_missing:cost-state'], 'version': None}),
                         ('cache_error', {'what': 'write', 'error': 'PermissionError'}), ('cache_error', {'what': 'untrusted'}),
                         ('evidence_conflict', {'other': self.SID}), ('content_author_differs', {'other': self.SID}), ('content_author_differs', {'other': self.AID}),
                         ('node_unresolved', {'run': 1}), ('node_unresolved', {}), ('orphan_launch', {'n': 2, 'ambiguous': True}), ('orphan_launch', {'n': 1}),
                         ('path_unresolved', {'n': 2}), ('content_only', {}), ('ambiguous_content', {}), ('fingerprint_incomplete', {}), ('proc_unknown', {}),
                         ('silent_live', {}), ('seat_tie_held', {'detail': '1/B_gate'}), ('seat_tie_held', {'detail': '-/sol'}), ('alias_collision', {'detail': 'A.md,a.md'}),
                         ('alias_collision', {'detail': 'r1,r01'}), ('path_ambiguous', {}), ('debate_in_misc', {}), ('declaration_missing', {}), ('listing_capped', {})):
            self.assertEqual(self.params(code, **kw), kw, (code, kw))

    def test_a_parameter_the_code_does_not_have_is_dropped(self):
        self.assertEqual(self.params('not_resumed', reason='limit', text='SYNTH instruction text', path='/SYNTH/x'), {'reason': 'limit'})
        self.assertEqual(self.params('content_only', n=3, other=self.SID), {})
        self.assertEqual(self.params('proc_unknown', env='CLAUDE_CODE_SESSION_ID=' + self.SID), {})
        self.assertEqual(self.params('silent_live', pids=2), {})

    def test_a_text_that_does_not_have_the_shape_is_dropped(self):
        leak = '2.1.290 /SYNTH/private_note'
        self.assertEqual(self.params('format_drift', version=leak, what=['out_of_range:' + leak, 'marker_missing:cost-state']), {'what': ['marker_missing:cost-state']})
        self.assertEqual(self.params('format_drift', version=leak, what=leak), {})
        self.assertEqual(self.params('format_drift', version='2.1.290\n', what=['out_of_range:2.1.290\n', 'invalid:version\n']), {})
        self.assertEqual(self.params('not_resumed', reason='exited'), {})                    # only a limit or an API error is waited on
        self.assertEqual(self.params('not_resumed', reason='/SYNTH/x'), {})
        self.assertEqual(self.params('cache_error', what='write', error='[Errno 13] Permission denied: /SYNTH/cache'), {'what': 'write'})
        self.assertEqual(self.params('cache_error', what='/SYNTH/x'), {})
        self.assertEqual(self.params('evidence_conflict', other='/SYNTH/x'), {})
        self.assertEqual(self.params('evidence_conflict', other=self.SID + ' /SYNTH/x'), {})
        self.assertEqual(self.params('seat_tie_held', detail='1/B\n/SYNTH/x'), {})
        self.assertEqual(self.params('seat_tie_held', detail='x' * 200), {})
        self.assertEqual(self.params('alias_collision', detail='a\x00b'), {})

    def test_a_number_has_to_be_a_plain_small_number(self):
        for bad in (True, -1, 10 ** 12, 1.5, 'x', None, [1], float('inf')):
            self.assertEqual(self.params('torn_lines', recovered=bad, lost=0).get('recovered'), None, repr(bad))
        self.assertEqual(self.params('limit_group', resets_at=float('nan'), members=2), {'members': 2})
        self.assertEqual(self.params('limit_group', resets_at='tomorrow', members=2), {'members': 2})
        self.assertEqual(self.params('limit_group', resets_at=True, members=2), {'members': 2})
        self.assertEqual(self.params('orphan_launch', n=1, ambiguous='yes'), {'n': 1})

    def test_a_code_nobody_knows_carries_no_parameter(self):
        e = diag._entry('made_up_code', 'agent', 'a1', n=1, text='x')
        self.assertEqual((e['code'], e['params']), ('made_up_code', {}))

    def test_every_code_of_the_plan_has_a_row(self):
        self.assertEqual(set(DIAG_CODES) - set(diag.PARAMS), set())

    def test_the_entry_still_has_its_other_fields(self):
        e = diag._entry('limit_group', 'orch', None, resets_at=5.0, members=2)
        self.assertEqual(e, {'code': 'limit_group', 'level': 'info', 'scope': 'orch', 'agent': None, 'unit': None, 'params': {'resets_at': 5.0, 'members': 2}})
        self.assertEqual(json.loads(json.dumps(e)), e)


if __name__ == '__main__':
    unittest.main()
