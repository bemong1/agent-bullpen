#!/usr/bin/env python3
"""Runs one edition of the board (0.2.1 or 0.3.0) over a frozen HOME (home_freeze.py) inside the jail (home_jail.py): one process, one pass, the capture as the only disk.

    python3.12 tools/regress/home_replay.py --code <board code folder> --cap <capture> --out <projection.json> [--trace --touched <json>]

The result is the projection of every captured session (what home_diff.py compares): the roots, the current debate, the rooms, the cells with their owners, what each agent is placed in, the finals, the diagnostics count;
next to it `<out>.jail_miss.json` (every disk lookup the capture did not know; empty is the first condition of a faithful replay) and `<out>.raw/<session>.json` (the whole state, for explaining a difference).
Python 3.11 or later (3.12 is what the contract names): since 3.11 pathlib reaches the disk through os.* when it is called, so the jail's wrappers are in its way.

What is fixed, so that two runs give the same bytes: time.time = the moment of the capture (the 0.2.1 judgments read the clock; the 0.3.0 ones do not), each agent's status = what the live board said then (proc.json), the process table is empty
(the environment tags of live processes come from proc.json), the board's own repository is where the live board ran (units.own_top, so it is left out of the hints as it was), the repository walk is one synchronous pass, nothing runs in the background,
HOME is the original value (the jail answers for it), XDG_CACHE_HOME is a fresh copy of the captured link cache for each run."""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import home_jail  # noqa: E402

PROJECTION_VERSION = 1
CELL_KEYS = ('path', 'agent', 'owner', 'editors', 'evidence', 'hint', 'state', 'previous', 'planned')       # 0.2.1 has agent/state/planned; 0.3.0 the others too (CONTRACT 2.6, 2.7)
DETAIL_KEYS = ('write_events', 'writes', 'run', 'placed', 'launch', 'room_tag')        # `room_tag` (BULLPEN_ROOM/SEAT, O16), not `tag`: that is the letter the screen names the agent by


# ---------- the projection of one /api/state (pure; the same function reads the live answer and the replayed one) ----------
def _stem(path):
    b = os.path.basename(path or '')
    return b[:-3] if b.endswith('.md') else b


def _rdir(path, topic_dir):
    if not path:
        return ''
    d = os.path.dirname(path)
    if topic_dir and d.rstrip('/') == topic_dir.rstrip('/'):
        return ''
    return os.path.basename(d)


def _final(f):
    if not isinstance(f, dict):
        return None
    out = {'path': f.get('path'), 'exists': bool(f.get('exists')) if 'exists' in f else None}
    if 'confirmed' in f:
        out['confirmed'] = bool(f.get('confirmed'))
    for k in ('why', 'candidates'):
        if k in f:
            out[k] = [x.get('path') if isinstance(x, dict) else x for x in f.get(k) or []] if k == 'candidates' else list(f.get(k) or [])
    for k in ('by', 'scope'):
        if k in f:
            out[k] = f.get(k)
    return out


def project_state(state, details=None, diag_items=None):
    """The projection of one session: a dict of plain values, keys sorted when written. `details` = {agent id: /api/agent answer} (only what DETAIL_KEYS names is kept)."""
    debates = state.get('debates') or []
    out = {'roots': [d.get('root') for d in debates], 'current': [d.get('root') for d in debates if d.get('current')], 'rooms': {}, 'cells': {}, 'finals': {}, 'root_finals': {},
           'sure': {d.get('root'): d.get('sure') for d in debates if 'sure' in d}, 'agents': {}, 'diag_n': (state.get('diag') or {}).get('n'),
           'diag_codes': (state.get('diag') or {}).get('by_code') or {}, 'diag_items': list(diag_items or [])}
    for d in debates:
        root = d.get('root')
        if 'final' in d:
            out['root_finals'][root] = _final(d.get('final'))
        elif d.get('finals') is not None:
            out['root_finals'][root] = sorted(f.get('path') or '' for f in d.get('finals') or [] if isinstance(f, dict))
        for t in d.get('topics') or []:
            topic = t.get('dir') or t.get('key')
            key = '%s|%s' % (root, topic)
            if t.get('room'):
                members = sorted({r.get('p') for r in t.get('rows') or [] if r.get('p')})
                out['rooms'][key] = {'kind': t.get('room'), 'sure': t.get('room_sure', True), 'members': members}
            out['finals'][key] = _final(t.get('final'))
            for r in t.get('rows') or []:
                for c in r.get('cells') or []:
                    rd = _rdir(c.get('path'), t.get('dir')) or ('r%s' % c['round'] if not c.get('path') and c.get('round') is not None else '')       # a cell with no path (waiting) is told by its round
                    ck = '%s|%s|%s' % (key, rd, _stem(c.get('path')) or r.get('p'))
                    out['cells'][ck] = {k: c[k] for k in CELL_KEYS if k in c}
    for a in state.get('agents') or []:
        row = {'placed': a.get('placed'), 'units': sorted(a.get('units') or []), 'work_units': sorted(a.get('work_units') or []), 'status': a.get('status'), 'room_tag': a.get('room_tag')}
        det = (details or {}).get(a.get('id'))
        if det:
            row['detail'] = {k: det[k] for k in DETAIL_KEYS if k in det}
        out['agents'][a.get('id')] = row
    return out


def cell_agent_ids(state):
    """The agents that stand in a cell (the ones /api/agent is read for, in the capture and in the replay)."""
    ids = set()
    for d in state.get('debates') or []:
        for t in d.get('topics') or []:
            for r in t.get('rows') or []:
                for c in r.get('cells') or []:
                    for k in ('agent', 'owner', 'writer'):
                        if isinstance(c.get(k), str):
                            ids.add(c[k])
                    ids.update(x for x in c.get('editors') or [] if isinstance(x, str))
    return ids


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def dump_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, sort_keys=True, ensure_ascii=False, indent=1, default=str)
        f.write('\n')


# ---------- the board, patched ----------
def fix_board(manifest, proc, board_top):
    """Makes the imported board deterministic over the capture (see the module doc). Every name is looked for and skipped when it is not there, so that a later edition of the board that moved one is still run."""
    from board import procs, sessions, units
    from board import runstate as RS
    from board import catalog
    sessions.WALK_ENABLED = False
    catalog.Registry._warm = staticmethod(lambda s: None)               # a background state() would race the replay
    if hasattr(units, 'own_top'):
        units.own_top = lambda: board_top
    empty_table = lambda *a, **k: {}                                    # noqa: E731
    for name, val in (('has_proc', lambda: False), ('table_known', lambda: True), ('_ps_table', lambda: {}), ('_ps_pairs', empty_table), ('_run_ps', lambda: None)):       # the rest (codex_procs, ancestors, uid ...) is built on these
        if hasattr(procs, name):
            setattr(procs, name, val)
    env_by_pid = {int(e['pid']): e.get('env') or {} for e in proc.get('env') or [] if e.get('pid')}

    def env_values(pid, names):
        if pid not in env_by_pid:
            return None
        want = {n.decode() if isinstance(n, bytes) else n for n in names}
        return {k: v for k, v in env_by_pid[pid].items() if k in want}
    if hasattr(procs, 'env_values'):
        procs.env_values = env_values
    recorded = proc.get('agents') or {}
    orig = getattr(sessions.Session, 'agent_verdict', None)

    def agent_verdict(self, a, alive, now, _seen=None):
        r = (recorded.get(self.id) or {}).get(a.id)
        if r is None:
            return orig(self, a, alive, now, _seen)
        return RS.Verdict(r.get('status'), r.get('reason'), r.get('resets_at'))
    if orig is not None:
        sessions.Session.agent_verdict = agent_verdict
    return sessions


def fresh_cache(src, dst):
    """A fresh copy of the captured link cache for one run, as private as the live board's own: it only reads a cache folder and file that are the user's and that nobody else can write
    (lineage._cache_trusted), and a copy of a capture made with a umask of 002 is group-writable."""
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        os.makedirs(dst)
    os.chmod(dst, 0o700)
    for dirpath, dirs, names in os.walk(dst):
        for d in dirs:
            os.chmod(os.path.join(dirpath, d), 0o700)
        for n in names:
            os.chmod(os.path.join(dirpath, n), 0o600)


def run(code, cap, out_json, mode='replay', touched_out=None, raw=True, walk_budget=30.0, only=None, details_mode='all', debug=False):
    if sys.version_info < (3, 11):
        raise SystemExit('home_replay needs Python 3.11 or later (3.12 is what the contract names): pathlib reaches the disk through os.* only from 3.11 on')
    code, cap, out_json = (os.path.abspath(p) for p in (code, cap, out_json))
    manifest, proc = load_json(os.path.join(cap, 'manifest.json')), load_json(os.path.join(cap, 'proc.json'))
    T = float(manifest['T'])
    run_dir = out_json + '.run'
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(os.path.join(run_dir, 'tmp'))
    fresh_cache(os.path.join(cap, 'cache'), os.path.join(run_dir, 'cache'))
    os.environ.update(HOME=manifest['home'], XDG_CACHE_HOME=os.path.join(run_dir, 'cache'), TMPDIR=os.path.join(run_dir, 'tmp'), PYTHONDONTWRITEBYTECODE='1')
    for key, flag in (('CLAUDE_CONFIG_DIR', 'claude_home'), ('CODEX_HOME', 'codex_home')):
        os.environ.pop(key, None)
        if manifest.get(flag) and manifest[flag] != os.path.join(manifest['home'], '.claude' if key.startswith('CLAUDE') else '.codex'):
            os.environ[key] = manifest[flag]
    sys.dont_write_bytecode = True
    claude_projects, codex_sessions = os.path.join(manifest['claude_home'], 'projects'), os.path.join(manifest['codex_home'], 'sessions')
    raw_dir = out_json + '.raw'
    jail = home_jail.jail_for(cap, mode, code, [run_dir, raw_dir], extra_pass=('/dev/null', '/dev/urandom', '/dev/zero'), record_roots=(claude_projects, codex_sessions))
    sessions_out, board_version, perf = {}, None, {}
    real_time = time.time
    t_start = time.perf_counter()
    log = lambda msg: print('[%5.1fs] %s' % (time.perf_counter() - t_start, msg), file=sys.stderr, flush=True)       # noqa: E731
    try:
        jail.install()
        time.time = lambda: T
        sys.path.insert(0, code)
        for m in [m for m in sys.modules if m == 'board' or m.startswith('board.')]:
            del sys.modules[m]
        import board                                                                     # noqa: F401
        if not os.path.abspath(board.__file__).startswith(code + os.sep):
            raise SystemExit('the board was imported from %s, not from the code folder %s' % (board.__file__, code))
        log('board imported')
        sess_mod = fix_board(manifest, proc, manifest.get('board_top'))
        from board.catalog import REG
        from board.link import LINKS
        LINKS.deep_inline = True
        if debug:                                                                       # the scan keeps going past a stage that fails and says only its type: say the whole of it
            import traceback

            def guard(fn):
                try:
                    fn()
                except Exception:                                                       # noqa: BLE001
                    traceback.print_exc()
            LINKS._guard = guard
        from board.lineage import LINK_CACHE
        LINKS.lineage.enable_cache(LINK_CACHE)
        t_scan = time.perf_counter()
        LINKS.scan()
        log('link scan %.1f s' % (time.perf_counter() - t_scan))
        if raw:
            os.makedirs(raw_dir, exist_ok=True)
        held = []
        for item in manifest['sessions']:                                                # first pass: what the judgment costs, session after session, with nothing else in between
            sid = item['id']
            if only and sid not in only:
                continue
            row = sessions_out[sid] = {'error': None}
            try:
                t_get = time.perf_counter()
                s = REG.get(sid)
                if s is None:
                    raise LookupError('the board did not open the session')
                t0 = time.perf_counter()
                s.walk_repos(walk_budget)
                t1 = time.perf_counter()
                st = s.state()
                t2 = time.perf_counter()
                s.state()
                t3 = time.perf_counter()
                perf[sid] = {'open_sec': round(t0 - t_get, 4), 'walk_sec': round(t1 - t0, 4), 'state_cold_sec': round(t2 - t1, 4), 'state_warm_sec': round(t3 - t2, 4)}
                log('%s: %d agents, state %.2f s' % (sid[:8], len(st.get('agents') or []), t2 - t1))
                held.append((sid, s, st, row))
            except Exception as e:                                                       # noqa: BLE001 — a session that cannot be replayed is reported, the others go on
                row['error'] = '%s: %s' % (type(e).__name__, e)
        for sid, s, st, row in held:                                                     # second pass: the answers of /api/agent, timed apart, so that what they warm is not in the first pass
            try:
                details, t0 = {}, time.perf_counter()
                if details_mode != 'none':
                    for aid in sorted(a['id'] for a in st.get('agents') or [] if a.get('id')) if details_mode == 'all' else sorted(cell_agent_ids(st)):
                        try:
                            details[aid] = s.agent_detail(aid)
                        except Exception as e:                                           # noqa: BLE001 — one agent's detail must not stop the replay
                            details[aid] = {'error': type(e).__name__}
                perf[sid].update(details_sec=round(time.perf_counter() - t0, 4), details_agents=len(details))
                items = sorted(({k: d.get(k) for k in ('code', 'agent', 'unit', 'detail')} for d in getattr(s, 'debate_diag', None) or () if isinstance(d, dict)),
                               key=lambda d: tuple(str(d[k]) for k in ('code', 'agent', 'unit', 'detail')))      # what the debate judgment held or could not settle, item by item (the state carries only the counts)
                row.update(projection=project_state(st, details, items))
                if raw:
                    dump_json(os.path.join(raw_dir, sid + '.json'), {'state': st, 'details': details})
            except Exception as e:                                                       # noqa: BLE001
                row['error'] = '%s: %s' % (type(e).__name__, e)
        log('details %s: %.1f s' % (details_mode, sum(v.get('details_sec', 0) for v in perf.values())))
        board_version = getattr(board, '__version__', None)
    finally:
        time.time = real_time
        jail.uninstall()
        if sys.path and sys.path[0] == code:
            sys.path.pop(0)
    result = {'version': PROJECTION_VERSION, 'mode': mode, 'board_version': board_version, 'python': '%d.%d.%d' % sys.version_info[:3], 'T': T, 'cap': os.path.basename(cap),
              'sessions': sessions_out, 'jail_miss_count': jail.miss_total()}
    dump_json(out_json, result)
    dump_json(out_json + '.jail_miss.json', jail.miss_list())
    dump_json(out_json + '.perf.json', perf)             # the times are not part of the projection (two runs must agree byte for byte)
    if touched_out:
        dump_json(touched_out, {p: {'funcs': sorted(t['funcs']), 'read': t['read'], 'listed': t['listed']} for p, t in sorted(jail.touched.items())})
    shutil.rmtree(run_dir, ignore_errors=True)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description='Replay one board edition over a frozen HOME inside the jail.')
    ap.add_argument('--code', required=True, help='the board code folder (the one with board/ in it)')
    ap.add_argument('--cap', required=True, help='the capture folder (home_freeze.py)')
    ap.add_argument('--out', required=True, help='the projection JSON to write (next to it: .jail_miss.json and the .raw folder)')
    ap.add_argument('--trace', action='store_true', help='trace mode (home_freeze.py uses it): what the capture does not hold is read from the live disk and noted in --touched')
    ap.add_argument('--touched', help='trace mode: where to write the paths that were read from the live disk')
    ap.add_argument('--no-raw', action='store_true', help='do not keep the whole state of each session')
    ap.add_argument('--walk-budget', type=float, default=30.0, help='seconds the repository walk may take (the live board has 1.5; here the walk is bounded by its counts alone, so that two runs agree)')
    ap.add_argument('--session', action='append', help='only this session (repeatable)')
    ap.add_argument('--debug', action='store_true', help='print the traceback of a link-scan stage that fails')
    ap.add_argument('--details', choices=('all', 'cells', 'none'), default='all', help='/api/agent is read for every agent (default; the 0.3.0 comparison needs the write events of the agents it names), only for the agents that stand in a cell, or not at all (a run to time the judgment). The requests are made after every session has been judged and timed apart (`details_sec` of .perf.json)')
    a = ap.parse_args(argv)
    res = run(a.code, a.cap, a.out, 'trace' if a.trace else 'replay', a.touched, not a.no_raw, a.walk_budget, set(a.session or ()), a.details, a.debug)
    errs = {k: v['error'] for k, v in res['sessions'].items() if v['error']}
    print('replayed %d session(s), jail_miss %d%s' % (len(res['sessions']), res['jail_miss_count'], ', errors: %s' % errs if errs else ''))
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
