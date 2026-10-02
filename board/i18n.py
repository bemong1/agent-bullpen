"""Registration, reading and substitution of the UI dictionaries (static/locales/<code>.json), shared by the pages and the terminal, and the terminal language choice.
Standard library only, and independent of the other board modules (so a module at the bottom of the chain can print a terminal message through it too).

A dictionary file = {"meta": {code, name, locale}, "messages": {key: text | {one, other, ...}}, "formats": {...}, "limits": {...}}.
Registered = a well-formed regular file <code>.json directly under static/locales/ (links, FIFOs, names starting with `_`, and subfolders are not registered).
A new language is one JSON file. The server rescans the folder on every request and rereads only the files that changed.
"""

import json
import os
import re
import stat

LOCALES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'locales')   # tests replace it
DEFAULT = 'en'
FILE_RE = re.compile(r'([a-z]{2,3}(?:-[A-Za-z0-9]+)*)\.json')       # a registrable file name (fullmatch); the code is the file name
FILE_MAX = 1 << 20                                                 # size limit of one dictionary
ENV_ORDER = ('AGENT_BULLPEN_LANG', 'LC_ALL', 'LC_MESSAGES', 'LANG')   # environment variables that pick the terminal language (earlier wins)
PARAM_RE = re.compile(r'\{([A-Za-z_]\w*)(?::([A-Za-z]+))?\}')      # {name} and {name:formatter}

CLI_LANG = DEFAULT        # the terminal language; server.main sets it (set_cli_lang) so a module below the server can print a terminal text too
_cache = {}        # path -> ((mtime_ns, size, ino), entry | problem text)
_problems = {}     # file name -> why it was not registered, as of the last scan


def _read_regular(path):
    """Reads a regular file as bytes (a link as the last component is refused, and a FIFO is refused without blocking). OSError on failure."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError('not a regular file')
        if st.st_size > FILE_MAX:
            raise OSError('too large')
        return f.read(FILE_MAX + 1)


def _shape_problem(code, pack):
    """What is wrong with the shape of a dictionary (None when nothing). Key lists and placeholders are checked by tools/regress/i18n_check.py, not here."""
    if not isinstance(pack, dict) or not isinstance(pack.get('messages'), dict):
        return 'no "messages" object'
    meta = pack.get('meta')
    if not isinstance(meta, dict) or meta.get('code') != code:
        return 'meta.code is not "%s"' % code
    if not isinstance(meta.get('name'), str) or not meta['name']:
        return 'meta.name missing'
    for k, v in pack['messages'].items():
        if not (isinstance(v, str) or (isinstance(v, dict) and v and all(isinstance(x, str) for x in v.values()))):
            return 'message "%s" is neither a string nor an object of strings' % k
    for part in ('formats', 'limits'):
        if part in pack and not isinstance(pack[part], dict):
            return '"%s" is not an object' % part
    return None


def _entry(path, code):
    """(entry, None) or (None, reason)."""
    try:
        data = _read_regular(path)
        pack = json.loads(data.decode('utf-8'))
    except (OSError, ValueError) as e:
        return None, '%s: %s' % (type(e).__name__, e)
    why = _shape_problem(code, pack)
    if why:
        return None, why
    return {'code': code, 'name': pack['meta']['name'], 'file': code + '.json', 'data': data, 'pack': pack}, None


def registry():
    """{code: {code, name, file, data (the file's bytes), pack (the parsed dictionary)}}: the registered languages. Empty when the folder is missing."""
    out, problems = {}, {}
    try:
        names = sorted(os.listdir(LOCALES_DIR))
    except OSError:
        names = []
    for name in names:
        m = FILE_RE.fullmatch(name)
        if not m:
            continue
        path = os.path.join(LOCALES_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            problems[name] = 'not a regular file'
            continue
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = _cache.get(path)
        if not hit or hit[0] != sig:
            hit = _cache[path] = (sig, _entry(path, m.group(1)))
        entry, why = hit[1]
        if entry:
            out[entry['code']] = entry
        else:
            problems[name] = why
    _problems.clear()
    _problems.update(problems)
    return out


def problems():
    """{file name: reason} for the files the last registry() could not register. The server reports them at start."""
    registry()
    return dict(_problems)


def languages():
    """[{code, name}], English first and the rest by code: what the page's language selector and /api/i18n use."""
    reg = registry()
    return [{'code': c, 'name': reg[c]['name']} for c in sorted(reg, key=lambda c: (c != DEFAULT, c))]


def default_code():
    """English when it is registered or nothing is; otherwise the first registered language (what the page falls back to)."""
    langs = languages()
    return DEFAULT if not langs or any(l['code'] == DEFAULT for l in langs) else langs[0]['code']


def match(tag, supported):
    """A language tag matched to a supported code: ko_KR.UTF-8, ko-KR, KO -> ko. Case-insensitive, the exact tag first, then with trailing parts dropped. None when
    nothing matches (C and POSIX included)."""
    if not isinstance(tag, str):
        return None
    parts = [p for p in re.split(r'[.@]', tag.strip(), maxsplit=1)[0].lower().replace('_', '-').split('-') if p]
    for n in range(len(parts), 0, -1):
        want = '-'.join(parts[:n])
        for code in supported:
            if code.lower() == want:
                return code
    return None


def cli_lang(flag=None, environ=None):
    """The terminal language: --lang, AGENT_BULLPEN_LANG, LC_ALL, LC_MESSAGES, LANG, then English.
    An empty value and `auto` count as not set. The first value that is set decides, and when it is not a supported language (C, POSIX, fr_FR ...) the later
    variables are not looked at: English (earlier variables win, as in POSIX). AGENT_BULLPEN_LANG is how tests and tools pin the server's language."""
    env = os.environ if environ is None else environ
    supported = list(registry())
    for v in (flag,) + tuple(env.get(k) for k in ENV_ORDER):
        v = (v or '').strip()
        if v and v.lower() != 'auto':
            return match(v, supported) or DEFAULT
    return DEFAULT


def set_cli_lang(lang):
    global CLI_LANG
    CLI_LANG = lang


def cli_t(key, **params):
    """A terminal text in the language set by set_cli_lang (English until it is set)."""
    return translator(CLI_LANG)(key, **params)


def subject(word):
    """The Korean subject particle i/ga: i when the last character is a Hangul syllable with a final consonant, a digit 0 1 3 6 7 8 or a letter l m n r;
    otherwise ga. The same rule as gaI in static/i18n.js."""
    c = str(word)[-1:]
    if not c:
        return '가'
    k = ord(c)
    final = (k - 0xAC00) % 28 if 0xAC00 <= k <= 0xD7A3 else 1 if re.match(r'[013678lmnrLMNR]', c) else 0
    return '이' if final else '가'


def _number(v):
    return format(v, ',') if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


FORMATTERS = {'subject': lambda v: str(v) + subject(v), 'number': _number}


def fill(text, params):
    """Replaces {name} in text from params; a name without a value stays as {name}. {name:subject} puts i/ga after the value, {name:number} groups thousands."""
    def one(m):
        v = params.get(m.group(1))
        if v is None:
            return m.group(0)
        f = FORMATTERS.get(m.group(2))
        return f(v) if f else str(v)
    return PARAM_RE.sub(one, text)


def translator(lang):
    """t(key, **params) -> text: the chosen language, then English, then '[key]'. Terminal texts are written without a plural algorithm, so a {one, other} value gives `other`."""
    reg = registry()
    packs = [reg[c]['pack']['messages'] for c in ((lang, DEFAULT) if lang != DEFAULT else (DEFAULT,)) if c in reg]

    def t(key, **params):
        for messages in packs:
            v = messages.get(key)
            if isinstance(v, dict):
                v = v.get('other')
            if isinstance(v, str):
                return fill(v, params)
        return '[%s]' % key
    return t
