"""Path constants, text and time helpers, the follower for record files (Tail), and shared thresholds. The bottom module that every other board module relies on."""

import fnmatch
import glob
import json
import os
import re
import stat
import time
from datetime import datetime

from . import PATH_FLAGS, i18n, procs

HOME = os.path.expanduser('~')

SID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')   # session id (used with fullmatch). No path or glob characters can get through
STALL_SEC = 600          # no activity for this long makes us suspect a stall
TOOL_STALL_SEC = 1800    # a tool call waiting for its result is treated as normal up to this long


def _dir_choice(flag, env_name, default, env):
    """(folder, source): command argument > standard environment variable (CLAUDE_CONFIG_DIR, CODEX_HOME) > default. An empty value counts as unset."""
    if flag:
        return os.path.abspath(os.path.expanduser(flag)), 'flag'
    if env.get(env_name):
        return os.path.abspath(os.path.expanduser(env[env_name])), 'env'
    return default, 'default'


def resolve_paths(flags=None, env=None, home=None):
    """Decides in one place the folders to read and the files never to open. DENY_FILES, the credentials, the .claude.json candidates and the Codex path comparison all come from this result.
    flags = {'claude': folder, 'codex': folder} (command arguments). A pure function, so tests call it with fake values.

    The .claude.json location follows Claude Code's rule: ~/.claude.json by default, <folder>/.claude.json under CLAUDE_CONFIG_DIR.
    When a command argument gives a folder such as ~/.claude, and <folder>/.claude.json does not exist while the folder is named .claude, the one beside the folder (in its parent) is read.
    Every candidate goes on the deny list, whether it is read or not."""
    flags = flags or {}
    env = os.environ if env is None else env
    home = home or HOME
    claude, c_from = _dir_choice(flags.get('claude'), 'CLAUDE_CONFIG_DIR', os.path.join(home, '.claude'), env)
    codex, x_from = _dir_choice(flags.get('codex'), 'CODEX_HOME', os.path.join(home, '.codex'), env)
    own = os.path.join(claude, '.claude.json')
    beside = os.path.join(os.path.dirname(claude), '.claude.json')
    if c_from == 'default':
        read = (beside,)
    elif c_from == 'env' or os.path.basename(claude) != '.claude':
        read = (own,)
    else:
        read = (own, beside)
    return {
        'HOME': home, 'CLAUDE_HOME': claude, 'PROJECTS': os.path.join(claude, 'projects'), 'CLAUDE_JSON': read,
        'CODEX_HOME': codex, 'CODEX_SESSIONS': os.path.join(codex, 'sessions'), 'CODEX_NAMES': os.path.join(codex, 'session_index.jsonl'),
        # files that /api/file never opens under any allow rule: the three auth files (every candidate for .claude.json), and everything under ~/.codex/
        'DENY_FILES': tuple(dict.fromkeys((os.path.join(claude, '.credentials.json'),) + read + (own, beside) +
                                          (os.path.join(codex, 'auth.json'),))),
        'FROM': {'claude': c_from, 'codex': x_from},
    }


# the folders are fixed once, at import time. When server.py is run directly, the command arguments (board.PATH_FLAGS) are already in place
_R = resolve_paths(PATH_FLAGS)
CLAUDE_HOME, PROJECTS, CLAUDE_JSON = _R['CLAUDE_HOME'], _R['PROJECTS'], _R['CLAUDE_JSON']
# Codex records: read only. sqlite, auth.json, config.toml and history.jsonl are not opened, and session_meta's creator_* is not read.
CODEX_HOME, CODEX_SESSIONS, CODEX_NAMES = _R['CODEX_HOME'], _R['CODEX_SESSIONS'], _R['CODEX_NAMES']
DENY_FILES = _R['DENY_FILES']
PATH_FROM = _R['FROM']       # where the folder came from: 'flag' | 'env' | 'default'


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def trunc(s, n):
    s = s or ''
    return s if len(s) <= n else s[:n - 1] + '…'


def short_path(p):
    p = p or ''
    return '~' + p[len(HOME):] if p == HOME or p.startswith(HOME + os.sep) else p


def expand(p):
    return os.path.normpath(os.path.expanduser(p))


def norm_key(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def as_text(x):
    """Text, or a list of content blocks (input with an attached image is [{type:'text'…}, {type:'image'…}]), as text. Only the text blocks are joined."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return '\n'.join(b.get('text') or '' for b in x
                         if isinstance(b, dict) and b.get('type') in ('text', 'input_text', 'output_text'))
    return ''


def line_error(where, d, e):
    """If one line raises an exception, skip just that line. Its content is not printed."""
    ts = d.get('timestamp') if isinstance(d, dict) else None
    print(i18n.cli_t('cli.log.line_skipped', where=str(where), ts=str(ts), error=type(e).__name__), flush=True)


def strip_reminders(text):
    return re.sub(r'<system-reminder>.*?</system-reminder>', '', as_text(text) or '', flags=re.S).strip()


# ---------- three ways to handle files ----------
# 1. The way that opens content (open_safe: document view, reading brief, counting lines, -o sha1): right before opening, realpath and the deny check are redone **without any cache**.
# 2. The way that only looks at metadata (stat_regular: debate cells, the final in the brief table): it sees only existence, size and time. It opens nothing, so using the cache below leaks no content.
# 3. The way that searches a folder for candidates (stat_plain: auto_final candidates, docs, finals list): way 2 plus the dot-path and secret-name rules. It opens nothing either.
_DENY_ROOTS = {'e': None}     # (key, creation time, value): realpaths of the deny list. Only the non-opening way (stat_regular) uses this brief memory
DENY_ROOTS_TTL = 2.0          # seconds


def _deny_roots(fresh):
    """(set of realpaths of the denied files, realpath of the Claude config folder, realpath of the Codex folder). Unless fresh, one from within DENY_ROOTS_TTL is used."""
    key = (CLAUDE_HOME, CODEX_HOME, DENY_FILES)
    e = _DENY_ROOTS['e']
    now = time.monotonic()
    if fresh or e is None or e[0] != key or now - e[1] > DENY_ROOTS_TTL:
        e = (key, now, ({os.path.realpath(p) for p in DENY_FILES}, os.path.realpath(CLAUDE_HOME), os.path.realpath(CODEX_HOME)))
        if not fresh:
            _DENY_ROOTS['e'] = e
    return e[2]


VIRTUAL_ROOTS = ('/proc', '/sys', '/dev')     # kernel views and devices: what looks like a regular file there is not a document (/proc/<pid>/environ is a process's environment)
SHARED_MEMORY = '/dev/shm'                    # the exception inside /dev: a memory-backed folder that holds ordinary files, which go through the usual checks like any other


def _virtual(real):
    if real.startswith(SHARED_MEMORY + os.sep):
        try:
            return not stat.S_ISREG(os.lstat(real).st_mode)       # `real` is a realpath, so a link to a device has already become the device; a file that is not there stays refused
        except OSError:
            return True
    return any(real == r or real.startswith(r + os.sep) for r in VIRTUAL_ROOTS)


def denied_file(real, fresh=True):
    """The three auth files, ~/.claude/settings*.json (env can hold keys), everything under ~/.codex/, and everything under /proc, /sys and /dev (by realpath) except an ordinary file in /dev/shm. By default it
    is always resolved fresh (fresh). fresh=False is used only by the non-opening way (stat_regular)."""
    if _virtual(real):
        return True
    files, claude, codex = _deny_roots(fresh)
    if real in files:
        return True
    if os.path.dirname(real) == claude and fnmatch.fnmatchcase(os.path.basename(real), 'settings*.json'):
        return True
    return real == codex or real.startswith(codex + os.sep)


FILE_MAX = 2 << 20           # cap (2 MiB) on how much the document view (/api/file) and the debate state read from a file
# names that are likely to hold secrets (compared in lower case). Applied whatever the extension (credentials.md and secrets.md are refused too)
SECRET_NAMES = ('*secret*', '*credential*', '*password*', '*passwd*', '*apikey*', '*api_key*', '*api-key*',
                '*token*', 'auth.json', 'kubeconfig*',
                'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*', '*.pem', '*.key', '*.p12', '*.pfx', '*.ppk', '*.jks', '*.keystore')    # the last row: private keys and key stores


def _worktree_dot(parts):
    """Claude Code --worktree works in <repo>/.claude/worktrees/<name>/. The position of that `.claude` (an index into parts), or -1 if there is none.
    There must be more path below <name>, and it does not apply if <name> starts with a dot. It does not apply to the Claude config folder (~/.claude) itself."""
    for i in range(len(parts) - 3):
        if parts[i] == '.claude' and parts[i + 1] == 'worktrees' and not parts[i + 2].startswith('.'):
            if os.sep + os.sep.join(parts[:i + 1]) != os.path.realpath(CLAUDE_HOME):
                return i
            return -1
    return -1


def hidden_or_secret(real):
    """A path with a component that starts with a dot (~/.docker, .config/gh, .mcp.json …), or a file whose name looks like a secret.
    The only exception to the dot-path rule is the three components `.claude/worktrees/<name>/` (dot components below it, such as .env, .git and .config, stay hidden)."""
    parts = [p for p in real.split(os.sep) if p]
    ok = _worktree_dot(parts) if '.claude' in parts else -1
    if any(part.startswith('.') and i != ok for i, part in enumerate(parts)):
        return True
    name = os.path.basename(real).lower()
    return any(fnmatch.fnmatchcase(name, p) for p in SECRET_NAMES)


_REAL_DIRS = {}               # folder -> (creation time, realpath). Remembered for DENY_ROOTS_TTL seconds so that stat_regular does not resolve the same folder again for every cell


def _real_dir(d):
    now = time.monotonic()
    c = _REAL_DIRS.get(d)
    if c is None or now - c[0] > DENY_ROOTS_TTL:
        if len(_REAL_DIRS) > 1024:
            _REAL_DIRS.clear()
        c = _REAL_DIRS[d] = (now, os.path.realpath(d))
    return c[1]


def stat_regular(path, strict=False):
    """Looks at metadata only, without opening (debate cells, the final in the brief table): (realpath, os.stat result), or None for what is to count as a missing file.
    Auth files (denied_file) and anything that is not a regular file (FIFO, device, folder) count as missing. With strict, dot paths and secret names (hidden_or_secret) count as missing too
    (for candidates found by searching a folder: stat_plain). stat does not open a FIFO, so it does not block.
    It is called for every cell, so the folder's realpath and the deny list's realpath use what was remembered for DENY_ROOTS_TTL seconds (a last component that is a link is resolved fresh).
    Since nothing is opened, a stale verdict cannot leak content: open_safe checks again right before opening."""
    head, name = os.path.split(path)
    real = os.path.join(_real_dir(head), name)
    try:
        st = os.lstat(real)
        if stat.S_ISLNK(st.st_mode):
            real = os.path.realpath(real)
            st = os.stat(real)
    except OSError:
        return None
    if denied_file(real, fresh=False) or not stat.S_ISREG(st.st_mode) or (strict and hidden_or_secret(real)):
        return None
    return real, st


def stat_plain(path):
    """For candidates found by searching a folder (auto_final candidates, docs, finals list): stat_regular plus the dot-path and secret-name rules."""
    return stat_regular(path, strict=True)


class Denied(OSError):
    """Refused, so it was not opened. why: 'denied' (auth files, settings, the Codex folder) | 'hidden' (dot path, secret name)."""

    def __init__(self, why):
        OSError.__init__(self, 'not allowed')
        self.why = why


def via_link(path, real):
    """Whether the file itself is reached through a link: the last component is a link, or the file name of the realpath differs. A file in place that only has a folder link on the way is not
    (a folder link reaches only a file of the same name. To pull in a hidden or secret file of another name, the file itself would have to be a link)."""
    ap = os.path.abspath(path)
    return os.path.islink(ap) or os.path.basename(real) != os.path.basename(ap)


def open_safe(path, binary=False, strict=True):
    """Common entrance of the way that opens content. Right before opening it redoes realpath and the deny check without a cache (denied_file; dot paths and secret names, hidden_or_secret),
    and opens only a regular file, without following links on any component (open_nofollow). Denied if refused; OSError if it is not a regular file or cannot be opened.
    strict=False (a fixed-name brief such as brief.md) relaxes the dot-path and secret-name rules only for a regular file in place. If the file is reached through a link
    (via_link), its target is refused for dot paths and secret names just as with strict: so a link cannot pull in the content of a hidden or secret file."""
    real = os.path.realpath(path)
    if denied_file(real):
        raise Denied('denied')
    if (strict or via_link(path, real)) and hidden_or_secret(real):
        raise Denied('hidden')
    return open_nofollow(real, binary)


def open_nofollow(real, binary=False):
    """Opens the absolute path real (links already resolved by realpath) without following a symbolic link on any component (it walks down through directory FDs).
    O_NOFOLLOW blocks only the last component, so this keeps a middle folder that is swapped for a link after the check from dragging the open into a forbidden area.
    Opens only regular files (something like a FIFO is refused, not blocked on). Returns a utf-8, errors=replace text file object (a bytes file object if binary=True). OSError on failure."""
    parts = [p for p in real.split(os.sep) if p]
    if not os.path.isabs(real) or not parts:
        raise OSError('절대 경로가 아닙니다')
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for p in parts[:-1]:
            nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(ffd).st_mode):
            raise OSError('일반 파일이 아닙니다')
        return os.fdopen(ffd, 'rb') if binary else os.fdopen(ffd, 'r', encoding='utf-8', errors='replace')
    except BaseException:
        os.close(ffd)
        raise


class Tail:
    """Reads a jsonl file on from where the last read ended. Returns only complete lines.
    If start is given it begins there and drops the first partial line. max_read is the amount read at a time (given only to the Codex tail)."""

    def __init__(self, path, start=None, max_read=None):
        self.path = path
        self.pos = start or 0
        self.buf = b''
        self.skip_partial = bool(start)
        self.max_read = max_read

    def read(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.pos:
            self.pos, self.buf, self.skip_partial = 0, b'', False
        if size == self.pos:
            return []
        with open(self.path, 'rb') as f:
            f.seek(self.pos)
            data = f.read(self.max_read) if self.max_read else f.read()
            self.pos = f.tell()
        if self.skip_partial:
            i = data.find(b'\n')
            if i < 0:
                return []
            data, self.skip_partial = data[i + 1:], False
        lines = (self.buf + data).split(b'\n')
        self.buf = lines.pop()
        return lines

    def behind(self):
        try:
            return os.path.getsize(self.path) > self.pos
        except OSError:
            return False


# A torn line: the writer was cut in the middle of a record and the next record was appended on the same line (seen in real records). Every Claude record
# starts with one of these two openings; they only occur at a structural position, because the quotes inside a JSON string are escaped.
_RECORD_START = re.compile(r'\{"(?:parentUuid|type)":')
_TORN_MAX = 16 << 20          # a line longer than this is not searched for a second record
_TORN_TRIES = 64              # record openings tried on one torn line
_ATTACH_HEAD = b'"type":"attachment"'
_ATTACH_HEAD_S = _ATTACH_HEAD.decode()


def _is_record(d):
    """A top-level record (and not a content block inside one): it has a type and an identity."""
    return isinstance(d, dict) and 'type' in d and ('sessionId' in d or 'session_id' in d or 'uuid' in d)


def _recover(raw):
    """The complete records that can still be read from a line that does not parse as one JSON value: [dict, ...] in order (empty if there is none).
    A record is taken when it ends the line or is followed by the opening of another record, so a content block inside a half record is never taken for a record."""
    if len(raw) > _TORN_MAX:
        return []
    s = raw.decode('utf-8', 'replace')
    dec = json.JSONDecoder()
    starts = [m.start() for m in _RECORD_START.finditer(s)]
    out, pos, tries = [], 0, 0
    for j in starts:
        if j < pos:
            continue
        tries += 1
        if tries > _TORN_TRIES:
            break
        try:
            obj, end = dec.raw_decode(s, j)
        except ValueError:
            continue
        rest = s[end:].lstrip()
        if _is_record(obj) and (not rest or _RECORD_START.match(rest)):
            if not (_ATTACH_HEAD_S in s[j:j + 400] and 'queued_command' not in s[j:end] and '"type":"model"' not in s[j:end]):
                out.append(obj)
            pos = end
    return out


def parse_records(raw):
    """(records, torn): the records of one line of a record file. A good line is one record. A torn line (half a record with the next record glued behind it, or two
    records on one line) gives the complete records that can be read and torn=True; the lost half is dropped. Attachment lines other than queued_command and model are skipped
    unread (they are large and never used), but not when a second record starts inside them."""
    raw = raw.strip()
    if not raw:
        return [], False
    if _ATTACH_HEAD in raw[:400] and b'queued_command' not in raw and b'"type":"model"' not in raw and raw.find(b'{"parentUuid"', 1) < 0:
        return [], False
    try:
        return [json.loads(raw)], False
    except ValueError:
        recs = _recover(raw)
        return recs, True


def parse_record(raw):
    """(record or None, torn): the last record of the line (the freshest state when two are on one line) and whether the line was torn."""
    recs, torn = parse_records(raw)
    return (recs[-1] if recs else None), torn


def load_json_line(raw):
    """The record of one line, or None. A torn line gives the complete record behind the torn half (parse_record says it was torn)."""
    return parse_record(raw)[0]


def claude_session_files():
    """The contents (dicts) of ~/.claude/sessions/<pid>.json. Ones that cannot be read or have another shape are skipped."""
    for f in glob.glob(os.path.join(CLAUDE_HOME, 'sessions', '*.json')):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            yield d


def claude_alive_ids():
    """Ids of the Claude sessions that cannot be called finished: the process is alive, or there is no way to know about the process (board/procs.py).
    Only a nonexistent process or another command's pid is left out."""
    return {d.get('sessionId') for d in claude_session_files() if procs.cmdline_has(d.get('pid'), b'claude') is not False}


BOOT = time.time()     # when the server started: if it changes, the page recounts event numbers from scratch (so past events are not replayed after a restart)
