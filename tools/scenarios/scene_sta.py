"""The state scene: one agent (a `claude -p` child, a sub-agent, a grand-child sub-agent, a Codex thread, or the orchestrator itself) lives one life:
running, finished, stopped by a usage limit, an API error, the Bash time limit, a kill, a crash ... and the records, notifications and processes that life leaves.

The subject builders (cli_subject, sub_subject, codex_subject) are shared with the debate scene: a debate participant has a life too.
"""

import os

from .axes import FLAW_VERSION, RESUMABLE, digest
from .build import FUTURE_VERSION, MODEL, NOTE_TEXT, OLD_VERSION, VERSION, Phase, Transcript, agent_id_of, dump, iso, proc, prose, put, session_file
from .scene_aff import bgid, toolu, codex_rollout

ERR = {                             # life -> (http status, error kind, limit type, seconds until resets_at or None)
    'limit_exit': (429, 'rate_limit', 'five_hour', 7200), 'sub_limit_resume': (429, 'rate_limit', 'five_hour', 7200),
    'sub_limit_dead': (429, 'rate_limit', 'five_hour', 1200), 'weekly': (429, 'rate_limit', 'seven_day', 4 * 86400),
    'no_reset': (429, 'rate_limit', None, None), 'api_529': (529, 'server_error', None, None),     # the names the real records carry
    'api_400': (400, 'invalid_request', None, None),
}
LIMIT_LIVES = ('limit_exit', 'sub_limit_resume', 'sub_limit_dead', 'weekly', 'no_reset')
BG_TIME_LIMIT_MS = 1800062
COORD = 'The coordinator sent a message while you were working:'
RESUME_TEXT = 'Continue the review where you stopped.'      # what a resumed `claude -p` run is told: the resume call carries exactly this and the run's first line says it
CODEX_RESUME_TEXT = 'Continue.'
FIELD_GONE_VERSION = FLAW_VERSION['field_gone']


class Sta:
    """Builds the lives of subjects into the Built `b`. Holds the orchestrator record and the processes of the final observation."""

    def __init__(self, b, W=None, flaw='none', os_kind='linux', entry='cli', start=True):
        """`entry`: the entrypoint of the orchestrator's record (`cli`, or `sdk-cli` for the record of a `claude -p` run); `start`: whether the record opens with a prompt."""
        self.b, self.cid = b, b.case.id
        self.W = W or os.path.join(b.work, 'repo')
        os.makedirs(self.W, exist_ok=True)
        self.flaw = flaw
        sdk = entry == 'sdk-cli'
        self.O = b.transcript('orch', self.W, entry)
        if start:
            self.O.prompt(b.T(-3000), 'Start the work and keep me posted.', source='sdk' if sdk else 'human')
        self.procs = [proc(100, 1, ['claude', '-p', '--model', MODEL] if sdk else ['claude', '--dangerously-skip-permissions'], env={},
                           session=session_file(b, 100, b.sid('orch'), self.W, entry, b.T(-3000), status=None if sdk else 'idle'))]
        self.early = None                       # the process list of an earlier look (restart scenes)
        self.now = b.T(30)
        self.subs = []
        self.extra_tr = []
        self.tear = []                          # (transcript, which line to tear)
        self.resets_at = None
        self.live_child = False
        self.extra_proc = None

    # ------------------------------------------------------------------------------------------------ helpers
    def reset_time(self, life):
        secs = ERR[life][3] if life in ERR else None
        return None if secs is None else self.b.T(secs)

    def err(self, tr, t, life):
        status, kind, ltype, secs = ERR[life]
        r = self.reset_time(life)
        tr.api_error(t, status=status, kind=kind, resets_at=r, limit_type=ltype or 'five_hour',
                     text='Usage limit reached' if status == 429 else 'API error %d' % status)
        if ltype is None and status == 429:
            tr.rows[-1][2].pop('quotaLimits', None)       # a limit with no reset time in the record
        return r

    def prompt(self, tr, t, text, legacy=False):
        """A run start of a `claude -p` child: the turn index keeps counting across runs; a legacy record has no source markers at all."""
        tr.prompt(t, text, source='legacy' if legacy else 'sdk')

    def version(self):
        """The Claude Code version the subject's records carry: the observed one, an older one (the legitimate old format) or a newer one (a version outside the observed
        range is no drift by itself), or one inside the range whose `cost-state` lost a known field (that is `format_drift`)."""
        return {'old_format': OLD_VERSION, 'future_version': FUTURE_VERSION, 'field_gone': FIELD_GONE_VERSION}.get(self.flaw, VERSION)

    def finish_flaws(self, tr, life, legacy):
        v = self.flaw
        if v == 'unknown_type':
            tr.raw(self.b.T(1), {'type': 'mystery-event', 'subtype': 'unseen-kind', 'sessionId': tr.sid, 'timestamp': iso(self.b.T(1))})
        if v == 'torn':
            self.tear.append(tr)

    # ------------------------------------------------------------------------------------------------ claude -p child
    def cli_subject(self, role, text, cmd, life, at, cwd, legacy=False, tail=None, launcher=None, tree=None, final=None, off=0.0, msg=None, env_extra=None, shared=None):
        """`launcher`: the record that holds the launching call (default: the main session); `tree`: (session id, pid, shell pid) of the Claude process above it; `final`: the last
        message of the run when it ends well (what a `>` redirect of the launch writes into its file); `off`: when the launching call is made (seconds from the case's base time);
        `msg`: the id of the assistant message the launching call is in (None: its own, False: none); `env_extra`: more names in the environment of the process (a tag); `shared`: {tid, bgi, emit}, the launching call that
        also starts another run (one Bash call, two children): only the run with `emit` writes the call, its result and the notice of its end."""
        b, O = self.b, launcher or self.O
        tree_sid, tree_pid, shell_pid = tree or (b.sid('orch'), 100, 101)
        G = b.sid(role)
        tid, bgi = (shared['tid'], shared['bgi']) if shared else (toolu(b, 'launch-' + role), bgid(b, 'bg-' + role))
        emit = not shared or shared['emit']
        tc, t0 = b.T(off), b.T(off + 2.4)
        C = b.transcript(role, cwd, 'sdk-cli', sid=G)
        C.version = self.version()
        self.extra_tr.append(C)
        if emit:
            O.tool(tc, 'Bash', {'command': cmd, 'description': 'launch child', 'run_in_background': True}, tid, msg=msg)
            O.result(tc + 0.4, tid, 'started', bg=bgi)
        self.prompt(C, t0, text, legacy=legacy)
        C.tool(t0 + 3, 'Read', {'file_path': os.path.join(cwd, 'notes.md')}, toolu(b, 'rd-' + role))
        C.result(t0 + 4, toolu(b, 'rd-' + role), 'notes')
        if tail:
            tail(C, t0 + 4.2)
        t_end = t0 + 8
        notif = None                                    # (time, status, summary)
        r = None
        alive = False
        if life == 'running':
            C.tool(t0 + 4.5, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'wk-' + role))
            alive = True
            self.now = t0 + 30
        elif life == 'stalled_silent':
            alive = True                                  # the process is there, the record has not grown for 20 minutes and no tool call is open
            self.now = t0 + 4 + 1200
        elif life == 'normal_end':
            C.say(t_end, final or 'Finished the review.', 'end_turn')
            notif = (t_end + 1.5, 'completed', 'Background command "launch child" completed (exit code 0)')
        elif life in ERR:
            r = self.err(C, t_end, life)
            notif = (t_end + 1.5, 'failed', 'Background command "launch child" failed with exit code 1')
        elif life in ('time_limit_kill', 'time_limit_silent'):
            C.tool(t0 + 4.5, 'Bash', {'command': 'make test', 'description': 'run the long job'}, toolu(b, 'wk-' + role))
            t_end = t0 + 1802
            if life == 'time_limit_kill':
                notif = (t_end + 1, 'killed', 'Background command "launch child" was stopped after reaching its background time limit')
        elif life in ('taskstop_kill', 'kill_resume'):
            C.tool(t0 + 4.5, 'Bash', {'command': 'make test', 'description': 'run the long job'}, toolu(b, 'wk-' + role))
            t_end = t0 + 60
            stop = toolu(b, 'stop-' + role)
            O.tool(t_end - 1, 'TaskStop', {'task_id': bgi}, stop)
            O.result(t_end - 0.5, stop, 'stopped')
            notif = (t_end + 1, 'killed', 'Background command "launch child" was stopped')
        elif life == 'crash':
            C.tool(t0 + 4.5, 'Bash', {'command': 'make test', 'description': 'run the long job'}, toolu(b, 'wk-' + role))
            notif = (t_end + 1.5, 'failed', 'Background command "launch child" failed with exit code 137')
        if life not in ('running', 'stalled_silent') and life != 'crash' and not legacy:
            C.cost_state(t_end + 1, BG_TIME_LIMIT_MS if life.startswith('time_limit') else 9000, drop=('totalDuration',) if self.flaw == 'field_gone' else ())
        if life not in ('running', 'stalled_silent'):
            self.now = t_end + 120
            if life == 'sub_limit_dead':
                self.now = r + 3600 if r else self.now
        if notif and life != 'time_limit_silent' and emit:
            O.notification(notif[0], bgi, tid, notif[1], notif[2])
        self.resets_at = r
        if at in ('after_resume', 'resume_stopped') and life in RESUMABLE and life not in ('running', 'stalled_silent'):
            t_res = t_end + 300
            tid2, bg2 = toolu(b, 'resume-' + role), bgid(b, 'bg2-' + role)
            O.tool(t_res, 'Bash', {'command': cmd.replace('claude -p', 'claude -p --resume %s' % G, 1).replace(text, RESUME_TEXT), 'description': 'resume child',
                                   'run_in_background': True}, tid2)
            O.result(t_res + 0.4, tid2, 'started', bg=bg2)
            self.prompt(C, t_res + 2.4, RESUME_TEXT, legacy=legacy)
            C.tool(t_res + 6, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'wk2-' + role))
            alive = True
            self.now = t_res + 30
            if at == 'resume_stopped':
                # the main session stops the resumed run through its own background call: whatever ended the first run says nothing about this one
                stop2 = toolu(b, 'stop2-' + role)
                O.tool(t_res + 40, 'TaskStop', {'task_id': bg2}, stop2)
                O.result(t_res + 40.5, stop2, 'stopped')
                O.notification(t_res + 41, bg2, tid2, 'killed', 'Background command "resume child" was stopped')
                alive = False
                self.now = t_res + 120
        self.finish_flaws(C, life, legacy)
        child = proc(103, shell_pid, ['claude', '-p', '--model', MODEL], env=dict({'CLAUDE_CODE_SESSION_ID': tree_sid, 'CLAUDE_PID': str(tree_pid)}, **(env_extra or {})),
                     session=session_file(b, 103, G, cwd, 'sdk-cli', t0))
        shell = proc(shell_pid, tree_pid, ['/bin/bash', '-c', 'eval run'])
        if self.flaw == 'multi_proc':
            self.extra_proc = proc(104, shell_pid, ['claude', '-p', '--model', MODEL, '--resume'], env={'CLAUDE_CODE_SESSION_ID': tree_sid, 'CLAUDE_PID': str(tree_pid)},
                                   session=session_file(b, 104, G, cwd, 'sdk-cli', t0 + 1))
        early = list(self.procs) + [shell, child]
        self.early = early
        if alive:
            self.procs += [shell, child] + ([self.extra_proc] if self.flaw == 'multi_proc' else [])
            self.live_child = True
        elif self.flaw == 'multi_proc':
            self.procs += [shell, child, self.extra_proc]          # both processes stay open although the run is over: the same session id twice
        if self.flaw == 'child_bg':
            bgc = bgid(b, 'child-own-bg')
            ctid = toolu(b, 'child-bg-call')
            C.tool(t0 + 5, 'Bash', {'command': 'make watch', 'description': 'watch', 'run_in_background': True}, ctid)
            C.result(t0 + 5.2, ctid, 'started', bg=bgc)
            body = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/claude-synth/tasks/%s.output</output-file>\n<status>completed</status>\n'
                    '<summary>Background command "watch" completed (exit code 0)</summary>\n<note>%s</note>\n</task-notification>') % (bgc, ctid, bgc, NOTE_TEXT)
            C.raw(t0 + 7, {'type': 'attachment', 'timestamp': iso(t0 + 7), 'sessionId': G, 'attachment': {
                'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': body, 'timestamp': iso(t0 + 7)}})
        b.meta['t_end'] = t_end
        return C

    # ------------------------------------------------------------------------------------------------ sub-agent
    def make_sub(self, role, desc, at_off, parent=None, depth=None, prompt='Work on the task.', msg=None):
        """A sub-agent: the Agent call (and its async-launched result) in its parent's record, the meta file (depth 1 or more), the record with its spawn line. `msg`: the id of the
        assistant message the Agent call is in (sub-agents launched in parallel share one; None: a message of its own, False: none)."""
        b, O = self.b, self.O
        aid = b.ids.get(role) or b.ids.setdefault(role, agent_id_of(self.cid, role))
        tu = toolu(b, 'spawn-' + role)
        host = O if parent is None else parent
        host.tool(b.T(at_off), 'Agent', {'description': desc, 'prompt': prompt}, tu, msg=msg)
        host.result(b.T(at_off + 0.3), tu, 'Async agent launched', extra={
            'isAsync': True, 'status': 'async_launched', 'agentId': aid, 'description': desc, 'prompt': prompt,
            'outputFile': '/tmp/claude-synth/tasks/%s.output' % aid, 'canReadOutputFile': True})
        d = os.path.join(os.path.dirname(O.path), b.sid('orch'), 'subagents')
        meta = {'agentType': 'general-purpose', 'description': desc, 'requestNonInteractive': True, 'requestShape': 'background',
                'spawnDepth': depth or 1, 'toolUseId': tu}
        if parent is not None:
            meta['parentAgentId'] = parent.agent
        put(os.path.join(d, 'agent-%s.meta.json' % aid), dump(meta))
        S = Transcript(os.path.join(d, 'agent-%s.jsonl' % aid), b.sid('orch'), self.W, 'cli', side=True, agent=aid, cid=self.cid)
        S.prompt(b.T(at_off + 1), prompt, source='spawn')
        b.paths[role] = S.path
        self.subs.append(S)
        return S, aid, tu

    def sub_subject(self, role, desc, prompt, life, at, parent=None, depth=None, tail=None, off=-200, msg=None):
        """`tail(S, t)` may add the subject's own work records (a debate participant writes its report there). `off`: when the Agent call is made, in seconds from the case's base time."""
        b, O = self.b, self.O
        S, aid, tu = self.make_sub(role, desc, off, parent=parent, depth=depth, prompt=prompt, msg=msg)
        S.version = self.version()
        t0 = b.T(off + 1)
        notes_to = parent if parent is not None else O
        S.tool(t0 + 3, 'Read', {'file_path': os.path.join(self.W, 'notes.md')}, toolu(b, 'rd-' + role))
        S.result(t0 + 4, toolu(b, 'rd-' + role), 'notes')
        t = t0 + 5
        if tail:
            t = tail(S, t)
        t_end = t + 4
        r = None
        if life == 'running':
            S.tool(t + 0.5, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'wk-' + role))
            self.now = b.T(0)
        elif life == 'stalled_silent':
            self.now = t + 1200
        elif life == 'normal_end':
            S.say(t_end, 'Report written.', 'end_turn')
            self.notify(notes_to, t_end + 1, aid, tu, 'completed', 'Agent "%s" finished' % desc)
        elif life in ERR:
            r = self.err(S, t_end, life)
            # the real notice names no error type and no HTTP status: the cause is only in the sub-agent's own record
            self.notify(notes_to, t_end + 1.5, aid, tu, 'failed', 'Agent "%s" failed: Agent terminated early due to an API error: the request was rejected' % desc)
        elif life in ('taskstop_kill', 'kill_resume'):
            S.tool(t + 0.5, 'Bash', {'command': 'make test', 'description': 'long'}, toolu(b, 'wk-' + role))
            stop = toolu(b, 'stop-' + role)
            notes_to.tool(t_end + 30, 'TaskStop', {'task_id': aid}, stop)
            notes_to.result(t_end + 30.5, stop, 'stopped')
        if life not in ('running', 'stalled_silent'):
            self.now = t_end + 120
            if life == 'sub_limit_dead' and r:
                self.now = r + 3600
        self.resets_at = r
        if at == 'after_resume' and life in RESUMABLE and life not in ('running', 'stalled_silent'):
            t_res = t_end + 300
            msg = toolu(b, 'msg-' + role)
            notes_to.tool(t_res, 'SendMessage', {'to': aid, 'summary': 'resume', 'message': 'Continue where you stopped.'}, msg)
            notes_to.result(t_res + 0.5, msg, 'sent')
            S.coordinator(t_res + 2, COORD + ' Continue where you stopped.')
            S.tool(t_res + 6, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'wk2-' + role))
            self.now = t_res + 30
        if self.flaw == 'torn':
            self.tear.append(S)
        if self.flaw == 'unknown_type':
            S.raw(b.T(1), {'type': 'mystery-event', 'subtype': 'unseen-kind', 'sessionId': S.sid, 'timestamp': iso(b.T(1))})
        return S

    def notify(self, host, t, aid, tu, status, summary):
        """A completion notice for a sub-agent: in the main record (child of the main session) or in the parent's record (grand-child)."""
        body = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/claude-synth/tasks/%s.output</output-file>\n'
                '<status>%s</status>\n<summary>%s</summary>\n<note>%s</note>\n</task-notification>') % (aid, tu, aid, status, summary, NOTE_TEXT)
        if host is self.O:
            host.prompt(t, body, source='task-notification')
        else:
            host.raw(t, {'type': 'attachment', 'timestamp': iso(t), 'sessionId': host.sid, 'attachment': {
                'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': body, 'timestamp': iso(t)}})

    # ------------------------------------------------------------------------------------------------ Codex thread
    def codex_subject(self, role, text, cmd, life, at, cwd, extra=None, final=None):
        """`final`: the last message of the run when it ends well (what `-o` of the launch writes into its file)."""
        b, O = self.b, self.O
        tid = b.ids[role] = '019a%04d-0000-7000-8000-%012d' % (int(digest(self.cid, role, 'cx', n=3), 16) % 10000, int(digest(self.cid, role, 'cx2', n=6), 16) % 10 ** 12)
        tc = b.T(0)
        ctid = toolu(b, 'cx-launch-' + role)
        O.tool(tc, 'Bash', {'command': cmd, 'description': 'launch codex', 'run_in_background': True}, ctid)
        bg = bgid(b, 'cx-bg-' + role)
        O.result(tc + 0.4, ctid, 'started', bg=bg)
        start = tc + 2.4
        complete = life in ('normal_end', 'codex_error')
        alive = life in ('running', 'stalled_silent')
        err = {'codex_error_info': 'server_overloaded', 'message': 'overloaded'} if life == 'codex_error' else None
        path = codex_rollout(b, tid, start, cwd, text, 'codex_exec', complete=complete, error=err, extra=extra, final=final)
        if life == 'taskstop_kill':
            with open(path, 'a') as f:
                f.write(dump({'timestamp': iso(start + 5), 'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'reason': 'interrupted'}}) + '\n')
            stop = toolu(b, 'cx-stop-' + role)
            O.tool(start + 6, 'TaskStop', {'task_id': bg}, stop)
            O.result(start + 6.5, stop, 'stopped')
        if complete:
            O.notification(start + 8.5, bg, ctid, 'completed' if life == 'normal_end' else 'failed',
                           'Background command "launch codex" %s' % ('completed (exit code 0)' if life == 'normal_end' else 'failed with exit code 1'))
        b.paths[role] = path
        shell = proc(101, 100, ['/bin/bash', '-c', 'eval run'])
        kid = proc(103, 101, ['codex', 'exec', '-m', 'gpt-6.1-sol', text], env={'CLAUDE_CODE_SESSION_ID': b.sid('orch'), 'CLAUDE_PID': '100'}, fds=[path])
        self.early = list(self.procs) + [shell, kid]
        if alive:
            self.procs += [shell, kid]
            self.now = start + (1200 if life == 'stalled_silent' else 30)           # a silent one: the process is there, the record has not grown for twenty minutes
        else:
            self.now = start + 200
        if at == 'after_resume' and life in RESUMABLE and life != 'running':
            t_res = start + 300                                  # `codex exec resume`: a new turn in the same rollout, the process is back
            rtid = toolu(b, 'cx-resume-' + role)
            O.tool(t_res, 'Bash', {'command': cmd.replace('codex exec', 'codex exec resume %s' % tid, 1).replace(text, CODEX_RESUME_TEXT), 'description': 'resume codex',
                                   'run_in_background': True}, rtid)
            O.result(t_res + 0.4, rtid, 'started', bg=bgid(b, 'cx-bg2-' + role))
            with open(path, 'a') as f:
                for dt, typ, payload in ((2.4, 'event_msg', {'type': 'task_started', 'model_context_window': 200000}),
                                         (2.5, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': CODEX_RESUME_TEXT}]})):
                    f.write(dump({'timestamp': iso(t_res + dt), 'type': typ, 'payload': payload}) + '\n')
            kid2 = proc(103, 101, ['codex', 'exec', 'resume', tid], env={'CLAUDE_CODE_SESSION_ID': b.sid('orch'), 'CLAUDE_PID': '100'}, fds=[path])
            self.procs += [shell, kid2]
            self.now = t_res + 30
        return path

    # ------------------------------------------------------------------------------------------------ the orchestrator itself
    def local_command(self, t, name):
        """The three lines a local slash command leaves in a typed session: the caveat (meta), the command, and what it printed. No turn starts and nobody answers."""
        O = self.O
        O.add(t, 'user', message={'role': 'user', 'content': '<local-command-caveat>Caveat: the messages below were generated by the user while running local commands. '
                                                                'Do not respond to them unless the user explicitly asks you to.</local-command-caveat>'}, isMeta=True)
        O.add(t + 0.05, 'user', message={'role': 'user', 'content': '<command-name>%s</command-name>\n<command-message>%s</command-message>\n<command-args></command-args>' % (name, name.lstrip('/'))})
        O.add(t + 0.1, 'user', message={'role': 'user', 'content': '<local-command-stdout>The panel is shown.</local-command-stdout>'})

    def session_end(self, entry, tail, process):
        """The orchestrator's record of a session that was not stopped by a limit, ending the way `tail` says: a turn in progress, a turn that ended (a typed session closes it
        with a turn-duration line, the record of a `claude -p` run with a stop-hook summary, the last prompt and the cost line), a prompt nobody has answered, local
        slash commands after the end, or nothing but local slash commands. `process`: whether the session's process is still there."""
        b, O = self.b, self.O
        sdk = entry == 'sdk'
        if tail == 'mid':
            O.tool(b.T(-5), 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'o-wk'))
        elif tail == 'commands_only':
            self.local_command(b.T(-60), '/usage')
            self.local_command(b.T(-30), '/status')
        else:
            O.tool(b.T(-40), 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'o-wk'))
            O.result(b.T(-39), toolu(b, 'o-wk'), 'files')
            O.say(b.T(-6), 'Which of the two designs do you want?' if tail == 'asked' else 'The work is done.', 'end_turn')
            if sdk and tail != 'next':
                O.add(b.T(-5.8), 'system', subtype='stop_hook_summary', hookCount=0, hookInfos=[], hookErrors=[], preventedContinuation=False, hasOutput=False, level='suggestion')
                O.raw(b.T(-5.7), {'type': 'last-prompt', 'lastPrompt': 'Start the work and keep me posted.', 'sessionId': O.sid})
                O.cost_state(b.T(-5.6), 9000)
            elif not sdk:
                O.turn_end(b.T(-5.5))
            if tail == 'prompt':
                O.prompt(b.T(-3), 'One more thing: check the result.', source='sdk' if sdk else 'human')
            elif tail == 'next':                                                          # the next instruction and its first long call, within a second of the end
                O.prompt(b.T(-5.3), 'And now the second part.', source='sdk' if sdk else 'human')
                O.tool(b.T(-5.1), 'Bash', {'command': 'sleep 600', 'description': 'wait'}, toolu(b, 'o-wk2'))
            elif tail == 'commands':
                self.local_command(b.T(-3), '/usage')
        self.now = b.T(20)
        turn_going = tail in ('mid', 'prompt', 'next')
        if (entry, tail, process) != ('cli', 'mid', 'there'):
            self.procs[0]['session']['status'] = 'busy' if turn_going else 'idle'      # what the session file of a live process says (a hint)
        if process == 'gone':
            self.procs = []                                                               # no process, no session file: the record is all there is

    def main_subject(self, life, at, entry='cli', tail='mid', process='there'):
        b, O = self.b, self.O
        W = self.W
        resets = self.reset_time('limit_exit')
        self.resets_at = resets
        if life == 'running':
            self.session_end(entry, tail, process)
            return
        O.prompt(b.T(-50), 'Continue with the plan and report.', source='human')
        O.tool(b.T(-40), 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'o-wk'))
        O.result(b.T(-39), toolu(b, 'o-wk'), 'files')
        # a sub-agent that hit the same limit (the real records show the orchestrator and its agents stop together)
        S, aid, tu = self.make_sub('child', 'Analysis helper', -30)
        S.tool(b.T(-20), 'Read', {'file_path': os.path.join(W, 'notes.md')}, toolu(b, 'o-rd'))
        S.result(b.T(-19), toolu(b, 'o-rd'), 'notes')
        self.err(S, b.T(-1), 'limit_exit')
        self.notify(O, b.T(-0.5), aid, tu, 'failed', 'Agent "Analysis helper" failed: Agent terminated early due to an API error: the request was rejected')
        self.err(O, b.T(0), 'limit_exit')
        if life in ('limit_auto', 'limit_repeat'):
            O.notice(b.T(0.1), 'Usage limit reached · continuing automatically at 05:30')
        O.turn_end(b.T(0.2))
        if life == 'limit_repeat':
            for i in (1, 2, 3):
                self.notify(O, b.T(30 * i), aid, tu, 'failed', 'Agent "Analysis helper" failed: Agent terminated early due to an API error: the request was rejected (%d)' % i)
                self.err(O, b.T(30 * i + 1), 'limit_exit')
                O.turn_end(b.T(30 * i + 1.2))
        self.now = b.T(200)
        if at == 'after_resume':
            t = resets + 5
            O.notice(t, 'Usage limit reset')
            O.add(t + 1, 'user', message={'role': 'user', 'content': 'Continue.'}, isMeta=True, origin={'kind': 'auto-continuation'}, promptSource='system')
            O.tool(t + 3, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'o-wk2'))
            self.now = t + 20
        b.ids['sub'] = aid

    # ------------------------------------------------------------------------------------------------ closing
    def finish(self):
        """Saves every record, tears the lines the flaw asks for, and sets the observation phases."""
        for tr in [self.O] + self.subs + self.extra_tr:
            tr.save()
        for tr in self.tear:
            tear_line(tr)
        b = self.b
        if b.case.v.get('at') == 'after_restart':
            b.phases = [Phase(b.T(30), self.early or self.procs, 0), Phase(self.now, self.procs, 1)]
        else:
            b.phases = [Phase(self.now, self.procs, 0)]
        b.main_path = self.O.path


def tear_line(tr):
    """A record torn in half with the next record glued behind it on the same line (the real records show this)."""
    with open(tr.path) as f:
        lines = f.read().split('\n')
    lines = [x for x in lines if x]
    idx = next((i for i in range(len(lines) - 1, 0, -1) if '"stop_reason":"end_turn"' in lines[i] or '"isApiErrorMessage":true' in lines[i]), None)
    if idx is None:
        return
    prev = lines[idx - 1]
    lines[idx - 1] = prev[:len(prev) // 2] + lines[idx]
    del lines[idx]
    with open(tr.path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def build_sta(b):
    """The state bundle: one subject of kind `skind` living the life `life`."""
    v, cid = b.case.v, b.case.id
    sk, life, at = v['skind'], v['life'], v['at']
    S = Sta(b, flaw=v['flaw'], entry='sdk-cli' if v['entry'] == 'sdk' else 'cli', start=v['tail'] != 'commands_only')
    W = S.W
    legacy = v['flaw'] == 'old_format'
    text = prose(cid, 'child', 200)
    if sk == 'cli':
        S.cli_subject('child', text, 'cd %s && claude -p --model %s "%s"' % (W, MODEL, text), life, at, W, legacy=legacy)
    elif sk == 'sub':
        S.sub_subject('child', 'Analysis helper', 'Work on the analysis.', life, at)
    elif sk == 'grandsub':
        lead, _, _ = S.make_sub('parent', 'Team lead', -300)
        lead.tool(b.T(-250), 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'lead-wk'))
        S.sub_subject('child', 'Analysis helper', 'Work on the analysis.', life, at, parent=lead, depth=2)
    elif sk == 'codex':
        S.codex_subject('child', text, 'cd %s && codex exec -m gpt-6.1-sol "%s"' % (W, text), life, at, W)
    else:
        S.main_subject(life, at, v['entry'], v['tail'], v['process'])
    S.finish()
    return S
