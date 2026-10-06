"""The same folder, seen more than once: a link into a folder, and the copies of one repository's folder in its linked worktrees.

A debate folder that a repository keeps (docs/records/...) is in every worktree of that repository, and a folder can be reached through a link too. Two folders are the same debate
when their real path is the same, or when they are in checkouts of one repository (the same common git folder) at the same place in it. This reads two small files at most for a
checkout and never runs git.

`fold` hides the copies this session has nothing to do with: a copy that a cell was written or asked for in, that a room tag or the orchestrator's hint points at, is a debate of its own and
stays, and the copies with none of that are not shown beside one that has. Where no copy has any (the walk found them all, nothing of the session is in any), one stands for them: the one in
the main checkout, else the one by its real path, else the shortest (the order of 0.2.1; O10). Nothing is moved from a hidden copy to another: what was read or written in it is no evidence for the others.

Standard library only; Python 3.9 compatible.
"""

import os
import re
import stat

from .units import repo_top

GITDIR_RE = re.compile(r'^gitdir:\s*(.+?)\s*$')
POINTER_MAX = 4096                    # a `.git` file or a `commondir` file is one short line


def _first_line(path, cat=None):
    """The first line of a small regular file, or None (a folder, a FIFO, a missing or an unreadable file: nothing is opened that could block)."""
    if cat is not None:
        return cat.first_line(path)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, 'rb') as f:
            data = f.read(POINTER_MAX)
    except OSError:
        return None
    lines = data.decode('utf-8', 'replace').splitlines()
    return lines[0].strip() if lines else ''


def git_common_dir(top, cat=None):
    """The common git folder of the checkout whose top is `top` (real path): its `.git` folder for a main checkout; for a linked worktree (a `.git` file that says
    `gitdir: <path>`) the folder the `commondir` file of that git folder names; for a submodule (no `commondir`) the git folder itself. None when `top` is no checkout
    or its pointer is broken."""
    real = cat.realpath if cat is not None else os.path.realpath
    isdir = cat.is_dir if cat is not None else os.path.isdir
    dot = os.path.join(top, '.git')
    if isdir(dot):
        return real(dot)
    line = _first_line(dot, cat)
    m = GITDIR_RE.match(line or '')
    if not m:
        return None
    gd = real(os.path.join(top, m.group(1)))
    if not isdir(gd):
        return None
    common = _first_line(os.path.join(gd, 'commondir'), cat)
    if common:
        common = real(os.path.join(gd, common))
        return common if isdir(common) else None
    return gd


def identity(path, memo=None, cat=None):
    """What makes two folders the same folder: ('git', common git folder, the place in the repository) for a folder in a checkout, else ('real', real path). `memo` keeps
    the common folder of each checkout top between calls; `cat` asks the disk through a Catalog."""
    memo = {} if memo is None else memo
    real = cat.realpath(path) if cat is not None else os.path.realpath(path)
    top = repo_top(real, cat=cat)
    if top:
        if top not in memo:
            memo[top] = git_common_dir(top, cat)
        if memo[top]:
            return ('git', memo[top], os.path.relpath(real, top))
    return ('real', real)


def is_main_checkout(path, cat=None):
    """Whether the folder is in a main checkout (the top has a `.git` folder), not in a linked worktree."""
    top = repo_top(cat.realpath(path) if cat is not None else os.path.realpath(path), cat=cat)
    return bool(top) and (cat.is_dir(os.path.join(top, '.git')) if cat is not None else os.path.isdir(os.path.join(top, '.git')))


def fold(roots, evidence, memo=None, cat=None):
    """Of the folders in `roots` that are the same folder, the copies this session has nothing to do with are not shown. `evidence(folders)` is asked once, for every folder that has a copy among
    the others, and returns {folder: (cells, tags, hints)}: how many cells were written or asked for in it, room tags and hints of the orchestrator that point at it (a read is none). Returns
    (kept, folded): `kept` the folders that stay (in the order given), `folded` {kept folder: [the copies hidden]}. A copy with any evidence stays, and the others are hidden; where no copy has any,
    the first of them (the main checkout, a real path, a short one) stays and the others are hidden."""
    memo = {} if memo is None else memo
    groups = {}
    for r in roots:
        groups.setdefault(identity(r, memo, cat), []).append(r)
    copied = [g for g in groups.values() if len(g) > 1]
    if not copied:
        return list(roots), {}
    ev = evidence([r for g in copied for r in g])
    real = cat.realpath if cat is not None else os.path.realpath
    drop, folded = set(), {}
    for group in copied:
        order = sorted(group, key=lambda r: (-sum(ev[r]), not is_main_checkout(r, cat), real(r) != r, len(r), r))
        keep = [r for r in order if sum(ev[r]) > 0] or order[:1]       # where no copy has any evidence one stands for all of them (O10)
        for r in order:
            if r not in keep:
                drop.add(r)
                folded.setdefault(keep[0], []).append(r)
    return [r for r in roots if r not in drop], folded
