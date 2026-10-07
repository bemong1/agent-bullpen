"""Debate folders found on disk, and the pure judgment of who sits where in them.

Three questions, answered apart: which folders are debates (`Catalog`), who holds which seat and what each cell shows (`assign`), and when a debate is over (`Final`). Nothing here
reads a session and nothing reads a word of a text: `debates.py` turns an agent into `AgentFacts`, a plain structure with no sentence in it, and `assign` returns the Judgement.
The judgment is a pure function of (the facts, the disk as `Catalog` answers it): the same input gives the same Judgement, and it reads no clock (an open window runs to the end of time).

What the code below keeps:
  - A folder is a debate when it has an exact round folder (r1, r01, round1), or two session authors' confirmed tool/shell writes of different seats, with two seats in one file round (O23). A `brief.md`, `README.md` or `index.md` is its guide next to those rounds. The folder
    that holds a guide and the topics below it is the root of a bundle.
  - A cell is a file `<unit>/<round folder>/<name>.md`. It exists when the file is on disk, a write that is sure made it, or a launch command or a room tag names it. No sentence makes one.
  - A write is sure (`J1`) when its result says it worked and the tool or the exit status of the whole command decides it; the others (a command whose later part can hide a failure,
    a `-o` file, a `claude -p` redirect) are sure only when the named file was really saved in the time the command ran (`F`, judged here, on the disk as it is now).
  - The owner of a cell is the first agent whose sure write made the whole file; the others are editors. Two such writes within TIE_SEC, a write by another agent that may have made the
    file first (a try that is not sure, a failure too, from before the first save to TIE_SEC after it), or a record that was not read hold the cell (`seat_tie_held`, `history_lost`): nobody is made the owner by guessing.
  - Whether a cell is submitted is the run's own business: an agent that was asked to save the file in this run (`Planned`, a room tag's seat) has submitted it when a sure write of that run
    or a later one follows (an agent that was resumed and asked nothing of the file in its new run keeps its earlier save submitted). What is only on disk from before is `previous`.
  - What is uncertain only holds things back: a try that failed, a result that is not known, a save that cannot be checked, a file that changed with no event never remove a competitor and never
    make a final; a document that came after the reports with no event at all competes with the final, and an agent whose state nobody gave may still be working.
  - A room is a folder that the records make one (two agents with the same room tag, or launched together with a file each in it): never a word.

Standard library only; Python 3.9 compatible.
"""

import bisect
import collections
import dataclasses
import hashlib
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .facts import AUTHORING, CHECKED, FINAL_WHY, Assignment, CmdWindow, Evidence, Hint, LaunchKey, Planned, ReadEvent, Tag, Unit, WriteEvent
from .util import FILE_MAX, HOME, Denied, open_safe

GUIDE_NAMES = ('brief.md', 'README.md', 'index.md')
ROUND_DIR_RE = re.compile(r'(?:r|round)(\d+)\Z')
ROUND_FILE_RE = re.compile(r'(?:r|round)(\d+)_([^/]+)\.md\Z', re.I)
_RN = r'(?:r|round)(\d+)'
# a report path: <folder>/<r|round><N>/<name>.md. Left boundary: it does not start in the middle of a path ('.../t2_error/…', '…/x/…', 'a/b/…')
REPORT_RE = re.compile(r'((?<![\w.~/…}-])(?:~|/)[^\s`\'"()<>|,*]*?)/' + _RN + r'/([A-Za-z0-9_.-]+?)\.md')
_HAS_DIR_RE = re.compile(r'/(?:r|round)\d+/')
LETTER_PATH_RE = re.compile(r'^(.*)/' + _RN + r'/([^/]+?)\.md$')

TIE_SEC = 1.0              # two sure writes that made a file this close in time: which came first is not told by the records
SAME_TIME = 2.0            # seconds. Within this, times count as the same (depending on copy order, a conclusion file can be written 1 ms before the reports)
W4_GRACE = 2.0             # a launch command's output may be saved this long after the run ended and still be its save
WIN_EPS = 0.05             # the slack of a command window against the time of a file
LIVE = ('running', 'stalled')
OPEN = LIVE + ('unknown',)             # statuses of an agent that may still be working: `unknown` means its process cannot be seen, not that it ended
NOT_SUBMITTED = ('writing', 'draft', 'paused', 'missing', 'waiting')        # the states of a cell that is not submitted (J15 `open_cell`)
ROUND_DOC_RE = re.compile(r'^(brief|round\d+.*|r\d+[_-].*)\.md$', re.I)      # the brief and the round instruction files are not conclusions
# the rule for putting the candidates of a final in order (only the order: the text of a document is never read)
CONCLUDE_RE = re.compile(r'ruling|final|verdict|decision|conclusion|summary|plan|(?<![a-z])clos(?:e|ing|ure)|결론|판정|합의|최종|정리|종결|마무리', re.I)
COMMON_FOLDERS = ('docs', 'doc')               # a guide directly in these folders of a repository is a document of the repository, not of a room
STATE_DIRS = tuple(os.path.join(HOME, d) + os.sep for d in ('.claude', '.codex'))          # what an agent keeps of itself is no edit of the work
SCRATCH_DIRS = tuple(sorted({os.path.normpath(d) + os.sep for d in ('/tmp', '/var/tmp', '/private/tmp', '/var/folders', '/private/var/folders', tempfile.gettempdir(),
                                                                    os.environ.get('TMPDIR') or '/tmp') if os.path.isabs(d)}))        # where a file kept for scratch work goes
INF = float('inf')


# ---------------------------------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------------------------------
def round_of(name):
    """Round number of a round folder name (r1, r01, round1), or None."""
    m = ROUND_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def file_round_of(name):
    """(round, seat) of round<N>_<seat>.md or r<N>_<seat>.md (O23), or None."""
    m = ROUND_FILE_RE.fullmatch(name)
    return (int(m.group(1)), m.group(2).lower()) if m else None


def final_sort_key(name, mtime):
    """The order of the candidates of a final: a name that looks like a conclusion first, then the latest, then the name. Only an order."""
    return (CONCLUDE_RE.search(name[:-3] if name.endswith('.md') else name) is None, -(mtime or 0), name)


def report_of_path(path):
    """(folder, round, name, round folder name) of an absolute report path <folder>/<r|round><N>/<name>.md, or None."""
    if not path.endswith('.md'):
        return None
    m = LETTER_PATH_RE.match(path)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3), path[len(m.group(1)) + 1:].split('/')[0]


# ---------------------------------------------------------------------------------------------------------------------
# the disk, as the judgment looks at it
# ---------------------------------------------------------------------------------------------------------------------
def too_broad(path):
    """A folder that is never a debate or a root of one: the filesystem root, HOME, an ancestor of HOME, the temporary folders. `path` is normalised."""
    return path in ('/', '/tmp', '/var/tmp') or HOME == path or HOME.startswith(path + os.sep)


def _kind(mode):
    return 'f' if stat.S_ISREG(mode) else 'd' if stat.S_ISDIR(mode) else 'l' if stat.S_ISLNK(mode) else 'o'


def _stat_answer(st):
    return (_kind(st.st_mode), st.st_ino, st.st_size, st.st_mtime_ns)


def _real_via(path, prefix):
    """`os.path.realpath(path)` for a path that is absolute and has no `.`, `..` or doubled slash, with the real path of every folder above it kept in `prefix` ({a path: its real path}): one `lstat` for a
    folder however many paths run through it, where `realpath` asks one for every name of every path, and only the names below the nearest folder already known are walked. A name that is a link is left
    to `realpath` (it is one lookup of its own); one that is not there is taken as it is, as `realpath` does. Any other form of path is `realpath`'s."""
    if not path.startswith('/') or path.startswith('//') or '\0' in path or path != os.path.normpath(path):
        return os.path.realpath(path)
    todo, here = [], path
    while True:
        real = prefix.get(here)
        if real is not None:
            break
        if here == '/':
            real = prefix['/'] = '/'
            break
        todo.append(here)
        here = here.rsplit('/', 1)[0] or '/'
    for here in reversed(todo):
        cand = (real if real != '/' else '') + '/' + here.rsplit('/', 1)[1]
        try:
            link = stat.S_ISLNK(os.lstat(cand).st_mode)
        except OSError:
            link = False
        real = prefix[here] = os.path.realpath(cand) if link else cand
    return real


def _probe(op, arg, prefix=None):
    """The answer of the disk to one lookup of the judgment, as a comparable value. A lookup that fails is the answer None: it is remembered too, since a file that appears later is a change."""
    try:
        if op == 'stat':
            return _stat_answer(os.stat(arg))
        if op == 'lstat':
            return _stat_answer(os.lstat(arg))
        if op == 'list':
            out = []
            with os.scandir(arg) as it:
                for e in it:
                    try:
                        kind = 'd' if e.is_dir() else 'f' if e.is_file() else 'o'
                    except OSError:
                        kind = 'o'
                    out.append((e.name, kind))
            return tuple(sorted(out))
        if op == 'real':
            try:
                return _real_via(arg, {} if prefix is None else prefix)
            except ValueError:                                  # a name the disk cannot hold (a NUL byte): it is where it says it is
                return os.path.normpath(arg)
        if op == 'link':
            return os.readlink(arg)
        if op == 'sha':
            st = os.stat(arg)
            if not stat.S_ISREG(st.st_mode) or st.st_size > FILE_MAX:
                return (_stat_answer(st), None)
            try:
                with open_safe(arg, binary=True, strict=False) as f:          # a digest tells nothing of the bytes: a folder that starts with a dot is no reason to refuse (a file of the credentials is refused still)
                    return (_stat_answer(st), hashlib.sha1(f.read(FILE_MAX + 1)).hexdigest())
            except (OSError, Denied):
                return (_stat_answer(st), None)
        if op == 'line':                                      # the first line of a small pointer file (`.git`, `commondir`)
            if not stat.S_ISREG(os.stat(arg).st_mode):
                return None
            with open(arg, 'rb') as f:
                data = f.read(4096)
            lines = data.decode('utf-8', 'replace').splitlines()
            return lines[0].strip() if lines else ''
    except (OSError, ValueError):
        return None
    raise ValueError(op)


class Catalog:
    """Everything the judgment reads of the disk, and what it read. The only door: a stat, an lstat, a listing, a real path, a link, the sha1 of a file, the first line of a pointer file. One
    generation (`begin`) is one judgment: a lookup is made once however often it is asked, and `reads` is the list of the lookups with their answers (J19). A judgment that was made on
    `reads` is good while `changed(reads)` says no: every lookup asked again gives the same answer (a stat is (kind, inode, size, mtime_ns), a listing the sorted names and kinds, a sha the sha and the stat of the file).
    The debate folders are looked at through it too (`unit_at`)."""

    def __init__(self, text_of=None):
        self._text_of = text_of            # (the judgment reads no text; kept for the callers that name it)
        self._memo = {}                    # (lookup, argument) -> its answer, for this generation (in the order asked)
        self._units = {}                   # path -> the Unit or None, for this generation
        self._prefix = {}                  # the real paths of the folders the real paths of this generation run through
        self.gen = 0

    def begin(self):
        self.gen += 1
        self._memo = {}
        self._units = {}
        self._prefix = {}

    def _ask(self, op, arg):
        key = (op, arg)
        try:
            return self._memo[key]
        except KeyError:
            ans = self._memo[key] = _probe(op, arg, self._prefix)
            return ans

    def reads(self):
        """The lookups of this generation with their answers."""
        return tuple(self._memo.items())

    @staticmethod
    def changed(reads):
        """Whether the disk answers any of `reads` differently now. The real paths share what they find of the folders above them (each is looked at once however many paths run through it), and the
        sha of a file is not made again while its stat is the same (kind, inode, size, time): what is asked of the bytes is whether they were replaced, and a replaced file has another time or size."""
        prefix, links = {}, {}                           # (the lstat of a path that is no link is its stat: one look serves both)
        for (op, arg), ans in reads:
            if op == 'sha':
                try:
                    now = _stat_answer(os.stat(arg))
                except OSError:
                    now = None
                if (ans is None) != (now is None) or (ans is not None and ans[0] != now):
                    return True
                continue
            if op == 'stat' and arg in links and (links[arg] is None or links[arg][0] != 'l'):
                now = links[arg]
            else:
                now = _probe(op, arg, prefix)
            if now != ans:
                return True
            if op == 'lstat':
                links[arg] = now
        return False

    def stat(self, path):
        """(kind, inode, size, mtime_ns) of what a path leads to ('f' a regular file, 'd' a folder, 'o' other), or None."""
        return self._ask('stat', path)

    def lstat(self, path):
        return self._ask('lstat', path)

    def listdir(self, path):
        """The sorted (name, kind) pairs of a folder (kind 'd' a folder, 'f' a regular file, 'o' other; links are followed), or None."""
        return self._ask('list', path)

    def realpath(self, path):
        return self._ask('real', path)

    def real_folder_of(self, path):
        """The path with the real path of its folder (asked once for all the files in it), the name of the file as it is: for the files whose own links do not matter."""
        folder, name = os.path.split(os.path.normpath(path))
        return os.path.join(self.realpath(folder), name)

    def real_file(self, path):
        """The real path of a file the judgment is told of: the real path of its folder (asked once for all the files in it) and its own name, unless the file is itself a link (one `lstat` tells). Many files, few lookups."""
        path = os.path.normpath(path)
        folder, name = os.path.split(path)
        real = os.path.join(self.realpath(folder), name)
        st = self.lstat(real)
        return self.realpath(real) if st is not None and st[0] == 'l' else real

    def readlink(self, path):
        return self._ask('link', path)

    def sha1(self, path):
        """The sha1 of a regular file the document view may open (not over FILE_MAX), or None."""
        got = self._ask('sha', path)
        return got[1] if got else None

    def first_line(self, path):
        return self._ask('line', path)

    def is_file(self, path):
        """Whether the path is a regular file."""
        st = self.stat(path)
        return st is not None and st[0] == 'f'

    def is_dir(self, path):
        st = self.stat(path)
        return st is not None and st[0] == 'd'

    def unit_at(self, path):
        """The Unit that is exactly this folder (round folders or qualified file rounds), or None."""
        path = os.path.normpath(path)
        if path in self._units:
            return self._units[path]
        unit = None
        if not too_broad(path):
            names = self.listdir(path)
            if names is not None:
                rounds = {}
                for name, kind in names:
                    n = round_of(name)
                    if n is not None and kind == 'd':
                        rounds.setdefault(n, []).append(name)
                if rounds:
                    unit = Unit(id=path, path=path, rounds=rounds)
                    guide = next((g for g in GUIDE_NAMES if (g, 'f') in names), None)
                    if guide:
                        unit.brief = os.path.join(path, guide)
                    real = self.realpath(path)
                    if real != path:
                        unit.aliases.append(real)
        self._units[path] = unit
        return unit

    def md_stems(self, folder):
        """The names (without .md) of the markdown files directly in a folder."""
        return [n[:-3] for n, kind in (self.listdir(folder) or ()) if kind == 'f' and n.endswith('.md')]

    def add_file_rounds(self, writes, sure):
        """O23: two session authors must make different seats, with two seats in one round. Existing topics and bundles take precedence."""
        by_folder = collections.defaultdict(list)
        for w in writes:
            if w.agent != 'orch' and w.kind in AUTHORING and w.evidence in ('tool', 'shell') and file_round_of(os.path.basename(w.path)) and sure(w):
                by_folder[os.path.dirname(w.path)].append(w)
        scratch = {os.path.normpath(d) for d in SCRATCH_DIRS} if by_folder else set()
        scratch.update(self.realpath(d) for d in tuple(scratch))
        for folder in sorted(by_folder, key=lambda f: (-f.count(os.sep), f)):
            if folder in scratch or _is_common(folder, self) or self.unit_at(folder) is not None or self.children(folder) or round_of(os.path.basename(folder)) is not None:
                continue
            ws = by_folder[folder]
            seats = collections.defaultdict(set)
            for w in ws:
                rnd, seat = file_round_of(os.path.basename(w.path))
                seats[rnd].add(seat)
            if len({w.agent for w in ws}) < 2 or not any(len(ss) >= 2 for ss in seats.values()):
                continue
            names = self.listdir(folder)
            if names is None:
                continue
            files = sorted({n for n, k in names if k == 'f' and file_round_of(n)} | {os.path.basename(w.path) for w in ws})
            rounds = {}
            for n in files:
                rounds.setdefault(file_round_of(n)[0], []).append(n)
            guide = next((g for g in GUIDE_NAMES if (g, 'f') in names), None)
            real = self.realpath(folder)
            self._units[folder] = Unit(id=folder, path=folder, file_rounds=rounds,
                                       brief=os.path.join(folder, guide) if guide else None, aliases=[real] if real != folder else [])

    def children(self, root, skip=('final',)):
        """The sub-folders of a grouping folder that are debate folders (the topics of a shared guide)."""
        out = []
        for name, kind in self.listdir(root) or ():
            if kind != 'd' or name in skip:
                continue
            u = self.unit_at(os.path.join(root, name))
            if u:
                out.append(u)
        return out


# folders a walk never enters: what a repository keeps its tools and builds in
WALK_SKIP = frozenset(('node_modules', '__pycache__', 'venv', 'site-packages', 'dist', 'build', 'target', 'vendor'))


def repo_top(cwd, levels=12, cat=None):
    """The nearest folder at or above `cwd` that holds a `.git` (a folder, or a file in a worktree): the top of a repository. None when there is none, or
    when it would be a folder that is too broad. `cat` asks the disk through a Catalog (so that the judgment remembers it)."""
    d = os.path.normpath(cwd) if cwd else ''
    for _ in range(levels):
        if not d or not os.path.isabs(d) or too_broad(d):
            return None
        if (cat.lstat(os.path.join(d, '.git')) is not None) if cat is not None else os.path.exists(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def holds_repositories(folder, at_least=2):
    """Whether a folder is a place that keeps repositories (~/Dev) rather than a project: at least `at_least` of its sub-folders hold a `.git`."""
    n = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and os.path.exists(os.path.join(e.path, '.git')):
                    n += 1
                    if n >= at_least:
                        return True
    except OSError:
        pass
    return False


def walk_repo(top, catalog, max_dirs=3000, max_depth=6, deadline=None):
    """The debate folders below a repository top, by a bounded breadth-first walk. Meant for the background stage of the board, not for a state build.
    Never walks a folder that is too broad (HOME or what holds it, /tmp ...), a folder that keeps repositories (~/Dev), dot folders, links, or the usual dependency
    folders. Returns (units, capped): `capped` is True when the budget ran out before the walk was done (`listing_capped`)."""
    top = os.path.normpath(top)
    if too_broad(top) or not os.path.isdir(top) or (not os.path.exists(os.path.join(top, '.git')) and holds_repositories(top)):
        return [], False
    found, queue, seen = [], collections.deque([(top, 0)]), 0
    while queue:
        d, depth = queue.popleft()
        seen += 1
        if seen > max_dirs or (deadline is not None and time.monotonic() > deadline):
            return found, True
        u = catalog.unit_at(d)
        if u:
            found.append(u)
        if depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                subs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for name in subs:
            if name.startswith('.') or name in WALK_SKIP or (u and round_of(name) is not None):
                continue
            queue.append((os.path.join(d, name), depth + 1))
    return found, False


def listing_hint(path, is_dir=False):
    """The folder an orchestrator's own successful write says to look at, or None. `path` is absolute. A guide written (`<D>/brief.md`, `README.md`, `index.md`), a report under a
    round folder (`<D>/<r|round N>/<x>.md`) or a round folder made (`<D>/<r|round N>`, `is_dir`) is for D. Nothing else is: not a plan, a note or a closing document, not an
    arbitrary folder, and no folder above D is looked at. The name settles nothing: `written_debate` asks the disk."""
    if not path or not os.path.isabs(path):
        return None
    path = os.path.normpath(path)
    name = os.path.basename(path)
    if is_dir:
        return os.path.dirname(path) if round_of(name) is not None else None
    if name in GUIDE_NAMES:
        return os.path.dirname(path)
    rep = report_of_path(path)
    return rep[0] if rep else None


_OWN_TOP = []


def own_top():
    """The top of the repository this program runs from (a checkout), or None (an installed copy has none). Tests of the program keep their fixtures there."""
    if not _OWN_TOP:
        _OWN_TOP.append(repo_top(os.path.dirname(os.path.abspath(__file__))))
    return _OWN_TOP[0]


def _is_common(folder, cat=None):
    """Whether a guide in this folder is a document everybody of a repository reads: the folder is the top of a repository (it holds `.git`), or the `docs` folder directly in it."""
    has = (lambda p: cat.lstat(p) is not None) if cat is not None else os.path.exists
    return too_broad(folder) or has(os.path.join(folder, '.git')) or \
        (os.path.basename(folder) in COMMON_FOLDERS and has(os.path.join(os.path.dirname(folder), '.git')))


def written_debate(u, cat=None):
    """Whether the folder `u` (a Unit) that the orchestrator wrote into may be listed as a debate: it has a guide and exact round folders on disk, and it is not one of the places a guide is a
    document of its own: a folder that is too broad, what the tools keep (`STATE_DIRS`), this program's own repository, and, for a README.md or an index.md, the top of a repository or its `docs`."""
    d = u.path
    if too_broad(d) or d.startswith(STATE_DIRS):
        return False
    own = own_top()
    if own and (d == own or d.startswith(own + os.sep)):
        return False
    guide = os.path.basename(u.brief) if u.brief else None
    if not (u.rounds and guide):
        return False
    return guide == 'brief.md' or not _is_common(d, cat)


# ---------------------------------------------------------------------------------------------------------------------
# what the judgment is given (no sentence in it: J20)
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class AgentFacts:
    """What the judgment needs to know of one agent. The collector (debates.facts_of) fills it from the agent; the judgment reads nothing else. No field holds a sentence of any record
    (an instruction, a message, a report): the strings are an id, the provider, the origin, two folders and the status."""
    id: str
    provider: str = 'claude'
    origin: str = 'subagent'                       # subagent | cli | exec
    cwd: str = ''                                  # the agent's own working folder ('' when unknown)
    launcher_cwd: str = ''                         # the working folder of the session that started it ('' when unknown)
    start: float = 0.0                             # when it was started (the call that spawned it, else its first record)
    first: float = 0.0                             # the time of its own first record
    last: float = 0.0                              # its last record
    status: Optional[str] = None                   # the state the page gives it (facts.STATUSES); one nobody gave counts as `unknown` (L4)
    launch: Optional[LaunchKey] = None
    tag: Optional[Tag] = None
    writes: Tuple[WriteEvent, ...] = ()
    reads: Tuple[ReadEvent, ...] = ()
    planned: Tuple[Planned, ...] = ()
    windows: Tuple[CmdWindow, ...] = ()
    windows_dropped: Optional[Tuple[float, float]] = None
    writes_dropped: Optional[Tuple[float, float]] = None       # the shell writes of paths that are not markdown that were let go for the budget (J14 only)
    lost: Optional[Tuple[float, float]] = None     # the part of its record that was not read (J3 history_lost, J15). The end may be infinite
    run: Optional[int] = None                      # the number of its last run
    run_start: float = 0.0                         # when that run started
    sent_to: Tuple[str, ...] = ()                  # the ids of the agents of the same session it sent a message to that went through


@dataclass
class SessionFacts:
    agents: List[AgentFacts]
    orch_writes: Tuple[WriteEvent, ...] = ()
    orch_windows: Tuple[CmdWindow, ...] = ()
    orch_windows_dropped: Optional[Tuple[float, float]] = None
    orch_lost: Optional[Tuple[float, float]] = None
    hints: Dict[str, Hint] = field(default_factory=dict)       # folder -> where and by which call the orchestrator wrote there
    walked: Tuple[str, ...] = ()                                # the debate folders a walk of the repositories found
    walk_capped: bool = False
    hints_dropped: bool = False
    launcher_cwd: str = ''


# ---------------------------------------------------------------------------------------------------------------------
# what the judgment gives
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Cell:
    unit: str
    round: Optional[int]                 # None for the cell of a room
    rdir: str                            # the round folder as it is on disk ('-' for a room)
    stem: str
    path: str
    owner: Optional[str]                 # J3·J4
    agent: Optional[str]                 # the owner, else the agent J7 picked from those that were asked to save the file; None where the cell is held or contested
    editors: List[str]                   # J5 (agent ids and 'orch')
    evidence: Optional[str]              # the evidence of the owner's first write: 'tool' | 'shell' | 'planned'; 'planned' or 'tag' for a cell nobody has written
    hint: Optional[dict]                 # {'kind': 'window', 'agent': id} (J8) or None
    previous: bool                       # the file is there and is from before: nobody owns it, or the cell agent has not saved it in this run yet (J6)
    state: str                           # facts.CELLS


@dataclass
class Placement:
    agent: str
    unit: str                            # the root of the bundle
    topic: Optional[str]                 # the topic when there is exactly one besides the root, else None
    why: str                             # tag | launch_call | launch_peer | guide_read: the strongest of whys
    whys: List[str]
    sure: bool                           # only a room tag makes it sure


@dataclass
class Room:
    folder: str
    kind: str                            # cells | members
    sure: bool                           # a room tag says it (True) or the launch does (False)
    why: str                             # tag | launch
    members: List[str] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)       # agent id -> the absolute path of its file in the room
    guide: Optional[str] = None


@dataclass
class Final:
    unit: str
    scope: str                           # topic | room | bundle
    confirmed: bool
    path: Optional[str]
    by: Optional[str]
    why: List[str]                       # facts.FINAL_WHY, in that order
    candidates: List[str]
    last_report: Optional[float]         # the time of the last write attempt or change of a cell file (La)


@dataclass
class Judgement:
    cells: Dict[Tuple[str, str, str], Cell] = field(default_factory=dict)          # (unit or room folder, round folder or '-', name) -> Cell
    placed: Dict[str, Placement] = field(default_factory=dict)                      # agent id -> where it is thought to work
    units: Dict[str, Unit] = field(default_factory=dict)
    roots: Dict[str, List[str]] = field(default_factory=dict)                       # root -> the topics (unit folders) of it
    listed: Dict[str, str] = field(default_factory=dict)                            # root -> cell | tag | room | walk | hint
    hinted: Set[str] = field(default_factory=set)                                   # the roots that are listed only because the orchestrator wrote there
    rooms: Dict[str, Room] = field(default_factory=dict)
    finals: Dict[str, Final] = field(default_factory=dict)                          # a topic, a room, a root that is none
    closable: Dict[str, bool] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)                                  # the roots in the order of the list
    current: Optional[str] = None
    agent_units: Dict[str, Set[str]] = field(default_factory=dict)                  # the units where an agent owns a cell
    worked: Dict[str, Set[str]] = field(default_factory=dict)                       # the units an agent is tied to for sure (owner, editor, asked to save, room tag) and where it is placed
    assignments: List[Assignment] = field(default_factory=list)
    diag: List[dict] = field(default_factory=list)
    folded: Dict[str, List[str]] = field(default_factory=dict)                      # a folder -> the copies of it that are not shown
    unit_ts: Dict[str, float] = field(default_factory=dict)                         # a unit or room -> the latest start of an agent tied to it for sure and the time of a hint (the last activity)
    bound: Dict[str, Set[str]] = field(default_factory=dict)                        # a unit or room -> the agents tied to it for sure


def _diag(out, code, agent=None, unit=None, detail=None):
    row = {'code': code, 'agent': agent, 'unit': unit, 'detail': detail}
    if row not in out:
        out.append(row)


def cell_state(status, file_ok, working_here, has_agent):
    """The state of a report cell. `working_here`: the agent holds this round now. A working agent has a draft (the file exists) or is writing, and so does one
    whose process cannot be seen (`unknown`: nothing says it ended); an agent whose run was cut off (`interrupted`) has a draft, or is paused with no file;
    for an agent that is over for any other reason the file is the answer: done if it is there, missing if it is not; a seat without an agent waits."""
    if working_here and status in OPEN:
        return 'draft' if file_ok else 'writing'
    if working_here and status == 'interrupted':
        return 'draft' if file_ok else 'paused'
    if file_ok:
        return 'done'
    return 'missing' if has_agent else 'waiting'


def _over_before(g, status, f):
    """Whether agent g was already over when agent f started (so f takes the seat over rather than competing for it): its last record is not later than f's start and
    it is not working. A run that was cut off (`interrupted`: a usage limit, an error, an early exit) is over for this, however it may be resumed: the same script run again
    after its last record takes the seat. If the cut-off run is resumed later and works after f has started, g's last record is later than f's start and the two
    overlap. One whose process cannot be seen (`unknown`) may still be working: it is not over."""
    return status is not None and status not in OPEN and (g.last or g.start or 0) <= (f.start or 0)


def _inside(path, folder):
    """Whether a path is the room's folder, directly in it, or one folder below it: where the files of a room are."""
    return path == folder or _in_reach(path, folder)


def _in_reach(path, folder):
    return os.path.dirname(path) == folder or os.path.dirname(os.path.dirname(path)) == folder


def _work_file(path, roots):
    """Whether a file is one of the work that an agent may change: inside a repository it works in or the guide is in (`roots`), not in what the agent keeps of itself, and,
    where no repository is known, not in a folder for scratch work."""
    if path.startswith(STATE_DIRS):
        return False
    if roots:
        return any(path == r or path.startswith(r + os.sep) for r in roots)
    return not path.startswith(SCRATCH_DIRS)

# ---------------------------------------------------------------------------------------------------------------------
# the judgment
# ---------------------------------------------------------------------------------------------------------------------
_REASON_RANK = ('cell', 'tag', 'room', 'walk', 'hint')          # the strongest reason a root is listed for, first
_WHY_RANK = ('tag', 'launch_call', 'launch_peer', 'guide_read')


class _Judge:
    """One judgment: the facts and the Catalog in, a Judgement out. The methods follow the lines of the contract (J1 ... J19); every read of the disk goes through the Catalog."""

    def __init__(self, sf, cat, tops_of=None):
        self.sf, self.cat = sf, cat
        self.tops_of = tops_of or (lambda cwd: ())
        self.agents = [a if a.status is not None else dataclasses.replace(a, status='unknown') for a in sf.agents]          # a state nobody gave is no state that says it ended (L4)
        self.by_id = {a.id: a for a in self.agents}
        self._real, self._sure, self._seat_memo, self._own, self._ucp, self._live = {}, {}, {}, {}, {}, {}
        self._wins = None
        self.events = self._events()
        self.by_path, self.by_agent, self.by_dir = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
        for w in self.events:
            self.by_path[w.path].append(w)
            self.by_agent[w.agent].append(w)
            self.by_dir[os.path.dirname(w.path)].append(w)
        self.cat.add_file_rounds(self.events, self.sure)
        self.demand = {a.id: self._demands(a) for a in self.agents}
        self.askers = collections.defaultdict(list)                      # path -> the agents that were asked to save it
        for a in self.agents:
            for q in self.demand[a.id]:
                self.askers[q].append(a)
        self._rounds = {}
        self.jd = Judgement()

    # ----- paths and events -----
    def real(self, path):
        r = self._real.get(path)
        if r is None:
            r = self._real[path] = self.cat.real_file(path)
        return r

    def _events(self):
        out = []
        for owner, ws in [(a.id, a.writes) for a in self.agents] + [('orch', self.sf.orch_writes)]:
            for w in ws:
                path = self.real(w.path) if w.path.lower().endswith('.md') else self.cat.real_folder_of(w.path)
                if path != w.path or w.agent != owner:
                    w = dataclasses.replace(w, path=path, agent=owner)
                out.append(w)
        out.sort(key=lambda w: (w.ts, w.agent))
        return out

    def mtime(self, path):
        st = self.cat.stat(path)
        return st[3] / 1e9 if st is not None and st[0] == 'f' else None

    def sure(self, w):
        """J1: a write is sure when its result says it worked and the tool or the exit status of the whole command decides it, or the file named was saved in the time the command ran (F)."""
        k = id(w)
        got = self._sure.get(k)
        if got is None:
            got = self._sure[k] = w.ok is True and (w.proof in ('tool', 'exit') or (w.proof in CHECKED and self._saved(w)))
        return got

    def _saved(self, w):
        """F: on the disk as it is now, the named file is a regular file whose time is strictly after the start of the window of the command (a save at the start cannot be told from one
        before it) and not later than its end and a grace; when bytes are expected (a sha, the text of a `claude -p` run) it is those. The size is not looked at: an empty save is a save."""
        st = self.cat.stat(w.path)
        if st is None or st[0] != 'f' or not w.span:
            return False
        m = st[3] / 1e9
        if not (w.span[0] < m <= w.span[1] + (W4_GRACE if w.evidence == 'planned' else WIN_EPS)):
            return False
        return not w.shas or self.cat.sha1(w.path) in w.shas

    def _demands(self, a):
        """{path: [(time, kind, run)]} the files a run of the agent was asked to save (J7): the outputs a launch command names (`Planned`), and the seat of its room tag (J13): `rN/name` in a
        folder with rounds, `name` in a folder without. Sorted by time."""
        d = collections.defaultdict(list)
        for q in a.planned:
            d[self.real(q.path)].append((q.ts, 'planned', q.run))
        t = a.tag
        if t and t.seat:
            seat = t.seat[:-3] if t.seat.lower().endswith('.md') else t.seat
            unit = self.cat.unit_at(t.room)
            if '/' in seat or (unit and unit.file_rounds and file_round_of(seat + '.md')) or unit is None:
                d[self.real(os.path.join(t.room, seat + '.md'))].append((a.run_start, 'tag', t.run))
        return {p: sorted(v, key=lambda x: (x[0], x[1])) for p, v in d.items()}

    def asked(self, a, p):
        return self.demand[a.id].get(p, [])

    # ----- J3 J4 J5 J7: who owns a cell -----
    def seat(self, p):
        """(owner, agent, editors, evidence, diag, held, taken) of the cell file p. `diag` is [(code, agent)]; `taken` is [(agent that was taken over from, the time, the evidence of its first write)]."""
        if p in self._seat_memo:
            return self._seat_memo[p]
        ws = self.by_path.get(p, [])
        conf = [w for w in ws if self.sure(w)]
        first = {}
        for w in conf:
            if w.agent != 'orch' and w.kind in AUTHORING:
                first.setdefault(w.agent, w)
        authors = sorted(first.values(), key=lambda w: (w.ts, w.agent))
        diag, owner, held, taken = [], None, False, []
        if authors:
            y = authors[0]
            tied = [w.agent for w in authors[1:] if w.ts - y.ts <= TIE_SEC]
            # a write by another agent that is not sure (a result that is not known, a save that cannot be checked, a failure too: a shell that failed may have written) may have made the file first
            unsure = sorted({w.agent for w in ws if w.agent not in ('orch', y.agent) and w.kind in AUTHORING and not self.sure(w) and w.ts <= y.ts + TIE_SEC})
            lost = sorted(a.id for a in self.agents if a.id != y.agent and a.lost and a.lost[0] <= y.ts + TIE_SEC)
            if tied or unsure:
                held = True
                diag += [('seat_tie_held', x) for x in sorted({y.agent, *tied, *unsure})]
            if lost:
                held = True
                diag += [('history_lost', x) for x in lost]
            if not held:
                owner = y.agent
        prev = None
        if owner:
            for w in authors[1:]:
                b = self.by_id[w.agent]
                g = self.by_id[owner]
                if _over_before(g, g.status, b) and self.asked(b, p) and w.ts >= (b.start or 0):
                    prev, owner = owner, b.id
                    taken.append((prev, b.start, first[prev].evidence))
        editors = list(dict.fromkeys(w.agent for w in conf if w.agent != owner))
        if prev:
            editors.remove(prev)
            editors.insert(0, prev)
        agent, evidence = owner, (first[owner].evidence if owner else None)
        if not owner and not held:
            cand = self.askers.get(p, [])
            left = [g for g in cand if not any(h is not g and _over_before(g, g.status, h) for h in cand)]
            if len(left) == 1:
                agent, evidence = left[0].id, ('planned' if any(k == 'planned' for _t, k, _r in self.asked(left[0], p)) else 'tag')
            elif len(left) > 1:
                evidence = 'planned' if any(k == 'planned' for g in left for _t, k, _r in self.asked(g, p)) else 'tag'
                diag += [('seat_tie_held', g.id) for g in sorted(left, key=lambda g: g.id)]
        out = self._seat_memo[p] = (owner, agent, editors, evidence, diag, held, taken)
        return out

    # ----- J6 J8: what a cell shows -----
    def _met(self, a, p):
        """(not saved, the state of the run that asked) of the latest request of agent a for p, or None when it has none (J6). The latest request is the one of the greatest run, then the latest time;
        it is met by a sure write of the agent on p in that run or a later one, not before the request. A run that is not known meets nothing."""
        dem = self.asked(a, p)
        if not dem:
            return None
        if any(r is None for _t, _k, r in dem):
            return True, a.status
        d_ts, _kind, d_run = max(dem, key=lambda d: (d[2], d[0]))
        unmet = not any(w.agent == a.id and w.run is not None and w.run >= d_run and w.ts >= d_ts and self.sure(w) for w in self.by_path.get(p, ()))
        return unmet, (a.status if a.run is None or d_run == a.run else 'done')

    def _resumed_idle(self, a, p):
        """Whether agent a was resumed (the run it is in now is later than every run that asked it for p or wrote p, whatever came of the write) and has done nothing about p in it: what it saved in an
        earlier run stays a submission however the resumed run is going (D19). A run that is not known holds the cell: it may be this one."""
        if a.run is None:
            return False
        runs = [r for _t, _k, r in self.asked(a, p)] + [w.run for w in self.by_path.get(p, ()) if w.agent == a.id]
        return bool(runs) and all(r is not None and r < a.run for r in runs)

    def cell(self, unit, rdir, stem, path, room=False):
        u = self.cat.unit_at(unit)
        n = round_of(rdir.lower()) if u and u.file_rounds else round_of(rdir)
        owner, agent, editors, evidence, _diag_, _held, _taken = self.seat(path)
        st = self.cat.stat(path)
        exists = st is not None and st[0] == 'f'
        file_ok = exists and st[2] > 0
        previous = exists and owner is None
        if owner:
            o = self.by_id[owner]
            met = self._met(o, path)
            if met and met[0]:
                state, previous = cell_state(met[1], False, True, True), exists
            else:
                if room:
                    here = True
                else:                                              # the round it works in now: the latest where it is the agent of a cell of this unit
                    here = n == self._current_round(unit).get(o.id)
                state = cell_state('done' if self._resumed_idle(o, path) else o.status, file_ok, here, True)
        elif agent:
            state = cell_state(self._met(self.by_id[agent], path)[1], False, True, True)
        else:
            state = 'previous' if exists else 'waiting'
        hint = self._hint(path) if owner is None and exists else None
        return Cell(unit, None if room else n, rdir, stem, path, owner, agent, editors, evidence, hint, previous, state)

    def _current_round(self, unit):
        """{agent id: the round it works in now}: the latest round of this unit in which it is the agent of a cell."""
        got = self._rounds.get(unit)
        if got is None:
            got = {}
            for q in self.unit_cell_paths(unit):
                aid = self.seat(q)[1]
                if aid is not None:
                    ck = self.cell_key(q)
                    n = round_of(ck[1].lower())
                    got[aid] = max(got.get(aid, n), n)
            self._rounds[unit] = got
        return got

    def _hint(self, p):
        """J8: the one command that ran when the file was saved, when there is exactly one and it worked (a hint, nothing more). Every command window counts, whatever its result; an open one
        runs to the end of time; one that read the file is not the one that wrote it; a window let go for the budget that may have held the time makes it unknown."""
        m = self.mtime(p)
        if m is None:
            return None
        st = self.cat.stat(p)
        hi = m + 1 if st[3] % 1000000000 == 0 else m                   # a file system that keeps whole seconds
        if self._wins is None:                                           # every window of everybody by its start, and the latest end up to each: a file that none reaches is found without a look at them all
            allw = sorted(((w.t0, aid, w) for aid, ws in [(a.id, a.windows) for a in self.agents] + [('orch', self.sf.orch_windows)] for w in ws), key=lambda x: (x[0], x[1]))
            top, pmax = -INF, []
            for _t0, _aid, w in allw:
                top = max(top, INF if w.t1 is None else w.t1)
                pmax.append(top)
            self._wins = (allw, [x[0] for x in allw], pmax)
        allw, t0s, pmax = self._wins
        lost = any(cap and cap[0] <= hi + WIN_EPS and cap[1] >= m - WIN_EPS for cap in [a.windows_dropped for a in self.agents] + [self.sf.orch_windows_dropped])
        found = []
        for _t0, aid, w in allw[bisect.bisect_left(pmax, m - WIN_EPS):bisect.bisect_right(t0s, hi + WIN_EPS)]:
            if (INF if w.t1 is None else w.t1) >= m - WIN_EPS and p not in [self.real(r) for r in w.reads]:
                found.append((aid, w))
        if len(found) > 1:                                           # the call that launched a candidate runs through the whole of its run and is no rival of it (O2); every other window is
            for aid in sorted({x for x, _w in found if x != 'orch'}):
                launched = self.by_id[aid].launch
                mine = [(x, w) for x, w in found if x == aid]
                if len(mine) == 1 and all(x == aid or (launched is not None and w.call == launched.call) for x, w in found):
                    found = mine
                    break
        if len(found) == 1 and not lost and found[0][0] != 'orch' and found[0][1].ok is True:
            return {'kind': 'window', 'agent': found[0][0]}
        return None

    # ----- the cells of a unit -----
    def cell_key(self, path):
        """(unit folder, round name, seat) of a round-folder cell or a qualified file-round cell, else None."""
        d = os.path.dirname(path)
        fr = file_round_of(os.path.basename(path))
        u = self.cat.unit_at(d) if fr else None
        if u is not None and u.file_rounds and fr:
            return d, os.path.basename(path).split('_', 1)[0], fr[1]
        if not path.endswith('.md'):
            return None
        rdir = os.path.basename(d)
        if round_of(rdir) is None:
            return None
        unit = os.path.dirname(d)
        if self.cat.unit_at(unit) is None:
            return None
        return unit, rdir, os.path.basename(path)[:-3]

    def _index(self):
        """The cell files that something names, by unit: a sure write of anyone, a request of an agent (J7). {unit: set of paths}, and the units an agent is tied to by a write or a request."""
        if hasattr(self, '_named'):
            return
        named = collections.defaultdict(set)
        writers, askers = collections.defaultdict(set), collections.defaultdict(set)
        for w in self.events:
            if w.path.lower().endswith('.md'):
                ck = self.cell_key(w.path)
                if ck and self.sure(w):
                    named[ck[0]].add(w.path)
                    if w.agent != 'orch':
                        writers[ck[0]].add(w.agent)
        for a in self.agents:
            for p in self.demand[a.id]:
                ck = self.cell_key(p)
                if ck:                                           # a request names a cell, the round folder there or not yet (an unmet request holds a final back)
                    named[ck[0]].add(p)
                    askers[ck[0]].add(a.id)
        self._named, self._unit_writers, self._unit_askers = named, writers, askers

    def unit_cell_paths(self, unit):
        """Every cell file of a unit: the markdown files in its round folders, and what a sure write or a request names there."""
        got = self._ucp.get(unit)
        if got is not None:
            return got
        self._index()
        u = self.cat.unit_at(unit)
        paths = set(self._named.get(unit, ()))
        for files in (u.file_rounds.values() if u else ()):
            paths.update(os.path.join(unit, n) for n in files)
        for dirs in (u.rounds.values() if u else ()):
            for d in dirs:
                folder = os.path.join(unit, d)
                paths.update(os.path.join(folder, n) for n, kind in (self.cat.listdir(folder) or ()) if kind == 'f' and n.endswith('.md'))
        got = self._ucp[unit] = sorted(paths)
        return got

    # ----- J14 J13: rooms -----
    def _tag_room(self, a):
        """The folder of the room tag of an agent when it is a real folder that may be one (J13), else None."""
        t = a.tag
        if not t:
            return None
        room = self.real(t.room)
        if not os.path.isabs(room) or too_broad(room) or (room + os.sep).startswith(STATE_DIRS) or not self.cat.is_dir(room):
            return None
        return room

    def _own_files(self, a):
        """The `.md` files an agent made whole with a sure write by a tool or a shell command, first first, that are fit to be the file of its own in a room: no guide, not in a round folder."""
        got = self._own.get(a.id)
        if got is None:
            got = []
            for w in self.by_agent.get(a.id, ()):
                if w.kind in AUTHORING and w.evidence in ('tool', 'shell') and w.path.endswith('.md') and w.path not in got and self.sure(w):
                    d = os.path.dirname(w.path)
                    if os.path.basename(w.path) not in GUIDE_NAMES and round_of(os.path.basename(d)) is None:
                        got.append(w.path)
            self._own[a.id] = got
        return got

    def _writers(self, p):
        return {w.agent for w in self.by_path.get(p, ()) if w.agent != 'orch' and w.evidence in ('tool', 'shell') and self.sure(w)}

    def _changes_elsewhere(self, a, folder, roots):
        """J14: whether an agent changed a file of the work outside the room's folder with a sure write (a tool or a shell command; what a launch command saves is no change of the work). An agent whose
        shell writes were let go for the budget counts as one that did."""
        if a.writes_dropped:
            return True
        return any(w.evidence in ('tool', 'shell') and not _inside(w.path, folder) and _work_file(w.path, roots) and self.sure(w) for w in self.by_agent.get(a.id, ()))

    def _room_folder_ok(self, folder):
        """The folders that are no room (J14): a place everybody reads (the top of a repository, its `docs`), a debate folder, the folder of a bundle of debates, a round folder."""
        return not (_is_common(folder, self.cat) or self.cat.unit_at(folder) is not None or self.cat.children(folder) or round_of(os.path.basename(folder)) is not None)

    def _works_elsewhere(self, folder, members):
        """Whether most of the members change a file of the work outside the folder (a plan done by changing the work, not a meeting)."""
        guide_top = repo_top(folder, cat=self.cat)
        outside = sum(1 for a in members if self._changes_elsewhere(a, folder, {t for t in (guide_top, *self.tops_of(a.cwd)) if t} if guide_top else ()))
        return 2 * outside > len(members)

    def _room_ok(self, folder, members):
        return self._room_folder_ok(folder) and not self._works_elsewhere(folder, members)

    def _file_in(self, a, folder, counts):
        """The first file of its own that an agent has in a room: in the folder or one below it, and written by it alone."""
        for p in self._own_files(a):
            d = os.path.dirname(p)
            if _in_reach(p, folder) and counts(p) == 1 and (d == folder or self.cat.unit_at(d) is None):
                return p
        return None

    def _make_room(self, folder, members, why, sure, guide=None):
        counts = lambda p: len(self._writers(p))                    # noqa: E731
        files = {}
        for a in members:
            p = self._file_in(a, folder, counts)
            seat = a.tag.seat if why == 'tag' and a.tag and a.tag.seat and '/' not in a.tag.seat else None
            if p is None and seat:
                p = self.real(os.path.join(folder, (seat[:-3] if seat.endswith('.md') else seat) + '.md'))
            if p:
                files[a.id] = p
        kind = 'cells' if len(files) >= (1 if sure else 2) else 'members'          # a room of a tag has the file of any member (a seat names it too); one that only the launch makes has the files of two at least
        if kind == 'members':
            files = {}
        if guide is None:
            guide = next((os.path.join(folder, g) for g in GUIDE_NAMES if self.cat.is_file(os.path.join(folder, g))), None)
        return Room(folder, kind, sure, why, [a.id for a in members], files, guide)

    def rooms(self):
        """J14. A sure room: two or more agents with the same room tag on a folder that has no round folder. An unsure one: agents launched together that each made a file of their own in a folder or
        one below it, or that wrote nothing but read the same document of one folder, talked to each other and went through. One room for an agent, a sure one before an unsure one."""
        rooms, taken = {}, set()
        by_room = collections.defaultdict(list)
        for a in self.agents:
            f = self._tag_room(a)
            if f:
                by_room[f].append(a)
        for folder in sorted(by_room):
            members = sorted((a for a in by_room[folder] if a.id not in taken), key=lambda a: (a.start or 0, a.id))
            if len(members) >= 2 and self.cat.unit_at(folder) is None and not self.cat.children(folder):          # (the folder of a bundle of debates is no room: its topics are what the tags point at)
                rooms[folder] = self._make_room(folder, members, 'tag', True)
                taken.update(a.id for a in members)
        groups = collections.defaultdict(list)
        for a in self.agents:
            if a.launch is not None and a.id not in taken:
                groups[a.launch.gkey()].append(a)
        counts = lambda p: len(self._writers(p))                    # noqa: E731
        for _gk, grp in sorted(groups.items(), key=lambda kv: str(kv[0])):
            grp = [a for a in grp if a.id not in taken]
            if len(grp) < 2:
                continue
            own = {}                                               # agent id -> the first file of its own (written by it alone, not in a debate folder)
            for a in grp:
                for p in self._own_files(a):
                    if counts(p) == 1 and self.cat.unit_at(os.path.dirname(p)) is None:
                        own[a.id] = p
                        break
            folders = {os.path.dirname(p) for p in own.values()}
            if len(own) >= 2 and len(folders) == 1:                # the folder that holds their own files themselves, the same for all of them: files in two folders make no room (the orchestrator's answer O1)
                folder = next(iter(folders))
                members = sorted((self.by_id[i] for i in own), key=lambda a: (a.start or 0, a.id))
                if folder not in rooms and self._room_folder_ok(folder) and not self._works_elsewhere(folder, members):
                    rooms[folder] = self._make_room(folder, members, 'launch', False)
                    taken.update(a.id for a in members)
            members_only = self._reading_room(grp, taken)
            if members_only:
                folder, members, guide = members_only
                rooms[folder] = Room(folder, 'members', False, 'launch', [a.id for a in members], {}, guide)
                taken.update(a.id for a in members)
        return rooms

    def _reading_room(self, grp, taken):
        """J14 (D6): the agents launched together that are left (two at least) that wrote no markdown file whole, all read one document of one folder, and talked to each other: a room of participants only."""
        rest = [a for a in grp if a.id not in taken]
        if len(rest) < 2 or any(w.kind in AUTHORING and w.path.endswith('.md') and self.sure(w) for a in rest for w in self.by_agent.get(a.id, ())):
            return None
        ids = {a.id for a in rest}
        if not any(to in ids and to != a.id for a in rest for to in a.sent_to):
            return None
        shared = set.intersection(*({os.path.normpath(r.path) for r in a.reads if r.path.endswith('.md')} for a in rest))
        members = sorted(rest, key=lambda a: (a.start or 0, a.id))
        for p in sorted(self.real(q) for q in shared):
            if self._room_ok(os.path.dirname(p), members):
                return os.path.dirname(p), members, p
        return None

    # ----- J9 J10: the list -----
    def root_of(self, unit):
        """The root of a bundle: the folder above a topic that has a `brief.md` and no round folder of its own, else the topic itself (J9)."""
        parent = os.path.dirname(unit)
        if parent != unit and not too_broad(parent) and self.cat.is_file(os.path.join(parent, 'brief.md')) and self.cat.unit_at(parent) is None:
            return parent
        return unit

    def listing(self, rooms):
        """J9: {unit or room folder: set of reasons}. A sure write of an agent or a request for a cell, a room tag, the orchestrator writing a guide and round folders there (hint), a walk, a room."""
        why = collections.defaultdict(set)
        self._index()
        for unit, ids in self._unit_writers.items():
            if ids:
                why[unit].add('cell')
        for unit, ids in self._unit_askers.items():
            if ids:
                why[unit].add('cell')
        for a in self.agents:
            f = self._tag_room(a)
            if f is None or f in rooms:
                continue
            if self.cat.unit_at(f) is not None:
                why[f].add('tag')
            else:
                for child in self.cat.children(f):
                    if self.root_of(child.path) == f:                      # the folder of a bundle (a guide, no round folder): its topics are what the tag points at
                        why[child.path].add('tag')
        for folder, h in self.sf.hints.items():
            folder = self.real(folder)
            u = self.cat.unit_at(folder)
            if u is not None and written_debate(u, self.cat):
                why[u.path].add('hint')
        for p in self.sf.walked:
            u = self.cat.unit_at(self.real(p))
            if u is not None:
                why[u.path].add('walk')
        for f in rooms:
            why[f].add('room')
        return why

    # ----- J11 J12: the places agents are thought to work -----
    def _outside_md(self, a):
        """The `.md` files an agent made whole with a sure write (any evidence)."""
        return [w.path for w in self.by_agent.get(a.id, ()) if w.kind in AUTHORING and w.path.endswith('.md') and self.sure(w)]

    def placements(self, roots, bound_units, involved):
        """J11: an agent that is no owner, editor or asker of any cell, for the roots something of the structure points it to: its room tag (sure), the call or message group that made the folder (`launch_call`),
        a peer launched with it that is tied to the folder for sure (`launch_peer`), the guide of the folder read (`guide_read`). Reasons that point at one root place the agent there (at the topic when
        they name one topic besides the root); at two or more roots it is placed nowhere (`launch_split`). An agent that wrote a markdown file of the work outside a root gives up the unsure reasons for it."""
        jd = self.jd
        topic_root = {U: R for R, us in roots.items() for U in us if U not in jd.rooms}
        hints = {self.real(f): h for f, h in self.sf.hints.items()}
        groups = collections.defaultdict(list)
        for a in self.agents:
            if a.launch is not None:
                groups[a.launch.gkey()].append(a)
        for a in self.agents:
            if a.id in involved:
                continue
            reasons, topics = collections.defaultdict(set), collections.defaultdict(set)

            def add(unit, why):
                R = topic_root.get(unit, unit)
                reasons[R].add(why)
                topics[R].add(unit)
            tag_root = None
            f = self._tag_room(a)
            if f:
                if f in jd.rooms:
                    tag_root = f
                    reasons[f].add('tag')
                elif f in topic_root:
                    tag_root = topic_root[f]
                    add(f, 'tag')
                elif f in roots:
                    tag_root = f
                    reasons[f].add('tag')
                    topics[f].add(f)
            if a.launch is not None:
                named = lambda h: h is not None and (a.launch.call in h.calls or a.launch.gkey() in h.groups)       # noqa: E731
                for R in sorted(set(topic_root.values())):
                    if named(hints.get(R)):                    # what was done in the root points at the root: every topic of it waits (O14)
                        reasons[R].add('launch_call')
                        topics[R].add(R)
                for U, R in topic_root.items():
                    if U != R and named(hints.get(U)):
                        add(U, 'launch_call')
                for b in groups[a.launch.gkey()]:
                    if b is not a:
                        for U in bound_units.get(b.id, ()):
                            if U in topic_root:
                                add(U, 'launch_peer')
            for r in a.reads:
                if os.path.basename(r.path) in GUIDE_NAMES:
                    folder = os.path.dirname(self.real(r.path))
                    if folder in topic_root:
                        add(folder, 'guide_read')
                    elif folder in roots and folder not in jd.rooms:
                        reasons[folder].add('guide_read')
                        topics[folder].add(folder)
            if reasons:
                outside = self._outside_md(a)
                for R in list(reasons):
                    top = repo_top(R, cat=self.cat)
                    work = {t for t in (top, *self.tops_of(a.cwd)) if t} if top else ()
                    if any(not (p == R or p.startswith(R + os.sep)) and _work_file(p, work) for p in outside):
                        reasons[R] -= {'launch_call', 'launch_peer', 'guide_read'}
                        if not reasons[R]:
                            del reasons[R]
            if tag_root is not None and tag_root in reasons:
                chosen = tag_root
            else:
                live = [R for R in reasons if reasons[R]]
                if len(live) > 1:
                    _diag(jd.diag, 'launch_split', a.id, None, ','.join(sorted(os.path.basename(R) for R in live))[:80])
                    continue
                if not live:
                    continue
                chosen = live[0]
            whys = [w for w in _WHY_RANK if w in reasons[chosen]]
            ts = topics[chosen]
            topic = next(iter(ts)) if len(ts) == 1 and next(iter(ts)) != chosen else None
            jd.placed[a.id] = Placement(a.id, chosen, topic, whys[0], whys, tag_root == chosen)

    # ----- J15 J16: the final of a debate -----
    def _final(self, scope, key, units, paths, parents, estimated):
        """J15. Returns (Final, live, estimated). `paths` are the cell files of the scope, `parents` the folders where the document of the final may be (the folder of the topic or the room, for a bundle the root and
        its `final` folder). Two times, Lc and La (principle U: what is uncertain only holds things back):
          Lc  the latest sure write on a cell file (any kind, anybody, the orchestrator too): it sets which documents are candidates, which compete, and whether a lost record matters;
          La  the latest write attempt on a cell file or on any markdown file of a round folder, sure or not, and the time the cell files themselves show now: only it withdraws a final (nothing may have changed after the final was written).
        The final is the one document that competes and was written by a sure tool or shell write not before La; competitors count by write attempts (a failed one, one that cannot be checked, a file
        that is gone or empty still competes) and by the candidates that came after Lc with no record at all (principle U); in a room, the other files its members made are theirs and no document of it.
        Not final while any cell is not submitted, a round folder is empty, somebody tied to it may still work, or a record that may hold a later write was not read."""
        jd = self.jd
        cp = self.cell_by_path
        pset = set(paths)
        who = set()
        for p in paths:
            c = cp[p]
            who |= {c.owner, c.agent, *c.editors}
            who |= {w.agent for w in self.by_path.get(p, ())}
            who |= {a.id for a in self.askers.get(p, ())}
        roots_of = {key} | {self.root_of(u) for u in units} | set(units)
        for a in self.agents:
            f = self._tag_room(a)
            if f is not None and f in roots_of:
                who.add(a.id)
        for aid, pl in jd.placed.items():
            if (scope == 'bundle' and pl.unit == key) or (scope == 'topic' and pl.unit == self.root_of(key) and pl.topic in (None, key)):
                who.add(aid)
        live = any(self.by_id[i].status in OPEN for i in who if i in self.by_id)
        conf = sorted((w for p in paths for w in self.by_path.get(p, ()) if self.sure(w)), key=lambda w: (w.ts, w.agent))
        if not any(w.agent != 'orch' and w.kind in AUTHORING for w in conf):
            return Final(key, scope, False, None, None, ['no_report'], [], None), live, estimated
        lc = max(w.ts for w in conf)
        rounds_of_scope = [os.path.join(u, d) for u in units for d in self._round_dirs(u)]
        file_attempts = [w.ts for u in units if self.cat.unit_at(u).file_rounds for w in self.by_dir.get(u, ()) if file_round_of(os.path.basename(w.path))]
        la = max([w.ts for p in paths for w in self.by_path.get(p, ())] + file_attempts + [w.ts for rd in rounds_of_scope for w in self.by_dir.get(rd, ()) if w.path.endswith('.md')]            # a try at a file of a round folder that is not there: it may have made it
                 + [m for m in (self.mtime(p) for p in paths) if m is not None])

        mine = set()                                            # a room: the other files that its members made are theirs, no candidate of the room (a file somebody else wrote at too is)
        if scope == 'room' and key in jd.rooms:
            members = set(jd.rooms[key].members)
            mine = {w.path for a in members for w in self.by_agent.get(a, ()) if w.kind in AUTHORING and self.sure(w) and all(x.agent in members for x in self.by_path.get(w.path, ()))}

        def spot(p):                                            # a place for the document of a final: no guide, no round document, no cell file
            b = os.path.basename(p)
            return os.path.dirname(p) in parents and p.endswith('.md') and b not in GUIDE_NAMES and not ROUND_DOC_RE.match(b) and p not in pset

        def place(p):
            return spot(p) and p not in mine
        cands = []
        for folder in sorted(parents):
            for name, kind in self.cat.listdir(folder) or ():
                p = os.path.join(folder, name)
                if kind == 'f' and place(p):
                    st = self.cat.stat(p)
                    if st is not None and st[2] > 0 and st[3] / 1e9 >= lc - SAME_TIME:
                        cands.append(p)
        cands.sort(key=lambda p: final_sort_key(os.path.basename(p), self.mtime(p)))
        attempts = [w for folder in parents for w in self.by_dir.get(folder, ()) if place(w.path) and w.ts >= lc - SAME_TIME]          # the competitors: write attempts, whatever came of them
        proven = sorted({w.path for w in attempts if w.evidence in ('tool', 'shell') and w.ts >= la - SAME_TIME and w.path in cands and (self.mtime(w.path) or 0) >= la - SAME_TIME and self.sure(w)})
        rivals = {w.path for w in attempts} | set(cands)                                         # a document that came or changed after Lc with no record at all competes too (principle U)
        if mine:                                                # O14a: the files of the members are no candidates, but they compete when they may be the later word
            wrote = [max(w.ts for w in attempts if w.path == q and w.evidence in ('tool', 'shell') and w.ts >= la - SAME_TIME and self.sure(w)) for q in proven]
            for m in sorted(mine):
                if not spot(m):
                    continue
                evs = self.by_path.get(m, ())
                after_final = wrote and any(w.kind in AUTHORING and w.ts >= min(wrote) - SAME_TIME for w in evs)          # written (or tried) when, or after, the final was written
                moved = self.mtime(m) is not None and self.mtime(m) > max(max((w.ts, *(w.span or ()))) for w in evs) + SAME_TIME     # changed after its last record: nobody's event says by whom
                if after_final or moved:
                    rivals.add(m)
        rivals = sorted(rivals)
        lost = any(x and x[1] >= lc - SAME_TIME for x in [a.lost for a in self.agents] + [self.sf.orch_lost])
        why = set()
        stats = {p: self.cat.stat(p) for p in paths}
        if any(c.state in NOT_SUBMITTED for c in (cp[p] for p in paths)):
            why.add('open_cell')
        if any(self.seat(p)[4] for p in paths):                  # a cell that is held (J3) or contested (J7) may be submitted by nobody
            why.add('open_cell')
        if any(st is not None and st[0] == 'f' and st[2] == 0 for st in stats.values()):
            why.add('open_cell')                                 # an empty cell file is no submission
        if not proven:
            why.add('none')
        if len(rivals) > 1:
            why.add('several')
        if any(not any(kind == 'f' and n.endswith('.md') for n, kind in (self.cat.listdir(os.path.join(u, d)) or ())) for u in units for d in self._round_dirs(u)):
            why.add('empty_round')
        if live:
            why.add('live_participant')
        if estimated:
            why.add('estimated_room')
        if lost:
            why.add('history_lost')
        why = [w for w in FINAL_WHY if w in why]
        path = proven[0] if not why else None
        by = next((w.agent for w in reversed(self.by_path.get(path, ())) if self.sure(w)), None) if path else None
        return Final(key, scope, not why, path, by, why, cands, la), live, estimated

    def _round_dirs(self, unit):
        u = self.cat.unit_at(unit)
        return sorted(d for dirs in (u.rounds.values() if u else ()) for d in dirs)

    # ----- the whole judgment -----
    def run(self):
        jd = self.jd
        cat = self.cat
        rooms = self.rooms()
        jd.rooms = rooms
        why = self.listing(rooms)
        roots = collections.defaultdict(set)
        for U in why:
            roots[U if U in rooms else self.root_of(U)].add(U)
        for R in list(roots):
            if R in rooms or cat.unit_at(R) is not None:
                continue
            if all(why[U] == {'hint'} for U in roots[R]):
                continue                                         # a debate that is on the list only because the orchestrator wrote there brings in nothing around it
            for child in cat.children(R):
                roots[R].add(child.path)
        self._index()
        # J17: copies of one folder (the same real path, or the same place of the checkouts of one repository): a copy that nothing of this session is tied to is not shown, nothing is moved
        from . import copies
        kept, folded = copies.fold(sorted(roots), lambda group: self._evidence(group, roots), cat=cat)
        for R in roots.keys() - set(kept):
            del roots[R]
        jd.folded = {k: sorted(v) for k, v in folded.items()}
        topics = sorted({U for R, us in roots.items() if R not in rooms for U in us})
        for U in topics:
            jd.units[U] = cat.unit_at(U)
        for R, us in roots.items():
            if R in rooms:
                jd.units[R] = Unit(id=R, path=R, brief=rooms[R].guide)
            elif R not in jd.units:
                jd.units[R] = Unit(id=R, path=R, brief=os.path.join(R, 'brief.md'))
        # the cells
        for U in topics:
            for p in self.unit_cell_paths(U):
                ck = self.cell_key(p)
                jd.cells[ck] = self.cell(ck[0], ck[1], ck[2], p)
        for F, room in rooms.items():
            paths = list(dict.fromkeys(room.files.values()))
            if room.kind == 'cells':
                for aid in room.members:                              # the seat a member's tag names is a cell of the room too (it may be a file other than its own)
                    paths += [q for q, dem in self.demand[aid].items() if os.path.dirname(q) == F and any(k == 'tag' for _t, k, _r in dem) and q not in paths]
            seats = {path: os.path.splitext(os.path.basename(path))[0] for path in paths}
            taken = collections.Counter(seats.values())
            for path in paths:
                stem = seats[path] if taken[seats[path]] == 1 else os.path.splitext(os.path.relpath(path, F))[0]
                jd.cells[(F, '-', stem)] = self.cell(F, '-', stem, path, room=True)
        self.cell_by_path = {c.path: c for c in jd.cells.values()}
        # who is tied to what
        bound = collections.defaultdict(set)
        involved = set()
        for c in jd.cells.values():
            ids = ({c.owner, c.agent, *c.editors} | {a.id for a in self.askers.get(c.path, ())}) - {None, 'orch'}
            bound[c.unit] |= ids
            involved |= ids
        for a in self.agents:
            f = self._tag_room(a)
            if f is None:
                continue
            for U in ([f] if f in rooms or f in topics else sorted(roots.get(f, ())) if f in roots else []):
                bound[U].add(a.id)
        bound_units = collections.defaultdict(set)
        for U, ids in bound.items():
            for i in ids:
                bound_units[i].add(U)
        jd.bound = {U: set(ids) for U, ids in bound.items()}
        # the listing
        for R, us in roots.items():
            reasons = set()
            for U in us:
                reasons |= why.get(U, set())
            jd.listed[R] = next(r for r in _REASON_RANK if r in reasons) if reasons else 'walk'
            if reasons == {'hint'}:
                jd.hinted.add(R)
        jd.roots = {R: sorted(us) for R, us in roots.items()}
        self.placements(roots, bound_units, involved)
        # what an agent is tied to
        for a in self.agents:
            us = set(bound_units.get(a.id, ()))
            pl = jd.placed.get(a.id)
            if pl:
                us.add(pl.topic or pl.unit)
            if us:
                jd.worked[a.id] = us
        for c in jd.cells.values():
            if c.owner:
                jd.agent_units.setdefault(c.owner, set()).add(c.unit)
        for (unit, rdir, stem), c in sorted(jd.cells.items()):
            if c.owner:
                owner, _agent, _ed, ev, _dg, _held, taken = self.seat(c.path)
                for prev, end, prev_ev in taken:
                    jd.assignments.append(self._assignment(prev, c, prev_ev, end))
                jd.assignments.append(self._assignment(owner, c, ev, None))
        # the finals
        for U in topics:
            paths = self.unit_cell_paths(U)
            jd.finals[U], self._live[U], _est = self._final('topic', U, [U], paths, {U}, False)
        for F, room in rooms.items():
            paths = [c.path for c in jd.cells.values() if c.unit == F]
            jd.finals[F], live, est = self._final('room', F, [], paths, {F}, not room.sure)
            jd.closable[F] = jd.finals[F].confirmed and not live and room.sure
        for R, us in roots.items():
            if R in rooms or R in topics:
                continue
            paths = [p for U in sorted(us) for p in self.unit_cell_paths(U)]
            jd.finals[R], _live, _est = self._final('bundle', R, sorted(us), paths, {R, os.path.join(R, 'final')}, False)
        for U in topics:
            R = self.root_of(U)
            fr = jd.finals.get(R) if R != U and R in roots else None
            jd.closable[U] = bool((jd.finals[U].confirmed or (fr is not None and fr.confirmed)) and not self._live[U])
        # the order of the list
        for R, us in roots.items():
            jd.unit_ts[R] = max([self.by_id[i].start or 0 for U in us for i in bound.get(U, ())] + [self.hint_ts(U) for U in (*us, R)] + [0])
        jd.order = sorted(roots, key=lambda R: (not any(bound.get(U) for U in (*roots[R], R)), R in jd.hinted, R in rooms and not rooms[R].sure, -jd.unit_ts[R], R))
        jd.current = next((R for R in jd.order if not (R in rooms and not rooms[R].sure)), None)
        self._diagnostics(roots, topics)
        return jd

    def hint_ts(self, folder):
        for f, h in self.sf.hints.items():
            if self.real(f) == folder:
                return h.ts or 0
        return 0

    def _assignment(self, aid, c, evidence, end):
        a = self.by_id[aid]
        ev = Evidence(kind=evidence or 'tool', subject=aid, field='seat', candidates=[c.unit, c.round, c.stem, c.rdir], rank=1)
        return Assignment(agent=aid, unit=c.unit, round=c.round if c.round is not None else 1, seat=c.stem, start=a.start, end=end, evidence=[ev])

    def _evidence(self, folders, roots):
        """J17: what this session is tied to in each folder of a group of copies: (cells that are written or asked for, room tags, hints). A read is none."""
        out = {}
        for R in folders:
            us = roots.get(R, ())
            cells = sum(len(self._writers_by_unit(U)) + len(self._askers_by_unit(U)) for U in us)
            tags = sum(1 for a in self.agents if (self._tag_room(a) or '') in (R, *us))
            hints = sum(1 for f in self.sf.hints if self.real(f) in (R, *us))
            out[R] = (cells, tags, hints)
        return out

    def _writers_by_unit(self, U):
        return self._unit_writers.get(U, ())

    def _askers_by_unit(self, U):
        return self._unit_askers.get(U, ())

    def _diagnostics(self, roots, topics):
        jd = self.jd
        for (unit, rdir, stem), c in jd.cells.items():
            for code, agent in self.seat(c.path)[4]:
                _diag(jd.diag, code, agent, unit, '%s/%s' % (c.round if c.round is not None else '-', stem))
        for U in topics:
            u = jd.units[U]
            for n, dirs in u.rounds.items():
                if len(dirs) > 1:
                    _diag(jd.diag, 'alias_collision', None, U, ','.join(dirs))
        jd.diag.sort(key=lambda d: (d['code'], d['unit'] or '', d['detail'] or '', d['agent'] or ''))


def assign(sf, cat, tops_of=None):
    """The judgment of a session: `sf` the SessionFacts, `cat` the Catalog (every look at the disk goes through it; the lookups are `cat.reads()` afterwards), `tops_of` a function from a working folder to
    the folders above it that make a repository (the git top). Pure: the same facts and the same answers of the disk give the same Judgement."""
    cat.begin()
    return _Judge(sf, cat, tops_of).run()
