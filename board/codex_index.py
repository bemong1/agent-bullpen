"""The list of rollouts in ~/.codex/sessions (CodexIndex, singleton CODEX): re-looks only at changed files and reads only what it needs, going by line heads."""

import copy
import glob
import json
import os
import stat
import threading
import time

from .util import CODEX_NAMES, CODEX_SESSIONS, line_error, parse_ts, trunc
from .codex_parse import CX_BIG, CX_HEAD_RE, CX_LINE_MAX, codex_user_text


CX_TAIL = 4 << 20          # for the list: the first line + the last 4 MB

CX_ORIGIN = {'codex-tui': 'tui', 'Codex Desktop': 'desktop', 'codex_exec': 'exec'}


def _num(x):
    """x when it is a plain number (not a bool), else None: a reset time or a percent of another shape does not reach a comparison."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


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
        self._bad = {}              # rollout path -> (size, mtime) of a file that is not a rollout (no usable first line): left alone until it changes
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
                    changed |= self._refresh_one(p)
                except Exception as ex:   # noqa: BLE001 — one rollout must not stop the list
                    print('codex index', os.path.basename(p), type(ex).__name__, flush=True)
            for p in [p for p in self.files if p not in seen]:
                self.by_id.pop(self.files.pop(p)['id'], None)
                changed = True
            for p in [p for p in self._bad if p not in seen]:
                del self._bad[p]
            if changed:
                self.version += 1

    def _refresh_one(self, p):
        """Looks at one rollout again if it changed. True when the list changed."""
        try:
            st = os.stat(p)
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode) or self._bad.get(p) == (st.st_size, st.st_mtime):
            return False
        e = self.files.get(p)
        if e and e['size'] == st.st_size and e['mtime'] == st.st_mtime:
            return False
        dropped = False
        if e is None or st.st_size < e['size']:
            new = self._new(p)
            if not new:
                self._bad[p] = (st.st_size, st.st_mtime)
                if e is None:
                    return False
                self.by_id.pop(self.files.pop(p)['id'], None)         # it was a rollout and is not any more
                return True
            e = self.files[p] = new
            self.by_id[e['id']] = e
            self._bad.pop(p, None)
        try:
            self._scan(e, st)
        except (OSError, ValueError) as ex:
            print('codex index', os.path.basename(p), type(ex).__name__, flush=True)
        return True

    def _new(self, p):
        """The entry of a rollout from its first line, or None when that line is not a session_meta-like object (valid JSON of another shape is no rollout either)."""
        try:
            with open(p, 'rb') as f:
                line = f.readline(4 << 20)
            d = json.loads(line)
        except (OSError, ValueError, RecursionError):
            return None
        m = d.get('payload') if isinstance(d, dict) else None
        if not isinstance(m, dict):
            return None
        tid = m.get('id') or m.get('session_id')
        if not tid or not isinstance(tid, str):
            return None
        src, cwd, parent, origin = m.get('source'), m.get('cwd'), m.get('parent_thread_id'), m.get('originator')
        stamp = m.get('timestamp') or d.get('timestamp')
        return {'path': p, 'id': tid, 'size': 0, 'mtime': 0, 'pos': len(line),
                'origin': CX_ORIGIN.get(origin if isinstance(origin, str) else None, 'tui'),
                'cwd': cwd if isinstance(cwd, str) else '', 'meta_ts': parse_ts(stamp) if isinstance(stamp, str) else None,
                'parent': parent if isinstance(parent, str) else None,
                'guardian': bool(parent) or m.get('thread_source') == 'guardian_review' or
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
                p = json.loads(raw).get('payload')
                rl = p.get('rate_limits') if isinstance(p, dict) else None
                rl = rl if isinstance(rl, dict) else {}
                if isinstance(rl.get('primary'), dict) and rl['primary'] and ts and (not e['limit'] or ts >= e['limit']['ts']):
                    cr, sec = rl.get('credits'), rl.get('secondary')
                    e['limit'] = {'ts': ts, 'primary': rl['primary'], 'secondary': sec if isinstance(sec, dict) else None,
                                  'reached': rl.get('rate_limit_reached_type') is not None, 'plan': rl.get('plan_type'),
                                  'credits': {k: cr.get(k) for k in ('has_credits', 'unlimited', 'balance')} if isinstance(cr, dict) and cr else None}
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
                        except (ValueError, RecursionError):
                            continue
                        if isinstance(x, dict) and isinstance(x.get('id'), str) and isinstance(x.get('thread_name'), str) and x['id'] and x['thread_name']:
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
        resets = _num(p.get('resets_at'))
        win = lambda w: {'used_percent': w.get('used_percent'), 'window_minutes': w.get('window_minutes'), 'resets_at': _num(w.get('resets_at')),
                         'stale': bool(_num(w.get('resets_at')) and w['resets_at'] <= time.time())} if w else None
        return {'used_percent': p.get('used_percent'), 'window_minutes': p.get('window_minutes'),
                'resets_at': resets, 'as_of': best['ts'], 'stale': bool(resets and resets <= time.time()),
                'reached': best['reached'], 'secondary': win(sec), 'plan_type': best.get('plan'), 'credits': best.get('credits')}


CODEX = CodexIndex()
