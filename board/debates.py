"""Reading debates (topic × round × participant): the cell structure and its state, built from the debate folders on disk (units.py) and from what the agents did (the write, read and command events
the collectors keep). The judgments are separate and read no sentence: the list of debate folders and who holds which cell and when a debate is over (units.assign), and what a cell shows
(units.cell_state). This module turns the session into the facts they read (`facts_of`, `session_facts`) and the Judgement into the dicts the board shows (`judge`)."""

import collections
import contextlib
import os
import re

from . import units as U
from .units import REPORT_RE, AgentFacts, Catalog, SessionFacts  # noqa: F401
from .util import FILE_MAX, Denied, open_safe, short_path, stat_plain, stat_regular

# the rule for putting the candidates of a final in order lives with the judgment (units.final_sort_key); kept under its old name for the callers that sort by it
CONCLUDE_RE = U.CONCLUDE_RE
SAME_TIME = U.SAME_TIME


def agent_cwd(a):
    """Absolute working folder of an agent: its own records (Claude subagent, claude -p child), Codex index entry, else the link's cwd."""
    c = getattr(a, 'cwd', '') or ((a.cli or {}).get('cwd') if getattr(a, 'cli', None) else '') or ''
    return os.path.normpath(c) if c and os.path.isabs(c) else ''


def real_unit(unit):
    """Whether this is a real debate folder: it has an exact round folder (r1, r01, round1). The folder is looked at on disk; a name alone settles nothing."""
    cat = Catalog()
    cat.begin()
    return cat.unit_at(os.path.normpath(unit)) is not None


HEAD_CHARS = 200 * 200   # number of leading characters read from a debate folder's brief.md


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
    so the cell stands even for a dot path or secret name (strict=False). Ones found by searching a folder (the candidates of a final, the finals list) use strict=True: a dot path or secret name is a missing file too.
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
    """{name: agent id} for the names a message to another agent can use: its id and nothing else. A description, the letter at its head and the model name are what the page makes of
    a record, not names that anybody gave (they change with the language of a sentence), so a message to one of them went to nobody that is known."""
    return {a.id.lower(): a.id for a in s.agents.values() if a.id}


def facts_of(a, launcher_cwd='', names=None, status=None):
    """The AgentFacts of an agent: the events the collectors kept of it and nothing it said (units.AgentFacts has no sentence). The lists are taken as tuples under the lock of its event log,
    since the collector changes them in place while the judgment reads. `names` (peer_names) tells who the messages it sent (`a.sent`) went to: the agents of the same session, never itself. A message whose
    result is known to be an error (`ok` False) went to nobody; one whose result is not known (`ok` None, or absent in an older record) counts as sent."""
    ev = getattr(a, 'ev', None)
    with (ev.lock if ev is not None else contextlib.nullcontext()):
        writes, reads = tuple(getattr(a, 'write_events', ())), tuple(getattr(a, 'read_events', ()))
        windows, planned = tuple(getattr(a, 'windows', ())), tuple(getattr(a, 'planned', ()))
        windows_dropped, writes_dropped, lost = getattr(a, 'windows_dropped', None), getattr(a, 'writes_dropped', None), getattr(a, 'lost', None)
    sent_to = sorted({names[m['to'].lower()] for m in a.sent if m.get('ok') is not False and names.get(m['to'].lower()) not in (None, a.id)}) if names else []
    return AgentFacts(id=a.id, provider=a.provider, origin=a.origin, cwd=agent_cwd(a), launcher_cwd=launcher_cwd, start=a.spawn_ts or a.first_ts or 0, first=a.first_ts or 0, last=a.last_ts or 0,
                      status=status, launch=getattr(a, 'launch', None), tag=getattr(a, 'room_tag', None), writes=writes, reads=reads, planned=planned, windows=windows,
                      windows_dropped=windows_dropped, writes_dropped=writes_dropped, lost=lost, run=getattr(a, 'run', None), run_start=getattr(a, 'run_start', 0.0) or 0.0, sent_to=tuple(sent_to))


def session_facts(s, statuses, fresh=False):
    """The SessionFacts of a session: its agents' facts (with the state the page gives each), the orchestrator's own events and command windows, the folders it wrote a guide or a round in (the hints),
    the folders a walk found. No clock is read and nothing of the disk. What is made of more than one record (the run, the call that launched an agent, its room tag, the output a `claude -p` child was told
    to write) is brought up to date first, unless the caller says (`fresh`) that it has done that for this request."""
    statuses = statuses or {}
    refresh = getattr(s, 'refresh_facts', None)
    if refresh is not None and not fresh:                     # (a stand-in for a session in a test has none)
        refresh(statuses)
    with (getattr(s, 'lock', None) or contextlib.nullcontext()):
        launcher = os.path.normpath(getattr(s, 'cwd', '') or '') if getattr(s, 'cwd', '') else ''
        names = peer_names(s)
        agents = [facts_of(a, launcher, names, statuses.get(a.id)) for a in s.agents.values()]
        log = getattr(s, 'orch_log', None)
        with (log.lock if log is not None else contextlib.nullcontext()):
            orch_writes, orch_windows = tuple(getattr(s, 'orch_events', ())), tuple(getattr(s, 'orch_windows', ()))
            orch_dropped, orch_lost = getattr(s, 'orch_windows_dropped', None), getattr(s, 'orch_lost', None)
        return SessionFacts(agents=agents, orch_writes=orch_writes, orch_windows=orch_windows, orch_windows_dropped=orch_dropped, orch_lost=orch_lost,
                            hints=dict(getattr(s, 'orch_hint_facts', None) or {}), walked=tuple(getattr(s, 'walked_units', ()) or ()), walk_capped=bool(getattr(s, 'walk_capped', False)),
                            hints_dropped=bool(getattr(s, 'orch_hints_dropped', False)), launcher_cwd=launcher)


def _catalog(s):
    """The disk catalog of a session: kept on it, so that what the judgment looked at last time is there to look at again."""
    cat = getattr(s, '_unit_catalog', None)
    if cat is None:
        cat = Catalog(text_of=lambda p: read_head(s, p))
        try:
            s._unit_catalog = cat
        except AttributeError:
            pass
    return cat


def _above(path):
    """The folders above a path, nearest first."""
    out, d = [], os.path.dirname(path)
    while d and d != os.path.dirname(d):
        out.append(d)
        d = os.path.dirname(d)
    return out


def _launch_text(key):
    """The launch key of an agent as the page gets it: an opaque string, the same for the agents that were launched together."""
    return '%s:%s:%s:%s' % (key.provider, (key.tree or '')[:8], (key.node or '-')[:8], key.group)


Judged = collections.namedtuple('Judged', 'debates agent_units members assignments diag placed launch cells finals reads')


def judge(s, statuses, fresh=False):
    """The debate structure, with everything the judgments found: Judged(debates, agent_units, members, assignments, diag, placed, launch, cells, finals, reads).
    debates      the list the board shows (see `_debate`)
    agent_units  {agent id: {unit}} the debates where an agent owns a cell
    members      {agent id: {unit}} the debates an agent is tied to: it owns, edited or was asked to save a cell there, has its room tag, or is thought to work there
    assignments  facts.Assignment list (the cells with their owners, the evidence and the period each was held)
    diag         {code, agent, unit, detail} dicts (alias_collision, seat_tie_held, launch_split, history_lost, listing_capped)
    placed       {agent id: {unit, topic, why, whys, sure}} where an agent that owns no cell is thought to work; launch {agent id: opaque text} the call it was launched by (the same text: launched together)
    cells, finals  the units.Cell and units.Final of the Judgement
    reads        what the judgment looked at on the disk with the answers (Catalog.reads): the judgment is good while Catalog.changed(reads) says no
    `fresh`: the caller has brought the facts up to date (`refresh_facts`) for this request already.
    Extra inputs the integrator may set on the session: `s.walked_units` (the paths units.walk_repo found in the background stage: debates on disk that no
    session names) and `s.walk_capped` (that walk ran out of budget: the diagnostic `listing_capped`)."""
    cat = _catalog(s)
    statuses = statuses or {}
    tops = {}

    def tops_of(cwd):
        if cwd not in tops:
            top = U.repo_top(cwd, cat=cat) if cwd else None
            tops[cwd] = (top,) if top else ()
        return tops[cwd]

    sf = session_facts(s, statuses, fresh)
    jd = U.assign(sf, cat, tops_of)
    diag = list(jd.diag)
    if sf.walk_capped:                                                    # the background walk ran out of budget: the list may be incomplete
        diag.append({'code': 'listing_capped', 'agent': None, 'unit': None, 'detail': None})
    if sf.hints_dropped:                                                  # the orchestrator wrote into more folders than the page keeps: the oldest were let go
        diag.append({'code': 'listing_capped', 'agent': None, 'unit': None, 'detail': 'writes'})
    cells_of = collections.defaultdict(list)
    for c in jd.cells.values():
        cells_of[c.unit].append(c)
    cell_names = collections.defaultdict(list)
    for c in jd.cells.values():
        cell_names[os.path.basename(c.path)].append(c.path)
    readers_of = collections.defaultdict(list)       # cell path -> [(id of the reading agent, name)]. Built once so that every cell does not sweep all the agents. A read of the file is one of the cell when it leads to the same real file
    for a in s.agents.values():
        for path in a.reads:
            for q in cell_names.get(os.path.basename(path), ()):
                if q == path or cat.real_file(path) == q:
                    readers_of[q].append((a.id, a.name_tag or a.id[:6]))
    out = []
    for root in jd.order:
        d = _debate(s, root, jd.roots[root], jd, cells_of, readers_of, statuses)
        d['copies'] = len(jd.folded.get(root, ()))
        d['current'] = False
        out.append(d)
    # A folder that shows nothing (an instruction and its notes: no cell, no final) inside the folder of a debate that does is a part of that debate's records, not a debate of its own; a folder
    # like that with no debate around it is still listed, as a title.
    shown = {d['root'] for d in out if any(t['rows'] or t['final']['exists'] for t in d['topics'])}
    out = [d for d in out if d['root'] in shown or not any(above in shown for above in _above(d['root']))]
    first = next((d['root'] for d in out if not (d['root'] in jd.rooms and not jd.rooms[d['root']].sure)), None)         # the first of what the page lists, an unsure room never
    for d in out:
        d['current'] = d['root'] == first
    placed = {aid: {'unit': p.unit, 'topic': p.topic, 'why': p.why, 'whys': list(p.whys), 'sure': p.sure} for aid, p in jd.placed.items()}
    launch = {a.id: _launch_text(a.launch) for a in sf.agents if a.launch is not None}
    return Judged(out, jd.agent_units, jd.worked, jd.assignments, diag, placed, launch, jd.cells, jd.finals, cat.reads())


def debates(s, statuses):
    """Builds the debate structure from the debate folders on disk and what the agents did: (list of debates, {agent id: {unit}} the debates each agent owns a cell in)."""
    r = judge(s, statuses)
    try:
        s.debate_diag = r.diag                        # for the integrator's diagnostics (board/diag.py)
    except AttributeError:
        pass
    return r.debates, r.agent_units


def _info(s, cat, path, strict=False):
    """`file_info` of a file the page shows, after the Catalog has looked at it (one stat, the one the judgment made if it made it): what the page shows of a file (its lines, its time) is made again
    when that stat changes, with the judgment (J19), and not by a look of its own at every poll."""
    cat.stat(path)
    return file_info(s, path, strict)


def _head(s, cat, path):
    """`read_head` of a brief, after the Catalog has looked at the file (see `_info`)."""
    cat.stat(path)
    return read_head(s, path)


def _final_dict(s, f, rel_to, table_path=None):
    """The final of a topic, a room or a bundle as the page gets it (J15): whether it is confirmed, the document, who wrote it, why it is not, and the candidates."""
    cat = _catalog(s)
    info = _info(s, cat, f.path, strict=True) if f.path else None
    cands = []
    for p in f.candidates:
        i = _info(s, cat, p, strict=True)
        if i:
            cands.append({'path': p, 'rel': os.path.relpath(p, rel_to), 'mtime': i['mtime'], 'lines': i['lines']})
    ok = bool(f.confirmed and info)
    why = list(f.why) if not (f.confirmed and not info) else ['none']      # a document the page may not open (a dot folder, a secret name) is none for the page
    return {'confirmed': ok, 'exists': ok, 'path': f.path if ok else None, 'rel': os.path.relpath(f.path, rel_to) if ok else None, 'by': f.by if ok else None,
            'mtime': info['mtime'] if ok else None, 'lines': info['lines'] if ok else 0, 'why': why, 'candidates': cands, 'scope': f.scope, 'table_path': table_path}


def _placed_rows(jd, items, statuses):
    return [{'agent': p.agent, 'why': p.why, 'whys': list(p.whys), 'sure': p.sure, 'live': (statuses.get(p.agent) or 'unknown') in U.OPEN} for p in items]


def _debate(s, root, unit_list, jd, cells_of, readers_of, statuses):
    cat = _catalog(s)
    rooms = jd.rooms
    root_room = rooms.get(root)
    root_brief = _head(s, cat, (root_room.guide or '') if root_room else os.path.join(root, 'brief.md'))
    title = (re.search(r'^#\s+(.+)$', root_brief, re.M) or [None, os.path.basename(root)])[1]
    table = {} if root in rooms else brief_table(root_brief)
    topics = []
    last_ts = jd.unit_ts.get(root, 0)
    rootfinal = jd.finals.get(root) if root not in unit_list and root not in rooms else None
    root_final = _final_dict(s, rootfinal, root) if rootfinal else None
    cell_keys = {(c.unit, c.path): key for key, c in jd.cells.items()}
    for unit in unit_list:
        un = cat.unit_at(unit)
        tb = _head(s, cat, un.brief if un and un.brief else ((rooms[unit].guide or '') if unit in rooms else os.path.join(unit, 'brief.md')))
        ttitle = (re.search(r'^#\s+(.+)$', tb, re.M) or [None, ''])[1]
        ttitle = re.sub(r'\s*[—-]\s*주제 지침\s*$', '', ttitle)
        row = table.get(os.path.basename(unit), {})
        room = rooms.get(unit)
        by_round = collections.defaultdict(list)
        for c in cells_of.get(unit, ()):
            by_round[(1 if room is not None else c.round, c.stem)].append(c)
        rounds, stems = set(), set()
        for c in cells_of.get(unit, ()):
            rounds.add(1 if room is not None else c.round)
            stems.add(c.stem)
        for n, dirs in (un.rounds if un else {}).items():
            files = [f for d in dirs for f in cat.md_stems(os.path.join(unit, d))]       # every physical folder of the round: r01 and r1 are two sets of files
            if files or n == 1:
                rounds.add(n)
            stems.update(files)
        parts = stems
        if room is not None and room.kind == 'members':
            parts, rounds = set(), set()                  # participants only: a row each, no cell and no round
        rows = []
        for p in sorted(parts):
            cells = []
            row_agents = []
            for rnd in sorted(rounds, key=lambda x: (x is None, x or 0)):
                dirs = (un.rounds.get(rnd) or []) if un else []
                here = sorted(by_round.get((rnd, p), ()), key=lambda x: (x.rdir, x.path))     # a round with two spellings of its folder has two: the cell somebody holds, else the first
                c = next((x for x in here if x.owner or x.agent), here[0] if here else None)
                if c is not None:
                    path = c.path
                else:                                        # nobody holds this seat in this round and the file is not there: the seat waits
                    if un and un.file_rounds:
                        files = un.file_rounds.get(rnd) or []
                        prefix = files[0].split('_', 1)[0] if files else 'round%d' % rnd
                        path = os.path.join(unit, prefix + '_' + p + '.md')
                    else:
                        path = os.path.join(unit, dirs[0] if dirs else 'r%d' % rnd, p + '.md')
                info = _info(s, cat, path)
                agent_id = c.agent if c else None
                if agent_id and agent_id not in row_agents:
                    row_agents.append(agent_id)
                readers = sorted({name for aid, name in readers_of.get(path, ()) if aid != ((c.owner or c.agent) if c else None)})
                state = c.state if c else ('previous' if info and info['size'] > 0 else 'waiting')
                previous = bool(c.previous) if c else bool(info and info['size'] > 0)
                if info is None and state == 'previous':            # a file the page may not show (a link to a credential file, a secret name) is no file to show
                    state, previous = 'waiting', False
                cells.append({'round': rnd, 'state': state, 'path': path, 'agent': agent_id, 'owner': c.owner if c else None, 'editors': list(c.editors) if c else [],
                              'reports': [{'key': list(cell_keys[(x.unit, x.path)]), 'path': x.path, 'owner': x.owner, 'agent': x.agent,
                                           'editors': list(x.editors), 'evidence': x.evidence, 'state': x.state, 'previous': x.previous, 'hint': x.hint} for x in here],
                              'evidence': c.evidence if c else None, 'hint': c.hint if c else None, 'previous': previous,
                              'rdir': c.rdir if c else (dirs[0] if dirs else 'r%d' % rnd), 'readers': readers,
                              'lines': info['lines'] if info else 0, 'mtime': info['mtime'] if info else None,
                              'planned': bool(c and c.evidence == 'planned' and c.owner is None)})
            rows.append({'p': p, 'role': '', 'agents': row_agents, 'cells': cells})
        if room is not None and room.kind == 'members':
            for aid in room.members:
                a = s.agents.get(aid)
                rows.append({'p': (a.name_tag or aid[:6]) if a else aid[:6], 'role': '', 'agents': [aid], 'cells': []})
        docs = []
        for f, _kind in cat.listdir(unit) or ():
            if f.lower().endswith('.md') and cat.stat(os.path.join(unit, f)) is not None and stat_plain(os.path.join(unit, f)):          # a document that cannot be opened is not listed
                docs.append({'name': f, 'path': os.path.join(unit, f)})
        final = jd.finals.get(unit)
        table_final = os.path.join(root, row['final']) if row.get('final') else None
        final_d = _final_dict(s, final, unit, table_final) if final else {'confirmed': False, 'exists': False, 'path': None, 'rel': None, 'by': None, 'mtime': None, 'lines': 0,
                                                                         'why': [], 'candidates': [], 'scope': 'topic', 'table_path': table_final}
        topic = {'dir': unit, 'key': os.path.basename(unit), 'title': ttitle or row.get('name') or os.path.basename(unit), 'name': row.get('name', ''), 'deps': row.get('deps', ''),
                 'kind': 'rounds',
                 **({'room': room.kind, 'guide': room.guide, 'room_sure': room.sure, 'room_why': room.why} if room else {}),
                 'final': final_d,
                 'rounds': sorted(rounds), 'rows': rows, 'docs': docs,
                 'closable': bool(jd.closable.get(unit) and (final_d['confirmed'] or (root_final is not None and root_final['confirmed']))),          # what the page cannot show as a final does not close it
                 'placed': _placed_rows(jd, [p for p in jd.placed.values() if p.unit == root and (p.topic == unit or (p.topic is None and (root == unit or len([u for u in unit_list if u != root]) == 1)))], statuses),
                 'brief': cat.stat(os.path.join(unit, 'brief.md')) is not None or room is not None}
        topics.append(topic)
    finals = []
    fdir = os.path.join(root, 'final')
    if cat.is_dir(fdir):
        for f, _kind in cat.listdir(fdir) or ():
            info = _info(s, cat, os.path.join(fdir, f), strict=True)
            if info:
                finals.append({'name': f, 'path': os.path.join(fdir, f), 'mtime': info['mtime'],
                               'lines': info['lines']})
    multi = len([u for u in unit_list if u != root]) > 1
    return {'root': root, 'short': short_path(root), 'name': os.path.basename(root), 'title': title,
            'topics': topics, 'finals': finals, 'last_ts': last_ts, 'sure': not (root_room is not None and not root_room.sure),
            'final': root_final,
            'placed': _placed_rows(jd, [p for p in jd.placed.values() if p.unit == root and p.topic is None and multi], statuses)}
