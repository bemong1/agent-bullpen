#!/usr/bin/env python3
"""Compares two replays of one frozen HOME (home_replay.py), or a replay with what the live board said at the moment of the capture (the fidelity gate).

    python3 tools/regress/home_diff.py --cap <capture> --a <0.2.1 projection.json> --fidelity                       # the replay gate: replay(0.2.1) against api_live
    python3 tools/regress/home_diff.py --cap <capture> --a <0.2.1.json> --b <0.3.0.json> --truth <truth.json> [--expect <expected.json>] [--b-nonorch <0.3.0 without the orchestrator's writes>.json]

The fidelity gate (CONTRACT 5.3 ②): of all the items of the projection (roots, the current debate, rooms, cells, finals) at most 1% differ, and every one that does is explained (the file given with --explain says why).
The comparison (CONTRACT 5.4) writes report.md, new_confirmed.json and lost.json into the capture folder (or --report-dir) and returns 1 when a line that decides the release is not met:
  1 the cells the tools wrote keep their owner (a cell whose agent has a successful W1 write event on its path: the 0.3.0 owner is that agent; else only `editor_first`, and those are listed to be confirmed one by one; a cell of a folder of files with no round folder that 0.3.0 does not list (`flat_review`) is the accepted exception, O16: counted apart, not a failure)
  2 no new confirmed falsehood: every confirmed item 0.3.0 has and 0.2.1 had not (a cell's owner, a room, a final, a root) is marked true in truth.json; one marked false, or not marked at all, stops the release
  3 what 0.2.1 had and 0.3.0 has not (listed with a reason, `other` explained one by one), 4 against the expected losses written beforehand, 5 the confirmed finals with and without the orchestrator's writes, 6 the rest, 7 time.
truth.json: {"items": [{"session": <id prefix>, "kind": cell|room|final|root|root_final, "key": <fnmatch pattern of the item key>, "owner": <for a cell, optional>, "verdict": "true"|"false", "why": <text>}]}
expected.json: {"items": [{"session": <id prefix>, "reason": <a reason below>, "count": n, "note": <text>}]}
Everything here reads only projections (no board code is imported); the projection of a 0.3.0 run uses the 2.7 field names, and a field a run does not have is not asked for."""
import argparse
import fnmatch
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import home_jail  # noqa: E402
import home_replay  # noqa: E402

FIDELITY_MAX = 0.01
REASONS = ('planned_text', 'role_row', 'flat_review', 'work_units_text', 'w3_hint_only', 'python_only_room', 'final_unconfirmed', 'orch_final_reading', 'cand_files', 'edit_only',
           'estimated_room_not_current', 'unchecked_save', 'held_unsure', 'history_lost', 'demand_unmet', 'estimated_room_final', 'editor_first', 'no_write_event', 'seat_text', 'final_other_file', 'other')
INFO_REASONS = ('previous_gray',)                 # a change of state, not a loss: counted for the expected-losses table


def sid_match(sid, prefix):
    return sid.startswith(prefix)


# ---------- reading ----------
def load_replay(path):
    """({session: projection}, the whole file, [sessions that are left out and why])"""
    d = home_replay.load_json(path)
    try:
        perf = home_replay.load_json(path + '.perf.json')
    except OSError:
        perf = {}
    out, skipped = {}, []
    for sid, row in d['sessions'].items():
        row.update(perf.get(sid) or {})
        if row.get('error') or 'projection' not in row:
            skipped.append((sid, row.get('error') or 'no projection'))
        else:
            out[sid] = row['projection']
    return out, d, skipped


def load_live(cap, only=None):
    """{session: projection} of the live answers kept in the capture (the /api/agent of the agents that stand in a cell are read as they were kept)."""
    out = {}
    base = os.path.join(cap, 'api_live')
    for sid in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if only and sid not in only:
            continue
        sp = os.path.join(base, sid, 'state.json')
        if not os.path.isfile(sp):
            continue
        details = {}
        for n in os.listdir(os.path.join(base, sid)):
            if n.startswith('agent_') and n.endswith('.json'):
                details[n[len('agent_'):-5]] = home_replay.load_json(os.path.join(base, sid, n))
        out[sid] = home_replay.project_state(home_replay.load_json(sp), details)
    return out


def left_out(cap):
    """Sessions the capture says are not to be compared (a record changed under the copy three times)."""
    try:
        return set(home_replay.load_json(os.path.join(cap, 'manifest.json')).get('unstable_sessions') or ())
    except OSError:
        return set()


# ---------- the same place under one name ----------
class Resolver:
    """The real path of a folder or a file as the capture says it (the links of disk.json followed in the original path space; the live disk is never asked). A path the capture does not
    know is left as it is. 0.2.1 names a folder by the link it was reached through and 0.3.0 by the folder itself, so the two projections do not meet until both are written the same way."""

    def __init__(self, cap):
        self.jail = home_jail.Jail(home_jail.load_entries(cap), cap, 'replay')
        self.memo = {}

    def folder(self, path):
        if not isinstance(path, str) or not path.startswith('/'):
            return path
        got = self.memo.get(path)
        if got is None:
            status, final, _ = self.jail.walk(path, True)
            got = self.memo[path] = final if status == 'ok' else os.path.normpath(path)
        return got

    def file(self, path):
        """The folder part resolved, the last name kept (a link that is itself the file is the file)."""
        if not isinstance(path, str) or not path.startswith('/'):
            return path
        d, b = os.path.split(path)
        return os.path.join(self.folder(d), b)


def _split_key(key, parts):
    """The key of a cell (root|topic|rdir|stem), a room or a final (root|topic) or a root, as its path parts and the names behind them."""
    bits = key.split('|')
    return bits[:parts], bits[parts:]


def normalize(p, res, collisions=None):
    """The projection with every path as its real path: roots, topics, cell paths and keys, finals and their candidates, the units of the agents, the paths of their write events. A key that two names of one place
    both took keeps the first (by the old key) and is noted in `collisions`."""
    fold, file_ = res.folder, res.file

    def keyed(d, parts, kind):
        out = {}
        for k in sorted(d):
            head, tail = _split_key(k, parts)
            nk = '|'.join([fold(x) for x in head] + tail)
            if nk in out:
                if collisions is not None:
                    collisions.append({'kind': kind, 'key': nk, 'old': k})
                continue
            out[nk] = d[k]
        return out

    def final(f):
        if not isinstance(f, dict):
            return [file_(x) for x in f] if isinstance(f, list) else f
        f = dict(f)
        if f.get('path'):
            f['real'] = fold(f['path'])                                                       # the file itself, a link at the end followed: a final named through a link is the file it leads to
            f['path'] = file_(f['path'])
        if isinstance(f.get('candidates'), list):
            f['candidates'] = [file_(x) for x in f['candidates']]
        return f

    def uniq(xs, fn):
        return sorted({fn(x) for x in xs})

    out = dict(p)
    out['roots'] = []
    for r in p.get('roots') or []:
        if fold(r) not in out['roots']:
            out['roots'].append(fold(r))
    out['current'] = uniq(p.get('current') or [], fold)
    out['rooms'] = keyed(p.get('rooms') or {}, 2, 'room')
    out['sure'] = {fold(k): v for k, v in (p.get('sure') or {}).items()}
    cells = {}
    for k, c in (p.get('cells') or {}).items():
        c = dict(c)
        if c.get('path'):
            c['path'] = file_(c['path'])
        cells[k] = c
    out['cells'] = keyed(cells, 2, 'cell')
    out['finals'] = keyed({k: final(f) for k, f in (p.get('finals') or {}).items()}, 2, 'final')
    out['root_finals'] = keyed({k: final(f) for k, f in (p.get('root_finals') or {}).items()}, 1, 'root_final')
    agents = {}
    for aid, a in (p.get('agents') or {}).items():
        a = dict(a)
        a['units'], a['work_units'] = uniq(a.get('units') or [], fold), uniq(a.get('work_units') or [], fold)
        if isinstance(a.get('placed'), dict):
            a['placed'] = dict(a['placed'], **{k: fold(a['placed'][k]) for k in ('unit', 'topic') if a['placed'].get(k)})
        det = a.get('detail')
        if isinstance(det, dict):
            det = dict(det)
            for key in ('write_events', 'writes'):
                if isinstance(det.get(key), list):
                    det[key] = [dict(e, **{k: file_(e[k]) for k in ('path', 'short') if isinstance(e.get(k), str) and e[k].startswith('/')}) if isinstance(e, dict) else e for e in det[key]]
            if isinstance(det.get('placed'), dict):
                det['placed'] = dict(det['placed'], **{k: fold(det['placed'][k]) for k in ('unit', 'topic') if det['placed'].get(k)})
            a['detail'] = det
        agents[aid] = a
    out['agents'] = agents
    return out


def normalize_all(projections, res):
    """({session: normalized projection}, [collisions])"""
    out, col = {}, []
    for sid, p in projections.items():
        c = []
        out[sid] = normalize(p, res, c)
        col += [dict(x, session=sid) for x in c]
    return out, col


# ---------- the fidelity gate ----------
def items_of(p):
    """{item id: comparable value} of one projection."""
    out = {}
    for root in p.get('roots') or []:
        out['root|%s' % root] = True
    for root in p.get('current') or []:
        out['current|%s' % root] = True
    for key, r in (p.get('rooms') or {}).items():
        out['room|%s' % key] = [r.get('kind'), r.get('sure'), r.get('members')]
    for key, c in (p.get('cells') or {}).items():
        out['cell|%s' % key] = [c.get('agent'), c.get('state')]
    for key, f in (p.get('finals') or {}).items():
        out['final|%s' % key] = [f.get('exists'), f.get('path')] if f else None
    for root, f in (p.get('root_finals') or {}).items():
        out['root_final|%s' % root] = [f.get('exists'), f.get('path')] if isinstance(f, dict) else f
    return out


def fidelity(replay, live, explain=None, skipped=()):
    """{ratio, total, differing: [...], unexplained: [...], passed} of the replay projections against the live ones, over the sessions both have."""
    explain = explain or {}
    total, rows = 0, []
    per = {}
    for sid in sorted(set(replay) & set(live)):
        a, b = items_of(live[sid]), items_of(replay[sid])
        keys = sorted(set(a) | set(b))
        total += len(keys)
        diff = [k for k in keys if a.get(k) != b.get(k) or (k in a) != (k in b)]
        per[sid] = {'items': len(keys), 'differing': len(diff)}
        for k in diff:
            rows.append({'session': sid, 'item': k, 'live': a.get(k), 'replay': b.get(k), 'in_live': k in a, 'in_replay': k in b, 'why': explain.get('%s|%s' % (sid, k)) or explain.get(k)})
    ratio = (len(rows) / total) if total else 0.0
    unexplained = [r for r in rows if not r['why']]
    return {'ratio': ratio, 'total': total, 'differing': rows, 'unexplained': unexplained, 'per_session': per, 'skipped': [list(s) for s in skipped],
            'only_live': sorted(set(live) - set(replay)), 'only_replay': sorted(set(replay) - set(live)),
            'passed': ratio <= FIDELITY_MAX and not unexplained}


# ---------- the comparison ----------
def same_file(a, b):
    """Whether two finals are one file: by `real` (the link at the end followed, when the paths were normalized), else by the path."""
    if not a or not b:
        return False
    return (a.get('real') or a.get('path')) == (b.get('real') or b.get('path'))


ROUND_DIR = re.compile(r'[A-Za-z]{0,8}\d+')       # r1, round2, 3: a folder the board takes for a round


def w1_events(B, sid, agent, path=None):
    """The successful tool writes (W1) the 0.3.0 projection holds for an agent (its /api/agent write_events), of one path if given."""
    det = ((B.get(sid) or {}).get('agents') or {}).get(agent, {}).get('detail') or {}
    out = [e for e in det.get('write_events') or [] if e.get('evidence') == 'tool' and e.get('ok') is True and e.get('sure', True)]
    return [e for e in out if path is None or e.get('path') == path]


class Context:
    """What the rules know of one session: both projections and the indexes they ask. A cell, a room or a final that 0.3.0 groups under another root than 0.2.1 did (a topic 0.2.1 reached through the
    root above it and 0.3.0 lists as a root of its own) is found by its topic: the grouping is not what is compared, the place is."""

    def __init__(self, sid, a, b):
        self.sid, self.a, self.b = sid, a, b
        self.regrouped = []
        self.b_cells_by_place, self.a_cells_by_place, self.b_topics = {}, {}, set()
        for k in b.get('cells') or {}:
            root, topic, rd, st = k.split('|')
            self.b_cells_by_place.setdefault((topic, rd, st), []).append(k)
            self.b_topics.add(topic)
        for k in a.get('cells') or {}:
            root, topic, rd, st = k.split('|')
            self.a_cells_by_place.setdefault((topic, rd, st), []).append(k)
        for kind in ('rooms', 'finals'):
            for k in b.get(kind) or {}:
                self.b_topics.add(k.split('|')[1])
        self.b_roots = set(b.get('roots') or [])
        self.b_by_topic = {kind: {} for kind in ('rooms', 'finals')}
        for kind in self.b_by_topic:
            for k in b.get(kind) or {}:
                self.b_by_topic[kind].setdefault(k.split('|')[1], []).append(k)
        self.a_by_topic = {'finals': {}}
        for k in a.get('finals') or {}:
            self.a_by_topic['finals'].setdefault(k.split('|')[1], []).append(k)
        self.held = {}
        for d in b.get('diag_items') or []:
            self.held.setdefault((d.get('code'), d.get('unit')), []).append(d.get('detail'))
        self.a_rdirs = {}                                                                   # topic (or root) -> the folders of the cells 0.2.1 had under it
        for k in a.get('cells') or {}:
            root, topic, rd, st = k.split('|')
            for u in {root, topic}:
                self.a_rdirs.setdefault(u, set()).add(rd)
        self.agent_cells = {}
        for k, c in (a.get('cells') or {}).items():
            if c.get('agent'):
                root, topic = k.split('|')[:2]
                self.agent_cells.setdefault((c['agent'], topic), []).append(k)
                if root != topic:
                    self.agent_cells.setdefault((c['agent'], root), []).append(k)

    def events(self, agent, path):
        """Every write event (any kind, sure or not, failed or not) of an agent on a path in the 0.3.0 projection."""
        det = ((self.b.get('agents') or {}).get(agent) or {}).get('detail') or {}
        return [e for e in det.get('write_events') or [] if e.get('path') == path]

    def b_cell(self, key):
        """(key in 0.3.0, cell) of the cell 0.2.1 calls `key`: the same key, else the one cell of that place under another root."""
        c = (self.b.get('cells') or {}).get(key)
        if c is not None:
            return key, c
        root, topic, rd, st = key.split('|')
        found = self.b_cells_by_place.get((topic, rd, st)) or []
        if len(found) == 1:
            self.regrouped.append({'session': self.sid, 'kind': 'cell', 'a': key, 'b': found[0]})
            return found[0], self.b['cells'][found[0]]
        return None, None

    def a_cell(self, key):
        c = (self.a.get('cells') or {}).get(key)
        if c is not None:
            return c
        root, topic, rd, st = key.split('|')
        found = self.a_cells_by_place.get((topic, rd, st)) or []
        return self.a['cells'][found[0]] if len(found) == 1 else None

    def is_held(self, code, topic, rd, st):
        """Whether 0.3.0 says it holds the cell (`code`: history_lost / seat_tie_held); the item names the round and the file ('1/C', '-/C' for no round)."""
        digits = re.findall(r'\d+', rd)
        for detail in self.held.get((code, topic), ()):
            rnd, _, stem = (detail or '').partition('/')
            if stem == st and ((digits and rnd == digits[-1]) or (not rd and rnd in ('-', '1'))):                # `-` is a file with no round folder
                return True
        return False

    def topic_known(self, topic):
        return topic in self.b_topics or topic in self.b_roots

    def flat_loss(self, topic):
        """Whether 0.3.0 lists nothing of a topic that 0.2.1 had only as a folder of files with no round folder (0.3.0 takes only folders with rounds for a topic)."""
        rds = self.a_rdirs.get(topic)
        return not self.topic_known(topic) and bool(rds) and not any(ROUND_DIR.fullmatch(r) for r in rds)

    def a_room_or_final_in_b(self, kind, key):
        """The 0.3.0 key of a room or final: the same key, else the only one of that topic under another root."""
        if key in (self.b.get(kind) or {}):
            return key
        found = self.b_by_topic[kind].get(key.split('|')[1]) or []
        return found[0] if len(found) == 1 else None


def cell_reason(ctx, key, ac, bc, x):
    """(reason, detail) for the cell 0.2.1 gave to x and 0.3.0 does not (bc: the 0.3.0 cell, or None), or None when 0.3.0 gives it to x too. The first rule that holds is the reason."""
    root, topic, rd, st = key.split('|')
    if bc is None:
        if ac.get('planned'):
            return 'planned_text', None
        if ac.get('state') in ('waiting', 'missing'):
            return 'role_row', None
        if not ctx.topic_known(topic) and not ROUND_DIR.fullmatch(rd):
            return 'flat_review', 'no round folder, and 0.3.0 lists nothing of the topic'
        return 'other', 'the cell is not in 0.3.0 at all'
    owner = bc.get('owner')
    if owner == x:
        return None
    hint = bc.get('hint')
    if isinstance(hint, dict) and hint.get('kind') == 'window' and hint.get('agent') == x:
        return 'w3_hint_only', None
    if owner is None:
        events = ctx.events(x, bc.get('path')) if bc.get('path') else []
        sure_writes = [e for e in events if e.get('sure') and e.get('kind') in ('create', 'replace', 'unknown')]       # what makes an owner (J3, D2: an edit or an append alone does not)
        if sure_writes:
            if ctx.is_held('history_lost', topic, rd, st):
                return 'history_lost', None
            if ctx.is_held('seat_tie_held', topic, rd, st):
                return 'held_unsure', None
            return 'other', 'the agent has a sure write on it, nobody owns the cell (state %s) and no diagnostic holds it' % bc.get('state')
        if x in (bc.get('editors') or []):
            return 'edit_only', None
        if any(e.get('evidence') in ('shell', 'planned') and not e.get('sure') for e in events):
            return 'unchecked_save', None
        if bc.get('state') in ('missing', 'waiting') and bc.get('agent') == x:
            return 'demand_unmet', None
        if not events:
            return 'no_write_event', '0.2.1 gave the cell to an agent that has no write on it in 0.3.0 (read, window or text)'
        return 'other', 'owner none, state %s, %d write event(s) of the agent on the path, none sure' % (bc.get('state'), len(events))
    if x in (bc.get('editors') or []) or bc.get('agent') == x:
        return 'editor_first', 'owner %s' % owner
    return 'other', 'owner %s, state %s' % (owner, bc.get('state'))


def contexts(A, B, sessions):
    return {sid: Context(sid, A[sid], B[sid]) for sid in sessions}


def check_tool_writes(A, B, sessions, ctxs=None):
    """Line 1. {baseline, same, editor_first: [...], accepted: [...], violations: [...]}; a row has the reason the rules of line 3 give. `accepted` holds what PLAN 4 and the contract accept (O16):
    the cells of a folder of files with no round folder, which 0.3.0 does not list (`flat_review`). They are counted apart and are not a failure."""
    ctxs = ctxs or contexts(A, B, sessions)
    res = {'baseline': 0, 'same': 0, 'editor_first': [], 'accepted': [], 'violations': []}
    for sid in sessions:
        ctx = ctxs[sid]
        for key, a in sorted((A[sid].get('cells') or {}).items()):
            x, path = a.get('agent'), a.get('path')
            if not x or not path:
                continue
            xs = w1_events(B, sid, x, path)
            if not xs:
                continue
            res['baseline'] += 1
            bkey, b = ctx.b_cell(key)
            owner = b.get('owner') if b else None
            if owner == x:
                res['same'] += 1
                continue
            why = cell_reason(ctx, key, a, b, x)
            row = {'session': sid, 'cell': key, 'a_agent': x, 'b_owner': owner, 'b_state': b.get('state') if b else None, 'b_hint': b.get('hint') if b else None, 'reason': why[0] if why else None, 'detail': why[1] if why else None}
            ys = w1_events(B, sid, owner, path) if owner and owner != 'orch' else []
            if ys and min(e.get('ts') or 0 for e in ys) < min(e.get('ts') or 0 for e in xs):
                res['editor_first'].append(row)
            elif row['reason'] == 'flat_review':
                res['accepted'].append(row)
            else:
                res['violations'].append(row)
    return res


def new_confirmed(A, B, sessions, ctxs=None):
    """Line 2's items: what 0.3.0 confirms and 0.2.1 did not have."""
    ctxs = ctxs or contexts(A, B, sessions)
    out = []
    for sid in sessions:
        a, b, ctx = A[sid], B[sid], ctxs[sid]
        for key, c in sorted((b.get('cells') or {}).items()):
            owner = c.get('owner')
            if owner and (ctx.a_cell(key) or {}).get('agent') != owner:
                out.append({'session': sid, 'kind': 'cell', 'key': key, 'owner': owner})
        a_topics = {k.split('|')[1] for kind in ('rooms', 'finals') for k in a.get(kind) or {}} | {k.split('|')[1] for k in a.get('cells') or {}}
        for key, r in sorted((b.get('rooms') or {}).items()):
            if r.get('sure', True) and key not in (a.get('rooms') or {}) and key.split('|')[1] not in {k.split('|')[1] for k in a.get('rooms') or {}}:
                out.append({'session': sid, 'kind': 'room', 'key': key, 'members': r.get('members')})
        for key, f in sorted((b.get('finals') or {}).items()):
            if f and f.get('confirmed'):
                af = (a.get('finals') or {}).get(key) or next(((a.get('finals') or {})[k] for k in ctx.a_by_topic['finals'].get(key.split('|')[1], [])[:1]), None)
                if not (af and af.get('exists') and same_file(af, f)):
                    out.append({'session': sid, 'kind': 'final', 'key': key, 'path': f.get('path')})
        for root in b.get('roots') or []:
            if root not in (a.get('roots') or []) and (b.get('sure') or {}).get(root) is not False:
                under = [k for k in b.get('cells') or {} if k.split('|')[0] == root]
                out.append({'session': sid, 'kind': 'root', 'key': root, 'known_topic': root in a_topics, 'cells': len(under), 'owners': len({(b['cells'][k].get('owner')) for k in under if b['cells'][k].get('owner')})})
        for root, f in sorted((b.get('root_finals') or {}).items()):
            if isinstance(f, dict) and f.get('confirmed'):
                af = (a.get('root_finals') or {}).get(root)
                if not (isinstance(af, dict) and af.get('exists') and same_file(af, f)) and not (isinstance(af, list) and f.get('path') in af):
                    out.append({'session': sid, 'kind': 'root_final', 'key': root, 'path': f.get('path')})
    return out


def mark(items, truth):
    """Puts the verdict of truth.json on each item: 'true', 'false' or None (not marked). A mark that matches both ways ('false' wins)."""
    rows = (truth or {}).get('items') or []
    for it in items:
        verdicts = []
        for t in rows:
            if t.get('kind') != it['kind'] or not sid_match(it['session'], t.get('session', '')) or not fnmatch.fnmatchcase(it['key'], t.get('key', '*')):
                continue
            if t.get('owner') is not None and t['owner'] != it.get('owner'):
                continue
            verdicts.append((t.get('verdict'), t.get('why')))
        v = 'false' if any(x[0] == 'false' for x in verdicts) else 'true' if verdicts and all(x[0] == 'true' for x in verdicts) else None
        it['verdict'] = v
        it['why'] = next((w for x, w in verdicts if x == v and w), None)
    return items


def lost_items(A, B, sessions, ctxs=None):
    """Line 3: what 0.2.1 had and 0.3.0 has not, each with a reason. The rules read only the 2.7 fields and the diagnostics of the debate judgment (the order matters, the first that holds is the reason);
    a field a run lacks is not asked. What a cell, a room or a final becomes under another root is not lost; it is listed under `regrouped` of the context."""
    ctxs = ctxs or contexts(A, B, sessions)
    out = []

    def add(sid, kind, key, a, b, reason, detail=None):
        out.append({'session': sid, 'kind': kind, 'key': key, 'a': a, 'b': b, 'reason': reason, 'detail': detail})
    for sid in sessions:
        a, b, ctx = A[sid], B[sid], ctxs[sid]
        lost_cells = {}                                                                      # (agent, topic) -> reasons of the cells it lost there
        for key, ac in sorted((a.get('cells') or {}).items()):
            x = ac.get('agent')
            bkey, bc = ctx.b_cell(key)
            if bc is not None and ac.get('state') == 'done' and not x and bc.get('state') == 'previous':
                add(sid, 'cell', key, ac, bc, 'previous_gray')
                continue
            if not x and bc is None and ac.get('state') in ('waiting', 'missing'):
                add(sid, 'cell', key, ac, None, 'role_row', 'a waiting cell 0.2.1 made from a sentence (a role line, a declared participant); 0.3.0 has none')
                continue
            if not x:
                continue
            why = cell_reason(ctx, key, ac, bc, x)
            if why is None:
                continue
            add(sid, 'cell', key, ac, bc, why[0], why[1])
            for u in {key.split('|')[0], key.split('|')[1]}:
                lost_cells.setdefault((x, u), []).append(why[0])
        for key, ar in sorted((a.get('rooms') or {}).items()):
            bkey = ctx.a_room_or_final_in_b('rooms', key)
            br = (b.get('rooms') or {}).get(bkey) if bkey else None
            topic = key.split('|')[1]
            if br is None:
                add(sid, 'room', key, ar, None, 'flat_review' if ctx.flat_loss(topic) else 'other', 'files with no round folder, not listed in 0.3.0' if ctx.flat_loss(topic) else 'the room is not in 0.3.0')
            elif not br.get('sure', True) and ar.get('sure', True):
                add(sid, 'room', key, ar, br, 'estimated_room_not_current')
        for key, af in sorted((a.get('finals') or {}).items()):
            if not af or not af.get('exists'):
                continue
            bkey = ctx.a_room_or_final_in_b('finals', key)
            bf = (b.get('finals') or {}).get(bkey) if bkey else None
            if bf and bf.get('confirmed') and same_file(af, bf):
                continue
            why = list((bf or {}).get('why') or [])
            if bf is None:
                topic = key.split('|')[1]
                add(sid, 'final', key, af, None, 'flat_review' if ctx.flat_loss(topic) else 'other', 'files with no round folder, not listed in 0.3.0' if ctx.flat_loss(topic) else 'the topic has no final in 0.3.0')
            elif 'estimated_room' in why:
                add(sid, 'final', key, af, bf, 'estimated_room_final')
            elif 'history_lost' in why:
                add(sid, 'final', key, af, bf, 'history_lost')
            elif bf.get('by') == 'orch' and not bf.get('confirmed'):
                add(sid, 'final', key, af, bf, 'orch_final_reading')
            elif why:
                add(sid, 'final', key, af, bf, 'final_unconfirmed', why[0])
            else:
                if bf.get('confirmed'):
                    add(sid, 'final', key, af, bf, 'final_other_file', '0.3.0 confirms %s where 0.2.1 had %s' % (os.path.basename(bf.get('path') or ''), os.path.basename(af.get('path') or '')))
                else:
                    add(sid, 'final', key, af, bf, 'other', 'not confirmed and no reason')
        topics_in_a = {k.split('|')[1] for k in a.get('cells') or {}}
        for root in a.get('roots') or []:
            if root not in ctx.b_roots:
                add(sid, 'root', root, True, None, 'flat_review' if ctx.flat_loss(root) else 'other',
                    'a folder of files with no round folder: 0.3.0 does not list it' if ctx.flat_loss(root) else 'the root is not listed in 0.3.0 (its topics are)' if ctx.topic_known(root) else 'the root is not listed in 0.3.0')
        for aid, ag in sorted((a.get('agents') or {}).items()):
            bg = (b.get('agents') or {}).get(aid)
            for u in sorted(set(ag.get('work_units') or []) - set((bg or {}).get('work_units') or [])):
                add(sid, 'work_unit', '%s|%s' % (aid, u), True, None, 'flat_review' if ctx.flat_loss(u) else 'work_units_text')
            for u in sorted(set(ag.get('units') or []) - set((bg or {}).get('units') or [])):
                reasons = lost_cells.get((aid, u))
                if reasons:
                    main = max(set(reasons), key=lambda r: (reasons.count(r), -REASONS.index(r) if r in REASONS else 0))
                    add(sid, 'unit', '%s|%s' % (aid, u), True, None, main, 'the unit of the cells it lost (%s)' % ', '.join('%s %d' % kv for kv in sorted(Counter(reasons).items())))
                elif (aid, u) in ctx.agent_cells:
                    add(sid, 'unit', '%s|%s' % (aid, u), True, None, 'other', 'its cells there are all kept, the unit is not held')
                else:
                    add(sid, 'unit', '%s|%s' % (aid, u), True, None, 'seat_text', '0.2.1 seated the agent there with no cell of its own (a sentence, a declaration)')
    return out


def _topic_of(item):
    """The topic (or root) an item of line 3 is about."""
    if item['kind'] in ('cell', 'room', 'final'):
        return item['key'].split('|')[1]
    if item['kind'] in ('unit', 'work_unit'):
        return item['key'].split('|', 1)[1]
    return item['key']


def expected_check(lost, expect, sessions):
    """Line 4: the actual count per (session prefix, reason) against what was written beforehand (`count_by`: `item`, the default, or `topic`: how many topics). -> {rows: [...], unexpected: [...]}"""
    actual = Counter()
    for it in lost:
        actual[(it['session'], it['reason'])] += 1
    rows, claimed = [], set()
    for e in (expect or {}).get('items') or []:
        mine = [it for it in lost if it['reason'] == e.get('reason') and sid_match(it['session'], e.get('session', ''))]
        n = len({(it['session'], _topic_of(it)) for it in mine}) if e.get('count_by') == 'topic' else len(mine)
        for (sid, r) in list(actual):
            if r == e.get('reason') and sid_match(sid, e.get('session', '')):
                claimed.add((sid, r))
        absent = not any(sid_match(s, e.get('session', '')) for s in sessions)                  # a session that is not in the capture (left out by the size limit) has nothing to compare
        rows.append({'session': e.get('session'), 'reason': e.get('reason'), 'expected': e.get('count'), 'actual': n, 'ok': None if absent else (e.get('count') is None or n == e['count']),
                     'note': 'session not in the capture' if absent else e.get('note'), 'count_by': e.get('count_by') or 'item'})
    unexpected = [{'session': sid, 'reason': r, 'count': c} for (sid, r), c in sorted(actual.items()) if (sid, r) not in claimed and r not in INFO_REASONS]
    return {'rows': rows, 'unexpected': unexpected}


def confirmed_finals(P):
    """The number of topic and root finals that stand: `confirmed` in 0.3.0, `exists` in 0.2.1 (a projection of it has no `confirmed`; D13 keeps exists as an alias)."""
    def stands(f):
        return isinstance(f, dict) and bool(f.get('confirmed') if 'confirmed' in f else f.get('exists'))
    return sum(1 for p in P.values() for f in list((p.get('finals') or {}).values()) + list((p.get('root_finals') or {}).values()) if stands(f))


def perf_rows(ra, rb):
    """Line 7: the first (cold) and second (warm) state() of each session in both runs, and apart from them the time of the /api/agent requests (a run made without them has none)."""
    rows = []
    for sid in sorted(set(ra['sessions']) & set(rb['sessions'])):
        a, b = ra['sessions'][sid], rb['sessions'][sid]
        if a.get('state_cold_sec') is None or b.get('state_cold_sec') is None:
            continue
        rows.append({'session': sid, 'a_cold': a['state_cold_sec'], 'a_warm': a['state_warm_sec'], 'b_cold': b['state_cold_sec'], 'b_warm': b['state_warm_sec'],
                     'a_details': a.get('details_sec'), 'a_details_agents': a.get('details_agents'), 'b_details': b.get('details_sec'), 'b_details_agents': b.get('details_agents')})
    return rows


def compare(A, B, truth=None, expect=None, b_nonorch=None, ra=None, rb=None, excluded=()):
    sessions = [s for s in sorted(set(A) & set(B)) if s not in excluded]
    res = {'sessions': sessions, 'only_a': sorted(set(A) - set(B)), 'only_b': sorted(set(B) - set(A)), 'excluded': sorted(excluded)}
    ctxs = contexts(A, B, sessions)
    res['line1'] = check_tool_writes(A, B, sessions, ctxs)
    new = mark(new_confirmed(A, B, sessions, ctxs), truth)
    res['new_confirmed'] = new
    res['line2'] = {'total': len(new), 'true': sum(1 for x in new if x['verdict'] == 'true'), 'false': [x for x in new if x['verdict'] == 'false'], 'unmarked': [x for x in new if x['verdict'] is None]}
    lost = lost_items(A, B, sessions, ctxs)
    res['lost'] = lost
    res['regrouped'] = [r for c in ctxs.values() for r in c.regrouped]
    res['line3'] = {'by_reason': dict(Counter(x['reason'] for x in lost)), 'by_session': {s: dict(Counter(x['reason'] for x in lost if x['session'] == s)) for s in sessions},
                    'other': [x for x in lost if x['reason'] == 'other']}
    res['line4'] = expected_check(lost, expect, sessions)
    res['line5'] = {'with_orch': confirmed_finals({s: B[s] for s in sessions}), 'without_orch': confirmed_finals({s: b_nonorch[s] for s in sessions if s in b_nonorch}) if b_nonorch is not None else None,
                    'in_0_2_1': confirmed_finals({s: A[s] for s in sessions})}
    res['line6'] = {'other': len(res['line3']['other']), 'diag_a': {s: A[s].get('diag_n') for s in sessions}, 'diag_b': {s: B[s].get('diag_n') for s in sessions}}
    res['line7'] = perf_rows(ra, rb) if ra and rb else []
    res['passed'] = {'line1': not res['line1']['violations'], 'line2': not res['line2']['false'] and not res['line2']['unmarked']}
    return res


# ---------- the report ----------
def render_fidelity(res):
    out = ['# Replay fidelity (replay of 0.2.1 against the live answers kept in the capture)', '',
           'items %d, differing %d (%.2f%%; limit %.0f%%), unexplained %d: **%s**' % (res['total'], len(res['differing']), res['ratio'] * 100, FIDELITY_MAX * 100, len(res['unexplained']), 'PASS' if res['passed'] else 'NOT MET'), '']
    if res['skipped']:
        out += ['Left out of the comparison: ' + ', '.join('%s (%s)' % (s[:8], w) for s, w in res['skipped']), '']
    out += ['| session | items | differing |', '|---|---|---|'] + ['| %s | %d | %d |' % (s[:8], v['items'], v['differing']) for s, v in sorted(res['per_session'].items())] + ['']
    if res['differing']:
        out += ['## Differences', '', '| session | item | live | replay | why |', '|---|---|---|---|---|']
        for r in res['differing']:
            out.append('| %s | `%s` | `%s` | `%s` | %s |' % (r['session'][:8], r['item'], json.dumps(r['live'], ensure_ascii=False)[:120] if r['in_live'] else '-', json.dumps(r['replay'], ensure_ascii=False)[:120] if r['in_replay'] else '-', r['why'] or '**unexplained**'))
    return '\n'.join(out) + '\n'


def _short(x):
    return x.replace(os.path.expanduser('~'), '~') if isinstance(x, str) else x


def _brief(v):
    """One cell of a table for a value of an item: agent/owner and state of a cell, else a cut of its JSON."""
    if v is None:
        return '-'
    if isinstance(v, dict) and ('state' in v or 'owner' in v or 'agent' in v):
        bits = [str(v.get('agent') or '-')[:12]] if 'owner' not in v else ['owner %s' % str(v.get('owner') or '-')[:12], 'agent %s' % str(v.get('agent') or '-')[:12]]
        bits.append(str(v.get('state')))
        if v.get('editors'):
            bits.append('editors %d' % len(v['editors']))
        if v.get('hint'):
            bits.append('hint')
        return ', '.join(bits)
    return _short(json.dumps(v, ensure_ascii=False))[:70]


def render_report(res):
    l1, l2, l3, l4, l5, l6 = (res[k] for k in ('line1', 'line2', 'line3', 'line4', 'line5', 'line6'))
    out = ['# 0.2.1 / 0.3.0 over the frozen HOME', '', 'sessions compared: %d%s' % (len(res['sessions']), (' · left out (unstable capture): ' + ', '.join(s[:8] for s in res['excluded'])) if res['excluded'] else ''), '',
           '| line | result |', '|---|---|',
           '| 1 cells the tools wrote keep their owner | %s: baseline %d, same %d, editor_first %d (to confirm one by one), accepted flat_review (O16) %d, violations %d |' % ('PASS' if res['passed']['line1'] else 'FAIL', l1['baseline'], l1['same'], len(l1['editor_first']), len(l1['accepted']), len(l1['violations'])),
           '| 2 no new confirmed falsehood | %s: new confirmed %d, true %d, false %d, unmarked %d |' % ('PASS' if res['passed']['line2'] else 'FAIL', l2['total'], l2['true'], len(l2['false']), len(l2['unmarked'])),
           '| 3 lost (not a gate) | %s |' % (', '.join('%s %d' % kv for kv in sorted(l3['by_reason'].items())) or 'none'),
           '| 4 against the expected losses | %d rows, %d off, %d unexpected reason(s) |' % (len(l4['rows']), sum(1 for r in l4['rows'] if r['ok'] is False), len(l4['unexpected'])),
           '| 5 confirmed finals | 0.2.1: %s, 0.3.0 with the orchestrator\'s writes: %s, without: %s |' % (l5['in_0_2_1'], l5['with_orch'], l5['without_orch'] if l5['without_orch'] is not None else 'not given'),
           '| 6 the rest (report only) | other %d |' % l6['other'], '']
    norm = res.get('normalized') or {}
    out += ['Paths are written as the real path the capture gives (a folder 0.2.1 reached through a link and 0.3.0 by its own name is one place)%s; %d cell(s) are met under another root in 0.3.0 and compared there (regrouped).' % (
        '' if norm.get('real_paths') else ' — NOT in this run (--raw-paths)', len(res.get('regrouped') or [])), '']
    if norm.get('collisions'):
        out += ['%d key(s) that two names of one place both took were merged (first kept): %s' % (len(norm['collisions']), ', '.join('%s %s' % (c['kind'], _short(c['key'])[-50:]) for c in norm['collisions'][:6])), '']
    if l1['violations'] or l1['editor_first'] or l1['accepted']:
        out += ['## Line 1: cells whose owner changed', '', '| kind | session | cell | 0.2.1 agent | 0.3.0 owner | state | hint | reason |', '|---|---|---|---|---|---|---|---|']
        for kind, rows in (('VIOLATION', l1['violations']), ('editor_first', l1['editor_first']), ('accepted (O16)', l1['accepted'])):
            out += ['| %s | %s | `%s` | %s | %s | %s | %s | %s |' % (kind, r['session'][:8], _short(r['cell']), r['a_agent'], r['b_owner'], r['b_state'], json.dumps(r['b_hint']), r.get('reason') or '') for r in rows]
        out += ['', 'by reason: ' + (', '.join('%s %d' % kv for kv in sorted(Counter(r.get('reason') for r in l1['violations']).items(), key=lambda kv: -kv[1])) or 'none'), '']
    if l2['false'] or l2['unmarked']:
        out += ['## Line 2: confirmed items that are not marked true', '']
        for kind, rows in (('FALSE', l2['false']), ('unmarked (stops the release)', l2['unmarked'])):
            out += ['- %s `%s` %s %s' % (kind, r['kind'], r['session'][:8], _short(r['key']) + (' → ' + str(r['owner']) if r.get('owner') else '')
                    + (' (0.3.0 has %d cell(s) with %d owner(s) under it%s)' % (r['cells'], r['owners'], ', and 0.2.1 had it as a topic' if r.get('known_topic') else '') if r['kind'] == 'root' else '')) for r in rows]
        out.append('')
    out += ['## Line 3: lost, by session and reason', '', '| session | ' + ' | '.join(REASONS + INFO_REASONS) + ' |', '|---|' + '---|' * (len(REASONS) + len(INFO_REASONS))]
    for s, d in sorted(l3['by_session'].items()):
        out.append('| %s | ' % s[:8] + ' | '.join(str(d.get(r, '')) for r in REASONS + INFO_REASONS) + ' |')
    out += ['']
    if l3['other']:
        out += ['### `other`, to explain one by one', '', '| session | kind | item | 0.2.1 | 0.3.0 | note |', '|---|---|---|---|---|---|']
        out += ['| %s | %s | `%s` | %s | %s | %s |' % (x['session'][:8], x['kind'], _short(x['key'])[-120:], _brief(x['a']), _brief(x['b']), x['detail'] or '') for x in l3['other']] + ['']
    if l4['rows'] or l4['unexpected']:
        out += ['## Line 4: expected losses', '', '| session | reason | counted by | expected | actual | |', '|---|---|---|---|---|---|'] + ['| %s | %s | %s | %s | %s | %s |' % (r['session'], r['reason'], r.get('count_by', 'item'), r['expected'], r['actual'], 'not in the capture' if r['ok'] is None else 'ok' if r['ok'] else 'OFF') for r in l4['rows']]
        out += [''] + ['- unexpected loss: %s %s ×%d' % (u['session'][:8], u['reason'], u['count']) for u in l4['unexpected']] + ['']
    if res['line7']:
        det = lambda sec, n: ('%.3f (%d agents)' % (sec, n or 0)) if sec is not None else '-'      # noqa: E731
        out += ['## Line 7: time of the first (cold) and second (warm) state(), seconds', '',
                'The judgment alone, session after session. The /api/agent requests are made after all of them and timed apart (last two columns), so what they warm is not in these.', '',
                '| session | 0.2.1 cold | 0.2.1 warm | 0.3.0 cold | 0.3.0 warm | 0.2.1 /api/agent | 0.3.0 /api/agent |', '|---|---|---|---|---|---|---|']
        out += ['| %s | %.3f | %.3f | %.3f | %.3f | %s | %s |' % (r['session'][:8], r['a_cold'], r['a_warm'], r['b_cold'], r['b_warm'], det(r['a_details'], r['a_details_agents']), det(r['b_details'], r['b_details_agents'])) for r in res['line7']] + ['']
    return '\n'.join(out) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description='Compare replays of a frozen HOME.')
    ap.add_argument('--cap', required=True, help='the capture folder')
    ap.add_argument('--a', required=True, help='the projection JSON of the 0.2.1 replay')
    ap.add_argument('--b', help='the projection JSON of the 0.3.0 replay')
    ap.add_argument('--b-nonorch', help='the 0.3.0 projection with the orchestrator\'s writes not counted (D1)')
    ap.add_argument('--truth', help='truth.json')
    ap.add_argument('--expect', help='expected.json')
    ap.add_argument('--fidelity', action='store_true', help='the replay gate: --a against the live answers of the capture')
    ap.add_argument('--explain', help='fidelity: a JSON {"<session>|<item>": "why"}')
    ap.add_argument('--report-dir', help='default: the capture folder')
    ap.add_argument('--raw-paths', action='store_true', help='compare the paths as the projections wrote them (default: both written as the real path the capture gives)')
    a = ap.parse_args(argv)
    out_dir = a.report_dir or a.cap
    A, ra, skipped_a = load_replay(a.a)
    if a.fidelity:
        explain = home_replay.load_json(a.explain) if a.explain else {}
        res = fidelity(A, load_live(a.cap, set(A)), explain, skipped_a + [(s, 'unstable capture') for s in sorted(left_out(a.cap)) if s in A])
        for s in left_out(a.cap):
            res['per_session'].pop(s, None)
        home_replay.dump_json(os.path.join(out_dir, 'fidelity.json'), res)
        with open(os.path.join(out_dir, 'fidelity.md'), 'w', encoding='utf-8') as f:
            f.write(render_fidelity(res))
        print('fidelity: %d items, %d differing (%.2f%%), %d unexplained: %s' % (res['total'], len(res['differing']), res['ratio'] * 100, len(res['unexplained']), 'PASS' if res['passed'] else 'NOT MET'))
        return 0 if res['passed'] else 1
    if not a.b:
        ap.error('--b is required (or --fidelity)')
    B, rb, skipped_b = load_replay(a.b)
    nonorch = load_replay(a.b_nonorch)[0] if a.b_nonorch else None
    resolver = None if a.raw_paths else Resolver(a.cap)
    collisions = []
    if resolver is not None:
        (A, c1), (B, c2) = normalize_all(A, resolver), normalize_all(B, resolver)
        nonorch, c3 = normalize_all(nonorch, resolver) if nonorch is not None else (None, [])
        collisions = c1 + c2 + c3
    res = compare(A, B, home_replay.load_json(a.truth) if a.truth else None, home_replay.load_json(a.expect) if a.expect else None, nonorch, ra, rb, left_out(a.cap))
    res['normalized'] = {'real_paths': resolver is not None, 'collisions': collisions}
    home_replay.dump_json(os.path.join(out_dir, 'new_confirmed.json'), res['new_confirmed'])
    home_replay.dump_json(os.path.join(out_dir, 'lost.json'), res['lost'])
    with open(os.path.join(out_dir, 'report.md'), 'w', encoding='utf-8') as f:
        f.write(render_report(res))
    print('line 1 %s, line 2 %s; lost %d, new confirmed %d (report.md in %s)' % ('PASS' if res['passed']['line1'] else 'FAIL', 'PASS' if res['passed']['line2'] else 'FAIL', len(res['lost']), len(res['new_confirmed']), out_dir))
    return 0 if all(res['passed'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
