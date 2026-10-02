"""Reading sessions and building their state: CodexLinker (the Codex agents inside a Claude session), Session (one Claude session), CodexSession (a standalone Codex conversation)."""

import collections
import glob
import hashlib
import json
import os
import re
import threading
import time

from . import affil, lineage, procs
from .facts import Redirect
from . import runstate as RS
from .util import (
    CLAUDE_HOME, FILE_MAX, PROJECTS, STALL_SEC, Tail, as_text, line_error, open_safe, parse_records, parse_ts,
    stat_plain, strip_reminders, trunc,
)
from .tokens import TokenMeter, claude_model_short, cx_model_short
from .codex_parse import (
    CX_MAX_READ, CX_PROMPT_MIN, CX_WINDOW, codex_call, codex_say_text, codex_user_text, cx_alive, cx_decode,
    cx_open, cx_procs, cx_start_offset,
)
from .codex_index import CODEX
from .link import LINKS, _cx_expand, cx_parse_call
from .agents import (
    Agent, CodexAgent, cx_record_usage, cx_reprice, cx_sync_base, cx_sync_guardians, model_numbers, tool_brief,
)
from .debates import REPORT_RE, brief_table, judge as judge_debates, read_head, writer_table
from . import units as U
from . import views


AGENT_ID_RE = re.compile(r'^a[0-9a-f]{16}$')
HANDBACK_MARK = 'The report follows:'
STATUS_LABEL = {'completed': '작업 끝', 'done': '작업 끝', 'failed': '실패', 'killed': '중지됨'}   # end notice · turn status → title of the flow card
STATUS_KEY = {'completed': 'event.notify.done', 'done': 'event.notify.done', 'failed': 'event.notify.failed', 'killed': 'event.notify.killed'}   # notify status -> the dictionary key of its card title
DEBATE_TTL = 30           # seconds after which a debate judgment is made again whatever the signature says
WALK_EVERY = 60           # seconds between two walks of the repository folders (background)
WALK_ENABLED = False      # the server turns the background walk on (server.py main); tests and tools read only what they ask for
_WALK_SLOT = threading.Semaphore(1)
LATER_AFTER = 10         # seconds after the first link scan is ready: the latest the work that waits for the first picture starts, whether or not a picture was built
MAX_NEST = 4             # how many levels of `claude -p` inside `claude -p` one page follows
USER_DUP_SEC = 120       # if the same user instruction is recorded again within this time, it counts once


class LaterWork:
    """The work that can wait for the first picture of the server: the second pass of the link index (`LINKS.start_deep`, when the index has one) and the walk of the
    repository folders. Both compete with the first read of a session for the interpreter, so they start when the first `state()` has been built or `LATER_AFTER`
    seconds after the first link scan was ready, whichever is first. Only the server arms it; tests and tools open the gate and nothing starts."""

    def __init__(self):
        self.event = threading.Event()
        self.armed, self.seconds, self._since = False, LATER_AFTER, None
        self._lock = threading.Lock()

    def arm(self, seconds=LATER_AFTER):
        """Called once by the server. The latest start is then checked by `tick` (the registry loop calls it): no thread waits for it."""
        self.armed, self.seconds, self._since = True, seconds, None

    def tick(self):
        """Opens the gate `seconds` after the first link scan became ready (the second pass builds on that scan, and the scan of a big HOME can take longer than `seconds`)."""
        if not self.armed or self.event.is_set():
            return
        ready = getattr(LINKS, 'ready', None)
        if ready is not None and not ready.is_set():
            return
        now = time.monotonic()
        if self._since is None:
            self._since = now
        if now - self._since >= self.seconds:
            self.open()

    def is_open(self):
        return self.event.is_set()

    def open(self):
        if self.event.is_set():
            return
        with self._lock:
            if self.event.is_set():
                return
            self.event.set()
        start = getattr(LINKS, 'start_deep', None) if self.armed else None
        if start:
            try:
                start()
            except Exception as e:   # noqa: BLE001 — the second pass is an extra: the board goes on with the first
                print('link second pass start error', type(e).__name__, flush=True)


LATER = LaterWork()


def notify_card(status):
    """(title, extra, title_key) of the notify card of an agent that ended. The title is the old Korean status label (or the raw status); `extra` adds `status`, a code
    (done | failed | killed, anything else as it came); title_key is the dictionary key of the label."""
    return STATUS_LABEL.get(status, status), {'status': {'completed': 'done'}.get(status, status)}, STATUS_KEY.get(status)


def tail_records(tail):
    """(restarted, an iterator of (records, torn)) over the new complete lines of a record file; a line is parsed when it is reached and let go after it is fed, so
    the first read of a big record does not hold every parsed line at once. `restarted`: the file shrank and is read again from its start, so whatever was derived
    from the old lines (the run trackers) is rebuilt from the same lines instead of counting them twice. (A tail only moves forward, except when it starts over:
    a position that is smaller after the read than before it is a restart.)"""
    before = tail.pos
    lines = tail.read()
    return tail.pos < before, (parse_records(raw) for raw in lines)


def count_torn(tracker, recs, torn):
    """A torn line is counted on the tracker of its file: recovered when a record could still be read from it, lost when none could (as RunTracker.feed_raw does)."""
    if torn:
        if recs:
            tracker.torn += 1
        else:
            tracker.lost += 1


def session_procs():
    """{session id: runstate.Proc} of the processes that hold each Claude session, from ~/.claude/sessions/<pid>.json: alive True / False (no such process, another
    command, or a pid that now belongs to another process, user or pid namespace: lineage.holds) / None (it cannot be told). Several files for one session are one Proc
    (runstate.proc_snapshot). The `status` of the file (busy, idle ...) is a hint the judgment may only use as a diagnostic."""
    table = {}
    for pid, d, alive in lineage.claims():
        sid = d.get('sessionId')
        if not isinstance(sid, str):
            continue
        ts = d.get('statusUpdatedAt') or d.get('updatedAt')
        ts = ts / 1000.0 if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 10 ** 11 else ts
        table.setdefault(sid, []).append((pid, alive, d.get('status'), ts if isinstance(ts, (int, float)) else None))
    return {sid: RS.proc_snapshot(entries) for sid, entries in table.items()}


class CodexLinker:
    """The Codex agents inside one Claude session. Ownership is decided by the global LINKS; this class handles this session's Bash calls (background jobs, end, stop),
    turn ↔ call, the planned -o paths, reports confirmed by sha1, and the flow events."""

    def __init__(self, s):
        self.s = s
        self.calls = {}          # tool_use_id -> call
        self.order = []          # in time order
        self.bg = {}             # background job id -> tool_use_id
        self.agents = {}
        self.tails = {}
        self._gone = {}          # threads that lost ownership: tid -> (agent, tail). If owned again they are revived as they were (events are not duplicated because emitted blocks them)
        self.emitted = set()
        self._sha = {}

    # ---- from this session's main record ----
    def note_bash(self, d, ts, b):
        c = cx_parse_call(ts, b.get('id'), b.get('input') or {}, d.get('cwd'))
        if c and c['id'] not in self.calls:
            self.calls[c['id']] = c
            self.order.append(c)

    def note_result(self, tuid, d, ts):
        c = self.calls.get(tuid)
        if not c:
            return
        r = d.get('toolUseResult') if isinstance(d.get('toolUseResult'), dict) else {}
        if r.get('backgroundTaskId'):
            c['bg'] = r['backgroundTaskId']
            self.bg[c['bg']] = tuid
        elif c['end'] is None:
            c['end'] = {'ts': ts, 'status': 'completed', 'exit': None}   # foreground run: when the result comes, the process has ended

    def note_notification(self, text, ts):
        tu = re.search(r'<tool-use-id>([^<]+)</tool-use-id>', text)
        tk = re.search(r'<task-id>([^<]+)</task-id>', text)
        c = self.calls.get(tu.group(1)) if tu else None
        if not c and tk:
            c = self.calls.get(self.bg.get(tk.group(1)))
        if not c:
            return False
        ex = re.search(r'exit code (\d+)', text)
        c['end'] = {'ts': ts, 'status': (re.search(r'<status>([^<]+)</status>', text) or [None, 'completed'])[1],
                    'exit': int(ex.group(1)) if ex else None}
        return True

    def note_stop(self, task_id, ts):
        tu = self.bg.get(task_id)
        if not tu:
            return False
        self.calls[tu]['stopped'] = ts
        return True

    # ---- the Codex side ----
    def poll(self):
        if not LINKS.ready.is_set():
            return False
        changed = False
        owned = LINKS.owned_by(self.s.id)
        for tid in [t for t in self.agents if t not in owned]:      # linked → ambiguous or another session: it is no longer this session's agent
            self._gone[tid] = (self.agents.pop(tid), self.tails.pop(tid))
            self.s.agents.pop(tid, None)                             # the events (feed) and idx already given are left as they are
            changed = True
        for tid, info in owned.items():
            a = self.agents.get(tid)
            if a is None and tid in self._gone:
                a, self.tails[tid] = self._gone.pop(tid)
                self.agents[tid] = self.s.agents[tid] = a
                changed = True
            if a is None:
                e = CODEX.get(tid)
                if not e:
                    continue
                a = CodexAgent(e, info)
                a.spawn_ts = info['bash_ts'] or e['meta_ts']     # one linked by process lineage (rule 'proc') does not know the Bash call: the time the thread started
                try:
                    size = os.path.getsize(e['path'])
                except OSError:
                    continue
                a.partial = cx_start_offset(e['path'], size)
                self.agents[tid] = a
                self.s.agents[tid] = a
                self.tails[tid] = Tail(e['path'], start=a.partial, max_read=CX_MAX_READ)
                changed = True
            a.link = info
            fed = False
            before = self.tails[tid].pos
            lines = self.tails[tid].read()
            if self.tails[tid].pos < before:
                a.reset_runs()                               # the rollout is read again from its start: rebuild the runs from the same lines
            for raw in lines:
                r = cx_decode(raw)
                if r:
                    try:
                        a.feed_cx(r)
                    except Exception as e:   # noqa: BLE001 — one line must not stop the whole board
                        line_error(tid, {'timestamp': r['ts']}, e)
                    fed = True
            if fed:
                cx_sync_base(a.tokens, a.partial, a.thread_total, a.seen, a.model)
                cx_sync_guardians(a.tokens, tid)
                changed = True
            if self._derive(a):
                changed = True
            a.trim_turns()                                   # cut after the events are made (even if a lot is read at once, the events of the earlier turns remain)
        # when there is no report name, the model name (sol6.1). If several have the same model, sol6.1, sol6.1-2, … in order of start time (the same after a restart)
        order = sorted(self.agents.values(), key=lambda x: (x.meta_ts or 0, x.id))
        numbered = model_numbers([a for a in order if not a.report_tag], lambda a: cx_model_short(a.model))
        for a in order:
            tag = a.report_tag or numbered[a.id]
            if a.tag != tag:
                a.tag = tag
                changed = True
        return changed

    def _turn_call(self, tid, i, t):
        """The call that started a turn, and the run (L) within it: a call within 30 s before the turn start whose instruction matches, or that mentioned this thread id."""
        user = (t['user'] or '').strip()
        best = (None, None)
        for c in reversed(self.order):
            if c['ts'] is None or c['ts'] > (t['start'] or 0) + 1:
                continue
            if (t['start'] or 0) - c['ts'] > CX_WINDOW:
                break
            for L in c['L']:
                if L['literal'] >= CX_PROMPT_MIN and user and L['rx'].fullmatch(user) and \
                        (L['resume'] is None if i == 0 else True) and \
                        (not L['resume'] or '$' in L['resume'] or L['resume'] == tid):
                    return c, L
            if tid in c.get('resumes', ()) and best[0] is None:                    # the call that resumed this thread (a UUID in a path says nothing)
                best = (c, None)
        return best

    def _resolve_out(self, c, L, user):
        if not L or not L['out']:
            return None
        m = L['rx'].fullmatch((user or '').strip())
        env = dict(c['assigns'], **L.get('env', {}))      # a run inside a script is resolved with that script's variables (L['env'])
        if m:
            for g, var in L['names']:
                if var and m.group(g) is not None:
                    env.setdefault(var, m.group(g))
        p = _cx_expand(L['out'], env)
        if not p:
            return None
        if not os.path.isabs(p):
            if not L['scwd'] or '$' in L['scwd']:
                return None
            p = os.path.join(L['scwd'], p)
        return os.path.normpath(p)

    def _sha_file(self, path):
        """sha1 of the -o file. The same policy as the document view: a denied target, a non-regular file (FIFO) or one over FILE_MAX is a missing file (None).
        What opens it is open_safe, which redoes the deny check right before opening."""
        plain = stat_plain(path)
        if not plain or plain[1].st_size > FILE_MAX:
            return None
        st = plain[1]
        key = (path, st.st_mtime, st.st_size)
        if key not in self._sha:
            try:
                with open_safe(path, binary=True) as f:
                    self._sha[key] = hashlib.sha1(f.read(FILE_MAX + 1)).hexdigest()
            except OSError:
                return None
        return self._sha[key]

    def _cand_files(self, t):
        """A turn whose -o could not be expanded: .md/.txt files changed around the end time, in the folders of the paths in the instruction (+ r*/)."""
        dirs = []
        for p in re.findall(r'(/[\w.\-~/]+)', t['user'] or ''):
            d = p if os.path.isdir(p) else os.path.dirname(p)
            for x in [d, os.path.dirname(d)] + glob.glob(os.path.join(d, 'r[0-9]*')):
                if os.path.isdir(x) and x not in dirs and x != '/':
                    dirs.append(x)
        out = []
        for d in dirs[:20]:
            for f in glob.glob(os.path.join(d, '*.md')) + glob.glob(os.path.join(d, '*.txt')):
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    continue
                if (t['end'] or 0) - 2 <= mt <= (t['end'] or 0) + 30:
                    out.append(f)
        return out

    @staticmethod
    def _unresolved_out(a, raw):
        """A `-o` path that names a variable no scope of the call defines (none is guessed): the seat judgment still learns that this thread writes a report
        somewhere it cannot name (`path_unresolved`)."""
        names = sorted(set(re.findall(r'\$\{?([A-Za-z_]\w*)', raw)))
        if names and not any(r.op == '-o' and r.path_raw == raw for r in a.redirects):
            a.redirects.append(Redirect(fd=1, op='-o', path_raw=raw, path_resolved=None, unresolved_vars=names))

    def _set_out(self, a, t, path):
        if path and not any(o['path'] == path for o in a.out_paths):
            a.out_paths.append({'ts': t['start'], 'path': path})
        m = REPORT_RE.search(path or '')
        if m and not a.report_tag:
            a.report_tag = a.tag = m.group(3)

    def _derive(self, a):
        """Once per turn: finding the call, the planned -o path, the flow events (spawn/orch_msg, handback/notify, stop), and sha1 confirmation."""
        changed, tid, now = False, a.id, time.time()
        for t in a.turns:
            i = t['n']
            if not t.get('mapped') and (t['user'] is not None or t['end']):
                t['mapped'] = True
                c, L = self._turn_call(tid, i, t)
                if c:
                    t['call'], t['bash_ts'] = c['id'], c['ts']
                    out = self._resolve_out(c, L, t['user'])
                    if out:
                        t['out'], t['out_state'] = out, 'planned'
                        self._set_out(a, t, out)
                    elif L and L.get('out') and '$' in L['out']:
                        self._unresolved_out(a, L['out'])
                changed = True
            if t.get('mapped') and ('in', i, tid) not in self.emitted:
                self.emitted.add(('in', i, tid))
                c = self.calls.get(t['call'])
                ts = t['bash_ts'] or t['start']
                text = t['user'] or ''
                if i == 0:
                    title = os.path.basename(t['out']) if t['out'] else trunc(text.strip().splitlines()[0] if text.strip() else '', 80)
                    self.s._event(ts, 'spawn', 'orch', tid, title, text, agent=tid,
                                  extra={'tool_use_id': c['id'] if c else None, 'model': a.model})
                else:
                    self.s._event(ts, 'orch_msg', 'orch', tid, (c['desc'] if c and c['desc'] else '메시지'), text,
                                  agent=tid, title_key=None if c and c['desc'] else 'event.message.title')
                changed = True
            if t['end'] and ('end', i, tid) not in self.emitted:
                self.emitted.add(('end', i, tid))
                if t['status'] == 'done' and t['msg']:
                    self.s._event(t['end'], 'handback', tid, 'orch', '최종 보고', t['msg'], agent=tid, title_key='event.handback.title')
                summary = t['error'] or trunc((t['msg'] or '').strip().splitlines()[0] if (t['msg'] or '').strip() else '', 160)
                title, extra, key = notify_card(t['status'])
                self.s._event(t['end'], 'notify', tid, 'orch', title, summary, agent=tid, extra=extra, title_key=key)
                a.notifications.append({'ts': t['end'], 'status': {'done': 'completed'}.get(t['status'], t['status']),
                                        'summary': summary, 'tokens': None, 'tools': None, 'duration_ms': None})
                changed = True
            c = self.calls.get(t['call'])
            if c and c['stopped'] and ('stop', i, tid) not in self.emitted and \
                    (t['end'] is None or t['status'] == 'killed'):
                self.emitted.add(('stop', i, tid))
                if t['end'] is None:
                    t['status'] = 'killed'
                self.s._event(c['stopped'], 'stop', 'orch', tid, '에이전트 중지', agent=tid, title_key='event.stop.title')
                changed = True
            if t['end'] and t['sha'] and t['out_state'] != 'confirmed' and not t.get('given_up'):
                paths = [t['out']] if t['out'] else self._cand_files(t)
                hit = next((p for p in paths if self._sha_file(p) == t['sha']), None)
                if hit:
                    t['out'], t['out_state'] = hit, 'confirmed'
                    a.writes.append({'ts': t['end'], 'path': hit})
                    self._set_out(a, t, hit)
                    changed = True
                elif now - t['end'] > 60:
                    t['given_up'] = True          # the -o file appears within 1 second of the end. After 1 minute it is not looked at again
        return changed

    def verdict(self, a, now):
        """The runstate.Verdict of a Codex thread: its own turns (a run is a turn), what the launching Bash call recorded (the end notice of the background
        job, TaskStop) and whether a codex process has the rollout open (True / False / None: `ps` cannot say)."""
        calls = [t['call'] for t in a.turns if t.get('call')] or [(a.link or {}).get('call')]
        node = (a.link or {}).get('node')
        ledger = self.s.agents[node].ledger if node in self.s.agents else getattr(self.s, 'ledger', None)
        f = RS.facts_of(a.runs, 'codex', ledger=ledger, launch_calls=[c for c in calls if c], spawn_ts=a.spawn_ts)
        f.last_ts = max(f.last_ts or 0.0, a.last_ts or 0.0) or None
        f.pending = [p['ts'] for p in a.pending.values() if p.get('ts')]
        return RS.judge(f, RS.Proc(cx_open(a.path, a.id, cx_procs())), now)


class Session:
    def __init__(self, path):
        self.path = path
        self.id = os.path.basename(path)[:-len('.jsonl')]
        self.dir = path[:-len('.jsonl')]
        self.tail = Tail(path)
        self.agent_tails = {}
        self.agents = {}
        self.feed = []
        self.title = ''
        self.slug = ''
        self.cwd = ''
        self.orch = {'state': 'idle', 'last_action': '', 'last_action_ts': None, 'last_say': '',
                     'last_say_ts': None, 'last_ts': None, 'pending_bg': 0,
                     'model': '', 'effort': ''}
        self.orch_tokens = TokenMeter()
        self.spawns = {}          # toolUseId -> {ts, description, prompt}
        self.pending_q = {}       # AskUserQuestion waiting for an answer: tool_use id -> {ts, questions}
        self.first_ts = None
        self.notif_seen = set()
        self.user_seen = {}         # the whole text of a user instruction -> the last time it was emitted (to filter double records)
        self._pending_notifs = {}   # notices that came before the meta file
        self._routed = {}           # agent id -> the number of child completion notices from its record that were moved (grandchild state)
        self._derived = {}          # per agent, how far events have been made: [number of sent messages, number of reads, {path: ts of the last event}, number of cli conversation items]
        self._sorted = False
        self.version = 0
        self.lock = threading.RLock()
        self._file_cache = {}
        self._head_cache = {}       # the head of a text like brief.md: path -> ((mtime_ns, size), text)
        self.provider = 'claude'
        self.codex = CodexLinker(self)
        self._cli_alive = set()     # ids of the live child Claude sessions (claude -p)
        self._cli_gone = {}         # child sessions that lost ownership: id -> (agent, tail). If owned again they are revived as they were
        self._cli_spawn = {}        # spawn events of child sessions whose body (first instruction) has not been filled in yet: id -> event
        self._sys_limit = None                # the feed event of the last usage-limit line, which the notice that follows it ("continuing automatically") amends
        self.runs = RS.RunTracker(self.id)    # the runs of this record file (the orchestrator's: a usage limit it waits on, board/runstate.py)
        self.ledger = RS.Ledger()             # what the main record knows about the agents it launched: notices, TaskStop calls, background task ids
        self.parse_errors = 0                 # lines whose handling raised an error (the line is skipped, the board goes on)
        self._cli_procs = {}                  # child session id -> runstate.Proc, rebuilt at every state()
        self._verdicts = {}                   # agent id -> runstate.Verdict of the last state()
        self._orch_verdict = RS.OrchVerdict('idle')
        self._orch_proc = RS.Proc(None)       # the process of the orchestrator as the last state() saw it
        self.cx_turn = RS.TurnMarks()         # a Codex thread's turn: `task_started` and what it does open it, `task_complete` and `turn_aborted` close it
        self._diag = None                     # the diagnostics of the last state() (board/diag.py)
        self.walked_units, self.walk_capped, self.walk_gen = [], False, 0
        self._walk_thread, self._walk_at = None, time.monotonic() - WALK_EVERY + 5       # the first walk comes a few seconds after the session is opened

    # ---------- reading ----------
    def poll(self):
        changed = False
        with self.lock:
            restarted, lines = tail_records(self.tail)
            if restarted:
                self.runs, self.ledger = RS.RunTracker(self.id), RS.Ledger()
            for recs, torn in lines:
                count_torn(self.runs, recs, torn)
                for d in recs:
                    if not isinstance(d, dict):
                        continue
                    failed = None
                    try:
                        rc = RS.rec(d)
                        self.runs.feed(d, rc)
                        self.ledger.feed(d, rc)
                    except Exception as e:   # noqa: BLE001 — a field of the wrong type must not stop the session from opening
                        failed = e
                    try:
                        self._feed_main(d)
                    except Exception as e:   # noqa: BLE001 — one line must not stop the whole board
                        failed = failed or e
                    if failed:
                        self.parse_errors += 1
                        line_error(self.id, d, failed)
                    changed = True
            for mp in glob.glob(os.path.join(self.dir, 'subagents', 'agent-*.meta.json')):
                aid = os.path.basename(mp)[len('agent-'):-len('.meta.json')]
                if aid not in self.agents:
                    try:
                        with open(mp) as f:
                            meta = json.load(f)
                    except (OSError, ValueError):
                        continue
                    a = Agent(aid, meta)
                    sp = self.spawns.get(a.tool_use_id)
                    if sp:
                        a.spawn_ts = sp['ts']
                    self.agents[aid] = a
                    self.agent_tails[aid] = Tail(os.path.join(self.dir, 'subagents', 'agent-%s.jsonl' % aid))
                    changed = True
            if self.provider == 'claude':
                # Claude Code sessions started from Bash (claude -p): as sub-agents (when started with the model and effort set directly).
                # If the ownership changes (cancelled, or reassigned to another parent) it is no longer this session's agent. The events and idx already given are left as they are,
                # and if it is owned again the same agent and tail are revived (the spawn event is not made again)
                owned = self._cli_children()
                for csid in [c for c, a in self.agents.items() if a.origin == 'cli' and c not in owned]:
                    self._cli_gone[csid] = (self.agents.pop(csid), self.agent_tails.pop(csid))
                    changed = True
                for csid, o in owned.items():
                    if csid in self._cli_gone:
                        a, self.agent_tails[csid] = self._cli_gone.pop(csid)
                        a.cli, a.spawn_ts = o, o['bash_ts']
                        self.agents[csid] = a
                        a.redirects = self._redirects_of(csid)
                        changed = True
                        continue
                    if csid in self.agents:
                        self.agents[csid].cli = o
                        self.agents[csid].redirects = self._redirects_of(csid)
                        continue
                    paths = glob.glob(os.path.join(PROJECTS, '*', csid + '.jsonl'))
                    if not paths:
                        continue
                    a = Agent(csid, {'description': o['bash_desc'] or 'claude -p'})
                    a.origin, a.spawn_ts, a.cli = 'cli', o['bash_ts'], o
                    a.redirects = self._redirects_of(csid)
                    self.agents[csid] = a
                    self.agent_tails[csid] = Tail(paths[0])
                    self._cli_spawn[csid] = self._event(o['bash_ts'], 'spawn', self.launcher_of(a), csid, o['bash_desc'] or 'Claude Code 실행', '', agent=csid,
                                                    title_key=None if o['bash_desc'] else 'event.spawn_cli.title')
                    changed = True
            for aid, t in self.agent_tails.items():
                a = self.agents[aid]
                restarted, lines = tail_records(t)
                if restarted:
                    a.reset_runs()
                for recs, torn in lines:
                    count_torn(a.runs, recs, torn)
                    for d in recs:
                        if not isinstance(d, dict):
                            continue
                        try:
                            a.feed(d)
                        except Exception as e:   # noqa: BLE001
                            self.parse_errors += 1
                            line_error(aid, d, e)
                        changed = True
            self._route_child_notes()
            if self.codex and self.codex.poll():
                changed = True
            if self.provider == 'claude':
                self._walk_later()
            if changed:
                self._agent_events()
                if not self._sorted:      # once, at the first read: the agent-side events are slotted in time order
                    self.feed.sort(key=lambda e: e['ts'] or 0)
                    self._sorted = True
                self.version += 1
        return changed

    def _cli_children(self):
        """The `claude -p` children shown on this page: the ones this session launched (itself or through one of its sub-agents), and, one level of owner at a time,
        the ones that a child of this page launched in turn (the page of the top orchestrator shows a grand-child under the child that launched it)."""
        by_tree = {}
        for c, o in list(LINKS.cli_owners.items()):                 # one pass: the children of every tree
            by_tree.setdefault(o['sid'], {})[c] = o
        owned = dict(by_tree.get(self.id, ()))
        frontier = list(owned)
        for _ in range(MAX_NEST):
            nxt = {}
            for csid in frontier:
                nxt.update({c: o for c, o in by_tree.get(csid, {}).items() if c not in owned and c != self.id})
            owned.update(nxt)
            frontier = list(nxt)
            if not frontier:
                break
        return owned

    def launcher_of(self, a):
        """Who an agent hangs under on the page: 'orch' (this session's orchestrator), or the id of the sub-agent or `claude -p` child that launched it."""
        if a.origin == 'cli' and a.cli:
            node, tree = a.cli.get('node'), a.cli.get('sid')
            if node:
                return node
            if tree and tree != self.id:
                return tree
        elif a.origin == 'subagent' and a.parent_agent:
            return a.parent_agent
        return 'orch'

    def _redirects_of(self, csid):
        """The output redirects of the launching command that name this child's report, for the seat judgment of board/debates.py. Only the launches that can be
        this child's (the command reader's veto) count; when more than one launch could be, none: a seat taken from another child's redirect is worse than none."""
        dec = LINKS.decisions.get(csid)
        if dec is None or dec.call is None or not dec.child.runs:
            return []
        run = dec.child.runs[0]
        cand = [L for L in dec.call.launches if L.reader and affil.launch_ok(L, dec.child, run)]
        return list(cand[0].redirects) if len(cand) == 1 else []

    def _event(self, ts, kind, frm, to, title, text='', agent=None, extra=None, title_key=None, questions=None):
        """Appends a feed event. `title` and `text` are the old Korean fields (unchanged). The fields below are added after them:
        title_key = the dictionary key of the title when the server wrote it (title_i18n {key, params}; title_is_default is true for a stand-in title, false for a
        status label); questions = the structured body of a choice question."""
        ev = {'ts': ts, 'kind': kind, 'from': frm, 'to': to, 'title': title, 'text': text, 'agent': agent}
        if extra:
            ev.update(extra)
        if title_key:
            ev['title_i18n'] = {'key': title_key, 'params': {}}
            ev['title_is_default'] = not title_key.startswith('event.notify.')
        if questions is not None:
            ev['questions'] = questions
        self.feed.append(ev)
        return ev

    def _orch_tool(self, ts, name, text):
        """The orchestrator's last action."""
        self.orch['last_action'] = '%s: %s' % (name, trunc(text, 160))
        self.orch['last_action_ts'] = ts

    def _sys_event(self, ts, code, status=None, at=None, auto=False):
        """A system line of the feed (kind `sys`): something the board saw in the record that the orchestrator did not say. `title` is the old Korean field; `title_i18n`
        carries the dictionary key and the numbers (status, and the reset time `at` as epoch seconds) the page words it from, and `sys` the plain values."""
        key, params, ko = views.sys_line(code, status, at, auto)
        ev = self._event(ts, 'sys', 'sys', 'user', ko, '', title_key=key)
        ev['title_i18n']['params'] = params
        ev['sys'] = {'code': code, 'status': status, 'resets_at': at, 'auto': auto}
        return ev

    def _sys_error(self, ts, err):
        """An API error line: a usage limit (with its reset time when the record has one), a server-side hiccup that passes, a request the server refuses, a login that expired."""
        state, reason = RS.classify_error(err)
        if reason == 'limit':
            self._sys_limit = self._sys_event(ts, 'limit', err.status, err.resets_at)
        else:
            self._sys_limit = None
            code = 'api_error' if state == 'interrupted' else 'auth' if 'auth' in (err.type or '').lower() else 'rejected'
            self._sys_event(ts, code, err.status)

    def _sys_notice(self, ts, kind):
        """The informational notice after a limit line: `auto` (it continues by itself) amends the line before it, `reset` (the limit is over) is a line of its own."""
        last = self._sys_limit
        if kind == 'auto' and last is not None and (ts or 0) - (last['ts'] or 0) < 60 and not last['sys']['auto']:
            last['sys']['auto'] = True
            key, params, ko = views.sys_line('limit', last['sys']['status'], last['sys']['resets_at'], True)
            last['title'], last['title_i18n'] = ko, {'key': key, 'params': params}
        elif kind == 'reset':
            self._sys_limit = None
            self._sys_event(ts, 'reset')

    def _feed_main(self, d):
        typ = d.get('type')
        ts = RS.rec(d).ts
        o = self.orch
        if ts and not self.first_ts:
            self.first_ts = ts
        if d.get('cwd'):
            self.cwd = d['cwd']
        if d.get('slug'):
            self.slug = d['slug']
        if typ == 'ai-title':
            self.title = d.get('aiTitle') or self.title
        elif typ == 'system' and d.get('subtype') == 'turn_duration':
            o['pending_bg'] = d.get('pendingBackgroundAgentCount') or 0
        elif typ == 'system' and d.get('subtype') == 'informational':
            self._sys_notice(ts, RS.notice_kind(d))
        elif typ == 'assistant':
            o['last_ts'] = ts
            if d.get('effort'):
                o['effort'] = d['effort']
            m = d.get('message') or {}
            if m.get('model') and not m['model'].startswith('<'):
                o['model'] = m['model']
            self.orch_tokens.add(m)
            err = RS.error_of(d)
            limit_line = err is not None          # an API error line ("You've hit your limit ...") is the board's news, not something the orchestrator said to the user
            if limit_line:
                self._sys_error(ts, err)          # ... so it goes to the feed as a system line, worded by the page with numbers and a time
            for b in m.get('content') or []:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text' and b.get('text', '').strip() and not limit_line:
                    o['last_say'], o['last_say_ts'] = b['text'].strip(), ts
                    self._event(ts, 'orch_say', 'orch', 'user', '사용자에게 보고', b['text'].strip(), title_key='event.orch_say.title')
                elif b.get('type') == 'tool_use':
                    name, inp = b.get('name', ''), b.get('input') or {}
                    self._orch_tool(ts, name, tool_brief(name, inp))
                    if name == 'Bash':
                        self.codex.note_bash(d, ts, b)
                    if name == 'Agent':
                        self.spawns[b.get('id')] = {'ts': ts, 'description': inp.get('description', ''),
                                                    'prompt': inp.get('prompt', '')}
                        agent = next((a for a in self.agents.values() if a.tool_use_id == b.get('id')), None)
                        if agent:
                            agent.spawn_ts = ts
                        self._event(ts, 'spawn', 'orch', b.get('id'), inp.get('description', ''),
                                    inp.get('prompt', ''), extra={'tool_use_id': b.get('id'),
                                                                  'model': inp.get('model', '')})
                    elif name == 'SendMessage':
                        to = str(inp.get('to', ''))
                        self._event(ts, 'orch_msg', 'orch', to, inp.get('summary') or '메시지',
                                    inp.get('message') if isinstance(inp.get('message'), str)
                                    else json.dumps(inp.get('message'), ensure_ascii=False), agent=to,
                                    title_key=None if inp.get('summary') else 'event.message.title')
                    elif name == 'AskUserQuestion':
                        qs = inp.get('questions') or []
                        self.pending_q[b.get('id')] = {'ts': ts, 'questions': qs}
                        lines = []
                        for q in qs:
                            lines.append('**%s** %s' % (q.get('header') or '질문', q.get('question') or ''))
                            lines += ['- %s — %s' % (op.get('label', ''), op.get('description', '')) for op in q.get('options') or []]
                            lines.append('')
                        self._event(ts, 'orch_ask', 'orch', 'user', '선택지 질문', '\n'.join(lines).strip(), title_key='event.orch_ask.title',
                                    questions=[{'header': q.get('header') or None, 'question': q.get('question') or '',
                                                'options': [{'label': op.get('label', ''), 'description': op.get('description', '')} for op in q.get('options') or []]}
                                               for q in qs])
                    elif name == 'TaskStop':
                        tid = inp.get('task_id') or ''
                        self.codex.note_stop(tid, ts)      # the case where the Bash background job that started Codex was stopped
                        if AGENT_ID_RE.match(tid):
                            self._event(ts, 'stop', 'orch', tid, '에이전트 중지', agent=tid, title_key='event.stop.title')
        elif typ == 'user':
            if RS.is_work(d):                                     # a local command's echo or a meta line gives the orchestrator nothing to do
                o['last_ts'] = ts
            origin = d.get('origin') or {}
            kind = origin.get('kind')
            c = (d.get('message') or {}).get('content')
            text = c if isinstance(c, str) else '\n'.join(
                b.get('text') or '' for b in (c or []) if isinstance(b, dict) and b.get('type') == 'text')
            for b in (c if isinstance(c, list) else []):
                if isinstance(b, dict) and b.get('type') == 'tool_result':
                    self.codex.note_result(b.get('tool_use_id'), d, ts)
                    q = self.pending_q.pop(b.get('tool_use_id'), None)
                    if q is not None:                    # the user's answer to a choice question
                        r = d.get('toolUseResult') if isinstance(d.get('toolUseResult'), dict) else {}
                        ans = r.get('answers') if isinstance(r.get('answers'), dict) else None
                        if ans:
                            hdr = {x.get('question'): x.get('header') for x in q['questions']}
                            text = '\n'.join('**%s** %s' % (hdr.get(k) or k, v) for k, v in ans.items())
                        else:
                            raw = b.get('content')
                            raw = raw if isinstance(raw, str) else ' '.join(x.get('text', '') for x in raw or [] if isinstance(x, dict))
                            text = raw.split(' Read the answers carefully')[0]
                            for pre in ('The user answered: ', 'Your questions have been answered: ', "User has answered your questions: "):
                                text = text.replace(pre, '')
                        self._event(ts, 'user_answer', 'user', 'orch', '선택', text.strip(), title_key='event.user_answer.title')
            if kind == 'human':
                self._user_say(ts, text)
            elif kind == 'task-notification' or text.startswith('<task-notification>'):
                self._notification(ts, text)
            elif kind == 'peer':
                frm = origin.get('from') or ''
                body = origin.get('body') or text
                if origin.get('handback') or 'Subagent hand-back' in body:
                    rep = body.split(HANDBACK_MARK, 1)[-1]
                    rep = '\n'.join(line[2:] if line.startswith('  ') else line for line in rep.split('\n')).strip()
                    self._event(ts, 'handback', frm, 'orch', '최종 보고', rep, agent=frm, title_key='event.handback.title')
                else:
                    self._event(ts, 'peer', frm, 'orch', '메시지', body, agent=frm, title_key='event.message.title')
        elif typ == 'attachment':
            a = d.get('attachment') or {}
            if a.get('type') == 'model':
                self.orch_tokens.model((a.get('identity') or {}).get('modelId'))
            if a.get('type') == 'queued_command':
                mode = a.get('commandMode')
                ats = parse_ts(a.get('timestamp')) or ts
                if mode == 'prompt' and (a.get('origin') or {}).get('kind') == 'human':
                    self._user_say(ats, as_text(a.get('prompt')))
                elif mode == 'task-notification':
                    self._notification(ats, as_text(a.get('prompt')))

    def _agent_events(self):
        """What passed between agents: messages sent to other agents (agent_msg), reading other participants' reports (xread). The conversation of claude -p child sessions (_cli_talk) is also produced here."""
        writer, names = writer_table(self), {}
        for a in self.agents.values():
            names[a.id] = a.id
            for t in (a.tag, a.auto_tag):
                if t:
                    names[t.lower()] = a.id
            names[a.description.lower()] = a.id
        for a in self.agents.values():
            d = self._derived.setdefault(a.id, [0, 0, {}, 0])
            for m in a.sent[d[0]:]:
                to = names.get(m['to'].lower())
                peer = to if to and to != a.id else None
                self._event(m['ts'], 'agent_msg', a.id, peer or 'orch', m['summary'] or '메시지', m['text'], agent=a.id,
                            extra={'peer': peer}, title_key=None if m['summary'] else 'event.message.title')
            d[0] = len(a.sent)
            for ts, path in a.read_log[d[1]:]:
                m, au = REPORT_RE.search(path), writer.get(path)
                if not m or not au or au == a.id or (ts or 0) - d[2].get(path, -1e9) < 600:
                    continue
                d[2][path] = ts or 0
                self._event(ts, 'xread', au, a.id, 'r%s/%s.md' % (m.group(2), m.group(3)), '', agent=a.id,
                            extra={'author': au, 'path': path})
            d[1] = len(a.read_log)
            if a.origin == 'cli':
                self._cli_talk(a, d)

    def _cli_talk(self, a, d):
        """The conversation of a claude -p child session: the first instruction goes into the body of the spawn event already made (so the event number stays), later instructions are orch_msg, and the last text of a turn that ended with end_turn is handback."""
        for it in a.talk[d[3]:]:
            if it['kind'] == 'end':
                self._event(it['ts'], 'handback', a.id, 'orch', '최종 보고', it['text'], agent=a.id, title_key='event.handback.title')
                continue
            sp = self._cli_spawn.pop(a.id, None)
            if sp is not None:
                sp['text'] = it['text']
            else:
                self._event(it['ts'], 'orch_msg', 'orch', a.id, '메시지', it['text'], agent=a.id, title_key='event.message.title')
        d[3] = len(a.talk)

    def _user_say(self, ts, text, raw=False):
        """The user instruction event. If the whole text is the same and the time difference is within USER_DUP_SEC, it is taken as a double record of the same instruction (a queued_command and a human line) and filtered out.
        With raw=True (Codex) the stripping of <system-reminder> and the filtering of text starting with '<' are skipped (codex_user_text has already stripped the head)."""
        if not raw:
            text = strip_reminders(text)
            if not text or text.startswith('<'):
                return
        key = text.strip()
        if key in self.user_seen:
            last = self.user_seen[key]
            if ts is None or last is None or abs(ts - last) <= USER_DUP_SEC:
                return
        self.user_seen[key] = ts
        self._event(ts, 'user_say', 'user', 'orch', '사용자 지시', text, title_key='event.user_say.title')

    def _notification(self, ts, text):
        text = as_text(text)
        if self.codex and self.codex.note_notification(text, ts):
            return                          # the Bash background job that started Codex has finished (process exit)
        tid = re.search(r'<task-id>([^<]+)</task-id>', text)
        if not tid or not AGENT_ID_RE.match(tid.group(1)):
            return
        status = (re.search(r'<status>([^<]+)</status>', text) or [None, ''])[1]
        summary = (re.search(r'<summary>([^<]+)</summary>', text) or [None, ''])[1]
        usage = re.search(r'<subagent_tokens>(\d+)</subagent_tokens>.*?<tool_uses>(\d+)</tool_uses>'
                          r'.*?<duration_ms>(\d+)</duration_ms>', text, re.S)
        self._agent_note(ts, tid.group(1), status, summary, usage.group(0) if usage else None,
                         tuple(int(x) for x in usage.groups()) if usage else None)

    def _agent_note(self, ts, aid, status, summary, usage_key, usage):
        """One completion notice for one agent (a notice in the main record; for a grandchild, a notice in the parent agent's record). The same notice is counted only once."""
        key = (aid, status, usage_key if usage_key else summary)
        if key in self.notif_seen:
            return
        self.notif_seen.add(key)
        n = {'ts': ts, 'status': status, 'summary': summary,
             'tokens': usage[0] if usage else None,
             'tools': usage[1] if usage else None,
             'duration_ms': usage[2] if usage else None}
        if aid in self.agents:
            self.agents[aid].notifications.append(n)
        else:
            self._pending_notifs.setdefault(aid, []).append(n)
        title, extra, key = notify_card(status)
        self._event(ts, 'notify', aid, 'orch', title, summary, agent=aid, extra=extra, title_key=key)

    def _route_child_notes(self):
        """Completion of a grandchild agent: for a sub-agent started by a sub-agent, the notice stays in the parent agent's record, not in the main record.
        The notices read from the parent record (task-notification) and the results of Agent tool calls (foreground completion) are moved to that agent's notices. What was moved is not moved again."""
        for a in list(self.agents.values()):
            i = self._routed.get(a.id, 0)
            for n in a.child_notes[i:]:
                u = n['usage'] if n['usage'] and None not in n['usage'] else None
                self._agent_note(n['ts'], n['task'], n['status'], n['summary'], u, u)
            self._routed[a.id] = len(a.child_notes)
            for r in a.child_results:
                if r.get('done'):
                    continue
                child = next((x for x in self.agents.values() if x.parent_agent == a.id and (x.id == r['agent_id'] or x.tool_use_id == r['tool_use_id'])), None)
                if child:                                # if none, that agent's meta file is not visible yet: try again next time
                    r['done'] = True
                    self._agent_note(r['ts'], child.id, r['status'], '', ('result', r['tool_use_id']), None)

    # ---------- derived ----------
    def alive(self):
        """(alive?, pid, name): True / False / None (no way to know the process — the page shows "session state unknown")."""
        unknown = None
        for pid, d, r in lineage.claims():
            if d.get('sessionId') != self.id:
                continue
            if r:
                return True, pid, d.get('name')
            if r is None:
                unknown = (None, pid, d.get('name'))
        return unknown or (False, None, None)

    def _attach_main_side(self):
        """Attaches the notices, reports and instructions from the main record to the agents (an agent may be found late)."""
        for aid, lst in list(self._pending_notifs.items()):
            if aid in self.agents:
                self.agents[aid].notifications.extend(lst)
                del self._pending_notifs[aid]
        for a in self.agents.values():
            a.handbacks, a.orch_msgs, a.stopped_ts = [], [], None
        for ev in self.feed:
            a = self.agents.get(ev.get('agent') or '')
            if not a:
                continue
            if ev['kind'] == 'handback':
                a.handbacks.append({'ts': ev['ts'], 'text': ev['text']})
            elif ev['kind'] == 'orch_msg':
                a.orch_msgs.append({'ts': ev['ts'], 'summary': ev['title'], 'text': ev['text']})
            elif ev['kind'] == 'stop':
                a.stopped_ts = ev['ts']

    def _launcher_ledger(self, a):
        """The Ledger of whoever launched this child: the sub-agent that holds its launching call (node), or the `claude -p` child that launched a grandchild, else the main record."""
        o = a.cli or {}
        node, tree = o.get('node'), o.get('sid')
        if node and node in self.agents:
            return self.agents[node].ledger
        if tree and tree != self.id and tree in self.agents:
            return self.agents[tree].ledger
        return getattr(self, 'ledger', None)

    def cli_procs(self):
        """{child session id: Proc} of the live process table (rebuilt by every state(); the judgment of a `claude -p` child reads it)."""
        self._procs_known = procs.table_known()
        return session_procs()

    def _cli_proc(self, sid):
        """The Proc of a child session. Not among the session files: with a process table that can be read, the plain "is this id among the live sessions" answer
        (`_cli_alive`); without one nothing can be said about it (alive None), not even that it is gone."""
        p = self._cli_procs.get(sid)
        if p is not None:
            return p
        return RS.Proc(sid in self._cli_alive if getattr(self, '_procs_known', True) else None)

    def agent_verdict(self, a, alive, now, _seen=None):
        """The runstate.Verdict (status, reason, resets_at, diagnostics) of one agent: board/runstate.py judges it from the runs of its own record, what its launcher
        recorded about it (notices, TaskStop) and the process table. `alive` is the main session's process (a sub-agent has none of its own)."""
        if a.provider == 'codex' and self.codex:
            return self.codex.verdict(a, now)
        if a.origin == 'cli':        # claude -p child session: its own process, found by its session id
            f = RS.facts_of(a.runs, 'cli', ledger=self._launcher_ledger(a), launch_calls=self.launch_calls(a.cli or {}), spawn_ts=a.spawn_ts)
        else:
            parent_over = bool(a.parent_agent and a.origin == 'subagent' and self._parent_over(a, alive, now, _seen))       # only a sub-agent ends with its parent
            parent = self.agents.get(a.parent_agent) if a.parent_agent else None
            f = RS.facts_of(a.runs, 'subagent', ledger=parent.ledger if parent is not None else getattr(self, 'ledger', None), agent_id=a.id, tool_use_id=a.tool_use_id,
                            handbacks=[h['ts'] for h in a.handbacks if h['ts']], parent_over=parent_over, spawn_ts=a.spawn_ts)
        f.last_ts = max(f.last_ts or 0.0, a.last_ts or 0.0) or None          # a notice the agent received counts as a sign of life, as it always did
        f.pending = [p['ts'] for p in a.pending.values() if p.get('ts')]    # the calls still waiting for their result: the same list the page has always shown as `pending`
        proc = self._cli_proc(a.id) if a.origin == 'cli' else RS.Proc(alive)
        return RS.judge(f, proc, now)

    @staticmethod
    def launch_calls(o):
        """The Bash calls that started the runs of a `claude -p` child, from its link (`cli_owners[child]`): the call of each run (`calls`), the ones the evidence
        names (`calls_certain`) and not those the matching only paired by order, because a TaskStop or a "background time limit" notice found on a wrong call would
        stop or time-limit the wrong child. A resumed run has its own call, so its TaskStop and time limit reach the judgment too; what an earlier run's call said is
        older than the run's records and does not count (runstate.judge). A link made before `calls` existed gives its one `call`."""
        calls = o.get('calls')
        if calls is None:
            return [o['call']] if o.get('call') else []
        sure = o.get('calls_certain') or [True] * len(calls)
        out = [c for c, ok in zip(calls, sure) if c and ok]
        if o.get('call') and o['call'] not in out:
            out.insert(0, o['call'])                          # the call the evidence named for the first run
        return out

    def agent_status(self, a, alive, now, _seen=None):
        return self.agent_verdict(a, alive, now, _seen).status

    def _parent_over(self, a, alive, now, seen):
        """Whether the grandchild's parent agent is in an ended state (done · failed · killed · ended). If the parent is unknown (meta not visible yet), no."""
        parent = self.agents.get(a.parent_agent)
        seen = (seen or ()) + (a.id,)
        if parent is None or parent.id in seen:
            return False
        return self.agent_status(parent, alive, now, seen) in ('done', 'failed', 'killed', 'ended')       # an interrupted parent may resume: it is not over

    # ---------- debates ----------
    def _debate_key(self, statuses):
        """What the debate judgment reads from the session, as a small comparable value: the statuses, the walk's generation, the launcher's folder and, per agent,
        the sizes of the things its instructions, reads, writes and messages to other agents (a room of participants only is made of them) are made of. A judgment is reused while this is the same and the folders on disk are too."""
        per = tuple((a.id, a.tag, a.cwd, a.spawn_ts, a.first_ts, a.last_ts, a.spawn_prompt is not None, len(a.received), len(a.orch_msgs), len(a.reads),
                     len(a.writes), sum(1 for w in a.writes if w.get('ok') is not None), len(a.shell_writes), sum(1 for w in a.shell_writes if w.get('ok') is not None),
                     len(a.out_paths), len(a.redirects), (a.cli or {}).get('sid'), len(a.sent), sum(1 for m in a.sent if m.get('ok') is not None))
                    for a in self.agents.values())
        return (tuple(sorted(statuses.items())), getattr(self, 'walk_gen', 0), getattr(self, 'cwd', ''), per)

    @staticmethod
    def _disk_signature(debates):
        """The state of the debate folders and files a judgment looked at, read from its own result: the cells, finals, documents and briefs, and every folder
        among them (a file made or removed there changes the folder's time). One stat each; no glob."""
        paths, dirs = [], set()
        for d in debates:
            dirs.update((d['root'], os.path.join(d['root'], 'final')))
            paths.append(os.path.join(d['root'], 'brief.md'))
            paths += [f['path'] for f in d['finals']]
            for t in d['topics']:
                dirs.add(t['dir'])
                paths += [os.path.join(t['dir'], n) for n in ('brief.md', 'README.md', 'index.md')]
                if t['final']['path']:
                    paths.append(t['final']['path'])
                paths += [x['path'] for x in t['docs']]
                for r in t['rows']:
                    for c in r['cells']:
                        paths.append(c['path'])
                        dirs.add(os.path.dirname(c['path']))
        sig = []
        for p in sorted(dirs) + sorted(set(paths)):
            try:
                st = os.stat(p)
                sig.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((p, None, None))
        return tuple(sig)

    def judged(self, statuses):
        """The whole debate judgment (debates.Judged: the debates, who works where, the seats, the diagnostics), made again only when something it reads has changed:
        an agent's record, a status, the walk's list, or a folder or file of the debates on disk (a stat of each, instead of the globs and listings of a build). It is
        also made again after DEBATE_TTL seconds, for a folder that appeared where nothing had looked before. A few judgments are kept (one per set of statuses)."""
        cache = self.__dict__.setdefault('_jd_cache', collections.OrderedDict())
        key, now = self._debate_key(statuses), time.monotonic()
        hit = cache.get(key)
        if hit is not None and now - hit[0] < DEBATE_TTL and hit[1] == self._disk_signature(hit[2].debates):
            cache.move_to_end(key)
            self.debate_diag = hit[2].diag
            return hit[2]
        jd = judge_debates(self, statuses)
        self.debate_diag = jd.diag
        cache[key] = (now, self._disk_signature(jd.debates), jd)
        cache.move_to_end(key)
        while len(cache) > 4:
            cache.popitem(last=False)
        return jd

    def debates(self, statuses):
        """(list of debates, {agent id: {unit}} the debates each agent holds a seat in): the debate structure from the report paths and the folders on disk (board/debates.py).
        The widened set of debates an agent works in (readers, held, named) is `judged(...).agent_units`; this is the seats alone."""
        jd = self.judged(statuses)
        return jd.debates, self.seated_units(jd)

    @staticmethod
    def seated_units(jd):
        out = collections.defaultdict(set)
        for asg in jd.assignments:
            out[asg.agent].add(asg.unit)
        return out

    def walk_repos(self, budget=1.5):
        """The debate folders that exist on disk below the repository tops this session and its agents work in, though no record names them: a bounded walk
        (units.walk_repo). The result goes to `walked_units` and `walk_capped`; `walk_gen` counts the changes so the debate judgment is made again."""
        with self.lock:
            cwds = {c for c in [self.cwd] + [a.cwd for a in self.agents.values()] if c}
        cat, found, capped, deadline = U.Catalog(text_of=lambda p: read_head(self, p)), [], False, time.monotonic() + budget
        cat.begin()
        for top in sorted({t for t in (U.repo_top(c) for c in cwds) if t})[:8]:      # only the top of a repository: a plain working folder is no place to look through
            units, cap = U.walk_repo(top, cat, deadline=deadline)
            found += [u.path for u in units]
            capped = capped or cap
        found = sorted(set(found))
        with self.lock:
            if found != list(getattr(self, 'walked_units', ())) or capped != getattr(self, 'walk_capped', False):
                self.walked_units, self.walk_capped = found, capped
                self.walk_gen = getattr(self, 'walk_gen', 0) + 1

    def _walk_later(self):
        """The background walk (every WALK_EVERY seconds, one at a time, off the polling thread). Off unless the server turned it on (WALK_ENABLED); it waits for the first picture (LATER)."""
        t = self._walk_thread
        if not WALK_ENABLED or not LATER.is_open() or (t is not None and t.is_alive()) or time.monotonic() - self._walk_at < WALK_EVERY:
            return
        self._walk_at = time.monotonic()
        self._walk_thread = threading.Thread(target=self._walk_safely, daemon=True)
        self._walk_thread.start()

    def _walk_safely(self):
        if not _WALK_SLOT.acquire(False):                # one walk at a time for the whole server: a busy slot means try again in a few seconds
            self._walk_at = time.monotonic() - WALK_EVERY + 5
            return
        try:
            self.walk_repos()
        except Exception as e:   # noqa: BLE001 — the walk is an extra: the board goes on without it
            print('debate folder walk error', type(e).__name__, flush=True)
        finally:
            _WALK_SLOT.release()

    def _brief_table(self, text):
        """The table of the shared brief with the columns '| 주제 | 폴더 | 선행 | 최종 산출물 |' (topic | folder | prerequisite | final deliverable) (board/debates.py)."""
        return brief_table(text)

    def state(self):
        """The whole state the page reads every 3 seconds (board/views.py)."""
        st = views.state(self)
        LATER.open()
        return st

    def alerts(self, statuses, now):
        """What the user needs to look at: a decision needed (decide) · something to check (check) · the user's turn (info) (board/views.py)."""
        return views.alerts(self, statuses, now)

    def _codex_busy(self, statuses, now):
        return any(self.agents[aid].provider == 'codex' and st in ('running', 'stalled') for aid, st in statuses.items())

    def _name_agents(self):
        """Gives model names to Claude agents that have no role name. If several have the same model, in order of start: opus5.5, opus5.5-2, …"""
        order = sorted((x for x in self.agents.values() if x.provider == 'claude' and not x.tag),
                       key=lambda x: (x.spawn_ts or x.first_ts or 0, x.id))
        for aid, name in model_numbers(order, lambda a: claude_model_short(a.model or a.model_hint)).items():
            self.agents[aid].auto_tag = name

    def agent_detail(self, aid):
        return views.agent_detail(self, aid)

    def unlinked(self):
        """Missed candidates: a claude -p or Codex exec that started shortly after this session's Bash call but could not be linked to any session (board/link.py). Claude sessions only."""
        return LINKS.unlinked_for(self.id) if self.provider == 'claude' else []

    def timeline(self, since):
        return views.timeline(self, since)

    def allowed_file(self, path):
        """The real path (realpath) that was checked if the file may be opened, else None (board/views.py)."""
        return views.allowed_file(self, path)


class CodexSession(Session):
    """An unlinked Codex thread (tui · desktop · an exec that could not be linked) seen as a session. The thread itself is the orchestrator.
    A guardian child thread is not an agent; it comes in only as tokens (tokens.guardian)."""

    def __init__(self, e):
        Session.__init__(self, e['path'])       # the Claude session's Tail and CodexLinker get made, but below the tail is replaced for this thread and the linker is not used (codex = None)
        self.id = e['id']
        self.dir = None
        self.provider = 'codex'
        self.codex = None
        self.entry = e
        self.cwd = e['cwd'] or ''
        self.first_ts = e['meta_ts']
        self.title = CODEX.title(e)
        try:
            size = os.path.getsize(e['path'])
        except OSError:
            size = 0
        self.partial = cx_start_offset(e['path'], size)
        self.tail = Tail(e['path'], start=self.partial, max_read=CX_MAX_READ)
        self.orch_tokens.fixed_limit = True
        self.orch['model'] = e['model']       # so that calls before the first turn_context are priced too when reading from the end
        self.seen, self.thread_total, self.nomodel = {}, None, []
        self._alive = None

    def poll(self):
        changed = False
        with self.lock:
            for raw in self.tail.read():
                r = cx_decode(raw)
                if not r:
                    continue
                try:
                    self._feed_cx(r)
                except Exception as e:   # noqa: BLE001 — one line must not stop the whole board
                    line_error(self.id, {'timestamp': r['ts']}, e)
                changed = True
            if changed:
                cx_sync_base(self.orch_tokens, self.partial, self.thread_total, self.seen, self.orch['model'])
            t = self.orch_tokens.t
            before = (t['g_calls'], t['g_input'], t['g_output'])
            cx_sync_guardians(self.orch_tokens, self.id)
            if (t['g_calls'], t['g_input'], t['g_output']) != before:
                changed = True
            if changed:
                if not self._sorted:
                    self.feed.sort(key=lambda e: e['ts'] or 0)
                    self._sorted = True
                self.version += 1
        return changed

    def _feed_cx(self, r):
        ts, typ, pt, p = r['ts'], r['type'], r['pt'], r['p']
        o = self.orch
        if p is None or typ == 'session_meta':
            return
        if typ == 'turn_context':
            o['model'] = p.get('model') or o['model']
            o['effort'] = p.get('effort') or o['effort']
            cx_reprice(self.orch_tokens, self.nomodel, o['model'])
        elif typ == 'token_usage_record':
            cx_record_usage(self, self.orch_tokens, p, o['model'])
        elif typ == 'event_msg':
            w = p.get('model_context_window') or (p.get('info') or {}).get('model_context_window')
            if w:
                self.orch_tokens.ctx_limit = w
            if pt == 'task_started':
                o['last_ts'] = ts
                self.cx_turn.begin()
            elif pt in ('task_complete', 'turn_aborted'):
                self.cx_turn.end(ts)
        elif typ == 'response_item':
            if pt == 'message' and p.get('role') == 'user':
                text = codex_user_text(p)
                if text:
                    o['last_ts'] = ts
                    self.cx_turn.begin()
                    self.pending_q.clear()       # the answer to request_user_input_async comes as the next user message
                    self._user_say(ts, text, raw=True)
            elif pt == 'message' and p.get('role') == 'assistant':
                text = codex_say_text(p)
                if text:                          # both commentary and final_answer (the same treatment as a Claude orchestrator's text block)
                    o['last_ts'] = ts
                    self.cx_turn.begin()
                    o['last_say'], o['last_say_ts'] = text, ts
                    self._event(ts, 'orch_say', 'orch', 'user', '사용자에게 보고', text, title_key='event.orch_say.title')
            elif pt in ('custom_tool_call', 'function_call'):
                o['last_ts'] = ts
                self.cx_turn.begin()
                name, text = codex_call(pt, p)
                self._orch_tool(ts, name, text)
                if name == 'request_user_input_async':
                    self._codex_ask(ts, p)
            elif pt in ('custom_tool_call_output', 'function_call_output'):
                o['last_ts'] = ts
                self.cx_turn.begin()

    def _codex_ask(self, ts, p):
        try:
            qs = json.loads(p.get('arguments') or '{}').get('questions') or []
        except (ValueError, AttributeError):
            qs = []
        qs = [{'header': '질문', 'question': q.get('title') or q.get('question') or '',
               'options': [{'label': x if isinstance(x, str) else (x or {}).get('label', ''), 'description': ''}
                           for x in q.get('options') or []]} for q in qs if isinstance(q, dict)]
        self.pending_q[p.get('call_id')] = {'ts': ts, 'questions': qs}
        lines = []
        for q in qs:
            lines.append('**%s** %s' % (q['header'], q['question']))
            lines += ['- %s' % op['label'] for op in q['options']]
            lines.append('')
        self._event(ts, 'orch_ask', 'orch', 'user', '선택지 질문', '\n'.join(lines).strip(), title_key='event.orch_ask.title',
                    questions=[{'header': None, 'question': q['question'], 'options': [{'label': op['label'], 'description': None} for op in q['options']]}
                               for q in qs])

    def alive(self):
        e = CODEX.get(self.id) or self.entry
        self._alive = cx_alive(e)
        return self._alive, None, self.title

    def _codex_busy(self, statuses, now):
        if self.cx_turn.open:
            return True
        try:
            recent = now - os.path.getmtime(self.path) < STALL_SEC
        except OSError:
            recent = False
        return self._alive is None and recent
