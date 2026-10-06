"""The contract scene: a debate folder (`talk/`, a guide and round folders) of a repository, the participant under test `S` (a sub-agent, or a `claude -p` run when a tag is in play),
a peer `P` started with it, far from it, or by a call nobody can tell, and what comes later (a group writing round 2, one participant alone, editors of round-1 reports), with the
orchestrator's conclusion document before or after it.

The scene is said by the structure axes (axes.py: `launch`, `wmethod`, `overlap`, `read`, `tag`, `later`, `final`, `life`) and by nothing else: what the participants are told is
the same few words in every case. plan.ctr_scene says once what the records hold; the builder writes it (and every file and its time are the plan's).
"""

import os

from . import plan
from .axes import digest
from .build import MODEL, prose, put
from .scene_aff import bgid, toolu
from .scene_sta import Sta

GUIDE_TEXT = '# Review of the gate policy\n\nEach participant writes its report below the round folder.\n'


class Ctr:
    def __init__(self, b):
        self.b, self.case, self.v, self.cid = b, b.case, b.case.v, b.case.id
        self.W = os.path.join(b.work, 'repo')
        os.makedirs(os.path.join(self.W, '.git'), exist_ok=True)
        put(os.path.join(self.W, '.git', 'HEAD'), 'ref: refs/heads/main\n')
        self.unit = os.path.join(self.W, plan.CTR_UNIT)
        s, p, guide, ruling = plan.ctr_paths(self.v)
        self.s_path, self.p_path, self.guide, self.ruling = (os.path.join(self.W, x) for x in (s, p, guide, ruling))

    # ---- one command of a participant's own record
    def bash(self, tr, t, cmd, tag):
        tid = toolu(self.b, tag)
        tr.tool(t, 'Bash', {'command': cmd, 'description': 'run'}, tid)
        tr.result(t + 0.5, tid, 'done')

    def tool(self, tr, t, name, inp, tag):
        tid = toolu(self.b, tag)
        tr.tool(t, name, inp, tid)
        tr.result(t + 0.5, tid, 'ok')

    # ---- what the participants do
    def tail_s(self):
        """What S does in its own record: reads the guide the way `read` says, saves its report the way `wmethod` says."""
        v = self.v

        def run(tr, t):
            if v['read'] == 'tool':
                self.tool(tr, t, 'Read', {'file_path': self.guide}, 's-read')
                t += 1.0
            elif v['read'] == 'cat':
                self.bash(tr, t, 'cat %s' % self.guide, 's-cat')
                t += 1.0
            wm, path = v['wmethod'], self.s_path
            if wm == 'write':
                self.tool(tr, t, 'Write', {'file_path': path, 'content': plan.REPORT_BODY}, 's-write')
            elif wm == 'edit':
                if plan.ctr_cli(v):                                                                       # (a `claude -p` run edits with MultiEdit, a sub-agent with Edit)
                    self.tool(tr, t, 'MultiEdit', {'file_path': path, 'edits': [{'old_string': 'old text', 'new_string': 'new text'}]}, 's-edit')
                else:
                    self.tool(tr, t, 'Edit', {'file_path': path, 'old_string': 'old text', 'new_string': 'new text'}, 's-edit')
            elif wm == 'redirect':
                self.bash(tr, t, 'echo "# S: first finding" > %s' % path, 's-redirect')
            elif wm == 'window':
                self.bash(tr, t, "cat > %s <<'EOF'\n# S: first finding\nEOF\nls %s" % (path, os.path.dirname(path)), 's-window')
            elif wm == 'stale':
                self.bash(tr, t, 'set -C; printf new > %s; true' % path, 's-stale')
            elif wm == 'python':
                self.bash(tr, t, "python3 - <<'PY'\nfrom pathlib import Path\nPath('%s').write_text('# S: first finding')\nPY" % path, 's-python')
            if wm != 'none':
                t += 1.0
            return t
        return run

    def tail_p(self):
        """P writes its report with the Write tool; with `overlap=agent` it also runs a command that overlaps the moment S's file appears."""
        v = self.v

        def run(tr, t):
            self.tool(tr, t, 'Write', {'file_path': self.p_path, 'content': plan.PARTNER_BODY}, 'p-write')
            if v['overlap'] == 'agent':
                self.bash(tr, self.b.T(plan.ctr_times(v)['S']['tail'] + (1.0 if v['read'] != 'none' else 0.0) + 0.1), 'git status', 'p-ls')
            return t + 1.0
        return run

    def tail_later(self, role, target, edit):
        def run(tr, t):
            if edit:
                self.tool(tr, t, 'Edit', {'file_path': target, 'old_string': 'old text', 'new_string': 'new text'}, 'later-' + role)
            else:
                self.tool(tr, t, 'Write', {'file_path': target, 'content': plan.REPORT_BODY}, 'later-' + role)
            return t + 1.0
        return run

    # ---- the case
    def build(self):
        b, v = self.b, self.v
        T = b.T
        self.sta = sta = Sta(b, W=self.W, flaw='none')
        msg1 = ('msg_' + digest(self.cid, 'launch-r1', n=12)) if v['launch'] in ('msg', 'call') else (False if v['launch'] == 'none' else None)
        times = plan.ctr_times(v)
        text = lambda role: prose(self.cid, role, 90)                                                   # noqa: E731  (the same few words whatever the axes say)
        env = {}
        room = ''
        if v['tag'] != 'none':
            room = 'BULLPEN_ROOM=%s ' % self.unit + ('BULLPEN_SEAT=r1/S ' if v['tag'] == 'seat' else '')
            env['BULLPEN_ROOM'] = self.unit
            if v['tag'] == 'seat':
                env['BULLPEN_SEAT'] = 'r1/S'
        claude = lambda role, prefix='': "%sclaude -p --model %s '%s'" % (prefix, MODEL, text(role))        # noqa: E731
        if v['launch'] == 'call':
            # one Bash call starts both runs (one call id, one message): P is a `claude -p` run as well
            shared = dict(tid=toolu(b, 'launch-call'), bgi=bgid(b, 'bg-call'), emit=True)
            cmd = 'cd %s && (%s & %s & wait)' % (self.W, claude('S', room), claude('P'))
            sta.cli_subject('S', text('S'), cmd, v['life'], 'just_ended', self.W, tail=self.tail_s(), off=times['S']['off'], msg=msg1, env_extra=env, shared=shared)
            sta.cli_subject('P', text('P'), cmd, 'normal_end', 'just_ended', self.W, tail=self.tail_p(), off=times['P']['off'], msg=msg1, shared=dict(shared, emit=False))
        else:
            # P: a report with the Write tool, long over when the board looks
            sta.sub_subject('P', 'P review', text('P'), 'normal_end', 'just_ended', tail=self.tail_p(), off=times['P']['off'], msg=msg1)
            # S: the participant under test
            if plan.ctr_cli(v):
                sta.cli_subject('S', text('S'), 'cd %s && %s' % (self.W, claude('S', room)), v['life'], 'just_ended', self.W, tail=self.tail_s(), off=times['S']['off'], msg=msg1, env_extra=env)
            else:
                sta.sub_subject('S', 'S review', text('S'), v['life'], 'just_ended', tail=self.tail_s(), off=times['S']['off'], msg=msg1)
        # the orchestrator's own command that overlaps the moment S's file appears
        if v['overlap'] == 'orch':
            at = T(times['S']['tail'] + (1.0 if v['read'] != 'none' else 0.0) + 0.1)
            tid = toolu(b, 'o-git')
            sta.O.tool(at, 'Bash', {'command': 'git status', 'description': 'look'}, tid)
            sta.O.result(at + 0.5, tid, 'clean')
        # what comes later
        later = v['later']
        if later in ('group', 'alone'):
            msg2 = ('msg_' + digest(self.cid, 'launch-later', n=12)) if later == 'group' else None
            for k, role in enumerate(['Q1', 'Q2'] if later == 'group' else ['Q']):
                sta.sub_subject(role, '%s round 2' % role, text(role), 'normal_end', 'just_ended', off=plan.CTR_LATER + 0.01 * k, msg=msg2,
                              tail=self.tail_later(role, os.path.join(self.unit, 'r2', role + '.md'), False))
        elif later == 'editors':
            wrote = v['wmethod'] in ('write', 'edit', 'redirect', 'window', 'python', 'stale')
            targets = [self.p_path, self.s_path if wrote else self.p_path]
            msg2 = 'msg_' + digest(self.cid, 'launch-later', n=12)
            for k, role in enumerate(['E1', 'E2']):
                sta.sub_subject(role, '%s fixes' % role, text(role), 'normal_end', 'just_ended', off=plan.CTR_LATER + 0.01 * k, msg=msg2, tail=self.tail_later(role, targets[k], True))
        # the conclusion of the orchestrator
        if v['final'] != 'none':
            at = T(plan.CTR_RULING[v['final']])
            tid = toolu(b, 'ruling')
            sta.O.tool(at, 'Write', {'file_path': self.ruling, 'content': '# Conclusion\n\nThe review is settled.\n'}, tid)
            sta.O.result(at + 0.5, tid, 'written')
        sta.now = T(plan.CTR_NOW)
        self.disk()
        sta.finish()
        b.meta.update(repo=self.W, unit=self.unit, report=self.s_path, roles=['S', 'P', 'Q1', 'Q2', 'Q', 'E1', 'E2'])
        return sta

    def disk(self):
        """The folder of the debate and the files the plan says are on disk, each at its time (a file nobody's record writes is older than every run)."""
        put(self.guide, GUIDE_TEXT)
        for d in ('r1', 'r2'):
            if d == 'r1' or self.v['later'] in ('group', 'alone'):
                os.makedirs(os.path.join(self.unit, d), exist_ok=True)

    def set_times(self):
        """Every file of the plan exists, with the time the plan gives it (called after the records are saved)."""
        b = self.b
        for rel, meta in plan.ctr_scene(self.v).F.files.items():
            path = os.path.join(self.W, rel)
            if not os.path.exists(path):
                put(path, '# x\n' if meta['size'] else '')
            os.utime(path, (b.T(meta['mtime']), b.T(meta['mtime'])))


def build_ctr(b):
    C = Ctr(b)
    C.build()
    C.set_times()
    return C
