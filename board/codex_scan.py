"""The front of a big Codex rollout, read in the background.

A rollout over 64 MB is read from its end (`codex_parse.cx_start_offset`): what is before that place was never read, and the span it covers is `lost` (the judgment holds a first author back
when an agent may have written before it, J3 ③). The front of a big rollout is mostly long outputs and images, and what a write event is made of (the `item_completed` of a command or of a file
change) is a small part of it. `scan` reads the front once, one block at a time, keeps only the lines whose first bytes say they are such an item (a test of bytes, no JSON for the rest),
decodes only those and makes their events and windows (`agents.cx_item`) in an event log of their own. `FrontScan` does it on a thread of its own, one scan at a time for the whole
server (the first picture does not wait for it), remembers what it did for a file (its identity and the place the read began) so that it is not done twice, and hands the result over to the
event log of the agent in one step (`EventLog.merge_front`): from then on that part is no longer `lost`, and `facts_gen` has gone up.

Memory: a block of BLOCK bytes and the line being read when it is such an item (at most `codex_parse.CX_ITEM_MAX`); a long line that is not one is skipped as it streams by, whatever its length."""

import collections
import hashlib
import json
import os
import threading
import time

from . import codex_parse as CP
from .codex_parse import CX_HEAD_RE, CX_ITEM_HEAD, CX_ORD_RE, cx_write_item
from .util import parse_ts

BLOCK = 8 << 20            # bytes read at a time
SAMPLE = 64 << 10          # bytes of the head of a file, and of the end of its front, that tell it from another file (and from the same file written over)
INF = float('inf')
YIELD_EVERY = 64           # items decoded between two moments the thread lets the others go on
DONE_KEEP = 4              # the results of this many files are remembered (their events are bounded by the event log's own limits)
_SLOT = threading.Semaphore(1)
_DONE = collections.OrderedDict()      # (path, ordinal of the copied history, `file_version`) -> the event log made of the front of that file, its stats, and (size, mtime) of the file when it was done
_DONE_LOCK = threading.Lock()


def file_version(path, partial):
    """What a front is made of, as far as it can be told cheaply: the file (inode and device), the place its front ends, and the bytes at its head and just before that place (a hash of each). A file that
    was written over (the same inode, the same size) or replaced by another differs in them; one that only grew does not (a rollout is only appended to). None when it cannot be read."""
    try:
        st = os.stat(path)
        with open(path, 'rb') as f:
            head = f.read(SAMPLE)
            f.seek(max(0, partial - SAMPLE))
            tail = f.read(min(SAMPLE, partial))
    except OSError:
        return None
    if st.st_size < partial:
        return None
    return (st.st_ino, st.st_dev, partial, hashlib.sha1(head).hexdigest(), hashlib.sha1(tail).hexdigest())


def stat_of(path):
    """(size, mtime in ns) of a file, None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def only_grew(before, after):
    """Whether a file that was (size, mtime) `before` is (size, mtime) `after` by what is written to the end of it alone (CONTRACT O18): longer, or the same as it was. The same size with another time, or
    a shorter file, was written over. (A rollout is only appended to: a file that was written over in the middle and grew in the same time is not told from one that only grew. Known limit.)"""
    return before is not None and after is not None and (after[0] > before[0] or after == before)


def candidates(f, limit, prefix_ord=None, stats=None):
    """The lines (bytes) of the file `f` from its start that are the `item_completed` of a command or of a file change (or were cut before that could be read: `codex_parse.cx_cut_head`), up to the line that holds the byte `limit` (the line the later read drops
    as a partial one is the last one looked at). A line that is such an item and longer than `codex_parse.CX_ITEM_MAX` is not given: `stats['big']` gets its time. `prefix_ord`: the file is a sub-agent's, and
    the lines whose ordinal (the line's own number when it has none) is at most this are its parent's copied history: they are none of its own. For the reader of a line that cannot be read, `stats` holds
    the head of the line before the one given (`prev_head`) and, when the reader sets `stats['arm']`, the head of the line after it (`next_head`); `stats['end']` is where the last line looked at ends."""
    stats = {} if stats is None else stats
    stats.setdefault('big', [])
    stats.setdefault('lines', 0)
    base, n = 0, 0                         # the place of the start of `data` in the file; the number of the line being read
    chunks, total = [], 0                  # the bytes of the line so far, while they are kept
    keep, decided = True, False            # whether they are kept; whether the head of the line was looked at
    prev_head = cur_head = None            # the head of the line before the one being read and of the one being read
    copied = prefix_ord is not None        # still inside the history copied from the parent
    while True:
        data = f.read(BLOCK)
        if not data:
            return
        i = 0
        while i < len(data):
            j = data.find(b'\n', i)
            if keep:
                seg = data[i:j] if j >= 0 else data[i:]
                chunks.append(seg)
                total += len(seg)
                if not decided and (total >= CX_ITEM_HEAD or j >= 0):
                    head = (chunks[0] if len(chunks) == 1 else b''.join(chunks))[:CX_ITEM_HEAD]
                    decided = True
                    own = True
                    if copied:
                        om = CX_ORD_RE.match(head)
                        if (int(om.group(1)) if om else n) <= prefix_ord:
                            own = False
                        else:
                            copied = False
                    if own:                                        # (the lines of the parent's history that a sub-agent's rollout begins with are no lines of it)
                        prev_head, cur_head = cur_head, head
                        if stats.get('arm'):                       # the reader asked for the head of the line after the one it has: this one
                            stats['next_head'], stats['arm'] = head, False
                    keep = own and (cx_write_item(head) or CP.cx_cut_head(head))          # (a line cut before its kind could be read may be a write)
                    if not keep:
                        chunks, total = [], 0
                if keep and total > CP.CX_ITEM_MAX:
                    m = CX_HEAD_RE.match((chunks[0] if len(chunks) == 1 else b''.join(chunks))[:CX_ITEM_HEAD])
                    stats['big'].append(parse_ts(m.group(1).decode()) if m else None)
                    keep, chunks, total = False, [], 0
            if j >= 0:
                if keep and decided:
                    stats['prev_head'] = prev_head                 # (the head of the line before the one that is given)
                    yield b''.join(chunks)
                end = base + j
                n += 1
                stats['lines'] = n
                chunks, total, keep, decided = [], 0, True, False
                if end >= limit:
                    stats['end'] = end                             # where the last line looked at ends: the line after it is not looked at here
                    return
                i = j + 1
            else:
                break
        base += len(data)


def scan(path, limit, who, cwd, windows_keep, prefix_ord=None):
    """(EventLog, stats) made of the front of a rollout (bytes 0 to the line that holds `limit`): the write events, the reads and the command windows of the commands and file changes in it,
    for the author `who`. The run of such an event is not known (the turns of the front were never counted): None."""
    from . import agents as AG
    log, stats = AG.EventLog(windows_keep), {'items': 0, 'bad': 0}
    waiting = None                         # a write that could not be read: (its head, the time of the line before it); the line after it says whether it leaves a hole

    def settle(f):
        """What follows the unread write that waits: the line after it (its head from the reader, else read from the file where the last line looked at ends), and so the hole it leaves."""
        nonlocal waiting
        bad, prev_ts = waiting
        waiting = None
        head = stats.pop('next_head', None)
        if head is None and stats.get('end') is not None:
            f.seek(stats['end'] + 1)
            head = f.read(CP.CX_ITEM_HEAD) or None
        nxt = (CP.cx_head(head) or {}) if head is not None else None
        span = CP.cx_hole(bad, prev_ts, nxt)
        if span is not None:
            log.widen_lost(*span)
    with open(path, 'rb') as f:
        for raw in candidates(f, limit, prefix_ord, stats):
            if waiting is not None:
                settle(f)
            try:
                d = json.loads(raw)
            except (ValueError, RecursionError):
                d = None
            if d is not None:                                          # JSON: read as `cx_rows` reads it (a line that is no write is none; a write with no time cannot be put in the record)
                p = d.get('payload') if isinstance(d, dict) else None
                item = p.get('item') if isinstance(p, dict) else None
                if not (isinstance(item, dict) and item.get('type') in ('CommandExecution', 'FileChange')):
                    continue
                ts = parse_ts(d.get('timestamp'))
            else:
                item, ts = None, None
            if ts is None:                                             # the line of a write that cannot be read
                prev = CP.cx_head(stats['prev_head']) if stats.get('prev_head') else None
                waiting = (CP.cx_head(raw[:CP.CX_ITEM_HEAD]) or {}, prev['ts'] if prev else None)
                stats['arm'] = True
                stats['bad'] += 1
                continue
            del raw
            AG.cx_item(log, who, ts, item, cwd, None, None)
            stats['items'] += 1
            if stats['items'] % YIELD_EVERY == 0:
                time.sleep(0)
        if waiting is not None:
            settle(f)
    for ts in stats['big']:
        if ts is not None:
            log.widen_lost(ts, ts)                       # a write or a command too long to be read even here
    return log, stats


class FrontScan(object):
    """The reading of the front of one big rollout (what `scan` does), for the owner that has its events: `state` is None (not started), 'running', 'done' or 'failed'. It starts once for a
    file (its identity and the place the read began), and `reset` (the rollout is read again from its start) lets it start again."""

    def __init__(self):
        self.state = None
        self.thread = None
        self.stats = None
        self._gen = 0                    # goes up on reset: a scan of an earlier time hands nothing over
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self._gen += 1
            self.state, self.thread, self.stats = None, None, None

    def start(self, path, partial, log, who, cwd, prefix_ord=None):
        """Begins the scan of the bytes before `partial` of the rollout `path`, for the event log `log`, on a thread. Does nothing when it was begun for this file already. Returns the thread (None when the
        result was there already or nothing begins). What was made of a file before is good while the file has only grown (CONTRACT O18: the same inode and place, the sampled bytes the same, and not shorter
        or of another time at the same size); one that was written over is scanned again, and a scan of a file that was written over while it ran is handed over to nobody (the front stays `lost`).
        The remembered results are of this process alone: a process that begins again scans again."""
        with self._lock:
            if self.state is not None or partial <= 0:
                return None
            version = file_version(path, partial)
            if version is None:
                return None
            key = (path, prefix_ord, version)
            now = stat_of(path)
            with _DONE_LOCK:
                got = _DONE.get(key)
                if got is not None and not only_grew(got[2], now):
                    del _DONE[key]                    # the file was written over since (the same size and another time, or shorter): what was made of it is no more
                    got = None
                if got is not None:
                    _DONE[key] = got[:2] + (now,)         # (the file as it is now is what the next look compares with)
                    _DONE.move_to_end(key)
            if got is not None:                   # done for this file before (another page, an agent made again): the same events, as this agent's
                self.state, self.stats = 'done', got[1]
                log.merge_front(got[0], who)
                log.clear_front_lost()
                return None
            self.state = 'running'
            gen = self._gen
            self.thread = threading.Thread(target=self._run, args=(gen, key, version, path, partial, log, who, cwd, prefix_ord), daemon=True)
            self.thread.start()
            return self.thread

    def _run(self, gen, key, version, path, partial, log, who, cwd, prefix_ord):
        with _SLOT:                              # one at a time for the whole server
            if gen != self._gen:
                return
            try:
                before = stat_of(path)
                front, stats = scan(path, partial, who, cwd, log.windows_keep, prefix_ord)
                after = stat_of(path)
                if file_version(path, partial) != version or not only_grew(before, after):
                    raise OSError('the file was written over')
            except Exception as e:   # noqa: BLE001 — the scan is an extra: the front stays `lost`
                print('rollout front scan error', type(e).__name__, flush=True)
                with self._lock:
                    if gen == self._gen:
                        self.state = 'failed'
                return
            with self._lock:
                if gen != self._gen:
                    return
                with _DONE_LOCK:
                    _DONE[key] = (front, stats, after)
                    while len(_DONE) > DONE_KEEP:
                        _DONE.popitem(last=False)
                log.merge_front(front, who)
                log.clear_front_lost()
                self.state, self.stats = 'done', stats
