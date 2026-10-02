"""Picks the cases, runs them against the board, grades every (case, subject, field) as pass / miss / wrong, and writes the red-cell tables.

The grading table (everything else keys off it):

    truth      board shows                result
    ---------  -------------------------  -----
    nothing    nothing (field not emitted) pass        there was nothing to show and nothing is shown
    nothing    nothing / none              pass
    nothing    a value                     WRONG       a false link / seat / state (the precision alarm)
    a value    not emitted / none / null   miss        an honest miss
    a value    the same value              pass
    a value    another value               WRONG
    unknown    anything but unknown        WRONG       the board claimed what the records cannot tell
    a value    unknown                     miss
    forbid     the board shows it          WRONG       one cell per forbidden fact; not shown is a pass

`node` is special on both sides: its truth `None` means "the main session", a value, so a board that does not emit `node` has missed it, and a board that
says `None` for a sub-agent's child has claimed the main session (wrong).

    python3 -m tools.scenarios.run                 # summary table
    python3 -m tools.scenarios.run --md            # also print the table of red cells (to standard output)
    python3 -m tools.scenarios.run --md red.md     # ... or write it to a file
    python3 -m tools.scenarios.run --cases 'aff:*way=tmux*'    # only the cases whose id matches the glob
"""

import argparse
import collections
import fnmatch
import itertools
import os
import random
import shutil
import sys
import tempfile
import time
from unittest import mock

from . import axes, build, observe, oracle
from .axes import AXES, BUNDLES, Case, normalize
from .observe import MISSING

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLAIM_NONE = ('node',)               # a `None` the board reports is a claim ("the main session"), not an absence
CLASS_ORDER = ('none', 'guess', 'certain')
ALWAYS_REPORTED = ('node',)          # not emitting it is a miss even when the truth is None


def resolve(val, b):
    """'@role' -> the id the builder made for that role; ('T', n) -> the case time n seconds after its base; tuples are resolved item by item."""
    if isinstance(val, str) and val.startswith('@'):
        return b.ids.get(val[1:], val)
    if isinstance(val, tuple):
        if len(val) == 2 and val[0] == 'T':
            return b.T(val[1])
        return tuple(resolve(x, b) for x in val)
    return val


def grade_set(want, got):
    """A field that is the whole set of places something shows (every seat of one agent, every debate of the list). A place the truth does not list is a false
    place (wrong, whatever else is right); a listed place that is not shown is a miss. An empty or absent side is the empty set."""
    w = set() if want is None or want is MISSING else set(want)
    g = set() if got is MISSING or got is None else set(got)
    if g - w:
        return 'wrong'
    return 'miss' if w - g else 'pass'


def grade(field, want, got):
    """pass | miss | wrong for one field (see the table in the module docstring)."""
    if isinstance(want, (set, frozenset)) or isinstance(got, (set, frozenset)):
        return grade_set(want, got)
    if got is MISSING:
        return 'pass' if (want is None or want == 'none') and field not in ALWAYS_REPORTED else 'miss'
    if want is None or want == 'none':
        if field == 'node':
            return 'pass' if got is None else 'wrong'
        return 'pass' if got is None or got == 'none' else 'wrong'
    if want == 'unknown':
        return 'pass' if got == 'unknown' else 'wrong'
    if got is None or got == 'none' or got == 'unknown':
        return 'wrong' if field in CLAIM_NONE and got is None else 'miss'
    if field == 'rule_class' and want in CLASS_ORDER and got in CLASS_ORDER:
        # less sure than the records allow is a miss of certainty; surer than they allow is a false claim
        return 'pass' if got == want else ('miss' if CLASS_ORDER.index(got) < CLASS_ORDER.index(want) else 'wrong')
    if isinstance(want, (int, float)) and isinstance(got, (int, float)) and not isinstance(want, bool):
        return 'pass' if abs(got - want) < 1 else 'wrong'
    return 'pass' if got == want else 'wrong'


def sym(v, b):
    """A stable, JSON-able form of a value: ids become '@role', case times become 'T+n', sets become sorted lists."""
    if v is MISSING:
        return 'MISSING'
    if isinstance(v, str):
        for k, x in b.ids.items():
            if x == v:
                return '@' + k
        return v
    if isinstance(v, (set, frozenset)):
        return sorted(sym(x, b) for x in v)
    if isinstance(v, (tuple, list)):
        return [sym(x, b) for x in v]
    if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v - b.t0) < 10 ** 7:
        return 'T%+d' % round(v - b.t0)
    return v


class Cell:
    """One graded fact: a subject's field in a case. `ws` / `gs` are the stable forms of what the truth wants and what the board shows."""
    __slots__ = ('case', 'role', 'field', 'result', 'want', 'got', 'ws', 'gs', 'axes', 'bundle', 'real', 'twin')

    def __init__(self, case, role, field, result, want, got, b):
        self.case, self.role, self.field, self.result, self.want, self.got = case.id, role, field, result, want, got
        self.ws, self.gs = sym(want, b), sym(got, b)
        self.axes, self.bundle, self.real, self.twin = dict(case.v), case.bundle, case.real, case.twin_of is not None
        if case.bundle == 'cpl':                     # the integrated scene's participant is always a `claude -p` child writing a plain report
            self.axes.update(kind='cli', role='writer', nstyle='plain', structure='single', rdir='r1', homonym='none', decl='yes')

    @property
    def key(self):
        return '%s|%s|%s' % (self.case, self.role, self.field)

    @property
    def absent(self):
        """A miss because the board does not emit the field at all (as opposed to emitting nothing / null)."""
        return self.result == 'miss' and self.got is MISSING

    def show(self, v):
        return 'MISSING' if v is MISSING else repr(v)


PAGE_WIDE = ('proc_unknown',)         # said about the page's process table, not about one subject: graded by code (every agent and the orchestrator may carry it)


def grade_case(case, b, obs, truth):
    """Every cell of one case."""
    cells = []
    for role, fields in truth.subjects.items():
        for field, want in fields.items():
            want = resolve(want, b)
            got = obs.get(role, field)
            if isinstance(got, tuple):
                got = tuple(got)
            result = 'pass' if got in truth.accepted.get((role, field), ()) else grade(field, want, got)
            cells.append(Cell(case, role, field, result, want, got, b))
    for code, role in truth.diag:
        shown = any(c == code for c, _ in obs.diag) if code in PAGE_WIDE else (code, role) in obs.diag
        cells.append(Cell(case, role, 'diag:' + code, 'pass' if shown else 'miss', code, code if shown else MISSING, b))
        # the small values the truth states for the entry (a count, the members of a group ...): every entry of that code and subject has to carry them
        entries = obs.diag_params.get((code, role), [])
        for key, want in sorted(truth.diag_params.get((code, role), {}).items()):
            if not entries:
                continue                                           # the entry itself is the missing cell above
            want = resolve(want, b)
            got = next((e.get(key, MISSING) for e in entries if grade(key, want, e.get(key, MISSING)) != 'pass'), entries[0].get(key, MISSING))
            cells.append(Cell(case, role, 'diag:%s.%s' % (code, key), grade(key, want, got), want, got, b))
    expected = set(truth.diag)
    expected_codes = {c for c, _ in truth.diag}
    scope = oracle.diag_scope(case)
    for code, role in sorted(obs.diag):
        allowed = any(c == code for c, _ in truth.allowed) if code in PAGE_WIDE else (code, role) in truth.allowed
        if (scope is not None and code not in scope) or allowed:
            continue
        if (code in expected_codes) if code in PAGE_WIDE else ((code, role) in expected):
            continue
        cells.append(Cell(case, role, 'diag:' + code, 'wrong', None, code, b))
    for role, field, value in truth.forbid:
        value = resolve(value, b)
        got = obs.get(role, field)
        hit = got is not MISSING and ((value in got) if isinstance(got, (set, frozenset)) else got == value)
        cells.append(Cell(case, role, 'forbid:%s=%s' % (field, sym(value, b)), 'wrong' if hit else 'pass', 'not %s' % sym(value, b), got, b))
    return cells


def run_case(case, root):
    """Build, observe, grade one case. Returns its cells."""
    case_root = os.path.join(root, axes.digest(case.id, n=12))
    b = build.build_case(case, case_root)
    try:
        # the board is read with the clock frozen at the case's first look, the cache load included (it happens before each phase freezes its own clock):
        # nothing here may depend on today's date
        with mock.patch('time.time', lambda: b.phases[0].now):
            obs = observe.observe(b)
        truth = oracle.truth(case)
        return grade_case(case, b, obs, truth)
    finally:
        shutil.rmtree(case_root, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------------------------------------------------
def _pair_rows(bundle, names, a, x, b, y, uncovered, tries):
    """Candidate rows that keep the pair (a=x, b=y) after normalisation, best first. Random fills seeded by the pair, so the result never changes between runs."""
    rnd = random.Random(int(axes.digest('pair', bundle, a, x, b, y, n=12), 16))
    best, best_gain = None, -1
    for _ in range(tries):
        row = {n: rnd.choice(AXES[n]) for n in names}
        row[a], row[b] = x, y
        case = normalize(Case(bundle, row))
        if case.v[a] != x or case.v[b] != y:
            continue
        gain = sum(1 for (i, m) in itertools.combinations(names, 2) if (i, case.v[i], m, case.v[m]) in uncovered)
        if gain > best_gain:
            best, best_gain = case, gain
    return best


def pairwise(bundle, names, seed_cases=(), tries=300):
    """A deterministic greedy cover of every pair of values of the axes `names`. A pair the normaliser folds away in every random completion is impossible
    and dropped (the test re-checks that claim with more tries)."""
    names = list(names)
    uncovered = {(a, x, b, y) for a, b in itertools.combinations(names, 2) for x in AXES[a] for y in AXES[b]}
    chosen, seen = [], set()

    def take(case):
        if case.key() in seen:
            return
        seen.add(case.key())
        chosen.append(case)
        for a, b in itertools.combinations(names, 2):
            uncovered.discard((a, case.v[a], b, case.v[b]))

    for c in seed_cases:
        take(normalize(c))
    while uncovered:
        a, x, b, y = min(uncovered)
        case = _pair_rows(bundle, names, a, x, b, y, uncovered, tries)
        if case is None:
            uncovered.discard((a, x, b, y))              # impossible: no completion keeps the pair
            continue
        take(case)
    return chosen


PAIR_AXES = {
    'aff': [a for a in BUNDLES['aff'] if a != 'bait' and a not in axes.OPTIONAL['aff']],
    'deb': [a for a in BUNDLES['deb'] if a not in axes.OPTIONAL['deb']],
    'sta': list(BUNDLES['sta']),
    'cpl': list(BUNDLES['cpl']),
}
BAITS = ('cwd_mismatch', 'text_mismatch', 'too_old', 'echo_only', 'other_user', 'pid_reuse')
CODEX_LOOKALIKES = ('cwd_mismatch', 'text_mismatch', 'too_old', 'echo_only')        # what replaces the launch of a Codex exec thread (the others need a Claude process)
NEGATIVE_ROLES = ('reader', 'quoter', 'negator', 'tag_only', 'failed_write', 'ghost', 'rival')


def is_positive(case):
    """A case whose truth links, seats or classifies something (so a decoy twin is worth having)."""
    t = oracle.truth(case)
    for subj, fields in t.subjects.items():
        if fields.get('tree') or fields.get('seat') or fields.get('status') in ('interrupted', 'killed', 'done', 'running'):
            return True
    return False


def twin_of(case):
    """The decoy twin of a positive case: the same scene with the evidence replaced by a lookalike that must not count."""
    h = int(axes.digest(case.id, 'twin', n=6), 16)
    v = dict(case.v)
    if case.bundle == 'aff':
        cands = [x for x in (CODEX_LOOKALIKES if v['target'] == 'cx_exec' else BAITS) if v['os'] == 'linux' or x not in ('other_user', 'pid_reuse')]
        v['bait'] = cands[h % len(cands)]
    elif case.bundle == 'deb':
        v['role'] = NEGATIVE_ROLES[h % len(NEGATIVE_ROLES)]
    elif case.bundle == 'sta':
        if v['life'] not in axes.RESUMABLE or v['skind'] == 'main':
            return None
        v['at'] = 'after_resume'                      # the old error must not overrule the run that followed it
    else:
        return None
    t = normalize(Case(case.bundle, v))
    t.twin_of = case.id
    return t if t.key() != case.key() else None


def foreign_twin_of(case):
    """The second twin of an affiliation positive: the launch stays exactly as it is (way, spawner, output file, folder, timing, when the board looked) and only
    the child is a stranger (it was asked something else, nothing of Claude is above its process, the output file names another id). It tests the evidence
    path of the positive itself; the classic twin above replaces the launch by a lookalike. Only a launch with a complete literal argument can be refuted."""
    if case.bundle != 'aff' or case.v['target'] not in ('cli', 'cx_exec') or case.v['bait'] != 'none' or case.v['form'] in ('nopersist', 'deleted'):
        return None
    if case.v['out'] == 'overwritten' or case.v['author'] != 'self':
        return None                                   # these shapes are themselves the lookalike: the launch is read from a file or the proof belongs to another call
    t = normalize(Case('aff', dict(case.v, bait='foreign')))
    t.twin_of = case.id
    return t if t.v['bait'] == 'foreign' and t.key() != case.key() else None


def twins_of(case):
    """Every decoy twin of a positive (one or two)."""
    return [t for t in (twin_of(case), foreign_twin_of(case)) if t is not None]


def shapes():
    """The shapes the pairwise cover cannot reach: each is a product of a few values of one new axis (or value) with the values it interacts with. Real records show
    these; the first generator did not draw them."""
    out = []
    n = 0
    # an instruction file written by somebody else: who wrote it x what that session was running x how the launch reads it x what the board saw
    for author, busy, (via, src), seen in itertools.product(('peer', 'main', 'sub'), AXES['busy'], (('subst', 'prior'), ('stdin', 'write')), ('live', 'ended_unseen')):
        n += 1
        out.append(normalize(Case('aff', dict(author=author, busy=busy, via=via, src=src, seen=seen, way='bg' if n % 2 else 'direct'))))
    # a person started the child in a terminal and a session only wrote the instruction file: nobody's call launched it, whatever the author was running
    n = 0
    for author, busy, seen in itertools.product(('peer', 'main', 'sub'), ('py', 'sh', 'unittest', 'idle', 'script_unread', 'script_var'), ('live', 'ended_unseen')):
        n += 1
        out.append(normalize(Case('aff', dict(starter='person', author=author, busy=busy, seen=seen, src='prior' if n % 2 else 'write'))))
    # the output path of another launch, written over later by a resume run of the real child
    for way, seen in itertools.product(('direct', 'bg'), AXES['seen']):
        out.append(normalize(Case('aff', dict(out='overwritten', way=way, seen=seen))))
    # the folder of a launch that `pushd`s, or that `cd`s to a variable nothing defines (a stranger beside it must not be taken for the child)
    for cwd, bait, (via, src), seen, way in itertools.product(('pushd', 'var'), ('none', 'foreign'), (('arg', 'call'), ('subst', 'prior')), ('live', 'ended_unseen'), ('direct', 'bg')):
        out.append(normalize(Case('aff', dict(cwd=cwd, bait=bait, via=via, src=src, seen=seen, way=way))))
    # Codex: the lookalikes of an exec thread, and a stranger thread beside a real launch
    for bait, way, seen, spawner in itertools.product(axes.CODEX_BAITS, ('direct', 'bg', 'later'), ('live', 'ended_unseen'), ('main', 'sub')):
        out.append(normalize(Case('aff', dict(target='cx_exec', bait=bait, way=way, seen=seen, spawner=spawner))))
    # the process table of the first look is gone at the second (the board restarted): only the cache can tell two launches with the same words apart
    for decoy, way, (via, src) in itertools.product(('twin_text', 'concurrent'), ('direct', 'bg'), (('arg', 'call'), ('subst', 'prior'))):
        out.append(normalize(Case('aff', dict(decoy=decoy, way=way, via=via, src=src, seen='restart_seen'))))
    # Codex exec threads that start late (a `sleep` before the launch), alive or after the run, with and without a process table that names the open file
    for way, seen, osk, spawner in itertools.product(('direct', 'bg', 'later'), ('live', 'ended_unseen'), ('linux', 'mac'), ('main', 'sub')):
        out.append(normalize(Case('aff', dict(target='cx_exec', way=way, seen=seen, os=osk, spawner=spawner))))
    # a run that was resumed and then stopped again
    for life, osk in itertools.product(axes.LIFE_BY_KIND['cli'], ('linux', 'mac_nops')):
        out.append(normalize(Case('sta', dict(skind='cli', life=life, at='resume_stopped', os=osk))))
    # debate: a participant whose process cannot be seen (macOS without `ps`) and whose record is quiet
    for kind, life, osk, fstate in itertools.product(AXES['kind'], ('running', 'stalled_silent', 'normal_end'), AXES['os'], ('none', 'written')):
        out.append(normalize(Case('deb', dict(kind=kind, life=life, os=osk, fstate=fstate))))
    # debate: a reader that only names the report, in both languages, and the other roles in Korean
    for role, kind, lang, structure in itertools.product(('ref_reader',), AXES['kind'], AXES['lang'], ('single', 'topics', 'flat')):
        out.append(normalize(Case('deb', dict(role=role, kind=kind, lang=lang, structure=structure))))
    out.append(normalize(Case('deb', dict(role='ref_reader', nstyle='lower', lang='ko'))))
    for role, kind in itertools.product(('writer', 'reader', 'quoter', 'negator', 'ghost', 'tag_only', 'failed_write'), AXES['kind']):
        out.append(normalize(Case('deb', dict(role=role, kind=kind, lang='ko', marker='own' if role == 'writer' else 'none'))))
    # debate: a marker that is only quoted
    for kind, lang in itertools.product(AXES['kind'], AXES['lang']):
        out.append(normalize(Case('deb', dict(role='quoter', marker='quoted', kind=kind, lang=lang))))
    # debate: round folders of two spellings side by side, and an output file of the launch beside the report in the round folder
    for kind, fstate, structure in itertools.product(AXES['kind'], ('none', 'written'), ('single', 'topics')):
        out.append(normalize(Case('deb', dict(rdir='both', kind=kind, fstate=fstate, structure=structure))))
    for kind, rpath, marker in itertools.product(AXES['kind'], ('instr_only', 'var_ext'), ('own', 'cross')):          # only a marker names the seat: which folder is it?
        out.append(normalize(Case('deb', dict(rdir='both', kind=kind, rpath=rpath, marker=marker, structure='topics'))))
    for kind, life, fstate in itertools.product(('cli', 'codex'), ('running', 'normal_end'), ('none', 'written')):
        out.append(normalize(Case('deb', dict(rpath='dash_o_aux', kind=kind, life=life, fstate=fstate))))
    # debate: the launch also writes the report's own name into the other spelling of the round folder, and the report's own file is or is not there
    for kind, life, fstate, structure, rpath in itertools.product(('cli', 'codex'), ('running', 'normal_end'), ('none', 'written'), ('single', 'topics'), ('abs', 'short')):
        out.append(normalize(Case('deb', dict(aux='alias', kind=kind, life=life, fstate=fstate, structure=structure, rpath=rpath))))
    # debate: the participant writes with a Bash command (redirect, `tee`, heredoc) where the Write tool was, in the scenes where the write is the proof of the seat
    scenes = (dict(rpath='abs'), dict(rpath='short'), dict(rpath='var_ext', marker='none', nstyle='numbered'), dict(rpath='var_ext', marker='own', nstyle='collide'),
              dict(rpath='instr_only', marker='own', rdir='both'), dict(rpath='instr_only', marker='own', nstyle='named'), dict(rpath='short', homonym='two'),
              dict(structure='flat', homonym='two'))
    modes = ('redirect', 'tee', 'heredoc')
    for scene, wmode, kind in itertools.product(scenes, modes, ('sub', 'cli')):
        out.append(normalize(Case('deb', dict(scene, wmode=wmode, kind=kind, fstate='written'))))
    # ... and where it must not be: a reader that keeps a note of its own, a document or a tag-only participant whose command only quotes a redirect to the report, a write that failed
    for role, wmode, kind in itertools.product(('reader', 'quoter', 'tag_only', 'failed_write'), modes, ('sub', 'cli')):
        out.append(normalize(Case('deb', dict(role=role, wmode=wmode, kind=kind))))
    # debate: two flat reviews that declare the same result file
    for kind, fstate in itertools.product(('cli', 'codex'), ('none', 'written')):
        out.append(normalize(Case('deb', dict(structure='flat', homonym='two', kind=kind, fstate=fstate))))
    # debate: an editing job beside the topics of a review (a guide with bold finding numbers and no round folder), whatever the participant of the topic does
    for kind, role, rdir in itertools.product(AXES['kind'], ('writer', 'reader', 'quoter', 'absent'), ('r1', 'round1')):
        out.append(normalize(Case('deb', dict(structure='topics', edits='beside', kind=kind, role=role, rdir=rdir))))
    return out


_SELECTED = []                    # the selection is deterministic and costs 3 s: made once per process


def select():
    """The case list, deterministic: real anchors, pairwise per bundle, the full risky product, launcher x life, and a decoy twin per positive."""
    if _SELECTED:
        return list(_SELECTED)
    chosen = collections.OrderedDict()

    def add(c):
        if c.id not in chosen:
            chosen[c.id] = c
        elif c.real and chosen[c.id].real:                     # two real cases that are the same scene (A13 and C13): one case, both names
            have = chosen[c.id].real.split('/')
            chosen[c.id].real = '/'.join(have + [n for n in c.real.split('/') if n not in have])

    reals = axes.real_cases()
    for name, c in reals:
        add(c)
    for bundle, names in PAIR_AXES.items():
        seed = [c for _, c in reals if c.bundle == bundle]
        for c in pairwise(bundle, names, seed_cases=seed):
            add(c)
    core = list(chosen.values())                          # the real cases and the pairwise cover: each positive of these gets a decoy twin
    # the risky core: how it is launched x how the instruction travels x what shape the session has (11 x 5 x 5 = 275 before folding)
    for way, via, form in itertools.product(AXES['way'], AXES['via'], AXES['form']):
        add(normalize(Case('aff', {'way': way, 'via': via, 'form': form})))
    # the debate's risky core: how the path is written x what the participant does with it x what kind of agent it is
    for rp, role, kind in itertools.product(AXES['rpath'], ('writer', 'reader', 'quoter', 'negator'), AXES['kind']):
        add(normalize(Case('deb', {'rpath': rp, 'role': role, 'kind': kind})))
    # launcher x life in the integrated scene
    for sp, life in itertools.product(AXES['spawner'], axes.CPL_LIFE):
        add(normalize(Case('cpl', {'spawner': sp, 'life': life})))
    # kind x life in the state bundle (every life of every kind, with and without a resume)
    for sk, life in itertools.product(AXES['skind'], AXES['life']):
        c = normalize(Case('sta', {'skind': sk, 'life': life}))
        add(c)
    for c in core:
        if c.twin_of is None and is_positive(c):
            for t in twins_of(c):
                add(t)
    for c in core:
        c.core = True
    for c in shapes():
        add(c)
    _SELECTED.extend(chosen.values())
    return list(_SELECTED)


def run_all(cases, root=None, progress=False):
    """Runs every case; returns (cells, errors). A case that raises is an error of the generator, never a pass."""
    own = root is None
    root = root or tempfile.mkdtemp(prefix='scenarios-')
    cells, errors = [], []
    try:
        for i, c in enumerate(cases):
            try:
                cells += run_case(c, root)
            except Exception as e:   # noqa: BLE001 - reported with the case id, the run goes on
                errors.append((c.id, '%s: %s' % (type(e).__name__, e)))
    finally:
        if own:
            shutil.rmtree(root, ignore_errors=True)
    return cells, errors


# ---------------------------------------------------------------------------------------------------------------------
# why a cell is red: the reason table (each red cell gets exactly one; a cell with no reason fails the xfail writer)
# ---------------------------------------------------------------------------------------------------------------------
REASONS = {
    # affiliation
    'A-NODE': ('link/views', 'No node: a child is always listed under the main session; no field says which sub-agent launched it (link.py cli_owners, sessions.py Session.poll, views.py agents).'),
    'A-BY': ('link/views', 'No `by`: a resumed run does not record who handed it its instruction (link.py reads the first run only; views.py has no field).'),
    'A-RULE-CLASS': ('link', 'Only the time and lineage rules exist: an instruction/output-file/`--resume` proof is never read, so a link that could be certain is shown as a guess or not at all (link.py CERTAIN_RULES, _link_cli).'),
    'A-TREE-WINDOW': ('link', 'Fixed time window after the launching call (`claude -p`: 60 s, link.py CLI_WINDOW; a Codex thread: 30 s, link.py cx_link): a child that starts later (sequential loop, `sleep && claude`, `sleep && codex exec`) is lost, and it is not even listed as unlinked.'),
    'A-TREE-SUBAGENT': ('link', 'Calls made by a sub-agent are not read: only main-session records are scanned (link.py LinkIndex.scan globs top-level records), so its child is unlinked.'),
    'A-TREE-COMMAND': ('link', 'The command reader does not recognise the launch (`env -i`, tmux, xargs, python subprocess ...) and there is no content rule to fall back on (link.py CLAUDE_LAUNCH_RE).'),
    'A-TREE-NOEVIDENCE': ('link', 'The only proof is the instruction text, an output file or an environment the board cannot use here (no content rule, no output-file rule, `ps` unusable): the child stays unlinked.'),
    'A-TREE-AMBIGUOUS': ('link', 'Two launches fit the same child (same folder, same moment): the earliest child is given to the earliest call instead of being held as ambiguous (link.py _link_cli).'),
    'A-TREE-CONTRADICTION': ('link', 'A launch whose literal prompt differs from the child\'s first instruction (or, with the same words, whose known folder differs) is taken for its parent: the time rule never compares the text. Also a stranger child that shares only the folder and the moment.'),
    'A-TREE-RESUME': ('link/lineage', 'A resumed child is given the parent of the run that resumed it: the process and environment of run 2 are used for the tree instead of only for `by`; the tree is the one of the first run (link.py _link_cli, lineage.py).'),
    'C-GRAND-SCREEN': ('views/debates', 'The grand-child of a `claude -p` child is not on the top orchestrator\'s page: its status, cell and seat are read from that page (under its launcher) and the board does not put it there yet. Decide the screen shape first.'),
    'A-TREE-AUTHOR': ('link', 'The instruction was written by another session (or the other node of the tree) that was only running a script or an unrelated launch: the text found in its records is taken for the proof of who launched the child, and the real launcher is left with a childless launch (the content rule counts any running call as the launch).'),
    'A-TREE-NO-LAUNCH': ('link', 'A person started the child in a terminal and a session only wrote the instruction: that session (or the page\'s main session) is taken for the parent although no call of it started anything, or a script it was running whose body cannot be read gives a certain link.'),
    'A-TREE-STALE-OUT': ('link', 'An output file that holds the child\'s id is credited to the first call that redirected to the path, although another session started the child and a later run wrote the file over (the proof is not checked against who wrote it when).'),
    'A-TREE-PUSHD': ('link', '`pushd X && claude -p ...` is not read as a change of folder: the launch keeps the session\'s folder, is refuted by it, and no parent is found; the launch is counted as one with no child.'),
    'A-TREE-CODEX': ('link', 'A Codex exec thread is linked to a `codex exec` call by time and folder alone: the words of the call and its `-C` folder are not compared with the thread, so a stranger thread beside a real launch is taken for its child.'),
    'A-TREE-UNKNOWN-DIR': ('link', 'A launch whose folder nothing tells (`cd "$VAR"`) fits every child\'s folder in the time rule, so a stranger started in another project is linked to it.'),
    'A-TREE-SWITCH': ('link/lineage', 'The parent process switched sessions (/resume): the lineage follows the process\'s new session while the output file proves the old one; the higher-ranked proof is ignored (lineage.py parent_session).'),
    'A-UNLINKED': ('link', 'The unlinked list does not say `ambiguous` for a child two launches claim (link.py _unlinked).'),
    'D-AFFIL': ('link', 'The affiliation diagnostic differs from the one the records support: one the board shows that the records do not call for (`orphan_launch`), or one it does not show (link.py LINKS.diags).'),
    'D-STATE': ('runstate', 'The state diagnostic differs from the one the records support (runstate.py judge).'),
    'D-DEBATE': ('debates', 'The debate diagnostic differs from the one the records support (debates.py).'),
    'D-PROC': ('runstate', '`ps` unusable and the page says nothing: `proc_unknown` is given per agent (runstate.py judge), so a page with no agent to carry it (a bait, a launch with no record, an unlinked child, a bare orchestrator) is silent. The expected answer is "always when `ps` cannot be read"; whether the page says it once on its own is a design choice.'),
    # state
    'S-INTERRUPTED': ('runstate', 'A run that ended on a limit, an overload or the Bash time limit is shown as `ended` ("session ended"): the error line and `cost-state` are not read (sessions.py agent_status cli branch, agents.py).'),
    'S-FAILED': ('runstate', 'A `failed` notice of a sub-agent is shown as `failed`; the cause (a 429/529 line with `apiErrorStatus` and `quotaLimits`) is only in the sub-agent\'s own record, and the notice names none (sessions.py agent_status notification branch, agents.py).'),
    'S-KILLED': ('runstate', 'A TaskStop of the launching background call is not followed to its child (tool_use id -> backgroundTaskId -> child): the child shows `ended` (sessions.py CodexLinker.note_stop is Codex only).'),
    'S-TORN': ('util/runstate', 'A torn record line (half a record glued to the next) is dropped whole, so the end-of-turn behind it is lost and the finished child shows `ended` (util.py load_json_line).'),
    'S-UNKNOWN': ('runstate', 'No process information (`ps` unusable) is read as "the process is gone": the run shows `ended`, not `unknown` (sessions.py agent_status).'),
    'S-STATUS': ('runstate', 'The status differs from the one the records support.'),
    'S-REASON': ('views', 'No `reason` field: the board does not say why a run stopped (views.py agents).'),
    'S-RESETS': ('views', 'No `resets_at` field: the reset time in the limit record is never shown (views.py agents, lineage.py/link.py `_quota` only feeds the plan bar).'),
    'S-ALERT-FAIL': ('views', 'A limit stop raises a per-agent "failed" check alert instead of one grouped limit notice (views.py alerts).'),
    'S-ALERT-TURN': ('views', 'With an automatic-continue notice the board still says the orchestrator waits for the next instruction (views.py alerts `turn`).'),
    'S-ORCH-SAY': ('sessions', 'The limit text of the orchestrator is reported as something it said to the user (`orch_say`) instead of a limit event (sessions.py _feed_main).'),
    'S-ORCH-STATE': ('views', 'The orchestrator waiting for a limit reset shows `idle`: there is no `limit_wait` state (views.py state).'),
    'S-STRAY': ('sessions/agents', 'A child\'s own background-task notification becomes a fake "work finished" card: `_child_note` does not filter on an agent id (agents.py, sessions.py _route_child_notes).'),
    # debate
    'B-EDIT-CELLS': ('units/debates', 'A folder with a guide that only lists findings by bold numbers (`**C-12**`) and has no round folder is given rows and a round: the numbers are read as participants (units.py ROLE_RE) and round 1 is made up (debates.py _debate).'),
    'B-LISTING-PARENT': ('debates', 'The debate list has a folder that is no debate: the folder that holds the common brief of several topics is listed as one of its own (a card with no rows).'),
    'B-FLAT': ('units/debates', 'A flat review (no r<N> folders, nothing declared) is given an invented round-1 seat from the description tag (debates.py _seat_by_folder).'),
    'B-READER': ('units/debates', 'An agent that only reads (and carries a seat letter in its tag) gets the seat: a reader is not a participant (debates.py _seat_by_folder).'),
    'B-QUOTE': ('units/debates', 'A path that is only quoted, negated or whose write failed seats the agent: write intent is read from the words next to the path, a failed write counts as a write (debates.py write_intent, agents.py writes).'),
    'B-ALIAS-FILE': ('units/debates', 'A file the launch writes under the other spelling of the round folder (`-o r01/B.md` while the instruction says `r1/B.md`) takes the place of the seat\'s own file: the folder and the cell follow it, so a report that was never written shows as submitted.'),
    'B-BASH-WRITE': ('units/debates', 'A write made by a Bash command (redirect, `tee`, heredoc) is not read as a write of the report, or a command that only names the report and writes elsewhere (or one that failed) is.'),
    'B-GHOST': ('units/debates', 'An absolute path outside any debate folder on disk makes a debate (debates.py debates() takes absolute report paths without checking the folder).'),
    'B-PATH': ('units/debates', 'The report path is written as a variable / `-o`/redirect target the path reader does not resolve, or by a spelling it does not know (r01, round1, a name with a prefix): no seat.'),
    'B-HOLD': ('units/debates', 'The path resolves by the agent cwd alone (two debates fit, or a tie): the seat is given instead of held (debates.py).'),
    'B-LISTING': ('units/debates', 'The debate list comes from agent records only: a debate that is on disk but named by no session is not listed (debates.py).'),
    'B-CELL': ('debates/runstate', 'No `paused` cell and no use of the interrupted state: an interrupted agent is read as over (file = done, none = missing) (debates.py _debate).'),
    'B-SEAT': ('units/debates', 'The seat differs from the one the evidence ranks first (spelling kept, path intent first).'),
    'B-UNIT': ('units/debates', 'The debate folder is not found from the path as the agent wrote it.'),
    'B-DEBATE': ('units/debates', 'The debate fact differs from the one the evidence supports.'),
    # coupling
    'C-UNLINKED': ('link', 'The participant was launched by a sub-agent or a child and is not linked, so it shows no status and no cell.'),
}
STATE_FIELDS = ('status', 'reason', 'resets_at', 'orch_state')


def aff_shape_reason(a):
    """The reason of the red cells of an affiliation case built around one of the later shapes (None for the older ones)."""
    if a['starter'] == 'person':
        return 'A-TREE-NO-LAUNCH'
    if a['author'] != 'self':
        return 'A-TREE-AUTHOR'
    if a['out'] == 'overwritten':
        return 'A-TREE-STALE-OUT'
    if a['target'] == 'cx_exec' and a['bait'] != 'none':
        return 'A-TREE-CODEX'
    if a['target'] == 'cx_exec' and a['way'] == 'later':
        return 'A-TREE-WINDOW'
    if a['cwd'] == 'var' and a['bait'] == 'foreign':
        return 'A-TREE-UNKNOWN-DIR'
    if a['cwd'] == 'pushd':
        return 'A-TREE-PUSHD'
    return None


def reason_of(c):
    """The reason id of a red cell, by what the cell is about and which axis values make the scene. None when no rule explains it (the xfail writer refuses)."""
    from board import facts            # noqa: PLC0415  (the diagnostic code lists; the oracle itself never imports board)
    f, a, b = c.field, c.axes, c.bundle
    head = f.split(':')[0].split('=')[0]
    if f.startswith('diag:') and f[5:].split('.')[0] == 'proc_unknown':
        return 'D-PROC'
    if b == 'aff':
        why = aff_shape_reason(a)
        if why:
            return why                                    # a cell of a case that is built around one new shape is about that shape, whichever field shows it
    if head in ('edit_rows', 'edit_rounds'):
        return 'B-EDIT-CELLS'
    if head == 'units':
        return 'B-LISTING-PARENT'
    if f.startswith('diag:'):
        code = f[5:].split('.')[0]
        return 'D-STATE' if code in facts.DIAG_STATE else ('D-AFFIL' if code in facts.DIAG_AFFIL else 'D-DEBATE')
    if head == 'node':
        return 'A-NODE'
    if head == 'by':
        return 'A-BY'
    if head == 'unlinked':
        return 'A-UNLINKED'
    if b == 'cpl' and head in ('status', 'reason', 'resets_at', 'unit', 'round', 'seat', 'role', 'cell', 'placements') and a['spawner'] != 'main' \
            and (c.gs in ('MISSING', None, 'none')):
        return 'C-GRAND-SCREEN' if a['spawner'] == 'grand' else 'C-UNLINKED'
    if b == 'cpl' and head == 'rule_class' and a['spawner'] != 'main' and (c.gs in ('MISSING', None, 'none')):
        return 'C-UNLINKED'
    if head in ('tree', 'rule_class') or (head == 'forbid' and f.startswith('forbid:tree')):
        if a.get('bait') in ('text_mismatch', 'cwd_mismatch', 'echo_only', 'foreign'):
            return 'A-TREE-CONTRADICTION'
        if a.get('form') == 'resume' and head == 'tree':
            return 'A-TREE-RESUME'
        if a.get('decoy') == 'switch':
            return 'A-TREE-SWITCH'
        if a.get('decoy') in ('concurrent', 'twin_text') and (c.result == 'wrong' or head == 'tree'):
            return 'A-TREE-AMBIGUOUS'
        if head == 'rule_class':
            return 'A-RULE-CLASS' if c.result == 'miss' else 'A-TREE-AMBIGUOUS'
        if head == 'forbid':
            return 'A-TREE-AMBIGUOUS'
        if a['spawner'] == 'sub':
            return 'A-TREE-SUBAGENT'
        if a['timing'] == 'seq_late' or a['way'] in ('later', 'loop'):
            return 'A-TREE-WINDOW'
        if a['way'] in ('envi', 'tmux', 'xargs', 'pysub'):
            return 'A-TREE-COMMAND'
        return 'A-TREE-NOEVIDENCE'
    if head in STATE_FIELDS or head == 'forbid' and (f.startswith('forbid:alert') or f.startswith('forbid:event')):
        if head == 'status':
            if c.ws == 'unknown':
                return 'S-UNKNOWN'
            if c.ws == 'killed':
                return 'S-KILLED'
            if a.get('flaw') == 'torn' and c.ws == 'done':
                return 'S-TORN'                           # only the lost end-of-turn; an interrupted/failed truth below needs the error reading as well
            if c.ws == 'interrupted' and c.gs == 'failed':
                return 'S-FAILED'
            if c.ws in ('interrupted', 'failed') and c.gs == 'ended':
                return 'S-INTERRUPTED'
            return 'S-STATUS'
        if head == 'reason':
            return 'S-REASON'
        if head == 'resets_at':
            return 'S-RESETS'
        if head == 'orch_state':
            return 'S-ORCH-STATE'
        if f.startswith('forbid:alert=fail'):
            return 'S-ALERT-FAIL'
        if f.startswith('forbid:alert=turn'):
            return 'S-ALERT-TURN'
        if f.startswith('forbid:event=orch_say_limit'):
            return 'S-ORCH-SAY'
        if f.startswith('forbid:event=notify_stray'):
            return 'S-STRAY'
    if head in ('unit', 'round', 'seat', 'cell', 'role', 'placements'):
        if a.get('aux') == 'alias':
            return 'B-ALIAS-FILE'
        if a.get('wmode', 'tool') != 'tool':
            return 'B-BASH-WRITE'
        role = a.get('role')
        if role in ('quoter', 'negator', 'tag_only', 'failed_write'):
            return 'B-QUOTE'
        if role == 'reader':
            return 'B-READER'
        if role == 'ghost':
            return 'B-GHOST'
        if role == 'absent':
            return 'B-LISTING'
        if role == 'rival' or a.get('homonym') == 'two':
            return 'B-HOLD'
        if a.get('structure') == 'flat':
            return 'B-FLAT'
        if head == 'cell' and (c.ws == 'paused' or a.get('life') in ('limit_exit', 'time_limit_kill', 'api_529')):
            return 'B-CELL'
        if a.get('rpath') in ('var', 'var_ext', 'dash_o', 'redirect', 'dash_o_last') or a.get('rdir') != 'r1' or a.get('nstyle') in ('numbered', 'lower', 'collide'):
            return 'B-PATH'
        return {'seat': 'B-SEAT', 'unit': 'B-UNIT'}.get(head, 'B-DEBATE')
    return None


# ---------------------------------------------------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------------------------------------------------
XFAIL = os.path.join(REPO, 'tests', 'scenarios_xfail.json')


def _count(cells, keyf):
    out = collections.defaultdict(collections.Counter)
    for c in cells:
        k = keyf(c)
        out[k]['cells'] += 1
        if c.result == 'pass':
            out[k]['pass'] += 1
        elif c.result == 'wrong':
            out[k]['wrong'] += 1
        elif c.absent:
            out[k]['absent'] += 1
        else:
            out[k]['miss'] += 1
    return out


def _row(*cols):
    return '| ' + ' | '.join(str(x) for x in cols) + ' |'


def summary_text(cases, cells, errors=(), seconds=None):
    """The summary the run prints (and the test prints): counts per bundle and field, and the red cells per reason."""
    red = [c for c in cells if c.result != 'pass']
    lines = ['scenarios: %d cases (%s; %d decoy twins), %d cells: %d pass, %d miss (%d of them the field is not emitted), %d wrong%s' % (
        len(cases), ', '.join('%s %d' % (b, n) for b, n in sorted(collections.Counter(c.bundle for c in cases).items())),
        sum(1 for c in cases if c.twin_of), len(cells), len(cells) - len(red), sum(1 for c in red if c.result == 'miss'), sum(1 for c in red if c.absent),
        sum(1 for c in red if c.result == 'wrong'), '' if seconds is None else ' in %.1f s' % seconds)]
    if errors:
        lines.append('GENERATOR ERRORS: %d (first: %s)' % (len(errors), errors[0]))
    lines.append('')
    lines.append('%-5s %-18s %6s %6s %7s %6s %6s' % ('', 'field', 'cells', 'pass', 'absent', 'miss', 'wrong'))
    for (b, f), n in sorted(_count(cells, lambda c: (c.bundle, c.field.split(':')[0].split('=')[0])).items()):
        lines.append('%-5s %-18s %6d %6d %7d %6d %6d' % (b, f, n['cells'], n['pass'], n['absent'], n['miss'], n['wrong']))
    lines.append('')
    lines.append('red cells per reason (module):')
    per = collections.Counter(reason_of(c) for c in red)
    for rid, n in sorted(per.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        module, _ = REASONS.get(rid, ('?', ''))
        lines.append('  %5d  %-22s %s' % (n, rid, module))
    return '\n'.join(lines)


def red_md(cases, cells):
    """The table of red cells per axis value, as Markdown. No dates and no ids, so a rerun with the same board diffs to nothing."""
    red = [c for c in cells if c.result != 'pass']
    out = ['# Red cells of the scenario generator',
           '',
           'Written by `python3 -m tools.scenarios.run --md`. It lists where the board as it is today does not match',
           'the truth that `tools/scenarios/oracle.py` computes from the axis values. Cell = one (case, subject, field). `absent` = a miss because the board does not',
           'emit the field at all; `miss` = it emits nothing/null where the truth has a value; `wrong` = it shows something the records do not support (a false link,',
           'seat or state: the one to fix first). The exact list, with the value the board shows today and the reason, is `tests/scenarios_xfail.json`.',
           '',
           '%d cases (%s), %d decoy twins, %d cells: %d pass, %d absent, %d miss, %d wrong.' % (
               len(cases), ', '.join('%s %d' % (b, n) for b, n in sorted(collections.Counter(c.bundle for c in cases).items())), sum(1 for c in cases if c.twin_of),
               len(cells), len(cells) - len(red), sum(1 for c in red if c.absent), sum(1 for c in red if c.result == 'miss' and not c.absent),
               sum(1 for c in red if c.result == 'wrong')),
           '']
    out += ['## By field', '', _row('bundle', 'field', 'cells', 'pass', 'absent', 'miss', 'wrong'), _row(*['---'] * 7)]
    for (b, f), n in sorted(_count(cells, lambda c: (c.bundle, c.field.split(':')[0].split('=')[0])).items()):
        out.append(_row(b, f, n['cells'], n['pass'], n['absent'], n['miss'], n['wrong']))
    out += ['', '## By reason', '', _row('reason', 'module', 'red cells', 'wrong', 'what the board does'), _row(*['---'] * 5)]
    by_reason = _count(red, reason_of)
    for rid in sorted(by_reason, key=lambda r: (-(by_reason[r]['wrong'] + by_reason[r]['miss'] + by_reason[r]['absent']), str(r))):
        n = by_reason[rid]
        module, text = REASONS.get(rid, ('?', '?'))
        out.append(_row(rid, module, n['wrong'] + n['miss'] + n['absent'], n['wrong'], text))
    case_n = collections.Counter()
    for c in cases:
        for a in c.axes:
            case_n[(c.bundle, a, c.v[a])] += 1
    for bundle in BUNDLES:
        out += ['', '## By axis value: %s' % bundle, '',
                _row('axis', 'value', 'cases', 'red cells', 'wrong', 'miss', 'absent', 'cells'), _row(*['---'] * 8)]
        per = collections.defaultdict(collections.Counter)
        for c in cells:
            if c.bundle != bundle:
                continue
            for a in BUNDLES[bundle]:
                k = (a, c.axes[a])
                per[k]['cells'] += 1
                if c.result == 'wrong':
                    per[k]['wrong'] += 1
                elif c.result == 'miss':
                    per[k]['absent' if c.absent else 'miss'] += 1
        for a in BUNDLES[bundle]:
            for val in AXES[a]:
                n = per.get((a, val))
                if n is None:
                    continue
                out.append(_row(a, val, case_n[(bundle, a, val)], n['wrong'] + n['miss'] + n['absent'], n['wrong'], n['miss'], n['absent'], n['cells']))
    out += ['', '## The real cases', '', _row('case', 'bundle', 'red cells', 'wrong', 'fields (wrong marked *)'), _row(*['---'] * 5)]
    for c in cases:
        if not c.real:
            continue
        mine = [x for x in red if x.case == c.id]
        names = sorted({x.role + '.' + x.field + ('*' if x.result == 'wrong' else '') for x in mine})
        out.append(_row(c.real, c.bundle, len(mine), sum(1 for x in mine if x.result == 'wrong'), ', '.join(names) or 'none'))
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------------------------------------------------------------
# strict xfail
# ---------------------------------------------------------------------------------------------------------------------
def xfail_doc(cells):
    """The xfail document of the current red cells. Refuses a cell no reason explains."""
    red = [c for c in cells if c.result != 'pass']
    unexplained = [c for c in red if reason_of(c) is None]
    if unexplained:
        raise ValueError('no reason for %d red cells, first: %s %s -> %s' % (len(unexplained), unexplained[0].key, unexplained[0].ws, unexplained[0].gs))
    used = sorted({reason_of(c) for c in red})
    doc = {'version': 1,
           'about': 'Red cells of tools/scenarios today: every one is expected to stay red until the board is fixed, and fails the test if it turns green or changes. '
                    'cells[case]["subject.field"] = [result, what the truth wants, what the board shows, reason id]. Regenerate with python3 -m tools.scenarios.run --write-xfail.',
           'reasons': {r: {'module': REASONS[r][0], 'text': REASONS[r][1]} for r in used},
           'cells': {}}
    for c in red:
        doc['cells'].setdefault(c.case, {})['%s.%s' % (c.role, c.field)] = [c.result, c.ws, c.gs, reason_of(c)]
    return doc


def dump_xfail(doc):
    """One line per case so a diff shows which cases changed."""
    import json
    lines = ['{', '"version": %d,' % doc['version'], '"about": %s,' % json.dumps(doc['about']), '"reasons": %s,' % json.dumps(doc['reasons'], indent=1, sort_keys=True), '"cells": {']
    items = sorted(doc['cells'].items())
    for i, (cid, cs) in enumerate(items):
        lines.append('%s: %s%s' % (json.dumps(cid), json.dumps(cs, sort_keys=True, separators=(',', ':')), ',' if i < len(items) - 1 else ''))
    lines += ['}', '}']
    return '\n'.join(lines) + '\n'


def compare_xfail(cells, doc):
    """Problems of the run against the xfail document (empty = the red list is exactly right)."""
    problems = []
    seen = set()
    for c in cells:
        key = '%s.%s' % (c.role, c.field)
        listed = doc['cells'].get(c.case, {}).get(key)
        if c.result == 'pass':
            if listed:
                problems.append('now passes, remove from the xfail list: %s %s' % (c.case, key))
            continue
        seen.add((c.case, key))
        if not listed:
            problems.append('NEW %s: %s %s truth=%r board=%r' % (c.result.upper(), c.case, key, c.ws, c.gs))
        elif [listed[0], listed[1], listed[2]] != [c.result, c.ws, c.gs]:
            problems.append('CHANGED %s %s: listed %s want=%r got=%r, now %s want=%r got=%r' % (c.case, key, listed[0], listed[1], listed[2], c.result, c.ws, c.gs))
    run_cases = {c.case for c in cells}
    for cid, cs in doc['cells'].items():
        if cid in run_cases:
            for key in cs:
                if (cid, key) not in seen and not any(c.case == cid and '%s.%s' % (c.role, c.field) == key for c in cells):
                    problems.append('stale xfail entry (no such cell any more): %s %s' % (cid, key))
        else:
            problems.append('stale xfail entry (no such case any more): %s' % cid)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--md', nargs='?', const='-', metavar='PATH', help='also write the table of red cells to PATH (standard output when no path is given)')
    ap.add_argument('--write-xfail', action='store_true', help='rewrite the list of expected red cells from this run (the only writer of that file)')
    ap.add_argument('--cases', default='*', help='only the cases whose id matches this glob')
    ap.add_argument('--list', action='store_true', help='print the case ids and stop')
    args = ap.parse_args(argv)
    cases = [c for c in select() if fnmatch.fnmatch(c.id, args.cases)]
    if args.list:
        for c in cases:
            print(c.id)
        return 0
    t0 = time.time()
    cells, errors = run_all(cases)
    dt = time.time() - t0
    print(summary_text(cases, cells, errors, dt))
    if args.md == '-':
        print()
        sys.stdout.write(red_md(cases, cells))
    elif args.md:
        with open(args.md, 'w') as f:
            f.write(red_md(cases, cells))
        print('wrote', args.md)
    if args.write_xfail:
        with open(XFAIL, 'w') as f:
            f.write(dump_xfail(xfail_doc(cells)))
        print('wrote', os.path.basename(XFAIL))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
