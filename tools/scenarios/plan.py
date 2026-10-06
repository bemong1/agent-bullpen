"""What the records of a scene say, as the facts of the contract (the JSON shape of tests/data/contract_cases.json, 3.1): who wrote which file, who read which, which launch
command named which output, who was launched together, which tag the process carried, which files are on disk with which time. The scene builders write the same things as
records (the Write tool, a command, a `FileChange`, `-o`, `claude -p > file`); this module says them once from the axis values, and `contract.truth` judges them.
It never imports board/ and it never reads a word of any text: an axis that only changes words (the language, a marker, a quote, a decl, a word) is not in here.

Times are seconds from the case's base time (the builder's `b.T(0)`), paths are relative to the repository top (`/W`); what the participants keep outside the repository
(a scratch folder, a log of the board) is below `tmp/`.
"""

import collections

from .axes import (CHILD_START, CLAUDE_RUN, CODEX_RUN, ABOVE_DOC, ALIAS_DIR, COMMON_DOCS, EDIT_DIR, ROOM_ALIAS, ROOM_BUNDLE, ROOM_GUIDES, ROUND_DIR, SEAT_LETTERS, UNIT_REL, aux_file_exists, launcher_writes, room_code,
                   room_disk, room_folder, room_guide, room_late_guide, room_out, room_timing, room_tree, wrote_itself, cxo_record)

REPORT_BODY = '# Report\n\nFirst finding.\nSecond finding.\n'
AUX_BODY = 'The last message of the run.'
PARTNER_BODY = '# Partner report\n\nThe flow is fine.\n'
GUIDE_T = -1000.0                                    # when the guides were written (before every run)
STARTED = ('running', 'stalled', 'unknown')


class Facts:
    """A contract case under construction: agents, their events, the files on disk and the orchestrator's writes."""

    def __init__(self, cid='scene'):
        self.cid = cid
        self.agents = collections.OrderedDict()
        self.files, self.dirs, self.links, self.git = {}, [], {}, {}
        self.orch = {'writes': [], 'windows': [], 'hints': {}, 'lost': None}
        self.walked = []

    # ---- agents and what they did
    def agent(self, aid, provider='claude', origin='subagent', status='done', start=0.0, first=None, last=None, launch=True, tree='S', node=None, call=None):
        """`launch=True`: launched by a call of its own (a group of its own, so it is nobody's peer); a group name: launched together with the others of that name (`call`: by the same
        call too); None: no key."""
        key = None
        if launch:
            key = {'provider': 'claude', 'tree': tree, 'node': node, 'group': 'm:' + aid if launch is True else launch, 'call': call or 'c:' + aid}
        a = {'id': aid, 'provider': provider, 'origin': origin, 'status': status, 'start': start, 'first': first if first is not None else start,
             'last': last if last is not None else start + 20, 'run': 1, 'run_start': start, 'launch': key, 'writes': [], 'reads': [], 'planned': [], 'windows': [],
             'windows_capped': None, 'writes_dropped': None, 'lost': None, 'tag': None, 'sent_to': []}
        self.agents[aid] = a
        return a

    def write(self, aid, path, ts, kind='create', evidence='tool', ok=True, proof=None, span=None, out=None, run=1, call=None, orch=False):
        """A write event as the collector gives it (2.1): `proof` tool for a tool, exit for a command in a deciding place, window/sha/content for what a later look at the disk has to confirm."""
        proof = proof or {'tool': 'tool', 'shell': 'exit', 'planned': 'sha'}[evidence]
        w = {'path': path, 'ts': ts, 'kind': kind, 'evidence': evidence, 'ok': ok, 'call': call, 'run': run, 'proof': proof, 'span': list(span) if span else None, 'out': out}
        (self.orch['writes'] if orch else self.agents[aid]['writes']).append(w)
        return w

    def read(self, aid, path, ts, via='tool'):
        self.agents[aid]['reads'].append({'path': path, 'ts': ts, 'via': via})

    def plan(self, aid, path, op, ts, run=1, call=None):
        """A launch command named `path` as its output: it asks `aid` to have written it."""
        self.agents[aid]['planned'].append({'path': path, 'op': op, 'ts': ts, 'run': run, 'call': call})

    def tag(self, aid, room, seat=None, source='environ', run=1):
        self.agents[aid]['tag'] = {'room': room, 'seat': seat, 'source': source, 'run': run}

    # ---- the disk
    def file(self, path, mtime, size=1, body=None, coarse=False):
        m = {'mtime': mtime, 'size': len(body.encode()) if body is not None else size}
        if body is not None:
            m['body'] = body
        if coarse:
            m['coarse'] = True
        self.files[path] = m

    def dir(self, path):
        if path not in self.dirs:
            self.dirs.append(path)

    def case(self):
        return {'id': self.cid, 'disk': {'dirs': sorted(self.dirs), 'files': dict(self.files), 'links': dict(self.links), 'git': dict(self.git)}, 'orch': self.orch,
                'agents': list(self.agents.values()), 'walked': list(self.walked), 'expect': {}, 'then': []}


class Scene:
    """The facts of a scene and the names a truth needs: `F` (Facts), `unit` (the debate folder of the participant under test), `report` (its report path), `me` (the agent id under test)."""

    def __init__(self, F, **names):
        self.F = F
        self.__dict__.update(names)


# ---------------------------------------------------------------------------------------------------------------------
# the debate scene (scene_deb.Deb)
# ---------------------------------------------------------------------------------------------------------------------
def deb_names(v):
    """(unit, unit2, root, stem of the participant's report, stem of the partner's, name of the guide) of the debate scene: where the folders are and how the files are called."""
    s = v['structure']
    root = 'docs/rev' if s == 'topics' else None
    unit = {'topics': 'docs/rev/t1', 'deep3': 'docs/records/2026/rev', 'dot': '.records/rev'}.get(s, 'docs/rev')
    unit2 = 'docs/rev/t2' if s == 'topics' else ('docs/rev2' if v['homonym'] == 'two' else None)
    n = v['nstyle']
    stem = {'plain': 'B', 'named': 'B_gate', 'lower': 'b', 'numbered': 'opus1', 'collide': 'B_gate'}[n]
    pstem = {'numbered': 'astra1', 'named': 'A_flow'}.get(n, 'A')
    if s == 'flat':
        stem, pstem = ('sol', 'opus') if n == 'plain' else ('sol-final3', 'opus')
    return unit, unit2, root, stem, pstem, 'README.md' if s == 'readme' else 'brief.md'


def child_times(kind, life):
    """The times of the participant under test, as the builder makes them (scene_sta): when it was launched, when it began, when its tail work is done, when it ended or None."""
    if kind == 'sub':
        t = -194.0                                   # the Agent call at -200, its first record at -199, the tail 5 s after that
        return dict(start=-200.0, first=-199.0, tail=t, end=None if life in ('running', 'stalled_silent') else t + 4)
    if kind == 'cli':
        end = None if life in ('running', 'stalled_silent') else {'time_limit_kill': 1804.4, 'time_limit_silent': 1804.4, 'taskstop_kill': 62.4}.get(life, 10.4)
        return dict(start=0.0, first=2.4, tail=6.6, end=end)
    return dict(start=0.0, first=2.4, tail=7.4, end=None if life in ('running', 'stalled_silent') else (7.4 if life == 'taskstop_kill' else 9.4))


def deb_launch(v, mine, unit, rd, stem):
    """(the paths the launch command of the participant names as the output of the run, the last message of the run that lands in them): `>` of a `claude -p` run, `-o` of a
    `codex exec` run. The report itself when the instruction's path is also the output (`redirect`, `dash_o`, `var`), the file of another name beside it (`dash_o_aux`), a file
    outside the repository (`dash_o_last`), or the report's own name in the other spelling of the round folder (`aux=alias`). The message is the report's body when it is the report."""
    kind, rp = v['kind'], v['rpath']
    asked = []
    if kind == 'cli':
        if rp in ('redirect', 'var'):
            asked.append(mine)                                              # (`var`: `D=<folder>; ... > "$D/..."`, the variable is set in the command itself)
        elif rp == 'dash_o_aux':
            asked.append('%s/%s/%s_last.md' % (unit, rd, stem))
    elif kind == 'codex':
        if rp in ('dash_o', 'var'):
            asked.append(mine)
        elif rp == 'dash_o_last':
            asked.append('tmp/scratch/B_last.md')
        elif rp == 'dash_o_aux':
            asked.append('%s/%s/%s_last.md' % (unit, rd, stem))
    if v['aux'] == 'alias' and kind in ('cli', 'codex'):
        asked = ['%s/%s/%s.md' % (unit, ALIAS_DIR, stem)]                  # the launch writes the report's own name in the other spelling of the round folder
    if v['role'] != 'writer' or not asked:
        return [], None
    return asked, REPORT_BODY if mine in asked else AUX_BODY


def deb_scene(v, status, coupling=None):
    """The facts of the debate scene of axis values `v` (`status`: what the life of the participant makes of it, from oracle.life_state). A coupling scene (`coupling` is the
    spawner) is a `claude -p` participant that writes a plain report."""
    F = Facts('deb')
    kind, role, life, rp = ('cli' if coupling else v['kind']), v['role'], v['life'], v['rpath']
    unit, unit2, root, stem, pstem, guide = deb_names(v)
    flat = v['structure'] == 'flat'
    rd, rd2 = ROUND_DIR[v['rdir']] % 1, ROUND_DIR[v['rdir']] % 2
    mine = '%s/%s.md' % (unit, stem) if flat else '%s/%s/%s.md' % (unit, rd, stem)
    pfile = '%s/%s.md' % (unit, pstem) if flat else '%s/%s/%s.md' % (unit, rd, pstem)
    # ---- the disk the harness leaves (scene_deb.write_disk)
    units = [unit] + ([unit2] if unit2 else [])
    if root:
        F.file(root + '/brief.md', GUIDE_T)
    if v['edits'] == 'beside':
        F.file('%s/%s/brief.md' % (root, EDIT_DIR), GUIDE_T)
        F.file('%s/%s/names.md' % (root, EDIT_DIR), GUIDE_T)
    for u in units:
        F.file('%s/%s' % (u, guide), GUIDE_T)
        if not flat:
            F.dir('%s/%s' % (u, rd))
            F.dir('%s/%s' % (u, rd2))
    if v['rdir'] == 'both':
        F.dir('%s/%s' % (unit, ALIAS_DIR))
    F.dir('.git')
    F.file(pfile, -900.0)
    if v['homonym'] == 'two':
        F.file('%s/%s.md' % (unit2, pstem) if flat else '%s/%s/%s.md' % (unit2, rd, pstem), GUIDE_T + 1)
    ct = child_times(kind, life)
    ended = ct['end'] is not None
    if role != 'absent':
        F.agent('partner', status='done', start=-950.0, first=-949.0, last=-894.0)
        F.write('partner', pfile, -900.0)
    if v['nstyle'] == 'collide':
        for other in ('B.md', 'b.md'):
            F.file('%s/%s/%s' % (unit, rd, other), GUIDE_T + 2)
    if role in ('reader', 'ref_reader'):
        F.agent('writerB', status='done', start=-940.0, first=-939.0, last=-884.0)
        F.write('writerB', mine, -890.0)
        F.file(mine, -890.0)
    elif role == 'absent':
        F.file(mine, GUIDE_T + 3)
    if role == 'rival':
        F.agent('rival', status='running', start=-320.0, first=-319.0, last=-300.0)
    if v['rdir'] == 'both' and v['aux'] != 'alias':
        F.file('%s/r01/%s.md' % (unit, stem), GUIDE_T + 4)                    # somebody else's file of the same name in the other spelling
    # ---- the participant under test
    if role == 'absent':
        F.walked = [u for u in ([unit] + ([unit2] if unit2 else [])) if not any(seg.startswith('.') for seg in u.split('/'))]
        return Scene(F, unit=unit, report=mine, me=None, root=root)
    F.agent('child', provider='codex' if kind == 'codex' else 'claude', origin={'sub': 'subagent', 'cli': 'cli', 'codex': 'exec'}[kind], status=status, start=ct['start'],
            first=ct['first'], last=ct['end'] if ended else ct['tail'] + 4)
    t = ct['tail']
    guide_path = '%s/%s' % (unit, guide)
    if role in ('reader', 'ref_reader'):
        if kind == 'codex':
            F.read('child', guide_path, ct['first'] + 1.0, 'codex')
            F.read('child', mine, ct['first'] + 2.0, 'codex')
        else:
            F.read('child', guide_path, t)
            F.read('child', mine, t + 0.4)
    elif role == 'tag_only':
        F.read('child', guide_path, t)
    # ---- what it writes itself (a Write tool, a patch or a command)
    scratch = 'tmp/scratch'
    shell = v['wmode'] != 'tool'

    def own(path, ts, ok=True, name=None):
        """The participant's own write of `path`: a tool (Write, or a patch for Codex) or a command in a deciding place (a redirect, `tee`, a heredoc)."""
        if shell and kind != 'codex':
            F.write('child', path, ts, 'replace', 'shell', ok)
        else:
            F.write('child', path, ts, 'create', 'tool', ok)

    if role == 'writer' and wrote_itself(dict(v, kind=kind)):
        own(mine, t + (0.0 if kind != 'codex' else 0.0))
        F.file(mine, t + 0.5, 1)
    elif role == 'failed_write':
        target = '%s/%s/B.md' % (unit, rd)
        F.write('child', target, t, 'replace' if shell and kind != 'codex' else 'create', 'shell' if shell and kind != 'codex' else 'tool', False)
    elif role in ('quoter', 'tag_only') and shell:
        F.write('child', scratch + '/doc.md', t + (0.5 if role == 'tag_only' else 0.0), 'replace', 'shell')
    elif role == 'reader' and shell:
        F.write('child', scratch + ('/notes.md' if v['wmode'] == 'heredoc' else '/count.txt'), t + 0.8, 'replace', 'shell')
    # ---- what the launch command asks for (`>`, `-o`) and what the run's end gives
    asked, final = deb_launch(dict(v, kind=kind), mine, unit, rd, stem)
    if role == 'writer' and asked:
        op = '-o' if kind == 'codex' else '>'
        for path in asked:
            F.plan('child', path, op, 0.0)
            if ended:
                F.write('child', path, ct['end'], 'replace', 'planned', ok=(life == 'normal_end'), proof='sha' if kind == 'codex' else ('content' if life == 'normal_end' else 'window'),
                        span=(0.0, ct['end']), out=final)
    # ---- the files the scene leaves that nobody's record writes: the report of a launch that fills it, an empty one, one that somebody else wrote, the aux file of the launch
    running = life in ('running', 'stalled_silent')
    if role == 'writer':
        if mine in asked:                                                   # filled by the launch: the harness writes it when the scene says it is there
            if v['fstate'] == 'written':
                F.file(mine, ct['end'] if ended else 0.0, body=REPORT_BODY)
            elif v['fstate'] == 'empty':
                F.file(mine, 0.0, 0)
        elif mine not in F.files:
            if v['fstate'] == 'written':
                F.file(mine, GUIDE_T + 5)                                   # a report that somebody else's hands wrote
            elif v['fstate'] == 'empty':
                F.file(mine, 0.0, 0)
    for path in asked:
        if path != mine and not path.startswith('tmp/') and (kind == 'cli' or not running):
            F.file(path, ct['end'] if ended else 0.0, body='' if running else AUX_BODY)       # `>` makes it at the launch; `-o` writes it at the end
    F.walked = [u for u in units if not any(seg.startswith('.') for seg in u.split('/'))]
    return Scene(F, unit=unit, report=mine, me='child', root=root, final=final)


# ---------------------------------------------------------------------------------------------------------------------
# the room scene (scene_room.Room)
# ---------------------------------------------------------------------------------------------------------------------
def room_scene(v):
    """The facts of the room scene of axis values `v`: sub-agents of one orchestrator that read a guide, message each other, write a file of their own (or code, or a log) and are
    launched together or one by one. The paths are those of the repository `repo` below `work/` (so that a linked worktree beside it can be told from it): `P(rel)`."""
    F = Facts('room')
    n = int(v['people'])
    page = [i for i in range(n) if room_tree(v, i) == 1]
    P = lambda rel: 'repo/' + rel                                                   # noqa: E731
    F.dir('repo/.git')
    folder = room_folder(v)
    written = v['proof'] in ('wrote', 'both') and v['shape'] != 'none'
    # ---- the disk the harness leaves (scene_room.write_disk)
    if v['bundle'] == 'root':
        F.file(P('%s/brief.md' % ROOM_BUNDLE), GUIDE_T)
        if v['copy'] == 'link':
            F.links[P(ROOM_ALIAS)] = P(ROOM_BUNDLE)
    if v['guide'] in ROOM_GUIDES:
        F.file(P(room_disk(v, '%s/%s' % (folder, ROOM_GUIDES[v['guide']]))), GUIDE_T)
    elif v['guide'] in COMMON_DOCS:
        F.file(P(COMMON_DOCS[v['guide']]), GUIDE_T)
    elif v['guide'] == 'own':
        for i in range(n):
            F.file(P(room_guide(v, i)[0]), GUIDE_T)
    elif v['guide'] == 'late':
        F.file(P(room_late_guide(v)), GUIDE_T)
    if v['shape'] == 'r1':
        F.dir(P(room_disk(v, '%s/r1' % folder)))
    if v['above'] != 'none':
        F.file(P('%s/%s' % (ROOM_BUNDLE, ABOVE_DOC[v['above']])), -500.0 if v['above'] == 'early' else 60.0)         # beside the room's folder, written by the harness
    # ---- the participants of the page
    if v['launch'] == 'none':
        launch = None                                                                # the call that started it is not known: no group
    elif v['launch'] == 'far' or v['rtime'] != 'overlap':
        launch = True                                                                # a message of its own (True: the group is the agent's own)
    else:
        launch = 'msg:room'                                                          # one message of the orchestrator started them all
    ids = {i: 'p%d' % (i + 1) for i in range(n)}
    wrote_at = {}
    for k, i in enumerate(page):
        life, off = room_timing(v, k, len(page))
        status = 'running' if life == 'running' else 'done'
        t = off + 6.0
        F.agent(ids[i], status=status, start=off, first=off + 1.0, last=t + 8, launch=launch)
        guide, on_disk = room_guide(v, i)
        out = room_out(v, i)
        if guide and on_disk:
            F.read(ids[i], P(guide), t)
            t += 0.5
        if v['guide'] == 'late':
            F.read(ids[i], P(room_late_guide(v)), t + 0.3)
            t += 1
        if v['talk'] == 'peer':
            same = [j for j in range(n) if room_tree(v, j) == room_tree(v, i)]
            to = same[(same.index(i) + 1) % len(same)] if len(same) > 1 else None
            if to is not None:
                if v['delivery'] != 'failed':
                    F.agents[ids[i]]['sent_to'].append(ids[to])
                t += 0.5
        if out is not None and written:
            F.write(ids[i], P(out), t)
            wrote_at[i] = t
            t += 1
            if room_code(v, i):
                F.write(ids[i], P(room_code(v, i)), t)
                F.file(P(room_code(v, i)), t + 0.5)
                t += 1
        if v['scratch'] == 'tmp':
            for name in ('repro_%s.py', 'draft_%s.md'):
                F.write(ids[i], 'tmp/scratch/' + name % SEAT_LETTERS[i], t)
                t += 1
        elif v['scratch'] == 'log':
            F.write(ids[i], P('logs/%s.txt' % SEAT_LETTERS[i]), t, 'replace', 'shell')
            t += 1
    # ---- the files on disk (the page's own were written at the time of the write; the other orchestrator's participants wrote theirs in their own session)
    if written:
        for i in range(n):
            out = room_out(v, i)
            if out is None:
                continue
            at = wrote_at.get(i, -290.0 + i)
            if v['shape'] == 'scatter':
                F.dir(P('repos/svc_%s/.git' % SEAT_LETTERS[i].lower()))
            F.file(P(room_disk(v, out)), at + 0.5)
            if room_code(v, i) and i not in wrote_at:
                F.file(P(room_code(v, i)), at + 0.5)
    if v['copy'] == 'worktree':
        F.dir('wt')
        F.file('wt/.git', GUIDE_T)
        F.git = {'repo': 'common', 'wt': 'common'}
        F.agent('bystander', status=('running' if v['phase'] == 'working' else 'done'), start=-400.0, first=-399.0, last=-380.0)
        for rel, meta in list(F.files.items()):
            if rel.startswith(P(ROOM_BUNDLE) + '/'):
                F.file('wt/' + rel[len('repo/'):], meta['mtime'])
        F.walked = ['wt/' + room_disk(v, folder)] if v['shape'] == 'r1' else []
    if v['shape'] == 'r1':
        F.walked = F.walked + [P(room_disk(v, folder))]
    F.walked = [u for u in F.walked if not any(seg.startswith('.') for seg in u.split('/'))]        # the walk of the repository goes into no dot folder
    F.top = 'repo/'
    return Scene(F, folder=P(room_disk(v, folder)), ids=ids, page=page, top='repo/')


# ---------------------------------------------------------------------------------------------------------------------
# a participant of a debate folder, started from the shell of a Codex orchestrator (scene_cxo, `topic`)
# ---------------------------------------------------------------------------------------------------------------------
def cxo_report(letter):
    """The report of the participant `letter`, and the last message of a `codex exec` run whose `-o` is beside it (`talk_aux`)."""
    return 'Findings of %s: all is in order.\n' % letter, 'The last message of B.\n'


def cxo_talk_scene(v):
    """The facts of the participant of `talk/` that a shell call of the page's Codex thread started: a `claude -p` run (told its report path, or whose output a shell `>` writes to it) or
    a `codex exec` run (whose `-o` names the report, or a file beside it while the run itself writes the report with a patch). The run is over or still going when the board looks."""
    F = Facts('cxo')
    kind = 'cx' if v['subj'] == 'cx' else 'cl'
    topic = v['topic']
    letter = 'B' if kind == 'cx' else 'A'
    rpath = 'talk/r1/%s.md' % letter
    F.dir('.git')
    F.file('talk/brief.md', GUIDE_T)
    F.dir('talk/r1')
    finished = v['look'] == 'ended'
    length = CODEX_RUN if kind == 'cx' else CLAUDE_RUN
    end = CHILD_START + length if finished else None
    body, aux_body = cxo_report(letter)
    F.agent('child', provider='codex' if kind == 'cx' else 'claude', origin='exec' if kind == 'cx' else 'cli', status='done' if finished else 'running', start=0.0, first=CHILD_START,
            last=end if finished else CHILD_START + 7)
    F.walked = ['talk']
    if kind == 'cl':
        if topic == 'talk_redir':
            F.plan('child', rpath, '>', 0.0)
            F.file(rpath, end if finished else 0.0, body=body if finished else '')                    # the shell made it at the launch; the output lands when the run ends
            if finished:
                F.write('child', rpath, end, 'replace', 'planned', ok=True if cxo_record(v) else None, proof='content', span=(0.0, end), out=body)
        elif finished:
            F.write('child', rpath, CHILD_START + 6.0)                                              # the run was told the path and wrote the report with its Write tool
            F.file(rpath, CHILD_START + 6.5, body=body)
    else:
        o_path = 'talk/r1/B_last.md' if topic == 'talk_aux' else rpath
        F.plan('child', o_path, '-o', 0.0)
        if finished:
            if topic == 'talk_aux':
                F.write('child', rpath, end - 2.0)                                                  # the run's own file, by a patch
                F.file(rpath, end - 1.5, body=body)
            F.write('child', o_path, end, 'replace', 'planned', ok=True, proof='sha', span=(0.0, end), out=aux_body if topic == 'talk_aux' else body)
            F.file(o_path, end, body=aux_body if topic == 'talk_aux' else body)
    return Scene(F, unit='talk', report=rpath, me='child', root=None, final=None)


# ---------------------------------------------------------------------------------------------------------------------
# the contract scene (scene_ctr.Ctr): a debate folder, the participant under test (S), a peer (P), what comes later, the orchestrator's conclusion
# ---------------------------------------------------------------------------------------------------------------------
CTR_R1 = -300.0                  # the Agent / launch calls that start S (and P with it)
CTR_FAR = -700.0                 # P, when it is started far from S
CTR_LATER = -200.0               # what comes after round 1
CTR_RULING = {'early': -260.0, 'late': -120.0}
CTR_NOW = 30.0                   # when the board looks
CTR_UNIT = 'talk'
CTR_RULING_NAME = {'ruling': 'ruling.md', 'verdict_ko': '판정문.md', 'plain': 'notes.md'}      # the conclusion's name by `cname`


def ctr_paths(v):
    """The files of the scene: (S's report, P's report, the guide, the conclusion)."""
    return CTR_UNIT + '/r1/S.md', CTR_UNIT + '/r1/P.md', CTR_UNIT + '/brief.md', CTR_UNIT + '/' + CTR_RULING_NAME[v['cname']]


def ctr_cli(v):
    """The participant under test is a `claude -p` run (it can carry a tag) and not a sub-agent; so is the peer when one call starts both."""
    return v['tag'] != 'none' or v['launch'] == 'call'


def ctr_times(v):
    """The times of the participants: {role: dict(start, tail, ...)}; a sub-agent's Agent call is at `off` and its tail 6 s later, a `claude -p` run's launch call at `off` and its tail
    at 6.6 (scene_sta: the run's first record at off + 2.4, the tail 4.2 s after that)."""
    cli = ctr_cli(v)
    off_s = CTR_R1
    off_p = CTR_FAR if v['launch'] == 'far' else (CTR_R1 if v['launch'] == 'call' else CTR_R1 + 0.01)
    return {'S': dict(off=off_s, tail=off_s + (6.6 if cli else 6.0)), 'P': dict(off=off_p, tail=off_p + (6.6 if v['launch'] == 'call' else 6.0))}


def ctr_scene(v):
    """The facts of the contract scene of axis values `v`. The participant under test S saves its report the way `wmethod` says, reads the guide the way `read` says, may carry a tag, is
    started together with the peer P, far from it, or by a call nobody can tell (`launch`); `later` is a group, one participant or editors after round 1; `final` is the orchestrator's
    conclusion; `life` says whether S is still at work when the board looks."""
    F = Facts('ctr')
    s_path, p_path, guide, ruling = ctr_paths(v)
    cli, wm, life = ctr_cli(v), v['wmethod'], v['life']
    F.dir('.git')
    F.file(guide, GUIDE_T)
    F.dir(CTR_UNIT + '/r1')
    if v['later'] in ('group', 'alone'):
        F.dir(CTR_UNIT + '/r2')                                                         # the next round's folder, where the later participants write
    F.walked = [CTR_UNIT]
    times = ctr_times(v)
    group = {'msg': 'msg:r1', 'call': 'msg:r1', 'far': True, 'none': None}[v['launch']]
    same_call = 'c:r1' if v['launch'] == 'call' else None
    ts, tp = times['S'], times['P']
    # ---- the peer: a report with the Write tool, long over
    F.agent('P', origin='cli' if v['launch'] == 'call' else 'subagent', status='done', start=tp['off'], first=tp['off'] + (2.4 if v['launch'] == 'call' else 1.0), last=tp['tail'] + 5, launch=group,
            call=same_call)
    F.write('P', p_path, tp['tail'])
    F.file(p_path, tp['tail'] + 0.5)
    # ---- the participant under test
    status = 'running' if life == 'running' else 'done'
    t = ts['tail']
    F.agent('S', origin='cli' if cli else 'subagent', status=status, start=ts['off'], first=ts['off'] + (2.4 if cli else 1.0), last=t + (4 if life == 'normal_end' else 1), launch=group, call=same_call)
    a = F.agents['S']
    n_call = [0]

    def window(t0, t1, ok=True, reads=()):
        n_call[0] += 1
        a['windows'].append({'call': 's%d' % n_call[0], 't0': t0, 't1': t1, 'ok': ok, 'reads': list(reads)})

    if v['read'] == 'tool':
        F.read('S', guide, t)
        t += 1.0
    elif v['read'] == 'cat':
        F.read('S', guide, t, 'shell')
        window(t, t + 0.5, True, [guide])
        t += 1.0
    if wm == 'write':
        F.write('S', s_path, t)
        F.file(s_path, t + 0.5)
    elif wm == 'edit':
        F.file(s_path, GUIDE_T + 6)                                                 # the file was there before the run (nobody's record wrote it)
        F.write('S', s_path, t, 'update')
        F.file(s_path, t + 0.5)
    elif wm == 'redirect':
        F.write('S', s_path, t, 'replace', 'shell')
        window(t, t + 0.5)
        F.file(s_path, t + 0.25)
    elif wm == 'window':
        F.write('S', s_path, t, 'replace', 'shell', True, 'window', span=(t, t + 0.5))
        window(t, t + 0.5)
        F.file(s_path, t + 0.25)
    elif wm == 'stale':
        F.file(s_path, GUIDE_T + 6)                                                 # the old file stays: `set -C` refuses to overwrite it
        F.write('S', s_path, t, 'replace', 'shell', True, 'window', span=(t, t + 0.5))
        window(t, t + 0.5)
    elif wm == 'python':
        window(t, t + 0.5)
        F.file(s_path, t + 0.28)                                                    # a look-around call that starts at +0.3 (a `claude -p` run, still at work) is clearly within WIN_EPS: no edge
        t_write = t
    if wm != 'none':
        t += 1.0
    if life == 'running':
        window(ts['tail'] + 0.3 if cli else t + 0.5, None, None)                       # the look-around call whose result never came (a `claude -p` run makes it at 6.9)
    if cli:                                                                          # the launch of a `claude -p` run is a background command of the orchestrator: its window is the run's
        end = None if life == 'running' else ts['off'] + 11.9
        F.orch['windows'].append({'call': (F.agents['S']['launch'] or {}).get('call') or 'launch-S', 't0': ts['off'], 't1': end, 'ok': None if life == 'running' else True, 'reads': []})
    if v['tag'] != 'none':
        F.tag('S', CTR_UNIT, 'r1/S' if v['tag'] == 'seat' else None, 'environ' if life == 'running' else 'command')
    if v['overlap'] == 'agent':
        F.agents['P']['windows'].append({'call': 'p-ls', 't0': t_write + 0.1, 't1': t_write + 0.6, 'ok': True, 'reads': []})
    elif v['overlap'] == 'orch':
        F.orch['windows'].append({'call': 'o-git', 't0': t_write + 0.1, 't1': t_write + 0.6, 'ok': True, 'reads': []})
    # ---- what comes later
    later = v['later']
    if later in ('group', 'alone'):
        names = ['Q1', 'Q2'] if later == 'group' else ['Q']
        for k, role in enumerate(names):
            off = CTR_LATER + 0.01 * k
            F.agent(role, status='done', start=off, first=off + 1.0, last=off + 11, launch='msg:later' if later == 'group' else True)
            F.write(role, '%s/r2/%s.md' % (CTR_UNIT, role), off + 6.0)
            F.file('%s/r2/%s.md' % (CTR_UNIT, role), off + 6.5)
    elif later == 'editors':
        targets = [p_path, s_path if (wm in ('write', 'edit', 'redirect', 'window', 'python', 'stale')) else p_path]
        for k, role in enumerate(['E1', 'E2']):
            off = CTR_LATER + 0.01 * k
            F.agent(role, status='done', start=off, first=off + 1.0, last=off + 11, launch='msg:later')
            F.write(role, targets[k], off + 6.0, 'update')
            F.file(targets[k], off + 6.5)
    # ---- the orchestrator's conclusion
    if v['final'] != 'none':
        at = CTR_RULING[v['final']]
        F.write('orch', ruling, at, orch=True)
        F.file(ruling, at + 0.5)
    return Scene(F, unit=CTR_UNIT, report=s_path, me='S', root=None, roles=[r for r in F.agents])
