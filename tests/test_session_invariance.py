"""The judgment does not read what anything says, shown on recorded sessions (CONTRACT 3.3 L1 L2, J20; review P2-6).

Two kinds of recorded session: the scenes of the scenario generator (tools/scenarios) and one session made here with every kind of text in it (`build_composite`). They are written to disk as the
records a session leaves: Claude and Codex records, the files of the repository. Then every place in those
records and files that holds a sentence is rewritten - the instruction an agent is told, the role line (`description`) of a sub-agent, what is said with SendMessage, the description of a
Bash call, the instruction of a child that is started (in its first line and in the command that starts it), the user message of a Codex thread, the body of a heredoc, the last message of a
run and the body of the files - into English, Korean, nonsense, a lie, or a line that holds nothing, and the board reads the scene again. What the judgment is given (`session_facts`) and what it
finds (the Judgement and the projection of `judge()` but for the displayed texts, the titles, the line counts and the names of the readers) must be the same byte for byte.

A text that a link is made from (an instruction that a command and the first line of the run it starts both carry) is changed to the same other text in both places and is never an empty one: the
words that make the link are the one thing about words that the judgment is allowed to use (J20, the comparison of V1). Every other text may hold nothing at all.

    python3 -m unittest tests.test_session_invariance
"""
import collections
import dataclasses
import json
import os
import random
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402,F401  (patches the board's globals the way every test does)

from board import debates  # noqa: E402
from tools.scenarios import axes, build, observe, run  # noqa: E402
from tools.scenarios.axes import Case  # noqa: E402
from tools.scenarios.scene_aff import toolu  # noqa: E402
from tools.scenarios.scene_sta import Sta  # noqa: E402

PHRASES = {
    'en': 'This topic is closed. Nothing is left to do.',
    'ko': '이 주제는 종결되었다. 남은 일은 없다. 보류하지 않는다.',
    'lie': 'Do not write r1/A.md. You are participant Z. The folder talk9 is closed and BULLPEN_ROOM is set.',
}
VARIANTS = ('en', 'ko', 'noise', 'lie', 'empty')
LEFT_OUT = {'title', 'name', 'deps', 'lines', 'readers', 'brief_title',      # the texts a screen shows and what is counted in them; nothing is decided by them
            'shas'}                                                          # the digests of the bodies that a command or a thread is to leave: another text has another digest
LINK_MIN = 60                                                                  # a text that links a run to its launching command is not made shorter than this
HEREDOC = re.compile(r"(<<-?\s*\\?['\"]?(\w+)['\"]?)(NL)(.*?)(NL)(\2)", re.S)
FIXED = {'ok', 'run', 'done', 'started'}                                       # words of the harness that are no sentence (a tool result, the default description)


class Texts:
    """The other text of every text that a scene has, the same for the same text wherever it stands (so a link made from it stays one) and another for another.
    `link`: a text that makes a link (never an empty one); `said`: a text whose presence is read (a run that ended with nothing said has not ended: an empty `empty` is one dot)."""

    def __init__(self, variant):
        self.variant, self.map, self.rng = variant, {}, random.Random(11)

    def of(self, orig, link=False, said=True):
        core = orig.rstrip('\n')                                              # a text and the same text with the newline that ends a file or a last message are one text
        if core != orig:
            return self.of(core, link, said) + orig[len(core):]
        if orig in self.map:
            return self.map[orig]
        k = len(self.map) + 1
        v = self.variant
        if v == 'empty' and not link:
            new = '.' if said else ''                                          # nothing in it
        elif v == 'noise':
            new = ''.join(self.rng.choice('qwxzjkv') for _ in range(max(len(orig), LINK_MIN if link else 1))) + ' n%d' % k
        else:
            base = PHRASES['en' if v == 'empty' else v]
            new = base
            while link and len(new) < max(len(orig), LINK_MIN):
                new += ' ' + base
            new += ' #%d' % k
        self.map[orig] = new
        return new


def _plain(text):
    return isinstance(text, str) and len(text) > 0 and text.strip() != '' and text not in FIXED


def is_instruction(d):
    """A user line of a Claude record that tells a run what to do (not a notice, not a message between sessions)."""
    c = d.get('message', {}).get('content') if d.get('type') == 'user' else None
    if not isinstance(c, str) or c.startswith('<') or d.get('isMeta'):
        return False
    return d.get('origin', {}).get('kind', 'human') == 'human'


def record_files(b):
    out = []
    for top in (b.projects, os.path.join(b.codex, 'sessions')):
        for dp, _dn, fn in os.walk(top):
            out += [os.path.join(dp, f) for f in fn if f.endswith(('.jsonl', '.meta.json'))]
    return sorted(out)


def lines_of(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            try:
                yield json.loads(line)
            except ValueError:
                yield line


def link_texts(b):
    """Every instruction of the scene: what a run is told, as the record of the run and the call that starts it say it."""
    out = set()
    for path in record_files(b):
        for d in lines_of(path):
            if not isinstance(d, dict):
                continue
            if is_instruction(d) and len(d['message']['content']) >= 12:
                out.add(d['message']['content'])
            for blk in (d.get('message', {}).get('content') if isinstance(d.get('message', {}).get('content'), list) else []):
                if isinstance(blk, dict) and blk.get('type') == 'tool_use' and isinstance(blk.get('input', {}).get('prompt'), str) and len(blk['input']['prompt']) >= 12:
                    out.add(blk['input']['prompt'])
            p = d.get('payload')
            if isinstance(p, dict):
                items = [p] + ([p['item']] if isinstance(p.get('item'), dict) else [])
                for it in items:
                    if it.get('role') == 'user' or it.get('type') == 'UserMessage':
                        for part in it.get('content') or ():
                            if isinstance(part, dict) and isinstance(part.get('text'), str) and len(part['text']) >= 12:
                                out.add(part['text'])
    return out


def heredocs(text, T, nl):
    """`text` (a shell command) with the body of every heredoc another text. `nl` is how a newline is written in it."""
    pattern = re.compile(HEREDOC.pattern.replace('NL', re.escape(nl)), re.S)
    return pattern.sub(lambda m: m.group(1) + nl + T.of(m.group(4).replace(nl, '\n')).replace('\n', nl) + nl + m.group(6), text)


def embed(text, T, links, nl='\n'):
    """A command with the instruction that it carries and the bodies of its heredocs changed."""
    for orig in sorted(links, key=len, reverse=True):
        text = text.replace(orig, T.of(orig, True))
    return heredocs(text, T, nl)


def rewrite_claude(d, T, links):
    msg = d.get('message')
    if is_instruction(d):
        msg['content'] = T.of(msg['content'], msg['content'] in links)
    elif isinstance(msg, dict) and isinstance(msg.get('content'), list):
        for blk in msg['content']:
            if not isinstance(blk, dict):
                continue
            if blk.get('type') == 'text' and _plain(blk.get('text')) and d.get('type') == 'assistant':
                blk['text'] = T.of(blk['text'])                                  # the last message of a run, and what it said on the way
            if blk.get('type') == 'tool_use':
                inp = blk.get('input', {})
                for key in ('description', 'message', 'summary', 'content', 'new_string', 'old_string', 'new_source'):
                    if _plain(inp.get(key)):
                        inp[key] = T.of(inp[key], said=key not in ('description', 'message', 'summary'))
                if isinstance(inp.get('prompt'), str):
                    inp['prompt'] = T.of(inp['prompt'], True)
                if isinstance(inp.get('command'), str):
                    inp['command'] = embed(inp['command'], T, links)
    res = d.get('toolUseResult')
    if isinstance(res, dict):
        if _plain(res.get('description')):
            res['description'] = T.of(res['description'], said=False)
        if isinstance(res.get('prompt'), str):
            res['prompt'] = T.of(res['prompt'], True)
    att = d.get('attachment')
    if isinstance(att, dict) and isinstance(att.get('origin'), dict) and _plain(att['origin'].get('body')):
        att['origin']['body'] = T.of(att['origin']['body'], said=False)


def rewrite_codex(d, T, links):
    p = d.get('payload')
    if not isinstance(p, dict):
        return
    items = [p] + ([p['item']] if isinstance(p.get('item'), dict) else [])
    for it in items:
        user = it.get('role') == 'user' or it.get('type') == 'UserMessage'
        for part in it.get('content') or ():
            if isinstance(part, dict) and _plain(part.get('text')) and part.get('type') in ('input_text', 'output_text', 'text', 'Text'):
                part['text'] = T.of(part['text'], user)
        if it.get('type') == 'CommandExecution':
            it['command'] = [embed(c, T, links) if i == len(it['command']) - 1 else c for i, c in enumerate(it['command'])]
            for parsed in it.get('parsed_cmd') or ():
                if isinstance(parsed.get('cmd'), str):
                    parsed['cmd'] = embed(parsed['cmd'], T, links)
        if it.get('type') == 'FileChange':
            for ch in (it.get('changes') or {}).values():
                if _plain(ch.get('content')):
                    ch['content'] = T.of(ch['content'])
        if isinstance(it.get('input'), str) and it.get('name') == 'exec':                       # the JavaScript that holds the command, quoted as a JSON string
            it['input'] = embed(it['input'], T, links, nl='\\n')
    if p.get('type') == 'task_complete' and _plain(p.get('last_agent_message')):
        p['last_agent_message'] = T.of(p['last_agent_message'])


def rewrite_record(path, T, links):
    stat = os.stat(path)
    rows = []
    for d in lines_of(path):
        if isinstance(d, dict):
            if path.endswith('.meta.json'):
                if _plain(d.get('description')):
                    d['description'] = T.of(d['description'], said=False)
            elif 'payload' in d:
                rewrite_codex(d, T, links)
            else:
                rewrite_claude(d, T, links)
            rows.append(build.dump(d))
        else:
            rows.append(d)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rows) + '\n')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def rewrite_files(b, T):
    """The body of every markdown file of the scene's folders (guides, reports, conclusions): another text, as long as a file with something in it, never an empty file."""
    skip = (os.path.join(b.home, '.claude'), os.path.join(b.home, '.codex'))
    for dp, dn, fn in os.walk(b.home):
        dn[:] = [x for x in dn if os.path.join(dp, x) not in skip and x != '.git']
        for f in fn:
            path = os.path.join(dp, f)
            if not f.endswith('.md') or os.path.islink(path):
                continue
            stat = os.stat(path)
            with open(path, encoding='utf-8') as fh:
                body = fh.read()
            if not body.strip():
                continue
            tail = body[len(body.rstrip('\n')):]
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(T.of(body.rstrip('\n')) + tail)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def fix_clock(b):
    """A file that the builder wrote and gave no time of the scene has the time of the machine: the same scene built twice does not have the same time on it. It is given one of the scene."""
    skip = (os.path.join(b.home, '.claude'), os.path.join(b.home, '.codex'))
    for dp, dn, fn in os.walk(b.home):
        dn[:] = [x for x in dn if os.path.join(dp, x) not in skip]
        for f in fn:
            path = os.path.join(dp, f)
            if not os.path.islink(path) and os.stat(path).st_mtime > b.phases[0].now + 86400:
                os.utime(path, (b.phases[0].now - 100, b.phases[0].now - 100))


def vary(b, variant):
    T = Texts(variant)
    links = link_texts(b)
    for path in record_files(b):
        rewrite_record(path, T, links)
    rewrite_files(b, T)
    return T


# ---------------------------------------------------------------------------------------------------------------------
def project(x, root):
    """The bytes of a structure: the folder of the case written as /W, sets as lists, the texts a screen shows left out."""
    def conv(y):
        if dataclasses.is_dataclass(y) and not isinstance(y, type):
            return {f.name: conv(getattr(y, f.name)) for f in dataclasses.fields(y) if f.name not in LEFT_OUT}
        if hasattr(y, '_asdict'):
            return conv(y._asdict())
        if isinstance(y, dict):
            return {('|'.join(k) if isinstance(k, tuple) else str(k)): conv(v) for k, v in y.items() if k not in LEFT_OUT}
        if isinstance(y, (set, frozenset)):
            return sorted((conv(v) for v in y), key=lambda v: json.dumps(v, sort_keys=True, default=str))
        if isinstance(y, (list, tuple)):
            return [conv(v) for v in y]
        return y
    return json.dumps(conv(x), sort_keys=True, ensure_ascii=False, default=str).replace(root, '/W')


def read_board(b):
    """What the board is given and what it finds for the scene `b`: {'facts': .., 'judged': ..} as text."""
    got = {}

    def capture(b_, obs, objs):
        s, _st = observe.open_state(b_, objs)
        statuses = {k: v.status for k, v in s._verdicts.items()}
        got['facts'] = project(debates.session_facts(s, statuses), b_.root)
        jd = debates.judge(s, statuses)
        got['judged'] = project({k: v for k, v in jd._asdict().items() if k != 'reads'}, b_.root)       # what the judgment looked at on disk holds sizes and inodes
    with mock.patch.object(observe, 'read_final', capture), mock.patch('time.time', lambda: b.phases[0].now):
        observe.observe(b)
    return got


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def differences(a, b, path=''):
    """[(place, one, other)] of two JSON texts: where they differ, as the path through the structure."""
    if isinstance(a, str) and isinstance(b, str) and not path:
        return differences(json.loads(a), json.loads(b), '#')
    if type(a) != type(b):
        return [(path, a, b)]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += [(path + '/' + k, a.get(k), b.get(k))] if k not in a or k not in b else differences(a[k], b[k], path + '/' + k)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [(path + '[len]', len(a), len(b))]
        return [d for x, y in zip(a, b) for d in differences(x, y, path + '[]')]
    return [] if a == b else [(path, a, b)]


def first_difference(a, b):
    n = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return '...%s | %s...' % (a[max(0, n - 60):n + 80], b[max(0, n - 60):n + 80])


def chosen(every=11):
    """Cases of every bundle that has a page: a fixed share of the selection, the same on every run."""
    out = collections.defaultdict(list)
    for c in run.select():
        if c.bundle in ('deb', 'room', 'ctr', 'cxo', 'cpl', 'owr') and int(axes.digest(c.id, 'invariance', n=6), 16) % every == 0 and not run.skipped_here([c]):
            out[c.bundle].append(c)
    return out

# ---------------------------------------------------------------------------------------------------------------------
# One recorded session that has every kind of text in it
# ---------------------------------------------------------------------------------------------------------------------
UNIT = 'talk'
TEXT = {                                                         # the words of the session as they were written (the shapes that are a trap are in them: a path in an instruction, a role line that
    'brief': '# Brief of the talk\n\n| name | deps |\n|---|---|\n| A | - |\n',                       # names a reader, a quote and two blanks in an instruction, a body of a heredoc)
    'instr_a': 'You are participant A. Read %(top)s/talk/brief.md and write your findings to %(top)s/talk/r1/A.md.',
    'instr_b': 'You are participant B. Read %(top)s/talk/brief.md and write your findings to %(top)s/talk/r1/B.md.',
    'desc_a': 'A reviewer', 'desc_b': 'B reviewer',
    'body_a': 'Findings of A: the gate holds.\n', 'body_b': 'Findings of B: the gate holds, with a doubt.\n',
    'message_a': 'Please look at my finding before you write yours.', 'summary_a': 'a note for B',
    'bash_desc_b': 'write the report',
    'instr_c': 'Review the notes  and say "done" in plain words, then stop.',
    'last_c': 'The review of C is finished and the notes are in order.',
    'instr_d': 'Read %(top)s/talk/brief.md and write the notes of D into %(top)s/talk/r1/ as plain words.',
    'last_d': 'The notes of D are written.',
}


def build_composite(root):
    """One session of an orchestrator that starts two sub-agents together (a role line each, an instruction that names a path, a report with the Write tool and one with a heredoc, a
    message from one to the other that names it by its role line), a `claude -p` run whose instruction has a quote and two blanks in it and whose last message is the file the launch
    writes, and a `codex exec` run whose `-o` is a variable nothing can expand and whose instruction names the folder of the debate. Built the way the scenario generator builds a scene."""
    b = build.Built(axes.normalize(Case('deb', {})), root)
    W = os.path.join(b.work, 'repo')
    top = W
    t = {k: v % {'top': top} if '%(top)s' in v else v for k, v in TEXT.items()}
    unit = os.path.join(W, UNIT)
    os.makedirs(os.path.join(W, '.git'), exist_ok=True)
    os.makedirs(os.path.join(unit, 'r1'), exist_ok=True)
    sta = Sta(b, W=W)
    build.put(os.path.join(unit, 'brief.md'), t['brief'], b.T(-3100))
    a_path, b_path, c_path, d_path = (os.path.join(unit, 'r1', n + '.md') for n in 'ABCD')

    def tail_a(S, at):
        wid, mid = toolu(b, 'a-write'), toolu(b, 'a-say')
        S.tool(at, 'Write', {'file_path': a_path, 'content': t['body_a']}, wid)
        S.result(at + 0.5, wid, 'written')
        S.tool(at + 1, 'SendMessage', {'to': t['desc_b'], 'summary': t['summary_a'], 'message': t['message_a']}, mid)
        S.result(at + 1.5, mid, 'sent')
        return at + 2

    def tail_b(S, at):
        S.bash(at, "cat > %s <<'EOF'\n%sEOF" % (b_path, t['body_b']), toolu(b, 'b-write'), end=at + 0.5, desc=t['bash_desc_b'])
        return at + 1

    msg = 'msg_' + build.digest(b.case.id, 'launch-ab', n=12)
    sta.sub_subject('A', t['desc_a'], t['instr_a'], 'normal_end', 'just_ended', tail=tail_a, off=-200, msg=msg)
    sta.sub_subject('B', t['desc_b'], t['instr_b'], 'normal_end', 'just_ended', tail=tail_b, off=-199.99, msg=msg)
    build.put(a_path, t['body_a'], b.T(-193))
    build.put(b_path, t['body_b'], b.T(-192))
    sta.cli_subject('C', t['instr_c'], "cd %s && claude -p --model m '%s' > %s" % (W, t['instr_c'], c_path), 'normal_end', 'just_ended', W, final=t['last_c'], off=-100)
    build.put(c_path, t['last_c'] + '\n', b.T(-100 + 10.2))
    sta.codex_subject('D', t['instr_d'], 'cd %s && codex exec -m gpt-6.1-sol -o "$OUT" "%s"' % (W, t['instr_d']), 'normal_end', 'just_ended', W, final=t['last_d'])
    build.put(d_path, t['last_d'], b.T(9.5))
    sta.now = b.T(300)
    sta.finish()
    b.meta.update(repo=W, unit=unit)
    return b


class Recorded(unittest.TestCase):
    """Every text of a recorded scene changed: the facts and the judgment stay the same."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='sinv-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def read(self, c, variant):
        """The board's reading of the case `c` with its texts of `variant` (`original`: as the builder wrote them). Every reading is built in the same folder, so that no path differs."""
        b = build.build_case(c, os.path.join(self.root, axes.digest(c.id, n=12)))
        try:
            if any(ph.hook for ph in b.phases):
                return None                                                    # records that arrive between two looks are written from what the builder holds: they would say the old texts
            fix_clock(b)
            if variant != 'original':
                vary(b, variant)
            return read_board(b)
        finally:
            shutil.rmtree(b.root, ignore_errors=True)

    def test_the_scene_of_every_bundle_reads_the_same_whatever_its_texts_say(self):
        bad, compared = [], 0
        for bundle, cases in sorted(chosen().items()):
            for c in cases:
                base = self.read(c, 'original')
                if base is None:
                    continue
                for variant in VARIANTS:
                    got = self.read(c, variant)
                    compared += 1
                    for part in ('facts', 'judged'):
                        if got[part] != base[part]:
                            bad.append('%s %s %s: %s' % (c.id, variant, part, first_difference(base[part], got[part])))
        self.assertGreater(compared, 800)
        self.assertEqual(bad, [], '%d of %d readings differ' % (len(bad), compared))


class Composite(unittest.TestCase):
    """One session with every kind of text in it (`build_composite`): read with its words as they were written and with each of the other texts. What it is given and what the judgment finds
    stay the same. (The two things that this finds in the board are in the report of review round 1: a message is addressed by the role line of its agent, and a Codex file is looked for in the
    folders that an instruction names.)"""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='sinv-comp-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def read(self, variant):
        b = build_composite(os.path.join(self.root, 'c'))
        try:
            fix_clock(b)
            if variant != 'original':
                vary(b, variant)
            return read_board(b)
        finally:
            shutil.rmtree(b.root, ignore_errors=True)

    def test_the_session_reads_the_same_whatever_its_texts_say(self):
        base = self.read('original')
        self.assertEqual(len(json.loads(base['facts'])['agents']), 4)
        self.assertEqual(sorted(k.split('/')[-1] for k in json.loads(base['judged'])['cells']), ['talk|r1|A', 'talk|r1|B', 'talk|r1|C', 'talk|r1|D'])
        bad = {}
        for variant in VARIANTS:
            got = self.read(variant)
            for part in ('facts', 'judged'):
                for place, one, other in differences(base[part], got[part]):
                    bad.setdefault((part, place), (str(one)[:60], str(other)[:60], []))[2].append(variant)
        self.assertEqual(bad, {}, '\n'.join('%s %s: %s -> %s (%s)' % (part, place, one, other, ' '.join(v)) for (part, place), (one, other, v) in sorted(bad.items())))


class Sensitivity(unittest.TestCase):
    """The control: the comparison above sees a board that reads a sentence. Each of these is a board that reads one kind of text (a role line, the body of a file, the text of a command, the
    last message of a run) to decide something; for some of the scenes the reading differs between the texts, so the test is not vacuous."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='sinv-')
        cls.inv = Recorded('test_the_scene_of_every_bundle_reads_the_same_whatever_its_texts_say')
        cls.inv.root = cls.root

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def differs(self, cases, mutate):
        """Whether the reading of some of `cases` differs between its original text and one of the variants while `mutate` (a context manager) is on."""
        with mutate:
            for c in cases:
                base = self.inv.read(c, 'original')
                for variant in VARIANTS if base is not None else ():
                    if self.inv.read(c, variant) != base:
                        return True
        return False

    def cases(self, bundle, **v):
        return [c for c in run.select() if c.bundle == bundle and all(c.v.get(k) == x for k, x in v.items()) and not c.twin_of][:4]

    def test_a_role_line_that_changes_what_an_agent_is(self):
        real = debates.facts_of

        def facts_of(a, *args, **kw):
            f = real(a, *args, **kw)
            return dataclasses.replace(f, status='running') if 'closed' in (a.description or '') else f
        self.assertTrue(self.differs(self.cases('ctr', launch='msg', wmethod='write'), mock.patch.object(debates, 'facts_of', facts_of)))

    def test_the_body_of_a_file_that_changes_a_cell(self):
        real = debates.judge

        def judge(s, statuses):
            jd = real(s, statuses)
            said = any(os.path.isfile(c.path) and 'closed' in _read(c.path) for c in jd.cells.values())
            return jd._replace(placed=dict(jd.placed, mutated=None)) if said else jd
        self.assertTrue(self.differs(self.cases('ctr', launch='msg', wmethod='write'), mock.patch.object(debates, 'judge', judge)))

    def test_the_text_of_a_command_that_changes_what_it_wrote(self):
        from board import agents
        real = agents.shell_events

        def shell_events(who, ts, run_, call, cmd, *args, **kw):
            return [] if 'closed' in cmd else real(who, ts, run_, call, cmd, *args, **kw)
        cases = self.cases('owr', ow='redirect') + self.cases('ctr', wmethod='redirect')
        self.assertTrue(self.differs(cases, mock.patch.object(agents, 'shell_events', shell_events)))


if __name__ == '__main__':
    unittest.main()
