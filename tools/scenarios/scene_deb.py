"""The debate scene: a repository with a debate folder on disk and a participant (a sub-agent, a `claude -p` child or a Codex thread) that is told where its
report goes in one of ten ways, plays one of eight roles (writer, reader, someone who only quotes the path ...) and lives one life.

Also the coupling scene (`cpl`): the same debate with a `claude -p` participant launched by the main session, a sub-agent or a child, crossing launcher x life x
report path x file state x OS. Every name, path and text is synthetic.
"""

import os

from .axes import ALIAS_DIR, EDIT_DIR, ROUND_DIR, aux_file_exists, wrote_itself
from .build import MODEL, proc, prose, put, session_file
from .scene_aff import toolu
from .scene_sta import Sta

ROLES = {'A': 'Development flow', 'B': 'Gate design', 'C': 'GitHub operations'}


def brief_text(rd, flat=False, declared=True, topics=None):
    if flat:
        decl = ('Reviewers and their result files (declared): `sol.md` (reviewer sol) and `opus.md` (reviewer opus). A reviewer may add `sol-final3.md` for a final pass.\n'
                if declared else 'Review the release notes once and report what you find.\n')
        return '# Release notes review\n\n%s\n' % decl
    out = '# Review of the gate policy\n\n'
    if topics:
        out += '| Topic | Folder | Depends on | Final |\n|---|---|---|---|\n' + ''.join('| %s | `%s/` | — | |\n' % (n, f) for n, f in topics) + '\n'
    out += ''.join('**%s — %s**\n' % (k, v) for k, v in ROLES.items())
    out += '\nReports are written to `%s/<id>.md`. The orchestrator writes the final text.\n' % rd
    return out


def edit_brief_text():
    """The guide of an editing job: findings to fix, named by bold numbers (and a table of two sources). No participant, no reviewer, no report path, no round folder."""
    return ('# Edit pass 2: apply the re-check\n\nFix exactly these findings in the final text, in this order. Change nothing else.\n\n'
            '- **C-12**: the gate section names two owners; keep one.\n'
            '- **X-1**: the retry limit differs between the table and the text.\n'
            '- **C-30·C-31**: the two examples in section 4 use the old names.\n\n'
            '| Item | Finding | Fix |\n|---|---|---|\n'
            '| 1 | **Codex X-2 · Claude C-36** | Use one spelling of the error code. |\n'
            '| 2 | **X-2 · C-36** | Same as item 1. |\n\n'
            'When done, list what changed in `names.md`.\n')


class Deb:
    def __init__(self, b, coupling=None):
        self.b, self.case, self.v, self.cid = b, b.case, b.case.v, b.case.id
        self.coupling = coupling                          # None, or the spawner of a cpl case
        self.W = os.path.join(b.work, 'repo')
        os.makedirs(os.path.join(self.W, '.git'), exist_ok=True)       # the debate folders sit in a repository: the list is made from the repository top
        put(os.path.join(self.W, '.git', 'HEAD'), 'ref: refs/heads/main\n')
        v = self.v
        self.rdn = ROUND_DIR[v['rdir']]
        s = v['structure']
        self.root = None
        if s == 'topics':
            self.root = os.path.join(self.W, 'docs', 'rev')
            self.unit, self.unit2 = os.path.join(self.root, 't1'), os.path.join(self.root, 't2')
        elif s == 'deep3':
            self.unit = os.path.join(self.W, 'docs', 'records', '2026', 'rev')
        elif s == 'dot':
            self.unit = os.path.join(self.W, '.records', 'rev')
        else:
            self.unit = os.path.join(self.W, 'docs', 'rev')
        if v['homonym'] == 'two':
            self.unit2 = os.path.join(self.W, 'docs', 'rev2')
        self.guide = 'README.md' if s == 'readme' else 'brief.md'
        n = v['nstyle']
        self.stem = {'plain': 'B', 'named': 'B_gate', 'lower': 'b', 'numbered': 'opus1', 'collide': 'B_gate'}[n]
        self.pstem = {'numbered': 'astra1', 'named': 'A_flow'}.get(n, 'A')
        if s == 'flat':
            self.stem, self.pstem = ('sol', 'opus') if n == 'plain' else ('sol-final3', 'opus')
        self.letter = 'B'

    # ------------------------------------------------------------------------------------------------ disk
    def rpath(self, base, rnd, stem):
        return os.path.join(base, self.rdn % rnd, stem + '.md')

    def report_path(self):
        if self.v['structure'] == 'flat':
            return os.path.join(self.unit, self.stem + '.md')
        return self.rpath(self.unit, 1, self.stem)

    def aux_path(self):
        """The file `-o` / `>` of the launch writes next to the report (the last message of the run), inside the round folder."""
        return os.path.join(self.unit, self.rdn % 1, self.stem + '_last.md')

    def alias_path(self):
        """The file `-o` / `>` of the launch writes under the report's own name in the other spelling of the round folder (`aux=alias`)."""
        return os.path.join(self.unit, ALIAS_DIR, self.stem + '.md')

    def launch_ref(self, path, cwd):
        """How the launch command names a file of its own: the way the instruction names the report (a path relative to the folder it runs in, or absolute)."""
        return os.path.relpath(path, cwd) if self.v['rpath'] in ('short', 'folder', 'dotdot') else path

    def write_disk(self):
        v = self.v
        flat = v['structure'] == 'flat'
        guide = brief_text(self.rdn % 1, flat=flat, declared=v['decl'] == 'yes', topics=[('Gate policy', 't1'), ('Release flow', 't2')] if self.root else None)
        units = [self.unit] + ([self.unit2] if v['structure'] == 'topics' or v['homonym'] == 'two' else [])
        if self.root:
            put(os.path.join(self.root, 'brief.md'), brief_text(self.rdn % 1, topics=[('Gate policy', 't1'), ('Release flow', 't2')]))
        if v['edits'] == 'beside':
            put(os.path.join(self.root, EDIT_DIR, 'brief.md'), edit_brief_text())
            put(os.path.join(self.root, EDIT_DIR, 'names.md'), '# Names\n\nThe changes of the pass.\n')
        for u in units:
            put(os.path.join(u, self.guide), guide)
            if not flat:
                os.makedirs(os.path.join(u, self.rdn % 1), exist_ok=True)
                os.makedirs(os.path.join(u, self.rdn % 2), exist_ok=True)
        partner = os.path.join(self.unit, self.pstem + '.md') if flat else self.rpath(self.unit, 1, self.pstem)
        put(partner, '# Partner report\n\nThe flow is fine.\n')
        if v['homonym'] == 'two':
            put(os.path.join(self.unit2, self.pstem + '.md') if flat else self.rpath(self.unit2, 1, self.pstem), '# Partner report\n\nOther debate.\n')
        mine = self.report_path()
        if v['role'] in ('writer', 'reader', 'ref_reader', 'absent'):
            if v['fstate'] == 'written' or v['role'] in ('reader', 'ref_reader', 'absent'):
                put(mine, '# Report\n\nFirst finding.\nSecond finding.\n')
            elif v['fstate'] == 'empty':
                put(mine, '')
        if v['nstyle'] == 'collide':
            for other in ('B.md', 'b.md'):
                put(os.path.join(self.unit, self.rdn % 1, other), '# Another file\n\nSame letter, different file.\n')
        if v['rdir'] == 'both' and v['aux'] == 'alias':
            # the folder of the other spelling is there too; the launch writes the report's own name into it (a redirect creates the file empty and the output lands when the run ends)
            os.makedirs(os.path.join(self.unit, ALIAS_DIR), exist_ok=True)
            if aux_file_exists(v):
                put(self.alias_path(), 'The last message of the run.\n' if v['life'] not in ('running', 'stalled_silent') else '')
        elif v['rdir'] == 'both':
            # the folder of the other spelling is there too, and holds a file of the same name that somebody else wrote
            put(os.path.join(self.unit, 'r01', self.stem + '.md'), '# Someone else\n\nA different file in the other round folder.\n')
        if v['rpath'] == 'dash_o_aux' and (v['kind'] == 'cli' or v['life'] not in ('running', 'stalled_silent')):
            put(self.aux_path(), 'The last message of the run.\n' if v['life'] not in ('running', 'stalled_silent') else '')       # `>` creates it empty; `-o` writes it when the run ends

    # ------------------------------------------------------------------------------------------------ the text the participant is given
    def agent_cwd(self):
        v = self.v
        if v['homonym'] == 'two':
            return self.unit
        return {'short': self.unit, 'dotdot': os.path.join(self.W, 'docs', 'other')}.get(v['rpath'], self.W)

    def orch_cwd(self):
        v = self.v
        if v['homonym'] == 'two':
            return self.unit2
        if v['kind'] == 'sub' or self.coupling == 'sub':
            return self.agent_cwd()                      # a sub-agent works where the main session works
        return self.W

    def form(self, path, cwd):
        """How the report path is written in the instruction."""
        rp, home = self.v['rpath'], self.b.home
        if rp in ('abs', 'dash_o', 'dash_o_last', 'dash_o_aux', 'redirect', 'instr_only', 'var'):
            return path
        if rp == 'tilde':
            return '~' + path[len(home):]
        if rp in ('short', 'folder', 'dotdot'):
            return os.path.relpath(path, cwd if rp != 'folder' else self.W)
        return '$REPORT_DIR/' + os.path.relpath(path, self.unit)            # var_ext (a sub-agent's instruction): the variable is never defined

    def instruction(self):
        v = self.v
        rp, role, marker = v['rpath'], v['role'], v['marker']
        kind_ = 'cli' if self.coupling else v['kind']
        ko = v['lang'] == 'ko'
        cwd = self.agent_cwd()
        guide = os.path.join(self.unit, self.guide)
        if rp in ('tilde', 'folder', 'short', 'dotdot'):
            gref = self.form(guide, cwd)
        else:
            gref = guide
        mine = self.report_path()
        pref = self.form(mine, cwd)
        if marker == 'own':
            who = ('[REVIEW-%s] 당신은 검토 토론의 참가자 %s(%s)입니다. ' if ko else '[REVIEW-%s] You are participant %s (%s) of the review debate. ') % (self.letter, self.letter, ROLES['B'])
        else:
            who = '당신은 검토 토론의 참가자입니다. ' if ko else 'You are a participant of the review debate. '
        if v['marker'] == 'cross':
            who += ('다른 주제는 `%s`에 설명되어 있습니다. ' if ko else 'The other topic is described in `%s`. ') % os.path.join(self.unit2, 'brief.md')
        in_command = kind_ != 'sub' and rp in ('dash_o', 'redirect', 'var', 'var_ext')
        if role in ('writer', 'rival'):
            if rp == 'instr_only' or in_command:
                return who + ('`%s`를 읽고 따르세요. 1라운드 결과는 지침에 적힌 위치에 작성하세요. ' if ko else 'Read `%s` and follow it. Write your round-1 result where the brief says. ') % gref + \
                    prose(self.cid, 'task', 80)
            return who + ('`%s`를 읽고 따르세요. 1라운드 결과를 `%s`에 작성하세요. ' if ko else 'Read `%s` and follow it. Write your round-1 result to `%s`. ') % (gref, pref) + \
                prose(self.cid, 'task', 60)
        if role == 'reader':
            other = self.form(mine, cwd)
            return ('`%s`와 `%s`를 읽고 보고서를 검토해서 답으로만 알려주세요. 파일은 쓰지 마세요. ' if ko else
                    'Read `%s` and `%s`, then review the report in your answer. Do not write any file. ') % (gref, other) + prose(self.cid, 'task', 40)
        if role == 'ref_reader':
            # the report is only named (no verb of writing, nothing about a result to write): a reader that asks for a check, in the shapes people write it
            other = self.form(mine, cwd)
            return ('대상 파일: `%s`. 이 파일의 주장을 검증해서 답으로만 알려줘. ' if ko else 'The round-1 report of B: `%s`. Summarise it in your answer. Do not write any file. ') % other + \
                prose(self.cid, 'task', 40)
        if role == 'quoter':
            text = (('검토 폴더에서 보고서를 찾는 방법을 문서로 정리해 주세요. 사람들이 주는 지시의 예: “결과를 `%s`에 작성하세요”. 구성은 `%s`를 보세요. ') if ko else
                    ('Please document how reports are found in a review folder. Examples of instructions people give: "write the result to `%s`". See `%s` for the layout. ')) % (pref, gref)
            if marker == 'quoted':
                text += ('첫 줄 예: “[REVIEW-B] 당신은 B 담당(게이트 설계) 참가자입니다.” ' if ko else
                         'An example of a first line: "[REVIEW-B] You are participant B (Gate design) of the review debate." ')
            return text + prose(self.cid, 'task', 40)
        if role == 'negator':
            return ('`%s`는 작성할 필요가 없습니다. 채팅으로만 답하세요. 배경은 `%s`에 있습니다. ' if ko else 'You do not need to write `%s`; answer in chat only. Background is in `%s`. ') % (pref, gref) + \
                prose(self.cid, 'task', 40)
        if role == 'ghost':
            ghost = os.path.join(self.W, 'docs', 'ghost')
            return ('결과를 `%s/%s/B.md`에 작성하세요. 지침은 `%s/brief.md`입니다. ' if ko else 'Write the result to `%s/%s/B.md`. The brief is `%s/brief.md`. ') % (ghost, self.rdn % 1, ghost) + \
                prose(self.cid, 'task', 40)
        return ('검토 폴더를 둘러보고 찾은 것을 요약해 주세요. ' if ko else 'Look around the review folder and summarise what you find. ') + prose(self.cid, 'task', 40)       # tag_only / failed_write

    def description(self):
        v = self.v
        if v['kind'] != 'sub' and self.coupling is None:
            return None
        if v['role'] in ('reader', 'ref_reader'):
            return 'b check' if v['nstyle'] == 'lower' else 'B check'
        if v['role'] in ('tag_only', 'failed_write'):
            return 'B survey'
        return {'numbered': 'opus-1 review'}.get(v['nstyle'], 'B gate review')

    # ------------------------------------------------------------------------------------------------ the participants
    def partners(self, S):
        """The other participant (seat A), and, for a reader, the writer of seat B whose report is read."""
        b, v = self.b, self.v
        if v['role'] == 'absent':
            return
        t = b.T(-900)
        if v['role'] == 'rival':
            R, rid, ru = S.make_sub('rival', 'B gate review', -320, prompt=self.instruction())
            R.tool(b.T(-300), 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'rival-wk'))
        P, aid, tu = S.make_sub('partner', 'A flow review', -950, prompt='Review the flow; write your report to `%s`.' % self.rpath(self.unit, 1, self.pstem)
                                if v['structure'] != 'flat' else 'Review the notes.')
        path = self.rpath(self.unit, 1, self.pstem) if v['structure'] != 'flat' else os.path.join(self.unit, self.pstem + '.md')
        ptid = toolu(b, 'partner-write')
        P.tool(t, 'Write', {'file_path': path, 'content': '# Partner report\n'}, ptid)
        P.result(t + 1, ptid, 'written')
        P.say(t + 5, 'Report written.', 'end_turn')
        S.notify(S.O, t + 6, aid, tu, 'completed', 'Agent "A flow review" finished')
        if v['role'] in ('reader', 'ref_reader'):
            Q, qid, qu = S.make_sub('writerB', 'B gate review', -940, prompt='Write your report to `%s`.' % self.report_path())
            qtid = toolu(b, 'writerB-write')
            Q.tool(t + 10, 'Write', {'file_path': self.report_path(), 'content': '# B report\n'}, qtid)
            Q.result(t + 11, qtid, 'written')
            Q.say(t + 15, 'Report written.', 'end_turn')
            S.notify(S.O, t + 16, qid, qu, 'completed', 'Agent "B gate review" finished')

    def subject(self, S):
        """The participant itself, living `life`."""
        b, v = self.b, self.v
        text = self.instruction()
        role, kind = v['role'], v['kind']
        life = v['life']
        mine = self.report_path()
        rp = v['rpath']
        wrote = wrote_itself(v)                                                              # the agent itself wrote the file (a Write call / a patch / a Bash command)
        cwd = self.agent_cwd()

        def tail(tr, t):
            """What the participant does in its own record before it ends."""
            if role in ('reader', 'ref_reader'):
                tr.tool(t, 'Read', {'file_path': os.path.join(self.unit, self.guide)}, toolu(b, 'rd-guide'))
                tr.result(t + 0.2, toolu(b, 'rd-guide'), 'guide')
                tr.tool(t + 0.4, 'Read', {'file_path': mine}, toolu(b, 'rd-report'))
                tr.result(t + 0.6, toolu(b, 'rd-report'), 'report')
                if v['wmode'] != 'tool':                                  # it also keeps a note of its own: a command that names the report and writes somewhere else
                    self.bash_write(tr, t + 0.8, self.bash_note(self.form(mine, cwd)), toolu(b, 'rd-note'), ok=True)
                    return t + 1.5
                return t + 1
            if role == 'quoter' and v['wmode'] != 'tool':             # it writes its document, and the report's path is only in the text of the command
                self.bash_write(tr, t, self.bash_quote(self.form(mine, cwd)), toolu(b, 'qt-note'), ok=True)
                return t + 1
            if role == 'tag_only':
                tr.tool(t, 'Read', {'file_path': os.path.join(self.unit, self.guide)}, toolu(b, 'rd-guide'))
                tr.result(t + 0.2, toolu(b, 'rd-guide'), 'guide')
                if v['wmode'] != 'tool':                                  # its tag is the only thing that says B: a command that only quotes the report's path must not add a write
                    self.bash_write(tr, t + 0.5, self.bash_quote(self.form(mine, cwd)), toolu(b, 'qt-note'), ok=True)
                    return t + 1.5
                return t + 1
            if role == 'failed_write':
                target = self.rpath(self.unit, 1, 'B')
                if v['wmode'] != 'tool':
                    self.bash_write(tr, t, self.bash_command(v['wmode'], target), toolu(b, 'wr-fail'), ok=False)
                    return t + 1
                tr.tool(t, 'Write', {'file_path': target, 'content': '# Report\n'}, toolu(b, 'wr-fail'))
                tr.result(t + 0.5, toolu(b, 'wr-fail'), 'permission denied', is_error=True)
                return t + 1
            if wrote:
                if v['wmode'] != 'tool':
                    self.bash_write(tr, t, self.bash_command(v['wmode'], self.launch_ref(mine, cwd)), toolu(b, 'wr-ok'), ok=True)
                    return t + 1
                tr.tool(t, 'Write', {'file_path': mine, 'content': '# Report\n\nFirst finding.\n'}, toolu(b, 'wr-ok'))
                tr.result(t + 0.5, toolu(b, 'wr-ok'), 'written')
                return t + 1
            return t

        desc = self.description()
        if kind == 'sub' and self.coupling is None:
            S.sub_subject('child', desc, text, life, 'just_ended', tail=tail)
        else:
            rp = v['rpath']
            rel = os.path.relpath(mine, self.unit)
            pre = 'D=%s; ' % self.unit if rp == 'var' else ''
            if kind == 'codex':
                o = {'dash_o': ' -o %s' % mine, 'dash_o_last': ' -o %s' % os.path.join(b.scratch, 'B_last.md'), 'dash_o_aux': ' -o %s' % self.aux_path(),
                     'var': ' -o "$D/%s"' % rel, 'var_ext': ' -o "$REPORT_DIR/%s"' % rel}.get(rp, '')
                if v['aux'] == 'alias':
                    o = ' -o %s' % self.launch_ref(self.alias_path(), cwd)
                cmd = "%scd %s && codex exec -m gpt-6.1-sol%s '%s'" % (pre, cwd, o, text)
                extra = [(5, {'type': 'item_completed', 'item': {'type': 'FileChange', 'changes': {mine: {}}}})] if wrote else []
                if role in ('reader', 'ref_reader'):                   # Codex says what it read through the parsed command of a finished command execution
                    for i, path in enumerate((os.path.join(self.unit, self.guide), mine)):
                        extra.append((1.0 + i, {'type': 'item_completed', 'item': {
                            'type': 'CommandExecution', 'cwd': 'file://' + cwd, 'command': 'cat ' + path,
                            'parsed_cmd': [{'type': 'read', 'path': path, 'name': os.path.basename(path), 'cmd': 'cat ' + path}]}}))
                S.codex_subject('child', text, cmd, life, 'just_ended', cwd, extra=extra or None)
            else:
                out = {'redirect': ' > %s' % mine, 'dash_o_aux': ' > %s' % self.aux_path(), 'var': ' > "$D/%s"' % rel, 'var_ext': ' > "$REPORT_DIR/%s"' % rel}.get(rp, '')
                if v['aux'] == 'alias':
                    out = ' > %s' % self.launch_ref(self.alias_path(), cwd)
                cmd = "%scd %s && claude -p --model %s '%s'%s" % (pre, cwd, MODEL, text, out)
                launcher, tree = self.launcher_for()
                S.cli_subject('child', text, cmd, life, 'just_ended', cwd, tail=tail, launcher=launcher, tree=tree)

    # ------------------------------------------------------------------------------------------------ Bash commands of the participant's own record
    @staticmethod
    def bash_command(mode, target):
        """The Bash command that writes a report to `target` the way `mode` says: a redirect, `tee`, or a heredoc."""
        if mode == 'redirect':
            return 'echo "# Report: first finding" > %s' % target
        if mode == 'tee':
            return 'echo "# Report: first finding" | tee %s' % target
        return "cat > %s <<'EOF'\n# Report\n\nFirst finding.\nEOF" % target

    def bash_note(self, report):
        """A command that names the report and writes a different file (a count, a note): it reads the report, it does not write it."""
        note = os.path.join(self.b.scratch, 'count.txt')
        mode = self.v['wmode']
        if mode == 'redirect':
            return 'wc -l %s > %s' % (report, note)
        if mode == 'tee':
            return 'wc -l %s | tee %s' % (report, note)
        return "cat > %s <<'EOF'\nNotes on %s\nEOF" % (os.path.join(self.b.scratch, 'notes.md'), report)

    def bash_quote(self, report):
        """A command that writes a document of its own and only quotes how a report is written: the redirect or `tee` to the report is text inside quotes or inside the
        body of a heredoc, so it does nothing."""
        doc = os.path.join(self.b.scratch, 'doc.md')
        mode = self.v['wmode']
        if mode == 'redirect':
            return 'echo "example: run the tool > %s" > %s' % (report, doc)
        if mode == 'tee':
            return 'echo "example: run the tool | tee %s" | tee %s' % (report, doc)
        return "cat > %s <<'DOC'\nExample: run the tool > %s\nDOC" % (doc, report)

    def bash_write(self, tr, t, command, tid, ok):
        """The Bash call and its result (a failed one carries the error flag and the shell's complaint)."""
        tr.tool(t, 'Bash', {'command': command, 'description': 'write the report'}, tid)
        if ok:
            tr.result(t + 0.5, tid, 'done')
        else:
            tr.result(t + 0.5, tid, 'bash: Permission denied', is_error=True)

    def launcher_for(self):
        """(record with the launching call, (tree session id, tree pid, shell pid)) of a `claude -p` participant."""
        b, S = self.b, self.S
        sp = self.coupling
        if sp in (None, 'main'):
            return None, None
        if sp == 'sub':
            L, aid, _ = S.make_sub('launcher', 'Launch helper', -400)
            return L, (b.sid('orch'), 100, 101)
        K = b.transcript('mid', self.agent_cwd(), 'sdk-cli')
        ktext = prose(self.cid, 'mid', 160)
        S.O.bash(b.T(-100), 'cd %s && claude -p --model %s "%s"' % (self.agent_cwd(), MODEL, ktext), toolu(b, 'mid-call'), end=b.T(1500), desc='launch mid')
        K.prompt(b.T(-98), ktext, source='sdk', turn=1)
        S.extra_tr.append(K)
        S.procs += [proc(101, 100, ['/bin/bash', '-c', 'eval run']),
                    proc(110, 101, ['claude', '-p', '--model', MODEL], env={'CLAUDE_CODE_SESSION_ID': b.sid('orch'), 'CLAUDE_PID': '100'},
                         session=session_file(b, 110, b.sid('mid'), self.agent_cwd(), 'sdk-cli', b.T(-98)))]
        return K, (b.sid('mid'), 110, 111)

    # ------------------------------------------------------------------------------------------------ the case
    def build(self):
        b, v = self.b, self.v
        self.write_disk()
        self.S = S = Sta(b, W=self.orch_cwd(), flaw='none')
        b.meta['unit'] = self.unit
        b.meta['unit2'] = getattr(self, 'unit2', None)
        b.meta['stem'] = self.stem
        b.meta['root'] = self.root
        b.meta['edit'] = os.path.join(self.root, EDIT_DIR) if v['edits'] == 'beside' else None       # the editing job beside the topics, if the case has one
        b.meta['repo'] = self.W
        b.meta['report'] = self.report_path()
        b.meta['stems'] = (self.pstem, self.stem)
        self.partners(S)
        if v['role'] != 'absent':
            self.subject(S)
        else:
            S.now = b.T(30)
        S.finish()
        for p in (b.meta['report'],):
            if os.path.exists(p) and 'end' in b.meta or os.path.exists(p):
                t = b.meta.get('t_end', b.T(10))
                os.utime(p, (t, t))
        return S


def build_deb(b):
    D = Deb(b)
    D.build()
    return D


def build_cpl(b):
    """Coupling: a `claude -p` participant of a debate, launched by `spawner`, with a life, a report path and a file state, on an OS."""
    v = b.case.v
    D = Deb(b, coupling=v['spawner'])
    D.v = dict(D.v)
    D.v.update(kind='cli', role='writer', nstyle='plain', structure='single')
    D.build()
    return D
