"""Run and turn boundaries, error classification and the status judgment of one agent.

Everything here is pure: the collectors take parsed record lines (dicts), the judgments take facts and a process snapshot, and `now` is an argument. The same records
and the same snapshot always give the same verdict, so a restart of the board shows what it showed before. Nothing reads a file or a process (that is the wiring of
agents.py, sessions.py and views.py). Standard library only; Python 3.9 compatible.

  RunTracker       the lines of ONE record file (a main session, a sub-agent, a `claude -p` child) -> runs (process lifetimes), turns, the end of the last turn
  CodexTracker     the lines of one Codex rollout -> runs (one per turn)
  Ledger           the lines of the LAUNCHER's record -> what it knows about its children: completion notices, TaskStop calls, tool_use id -> backgroundTaskId
  facts_of         tracker + ledger + the link of the child -> RunFacts
  judge            RunFacts + Proc + now -> Verdict(status, reason, resets_at, diag)
  orch_state       the orchestrator's tracker + Proc + now -> OrchVerdict(working | idle | limit_wait, resets_at, auto, diag)
  group_limits     the limit stops with one reset time -> one notice

Run boundaries, the one definition. A run is one process lifetime of a record file.
  opens   at the first user or assistant line after a boundary (the start of the file, a `cost-state`, or for a sub-agent the coordinator's resume line). RunRec.start_ts is
          that line's time. Notices (task-notification), `queue-operation` lines, `cost-state` and system lines open nothing.
  closes  a `cost-state` line (RunRec.exited). A sub-agent writes none: its coordinator's resume line (origin coordinator) closes the open run (not `exited`) and opens the
          next one (start_kind 'coordinator'). A process that died without one (a crash, a kill, an old record) is found by its resume (RunTracker._resumed): a new sdk
          turn while the open run's last turn is in the middle of its work, or a second instruction in the record of a print process in the old format, closes that run (not
          `exited`) and opens the next one (start_kind 'process'); the resumed call then has a run to belong to.
  turns   the instruction lines of the run (turn_source). The first turn is the run's start instruction and normally the line that opened it, so the run and its first turn start
          together. Two ways the first line is not a plain instruction: a slash command (`claude -p "/init"`) records its expansion first; when that line is marked as an sdk turn
          it is the first turn with the instruction unknown (Turn.prompt_head '' and prompt_text(d) ''), and it opens the run, so the run starts when the command line was
          written and not at the expansion line after it. In an older record the command line is no marked turn: it still opens the run, and the run has no turns (unknown
          instruction at start_ts). A caller that needs the instruction of run k reads runs[k-1].turns[0] and treats a missing turn or an empty prompt_text as unknown.

Private formats. Every marker below is something Claude Code writes but never promised; the observed versions are in OBSERVED and FORMAT_WINDOWS.
A marker that is present is used, whatever the version. `format_drift` is only a known marker or field missing from a record whose version is inside the marker's window;
then the judgment falls back to the other evidence (the process, the launcher's notice). A version outside OBSERVED is not drift by itself (every Claude Code release would
raise it for every user), and neither is an unknown kind of line: a real session already has twenty or so.
"""

import functools
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .facts import ErrInfo, NODE_ID_RE, Run, Turn
from .util import STALL_SEC, TOOL_STALL_SEC, parse_records, parse_ts, strip_reminders

# ---------------------------------------------------------------------------------------------------------------------
# thresholds and private formats
# ---------------------------------------------------------------------------------------------------------------------
NOTE_SLACK = 5.0               # a completion notice or a TaskStop older than the last record by more than this belongs to an earlier run
ECHO_SEC = 5.0                 # an instruction line with the same words this close after another is that line written again, not a new instruction
PROC_SILENT_SEC = 60           # without a process view, a record that has been quiet this long no longer proves the agent is alive
CODEX_GONE_GRACE = 60          # a Codex rollout nobody has open is only called ended after this much silence (the process may not have opened it yet)
UNRESUMED_LIMIT_SEC = 600      # an interrupted limit stop is "not resumed" this long after its reset time
UNRESUMED_OTHER_SEC = 1800     # any other interrupted stop, this long after the run ended

OBSERVED = ('2.1.235', '2.1.286')    # the Claude Code versions the private formats were checked against (the window of `cost-state`); `out_of_range` says whether a version is outside them
FORMAT_WINDOWS = {             # marker -> (first, last) version it was observed in. A marker missing from a record whose version is inside its window is `format_drift`
    'cost-state': ('2.1.235', '2.1.286'),
    'quotaLimits': ('2.1.235', '2.1.286'),
    'limit_notice': ('2.1.280', '2.1.285'),
    'auto-continuation': ('2.1.280', '2.1.281'),
    'promptSource': ('2.1.284', '2.1.286'),
    'turnPosition': ('2.1.284', '2.1.286'),
    'session_status': ('2.1.270', '2.1.286'),
}

AGENT_PROMPT_MARK = '<task-notification>'


VERSION_RE = re.compile(r'([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,5})')      # a Claude Code version as a record writes it: three numbers (ASCII digits, nothing around them)


@functools.lru_cache(maxsize=512)
def _vkey(v):
    """(major, minor, patch) of a version string, or None for anything that is not three numbers. Remembered per string: a page asks it for every agent and there are a
    handful of distinct versions."""
    m = VERSION_RE.fullmatch(v) if isinstance(v, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def _key(v):
    return _vkey(v) if isinstance(v, str) else None           # a record can hold any JSON value there; only a string is looked at (and only a hashable one can be remembered)


def norm_version(v):
    """The version a record claims, as `major.minor.patch` numbers, or None when the value is not a version. A record is data from outside the board: its `version` text is
    never kept as written (it would reach a diagnostic)."""
    k = _key(v)
    return '%d.%d.%d' % k if k else None


def in_window(marker, version):
    """Whether a record of this Claude Code version is inside the window the marker was observed in: it is expected to carry the marker. An unknown version is not."""
    k = _key(version)
    lo, hi = _vkey(FORMAT_WINDOWS[marker][0]), _vkey(FORMAT_WINDOWS[marker][1])
    return k is not None and lo <= k <= hi


def out_of_range(version):
    """A version older or newer than anything the formats were checked against. Only a fact about the version: it is no `format_drift` by itself (see the module
    docstring), and unknown record kinds are not drift either (a real session has twenty or so)."""
    k = _key(version)
    return k is not None and not (_vkey(OBSERVED[0]) <= k <= _vkey(OBSERVED[1]))


# ---------------------------------------------------------------------------------------------------------------------
# lines
# ---------------------------------------------------------------------------------------------------------------------
def is_exit_marker(d):
    """The `cost-state` line a Claude Code process writes when it exits: the end of a run."""
    return d.get('type') == 'cost-state'


def is_agent_id(s):
    """A sub-agent id (`a` + 16 hex). A task id of any other shape in a notice is a Bash background task, never an agent."""
    return bool(s) and NODE_ID_RE.fullmatch(s) is not None


class Rec:
    """What several readers of one record line need from it, worked out once: the time, the content blocks and the instruction text. The board feeds one parsed
    line to the run tracker, the notice ledger, the agent's own bookkeeping and the token meter one after the other, and each used to parse the time and walk the
    content again (opening a big session did it four times per line). `rec(d)` gives the same Rec to all of them: the line they are handed one after the other is
    the last one made."""
    __slots__ = ('ts', 'blocks', '_text', '_t', '_c', '_notice', '_att')

    def __init__(self, d):
        self._t = d.get('timestamp')
        self.ts = parse_ts(self._t)
        c = self._c = (d.get('message') or {}).get('content')
        self.blocks = c if isinstance(c, list) else []
        self._text = None
        self._notice = _UNSET                              # notice_text(d), when it has been asked
        self._att = None

    def text(self, d):
        """The instruction text of the line with reminders stripped, '' for a line of tool results (see `prompt_text`)."""
        if self._text is None:
            c = (d.get('message') or {}).get('content')
            if isinstance(c, str):
                self._text = strip_reminders(c)
            elif isinstance(c, list):
                if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c):
                    self._text = ''
                else:
                    self._text = strip_reminders('\n'.join(b.get('text') or '' for b in c if isinstance(b, dict) and b.get('type') == 'text'))
            else:
                self._text = ''
        return self._text


_UNSET = object()
_LAST = (None, None)        # (the record dict, its Rec): one pair, replaced in one step so another thread never sees half of it


def rec(d):
    global _LAST
    last = _LAST
    if last[0] is d and last[1]._t is d.get('timestamp') and last[1]._c is (d.get('message') or {}).get('content'):      # the same dict, still holding the same time and content
        return last[1]
    r = Rec(d)
    _LAST = (d, r)
    return r


def _blocks(d):
    return rec(d).blocks


def _raw_text(d):
    return rec(d).text(d)


def is_command_text(text):
    """A slash command is recorded as its expansion (`<command-message>init is running</command-message><command-name>/init</command-name> ...`), which says
    nothing about the words the launcher typed."""
    return text.lstrip().startswith('<command-')


def is_local_echo(text):
    """What a session records when the user runs a command in the session itself (`/usage`, `/model`): the command, its output (`<local-command-stdout>`) and the caveat
    before them (`<local-command-caveat>`). Nothing answers them, so they start no work."""
    return is_command_text(text) or text.lstrip().startswith('<local-command-')


def is_work(d, rc=None):
    """Whether a line is the orchestrator at work or being given work: an assistant line, a line that starts a turn (`turn_source`: an instruction, the automatic continue
    after a limit, a `claude -p "/init"` command line), or a user line (a tool result, a notice) that is not meta, a compaction summary, a line only the transcript shows
    or the echo of a local command. A line that is not work does not start a turn, whatever its time."""
    typ = d.get('type')
    if typ == 'assistant':
        return True
    if typ != 'user':
        return False
    if turn_source(d) is not None:
        return True
    if d.get('isMeta') or d.get('isCompactSummary') or d.get('isVisibleInTranscriptOnly'):
        return False
    return not is_local_echo((rc or rec(d)).text(d))


def ends_turn(d):
    """Whether an assistant line closes the turn: its closing text with stop_reason end_turn (`assistant_end`), not the synthetic line of an API error. A `claude -p`
    record has no `turn_duration`: this line is the only mark of the end."""
    return d.get('type') == 'assistant' and not d.get('isApiErrorMessage') and assistant_end(d) == 'end_turn'


TOGETHER_SEC = 1.0                 # a notice written this soon after the end of a turn was written together with it


class TurnMarks:
    """Whether the orchestrator is in the middle of a turn, kept from its lines in the order they come: a line of work opens the turn, the end of a turn closes it, and what
    stands last decides. The time between two lines decides nothing, so a turn that starts at once after the end of the one before is open, with its first call still waiting."""
    __slots__ = ('open', 'end_ts')

    def __init__(self):
        self.open = False
        self.end_ts = None                                 # the time of the last end that had one: what a notice is measured from

    def begin(self):
        self.open = True

    def end(self, ts=None):
        self.open = False
        if ts:
            self.end_ts = max(self.end_ts or 0.0, ts)

    def notice(self, ts):
        """A notice (a finished background task) that no instruction or call goes with: it wakes the orchestrator, unless it was written together with the end of the turn,
        when what the orchestrator does about it is the work that opens the next one."""
        if not (ts and self.end_ts and ts <= self.end_ts + TOGETHER_SEC):
            self.open = True

    def feed_line(self, d, ts, rc=None):
        """One line of a Claude record (see `is_work` for the lines that count)."""
        typ = d.get('type')
        if typ not in ('assistant', 'user') or not is_work(d, rc):
            return
        if typ == 'assistant':
            if ends_turn(d):
                self.end(ts)
            elif d.get('isApiErrorMessage') or assistant_end(d) == 'mid_turn':
                self.begin()                               # a line with no text and no call (the rest of a message that closed the turn) opens nothing
        elif turn_source(d) is not None or any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in (rc or rec(d)).blocks):
            self.begin()                                   # an instruction, or what the orchestrator goes on with after a call
        else:
            self.notice(ts)


def turn_open(marks, proc=None):
    """Whether the orchestrator is in the middle of a turn (`TurnMarks`) and its process is not known to be gone. An unseen process (alive None) leaves it to the record."""
    return not (proc is not None and proc.alive is False) and marks.open


def prompt_text(d):
    """The instruction text of a user line (reminders stripped). '' for a line that is only tool results and for a slash command's expansion: that instruction
    is unknown (it vetoes nothing and cannot be matched by content)."""
    t = _raw_text(d)
    return '' if is_command_text(t) else t


def turn_source(d):
    """The source of an instruction line or None when the line is not an instruction.
    sdk     promptSource == "sdk" (a `claude -p` run starts with one; the line also carries turnPosition.turnIndex). One step wider than that: a line with a
            turnPosition.turnIndex and no promptSource counts too (seen once in real records); a caller that wants promptSource == "sdk" only checks d.get('promptSource') itself
    user    a person typed it (promptSource typed/queued/..., origin human)
    system  the automatic continue after a usage limit (origin auto-continuation), or the coordinator's message that resumes a sub-agent (origin coordinator)
    legacy  older records: string content that is not meta and does not start with `<`
    A slash command (`claude -p "/init"`) starts its run with the expansion line `<command-message>...<command-name>/init</command-name>`: when that line is marked as
    an sdk turn (promptSource "sdk", or a turnPosition) it is an `sdk` turn whose instruction is unknown (prompt_head and prompt_text are ''). It opens the run
    like any other first line, so the run starts when the line was written, not at the expansion that follows it. Unmarked (an older record, a command typed in an
    interactive session) it is no turn.
    Not an instruction: notices (task-notification), cross-session messages (peer), compaction summaries, meta lines, tool results, text that starts with `<`."""
    if d.get('type') != 'user' or d.get('isCompactSummary') or d.get('isVisibleInTranscriptOnly'):
        return None
    kind = (d.get('origin') or {}).get('kind')
    if kind in ('auto-continuation', 'coordinator'):
        return 'system'
    if kind and kind != 'human':
        return None
    if d.get('isMeta'):
        return None
    text = _raw_text(d)
    if not text:
        return None
    src = d.get('promptSource')
    marked = isinstance(d.get('turnPosition'), dict) and d['turnPosition'].get('turnIndex') is not None
    if text.lstrip().startswith('<'):
        return 'sdk' if is_command_text(text) and (src == 'sdk' or (src is None and marked)) else None
    if src == 'sdk':
        return 'sdk'
    if src == 'system':
        return None
    if kind == 'human' or src in ('typed', 'queued', 'suggestion_accepted'):
        return 'user'
    if marked:
        return 'sdk'
    return 'legacy'


def is_resume_line(d):
    """The coordinator's message that resumes a sub-agent: it opens a new run of that sub-agent. A sub-agent has no cost-state, so this line is the only boundary."""
    return d.get('type') == 'user' and (d.get('origin') or {}).get('kind') == 'coordinator'


def turn_of(d, sid=None):
    """The Turn an instruction line starts, or None (turn_source says which lines). `idx` is turnPosition.turnIndex, 0 when the line has none."""
    src = turn_source(d)
    if src is None:
        return None
    tp = d.get('turnPosition')
    idx = tp.get('turnIndex') if isinstance(tp, dict) else None
    return Turn(sid=sid or d.get('sessionId') or '', idx=idx if isinstance(idx, int) else 0, ts=rec(d).ts or 0.0, source=src,
                prompt_head=prompt_text(d)[:80])


def error_of(d):
    """The ErrInfo of an API error line (a synthetic assistant line with isApiErrorMessage), or None. resets_at is the epoch seconds of quotaLimits.resetsAt, or None."""
    if not d.get('isApiErrorMessage') or d.get('type') != 'assistant':
        return None
    q = d.get('quotaLimits')
    r = q.get('resetsAt') if isinstance(q, dict) else None
    if isinstance(r, bool) or not isinstance(r, (int, float)):
        r = None
    elif r > 10 ** 11:
        r = r / 1000.0                                     # milliseconds
    st = d.get('apiErrorStatus')
    return ErrInfo(status=st if isinstance(st, int) and not isinstance(st, bool) else None, type=d.get('error') if isinstance(d.get('error'), str) else None,
                   resets_at=r)


def classify_error(err):
    """(status, reason) of an API error. The HTTP status decides first (429, 529, 400 ...), the error name only when there is no status.
    A usage limit and a server-side hiccup stop the work but are expected to pass: `interrupted`. A request the server refuses (400, authentication) will not pass
    by itself: `failed`. Both carry the reason `api_error` unless it is a limit. Real names: `rate_limit` (429), `server_error` (529), `invalid_request` (400)."""
    typ = (err.type or '').lower() if err else ''
    st = err.status if err else None
    if st is not None:
        if st == 429:
            return 'interrupted', 'limit'
        if st >= 500:
            return 'interrupted', 'api_error'
        return 'failed', 'api_error'
    if 'rate_limit' in typ or 'usage_limit' in typ:
        return 'interrupted', 'limit'
    if 'overload' in typ or typ in ('server_error', 'api_error', 'internal_server_error'):
        return 'interrupted', 'api_error'
    return 'failed', 'api_error'


def assistant_end(d):
    """What an assistant line says about the end of the turn: `mid_turn` (it asks for a tool, or stops without an end), `end_turn` (a text that closes the turn), or
    None (a line that says nothing: a thinking block of a message whose text comes next, a synthetic stop)."""
    m = d.get('message') or {}
    blocks = [b for b in _blocks(d) if isinstance(b, dict)]
    if any(b.get('type') == 'tool_use' for b in blocks):
        return 'mid_turn'
    stop = m.get('stop_reason')
    if stop == 'end_turn':
        return 'end_turn' if any(b.get('type') == 'text' and (b.get('text') or '').strip() for b in blocks) else None
    if stop in (None, 'tool_use', 'max_tokens', 'pause_turn') and blocks:
        return 'mid_turn'
    return None


def notice_kind(d):
    """`auto` (usage limit reached, continuing automatically), `reset` (usage limit reset) or `limit` (a limit notice without an automatic continue) for a
    system/informational line; None for anything else. The notice never carries the reset time as a date, so none is read from it."""
    if d.get('type') != 'system' or d.get('subtype') != 'informational':
        return None
    t = d.get('content')
    t = t.lower() if isinstance(t, str) else ''
    if 'usage limit reset' in t:
        return 'reset'
    if 'usage limit reached' in t:
        return 'auto' if 'continuing automatically' in t else 'limit'
    return None


@dataclass
class Note:
    """One completion notice (task-notification) or one foreground Agent result."""
    ts: float
    task: Optional[str]                                    # an agent id, or the id of a Bash background task
    tool_use_id: Optional[str]
    status: str                                            # completed | failed | killed (as written)
    summary: str = ''
    err_status: Optional[int] = None                       # "(error type X, HTTP N)" at the end of a failed summary
    err_type: Optional[str] = None
    reason: Optional[str] = None                           # time_limit | stopped | None
    exit_code: Optional[int] = None
    usage: Optional[Tuple[int, int, int]] = None           # (tokens, tool uses, duration ms) of the agent at that notice: tells two notices with the same words apart

    @property
    def is_agent(self):
        return is_agent_id(self.task)


_NOTE_ERR_RE = re.compile(r'error type ([A-Za-z_]+), HTTP (\d{3})')
_NOTE_EXIT_RE = re.compile(r'exit code (-?\d+)')
_NOTE_USAGE_RE = re.compile(r'<subagent_tokens>(\d+)</subagent_tokens>.*?<tool_uses>(\d+)</tool_uses>.*?<duration_ms>(\d+)</duration_ms>', re.S)
NOTE_DUP_SEC = 120.0           # two notices with the same words and no usage to tell them apart are one notice recorded twice when they are this close


def _note_error(summary):
    """(http status, error type) from "(error type X, HTTP N)" of a failed notice, or (None, None)."""
    m = _NOTE_ERR_RE.search(summary or '')
    return (int(m.group(2)), m.group(1)) if m else (None, None)


def parse_note(text, ts=0.0):
    """The Note of a task-notification text (only the tags at its head are read), or None when the text has no task id."""
    head = (text or '')[:4000]
    tag = lambda t: (re.search(r'<%s>([^<]*)</%s>' % (t, t), head) or [None, None])[1]
    task = tag('task-id')
    if not task:
        return None
    status, summary = tag('status') or '', tag('summary') or ''
    st, typ = _note_error(summary)
    code = _NOTE_EXIT_RE.search(summary)
    low = summary.lower()
    reason = 'time_limit' if 'background time limit' in low else ('stopped' if status == 'killed' else None)
    use = _NOTE_USAGE_RE.search(text or '')
    return Note(ts=ts, task=task, tool_use_id=tag('tool-use-id'), status=status, summary=summary, err_status=st, err_type=typ, reason=reason,
                exit_code=int(code.group(1)) if code else None, usage=tuple(int(x) for x in use.groups()) if use else None)


def notice_text(d):
    """(text, ts) of a task-notification carried by a line (a user line, or a queued_command attachment), or None. The tracker and the ledger both ask it of every
    line; the answer is kept on the line's Rec."""
    return _notice_of(d, rec(d))


def _notice_of(d, r):
    if r._notice is not _UNSET and r._att is d.get('attachment'):
        return r._notice
    r._notice, r._att = _notice_text(d, r.ts), d.get('attachment')
    return r._notice


def _notice_text(d, ts):
    typ = d.get('type')
    if typ == 'attachment':
        a = d.get('attachment') or {}
        if a.get('type') == 'queued_command' and a.get('commandMode') == 'task-notification':
            p = a.get('prompt')
            p = p if isinstance(p, str) else '\n'.join(b.get('text') or '' for b in p if isinstance(b, dict)) if isinstance(p, list) else ''
            return p, parse_ts(a.get('timestamp')) or ts
    elif typ == 'user' and (d.get('origin') or {}).get('kind') in ('task-notification', None):
        c = (d.get('message') or {}).get('content')
        if isinstance(c, str) and c.lstrip().startswith(AGENT_PROMPT_MARK):
            return c, ts
        if isinstance(c, list):
            t = '\n'.join(b.get('text') or '' for b in c if isinstance(b, dict) and b.get('type') == 'text')
            if t.lstrip().startswith(AGENT_PROMPT_MARK):
                return t, ts
    return None


# ---------------------------------------------------------------------------------------------------------------------
# runs of one record file
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class RunRec(Run):
    """A facts.Run plus what the judgment needs from the same lines."""
    start_ts: Optional[float] = None
    last_ts: Optional[float] = None
    err_ts: Optional[float] = None                         # the last API error line while end_kind == 'error'
    streak_ts: Optional[float] = None                      # the first error line of the errors in a row
    auto_ts: Optional[float] = None                        # the last "continuing automatically" notice
    reset_ts: Optional[float] = None                       # the last "usage limit reset" notice
    total_ms: Optional[int] = None                         # `totalDuration` of the cost-state that closed it
    version: Optional[str] = None
    aborted: bool = False                                  # Codex: the turn was aborted without an end record
    start_kind: str = 'first'                              # first (the first run of the file) | process (after a cost-state) | coordinator (a sub-agent resumed by its coordinator)


class RunTracker:
    """The lines of ONE Claude record file, fed in order. `runs` are the process lifetimes (a `cost-state` line closes one; the next instruction opens the next),
    `cur` the open one. A tracker built from the same lines is always in the same state, so a restart of the board rebuilds it by feeding the file again."""

    def __init__(self, sid=None):
        self.sid = sid
        self.runs = []                                     # List[RunRec]
        self.cur = None                                    # the open run, or None
        self.last_ts = None                                # the last assistant/user line
        self.turn = TurnMarks()                            # whether the orchestrator is in the middle of a turn: a line of work opens it; turn_duration, an assistant line that ends the turn (ends_turn) and the exit marker close it
        self.pending = {}                                  # tool_use id -> time of a call without a result
        self.version = None
        self._vraw = None                                  # the last `version` value a line carried, as written (not kept anywhere else)
        self.entrypoint = None
        self.torn = 0                                      # lines recovered from a torn line
        self.lost = 0                                      # lines that could not be read at all
        self.stray = 0                                     # task-notifications of Bash background tasks found in this file
        self.drift = []                                    # [(kind, name)]: marker_missing:<marker>, invalid:version
        self.prev_total = 0
        self._seen_marker = set()

    # ---- input ----
    def feed_raw(self, raw):
        """One line of the file as bytes: parsed with torn-line recovery and counted. Returns the number of records fed."""
        recs, torn = parse_records(raw)
        if torn:
            if recs:
                self.torn += 1
            else:
                self.lost += 1
        for d in recs:
            self.feed(d)
        return len(recs)

    def feed(self, d, rc=None):
        """One parsed record line. `rc`: the line's Rec when the caller already has it (the board feeds the same line to several readers)."""
        if not isinstance(d, dict):
            return
        typ = d.get('type')
        rc = rc or rec(d)
        ts = rc.ts
        self._meta(d, typ)
        if is_exit_marker(d):
            return self._exit(d)
        self.turn.feed_line(d, ts, rc)
        t = turn_of(d, self.sid)                           # the one place that decides which lines start a turn (and so a run)
        if t is not None:
            kind, side = None, bool(d.get('isSidechain')) or 'agentId' in d
            if is_resume_line(d) and self.cur is not None and self.cur.turns:
                self.cur.end_ts, self.cur, kind = self.cur.last_ts, None, 'coordinator'     # a sub-agent has no cost-state: its coordinator's message is the boundary
            elif self._resumed(t, side):
                self.cur.end_ts, self.cur, kind = self.cur.last_ts, None, 'process'         # the process that wrote the turn before is gone and this is its resume
            r = self._touch(self.cur or self._open(ts, kind), ts)
            return self._turn(r, t, side)
        if typ == 'system':
            return self._system(d, ts)
        n = _notice_of(d, rc) if typ in ('user', 'attachment') else None
        if n is not None:
            return self._notice(n[0])
        if typ not in ('assistant', 'user'):
            return
        r = self._touch(self.cur or self._open(ts), ts)
        if typ == 'assistant':
            self._assistant(r, d, ts, rc.blocks)
        else:
            for b in rc.blocks:
                if isinstance(b, dict) and b.get('type') == 'tool_result':
                    self.pending.pop(b.get('tool_use_id'), None)
                    r.end_kind = 'mid_turn'

    # ---- pieces ----
    def _meta(self, d, typ):
        if not self.sid and isinstance(d.get('sessionId'), str):
            self.sid = d['sessionId']
        v = d.get('version')
        if v is not None and v != '' and v != self._vraw:      # nearly every line says the same version: the value is looked at again only when it changes
            self._vraw = v
            nv = norm_version(v)
            if nv is not None:
                self.version = nv
            elif ('invalid', 'version') not in self.drift:
                self.drift.append(('invalid', 'version'))      # the value is not a version: the kept one stays, and nothing of the value is kept
        if isinstance(d.get('entrypoint'), str):
            self.entrypoint = d['entrypoint']

    def _touch(self, r, ts):
        if ts:
            self.last_ts = max(self.last_ts or 0.0, ts)
            r.last_ts = max(r.last_ts or 0.0, ts)
        return r

    def _notice(self, text):
        """A task-notification found in this file: one of a Bash background task (not an agent) is the agent's own and says nothing about a child."""
        note = parse_note(text)
        if note is not None and not note.is_agent:
            self.stray += 1

    def _resumed(self, t, side):
        """Whether the turn `t` is the resume of a process that died without a `cost-state` (a crash, a kill, an old record that has none). Only the record's own evidence
        is used: the launcher's notice is in another file and may not be read yet.
        sdk     a new turn of the sdk while the open run's last turn is in the middle of its work (it asked for a tool, or a tool result is waiting for the next step). A process
                takes the next prompt only after it has ended the turn, so the one that wrote that turn is gone. A clean end of the turn does not count (that could be the
                same process taking another prompt), nor does an error line (see `_turn`), nor a turn nothing has answered yet: a reader that is fed no assistant lines never
                sees an answer, and has to give the same runs.
        legacy  an instruction in the record of a print process (entrypoint sdk-cli) that already has one: such a process takes one prompt, and an old record has no marker
                of any other kind. Not a sub-agent's line, and not the same line written twice."""
        r = self.cur
        if r is None or not r.turns:
            return False
        if t.source == 'sdk':
            return bool(t.idx) and r.end_kind == 'mid_turn' and not any(x.source == 'sdk' and x.idx == t.idx for x in r.turns)     # the same index again is the line written twice
        if t.source == 'legacy' and self.entrypoint == 'sdk-cli' and not side:
            return not any(x.prompt_head == t.prompt_head and abs(x.ts - t.ts) < ECHO_SEC for x in r.turns)
        return False

    def _open(self, ts, kind=None):
        if self.runs and kind != 'coordinator':
            self.pending.clear()                           # a call of the process that is gone never gets its result (a coordinator's message resumes the same process)
        r = RunRec(sid=self.sid or '', epoch=len(self.runs) + 1, start_ts=ts, version=self.version,
                   start_kind=kind or ('process' if self.runs and self.runs[-1].exited else 'first'))
        self.runs.append(r)
        self.cur = r
        return r

    def _exit(self, d):
        total = d.get('totalDuration')
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        if total is None:
            self._lost('cost-state', 'cost-state.totalDuration')           # the line is there but a field the judgment reads is gone
        self.turn.end()                                                    # the process that was in the middle of a turn has exited: nothing is left to do
        r = self.cur
        if r is not None:
            r.exited, r.end_ts, r.total_ms = True, r.last_ts, total
            if total is not None:
                r.dur_ms = total - self.prev_total if total >= self.prev_total else total
            self.cur = None
        if total is not None:
            self.prev_total = total

    def _system(self, d, ts):
        if d.get('subtype') == 'turn_duration':
            self.turn.end(ts)
            return
        k = notice_kind(d)
        r = self.cur
        if k and r is not None and ts:
            if k == 'reset':
                r.reset_ts = ts
            else:
                r.auto_ts = ts if k == 'auto' else r.auto_ts

    def _assistant(self, r, d, ts, blocks=None):
        err = error_of(d) if d.get('isApiErrorMessage') else None
        if err is not None:
            if isinstance(d.get('quotaLimits'), dict) and err.resets_at is None:
                self._lost('quotaLimits', 'quotaLimits.resetsAt')
            if r.end_kind != 'error':
                r.streak_ts = ts
            r.end_kind, r.err, r.err_ts = 'error', err, ts
            return
        tool = False
        for b in (_blocks(d) if blocks is None else blocks):
            if isinstance(b, dict) and b.get('type') == 'tool_use':
                tool = True
                if b.get('id'):
                    self.pending[b['id']] = ts
        k = 'mid_turn' if tool else assistant_end(d)       # a line that asks for a tool is a turn in the middle (assistant_end says the same, without a second walk)
        if k:
            r.end_kind, r.err, r.err_ts, r.streak_ts = k, None, None, None

    def _turn(self, r, t, side=False):
        """`side`: the line is a sub-agent's (a `claude -p` parent's sub-agents also say entrypoint sdk-cli, and their first line has no promptSource)."""
        if t.source == 'sdk' and t.idx and any(x.source == 'sdk' and x.idx == t.idx for x in r.turns):
            return                                         # the same turn written again
        if t.source == 'sdk' and not t.idx:
            self._lost('turnPosition', 'turnPosition')
        if t.source == 'legacy' and self.entrypoint == 'sdk-cli' and not side:
            self._lost('promptSource', 'promptSource')
        if not t.idx:
            t.idx = len(r.turns) + 1
        r.turns.append(t)
        r.end_kind, r.err, r.err_ts, r.streak_ts = 'none', None, None, None

    def _lost(self, marker, name):
        """A marker the version of this record should carry is not there."""
        if in_window(marker, self.version) and ('marker_missing', name) not in self.drift:
            self.drift.append(('marker_missing', name))

    # ---- output ----
    def waiting(self):
        """Start times of the tool calls that have no result yet."""
        return [t for t in self.pending.values() if t]


# ---------------------------------------------------------------------------------------------------------------------
# runs of one Codex rollout
# ---------------------------------------------------------------------------------------------------------------------
class CodexTracker:
    """The lines of one Codex rollout. A Codex process lives for one `exec` call, so a run is one turn: task_started opens it, task_complete (with or without an
    error) or turn_aborted closes it, a new task_started without either closes the old one as aborted."""

    def __init__(self, sid=None):
        self.sid = sid
        self.runs = []
        self.cur = None
        self.last_ts = None
        self.pending = {}
        self.stray = 0
        self.torn = 0
        self.lost = 0
        self.drift = []
        self.version = None
        self.entrypoint = None

    def feed_cx(self, ts, typ, pt, p):
        """One decoded rollout line: time, line type, payload type, payload dict (None when the line was too long to decode)."""
        if typ == 'session_meta':
            return
        if ts:
            self.last_ts = max(self.last_ts or 0.0, ts)
            if self.cur is not None:
                self.cur.last_ts = self.last_ts
        p = p or {}
        if typ == 'event_msg':
            if pt == 'task_started':
                self._start(ts)
            elif pt in ('task_complete', 'turn_aborted') and self.cur is not None:
                r = self.cur
                r.exited, r.end_ts = True, ts
                if pt == 'turn_aborted':
                    r.aborted, r.end_kind = True, 'none'
                elif p.get('error'):
                    r.end_kind, r.err = 'error', codex_error(p['error'])
                else:
                    r.end_kind = 'end_turn'
                self.cur = None
        elif typ == 'response_item':
            if pt in ('function_call', 'custom_tool_call') and p.get('call_id'):
                self.pending[p['call_id']] = ts
            elif pt in ('function_call_output', 'custom_tool_call_output'):
                self.pending.pop(p.get('call_id'), None)
            elif pt == 'message' and p.get('role') == 'user' and self.cur is not None and not self.cur.turns:
                text = '\n'.join(c.get('text') or '' for c in p.get('content') or [] if isinstance(c, dict))
                self.cur.turns.append(Turn(sid=self.sid or '', idx=1, ts=ts or 0.0, source='sdk', prompt_head=text.strip()[:80]))

    def feed(self, d):
        """One rollout line as parsed JSON."""
        if isinstance(d, dict):
            p = d.get('payload') if isinstance(d.get('payload'), dict) else {}
            self.feed_cx(parse_ts(d.get('timestamp')), d.get('type'), p.get('type'), p)

    def _start(self, ts):
        if self.cur is not None:                           # the next turn came without an end record: the process was stopped
            self.cur.exited, self.cur.aborted, self.cur.end_ts = True, True, self.cur.last_ts
        r = RunRec(sid=self.sid or '', epoch=len(self.runs) + 1, start_ts=ts, last_ts=ts)
        self.runs.append(r)
        self.cur = r

    def waiting(self):
        return [t for t in self.pending.values() if t]


def codex_error(err):
    """The ErrInfo of the `error` of a Codex task_complete: {codex_error_info, message}. The info is a name (server_overloaded, other ...) or a dict keyed by the name."""
    if not isinstance(err, dict):
        return ErrInfo(type=str(err)[:60] if err else None)
    info = err.get('codex_error_info')
    status = None
    if isinstance(info, dict):
        name = next(iter(info), None)
        inner = info.get(name) if name else None
        if isinstance(inner, dict):
            status = inner.get('http_status_code')
        info = name
    typ = info if isinstance(info, str) else None
    if status is None and typ in (None, 'other'):
        m = re.search(r'\b([45]\d\d)\b', str(err.get('message') or ''))
        status = int(m.group(1)) if m else None
    return ErrInfo(status=status if isinstance(status, int) else None, type=typ)


# ---------------------------------------------------------------------------------------------------------------------
# what the launcher's record knows about its children
# ---------------------------------------------------------------------------------------------------------------------
class Ledger:
    """The lines of the record of whoever launches children (the main session, or a sub-agent that starts agents or `claude -p` children):
    completion notices, TaskStop calls and the chain  tool_use id -> toolUseResult.backgroundTaskId -> notice of that task."""

    def __init__(self):
        self.bg_of_call = {}                               # Bash tool_use id -> backgroundTaskId
        self.agent_calls = {}                              # Agent tool_use id -> time (a foreground result of one is a completion)
        self.notes = []                                    # [Note]
        self.stops = []                                    # [(ts, task id)] TaskStop calls
        self._seen = {}                                    # notice key -> times it was added at

    def feed_raw(self, raw):
        for d in parse_records(raw)[0]:
            self.feed(d)

    def feed(self, d, rc=None):
        if not isinstance(d, dict):
            return
        typ = d.get('type')
        rc = rc or rec(d)
        ts = rc.ts
        if typ == 'assistant':
            for b in rc.blocks:
                if not isinstance(b, dict) or b.get('type') != 'tool_use':
                    continue
                inp = b.get('input') if isinstance(b.get('input'), dict) else {}
                if b.get('name') == 'TaskStop':
                    tid = inp.get('task_id') or inp.get('shell_id')
                    if tid:
                        self.stops.append((ts, tid))
                elif b.get('name') == 'Agent' and b.get('id'):
                    self.agent_calls[b['id']] = ts
            return
        n = _notice_of(d, rc)
        if n:
            note = parse_note(n[0], n[1] or ts or 0.0)
            if note:
                if note.usage is None:
                    a = d.get('attachment') or {}
                    u = a.get('usage') if isinstance(a.get('usage'), dict) else {}
                    if all(isinstance(u.get(k), int) for k in ('totalTokens', 'toolUses', 'durationMs')):
                        note.usage = (u['totalTokens'], u['toolUses'], u['durationMs'])
                self._add(note)
            return
        if typ == 'user':
            tur = d.get('toolUseResult') if isinstance(d.get('toolUseResult'), dict) else {}
            for b in rc.blocks:
                if not isinstance(b, dict) or b.get('type') != 'tool_result':
                    continue
                call = b.get('tool_use_id')
                if tur.get('backgroundTaskId') and call:
                    self.bg_of_call[call] = tur['backgroundTaskId']
                if call in self.agent_calls:
                    self._agent_result(call, b, tur, ts)

    def _agent_result(self, call, block, tur, ts):
        """A foreground result of an Agent call: the confirmation that it started in the background is not a completion."""
        if tur.get('isAsync') or tur.get('status') == 'async_launched':
            return
        st = 'failed' if block.get('is_error') else tur.get('status') if tur.get('status') in ('completed', 'failed', 'killed') else None
        if st:
            self._add(Note(ts=ts or 0.0, task=tur.get('agentId'), tool_use_id=call, status=st, reason='stopped' if st == 'killed' else None))

    def _add(self, note):
        """One notice is recorded twice (a queued line and the delivered line); an agent that is resumed and ends again says the same words a second time and that
        second notice is the one that says it is over. So two notices are one when they match in task, call, status, words and usage, and, when there is no
        usage to tell them apart, came within NOTE_DUP_SEC of each other."""
        key = (note.task, note.tool_use_id, note.status, note.summary, note.usage)
        seen = self._seen.setdefault(key, [])
        if seen and (note.usage is not None or any(abs(note.ts - t) <= NOTE_DUP_SEC for t in seen)):
            return
        seen.append(note.ts)
        self.notes.append(note)

    # ---- queries ----
    def bg_ids(self, calls):
        """The background task ids of the given launch calls (a call that is not a background call has none)."""
        return {self.bg_of_call[c] for c in calls if c in self.bg_of_call}

    def agent_notes(self, agent_id, tool_use_id=None):
        return [n for n in self.notes if (n.task and n.task == agent_id) or (tool_use_id and n.tool_use_id == tool_use_id and not n.task)]

    def bg_notes(self, task_ids):
        return [n for n in self.notes if n.task in task_ids]

    def stops_of(self, task_ids):
        return [ts for ts, t in self.stops if t in task_ids and ts is not None]

    def strays(self):
        """Notices whose task is neither an agent nor a Bash background task this record started."""
        known = set(self.bg_of_call.values())
        return [n for n in self.notes if not n.is_agent and n.task not in known]


# ---------------------------------------------------------------------------------------------------------------------
# facts and the judgment
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Proc:
    """What the process table says about the agent (the wiring builds it; proc_snapshot picks it from several candidates).
    alive  True / False / None (the board cannot see processes). For a sub-agent: the session's process (a sub-agent has none of its own)."""
    alive: Optional[bool]
    pids: Tuple[int, ...] = ()
    status: Optional[str] = None                           # busy | idle | shell, from the sessions file (a hint, never the verdict)
    status_ts: Optional[float] = None


def proc_snapshot(entries):
    """One Proc from every process that holds the session: entries = [(pid, alive True/False/None, status, status_ts)].
    Alive if any is alive; unknown if none is alive and some is unknown; gone if all are gone. With several live ones the busy one gives the status, else the latest."""
    if not entries:
        return Proc(False)
    live = [e for e in entries if e[1]]
    if live:
        busy = [e for e in live if e[2] == 'busy']
        pick = busy[0] if busy else max(live, key=lambda e: e[3] or 0.0)
        return Proc(True, tuple(e[0] for e in live), pick[2], pick[3])
    return Proc(None if any(e[1] is None for e in entries) else False)


@dataclass
class RunFacts:
    """Everything `judge` reads about one agent."""
    kind: str                                              # cli | subagent | codex
    runs: List[RunRec] = field(default_factory=list)
    last_ts: Optional[float] = None
    spawn_ts: Optional[float] = None
    pending: List[float] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)        # completion notices of this child (its agent id, or the Bash tasks that launched it)
    stops: List[float] = field(default_factory=list)       # TaskStop calls on it
    handbacks: List[float] = field(default_factory=list)   # times of its final report as seen by the launcher
    parent_over: bool = False                              # sub-agent: its parent agent is over
    torn: int = 0
    lost: int = 0
    stray: int = 0
    drift: List[Tuple[str, str]] = field(default_factory=list)
    version: Optional[str] = None
    entrypoint: Optional[str] = None


def facts_of(tracker, kind, ledger=None, agent_id=None, tool_use_id=None, launch_calls=(), handbacks=(), parent_over=False, spawn_ts=None):
    """RunFacts of one child from its own tracker and its launcher's Ledger.
    sub-agent: the notices and TaskStop calls are looked up by its agent id (and the tool_use id of its Agent call for a foreground result).
    claude -p / Codex child: by the Bash background tasks of the calls that launched it (`launch_calls`: the first call and every resume call the link knows)."""
    f = RunFacts(kind=kind, runs=list(tracker.runs), last_ts=tracker.last_ts, spawn_ts=spawn_ts, pending=tracker.waiting(), handbacks=list(handbacks),
                 parent_over=parent_over, torn=tracker.torn, lost=tracker.lost, stray=tracker.stray, drift=list(tracker.drift), version=tracker.version,
                 entrypoint=tracker.entrypoint)
    if ledger is not None:
        if kind == 'subagent':
            ids = {agent_id} if agent_id else set()
            f.notes = ledger.agent_notes(agent_id, tool_use_id)
        else:
            ids = ledger.bg_ids(launch_calls)
            f.notes = ledger.bg_notes(ids)
        f.stops = ledger.stops_of(ids)
    return f


@dataclass
class Verdict:
    status: str                                            # facts.STATUSES
    reason: Optional[str] = None                           # facts.REASONS or None
    resets_at: Optional[float] = None                      # epoch seconds, only for a usage limit and only when the record has it
    diag: Tuple[Tuple[str, dict], ...] = ()                # state diagnostics: (code, params) with codes of facts.DIAG_STATE
    basis: str = ''                                        # which evidence decided it (error_line, exit_marker, notice, stop, process, silence ...)

    def codes(self):
        return {c for c, _ in self.diag}


def _alive_status(f, proc, last, now):
    """Status of an agent that has no ending record and is not known to be gone. Without a process view (proc.alive None) a record that has been quiet for
    PROC_SILENT_SEC is `unknown` (it may be dead or only quiet), except for a sub-agent, whose life does not depend on a process of its own."""
    if proc.alive is None and f.kind != 'subagent' and now - last > PROC_SILENT_SEC:
        return 'unknown', 'silence'
    waiting = [t for t in f.pending if t]
    if waiting and now - min(waiting) < TOOL_STALL_SEC:
        return 'running', 'process'
    if now - last > STALL_SEC:
        return 'stalled', 'silence'
    return 'running', 'process'


def _fresh(notes, last):
    """The latest note that is not older than the last record (an older one belongs to an earlier run)."""
    cur = [n for n in notes if n.ts is not None and n.ts >= last - NOTE_SLACK]
    return max(cur, key=lambda n: n.ts) if cur else None


def _from_note(n):
    """(status, reason, basis) a completion notice of an agent says. Its own record's error line decides before this is asked: the notice is the
    fallback for a record that has not been read. The summary of a real failed notice ends with "(error type rate_limit, HTTP 429" and says "hit your ... limit"."""
    if n.status == 'failed':
        low = n.summary.lower()
        if n.err_status is not None or n.err_type:
            st, why = classify_error(ErrInfo(status=n.err_status, type=n.err_type))
            return st, why, 'notice'
        if 'hit your' in low and 'limit' in low:
            return 'interrupted', 'limit', 'notice'
        return 'failed', ('api_error' if 'api error' in low else None), 'notice'
    if n.status == 'killed':
        return 'killed', 'stopped', 'notice'
    return 'done', None, 'notice'


def judge(f, proc, now):
    """The status of one agent. Pure: the same facts, process and `now` give the same Verdict, whatever the board did before.

    Order: only the last run counts (an error of an earlier run never covers a later one) -> an explicit stop -> the run's own error line (before the process:
    a limit is a limit whether or not the process is gone) -> the end of the run (`cost-state`) -> what the launcher says -> the process.
      cli / Codex   closed by cost-state:  end turn -> done; the launch call hit the Bash time limit -> interrupted/time_limit; stopped -> killed;
                    nothing -> interrupted/exited (nobody is blamed)
      not closed    process alive -> running/stalled; gone -> done after an end turn, else ended/crash; unseen -> running, then unknown after PROC_SILENT_SEC
      sub-agent     the notice of the launcher, then the session: gone -> ended; parent over -> ended; else running/stalled"""
    r = f.runs[-1] if f.runs else None
    last = f.last_ts or f.spawn_ts or 0.0
    st, why, resets, basis = None, None, None, ''
    stop = [t for t in f.stops if t >= last - NOTE_SLACK]
    n = _fresh(f.notes, last)
    closed = r is not None and r.exited
    gone = proc.alive is False or (proc.alive is None and n is not None and not n.is_agent)       # the launching call has ended: the process is over, seen or not
    if closed and f.kind != 'subagent' and r.end_kind == 'end_turn':
        st, basis = 'done', 'exit_marker'                  # the run finished by itself; a later TaskStop or notice changes nothing
    elif stop or (r is not None and r.aborted and f.kind == 'codex'):
        st, why, basis = 'killed', 'stopped', 'stop'
    elif r is not None and r.end_kind == 'error' and r.err is not None:
        st, why = classify_error(r.err)
        resets, basis = (r.err.resets_at if why == 'limit' else None), 'error_line'
    elif f.kind == 'subagent':
        st, why, basis = _sub_status(f, proc, r, n, last, now)
    elif closed:
        if n is not None and n.reason == 'time_limit':
            st, why, basis = 'interrupted', 'time_limit', 'bg_chain'
        elif n is not None and n.status == 'killed':
            st, why, basis = 'killed', 'stopped', 'bg_chain'
        else:
            st, why, basis = 'interrupted', 'exited', 'exit_marker'
    elif r is not None and r.end_kind == 'end_turn' and proc.alive is not True:
        st, basis = 'done', 'end_turn'
    elif n is not None and n.reason == 'time_limit':
        st, why, basis = 'interrupted', 'time_limit', 'bg_chain'
    elif n is not None and n.status == 'killed':
        st, why, basis = 'killed', 'stopped', 'bg_chain'
    elif gone and (f.kind != 'codex' or now - last > CODEX_GONE_GRACE):
        st, why, basis = 'ended', 'crash', 'process'
    else:
        st, basis = _alive_status(f, proc, last, now)
    return Verdict(st, why, resets, _diag(f, proc, r, st, why, resets, last, now, n), basis)


def _sub_status(f, proc, r, n, last, now):
    """A sub-agent has no process and no exit marker of its own: its launcher's notice, then the session it lives in."""
    if n is not None:
        st, why, basis = _from_note(n)
        return st, why, basis
    if proc.alive is False:
        return 'ended', None, 'process'
    if r is not None and r.end_kind == 'end_turn' and f.handbacks and max(f.handbacks) >= last - NOTE_SLACK:
        return 'done', None, 'handback'
    if f.parent_over:
        return 'ended', None, 'parent_over'
    st, basis = _alive_status(f, proc, last, now)
    return st, None, basis


def _ended_at(r, last, now, n):
    """When the stop happened: the last record, or later when the exit marker reports a longer run (start + length) or the launcher's notice says so
    (a call the Bash time limit cut leaves no record)."""
    t = last
    if r is not None and r.exited and r.dur_ms and r.start_ts:
        t = max(t, r.start_ts + r.dur_ms / 1000.0)
    if n is not None and n.ts:
        t = max(t, n.ts)
    return min(t, now)


def _diag(f, proc, r, st, why, resets, last, now, n):
    out = []
    if f.torn or f.lost:
        out.append(('torn_lines', {'recovered': f.torn, 'lost': f.lost}))
    if f.stray:
        out.append(('stray_notice', {'count': f.stray}))
    lost = _marker_lost(f, proc, r)
    if f.drift or lost:
        names = sorted({'%s:%s' % x for x in f.drift} | ({'marker_missing:cost-state'} if lost else set()))
        out.append(('format_drift', {'what': names, 'version': f.version}))
    if len(proc.pids) > 1:
        out.append(('multi_process', {'pids': len(proc.pids)}))
    if proc.alive is None:
        out.append(('proc_unknown', {}))
    if st == 'interrupted' and why in ('limit', 'api_error'):       # only these pass by themselves; a run that exited or hit the time limit waits for nothing
        due = resets + UNRESUMED_LIMIT_SEC if why == 'limit' and resets else _ended_at(r, last, now, n) + UNRESUMED_OTHER_SEC
        if now > due:
            out.append(('not_resumed', {'reason': why}))
    if f.kind == 'cli' and st in ('running', 'stalled') and proc.alive and proc.status == 'idle' and not f.pending and now - last > STALL_SEC:
        out.append(('silent_live', {}))
    return tuple(out)


def _marker_lost(f, proc, r):
    """A `claude -p` child whose last turn ended cleanly and whose process is gone, in a version that writes cost-state, with no cost-state: the format has changed
    (a crash would have no end turn)."""
    return f.kind == 'cli' and r is not None and not r.exited and r.end_kind == 'end_turn' and proc.alive is False and in_window('cost-state', f.version)


# ---------------------------------------------------------------------------------------------------------------------
# the orchestrator and the limit notice
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class OrchVerdict:
    state: str                                             # facts.ORCH_STATES
    resets_at: Optional[float] = None                      # limit_wait: the reset time of the limit line, when it has one
    auto: bool = False                                     # limit_wait: a "continuing automatically" notice came with it
    diag: Tuple[Tuple[str, dict], ...] = ()                # `proc_unknown` when the orchestrator's own process cannot be told (the page's entry)


def orch_state(t, proc, now):
    """The orchestrator's state from its own tracker. `limit_wait`: its last turn ended on a usage limit, no instruction (typed or automatic) came since, and the
    process is not known to be gone. Otherwise `working` while a turn is open (`turn_open`: the lines of work and the ends of turns, in the order they were written, leave
    one open, and the process is not known to be gone), else idle."""
    r = t.runs[-1] if t.runs else None
    diag = (('proc_unknown', {}),) if proc.alive is None else ()
    if r is not None and not r.exited and r.end_kind == 'error' and r.err is not None and classify_error(r.err) == ('interrupted', 'limit') \
            and proc.alive is not False:
        return OrchVerdict('limit_wait', r.err.resets_at, r.auto_ts is not None and r.auto_ts >= (r.streak_ts or 0.0) - 1e-6, diag)
    return OrchVerdict('working' if turn_open(t.turn, proc) else 'idle', diag=diag)


@dataclass
class LimitGroup:
    """Everything that stopped on the same usage limit: one notice instead of one per agent."""
    resets_at: Optional[float]
    agents: List[str] = field(default_factory=list)
    orch: bool = False
    auto: bool = False                                     # the orchestrator continues by itself: the "waiting for the next instruction" notice is hidden

    @property
    def members(self):
        return len(self.agents) + (1 if self.orch else 0)


def group_limits(verdicts, orch=None):
    """[LimitGroup] from {agent id: Verdict} and the OrchVerdict. Agents that are `interrupted/limit` and an orchestrator in `limit_wait` with the same reset time
    form one group. A stop whose record has no reset time never joins a group that has one (nothing is made up), and those without a time share one group."""
    groups = {}
    for aid in sorted(verdicts):
        v = verdicts[aid]
        if v.status == 'interrupted' and v.reason == 'limit':
            groups.setdefault(v.resets_at, LimitGroup(v.resets_at)).agents.append(aid)
    if orch is not None and orch.state == 'limit_wait':
        g = groups.setdefault(orch.resets_at, LimitGroup(orch.resets_at))
        g.orch, g.auto = True, orch.auto
    return sorted(groups.values(), key=lambda g: (g.resets_at is None, g.resets_at or 0.0))


def group_diag(groups):
    """The `limit_group` diagnostics: one per group of two or more."""
    return tuple(('limit_group', {'resets_at': g.resets_at, 'members': g.members}) for g in groups if g.members > 1)
