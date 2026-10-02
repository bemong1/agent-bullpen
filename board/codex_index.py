"""The list of rollouts in ~/.codex/sessions (CodexIndex, singleton CODEX): re-looks only at changed files and reads only what it needs, going by line heads."""

import copy
import glob
import json
import os
import threading
import time

from .util import CODEX_NAMES, CODEX_SESSIONS, line_error, parse_ts, trunc
from .codex_parse import CX_BIG, CX_HEAD_RE, CX_LINE_MAX, codex_user_text


CX_TAIL = 4 << 20          # for the list: the first line + the last 4 MB

CX_ORIGIN = {'codex-tui': 'tui', 'Codex Desktop': 'desktop', 'codex_exec': 'exec'}


def _snapshot(e):
    """A copy of one entry: the inner mutable values (turns, thread_total, limit) are copied too, so that readers and the scan do not affect each other."""
    c = dict(e)
    for k in ('turns', 'thread_total', 'limit'):
        if e.get(k) is not None:
            c[k] = copy.deepcopy(e[k])
    return c


class CodexIndex:
    """The list of rollouts in ~/.codex/sessions. Only files whose (size, mtime) changed are looked at again.
    Under 64 MB it reads on from where it left off (filters by line head, JSON only for the needed lines); at or over that, only the first line + the last 4 MB."""

    def __init__(self):
        self.lock = threading.RLock()
        self.files = {}
        self.by_id = {}
        self.version = 0
        self._last = 0
        self._names = (None, {})

    def refresh(self, force=False):
        with self.lock:
            if not force and time.time() - self._last < 5:
                return
            self._last = time.time()
            changed, seen = False, set()
            for p in glob.glob(os.path.join(CODEX_SESSIONS, '*', '*', '*', 'rollout-*.jsonl')):
                seen.add(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                e = self.files.get(p)
                if e and e['size'] == st.st_size and e['mtime'] == st.st_mtime:
                    continue
                if e is None or st.st_size < e['size']:
                    e = self._new(p)
                    if not e:
                        continue
                    self.files[p] = e
                    self.by_id[e['id']] = e
                try:
                    self._scan(e, st)
                except (OSError, ValueError) as ex:
                    print('codex index', os.path.basename(p), type(ex).__name__, flush=True)
                changed = True
            for p in [p for p in self.files if p not in seen]:
                self.by_id.pop(self.files.pop(p)['id'], None)
                changed = True
            if changed:
                self.version += 1

    def _new(self, p):
        try:
            with open(p, 'rb') as f:
                line = f.readline(4 << 20)
            d = json.loads(line)
        except (OSError, ValueError):
            return None
        m = d.get('payload') or {}
        tid = m.get('id') or m.get('session_id')
        if not tid:
            return None
        src = m.get('source')
        return {'path': p, 'id': tid, 'size': 0, 'mtime': 0, 'pos': len(line),
                'origin': CX_ORIGIN.get(m.get('originator'), 'tui'),
                'cwd': m.get('cwd') or '', 'meta_ts': parse_ts(m.get('timestamp') or d.get('timestamp')),
                'parent': m.get('parent_thread_id'),
                'guardian': bool(m.get('parent_thread_id')) or m.get('thread_source') == 'guardian_review' or
                isinstance(src, dict),
                'partial': False, 'turns': [], 'calls': 0, 'open': False, 'last_ts': None,
                'thread_total': None, 'limit': None, 'model': '', 'first_user': None}

    def _scan(self, e, st):
        if st.st_size >= CX_BIG:            # the beginning is not read
            e['partial'] = True
            n = CX_TAIL
            while True:
                with open(e['path'], 'rb') as f:
                    f.seek(max(0, st.st_size - n))
                    data = f.read(n)
                lines = data.split(b'\n')[1:]
                e['thread_total'] = None
                for raw in lines:
                    self._safe_line(e, raw)
                if e['thread_total'] or n >= CX_BIG:
                    break
                n *= 4
        else:
            with open(e['path'], 'rb') as f:
                f.seek(e['pos'])
                data = f.read(st.st_size - e['pos'])
            cut = data.rfind(b'\n') + 1
            for raw in data[:cut].split(b'\n'):
                self._safe_line(e, raw)
            e['pos'] += cut
        e['size'], e['mtime'] = st.st_size, st.st_mtime

    def _safe_line(self, e, raw):
        try:
            self._line(e, raw)
        except Exception as ex:   # noqa: BLE001 — one line must not stop the whole list
            line_error(e['id'], None, ex)

    def _line(self, e, raw):
        m = CX_HEAD_RE.match(raw)
        if not m:
            return
        typ, pt = m.group(2), m.group(3)
        ts = parse_ts(m.group(1).decode())
        if ts:
            e['last_ts'] = max(e['last_ts'] or 0, ts)
        small = len(raw) < CX_LINE_MAX
        if typ == b'event_msg':
            if pt == b'task_started':
                e['open'] = True
                if not e['partial'] and e['origin'] == 'exec' and not e['guardian']:
                    e['turns'].append({'start': ts, 'user': None})
            elif pt in (b'task_complete', b'turn_aborted'):
                e['open'] = False
            elif pt == b'token_count' and small and b'"rate_limits"' in raw:
                p = (json.loads(raw).get('payload') or {})
                rl = p.get('rate_limits') or {}
                if rl.get('primary') and ts and (not e['limit'] or ts >= e['limit']['ts']):
                    cr = rl.get('credits') or {}
                    e['limit'] = {'ts': ts, 'primary': rl['primary'], 'secondary': rl.get('secondary'),
                                  'reached': rl.get('rate_limit_reached_type') is not None, 'plan': rl.get('plan_type'),
                                  'credits': {k: cr.get(k) for k in ('has_credits', 'unlimited', 'balance')} if cr else None}
        elif typ == b'token_usage_record' and small:
            p = json.loads(raw).get('payload') or {}
            if not e['partial']:
                e['calls'] += 1
            e['thread_total'] = p.get('thread_token_usage') or e['thread_total']
        elif typ == b'turn_context' and small:
            e['model'] = (json.loads(raw).get('payload') or {}).get('model') or e['model']
        elif typ == b'response_item' and pt == b'message' and small and b'"role":"user"' in raw[:400]:
            need_turn = e['turns'] and e['turns'][-1]['user'] is None
            if e['first_user'] is None or need_turn:
                t = codex_user_text(json.loads(raw).get('payload') or {})
                if t:
                    if e['first_user'] is None:
                        e['first_user'] = t[:20000]
                    if need_turn:
                        e['turns'][-1]['user'] = t[:20000]

    # ---- reading ----
    # Readers get a copy of the entry (the inner values turns, thread_total and limit that the scan updates are copied separately too). `files` and `by_id` are not read directly.
    def entries(self):
        with self.lock:
            return [_snapshot(e) for e in self.files.values()]

    def get(self, thread_id):
        with self.lock:
            e = self.by_id.get(thread_id)
            return _snapshot(e) if e else None

    def children(self, parent_id):
        with self.lock:
            return [_snapshot(e) for e in self.files.values() if e['parent'] == parent_id]

    def title(self, e):
        try:
            mt = os.path.getmtime(CODEX_NAMES)
        except OSError:
            mt = None
        if mt != self._names[0]:
            names = {}
            try:
                with open(CODEX_NAMES, encoding='utf-8', errors='replace') as f:
                    for ln in f:
                        try:
                            x = json.loads(ln)
                        except ValueError:
                            continue
                        if x.get('id') and x.get('thread_name'):
                            names[x['id']] = x['thread_name']     # the later one is the latest name
            except OSError:
                pass
            self._names = (mt, names)
        return self._names[1].get(e['id']) or trunc((e['first_user'] or '').strip().split('\n')[0], 40)

    def limit(self):
        """The account-wide weekly limit: the latest of the records that have rate_limits.primary."""
        with self.lock:
            best = max((e['limit'] for e in self.files.values() if e['limit']), key=lambda x: x['ts'], default=None)
        if not best:
            return None
        p, sec = best['primary'], best.get('secondary')
        resets = p.get('resets_at')
        win = lambda w: {'used_percent': w.get('used_percent'), 'window_minutes': w.get('window_minutes'), 'resets_at': w.get('resets_at'),
                         'stale': bool(w.get('resets_at') and w['resets_at'] <= time.time())} if w else None
        return {'used_percent': p.get('used_percent'), 'window_minutes': p.get('window_minutes'),
                'resets_at': resets, 'as_of': best['ts'], 'stale': bool(resets and resets <= time.time()),
                'reached': best['reached'], 'secondary': win(sec), 'plan_type': best.get('plan'), 'credits': best.get('credits')}


CODEX = CodexIndex()
