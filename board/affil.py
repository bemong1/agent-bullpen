"""Affiliation judgment: who launched a `claude -p` child session.

The judgment is split into four fields that are decided separately and carry their own certainty:
  tree   the session whose record holds the launching call (the launcher's own session id; for a grandchild that is the `claude -p` child that launched it)
  node   the main session (None) or one of its sub-agents (`a` + 16 hex) whose record holds the call
  call   the Bash tool_use that launched it
  by     per run of the child (a resumed child has several): who handed that run its instruction

Evidence ranks, best first: 1 `out` (the launching call's redirect file holds the child's session id) · 2 `env` / `proc` / `file` / `cache` (the live
process, its environment, a remembered link: they name the TREE only, never the node) · 3 `content` (a long instruction found in the text of an owner that
has a launching call running at that moment, one the command reader or the text of a command or script shows to start a session) and a literal `--resume <id>`
in a running call · 4 `content_short` (a short instruction equal to a literal argument of a running call; also a long instruction found in the text of an owner
whose only launching call is a script nobody could read, or whose text comparison was cut) · 5 `time` (the command reader sees a `claude -p` at a command
position in the same folder, one candidate). A tie at the best rank holds the field: the judgment never falls through to a lower rank to break it.
An owner whose text holds the words but that ran nothing able to start a session wrote them and is not their launcher: it is no evidence at all, the child
is not linked on that account, and the author is named in the diagnostic `content_author_differs`.

All functions here are pure: they take facts (Span-like calls, instruction turns, text pools) and return a decision. Nothing reads a file or a process.
Standard library only; Python 3.9 compatible.

Run boundaries belong to board/runstate.py: `RunStarts` below feeds its RunTracker and only adds the words of each run's start instruction (for the
anchors). Counting launches against runs (`assign_launches`) is at the end of this module.
"""

import bisect
import collections
import json
import re

from . import fingerprint as fp
from . import runstate
from .facts import RULE_RANK, Relation

_TYPE_S_RE = re.compile(r'"type"\s*:\s*"(?:user|cost-state)"')
_TYPE_B_RE = re.compile(rb'"type"\s*:\s*"(?:user|cost-state)"')
RUN_SLACK_BEFORE = 1.0           # a call is running at t0 when start <= t0 + 1 s ...
RUN_GRACE = 10.0                 # ... and t0 <= end + 10 s (a background call that returned at once: -0.4 s seen)
OPEN_SPAN_MAX = 7200.0           # a call whose end was never seen is taken to run this long at most
OUT_MAX_AGE = 7200.0             # an output file only proves a launch that started at most this long before the child
CALL_SCAN_MAX = 21600.0          # how far back a running call is looked for (longer calls with a seen end are not found)
ORPHAN_GRACE = 10.0              # a call that ended is judged for `orphan_launch` after this long (a child can still be starting)
ORPHAN_OPEN = 15.0               # a call still running is judged (one launch, no child at all) after this long
UNKNOWN_COUNT = 16               # a loop whose count is not known is given at least this many launches (link.LOOP_UNKNOWN)
NODE_ALIVE_GRACE = 60.0          # a sub-agent is alive from its first record until its last one + this
NODE_RANKS = (1, 3, 4, 5)        # ranks whose evidence says something about the node (2 says the tree only)
TOOLS = ('claude', 'codex')          # what a call can start: `claude -p` sessions and `codex exec` threads
KIND_ORDER = ('out', 'env', 'proc', 'file', 'cache', 'content', 'resume', 'content_short', 'time')
RANK_OF = {'out': 1, 'env': 2, 'proc': 2, 'file': 2, 'cache': 2, 'content': 3, 'resume': 3, 'content_short': 4, 'time': 5}
RULE_OF = {'resume': 'content'}  # a literal --resume <id> in a running call is stored under the content rule


class Launch:
    """One `claude -p` the command reader saw inside a call: where it runs, how many children it can start (loop count), what it was told."""
    __slots__ = ('cwd', 'n', 'arg', 'loop_args', 'resume', 'session_id', 'persist', 'reader', 'redirects', 'reopens', 'cwds', 'src_text')

    def __init__(self, cwd=None, n=1, arg=None, loop_args=(), resume=None, session_id=None, persist=True, reader=True, redirects=None, reopens=False, cwds=(),
                 src_text=None):
        self.cwd, self.n, self.arg, self.loop_args = cwd, n, arg, tuple(loop_args)
        self.src_text = src_text                    # when the instruction is read from one file (`$(cat P)`, `< P`): a function giving the normalised text P held when the call ran, or None when that is not known
        self.cwds = tuple(cwds)                     # when `cwd` is not known: the few folders it can be (a `cd $T/$d` in a loop over a literal list), else ()
        self.resume, self.session_id, self.persist, self.reader = resume, session_id, persist, reader
        self.redirects = redirects or []            # [facts.Redirect]
        self.reopens = reopens                      # the command asks to take an existing session up again (--resume, --continue, --session-id ...), whatever the value


class Call:
    """One launchy Bash call: the facts.Span (when it ran, how it ended), its launches, and the short literals found in its command.
    `starts(tool)`: the call could start a session of that kind, `claude` (`claude -p`) or `codex` (`codex exec`). A call with launches the reader found (or could
    not place) can start claude; a call that names a tool, or runs a script file whose body names it or could not be read, can start that tool. A call that only
    runs a server, a test run or a script that was read and starts nothing starts neither; a call that starts Codex cannot have started a `claude -p` child.
    `starts(tool, read=True)` leaves out what is only assumed: a script nobody could read (missing, too big, binary, a path that is a variable) may start either
    tool, which is no evidence that it starts this one.
    `launching` says which: None decides from the launches and `probe` (a function returning the tools the call names and the tools it only assumes, called
    the first time it is asked; it may read files), True means it names a tool without saying which (both), False none, a tuple the tools it names. `assumed`
    adds tools that are only assumed."""
    __slots__ = ('span', 'launches', 'short_lits', 'desc', 'off', 'id_args', '_tools', '_assumed', '_extra', '_probe')

    def __init__(self, span, launches=None, short_lits=frozenset(), desc='', off=None, id_args=frozenset(), launching=None, probe=None, assumed=()):
        self.span, self.launches, self.short_lits, self.desc, self.off = span, launches or [], short_lits, desc, off
        self.id_args = id_args                         # session ids written right after --resume / --session-id / -r anywhere in the command (a loose reading)
        self._probe = probe
        self._extra = frozenset(assumed)               # tools that are only assumed, whatever else is found
        self._tools = self._assumed = None
        if launching is not None or probe is None:
            self._set(TOOLS if launching is True else launching or ())

    def _set(self, named, assumed=()):
        named = frozenset(named) | (frozenset(('claude',)) if self.launches else frozenset())
        self._assumed = (frozenset(assumed) | self._extra) - named
        self._tools = named | self._assumed

    @property
    def tools(self):
        if self._tools is None:
            self._set(*self._probe())
            self._probe = None
        return self._tools

    def starts(self, tool, read=False):
        if tool == 'claude' and self.launches:
            return True                                # a `claude -p` the reader found: nothing to look up
        return tool in self.tools and not (read and tool in self._assumed)

    @property
    def launching(self):
        return bool(self.launches) or bool(self.tools)


class Instr:
    """The start instruction of one run, reduced: its anchors, its short form, and a normalised head to check a literal argument against."""
    __slots__ = ('ts', 'idx', 'norm_len', 'anchors', 'short', 'head', 'truncated')

    def __init__(self, ts, idx, text):
        raw = fp.clip(text, fp.HEAD_BYTES * 2)                         # the limits count the bytes the text holds, not its characters
        head = fp.normalize(raw)
        self.ts, self.idx = ts, idx
        self.truncated = len(text) > len(raw) or fp.nbytes(head) > fp.HEAD_BYTES
        self.head = fp.clip(head, fp.HEAD_BYTES)
        self.norm_len = len(self.head)
        self.anchors = fp.anchors(self.head)
        self.short = self.head if 0 < self.norm_len < fp.SHORT_MIN else None

    def same_as(self, arg):
        """Whether a normalised literal argument is this instruction (heads only when either side was cut). An instruction that is not known vetoes nothing."""
        if not self.head or arg == self.head:
            return True
        if self.truncated and arg.startswith(self.head):
            return True
        return fp.nbytes(arg) > fp.HEAD_BYTES - 4 and self.head.startswith(arg)       # an argument cut at the cap is a prefix of the head


class ChildFacts:
    """`codex_free`: the environment of the child's process was read and held no name of Codex (`CODEX_THREAD_ID`, `CODEX_SESSION_ID`): no shell of a Codex thread started it, so
    no Codex thread can be what it was started by. False when the environment has them or was never read (a process not seen, no way to read it)."""
    __slots__ = ('sid', 't0', 'cwd', 'entry', 'runs', 'codex_free', 'unknown', 'cleared')

    def __init__(self, sid, t0, cwd, entry, runs, codex_free=False, unknown=(), cleared=()):
        self.sid, self.t0, self.cwd, self.entry, self.runs = sid, t0, cwd, entry, runs
        self.codex_free = codex_free
        self.cleared = tuple(cleared)             # (run number, tree) whose names in the environment were found not to be about that run (_stale_pin): for that run their facts are whole, so they are no competitor nobody can read
        self.unknown = tuple(unknown)             # the Codex threads its environment names that nothing is known of (not in the index): a thread whose commands nobody can read started it, as far as anyone can tell


class Owner:
    """One record file that can hold launching calls: a main session (node None) or one sub-agent, or a Codex thread (a root: node None; a sub-agent: its thread id). `pool(lo, hi)`
    gives (normalised text, complete) of its Bash commands, Write contents and Edit replacements (a Codex thread: the command strings) between two times; `text_of(call)` the
    normalised text of one call. `blind_at(lo, hi)`: the owner may have run a command in that time that its facts do not hold (an unknown-text competitor); `gap_at(lo, hi)`: the same
    leaving out an open turn on its own (what is left is a command known to run whose record has not come, text that was not kept, a line that could not be read ...)."""

    def __init__(self, tree, node=None, first_ts=None, last_ts=None, pool=None, text_of=None, provider='claude', cwd=None, blind_at=None, gap_at=None):
        self.tree, self.node = tree, node
        self.first_ts, self.last_ts = first_ts, last_ts
        self.calls, self._starts = [], []
        self.pool = pool or (lambda lo, hi: ('', True))
        self.text_of = text_of or (lambda call: '')
        self.provider = provider                     # whose record it is: `claude` (a transcript) or `codex` (a thread: its commands come from the facts of the Codex index)
        self.cwd = cwd                               # the folder the thread was opened in (a Codex thread: for the blind test)
        self.blind_at = blind_at or (lambda lo, hi: False)      # True when the owner may have run something between the two times that its facts do not hold (see _blind)
        self.gap_at = gap_at or self.blind_at                   # the same, without a turn that is merely open: for asking whether a thread whose commands are all there can have started something (see _stale_pin)

    def add(self, call):
        i = bisect.bisect_right(self._starts, call.span.start)
        self._starts.insert(i, call.span.start)
        self.calls.insert(i, call)

    def running_at(self, t):
        lo = bisect.bisect_left(self._starts, t - CALL_SCAN_MAX)
        hi = bisect.bisect_right(self._starts, t + RUN_SLACK_BEFORE)
        return [c for c in self.calls[lo:hi] if span_running(c.span, t)]

    def alive_at(self, t):
        return self.first_ts is not None and self.last_ts is not None and self.first_ts <= t <= self.last_ts + NODE_ALIVE_GRACE


def span_running(span, t):
    """Whether the call was running at time t: start <= t + 1 s and t <= end + 10 s; a call whose end was never seen runs for OPEN_SPAN_MAX."""
    if span.start > t + RUN_SLACK_BEFORE:
        return False
    if span.end is None:
        return t <= span.start + OPEN_SPAN_MAX
    return t <= span.end + RUN_GRACE


# ---------------------------------------------------------------------------------------------------------------------
# run starts (the boundary is runstate's, see the module docstring)
# ---------------------------------------------------------------------------------------------------------------------
class RunStarts:
    """Feeds the raw lines of one child record in file order and collects the start instruction of every run. `.runs` is a list of Instr, one per run.
    The boundary is board/runstate.py's ("Run boundaries" in its docstring): a RunTracker is fed the same lines, so the runs are its runs (the first
    user or assistant line after the start of the file or a `cost-state` opens one, a `cost-state` closes it, a turn index written again is the same turn); this
    class only adds what the tracker does not keep, the words of the instruction (for the anchors). The instruction is runs[k].turns[0]; a run without one (an
    older record's slash command) and a slash command's expansion (runstate.prompt_text is '') are instructions of unknown words that veto nothing."""
    MAX_RUNS = 16

    def __init__(self):
        self.runs = []
        self.tracker = runstate.RunTracker()
        self._last = None                                             # the tracker run that has been given its Instr
        self._bare = False                                            # that Instr is of a run that has no instruction line yet

    CONTEXT_MAX = 256 << 10          # a line longer than this is not read as context

    def feed_context(self, raw):
        """A line that comes just before an instruction (the assistant line that asked for a tool, a tool result): handed to the tracker only to let it see how the open run
        stood when the next instruction came, so that a process killed in the middle of its work is told from one that took its next prompt. Nothing else is read of
        such lines (the record is not read line by line) and an instruction line itself is never taken here."""
        tr = self.tracker
        if tr.cur is None or not tr.cur.turns or len(raw) > self.CONTEXT_MAX:
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if isinstance(d, dict) and d.get('type') in ('assistant', 'user') and not d.get('promptSource') and 'turnPosition' not in d:
            tr.feed(d)

    def feed(self, raw):
        """raw: one record line (bytes or str). Returns True when the line opened a run or gave an open run its instruction (that run's Instr is then `.runs[-1]`)."""
        if not (_TYPE_B_RE if isinstance(raw, bytes) else _TYPE_S_RE).search(raw[:400]):
            return False                                              # only these two kinds of line matter here: no need to decode the rest
        try:
            d = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(d, dict):
            return False
        tr = self.tracker
        tr.feed(d)
        r = tr.runs[-1] if tr.runs else None
        if r is None:
            return False
        if r is self._last:
            if self._bare and r.turns:
                self._bare = False                                    # the run opened with a line that is no instruction: its first instruction line comes now
                t = r.turns[0]
                self.runs[-1] = Instr(t.ts or r.start_ts or 0.0, t.idx, runstate.prompt_text(d))
                return True                                           # the run now has its words: it is judged again
            return False
        self._last = r
        if len(self.runs) >= self.MAX_RUNS:
            self._bare = False
            return False
        self._bare = not r.turns
        if r.turns:
            t = r.turns[0]
            self.runs.append(Instr(t.ts or r.start_ts or 0.0, t.idx, runstate.prompt_text(d)))
        else:
            self.runs.append(Instr(r.start_ts or 0.0, 0, ''))
        return True


def _ts(s):
    from .util import parse_ts          # imported here: util pulls in the path constants, and this module must stay importable without them
    return parse_ts(s)


def _user_text(d):
    c = (d.get('message') or {}).get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text']
        if parts and not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c):
            return '\n'.join(parts)
    return None


# ---------------------------------------------------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------------------------------------------------
class Decision:
    """The judgment about one child. `relation` is the facts.Relation (None tree = not linked); `call` the Call object (or None); `held` is 'ambiguous'
    when the evidence tied at its best rank; `diags` a list of (code, detail dict); `by` one (tree, node) or None per run."""
    __slots__ = ('child', 'relation', 'call', 'held', 'held_trees', 'diags', 'rank', 'kind', 'by', 'incomplete', 'assumed', 'claims', 'calls', 'run_claims', 'run_ts', 'run_ranks')

    def __init__(self, child):
        self.child = child
        self.relation = Relation(child=child.sid, tree=None, node=None, call=None)
        self.call, self.held, self.held_trees, self.diags, self.rank, self.kind, self.by, self.incomplete = None, None, (), [], None, None, [], False
        self.assumed = False                     # the link is a guess because the words sit in a session whose only launching call was a script nobody could read (not because the instruction is short)
        self.claims = []                         # the launching calls this child's runs are accounted to (a launch no child is accounted to is an orphan)
        self.calls = []                          # per run: the Call the evidence names for it, or None (`call` is the first run's)
        self.run_claims = []                     # per run: the calls it could have been started by (one entry of `claims`); a run the evidence names has just that call
        self.run_ts = []                         # per run: when it started
        self.run_ranks = []                      # per run: the rank of the evidence that decided its tree (None: none did)

    @property
    def tree(self):
        return self.relation.tree

    @property
    def node(self):
        return self.relation.node

    @property
    def rule(self):
        return self.relation.rule

    @property
    def certain(self):
        return bool(self.relation.certain.get('tree'))


def _cwd_same(a, b):
    import os
    return a is not None and b is not None and os.path.normpath(a) == os.path.normpath(b)


def launch_ok(L, child, run):
    """Whether one launch the command reader found can still be what started this run: it did not ask for no session record, its literal
    instruction argument (or, for a loop over a literal list, one of the listed words; or the file it reads the instruction from, when the board saw it written) is this instruction, and it runs in the child's folder (through
    realpath, so a symlinked folder is the same folder; an unknown folder refutes nothing)."""
    if not L.persist:
        return False
    if L.arg is not None and not run.same_as(L.arg):
        return False
    if L.loop_args and not any(run.same_as(a) for a in L.loop_args):
        return False                                                  # a loop over a literal list can only produce the words of that list
    if L.src_text is not None and L.arg is None and run.anchors:
        held = L.src_text()                                           # the instruction comes from a file the board saw written: it is not this child's if the words are not in it
        if held is not None and fp.coverage(run.anchors, held) < fp.COVER_MIN:
            return False
    if L.cwd is None:
        return not L.cwds or child.cwd is None or any(_same_dir(w, child.cwd) for w in L.cwds)
    return _same_dir(L.cwd, child.cwd)


def viable(call, child, run):
    """Whether a call can still be the one that launched this child (applied to every rank): it is out when the command reader found launches
    in it and none of them can be it. A call the reader could not read (tmux, xargs, a script it could not follow) has no launch to contradict and stays."""
    readers = [L for L in call.launches if L.reader]
    return not readers or any(launch_ok(L, child, run) for L in readers)


def _under(path, base):
    """Whether `path` is `base` or inside it (through realpath when the names differ); an unknown folder on either side is taken to be."""
    import os
    if not path or not base:
        return True
    if _inside(path, base):
        return True
    try:
        return _inside(os.path.realpath(path), os.path.realpath(base))
    except OSError:
        return False


def _blind(owners, child, run, k):
    """The (tree, node) of the owners that may have started run number `k` with something their facts do not hold: a Codex thread whose commands have a gap in the time
    the run started in (a command that runs is written when it ends; a text that was not kept), and that was opened in the child's folder or above it. Such an owner
    is a competitor whose text nobody knows: no other owner's content match is a certain link then (a guess, and its comparison is incomplete). Nobody is one for a child
    whose environment was read and holds no name of Codex (`codex_free`: no Codex shell started it). A tree whose names were found not to be about this run (`child.cleared`) is none for
    this run only: another run of the child, which they may be about, still has it."""
    out = set()
    if child.codex_free:
        return out
    for o in owners:
        if o.tree != child.sid and (k, o.tree) not in child.cleared and o.blind_at(run.ts - RUN_GRACE, run.ts + RUN_SLACK_BEFORE) and _under(child.cwd, o.cwd):
            out.add((o.tree, o.node))
    return out


def _running(owners, child, run):
    """[(owner, call)] whose call was running at the run's start and could be its launcher, leaving out the child's own records."""
    out = []
    for o in owners:
        if o.tree == child.sid:
            continue
        for c in o.running_at(run.ts):
            if viable(c, child, run):
                out.append((o, c))
    return out


def _may_reopen(call, redirect, sid):
    """Whether a later call can have put the id of session `sid` into the file it redirects to: it truncated the file (what the file holds is its own output),
    or it can take an existing session up again (a launch with --resume / --continue / --session-id, one the reader could not place, or the id in its text)."""
    if redirect.op != '>>':
        return True
    return sid in call.id_args or any(not L.reader or L.reopens or L.resume is not None or L.session_id is not None for L in call.launches)


def _writer_ok(call, redirect, child, run):
    """The veto of a launch that cannot be the parent, for a call that wrote an output file: the launch that owns the redirect must still be able to start this run (the call as a whole when the
    redirect is not tied to a launch)."""
    mine = [L for L in call.launches if any(x is redirect for x in L.redirects)]
    return any(launch_ok(L, child, run) for L in mine) if mine else viable(call, child, run)


def _out_evidence(child, run, out, run_index=0):
    """Rank 1. A redirect file that holds the child's session id proves the launching call: the latest call that truncated it (and every call that appended
    to it after that) wrote it. One owner among them gives tree and node (the call only when it is the single writer); several owners tie.
    The file proves the launch of the content it holds now. A call that started after this run did and truncated the file (or can take a session up again)
    wrote that content, so the file shows nothing about who started the run; a writer whose launch the veto refutes (another instruction, another folder) is not the launcher either."""
    t0 = run.ts
    ev, ties = [], []
    if out is None:
        return ev, ties
    for path in out.files_with(child.sid):
        everyone = [(c, r) for c, r in out.writers(path) if c.span.owner_tree != child.sid]
        ws = [(c, r) for c, r in everyone if c.span.start <= t0 + RUN_SLACK_BEFORE and t0 - c.span.start <= OUT_MAX_AGE]
        if not ws:
            continue
        if any(c.span.start > t0 + RUN_SLACK_BEFORE and _may_reopen(c, r, child.sid) for c, r in everyone):
            continue                                                # a later generation wrote (or may have rewritten) the id
        last = max((i for i, (c, r) in enumerate(ws) if r.op != '>>'), default=0)
        eff = [(c, r) for c, r in ws[last:] if _writer_ok(c, r, child, run)]
        if run_index > 0:
            eff = [(c, r) for c, r in eff if span_running(c.span, t0)]       # the file names the call of the run it was written for: a later run needs a call that was running then
        if not eff:
            continue
        who = sorted({(c.span.owner_tree, c.span.owner_node) for c, r in eff}, key=lambda k: (k[0], k[1] or ''))
        if len(who) == 1:
            ev.append((1, 'out', who[0][0], who[0][1], eff[-1][0] if len(eff) == 1 else None))
        else:
            ties.append((1, 'out', who))
    return ev, ties


def _best_call(owner, calls, run):
    """The call inside `owner` that launched the run: the only running one, else the only one whose own text holds the instruction, else None (held)."""
    if len(calls) == 1:
        return calls[0]
    if run.anchors:
        hit = [c for c in calls if fp.coverage(run.anchors, owner.text_of(c)) >= fp.COVER_MIN]
        if len(hit) == 1:
            return hit[0]
    return None


def run_evidence(child, run, run_index, owners, out=None, content=True, pin_trees=(), up=None):
    """Evidence of ranks 1, 3, 4 and 5 for one run: (evidence list, tie list, notes). Evidence = (rank, kind, tree, node, call); tie = (rank, kind, [(tree, node)]).
    notes: {'incomplete': bool, 'content_tie': bool, 'author': tree or None, 'assumed': bool, 'unresolved': [Redirect], 'running': [Call]} (`running`: the calls with launches, read or not, that were running and could be the launcher; `author`: the session whose text holds the instruction although it ran nothing able to start a child; `assumed`: the content evidence is a guess because the owner's only launching call was a script nobody could read). content=False leaves out the fingerprint (rank 3 by text): the quick first
    judgment before the text index exists. Weaker evidence is not looked for once an output file decides tree, node and call (nothing below rank 1 can change
    that), and when a live process or environment already names the tree (`pin_trees`) the text is compared only for owners inside it (it can only pick the node)."""
    t0 = run.ts
    ev, ties = _out_evidence(child, run, out, run_index)
    running = _running(owners, child, run)
    notes = {'incomplete': False, 'content_tie': False, 'author': None, 'assumed': False, 'unresolved': [], 'running': [c for o, c in running if c.launches]}
    by_owner = {}
    for o, c in running:
        by_owner.setdefault((o.tree, o.node), (o, []))[1].append(c)
        for L in c.launches:
            notes['unresolved'].extend(r for r in L.redirects if r.path_resolved is None and r.unresolved_vars)
    if ev or ties:
        return ev, ties, notes                                       # an output file settled (or tied) the launcher: ranks below it cannot change that
    # rank 3: the child's own id written literally after --resume or --session-id in a running call
    hits = [(o, c) for o, c in running if child.sid in c.id_args]                # the call's own text only: a script body is not "the call"
    keys = sorted({(o.tree, o.node) for o, c in hits}, key=lambda k: (k[0], k[1] or ''))
    if len(keys) == 1:
        ev.append((3, 'resume', keys[0][0], keys[0][1], hits[0][1] if len(hits) == 1 else None))
    elif keys:
        ties.append((3, 'resume', keys))
    # rank 3: content, a long instruction found in the text of an owner with a running launching call
    if content and run.anchors and by_owner:
        blind = _blind(owners, child, run, run_index)
        scores, complete, firm, assumed = {}, True, set(), set()
        for key, (o, calls) in by_owner.items():
            if pin_trees and o.tree not in pin_trees:
                continue                                              # a live process or environment names the tree: only its owners can still be told apart by text
            pool, ok = o.pool(t0 - fp.WINDOW_BEFORE, t0 + fp.WINDOW_AFTER)
            scores[key] = fp.coverage(run.anchors, pool)
            complete = complete and ok
            if any(c.starts('claude', read=True) for c in calls):
                firm.add(key)                                         # the owner was running a call shown to start a session: a launch the reader placed, a command or a script that names it
            elif any(c.starts('claude') for c in calls):
                assumed.add(key)                                      # ... or one that may: a script nobody could read
        if fp.pick(scores)[0] == 'tie':
            notes['content_tie'] = True                              # the diagnostic: the same instruction fits several launchers
        scope = {k: v for k, v in scores.items() if not pin_trees or k[0] in pin_trees}
        verdict, got = fp.pick(scope) if scope else ('none', None)
        if verdict in ('tie', 'weak'):
            # Owners that only wrote the words (they ran nothing that could start a session: no evidence, see below) do not make a tie with one that ran a launch: among the owners
            # that did, one that stands out is the answer (a session that relays the instruction through a script it runs, next to the thread that really ran it).
            starters = {k: v for k, v in scope.items() if k in firm or k in assumed}
            v2, g2 = fp.pick(starters) if len(starters) < len(scope) and starters else ('none', None)
            if v2 == 'tie':
                verdict, got = v2, g2                                     # two launchers fit: they are the tie, the one that only wrote the words is not in it
            if v2 == 'ok':
                verdict, got = v2, g2
                wrote = sorted((k for k, v in scope.items() if v >= fp.COVER_MIN and k not in starters), key=lambda k: (k[0], k[1] or ''))
                if wrote:
                    notes['author'] = by_owner[wrote[0]][0].tree         # who wrote the words is still said
        if verdict == 'ok' and got not in firm and got not in assumed:
            # The owner whose text it is was running nothing that could start a session (a server, a test run, a script that was read and starts nothing): it shows
            # who wrote the words, not who started the child. That is no evidence of a launch, so no link: the author is said, and a launcher that reads the words
            # from a file is not pushed out by their author.
            notes['author'] = by_owner[got][0].tree
            notes['incomplete'] = notes['incomplete'] or not complete
        elif verdict == 'ok':
            # Firm only when the comparison was whole and a call shown to start a session was running; a script nobody could read, or text left out of the
            # comparison, makes it a guess.
            o, calls = by_owner[got]
            # A thread that may have run something unknown is a competitor and the comparison is not whole, unless it is one of the winner's own tree (it cannot change the tree: the node is
            # the sub-agent whose recorded launch shows it, a possibility of another node is no evidence) or above the winner's tree in the chain of certain links (the winner is its own
            # descendant, started from a command that may still be running: that blindness is what a launch of the winner looks like from above).
            above = up(got[0]) if (up and blind) else ()
            seen = complete and not child.unknown and not {k for k in blind if k[0] != got[0] and k[0] not in above}     # (a thread nothing is known of is a competitor whose text nobody can read)
            rank, kind = (3, 'content') if seen and got in firm else (4, 'content_short')
            notes['assumed'] = got not in firm
            ev.append((rank, kind, o.tree, o.node, _best_call(o, [c for c in calls if c.starts('claude')], run)))
            notes['incomplete'] = notes['incomplete'] or not seen         # the comparison left text out: never a certain link
        elif verdict == 'tie':
            ties.append((3, 'content', sorted(got, key=lambda k: (k[0], k[1] or ''))))
        elif verdict == 'weak':
            notes['content_tie'] = True                              # one owner leads, but not clearly: no content evidence, the diagnostic only
        elif not complete:
            notes['incomplete'] = True
    # rank 4: a short instruction equal to a literal argument of a running call (same folder when both are known)
    if run.short:
        hits = []
        for o, c in running:
            lits = c.short_lits
            loops = any(run.short == fp.normalize(a) for L in c.launches for a in L.loop_args)
            if (run.short in lits or loops) and c.starts('claude'):
                cw = [L.cwd for L in c.launches if L.reader]
                if not cw or all(w is None for w in cw) or any(_same_dir(w, child.cwd) for w in cw if w is not None):
                    hits.append((o, c))
        keys = sorted({(o.tree, o.node) for o, c in hits}, key=lambda k: (k[0], k[1] or ''))
        if len(keys) == 1:
            ev.append((4, 'content_short', keys[0][0], keys[0][1], hits[0][1] if len(hits) == 1 else None))
        elif keys:
            ties.append((4, 'content_short', keys))
    # rank 5: the command reader saw a `claude -p` in a running call in the child's folder. A launch whose literal argument differs from this instruction
    # is not its parent and leaves the candidates first; what is left must be one call (several calls of one owner give tree and node, not the call)
    cands = [(o, c, L) for o, c in running for L in c.launches if L.reader and launch_ok(L, child, run) and _time_folder_ok(L, c, child.cwd)]
    calls = {id(c): (o, c) for o, c, L in cands}
    keys = sorted({(o.tree, o.node) for o, c in calls.values()}, key=lambda k: (k[0], k[1] or ''))
    if len(keys) == 1:
        o = calls[next(iter(calls))][0]
        ev.append((5, 'time', o.tree, o.node, cands[0][1] if len(calls) == 1 else None))
    elif keys:
        ties.append((5, 'time', keys))
    return ev, ties, notes


def _same_dir(launch_cwd, child_cwd):
    """Rank 4 compares folders through realpath when both are known (a symlinked launch folder is the same folder)."""
    import os
    if launch_cwd is None or child_cwd is None:
        return True
    if os.path.normpath(launch_cwd) == os.path.normpath(child_cwd):
        return True
    try:
        return os.path.realpath(launch_cwd) == os.path.realpath(child_cwd)
    except OSError:
        return False


def _inside(path, base):
    import os
    path, base = os.path.normpath(path), os.path.normpath(base)
    return path == base or path.startswith(base.rstrip('/') + '/')


def _time_folder_ok(L, call, child_cwd):
    """Rank 5 (a guess) compares the folder texts: a known launch folder must be the child's folder (a symlinked one is not); a launch whose folder could not be
    worked out (`cd "$WT" && claude -p ...`) fits only a child that runs inside the folder the session itself was in at that call."""
    import os
    if child_cwd is None:
        return False
    if L.cwd is not None:
        return os.path.normpath(L.cwd) == os.path.normpath(child_cwd)
    if L.cwds:
        return any(os.path.normpath(w) == os.path.normpath(child_cwd) for w in L.cwds)
    base = call.span.cwd
    return base is not None and _inside(child_cwd, base)


def _launched_elsewhere(owners, child, run, tree, node):
    """Whether rank 3 chose a node by text although the launches that node was running read their instruction from somewhere else (`$(cat file)`, a variable,
    stdin: no literal argument ties them to the words) and another node of the same tree was running a launch that fits the child. The text then shows who
    wrote the words, not which node started the child."""
    def launches(o):
        return [L for c in o.running_at(run.ts) for L in c.launches if not L.reader or launch_ok(L, child, run)]
    mine = [L for o in owners if (o.tree, o.node) == (tree, node) and o.tree != child.sid for L in launches(o)]
    if not mine or not all(L.reader and L.arg is None and not L.loop_args for L in mine):
        return False
    return any(L.reader for o in owners if o.tree == tree and o.node != node and o.tree != child.sid for L in launches(o))


def settle(evs, ties, exact=()):
    """The best evidence decides the tree; the best node-capable evidence inside that tree decides the node. A tie at the deciding rank holds the field.
    `exact`: [(tree, node)] of the rank 2 evidence that names the node as well (a Codex environment: the thread that ran the command is the node, or the root itself:
    node None); the explicit exception to "rank 2 says the tree only". -> dict(tree, rank, kind, held, node, node_rank, node_tie, call_ev)"""
    res = {'tree': None, 'rank': None, 'kind': None, 'held': False, 'held_trees': (), 'node': None, 'node_rank': None, 'node_tie': False, 'call': None, 'node_known': False}
    if any(e[1] == 'resume' for e in evs):
        # the child's own id written in a running call names its launcher; the words of an instruction (which another owner can share) are not weighed against it
        evs = [e for e in evs if not (e[0] == 3 and e[1] != 'resume')]
        ties = [t for t in ties if not (t[0] == 3 and t[1] != 'resume')]
    for r in sorted({e[0] for e in evs} | {t[0] for t in ties}):
        # evidence that names one launcher outright (an id written in the call, a single owner's text) is not undone by a tie between other candidates of
        # the same rank: a tie only matters when nothing at that rank is positive
        trees = {e[2] for e in evs if e[0] == r} or {k[0] for t in ties if t[0] == r for k in t[2]}
        if len(trees) > 1:
            res['held'], res['held_trees'] = True, sorted(trees)
            return res
        if trees:
            res['tree'], res['rank'] = next(iter(trees)), r
            kinds = {e[1] for e in evs if e[0] == r} or {t[1] for t in ties if t[0] == r}
            res['kind'] = min(kinds, key=KIND_ORDER.index)
            break
    tree = res['tree']
    if tree is None:
        return res
    cand = [(e[0], e[3], e[4]) for e in evs if e[2] == tree and e[0] in NODE_RANKS]
    cand += [(2, n, None) for t, n in exact if t == tree]
    tie_nodes = [(t[0], k[1]) for t in ties if t[0] in NODE_RANKS for k in t[2] if k[0] == tree]
    ranks = [c[0] for c in cand] + [t[0] for t in tie_nodes]
    if ranks:
        rb = min(ranks)
        nodes = {n for r, n, c in cand if r == rb} | {n for r, n in tie_nodes if r == rb}
        res['node_rank'], res['node_known'] = rb, True
        if len(nodes) == 1:
            res['node'] = next(iter(nodes))
            of_rank = (rb,) if rb != 2 else (1, 3)                    # an environment (2) does not know the call: a certain output file or text match at the same node does
            calls = {id(c): c for r, n, c in cand if r in of_rank and n == res['node'] and c is not None}      # evidence that does not know the call does not undo one that does
            res['call'] = next(iter(calls.values())) if len(calls) == 1 else None
        else:
            res['node_tie'] = True
    return res


def _pin_runs(pins, runs):
    """Which run each pin (live process / environment / remembered link) speaks for: a resumed run's process says who resumed it, not who first
    launched the child. A child with one run takes every pin; with several, a pin belongs to the run that started closest to the process (within two
    minutes), a pin without a start time to the latest run. -> {run index: [pin]}"""
    out = {}
    for p in pins:
        if not (p.get('tree') or p.get('conflict')):
            continue
        k = 0
        if len(runs) > 1:
            ts = p.get('ts')
            if ts is None:
                k = len(runs) - 1
            else:
                k, gap = min(((i, abs(r.ts - ts)) for i, r in enumerate(runs)), key=lambda x: x[1])
                if gap > 120.0:
                    k = len(runs) - 1
        out.setdefault(k, []).append(p)
    return out


def _pin_evidence(pins):
    """(evidence, ties) of the pins of one run. A pin with a `conflict` names no tree: the environment holds the names of two providers that point to different parents and
    nothing decided between them (Lineage.pending): the two are a tie at rank 2, which holds the child (rank 3 and below do not break it)."""
    return ([(2, p['kind'], p['tree'], None, None) for p in pins if p.get('tree')],
            [(2, p['kind'], [tuple(k) for k in p['conflict']]) for p in pins if p.get('conflict')])


def _exact(pins):
    """[(tree, node)] of the pins that name the node as well (`exact`: the environment of a Codex shell says which thread ran the command)."""
    return [(p['tree'], p.get('node')) for p in pins if p.get('exact')]


def _claims(res, notes):
    """The launching calls one run is accounted to: the call itself when it is known; otherwise every call that was running at its start and could have
    started it inside the decided tree (and node), or inside any tree it is held between. A run that could not be placed at all still accounts for the calls
    the command reader read and did not refute (it started while they ran), but not for the ones it could not read; a run every launch of which was refuted
    (a stranger: another instruction, another folder) accounts for nothing, so those launches keep no child. Over-counting here only hides an orphan."""
    if res['call'] is not None:
        return [res['call']]
    cands = notes.get('running') or []
    if res['tree'] is not None:
        return [c for c in cands if c.span.owner_tree == res['tree'] and (not res['node_known'] or c.span.owner_node == res['node'])]
    if res['held']:
        trees = set(res['held_trees'])
        return [c for c in cands if c.span.owner_tree in trees]
    return [c for c in cands if any(L.reader for L in c.launches)]


def _stale_pin(child, run, k, pin, owners, out, content, up):
    """Whether an environment pin (a tree) is not about this run: the names a tmux server or another long-lived process carried on from the shell that
    started it. The pin counts as long as the tree it names (the thread, for a Codex environment) can have started the run: it ran a call that was running at the run's start and could be
    its launcher, or its facts may lack a command in that time (a gap). It is out only when nothing of it can have started the run, its record is whole, and another tree's launch
    fits the run firmly (an output file or an id written in a call that names it, or a match of the instruction that is not a guess: with a thread nobody can read about, it is). The comparison is made as if a Codex shell might
    have started the child, whatever the pin says."""
    tree = pin['tree']
    mine = [o for o in owners if o.tree == tree]                       # the tree: its main record and every sub-agent (a Codex environment names one thread of it; the tree is what a call of any node supports)
    if not mine or any(o.gap_at(run.ts - RUN_GRACE, run.ts + RUN_SLACK_BEFORE) for o in mine):
        return False
    if any(c.starts('claude') for _, c in _running(mine, child, run)):
        return False
    other = ChildFacts(child.sid, child.t0, child.cwd, child.entry, child.runs, False, child.unknown, child.cleared)
    ev, _, notes = run_evidence(other, run, k, [o for o in owners if o.tree != tree], out, content, (), up)
    return any(e[0] <= 3 and e[2] != tree for e in ev)


def decide(child, owners, pins=(), out=None, saved=None, content=True, up=None):
    """The Decision for one child. `owners`: [Owner]; `pins`: [{'kind': env|proc|file, 'tree': sid, 'ts': process start or None, 'node': the thread, 'exact': True}]
    (live process, environment, a remembered link: tree evidence of rank 2 for the run that process belongs to; `exact` is only for a Codex environment, which also
    says the node: the thread that ran the command, None for a root); `out`: an object with files_with(sid) and writers(path)
    (see _out_evidence); `saved`: a remembered {'tree', 'node'} used only when the records and the processes give no evidence at all (rule `cache`);
    `content`: False skips the text fingerprint (the quick first judgment). `up(tree)`: the ids above a tree through links that are certain (nearest first), for the blind test."""
    dec = Decision(child)
    rel = dec.relation
    runs = child.runs
    if not runs and child.t0 is not None:
        runs = [Instr(child.t0, 1, '')]                              # no instruction line was read: judge by the start time alone (nothing can veto)
    if not runs:
        return dec
    pinned = _pin_runs([p for p in pins if p.get('tree') != child.sid], runs)
    unknown = tuple(sorted({u for p in pins if p.get('unknown') for u in p['unknown']}))
    stale = []
    for k, run in enumerate(runs):
        kept = []
        for p in pinned.get(k, []):
            if p.get('kind') == 'env' and p.get('tree') and _stale_pin(child, run, k, p, owners, out, content, up):
                stale.append((k, p['tree']))
            else:
                kept.append(p)
        if k in pinned:
            pinned[k] = kept
    if stale or unknown:
        # an environment that is not about this run takes the claim that nothing of Codex can have started it with it (`codex_free`); a thread nothing is known of can have
        child = ChildFacts(child.sid, child.t0, child.cwd, child.entry, child.runs, child.codex_free and not stale, unknown, stale)
        dec.child = child
    dec.diags += [('evidence_conflict', {'other': t}) for t in sorted({t for _, t in stale} | set(unknown))]
    per_run = []
    for k, run in enumerate(runs):
        e, t, n = run_evidence(child, run, k, owners, out, content, {p['tree'] for p in pinned.get(k, []) if p.get('tree')}, up)
        per_run.append((e, t, n))
    evs, ties, notes = per_run[0]
    t_first = runs[0].ts
    pin_ev, pin_ties = _pin_evidence(pinned.get(0, []))
    first_ev = list(evs) + pin_ev
    ties = list(ties) + pin_ties
    exact = _exact(pinned.get(0, []))
    s = settle(first_ev, ties, exact)
    if s['tree'] is None and not s['held'] and saved and saved.get('tree') != child.sid and not unknown:
        first_ev = first_ev + [(2, 'cache', saved['tree'], saved.get('node'), None)]
        s = settle(first_ev, ties, exact)
        if s['tree'] is not None and saved.get('node') is not None and not s['node_known']:
            s['node'], s['node_known'], s['node_rank'] = saved['node'], True, 3         # the remembered node came from a firm rule
    if s['tree'] is not None and s['rank'] == 3 and s['kind'] == 'content' and _launched_elsewhere(owners, child, runs[0], s['tree'], s['node']):
        s['node'], s['node_known'], s['node_rank'], s['node_tie'], s['call'] = None, True, 3, True, None       # the tree is sure; the node is held
    unresolved = [r for _, _, n in per_run for r in n['unresolved']]
    dec.incomplete = notes['incomplete']
    dec.assumed = notes['assumed'] and s['kind'] == 'content_short'
    if any(n['incomplete'] for _, _, n in per_run):
        dec.diags.append(('fingerprint_incomplete', {}))
    if any(n['content_tie'] for _, _, n in per_run):
        dec.diags.append(('ambiguous_content', {}))
    if unresolved:
        dec.diags.append(('path_unresolved', {'n': len(unresolved)}))
    if s['held']:
        dec.held, dec.held_trees = 'ambiguous', tuple(s['held_trees'])
        if pin_ties and s['rank'] is None:
            dec.diags += [('evidence_conflict', {'other': t}) for t in sorted(s['held_trees'])]      # two providers' names in one environment, nothing says which is the parent
    dec.claims = _claims(s, notes)
    dec.calls, dec.run_claims, dec.run_ts, dec.run_ranks = [s['call']], [list(dec.claims)], [t_first], [s['rank']]
    if s['tree'] is not None:
        tree = s['tree']
        rel.tree, rel.node, dec.rank, dec.kind = tree, s['node'], s['rank'], s['kind']
        rel.rule = RULE_OF.get(s['kind'], s['kind'])
        dec.call = s['call']
        rel.call = s['call'].span.call_id if s['call'] is not None else None
        alive = any(o.tree == tree and o.node is not None and o.alive_at(t_first) for o in owners)
        node_certain = (s['node_known'] and s['node_rank'] <= 3 and not s['node_tie']) or (not s['node_known'] and not alive)
        rel.certain = {'tree': s['rank'] <= 3, 'node': bool(node_certain), 'call': s['call'] is not None and s['node_rank'] is not None and s['node_rank'] <= 3}
        if s['node_tie'] or (not s['node_known'] and alive):
            dec.diags.append(('node_unresolved', {}))
        for kind, other in sorted({(e[1], e[2]) for e in first_ev if e[0] <= 3 and e[2] != tree}):
            dec.diags.append(('content_author_differs' if kind in ('content', 'resume') else 'evidence_conflict', {'other': other}))
        if s['kind'] == 'content' and len(runs) <= 1 and not any(e[0] < 3 or e[1] == 'resume' for e in first_ev):
            dec.diags.append(('content_only', {}))                # the instruction text alone made it firm (no output file, no process, no id written in the call)
    # by: who handed each run its instruction. The first run's is the relation's; a later run is judged on its own evidence and its own process
    for k, (e, t, n) in enumerate(per_run):
        if k == 0:
            dec.by.append((s['tree'], s['node']) if s['tree'] is not None else None)
            continue
        pe_k, pt_k = _pin_evidence(pinned.get(k, []))
        ev_k = list(e) + pe_k
        sk = settle(ev_k, list(t) + pt_k, _exact(pinned.get(k, [])))
        ck = _claims(sk, n)
        dec.claims += ck
        dec.calls.append(sk['call'])
        dec.run_claims.append(ck)
        dec.run_ts.append(runs[k].ts)
        dec.run_ranks.append(sk['rank'])
        if sk['tree'] is None:
            dec.by.append(None)
            continue
        dec.by.append((sk['tree'], sk['node']))
        if sk['node_tie'] or (not sk['node_known'] and any(o.tree == sk['tree'] and o.node is not None and o.alive_at(runs[k].ts) for o in owners)):
            dec.diags.append(('node_unresolved', {'run': k}))
    linked = {b[0] for b in dec.by if b}
    for author in sorted({n['author'] for _, _, n in per_run if n['author']} - linked):
        if ('content_author_differs', {'other': author}) not in dec.diags:
            dec.diags.append(('content_author_differs', {'other': author}))      # the words are in that session's record, nothing says it launched the child
    rel.by = list(dec.by)
    return dec


# ---------------------------------------------------------------------------------------------------------------------
# counting launches against runs
# ---------------------------------------------------------------------------------------------------------------------
# A call can start only as many children as its launches allow, and a run was started by one launch. The judgment above says per run which calls could have
# started it (`Decision.run_claims`) or which one did (`Decision.calls`); this matches runs to launches so that each launch is given at most its count of runs.
# What it answers: which call each run most likely came from (when the evidence names none), and which launches no run was accounted to (the orphans).
# Readable launches with a known count come first, then the ones the reader could not place (tmux, xargs) and the loops of unknown count: a run that fits
# a call the reader understood is not given to a launch it could not read. The earliest run gets the earliest call that was running (a call that had ended
# within the grace is the last choice), and a later run can push an earlier one to another call when that gives a run a launch it would otherwise lack.
FREE = 1 << 30


def capacity(call):
    """(readable, other, open): how many children one call can start. `readable`: launches the command reader understood, with their known counts; `other`: launches
    it could not place (one each); `open`: a loop of unknown count (any number). A launch that asked for no session record starts no child that has a record."""
    kr = kw = 0
    unknown = False
    for L in call.launches:
        if not L.persist:
            continue
        if L.n >= UNKNOWN_COUNT:
            unknown = True
        elif L.reader:
            kr += L.n
        else:
            kw += L.n
    return kr, kw, unknown


class Assignment:
    """Result of assign_launches. `call_of[key]` the call of each run (the one the evidence named, else the pick, else None), `named` the keys whose call the
    evidence named, `filled(call)` how many runs are accounted to a call. `reach(call)` the calls that a run missing from `call` could instead be missing from:
    a run that fits two launches leaves one of them without a child, and which one is not known."""

    def __init__(self):
        self.call_of, self.named = {}, set()
        self._fill = collections.Counter()
        self._moves = {}                 # id(call) -> [id(call)]: a run on the second could move to the first (the first would take it, the second would then lack one)
        self._calls = {}

    def filled(self, call):
        return self._fill.get(id(call), 0)

    def reach(self, call):
        seen, todo = {id(call)}, [id(call)]
        while todo:
            for y in self._moves.get(todo.pop(), ()):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        out = [self._calls[i] for i in seen if i in self._calls]
        return out if id(call) in self._calls else out + [call]


def _order(cands, ts):
    """The calls a run may be given to, best first: those running at its start (earliest first), then the ones that ended within the grace."""
    def key(c):
        sp = c.span
        live = sp.start <= ts + RUN_SLACK_BEFORE and (sp.end is None or ts <= sp.end)
        return (0 if live else 1, sp.start, sp.call_id or '')
    return sorted(cands, key=key)


def _match(units, room, cands_of):
    """One round of capacity-aware bipartite matching (augmenting paths, the units tried in the given order). units: [index]; room: {id(call): free places};
    cands_of[index]: [call] in preference order. -> {id(call): [index]} (who got each call; an index is in at most one list)."""
    got = {}

    def place(u, seen):
        for c in cands_of[u]:                                        # a call with a free place first (best first), only then somebody is moved
            ci = id(c)
            if room.get(ci, 0) > 0 and len(got.get(ci, ())) < room[ci]:
                got.setdefault(ci, []).append(u)
                return True
        for c in cands_of[u]:
            ci = id(c)
            if ci in seen or room.get(ci, 0) <= 0:
                continue
            seen.add(ci)
            mine = got[ci]
            for v in list(mine):
                if place(v, seen):
                    mine.remove(v)
                    mine.append(u)
                    return True
        return False
    for u in units:
        place(u, set())
    return got


def assign_launches(units):
    """units: [(key, ts, named, cands)]: one per run of a child, `named` the Call the evidence names for it (or None), `cands` the calls it could have been
    started by (a run that is named has just that one). -> Assignment."""
    res = Assignment()
    room_a, room_b = {}, {}                                          # places left: readable launches, then the others
    for _, _, named, cands in units:
        for c in ([named] if named is not None else []) + list(cands):
            if id(c) not in res._calls:
                res._calls[id(c)] = c
                kr, kw, unknown = capacity(c)
                room_a[id(c)], room_b[id(c)] = kr, FREE if unknown else kw
    free = []
    for i, (key, ts, named, cands) in enumerate(units):
        if named is not None:
            res.call_of[key] = named
            res.named.add(key)
            res._fill[id(named)] += 1
            if room_a[id(named)] > 0:
                room_a[id(named)] -= 1
            elif room_b[id(named)] > 0:
                room_b[id(named)] -= 1
        else:
            res.call_of[key] = None
            if cands:
                free.append(i)
    free.sort(key=lambda i: (units[i][1], str(units[i][0])))
    cands_of = {i: _order(units[i][3], units[i][1]) for i in free}
    phase = {}
    rest = free
    for ph, room in ((1, room_a), (2, room_b)):
        got = _match(rest, room, cands_of)
        taken = set()
        for ci, lst in got.items():
            for u in lst:
                res.call_of[units[u][0]] = res._calls[ci]
                res._fill[ci] += 1
                phase[u] = (ph, ci)
                taken.add(u)
        for ci, lst in got.items():
            room[ci] = room.get(ci, 0) - len(lst)
        rest = [u for u in rest if u not in taken]
    # which launch a run could instead have been given to: from the call it was given to, to the others that fit it and had a place in that round
    caps_a = {i: capacity(c) for i, c in res._calls.items()}
    for u, (ph, ci) in phase.items():
        for c in cands_of[u]:
            xi = id(c)
            if xi != ci and (caps_a[xi][0] > 0 if ph == 1 else (caps_a[xi][1] > 0 or caps_a[xi][2])):
                res._moves.setdefault(xi, []).append(ci)
    return res
