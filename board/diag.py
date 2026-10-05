"""Diagnostics: what the judgments noticed and could not settle, in one list per session. The affiliation judgment (board/affil.py through LINKS.diags), the
state judgment (board/runstate.py: per agent, and the limit groups), the debate judgment (board/debates.py) and the board's own reading (torn lines, lines that
raised an error, a link cache it cannot trust, a live process with no record) all say their codes here. An entry has a code, a level, who it is about and a few
small values: never the text of a record, an instruction, a path inside a record or a value of an environment. At most MAX_PER_SESSION entries are kept."""

import math
import os
import re
import time

from . import lineage, procs, runstate as RS
from .facts import NODE_ID_RE
from .codex_index import CODEX
from .link import LINKS
from .util import SID_RE, short_path

MAX_PER_SESSION = 200
LEVEL = {                                       # codes that are only news; every other code is a warning
    'content_only': 'info', 'limit_group': 'info', 'format_drift': 'info', 'declaration_missing': 'info', 'debate_in_misc': 'info',
    'proc_unknown': 'info', 'torn_lines': 'info', 'stray_notice': 'info',
}
AGENT_CODES = ('evidence_conflict', 'content_author_differs', 'content_only', 'ambiguous_content', 'node_unresolved', 'fingerprint_incomplete', 'path_unresolved')
VALUE_MAX = 80
LIST_MAX = 20
COUNT_MAX = 10 ** 9


class _Items:
    """A parameter that is a short list: each item has to pass `ok`, the others are left out."""

    def __init__(self, ok):
        self.ok = ok


def _count(v):
    return type(v) is int and 0 <= v <= COUNT_MAX


def _when(v):
    return v is None or (type(v) in (int, float) and math.isfinite(v) and 0 <= v < 1e12)


def _flag(v):
    return type(v) is bool


def _one_of(*names):
    return lambda v: isinstance(v, str) and v in names


def _shaped(rx):
    rx = re.compile(rx)
    return lambda v: isinstance(v, str) and len(v) <= VALUE_MAX and rx.fullmatch(v) is not None


_VERSION = _shaped(RS.VERSION_RE.pattern)
_TEXT = r'[^\x00-\x1f\x7f]'                  # any character but a control character
_STEM = r'[^\x00-\x1f\x7f/]'                  # ... and no slash: a file name
_TREE = lambda v: isinstance(v, str) and (SID_RE.fullmatch(v) is not None or NODE_ID_RE.fullmatch(v) is not None)

# What each code carries, by name and by shape. A diagnostic is for the page and /api/diag: it gets only what its code is known to give, and a text only in the shape that
# code gives it, so nothing a record said (a version, an instruction, a path, a message) can get through by being put in a parameter. A code that is not here carries none.
PARAMS = {
    'limit_group': {'resets_at': _when, 'members': _count},
    'not_resumed': {'reason': _one_of('limit', 'api_error')},
    'silent_live': {},
    'torn_lines': {'recovered': _count, 'lost': _count},
    'multi_process': {'pids': _count},
    'invisible_child': {'n': _count},
    'format_drift': {'what': _Items(_shaped(r'(?:marker_missing|out_of_range|invalid):[A-Za-z0-9_.\-]{1,40}')), 'version': lambda v: v is None or _VERSION(v)},
    'parse_errors': {'n': _count},
    'stray_notice': {'count': _count},
    'proc_unknown': {},
    'cache_error': {'what': _one_of('write', 'untrusted'), 'error': _shaped(r'[A-Za-z_][A-Za-z0-9_]{0,39}')},
    'listing_capped': {},
    'evidence_conflict': {'other': _TREE},
    'content_author_differs': {'other': _TREE},
    'content_only': {},
    'ambiguous_content': {},
    'node_unresolved': {'run': _count},
    'orphan_launch': {'n': _count, 'ambiguous': _flag},
    'fingerprint_incomplete': {},
    'path_unresolved': {'n': _count},
    'path_ambiguous': {},
    'alias_collision': {'detail': _shaped(_TEXT + '{1,%d}' % VALUE_MAX)},
    'seat_tie_held': {'detail': _shaped(r'(?:-|[0-9]{1,4})/' + _STEM + '{1,60}')},
    'debate_in_misc': {},
    'declaration_missing': {},
}


def _clean(params):
    """Only small plain values: numbers, booleans, None, short strings, short lists of those. Anything else is left out."""
    out = {}
    for k, v in (params or {}).items():
        if isinstance(v, (bool, int, float)) or v is None:
            out[str(k)] = v
        elif isinstance(v, str) and len(v) <= VALUE_MAX:
            out[str(k)] = v
        elif isinstance(v, (list, tuple, set, frozenset)) and len(v) <= LIST_MAX and all(isinstance(x, (bool, int, float, str)) and len(str(x)) <= VALUE_MAX for x in v):
            out[str(k)] = sorted(v) if isinstance(v, (set, frozenset)) else list(v)
    return out


def _allowed(code, params):
    """The parameters `code` is known to have, each in its own shape (see PARAMS). Anything else is left out."""
    rows = PARAMS.get(code) or {}
    out = {}
    for k, v in params.items():
        spec = rows.get(k)
        if spec is None:
            continue
        if isinstance(spec, _Items):
            keep = [x for x in v if spec.ok(x)] if isinstance(v, list) else []
            if keep:
                out[k] = keep
        elif spec(v):
            out[k] = v
    return out


def _entry(code, scope, agent=None, unit=None, **params):
    return {'code': code, 'level': LEVEL.get(code, 'warn'), 'scope': scope, 'agent': agent, 'unit': unit, 'params': _allowed(code, _clean(params))}


INVISIBLE_EVERY = 3.0            # seconds: the process table is read at most this often per session page
_invisible = {}                  # id of a Session object -> (monotonic time, count)


def invisible_children(s):
    """Live `claude -p` processes started under this session (their environment or process parents say so) whose session has no record on disk
    (`--no-session-persistence`): the board can see a child run and cannot list it. -> number of them (looked at again after INVISIBLE_EVERY seconds)."""
    now = time.monotonic()
    hit = _invisible.get(id(s))
    if hit is not None and now - hit[0] < INVISIBLE_EVERY and hit[2] is s:
        return hit[1]
    n = _invisible_now(s)
    _invisible[id(s)] = (now, n, s)
    if len(_invisible) > 64:
        _invisible.pop(next(iter(_invisible)))
    return n


def _invisible_now(s):
    try:
        sess = lineage.read_sessions()
    except Exception:   # noqa: BLE001 — a diagnostic never stops the page
        return 0
    have = {f['sid'] for f in LINKS.files.values()}
    n = 0
    for pid, d in sess.items():
        if not d.get('print') or d['sid'] in have or d['sid'] == s.id:
            continue
        psid, _rule = lineage.parent_session(pid, d['sid'], sess, {s.id})
        if psid == s.id:
            n += 1
    return n


def codex_drift(s):
    """The shapes of Codex records that the board could not read, among the Codex threads of this page (its own thread when it is a Codex page, and every Codex agent on it), as
    `format_drift` names: a thread whose first line is of a shape the board does not know (`internal`, hidden like a guardian: another `source`, a parent where none is expected, a
    sub-agent whose history end is unknown) and command records that could not be read. Made again only when the Codex index changed or the threads of the page did."""
    ids = frozenset(([s.id] if s.provider == 'codex' else []) + [a.id for a in s.agents.values() if a.provider == 'codex'])
    key = (CODEX.version, ids)
    hit = getattr(s, '_cx_drift', None)
    if hit is not None and hit[0] == key:
        return hit[1]
    what = set()
    for tid in ids:
        e = CODEX.get(tid)
        if e is not None and e.get('cmds_skipped'):
            what.add('invalid:CommandExecution')
        if any(c.get('kind') == 'internal' and c.get('drift') for c in CODEX.children(tid)):
            what.add('invalid:thread_source')
    s._cx_drift = (key, what)
    return what


def collect(s, now, verdicts, groups):
    """The diagnostics of one session page as a list of entries (see the module text), most serious first, at most MAX_PER_SESSION.
    `verdicts` {agent id: runstate.Verdict}, `groups` the runstate.LimitGroups of this moment."""
    out = []
    on_page = set(s.agents)
    # a child held between sessions is on no page; its diagnostics go to the pages that list it as an unlinked candidate
    unl = {x['id'] for x in LINKS.unlinked_for(s.id)}
    # who launched what: the affiliation judgment (child subjects) and the launches that left no child (the launching tree)
    for d in list(LINKS.diags):
        code, subject = d.get('code'), d.get('subject')
        extra = {k: v for k, v in d.items() if k not in ('code', 'subject', 'tree', 'node')}
        if code == 'orphan_launch':
            if subject == s.id or subject in on_page:
                out.append(_entry(code, 'orch', d.get('node') if d.get('node') in on_page else None, **extra))
        elif subject in on_page or subject in unl:
            out.append(_entry(code, 'agent', subject, **extra))
    inv = invisible_children(s) if s.provider == 'claude' else 0
    if inv:
        out.append(_entry('invisible_child', 'orch', None, n=inv))
    # the state judgment: what each agent's own judgment noticed, and the groups of agents stopped by one limit
    for aid, v in verdicts.items():
        for code, params in v.diag:
            out.append(_entry(code, 'agent', aid, **params))
    for code, params in RS.group_diag(groups):
        out.append(_entry(code, 'orch', None, **params))
    # the board's own reading of this record
    runs = getattr(s, 'runs', None)
    if runs is not None and (runs.torn or runs.lost):
        out.append(_entry('torn_lines', 'orch', None, recovered=runs.torn, lost=runs.lost))
    seen = {'%s:%s' % x for x in runs.drift} if runs is not None and runs.drift else set()           # a known marker gone from its window, or a value that is no version; a version outside what was checked says nothing by itself
    seen |= codex_drift(s)
    if seen:
        out.append(_entry('format_drift', 'orch', None, what=sorted(seen), version=runs.version if runs is not None and runs.drift else None))
    if getattr(s, 'parse_errors', 0):
        out.append(_entry('parse_errors', 'session', None, n=s.parse_errors))
    ln = LINKS.lineage
    if ln.write_error:
        out.append(_entry('cache_error', 'session', None, what='write', error=str(ln.write_error)[:40]))
    elif ln.cache and not lineage._cache_trusted(ln.cache):             # the folder is another user's or others can write to it: the cache is neither read nor written
        out.append(_entry('cache_error', 'session', None, what='untrusted'))
    # the debate judgment: seats held, paths that fit two folders, names that collide, reviews that declare no reviewer
    for d in getattr(s, 'debate_diag', None) or ():
        code, agent, unit, detail = d.get('code'), d.get('agent'), d.get('unit'), d.get('detail')
        scope = 'agent' if agent else ('session' if code == 'listing_capped' else 'unit')
        out.append(_entry(code, scope, agent, short_path(unit) if unit else None, **({'detail': detail} if detail else {})))
    rank = {'warn': 0, 'info': 1}
    out.sort(key=lambda e: (rank[e['level']], e['code'], e['agent'] or '', e['unit'] or ''))        # a stable order, so a page that polls sees no flicker
    return out[:MAX_PER_SESSION]


def counts(entries):
    """What /api/state carries: the number of entries, per level and per code. No entry itself."""
    by_code, by_level = {}, {'warn': 0, 'info': 0}
    for e in entries:
        by_code[e['code']] = by_code.get(e['code'], 0) + 1
        by_level[e['level']] += 1
    return {'n': len(entries), 'warn': by_level['warn'], 'info': by_level['info'], 'by_code': by_code, 'capped': len(entries) >= MAX_PER_SESSION}
