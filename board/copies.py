"""The same folder, seen more than once: a link into a folder, and the copies of one repository's folder in its linked worktrees.

A debate folder that a repository keeps (docs/records/...) is in every worktree of that repository, and a folder can be reached through a link too. The board shows the debate
once. Two folders are the same debate when their real path is the same, or when they are in checkouts of one repository (the same common git folder) at the same place
in it. This reads two small files at most for a checkout and never runs git.

`fold` picks, of the copies of a folder, the one that stands for them: the one this session's agents sit in, else write in (a write that is known to have worked), else read or
name, else the main checkout, else a real folder (not one reached through a link). A copy that holds a seat of this session stays a debate of its own: a cell is never hidden.
A copy is a folder, not a file: what an agent read or wrote in a folded copy belongs to the report of the folder that stands for it only when it is the same report (same_file).

Standard library only; Python 3.9 compatible.
"""

import os
import re
import stat

from .units import repo_top
from .util import open_safe, stat_regular

GITDIR_RE = re.compile(r'^gitdir:\s*(.+?)\s*$')
POINTER_MAX = 4096                    # a `.git` file or a `commondir` file is one short line
COMPARE_MAX = 1 << 20                 # two reports bigger than this are not compared


def _first_line(path):
    """The first line of a small regular file, or None (a folder, a FIFO, a missing or an unreadable file: nothing is opened that could block)."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, 'rb') as f:
            data = f.read(POINTER_MAX)
    except OSError:
        return None
    lines = data.decode('utf-8', 'replace').splitlines()
    return lines[0].strip() if lines else ''


def git_common_dir(top):
    """The common git folder of the checkout whose top is `top` (real path): its `.git` folder for a main checkout; for a linked worktree (a `.git` file that says
    `gitdir: <path>`) the folder the `commondir` file of that git folder names; for a submodule (no `commondir`) the git folder itself. None when `top` is no checkout
    or its pointer is broken."""
    dot = os.path.join(top, '.git')
    if os.path.isdir(dot):
        return os.path.realpath(dot)
    line = _first_line(dot)
    m = GITDIR_RE.match(line or '')
    if not m:
        return None
    gd = os.path.realpath(os.path.join(top, m.group(1)))
    if not os.path.isdir(gd):
        return None
    common = _first_line(os.path.join(gd, 'commondir'))
    if common:
        common = os.path.realpath(os.path.join(gd, common))
        return common if os.path.isdir(common) else None
    return gd


def identity(path, memo=None):
    """What makes two folders the same folder: ('git', common git folder, the place in the repository) for a folder in a checkout, else ('real', real path). `memo` keeps
    the common folder of each checkout top between calls."""
    memo = {} if memo is None else memo
    real = os.path.realpath(path)
    top = repo_top(real)
    if top:
        if top not in memo:
            memo[top] = git_common_dir(top)
        if memo[top]:
            return ('git', memo[top], os.path.relpath(real, top))
    return ('real', real)


def is_main_checkout(path):
    """Whether the folder is in a main checkout (the top has a `.git` folder), not in a linked worktree."""
    top = repo_top(os.path.realpath(path))
    return bool(top) and os.path.isdir(os.path.join(top, '.git'))


def same_file(a, b):
    """Whether two paths are one report: the same file under two names (the same real path), or two regular files with the same bytes. A file that is missing, is no regular
    file, is bigger than COMPARE_MAX, or is refused to be opened (a dot path, a secret name, an auth file) is not told to be the same as another."""
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    if ra == rb:
        return os.path.isfile(ra)
    pa, pb = stat_regular(a), stat_regular(b)
    if not pa or not pb or pa[1].st_size != pb[1].st_size or pa[1].st_size > COMPARE_MAX:
        return False
    try:
        with open_safe(a, binary=True) as fa, open_safe(b, binary=True) as fb:
            return fa.read(COMPARE_MAX + 1) == fb.read(COMPARE_MAX + 1)
    except OSError:
        return False


def fold(roots, evidence, memo=None):
    """Of the folders in `roots` that are the same folder, one stands for the others. `evidence(folders)` is asked once, for every folder that has a copy among the others, and returns
    {folder: (seats, writes, touches)}: how often this session's agents hold a seat in it, write in it (a write that worked), read or name it. Returns (kept, folded): `kept` the folders that stay
    (in the order given), `folded` {kept folder: [the copies it stands for]}. A copy with a seat stays, so that none of its cells is hidden."""
    memo = {} if memo is None else memo
    groups = {}
    for r in roots:
        groups.setdefault(identity(r, memo), []).append(r)
    copied = [g for g in groups.values() if len(g) > 1]
    if not copied:
        return list(roots), {}
    ev = evidence([r for g in copied for r in g])
    drop, folded = set(), {}
    for group in copied:
        order = sorted(group, key=lambda r: (-ev[r][0], -ev[r][1], -ev[r][2], not is_main_checkout(r), os.path.realpath(r) != r, len(r), r))
        for r in order[1:]:
            if ev[r][0] == 0:
                drop.add(r)
                folded.setdefault(order[0], []).append(r)
    return [r for r in roots if r not in drop], folded
