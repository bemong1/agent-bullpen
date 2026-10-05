"""The Codex orchestrator scene: a page whose top is a Codex thread (TUI or `exec`) or, for one chain, a Claude session; the team around it (native sub-agent threads, `claude -p`
and `codex exec` runs started from a shell, a guardian thread, a debate folder) and the lookalikes that are no team (a session that relays the instruction with
`tmux send-keys`, a second orchestrator of the folder that says the same words, a script that runs `codex exec`, a Codex thread with a command nobody can read yet).

The scene writes only what exists on disk and in the process table: the Codex rollouts (tools/scenarios/cx_record.py), the Claude records, the output and report files,
the process lineage and environment of each look. What a reader may conclude from that is the oracle's business (the helpers of axes.py are the one place both agree).
"""

import os

from .axes import CXO_OWN_LAUNCHES, CXO_RELAYS, CXO_TWINS, cxo_alive, digest
from .build import MODEL, Phase, Transcript, agent_id_of, dump, proc, prose, put, session_file
from .cx_record import MODEL as CX_MODEL, Rollout, rollout_path, tid_of
from .scene_aff import out_json, toolu
from .scene_deb import brief_text

CHILD_START = 2.4            # the child's first record, seconds after the call that starts it
CLAUDE_RUN = 9.0             # a `claude -p` run: first record to `cost-state`
CODEX_RUN = 7.0              # a `codex exec` run: first record to `task_complete`
LONG_RUN = 130.0             # a run that is long enough for its command's record to come after the board has looked once (`rec=late`)
GUARD_AT = -30.0
SUB_AT = 10.0                # the spawn of the first native sub-agent
SUB2_AT = 20.0               # the spawn of the second one (below the first)
SUB_MID = 30.0               # a report in the middle of a sub-agent's work
SUB_END = 50.0               # a native sub-agent finishes
SUB_INT_MID = 35.0           # an `interrupt_agent` that comes while it works
SUB_INT_AFTER = 60.0         # ... and one that comes after it finished
TOP_AT = -60.0               # the top's own instruction
STALE_AT = -7200.0           # a Codex thread that has been over for two hours
RELAY_AT = -5.0              # a session passes the instruction on to the terminal of the top


def child_env(env, launcher, root, claude=None):
    """The environment of a process the shell of thread `launcher` (tree `root`) started: the names of Codex, those of both providers (`claude` = (session id, pid) of the
    Claude session above), or none. The other names Codex sets (version, CI) are there too: the board reads two of them and drops the rest."""
    out = {}
    if env == 'both' and claude:
        out.update(CLAUDE_CODE_SESSION_ID=claude[0], CLAUDE_PID=str(claude[1]))
    if env in ('codex', 'both'):
        out.update(CODEX_THREAD_ID=launcher, CODEX_SESSION_ID=root, CODEX_VERSION='0.160.0', CODEX_CI='1')
    return out


class Cxo:
    """Builds one `cxo` case into the Built `b`."""

    def __init__(self, b):
        self.b, self.case, self.v, self.cid = b, b.case, b.case.v, b.case.id
        self.W = os.path.join(b.work, 'repo')
        os.makedirs(os.path.join(self.W, '.git'), exist_ok=True)
        put(os.path.join(self.W, '.git', 'HEAD'), 'ref: refs/heads/main\n')
        self.files = {}                                       # path -> text, written at the end
        self.rolls = []                                       # every Rollout of the scene (saved at the end)
        self.trs = []                                         # every Transcript of the scene (saved at the end)
        self.turn = {}                                        # thread id -> its turn id
        self.late = None                                      # (rollout, row) of a record that comes after the board's first look
        self.root_text = prose(self.cid, 'top-task', 160)
        self.top_at = TOP_AT                                  # when the top is told what to do

    # ---- ids and times ----
    def tid(self, role):
        t = tid_of(self.cid, role)
        self.b.ids[role] = t
        return t

    def turn_of(self, tid):
        return self.turn.setdefault(tid, 'turn-' + digest(self.cid, 'turn', tid, n=16))

    def at(self, off):
        return self.b.T(off)

    # ---- Codex threads ----
    def root(self, role, origin, t, text):
        """A root thread: its meta, first turn and the user's message."""
        tid = self.tid(role)
        r = Rollout(rollout_path(self.b.codex, tid, t), tid, self.W, self.cid, kind='root', origin=origin)
        r.meta(t)
        turn = self.turn_of(tid)
        r.task_started(t + 0.5, turn)
        r.turn_context(t + 0.6, turn)
        r.user(t + 1.0, text, turn)
        self.rolls.append(r)
        self.b.paths[role] = r.path
        return r

    def sub(self, role, parent, t, task, nick, depth, parent_text, parent_started):
        """A native sub-agent of `parent` (a Rollout), spawned at `t` by the parent's `spawn_agent` call. The parent's rollout gets the call and the `started` line, the
        sub-agent's its meta, the front part of its parent and its own first turn; its first message is the forwarding notice with the ciphertext of the instruction."""
        tid = self.tid(role)
        path = '%s/%s' % (parent.agent_path.rstrip('/'), task)
        pt = self.turn_of(parent.tid)
        parent.spawn(t, toolu(self.b, 'spawn-' + role), pt, task, tid, path)
        s = Rollout(rollout_path(self.b.codex, tid, t + 0.06), tid, self.W, self.cid, kind='sub', origin=parent.origin, parent=parent.tid, agent_path=path, nick=nick, depth=depth,
                    root_id=parent.root_id)
        s.fork(t + 0.06, parent, pt, parent_text, parent_started)
        turn = self.turn_of(tid)
        s.task_started(t + 0.07, turn)
        s.turn_context(t + 0.1, turn)
        notice = self.b.ids['notice_' + role] = 'Message from %s: your task is in the attached content.' % parent.agent_path
        s.agent_message(t + 0.2, parent.agent_path, path, notice, cipher=True, turn=turn)
        s.usage(t + 2.0, turn, 1200, 90)
        s.usage(t + 6.0, turn, 1500, 120)
        self.rolls.append(s)
        self.b.paths[role] = s.path
        return s

    def guardian(self, parent):
        """An approval-review thread of the top: three model calls, no turn of the team."""
        tid = self.tid('guardian')
        g = Rollout(rollout_path(self.b.codex, tid, self.at(GUARD_AT)), tid, self.W, self.cid, kind='guardian', origin=parent.origin, parent=parent.tid, root_id=parent.root_id)
        g.meta(self.at(GUARD_AT))
        turn = self.turn_of(tid)
        g.task_started(self.at(GUARD_AT + 0.5), turn)
        for i in range(3):
            g.usage(self.at(GUARD_AT + 1 + i), turn, 800 + 10 * i, 40)
        g.complete(self.at(GUARD_AT + 5), turn, 'approved')
        self.rolls.append(g)
        return g

    # ---- shell calls ----
    def launch_cmd(self, kind, text, how, extra='', out=None):
        """The command of a shell call that starts a `claude -p` or a `codex exec` run in the foreground, or with `&` / `setsid nohup ... &` (`how`). `out`: a file the output of
        the run is redirected to."""
        if kind == 'cl':
            core = 'claude -p --model %s%s "%s"' % (MODEL, extra, text)
        else:
            core = 'codex exec -m %s%s "%s"' % (CX_MODEL, extra, text)
        sink = out or '/dev/null'
        if how == 'detach':
            return 'cd %s && setsid nohup %s > %s 2>&1 < /dev/null &' % (self.W, core, sink)
        if how == 'bg':
            return 'cd %s && nohup %s > %s 2>&1 &' % (self.W, core, sink)
        return 'cd %s && %s%s' % (self.W, core, ' > %s' % out if out else '')

    def call(self, L, t, cmd, key, child_end, how, rec='end'):
        """The shell call of thread `L` (a Rollout) at `t`. A foreground call returns when the child ends (`child_end`; None: still running, no record yet); a `&` call returns
        at once. The record of the command is written when its process ends, never when `rec=lost`, and a long while after the child started when `rec=late` (the call's
        output then says the command is still running, and the record is held back for the second look)."""
        turn = self.turn_of(L.tid)
        cid = toolu(self.b, key)
        if how == 'fg':
            end = None if child_end is None else child_end + 0.1
            out_at = end
        else:
            end, out_at = t + 0.4, t + 0.45
        if rec == 'late':
            L.shell(t, cmd, self.W, cid, turn, end=None, out_at=t + 1.0, running=True)
            L.shell_done(cmd, self.W, cid, turn, t, end)
            self.late = (L, L.rows.pop())
        else:
            L.shell(t, cmd, self.W, cid, turn, end=end if rec == 'end' else None, out_at=out_at)
        return end

    # ---- Claude records ----
    def claude_run(self, role, cwd, text, t0, finished, report=None, sub=None, length=CLAUDE_RUN):
        """A `claude -p` run (the record of a `sdk-cli` session): its first line, a tool call, and, when finished, the end of its turn and `cost-state`. `report` = a file it
        writes (a Write call)."""
        sid = self.b.sid(role)
        tr = self.b.transcript(role, cwd, 'sdk-cli', sid=sid)
        tr.prompt(t0, text, source='sdk')
        tid = toolu(self.b, 'work-' + role)
        tr.tool(t0 + 3, 'Read', {'file_path': os.path.join(cwd, 'notes.md')}, tid)
        tr.result(t0 + 4, tid, 'notes')
        if sub:
            self.claude_sub(tr, sub, t0 + 4.5, finished)
        if report and finished:
            wid = toolu(self.b, 'report-' + role)
            tr.tool(t0 + 6, 'Write', {'file_path': report[0], 'content': report[1]}, wid)
            tr.result(t0 + 6.5, wid, 'written')
            self.files[report[0]] = report[1]
        if finished:
            tr.say(t0 + length - 1, 'Finished the review.', 'end_turn')
            tr.cost_state(t0 + length, 9000)
        else:
            tr.tool(t0 + 7, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(self.b, 'live-' + role))
        self.trs.append(tr)
        self.b.paths[role] = tr.path
        return tr

    def claude_sub(self, K, role, at, finished):
        """A sub-agent of the `claude -p` run `K`: the Agent call, its result, its meta file and its record. The agent id is the one of `role`."""
        b = self.b
        aid = b.ids[role] = agent_id_of(self.cid, role)
        tu = toolu(b, 'spawn-' + role)
        task = 'Check the notes and report back.'
        K.tool(at, 'Agent', {'description': 'Notes checker', 'prompt': task}, tu)
        K.result(at + 0.3, tu, 'Async agent launched', extra={'isAsync': True, 'status': 'async_launched', 'agentId': aid, 'description': 'Notes checker', 'prompt': task,
                                                             'outputFile': '/tmp/claude-synth/tasks/%s.output' % aid, 'canReadOutputFile': True})
        d = os.path.join(os.path.dirname(K.path), K.sid, 'subagents')
        put(os.path.join(d, 'agent-%s.meta.json' % aid), dump({'agentType': 'general-purpose', 'description': 'Notes checker', 'requestNonInteractive': True,
                                                              'requestShape': 'background', 'spawnDepth': 1, 'toolUseId': tu}))
        S = Transcript(os.path.join(d, 'agent-%s.jsonl' % aid), K.sid, self.W, 'sdk-cli', side=True, agent=aid, cid=self.cid)
        S.prompt(at + 1, task, source='spawn')
        if finished:
            S.say(at + 3, 'Checked.', 'end_turn')
        else:
            S.tool(at + 3, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'sub-live'))
        b.paths[role] = S.path
        self.trs.append(S)
        return S

    # ---- Codex runs ----
    def codex_run(self, role, t0, text, finished, report=None, length=CODEX_RUN):
        """A `codex exec` run: its rollout (root thread, source "exec"); `report` = the file `-o` writes at the end."""
        r = self.root(role, 'codex_exec', t0, text)
        turn = self.turn_of(r.tid)
        r.usage(t0 + 3, turn, 1000, 80)
        if finished:
            r.say(t0 + length - 1, 'Finished the review.', turn, final=True)
            r.complete(t0 + length, turn, 'Finished the review.', started=t0)
            if report:
                self.files[report[0]] = report[1]
        return r

    def finish(self, main_path, alive, dead, now_live, now_dead):
        """Writes everything and fixes the looks: one (alive, or after everything is over); for `rec=late` two (the child still running and unlinked, then over with the late
        record written)."""
        b, v = self.b, self.v
        b.main_path = main_path
        for p, t in self.files.items():
            put(p, t)
        first = now_live if self.late else None                # a late record: the first look sees the files as they were then, the rest is written between the looks
        for r in self.rolls:
            r.save(first)
        for tr in self.trs:
            tr.save(first)
        if self.late:
            L, row = self.late

            def arrive():
                L.rows.append(row)
                for r in self.rolls:
                    r.save()
                for tr in self.trs:
                    tr.save()
            b.phases = [Phase(now_live, alive, 0), Phase(now_dead, dead, 0, hook=arrive)]
        elif v['look'] == 'live':
            b.phases = [Phase(now_live, alive, 0)]
        else:
            b.phases = [Phase(now_dead, dead, 0)]

    # ---- the cases ----
    def build(self):
        v = self.v
        self.b.meta['repo'] = self.W
        self.b.meta['page'] = 'claude' if v['top'] == 'claude' else 'codex'
        if v['subj'] == 'cx_sub':
            return self.build_sub()
        chain = v['chain']
        if chain == 'one':
            return self.build_one()
        if chain == 'cl>cx>cl':
            return self.build_clcxcl()
        if chain == 'cx>cl>sub':
            return self.build_cxclsub()
        return self.build_cxcx()

    def top_text(self, cmd):
        """What the orchestrator is told by a relay: the whole command to run (the command line, with the child's instruction in it, travels as typed text)."""
        return 'Run this command now and report what it prints: %s' % cmd

    def top_thread(self):
        """The page's Codex thread, and the guardian beside it."""
        v = self.v
        self.b.ids['root_text'] = self.root_text
        top = self.root('top', 'codex_exec' if v['top'] == 'cx_exec' else 'codex-tui', self.at(self.top_at), self.root_text)
        top.say(self.at(self.top_at + 4), 'Starting the work.', self.turn_of(top.tid))
        if v['guard'] == 'one':
            self.guardian(top)
        return top

    def codex_proc(self, top):
        """The Codex process of the page: a TUI or an `exec`, holding the rollout of the top (a sub-agent's rollout is not held open)."""
        argv = ['codex', 'exec', '-m', CX_MODEL] if self.v['top'] == 'cx_exec' else ['codex', '-m', CX_MODEL]
        return proc(300, 1, argv, env={}, fds=[top.path])

    def host_thread(self, top):
        """The thread whose shell makes the first call: the top, or a native sub-agent of it (`host=sub`)."""
        if self.v['host'] != 'sub':
            return top
        return self.sub('host', top, self.at(-40), 's1', 'Atlas', 1, self.root_text, self.at(TOP_AT))

    def end_host(self, top, L, finished, at):
        """A sub-agent that ran the call is over when the child is."""
        if L is not top and finished:
            L.say(self.at(at - 1), 'Done with the helper.', self.turn_of(L.tid), final=True)
            L.complete(self.at(at), self.turn_of(L.tid), 'Done with the helper.')

    # ---- one hop: the page's own thread starts the subject ----
    def build_one(self):
        b, v, cid, W = self.b, self.v, self.cid, self.W
        kind = 'cx' if v['subj'] == 'cx' else 'cl'
        text = b.ids['child_text'] = prose(cid, 'child', 200)
        report = None
        if v['topic'] == 'talk':
            letter = 'B' if kind == 'cx' else 'A'
            rpath = os.path.join(W, 'talk', 'r1', letter + '.md')
            self.files[os.path.join(W, 'talk', 'brief.md')] = brief_text('r1')
            report = (rpath, 'Findings of %s: all is in order.\n' % letter)
            if kind == 'cl':
                text = '%s Write your report to %s.' % (text, rpath)
            b.meta['report'] = rpath
        extra = ' -o %s' % report[0] if (report and kind == 'cx') else ''
        out = None
        if v['lure'] == 'twin_out':                            # the command redirects the output of the run to a file; the file holds the run's session id once it is over
            extra, out = ' --output-format json', os.path.join(b.scratch, 'out_x.json')
        cmd = self.launch_cmd(kind, text, v['how'], extra, out)
        if v['lure'] in CXO_RELAYS:
            self.root_text = self.top_text(cmd)
            self.top_at = RELAY_AT + 1.0                       # the terminal gets the words a moment after the relay's call
        if v['env'] == 'stale':
            self.top_at = STALE_AT                              # the thread whose names the child carries was over hours ago
        top = self.top_thread()
        if v['lure'] == 'gap':
            return self.build_gap(top, text)
        if v['lure'] in CXO_OWN_LAUNCHES:
            return self.build_own_launch(top, text)
        if v['env'] == 'stale':
            return self.build_stale(top, text)
        if v['lure'] == 'pin_unknown':
            return self.build_pin_unknown(top, text)
        if v['lure'] == 'pin_stale_claude':
            return self.build_pin_stale_claude(top, text)
        L = self.host_thread(top)
        finished = v['look'] == 'ended'
        late = v['rec'] == 'late'
        length = LONG_RUN if late else (CODEX_RUN if kind == 'cx' else CLAUDE_RUN)
        tc, t0c = self.at(0), self.at(CHILD_START)
        child_end = (t0c + length) if finished else None
        if v['lure'] == 'user_script':
            decoy = 'cd %s && codex exec -m %s "%s"' % (W, CX_MODEL, prose(cid, 'other-task', 150))
            top.shell(self.at(-6), decoy, W, toolu(b, 'decoy'), self.turn_of(top.tid), end=self.at(10), out_at=self.at(10.05))
            self.codex_run('child', t0c, text, finished)
        else:
            self.call(L, tc, cmd, 'launch', child_end if v['how'] == 'fg' else None, v['how'], v['rec'])
            if out and finished:
                self.files[out] = out_json(b.sid('child'))
            if out and not finished:
                self.files[out] = ''
            if v['how'] != 'bg':
                if kind == 'cl':
                    self.claude_run('child', W, text, t0c, finished, report=report, length=length)
                else:
                    self.codex_run('child', t0c, text, finished, report=report, length=length)
        self.lookalikes(text, finished)
        self.end_host(top, L, finished, 15 if not late else 140)
        self.processes_one(top, L, kind, text, cmd, t0c, late)

    def lookalikes(self, text, finished):
        """The lookalikes: a session that relays the instruction, a second orchestrator of the folder with the same words."""
        b, v = self.b, self.v
        if v['lure'] in CXO_RELAYS:
            R = b.transcript('relayer', self.W, 'cli')              # a session of the same repository, so the folder refutes nothing
            R.prompt(b.T(-400), 'Pass the instruction on to the Codex window.', source='human')
            lure = v['lure']
            if lure == 'relay':
                cmd = "tmux send-keys -t cx:0 -l '%s' && tmux send-keys -t cx:0 Enter" % self.root_text
            elif lure == 'relay_py':
                cmd = "python3 -c 'import subprocess; subprocess.run([\"tmux\", \"send-keys\", \"-t\", \"cx:0\", \"-l\", \"%s\"])'" % self.root_text.replace('"', '\\"')
            elif lure == 'relay_script':
                # a script that only types the words into a window; the call that runs it carries the words as its argument
                path, body = os.path.join(b.scratch, 'relay.sh'), 'tmux send-keys -t w "claude -p \\"$1\\"" Enter\n'
                self.files[path] = body
                R.bash(b.T(RELAY_AT - 30), "cat > %s <<'EOF'\n%sEOF" % (path, body), toolu(b, 'relay-write'), end=b.T(RELAY_AT - 29.5), desc='write the relay script')
                cmd = 'bash %s "%s"' % (path, text)
            elif lure == 'relay_pyfile':
                # a Python file that only types the words into a window; the call that runs it carries the words as its argument
                path = os.path.join(b.scratch, 'relay.py')
                body = 'import subprocess, sys\nsubprocess.run(["tmux", "send-keys", "-t", "w", "claude -p \\"%s\\"" % sys.argv[1], "Enter"])\n'
                self.files[path] = body
                R.bash(b.T(RELAY_AT - 30), "cat > %s <<'EOF'\n%sEOF" % (path, body), toolu(b, 'relay-write'), end=b.T(RELAY_AT - 29.5), desc='write the relay file')
                cmd = 'python3 %s "%s"' % (path, text)
            elif lure == 'relay_xargs':
                cmd = "printf '%%s\\n' w | xargs -I{} tmux send-keys -t {} 'claude -p \"%s\"' Enter" % text
            elif lure == 'relay_ssh':
                cmd = "ssh h echo 'claude -p \"%s\"'" % text
            elif lure == 'relay_kube':
                cmd = "kubectl exec pod -- tmux send-keys -t w 'claude -p \"%s\"' Enter" % text
            else:                                                    # relay_curl: the words are an argument of a program that is no launcher
                cmd = 'curl --data claude -p --data "%s" https://example.invalid' % text
            R.bash(b.T(RELAY_AT), cmd, toolu(b, 'relay'), end=b.T(RELAY_AT + 0.5), desc='pass the instruction on')
            self.trs.append(R)
            self.side = [proc(150, 1, ['claude'], env={}, session=session_file(b, 150, R.sid, self.W, 'cli', b.T(-400)))]
        elif v['lure'] in CXO_TWINS:
            C = b.transcript('orchc', self.W, 'cli')
            C.prompt(b.T(-400), 'Another orchestrator at work in this folder.', source='human')
            twin_out = os.path.join(b.scratch, 'out_c.json')
            C.bash(self.at(0.5), 'cd %s && claude -p --model %s%s "%s"%s' % (self.W, MODEL, ' --output-format json' if v['lure'] == 'twin_out' else '', text,
                                                                           ' > %s' % twin_out if v['lure'] == 'twin_out' else ''), toolu(b, 'twin-call'),
                   end=self.at(CHILD_START + CLAUDE_RUN + 0.5) if finished else None, desc='launch child')
            self.trs.append(C)
            self.claude_run('twin', self.W, text, self.at(CHILD_START + 0.3), finished)
            if v['lure'] == 'twin_out':
                self.files[twin_out] = out_json(b.sid('twin')) if finished else ''
            self.side = [proc(110, 1, ['claude'], env={}, session=session_file(b, 110, C.sid, self.W, 'cli', b.T(-400)))]
            if not finished:
                self.side += [proc(111, 110, ['/bin/bash', '-c', 'eval "claude -p"']),
                              proc(112, 111, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': C.sid, 'CLAUDE_PID': '110'},
                                   session=session_file(b, 112, b.ids['twin'], self.W, 'sdk-cli', self.at(CHILD_START + 0.3)))]

    def processes_one(self, top, L, kind, text, cmd, t0c, late):
        b, v = self.b, self.v
        base = [self.codex_proc(top)] + getattr(self, 'side', [])
        alive = list(base)
        env = child_env(v['env'], L.tid, top.tid)
        fg = v['how'] == 'fg'
        if v['lure'] == 'user_script':
            if v['look'] == 'live':
                alive += [proc(308, 1, ['bash', os.path.join(b.scratch, 'run.sh')]), proc(302, 308, ['codex', 'exec', '-m', CX_MODEL, text], env={}, fds=[b.paths['child']])]
        elif cxo_alive(v):
            if fg:
                alive.append(proc(301, 300, ['/bin/bash', '-lc', cmd]))
            ppid = 301 if fg else 1
            if kind == 'cl':
                alive.append(proc(302, ppid, ['claude', '-p', '--model', MODEL], env=env, session=session_file(b, 302, b.ids['child'], self.W, 'sdk-cli', t0c)))
            else:
                alive.append(proc(302, ppid, ['codex', 'exec', '-m', CX_MODEL, text], env=env, fds=[b.paths['child']]))
        first = base if late else alive            # the first look of a late record: the child runs in a namespace the board cannot see into
        self.finish(top.path, first, base, t0c + (60 if late else 20), t0c + 300)

    def build_gap(self, top, text):
        """The page's Codex thread has a command that is still running and whose record has not come, and a Claude session of the folder has a call with the same words.
        By default the Claude session started the child and it is over when the board looks (only the words can tell whose it is). With `env=none` the child is the Codex thread's:
        its shell started it with `tmux new-window` (nothing of Codex or Claude in its environment) inside a command that has not returned, and it is still running, as is the
        Claude call (with a child of its own)."""
        b, v, W = self.b, self.v, self.W
        k1 = v['env'] == 'none'
        C = b.transcript('orchc', W, 'cli')
        C.prompt(b.T(-400), 'Another orchestrator at work in this folder.', source='human')
        c_proc = proc(110, 1, ['claude'], env={}, session=session_file(b, 110, C.sid, W, 'cli', b.T(-400)))
        if not k1:
            top.shell(self.at(-1), 'sleep 600', W, toolu(b, 'long'), self.turn_of(top.tid), end=None, out_at=self.at(-0.9), running=True)
            C.bash(self.at(0.5), 'cd %s && claude -p --model %s "%s"' % (W, MODEL, text), toolu(b, 'gap-call'), end=self.at(CHILD_START + CLAUDE_RUN + 0.5), desc='launch child')
            self.trs.append(C)
            self.claude_run('child', W, text, self.at(CHILD_START + 0.3), True)
            base = [self.codex_proc(top), c_proc]
            return self.finish(top.path, base, base, self.at(20), self.at(300))
        top.shell(self.at(-1), "cd %s && tmux new-window -t w 'claude -p --model %s \"%s\"'; sleep 600" % (W, MODEL, text), W, toolu(b, 'long'), self.turn_of(top.tid), end=None,
                  out_at=self.at(-0.9), running=True)
        C.bash(self.at(0.5), 'cd %s && claude -p --model %s "%s"' % (W, MODEL, text), toolu(b, 'gap-call'), end=None, desc='launch child')
        self.trs.append(C)
        self.claude_run('child', W, text, self.at(CHILD_START), False)
        self.claude_run('twin', W, text, self.at(CHILD_START + 0.3), False)
        base = [self.codex_proc(top), c_proc]
        alive = base + [proc(302, 1, ['claude', '-p', '--model', MODEL], env={}, session=session_file(b, 302, b.ids['child'], W, 'sdk-cli', self.at(CHILD_START))),
                        proc(111, 110, ['/bin/bash', '-c', 'eval "claude -p"']),
                        proc(112, 111, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': C.sid, 'CLAUDE_PID': '110'},
                             session=session_file(b, 112, b.ids['twin'], W, 'sdk-cli', self.at(CHILD_START + 0.3)))]
        self.finish(top.path, alive, base, self.at(20), self.at(300))

    def build_own_launch(self, top, text):
        """A Claude session of the folder starts the child itself through a wrapper that is a launch: `tmux new-session -d '...'` (the child belongs to the tmux server: no names, no
        lineage) or `printf ... | xargs -I{} claude -p "{}"`. The Codex thread of the page has been quiet for a while (its turn is over), so nothing there is a command to be read."""
        b, v, W = self.b, self.v, self.W
        top.complete(self.at(-30), self.turn_of(top.tid), 'Idle.', started=self.at(self.top_at))
        finished = v['look'] == 'ended'
        tc, t0c = self.at(0), self.at(CHILD_START)
        C = b.transcript('orchc', W, 'cli')
        C.prompt(b.T(-400), 'Another orchestrator at work in this folder.', source='human')
        base = [self.codex_proc(top), proc(110, 1, ['claude'], env={}, session=session_file(b, 110, C.sid, W, 'cli', b.T(-400)))]
        alive = list(base)
        if v['lure'] == 'launch_tmux':
            C.bash(tc, "cd %s && tmux new-session -d -s w 'claude -p --model %s \"%s\"'" % (W, MODEL, text), toolu(b, 'own-call'), end=tc + 0.4, desc='launch child')
            if not finished:
                alive.append(proc(302, 1, ['claude', '-p', '--model', MODEL], env={}, session=session_file(b, 302, b.sid('child'), W, 'sdk-cli', t0c)))
        elif v['lure'] == 'launch_pyfile':
            # a Python file that really runs `claude -p`: the call that runs it carries the words as its argument
            path = os.path.join(b.scratch, 'launch.py')
            body = 'import subprocess, sys\nsubprocess.run(["claude", "-p", "--model", "%s", sys.argv[1]])\n' % MODEL
            self.files[path] = body
            C.bash(self.at(-30), "cat > %s <<'EOF'\n%sEOF" % (path, body), toolu(b, 'own-write'), end=self.at(-29.5), desc='write the launch file')
            C.bash(tc, 'cd %s && python3 %s "%s"' % (W, path, text), toolu(b, 'own-call'), end=(t0c + CLAUDE_RUN + 0.1) if finished else None, desc='launch child')
            if not finished:
                alive += [proc(111, 110, ['/bin/bash', '-c', 'eval "run"']), proc(112, 111, ['python3', path, text]),
                          proc(113, 112, ['claude', '-p', '--model', MODEL, text], env={'CLAUDE_CODE_SESSION_ID': C.sid, 'CLAUDE_PID': '110'},
                               session=session_file(b, 113, b.sid('child'), W, 'sdk-cli', t0c))]
        else:
            cmd = "cd %s && printf '%%s\\n' \"%s\" | xargs -I{} claude -p --model %s \"{}\"" % (W, text, MODEL)
            C.bash(tc, cmd, toolu(b, 'own-call'), end=(t0c + CLAUDE_RUN + 0.1) if finished else None, desc='launch child')
            if not finished:
                alive += [proc(111, 110, ['/bin/bash', '-c', 'eval "run"']), proc(112, 111, ['xargs', '-I{}', 'claude', '-p']),
                          proc(113, 112, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': C.sid, 'CLAUDE_PID': '110'},
                               session=session_file(b, 113, b.sid('child'), W, 'sdk-cli', t0c))]
        self.trs.append(C)
        self.claude_run('child', W, text, t0c, finished)
        self.finish(top.path, alive, base, t0c + 20, t0c + 300)

    def build_stale(self, top, text):
        """The names of a Codex thread that was over hours ago are in the environment of the child: a tmux server that one of its shells started long ago passes them on. A Claude
        session started the child with `tmux new-window -t w '...'` (its call has the words); the old thread's turn does not reach the child's start. With `lure=stale_turn` the thread
        is working again: it started the tmux server in its earlier turn, and the turn that is going on has only `ls` commands (all with their records)."""
        b, v, W = self.b, self.v, self.W
        again = v['lure'] == 'stale_turn'
        t1 = self.turn_of(top.tid)
        if again:
            top.shell(self.at(STALE_AT + 10), 'tmux new-session -d -s w', W, toolu(b, 'server'), t1, end=self.at(STALE_AT + 10.4), out_at=self.at(STALE_AT + 10.45))
        top.complete(self.at(STALE_AT + 100), t1, 'Done.', started=self.at(STALE_AT))
        if again:
            t2 = 'turn-' + digest(self.cid, 'turn2', n=16)
            top.task_started(self.at(-300), t2)
            top.turn_context(self.at(-299.9), t2)
            top.user(self.at(-299.8), 'Continue with the review.', t2)
            for i, off in enumerate((-200.0, -100.0)):
                top.shell(self.at(off), 'ls', W, toolu(b, 'ls%d' % i), t2, end=self.at(off + 0.3), out_at=self.at(off + 0.35))
        tc, t0c = self.at(0), self.at(CHILD_START)
        C = b.transcript('orchc', W, 'cli')
        C.prompt(b.T(-400), 'Another orchestrator at work in this folder.', source='human')
        C.bash(tc, "cd %s && tmux new-window -t w 'claude -p --model %s \"%s\"'" % (W, MODEL, text), toolu(b, 'own-call'), end=tc + 0.3, desc='launch child')
        self.trs.append(C)
        self.claude_run('child', W, text, t0c, False)
        base = [self.codex_proc(top), proc(110, 1, ['claude'], env={}, session=session_file(b, 110, C.sid, W, 'cli', b.T(-400)))]
        alive = base + [proc(302, 1, ['claude', '-p', '--model', MODEL], env=child_env('codex', top.tid, top.tid), session=session_file(b, 302, b.ids['child'], W, 'sdk-cli', t0c))]
        self.finish(top.path, alive, base, t0c + 20, t0c + 300)

    def build_pin_unknown(self, top, text):
        """The child has the names of a Codex thread the index does not have (`CODEX_THREAD_ID` = `CODEX_SESSION_ID` = a thread with no rollout) and nothing above it; a Claude call
        with the same words is running (with a child of its own). The Codex thread of the page is quiet. The Claude link of the child cannot be certain: the child may be the unknown
        thread's."""
        b, W = self.b, self.W
        top.complete(self.at(-30), self.turn_of(top.tid), 'Idle.', started=self.at(self.top_at))
        ghost = tid_of(self.cid, 'ghost')
        C = b.transcript('orchc', W, 'cli')
        C.prompt(b.T(-400), 'Another orchestrator at work in this folder.', source='human')
        C.bash(self.at(0.5), 'cd %s && claude -p --model %s "%s"' % (W, MODEL, text), toolu(b, 'gap-call'), end=None, desc='launch child')
        self.trs.append(C)
        self.claude_run('child', W, text, self.at(CHILD_START), False)
        self.claude_run('twin', W, text, self.at(CHILD_START + 0.3), False)
        base = [self.codex_proc(top), proc(110, 1, ['claude'], env={}, session=session_file(b, 110, C.sid, W, 'cli', b.T(-400)))]
        alive = base + [proc(302, 1, ['claude', '-p', '--model', MODEL], env=child_env('codex', ghost, ghost),
                             session=session_file(b, 302, b.ids['child'], W, 'sdk-cli', self.at(CHILD_START))),
                        proc(111, 110, ['/bin/bash', '-c', 'eval "claude -p"']),
                        proc(112, 111, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': C.sid, 'CLAUDE_PID': '110'},
                             session=session_file(b, 112, b.ids['twin'], W, 'sdk-cli', self.at(CHILD_START + 0.3)))]
        self.finish(top.path, alive, base, self.at(20), self.at(300))

    def build_pin_stale_claude(self, top, text):
        """The page's Codex thread starts the child with `tmux new-window -t w '...'` (its command record shows it at once). The tmux server was started from the shell of a Claude
        session P, long ago: the names of P (`CLAUDE_CODE_SESSION_ID`, and the number of its process, which is alive) are in the child's environment. P has no call running."""
        b, W = self.b, self.W
        tc, t0c = self.at(0), self.at(CHILD_START)
        P = b.transcript('orchc', W, 'cli')
        P.prompt(b.T(-7300), 'Set up a window for the helpers.', source='human')
        P.bash(b.T(-7200), 'tmux new-session -d -s w', toolu(b, 'p-server'), end=b.T(-7199.6), desc='start a tmux server')
        P.say(b.T(-7190), 'The window is ready.', 'end_turn')
        self.trs.append(P)
        cmd = "cd %s && tmux new-window -t w 'claude -p --model %s \"%s\"'" % (W, MODEL, text)
        top.shell(tc, cmd, W, toolu(b, 'launch'), self.turn_of(top.tid), end=tc + 0.3, out_at=tc + 0.35)
        self.claude_run('child', W, text, t0c, False)
        base = [self.codex_proc(top), proc(140, 1, ['claude'], env={}, session=session_file(b, 140, P.sid, W, 'cli', b.T(-7300)))]
        alive = base + [proc(302, 1, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': P.sid, 'CLAUDE_PID': '140'},
                             session=session_file(b, 302, b.ids['child'], W, 'sdk-cli', t0c))]
        self.finish(top.path, alive, base, t0c + 20, t0c + 300)

    # ---- a chain ----
    def build_clcxcl(self):
        """Claude (the top) starts `codex exec` (M), whose shell starts `claude -p` (the subject, K). Both providers' names may be in K's environment."""
        b, v, cid, W = self.b, self.v, self.cid, self.W
        O = b.transcript('top', W, 'cli')
        O.prompt(b.T(-3000), 'Start the work and keep me posted.', source='human')
        self.trs.append(O)
        m_text = b.ids['mid_text'] = prose(cid, 'mid-task', 160)
        k_text = b.ids['child_text'] = prose(cid, 'child', 200)
        finished = v['look'] == 'ended'
        fg = v['how'] == 'fg'
        t_m, tc, t0c = self.at(-100), self.at(0), self.at(CHILD_START)       # the Claude call that starts M, M's call that starts K, K's first record
        k_end = (t0c + CLAUDE_RUN) if finished else None
        m_end = (k_end + 1.0) if (fg and finished) else (tc + 3.0 if not fg else None)
        M = self.root('mid', 'codex_exec', t_m + 2.4, m_text)
        mt = self.turn_of(M.tid)
        k_cmd = self.launch_cmd('cl', k_text, v['how'])
        self.call(M, tc, k_cmd, 'launch-k', k_end if fg else None, v['how'], v['rec'])
        if m_end is not None:
            M.say(m_end - 0.5, 'Done.', mt, final=True)
            M.complete(m_end, mt, 'Done.', started=t_m + 2.4)
        if v['edge'] == 'guess':
            # the Claude call reads the words from a file no record shows being written: only the time and the folder tie the run to the session
            f = os.path.join(b.scratch, 'm_prompt.txt')
            self.files[f] = m_text
            m_cmd = 'cd %s && codex exec -m %s "$(cat %s)"' % (W, CX_MODEL, f)
        else:
            m_cmd = 'cd %s && codex exec -m %s "%s"' % (W, CX_MODEL, m_text)
        O.bash(t_m, m_cmd, toolu(b, 'launch-m'), end=(m_end + 0.5) if m_end is not None else None, desc='launch codex')
        self.claude_run('child', W, k_text, t0c, finished)
        base = [proc(100, 1, ['claude'], env={}, session=session_file(b, 100, O.sid, W, 'cli', b.T(-3000)))]
        alive = list(base)
        if v['look'] == 'live':
            if fg:                                                   # M is still waiting for its call
                alive += [proc(101, 100, ['/bin/bash', '-c', 'eval "codex exec"']),
                          proc(103, 101, ['codex', 'exec', '-m', CX_MODEL, m_text], env={'CLAUDE_CODE_SESSION_ID': O.sid, 'CLAUDE_PID': '100'}, fds=[M.path]),
                          proc(104, 103, ['/bin/bash', '-lc', k_cmd])]
            env = child_env(v['env'], M.tid, M.tid, claude=(O.sid, 100))
            alive.append(proc(105, 104 if fg else 1, ['claude', '-p', '--model', MODEL], env=env, session=session_file(b, 105, b.ids['child'], W, 'sdk-cli', t0c)))
        self.finish(O.path, alive, base, t0c + 20, t0c + 300)

    def build_cxclsub(self):
        """Codex (the top, or a native sub-agent of it) starts `claude -p` (K); a Claude sub-agent (the subject, S) works inside K."""
        b, v, cid, W = self.b, self.v, self.cid, self.W
        top = self.top_thread()
        L = self.host_thread(top)
        k_text = b.ids['mid_text'] = prose(cid, 'mid-task', 200)
        finished = v['look'] == 'ended'
        tc, t0c = self.at(0), self.at(CHILD_START)
        k_end = (t0c + CLAUDE_RUN) if finished else None
        k_cmd = self.launch_cmd('cl', k_text, 'fg')
        self.call(L, tc, k_cmd, 'launch-k', k_end, 'fg')
        K = self.claude_run('mid', W, k_text, t0c, finished, sub='child')
        self.end_host(top, L, finished, 15)
        base = [self.codex_proc(top)]
        alive = list(base)
        if v['look'] == 'live':
            alive += [proc(301, 300, ['/bin/bash', '-lc', k_cmd]),
                      proc(302, 301, ['claude', '-p', '--model', MODEL], env=child_env('codex', L.tid, top.tid), session=session_file(b, 302, K.sid, W, 'sdk-cli', t0c))]
        self.finish(top.path, alive, base, t0c + 20, t0c + 300)

    def build_cxcx(self):
        """Codex (the top, or a native sub-agent of it) starts `codex exec` (M), whose shell starts `codex exec` (the subject)."""
        b, v, cid, W = self.b, self.v, self.cid, self.W
        top = self.top_thread()
        L = self.host_thread(top)
        m_text = b.ids['mid_text'] = prose(cid, 'mid-task', 160)
        x_text = b.ids['child_text'] = prose(cid, 'child', 200)
        finished = v['look'] == 'ended'
        fg = v['how'] == 'fg'
        t_m, tc, t0c = self.at(-20), self.at(0), self.at(CHILD_START)
        x_end = (t0c + CODEX_RUN) if finished else None
        m_end = (x_end + 1.0) if (fg and finished) else (tc + 3.0 if not fg else None)
        M = self.root('mid', 'codex_exec', t_m + 2.4, m_text)
        mt = self.turn_of(M.tid)
        x_cmd = self.launch_cmd('cx', x_text, v['how'])
        self.call(M, tc, x_cmd, 'launch-x', x_end if fg else None, v['how'], v['rec'])
        if m_end is not None:
            M.say(m_end - 0.5, 'Done.', mt, final=True)
            M.complete(m_end, mt, 'Done.', started=t_m + 2.4)
        m_cmd = 'cd %s && codex exec -m %s "%s"' % (W, CX_MODEL, m_text)
        self.call(L, t_m, m_cmd, 'launch-m', m_end, 'fg')           # the first call: a plain foreground one; it ends when M ends (no record while M is still running)
        self.codex_run('child', t0c, x_text, finished)
        self.end_host(top, L, finished, 25)
        base = [self.codex_proc(top)]
        alive = list(base)
        if v['look'] == 'live':
            if fg:
                alive += [proc(301, 300, ['/bin/bash', '-lc', m_cmd]), proc(303, 301, ['codex', 'exec', '-m', CX_MODEL, m_text], env=child_env('codex', L.tid, top.tid), fds=[M.path]),
                          proc(304, 303, ['/bin/bash', '-lc', x_cmd])]
            alive.append(proc(305, 304 if fg else 1, ['codex', 'exec', '-m', CX_MODEL, x_text], env=child_env(v['env'], M.tid, M.tid), fds=[b.paths['child']]))
        self.finish(top.path, alive, base, t0c + 20, t0c + 300)

    # ---- a native sub-agent ----
    def build_sub(self):
        """The top's rollout and a native sub-agent below it (or two, one below the other): the front part the sub-agent inherits, its own turn, how it ends. In the parent's
        rollout a sub-agent's end is `completed` and then its message; a report in the middle of the work is a message too."""
        b, v, cid, W = self.b, self.v, self.cid, self.W
        top = self.top_thread()
        tt = self.turn_of(top.tid)
        on_sub = v['host'] == 'sub'
        S1 = self.sub('host' if on_sub else 'child', top, self.at(SUB_AT), 's1', 'Atlas', 1, self.root_text, self.at(TOP_AT))
        s1t = self.turn_of(S1.tid)
        subject, parent, pt = S1, top, tt
        if on_sub:
            subject = self.sub('child', S1, self.at(SUB2_AT), 'ss', 'Birch', 2, self.root_text, self.at(SUB_AT + 0.07))
            parent, pt = S1, s1t
        b.ids['notice'] = b.ids['notice_child']
        st = self.turn_of(subject.tid)
        sc = v['substate']
        base_t = SUB2_AT if on_sub else SUB_AT
        tool_t = self.at(base_t + 5)
        subject.shell(tool_t, 'cd %s && ls' % W, W, toolu(b, 'sub-ls'), st, end=tool_t + 0.2, out_at=tool_t + 0.25)
        parent.wait_agent(self.at(base_t + 1), self.at(base_t + 4), toolu(b, 'wait1'), pt, [subject.tid])
        mid_text = 'Progress: half of the notes are checked.'
        subject.agent_message(self.at(SUB_MID), subject.agent_path, parent.agent_path, mid_text, msg_id='msg-mid-' + digest(cid, 'mid', n=12), turn=pt)
        parent.agent_message(self.at(SUB_MID + 0.01), subject.agent_path, parent.agent_path, mid_text, msg_id='msg-mid-' + digest(cid, 'mid', n=12), turn=pt)
        if sc in ('done', 'int_after'):
            last = 'Result of the work: all is in order.'
            mid = 'msg-end-' + digest(cid, 'end', n=12)
            subject.agent_message(self.at(SUB_END - 0.1), subject.agent_path, parent.agent_path, last, msg_id=mid, turn=pt)
            subject.say(self.at(SUB_END - 0.05), last, st, final=True)
            subject.complete(self.at(SUB_END), st, last)
            parent.sub_completed(self.at(SUB_END + 0.005), pt, st, subject.tid, subject.agent_path)       # `completed` first, the message after it (as the records order them)
            parent.agent_message(self.at(SUB_END + 0.01), subject.agent_path, parent.agent_path, last, msg_id=mid, turn=pt)
        if sc == 'int_after':
            parent.interrupt(self.at(SUB_INT_AFTER), toolu(b, 'interrupt'), pt, subject.tid, subject.agent_path)
        elif sc == 'int_mid':
            parent.interrupt(self.at(SUB_INT_MID), toolu(b, 'interrupt'), pt, subject.tid, subject.agent_path)
            subject.aborted(self.at(SUB_INT_MID + 0.3), st)
        if on_sub and sc in ('done', 'int_after', 'int_mid'):
            # the sub-agent below the top's own one is over: that one hands its answer back and ends
            back = 'The helper is done.'
            mid = 'msg-s1-' + digest(cid, 's1', n=12)
            S1.agent_message(self.at(55.5), S1.agent_path, top.agent_path, back, msg_id=mid, turn=tt)
            S1.say(self.at(55.6), back, s1t, final=True)
            S1.complete(self.at(56), s1t, back)
            top.sub_completed(self.at(56.005), tt, s1t, S1.tid, S1.agent_path)
            top.agent_message(self.at(56.01), S1.agent_path, top.agent_path, back, msg_id=mid, turn=tt)
        top.say(self.at(70), 'Collecting the results.', tt)
        self.finish(top.path, [self.codex_proc(top)], [], self.at(80), self.at(600))
