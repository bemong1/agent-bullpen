#!/usr/bin/env python3
"""Harvests the *shape* of a real session tree as scenario-generator axis values plus the expected answers. Local use only; read-only.

    python3 tools/harvest.py <session id or prefix> [--claude-home DIR] [--json] [--now EPOCH]
    python3 tools/harvest.py --check FILE        # is every token of an earlier output inside the allowed vocabulary? (exit 0 / 1)

What comes out: lines of `aff:target=cli;spawner=main;...` (a case id of `tools/scenarios/axes.py`, so `Case.from_id` reads it back) with how often it
occurred, and the truth `tools/scenarios/oracle.py` computes from those axis values. Nothing else: no text, path, session or agent id, time, or hash of
any of them. That is enforced, not hoped for: every line goes through `Out`, which refuses a token that is not in the closed vocabulary (axis names and
values, truth fields and enumerations, the oracle's symbolic names, a fixed set of keywords, and small numbers). `--check` runs the same test on a file.

What does not come out as a spec: a feature the axes cannot express. It is reported as `new axis value needed: <feature>` (the feature names are a fixed
set below, never taken from a record) and the exit code is 2. Axes that a finished record cannot show (`seen`, `os`, `bait`) are filled with the value a
replay of a historical record needs and listed as `unobserved`.

Two functions are the interface for other tools: `read_tree(claude_home, sid)` reads the records (this is where the texts are, in memory only) and
`classify(tree, now)` turns them into cases (pure: no file access except `os.path` look-ups of the files the calls name, which are only measured, never kept).
It does not use the board's judgments (its own linking has the misses this tool is meant to find): only the definitions in `board/facts.py` and, for a file a
record points at (a script, an output file), the board's own safe way of opening it (`board.util.open_safe`: no link on the way, no hidden or secret name, no auth
file, regular files only, never blocking on a pipe).
"""

import argparse
import bisect
import collections
import glob
import json
import os
import re
import stat
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.scenarios import axes as A  # noqa: E402
from tools.scenarios import oracle as O  # noqa: E402

NEW_AXIS = 'new axis value needed: '
LINE_MAX = 16 << 20                 # a record line longer than this is skipped, not parsed
TEXT_CAP = 64 << 10                 # how much of a written file / a command is kept in memory
SHORT = A.SHORT_PROMPT_CHARS
ANCHORS, ANCHOR_LEN = 16, 32        # sixteen 32-character anchors of the normalised instruction
COVER_MIN, COVER_RATIO = 0.4, 2.0
WINDOW_BEFORE, WINDOW_AFTER = 7200.0, 2.0
STALE_SECS = 60                     # a record that has not grown for this long is not running
STALL_SECS = 1200                   # ... and for this long without an open call it is a silent stall
KNOWN_TYPES = frozenset((
    'user', 'assistant', 'system', 'attachment', 'queue-operation', 'last-prompt', 'mode', 'permission-mode', 'atis-latch', 'ai-title', 'bridge-session',
    'file-history-snapshot', 'file-history-delta', 'cost-state', 'summary', 'progress', 'custom-title', 'agent-name', 'pr-link', 'tag', 'worktree-state',
    'content-replacement', 'speculation-accept', 'marble-origami-commit', 'marble-origami-snapshot'))

# ---------------------------------------------------------------------------------------------------------------------
# The closed vocabulary of the output
# ---------------------------------------------------------------------------------------------------------------------
FEATURES = (            # names of "new axis value needed": the only free-form part of a message, and it is a fixed set
    'way_combination', 'launch_shape', 'prompt_shape', 'redirect_shape', 'record_type', 'life_shape', 'debate_shape', 'axis_combination', 'target_other',
    'launcher_other', 'resume_shape')
KEYWORDS = ('harvest', 'in', 'id', 'count', 'new_axis_value_needed', 'case', 'cases', 'expect', 'diag', 'forbid', 'unobserved', 'assumed', 'skipped', 'unlinked', 'tree', 'nodes', 'children', 'agents',
            'main', 'sessions', 'orphan', 'launches', 'new', 'axis', 'value', 'needed', 'spec', 'x', 'folded', 'from', 'to', 'none', 'T', 'None', 'True', 'False',
            'bundle', 'shapes', 'no', 'found', 'not', 'dropped', 'resume', 'calls', 'total', 'counts', 'only', 'exit', 'code', 'members', 'pids', 'n')
ORACLE_WORDS = ('orch', 'orch2', 'orch_new', 'mid', 'child', 'child2', 'sub', 'launcher', 'mention', 'paste', 'listing', 'alert', 'event',
                'turn', 'fail', 'stall', 'hb', 'ask', 'say', 'notify_stray', 'orch_say_limit', 'B', 'B_gate', 'b', 'opus1', 'sol', 'sol-final3',
                'docs', 'rev', 'rev2', 't1', 't2', 'edit1', 'records', 'unit', 'units', 'edit_rows', 'edit_rounds', 'placements', 'seat', 'round', 'ambiguous')
OTHER_WORDS = ('target', 'spawner', 'node', 'tree')
COUNTS = ('nodes', 'main', 'sub', 'child', 'nopersist_launches', 'skipped_ended_main', 'skipped_deb_roles', 'linked_by_time', 'linked_by_content', 'linked_by_out', 'hidden_scripts')
DETAILS = ('no_invocation', 'variable_prompt', 'no_prompt_file', 'output', 'report_path', 'seat_name', 'decoy_switch', 'decoy_sidmention', 'link_by_time',
           'multi_proc', 'deleted', 'at_restart', 'codex_targets')


def vocabulary():
    """The set of words the output may contain."""
    from board import facts as F          # definitions only (no logic): the status / reason / cell / diagnostic names
    words = set(KEYWORDS) | set(ORACLE_WORDS) | set(OTHER_WORDS) | set(DETAILS) | set(COUNTS) | set(FEATURES) | set(A.BUNDLES)
    for name, values in A.AXES.items():
        words.add(name)
        words.update(values)
    for group in (F.STATUSES, F.REASONS, F.CELLS, F.ROLES, F.ORCH_STATES, F.RULE_CLASSES, F.RULES, F.TRUTH_FIELDS, F.DIAG_CODES):
        words.update(group)
    return frozenset(words)


TOKEN_RE = re.compile(r'[A-Za-z0-9_@.\-]+|[^\sA-Za-z0-9_@.\-]')
NUM_RE = re.compile(r'\d{1,6}')
PUNCT_OK = frozenset(':;,=()[]{}+!#/\'"*<>?|%~^&$`\\')      # punctuation that carries no text by itself
SYMBOL_RE = re.compile(r'@[a-z0-9_]+')


class NotAllowed(Exception):
    """A token outside the vocabulary: the message never quotes it (it could be real text)."""


def token_ok(tok, vocab):
    if tok in vocab or NUM_RE.fullmatch(tok):
        return True
    if SYMBOL_RE.fullmatch(tok):
        return tok[1:] in vocab
    if tok.startswith('.') and tok[1:] in vocab:           # `.records` (an oracle unit name)
        return True
    return False


def foreign_tokens(text, vocab):
    """How many tokens of `text` are not allowed (the tokens themselves are not returned: they may be real text)."""
    bad = 0
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok[0].isalnum() or tok[0] in '_@.-':
            if tok in ('-', '.', '--') or set(tok) <= set('-._'):
                continue
            if not token_ok(tok, vocab):
                bad += 1
        elif tok not in PUNCT_OK:
            bad += 1
    return bad


class Out:
    """The only way anything leaves this tool: a line is written only if every token of it is inside the vocabulary."""

    def __init__(self, stream):
        self.stream, self.vocab = stream, vocabulary()

    def line(self, text):
        n = foreign_tokens(text, self.vocab)
        if n:
            raise NotAllowed('%d token(s) outside the allowed vocabulary in a line of %d characters' % (n, len(text)))
        self.stream.write(text + '\n')


# ---------------------------------------------------------------------------------------------------------------------
# Files a record points at
# ---------------------------------------------------------------------------------------------------------------------
NEVER_READ = ('/proc/', '/sys/', '/dev/')          # not files of the user's work: an environment, a device


def read_aux(path, cap, skip_big=True):
    """Up to `cap` bytes of a file a record points at (a script a call runs, the output file of a launch, a file a call wrote), or None.

    What a record names is not to be trusted: a redirect may point at a credentials file, a hidden folder, a link to one, a pipe that never ends, the
    environment of a process. The file is opened the way the board opens what a record points at (`board.util.open_safe`): links are resolved and refused when they
    lead to an auth file, a hidden path or a secret name, no link is followed on the way, only a regular file is opened and never blocking. Never /proc, /sys or /dev.
    With `skip_big` a file larger than `cap` is not read at all (None); otherwise its first `cap` bytes are."""
    from board.util import open_safe          # the board's own entrance (lazily: the output check and the pure readers need none of it)
    try:
        real = os.path.realpath(path)
        if real.startswith(NEVER_READ):
            return None
        with open_safe(real, binary=True) as f:
            data = f.read(cap + 1 if skip_big else cap)
    except (OSError, ValueError):                 # Denied is an OSError
        return None
    return None if skip_big and len(data) > cap else data


def open_record(path):
    """A session record (or a sub-agent's meta file) found under the Claude folder: a regular file that is not a link, opened without blocking (a pipe left
    in the folder must not stop the harvest)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('not a regular file')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


# ---------------------------------------------------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------------------------------------------------
_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def parse_ts(s):
    """ISO time of a record ('2026-10-01T05:35:21.123Z') -> epoch seconds, or None. Hand-rolled: a long record has hundreds of thousands of them."""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        y, mo, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        t = int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])
    except ValueError:
        return None
    if not 1 <= mo <= 12:
        return None
    days = (y - 1970) * 365 + (y - 1969) // 4 - (y - 1901) // 100 + (y - 1601) // 400 + _DAYS[mo - 1] + d - 1
    if mo > 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        days += 1
    t += days * 86400
    if len(s) > 20 and s[19] == '.':
        frac = re.match(r'\d+', s[20:])
        if frac:
            t += float('0.' + frac.group(0))
    return float(t)


_STRIP = str.maketrans('', '', '\\\'"`')


def norm(text):
    """NFC, drop `\\ ' " ``, collapse whitespace."""
    return ' '.join(unicodedata.normalize('NFC', text or '').translate(_STRIP).split())


def anchors_of(n):
    """Sixteen 32-character windows spread over a normalised text (none for a text shorter than 32)."""
    if len(n) < ANCHOR_LEN:
        return []
    last = len(n) - ANCHOR_LEN
    return sorted({n[(i * last) // (ANCHORS - 1): (i * last) // (ANCHORS - 1) + ANCHOR_LEN] for i in range(ANCHORS)})


def coverage(anchors, hay):
    return (sum(1 for a in anchors if a in hay) / float(len(anchors))) if anchors else 0.0


UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
NOTIF_RE = re.compile(r'<task-notification>.*?</task-notification>', re.S)


def tag(body, name):
    m = re.search(r'<%s>(.*?)</%s>' % (name, name), body, re.S)
    return m.group(1).strip() if m else None


def block_text(content):
    """The text of a message content (string, or a list of text / tool_result blocks)."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get('type') == 'text':
                out.append(b.get('text') or '')
            elif b.get('type') == 'tool_result':
                out.append(block_text(b.get('content')))
    return '\n'.join(out)


# ---------------------------------------------------------------------------------------------------------------------
# The records of one transcript (kept in memory while the tool runs; the classifier turns them into axis values and drops them)
# ---------------------------------------------------------------------------------------------------------------------
class Call:
    """A Bash tool call."""
    __slots__ = ('id', 'ts', 'cmd', 'desc', 'bg', 'cwd', 'end', 'result', 'bg_id', 'notif', 'notif_status', 'notif_text', 'is_error', 'node')

    def __init__(self, cid, ts, cmd, desc, bg, cwd):
        self.id, self.ts, self.cmd, self.desc, self.bg, self.cwd = cid, ts, cmd, desc, bg, cwd
        self.end = self.result = self.bg_id = self.notif = self.notif_status = self.notif_text = None
        self.is_error = False
        self.node = None

    def over(self):
        """When the call stopped running: a background call at its notification, a foreground one at its result."""
        if self.bg_id:
            return self.notif
        return self.end

    def running_at(self, t, slack=10.0):
        over = self.over()
        return self.ts <= t + 1.0 and (over is None or t <= over + slack)


class Node:
    """One transcript: the main session, a sub-agent, or a `claude -p` child."""

    def __init__(self, kind, sid, path):
        self.kind, self.sid, self.path = kind, sid, path
        self.agent = None                  # agent id of a sub-agent
        self.meta = {}
        self.cwd = None
        self.entry = None
        self.first_ts = self.last_ts = None
        self.mtime = None
        self.prompts = []                  # (ts, source, text): sdk / legacy / human / system / peer / notification instructions, in order
        self.calls = []
        self.writes = []                   # (ts, path, text, ok)
        self.reads = []                    # (ts, path)
        self.taskstops = []                # (ts, target id)
        self.sendmsgs = []                 # (ts, target id)
        self.agent_calls = []              # (ts, tool_use id, description, prompt text)
        self.errors = []                   # (ts, status, kind, resets_at, limit_type)
        self.notices = []                  # (ts, text) of system informational lines
        self.notifs = []                   # (ts, task id, tool_use id, status, summary)
        self.cost_states = []
        self.ends = []                     # (ts, stop_reason) of assistant lines with a plain end
        self.turn_ends = []
        self.type_counts = collections.Counter()
        self.versions = set()              # the Claude Code versions the lines carry
        self.cost_field_gone = False       # a `cost-state` line without one of the fields every line of the known range has
        self.torn = 0                      # lines that are not JSON but are followed by a line that is
        self.results = {}                  # tool_use id -> (ts, text, is_error)
        self.nlines = 0
        self.children = []                 # child nodes launched from here (filled by the linker)
        self.parent = None                 # (node, call) once linked
        self.link_rule = None
        self.live_proc = False

    def ident(self):
        return self.sid if self.kind != 'sub' else self.agent

    def __repr__(self):
        return 'Node(%s)' % self.kind


class Tree:
    def __init__(self, claude_home, root):
        self.claude_home, self.root = claude_home, root
        self.nodes = []                    # every node of the tree, root first
        self.heads = {}                    # session path -> head info of sessions that are not (yet) in the tree
        self.unlinked = 0
        self.skipped = collections.Counter()
        self.others = []                   # (sid, path, first_ts) of the other main sessions of the project(s) (decoys)


def _load_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


RECORD_START_RE = re.compile(rb'\{"(?:parentUuid|type|sessionId|timestamp|uuid)"')


def _recover(raw):
    """A record torn in half with the next record glued behind it on the same line: the whole record at the end of the line, or None."""
    for m in RECORD_START_RE.finditer(raw, 1):
        d = _load_json(raw[m.start():])
        if isinstance(d, dict):
            return d
    return None


def read_node(path, kind, sid=None, agent=None, meta=None):
    """Parses a transcript into a Node. Every line is decoded (the biggest records are 60 MB: about a second); a line over LINE_MAX is skipped."""
    n = Node(kind, sid or os.path.basename(path)[:-len('.jsonl')], path)
    n.agent, n.meta = agent, meta or {}
    try:
        n.mtime = os.stat(path).st_mtime
    except OSError:
        return n
    pending_torn = 0
    calls = {}
    with open_record(path) as fh:
        for raw in fh:
            if len(raw) > LINE_MAX:
                continue
            raw = raw.strip()
            if not raw:
                continue
            d = _load_json(raw)
            if not isinstance(d, dict):
                d = _recover(raw)
                if d is not None:
                    n.torn += 1
                else:
                    pending_torn += 1
                    continue
            if pending_torn:
                n.torn += pending_torn
                pending_torn = 0
            n.nlines += 1
            _feed(n, d, calls)
    for c in n.calls:
        n.results.pop(c.id, None)
    return n


def _feed(n, d, calls):
    typ = d.get('type')
    n.type_counts[typ] += 1
    ts = parse_ts(d.get('timestamp'))
    if ts:
        n.first_ts = ts if n.first_ts is None else min(n.first_ts, ts)
        n.last_ts = ts if n.last_ts is None else max(n.last_ts, ts)
    if n.entry is None and d.get('entrypoint'):
        n.entry = d['entrypoint']
    if n.cwd is None and isinstance(d.get('cwd'), str):
        n.cwd = d['cwd']
    if isinstance(d.get('version'), str):
        n.versions.add(d['version'])
    if typ == 'cost-state':
        n.cost_states.append(ts or (n.last_ts or 0))
        if 'totalDuration' not in d:
            n.cost_field_gone = True
    elif typ == 'user':
        _user(n, d, ts, calls)
    elif typ == 'assistant':
        _assistant(n, d, ts, calls)
    elif typ == 'system':
        if d.get('subtype') == 'informational' and isinstance(d.get('content'), str):
            n.notices.append((ts, d['content'][:300]))
        elif d.get('subtype') == 'turn_duration':
            n.turn_ends.append(ts)
    elif typ == 'attachment':
        att = d.get('attachment') or {}
        if att.get('type') == 'queued_command' and isinstance(att.get('prompt'), str):
            origin = att.get('origin') if isinstance(att.get('origin'), dict) else {}
            if origin.get('kind') == 'peer':
                n.prompts.append((parse_ts(att.get('timestamp')) or ts, 'peer', att['prompt'][:2000]))
            else:
                _notification(n, att['prompt'], parse_ts(att.get('timestamp')) or ts, calls)


def _user(n, d, ts, calls):
    msg = d.get('message') or {}
    content = msg.get('content')
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get('type') == 'tool_result':
                tid = b.get('tool_use_id')
                text = block_text(b.get('content'))
                c = calls.get(tid)
                tur = d.get('toolUseResult') if isinstance(d.get('toolUseResult'), dict) else {}
                if c is not None:
                    c.end = ts
                    c.result = text[:2048]
                    c.is_error = bool(b.get('is_error'))
                    if tur.get('backgroundTaskId'):
                        c.bg_id = tur['backgroundTaskId']
                n.results[tid] = (ts, text[:2048], bool(b.get('is_error')))
                for w in n.writes:
                    if w[3] is None and w[4] == tid:
                        pass
        return
    if not isinstance(content, str):
        return
    origin = d.get('origin') if isinstance(d.get('origin'), dict) else {}
    src, okind = d.get('promptSource'), origin.get('kind')
    if '<task-notification>' in content[:200] or okind == 'task-notification':
        _notification(n, content, ts, calls)
        return
    if content.startswith('<cross-session-message') or okind == 'peer' or src == 'peer':
        n.prompts.append((ts, 'peer', content[:2000]))
        return
    if okind == 'auto-continuation' or src == 'system':
        n.prompts.append((ts, 'system', content[:300]))
        return
    if src == 'sdk':
        n.prompts.append((ts, 'sdk', content))
    elif src in ('typed', 'queued', 'suggestion_accepted') or okind == 'human':
        n.prompts.append((ts, 'human', content[:2000]))
    elif not content.startswith('<'):
        n.prompts.append((ts, 'legacy', content))


def _notification(n, body, ts, calls):
    for m in NOTIF_RE.finditer(body):
        b = m.group(0)
        task, tuid, status, summary = tag(b, 'task-id'), tag(b, 'tool-use-id'), tag(b, 'status'), tag(b, 'summary') or ''
        n.notifs.append((ts, task, tuid, status, summary[:400]))
        c = calls.get(tuid)
        if c is not None:
            c.notif, c.notif_status, c.notif_text = ts, status, summary[:400]


def _assistant(n, d, ts, calls):
    msg = d.get('message') or {}
    if d.get('isApiErrorMessage') or d.get('apiErrorStatus'):
        q = d.get('quotaLimits') if isinstance(d.get('quotaLimits'), dict) else {}
        n.errors.append((ts, d.get('apiErrorStatus'), d.get('error'), q.get('resetsAt'), q.get('rateLimitType')))
        return
    if msg.get('stop_reason') in ('end_turn', 'stop_sequence'):
        n.ends.append((ts, msg['stop_reason']))
    for b in msg.get('content') or []:
        if not (isinstance(b, dict) and b.get('type') == 'tool_use'):
            continue
        name, inp, tid = b.get('name'), b.get('input') or {}, b.get('id')
        if name == 'Bash':
            c = Call(tid, ts or 0.0, str(inp.get('command') or '')[:TEXT_CAP], str(inp.get('description') or '')[:200], bool(inp.get('run_in_background')),
                     d.get('cwd'))
            c.node = n
            calls[tid] = c
            n.calls.append(c)
        elif name == 'Write':
            n.writes.append((ts, str(inp.get('file_path') or ''), str(inp.get('content') or '')[:TEXT_CAP], None, tid))
        elif name == 'Edit':
            n.writes.append((ts, str(inp.get('file_path') or ''), str(inp.get('new_string') or '')[:TEXT_CAP], None, tid))
        elif name == 'Read':
            n.reads.append((ts, str(inp.get('file_path') or '')))
        elif name == 'TaskStop':
            n.taskstops.append((ts, inp.get('task_id') or inp.get('shell_id')))
        elif name == 'SendMessage':
            n.sendmsgs.append((ts, inp.get('to')))
        elif name in ('Agent', 'Task'):
            n.agent_calls.append((ts, tid, str(inp.get('description') or '')[:200], str(inp.get('prompt') or '')))


def first_instruction(n):
    """(ts, text) of the instruction a child run starts with (the first sdk line, else the first plain user line), or None."""
    for ts, src, text in n.prompts:
        if src in ('sdk', 'legacy'):
            return ts, text
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------------------------------------------------
def projects_of(claude_home):
    return os.path.join(claude_home, 'projects')


def find_session(claude_home, prefix):
    """The one session file whose id starts with `prefix` (None when there is none, ValueError when several)."""
    hits = sorted(glob.glob(os.path.join(projects_of(claude_home), '*', prefix + '*.jsonl')))
    hits = [h for h in hits if os.sep + 'subagents' + os.sep not in h]
    if len(hits) > 1:
        raise ValueError('more than one session starts with that prefix')
    return hits[0] if hits else None


def read_subagents(main, claude_home, alive_at=None):
    """The sub-agent nodes of a main session (meta + transcript)."""
    out = []
    d = os.path.join(main.path[:-len('.jsonl')], 'subagents')
    for mp in sorted(glob.glob(os.path.join(d, 'agent-*.meta.json'))):
        aid = os.path.basename(mp)[len('agent-'):-len('.meta.json')]
        try:
            with open_record(mp) as f:
                meta = json.loads(f.read(1 << 16))
        except (OSError, ValueError):
            meta = {}
        p = os.path.join(d, 'agent-%s.jsonl' % aid)
        if not os.path.exists(p) or (alive_at is not None and os.stat(p).st_mtime < alive_at - 60):
            continue
        s = read_node(p, 'sub', sid=main.sid, agent=aid, meta=meta if isinstance(meta, dict) else {})
        s.parent = (main, None)
        out.append(s)
    return out


def session_heads(claude_home, max_lines=4000, max_bytes=16 << 20):
    """{path: Node} of every session file in every project, holding only the head: entry point, cwd, first time, and the first instruction or typed prompt.
    Reading stops at the first prompt, so a 60 MB record costs a few lines."""
    out = {}
    for p in glob.glob(os.path.join(projects_of(claude_home), '*', '*.jsonl')):
        n = Node('head', os.path.basename(p)[:-len('.jsonl')], p)
        try:
            n.mtime = os.stat(p).st_mtime
            with open_record(p) as fh:
                read = 0
                for i, raw in enumerate(fh):
                    read += len(raw)
                    if i >= max_lines or read > max_bytes:
                        break
                    if len(raw) > LINE_MAX:
                        continue
                    d = _load_json(raw)
                    if isinstance(d, dict):
                        _feed(n, d, {})
                    if n.prompts and n.entry:
                        break
        except OSError:
            continue
        out[p] = n
    return out


def read_tree(claude_home, sid):
    """Reads the tree the session `sid` belongs to. For a `claude -p` child the tree is its launcher's (found from the other sessions' records); the root is read
    with its sub-agents, then every `claude -p` child any node of the tree launched is linked and read (`link_children`). `Tree.subject` is the session asked for."""
    path = find_session(claude_home, sid)
    if not path:
        raise LookupError('no such session')
    tree = Tree(claude_home, None)
    tree.heads = session_heads(claude_home)
    first = read_node(path, 'main')
    root = first
    if first.entry == 'sdk-cli':
        up = find_launcher(tree, first)
        if up is not None:
            root = up
    root.kind = 'child' if root.entry == 'sdk-cli' else 'main'
    tree.root = root
    tree.nodes = [root] + read_subagents(root, claude_home)
    link_children(tree)
    tree.subject = next((n for n in tree.nodes if n.kind != 'sub' and n.sid == first.sid), root)
    tree.others = [h for h in tree.heads.values() if h.entry != 'sdk-cli' and h.sid != root.sid]
    return tree


def node_index(n):
    """(times, offsets, text) of everything a node launched things with (Bash commands, Write contents, Edit new text), normalised once and joined, so that a window
    of it is a pair of offsets and an anchor is looked up with find(a, start, end), never a copy."""
    idx = getattr(n, '_idx', None)
    if idx is None:
        items = sorted([(c.ts, c.cmd) for c in n.calls] + [(w[0], w[2]) for w in n.writes if w[0]], key=lambda x: x[0])
        tss, offs, parts, pos = [], [], [], 0
        for ts, text in items:
            t = norm(text)
            tss.append(ts)
            offs.append(pos)
            parts.append(t)
            pos += len(t) + 1
        idx = n._idx = (tss, offs, '\n'.join(parts))
    return idx


def window_coverage(n, t0, anchors):
    """The fraction of the anchors found in what node n launched with in [t0 - 2 h, t0 + 2 s]."""
    tss, offs, text = node_index(n)
    lo, hi = bisect.bisect_left(tss, t0 - WINDOW_BEFORE), bisect.bisect_right(tss, t0 + WINDOW_AFTER)
    if lo >= hi or not anchors:
        return 0.0
    start, end = offs[lo], (offs[hi] if hi < len(offs) else len(text))
    return sum(1 for a in anchors if text.find(a, start, end) >= 0) / float(len(anchors))


def ranked_by_content(nodes, t0, anchors):
    """[(coverage, node)] best first for the nodes whose launching material covers the anchors of an instruction that started at t0."""
    out = []
    for nd in nodes:
        if nd.first_ts is not None and nd.last_ts is not None and not (nd.first_ts - 5 <= t0 <= nd.last_ts + WINDOW_BEFORE):
            continue
        cov = window_coverage(nd, t0, anchors)
        if cov:
            out.append((cov, nd))
    out.sort(key=lambda x: -x[0])
    return out


def decided(ranked):
    """The best node when it covers at least 0.4 and at least twice the second one."""
    if ranked and ranked[0][0] >= COVER_MIN and (len(ranked) == 1 or ranked[0][0] >= COVER_RATIO * ranked[1][0]):
        return ranked[0][1]
    return None


LAUNCH_RE = re.compile(r'claude\b[^;&|\n]*\s(?:-p|--print)\b')       # a `claude -p` command (a heredoc body must be stripped first)
LAUNCHY_RE = re.compile(r'claude|\.sh\b|\.py\b|\./|\bbash\b|\bsh\b|python')
HEREDOC_WRITE_RE = re.compile(r"(?:cat\s*>>?\s*|tee\s+(?:-a\s+)?)(\S+)\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\2(?:\n|$)", re.S)


def node_files(node):
    """{path: [(time, text), ...]} of the files a node's own records wrote (a heredoc in a shell call, the Write tool), in time order. Built once per node."""
    f = getattr(node, '_files', None)
    if f is None:
        f = node._files = collections.defaultdict(list)
        for c in node.calls:
            if '<<' in c.cmd:
                for m in HEREDOC_WRITE_RE.finditer(c.cmd):
                    f[unquote(m.group(1))].append((c.ts, m.group(3)))
        for w in node.writes:
            if w[0]:
                f[w[1]].append((w[0], w[2]))
        for v in f.values():
            v.sort(key=lambda x: x[0])
    return f


def record_files(node, before):
    """{path: text} of the files a node's own records had written before `before`: what a replay can rebuild."""
    out = {}
    for path, versions in node_files(node).items():
        for ts, text in versions:
            if ts < before:
                out[path] = text
    return out


def call_material(node, call):
    """The normalised text that makes up what a call launches: its command plus the files and scripts its words name that the node's records wrote earlier."""
    files = record_files(node, call.ts)
    parts = [call.cmd]
    for path in re.findall(r'[~/.\w$-][^\s"\'`;&|<>()]*', call.cmd):
        if path in files:
            parts.append(files[path])
    return norm('\n'.join(parts))


def launch_call_at(node, t0, text_n=None, anchors=None):
    """The Bash call of `node` that launched a child that started at t0: among the calls running then that could launch something, the one whose material (command
    and the files it names) covers the child's instruction best; with no text to compare, the latest to start."""
    cands = [c for c in node.calls if c.running_at(t0) and LAUNCHY_RE.search(c.cmd)]
    if not cands:
        return None
    if text_n or anchors:
        scored = []
        for c in cands:
            mat = call_material(node, c)
            cov = 1.0 if (text_n and text_n in mat) else coverage(anchors or [], mat)
            scored.append((cov, c.ts, c))
        scored.sort(key=lambda x: (x[0], x[1]))
        if scored[-1][0] > 0:
            return scored[-1][2]
    return max(cands, key=lambda c: c.ts)


def find_launcher(tree, child):
    """For an sdk-cli session: the full main Node of the session that launched it (its record read), or None. The child's first instruction is looked for in the
    launching material of the other sessions that were alive at that time, and the child's id in their call results."""
    fi = first_instruction(child)
    if not fi:
        return None
    t0, text = fi
    n_text = norm(text)
    anchors = anchors_of(n_text)
    best = None
    for p, h in tree.heads.items():
        if h.sid == child.sid or h.first_ts is None or h.first_ts > t0 or (h.mtime or 0) < t0 - 5:
            continue
        cand = read_node(p, 'main')
        nodes = [cand] + read_subagents(cand, tree.claude_home, alive_at=t0)
        if _link_by_id(nodes, child.sid, t0, {'done': set(), 'ids': {}}):
            return cand
        if anchors:
            ranked = ranked_by_content(nodes, t0, anchors)
            node = decided(ranked)
            if node is not None and launch_call_at(node, t0, anchors=anchors):
                if best is None or ranked[0][0] > best[0]:
                    best = (ranked[0][0], cand)
        elif any(n_text in norm(c.cmd) for nd in nodes for c in nd.calls if c.running_at(t0)):
            best = best or (1.0, cand)
    return best[1] if best else None


def id_index(nodes, cache):
    """{session id: (node, call)} of the ids a launching call shows: in its result, or in the file its output is redirected to. Built once per node."""
    for nd in nodes:
        if id(nd) in cache['done']:
            continue
        cache['done'].add(id(nd))
        for c in nd.calls:
            if c.result:
                for u in UUID_RE.findall(c.result):
                    cache['ids'].setdefault(u, (nd, c))
            if LAUNCH_RE.search(strip_heredocs(c.cmd)[0]):
                for p in _redirect_paths(c.cmd):
                    for u in _file_uuids(p, c.cwd):
                        cache['ids'].setdefault(u, (nd, c))
    return cache['ids']


def _link_by_id(nodes, child_sid, t0, cache):
    """(node, call) whose result (or whose redirect file) holds the child's session id, when that call started before the child: the firmest evidence a record gives."""
    hit = id_index(nodes, cache).get(child_sid)
    return hit if hit and hit[1].ts <= t0 + 1 else None


def _file_uuids(path, cwd=None):
    p = os.path.join(cwd, path) if cwd and not os.path.isabs(path) else path
    data = read_aux(p, TEXT_CAP * 4)
    return UUID_RE.findall(data.decode('utf-8', 'replace')) if data is not None else []


def link_children(tree):
    """Links every `claude -p` session to the node of the tree that launched it, until nothing new links (a child may launch grandchildren): by the child's id in
    a call result or output file, else by its first instruction in the launching material, else, for a short instruction, by the one launching
    call that held it while it started, else by the one `claude -p` call that was running (a guess)."""
    pool = {h.sid: h for h in tree.heads.values() if h.entry == 'sdk-cli' and h.sid != tree.root.sid and first_instruction(h)}
    pool = {s: h for s, h in pool.items() if not any(n.sid == s for n in tree.nodes if n.kind != 'sub')}
    nodes = list(tree.nodes)
    cache = {'done': set(), 'ids': {}}
    changed = True
    while changed and pool:
        changed = False
        for sid, head in sorted(pool.items(), key=lambda kv: first_instruction(kv[1])[0]):
            t0, text = first_instruction(head)
            n_text = norm(text)
            anchors = anchors_of(n_text)
            hit, rule = _link_by_id(nodes, sid, t0, cache), 'out'
            if hit is None and anchors:
                node = decided(ranked_by_content(nodes, t0, anchors))
                call = launch_call_at(node, t0, anchors=anchors) if node is not None else None
                hit, rule = ((node, call), 'content') if call else (None, None)
            elif hit is None:
                cands = [(nd, c) for nd in nodes for c in nd.calls if c.running_at(t0, 0.0) and n_text and n_text in norm(c.cmd)]
                hit, rule = (cands[0], 'content_short') if len(cands) == 1 else (None, None)
            if hit is None:
                cands = [(nd, c) for nd in nodes for c in nd.calls if c.running_at(t0, 0.0) and
                         (LAUNCH_RE.search(strip_heredocs(c.cmd)[0]) or (LAUNCHY_RE.search(c.cmd) and 'claude' in (read_disk_script(c) or '')))
                         and (not head.cwd or not c.cwd or head.cwd == c.cwd or head.cwd in c.cmd or '$' in c.cmd or read_disk_script(c))]
                hit, rule = (cands[0], 'time') if len(cands) == 1 else (None, None)
            if hit is None:
                continue
            node, call = hit
            full = read_node(head.path, 'child', sid=sid)
            full.parent, full.link_rule = (node, call), rule
            node.children.append(full)
            tree.nodes.append(full)
            nodes.append(full)
            del pool[sid]
            changed = True
    tree.unlinked = len(pool)


def _file_has(path, needle, cwd=None):
    p = os.path.join(cwd, path) if cwd and not os.path.isabs(path) else path
    data = read_aux(p, TEXT_CAP * 4)
    return data is not None and needle.encode() in data


REDIR_RE = re.compile(r'(?<![<\d&])(?:\d?>>?|&>)\s*("([^"]+)"|\'([^\']+)\'|([^\s;&|)<>]+))')


def _redirect_paths(cmd):
    out = []
    for m in REDIR_RE.finditer(cmd):
        p = m.group(2) or m.group(3) or m.group(4)
        if p and not p.startswith('&') and p != '/dev/null' and '$' not in p:
            out.append(p)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Reading a launching command (pure)
# ---------------------------------------------------------------------------------------------------------------------
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_]\w*))")


def strip_heredocs(cmd):
    """The command without the bodies of its heredocs (the `<<'EOF'` markers stay), and how many heredocs it had."""
    lines, out, i, count = cmd.split('\n'), [], 0, 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC_RE.finditer(line):
            count += 1
            marker = m.group(2) or m.group(3) or m.group(4)
            while i < len(lines) and lines[i].strip() != marker:
                i += 1
            i += 1
    return '\n'.join(out), count


def _scan_paren(code, i):
    """The index just after the parenthesis group that opens at code[i] == '('."""
    depth, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == '\\':
            i += 2
            continue
        if c in '\'"':
            j = code.find(c, i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _scan_word(code, i):
    n = len(code)
    while i < n:
        c = code[i]
        if c == '\\':
            i += 2
        elif c == "'":
            j = code.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"':
            i += 1
            while i < n and code[i] != '"':
                if code[i] == '\\':
                    i += 2
                elif code[i] == '$' and code[i + 1:i + 2] == '(':
                    i = _scan_paren(code, i + 1)
                else:
                    i += 1
            i += 1
        elif c == '$' and code[i + 1:i + 2] == '(':
            i = _scan_paren(code, i + 1)
        elif c == '`':
            j = code.find('`', i + 1)
            i = n if j < 0 else j + 1
        elif c in ' \t\r\n;&|()<>':
            break
        else:
            i += 1
    return min(i, n)


REDIR_OP_RE = re.compile(r'<<-?|>>|>&|<&|>\||<|>')


def lex(code):
    """Tokens (kind, text): word, op (; && || | & newline ( )) and redir (a redirect operator, its fd prefix included). Quotes stay inside the word text."""
    toks, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if c in ' \t\r':
            i += 1
        elif c == '\n':
            toks.append(('op', '\n'))
            i += 1
        elif c == '#' and (i == 0 or code[i - 1] in ' \t\n;|&('):
            j = code.find('\n', i)
            i = n if j < 0 else j
        elif code[i:i + 3] == '<<<':
            toks.append(('redir', '<<<'))
            i += 3
        elif code[i:i + 2] in ('&&', '||', ';;'):
            toks.append(('op', code[i:i + 2]))
            i += 2
        elif code[i:i + 2] in ('<(', '>('):
            j = _scan_paren(code, i + 1)
            toks.append(('word', code[i:j]))
            i = j
        elif c in ';|':
            toks.append(('op', c))
            i += 1
        elif c == '&':
            if code[i + 1:i + 2] == '>':
                j = i + 2 + (code[i + 2:i + 3] == '>')
                toks.append(('redir', code[i:j]))
                i = j
            else:
                toks.append(('op', '&'))
                i += 1
        elif c in '()':
            toks.append(('op', c))
            i += 1
        elif c in '<>':
            op = REDIR_OP_RE.match(code, i).group(0)
            toks.append(('redir', op))
            i += len(op)
        else:
            j = _scan_word(code, i)
            w = code[i:j]
            if w.isdigit() and j < n and code[j] in '<>':
                op = REDIR_OP_RE.match(code, j).group(0)
                toks.append(('redir', w + op))
                i = j + len(op)
            else:
                toks.append(('word', w))
                i = max(j, i + 1)
    return toks


def unquote(w):
    """A word without its quotes (a double-quoted `$(...)` stays as written)."""
    out, i, n = [], 0, len(w)
    while i < n:
        c = w[i]
        if c == "'":
            j = w.find("'", i + 1)
            j = n if j < 0 else j
            out.append(w[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and w[i] != '"':
                if w[i] == '\\' and i + 1 < n:
                    i += 1
                out.append(w[i])
                i += 1
            i += 1
        elif c == '\\' and i + 1 < n:
            out.append(w[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


class Cmd:
    """One simple command of a script: assignments, prefix words, the head word, its arguments and redirects."""

    def __init__(self):
        self.assigns, self.prefixes, self.head, self.args, self.redirs = {}, [], None, [], []
        self.bg = False
        self.pipe_in = self.pipe_out = False
        self.pipe_prefixes = set()       # the prefixes (timeout, env -i ...) of the earlier stages of the pipeline this command is in
        self.loop = 0
        self.sleep_before = False
        self.raw = ''


ASSIGN_RE = re.compile(r'^[A-Za-z_]\w*=')
KEYWORDS_PRE = ('do', 'then', 'else', 'elif', 'if', 'while', 'until', '!', '{', 'time')
PREFIX_SIMPLE = ('nohup', 'setsid', 'exec', 'command', 'time', 'stdbuf', 'nice', 'sudo', 'caffeinate')


def commands(code):
    """The simple commands of `code` in order, with loop depth, pipe neighbours, a trailing `&`, and whether a `sleep N` ran before in the same list."""
    toks = lex(code)
    out, cur, loops = [], [], 0
    sleeping = False
    prev_sep = None

    def flush(sep):
        nonlocal cur, sleeping, prev_sep
        if cur:
            c = _simple(cur, loops)
            c.pipe_in = prev_sep == '|'
            if c.pipe_in and out:
                c.pipe_prefixes = set(out[-1].prefixes) | set(out[-1].pipe_prefixes)
            c.pipe_out = sep == '|'
            c.bg = sep == '&'
            c.sleep_before = sleeping
            if c.head == 'sleep' and c.args and re.fullmatch(r'\d+(?:\.\d+)?[smh]?', unquote(c.args[0])):
                sleeping = sleeping or _seconds(unquote(c.args[0])) >= 30
            out.append(c)
        cur = []
        prev_sep = sep
    for kind, text in toks:
        if kind == 'op':
            if text in ('(', ')'):
                if text == '(':
                    flush(text)
                else:
                    flush(text)
                continue
            if cur and cur[0] == ('word', 'for') or cur and cur[0] == ('word', 'while') or cur and cur[0] == ('word', 'until'):
                loops += 1
                cur = []
                prev_sep = text
                continue
            if cur and cur[0] == ('word', 'done'):
                loops = max(0, loops - 1)
                cur = []
                prev_sep = text
                continue
            flush(text)
        else:
            cur.append((kind, text))
    flush(None)
    return out


def _seconds(s):
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([smh]?)', s)
    return float(m.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[m.group(2)] if m else 0.0


def _simple(toks, loops):
    c = Cmd()
    c.loop = loops
    words = []
    i = 0
    while i < len(toks):
        kind, text = toks[i]
        if kind == 'redir':
            path = toks[i + 1][1] if i + 1 < len(toks) and toks[i + 1][0] == 'word' else None
            c.redirs.append((text, path))
            i += 2
            continue
        words.append(text)
        i += 1
    c.raw = ' '.join(words)
    k = 0
    while k < len(words):
        w = words[k]
        if ASSIGN_RE.match(w):
            name, _, val = w.partition('=')
            c.assigns[name] = val
            k += 1
        elif w in KEYWORDS_PRE and k + 1 < len(words):
            k += 1
        elif w in PREFIX_SIMPLE:
            c.prefixes.append(w)
            k += 1
        elif w == 'timeout':
            c.prefixes.append('timeout')
            k += 1
            while k < len(words) and (words[k].startswith('-') or re.fullmatch(r'\d+(?:\.\d+)?[smhd]?', words[k])):
                k += 2 if words[k] in ('-k', '-s', '--signal', '--kill-after') else 1
        elif w == 'env':
            k += 1
            c.prefixes.append('env')
            while k < len(words) and (words[k].startswith('-') or ASSIGN_RE.match(words[k])):
                if words[k] in ('-i', '--ignore-environment'):
                    c.prefixes.append('env-i')
                elif ASSIGN_RE.match(words[k]):
                    n, _, v = words[k].partition('=')
                    c.assigns[n] = v
                k += 1
        else:
            break
    if k < len(words):
        c.head = os.path.basename(unquote(words[k]))
        c.head_raw = unquote(words[k])
        c.args = words[k + 1:]
    return c


VALUE_FLAGS = frozenset(('--model', '--effort', '--output-format', '--input-format', '--resume', '-r', '--session-id', '--fork-session-id', '--allowedTools',
                         '--allowed-tools', '--disallowedTools', '--disallowed-tools', '--max-turns', '--append-system-prompt', '--system-prompt',
                         '--permission-mode', '--add-dir', '--mcp-config', '--settings', '--agents', '--plugin-dir', '-d', '--debug-file', '--name', '-n'))
FORM_FLAGS = {'--resume': 'resume', '-r': 'resume', '--session-id': 'session_id', '--fork-session': 'fork', '--no-session-persistence': 'nopersist'}


class Inv:
    """One `claude -p` invocation found in a command (or in the script it runs)."""

    def __init__(self):
        self.prefixes = set()
        self.via = None
        self.files = []
        self.prompt_raw = None
        self.posarg = False
        self.form = 'new'
        self.resume_id = None
        self.redirs = []
        self.json = False
        self.loop = False
        self.container = None             # 'xargs' | 'tmux' | 'pysub' | None
        self.script = None                # path of the script that holds it
        self.bg = False
        self.after_sleep = False
        self.assigns = {}
        self.cd = None
        self.pipe_files = []


def _cat_files(text):
    """The files named by `$(cat a b)` / `$(< a)` substitutions and `<(cat a b)` inside a word."""
    out = []
    for m in re.finditer(r'(?:\$\(|<\()\s*(?:cat\s+([^)]*)|<\s*([^)\s]+))\)', text):
        for p in (m.group(1) or m.group(2) or '').split():
            out.append(unquote(p))
    return out


def invocation_of(c, in_script=None):
    """Inv for a command whose head is `claude` with -p / --print, else None."""
    if c.head != 'claude':
        return None
    args = c.args
    words = [unquote(a) for a in args]
    if not any(w in ('-p', '--print') for w in words):
        return None
    inv = Inv()
    inv.prefixes = set(c.prefixes) | set(c.pipe_prefixes)
    inv.bg, inv.loop, inv.assigns, inv.script = c.bg, bool(c.loop), dict(c.assigns), in_script
    inv.after_sleep = c.sleep_before
    k = 0
    prompt = None
    while k < len(args):
        a, w = args[k], words[k]
        if w in FORM_FLAGS:
            inv.form = FORM_FLAGS[w]
            if w in ('--resume', '-r', '--session-id') and k + 1 < len(args):
                inv.resume_id = words[k + 1]
                k += 1
        elif w.startswith('--resume='):
            inv.form, inv.resume_id = 'resume', w.split('=', 1)[1]
        elif w == '--output-format' and k + 1 < len(args):
            inv.json = 'json' in words[k + 1]
            k += 1
        elif w.startswith('--output-format='):
            inv.json = 'json' in w
        elif w in VALUE_FLAGS and k + 1 < len(args):
            k += 1
        elif w.startswith('-') and a.startswith('-'):
            pass
        elif prompt is None:
            prompt = a
        k += 1
    if inv.form == 'resume' and '--fork-session' in words:
        inv.form = 'fork'
    inv.redirs = list(c.redirs)
    heredoc = any(op in ('<<', '<<-') for op, _ in c.redirs)
    stdin = [p for op, p in c.redirs if op == '<' and p]
    here_string = any(op == '<<<' for op, _ in c.redirs)
    if heredoc or here_string:
        inv.via = 'heredoc'
    elif prompt is not None:
        inv.prompt_raw = prompt
        files = _cat_files(prompt)
        pv = re.fullmatch(r'"?\$\{?(\w+)\}?"?', prompt)
        if files:
            inv.via, inv.files = 'subst', files
        elif pv:
            name = pv.group(1)
            if name.isdigit():
                inv.posarg = True
                inv.via = 'arg'
            elif inv.loop:
                inv.via = 'arg'
            elif name in c.assigns:
                inv.via, inv.files = 'subst', _cat_files(c.assigns[name])
            else:
                inv.via = 'var'
        else:
            inv.via = 'arg'
    elif stdin:
        p = stdin[0]
        inv.via, inv.files = 'stdin', (_cat_files(p) if p.startswith('<(') else [unquote(p)])
    elif c.pipe_in:
        inv.via = 'pipe'
    else:
        inv.via = 'arg'
    return inv


def analyze_command(cmd, read_script=None, depth=0, script=None):
    """Every `claude -p` invocation a command starts (also inside the scripts it runs, via `read_script(path) -> text or None`), and the structural features around
    them: {'invs': [Inv], 'features': set of (name,), 'cd': [...]}. Pure apart from `read_script`."""
    code, _ = strip_heredocs(cmd)
    cmds = commands(code)
    invs, scripts = [], []
    cds, scope, prev = [], {}, None
    for c in cmds:
        if c.head is None:
            scope.update(c.assigns)
        if c.head == 'cd' and c.args:
            cds.append(unquote(c.args[0]))
        inv = invocation_of(c, in_script=script)
        before, prev = prev, c
        if inv is not None:
            inv.cd = cds[-1] if cds else None
            inv.bg = inv.bg or c.bg
            inv.assigns = dict(scope, **c.assigns)
            if inv.via == 'pipe' and before is not None and before.head == 'cat':
                inv.files = [unquote(a) for a in before.args if not a.startswith('-')]
            invs.append(inv)
            continue
        if c.head == 'xargs':
            words = [unquote(a) for a in c.args]
            if 'claude' in words:
                sub = Cmd()
                sub.head, sub.args, sub.prefixes, sub.loop = 'claude', [a for a in c.args[words.index('claude') + 1:]], list(c.prefixes), c.loop
                sub.redirs = list(c.redirs)
                sub.sleep_before = c.sleep_before
                inv = invocation_of(sub, in_script=script)
                if inv is not None:
                    inv.container, inv.cd = 'xargs', cds[-1] if cds else None
                    inv.via = 'arg'
                    inv.assigns = dict(scope, **c.assigns)
                    invs.append(inv)
            continue
        if c.head == 'tmux' and 'claude' in c.raw:
            m = re.search(r"claude\b[^'\"]*?(?:\s(?:-p|--print)\b)", c.raw)
            if m:
                inner = re.search(r"'(.*)'|\"(.*)\"", c.raw, re.S)
                text = (inner.group(1) or inner.group(2)) if inner else c.raw
                for sub_inv in analyze_command(text, read_script, depth + 1, script)['invs']:
                    sub_inv.container, sub_inv.cd = 'tmux', cds[-1] if cds else None
                    sub_inv.assigns = dict(scope, **sub_inv.assigns)
                    invs.append(sub_inv)
            continue
        if c.head in ('python', 'python3') and depth < 2:
            words = [unquote(a) for a in c.args]
            body = None
            if '-c' in words and words.index('-c') + 1 < len(words):
                body = words[words.index('-c') + 1]
                path = None
            else:
                path = next((w for w in words if w.endswith('.py')), None)
                body = read_script(path) if path and read_script else None
            if body and 'claude' in body and re.search(r'subprocess|os\.system|popen', body):
                inv = Inv()
                inv.container, inv.script, inv.via, inv.cd = 'pysub', path, 'arg', cds[-1] if cds else None
                lits = re.findall(r"['\"]([^'\"]*)['\"]", body)
                inv.form = 'fork' if '--fork-session' in lits else next((FORM_FLAGS[w] for w in lits if w in FORM_FLAGS), 'new')
                for flag in ('--resume', '--session-id'):
                    if flag in lits and lits.index(flag) + 1 < len(lits):
                        inv.resume_id = lits[lits.index(flag) + 1]
                inv.prompt_raw = ''
                inv.prefixes = set(c.prefixes)
                inv.bg = c.bg
                inv.assigns = dict(scope, **c.assigns)
                invs.append(inv)
            continue
        if depth < 2 and (c.head in ('bash', 'sh', 'zsh', 'source', '.') or (c.head and '/' in getattr(c, 'head_raw', '') and not c.head_raw.startswith('-'))):
            words = [unquote(a) for a in c.args]
            if c.head in ('bash', 'sh', 'zsh') and '-c' in words and words.index('-c') + 1 < len(words):
                for sub_inv in analyze_command(words[words.index('-c') + 1], read_script, depth + 1, script)['invs']:
                    invs.append(sub_inv)
                continue
            path = next((w for w in ([c.head_raw] if c.head not in ('bash', 'sh', 'zsh', 'source', '.') else words) if not w.startswith('-')), None)
            body = read_script(path) if path and read_script else None
            if path:
                scripts.append(path)
            if body and 'claude' in body:
                sub = analyze_command(body, read_script, depth + 1, script=path)
                for inv in sub['invs']:
                    inv.script = inv.script or path
                    inv.prefixes |= set(c.prefixes)
                    inv.bg = inv.bg or c.bg
                    inv.script_args = [unquote(a) for a in c.args[1:]] if c.head in ('bash', 'sh', 'zsh', 'source', '.') else [unquote(a) for a in c.args]
                    inv.after_sleep = inv.after_sleep or c.sleep_before
                    if inv.cd is None and cds:
                        inv.cd = cds[-1]
                    invs.append(inv)
    return {'invs': invs, 'scripts': scripts, 'cd': cds[-1] if cds else None}


# ---------------------------------------------------------------------------------------------------------------------
# From records to axis values
# ---------------------------------------------------------------------------------------------------------------------
NEUTRAL_WAYS = ('direct', 'bg')
WAY_ORDER = ('tmux', 'xargs', 'pysub', 'loop', 'script', 'envi', 'detach', 'timeout', 'later')
PROPS = ('env', 'lineage', 'redirect', 'time')


class Result:
    """What `classify` found: cases by id (with how often), what the axes cannot say, and the numbers around it."""

    def __init__(self):
        self.cases = collections.OrderedDict()      # id -> [Case, count]
        self.unexpressible = collections.Counter()  # 'feature(detail)' -> count
        self.unobserved = set()
        self.counts = collections.Counter()

    def add(self, case, why=None):
        n = A.normalize(case)
        if n.key() != case.key():
            changed = sorted('%s=%s' % (a, case.v[a]) for a in case.axes if case.v[a] != n.v[a])
            self.unexpressible['axis_combination(%s) in %s' % (','.join(changed), case.id)] += 1
            return None
        slot = self.cases.setdefault(case.id, [case, 0])
        slot[1] += 1
        return case

    def need(self, feature, detail=''):
        assert feature in FEATURES
        self.unexpressible['%s%s' % (feature, '(%s)' % detail if detail else '')] += 1


def subtree(tree):
    """The nodes the output is about: the subject, what it launched, and its sub-agents when it is a main session."""
    out, seen = [], set()

    def walk(n):
        if id(n) in seen:
            return
        seen.add(id(n))
        out.append(n)
        for c in n.children:
            walk(c)
    s = tree.subject
    walk(s)
    if s.kind in ('main', 'child'):
        for n in tree.nodes:
            if n.kind == 'sub' and n.sid == s.sid:
                walk(n)
    return out


def classify(tree, now=None):
    """The cases (axis values) the tree shows, as a Result. The aff cases are one per launch of a linked child, sta one per agent, deb one per debate participant."""
    now = time.time() if now is None else now
    res = Result()
    nodes = subtree(tree)
    ctx = Ctx(tree, now)
    res.counts['nodes'] = len(nodes)
    for n in nodes:
        res.counts[n.kind] += 1
    for n in nodes:
        if n.link_rule:
            res.counts['linked_by_' + {'content_short': 'content'}.get(n.link_rule, n.link_rule)] += 1
    for n in nodes:
        if n.kind == 'child' and n.parent:
            aff_cases(ctx, res, n)
        sta_case(ctx, res, n)
        deb_cases(ctx, res, n)
    nopersist_cases(ctx, res, nodes)
    return res


class Ctx:
    def __init__(self, tree, now):
        self.tree, self.now = tree, now
        self.all = list(tree.nodes)
        self.cache = {}

    # ---- text of files the launching calls name: records first (what a replay can rebuild), disk second ----
    def script_reader(self, launcher, call):
        recs = record_files(launcher, call.ts)

        def read(path):
            if not path:
                return None
            p = path if os.path.isabs(path) else os.path.join(call.cwd or '', path)
            if p in recs or path in recs:
                return recs.get(p, recs.get(path))
            data = read_aux(p, TEXT_CAP * 4)
            return data.decode('utf-8', 'replace') if data is not None else None
        read.recs = recs
        return read


def writer_kind(path, launcher, before, cwd=None):
    """How a prompt file got written in the launcher's records before `before`: 'write' (the Write tool), 'sed', 'prior' (a shell write) or None."""
    p = path if os.path.isabs(path) or not cwd else os.path.join(cwd, path)
    for w in launcher.writes:
        if w[0] and w[0] < before and w[1] in (path, p):
            return 'write'
    kind = None
    for c in launcher.calls:
        if c.ts >= before:
            continue
        if re.search(r'>>?\s*["\']?%s["\']?(?:\s|$|;)' % re.escape(path), c.cmd) or re.search(r'\btee\s+(?:-a\s+)?["\']?%s' % re.escape(path), c.cmd):
            kind = 'sed' if re.search(r'\bsed\b', c.cmd) and not re.search(r"cat\s*>>?\s*%s\s*<<" % re.escape(path), c.cmd) else (kind or 'prior')
    return kind


def aff_cases(ctx, res, child):
    launcher, call = child.parent
    fi = first_instruction(child)
    t0, text = fi
    n_text = norm(text)
    read = ctx.script_reader(launcher, call)
    shape = analyze_command(call.cmd, read)
    invs = shape['invs']
    if not invs and shape['scripts']:
        hidden = Inv()                       # a script whose body is gone (not in the records, not on disk any more): the launch text is simply not there
        hidden.script, hidden.via, hidden.prompt_raw = shape['scripts'][0], 'arg', ''
        hidden.prefixes = set()
        hidden.cd = shape['cd']
        invs = [hidden]
        res.counts['hidden_scripts'] += 1
    if not invs:
        res.need('launch_shape', 'no_invocation')
        return
    inv = pick_invocation(invs, n_text, call)
    base = aff_axes(ctx, child, launcher, call, inv, read, t0, n_text, res)
    if base is None:
        return
    decoys = decoys_of(ctx, child, launcher, call, n_text, t0)
    for decoy in decoys:
        v = dict(base)
        v['decoy'] = decoy
        res.add(A.Case('aff', v))
    for rl, rc, rinv in resume_calls(ctx, child):
        rread = ctx.script_reader(rl, rc)
        t_run, text_run = next(((ts, tx) for ts, src, tx in child.prompts if src in ('sdk', 'legacy') and ts and ts >= rc.ts - 1), (t0, text))
        n_run = norm(text_run)
        v = aff_axes(ctx, child, rl, rc, rinv, rread, t_run, n_run, res, form='resume')
        if v is not None:
            for decoy in decoys_of(ctx, child, rl, rc, n_run, t_run):
                v2 = dict(v)
                v2['decoy'] = decoy
                res.add(A.Case('aff', v2))


def pick_invocation(invs, n_text, call):
    if len(invs) == 1:
        return invs[0]
    lit = [i for i in invs if i.prompt_raw and n_text and norm(unquote(i.prompt_raw)) == n_text]
    if len(lit) == 1:
        return lit[0]
    loops = [i for i in invs if i.loop]
    if loops and n_text in norm(call.cmd):
        return loops[-1]
    return invs[-1]


def pick_way(inv, call, res):
    found = set()
    if inv.container:
        found.add(inv.container)
    if inv.loop:
        found.add('loop')
    if inv.script and inv.container is None:
        found.add('script')
    if 'env-i' in inv.prefixes:
        found.add('envi')
    if 'setsid' in inv.prefixes or 'nohup' in inv.prefixes:
        found.add('detach')
    if 'timeout' in inv.prefixes:
        found.add('timeout')
    if inv.after_sleep:
        found.add('later')
    if call.bg or inv.bg:
        found.add('bg')
    structural = [w for w in WAY_ORDER if w in found]
    if not structural:
        return 'bg' if 'bg' in found else 'direct'
    eff = {p: all(A.WAY[w][p] for w in structural + (['bg'] if 'bg' in found else [])) for p in PROPS}
    for w in structural:
        if all(A.WAY[w][p] == eff[p] for p in PROPS):
            return w
    res.need('way_combination', '+'.join(structural))
    return None


def aff_axes(ctx, child, launcher, call, inv, read, t0, n_text, res, form=None):
    v = {'target': 'cli', 'os': 'linux', 'bait': 'none'}
    v['spawner'] = {'main': 'main', 'sub': 'sub', 'child': 'grand'}[launcher.kind]
    way = pick_way(inv, call, res)
    if way is None:
        return None
    v['way'] = way
    v['form'] = form or (inv.form if inv.form in ('new', 'fork', 'session_id', 'resume') else 'new')
    if v['form'] == 'resume' and form is None and inv.resume_id != child.sid:
        v['form'] = 'new'
    if inv.via not in ('arg', 'heredoc', 'subst', 'stdin', 'pipe'):
        res.need('prompt_shape', 'variable_prompt')
        return None
    v['via'] = inv.via
    # where the text came from
    if inv.posarg:
        v['src'] = 'posarg'
    elif inv.via in ('arg', 'heredoc'):
        v['src'] = 'absent' if (way in A.FILE_WAYS and inv.script and inv.script not in read.recs) else 'call'
    else:
        if not inv.files:
            res.need('prompt_shape', 'no_prompt_file')
            return None
        kinds = [writer_kind(f, launcher, call.ts, call.cwd) for f in inv.files]
        if len(inv.files) >= 2:
            v['src'] = 'parts' if all(kinds) else 'absent'
        else:
            v['src'] = kinds[0] or 'absent'
    v['cwd'] = cwd_kind(child, call, inv)
    out = out_kind(child, call, inv, ctx)
    if out is None:
        res.need('redirect_shape', 'output')
        return None
    v['out'] = out
    v['timing'] = timing_kind(child, call, inv, way, t0, ctx)
    v['seen'] = 'live' if ctx.now - (child.mtime or 0) < STALE_SECS else 'ended_unseen'
    res.unobserved.add('seen')
    return v


def cwd_kind(child, call, inv):
    """same / cd / other / worktree / symlink, from where the launching call stood, the `cd` before the launch, and where the child records its cwd."""
    cc = child.cwd or ''
    if '/.claude/worktrees/' in cc or '/.worktrees/' in cc:
        return 'worktree'
    cd = inv.cd
    if cd:
        if '$' in cd:
            args = getattr(inv, 'script_args', [])
            m = re.fullmatch(r'"?\$(\d)"?', cd)
            cd = args[int(m.group(1)) - 1] if m and int(m.group(1)) <= len(args) else None
    if not cd:
        return 'same' if cc == (call.cwd or cc) else 'other'
    target = os.path.normpath(os.path.join(call.cwd or '/', os.path.expanduser(unquote(cd))))
    if os.path.islink(target) or (os.path.realpath(target) != target and os.path.realpath(target) == os.path.realpath(cc)):
        return 'symlink'
    base = call.cwd or ''
    if target == os.path.normpath(base) and cc == base:
        return 'same'
    return 'cd' if target.startswith(os.path.normpath(base) + os.sep) else 'other'


def out_kind(child, call, inv, ctx):
    """none / json_file / json_var / json_var_ext / log_only / reused / removed (None for a redirect shape the axis cannot say)."""
    outs = [(op, p) for op, p in inv.redirs if op in ('>', '>>', '1>') and p and p != '/dev/null']
    if not outs:
        return 'none'
    op, raw = outs[0]
    if not inv.json:
        return 'log_only'
    path = unquote(raw)
    if '$' in path:
        m = re.fullmatch(r'\$\{?(\w+)\}?', path)
        if m and m.group(1) in inv.assigns:
            path, kind = unquote(inv.assigns[m.group(1)]), 'json_var'
        else:
            return 'json_var_ext'
    else:
        kind = 'json_file'
    p = path if os.path.isabs(path) else os.path.join(call.cwd or '', path)
    data = read_aux(p, TEXT_CAP, skip_big=False)
    if data is None:
        return kind if os.path.lexists(p) else 'removed'       # a file that is there but must not be read says nothing: it is not a removed one
    if child.sid.encode() in data:
        return kind
    return 'reused' if UUID_RE.search(data.decode('utf-8', 'replace')) else kind


def timing_kind(child, call, inv, way, t0, ctx):
    over = call.end
    immediate = over is not None and over - call.ts < 5.0 and (call.bg or way in ('detach', 'tmux', 'bg'))
    if call.bg and call.bg_id and call.notif is None and child.mtime and ctx.now - child.mtime > STALE_SECS:
        return 'bg_no_notif'
    if immediate and t0 - call.ts > 5.0 and t0 > over:
        return 'call_first'
    if not immediate and way != 'later' and t0 - call.ts > 60.0:
        return 'seq_late'
    return 'normal'


def uuid_calls(ctx):
    """{uuid: [(node, call)]} of the launching-looking calls that name an id, in their command, in a file their words name that the records wrote, or in the script they run. Built once."""
    idx = ctx.cache.get('uuid_calls')
    if idx is None:
        idx = ctx.cache['uuid_calls'] = collections.defaultdict(list)
        for nd in ctx.all:
            files = node_files(nd)
            for c in nd.calls:
                if not LAUNCHY_RE.search(c.cmd):
                    continue
                ids = set(UUID_RE.findall(c.cmd))
                for word in re.findall(r'[~/.\w$-][^\s"\'`;&|<>()]*', c.cmd):
                    if word in files:
                        for _, text in files[word]:
                            ids.update(UUID_RE.findall(text))
                disk = read_disk_script(c) if re.match(r'\s*(?:bash|sh|zsh|source|\.|python3?)?\s*\S+\.(?:sh|py)\b', c.cmd) else None
                if disk:
                    ids.update(UUID_RE.findall(disk))
                for u in ids:
                    idx[u].append((nd, c))
    return idx


def resume_calls(ctx, child):
    """(launcher node, call, Inv) of calls that resumed this child with `--resume <its id>` (also from a script the call runs)."""
    out = []
    for nd, c in uuid_calls(ctx).get(child.sid, ()):
        read = ctx.script_reader(nd, c)
        for inv in analyze_command(c.cmd, read)['invs']:
            if inv.form == 'resume' and inv.resume_id == child.sid:
                out.append((nd, c, inv))
    return out


def other_launches(ctx, path, lo, hi):
    """(ts, cwd, normalised command) of the `claude -p` Bash calls of another session in [lo, hi]. The file is streamed and only the lines that can hold such a call are decoded."""
    key = ('launches', path)
    if key not in ctx.cache:
        rows = []
        try:
            with open_record(path) as fh:
                for raw in fh:
                    if b'"Bash"' in raw and b'claude' in raw and len(raw) < LINE_MAX:
                        d = _load_json(raw)
                        if not isinstance(d, dict) or d.get('type') != 'assistant':
                            continue
                        for b in (d.get('message') or {}).get('content') or []:
                            if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                                cmd = str((b.get('input') or {}).get('command') or '')
                                if re.search(r'claude\b[^;&|\n]*\s(?:-p|--print)\b', cmd):
                                    rows.append((parse_ts(d.get('timestamp')) or 0.0, d.get('cwd'), norm(cmd)))
        except OSError:
            pass
        ctx.cache[key] = rows
    return [r for r in ctx.cache[key] if lo <= r[0] <= hi]


def decoys_of(ctx, child, launcher, call, n_text, t0):
    """The values of the `decoy` axis this child's surroundings show (a list: more than one is more than one case; ['none'] when there is none)."""
    out = []
    if len(n_text) < SHORT:
        out.append('short')
    sibs = [c for c in launcher.children if c is not child]
    mine_a = anchors_of(n_text)
    same = overlap = False
    for s in sibs:
        fi = first_instruction(s)
        if not fi:
            continue
        sn = norm(fi[1])
        if sn == n_text:
            same = True
        elif mine_a and 0 < coverage(mine_a, sn) < COVER_MIN:
            overlap = True
    if same:
        out.append('same_n')
    if overlap:
        out.append('sibling')
    if any(src == 'peer' for _, src, _ in child.prompts):
        out.append('xmsg')
    inf = float('inf')
    if any(c is not call and c.ts < t0 and c.running_at(t0, 0.0) and (inf if c.over() is None else c.over()) - c.ts >= 600 for c in launcher.calls):
        out.append('watcher')
    if launcher.kind == 'main' and any(n.kind == 'sub' and any(c.running_at(t0, 0.0) and (inf if c.over() is None else c.over()) - c.ts >= 60 for c in n.calls)
                                       for n in ctx.tree.nodes):
        out.append('subnoise')
    twin = concurrent = False
    for o in ctx.tree.others:
        if o.first_ts is None or o.first_ts > call.ts or (o.mtime or 0) < call.ts - 60:
            continue
        for ts, cwd, cmd in other_launches(ctx, o.path, call.ts - 60, call.ts + 60):
            if cwd == call.cwd:
                if n_text and n_text in cmd:
                    twin = True
                else:
                    concurrent = True
    if twin:
        out.append('twin_text')
    elif concurrent:
        out.append('concurrent')
    for o in ctx.tree.others:
        fi = next(((ts, x) for ts, src, x in o.prompts if src == 'human'), None)
        if fi and mine_a and coverage(mine_a, norm(fi[1])) >= COVER_MIN:
            out.append('paste')
            break
    return out or ['none']


def read_disk_script(call):
    """The text of the script a call runs (`bash x.sh`, `./x.sh`), read from disk: the part of the launch a record may not hold."""
    m = re.match(r'\s*(?:bash|sh|zsh|source|\.|python3?)?\s*(\S+)', call.cmd)
    p = m.group(1) if m else None
    if not p:
        return None
    data = read_aux(p if os.path.isabs(p) else os.path.join(call.cwd or '', p), TEXT_CAP, skip_big=False)
    return data.decode('utf-8', 'replace') if data is not None else None


def nopersist_cases(ctx, res, nodes):
    """Calls that start `claude -p --no-session-persistence`: no record is left, so there is no child to link; the call alone says the shape."""
    for nd in nodes:
        if nd.kind == 'sub' and not nd.agent:
            continue
        for c in nd.calls:
            if not LAUNCHY_RE.search(c.cmd) or '--no-session-persistence' not in call_material(nd, c) + (read_disk_script(c) or ''):
                continue
            read = ctx.script_reader(nd, c)
            for inv in analyze_command(c.cmd, read)['invs']:
                if inv.form != 'nopersist':
                    continue
                way = pick_way(inv, c, res)
                if way is None:
                    continue
                v = {'target': 'cli', 'os': 'linux', 'bait': 'none', 'spawner': {'main': 'main', 'sub': 'sub', 'child': 'grand'}[nd.kind], 'way': way,
                     'form': 'nopersist', 'via': 'arg', 'src': 'call', 'cwd': 'same', 'out': 'none', 'timing': 'normal', 'decoy': 'none',
                     'seen': 'live' if c.over() is None else 'ended_unseen'}
                res.add(A.Case('aff', v))
                res.counts['nopersist_launches'] += 1


# ---------------------------------------------------------------------------------------------------------------------
# State: the life an agent lived
# ---------------------------------------------------------------------------------------------------------------------
def last_run_start(n):
    """When the last run of a node began: the last sdk / legacy instruction of a child, the last coordinator or typed message of a sub-agent, else its first record."""
    starts = [ts for ts, src, _ in n.prompts if src in ('sdk', 'legacy')] if n.kind == 'child' else [ts for ts, src, _ in n.prompts if src in ('human', 'legacy', 'sdk')]
    return max([t for t in starts if t] or [n.first_ts or 0])


def last_launch_call(ctx, n, t_run):
    """The call that started the last run of a child: the latest resume call at or before it, else the call that launched it."""
    best = n.parent[1] if n.parent else None
    for nd in ctx.all:
        for c in nd.calls:
            if c.ts <= t_run + 1 and n.sid in c.cmd and '--resume' in c.cmd and (best is None or c.ts > best.ts):
                best = c
    return best


def life_of(ctx, n):
    """(life, at, notes) of a node, from its last run. `at` is `after_resume` when an earlier run ended in a resumable way and the node runs again."""
    now = ctx.now
    kind = {'main': 'main', 'child': 'cli', 'sub': 'sub'}[n.kind]
    if kind == 'sub' and n.meta.get('parentAgentId'):
        kind = 'grandsub'
    t_run = last_run_start(n)
    fresh = (n.mtime or 0) > now - STALE_SECS
    errs = [e for e in n.errors if e[0] and e[0] >= t_run - 1]
    ends = [e for e in n.ends if e[0] and e[0] >= t_run]
    cost = [t for t in n.cost_states if t and t >= t_run]
    last_err = max((e[0] for e in errs), default=0)
    last_end = max((e[0] for e in ends), default=0)
    call = last_launch_call(ctx, n, t_run) if n.kind == 'child' else (n.parent[1] if n.parent else None)
    stopped = _stopped(ctx, n, call, t_run)
    life = None
    resumed = False
    if kind == 'main':
        if errs:
            auto = any(t and t >= last_err - 5 and 'automatically' in x for t, x in n.notices)
            life = 'limit_auto' if auto else ('limit_repeat' if len(errs) >= 3 else 'limit_exit')
            resumed = any(src == 'system' and ts and ts > last_err for ts, src, _ in n.prompts) or any(c.ts > last_err + 1 for c in n.calls)
        elif fresh:
            life = 'running'
    elif errs and last_err >= last_end:
        status, ktype, reset, ltype = errs[-1][1], errs[-1][2], errs[-1][3], errs[-1][4]
        if status == 429 or ktype == 'rate_limit':
            if kind in ('sub', 'grandsub'):
                life = 'sub_limit_dead' if (reset and parse_reset(reset) and now > parse_reset(reset)) else 'sub_limit_resume'
            elif not reset:
                life = 'no_reset'
            else:
                life = 'weekly' if ltype and 'seven' in str(ltype) else 'limit_exit'
        elif status == 529 or ktype == 'overloaded':
            life = 'api_529'
        elif status == 400:
            life = 'api_400'
        else:
            return None, None, ['life_shape']
    elif stopped == 'time_limit':
        life = 'time_limit_kill'
    elif stopped == 'task':
        life = 'taskstop_kill'
    elif last_end and last_end >= (n.last_ts or 0) - 600:
        life = 'normal_end'
    elif kind == 'cli' and cost:
        life = 'time_limit_silent'
    elif fresh:
        life = 'running'
    elif kind == 'cli' and call is not None and call.notif_status == 'failed':
        life = 'crash'
    else:
        life = 'stalled_silent' if (n.last_ts and now - n.last_ts >= STALL_SECS) else 'running'
    if life is None:
        return None, None, ['life_shape']
    at = 'live' if life in ('running', 'stalled_silent') else 'just_ended'
    if resumed and life in A.RESUMABLE:
        at = 'after_resume'
    prev = _previous_life(ctx, n, t_run, kind)
    if life in ('running', 'stalled_silent') and prev in A.RESUMABLE:
        life, at = prev, 'after_resume'
    if life not in A.LIFE_BY_KIND[kind]:
        return None, None, ['life_shape']
    return life, at, []


def parse_reset(r):
    if isinstance(r, (int, float)):
        return float(r)
    return parse_ts(r) if isinstance(r, str) else None


def _stopped(ctx, n, call, t_run=0.0):
    """'time_limit' when the launching call's notification says it hit the background time limit, 'task' when the launcher stopped it (TaskStop) or its notice says
    stopped; only what happened in the last run counts."""
    if call is not None and call.notif_text and (call.notif or 0) >= t_run:
        t = call.notif_text.lower()
        if 'time limit' in t:
            return 'time_limit'
        if 'stopped' in t or call.notif_status == 'killed':
            return 'task'
    ids = {x for x in (call.bg_id if call is not None else None, n.agent) if x}
    for nd in ctx.all:
        if any(t in ids and ts and ts >= t_run for ts, t in nd.taskstops):
            return 'task'
    return None


def _previous_life(ctx, n, t_run, kind):
    """The life the run before the last one ended in, when it ended in a resumable way (else None)."""
    before = [e for e in n.errors if e[0] and e[0] < t_run - 1]
    if before:
        e = before[-1]
        if e[1] == 529 or e[2] == 'overloaded':
            return 'api_529'
        if e[1] == 429:
            if kind in ('sub', 'grandsub'):
                return 'sub_limit_resume'
            if not e[3]:
                return 'no_reset'
            return 'weekly' if e[4] and 'seven' in str(e[4]) else 'limit_exit'
    if n.kind == 'child':
        first = n.parent[1] if n.parent else None
        old_cost = [t for t in n.cost_states if t and t < t_run - 1]
        if first is not None and first.notif_text and (first.notif or 0) < t_run:
            t = first.notif_text.lower()
            if 'time limit' in t:
                return 'time_limit_kill'
            if 'stopped' in t:
                return 'kill_resume'
        if old_cost and not any(e[0] and e[0] < t_run - 1 for e in n.ends):
            return 'time_limit_silent'
    for nd in ctx.all:
        if any(ts and ts < t_run and (t == n.agent or (n.parent and n.parent[1] is not None and t == n.parent[1].bg_id)) for ts, t in nd.taskstops):
            return 'kill_resume'
    return None


VERSION_MIN, VERSION_MAX = (2, 1, 235), (2, 1, 286)       # the range the private record formats (`cost-state`, `quotaLimits`, notices) were observed in


def _version(v):
    m = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', v)
    return tuple(int(x) for x in m.groups()) if m else None


def flaws_of(n):
    """The record flaws a node shows (a list; ['none'] when it shows none): torn, unknown_type, future_version, old_format, field_gone, child_bg. multi_proc needs the
    process table, which a record cannot show."""
    out = []
    if n.torn:
        out.append('torn')
    if [t for t in n.type_counts if t and t not in KNOWN_TYPES]:
        out.append('unknown_type')
    vs = [x for x in (_version(v) for v in n.versions) if x]
    if any(x > VERSION_MAX for x in vs):
        out.append('future_version')
    legacy = n.kind == 'child' and any(src == 'legacy' for _, src, _ in n.prompts) and not any(src == 'sdk' for _, src, _ in n.prompts)
    if any(x < VERSION_MIN for x in vs) or legacy:
        out.append('old_format')
    if n.cost_field_gone and not ({'future_version', 'old_format'} & set(out)):
        out.append('field_gone')
    if n.kind == 'child' and any(c.bg and c.bg_id and c.notif for c in n.calls):
        out.append('child_bg')
    return out or ['none']


def sta_case(ctx, res, n):
    if n.kind == 'child' and not n.parent and n is not ctx.tree.root:
        return
    life, at, notes = life_of(ctx, n)
    if life is None:
        if n.kind == 'main' and n.mtime and ctx.now - n.mtime >= STALE_SECS and not n.errors:
            res.counts['skipped_ended_main'] += 1
            return
        res.need('life_shape')
        return
    kind = {'main': 'main', 'child': 'cli', 'sub': 'sub'}[n.kind]
    if kind == 'sub' and n.meta.get('parentAgentId'):
        kind = 'grandsub'
    for flaw in flaws_of(n):
        res.add(A.Case('sta', {'skind': kind, 'life': life, 'flaw': flaw, 'at': at, 'os': 'linux'}))
    res.unobserved.add('os')


# ---------------------------------------------------------------------------------------------------------------------
# Debate: where a participant's report goes and what it did about it
# ---------------------------------------------------------------------------------------------------------------------
RPATH_RE = re.compile(r'((?:~|\.{1,2}|\$\{?\w+\}?|/)?[\w./~$ {}-]*?)((?:r0?|round)\d+)/([A-Za-z][\w.-]*?)\.md')
REPORT_FILE_RE = re.compile(r'((?:r0?|round)\d+)/([A-Za-z][\w.-]*?)\.md$')
MARKER_RE = re.compile(r'^\[[A-Z][\w-]*\]')


def rdir_style(tok):
    return 'round1' if tok.startswith('round') else ('r01' if re.fullmatch(r'r0\d+', tok) else 'r1')


def nstyle_of(stem, sibling_stems):
    letters = collections.defaultdict(set)
    for x in sibling_stems | {stem}:
        letters[x[:1].upper()].add(x)
    if len([x for x in letters.get(stem[:1].upper(), ()) if x != stem]) >= 2 and re.fullmatch(r'[A-Za-z](_\w+)?', stem):
        return 'collide'
    if re.fullmatch(r'[A-Z]', stem):
        return 'plain'
    if re.fullmatch(r'[a-z]', stem):
        return 'lower'
    if re.fullmatch(r'[A-Z]_\w+', stem):
        return 'named'
    if re.fullmatch(r'[A-Za-z]+\d+', stem):
        return 'numbered'
    return None


NEGATION_RE = re.compile(r"(?i)(?:do not|don't|no need|not need|need not|never|without)[^.`]{0,30}$")
WRITE_VERB_RE = re.compile(r'(?i)\b(?:write|save|put|output|store)\b[^.`]{0,60}$')


def resolve_report(rel, cwd, extra=()):
    """The absolute path a report path names, looked up from the folders an agent may have stood in; the first base where the path exists wins, else the first base."""
    if rel.startswith('~'):
        rel = os.path.expanduser(rel)
    if os.path.isabs(rel):
        return os.path.normpath(rel)
    bases = [b for b in [cwd] + list(extra) if b]
    cur = cwd
    for _ in range(6):
        cur = os.path.dirname(cur) if cur else None
        if cur and cur not in ('/', ''):
            bases.append(cur)
    for b in bases:
        p = os.path.normpath(os.path.join(b, rel))
        if os.path.exists(p) or os.path.isdir(os.path.dirname(p)):
            return p
    return os.path.normpath(os.path.join(bases[0], rel)) if bases else rel


def intent_of(ins, start):
    """The role the words before a path in an instruction give it: writer (`write ... to <path>`), negator (`do not need to write <path>`), quoter (the path sits inside a quoted example), else None."""
    before = ins[max(0, start - 90):start].rstrip(' `\'"')
    if NEGATION_RE.search(before):
        return 'negator'
    if re.search(r'["\u201c][^"\u201d]{0,40}$', before) and re.search(r'(?i)\bwrite\b', before[-70:]):
        return 'quoter'
    if WRITE_VERB_RE.search(before):
        return 'writer'
    return None


def deb_cases(ctx, res, n):
    """A debate participant: a sub-agent or a `claude -p` child told where its report goes. One case from what its instruction and its calls say (nothing when it is not one)."""
    ins = next((x for ts, src, x in n.prompts if src in ('human', 'sdk', 'legacy')), '')
    kind = {'sub': 'sub', 'child': 'cli'}.get(n.kind)
    if kind is None:
        return
    launch = n.parent[1] if (n.kind == 'child' and n.parent) else None
    inv = None
    if launch is not None:
        read = ctx.script_reader(n.parent[0], launch)
        invs = analyze_command(launch.cmd, read)['invs']
        inv = pick_invocation(invs, norm(ins), launch) if invs else None
    m = RPATH_RE.search(ins)
    rpath = where = intent = None
    if inv is not None:
        for op, raw in inv.redirs:
            if op in ('>', '>>') and raw and REPORT_FILE_RE.search(unquote(raw)):
                where = unquote(raw)
                mv = re.match(r'\$\{?(\w+)\}?', where)
                rpath = ('var' if mv and mv.group(1) in inv.assigns else 'var_ext') if mv else 'redirect'
                intent = 'writer'
    if rpath is None and m:
        where = m.group(0).strip()
        intent = intent_of(ins, m.start())
        first = where[:1]
        if first == '~':
            rpath = 'tilde'
        elif first == '/':
            rpath = 'abs'
        elif first == '$':
            mv = re.match(r'\$\{?(\w+)', where)
            rpath = 'var' if kind == 'cli' and inv is not None and mv and mv.group(1) in inv.assigns else 'var_ext'
        elif where.startswith('..'):
            rpath = 'dotdot'
        else:
            rpath = 'folder' if '/' in where[:where.rindex(m.group(2))].strip('/') else 'short'
    wrote = [w for w in n.writes if REPORT_FILE_RE.search(w[1])]
    if rpath is None and wrote and 'brief' in ins.lower():
        rpath, where = 'instr_only', wrote[0][1]
        intent = 'writer' if any(n.results.get(w[4]) is None or not n.results[w[4]][2] for w in wrote) else None
    if rpath is None:
        return
    tail = REPORT_FILE_RE.search(where) or (REPORT_FILE_RE.search(wrote[0][1]) if wrote else None)
    if tail is None:
        res.need('debate_shape', 'report_path')
        return
    rd, stem = rdir_style(tail.group(1)), tail.group(2)
    report = resolve_report(where, n.cwd or '', [ctx.tree.root.cwd or ''] if rpath not in ('var', 'var_ext') else [])
    if rpath == 'var_ext':
        report = ''
    unit_dir = os.path.dirname(os.path.dirname(report)) if report else None
    structure, siblings = 'single', set()
    if unit_dir:
        rel = os.path.relpath(unit_dir, n.cwd) if n.cwd and unit_dir.startswith(n.cwd.rstrip('/') + '/') else unit_dir
        if os.path.isdir(unit_dir):
            rdirs = [d for d in os.listdir(unit_dir) if re.fullmatch(r'(?:r0?|round)\d+', d)]
            siblings = {f[:-3] for d in rdirs for f in os.listdir(os.path.join(unit_dir, d)) if f.endswith('.md')}
            guides = [g for g in ('brief.md', 'README.md') if os.path.exists(os.path.join(unit_dir, g))]
            if os.path.exists(os.path.join(os.path.dirname(unit_dir), 'brief.md')):
                structure = 'topics'
            elif guides == ['README.md']:
                structure = 'readme'
            elif not guides and not rdirs:
                intent = intent if intent in ('negator', 'quoter') else 'ghost'
        elif unit_dir:
            intent = intent if intent in ('negator', 'quoter') else 'ghost'
        if structure == 'single':
            parts = [c for c in rel.split(os.sep) if c not in ('', '.', '..')]
            if any(c.startswith('.') for c in parts):
                structure = 'dot'
            elif len(parts) >= 4 and 'records' in parts:
                structure = 'deep3'
    ns = nstyle_of(stem, siblings)
    if ns is None:
        res.need('debate_shape', 'seat_name')
        return
    life, at, _ = life_of(ctx, n)
    if life is None:
        res.need('life_shape')
        return
    life = {'sub_limit_resume': 'limit_exit', 'sub_limit_dead': 'limit_exit'}.get(life, life)
    fstate = 'none'
    if report and os.path.exists(report):
        fstate = 'written' if os.path.getsize(report) > 0 else 'empty'
    failed = [w for w in wrote if n.results.get(w[4]) is not None and n.results[w[4]][2]]
    ok_writes = [w for w in wrote if w not in failed]
    reads_report = any(REPORT_FILE_RE.search(r[1]) for r in n.reads)
    if intent in ('negator', 'quoter', 'ghost'):
        role = intent
    elif ok_writes or intent == 'writer':
        role = 'writer'
    elif failed:
        role = 'failed_write'
    elif reads_report:
        role = 'reader'
    else:
        res.counts['skipped_deb_roles'] += 1
        return
    guides = {os.path.dirname(x) for x in re.findall(r'[\w./~$-]*(?:brief|README)\.md', ins)}
    marker = 'own' if MARKER_RE.match(ins) else ('cross' if len(guides) >= 2 else 'none')
    v = {'structure': structure, 'kind': kind, 'rpath': rpath, 'nstyle': ns, 'rdir': rd, 'role': role, 'marker': marker,
         'decl': 'yes', 'homonym': 'none', 'fstate': fstate, 'life': life}
    res.add(A.Case('deb', v))


# ---------------------------------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------------------------------
def fmt_value(x):
    if x is None:
        return 'None'
    if isinstance(x, tuple):
        if len(x) == 2 and x[0] == 'T':
            return 'T+%d' % x[1]
        return '(%s)' % ','.join(fmt_value(i) for i in x)
    if isinstance(x, (set, frozenset, list)):
        return '[%s]' % ','.join(sorted(fmt_value(i) for i in x))
    return str(x)


def expect_lines(case):
    """The expected answers of a case, from the oracle: one line per subject, then the diagnostics and the facts the board must not show."""
    T = O.truth(case)
    out = []
    for subj in sorted(T.subjects):
        out.append('  expect %s %s' % (subj, ' '.join('%s=%s' % (k, fmt_value(T.subjects[subj][k])) for k in sorted(T.subjects[subj]))))
    for code, subj in T.diag:
        params = {k: v for k, v in T.diag_params.get((code, subj), {}).items() if k != 'version'}
        out.append('  diag %s %s%s' % (code, subj, ''.join(' %s=%s' % (k, fmt_value(params[k])) for k in sorted(params))))
    for subj, field, value in T.forbid:
        out.append('  forbid %s %s != %s' % (subj, field, fmt_value(value)))
    return out


def report(res, out, as_json=False):
    """Writes the result through `Out`. Returns the exit code: 0, or 2 when a feature needs a new axis value."""
    if as_json:
        doc = {'cases': [{'id': c.id, 'count': n, 'expect': expect_lines(c)} for c, n in res.cases.values()],
               'unobserved': sorted(res.unobserved), 'counts': dict(sorted(res.counts.items())), 'new_axis_value_needed': sorted(res.unexpressible.items())}
        out.line(json.dumps(doc, sort_keys=True))
        return 2 if res.unexpressible else 0
    out.line('harvest: %d case shapes' % len(res.cases))
    out.line('counts: ' + ' '.join('%s=%d' % kv for kv in sorted(res.counts.items())))
    for case, n in res.cases.values():
        out.line('case %d x %s' % (n, case.id))
        for line in expect_lines(case):
            out.line(line)
    if res.unobserved:
        out.line('unobserved: ' + ' '.join(sorted(res.unobserved)))
    for name, n in sorted(res.unexpressible.items()):
        out.line('%s%s  x %d' % (NEW_AXIS, name, n))
    return 2 if res.unexpressible else 0


def check_text(text):
    """(ok, bad token count) for an earlier output: every token must be inside the vocabulary."""
    bad = foreign_tokens(text, vocabulary())
    return bad == 0, bad


def main(argv=None):
    ap = argparse.ArgumentParser(description='Axis values and expected answers from a real session tree (nothing else is printed).')
    ap.add_argument('session', nargs='?', help='session id or an unambiguous prefix of it')
    ap.add_argument('--claude-home', default=os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude'))
    ap.add_argument('--json', action='store_true', help='one JSON document instead of lines')
    ap.add_argument('--now', type=float, default=None, help='the clock (epoch seconds) for liveness: default now')
    ap.add_argument('--check', metavar='FILE', help='test that every token of FILE (or - for stdin) is inside the allowed vocabulary')
    args = ap.parse_args(argv)
    if args.check:
        if args.check == '-':
            text = sys.stdin.read()
        else:
            with open(args.check, encoding='utf-8') as f:
                text = f.read()
        ok, bad = check_text(text)
        print('ok' if ok else 'not allowed: %d token(s)' % bad)
        return 0 if ok else 1
    if not args.session:
        ap.error('a session id is needed (or --check FILE)')
    try:
        tree = read_tree(args.claude_home, args.session)
    except LookupError:
        print('no such session', file=sys.stderr)
        return 1
    except ValueError:                                  # find_session: more than one session starts with the prefix (a fixed text: nothing of a record may reach stderr)
        print('ambiguous session prefix', file=sys.stderr)
        return 1
    res = classify(tree, args.now)
    return report(res, Out(sys.stdout), as_json=args.json)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:      # noqa: BLE001 - a crash must not print a traceback: its lines can hold text of the records (a KeyError names its key)
        print('harvest failed: %s' % type(e).__name__, file=sys.stderr)
        sys.exit(3)
