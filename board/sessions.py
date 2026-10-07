"""Reading sessions and building their state: CodexLinker (the Codex agents inside a Claude session), Session (one Claude session), CodexSession (a standalone Codex conversation)."""

import collections
import contextlib
import glob
import hashlib
import json
import os
import re
import threading
import time

from . import affil, lineage, procs
from . import fingerprint as fp
from .facts import Hint, LaunchKey, Planned, Redirect, Tag, WriteEvent
from . import runstate as RS
from .util import (
    CLAUDE_HOME, CODEX_SESSIONS, FILE_MAX, PROJECTS, STALL_SEC, Tail, as_text, line_error, open_safe, parse_records, parse_ts,
    stat_plain, strip_reminders, trunc,
)
from .tokens import TokenMeter, claude_model_short, cx_model_short
from .codex_parse import (
    CX_CALL_ID_RE, CX_MAX_READ, CX_PROMPT_MIN, CX_WINDOW, codex_call, codex_say_text, codex_user_text, cx_alive, cx_open, cx_procs, cx_start_offset,
)
from .codex_facts import shell_command
from .codex_scan import FrontScan
from .codex_index import CODEX
from .link import LINKS, SCRIPTY_RE, _cx_expand, bash_scripts, certain, cx_parse_call
from .agents import (
    Agent, ClaudeCalls, CodexAgent, EventLog, ForkSkip, ORCH_WINDOWS_KEEP, OpenExecs, cx_item, cx_record_usage, cx_reprice, cx_rows, cx_sync_base, cx_sync_guardians, launch_pieces, model_numbers, outside_heredocs, parse, piece_env, script_env,
    shell_mkdirs, shell_writes, tool_brief,
)
from .debates import REPORT_RE, brief_table, judge as judge_debates, read_head
from . import units as U
from . import views


AGENT_ID_RE = re.compile(r'^a[0-9a-f]{16}$')
HANDBACK_MARK = 'The report follows:'
ENCRYPTED_KO = '지시 내용은 기록에서 암호화되어 있어 볼 수 없습니다.'   # the old Korean field of a body that is ciphertext in the record (the page words it from `event.encrypted.text`)
STATUS_LABEL = {'completed': '작업 끝', 'done': '작업 끝', 'failed': '실패', 'killed': '중지됨'}   # end notice · turn status → title of the flow card
STATUS_KEY = {'completed': 'event.notify.done', 'done': 'event.notify.done', 'failed': 'event.notify.failed', 'killed': 'event.notify.killed'}   # notify status -> the dictionary key of its card title
DEBATE_TTL = 30           # seconds after which a debate judgment is made again whatever the signature says
WALK_EVERY = 60           # seconds between two walks of the repository folders (background)
WALK_ENABLED = False      # the server turns the background walk on (server.py main); tests and tools read only what they ask for
_WALK_SLOT = threading.Semaphore(1)
LATER_AFTER = 10         # seconds after the first link scan is ready: the latest the work that waits for the first picture starts, whether or not a picture was built
ORCH_HINTS_MAX = 64      # the folders the orchestrator's own writes put on the list of debates that the page keeps (the oldest are let go: `listing_capped`)
ORCH_PENDING_MAX = 256   # the writes of the main record that wait for their result
UNOBSERVED = object()                # what a launcher's environment is when nobody saw it
RUN_SLACK = 1.0                      # seconds: the instruction of a run is stamped at its start or a little after
HINT_WORDS_RE = re.compile(r'(?<![\w])(?:r|round)\d+(?![\w])|(?:brief|README|index)\.md')        # a shell command that says none of these cannot point the list at a folder



def _literal_instruction(piece):
    """The instruction a piece of a launching command (`agents.launch_pieces`) gives its agent when it is one literal word (`claude -p "do this"`, `codex exec "do this"`), else None: no word, more than one,
    or one with a variable or a substitution in it (the text of a file, the input of a pipe): the instruction may be anything."""
    from . import link as L
    words = piece['words']
    if piece.get('loose'):
        return None                                              # (found by looking through the words: nothing says which is its instruction)
    if piece['tool'] == 'claude':
        pos = L.parse_claude_args(words)['pos']
    else:
        j = 0
        while j < len(words) and words[j] is not None and words[j] not in ('exec', 'e'):
            j += 2 if words[j] in L.CODEX_VALUE_OPTS and '=' not in words[j] else 1
        pos, j = [], j + 1
        while j < len(words):
            w = words[j]
            if w is not None and w.startswith('-') and w != '-':
                j += 2 if w in L.CX_OPT_ARG and '=' not in w else 1
            else:
                pos.append(w)
                j += 1
    if len(pos) != 1 or pos[0] is None or pos[0] == '-' or '$' in pos[0] or '`' in pos[0]:
        return None
    return pos[0]


def _same_instruction(arg, first):
    """Whether the literal instruction of a launching command (`arg`) is the one an agent began with (`first`): the same normalising and the same comparison the link makes of them (`affil.launch_ok`)."""
    from . import link as L
    return affil.Instr(0, 0, first).same_as(fp.clip(fp.normalize(arg), L.LIT_ARG_MAX))


def _may_have(piece, prompt, sid=None):
    """Whether a piece of a launching command may have started an agent that began (the run its call started) with `prompt` and has the id `sid`: a piece that resumes another session did not start it, and
    otherwise its literal instruction is that text, or it has none (it may be any)."""
    if sid and piece['tool'] == 'claude' and not piece.get('loose'):
        from . import link as L
        resumed = L.parse_claude_args(piece['words']).get('resume')
        if resumed and resumed != sid:
            return False
    lit = _literal_instruction(piece)
    return lit is None or not prompt or _same_instruction(lit, prompt)
USER_DUP_SEC = 120       # if the same user instruction is recorded again within this time, it counts once
TAG_ROOM, TAG_SEAT = 'BULLPEN_ROOM', 'BULLPEN_SEAT'     # the two variables a user may set to tell where an agent works (lineage.TAG_NAMES)
TAG_SEAT_RE = re.compile(r'(?:(?:r|round)\d+/)?[^\s/]+')       # a seat: `name` or `rN/name`
ENDED = ('done', 'failed', 'killed', 'ended')       # the states of an agent whose run is over


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


class _Relay:
    """What the record of a descendant Claude of a page (a `claude -p` child, or a sub-agent that child started) tells the Codex linker of the page: its Bash calls, their results, its
    TaskStop calls and the notices of its background jobs. A call carries whose it is (the session it ran in, and the sub-agent), so that a thread is matched only with a call of the
    one that started it."""

    def __init__(self, linker, tree, node):
        self.linker, self.tree, self.node = linker, tree, node

    def bash(self, d, ts, b):
        self.linker.note_bash(d, ts, b, self.tree, self.node)

    def result(self, tuid, d, ts):
        self.linker.note_result(tuid, d, ts)

    def stop(self, task_id, ts):
        self.linker.note_stop(task_id, ts)

    def notice(self, text, ts):
        self.linker.note_notification(text, ts)


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
        self._cmd_seen = {}      # Codex thread id -> the item ids of its commands that were looked at (the commands of a Codex thread are calls like the Bash calls of the main record)
        self._in_events = {}     # (thread id, turn number) -> the spawn or orch_msg event made for the turn (a call found later is told to it)
        self.cmds = {}           # command item id -> (text, folder) of the commands of Codex threads that name BULLPEN_ROOM or BULLPEN_SEAT (read for the room tag of what they launched)

    # ---- from this session's main record ----
    def note_bash(self, d, ts, b, tree=None, node=None):
        """A Bash call of the main record (tree: this page's session), or of a descendant Claude (its session and sub-agent: `_Relay`)."""
        c = cx_parse_call(ts, b.get('id'), b.get('input') or {}, d.get('cwd'))
        if c and c['id'] not in self.calls:
            c['tree'], c['node'] = tree or self.s.id, node
            msg = (d.get('message') or {}).get('id') if isinstance(d.get('message'), dict) else None
            c['grp'], c['prov'] = msg if isinstance(msg, str) else None, 'claude'          # the group the call was made in (the message.id of its line) and the provider of the record: what a launch key is made of
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
    def poll(self, owned=None):
        """Reads the Codex threads of this page: `owned` {thread id: its link}, parents before children (the native sub-agents and the `codex exec` threads below the page in the
        graph of who started whom: Session._team); None: the threads this session owns directly."""
        if not LINKS.ready.is_set():
            return False
        changed = False
        owned = LINKS.owned_by(self.s.id) if owned is None else owned
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
                a.spawn_ts = info.get('bash_ts') or e['meta_ts']     # one linked by process lineage (rule 'proc') does not know the Bash call: the time the thread started
                try:
                    size = os.path.getsize(e['path'])
                except OSError:
                    continue
                a.partial = cx_start_offset(e['path'], size)
                a.fork = ForkSkip(e['prefix_ord'] if a.kind == 'sub' else None, a.partial)
                if a.kind == 'sub':
                    self._sub_identity(a, e)
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
                a.fork.restart()
            for r in cx_rows(lines, a.fork):
                try:
                    a.feed_cx(r)
                except Exception as e:   # noqa: BLE001 — one line must not stop the whole board
                    line_error(tid, {'timestamp': r['ts']}, e)
                fed = True
            if fed:
                cx_sync_base(a.tokens, a.partial, a.thread_total, a.seen, a.model)
                cx_sync_guardians(a.tokens, tid)
                changed = True
        if self._commands(owned):
            changed = True
        if self._collab(owned):
            changed = True
        for tid in owned:
            a = self.agents.get(tid)
            if a is not None:
                if self._derive(a):
                    changed = True
                a.trim_turns()                               # cut after the events are made (even if a lot is read at once, the events of the earlier turns remain)
        # when there is no report name, the model name (sol6.1). If several have the same model, sol6.1, sol6.1-2, … in order of start time (the same after a restart)
        order = sorted(self.agents.values(), key=lambda x: (x.meta_ts or 0, x.id))
        numbered = model_numbers([a for a in order if not a.report_tag and a.origin != 'subagent'], lambda a: cx_model_short(a.model))
        for a in order:
            tag = a.report_tag or (a.sub_name if a.origin == 'subagent' else numbered[a.id])
            if a.tag != tag:
                a.tag = tag
                changed = True
        return changed

    @staticmethod
    def _sub_identity(a, e):
        """What a native sub-agent is called: the end of its path (`/root/s1` → `s1`), else its nickname, as the name tag; the name Codex gave the thread (or the nickname) as the
        title. Its instruction is encrypted in the record, so nothing else names it. The root runtime it lives in is looked up once."""
        a.sub_name = (a.agent_path or '').rstrip('/').rsplit('/', 1)[-1] or a.nick or ''
        named = CODEX.title(e)
        a.title = a.description = (named if named != a.sub_name else a.nick) or a.sub_name           # the name it was given, else its nickname beside the task name on the tag
        root = CODEX.root_of(a.id)
        re_ = CODEX.get(root) if root else None
        a.runtime = (root, re_['path']) if re_ else None

    # ---- the commands of Codex threads: calls of the page ----
    def _commands(self, owned):
        """The commands the Codex threads of this page ran (the page's own thread when it is a Codex page, and the threads below it that can start something), as calls the
        way `note_bash` makes them from the Bash calls of a Claude record: so the turn of a `codex exec` thread finds the call that started it. True when there was a new call."""
        hosts = ([self.s.id] if getattr(self.s, 'provider', 'claude') == 'codex' else []) + [t for t in owned if t in self.agents]
        fresh = False
        for tid in hosts:
            cmds = CODEX.cmds(tid)
            seen = self._cmd_seen.setdefault(tid, set())
            if not cmds or cmds[-1]['item_id'] in seen:
                continue
            new = []
            for c in reversed(cmds):
                if c['item_id'] in seen:
                    break
                new.append(c)
            sub = tid in self.agents and self.agents[tid].origin == 'subagent'
            for c in reversed(new):
                seen.add(c['item_id'])
                text = c['cmd'] if c['cmd'] is not None else CODEX.cmd_text(tid, c['item_id'])
                if not isinstance(text, str):
                    continue                                                          # no text (not a shell command, over the limit, not kept): its time is a gap for the link index
                group = (c.get('exec') or (c['item_id'],))[0]                            # the exec call the command ran in, else the command itself
                if 'BULLPEN_' in text:
                    self.cmds[c['item_id']] = (text, c['cwd'])                         # a command that may carry a room tag: kept for reading it when the agent it launched is known
                if tid == self.s.id and c['status'] == 'completed' and c['exit_code'] == 0:
                    self.s._note_orch_shell(text, c['cwd'], c['end'], c['item_id'], ('codex', self.s.id, None, group))        # the page's own thread: what its shell wrote or made points the list of debates at a folder
                scripts = bash_scripts(text, c['cwd']) if (not sub or SCRIPTY_RE.search(text)) else []       # (a sub-agent's command is read for a script only when it names one)
                cc = cx_parse_call(c['start'], c['item_id'], {'command': text}, c['cwd'], scripts=scripts)
                if cc and cc['id'] not in self.calls:
                    cc['tree'] = (self.agents[tid].runtime or (None,))[0] if sub else tid       # the root thread whose tree this command ran in
                    cc['node'] = tid if sub else None
                    cc['grp'], cc['prov'] = group, 'codex'
                    cc['end'] = {'ts': c['end'], 'status': c['status'], 'exit': c['exit_code']}      # a command is recorded when its process ends
                    self.calls[cc['id']] = cc
                    self.order.append(cc)
                    fresh = True
        if fresh:
            self.order.sort(key=lambda c: c['ts'] or 0)
        return fresh

    # ---- the collaboration of native sub-agents: the feed events ----
    def _collab(self, owned):
        """The feed events of the native sub-agents (the parent's record says what happened between the threads): `started` is the spawn (its body is ciphertext: the card says
        so, and the note that comes with it is no instruction), a message from the parent is an orch_msg (the first one is the spawn's own body), the first message a sub-agent
        sends its parent after its `completed` is its handback, any other message between agents is an agent_msg; waiting for an agent is no event. Also the times its parent
        interrupted a sub-agent (`cuts`: an interrupt that came while it worked makes it interrupted, one after its end changes nothing). True when something was added."""
        subs = {t: a for t, a in self.agents.items() if a.origin == 'subagent'}
        if not subs and getattr(self.s, 'provider', 'claude') != 'codex':
            return False
        page = self.s.id
        hosts = ([page] if getattr(self.s, 'provider', 'claude') == 'codex' else []) + [t for t in owned if t in self.agents]
        changed = False
        for host in hosts:
            col = CODEX.collab(host)
            if not col:
                continue
            tree = host if host == page or host not in subs else (subs[host].runtime or (None,))[0]
            who = {'/root': 'orch' if tree == page else tree}
            paths = {}
            for t, a in subs.items():
                if a.runtime and a.runtime[0] == tree and a.agent_path:
                    paths[a.agent_path] = t
            armed = set()
            for c in col:
                kind = c['kind']
                if kind == 'completed':
                    armed.add(c['agent_path'])
                elif kind == 'started':
                    armed.discard(c['agent_path'])
                    a = subs.get(c['agent_thread_id'])
                    if a is not None and c['call_id']:
                        a.launch_src = (host, c['call_id'], c['ts'])
                    if a is not None and ('spawn', a.id) not in self.emitted:
                        self.emitted.add(('spawn', a.id))
                        self._spawn(a, c)
                        changed = True
                elif kind == 'interrupted':
                    a = subs.get(c['agent_thread_id'])
                    if a is not None and c['ts'] and c['ts'] not in a.cuts:
                        a.cuts.append(c['ts'])
                        changed = True
                elif kind == 'message':
                    first = c['author'] in armed and c['recipient'] == (c['author'] or '').rsplit('/', 1)[0]       # the first message of an agent after its `completed`: its final report
                    if first:
                        armed.discard(c['author'])                          # (also when it was told earlier: the end of an agent is one report, however often the record is read)
                    if self._message(c, who, paths, subs, first):
                        changed = True
        return changed

    @staticmethod
    def _msg_key(c):
        """What one agent_message is called, so that it is made an event once (the same message can be in the record of the parent and in the record of the sub-agent)."""
        return 'msg', c['msg_id'] or (c['ts'], c['author'], c['recipient'], c['text'])

    def _message(self, c, who, paths, subs, final):
        """One agent_message: the event it stands for, or none (the same message twice, the first one the parent sent, a thread that is not on this page). True when an event was made."""
        author, rec = c['author'], c['recipient']
        key = self._msg_key(c)
        if key in self.emitted or not author or not rec:
            return False
        frm, to = who.get(author) or paths.get(author), who.get(rec) or paths.get(rec)
        if frm is None or to is None:
            return False
        self.emitted.add(key)
        text, why = (ENCRYPTED_KO, 'event.encrypted.text') if c['encrypted'] else (c['text'] or '', None)       # the note beside ciphertext says nothing of what was said
        if rec == author.rsplit('/', 1)[0] and frm in subs:        # a sub-agent to its parent
            if final:                                              # the first message after its `completed`: its final report
                self.s._event(c['ts'], 'handback', frm, to, '최종 보고', text, agent=frm, title_key='event.handback.title', text_key=why)
            else:
                self.s._event(c['ts'], 'agent_msg', frm, to, '메시지', text, agent=frm, extra={'peer': None if to == 'orch' else to}, title_key='event.message.title', text_key=why)
            return True
        if author == rec.rsplit('/', 1)[0] and to in subs:         # the parent to a sub-agent
            if ('first', to) not in self.emitted:                   # the first one is the body of the spawn: the card of the spawn carries it
                self.emitted.add(('first', to))
                return False
            self.s._event(c['ts'], 'orch_msg', frm, to, '메시지', text, agent=to, title_key='event.message.title', text_key=why)
            return True
        if frm in subs or to in subs:                              # between two agents
            self.s._event(c['ts'], 'agent_msg', frm, to, '메시지', text, agent=frm if frm in subs else to, extra={'peer': None if to == 'orch' else to},
                          title_key='event.message.title', text_key=why)
            return True
        return False

    def _spawn(self, a, c):
        """The spawn card of a native sub-agent. Its instruction is ciphertext: the card says so (the plain note that comes with it names a path and is no instruction), unless
        the record holds the first message to it as plain text."""
        first = next((m for m in CODEX.collab(a.id) if m['kind'] == 'message' and m['recipient'] == a.agent_path), None)
        plain = first['text'] if first and not first['encrypted'] and first['text'] else ''
        if first is not None:
            self.emitted.update((self._msg_key(first), ('first', a.id)))          # that message is what the card carries, not one more message
        self.s._event(c['ts'], 'spawn', self.s.launcher_of(a), a.id, a.sub_name or a.id[:8], plain or ENCRYPTED_KO, agent=a.id, extra={'tool_use_id': c['call_id'], 'model': a.model},
                      text_key=None if plain else 'event.encrypted.text')

    def _turn_call(self, tid, i, t, link=None, cwd=None):
        """The call that started a turn, and the run (L) within it. `link` (what the link index decided of this thread: its tree, its node and, for the first turn, its call) makes
        the choice the index's: the first turn takes the call the index named (never another one by its time or its words: where the index held the call, none), and a later
        turn (a resume) only a call of the same tree and node, in the thread's folder, and only when exactly one call fits. Without `link` (nothing decided it): a call within
        30 s before the turn start whose instruction matches, or that mentioned this thread id."""
        user = (t['user'] or '').strip()
        if link is not None:
            return self._decided_call(tid, i, t, user, link, cwd)
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

    @staticmethod
    def _of(c, link):
        """Whether a call is one of the launcher the link index named for a thread: the same tree and the same node (a call of the main record has no node)."""
        return c.get('tree', link.get('sid')) == link.get('sid') and c.get('node') == link.get('node')

    def _decided_call(self, tid, i, t, user, link, cwd):
        if i == 0:
            c = self.calls.get(link.get('call') or '')
            if c is None or not self._of(c, link):
                return None, None
            return c, next((L for L in c['L'] if L['literal'] >= CX_PROMPT_MIN and user and L['rx'].fullmatch(user) and L['resume'] is None), None)
        start = t['start'] or 0
        found, said = {}, {}
        for c in self.order:
            if c['ts'] is None or c['ts'] > start + 1 or start - c['ts'] > CX_WINDOW or not self._of(c, link):
                continue
            for L in c['L']:
                if cwd and L['cwd'] and os.path.normpath(cwd) != L['cwd']:
                    continue
                if L['literal'] >= CX_PROMPT_MIN and user and L['rx'].fullmatch(user) and (not L['resume'] or '$' in L['resume'] or L['resume'] == tid):
                    said[id(c)] = (c, L)
            if tid in c.get('resumes', ()):                                       # the call that resumed this thread (a UUID in a path says nothing)
                found[id(c)] = (c, None)
        pick = said or found
        return next(iter(pick.values())) if len(pick) == 1 else (None, None)         # two calls fit: nothing says which one, and time does not break the tie

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

    def _set_out(self, a, t, path, src=None):
        """A planned -o path of a turn: a seat plan (not a link). `src` 'argv': it comes from the command line of the live process, which the command record (when it comes) replaces."""
        if path and not any(o['path'] == path for o in a.out_paths):
            a.out_paths.append(dict({'ts': t['start'], 'path': path}, **({'src': src} if src else {})))
        m = REPORT_RE.search(path or '')
        if m and not a.report_tag:
            a.report_tag = a.tag = m.group(3)

    @staticmethod
    def _drop_argv(a, t):
        """The path taken from the command line of the process is given up for what the command record says: its entry goes, and the name it gave is worked out again from what is left."""
        if t.get('out_src') != 'argv':
            return
        t['out_src'] = None
        a.out_paths[:] = [o for o in a.out_paths if not (o.get('src') == 'argv' and o['path'] == t['out'])]
        m = REPORT_RE.search(t['out'] or '')
        if m and a.report_tag == m.group(3):
            a.report_tag = next((x.group(3) for x in (REPORT_RE.search(o['path']) for o in a.out_paths) if x), '')

    def _revoke_argv(self, a, t, i):
        """A plan made from the command line of a live process is given up (the processes disagree now, or another one names another file): the path, the name it gave and the seat
        go, and the spawn card is what it was before the plan. A file the command record named is never touched here."""
        self._drop_argv(a, t)
        t['out'] = t['out_state'] = None
        ev = self._in_events.get((a.id, i))
        if ev is not None and i == 0:
            first = (t['user'] or '').strip().splitlines()
            ev['title'] = trunc(first[0] if first else '', 80)

    def _derive(self, a):
        """Once per turn: finding the call, the planned -o path, the flow events (spawn/orch_msg, handback/notify, stop), and sha1 confirmation."""
        changed, tid, now = False, a.id, time.time()
        sub = a.origin == 'subagent'          # a native sub-agent: no call started it (its spawn and messages come from its parent's record: _collab), and its instruction is not known
        up = self.s.launcher_of(a)            # who started it, as the page hangs it: the events are between that one and this thread (the orchestrator itself: 'orch')
        facts = (len(self.calls), (a.link or {}).get('call'), (a.link or {}).get('node'))      # what the choice of a turn's call rests on: a command that comes late, or a link decided again, may find it
        for t in a.turns:
            i = t['n']
            if sub:
                if not t.get('mapped'):
                    t['mapped'] = True
                    changed = True
            elif (t['user'] is not None or t['end']) and t.get('call') is None and t.get('facts') != facts:
                t['mapped'], t['facts'] = True, facts
                c, L = self._turn_call(tid, i, t, a.link or {}, a.cwd)
                if c:
                    t['call'], t['bash_ts'] = c['id'], c['ts']
                    out = self._resolve_out(c, L, t['user'])
                    if out:
                        hit_before = t['out']
                        self._drop_argv(a, t)                                      # the record of the command wins over the command line of the process
                        if t.get('out_src') == 'cand':                             # ... and over a file that was only found around the instruction
                            t['out_src'], t['out_state'] = None, None
                        t['out'] = out
                        if t.get('out_state') != 'confirmed' or t['out'] != hit_before:                         # (a path that the report already confirmed stays confirmed: it is written once)
                            t['out_state'] = 'planned'
                        self._set_out(a, t, out)
                    elif L and L.get('out') and '$' in L['out']:
                        self._unresolved_out(a, L['out'])                          # (a path the record cannot work out leaves the command line's, which is the value the shell made)
                    ev = self._in_events.get((tid, i))
                    if ev is not None:                                         # the card was made before the call was found (a command is written when it ends): it learns the call, once
                        if i == 0 and t['out']:
                            ev['title'] = os.path.basename(t['out'])
                        if i == 0 and ev.get('tool_use_id') is None:
                            ev['tool_use_id'] = c['id']
                changed = True
            if not sub and t['end'] is None and t.get('call') is None and (a.link or {}).get('rule') in ('env', 'proc'):
                live = LINKS.lineage.live_out.get(tid)                             # a live `codex exec` thread already linked by its environment or process: its own command line says where it writes
                if t.get('out_src') == 'argv' and (tid in LINKS.lineage.live_out_held or (live and live != t['out'])):
                    self._revoke_argv(a, t, i)                                     # processes that have its transcript open now disagree (or say another file): the plan made from the first is given up
                    changed = True
                if t['out'] is None and live:
                    t['out'], t['out_state'], t['out_src'] = live, 'planned', 'argv'
                    self._set_out(a, t, live, 'argv')
                    changed = True
            if t.get('mapped') and ('in', i, tid) not in self.emitted:
                self.emitted.add(('in', i, tid))
                if not sub:
                    c = self.calls.get(t['call'])
                    ts = t['bash_ts'] or t['start']
                    text = t['user'] or ''
                    if i == 0:
                        title = os.path.basename(t['out']) if t['out'] else trunc(text.strip().splitlines()[0] if text.strip() else '', 80)
                        ev = self.s._event(ts, 'spawn', up, tid, title, text, agent=tid,
                                           extra={'tool_use_id': c['id'] if c else None, 'model': a.model})
                    else:
                        ev = self.s._event(ts, 'orch_msg', up, tid, (c['desc'] if c and c['desc'] else '메시지'), text,
                                           agent=tid, title_key=None if c and c['desc'] else 'event.message.title')
                    self._in_events[(tid, i)] = ev
                    changed = True
            if t['end'] and ('end', i, tid) not in self.emitted:
                self.emitted.add(('end', i, tid))
                if t['status'] == 'done' and t['msg'] and not sub:          # (a sub-agent's final report is the message its parent's record holds)
                    self.s._event(t['end'], 'handback', tid, up, '최종 보고', t['msg'], agent=tid, title_key='event.handback.title')
                summary = t['error'] or trunc((t['msg'] or '').strip().splitlines()[0] if (t['msg'] or '').strip() else '', 160)
                title, extra, key = notify_card(t['status'])
                if not (sub and t['status'] == 'killed'):                    # an interrupted sub-agent is held, not finished: its state says so
                    self.s._event(t['end'], 'notify', tid, up, title, summary, agent=tid, extra=extra, title_key=key)
                a.notifications.append({'ts': t['end'], 'status': {'done': 'completed'}.get(t['status'], t['status']),
                                        'summary': summary, 'tokens': None, 'tools': None, 'duration_ms': None})
                changed = True
            c = self.calls.get(t['call'])
            if c and c['stopped'] and ('stop', i, tid) not in self.emitted and \
                    (t['end'] is None or t['status'] == 'killed'):
                self.emitted.add(('stop', i, tid))
                if t['end'] is None:
                    t['status'] = 'killed'
                self.s._event(c['stopped'], 'stop', up, tid, '에이전트 중지', agent=tid, title_key='event.stop.title')
                changed = True
            if t['end'] and t['sha'] and t['out_state'] != 'confirmed' and not t.get('given_up'):
                paths = [t['out']] if t['out'] else self._cand_files(t)
                hit = next((p for p in paths if self._sha_file(p) == t['sha']), None)
                if hit:
                    found = not t['out']                                                # a file found by looking around the folders of the instruction: shown, no more (O13, D8)
                    t['out'], t['out_state'] = hit, 'confirmed'
                    if found:
                        t['out_src'] = 'cand'
                    if not any(w['path'] == hit and w['ts'] == t['end'] for w in a.writes):
                        a.writes.append({'ts': t['end'], 'path': hit})
                    if not found:
                        self._set_out(a, t, hit)
                    changed = True
                elif now - t['end'] > 60:
                    t['given_up'] = True          # the -o file appears within 1 second of the end. After 1 minute it is not looked at again
        if self._plan(a):
            changed = True
        return changed

    @staticmethod
    def _plan(a):
        """What the debate judgment is told of the -o files of a Codex thread: a `Planned` for every turn that names one (a requirement of that run) and, when the turn has ended, one write event for it:
        `ok` is whether the turn ended well, `proof` is `sha` with the sha1 of its last message (`window` when it has none), and the window the judgment checks is from the call that launched the turn to
        its end. Nothing here looks at the file: the judgment does (J1). A path only the command line of a live process gave (`argv`) is a requirement but no event; a path that was found by looking
        around the folders of the instruction (`_cand_files`, `out_src` 'cand') is neither: it is shown, and the judgment never reads it (D8). A plan that is given up (`_revoke_argv`) is taken away with its event."""
        changed = False
        for t in a.turns:
            n, out = t['n'], t.get('out') if t.get('out_src') != 'cand' else None        # (a file found around the instruction is no requirement)
            src = 'argv' if t.get('out_src') == 'argv' else 'command'
            at = (t.get('bash_ts') if src == 'command' else t.get('start')) or t.get('start') or 0.0
            want = (out, src, at, t.get('call')) if out else None
            if t.get('pl') != want:
                a.ev.drop_planned(lambda p, n=n: p.run != n)
                if want:
                    a.ev.add_planned(Planned(a.id, out, '-o', t.get('call'), n, src, at))
                t['pl'], changed = want, True
            ev_want = (out, at, t['end'], t['status'] == 'done', t['sha']) if (want and src == 'command' and t.get('end')) else None
            if t.get('pl_want') != ev_want:
                if t.get('pl_ev') is not None:
                    a.ev.remove_write(t['pl_ev'])
                t['pl_ev'] = None
                if ev_want:
                    t['pl_ev'] = a.ev.add_write(WriteEvent(a.id, out, t['end'], 'replace', 'planned', ev_want[3], t.get('call'), n, 'sha' if t['sha'] else 'window', (at, t['end']),
                                                           (t['sha'],) if t['sha'] else ()))
                t['pl_want'], changed = ev_want, True
        return changed

    def verdict(self, a, now):
        """The runstate.Verdict of a Codex thread: its own turns (a run is a turn), what the launching Bash call recorded (the end notice of the background
        job, TaskStop) and whether a codex process has the rollout open (True / False / None: `ps` cannot say)."""
        if a.origin == 'subagent':
            # a native sub-agent has no process of its own and no launching call: the runtime of its root thread holds its life, its own record says how its turn ended and
            # its parent's record when it interrupted it (_cxsub_status)
            f = RS.facts_of(a.runs, 'cxsub', spawn_ts=a.spawn_ts)
            f.last_ts = max(f.last_ts or 0.0, a.last_ts or 0.0) or None
            f.pending = [p['ts'] for p in a.pending.values() if p.get('ts')]
            f.cuts = list(a.cuts)
            return RS.judge(f, RS.Proc(cx_open(a.runtime[1], a.runtime[0], cx_procs()) if a.runtime else None), now)
        calls = [t['call'] for t in a.turns if t.get('call')] or [(a.link or {}).get('call')]
        node, tree = (a.link or {}).get('node'), (a.link or {}).get('sid')
        if node in self.s.agents:
            ledger = self.s.agents[node].ledger
        elif tree and tree != self.s.id and tree in self.s.agents:                  # started by a descendant of the page: its record holds the notices of the job
            ledger = self.s.agents[tree].ledger
        else:
            ledger = getattr(self.s, 'ledger', None)
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
        self._cli_spawn_ev = {}     # the spawn event of every child session (kept, so that the title and the launcher can be made again when the call that started it becomes known): id -> event
        self._host_spawn = {}       # the same for the sub-agents of the child sessions: id -> event (None until the agent's first record is read)
        self._host_seen = set()     # the sub-agents of child sessions whose spawn event was made
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
        self.orch_hints = {}                  # folder -> the time of the orchestrator's last successful write that points at it (units.listing_hint): the debate list looks there, nothing else
        self.orch_hint_facts = {}             # folder -> facts.Hint: the same time and the calls and groups of the writes (what `orch_hints` becomes for the new judgment; the key set is the same)
        self.orch_hints_gen = 0               # counts the changes of orch_hints (the debate judgment is made again)
        self.orch_hints_dropped = False       # an older folder was let go for the limit
        self._orch_pending = collections.OrderedDict()      # tool_use id -> (time, [(path, is a folder)], message.id) of a Write, Edit, MultiEdit or Bash of the main record that has no result yet
        # what the debate judgment reads (board/facts.py): the orchestrator's own write events and command windows ('orch'), kept the way an agent's are
        self.orch_log = EventLog(ORCH_WINDOWS_KEEP)
        self.orch_calls = ClaudeCalls('orch', self.orch_log, lambda: None)
        self.spawn_cmds = {}                  # tool_use id -> (text, folder) of the Bash calls of the main record that name BULLPEN_ROOM or BULLPEN_SEAT
        self._tag_cmds = {}                   # call id -> its command read once (agents.launch_pieces): (text, folder, pieces, setters, gives)
        self.walked_units, self.walk_capped, self.walk_gen = [], False, 0
        self._walk_thread, self._walk_at = None, time.monotonic() - WALK_EVERY + 5       # the first walk comes a few seconds after the session is opened

    orch_events = property(lambda self: self.orch_log.events())
    orch_windows = property(lambda self: self.orch_log.windows)
    orch_windows_dropped = property(lambda self: self.orch_log.windows_dropped)
    orch_writes_dropped = property(lambda self: self.orch_log.writes_dropped)       # only written down: the judgment of a room counts the agents' alone
    orch_lost = property(lambda self: self.orch_log.lost)
    facts_gen = property(lambda self: self.orch_log.gen if getattr(self, 'orch_log', None) is not None else 0)       # goes up when anything of the orchestrator's side the debate judgment reads changes (an event, a window, a hint)

    # ---------- reading ----------
    def poll(self):
        changed = False
        with self.lock:
            changed = self._read_main()
            if self._read_team():
                changed = True
            self._walk_later()
            self._front_later()
            if changed:
                self._agent_events()
                if not self._sorted:      # once, at the first read: the agent-side events are slotted in time order
                    self.feed.sort(key=lambda e: e['ts'] or 0)
                    self._sorted = True
                self.version += 1
        return changed

    def _read_main(self):
        """The record of the orchestrator itself (a Claude session) and the folder of the sub-agents it started with its Agent tool. True when something was read."""
        changed = False
        restarted, lines = tail_records(self.tail)
        if restarted:
            self.runs, self.ledger = RS.RunTracker(self.id), RS.Ledger()
            self.orch_log.reset_record()
            self.orch_calls.reset()
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
                    self.orch_calls.pump(self.ledger)
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
        return changed

    def _read_team(self):
        """Everything below the orchestrator on this page, read from the one graph of who started whom (LINKS): the `claude -p` children, the native sub-agent threads and the
        `codex exec` threads of Codex, whoever of the page started them, and the sub-agents each `claude -p` child started with its own Agent tool. Then every record of them."""
        changed = False
        cli, cx = self._team()
        # Claude Code sessions started from Bash (claude -p): as sub-agents (when started with the model and effort set directly).
        # If the ownership changes (cancelled, or reassigned to another parent) it is no longer this session's agent. The events and idx already given are left as they are,
        # and if it is owned again the same agent and tail are revived (the spawn event is not made again)
        for csid in [c for c, a in self.agents.items() if a.origin == 'cli' and c not in cli]:
            self._cli_gone[csid] = (self.agents.pop(csid), self.agent_tails.pop(csid))
            for aid in [x for x, a in self.agents.items() if a.host == csid]:      # what the child started goes with it (it comes back with it)
                del self.agents[aid]
                del self.agent_tails[aid]
                self._host_spawn.pop(aid, None)
            changed = True
        for csid, o in cli.items():
            if csid in self._cli_gone:
                a, self.agent_tails[csid] = self._cli_gone.pop(csid)
                a.cli, a.spawn_ts = o, o['bash_ts']
                self.agents[csid] = a
                a.redirects, a.run_redirects = self._redirects_of(csid), self._redirects_by_run(csid)
                self._cli_retitle(a, o)
                changed = True
                continue
            if csid in self.agents:
                a = self.agents[csid]
                a.cli = o
                a.redirects, a.run_redirects = self._redirects_of(csid), self._redirects_by_run(csid)
                if self._cli_retitle(a, o):
                    changed = True
                continue
            paths = glob.glob(os.path.join(PROJECTS, '*', csid + '.jsonl'))
            if not paths:
                continue
            a = Agent(csid, {'description': o['bash_desc'] or 'claude -p'})
            a.origin, a.spawn_ts, a.cli, a.cli_desc, a.cli_node = 'cli', o['bash_ts'], o, o['bash_desc'], o.get('node')
            a.relay = _Relay(self.codex, csid, None)
            a.redirects, a.run_redirects = self._redirects_of(csid), self._redirects_by_run(csid)
            self.agents[csid] = a
            self.agent_tails[csid] = Tail(paths[0])
            self._cli_spawn[csid] = self._cli_spawn_ev[csid] = self._event(o['bash_ts'], 'spawn', self.launcher_of(a), csid, o['bash_desc'] or 'Claude Code 실행', '', agent=csid,
                                                                           title_key=None if o['bash_desc'] else 'event.spawn_cli.title')
            changed = True
        if self._hosted(cli):
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
        self._hosted_spawns()
        self._route_child_notes()
        if self.codex and self.codex.poll(cx):
            changed = True
        return changed

    def _team(self):
        """({claude -p child id: its link}, {Codex thread id: its link}) of what this page shows below its orchestrator: the descendants of the page in the graph of who started
        whom (`LINKS.descendants`: parents before the children they started), by what each is: a `claude -p` child (`cli_owners`), a `codex exec` thread (`owners`) or a native
        sub-agent thread of Codex (its link is what the graph says of it)."""
        cli, cx = {}, {}
        if not LINKS.ready.is_set():
            return cli, cx
        for tid in LINKS.descendants(self.id):
            o = LINKS.owner_of(tid)
            if o is None:
                continue
            link = {'cli': LINKS.cli_owners, 'cx': LINKS.owners}.get(o['kind'], {}).get(tid)
            if o['kind'] == 'cli' and link is not None:
                cli[tid] = link
            elif o['kind'] == 'cx' and link is not None:
                cx[tid] = link
            elif o['kind'] == 'sub':
                cx[tid] = o
        return cli, cx

    def _hosted(self, cli):
        """The sub-agents (Agent tool) that the `claude -p` children of this page started: each child is a Claude session with a folder of its own, read like this page's own
        (`a.host`: the child it hangs under). True when one was found."""
        changed = False
        for csid in cli:
            t = self.agent_tails.get(csid)
            if csid not in self.agents or t is None:
                continue
            folder = os.path.join(os.path.dirname(t.path), csid, 'subagents')
            for mp in glob.glob(os.path.join(folder, 'agent-*.meta.json')):
                aid = os.path.basename(mp)[len('agent-'):-len('.meta.json')]
                if aid in self.agents:
                    continue
                try:
                    with open(mp) as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    continue
                a = Agent(aid, meta)
                a.host = csid
                a.relay = _Relay(self.codex, csid, aid)
                self.agents[aid] = a
                self.agent_tails[aid] = Tail(os.path.join(folder, 'agent-%s.jsonl' % aid))
                if aid not in self._host_seen:
                    self._host_spawn[aid] = None
                changed = True
        return changed

    def _hosted_spawns(self):
        """The spawn card of a sub-agent of a `claude -p` child: made when its record has been read (its first time), its body filled in when its instruction is known."""
        for aid, ev in list(self._host_spawn.items()):
            a = self.agents.get(aid)
            if a is None:
                continue
            if ev is None:
                ts = a.spawn_ts or a.first_ts
                if ts is None:
                    continue
                ev = self._host_spawn[aid] = self._event(ts, 'spawn', self.launcher_of(a), aid, a.description, '', agent=aid, extra={'tool_use_id': a.tool_use_id, 'model': a.model_hint})
                self._host_seen.add(aid)
            if a.spawn_prompt is not None:
                ev['text'] = a.spawn_prompt
                del self._host_spawn[aid]

    def _cli_retitle(self, a, o):
        """A child is shown as soon as a process, an environment or a remembered line places it, and the call that started it (and the agent inside the parent whose call it was) is known
        later: the sub-agents' records and the text index are read after the first screen. When the link now names a call the child was shown without (or another one), the title, the time it
        started and its spawn event (the title, the launcher) are made again from it. True when something changed."""
        if o['bash_desc'] == getattr(a, 'cli_desc', o['bash_desc']) and o.get('node') == getattr(a, 'cli_node', o.get('node')):
            return False
        a.cli_desc, a.cli_node = o['bash_desc'], o.get('node')
        a.describe(o['bash_desc'] or 'claude -p')
        a.spawn_ts = o['bash_ts']
        ev = self._cli_spawn_ev.get(a.id)
        if ev is not None:
            ev['title'], ev['from'] = o['bash_desc'] or 'Claude Code 실행', self.launcher_of(a)
            if o['bash_desc']:
                ev.pop('title_i18n', None)
                ev.pop('title_is_default', None)
            else:
                ev['title_i18n'], ev['title_is_default'] = {'key': 'event.spawn_cli.title', 'params': {}}, True
        return True

    def launcher_of(self, a):
        """Who an agent hangs under on the page: 'orch' (this page's orchestrator), or the id of the sub-agent, `claude -p` child or Codex thread that started it."""
        if a.provider == 'codex':
            link = a.link or {}
            if a.origin == 'subagent':
                parent = link.get('parent')
                return parent if parent and parent != self.id and parent in self.agents else 'orch'
            node, tree = link.get('node'), link.get('sid')
            if link.get('parent_kind') == 'codex' and node in self.agents:        # a shell of a native sub-agent of Codex started it
                return node
            return tree if tree and tree != self.id and tree in self.agents else 'orch'
        if a.origin == 'cli' and a.cli:
            node, tree = a.cli.get('node'), a.cli.get('sid')
            if node:
                return node
            if tree and tree != self.id:
                return tree
        elif a.origin == 'subagent':
            if a.parent_agent:
                return a.parent_agent
            if a.host:
                return a.host
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

    def _redirects_by_run(self, csid):
        """The output redirects of the launching command of every run of a `claude -p` child ([the list of run 0, of run 1 ...]): the launches of the call the evidence names for that run that can be that
        run's (`_redirects_of`'s veto, run by run); a run no call is named for has none. The first is what `_redirects_of` says."""
        dec = LINKS.decisions.get(csid)
        if dec is None or dec.call is None or not dec.child.runs:
            return []
        out = []
        for k, run in enumerate(dec.child.runs):
            call = dec.call if k == 0 else (dec.calls[k] if k < len(dec.calls) else None)
            cand = [L for L in call.launches if L.reader and affil.launch_ok(L, dec.child, run)] if call is not None else []
            out.append(list(cand[0].redirects) if len(cand) == 1 else [])
        return out

    def _event(self, ts, kind, frm, to, title, text='', agent=None, extra=None, title_key=None, questions=None, text_key=None):
        """Appends a feed event. `title` and `text` are the old Korean fields (unchanged). The fields below are added after them:
        title_key = the dictionary key of the title when the server wrote it (title_i18n {key, params}; title_is_default is true for a stand-in title, false for a
        status label); questions = the structured body of a choice question."""
        ev = {'ts': ts, 'kind': kind, 'from': frm, 'to': to, 'title': title, 'text': text, 'agent': agent}
        if extra:
            ev.update(extra)
        if title_key:
            ev['title_i18n'] = {'key': title_key, 'params': {}}
            ev['title_is_default'] = not title_key.startswith('event.notify.')
        if text_key:
            ev['text_i18n'] = {'key': text_key, 'params': {}}
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
                    self._orch_pend(b, name, inp, d, ts)
                    if isinstance(inp, dict):
                        cwd = d.get('cwd') if isinstance(d.get('cwd'), str) and d.get('cwd') else self.cwd
                        self.orch_calls.use(ts, b, name, inp, cwd)
                        if name == 'Bash' and b.get('id') and isinstance(inp.get('command'), str) and 'BULLPEN_' in inp['command']:
                            self.spawn_cmds[b['id']] = (inp['command'], cwd)
                    if name == 'Bash':
                        self.codex.note_bash(d, ts, b)
                    if name == 'Agent':
                        self.spawns[b.get('id')] = {'ts': ts, 'description': inp.get('description', ''),
                                                    'prompt': inp.get('prompt', ''), 'msg': m.get('id') if isinstance(m.get('id'), str) else None}
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
                    self.orch_calls.result(ts, b, d, self.ledger)
                    self._orch_done(b.get('tool_use_id'), bool(b.get('is_error')))
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

    # ---------- the orchestrator's own writes: where the list of debates looks ----------
    @staticmethod
    def _shell_hints(text, cwd):
        """[(path, is a folder)] a shell command writes (a markdown file by a redirect or tee) or makes (mkdir): what `units.listing_hint` may take."""
        if not HINT_WORDS_RE.search(outside_heredocs(text)):          # (the body of a heredoc is a text the command writes, not words of the command)
            return []
        return [(p, False) for p in shell_writes(text, cwd, True)] + [(p, True) for p in shell_mkdirs(text, cwd)]

    def _orch_pend(self, b, name, inp, d, ts):
        """A Write, Edit, MultiEdit or Bash call of the main record that could point the list at a folder waits for its result (a failed one points nowhere)."""
        tid = b.get('id')
        if not tid:
            return
        if name in ('Write', 'Edit', 'MultiEdit'):
            got = [(inp.get('file_path'), False)]
        elif name == 'Bash' and isinstance(inp.get('command'), str):
            got = self._shell_hints(inp['command'], d.get('cwd') if isinstance(d.get('cwd'), str) and d.get('cwd') else self.cwd)
        else:
            return
        got = [(p, isdir) for p, isdir in got if isinstance(p, str) and U.listing_hint(p, isdir)]
        if got:
            msg = (d.get('message') or {}).get('id') if isinstance(d.get('message'), dict) else None
            self._orch_pending[tid] = (ts, got, msg if isinstance(msg, str) else None)
            while len(self._orch_pending) > ORCH_PENDING_MAX:
                self._orch_pending.popitem(last=False)

    def _orch_done(self, tid, failed):
        """The result of a call that waited: a success puts its folders on the list (`_note_orch_write`)."""
        got = self._orch_pending.pop(tid, None) if tid else None
        if got and not failed:
            for p, isdir in got[1]:
                self._note_orch_write(p, got[0], isdir, tid, ('claude', self.id, None, got[2]) if got[2] else None)

    def _orch_item(self, ts, it):
        """A `FileChange` of the Codex orchestrator's own thread that was completed: a file it added or changed (one it deleted points nowhere; a move counts at its new path)."""
        changes = it.get('changes') if isinstance(it, dict) and it.get('type') == 'FileChange' and it.get('status') == 'completed' else None
        for path, info in (changes.items() if isinstance(changes, dict) else ()):
            kind = info.get('type') if isinstance(info, dict) else None
            if kind == 'update' and isinstance(info.get('move_path'), str) and info['move_path']:
                path = info['move_path']
            elif kind not in ('add', 'update'):
                continue
            if isinstance(path, str):
                call = it.get('id') if isinstance(it.get('id'), str) else None
                self._note_orch_write(path, ts, False, call, ('codex', self.id, None, call) if call else None)

    def _note_orch_shell(self, text, cwd, ts, call=None, group=None):
        """A shell command of the Codex orchestrator's own thread that worked (exit 0): the folders it wrote into or made."""
        for p, isdir in self._shell_hints(text, cwd):
            self._note_orch_write(p, ts, isdir, call, group)

    def _note_orch_write(self, path, ts, is_dir=False, call=None, group=None):
        """A successful write of the orchestrator itself: the folder it points at (units.listing_hint) is where the list of debates looks next, once the disk says it is a debate
        of a shape the list takes (units.written_debate). At most ORCH_HINTS_MAX folders are kept: the ones written last (by the time of the write, not by the order they were read in).
        `call` and `group` (a LaunchKey.gkey() of the record) say which call made the write: an agent launched by the same call or the same message was launched together with it."""
        folder = U.listing_hint(path, is_dir)
        if folder is None:
            return
        old = self.orch_hints.get(folder)
        self.orch_hints[folder] = max(old or 0.0, ts or 0.0)
        changed = old is None or self.orch_hints[folder] != old
        was = self.orch_hint_facts.get(folder)
        self.orch_hint_facts[folder] = Hint(self.orch_hints[folder], (was.calls if was else frozenset()) | ({call} if call else frozenset()),
                                            (was.groups if was else frozenset()) | ({group} if group else frozenset()))
        changed = changed or was != self.orch_hint_facts[folder]
        while len(self.orch_hints) > ORCH_HINTS_MAX:                        # the one written longest ago goes, whatever the order the records were read in (a late result, a file change before a command)
            gone = min(self.orch_hints, key=lambda k: (self.orch_hints[k], k))
            self.orch_hints.pop(gone)
            self.orch_hint_facts.pop(gone, None)
            self.orch_hints_dropped, changed = True, True
        if changed:
            self.orch_hints_gen += 1
            self.orch_log.bump()

    def _first_writers(self):
        """path -> the agent that wrote to that path first (by write time, then by id if equal): who a report that another agent read is by, for the feed. Not a judgment of who holds a cell (units.assign)."""
        best = {}
        for a in self.agents.values():
            for w in a.writes:
                k = (w['ts'] or 0, a.id)
                if w['path'] not in best or k < best[w['path']]:
                    best[w['path']] = k
        return {path: k[1] for path, k in best.items()}

    def _agent_events(self):
        """What passed between agents: messages sent to other agents (agent_msg), reading other participants' reports (xread). The conversation of claude -p child sessions (_cli_talk) is also produced here."""
        writer, names = self._first_writers(), {}
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
        if a.host:                   # a sub-agent of a `claude -p` child lives in that child's session: its process is the one that counts
            alive = self._cli_proc(a.host).alive
        if a.origin == 'cli':        # claude -p child session: its own process, found by its session id
            f = RS.facts_of(a.runs, 'cli', ledger=self._launcher_ledger(a), launch_calls=self.launch_calls(a.cli or {}), spawn_ts=a.spawn_ts)
        else:
            parent_over = bool(a.parent_agent and a.origin == 'subagent' and self._parent_over(a, alive, now, _seen))       # only a sub-agent ends with its parent
            parent = self.agents.get(a.parent_agent) if a.parent_agent else self.agents.get(a.host) if a.host else None       # (a sub-agent of a child session: the notices are in the record of that child)
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

    # ---------- what the debate judgment reads of the agents that no record line says (0.3.0) ----------
    def refresh_facts(self, statuses=None):
        """Brings up to date what the judgment reads of every agent that is made from more than one record: the run it is in, the call that launched it (`launch`), its room tag (`room_tag`) and, for a
        `claude -p` child, the output it was told to write (`planned`) with the event of that output when its run is over. `statuses` {agent id: state} is the state the page works from. Each of
        them goes up `facts_gen` when it changed (J19). Reads the process table for the environment of a live child and nothing else outside the records."""
        statuses = statuses or {}
        with getattr(self, 'lock', None) or contextlib.nullcontext():
            env = {}
            for a in list(self.agents.values()):
                try:
                    run, start = self._run_of(a)
                    a.set_fact('run', run)
                    a.set_fact('run_start', start)
                    a.set_fact('launch', self._launch_of(a))
                    a.set_fact('room_tag', self._tag_of(a, env))
                    if a.origin == 'cli':
                        self._plan_cli(a, statuses.get(a.id))
                except Exception as e:   # noqa: BLE001 — one agent whose facts cannot be made must not stop the page
                    self.parse_errors += 1
                    line_error(a.id, None, e)

    @staticmethod
    def _run_of(a):
        """(number, start) of the last run of an agent: the Claude run tracker's last epoch, the Codex thread's last turn."""
        if a.provider == 'codex':
            t = a.turns[-1] if getattr(a, 'turns', None) else None
            return (t['n'], t['start'] or 0.0) if t else (None, 0.0)
        runs = getattr(a.runs, 'runs', None)
        r = runs[-1] if runs else None
        return (r.epoch, r.start_ts or 0.0) if r else (None, 0.0)

    def _launch_of(self, a):
        """The LaunchKey of the call that launched an agent (2.3), or None when it is not known: no call, or a link that was only guessed (a `time` or `content_short` link says no call)."""
        if a.provider == 'claude' and a.origin == 'subagent':
            if not a.tool_use_id:
                return None
            if a.parent_agent:                                   # started by a sub-agent: the call is in that sub-agent's record
                owner = self.agents.get(a.parent_agent)
                tree, node, msgs = ((owner.host or self.id) if owner else self.id), a.parent_agent, (owner.spawn_msgs if owner else {})
            elif a.host:                                         # started by a `claude -p` child of the page: in the main record of that child
                owner = self.agents.get(a.host)
                tree, node, msgs = a.host, None, (owner.spawn_msgs if owner else {})
            else:
                tree, node, msgs = self.id, None, {k: v.get('msg') for k, v in self.spawns.items()}
            msg = msgs.get(a.tool_use_id)
            return LaunchKey('claude', tree, node, msg, a.tool_use_id) if msg else None
        if a.origin == 'cli' and a.cli:
            o = a.cli
            dec = LINKS.decisions.get(a.id)
            span = dec.call.span if dec is not None and dec.call is not None else None
            call = o.get('call')
            if not call or span is None or span.call_id != call or not span.msg_id or not certain(o.get('rule')):
                return None
            calls, sure = o.get('calls') or [], o.get('calls_certain') or []
            if calls and calls[0] == call and sure and not sure[0]:
                return None                                      # the call was only counted by order
            return LaunchKey('codex' if o.get('parent_kind') == 'codex' else 'claude', o['sid'], o.get('node'), span.msg_id, call)
        if a.provider == 'codex' and a.origin == 'exec' and a.link:
            link = a.link
            c = self.codex.calls.get(link.get('call') or '')
            if c is None or not c.get('grp') or not certain(link.get('rule')) or not self.codex._of(c, link):
                return None
            return LaunchKey(c.get('prov') or 'claude', link.get('sid'), link.get('node'), c['grp'], c['id'])
        if a.provider == 'codex' and a.origin == 'subagent' and a.launch_src and a.runtime:
            host, call, ts = a.launch_src
            turn = self._turn_of(host, ts)
            return LaunchKey('codex', a.runtime[0], host, '%s:%s' % (host, turn), call) if turn else None
        return None

    @staticmethod
    def _turn_of(thread, ts):
        """The recorded id of the turn of a Codex thread open at `ts`, or None when it is not known (O22)."""
        e = CODEX.get(thread)
        for n in range((len(e['turns']) if e else 0) - 1, -1, -1):
            t = e['turns'][n]
            if t['start'] is not None and t['start'] <= ts and (t['end'] is None or ts <= t['end']):
                return t.get('id')
        return None

    # ---- room tags (2.4) ----
    def _cmd_text(self, call):
        """(text, folder) of a command that named BULLPEN_ROOM or BULLPEN_SEAT and has this call id, from the record that holds it; None when there is none."""
        if call in self.spawn_cmds:
            return self.spawn_cmds[call]
        if call in self.codex.cmds:
            return self.codex.cmds[call]
        for a in self.agents.values():
            if call in a.calls.cmds:
                return a.calls.cmds[call]
        return None

    def _last_call(self, a):
        """The call that launched the agent last (a resumed child or thread has more than one), or None."""
        if a.origin == 'cli' and a.cli:
            calls = self.launch_calls(a.cli)
            return calls[-1] if calls else None
        if a.provider == 'codex':
            return next((t['call'] for t in reversed(a.turns) if t.get('call')), (a.link or {}).get('call'))
        return None

    def _command_tag(self, call, tool, prompt, sid=None):
        """(room, seat, folder, given) the command of a launch gives to the agent it started: what is read is the piece of the command that started *that* agent (`agents.launch_pieces`), and of its environment
        only what was delivered to it (`agents.piece_env`): `VAR=value cmd` and `env VAR=value cmd` in front of it, else what the shell had exported before it in the same shell (`export VAR=value`) and
        nothing taken away since (`unset`, `env -u`, `env -i`); a variable that was only assigned is not exported and reaches no child. A command that starts one agent of this kind is that piece; one that starts
        several gives each its own piece, told by the instruction the agent began with (`prompt`): a piece may be the agent's when its literal instruction is that text or when it has no literal one (a variable, a
        file, the input of a pipe: it may be anything); when that does not tell them apart the values of the pieces must be the same for all of them, else nobody is given a value (a tag of another agent is
        worse than none). A command that starts agents like this one and has none that can be this agent starts nobody that has a tag (a resume is told by the id it resumes and by the first instruction of
        the run it began, `sid` is the id of the agent). A command whose launch is not in its text (it has no such piece: a script) gives what every command that could have started it gives (`agents.script_env`),
        when it is one value. A name with a `$` left is not given a value. `given` says which names the command gives that agent at all (a value that is only inherited has none). The room as written; a relative
        one counts from the folder the shell was in where the piece stands. None when the command is not known."""
        got = self._cmd_text(call)
        if got is None:
            return None
        parsed = self._tag_cmds.get(call)
        if parsed is None:
            parsed = self._tag_cmds[call] = (got[0], got[1]) + launch_pieces(got[0])
        text, cwd, pieces, setters, others = parsed
        names = (TAG_ROOM, TAG_SEAT)
        mine = [p for p in pieces if p['tool'] == tool]
        cands = [(p, piece_env(p, setters, names)) for p in mine if _may_have(p, prompt, sid)]
        at, env = None, {}
        if mine and not cands:                                       # the text starts agents like this one and none of them is this agent: what it was started by is not in the text
            return (None, None, None, {TAG_ROOM: False, TAG_SEAT: False})
        if not cands:                                                # the launch is not in the text: a script, `xargs`
            env = script_env(others, setters, names)
        else:
            if any(pe[1] != cands[0][1] for pe in cands):            # several pieces that cannot be told apart and give different things
                return (None, None, None, {TAG_ROOM: False, TAG_SEAT: False})
            at, env = cands[0][0]['at'], cands[0][1]
        from . import link as L
        base = os.path.normpath(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None
        P = parse(text)                                              # (`at` is a place in the text the pieces were read from: this one)
        here = L.shell_cwd(P.text, P.code, len(P.text) if at is None else at, P.assigns(), base) or base
        return (env.get(TAG_ROOM), env.get(TAG_SEAT), here, {n: n in env for n in names})

    @staticmethod
    def _run_instruction(a, call):
        """The first instruction of the run that the call `call` started: a child that was resumed has one run for each call, and the command of a resume names the instruction of the run it began, not
        the one the child began with. The first instruction of the child when the call is its first or the run is not known."""
        first = Session._first_instruction(a)
        calls = (a.cli or {}).get('calls') or [] if a.origin == 'cli' else []
        runs = getattr(a.runs, 'runs', None) or []
        if call in calls:
            k = len(calls) - 1 - calls[::-1].index(call)
            if 0 < k < len(runs) and runs[k].start_ts is not None:
                said = next((t['text'] for t in a.talk if t['kind'] == 'in' and t['ts'] is not None and t['ts'] >= runs[k].start_ts - RUN_SLACK), None)
                return said.strip() if isinstance(said, str) and said.strip() else None
        return first

    @staticmethod
    def _first_instruction(a):
        """The first instruction a `claude -p` child or a `codex exec` thread was given, or None."""
        said = next((t['text'] for t in a.talk if t['kind'] == 'in'), None) if a.origin == 'cli' else None
        text = said if said is not None else getattr(a, 'spawn_prompt', None)
        return text.strip() if isinstance(text, str) and text.strip() else None

    def _pids_of(self, a):
        """The live processes of a `claude -p` child or a `codex exec` thread (empty when there are none or they cannot be told)."""
        if a.origin == 'cli':
            p = self._cli_procs.get(a.id)
            return list(p.pids) if p is not None and p.alive else []
        found = procs.codex_pids(CODEX_SESSIONS) or []
        return [i['pid'] for i in found if a.path in i['fds'] or a.id.encode() in b' '.join(i['argv'])]

    def _orch_tag_env(self):
        """BULLPEN_ROOM and BULLPEN_SEAT in the environment of the orchestrator's own process, as {name: value}; None when that cannot be read."""
        if self.provider == 'codex':
            found = procs.codex_pids(CODEX_SESSIONS) or []
            pids = [i['pid'] for i in found if self.path in i['fds'] or self.id.encode() in b' '.join(i['argv'])]
        else:
            alive, pid, _name = self.alive()
            pids = [pid] if alive and pid else []
        return self._environ(pids)

    @staticmethod
    def _environ(pids):
        """The tag variables the processes `pids` agree on, {name: value}; None when there is no process or one of them cannot be read."""
        got = [procs.env_values(pid, lineage.TAG_NAMES) for pid in pids]
        if not got or any(g is None for g in got):
            return None
        out = {}
        for name in (TAG_ROOM, TAG_SEAT):
            values = {g[name] for g in got if g.get(name)}
            if len(values) == 1:
                out[name] = next(iter(values))
        return out

    def _launcher_env(self, a, memo):
        """BULLPEN_ROOM and BULLPEN_SEAT as the agent that launched `a` has them, {name: value}: from its live process, else what its environment said while it lived, else its tag (the room as the
        folder it is). None when the one that launched it is the orchestrator itself (its environment is read apart) or a sub-agent. `UNOBSERVED` when it is one of the agents of the page and nothing of its
        environment was ever seen (it ended before it was looked at, and its own command named no tag), or when it is not one of them: what it passed on is not known."""
        o = (a.cli if a.origin == 'cli' else a.link) or {}
        node, tree = o.get('node'), o.get('sid')
        if node:
            who = self.agents.get(node)
        elif tree and tree != self.id:
            who = self.agents.get(tree)
        else:
            return None                                              # the orchestrator
        if who is None:
            return UNOBSERVED
        if who is a or who.origin not in ('cli', 'exec'):
            return None
        got = {}
        env = self._environ(self._pids_of(who))
        if env:
            got.update(env)
        cached = getattr(who, 'tag_env', None)
        for name, value in zip((TAG_ROOM, TAG_SEAT), cached or ()):
            if value:
                got.setdefault(name, value)
        tag = self._tag_of(who, memo)                                # (the launcher's own tag first: it may not have been worked out yet)
        if tag is not None:
            got.setdefault(TAG_ROOM, tag.room)
            if tag.seat:
                got.setdefault(TAG_SEAT, tag.seat)
        if env is None and cached is None and tag is None:
            return UNOBSERVED
        return got

    def _tag_of(self, a, memo):
        """The facts.Tag of a `claude -p` child or a `codex exec` thread: from the environment of its live process when that can be read, else from the command that launched it last (2.4). A value
        the orchestrator's own environment has too, or the one that launched it has (a grandchild has the tag of the child that started it), and the command does not give, was only inherited: it is
        not a tag. The room must be a folder that exists and is no place a debate cannot be (too broad, the folders the tools keep); a seat is `name` or `rN/name` (a `.md` tail is cut). When the
        environment of the orchestrator cannot be read, a value that only the environment gives is taken for inherited, whatever it is (a missed tag, not a room made of children that were never
        together). What an ended agent's environment said is kept for the call it was read for: a run started by another call (a resume) that gives no tag has none. None for an agent that cannot
        have one (a sub-agent has no process of its own)."""
        if a.origin not in ('cli', 'exec'):
            return None
        done = memo.setdefault('tags', {})
        if a.id in done:
            return done[a.id]
        if a.id in memo.setdefault('busy', set()):                   # (a launcher that is launched by its own child: no tag from it)
            return None
        memo['busy'].add(a.id)
        try:
            tag = done[a.id] = self._tag_of_now(a, memo)
        finally:
            memo['busy'].discard(a.id)
        return tag

    def _tag_of_now(self, a, memo):
        call = self._last_call(a)
        if getattr(a, 'tag_env', None) and getattr(a, 'tag_env_call', None) != call:
            a.tag_env = None                                         # what was read for an earlier call says nothing of the run this one started
        cmd = self._command_tag(call, 'claude' if a.origin == 'cli' else 'codex', self._run_instruction(a, call), a.id) if call else None
        env = self._environ(self._pids_of(a))
        if env is not None:
            if env and 'orch' not in memo:
                memo['orch'] = self._orch_tag_env()
            orch = memo.get('orch')
            launcher = self._launcher_env(a, memo) if env else None
            for name in list(env):
                inherited = orch is None or orch.get(name) == env[name] or launcher is UNOBSERVED or (isinstance(launcher, dict) and launcher.get(name) and (
                    launcher[name] == env[name] or (name == TAG_ROOM and os.path.realpath(launcher[name]) == os.path.realpath(env[name]))))
                if inherited and not (cmd and cmd[3][name]):
                    del env[name]                                  # the same value as the orchestrator's own (or the environment of the orchestrator cannot be read, so it cannot be told from it) or as the one that launched it: inherited, nobody set it for this agent
            a.tag_env, a.tag_env_call = (env.get(TAG_ROOM), env.get(TAG_SEAT)), call
            room, seat, source, here = env.get(TAG_ROOM), env.get(TAG_SEAT), 'environ', a.cwd
        elif cmd is not None and (cmd[0] or cmd[1]):
            room, seat, source, here = cmd[0], cmd[1], 'command', cmd[2]
        elif cmd is None and getattr(a, 'tag_env', None):
            room, seat, source, here = a.tag_env[0], a.tag_env[1], 'environ', a.cwd         # the process is gone: what its environment said while it lived
        else:
            return None
        if not room:
            return None
        if not os.path.isabs(room):
            if not here:
                return None
            room = os.path.join(here, room)
        real = os.path.realpath(room)
        if not os.path.isdir(real) or U.too_broad(real) or (real + os.sep).startswith(tuple(U.STATE_DIRS)):
            return None
        if seat:
            seat = seat[:-3] if seat.endswith('.md') else seat
            seat = seat if TAG_SEAT_RE.fullmatch(seat) else None
        return Tag(real, seat or None, source, a.run)

    # ---- the output a `claude -p` child was told to write (W4) ----
    def _launcher_window(self, o, call=None):
        """The command window (facts.CmdWindow) of the Bash call that launched a `claude -p` child (`call`: the call of one of its runs, else the call the link names), in the record of the launcher, or None."""
        call, node, tree = call or o.get('call'), o.get('node'), o.get('sid')
        log = self.agents[node].ev if node in self.agents else self.orch_log if tree == self.id else self.agents[tree].ev if tree in self.agents else None
        if log is None or not call:
            return None
        return next((w for w in reversed(log.windows) if w.call == call), None)

    def _launcher_window_ok(self, o, call=None):
        """Whether the Bash call that launched a `claude -p` child ended well: its command window in the record of the launcher (True worked, False failed, None not known)."""
        w = self._launcher_window(o, call)
        return w.ok if w is not None else None

    def _open_execs(self, tree, node):
        """The exec calls that have not ended of the Codex thread whose shell may have started a child (`tree` the thread of the page, `node` a sub-agent thread of it), or None."""
        for host in ([self.agents.get(node)] if node else []) + [self if tree == self.id else self.agents.get(tree)]:
            execs = getattr(host, '_cx_execs', None)
            if isinstance(execs, OpenExecs):
                return execs
        return None

    def _open_redirects(self, a):
        """([(path, op)], call id, time) of the one command of the thread that started a `claude -p` child that has not ended: the exec call it runs in has its text (`tools.exec_command({cmd: \"…\"})`)
        while the record of the command itself is written when it ends. Only a command made before the child began, that starts one `claude -p` here (not a loop of them) that can be this child's
        (it runs in the child's folder, and what it is told is what the child began with), and only when it is the one such command of the thread: else nothing is said (a requirement of another
        child is worse than none). The redirects are those of the launch, as `_redirects_of` reads them from the record of the command."""
        o = a.cli or {}
        execs = self._open_execs(o.get('sid'), o.get('node'))
        if execs is None:
            return None
        from . import link as L
        first, here, found = self._first_instruction(a), os.path.realpath(a.cwd) if a.cwd else None, []
        for call_id, ts, cmd, where in execs.commands():
            if a.first_ts is not None and ts > a.first_ts + 1.0:
                continue                                             # called after the child began: it did not start it
            for d in L.launch_facts(cmd, L.shell_code(cmd), where, None, 'claude'):
                if not d['persist'] or d['n'] > 1 or (d['arg'] is not None and first is not None and not affil.Instr(0, 0, first).same_as(d['arg'])):          # (`arg` is normalised: the instruction is compared as the link compares it)
                    continue
                if d['cwd'] and here and os.path.realpath(d['cwd']) != here:
                    continue
                found.append((call_id, ts, d))
        if len(found) != 1:
            return None
        call_id, ts, d = found[0]
        reds = [(os.path.normpath(r.path_resolved), r.op) for r in d['redirects'] if r.fd == 1 and r.op in ('>', '>>') and r.path_resolved]
        return (reds, call_id, ts) if reds else None

    def _plan_cli(self, a, status):
        """A `Planned` for every output redirect (`> f`, `>> f`) of the command that launched a `claude -p` child (`_redirects_of`: when one launch could be the child's), and, once its run is over,
        one write event for each: `ok` is whether the launching call ended well; when the last text of the child is known the event is `content` (the sha1 of the text and of the text with a
        new line, which is what the shell writes) and, when it is not, `window`; `>>` is an `append` (the file is the old text and the new, nothing to compare) and no owner. A child that was resumed
        has a launching command for every run: each run has its own requirements and events, made of its own call and time, its own end and its own last text (`_redirects_by_run`). Nothing here looks at the
        file: the judgment does (J1)."""
        o = a.cli or {}
        runs = getattr(a.runs, 'runs', None) or []
        calls, sure = o.get('calls') or [], o.get('calls_certain') or []
        per = list(a.run_redirects) if a.run_redirects else [a.redirects]
        plan = []                                                    # per run: (reds, at, call, epoch, end, text)
        for k, redirects in enumerate(per):
            call = o.get('call') if k == 0 else (calls[k] if k < len(calls) and (sure[k] if k < len(sure) else True) else None)
            if k and not call:
                continue                                             # a run whose launching call is not known (or was only counted by order): no requirement of it
            at = (o.get('bash_ts') or 0.0) if k == 0 else (getattr(self._launcher_window(o, call), 't0', None) or (runs[k].start_ts if k < len(runs) else 0.0) or 0.0)
            reds = [(os.path.normpath(r.path_resolved), r.op) for r in redirects if r.fd == 1 and r.op in ('>', '>>') and r.path_resolved]
            if k == 0 and not reds and not call:                     # the call that started it is not on record yet (a Codex shell writes its command when it ends): its exec call is
                got = self._open_redirects(a)
                if got:
                    reds, call, at = got
            epoch = runs[k].epoch if k < len(runs) else k + 1
            last = k >= len(per) - 1 and k >= len(runs) - 1          # the last run of the child
            lo, hi = (runs[k].start_ts if k < len(runs) else None), (runs[k + 1].start_ts if k + 1 < len(runs) else None)
            ends = [t for t in a.talk if t['kind'] == 'end' and (lo is None or (t['ts'] or 0.0) >= lo) and (hi is None or (t['ts'] or 0.0) < hi)]
            if last:
                end = a.last_ts if status in ENDED and a.last_ts else None
            else:
                end = max([t['ts'] for t in ends] or [None], key=lambda x: x or 0.0) or (runs[k].last_ts if k < len(runs) else None)
            plan.append((tuple(dict.fromkeys(reds)), at, call, epoch, end, ends[-1]['text'] if ends else None))
        want = tuple((reds, at, call, epoch) for reds, at, call, epoch, _end, _text in plan)
        event_want = tuple((w, end, self._launcher_window_ok(o, w[2]) if end else None, text) for w, (_r, _a, _c, _e, end, text) in zip(want, plan) if end) or None
        if a.plan_want and a.plan_want[0] == want and a.plan_want[1] == event_want:
            return
        if a.plan_want is None or a.plan_want[0] != want:
            a.ev.drop_planned(lambda p: False)
            for reds, at, call, epoch in want:
                for path, op in reds:
                    a.ev.add_planned(Planned(a.id, path, op, call, epoch, 'command', at))
        for ev in a.plan_events:
            a.ev.remove_write(ev)
        made = []
        for (reds, at, call, epoch), end, ok, text in (event_want or ()):
            for path, op in reds:
                if op == '>>' or text is None:
                    ev = WriteEvent(a.id, path, end, 'append' if op == '>>' else 'replace', 'planned', ok, call, epoch, 'window', (at, end))
                else:
                    ev = WriteEvent(a.id, path, end, 'replace', 'planned', ok, call, epoch, 'content', (at, end),
                                    (hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest(), hashlib.sha1((text + '\n').encode('utf-8', 'replace')).hexdigest()))
                got = a.ev.add_write(ev)
                if got is not None:
                    made.append(got)
        a.plan_want, a.plan_events = (want, event_want), tuple(made)

    # ---------- debates ----------
    def _debate_key(self, statuses):
        """What the debate judgment reads from the session, as a small comparable value: the statuses, the walk's generation, the folder the session was started in and the generation of everything the
        collectors keep for it (`facts_gen` of the session and of each agent: it goes up when an event, a window, a read, a plan, a launch, a tag or a run changes, so a window whose end was set
        later is told from one that was not). What the judgment reads of an agent that is no event: its names (the ones a message to it can use), its folder, when it started and was last heard of
        (`_over_before`), and how many messages it sent that did not fail (a room of participants only is made of them). A judgment is reused while this is the same and the disk is too. The facts
        that come from more than one record are brought up to date first (`refresh_facts`)."""
        refresh = getattr(self, 'refresh_facts', None)
        if refresh is not None:                                  # (a stand-in for a session in a test has none)
            refresh(statuses)
        per = tuple((a.id, a.tag, a.auto_tag, a.description, a.cwd, (a.cli or {}).get('cwd'), a.spawn_ts, a.first_ts, a.last_ts, len(a.sent), sum(1 for m in a.sent if m.get('ok') is False),
                     getattr(a, 'facts_gen', 0)) for a in self.agents.values())
        return (tuple(sorted(statuses.items())), getattr(self, 'walk_gen', 0), getattr(self, 'cwd', ''), getattr(self, 'facts_gen', 0), per)

    def judged(self, statuses):
        """The whole debate judgment (debates.Judged: the debates, who works where, the cells, the diagnostics), made again only when something it reads has changed (J19): an agent's record or the
        orchestrator's (`facts_gen`, in the key), a status, the walk's list, or the disk as the judgment looked at it (every lookup it made asked again, the lookups of what the page shows of a file or
        a folder among them: a folder that got a file, a file written over in place, a new link, a file whose size or time moved). It is also made again after DEBATE_TTL seconds, for a folder that
        appeared where nothing had looked before. A few judgments are kept (one per set of statuses). The facts are brought up to date once, by the key, for the request."""
        cache = self.__dict__.setdefault('_jd_cache', collections.OrderedDict())
        key, now = self._debate_key(statuses), time.monotonic()
        hit = cache.get(key)
        if hit is not None and now - hit[0] < DEBATE_TTL and not U.Catalog.changed(hit[1].reads):
            cache.move_to_end(key)
            jd = hit[1]
        else:
            jd = judge_debates(self, statuses, fresh=True)
            cache[key] = (now, jd)
            cache.move_to_end(key)
            while len(cache) > 4:
                cache.popitem(last=False)
        self.debate_diag = jd.diag
        self._last_judged = (tuple(sorted(statuses.items())), jd)
        return jd

    def last_judged(self, statuses):
        """The judgment `judged` gave last, when it was made for these statuses, else None: what a request that only needs to know where an agent is thought to work reads (the same input gives the
        same judgment, J19), where `judged` would look at the whole disk again for it."""
        got = getattr(self, '_last_judged', None)
        return got[1] if got is not None and got[0] == tuple(sorted(statuses.items())) else None

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

    def _front_later(self):
        """The background read of the front of every big rollout of the page (the threads and the thread of the page itself): off unless the server turned the background on (WALK_ENABLED), and it waits
        for the first picture (LATER). Each one starts once; the read runs on a thread of its own, one at a time for the server (board/codex_scan.py)."""
        if not WALK_ENABLED or not LATER.is_open():
            return
        for owner in [self] + list(self.agents.values()):
            if getattr(owner, 'partial', 0) and getattr(owner, 'front', None) is not None:
                owner.front_scan()

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
    """A Codex thread that no session or thread started (tui · desktop · an exec that could not be linked) seen as a page. The thread itself is the orchestrator, and what it
    started is its team like a Claude page's: its native sub-agent threads, the `claude -p` and `codex exec` runs its shell (or a sub-agent's) started. A guardian child thread
    is not an agent; it comes in only as tokens (tokens.guardian)."""

    def __init__(self, e):
        Session.__init__(self, e['path'])       # the Claude session's Tail and CodexLinker get made, but below the tail is replaced for this thread (the linker reads the threads below it)
        self.id = e['id']
        self.dir = None
        self.provider = 'codex'
        self.cwd = e['cwd'] or ''
        self.first_ts = e['meta_ts']
        self.title = CODEX.title(e)
        try:
            size = os.path.getsize(e['path'])
        except OSError:
            size = 0
        self.partial = cx_start_offset(e['path'], size)
        self.tail = Tail(e['path'], start=self.partial, max_read=CX_MAX_READ)
        self.fork = ForkSkip(e['prefix_ord'] if e.get('kind') == 'sub' else None, self.partial)
        self.orch_tokens.fixed_limit = True
        self.orch['model'] = e['model']       # so that calls before the first turn_context are priced too when reading from the end
        self.seen, self.thread_total, self.nomodel = {}, None, []
        self._alive = None
        self._cx_execs = OpenExecs()          # the exec calls of the open turn whose output has not come (a command that runs inside one has its window start with it)
        self.front = FrontScan()              # the read of the front of a big rollout (before `partial`), in the background: until it is done that part is `lost`
        self._first_row = None                # the time of the first line read (a big rollout is read from its end: what is before it was not read)

    def _read_main(self):
        """The rollout of the thread itself. True when something was read."""
        changed = False
        before = self.tail.pos
        lines = self.tail.read()
        if self.tail.pos < before:
            self.fork.restart()
            self.orch_log.reset_record()
            self._cx_execs.clear()
            self._first_row = None
            self.front.reset()
        for r in cx_rows(lines, self.fork):
            try:
                self._feed_cx(r)
            except Exception as e:   # noqa: BLE001 — one line must not stop the whole board
                line_error(self.id, {'timestamp': r['ts']}, e)
            changed = True
        if changed:
            cx_sync_base(self.orch_tokens, self.partial, self.thread_total, self.seen, self.orch['model'])
        t = self.orch_tokens.t
        reviewed = (t['g_calls'], t['g_input'], t['g_output'])
        cx_sync_guardians(self.orch_tokens, self.id)
        if (t['g_calls'], t['g_input'], t['g_output']) != reviewed:
            changed = True
        return changed

    def _feed_cx(self, r):
        ts, typ, pt, p = r['ts'], r['type'], r['pt'], r['p']
        if typ == 'hole':                   # a write of the record that cannot be read (`cx_rows`)
            self.orch_log.widen_lost(*r['span'])
            return
        o = self.orch
        if typ != 'session_meta' and ts and self.partial and self._first_row is None:
            self._first_row = ts
            if self.front.state != 'done':                              # (a scan that was done before the first line came has read the front already)
                self.orch_log.set_front_lost(self.first_ts or ts, ts)       # the front of a big rollout was not read: from its start to the first line read
        if p is None:
            if pt == 'custom_tool_call_output':
                m = CX_CALL_ID_RE.search(r['head'])
                if m:
                    self._cx_execs.output(m.group(1).decode())
            elif pt == 'item_completed' and ts and (b'"CommandExecution"' in r['head'] or b'"FileChange"' in r['head']):
                self.orch_log.widen_lost(ts, ts)                   # a write or a command that was too long to be read
            return
        if typ == 'session_meta':
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
                self._cx_execs.clear()
            elif pt in ('task_complete', 'turn_aborted'):
                self.cx_turn.end(ts)
                self._cx_execs.clear()
            elif pt == 'item_completed':
                self._orch_item(ts, p.get('item'))
                if isinstance(p.get('item'), dict):
                    cx_item(self.orch_log, 'orch', ts, p['item'], self.cwd, None, self._cx_execs.only())
                    if p['item'].get('type') == 'CommandExecution' and shell_command(p['item'].get('command')) is not None:
                        self._cx_execs.finished(shell_command(p['item']['command']))
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
                if pt == 'custom_tool_call' and p.get('name') == 'exec' and p.get('call_id'):
                    self._cx_execs.add(p['call_id'], ts, p.get('input'))
                if name == 'request_user_input_async':
                    self._codex_ask(ts, p)
            elif pt in ('custom_tool_call_output', 'function_call_output'):
                o['last_ts'] = ts
                self.cx_turn.begin()
                if pt == 'custom_tool_call_output':
                    self._cx_execs.output(p.get('call_id'), p.get('output'))

    def front_scan(self, wait=False):
        """The same for the rollout of the thread of the page itself (its events are the orchestrator's)."""
        t = self.front.start(self.path, self.partial, self.orch_log, 'orch', self.cwd, self.fork.prefix if self.fork.sub else None) if self.partial else None
        if wait and t is not None:
            t.join()
        return t

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
