"""The truth of the 0.3.0 contract: what a correct judgment of the records says about the debate folders, their cells, the participants that stand beside them, the rooms and
the finals, computed from structure alone (events, reads, launch keys, tags, files on disk). It never imports board/ and it never reads a word of any text.

    truth(case)       -> {listed, hinted, current, order, units, cells, paths, placed, rooms, finals, closable, diag}   one judgment of one input (`units`: the topics and rooms the list
                         shows; `paths`: the file of each cell)
    steps(case)       -> [(k, input, expect)]  the input of the case and of every `then` step (each step adds to the one before), with the keys that step compares
    compare(t, want)  -> [(key, got, wanted)]  the keys of `want` that `t` does not reproduce

A case is the JSON of tests/data/contract_cases.json (CONTRACT 3.1; paths relative to a synthetic root). That file is the reference copy of the table: a case is added there. The rules are the lines J1-J20 of the contract, in the order they are
named in the comments. Where the contract leaves a choice, the choice is the one that misses (no false cell, no false place, no false final); each is marked `choice:`.
"""

import collections
import copy
import math
import re

CONSTS = {'TIE_SEC': 1.0, 'SAME_TIME': 2.0, 'W4_GRACE': 2.0, 'WIN_EPS': 0.05}     # CONTRACT head; test_scenarios checks them against `consts` of the table
OPEN = ('running', 'stalled', 'unknown')
AUTHORING = ('create', 'replace', 'unknown')
SHOWN = ('tool', 'shell')                                    # the evidence of an event that says a file was made (not a launch command's output)
GUIDE_NAMES = ('brief.md', 'README.md', 'index.md')
COMMON_FOLDERS = ('docs', 'doc')
STATE_DIRS = ('.claude/', '.codex/')                         # what the tools keep of themselves (units.py STATE_DIRS, below the synthetic root)
SCRATCH_DIRS = ('tmp/', 'var/tmp/', 'private/tmp/', 'var/folders/', 'private/var/folders/')
ROUND_DIR_RE = re.compile(r'(?:r|round)(\d+)\Z')
ROUND_FILE_RE = re.compile(r'(?:r|round)(\d+)_([^/]+)\.md\Z', re.I)
ROUND_DOC_RE = re.compile(r'^(brief|round\d+.*|r\d+[_-].*)\.md$', re.I)
CONCLUDE_RE = re.compile(r'ruling|final|verdict|decision|conclusion|summary|plan|(?<![a-z])clos(?:e|ing|ure)|결론|판정|합의|최종|정리|종결|마무리', re.I)
WHY_ORDER = ('no_report', 'open_cell', 'none', 'several', 'empty_round', 'live_participant', 'estimated_room', 'history_lost')      # FINAL_WHY (J15)
REASON_ORDER = ('tag', 'launch_call', 'launch_peer', 'guide_read')                                                                  # the strongest first (J11)
NOT_SUBMITTED = ('writing', 'draft', 'paused', 'missing', 'waiting')


def parent(p):
    return p.rsplit('/', 1)[0] if '/' in p else ''


def name(p):
    return p.rsplit('/', 1)[-1]


def cell_identity(unit, stem, path, conflicting):
    """NUL separates a conflicting physical path from every valid normal seat name."""
    return '\0' + path[len(unit) + 1:] if conflicting else stem


def under(p, d):
    return p == d or p.startswith(d + '/')


def in_reach(p, folder):
    """A file of a room: in its folder or one folder below it."""
    return parent(p) == folder or parent(parent(p)) == folder


def cell_state(status, file_ok, working_here, has_agent):             # units.py cell_state, unchanged by the contract
    if working_here and status in OPEN:
        return 'draft' if file_ok else 'writing'
    if working_here and status == 'interrupted':
        return 'draft' if file_ok else 'paused'
    if file_ok:
        return 'done'
    return 'missing' if has_agent else 'waiting'


def over_before(g, f):                                                  # units.py _over_before: g is over when f begins
    return g['status'] is not None and g['status'] not in OPEN and (g['last'] or g['start'] or 0) <= (f['start'] or 0)


def gkey(launch):
    """A launch key's group (J11): the same tuple means launched together. A launch with no key is no group."""
    return (launch['provider'], launch['tree'], launch['node'], launch['group']) if launch else None


def hint_groups(hint):
    """The groups of an orchestrator hint as gkey tuples: a plain string is a group of the main claude session `S` (3.1), a list is the tuple itself."""
    return {tuple(g) if isinstance(g, (list, tuple)) else ('claude', 'S', None, g) for g in hint['groups']}


class Judge:
    def __init__(self, case, consts=None):
        k = dict(CONSTS)
        k.update(consts or {})
        self.TIE, self.SAME, self.GRACE, self.EPS = k['TIE_SEC'], k['SAME_TIME'], k['W4_GRACE'], k['WIN_EPS']
        self.c = case
        self.agents = [dict(a, status=a['status'] or 'unknown') for a in case['agents']]       # L4 (O14): a state nobody gave is the state of one that may still be working
        self.ids = {a['id']: a for a in self.agents}
        self.links = case['disk'].get('links', {})
        self.files = {self.real(p): m for p, m in case['disk']['files'].items()}
        self.dirs = {self.real(d) for d in case['disk']['dirs']}
        self.orch = case['orch']
        self.walked = [self.real(p) for p in case.get('walked', [])]
        self.events = self._events()
        self.conf = [w for w in self.events if w['confirmed']]
        self.topics = sorted({parent(d) for d in self.dirs if ROUND_DIR_RE.fullmatch(name(d))})
        self.round_dirs_of = {u: sorted(d for d in self.dirs if parent(d) == u and ROUND_DIR_RE.fullmatch(name(d))) for u in self.topics}
        self.file_topics = set()
        # O23: two authors of different seats, with two seats in one round; existing topics and bundles win.
        authors = [w for w in self.conf if w['agent'] != 'orch' and w['kind'] in AUTHORING and w['evidence'] in SHOWN and ROUND_FILE_RE.fullmatch(name(w['path']))]
        folders = {parent(w['path']) for w in authors}
        scratch = {d.rstrip('/') for d in SCRATCH_DIRS}
        scratch |= {self.real(d) for d in scratch}
        for f in sorted(folders, key=lambda f: (-f.count('/'), f)):
            children = any(parent(u) == f and name(u) != 'final' for u in self.topics)
            if f in scratch or self.is_common(f) or f in self.topics or ROUND_DIR_RE.fullmatch(name(f)) or children or not self.exists_folder(f):
                continue
            ws = [w for w in authors if parent(w['path']) == f]
            seats = collections.defaultdict(set)
            for w in ws:
                m = ROUND_FILE_RE.fullmatch(name(w['path']))
                seats[int(m.group(1))].add(m.group(2).lower())
            if len({w['agent'] for w in ws}) >= 2 and any(len(ss) >= 2 for ss in seats.values()):
                self.file_topics.add(f)
                self.round_dirs_of[f] = []
                self.topics.append(f)
        self.topics = sorted(set(self.topics) | self.file_topics)
        self._seat = {}
        self.rooms, self.room_cells = self._rooms()
        self.cell_aliases = {}
        self.cell_paths = self._cell_paths()
        self.cells = {}
        for key, p in sorted(self.cell_paths.items()):
            self.cells[key] = self._cell(key[0], p, room=key[1] == '-')
        self.roots = self._roots()
        self.placed, self.launch_split = self._placed()
        self.listed, self.hinted, self.order = self._listing()

    # ------------------------------------------------------------------------------------------------------------ input
    def real(self, p):
        for alias, target in self.links.items():
            if p == alias or p.startswith(alias + '/'):
                return target + p[len(alias):]
        return p

    def _events(self):
        out = []
        for a in self.agents:
            out += [dict(w, agent=a['id'], path=self.real(w['path'])) for w in a['writes']]
        out += [dict(w, agent='orch', path=self.real(w['path'])) for w in self.orch['writes']]
        for w in out:
            w['confirmed'] = self.confirmed(w)
        return sorted(out, key=lambda w: (w['ts'], w['agent']))

    def exists_folder(self, f):
        return bool(f) and (any(under(d, f) for d in self.dirs) or any(under(p, f) for p in self.files))

    # ----------------------------------------------------------------------------------------------------------------- J1
    def fresh(self, w):
        """The save check F: the named file is there, its mtime interval (exact [m, m], coarse [m, m+1)) lies strictly after the span start (a save at or before it cannot be told
        from one made before the run) and starts by the span end plus the grace, and its body is the expected one when there is one. The size is not looked at (3rd revision)."""
        meta = self.files.get(w['path'])
        if not meta or not w.get('span'):
            return False
        lo = w['span'][0]
        m = meta['mtime']
        hi = w['span'][1] + (self.GRACE if w['evidence'] == 'planned' else self.EPS)
        if not (lo < m <= hi):
            return False
        return w['out'] is None or meta.get('body', 'x' * meta['size']) == w['out']

    def confirmed(self, w):
        return w['ok'] is True and (w['proof'] in ('tool', 'exit') or self.fresh(w))

    def unsure_attempt(self, w):
        """J3 (2), 4th revision: an authoring attempt that is not confirmed, whatever its result (unknown, a checked save that cannot be checked now, or a failure: a failed command
        may still have written, `printf x > Y.md && false`). An uncertain thing is never read as "did not write" (principle U)."""
        return w['kind'] in AUTHORING and not w['confirmed']

    # ------------------------------------------------------------------------------------------------------------ units
    def root_of(self, unit):
        """A bundle's root: the parent of a topic when it holds a `brief.md` and no round folder of its own, else the topic itself."""
        up = parent(unit)
        if up != unit and up + '/brief.md' in self.files and up not in self.topics:
            return up
        return unit

    def rnum(self, p):
        if parent(p) in self.file_topics:
            m = ROUND_FILE_RE.fullmatch(name(p))
            return int(m.group(1)) if m else None
        m = ROUND_DIR_RE.fullmatch(name(parent(p)))
        return int(m.group(1)) if m else None

    def too_broad(self, f):
        return f == '' or f.startswith(STATE_DIRS) or f in ('tmp', 'var/tmp')

    def is_common(self, f):
        """A folder every agent of a repository reads: the top of a repository (it holds `.git`), or the `docs` folder directly in it."""
        has_git = lambda d: (d + '/.git' in self.dirs) or (d + '/.git' in self.files) or d in self.c['disk'].get('git', {})
        return self.too_broad(f) or has_git(f) or (name(f) in COMMON_FOLDERS and has_git(parent(f)))

    def is_round_dir(self, f):
        """Whether `f` is a round folder (r1, r01, round1) of a topic: a place for cells, never a room (choice, J14 is silent on a room that has no file of its own)."""
        return f in {d for ds in self.round_dirs_of.values() for d in ds}

    def unit_below(self, f):
        """Whether a debate folder lies below `f` (a bundle's root, or a folder that holds topics): no room."""
        return any(u != f and under(u, f) for u in self.topics)

    def work_file(self, p):
        return not p.startswith(STATE_DIRS) and not p.startswith(SCRATCH_DIRS)

    # --------------------------------------------------------------------------------------------------------- demands J7
    def demands(self, a, p):
        """(ts, kind, run) of the runs of agent `a` that name `p`: a launch command's output (W4) or a BULLPEN_SEAT (J7, J13), in time order."""
        out = [(q['ts'], 'planned', q['run']) for q in a['planned'] if self.real(q['path']) == p]
        t = a['tag']
        if t and t['seat'] and self.valid_tag(t):
            seat = t['seat'] if t['seat'].lower().endswith('.md') else t['seat'] + '.md'
            if '/' in seat:
                if self.real(t['room'] + '/' + seat) == p:
                    out.append((a['run_start'], 'tag', t['run']))
            elif (t['room'] not in self.topics or (t['room'] in self.file_topics and ROUND_FILE_RE.fullmatch(seat))) and self.real(t['room'] + '/' + seat) == p:
                out.append((a['run_start'], 'tag', t['run']))                 # a room's seat is its own file (J13)
        return sorted(out, key=lambda d: (d[0], d[1]))

    def valid_tag(self, t):
        """J13 (2.4): a tag names a folder that is there, that is no state folder and not too broad."""
        return self.exists_folder(t['room']) and not self.too_broad(t['room'])

    def met(self, a, p):
        """J6 (3rd revision): (unmet, status of the run that asked) of agent a's latest demand on p, None if it has none. The latest demand is the one of the highest run, then the
        latest time. It is met by a confirmed event of a on p whose run is the demand's or a later one and whose time is not before it. A run that is not known (on any demand of
        a on p or on the event) meets nothing. The status is a's when the demand is of a's present run (or a run is unknown), else 'done' (a later run pushed it out)."""
        dem = self.demands(a, p)
        if not dem:
            return None
        if any(r is None for _, _, r in dem):
            return True, a['status']
        d_ts, _, d_run = max(dem, key=lambda d: (d[2], d[0]))
        unmet = not any(w['confirmed'] and w['agent'] == a['id'] and w['path'] == p and w['run'] is not None and w['run'] >= d_run and w['ts'] >= d_ts for w in self.events)
        return unmet, (a['status'] if a['run'] is None or d_run == a['run'] else 'done')

    # --------------------------------------------------------------------------------------------------------- J3 J4 J5 J7
    def seat_of(self, p):
        """owner, cell agent, editors, evidence and the seat diagnostics of the path p (no state yet): the earliest authoring event of an agent owns it, unless another agent's
        authoring event came within TIE_SEC, an unconfirmed attempt of another agent may have come first, or another agent's records are lost from before it (the seat is held)."""
        if p in self._seat:
            return self._seat[p]
        ws = [w for w in self.events if w['path'] == p]
        conf = [w for w in ws if w['confirmed']]
        first = {}
        for w in conf:
            if w['agent'] != 'orch' and w['kind'] in AUTHORING:
                first.setdefault(w['agent'], w)
        authors = sorted(first.values(), key=lambda w: (w['ts'], w['agent']))
        aliases = self.cell_aliases.get(p, ())
        diag, owner, held = ([('alias_collision', None)] if aliases else []), None, bool(aliases)
        if authors:
            y = authors[0]
            tied = [w['agent'] for w in authors[1:] if w['ts'] - y['ts'] <= self.TIE]
            unsure = sorted({w['agent'] for w in ws if w['agent'] not in ('orch', y['agent']) and self.unsure_attempt(w) and w['ts'] <= y['ts'] + self.TIE})
            lost = sorted(a['id'] for a in self.agents if a['id'] != y['agent'] and a.get('lost') and a['lost'][0] <= y['ts'] + self.TIE)
            if tied or unsure:
                held = True
                diag += [('seat_tie_held', x) for x in sorted({y['agent'], *tied, *unsure})]
            if lost:
                held = True
                diag += [('history_lost', x) for x in lost]
            if not held:
                owner = y['agent']
        prev_owner = None
        if owner:
            for w in authors[1:]:                                       # J4: a later author that was asked for this file and writes it takes it over from one that is over
                b = self.ids[w['agent']]
                if over_before(self.ids[owner], b) and self.demands(b, p) and w['ts'] >= b['start']:
                    prev_owner, owner = owner, b['id']
        editors = list(dict.fromkeys(w['agent'] for w in conf if w['agent'] != owner))
        if prev_owner:
            editors.remove(prev_owner)
            editors.insert(0, prev_owner)
        agent, evidence = owner, (first[owner]['evidence'] if owner else None)
        if not owner and not held:                                       # J7: nobody wrote it: the one asked for it, unless several were asked and none was over before another began
            cand = [a for a in self.agents if self.demands(a, p)]
            left = [g for g in cand if not any(h is not g and over_before(g, h) for h in cand)]
            if len(left) == 1:
                agent = left[0]['id']
                evidence = 'planned' if any(k == 'planned' for _, k, _ in self.demands(left[0], p)) else 'tag'   # J7: a launch output and a SEAT both: planned
            elif len(left) > 1:
                evidence = 'planned' if any(k == 'planned' for g in left for _, k, _ in self.demands(g, p)) else 'tag'
                diag += [('seat_tie_held', g['id']) for g in sorted(left, key=lambda g: g['id'])]
        self._seat[p] = dict(owner=owner, agent=agent, editors=editors, evidence=evidence, diag=diag)
        return self._seat[p]

    # -------------------------------------------------------------------------------------------------------------- cells
    def in_round(self, p, u):
        """Whether `p` is a `.md` directly in a round folder of topic `u`, whether or not the round folder is on disk (J2: a confirmed write, a launch output or a SEAT makes the cell)."""
        if u in self.file_topics:
            return parent(p) == u and bool(ROUND_FILE_RE.fullmatch(name(p)))
        return p.lower().endswith('.md') and parent(parent(p)) == u and bool(ROUND_DIR_RE.fullmatch(name(parent(p))))

    def _cell_paths(self):
        """J2: the cells of the judgment: {(unit, rdir, stem): path}. A cell is a `.md` directly in a round folder (on disk, written by a confirmed event, or asked for by a launch
        command or a SEAT; the round folder need not be on disk any more or yet) and, for a room, the file of each member (and the seat names of its tagged members)."""
        out = {}
        for u in self.topics:
            rd = set(self.round_dirs_of[u])
            ps = {p for p in self.files if parent(p) in rd and p.lower().endswith('.md')}
            if u in self.file_topics:
                ps |= {p for p in self.files if self.in_round(p, u)}
            ps |= {w['path'] for w in self.conf if self.in_round(w['path'], u)}
            for a in self.agents:
                for q in a['planned']:
                    q = self.real(q['path'])
                    if self.in_round(q, u):
                        ps.add(q)
                t = a['tag']
                if t and t['seat'] and ('/' in t['seat'] or u in self.file_topics) and t['room'] == u and self.valid_tag(t):
                    q = u + '/' + (t['seat'] if t['seat'].lower().endswith('.md') else t['seat'] + '.md')
                    if self.in_round(q, u):
                        ps.add(q)
            groups = collections.defaultdict(list)
            for p in sorted(ps):
                if u in self.file_topics:
                    m = ROUND_FILE_RE.fullmatch(name(p))
                    groups[(int(m.group(1)), m.group(2).lower())].append(p)
                else:
                    groups[(name(parent(p)), name(p)[:-3])].append(p)
            self.cell_aliases.update({p: tuple(ps) for ps in groups.values() if len(ps) > 1 for p in ps})
            for p in sorted(ps):
                if u in self.file_topics:
                    seat = ROUND_FILE_RE.fullmatch(name(p)).group(2).lower()
                    identity = cell_identity(u, seat, p, p in self.cell_aliases)
                    out[(u, name(p).split('_', 1)[0], identity)] = p
                else:
                    seat = name(p)[:-3]
                    identity = cell_identity(u, seat, p, p in self.cell_aliases)
                    out[(u, name(parent(p)), identity)] = p
        for f, files in self.room_cells.items():
            taken = collections.Counter(name(p)[:-3] for p in files)
            seats = {p: name(p)[:-3] if taken[name(p)[:-3]] == 1 else p[len(f) + 1:-3] for p in files}
            groups = collections.defaultdict(list)
            for p in sorted(files):
                groups[seats[p]].append(p)
            self.cell_aliases.update({p: tuple(ps) for ps in groups.values() if len(ps) > 1 for p in ps})
            for p in sorted(files):
                identity = cell_identity(f, seats[p], p, p in self.cell_aliases)
                out[(f, '-', identity)] = p
        return out

    def unit_paths(self, u):
        """The cell files of a topic (J15: every cell path of it): the files its round folders hold and the paths written, asked for or seated in its rounds."""
        return sorted(p for (uu, _r, _s), p in self.cell_paths.items() if uu == u)

    def window_hint(self, p):
        """J8: the one command window that could have written a file nobody was seen to write, or None (a hint only: it decides nothing else)."""
        meta = self.files.get(p)
        if not meta:
            return None
        m = meta['mtime']
        hi = m + 1 if meta.get('coarse') else m
        cands, lost = [], False
        for a in self.agents + [dict(id='orch', windows=self.orch['windows'], windows_capped=self.orch.get('windows_capped'))]:
            cap = a.get('windows_capped')
            lost |= bool(cap and cap[0] <= m <= cap[1])
            for w in a['windows']:
                t1 = w['t1'] if w['t1'] is not None else math.inf                 # an open window ends at infinity: no clock
                if w['t0'] <= hi + self.EPS and t1 >= m - self.EPS and p not in [self.real(r) for r in w['reads']]:
                    cands.append((a['id'], w))
        # O2: the call that launched a candidate agent X (a foreground `claude -p` covers X's whole run) is no rival of X's own window; any other window of the orchestrator is
        launch_calls = {x['launch']['call'] for x in self.agents if x['launch'] and x['launch']['call'] and any(aid == x['id'] for aid, _w in cands)}
        cands = [(aid, w) for aid, w in cands if w['call'] not in launch_calls]
        if len(cands) == 1 and not lost and cands[0][0] != 'orch' and cands[0][1]['ok'] is True:
            return {'kind': 'window', 'agent': cands[0][0]}
        return None

    def resumed_idle(self, a, p):
        """J6 (O14, D19): agent a was resumed (the run it is in is later than every run that asked it for p or wrote p, whatever came of the write) and has done nothing about p in it: what it saved
        in an earlier run stays a submission. A run that is not known holds the cell."""
        if a['run'] is None:
            return False
        runs = [r for _, _, r in self.demands(a, p)] + [w['run'] for w in self.events if w['agent'] == a['id'] and w['path'] == p]
        return bool(runs) and all(r is not None and r < a['run'] for r in runs)

    def _cell(self, scope, p, room=False):
        """J6: the state of a cell, whether its file is a previous one, and who it is shown for."""
        s = self.seat_of(p)
        meta = self.files.get(p)
        exists, file_ok = meta is not None, bool(meta and meta['size'] > 0)
        previous = exists and s['owner'] is None
        g = s['owner'] or s['agent']
        if s['owner']:
            o = self.ids[s['owner']]
            d = self.met(o, p)
            if d and d[0]:                                                    # the owner was asked again and has not saved since: not submitted, the old file is a previous one
                state, previous = cell_state(d[1], False, True, True), exists
            else:
                if room:
                    here = True
                else:                                                         # the round it works in now: the highest it holds in this topic
                    mine = [self.rnum(q) for (uu, _r, _s), q in self.cell_paths.items() if uu == scope and self.seat_of(q)['agent'] == o['id'] and self.rnum(q) is not None]
                    here = self.rnum(p) == max(mine)
                state = cell_state('done' if self.resumed_idle(o, p) else o['status'], file_ok, here, True)
        elif g:
            state = cell_state(self.met(self.ids[g], p)[1], False, True, True)
        else:
            state = 'previous' if exists else 'waiting'
        hint = self.window_hint(p) if s['owner'] is None and exists else None
        return dict(owner=s['owner'], agent=s['agent'], editors=s['editors'], evidence=s['evidence'], hint=hint, previous=previous, state=state)

    # -------------------------------------------------------------------------------------------------------------- rooms
    def own_files(self, a):
        """The files that agent `a` made in its own right (J14): confirmed authoring events on a `.md` that no guide name, no round folder and no other agent's own event is."""
        mine = {}
        for w in self.conf:
            if w['agent'] == a['id'] and w['kind'] in AUTHORING and w['evidence'] in SHOWN and w['path'].lower().endswith('.md') and name(w['path']) not in GUIDE_NAMES \
                    and not ROUND_DIR_RE.fullmatch(name(parent(w['path']))):
                mine.setdefault(w['path'], w)
        return [p for p in mine if not any(w['path'] == p and w['agent'] not in (a['id'], 'orch') for w in self.conf if w['kind'] in AUTHORING)]

    def outside_work(self, a, folder):
        """Whether agent a changed a file of the work outside the room's folder (a confirmed event of a tool or a command, never a command's output file), or lost track of some."""
        if a.get('writes_dropped'):
            return True
        return any(w['agent'] == a['id'] and w['evidence'] in SHOWN and not in_reach(w['path'], folder) and w['path'] != folder and self.work_file(w['path']) for w in self.conf)

    def _rooms(self):
        """J13 J14: {folder: {kind, sure, why, members, files}} and the files of each room's cells. Confirmed rooms: two or more agents tagged with a folder with no round folder.
        Estimated rooms: agents launched together that each made a file of their own directly in one and the same folder (O1), or that only talk and read one file of it."""
        rooms, taken = {}, set()
        tagged = collections.defaultdict(list)
        for a in self.agents:
            if a['tag'] and self.valid_tag(a['tag']):
                tagged[a['tag']['room']].append(a)
        for f, ms in sorted(tagged.items()):
            if len(ms) >= 2 and f not in self.topics and not self.unit_below(f):       # choice: a bundle's root is no room
                files = {a['id']: next((p for p in self.own_files(a) if in_reach(p, f)), None) for a in ms}
                seats = {a['id']: a['tag']['seat'] for a in ms if a['tag']['seat'] and '/' not in a['tag']['seat']}
                paths = [p for p in files.values() if p] + [f + '/' + (s if s.lower().endswith('.md') else s + '.md') for s in seats.values()]
                rooms[f] = dict(kind='cells' if paths else 'members', sure=True, why='tag', members=sorted(a['id'] for a in ms), files=sorted(set(paths)))
                taken |= {a['id'] for a in ms}
        groups = collections.defaultdict(list)
        for a in self.agents:
            if a['launch'] and a['id'] not in taken:
                groups[gkey(a['launch'])].append(a)
        for g, ms in sorted(groups.items(), key=lambda kv: str(kv[0])):
            ms = [a for a in ms if a['id'] not in taken]
            if len(ms) < 2:
                continue
            # O1: the folder of the room is the one that holds the members' own files directly (dirname of the first `.md` each made); they all have to be the same folder
            first = {a['id']: self.own_files(a)[0] for a in ms if self.own_files(a)}
            folders = {parent(p) for p in first.values()}
            if len(first) >= 2 and len(folders) == 1:
                (f,) = folders
                mine = [a for a in ms if a['id'] in first]
                if not (self.is_common(f) or f in self.topics or self.unit_below(f) or f in rooms):
                    if 2 * sum(1 for a in mine if self.outside_work(a, f)) > len(mine):
                        taken |= {a['id'] for a in mine}                           # choice: most of them work elsewhere, so none of them is a member of a room
                    else:
                        rooms[f] = dict(kind='cells', sure=False, why='launch', members=sorted(first), files=sorted(first.values()))
                        taken |= set(first)
            rest = [a for a in ms if a['id'] not in taken]
            if len(rest) >= 2 and not any(w['agent'] in {a['id'] for a in rest} and w['kind'] in AUTHORING and w['path'].lower().endswith('.md') for w in self.conf):
                talk = any(t in {a['id'] for a in rest} and t != a['id'] for a in rest for t in a['sent_to'])
                shared = set.intersection(*({self.real(r['path']) for r in a['reads'] if r['path'].lower().endswith('.md')} for a in rest))
                folders = sorted({parent(p) for p in shared})
                for f in folders:
                    if talk and not (self.is_common(f) or f in self.topics or self.is_round_dir(f) or self.unit_below(f) or f in rooms):
                        if 2 * sum(1 for a in rest if self.outside_work(a, f)) > len(rest):      # J14: the exclusions are those of every estimated room, most of them working elsewhere too
                            continue
                        rooms[f] = dict(kind='members', sure=False, why='launch', members=sorted(a['id'] for a in rest), files=[])
                        taken |= {a['id'] for a in rest}
                        break
        room_cells = {f: r['files'] for f, r in rooms.items() if r['kind'] == 'cells'}
        return rooms, room_cells

    # ------------------------------------------------------------------------------------------------------------ binding
    def bound(self, a):
        """The folders a confirmed binding ties agent `a` to (J10, J15): the topic of every cell it owns, edited or was asked for, the folder a tag names, its room."""
        out = set()
        for (u, _r, _s), p in self.cell_paths.items():
            x = self.cells[(u, _r, _s)]
            if a['id'] in (x['owner'], x['agent'], *x['editors']) or self.demands(a, p):
                out.add(u)
        if a['tag'] and self.valid_tag(a['tag']):
            out.add(a['tag']['room'])
        for f, r in self.rooms.items():
            if a['id'] in r['members']:
                out.add(f)
        return out

    # --------------------------------------------------------------------------------------------------------- J11 J12
    def _placed(self):
        """J11: for an agent that sits in no cell, the debate it most likely belongs to, with the reasons; J12: the agents whose reasons point at two roots."""
        placed, split = {a['id']: None for a in self.agents}, {}
        seated = set()
        for key, x in self.cells.items():
            seated |= {x['owner'], x['agent'], *x['editors']} - {None}
        for a in self.agents:
            for p in self.cell_paths.values():
                if self.demands(a, p):
                    seated.add(a['id'])
        for f, r in self.rooms.items():
            seated |= set(r['members'])
        units = [u for u in self.topics if self.root_of(u) in self.roots]              # J11: only the debates on the list (before J17 hides a copy: a placement is evidence)
        for a in self.agents:
            if a['id'] in seated:
                continue
            reasons = collections.defaultdict(set)                                 # topic -> reasons
            for u in units:
                root = self.root_of(u)
                if a['tag'] and self.valid_tag(a['tag']) and a['tag']['room'] in (u, root):
                    reasons[u].add('tag')
                named = lambda h: bool(h and a['launch'] and (a['launch']['call'] in h['calls'] or gkey(a['launch']) in hint_groups(h)))        # noqa: E731
                if named(self.orch['hints'].get(u)):
                    reasons[u].add('launch_call')
                if root != u and named(self.orch['hints'].get(root)):                  # O14: what was done in the root points at the root, and every topic of it waits
                    reasons[root].add('launch_call')
                g = gkey(a['launch'])
                if g and any(b is not a and gkey(b['launch']) == g and (u in self.bound(b) or root in self.bound(b)) for b in self.agents):
                    reasons[u].add('launch_peer')
                if any(r['path'] and self.real(r['path']) in [u + '/' + n for n in GUIDE_NAMES] + [root + '/' + n for n in GUIDE_NAMES] for r in a['reads']):
                    reasons[u].add('guide_read')
            for u in list(reasons):                                               # J11 invalid: it wrote a `.md` of the work elsewhere, so the weak reasons for this debate go
                if any(w['agent'] == a['id'] and w['kind'] in AUTHORING and w['evidence'] in SHOWN and w['path'].lower().endswith('.md') and not under(w['path'], u)
                       and self.work_file(w['path']) for w in self.conf):
                    reasons[u].discard('launch_call')
                    reasons[u].discard('launch_peer')
                    reasons[u].discard('guide_read')
                if not reasons[u]:
                    del reasons[u]
            if not reasons:
                continue
            by_root = collections.defaultdict(dict)
            for u, rs in reasons.items():
                by_root[self.root_of(u)][u] = rs
            sure = [r for r, us in by_root.items() if any('tag' in rs for rs in us.values())]
            if sure:                                                               # a tag settles it; the reasons of other roots are not counted
                by_root = {r: by_root[r] for r in sure}
            if len(by_root) > 1:
                split[a['id']] = ','.join(sorted(name(r) for r in by_root))               # the names of the roots (J12), not their paths
                continue
            (root, us), = by_root.items()
            whys = [w for w in REASON_ORDER if any(w in rs for rs in us.values())]
            topic = next(iter(us)) if len(us) == 1 and next(iter(us)) != root else None
            placed[a['id']] = dict(unit=root, topic=topic, why=whys[0], whys=whys, sure=whys[0] == 'tag')
        return placed, split

    # -------------------------------------------------------------------------------------------------------------- J9 J10
    def _roots(self):
        """J9: the roots a debate is listed for, before copies are hidden: {root: what lists it (cell, tag, hint, walk, room)}."""
        by_unit = collections.defaultdict(set)                                     # topic -> what lists it: cell, tag, hint, walk
        for u in self.topics:
            for (uu, _r, _s), p in self.cell_paths.items():
                if uu == u and any(w['agent'] != 'orch' and w['path'] == p for w in self.conf):
                    by_unit[u].add('cell')
                if uu == u and any(self.demands(a, p) for a in self.agents):
                    by_unit[u].add('cell')
            if any(a['tag'] and self.valid_tag(a['tag']) and a['tag']['room'] in (u, self.root_of(u)) for a in self.agents):
                by_unit[u].add('tag')
            if u in self.orch['hints'] and self.written_debate(u):
                by_unit[u].add('hint')
            if u in self.walked:
                by_unit[u].add('walk')
        roots = collections.defaultdict(set)
        for u in by_unit:
            roots[self.root_of(u)] |= by_unit[u]
        for f in self.rooms:
            roots[f] |= {'room'}
        return dict(roots)

    def _listing(self):
        """J9: the debates on the list (roots), those the orchestrator's writes alone put there, and the list order (J10, with J17's hiding of copies)."""
        roots = {r: set(why) for r, why in self.roots.items()}
        hidden = self._hidden_copies(roots)
        for r in hidden:
            del roots[r]
        listed = sorted(roots)
        hinted = sorted(r for r, why in roots.items() if why == {'hint'})
        order = sorted(roots, key=lambda r: (not self._bound_root(r), r in hinted, r in self.rooms and not self.rooms[r]['sure'], -self._last_ts(r), r))
        return listed, hinted, order

    def written_debate(self, u):
        """The orchestrator's write names a debate only when the folder is one on disk: a guide and a round folder, not a state or a too broad folder."""
        guides = [n for n in GUIDE_NAMES if u + '/' + n in self.files]
        return bool(guides) and bool(self.round_dirs_of.get(u)) and not self.too_broad(u) and (guides[0] == 'brief.md' or not self.is_common(u))

    def _topics_of(self, root):
        return [u for u in self.topics if self.root_of(u) == root]

    def _bound_root(self, root):
        return any(self.bound(a) & (set(self._topics_of(root)) | {root}) for a in self.agents)

    def _last_ts(self, root):
        ts = [a['start'] for a in self.agents if self.bound(a) & (set(self._topics_of(root)) | {root})]
        ts += [h['ts'] for f, h in self.orch['hints'].items() if f == root or self.root_of(f) == root]
        return max(ts) if ts else 0

    def _hidden_copies(self, roots):
        """J17, O10: of the folders that are one place of one repository (a checkout and its worktrees), the ones with no evidence of work in them are hidden when another has some; where none has any,
        one stands for them: the one in the main checkout (the top holds a `.git` folder), else the shortest path, else by name (the order of 0.2.1's `copies.fold`)."""
        git = self.c['disk'].get('git', {})
        ident, top_of = {}, {}
        for r in roots:
            top = next((t for t in sorted(git, key=len, reverse=True) if under(r, t)), None)
            ident[r] = ('git', git[top], r[len(top):]) if top else ('real', r)
            top_of[r] = top
        hidden = set()
        for r in roots:
            same = [q for q in roots if q != r and ident[q] == ident[r]]
            if not same:
                continue
            if any(self._evidence(q) for q in [r] + same):
                if not self._evidence(r):
                    hidden.add(r)
            else:
                group = sorted([r] + same, key=lambda q: (not (top_of[q] is not None and top_of[q] + '/.git' in self.dirs), len(q), q))
                if group[0] != r:
                    hidden.add(r)
        return hidden

    def _evidence(self, root):
        """What shows that work happens in a folder (J17): a cell somebody sits in, a tag, a placement, a hint. A read is none."""
        tops = set(self._topics_of(root)) | {root}
        return any(self.bound(a) & tops for a in self.agents) or any(p and p['unit'] == root for p in self.placed.values()) or root in self.orch['hints'] \
            or any(h in self.orch['hints'] for h in tops)

    def current(self):
        for r in self.order:
            if not (r in self.rooms and not self.rooms[r]['sure']):                # choice: an estimated room is never the current one
                return r
        return None

    # ----------------------------------------------------------------------------------------------------------- J15 J16
    def scope_of(self, kind, name_):
        if kind == 'topic':
            return [name_]
        if kind == 'bundle':
            return self._topics_of(name_)
        return []

    def final(self, kind, scope):
        """J15: the one document that closes a topic, a room or a bundle, or why none is confirmed. Returns (final, live, estimated)."""
        topics = self.scope_of(kind, scope)
        if kind == 'room':
            room = self.rooms[scope]
            paths = set(room['files'])
            estimated = not room['sure']
        else:
            paths = {p for u in topics for p in self.unit_paths(u)}
            estimated = False
        cells = {p: x for (u, _r, _s), p in self.cell_paths.items() if p in paths for x in [self.cells[(u, _r, _s)]]}
        root = scope if kind == 'bundle' else self.root_of(scope)
        who = set()                                                                # S's participants: the ones in its cells, who tried to write one, who was asked for one, who is tagged
        for p, x in cells.items():
            who |= {x['owner'], x['agent'], *x['editors']}
            who |= {w['agent'] for w in self.events if w['path'] == p}
            who |= {a['id'] for a in self.agents if self.demands(a, p)}
        who |= {a['id'] for a in self.agents if a['tag'] and (a['tag']['room'] in topics or a['tag']['room'] in (scope, root))}
        for aid, pl in self.placed.items():
            if pl and ((kind == 'bundle' and pl['unit'] == scope) or (kind != 'bundle' and pl['unit'] == root and pl['topic'] in (None, scope))):
                who.add(aid)
        if kind == 'room':
            who |= set(self.rooms[scope]['members'])
        live = any(a['id'] in who and a['status'] in OPEN for a in self.agents)
        reports = [w for w in self.conf if w['path'] in paths and w['agent'] != 'orch' and w['kind'] in AUTHORING]
        if not reports:
            return dict(confirmed=False, path=None, by=None, why=['no_report'], candidates=[]), live, estimated
        lc, la = self.last_confirmed(paths), self.last_attempt(paths)
        cands = self.candidates(kind, scope, paths, lc)
        rivals = self.rivals(kind, scope, paths, lc)
        proven = sorted({w['path'] for w in self.conf if w['path'] in rivals and w['evidence'] in SHOWN and w['ts'] >= la - self.SAME and w['path'] in cands
                         and self.files[w['path']]['mtime'] >= la - self.SAME})
        rivals = sorted(set(rivals) | self.member_rivals(kind, scope, paths, proven, la))
        dirs = {d for u in topics for d in self.round_dirs_of[u]}
        empty = any(not any(parent(p) == d and p.lower().endswith('.md') for p in self.files) for d in dirs)
        lost = any(x.get('lost') and x['lost'][1] >= lc - self.SAME for x in self.agents + [self.orch])
        held = any(self.seat_of(p)['diag'] for p in paths)                           # a cell nobody could be told to own (J3 hold, J7 contention) is not submitted
        hollow = any(p in self.files and self.files[p]['size'] <= 0 for p in paths)  # an empty cell file is not a submission
        why = set()
        if any(x['state'] in NOT_SUBMITTED for x in cells.values()) or held or hollow:
            why.add('open_cell')
        if not proven:
            why.add('none')
        if len(rivals) > 1:
            why.add('several')
        if empty:
            why.add('empty_round')
        if live:
            why.add('live_participant')
        if estimated:
            why.add('estimated_room')
        if lost:
            why.add('history_lost')
        why = [w for w in WHY_ORDER if w in why]
        path = proven[0] if not why else None
        by = next((w['agent'] for w in reversed(self.conf) if w['path'] == path), None) if path else None
        return dict(confirmed=not why, path=path, by=by, why=why, candidates=cands), live, estimated

    def last_confirmed(self, paths):
        """J15 Lc: the latest confirmed write on the cell files, whatever its kind and whoever wrote it: it sets the candidates, the rivals and what may be lost from the records (only a
        confirmed update can remove a rival)."""
        return max(w['ts'] for w in self.conf if w['path'] in paths)

    def last_attempt(self, paths):
        """J15 La: the latest time a cell file may have changed: a write attempt on it, whatever it was and whatever came of it (a failed one may have emptied the file), or the file's
        present time (a change nobody's record shows). It can only withdraw a conclusion, never shorten the rivals."""
        file_units = {parent(p) for p in paths if parent(p) in self.file_topics}
        return max([w['ts'] for w in self.events if w['path'] in paths or (parent(w['path']) in file_units and ROUND_FILE_RE.fullmatch(name(w['path'])))]
                   + [self.files[p]['mtime'] for p in paths if p in self.files])

    def room_mine(self, kind, scope):
        """O14: in a room, the files its members made with a confirmed write and nobody else tried to write: theirs, no candidate of the room (one somebody else wrote at too is a document of it)."""
        if kind != 'room':
            return set()
        members = set(self.rooms[scope]['members'])
        return {w['path'] for w in self.conf if w['agent'] in members and w['kind'] in AUTHORING and all(x['agent'] in members for x in self.events if x['path'] == w['path'])}

    def place_of_final(self, kind, scope, paths, members_too=False):
        """The candidate places K: the documents directly in the scope (a bundle's folder and its `final/`), no guide, no round instruction, no cell file, and (a room) no file of a member of its own."""
        parents = {scope, scope + '/final'} if kind == 'bundle' else {scope}
        mine = set() if members_too else self.room_mine(kind, scope)
        return lambda p: parent(p) in parents and p.lower().endswith('.md') and name(p) not in GUIDE_NAMES and not ROUND_DOC_RE.match(name(p)) and p not in paths and p not in mine

    def member_rivals(self, kind, scope, paths, proven, la):
        """O14a: a room member's own file is no candidate but may be the later word, so it competes when it was written (or tried: principle U) not before the final was written less SAME_TIME, or when
        its time moved after its last record (nobody's event says by whom). A report that finished before the final does not."""
        place = self.place_of_final(kind, scope, paths, members_too=True)
        wrote = [max(w['ts'] for w in self.conf if w['path'] == q and w['evidence'] in SHOWN and w['ts'] >= la - self.SAME) for q in proven]
        out = set()
        for m in self.room_mine(kind, scope):
            if not place(m) or m not in self.files:
                continue
            evs = [x for x in self.events if x['path'] == m]
            if (wrote and any(x['kind'] in AUTHORING and x['ts'] >= min(wrote) - self.SAME for x in evs)) or self.files[m]['mtime'] > max(max([x['ts']] + list(x.get('span') or [])) for x in evs) + self.SAME:
                out.add(m)
        return out

    def candidates(self, kind, scope, paths, lc):
        """J15 C (shown, sorted): the documents of K that are sized and were made at or after the last confirmed write on the cells; the ones whose name says a conclusion first (a
        sort, nothing more), then the latest."""
        place = self.place_of_final(kind, scope, paths)
        cands = [p for p, m in self.files.items() if place(p) and m['size'] > 0 and m['mtime'] >= lc - self.SAME]
        return sorted(cands, key=lambda p: (not bool(CONCLUDE_RE.search(name(p)[:-3])), -self.files[p]['mtime'], p))

    def rivals(self, kind, scope, paths, lc):
        """J15 A: the places of K with a write attempt (any result, any evidence, whoever) at or after the last confirmed write on the cells: a file that was deleted or emptied, or whose time is older,
        is still a rival. O14 (principle U): so is a candidate (C) that came or changed after it with no event at all."""
        place = self.place_of_final(kind, scope, paths)
        return sorted({w['path'] for w in self.events if place(w['path']) and w['ts'] >= lc - self.SAME} | set(self.candidates(kind, scope, paths, lc)))

    # ------------------------------------------------------------------------------------------------------------ output
    def shown_topics(self):
        return [u for r in self.listed for u in self._topics_of(r)]

    def seat_diags(self):
        out = []
        for key, p in self.cell_paths.items():
            if key[0] not in self.shown_topics() and key[0] not in self.rooms:
                continue
            for code, who in self.seat_of(p)['diag']:
                seat = ROUND_FILE_RE.fullmatch(name(p)).group(2).lower() if key[0] in self.file_topics else name(p)[:-3] if p in self.cell_aliases else key[2]
                paths = self.cell_aliases[p] if code == 'alias_collision' else None
                detail = ','.join(q[len(key[0]) + 1:] for q in paths) if paths else '%s/%s' % (self.rnum(p) if key[1] != '-' else '-', seat)
                row = {'code': code, 'agent': who, 'unit': key[0], 'detail': detail}
                if paths is not None:
                    row['paths'] = list(paths)
                if row not in out:
                    out.append(row)
        return out

    def alias_diags(self):
        out = []
        for u in self.shown_topics():
            by_round = collections.defaultdict(list)
            for d in self.round_dirs_of[u]:
                by_round[int(ROUND_DIR_RE.fullmatch(name(d)).group(1))].append(name(d))
            out += [{'code': 'alias_collision', 'agent': None, 'unit': u, 'detail': ','.join(sorted(ds))} for ds in by_round.values() if len(ds) > 1]
        return out

    def truth(self):
        shown = self.shown_topics()
        cells = {'%s|%s|%s' % key: x for key, x in self.cells.items() if key[0] in shown or key[0] in self.rooms}
        finals, closable = {}, {}
        for u in shown:
            finals[u], live, est = self.final('topic', u)
            closable[u] = False
        for r in self.listed:
            if r in self.rooms:
                finals[r], live, est = self.final('room', r)
        for r in self.listed:
            if r in self.rooms or r in self.topics:
                continue
            if self._topics_of(r):
                finals[r] = self.final('bundle', r)[0]
        for u in shown:
            f, live, est = self.final('topic', u)
            root = self.root_of(u)
            fr = finals.get(root) if root != u else None
            closable[u] = bool((f['confirmed'] or (fr and fr['confirmed'])) and not live and not est)
        for r in self.listed:
            if r in self.rooms:
                f, live, est = self.final('room', r)
                closable[r] = bool(f['confirmed'] and not live and not est)
        diag = self.seat_diags() + self.alias_diags() + [{'code': 'launch_split', 'agent': aid, 'unit': None, 'detail': d} for aid, d in sorted(self.launch_split.items())]
        diag.sort(key=lambda d: (d['code'], d['unit'] or '', d['detail'] or '', d['agent'] or ''))
        paths = {'%s|%s|%s' % key: p for key, p in self.cell_paths.items() if key[0] in shown or key[0] in self.rooms}
        units = sorted(set(shown) | {r for r in self.listed if r in self.rooms})
        return dict(listed=self.listed, hinted=self.hinted, current=self.current(), order=self.order, units=units, cells=cells, paths=paths, placed=self.placed,
                    rooms={f: {k: v for k, v in r.items() if k != 'files'} for f, r in self.rooms.items() if f in self.listed},
                    finals=finals, closable=closable, diag=diag)


def truth(case, consts=None):
    """The truth of one input (one case, or one step of one)."""
    return Judge(case, consts).truth()


def apply(case, add):
    """A `then` step's change to an input (3.1): agents are added; fields are set on agents; writes and launch commands are appended; files and folders are added or replaced."""
    c = copy.deepcopy(case)
    c['agents'] += copy.deepcopy(add.get('agents', []))
    ids = {a['id']: a for a in c['agents']}
    for aid, kv in add.get('set_agent', {}).items():
        ids[aid].update(copy.deepcopy(kv))
    for aid, ws in add.get('agent_writes', {}).items():
        ids[aid]['writes'] += copy.deepcopy(ws)
    for aid, ps in add.get('agent_planned', {}).items():
        ids[aid]['planned'] += copy.deepcopy(ps)
    c['disk']['files'].update(copy.deepcopy(add.get('files', {})))
    c['disk']['dirs'] += [d for d in add.get('dirs', []) if d not in c['disk']['dirs']]
    return c


def steps(case):
    """[(k, input, expect)]: step 0 is the case, step k the input after the k-th `then` (each one adds to the one before)."""
    out = [(0, case, case['expect'])]
    cur = case
    for k, t in enumerate(case['then'], 1):
        cur = apply(cur, t['add'])
        out.append((k, cur, t['expect']))
    return out


def compare(got, want):
    """The keys of `want` that `got` does not reproduce (3.1: only the keys the expectation has, each whole). Lists of places are compared as sorted lists."""
    bad = []
    for k, w in want.items():
        g = got[k]
        if k == 'diag':
            key = lambda d: (d['code'], d['unit'] or '', d['detail'] or '', d['agent'] or '')           # noqa: E731
            g, w = sorted(g, key=key), sorted(w, key=key)
        if g != w:
            bad.append((k, g, w))
    return bad
