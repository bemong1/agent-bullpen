"""State of a single agent (Agent: Claude sub-agent or claude -p; CodexAgent: Codex thread) and the helper functions both share (tool categories, stall judgment, name numbering, Codex token sync)."""

import bisect
import collections
import dataclasses
import hashlib
import json
import os
import re
import threading

from . import runstate as RS
from .facts import CmdWindow, ReadEvent, WriteEvent
from .util import as_text, norm_key, parse_ts, short_path, strip_reminders, trunc
from .tokens import TokenMeter
from .codex_parse import CX_CALL_ID_RE, codex_call, codex_exec_command, codex_say_text, codex_user_text, cx_broken, cx_decode, cx_head, cx_hole, cx_usage_add, cx_write_item_of
from .codex_facts import file_path, shell_command
from .codex_scan import FrontScan
from .codex_index import CODEX


COORD_PREFIX = 'The coordinator sent a message while you were working:'
TURNS_KEEP = 200     # number of turns CodexAgent keeps for the detail view
WINDOWS_KEEP = 2000        # command windows kept per agent (the newest); the span of the ones let go is `windows_dropped`
ORCH_WINDOWS_KEEP = 4000   # the same for the orchestrator of a session
OTHER_WRITES_KEEP = 500    # write events on a path that is not markdown, kept per author (the newest); the span of the ones let go is `writes_dropped`
MD_EVENTS_MAX = 20000      # markdown write events kept per author: a markdown event is never let go for the count of the other ones; past this the later ones are not kept and `lost` says so
READS_KEEP = 20000         # read events kept per author (the newest)
PENDING_KEEP = 2048        # calls kept while they wait for their result
INF = float('inf')


def tool_category(name):
    # Codex is classified by the inner tool name inside exec (JS), decided at call time. The same rule as a Claude Bash `cat` counting as bash
    if name in ('Read', 'Grep', 'Glob', 'view_image'):
        return 'read'
    if name in ('Write', 'Edit', 'NotebookEdit', 'apply_patch'):
        return 'write'
    if name in ('Bash', 'exec_command', 'write_stdin'):
        return 'bash'
    if name in ('WebFetch', 'WebSearch', 'web__run'):
        return 'web'
    if name in ('SendMessage', 'SubagentHandback', 'Agent'):
        return 'msg'
    return 'other'


TOOL_TEXT_KEY = {'SubagentHandback': 'event.tool.handback'}     # tools whose summary is the board's own wording, not the agent's data: the page words them from the dictionary


def tool_brief(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == 'Bash':
        d = inp.get('description') or ''
        cmd = (inp.get('command') or '').strip().splitlines()
        return d or (cmd[0] if cmd else '')
    if name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        return short_path(inp.get('file_path') or inp.get('notebook_path'))
    if name in ('Grep', 'Glob'):
        return inp.get('pattern') or ''
    if name == 'WebFetch':
        return inp.get('url') or ''
    if name == 'WebSearch':
        return inp.get('query') or ''
    if name == 'SendMessage':
        return '→ %s: %s' % (inp.get('to', ''), inp.get('summary') or trunc(inp.get('message'), 60))
    if name == 'Agent':
        return inp.get('description') or ''
    if name == 'SubagentHandback':
        return '최종 보고 전달'
    if name == 'TaskStop':
        return inp.get('task_id') or inp.get('shell_id') or ''
    if name == 'ToolSearch':
        return inp.get('query') or ''
    return trunc(json.dumps(inp, ensure_ascii=False), 80)


CD_WORD_RE = re.compile(r'\b(?:cd|pushd|popd)\b')
ASSIGN_WORD_RE = re.compile(r'[A-Za-z_]\w*=')
SHELL_MOVES_MAX = 64


SURE_BEFORE = frozenset(('cd', 'pushd', 'popd', 'mkdir', 'export', 'set'))     # a command after which `&&` leaves the next one as sure as it was: it only moves, or makes what the next one needs
HEREDOC_OP_RE = re.compile(r'\d*<<(?!<)(-?)')
HEREDOC_QUOTE_RE = re.compile(r'''['"\\]''')
FLOW_OP_RE = re.compile(r'&&|\|\||\|&|;;|;|&|\||\n|\)')
COMPOUND_OPEN = {'if': 'if', 'while': 'loop', 'until': 'loop', 'case': 'case', 'for': 'for', 'select': 'for'}
COMPOUND_CLOSE = {'fi': ('if',), 'done': ('loop', 'for'), 'esac': ('case',), '}': ('group', 'func')}
BODY_OPEN = {'then': ('if',), 'elif': ('if',), 'else': ('if',), 'do': ('loop', 'for')}
LEAVES = {'exit': 4, 'exec': 4, 'return': 2, 'break': 1, 'continue': 1}        # after one of these the commands that follow in the list may not run: 4 it leaves the shell, 2 the function, 1 the pass of the loop
STOPS_AT = {'loop': 1, 'for': 1, 'func': 3, 'sub': 7, 'csub': 7}                             # the frames the bits of LEAVES stop at: a `break` leaves a loop, a `return` a function, `exit` a subshell (a function that exits is not told from the shell)


L_KEYWORDS = frozenset(('if', 'then', 'elif', 'else', 'fi', 'while', 'until', 'do', 'done', 'case', 'esac', 'for', 'select', 'in', '!', '{', '}'))


class _Flow(object):
    """What the shell text read so far says of the next command: whether it is run whatever the commands before it did (`sure`). The text is read as lists of commands
    (`;` a new line a lone `&` start the list again) inside the frames a compound command makes: `{ }` and a function, `( )`, `if`, a loop, `case`. A frame that is not
    sure to run (after `||`, after `&&` that depends on a command that decides something, the body of a function, of `if`, `while`, `case`, or of a `for` over nothing or over a
    list that may be empty) keeps every command in it not sure to its end, however the list inside restarts. A command that leaves the shell or the pass of a loop makes the rest of
    its frame not sure, and the frames around it. What cannot be read (a `}` with no `{`, a brace that is a word of a command, `for ((`) makes everything after it not sure."""

    def __init__(self):
        self.chain_ok = self.prev_safe = True              # chain_ok: this and-or list so far runs its next command; prev_safe: the command before it is one of SURE_BEFORE
        self.frames = [{'kind': 'root', 'base': True, 'body': True, 'dead': 0, 'saved': True}]       # base: the list inside starts as sure as this; dead: bits of LEAVES, a command in this frame left
        self.func_next = False                             # the next frame is the body of a function
        self.poisoned = False
        self.last_closed = None                            # the kind of the frame the last close took off
        self.trace = None                                  # a list when the caller wants the structure read: ('op', op) ('open', kind, word) ('close', kind) ('body', word) ('neg',) ('cmd', n) ('poison',)

    def _t(self, *item):
        if self.trace is not None:
            self.trace.append(item)

    def sure(self):
        return self.chain_ok and not self.poisoned

    def op(self, op):
        self._t('op', op)
        if op == '&&':
            self.chain_ok = self.chain_ok and self.prev_safe
        elif op == '||':
            self.chain_ok = False
        elif op == ')':
            self.close_paren()
        elif op not in ('|', '|&'):                         # `;` `;;` a new line `&`: the list starts again, as sure as the frame it is in
            top = self.frames[-1]
            self.chain_ok, self.prev_safe = top['base'] and not top['dead'], True

    def open(self, kind, body=False, runs=True, word=False):
        """A frame whose first list is as sure as the command that opens it (and not sure at all when `runs` is false: the commands of a `case` are in its branches); `body`: whether
        its body is sure when it comes (after `then`, `do`). A group that follows the head of a function is the body of that function."""
        base = self.chain_ok and runs and not self.func_next
        if self.func_next and kind == 'group':
            kind = 'func'
        self.func_next = False
        self._t('open', kind, word)
        self.frames.append({'kind': kind, 'base': base, 'body': base and body, 'dead': 0, 'saved': self.chain_ok})
        self.chain_ok = base

    def close(self, kinds):
        top = self.frames[-1]
        if top['kind'] not in kinds:
            self.poisoned = True
            self._t('poison')
            return
        self._t('close', top['kind'])
        self.last_closed = top['kind']
        self.frames.pop()
        outer = self.frames[-1]
        outer['dead'] |= top['dead'] & ~STOPS_AT.get(top['kind'], 0)
        self.chain_ok, self.prev_safe = top['saved'] and not outer['dead'], False

    def close_paren(self):
        if self.frames[-1]['kind'] != 'case':               # in a `case` the `)` ends a pattern
            self.close(('sub', 'csub'))

    def body(self, word):
        top = self.frames[-1]
        if top['kind'] not in BODY_OPEN[word]:
            self.poisoned = True
            self._t('poison')
            return
        self._t('body', word)
        top['base'] = top['body'] and not top['dead']
        self.chain_ok = self.chain_ok and top['base']

    def leaves(self, bit):
        self.frames[-1]['dead'] |= bit
        self.chain_ok = False


def _list_runs(code, text, toks, at):
    """Whether the loop `for NAME in WORDS` that starts at toks[at] surely has a word to go over: a word that is no variable, no substitution (a quote around one is no help)."""
    if at + 2 >= len(toks) or code[toks[at + 2][0]:toks[at + 2][1]] != 'in':
        return False                                       # `for x; do` goes over the arguments
    return any('$' not in text[a:b] and '`' not in text[a:b] for a, b in toks[at + 3:])


REDIRECT_START_RE = re.compile(r'\d*(?:&>>|&>|>>|>&|>|<<<|<<|<&|<)(?!\()')
SHELL_C_RE = re.compile(r'-[A-Za-z]*c[A-Za-z]*\Z')


def _is_redirect(code, tok):
    """Whether a word of masked code (its range) is a redirection (`> f`, `2>&1`, `<<EOF`) and not a process substitution."""
    return REDIRECT_START_RE.match(code, tok[0], tok[1]) is not None


def _shell_c_text(code, text, words):
    """The place in `words` (token ranges of one simple command) of the command text of a `bash -c "…"` (the reader of the code makes the quote a `(`, `link.shell_code`): the word that starts with `(` right after
    an option that holds a `c`, when the command word (after what leads it) is a shell. None when the command is no such thing."""
    from . import link as L
    shape = [code[a:b] for a, b in words]
    if len(shape) < 3:
        return None
    k, _ok, _ops = L.skip_prefixes(shape)
    if k >= len(shape) or shape[k].rsplit('/', 1)[-1] not in L.CX_SHELLS:
        return None
    for j in range(k + 2, len(shape)):
        if shape[j].startswith('(') and SHELL_C_RE.match(shape[j - 1]):
            return j
    return None


def _commands_of(code, sure_only=False, text=None, trace=None):
    """The simple commands of masked shell code in order, as `(token ranges, sure)`: `sure` says that the command is run whatever the ones before it said. It is not when it comes after
    `||`, after `&&` that follows anything but a command that only moves or makes folders (`cd x && mkdir r1` is as sure as `mkdir r1`; `test -d x && mkdir r1` is not), inside the
    branch of an `if`, a `case` or a `while`, inside a group, a subshell or a function body that is not sure itself (`true || { true; mkdir r1; }`: the `;` starts the list again
    inside the group only), after a command that leaves the shell (`exit 0; mkdir r1`), or inside a loop over nothing. What the line is after `;`, a new line or a lone `&` starts
    again. A loop over a list (`for`) runs its body. With `sure_only` the others are not given. `text` is the unmasked text (the same length as `code`). `trace`: a list that is filled with
    the structure of the text as it is read (`_Flow.trace`, and `('cmd', n)` where the n-th command of all, sure or not, is given), for `_places`."""
    from . import link as L
    text = code if text is None else text
    n, i, prev_end, count = len(code), 0, 0, 0
    flow = _Flow()
    flow.trace = trace
    heredocs = []                                          # the words that end the heredocs the commands before opened: (word, with `<<-`)
    while i < n:
        line_end = code.find('\n', i)
        line_end = n if line_end < 0 else line_end
        if i < line_end and not code[i:line_end].strip('x'):                # a line the reader masked: the body of a heredoc
            i = line_end + 1
            continue
        if heredocs:
            line = text[i:line_end].rstrip('\r')
            if (line.lstrip('\t') if heredocs[0][1] else line) == heredocs[0][0]:        # the line that ends a heredoc is no command
                del heredocs[0]
                i = line_end + 1
                continue
        toks, end = L._simple_tokens(code, i)
        i = end + 1
        if not toks:
            continue
        for q, (a, b) in enumerate(toks):                  # `<<EOF`, `<< 'EOF'`, `<<-EOF` (not the here-string `<<<`)
            m = HEREDOC_OP_RE.match(code[a:b])
            if m:
                word = text[a + m.end():b] if m.end() < b - a else (text[toks[q + 1][0]:toks[q + 1][1]] if q + 1 < len(toks) else '')
                if word:
                    heredocs.append((HEREDOC_QUOTE_RE.sub('', word), bool(m.group(1))))
        gap = FLOW_OP_RE.findall(code[prev_end:toks[0][0]])
        flow.last_closed = None
        for op in gap:
            flow.op(op)
        prev_end = end
        # `( … ) > f`, `{ …; } > f`, `bash -c "…" > f`: the redirect of what the `)` closed; and what follows the text of a `bash -c '…' _ arg > f` (its arguments and redirects are the shell's: the same simple command)
        redirect_only = gap == [')'] and (_is_redirect(code, toks[0]) or flow.last_closed == 'csub')
        queue = [toks]
        while queue:
            toks = queue.pop(0)
            opened, k, closed = 0, 0, False                    # what leads the command: `(` that open subshells and keywords, in any order (`( if`, `{ (`, `then {`)
            while k < len(toks):
                a, b = toks[k]
                while a < b and code[a] == '(':
                    flow.open('sub')
                    opened, a = opened + 1, a + 1
                w = code[a:b]
                if a == b:
                    k += 1
                elif w not in L_KEYWORDS:
                    break
                else:
                    if w == '{':
                        flow.open('group')
                    elif w == '!':
                        flow._t('neg')
                    elif w in COMPOUND_OPEN:
                        if w == 'for' and k + 1 < len(toks) and code[toks[k + 1][0]] == '(':
                            flow.poisoned = True               # `for ((` cuts the command at its `;`
                        flow.open(COMPOUND_OPEN[w], body=w == 'for' and _list_runs(code, text, toks, k), runs=w != 'case')
                    elif w in BODY_OPEN:
                        flow.body(w)
                    elif w in COMPOUND_CLOSE:
                        flow.close(COMPOUND_CLOSE[w])
                        closed = True
                    k += 1
            words = ([(a, toks[k][1])] + toks[k + 1:]) if k < len(toks) else []
            if closed and words and _is_redirect(code, words[0]):             # `{ …; } > f`, `done > f`: the redirect of what the word closed
                redirect_only = True
            cut = _shell_c_text(code, text, words)
            inner = None
            if cut is not None:                                # `bash -c (…)`: the command text of the shell is another process: it is a command of its own, in a frame of its own
                at = len(toks) - len(words) + cut
                inner = ([(toks[at][0] + 1, toks[at][1])] if toks[at][0] + 1 < toks[at][1] else []) + toks[at + 1:]
                toks, words = toks[:at], words[:cut]
            shape = [code[a:b] for a, b in words]
            names = [w.rsplit('/', 1)[-1] for w in shape]
            net = code.count('(', toks[0][0], toks[-1][1]) - code.count(')', toks[0][0], toks[-1][1]) - opened
            flow.poisoned = flow.poisoned or net < 0 or any(w in ('{', '}') and not (w == '{' and names[0] == 'function') for w in shape)        # a brace that is a word of a command
            sure = flow.sure()
            if redirect_only and not queue and inner is None:
                flow._t('redir', count)
            flow._t('cmd', count, bool(words) and not redirect_only)
            redirect_only = False
            count += 1
            if sure or not sure_only:
                yield toks, sure
            waiting = flow.func_next
            for _ in range(max(net, 0)):                       # a `(`, `<(`, `$((` inside a word: the `)` that ends it comes with the commands that follow
                flow.open('sub', word=True)
            flow.prev_safe = bool(names) and names[0] in SURE_BEFORE
            for name in names:                                 # the command word, after `VAR=x` and `time`, `command` ...
                if ASSIGN_WORD_RE.match(name):
                    continue
                if name in LEAVES:
                    flow.leaves(LEAVES[name])
                if name not in L.CMD_PREFIXES:
                    break
            if names[:1] == ['function'] or (code[end:end + 1] == ')' and shape and shape[-1].endswith('(')):                # `function f`, `f()`, `f ()`
                flow.func_next = True                          # a function is only defined here: its body is the next frame (`function f {` has it in this command)
                if '{' in shape:
                    flow.open('group')
            elif waiting and flow.func_next:
                flow.poisoned = True                           # a function that has no body to read
            if inner is not None:
                flow.open('csub')
                queue.insert(0, inner)
    if trace is not None:
        for op in FLOW_OP_RE.findall(code[prev_end:]):      # what comes after the last command: a `)` or `}` that closes a group, a lone `&`
            flow.op(op)
        if flow.poisoned or len(flow.frames) > 1:
            trace.append(('poison',))


def _tree(trace):
    """(root, count, poisoned) of a trace (`_commands_of(trace=)`): the commands as a tree of frames (`{ }`, `( )`, `if` ...): a frame is {'kind', 'word', 'items', 'seps'}, an item `('cmd', n, negated)` or
    `('frame', frame, negated)`, and `seps[j]` what joins the item j to the next one in its frame: `&&`, `||`, `|` (a pipe), `;` or `&`. `count` is how many commands there were; `poisoned`: the
    text could not be read to its end."""
    root = {'kind': 'root', 'word': False, 'items': [], 'gap': [], 'seps': []}
    stack, neg, count = [root], False, 0
    sep_of = {'&&': '&&', '||': '||', '|': '|', '|&': '|'}

    def settle(frame):
        """The gap before an item (or before the end of the frame) as one separator."""
        gap, frame['gap'] = [x for x in frame['gap'] if x != ')'], []
        pick = next((sep_of[x] for x in gap if x in sep_of), None) or ('&' if '&' in gap else ';')
        return pick

    for item in trace:
        kind = item[0]
        cur = stack[-1]
        if kind == 'poison':
            return root, count, True
        if kind == 'op':
            cur['gap'].append(item[1])
        elif kind == 'neg':
            neg = True
        elif kind == 'redir':                                # the redirect of the group (a frame) that closed just before
            last = cur['items'][-1] if cur['items'] else None
            if last is not None and last[0] == 'frame':
                last[1].setdefault('redirs', []).append(item[1])
        elif kind == 'body':
            cur['gap'].append(';')
        elif kind == 'open':
            frame = {'kind': item[1], 'word': item[2], 'items': [], 'gap': [], 'seps': []}
            if not item[2]:
                if cur['items']:
                    cur['seps'].append(settle(cur))
                cur['gap'] = []
                cur['items'].append(('frame', frame, neg))
                neg = False
            stack.append(frame)
        elif kind == 'close':
            if len(stack) > 1:
                done = stack.pop()
                done['seps'].append(settle(done) if done['items'] else ';')
        elif kind == 'cmd':
            n, has_words = item[1], item[2]
            count = n + 1
            if has_words:
                if cur['items']:
                    cur['seps'].append(settle(cur))
                cur['gap'] = []
                cur['items'].append(('cmd', n, neg))
                neg = False
    for frame in stack:                                  # a frame that never closed: it ends where the text ends
        if frame['items'] and len(frame['seps']) < len(frame['items']):
            frame['seps'].append(settle(frame))
    return root, count, False


def _piped_after(trace, launches):
    """The numbers of the simple commands that stand in a pipeline after a stage that starts a child (`launches`: the numbers of the commands that do; a group or a subshell that holds one is such
    a stage): what such a command does with its input is something done with the output of the child. Empty when the text could not be read to its end."""
    root, _count, poisoned = _tree(trace)
    out = set()
    if poisoned:
        return out

    def commands(item):
        return [item[1]] if item[0] == 'cmd' else [n for x in item[1]['items'] for n in commands(x)]

    def starts(item):
        return any(n in launches for n in commands(item))

    def walk(frame):
        seen = False
        for j, item in enumerate(frame['items']):
            if j and frame['seps'][j - 1] != '|':
                seen = False                             # another pipeline begins
            if seen:
                out.update(commands(item))
            if item[0] == 'frame':
                walk(item[1])
            seen = seen or starts(item)
    walk(root)
    return out


def _frame_commands(item):
    """The numbers of the simple commands of an item of `_tree` (a command, or a frame and everything in it)."""
    return [item[1]] if item[0] == 'cmd' else [n for x in item[1]['items'] for n in _frame_commands(x)]


def _group_redirects(trace, launches):
    """The numbers of the commands that are the redirect of a group (`( … ) > f`, `{ …; } > f`, `bash -c "…" > f`, `done > f`) that holds a command that starts a child (`launches`): what such a redirect
    takes is the output of the child. Empty when the text could not be read to its end."""
    root, _count, poisoned = _tree(trace)
    out = set()
    if poisoned:
        return out

    def walk(frame):
        for item in frame['items']:
            if item[0] == 'frame':
                if any(n in launches for n in _frame_commands(item)):
                    out.update(item[1].get('redirs', ()))
                walk(item[1])
    walk(root)
    return out


def _after_loose_cd(trace, is_cd):
    """The numbers of the commands that come after a `cd` (`is_cd(n)`: the n-th command moves the folder) that is not joined to the next command by `&&`, in the same shell (a `cd` in a subshell
    ends with it): the folder such a command runs in is where the shell was if the `cd` failed, and the files it names with a relative path may be elsewhere than the `cd` says. All of them when the
    text could not be read to its end."""
    root, count, poisoned = _tree(trace)
    if poisoned:
        return set(range(count))
    out = set()

    def walk(frame, loose):
        for j, item in enumerate(frame['items']):
            if item[0] == 'cmd':
                if loose:
                    out.add(item[1])
                if is_cd(item[1]) and (j >= len(frame['seps']) or frame['seps'][j] != '&&'):
                    loose = True
            else:
                inner = walk(item[1], loose)
                if item[1]['kind'] not in ('sub', 'csub') and inner:
                    loose = True                                 # (the `cd` of a group, an `if`, a loop stays in the shell)
        return loose
    walk(root, False)
    return out


def _places(trace):
    """Which of the simple commands of a trace (`_commands_of(trace=)`) is in a deciding place: a place where the exit status 0 of the whole command says that the simple command was run and
    worked, by the grammar alone. A command is in one when it is the last of its pipeline and not turned round by a `!`, nothing after it in its and-or list is joined by `||`
    (`a > X && b` is, `a > X || b` is not), its and-or list is the last of the list the command is in and is not sent to the background, and, when it is in `{ }` or `( )`, that group is in such
    a place itself. A command in `if`, a loop, `case`, a function body, or `$( )` is in none (so is every one when the text could not be read to its end). A command after a `;`, a new
    line or a `&` is not in the last list, whatever follows. `set -e` and `pipefail` are not looked at: a place they would make a deciding one stays masked. Returns {n: True | False}."""
    root, count, poisoned = _tree(trace)
    out = {n: False for n in range(count)}
    if poisoned:
        return out

    def mark(frame):
        if frame['kind'] not in ('root', 'group', 'sub') or frame['word'] or not frame['items']:
            return
        items, seps = frame['items'], frame['seps']
        last = len(items) - 1
        if seps[last] == '&':                            # the list runs in the background: its status says nothing
            return
        start = max([k + 1 for k in range(last) if seps[k] in (';', '&')] or [0])
        for j in range(start, last + 1):
            if items[j][2] or (j < last and seps[j] == '|') or any(seps[k] == '||' for k in range(j, last)):
                continue
            if items[j][0] == 'cmd':
                out[items[j][1]] = True
            else:
                for rn in items[j][1].get('redirs', ()):              # the redirect of a group is as deciding as the group (`{ a; } > f`, `( a ) > f`)
                    out[rn] = True
                mark(items[j][1])
    mark(root)
    return out


PARSED_KEEP_TEXT = 64 << 10          # a command text is read once and the reading is remembered for a while (what it writes, what it reads, what it launches come from one reading), when it is this short ...
PARSED_KEEP = 2 << 20                # ... and all the remembered readings together are about this many bytes of the texts they are of (a reading holds about ten times that)
_PARSED = collections.OrderedDict()
_PARSED_BYTES = [0]
_PARSED_LOCK = threading.Lock()
LAUNCH_NAMES = frozenset(('claude', 'codex'))
RUNS_ARGUMENTS = frozenset(('xargs', 'watch', 'parallel', 'ssh', 'su', 'npx', 'bunx', 'script', 'tmux', 'screen', 'docker', 'podman', 'kubectl'))     # programs that run the command line they are given (the link reads these as launches it cannot place)
CD_NAMES = frozenset(('cd', 'pushd', 'popd'))
NOISE_RED_RE = re.compile(r'\d*>>?&(?:\d+|-)|\d*>>?&?/dev/null|&>>?/dev/null')        # a redirect that is a copy of a descriptor or a write to /dev/null: no file
NOISE_RED_OP_RE = re.compile(r'\d*>>?&?|&>>?')                                        # the operator alone: the word after it says what it is
NOISE_RED_ARG_RE = re.compile(r'\d+|-|/dev/null')
NOISE_SCRUB_RE = re.compile(r'(?<![<>&\w])(?:\d*>>?&(?:\d+|-)|\d*>>?&?\s*/dev/null|&>>?\s*/dev/null)(?![\w/.-])')


HEREDOC_START_RE = re.compile(r'(?<!<)<<(?!<)(-?)[ \t]*(?:\'([^\'\n]*)\'|"([^"\n]*)"|\\?([A-Za-z_]\w*))')
ESCAPE_RE = re.compile(r'\\.', re.S)
COMMENT_RE = re.compile(r'(?:^|\s)#')


_LAST_OUTSIDE = [None]                # the last text given to `outside_heredocs` and its answer, as one pair (the writes and the reads of one command ask one after the other; more than one thread asks)


def outside_heredocs(text):
    """The text of a command without the bodies of its heredocs (what the shell does not run): from the line after `<<WORD` to the line that is WORD. This only makes a first look at a long text cheap (the
    reading of the command is `link.shell_code`'s), so it takes out a body only when it is sure there is one, and leaves the rest as it is: a `<<` in a comment is no heredoc (that one is passed over),
    and when a `<<` stands where a quote may be open (an odd number of quotes before it, on its line or in what is kept above it), after `((`, or when it shares its line with another heredoc, nothing
    after it is taken out (a text in a comment or in a prompt that teaches `cat <<EOF` must not make a write of the command go)."""
    if '<<' not in text:
        return text
    last = _LAST_OUTSIDE[0]
    if last is not None and last[0] is text:
        return last[1]
    out, pos, at, counted, sq, dq = [], 0, 0, 0, 0, 0               # pos: where the text not given yet begins; counted: how far the quotes of the kept text were counted
    while True:
        m = HEREDOC_START_RE.search(text, at)
        if m is None:
            break
        at = m.end()
        ls = text.rfind('\n', 0, m.start()) + 1
        if ls < pos:
            break                                                   # (a second heredoc on a line whose first one was taken out)
        before = text[ls:m.start()]
        if COMMENT_RE.search(before):
            continue                                                # a comment: this `<<` is no heredoc; the lines after it are as they were
        above = ESCAPE_RE.sub('', text[counted:ls])
        sq, dq, counted = sq + above.count("'"), dq + above.count('"'), ls
        near = ESCAPE_RE.sub('', before)
        if (sq + near.count("'")) % 2 or (dq + near.count('"')) % 2 or '((' in before:
            break                                                   # (a quote may be open: this may be text, and what follows it is not known)
        word = next(g for g in m.groups()[1:] if g is not None)
        eol = text.find('\n', m.end())
        if eol < 0:
            break
        i, end, dash = eol + 1, None, bool(m.group(1))
        while i <= len(text):
            j = text.find('\n', i)
            line = text[i:len(text) if j < 0 else j].rstrip('\r')
            if (line.lstrip('\t') if dash else line) == word:
                end = i
                break
            if j < 0:
                break
            i = j + 1
        if end is None:
            break
        rest = ESCAPE_RE.sub('', text[ls:eol + 1])                   # (the line itself stays: its quotes count from now on)
        sq, dq, counted = sq + rest.count("'"), dq + rest.count('"'), end
        out.append(text[pos:eol + 1])
        pos = at = end
    out.append(text[pos:])
    got = ''.join(out)
    _LAST_OUTSIDE[0] = (text, got)
    return got


def may_write(cmd):
    """Whether the text of a command can say that it writes a file: a `>` that is no copy of a descriptor and no write to /dev/null (`2>&1`, `>/dev/null`), or a `tee`, outside its heredoc bodies.
    A text that cannot is not read."""
    cmd = outside_heredocs(cmd)
    if 'tee' in cmd:
        return True
    return '>' in NOISE_SCRUB_RE.sub(' ', cmd)


def _traps_the_end(args):
    """Whether the words after `trap` set a handler (not `-`, not empty) for the end of the shell or for an error (`EXIT`, `0`, `ERR`): such a handler can end the shell with another status."""
    if len(args) < 2 or args[0] in ('', '-') or (args[0] or '').startswith('-'):
        return False
    return any(w in ('EXIT', '0', 'ERR', 'SIGEXIT') for w in args[1:] if w)


class _LazyEnv(object):
    """`link.literal_env(text, code)` that is worked out when it is first looked into: `link.resolve_path` asks it only for a word that holds a variable."""
    __slots__ = ('_args', '_env')

    def __init__(self, text, code):
        self._args, self._env = (text, code), None

    def _get(self):
        if self._env is None:
            from . import link as L
            self._env = L.literal_env(*self._args)
        return self._env

    def __contains__(self, name):
        return name in self._get()

    def __getitem__(self, name):
        return self._get()[name]

    def get(self, name, default=None):
        return self._get().get(name, default)


class Parsed(object):
    """A command text read once: its masked code (`link.shell_code`), its simple commands in order with whether each is sure to have run (`_commands_of`), the structure around them (`trace`), and, as they
    are asked for and then kept, the words of each command (`classify`: `link._classify`), whether it starts a child (`tool`), where it stands (`places`), what stands in a pipe after a launch (`piped`) and
    what the text says of variables (`env`, `assigns`). Everything the collector wants of a Bash command comes from the one reading (O15)."""
    __slots__ = ('text', 'code', 'commands', 'trace', '_cl', '_tool', '_places', '_piped', '_groups', '_loose', '_env', '_assigns')

    def __init__(self, text):
        from . import link as L
        self.text = outside_heredocs(text)                           # (the bodies of heredocs are no code: the places the ranges and `at` name are in this text)
        self.code = L.shell_code(self.text)
        self.trace = []
        self.commands = list(_commands_of(self.code, False, self.text, self.trace))          # [(token ranges, sure)]
        self._cl = [None] * len(self.commands)
        self._tool = [False] * len(self.commands)
        self._places = self._piped = self._groups = self._loose = self._env = self._assigns = None

    def words_of(self, i):
        """The words of the i-th command as they stand in the text, quotes left (the first look at it: which command is it?)."""
        toks = self.commands[i][0]
        return [self.text[a:b] for a, b in toks]

    def classify(self, i):
        """(words, reds, stdin) of the i-th command (`link._classify`)."""
        got = self._cl[i]
        if got is None:
            from . import link as L
            got = self._cl[i] = L._classify(self.text, self.code, self.commands[i][0])
        return got

    def mentions(self, i, names):
        """Whether a word of the i-th command, as it stands in the text (a `(` in front of it, a directory and quotes taken off), is one of `names`."""
        for w in self.words_of(i):
            if w.lstrip('(').rsplit('/', 1)[-1].strip('\'"') in names:
                return True
        return False

    def says(self, i, names):
        """Whether the text of the i-th command holds one of `names` anywhere (inside a quoted word too: `env -S 'claude -p x'`)."""
        toks = self.commands[i][0]
        seg = self.text[toks[0][0]:toks[-1][1]]
        return any(n in seg for n in names)

    def tool(self, i):
        """`claude` or `codex` when the i-th command starts a child (`starts_launch`), else None. Only a command that says one of the two names is taken apart."""
        got = self._tool[i]
        if got is False:
            got = None
            if self.says(i, LAUNCH_NAMES):
                got = launch_of(self.classify(i)[0])
            self._tool[i] = got
        return got

    def places(self):
        """{n: whether the n-th command is in a deciding place} (`_places`). A `trap` on the end of the shell (`EXIT`, `ERR`) can turn any status into 0, so none is deciding then; a `coproc` runs on its own."""
        if self._places is None:
            places = _places(self.trace)
            for i in range(len(self.commands) if ('trap' in self.text or 'coproc' in self.text) else 0):
                if self.mentions(i, ('trap', 'coproc')):
                    words, k, _ok, _ops = _head_of(self.classify(i)[0])
                    name = words[k].rsplit('/', 1)[-1] if k < len(words) and words[k] else None
                    if name == 'coproc':
                        places[i] = False
                    elif name == 'trap' and _traps_the_end(words[k + 1:]):
                        places = {n: False for n in places}
                        break
            self._places = places
        return self._places

    def group_redirects(self):
        """The numbers of the commands that are the redirect of a group that holds a launch (`_group_redirects`)."""
        if self._groups is None:
            launches = {i for i in range(len(self.commands)) if self.tool(i)}
            self._groups = _group_redirects(self.trace, launches) if launches else set()
        return self._groups

    def loose_cd(self):
        """The numbers of the commands that come after a `cd` that is not joined to the next command by `&&` (`_after_loose_cd`)."""
        if self._loose is None:
            moves = [i for i in range(len(self.commands)) if self.mentions(i, CD_NAMES)]
            if not moves:
                self._loose = set()
            else:
                def is_cd(i):
                    words, k, _ok, _ops = _head_of(self.classify(i)[0])
                    return k < len(words) and bool(words[k]) and words[k].rsplit('/', 1)[-1] in CD_NAMES
                self._loose = _after_loose_cd(self.trace, is_cd)
        return self._loose

    def piped(self):
        """The numbers of the commands that stand in a pipeline after a command that starts a child (`_piped_after`)."""
        if self._piped is None:
            self._piped = _piped_after(self.trace, {i for i in range(len(self.commands)) if self.tool(i)}) if any(self.tool(i) for i in range(len(self.commands))) else set()
        return self._piped

    def env(self):
        """What the text says of its variables (`link.literal_env`), as a mapping that is made when the first variable is asked for (most commands name no variable in a path)."""
        if self._env is None:
            self._env = _LazyEnv(self.text, self.code)
        return self._env

    def assigns(self):
        if self._assigns is None:
            from . import link as L
            self._assigns = L._cmd_assigns(self.text)
        return self._assigns


def parse(text):
    """The `Parsed` of a command text, from the readings that are remembered when there is one."""
    if len(text) <= PARSED_KEEP_TEXT:
        with _PARSED_LOCK:
            hit = _PARSED.get(text)
            if hit is not None:
                _PARSED.move_to_end(text)
                return hit
    got = Parsed(text)
    if len(text) <= PARSED_KEEP_TEXT:
        with _PARSED_LOCK:
            if text not in _PARSED:
                _PARSED_BYTES[0] += len(text)
            _PARSED[text] = got
            while _PARSED_BYTES[0] > PARSED_KEEP and _PARSED:
                old, _ = _PARSED.popitem(last=False)
                _PARSED_BYTES[0] -= len(old)
    return got


ShellWrite = collections.namedtuple('ShellWrite', 'path decides kind')       # a path a command writes, whether the command is in a deciding place for it (`_places`), and `replace` | `append`


def _is_no_work(path):
    """Whether a file that is not markdown is no work: what the tools keep of themselves (units.STATE_DIRS), and a scratch file (units.SCRATCH_DIRS) unless it is inside a repository: the folders
    of a repository that stands in a scratch folder (a checkout in /tmp, `make test > logs/x.txt`) are work, as the judgment (`units._work_file`) reads them."""
    from . import units as U
    if path.startswith(tuple(U.STATE_DIRS)):
        return True
    return path.startswith(tuple(U.SCRATCH_DIRS)) and U.repo_top(os.path.dirname(path)) is None


def shell_writes(cmd, cwd, sure_only=False, places=False, md_only=True, skip_launch=False):
    """The markdown files a Bash command writes with the shell itself: a redirect of standard output (`> f`, `>> f`, `&> f`, `cat > f <<EOF`) or `tee f`, as absolute normalised
    paths in the order they come. The command is read the way board/link.py reads a launching command: text in quotes, in heredoc bodies and in comments does nothing, a `cd`
    before the redirect moves the folder, and a variable counts only when the command itself gives it one value. What cannot be told (a path with a variable or a substitution
    in it, a relative path with no known folder, a file with two possible names) is left out. `cwd` is the folder the call ran in. With `sure_only` a write that may not have been
    run is left out too (after `||`, after `&&` that depends on a command that decides something, inside an `if`, a `case` or a `while`: `_commands_of`), and `tee --help`.
    With `places` it gives `ShellWrite(path, decides, kind)` instead of the path: whether the command is in a deciding place (`_places`) and how it writes (`>` and `tee` replace, `>>` and
    `tee -a` append). With `md_only` False it gives files of every kind (not `/dev/*`, and not a file other than markdown in a folder the tools keep or in a scratch folder that is no part of a
    repository: `_is_no_work`). With `skip_launch` the redirects of a command that starts `claude -p` or `codex exec` are left out, and so is a `tee` in the same pipeline after one (`claude -p x |
    tee f`): what the shell writes of the output of a child is the child's report (its requirement and event, J7), not a write of the one that started it (O8, O8b); a redirect or a
    `tee` of another command of the same text (not in that pipeline) stays."""
    if (md_only and '.md' not in cmd) or not may_write(cmd):
        return []
    from . import link as L
    P = parse(cmd)
    code = P.code
    base = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
    moves, budget = bool(CD_WORD_RE.search(cmd)), SHELL_MOVES_MAX      # a `cd` in the command moves the folder; following it is a pass over the text before the redirect, so only so many
    out, seen = [], set()
    decide = P.places() if places else {}
    loose = P.loose_cd() if (places and moves) else ()
    mentions_launch = skip_launch and ('claude' in cmd or 'codex' in cmd)
    piped = P.piped() if mentions_launch else ()
    groups = P.group_redirects() if mentions_launch else ()
    for n, (toks, sure) in enumerate(P.commands):
        if sure_only and not sure:
            continue
        if not _may_write_here(code, toks):                                          # words are read only in a command that has a redirect that is a file or a tee (a heredoc body is hundreds of lines)
            continue
        if mentions_launch and (n in piped or n in groups or P.tool(n)):             # what it takes is the output of a child: the child's report, not a write of the one that ran the command (O8, O8b, O11)
            continue
        words, reds, _stdin = P.classify(n)
        words, k, _ok, _ops = _head_of(words)
        tee = k < len(words) and bool(words[k]) and words[k].rsplit('/', 1)[-1] == 'tee'
        if not reds and not tee:
            continue
        env, assigns = P.env(), P.assigns()
        here = base
        if moves and CD_WORD_RE.search(P.text, 0, toks[0][0]):                  # (no `cd` before it: the folder is the one of the call)
            budget -= 1
            here = L.shell_cwd(P.text, code, toks[0][0], assigns, base) if budget >= 0 else None
        got = collections.OrderedDict()                               # the word as written -> the paths it can mean
        kinds = {}                                                    # the word as written -> how it is written
        for r in L.build_redirects(reds, env, here):
            if r.fd == 1 and r.op in ('>', '>>'):
                got.setdefault(r.path_raw, []).append(r.path_resolved)
                kinds.setdefault(r.path_raw, 'append' if r.op == '>>' else 'replace')
        if tee:
            options, append = True, False
            if not (sure_only and any(w in ('--help', '--version') for w in words[k + 1:] if w)):          # `tee --help f` prints and writes nothing
                for w in words[k + 1:]:
                    if w is None:
                        continue
                    if options and w == '--':
                        options = False
                    elif options and w.startswith('-') and len(w) > 1:
                        append = append or w == '--append' or (not w.startswith('--') and 'a' in w[1:])
                    else:
                        got.setdefault(w, []).extend(p for p, un in L.resolve_path(w, env, here, quoted=False) if not un)
                        kinds.setdefault(w, 'append' if append else 'replace')
        for raw, paths in got.items():
            found = {os.path.normpath(p) for p in paths if p}
            if len(found) == 1 and None not in paths:
                path = found.pop()
                if path in seen:
                    continue
                if md_only:
                    if not path.endswith('.md'):
                        continue
                elif path.startswith('/dev/'):
                    continue
                elif not path.endswith('.md') and _is_no_work(path):
                    continue
                seen.add(path)
                if places:
                    sure_place = decide.get(n, False) and not (n in loose and not _cwd_free(raw, env))        # a path worked out from a `cd` that may have failed is not a place the status can vouch for
                    out.append(ShellWrite(path, sure_place, kinds.get(raw, 'replace')))
                else:
                    out.append(path)
    return out


def _cwd_free(raw, env):
    """Whether the word of a redirect target or a file does not depend on the folder the shell is in: an absolute path (`~` and a variable with an absolute value too)."""
    from . import link as L
    return any(p is not None for p, _un in L.resolve_path(raw, env, None, quoted=False if not raw[:1] in ('"', "'") else True))


def _may_write_here(code, toks):
    """Whether a simple command (its token ranges in the masked code) can write a file: a `tee`, or a word with a `>` that is not a copy of a descriptor or a write to /dev/null (`2>&1`,
    `>/dev/null`, `2> /dev/null`)."""
    k, n = 0, len(toks)
    while k < n:
        t = code[toks[k][0]:toks[k][1]]
        if t.rsplit('/', 1)[-1] == 'tee':
            return True
        if '>' in t:
            if NOISE_RED_RE.fullmatch(t):
                pass
            elif NOISE_RED_OP_RE.fullmatch(t) and k + 1 < n and NOISE_RED_ARG_RE.fullmatch(code[toks[k + 1][0]:toks[k + 1][1]]):
                k += 1
            else:
                return True
        k += 1
    return False


SED_IN_PLACE_RE = re.compile(r'-[A-Za-z]*i')                                          # `-i`, `-i.bak`, `-ni`
READERS = frozenset(('cat', 'head', 'tail', 'less', 'more', 'nl', 'sed'))      # the commands whose file arguments a Bash call reads
READERS_RE = re.compile(r'\b(?:' + '|'.join(sorted(READERS)) + r')\b')            # a call that says none of them as a word is not read (`nl` and `more` are inside many words)
READER_VALUE_OPTS = {'head': ('-n', '-c', '--lines', '--bytes'), 'tail': ('-n', '-c', '--lines', '--bytes', '--pid', '-s', '--sleep-interval'), 'less': ('-b', '-h', '-j', '-p', '-P', '-t', '-T', '-x', '-y', '-z'),
                     'more': ('-n', '-p'), 'nl': ('-b', '-d', '-f', '-h', '-i', '-l', '-n', '-s', '-v', '-w'), 'sed': ('-e', '-f', '--expression', '--file', '-l', '--line-length'), 'cat': ()}


def shell_reads(cmd, cwd, md_only=True):
    """The files a Bash command reads with `cat`, `head`, `tail`, `less`, `more`, `nl` or `sed` (not `sed -i`), as absolute normalised paths in the order they come: the file words of those
    commands and a `< file` of theirs. Read the way `shell_writes` reads a command (`_commands_of` with the commands that are sure to run only; quotes and heredoc bodies do nothing, a
    `cd` before moves the folder, a variable counts only when the command itself gives it one value); a word that cannot be told is left out. `cwd` is the folder the call ran in. With `md_only` (the
    default) a command that does not say `.md` is not read: the judgment looks at the reads of markdown files only (the guides of a folder, a document two agents read, the file a window read), and a read
    of any other file would only take a place of the kept ones (READS_KEEP) and the time of a first picture."""
    if (md_only and '.md' not in cmd) or not READERS_RE.search(outside_heredocs(cmd)):
        return []
    from . import link as L
    P = parse(cmd)
    code = P.code
    base = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
    moves, budget = bool(CD_WORD_RE.search(cmd)), SHELL_MOVES_MAX
    out = []
    for n, (toks, sure) in enumerate(P.commands):
        if not sure or not P.mentions(n, READERS):                                  # (the words of a command are taken apart only when one of them is a reader)
            continue
        words, _reds, stdin = P.classify(n)
        k = 0
        while k < len(words) and words[k] and (ASSIGN_WORD_RE.match(words[k]) or words[k] in L.CMD_PREFIXES):
            k += 1
        name = words[k].rsplit('/', 1)[-1] if k < len(words) and words[k] else None
        if name not in READERS:
            continue
        args, options, take, script, in_place = [], True, 0, name == 'sed', False
        value_opts = READER_VALUE_OPTS.get(name, ())
        for w in words[k + 1:]:
            if w is None:
                args.append(None)
                take = 0
                continue
            if take:
                take -= 1
                continue
            if options and w == '--':
                options = False
            elif options and w.startswith('-') and len(w) > 1:
                if name == 'sed' and (w == '--in-place' or w.startswith('--in-place=') or SED_IN_PLACE_RE.match(w)):
                    in_place = True
                if w in ('-e', '-f', '--expression', '--file') or w.startswith(('--expression=', '--file=')):
                    script = False                                  # the script is given by an option: every word is a file
                if w in value_opts:
                    take = 1
            elif script:
                script = False                                      # `sed 's/a/b/' file`: the first word that is no option is the script
            else:
                args.append(w)
        if in_place:
            continue
        env, assigns = P.env(), P.assigns()
        here = base
        if moves and CD_WORD_RE.search(P.text, 0, toks[0][0]):                  # (no `cd` before it: the folder is the one of the call)
            budget -= 1
            here = L.shell_cwd(P.text, code, toks[0][0], assigns, base) if budget >= 0 else None
        words_to_read = [w for w in args if w] + ([stdin] if isinstance(stdin, str) and stdin else [])
        for w in words_to_read:
            paths = [p for p, un in L.resolve_path(w, env, here, quoted=False) if not un]
            found = {os.path.normpath(p) for p in paths if p}
            if len(found) == 1 and found <= {p for p in found if os.path.isabs(p)}:
                path = next(iter(found))
                if path not in out and not path.startswith('/dev/') and (path.endswith('.md') or not md_only):
                    out.append(path)
    return out


SETTER_NAMES = frozenset(('export', 'declare', 'typeset', 'readonly', 'local', 'unset'))     # the commands that change the variables of the shell for what comes after them
NAME_RE = re.compile(r'[A-Za-z_]\w*\Z')


def _given(word):
    """(name, value) of a word `NAME=value` (value None when it holds a `$` or a backtick: it is no literal), or None when the word is no assignment."""
    if not word or not ASSIGN_WORD_RE.match(word):
        return None
    name, _, value = word.partition('=')
    return name, (None if ('$' in value or '`' in value or name.endswith('+')) else value)


def _head_of(words):
    """(words, k, ok, ops) of the words of a simple command (`_classify`): the words without what leads the command (`(`, `{`, `then`, `do`, `!`), the place of the command word after the assignments and
    prefix commands in front of it (`VAR=value cmd`, `env -u A cmd`, `timeout -s KILL 5 cmd`: the closed list of `link.skip_prefixes`, the one the reader of the link reads a launch with), whether every
    option in front of the command word was one of that list, and what the prefix does to the environment of the command, in order: ('set', NAME, value or None), ('unset', NAME), ('clear',)."""
    from . import link as L
    while words and words[0] is not None and (words[0] in L_KEYWORDS or words[0].startswith('(')):
        if words[0] in L_KEYWORDS:
            words = words[1:]
        else:
            words = ([words[0].lstrip('(')] if words[0].lstrip('(') else []) + words[1:]
    k, ok, ops = L.skip_prefixes(words)
    return words, k, ok, ops


def _launch_tool(name, rest):
    """`claude` or `codex` when a command word and the words after it start `claude -p` or `codex exec` (`codex e`, and `codex -c key=value exec`), else None."""
    if name == 'claude' and any(w in ('-p', '--print') for w in rest if w):
        return 'claude'
    if name == 'codex':
        from . import link as L
        if L.codex_sub(rest) in ('exec', 'e') or 'exec' in [w for w in rest[:8] if w]:
            return 'codex'
    return None


def _tool_at(words, k, ok):
    """(tool, place, loose) of a simple command whose words have been walked to the command word at `k` (`_head_of`): the tool it starts (`_launch_tool`), where its name is, and whether it was found
    only by looking through the words (`loose`: the place is then the command word, not the tool's). When a prefix in front of the
    command word could not be read (`ok` False), the command word is not known: a `claude -p` or `codex exec` later in the words may be what it runs, and it is taken for it."""
    name = words[k].rsplit('/', 1)[-1] if k < len(words) and words[k] else None
    tool = _launch_tool(name, words[k + 1:]) if name else None
    if tool is not None:
        return tool, k, False
    if not ok or name in RUNS_ARGUMENTS:
        flat = [x for w in words[k:] if w for x in w.split()]                   # (a command line in one word: `env -S 'claude -p x'`, `xargs -I{} sh -c '…'`)
        for j, w in enumerate(flat):
            base = w.rsplit('/', 1)[-1]
            if base in LAUNCH_NAMES and _launch_tool(base, flat[j + 1:]):
                return _launch_tool(base, flat[j + 1:]), k, True
    return None, k, False


def launch_of(words):
    """The tool (`claude` or `codex`) that the simple command with these words (`_classify`) starts, or None: `claude -p` or `codex exec` after assignments and prefix commands
    (`BULLPEN_ROOM=x nohup claude -p …`, `env -u A timeout -s KILL 9 claude -p …`); also when a prefix could not be read and the words say one of them after it."""
    words, k, ok, _ops = _head_of(words)
    return _tool_at(words, k, ok)[0]


def starts_launch(words):
    """Whether the simple command with these words (`_classify`) is a launch (`launch_of`)."""
    return launch_of(words) is not None


def _setter_ops(name, rest):
    """What a command that changes the variables of the shell does: ('set', NAME, value or None, exported), ('export', NAME), ('unexport', NAME), ('unset', NAME), in order. `export` exports; `declare` and
    `typeset` only with `-x`; `readonly` and `local` do not (a variable that is not exported does not reach what the shell starts)."""
    ops, exported, unexport = [], name == 'export', False
    for w in rest:
        if w is None:
            continue
        if w[:1] in ('-', '+') and len(w) > 1 and not _given(w):
            if name in ('declare', 'typeset') and 'x' in w[1:]:
                exported, unexport = w[0] == '-', w[0] == '+'
            elif name == 'export' and w == '-n':
                exported, unexport = False, True
            continue
        if name == 'unset':
            if NAME_RE.match(w):
                ops.append(('unset', w))
            continue
        g = _given(w)
        if g:
            ops.append(('set', g[0], g[1], exported))
            if unexport:
                ops.append(('unexport', g[0]))
        elif NAME_RE.match(w):
            ops.append(('unexport', w) if unexport else ('export', w) if exported else ('keep', w))
    return ops


def launch_pieces(cmd):
    """The commands of a command text, as far as the variables a launch gets are concerned: (pieces, setters, others). A piece is a simple command that starts `claude -p` or `codex exec`
    (`tool`), with what stands in front of it done to its environment as `ops` (`_head_of`: `VAR=value cmd`, `env -u A cmd`, `env -i cmd`), whether the prefix could be read (`ok`), the words that follow its name
    (`words`), where it starts (`at`, a place in `parse(cmd).text`), its number (`n`), the subshells it is in (`scope`) and whether it is sure to run (`sure`). A setter is a command that changes the variables of the
    shell (`VAR=value`, `export VAR=value`, `unset VAR`): its `ops` (`_setter_ops`), `n`, `scope` and `sure`. `others` are the commands that could start something the text does not show (a script): the commands
    that are no launch, no setter and no command that only prints, searches or moves files, with the same `ops`, `n`, `scope` and `sure` as a piece (`script_env` says what they give). The text is read the way `shell_writes`
    reads one: nothing inside quotes, heredoc bodies and comments, `shlex` for the words."""
    from . import link as L
    P = parse(cmd)
    scope, stack, nxt = {}, [], 0
    for item in P.trace:                                         # the subshells (and substitutions) around each command: an assignment in one does not reach what is outside it
        if item[0] == 'open':
            stack.append((nxt, item[1] in ('sub', 'csub') or bool(item[2])))
            nxt += 1
        elif item[0] == 'close' and stack:
            stack.pop()
        elif item[0] == 'cmd':
            scope[item[1]] = frozenset(i for i, sub in stack if sub)
    pieces, setters, others = [], [], []
    for n, (toks, sure) in enumerate(P.commands):
        words = P.classify(n)[0]
        words, k, ok, ops = _head_of(words)
        name = words[k].rsplit('/', 1)[-1] if k < len(words) and words[k] else None
        rest = list(words[k + 1:]) if name else []
        here = scope.get(n, frozenset())
        tool, at, loose = _tool_at(words, k, ok)
        if tool is not None:
            pieces.append({'tool': tool, 'ops': ops, 'ok': ok, 'words': list(words[at + 1:]), 'loose': loose, 'at': toks[0][0], 'n': n, 'scope': here, 'sure': sure})
        elif name in SETTER_NAMES:
            setters.append({'n': n, 'scope': here, 'sure': sure, 'ops': _setter_ops(name, rest)})
        elif name is None and ops and k >= len(words) and _given(words[0] if words else None):          # `A=1 B=2` alone: variables of the shell that are not exported
            setters.append({'n': n, 'scope': here, 'sure': sure, 'ops': [('set', op[1], op[2], False) for op in ops if op[0] == 'set']})
        elif name is not None and name not in L.PRINT_CMDS:      # a command that may start a script
            others.append({'n': n, 'scope': here, 'sure': sure, 'ops': ops})
    return pieces, setters, others


def script_env(others, setters, names):
    """{name: value (None: not a literal)} of the variables among `names` that the text gives a launch it does not show (a script, `xargs`): what every command that could have started it gives it
    (`piece_env`: the variables the shell had exported before it in the same shell and nothing taken away since, then what stands in front of the command). A command that could have started it and gives
    another value, or none, leaves the name unknown: nothing is given when the text cannot say which command it was."""
    envs = [piece_env(c, setters, names) for c in others]
    out = {}
    for name in names:
        values = [e[name] if name in e else _NO_VALUE for e in envs]
        if values and all(v == values[0] for v in values) and values[0] is not _NO_VALUE:
            out[name] = values[0]
    return out


_NO_VALUE = object()


def piece_env(piece, setters, names):
    """{name: value (None: not a literal)} of the variables among `names` that a launch (a piece of `launch_pieces`) is given: the ones the shell had exported before it (the setters before it in the same
    shell: a setter in a subshell the launch is not in does not reach it, one that may not have run leaves the name unknown, and a variable that was only assigned is not exported), and then what stands in
    front of the command (`VAR=value cmd`, `env -u VAR cmd`, `env -i cmd`) in the order it stands."""
    exported, assigned, unknown = {}, {}, set()
    for st in setters:
        if st['n'] >= piece['n'] or not st['scope'] <= piece['scope']:
            continue
        for op in st['ops']:
            kind, name = op[0], op[1]
            if not st['sure']:
                unknown.add(name)                                # it may not have been run: what the variable is now is not known
            elif kind == 'set':
                assigned[name] = op[2]
                if op[3] or name in exported:                    # a variable that was exported stays so when it is given another value
                    exported[name] = op[2]
            elif kind == 'export':
                exported[name] = assigned.get(name)
            elif kind == 'unexport':
                exported.pop(name, None)
            elif kind == 'unset':
                exported.pop(name, None)
                assigned.pop(name, None)
    env = {n: v for n, v in exported.items() if n in names and n not in unknown}
    for op in piece['ops']:
        if op[0] == 'set' and op[1] in names:
            env[op[1]] = op[2]
        elif op[0] == 'unset':
            env.pop(op[1], None)
        elif op[0] == 'clear':
            env.clear()
    return env


def shell_mkdirs(cmd, cwd):
    """The folders a Bash command makes with `mkdir` (`mkdir -p talk/r1`, `cd x && mkdir r1`, `D=/a; mkdir -p "$D/r1"`), as absolute normalised paths in the order they come. Read the way
    `shell_writes` reads a command: only where `mkdir` is the command (not an argument of `echo`, text in quotes or in a heredoc body), a `cd` before it moves the folder, a variable
    counts only when the command itself gives it one value; the mode (`-m 700`), `-p`, `-v` and `--` are not folders. What cannot be told is left out: a word with a brace list
    (`t/{r1,r2}`), a glob, a variable or a substitution that has no value here, a relative path with no known folder, `--help` and `--version`, and a `mkdir` that may not have been run
    (after `||`, after `&&` that depends on a command that decides something, inside an `if`, a `case` or a `while`: `_commands_of`). `cwd` is the folder the call ran in."""
    if 'mkdir' not in cmd:
        return []
    from . import link as L
    P = parse(cmd)
    code = P.code
    base = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
    moves, budget = bool(CD_WORD_RE.search(cmd)), SHELL_MOVES_MAX
    out = []
    for n, (toks, sure) in enumerate(P.commands):
        if not sure or not P.mentions(n, ('mkdir',)):
            continue
        words, _reds, _stdin = P.classify(n)
        k = 0
        while k < len(words) and words[k] and (ASSIGN_WORD_RE.match(words[k]) or words[k] in L.CMD_PREFIXES):
            k += 1
        if not (k < len(words) and words[k] and words[k].rsplit('/', 1)[-1] == 'mkdir'):
            continue
        env, assigns = P.env(), P.assigns()
        here = base
        if moves and CD_WORD_RE.search(P.text, 0, toks[0][0]):                  # (no `cd` before it: the folder is the one of the call)
            budget -= 1
            here = L.shell_cwd(P.text, code, toks[0][0], assigns, base) if budget >= 0 else None
        if any(w in ('--help', '--version') for w in words[k + 1:] if w):        # it prints and makes nothing
            continue
        options, take_value = True, False
        for w in words[k + 1:]:
            if w is None:
                take_value = False
                continue
            if take_value:                                                  # the mode of `-m 700`
                take_value = False
                continue
            if options and w == '--':
                options = False
                continue
            if options and w.startswith('-') and len(w) > 1:
                take_value = w in ('-m', '--mode')
                continue
            if any(ch in w for ch in '{}*?[`'):
                continue
            paths = [p for p, un in L.resolve_path(w, env, here, quoted=False)]
            found = {os.path.normpath(p) for p in paths if p}
            if len(found) == 1 and None not in paths:
                path = found.pop()
                if path not in out:
                    out.append(path)
    return out


def _hull(a, b):
    """The one span (t0, t1) that covers the spans a and b (either may be None)."""
    return a if b is None else b if a is None else (min(a[0], b[0]), max(a[1], b[1]))


class EventLog(object):
    """What one author (an agent, or the orchestrator of a session) has done, as the debate judgment reads it: write events, read events, command windows and the planned outputs of
    its runs, with what could not be kept. Nothing here reads the disk. `gen` goes up every time something the judgment reads changes (J19): a cache of a judgment is good while it is the same.
    Markdown write events are never let go for the number of the others; the others (every other path) are kept to the newest OTHER_WRITES_KEEP and the span of the ones let go
    is `writes_dropped`; past MD_EVENTS_MAX the later markdown events are not kept and `lost` runs from the first one not kept to the end of time. Windows are kept to the newest `windows_keep`;
    `windows_dropped` is the span of the ones let go (an open one reaches to the end of time). `lost` is also the part of a record that was not read (a big rollout read from its end)."""

    def __init__(self, windows_keep=WINDOWS_KEEP):
        self.gen = 0
        self.windows_keep = windows_keep
        self._md, self._md_ts = [], []                     # markdown events, in the order of their time (and, at the same time, of their arrival)
        self._other = collections.deque()                  # the newest events on other paths
        self._merged = (-1, [])                            # (gen, the events of both in the order of their time)
        self.writes_dropped = self.windows_dropped = None
        self._lost_front = self._lost_rest = None          # the part of the record that was not read because the read began late (a big rollout), and the other parts (a line too long, past MD_EVENTS_MAX)
        self.reads = []
        self.windows = collections.deque()
        self._open = {}                                    # call id -> its window while it is open
        self.planned = []
        self.lock = threading.RLock()                      # the collector writes, the judgment reads: they are not the same thread

    def bump(self):
        with self.lock:
            self.gen += 1

    def reset_record(self):
        """The record is read again from its start: everything made of its lines is made again (what came from elsewhere, the planned outputs and their events, stays)."""
        with self.lock:
            keep = [e for e in self._md if e.evidence == 'planned']
            self._md, self._md_ts = keep, [e.ts for e in keep]
            self._other = collections.deque(e for e in self._other if e.evidence == 'planned')
            self.writes_dropped = self.windows_dropped = None
            self._lost_front = self._lost_rest = None
            self.reads = []
            self.windows.clear()
            self._open.clear()
            self.bump()

    @property
    def lost(self):
        """The span of the record that was not read: one span that covers the part before the start of the read and the other parts (J3 ③, J15 history_lost)."""
        a, b = self._lost_front, self._lost_rest
        return a if b is None else b if a is None else (min(a[0], b[0]), max(a[1], b[1]))

    @lost.setter
    def lost(self, span):
        """Sets the whole span at once (a log made by hand, as a test makes one)."""
        with self.lock:
            self._lost_front, self._lost_rest = None, span

    # ---- write events ----
    def events(self):
        with self.lock:
            gen, got = self._merged
            if gen != self.gen:
                got = sorted(self._md + list(self._other), key=lambda e: e.ts)
                self._merged = (self.gen, got)
            return got

    def add_write(self, ev):
        """Keeps one write event; returns it, or None when it was not kept (past MD_EVENTS_MAX)."""
        with self.lock:
            if ev.path.endswith('.md'):
                if len(self._md) >= MD_EVENTS_MAX:
                    self.widen_lost(ev.ts, INF)
                    return None
                i = bisect.bisect_right(self._md_ts, ev.ts)
                self._md_ts.insert(i, ev.ts)
                self._md.insert(i, ev)
            else:
                if len(self._other) >= OTHER_WRITES_KEEP:
                    old = self._other.popleft()
                    self.writes_dropped = (old.ts, old.ts) if self.writes_dropped is None else (min(self.writes_dropped[0], old.ts), max(self.writes_dropped[1], old.ts))
                self._other.append(ev)
            self.bump()
            return ev

    def replace_write(self, old, new):
        """The event `old` becomes `new` (its result came). False when `old` is not kept any more."""
        with self.lock:
            for seq in (self._md, self._other):
                for i in range(len(seq) - 1, -1, -1):
                    if seq[i] is old:
                        seq[i] = new
                        self.bump()
                        return True
        return False

    def remove_write(self, ev):
        """Takes one write event away (the plan it came from was given up). False when it is not kept."""
        with self.lock:
            for seq, keys in ((self._md, self._md_ts), (self._other, None)):
                for i in range(len(seq) - 1, -1, -1):
                    if seq[i] is ev:
                        del seq[i]
                        if keys is not None:
                            del keys[i]
                        self.bump()
                        return True
        return False

    def widen_lost(self, t0, t1):
        """A part of the record that was not read, from t0 to t1; one span covers all of them."""
        with self.lock:
            self._lost_rest = (t0, t1) if self._lost_rest is None else (min(self._lost_rest[0], t0), max(self._lost_rest[1], t1))
            self.bump()

    def set_front_lost(self, t0, t1):
        """The part of the record before the place the read began (a big rollout is read from its end) was not read: from t0 to t1. It goes when that part has been read (`clear_front_lost`)."""
        with self.lock:
            self._lost_front = (t0, t1)
            self.bump()

    def clear_front_lost(self):
        with self.lock:
            if self._lost_front is not None:
                self._lost_front = None
                self.bump()

    def merge_front(self, front, who=None):
        """The events, reads and windows of an earlier part of the record (`front`: an EventLog made from it alone) come before what is kept: markdown events are all kept up to MD_EVENTS_MAX, the
        others and the windows are the newest of both. What `front` could not keep is lost here too. `who`: the author they are of here (a front that was made for another agent of the same file is
        the same history: the events are this one's)."""
        with self.lock:
            if who is not None:
                front = front.owned_by(who)
            for e in front._md:
                if len(self._md) >= MD_EVENTS_MAX:
                    self.widen_lost(e.ts, INF)
                    continue
                i = bisect.bisect_right(self._md_ts, e.ts)
                self._md_ts.insert(i, e.ts)
                self._md.insert(i, e)
            other = sorted(list(front._other) + list(self._other), key=lambda e: e.ts)
            if len(other) > OTHER_WRITES_KEEP:
                gone = other[:len(other) - OTHER_WRITES_KEEP]
                other = other[len(other) - OTHER_WRITES_KEEP:]
                self.writes_dropped = _hull(self.writes_dropped, (gone[0].ts, gone[-1].ts))
            self._other = collections.deque(other)
            self.writes_dropped = _hull(self.writes_dropped, front.writes_dropped)
            self.reads = (front.reads + self.reads)[-READS_KEEP:]
            windows = list(front.windows) + list(self.windows)
            if len(windows) > self.windows_keep:
                gone = windows[:len(windows) - self.windows_keep]
                windows = windows[len(windows) - self.windows_keep:]
                self.windows_dropped = _hull(self.windows_dropped, (min(w.t0 for w in gone), max(INF if w.t1 is None else w.t1 for w in gone)))
            self.windows_dropped = _hull(self.windows_dropped, front.windows_dropped)
            self.windows = collections.deque(windows)
            if front.lost is not None:
                self._lost_rest = _hull(self._lost_rest, front.lost)
            self.bump()

    def owned_by(self, who):
        """A copy of this log with every event, read and window made the work of `who` (the log itself is not touched: it may be the remembered front of a file that more than one agent has)."""
        with self.lock:
            got = EventLog(self.windows_keep)
            sw = lambda x: x if x.agent == who else dataclasses.replace(x, agent=who)
            got._md, got._md_ts = [sw(e) for e in self._md], list(self._md_ts)
            got._other = collections.deque(sw(e) for e in self._other)
            got.writes_dropped, got.windows_dropped = self.writes_dropped, self.windows_dropped
            got._lost_front, got._lost_rest = self._lost_front, self._lost_rest
            got.reads = [sw(r) for r in self.reads]
            got.windows = collections.deque(sw(w) for w in self.windows)
            got.planned = list(self.planned)
            return got

    # ---- reads ----
    def add_read(self, ev):
        with self.lock:
            self.reads.append(ev)
            if len(self.reads) > READS_KEEP:
                del self.reads[:len(self.reads) - READS_KEEP]
            self.bump()

    # ---- windows ----
    def add_window(self, w):
        with self.lock:
            dq = self.windows
            if len(dq) >= self.windows_keep:
                old = dq.popleft()
                if self._open.get(old.call) is old:
                    del self._open[old.call]
                t1 = INF if old.t1 is None else old.t1
                self.windows_dropped = (old.t0, t1) if self.windows_dropped is None else (min(self.windows_dropped[0], old.t0), max(self.windows_dropped[1], t1))
            dq.append(w)
            if w.t1 is None:
                self._open[w.call] = w
            self.bump()

    def close_window(self, call, t1, ok):
        """The call has ended (a result, a completion notice): its window gets the end and the result. False when there is no open window of that call."""
        with self.lock:
            w = self._open.pop(call, None)
            if w is None:
                return False
            dq = self.windows
            for i in range(len(dq) - 1, -1, -1):
                if dq[i] is w:
                    dq[i] = dataclasses.replace(w, t1=t1, ok=ok)
                    self.bump()
                    return True
        return False

    # ---- planned outputs ----
    def add_planned(self, p):
        with self.lock:
            if p not in self.planned:
                self.planned.append(p)
                self.bump()

    def drop_planned(self, keep):
        """Takes away the planned outputs for which `keep(p)` is false."""
        with self.lock:
            kept = [p for p in self.planned if keep(p)]
            if len(kept) != len(self.planned):
                self.planned = kept
                self.bump()


WRITE_TOOLS = frozenset(('Write', 'Edit', 'MultiEdit', 'NotebookEdit'))
WRITE_KIND_OF_RESULT = {'create': 'create', 'update': 'replace'}      # toolUseResult.type of a Write


def shell_events(who, ts, run, call, cmd, cwd, ok=None):
    """The WriteEvents of one shell command (a Claude Bash call, a Codex CommandExecution): one for each file it writes in a place that is sure to have run (`shell_writes`). A deciding place
    gives `ok` of the whole command and `proof='exit'`; a masked place `ok` True when the command worked, else None, with `proof='window'` (the judgment checks the file: J1).
    The redirect of a piece that starts `claude -p` or `codex exec` is no write of the one that ran the command (O8): it is the requirement and the event of the child it started."""
    out = []
    for w in shell_writes(cmd, cwd, True, places=True, md_only=False, skip_launch=True):
        if w.decides:
            out.append(WriteEvent(who, w.path, ts, w.kind, 'shell', ok, call, run, 'exit'))
        else:
            out.append(WriteEvent(who, w.path, ts, w.kind, 'shell', True if ok else None, call, run, 'window', (ts, ts)))
    return out


def close_shell_event(ev, start, end, ok):
    """The event of a shell command that has ended, with the result and the window the check looks in."""
    if ev.proof == 'exit':
        return dataclasses.replace(ev, ok=ok)
    return dataclasses.replace(ev, ok=True if ok else None, span=(start, end))


class ClaudeCalls(object):
    """The tool calls of one Claude record (an agent's, or the orchestrator's) from the call to its result: a Write, Edit, MultiEdit or NotebookEdit is a write event; a Bash call is a command
    window, the write events of the files it writes and what it reads. A result closes them (`ok` of the result); a background command is closed by its completion notice, when it has one."""

    def __init__(self, who, log, run_of):
        self.who, self.log, self.run_of = who, log, run_of     # run_of(): the number of the run the record is in now
        self.pend = collections.OrderedDict()                  # tool_use id -> {'kind': 'write' | 'bash', 'ts', 'events'}
        self.bg = {}                                           # the id of a background task -> the tool_use id of its call
        self.seen = 0                                          # how many of the notices of the ledger were looked at
        self.cmds = {}                                         # tool_use id -> (text, folder) of the Bash calls that name BULLPEN_ROOM or BULLPEN_SEAT (read for the room tag of what they launched)

    def reset(self):
        self.pend.clear()
        self.bg.clear()
        self.seen = 0

    def use(self, ts, b, name, inp, cwd):
        """A tool_use block of an assistant line. `cwd` is the folder of the line. A line with no time makes no event (the events are in the order of their time). A Bash call gives back the write events it made
        (the page's list of the markdown files a call wrote is made of them: the command is read once), None when it made none because it could not be taken in."""
        call = b.get('id')
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return
        if name in WRITE_TOOLS:
            path = inp.get('notebook_path') if name == 'NotebookEdit' else inp.get('file_path')
            if not (isinstance(path, str) and os.path.isabs(path)):
                return
            ev = self.log.add_write(WriteEvent(self.who, os.path.normpath(path), ts, 'unknown' if name == 'Write' else 'update', 'tool', None, call, self.run_of(), 'tool'))
            if ev is not None and call:
                self._wait(call, {'kind': 'write', 'ts': ts, 'events': [ev]})
        elif name == 'Bash' and isinstance(inp.get('command'), str) and call:
            cmd, run = inp['command'], self.run_of()
            if 'BULLPEN_' in cmd:
                self.cmds[call] = (cmd, cwd)
            events = [e for e in (self.log.add_write(e) for e in shell_events(self.who, ts, run, call, cmd, cwd)) if e is not None]
            reads = shell_reads(cmd, cwd)
            for path in reads:
                self.log.add_read(ReadEvent(self.who, path, ts, 'shell'))
            self.log.add_window(CmdWindow(self.who, call, ts, None, None, tuple(reads)))
            self._wait(call, {'kind': 'bash', 'ts': ts, 'events': events})
            return events

    def _wait(self, call, entry):
        self.pend[call] = entry
        while len(self.pend) > PENDING_KEEP:
            self.pend.popitem(last=False)

    def result(self, ts, block, d, ledger):
        """A tool_result block of a user line (`d`). A foreground call ends here; a call that went to the background ends with its completion notice."""
        call = block.get('tool_use_id')
        p = self.pend.get(call)
        if p is None:
            return
        err = bool(block.get('is_error'))
        tur = d.get('toolUseResult') if isinstance(d.get('toolUseResult'), dict) else {}
        if p['kind'] == 'write':
            del self.pend[call]
            ev = p['events'][0]
            kind = WRITE_KIND_OF_RESULT.get(tur.get('type'), 'unknown') if ev.kind == 'unknown' else ev.kind
            self.log.replace_write(ev, dataclasses.replace(ev, ok=not err, kind=kind))
        elif tur.get('backgroundTaskId') and not err:
            task = tur['backgroundTaskId']
            self.bg[task] = call
            for note in ledger.notes:                           # (its notice came first)
                if note.task == task:
                    self.note(note)
        else:
            self._finish(call, ts, not err)

    def note(self, note):
        """A completion notice (runstate.Note): the end of a background command."""
        call = self.bg.get(note.task) if note.task else None
        if call is None and note.tool_use_id in self.pend:
            call = note.tool_use_id
        if call in self.pend:
            self._finish(call, note.ts, note.status == 'completed' and note.exit_code in (None, 0))

    def pump(self, ledger):
        """The notices the ledger has taken in since the last time."""
        notes = ledger.notes
        for note in notes[self.seen:]:
            self.note(note)
        self.seen = len(notes)

    def _finish(self, call, t1, ok):
        p = self.pend.pop(call, None)
        if p is None:
            return
        for task in [t for t, c in self.bg.items() if c == call]:
            del self.bg[task]
        self.log.close_window(call, t1, ok)
        for ev in p['events']:
            self.log.replace_write(ev, close_shell_event(ev, p['ts'], t1, ok))


class OpenExecs(object):
    """The exec calls of a Codex thread that have not ended: those whose output has not come (a command that runs now in the foreground, and the ones that run inside such a call), and those
    whose output said `Script running with cell ID N` (the cell went on in the background: its command is still running and its record is to come). call id -> (time, text of the command,
    folder it is made in, running in the background); the text is the `cmd` of the one `tools.exec_command` the call makes, None when it makes none or several. A turn that begins or ends
    forgets them (a call of an earlier turn that never got its output is no command running now)."""
    KEEP = 64
    RUNNING = 'Script running with cell ID'

    def __init__(self):
        self.calls = collections.OrderedDict()

    def add(self, call_id, ts, js):
        got = codex_exec_command(js)
        self.calls[call_id] = (ts, got[0] if got else None, got[1] if got else None, False)
        while len(self.calls) > self.KEEP:
            self.calls.popitem(last=False)

    def output(self, call_id, output=None):
        """The output of the call came. A cell that answers that it is still running keeps its command (it is no longer an exec call that is open: the commands after it are not run inside it)."""
        text = output if isinstance(output, str) else ' '.join(b.get('text') or '' for b in output if isinstance(b, dict)) if isinstance(output, list) else ''
        got = self.calls.get(call_id)
        if got is not None and self.RUNNING in text[:200]:
            self.calls[call_id] = got[:3] + (True,)
        else:
            self.calls.pop(call_id, None)

    def finished(self, text):
        """The command with this text has ended (its record came): a cell that kept it forgets it."""
        for cid in [c for c, v in self.calls.items() if v[3] and v[1] == text]:
            del self.calls[cid]

    def clear(self):
        self.calls.clear()

    def only(self):
        """The time of the one exec call whose output has not come, None when none or several are (nothing says which one a command ran in)."""
        opened = [v[0] for v in self.calls.values() if not v[3]]
        return opened[0] if len(opened) == 1 else None

    def commands(self):
        """[(call id, time, text, folder)] of the calls that have not ended and whose command text is known."""
        return [(cid, v[0], v[1], v[2]) for cid, v in self.calls.items() if v[1]]


def cx_item(log, who, ts, it, cwd, run, wrapper_ts=None):
    """One completed item of a Codex rollout (`item_completed`). A `FileChange` that completed is a write event for each file it added or changed (a move is a new file at the new path; a delete
    is none). A `CommandExecution` is a command window, the write events of the files it writes (`shell_events`) and, from Codex's own reading of it (`parsed_cmd`), the files it read.
    `cwd`: the folder of the thread when the item has none. `wrapper_ts`: the time of the one exec call that was open when the command ran (the window starts there), else None."""
    kind = it.get('type') if isinstance(it, dict) else None
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return
    if kind == 'FileChange':
        changes = it.get('changes')
        if it.get('status') != 'completed' or not isinstance(changes, dict):
            return
        for path, info in changes.items():
            how = info.get('type') if isinstance(info, dict) else None
            if how == 'update' and isinstance(info.get('move_path'), str) and info['move_path']:
                path, how = info['move_path'], 'create'
            elif how not in ('add', 'update'):
                continue
            if isinstance(path, str) and not os.path.isabs(path) and cwd:
                path = os.path.join(cwd, path)
            if isinstance(path, str) and os.path.isabs(path):
                log.add_write(WriteEvent(who, os.path.normpath(path), ts, 'create' if how == 'add' else how, 'tool', True, it.get('id'), run, 'tool'))
    elif kind == 'CommandExecution':
        cwd = file_path(it.get('cwd')) or cwd
        dur = it.get('duration') if isinstance(it.get('duration'), dict) else {}
        secs, nanos = dur.get('secs'), dur.get('nanos')
        start = ts - (secs + (nanos or 0) / 1e9) if isinstance(secs, (int, float)) and not isinstance(secs, bool) else ts
        code, status = it.get('exit_code'), it.get('status')
        ok = True if (status == 'completed' and code == 0) else False if (status == 'failed' or (isinstance(code, int) and not isinstance(code, bool) and code != 0)) else None
        call = it.get('id') if isinstance(it.get('id'), str) else None
        reads = []
        for pc in it.get('parsed_cmd') or []:
            if isinstance(pc, dict) and pc.get('type') == 'read' and isinstance(pc.get('path'), str) and pc['path']:
                path = os.path.normpath(os.path.join(cwd or '/', pc['path']))
                if path not in reads:
                    reads.append(path)
                log.add_read(ReadEvent(who, path, ts, 'codex'))
        if call:
            log.add_window(CmdWindow(who, call, wrapper_ts if wrapper_ts is not None and wrapper_ts <= start else start, ts, ok, tuple(reads)))
        text = shell_command(it.get('command'))
        if text is not None and call:
            for ev in shell_events(who, ts, run, call, text, cwd, ok):                    # the time of the event is the time of the item, as a FileChange's is: the write is somewhere in the window
                log.add_write(close_shell_event(ev, start, ts, ok) if ev.proof == 'window' else ev)


class Agent:
    def describe(self, description):
        """Sets the description; the short role marker at its head (T1-A, A, opus-1) is the tag, the rest the title."""
        self.description = description
        m = re.match(r'^(T\d+-[A-Z]|[A-Z]|[a-z]+-\d+)\s+(.*)$', description)
        self.tag, self.title = (m.group(1), m.group(2)) if m else ('', description)

    def __init__(self, agent_id, meta):
        self.id = agent_id
        self.description = meta.get('description') or agent_id
        self.agent_type = meta.get('agentType') or ''
        self.model_hint = meta.get('model') or ''
        self.tool_use_id = meta.get('toolUseId')
        # a sub-agent started by a sub-agent (a grandchild): meta carries the parent agent id and the depth. Its completion notice is left in the parent agent's record, not in the main record
        self.parent_agent = meta.get('parentAgentId') if isinstance(meta.get('parentAgentId'), str) else None
        self.depth = meta.get('spawnDepth')
        self.host = None                                 # a sub-agent that a `claude -p` child of the page started: the session id of that child (the page of the top orchestrator shows it under the child)
        self.relay = None                                # a descendant Claude of a page: what its record tells the page's Codex linker (Bash calls, results, TaskStop, notices of background jobs)
        self.child_notes = []                            # completion notices, left in this agent's own record, for the agents it started {ts, task, status, summary, usage}
        self.child_results = []                          # results that this agent's Agent tool calls got back (completions, not ones still being waited for) {ts, tool_use_id, agent_id, status}
        self.describe(self.description)
        self.model = ''
        self.effort = ''
        self.spawn_prompt = None
        self.spawn_ts = None
        self.first_ts = None
        self.last_ts = None
        self.tool_count = 0
        self.tool_counts = collections.Counter()
        self.activity = collections.deque(maxlen=600)   # {ts, kind, name, text}
        self.ticks = []                                  # (ts, category)
        self.texts = collections.deque(maxlen=40)
        self.cwd = ''                                    # working folder from the first record that carries one (Codex: set from its index entry)
        self.received = []                               # {ts, text}
        self.writes = []                                 # {ts, path, id, ok}: ok (True/False) appears when the tool result of that Write/Edit is known, not before. An entry kept at completion (Codex) has neither
        self._by_call = {}                               # Write/Edit/Bash/SendMessage tool_use id -> its entry of `writes`, `shell_writes` or `sent`, until the result comes
        self.shell_writes = []                           # {ts, paths, id, ok}: the markdown files a Bash call writes by a shell redirect or tee (shell_writes()); ok as in `writes`. Kept apart: `writes` is what the tools wrote
        self.redirects = []                              # cli: the output redirects of the launching command (facts.Redirect) that name this child's report
        self.run_redirects = []                          # cli: the same for every run of a child that was resumed (a list of lists, run by run); empty: the first run's alone are known
        self.reads = {}                                  # path -> ts
        self.read_log = []                               # (ts, path) in the order read
        self.sent = []                                   # {ts, to, summary, text, id, ok}: the SendMessage calls this agent made; ok as in `writes` (a message that was not delivered is no talk)
        self.pending = {}                                # tool calls with no result yet: id -> {ts, name, text}
        self.tokens = TokenMeter()
        self.last_stop = None
        self.errors = 0
        # filled in from the main record
        self.notifications = []                          # {ts, status, summary, usage}
        self.handbacks = []                              # {ts, text}
        self.orch_msgs = []                              # {ts, summary, text}
        self.stopped_ts = None
        self.provider = 'claude'
        self.origin = 'subagent'                         # subagent | exec (Codex) | cli (claude -p started from Bash)
        self.cli = None                                  # cli: ownership info (an item of LINKS.cli_owners)
        self.talk = []                                   # cli: the conversation exchanged with the orchestrator {ts, kind: in (instruction)|end (last text of a turn), text}
        self._talk_keys = set()                          # so the same line is not counted twice even when the record is read again (Tail from the start)
        self.out_paths = []                              # Codex: planned -o paths {ts, path}
        self.auto_tag = ''                               # model name to show on the page when there is no role name (opus5.5, opus5.5-2)
        self.runs = RS.RunTracker(agent_id)              # the process lifetimes (runs) of this record file, in the order the lines came (board/runstate.py)
        self.ledger = RS.Ledger()                        # what this record knows about the children it launched: completion notices, TaskStop calls, background task ids
        # what the debate judgment reads (board/facts.py: WriteEvent, ReadEvent, CmdWindow, Planned): the old `writes`, `shell_writes`, `reads`, `out_paths` and `redirects` above stay as they were
        self.ev = EventLog(WINDOWS_KEEP)
        self.calls = ClaudeCalls(agent_id, self.ev, self._run_no)
        self.spawn_msgs = {}                             # Agent tool_use id -> the message.id of the line that holds it (the call this agent made to start another: the group of the launch key)
        self.launch = None                               # facts.LaunchKey: the call that launched this agent, None when it is not known (set by the session)
        self.room_tag = None                             # facts.Tag: BULLPEN_ROOM / BULLPEN_SEAT of the agent (`tag` is the name tag above)
        self.run = None                                  # the number of the last run (the Claude RunTracker epoch, the Codex turn n) and when it started
        self.run_start = 0.0
        self.launch_src = None                           # a native Codex sub-agent: (the thread whose record holds the `started`, the id of the call, the time) of the call that spawned it
        self.tag_env = None                              # (room, seat) its live process had in its environment when it was last read: what a child that has ended is still told by
        self.plan_want = None                            # cli: what the planned output of the child and its event were made from (the session makes them again when it changes)
        self.plan_events = ()

    def _run_no(self):
        return self.runs.runs[-1].epoch if getattr(self.runs, 'runs', None) else None

    def set_fact(self, name, value):
        """Sets `launch`, `room_tag`, `run` or `run_start`; what the judgment reads changed when the value did (`facts_gen`)."""
        with self.ev.lock:
            if getattr(self, name) != value:
                setattr(self, name, value)
                self.ev.bump()

    write_events = property(lambda self: self.ev.events())
    read_events = property(lambda self: self.ev.reads)
    windows = property(lambda self: self.ev.windows)
    windows_dropped = property(lambda self: self.ev.windows_dropped)
    writes_dropped = property(lambda self: self.ev.writes_dropped)
    lost = property(lambda self: self.ev.lost)
    planned = property(lambda self: self.ev.planned)
    facts_gen = property(lambda self: self.ev.gen)               # goes up when anything the debate judgment reads changes: a cached judgment is good while it is the same (J19)

    def reset_runs(self):
        """The record is read again from its start (the file shrank): the trackers are rebuilt from the same lines, so nothing is counted twice."""
        self.runs, self.ledger = RS.RunTracker(self.id), RS.Ledger()
        self.ev.reset_record()
        self.calls.reset()

    @property
    def name_tag(self):
        """The name to show on the page: the role or report name, else the model name. Matching to debate cells (key) uses only tag."""
        return self.tag or self.auto_tag

    @property
    def key(self):
        """Participant key to match against report file names (T1-C → c, opus-1 → opus1, A → a)."""
        t = self.tag
        m = re.match(r'^T\d+-([A-Za-z])$', t)
        if m:
            return m.group(1).lower()
        return norm_key(t)

    def feed(self, d):
        typ = d.get('type')
        rc = RS.rec(d)                    # the time and the content blocks are read once for the tracker, the ledger and this
        ts = rc.ts
        if isinstance(self.runs, RS.RunTracker):
            self.runs.feed(d, rc)
        else:                                 # a Codex thread's tracker takes the raw rollout line
            self.runs.feed(d)
        self.ledger.feed(d, rc)
        self.calls.pump(self.ledger)
        if not self.cwd and isinstance(d.get('cwd'), str):
            self.cwd = d['cwd']
        if typ == 'assistant':
            self._touch(ts)
            if d.get('effort'):
                self.effort = d['effort']
            m = d.get('message') or {}
            if m.get('model') and not m['model'].startswith('<'):
                self.model = m['model']
            self.tokens.add(m)
            if m.get('stop_reason'):
                self.last_stop = m['stop_reason']
            api_error = bool(d.get('isApiErrorMessage')) and RS.error_of(d) is not None      # the line of an API error ("You've hit your limit ..") is the board's news (the state says it), not something the agent said
            msg_id = m.get('id') if isinstance(m.get('id'), str) else None
            for b in rc.blocks:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text' and b.get('text', '').strip() and not api_error:
                    self._say(ts, b['text'].strip())
                elif b.get('type') == 'tool_use':
                    name, inp = b.get('name', ''), b.get('input') or {}
                    self._tool(ts, name, trunc(tool_brief(name, inp), 240), b.get('id'))
                    made = None
                    if isinstance(inp, dict):
                        made = self.calls.use(ts, b, name, inp, d.get('cwd') if isinstance(d.get('cwd'), str) and d.get('cwd') else self.cwd)
                        if name == 'Read' and isinstance(ts, (int, float)) and isinstance(inp.get('file_path'), str) and os.path.isabs(inp['file_path']):
                            self.ev.add_read(ReadEvent(self.id, os.path.normpath(inp['file_path']), ts, 'tool'))
                        elif name == 'Agent' and b.get('id') and msg_id:
                            self.spawn_msgs[b['id']] = msg_id
                    if name in ('Write', 'Edit') and inp.get('file_path'):
                        w = {'ts': ts, 'path': os.path.normpath(inp['file_path']), 'id': b.get('id')}
                        self.writes.append(w)
                        if b.get('id'):
                            self._by_call[b['id']] = w
                    elif name == 'Read' and inp.get('file_path'):
                        self.reads[os.path.normpath(inp['file_path'])] = ts
                        self.read_log.append((ts, os.path.normpath(inp['file_path'])))
                    elif name == 'Bash' and isinstance(inp.get('command'), str):
                        if self.relay is not None:
                            self.relay.bash(d, ts, b)
                        if made is not None:                                  # (the markdown files the command writes, as the events say: it was read for them already)
                            paths = [e.path for e in made if e.path.endswith('.md')]
                        else:
                            paths = shell_writes(inp['command'], d.get('cwd') if isinstance(d.get('cwd'), str) and d.get('cwd') else self.cwd)
                        if paths:
                            w = {'ts': ts, 'paths': paths, 'id': b.get('id')}
                            self.shell_writes.append(w)
                            if b.get('id'):
                                self._by_call[b['id']] = w
                    if name == 'TaskStop' and self.relay is not None:
                        self.relay.stop(inp.get('task_id') or '', ts)
                    if name == 'SendMessage':
                        msg = inp.get('message')
                        m = {'ts': ts, 'to': str(inp.get('to', '')), 'summary': inp.get('summary') or '', 'id': b.get('id'),
                             'text': msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)}
                        self.sent.append(m)
                        if b.get('id'):
                            self._by_call[b['id']] = m
            if self.origin == 'cli' and m.get('stop_reason') == 'end_turn':
                self._cli_end(ts, d, m)
        elif typ == 'attachment':
            a = d.get('attachment') or {}
            if a.get('type') == 'model':
                self.tokens.model((a.get('identity') or {}).get('modelId'))
            elif a.get('type') == 'queued_command' and a.get('commandMode') == 'task-notification':
                self._child_note(parse_ts(a.get('timestamp')) or ts, as_text(a.get('prompt')), a.get('usage'))
                if self.relay is not None:
                    self.relay.notice(as_text(a.get('prompt')), parse_ts(a.get('timestamp')) or ts)
        elif typ == 'user':
            self._touch(ts)
            c = (d.get('message') or {}).get('content')
            if self.origin == 'cli' and not d.get('isMeta'):
                self._cli_prompt(ts, d, c)
            if isinstance(c, str):
                self._user_text(ts, c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'tool_result':
                        self.calls.result(ts, b, d, self.ledger)
                        if self.relay is not None:
                            self.relay.result(b.get('tool_use_id'), d, ts)
                        called = self.pending.pop(b.get('tool_use_id'), None)
                        if called and called['name'] == 'Agent':
                            self._child_result(ts, b, d.get('toolUseResult'))
                        w = self._by_call.pop(b.get('tool_use_id'), None)
                        if w is not None:
                            w['ok'] = not b.get('is_error')         # a failed write is no report and no seat; a message that was not delivered is no talk
                    if b.get('type') == 'tool_result' and b.get('is_error'):
                        self.errors += 1
                        self.ticks.append((ts, 'error'))
                    elif b.get('type') == 'text':
                        self._user_text(ts, b.get('text', ''))

    def _child_note(self, ts, text, usage):
        """The completion notice (task-notification) of an agent it started. Only the tags at the front of the notice text are read (the <result> after them is long)."""
        head = text[:4000]
        task, status, summary = (re.search(r'<%s>([^<]+)</%s>' % (t, t), head) for t in ('task-id', 'status', 'summary'))
        if not task or not RS.is_agent_id(task.group(1)):
            return                                       # a notice of the agent's own Bash background task: no agent finished, so no "work finished" card
        u = usage if isinstance(usage, dict) else {}
        self.child_notes.append({'ts': ts, 'task': task.group(1), 'status': status.group(1) if status else '', 'summary': summary.group(1) if summary else '',
                                 'usage': (u.get('totalTokens'), u.get('toolUses'), u.get('durationMs')) if u else None})

    def _child_result(self, ts, block, result):
        """Its own Agent tool call got a result back. The confirmation that it started in the background (async_launched) is not a completion. Only a status in the closed list counts as complete."""
        r = result if isinstance(result, dict) else {}
        if r.get('isAsync') or r.get('status') == 'async_launched':
            return
        status = 'failed' if block.get('is_error') else r.get('status') if r.get('status') in ('completed', 'failed', 'killed') else None
        if status:
            self.child_results.append({'ts': ts, 'tool_use_id': block.get('tool_use_id'), 'agent_id': r.get('agentId'), 'status': status})

    def _tool(self, ts, name, text, call_id=None):
        """One tool call: count, tick and activity; if there is a call_id it is also recorded as a call waiting for its result. text is already within 240 characters."""
        cat = tool_category(name)
        self.tool_count += 1
        self.tool_counts[name] += 1
        self.ticks.append((ts, cat))
        entry, wait = {'ts': ts, 'kind': 'tool', 'name': name, 'text': text, 'cat': cat}, {'ts': ts, 'name': name, 'text': trunc(text, 160)}
        if name in TOOL_TEXT_KEY:
            entry['text_i18n'] = wait['text_i18n'] = {'key': TOOL_TEXT_KEY[name], 'params': {}}
        self.activity.append(entry)
        if call_id:
            self.pending[call_id] = wait

    def _say(self, ts, text):
        """What the agent said (text with leading and trailing whitespace stripped)."""
        self.texts.append({'ts': ts, 'text': text})
        self.activity.append({'ts': ts, 'kind': 'text', 'name': '', 'text': trunc(text, 400), 'cat': 'other'})

    def _user_text(self, ts, text):
        if self.spawn_prompt is None:
            self.spawn_prompt = text
            self.spawn_ts = self.spawn_ts or ts
            return
        text = strip_reminders(text)
        if text.startswith('<task-notification>'):
            if self.relay is not None:
                self.relay.notice(text, ts)
            if self.origin == 'cli':                     # a `claude -p` child is a session of its own: the notices of the agents it started are in its record as user lines
                u = re.search(r'<subagent_tokens>(\d+)</subagent_tokens>.*?<tool_uses>(\d+)</tool_uses>.*?<duration_ms>(\d+)</duration_ms>', text, re.S)
                self._child_note(ts, text, {'totalTokens': int(u.group(1)), 'toolUses': int(u.group(2)), 'durationMs': int(u.group(3))} if u else None)
            return
        if not text:
            return
        if text.startswith(COORD_PREFIX):
            text = text[len(COORD_PREFIX):].strip()
        self.received.append({'ts': ts, 'text': text})
        self.activity.append({'ts': ts, 'kind': 'msg', 'name': '', 'text': trunc(text, 400), 'cat': 'msg'})
        self.ticks.append((ts, 'recv'))

    def _cli_end(self, ts, d, m):
        """The last text of a turn that ended with end_turn. A record holding only thinking has no text, so it is skipped and the text is caught from the text record of the same message (stop_sequence etc. is not the end of a turn)."""
        said = [b['text'].strip() for b in m.get('content') or []
                if isinstance(b, dict) and b.get('type') == 'text' and (b.get('text') or '').strip()]
        if said:
            self._talk(ts, 'end', m.get('id') or d.get('uuid') or d.get('timestamp'), said[-1])

    def _cli_prompt(self, ts, d, c):
        """The user instruction a claude -p child session received (the first instruction, and one given by continuing with --resume). Tool results, notices and isMeta (image notes) are not instructions."""
        text = c if isinstance(c, str) else '\n'.join(
            b.get('text') or '' for b in (c or []) if isinstance(b, dict) and b.get('type') == 'text')
        text = strip_reminders(text)
        if not text or text.startswith('<task-notification>'):
            return
        if text.startswith(COORD_PREFIX):
            text = text[len(COORD_PREFIX):].strip()
        self._talk(ts, 'in', d.get('uuid') or '%s|%s' % (d.get('timestamp'), text), text)

    def _talk(self, ts, kind, key, text):
        """One cli conversation item. If (kind, key) is the same it is not added again: key is the uuid of the record line or the message id."""
        if (kind, key) not in self._talk_keys:
            self._talk_keys.add((kind, key))
            self.talk.append({'ts': ts, 'kind': kind, 'text': text})

    def _touch(self, ts):
        if ts is None:
            return
        self.first_ts = self.first_ts or ts
        self.last_ts = max(self.last_ts or 0, ts)


def model_numbers(items, base_of):
    """Gives names to agents laid out in order: if several have the same model, base, base-2, base-3, … → {id: name}. Sorting and filtering the targets is up to the caller."""
    seen, out = collections.Counter(), {}
    for a in items:
        base = base_of(a)
        seen[base] += 1
        out[a.id] = base if seen[base] == 1 else '%s-%d' % (base, seen[base])
    return out


def cx_sync_base(meter, partial, thread_total, seen, model):
    """A big rollout whose front part was not read: the tokens of the unread span are got by subtracting what was seen from the last thread_token_usage, and added as one lump."""
    if partial and thread_total:
        base = {k: max(0, (thread_total.get(k) or 0) - seen.get(k, 0)) for k in
                ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
                 'reasoning_output_tokens')}
        meter.add_codex('__base__', base, model, ctx=False, calls=0)


def cx_record_usage(h, meter, p, model):
    """One token_usage_record line: add it to the meter, put it on the list to recount if the model is unknown, and update the total of the span read (seen) and the thread's running total.
    h is a CodexAgent or CodexSession that has seen, nomodel and thread_total."""
    u = p.get('usage') or {}
    rid = p.get('response_id') or 'r%d' % len(meter.by_msg)
    meter.add_codex(rid, u, model)
    if not model:
        h.nomodel.append((rid, u))
    cx_usage_add(h.seen, u)
    h.thread_total = p.get('thread_token_usage') or h.thread_total


def cx_reprice(meter, nomodel, model):
    """Recounts calls that were counted without knowing the model, once the model is known (same id, so it is swapped in)."""
    if model and nomodel:
        for rid, u in nomodel:
            meter.add_codex(rid, u, model, ctx=False)
        nomodel.clear()


def cx_sync_guardians(meter, parent_id):
    """Puts the cumulative tokens of guardian (approval review) child threads into the parent total (the subset tokens.guardian). Activity is not read.
    A native sub-agent is a child thread too, but its tokens are its own card's (CodexAgent counts them): only `kind == 'guardian'` comes in here."""
    for g in CODEX.children(parent_id):
        if g.get('kind') == 'guardian' and g['thread_total']:
            meter.add_codex('guardian:' + g['id'], g['thread_total'], g['model'] or 'codex-auto-review',
                            ctx=False, guardian=True, calls=g['calls'])


class ForkSkip:
    """The front of a native sub-agent's rollout is its parent's history, copied when it was spawned: the lines whose ordinal is at most `prefix_ord` (a line with no ordinal
    counts by its place in the file: the meta line is 0, the next 1 ...). None of it is the sub-agent's own turn, activity, tool call, instruction or tokens, so the reader leaves
    it out before anything is fed. Read from the start of the file, the first line behind it ends the copied part for good. A read that starts inside the file (`partial`, the tail
    of a big rollout) may still hold some of it, and nothing says where it began: each line is told by its own ordinal, and a line with none is not known to be the thread's
    own, so it is left out too. `prefix_ord` None: a thread that is no sub-agent has no copy."""

    def __init__(self, prefix_ord=None, partial=False):
        self.prefix = prefix_ord or 0
        self.partial = bool(partial) and prefix_ord is not None
        self.sub = prefix_ord is not None
        self.restart()

    def restart(self):
        """The rollout is read again from its start."""
        self.line = 0
        self.over = not self.sub or (not self.partial and not self.prefix)
        self.prev, self.bad = None, None


def cx_rows(lines, fork=None):
    """The decoded rollout lines of one read (cx_decode), without the lines `fork` (a ForkSkip) says are the parent's copy. A line that cannot be decoded is no row, and
    still has its place in the count. A line of a write that cannot be decoded (it is no JSON, and its head says it is a command or a file change, or it was cut before that could be read) is a hole in
    the time of the record, unless it is the last line of a process that died writing it (`codex_parse.cx_hole`, CONTRACT O17): it waits for the next *physical* line (whatever that line is, readable or not:
    its head says whether a process began) and the hole comes as a row of kind `hole` with its `span`, before that line; when there is no next line yet it waits for the next read (`fork` keeps what is
    waiting and the time of the line before). The scan of the front of a big rollout (`codex_scan.scan`) judges the same lines the same way."""
    guard = fork if fork is not None else ForkSkip()
    for raw in lines:
        r = cx_decode(raw)
        b = cx_broken(raw) if r is None else None
        if r is not None and r['ts'] is None and cx_write_item_of(r):       # a write whose time cannot be read cannot be put in the record: it is as unread as one that is cut short
            b, r = {'ts': None, 'ord': r['ord'], 'type': r['type']}, None
        head = {'ts': r['ts'], 'ord': r['ord'], 'type': r['type']} if r else (b if b is not None else (cx_head(raw) or {}))
        o = head.get('ord')
        known = r is not None or b is not None
        if fork is not None and fork.partial:
            if known and (o is None or o <= fork.prefix):
                continue
        elif fork is not None and not fork.over:
            n, fork.line = fork.line, fork.line + 1
            if (o if known and o is not None else n) <= fork.prefix:
                continue
            fork.over = True
        if guard.bad is not None:                          # the line after a broken one says whether the broken one was the last of its process
            span = cx_hole(guard.bad[0], guard.bad[1], head)
            guard.bad = None
            if span is not None:
                yield {'ts': span[1], 'type': 'hole', 'pt': '', 'ord': None, 'p': {}, 'span': span}
        if b is not None:
            guard.bad = (b, guard.prev)
        if head.get('ts') is not None:
            guard.prev = head['ts']
        if r is not None:
            yield r


class CodexAgent(Agent):
    """A Codex thread that a Claude session started with Bash `codex exec` = one agent. A turn = an instruction received, the turn's final answer = the final report."""

    def __init__(self, e, link):
        Agent.__init__(self, e['id'], {})
        self.kind = e.get('kind') or 'root'
        self.provider, self.origin = 'codex', 'subagent' if self.kind == 'sub' else 'exec'      # a native sub-agent thread of Codex, or a `codex exec` thread started from a shell
        self.tag = self.title = self.description = ''
        self.agent_path, self.nick = e.get('agent_path'), e.get('nick')
        self.fork = ForkSkip(e.get('prefix_ord') if self.kind == 'sub' else None)      # the parent's copied history at the front of a sub-agent's rollout is none of its own (set again once `partial` is known)
        self.cuts = []             # sub-agent: the times its parent wrote `SubAgentActivity interrupted` for it
        self.runtime = None        # sub-agent: (id, rollout path) of the root thread, the process it lives in has that rollout open
        self.report_tag = ''       # report name (r<N>/<name>.md). If none, tag = the model name (sol6.1, sol6.1-2)
        self.meta_ts = e['meta_ts']
        self.link = link
        self.path = e['path']
        self.cwd = e['cwd']
        self.turns = []            # {n, start, end, status, error, sha, msg, user, call, bash_ts, out, out_state}. After the events are produced, trim_turns keeps only 200
        self.turn_seq = 0          # monotonically increasing number given to turns (n). Used for event dedup keys and first-turn detection, and unrelated to trimming the kept list
        self.partial = 0
        self.seen = {}
        self.thread_total = None
        self.tokens.fixed_limit = True
        self.model = e['model']
        self.nomodel = []          # calls that came before the model was known (when reading from the end): recounted once the model is known
        self.runs = RS.CodexTracker(e['id'])      # a Codex process lives for one `exec`: a run is a turn (board/runstate.py)
        self._cx_execs = OpenExecs()   # the exec calls of the open turn whose output has not come (the commands run inside one of them: the window of a command starts with it)
        self.front = FrontScan()   # the read of the front of a big rollout (before `partial`), in the background: until it is done that part is `lost`
        self._first_row = None     # the time of the first line read (a big rollout is read from its end: what is before it was not read)

    def _run_no(self):
        return self.turns[-1]['n'] if self.turns else None

    def reset_runs(self):
        self.runs = RS.CodexTracker(self.id)
        self.ev.reset_record()
        self._cx_execs.clear()
        self._first_row = None
        self.front.reset()

    def front_scan(self, wait=False):
        """Begins the read of the front of the rollout when it was read from its end (`partial`): its commands and file changes make the events that are `lost` until then. Returns the thread.
        `wait`: the caller waits for it (a test)."""
        t = self.front.start(self.path, self.partial, self.ev, self.id, self.cwd, self.fork.prefix if self.fork.sub else None) if self.partial else None
        if wait and t is not None:
            t.join()
        return t

    def trim_turns(self):
        """Trims the kept turns to the last TURNS_KEEP. Call it **after** the events have been derived (CodexLinker._derive):
        so that the events of earlier turns are not cut off and lost when a lot was read at once (the initial read)."""
        if len(self.turns) > TURNS_KEEP:
            del self.turns[:-TURNS_KEEP]

    def feed_cx(self, r):
        ts, typ, pt, p = r['ts'], r['type'], r['pt'], r['p']
        if typ == 'hole':                   # a write of the record that cannot be read (`cx_rows`)
            self.ev.widen_lost(*r['span'])
            return
        self.runs.feed_cx(ts, typ, pt, p)
        if typ == 'session_meta':
            return
        if self.partial and self._first_row is None and ts:
            self._first_row = ts
            if self.front.state != 'done':                       # (a scan that was done before the first line came has read the front already)
                self.ev.set_front_lost(self.spawn_ts or self.meta_ts or ts, ts)       # the front of a big rollout was not read: from its start to the first line read
        if p is None:                       # a line over 1 MB: only close the tool call that was waiting
            if pt in ('custom_tool_call_output', 'function_call_output'):
                m = CX_CALL_ID_RE.search(r['head'])
                if m:
                    self.pending.pop(m.group(1).decode(), None)
                    if pt == 'custom_tool_call_output':
                        self._cx_execs.output(m.group(1).decode())
            elif pt == 'item_completed' and ts and (b'"CommandExecution"' in r['head'] or b'"FileChange"' in r['head']):
                self.ev.widen_lost(ts, ts)      # a write or a command that was too long to be read
            self._touch(ts)
            return
        if typ == 'turn_context':
            self.model = p.get('model') or self.model
            self.effort = p.get('effort') or self.effort
            cx_reprice(self.tokens, self.nomodel, self.model)
        elif typ == 'token_usage_record':
            cx_record_usage(self, self.tokens, p, self.model)
        elif typ == 'event_msg':
            self._feed_event(ts, pt, p)
        elif typ == 'response_item':
            self._feed_item(ts, pt, p)

    def _feed_event(self, ts, pt, p):
        if pt == 'task_started':
            self._touch(ts)
            self._cx_execs.clear()
            if p.get('model_context_window'):
                self.tokens.ctx_limit = p['model_context_window']
            if self.turns and self.turns[-1]['end'] is None and self.turns[-1]['status'] == 'running':
                self.turns[-1]['status'] = 'killed'     # the next turn came without an end record (the process was stopped)
            self.turns.append({'n': self.turn_seq, 'start': ts, 'end': None, 'status': 'running', 'error': None, 'sha': None,
                               'msg': None, 'user': None, 'call': None, 'bash_ts': None, 'out': None,
                               'out_state': None})
            self.turn_seq += 1
        elif pt in ('task_complete', 'turn_aborted') and self.turns and self.turns[-1]['end'] is None:
            t = self.turns[-1]
            self._touch(ts)
            self._cx_execs.clear()
            t['end'] = ts
            self.last_stop = 'end_turn'
            if pt == 'turn_aborted':
                t['status'], t['error'] = 'killed', p.get('reason') or 'aborted'
                return
            msg = p.get('last_agent_message') or ''
            t['msg'] = msg
            t['sha'] = hashlib.sha1(msg.encode()).hexdigest() if msg else None
            err = p.get('error')
            if err:
                t['status'] = 'failed'
                t['error'] = trunc('%s %s' % (err.get('codex_error_info') or '', err.get('message') or ''), 300).strip()
                self.errors += 1
                self.ticks.append((ts, 'error'))
            else:
                t['status'] = 'done'
        elif pt == 'token_count':
            w = (p.get('info') or {}).get('model_context_window')
            if w:
                self.tokens.ctx_limit = w
        elif pt == 'item_completed':
            it = p.get('item') or {}
            if it.get('type') == 'CommandExecution':      # Codex's own parsing of the command: the files read (cross-review "read", xread)
                cwd = (it.get('cwd') or '').replace('file://', '') or self.cwd or '/'
                for pc in it.get('parsed_cmd') or []:
                    if pc.get('type') == 'read' and pc.get('path'):
                        path = os.path.normpath(os.path.join(cwd, pc['path']))
                        self.reads[path] = ts
                        self.read_log.append((ts, path))
            elif it.get('type') == 'FileChange':
                for path in it.get('changes') or {}:
                    self.writes.append({'ts': ts, 'path': os.path.normpath(path)})
            cx_item(self.ev, self.id, ts, it, self.cwd, self._run_no(), self._cx_execs.only())
            if it.get('type') == 'CommandExecution' and shell_command(it.get('command')) is not None:
                self._cx_execs.finished(shell_command(it['command']))

    def _feed_item(self, ts, pt, p):
        if pt == 'message':
            role = p.get('role')
            if role == 'user':
                text = codex_user_text(p)
                if not text:
                    return
                self._touch(ts)
                if self.turns and self.turns[-1]['user'] is None:
                    self.turns[-1]['user'] = text
                if not self.title:
                    self.title = self.description = trunc(text.strip().splitlines()[0], 80)
                self._user_text(ts, text)
            elif role == 'assistant':
                text = codex_say_text(p)
                if text:
                    self._touch(ts)
                    self._say(ts, text)
        elif pt in ('custom_tool_call', 'function_call'):
            self._touch(ts)
            name, text = codex_call(pt, p)
            self._tool(ts, name, text, p.get('call_id'))
            if pt == 'custom_tool_call' and p.get('name') == 'exec' and p.get('call_id'):
                self._cx_execs.add(p['call_id'], ts, p.get('input'))
        elif pt in ('custom_tool_call_output', 'function_call_output'):
            self._touch(ts)
            self.pending.pop(p.get('call_id'), None)
            if pt == 'custom_tool_call_output':
                self._cx_execs.output(p.get('call_id'), p.get('output'))
