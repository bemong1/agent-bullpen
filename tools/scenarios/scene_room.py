"""The room scene: several sub-agents of one orchestrator that work together in a folder (a meeting, an agenda, a debate ...) and the lookalikes that are no room.

What the scene holds is said by the axes (axes.py: `guide` is what the first instruction points at, `shape` where each participant's own file is, `talk` who messages whom,
`trees` whose participants they are); the words of the instruction (`word`, `lang`) and the way the participant's seat is named (`seatmark`) change the text only. Every
name, path and text is synthetic.
"""

import os
import shutil

from .axes import (ABOVE_DOC, ABOVE_TEXT, COMMON_DOCS, COMMON_TITLE, ROOM_ALIAS, ROOM_BUNDLE, ROOM_GUIDES, SEAT_LETTERS, room_code, room_disk, room_folder, room_guide, room_late_guide,
                   room_out, room_title, room_tree)
from .build import Transcript, agent_id_of, dump, prose, put
from .scene_aff import toolu
from .scene_sta import COORD, Sta

TOPICS = {'en': ('Schedule', 'Budget', 'Risk', 'Scope', 'Quality'), 'ko': ('일정', '예산', '위험', '범위', '품질')}
NOUN = {'en': {'debate': 'debate', 'meeting': 'meeting', 'mtg': 'sync-up', 'agenda': 'agenda session'},
        'ko': {'debate': '토론', 'meeting': '회의', 'mtg': '미팅', 'agenda': '아젠다 논의'}}
BODY = {'en': 'Topics:\n\n- schedule\n- budget\n- risk\n\nEveryone keeps notes in a file of their own.\n',
        'ko': '안건:\n\n- 일정\n- 예산\n- 위험\n\n각자 자기 파일에 노트를 남깁니다.\n'}
# a letter in the text that is no seat of the participant: three English lookalikes (one for each way a seat is named) and two Korean ones
OTHER = {'en': ('Compile the sample as C (not C++) before anything else. ', 'Book seat C on the train for the offsite before anything else. ',
                'Note that participant C of the user study asked for a dark theme. '),
         'ko': ('C(언어) 담당 팀이 만든 샘플을 먼저 확인하세요. ', 'C 담당자에게 문의 내용을 전달하세요. ')}
FILE_TEXT = '# Notes\n\nFirst point.\nSecond point.\n'
CODE_TEXT = 'print(1)\n'
# the introduction of an earlier instruction that is only shown for review, by language; and the read-only review that names its guide and file in plain words
CITE_INTRO = {'en': 'Here is an instruction that was given to a team earlier. It is shown only so that you can review its wording; do not carry it out',
              'ko': '아래는 앞서 한 팀에 내려간 지침입니다. 문구를 검토하라고 보여 드리는 것이니 실행하지 마세요'}
EXEC_INTRO = {'en': 'Execute these instructions:', 'ko': '다음 지시를 따르세요:'}                       # what leads into the instruction that is meant for the participant, when it sits in a code fence
CITE_SENTENCE = {'en': 'Read-only quote of an earlier instruction.', 'ko': '이전 지침의 읽기 전용 인용입니다.'}      # a sentence that says the lines after it are a quote, and that ends in a full stop
CITE_READONLY = {'en': 'This is a read-only review: do not create, change or run anything. The earlier instruction told the team to read `%s` and to write notes to `%s`; '
                       'check that its wording is clear and answer in your reply. ',
                 'ko': '이 작업은 읽기 전용 검토입니다. 파일을 만들거나 고치거나 실행하지 마세요. 앞선 지침은 팀에게 `%s`를 읽고 노트를 `%s`에 쓰라고 했습니다. '
                       '문구가 분명한지 검토해서 답으로만 알려주세요. '}


def mark_text(v, i, ko):
    """The words in front of the instruction that name the participant's seat (or only look as if they did)."""
    letter, other = SEAT_LETTERS[i], SEAT_LETTERS[(i + 1) % 5]
    topic = TOPICS['ko' if ko else 'en'][i]
    kind = v['seatmark']
    if kind == 'bracket':
        return '[ROOM-%s] ' % letter
    if kind == 'dam':
        return '당신은 %s 담당입니다. ' % letter
    if kind == 'dam_paren':
        return '당신은 %s(%s) 담당입니다. ' % (letter, topic)
    if kind == 'en_participant':
        return 'You are participant %s (%s). ' % (letter, topic)
    if kind == 'en_as':
        return 'Work as %s (%s). ' % (letter, topic)
    if kind == 'en_seat':
        return 'You hold seat %s. ' % letter
    if kind == 'quoted':
        return ('다른 지침의 첫 줄 예: "[ROOM-%s] 당신은 %s 담당입니다." 이는 예시일 뿐입니다. ' % (other, other)) if ko else \
            'Example of a first line some briefs use: "You are participant %s (%s)." That is only an example. ' % (other, TOPICS['en'][(i + 1) % 5])
    if kind == 'negated':
        return ('당신은 %s 담당이 아닙니다. ' % other) if ko else 'You are not participant %s. ' % other
    if kind == 'other':
        return OTHER['ko' if ko else 'en'][i % (2 if ko else 3)]
    return ''


class Room:
    def __init__(self, b):
        self.b, self.case, self.v, self.cid = b, b.case, b.case.v, b.case.id
        self.W = os.path.join(b.work, 'repo')
        os.makedirs(os.path.join(self.W, '.git'), exist_ok=True)
        put(os.path.join(self.W, '.git', 'HEAD'), 'ref: refs/heads/main\n')
        self.n = int(self.v['people'])
        self.life = 'running' if self.v['phase'] == 'working' else 'normal_end'
        self.page = [i for i in range(self.n) if room_tree(self.v, i) == 1]

    def abs(self, rel):
        return os.path.join(self.W, rel)

    def disk(self, rel):
        """Where the file is on disk: a path written with the link (`copy=link`) is a file below the bundle's real folder."""
        return os.path.join(self.W, room_disk(self.v, rel))

    def ref(self, rel):
        """How the instruction writes a path of the repository: absolute, relative to the repository top (where the participant works), or with `~`."""
        kind = self.v['ref']
        if kind == 'rel':
            return rel
        return '~' + self.abs(rel)[len(self.b.home):] if kind == 'tilde' else self.abs(rel)

    def role(self, i):
        return 'p%d' % (i + 1)

    def aid(self, i):
        return self.b.ids.get(self.role(i)) or self.b.ids.setdefault(self.role(i), agent_id_of(self.cid, self.role(i)))

    def peers(self, i):
        """The participant that participant i messages: the next one of its own orchestrator (a ring: with two of them they message each other)."""
        same = [j for j in range(self.n) if room_tree(self.v, j) == room_tree(self.v, i)]
        return same[(same.index(i) + 1) % len(same)] if len(same) > 1 else None

    # ------------------------------------------------------------------------------------------------ disk
    def guide_text(self, title):
        return '# %s\n\n%s\n' % (title, BODY[self.v['lang']])

    def write_disk(self):
        v, b = self.v, self.b
        folder = room_folder(v)
        t_guide = b.T(-1000)
        if v['bundle'] == 'root':
            put(self.abs('%s/brief.md' % ROOM_BUNDLE), '# The bundle\n\nEvery folder below is one meeting of it.\n', t_guide)
            if v['copy'] == 'link':
                os.symlink(self.abs(ROOM_BUNDLE), self.abs(ROOM_ALIAS))                     # the participants write their paths through this link
        if v['guide'] in ROOM_GUIDES:
            put(self.disk('%s/%s' % (folder, ROOM_GUIDES[v['guide']])), self.guide_text(room_title(v)), t_guide)
        elif v['guide'] in COMMON_DOCS:
            put(self.abs(COMMON_DOCS[v['guide']]), '# %s\n\nNotes for everybody who works on this repository.\n' % COMMON_TITLE[v['guide']], t_guide)
        elif v['guide'] == 'own':
            for i in range(self.n):
                put(self.abs(room_guide(v, i)[0]), '# Agenda for %s\n\n%s\n' % (SEAT_LETTERS[i], BODY[v['lang']]), t_guide)
        elif v['guide'] == 'late':
            put(self.abs(room_late_guide(v)), self.guide_text(room_title(dict(v, guide='agenda'))), t_guide)
        if v['shape'] == 'r1':
            os.makedirs(self.disk('%s/r1' % folder), exist_ok=True)                  # the first round has started
        if v['proof'] == 'told' or v['shape'] == 'none':
            self.write_above()
            return
        for i in range(self.n):
            path = room_out(v, i)
            if v['shape'] == 'scatter':
                put(self.abs('repos/svc_%s/.git/HEAD' % SEAT_LETTERS[i].lower()), 'ref: refs/heads/main\n')
                put(self.abs(path), CODE_TEXT, b.T(10))
            else:
                put(self.disk(path), FILE_TEXT, b.T(10))
            if room_code(v, i):
                put(self.abs(room_code(v, i)), CODE_TEXT, b.T(10))
        self.write_above()

    def write_above(self):
        """The document beside the room's folder in the bundle's folder (`above`), and the copy of the bundle in a linked worktree (`copy=worktree`)."""
        v, b = self.v, self.b
        if v['above'] != 'none':
            put(self.abs('%s/%s' % (ROOM_BUNDLE, ABOVE_DOC[v['above']])), ABOVE_TEXT[v['above']], b.T(-500) if v['above'] == 'early' else b.T(60))
        if v['copy'] == 'worktree':
            self.wt = os.path.join(b.work, 'wt')
            git = os.path.join(self.W, '.git', 'worktrees', 'wt')
            put(os.path.join(git, 'commondir'), '../..\n')
            put(os.path.join(self.wt, '.git'), 'gitdir: %s\n' % git)                      # a linked worktree of the repository: its `.git` is a file that points into the main one
            shutil.copytree(self.abs(ROOM_BUNDLE), os.path.join(self.wt, ROOM_BUNDLE), copy_function=shutil.copy2)

    # ------------------------------------------------------------------------------------------------ the text the participant is given
    def description(self, i):
        v = self.v
        topic = TOPICS['en'][i]
        return '%s %s notes' % (SEAT_LETTERS[i], topic) if v['seatmark'] == 'tag' else '%s notes' % topic

    def instruction(self, i):
        v = self.v
        ko = v['lang'] == 'ko'
        out = room_out(v, i)
        noun = NOUN['ko' if ko else 'en'].get(v['word'])
        text = mark_text(v, i, ko)
        if ko:
            text += ('당신은 %s의 참가자입니다. ' % noun) if noun else '당신은 이 작업의 여러 에이전트 중 한 명입니다. '
        else:
            text += ('You are a participant of the %s. ' % noun) if noun else 'You are one of several agents on this work. '
        guide, _ = room_guide(v, i)
        if v['cite'] != 'none':
            return text + self.cited(v, ko, guide, out) + prose(self.cid, 'p%d' % i, 80)
        head, text = text, ''                                          # `head` is addressed to the participant in any case; the rest may sit in a code fence
        if guide:
            text += ('`%s`를 읽고 따르세요. ' if ko else 'Read `%s` and follow it. ') % self.ref(guide)
        told = v['proof'] in ('told', 'both')
        if out is None:
            if v['talk'] == 'peer':
                text += '열린 항목은 다른 참가자와 메시지로 정리하고 파일은 만들지 마세요. ' if ko else 'Settle the open items with the other participants by message; write no file. '
            else:
                text += '답변으로만 알려주고 파일은 만들지 마세요. ' if ko else 'Answer in your reply only; write no file. '
        elif told:
            if v['shape'] == 'scatter':
                text += ('맡은 부분은 `%s`에 구현하세요. ' if ko else 'Implement your part in `%s`. ') % self.ref(out)
            elif v['shape'] == 'same':
                text += ('노트는 `%s`에 이어서 작성하세요. ' if ko else 'Add your notes to `%s`. ') % self.ref(out)
            else:
                text += ('노트는 `%s`에 작성하세요. ' if ko else 'Write your notes to `%s`. ') % self.ref(out)
        else:
            if v['shape'] == 'scatter':
                text += '맡은 부분은 자기 소스 파일에 구현하세요. ' if ko else 'Implement your part in a source file of your own. '
            else:
                text += '끝나면 노트를 파일로 저장하세요. ' if ko else 'When you are done, save your notes in a file. '
        code = room_code(v, i)
        if code:
            if told:
                text += ('맡은 단계의 코드는 `%s`를 고쳐 구현하세요. ' if ko else 'Implement your step by changing the code in `%s`. ') % self.ref(code)
            else:
                text += '맡은 단계의 코드는 소스 파일에 구현하세요. ' if ko else 'Implement your step in a source file of the code. '
        elif v['code'] == 'implied':
            text += '맡은 단계의 코드는 소스 파일에 구현하세요. ' if ko else 'Implement your step in a source file of the code. '      # no path is named and nothing is written
        if v['talk'] == 'peer' and out is not None:
            text += '다른 참가자에게 메시지로 의견을 알리세요. ' if ko else 'Tell the others your view by message. '
        if v['wrap'] == 'exec_fence':
            return head + EXEC_INTRO['ko' if ko else 'en'] + '\n\n```text\n' + text.strip().replace('. ', '.\n').replace('요. ', '요.\n') + '\n```\n\n' + prose(self.cid, 'p%d' % i, 80)
        return head + text + prose(self.cid, 'p%d' % i, 80)

    def cited(self, v, ko, guide, out):
        """The instruction of a participant that is only asked to review the words of an earlier instruction (which points at the guide and names the notes file): quoted in
        one line, in a code fence or a block quote, or as a read-only review. Nothing in it is meant for the participant to do."""
        lang = 'ko' if ko else 'en'
        g, o = self.ref(guide), self.ref(out)
        lines = [('`%s`를 읽고 따르세요.' if ko else 'Read `%s` and follow it.') % g, ('노트는 `%s`에 작성하세요.' if ko else 'Write your notes to `%s`.') % o]
        form = v['cite']
        if form == 'readonly':
            return CITE_READONLY[lang] % (g, o)
        if form == 'sentence':
            return '%s\n%s\n\n' % (CITE_SENTENCE[lang], '\n'.join(lines))
        intro = CITE_INTRO[lang]
        if form == 'inline':
            return '%s: "%s" ' % (intro, ' '.join(lines))
        if form == 'fence':
            return '%s:\n\n```text\n%s\n```\n\n' % (intro, '\n'.join(lines))
        return '%s:\n\n%s\n\n' % (intro, '\n'.join('> ' + x for x in lines))

    # ------------------------------------------------------------------------------------------------ the participants
    def timing(self, k):
        """(life, when the Agent call is made) of the k-th participant of the page. Overlapping runs start a second apart; sequential ones start ten minutes apart, each after the
        one before has finished (the last one may still be running)."""
        if self.v['rtime'] == 'quiet':                                      # every one is still running, though each has been quiet since the next one began
            return 'running', -300 - 600 * (len(self.page) - 1 - k)
        if self.v['rtime'] == 'sequential':
            last = len(self.page) - 1 - k
            return ('running' if self.v['phase'] == 'working' and last == 0 else 'normal_end'), -300 - 600 * last
        return self.life, -300 - k

    def tail(self, i):
        """What participant i does in its own record before it ends: reads the guide, tells the others its view, writes its own file."""
        v, b = self.v, self.b
        guide, on_disk = room_guide(v, i)
        out = room_out(v, i)

        def run(tr, t):
            if guide and on_disk:
                self.read(tr, t, self.abs(guide), 'rd-guide-%d' % i)
                t += 0.5
            if v['guide'] == 'late':
                late = room_late_guide(v)
                tr.coordinator(t, '%s Read `%s` and follow it.' % (COORD, self.ref(late)))
                self.read(tr, t + 0.3, self.abs(late), 'rd-late-%d' % i)
                t += 1
            to = self.peers(i) if v['talk'] == 'peer' else None
            if to is not None:
                tid = toolu(b, 'msg-%d' % i)
                tr.tool(t, 'SendMessage', {'to': self.aid(to), 'summary': 'my view', 'message': 'Here is my view on the open items.'}, tid)
                if v['delivery'] == 'failed':
                    tr.result(t + 0.3, tid, 'Error: the recipient is not running, nothing was delivered', is_error=True)
                else:
                    tr.result(t + 0.3, tid, 'sent')
                t += 0.5
            if out is not None and v['proof'] in ('wrote', 'both'):
                tid = toolu(b, 'wr-%d' % i)
                tr.tool(t, 'Write', {'file_path': self.abs(out), 'content': CODE_TEXT if v['shape'] == 'scatter' else FILE_TEXT}, tid)
                tr.result(t + 0.5, tid, 'written')
                t += 1
                if room_code(v, i):
                    tid = toolu(b, 'wr-code-%d' % i)
                    tr.tool(t, 'Write', {'file_path': self.abs(room_code(v, i)), 'content': CODE_TEXT}, tid)
                    tr.result(t + 0.5, tid, 'written')
                    t += 1
            if v['scratch'] == 'tmp':                                       # files kept for scratch work, in a folder outside the repository
                for name in ('repro_%s.py', 'draft_%s.md'):
                    tid = toolu(b, 'wr-%s-%d' % (name[:5], i))
                    tr.tool(t, 'Write', {'file_path': os.path.join(b.work, 'scratch', name % SEAT_LETTERS[i]), 'content': CODE_TEXT}, tid)
                    tr.result(t + 0.5, tid, 'written')
                    t += 1
            elif v['scratch'] == 'log':                                     # the output of a command saved in a log inside the repository
                tid = toolu(b, 'log-%d' % i)
                tr.tool(t, 'Bash', {'command': 'make test > %s' % self.abs('logs/%s.txt' % SEAT_LETTERS[i]), 'description': 'run the checks'}, tid)
                tr.result(t + 0.5, tid, 'ok')
                t += 1
            return t
        return run

    def read(self, tr, t, path, tag):
        tid = toolu(self.b, tag)
        tr.tool(t, 'Read', {'file_path': path}, tid)
        tr.result(t + 0.2, tid, 'text')

    def strangers(self, S):
        """The participants of another orchestrator: its record, its sub-agents (under its own session id), which run the same text and write in the same folder."""
        b, v = self.b, self.v
        other = [i for i in range(self.n) if room_tree(v, i) == 2]
        if not other:
            return
        O2 = b.transcript('orch2', self.W, 'cli')
        O2.prompt(b.T(-3000), 'Start the other work and keep me posted.', source='human')
        S.extra_tr.append(O2)
        base = os.path.join(os.path.dirname(O2.path), O2.sid, 'subagents')
        for i in other:
            aid = self.aid(i)
            tu = toolu(b, 'spawn-' + self.role(i))
            text = self.instruction(i)
            t0 = b.T(-300 - 10 * i)
            O2.tool(t0, 'Agent', {'description': self.description(i), 'prompt': text}, tu)
            O2.result(t0 + 0.3, tu, 'Async agent launched', extra={'isAsync': True, 'status': 'async_launched', 'agentId': aid, 'description': self.description(i), 'prompt': text,
                                                                    'outputFile': '/tmp/claude-synth/tasks/%s.output' % aid, 'canReadOutputFile': True})
            put(os.path.join(base, 'agent-%s.meta.json' % aid), dump({'agentType': 'general-purpose', 'description': self.description(i), 'requestNonInteractive': True,
                                                                       'requestShape': 'background', 'spawnDepth': 1, 'toolUseId': tu}))
            T = Transcript(os.path.join(base, 'agent-%s.jsonl' % aid), O2.sid, self.W, 'cli', side=True, agent=aid, cid=self.cid)
            T.prompt(t0 + 1, text, source='spawn')
            t = self.tail(i)(T, t0 + 6)
            T.tool(t + 0.5, 'Bash', {'command': 'ls', 'description': 'look'}, toolu(b, 'wk-' + self.role(i)))
            S.subs.append(T)
            b.meta.setdefault('strangers', []).append(self.role(i))

    def build(self):
        b, v = self.b, self.v
        self.write_disk()
        self.S = S = Sta(b, W=self.W, flaw='none')
        b.meta['repo'] = self.W
        b.meta['unit'] = self.abs(room_folder(v))
        b.meta['guide'] = self.abs(room_guide(v, 0)[0]) if v['guide'] in ROOM_GUIDES else None
        b.meta['page'] = [self.role(i) for i in self.page]
        if v['talk'] == 'orch':
            for k, i in enumerate(self.page):                                      # the orchestrator tells each of them something; they tell each other nothing
                tid = toolu(b, 'orch-msg-%d' % i)
                at = -150 + k if v['rtime'] == 'overlap' else self.timing(k)[1] + 3   # while that participant runs
                S.O.tool(b.T(at), 'SendMessage', {'to': self.aid(i), 'summary': 'status', 'message': 'Please keep it short.'}, tid)
                S.O.result(b.T(at + 0.5), tid, 'sent')
        if v['copy'] == 'worktree':                                              # another agent of the orchestrator works in the linked worktree, where the copy of the bundle is
            here, S.W = S.W, self.wt
            S.sub_subject('bystander', 'look around', 'Please look around this checkout and report in plain words.', self.life, 'just_ended', off=-400)
            S.W = here
        for k, i in enumerate(self.page):
            life, off = self.timing(k)
            S.sub_subject(self.role(i), self.description(i), self.instruction(i), life, 'just_ended', tail=self.tail(i), off=off)
        self.strangers(S)
        S.finish()
        return S


def build_room(b):
    R = Room(b)
    R.build()
    return R
