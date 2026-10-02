"""The affiliation scene: a launcher (main session, sub-agent, or a `claude -p` child) launches a child session (or a Codex thread) in one of 11 ways.

The scene writes down only what really exists on disk and in the process table: the launching call and its prior calls, the output files, the child's
records, the process lineage and environment of each observation phase. What a reader may conclude from that is the oracle's business.
"""

import os

from .axes import WAY, digest
from .build import MODEL, Phase, Transcript, agent_id_of, dump, iso, proc, prose, put, session_file

PRIOR_AT = -1500                                   # seconds before the launching call: the calls that prepare the instruction
RESUME_TEXT = 'Continue the review where you stopped.'      # what a resumed run is told (the call and the run's first line say exactly this)
BLOCKS = ('direct', 'timeout', 'envi', 'later', 'script', 'loop', 'xargs', 'pysub')      # the Bash call stays open until the child ends
OTHER_UID_DELTA = 1


def toolu(b, tag):
    return 'toolu_' + digest(b.case.id, tag, n=20)


def bgid(b, tag):
    return 'b' + digest(b.case.id, tag, n=8)


def split_text(text):
    """Two pieces of an instruction, the first ending with a space (a common part and a role part)."""
    cut = text.rfind(' ', 0, int(len(text) * 0.55)) + 1
    return text[:cut], text[cut:]


def out_json(sid):
    return dump({'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': sid, 'result': 'ok', 'total_cost_usd': 0.0})


class Launch:
    """One launching call: its command, the records that prepare it, the files it needs, and how the call behaves."""

    def __init__(self, cmd, prior, files, mode='fg'):
        self.cmd, self.prior, self.files, self.mode = cmd, prior, files, mode


class Aff:
    """Builds one affiliation case into the Built `b`."""

    def __init__(self, b):
        self.b, self.case, self.v, self.cid = b, b.case, b.case.v, b.case.id
        self.W = os.path.join(b.work, 'repo')
        os.makedirs(self.W, exist_ok=True)
        self.files = {}                                       # path -> text: scripts, instruction files, output files (written at the end)
        v = self.v
        self.cwd_launch, self.child_cwd = None, self.W        # the folder the launch text cds to, and the folder the child records
        self.author_cwd = os.path.join(b.work, 'author')       # where a peer session that wrote the instruction works
        if v['cwd'] != 'same':
            d = {'cd': os.path.join(self.W, 'sub'), 'other': os.path.join(b.work, 'other'), 'pushd': os.path.join(self.W, 'sub'), 'var': os.path.join(b.work, 'wt'),
                 'worktree': os.path.join(self.W, '.claude', 'worktrees', 'wt1'), 'symlink': os.path.join(b.work, 'real')}[v['cwd']]
            os.makedirs(d, exist_ok=True)
            self.cwd_launch = self.child_cwd = d
            if v['cwd'] == 'symlink':
                link = os.path.join(b.work, 'link')
                if not os.path.lexists(link):
                    os.symlink(d, link)
                self.cwd_launch = link
            if v['cwd'] == 'var' and v['bait'] == 'foreign':
                self.child_cwd = os.path.join(b.work, 'elsewhere')      # a stranger started by a person in another project: nothing in the call tells where
                os.makedirs(self.child_cwd, exist_ok=True)
        self.text = '/init' if v['decoy'] == 'short' else prose(self.cid, 'child', 200)
        self.out_path = os.path.join(b.scratch, 'out.json')
        self.G = b.sid('child')
        self.tc = 0.0                                         # offset of the launching call
        self.t0c = 2.4                                        # offset of the child's first record
        self.call_end = None
        self.A = None                                         # the transcript of the session that wrote the instruction file (when it is not the launcher)
        self.peer = None                                      # the transcript of a peer session that is the real launcher (out=overwritten)
        self.obs_shift = 0.0                                  # how much later than the child's start the board looks
        self.file_times = {}                                  # path -> modification time of a file the scene writes

    # ------------------------------------------------------------------------------------------------ who launches
    def make_launcher(self):
        b, v, cid, W = self.b, self.v, self.cid, self.W
        self.O = O = b.transcript('orch', W, 'cli')
        O.prompt(b.T(-3000), 'Start the work and keep me posted.', source='human')
        sp = v['spawner'] if v['bait'] in ('none', 'foreign') else 'main'
        self.tree_sid, self.tree_pid, self.node = b.sid('orch'), 100, None
        if sp == 'main':
            self.L = O
        elif sp == 'sub':
            self.node = b.ids['sub'] = agent_id_of(cid, 'sub')
            self.L = self.make_sub('sub', 'Analysis helper', -400)
        else:                                                 # grand: a `claude -p` child K launches the target; K is the tree
            K = self.K = b.transcript('mid', W, 'sdk-cli')
            ktext = prose(cid, 'mid', 160)
            O.bash(b.T(-100), 'cd %s && claude -p --model %s "%s"' % (W, MODEL, ktext), toolu(b, 'mid-call'), end=b.T(1500), desc='launch mid')
            K.prompt(b.T(-98), ktext, source='sdk')
            self.tree_sid, self.tree_pid, self.L = b.sid('mid'), 110, K

    def make_sub(self, role, desc, at):
        """A sub-agent of the main session: the Agent call and its result, its meta file and its record. Returns its Transcript."""
        b, O = self.b, self.O
        aid = b.ids.get(role) or b.ids.setdefault(role, agent_id_of(self.cid, role))
        tu = toolu(b, 'spawn-' + role)
        task = 'Work on the task and launch what is needed.'
        O.tool(b.T(at), 'Agent', {'description': desc, 'prompt': task}, tu)
        O.result(b.T(at + 0.3), tu, 'Async agent launched', extra={
            'isAsync': True, 'status': 'async_launched', 'agentId': aid, 'description': desc, 'prompt': task,
            'outputFile': '/tmp/claude-synth/tasks/%s.output' % aid, 'canReadOutputFile': True})
        d = os.path.join(os.path.dirname(O.path), b.sid('orch'), 'subagents')
        put(os.path.join(d, 'agent-%s.meta.json' % aid), dump({'agentType': 'general-purpose', 'description': desc, 'requestNonInteractive': True,
                                                              'requestShape': 'background', 'spawnDepth': 1, 'toolUseId': tu}))
        S = Transcript(os.path.join(d, 'agent-%s.jsonl' % aid), b.sid('orch'), self.W, 'cli', side=True, agent=aid, cid=self.cid)
        S.prompt(b.T(at + 1), task, source='spawn')
        b.paths[role] = S.path
        self.subs = getattr(self, 'subs', []) + [S]
        return S

    # ------------------------------------------------------------------------------------------------ command text
    def cd_prefix(self):
        """How the launching command goes to the folder: `cd X &&`, `pushd X &&`, or `cd "$WT" &&` with a variable nothing in the command defines."""
        if self.v['cwd'] == 'var':
            return 'cd "$WT" && '
        if self.v['cwd'] == 'pushd':
            return 'pushd %s && ' % self.cwd_launch
        return 'cd %s && ' % self.cwd_launch if self.cwd_launch else ''

    def prompt_source(self, text, tag=''):
        """How the instruction reaches `claude`: (prompt words, prior records, files, instruction files)."""
        b, v = self.b, self.v
        via, src = v['via'], v['src']
        if via in ('arg', 'heredoc'):
            return text, [], {}, None
        P, A, B = (os.path.join(b.scratch, n % tag) for n in ('p%s.txt', 'pa%s.txt', 'pb%s.txt'))
        prior, files = [], {}
        if src == 'parts':
            a, c = split_text(text)
            files[A], files[B] = a, c
            prior += [(PRIOR_AT, 'bash', "cat > %s <<'EOF'\n%s\nEOF" % (A, a)), (PRIOR_AT + 120, 'bash', "cat > %s <<'EOF'\n%s\nEOF" % (B, c))]
            return None, prior, files, [A, B]
        files[P] = text
        if src == 'prior':
            prior.append((PRIOR_AT, 'bash', "cat > %s <<'EOF'\n%s\nEOF" % (P, text)))
        elif src == 'sed':
            a, c = split_text(text)
            tpl = os.path.join(b.scratch, 'tpl%s.txt' % tag)
            files[tpl] = a + '__ROLE__'
            prior.append((PRIOR_AT, 'bash', "cat > %s <<'EOF'\n%s__ROLE__\nEOF" % (tpl, a)))
            prior.append((PRIOR_AT + 90, 'bash', 'sed "s/__ROLE__/%s/" %s > %s' % (c.rstrip('.'), tpl, P)))
        elif src == 'write':
            prior.append((PRIOR_AT, 'write', (P, text)))
        return None, prior, files, [P]                         # absent: the file is on disk only

    def claude_cmd(self, text, flags, tail, tag=''):
        """`claude -p ...` for one launch of `text`; tail = the output redirect after the options."""
        via = self.v['via']
        words, prior, files, paths = self.prompt_source(text, tag)
        if via == 'arg':
            cmd = 'claude %s%s "%s"' % (flags, tail, words)
        elif via == 'heredoc':
            cmd = "claude %s%s <<'EOF'\n%s\nEOF" % (flags, tail, text)
        elif via == 'subst':
            cmd = 'claude %s%s "%s"' % (flags, tail, ''.join('$(cat %s)' % p for p in paths))
        elif via == 'stdin':
            cmd = 'claude %s%s < %s' % (flags, tail, paths[0] if len(paths) == 1 else '<(cat %s)' % ' '.join(paths))
        else:
            cmd = 'cat %s | claude %s%s' % (' '.join(paths), flags, tail)
        return cmd, prior, files

    def flags(self, resume_old=None):
        f, form = '-p --model %s' % MODEL, self.v['form']
        if form == 'resume':
            f += ' --resume %s' % self.G
        elif form == 'session_id':
            f += ' --session-id %s' % self.G
        elif form == 'fork':
            f += ' --resume %s --fork-session' % resume_old
        elif form == 'nopersist':
            f += ' --no-session-persistence'
        return f

    def out_parts(self):
        out = self.v['out']
        if out in ('json_file', 'reused', 'removed'):
            return '', ' --output-format json > %s' % self.out_path
        if out == 'json_var':
            return 'OUT=%s; ' % self.out_path, ' --output-format json > "$OUT"'
        if out == 'json_var_ext':
            return '', ' --output-format json > "$SPD/out.json"'
        if out == 'log_only':
            return '', ' > %s 2>&1' % os.path.join(self.b.scratch, 'run.log')
        return '', ''

    def compose(self, text, flags, tag=''):
        """The launching Bash command for `text` in the way the case says."""
        b, v = self.b, self.v
        way = v['way']
        head, tail = self.out_parts()
        cmd, prior, files = self.claude_cmd(text, flags, tail, tag)
        cd = self.cd_prefix()
        seq = 'claude -p --model %s "%s"; ' % (MODEL, prose(self.cid, 'seq-first', 130)) if v['timing'] == 'seq_late' and way != 'loop' else ''
        if v['src'] == 'posarg':
            script = os.path.join(b.scratch, 'run.sh')
            wrap = 'env -i PATH="$PATH" HOME="$HOME" ' if way == 'envi' else ''
            body = 'cd "$1"\n%sclaude -p --model "$2"%s "$3"\n' % (wrap, tail)
            prior.append((PRIOR_AT + 200, 'bash', "cat > %s <<'EOF'\n%sEOF" % (script, body)))
            files[script] = body
            return Launch(head + 'bash %s %s %s "%s"' % (script, self.cwd_launch or self.W, MODEL, text), prior, files)
        if way == 'bg':
            return Launch(head + cd + cmd, prior, files, 'bg')
        if way == 'detach':
            return Launch(head + cd + 'setsid nohup ' + cmd + (' > /dev/null' if not tail else '') + ' 2>&1 &', prior, files)
        if way == 'tmux':
            return Launch(head + cd + "tmux new-session -d -s w 'claude %s%s \"%s\"'" % (flags, tail, text), prior, files)
        if way == 'xargs':
            return Launch(head + cd + "printf '%%s\\n' \"%s\" | xargs -P2 -I{} claude %s%s \"{}\"" % (text, flags, tail), prior, files)
        if way == 'loop':
            first = prose(self.cid, 'seq-first', 130)
            return Launch(head + cd + 'for x in "%s" "%s"; do claude %s%s "$x"; done' % (first, text, flags, tail), prior, files)
        if way in ('script', 'pysub'):
            hidden = v['via'] == 'arg' and v['src'] == 'absent'
            if way == 'script':
                path, body = os.path.join(b.scratch, 'run.sh'), ('cd %s\n' % self.cwd_launch if self.cwd_launch else '') + head + cmd + '\n'
                call = 'bash %s' % path
            else:
                path = os.path.join(b.scratch, 'launch.py')
                argv = ', '.join(repr(a) for a in flags.split() + [text])
                body = 'import subprocess\nsubprocess.run([\'claude\', %s])\n' % argv
                call = 'python3 %s' % path
            files[path] = body
            if not hidden:
                prior.append((PRIOR_AT + 200, 'bash', "cat > %s <<'EOF'\n%sEOF" % (path, body)))
            return Launch(call, prior, files)
        wrap = {'direct': '', 'timeout': 'timeout 600 ', 'envi': 'env -i PATH="$PATH" HOME="$HOME" ', 'later': ''}[way]
        pre = 'sleep 90 && ' if way == 'later' else ''
        return Launch(head + pre + cd + seq + wrap + cmd, prior, files)

    # ------------------------------------------------------------------------------------------------ timeline
    def timeline(self):
        """Offsets: the launching call, the child's first record, how long the call stays open."""
        v, way = self.v, self.v['way']
        immediate = way in ('bg', 'detach', 'tmux')
        self.t0c = {'normal': 2.4, 'call_first': 8.4, 'seq_late': 121.0, 'bg_no_notif': 2.4}[v['timing']]
        if way == 'later':
            self.t0c = 92.0
        elif way == 'loop' and v['timing'] == 'normal':
            self.t0c = 41.0
        self.call_end = 0.4 if immediate else None            # a blocking call ends after the child; filled in when the child's end is known
        self.immediate = immediate

    def record_launch(self, tr, launch, tid, tc, end, notif=None, bg=None, prior_tr=None):
        """The prior calls (in `prior_tr`: the session that wrote the instruction, default the launcher), then the launching call itself, in `tr`."""
        b = self.b
        for i, (off, kind, payload) in enumerate(launch.prior):
            t = tc + off
            ptid = toolu(b, 'prior-%s-%d' % (tid, i))
            ptr = prior_tr or tr
            if kind == 'bash':
                ptr.bash(t, payload, ptid, end=t + 0.5, desc='prepare')
            elif kind == 'write':
                ptr.tool(t, 'Write', {'file_path': payload[0], 'content': payload[1]}, ptid)
                ptr.result(t + 0.5, ptid, 'written')
        bgi = bgid(b, 'bg-' + tid) if launch.mode == 'bg' else None
        tr.bash(tc, launch.cmd, tid, end=end, desc='launch child', bg=bgi, notif=notif if bgi else None)
        for p, t in launch.files.items():
            self.files[p] = t

    def record_instruction_only(self, launch, tid, tc):
        """A person started the child in a terminal: the calls that prepared the instruction file are in the author's record, the launching call is in nobody's."""
        for i, (off, kind, payload) in enumerate(launch.prior):
            t = tc + off
            ptid = toolu(self.b, 'prior-%s-%d' % (tid, i))
            if kind == 'bash':
                self.A.bash(t, payload, ptid, end=t + 0.5, desc='prepare')
            else:
                self.A.tool(t, 'Write', {'file_path': payload[0], 'content': payload[1]}, ptid)
                self.A.result(t + 0.5, ptid, 'written')
        for p, t in launch.files.items():
            self.files[p] = t

    # ------------------------------------------------------------------------------------------------ child records
    def child_records(self, G, cwd, text, t0, finished, turn=None, tr=None, forked_from=None, cost_ms=9000):
        """One run of a `claude -p` child."""
        tr = tr or self.child
        tr.prompt(t0, text, source='sdk', turn=turn)
        if forked_from:
            tr.rows[-1][2]['forkedFrom'] = {'sessionId': forked_from}
        if finished:
            tr.tool(t0 + 3, 'Read', {'file_path': os.path.join(cwd, 'notes.md')}, toolu(self.b, 'cr-%s-%s' % (G, t0)))
            tr.result(t0 + 4, toolu(self.b, 'cr-%s-%s' % (G, t0)), 'notes')
            tr.say(t0 + 8, 'Finished the review.', 'end_turn')
            tr.cost_state(t0 + 9, cost_ms)
        else:
            tr.tool(t0 + 4, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(self.b, 'cr-%s-%s' % (G, t0)))
        return tr

    # ------------------------------------------------------------------------------------------------ the case
    def build(self):
        b, v, cid = self.b, self.v, self.cid
        if v['target'] != 'cli':
            return self.build_codex()
        self.make_launcher()
        if v['out'] == 'overwritten':
            self.setup_peer()
        if v['author'] != 'self' and v['bait'] == 'none':
            self.setup_author()
        self.timeline()
        live = v['seen'] == 'live'
        tc = b.T(self.tc)
        t0c = b.T(self.t0c)
        finished = not live or v['out'] == 'overwritten'          # a peer's child was resumed later: its first run is over either way
        G = self.G
        persists = v['form'] not in ('nopersist', 'deleted')
        bait = v['bait']
        # --- a resumed child: its first run, launched by the main session earlier
        if v['form'] == 'resume':
            self.child = b.transcript('child', self.child_cwd, 'sdk-cli', sid=G)
            first = prose(cid, 'child-run1', 160)
            self.O.bash(b.T(-3600), 'cd %s && claude -p --model %s "%s"' % (self.child_cwd, MODEL, first), toolu(b, 'run1'), end=b.T(-3590), desc='first run')
            self.child_records(G, self.child_cwd, first, b.T(-3598), True, tr=self.child)
        # --- the launching call
        if bait in ('none', 'foreign'):
            self.launch = launch = self.compose(self.text, self.flags(resume_old=b.sid('forkbase')))
            end = b.T(self.call_end) if self.call_end is not None else (t0c + 9.5 if finished else None)
            bgn = b.T(self.t0c + 9) if (launch.mode == 'bg' and v['timing'] != 'bg_no_notif' and finished) else None
            if v['timing'] == 'seq_late':
                end = (t0c + 100) if finished else None
            if v['way'] == 'later' and finished:
                end = t0c + 9.5
            tid = self.launch_tid = toolu(b, 'launch')
            if v['timing'] == 'seq_late' or v['way'] == 'loop':        # a first child ran in the same call before the target
                self.sequential_first(tc)
            if v['starter'] == 'person':
                self.record_instruction_only(launch, tid, tc)
            else:
                self.record_launch(self.L, launch, tid, tc, end, notif=bgn, prior_tr=self.A)
            if self.A is not None:
                self.author_busy(tc)
        else:
            self.bait_call(tc, t0c)
        # --- output-file sibling for a reused path
        if v['out'] == 'reused':
            S2 = b.sid('sib2')
            other = prose(cid, 'child2', 150)
            cmd2 = 'cd %s && claude -p --model %s --output-format json "%s" > %s' % (self.W, MODEL, other, self.out_path)
            self.L.bash(tc + 30, cmd2, toolu(b, 'launch2'), end=tc + 45, desc='second launch')
            s2 = b.transcript('sib2', self.W, 'sdk-cli', sid=S2)
            self.child_records(S2, self.W, other, b.T(32), True, tr=s2)
            s2.save()
            self.files[self.out_path] = out_json(S2)
        elif v['out'] == 'overwritten':
            self.stale_launch(tc)
        elif v['out'] in ('json_file', 'json_var', 'removed') and bait in ('none', 'foreign'):
            self.files[self.out_path] = out_json(G if bait == 'none' else b.sid('stranger'))
        elif v['out'] == 'json_var_ext':
            self.files[os.path.join(b.scratch, 'out.json')] = out_json(G if bait == 'none' else b.sid('stranger'))
        elif v['out'] == 'log_only':
            self.files[os.path.join(b.scratch, 'run.log')] = 'done\n'
        # --- the child itself
        if persists and v['form'] != 'resume':
            self.child = b.transcript('child', self.child_cwd, 'sdk-cli', sid=G)
        if persists:
            fork = b.sid('forkbase') if v['form'] == 'fork' else None
            self.child_records(G, self.child_cwd, self.child_text(), t0c, finished, forked_from=fork)
            self.child_extras()
            if v['out'] == 'overwritten':
                self.resume_over(tc)
            self.child.save()
        else:
            b.ids['child'] = G
            b.meta['norecord'] = True
        self.decoys(tc, t0c)
        self.finish_launcher(finished)
        self.processes(live, t0c)
        for p, t in self.files.items():
            put(p, t, self.file_times.get(p))
        if v['out'] == 'removed' and os.path.exists(self.out_path):
            os.remove(self.out_path)
        b.main_path = self.O.path
        others = [x for x in (self.peer, self.A) if x is not None and x is not self.O]
        for tr in [self.O] + getattr(self, 'subs', []) + ([self.K] if hasattr(self, 'K') else []) + [x for x in others if x not in getattr(self, 'subs', [])]:
            tr.save()

    # ------------------------------------------------------------------------------------------------ somebody else's part of the scene
    def setup_author(self):
        """The session that writes the instruction file: a peer session in another folder, the main session (while a sub-agent launches) or a sub-agent (while the
        main session launches). It does not launch this child; it writes the text and is busy with something else when the launch happens."""
        b, who = self.b, self.v['author']
        if who == 'peer':
            os.makedirs(self.author_cwd, exist_ok=True)
            self.A = b.transcript('author', self.author_cwd, 'cli')
            self.A.prompt(b.T(-2000), 'Another session at work.', source='human')
        elif who == 'main':
            self.A = self.O
        else:
            self.A = self.make_sub('writer', 'Prompt writer', -500)

    def author_busy(self, tc):
        """What the author runs while the launch happens: a script that launches nothing (a server, a build, a test run), or the launch of an unrelated child."""
        b, v, A = self.b, self.v, self.A
        kind, tid, bg = v['busy'], toolu(b, 'busy'), bgid(b, 'busy')
        if kind == 'idle':
            return                                         # the author wrote the file and runs nothing
        if kind in ('script_unread', 'script_var'):
            # a script whose body cannot be read, so nothing says what it starts: its file is not on disk, or its path is a variable nothing in the command defines
            path = os.path.join(b.scratch, 'tool.py') if kind == 'script_unread' else '"$TOOLS/tool.py"'
            A.bash(tc - 30, 'cd %s && python3 %s' % (A.cwd, path), tid, end=tc - 29.6, desc='run the tool', bg=bg)
        elif kind == 'py':
            path = os.path.join(b.scratch, 'serve.py')
            self.files[path] = 'import time\nprint("serving")\ntime.sleep(3600)\n'
            A.bash(tc - 30, 'cd %s && python3 %s' % (A.cwd, path), tid, end=tc - 29.6, desc='start the server', bg=bg)
        elif kind == 'sh':
            path = os.path.join(b.scratch, 'build.sh')
            self.files[path] = 'echo building\nsleep 400\n'
            A.bash(tc - 30, 'bash %s' % path, tid, end=tc + 400, desc='build')
        elif kind == 'unittest':
            A.bash(tc - 30, 'cd %s && python3 -m unittest discover -s tests' % A.cwd, tid, end=tc + 300, desc='run the tests')
        else:
            other = prose(self.cid, 'busy-child', 150)
            K = b.transcript('busykid', A.cwd, 'sdk-cli')
            words = other
            if kind == 'child_file':
                # the author's own launch reads its words from a file the author wrote: what it launches is known, and is not this child
                path = os.path.join(b.scratch, 'other.md')
                self.files[path] = other
                wid = toolu(b, 'busy-write')
                A.tool(tc - 200, 'Write', {'file_path': path, 'content': other}, wid)
                A.result(tc - 199.5, wid, 'written')
                words = '$(cat %s)' % path
            A.bash(tc - 5, 'cd %s && claude -p --model %s "%s"' % (A.cwd, MODEL, words), tid, end=tc - 4.6, desc='launch another child', bg=bg, notif=tc + 8.5)
            self.child_records(K.sid, A.cwd, other, tc - 2.6, True, tr=K)
            K.save()

    def setup_peer(self):
        """out=overwritten: the launcher is a peer session of the main session's folder (not the page's session)."""
        b = self.b
        self.peer = b.transcript('orch2', self.W, 'cli')
        self.peer.prompt(b.T(-2000), 'Another orchestrator at work.', source='human')
        self.L = self.peer
        self.tree_sid, self.tree_pid = b.sid('orch2'), 200
        self.obs_shift = 210.0

    def stale_launch(self, tc):
        """The main session's own launch of another child, redirected to the output path and still running when the target starts (its words are not the target's)."""
        b, W = self.b, self.W
        text = prose(self.cid, 'child2', 150)
        H = b.transcript('sib2', W, 'sdk-cli', sid=b.sid('sib2'))
        self.O.bash(tc - 60, 'cd %s && claude -p --model %s --output-format json "%s" > %s' % (W, MODEL, text, self.out_path), toolu(b, 'stale-launch'), end=tc + 250, desc='launch another child')
        self.child_records(H.sid, W, text, tc - 57.6, True, tr=H)
        H.save()

    def resume_over(self, tc):
        """Much later the peer resumes the real child and its output lands on the same path: the file now names the child, and no launch call of the main session made it."""
        b, W, G = self.b, self.W, self.G
        t = tc + 200
        live = self.v['seen'] == 'live'
        self.peer.bash(t, 'cd %s && claude -p --model %s --resume %s --output-format json "%s" > %s' % (W, MODEL, G, RESUME_TEXT, self.out_path), toolu(b, 'resume-over'),
                       end=None if live else t + 12, desc='resume child')
        self.child_records(G, self.child_cwd, RESUME_TEXT, t + 2.4, not live, tr=self.child)
        self.files[self.out_path] = out_json(G)
        self.file_times[self.out_path] = t + 12

    def child_text(self):
        """What the child's first instruction line says: the text the launcher shows, except for a stranger (the `foreign` twin), which was asked something else."""
        if self.v['bait'] == 'foreign':
            return '/zz-help' if self.v['decoy'] == 'short' else prose(self.cid, 'stranger', 200)
        return self.text

    def child_extras(self):
        v, b = self.v, self.b
        if v['decoy'] == 'xmsg':
            self.child.peer_message(b.T(self.t0c + 5), self.sibling_text())

    def sibling_text(self):
        """A sibling's instruction: a quarter of the target's text plus its own words (the 0.19-0.31 overlap the real records show)."""
        return self.text[:max(33, len(self.text) // 4)] + ' ' + prose(self.cid, 'sibling', 140, lead='Then', tail='and stop.')

    def sequential_first(self, tc):
        """seq_late: the call first ran another child (for 118 s) and only then the target."""
        b = self.b
        first = prose(self.cid, 'seq-first', 130)
        S1 = b.transcript('seq1', self.child_cwd, 'sdk-cli', sid=b.sid('seq1'))      # the same call, so the same folder as the target (after its `cd`)
        self.child_records(b.sid('seq1'), self.child_cwd, first, tc + 1.0, True, tr=S1)
        S1.save()

    def bait_call(self, tc, t0c):
        """The twin's launch call: it looks like a launch of the child but is not one (see the oracle for why each bait is a negative)."""
        b, v, W = self.b, self.v, self.W
        bait, tid = v['bait'], toolu(b, 'bait')
        other = prose(self.cid, 'bait-other', 160)
        if bait == 'cwd_mismatch':
            d = os.path.join(b.work, 'elsewhere')
            os.makedirs(d, exist_ok=True)
            self.L.bash(tc, 'cd %s && claude -p --model %s "%s"' % (d, MODEL, self.text), tid, end=t0c + 9, desc='launch')      # same words, another folder: only the folder differs
        elif bait == 'text_mismatch':
            self.L.bash(tc, 'cd %s && claude -p --model %s "%s"' % (W, MODEL, other), tid, end=t0c + 9, desc='launch')
        elif bait == 'too_old':
            self.L.bash(tc - 7200, 'cd %s && claude -p --model %s "%s"' % (W, MODEL, self.text), tid, end=tc - 7190, desc='launch')
        elif bait == 'echo_only':
            self.L.bash(tc, "echo 'Run: claude -p \"%s\"'" % self.text, tid, end=tc + 0.2, desc='note the command')
        # other_user / pid_reuse: no launching call at all, only a process that must not be trusted

    def decoys(self, tc, t0c):
        b, v, cid, W = self.b, self.v, self.cid, self.W
        d = v['decoy']
        if d == 'sibling':
            S = b.transcript('sibling', self.W, 'sdk-cli')
            st = self.sibling_text()
            t_sib = self.t0c + 0.2                                     # the sibling starts right after the target, however late the target starts
            self.L.bash(tc + 0.3, 'cd %s && claude -p --model %s "%s"' % (W, MODEL, st), toolu(b, 'sib-call'), end=b.T(t_sib + 9.5), desc='launch sibling')   # its call is still open then: no orphan
            self.child_records(S.sid, W, st, b.T(t_sib), True, tr=S)
            S.save()
        elif d == 'watcher':
            self.L.bash(tc - 600, 'while [ -d /proc/99999 ]; do sleep 5; done', toolu(b, 'watch'), end=None, desc='watch', bg=bgid(b, 'watch'))
        elif d in ('concurrent', 'twin_text'):
            O2 = b.transcript('orch2', W, 'cli')
            O2.prompt(b.T(-2000), 'Another orchestrator at work.', source='human')
            t2 = self.text if d == 'twin_text' else prose(cid, 'child-2', 200)
            # the competing launch runs in the same folder as the target's (it would otherwise be refuted by folder and the tie would not exist)
            O2.bash(tc + 0.5, 'cd %s && claude -p --model %s "%s"' % (self.cwd_launch or W, MODEL, t2), toolu(b, 'o2-call'), end=t0c + 9.5, desc='launch')
            G2 = b.transcript('child2', self.child_cwd, 'sdk-cli')
            self.child_records(G2.sid, self.child_cwd, t2, t0c + 0.3, v['seen'] != 'live', tr=G2)
            G2.save()
            O2.save()
        elif d == 'same_n':
            for i, off in enumerate((-120.0, -60.0)):
                S = b.transcript('rep%d' % i, W, 'sdk-cli')
                self.L.bash(tc + off, 'cd %s && claude -p --model %s "%s"' % (W, MODEL, self.text), toolu(b, 'rep-call%d' % i), end=tc + off + 10, desc='launch again')
                self.child_records(S.sid, W, self.text, b.T(off + 2.4), True, tr=S)
                S.save()
        elif d == 'subnoise':
            S = self.make_sub('noise', 'Test runner', -300)
            S.bash(tc - 5, 'bash %s' % os.path.join(b.scratch, 'watch.sh'), toolu(b, 'noise-call'), end=tc + 600, desc='run tests')
            self.files[os.path.join(b.scratch, 'watch.sh')] = 'sleep 600\n'
        elif d == 'paste':
            I = b.transcript('paste', W, 'cli')
            I.prompt(b.T(self.t0c + 3), self.text, source='human')
            I.say(b.T(self.t0c + 10), 'Looking at it.', 'end_turn')
            I.save()
        elif d == 'sidmention':
            M = b.transcript('mention', os.path.join(b.work, 'audit'), 'cli')
            M.prompt(b.T(self.t0c + 20), 'Audit the sessions.', source='human')
            M.bash(b.T(self.t0c + 30), 'grep -c %s /tmp/notes.txt' % self.G, toolu(b, 'mention'), end=b.T(self.t0c + 31), desc='look for the id')
            M.save()
        elif d == 'switch':
            new = b.transcript('orch_new', W, 'cli')
            new.prompt(b.T(-100), 'Fresh session after the switch.', source='human')
            new.save()

    def finish_launcher(self, finished):
        """What the launcher's record shows afterwards: a sub-agent that launched its child says it is done."""
        if self.v['spawner'] == 'sub' and self.v['bait'] == 'none':
            self.L.say(self.b.T(self.t0c + 60), 'Launched and reviewed.', 'end_turn')

    # ------------------------------------------------------------------------------------------------ processes
    def processes(self, live, t0c):
        b, v = self.b, self.v
        now_live, now_dead = b.T(self.t0c + 20 + self.obs_shift), b.T(self.t0c + 300 + self.obs_shift)
        O_sid = b.sid('orch')
        if v['decoy'] == 'switch':
            O_sess = session_file(b, 100, b.sid('orch_new'), self.W, 'cli', b.T(-100))
        else:
            O_sess = session_file(b, 100, O_sid, self.W, 'cli', b.T(-3000))
        base = [proc(100, 1, ['claude', '--dangerously-skip-permissions'], env={}, session=O_sess)]
        if self.peer is not None:
            base.append(proc(200, 1, ['claude'], env={}, session=session_file(b, 200, b.sid('orch2'), self.W, 'cli', b.T(-2000))))
        elif v['author'] == 'peer' and v['bait'] == 'none':
            base.append(proc(200, 1, ['claude'], env={}, session=session_file(b, 200, b.sid('author'), self.author_cwd, 'cli', b.T(-2000))))
        alive = list(base)
        bait, way = v['bait'], v['way']
        has_proc = v['form'] != 'deleted'
        parent_sid, parent_pid = self.tree_sid, self.tree_pid
        shell_parent = 100
        if v['spawner'] == 'grand' and bait in ('none', 'foreign'):
            alive.append(proc(101, 100, ['/bin/bash', '-c', 'eval "claude -p"']))
            alive.append(proc(110, 101, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': O_sid, 'CLAUDE_PID': '100'},
                              session=session_file(b, 110, b.sid('mid'), self.W, 'sdk-cli', b.T(-98))))
            shell_parent = 110
        shell = 111 if v['spawner'] == 'grand' and bait in ('none', 'foreign') else 101
        if self.peer is not None:
            shell_parent, shell = 200, 201
        alive.append(proc(shell, shell_parent, ['/bin/bash', '-c', 'source snapshot.sh && eval "run"']))
        mid = None
        if bait in ('none', 'foreign'):
            if way in ('script', 'xargs', 'pysub'):
                mid = shell + 1
                alive.append(proc(mid, shell, {'script': ['/bin/bash', os.path.join(b.scratch, 'run.sh')], 'xargs': ['xargs', '-P2', '-I{}', 'claude'],
                                             'pysub': ['python3', os.path.join(b.scratch, 'launch.py')]}[way]))
            ppid = {'detach': 1, 'tmux': 1}.get(way, mid or shell)
        else:
            ppid = shell
        kid_pid = shell + 2
        env = {'CLAUDE_CODE_SESSION_ID': O_sid if v['decoy'] == 'switch' else parent_sid, 'CLAUDE_PID': str(parent_pid)} if WAY[way]['env'] else {}
        if bait in ('other_user', 'pid_reuse'):
            env, ppid = {'CLAUDE_CODE_SESSION_ID': O_sid, 'CLAUDE_PID': '100'}, shell          # the process looks like a child of the main session, but must not be trusted
        elif bait != 'none' or v['starter'] == 'person':
            env, ppid = {}, 1                                                                # started from a plain terminal: no Claude above it
        kid_start = self.t0c + (200 if self.peer is not None else 0)                  # the process of the run that is alive: the resumed one for a peer's child
        sess = session_file(b, kid_pid, self.G, self.child_cwd, 'sdk-cli', b.T(kid_start), proc_start='auto' if bait != 'pid_reuse' else '999999')
        kid = proc(kid_pid, ppid, ['claude', '-p', '--model', MODEL], env=env, session=sess, uid='other' if bait == 'other_user' else None)
        if has_proc and v['target'] == 'cli':
            alive.append(kid)
        if v['decoy'] in ('concurrent', 'twin_text'):
            alive += [proc(200, 1, ['claude'], env={}, session=session_file(b, 200, b.sid('orch2'), self.W, 'cli', b.T(-2000))),
                      proc(201, 200, ['/bin/bash', '-c', 'eval run']),
                      proc(230, 201, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': b.sid('orch2'), 'CLAUDE_PID': '200'},
                           session=session_file(b, 230, b.sid('child2'), self.child_cwd, 'sdk-cli', b.T(self.t0c + 0.3)))]
        dead = list(base)
        if v['decoy'] in ('concurrent', 'twin_text'):
            dead.append(alive[-3])
        seen = v['seen']
        if seen == 'live':
            b.phases = [Phase(now_live, alive, 0)]
        elif seen == 'ended_seen':
            b.phases = [Phase(now_live, alive, 0), Phase(now_dead, dead, 0)]
        elif seen == 'ended_unseen':
            b.phases = [Phase(now_dead, dead, 0)]
        elif seen == 'restart_seen':
            b.phases = [Phase(now_live, alive, 0), Phase(now_dead, dead, 1)]
        else:
            b.phases = [Phase(now_dead, dead, 1)]

    # ------------------------------------------------------------------------------------------------ Codex targets
    def build_codex(self):
        b, v, cid, W = self.b, self.v, self.cid, self.W
        self.make_launcher()
        tc = b.T(0)
        tid = b.ids['child'] = '019a%04d-0000-7000-8000-%012d' % (int(digest(cid, 'tid', n=3), 16) % 10000, int(digest(cid, 'tid2', n=6), 16) % 10 ** 12)
        text = prose(cid, 'codex-child', 200)
        tgt, bait = v['target'], v['bait']
        thread_text = prose(cid, 'stranger', 200) if bait == 'foreign' else text           # what the thread is asked: a stranger was asked something else
        stranger = bait != 'none'
        if tgt == 'cx_exec':
            if bait == 'none' or bait == 'foreign':
                cmd = 'cd %s && codex exec -m gpt-6.1-sol "%s"' % (W, text)
                if v['way'] == 'bg':
                    self.L.bash(tc, cmd, toolu(b, 'cx-launch'), end=tc + 0.4, desc='launch codex', bg=bgid(b, 'cx'), notif=tc + 40 if v['seen'] != 'live' else None)
                else:
                    pre = 'sleep 40 && ' if v['way'] == 'later' else ''
                    done = tc + (51.4 if v['way'] == 'later' else 42)               # the call returns when the thread is over, whenever it started
                    self.L.bash(tc, pre + cmd, toolu(b, 'cx-launch'), end=done if v['seen'] != 'live' else None, desc='launch codex')
            else:
                self.codex_bait_call(tc, text)
            start = tc + (42.4 if v['way'] == 'later' else 2.4)
            origin, parent = 'codex_exec', None
        else:
            # a thread that is not an exec child; the main session also launches an unrelated exec so there is something to mistake it for
            self.L.bash(tc, 'cd %s && codex exec -m gpt-6.1-sol "%s"' % (W, prose(cid, 'cx-other', 160)), toolu(b, 'cx-other'), end=tc + 20, desc='launch codex')
            start = tc + 3.0
            origin = {'cx_tui': 'codex-tui', 'cx_desktop': 'Codex Desktop', 'cx_guardian': 'codex_exec'}[tgt]
            parent = '019a0000-0000-7000-8000-%012d' % 7 if tgt == 'cx_guardian' else None
        path = codex_rollout(b, tid, start, W, thread_text, origin, parent=parent, complete=v['seen'] != 'live')
        b.paths['child'] = path
        launched = tgt == 'cx_exec' and not stranger         # only an exec thread of the main session has a Claude above it; the others were opened by a person in a plain terminal
        shell = proc(101, 100, ['/bin/bash', '-c', 'eval "codex exec"'])
        argv = ['codex', 'exec', '-m', 'gpt-6.1-sol', thread_text] if (launched or (stranger and tgt == 'cx_exec')) else ['codex']
        kid = proc(103, 101 if launched else 1, argv, env={'CLAUDE_CODE_SESSION_ID': b.sid('orch'), 'CLAUDE_PID': '100'} if launched else {}, fds=[path])
        O_sess = session_file(b, 100, b.sid('orch'), W, 'cli', b.T(-3000))
        base = [proc(100, 1, ['claude'], env={}, session=O_sess)]
        alive = base + [shell, kid]
        if v['seen'] == 'live':
            b.phases = [Phase(start + 20, alive, 0)]
        else:
            b.phases = [Phase(start + 300, base, 0)]
        self.O.say(b.T(1000), 'Done.', 'end_turn')
        self.O.save()
        for S in getattr(self, 'subs', []):
            S.save()
        if hasattr(self, 'K'):
            self.K.save()
        b.main_path = self.O.path

    def codex_bait_call(self, tc, text):
        """The launch call of a Codex twin: it looks like the launch of the thread and is not one (see the oracle for why each bait is a negative)."""
        b, W = self.b, self.W
        tid = toolu(b, 'cx-bait')
        bait = self.v['bait']
        if bait == 'text_mismatch':
            self.L.bash(tc, 'cd %s && codex exec -m gpt-6.1-sol "%s"' % (W, prose(self.cid, 'bait-other', 160)), tid, end=tc + 9, desc='launch codex')      # other words
        elif bait == 'cwd_mismatch':
            d = os.path.join(b.work, 'elsewhere')
            os.makedirs(d, exist_ok=True)
            self.L.bash(tc, 'codex exec -C %s -m gpt-6.1-sol "%s"' % (d, text), tid, end=tc + 9, desc='launch codex')                                  # same words, another folder
        elif bait == 'too_old':
            self.L.bash(tc - 7200, 'cd %s && codex exec -m gpt-6.1-sol "%s"' % (W, text), tid, end=tc - 7190, desc='launch codex')
        elif bait == 'echo_only':
            self.L.bash(tc, "echo 'Run: codex exec \"%s\"'" % text, tid, end=tc + 0.2, desc='note the command')


def codex_rollout(b, tid, t, cwd, user, originator, parent=None, complete=True, error=None, model='gpt-6.1-sol', extra=None):
    """One Codex rollout file: session_meta, task_started, the user message, an answer, and (when finished) task_complete."""
    d = os.path.join(b.codex, 'sessions', '2026', '10', '01')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'rollout-2026-10-01T00-00-00-%s.jsonl' % tid)
    meta = {'id': tid, 'originator': originator, 'cwd': cwd, 'timestamp': iso(t), 'cli_version': '0.157.0'}
    if parent:
        meta['parent_thread_id'] = parent
    rows = [(t, {'timestamp': iso(t), 'type': 'session_meta', 'payload': meta}),
            (t + 0.2, {'timestamp': iso(t + 0.2), 'type': 'event_msg', 'payload': {'type': 'task_started', 'model_context_window': 200000}}),
            (t + 0.3, {'timestamp': iso(t + 0.3), 'type': 'turn_context', 'payload': {'model': model, 'effort': 'high'}}),
            (t + 0.4, {'timestamp': iso(t + 0.4), 'type': 'response_item',
                       'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': user}]}})]
    for dt, payload in (extra or []):
        rows.append((t + dt, {'timestamp': iso(t + dt), 'type': 'event_msg', 'payload': payload}))
    if complete:
        rows.append((t + 6, {'timestamp': iso(t + 6), 'type': 'response_item',
                             'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Finished.'}]}}))
        done = {'type': 'task_complete', 'last_agent_message': 'Finished.'}
        if error:
            done['error'] = error
        rows.append((t + 7, {'timestamp': iso(t + 7), 'type': 'event_msg', 'payload': done}))
    put(path, ''.join(dump(d) + '\n' for _, d in sorted(rows, key=lambda r: r[0])), rows[-1][0])
    return path
