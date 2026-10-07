"""The list of rollouts in ~/.codex/sessions (CodexIndex, singleton CODEX): re-looks only at changed files and reads only what it needs, going by line heads."""

import copy
import glob
import hashlib
import heapq
import json
import os
import stat
import threading
import time
import zlib
from collections import OrderedDict

from .util import CODEX_NAMES, CODEX_SESSIONS, line_error, parse_ts, trunc
from .fingerprint import nbytes
from .codex_parse import CX_BIG, CX_HEAD_RE, CX_LINE_MAX, CX_ORD_RE, codex_user_text
from .codex_facts import (CX_ANY_CALL_RE, CX_DEPTH_MAX, CX_ITEM_HEAD, CX_ITEM_RE, CX_NAME_SCAN, CX_NAMES, CX_TEXT_TOTAL, INF, SpawnProofs, ThreadFacts, classify, parse_activity, parse_cmd_exec,
                          parse_message, read_cmd_text, run_window)


CX_TAIL = 4 << 20          # for the list: the first line + the last 4 MB
CX_TEXT_CACHE = 256        # command texts read again that are kept (the newest), and
CX_TEXT_CACHE_BYTES = 4 << 20   # their size in all (bytes of memory)
CX_TURNS_KEEP = 2000       # turns kept per thread (the newest)
CX_ANCHOR = 4096           # bytes before the end of what was read that are looked at again (by their checksum), to tell a file written over from one that grew

CX_ORIGIN = {'codex-tui': 'tui', 'Codex Desktop': 'desktop', 'codex_exec': 'exec'}


def _num(x):
    """x when it is a plain number (not a bool), else None: a reset time or a percent of another shape does not reach a comparison."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


_PRIVATE = ('_facts', '_fork', '_nline', '_off', '_head', '_anchor', '_kdrift', '_turn_text', '_pending', '_readable', '_spawns', '_file_id')       # what the scan keeps in an entry for itself: never in a snapshot (the facts are read by `cmds` and `collab`)


def _snapshot(e):
    """A copy of one entry: the inner mutable values (turns, thread_total, limit) are copied too, so that readers and the scan do not affect each other.
    The lists of commands and collaboration events are not copied (that would cost a copy per entry per call): they are read through CodexIndex.cmds and collab."""
    c = dict(e)
    for k in _PRIVATE:
        c.pop(k, None)
    if e.get('turns') is not None:
        c['turns'] = [dict(t) for t in e['turns']]        # spans of plain values: a shallow copy of each is a copy
    for k in ('thread_total', 'limit'):
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
        self._gen = {}                # thread id -> its latest generation (see `gen` of an entry): kept when its file goes, so that the number only goes up
        self._texts = OrderedDict()   # (thread, generation, item id) -> command text read again
        self._texts_size = 0

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
                self._forget(self.files[p])
                changed = True
            for p in [p for p in self._bad if p not in seen]:
                del self._bad[p]
            changed |= self._resolve_subs()
            if changed:
                self._keep_budget()
                self.version += 1

    def _keep_budget(self):
        """The text held over all threads is bounded: when it is over, the oldest text goes first, whatever the thread (so what is recent stays)."""
        facts = [e['_facts'] for e in self.files.values() if e.get('_facts')]
        total = sum(f.nbytes for f in facts)
        if total <= CX_TEXT_TOTAL:
            return
        heap = [(f.oldest(), i, f) for i, f in enumerate(facts) if f.oldest() is not None]
        heapq.heapify(heap)
        while total > CX_TEXT_TOTAL and heap:
            _, i, f = heapq.heappop(heap)
            total -= f.strip_oldest() or 0
            if f.oldest() is not None:
                heapq.heappush(heap, (f.oldest(), i, f))

    def _refresh_one(self, p):
        """Looks at one rollout again if it changed. True when the list changed."""
        try:
            st = os.stat(p)
        except OSError:
            e = self.files.get(p)
            if e and e['_readable']:
                e['_readable'] = False
                return True
            return False
        if not stat.S_ISREG(st.st_mode) or self._bad.get(p) == (st.st_size, st.st_mtime):
            return False
        e = self.files.get(p)
        if e and e['_readable'] and e['size'] == st.st_size and e['mtime'] == st.st_mtime and e['_file_id'] == (st.st_dev, st.st_ino):
            return False
        had = e is not None
        if had and (st.st_size < e['size'] or not self._same_file(e)):
            self._forget(e)                                  # smaller, or written over: nothing of it is trusted any more, its facts are made again from the start
            e = None
        if e is None:
            new = self._new(p)
            if not new:
                self._bad[p] = (st.st_size, st.st_mtime)
                return had                                   # it was a rollout and is not any more
            new['gen'] = self._gen[new['id']] = self._gen.get(new['id'], 0) + 1
            e = self.files[p] = new
            self.by_id[e['id']] = e
            self._bad.pop(p, None)
        try:
            self._scan(e, st)
            e['_readable'] = True
        except (OSError, ValueError) as ex:
            e['_readable'] = False
            print('codex index', os.path.basename(p), type(ex).__name__, flush=True)
        return True

    def _resolve_subs(self):
        """A v2 child without a history boundary stays hidden until its parent pairs its started item with an explicit fork_turns: none spawn call.
        Parents may be found after children; repeat for nested children. A parent that went away or was rewritten withdraws its proof too."""
        changed = False
        for _ in range(CX_DEPTH_MAX + 1):
            moved = False
            for e in list(self.files.values()):
                if not e['_pending']:
                    continue
                parent = self.by_id.get(e['parent'])
                proof = parent.get('_spawns') if parent and parent['_readable'] else None
                none = bool(proof and self._verified_spawn(parent, e['id']))
                if (e['kind'] == 'sub') == none:
                    continue
                new = self._new(e['path'], no_copy=none)
                if new is None:
                    self._forget(e)
                    moved = True
                    continue
                try:
                    self._scan(new, os.stat(new['path']))
                    new['_readable'] = True
                except (OSError, ValueError):
                    if none:
                        continue                             # no readable child: no facts from it are published
                self._forget(e)
                new['gen'] = self._gen[new['id']] = self._gen.get(new['id'], 0) + 1
                self.files[new['path']] = self.by_id[new['id']] = new
                moved = True
            changed |= moved
            if not moved:
                break
        return changed

    @staticmethod
    def _verified_spawn(parent, child):
        """A retained no-copy claim is usable only while its file identity, meta, spawn line and started line can still be verified, even outside a big file's tail."""
        proof = parent['_spawns']
        calls = proof.none_calls(child)
        if not calls:
            return False
        try:
            st = os.stat(parent['path'])
            if (st.st_dev, st.st_ino) != parent['_file_id'] or not stat.S_ISREG(st.st_mode):
                proof.invalidate()
                return False
            with open(parent['path'], 'rb') as f:
                st = os.fstat(f.fileno())
                head = f.readline(4 << 20)
                if (st.st_dev, st.st_ino) != parent['_file_id'] or (len(head), zlib.crc32(head)) != parent['_head']:
                    proof.invalidate()
                    return False
                for c in calls:
                    for key in ('call_line', 'started_line'):
                        stamp = c.get(key)
                        if stamp is None:
                            c['bad'] = True
                            break
                        off, length, digest = stamp
                        f.seek(off)
                        raw = f.read(length + 1)
                        if len(raw) != length + 1 or raw[-1:] != b'\n' or hashlib.sha256(raw[:-1]).digest() != digest:
                            c['bad'] = True
                            break
            st = os.stat(parent['path'])
            if (st.st_dev, st.st_ino) != parent['_file_id']:
                proof.invalidate()
                return False
        except OSError:
            proof.invalidate()
            return False
        return proof.has_none(child)

    def _same_file(self, e):
        """False when the file is not the one that was read: its first line is another, or the last bytes that were read are not where they were. A file that only grew is the same."""
        try:
            with open(e['path'], 'rb') as f:
                st = os.fstat(f.fileno())
                if (st.st_dev, st.st_ino) != e['_file_id']:
                    return False
                line = f.readline(4 << 20)
                if (len(line), zlib.crc32(line)) != e['_head']:
                    return False
                a = e['_anchor']
                if a:
                    f.seek(a[0])
                    return zlib.crc32(f.read(a[1])) == a[2]
        except OSError:
            pass
        return True

    def _forget(self, e):
        """Takes an entry out: of the list, of the ids and of the texts read again (they belong to the file that was)."""
        self.files.pop(e['path'], None)
        if self.by_id.get(e['id']) is e:
            del self.by_id[e['id']]
        for k in [k for k in self._texts if k[0] == e['id']]:
            self._texts_size -= nbytes(self._texts.pop(k))

    def _new(self, p, no_copy=False):
        """The entry of a rollout from its first line, or None when that line is not a session_meta-like object (valid JSON of another shape is no rollout either)."""
        try:
            with open(p, 'rb') as f:
                line = f.readline(4 << 20)
                st = os.fstat(f.fileno())
            d = json.loads(line)
        except (OSError, ValueError, RecursionError):
            return None
        m = d.get('payload') if isinstance(d, dict) else None
        if not isinstance(m, dict):
            return None
        tid = m.get('id') or m.get('session_id')
        if not tid or not isinstance(tid, str):
            return None
        cwd, origin = m.get('cwd'), m.get('originator')
        stamp = m.get('timestamp') or d.get('timestamp')
        k = classify(m, no_copy=no_copy)
        pending = 'subagent_history_start_ordinal' not in m and classify(m, no_copy=True)['kind'] == 'sub'
        origin_exec = CX_ORIGIN.get(origin if isinstance(origin, str) else None, 'tui') == 'exec'
        return {'path': p, 'id': tid, 'size': 0, 'mtime': 0, 'pos': len(line),
                'origin': CX_ORIGIN.get(origin if isinstance(origin, str) else None, 'tui'),
                'cwd': cwd if isinstance(cwd, str) else '', 'meta_ts': parse_ts(stamp) if isinstance(stamp, str) else None,
                'kind': k['kind'], 'parent': k['parent'], 'agent_path': k['agent_path'], 'nick': k['nick'], 'depth': k['depth'],
                'prefix_ord': k['prefix_ord'], 'drift': k['drift'], 'gen': 0,
                # "not a root": the old name stays for the callers that keep a thread out of the lists and out of the link candidates; it is no longer only the approval review
                'guardian': k['kind'] != 'root',
                'partial': False, 'turns': [], 'calls': 0, 'open': False, 'last_ts': None,
                'thread_total': None, 'limit': None, 'model': '', 'first_user': None,
                'cmds_skipped': 0, 'cmds_drift': 0, 'cmds_big': 0, 'cmds_evicted': 0,
                '_facts': ThreadFacts() if k['kind'] in ('root', 'sub') else None,   # commands and collaboration are read for the threads that can launch or be launched
                '_spawns': SpawnProofs() if k['kind'] in ('root', 'sub') else None,   # proof belongs to this file generation, including when its tail and cards are read again
                '_fork': k['kind'] == 'sub',       # still inside the history copied from the parent (the first lines of a sub-agent's rollout)
                '_nline': 0,                       # the number of the line being read (the meta line is 0), while _fork: the ordinal when a line has none
                '_turn_text': (origin_exec and k['kind'] == 'root') or k['kind'] == 'sub',   # the instruction text of a turn is kept (up to 20,000 characters): for these only; the others keep spans
                '_off': 0,                         # where the line being read starts in the file
                '_head': (len(line), zlib.crc32(line)),   # the first line as it was, and the last bytes read (set by the scan): to tell a file written over from one that grew
                '_file_id': (st.st_dev, st.st_ino),
                '_anchor': None,
                '_kdrift': k['drift'],             # `drift` of the kind alone (drift also rises when a record of a kind we read cannot be read)
                '_pending': pending, '_readable': False}

    def _scan(self, e, st):
        if st.st_size >= CX_BIG:            # the beginning is not read
            e['partial'] = True
            e['_fork'] = False              # the copied history is at the beginning, which is not read (what of it is in the tail is told by the ordinal of each line)
            e['_anchor'] = None
            n = CX_TAIL
            while True:
                start = max(0, st.st_size - n)
                with open(e['path'], 'rb') as f:
                    f.seek(start)
                    data = f.read(n)
                lines = data.split(b'\n')
                off = start + len(lines[0]) + 1
                e['thread_total'] = None
                e['turns'] = []             # the same for the turns (spans, as far as the tail shows them)
                if e['_facts']:
                    e['_facts'].reset()     # the tail is read again from its start each time: what it holds is the whole of what is kept
                for raw in lines[1:]:
                    e['_off'] = off
                    self._safe_line(e, raw)
                    off += len(raw) + 1
                if e['thread_total'] or n >= CX_BIG:
                    break
                n *= 4
        else:
            with open(e['path'], 'rb') as f:
                f.seek(e['pos'])
                data = f.read(st.st_size - e['pos'])
            cut = data.rfind(b'\n') + 1
            off = e['pos']
            for raw in data[:cut].split(b'\n')[:-1]:        # whole lines only: what is after the last line end is not a line (yet)
                e['_off'] = off
                self._safe_line(e, raw)
                off += len(raw) + 1
            if cut:
                tail = data[max(0, cut - CX_ANCHOR):cut]
                e['_anchor'] = (e['pos'] + cut - len(tail), len(tail), zlib.crc32(tail))
            e['pos'] += cut
        f = e['_facts']
        if f:
            e['cmds_skipped'], e['cmds_drift'], e['cmds_big'], e['cmds_evicted'] = f.skipped, f.drift, f.big, f.evicted
            e['drift'] = e['_kdrift'] or f.drift > 0
        e['size'], e['mtime'] = st.st_size, st.st_mtime

    def _safe_line(self, e, raw):
        try:
            self._line(e, raw)
        except Exception as ex:   # noqa: BLE001 — one line must not stop the whole list
            line_error(e['id'], None, ex)

    def _line(self, e, raw):
        fork = e['_fork']
        if fork:
            e['_nline'] += 1
        m = CX_HEAD_RE.match(raw)
        facts = e['_facts']
        ts = parse_ts(m.group(1).decode()) if m else None
        if ts:
            e['last_ts'] = max(e['last_ts'] or 0, ts)
        if m and facts and e['partial'] and facts.tail_ts is None:
            facts.tail_ts = ts
        if (fork or (e['partial'] and e['kind'] == 'sub')) and self._copied(e, raw):
            return                  # the history copied from the parent is the parent's: no turn, no instruction, no command of this thread
        if not m:
            if facts:
                self._odd(e, raw)   # not the usual head (the keys are in another order): still looked at when it names an item we read
            return
        typ, pt = m.group(2), m.group(3)
        small = len(raw) < CX_LINE_MAX
        if typ == b'event_msg':
            if pt == b'task_started':
                e['open'] = True
                if facts:
                    facts.begin_turn(ts)
                if facts:
                    self._turn(e, ts)
            elif pt in (b'task_complete', b'turn_aborted'):
                e['open'] = False
                if facts:
                    facts.end_turn(ts or e['last_ts'])
                    self._turn_end(e, ts)
            elif pt == b'token_count' and small and b'"rate_limits"' in raw:
                p = json.loads(raw).get('payload')
                rl = p.get('rate_limits') if isinstance(p, dict) else None
                rl = rl if isinstance(rl, dict) else {}
                if isinstance(rl.get('primary'), dict) and rl['primary'] and ts and (not e['limit'] or ts >= e['limit']['ts']):
                    cr, sec = rl.get('credits'), rl.get('secondary')
                    e['limit'] = {'ts': ts, 'primary': rl['primary'], 'secondary': sec if isinstance(sec, dict) else None,
                                  'reached': rl.get('rate_limit_reached_type') is not None, 'plan': rl.get('plan_type'),
                                  'credits': {k: cr.get(k) for k in ('has_credits', 'unlimited', 'balance')} if isinstance(cr, dict) and cr else None}
            elif pt == b'item_completed' and facts:
                self._item(e, raw, ts)
            elif pt is None and facts:
                self._odd(e, raw)
        elif typ == b'token_usage_record' and small:
            p = json.loads(raw).get('payload') or {}
            if not e['partial']:
                e['calls'] += 1
            e['thread_total'] = p.get('thread_token_usage') or e['thread_total']
        elif typ == b'turn_context' and small:
            e['model'] = (json.loads(raw).get('payload') or {}).get('model') or e['model']
        elif typ == b'response_item' and pt == b'message' and small and facts and b'"role":"user"' in raw[:400]:
            need_turn = e['_turn_text'] and not e['partial'] and e['turns'] and e['turns'][-1]['user'] is None
            if e['first_user'] is None or need_turn:
                t = codex_user_text(json.loads(raw).get('payload') or {})
                if t:
                    if e['first_user'] is None:
                        e['first_user'] = t[:20000]
                    if need_turn:
                        e['turns'][-1]['user'] = t[:20000]
        elif typ == b'response_item' and pt == b'agent_message' and small and facts:
            self._message(e, raw, ts)
        elif typ == b'response_item' and facts and pt in (b'custom_tool_call', b'custom_tool_call_output', b'function_call', b'function_call_output'):
            facts.note_call(pt, raw, ts or e['last_ts'])
            if pt in (b'function_call', b'custom_tool_call'):
                self._spawn_call(e, raw, small)

    @staticmethod
    def _spawn_call(e, raw, small):
        """Every call identity can invalidate a spawn, even another tool or unreadable arguments. Only a readable spawn can prove none; no instruction text is kept."""
        cid = CX_ANY_CALL_RE.search(raw)
        if cid is None:
            return
        p = None
        if small and b'"spawn_agent"' in raw[:1024]:
            try:
                p = json.loads(raw).get('payload')
            except (ValueError, TypeError, AttributeError, RecursionError):
                pass
        stamp = (e['_off'], len(raw), hashlib.sha256(raw).digest()) if isinstance(p, dict) else None
        e['_spawns'].note_call(cid.group(1).decode('utf-8', 'replace'), e['_off'], p, stamp)

    @staticmethod
    def _copied(e, raw):
        """True for a line of a sub-agent's rollout that is the parent's history. While the rollout is read from its beginning (e['_fork']): a line whose ordinal is at most
        subagent_history_start_ordinal (a line without the key counts by its place in the file, which is the same number); the first line behind it ends the copied part for good.
        In the tail of a big rollout the copied part may still be there and nothing says where the tail began: each line is told by its own ordinal, and a line with none is
        not known to be the thread's own."""
        om = CX_ORD_RE.match(raw)
        if e['partial']:
            return om is None or int(om.group(1)) <= e['prefix_ord']
        if (int(om.group(1)) if om else e['_nline']) <= e['prefix_ord']:
            return True
        e['_fork'] = False
        return False

    def _item(self, e, raw, ts):
        """An item_completed line: the commands the thread ran and the sub-agent events. Other items are not decoded."""
        im = CX_ITEM_RE.search(raw, 0, CX_ITEM_HEAD)
        if not im:
            self._odd(e, raw, ts)
        elif im.group(1) == b'CommandExecution':
            self._command(e, raw, im.start(), ts)
        elif len(raw) < CX_LINE_MAX:
            self._activity(e, raw, ts)

    def _command(self, e, raw, at, ts):
        """A CommandExecution item (at byte `at` of the line): a command; one that cannot be read is counted, and its time is a gap."""
        f = e['_facts']
        c, big = parse_cmd_exec(raw, at, e['id'], ts, e['_off'])
        if c is None:
            f.unreadable(*(run_window(raw, ts) or (e['meta_ts'] or -INF, ts or e['last_ts'])))      # when it ran if the line says, else sometime between the start of the thread and now
        elif c is not False:
            c['exec'] = f.wrapper(c['start'])      # the exec call the command ran in, when it is known (the group of a launch, the start of its window)
            f.add_cmd(c, big)

    def _activity(self, e, raw, ts):
        c = parse_activity(raw, e['id'], ts)
        if c:
            if c['kind'] == 'started':
                e['_spawns'].note_started(c, (e['_off'], len(raw), hashlib.sha256(raw).digest()))
            e['_facts'].add_collab(c)

    def _odd(self, e, raw, ts=None):
        """A line that does not start the usual way (an item whose id comes before its type, a payload whose type is not first, a line whose time is not first): when it names
        a kind of item we read anywhere in its front it is decoded whole, whatever the order of its keys. A CommandExecution that cannot be read that way is a gap and a drift,
        not nothing; a line that only has the name somewhere else (another kind of item, a text) is nothing."""
        named = [n for n in CX_NAMES if raw.find(n, 0, CX_NAME_SCAN) >= 0]
        if not named:
            return
        f = e['_facts']
        if len(raw) >= CX_LINE_MAX:                         # too big to decode, and the front is not in the usual order
            if CX_NAMES[0] in named:
                f.unreadable(e['meta_ts'] or -INF, ts or e['last_ts'])
            return
        try:
            d = json.loads(raw)
            p = d['payload']
            kind = p['item'].get('type') if p.get('type') == 'item_completed' else None
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            if CX_NAMES[0] in named:
                f.unreadable(e['meta_ts'] or -INF, ts or e['last_ts'])
            return
        if ts is None and isinstance(d.get('timestamp'), str):
            ts = parse_ts(d['timestamp'])
        if kind == 'CommandExecution':
            self._command(e, raw, 0, ts)
        elif kind == 'SubAgentActivity':
            self._activity(e, raw, ts)

    @staticmethod
    def _turn(e, ts):
        """A turn began: its span (start, end) is kept for every root and sub-agent (no text, except for the ones whose instruction is read: see _turn_text). A turn that never
        ended (the process was stopped) ends when the next one begins."""
        turns = e['turns']
        if turns and turns[-1]['end'] is None:
            turns[-1]['end'] = ts
        turns.append({'start': ts, 'end': None, 'user': None})
        if len(turns) > CX_TURNS_KEEP:
            del turns[0]

    @staticmethod
    def _turn_end(e, ts):
        """A turn ended. In the tail of a big rollout the end may be of a turn that began before the tail: a span with no start (it is not known)."""
        turns = e['turns']
        if turns and turns[-1]['end'] is None:
            turns[-1]['end'] = ts
        elif e['partial']:
            turns.append({'start': None, 'end': ts, 'user': None})
            if len(turns) > CX_TURNS_KEEP:
                del turns[0]

    def _message(self, e, raw, ts):
        """An agent_message line: a collaboration event. It is never the instruction of a sub-agent: the first message to it is the instruction and Codex encrypts that, what
        is readable is only the note that comes with it (who it is from and where it goes), and a plain message is no different. The instruction is not in the record,
        so `first_user` and the `user` of its turns stay None."""
        p = json.loads(raw).get('payload')
        if isinstance(p, dict):
            e['_facts'].add_collab(parse_message(p, e['id'], ts)[0])

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

    def root_of(self, thread_id):
        """The root thread above a thread, following `parent`; the thread itself when it is a root. None for an unknown thread, a missing parent, a cycle,
        more than CX_DEPTH_MAX steps, and any chain that passes an `internal` thread (its parent is not to be trusted)."""
        with self.lock:
            seen, cur = set(), thread_id
            for _ in range(CX_DEPTH_MAX + 1):
                e = self.by_id.get(cur)
                kind = e.get('kind', 'guardian' if e.get('guardian') else 'root') if e else None
                if kind in (None, 'internal') or cur in seen:
                    return None
                if kind == 'root':
                    return cur
                seen.add(cur)
                cur = e['parent']
            return None

    def cmds(self, thread_id):
        """The commands a thread ran (CmdExec dicts), oldest first, without the part of a sub-agent's rollout that its parent copied. At most CX_CMDS_KEEP of the newest.
        The dicts are shared with the scan: do not change them."""
        with self.lock:
            e = self.by_id.get(thread_id)
            return tuple(e['_facts'].cmds) if e and e.get('_facts') else ()

    def gaps(self, thread_id):
        """Where the commands of a thread may be missing: a tuple of (start, end, why), sorted, end None when it is still open. why:
        turn      a turn is open: a command that runs now is written when it ends (from the start of the turn)
        open      a command is known to run whose record has not come (it started with an exec cell that answered "running", or with a PTY session): while no turn is open
                  that is a command that outlived its turn; the record may never come (from the start of the cell to the end of the turn)
        big       a command text over the limit: the command is known, what it ran is not
        unreadable  a record line that could not be read (from the start of the thread to the line)
        partial   only the end of a big file is read: everything before it is not known (from -inf)
        budget    the text of the commands in this range was dropped to keep within the budget (cmd_text may read it again)
        evicted   the commands in this range were pushed out by the count of CX_CMDS_KEEP
        reread    a text that cmd_text could not read again
        many      several of the above in one range, when a thread has too many to keep apart
        A command whose record has come is not a gap. Empty for a thread that is not a root or a sub-agent."""
        with self.lock:
            e = self.by_id.get(thread_id)
            f = e.get('_facts') if e else None
            if not f:
                return ()
            out = list(f.gaps)
            if f.evict:
                out.append((f.evict[0], f.evict[1], 'evicted'))
            if f.strip:
                out.append((f.strip[0], f.strip[1], 'budget'))
            if e['partial']:
                out.append((-INF, f.tail_ts, 'partial'))
            if e['open']:
                out.append((f.turn_start if f.turn_start is not None else -INF, None, 'turn'))
            else:
                out.extend((start, None, 'open') for start in f.runs.values())
            return tuple(sorted(out, key=lambda g: (g[0], g[1] is None, g[1] or 0, g[2])))

    def running(self, thread_id):
        """The times the commands that run now in an open turn started, whose records have not come: an exec call with no output yet, a cell that answered "running", a PTY session. `gaps`
        lists them under the open turn (`turn`), which says only that one may be there; this says that one is. A tuple, sorted; empty when no turn is open."""
        with self.lock:
            e = self.by_id.get(thread_id)
            f = e.get('_facts') if e else None
            if not f or not e['open']:
                return ()
            return tuple(sorted(list(f.execs.values()) + list(f.runs.values())))

    def cmd_text(self, thread_id, item_id):
        """The command text of a command whose text was dropped for the budget, read again from its record in the file (the newest few are kept); the text itself when it is still held.
        None when the command is unknown, never had a text (not a shell command, over the limit) or the file no longer has it: then its time is a gap (why 'reread')."""
        with self.lock:
            e = self.by_id.get(thread_id)
            f = e.get('_facts') if e else None
            if not f:
                return None
            c = next((c for c in reversed(f.cmds) if c['item_id'] == item_id), None)
            if c is None:
                return None
            if c['cmd'] is not None:
                return c['cmd']
            if item_id not in f.stripped:
                return None
            key = (thread_id, e['gen'], item_id)
            if key in self._texts:
                self._texts.move_to_end(key)
                return self._texts[key]
            text = read_cmd_text(e['path'], c['offset'], item_id)
            if text is None:
                f.stripped.discard(item_id)
                f.add_gap(c['start'], c['end'], 'reread')
                self.version += 1
                return None
            self._texts[key] = text
            self._texts_size += nbytes(text)
            while len(self._texts) > CX_TEXT_CACHE or self._texts_size > CX_TEXT_CACHE_BYTES:
                self._texts_size -= nbytes(self._texts.popitem(last=False)[1])
            return text

    def collab(self, thread_id):
        """The collaboration events in a thread's record (Collab dicts), oldest first, without the copied part. At most CX_COLLAB_KEEP of the newest. Do not change the dicts."""
        with self.lock:
            e = self.by_id.get(thread_id)
            return tuple(e['_facts'].collab) if e and e.get('_facts') else ()

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
        name = self._names[1].get(e['id']) or trunc((e['first_user'] or '').strip().split('\n')[0], 40)
        if not name and e.get('kind') == 'sub':
            name = (e['agent_path'] or '').rstrip('/').rsplit('/', 1)[-1] or e['nick'] or ''     # its instruction is not in the record: the name it was given
        return name

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
