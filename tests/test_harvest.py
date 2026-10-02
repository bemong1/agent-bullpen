"""tools/harvest.py: a real session tree -> axis values + expected answers, and nothing else.

What is checked:
  * the output is an allow-list, not a filter: `Out` refuses a token outside the closed vocabulary, `--check` runs the same test on a file, and the vocabulary
    covers every case id and every expected answer the generator and the oracle can produce today;
  * secret baits (a token-shaped string, an e-mail, a path, an id, a stray UUID, a word that sits in the HOME path itself) put into the records of a tree
    never reach stdout or stderr, in the text and the JSON form, and the output passes `--check`;
  * round trip: a case built by the generator (tools/scenarios) is read back by harvest as the same axis values, for the axes a finished record can show
    (not `seen` of a dead process, `os`, `bait`, a deleted record, a switched or merely mentioned session, a Codex thread, a process table).
    The listed real cases are exact; the broad sample has a floor;
  * a feature the axes cannot express ends with `new axis value needed: <name>` and exit code 2; an unknown session ends with 1;
  * the tool does not use the board's judgments (only the definitions in board/facts.py and the board's safe way of opening a file a record points at);
  * a file a record points at (a script, an output file) is read only through that safe way: a secret name, a hidden path, a link to one, a pipe, /proc
    never reach the tool, and what such a file holds never changes what it says.

    python3 -m unittest discover -s tests
"""
import collections
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import harvest as H  # noqa: E402
from tools.scenarios import axes as A  # noqa: E402
from tools.scenarios import build as B  # noqa: E402
from tools.scenarios import run as R  # noqa: E402

HARVEST = os.path.join(REPO, 'tools', 'harvest.py')

# the axes a finished record cannot show, by bundle; and the cases whose scene the records cannot tell apart from a plainer one
UNSEEN = {'aff': ('seen', 'os', 'bait', 'target'), 'sta': ('os',), 'deb': ('decl', 'homonym', 'os')}
# values the generator draws that harvest does not read back (they were found in real records after the first generator and have no reading here yet): who wrote
# the instruction, a peer's stale output file, `pushd` and an unset folder variable, a run stopped again after a resume, a reader that only names the report,
# a quoted marker, a second round folder, an output file beside the report, a Korean instruction, a quiet participant
NOT_READ_BACK = {('aff', 'author'): ('peer', 'main', 'sub'), ('aff', 'out'): ('overwritten',), ('aff', 'cwd'): ('pushd', 'var'), ('sta', 'at'): ('resume_stopped',),
                 ('deb', 'role'): ('ref_reader',), ('deb', 'marker'): ('quoted',), ('deb', 'rdir'): ('both',), ('deb', 'rpath'): ('dash_o_aux',), ('deb', 'lang'): ('ko',),
                 ('deb', 'life'): ('stalled_silent',)}


def harvest_case(case, root):
    """Builds `case` as a synthetic HOME under root and harvests its orchestrator: (Built, Result)."""
    b = B.build_case(case, root)
    now = b.phases[-1].now if b.phases else B.T_BASE
    return b, H.classify(H.read_tree(b.claude, b.sid('orch')), now)


def specs(res, bundle):
    return [dict(kv.split('=', 1) for kv in cid.split(':', 1)[1].split(';')) for cid in res.cases if cid.startswith(bundle + ':')]


def folds_case(case):
    """Whether the scene of `case` needs B.md and b.md side by side on a file system that folds case (macOS by default): it cannot be built, so there is nothing to read back."""
    return B.needs_two_names_by_case(case) and B.folds_case(tempfile.gettempdir())


def comparable(case):
    """The axis values of `case` a record can show, or None when the whole case is out of reach of a finished record."""
    if case.bundle not in UNSEEN:
        return None                             # the coupling bundle is not harvested
    if any(case.v[a] in vals for (b, a), vals in NOT_READ_BACK.items() if b == case.bundle):
        return None
    if folds_case(case):
        return None
    optional = A.OPTIONAL.get(case.bundle, ())
    v, want = case.v, {a: case.v[a] for a in case.axes if a not in UNSEEN[case.bundle] and a not in optional}
    if case.bundle == 'aff':
        if v['target'] != 'cli' or v['bait'] != 'none' or v['form'] == 'deleted' or v['decoy'] in ('switch', 'sidmention'):
            return None
        # a call that does not return at once cannot end before its child starts, and a detached call has no background id: the record shows neither
        if v['timing'] in ('call_first', 'bg_no_notif') and not (v['way'] == 'bg' or (v['way'] == 'detach' and v['timing'] == 'call_first')):
            want.pop('timing')
        if v['src'] == 'posarg' and v['form'] in ('session_id', 'fork'):
            want.pop('form')                    # the scene's positional script carries no flag
        if v['way'] == 'pysub':
            want.pop('out')                     # a python call hides its redirect
    elif case.bundle == 'sta':
        if v['skind'] == 'codex' or v['at'] == 'after_restart' or v['flaw'] == 'multi_proc':
            return None
        if v['life'] == 'kill_resume' and v['at'] != 'after_resume':
            want.pop('life')                    # the same record as a plain stop until it is resumed
        if v['flaw'] == 'old_format' and v['skind'] != 'cli':
            want.pop('flaw')
        if v['flaw'] in ('unknown_type', 'torn', 'future_version', 'field_gone') and v['skind'] == 'main':
            want.pop('flaw')
    else:
        if v['kind'] == 'codex' or v['structure'] == 'flat' or v['role'] in ('absent', 'tag_only', 'rival', 'failed_write', 'ghost', 'quoter'):
            return None
    return want


def best_match(res, case, want):
    """(matching axes, got) of the spec harvest emitted that agrees with `want` on the most axes."""
    best, n = None, -1
    for got in specs(res, case.bundle):
        k = sum(1 for a in want if got.get(a) == want[a])
        if k > n:
            best, n = got, k
    return best, n


class Vocabulary(unittest.TestCase):
    def test_out_refuses_a_token_outside_the_vocabulary(self):
        out = H.Out(io.StringIO())
        out.line('case 3 x sta:skind=cli;life=normal_end;flaw=none;at=just_ended;os=linux')
        for bad in ('a path /home/zoe/project', 'toolu_0123456789abcdef', 'sk-ant-api03-AbCdEf', 'zoe@example.org', 'c0ffee11-3d6a', 'x' * 5 + 'yz'):
            with self.assertRaises(H.NotAllowed, msg=bad):
                out.line('case 1 x ' + bad)

    def test_the_error_does_not_quote_the_token(self):
        secret = 'sk-ant-api03-SECRETSECRET'
        with self.assertRaises(H.NotAllowed) as cm:
            H.Out(io.StringIO()).line('case ' + secret)
        self.assertNotIn('SECRET', str(cm.exception))

    def test_check_mode(self):
        ok, bad = H.check_text('case 2 x sta:skind=cli;life=normal_end;flaw=none;at=just_ended;os=linux\n  expect child status=done reason=None resets_at=None\n')
        self.assertTrue(ok, bad)
        ok, bad = H.check_text('case 2 x sta:skind=cli\nthe orchestrator said hello\n')
        self.assertFalse(ok)
        self.assertGreater(bad, 0)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, 'out.txt')
        with open(path, 'w') as f:
            f.write('case 1 x sta:skind=cli;life=normal_end;flaw=none;at=just_ended;os=linux\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(H.main(['--check', path]), 0)
            with open(path, 'a') as f:
                f.write('see /home/zoe/notes\n')
            self.assertEqual(H.main(['--check', path]), 1)

    def test_every_generated_case_and_answer_is_inside_the_vocabulary(self):
        vocab = H.vocabulary()
        bad = []
        for case in R.select():
            for line in ['case 1 x ' + case.id] + H.expect_lines(case):
                if H.foreign_tokens(line, vocab):
                    bad.append(line[:100])
        self.assertEqual(bad[:3], [], 'the generator or the oracle grew a word harvest does not know: add it to tools/harvest.py (%d lines)' % len(bad))

    JUDGMENTS = ('board.link', 'board.affil', 'board.units', 'board.runstate', 'board.debates', 'board.sessions', 'board.lineage', 'board.fingerprint', 'board.views',
                 'board.agents', 'board.diag', 'board.plans', 'board.catalog', 'board.tokens')

    def test_harvest_uses_no_judgment_of_the_board(self):
        """Its own linking has the misses this tool is meant to find: reading a tree (and a file a record points at) loads none of the modules that judge."""
        code = ('import sys, tempfile, os; import tools.harvest as H; d = tempfile.mkdtemp(); p = os.path.join(d, "x.txt"); open(p, "w").write("x"); '
                'assert H.read_aux(p, 10) == b"x"; print(sorted(m for m in sys.modules if m.startswith("board.")))')
        out = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(out.returncode, 0, out.stderr)
        loaded = set(eval(out.stdout.strip()))
        self.assertIn('board.util', loaded)                                       # the safe way of opening is the board's own
        self.assertEqual(loaded & set(self.JUDGMENTS), set(), loaded)


class Commands(unittest.TestCase):
    """The reading of one launching command (pure)."""

    def inv(self, cmd, files=None):
        r = H.analyze_command(cmd, (files or {}).get)
        self.assertEqual(len(r['invs']), 1, cmd)
        return r['invs'][0]

    def test_ways_and_prompt_forms(self):
        i = self.inv('cd /w && claude -p --model m "Please review the amber basin"')
        self.assertEqual((i.via, i.form, i.container, i.cd), ('arg', 'new', None, '/w'))
        i = self.inv('cd /w && setsid nohup claude -p --model m "x y z" > /dev/null 2>&1 &')
        self.assertTrue({'setsid', 'nohup'} <= i.prefixes and i.bg)
        self.assertTrue(self.inv('for x in "aaa" "bbb"; do claude -p --model m "$x"; done').loop)
        self.assertEqual(self.inv("claude -p --model m <<'EOF'\nline one\nEOF").via, 'heredoc')
        i = self.inv('claude -p --model m "$(cat /s/pa.txt)$(cat /s/pb.txt)"')
        self.assertEqual((i.via, i.files), ('subst', ['/s/pa.txt', '/s/pb.txt']))
        self.assertEqual(self.inv('claude -p --model m < /s/p.txt').via, 'stdin')
        i = self.inv('cat /s/p.txt | claude -p --model m')
        self.assertEqual((i.via, i.files), ('pipe', ['/s/p.txt']))
        self.assertEqual(self.inv("printf '%s\\n' \"text\" | xargs -P2 -I{} claude -p --model m \"{}\"").container, 'xargs')
        self.assertEqual(self.inv("cd /w && tmux new-session -d -s w 'claude -p --model m \"text\"'").container, 'tmux')
        self.assertIn('timeout', self.inv('timeout 600 claude -p "text"').prefixes)
        self.assertTrue(self.inv('sleep 90 && claude -p "text"').after_sleep)
        self.assertIn('env-i', self.inv('env -i PATH="$PATH" claude -p "text"').prefixes)

    def test_form_flags(self):
        self.assertEqual(self.inv('claude -p --resume abc "t"').form, 'resume')
        self.assertEqual(self.inv('claude -p --resume abc --fork-session "t"').form, 'fork')
        self.assertEqual(self.inv('claude -p --session-id abc "t"').form, 'session_id')
        self.assertEqual(self.inv('claude -p --no-session-persistence "t"').form, 'nopersist')

    def test_a_script_is_read_through_its_body(self):
        files = {'/s/run.sh': 'cd "$1"\nenv -i PATH="$PATH" claude -p --model "$2" > /x/o.json "$3"\n', '/s/launch.py': "import subprocess\nsubprocess.run(['claude','-p','--resume','abc','text'])\n"}
        i = self.inv('bash /s/run.sh /w m "hello there"', files)
        self.assertEqual((i.posarg, i.script), (True, '/s/run.sh'))
        self.assertIn('env-i', i.prefixes)
        i = self.inv('python3 /s/launch.py', files)
        self.assertEqual((i.container, i.form, i.resume_id), ('pysub', 'resume', 'abc'))

    def test_text_in_a_heredoc_or_a_string_is_not_a_launch(self):
        self.assertEqual(H.analyze_command("cat > /s/doc.md <<'EOF'\nrun claude -p \"x\" to start\nEOF")['invs'], [])
        self.assertEqual(H.analyze_command("echo 'claude -p \"x\"'")['invs'], [])


class CwdKind(unittest.TestCase):
    """Where a `cd` leads against where the child records its own folder. A link on the way (macOS keeps /tmp and /var behind /private) is a symlinked folder only when
    the child names the folder differently; the same name needs no resolving."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='hv-cwd-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, 'real', 'work', 'sub'))
        os.makedirs(os.path.join(self.root, 'real', 'other'))
        os.symlink('real', os.path.join(self.root, 'via'))                      # like /var -> private/var
        os.symlink(os.path.join(self.root, 'real', 'work', 'sub'), os.path.join(self.root, 'real', 'work', 'link'))

    def kind(self, call_cwd, cd, child_cwd):
        inv = H.Inv()
        inv.cd = cd
        return H.cwd_kind(types.SimpleNamespace(cwd=os.path.join(self.root, child_cwd)), types.SimpleNamespace(cwd=os.path.join(self.root, call_cwd)), inv)

    def test_the_same_name_behind_a_link_on_the_way_is_not_a_symlink(self):
        self.assertEqual(self.kind('via/work', 'sub', 'via/work/sub'), 'cd')
        self.assertEqual(self.kind('via/work', '../other', 'via/other'), 'other')
        self.assertEqual(self.kind('via/work', None, 'via/work'), 'same')
        self.assertEqual(self.kind('via/work', None, 'via/other'), 'other')
        self.assertEqual(self.kind('real/work', 'sub', 'real/work/sub'), 'cd')            # nothing is linked: the same answer

    def test_a_folder_that_is_a_link_or_is_recorded_under_another_name_is_a_symlink(self):
        self.assertEqual(self.kind('via/work', 'link', 'real/work/sub'), 'symlink')       # the folder itself is a link
        self.assertEqual(self.kind('via/work', 'sub', 'real/work/sub'), 'symlink')        # the child records the resolved name of what the call named through a link


class RoundTrip(unittest.TestCase):
    """A case the generator builds is read back as the same axis values."""

    def check(self, case, strict=True):
        root = tempfile.mkdtemp(prefix='hv-')
        try:
            _, res = harvest_case(case, root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        want = comparable(case)
        if want is None:
            return None
        got, n = best_match(res, case, want)
        return None if got is None else [a for a in want if got.get(a) != want[a]]

    def test_the_real_cases_of_the_plan_come_back_exactly(self):
        exact = ('A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9', 'A10', 'A11', 'A13', 'A14', 'A16', 'A17', 'A18', 'A20',
                 'B1', 'B6', 'B7', 'B8', 'B9', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9', 'C10', 'C12', 'C16')
        reals = dict(A.real_cases())
        wrong = {}
        for name in exact:
            if folds_case(reals[name]):
                continue                                                          # B9: B.md and b.md are one file on this file system
            diff = self.check(reals[name])
            if diff != []:
                wrong[name] = diff
        self.assertEqual(wrong, {}, 'axes that did not come back (None: no spec at all)')

    def test_a_scene_that_needs_two_names_by_case_is_left_out_on_a_folding_file_system(self):
        b9 = dict(A.real_cases())['B9']                                           # B.md next to b.md
        disk_folds = B.folds_case(tempfile.gettempdir())                        # this machine's own disk (macOS folds case by default)
        with mock.patch.object(B, 'folds_case', lambda folder: True):
            self.assertTrue(folds_case(b9))
            self.assertIsNone(comparable(b9))
        with mock.patch.object(B, 'folds_case', lambda folder: False):
            self.assertFalse(folds_case(b9))
            self.assertIn('nstyle', comparable(b9))
            if not disk_folds:                                                    # the round trip writes both files for real: only a case-sensitive disk keeps them apart
                self.assertEqual(self.check(b9), [])

    def test_the_unreachable_real_cases_are_reported_as_such(self):
        reals = dict(A.real_cases())
        for name in ('A12', 'A15', 'A19', 'A21', 'B5', 'B10', 'C11', 'C14'):
            self.assertIsNone(comparable(reals[name]), name)

    def sample(self, bundle):
        out = []
        for case in R.select():
            if case.bundle == bundle and comparable(case) is not None:
                out.append(case)
        return out

    def floor(self, bundle, floor):
        cases = self.sample(bundle)
        self.assertGreater(len(cases), 20, bundle)
        bad = []
        for case in cases:
            diff = self.check(case)
            if diff != []:
                bad.append((case.id, diff))
        share = 1.0 - len(bad) / float(len(cases))
        self.assertGreaterEqual(share, floor, '%s: %d of %d cases did not come back; first: %s' % (bundle, len(bad), len(cases), bad[:2]))

    def test_aff_sample(self):
        self.floor('aff', 0.92)

    def test_sta_sample(self):
        self.floor('sta', 0.93)

    def test_deb_sample(self):
        self.floor('deb', 0.5)           # the debate bundle is the weakest: its scene depends on disk listings and roles a record shows only in part


class FilesARecordPointsAt(unittest.TestCase):
    """A record may point at anything: a credentials file, a hidden folder, a pipe that never ends, the environment of a process. Every read of such a file goes
    through the board's safe way of opening it, and what a refused file holds never changes what harvest says."""

    UUID = '0badc0de-5a17-4b1d-9e0f-c0ffee123456'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='hv-files-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_a_plain_file_is_read_and_a_big_one_is_skipped(self):
        p = self.put('plain.txt', 'see %s here' % self.UUID)
        self.assertEqual(H.read_aux(p, 100), b'see %s here' % self.UUID.encode())
        self.assertIsNone(H.read_aux(p, 5))                                       # larger than the cap: not read at all
        self.assertEqual(H.read_aux(p, 5, skip_big=False), b'see 0')              # unless the first bytes are what is asked for
        self.assertIsNone(H.read_aux(os.path.join(self.root, 'missing.txt'), 10))

    def test_a_secret_name_a_hidden_path_and_a_link_to_one_are_refused(self):
        secret = self.put('api_token.txt', self.UUID)
        hidden = self.put('.env', self.UUID)
        dotdir = self.put('.config/gh/hosts.yml', self.UUID)
        cred = self.put('.credentials.json', self.UUID)
        link_hidden = os.path.join(self.root, 'looks-plain.txt')
        os.symlink(hidden, link_hidden)
        link_secret = os.path.join(self.root, 'notes.txt')
        os.symlink(secret, link_secret)
        for path in (secret, hidden, dotdir, cred, link_hidden, link_secret):
            self.assertIsNone(H.read_aux(path, 1000), path)
            self.assertEqual(H._file_uuids(path), [], path)
            self.assertFalse(H._file_has(path, self.UUID), path)
        plain = self.put('plain.txt', self.UUID)
        link_plain = os.path.join(self.root, 'to-plain.txt')
        os.symlink(plain, link_plain)
        self.assertEqual(H._file_uuids(link_plain), [self.UUID])                  # a link to an ordinary file is an ordinary file
        self.assertEqual(H._file_uuids(plain), [self.UUID])

    def test_the_environment_of_a_process_and_devices_are_never_read(self):
        for path in ('/proc/self/environ', '/proc/self/cmdline', '/dev/null', '/dev/zero', '/sys/kernel/hostname'):
            self.assertIsNone(H.read_aux(path, 100), path)
        self.assertIsNone(H.read_aux(self.root, 100))                             # a folder

    def test_a_pipe_does_not_stop_the_harvest(self):
        fifo = os.path.join(self.root, 'pipe.out')
        os.mkfifo(fifo)
        code = ('import sys; sys.path.insert(0, %r); import tools.harvest as H; '
                'assert H.read_aux(%r, 100) is None and H._file_uuids(%r) == [] and not H._file_has(%r, "x"); print("done")') % (REPO, fifo, fifo, fifo)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual((out.returncode, out.stdout.strip()), (0, 'done'), out.stderr)

    def test_what_a_refused_output_file_holds_does_not_change_the_answer(self):
        import types
        child = types.SimpleNamespace(sid='aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa')
        inv = lambda path: types.SimpleNamespace(redirs=[('>', path)], json=True, assigns={})
        call = types.SimpleNamespace(cwd='')
        ctx = None
        other = '%s' % self.UUID
        plain = self.put('out.json', '{"session_id": "%s"}' % other)
        secret = self.put('secret_out.json', '{"session_id": "%s"}' % other)
        mine = self.put('mine.json', '{"session_id": "%s"}' % child.sid)
        self.assertEqual(H.out_kind(child, call, inv(plain), ctx), 'reused')      # another session's id in an ordinary file: the proof was written over
        self.assertEqual(H.out_kind(child, call, inv(secret), ctx), 'json_file')  # the same words in a file that must not be read: nothing is learned from it
        self.assertEqual(H.out_kind(child, call, inv(mine), ctx), 'json_file')
        self.assertEqual(H.out_kind(child, call, inv(os.path.join(self.root, 'gone.json')), ctx), 'removed')       # a file that is not there is a removed one

    def test_a_script_a_call_runs_is_read_the_same_way(self):
        import types
        sh = self.put('run.sh', 'claude -p "hello"\n')
        secret_sh = self.put('token_run.sh', 'claude -p "hello"\n')
        mk = lambda path: types.SimpleNamespace(cmd='bash %s' % path, cwd='', ts=0.0)
        self.assertEqual(H.read_disk_script(mk(sh)), 'claude -p "hello"\n')
        self.assertIsNone(H.read_disk_script(mk(secret_sh)))
        node = H.Node('main', 'sid', os.path.join(self.root, 'x.jsonl'))
        read = H.Ctx(types.SimpleNamespace(nodes=[]), 0.0).script_reader(node, mk(sh))
        self.assertEqual(read(sh), 'claude -p "hello"\n')
        self.assertIsNone(read(secret_sh))
        self.assertIsNone(read('/proc/self/environ'))

    def test_no_reader_of_such_a_file_opens_it_by_itself(self):
        import ast
        with open(os.path.join(REPO, 'tools', 'harvest.py')) as f:
            tree = ast.parse(f.read())
        names = ('_file_uuids', '_file_has', 'out_kind', 'read_disk_script', 'script_reader')
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in names:
                seen.add(node.name)
                opens = [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'open']
                self.assertEqual(opens, [], '%s opens a file itself' % node.name)
        self.assertEqual(seen, set(names))

    def test_a_pipe_or_a_link_left_among_the_records_is_skipped(self):
        home = os.path.join(self.root, 'claude')
        proj = os.path.join(home, 'projects', 'p')
        os.makedirs(proj)
        sid = '11111111-2222-4333-8444-555555555555'
        good = os.path.join(proj, sid + '.jsonl')
        with open(good, 'w') as f:
            f.write(B.dump({'type': 'user', 'timestamp': B.iso(B.T_BASE), 'sessionId': sid, 'cwd': '/w', 'entrypoint': 'cli', 'message': {'role': 'user', 'content': 'hello'}}) + '\n')
        os.mkfifo(os.path.join(proj, '22222222-2222-4333-8444-555555555555.jsonl'))
        os.symlink(good, os.path.join(proj, '33333333-2222-4333-8444-555555555555.jsonl'))
        code = ('import sys; sys.path.insert(0, %r); import tools.harvest as H; heads = H.session_heads(%r); '
                'print(sorted(p.rsplit("/", 1)[1][:8] for p in heads))') % (REPO, home)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "['11111111']")


class Secrets(unittest.TestCase):
    """Baits in the records of a tree never reach the output."""

    BAIT_WORD = 'zqxvbaitword'
    BAIT_TOKEN = 'sk-ant-api03-Zq9xV7bTr2LmP4wKd8NcYh5GfJe3UaS1oIvX6tRq0zWn'
    BAIT_MAIL = 'zoe.baitwoman@example.invalid'
    BAIT_USER = 'baituserzq'
    BAIT_UUID = '0badc0de-5a17-4b1d-9e0f-c0ffee123456'
    BAIT_HEX = 'deadbeefcafe1234'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='hv-%s-' % self.BAIT_WORD)         # the bait sits in the HOME path, so in every cwd and file name of the records
        self.addCleanup(shutil.rmtree, self.root, True)

    def baits(self):
        return [self.BAIT_WORD, self.BAIT_TOKEN, self.BAIT_MAIL, self.BAIT_USER, self.BAIT_UUID, self.BAIT_HEX, os.path.basename(self.root)]

    def line(self, t, typ, sid, **kw):
        d = {'type': typ, 'uuid': B.sid_of('bait', '%s-%s' % (typ, t)), 'timestamp': B.iso(t), 'sessionId': sid, 'cwd': '/home/%s/secret-project' % self.BAIT_USER,
             'userType': 'external', 'entrypoint': 'cli', 'version': B.VERSION}
        d.update(kw)
        return B.dump(d) + '\n'

    def poison(self, b):
        """Appends bait-bearing records to the orchestrator, the child and the sub-agent records, and writes a bait file next to them. They sit before the launch, in a
        window that does not change what the tree looks like."""
        early = b.T(-5000)
        tid = 'toolu_%s' % self.BAIT_HEX
        call = {'command': 'echo %s | mail -s "%s" %s ; cat /home/%s/secret-project/%s.txt' % (self.BAIT_TOKEN, self.BAIT_WORD, self.BAIT_MAIL, self.BAIT_USER, self.BAIT_WORD),
                'description': 'send %s' % self.BAIT_WORD}
        for role, path in list(b.paths.items()):
            if not path.endswith('.jsonl'):
                continue
            sid = b.ids.get(role) or b.sid('orch')
            st = os.stat(path)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(self.line(early, 'assistant', sid, message={'role': 'assistant', 'model': B.MODEL, 'stop_reason': 'tool_use', 'content': [
                    {'type': 'tool_use', 'id': tid + role, 'name': 'Bash', 'input': call}]}))
                f.write(self.line(early + 1, 'user', sid, message={'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': tid + role, 'content': 'token %s id %s mail %s' % (self.BAIT_TOKEN, self.BAIT_UUID, self.BAIT_MAIL)}]},
                    toolUseResult={'stdout': self.BAIT_TOKEN, 'stderr': '', 'interrupted': False}))
                f.write(self.line(early + 2, 'user', sid, message={'role': 'user', 'content': 'remember %s and %s at /home/%s/%s' % (self.BAIT_TOKEN, self.BAIT_UUID, self.BAIT_USER, self.BAIT_WORD)},
                                  promptSource='typed', origin={'kind': 'human'}))
            os.utime(path, (st.st_atime, st.st_mtime))                       # the record's age is part of what harvest reads: keep it
        for p in self.sub_metas(b):
            with open(p, encoding='utf-8') as f:
                meta = json.load(f)
            meta['description'] = '%s %s' % (meta.get('description', ''), self.BAIT_WORD)
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(meta, f)
        with open(os.path.join(b.scratch, '%s.txt' % self.BAIT_WORD), 'w', encoding='utf-8') as f:
            f.write('%s %s %s\n' % (self.BAIT_TOKEN, self.BAIT_MAIL, self.BAIT_UUID))

    def sub_metas(self, b):
        out = []
        for dirpath, _, files in os.walk(b.projects):
            out += [os.path.join(dirpath, f) for f in files if f.endswith('.meta.json')]
        return out

    def run_cli(self, b, *extra):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        cp = subprocess.run([sys.executable, HARVEST, b.sid('orch'), '--claude-home', b.claude, '--now', str(b.phases[-1].now)] + list(extra), capture_output=True, text=True,
                            cwd=REPO, env=env)
        return cp

    def assert_clean(self, text, b):
        for bait in self.baits():
            self.assertNotIn(bait, text, 'a bait reached the output')
        # nothing of an id, however short: the ids of this tree are made from the case id, so every 8-character window of them is a leak
        ids = [x for x in list(b.ids.values()) + [self.BAIT_TOKEN, self.BAIT_UUID, self.BAIT_HEX] if isinstance(x, str) and len(x) >= 12]
        for x in ids:
            for i in range(0, len(x) - 8):
                self.assertNotIn(x[i:i + 9], text, 'a piece of an id reached the output')
        self.assertNotRegex(text, r'[0-9a-f]{8}-[0-9a-f]{4}-')
        self.assertNotRegex(text, r'@[a-z0-9-]+\.[a-z]{2,}')
        self.assertNotIn('/home/', text)
        self.assertNotIn(self.root, text)
        ok, bad = H.check_text(text)
        self.assertTrue(ok, '%d token(s) outside the vocabulary' % bad)

    def cases(self):
        reals = dict(A.real_cases())
        return [reals[n] for n in ('A9', 'A2', 'A11', 'A8', 'C1', 'C10', 'B1', 'B6')]

    def test_baits_in_the_records_never_reach_the_output(self):
        for case in self.cases():
            root = os.path.join(self.root, case.id.replace(':', '_').replace(';', '_').replace('=', '-')[:60])
            b = B.build_case(case, root)
            self.poison(b)
            for extra in ((), ('--json',)):
                cp = self.run_cli(b, *extra)
                self.assertIn(cp.returncode, (0, 2), cp.stderr[-300:])
                self.assertTrue(cp.stdout.strip(), 'no output for %s' % case.id)
                self.assert_clean(cp.stdout + cp.stderr, b)
                if extra:
                    doc = json.loads(cp.stdout)
                    self.assertTrue(doc['cases'])

    def test_the_poisoned_tree_gives_the_same_specs_as_the_clean_one(self):
        case = dict(A.real_cases())['A9']
        clean = B.build_case(case, os.path.join(self.root, 'clean'))
        poisoned = B.build_case(case, os.path.join(self.root, 'poisoned'))
        self.poison(poisoned)
        a = H.classify(H.read_tree(clean.claude, clean.sid('orch')), clean.phases[-1].now)
        c = H.classify(H.read_tree(poisoned.claude, poisoned.sid('orch')), poisoned.phases[-1].now)
        self.assertEqual(sorted(a.cases), sorted(c.cases))

    def test_a_leak_would_be_caught(self):
        """The test above is only worth something if it fails on a leak: feed the checker a line that holds one."""
        for leak in (self.BAIT_TOKEN, self.BAIT_MAIL, '/home/%s/x' % self.BAIT_USER, self.BAIT_UUID):
            ok, bad = H.check_text('case 1 x sta:skind=cli;life=normal_end;flaw=none;at=just_ended;os=linux %s\n' % leak)
            self.assertFalse(ok, leak)


class ExitCodes(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='hv-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def edit(self, b, old, new):
        n = 0
        for dirpath, _, files in os.walk(b.projects):
            for f in files:
                if f.endswith('.jsonl'):
                    p = os.path.join(dirpath, f)
                    with open(p, encoding='utf-8') as fh:
                        text = fh.read()
                    if old in text:
                        n += text.count(old)
                        with open(p, 'w', encoding='utf-8') as fh:
                            fh.write(text.replace(old, new))
        self.assertGreater(n, 0)

    def run_cli(self, b, *args):
        return subprocess.run([sys.executable, HARVEST, b.sid('orch'), '--claude-home', b.claude, '--now', str(b.phases[-1].now)] + list(args), capture_output=True, text=True,
                              cwd=REPO, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))

    def test_a_way_the_axes_cannot_say_exits_2_and_names_the_feature(self):
        case = A.Case('aff', {'way': 'direct'})
        b = B.build_case(A.normalize(case), self.root)
        self.edit(b, '"command":"claude -p', '"command":"env -i setsid claude -p')           # env -i loses the environment, setsid the lineage: no single way has both
        cp = self.run_cli(b)
        self.assertEqual(cp.returncode, 2, cp.stdout[-400:] + cp.stderr[-300:])
        self.assertIn(H.NEW_AXIS + 'way_combination(envi+detach)', cp.stdout)
        ok, bad = H.check_text(cp.stdout)
        self.assertTrue(ok)

    def test_a_clean_tree_exits_0(self):
        b = B.build_case(A.normalize(A.Case('aff', {'way': 'bg'})), self.root)
        cp = self.run_cli(b)
        self.assertEqual(cp.returncode, 0, cp.stdout[-400:] + cp.stderr[-300:])
        self.assertNotIn('new axis value needed', cp.stdout)
        self.assertIn('case ', cp.stdout)

    def test_an_unknown_session_exits_1(self):
        b = B.build_case(A.normalize(A.Case('aff', {})), self.root)
        cp = subprocess.run([sys.executable, HARVEST, 'ffffffff', '--claude-home', b.claude], capture_output=True, text=True, cwd=REPO,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(cp.stdout, '')

    def test_the_output_reads_back_as_case_ids(self):
        b = B.build_case(A.normalize(A.Case('aff', {'way': 'bg', 'out': 'json_file'})), self.root)
        cp = self.run_cli(b)
        ids = re.findall(r'^case \d+ x (\S+)$', cp.stdout, re.M)
        self.assertTrue(ids)
        for cid in ids:
            case = A.Case.from_id(cid)
            self.assertEqual(case.id, cid)
            self.assertTrue(A.is_valid(case), cid)


if __name__ == '__main__':
    unittest.main()
