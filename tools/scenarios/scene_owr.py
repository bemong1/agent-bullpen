"""The orchestrator-writes scene: the orchestrator (a Claude session, a Codex TUI thread or a `codex exec` thread) makes a folder that may be a debate, with the Write tool, a patch,
a shell redirect with a heredoc, a `mkdir` of the round folder, or only says so (`echo "mkdir -p ..."`), or tries and fails. What the folder holds when the board looks (a brief.md
and a round folder, a README.md and a round folder, a lone brief.md, a brief that declares two result files, ...) is the shape; where it is (below the repository's docs, in a plain
folder, in a scratch folder, under the agent's own state folder, at the top of the repository, at its docs folder) is the site. A `claude -p` run may stand beside it: started with
`&` and gone, a record nobody started, or a participant the orchestrator started and told its report path.

The files exist in every case; what differs is whose they are. A write that failed, or only a command's words, leaves the shape somebody else made. The scene writes what exists on
disk and in the records; what the page should list is the oracle's business (the helpers of axes.py are the one place both agree).
"""

import os
import shutil

from .axes import OWR_DEBATE, OWR_GUIDE, OWR_ROUNDED, OWR_SITE_REL, owr_rounds_on_disk
from .build import Phase, proc, prose, put, session_file
from .scene_aff import toolu
from .scene_cxo import CHILD_START, CLAUDE_RUN, Cxo
from .scene_deb import brief_text

ONE_RESULT = '# Release notes review\n\nReviewers and their result files (declared): `sol.md` (reviewer sol).\n'
NOTES = '# Notes of the week\n\nThings to remember about the release.\n'
WRITE_AT = -2000.0               # the orchestrator makes the folder
LAUNCH_AT = 0.0                  # ... and starts the run beside it
LOOK_AT = 300.0                  # the board looks
ORCH_AT = -3000.0                # the orchestrator's own instruction
WT = 'wt'                        # the linked worktree of the repository where another agent works (`copy=worktree`)


def guide_text(shape):
    """The guide of the folder: what a brief.md or a README.md of that shape says."""
    if shape == 'declared2':
        return brief_text('r1', flat=True)
    if shape == 'brief_only':
        return brief_text('r1', flat=True, declared=False)
    if shape == 'declared1':
        return ONE_RESULT
    if shape == 'notes':
        return NOTES
    return brief_text('r1')


class Owr(Cxo):
    """Builds one `owr` case into the Built `b`."""

    def __init__(self, b):
        super().__init__(b)
        v = self.v
        self.D = os.path.join(b.home, OWR_SITE_REL[v['dsite']])           # the folder the orchestrator makes
        self.guide = os.path.join(self.D, OWR_GUIDE[v['dshape']])
        self.r1 = os.path.join(self.D, 'r1')
        self.text = guide_text(v['dshape'])
        self.root_text = 'Set up the review of the gate policy in %s and start its participants.' % self.D
        self.L = None                                                     # the orchestrator's records: a Transcript (Claude) or a Rollout (Codex)

    # ---- the orchestrator's own writes ----
    def shell(self, key, t, cmd, exit_code=0, text='ok'):
        """A shell call of the orchestrator that returns at once: a Bash call (Claude) or a command that ends (Codex). `exit_code` != 0: it failed."""
        b = self.b
        cid = toolu(b, key)
        if self.v['top'] == 'claude':
            self.L.bash(b.T(t), cmd, cid, end=b.T(t + 0.4), desc='Make the folder', is_error=exit_code != 0)
        else:
            self.L.shell(b.T(t), cmd, self.W, cid, self.turn_of(self.L.tid), end=b.T(t + 0.5), out_at=b.T(t + 0.55), exit_code=exit_code, text=text)

    def write_guide(self, key, t, ok=True):
        """The guide with the orchestrator's file tool: the Write tool (Claude; a failed Write has `is_error`) or a patch (Codex; a patch that failed leaves no FileChange, so a failed
        Codex write is a shell command that exits with an error: see make_folder)."""
        b = self.b
        cid = toolu(b, key)
        if self.v['top'] == 'claude':
            self.L.tool(b.T(t), 'Write', {'file_path': self.guide, 'content': self.text}, cid)
            self.L.result(b.T(t + 0.4), cid, 'File created successfully' if ok else 'Error: permission denied', is_error=not ok)
        else:
            self.L.file_change(b.T(t), self.guide, self.text, self.turn_of(self.L.tid))

    def redirect(self):
        """One command: the round folder (when the shape has one) and the guide through a heredoc."""
        mk = 'mkdir -p %s && ' % self.r1 if self.v['dshape'] in OWR_ROUNDED else ''
        return "%scat > %s <<'EOF'\n%sEOF" % (mk, self.guide, self.text)

    def make_folder(self):
        v = self.v
        ow, shape = v['ow'], v['dshape']
        t = WRITE_AT
        if ow in ('tool', 'patch'):
            self.write_guide('write-guide', t)
        elif ow == 'redirect':
            self.shell('redirect', t, self.redirect())
        elif ow == 'mkdir_only':
            self.shell('mkdir', t, 'mkdir -p %s' % (self.r1 if shape in OWR_DEBATE else self.D))
        elif ow == 'failed':
            if v['top'] == 'claude':
                self.write_guide('write-guide', t, ok=False)
            else:
                self.shell('redirect', t, self.redirect(), exit_code=1, text='cat: permission denied')
            if shape in OWR_ROUNDED:
                self.shell('mkdir', t + 5, 'mkdir -p %s' % self.r1, exit_code=1, text='mkdir: cannot create directory: permission denied')
        else:                                                              # words: the command only says what would make the folder
            self.shell('words', t, 'echo "mkdir -p %s" && printf \'%%s\\n\' "%s"' % (self.r1, self.guide))

    def write_disk(self):
        """The folder as it is when the board looks, whoever made it."""
        v, b = self.v, self.b
        put(self.guide, self.text, b.T(-1900))
        if owr_rounds_on_disk(v):
            os.makedirs(self.r1, exist_ok=True)
        else:
            os.makedirs(self.D, exist_ok=True)

    # ---- the run beside the folder ----
    def kid(self):
        """The `claude -p` run: started with `&` (no record), written by nobody's launch (a record, no link), or started by the orchestrator and told its report."""
        b, v = self.b, self.v
        kind = v['kid']
        if kind == 'none':
            return
        text = b.ids['kid_text'] = prose(self.cid, 'kid', 200)
        t0 = self.at(LAUNCH_AT + CHILD_START)
        if kind == 'died':
            self.launch(self.launch_cmd('cl', text, 'bg'), 'bg', None)       # the child dies with the call: nothing of it is recorded
            return
        if kind == 'unlinked':
            report = (os.path.join(self.r1, 'B.md'), 'Findings of B: all is in order.\n') if v['dshape'] in OWR_ROUNDED else None
            self.claude_run('kid', self.W, text, t0, True, report=report)
            return
        rpath = os.path.join(self.r1, 'A.md')
        text = b.ids['kid_text'] = '%s Write your report to %s.' % (text, rpath)
        self.launch(self.launch_cmd('cl', text, 'fg'), 'fg', t0 + CLAUDE_RUN + 0.1)
        self.claude_run('kid', self.W, text, t0, True, report=(rpath, 'Findings of A: all is in order.\n'))

    def launch(self, cmd, how, end):
        """The orchestrator's call that starts the run."""
        b = self.b
        if self.v['top'] == 'claude':
            self.L.bash(b.T(LAUNCH_AT), cmd, toolu(b, 'launch'), end=end if end is not None else b.T(LAUNCH_AT + 0.4), desc='Start participant A')
        else:
            self.call(self.L, self.at(LAUNCH_AT), cmd, 'launch', end, how, 'end')

    # ---- another agent in a linked worktree that holds a copy ----
    def worktree(self):
        """`copy=worktree`: a linked worktree of the repository where another agent of the page works, with a copy of the folder (made after the files are written)."""
        b, v = self.b, self.v
        self.wt = os.path.join(b.work, WT)
        git = os.path.join(self.W, '.git', 'worktrees', WT)
        put(os.path.join(git, 'commondir'), '../..\n')
        put(os.path.join(self.wt, '.git'), 'gitdir: %s\n' % git)
        here, self.W = self.W, self.wt
        try:
            if v['top'] == 'claude':
                self.claude_sub(self.L, 'bystander', self.at(-400), True)
            else:
                self.sub('bystander', self.L, self.at(-400), 's1', 'Atlas', 1, self.root_text, self.at(ORCH_AT))
        finally:
            self.W = here

    # ---- the case ----
    def build(self):
        b, v = self.b, self.v
        W = self.W
        b.meta['repo'] = W
        b.meta['page'] = 'claude' if v['top'] == 'claude' else 'codex'
        if v['top'] == 'claude':
            self.L = b.transcript('orch', W, 'cli')
            self.L.prompt(self.at(ORCH_AT), self.root_text, source='human')
            self.trs.append(self.L)
            base = [proc(100, 1, ['claude'], env={}, session=session_file(b, 100, self.L.sid, W, 'cli', self.at(ORCH_AT)))]
            main_path = self.L.path
        else:
            self.L = self.root('top', 'codex_exec' if v['top'] == 'cx_exec' else 'codex-tui', self.at(ORCH_AT), self.root_text)
            self.L.say(self.at(ORCH_AT + 4), 'Starting the work.', self.turn_of(self.L.tid))
            base = [self.codex_proc(self.L)]
            main_path = self.L.path
        self.make_folder()
        self.kid()
        if v['copy'] == 'worktree':
            self.worktree()
        self.write_disk()
        for p, text in self.files.items():
            put(p, text)
        for r in self.rolls:
            r.save()
        for tr in self.trs:
            tr.save()
        if v['copy'] == 'worktree':
            shutil.copytree(os.path.join(W, 'docs', 'talk'), os.path.join(self.wt, 'docs', 'talk'), copy_function=shutil.copy2)       # the copy, with the files as they are now
        b.main_path = main_path
        b.phases = [Phase(self.at(LOOK_AT), base, 0)]
