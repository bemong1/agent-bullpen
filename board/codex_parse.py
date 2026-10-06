"""Pure functions and constants for reading Codex rollout records: decoding a line, picking out tools and text, the start position in a big file, inspecting codex processes."""

import json
import re

from . import procs
from .util import CODEX_SESSIONS, as_text, parse_ts, short_path, trunc


CX_BIG = 64 << 20          # a rollout bigger than this is not read from its beginning (only the last 64 MB, or 2000 line heads)
CX_HEADS = 2000

CX_MAX_READ = 32 << 20     # amount the Codex tail reads at a time
CX_LINE_MAX = 1 << 20      # a line longer than this (a base64 image, etc.) is not decoded as JSON ...
CX_ITEM_MAX = 64 << 20     # ... unless it is the item of a command or of a file change (what a write event is made of), which is read in full up to this size: a command's output can be megabytes long
CX_ITEM_HEAD = 1024        # how far into a line the kind of its item is looked for
CX_WRITE_ITEM_RE = re.compile(rb'"item":\{"type":"(?:CommandExecution|FileChange)"')
CX_WINDOW = 30             # allowed span (seconds) from a Bash call to the rollout start. Measured 0.3–5 s
CX_PROMPT_MIN = 40         # number of characters outside variables that the prompt rule needs

CX_TOOL_ORDER = ('apply_patch', 'web__run', 'exec_command', 'view_image', 'write_stdin')

CX_HEAD_RE = re.compile(rb'^\{"timestamp":"([^"]*)",(?:"ordinal":\d+,)?"type":"([a-z_]+)"(?:,"payload":\{"type":"([A-Za-z_]+)")?')
CX_ORD_RE = re.compile(rb'^\{"timestamp":"[^"]*","ordinal":(\d+),')       # the line's own number minus one, when the record has it (a sub-agent's copied history is told by it)
CX_CALL_ID_RE = re.compile(rb'"call_id":"([^"]+)"')

CX_CTX_BLOCK_RE = re.compile(r'<(environment_context|recommended_plugins|user_instructions|permissions[a-z_]*)\b[^>]*>'
                             r'.*?</\1>|# AGENTS\.md instructions for [^\n]*\n+<INSTRUCTIONS>.*?</INSTRUCTIONS>', re.S)


# ---------- Codex helper functions ----------
def _js_str(s):
    """A JS string piece ('…', "…", `…`) as text."""
    if not s:
        return ''
    if s[0] == '"':
        try:
            return json.loads(s)
        except ValueError:
            pass
    return s[1:-1]


def codex_tool(js):
    """From the JS of a custom_tool_call exec, the inner tool name and a short description. If there are several, one, in CX_TOOL_ORDER order.
    The description is a literal the call gives (a command, a path, a query; the session number of a write_stdin): when there is none it is empty, never a line of the JS."""
    js = js or ''
    names = re.findall(r'\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(', js)
    name = next((n for n in CX_TOOL_ORDER if n in names), names[0] if names else 'exec')
    lit = r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)'
    text = ''
    if name == 'exec_command':
        m = re.search(r'\bcmd\s*:\s*' + lit, js)
        text = _js_str(m.group(1)) if m else ''
    elif name == 'write_stdin':            # what is typed into the process is not shown: the session it goes to is
        m = re.search(r'\bsession_id\s*:\s*[\'"]?(\d+)', js)
        text = 'session ' + m.group(1) if m else ''
    elif name == 'web__run':
        m = re.search(r'\b(?:q|ref_id|pattern)\s*:\s*' + lit, js)
        text = _js_str(m.group(1)) if m else ''
    elif name == 'apply_patch':
        text = ', '.join(short_path(p) for p in re.findall(r'\*\*\* (?:Add|Update|Delete) File: (\S+)', js)[:3])
    elif name == 'view_image':
        m = re.search(r'\bpath\s*:\s*' + lit, js)
        text = short_path(_js_str(m.group(1))) if m else ''
    return name, trunc(text.strip().splitlines()[0] if text.strip() else '', 240)


EXEC_CMD_MAX = 64 << 10      # a command longer than this is not read out of an exec call that has not ended


def codex_exec_command(js):
    """(cmd, workdir) of the one shell command an exec call makes (`tools.exec_command({cmd: "…", workdir: "…"})`), written out in full, or None when the call makes none, several (parallel
    commands: nothing says which one a process belongs to), or one whose text is not a literal."""
    js = js if isinstance(js, str) else ''
    if 'exec_command' not in js:
        return None
    lit = r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`$]*`)'
    found = re.findall(r'\bcmd\s*:\s*' + lit, js)
    if len(found) != 1 or len(found[0]) > EXEC_CMD_MAX:
        return None
    where = re.findall(r'\bworkdir\s*:\s*' + lit, js)
    return _js_str(found[0]), (_js_str(where[0]) if len(where) == 1 else None)


def codex_call(pt, p):
    """(tool name, short description) of one custom_tool_call or function_call line."""
    if pt == 'custom_tool_call':
        return codex_tool(p.get('input'))
    return p.get('name') or 'function', trunc(p.get('arguments') or '', 240)


def codex_user_text(p):
    """The text of a response_item message (user). Strips attached heads such as <environment_context> and AGENTS.md; '' if nothing is left."""
    return CX_CTX_BLOCK_RE.sub('', as_text(p.get('content'))).strip()


def codex_say_text(p):
    return as_text(p.get('content')).strip()


def cx_write_item(raw):
    """Whether a rollout line (bytes) is the `item_completed` of a command or of a file change, judged from its first bytes: the lines that write events are made of."""
    m = CX_HEAD_RE.match(raw)
    return bool(m) and m.group(3) == b'item_completed' and CX_WRITE_ITEM_RE.search(raw, 0, CX_ITEM_HEAD) is not None


def cx_head(raw):
    """{'ts', 'ord', 'type'} of a rollout line (bytes) from its first bytes: the time (None when it cannot be read), the ordinal (None when the line has none) and the kind of the line; None when the
    bytes are no line head."""
    raw = raw.lstrip()
    m = CX_HEAD_RE.match(raw)
    if not m:
        return None
    om = CX_ORD_RE.match(raw)
    return {'ts': parse_ts(m.group(1).decode('utf-8', 'replace')), 'ord': int(om.group(1)) if om else None, 'type': m.group(2).decode()}


CX_ITEM_TYPE_RE = re.compile(rb'"item":\{"type":"[A-Za-z]+"')


def cx_write_item_of(row):
    """Whether a decoded row (`cx_decode`) is the `item_completed` of a command or of a file change."""
    p = row.get('p')
    item = p.get('item') if isinstance(p, dict) else None
    return row.get('pt') == 'item_completed' and isinstance(item, dict) and item.get('type') in ('CommandExecution', 'FileChange')


def cx_cut_head(head):
    """Whether the first bytes of a rollout line (bytes) are a line that was cut before the kind of what it holds could be read: an `item_completed` with no readable kind of item, or an `event_msg` with no
    readable kind of payload. Such a line may be a write."""
    m = CX_HEAD_RE.match(head)
    if m is None:
        return False
    if m.group(3) is None:
        return m.group(2) == b'event_msg'
    return m.group(3) == b'item_completed' and CX_WRITE_ITEM_RE.search(head) is None and CX_ITEM_TYPE_RE.search(head) is None


def cx_broken(raw):
    """`cx_head` of a line that is no JSON and that is, by its head, the item of a command or of a file change (a write that cannot be read) or a line that was cut before that could be read
    (`cx_cut_head`: it may be a write); None for any other line."""
    raw = raw.strip()
    if not raw:
        return None
    head = raw[:CX_ITEM_HEAD]
    return cx_head(raw) if (cx_write_item(raw) or cx_cut_head(head)) else None


def cx_hole(bad, prev_ts, nxt):
    """The hole a write line that cannot be read leaves in the record, (t0, t1), or None when it leaves none (CONTRACT O17). It leaves none when it is the last line of a process that died
    writing it: nothing follows it (`nxt` None), or what follows it is a process that begins (a `session_meta`, or a line whose ordinal does not go on from the broken one: it is at most it, as a process
    that took up the file again counts from the lines it could read). Else it is the time between the line before it (`prev_ts`) and the line after it (`nxt`), with its own, when they are known.
    `bad` and `nxt` are `cx_head`s ({} for a line whose head could not be read)."""
    if nxt is None or nxt.get('type') == 'session_meta':
        return None
    if bad.get('ord') is not None and nxt.get('ord') is not None and nxt['ord'] <= bad['ord']:
        return None
    times = [t for t in (prev_ts, bad.get('ts'), nxt.get('ts')) if t is not None]
    return (min(times), max(times)) if times else None


def cx_decode(raw):
    """A rollout line. A line over 1 MB is not decoded; only the line head (time, kind) and the first 600 bytes are returned, unless it is the item of a command or of a file change (a write is made
    of it), which is decoded in full up to CX_ITEM_MAX. `ord` is the line's ordinal (None when it has none)."""
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) > CX_LINE_MAX:
        m = CX_HEAD_RE.match(raw)
        if not m:
            return None
        om = CX_ORD_RE.match(raw)
        if len(raw) <= CX_ITEM_MAX and cx_write_item(raw):
            try:
                d = json.loads(raw)
            except (ValueError, RecursionError):
                return None                                  # cut short (a writer that died in the middle of it): the reader of the rows judges it (`cx_rows`, `cx_hole`)
            if isinstance(d, dict):
                p = d.get('payload') if isinstance(d.get('payload'), dict) else {}
                return {'ts': parse_ts(d.get('timestamp')), 'type': d.get('type'), 'ord': int(om.group(1)) if om else None, 'pt': p.get('type'), 'p': p}
        return {'ts': parse_ts(m.group(1).decode()), 'type': m.group(2).decode(), 'ord': int(om.group(1)) if om else None,
                'pt': (m.group(3) or b'').decode(), 'p': None, 'head': raw[:600]}
    try:
        d = json.loads(raw)
    except (ValueError, RecursionError):
        return None                                          # a line that is no JSON (a writer that died in the middle of it) is skipped, as the Claude reader skips one: `lost` is for what the limits of the reader left unread
    p = d.get('payload') if isinstance(d.get('payload'), dict) else {}
    o = d.get('ordinal')
    return {'ts': parse_ts(d.get('timestamp')), 'type': d.get('type'), 'ord': o if isinstance(o, int) and not isinstance(o, bool) else None, 'pt': p.get('type'), 'p': p}


def cx_start_offset(path, size):
    """For 64 MB or more, the start position within the last 64 MB or 2000 line heads from the end. 0 if smaller."""
    if size < CX_BIG:
        return 0
    pos, heads, floor = size, 0, size - CX_BIG
    try:
        with open(path, 'rb') as f:
            while pos > floor and heads < CX_HEADS:
                step = min(8 << 20, pos - floor)
                pos -= step
                f.seek(pos)
                heads += f.read(step).count(b'\n')
    except OSError:
        return 0
    return pos


def cx_usage_add(acc, u):
    for k in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
              'reasoning_output_tokens'):
        acc[k] = acc.get(k, 0) + (u.get(k) or 0)


def cx_procs():
    """Command lines of codex processes and open rollout paths (cached for 3 seconds, board/procs.py). Never raises.
    If known (whether the process list was obtained) or fd_known (whether open files are known too) is False, the judgments below become None (unknown)."""
    return procs.codex_procs(CODEX_SESSIONS)


def cx_open(path, tid, pr):
    """Whether a codex process has the rollout open (an open file, or the id in a command line): True / False (none) / None (unknown: ps does not know open files)."""
    if path in pr['fds'] or any(tid.encode() in c for c in pr['cmd']):
        return True
    if not pr.get('known', True):
        return None
    return False if pr.get('fd_known', True) or not pr['any'] else None


def cx_alive(e):
    """Process of a standalone Codex session: True (id in a command line, or an open rollout) / False (no codex process, or a finished exec) / None (unknown)."""
    pr = cx_procs()
    if cx_open(e['path'], e['id'], pr):
        return True
    if e['origin'] == 'exec' and not e['open']:
        return False
    if not pr.get('known', True):
        return None
    return None if pr['any'] else False
