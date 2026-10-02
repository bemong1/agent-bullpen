"""How far `cd` reaches (shell_cwd in board/link.py): a `cd` inside a subshell, a list run with `&`, or a pipe does not leak into later commands.
`claude -p` (LinkIndex._cli_line) and `codex exec` (scwd in cx_launches) use the same function.

    python3 -m unittest discover -s tests
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
import test_stage2 as st2  # noqa: E402

T0 = st2.T0
HOME_X = os.path.expanduser('~/work/demo-app')


def cli_cwds(cmd, base='/w'):
    """The working folder at each place in the command where `claude -p` runs (in order of appearance)."""
    f = {'pos': 0, 'sid': 's', 'calls': [], 'cli': []}
    server.LinkIndex()._cli_line(f, st2.bash_line(T0, cmd, base).encode())
    return [c['cwd'] for c in f['cli']]


def cx_cwds(cmd, base='/w'):
    return [L['scwd'] for L in server.cx_launches(cmd, {}, base)]


class CliScope(unittest.TestCase):
    def test_reported_command(self):
        """A command shape whose linking really went wrong: in `cd X && (A) & (B); wait`, A runs in X and B in the call's folder."""
        cmd = ('SP=/tmp/sp; mkdir -p ~/work/demo-app/docs\n'
               'cd ~/work/demo-app && (claude -p --model m --effort xhigh "$(cat $SP/a.md)" < /dev/null > $SP/a.json 2>&1; echo "a exit $?") '
               '& (claude -p --model m --effort xhigh "$(cat $SP/b.md)" < /dev/null > $SP/b.json 2>&1; echo "b exit $?"); wait')
        self.assertEqual(cli_cwds(cmd, '/home/x/other-proj'), [HOME_X, '/home/x/other-proj'])

    def test_subshell_does_not_leak(self):
        self.assertEqual(cli_cwds('(cd /x && claude -p a); claude -p b'), ['/x', '/w'])
        self.assertEqual(cli_cwds('cd /x; (cd /y; claude -p a); claude -p b'), ['/y', '/x'])
        self.assertEqual(cli_cwds('(cd /x; (cd /y; claude -p a) & claude -p b); claude -p c'), ['/y', '/x', '/w'])

    def test_command_substitution_and_backticks(self):
        self.assertEqual(cli_cwds('d=$(cd /x; pwd); claude -p a'), ['/w'])
        self.assertEqual(cli_cwds('echo `cd /x; pwd`; claude -p a'), ['/w'])
        self.assertEqual(cli_cwds('echo "$(cd /x; claude -p a)"; claude -p b'), ['/x', '/w'])
        self.assertEqual(cli_cwds('cat <(cd /x; claude -p a); claude -p b'), ['/x', '/w'])

    def test_background_list_is_a_subshell(self):
        self.assertEqual(cli_cwds('cd /x && claude -p a & claude -p b'), ['/x', '/w'])
        self.assertEqual(cli_cwds('cd /x & claude -p a'), ['/w'])
        self.assertEqual(cli_cwds('cd /x && claude -p a & cd /y && claude -p b'), ['/x', '/y'])
        self.assertEqual(cli_cwds('cd /x; claude -p a & claude -p b'), ['/x', '/x'])           # a cd ended by ";" stays in the shell
        self.assertEqual(cli_cwds('cd /x &&\nclaude -p a &\nclaude -p b'), ['/x', '/w'])       # a newline after && continues the list

    def test_pipe_parts_are_subshells(self):
        self.assertEqual(cli_cwds('cd /x | claude -p a; claude -p b'), ['/w', '/w'])
        self.assertEqual(cli_cwds('claude -p a | cd /x; claude -p b'), ['/w', '/w'])
        self.assertEqual(cli_cwds('cd /x && claude -p a | tee o; claude -p b'), ['/x', '/x'])   # a cd before the pipe covers the whole pipe
        self.assertEqual(cli_cwds('cd /x && { cd /y | claude -p a; } && claude -p b'), ['/x', '/x'])

    def test_same_shell_groups_keep_cd(self):
        self.assertEqual(cli_cwds('{ cd /x; claude -p a; }; claude -p b'), ['/x', '/x'])
        self.assertEqual(cli_cwds('if cd /x; then claude -p a; fi; claude -p b'), ['/x', '/x'])
        self.assertEqual(cli_cwds('for i in 1 2; do cd /x; claude -p a; done; claude -p b'), ['/x', '/x'])
        self.assertEqual(cli_cwds('while cd /x; do claude -p a; break; done; claude -p b'), ['/x', '/x'])
        self.assertEqual(cli_cwds('case z in z) cd /x; claude -p a;; esac; claude -p b'), ['/x', '/x'])
        self.assertEqual(cli_cwds('cd /x ; claude -p a; FOO=1 cd /y; claude -p b'), ['/x', '/y'])        # a leading assignment leaves the command position as it is
        self.assertEqual(cli_cwds('cd /x || true; claude -p a'), ['/x'])
        self.assertEqual(cli_cwds('cd /x\ncd sub\nclaude -p a'), ['/x/sub'])

    def test_group_in_background_or_pipe_is_a_subshell(self):
        self.assertEqual(cli_cwds('{ cd /x; claude -p a; } & claude -p b'), ['/x', '/w'])
        self.assertEqual(cli_cwds('for i in 1 2; do cd /x; claude -p a; done & claude -p b'), ['/x', '/w'])
        self.assertEqual(cli_cwds('{ cd /x; claude -p a; } | cat; claude -p b'), ['/x', '/w'])

    def test_quotes_heredoc_comments_redirections_are_not_boundaries(self):
        self.assertEqual(cli_cwds("echo 'a & (b'; cd /x && claude -p a"), ['/x'])
        self.assertEqual(cli_cwds('echo "x & (y) | z"; cd /x; claude -p a'), ['/x'])
        self.assertEqual(cli_cwds("echo 'cd /nope) &'; cd /x; claude -p a; claude -p b"), ['/x', '/x'])
        self.assertEqual(cli_cwds("cat <<'EOF'\n(cd /z) & claude -p no\nEOF\ncd /x; claude -p a"), ['/x'])
        self.assertEqual(cli_cwds('cd /x # && (cd /y)\nclaude -p a'), ['/x'])
        self.assertEqual(cli_cwds('claude -p a 2>&1 | tail -1; cd /x; claude -p b &>/dev/null; claude -p c >&2'), ['/w', '/x', '/x'])
        self.assertEqual(cli_cwds('cd /x \\\n && claude -p a'), ['/x'])                       # line continuation

    def test_nested_shell_c_is_another_process(self):
        self.assertEqual(cli_cwds('bash -c "cd /x && claude -p a"; claude -p b'), ['/x', '/w'])
        self.assertEqual(cli_cwds("bash -c 'cd /x; claude -p a' & claude -p b"), ['/x', '/w'])
        self.assertEqual(cli_cwds('cd /x; bash -c "cd /y && claude -p a"; claude -p b'), ['/y', '/x'])
        self.assertEqual(cli_cwds('eval "cd /x; claude -p a"; claude -p b'), ['/x', '/x'])   # eval runs in the same shell
        self.assertEqual(cli_cwds('bash -lc "cd /x && claude -p a"'), ['/x'])
        self.assertEqual(cli_cwds('(cd /x && eval "claude -p a")'), ['/x'])

    def test_unknown_stays_unknown(self):
        self.assertEqual(cli_cwds('cd $X && (claude -p a) & (claude -p b)'), [None, '/w'])
        self.assertEqual(cli_cwds('cd sub && (claude -p a)', None), [None])

    def test_relative_cd_follows_the_scope(self):
        self.assertEqual(cli_cwds('cd sub && (cd deeper; claude -p a) & claude -p b', '/w'), ['/w/sub/deeper', '/w'])


class CodexScope(unittest.TestCase):
    def test_same_rules_for_codex(self):
        self.assertEqual(cx_cwds('cd /x && (codex exec "p") & (codex exec "q"); wait'), ['/x', '/w'])
        self.assertEqual(cx_cwds('(cd /x && codex exec "p"); codex exec "q"'), ['/x', '/w'])
        self.assertEqual(cx_cwds('cd /x | codex exec "p"'), ['/w'])
        self.assertEqual(cx_cwds('cd /x && codex exec "p" & cd /y && codex exec "q"'), ['/x', '/y'])
        self.assertEqual(cx_cwds('{ cd /x; codex exec "p"; }; codex exec "q"'), ['/x', '/x'])
        self.assertEqual(cx_cwds('d=$(cd /x; pwd); codex exec "p"'), ['/w'])
        self.assertEqual(cx_cwds('echo "a & (b"; cd /x; codex exec "p"'), ['/x'])

    def test_cd_option_follows_shell_scope(self):
        L = server.cx_launches('cd /x && (codex exec -C sub "p") & (codex exec -C sub "q")', {}, '/w')
        self.assertEqual([(a['scwd'], a['cwd']) for a in L], [('/x', '/x/sub'), ('/w', '/w/sub')])

    def test_parse_call_keeps_scope(self):
        c = server.cx_parse_call(T0, 't1', {'command': 'cd /x && (codex exec "p") & (codex exec resume 019a0000-0000-7000-8000-000000000000 "q"); wait'}, '/w')
        self.assertEqual([L['scwd'] for L in c['L']], ['/x', '/w'])


def child_text(t, cwd, text):
    """A child record that starts with the given instruction: a call whose literal argument differs from the child's first instruction is not its parent."""
    return [st2.dump({'type': 'user', 'timestamp': st2.iso(t), 'cwd': cwd, 'message': {'role': 'user', 'content': text}})]


class CliLink(st2.CliFixture):
    """Two sessions started with `cd X && (A) & (B)` are both linked as children of that parent (A starts in X, B in the call's folder)."""
    C1, C2 = '22222222-2222-4222-8222-222222222222', '33333333-3333-4333-8333-333333333333'

    def test_both_children_linked(self):
        cmd = 'cd /x && (claude -p "$(cat $SP/a.md)" < /dev/null > a.json 2>&1; echo a) & (claude -p "$(cat $SP/b.md)" < /dev/null > b.json 2>&1; echo b); wait'
        self.write(self.PARENT, [st2.bash_line(T0, cmd, cwd='/w')])
        self.write(self.C1, st2.child_lines(T0 + 1, cwd='/x'))
        self.write(self.C2, st2.child_lines(T0 + 2, cwd='/w'))
        own = self.scan()
        self.assertEqual({k: v['sid'] for k, v in own.items()}, {self.C1: self.PARENT, self.C2: self.PARENT})
        self.assertEqual((own[self.C1]['cwd'], own[self.C2]['cwd']), ('/x', '/w'))

    def test_child_started_in_leaked_folder_is_not_linked(self):
        """In the shell B runs in /w; even if a session started in /x (an unrelated session) exists, it is not linked as a child of B."""
        cmd = 'cd /x && (claude -p a) & (claude -p b)'
        self.write(self.PARENT, [st2.bash_line(T0, cmd, cwd='/w')])
        self.write(self.C2, child_text(T0 + 2, '/x', 'a'))
        self.assertEqual(set(self.scan()), {self.C2})                # A (/x) takes it; there is no share left for B (/w)
        self.write('44444444-4444-4444-8444-444444444444', child_text(T0 + 3, '/w', 'b'))
        self.assertEqual(len(self.scan()), 2)


@unittest.skipUnless(shutil.which('bash'), 'bash 없음')
class MatchesRealBash(unittest.TestCase):
    """The working folder when a real bash calls `claude -p Pn` equals what shell_cwd computes (bash is the reference)."""
    CASES = [
        'cd {X} && (claude -p P1 >/dev/null 2>&1; echo "e $?") & (claude -p P2 >/dev/null 2>&1; echo "e $?"); wait',
        '(cd {X} && claude -p P1); claude -p P2',
        'cd {X} | claude -p P1; claude -p P2',
        'd=$(cd {X}; pwd); claude -p P1',
        '{{ cd {X}; claude -p P1; }}; claude -p P2',
        'cd {X}; claude -p P1 & claude -p P2',
        'cd {X} && claude -p P1 & cd {Y} && claude -p P2',
        "echo 'a & (b'; cd {X} && claude -p P1 & claude -p P2",
        'cd {X} &&\n claude -p P1 &\n claude -p P2',
        "cat <<'EOF'\n(cd {Z}) & claude -p no\nEOF\ncd {X}; claude -p P1",
        'claude -p P1 2>&1 | tail -1; cd {X}; claude -p P2 &>/dev/null; claude -p P3 >&2',
        'if cd {X}; then claude -p P1; fi; claude -p P2',
        'for i in 1 2; do cd {X}; claude -p P1; done & claude -p P2',
        'cat <(cd {X}; claude -p P1); claude -p P2',
        'cd {X}; (cd {Y}; (cd {Z}; claude -p P1) & claude -p P2); claude -p P3',
        'cd {X}; claude -p P1 | claude -p P2 | claude -p P3; claude -p P4',
        'cd {X} && {{ cd {Y} | claude -p P1; }} && claude -p P2',
        'x=`cd {X}; pwd`; claude -p P1; : "$(cd {Y}; claude -p P2)"; claude -p P3',
        'case x in x) cd {X}; claude -p P1;; esac; claude -p P2',
        'bash -c "cd {X} && claude -p P1"; claude -p P2',
        'eval "cd {X}; claude -p P1"; claude -p P2',
        '{{ cd {X}; claude -p P1; }} | cat; claude -p P2',
    ]

    def test_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            dirs = {k: os.path.join(tmp, k) for k in 'XYZ'}
            base = os.path.join(tmp, 'base')
            for d in list(dirs.values()) + [base]:
                os.mkdir(d)
            out = os.path.join(tmp, 'out')
            pre = 'claude() { cat > /dev/null; echo "$2 $PWD" >> "$OUT"; }\nexport -f claude\n'
            for case in self.CASES:
                cmd = case.format(**dirs)
                open(out, 'w').close()
                subprocess.run(['bash', '--norc', '-c', pre + cmd + '\nwait\n'], cwd=base, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30, env={'OUT': out, 'PATH': os.environ['PATH']})
                with open(out) as f:
                    real = dict(line.rstrip('\n').split(' ', 1) for line in f)
                names = re.findall(r'claude -p (P\d+)', cmd)
                self.assertEqual(sorted(real), sorted(names), cmd)                # every `claude -p` really ran
                self.assertEqual(dict(zip(names, cli_cwds(cmd, base))), real, cmd)


if __name__ == '__main__':
    unittest.main()
