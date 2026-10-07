"""What a Codex rollout states about itself and its neighbours: the kind of a thread from its first line (root, native sub-agent, guardian, internal),
the commands it ran (CommandExecution) and the collaboration events (SubAgentActivity, agent_message). Pure functions and a small bounded store; no process, and the only file it opens is a record line it is asked to read again (read_cmd_text)."""

import json
import re
from collections import deque
from urllib.parse import unquote

from .codex_parse import CX_LINE_MAX
from .fingerprint import clip, nbytes


# Every size of a text below is in bytes of memory (board/fingerprint.py: CPython keeps 1, 2 or 4 bytes a character, by the widest one), not in characters.
CX_CMDS_KEEP = 5000        # commands kept per thread (the newest)
CX_CMD_MAX = 64 << 10      # a command text longer than this is not kept (the command itself still is, with cmd None)
CX_TEXT_THREAD = 2 << 20   # command text and message text kept per thread; past it the oldest texts go first (the command stays, with cmd None)
CX_TEXT_TOTAL = 24 << 20   # the same over all threads
CX_COLLAB_KEEP = 2000      # collaboration events kept per thread (the newest)
CX_MSG_MAX = 4 << 10       # the text of an agent_message is cut to this size
CX_DEPTH_MAX = 8           # parent steps root_of follows
CX_GAPS_MAX = 256          # gaps of one thread kept apart; more are folded into one that covers them
CX_REREAD = 512 << 10      # bytes read from the front of a record line to get a command text again
CX_CALLS_KEEP = 256        # calls remembered while they wait for their output
CX_SPAWNS_KEEP = 65536     # call identities kept for spawn proof, apart from the conversation cards; when full, new identities cannot prove anything

CX_SHELLS = frozenset(('sh', 'bash', 'zsh', 'dash', 'ksh', 'fish'))

# The prefix an item_completed line of a kind we read has, found in the first bytes (the item comes after thread_id and turn_id): other items are not decoded.
CX_ITEM_RE = re.compile(rb'"item":\{"type":"(CommandExecution|SubAgentActivity)"')
CX_ITEM_HEAD = 1024
CX_NAME_SCAN = 64 << 10    # how far into a line the name of an item kind is looked for when the usual prefix is not there (the keys are in another order)
CX_NAMES = (b'"CommandExecution"', b'"SubAgentActivity"')

# A CommandExecution item is read from the front of its line, the way Codex writes it: id, process_id, command, cwd ... status ... a long output ... exit_code, duration ...
# The long parts are never decoded, so a line over CX_LINE_MAX is read the same way. A string body is `[^"\\]*(?:\\.[^"\\]*)*`.
_STR = rb'"[^"\\]*(?:\\.[^"\\]*)*"'
CX_CMD_RE = re.compile(rb'"item":\{"type":"CommandExecution","id":"([^"\\]*)","process_id":"([^"\\]*)","command":(\[(?:' + _STR + rb'(?:,' + _STR + rb')*)?\]),"cwd":(' + _STR + rb')')
CX_STATUS_RE = re.compile(rb',"source":"[^"\\]*","status":"([a-z_]+)"')
CX_EXIT_KEY = b'"exit_code":'       # a quote inside a JSON string is always escaped, so this exact text is a key and never a part of an output
CX_EXIT_RE = re.compile(rb'"exit_code":(-?\d+|null),"duration":\{"secs":(\d+),"nanos":(\d+)\}')

# The lines that tell a command is still running: the output of an exec cell (or of a wait on it) says "Script running with cell ID N" or holds a result
# ({"chunk_id":..,"wall_time_seconds":..,"session_id":N,...}) with the PTY `session_id` of a command that went on in the background; the CommandExecution of that process (process_id = that number) comes when the process ends. call_id is before name in a call,
# after the arguments in a wait, and before output in an output.
CX_EXEC_CALL_RE = re.compile(rb'"call_id":"([^"\\]*)","name":"exec"')
CX_WAIT_HEAD = b'"name":"wait"'
CX_CELL_ARG_RE = re.compile(rb'\\"cell_id\\": ?\\"(\d+)\\"')
CX_ANY_CALL_RE = re.compile(rb'"call_id":"([^"\\]*)"')
CX_OUT_RE = re.compile(rb'"call_id":"([^"\\]*)","output":')
CX_RUNNING = b'"Script running with cell ID '
CX_SID_RE = re.compile(rb'\\?"wall_time_seconds\\?": ?[0-9.eE+-]+,\\?"session_id\\?": ?(\d+)')      # in the shape of Codex's own result, not any number a command printed under that name
CX_OUT_HEAD = 16 << 10     # how far into an output the session numbers are looked for

CX_STATES = ('completed', 'failed')
CX_ACTIVITY = ('started', 'completed', 'interrupted')


def _str(x):
    return x if isinstance(x, str) and x else None


def _int(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


# ---------- the kind of a thread (its first line) ----------
def classify(m, no_copy=False):
    """The kind of a thread from the payload of its first line (a dict).
    root      a thread somebody started: `source` is a plain string (or missing) and there is no parent
    sub       a native sub-agent: source.subagent.thread_spawn, whose parent_thread_id is the top-level parent_thread_id; the copied history ends at a known line, or the parent proved none was copied
    guardian  an approval review: source.subagent.other == "guardian" or thread_source == "guardian_review"
    internal  any other dict `source`, a parent where none is expected, parents that disagree, a sub-agent whose history end is unknown: hidden like a guardian, and `drift` says the shape is unknown
    `no_copy` is the index's proof from the parent's matching spawn_agent call, never a guess from this meta.
    Returns {kind, parent, agent_path, nick, depth, prefix_ord, drift}. depth is 0 for a root, and a child is at least 1."""
    src, top = m.get('source'), m.get('parent_thread_id')
    parent = top if isinstance(top, str) and top else None
    sub = src.get('subagent') if isinstance(src, dict) else None
    sub = sub if isinstance(sub, dict) else {}
    spawn = sub.get('thread_spawn') if isinstance(sub.get('thread_spawn'), dict) else None
    out = {'kind': 'internal', 'parent': parent, 'agent_path': None, 'nick': None, 'depth': 1, 'prefix_ord': 0, 'drift': True}
    if (m.get('thread_source') not in (None, 'user', 'subagent', 'guardian_review') or
            m.get('multi_agent_version') not in (None, 'disabled', 'v1', 'v2') or m.get('history_mode') not in (None, 'legacy', 'paginated')):
        return out
    if sub.get('other') == 'guardian' or m.get('thread_source') == 'guardian_review':
        out.update(kind='guardian', drift=False)
    elif spawn is not None:
        start, sp = _int(m.get('subagent_history_start_ordinal')), spawn.get('parent_thread_id')
        if (no_copy and 'subagent_history_start_ordinal' not in m and m.get('thread_source') == 'subagent' and
                m.get('multi_agent_version') == 'v2' and m.get('history_mode') == 'paginated'):
            start = 0
        if parent and sp == parent and start is not None and start >= 0:
            path = _str(spawn.get('agent_path')) or _str(m.get('agent_path'))
            depth = _int(spawn.get('depth'))
            if depth is None or depth < 1:
                depth = len(path.strip('/').split('/')) - 1 if path and path.startswith('/root/') else 1
            out.update(kind='sub', agent_path=path, nick=_str(spawn.get('agent_nickname')) or _str(m.get('agent_nickname')),
                       depth=max(1, depth), prefix_ord=start, drift=False)
    elif not isinstance(src, dict) and not top and m.get('thread_source') in (None, 'user'):
        out.update(kind='root', depth=0, drift=False)
    return out


# ---------- the memory of one thread ----------
INF = float('inf')


class SpawnProofs:
    """The small structural facts that prove a child copied no history, for one parent's file generation. No message text and no conversation-card limit.
    One call at one file offset must be a readable collaboration spawn_agent with fork_turns none; a second call with its id or a conflicting started claim invalidates it.
    The index verifies the hashes of the call and started lines before using them. Re-reading the same offset is no second call.
    When the separate count is full, existing facts stay and new identities are refused (never evicted and later trusted anew)."""

    __slots__ = ('calls', 'children')

    def __init__(self):
        self.calls, self.children = {}, {}

    def _entry(self, cid):
        if not _str(cid):
            return None
        e = self.calls.get(cid)
        if e is None and len(self.calls) < CX_SPAWNS_KEEP:
            e = self.calls[cid] = {'off': None, 'none': False, 'started': None, 'bad': False}
        return e

    def note_call(self, cid, off, p=None, line=None):
        e = self._entry(cid)
        if e is None:
            return
        if e['off'] is not None:
            if e['off'] != off:
                e['bad'] = True
            return
        e['off'] = off
        if (not isinstance(p, dict) or p.get('call_id') != cid or p.get('type') != 'function_call' or
                p.get('name') != 'spawn_agent' or p.get('namespace') != 'collaboration'):
            e['bad'] = True
            return
        try:
            args = json.loads(p['arguments'])
        except (KeyError, TypeError, ValueError, RecursionError):
            e['bad'] = True
            return
        e['none'] = isinstance(args, dict) and args.get('fork_turns') == 'none'
        if e['none']:
            if line is None:
                e['bad'] = True
            else:
                e['call_line'] = line

    def note_started(self, c, line=None):
        e = self._entry(c['call_id'])
        if e is None or e['bad']:
            return
        child = _str(c['agent_thread_id'])
        claim = (child, c['agent_path'])
        if child is None or line is None or (e['started'] is not None and e['started'] != claim):
            e['bad'] = True
        elif e['started'] is None:
            e['started'] = claim
            e['started_line'] = line
            self.children.setdefault(child, set()).add(c['call_id'])

    def none_calls(self, child):
        return tuple(e for cid in self.children.get(child, ()) if (e := self.calls[cid])['none'] and not e['bad'])

    def has_none(self, child):
        return bool(self.none_calls(child))

    def invalidate(self):
        for e in self.calls.values():
            e['bad'] = True


class ThreadFacts:
    """The commands and the collaboration events of one thread, and what is known not to be there (the gaps).
    Bounded: the newest CX_CMDS_KEEP commands and CX_COLLAB_KEEP events; the texts of both (a command's `cmd`, a message's `text`) at most CX_TEXT_THREAD of them here and CX_TEXT_TOTAL
    over all threads, the oldest text going first: its command or event stays with `cmd` / `text` None (a command's text can be read again, CodexIndex.cmd_text).
    Counts of what did not make it: skipped (lines of a kind we read that could not be read), drift (the same, counted for the diagnostics: the shape is not what is known),
    big (a command text over CX_CMD_MAX), evicted (commands pushed out by the count)."""
    __slots__ = ('cmds', 'texted', 'collab', 'ctexted', 'nbytes', 'skipped', 'drift', 'big', 'evicted', 'evict', 'strip', 'stripped', 'gaps', 'runs', 'execs', 'seen_pids', 'calls',
                 'cells', 'turn_start', 'tail_ts')

    def __init__(self):
        self.cmds, self.texted, self.collab, self.ctexted = deque(), deque(), deque(), deque()    # the texted ones are the same dicts as in cmds / collab, oldest first
        self.reset()

    def reset(self):
        for d in (self.cmds, self.texted, self.collab, self.ctexted):
            d.clear()
        self.nbytes = self.skipped = self.drift = self.big = self.evicted = 0
        self.evict = self.strip = None             # [first start, last end] of the commands pushed out by the count / whose text was dropped for the budget
        self.stripped = set()                      # item ids of the commands whose text was dropped for the budget (the only ones a re-read is for)
        self.gaps = []                             # (start, end, why) that are settled: an unreadable line, a text over the limit, a re-read that failed, a run that never came
        self.runs = {}                             # a command known to run whose record has not come: ('pty', process_id) or ('cell', cell id) -> when it started
        self.execs = {}                            # an exec call of the open turn whose output has not come (a command that runs now, in the foreground): call id -> when it was called
        self.seen_pids, self.calls, self.cells = {}, {}, {}
        self.turn_start = self.tail_ts = None

    # ---- commands and messages ----
    def add_cmd(self, c, big=False):
        self.cmds.append(c)
        if c['cmd'] is not None:
            self.texted.append(c)
            self.nbytes += nbytes(c['cmd'])
        if big:                                                # a text over the limit: the command is known, what it ran is not
            self.big += 1
            self.add_gap(c['start'], c['end'], 'big')
        elif c['cmd'] is None:                                 # not a shell command (an argument list): what it ran is not known either
            self.add_gap(c['start'], c['end'], 'argv')
        self.seen_pids[c['process_id']] = c['end']
        if len(self.seen_pids) > CX_CALLS_KEEP:
            del self.seen_pids[next(iter(self.seen_pids))]
        self.runs.pop(('pty', c['process_id']), None)         # the record of a running command has come
        while len(self.cmds) > CX_CMDS_KEEP:
            old = self.cmds.popleft()
            if old['cmd'] is not None:
                self.texted.popleft()
                self.nbytes -= nbytes(old['cmd'])
            self.stripped.discard(old['item_id'])
            self.evicted += 1
            self.evict = [min(old['start'], self.evict[0]), max(old['end'], self.evict[1])] if self.evict else [old['start'], old['end']]
        self.trim(CX_TEXT_THREAD)

    def add_collab(self, c):
        if len(self.collab) >= CX_COLLAB_KEEP:
            old = self.collab.popleft()
            if old['text'] is not None and old['kind'] == 'message':
                self.ctexted.popleft()
                self.nbytes -= nbytes(old['text'])
        self.collab.append(c)
        if c['text'] is not None:
            self.ctexted.append(c)
            self.nbytes += nbytes(c['text'])
        self.trim(CX_TEXT_THREAD)

    def oldest(self):
        """The time of the oldest text held (a command's end, a message's time), or None when no text is held."""
        a, b = self.texted[0]['end'] if self.texted else None, self.ctexted[0]['ts'] if self.ctexted else None
        return b if a is None else a if b is None else min(a, b)

    def strip_oldest(self):
        """Drops the oldest text held; returns how many bytes that freed, or None when no text is held."""
        a, b = self.texted[0]['end'] if self.texted else None, self.ctexted[0]['ts'] if self.ctexted else None
        if a is not None and (b is None or a <= b):
            c = self.texted.popleft()
            n, c['cmd'] = nbytes(c['cmd']), None
            self.stripped.add(c['item_id'])
            self.strip = [min(c['start'], self.strip[0]), max(c['end'], self.strip[1])] if self.strip else [c['start'], c['end']]
        elif b is not None:
            c = self.ctexted.popleft()
            n, c['text'] = nbytes(c['text']), None
        else:
            return None
        self.nbytes -= n
        return n

    def trim(self, limit):
        while self.nbytes > limit and self.strip_oldest() is not None:
            pass

    # ---- gaps ----
    def unreadable(self, start, end):
        """A line of a kind we read that could not be read: counted, and the time it may have run in is a gap."""
        self.skipped += 1
        self.drift += 1
        self.add_gap(start, end, 'unreadable')

    def add_gap(self, start, end, why):
        if len(self.gaps) >= CX_GAPS_MAX:                      # too many to keep apart: one that covers them all
            self.gaps = [(min(g[0] for g in self.gaps), None if any(g[1] is None for g in self.gaps) else max(g[1] for g in self.gaps), 'many')]
        self.gaps.append((start, end, why))

    def wrapper(self, start):
        """(call id, time) of the exec call that a command which began at `start` ran in: the one exec call that was open then (called before it, its output not come yet); None when there was no
        such call or there were several (nothing says which)."""
        found = [(cid.decode() if isinstance(cid, bytes) else cid, t) for cid, t in self.execs.items() if t <= start + 1.0]       # (the ids are kept as the bytes the line was read as)
        return found[0] if len(found) == 1 else None

    def begin_turn(self, ts):
        self.turn_start = ts
        self.execs.clear()                                     # a call of an earlier turn that never got its output is no command running now

    def end_turn(self, ts):
        """A turn ended: what was still running has no record any more to wait for (the process outlived the turn, or its record is not written): settled gaps."""
        for start in self.runs.values():
            self.add_gap(start, ts, 'open')
        self.runs.clear()
        self.cells.clear()
        self.execs.clear()
        self.turn_start = None

    def note_call(self, pt, raw, ts):
        """A call line or an output line (response_item custom_tool_call, custom_tool_call_output, function_call, function_call_output): follows the exec cells and the waits on them
        to know which commands run on after their cell answered."""
        if pt == b'custom_tool_call':
            m = CX_EXEC_CALL_RE.search(raw, 0, 600)
            if m:
                self._remember(m.group(1), (ts, None))
                self.execs[m.group(1)] = ts
                if len(self.execs) > CX_CALLS_KEEP:
                    del self.execs[next(iter(self.execs))]
        elif pt == b'function_call':
            if raw.find(CX_WAIT_HEAD, 0, 300) >= 0:
                cid, cell = CX_ANY_CALL_RE.search(raw, 0, 900), CX_CELL_ARG_RE.search(raw, 0, 900)
                if cid:
                    self._remember(cid.group(1), (ts, cell.group(1).decode() if cell else None))
        else:
            m = CX_OUT_RE.search(raw, 0, 600)
            if not m:
                return
            at, cell = self.calls.pop(m.group(1), (ts, None))
            self.execs.pop(m.group(1), None)
            head = raw[m.end():m.end() + CX_OUT_HEAD]
            if head.startswith(CX_RUNNING):
                n = re.match(rb'\d+', head[len(CX_RUNNING):])
                if n:
                    n = n.group(0).decode()
                    if cell is None:
                        self.cells[n] = at                         # the cell answered "running": it began when its call did
                    else:
                        at = self.cells.setdefault(n, at)          # a wait on a cell that runs on: the start is the cell's, not the wait's
                    self.runs.setdefault(('cell', n), at)
                return
            if cell is not None:
                at = self.cells.get(cell, at)
                if b'Script completed' in head[:120] or b'Script failed' in head[:120]:
                    self.runs.pop(('cell', cell), None)
                    self.cells.pop(cell, None)
            for sid in CX_SID_RE.findall(head) if b'session_id' in head else ():       # the plain search first: the pattern has no literal to start from
                sid = sid.decode()
                if self.seen_pids.get(sid, -INF) < at:         # not already written (the record of a command that ended right then can come first)
                    self.runs.setdefault(('pty', sid), at)

    def _remember(self, call_id, what):
        self.calls[call_id] = what
        if len(self.calls) > CX_CALLS_KEEP:
            del self.calls[next(iter(self.calls))]


# ---------- CommandExecution ----------
def shell_command(command):
    """The command text of a `command` value shaped [shell, '-lc' | '-c', text]; None for any other shape (an argument list, a program that is no shell)."""
    if not (isinstance(command, list) and len(command) == 3 and all(isinstance(x, str) for x in command)):
        return None
    shell, flag, text = command
    if flag not in ('-lc', '-c') or shell.replace('\\', '/').rsplit('/', 1)[-1].lower().replace('.exe', '') not in CX_SHELLS:
        return None
    return text


def file_path(cwd):
    """A folder from a `file://` address (`file:///a/b` -> `/a/b`, percent signs decoded); a plain absolute path stays; anything else is None."""
    if not isinstance(cwd, str):
        return None
    if cwd.startswith('file://'):
        rest = cwd[7:]
        if rest.startswith('localhost/'):
            rest = rest[9:]
        return unquote(rest) if rest.startswith('/') else None
    return cwd if cwd.startswith('/') else None


def _cmd_exec(thread, ts, offset, item_id, process_id, command, cwd, status, exit_code, secs, nanos):
    cmd = shell_command(command)
    big = cmd is not None and nbytes(cmd) > CX_CMD_MAX
    dur = secs + nanos / 1e9 if secs is not None and nanos is not None else 0
    return {'thread': thread, 'item_id': item_id, 'process_id': process_id, 'cmd': None if big else cmd, 'cwd': file_path(cwd),
            'start': ts - dur, 'end': ts, 'status': status, 'exit_code': exit_code, 'offset': offset, 'exec': None}, big


def parse_cmd_exec(raw, at, thread, ts, offset=None):
    """(CmdExec, big) from one item_completed line whose item is a CommandExecution that starts at byte `at`, or (None, False) when it cannot be read (the caller counts that),
    or (False, False) when the line turns out not to hold a CommandExecution at all (it was only found by name, the keys being in another order than usual).
    `offset` is where the line is in its file, kept for reading the command again. The front of the line gives the item id, process id, command, folder and status; exit_code and
    duration come from the long part behind the output when they are where Codex puts them, else they are None (and the start is the end). A line whose front is not in the
    usual order is decoded whole, whatever the order, unless it is over CX_LINE_MAX."""
    m = CX_CMD_RE.match(raw, at)
    if m and ts is not None:
        st = CX_STATUS_RE.search(raw, m.end())
        if st and st.group(1).decode() in CX_STATES:
            try:
                command, cwd = json.loads(m.group(3)), json.loads(m.group(4))
            except ValueError:
                return None, False
            exit_code = secs = nanos = None
            at = raw.rfind(CX_EXIT_KEY, st.end())
            tail = CX_EXIT_RE.match(raw, at) if at >= 0 else None
            if tail:
                exit_code = None if tail.group(1) == b'null' else int(tail.group(1))
                secs, nanos = int(tail.group(2)), int(tail.group(3))
            return _cmd_exec(thread, ts, offset, m.group(1).decode(), m.group(2).decode(), command, cwd, st.group(1).decode(), exit_code, secs, nanos)
    if len(raw) > CX_LINE_MAX:
        return None, False
    try:
        it = json.loads(raw)['payload']['item']
        if it.get('type') != 'CommandExecution':
            return False, False
        dur = it.get('duration') if isinstance(it.get('duration'), dict) else {}
        pid = it.get('process_id')
        if ts is None or it['status'] not in CX_STATES or not isinstance(it['id'], str):
            return None, False
        return _cmd_exec(thread, ts, offset, it['id'], pid if isinstance(pid, str) else (str(pid) if _int(pid) is not None else None), it.get('command'), it.get('cwd'),
                         it['status'], _int(it.get('exit_code')), _int(dur.get('secs')), _int(dur.get('nanos')))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None, False


def run_window(raw, ts):
    """(start, end) of the run of a CommandExecution line that could not be read as a command, from the duration it gives (found where Codex writes it, else in the decoded
    line when that is small); None when the duration is not to be had. The end is the time of the line."""
    if ts is None:
        return None
    at = raw.rfind(CX_EXIT_KEY)
    tail = CX_EXIT_RE.match(raw, at) if at >= 0 else None
    if tail:
        return ts - (int(tail.group(2)) + int(tail.group(3)) / 1e9), ts
    if len(raw) <= CX_LINE_MAX:
        try:
            dur = json.loads(raw)['payload']['item']['duration']
            return ts - (dur['secs'] + dur['nanos'] / 1e9), ts
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            pass
    return None


def read_cmd_text(path, offset, item_id):
    """The command text of the record line at `offset` of a rollout, or None: the file or the line is not what it was, the item is not `item_id`, the command is no shell
    command or is over CX_CMD_MAX. Reads the front of the line only."""
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            raw = f.read(CX_REREAD)
    except (OSError, TypeError, ValueError, OverflowError):
        return None
    im = CX_ITEM_RE.search(raw, 0, CX_ITEM_HEAD)
    m = CX_CMD_RE.match(raw, im.start()) if im and raw.startswith(b'{"timestamp"') else None
    if not m or m.group(1).decode() != item_id:
        return None
    try:
        text = shell_command(json.loads(m.group(3)))
    except ValueError:
        return None
    return text if text is not None and nbytes(text) <= CX_CMD_MAX else None


# ---------- collaboration ----------
SUB_DONE = 'subagent-completed-'


def parse_activity(raw, thread, ts):
    """The Collab of a SubAgentActivity item (kind started | completed | interrupted), or None. `call_id` is the id of the item: for `started` it is the call that spawned the
    sub-agent; for `completed` it is `subagent-completed-<turn id of the sub-agent>`, and that turn id is `sub_turn_id`."""
    try:
        it = json.loads(raw)['payload']['item']
        if it['kind'] not in CX_ACTIVITY:
            return None
        cid = _str(it.get('id'))
        turn = cid[len(SUB_DONE):] if it['kind'] == 'completed' and cid and cid.startswith(SUB_DONE) and len(cid) > len(SUB_DONE) else None
        return collab(thread, it['kind'], ts, call_id=cid, agent_thread_id=_str(it.get('agent_thread_id')), agent_path=_str(it.get('agent_path')), sub_turn_id=turn)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None


def collab(thread, kind, ts, **kw):
    """A Collab dict: every key is there, None (encrypted: False) where the event has no such thing."""
    c = {'thread': thread, 'kind': kind, 'ts': ts, 'call_id': None, 'agent_thread_id': None, 'agent_path': None, 'author': None, 'recipient': None, 'text': None,
         'msg_id': None, 'turn_id': None, 'sub_turn_id': None, 'encrypted': False}
    c.update(kw)
    return c


def message_text(p):
    """The text of an agent_message payload: its input_text parts joined by a line break; None when there is none (the parts may also hold an encrypted_content). Whole, not cut.
    When the message is encrypted this is only the note that comes with it (who it is from and where it goes), not what it says."""
    parts = [b.get('text') for b in p.get('content') or [] if isinstance(b, dict) and b.get('type') == 'input_text' and isinstance(b.get('text'), str)] \
        if isinstance(p.get('content'), list) else []
    return '\n'.join(parts) if parts else None


def parse_message(p, thread, ts):
    """(Collab, whole text) of an agent_message payload (a dict). The Collab text is the first CX_MSG_MAX bytes (of memory) of the input_text parts; `encrypted` says the message also has an
    encrypted_content part: what it says is not in the record. `turn_id` is the turn of the thread that receives the message."""
    text = message_text(p)
    short = clip(text, CX_MSG_MAX) if text is not None else None
    meta = p.get('internal_chat_message_metadata_passthrough')
    return collab(thread, 'message', ts, author=_str(p.get('author')), recipient=_str(p.get('recipient')), text=short, msg_id=_str(p.get('id')),
                  turn_id=_str(meta.get('turn_id')) if isinstance(meta, dict) else None,
                  encrypted=isinstance(p.get('content'), list) and any(isinstance(b, dict) and b.get('type') == 'encrypted_content' for b in p['content'])), text
