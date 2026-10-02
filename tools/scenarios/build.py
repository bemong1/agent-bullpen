"""Case -> synthetic HOME: Claude records (main, sub-agent + meta, `claude -p` children), Codex rollouts, output files, debate folders, a process snapshot per
observation phase. Nothing real is read: every name, path, id and time is made from the case id (sha1), so a rebuild is byte-identical.

The builder realises the *scene* an axis combination describes; it never says what the board should answer (that is oracle.py). Both read the
way/evidence tables in axes.py, so "the environment was cleared" means the same thing in the files and in the truth.
"""

import json
import os
import tempfile
import time

from .axes import FLAW_VERSION, VERSION, digest

T_BASE = 1790000000
OLD_VERSION = FLAW_VERSION['old_format']
FUTURE_VERSION = FLAW_VERSION['future_version']
NOTE_TEXT = ('A task-notification fires each time this agent stops with no live background children of its own. '
             'The user can send it another message and resume it, so the same task-id may notify more than once.')
MODEL = 'claude-sonnet-5-5'
NS = 'pid:[4026531836]'
START_OF = lambda pid: pid * 10                      # the fake /proc start time of a pid (clock ticks), also written as procStart in its session file


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.%03dZ' % int((t % 1) * 1000)


def dump(d):
    return json.dumps(d, separators=(',', ':'), ensure_ascii=False)


def sid_of(cid, role):
    """A session id (uuid shape) made from the case id and a role name."""
    h = digest(cid, role, n=32)
    return '%s-%s-4%s-8%s-%s' % (h[:8], h[8:12], h[13:16], h[17:20], h[20:32])


def agent_id_of(cid, role):
    return 'a' + digest(cid, role, n=16)


def put(path, text, t=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    if t:
        os.utime(path, (t, t))
    return path


WORDS = ('amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow nectar orchard prairie quartz ridge summit tundra umber '
         'valley willow xenon yarrow zephyr alloy beacon cobalt dune estuary flint garnet hollow iris jasper knoll lantern marble nickel opal pebble').split()


def pick(cid, role, i):
    """The i-th deterministic word of a (case, role): the same case and role always give the same sentence."""
    return WORDS[int(digest(cid, role, i, n=4), 16) % len(WORDS)]


def prose(cid, role, chars, lead='Please review the', tail='and report in plain words.'):
    """A synthetic instruction of about `chars` characters: plain words, one sentence, no quotes or shell characters."""
    out = []
    while len(lead) + len(tail) + sum(len(w) + 1 for w in out) < chars:
        out.append(pick(cid, role, len(out)))
    return ' '.join([lead] + out + [tail])


class Transcript:
    """One .jsonl record file: rows are (time, order, dict); saved in time order, mtime = the last row."""

    def __init__(self, path, sid, cwd, entry='cli', side=False, agent=None, cid='', version=VERSION):
        self.path, self.sid, self.cwd, self.entry, self.side, self.agent, self.cid = path, sid, cwd, entry, side, agent, cid
        self.version = version
        self.rows = []
        self.n = 0
        self.sdk_turn = 0                  # turnPosition.turnIndex keeps counting across the runs of one session, also after a resume

    def uuid(self):
        self.n += 1
        return sid_of(self.cid, '%s-line-%d' % (self.sid, self.n))

    def add(self, t, typ, **kw):
        d = {'parentUuid': None, 'isSidechain': self.side, 'type': typ, 'uuid': self.uuid(), 'timestamp': iso(t), 'userType': 'external',
             'entrypoint': self.entry, 'cwd': self.cwd, 'sessionId': self.sid, 'version': self.version}
        if self.agent:
            d['agentId'] = self.agent
        d.update(kw)
        self.rows.append((t, len(self.rows), d))
        return d

    def raw(self, t, d):
        self.rows.append((t, len(self.rows), d))

    # ---- lines ----
    def prompt(self, t, text, source='sdk', turn=None):
        """The user line a run starts with. source: sdk (claude -p), human (typed), system kinds (auto-continuation, task-notification ...),
        spawn (the first line of a sub-agent: no source markers at all), legacy (a record from before promptSource: a plain user line)."""
        kw = {'message': {'role': 'user', 'content': text}, 'promptId': sid_of(self.cid, 'prompt-%s-%d' % (self.sid, self.n))}
        if source == 'sdk':
            self.sdk_turn = turn if turn is not None else self.sdk_turn + 1
            kw.update(permissionMode='default', promptSource='sdk', turnOrigin='sdk', turnPosition={'promptIndex': 0, 'turnIndex': self.sdk_turn})
        elif source == 'human':
            kw.update(permissionMode='default', promptSource='typed', origin={'kind': 'human'})
        elif source in ('spawn', 'legacy'):
            pass
        else:
            kw.update(permissionMode='default', promptSource='system', origin={'kind': source})
        return self.add(t, 'user', **kw)

    def coordinator(self, t, text):
        """The line a sub-agent receives when its coordinator resumes it with a message (it opens a new run of that sub-agent)."""
        return self.add(t, 'user', message={'role': 'user', 'content': text}, promptId=sid_of(self.cid, 'coord-%s-%d' % (self.sid, self.n)), isMeta=True,
                        origin={'kind': 'coordinator'})

    def peer_message(self, t, body, name='sibling', pid=4242):
        """A cross-session message from a sibling session: a queued_command attachment, meta, with the sender in `origin` and the text in `origin.body`."""
        att = {'type': 'queued_command', 'prompt': 'Message from %s: %s' % (name, body), 'commandMode': 'prompt', 'isMeta': True,
               'delivery_id': sid_of(self.cid, 'delivery-%d' % self.n), 'source_uuid': sid_of(self.cid, 'source-%d' % self.n), 'timestamp': iso(t),
               'origin': {'kind': 'peer', 'from': 'uds:/run/user/1000/cc-socks/%d.sock' % pid, 'verifiedPeerPid': pid, 'verifiedPeerProcStart': str(pid * 10),
                          'msg_id': sid_of(self.cid, 'msg-%d' % self.n), 'name': name, 'fromMode': 'bypass', 'body': body}}
        return self.add(t, 'attachment', attachment=att)

    def tool(self, t, name, inp, tid, text=None):
        content = ([{'type': 'text', 'text': text}] if text else []) + [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]
        return self.add(t, 'assistant', message={'id': 'msg_' + tid[-8:], 'role': 'assistant', 'model': MODEL, 'stop_reason': 'tool_use', 'content': content,
                                                 'usage': {'input_tokens': 10, 'output_tokens': 5}})

    def say(self, t, text, stop='end_turn'):
        return self.add(t, 'assistant', message={'id': 'msg_%d' % self.n, 'role': 'assistant', 'model': MODEL, 'stop_reason': stop,
                                                 'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 10, 'output_tokens': 5}})

    def result(self, t, tid, text='ok', is_error=False, bg=None, extra=None):
        tur = {'stdout': text, 'stderr': '', 'interrupted': False}
        if bg:
            tur['backgroundTaskId'] = bg
        if extra:
            tur.update(extra)
        return self.add(t, 'user', message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': text, 'is_error': is_error}]},
                        toolUseResult=tur)

    def bash(self, t, cmd, tid, end=None, desc='run', bg=None, notif=None, notif_status='completed', notif_text=None, is_error=False, bg_flag=False):
        """A Bash call. end: result time (foreground) / the immediate result of a background call. notif: time of the task-notification of a background call."""
        inp = {'command': cmd, 'description': desc}
        if bg:
            inp['run_in_background'] = True
        self.tool(t, 'Bash', inp, tid)
        if end is not None:
            self.result(end, tid, 'started' if bg else 'done', is_error=is_error, bg=bg)
        if bg and notif is not None:
            text = notif_text or 'Background command "%s" completed (exit code 0)' % desc
            self.notification(notif, bg, tid, notif_status, text)

    def notification(self, t, task, tid, status, summary):
        body = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/claude-synth/tasks/%s.output</output-file>\n'
                '<status>%s</status>\n<summary>%s</summary>\n<note>%s</note>\n</task-notification>') % (task, tid, task, status, summary, NOTE_TEXT)
        return self.prompt(t, body, source='task-notification')

    def cost_state(self, t, total_ms, drop=()):
        """The `cost-state` line that closes a run; `drop` names fields that vanished (a format change inside the observed range)."""
        d = {'type': 'cost-state', 'sessionId': self.sid, 'totalCostUSD': 0.01, 'totalAPIDuration': 1000, 'totalToolDuration': 10,
             'totalLinesAdded': 0, 'totalLinesRemoved': 0, 'totalDuration': total_ms, 'startTime': int(t * 1000), 'modelUsage': {}, 'hasUnknownModelCost': False}
        for k in drop:
            d.pop(k, None)
        self.raw(t, d)

    def api_error(self, t, status=429, kind='rate_limit', resets_at=None, limit_type='five_hour', text='API error'):
        d = self.add(t, 'assistant', message={'id': 'msg_err%d' % self.n, 'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence',
                                              'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 0, 'output_tokens': 0}},
                     error=kind, isApiErrorMessage=True, apiErrorStatus=status)
        if resets_at is not None:
            d['quotaLimits'] = {'status': 'rejected', 'resetsAt': resets_at, 'rateLimitType': limit_type}
        return d

    def notice(self, t, text):
        return self.add(t, 'system', subtype='informational', content=text, level='notice', isMeta=False)

    def turn_end(self, t, ms=1000):
        return self.add(t, 'system', subtype='turn_duration', durationMs=ms, isMeta=True, messageCount=2)

    def save(self):
        rows = sorted(self.rows, key=lambda r: r[:2])
        put(self.path, ''.join(dump(d) + '\n' for _, _, d in rows), rows[-1][0] if rows else None)


# ---------------------------------------------------------------------------------------------------------------------
class Phase:
    """One look at the board: the time, which processes exist, and whether the board process is a fresh one (a restart)."""

    def __init__(self, now, procs, boot=0, note=''):
        self.now, self.procs, self.boot, self.note = now, procs, boot, note


class Built:
    """What the builder hands to the oracle-free observer: paths, ids, phases."""

    def __init__(self, case, root):
        self.case, self.root = case, root
        self.home = os.path.join(root, 'home')
        self.claude = os.path.join(self.home, '.claude')
        self.projects = os.path.join(self.claude, 'projects')
        self.sess_dir = os.path.join(self.claude, 'sessions')
        self.codex = os.path.join(self.home, '.codex')
        self.cache = os.path.join(root, 'cache')
        self.work = os.path.join(self.home, 'work')
        self.scratch = os.path.join(self.home, 'scratch')
        self.ids = {}                      # role -> session / agent / thread id
        self.paths = {}                    # role -> path
        self.phases = []
        self.main_path = None              # the orchestrator record the board opens
        self.meta = {}                     # scene facts the observer needs (debate unit paths ...)
        self.t0 = T_BASE + int(digest(case.id, 'base', n=6), 16) % 400000        # 21-25 Sep 2026: never ahead of the real clock, so a reader that looks at the real time sees the same
        for d in (self.projects, self.sess_dir, os.path.join(self.codex, 'sessions'), self.cache, self.work, self.scratch):
            os.makedirs(d, exist_ok=True)
        os.chmod(self.cache, 0o700)          # the links cache is only read from a folder nobody else can write (a group-writable umask would make it untrusted)

    def T(self, off):
        return self.t0 + off

    def project_dir(self, cwd):
        d = os.path.join(self.projects, '-' + cwd.strip('/').replace('/', '-'))
        os.makedirs(d, exist_ok=True)
        return d

    def sid(self, role):
        if role not in self.ids:
            self.ids[role] = sid_of(self.case.id, role)
        return self.ids[role]

    def transcript(self, role, cwd, entry='cli', sid=None):
        sid = sid or self.sid(role)
        self.ids[role] = sid
        t = Transcript(os.path.join(self.project_dir(cwd), sid + '.jsonl'), sid, cwd, entry, cid=self.case.id)
        self.paths[role] = t.path
        return t


def proc(pid, ppid, argv, env=None, session=None, uid=None, start=None, fds=(), mac_hidden=False):
    return {'pid': pid, 'ppid': ppid, 'argv': list(argv), 'env': env, 'session': session, 'uid': uid, 'start': start if start is not None else START_OF(pid),
            'fds': list(fds)}


def session_file(b, pid, sid, cwd, entry, t, start=None, name=None, status=None, proc_start='auto'):
    d = {'pid': pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': int(t * 1000), 'kind': 'interactive', 'entrypoint': entry}      # a `claude -p` session file says interactive too
    status = status or ('busy' if entry == 'sdk-cli' else 'idle')
    if proc_start == 'auto':
        d['procStart'] = str(start if start is not None else START_OF(pid))
    elif proc_start is not None:
        d['procStart'] = proc_start
    d['pidDomain'] = 'linux:abc:' + NS
    if status:
        d['status'] = status
        d['statusUpdatedAt'] = int(t * 1000)
    if name:
        d['name'] = name
    return d


_FOLDS = {}                                           # device number -> whether that file system folds case


def folds_case(folder):
    """Whether `folder` is on a file system that treats `B.md` and `b.md` as one file (the default on macOS and Windows). False when it cannot be told."""
    try:
        dev = os.stat(folder).st_dev
        if dev not in _FOLDS:
            fd, probe = tempfile.mkstemp(prefix='CaseProbe', dir=folder)
            os.close(fd)
            try:
                _FOLDS[dev] = os.path.exists(os.path.join(os.path.dirname(probe), os.path.basename(probe).swapcase()))
            finally:
                os.unlink(probe)
        return _FOLDS[dev]
    except OSError:
        return False


def needs_two_names_by_case(case):
    """Whether the scene writes two files whose names differ only in case (a debate with `nstyle=collide`: B.md and b.md), which a case-folding file system cannot hold."""
    return case.bundle == 'deb' and case.v['nstyle'] == 'collide'


def build_case(case, root):
    """Builds `case` under the folder `root` (made if absent) and returns the Built."""
    os.makedirs(root, exist_ok=True)
    b = Built(case, root)
    bundle = case.bundle
    if bundle == 'aff':
        from .scene_aff import Aff
        Aff(b).build()
    elif bundle == 'deb':
        from .scene_deb import build_deb
        build_deb(b)
    elif bundle == 'sta':
        from .scene_sta import build_sta
        build_sta(b)
    else:
        from .scene_deb import build_cpl
        build_cpl(b)
    return b
