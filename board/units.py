"""Debate folders found on disk, and the pure judgment of who sits where in them.

The questions are answered separately: which folders are debates (`read_unit`, `Catalog`), who holds which seat (`assign`), and what a seat's
cell shows (`cell_state`). Nothing here reads a session. `debates.py` turns an agent into `AgentFacts`; `assign` returns the Assignments, the
diagnostics and the membership; both are plain data.

Rules that the code below keeps:
  - A folder is a debate only when the disk says so: a brief.md, or an exact round folder (r1, r01, round1). A README.md or an index.md is a guide only
    next to a round folder. A path that names no such folder seats nobody (the ghost path), and no name alone settles a debate. A guide an agent merely
    names does not list a debate; a report path does.
  - A report path in an instruction is resolved against several bases (the absolute or ~ form, the agent's folder, the folder of the session that started it,
    the git top of the agent's folder, the debate folders the text itself names) and checked against the list; none or one fits, or the path is held.
  - A seat comes from, in this order: the path the agent is told to write, the seat marker of the instruction, a report write that succeeded (a Write or Edit
    whose result was not an error, or a Bash redirect or `tee` whose result was not an error and that left the file), and the description tag only together with a
    report write that succeeded (the tag alone seats nobody, and neither does a bare mention of the report the tag names, or a path that could not be resolved). A reader,
    a quotation (of a path or of a marker), a negation ("no need to write r1/B.md") and a failed write are not a seat. A marker or a tag seats in one debate, never in
    several. Two live agents at the same rank hold the seat (`seat_tie_held`); a later agent takes over from one that is already over, but only with a claim at least
    as strong as the one it takes over from. Over means: its last record is not later than the start of the later one and it is not working (a run that was cut off
    counts: the same script run again with the same instruction takes the seat; if the cut-off run is resumed and writes its report after the new one began, the two
    overlap and hold it as any two live agents do), and an agent whose process cannot be seen is not over. What an instruction only shows is not told: a code fence, a Markdown block quote, an earlier instruction that is said not to be
    carried out, and the write words of a text that forbids writing anything are quoted (`dead_spans`), for a path, a marker and a guide alike; a write that succeeded stays a
    seat whatever the instruction quotes.
  - The orchestrator's own successful writes name where to look, never who sits where: a guide it wrote (brief.md, README.md, index.md), a report under a round folder or a round
    folder it made (`listing_hint`) puts that folder on the page's list when the disk says it is a debate of the shape `written_debate` allows (a guide with exact round folders, or a
    brief.md that declares two result files or more) and it is not the top of a repository, `docs` of one, what the tools keep (~/.claude, ~/.codex), or this program's own repository.
    It makes no seat, no room and no member (the orchestrator is no agent of the debate); the name of a file alone settles nothing.
  - The spelling of a round folder (r01, round1) and of a file (A.md, a.md, A_flow.md) is kept as written; physically different files are never merged. The output file of
    a launch command is compared with the report the instruction names by its whole path, and one agent has one file of a seat.
  - A review with no round folder is a seat only for the reviewers and result files its guide declares; with no declaration it is a title and a
    diagnostic. A round, a seat or a file is never made up. A guide declares a participant by a letter and a description (`**A — development flow**`), never by a
    number: the finding numbers an editing job lists in bold (`**C-23**`) are no participants, so such a folder is a title with no cell.
  - A room is a folder that the structure of the records makes one, whatever its guide is called and whatever the work is called (the words debate, meeting,
    agenda are never read): two or more agents of one orchestrator whose first instruction points at the same guide (any `.md` that is on disk), who were at work at the
    same moment, and who are told, or have, each a file of their own in the guide's folder or one folder below it (a cell each, one round), or who share the guide, have no
    file anywhere and message each other (a room of participants only; a message that failed is none). Agents that run one after the other, or most of whom change
    something outside the guide's folder (code of their own part), are parallel work on a plan, not a room. A document everybody reads (the guide at the top of a repository or directly in its `docs` folder), a folder that is a
    debate already (it keeps its rules), one file that all of them write, files elsewhere, a guide of each one's own, one that is not on disk or that a later message
    names, and a lone agent are no room.

Standard library only; Python 3.9 compatible.
"""

import collections
import dataclasses
import functools
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .facts import Assignment, Evidence, Unit
from .util import HOME, expand, open_safe

GUIDE_NAMES = ('brief.md', 'README.md', 'index.md')
ROUND_DIR_RE = re.compile(r'(?:r|round)(\d+)\Z')
# "**A — development flow**": a participant row of a guide, a letter and then words that say who it is. A number after the dash is no description: a bold finding
# number of an editing job (`**C-23**`, `**X-2 · C-36**`) is not a participant, so the description may not start with a digit and has to hold a letter.
ROLE_RE = re.compile(r'\*\*([A-Z])\s*[—–-]\s*((?!\d)(?=[^*]*[^\W\d_])[^*]+?)\*\*')
GUIDE_CHARS = 200 * 200
LIVE = ('running', 'stalled')
OPEN = LIVE + ('unknown',)             # statuses of an agent that may still be working: `unknown` means its process cannot be seen, not that it ended

# a report path: <folder>/<r|round><N>/<name>.md. Left boundary: it does not start in the middle of a path ('.../t2_error/…', '…/x/…', 'a/b/…')
_RN = r'(?:r|round)(\d+)'
REPORT_RE = re.compile(r'((?<![\w.~/…}-])(?:~|/)[^\s`\'"()<>|,*]*?)/' + _RN + r'/([A-Za-z0-9_.-]+?)\.md')
# a relative one with nothing in front (r1/B.md), resolved against the agent's working folder or the debate its text names
REL_REPORT_RE = re.compile(r'(?<![\w/~.-])' + _RN + r'/([A-Za-z0-9_.-]+?)\.md')
# a relative one with a folder in front (docs/records/x/r1/C_github.md, ../x/r1/A.md)
REL_DIR_REPORT_RE = re.compile(r'(?<![\w/~.…$-])((?:[\w.-]+/)+)' + _RN + r'/([A-Za-z0-9_.-]+?)\.md')
# one that starts with a shell variable ($D/r1/A.md, ${D}/x/r1/A.md): not resolved, only noted (`path_unresolved`)
VAR_REPORT_RE = re.compile(r'\$\{?[A-Za-z_]\w*\}?(?:/[\w.-]+)*?/' + _RN + r'/[A-Za-z0-9_.-]+?\.md')
# guide paths (absolute, ~, or with a folder in front): the debate folder is the folder that holds it
GUIDE_ABS_RE = re.compile(r'((?<![\w.~/…}-])(?:~|/)[^\s`\'"()<>|,*]*?)/(?:brief|README|index)\.md')
GUIDE_REL_RE = re.compile(r'(?<![\w/~.…$-])((?:[\w.-]+/)+)(?:brief|README|index)\.md')
GUIDE_BARE_RE = re.compile(r'(?<![\w/~.…$-])brief\.md')       # `brief.md` with nothing in front: the guide of the folder the agent (or its launcher) works in
_HAS_DIR_RE = re.compile(r'/(?:r|round)\d+/')       # characters that must be present for the path regexes to match; text without them skips the slow ones
_HAS_REL_RE = re.compile(r'(?:r|round)\d+/')

# Seat markers at the start of an instruction: "[REVIEW11-C]", "C(GitHub ops) 담당", "C 담당" (Korean for "C in charge"), and the English sentences that address the
# participant itself: "You are participant C", "You hold seat C", "Work as C (...)". The bare words `participant C`, `seat C` and `as C (` are no marker (a user study's
# participant, a seat on a train, a language), and neither are "C 담당자" (the person in charge of C) and "C(언어) 담당 팀" (a team in charge of C): only the framed
# sentence, at the start of a sentence, is meant for the participant.
_ENGLISH_MARK = r'(?:^|(?<=\n)|(?<=[.!?\]]\s))(?:You\s+are\s+participant\s+([A-Z])(?![\w+#])|You\s+hold\s+seat\s+([A-Z])(?![\w+#])|Work\s+as\s+([A-Z])\s*\()'
MARKER_RE = re.compile(r'\[[A-Za-z0-9_]+-([A-Z])\]|(?<![A-Za-z0-9])([A-Z])\s*\([^)\n]{1,60}\)\s*담당(?!자|\s*팀)|(?<![A-Za-z0-9])([A-Z])\s*담당(?!자|\s*팀)|' + _ENGLISH_MARK)
MARKER_SCAN = 300                       # only the first characters of the spawn prompt: a later message or a long text that merely quotes a marker is not a seat
MARKER_NEG_AFTER_RE = re.compile(r'^\s*(?:이|은|는|가)?\s*(?:아니|아님|아닙|아닌)')                   # "B 담당이 아니다" (Korean: "is not B in charge")
MARKER_NEG_BEFORE_RE = re.compile(r"(?:\bnot|\bnever|\bisn't|\baren't|아닌)\s+(?:the\s+|a\s+)?$", re.I)
ROUND_RE = re.compile(r'(\d+)\s*라운드|[Rr]ound\s*(\d+)|(?<![\w/])r(\d+)(?![\w])')

# What the words around a path say it is for. A word meaning "write" (쓴다/작성/저장 in Korean, write… in English) just after or before makes it a
# report that agent will write; a "read" word makes it a report that agent only reads.
WRITE_AFTER_RE = re.compile(r'^[\s`\'"]*(?:에다?|을|를|로|으로)?\s*(?:쓴다|쓰고|써|쓸|작성|저장|기록|남)')
WRITE_BEFORE_RE = re.compile(r'(?:write|save|output|저장|작성)\w*[^.\n]{0,30}$', re.I)
READ_AFTER_RE = re.compile(r'^[\s`\'"]*(?:을|를)?\s*(?:읽|참고|참조|확인|검토|비교|본다|보고|보며)')
READ_VERB_RE = re.compile(r'(?:read|open|review|look\s+at|see|check|compare|consult|refer\s+to|study|참고|참조|읽)\w*', re.I)
CLAUSE_END_RE = re.compile(r'[.!?;]\s|[.!?;]$|\n')            # a full stop followed by a space ends a clause; a dot inside a file name (brief.md) does not
# "do not write", "no need to write", "쓸 필요 없다": the path is named so that it is NOT written
NEG_BEFORE_RE = re.compile(r"(?:\bdo(?:es)?\s+not|\bdon'?t|\bnever|\bno\s+need\s+to|\bneed\s+not|\bshould\s+not|\bmust\s+not|\bnot\s+to|\bwithout)\s+"
                           r"(?P<mid>(?:\w+\s+){0,2}?)(?:write|save|create|put|store|produce|output|record|submit|deliver)\w*[^.\n;]{0,40}$", re.I)
NEG_BEFORE_KO_RE = re.compile(r'(?:쓰지|작성하지|저장하지|남기지)\s*(?:않|마|말)[^.\n;]{0,20}$')
NEG_AFTER_RE = re.compile(r'^[\s`\'"]*(?:에다?|은|는|을|를|로|으로)?\s*(?:쓸\s*필요(?:는|가)?\s*없|쓰지\s*(?:않|마|말)|작성(?:할\s*필요(?:는|가)?\s*없|하지\s*(?:않|마|말))|'
                          r'저장(?:할\s*필요(?:는|가)?\s*없|하지\s*(?:않|마|말))|남기지\s*(?:않|마|말))')
NEG_NOT_WORDS = {'forget', 'fail', 'hesitate', 'neglect', 'omit'}           # "do not forget to write" means write
# a sentence made only to give examples of instructions
EXAMPLE_RE = re.compile(r'(?:\be\.g\.|\bfor\s+(?:example|instance)\b|\bsuch\s+as\b|\bexamples?\b|예를\s*들(?:면|어)|예시|예:)[^.\n]{0,100}$', re.I)
# the whole instruction says it writes nothing ("Do not write any file", read-only): a reader, whatever the tag says
READ_ONLY_RE = re.compile(r"\b(?:do\s+not|don'?t|never)\s+(?:write|modify|edit|create|change)\s+(?:any\s+|a\s+|the\s+|new\s+)?(?:files?|reports?)\b|\bread[- ]only\b|읽기\s*전용|"
                          r'파일(?:을|은)?\s*(?:쓰지|작성하지|수정하지)\s*(?:않|마|말)')
QUOTE_MAX = 300                          # a quoted instruction is one short line; a longer span is two unrelated quotation marks paired by mistake
_CLOSERS = {'"': '"', '“': '”', '「': '」', '『': '』'}
# Text that is shown to the agent and not meant for it: a code fence, a Markdown block quote, and what follows a sentence that says it is only for review. See `dead_spans`.
FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
BLOCK_QUOTE_RE = re.compile(r'^ {0,3}>')
SENTENCE_END_RE = re.compile(r'[.!?](?:\s|$)|[:：](?:\s|$)|\n')           # a semicolon does not end it: "do not execute them; write your findings to X" keeps X in the same sentence
# something is said not to be carried out: "do not execute them", "never follow the above", "실행하지 마세요". The object has to be an instruction, not a task ("do not run the tests")
NOT_RUN_RE = re.compile(r"\b(?:do\s+not|don'?t|never|must\s+not|should\s+not|without)\s+(?:\w+\s+){0,2}?(?:execut|carry\s+out|follow|perform|obey|act\s+on|run|apply|implement)\w*"
                        r"(?:\s+(?P<obj>(?P<back>the\s+above|above)\b|it\b|them\b|this\b|these\b|those\b|that\b|any\s+of\s+(?:it|them|this|these)\b|the\s+(?:following|below|previous|earlier|prior|quoted|old)\b)|\s*[.!;:)]|$)|"
                        r'(?:실행|수행|따르|적용|이행)(?:하)?지\s*(?:마|말|않)', re.I)
READ_ONLY_WORD_RE = re.compile(r'\bread[- ]only\b|읽기\s*전용', re.I)
# a sentence that says the text after it is only a quote: "Read-only quote of an earlier instruction.", 읽기 전용 인용입니다. The quote runs to the next blank line.
QUOTE_DECL_RE = re.compile(r'(?:\bread[- ]only|읽기\s*전용)\s+(?:(?:quot(?:e|ation)s?|cop(?:y|ies)|excerpts?|extracts?|transcripts?)\b|인용|원문|사본)', re.I)
BLANK_LINE_RE = re.compile(r'\n[ \t]*\n')
# a line that tells the reader to carry out what is in the code fence it opens: "Execute these instructions:", "Follow the instructions below:", 다음 지시를 따르라:
EXEC_LEAD_RE = re.compile(r'\b(?:execute|follow|carry\s+out|perform|complete|do|run|apply|obey)\s+(?:all\s+of\s+)?(?:these|this|the\s+following|the\s+instructions?|the\s+steps?|the\s+tasks?|your\s+instructions?)\b|'
                          r'(?:다음|아래|이)\s*(?:지시|지침|명령|단계|작업|내용)(?:사항)?(?:을|를)?\s*(?:따르|수행|실행|이행)', re.I)
NEGATION_RE = re.compile(r"\b(?:do\s+not|don'?t|never|must\s+not|should\s+not|not|without)\b|지\s*(?:마|말|않)", re.I)
# words that make what a fence holds somebody's earlier text, whatever the line says to do with it (not `below` or `following`: they stand in "Follow the instructions below:")
PAST_WORD_RE = re.compile(r'\b(?:earlier|previous|prior|past|old|quote[sd]?|quotation|verbatim|for\s+reference|executed|carried\s+out)\b|이전|지난|인용|예시|참고|앞선', re.I)
# the words that make an instruction a text under review: they stand in the sentence that says not to carry it out, or in the 200 characters before it
REVIEW_WORD_RE = re.compile(r'\b(?:earlier|previous|prior|past|old|below|following|above|quote[sd]?|verbatim|for\s+reference)\b|이전|지난|아래|위의|다음|인용|예시|참고|앞선', re.I)
# a text that forbids writing anything ("do not create, change or run anything", "Do not write any file", 파일을 만들거나 고치거나 실행하지 마세요) is not telling the agent to write:
# the write words in it are about somebody else's instruction. A prohibition with an exception ("any file other than", "anything outside") is none.
_VERBS = r'(?:create|change|modify|edit|write|run|execute|delete|touch|alter|save)\w*'
NO_WRITING_RE = re.compile(r"\b(?:do\s+not|don'?t|never|must\s+not)\s+(?:" + _VERBS + r"(?:\s*,\s*(?:or\s+)?|\s+(?:or|and)\s+|\s+)){1,6}?(?:anything|any\s+files?|any\s+reports?)\b"
                           r"(?!\s+(?:else|other|outside|except|besides|but|apart|in|under|inside|beyond|elsewhere|than|that|which|you|unless|if)\b)|"
                           r'(?:만들|고치|수정|변경|작성|쓰|삭제)(?:하)?(?:거나|고)\s*(?:[가-힣]+(?:거나|고)\s*){0,3}(?:만들|고치|수정|변경|작성|쓰|실행|삭제)(?:하)?지\s*(?:마|말|않)|'
                           r'아무것도\s*(?:만들|쓰|수정|변경|작성|실행)(?:하)?지\s*(?:마|말|않)|파일(?:을|은)?\s*(?:쓰지|작성하지|수정하지)\s*(?:않|마|말)', re.I)

# bare files of a flat review's declaration and of a debate guide
CUE_RE = re.compile(r'reviewers?\b|검토자|리뷰어', re.I)                  # a result file is declared next to the reviewer who writes it
CUE_REACH = 60                                                      # characters between the reviewer word and the file name
MD_NAME_RE = re.compile(r'`([A-Za-z0-9][A-Za-z0-9_.-]*?)\.md`')
FLAT_MD_RE = re.compile(r'(?<![\w/~.…$-])((?:[\w.~-]*/)*)([A-Za-z0-9][A-Za-z0-9_.-]*?)\.md(?![\w])')
LETTER_PATH_RE = re.compile(r'^(.*)/' + _RN + r'/([A-Za-z0-9_.-]+?)\.md$')


# ---------------------------------------------------------------------------------------------------------------------
# names (spelling is kept, nothing is merged)
# ---------------------------------------------------------------------------------------------------------------------
def round_of(name):
    """Round number of a round folder name (r1, r01, round1), or None."""
    m = ROUND_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def stem_letter(stem):
    """Role letter of a report stem: 'A' for 'A', 'A_flow' and 'A-flow'; '' for anything else."""
    m = re.match(r'^([A-Z])(?:[_-].+)?$', stem)
    return m.group(1) if m else ''


def stem_aliases(stems):
    """{letter: stem} for stems of the form X_name that stand for role X. Only when it is unambiguous: a bare X stem next to
    X_name (or two X_ names) means the files are separate participants, so nothing is merged."""
    by = collections.defaultdict(set)
    for st in stems:
        if stem_letter(st):
            by[stem_letter(st)].add(st)
    return {letter: next(iter(v)) for letter, v in by.items() if len(v) == 1 and next(iter(v)) != letter}


def stem_collisions(stems):
    """{letter: sorted stems} where one role letter has several physical files (A.md next to A_flow.md, B_gate.md next to b.md ...). They stay separate rows."""
    by = collections.defaultdict(set)
    for st in stems:
        key = stem_letter(st) or (st.upper() if len(st) == 1 and st.isalpha() else '')
        if key:
            by[key].add(st)
    return {k: sorted(v) for k, v in by.items() if len(v) > 1}


def round_dir_name(unit, rnd, spelled=None, holds=None):
    """The folder name of round `rnd` in a unit: the real folder, else the spelling the unit's other rounds use (r01 keeps its width), else the spelling of the
    mention that named it, else r<N>. Where the round has several real folders (r01 next to r1) `holds(name)` says which of them has the file in question: the
    one that does when it is only one, else the first (the files are never merged, `alias_collision` says there are several)."""
    real = unit.rounds.get(rnd) if unit is not None else None
    if real:
        if holds is not None and len(real) > 1:
            has = [d for d in real if holds(d)]
            return has[0] if len(has) == 1 else real[0]
        return real[0]
    sample = next((d[0] for _n, d in sorted(unit.rounds.items()) if d), None) if unit is not None else None
    if sample:
        m = re.match(r'^(r|round)(0*)(\d+)$', sample)
        width = len(m.group(2)) + len(m.group(3)) if m and m.group(2) else 0
        return m.group(1) + str(rnd).zfill(width) if m else sample
    return spelled or 'r%d' % rnd


# ---------------------------------------------------------------------------------------------------------------------
# the debate folders on disk
# ---------------------------------------------------------------------------------------------------------------------
def too_broad(path):
    """A folder that is never a debate or a root of one: the filesystem root, HOME, an ancestor of HOME, the temporary folders. `path` is normalised."""
    return path in ('/', '/tmp', '/var/tmp') or HOME == path or HOME.startswith(path + os.sep)


def _read_text(path):
    """The first characters of a guide, through the safe opener (a guide has a fixed name, so a regular file in place is read even under a dot folder)."""
    try:
        with open_safe(path, strict=False) as f:
            return f.read(GUIDE_CHARS)
    except OSError:
        return ''


def declared_reports(text):
    """Result files a flat review's guide declares: a `name.md` that stands within CUE_REACH characters of a reviewer word on the same line ("Reviewers and their
    result files: `sol.md` (reviewer sol)"). A file a guide only names (a document to review, a table of inputs) is not a declaration; guide names never count."""
    out = []
    for line in text.splitlines():
        cues = [m.start() for m in CUE_RE.finditer(line)]
        if not cues:
            continue
        for m in MD_NAME_RE.finditer(line):
            stem = m.group(1)
            if (stem + '.md').lower() in {g.lower() for g in GUIDE_NAMES} or stem in out or min(abs(m.start() - c) for c in cues) > CUE_REACH:
                continue
            out.append(stem)
    return out


def declares_rounds(text):
    """Whether a guide describes a round structure: participant rows (**A — flow**) or report paths under a round folder (r1/<id>.md)."""
    return bool(ROLE_RE.search(text) or re.search(r'(?<![\w/])(?:r|round)\d+/', text))


def read_unit(path, text_of=None):
    """The debate folder at `path` as a Unit, or None. A folder is a debate when it has an exact round folder (a README.md or an index.md next to it is then its guide),
    or a brief.md. With round folders the unit is `rounds`; with only a guide it is `rounds` when the guide describes rounds (a debate that has
    not started) and `flat` otherwise: its `declared_reports` are the only seats it has."""
    text_of = text_of or _read_text
    path = os.path.normpath(path)
    try:
        names = os.listdir(path)
    except OSError:
        return None
    rounds = {}
    for name in sorted(names):
        n = round_of(name)
        if n is not None and os.path.isdir(os.path.join(path, name)):
            rounds.setdefault(n, []).append(name)
    guide = next((g for g in GUIDE_NAMES if g in names and os.path.isfile(os.path.join(path, g))), None)
    unit = Unit(id=path, path=path, rounds=rounds)
    real = os.path.realpath(path)
    if real != path:
        unit.aliases.append(real)
    if guide:
        unit.brief = os.path.join(path, guide)
    if rounds:
        return unit
    if guide != 'brief.md':
        return None                                                  # a README.md or an index.md confirms nothing by itself: only with a round folder next to it
    text = text_of(unit.brief)
    if declares_rounds(text):
        return unit
    unit.kind = 'flat'
    unit.declared_reports = declared_reports(text)
    return unit


class Catalog:
    """The debate folders, remembered by what the folder looks like (its modification time and its guide's). One generation is one state build: a folder is
    looked at once per generation, whatever number of paths ask about it."""

    LIMIT = 4096

    def __init__(self, text_of=None):
        self._text_of = text_of or _read_text
        self._seen = {}              # path -> (generation, key, Unit|None)
        self._alias = {}             # a path as it was asked for -> the same path normalised
        self._lists = {}             # folder -> (generation, the .md stems in it)
        self._rooms = {}             # folder -> the Unit of a room of this generation (a room lives for one build: the records make it, not the disk)
        self._files = {}             # path -> (generation, whether it is a regular file)
        self.gen = 0

    def begin(self):
        self.gen += 1
        self._rooms = {}

    def add_room(self, unit):
        """Makes `unit` what the folder is for the rest of this generation."""
        self._rooms[unit.path] = unit

    def is_file(self, path):
        """Whether the path is a regular file, looked at once per generation."""
        c = self._files.get(path)
        if c and c[0] == self.gen:
            return c[1]
        try:
            ok = stat.S_ISREG(os.stat(path).st_mode)
        except OSError:
            ok = False
        if len(self._files) > self.LIMIT:
            self._files.clear()
        self._files[path] = (self.gen, ok)
        return ok

    @staticmethod
    def _key(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        key = [st.st_mtime_ns]
        for g in GUIDE_NAMES:
            try:
                gs = os.stat(os.path.join(path, g))
                key.append((gs.st_mtime_ns, gs.st_size))
            except OSError:
                key.append(None)
        return tuple(key)

    def unit_at(self, path):
        """The Unit that is exactly this folder, or None."""
        path = self._alias.get(path, path)
        if path in self._rooms:
            return self._rooms[path]
        c = self._seen.get(path)
        if c and c[0] == self.gen:
            return c[2]
        raw, path = path, os.path.normpath(path)
        if path != raw:
            self._alias[raw] = path
            if path in self._rooms:
                return self._rooms[path]
            c = self._seen.get(path)
            if c and c[0] == self.gen:
                return c[2]
        if too_broad(path):
            return None
        key = self._key(path)
        if c and c[1] == key:
            unit = c[2]
        else:
            unit = read_unit(path, self._text_of) if key is not None else None
        if len(self._seen) > self.LIMIT:
            self._seen.clear()
            self._alias.clear()
        self._seen[path] = (self.gen, key, unit)
        return unit

    def promote(self, path):
        """A flat review named by a report path under a round folder (U/r1/B.md) is a debate with rounds that have not started: it is read as `rounds` from now on
        (until its folder changes). Returns the Unit."""
        path = os.path.normpath(self._alias.get(path, path))
        c = self._seen.get(path)
        u = c[2] if c else None
        if u is None or u.kind != 'flat':
            return u
        u = dataclasses.replace(u, kind='rounds', declared_reports=[])
        self._seen[path] = (c[0], c[1], u)
        return u

    def unit_above(self, path, levels=3):
        """The nearest debate folder among the folder of `path` and its ancestors (the ancestor check), at most `levels` folders up."""
        d = os.path.dirname(path)
        for _ in range(levels):
            if not d or too_broad(d):
                return None
            u = self.unit_at(d)
            if u:
                return u
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent
        return None

    def md_stems(self, folder):
        """The names (without .md) of the markdown files directly in a folder, listed once per generation."""
        c = self._lists.get(folder)
        if c and c[0] == self.gen:
            return c[1]
        try:
            names = [n[:-3] for n in os.listdir(folder) if n.endswith('.md')]
        except OSError:
            names = []
        if len(self._lists) > self.LIMIT:
            self._lists.clear()
        self._lists[folder] = (self.gen, names)
        return names

    def children(self, root, skip=('final',)):
        """The sub-folders of a grouping folder that are debate folders (the topics of a shared guide)."""
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return []
        out = []
        for name in names:
            p = os.path.join(root, name)
            if name in skip or not os.path.isdir(p):
                continue
            u = self.unit_at(p)
            if u:
                out.append(u)
        return out


# folders a walk never enters: what a repository keeps its tools and builds in
WALK_SKIP = frozenset(('node_modules', '__pycache__', 'venv', 'site-packages', 'dist', 'build', 'target', 'vendor'))


def repo_top(cwd, levels=12):
    """The nearest folder at or above `cwd` that holds a `.git` (a folder, or a file in a worktree): the top of a repository. None when there is none, or
    when it would be a folder that is too broad."""
    d = os.path.normpath(cwd) if cwd else ''
    for _ in range(levels):
        if not d or not os.path.isabs(d) or too_broad(d):
            return None
        if os.path.exists(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def holds_repositories(folder, at_least=2):
    """Whether a folder is a place that keeps repositories (~/Dev) rather than a project: at least `at_least` of its sub-folders hold a `.git`."""
    n = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and os.path.exists(os.path.join(e.path, '.git')):
                    n += 1
                    if n >= at_least:
                        return True
    except OSError:
        pass
    return False


def walk_repo(top, catalog, max_dirs=3000, max_depth=6, deadline=None):
    """The debate folders below a repository top, by a bounded breadth-first walk. Meant for the background stage of the board, not for a state build.
    Never walks a folder that is too broad (HOME or what holds it, /tmp ...), a folder that keeps repositories (~/Dev), dot folders, links, or the usual dependency
    folders. Returns (units, capped): `capped` is True when the budget ran out before the walk was done (`listing_capped`)."""
    top = os.path.normpath(top)
    if too_broad(top) or not os.path.isdir(top) or (not os.path.exists(os.path.join(top, '.git')) and holds_repositories(top)):
        return [], False
    found, queue, seen = [], collections.deque([(top, 0)]), 0
    while queue:
        d, depth = queue.popleft()
        seen += 1
        if seen > max_dirs or (deadline is not None and time.monotonic() > deadline):
            return found, True
        u = catalog.unit_at(d)
        if u:
            found.append(u)
        if depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                subs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for name in subs:
            if name.startswith('.') or name in WALK_SKIP or (u and round_of(name) is not None):
                continue
            queue.append((os.path.join(d, name), depth + 1))
    return found, False


# ---------------------------------------------------------------------------------------------------------------------
# what the words around a path say it is
# ---------------------------------------------------------------------------------------------------------------------
REMEMBER_CHARS = 32 * 1024         # a text longer than this is worked out again at every build instead of being remembered
REMEMBER_TEXTS = 1024              # at most this many texts per function: the keys are the agents' own strings, so the cost is the evicted ones kept alive (<= 32 MiB)


def _remembered(fn):
    """Remembers a pure function of one text. The instructions of an agent do not change between two state builds, and the regexes over them are the
    cost of a debate build: the first build pays, the next ones read the answer. A text too long for that is remembered only as the last one asked about (the same object),
    so that the answer is not worked out again for each path in it."""
    cached = functools.lru_cache(maxsize=REMEMBER_TEXTS)(fn)
    last = [None]                                           # (the last long text, its answer): replaced as one value

    @functools.wraps(fn)
    def call(text):
        if len(text) <= REMEMBER_CHARS:
            return cached(text)
        slot = last[0]
        if slot is not None and slot[0] is text:
            return slot[1]
        answer = fn(text)
        last[0] = (text, answer)
        return answer
    call.cache_info, call.cache_clear = cached.cache_info, cached.cache_clear
    return call



def _enclosing_quote(text, start, end):
    """The text span of the quotation ("..." “...” 「...」) that holds [start, end), or None. A quotation is a short single line: a longer span is two unrelated
    quotation marks paired by mistake."""
    best = None
    for op, cl in _CLOSERS.items():
        if op == cl:
            if text.count(op, 0, start) % 2 == 0:                    # marks pair up from the start of the text: an even number before means we are outside
                continue
            i = text.rfind(op, 0, start)
        else:
            i, closed = text.rfind(op, 0, start), text.rfind(cl, 0, start)
            if i < 0 or closed > i:
                continue
        j = text.find(cl, end)
        if j > 0 and 0 <= i and j - i <= QUOTE_MAX and '\n' not in text[i:j] and (best is None or j - i < best[1] - best[0]):
            best = (i, j)
    return (best[0] + 1, best[1]) if best else None


WORD_RE = re.compile(r"[^\s`'\"()<>|,;*]+")                      # a word of an instruction: up to a space, a quote, a bracket or a comma
BEFORE_LIMIT = 4096                      # the most characters read before a path to find what it is for (a path is never longer than this)


def _is_path(word):
    """Whether a word is a path written out: it has a slash and starts like a path (/ ~ . $), ends in .md or has several folders; not "and/or" or "read/check"."""
    return len(word) > 1 and '/' in word and (word[0] in '/~.$' or word.endswith('.md') or word.count('/') > 1)


def fold_paths(text):
    """`text` with each path in it shrunk to one short word, so that how long a path is does not decide how far the verb before the next path reaches. A path with a dot
    in it stays a word with a dot (a file name's dot has always stopped a 'write … name' chain), and the sentence punctuation that follows a path stays."""
    def short(m):
        word = m.group(0)
        body = word.rstrip('.!?:')
        return ('p.d' if '.' in body else 'p') + word[len(body):] if _is_path(body) else word
    return WORD_RE.sub(short, text) if '/' in text else text


def _before(text, start):
    """The 100 characters that stand before a path, each earlier path counted as one short word: more of the text is read while the paths in it are long."""
    width = 160
    while True:
        lo = max(0, start - width)
        folded = fold_paths(text[lo:start])
        if len(folded) >= 100 or lo == 0 or width >= BEFORE_LIMIT:
            return folded[-100:]
        width *= 3


def _carried_out(before):
    """Whether the text before a code fence ends with a line that tells the reader to carry out what the fence holds ("Execute these instructions:", 다음 지시를 따르라:): the
    line ends with a colon, its last sentence says to carry something out, and nothing in the line says it is somebody's earlier text, a read-only quote or not to be done."""
    line = before.rstrip().rsplit('\n', 1)[-1].strip()
    if not line.endswith((':', '：')):
        return False
    last = re.split(r'(?<=[.!?])\s+', line)[-1]
    return EXEC_LEAD_RE.search(last) is not None and not (NEGATION_RE.search(line) or READ_ONLY_WORD_RE.search(line) or PAST_WORD_RE.search(line))


def _dead_spans(text):
    """The spans of a text that are shown to the agent and are no instruction to it, sorted and merged: a fenced code block (a fence that never closes holds the rest of
    the text) unless the line before it tells the agent to carry out what is in it, a run of Markdown block-quote lines, what follows a sentence that says an earlier
    instruction is not to be carried out ("do not execute them"; "the above" looks back) or that opens a read-only quotation ("Read-only quote of the earlier instruction:"),
    and the lines after a sentence that says they are a read-only quote ("Read-only quote of an earlier instruction."), up to the blank line. A path, a seat marker or a
    guide inside them is quoted, not told."""
    spans = []
    fence, quote, pos = None, None, 0                           # fence: (character, length, start, whether it is carried out); quote: start of the run of quote lines
    for line in text.splitlines(True):
        body = line.rstrip('\r\n')
        m = FENCE_RE.match(body)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                if not fence[3]:
                    spans.append((fence[2], pos + len(line)))
                fence = None
        elif m and not (m.group(1)[0] == '`' and '`' in m.group(2)):          # "```x```" on one line is code in a line, not a fence
            fence = (m.group(1)[0], len(m.group(1)), pos, _carried_out(text[:pos]))
        if fence is None and BLOCK_QUOTE_RE.match(body):
            quote = pos if quote is None else quote
        elif quote is not None:
            spans.append((quote, pos))
            quote = None
        pos += len(line)
    if fence is not None and not fence[3]:
        spans.append((fence[2], len(text)))
    if quote is not None:
        spans.append((quote, len(text)))
    for m in NOT_RUN_RE.finditer(text):
        lead = SENTENCE_END_RE.search(text, m.end())
        sentence_end = lead.end() if lead else len(text)
        opens = re.match(r'[\s)]*[:：]', text[m.end():m.end() + 8]) is not None             # "(do not execute):" opens the text that is not to be carried out
        bare = m.group('obj') is None and m.group(0)[0].isascii()                       # "Do not run." names nothing: only the colon makes it a lead-in
        if not (opens or (not bare and REVIEW_WORD_RE.search(text, max(0, m.start() - 200), sentence_end))):
            continue                                            # "the tests are flaky; do not run them", "review the plan, do not execute it" say nothing about an earlier instruction
        if m.group('back'):
            before = [x.end() for x in SENTENCE_END_RE.finditer(text, 0, m.start())]
            spans.append((0, before[-1] if before else 0))
        else:
            spans.append((sentence_end, len(text)))
    for m in READ_ONLY_WORD_RE.finditer(text):                  # "Read-only quote of the earlier instruction:", "Read-only. Previous guidance follows:": the colon opens the text under review
        lo = max([x.end() for x in SENTENCE_END_RE.finditer(text, 0, m.start())] or [0])
        for _ in range(2):                                      # the sentence of the word, then the one after it
            lead = SENTENCE_END_RE.search(text, max(lo, m.end()))
            if lead is None:
                break
            if lead.group(0)[0] in ':：' and REVIEW_WORD_RE.search(text, lo, lead.end()):
                spans.append((lead.end(), len(text)))
                break
            lo = lead.end()
    for m in QUOTE_DECL_RE.finditer(text):                      # "Read-only quote of an earlier instruction." and the lines after it
        lead = SENTENCE_END_RE.search(text, m.end())
        start = lead.end() if lead else len(text)
        blank = BLANK_LINE_RE.search(text, start)
        spans.append((start, blank.start() if blank else len(text)))
    merged = []
    for a, b in sorted(x for x in spans if x[1] > x[0]):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return tuple(merged)


dead_spans = _remembered(_dead_spans)


def in_dead_span(text, pos):
    return any(a <= pos < b for a, b in dead_spans(text))


@_remembered
def writes_nothing(text):
    """Whether a text forbids writing anything ("do not create, change or run anything"): what it says to write is somebody else's instruction, quoted."""
    return bool(NO_WRITING_RE.search(text))


def mention_class(text, start, end):
    """What the words around the path text[start:end] make of it: 'negated' (named so that it is not written), 'quoted' (inside a quoted example of an
    instruction; inside a code fence, a block quote or an earlier instruction that is not to be carried out; or told to be written by a text that forbids writing anything),
    'write' (the agent is told to write it), 'read' (it is only read) or 'ref' (just named). The paths before it count as one word each."""
    cls = _mention_class(text, start, end)
    return 'quoted' if cls == 'write' and writes_nothing(text) else cls


def _mention_class(text, start, end):
    if in_dead_span(text, start):
        return 'quoted'
    before = _before(text, start)
    after = text[end:end + 40]
    if NEG_AFTER_RE.match(after) or NEG_BEFORE_KO_RE.search(before[-60:]):
        return 'negated'
    nm = NEG_BEFORE_RE.search(before[-90:])
    if nm and not NEG_NOT_WORDS & set(nm.group('mid').lower().split()):
        return 'negated'
    q = _enclosing_quote(text, start, end)
    if q and (WRITE_BEFORE_RE.search(fold_paths(text[q[0]:start])) or WRITE_AFTER_RE.match(text[end:q[1]])):
        return 'quoted'
    if EXAMPLE_RE.search(before):
        return 'quoted'
    if WRITE_AFTER_RE.match(after[:20]):
        return 'write'
    if READ_AFTER_RE.match(after):
        return 'read'
    tail = before[-100:]
    clause = tail[(list(CLAUSE_END_RE.finditer(tail)) or [None])[-1].end():] if CLAUSE_END_RE.search(tail) else tail
    w = WRITE_BEFORE_RE.search(tail[-60:])
    rs = list(READ_VERB_RE.finditer(clause))
    r = rs[-1] if rs else None
    if w and r:                                                  # the verb nearest to the path wins ("Read `a`, write `b`")
        w_at = len(tail) - len(tail[-60:]) + w.start()
        r_at = len(tail) - len(clause) + r.start()
        return 'write' if w_at > r_at else 'read'
    return 'write' if w else ('read' if r else 'ref')


def write_intent(text, m):
    """Whether the words around the path (match m) tell the agent to write it."""
    return mention_class(text, m.start(), m.end()) == 'write'


@_remembered
def guide_mentions(text):
    """The guide paths of a text as (form, prefix): form 'abs' (absolute or ~), 'dir' (a folder in front of it) or 'bare' (`brief.md` with nothing in front).
    A guide inside a code fence, a block quote or a text that is only for review is not one the agent is pointed at. Pure and remembered, like find_mentions."""
    if 'brief.md' not in text and 'README.md' not in text and 'index.md' not in text:
        return ()
    out = [('abs', m.group(1)) for m in GUIDE_ABS_RE.finditer(text) if not in_dead_span(text, m.start())]
    out += [('dir', m.group(1)) for m in GUIDE_REL_RE.finditer(text) if not in_dead_span(text, m.start())]
    if any(not in_dead_span(text, m.start()) for m in GUIDE_BARE_RE.finditer(text)):
        out.append(('bare', ''))
    return tuple(out)


@_remembered
def md_mentions(text):
    """Every markdown file name in a text with the folder written in front of it: (start, end, folder, stem). The files of a flat review are named like this."""
    return tuple((m.start(), m.end(), m.group(1), m.group(2)) for m in FLAT_MD_RE.finditer(text)) if '.md' in text else ()


@_remembered
def read_only_text(text):
    """Whether an instruction says that nothing is to be written ("Do not write any file", read-only)."""
    return bool(READ_ONLY_RE.search(text))


class Mention:
    """One report path in an instruction: where it is, how it is spelled and what it is for."""
    __slots__ = ('start', 'end', 'form', 'prefix', 'dirname', 'rnd', 'stem', 'cls')

    def __init__(self, start, end, form, prefix, dirname, rnd, stem, cls):
        self.start, self.end, self.form, self.prefix, self.dirname, self.rnd, self.stem, self.cls = start, end, form, prefix, dirname, rnd, stem, cls


def _dirname_of(m, prefix_len):
    """The spelling of the round folder (r1, r01, round1) inside a match."""
    return m.group(0)[prefix_len:].lstrip('/').split('/')[0]


@_remembered
def find_mentions(text):
    """Every report path of a text, as Mentions of form 'abs' (also ~), 'dir' (a folder in front), 'bare' (r1/B.md) or 'var' (a shell variable in front).
    A pure function of the text, so the answer is remembered: the instructions of an agent do not change between two state builds."""
    out = []
    has_dir = bool(_HAS_DIR_RE.search(text))
    if not has_dir and not _HAS_REL_RE.search(text):
        return ()
    if has_dir:
        for m in REPORT_RE.finditer(text):
            out.append(Mention(m.start(), m.end(), 'abs', m.group(1), _dirname_of(m, len(m.group(1))), int(m.group(2)), m.group(3),
                               mention_class(text, m.start(), m.end())))
        for m in REL_DIR_REPORT_RE.finditer(text):
            out.append(Mention(m.start(), m.end(), 'dir', m.group(1), _dirname_of(m, len(m.group(1))), int(m.group(2)), m.group(3),
                               mention_class(text, m.start(), m.end())))
        for m in VAR_REPORT_RE.finditer(text):
            out.append(Mention(m.start(), m.end(), 'var', '', '', int(m.group(1)), '', mention_class(text, m.start(), m.end())))
    for m in REL_REPORT_RE.finditer(text):
        out.append(Mention(m.start(), m.end(), 'bare', '', _dirname_of(m, 0), int(m.group(1)), m.group(2), mention_class(text, m.start(), m.end())))
    return tuple(out)


# ---------------------------------------------------------------------------------------------------------------------
# rooms: agents of one orchestrator that work together in a folder, whatever the work is called
# ---------------------------------------------------------------------------------------------------------------------
COMMON_FOLDERS = ('docs', 'doc')               # a guide directly in these folders of a repository is a document of the repository, not of a room


@dataclass
class Room:
    """A folder the records make a room of: `kind` 'cells' (each member has a file of its own: a cell each) or 'members' (a meeting by message only: participants and
    no cell). `members` are the agent ids in start order, `files` {agent id: absolute path of its own file} (cells), `guide` the guide's absolute path."""
    folder: str
    guide: str
    kind: str
    members: List[str] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)


# "do not read", "never open", "읽지 마": the document is named so that it is NOT used
NEG_READ_BEFORE_RE = re.compile(r"(?:\bdo(?:es)?\s+not|\bdon'?t|\bnever|\bno\s+need\s+to|\bshould\s+not|\bmust\s+not|\bwithout)\s+(?P<mid>(?:\w+\s+){0,2}?)(?:read|open|consult|follow|use|look\s+at|see)\w*[^.\n;]{0,40}$", re.I)
NEG_READ_AFTER_RE = re.compile(r'^[\s`\'"]*(?:을|를)?\s*(?:읽지|열지|따르지|보지)\s*(?:않|마|말)')


@_remembered
def md_words(text):
    """The `.md` files a text names, as (start, end, folder written in front, stem, what the words around it make of it): 'negated', 'quoted', 'write', 'read' or 'ref',
    and 'negated' also for a document named so that it is not read. Pure and remembered, like find_mentions."""
    out = []
    for start, end, pre, stem in md_mentions(text) if '.md' in text else ():
        cls = mention_class(text, start, end)
        if cls in ('read', 'ref'):
            nm = NEG_READ_BEFORE_RE.search(_before(text, start)[-90:])
            if (nm and not NEG_NOT_WORDS & set(nm.group('mid').lower().split())) or NEG_READ_AFTER_RE.match(text[end:end + 40]):      # "do not forget to read" means read
                cls = 'negated'
        out.append((start, end, pre, stem, cls))
    return tuple(out)


@_remembered
def other_paths(text):
    """The paths of a text (words with a folder in them) that are not a `.md` name, as (word, class): what an instruction names besides its documents."""
    out = []
    for m in WORD_RE.finditer(text) if '/' in text else ():
        word = m.group(0).rstrip('.!?:')
        if _is_path(word) and not word.endswith('.md') and '://' not in word[:12]:
            out.append((word, mention_class(text, m.start(), m.start() + len(word))))
    return tuple(out)


def _doc_paths(text, bases):
    """For each `.md` a text names that is no quotation or negation: (what the words around it make of it, the files it can mean, one for each of the bases)."""
    out = []
    for _start, _end, pre, stem, cls in md_words(text):
        if cls in ('quoted', 'negated'):
            continue
        name = pre + stem + '.md'
        cands = (expand(name),) if pre.startswith(('/', '~')) else tuple(dict.fromkeys(os.path.normpath(os.path.join(b, name)) for b in bases))
        out.append((cls, cands))
    return tuple(out)


_doc_paths_memo = functools.lru_cache(maxsize=REMEMBER_TEXTS)(_doc_paths)


def doc_paths(text, bases):
    """_doc_paths, remembered: the instructions of an agent and the folders it works in do not change between two state builds."""
    return _doc_paths_memo(text, bases) if len(text) <= REMEMBER_CHARS else _doc_paths(text, bases)


def _is_common(folder):
    """Whether a guide in this folder is a document everybody of a repository reads: the folder is the top of a repository (it holds `.git`), or the `docs` folder directly in it."""
    return too_broad(folder) or os.path.exists(os.path.join(folder, '.git')) or \
        (os.path.basename(folder) in COMMON_FOLDERS and os.path.exists(os.path.join(os.path.dirname(folder), '.git')))


def _in_reach(path, folder):
    return os.path.dirname(path) == folder or os.path.dirname(os.path.dirname(path)) == folder


# What an instruction says to change when it names a code path: "implement your part in src/x", "fix it in `src/x/part.py`" (not a `.md` name: those are judged as files of the room)
MODIFY_RE = re.compile(r'(?:implement|change|modify|edit|fix|update|patch|refactor|rewrite|build|write|save|create|add|put|store|produce|generate|move|rename|delete|remove|'
                       r'구현|수정|변경|고치|고쳐|작성|저장|추가|만들|삭제|이동)\w*', re.I)
STATE_DIRS = tuple(os.path.join(HOME, d) + os.sep for d in ('.claude', '.codex'))          # what an agent keeps of itself is no edit of the work
SCRATCH_DIRS = tuple(sorted({os.path.normpath(d) + os.sep for d in ('/tmp', '/var/tmp', '/private/tmp', '/var/folders', '/private/var/folders', tempfile.gettempdir(),
                                                                    os.environ.get('TMPDIR') or '/tmp') if os.path.isabs(d)}))        # where a file kept for scratch work goes


def _work_file(path, roots):
    """Whether a file is one of the work that an agent may change: inside a repository it works in or the guide is in (`roots`), not in what the agent keeps of itself, and,
    where no repository is known, not in a folder for scratch work."""
    if path.startswith(STATE_DIRS):
        return False
    if roots:
        return any(path == r or path.startswith(r + os.sep) for r in roots)
    return not path.startswith(SCRATCH_DIRS)


@_remembered
def modified_paths(text):
    """The paths (not `.md` names) that an instruction tells the agent to change, as (word, strong): "Implement your part in `src/x/part.py`". A word with one folder and no file
    extension (`src/x`) is weak: it counts only where it is there on disk, since "and/or" looks the same. Quotations, negations and what is only read are left out."""
    out = []
    for m in WORD_RE.finditer(text) if '/' in text else ():
        word = m.group(0).rstrip('.!?:')
        if not word or word.startswith('$') or word.endswith('.md') or '://' in word[:12] or '/' not in word.strip('/') or not (word[0] in '/~.' or word[0].isalnum()):
            continue
        cls = mention_class(text, m.start(), m.start() + len(word))
        if cls in ('quoted', 'negated', 'read'):
            continue
        before = _before(text, m.start())[-100:]
        clause = before[(list(CLAUSE_END_RE.finditer(before)) or [None])[-1].end():] if CLAUSE_END_RE.search(before) else before
        verbs = [v.start() for v in MODIFY_RE.finditer(clause)]
        reads = [v.start() for v in READ_VERB_RE.finditer(clause)]
        if cls == 'write' or (verbs and (not reads or verbs[-1] > reads[-1])):
            out.append((word, word[0] in '/~.' or word.count('/') > 1 or '.' in word.rsplit('/', 1)[-1]))
    return tuple(out)


def _inside(path, folder):
    """Whether a path is the room's folder, directly in it, or one folder below it: where the files of a room are."""
    return path == folder or _in_reach(path, folder)


def _present_together(pointers, statuses):
    """The agents among `pointers` that were at work at one moment, the most of them (the earliest such moment where two sets are as big), in start order. An agent is at
    work from its first record to its last, and a running one until now; one whose process cannot be seen (`unknown`) is there until its last record, since nothing says it
    is still there. Two agents of which one was over when the other began were never together. An agent whose start, or whose end when it is not running, the records do not
    give (0) cannot be placed in time, so it is in no room."""
    inf = float('inf')
    spans = []
    for f in pointers:
        begin, end = f.first or f.start, (inf if statuses.get(f.id) in LIVE else f.last)
        if begin and end:
            spans.append((begin, end, f))
    best = []
    for begin, _e, _f in spans:
        here = [g for b, e, g in spans if b <= begin and (begin < e or (e <= b and begin == b))]
        if len(here) > len(best):
            best = here
    return best


def _changes_elsewhere(change, folder, roots):
    """Whether an agent is told to change, or changed, a file of the work outside a room's folder (`_work_file`): `change` is (the places each path of its instruction can
    mean, the paths of its writes by a tool that succeeded)."""
    told, wrote = change
    return any(not any(_inside(c, folder) for c in cands) and any(_work_file(c, roots) for c in cands) for cands in told) or \
        any(not _inside(p, folder) and _work_file(p, roots) for p in wrote)


def find_rooms(agents, cat, tops_of, statuses=None):
    """The rooms of one orchestrator's agents. Things together make a room, all of them facts of the records and the disk, and no word is read:
      (1) two or more agents whose FIRST instruction points at the same guide (a `.md` of any name that is a file on disk; not one it is told to write, not one a
          quotation, a code fence, a block quote or a negation names), who were at work at the same moment (`_present_together`; `statuses` tells who is still running), and
      (2) those agents are told, or have, each a file of their own (`.md`) in the guide's folder or one folder below it: a room of cells; or none of them has
          a file anywhere and they message each other (a message that failed to go is none): a room of participants only, and
      (3) not more than half of them change a file of the work outside the guide's folder (a path the first instruction tells them to change, or a write by the Write, Edit or
          patch tool that succeeded: code of their own part somewhere else), which is parallel work on one plan, not a meeting. A file of the work is one in a repository
          the agent works in or the guide is in; what a shell command saved (a redirect, a log) and a file in a folder for scratch work outside the repository are not. The time
          and the vote are of the agents that could be in the room: those with a file of their own in it, or, for a room of participants only, all that point at the guide; one
          that has no file there (it writes its report elsewhere) neither makes nor breaks it.
    A guide at the top of a repository or directly in its `docs` folder is a document for everybody, and a folder that is a debate already (a round folder, a guide
    that declares rounds, a flat review with declared result files) keeps the rules of the debate. One file that all of them write is no file of their own. A
    document that one of the agents is told to write, or wrote, is that agent's result which the others read, not a guide: it is handed to the participants,
    not made by one of them. Returns a list of Rooms."""
    statuses = statuses or {}
    guides = collections.defaultdict(dict)             # guide path -> {agent id: facts} of the agents that point at it
    own = {}                                           # agent id -> its files in the order told, then written (absolute, `.md`)
    anywhere = {}                                      # agent id -> whether it is told to write, or wrote, a file (or names a document that is not there)
    changes = {}                                       # agent id -> what it is told to change, as lists of the places a path can mean, and the paths of the writes that succeeded
    bases_of = {}                                      # (its folder, its launcher's folder) -> the folders a relative path can mean: the agents of one orchestrator mostly share them
    for f in agents:
        text = f.spawn_prompt or ''
        if not text:
            continue
        mine, named = [], False
        key = (f.cwd, f.launcher_cwd)
        if key not in bases_of:
            bases_of[key] = tuple(_bases(f, tops_of(f.cwd)))
        told, wrote = [], []
        for cls, cands in doc_paths(text, bases_of[key]):
            if cls == 'write':
                named = True
                told.append(list(cands))
                if len(cands) > 1:                              # several bases: the one whose folder is there, when only one is
                    cands = [c for c in cands if os.path.isdir(os.path.dirname(c))]
                if len(cands) == 1 and cands[0] not in mine:
                    mine.append(cands[0])
                continue
            real = [c for c in cands if cat.is_file(c)]
            if len(real) == 1:
                guides[real[0]][f.id] = f
            elif not real:
                named = True                                    # a document it names that is not there: one it is to make
        for word, strong in modified_paths(text):
            cands = _word_candidates(f, word, tops_of)
            if strong or any(os.path.exists(c) for c in cands):
                told.append(cands)
        for path, ok in f.writes:
            named = True
            if not write_failed(path, ok):
                if path not in f.shell_only:
                    wrote.append(path)
                if path.endswith('.md') and path not in mine:
                    mine.append(path)
        own[f.id] = mine
        anywhere[f.id] = named or bool(f.planned)                 # what a launch command saves (`planned`) says the agent has a file somewhere, not that it changed the work
        changes[f.id] = (told, wrote)
    out, taken = [], set()
    made = {p for paths in own.values() for p in paths}                  # the files some agent is told to write or wrote
    for guide, by_id in sorted(guides.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        folder = os.path.dirname(guide)
        pointers = sorted(by_id.values(), key=lambda f: (f.start or 0, f.id))
        if len(pointers) < 2 or folder in taken or guide in made:
            continue
        unit = cat.unit_at(folder)
        if _is_common(folder) or (unit is not None and (unit.kind != 'flat' or unit.declared_reports)):
            continue
        counts = collections.Counter(p for f in pointers for p in own[f.id])
        files = {}
        for f in pointers:
            ok = [p for p in own[f.id] if counts[p] == 1 and p != guide and _in_reach(p, folder) and os.path.basename(p) not in GUIDE_NAMES
                  and round_of(os.path.basename(os.path.dirname(p))) is None and (os.path.dirname(p) == folder or cat.unit_at(os.path.dirname(p)) is None)]
            if ok:
                files[f.id] = ok[0]
        cells = len(files) >= 2
        group = _present_together([f for f in pointers if f.id in files] if cells else pointers, statuses)        # who could be in it, and were there at the same time
        if len(group) < 2:
            continue
        guide_top = repo_top(folder)                           # the repositories of the work: the guide's and each agent's own; none when the guide is in no repository (then only a scratch folder is left out)
        outside = sum(1 for f in group if _changes_elsewhere(changes[f.id], folder, {t for t in (guide_top, *tops_of(f.cwd)) if t} if guide_top else ()))
        if 2 * outside > len(group):
            continue
        if cells:
            out.append(Room(folder, guide, 'cells', [f.id for f in group], {f.id: files[f.id] for f in group}))
        elif _talk(group) and not any(anywhere[f.id] or _names_a_missing_path(f, tops_of) for f in pointers):
            out.append(Room(folder, guide, 'members', [f.id for f in group]))
        else:
            continue
        taken.add(folder)
    return out


def _names_a_missing_path(f, tops_of):
    """Whether the first instruction names a path (not a `.md` name) that is not there, other than in a quotation or a negation: a file it is told to make, whatever it is."""
    return any(cls not in ('quoted', 'negated') and not any(os.path.exists(c) for c in _word_candidates(f, word, tops_of)) for word, cls in other_paths(f.spawn_prompt or ''))


def _word_candidates(f, word, tops_of):
    """The places a path written in an instruction can mean for agent f: itself when it is absolute or ~, else under each base."""
    if word.startswith(('/', '~')):
        return [expand(word)]
    return [os.path.normpath(os.path.join(b, word)) for b in _bases(f, tops_of(f.cwd))]


def _talk(pointers):
    """Whether some of the agents sent a message to another of them (the orchestrator's messages to each of them are no meeting)."""
    ids = {f.id for f in pointers}
    return any(to in ids and to != f.id for f in pointers for to in f.sent_to)


# ---------------------------------------------------------------------------------------------------------------------
# the judgment
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class AgentFacts:
    """What the seat judgment needs to know of one agent. The collector (debates.facts_of) fills it from the agent; the judgment reads nothing else."""
    id: str
    key: str = ''                       # the description tag, normalised (A, opus1); '' for none
    cwd: str = ''                       # the agent's own working folder ('' when unknown)
    launcher_cwd: str = ''              # the working folder of the session that started it ('' when unknown)
    start: float = 0.0                  # when it was started (the call that spawned it, else its first record)
    last: float = 0.0                   # its last record
    spawn_prompt: str = ''
    texts: List[str] = field(default_factory=list)                       # the spawn prompt first, then what it was sent, then the orchestrator's messages
    reads: List[str] = field(default_factory=list)                       # files it read (absolute)
    writes: List[Tuple[str, Optional[bool]]] = field(default_factory=list)   # (path, write_ok): True/False once the tool result is known, None while it is not
    planned: List[str] = field(default_factory=list)                     # report files a launch command was told to fill (Codex -o, a shell redirect): absolute paths
    planned_ops: Dict[str, str] = field(default_factory=dict)            # path -> how the launch command fills it ('-o', '>', '>>'); a path that is not in it is of unknown kind
    unresolved: bool = False                                             # a launch command names its output with a variable that could not be resolved (`path_unresolved`)
    sent_to: List[str] = field(default_factory=list)                     # the ids of the agents of the same session it sent a message to (SendMessage) that went (not one that failed): who it talks with
    first: float = 0.0                                                   # the time of its own first record (0 when unknown): when it began to work, as against `start`, when it was asked to
    shell_only: frozenset = frozenset()                                  # the paths among `writes` that only a shell command is known to have written (a redirect, `tee`): no change of the work


@dataclass
class Judgement:
    """The result of `assign`. `slots` is {(unit, round|None, stem): [agent ids]} (an agent that was taken over is still listed, in start order);
    `folders` {(unit, round, stem): {agent id: round folder}} for the seats of a round that has several physical folders (r01 next to r1): each of those agents
    holds the file in its own folder (the seats of a round with one folder are not listed); `agent_units` the units where an agent holds a seat; `worked` the
    debates an agent works in (where it sits, and where it only reads, is held, or names a report without quoting or negating it); `members` every unit an agent
    is tied to, a quotation or a guide it merely names too; `units` every Unit tied to some agent (plus the extra ones); `listed` the unit paths that are shown
    as debates: those an agent names by a report (a path, a read, a write, a seat), a flat review it names by its guide, and the extra ones; `unit_ts` the latest
    start of an agent tied to a unit; `diag` plain dicts {code, agent, unit, detail} that carry no text of any record; `rooms` the folders the structure of the records
    makes a room of (find_rooms) and `room_files` the file of each seat of a room of cells."""
    assignments: List[Assignment] = field(default_factory=list)
    slots: Dict[tuple, List[str]] = field(default_factory=dict)
    agent_units: Dict[str, Set[str]] = field(default_factory=dict)
    members: Dict[str, Set[str]] = field(default_factory=dict)
    worked: Dict[str, Set[str]] = field(default_factory=dict)
    units: Dict[str, Unit] = field(default_factory=dict)
    listed: Dict[str, None] = field(default_factory=dict)
    hinted: Dict[str, None] = field(default_factory=dict)                            # the listed folders that are listed only because the orchestrator wrote there (no agent, no walk): the list shows them behind the debates that have agents
    unit_ts: Dict[str, float] = field(default_factory=dict)
    diag: List[dict] = field(default_factory=list)
    folders: Dict[tuple, Dict[str, str]] = field(default_factory=dict)
    rooms: Dict[str, 'Room'] = field(default_factory=dict)                           # folder -> the room the structure of the records makes of it
    room_files: Dict[Tuple[str, str], str] = field(default_factory=dict)             # (room folder, seat) -> the absolute path of that seat's file


def _diag(out, code, agent=None, unit=None, detail=None):
    row = {'code': code, 'agent': agent, 'unit': unit, 'detail': detail}
    if row not in out:
        out.append(row)


def report_of_path(path):
    """(folder, round, stem, round folder name) of an absolute report path <folder>/<r|round><N>/<stem>.md, or None."""
    if not path.endswith('.md'):
        return None
    m = LETTER_PATH_RE.match(path)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3), path[len(m.group(1)) + 1:].split('/')[0]


def write_failed(path, ok):
    """A write that is not a seat: its result said it failed, or its result is not known and it left no file. Where the tool result is not tracked (ok is None)
    the file on disk is the stand-in for it."""
    return ok is False or (ok is None and not os.path.exists(path))


class _Claim:
    """One agent's claim on one seat: the unit, the round (None for a flat review), the stem and the rank of the evidence (1 intent, 2 marker, 3 write, 4 tag
    together with a write)."""
    __slots__ = ('unit', 'rnd', 'stem', 'rank', 'kind', 'spelled', 'via')

    def __init__(self, unit, rnd, stem, rank, kind, spelled=None, via=None):
        self.unit, self.rnd, self.stem, self.rank, self.kind, self.spelled, self.via = unit, rnd, stem, rank, kind, spelled, via


class _Agent:
    """Everything `assign` works out about one agent before the seats are compared."""

    def __init__(self, f):
        self.f = f
        self.claims = []                             # seat claims that name their seat (rank 1 and 3)
        self.named = collections.defaultdict(set)    # unit path -> {'spawn', 'text', 'write', 'read'}: how the agent is tied to the unit
        self.units = {}                              # unit path -> Unit
        self.work = set()                            # unit paths it works in: a seat, a report it reads or writes, a path it is told to read or write
        self.report = {}                             # unit paths it names by a report, in the order it names them (a path, a write, a seat): a guide alone does not list a debate
        self.veto = collections.defaultdict(set)     # unit path -> {'quote', 'negation', 'report_read'}: what says the agent is not a participant there
        self.failed = set()                          # unit paths where its report write failed
        self.letter, self.src = _letter_of(f)
        self.held = []                               # (code, unit) for a path it names that cannot be placed, or a seat it cannot be given
        self.read_only = any(read_only_text(t) for t in f.texts if t)
        self.unresolved_write = False                # told to write a report at a path that starts with a variable
        self.unplaced = collections.defaultdict(set)  # round -> stems of the reports it is told to write at a path that could not be placed (a variable, two debates fit)
        self.unsat = {}                              # unit paths of flat reviews it writes a file in although the guide declares no result file

    def tie(self, u, kind, report=True):
        self.named[u.path].add(kind)
        self.units[u.path] = u
        if report:
            self.report.setdefault(u.path)
        if u.kind == 'flat':                            # a flat review has no report folder to name: its guide is where an agent works
            self.work.add(u.path)


@functools.lru_cache(maxsize=REMEMBER_TEXTS)
def _marker_of(text):
    """The seat letter of the first marker that opens a spawn prompt (`text` is its first MARKER_SCAN + QUOTE_MAX characters, so that a quotation which starts
    inside the scanned part can be seen to close). A marker inside a quotation (an example of what an instruction looks like), a code fence or a block quote, or one that is negated is not a seat."""
    for m in MARKER_RE.finditer(text):
        if m.start() >= MARKER_SCAN:
            break
        if in_dead_span(text, m.start()) or _enclosing_quote(text, m.start(), m.end()) or MARKER_NEG_AFTER_RE.match(text[m.end():m.end() + 12]) or MARKER_NEG_BEFORE_RE.search(text[max(0, m.start() - 20):m.start()]):
            continue
        return next(g for g in m.groups() if g)
    return ''


def _letter_of(f):
    """(seat letter, source) of an agent: the marker that opens its spawn prompt, else a one-letter description tag."""
    marker = _marker_of((f.spawn_prompt or '')[:MARKER_SCAN + QUOTE_MAX])
    if marker:
        return marker, 'marker'
    if len(f.key) == 1 and f.key.isalpha():
        return f.key.upper(), 'tag'
    return '', ''


def _distinct(units):
    """Units without those that are one folder reached two ways (a link): the first of each, in order."""
    out, seen = [], set()
    for u in units:
        r = u.aliases[0] if u.aliases else u.path                     # read_unit keeps the real path of a folder reached through a link
        if r not in seen:
            seen.add(r)
            out.append(u)
    return out


def _bases(f, tops):
    """The folders a relative path in an instruction can be meant from: where the agent works, where the session that started it works, the git top."""
    out = []
    for b in (f.cwd, f.launcher_cwd) + tuple(tops):
        if b and os.path.isabs(b) and os.path.normpath(b) not in out:
            out.append(os.path.normpath(b))
    return out


def _apply(ag, kind, mn, u, cat):
    """What a report path that fits the debate folder `u` is for, by the words around it."""
    if u.kind == 'flat' and mn.cls in ('write', 'read', 'ref'):
        u = cat.promote(u.path)                                      # a report under a round folder: the guide declared no rounds, the instruction does
    ag.tie(u, kind)
    if mn.cls in ('write', 'read', 'ref'):
        ag.work.add(u.path)
    if mn.cls == 'write':
        ag.claims.append(_Claim(u.path, mn.rnd, mn.stem, 1, 'write_intent', mn.dirname))
    elif mn.cls == 'quoted':
        ag.veto[u.path].add('quote')
    elif mn.cls == 'negated':
        ag.veto[u.path].add('negation')
    elif mn.cls == 'read':
        ag.veto[u.path].add('report_read')


def _collect(f, cat, tops_of):
    """Ties one agent to debate folders and works out its explicit claims from its texts, writes, reads and planned output files."""
    ag = _Agent(f)
    kind_of = lambda i: 'spawn' if i == 0 else 'text'                 # noqa: E731
    parsed = [find_mentions(t or '') for t in f.texts]
    for path, ok in f.writes:                                         # a report it really wrote names the debate it works in (a failed write is not that)
        rp = report_of_path(path)
        u = cat.unit_at(rp[0]) if rp else None
        if u is not None:
            if write_failed(path, ok):
                ag.failed.add(u.path)
                ag.units[u.path] = u
                ag.report.setdefault(u.path)
            else:
                u = cat.promote(u.path) if u.kind == 'flat' else u
                ag.tie(u, 'write')
                ag.work.add(u.path)
                ag.claims.append(_Claim(u.path, rp[1], rp[2], 3, 'write_ok', rp[3]))
    for path in f.reads:
        rp = report_of_path(path)
        u = cat.unit_at(rp[0]) if rp else None
        if u is not None:
            ag.tie(u, 'read', report=False)                           # reading a report works in the debate but does not make it the agent's debate to list
            ag.work.add(u.path)
            ag.veto[u.path].add('report_read')
        elif os.path.basename(path) in GUIDE_NAMES:
            u = cat.unit_at(os.path.dirname(path))
            if u:
                ag.tie(u, 'read', report=False)
    plans = []                                                        # (unit, parsed path, path) of the report files a launch command is told to fill: judged below
    for path in f.planned:
        rp = report_of_path(path)
        u = cat.unit_at(rp[0]) if rp else None
        if u is not None:
            plans.append((u, rp, path))
    if f.unresolved:
        ag.held.append(('path_unresolved', None))
    bases = None
    for i, t in enumerate(f.texts):                                   # the debate folders a text names by an absolute, ~ or folder-prefixed guide path
        for form, prefix in guide_mentions(t):
            if form == 'abs':
                u = cat.unit_at(expand(prefix))
                if u:
                    ag.tie(u, kind_of(i), report=False)
            elif form == 'dir' and (f.cwd or f.launcher_cwd):
                bases = bases or _bases(f, tops_of(f.cwd))
                cands = _distinct([u for u in (cat.unit_at(os.path.join(b, prefix)) for b in bases) if u])
                if len(cands) == 1:
                    ag.tie(cands[0], kind_of(i), report=False)
            elif form == 'bare':
                cands = _distinct([u for u in (cat.unit_at(b) for b in (f.cwd, f.launcher_cwd) if b) if u])
                if len(cands) == 1:
                    ag.tie(cands[0], kind_of(i), report=False)
        for mn in parsed[i]:
            if mn.form == 'abs':
                u = cat.unit_at(expand(mn.prefix))
                if u:
                    _apply(ag, kind_of(i), mn, u, cat)
    explicit = [ag.units[p] for p in ag.report if p in ag.units]     # debates it names by a report path or works on by a write: a relative path may lean on them (a guide may name the root)
    explicit += [u for u, _rp, _path in plans if u.path not in ag.report]        # and the ones its launch command writes in
    for i, mentions in enumerate(parsed):
        for mn in mentions:
            if mn.form == 'abs':
                continue
            if mn.form == 'var':
                ag.held.append(('path_unresolved', None))
                ag.unresolved_write = ag.unresolved_write or mn.cls == 'write'
                if mn.cls == 'write':
                    ag.unplaced[mn.rnd].add(f.texts[i][mn.start:mn.end].rsplit('/', 1)[-1][:-3])
                continue
            if mn.form == 'dir':
                bases = bases or _bases(f, tops_of(f.cwd))
                cands = [cat.unit_at(os.path.join(b, mn.prefix)) for b in bases]
            else:                                                    # r1/B.md: from where the agent works; the debates its text names only when that is no debate
                cands = _distinct([c for c in (cat.unit_at(b) for b in (f.cwd, f.launcher_cwd) if b) if c]) or explicit
            cands = _distinct([c for c in cands if c])
            if len(cands) > 1:
                ag.held.append(('path_ambiguous', None))
                if mn.cls == 'write':
                    ag.unplaced[mn.rnd].add(mn.stem)
            elif cands:
                _apply(ag, kind_of(i), mn, cands[0], cat)
    if plans:
        _plan_claims(ag, plans, f.planned_ops, cat)
    if f.unresolved or ag.unresolved_write:                           # it is to write a report it cannot place: it works in the debate whose guide it names
        ag.work.update(ag.named)
    return ag


def _file_folder(unit, rnd, folder, told=False):
    """The round folder that tells one physical file of a seat from its twin where a round has several (r01 next to r1): the folder as written, '' where the round
    has one folder and the spelling tells nothing. A folder a path *tells* (`told`) that is none of the real ones cannot say which file it means: ''."""
    dirs = unit.rounds.get(rnd) if unit is not None and rnd is not None else None
    if not dirs or len(dirs) < 2 or (told and folder not in dirs):
        return ''
    return folder


def _plan_claims(ag, plans, ops, cat):
    """The launch command's output files as write intents. An output file counts as the agent's report only when it fits what the instruction says to write:
    when the instruction names a report for the same round (in that debate, or at a path that could not be placed) and the output is another file, it is an
    auxiliary one (`-o B_last.md` next to `r1/B.md`) and seats nobody. The files are compared by their whole path: the same name in the round's other folder
    (`r1/B.md` told, `-o r01/B.md` given) is another file. An instruction that names no report leaves the output file as the only evidence."""
    told = collections.defaultdict(set)                               # (unit path | None, round) -> {(stem, folder)} the instruction says to write there
    for c in ag.claims:
        if c.kind == 'write_intent':
            told[(c.unit, c.rnd)].add((c.stem, _file_folder(ag.units.get(c.unit), c.rnd, c.spelled, told=True)))
    for rnd, stems in ag.unplaced.items():
        told[(None, rnd)] |= {(stem, '') for stem in stems}
    for u, rp, path in plans:
        here = told.get((u.path, rp[1]), set()) | told.get((None, rp[1]), set())
        folder = _file_folder(u, rp[1], rp[3])
        if here and not any(stem == rp[2] and (not at or not folder or at == folder) for stem, at in here):
            continue
        u = cat.promote(u.path) if u.kind == 'flat' else u
        ag.tie(u, 'text')
        ag.work.add(u.path)
        ag.claims.append(_Claim(u.path, rp[1], rp[2], 1, 'redirect', rp[3], ops.get(path)))


def _unit_stems(cat, unit, claims):
    """Report stems already known in a unit: the files in its round folders and the seats claimed so far."""
    stems = {c.stem for _f, c in claims if c.unit == unit.path and c.rnd is not None}
    for dirs in unit.rounds.values():
        for d in dirs:
            stems.update(cat.md_stems(os.path.join(unit.path, d)))
    return stems


def _round_hint(f):
    """The round the latest instruction names (a letter seat has no path to say it); 1 if none."""
    for t in reversed(f.texts):
        m = ROUND_RE.search(t or '')
        if m:
            return int(next(g for g in m.groups() if g))
    return 1


def _letter_claim(f, ag, cat, jd, claims):
    """The claim of an agent that has no explicit one: the seat marker (rank 2), or the description tag (rank 4) together with a write it succeeded in. A letter
    seats in ONE debate (a marker does not spread to the other debates a text mentions); two debates at the same weight hold it. A quotation, a negation or a
    read-only instruction takes the tag's seat away; the tag alone never seats, and neither does a write whose path could not be resolved."""
    if not ag.letter:
        return None
    marker = ag.src == 'marker'
    if not marker or not ag.named:                                    # a write inside a debate folder that is not a report (notes.txt) is the tag's write
        for path, ok in f.writes:
            u = cat.unit_above(path)
            if u and not write_failed(path, ok):
                ag.tie(u, 'write')
    score = {}
    for p, kinds in ag.named.items():
        u = ag.units[p]
        if u.kind == 'flat':
            continue
        veto = ag.veto.get(p, set())
        if marker:
            if veto & {'quote', 'negation'} and not kinds & {'spawn', 'write'}:
                continue
        elif veto & {'quote', 'negation'} or ag.read_only or 'write' not in kinds:         # a path it could not place is no write: the tag needs one that happened
            continue
        score[p] = 3 * ('spawn' in kinds) + 3 * ('write' in kinds) + 2 * ('text' in kinds) + ('read' in kinds)
    if not score:
        return None
    best = max(score.values())
    top = [p for p, s in score.items() if s == best]
    if len(top) > 1:
        ag.held.append(('seat_tie_held', None))
        return None
    u = ag.units[top[0]]
    stem = stem_aliases(_unit_stems(cat, u, claims)).get(ag.letter, ag.letter)
    return _Claim(u.path, _round_hint(f), stem, 2 if marker else 4, 'own_marker' if marker else 'tag')


def _flat_claims(agents, works, cat, jd, tops_of):
    """Seats of a flat review: a declared result file the agent is told to write, or wrote. A review that declares none gets no seat, and a file written
    there is only noted (`declaration_missing`). A file name in a text is judged as one path: the reviews it can mean are collected first, and only one
    gives the seat; two that both declare the file hold it (`path_ambiguous`). A write that succeeded names its own folder and is judged apart."""
    for f in agents:
        ag = works[f.id]
        tied = {p: u for p, u in ag.units.items() if u.kind == 'flat'}
        for b in (f.cwd, f.launcher_cwd):
            u = cat.unit_at(b) if b else None
            if u and u.kind == 'flat':
                tied.setdefault(u.path, u)
        if not tied:
            continue
        wdirs = {os.path.dirname(path) for path, _ok in f.writes if path.endswith('.md')}
        declared = {}
        for p, u in tied.items():
            if u.declared_reports:
                declared[p] = u
            elif p in wdirs and any(os.path.dirname(path) == p and path.endswith('.md') and os.path.basename(path) not in GUIDE_NAMES for path, _ok in f.writes):
                ag.unsat[p] = None                                   # a result file written in a review that declares none: nothing to seat, a diagnostic
            else:
                for t in f.texts:
                    for start, end, pre, stem in md_mentions(t or ''):
                        if (stem + '.md') not in GUIDE_NAMES and p in _folders_of(f, pre, tops_of) and mention_class(t, start, end) == 'write':
                            ag.unsat[p] = None
            if p in ag.unsat:
                ag.report.setdefault(p)                              # it is listed (title and diagnostic) only where someone writes in it
        folders = {}
        for i, t in enumerate(f.texts):
            for start, end, pre, stem in (md_mentions(t) if declared and t else ()):
                if pre not in folders:
                    folders[pre] = _folders_of(f, pre, tops_of)
                fits = _distinct([u for p, u in declared.items() if stem in u.declared_reports and p in folders[pre]])
                if not fits:
                    continue
                cls = mention_class(t, start, end)
                if len(fits) > 1 and cls not in ('quoted', 'negated'):
                    ag.held.append(('path_ambiguous', None))          # the file name fits two reviews that both declare it
                    continue
                for u in fits:
                    p = u.path
                    ag.tie(u, 'spawn' if i == 0 else 'text')
                    if cls not in ('quoted', 'negated'):
                        ag.work.add(p)
                    if cls == 'write':
                        ag.claims.append(_Claim(p, None, stem, 1, 'write_intent'))
                    elif cls in ('quoted', 'negated'):
                        ag.veto[p].add('quote' if cls == 'quoted' else 'negation')
                    elif cls == 'read':
                        ag.veto[p].add('report_read')
        for p, u in declared.items():
            for path, ok in f.writes:
                if path.endswith('.md') and os.path.dirname(path) == p and os.path.basename(path)[:-3] in u.declared_reports:
                    if write_failed(path, ok):
                        ag.failed.add(p)
                    else:
                        ag.tie(u, 'write')
                        ag.work.add(p)
                        ag.claims.append(_Claim(p, None, os.path.basename(path)[:-3], 3, 'write_ok'))


def _folders_of(f, pre, tops_of):
    """The folders a file name written with `pre` in front of it can mean: itself when it is absolute or ~, else from each base."""
    if pre.startswith(('/', '~')):
        return [expand(pre)]
    return [os.path.normpath(os.path.join(b, pre)) for b in _bases(f, tops_of(f.cwd))]


def _seat_rooms(rooms, by_id, works, cat, jd):
    """Makes each room the Unit of its folder for this build and gives the members of a room of cells the seat of the file they have: the letter their instruction
    names (or their description tag), else the file's name. The claim is of the weight of the evidence: the file told (1) or written (3). The members of a room of
    participants only work there and hold no seat. A room has one round and no round folder."""
    for room in rooms:
        unit = Unit(id=room.folder, path=room.folder, brief=room.guide)
        real = os.path.realpath(room.folder)
        if real != room.folder:
            unit.aliases.append(real)
        cat.add_room(unit)
        jd.rooms[room.folder] = room
        seats = {aid: works[aid].letter or os.path.splitext(os.path.basename(path))[0] for aid, path in room.files.items()}
        taken = collections.Counter(seats.values())
        for aid, path in room.files.items():
            if taken[seats[aid]] > 1:                               # two files of one name (`left/report.md`, `right/report.md`), or one letter named twice: the file below the folder
                seats[aid] = os.path.splitext(os.path.relpath(path, room.folder))[0]
        for aid in room.members:
            ag = works[aid]
            ag.work.add(room.folder)
            ag.report.setdefault(room.folder)
            path = room.files.get(aid)
            if path is None:
                continue
            seat = seats[aid]
            written = any(p == path and not write_failed(p, ok) for p, ok in by_id[aid].writes)
            ag.claims.append(_Claim(room.folder, 1, seat, 3 if written else 1, 'write_ok' if written else 'write_intent'))
            jd.room_files[(room.folder, seat)] = path


def listing_hint(path, is_dir=False):
    """The folder an orchestrator's own successful write says to look at, or None. `path` is absolute. A guide written (`<D>/brief.md`, `README.md`, `index.md`), a report under a
    round folder (`<D>/<r|round N>/<x>.md`) or a round folder made (`<D>/<r|round N>`, `is_dir`) is for D. Nothing else is: not a plan, a note or a closing document, not an
    arbitrary folder, and no folder above D is looked at. The name settles nothing: `written_debate` asks the disk."""
    if not path or not os.path.isabs(path):
        return None
    path = os.path.normpath(path)
    name = os.path.basename(path)
    if is_dir:
        return os.path.dirname(path) if round_of(name) is not None else None
    if name in GUIDE_NAMES:
        return os.path.dirname(path)
    rep = report_of_path(path)
    return rep[0] if rep else None


_OWN_TOP = []


def own_top():
    """The top of the repository this program runs from (a checkout), or None (an installed copy has none). Tests of the program keep their fixtures there."""
    if not _OWN_TOP:
        _OWN_TOP.append(repo_top(os.path.dirname(os.path.abspath(__file__))))
    return _OWN_TOP[0]


def written_debate(u):
    """Whether the folder `u` (a Unit) that the orchestrator wrote into may be listed as a debate: its shape is a guide with exact round folders on disk, or a brief.md that declares
    two result files or more (a flat review: `declared_reports`), and it is not one of the places a guide is a document of its own: a folder that is too broad, what the tools
    keep (`STATE_DIRS`), this program's own repository, and, for a README.md or an index.md, the top of a repository or its `docs`. A brief.md alone is not a debate here, whatever
    its text says of rounds."""
    d = u.path
    if too_broad(d) or d.startswith(STATE_DIRS):
        return False
    own = own_top()
    if own and (d == own or d.startswith(own + os.sep)):
        return False
    guide = os.path.basename(u.brief) if u.brief else None
    if not ((u.rounds and guide) or (u.kind == 'flat' and guide == 'brief.md' and len(u.declared_reports) >= 2)):
        return False
    return guide == 'brief.md' or not _is_common(d)


def assign(agents, cat, statuses=None, extra_units=(), tops_of=None, hinted=None):
    """Who sits where. `agents` are AgentFacts, `cat` the Catalog, `statuses` {agent id: status} ('running', 'done', 'interrupted' ...), `extra_units` the folders a walk
    of the repository found, `tops_of` a function from a working folder to the folders above it a relative path may be meant from (the git top), `hinted` {folder: time of
    the orchestrator's last successful write there} (listing_hint): the folders that pass `written_debate` are listed, and only listed (`listed`, `units`, `unit_ts` with the
    later of the two times); no seat, member, room or agent unit comes from them.
    Returns a Judgement. Pure with respect to the agents: the only reads are the debate folders themselves."""
    statuses = statuses or {}
    tops_of = tops_of or (lambda cwd: ())
    jd = Judgement()
    cat.begin()
    works = {f.id: _collect(f, cat, tops_of) for f in agents}
    by_id = {f.id: f for f in agents}
    _flat_claims(agents, works, cat, jd, tops_of)
    _seat_rooms(find_rooms(agents, cat, tops_of, statuses), by_id, works, cat, jd)
    claims = [(f, c) for f in agents for c in works[f.id].claims]
    for f in agents:                                                 # letter seats come after the explicit ones: they take the alias of a stem the others made known
        ag = works[f.id]
        if not ag.claims:
            c = _letter_claim(f, ag, cat, jd, claims)
            if c:
                claims.append((f, c))
                ag.report.setdefault(c.unit)
                ag.work.add(c.unit)
    for f in agents:                                                 # who is tied to which debate: whatever an agent names, a quotation too
        ag = works[f.id]
        for p, u in ag.units.items():
            jd.members.setdefault(f.id, set()).add(p)
            jd.units[p] = u
            if p in ag.report:                                       # a guide it merely names does not make the debate the latest one
                jd.unit_ts[p] = max(jd.unit_ts.get(p, 0), f.start or 0)
        jd.listed.update(ag.report)
        if ag.work:
            jd.worked[f.id] = set(ag.work)
    for folder, room in jd.rooms.items():                            # a room is what its folder is, also where a lone brief.md was read as a flat review
        jd.units[folder] = cat.unit_at(folder)
        jd.listed.setdefault(folder)
        jd.unit_ts[folder] = max(jd.unit_ts.get(folder, 0), max(by_id[aid].start or 0 for aid in room.members))
        for aid in room.members:
            jd.members.setdefault(aid, set()).add(folder)
    for p in extra_units:
        u = cat.unit_at(p)
        if u:
            jd.units.setdefault(u.path, u)
            jd.unit_ts.setdefault(u.path, 0)
            jd.listed.setdefault(u.path)
    for p, ts in (hinted or {}).items():
        u = cat.unit_at(p)
        if u and written_debate(u):
            if u.path not in jd.listed:
                jd.hinted.setdefault(u.path)
            jd.units.setdefault(u.path, u)
            jd.unit_ts[u.path] = max(jd.unit_ts.get(u.path, 0), ts or 0)
            jd.listed.setdefault(u.path)
    _settle(agents, works, claims, statuses, jd, cat)
    return jd


def _over_before(g, status, f):
    """Whether agent g was already over when agent f started (so f takes the seat over rather than competing for it): its last record is not later than f's start and
    it is not working. A run that was cut off (`interrupted`: a usage limit, an error, an early exit) is over for this, however it may be resumed: a new run of the same
    script started after its last record takes the seat (the claim of the new one is no weaker than the old one's, see `_settle`). If the cut-off run is resumed later
    and works after f has started, g's last record is later than f's start and the two overlap: the seat is held, as it is for any two agents at work at once; what it
    writes after that is a claim like any other write. One whose process cannot be seen (`unknown`) may still be working: it is not over."""
    return status is not None and status not in OPEN and (g.last or g.start or 0) <= (f.start or 0)


def _folder_of(c, units):
    """The real round folder a claim is for, when its round has several (r01 next to r1): the folder the claim spells, or '' when the claim does not say which
    (a marker, or a spelling no folder has). None when the round has one folder or none."""
    u = units.get(c.unit)
    dirs = u.rounds.get(c.rnd) if u is not None and c.rnd is not None else None
    if not dirs or len(dirs) < 2:
        return None
    return c.spelled if c.spelled in dirs else ''


def _settle(agents, works, claims, statuses, jd, cat):
    """Compares the claims on each seat: a claimant that is over when a later one starts is taken over (its assignment is closed) unless the later claim is
    weaker, which never takes a seat from a stronger one; of the claimants that remain the best rank keeps the seat, and two of the same rank hold it. A seat
    is a file: where a round has two folders (r01 next to r1) a claim that spells one is for that file, and a claim that cannot tell which is held (`alias_collision`).
    One agent has one file of a seat, never two: where it claims both, the stronger claim's file, and none (held) when they are as strong.
    Fills slots, assignments, agent_units and the diagnostics."""
    held = set()
    rows, files = [], collections.OrderedDict()                      # (agent, claim, folder); (agent id, unit, round, stem) -> {folder: strongest rank}
    for f, c in claims:
        folder = _folder_of(c, jd.units)
        if folder == '':
            held.add(f.id)                                           # the unit's own `alias_collision` below says why
            continue
        rows.append((f, c, folder))
        seen = files.setdefault((f.id, c.unit, c.rnd, c.stem), {})
        seen[folder] = min(seen.get(folder, c.rank), c.rank)
    giveup = set()                                                   # one agent has one file of a seat: its strongest claim's, and none where two files are as strong
    for (aid, unit, rnd, stem), seen in files.items():
        if len(seen) > 1:
            keep = [fo for fo, rank in seen.items() if rank == min(seen.values())]
            if len(keep) > 1:
                held.add(aid)
                keep = []
            giveup.update((aid, unit, rnd, stem, fo) for fo in seen if fo not in keep)
    slot_claims = collections.OrderedDict()
    for f, c, folder in rows:
        if (f.id, c.unit, c.rnd, c.stem, folder) not in giveup:
            slot_claims.setdefault((c.unit, c.rnd, c.stem, folder), []).append((f, c))
    for key, cs in slot_claims.items():
        best = {}
        for f, c in cs:                                              # per agent its strongest claim on this seat
            if f.id not in best or c.rank < best[f.id][1].rank:
                best[f.id] = (f, c)
        alive, closed = [], []
        for f, c in sorted(best.values(), key=lambda fc: (fc[0].start or 0, fc[0].id)):
            still = []
            for g, d in alive:
                (closed.append((g, d, f.start)) if c.rank <= d.rank and _over_before(g, statuses.get(g.id), f) else still.append((g, d)))
            alive = still + [(f, c)]
        top = min(d.rank for _g, d in alive)
        winners = [(g, d) for g, d in alive if d.rank == top]
        unit, rnd, stem, folder = key
        if len(winners) > 1:
            for g, d in winners:
                held.add(g.id)
                _diag(jd.diag, 'seat_tie_held', g.id, unit, '%s/%s' % (rnd if rnd is not None else '-', stem))
            continue
        real = folder or (jd.units[unit].rounds[rnd][0] if rnd is not None and jd.units[unit].rounds.get(rnd) else None)
        slot = (unit, rnd, stem)
        for g, d, end in sorted(closed + [(g, d, None) for g, d in winners], key=lambda x: (x[0].start or 0, x[0].id)):
            cand = [unit, rnd, stem, real] + ([d.via] if d.via else [])           # the file itself: its round folder as it is on disk, and how a launch command fills it
            ev = Evidence(kind=d.kind, subject=g.id, field='seat', candidates=cand, rank=d.rank)
            jd.assignments.append(Assignment(agent=g.id, unit=unit, round=rnd, seat=stem, start=g.start, end=end, evidence=[ev]))
            if g.id not in jd.slots.setdefault(slot, []):
                jd.slots[slot].append(g.id)
            if folder:
                jd.folders.setdefault(slot, {})[g.id] = folder
            jd.agent_units.setdefault(g.id, set()).add(unit)
    for f in agents:
        ag = works[f.id]
        for code, unit in ag.held:
            if code == 'seat_tie_held':
                held.add(f.id)
            _diag(jd.diag, code, f.id, unit)
        mem = [p for p in jd.members.get(f.id, ()) if jd.units[p].kind != 'flat' and p not in jd.rooms]
        if f.id in jd.agent_units or f.id in held or not mem:
            continue
        reader = any('report_read' in ag.veto.get(p, ()) for p in mem)
        quoted = any(ag.veto.get(p, set()) & {'quote', 'negation'} for p in mem)
        if (reader and not quoted and not ag.failed) or not (quoted or ag.failed or ag.letter) or ((f.unresolved or ag.unresolved_write) and not quoted):
            continue                                                 # a report it is told to write at a path it cannot place is held, and `path_unresolved` says so
        _diag(jd.diag, 'debate_in_misc', f.id, sorted(mem)[0])
    for p, u in jd.units.items():
        if p in jd.rooms:
            continue                                                 # a room has no round folder and no file names to collide
        if u.kind == 'flat':
            if not u.declared_reports and any(p in works[f.id].unsat for f in agents):
                _diag(jd.diag, 'declaration_missing', None, p)
            continue
        stems = _unit_stems(cat, u, [])                        # physical files only: a seat that names no file is not a collision
        for group in stem_collisions(stems).values():
            _diag(jd.diag, 'alias_collision', None, p, ','.join(group))
        for dirs in u.rounds.values():
            if len(dirs) > 1:
                _diag(jd.diag, 'alias_collision', None, p, ','.join(dirs))


# ---------------------------------------------------------------------------------------------------------------------
# the cell of a seat
# ---------------------------------------------------------------------------------------------------------------------
def cell_state(status, file_ok, working_here, has_agent):
    """The state of a report cell. `working_here`: the agent holds this round now. A working agent has a draft (the file exists) or is writing, and so does one
    whose process cannot be seen (`unknown`: nothing says it ended); an agent whose run was cut off (`interrupted`) has a draft, or is paused with no file;
    for an agent that is over for any other reason the file is the answer: done if it is there, missing if it is not; a seat without an agent waits."""
    if working_here and status in OPEN:
        return 'draft' if file_ok else 'writing'
    if working_here and status == 'interrupted':
        return 'draft' if file_ok else 'paused'
    if file_ok:
        return 'done'
    return 'missing' if has_agent else 'waiting'
