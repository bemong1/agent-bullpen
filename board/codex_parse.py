"""Pure functions and constants for reading Codex rollout records: decoding a line, picking out tools and text, the start position in a big file, inspecting codex processes."""

import json
import re

from . import procs
from .util import CODEX_SESSIONS, as_text, parse_ts, short_path, trunc


CX_BIG = 64 << 20          # a rollout bigger than this is not read from its beginning (only the last 64 MB, or 2000 line heads)
CX_HEADS = 2000

CX_MAX_READ = 32 << 20     # amount the Codex tail reads at a time
CX_LINE_MAX = 1 << 20      # a line longer than this (a base64 image, etc.) is not decoded as JSON
CX_WINDOW = 30             # allowed span (seconds) from a Bash call to the rollout start. Measured 0.3–5 s
CX_PROMPT_MIN = 40         # number of characters outside variables that the prompt rule needs

CX_TOOL_ORDER = ('apply_patch', 'web__run', 'exec_command', 'view_image', 'write_stdin')

CX_HEAD_RE = re.compile(rb'^\{"timestamp":"([^"]*)",(?:"ordinal":\d+,)?"type":"([a-z_]+)"(?:,"payload":\{"type":"([A-Za-z_]+)")?')
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
    """From the JS of a custom_tool_call exec, the inner tool name and a short description. If there are several, one, in CX_TOOL_ORDER order."""
    js = js or ''
    names = re.findall(r'\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(', js)
    name = next((n for n in CX_TOOL_ORDER if n in names), names[0] if names else 'exec')
    lit = r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)'
    text = ''
    if name in ('exec_command', 'write_stdin'):
        m = re.search(r'\b(?:cmd|chars)\s*:\s*' + lit, js)
        text = _js_str(m.group(1)) if m else ''
    elif name == 'web__run':
        m = re.search(r'\b(?:q|ref_id|pattern)\s*:\s*' + lit, js)
        text = _js_str(m.group(1)) if m else ''
    elif name == 'apply_patch':
        text = ', '.join(short_path(p) for p in re.findall(r'\*\*\* (?:Add|Update|Delete) File: (\S+)', js)[:3])
    elif name == 'view_image':
        m = re.search(r'\bpath\s*:\s*' + lit, js)
        text = short_path(_js_str(m.group(1))) if m else ''
    if not text:
        text = next((ln.strip() for ln in js.splitlines() if ln.strip()), '')
    return name, trunc(text.strip().splitlines()[0] if text.strip() else '', 240)


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


def cx_decode(raw):
    """A rollout line. A line over 1 MB is not decoded; only the line head (time, kind) and the first 600 bytes are returned."""
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) > CX_LINE_MAX:
        m = CX_HEAD_RE.match(raw)
        if not m:
            return None
        return {'ts': parse_ts(m.group(1).decode()), 'type': m.group(2).decode(),
                'pt': (m.group(3) or b'').decode(), 'p': None, 'head': raw[:600]}
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    p = d.get('payload') if isinstance(d.get('payload'), dict) else {}
    return {'ts': parse_ts(d.get('timestamp')), 'type': d.get('type'), 'pt': p.get('type'), 'p': p}


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
