"""Fact shapes and enumerations shared by the affiliation, debate and state judgments.

Definitions only: no logic, nothing here reads a file or a process. The collectors (link.py, agents.py, debates.py) fill these shapes from the raw
records, and the pure judgment functions (affil.py, units.py, runstate.py) read them. Standard library only; Python 3.9 compatible.

Fields that are not obvious:
  Span.owner_tree / owner_node   the main session id and the node (an agent id `a`+16 hex, or None for the main session) whose record holds the Bash call
  Span.end_status / end_reason   how the call ended: `result` (foreground), `notification` (background), `running` (no end seen); the reason is the
                                 machine phrase of the notification ("background time limit", "exit code N", a TaskStop) or None
  Span.launchy                   loose test: the command mentions `claude`/`codex` or runs a `.sh`/`.py` file (a candidate, never a proof)
  Turn.source                    `sdk` (promptSource=="sdk"), `system` (notification/auto-continue), `user` (a person typed it), `legacy` (older records)
  Run.end_kind                   what ended the last assistant turn of the run; `none` means the record stops without an end
  Evidence.field                 which judgment the evidence speaks to; a tree evidence says nothing about the node
  Relation.certain               per field: {tree, node, call} -> bool
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# ---------------------------------------------------------------------------------------------------------------------
# Enumerations (tuples, so a test can check membership and a table can iterate them in a fixed order)
# ---------------------------------------------------------------------------------------------------------------------
NODE_ID_RE = re.compile(r'a[0-9a-f]{16}')            # a Claude sub-agent id; the main session has no node (None)
CX_NODE_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')      # a Codex sub-agent: the thread id of the sub-agent
ANY_NODE_RE = re.compile(NODE_ID_RE.pattern + '|' + CX_NODE_RE.pattern)      # the node of a relation of either provider (NODE_ID_RE alone stays Claude's: runstate and diag read it)

RULE_CLASSES = ('certain', 'guess', 'none')
RULES = ('subagent', 'out', 'env', 'proc', 'file', 'content', 'content_short', 'time', 'cache')
RULE_RANK = {'subagent': 1, 'out': 1, 'env': 2, 'proc': 2, 'file': 2, 'cache': 2, 'content': 3, 'content_short': 4, 'time': 5}
CERTAIN_RULES = ('subagent', 'out', 'env', 'proc', 'file', 'cache', 'content')   # content_short and time are guesses

STATUSES = ('running', 'stalled', 'interrupted', 'done', 'failed', 'killed', 'ended', 'unknown')
REASONS = ('limit', 'api_error', 'time_limit', 'stopped', 'exited', 'crash')       # and None
CELLS = ('waiting', 'writing', 'draft', 'paused', 'done', 'missing', 'previous')
ROLES = ('writer', 'reader', 'none')
ORCH_STATES = ('working', 'idle', 'limit_wait')

SPAN_END_STATUS = ('result', 'notification', 'running')
REDIRECT_OPS = ('>', '>>', '2>', '2>&1', '-o')
TURN_SOURCES = ('sdk', 'system', 'user', 'legacy')
END_KINDS = ('end_turn', 'mid_turn', 'error', 'none')
EVIDENCE_FIELDS = ('tree', 'node', 'call', 'by', 'unit', 'seat', 'submit')
EVIDENCE_KINDS = RULES + ('write_ok', 'read', 'redirect', 'tool', 'shell', 'planned')
UNIT_KINDS = ('rounds',)

DIAG_STATE = ('limit_group', 'not_resumed', 'silent_live', 'torn_lines', 'multi_process', 'invisible_child', 'format_drift', 'parse_errors',
              'stray_notice', 'proc_unknown', 'cache_error', 'listing_capped')
DIAG_AFFIL = ('evidence_conflict', 'content_author_differs', 'content_only', 'ambiguous_content', 'node_unresolved', 'orphan_launch',
              'fingerprint_incomplete', 'path_unresolved')
DIAG_DEBATE = ('alias_collision', 'seat_tie_held', 'launch_split', 'history_lost')
DIAG_CODES = DIAG_STATE + DIAG_AFFIL + DIAG_DEBATE
FINAL_WHY = ('no_report', 'open_cell', 'none', 'several', 'empty_round', 'live_participant', 'estimated_room', 'history_lost')      # why a final is not confirmed (J15), in this order

# The truth fields a scenario can grade, plus the two the state bundle needs for the orchestrator.
TRUTH_FIELDS = ('tree', 'node', 'rule_class', 'by', 'status', 'reason', 'resets_at', 'unit', 'round', 'seat', 'cell', 'role',
                'orch_state', 'unlinked', 'placed', 'hint', 'edits')


# ---------------------------------------------------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Redirect:
    """One output redirect of a launched command: `> f`, `2> f`, `>> f`, `2>&1`, or the `-o f` option."""
    fd: int                                           # 1 or 2 (the file descriptor the redirect applies to)
    op: str                                           # one of REDIRECT_OPS
    path_raw: str                                     # as written in the command
    path_resolved: Optional[str] = None               # absolute path, or None when it could not be resolved
    unresolved_vars: List[str] = field(default_factory=list)


@dataclass
class Span:
    """One Bash tool call: when it ran, how it ended, and what it redirects."""
    owner_tree: str
    owner_node: Optional[str]
    call_id: str                                      # the tool_use id
    start: float
    end: Optional[float]                              # None while it is still running
    bg_id: Optional[str] = None                       # backgroundTaskId of a background call
    end_status: str = 'running'                       # one of SPAN_END_STATUS
    end_reason: Optional[str] = None
    launchy: bool = False
    cwd: Optional[str] = None
    redirects: List[Redirect] = field(default_factory=list)
    msg_id: Optional[str] = None                      # the group the call was made in: the `message.id` of a Claude Bash line, the exec call around a Codex command (else its item id); None when not known


@dataclass
class Turn:
    """One instruction line a run started with."""
    sid: str
    idx: int                                          # turnPosition.turnIndex (1-based inside a run)
    ts: float
    source: str                                       # one of TURN_SOURCES
    prompt_head: str = ''                             # the first characters only (never the whole text)


@dataclass
class ErrInfo:
    status: Optional[int] = None                      # HTTP status of an API error line
    type: Optional[str] = None                        # `rate_limit`, `overloaded`, ...
    resets_at: Optional[float] = None                 # epoch seconds, None when the record has no time


@dataclass
class Run:
    """One process lifetime of a session; `cost-state` closes it. Turns inside it are its sdk instructions."""
    sid: str
    epoch: int                                        # 1, 2, ... in record order
    turns: List[Turn] = field(default_factory=list)
    end_ts: Optional[float] = None
    end_kind: str = 'none'                            # one of END_KINDS
    err: Optional[ErrInfo] = None
    exited: bool = False                              # a `cost-state` line closed it
    dur_ms: Optional[int] = None


@dataclass
class Evidence:
    kind: str                                         # one of EVIDENCE_KINDS
    subject: str                                      # child sid, agent id or report path the evidence is about
    field: str                                        # one of EVIDENCE_FIELDS
    candidates: List[Any] = field(default_factory=list)
    rank: int = 5
    source: Tuple[Optional[str], Optional[int]] = (None, None)   # (file, line)
    valid: bool = True


@dataclass
class Relation:
    """The decided affiliation of one child session. `by` lists, per run, who handed the run its instruction."""
    child: str
    tree: Optional[str]
    node: Optional[str]
    call: Optional[str]
    by: List[Optional[Tuple[str, Optional[str]]]] = field(default_factory=list)
    rule: Optional[str] = None
    certain: Dict[str, bool] = field(default_factory=dict)
    conflicts: List[Evidence] = field(default_factory=list)


@dataclass
class Unit:
    """A debate folder found on disk."""
    id: str
    path: str
    aliases: List[str] = field(default_factory=list)
    brief: Optional[str] = None
    kind: str = 'rounds'                              # one of UNIT_KINDS
    parent: Optional[str] = None
    declared_reports: List[str] = field(default_factory=list)
    rounds: Dict[int, List[str]] = field(default_factory=dict)       # round number -> the real folders that carry it (r1, r01, round1 ...)
    file_rounds: Dict[int, List[str]] = field(default_factory=dict)  # round number -> filenames directly in the unit (O23)
    seat_aliases: Dict[str, str] = field(default_factory=dict)       # letter -> file stem


@dataclass
class Assignment:
    agent: str
    unit: str
    round: Optional[int]                              # None for a flat review
    seat: str
    start: Optional[float] = None
    end: Optional[float] = None                       # closed when the agent is reassigned
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class Artifact:
    path: str
    write_requested: bool = False
    write_ok: bool = False
    file_ok: bool = False                             # the file exists and has content
    writer_actor: Optional[str] = None                # who wrote it (a redirect is written by the launcher, not the child)
    report_author: Optional[str] = None


# ---------------------------------------------------------------------------------------------------------------------
# Collected events (the write, read and command records the collectors keep for the debate judgment; 0.3.0)
# ---------------------------------------------------------------------------------------------------------------------
WRITE_KINDS = ('create', 'replace', 'update', 'append', 'unknown')
AUTHORING = ('create', 'replace', 'unknown')        # events that made a whole file: they can make their agent the owner of a cell
WRITE_EVIDENCE = ('tool', 'shell', 'planned')        # a cell's evidence has `tag` as well (a planned cell)
READ_VIA = ('tool', 'codex', 'shell')
PROOFS = ('tool', 'exit', 'window', 'sha', 'content')
CHECKED = ('window', 'sha', 'content')               # these are sure only after the judgment's storage check F (J1)


@dataclass(frozen=True)
class LaunchKey:
    """The call that launched an agent. Launched together = the same gkey(). When the key is not known the agent's launch is None, and None is in no group with anything."""
    provider: str              # of the record the call is in: 'claude' | 'codex'
    tree: str                  # the session (Codex: thread) id of the record the call is in
    node: Optional[str]        # the id of the sub-agent / child thread whose record it is, None for the main record of the session
    group: str                 # what launched together means (2.3)
    call: str                  # the id of the call (2.3)

    def gkey(self):
        return (self.provider, self.tree, self.node, self.group)


@dataclass(frozen=True)
class WriteEvent:
    agent: str                 # agent id; the orchestrator of the session is 'orch'
    path: str                  # absolute, normpath (the judgment makes it realpath)
    ts: float                  # the time of the call (tool_use, item); a planned event: the end of the run
    kind: str                  # WRITE_KINDS
    evidence: str              # WRITE_EVIDENCE
    ok: Optional[bool]         # what the collector saw of the result: True worked, False failed, None not known. Sure is J1 (a CHECKED proof also needs F)
    call: Optional[str] = None # the id of the call that wrote (tool_use id, Codex item id, the call that launched the turn)
    run: Optional[int] = None  # the run the event is in: the Claude RunTracker epoch, the Codex turn n
    proof: str = 'tool'        # PROOFS: tool (W1) · exit (a deciding place of a shell command) · window (a masked place, claude -p with the text unknown) · sha (Codex -o) · content (claude -p)
    span: Optional[Tuple[float, float]] = None   # CHECKED only: the window the check looks in (start of the command, its end); planned: (Planned.ts, end of the run)
    shas: Tuple[str, ...] = ()                   # sha and content only: the sha1 of the bytes expected


@dataclass(frozen=True)
class ReadEvent:
    agent: str
    path: str
    ts: float
    via: str                   # READ_VIA


@dataclass(frozen=True)
class CmdWindow:
    """The time one command ran (for the W3 hint). Not only the ones that worked: failed, unknown and open ones are kept too (they compete)."""
    agent: str                 # agent id | 'orch'
    call: str
    t0: float                  # the time of the call. Codex: end - duration, inside an exec cell the time of the exec call around it
    t1: Optional[float]        # the end: foreground = tool_result, background = the completion notice, Codex = item_completed. None = open
    ok: Optional[bool]
    reads: Tuple[str, ...] = ()  # the paths the command read (the same reading as ReadEvent)


@dataclass(frozen=True)
class Planned:
    """One requirement: a run named the path as its output (J6, J7)."""
    agent: str
    path: str                  # absolute
    op: str                    # '-o' | '>' | '>>'
    call: Optional[str]
    run: Optional[int]         # the run that named the path (the Codex turn n · the epoch of the claude -p child)
    src: str                   # 'command' | 'argv'
    ts: float = 0.0            # the time of the call that launched the run (the turn's `bash_ts` · the time of the Span). The time of the requirement and the start of the check window


@dataclass(frozen=True)
class Tag:
    room: str                  # absolute realpath
    seat: Optional[str]        # 'name' | 'rN/name' | None
    source: str                # 'environ' | 'command'
    run: Optional[int] = None  # the run the value was read for: always the last run (environ = the live process, command = the command launched last). The time of a SEAT requirement = AgentFacts.run_start


@dataclass(frozen=True)
class Hint:
    ts: float                  # the last time an orchestrator write pointed at that folder (today's orch_hints value)
    calls: FrozenSet[str]      # the calls that made the write or the mkdir
    groups: FrozenSet[tuple]   # the LaunchKey.gkey() of those calls (the message.id of the orchestrator's record, ...)
