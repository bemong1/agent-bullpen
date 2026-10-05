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
from typing import Any, Dict, List, Optional, Tuple

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
CELLS = ('waiting', 'writing', 'draft', 'paused', 'done', 'missing')
ROLES = ('writer', 'reader', 'none')
ORCH_STATES = ('working', 'idle', 'limit_wait')

SPAN_END_STATUS = ('result', 'notification', 'running')
REDIRECT_OPS = ('>', '>>', '2>', '2>&1', '-o')
TURN_SOURCES = ('sdk', 'system', 'user', 'legacy')
END_KINDS = ('end_turn', 'mid_turn', 'error', 'none')
EVIDENCE_FIELDS = ('tree', 'node', 'call', 'by', 'unit', 'seat', 'submit')
EVIDENCE_KINDS = RULES + ('write_intent', 'own_marker', 'write_ok', 'tag', 'read', 'quote', 'negation', 'redirect', 'declared')
UNIT_KINDS = ('rounds', 'flat')

DIAG_STATE = ('limit_group', 'not_resumed', 'silent_live', 'torn_lines', 'multi_process', 'invisible_child', 'format_drift', 'parse_errors',
              'stray_notice', 'proc_unknown', 'cache_error', 'listing_capped')
DIAG_AFFIL = ('evidence_conflict', 'content_author_differs', 'content_only', 'ambiguous_content', 'node_unresolved', 'orphan_launch',
              'fingerprint_incomplete')
DIAG_DEBATE = ('path_unresolved', 'path_ambiguous', 'alias_collision', 'seat_tie_held', 'debate_in_misc', 'declaration_missing')
DIAG_CODES = DIAG_STATE + DIAG_AFFIL + DIAG_DEBATE

# The truth fields a scenario can grade, plus the two the state bundle needs for the orchestrator.
TRUTH_FIELDS = ('tree', 'node', 'rule_class', 'by', 'status', 'reason', 'resets_at', 'unit', 'round', 'seat', 'cell', 'role',
                'orch_state', 'unlinked')


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
