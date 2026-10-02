"""Reading debates (topic × round × participant): builds the cell structure and state from the debate folders on disk (units.py) and from what the agents'
instructions, reads and writes say about them. Four separate judgments: the list of debate folders, which agent is tied to which, who holds which seat
(units.assign), and what the seat's cell shows (units.cell_state)."""

import collections
import os
import re

from . import copies as C, units as U
from .units import (GUIDE_ABS_RE as BRIEF_ABS_RE, GUIDE_REL_RE as BRIEF_REL_RE, MARKER_RE, MARKER_SCAN, REL_DIR_REPORT_RE, REL_REPORT_RE, REPORT_RE,  # noqa: F401
                    ROUND_RE, WRITE_AFTER_RE, WRITE_BEFORE_RE, AgentFacts, Catalog, cell_state, round_dir_name, stem_aliases, stem_letter, write_intent)
from .util import FILE_MAX, Denied, open_safe, short_path, stat_plain, stat_regular, write_made


_HAS_DIR_RE = U._HAS_DIR_RE
_HAS_REL_RE = U._HAS_REL_RE


def report_refs(text):
    """The REPORT_RE matches in text (empty if none). Only a filter to get the same result faster."""
    return REPORT_RE.finditer(text) if _HAS_DIR_RE.search(text) else ()


def rel_report_refs(text):
    return REL_REPORT_RE.finditer(text) if _HAS_REL_RE.search(text) else ()


def agent_cwd(a):
    """Absolute working folder of an agent: its own records (Claude subagent, claude -p child), Codex index entry, else the link's cwd."""
    c = getattr(a, 'cwd', '') or ((a.cli or {}).get('cwd') if getattr(a, 'cli', None) else '') or ''
    return os.path.normpath(c) if c and os.path.isabs(c) else ''


def real_unit(unit):
    """Whether this is a real debate folder: it has a guide (brief.md; README.md or index.md when it declares participants or reports) or a round folder
    (r1, r01, round1). Checked when it is the base for resolving a relative path in text (when a Codex working folder was the repository top, text like
    "reads r1/C.md" used to create a fake debate). The folder is looked at on disk; a name alone settles nothing."""
    return U.read_unit(os.path.normpath(unit)) is not None


HEAD_CHARS = 200 * 200   # number of leading characters read from a debate folder's brief.md


def writer_table(s, at=None):
    """path → the agent that wrote to that path first (by write time, then by id if equal). Does not depend on the order the agents were found. `at` maps a path to the path that stands
    for it (a write in a folded copy is a write of the report shown when it is the same report)."""
    best = {}
    for a in s.agents.values():
        for w in a.writes:
            path = at(w['path']) if at else w['path']
            k = (w['ts'] or 0, a.id)
            if path not in best or k < best[path]:
                best[path] = k
    return {path: k[1] for path, k in best.items()}


def read_head(s, path):
    """The first 40 thousand characters of a text file. So that the same brief.md is not read again at every state build, the remembered text is returned if (mtime, size) is the same.
    The brief (brief.md) has a fixed name and its text is not shown as it is, so a regular file in place is read even under a dot folder (that is what makes the debate table and
    title of a hidden project folder stand up). If the file is reached through a link, its target gets the strict dot-path and secret-name rules (open_safe). An auth-file link and anything that is
    not a regular file (FIFO, device, folder) is a missing text (''), and the deny check is redone without a cache right before opening."""
    plain = stat_regular(path)
    if not plain:
        s._head_cache.pop(path, None)
        return ''
    st = plain[1]
    key = (st.st_mtime_ns, st.st_size)
    c = s._head_cache.get(path)
    if c and c[0] == key:
        return c[1]
    try:
        with open_safe(path, strict=False) as f:
            text = f.read(HEAD_CHARS)
    except OSError:
        s._head_cache.pop(path, None)
        return ''
    s._head_cache[path] = (key, text)
    return text


def count_lines(path):
    """Number of newlines. Reads only up to FILE_MAX (for a bigger file, the line count within the first FILE_MAX). The deny check is redone right before opening (open_safe).
    None = a denied target such as an auth file (a missing file), 0 = a dot path or secret name, or cannot be opened (the cell stays, only the line count is 0)."""
    n, left = 0, FILE_MAX
    try:
        with open_safe(path, binary=True) as f:
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    break
                n += b.count(b'\n')
                left -= len(b)
    except Denied as e:
        return None if e.why == 'denied' else 0
    except OSError:
        return 0
    return n


def file_info(s, path, strict=False):
    """{exists, size, lines, mtime} of a debate cell or final file. For a cell (report) and for the final named in the brief table only metadata (existence, size, time) is looked at,
    so the cell stands even for a dot path or secret name (strict=False). Ones found by searching a folder (auto_final candidates, finals list) use strict=True: a dot path or secret name is a missing file too.
    Either way an auth-file link and anything that is not a regular file (FIFO, device, folder) is a missing file (None). Lines are recounted only when (mtime, size) changes."""
    plain = stat_plain(path) if strict else stat_regular(path)
    if not plain:
        s._file_cache.pop(path, None)
        return None
    st = plain[1]
    key = (st.st_mtime, st.st_size)
    c = s._file_cache.get(path)
    if not c or c[0] != key:
        lines = count_lines(path)
        if lines is None:                          # it turned out right before opening that the target is denied
            s._file_cache.pop(path, None)
            return None
        c = (key, {'exists': True, 'size': st.st_size, 'lines': lines, 'mtime': st.st_mtime})
        s._file_cache[path] = c
    return c[1]


# the rule for finding the final in the topic folder when the brief table has no final deliverable (auto_final)
CONCLUDE_RE = re.compile(r'ruling|final|verdict|decision|conclusion|summary|plan|(?<![a-z])clos(?:e|ing|ure)|결론|판정|합의|최종|정리|종결|마무리', re.I)
OPEN_CELLS = ('draft', 'writing', 'paused')      # cells of a round that is not over: a conclusion is not looked for while one of them is there
SAME_TIME = 2.0      # seconds. Within this, times count as the same (depending on copy order, a conclusion file can be written 1 ms before the reports)
ROUND_DOC_RE = re.compile(r'^(brief|round\d+.*|r\d+[_-].*)\.md$', re.I)      # the brief and the round instruction files are not conclusions


def auto_final(s, unit, rows, skip=()):
    """For a topic whose brief table has no final deliverable: a document directly under the topic folder that is the same as or later than the last round's reports is taken as the final
    (excluding brief.md and round<N>.md, and the stems in `skip`: the result files of a flat review are its reports, not its conclusion). Names that look like a conclusion
    first, then the most recent. Not searched for if a cell is still open (being written, or paused by a cut-off run) or if no report has been submitted. When the
    next round starts after a conclusion, the new report is more recent, so it goes back to in progress by itself."""
    cells = [c for r in rows for c in r['cells']]
    if any(c['state'] in OPEN_CELLS for c in cells):
        return None, None
    last = max((c['mtime'] or 0 for c in cells if c['state'] == 'done'), default=0)
    if not last or not os.path.isdir(unit):
        return None, None
    cands = []
    for f in os.listdir(unit):                    # candidates are picked by metadata (stat) alone: among regular files with a fitting name, those whose time and size fit
        if not f.endswith('.md') or ROUND_DOC_RE.match(f) or f[:-3] in skip:
            continue
        plain = stat_plain(os.path.join(unit, f))
        if plain and plain[1].st_size > 0 and plain[1].st_mtime >= last - SAME_TIME:   # a cloned or copied folder has nearly the same times
            cands.append((bool(CONCLUDE_RE.search(f[:-3])), plain[1].st_mtime, f))
    for _, _, f in sorted(cands, reverse=True):   # only the one picked has its lines counted (if it cannot be opened, the next candidate)
        info = file_info(s, os.path.join(unit, f), strict=True)
        if info:
            return f, info
    return None, None


def names_topic(text, unit, root):
    """Whether a document names the topic folder `unit` of the bundle `root`: its path (absolute, or from the root: `part/topic`), or its name as a path (`topic/`, `` `topic` ``,
    "topic"); a name that carries a digit, an underscore or a dash (step2, t7_gate) is no word of the prose and counts as it stands. Never a name that only starts or ends
    like it (step20, xstep2, step2.md)."""
    if not text:
        return False
    name, rel = os.path.basename(unit), os.path.relpath(unit, root)
    forms = {unit, rel}
    if rel != name:
        forms.add(name)
    for form in forms:
        base = r'(?<![\w.-])' + re.escape(form) + r'(?![\w-]|\.\w)'
        if form == name and not re.search(r'[\d_-]', form):
            base = r'(?:(?<![\w.-])' + re.escape(form) + r'/|[`"\'(\[]' + re.escape(form) + r'(?![\w-]|\.\w))'
        if re.search(base, text):
            return True
    return False


# words that say a document leaves something open: it is then no closing of the whole bundle
OPEN_RE = re.compile(r'incomplete|unfinished|unresolved|pending|\bto-?do\b|not\s+(?:yet|done|complete|completed|closed|finished|final)|still\s+(?:open|in\s+progress)|in\s+progress|remain(?:s|ed|ing)?\b|미완|미종결|미해결|남[았아은음]|아직|진행\s*중|보류|후속', re.I)


def closes_bundle(text, topics, root):
    """Whether a document of the bundle's own folder can be read as the closing of every topic of the bundle: it names none of the topics (what names one is about that one, and
    says nothing of the others) and says nothing is left open. When it is not clear, it is not."""
    return bool(text) and not OPEN_RE.search(text) and not any(names_topic(text, t, root) for t in topics)


def closing_above(s, unit, root, rows, active=0.0, bundle=0.0, topics=()):
    """For a topic that has no conclusion of its own: a conclusion document in a folder above it, up to the root of its bundle (what ends a bundle, CLOSING.md, is written next
    to the topics). The name is a conclusion's (CONCLUDE_RE; not a guide or a round document), the document is not older than the topic's last activity (its done cells, and `active`:
    the documents written in its folder), and it either names the topic's folder (names_topic) or, in the bundle's own folder, is not older than everything the bundle did (`bundle`)
    and can be read as the bundle's closing (closes_bundle over `topics`, the topics of the bundle): a document that names other topics closes those only.
    A topic still open, or with no report in, is not closed. Returns (name relative to the topic, info) like auto_final: '../CLOSING.md'."""
    cells = [c for r in rows for c in r['cells']]
    done = [c['mtime'] or 0 for c in cells if c['state'] == 'done']
    if any(c['state'] in OPEN_CELLS for c in cells) or not done:
        return None, None
    unit, root = os.path.normpath(unit), os.path.normpath(root)
    last = max(done + [active])
    folders, d = [], os.path.dirname(unit)
    while unit != root and (d == root or d.startswith(root + os.sep)):
        folders.append(d)
        if d == root:
            break
        d = os.path.dirname(d)
    cands = []
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for f in names:
            if not f.endswith('.md') or ROUND_DOC_RE.match(f) or not CONCLUDE_RE.search(f[:-3]):
                continue
            path = os.path.join(folder, f)
            plain = stat_plain(path)
            if not plain or plain[1].st_size <= 0 or plain[1].st_mtime < last - SAME_TIME:
                continue
            text = read_head(s, path)
            named = names_topic(text, unit, root)
            if named or (folder == root and plain[1].st_mtime >= bundle - SAME_TIME and closes_bundle(text, topics, root)):       # one that names no topic speaks for the bundle only from the bundle's own folder
                cands.append((named, plain[1].st_mtime, path))
    for _, _, path in sorted(cands, reverse=True):
        info = file_info(s, path, strict=True)
        if info:
            return os.path.relpath(path, unit), info
    return None, None


def brief_table(text):
    """Reads the table of the shared brief with the columns '| 주제 | 폴더 | 선행 | 최종 산출물 |' (topic | folder | prerequisite | final deliverable)."""
    out = {}
    header = None
    for line in text.splitlines():
        if not line.startswith('|'):
            header = None if not line.strip() else header
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
            continue
        if header is None:
            header = cells
            continue
        folder = next((re.sub(r'[`/]', '', c) for c in cells if re.match(r'^`[^`]+/`$', c)), None)
        if not folder:
            continue
        row = {'name': cells[0]}
        for h, c in zip(header, cells):
            if '선행' in h or 'depend' in h.lower():
                row['deps'] = c.replace('—', '').strip()
            m = re.search(r'`(final/[^`]+)`', c)
            if m:
                row['final'] = m.group(1)
        out[folder] = row
    return out


def peer_names(s):
    """{name: agent id} for the names a message to another agent can use: its id, its tag, its model name, its description. A name that two agents share names none."""
    out = {}
    for a in s.agents.values():
        for n in (a.id, a.tag, a.auto_tag, a.description):
            n = (n or '').lower()
            if n:
                out[n] = a.id if out.get(n, a.id) == a.id else None
    return out


def facts_of(a, launcher_cwd='', names=None):
    """The AgentFacts of an agent: what its instructions, reads and writes say, for the seat judgment (units.assign).
    Inputs the integrator can add on the agent without touching this file:
      a.writes[i]['ok']   True or False once the tool result of that Write/Edit is known (a failed write is no seat); absent while it is not
      a.shell_writes      the markdown files its Bash calls wrote by a redirect or tee (agents.shell_writes), each with the `ok` of its call: a write like the above, except that a
                          call that succeeded is believed only when the file is there (the redirect may sit in a branch that did not run)
      a.redirects         the output redirects of the launch command (facts.Redirect or dicts with fd, op, path_resolved): a `>` or `-o` that names a report is its write intent
    `names` (peer_names) tells who the messages it sent (`a.sent`) went to: the agents of the same session, never itself. A message whose result is known to be an error (`ok` False) went
    to nobody; one whose result is not known (`ok` None, or absent in an older record) counts as sent."""
    texts = [a.spawn_prompt or ''] + [m['text'] for m in a.received] + [m['text'] for m in a.orch_msgs]
    planned, ops, unresolved = [o['path'] for o in a.out_paths], {o['path']: '-o' for o in a.out_paths}, False
    for r in getattr(a, 'redirects', None) or ():
        get = r.get if isinstance(r, dict) else (lambda k, _r=r: getattr(_r, k, None))
        if get('fd') in (1, None) and get('op') in ('>', '>>', '-o'):
            if get('path_resolved'):
                path = os.path.normpath(get('path_resolved'))
                planned.append(path)
                ops.setdefault(path, get('op'))
            elif get('unresolved_vars'):
                unresolved = True
    sent_to = sorted({names[m['to'].lower()] for m in a.sent if m.get('ok') is not False and names.get(m['to'].lower()) not in (None, a.id)}) if names else []
    writes = [(w['path'], w.get('ok')) for w in a.writes]
    shell = set()
    for w in getattr(a, 'shell_writes', ()):
        writes.extend((path, False if w.get('ok') is False else None) for path in w['paths'])        # None: the file on disk is the stand-in for the result (units.write_failed)
        shell.update(w['paths'])
    return AgentFacts(id=a.id, key=a.key, cwd=agent_cwd(a), launcher_cwd=launcher_cwd, start=a.spawn_ts or a.first_ts or 0, last=a.last_ts or 0, first=a.first_ts or 0,
                      spawn_prompt=a.spawn_prompt or '', texts=texts, reads=list(a.reads), writes=writes, planned=planned,
                      planned_ops=ops, unresolved=unresolved, sent_to=sent_to, shell_only=frozenset(shell - {w['path'] for w in a.writes}))


def _catalog(s):
    """The debate-folder catalog of a session: kept on it so that a folder is not read again at every state build."""
    cat = getattr(s, '_unit_catalog', None)
    if cat is None:
        cat = Catalog(text_of=lambda p: read_head(s, p))
        try:
            s._unit_catalog = cat
        except AttributeError:
            pass
    return cat


def _evidence(s, jd, folders):
    """{folder: (seats, writes, touches)} of the folders that are copies of one another: how often this session's agents hold a seat in each, write in it, and read or are tied to it.
    A path counts for every folder above it. Only a write that is known to have worked counts (a failed one, or one whose result is not known, says nothing about where the work is);
    what a shell call wrote counts when its call worked and the file is there."""
    ev = {r: [0, 0, 0] for r in folders}

    def bump(path, i):
        d = path
        while True:
            if d in ev:
                ev[d][i] += 1
            parent = os.path.dirname(d)
            if parent == d:
                return
            d = parent

    for asg in jd.assignments:
        bump(asg.unit, 0)
    for a in s.agents.values():
        for w in a.writes:
            if write_made(w):
                bump(w['path'], 1)
        for w in getattr(a, 'shell_writes', ()):
            if write_made(w):
                for path in w['paths']:
                    if os.path.exists(path):
                        bump(path, 1)
        for path in a.reads:
            bump(path, 2)
    for us in jd.members.values():
        for u in us:
            bump(u, 2)
    return {r: tuple(v) for r, v in ev.items()}


def _mapper(copies):
    """A function from a path to the same place under the folder that stands for its copy ({copy: the folder shown}); a path in no copy is itself."""
    if not copies:
        return lambda path: path

    def at(path):
        d = path
        while True:
            rep = copies.get(d)
            if rep is not None:
                return rep + path[len(d):]
            parent = os.path.dirname(d)
            if parent == d:
                return path
            d = parent
    return at


def _above(path):
    """The folders above a path, nearest first."""
    out, d = [], os.path.dirname(path)
    while d and d != os.path.dirname(d):
        out.append(d)
        d = os.path.dirname(d)
    return out


Judged = collections.namedtuple('Judged', 'debates agent_units members assignments diag')


def judge(s, statuses):
    """The debate structure, with everything the judgments found: Judged(debates, agent_units, members, assignments, diag).
    debates      the list the board shows (see below)
    agent_units  {agent id: {unit}} the debates an agent works in: where it holds a seat, and where it only reads, is held, or names a report without quoting it
    members      {agent id: {unit}} every debate folder an agent is tied to: a quotation, a negation or a guide it merely names too
    assignments  facts.Assignment list (the seats, with the evidence and the period each was held)
    diag         {code, agent, unit, detail} dicts (debate diagnostics: path_ambiguous, path_unresolved, alias_collision, seat_tie_held, debate_in_misc, declaration_missing)
    Extra inputs the integrator may set on the session: `s.walked_units` (the paths units.walk_repo found in the background stage: debates on disk that no
    session names) and `s.walk_capped` (that walk ran out of budget: the diagnostic `listing_capped`)."""
    cat = _catalog(s)
    launcher = os.path.normpath(getattr(s, 'cwd', '') or '') if getattr(s, 'cwd', '') else ''
    tops = {}

    def tops_of(cwd):
        if cwd not in tops:
            top = U.repo_top(cwd) if cwd else None
            tops[cwd] = (top,) if top else ()
        return tops[cwd]

    names = peer_names(s)
    facts = [facts_of(a, launcher, names) for a in s.agents.values()]
    jd = U.assign(facts, cat, statuses, extra_units=getattr(s, 'walked_units', ()) or (), tops_of=tops_of)
    if getattr(s, 'walk_capped', False):                                  # the background walk ran out of budget: the list may be incomplete
        jd.diag.append({'code': 'listing_capped', 'agent': None, 'unit': None, 'detail': None})
    # A shared guide that groups topics (a brief.md with a table, and topic folders below it) is a root, not a topic of its own, unless an agent holds a seat in it.
    # Whatever the guide says about rounds, a folder that has no round folder, no seat and debate folders below it is such a root (a common guide that describes the
    # round layout of its topics still holds none itself). Grouping roots: if both the unit and its parent have brief.md and the parent itself has no round folder,
    # the parent is the root (review/t1_naming → review). If r1 is directly below, that folder itself is the root.
    seated_units = {u for us in jd.agent_units.values() for u in us}
    groups = {p for p in jd.listed if not jd.units[p].rounds and p not in seated_units and p not in jd.rooms and cat.children(p)}
    roots = collections.defaultdict(set)
    for unit in jd.listed:
        u = jd.units[unit]
        parent = os.path.dirname(unit)
        pu = cat.unit_at(parent) if u.brief and (unit in jd.rooms or os.path.basename(u.brief) == 'brief.md') and os.path.exists(os.path.join(parent, 'brief.md')) else None
        if pu is not None and not pu.rounds:
            roots[parent].add(unit)
        elif unit in groups:
            roots[unit]
        else:
            roots[unit].add(unit)
    # One folder is one debate: the copies of a folder (a link into it, the same place in another worktree of the repository) are folded into the one this session's agents work in
    # (copies.fold). What was in a folded copy is known by the folder that stands for it: the agents tied to it, the writes and reads, the time of its last start.
    all_roots = list(roots)
    kept, folded = C.fold(all_roots, lambda group: _evidence(s, jd, group)) if len(all_roots) > 1 else (all_roots, {})
    at = _mapper({copy: rep for rep, copies in folded.items() for copy in copies})
    for root in all_roots:
        if root not in kept:
            del roots[root]
    jd.members = {aid: {at(u) for u in us} for aid, us in jd.members.items()}
    jd.worked = {aid: {at(u) for u in us} for aid, us in jd.worked.items()}
    # for each agent, the latest round it holds within a unit (the round it is writing now)
    cur_round = {}
    for a in jd.assignments:
        if a.round is not None:
            cur_round[(a.agent, a.unit)] = max(cur_round.get((a.agent, a.unit), 0), a.round)
    # What was done to a file in a folded copy is known by the same file of the folder that stands for it only when the two are one report (the same file, or the same text): a copy
    # of a folder is not a copy of what was read or written in it, and another text under the same name is another report.
    same = {}

    def at_file(path):
        rep = at(path)
        if rep == path:
            return path
        if (path, rep) not in same:
            same[(path, rep)] = C.same_file(path, rep)
        return rep if same[(path, rep)] else path
    writer_of = writer_table(s, at_file)
    readers_of = collections.defaultdict(list)       # path -> [(id of the reading agent, name)]. Built once so that every cell does not sweep all the agents
    for a in s.agents.values():
        for path in a.reads:
            readers_of[at_file(path)].append((a.id, a.name_tag or a.id[:6]))
    unit_info = {}
    for p, ts in jd.unit_ts.items():
        q = unit_info.setdefault(at(p), {'refs_ts': 0})
        q['refs_ts'] = max(q['refs_ts'], ts)
    out = []
    for root, us in roots.items():
        if len(us) > 1 or root not in us:
            for child in cat.children(root):
                us.add(child.path)
        if not us:
            continue
        d = _debate(s, root, sorted(us), jd.slots, unit_info, statuses, writer_of, readers_of, cur_round, jd.folders, jd.rooms, jd.room_files)
        d['copies'] = len(folded.get(root, ()))
        out.append(d)
    # A folder that shows nothing (an instruction and its notes: no seat, no cell, nobody declared, no final) inside the folder of a debate that does is a part of that debate's
    # records, not a debate of its own; a folder like that with no debate around it is still listed, as a title.
    shown = {d['root'] for d in out if any(t['rows'] or t['final']['exists'] for t in d['topics'])}
    out = [d for d in out if d['root'] in shown or not any(at(above) in shown for above in _above(d['root']))]
    out.sort(key=lambda d: -d['last_ts'])
    for i, d in enumerate(out):
        d['current'] = i == 0
    agent_units = collections.defaultdict(set)
    for aid, us in jd.worked.items():
        agent_units[aid] |= us
    for aid, us in jd.agent_units.items():
        agent_units[aid] |= us
    return Judged(out, agent_units, jd.members, jd.assignments, jd.diag)


def debates(s, statuses):
    """Builds the debate structure from the debate folders on disk and what the agents say about them: (list of debates, {agent id: {unit}} the debates each agent works in)."""
    r = judge(s, statuses)
    try:
        s.debate_diag = r.diag                        # for the integrator's diagnostics (board/diag.py)
    except AttributeError:
        pass
    return r.debates, r.agent_units


def _room_skip(room, room_files, unit):
    """The file stems directly in a room's folder that are no conclusion: its guide and the files of its participants (auto_final looks at the folder for a final)."""
    return {os.path.splitext(os.path.basename(p))[0] for p in [room.guide] + [path for (u, _seat), path in room_files.items() if u == unit] if os.path.dirname(p) == unit}


def _debate(s, root, unit_list, slots, units, statuses, writer_of, readers_of, cur_round, folders=None, rooms=None, room_files=None):
    cat = _catalog(s)
    rooms, room_files = rooms or {}, room_files or {}
    root_brief = read_head(s, rooms[root].guide if root in rooms else os.path.join(root, 'brief.md'))
    title = (re.search(r'^#\s+(.+)$', root_brief, re.M) or [None, os.path.basename(root)])[1]
    table = {} if root in rooms else brief_table(root_brief)
    topics, open_ended = [], []                     # open_ended: the topics that may be closed by a document above them, with what is needed to look for one
    last_ts = max([units[u]['refs_ts'] for u in unit_list if u in units] or [0])
    for unit in unit_list:
        un = cat.unit_at(unit)
        tb = read_head(s, un.brief if un and un.brief else os.path.join(unit, 'brief.md'))
        ttitle = (re.search(r'^#\s+(.+)$', tb, re.M) or [None, ''])[1]
        ttitle = re.sub(r'\s*[—-]\s*주제 지침\s*$', '', ttitle)
        roles = {}
        for m in U.ROLE_RE.finditer(tb):
            roles.setdefault(m.group(1), m.group(2).strip())
        row = table.get(os.path.basename(unit), {})
        room = rooms.get(unit)
        flat = un is not None and un.kind == 'flat'
        rounds, stems = set(), set()
        for (u, rnd, stem) in slots:
            if u == unit:
                if rnd is not None:
                    rounds.add(rnd)
                stems.add(stem)
        if not flat:
            for n, dirs in (un.rounds if un else {}).items():
                files = [f for d in dirs for f in cat.md_stems(os.path.join(unit, d))]       # every physical folder of the round: r01 and r1 are two sets of files
                if files or n == 1:
                    rounds.add(n)
                stems.update(files)
        # A role "**A — ...**" in the brief is one row. If the files call it A_flow the row keeps that file name and the role follows the
        # leading letter. (When A.md and A_flow.md both exist, or two X_ names share a letter, nothing is merged: separate rows.)
        alias = stem_aliases(stems)
        by_stem = {stem: letter for letter, stem in alias.items()}
        if flat:
            parts = stems | set(un.declared_reports)
            alias, by_stem, roles = {}, {}, {}
            rounds = {None}
        elif room is not None and room.kind == 'members':
            parts, rounds = set(), set()                  # participants only: a row each, no cell and no round
        else:
            parts = stems | {letter for letter in roles if letter not in alias}
            if not rounds and parts:                  # participants are declared and nothing has started: round 1 waits. With nobody declared there is no cell to place
                rounds = {1}
        rows = []
        for p in sorted(parts):
            cells = []
            row_agents = []
            for rnd in sorted(rounds, key=lambda x: (x is None, x or 0)):
                assigned = slots.get((unit, rnd, p), [])
                # cell owner: the agent started latest (by start time, the last by id if equal). Does not depend on the order found
                agent_id = max(assigned, key=lambda x: (s.agents[x].spawn_ts or s.agents[x].first_ts or 0, x)) if assigned else None
                if rnd is None:
                    path = os.path.join(unit, p + '.md')
                elif (unit, p) in room_files:            # a room's seat has the file its participant has, wherever it is in the room's folder
                    path = room_files[(unit, p)]
                else:      # the file of the seat: the folder its owner holds when the round has several (r01 next to r1), else the one that has the file, else the first
                    folder = (folders or {}).get((unit, rnd, p), {}).get(agent_id) or round_dir_name(un, rnd, holds=lambda d, _p=p: _p in cat.md_stems(os.path.join(unit, d)))
                    path = os.path.join(unit, folder, p + '.md')
                info = file_info(s, path)
                if agent_id and agent_id not in row_agents:
                    row_agents.append(agent_id)
                writer = writer_of.get(path)
                here = rnd is None or cur_round.get((agent_id, unit)) == rnd
                state = cell_state(statuses.get(agent_id), bool(info and info['size'] > 0), bool(agent_id) and here, bool(agent_id))
                readers = sorted({name for aid, name in readers_of.get(path, ()) if aid != (writer or agent_id)})
                ag = s.agents.get(agent_id)
                planned = bool(ag and ag.provider == 'codex' and not (info and info['size'] > 0) and
                               any(o['path'] == path for o in ag.out_paths))
                cells.append({'round': rnd, 'state': state, 'path': path, 'agent': agent_id,
                              'writer': writer, 'readers': readers,
                              'lines': info['lines'] if info else 0, 'mtime': info['mtime'] if info else None,
                              'planned': planned})
            rows.append({'p': p, 'role': roles.get(p) or roles.get(by_stem.get(p, ''), ''), 'agents': row_agents, 'cells': cells})
        if room is not None and room.kind == 'members':
            for aid in room.members:
                a = s.agents.get(aid)
                rows.append({'p': (a.name_tag or aid[:6]) if a else aid[:6], 'role': (a.title if a else '') or '', 'agents': [aid], 'cells': []})
        final, auto = row.get('final'), False
        final_path = os.path.join(root, final) if final else None
        finfo = file_info(s, final_path) if final_path else None
        table_final = bool(final)
        if not final:                                  # if it is not in the table, look in the topic folder (so the table need not be fitted per project)
            final, finfo = auto_final(s, unit, rows, skip=parts if flat else _room_skip(room, room_files, unit) if room else ())
            final_path, auto = (os.path.join(unit, final), True) if final else (None, False)
        docs, docs_last = [], 0.0
        if os.path.isdir(unit):
            guide = room.guide if room else None
            for f in sorted(os.listdir(unit)):
                plain = stat_plain(os.path.join(unit, f)) if f.endswith('.md') else None
                if plain:                              # a document that cannot be opened is not listed
                    docs.append({'name': f, 'path': os.path.join(unit, f)})
                    if f not in U.GUIDE_NAMES and os.path.join(unit, f) != guide:
                        docs_last = max(docs_last, plain[1].st_mtime)          # what is written there counts as the topic's activity, an instruction does not
        topic = {'dir': unit, 'key': os.path.basename(unit), 'title': ttitle or row.get('name') or
                 os.path.basename(unit), 'name': row.get('name', ''), 'deps': row.get('deps', ''),
                 'kind': 'flat' if flat else 'rounds',
                 **({'room': room.kind, 'guide': room.guide} if room else {}),
                 'final': {'path': final_path, 'rel': final, 'exists': bool(finfo), 'auto': auto,
                           'mtime': finfo['mtime'] if finfo else None,
                           'lines': finfo['lines'] if finfo else 0},
                 'rounds': [] if flat else sorted(rounds), 'rows': rows, 'docs': docs,
                 'brief': os.path.exists(os.path.join(unit, 'brief.md')) or room is not None}
        topics.append(topic)
        open_ended.append((topic, rows, docs_last, table_final))
    bundle = max([max([active] + [c['mtime'] or 0 for r in rows for c in r['cells'] if c['state'] == 'done']) for _t, rows, active, _f in open_ended] or [0])
    bundle_topics = [t['dir'] for t, _r, _a, _f in open_ended if t['dir'] != root]
    for topic, rows, active, table_final in open_ended:
        if table_final or topic['final']['exists'] or topic['dir'] == root:
            continue
        rel, info = closing_above(s, topic['dir'], root, rows, active, bundle, bundle_topics)      # a conclusion document of the bundle above the topic closes it
        if rel:
            topic['final'] = {'path': os.path.normpath(os.path.join(topic['dir'], rel)), 'rel': rel, 'exists': True, 'auto': True, 'mtime': info['mtime'], 'lines': info['lines']}
    finals = []
    fdir = os.path.join(root, 'final')
    if os.path.isdir(fdir):
        for f in sorted(os.listdir(fdir)):
            info = file_info(s, os.path.join(fdir, f), strict=True)
            if info:
                finals.append({'name': f, 'path': os.path.join(fdir, f), 'mtime': info['mtime'],
                               'lines': info['lines']})
    return {'root': root, 'short': short_path(root), 'name': os.path.basename(root), 'title': title,
            'topics': topics, 'finals': finals, 'last_ts': last_ts}
