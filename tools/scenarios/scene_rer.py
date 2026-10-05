"""The rerun scene: a Claude orchestrator starts the participants of a debate with scripts that redirect every run's result to a file named after the participant. The first runs
stop after about two minutes (no report). The orchestrator fixes the brief and starts the same scripts again, with the same command text: new sessions (no `--resume`), the same
instruction, the same output file, which the second runs overwrite, so that it names the new `claude -p` run only.

A is a `claude -p` run (`run_claude.sh mpd-A opus`, `--output-format json > out/mpd-A.json`), B a `codex exec` run (`run_codex.sh mpd-B sol`, `-o out/mpd-B.md`). Every name, path and
word is synthetic; the orchestrator's record holds what it wrote (the scripts and the instruction files with its Write tool), so the board can read what each command runs.
"""

import os

from .axes import digest
from .build import MODEL, Phase, proc, prose, put, session_file
from .cx_record import MODEL as CX_MODEL, Rollout, rollout_path, tid_of
from .scene_aff import bgid, out_json, toolu
from .scene_deb import brief_text

FIRST = {'a': 0.0, 'b': 1.0}              # when the first commands are called (offsets from the base of the case)
SECOND = {'a': 330.0, 'b': 331.0}         # ... and the second ones, after the brief was fixed
STOP_AFTER = 120.0                        # the first runs stop this long after they started
RUN2 = 60.0                               # the second runs take this long
SEAT = {'a': 'A', 'b': 'B'}
DESC1 = {'a': 'Start participant A', 'b': 'Start participant B'}
DESC2 = {'a': 'Start participant A again after the brief fix', 'b': 'Start participant B again after the brief fix'}


class Rer:
    """Builds one `rer` case into the Built `b`."""

    def __init__(self, b):
        self.b, self.v, self.cid = b, b.case.v, b.case.id
        self.W = os.path.join(b.work, 'repo')
        os.makedirs(os.path.join(self.W, '.git'), exist_ok=True)
        put(os.path.join(self.W, '.git', 'HEAD'), 'ref: refs/heads/main\n')
        self.unit = os.path.join(self.W, 'docs', 'rev')
        self.R = os.path.join(b.scratch, 'mpd')                       # the folder of the scripts, the instruction files and the output files
        self.parts = [x for x in ('a', 'b') if x in self.v['parts']]
        b.ids['title'] = DESC2['a']                                  # what the page should call the second `claude -p` run: the description of its command
        self.trs, self.rolls, self.files = [], [], {}

    def at(self, off):
        return self.b.T(off)

    def report_path(self, x):
        return os.path.join(self.unit, 'r1', SEAT[x] + '.md')

    def build(self):
        b, W, R = self.b, self.W, self.R
        b.meta['repo'] = W
        b.meta['page'] = 'claude'
        O = self.O = b.transcript('orch', W, 'cli')
        O.prompt(b.T(-3000), 'Run the review in docs/rev with the participants of the brief.', source='human')
        self.trs.append(O)
        brief = os.path.join(self.unit, 'brief.md')
        put(brief, brief_text('r1'))
        # what the orchestrator writes first: one script for each kind of run and the instruction of each participant (the same words every time it runs)
        scripts = {'a': ('run_claude.sh', '#!/bin/bash\n# usage: run_claude.sh <name> <model>\ncd %s\nclaude -p --model "$2" --output-format json "$(cat %s/prompts/$1.txt)" > %s/out/$1.json\n' % (W, R, R)),
                   'b': ('run_codex.sh', '#!/bin/bash\n# usage: run_codex.sh <name> <model>\ncd %s\ncodex exec -m "$2" -o %s/out/$1.md "$(cat %s/prompts/$1.txt)" > %s/out/$1.log 2>&1\n' % (W, R, R, R))}
        self.text = {x: '%s Write your report to %s.' % (prose(self.cid, 'rer-' + x, 150, lead='You are participant %s of the review in docs/rev. Read brief.md and' % SEAT[x], tail='when you are done.'),
                                                           self.report_path(x)) for x in ('a', 'b')}
        t = -400.0
        for x in self.parts:
            name, body = scripts[x]
            path = os.path.join(R, name)
            O.tool(b.T(t), 'Write', {'file_path': path, 'content': body}, toolu(b, 'w-script-' + x))
            O.result(b.T(t + 0.4), toolu(b, 'w-script-' + x), 'written')
            self.files[path] = body
            ppath = os.path.join(R, 'prompts', 'mpd-%s.txt' % SEAT[x])
            O.tool(b.T(t + 1), 'Write', {'file_path': ppath, 'content': self.text[x]}, toolu(b, 'w-prompt-' + x))
            O.result(b.T(t + 1.4), toolu(b, 'w-prompt-' + x), 'written')
            self.files[ppath] = self.text[x]
            t += 5
        for x in self.parts:
            self.first_run(x)
        O.tool(b.T(300), 'Edit', {'file_path': brief, 'old_string': 'Reports are written', 'new_string': 'Each participant writes a report of its own; they are written'}, toolu(b, 'edit-brief'))
        O.result(b.T(300.4), toolu(b, 'edit-brief'), 'edited')
        for x in self.parts:
            self.second_run(x)
        for p, text in self.files.items():
            put(p, text)
        for r in self.rolls:
            r.save()
        for tr in self.trs:
            tr.save()
        b.main_path = O.path
        base = [proc(100, 1, ['claude'], env={}, session=session_file(b, 100, O.sid, W, 'cli', b.T(-3000)))]
        b.phases = [Phase(b.T(500), base, 0)]

    # ---- the commands ----
    def command(self, x):
        script = 'run_claude.sh mpd-A opus' if x == 'a' else 'run_codex.sh mpd-B sol'
        return 'R=%s && $R/%s' % (self.R, script)

    def call(self, x, n, t, end, notif):
        b = self.b
        cid = b.ids['call_%s%d' % (x, n)] = toolu(b, 'run-%s%d' % (x, n))
        self.O.tool(b.T(t), 'Bash', {'command': self.command(x), 'description': (DESC1 if n == 1 else DESC2)[x], 'run_in_background': True}, cid)
        task = bgid(b, 'run-%s%d' % (x, n))
        self.O.result(b.T(t + 0.4), cid, 'started', bg=task)
        self.O.notification(b.T(end), task, cid, 'failed' if n == 1 else 'completed',
                            'Background command "%s" %s' % ((DESC1 if n == 1 else DESC2)[x], 'failed with exit code 1' if n == 1 else 'completed (exit code 0)'))

    def first_run(self, x):
        """The first run: it starts, reads a little and stops about two minutes later, with no report."""
        b, W = self.b, self.W
        t0 = FIRST[x] + 2.4
        self.call(x, 1, FIRST[x], t0 + STOP_AFTER + 0.5, None)
        if x == 'a':
            tr = b.transcript('a1', W, 'sdk-cli')
            tr.prompt(b.T(t0), self.text[x], source='sdk')
            tr.tool(b.T(t0 + 4), 'Read', {'file_path': os.path.join(self.unit, 'brief.md')}, toolu(b, 'a1-read'))
            tr.result(b.T(t0 + 5), toolu(b, 'a1-read'), 'brief')
            if self.v['stop'] == 'cost':
                tr.cost_state(b.T(t0 + STOP_AFTER), int(STOP_AFTER * 1000))          # closed with no end of turn: it was stopped
            self.trs.append(tr)
        else:
            tid = b.ids['b1'] = tid_of(self.cid, 'b1')
            r = Rollout(rollout_path(b.codex, tid, b.T(t0 + 1)), tid, W, self.cid, origin='codex_exec')
            turn = 'turn-' + digest(self.cid, 'b1', n=16)
            r.meta(b.T(t0 + 1))
            r.task_started(b.T(t0 + 1.5), turn)
            r.turn_context(b.T(t0 + 1.6), turn)
            r.user(b.T(t0 + 2), self.text[x], turn)
            r.usage(b.T(t0 + 20), turn, 900, 60)
            if self.v['stop'] == 'cost':
                r.aborted(b.T(t0 + STOP_AFTER), turn)
            else:
                r.usage(b.T(t0 + STOP_AFTER - 10), turn, 1100, 80)                      # the record just stops: the process was killed
            self.rolls.append(r)
            b.paths['b1'] = r.path

    def second_run(self, x):
        """The second run: the same command text, a new session with the same instruction; it writes its report and ends, and the output file now holds its session id."""
        b, W = self.b, self.W
        t0 = SECOND[x] + 2.4
        end = t0 + RUN2
        self.call(x, 2, SECOND[x], end + 0.5, None)
        report = 'Findings of %s: the gate policy reads clearly; one example needs a second look.\n' % SEAT[x]
        if x == 'a':
            tr = b.transcript('a2', W, 'sdk-cli')
            tr.prompt(b.T(t0), self.text[x], source='sdk')
            tr.tool(b.T(t0 + 10), 'Write', {'file_path': self.report_path(x), 'content': report}, toolu(b, 'a2-write'))
            tr.result(b.T(t0 + 10.5), toolu(b, 'a2-write'), 'written')
            tr.say(b.T(end - 1), 'The report is written.', 'end_turn')
            tr.cost_state(b.T(end), int(RUN2 * 1000))
            self.trs.append(tr)
            self.files[self.report_path(x)] = report
            self.files[os.path.join(self.R, 'out', 'mpd-A.json')] = out_json(tr.sid)         # the file the first run left now names the second one
        else:
            tid = b.ids['b2'] = tid_of(self.cid, 'b2')
            r = Rollout(rollout_path(b.codex, tid, b.T(t0 + 1)), tid, W, self.cid, origin='codex_exec')
            turn = 'turn-' + digest(self.cid, 'b2', n=16)
            r.meta(b.T(t0 + 1))
            r.task_started(b.T(t0 + 1.5), turn)
            r.turn_context(b.T(t0 + 1.6), turn)
            r.user(b.T(t0 + 2), self.text[x], turn)
            r.usage(b.T(t0 + 20), turn, 900, 60)
            r.file_change(b.T(t0 + 40), self.report_path(x), report, turn)
            r.say(b.T(end - 1), report, turn, final=True)
            r.complete(b.T(end), turn, report, started=b.T(t0 + 1.5))
            self.rolls.append(r)
            b.paths['b2'] = r.path
            self.files[self.report_path(x)] = report
            self.files[os.path.join(self.R, 'out', 'mpd-B.md')] = report                     # `-o` writes the last message
            self.files[os.path.join(self.R, 'out', 'mpd-B.log')] = 'done\n'
