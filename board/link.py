"""Links what a Claude session started with Bash to that session: Codex runs (cx_launches · cx_link) and claude -p child sessions (LinkIndex).
When Bash runs a script file (`bash file` · `source file` · `./file`), the runs inside that file are judged by the same rules (bash_scripts, one level only).
The firmest evidence is the process lineage (board/lineage.py): a link made once while the process was alive comes before the rules above."""

import array
import ast
import collections
import glob
import itertools
import json
import os
import re
import shlex
import stat
import bisect
import threading
import time
import types
import warnings

from . import affil, facts
from . import fingerprint as fp
from .facts import NODE_ID_RE, Redirect, Span
from .util import PROJECTS, SID_RE, line_error, open_regular, open_safe, parse_ts, short_path, stat_plain
from .codex_parse import CX_PROMPT_MIN, CX_WINDOW, cx_alive, cx_open, cx_procs
from .codex_index import CODEX
from .lineage import Lineage


# How certain a link rule is: a proper sub-agent, the process lineage (proc), an environment variable (env), a record kept in a file (file), a Codex instruction match (prompt) and the id (id) are firm,
# while command parsing + time · cwd (time), Codex's time (time) and "only this session at that time" (session) are estimates.
CERTAIN_RULES = frozenset(facts.CERTAIN_RULES) | frozenset(('prompt', 'id'))     # ranks 1-3 (+ the Codex rules `prompt` and `id`)
UNLINKED_MAX = 20            # cap on the missed candidates one session gives
UNLINKED_DAYS = 14           # a candidate that started longer ago than this is not looked for


def certain(rule):
    return rule in CERTAIN_RULES


def link_brief(a):
    """Link info of one agent {rule, certain}. A proper sub-agent is subagent, claude -p uses the rule of its cli ownership info, Codex uses the rule of its link."""
    if a.provider == 'codex':
        rule = (a.link or {}).get('rule')
    elif a.origin == 'cli':
        rule = (a.cli or {}).get('rule') or 'time'
    else:
        rule = 'subagent'
    return {'rule': rule, 'certain': certain(rule)}


CLI_WINDOW = 60            # only the missed-candidate list (`unlinked`) still looks this far back from a child's start; linking no longer has a window (it goes by the calls still running)
CX_VAR_RE = re.compile(r'\$\([^)]*\)|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$\d')
CX_OPT_ARG = {'-C', '--cd', '--add-dir', '-s', '--sandbox', '-m', '--model', '-c', '--config', '-o',
              '--output-last-message', '-p', '--profile', '-i', '--image', '--output-schema', '--color',
              '--enable', '--disable', '--thread-source', '--local-provider'}


def cx_prompt_rx(q):
    """A regex made by turning $VAR · ${…} · $(…) in an instruction into named lazy groups, the list of groups, and the number of characters outside variables."""
    names, parts, last = [], [], 0
    for m in CX_VAR_RE.finditer(q):
        parts.append(re.escape(q[last:m.start()]))
        g = 'v%d' % len(names)
        names.append((g, re.sub(r'[^A-Za-z0-9_]', '', m.group(0))))
        parts.append('(?P<%s>.*?)' % g)
        last = m.end()
    parts.append(re.escape(q[last:]))
    try:
        rx = re.compile(''.join(parts), re.S)
    except re.error:
        return None, [], 0
    return rx, names, len(CX_VAR_RE.sub('', q))


def _cx_expand(s, env):
    if s is None:
        return None
    s = s.strip('\'"')
    s = CX_VAR_RE.sub(lambda m: env.get(re.sub(r'[^A-Za-z0-9_]', '', m.group(0)), m.group(0)), s)
    return None if '$' in s else os.path.expanduser(s)


CX_CD_ARG_RE = re.compile(r'[ \t]+([^\s;&|)]+)')          # the argument after `cd` (does not cross a newline). Shared by Codex and claude -p
# tokens that split the masked code for shell_cwd: openers of a subshell ($( <( >( ( `), list and pipe separators (&& || |& ; & | newline), redirections (2>&1 &> …) are not separators, words
CX_TOK_RE = re.compile(r'\$\(|[<>]\(|&&|\|\||\|&|;;?&?|&>>?|[<>]&|[&|()`\n]|[<>]|[ \t\r]+|(?:[^\s;&|()<>`$]|\$(?!\())+|\$')
CX_HEREDOC_RE = re.compile(r'<<(-?)[ \t]*(?:\'([^\']*)\'|"([^"]*)"|\\?([A-Za-z_][A-Za-z0-9_]*))')
CX_DQ_SPECIAL_RE = re.compile(r'[\\"$`]')       # characters handled specially inside double quotes
CX_SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
CX_ASSIGN_RE = re.compile(r'[A-Za-z_]\w*\+?=')
CX_REDIR_RE = re.compile(r'\d*(?:>>?|<<?|>&|<&|&>)')
CX_KEYWORDS = {'then', 'do', 'else', 'elif', 'if', 'while', 'until', '!', '{'}     # even when one comes before a command, what follows is in command position
CX_DURATION = r'\d+(?:\.\d+)?[smhd]?|\$\{?\w+\}?'
CX_NAME = r'[\w.@][\w.@-]*'
# signals for timeout -s: known names (with or without the `SIG` prefix, upper case) and 0–31 (an unknown name or one out of range makes timeout exit 125 without running)
CX_SIGNAL = (r'(?:SIG)?(?:HUP|INT|QUIT|ILL|TRAP|ABRT|IOT|BUS|FPE|KILL|USR1|SEGV|USR2|PIPE|ALRM|TERM|STKFLT|CHLD|CLD|CONT|STOP|TSTP|TTIN|TTOU'
             r'|URG|XCPU|XFSZ|VTALRM|PROF|WINCH|IO|POLL|PWR|SYS)|[12]?\d|3[01]')
# prefixes that **run the following command as it is**, and the options of those that are known not to change the execution (a closed list).
# flags: options without a value / args: options that take a value (a regex for the value's shape, `--long=value` also allowed) / npos: a positional argument after the options (a duration) / assign: `VAR=x` allowed after the options
# If there is even one option that is not in the list (an unknown one such as `--help`, `--version`, `command -v`, `-V`, `env -S`), we do not know that the execution is kept, so the inside is not opened.
CX_PREFIX_CMDS = {
    'env': {'flags': {'-i', '--ignore-environment'}, 'args': {'-u': CX_NAME, '--unset': CX_NAME, '-C': r'[^\s-]\S*', '--chdir': r'[^\s-]\S*'},
            'assign': True},
    'timeout': {'flags': {'--preserve-status', '--foreground'},
                'args': {'-k': CX_DURATION, '--kill-after': CX_DURATION, '-s': CX_SIGNAL, '--signal': CX_SIGNAL}, 'npos': CX_DURATION},
    'command': {'flags': {'-p'}},
    'nice': {'args': {'-n': r'-?\d+', '--adjustment': r'-?\d+'}, 'numflag': True},
    'nohup': {}, 'exec': {}, 'time': {'flags': {'-p'}},
    'setsid': {'flags': {'-c', '-f', '-w', '--ctty', '--fork', '--wait'}},
    'sudo': {'flags': {'-E', '-H', '-n'}, 'args': {'-u': CX_NAME}, 'assign': True},
    'ionice': {'flags': {'-t', '--ignore'}, 'args': {'-c': r'[0-3]|none|realtime|best-effort|idle', '-n': r'[0-7]', '--class': r'[0-3]|none|realtime|best-effort|idle', '--classdata': r'[0-7]'}},
    'stdbuf': {'attached': True, 'args': {'-i': r'L|\d+[A-Za-z]*', '-o': r'L|\d+[A-Za-z]*', '-e': r'L|\d+[A-Za-z]*', '--input': r'L|\d+[A-Za-z]*', '--output': r'L|\d+[A-Za-z]*', '--error': r'L|\d+[A-Za-z]*'}},
}
CX_SHELL_SHORT_RE = re.compile(r'-[leuxic]+')                       # -l -e -u -x -i and their bundles (+ c: -lc, -ec)
# long options are accepted only by bash (dash and sh exit 2 with `Illegal option --`; zsh and ksh are not opened because we could not confirm that they accept them)
CX_SHELL_LONG = {'bash': {'--login', '--noprofile', '--norc'}, 'zsh': set(), 'ksh': set(), 'sh': set(), 'dash': set()}
# values of `-o` · `+o` that leave the execution as it is (a closed list, per shell). `noexec` (only reads commands without running them) and unknown values are outside.
# names the shell does not know are not included, since the shell ends with an error and does not run (`posix`, `errtrace`, `functrace` only for bash; `pipefail` is unknown to older dash)
CX_SHELL_OPTIONS = {'bash': {'pipefail', 'errexit', 'nounset', 'xtrace', 'verbose', 'noclobber', 'posix', 'errtrace', 'functrace'},
                    'zsh': {'pipefail', 'errexit', 'nounset', 'xtrace', 'verbose', 'noclobber'},
                    'ksh': {'pipefail', 'errexit', 'nounset', 'xtrace', 'verbose', 'noclobber'},
                    'sh': {'errexit', 'nounset', 'xtrace', 'verbose', 'noclobber'},
                    'dash': {'errexit', 'nounset', 'xtrace', 'verbose', 'noclobber'}}


def _skip_prefix(name, w, k, ops=None):
    """Position after skipping the prefix `name` (w[k]) and its options. None if any option is outside the closed list. `ops`: a list that gets what the prefix does to the environment of the
    command it runs, in order: ('set', NAME, value or None when it is no literal), ('unset', NAME), ('clear',) (`env -i`, and `sudo` that gives the command a clean one)."""
    spec = CX_PREFIX_CMDS[name]
    flags, args = spec.get('flags', ()), spec.get('args', {})
    if ops is not None and name == 'sudo':
        ops.append(('clear',))                                      # the environment of a command sudo runs is the one sudo makes (a variable of the caller does not reach it)
    k += 1
    while k < len(w) and w[k].startswith('-') and w[k] != '-':
        t = w[k]
        if t == '--':
            k += 1
            break
        opt, _, val = t.partition('=')
        if t in flags:
            k += 1
            if ops is not None and name == 'env' and t in ('-i', '--ignore-environment'):
                ops.append(('clear',))
        elif t in args:
            if k + 1 >= len(w) or w[k + 1] is None or not re.fullmatch(args[t], w[k + 1]):
                return None
            if ops is not None and name == 'env' and t in ('-u', '--unset'):
                ops.append(('unset', w[k + 1]))
            k += 2
        elif val and opt.startswith('--') and opt in args and re.fullmatch(args[opt], val):
            k += 1                                                   # only `--long=value`. The `=` of a short option such as `-u=FOO` is part of the value, so it does not run
            if ops is not None and name == 'env' and opt == '--unset':
                ops.append(('unset', val))
        elif spec.get('attached') and len(t) > 2 and not t.startswith('--') and t[:2] in args and re.fullmatch(args[t[:2]], t[2:]):
            k += 1                                                   # the value written against its option (`stdbuf -oL`): only for the prefix that is written so
        elif spec.get('numflag') and re.fullmatch(r'-\d+', t):
            k += 1
        else:
            return None
    while spec.get('assign') and k < len(w) and w[k] is not None and CX_ASSIGN_RE.match(w[k]):
        if ops is not None and name == 'env':
            nm, _, value = w[k].partition('=')
            ops.append(('set', nm, None if nm.endswith('+') or '$' in value or '`' in value else value))
        k += 1
    if spec.get('npos'):
        if k >= len(w) or w[k] is None or not re.fullmatch(spec['npos'], w[k]):
            return None
        k += 1
    return k


def skip_prefixes(words, k=0):
    """(k, ok, ops) for the words of a simple command, from `words[k]`: the place of the command word after what leads it (`VAR=x` assignments and prefix commands with their options: `env -u A`, `timeout -s KILL 5`,
    `nice -n 5`, `stdbuf -oL` ... the closed list CX_PREFIX_CMDS the shell -c reader uses), whether every option in front of it was one of the list (`ok`; when it was not, k is the place after the word
    of the prefix that could not be read), and what they do to the environment, in order (`_skip_prefix`'s `ops`)."""
    ops = []
    while k < len(words):
        w = words[k]
        if w is None:
            return k, False, ops
        if CX_ASSIGN_RE.match(w):
            nm, _, value = w.partition('=')
            ops.append(('set', nm, None if nm.endswith('+') or '$' in value or '`' in value else value))
            k += 1
            continue
        name = w.rsplit('/', 1)[-1]
        if name in CX_PREFIX_CMDS:
            got = _skip_prefix(name, words, k, ops)
            if got is None:
                return k + 1, False, ops
            k = got
            continue
        break
    return k, True, ops


CODEX_VALUE_OPTS = frozenset(('-c', '--config', '-m', '--model', '-s', '--sandbox', '-a', '--ask-for-approval', '-C', '--cd', '-p', '--profile', '-i', '--image', '--add-dir', '--enable', '--disable',
                              '--local-provider', '--thread-source'))     # the options of `codex` (before its subcommand) that take a value


def codex_sub(rest):
    """The subcommand of `codex` from the words after its name: the first word that is no option and no value of one (`exec` and its alias `e` start a thread; `-c key=value`, `-m model` and the like
    stand before it). None when there is none or an option cannot be told from its value."""
    j = 0
    while j < len(rest):
        t = rest[j]
        if t is None:
            return None
        if t.startswith('-') and t != '-':
            j += 1 if ('=' in t or t not in CODEX_VALUE_OPTS) else 2
        else:
            return t
    return None


def _prefix_regex(name, spec):
    """The regex of a prefix command and its options as the table CX_PREFIX_CMDS writes them (the same list `_skip_prefix` reads word by word)."""
    alts = [re.escape(f) for f in sorted(spec.get('flags', ()))]
    for opt, val in spec.get('args', {}).items():
        alts.append(re.escape(opt) + (r'(?:=(?:%s)|\s+(?:%s))' % (val, val) if opt.startswith('--') else r'\s*(?:%s)' % val))
    if spec.get('numflag'):
        alts.append(r'-\d+')
    rx = re.escape(name) + (r'(?:\s+(?:%s))*' % '|'.join(alts) if alts else '')
    if spec.get('assign'):
        rx += r'(?:\s+[A-Za-z_]\w*=\S*)*'
    if spec.get('npos'):
        rx += r'\s+(?:%s)' % spec['npos']
    return rx


# What stands in front of a launch: the prefix commands with the options of the closed list (`env -u A`, `timeout -s KILL 5`, `nice -n 5`, `stdbuf -oL` ...), and the shapes the launch reader always took
# (`timeout` and any one word, `env` and its few options), which stay as they were.
_PREFIX_RX = '(?:%s|timeout\\s+\\S+|nohup|setsid|time|exec|env(?:\\s+(?:-i|--ignore-environment|-u\\s*\\S+|[A-Za-z_]\\w*=\\S*))*)' % '|'.join(_prefix_regex(n, sp) for n, sp in CX_PREFIX_CMDS.items())
# the start of a command: a separator, then `{` (a group), `do` and `then` (reserved words only in command position: the `then` in `echo then codex exec …` and the `{` in `echo { codex exec …` are arguments, so they are no command boundary),
# and the words `NAME=value` in front of the command word (assignments of the shell: what follows them is still the command position, as it is after `env NAME=value`)
_LAUNCH_HEAD = r'(?:^|[;&|(`]|\$\()\s*(?:(?:do|then|\{)\s+)*(?:[A-Za-z_]\w*=\S*\s+)*(?:' + _PREFIX_RX + r'\s+)*'
# `codex` and the options before its subcommand (`codex -c model=x exec …`), then `exec` or its alias `e`, in command position (not text inside a string or heredoc, pgrep, or --help)
_CX_SHORT_VALUES = ''.join(sorted(o[1] for o in CODEX_VALUE_OPTS if not o.startswith('--')))
_CX_LONG_VALUES = '|'.join(sorted(o[2:] for o in CODEX_VALUE_OPTS if o.startswith('--')))
# an option that takes a value takes the next word: `-m e` is no `e`; the options that take none are the others
_CODEX_OPT = r'(?:-[%s]\s*\S+|--(?:%s)(?:=\S+|\s+\S+)|--(?!(?:%s)(?![\w-]))[\w-]+(?:=\S+)?|-(?![%s])[A-Za-z])' % (_CX_SHORT_VALUES, _CX_LONG_VALUES, _CX_LONG_VALUES, _CX_SHORT_VALUES)
LAUNCH_RE = re.compile(_LAUNCH_HEAD + r'codex(?:\s+' + _CODEX_OPT + r')*\s+(?:exec|e)(?![\w-])(?!\s+--help)', re.M)
# Claude Code started from Bash (`claude -p …`): a claude in command position followed by -p/--print. A session like that is a child agent of the session that started it
CLAUDE_LAUNCH_RE = re.compile(_LAUNCH_HEAD + r'claude\b(?=[^;&|\n]*?\s(?:-p|--print)\b)', re.M)


def _shell_c_operand(shell, rest):
    """If the words after a shell name are only options from the closed list (-l -e -u -x -i and their bundles, the long options of CX_SHELL_LONG[shell], `-o` · `+o` with a value of CX_SHELL_OPTIONS[shell])
    and one of them is `-c` (including the bundles `-lc` · `-ec`), the very next text is the command text. False if there is an option outside the list (`--help` · `-v` · `-O` · `-o noexec` …)."""
    seen_c, j = False, 0
    while j < len(rest):
        t = rest[j]
        if t == '--':
            j += 1
            break
        if t in ('-o', '+o'):
            if j + 1 >= len(rest) or rest[j + 1] not in CX_SHELL_OPTIONS[shell]:
                return False
            j += 2
        elif CX_SHELL_SHORT_RE.fullmatch(t):
            seen_c = seen_c or 'c' in t
            j += 1
        elif t in CX_SHELL_LONG[shell]:
            j += 1
        elif t.startswith(('-', '+')):
            return False
        else:
            break
    return seen_c and j >= len(rest)


def _code_arg(words):
    """From the (masked) words before a quote in the same simple command, whether that quoted text is really shell text (the first argument of eval gives 'eval', the command text of a shell -c gives 'sh'), else False.
    It is so only when the first word after skipping `VAR=x` assignments before the command, keywords such as `then` and `do`, and prefixes (`env` · `nohup` · `timeout N` · `exec` · `command` · `time` · `nice` · `sudo` · `setsid`)
    is `eval` or a shell (bash · sh · zsh · dash · ksh), and the prefixes and shell options skipped must all be from the **closed list**
    (`command -v bash -c '…'` and `env --help bash -c '…'` do not run). A quote that is the argument of another command, as in `echo 'bash -c …'`, `printf … bash -c '…'` or `git commit -m …`,
    is text. eval runs its arguments joined into one command, so the text is a command only when the quote is the **first argument** of eval
    (`eval echo '…'` is `echo …`, so text. Combinations of later arguments are not opened: not opening is the safe side)."""
    w, k, started = list(words), 0, False
    while k < len(w):
        t = w[k]
        if not started and (t in CX_KEYWORDS or CX_ASSIGN_RE.match(t)):
            k += 1                                     # keywords and `VAR=x` assignments only at the very front of a command (`timeout 5 VAR=1 bash` tries to run VAR=1 and fails)
        elif CX_REDIR_RE.match(t):
            k += 2 if CX_REDIR_RE.fullmatch(t) else 1
        elif t.rsplit('/', 1)[-1] in CX_PREFIX_CMDS:
            k, started = _skip_prefix(t.rsplit('/', 1)[-1], w, k), True
            if k is None:
                return False
        else:
            break
    if k >= len(w):
        return False
    name = w[k].rsplit('/', 1)[-1]
    if name == 'eval':
        return 'eval' if w[k + 1:] in ([], ['--']) else False
    return 'sh' if name in CX_SHELLS and _shell_c_operand(name, w[k + 1:]) else False


SHELL_CODE_KEEP_TEXT = 64 << 10   # the masked text of a command is remembered for a while (one command goes through shell_code several times when it is read), when the command is this short ...
SHELL_CODE_KEEP = 4 << 20         # ... and all the remembered commands together are about this many bytes (the command and its masked text)
_SHELL_CODE = collections.OrderedDict()
_SHELL_CODE_BYTES = [0]
_SHELL_CODE_LOCK = threading.Lock()
CODE_ARG_SPAN = 8192             # how much text before a quote (in its simple command) is read to see whether the quote is shell text


def shell_code(cmd):
    """In text of the same length as cmd, masks with 'x' the places that are not executed (text inside quotes, heredoc bodies, comments). It does not imitate the shell.
    A $(…) or `…` inside double quotes is execution, so it stays; the inside of the quotes of bash -c '…', sh -c "…" and eval '…' is a command too, so it stays, but the quoting, heredocs,
    comments and escapes inside it are masked again. A shell -c is another process, so the opening quote is changed to `(` and the closing quote to `)` (so a cd inside does not reach outside. For eval, `;`).
    Escaped characters (\\( \\; …) are masked too. If the shape cannot be resolved it returns **everything masked** (no ground for execution): a character that could not be interpreted is not treated as execution."""
    if len(cmd) <= SHELL_CODE_KEEP_TEXT:
        with _SHELL_CODE_LOCK:
            hit = _SHELL_CODE.get(cmd)
            if hit is not None:
                _SHELL_CODE.move_to_end(cmd)
                return hit
    try:
        got = _shell_code(cmd)
    except Exception:   # noqa: BLE001 — if it cannot be resolved it is not used as ground for execution
        got = re.sub(r'[^\n]', 'x', cmd)
    if len(cmd) <= SHELL_CODE_KEEP_TEXT:
        with _SHELL_CODE_LOCK:
            _SHELL_CODE[cmd] = got
            _SHELL_CODE_BYTES[0] += 2 * len(cmd)
            while _SHELL_CODE_BYTES[0] > SHELL_CODE_KEEP and _SHELL_CODE:
                old, _ = _SHELL_CODE.popitem(last=False)
                _SHELL_CODE_BYTES[0] -= 2 * len(old)
    return got


SCAN_STOP_RE = re.compile(r'[\\\'"`$#()<\n]')            # the characters `_shell_code` looks at: the others are text


def _shell_code(cmd):
    n, out, pending = len(cmd), list(cmd), []
    last_scan = [0, 0]           # [where the last code_quote scan began, where the simple command it looked at starts]: what lies before that position is final

    def mask(a, b):
        if a < b:
            out[a:b] = re.sub(r'[^\n]', 'x', ''.join(out[a:b]))

    def qmask(a, b):
        """Masks quoted text: all of it, a line break too (a line break inside quotes is text and ends no command; the line breaks that stay in the masked code are the ones the shell reads as breaks)."""
        if a < b:
            out[a:b] = 'x' * (b - a)

    def code_quote(i):
        """Whether the opening quote at position i is really the eval argument or the shell -c command text at a command position (looking similar is not enough: echo 'eval …' is text).
        If so, the inside is analysed again as shell text, and the opening quote is replaced with `;` (eval: the same shell) or `(` (shell -c: another process; the caller turns the closing quote into `)`)
        so that the first command inside is also seen at a command position. Returns 'eval' or 'sh' (else False)."""
        if i > 0 and cmd[i - 1] not in ' \t':
            return False
        # Where the simple command before the quote starts: the nearest separator before it. The text before an opening quote is already final, so a quote that follows
        # an earlier one with no separator between them shares its start (a long line with thousands of quotes is not scanned back to its start again for each).
        lo = last_scan[0] if last_scan[0] <= i else 0
        k = i
        while k > lo and out[k - 1] not in ';&|\n(`':
            k -= 1
        if k == lo and lo == last_scan[0] and lo > 0:
            k = last_scan[1]
        last_scan[0], last_scan[1] = i, k
        if i - k > CODE_ARG_SPAN:
            return False                  # nobody writes eval or `bash -c` after this much text in one simple command; a long line of data is not read word by word for each quote
        kind = _code_arg(''.join(out[k:i]).split())
        if kind:
            out[i] = '(' if kind == 'sh' else ';'
        return kind

    def heredocs(i):
        """i is the newline position. Masks the body of the pending heredocs and returns the start position of the next line."""
        pos = i + 1
        for word, dash in pending:
            start = pos
            while pos < n:
                end = cmd.find('\n', pos)
                end = n if end < 0 else end
                line = cmd[pos:end].rstrip('\r')
                if (line.lstrip('\t') if dash else line) == word:
                    mask(start, pos)
                    pos = min(end + 1, n)
                    break
                pos = min(end + 1, n)
            else:
                mask(start, n)
        del pending[:]
        return pos

    def dquote(i):
        """Inside double quotes (i is just after the opening quote). The position after the closing quote."""
        kind = code_quote(i - 1)
        if kind:
            j = i
            while j < n and cmd[j] != '"':
                j += 2 if cmd[j] == '\\' else 1
            j = min(j, n)
            nested(i, j, True)
            if kind == 'sh' and j < n:
                out[j] = ')'
            return min(j + 1, n)
        while i < n:
            m = CX_DQ_SPECIAL_RE.search(cmd, i)
            j = m.start() if m else n
            qmask(i, j)                                # a stretch with no special characters is masked all at once
            if not m:
                return n
            i, ch = j, cmd[j]
            if ch == '\\':
                qmask(i, min(i + 2, n))
                i += 2
            elif ch == '"':
                return i + 1
            elif ch == '$' and cmd.startswith('$(', i):
                i = scan(i + 2, ')') + 1
            elif ch == '`':
                i = scan(i + 1, '`') + 1
            else:
                qmask(i, i + 1)
                i += 1
        return n

    def nested(a, b, unescape):
        """The inside [a, b) of the quotes of bash -c '…', sh -c "…" and eval '…' is also shell text, so the quoting, heredocs, comments and escapes in it are masked again.
        Inside double quotes the backslashes of \\" \\\\ \\$ \\` are undone before looking (put back at the pre-undo positions to keep the same length)."""
        if not unescape:
            out[a:b] = list(_shell_code(cmd[a:b]))
            return
        chars, pos, k = [], [], a
        while k < b:
            if cmd[k] == '\\' and k + 1 < b and cmd[k + 1] in '"\\$`':
                out[k] = ' '
                chars.append(cmd[k + 1])
                pos.append(k + 1)
                k += 2
            else:
                chars.append(cmd[k])
                pos.append(k)
                k += 1
        for c, at in zip(_shell_code(''.join(chars)), pos):
            out[at] = c

    def scan(i, closer):
        depth = 0
        while i < n:
            m = SCAN_STOP_RE.search(cmd, i)                # (a stretch of ordinary characters is passed at once)
            if m is None:
                return n
            i = m.start()
            ch = cmd[i]
            if ch == '\\':
                if i + 1 < n and cmd[i + 1] == '\n':
                    out[i] = out[i + 1] = ' '      # a line continuation is like a space joining into one line (the head of the next line does not become a command position)
                else:
                    mask(i, min(i + 2, n))         # an escaped character (\( \; \" …) is plain text, not shell syntax
                i += 2
            elif ch == "'":
                j = cmd.find("'", i + 1)
                j = n if j < 0 else j
                kind = code_quote(i)
                if kind:
                    nested(i + 1, j, False)
                    if kind == 'sh' and j < n:
                        out[j] = ')'
                else:
                    qmask(i + 1, j)
                i = j + 1
            elif ch == '"':
                i = dquote(i + 1)
            elif ch == '`':
                if closer == '`':
                    return i
                i = scan(i + 1, '`') + 1
            elif ch == '$' and cmd.startswith('$(', i):
                i = scan(i + 2, ')') + 1
            elif ch == '#' and (i == 0 or cmd[i - 1] in ' \t\n;&|('):
                j = cmd.find('\n', i)
                j = n if j < 0 else j
                mask(i, j)
                i = j
            elif ch == '(' and closer == ')':
                depth += 1
                i += 1
            elif ch == ')' and closer == ')':
                if depth == 0:
                    return i
                depth -= 1
                i += 1
            elif ch == '<' and cmd.startswith('<<', i) and not cmd.startswith('<<<', i):
                m = CX_HEREDOC_RE.match(cmd, i)
                if m:
                    pending.append((m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4), bool(m.group(1))))
                    i = m.end()
                else:
                    i += 2
            elif ch == '\n' and pending:
                i = heredocs(i)
            else:
                i += 1
        return n

    scan(0, None)
    return ''.join(out)


def shell_cwd(cmd, code, upto, assigns, base_cwd):
    """The working folder in which the command starting at `upto` (the start of a LAUNCH_RE match: the separator before it, `;&|(` or `$(`, belongs to that command too) runs in the shell.
    Splits the masked code (code = shell_code(cmd), the same length) into tokens and follows only the range a `cd` reaches as in a shell. A relative `cd` is resolved against the working folder before it,
    and if it cannot be resolved (a variable, `cd -`, the earlier one unknown) None. The `cd` position is found in code and the argument is read from cmd (text inside quotes and heredocs is already masked).
    Range: when `( … )`, `$( … )`, backticks or `<( … )` close, the cd inside disappears. A list ending with `&` (`cd X && A &`) is a subshell running in the background, so a cd inside it does not reach later commands
    (it reaches the A inside the list). Each part of a pipe (`|`) is a subshell too, so a cd inside it reaches neither the other parts nor what comes after.
    The same shell joined by `;`, newline, `&&` and `||`, and the inside of `{ …; }`, if, for, while and case carry on (it does not imitate the shell: conditions are not weighed)."""
    end = upto + (2 if code.startswith('$(', upto) else 1 if code[upto:upto + 1] in (';', '&', '|', '(', '`') else 0)
    cwd = list0 = pipe0 = base_cwd     # the current working folder · this list (where to go back if closed by `&`) · the start folder of this pipeline (where each part forked off)
    piped = cont = False              # a pipe is continuing · what is before is && || | so a newline does not end the list
    cmdpos = True                     # whether this is a command position (a place where a word can be a command name)
    stack = []                        # open subshells ('sub' parenthesis, 'bt' backtick) and groups in the same shell ('grp' { if for while case)
    for m in CX_TOK_RE.finditer(code, 0, end):
        t = m.group()
        if t[0] in ' \t\r':
            continue
        if t == '\n':
            if cont:
                continue
            t = ';'
        if t in ('$(', '<(', '>(', '(') or (t == '`' and not (stack and stack[-1][0] == 'bt')):
            stack.append(('bt' if t == '`' else 'sub', cwd, list0, pipe0, piped, cmdpos))
            list0 = pipe0 = cwd
            piped, cmdpos, cont = False, True, False
        elif (t == ')' and stack and stack[-1][0] == 'sub') or t == '`':      # a backtick, if not opened above, closes the open backtick
            _, cwd, list0, pipe0, piped, cmdpos = stack.pop()             # a cd inside a subshell disappears when it is closed
            cont = False
        elif t == ')':                                                    # the closing parenthesis of a case pattern: a new list from the next command
            piped, cmdpos, cont = False, True, False
            list0 = pipe0 = cwd
        elif t[0] == ';' or t == '&':
            if piped:
                cwd, piped = pipe0, False
            if t == '&':
                cwd = list0                                               # a subshell running in the background: a cd inside it does not reach later commands
            list0 = pipe0 = cwd
            cmdpos, cont = True, False
        elif t in ('&&', '||'):
            if piped:
                cwd, piped = pipe0, False
            pipe0 = cwd
            cmdpos = cont = True
        elif t in ('|', '|&'):
            cwd, piped = pipe0, True                                      # each part of a pipe is a subshell forked from the start folder
            cmdpos = cont = True
        elif t[0] in '<>&':
            continue                                                      # redirection
        else:
            cont = False
            if not cmdpos:
                continue
            if t in ('cd', 'pushd', 'popd'):
                a = CX_CD_ARG_RE.match(cmd, m.end())
                if t == 'popd' or (t == 'pushd' and not a):
                    cwd = None                                                # back to a folder the directory stack holds / a swap: not followed
                elif a:
                    p = _cx_expand(a.group(1), assigns)
                    if p is None or p == '-' or (t == 'pushd' and p.startswith(('+', '-'))):
                        cwd = None
                    elif os.path.isabs(p):
                        cwd = p
                    else:
                        cwd = os.path.normpath(os.path.join(cwd, p)) if cwd and os.path.isabs(cwd) else None
                cmdpos = False
            elif t in ('{', 'if', 'while', 'until', 'for', 'select', 'case'):
                stack.append(('grp', cwd, list0, pipe0, piped, cmdpos))
                list0 = pipe0 = cwd
                piped, cmdpos = False, t in CX_KEYWORDS
            elif t in ('}', 'fi', 'done', 'esac'):
                if stack and stack[-1][0] == 'grp':
                    _, _, list0, pipe0, piped, _ = stack.pop()            # the same shell, so the cd stays, and only the starts of the list and pipe go back to the outer ones
                cmdpos = False
            elif t not in CX_KEYWORDS and not CX_ASSIGN_RE.match(t):
                cmdpos = False
    return cwd


# ---------- loops ----------
# A single line `for x in a b c; do … claude -p … & done` starts three sessions: not one session per textual occurrence (one place of execution). A run inside a loop body
# can get as many sessions as the loop count. If the count is known (`for x in a b c`, `{1..5}`) that number, if unknown (`while` · `until` · `$(…)` · a variable · a glob) LOOP_UNKNOWN, nested loops are multiplied, up to LOOP_MAX.
LOOP_UNKNOWN = affil.UNKNOWN_COUNT
LOOP_MAX = 64
LOOP_FOR_RE = re.compile(r'\s*[A-Za-z_]\w*\s+in\b(.*)', re.S)
LOOP_BRACE_RE = re.compile(r'\{(-?\d+)\.\.(-?\d+)\}|\{([^{}$`\s,]*(?:,[^{}$`\s,]*)+)\}')
LOOP_OPEN_WORDS = {'then', 'do', 'else', 'elif', 'if', 'while', 'until', '!', '{'}      # what follows these is also command position


def _loop_count(header):
    """The loop count of a `for` header (the text from after `for` to before `do`). LOOP_UNKNOWN if unknown."""
    m = LOOP_FOR_RE.match(header)
    if not m:
        return LOOP_UNKNOWN                      # `for x; do` (positional parameters) · `for ((i=0; …))`
    n = 0
    for w in _cmd_words(m.group(1), None):
        if re.search(r'[$`*?\[\]~]', w):
            return LOOP_UNKNOWN
        b = LOOP_BRACE_RE.fullmatch(w)
        if b and b.group(1) is not None:
            n += abs(int(b.group(2)) - int(b.group(1))) + 1
        elif b:
            n += b.group(3).count(',') + 1
        elif '{' in w or '}' in w:
            return LOOP_UNKNOWN
        else:
            n += 1
    return max(n, 1)


def loop_factor(text, code, upto):
    """The product (at least 1, at most LOOP_MAX) of the loop counts of the `for`/`while`/`until` loops enclosing that position (inside the body), when read up to text[:upto].
    The pairing of `for … do … done` is counted with the tokens of the masked code (code): text inside quotes, heredocs and comments is already masked."""
    stack = []                                   # open loops: [start position (header), inside the body?, loop count]
    cmdpos = True
    for m in CX_TOK_RE.finditer(code, 0, upto):
        t = m.group()
        if t[0] in ' \t\r':
            continue
        if t in ('$(', '<(', '>(', '(', '`', ')', '\n', '&&', '||', '|&') or t[0] in ';&|':
            cmdpos = True
        elif t[0] in '<>&':
            continue
        elif cmdpos and t in ('for', 'select'):
            stack.append([m.end(), False, LOOP_UNKNOWN])
            cmdpos = False
        elif cmdpos and t in ('while', 'until'):
            stack.append([m.end(), True, LOOP_UNKNOWN])         # the condition is unknown, so the count is unknown too
            cmdpos = True
        elif cmdpos and t == 'do':
            for loop in reversed(stack):
                if not loop[1]:
                    loop[1], loop[2] = True, _loop_count(text[loop[0]:m.start()])
                    break
            cmdpos = True
        elif cmdpos and t == 'done':
            if stack:
                stack.pop()
            cmdpos = False
        else:
            cmdpos = cmdpos and t in LOOP_OPEN_WORDS
    n = 1
    for _, body, k in stack:
        if body:
            n = min(n * k, LOOP_MAX)
    return n


# ---------- script files ----------
# When a Bash command runs a script file **in command position** (`bash file` · `sh file` · `source file` · `. file` · `./file` · running an absolute or relative path, or a path that starts with a variable
# the same command sets to a literal: `R=/x/runs; $R/run.sh`), the runs inside that file are looked at too.
# One level only (a script that calls a script is not followed). The file is opened under the same policy as the document view (open_safe: regular files only, dot paths and secret names refused).
SCRIPT_MAX = 256 << 10        # a script bigger than this is not looked at
SCRIPT_RUNS_MAX = 8           # cap on the number of scripts followed from one command
SCRIPT_TAIL = 4096            # cap on the text from which the file name and arguments are read (the rest of the command is not split whole)
SCRIPT_RUN_RE = re.compile(
    r'(?:^|[;&|(`]|\$\()\s*(?:(?:do|then|else|if|elif|while|until|!|\{)\s+)*(?:[A-Za-z_]\w*=\S*\s+)*'
    r'(?:(?:timeout\s+\S+|nohup|setsid|time|exec|env(?:\s+[A-Za-z_]\w*=\S*)*)\s+)*'
    r'(?:(?P<sh>(?:[^\s;&|()<>`$\'"]*/)?(?:bash|sh|zsh|dash|ksh))\s+|(?P<src>source|\.)\s+|(?=(?:\$\{?[A-Za-z_]\w*\}?)?[^\s;&|()<>`$]*/))', re.M)
SCRIPT_HEREDOC_RE = re.compile(r'<<-?[ \t]*([\'"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|\Z)', re.S)     # for a cheap filter: a heredoc body is not execution anyway
SCRIPT_CD_RE = re.compile(r'\b(?:cd|pushd)\b')
SCRIPT_ASSIGN_RE = re.compile(r'(?:^|[;&|(])[ \t]*(?:(?:export|local|readonly|declare)[ \t]+)?([A-Za-z_]\w*)=', re.M)
SCRIPT_ASSIGNS_MAX = 200
_SCRIPTS = {}                 # (realpath, mtime_ns, size) -> (text, masked text). So that the same file is not read again when the first scan goes back over past records
_SCRIPTS_BYTES = [0]
SCRIPTS_CACHE_MAX = 8 << 20


_MASKS_RE = re.compile(r"""['"\\#<`]""")          # characters that shell_code masks or re-reads (quotes, escapes, comments, heredoc, backtick). If there is none, the masked text = the original


def _masked(cmd):
    """shell_code(cmd). If there is no character to mask, the result equals the original, so it is not run (a short one-line command is the most common case)."""
    return shell_code(cmd) if _MASKS_RE.search(cmd) else cmd


def _cmd_assigns(cmd):
    """`VAR=value` in a command (those whose value has no $, quotes or parentheses)."""
    return dict(re.findall(r'(?:^|[;&\s(])([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|"\'$()`]+)', cmd))


def _cmd_words(tail, limit=SCRIPT_TAIL):
    """The words of tail up to before `;&|<>()` or a newline (quotes stripped). [] if it cannot be resolved. A newline ends the command (unlike cx_launches, it does not go over to the next line)."""
    lx = shlex.shlex(tail if limit is None else tail[:limit], posix=True, punctuation_chars=';&|<>()\n')
    lx.whitespace, lx.whitespace_split = ' \t\r', True
    toks = []
    try:
        for tk in lx:
            if tk and all(ch in ';&|<>()\n' for ch in tk):
                break
            toks.append(tk)
    except ValueError:
        return []
    return toks


_WORDS_END_RE = re.compile(r'[;&|()<>]')


def _words_at(text, code, start, limit=SCRIPT_TAIL):
    """The words of the simple command starting at text[start:]. The end is found in the masked text (code): `;&|()<>` inside quotes is masked, so it is not an end,
    and the closing quote of `bash -c "… ./run.sh"` (a `)` in code) is an end. A newline (outside quotes) is an end too. The file number in `2>&1` is not a word."""
    m = _WORDS_END_RE.search(code, start)
    end = m.start() if m else len(text)
    words = _cmd_words(text[start:end], limit)
    if m and code[end] in '<>' and words and words[-1].isdigit() and text[start:end].endswith(words[-1]):
        words.pop()
    return words


def _script_pick(kind, shell, words):
    """(file word, arguments) or None. kind: 'sh' (after a shell name: skip only the options from the closed list, and -c is not a file) | 'src' (source, `.`) | 'path' (run by path: a word with a slash)."""
    if kind == 'src':
        return (words[0], words[1:]) if words else None
    if kind == 'path':
        return (words[0], words[1:]) if words and '/' in words[0] else None
    j = 0
    while j < len(words):
        t = words[j]
        if t == '--':
            j += 1
            break
        if t in ('-o', '+o'):
            if j + 1 >= len(words) or words[j + 1] not in CX_SHELL_OPTIONS.get(shell, ()):
                return None
            j += 2
        elif CX_SHELL_SHORT_RE.fullmatch(t):
            if 'c' in t:
                return None                    # -c: the next text is the command (shell_code has already opened it)
            j += 1
        elif t in CX_SHELL_LONG.get(shell, ()):
            j += 1
        elif t.startswith(('-', '+')):
            return None                        # an option outside the list: we do not know whether it leaves the execution as it is
        else:
            break
    return (words[j], words[j + 1:]) if j < len(words) else None


ENV_FLAGS = frozenset(('-i', '--ignore-environment', '-v', '--debug'))                  # `env` options that take no value
ENV_VALUED = frozenset(('-u', '--unset', '-C', '--chdir', '-a', '--argv0'))             # ... and those whose value is the next word (a short one may also be written attached: `-uNAME`)
ENV_ATTACHED = ('--unset=', '--chdir=', '--argv0=')
ENV_STRING_BAD_RE = re.compile(r'[\'"\\$#]')                                             # what `env -S` reads in a string beyond whitespace (quotes, escapes, ${VAR}, a comment): not taken apart here
PY_FLAGS_RE = re.compile(r'-[BbdEIOPqRsSuvx]+')                                           # options of python that take no value and do not change what runs


def _env_words_target(words):
    """(the program, the words after it) that `env` runs for the words it is given: its options and the values they take (`-u NAME`, `-C DIR`, `-a NAME`, `-i`, `-v`, `--`) and the `NAME=VALUE`
    assignments come first. None when an option is not one of those (it is not known what it does to what runs)."""
    i = 0
    while i < len(words):
        w = words[i]
        if w == '--':
            i += 1
            break
        if w in ENV_FLAGS:
            i += 1
        elif w in ENV_VALUED:
            if i + 1 >= len(words):
                return None
            i += 2
        elif w.startswith(ENV_ATTACHED) or (len(w) > 2 and w[:2] in ('-u', '-C', '-a')):
            i += 1
        elif w.startswith('-'):
            return None
        elif '=' in w:
            i += 1
        else:
            break
    return (words[i], words[i + 1:]) if i < len(words) else None


def _shebang_target(text):
    """(the program a `#!` line makes run, its words, whether every system reads the line the same way), or None when the file has no such line or it cannot be read with certainty. The program is
    the interpreter, or for `env` what `env` runs once its options are taken (`env -S -u python3 true` runs `true`: `python3` is the name `-u` unsets). The last is False when the line has
    several words for `env` without `-S` (Linux hands the whole rest to env as one word, macOS splits it) and for an interpreter given more than one word."""
    if not text.startswith('#!'):
        return None
    parts = text[2:].split('\n', 1)[0].split(None, 1)
    if not parts:
        return None
    name, rest = os.path.basename(parts[0]), (parts[1].strip() if len(parts) > 1 else '')
    if name != 'env':
        return name, (rest.split() if rest else []), len(rest.split()) <= 1
    m = re.match(r'(?:-S|--split-string[= \t])', rest)
    if m:                                                                            # `env -S STRING`: env takes STRING apart itself, the same everywhere
        string = rest[m.end():]
        if ENV_STRING_BAD_RE.search(string):
            return None
        got = _env_words_target(string.split())
        return (os.path.basename(got[0]), got[1], True) if got else None
    words = rest.split()
    got = _env_words_target(words)
    return (os.path.basename(got[0]), got[1], len(words) == 1) if got else None


def _shell_shebang(text):
    """If there is a `#!` line, whether the program it makes run is a shell (`env bash` too, with the options of `env` taken: `env -S -u bash true` runs `true`). True if there is no line (a shell
    script run by path)."""
    if not text.startswith('#!'):
        return True
    got = _shebang_target(text)
    return got is not None and got[0] in CX_SHELLS


PY_SHEBANG_RE = re.compile(r'python[0-9.]*')


def _python_shebang(text):
    """Whether the file begins with a `#!` line that runs this file with python (`/usr/bin/python3`, `/usr/bin/env python3`, `/usr/bin/env -S python3 -u`): the program is python after the options
    of `env` are taken, and what is given to python are options that do not change what runs (`-u`, `-O`, not `-c`, `-m` or a file). A line that is not read the same way everywhere
    (`env python3 -u` without `-S`: Linux looks for a program of that name) or has an option that is not known is not read: a launch is not counted for a file that may not be run by python."""
    got = _shebang_target(text)
    if got is None or not got[2] or PY_SHEBANG_RE.fullmatch(got[0]) is None:
        return False
    return all(PY_FLAGS_RE.fullmatch(w) for w in got[1])


def read_script(path):
    """(text, masked text) of a script file, or None. Regular files only (not a link, FIFO or folder), size cap SCRIPT_MAX, dot paths, secret names and auth files refused (util.open_safe).
    If it is missing, cannot be opened or is binary (has a NUL), it is quietly None."""
    plain = stat_plain(path)
    if not plain or plain[1].st_size > SCRIPT_MAX:
        return None
    real, st = plain
    key = (real, st.st_mtime_ns, st.st_size)
    hit = _SCRIPTS.get(key)
    if hit:
        return hit
    try:
        with open_safe(path, binary=True) as fh:
            data = fh.read(SCRIPT_MAX + 1)
    except OSError:                              # cannot be opened · Denied (a refused target)
        return None
    if len(data) > SCRIPT_MAX or b'\0' in data:
        return None
    text = data.decode('utf-8', 'replace')
    got = (text, _masked(text))
    if _SCRIPTS_BYTES[0] + len(data) > SCRIPTS_CACHE_MAX:
        _SCRIPTS.clear()
        _SCRIPTS_BYTES[0] = 0
    _SCRIPTS[key] = got
    _SCRIPTS_BYTES[0] += len(data)
    return got


def _script_env(text, code, args):
    """Variables that can be resolved inside a script: `$HOME`, the positional arguments received from the call (`$1`…`$9`), and `VAR=value` assignments from the top (only those whose value resolves).
    Used only inside scripts: in the command text, things like `$HOME` are not resolved (so that existing links do not change)."""
    env = {'HOME': os.path.expanduser('~')}
    for i, a in enumerate(args[:9], 1):
        if '$' not in a:
            env[str(i)] = a
    for n, m in enumerate(SCRIPT_ASSIGN_RE.finditer(code)):
        if n >= SCRIPT_ASSIGNS_MAX:
            break
        w = _words_at(text, code, m.end())[:1]
        v = _cx_expand(w[0], env) if w else None
        if v is not None:
            env[m.group(1)] = v
    return env


def _script_kind(m):
    return 'sh' if m.group('sh') else 'src' if m.group('src') else 'path'


SCRIPT_WORD_RE = re.compile(r'[ \t]*(?:"([^"\n]*)"|\'([^\'\n]*)\'|([^\s;&|()<>"\']+))')
SCRIPT_CD_ARG_RE = re.compile(r'\b(?:cd|pushd)[ \t]+(?:"([^"\n]*)"|\'([^\'\n]*)\'|([^\s;&|()<>"\']+))')


def _quick_word(kind, shell, tail):
    """A quick candidate for the file name by regex (without shell word splitting): the word, or None. For a complicated quote where text follows right after the name (`"$D"/run.sh`), '' (go the slow exact way)."""
    pos = 0
    for _ in range(8):
        m = SCRIPT_WORD_RE.match(tail, pos)
        if not m:
            return None
        w = next((g for g in m.groups() if g is not None), None)
        if m.end() < len(tail) and tail[m.end()] not in ' \t\n;&|()<>"\'':      # a quote may be the closing quote of the outer text (`bash -c "… ./run.sh"`)
            return ''
        if kind == 'sh' and w[:1] in ('-', '+'):
            pos = m.end()
            if w in ('-o', '+o'):
                m = SCRIPT_WORD_RE.match(tail, pos)
                pos = m.end() if m else pos
            continue
        return w
    return None


def _mentions(text):
    return 'claude' in text or 'codex' in text


def _may_run_script(rough, ms, base_cwd, assigns):
    """Cheap filter (on the unmasked text, regex and cache only): among the files this command might run, is there a readable script that mentions claude or codex.
    (A script that does not has no Codex or claude -p run anyway, so it need not be looked at, and the expensive shell_code and shell word splitting are not called.)
    A relative path can change with `cd`, so if the cd targets resolve literally, look in those folders, and if they cannot be resolved (a variable etc.) assume there is one (the exact judgment comes later)."""
    dirs, unknown = None, False
    for m in ms[:SCRIPT_RUNS_MAX * 4]:
        kind, shell = _script_kind(m), os.path.basename(m.group('sh') or '')
        word = _quick_word(kind, shell, rough[m.end():])
        if word == '':
            pick = _script_pick(kind, shell, _cmd_words(rough[m.end():]))
            word = pick[0] if pick else None
        if not word:
            continue
        path = _cx_expand(word, assigns()) if '$' in word else os.path.expanduser(word)
        if not path:
            continue
        if os.path.isabs(path):
            paths = [os.path.normpath(path)]
        else:
            if dirs is None:
                dirs, unknown = _cd_dirs(rough, base_cwd)
            if unknown:
                return True
            paths = [os.path.normpath(os.path.join(d, path)) for d in dirs]
        if any(_launcher_file(p) for p in paths):
            return True
    return False


def _launcher_file(path):
    """Whether it is a readable script that mentions claude or codex (uses the cache of read_script: it is read again if the file changes)."""
    got = read_script(path)
    return bool(got and _mentions(got[0]))


def _cd_dirs(rough, base_cwd):
    """The folders where a relative file may be: the call's working folder and the `cd` targets that resolve literally. (the folder list, whether there is a cd that cannot be resolved)."""
    dirs = [base_cwd] if base_cwd and os.path.isabs(base_cwd) else []
    unknown = False
    for m in SCRIPT_CD_ARG_RE.finditer(rough):
        t = next((g for g in m.groups() if g is not None), '')
        if '$' in t or t in ('', '-'):
            unknown = True
        elif os.path.isabs(os.path.expanduser(t)):
            dirs.append(os.path.normpath(os.path.expanduser(t)))
        elif base_cwd and os.path.isabs(base_cwd):
            dirs.append(os.path.normpath(os.path.join(base_cwd, t)))
        else:
            unknown = True
    return dirs, unknown


def bash_scripts(cmd, base_cwd, assigns=None):
    """Among the script files that cmd runs in command position, those that mention claude or codex (one level): [{path, text, code, cwd (the working folder the script starts in, None if unknown), env}].
    Command positions are found in the text masked by shell_code (a `bash file` inside quotes, a heredoc or a comment is not execution). A relative path is against the working folder the command runs in (reflecting the cd range),
    and if it cannot be resolved (a variable, an unknown working folder) it is skipped. A file that is not a shell script (running `#!/usr/bin/env python3` by path) is skipped.
    A read failure or a missing file is quietly skipped."""
    if not isinstance(cmd, str):
        return []
    rough = SCRIPT_HEREDOC_RE.sub('', cmd) if '<<' in cmd else cmd      # looks first in the text with quotes not masked, so it is wider than the real execution (only heredoc bodies are removed)
    ms = list(SCRIPT_RUN_RE.finditer(rough))
    if not ms:
        return []
    got_assigns = [assigns]
    def assigns_():
        if got_assigns[0] is None:
            got_assigns[0] = _cmd_assigns(cmd)
        return got_assigns[0]
    if not _may_run_script(rough, ms, base_cwd, assigns_):
        return []
    assigns = assigns_()
    code = _masked(cmd)
    out = []
    for m in SCRIPT_RUN_RE.finditer(code):
        if len(out) >= SCRIPT_RUNS_MAX:
            break
        kind = _script_kind(m)
        pick = _script_pick(kind, os.path.basename(m.group('sh') or ''), _words_at(cmd, code, m.end()))
        if not pick:
            continue
        scwd = shell_cwd(cmd, code, m.start(), assigns, base_cwd)
        path = _cx_expand(pick[0], assigns)
        if path and not os.path.isabs(path):
            path = os.path.join(scwd, path) if scwd and os.path.isabs(scwd) else None
        path = os.path.normpath(path) if path else None
        got = read_script(path) if path else None
        if not got or not _mentions(got[0]) or (kind == 'path' and not _shell_shebang(got[0])) or not any(script_tools(got[0])):
            continue                                                    # (a script that only says the words, to print or to type them somewhere, runs no tool)
        out.append({'path': path, 'text': got[0], 'code': got[1], 'cwd': os.path.normpath(scwd) if scwd else None,
                    'env': _script_env(got[0], got[1], pick[1])})
    return out


PY_RUN_RE = re.compile(r'(?:^|[;&|(\s])(?:[^\s;&|()<>`$\'"]*/)?python[0-9.]*(?:[ \t]+-[A-Za-z]+)*[ \t]+([^\s;&|()<>`\'"-][^\s;&|()<>`\'"]*\.py)(?![\w.])')


CLAUDE_WORD_RE = re.compile(r'(?<![\w./~-])claude(?![\w-])')
CODEX_WORD_RE = re.compile(r'(?<![\w./~-])codex(?![\w-])')
BOTH_TOOLS = frozenset(('claude', 'codex'))


def _tools_named(text):
    """The tools (`claude`, `codex`) a text names as words."""
    return frozenset(t for t, rx in (('claude', CLAUDE_WORD_RE), ('codex', CODEX_WORD_RE)) if rx.search(text))


_SCRIPT_TOOLS = {}
_SCRIPT_TOOLS_BYTES = [0]


def script_tools(text):
    """(the tools a shell script's text runs, the tools its inline code may run): its text is read as a command is (exec_regions): a program word `claude` / `codex` at a command position, or
    in the command line a wrapper runs, counts; the words in what is printed or typed into a terminal (`tmux send-keys`, `echo`) do not."""
    hit = _SCRIPT_TOOLS.get(text)
    if hit is not None:
        return hit
    run, maybe = exec_regions(text)
    named = frozenset(t for t in (os.path.basename(r.split(None, 1)[0]) for r in run if r.strip()) if t in BOTH_TOOLS)
    got = (named, _tools_named('\n'.join(maybe)) - named)
    if _SCRIPT_TOOLS_BYTES[0] + len(text) > SCRIPTS_CACHE_MAX:
        _SCRIPT_TOOLS.clear()
        _SCRIPT_TOOLS_BYTES[0] = 0
    _SCRIPT_TOOLS[text] = got
    _SCRIPT_TOOLS_BYTES[0] += len(text)
    return got


def _tools_in(text):
    """The tools (`claude`, `codex`) a script's text mentions at all (loosely: a path to the binary counts)."""
    return frozenset(t for t in BOTH_TOOLS if t in text)


# ---------- python files ----------
# A python file is not read as shell words, but as code: it starts a tool where a call of the subprocess family hands it a command to run, and only there. A relay written in python
# (`subprocess.run(["tmux", "send-keys", "-t", "w", "claude -p " + sys.argv[1], "Enter"])`) only carries the words, like `tmux send-keys` typed in a shell does: no launch. A mention anywhere
# else (a string, a comment, a name) is only assumed. A source that does not parse (or that types text into a terminal, as `python -c` code does) says no more than that.
PY_EXEC_NAMES = (frozenset(('subprocess.run', 'subprocess.call', 'subprocess.check_call', 'subprocess.check_output', 'subprocess.Popen', 'subprocess.getoutput', 'subprocess.getstatusoutput',
                            'os.system', 'os.popen', 'os.posix_spawn', 'os.posix_spawnp', 'pty.spawn', 'asyncio.create_subprocess_exec', 'asyncio.create_subprocess_shell'))
                 | frozenset('os.exec' + k for k in ('l', 'le', 'lp', 'lpe', 'v', 've', 'vp', 'vpe'))
                 | frozenset('os.spawn' + k for k in ('l', 'le', 'lp', 'lpe', 'v', 've', 'vp', 'vpe')))
PY_SHELL_NAMES = frozenset(('os.system', 'os.popen', 'subprocess.getoutput', 'subprocess.getstatusoutput', 'asyncio.create_subprocess_shell'))     # the string is a shell command
PY_NODES_MAX = 20000              # a source with more nodes than this is not walked for calls
_PY_TOOLS = {}
_PY_TOOLS_BYTES = [0]


def _py_aliases(tree):
    """{local name: dotted name} for the names `import` and `from … import` bind (`import subprocess as sp`, `from subprocess import Popen`)."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[(a.asname or a.name).split('.')[0]] = a.name if a.asname else a.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                out[a.asname or a.name] = node.module + '.' + a.name
    return out


def _py_dotted(node, aliases):
    """The dotted name a call's function stands for (`sp.run` -> `subprocess.run`), or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return '.'.join(reversed(parts))


def _py_text(node):
    """The text a string expression stands for, with `x` where it is not known (a name, a call, a formatted value), or None when the expression is not a text at all (a name, a call).
    Literals, f-strings, `a + b`, `"…" % x` and `"…".format(x)` are read."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else 'x' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left = _py_text(node.left)
        if left is None:
            return None
        if isinstance(node.op, ast.Mod):
            return left
        right = _py_text(node.right)
        return left + (right if right is not None else 'x')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'format' and isinstance(node.func.value, (ast.Constant, ast.JoinedStr)):
        return _py_text(node.func.value)
    return None


def _py_argv_line(node):
    """A list or tuple of words as one shell command line (a word that is not known is `x`), or None when it is not a list of words."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts or len(node.elts) > 64:
        return None
    return ' '.join(shlex.quote(_py_text(e) or 'x') for e in node.elts)


def _py_command(name, call):
    """What a call of the subprocess family runs, as shell text to read like a command: the shell string for the calls that take one (and `shell=True`), the words of the list otherwise.
    '' when that cannot be told."""
    args = list(call.args)
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    if name.startswith('os.exec') or name.startswith('os.posix_spawn'):
        word = _py_text(args[0]) if args else None                     # the program (`os.execvp("claude", [...])`)
        return shlex.quote(word) if word else ''
    if name.startswith('os.spawn'):
        word = _py_text(args[1]) if len(args) > 1 else None
        return shlex.quote(word) if word else ''
    if name == 'asyncio.create_subprocess_exec':
        return ' '.join(shlex.quote(_py_text(a) or 'x') for a in args[:64])
    first = args[0] if args else kw.get('args')
    if first is None:
        return ''
    shell = name in PY_SHELL_NAMES or (isinstance(kw.get('shell'), ast.Constant) and kw['shell'].value is True)
    if shell:
        return _py_text(first) or ''
    line = _py_argv_line(first)
    if line is not None:
        return line
    if isinstance(first, ast.Call) and _py_dotted(first.func, {}) == 'shlex.split' and first.args:      # `shlex.split("claude -p …")`: the words of that text
        return _py_text(first.args[0]) or ''
    word = _py_text(first)                                              # a plain string without `shell=True` names the program itself
    return shlex.quote(word) if word else ''


def py_tools(text):
    """(the tools a python source starts, the tools it may start) - for a program started by `subprocess`, `os.exec*`, `os.spawn*`, `os.system` or the like, the command it is given is read as a
    shell command is (exec_regions): `claude` or `codex` at a command position counts, the words in what is typed into a terminal (`tmux send-keys`) do not. Every other mention in the source is
    only assumed, unless the source types text into a terminal (then it is nothing). A source that does not parse can only be assumed."""
    hit = _PY_TOOLS.get(text)
    if hit is not None:
        return hit
    mentioned = _tools_in(text)
    named, assumed = set(), set()
    if mentioned:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')                         # (an escape the source writes wrong is its own matter)
                tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
        delivery = DELIVERY_RE.search(text) is not None
        if tree is not None:
            aliases = _py_aliases(tree)
            for k, node in enumerate(ast.walk(tree)):
                if k > PY_NODES_MAX:
                    break
                if not isinstance(node, ast.Call):
                    continue
                name = _py_dotted(node.func, aliases)
                if name in PY_EXEC_NAMES:
                    cmd = _py_command(name, node)
                    if cmd and _tools_in(cmd):
                        got, may = script_tools(cmd)
                        named |= got
                        assumed |= may
        if not delivery:
            assumed |= mentioned - named
    got = (frozenset(named), frozenset(assumed - named))
    if _PY_TOOLS_BYTES[0] + len(text) > SCRIPTS_CACHE_MAX:
        _PY_TOOLS.clear()
        _PY_TOOLS_BYTES[0] = 0
    _PY_TOOLS[text] = got
    _PY_TOOLS_BYTES[0] += len(text)
    return got


def _file_path(word, cwd, assigns):
    """The absolute path a script file word of a command stands for, or None when it cannot be worked out."""
    path = _cx_expand(word, assigns)
    if path and not os.path.isabs(path):
        path = os.path.join(cwd, path) if cwd and os.path.isabs(cwd) else None
    return os.path.normpath(path) if path else None


def _file_tools(word, cwd, assigns, python):
    """For a script file word of a command: (the tools it names, the tools that are only assumed). Both, assumed, when the path cannot be worked out or the
    file cannot be read (missing, too big, a link, refused, binary): nothing rules them out and nothing shows them. A shell script is read for what it runs (script_tools) by its caller;
    a python file (`python`) is read as code (py_tools)."""
    path = _file_path(word, cwd, assigns)
    got = read_script(path) if path else None
    if got is None:
        return frozenset(), BOTH_TOOLS
    return py_tools(got[0]) if python else (frozenset(), frozenset())


def launch_kinds(cmd, base_cwd):
    """The tools a Bash command that holds no `claude -p` the reader could place can still start, as (named, assumed): `named` is `claude` / `codex` when the
    command names it somewhere that runs (a tmux line, a bare `codex exec`), or when it runs a script file (a shell script, a python file) whose text names it;
    `assumed` is both, less what is named, when such a file could not be read (nothing shows what it starts: only that nothing rules it out), and the tool
    that code given on the command line (`python -c`) names. Both empty for a server, a test run, a script that was read and starts nothing, and a command
    whose words only travel as text (`tmux send-keys`, `echo`). A call is a launcher of a child only for the tool the child is of."""
    if not isinstance(cmd, str):
        return frozenset(), BOTH_TOOLS
    code = _masked(cmd)
    run, maybe = exec_regions(cmd)                                      # the words that run, and the words in code that may run them (`python -c`)
    named, assumed = set(_tools_named('\n'.join(run))), set(_tools_named('\n'.join(maybe)))
    for x in bash_scripts(cmd, base_cwd):                              # a shell script that runs a tool (its text is read like a command: only a program word counts)
        got, may = script_tools(x['text'])
        named |= got
        assumed |= may
    assigns = _cmd_assigns(cmd)
    for m in list(SCRIPT_RUN_RE.finditer(code))[:SCRIPT_RUNS_MAX]:
        kind = _script_kind(m)
        pick = _script_pick(kind, os.path.basename(m.group('sh') or ''), _words_at(cmd, code, m.end()))
        if not pick:
            continue
        if kind == 'path' and '$' in pick[0] and not _cx_expand(pick[0], assigns):
            continue                                                   # `$R/run.sh` is a script file only where the command itself says what `$R` is
        if kind == 'path' and not pick[0].endswith('.sh'):
            # a program run by path is not a script we expect to read, unless it is a python file with a python shebang (`./relay.py`, `$R/relay.py`): it is read as code, like `python3 relay.py`
            # (a file without a shebang, or one for a shell, is read as before: the shell scripts above)
            path = _file_path(pick[0], shell_cwd(cmd, code, m.start(), assigns, base_cwd), assigns)
            got = read_script(path) if path else None
            if got is not None and _python_shebang(got[0]):
                named_, assumed_ = py_tools(got[0])
                named |= named_
                assumed |= assumed_
            continue
        got, guess = _file_tools(pick[0], shell_cwd(cmd, code, m.start(), assigns, base_cwd), assigns, False)
        named |= got
        assumed |= guess
    for m in list(PY_RUN_RE.finditer(code if len(code) == len(cmd) else cmd))[:SCRIPT_RUNS_MAX]:
        got, guess = _file_tools(cmd[m.start(1):m.end(1)], shell_cwd(cmd, code, m.start(), assigns, base_cwd), assigns, True)
        named |= got
        assumed |= guess
    return frozenset(named), frozenset(assumed - named)


def launch_tools(cmd, base_cwd):
    """Every tool launch_kinds finds, named or only assumed."""
    named, assumed = launch_kinds(cmd, base_cwd)
    return named | assumed


def could_launch(cmd, base_cwd):
    """Whether launch_tools finds any tool a command can start."""
    return bool(launch_tools(cmd, base_cwd))


def cx_launches(cmd, assigns, base_cwd, eol=False):
    """For each `codex exec` in the command {rx, names, literal, out, resume, cwd (Codex), scwd (shell), n (loop count), exact (the complete literal instruction or None)}. It does not imitate the shell.
    eol: a newline ends the command (a run inside a script file: so the word on the next line does not leak into the instruction). In command text the old rules stay as they were."""
    out, code = [], shell_code(cmd)
    for m in LAUNCH_RE.finditer(code):
        if eol:
            toks = _words_at(cmd, code, m.end(), None)
        else:
            lx = shlex.shlex(cmd[m.end():], posix=True, punctuation_chars=';&|<>()')
            lx.whitespace_split = True
            toks = []
            try:
                for tk in lx:
                    if tk and all(ch in ';&|<>()' for ch in tk):
                        if toks and toks[-1].isdigit() and tk[0] in '<>':
                            toks.pop()          # the file number of 2>&1 · 1>/dev/null
                        break
                    toks.append(tk)
            except ValueError:
                toks = []
        pos, opts, skip = [], {}, None
        for tk in toks:
            if skip:
                opts[skip] = tk
                skip = None
            elif tk in CX_OPT_ARG:
                skip = tk
            elif not tk.startswith('-'):
                pos.append(tk)
        resume = None
        if pos and pos[0] == 'resume':
            resume = pos[1] if len(pos) > 1 else ''
            pos = pos[2:]
        prompt = pos[-1] if pos else ''
        scwd = shell_cwd(cmd, code, m.start(), assigns, base_cwd)
        cdir = opts.get('-C') or opts.get('--cd')
        cwd = _cx_expand(cdir, dict(assigns, PWD=scwd) if scwd else assigns) if cdir else scwd
        if cwd and not os.path.isabs(cwd) and scwd and os.path.isabs(scwd):
            cwd = os.path.join(scwd, cwd)          # a relative -C is against the shell's working folder (-C . used to stay '.' and not match the thread cwd)
        rx, names, literal = cx_prompt_rx(prompt)
        n = loop_factor(cmd, code, m.end())
        out.append({'rx': rx, 'names': names, 'literal': literal if rx else 0, 'resume': resume,
                    'out': opts.get('-o') or opts.get('--output-last-message'),
                    'cwd': os.path.normpath(cwd) if cwd else None, 'scwd': scwd,
                    'n': n if cwd is not None or n < LOOP_UNKNOWN else 1,          # how many threads one place can start (a loop's count); a count and a folder both unknown: one
                    'exact': prompt if (rx is not None and not names and prompt.strip() and prompt != '-') else None})   # the complete literal instruction, when the call gives one
    return out


def resume_ids(text, code, launch_re=None):
    """The thread ids a command resumes: the id after `resume` in the simple command that follows each `codex exec` (code = shell_code(text), the same length:
    quoted words are masked there, so only options and `resume` itself are read from it; the id is read from the text, quotes stripped). An id anywhere else (a
    folder named like a thread, the rollout file of another thread, words in a quoted instruction) is not one."""
    out = set()
    for m in (launch_re or LAUNCH_RE).finditer(code):
        stop = re.search(r'[;&|\n]', code[m.end():m.end() + SCRIPT_TAIL])
        end = m.end() + (stop.start() if stop else SCRIPT_TAIL)
        toks = [(t.start() + m.end(), t.end() + m.end()) for t in re.finditer(r'\S+', code[m.end():end])]
        i = 0
        while i < len(toks):
            word = code[toks[i][0]:toks[i][1]]
            if word == 'resume':
                if i + 1 < len(toks):
                    cand = text[toks[i + 1][0]:toks[i + 1][1]].strip('\'"')
                    if SID_RE.fullmatch(cand):
                        out.add(cand)
                break
            if word in CX_OPT_ARG:
                i += 2
            elif word.startswith('-'):
                i += 1
            else:
                break                                                  # a positional word before `resume`: the instruction, not a resume
    return out


def cx_parse_call(ts, tuid, inp, line_cwd, by='orch', scripts=None):
    """One Claude Bash call. Keeps only calls that name codex (and ran it, or mention a UUID). A `codex exec` inside a script file that the command runs (bash_scripts, one level)
    is also put into this call's runs (L) (scripts: pass it in if it has already been read). `ids`: every UUID in the text (a loose reading); `resumes`: the thread
    ids the call resumes, the only ones that say which thread it started (cx_link)."""
    cmd = (inp or {}).get('command') or ''
    if scripts is None:
        scripts = bash_scripts(cmd, line_cwd)
    sl = [x for x in scripts if 'codex' in x['text'] and LAUNCH_RE.search(x['code'])]
    if 'codex' not in cmd and not sl:
        return None                                    # (a UUID alone - in a folder name, say - is no reason to keep a call)
    code = shell_code(cmd)
    launch = bool(LAUNCH_RE.search(code))
    ids = set(SID_RE.findall(cmd))
    if not launch and not ids and not sl:
        return None
    assigns = _cmd_assigns(cmd)
    L = cx_launches(cmd, assigns, line_cwd) if launch else []
    resumes = resume_ids(cmd, code) if launch else set()
    for x in sl:
        ids |= set(SID_RE.findall(x['text']))
        resumes |= resume_ids(x['text'], x['code'])
        for l in cx_launches(x['text'], x['env'], x['cwd'], eol=True):
            l['env'] = x['env']                       # so that the variables in the -o path (`$R/$t/last.md`) are resolved with this script's values
            L.append(l)
    return {'ts': ts, 'id': tuid, 'desc': (inp or {}).get('description') or '', 'ids': ids, 'resumes': resumes, 'launch': launch or bool(sl),
            'L': L, 'assigns': assigns, 'cwd': line_cwd,
            'by': by, 'bg': None, 'end': None, 'stopped': None}


CX_NODE_WINDOW = 900              # how long before a thread started a call may have started it, when the node of a thread placed by its process is looked for
CX_FIRST_MAX = 20000              # the Codex index keeps this many characters of a thread's first words
CX_OPEN_MAX = 7200.0              # a call whose end was never seen is taken to have run this long at most
CX_END_GRACE = 10.0               # a thread may begin this long after the call that started it ended


def cx_running(c, t):
    """Whether the call was running when a thread began (t): it had started, and it had not ended (a grace after), or its end was never seen and it is not
    older than CX_OPEN_MAX. Only for a call that carries `span_end` (link.py fills it in from the call's span); a call without it is known by its start only."""
    if 'span_end' not in c or not c['ts'] or t < c['ts'] - 1:
        return False
    end = c['span_end']
    return t - c['ts'] <= CX_OPEN_MAX if end is None else t <= end + CX_END_GRACE


def cx_differs(L, t):
    """The refutation of a Codex launch: it gave a complete literal instruction (no variable, not stdin) and the thread's first words are other words (blanks
    and quoting do not count; a first message the index cut is compared by its head). A launch that gives no instruction refutes nothing."""
    lit = L.get('exact')
    raw = t['first_user'] or ''
    if lit is None or not raw.strip():
        return False
    a, b = fp.normalize(lit), fp.normalize(raw)
    return a != b and not (len(raw) >= CX_FIRST_MAX and a.startswith(b))


def cx_link(all_calls, threads, fixed=None, rules=None, nodes=None, held=(), blind=None, up=None):
    """{thread_id: {sid, rule, call, bash_ts, bash_desc, dt, cwd}}.
    1 instruction (fullmatch, 40 or more characters outside variables, cwd, 30 s) → 2 an id inside a run call → 3 only when there is 1 candidate and 1 run call (an estimate)
    → 4 session only: if the run calls within 30 s before the thread started all belong to one Claude session, the agent of that session (an estimate, which call is not known).
    If two Claude sessions try to take the same thread, it is not linked.
    fixed {thread_id: parent session id} are links already decided by the process lineage (rule 'proc'): they come before the rules above, so a call of another session cannot take that thread
    (it is dropped from the candidates of rules 3 and 4 too), and if a call of the same session matches by rule 1 or 2, the details of that call (which call, when) are filled in.
    rules {thread_id: rule} are the rule names of fixed (proc by default, env if found through the environment variable, file if read from the file).
    nodes {thread_id: node}: the node a fixed link names exactly (the environment of a Codex shell says which thread ran the command; None is a root).
    A call's source `sid` is the tree of the record that holds it (a Claude session or a Codex root thread): a thread is never taken for what it launched itself.
    held {thread_id}: threads whose environment names two parents that nothing tells apart (Lineage.pending): no rule below the environment places them.
    blind(thread, trust_free=True, turns=True) → {(tree, node)}: the Codex threads that may have run a command their facts do not hold when this thread began (in its folder or above it; none when
    nothing can have come from a Codex shell, unless `trust_free` is False; an open turn counts unless `turns` is False): competitors nobody can read. The instruction match of a launcher of another tree is then an estimate (`time`) with `incomplete`, not the rule `prompt` (a link the
    lineage placed is not touched). A thread of the launcher's own tree, or above it in the chain of certain links `up(tree)`, is no competitor (it cannot change the tree).
    The tree, the node and the call are decided on their own (R1): the node a fixed link names is not changed by the words of a command of another node; when the launches that fit a
    thread are in several nodes of one tree, the tree is sure, the node and the call are held (`node_unresolved`): the nearest in time is not a reason."""
    execs = [t for t in threads if t['origin'] == 'exec' and not t['guardian'] and t['meta_ts'] and t['id'] not in held]
    known = {t['id'] for t in execs}
    fixed = {tid: sid for tid, sid in (fixed or {}).items() if tid in known}
    rules = rules or {}
    nodes = nodes or {}
    tmap = {t['id']: t for t in execs}

    def cwd_ok(L, t):
        return not L['cwd'] or os.path.normpath(t['cwd'] or '') == L['cwd']

    cand = collections.defaultdict(list)
    for sid, c in all_calls:
        for L in c['L']:
            if L['resume'] is not None or L['literal'] < CX_PROMPT_MIN or not c['ts']:
                continue
            for t in execs:
                if t['id'] == sid:
                    continue
                dt = t['meta_ts'] - c['ts']
                first = (t['first_user'] or '').strip()
                if (0 <= dt <= CX_WINDOW or cx_running(c, t['meta_ts'])) and first and cwd_ok(L, t) and L['rx'].fullmatch(first):
                    cand[t['id']].append((dt, sid, c, L))
    stale_pins = {}
    for tid in [t for t in fixed if rules.get(t) == 'env']:
        # the names a tmux server carries on from the shell that started it: the pin counts while its tree can have started the thread (a call of it that runs `codex` was running then, or the
        # facts of a Codex thread may lack a command); it is out when nothing of the tree can have, and another tree's launch fits the first words of the thread (not a guess)
        tree, t = fixed[tid], tmap[tid]
        if any(sid == tree and c['launch'] and c['ts'] and (0 <= t['meta_ts'] - c['ts'] <= CX_WINDOW or cx_running(c, t['meta_ts'])) for sid, c in all_calls):
            continue
        gaps = blind(t, False) if blind else set()                      # (whatever the pin says: the names that are in question cannot be what rules out a Codex shell)
        if blind and any(k[0] == tree for k in blind(t, False, False)):
            continue                                                    # the facts of the pin's own tree may lack a command (an open turn alone is no reason: its commands are all there)
        rivals = {x[1] for x in cand.get(tid, ()) if x[1] != tree}
        if len(rivals) != 1:
            continue
        win = next(iter(rivals))
        if not {k for k in gaps if k[0] not in (win, tree) and k[0] not in set(up(win) if up else ())}:
            stale_pins[tid] = fixed.pop(tid)
    owners = {t['id']: {'sid': fixed[t['id']], 'node': nodes.get(t['id']), 'rule': rules.get(t['id'], 'proc'), 'call': None, 'bash_ts': None, 'bash_desc': '', 'dt': None,
                        'cwd': t['cwd'], 'cwd_ok': False, 'prompt_ok': False} for t in execs if t['id'] in fixed}

    def info(rule, sid, c, t, L=None):
        return {'sid': sid, 'node': c.get('node'), 'rule': rule, 'call': c['id'], 'bash_ts': c['ts'], 'bash_desc': c['desc'],
                'dt': round(t['meta_ts'] - c['ts'], 3) if c['ts'] else None, 'cwd': t['cwd'],
                'cwd_ok': bool(L and L['cwd'] and os.path.normpath(t['cwd'] or '') == L['cwd']),
                'prompt_ok': rule == 'prompt'}

    for tid, lst in cand.items():
        if tid in fixed:
            lst = [x for x in lst if x[1] == fixed[tid] and (tid not in nodes or x[2].get('node') == nodes[tid])]       # the environment named the node: only a call of that thread
        if lst and len({x[1] for x in lst}) == 1:
            dt, sid, c, L = min(lst, key=lambda x: x[0])
            owners[tid] = info('prompt', sid, c, tmap[tid], L)
            if len({x[2].get('node') for x in lst}) > 1:                 # launches of several nodes of the tree fit it: the tree is sure, the node and the call are not
                owners[tid].update(node=None, call=None, bash_ts=None, bash_desc='', dt=None, node_unresolved=True)
    claims = collections.defaultdict(list)
    for sid, c in all_calls:
        if c['launch']:
            for tid in c.get('resumes', ()) & known:
                if tid != sid:
                    claims[tid].append((c['ts'] or 0, sid, c))
    for tid, lst in claims.items():
        if tid in fixed:
            lst = [x for x in lst if x[1] == fixed[tid] and (tid not in nodes or x[2].get('node') == nodes[tid])]
        if (tid not in owners or (tid in fixed and owners[tid]['rule'] == rules.get(tid, 'proc'))) and lst and len({x[1] for x in lst}) == 1:
            _, sid, c = min(lst, key=lambda x: x[0])
            owners[tid] = info('id', sid, c, tmap[tid])
    launches = [(sid, c) for sid, c in all_calls if c['L'] and c['ts']]
    for sid, c in launches:
        for L in c['L']:
            if L['resume'] is not None or L['literal'] >= CX_PROMPT_MIN:
                continue
            cands = [t for t in execs if t['id'] not in owners and t['id'] != sid and 0 <= t['meta_ts'] - c['ts'] <= CX_WINDOW and cwd_ok(L, t) and not cx_differs(L, t)]
            if len(cands) != 1:
                continue
            t = cands[0]
            others = [x for x in launches if x[1] is not c and 0 <= t['meta_ts'] - x[1]['ts'] <= CX_WINDOW]
            if not others:
                owners[t['id']] = info('time', sid, c, t, L)
    # if a loop starts several threads with "$Q", the call↔thread pairing is unknown but the session is one. A call starts as many threads as its launches
    # allow (a loop: its count; any other call: one): the earliest threads take the places, the later ones are left without a parent.
    room = {}
    for sid, c in launches:
        room[(sid, c['id'])] = sum(L.get('n', 1) for L in c['L'] if L['resume'] is None)
    taken = collections.Counter((o['sid'], o['call']) for o in owners.values() if o['call'] is not None)
    for t in sorted(execs, key=lambda x: x['meta_ts']):
        if t['id'] in owners:
            continue
        near = [(t['meta_ts'] - c['ts'], sid, c, L) for sid, c in launches for L in c['L']
                if sid != t['id'] and L['resume'] is None and 0 <= t['meta_ts'] - c['ts'] <= CX_WINDOW and cwd_ok(L, t) and not cx_differs(L, t)
                and (c['id'] is None or taken[(sid, c['id'])] < room[(sid, c['id'])])]
        if near and len({x[1] for x in near}) == 1:
            dt, sid, c, L = min(near, key=lambda x: x[0])
            owners[t['id']] = info('session', sid, c, t, L)
            taken[(sid, c['id'])] += 1
    if blind:
        # A competitor nobody can read (a Codex thread that may have run a command its facts do not hold): the match of a launcher of another tree is an estimate then. Not a thread of the
        # launcher's tree, nor one above it in the chain of certain links (this round's links, then `up` for what is not a thread of Codex).
        above_of = {tid: o['sid'] for tid, o in owners.items() if certain(o['rule'])}

        def above(x):
            out = []
            for _ in range(MAX_NEST):
                nxt = above_of.get(x) or (next(iter(up(x)), None) if up else None)
                if nxt is None or nxt in out or nxt == x:
                    break
                out.append(nxt)
                x = nxt
            return set(out)
        for tid, o in owners.items():
            if o['rule'] == 'prompt' and tid not in fixed and tid in tmap:
                gone = above(o['sid'])
                if {k for k in blind(tmap[tid]) if k[0] not in (o['sid'], stale_pins.get(tid)) and k[0] not in gone}:       # (the tree whose names were found not to be about it is no competitor nobody can read)
                    o['rule'], o['incomplete'] = 'time', True
    # a thread the process lineage or the environment placed has no call; the call that started it is looked for among the calls of its own session so that the
    # node (the sub-agent whose record holds the call) can be shown. Only the calls that could have started it are read (a `codex exec` inside the window, in its
    # folder, and with the thread's first words when the call gives them); the node is set when they agree on it, the call when there is one of them.
    for tid, o in owners.items():
        if o['call'] is not None or tid not in tmap:
            continue
        t = tmap[tid]
        first = (t['first_user'] or '').strip()
        wide = [(c, L) for sid, c in all_calls if sid == o['sid'] and c['ts'] for L in c['L']
                if L['resume'] is None and 0 <= t['meta_ts'] - c['ts'] <= CX_NODE_WINDOW and cwd_ok(L, t) and not cx_differs(L, t)]
        near = [(c, L) for c, L in wide if t['meta_ts'] - c['ts'] <= CX_WINDOW] or wide       # a call that waited before it ran `codex exec` is the last choice
        said = [(c, L) for c, L in near if L['literal'] >= CX_PROMPT_MIN and first and L['rx'].fullmatch(first)]
        near = said or near
        calls = {id(c): c for c, L in near}
        if tid in nodes:
            calls = {k: c for k, c in calls.items() if c.get('node') == nodes[tid]}      # the environment named the node: only a call of that thread can be the call
        if calls and len({c.get('node') for c in calls.values()}) == 1:
            c = next(iter(calls.values()))
            o['node'] = c.get('node')
            if len(calls) == 1:
                o['call'], o['bash_ts'], o['bash_desc'] = c['id'], c['ts'], c['desc']
                o['dt'] = round(t['meta_ts'] - c['ts'], 3) if c['ts'] else None
    for tid, tree in stale_pins.items():
        if tid in owners:
            owners[tid]['stale_pin'] = tree                               # the names were not about it: `evidence_conflict` says so
    return owners


# ---------- launches: arguments and redirects ----------
# One reader for what a launching command does with its output and what it is told. The affiliation judgment (board/affil.py) uses it for the `out` proof
# (the session id inside a redirect file) and for the literal argument (a launch whose literal prompt differs from the child's first instruction is not its parent; rank 4); the debate judgment (board/debates.py) is meant to use the same redirects for the
# report path (`> r1/A.md`, `-o`). It sees the redirections of a simple command in order (fd table: `> f 2>&1` sends both to f, `2>&1 > f` only fd 1),
# replaces a variable only by a value the command's own scope fixes (an assignment, a `for` list, a script argument), and never searches for a path.
WORD_PAT = r'(?<![\w./~-])(?:claude|codex)(?![\w-])'                     # the word itself: not `.claude/`, `~/.claude`, `claude-code`, `my_claude`
SCRIPT_FILE_RE = re.compile(r'\.(?:sh|py)(?![\w.])')                      # a script or a python file named
LAUNCHY_RE = re.compile(WORD_PAT + r'|\.(?:sh|py)(?![\w.])')            # loose: a call that might launch something (a candidate, never a proof)
WORD_RE = re.compile(WORD_PAT)
WEAK_PRINT_RE = re.compile(r'\s(?:-p|--print)(?![\w-])')
WEAK_LAUNCH_RE = re.compile(r'(?<![\w./~-])claude(?![\w-])[^\n;|&]*?\s(?:-p|--print)(?![\w-])')       # `claude … -p`: a launch the command reader did not place
SCRIPTY_RE = re.compile(r'claude|codex|\.sh\b')          # a sub-agent's command that might run a script file worth reading (the main record's commands are all looked at; a script without a .sh name run from a sub-agent is not followed)
LAUNCHY_B_RE = re.compile(WORD_PAT.encode() + rb'|\.(?:sh|py)(?![\w.])')
WORD_B_RE = re.compile(WORD_PAT.encode())
PY_B_RE = re.compile(rb'\.py(?![\w.])')
SCRIPTY_B_RE = re.compile(rb'\.sh(?![\w.])')
TOOL_ID_B_RE = re.compile(rb'"type":"tool_use","id":"([^"]+)","name":"Bash"')
CWD_B_RE = re.compile(rb'"cwd":"([^"]+)"')
BG_ID_B_RE = re.compile(rb'"backgroundTaskId":\s*"([^"]+)"')
LAUNCHY_SCAN = 4096               # how far into a command a sub-agent line is looked at for anything launchy
TS_B_RE = re.compile(rb'"timestamp":"([^"]+)"')
RED_TOK_RE = re.compile(r'(\d*)(&>>|&>|>>|>&|>|<<<|<<|<&|<)(.*)', re.S)
VAR_RE = re.compile(r'\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*|\d))')
ASSIGN_AT_RE = re.compile(r'(?:^|[;&|(\s])(?:(?:export|local|readonly|declare)[ \t]+)?([A-Za-z_]\w*)=', re.M)
ASSIGN_WORD_RE = re.compile(r'"([^"$`\\\n]*)"|\'([^\'\n]*)\'|([^\s;&|()<>"\'$`\\]+)')
FOR_LIST_RE = re.compile(r'(?:^|[;&|(\s])for[ \t]+([A-Za-z_]\w*)[ \t]+in[ \t]+([^;\n]*?)[ \t]*(?:;|\n)[ \t\n]*do\b')
PATHS_MAX = 8                     # candidate paths one variable list may give
VALUES_MAX = 16
LIT_ARG_MAX = fp.HEAD_BYTES       # a literal argument is kept this many bytes (normalised, bytes held)
SHORT_LITS_MAX = 64
CL_VALUE_OPTS = frozenset((
    '--model', '--effort', '--output-format', '--input-format', '--allowedTools', '--allowed-tools', '--disallowedTools', '--disallowed-tools', '--tools',
    '--max-turns', '--system-prompt', '--append-system-prompt', '--mcp-config', '--permission-mode', '--add-dir', '--settings', '--agents',
    '--setting-sources', '--max-budget-usd', '--fallback-model', '--plugin-dir', '--name', '--permission-prompt-tool', '--betas', '--agent',
    '--session-name', '--json-schema', '--worktree', '--append-system-prompt-file', '--system-prompt-file'))
CL_SHORT_VALUE_OPTS = frozenset(('-n', '-d', '-m'))
REOPEN_OPTS = frozenset(('--resume', '--continue', '--session-id', '--fork-session', '--from-pr'))      # options that take an existing session up again


TOK_TOP_RE = re.compile(r'[ \t\r;|\n)&`$(]')         # in a simple command the characters that end or change the word being read; the others are text
TOK_IN_RE = re.compile(r'[$)`(]')                     # inside `$( … )` or backticks only these do


def _simple_tokens(code, i0):
    """The word ranges [(a, b)] of the simple command that starts at code[i0:] and the position where it ends (`;` `|` newline, an unmatched `)`, a lone `&`).
    `$( … )` and backtick regions stay inside one word, `&>` and `>&` are not command ends. `code` is shell_code(text): quoted text is already masked."""
    n, i, start, depth, bt, toks = len(code), i0, None, 0, False, []
    while i < n:
        m = (TOK_TOP_RE if depth == 0 and not bt else TOK_IN_RE).search(code, i)
        j = m.start() if m else n
        if j > i:                                     # a stretch of ordinary characters is passed at once
            if start is None:
                start = i
            i = j
            if i >= n:
                break
        ch = code[i]
        top = depth == 0 and not bt
        if top and ch in ' \t\r':
            if start is not None:
                toks.append((start, i))
                start = None
            i += 1
            continue
        if top and (ch in ';|\n)' or (ch == '&' and code[i + 1:i + 2] != '>' and code[i - 1:i] != '>')):
            break
        if start is None:
            start = i
        if ch == '$' and code[i + 1:i + 2] == '(':
            depth += 1
            i += 2
            continue
        if ch == '(':
            if code[i - 1:i] in ('<', '>') and i > i0:                  # `<( … )` and `>( … )` (process substitution) are inside one word too
                depth += 1
        elif ch == ')' and depth:
            depth -= 1
        elif ch == '`':
            bt = not bt
        i += 1
    if start is not None:
        toks.append((start, i))
    return toks, i


QUOTE_CHARS_RE = re.compile(r'[\'"\\\s]')


def _classify(text, code, toks):
    """(words, reds, stdin): the shell words of a simple command (quotes removed, None for a word that does not split into exactly one), its redirections
    [(fd text, operator, raw target)] in order, and stdin: False, True (redirected) or the raw word of a `< file`. Operators are found in the masked code, so one
    inside quotes is a word."""
    words, reds, stdin, k = [], [], False, 0
    while k < len(toks):
        a, b = toks[k]
        k += 1
        ct = code[a:b]
        m = RED_TOK_RE.fullmatch(ct) if ct[:1] in '0123456789&<>' else None
        if m is not None and m.group(2) in ('<', '>') and not m.group(1) and m.group(3).startswith('('):
            m = None                                                           # `<( … )` and `>( … )` are a word (a file that is a process), no redirection
        if m is None:
            word = text[a:b]
            if word and not QUOTE_CHARS_RE.search(word):                      # a word with no quote and no backslash is itself
                words.append(word)
                continue
            try:
                parts = shlex.split(word)
            except ValueError:
                parts = None
            words.append(parts[0] if parts and len(parts) == 1 else None)
            continue
        fd, op, rest = m.group(1), m.group(2), m.group(3)
        if rest:
            raw = text[a + m.start(3):b]
        elif k < len(toks):
            raw = text[toks[k][0]:toks[k][1]]
            k += 1
        else:
            raw = ''
        if op[0] == '<':
            stdin = raw if (op == '<' and raw) else True                  # `< file`: the file word; a here-document, a here-string, `<&`: just "stdin is redirected"
        else:
            reds.append((fd, op, raw))
    return words, reds, stdin


def resolve_path(word, env, base_cwd, quoted=True):
    """[(path or None, [unresolved variable names])] for the raw word of a redirect target or an option value. A variable is replaced only by the values
    `env` ({name: [values]}) holds for it (several values give several candidate paths, at most PATHS_MAX); `$( … )` and unknown variables leave the path
    unresolved; a relative path needs a known absolute working folder. Nothing is searched for."""
    w = word
    if quoted:
        try:
            parts = shlex.split(word)
        except ValueError:
            return [(None, [])]
        if len(parts) != 1:
            return [(None, [])]
        w = parts[0]
    if '`' in w or '$(' in w or w.startswith(('<(', '>(')):
        return [(None, ['$(…)'])]
    names = sorted({m.group(1) or m.group(2) for m in VAR_RE.finditer(w)})
    missing = [x for x in names if x not in env]
    if missing:
        return [(None, missing)]
    out = []
    for vals in itertools.islice(itertools.product(*[env[x] for x in names]), PATHS_MAX):
        mp = dict(zip(names, vals))
        p = os.path.expanduser(VAR_RE.sub(lambda m: mp[m.group(1) or m.group(2)], w))
        if not p:
            continue
        if not os.path.isabs(p):
            if not (base_cwd and os.path.isabs(base_cwd)):
                out.append((None, ['cwd']))
                continue
            p = os.path.join(base_cwd, p)
        out.append((os.path.normpath(p), []))
    return out or [(None, [])]


def build_redirects(reds, env, base_cwd):
    """[facts.Redirect] of a simple command's redirections, with the fd table applied in order: `2>&1` copies where fd 1 goes at that moment; `&> f` sends
    both to f. Only fds 1 and 2 are followed; input redirections are not here. A target that is `-` or another fd is a duplication, not a file."""
    fds, out = {1: [], 2: []}, []
    for fdtxt, op, raw in reds:
        n = int(fdtxt) if fdtxt.isdigit() else 1
        if op == '>&' and (raw.isdigit() or raw == '-'):
            src = n
            dst = int(raw) if raw.isdigit() else None
            if src in fds and (dst in fds or raw == '-'):
                fds[src] = list(fds[dst]) if dst in fds else []
                if (src, dst) == (2, 1):
                    for p, un in (fds[1] or [(None, [])]):
                        out.append(Redirect(2, '2>&1', '&1', p, list(un)))
            continue
        both = op in ('&>', '&>>', '>&')
        targets = [1, 2] if both else ([n] if n in fds else [])
        if not targets:
            continue
        res = resolve_path(raw, env, base_cwd)
        for fd in targets:
            fds[fd] = res
            name = '>>' if (op in ('>>', '&>>') and fd == 1) else ('>' if fd == 1 else '2>')
            for p, un in res:
                out.append(Redirect(fd, name, raw, p, list(un)))
    return out


def literal_env(text, code, base=None):
    """{name: [values]} the command's own scope fixes: HOME, `base` (a script's positional arguments and resolved assignments), every distinct literal a
    `NAME=value` assignment gives, and the words of a `for NAME in a b c` list. A value with `$`, a backtick or a glob is not a literal and is left out."""
    env = {'HOME': [os.path.expanduser('~')]}
    for k, v in (base or {}).items():
        env.setdefault(k, [v] if isinstance(v, str) else list(v))
    for m in ASSIGN_AT_RE.finditer(code):
        w = ASSIGN_WORD_RE.match(text, m.end())
        if not w or (w.end() < len(text) and text[w.end()] not in ' \t\n;&|)'):
            continue
        val = next((g for g in w.groups() if g is not None), None)
        vals = env.setdefault(m.group(1), [])
        if val is not None and val not in vals and len(vals) < VALUES_MAX:
            vals.append(val)
    for m in FOR_LIST_RE.finditer(code):
        words = _cmd_words(text[m.start(2):m.end(2)], None)
        if words and not any(re.search(r'[$`*?\[\]~{}]', w) for w in words):
            vals = env.setdefault(m.group(1), [])
            vals.extend(w for w in words[:VALUES_MAX] if w not in vals)
    return {k: v for k, v in env.items() if v}


def parse_claude_args(words):
    """From the dequoted words after `claude`: {'resume', 'session_id', 'persist', 'pos'}. `pos` are the positional words (the instruction is the only one,
    when the command has exactly one). `--resume` takes a value only when it is a session id; an unknown option is taken for a flag."""
    resume = sid = None
    persist, reopens = True, False
    pos, i = [], 0
    while i < len(words):
        w = words[i]
        i += 1
        if w is None:
            pos.append(None)
        elif w == '--':
            pos.extend(words[i:])
            break
        elif w.startswith('--'):
            name, eq, val = w.partition('=')
            if name in REOPEN_OPTS:
                reopens = True                                       # whatever the value is (a variable too): the command takes an existing session up again
            if name == '--resume':
                if eq:
                    resume = val
                elif i < len(words) and words[i] and SID_RE.fullmatch(words[i]):
                    resume, i = words[i], i + 1
            elif name == '--session-id':
                if eq:
                    sid = val
                elif i < len(words):
                    sid, i = words[i], i + 1
            elif name == '--no-session-persistence':
                persist = False
            elif name in CL_VALUE_OPTS and not eq:
                i += 1
        elif w.startswith('-') and len(w) > 1:
            if w in ('-r', '-c'):
                reopens = True
            if w == '-r':
                if i < len(words) and words[i] and SID_RE.fullmatch(words[i]):
                    resume, i = words[i], i + 1
            elif w in CL_SHORT_VALUE_OPTS:
                i += 1
        else:
            pos.append(w)
    return {'resume': resume, 'session_id': sid, 'persist': persist, 'pos': pos, 'reopens': reopens}


PRINT_CMDS = frozenset((
    'echo', 'printf', 'cat', 'head', 'tail', 'less', 'more', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ls', 'wc', 'sort', 'uniq', 'cut', 'tr', 'diff', 'cmp', 'stat',
    'file', 'which', 'type', 'pwd', 'cd', 'test', '[', '[[', 'true', 'false', 'export', 'set', 'unset', 'read', 'date', 'basename', 'dirname', 'realpath',
    'readlink', 'mkdir', 'rm', 'touch', 'chmod', 'ln', 'cp', 'mv', 'tee', 'jq', 'sed', 'awk', 'git', 'gh', 'sleep', 'wait'))
CMD_PREFIXES = frozenset(('sudo', 'nohup', 'setsid', 'time', 'exec', 'env', 'timeout', 'nice', 'command', 'builtin', 'stdbuf', 'ionice'))
CMD_SKIP = frozenset(('then', 'do', 'else', 'elif', 'if', 'while', 'until', '!', '{', '}', 'fi', 'done', 'esac'))
CMD_SEPARATORS = frozenset(('$(', '<(', '>(', '(', '`', ')', ';', ';;', '&', '&&', '||', '|', '|&', '\n'))


def can_launch(code):
    """False when every simple command of the masked text only prints, searches or moves files (`echo 'run: claude -p …'`, `cat > p <<EOF`, `grep claude`):
    such a call can quote a launch and never start one, so it is not a launching call. True when any command could run something, or when the
    text could not be read as commands (the loose test stays loose)."""
    cmdpos, skip_next, seen = True, False, False
    delims = {m.group(2) or m.group(3) or m.group(4) for m in CX_HEREDOC_RE.finditer(code)}
    for m in CX_TOK_RE.finditer(code):
        t = m.group()
        if t[0] in ' \t\r':
            continue
        if t in CMD_SEPARATORS:
            cmdpos, skip_next = True, False
        elif skip_next:
            skip_next = False
        elif t[0] in '<>&':
            skip_next = True                                           # a redirection: the next word is a file, not a command
        elif cmdpos:
            if t in CMD_SKIP or t in delims or CX_ASSIGN_RE.match(t) or t.strip('x') == '':
                continue                                               # a masked heredoc body or quoted text is not a command
            base = t.rsplit('/', 1)[-1]
            if base in CMD_PREFIXES:
                continue
            if base not in PRINT_CMDS:
                return True
            seen = True
            cmdpos = False
    return not seen


# ---------- where a word runs ----------
# `claude` / `codex` in a command starts something only where the shell runs it: at a command position, in the unquoted arguments of a program, or in the
# command line a program that runs its arguments is given. A word that only travels as text (typed into a terminal by `tmux send-keys`, `screen -X stuff`,
# printed by `echo`) is no launch of the call that carries it: that call is nobody's launcher, not even as a guess. Inline code of an interpreter
# (`python -c '…'`) may start the tool and may not: it is only assumed.
# What the program given a command line starts: for each program that runs its arguments, the options that take a value (a short option letter ends a cluster and takes the word after it or
# what is left of the cluster; a long option takes the word after it unless it is written `--name=value`), so that the words which are the command line are told from the options and the
# operands (a host, a container) before it. A program with a subcommand is a run program only for some subcommands.
XARGS_SHORT, XARGS_LONG = 'InPLEsad', frozenset(('--max-args', '--max-procs', '--max-lines', '--eof', '--max-chars', '--arg-file', '--delimiter', '--process-slot-var'))
SSH_SHORT, SSH_LONG = 'bcDEeFIiJLlmOopQRSWw', frozenset()
WATCH_SHORT, WATCH_LONG = 'n', frozenset(('--interval',))
PARALLEL_SHORT, PARALLEL_LONG = 'jnNSaIlLmM', frozenset(('--jobs', '--max-args', '--sshlogin', '--arg-file', '--replace'))
SCREEN_SHORT, SCREEN_LONG = 'cehpQsStTX', frozenset()
DOCKER_SHORT = 'euwvlphmcaN'
DOCKER_LONG = frozenset(('--env', '--user', '--workdir', '--volume', '--name', '--network', '--publish', '--entrypoint', '--restart', '--cpus', '--memory', '--env-file', '--mount',
                         '--label', '--hostname', '--add-host', '--device', '--platform', '--pull', '--shm-size', '--cap-add', '--cap-drop', '--security-opt', '--ulimit', '--gpus',
                         '--group-add', '--cidfile', '--log-driver', '--log-opt', '--dns', '--pid', '--ipc', '--uts', '--userns', '--runtime', '--stop-signal', '--stop-timeout',
                         '--tmpfs', '--label-file', '--detach-keys', '--attach', '--link', '--ip', '--mac-address', '--cgroupns', '--cpuset-cpus', '--cpu-shares'))
KUBECTL_SHORT = 'cnfsl'
KUBECTL_LONG = frozenset(('--container', '--namespace', '--context', '--kubeconfig', '--filename', '--pod-running-timeout', '--request-timeout', '--cluster', '--user', '--server', '--selector'))
TMUX_GLOBAL = 'LSfcT'                                              # tmux options before the subcommand that take a value
TMUX_RUN = {                                                       # subcommand -> the letters of its options that take a value (the rest of its words is the shell command)
    'new-session': 'cEefFnstxyz', 'new': 'cEefFnstxyz', 'new-window': 'cefFnt', 'neww': 'cefFnt', 'split-window': 'cefFlt', 'splitw': 'cefFlt',
    'respawn-pane': 'cet', 'respawnp': 'cet', 'respawn-window': 'cet', 'respawnw': 'cet', 'run-shell': 'cdt', 'run': 'cdt',
    'display-popup': 'bcdehsStTwxy', 'popup': 'bcdehsStTwxy'}
RUN_PROGRAMS = frozenset(('ssh', 'su', 'xargs', 'watch', 'parallel'))                   # run the command line they are given
SUBCOMMAND_PROGRAMS = frozenset(('tmux', 'screen', 'script', 'docker', 'podman', 'kubectl'))     # nothing in their command runs unless the subcommand says so
INLINE_CODE = {'python': '-c', 'node': '-e', 'nodejs': '-e', 'ruby': '-e', 'perl': '-e', 'bun': '-e'}     # interpreters that run code given on the command line
INLINE_NAME_RE = re.compile(r'(python|node|nodejs|ruby|perl|bun)[0-9.]*')
SCRIPT_C_RE = re.compile(r'-[A-Za-z]*c|--command')
DELIVERY_RE = re.compile(r'send-keys|send_keys|sendkeys|paste-buffer|set-buffer|load-buffer|\bstuff\b')    # inline code that types text into a terminal
EXEC_DEPTH = 3                    # how deep the command line given to a program that runs it is read again
EXEC_LINE_MAX = LAUNCHY_SCAN      # a command line longer than this (text) is not read again
EXEC_SEG_MAX = 16384              # a simple command longer than this is not split into words (its masked text is all that is read)


def _command_starts(code):
    """The positions in masked text (shell_code) where the first word of a simple command stands (`can_launch` reads the same positions)."""
    cmdpos, skip_next, out = True, False, []
    delims = {m.group(2) or m.group(3) or m.group(4) for m in CX_HEREDOC_RE.finditer(code)}
    for m in CX_TOK_RE.finditer(code):
        t = m.group()
        if t[0] in ' \t\r':
            continue
        if t in CMD_SEPARATORS:
            cmdpos, skip_next = True, False
        elif skip_next:
            skip_next = False
        elif t[0] in '<>&':
            skip_next = True
        elif cmdpos:
            if t in CMD_SKIP or t in delims or t.strip('x') == '':
                continue
            out.append(m.start())
            cmdpos = False
    return out


def _program_index(words):
    """The index of the program among the dequoted words of a simple command, after `VAR=x` assignments and prefixes (nohup, env, timeout …). None when a prefix
    has an option this reader does not know (what runs is then not known)."""
    k = 0
    while k < len(words):
        w = words[k]
        if CX_ASSIGN_RE.match(w):
            k += 1
            continue
        name = w.rsplit('/', 1)[-1]
        if name in CX_PREFIX_CMDS:
            k = _skip_prefix(name, words, k)
            if k is None:
                return None
        elif name in CMD_PREFIXES:
            k += 1
            while k < len(words) and words[k].startswith('-'):
                k += 1
        else:
            return k
    return None


def _operands(args, short, long=frozenset()):
    """The words of `args` after the options of the program (`short`: the letters of its short options that take a value; `long`: its long options that take one): (index of the first
    operand, the words from there). A word `--` ends the options; a lone `-` is an operand."""
    i = 0
    while i < len(args):
        w = args[i]
        if w == '--':
            return i + 1, args[i + 1:]
        if not w.startswith('-') or w == '-':
            break
        if w.startswith('--'):
            i += 2 if (w in long and i + 1 < len(args)) else 1
            continue
        letters, took = w[1:], False
        for n, ch in enumerate(letters):
            if ch in short:
                took = n == len(letters) - 1                          # the value is the next word, or what is left of the cluster (then the word is done)
                break
        i += 2 if took and i + 1 < len(args) else 1
    return i, args[i:]


def _no_redirects(words):
    """The words of a simple command without its redirections (`> f`, `2>&1`, `< list`): they are not arguments of the program."""
    out, k = [], 0
    while k < len(words):
        w = words[k]
        k += 1
        if CX_REDIR_RE.match(w):
            if CX_REDIR_RE.fullmatch(w):
                k += 1                                                 # the operator alone: the target is the next word
            continue
        out.append(w)
    return out


def _argv_line(words):
    """One command line out of the words a program is given as an argument vector (it runs them as they are)."""
    return shlex.join(words) if words else ''


def _command_lines(prog, args):
    """The command lines a program given these words (after its name) runs, each one text to read again: `ssh host '…'` and `watch cmd` run the words joined by blanks (a shell reads them
    once more), `xargs`, `docker exec`, `kubectl exec --` and a `tmux` command with several words run the words as an argument vector. [] for a program that runs nothing here
    (`tmux send-keys '…'` types the text into a window, where a shell may or may not run it; `docker logs`, `screen -X stuff`). The options and operands before the command (a host, a
    container, a session name) are not part of it."""
    if prog == 'xargs':
        return [_argv_line(_operands(args, XARGS_SHORT, XARGS_LONG)[1])]
    if prog == 'ssh':
        rest = _operands(args, SSH_SHORT, SSH_LONG)[1]                # host, then the command
        return [' '.join(rest[1:])] if len(rest) > 1 else []
    if prog == 'watch':
        return [' '.join(_operands(args, WATCH_SHORT, WATCH_LONG)[1])]
    if prog == 'parallel':
        rest = _operands(args, PARALLEL_SHORT, PARALLEL_LONG)[1]
        cut = next((i for i, w in enumerate(rest) if w in (':::', '::::')), len(rest))
        return [' '.join(rest[:cut])]
    if prog in ('su', 'script'):
        return [args[i + 1] for i, a in enumerate(args[:-1]) if SCRIPT_C_RE.fullmatch(a)][:1]
    if prog == 'screen':
        if any(a == '-X' and 'stuff' in args[i + 1:i + 3] for i, a in enumerate(args)):
            return []
        return [_argv_line(_operands(args, SCREEN_SHORT, SCREEN_LONG)[1])]
    if prog in ('docker', 'podman'):
        sub, rest = _operands(args, DOCKER_SHORT, DOCKER_LONG)
        if not rest or rest[0] not in ('exec', 'run'):
            return []
        return [_argv_line(_operands(rest[1:], DOCKER_SHORT, DOCKER_LONG)[1][1:])]      # the container or the image, then the command
    if prog == 'kubectl':
        sub, rest = _operands(args, KUBECTL_SHORT, KUBECTL_LONG)
        if not rest or rest[0] != 'exec':
            return []
        rest = rest[1:]
        if '--' in rest:
            return [_argv_line(rest[rest.index('--') + 1:])]
        return [_argv_line(_operands(rest, KUBECTL_SHORT, KUBECTL_LONG)[1][1:])]
    if prog == 'tmux':
        out, words = [], _operands(args, TMUX_GLOBAL)[1]
        while words:                                                   # `cmd \; cmd`: the commands of one invocation
            cut = words.index(';') if ';' in words else len(words)
            cmd, words = words[:cut], words[cut + 1:]
            letters = TMUX_RUN.get(cmd[0]) if cmd else None
            if letters is not None:
                shell = _operands(cmd[1:], letters)[1]
                if shell:
                    out.append(shell[0] if len(shell) == 1 else _argv_line(shell))
        return out
    return []


TOOL_TOKEN_RE = re.compile(r'(?:claude|codex)')
EXEC_KEEP_TEXT = 16 << 10         # the regions of a command are remembered for a while (one command is read as a launch, a probe and a span) when it is this short ...
EXEC_KEEP = 4 << 20               # ... and all the remembered commands together are about this many bytes
_EXEC = collections.OrderedDict()
_EXEC_BYTES = [0]
_EXEC_LOCK = threading.Lock()


def exec_regions(text, depth=0):
    """(run, maybe): the pieces of a command's text in which the words `claude` and `codex` run, and those in which they only may (tuples of text). `run`: a tool at a command position
    (after `VAR=x` and prefixes such as nohup, env, timeout) with its plain arguments, and, for a program that runs a command line it is given (tmux new-session, ssh, xargs,
    docker exec …), that command line read once more as a command (its options and operands left out). The plain arguments of any other program (`curl --data claude -p`)
    are what it is given: no place where a tool runs. `maybe`: the code a `python -c` / `node -e` is given (unless it types text into a terminal). A simple command of `echo`,
    `printf`, `tmux send-keys` … gives nothing."""
    return exec_parts(text, depth)[:2]


def exec_parts(text, depth=0):
    """exec_regions and a third part: the masked text of the simple commands that run nothing the reader knows (their program and plain arguments), where a script or a python file
    may still be named."""
    if not isinstance(text, str):
        return (), (), ()
    keep = depth == 0 and len(text) <= EXEC_KEEP_TEXT
    if keep:
        with _EXEC_LOCK:
            hit = _EXEC.get(text)
            if hit is not None:
                _EXEC.move_to_end(text)
                return hit
    got = _exec_regions(text, depth)
    if keep:
        with _EXEC_LOCK:
            _EXEC[text] = got
            _EXEC_BYTES[0] += 2 * len(text) + sum(2 * len(t) for part in got for t in part)
            while _EXEC_BYTES[0] > EXEC_KEEP and _EXEC:
                old, parts = _EXEC.popitem(last=False)
                _EXEC_BYTES[0] -= 2 * len(old) + sum(2 * len(t) for part in parts for t in part)
    return got


def _exec_regions(text, depth):
    """(run, maybe, other): see exec_regions; `other` is the third part of exec_parts."""
    run, maybe, other = [], [], []
    code = _masked(text)
    for a in _command_starts(code):
        end = _simple_tokens(code, a)[1]
        seg = code[a:end]
        masked = seg.split()                                             # the words as the masked text shows them: enough to tell the program, which is not quoted
        k = _program_index(masked)
        prog = os.path.basename(masked[k]) if k is not None else None
        if prog in PRINT_CMDS:
            continue
        if prog is not None and (prog in RUN_PROGRAMS or prog in SUBCOMMAND_PROGRAMS or INLINE_NAME_RE.fullmatch(prog)):
            # a program that runs what it is given, or code given to an interpreter: the arguments are read as words (quotes taken off)
            try:
                words = shlex.split(text[a:min(end, a + EXEC_SEG_MAX)])
            except ValueError:
                words = None
            wk = _program_index(words) if words else None
            if wk is None:
                other.append(seg)
                continue
            args = _no_redirects(words[wk + 1:])
            lines = _command_lines(prog, args) if (prog in RUN_PROGRAMS or prog in SUBCOMMAND_PROGRAMS) else None
            if lines is not None:
                if not lines and prog in SUBCOMMAND_PROGRAMS:
                    continue                                             # `tmux send-keys`, `screen -X stuff`, `docker logs` …: what follows is not a command line
                if depth < EXEC_DEPTH:
                    for line in lines:                                   # the command line the program is given, read once more as a command
                        if line and len(line) <= EXEC_LINE_MAX and _maybe_launchy(line):
                            r, m, o = exec_parts(line, depth + 1)
                            run += r
                            maybe += m
                            other += o
                continue
            other.append(seg)
            flag = INLINE_CODE.get(INLINE_NAME_RE.fullmatch(prog).group(1))
            if flag and flag in args[:-1]:
                snippet = args[args.index(flag) + 1]
                if not DELIVERY_RE.search(snippet):
                    maybe.append(snippet)
            continue
        if prog is not None and TOOL_TOKEN_RE.fullmatch(prog):
            run.append(' '.join(masked[k:]))                             # a tool at a command position (after its prefixes): with its own plain arguments
        else:
            other.append(seg)                                            # any other program: only its place counts, its arguments are what it is given
    return tuple(run), tuple(maybe), tuple(other)


def _arg_literals(pos, env, stdin):
    """(arg, loop_args) of a claude command's instruction: `arg` the normalised literal when it is one word without a substitution (a variable with one
    known value counts), `loop_args` the normalised list when it is a loop variable with several known values (it can only confirm, never veto)."""
    if stdin or len(pos) != 1 or pos[0] is None:
        return None, ()
    w = pos[0]
    m = re.fullmatch(r'\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*|\d))', w)
    if m:
        vals = env.get(m.group(1) or m.group(2)) or []
        if len(vals) == 1:
            w = vals[0]
        elif len(vals) > 1:
            return None, tuple(fp.clip(fp.normalize(v), LIT_ARG_MAX) for v in vals)
        else:
            return None, ()
    if '$' in w or '`' in w:
        return None, ()
    arg = fp.clip(fp.normalize(w), LIT_ARG_MAX)
    return (arg or None), ()


CAT_SUBST_RE = re.compile(r'\$\(\s*(?:cat\s+(?:--\s+)?|<\s*)(\S+|"[^"]*"|\'[^\']*\')\s*\)')


def _arg_source(pos, stdin, env, cwd):
    """The one file a claude command reads its whole instruction from - `"$(cat P)"` as the only argument, or `< P` - as an absolute path, else None."""
    word = None
    if isinstance(stdin, str):
        word = stdin
    elif len(pos) == 1 and isinstance(pos[0], str):
        m = CAT_SUBST_RE.fullmatch(pos[0].strip())
        if m:
            word = m.group(1)
    if word is None:
        return None
    got = resolve_path(word, env, os.path.normpath(cwd) if cwd else None)
    return got[0][0] if len(got) == 1 and got[0][0] else None


QUOTED_SHORT_RE = re.compile(r'"([^"\n]{1,%d})"|\'([^\'\n]{1,%d})\'' % (fp.SHORT_MIN * 2, fp.SHORT_MIN * 2))
SHORT_SEG = 2048                  # how much of a command is looked at for short literals


def short_literals(cmd):
    """Normalised words of a command that are shorter than a long instruction, also from inside quoted strings (`tmux … 'claude -p "/init"'`): the
    literals a short instruction (rank 4) is compared with. Only the part from the first `claude`/`codex` on (SHORT_SEG characters) is looked at, and
    no shell parsing is done: it is a loose reading on purpose. At most SHORT_LITS_MAX."""
    i = min((k for k in (cmd.find('claude'), cmd.find('codex')) if k >= 0), default=0)
    seg = cmd[:SHORT_SEG] if i < SHORT_SEG else cmd[i:i + SHORT_SEG]
    out = {w for w in fp.normalize(seg).split(' ') if 0 < len(w) < fp.SHORT_MIN}
    for m in QUOTED_SHORT_RE.finditer(seg):
        n = fp.normalize(m.group(1) or m.group(2))
        if 0 < len(n) < fp.SHORT_MIN:
            out.add(n)
    if len(out) > SHORT_LITS_MAX:
        out = set(sorted(out)[:SHORT_LITS_MAX])
    return frozenset(out)


def _folder_options(text, code, upto, env, assigns, base_cwd):
    """The folders a launch can run in when the `cd` before it names variables with a few literal values (`for d in a b c; do (cd $T/$d && claude -p ..)`):
    a tuple of normalised paths, () when any of the variables is not known (not set by the command, a glob, a substitution) or there would be too many."""
    names = set()
    for m in SCRIPT_CD_ARG_RE.finditer(text, 0, upto + 8):
        for v in VAR_RE.finditer(next((g for g in m.groups() if g is not None), '')):
            names.add(v.group(1) or v.group(2))
    free = sorted(n for n in names if n not in assigns)
    if not free or 'HOME' in free or any(not env.get(n) for n in free):         # $HOME in a command stays unexpanded (as before)
        return ()
    total = 1
    for n in free:
        total *= len(env[n])
    if total > PATHS_MAX * 2:
        return ()
    out = []
    for combo in itertools.product(*[env[n] for n in free]):
        c = shell_cwd(text, code, upto, dict(assigns, **dict(zip(free, combo))), base_cwd)
        if c is None:
            return ()
        c = os.path.normpath(c)
        if c not in out:
            out.append(c)
    return tuple(out)


def launch_facts(text, code, base_cwd, env_base=None, tool='claude'):
    """One dict per launch of `tool` (`claude -p` or `codex exec`) at a command position of `text` (code = shell_code(text)):
    {cwd, cwds, n, arg, loop_args, resume, session_id, persist, redirects, reopens, src, tool}. `cwd` is the shell's working folder at that point (a `cd` before it counts; `cwds`
    the few folders it can be when a loop variable decides), `n`
    how many children one place can start (loop count). The redirects come from the same simple command. `env_base`: a script's resolved variables."""
    rx = CLAUDE_LAUNCH_RE if tool == 'claude' else LAUNCH_RE
    env = assigns = None
    out = []
    for m in rx.finditer(code):
        if env is None:
            env = literal_env(text, code, env_base)
        if assigns is None:
            assigns = dict(_cmd_assigns(text), **(env_base or {}))           # `T=~/p; cd $T/x`: the folder the command itself names (a script's resolved variables come first)
        cwd = shell_cwd(text, code, m.start(), assigns, base_cwd)
        cwds = ()
        if cwd is None:
            cwds = _folder_options(text, code, m.start(), env, assigns, base_cwd)
            if len(cwds) == 1:
                cwd, cwds = cwds[0], ()
        n = loop_factor(text, code, m.end())
        if cwd is None and n >= LOOP_UNKNOWN:
            n = 1                                                     # neither the count nor the working folder is known: not several by time alone
        toks, _ = _simple_tokens(code, m.end())
        words, reds, stdin = _classify(text, code, toks)
        redirects = build_redirects(reds, env, os.path.normpath(cwd) if cwd else None)
        d = {'tool': tool, 'cwd': os.path.normpath(cwd) if cwd else None, 'cwds': cwds, 'n': n, 'arg': None, 'loop_args': (), 'resume': None, 'session_id': None,
             'persist': True, 'redirects': redirects, 'reopens': False, 'src': None}
        if tool == 'claude':
            a = parse_claude_args(words)
            d['resume'], d['session_id'], d['persist'], d['reopens'] = a['resume'], a['session_id'], a['persist'], a['reopens']
            d['arg'], d['loop_args'] = _arg_literals(a['pos'], env, stdin)
            d['src'] = _arg_source(a['pos'], stdin, env, cwd) if d['arg'] is None else None
        else:
            for j, w in enumerate(words[:-1]):
                if w in ('-o', '--output-last-message'):
                    for p, un in resolve_path(words[j + 1] or '', env, os.path.normpath(cwd) if cwd else None, quoted=False):
                        redirects.append(Redirect(1, '-o', words[j + 1] or '', p, list(un)))
        out.append(d)
    return out


NEEDLE_SDK = b'"promptSource":"sdk"'
LINES_BEFORE = 3                  # how many lines before an instruction are looked at (they tell how the run before it stood)
NEEDLE_QUOTA = b'"quotaLimits"'
NEEDLE_BASH = b'"Bash"'
NEEDLES_RUN = (b'"promptSource":"sdk"', b'"turnPosition"', b'"cost-state"')       # an instruction line (a slash command's has the position and no source) and the end of a run
NEEDLES_WE = (b'"Write"', b'"Edit"', b'"MultiEdit"')
FIRST_LINES = 64                  # the first lines of a child record are always read for its start instruction (older records have no promptSource)
DEEP_DELAY = 0.0                  # seconds the later stage waits after start_deep() before it begins (the caller picks the moment: after the first screen is built)
SUB_LIST_EVERY = 30.0              # the sub-agent record listing is redone at least this often
RECHECK = 10.0                    # a new run is judged again on every scan for this long (the text around it is still being written)
SETTLE = 120.0                    # a child whose last run started this long ago is judged once and remembered
ORPHAN_GRACE = affil.ORPHAN_GRACE      # a call that ended is judged for `orphan_launch` after this long (a child can still be starting)
ORPHAN_OPEN = affil.ORPHAN_OPEN        # a call still running is judged (one launch, no child at all) after this long
BASH_EVENTS_INSERT_MAX = 2000      # more new calls than this at once: the event list is sorted again instead of filled in
FINISH_EVERY = 30.0               # the judgment is redone at least this often (a call that never ended ages out, an `orphan_launch` count waits for the clock)
RESULT_WINDOW = 256 << 10           # a call's result is looked for this far after its own line first (a result follows its call closely)
PEND_MAX = 16                     # calls whose end is still being looked for, per record
MAX_NEST = 8                      # how many links up the graph of who started whom is followed (a cycle is cut earlier)
CHUNK = 16 << 20
LINE_MAX = 64 << 20               # a record line longer than this is not read: it is dropped, once, up to its end (the position moves past it)
OUT_READ_HEAD = 4096
OUT_READ_TAIL = 64 << 10
OUT_SID_RE = re.compile(r'"session_?[iI]d"\s*:\s*"(%s)"' % SID_RE.pattern)
ID_ARG_RE = re.compile(r'(?:--resume|--session-id|(?<![\w-])-r)[ \t=]+["\']?(%s)' % SID_RE.pattern)
NOTE_RE = re.compile(r'<(status|summary)>(.*?)</\1>', re.S)


def _launchy_kind(raw):
    """0: the command of a Bash line (read from the raw JSON without decoding it) mentions nothing launchy; 1: only a `.py` file; 2: the word claude/codex,
    a `.sh` file, a script-looking path, or no readable command (all of them are decoded and read properly)."""
    i = raw.find(b'"command":"')
    if i < 0:
        return 2
    seg = raw[i + 11:i + 11 + LAUNCHY_SCAN]
    if (b'claude' in seg or b'codex' in seg or b'.sh' in seg) and (SCRIPTY_B_RE.search(seg) or WORD_B_RE.search(seg)):      # plain substring tests first: the regexes are slow
        return 2
    return 1 if (b'.py' in seg and PY_B_RE.search(seg)) else 0


def _raw_ts(raw):
    """The record time of a line, read from the raw JSON (the key sits after the message body, so it is looked for from the end)."""
    i = raw.rfind(b'"timestamp":"')
    if i < 0:
        return None
    return parse_ts(raw[i + 13:i + 13 + 32].split(b'"', 1)[0].decode('ascii', 'replace'))


def _weak_launch(cmd):
    """Whether a command that the reader found no `claude -p` in still starts one: the words stand unquoted in an argument (`xargs … claude -p`), or a
    quoted string is handed to a program that runs it (`tmux new-session`, ssh, ...). Text in a heredoc, a comment, a string given to anything else
    (`python3 - <<EOF`) or one that is only typed into a terminal (`tmux send-keys`, `echo`) is not a launch."""
    head = cmd[:LAUNCHY_SCAN]
    if 'claude' not in head or not WEAK_PRINT_RE.search(head):
        return False                                                   # cheap: no `claude` or no `-p` / `--print` anywhere
    return any(WEAK_LAUNCH_RE.search(t) for t in exec_regions(head)[0])


def _launchy_call(cmd):
    """Whether a command that no launch was read from and that runs no script file is still worth a span (a candidate the probe may read again): it holds a
    `.sh` / `.py` word or a tool word, and the tool word stands where something runs it. A call whose words only travel as text (typed into a terminal, printed)
    has no span: it is nobody's launcher, so it cannot be a rival of the one that is."""
    if not (_maybe_launchy(cmd) and LAUNCHY_RE.search(cmd)):
        return False
    if not WORD_RE.search(cmd):
        return True
    if not can_launch(_masked(cmd)):
        return False
    run, maybe, other = exec_parts(cmd)
    return any(LAUNCHY_RE.search(t) for t in run + maybe) or any(SCRIPT_FILE_RE.search(t) for t in other)


def _maybe_launchy(cmd):
    """A cheap necessary condition for LAUNCHY_RE (plain substring tests are much faster than the regex on a long command)."""
    return 'claude' in cmd or 'codex' in cmd or '.sh' in cmd or '.py' in cmd


def _launchy_line(raw):
    """Whether the command of a Bash tool_use line (read from the raw JSON without decoding the line) mentions anything launchy. A line without a readable command counts."""
    i = raw.find(b'"command":"')
    if i < 0:
        return True
    return LAUNCHY_B_RE.search(raw, i + 11, i + 11 + LAUNCHY_SCAN) is not None


def _lines_before(body, off, earlier=()):
    """The up to LINES_BEFORE lines just before the one that starts at `off` in body (a chunk ending in a newline); `earlier` (the last lines of the chunk before) fills
    in when the chunk has fewer. Lines longer than 256 KiB are left out."""
    out, end = [], off - 1
    while end > 0 and len(out) < LINES_BEFORE:
        start = body.rfind(b'\n', 0, end) + 1
        out.append(body[start:end])
        end = start - 1
    if end <= 0 and len(out) < LINES_BEFORE:
        out += list(reversed(earlier))[:LINES_BEFORE - len(out)]
    return [x for x in reversed(out) if x and len(x) <= affil.RunStarts.CONTEXT_MAX]


def _hit_lines(body, needles=(NEEDLE_QUOTA, NEEDLE_BASH)):
    """From body (a chunk ending in a newline), returns, in line order, only the lines that contain one of the needles (`"quotaLimits"` or `"Bash"` by default)."""
    return [line for _, line in _hit_spans(body, needles)]


def _hit_spans(body, needles):
    """[(offset in body, line)] in line order, only for the lines that contain one of the needles.
    Instead of searching each line, it searches the whole chunk for just those strings (memmem) and cuts out that line: almost every line of the record has none of them."""
    spans = set()
    for needle in needles:
        pos = body.find(needle)
        while pos >= 0:
            a, b = body.rfind(b'\n', 0, pos) + 1, body.find(b'\n', pos)
            b = len(body) if b < 0 else b
            spans.add((a, b))
            pos = body.find(needle, b)
    return [(a, body[a:b]) for a, b in sorted(spans)]


def _related_dir(a, b):
    """Whether two working folders are the same or one is inside the other (no if unknown)."""
    if not a or not b:
        return False
    return a == b or b.startswith(a.rstrip('/') + '/') or a.startswith(b.rstrip('/') + '/')


class _Idx:
    """A compact index of record lines that carry text: time, byte offset and length of each line, in file order."""
    __slots__ = ('ts', 'off', 'ln')

    def __init__(self):
        self.ts, self.off, self.ln = array.array('d'), array.array('Q'), array.array('I')

    def add(self, ts, off, ln):
        self.ts.append(ts)
        self.off.append(off)
        self.ln.append(ln)

    def insert(self, ts, off, ln):
        """Adds a line that may be older than the newest one (a window scanned later): the time order of the index is kept."""
        i = bisect.bisect_right(self.ts, ts)
        self.ts.insert(i, ts)
        self.off.insert(i, off)
        self.ln.insert(i, ln)


def _blocks(d):
    """The content blocks of a record line: a list, [] for any other shape (a broken line must not stop the scan)."""
    m = d.get('message') if isinstance(d, dict) else None
    c = m.get('content') if isinstance(m, dict) else None
    return c if isinstance(c, list) else []


def _input(b):
    """The input of a tool_use block: a dict, {} for any other shape."""
    inp = b.get('input')
    return inp if isinstance(inp, dict) else {}


def line_texts(raw):
    """The normalised texts a record line contributes to the fingerprint pool: Bash commands, Write contents, Edit replacements. [] for any other line.
    A text longer than LINE_BYTES (bytes held) is cut and ends with fp.CUT: the pool that holds it is incomplete."""
    try:
        d = json.loads(raw[:fp.LINE_BYTES * 4])
    except ValueError:
        try:
            d = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(d, dict) or d.get('type') != 'assistant':
        return []
    out = []
    for b in _blocks(d):
        if not (isinstance(b, dict) and b.get('type') == 'tool_use'):
            continue
        inp, name = _input(b), b.get('name')
        if name == 'Bash':
            vals = [inp.get('command')]
        elif name == 'Write':
            vals = [inp.get('content')]
        elif name == 'Edit':
            vals = [inp.get('new_string')]
        elif name == 'MultiEdit':
            edits = inp.get('edits')
            vals = [e.get('new_string') for e in edits if isinstance(e, dict)] if isinstance(edits, list) else []
        else:
            continue
        for v in vals:
            if isinstance(v, str):
                head = fp.clip(v, fp.LINE_BYTES)
                t = fp.normalize(head)
                if t:
                    out.append(t + fp.CUT if len(head) < len(v) else t)
    return out


class OutIndex:
    """The `out` proof (rank 1): which redirect files hold which child session ids, and which launching calls wrote those files. A file is read again only
    when its size or time changed; only `"session_id": "<id>"` keys count (a result text that merely mentions an id is not a proof)."""

    def __init__(self):
        self.by_path = {}                 # path -> [(call, redirect)] sorted by call start
        self.seen = {}                    # path -> (mtime_ns, size)
        self.sid_files = {}               # session id -> {path}
        self.file_sids = {}               # path -> frozenset of ids

    def add(self, call):
        for r in call.span.redirects:
            if r.path_resolved and r.op in ('>', '>>', '2>', '-o'):
                lst = self.by_path.setdefault(r.path_resolved, [])
                lst.append((call, r))
                if len(lst) > 1 and lst[-2][0].span.start > call.span.start:
                    lst.sort(key=lambda x: x[0].span.start)

    def drop(self, calls):
        """Forgets the redirects of these calls (a record that was written again: what its calls said is not true any more)."""
        gone = {id(c) for c in calls}
        for path in list(self.by_path):
            kept = [x for x in self.by_path[path] if id(x[0]) not in gone]
            if kept:
                self.by_path[path] = kept
            else:
                del self.by_path[path]

    def refresh(self):
        """Looks at every redirect file again (a stat each) and re-reads the ones that changed. True if the set of ids changed."""
        changed = False
        for path in list(self.by_path):
            plain = stat_plain(path)
            sig = (plain[1].st_mtime_ns, plain[1].st_size) if plain else None
            if self.seen.get(path) == sig:
                continue
            self.seen[path] = sig
            new = self._read(path) if sig else frozenset()
            old = self.file_sids.get(path, frozenset())
            if new != old:
                changed = True
                for s in old - new:
                    self.sid_files.get(s, set()).discard(path)
                for s in new - old:
                    self.sid_files.setdefault(s, set()).add(path)
                self.file_sids[path] = new
        return changed

    @staticmethod
    def _read(path):
        try:
            with open_safe(path, binary=True) as fh:
                size = os.fstat(fh.fileno()).st_size
                head = fh.read(OUT_READ_HEAD)
                tail = b''
                if size > OUT_READ_HEAD:
                    fh.seek(max(OUT_READ_HEAD, size - OUT_READ_TAIL))
                    tail = fh.read(OUT_READ_TAIL)
        except OSError:
            return frozenset()
        return frozenset(OUT_SID_RE.findall((head + b'\n' + tail).decode('utf-8', 'replace')))

    def files_with(self, sid):
        return sorted(self.sid_files.get(sid, ()))

    def writers(self, path):
        return self.by_path.get(path, [])


class _WrittenText:
    """What a file held when a launching call ran (the facts.Launch.src_text of a command that reads its instruction from one file): asked, it looks the file up in the
    record of the launching session. Known answers are kept; "not known (yet)" is asked again."""
    __slots__ = ('index', 'f', 'path', 'ts', 'text', 'known')

    def __init__(self, index, f, path, ts):
        self.index, self.f, self.path, self.ts, self.text, self.known = index, f, path, ts, None, False

    def __call__(self):
        if not self.known:
            try:
                self.text = self.index.written_text(self.f, self.path, self.ts)
            except (OSError, ValueError):
                self.text = None
            self.known = self.text is not None or self.index.deep_inline or self.index._text_ok
        return self.text


class LinkIndex:
    """Gathers Codex run calls and `claude -p` launches from the Claude records and builds {thread_id: owning Claude session} and {child session: owner info}.
    Stage 1 (`scan`, the first screen waits for it): the main records, only lines with `"quotaLimits"` or `"Bash"` decoded; the launching calls with their ends, a
    text index and the start instruction of every child. Stage 2 (`scan_deep`, run in the background once): sub-agent records and the Write/Edit text index, which
    the content fingerprint needs. A call's affiliation is judged by board/affil.py from these facts; this class only collects them and publishes the result.
    `deep_inline` True (the default) runs stage 2 at the end of every `scan()`, so a caller that scans once gets the finished answer."""

    def __init__(self):
        self.lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self.files = {}          # path -> per-record facts (main records)
        self.sub = {}            # path -> per-record facts (sub-agent records, stage 2)
        self.owners = {}
        self.ready = threading.Event()
        self.deep_ready = threading.Event()
        self.deep_inline = True
        self._key = None
        self.quota = {}          # records of Claude reaching a limit: rateLimitType -> {status, resets_at, ts}
        self.cli_owners = {}     # child Claude session id started with claude -p -> {sid (the launching session), node, rule, by, bash_ts, bash_desc, call, calls, calls_certain, dt, cwd}
        self.decisions = {}      # child session id -> affil.Decision (the judgment behind cli_owners)
        self.assignment = affil.assign_launches([])      # runs matched to the launches that could have started them (the call of a run, the orphan count)
        self.diags = []          # [{'code', 'subject', 'tree', 'node', ...}]: what the judgment noticed (no text, no path, no environment value)
        self.heads = {}          # Claude session record path -> (first time, working folder)
        self.lineage = Lineage()   # links decided by the process lineage (cli: child Claude sessions, cx: Codex threads): rank 2 evidence, the tree only
        self.entry = {}            # Claude session record path -> entry point (cli|sdk-cli): the "entrypoint" in the record head
        self.unlinked_map = {}     # session id -> candidates that started after that session's Bash call but could not be linked anywhere (unlinked_for)
        self.out = OutIndex()
        self.tcache = fp.TextCache()
        self.timing = {}           # seconds of the last stage 1 / stage 2 (for the performance report)
        self._ev = ([], [], -1)    # every Bash call's (time, session id, cwd) in time order, those times, and the number of calls when it was built
        self._ev_seen = {}         # record path -> how many of its calls are in self._ev
        self._entries = []         # the Codex list used in the last link calculation
        self._dirty = True
        self._pins_sig = None
        self._finished_at = 0.0
        self._deep_thread = None
        self._deep_lock = threading.Lock()
        self._text_ok = False       # True while stage 2 runs: the Write/Edit lines may be read now
        self._grew = True          # a main record grew since the sub-agent records were listed
        self._sub_paths = []
        self._sub_listed = 0.0
        self._link_codex_done = False
        self._chunk = None         # (bytes, offset) of the chunk being read: a call's result is looked for in it right away
        self._recheck_until = 0.0
        self._memo = {}            # child sid -> (inputs, Decision) of the children that are settled
        self.cx_files = {}         # Codex thread id -> per-thread facts (the roots and the sub-agents: the threads whose commands can launch something)
        self.cx_roots = frozenset()      # the ids of the Codex root threads that have a record here: a tree of a relation can be one of them
        self._cx_ver = None        # CODEX.version at the last sync of the thread facts
        self._ent, self._ent_ver = [], None      # the copy of the Codex entries and the version of the index it was made at
        self._entmap, self._entmap_ver = {}, None          # the same by thread id
        self._stale = {}           # `claude -p` child id -> the tree its environment names, for a child whose Codex names are older than any turn it could have begun in (no pin: said as a diagnostic)
        self._stale_cx = {}        # the same for the `codex exec` threads
        self._cx_ev = ([], [])     # every command of every Codex thread as (start, tree, folder) in time order, and the starts (for the missed candidates)
        self._cx_dirty = None      # the earliest time a Codex fact arrived for (a command, a gap) since the settled judgments were last checked
        self._cx_gaps = None       # {thread id: [(start, end, why)]} where the commands of a thread may be missing (for the blind test), made once for one judgment
        self._graph_key, self._graph_state = None, (0, {}, {})                 # the graph of who started whom (owner_of, page_of, descendants), made again when its inputs changed: (version, edges, children)
        self._graph_src = (None, None)                                         # the two dicts of links it was made from (kept, so that a new dict can never take the address of one it was compared with)
        self._pub = 0              # rises each time the links are published (the children's, the threads') or the Codex threads change: what the graph is made of

    def _quota(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        q, ts = d.get('quotaLimits'), parse_ts(d.get('timestamp'))
        if isinstance(q, dict) and q.get('rateLimitType') and ts:
            old = self.quota.get(q['rateLimitType'])
            if not old or ts >= old['ts']:
                self.quota[q['rateLimitType']] = {'status': q.get('status'), 'resets_at': q.get('resetsAt'), 'ts': ts}

    # ---------- reading the records ----------
    def _new_file(self, p, tree, node):
        f = {'pos': 0, 'we_pos': 0, 'path': p, 'sid': os.path.basename(p)[:-6], 'tree': tree, 'node': node, 'calls': [], 'cli': [], 'bash': [],
             'spans': {}, 'pend': {}, 'pbg': {}, 'tb': _Idx(), 'tw': _Idx(), 'wfile': {}, 'rs': None, 'mtime': None, 'we_cov': [], 'launch_calls': []}
        f['owner'] = affil.Owner(tree, node, pool=lambda lo, hi, f=f: self._pool(f, lo, hi), text_of=lambda call, f=f: self._call_text(f, call))
        return f

    def _line_text(self, f, off, ln, fh=None):
        key = (f['path'], off)
        t = self.tcache.get(key)
        if t is not None:
            return t
        try:
            if fh is not None:
                fh.seek(off)
                raw = fh.read(ln)
            else:
                with open_regular(f['path']) as h:
                    h.seek(off)
                    raw = h.read(ln)
        except OSError:
            return ''
        t = '\0'.join(line_texts(raw))
        self.tcache.put(key, t)
        return t

    def _call_text(self, f, call):
        return self._line_text(f, call.off[0], call.off[1]) if call.off else ''

    def _pool(self, f, lo, hi):
        """(normalised text, complete) of the Bash commands, Write contents and Edit replacements of one record between two times, newest line first,
        at most OWNER_BYTES (a bigger window is cut and reported incomplete: no certain link may come from it)."""
        self._scan_we(f, lo, hi)
        parts, key = [], [f['path'], 'pool']
        for k in ('tb', 'tw'):
            idx = f[k]
            i, j = bisect.bisect_left(idx.ts, lo), bisect.bisect_right(idx.ts, hi)
            key += [i, j, len(idx.ts)]
            parts.extend((idx.ts[x], idx.off[x], idx.ln[x]) for x in range(i, j))
        if not parts:
            return '', True
        key = tuple(key)
        hit = self.tcache.get(key)                           # the same window of the same lines (children launched in one burst share it)
        if hit is not None:
            return hit[1:], hit[0] == 'y'
        parts.sort(key=lambda x: -x[0])
        texts = []
        try:
            with open_regular(f['path']) as fh:
                size = 0
                for ts, off, ln in parts:
                    t = self._line_text(f, off, ln, fh)
                    texts.append(t)
                    size += fp.nbytes(t)
                    if size > fp.OWNER_BYTES:
                        break
        except OSError:
            return '', True
        pool, complete = fp.join_pool(texts)
        self.tcache.put(key, ('y' if complete else 'n') + pool)
        return pool, complete

    def _scan_we(self, f, lo, hi):
        """Makes sure the Write/Edit lines of a record between two times are in the text index. A record is not read for them until a fingerprint needs that
        window; the byte range is bracketed by the nearest Bash lines (the index of every Bash line is time ordered), and a range read once is not read again."""
        tb = f['tb']
        i, j = bisect.bisect_left(tb.ts, lo), bisect.bisect_right(tb.ts, hi)
        a = tb.off[i - 1] if i > 0 else 0
        b = min(f['pos'], tb.off[j] + tb.ln[j] + 1) if j < len(tb.ts) else f['pos']
        cov, gaps, at = f['we_cov'], [], a
        for ca, cb in cov:
            if cb <= at:
                continue
            if ca >= b:
                break
            if ca > at:
                gaps.append((at, ca))
            at = max(at, cb)
        if at < b:
            gaps.append((at, b))
        for ga, gb in gaps:
            done = self._feed(f['path'], f, ga, gb, {'we'})
            cov.append((ga, done))
        if gaps:
            cov.sort()
            merged = []
            for ca, cb in cov:
                if merged and ca <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], cb))
                else:
                    merged.append((ca, cb))
            f['we_cov'] = merged

    def _index(self, f, kind, ts, off, ln):
        idx = f[kind]
        if len(idx.ts) and ts < idx.ts[-1]:
            ts = idx.ts[-1]                                  # records are in time order; a stray earlier stamp must not break the bisect
        idx.add(ts, off, ln)

    def _feed(self, p, f, start, stop, kinds):
        """Reads record bytes [start, stop) (stop None = end of file) in chunks and hands every interesting line to its handler. Returns the new offset
        (after the last complete line). kinds: a set of 'quota', 'bash' (launching calls and their ends), 'runs' (the start instructions of a child), 'we' (Write/Edit text index)."""
        needles = []
        if 'quota' in kinds:
            needles.append(NEEDLE_QUOTA)
        if 'bash' in kinds:
            needles.append(NEEDLE_BASH)
        if 'runs' in kinds:
            needles += list(NEEDLES_RUN)
        if 'we' in kinds:
            needles += list(NEEDLES_WE)
        pos, rest, first = start, b'', start == 0
        dropping = stop is None and f.get('drop_at') == start             # the position is inside a line that was dropped for its length: its end is looked for first
        with open_regular(p) as fh:
            fh.seek(pos)
            while True:
                want = CHUNK if stop is None else min(CHUNK, stop - pos - len(rest))
                chunk = fh.read(want) if want > 0 else b''
                if not chunk:
                    break
                if dropping:
                    nl = chunk.find(b'\n')
                    pos += len(chunk) if nl < 0 else nl + 1
                    if nl < 0:
                        continue
                    chunk, dropping = chunk[nl + 1:], False
                    if not chunk:
                        continue
                data = rest + chunk
                cut = data.rfind(b'\n') + 1
                body = data[:cut]
                hits = _hit_spans(body, needles)
                if 'runs' in kinds and first:
                    seen = {a for a, _ in hits}
                    a = 0
                    for _ in range(FIRST_LINES):
                        b = body.find(b'\n', a)
                        if b < 0:
                            break
                        if a not in seen:
                            hits.append((a, body[a:b]))
                        a = b + 1
                    hits.sort(key=lambda x: x[0])
                first = False
                self._chunk = (body, pos)
                for off, raw in hits:
                    try:
                        if 'quota' in kinds and NEEDLE_QUOTA in raw:
                            self._quota(raw)
                        if 'bash' in kinds and NEEDLE_BASH in raw and self._bash(f, raw, pos + off):
                            f['_calls_changed'] = True
                        if 'runs' in kinds and f['rs'] is not None and NEEDLE_SDK in raw:
                            for before in _lines_before(body, off, f.get('prev_lines', ())):
                                f['rs'].feed_context(before)                   # how the run before this instruction stood (a tool that never returned)
                        if 'runs' in kinds and f['rs'] is not None and f['rs'].feed(raw):
                            self._dirty = True                             # a child started a run: it is judged now and again for a few seconds
                            self._recheck_until = time.time() + RECHECK
                        if 'we' in kinds and any(n in raw for n in NEEDLES_WE):
                            self._write_edit(f, raw, pos + off)
                    except Exception as e:   # noqa: BLE001 — one bad line must not stop the scan
                        line_error(f['sid'], None, e)
                self._chunk = None
                if 'bash' in kinds:
                    self._close_pending(f, body, pos)
                if 'runs' in kinds and f['rs'] is not None:
                    f['prev_lines'] = _lines_before(body, len(body), f.get('prev_lines', ()))        # the last lines of this chunk, for an instruction at the start of the next
                rest = data[cut:]
                pos += cut
                if len(rest) > LINE_MAX and stop is None:
                    pos += len(rest)                                       # a line this long is not a record line: skip it, and do not read it again at the next scan
                    rest, dropping = b'', True
        if stop is None:
            if dropping:
                f['drop_at'] = pos
            else:
                f.pop('drop_at', None)
        return pos

    def _read_file(self, p, f, kinds, st=None):
        """New bytes of one main/sub record. True if anything was read. `st`: the stat result when the caller already has it."""
        if st is None:
            try:
                st = os.stat(p)
            except OSError:
                return False
        f['mtime'] = st.st_mtime
        if st.st_size == f['pos']:
            return False
        if f['pos'] == 0:
            self._head(p)
            if 'runs' in kinds and self.entry.get(p) in (None, 'sdk-cli'):
                f['rs'] = affil.RunStarts()
        kk = set(kinds)
        if f['rs'] is None:
            kk.discard('runs')
        try:
            f['pos'] = self._feed(p, f, f['pos'], None, kk)
        except OSError as e:                                         # a record this user may not open (left by another user), gone while it was read
            if not f.get('unreadable'):
                line_error(f['sid'], None, e)                       # once, until the file can be read again
                self._dirty = True                                  # the diagnostic changes
            f['unreadable'] = True
            return False
        if f.pop('unreadable', None):
            self._dirty = True
        return True

    def scan(self):
        """Stage 1, then stage 2 when `deep_inline` (the default: a caller that scans once gets the finished answer) or once stage 2 has completed. With
        `deep_inline` False (the server's instance) a scan returns after stage 1 (`ready` is set) and judges what stage 1 knows; stage 2 starts on request
        (`start_deep`), once, in a background thread. A stage that fails is logged and the scan goes on."""
        with self._scan_lock:
            t0 = time.time()
            self._stage1()
            t1 = time.time()
            if self.deep_inline or self.deep_ready.is_set():
                self._guard(self._stage2)
            self.timing = {'stage1': round(t1 - t0, 3), 'stage2': round(time.time() - t1, 3)}

    @staticmethod
    def _guard(fn):
        """Runs one stage of the scan; an exception is logged (its type only, never a line) and the scan goes on with what it has."""
        try:
            fn()
        except Exception as e:   # noqa: BLE001 — a record this code cannot read must not stop the board
            print('link scan error', getattr(fn, '__name__', 'stage'), type(e).__name__, flush=True)

    def start_deep(self):
        """Starts stage 2 (sub-agent records, the Write/Edit text index, the judgment by content) in a background thread. The caller picks the moment: after the
        first screen has been built, so that its CPU is not shared with this stage. Asking again, while it runs or when it is done, does nothing. True when this
        call started it. Nothing to start for an index that runs stage 2 inline."""
        with self._deep_lock:
            if self.deep_inline or self.deep_ready.is_set() or self._deep_thread is not None:
                return False
            self._deep_thread = threading.Thread(target=self._deep_main, name='link-deep', daemon=True)
            self._deep_thread.start()
        return True

    def _deep_main(self):
        if DEEP_DELAY:
            time.sleep(DEEP_DELAY)
        try:
            self.scan_deep()
        except Exception as e:   # noqa: BLE001
            print('link scan error deep', type(e).__name__, flush=True)
            self.deep_ready.set()                                    # later scans run the stage themselves (and say so again if it keeps failing): never stuck at the first stage

    def scan_deep(self):
        """Stage 2 alone (sub-agent records, Write/Edit text index, the final judgment): meant for a background thread once stage 1 has set `ready`."""
        with self._scan_lock:
            t1 = time.time()
            self._stage2()
            self.timing['stage2'] = round(time.time() - t1, 3)

    def _stage1(self):
        with self.lock:
            present = set()
            for p in glob.glob(os.path.join(PROJECTS, '*', '*.jsonl')):
                f = self.files.get(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue                                         # a FIFO, a folder or a device with a record's name is not a record
                present.add(os.path.basename(p)[:-6])
                if f is None or st.st_size < f['pos']:
                    if f is not None:
                        self.tcache.drop_prefix(p)
                    sid = os.path.basename(p)[:-6]
                    f = self.files[p] = self._new_file(p, sid, None)
                if self._read_file(p, f, {'quota', 'bash', 'runs'}, st):
                    self._grew = True
            CODEX.refresh()
            try:
                if self._sync_codex():
                    self._dirty = True
            except Exception as e:   # noqa: BLE001 — the Claude records are judged even when the Codex facts cannot be read
                print('codex facts error', type(e).__name__, flush=True)
            try:
                if self.lineage.scan(present, CODEX.get, CODEX.root_of):
                    self._dirty = True
            except Exception as e:   # noqa: BLE001 — the other rules keep running even if the lineage cannot be read
                print('lineage scan error', type(e).__name__, e, flush=True)
        self._guard(self._link_codex)
        self._link_codex_done = True
        if not self.deep_inline and not self.deep_ready.is_set():
            # stage 2 has not run (yet): the first screen does not wait for it. What stage 1 knows is judged now (no text fingerprint yet) and again whenever
            # something changed, so that children that start meanwhile are linked as well as stage 1 can
            if not self.ready.is_set() or self._judgment_due():
                self._guard(lambda: self._finish(content=False))
            self.ready.set()

    def _all_files(self):
        """Every launching record: the Claude sessions, their sub-agents, and the Codex threads."""
        return list(self.files.values()) + list(self.sub.values()) + list(self.cx_files.values())

    def _link_codex(self):
        key = (CODEX.version, sum(len(f['calls']) for f in self._all_files()), self.lineage.version)
        changed = any(f.pop('_calls_changed', False) for f in self._all_files())
        if changed or key != self._key:
            self._key = key
            for f in self._all_files():
                for c in f['calls']:
                    sp = f['spans'].get(c['id'])
                    if sp is not None:
                        c['span_end'] = sp.span.end                    # None: still running (or its end never seen)
            all_calls = [(f['tree'], c) for f in self._all_files() for c in f['calls']]
            self._entries = self._codex_entries()
            self._cx_gaps = None
            self._cx_gaps = None
            cx, info, entmap = self.lineage.cx, self.lineage.cx_info, self._cx_ent_map()
            fixed, self._stale_cx = {}, {}
            for t, parent in cx.items():
                if info[t].get('exact') and not self._cx_env_fresh(info[t].get('node') or parent, (entmap.get(t) or {}).get('meta_ts')):
                    self._stale_cx[t] = parent                        # the names of a Codex thread older than any turn it could have begun this thread in: not about it
                else:
                    fixed[t] = parent
            owners = cx_link(all_calls, self._entries, fixed, {t: i['rule'] for t, i in info.items()},
                             {t: i.get('node') for t, i in info.items() if t in fixed and i.get('exact')},
                             {t for t, w in self.lineage.pending.items() if w['kind'] == 'cx'}, self._cx_blind_for, self._up_of(self.cli_owners, self.owners))
            for o in owners.values():
                o['parent_kind'] = self.tree_kind(o['sid'])
            with self.lock:
                self.owners = owners
                self._pub += 1

    def _stage2(self):
        self._text_ok = True
        try:
            self._stage2_body()
        finally:
            self._text_ok = False

    def _stage2_body(self):
        changed = False
        now = time.time()
        if self._grew or not self._sub_listed or now - self._sub_listed > SUB_LIST_EVERY:
            self._sub_paths = glob.glob(os.path.join(PROJECTS, '*', '*', 'subagents', 'agent-*.jsonl'))     # the listing is the costly part: only when a record grew or now and then
            self._sub_listed, self._grew = now, False
        for p in self._sub_paths:
            f = self.sub.get(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue                                             # a FIFO, a folder or a device with a record's name is not a record
            if f is None:
                m = re.fullmatch(r'agent-(%s)\.jsonl' % NODE_ID_RE.pattern, os.path.basename(p))
                if not m:
                    continue
                f = self.sub[p] = self._new_file(p, os.path.basename(os.path.dirname(os.path.dirname(p))), m.group(1))
                f['sid'] = m.group(1)
            if st.st_size < f['pos']:
                self.tcache.drop_prefix(p)
                f = self.sub[p] = self._new_file(p, f['tree'], f['node'])
            changed = self._read_file(p, f, {'bash'}, st) or changed
        if changed or not self._link_codex_done:
            self._link_codex()
        self._link_codex_done = False
        if self._judgment_due() or not self.deep_ready.is_set():
            self._finish()
        self.ready.set()
        self.deep_ready.set()

    def _judgment_due(self):
        """Whether the judgment has to be made again: something changed, the lineage or the live processes changed, or it is time (a call that never ended ages out)."""
        return (self._dirty or self._signature() != self._pins_sig or time.time() - self._finished_at > FINISH_EVERY or time.time() < self._recheck_until
                or bool(self.lineage.pending))             # a child with two environments waits for the chain: it is looked at again on every scan

    def _bash(self, f, raw, off=0):
        """A line holding a Bash call: gathers the Codex run calls (f['calls']), the `claude -p` launches (f['cli']) (including those inside scripts) and the call's span
        (when it ran: f['spans']). A line is decoded only once. True if the Codex calls grew. A failure on one side does not block the other."""
        if f['node'] is not None:
            kind = _launchy_kind(raw)                                 # a sub-agent line is read cheaply unless its command names claude/codex or a .sh script
            if kind < 2:
                ts = _raw_ts(raw)
                if ts:
                    self._index(f, 'tb', ts, off, len(raw))
                    if kind == 1:
                        self._quick_span(f, raw, ts, off)             # a bare .py command: a span without launches, no JSON decoding
                    return False
                if kind == 0:
                    return False
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get('type') != 'assistant':
            return False
        got = False
        for b in _blocks(d):
            if not (isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash'):
                continue
            inp = _input(b)
            cmd = inp.get('command')
            ts = parse_ts(d.get('timestamp'))
            if ts:
                f.setdefault('bash', []).append((ts, os.path.normpath(d['cwd']) if isinstance(d.get('cwd'), str) and d['cwd'] else None))
                if 'tb' in f:
                    self._index(f, 'tb', ts, off, len(raw))
            scripts = bash_scripts(cmd, d.get('cwd')) if (f['node'] is None or (isinstance(cmd, str) and SCRIPTY_RE.search(cmd))) else []
            if b'codex' in raw or scripts:
                try:
                    got = self._call(f, d, b, scripts) or got
                except Exception as e:   # noqa: BLE001
                    line_error(f['sid'], None, e)
            launches = []
            if (b'claude' in raw and (b' -p' in raw or b'--print' in raw)) or scripts:
                try:
                    launches = self._cli_call(f, d, b, scripts)
                except Exception as e:   # noqa: BLE001
                    line_error(f['sid'], None, e)
            if ts and 'spans' in f and isinstance(cmd, str) and (launches or scripts or _launchy_call(cmd)):
                self._span(f, d, b, ts, cmd, launches, off, len(raw))
        return got

    def _cli_line(self, f, raw):
        """A `claude -p` call started with Bash: time, description, working folder (the preceding `cd X &&`, else the cwd of that line). Those inside scripts too (bash_scripts)."""
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get('type') != 'assistant':
            return
        for b in _blocks(d):
            if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                self._cli_call(f, d, b, bash_scripts(_input(b).get('command'), d.get('cwd')))

    def _cli_call(self, f, d, b, scripts):
        """Every `claude -p` in a run position of the call (several are possible in one command) becomes a launch candidate in f['cli'] and is returned.
        Text in heredocs, quoted bodies and comments is not execution, so it is masked first with shell_code and then searched: a `claude -p` written in a
        document does not pull in an unrelated session that started in the same folder around the same time."""
        inp = _input(b)
        cmd = inp.get('command') or ''
        ts, desc = parse_ts(d.get('timestamp')), inp.get('description') or ''
        found = [(cmd, shell_code(cmd), None, d.get('cwd'))] if isinstance(cmd, str) and 'claude' in cmd else []
        found += [(x['text'], x['code'], x['env'], x['cwd']) for x in scripts if 'claude' in x['text']]
        new = []
        for text, code, env, base in found:
            for L in launch_facts(text, code, base, env, 'claude'):
                c = {'ts': ts, 'id': b.get('id'), 'desc': desc, 'cwd': L['cwd'], 'n': L['n'], 'arg': L['arg'], 'loop_args': L['loop_args'], 'resume': L['resume'],
                     'session_id': L['session_id'], 'persist': L['persist'], 'redirects': L['redirects'], 'reopens': L['reopens'], 'cwds': L['cwds'], 'src': L['src']}
                f.setdefault('cli', []).append(c)
                new.append(c)
        return new

    def _quick_span(self, f, raw, ts, off):
        """The span of a sub-agent's bare `.py` command, from the raw line (no JSON decoding): its id, start and folder. Without the id there is no span."""
        m = TOOL_ID_B_RE.search(raw)
        if not m:
            return
        tid = m.group(1).decode('utf-8', 'replace')
        c = CWD_B_RE.search(raw)
        span = Span(owner_tree=f['tree'], owner_node=f['node'], call_id=tid, start=ts, end=None, launchy=True,
                    cwd=os.path.normpath(c.group(1).decode('utf-8', 'replace')) if c else None, redirects=[])
        call = affil.Call(span, [], frozenset(), '', (off, len(raw)), probe=lambda: self._can_launch(f, tid, off, len(raw)))
        f['spans'][tid] = call
        f['owner'].add(call)
        self._track_end(f, call)
        self._dirty = True

    def _can_launch(self, f, tid, off, ln):
        """The tools a call without launches the reader placed could still start, as (named, assumed) (launch_kinds). The command is read from the record again, only
        now that it is asked for (a call is asked about when it was running as a child started). Both tools, assumed, when the line cannot be read: it cannot be
        ruled out and it shows nothing."""
        try:
            with open_regular(f['path']) as fh:
                fh.seek(off)
                d = json.loads(fh.read(ln))
            message = d.get('message') if isinstance(d, dict) else None
            for b in (message.get('content') if isinstance(message, dict) else None) or []:
                if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('id') == tid and isinstance(b.get('input'), dict):
                    return launch_kinds(b['input'].get('command'), d.get('cwd') if isinstance(d.get('cwd'), str) else None)
        except (OSError, ValueError):
            pass
        return frozenset(), BOTH_TOOLS

    def _track_end(self, f, call):
        """Looks for the end of a new call right after its own line; a call whose end is not there yet waits (at most PEND_MAX, the newest) for the later chunks."""
        if self._chunk is not None:
            self._close_one(f, call, self._chunk[0], self._chunk[1], RESULT_WINDOW)
        if call.span.end is None:
            f['pend'][call.span.call_id] = call
            if len(f['pend']) > PEND_MAX:
                f['pend'].pop(next(iter(f['pend'])))

    def _span(self, f, d, b, ts, cmd, launches, off, ln):
        """The launching call as a facts.Span with its launches; its end is looked for in the following lines (_close_pending)."""
        tid = b.get('id')
        if not tid:
            return
        msg = d.get('message') if isinstance(d.get('message'), dict) else {}
        call = self._make_call(f, tid, ts, cmd, d.get('cwd'), _input(b).get('description') or '', launches, (off, ln), lambda: self._can_launch(f, tid, off, ln),
                               msg_id=msg.get('id') if isinstance(msg.get('id'), str) else None)
        self._track_end(f, call)
        self._dirty = True

    def _make_call(self, f, tid, ts, cmd, cwd, desc, launches, loc, probe, end=None, end_status='running', msg_id=None):
        """The affil.Call of one launching command of a record `f` (a Claude Bash call or a Codex command), registered with its owner and the output index.
        `end`: when the call is already over (a Codex command is written when it ends). `msg_id`: the group the call was made in (`Span.msg_id`): the message.id of the line of a Claude
        call, the exec call around a Codex command (else the command's own id); what makes two launches one launch together."""
        reds = [r for L in launches for r in L['redirects']]
        span = Span(owner_tree=f['tree'], owner_node=f['node'], call_id=tid, start=ts, end=end, end_status=end_status, launchy=True,
                    cwd=os.path.normpath(cwd) if isinstance(cwd, str) and cwd else None, redirects=reds, msg_id=msg_id)
        lits = short_literals(cmd) if (launches or (('claude' in cmd or 'codex' in cmd) and WORD_RE.search(cmd))) else frozenset()
        launch_list = [affil.Launch(L['cwd'], L['n'], L['arg'], L['loop_args'], L['resume'], L['session_id'], L['persist'], True, L['redirects'], L['reopens'], L['cwds'],
                                    _WrittenText(self, f, L['src'], ts) if L.get('src') else None)
                       for L in launches]
        if not launch_list and 'claude' in cmd and _weak_launch(cmd):
            launch_list = [affil.Launch(None, 1, None, (), None, None, True, False, [])]      # not read (tmux, xargs, ...): never a refutation, only counted
        call = affil.Call(span, launch_list, lits, desc, loc,
                         frozenset(ID_ARG_RE.findall(cmd[:SCRIPT_TAIL * 4])) if ('--resume' in cmd or '--session-id' in cmd or ' -r' in cmd) else frozenset(),
                         probe=probe)
        f['spans'][tid] = call
        if call.launches:
            f['launch_calls'].append(call)
        f['owner'].add(call)
        self.out.add(call)
        return call

    def _close_pending(self, f, body, base):
        """Looks in this chunk for the end of every call still waiting (a call whose result or notification is further on than its own search window)."""
        for tid, call in list(f['pend'].items()):
            self._close_one(f, call, body, base, None)
        latest = max((c.span.start for c in f['pend'].values()), default=0)
        for tid in [t for t, c in f['pend'].items() if latest - c.span.start > affil.OPEN_SPAN_MAX]:
            del f['pend'][tid]

    def _close_one(self, f, call, body, base, window):
        """The end of one call: its result line (foreground) or, for a background call (its result carries `backgroundTaskId`), the task notification that follows.
        Searched in `body` after the call's own line, within `window` bytes (None: to the end of the chunk). A call whose end is not found stays waiting
        (`f['pend']`) and, if it never shows, open (affil.OPEN_SPAN_MAX)."""
        tid, span = call.span.call_id, call.span
        at = max(0, call.off[0] + call.off[1] - base)
        stop = None if window is None else at + window
        if not span.bg_id:
            hit = self._find_line(body, b'"tool_use_id":"%s"' % tid.encode(), at, stop)
            if hit is None:
                return
            ts, line, pos = hit
            m = BG_ID_B_RE.search(line) if b'"backgroundTaskId"' in line else None      # only a background call's result names a task (no need to decode every result)
            if m:
                span.bg_id = m.group(1).decode('utf-8', 'replace')       # a background call: its end is the notification that follows
                at = pos
            else:
                span.end, span.end_status = ts, 'result'
                f['pend'].pop(tid, None)
                self._dirty = True
                return
        hit = self._find_line(body, b'<tool-use-id>%s</tool-use-id>' % tid.encode(), at, stop)
        if hit is not None:
            ts, line, _ = hit
            span.end, span.end_status = ts, 'notification'
            notes = dict(NOTE_RE.findall(line.decode('utf-8', 'replace').replace('\\n', '\n')))
            span.end_reason = (notes.get('summary') or notes.get('status') or '')[:160] or None
            f['pend'].pop(tid, None)
            self._dirty = True

    @staticmethod
    def _find_line(body, needle, at, stop=None):
        """(time, line, line start) of the first line at or after `at` (and starting before `stop`) that holds the needle and has a time, else None."""
        pos = body.find(needle, at) if stop is None else body.find(needle, at, stop)
        while pos >= 0:
            a, e = body.rfind(b'\n', 0, pos) + 1, body.find(b'\n', pos)
            line = body[a:(len(body) if e < 0 else e)]
            ts = _raw_ts(line)
            if ts:
                return ts, line, a
            nxt = len(body) if e < 0 else e
            pos = body.find(needle, nxt) if stop is None else body.find(needle, nxt, stop)
        return None

    def _write_edit(self, f, raw, off):
        """A Write/Edit/MultiEdit tool call: its time and place are indexed, and which file it wrote (the text is read again when a window needs it)."""
        d = json.loads(raw)
        if not isinstance(d, dict) or d.get('type') != 'assistant':
            return
        files = [(_input(b).get('file_path'), b['name']) for b in _blocks(d)
                 if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') in ('Write', 'Edit', 'MultiEdit')]
        if files:
            ts = parse_ts(d.get('timestamp'))
            if ts:
                f['tw'].insert(ts, off, len(raw))
                f['wfile'][off] = [(os.path.normpath(p), n) for p, n in files if isinstance(p, str) and os.path.isabs(p)]

    def written_text(self, f, path, ts):
        """The normalised text the file `path` held at time `ts`, as far as the record `f` shows it: the newest Write of that file before `ts`, none of the
        Edits after it. None when that is not known (no such Write, an Edit changed the file since, the line was cut). Only once the text index is wanted
        (stage 2, or an index that runs it inline): the first screen does not read the Write/Edit lines."""
        if not (self.deep_inline or self._text_ok) or f.get('provider') == 'codex':
            return None                                              # (a Codex thread's file writes are not in its facts)
        self._scan_we(f, ts - fp.WINDOW_BEFORE, ts)
        tw = f['tw']
        for x in range(bisect.bisect_right(tw.ts, ts) - 1, -1, -1):
            if tw.ts[x] < ts - fp.WINDOW_BEFORE:
                break
            for p, name in f['wfile'].get(tw.off[x], ()):
                if p == path:
                    if name != 'Write':
                        return None
                    text = self._line_text(f, tw.off[x], tw.ln[x])
                    return None if fp.CUT in text else text
        return None

    def _head(self, p):
        """The first time and working folder of a Claude session record (the first 256 KB). Once found, it does not change."""
        h = self.heads.get(p)
        if h and h[0] and h[1]:
            return h
        ts = cwd = None
        try:
            with open_regular(p) as fh:
                head = fh.read(256 << 10)
        except OSError:
            return (None, None)
        k = re.search(rb'"timestamp":"([^"]+)"', head)
        ts = parse_ts(k.group(1).decode()) if k else None
        k = re.search(rb'"cwd":"([^"]+)"', head)
        cwd = os.path.normpath(k.group(1).decode('utf-8', 'replace')) if k else None
        k = re.search(rb'"entrypoint":"([a-z-]+)"', head)
        if k:
            self.entry[p] = k.group(1).decode()
        self.heads[p] = (ts, cwd)
        return (ts, cwd)

    # ---------- the Codex threads as launchers ----------
    # A Codex root thread (and each of its sub-agents) is a launching record like a Claude session: the commands it ran (the facts of board/codex_index.py: the
    # command string as written, the folder, when it started and ended) are read by the same command reader as a Bash call. The tree is the root thread, the node
    # the sub-agent thread (None for the root itself). Only a command that is over is known (Codex writes it when the process ends): a command still running, or
    # whose text is not kept, is a gap (`CODEX.gaps`), and a thread that may have such a command is a competitor nobody can read (affil._blind).
    def _new_cx_file(self, e, root):
        tid = e['id']
        f = {'provider': 'codex', 'tid': tid, 'gen': e.get('gen', 0), 'sid': tid, 'tree': root, 'node': None if tid == root else tid, 'calls': [], 'spans': {}, 'launch_calls': [], 'pos': 0,
             'ids': set(), 'items': [], 'events': [], 'tb': _Idx(), 'entry': e, 'last_id': None}
        f['owner'] = affil.Owner(root, f['node'], pool=lambda lo, hi, f=f: self._cx_pool(f, lo, hi), text_of=lambda call, f=f: self._cx_text(f, call.span.call_id),
                                 provider='codex', cwd=e['cwd'] or None, blind_at=lambda lo, hi, f=f: self._cx_blind(f, lo, hi),
                                 gap_at=lambda lo, hi, f=f: self._cx_blind(f, lo, hi, False))
        return f

    def _codex_entries(self):
        """CODEX.entries() (a copy of every entry), made once for one version of the index."""
        ver = CODEX.version
        if self._ent_ver != ver:
            self._ent, self._ent_ver = CODEX.entries(), ver
        return self._ent

    def _cx_ent_map(self):
        """{thread id: entry} of the Codex index, made once for one version of it."""
        ver = CODEX.version
        if self._entmap_ver != ver:
            self._entmap, self._entmap_ver = {e['id']: e for e in self._codex_entries()}, ver
        return self._entmap

    def _cx_env_fresh(self, thread, t0):
        """Whether a child that began at `t0` can have been started by a shell of the Codex thread whose names its environment carries: its turn was running then (it began before, and
        it ended after the child began less RUN_GRACE, or it is open and the process of its root is there). The names of a thread stay in the environment of whatever a shell of it left
        running (a tmux server, a daemon): a child it starts much later is not that thread's. True when that cannot be told (no time for the child, no turns known of the thread)."""
        e = self._cx_ent_map().get(thread)
        turns = e.get('turns') if e else None
        if t0 is None or e is None or turns is None:
            return True
        alive = None
        for tr in turns:
            start, end = tr.get('start'), tr.get('end')
            if start is not None and start > t0 + affil.RUN_SLACK_BEFORE:
                continue                                              # began after the child
            if end is not None:
                if t0 <= end + affil.RUN_GRACE:
                    return True
                continue
            if alive is None:                                         # an open turn: it is running while its process is there (unknown counts as there)
                f = self.cx_files.get(thread)
                root = self._cx_ent_map().get(f['tree']) if f else e
                alive = not self._cx_dead(root) if root and self._cx_dead(root) is not None else None
            if alive is not False or t0 <= (e['last_ts'] or 0) + affil.RUN_GRACE:
                return True
        return False

    def _sync_codex(self):
        """Brings the Codex launching records up to date with the Codex index (when it changed). True when a command or a thread is new."""
        ver = CODEX.version
        if ver == self._cx_ver:
            return False
        self._cx_ver = ver
        changed, roots, seen = False, set(), set()
        for e in self._codex_entries():
            tid = e['id']
            if e.get('kind') not in ('root', 'sub'):
                continue
            root = CODEX.root_of(tid)
            if root is None:
                continue
            seen.add(tid)
            f = self.cx_files.get(tid)
            if f is not None and (f['tree'] != root or f['gen'] != e.get('gen', 0)):
                self._cx_forget(f)                                   # the rollout was written again (it shrank, or its start changed): what was read of the old one is not true
                f = None
            if f is None:
                f = self.cx_files[tid] = self._new_cx_file(e, root)
                changed = True
            f['entry'] = e
            f['owner'].first_ts, f['owner'].last_ts, f['owner'].cwd = e['meta_ts'], e['last_ts'], e['cwd'] or None
            if e.get('kind') == 'root':
                roots.add(tid)
            changed = self._cx_commands(f) or changed
        for tid in [t for t in self.cx_files if t not in seen]:
            self._cx_forget(self.cx_files.pop(tid))
            changed = True
        gaps = {tid: tuple(CODEX.gaps(tid)) + tuple((at, None, 'running') for at in CODEX.running(tid)) for tid in self.cx_files}
        for tid, g in gaps.items():
            old = self.cx_files[tid].get('gaps')
            if old != g:
                self.cx_files[tid]['gaps'] = g
                for start, end, _ in set(g) ^ set(old or ()):
                    self._cx_touch(start if start != -float('inf') else end)
                changed = True
        self._cx_gaps = None
        if changed:
            self._pub += 1
            ev = sorted((st, f['tree'], cw) for f in self.cx_files.values() for st, cw in f['events'])
            self._cx_ev = (ev, [x[0] for x in ev])
        if frozenset(roots) != self.cx_roots:
            self.cx_roots, changed = frozenset(roots), True
        return changed

    def _cx_forget(self, f):
        """What was made of a Codex thread's commands is dropped (its calls, spans, redirects, the texts held for the fingerprint) and every settled judgment is made again: a rollout that
        was written again may hold other commands under the same ids, or none."""
        self.out.drop(list(f['spans'].values()))
        self.tcache.drop_prefix(f['tid'])
        self._cx_dirty = float('-inf')

    def _cx_touch(self, t):
        """A fact of the Codex index at time `t` arrived (or went): a settled judgment of a child that started after it is not settled."""
        if t is not None and t == t and abs(t) != float('inf') and (self._cx_dirty is None or t < self._cx_dirty):
            self._cx_dirty = t

    def _cx_commands(self, f):
        """The new commands of one thread: indexed for the text pool, and read for launches when one names a tool or a script. True when there were new ones."""
        tid = f['tid']
        cmds = CODEX.cmds(tid)
        if not cmds or cmds[-1]['item_id'] == f['last_id']:
            return False
        fresh = []
        for c in reversed(cmds):
            if c['item_id'] in f['ids']:
                break
            fresh.append(c)
        if not fresh:
            f['last_id'] = cmds[-1]['item_id']
            return False
        for c in reversed(fresh):                                     # oldest first
            f['ids'].add(c['item_id'])
            f['tb'].insert(c['start'], len(f['items']), 0)
            f['items'].append(c['item_id'])
            f['events'].append((c['start'], os.path.normpath(c['cwd']) if isinstance(c['cwd'], str) and c['cwd'] else None))
            self._cx_touch(c['start'])
            try:
                self._cx_command(f, c)
            except Exception as e:   # noqa: BLE001 — one command that cannot be read must not stop the rest
                line_error(tid, None, e)
        f['last_id'] = cmds[-1]['item_id']
        return True

    def _cx_command(self, f, c):
        """One finished command of a Codex thread, as a launching call (when it can launch) and as a call the Codex rules look at (`codex exec`)."""
        text = c['cmd'] if c['cmd'] is not None else CODEX.cmd_text(f['tid'], c['item_id'])
        if not isinstance(text, str):
            return                                                     # no text (not a shell command, over the limit, not kept): its time is a gap
        cwd, tid, item = c['cwd'], f['tid'], c['item_id']
        scripts = bash_scripts(text, cwd) if (f['node'] is None or SCRIPTY_RE.search(text)) else []
        if 'codex' in text or scripts:
            cc = cx_parse_call(c['start'], item, {'command': text}, cwd, scripts=scripts)
            if cc:
                cc['node'] = f['node']
                cc['span_end'] = c['end']
                f['calls'].append(cc)
        launches = []
        if (('claude' in text and (' -p' in text or '--print' in text)) or scripts):
            found = [(text, shell_code(text), None, cwd)] if 'claude' in text else []
            found += [(x['text'], x['code'], x['env'], x['cwd']) for x in scripts if 'claude' in x['text']]
            launches = [L for t, code, env, base in found for L in launch_facts(t, code, base, env, 'claude')]
        if launches or scripts or _launchy_call(text):
            self._make_call(f, item, c['start'], text, cwd, '', launches, None, lambda: self._cx_kinds(tid, item, cwd), end=c['end'], end_status='result',
                            msg_id=(c.get('exec') or (item,))[0])

    @staticmethod
    def _cx_kinds(tid, item, cwd):
        """launch_kinds of a Codex command (the text is read again from the index when it was dropped): both tools, assumed, when it cannot be read."""
        text = CODEX.cmd_text(tid, item)
        return launch_kinds(text, cwd) if isinstance(text, str) else (frozenset(), BOTH_TOOLS)

    def _cx_text(self, f, item):
        """The normalised text of one command of a thread (cut at LINE_BYTES, which ends it with fp.CUT); '' when the index does not hold it."""
        key = (f['tid'], item)
        t = self.tcache.get(key)
        if t is not None:
            return t
        raw = CODEX.cmd_text(f['tid'], item)
        if not isinstance(raw, str):
            return ''
        head = fp.clip(raw, fp.LINE_BYTES)
        t = fp.normalize(head)
        t = t + fp.CUT if len(head) < len(raw) and t else t
        self.tcache.put(key, t)
        return t

    def _cx_pool(self, f, lo, hi):
        """(normalised text, complete) of the commands of a thread that started between two times, newest first, at most OWNER_BYTES. Not complete when a command
        in the window has no text here (it was not kept) as well as when the window was cut."""
        idx = f['tb']
        i, j = bisect.bisect_left(idx.ts, lo), bisect.bisect_right(idx.ts, hi)
        if i >= j:
            return '', True
        texts, size, missing = [], 0, False
        for x in sorted(range(i, j), key=lambda k: -idx.ts[k]):
            t = self._cx_text(f, f['items'][idx.off[x]])
            if not t:
                missing = True
                continue
            texts.append(t)
            size += fp.nbytes(t)
            if size > fp.OWNER_BYTES:
                break
        pool, complete = fp.join_pool(texts)
        return pool, complete and not missing

    def _cx_dead(self, root_entry):
        """Whether nobody has the rollout of a root thread open and that can be told (the open files are known: /proc): the process of the thread is gone (None: not known)."""
        opened = cx_open(root_entry['path'], root_entry['id'], cx_procs())
        return None if opened is None else not opened

    def _cx_gap_map(self):
        """{thread id: [(start, end, why)]}: where the commands of a thread may be missing (and what says so, see CODEX.gaps; `running`: a command that runs now in an open turn, CODEX.running), for the threads that have such a place. A turn that never ended in a thread nobody has open
        (its process is gone, nothing writes to it any more) ends at the last time the thread was written; the process of a sub-agent is its root's. A gap that starts at the very
        beginning of what is known (-inf: only the end of a big file was read) starts when the thread did."""
        if self._cx_gaps is not None:
            return self._cx_gaps
        out, dead = {}, {}
        for tid, f in self.cx_files.items():
            g = f.get('gaps')
            if not g:
                continue
            e = f['entry']
            cap = None
            if any(why == 'turn' and end is None for _, end, why in g):
                root = f['tree']
                if root not in dead:
                    dead[root] = self._cx_dead(self.cx_files[root]['entry']) if root in self.cx_files else None
                cap = e['last_ts'] if dead[root] and e['last_ts'] else None
            floor = e['meta_ts'] if e['meta_ts'] is not None else float('-inf')
            out[tid] = [(floor if start == float('-inf') else start, cap if (end is None and why in ('turn', 'running') and cap is not None) else end, why) for start, end, why in g]
        self._cx_gaps = out
        return out

    def _cx_blind_for(self, t, trust_free=True, turns=True):
        """The (tree, node) of the Codex threads that may have run a command their facts do not hold when the thread `t` began, in its folder or above it (not its own tree; none for a
        thread whose environment was read and holds no name of Codex while something says why: lineage.codex_free, unless `trust_free` is False)."""
        if trust_free and self.lineage.codex_free.get(t['id']) is True:
            return set()
        lo, hi = t['meta_ts'] - affil.RUN_GRACE, t['meta_ts'] + affil.RUN_SLACK_BEFORE
        return {(f['tree'], f['node']) for f in self.cx_files.values()
                if f['tree'] != t['id'] and self._cx_blind(f, lo, hi, turns) and affil._under(t['cwd'] or None, f['owner'].cwd)}

    def _cx_blind(self, f, lo, hi, turns=True):
        """Whether the thread may have run a command between two times that its facts do not hold. An open turn is a place where one may be missing (a command that runs is written when it
        ends); with `turns` False it is left out: whether a thread whose commands are all there started something is asked of the commands that run, not of the turn being open."""
        return any(s <= hi and (e is None or e >= lo) for s, e, why in self._cx_gap_map().get(f['tid'], ()) if turns or why != 'turn')

    # ---------- the judgment ----------
    def _finish(self, content=True):
        """Judges every child again with what is known now and publishes it. The judgment is made without self.lock (the requests that read the links wait
        for nothing); the results are put in place together, under the lock, at the end."""
        res = self._link_cli(content)
        try:
            unlinked = self._unlinked(self._entries, res['cli_owners'], res['decisions'])
        except Exception as e:   # noqa: BLE001 — links keep running even if missed candidates cannot be found
            print('unlinked scan error', type(e).__name__, flush=True)
            unlinked = self.unlinked_map
        with self.lock:
            self.decisions, self.assignment, self.cli_owners, self.diags, self.unlinked_map = res['decisions'], res['assignment'], res['cli_owners'], res['diags'], unlinked
            self._pub += 1
        self._remember(res['decisions'])
        self._dirty = False
        self._finished_at = time.time()
        self._pins_sig = self._signature()

    def _signature(self):
        """What outside the records the judgment depends on: the lineage (identity, version, size) and which processes were alive in the last look."""
        ln = self.lineage
        return (id(ln), ln.version, len(ln.cli), len(ln.saved), frozenset(ln.live_sids), frozenset(ln.live_threads))

    def _owner_list(self):
        out = []
        for f in list(self.files.values()) + list(self.sub.values()):
            o = f['owner']
            ts, _ = self._head(f['path'])
            o.first_ts, o.last_ts = ts, f['mtime']
            out.append(o)
        out += [f['owner'] for f in self.cx_files.values()]
        return out

    def tree_kind(self, sid):
        """Whose record a tree id is: `codex` (a root thread) or `claude` (a session)."""
        return 'codex' if sid in self.cx_roots else 'claude'

    def _children(self):
        out = []
        for p, f in self.files.items():
            if self.entry.get(p) not in (None, 'sdk-cli'):
                continue                                         # a session a person opened is never a child
            ts, cwd = self._head(p)
            runs = f['rs'].runs if f['rs'] is not None else []
            out.append(affil.ChildFacts(f['sid'], runs[0].ts if runs else ts, cwd, self.entry.get(p), runs, self.lineage.codex_free.get(f['sid']) is True))
        return out

    def _pins_of(self, csid, known, cf=None):
        """The live process / environment / remembered link of a child as rank 2 evidence; only a parent that has a record can be shown."""
        w = self.lineage.pending.get(csid)
        if w and w['kind'] == 'cli':
            # the evidence of the environment is not a parent: the environment names the two providers and the chain does not say which is the direct parent, or it contradicts itself
            # (`CODEX_SESSION_ID` is not the root of `CODEX_THREAD_ID`): the trees it names are a tie at rank 2 (a remembered line of the same child does not count against it)
            if 'unknown' in w:
                return [{'kind': 'env', 'tree': None, 'ts': w.get('ts'), 'unknown': w['unknown']}]       # it names a Codex thread nothing is known of: nothing is claimed, and nothing firm is made of the text
            return [{'kind': 'env', 'tree': None, 'ts': w.get('ts'), 'conflict': w.get('conflict') or [(w['claude'], None), (w['codex'][0], w['codex'][1])]}]
        p = self.lineage.cli.get(csid)
        if not p or p['sid'] not in known:
            return []
        if p.get('exact') and cf is not None and not self._cx_env_fresh(p.get('node') or p['sid'], cf.t0):
            self._stale[csid] = p['sid']                              # a Codex environment older than the thread's turns: it is not about this child
            return []
        return [{'kind': p['rule'], 'tree': p['sid'], 'ts': p.get('ts'), 'node': p.get('node'), 'exact': bool(p.get('exact'))}]

    def _link_cli(self, content=True):
        """The judgment of every `claude -p` child (_judge), made again while a child whose two providers' environments name different parents gets its parent from the
        ownership chain (Lineage.resolve): the links are decided from the parents down, once more as long as something changed (at most MAX_NEST rounds)."""
        res = self._judge(content)
        for _ in range(MAX_NEST):
            if not self.lineage.pending or not self._resolve_pending(res):
                break
            res = self._judge(content)
        return res

    def _up_of(self, cli, cx):
        """up(id): the ids above a session or thread through links that are certain (the session or thread that started it, and what started that …, nearest first), read from the
        links of the `claude -p` children `cli` and the `codex exec` threads `cx` and the sub-agents' meta; a link that is only a guess is no part of it."""
        def parent(x):
            o = cli.get(x)
            if o and o['certain'] and o['sid'] != x:
                return o['sid']
            o = cx.get(x)
            if o and certain(o['rule']) and o['sid'] != x:
                return o['sid']
            e = CODEX.get(x)
            return e['parent'] if e and e.get('kind') == 'sub' else None

        def up(x):
            out, seen = [], {x}
            for _ in range(MAX_NEST):
                x = parent(x)
                if x is None or x in seen:
                    break
                out.append(x)
                seen.add(x)
            return out
        return up

    def _resolve_pending(self, res):
        """Gives the children with two environments their direct parent when the chain of certain links says it. The chain is read from this round's judgment, the Codex
        threads' links and the sub-agents' meta; a link that is only a guess is no part of it. True when a child was decided."""
        up = self._up_of(res['cli_owners'], self.owners)
        if not self.lineage.resolve(up):
            return False
        self._link_codex()                                       # a Codex thread that was decided changes who started what
        return True

    def _judge(self, content=True):
        """claude -p child → the session (and node, and call) that launched it: board/affil.py decides from the facts gathered above. A child decided by the
        process lineage keeps that link as rank 2 evidence; an output file or the content can be firmer or can contradict it (reported as a diagnostic).
        A child whose last run started more than SETTLE seconds ago is judged once and remembered (nothing that happens later can change what was running
        when it started); a recent one is judged again each time. A child whose judgment raises is left unlinked and said (a diagnostic, a log line with its
        session id); the others are judged as usual. Returns the results, publishes nothing: {decisions, assignment, cli_owners, diags}."""
        if self.out.refresh():
            self._memo.clear()
        if self._cx_dirty is not None:
            # a command or a gap of a Codex thread arrived (or went) at some time: a settled judgment of a child that started after it saw less than there is now (a
            # command is written when it ends, so one can come long after the child began)
            dirty, self._cx_dirty = self._cx_dirty, None
            for csid in [c for c, (k, _) in self._memo.items() if k[1] is None or k[1] >= dirty - 2]:
                del self._memo[csid]
        self._cx_gaps = None
        self._stale = {}
        owners = self._owner_list()
        known = {f['sid'] for f in self.files.values()} | self.cx_roots
        up = self._up_of(self.cli_owners, self.owners)                 # (the links of the last judgment: a child's own parent is older than the child)
        decisions, failed, now = {}, [], time.time()
        redo = []
        for cf in self._children():
            pins = self._pins_of(cf.sid, known, cf)
            saved = self.lineage.saved.get(cf.sid)
            last = cf.runs[-1].ts if cf.runs else cf.t0
            key = (len(cf.runs), last, tuple((p['kind'], p['tree'], p.get('ts'), p.get('node'), p.get('exact'), tuple(p.get('conflict') or ()), p.get('unknown')) for p in pins),
                   saved and (saved['tree'], saved['node']), content, len(owners), cf.codex_free)
            hit = self._memo.get(cf.sid)
            if hit is not None and hit[0] == key:
                dec = hit[1]
            else:
                try:
                    dec = affil.decide(cf, owners, pins, self.out, saved, content, up)
                except Exception as e:   # noqa: BLE001 — one child that cannot be judged must not stop the others
                    line_error(cf.sid, None, e)
                    failed.append(cf)
                    decisions[cf.sid] = affil.Decision(cf)
                    continue
                if dec.tree is not None and dec.tree not in known:
                    dec = affil.Decision(cf)                      # the launching session has no record to show it in
                if last is not None and now - last > SETTLE and not dec.incomplete:
                    self._memo[cf.sid] = (key, dec)
                elif dec.incomplete:
                    redo.append((cf, pins, saved))
            decisions[cf.sid] = dec
        if redo:
            # a comparison left incomplete by a thread nobody can read: whether that thread is above the winner's tree is known from this round's links, which the first look did not have
            up = self._up_of({c: {'sid': d.tree, 'certain': d.certain} for c, d in decisions.items() if d.tree is not None}, self.owners)
            for cf, pins, saved in redo:
                try:
                    dec = affil.decide(cf, owners, pins, self.out, saved, content, up)
                except Exception as e:   # noqa: BLE001
                    line_error(cf.sid, None, e)
                    continue
                if dec.tree is not None and dec.tree not in known:
                    dec = affil.Decision(cf)
                decisions[cf.sid] = dec
        self._share_calls(decisions)
        asg = affil.assign_launches([((csid, k), dec.run_ts[k], dec.calls[k], cl) for csid, dec in decisions.items() for k, cl in enumerate(dec.run_claims)])
        heads = {f['sid']: self._head(p) for p, f in self.files.items()}
        owners_out, diags = {}, []
        for csid, dec in decisions.items():
            for code, detail in dec.diags:
                diags.append(dict(detail, code=code, subject=csid, tree=dec.tree, node=dec.node))
            if dec.tree is None:
                continue
            # the call of a run: the one the evidence names, else the one the launches count to (FIFO) - but not for a run whose tree a live process, an environment or a remembered line settled (rank 2) and
            # whose text names no call: counting would hand it a call that another child's launch is waiting for (the launches are counted for the orphans, not to give a child a title)
            calls = [c if (csid, k) in asg.named or (dec.run_ranks[k] if k < len(dec.run_ranks) else None) != 2 else None
                     for k, c in ((k, asg.call_of.get((csid, k))) for k in range(len(dec.run_claims)))]
            call = calls[0] if calls else None                  # for the spawn time and description
            pin = self.lineage.cli.get(csid) or {}
            readers = [l for l in (call.launches if call else []) if l.reader]
            cw = decisions[csid].child.cwd
            L = next((l for l in readers if l.cwd and cw and os.path.normpath(l.cwd) == os.path.normpath(cw)), readers[0] if readers else None)
            first = (decisions[csid].child.runs[0].ts if decisions[csid].child.runs else heads.get(csid, (None,))[0])
            owners_out[csid] = {'sid': dec.tree, 'node': dec.node, 'parent_kind': self.tree_kind(dec.tree), 'rule': dec.rule, 'by': list(dec.by), 'certain': bool(dec.relation.certain.get('tree')),
                                'bash_ts': call.span.start if call else (pin.get('ts') or heads.get(csid, (None,))[0] or time.time()),
                                'bash_desc': call.desc if call else '', 'call': dec.call.span.call_id if dec.call else None,
                                'calls': [c.span.call_id if c else None for c in calls], 'calls_certain': [(csid, k) in asg.named for k in range(len(calls))],
                                'dt': round(first - call.span.start, 3) if call and first else None,
                                'cwd': (L.cwd if L else None) or pin.get('cwd')}
        diags = diags[:2000]
        for cf in failed:
            diags.append({'code': 'parse_errors', 'subject': cf.sid, 'tree': None, 'node': None, 'n': 1, 'what': 'judgment'})
        for f in list(self.files.values()) + list(self.sub.values()):
            if f.get('unreadable'):
                diags.append({'code': 'parse_errors', 'subject': f['sid'], 'tree': f['tree'], 'node': f['node'], 'n': 1, 'what': 'unreadable'})
        diags += self._orphans(asg)
        diags += [{'code': 'evidence_conflict', 'subject': c, 'tree': None, 'node': None, 'other': t} for c, t in list(self._stale.items()) + list(self._stale_cx.items())]
        for tid, o in list(self.owners.items()):                                  # what the judgment of a `codex exec` thread left open
            if o.get('node_unresolved'):
                diags.append({'code': 'node_unresolved', 'subject': tid, 'tree': o['sid'], 'node': None})
            if o.get('incomplete'):
                diags.append({'code': 'fingerprint_incomplete', 'subject': tid, 'tree': o['sid'], 'node': o.get('node')})
            if o.get('stale_pin'):
                diags.append({'code': 'evidence_conflict', 'subject': tid, 'tree': o['sid'], 'node': None, 'other': o['stale_pin']})
        return {'decisions': decisions, 'assignment': asg, 'cli_owners': owners_out, 'diags': diags}

    def _share_calls(self, decisions):
        """A call can start only as many children as its launches allow (a loop: its count). Of the children that were tied to one call by a guess (the time
        rule or a short instruction: ranks 5 and 4), the earliest ones up to that count keep the link; the rest are held."""
        by_call = {}
        for csid, dec in decisions.items():
            if dec.tree is not None and dec.kind in ('time', 'content_short') and dec.call is not None:
                by_call.setdefault(id(dec.call), []).append(dec)
        for lst in by_call.values():
            call = lst[0].call
            if not call.launches:
                continue                                          # no launch was found in it (a script nobody could read): how many children it starts is not known
            cap = max(1, sum(l.n for l in call.launches if l.reader and l.persist))
            if len(lst) <= cap:
                continue
            lst.sort(key=lambda d: (d.child.runs[0].ts if d.child.runs else d.child.t0 or 0, d.child.sid))
            for dec in lst[cap:]:
                held = affil.Decision(dec.child)                  # a new object: the remembered judgment stays as it was
                held.held, held.held_trees, held.claims = 'ambiguous', (dec.relation.tree,), list(dec.claims)
                held.run_claims, held.run_ts, held.calls = [list(c) for c in dec.run_claims], list(dec.run_ts), [None] * len(dec.calls)
                decisions[dec.child.sid] = held

    def _orphans(self, asg):
        """A launch no child is accounted to is only counted, as the diagnostic `orphan_launch` (no card, no seat). The count is what can be checked:
        every `claude -p` the command reader found in a call that ran (a loop counts as many as its known count), minus the runs accounted to that call
        (affil.assign_launches: a run is accounted to one launch, the one the evidence names or the one its launches can still give it). A launch that asked
        for no session record counts at once. A call that ended counts after ORPHAN_GRACE seconds (a child may still be starting); a call still running counts
        one launch, and only when it has no child at all after ORPHAN_OPEN seconds (the later iterations of a sequential loop have not started yet, and an
        unknown loop count is never guessed). The launches whose arguments or folder refute a child are the ones left over, so a stranger never accounts for
        them. When a run fits two launches (two sessions that said the same words) one of them has no child and the records do not say which: both owners
        say `orphan_launch` with `ambiguous`. -> the diagnostics."""
        now = time.time()
        sure, maybe = collections.Counter(), collections.Counter()
        for f in self._all_files():
            me = (f['tree'], f['node'])
            for call in f['launch_calls']:
                s_ = call.span
                readers = call.launches                              # the weak ones (reader False) count as one launch each
                sure[me] += sum(l.n for l in readers if not l.persist)
                known = sum(l.n for l in readers if l.persist and l.n < LOOP_UNKNOWN)
                unknown = any(l.persist and l.n >= LOOP_UNKNOWN for l in readers)
                got = asg.filled(call)
                lack = 0
                if s_.end is not None:
                    if now - s_.end > ORPHAN_GRACE:
                        lack = max(0, known - got) + (1 if unknown and not got and not known else 0)
                elif not got and (known or unknown) and now - s_.start > ORPHAN_OPEN:
                    lack = 1
                if not lack:
                    continue
                who = {(c.span.owner_tree, c.span.owner_node) for c in asg.reach(call)} | {me}
                if len(who) == 1:
                    sure[me] += lack
                else:
                    for w in who:
                        maybe[w] += lack
        threads = [e for e in self._entries if e['origin'] == 'exec' and not e['guardian'] and e['meta_ts']]
        for f in self.cx_files.values():                              # the `codex exec` launches of the Codex threads (the Claude sessions' are not counted)
            for c in f['calls']:
                sure[(f['tree'], f['node'])] += self._cx_lack(f, c, threads, now)
        out = []
        for tree, node in sorted(set(sure) | set(maybe), key=lambda k: (k[0], k[1] or '')):
            n, m = sure[(tree, node)], maybe[(tree, node)]
            if n or m:
                out.append(dict({'code': 'orphan_launch', 'subject': tree, 'tree': tree, 'node': node, 'n': n + m}, **({'ambiguous': True} if m else {})))
        return out

    @staticmethod
    def _cx_lack(f, c, threads, now):
        """How many of the `codex exec` launches of one command of a Codex thread have no thread behind them: the launches the command reader placed (a loop counts as many as its
        known count), less the threads that could have been started by it (they began in its time, in its folder, and its literal instruction does not refute them: a thread
        that was not linked is still a child of the call, so a launch is not called childless on account of a link that is missing). A command that is over counts after
        ORPHAN_GRACE seconds. A `&` command of a Codex shell takes the run down with the call: no thread was ever written, which is what this finds."""
        launches = [x for x in c['L'] if x['resume'] is None]
        end = c.get('span_end')
        if not launches or end is None or now - end <= ORPHAN_GRACE or not c['ts']:
            return 0
        known = sum(x['n'] for x in launches if x['n'] < LOOP_UNKNOWN)
        unknown = any(x['n'] >= LOOP_UNKNOWN for x in launches)
        got = sum(1 for t in threads if t['id'] != f['tree'] and any(
            (0 <= t['meta_ts'] - c['ts'] <= CX_WINDOW or cx_running(c, t['meta_ts'])) and (not x['cwd'] or os.path.normpath(t['cwd'] or '') == x['cwd']) and not cx_differs(x, t)
            for x in launches))
        return max(0, known - got) + (1 if unknown and not got and not known else 0)

    def _remember(self, decisions):
        """A link that rests on an output file or on a long instruction is kept (id, rule, time; the node id when it is a sub-agent) in the link cache."""
        for csid, dec in decisions.items():
            if dec.tree is None or dec.kind not in ('out', 'content', 'resume') or not dec.certain:
                continue
            try:
                self.lineage.remember(csid, dec.tree, dec.node, dec.rule, dec.child.t0, pk=self.tree_kind(dec.tree))
            except Exception as e:   # noqa: BLE001
                print('link cache remember error', type(e).__name__, flush=True)

    def unlinked_for(self, sid):
        """`claude -p` (sdk-cli) sessions and Codex exec threads that started shortly after this session's Bash call but could not be linked to any session (at most UNLINKED_MAX, most recent first)."""
        return [dict(x) for x in self.unlinked_map.get(sid, ())]

    def _bash_events(self):
        """Every Bash call's (time, session id, cwd) in time order. The calls of a record only grow, so a scan that finds a few new ones puts them into the
        list in place; it is built again from scratch when a record went away or shrank, or when many calls came at once."""
        seen, grown, total = self._ev_seen, [], 0
        rebuild = len(seen) > len(self.files) or any(p not in self.files for p in seen)
        for p, f in self.files.items():
            k = len(f.get('bash', ()))
            total += k
            was = seen.get(p, 0)
            if k < was:
                rebuild = True
            elif k > was:
                grown.append((p, f, was, k))
        if self._ev[2] == total and not rebuild:
            return self._ev
        ev, times, _ = self._ev
        if rebuild or self._ev[2] < 0 or sum(k - was for _, _, was, k in grown) > BASH_EVENTS_INSERT_MAX:
            ev = sorted((ts, f['sid'], cwd) for f in self.files.values() for ts, cwd in f.get('bash', ()))
            times = [e[0] for e in ev]
        else:
            for p, f, was, k in grown:
                for ts, cwd in f['bash'][was:k]:
                    e = (ts, f['sid'], cwd)
                    i = bisect.bisect_right(ev, e)
                    ev.insert(i, e)
                    times.insert(i, ts)
        self._ev_seen = {p: len(f.get('bash', ())) for p, f in self.files.items()}
        self._ev = (ev, times, total)
        return self._ev

    def _unlinked(self, entries, cli_owners=None, decisions=None):
        """Missed candidates: for each `claude -p` (sdk-cli) session or Codex exec thread X that is linked to no session, find the session S that called Bash up to W seconds before X started.
        `ambiguous`: the evidence tied (two launches or two owners fit it equally), reported under every session that tied. Otherwise, only when S called Bash in the same project as X
        (the working folders are the same or one is inside the other): if X's process has never been seen, ended_before_seen (only learned of it after it ended),
        and if it is alive but could not be linked, no_matching_call. If there are only runs started in another folder, it is not that session's share.
        At most UNLINKED_MAX per session, and only those within UNLINKED_DAYS days. `cli_owners` and `decisions`: the judgment just made (the published ones by default)."""
        cli_owners = self.cli_owners if cli_owners is None else cli_owners
        decisions = self.decisions if decisions is None else decisions
        now = time.time()
        floor = now - UNLINKED_DAYS * 86400
        by_sid = {os.path.basename(p)[:-6]: p for p in self.files}
        files = {f['sid']: f for f in self.files.values()}
        cands = []
        for sid, p in by_sid.items():
            if sid in cli_owners:
                continue
            ts, cwd = self._head(p)
            if ts and ts >= floor and self.entry.get(p) == 'sdk-cli':
                cands.append(('claude', sid, ts, cwd, sid in self.lineage.live_sids, CLI_WINDOW))
        for e in entries:
            if e['origin'] == 'exec' and not e['guardian'] and e['meta_ts'] and e['meta_ts'] >= floor and e['id'] not in self.owners:
                cands.append(('codex', e['id'], e['meta_ts'], os.path.normpath(e['cwd']) if e['cwd'] else None, e['id'] in self.lineage.live_threads, CX_WINDOW))
        ev, tss, _ = self._bash_events()
        out = {}
        for provider, xid, start, xcwd, alive, window in cands:
            held = decisions.get(xid).held_trees if provider == 'claude' and xid in decisions else ()
            if held:
                for tree in held:
                    out.setdefault(tree, []).append({'id': xid, 'provider': provider, 'started': start, 'cwd': short_path(xcwd or ''), 'reason': 'ambiguous'})
                continue
            near = {}
            for k in range(bisect.bisect_left(tss, start - window), bisect.bisect_right(tss, start)):
                if ev[k][1] != xid:
                    near.setdefault(ev[k][1], []).append(ev[k][2])
            cev, ctss = self._cx_ev                                              # the commands of the Codex threads: a thread of Codex is a launcher too
            for k in range(bisect.bisect_left(ctss, start - window), bisect.bisect_right(ctss, start)):
                if cev[k][1] != xid:
                    near.setdefault(cev[k][1], []).append(cev[k][2])
            for sid, cwds in near.items():
                fs = [files[sid]] if sid in files else [f for f in self.cx_files.values() if f['tree'] == sid]
                if provider == 'claude':
                    launches = [l['cwd'] for f in fs if 'cli' in f for l in f['cli'] if l['ts'] and 0 <= start - l['ts'] <= window]
                    launches += [L.cwd for f in fs if f.get('provider') == 'codex' for call in f['launch_calls'] if 0 <= start - call.span.start <= window for L in call.launches]
                else:
                    launches = [L['cwd'] for f in fs for c in f['calls'] if c['ts'] and 0 <= start - c['ts'] <= window for L in c['L'] if L['resume'] is None]
                if launches:
                    if not any(cw is None or cw == xcwd for cw in launches):
                        continue                                                  # only runs started in another folder: not this session's share
                    reason = 'ambiguous'                                          # a launch fits but could not take it (vetoed, or out of shares)
                elif any(_related_dir(cw, xcwd) for cw in cwds):
                    reason = 'no_matching_call' if alive else 'ended_before_seen'
                else:
                    continue
                out.setdefault(sid, []).append({'id': xid, 'provider': provider, 'started': start, 'cwd': short_path(xcwd or ''), 'reason': reason})
        for sid in out:
            out[sid] = sorted(out[sid], key=lambda x: -x['started'])[:UNLINKED_MAX]
        return out

    def cli_owned_by(self, sid):
        with self.lock:
            return {c: o for c, o in self.cli_owners.items() if o['sid'] == sid}

    def cli_owner(self, csid):
        return self.cli_owners.get(csid)

    def cli_count(self, sid):
        return sum(1 for o in list(self.cli_owners.values()) if o['sid'] == sid)

    # ---------- who started whom: one graph of sessions and threads ----------
    # Every link there is, in one place: a `claude -p` child (kind `cli`), a `codex exec` thread (`cx`), a native sub-agent thread of Codex (`sub`). The vertices are
    # sessions and threads; a Claude Agent-tool sub-agent is no vertex (it is the `node` of a link: the page reads its folder).
    def _graph(self):
        """(version, {id: {parent, node, kind, rule, certain}}, {parent id: [child id]}) made again when the links or the Codex index changed. The three are taken together under the
        lock and never changed afterwards, so a reader that uses only this tuple sees one graph, whatever is published meanwhile. The version is a whole number that rises each time
        the graph is made again."""
        with self.lock:
            # the whole number rises with every publication; the two dicts themselves also count, so that a reader that puts a dict of links in place by hand (a test, a tool) is seen too. They
            # are compared as objects and kept (`_graph_src`), not by their address: the address of a dict that was freed is given to the next one, which then looks like it
            key, src = (self._pub, CODEX.version), (self.cli_owners, self.owners)
            if self._graph_key == key and self._graph_src[0] is src[0] and self._graph_src[1] is src[1]:
                return self._graph_state
            g = {}
            for csid, o in src[0].items():
                g[csid] = {'parent': o['sid'], 'node': o.get('node'), 'kind': 'cli', 'rule': o['rule'], 'certain': bool(o['certain'])}
            for tid, o in src[1].items():
                g[tid] = {'parent': o['sid'], 'node': o.get('node'), 'kind': 'cx', 'rule': o['rule'], 'certain': certain(o['rule'])}
            for tid, f in self.cx_files.items():
                e = f['entry']
                if e.get('kind') != 'sub' or not e.get('parent'):
                    continue
                started = [c['call_id'] for c in CODEX.collab(e['parent']) if c['kind'] == 'started' and c['agent_thread_id'] == tid]
                g[tid] = {'parent': e['parent'], 'node': None, 'kind': 'sub', 'rule': 'subagent', 'certain': True,
                          'call': started[0] if len(started) == 1 else None, 'call_certain': len(started) == 1}
            kids = {}
            for cid, info in g.items():
                kids.setdefault(info['parent'], []).append(cid)
            self._graph_key, self._graph_src, self._graph_state = key, src, (self._graph_state[0] + 1, g, kids)
            return self._graph_state

    def edges(self):
        """(version, {id: {parent, node, kind, rule, certain[, call, call_certain]}}): every link of the graph, read only (a view of what the graph is made of: do not change the values).
        The version is a whole number that rises each time the graph was made again (the links were published, a Codex thread changed): a reader that keeps what it made from the edges
        asks again when the version is not the one it saw. The version and the edges are of the same graph."""
        ver, g, _ = self._graph()
        return ver, types.MappingProxyType(g)

    def owner_of(self, id):
        """Who started a session or thread: {parent (the session or thread above it), node (the agent inside the parent that did: a Claude sub-agent `a…` or a Codex sub-agent
        thread, None for the parent itself), kind (`sub`: a native Codex sub-agent, `cli`: a `claude -p` child, `cx`: a `codex exec` thread), rule, certain}, a copy; None when
        nothing is known of who started it (a session or thread a person opened). A `sub` also has `call` (the call that spawned it, from the parent's record) and `call_certain`."""
        o = self._graph()[1].get(id)
        return dict(o) if o else None

    def page_of(self, id):
        """The session or thread at the top of the links above `id` (the page its card is shown on); `id` itself when nothing is above it or the links go round in a circle. At most
        MAX_NEST links up."""
        g = self._graph()[1]
        x, seen = id, {id}
        for _ in range(MAX_NEST):
            o = g.get(x)
            if not o:
                return x
            x = o['parent']
            if x in seen:
                return id
            seen.add(x)
        return x

    def descendants(self, page):
        """Every session and thread started from `page`, directly or through others (down the links, at most MAX_NEST deep), parents before their children, each once (the sub-agents
        of a thread first, then the `codex exec` threads, then the `claude -p` children, each by id). A circle is cut."""
        _, g, kids = self._graph()
        out, seen = [], {page}
        level = [page]
        for _ in range(MAX_NEST):
            nxt = []
            for p in level:
                for c in sorted(kids.get(p, ()), key=lambda c: (('sub', 'cx', 'cli').index(g[c]['kind']), c)):
                    if c not in seen:
                        seen.add(c)
                        out.append(c)
                        nxt.append(c)
            if not nxt:
                break
            level = nxt
        return out

    def _line(self, f, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(d, dict) or d.get('type') != 'assistant':
            return False
        got = False
        for b in _blocks(d):
            if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                got = self._call(f, d, b, None) or got
        return got

    def _call(self, f, d, b, scripts):
        c = cx_parse_call(parse_ts(d.get('timestamp')), b.get('id'), _input(b), d.get('cwd'), scripts=scripts)
        if c:
            c['node'] = f.get('node')                          # the sub-agent whose record holds the call (None: the main session)
            f['calls'].append(c)
        return bool(c)

    def owned_by(self, sid):
        with self.lock:
            return {tid: o for tid, o in self.owners.items() if o['sid'] == sid}

    def owner(self, tid):
        return self.owners.get(tid)

    def count(self, sid):
        return sum(1 for o in list(self.owners.values()) if o['sid'] == sid)


LINKS = LinkIndex()
LINKS.deep_inline = False       # the server's index: the first scan returns after stage 1 and the deep pass runs in the background
